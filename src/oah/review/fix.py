# 리뷰 결과 수정 — 최신 영수증의 P0/P1 발견을 Codex 작업자로 리스 작업 트리에서 한 번 고치고, 시도의 시작과 단 하나의 결과를 기록하며, 커밋 명령을 보여 주고 커밋을 시도에 묶는다.
"""`oah fix` without the command line (PLAN 5.9, A1, A6, A7, A17, A22).

The dispatcher (the caller of these functions) owns every record; the corrector owns nothing. Each operation
holds the task's review lock (`receipts.task_lock_path`, shared with `oah review`) and writes only through
`receipts.publish`, in P6a's format. Nothing is ever rewritten.

`correct(request, ...)` runs one Codex correction.
- Refusals before dispatch, in this order: a dispatched worker (`OAH_FIX_WORKER_REFUSED`); a task without an
  active lease, an unbound lease, or an intent that is not current or not committed at HEAD as bound
  (`OAH_INTENT_NOT_CURRENT`); a correction stage other than Codex; proxy userinfo; a running `oah fix` or
  `oah review` of the task (`OAH_FIX_BUSY`); an unfinished attempt; an applied attempt waiting for its commit;
  any other history problem; no receipt, or a task's latest receipt that is not `changes-requested`; a dirty
  tracked tree; a HEAD that does not descend from the reviewed head; and the findings (the receipt's P0/P1
  findings without an applied attempt, or the ones named).
- The corrector is an `oah.call` worker (stage `correction`, Codex `workspace-write`) in the lease's worktree,
  with the reviewer's allowlisted environment. Its brief reads the packet committed at HEAD, the bytes a review
  snapshot holds, so both briefs carry the same intent section.
- The start record is published before launch, and exactly one outcome follows: `applied` (files changed),
  `no-change`, `failed` (the run did not finish, its effort did not match, or HEAD moved) or `aborted`
  (Ctrl-C or a stop signal, which also stops the corrector). Signals are held while a record is written.
- The diff against the start head is printed and saved in the run folder (`correction.patch`,
  `correction.json`) with the exact commit commands, which end the message with `Agent-Engine: codex`.

`bind(task, ...)` binds the one applied attempt waiting for its commit to HEAD. HEAD must differ from and
descend from its start head, the tracked tree must be clean and the attempt's new files committed, every new
commit needs a known engine (`oah.review.authors`, the rule `oah review` uses) and one of them the attempt's
engine, unless `engine` names who replaced its changes (recorded).

`record_manual(task, engine=...)` records a correction already committed by `claude` or `human`: start,
`applied` outcome and head binding. It starts from the newest bound result on the source receipt that HEAD
contains, else from the reviewed head.

`abort(task, ...)` records `aborted` for attempts whose `oah fix` stopped without an outcome (a hard kill), and
marks such an attempt's run `lost`, as `oah wait` does for a runner that died.

Round instructions
    `round_instructions(...)` fills `templates/review/correction.md`: the source receipt, the findings with
    their ids and class ids, and the class definitions, as bullets plus one fenced JSON block. Placeholders
    are replaced in one pass, so text from a finding is never substituted.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import difflib
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import tempfile
import threading
from importlib import resources
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from oah import briefs, call, call_cli, runs
from oah import intent as oah_intent
from oah.atomic import StateCorrupt, read_json, write_json_atomic, write_text_atomic
from oah.engines.proc import run_command
from oah.lease import Store
from oah.lockfile import LockTimeout, StateLock
from oah.paths import GIT_TIMEOUT, git_argv, repo_info, run_git, state_dir
from oah.review import ReviewError, authors, receipts, rounds, runner
from oah.review.schema import BLOCKING
from oah.scope import outside
from oah.toolkit.checks import CONTENT_RULES, UTF16_BOMS

ENGINE = "codex"
STAGE = "correction"
MANUAL_ENGINES = ("claude", "human")
DIFF_LINES = 400
INTERRUPTED = "OAH_FIX_INTERRUPTED"
OUTCOME_CODES = {"applied": "OAH_FIX_APPLIED", "no-change": "OAH_FIX_NO_CHANGE", "failed": "OAH_FIX_FAILED"}
PLACEHOLDERS = ("TASK", "ATTEMPT", "RECEIPT", "RECEIPT_DIGEST", "BASE", "REVIEWED", "HEAD", "FINDINGS", "CLASSES")
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")
_STOP_SIGNALS = tuple(getattr(signal, name) for name in (("SIGBREAK",) if os.name == "nt" else ("SIGTERM", "SIGHUP"))
                      if hasattr(signal, name))
LAUNCHED, RECORDED = "Nothing was launched", "Nothing was recorded"


@dataclasses.dataclass(frozen=True)
class Request:
    task: str
    findings: tuple[str, ...] = ()
    tier: str | None = None
    timeout_s: float | None = None


@dataclasses.dataclass
class Result:
    code: str
    exit_code: int
    message: str
    lines: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    attempt_id: str | None = None
    outcome: str | None = None
    records: list[dict] = dataclasses.field(default_factory=list)
    commands: list[str] = dataclasses.field(default_factory=list)
    next_command: str | None = None
    run_dir: str | None = None

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


class FixInterrupted(KeyboardInterrupt):
    """Ctrl-C or a stop signal ended `oah fix` after the attempt's outcome was recorded."""

    code = INTERRUPTED

    def __init__(self, message: str) -> None:
        self.message = f"{INTERRUPTED}: {message}"
        super().__init__(self.message)


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _fence(text: str, language: str = "text") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text.rstrip(chr(10))}\n{fence}"


def _line(text: object) -> str:
    return " ".join(str(text).split())


def _ids(items: Iterable[str]) -> str:
    return ", ".join(items)


def _are(items: Sequence[str]) -> str:
    return "is" if len(items) == 1 else "are"


def round_instructions(*, task: str, attempt_id: str, receipt: str, receipt_digest: str, base: str, reviewed: str,
                       head: str, findings: Sequence[Mapping], classes: Sequence[Mapping]) -> str:
    """The correction brief's round instructions; `findings` and `classes` are the receipt's own objects."""
    template = resources.files("oah").joinpath("templates/review/correction.md").read_text(encoding="utf-8")
    listed = "\n".join(f"- {item['finding_id']} · {item['severity']} · class {item['class_id']} · "
                       f"{_line(item.get('axis', ''))} · {_line(item.get('location', ''))} — "
                       f"{_line(item.get('title', ''))}" for item in findings)
    exact = json.dumps({"findings": list(findings), "classes": list(classes)}, ensure_ascii=False, indent=2,
                       sort_keys=True)
    values = {
        "TASK": task, "ATTEMPT": attempt_id, "RECEIPT": receipt, "RECEIPT_DIGEST": receipt_digest, "BASE": base,
        "REVIEWED": reviewed, "HEAD": head,
        "FINDINGS": listed + "\n\nThe findings and their classes exactly as the review recorded them:\n"
                    + _fence(exact, "json"),
        "CLASSES": "\n".join(f"- {item['class_id']}: {_line(item.get('mechanism', ''))}; rule: "
                             f"{_line(item.get('rule', ''))}" for item in classes) or "(none recorded)",
    }
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


# Signals -----------------------------------------------------------------------------------------


@contextlib.contextmanager
def _held_signals() -> Iterator[list[int]]:
    """Hold Ctrl-C and stop signals while a record is written; the caller decides afterwards what they meant."""
    held: list[int] = []
    saved: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, *_STOP_SIGNALS):
            try:
                if signal.getsignal(number) in (signal.SIG_IGN, None):
                    continue  # ignored on purpose (a background job, nohup), or owned by foreign code
                saved[number] = signal.signal(number, lambda signum, _frame: held.append(signum))
            except (OSError, ValueError):
                continue
    try:
        yield held
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


@contextlib.contextmanager
def _stop_signals() -> Iterator[None]:
    """Protect the whole attempt; keep repeated signals from interrupting tree cleanup."""
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise KeyboardInterrupt(f"signal {signum}")

    saved: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, *_STOP_SIGNALS):
            try:
                if signal.getsignal(number) not in (signal.SIG_IGN, None):
                    saved[number] = signal.signal(number, stop)
            except (OSError, ValueError):
                continue
    try:
        yield
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


# The task, its intent and its history ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Task:
    name: str
    state: pathlib.Path
    lease: object  # oah.lease.Lease
    root: pathlib.Path


def _task(task: str, cwd: pathlib.Path, env: Mapping[str, str], nothing: str) -> _Task:
    if env.get("OAH_WORKER") == "1":
        raise ReviewError("OAH_FIX_WORKER_REFUSED", "dispatched workers do not run or record corrections; return your "
                          "work to the dispatcher, who runs oah fix")
    info = repo_info(pathlib.Path(cwd))
    state = state_dir(info, env)
    receipts.review_dir(state, task)
    lease = next((lease for lease, stale in Store(state).snapshot() if lease.task == task and not stale), None)
    if lease is None:
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"task {task} has no active lease, so oah fix has no current intent "
                          f"or worktree to work with. {nothing}. Bind the accepted packet with oah start --task {task} "
                          f"--agent <you> --packet .oah/tasks/{task}/PACKET.md (use your packet's path), then run the "
                          "command again")
    root = pathlib.Path(lease.worktree)
    if not root.is_dir():
        raise ReviewError("OAH_WORKTREE_UNRESOLVED", f"the lease of task {task} names the worktree {root}, which no "
                          f"longer exists. {nothing}. Release it with oah release --task {task} --agent {lease.agent} "
                          '--reason "worktree removed", then start the lease again in the task\'s worktree')
    return _Task(task, state, lease, root)


def _committed_reader(root: pathlib.Path, *, ref: str = "HEAD") -> Callable[[pathlib.Path], str]:
    """Read a committed packet without checkout newline conversion; HEAD remains the dispatch default."""
    def read(path: pathlib.Path) -> str:
        try:
            relative = pathlib.Path(path).relative_to(root).as_posix()
        except ValueError:
            raise oah_intent.SourceError("OAH_INTENT_SOURCE_INVALID", f"{path} is outside the worktree") from None
        shown = run_command([*git_argv(), "-C", str(root), "cat-file", "blob", f"{ref}:{relative}"],
                            timeout=GIT_TIMEOUT, encoding="latin-1")  # latin-1 keeps every byte
        if shown.timed_out or shown.returncode != 0:
            raise oah_intent.SourceError("OAH_INTENT_SOURCE_MISSING", f"{relative} is not committed at {ref}")
        data = shown.stdout.encode("latin-1")
        if len(data) >= oah_intent.MAX_BYTES:
            raise oah_intent.SourceError("OAH_INTENT_SOURCE_INVALID", f"{relative} at {ref} is too large for a packet")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise oah_intent.SourceError("OAH_INTENT_SOURCE_INVALID", f"{relative} at {ref} is not UTF-8 text") from None
        return text[1:] if text.startswith("\ufeff") else text

    return read


def _check_intent(t: _Task, nothing: str) -> oah_intent.Binding:
    binding = t.lease.binding()
    rebind = (f"oah start --task {t.name} --agent {t.lease.agent} --packet "
              f"{binding.source if binding else f'.oah/tasks/{t.name}/PACKET.md'}")
    if binding is None:
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"task {t.name} has a lease without an intent binding (unbound), so "
                          f"there is no current intent to correct against. {nothing}. Bind the accepted packet with "
                          f"{rebind} (use your packet's path), then run the command again")
    found, reason = oah_intent.explain_binding(binding, t.root)
    if found != "current":
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"task {t.name} is bound to {binding.line()} and that intent is "
                          f"{found}: {reason}. {nothing}. Restore {binding.source} as it was bound, or raise its "
                          f"Revision, accept it and rebind with {rebind}; then run the command again")
    try:
        committed = oah_intent.parse(_committed_reader(t.root)(oah_intent.source_path(binding, t.root)))
    except (oah_intent.SourceError, oah_intent.IntentInvalid):
        committed = None
    if committed is None or committed.digest != binding.digest:
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"{binding.source} is not committed at HEAD as bound "
                          f"({binding.line()}). {nothing}. Correction and review briefs carry the committed packet, "
                          "so commit it as bound, then run the command again")
    return binding


def _check_review_intent(t: _Task, receipt: receipts.Entry, binding: oah_intent.Binding, nothing: str) -> None:
    matches = (receipt.record.get("intent") == binding.to_json()
               and receipt.record.get("intent_digest") == binding.digest)
    if matches:
        try:
            source = oah_intent.source_path(binding, t.root)
            reviewed = _committed_reader(t.root, ref=receipt.record["head"])(source)
            current = _committed_reader(t.root)(source)
            matches = (oah_intent.parse(reviewed).bind(binding.source) == binding
                       and briefs.raw_section(reviewed) == briefs.raw_section(current))
        except (oah_intent.SourceError, oah_intent.IntentInvalid):
            matches = False
    if not matches:
        raise ReviewError("OAH_INTENT_SOURCE_MISMATCH", f"the current binding or committed intent section differs "
                          f"from the intent reviewed by {receipt.name}. {nothing}. Run oah review --task {t.name} "
                          "to review the updated intent before correcting its findings")


@dataclasses.dataclass(frozen=True)
class _History:
    records: receipts.Records
    attempts: tuple[receipts.Attempt, ...]

    def start(self, attempt: receipts.Attempt) -> dict | None:
        return next((entry.record for entry in self.records.entries
                     if entry.kind == "attempt-start" and entry.record["attempt_id"] == attempt.attempt_id), None)

    def unfinished(self) -> list[receipts.Attempt]:
        return [item for item in self.attempts if not any(name.endswith("-outcome.json") for name in item.names)]

    def waiting(self) -> list[receipts.Attempt]:
        """Applied attempts whose result is not bound to a commit yet."""
        return [item for item in self.attempts if item.outcome == "applied" and item.after_head is None]

    def problems(self, *, besides: receipts.Attempt | None = None) -> list[str]:
        found = list(self.records.problems)
        for item in self.attempts:
            if besides is None or item.attempt_id != besides.attempt_id:
                found.extend(item.problems)
        return found

    def next_id(self) -> str:
        numbers = [int(entry.record["attempt_id"][1:]) for entry in self.records.entries if entry.kind != "receipt"]
        return f"A{max(numbers, default=0) + 1}"


def _history(t: _Task) -> _History:
    records = receipts.load(t.state, t.name)
    return _History(records, tuple(receipts.attempts(records)))


def _unfinished(t: _Task, attempts: Sequence[receipts.Attempt], nothing: str) -> ReviewError:
    ids = _ids([item.attempt_id for item in attempts])
    return ReviewError("OAH_FIX_ATTEMPT_UNFINISHED", f"{ids} {_are(attempts)} unfinished: the start is recorded but no "
                       "outcome, because the oah fix that started it stopped early (killed or crashed). "
                       f"{nothing}. Make sure "
                       f"no oah fix or Codex process is still running in {t.root}, check git status there for changes "
                       f"it may have left, then run oah fix --abort --task {t.name} to record it as aborted")


def _check_history(t: _Task, h: _History, nothing: str) -> None:
    unfinished = h.unfinished()
    if unfinished:
        raise _unfinished(t, unfinished, nothing)
    waiting = h.waiting()
    if waiting:
        raise ReviewError("OAH_FIX_BIND_REQUIRED", f"{waiting[0].attempt_id} applied a correction that is not bound "
                          f"to a commit yet. {nothing}. Commit it (oah fix printed the commands), then run oah fix "
                          f"--bind --task {t.name}; the next correction or review needs that binding first")
    problems = h.problems()
    if problems:
        raise ReviewError("OAH_FIX_HISTORY_INVALID", f"the review history of task {t.name} is incomplete or was changed, "
                          f"so no correction can be recorded against it. {nothing}:\n- " + "\n- ".join(problems) +
                          "\nRestore or complete the records first, then run the command again")


def _source(t: _Task, h: _History, nothing: str) -> receipts.Entry:
    found = h.records.receipts()
    if not found:
        raise ReviewError("OAH_FIX_NOTHING_TO_FIX", f"task {t.name} has no review receipt yet, so there are no findings "
                          f"to correct. {nothing}. Run oah review --task {t.name} first")
    latest = found[-1]
    history = rounds.series(h.records, receipts.read_round_log(t.state, t.name), latest.record["base"],
                            head=_head(t.root), is_ancestor=lambda a, b: _is_ancestor(t.root, a, b))
    stopped = rounds.decision_stop(history)
    if stopped:
        raise ReviewError("OAH_FIX_NOT_REQUESTED", f"task {t.name} is waiting for a decision: {stopped}. {nothing}. "
                          f'Decide how to continue, then run oah review --task {t.name} --decision "<what you decided>"')
    outcome = latest.record["outcome"]
    if outcome == "converged":
        raise ReviewError("OAH_FIX_NOT_REQUESTED", f"{latest.name} converged (the review approved head "
                          f"{latest.record['head'][:12]}), so there is nothing to correct. {nothing}. Run oah review "
                          f"--task {t.name} for new work, or finish the task")
    if outcome != "changes-requested":
        raise ReviewError("OAH_FIX_NOT_REQUESTED", f"{latest.name} stopped the series for a decision "
                          f"({latest.record.get('decision_stop_reason')}), so oah fix does not correct it. {nothing}. "
                          f'Decide how to continue, then run oah review --task {t.name} --decision "<what you decided>"')
    return latest


def _git_failed(root: pathlib.Path, what: str, result) -> ReviewError:
    detail = "timed out" if result.timed_out else (result.stderr.strip() or "no output")
    return ReviewError("OAH_FIX_GIT_FAILED", f"git could not {what} in {root} ({detail}); check the repository and retry")


def _head(root: pathlib.Path) -> str:
    result = run_git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    head = result.stdout.strip()
    if result.returncode != 0 or not receipts.SHA_RE.fullmatch(head):
        raise _git_failed(root, "read HEAD", result)
    return head


def _is_ancestor(root: pathlib.Path, commit: str, head: str) -> bool:
    return run_git(root, "merge-base", "--is-ancestor", commit, head).returncode == 0


def _names(root: pathlib.Path, *args: str) -> list[str]:
    result = run_git(root, *args)
    if result.returncode != 0 or result.timed_out:
        raise _git_failed(root, "list the changed files", result)
    return [name for name in result.stdout.split("\0") if name]


def _untracked(root: pathlib.Path) -> list[str]:
    return _names(root, "ls-files", "--others", "--exclude-standard", "-z")


def _check_tree(t: _Task, receipt: receipts.Entry, head: str, nothing: str) -> None:
    dirty = _tracked_dirty(t.root)
    if dirty:
        listed = _ids(dirty[:10]) + (f" and {len(dirty) - 10} more" if len(dirty) > 10 else "")
        raise ReviewError("OAH_FIX_DIRTY_TREE", f"tracked files have uncommitted changes ({listed}). {nothing}. A "
                          "correction is recorded against a clean, committed tree, so commit or stash them first, then "
                          "run the command again")
    reviewed = receipt.record["head"]
    if not _is_ancestor(t.root, reviewed, head):
        raise ReviewError("OAH_FIX_HEAD_UNRELATED", f"HEAD {head} does not descend from {reviewed}, the head "
                          f"{receipt.name} reviewed (the branch was switched or rewritten). {nothing}. Check out the "
                          f"reviewed branch, or review the current head first with oah review --task {t.name}")


def _select(t: _Task, receipt: receipts.Entry, h: _History, chosen: Sequence[str], nothing: str) -> list[dict]:
    blocking = [item for item in receipt.record["findings"] if item.get("severity") in BLOCKING]
    known = [item["finding_id"] for item in blocking]
    if chosen:
        unknown = [ident for ident in dict.fromkeys(chosen) if ident not in known]
        if unknown:
            raise ReviewError("OAH_FIX_FINDING_UNKNOWN", f"{_ids(unknown)} {_are(unknown)} not a P0 or P1 finding of "
                              f"{receipt.name}. {nothing}. Its P0/P1 findings are "
                              f"{_ids(known) or 'none'}; name one of them with --finding, or leave --finding out")
        return [item for item in blocking if item["finding_id"] in set(chosen)]
    applied: dict[str, str] = {}
    for attempt in h.attempts:
        if attempt.source_receipt_digest == receipt.digest and attempt.outcome == "applied":
            for ident in attempt.finding_ids:
                applied.setdefault(ident, attempt.attempt_id)
    picked = [item for item in blocking if item["finding_id"] not in applied]
    if not picked:
        raise ReviewError("OAH_FIX_NOTHING_TO_FIX", f"every P0/P1 finding of {receipt.name} already has an applied "
                          f"correction ({_ids(sorted(set(applied.values())))}). {nothing}. Run oah review --task "
                          f"{t.name} for the next round, or name a finding with --finding to correct it again")
    return picked


def _classes(h: _History, findings: Sequence[Mapping]) -> tuple[list[str], list[dict]]:
    defined: dict[str, dict] = {}
    for entry in h.records.receipts():
        for item in entry.record.get("classes", []):
            defined.setdefault(item["class_id"], item)
    ids = list(dict.fromkeys(item["class_id"] for item in findings))
    return ids, [defined.get(ident, {"class_id": ident, "mechanism": "(not recorded)", "rule": "(not recorded)"})
                 for ident in ids]


@contextlib.contextmanager
def _task_lock(t: _Task) -> Iterator[None]:
    lock = StateLock(receipts.task_lock_path(t.state, t.name), timeout=0)
    try:
        lock.__enter__()
    except LockTimeout:
        raise ReviewError("OAH_FIX_BUSY", f"another oah fix or oah review of task {t.name} is running; wait for it to "
                          "finish, then run the command again") from None
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _redacted(value, redact: Callable[[str], str]):
    """Redact decoded strings before serialization, including filenames and JSON keys."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redacted(item, redact) for item in value]
    if isinstance(value, dict):
        return {redact(key): _redacted(item, redact) for key, item in value.items()}
    return value


def _guarded(env: Mapping[str, str], operation: Callable[[Callable[[str], str]], Result]) -> Result:
    """Redact every output string; executable guidance is checked before it reaches this boundary."""
    redact = runner.credential_redactor(env)
    try:
        return Result(**_redacted(operation(redact).to_json(), redact))
    except BaseException as exc:
        message = str(exc)
        if redact(message) == message:
            raise
        code = getattr(exc, "code", "OAH_FIX_FAILED")
        message = redact(message)
        message = message[len(code) + 2:] if message.startswith(code + ": ") else message
        safe = FixInterrupted(message) if isinstance(exc, KeyboardInterrupt) else ReviewError(code, message)
    raise safe from None


# Changes in the worktree -------------------------------------------------------------------------


def _git_bytes(root: pathlib.Path, *args: str, ok: tuple[int, ...] = (0,)) -> bytes:
    result = run_command([*git_argv(), "-C", str(root), *args], timeout=GIT_TIMEOUT, encoding="latin-1")
    if result.timed_out or result.returncode not in ok:
        raise _git_failed(root, "show the change", result)
    return result.stdout.encode("latin-1")


@dataclasses.dataclass(frozen=True)
class _Changes:
    tracked: tuple[str, ...]  # changed, added in the index, or deleted, against the start head
    new: tuple[str, ...]  # untracked files that did not exist before the run
    user: tuple[str, ...] = ()  # preexisting untracked files changed by the run; never staged automatically

    @property
    def files(self) -> list[str]:
        return list(dict.fromkeys((*self.tracked, *self.new, *self.user)))


def _file_snapshot(root: pathlib.Path, name: str) -> tuple[int, bytes] | None:
    """Keep user-owned bytes in memory only, without following a file symlink."""
    path = root / name
    try:
        # A replaced parent directory must not make even discovery read outside the worktree.
        if not path.parent.resolve().is_relative_to(root.resolve()):
            return stat.S_IFDIR, b""
        info = path.lstat()
        kind = stat.S_IFMT(info.st_mode)
        data = (os.fsencode(os.readlink(path)) if stat.S_ISLNK(info.st_mode) else
                path.read_bytes() if stat.S_ISREG(info.st_mode) else b"")
        if stat.S_ISDIR(info.st_mode):
            # Detect dirty nested repositories without invoking their filters or following their links.
            digest = hashlib.sha256()
            if (path / ".git").exists():
                for args in (("rev-parse", "--verify", "HEAD"), ("ls-files", "--stage", "-z")):
                    digest.update(_git_bytes(path, *args))
            for child in sorted(path.rglob("*")):
                relative = child.relative_to(path)
                if ".git" in relative.parts:
                    continue
                snapshot = (_file_snapshot(root, child.relative_to(root).as_posix())
                            if child.is_symlink() or not child.is_dir() else None)
                if snapshot is not None:
                    for part in (os.fsencode(relative), str(snapshot[0]).encode("ascii"), snapshot[1]):
                        digest.update(len(part).to_bytes(8, "big"))
                        digest.update(part)
            data = digest.digest()
        return kind | (info.st_mode & 0o111), data
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReviewError("OAH_FIX_GIT_FAILED", f"cannot inspect file {name!r}: {exc}; check its permissions "
                          "and retry oah fix") from None


def _tree_files(root: pathlib.Path, before: str) -> dict[str, tuple[int, str]]:
    found = {}
    for entry in _git_bytes(root, "ls-tree", "-r", "-z", before).split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, _kind, oid = metadata.split()
            found[os.fsdecode(name)] = int(mode, 8), oid.decode("ascii")
    return found


def _index_files(root: pathlib.Path) -> dict[str, tuple[int, str]]:
    found = {}
    for entry in _git_bytes(root, "ls-files", "--stage", "-z").split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, oid, _stage = metadata.split()
            found[os.fsdecode(name)] = int(mode, 8), oid.decode("ascii")
    return found


def _index_state(root: pathlib.Path) -> dict[str, tuple[bytes, ...]]:
    """Include every stage, mode and object id, without refreshing the index or invoking filters."""
    found: dict[str, list[bytes]] = {}
    for entry in _git_bytes(root, "ls-files", "--stage", "-z").split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            found.setdefault(os.fsdecode(name), []).append(metadata)
    return {name: tuple(entries) for name, entries in found.items()}


def _tracked_dirty(root: pathlib.Path) -> list[str]:
    """Compare tree/index identities and raw file bytes; Git never converts working-tree content."""
    tree, index = _tree_files(root, "HEAD"), _index_files(root)
    stages = _index_state(root)
    filemode = _git_bytes(root, "config", "--bool", "core.filemode", ok=(0, 1)).strip() != b"false"

    def git_mode(mode):
        # Git tracks the owner executable bit on regular files; symlink permission bits are irrelevant.
        kind = stat.S_IFMT(mode)
        return kind | (0o111 if filemode and stat.S_ISREG(mode) and mode & stat.S_IXUSR else 0)

    dirty = []
    for name in sorted(tree.keys() | index.keys()):
        if tree.get(name) != index.get(name) or len(stages.get(name, ())) != 1 or not stages[name][0].endswith(b" 0"):
            dirty.append(name)
            continue
        if tree[name][0] == 0o160000:
            nested = root / name
            if (not nested.is_dir() or nested.is_symlink() or not nested.resolve().is_relative_to(root.resolve())
                    or _head(nested) != tree[name][1]
                    or _tracked_dirty(nested)):
                dirty.append(name)
            continue
        old, current = _blob_snapshot(root, tree[name]), _file_snapshot(root, name)
        if old and current:
            old, current = (git_mode(old[0]), old[1]), (git_mode(current[0]), current[1])
        if old != current:
            dirty.append(name)
    return dirty


def _blob_snapshot(root: pathlib.Path, entry: tuple[int, str] | None) -> tuple[int, bytes] | None:
    if entry is None:
        return None
    mode, oid = entry
    kind = stat.S_IFMT(mode)
    return kind | (mode & 0o111), (_git_bytes(root, "cat-file", "blob", oid) if kind != 0o160000 else b"")


def _changes(root: pathlib.Path, before: str, untracked: Mapping[str, tuple[int, bytes] | None], *,
             baseline: Mapping[str, tuple[int, bytes] | None] | None = None) -> _Changes:
    tree, index = _tree_files(root, before), _index_files(root)
    tracked = []
    for name in sorted(tree.keys() | index.keys() | (baseline.keys() if baseline is not None else set())):
        old = baseline.get(name) if baseline is not None else _blob_snapshot(root, tree.get(name))
        gitlink = any(entry and entry[0] == 0o160000 for entry in (tree.get(name), index.get(name)))
        if old != _file_snapshot(root, name) or (gitlink and tree.get(name) != index.get(name)):
            tracked.append(name)
    user = tuple(name for name, snapshot in untracked.items()
                 if name in tracked or _file_snapshot(root, name) != snapshot)
    return _Changes(tuple(name for name in tracked if name not in untracked),
                    tuple(name for name in _untracked(root) if name not in untracked), user)


def _scan_files(root: pathlib.Path, before: str, changes: _Changes,
                untracked: Mapping[str, tuple[int, bytes] | None],
                redact: Callable[[str], str]) -> tuple[dict[str, str], dict[str, str], dict]:
    """Capture and scan both raw sides once; only these bytes may enter the patch."""
    found, unsupported, captured = {}, {}, {}
    tree, index = _tree_files(root, before), _index_files(root)
    for position, name in enumerate(changes.files, 1):
        current = _file_snapshot(root, name)
        if current and stat.S_ISDIR(current[0]):
            unsupported[name] = f"[file {position}]"
        if any(entry and entry[0] == 0o160000 for entry in (tree.get(name), index.get(name))):
            unsupported[name] = f"[file {position}]"
        old = untracked[name] if name in untracked else _blob_snapshot(root, tree.get(name))
        for side in (old, current):
            if side and stat.S_ISDIR(side[0]):
                unsupported[name] = f"[file {position}]"
            if side and stat.S_ISLNK(side[0]):
                try:
                    target = (root / name).parent / os.fsdecode(side[1])
                    if not target.resolve().is_relative_to(root.resolve()):
                        unsupported[name] = f"[file {position}]"
                except (OSError, ValueError, RuntimeError):
                    unsupported[name] = f"[file {position}]"
        data = [side[1] for side in (old, current) if side]
        texts = [raw.decode("utf-8", errors="replace") for raw in data]
        texts += [raw.decode("utf-16", errors="replace") for raw in data if raw.startswith(UTF16_BOMS)]
        if any(redact(text) != text or any(pattern.search(text) for rule, pattern in CONTENT_RULES
                                         if rule in ("key", "env-secret")) for text in texts):
            found[name] = f"[file {position}]"
        if name not in found and name not in unsupported:
            captured[name] = old, current
    return found, unsupported, captured


def _patch(root: pathlib.Path, before: str, changes: _Changes,
           untracked: Mapping[str, tuple[int, bytes] | None],
           redact: Callable[[str], str] | None = None, *, captured: Mapping | None = None) -> bytes:
    if captured is None:
        sensitive, unsupported, captured = _scan_files(root, before, changes, untracked, redact or (lambda text: text))
        if sensitive or unsupported:
            raise ReviewError("OAH_FIX_UNSAFE_CONTENT", "diff inputs could not be safely scanned; restore the affected "
                              "files before retrying oah fix")
    parts = []
    for name in changes.files:
        old, new = captured[name]
        before_data, after_data = old[1] if old else b"", new[1] if new else b""
        parts.append(f"diff --git {json.dumps('a/' + name)} {json.dumps('b/' + name)}\n".encode("utf-8"))
        if old is None or new is None or old[0] != new[0]:
            parts.append(f"file mode {old[0] if old else 'absent'} -> {new[0] if new else 'absent'}\n".encode("ascii"))
        try:
            before_data.decode("utf-8")
            after_data.decode("utf-8")
            binary = b"\0" in before_data or b"\0" in after_data
        except UnicodeDecodeError:
            binary = True
        if binary:
            parts.append(f"Binary files differ: old binary, {len(before_data)} bytes; "
                         f"new binary, {len(after_data)} bytes\n".encode("ascii"))
        else:
            for line in difflib.diff_bytes(difflib.unified_diff, before_data.splitlines(keepends=True),
                                          after_data.splitlines(keepends=True),
                                          fromfile=json.dumps("a/" + name).encode("ascii") if old else b"/dev/null",
                                          tofile=json.dumps("b/" + name).encode("ascii") if new else b"/dev/null"):
                parts.append(line if line.endswith(b"\n") else line + b"\n\\ No newline at end of file\n")
    return b"".join(parts)


def _git_word(path: str) -> str | None:
    """A path as one shell word that sh and PowerShell read the same way; None when no quoting is safe.

    A leading `-` is quoted as a literal pathspec too: Windows PowerShell 5.1 may drop `--` for native commands."""
    if _SAFE_PATH.fullmatch(path) and not path.startswith("-"):
        return path
    if "'" in path or not path.isprintable():
        return None
    return f"':(literal){path}'"


def commit_commands(*, task: str, attempt_id: str, receipt: str, finding_ids: Sequence[str],
                    changes: _Changes, root: pathlib.Path) -> tuple[list[str], list[str]]:
    """The exact commands that commit a Codex correction, and the new files they could not name."""
    directory = root.as_posix()
    if not directory.isprintable():
        raise ReviewError("OAH_FIX_UNQUOTABLE_PATH", "the leased worktree directory contains control characters, so "
                          "commit commands were withheld; use a worktree path without control characters, inspect "
                          "the changes there and retry oah fix")
    # Single quotes protect both shells' metacharacters; concatenate a double-quoted apostrophe when needed.
    command = "git -C '" + directory.replace("'", "'\"'\"'") + "'"
    commands = [command + " add -u"] if changes.tracked else []
    words, unquoted = [], []
    for name in changes.new:
        word = _git_word(name)
        (words if word else unquoted).append(word or name)
    if words:
        commands.append(command + " add -- " + " ".join(words))
    named = _ids(finding_ids) if len(finding_ids) <= 4 else f"{len(finding_ids)} findings"
    commands.append(f'{command} commit -m "Correct {named} of {receipt} in task {task} (oah fix {attempt_id})" '
                    '-m "Agent-Engine: codex"')
    return commands, unquoted


# The correction run ------------------------------------------------------------------------------


def correct(request: Request, *, cwd: pathlib.Path, env: Mapping[str, str], keychain=None,
            rules: Sequence[Mapping] | None = None, temp_root: pathlib.Path | None = None) -> Result:
    """Run one Codex correction of the task's latest review; refusals raise ReviewError before any launch."""
    return _guarded(env, lambda redact: _correct(request, cwd=cwd, env=env, keychain=keychain, rules=rules,
                                                 temp_root=temp_root, redact=redact))


def _correct(request: Request, *, cwd, env, keychain, rules, temp_root, redact) -> Result:
    t = _task(request.task, cwd, env, LAUNCHED)
    binding = _check_intent(t, LAUNCHED)
    settings = call_cli.load_settings(t.root)
    stage = settings.stages.get(STAGE)
    if stage != ENGINE:
        raise ReviewError("OAH_FIX_ENGINE_UNSUPPORTED", f"[stages] correction is {stage!r} in .oah/config.toml, and oah "
                          f"fix runs Codex corrections. {LAUNCHED}. Set [stages] correction = \"codex\" and install "
                          f"Codex, or record a correction Claude made with oah fix --manual --engine claude --task "
                          f"{t.name}")
    try:
        launch_env = runner.reviewer_env(env)
    except ReviewError as exc:
        if exc.code != "OAH_REVIEW_PROXY_CREDENTIALS":
            raise
        raise ReviewError("OAH_FIX_PROXY_CREDENTIALS", f"proxy userinfo is not allowed in the corrector's environment. "
                          f"{LAUNCHED}. Use a credential-free proxy or Codex's own configuration, then run oah fix "
                          "again") from None
    with _task_lock(t):
        h = _history(t)
        _check_history(t, h, LAUNCHED)
        receipt = _source(t, h, LAUNCHED)
        before = _head(t.root)
        _check_tree(t, receipt, before, LAUNCHED)
        _check_review_intent(t, receipt, binding, LAUNCHED)
        findings = _select(t, receipt, h, request.findings, LAUNCHED)
        class_ids, classes = _classes(h, findings)
        attempt_id = h.next_id()
        untracked = {name: _file_snapshot(t.root, name) for name in _untracked(t.root)}
        # Compare discovery with checkout bytes, so an untouched CRLF/encoded file stays unchanged.
        baseline = {name: _file_snapshot(t.root, name) for name in _tree_files(t.root, before)}
        instructions = round_instructions(
            task=t.name, attempt_id=attempt_id, receipt=receipt.name.removesuffix(".json"),
            receipt_digest=receipt.digest, base=receipt.record["base"], reviewed=receipt.record["head"], head=before,
            findings=findings, classes=classes)
        tools = call_cli.intent_tools()
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="oah-fix-", dir=temp_root))
        try:
            final = scratch / "last-message.md"
            prepared = call.prepare(
                call.CallRequest(engine=ENGINE, stage=STAGE, task=t.name, tier=request.tier,
                                 round_instructions=redact(instructions), timeout_s=request.timeout_s,
                                 ephemeral=True, retain_worker_text=False),
                settings, root=t.root, state_dir=t.state, env=launch_env, footer=tools.footer,
                has_footer=tools.has_footer, parse_intent=tools.parse, read_source=_committed_reader(t.root),
                keychain=keychain)
            attempt = _Attempt(t, attempt_id, receipt, findings, class_ids, before, prepared, binding, redact,
                               baseline=baseline, index_baseline=_index_state(t.root))
            return attempt.run(launch_env=launch_env, rules=rules, final=final, untracked=untracked)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


@dataclasses.dataclass
class _Attempt:
    t: _Task
    attempt_id: str
    receipt: receipts.Entry
    findings: list[dict]
    class_ids: list[str]
    before: str
    prepared: call.Prepared
    binding: oah_intent.Binding
    redact: Callable[[str], str]
    published: list[dict] = dataclasses.field(default_factory=list)
    ended: bool = False
    baseline: Mapping[str, tuple[int, bytes] | None] | None = None
    index_baseline: Mapping[str, tuple[bytes, ...]] = dataclasses.field(default_factory=dict)

    @property
    def finding_ids(self) -> list[str]:
        return [item["finding_id"] for item in self.findings]

    @property
    def run_id(self) -> str:
        return self.prepared.run_id

    def _publish(self, kind: str, fields: dict, summary: dict | None = None) -> None:
        record = {"schema": receipts.SCHEMA, "kind": f"attempt-{kind}", "task": self.t.name,
                  "attempt_id": self.attempt_id, **fields}
        name = f"{self.attempt_id}-{kind}.json"
        digest = receipts.publish(self.t.state, self.t.name, name, record, summary=summary)
        self.published.append({"name": name, "digest": digest})

    def _end(self, outcome: str, evidence: str) -> None:
        self._publish("outcome", {"outcome": outcome, "after_head": None,
                                  "evidence": self.redact(evidence)[:2000] or "(no evidence)",
                                  "finished_at": _iso(), "run_id": self.run_id}, {"outcome": outcome})
        self.ended = True

    def _left_behind(self, untracked: Mapping[str, tuple[int, bytes] | None]) -> str:
        try:
            index = _index_state(self.t.root)
            if index != self.index_baseline:
                return "; index entries changed; restore the index and inspect the worktree before retrying"
            files = _changes(self.t.root, self.before, untracked, baseline=self.baseline).files
            return f"; it left {len(files)} changed file(s) in the worktree: {_ids(files[:20])}" if files else ""
        except BaseException:
            # Optional diagnostics must never prevent the attempt's terminal record.
            return "; oah could not list the changes it left in the worktree"

    def run(self, *, launch_env, rules, final: pathlib.Path,
            untracked: Mapping[str, tuple[int, bytes] | None]) -> Result:
        with _stop_signals():
            return self._run(launch_env=launch_env, rules=rules, final=final, untracked=untracked)

    def _run(self, *, launch_env, rules, final: pathlib.Path,
             untracked: Mapping[str, tuple[int, bytes] | None]) -> Result:
        start = {"source_receipt": self.receipt.name, "source_receipt_digest": self.receipt.digest,
                 "finding_ids": self.finding_ids, "class_ids": self.class_ids, "before_head": self.before,
                 "engine": ENGINE, "manual": False, "started_at": _iso(), "run_id": self.run_id}
        try:
            with _held_signals() as held:
                self._publish("start", start)
            if held:
                raise KeyboardInterrupt("stop signal while recording the start; Codex was never launched")
            with call._correction_cleanup():
                run = call.execute(self.prepared, base_env=launch_env, rules=rules, redact=self.redact)
            judged = self._judge(run, final, untracked)
            result = self._result(judged)
            with _held_signals() as held:
                self._end(judged.outcome, judged.evidence)
            result.records = list(self.published)
            if held:
                result.warnings.append(f"{INTERRUPTED}: a stop signal arrived during terminal publication; the outcome "
                                       f"of {self.attempt_id} is durable. Inspect the worktree before continuing")
            return result
        except BaseException as exc:
            if not self.published or self.ended:
                raise
            interrupted = isinstance(exc, KeyboardInterrupt)
            with _held_signals():
                if interrupted:
                    self._end("aborted", f"codex run {self.run_id} was interrupted ({str(exc) or 'Ctrl-C'}) and oah "
                                         f"stopped its process tree{self._left_behind(untracked)}")
                else:
                    self._end("failed", f"codex run {self.run_id} did not complete: {self.redact(str(exc))[:300]}"
                                        f"{self._left_behind(untracked)}")
            if interrupted:
                raise FixInterrupted(f"{self.attempt_id} was interrupted and recorded as aborted; oah stopped Codex. "
                                     f"Check git status in {self.t.root} for changes it may have left, then run oah "
                                     f"fix --task {self.t.name} again or correct the findings yourself") from None
            code = getattr(exc, "code", "OAH_FIX_FAILED")
            text = str(exc)
            text = text[len(code) + 2:] if text.startswith(code + ": ") else text
            raise ReviewError(code, f"{text}. {self.attempt_id} was recorded as failed; fix the cause, then run oah "
                              f"fix --task {self.t.name} again") from None

    def _judge(self, run: dict, final: pathlib.Path, untracked: Mapping[str, tuple[int, bytes] | None]) -> "_Judged":
        root, rid = self.t.root, self.run_id
        head = _head(root)
        index = _index_state(root)
        changed_index = sorted(name for name in index.keys() | self.index_baseline.keys()
                               if index.get(name) != self.index_baseline.get(name))
        if changed_index:
            # No index blob or path may reach a diff, diagnostic or staging command on this path.
            positions = tuple(f"[file {n}]" for n in range(1, len(changed_index) + 1))
            reason = (f"OAH_FIX_INDEX_CHANGED: the corrector changed index entries {_ids(positions)}; diff and "
                      "commit guidance were withheld. Restore the index and inspect the worktree before retrying")
            if head != self.before:
                reason += f"; HEAD moved from {self.before} to {head}"
            effort = f"effort {run.get('launch_effort')} (header {run.get('header_check')})"
            patch = reason + "\n"
            write_text_atomic(self.prepared.run.file_path("correction.patch"), patch)
            write_json_atomic(self.prepared.run.file_path("correction.json"), {
                "attempt_id": self.attempt_id, "before_head": self.before, "outcome": "failed",
                "changed": list(positions), "new_files": [], "user_files": [],
                "changes_sha256": receipts.digest(patch.encode("utf-8")), "commands": [], "out_of_scope": []})
            return _Judged("failed", reason, reason, _Changes(positions, ()), patch, effort, [], [], [], "", head)
        changes = _changes(root, self.before, untracked, baseline=self.baseline)
        sensitive_paths = any(self.redact(name) != name for name in changes.files)
        sensitive, unsupported, captured = _scan_files(root, self.before, changes, untracked, self.redact)
        withheld = {**sensitive, **unsupported}
        safe_changes = _Changes(*(tuple(name for name in group if name not in withheld)
                                   for group in (changes.tracked, changes.new, changes.user)))
        raw = (b"" if sensitive_paths or unsupported else
               _patch(root, self.before, safe_changes, untracked, captured=captured))
        if sensitive:
            raw += (f"OAH_FIX_SENSITIVE_CONTENT: diff withheld for {_ids(list(sensitive.values()))} because raw file "
                    "contents contain a credential; remove credentials or restore the affected files "
                    "before retrying oah fix\n").encode("utf-8")
        unsupported_reason = (f"OAH_FIX_UNSUPPORTED_SUBMODULE: diff withheld for {_ids(list(unsupported.values()))} "
                              "because submodules, gitlinks or symlinks leaving the worktree cannot be scanned as "
                              "ordinary files; all commit "
                              "guidance was withheld. Restore those changes before retrying oah fix")
        if unsupported:
            raw += (unsupported_reason + "\n").encode("utf-8")
        beyond = outside(self.t.lease.scope, changes.files)
        beyond = [withheld.get(name, name) for name in beyond]
        # Withheld files are identified only by their positions in every retained or printed artifact.
        changes = _Changes(*(tuple(withheld.get(name, name) for name in group)
                              for group in (changes.tracked, changes.new, changes.user)))
        # Git can octal-escape filenames; withhold those headers rather than retaining encoded credentials.
        patch = ("OAH_FIX_SENSITIVE_PATH: diff withheld because a filename contains a runtime credential; "
                 "rename or discard the affected files before retrying oah fix\n" if sensitive_paths else
                 self.redact(raw.decode("utf-8", errors="replace")))
        digest = receipts.digest(raw)
        message = ""  # Final-message output is streamed with stdout; no worker text is retained.
        verified = "verified" if run.get("effort_verified") is True else "unverified"
        effort = f"effort {run.get('launch_effort')} (header {run.get('header_check')}, {verified})"
        files = changes.files
        named = _ids(files[:20]) + (f" and {len(files) - 20} more" if len(files) > 20 else "")
        error = str(run.get("error") or run.get("status")).split("; ", 1)[0]
        if head != self.before:
            outcome = "failed"
            reason = (f"HEAD moved from {self.before} to {head} during the run, so its result cannot be attributed to "
                      f"{self.attempt_id}")
            evidence = f"codex run {rid} ended ({run.get('status')}), but {reason}"
        elif run.get("status") != "finished":
            outcome, reason = "failed", error
            evidence = f"codex run {rid} {run.get('status')}: {error}; {effort}"
        elif files:
            outcome, reason = "applied", None
            evidence = f"codex run {rid} finished and changed {len(files)} file(s): {named}; changes {digest}; {effort}"
        else:
            outcome, reason = "no-change", None
            evidence = f"codex run {rid} finished without changing any file; {effort}"
        if outcome == "applied" and changes.user:
            outcome = "failed"
            reason = ("Codex changed preexisting untracked files owned by the user; automatic commit guidance was "
                      "withheld. Inspect correction.patch and restore or keep those files yourself before retrying")
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        if outcome == "applied" and sensitive_paths:
            outcome = "failed"
            reason = ("OAH_FIX_SENSITIVE_PATH: a changed filename contains a runtime credential; commit guidance "
                      "was withheld. Rename or discard the affected files, then run oah fix again")
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        if sensitive:
            outcome = "failed"
            reason = (f"OAH_FIX_SENSITIVE_CONTENT: raw contents of {_ids(list(sensitive.values()))} contain a "
                      "credential; their diffs and all commit guidance were withheld. Remove credentials or "
                      "restore the affected files before retrying oah fix")
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        if unsupported:
            outcome, reason = "failed", unsupported_reason
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        if outcome == "failed" and files:
            evidence += f"; it left {len(files)} changed file(s) in the worktree: {named}"
        commands, unquoted = (commit_commands(task=self.t.name, attempt_id=self.attempt_id,
                                              receipt=self.receipt.name.removesuffix(".json"),
                                              finding_ids=self.finding_ids, changes=changes, root=root)
                              if outcome == "applied" else ([], []))
        if any(self.redact(command) != command for command in commands):
            outcome, commands = "failed", []
            reason = ("OAH_FIX_SENSITIVE_COMMAND: commit guidance contains a runtime credential and was withheld; "
                      "remove sensitive commit metadata before correcting again")
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        write_text_atomic(self.prepared.run.file_path("correction.patch"), patch)
        write_json_atomic(self.prepared.run.file_path("correction.json"), _redacted({
            "attempt_id": self.attempt_id, "before_head": self.before, "outcome": outcome,
            "changed": list(changes.tracked), "new_files": list(changes.new), "user_files": list(changes.user),
            "changes_sha256": digest,
            "commands": commands, "out_of_scope": beyond}, self.redact))
        return _Judged(outcome, evidence, reason, changes, patch, effort, commands, unquoted, beyond, message, head)

    def _result(self, judged: "_Judged") -> Result:
        t, ids, receipt = self.t, _ids(self.finding_ids), self.receipt.name.removesuffix(".json")
        warnings: list[str] = []
        if judged.beyond:
            warnings.append(f"OAH_FIX_OUT_OF_SCOPE: Codex changed files outside the scope {_ids(t.lease.scope)} of "
                            f"task {t.name}: {_ids(judged.beyond)}. Restore tracked files with git restore -- <path>; "
                            "recover preexisting untracked files from your own backup and inspect new files before "
                            "deleting them, or widen the scope with oah renew --scope")
        if judged.changes.user:
            warnings.append("OAH_FIX_USER_FILES_CHANGED: Codex modified or deleted preexisting untracked files: "
                            f"{_ids(judged.changes.user)}. Review correction.patch and restore or keep them yourself; "
                            "these files are excluded from automatic staging guidance")
        found, reason = oah_intent.explain_binding(self.binding, t.root)
        if found != "current":
            warnings.append(f"OAH_FIX_INTENT_CHANGED: after the correction the bound intent {self.binding.source} is "
                            f"{found} ({reason}). Restore it with git restore -- {self.binding.source} before you commit; "
                            "a changed goal needs a new revision, never an edit by a correction")
        warnings += [f"OAH_FIX_UNQUOTABLE_PATH: {name!r} cannot be written safely for every shell; add it to the "
                     "commit yourself with git add" for name in judged.unquoted]
        lines = [f"run folder: {self.prepared.run_dir}", judged.effort]
        if judged.changes.files:
            lines.append("changed files: " + _ids(judged.changes.tracked + tuple(f"{name} (new)"
                                                                                    for name in judged.changes.new)
                                                  + tuple(f"{name} (preexisting untracked)"
                                                          for name in judged.changes.user)))
            shown = judged.patch.splitlines()
            lines += shown[:DIFF_LINES]
            if len(shown) > DIFF_LINES:
                lines.append(f"… {len(shown) - DIFF_LINES} more lines: run git diff {self.before} in {t.root}, or read "
                             f"correction.patch in the run folder")
        result = Result(OUTCOME_CODES[judged.outcome], 0 if judged.outcome == "applied" else 1, "", lines=lines,
                        warnings=warnings, attempt_id=self.attempt_id, outcome=judged.outcome,
                        records=list(self.published), run_dir=self.prepared.run_dir)
        if judged.outcome == "applied":
            result.commands = list(judged.commands)
            result.next_command = f"oah fix --bind --task {t.name}"
            result.message = (f"OAH_FIX_APPLIED: {self.attempt_id} changed the worktree to correct {ids} of {receipt} "
                              f"in task {t.name}. Nothing is committed yet: review the change, commit it with the "
                              f"commands below, then run {result.next_command} so the next review counts it")
            result.lines += ["Commit the correction with:", *result.commands, "Then bind the commit to the attempt:",
                             result.next_command]
        elif judged.outcome == "no-change":
            result.message = (f"OAH_FIX_NO_CHANGE: {self.attempt_id} finished without changing any file; {ids} of "
                              f"{receipt} {_are(self.finding_ids)} recorded as attempted (no-change). Correct the "
                              f"findings yourself, commit, and record that with oah fix --manual --engine human|claude "
                              f"--task {t.name}, or run oah fix --task {t.name} again to retry")
        else:
            if judged.head != self.before:
                left = (f" Codex cannot commit in its sandbox, so something else moved HEAD; see git log "
                        f"{self.before[:12]}..HEAD and decide what to keep before you go on.")
            elif judged.changes.user:
                left = (f" It changed user-owned untracked files ({_ids(judged.changes.user[:20])}); inspect "
                        "correction.patch and recover them from your own backup or keep them yourself before retrying.")
            elif judged.changes.files:
                left = (f" It left changes in the worktree ({_ids(judged.changes.files[:20])}); inspect them with git "
                        f"diff, then discard them (git restore --source=HEAD --staged --worktree -- . and delete the "
                        f"new files) before you run oah fix again.")
            else:
                left = ""
            result.message = (f"OAH_FIX_FAILED: {self.attempt_id} failed: {judged.reason}. {ids} of {receipt} "
                              f"{_are(self.finding_ids)} recorded as attempted (failed), and the run's changes do not "
                              f"count.{left} Fix the cause, then run oah fix --task {t.name} again")
        return result


@dataclasses.dataclass(frozen=True)
class _Judged:
    outcome: str
    evidence: str
    reason: str | None
    changes: _Changes
    patch: str
    effort: str
    commands: list[str]
    unquoted: list[str]
    beyond: list[str]
    message: str
    head: str


# Binding, manual corrections and recovery --------------------------------------------------------


def _attribution(t: _Task, before: str, head: str, engine: str, nothing: str, *,
                 replaced: str | None = None) -> list[tuple[str, str]]:
    """The new commits' engines; each must be known and one of them `engine`."""
    commits = authors.commit_engines(t.root, before, head)
    unknown = [sha for sha, found in commits if found == authors.UNKNOWN]
    if unknown:
        raise ReviewError("OAH_FIX_TRAILER_MISSING", f"commit {_ids(sha[:12] for sha in unknown)} in "
                          f"{before[:12]}..{head[:12]} carries no single valid Agent-Engine trailer (claude, codex or "
                          f"human), so the next review could not tell who wrote it. {nothing}. Add the trailer (for the "
                          f'newest commit: git commit --amend --no-edit --trailer "Agent-Engine: {engine}"), then run '
                          "the command again")
    engines = sorted({found for _sha, found in commits})
    if engine not in engines:
        if replaced is not None:
            hint = (f"If these commits hold {replaced}'s changes, change their Agent-Engine trailer to {replaced} (git "
                    "commit --amend) so the next review labels its independence honestly, then run oah fix --bind "
                    f"again. If you replaced {replaced}'s changes with your own correction, run oah fix --bind --task "
                    f"{t.name} --engine <who wrote it>; that is recorded")
        else:
            hint = ("Commit the correction with that trailer, or name the engine that wrote it with --engine "
                    "claude|human")
        raise ReviewError("OAH_FIX_ENGINE_MISMATCH", f"no commit in {before[:12]}..{head[:12]} carries Agent-Engine: "
                          f"{engine} (found: {_ids(engines)}). {nothing}. {hint}")
    return commits


def _authored(commits: Sequence[tuple[str, str]]) -> str:
    return _ids(f"{sha[:12]} ({engine})" for sha, engine in reversed(commits))


def bind(task: str, *, cwd: pathlib.Path, env: Mapping[str, str], engine: str | None = None) -> Result:
    """Bind the committed result of the applied attempt that waits for it (PLAN 5.9 head binding)."""
    return _guarded(env, lambda redact: _bind(task, cwd=cwd, env=env, engine=engine, redact=redact))


def _bind(task: str, *, cwd, env, engine, redact) -> Result:
    t = _task(task, cwd, env, RECORDED)
    with _task_lock(t):
        h = _history(t)
        unfinished = h.unfinished()
        if unfinished:
            raise _unfinished(t, unfinished, RECORDED)
        waiting = h.waiting()
        if not waiting:
            raise ReviewError("OAH_FIX_NOTHING_TO_BIND", f"no applied correction of task {t.name} waits for its commit. "
                              f"{RECORDED}. Run oah fix --task {t.name} to correct the latest review, or record a "
                              "correction you made with oah fix --manual")
        if len(waiting) > 1:
            raise ReviewError("OAH_FIX_HISTORY_INVALID", f"{_ids(item.attempt_id for item in waiting)} all wait for a "
                              f"commit, which oah fix never leaves behind. {RECORDED}. Inspect the records of task "
                              f"{t.name} before you bind anything")
        attempt = waiting[0]
        start = h.start(attempt)
        # A well-formed attempt that waits for its commit has exactly that one problem (receipts._merge).
        problems = h.problems(besides=attempt) + (list(attempt.problems) if len(attempt.problems) != 1 else [])
        if problems or start is None:
            raise ReviewError("OAH_FIX_HISTORY_INVALID", f"the review history of task {t.name} is incomplete or was "
                              f"changed. {RECORDED}:\n- " + "\n- ".join(problems or attempt.problems))
        written_by = engine or start["engine"]
        before, head = attempt.before_head, _head(t.root)
        saved = _saved_correction(t, start)
        if head == before:
            commands = saved.get("commands") or []
            if any(redact(command) != command for command in commands):
                commands = []  # Never turn a saved executable command into redacted guidance.
            shown = (" with:\n" + "\n".join(commands)) if commands else ""
            raise ReviewError("OAH_FIX_NOT_COMMITTED", f"{attempt.attempt_id} waits for its commit, but HEAD is still "
                              f"its start head {before[:12]}. {RECORDED}. Commit the correction{shown}\nthen run oah fix "
                              f"--bind --task {t.name} again. If you rejected the change, commit your own correction, "
                              "or a revert of it, and bind that")
        if not _is_ancestor(t.root, before, head):
            raise ReviewError("OAH_FIX_HEAD_UNRELATED", f"HEAD {head} does not descend from {before}, where "
                              f"{attempt.attempt_id} started (the branch was switched or rewritten). {RECORDED}. Check "
                              "out the branch that holds the committed correction, then run oah fix --bind again")
        dirty = _tracked_dirty(t.root)
        untracked = set(_untracked(t.root))
        left = [name for name in saved.get("new_files") or [] if name in untracked]
        if dirty or left:
            listed = _ids(dirty[:10] + [f"{name} (new, not committed)" for name in left[:10]])
            raise ReviewError("OAH_FIX_DIRTY_TREE", f"the worktree still has uncommitted changes ({listed}). {RECORDED}. "
                              f"Commit what belongs to {attempt.attempt_id}'s correction and discard the rest, then "
                              f"run oah fix --bind --task {t.name} again")
        commits = _attribution(t, before, head, written_by, RECORDED,
                               replaced=None if engine else start["engine"])
        evidence = f"bound to {head} by oah fix --bind: {len(commits)} commit(s) {_authored(commits)}"
        if written_by != start["engine"]:
            evidence += (f"; written by {written_by}, not by the attempt's engine {start['engine']}: the commits "
                         f"replace its changes (--engine {written_by})")
        record = {"schema": receipts.SCHEMA, "kind": "attempt-head", "task": t.name, "attempt_id": attempt.attempt_id,
                  "after_head": head, "evidence": redact(evidence), "bound_at": _iso()}
        name = f"{attempt.attempt_id}-head.json"
        with _held_signals():
            digest = receipts.publish(t.state, t.name, name, record)
        if written_by != start["engine"]:
            Store(t.state).log(task=t.name, agent=t.lease.agent, message=f"fix {attempt.attempt_id}: bound to {head} "
                               f"with --engine {written_by} (not {start['engine']})")
    return Result("OAH_FIX_BOUND", 0, f"OAH_FIX_BOUND: {attempt.attempt_id} is bound to {head}, so the next review "
                  f"counts it. Run oah review --task {t.name} for the next round",
                  lines=[f"commits: {_authored(commits)}"], attempt_id=attempt.attempt_id, outcome=attempt.outcome,
                  records=[{"name": name, "digest": digest}], next_command=f"oah review --task {t.name}")


def _saved_correction(t: _Task, start: Mapping) -> dict:
    """Require an automatic attempt's manifest; only manual attempts need no run metadata."""
    if start.get("manual") is True:
        return {}
    run_id = start.get("run_id")
    saved = None
    try:
        if isinstance(run_id, str):
            run = runs.find_run(t.state, run_id)
            if run.task == t.name:
                saved = read_json(run.file_path("correction.json"), None)
    except (runs.RunError, StateCorrupt, OSError):
        pass  # Never echo malformed metadata or storage exception text.
    if (not isinstance(saved, dict) or saved.get("attempt_id") != start["attempt_id"]
            or saved.get("before_head") != start["before_head"] or saved.get("outcome") != "applied"
            or not isinstance(saved.get("changes_sha256"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", saved["changes_sha256"])
            or any(not isinstance(saved.get(field), list)
                   or any(not isinstance(item, str) or not item for item in saved[field])
                   for field in ("changed", "new_files", "user_files", "commands", "out_of_scope"))):
        raise ReviewError("OAH_FIX_MANIFEST_INVALID", f"the automatic correction manifest for {start['attempt_id']} "
                          f"is missing or invalid. {RECORDED}. Restore the original run metadata, then rerun "
                          f"oah fix --bind --task {t.name}; oah fix --abort settles only unfinished attempts")
    return saved


def record_manual(task: str, *, engine: str, cwd: pathlib.Path, env: Mapping[str, str],
                  findings: Sequence[str] = ()) -> Result:
    """Record a correction that `claude` or `human` already committed, exactly like a Codex attempt."""
    return _guarded(env, lambda redact: _manual(task, engine=engine, cwd=cwd, env=env, findings=tuple(findings),
                                                redact=redact))


def _manual(task: str, *, engine, cwd, env, findings, redact) -> Result:
    if engine not in MANUAL_ENGINES:
        raise ReviewError("OAH_USAGE", f"--engine must be one of {', '.join(MANUAL_ENGINES)} for a manual correction; "
                          "a Codex correction is run with oah fix --task <task>")
    t = _task(task, cwd, env, RECORDED)
    binding = _check_intent(t, RECORDED)
    with _task_lock(t):
        h = _history(t)
        _check_history(t, h, RECORDED)
        receipt = _source(t, h, RECORDED)
        head = _head(t.root)
        _check_tree(t, receipt, head, RECORDED)
        _check_review_intent(t, receipt, binding, RECORDED)
        chosen = _select(t, receipt, h, findings, RECORDED)
        class_ids, _classes_found = _classes(h, chosen)
        before = receipt.record["head"]
        for attempt in h.attempts:  # start after the newest bound result HEAD contains
            after = attempt.after_head
            if (attempt.source_receipt_digest == receipt.digest and attempt.outcome == "applied" and after
                    and after != before and _is_ancestor(t.root, before, after) and _is_ancestor(t.root, after, head)):
                before = after
        if head == before:
            raise ReviewError("OAH_FIX_NOT_COMMITTED", f"HEAD is still {before[:12]}, so there is no committed "
                              f"correction to record. {RECORDED}. Commit your correction with the trailer "
                              f"Agent-Engine: {engine}, then run oah fix --manual --engine {engine} --task {t.name} again")
        commits = _attribution(t, before, head, engine, RECORDED)
        attempt_id = h.next_id()
        ids = [item["finding_id"] for item in chosen]
        evidence = (f"{engine} corrected {_ids(ids)} by hand in {len(commits)} commit(s) after {before[:12]} "
                    "(oah fix --manual)")
        now = _iso()
        records = [
            ("start", {"source_receipt": receipt.name, "source_receipt_digest": receipt.digest, "finding_ids": ids,
                       "class_ids": class_ids, "before_head": before, "engine": engine, "manual": True,
                       "started_at": now}, None),
            ("outcome", {"outcome": "applied", "after_head": None, "evidence": redact(evidence), "finished_at": now},
             {"outcome": "applied"}),
            ("head", {"after_head": head, "evidence": redact(f"bound to {head} by oah fix --manual: "
                                                             f"{_authored(commits)}"), "bound_at": now}, None),
        ]
        published = []
        with _held_signals():
            for kind, fields, summary in records:
                name = f"{attempt_id}-{kind}.json"
                record = {"schema": receipts.SCHEMA, "kind": f"attempt-{kind}", "task": t.name,
                          "attempt_id": attempt_id, **fields}
                published.append({"name": name, "digest": receipts.publish(t.state, t.name, name, record,
                                                                           summary=summary)})
    return Result("OAH_FIX_RECORDED", 0, f"OAH_FIX_RECORDED: {attempt_id} records the {engine} correction of "
                  f"{_ids(ids)} of {receipt.name.removesuffix('.json')} ({len(commits)} commit(s) after "
                  f"{before[:12]}), so the next review counts it. Run oah review --task {t.name} for the next round",
                  lines=[f"commits: {_authored(commits)}"], attempt_id=attempt_id, outcome="applied",
                  records=published, next_command=f"oah review --task {t.name}")


def abort(task: str, *, cwd: pathlib.Path, env: Mapping[str, str]) -> Result:
    """Record `aborted` for attempts whose `oah fix` stopped without an outcome; nothing else changes."""
    return _guarded(env, lambda redact: _abort(task, cwd=cwd, env=env, redact=redact))


def _abort(task: str, *, cwd, env, redact) -> Result:
    t = _task(task, cwd, env, RECORDED)
    with _task_lock(t):
        h = _history(t)
        unfinished = h.unfinished()
        if not unfinished:
            waiting = h.waiting()
            hint = (f" {waiting[0].attempt_id} waits for its commit instead: commit it, then run oah fix --bind --task "
                    f"{t.name}." if waiting else "")
            raise ReviewError("OAH_FIX_NOTHING_TO_ABORT", f"task {t.name} has no unfinished correction attempt. "
                              f"{RECORDED}.{hint}")
        broken = [item.attempt_id for item in unfinished if h.start(item) is None]
        if broken:
            raise ReviewError("OAH_FIX_HISTORY_INVALID", f"{_ids(broken)} {'has' if len(broken) == 1 else 'have'} no "
                              f"start record, so oah fix did not start {'it' if len(broken) == 1 else 'them'}. "
                              f"{RECORDED}. Inspect the records of task {t.name} by hand")
        published = []
        with _held_signals():
            for attempt in unfinished:
                start = h.start(attempt)
                run, status = None, "unknown"
                if isinstance(start.get("run_id"), str):
                    try:
                        run = runs.find_run(t.state, start["run_id"])
                        status = run.read().get("status", "unknown")
                    except runs.RunError:
                        run, status = None, "missing"
                evidence = (f"recorded by oah fix --abort: the oah fix that started {attempt.attempt_id} stopped without "
                            f"recording an outcome (killed or crashed); its run was last {status}")
                fields = {"outcome": "aborted", "after_head": None, "evidence": redact(evidence), "finished_at": _iso()}
                if isinstance(start.get("run_id"), str):
                    fields["run_id"] = start["run_id"]
                name = f"{attempt.attempt_id}-outcome.json"
                record = {"schema": receipts.SCHEMA, "kind": "attempt-outcome", "task": t.name,
                          "attempt_id": attempt.attempt_id, **fields}
                published.append({"name": name, "digest": receipts.publish(t.state, t.name, name, record,
                                                                           summary={"outcome": "aborted"})})
                if run is not None and status not in runs.TERMINAL:
                    # The task lock proves the oah fix that ran it is gone, as oah wait concludes from a dead pid.
                    run.result(status="lost", error="OAH_RUN_LOST: the oah fix that ran this correction stopped "
                                                    "without recording a result; oah fix --abort recorded its attempt "
                                                    "as aborted")
        ids = _ids(item.attempt_id for item in unfinished)
        Store(t.state).log(task=t.name, agent=t.lease.agent, message=f"fix {ids}: recorded as aborted (oah fix --abort)")
    return Result("OAH_FIX_ABORTED", 0, f"OAH_FIX_ABORTED: {ids} {'is' if len(unfinished) == 1 else 'are'} recorded "
                  f"as aborted. An interrupted correction may have left changes in {t.root}: check git status, then "
                  "commit or discard them before the next oah fix or oah review",
                  attempt_id=unfinished[-1].attempt_id, outcome="aborted", records=published)
