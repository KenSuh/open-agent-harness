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
- Raw changes are scanned and hashed in memory. Fixed diagnostics and positional metadata are saved;
  output directs the dispatcher to git diff, staging and a commit with Agent-Engine: codex.

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
import sys
import tempfile
import threading
import time
from importlib import resources
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from oah import briefs, call, call_cli, runs
from oah import intent as oah_intent
from oah.atomic import StateCorrupt, read_json, write_json_atomic, write_text_atomic
from oah.config import load_config
from oah.engines.effort import BEHAVIOUR_CHANGING, ORDINARY, load_rules
from oah.engines.proc import run_command
from oah.lease import Store
from oah.lockfile import LockTimeout, StateLock, _try_lock
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
_TASK_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_STOP_SIGNALS = tuple(getattr(signal, name) for name in (("SIGBREAK",) if os.name == "nt" else ("SIGTERM", "SIGHUP"))
                      if hasattr(signal, name))
LAUNCHED, RECORDED = "Nothing was launched", "Nothing was recorded"

# Exception codes are data too: only these literal codes and sentences may cross an error boundary.
_ERROR_DIAGNOSTICS = {
    "OAH_FIX_FAILED": "the correction failed; inspect the worktree and task records, then retry oah fix.",
    "OAH_FIX_CLEANUP_FAILED": "process-tree cleanup could not be verified; inspect running workers and the "
                              "worktree before retrying oah fix.",
    "OAH_FIX_WORKTREE_CHANGED": "the worktree changed after cleanup and accounting; inspect and restore or keep "
                                "the changes before retrying oah fix.",
    "OAH_FIX_INTERRUPTED": "the correction was interrupted; inspect the worktree and task records, then retry "
                           "oah fix or settle an unfinished attempt with oah fix --abort --task <task>.",
    "OAH_INTENT_NOT_CURRENT": "the intent is unbound, missing, stale or not committed as bound; accept and bind "
                              "the committed packet with oah start, then retry.",
    "OAH_INTENT_SOURCE_MISMATCH": "the intent differs from the reviewed binding; run oah review --task <task> "
                                  "for the accepted intent before correcting it.",
    "OAH_RECOMMEND_INVALID": "the packet Plan has invalid difficulty axes; correct its kind, intensity, "
                             "reasoning and risk, then retry.",
    "OAH_FIX_WORKER_REFUSED": "dispatched workers cannot run corrections; return the work to the dispatcher "
                              "to run oah fix.",
    "OAH_FIX_ENGINE_UNSUPPORTED": "correction requires Codex; configure that engine or record Claude's work "
                                  "with oah fix --manual --engine claude --task <task>.",
    "OAH_FIX_PROXY_CREDENTIALS": "proxy userinfo is forbidden; configure a credential-free proxy, then retry.",
    "OAH_FIX_SENSITIVE_SCOPE": "scope metadata contains a credential; remove it from the lease and packet "
                               "scope before retrying oah fix.",
    "OAH_FIX_SENSITIVE_INPUT": "a correction brief input contains a credential; remove it from the accepted "
                               "packet or review inputs and review again before retrying oah fix.",
    "OAH_FIX_SENSITIVE_METADATA": "task or directory metadata contains a credential; use credential-free "
                                  "metadata and bind the task again before retrying oah fix.",
    "OAH_FIX_BUSY": "another correction or review is running; wait for it to finish, then retry.",
    "OAH_FIX_ATTEMPT_UNFINISHED": "an attempt is unfinished; inspect the worktree, then run "
                                 "oah fix --abort --task <task>.",
    "OAH_FIX_BIND_REQUIRED": "an applied correction needs binding; commit the correction, then run "
                             "oah fix --bind --task <task>.",
    "OAH_FIX_HISTORY_INVALID": "the correction history or its digests are invalid; restore the original "
                               "task records before retrying.",
    "OAH_FIX_NOTHING_TO_FIX": "no findings remain to correct; run oah review --task <task> for the next round.",
    "OAH_FIX_NOT_REQUESTED": "the review has converged or needs a decision; inspect it and use "
                             "oah review --decision before requesting another correction.",
    "OAH_FIX_DIRTY_TREE": "the worktree or index contains uncommitted changes; inspect and commit or restore "
                          "them before retrying.",
    "OAH_FIX_HEAD_UNRELATED": "HEAD does not descend from the reviewed commit; return to the reviewed branch "
                              "before retrying.",
    "OAH_FIX_FINDING_UNKNOWN": "the selection is not a P0/P1 finding of the latest receipt; inspect that "
                               "receipt and retry with its finding IDs.",
    "OAH_FIX_NOTHING_TO_BIND": "no applied attempt awaits binding; run oah fix or record a manual correction.",
    "OAH_FIX_NOT_COMMITTED": "the correction is not committed; inspect git diff, stage intended files and commit with an "
                             "Agent-Engine: codex trailer, then run oah fix --bind --task <task>.",
    "OAH_FIX_MANIFEST_INVALID": "the correction manifest is invalid; restore its original run metadata, then "
                                "rerun oah fix --bind --task <task>.",
    "OAH_FIX_TRAILER_MISSING": "a correction commit has no known author engine; add its Agent-Engine trailer "
                               "before retrying.",
    "OAH_FIX_ENGINE_MISMATCH": "the commits do not match the correction engine; use an Agent-Engine: codex "
                               "trailer or record the replacement author with --engine before retrying.",
    "OAH_FIX_NOTHING_TO_ABORT": "no unfinished attempt remains; inspect the records and run "
                               "oah fix --bind --task <task> if a committed correction awaits binding.",
    "OAH_FIX_GIT_FAILED": "Git inspection failed; check Git access and the worktree, then retry.",
    "OAH_REVIEW_GIT_FAILED": "review Git inspection failed; check Git access and the worktree, then retry.",
    "OAH_REVIEW_RECORD_EXISTS": "a review record already exists; inspect the task history before retrying.",
    "OAH_REVIEW_NEEDS_DECISION": "review needs a decision; inspect the task history and use oah review --decision.",
    "OAH_WORKTREE_UNRESOLVED": "the leased worktree cannot be resolved; restore that worktree, then retry.",
    "OAH_WORKTREE_LEASE_MISMATCH": "the worktree does not match the lease; retry in the leased worktree.",
    "OAH_LEASE_CHANGED": "the dispatch lease changed; check oah status and bind the accepted packet before retrying.",
    "OAH_FIX_STATE_CHANGED": "correction inputs changed before publication; inspect the task records and retry; "
                             "settle an unfinished attempt with oah fix --abort first.",
    "OAH_UNSAFE_ARGUMENT": "a brief argument is unsafe; use simple task and packet paths, then retry.",
    "OAH_FOOTER_NOT_APPENDED": "the brief footer is invalid; update oah and rebuild the packet before retrying.",
    "OAH_PACKET_INTENT_INVALID": "the packet intent is invalid; correct and accept the intent before retrying.",
    "OAH_PACKET_UNREADABLE": "the packet cannot be read; restore its committed source before retrying.",
    "OAH_INTENT_SOURCE_UNREADABLE": "the intent source cannot be read; restore the accepted packet before retrying.",
    "OAH_CONFIG_INVALID": "project configuration is invalid; correct .oah/config.toml before retrying.",
    "OAH_CODEX_CONFIG_INVALID": "Codex configuration is invalid; repair its login and configuration before retrying.",
    "OAH_ENGINE_MISSING": "Codex is unavailable; install it and sign in through its own CLI, then retry.",
    "OAH_UNSUPPORTED_BATCH_SHIM": "the engine shim is unsupported; configure a native executable, then retry.",
    "OAH_ENGINE_LAUNCH_FAILED": "Codex could not start; check the configured executable and permissions, then retry.",
    "OAH_ENGINE_FAILED": "Codex failed; inspect the live terminal output and worktree before retrying.",
    "OAH_RUN_TIMEOUT": "Codex timed out; inspect the worktree and increase the timeout or split the task before retrying.",
    "OAH_RUN_INTERRUPTED": "the run was interrupted; inspect the worktree and task records before retrying.",
    "OAH_RUN_LOST": "the correction stopped before completion; inspect git diff and task records before retrying.",
    "OAH_RUN_PATH_UNSAFE": "the run path is unsafe; restore the original state directory before retrying.",
    "OAH_RUN_LAUNCH_INVALID": "the saved launch is invalid; restore the original run metadata before retrying.",
    "OAH_RUN_CORRUPT": "the run record is invalid; restore its original metadata before retrying.",
    "OAH_RUN_NOT_FOUND": "the run record is missing; restore the original run folder before retrying.",
    "OAH_TASK_INVALID": "the task identifier is invalid; use a simple task name and retry oah fix.",
    "OAH_FIX_STREAM_FAILED": "the output stream failed; restore the terminal and inspect the worktree before retrying.",
    "OAH_STATE_IO": "state storage failed; check storage permissions and available space before retrying.",
    "OAH_STATE_CORRUPT": "state storage is invalid; restore the original state records before retrying.",
    "OAH_STATE_PENDING": "a state transaction is pending; recover the state transaction before retrying.",
    "OAH_STATE_LOCK_TIMEOUT": "state storage is busy; wait for the other operation to finish before retrying.",
    "OAH_MODEL_UNRESOLVED": "the Codex model cannot be resolved; refresh the model catalog before retrying.",
    "OAH_EFFORT_INVALID": "the configured effort is invalid; correct the Codex tier effort before retrying.",
    "OAH_EFFORT_UNSUPPORTED": "the model does not support the configured effort; select a supported effort before retrying.",
    "OAH_EFFORT_METADATA_MISSING": "model effort metadata is missing; refresh the model catalog before retrying.",
    "OAH_EFFORT_MISMATCH": "the observed effort differs from the requested effort; repair the Codex configuration before retrying.",
    "OAH_EFFORT_DOWNGRADED": "the observed effort was downgraded; restore the requested Codex effort before retrying.",
    "OAH_NOT_A_REPO": "no repository is available; run oah fix inside the leased Git worktree.",
    "OAH_GIT_MISSING": "Git is unavailable; install Git and retry in the leased worktree.",
    "OAH_USAGE": "the correction arguments are invalid; run oah fix --help and retry with the required options.",
}

_WARNING_DIAGNOSTICS = {
    "OAH_FIX_OUT_OF_SCOPE": "Codex changed {count} file(s) outside the lease scope: {positions}. Restore tracked "
                            "files with git restore -- <path>; recover preexisting untracked files from your own "
                            "backup and inspect new files before deleting them, or widen the scope with "
                            "oah renew --scope.",
    "OAH_FIX_USER_FILES_CHANGED": "Codex changed {count} user-owned or ignored file(s): {positions}. "
                                  "Inspect git diff and restore or keep them yourself; these files are "
                                  "withheld from automatic staging guidance.",
    "OAH_FIX_INTENT_CHANGED": "after the correction the bound intent is {state}. Restore the bound packet before "
                              "you commit; a changed goal needs a new revision, never an edit by a correction.",
    "OAH_FIX_UNQUOTABLE_PATH": "{positions} cannot be written safely for every shell; add it to the commit "
                              "yourself with git add.",
}


def _error_diagnostic(exc: BaseException) -> tuple[str, str]:
    """Never format exception text, paths, class names or unapproved codes, even without exported keys."""
    code = INTERRUPTED if isinstance(exc, KeyboardInterrupt) else getattr(exc, "code", None)
    if not isinstance(code, str) or code not in _ERROR_DIAGNOSTICS:
        code = "OAH_STATE_IO" if isinstance(exc, OSError) else "OAH_FIX_FAILED"
    return code, f"{code}: {_ERROR_DIAGNOSTICS[code]}"


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
    run_id: str | None = None

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


def _attempt_record(kind, task, attempt_id, fields):
    record = {"schema": receipts.SCHEMA, "kind": kind, "task": receipts.digest(task.encode("utf-8")),
              "attempt_id": attempt_id, **fields}
    return runs._correction_record(record, receipts._ATTEMPT_KEYS[kind] | receipts._OPTIONAL_KEYS)


def _write_manifest(run, record):
    fields = ("attempt_id", "before_head", "outcome", "changed", "new_files", "user_files", "new_file_digests",
              "changes_sha256", "commands", "out_of_scope")
    _artifact_text(run, "correction.json", json.dumps(runs._correction_record(record, fields),
                                                    ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _artifact_text(run, name, text):
    publication = call._CORRECTION_PUBLICATION.get()
    if publication is not None:
        publication.artifacts[name] = text
    else:
        write_text_atomic(run.file_path(name), text)


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
    lease_json: str


def _task(task: str, cwd: pathlib.Path, env: Mapping[str, str], nothing: str) -> _Task:
    if env.get("OAH_WORKER") == "1":
        raise ReviewError("OAH_FIX_WORKER_REFUSED", "dispatched workers do not run or record corrections; return your "
                          "work to the dispatcher, who runs oah fix")
    redact = runner.credential_redactor(env)
    if not isinstance(task, str) or not _TASK_ID.fullmatch(task) or _has_credential(task, redact):
        raise ReviewError("OAH_TASK_INVALID", _ERROR_DIAGNOSTICS["OAH_TASK_INVALID"])
    info = repo_info(pathlib.Path(cwd))
    state = state_dir(info, env)
    _safe_inputs((str(cwd), str(state)), redact, "OAH_FIX_SENSITIVE_METADATA")
    receipts.review_dir(state, task)
    store = Store(state)
    store.journal.check_pending()
    _raw, lease_data = runs._captured_state(store.leases_path, default={"version": 1, "leases": []})
    runs._CORRECTION_CAPTURE.get().pop(store.leases_path, None)  # Compare only this task's complete lease.
    lease = next((lease for lease in store._decode(lease_data)
                  if lease.task == task and not lease.expired(store.now())), None)
    if lease is None:
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"task {task} has no active lease, so oah fix has no current intent "
                          f"or worktree to work with. {nothing}. Bind the accepted packet with oah start --task {task} "
                          f"--agent <you> --packet .oah/tasks/{task}/PACKET.md (use your packet's path), then run the "
                          "command again")
    exact = next(item for item in lease_data["leases"] if item["task"] == task)
    if any(_has_credential(value, redact) for value in lease.scope):
        raise ReviewError("OAH_FIX_SENSITIVE_SCOPE", _ERROR_DIAGNOSTICS["OAH_FIX_SENSITIVE_SCOPE"])
    _safe_inputs(exact, redact, "OAH_FIX_SENSITIVE_METADATA")
    if re.fullmatch(r"[0-9a-f]{16}", lease.id) is None:  # Store.start's token_hex(8), never free text.
        raise ReviewError("OAH_STATE_CORRUPT", _ERROR_DIAGNOSTICS["OAH_STATE_CORRUPT"])
    root = pathlib.Path(lease.worktree)
    if not root.is_dir():
        raise ReviewError("OAH_WORKTREE_UNRESOLVED", f"the lease of task {task} names the worktree {root}, which no "
                          f"longer exists. {nothing}. Release it with oah release --task {task} --agent {lease.agent} "
                          '--reason "worktree removed", then start the lease again in the task\'s worktree')
    return _Task(task, state, lease, root, json.dumps(exact))


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


def _check_intent(t: _Task, nothing: str, *, committed_text: str | None = None) -> oah_intent.Binding:
    binding = t.lease.binding()
    rebind = (f"oah start --task {t.name} --agent {t.lease.agent} --packet "
              f"{binding.source if binding else f'.oah/tasks/{t.name}/PACKET.md'}")
    if binding is None:
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"task {t.name} has a lease without an intent binding (unbound), so "
                          f"there is no current intent to correct against. {nothing}. Bind the accepted packet with "
                          f"{rebind} (use your packet's path), then run the command again")
    found = oah_intent.binding_state(binding, t.root)
    if found != "current":
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"the bound intent is {found}. {nothing}. Restore the bound "
                          "packet as it was bound, or raise its Revision, accept it and rebind the lease with "
                          "oah start --packet <packet>; then run the command again")
    try:
        committed = oah_intent.parse(committed_text if committed_text is not None else
                                     _committed_reader(t.root)(oah_intent.source_path(binding, t.root)))
    except (oah_intent.SourceError, oah_intent.IntentInvalid):
        committed = None
    if committed is None or committed.digest != binding.digest:
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"{binding.source} is not committed at HEAD as bound "
                          f"({binding.line()}). {nothing}. Correction and review briefs carry the committed packet, "
                          "so commit it as bound, then run the command again")
    return binding


def _check_review_intent(t: _Task, receipt: receipts.Entry, binding: oah_intent.Binding, nothing: str, *,
                         current_text: str | None = None) -> None:
    matches = (receipt.record.get("intent") == binding.to_json()
               and receipt.record.get("intent_digest") == binding.digest)
    if matches:
        try:
            source = oah_intent.source_path(binding, t.root)
            reviewed = _committed_reader(t.root, ref=receipt.record["head"])(source)
            current = current_text if current_text is not None else _committed_reader(t.root)(source)
            matches = (oah_intent.parse(reviewed).bind(binding.source) == binding
                       and briefs.raw_section(reviewed) == briefs.raw_section(current))
        except (oah_intent.SourceError, oah_intent.IntentInvalid):
            matches = False
    if not matches:
        raise ReviewError("OAH_INTENT_SOURCE_MISMATCH", f"the current binding or committed intent section differs "
                          f"from the intent reviewed by {receipt.name}. {nothing}. Run oah review --task {t.name} "
                          "to review the updated intent before correcting its findings")


@dataclasses.dataclass
class _History:
    records: receipts.Records
    attempts: tuple[receipts.Attempt, ...]
    index_bytes: bytes = b""
    index_lines: tuple[dict, ...] = ()

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


def _history(t: _Task, *, capture: bool = False) -> _History:
    records, index, lines = (receipts._capture_records(t.state, t.name) if capture
                             else (receipts.load(t.state, t.name), b"", ()))
    if any("run_id" in entry.record and not runs.RUN_ID_RE.fullmatch(entry.record["run_id"] or "")
           for entry in records.entries if entry.kind != "receipt"):
        raise ReviewError("OAH_FIX_HISTORY_INVALID", _ERROR_DIAGNOSTICS["OAH_FIX_HISTORY_INVALID"])
    return _History(records, tuple(receipts.attempts(records)), index, lines)


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


def _source(t: _Task, h: _History, nothing: str, *, round_log: list[dict] | None = None) -> receipts.Entry:
    found = h.records.receipts()
    if not found:
        raise ReviewError("OAH_FIX_NOTHING_TO_FIX", f"task {t.name} has no review receipt yet, so there are no findings "
                          f"to correct. {nothing}. Run oah review --task {t.name} first")
    latest = found[-1]
    history = rounds.series(h.records, receipts.read_round_log(t.state, t.name) if round_log is None else round_log,
                            latest.record["base"],
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


def _ignored(root: pathlib.Path) -> list[str]:
    listed = [os.fsdecode(name) for name in _git_bytes(
        root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split(b"\0") if name]
    return _user_files(root, listed)


def _user_files(root: pathlib.Path, listed: Iterable[str]) -> list[str]:
    """Expand user-owned directories, including nested .git files, without following links."""
    found = set(listed)

    def unreadable(_error):
        raise ReviewError("OAH_FIX_GIT_FAILED", _ERROR_DIAGNOSTICS["OAH_FIX_GIT_FAILED"])

    # Git collapses nested repositories to a directory; their ignored metadata is user-owned too.
    for name in sorted(found):
        path = root / name
        if not path.parent.resolve().is_relative_to(root.resolve()):
            continue
        if not path.is_symlink() and path.is_dir() and path.resolve().is_relative_to(root.resolve()):
            for parent, directories, files in os.walk(path, followlinks=False, onerror=unreadable):
                found.update((pathlib.Path(parent) / child).relative_to(root).as_posix()
                             for child in (*directories, *files))
                directories[:] = [child for child in directories if not (pathlib.Path(parent) / child).is_symlink()
                                  and (pathlib.Path(parent) / child).resolve().is_relative_to(root.resolve())]
    return sorted(found)


def _ignored_state(root: pathlib.Path, names: Iterable[str]) -> dict[str, tuple[int, int, str] | None]:
    """Fingerprint user files using raw bytes, full modes and sizes, never Git conversion."""
    found = {}
    for name in names:
        snapshot = _file_snapshot(root, name)
        if not (root / name).parent.resolve().is_relative_to(root.resolve()):
            found[name] = (stat.S_IFDIR, 0, receipts.digest(b""))
            continue
        try:
            mode = (root / name).lstat().st_mode if snapshot is not None else None
        except FileNotFoundError:
            mode = None
        found[name] = ((mode, len(snapshot[1]), receipts.digest(snapshot[1]))
                       if mode is not None else None)
    return found


def _worktree_fingerprint(root: pathlib.Path) -> str:
    """Hash raw tracked/user/ignored contents and modes plus HEAD/index, without content conversion."""
    head = _head(root)
    names = _user_files(root, (*_tree_files(root, head), *_untracked(root), *_ignored(root)))
    data = (head, _index_state(root), _ignored_state(root, names))
    # Index metadata contains raw byte fields. Hash those directly via a deterministic representation.
    return receipts.digest(repr(data).encode("utf-8", errors="surrogateescape"))


def _check_tree(t: _Task, receipt: receipts.Entry, head: str, nothing: str,
                redact: Callable[[str], str]) -> None:
    dirty = _path_labels(_tracked_dirty(t.root), redact)
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


class _ExistingLock(StateLock):
    """Acquire an existing lock without mkdir, O_CREAT or any refusal-side write."""

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            raise ReviewError("OAH_INTENT_NOT_CURRENT", _ERROR_DIAGNOSTICS["OAH_INTENT_NOT_CURRENT"]) from None
        try:
            deadline = time.monotonic() + self.timeout
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise LockTimeout("OAH_STATE_LOCK_TIMEOUT: state is busy; wait and retry.")
                time.sleep(self.poll)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self


class _PublicationConflict(ReviewError):
    """Only a failed optimistic publication check may leave the durable start unfinished."""


@contextlib.contextmanager
def _publication(t: _Task, h: _History):
    """Optimistically compare dependencies before any write, holding only a bounded publication lock."""
    from oah.lease import utc_now

    store = Store(t.state)
    with _ExistingLock(store.lock_path):
        if store.journal.path.exists():
            from oah.journal import PendingTransaction
            raise PendingTransaction("OAH_STATE_PENDING: recover the pending transaction before retrying.")
        data = read_json(store.leases_path, {"version": 1, "leases": []})
        store._decode(data)
        live = next((item for item in data["leases"] if item["task"] == t.name), None)
        if live != json.loads(t.lease_json) or t.lease.expired(utc_now()):
            raise _PublicationConflict("OAH_LEASE_CHANGED", _ERROR_DIAGNOSTICS["OAH_LEASE_CHANGED"])
        for path, expected in runs._CORRECTION_CAPTURE.get().items():
            try:
                actual = path.read_bytes()
            except FileNotFoundError:
                actual = None
            if actual != expected:
                raise _PublicationConflict("OAH_FIX_STATE_CHANGED", _ERROR_DIAGNOSTICS["OAH_FIX_STATE_CHANGED"])
        token = call._CORRECTION_STATE_LOCK.set(t.state)
        try:
            with receipts._correction_publication(t.state, t.name, h.index_bytes, h.index_lines):
                try:
                    yield
                finally:
                    publication = receipts._CORRECTION_PUBLICATION.get()
                    h.index_bytes, h.index_lines = publication["index"], tuple(publication["lines"])
        finally:
            call._CORRECTION_STATE_LOCK.reset(token)


@contextlib.contextmanager
def _task_lock(t: _Task, *, create: bool = False) -> Iterator[None]:
    path = receipts.task_lock_path(t.state, t.name)
    if not create and not path.is_file():
        # A successful review already created this lock. Refusals never create or repair state.
        code = ("OAH_FIX_HISTORY_INVALID" if any(receipts.review_dir(t.state, t.name).glob("R*.json")) else
                "OAH_FIX_NOTHING_TO_FIX")
        raise ReviewError(code, _ERROR_DIAGNOSTICS[code])
    lock = _ExistingLock(path, timeout=0)
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


def _guarded(env: Mapping[str, str], operation: Callable[[Callable[[str], str]], Result], *, cwd: pathlib.Path) -> Result:
    """Capture one input snapshot without writes; publication alone takes the shared state lock."""
    raw_redact = runner.credential_redactor(env)
    folded_redact = runner.credential_redactor({name: value.replace("\r\n", "\n") for name, value in env.items()})

    def redact(text: str) -> str:
        return folded_redact(raw_redact(text))
    try:
        if env.get("OAH_WORKER") == "1":
            raise ReviewError("OAH_FIX_WORKER_REFUSED", _ERROR_DIAGNOSTICS["OAH_FIX_WORKER_REFUSED"])
        state = state_dir(repo_info(pathlib.Path(cwd)), env)
        _safe_inputs((str(cwd), str(state)), redact, "OAH_FIX_SENSITIVE_METADATA")
        if not (state / ".lock").is_file():
            raise ReviewError("OAH_INTENT_NOT_CURRENT", _ERROR_DIAGNOSTICS["OAH_INTENT_NOT_CURRENT"])
        token = runs._CORRECTION_CAPTURE.set({})
        try:
            return operation(redact)
        finally:
            runs._CORRECTION_CAPTURE.reset(token)
    except BaseException as exc:
        code, diagnostic = _error_diagnostic(exc)
        message = diagnostic[len(code) + 2:]
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
            # Include every nested file and directory mode, including .git, using raw filesystem bytes only.
            digest = hashlib.sha256()
            for child_name in _user_files(root, (name,)):
                if child_name == name:
                    continue
                child = root / child_name
                child_info = child.lstat()
                snapshot = (None if stat.S_ISDIR(child_info.st_mode) else _file_snapshot(root, child_name))
                child_data = snapshot[1] if snapshot else b""
                for part in (os.fsencode(child.relative_to(path)), str(child_info.st_mode).encode("ascii"),
                             str(len(child_data)).encode("ascii"), child_data):
                    digest.update(len(part).to_bytes(8, "big"))
                    digest.update(part)
            data = digest.digest()
        return kind | (info.st_mode & 0o111), data
    except FileNotFoundError:
        return None
    except OSError as exc:
        if _has_credential(name, lambda text: text):
            raise ReviewError("OAH_FIX_GIT_FAILED", "cannot inspect a file with a credential-shaped name; rename "
                              "or discard it before retrying oah fix") from None
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
    """Include every stage, mode, object id and assume-unchanged/skip-worktree flag, without conversion."""
    found: dict[str, list[bytes]] = {}
    for entry in _git_bytes(root, "ls-files", "--stage", "-v", "-z").split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            found.setdefault(os.fsdecode(name), []).append(metadata)
    return {name: tuple(entries) for name, entries in found.items()}


def _index_stat_matches(root: pathlib.Path) -> dict[str, bool]:
    """Unchanged checkout stat entries, with a flag for timestamps safely older than the index.

    --debug reads cached metadata without refreshing the index or running a content filter. Git for
    Windows records second-resolution timestamps and no useful POSIX device/inode/owner identity.
    """
    index_path = root / os.fsdecode(_git_bytes(root, "rev-parse", "--git-path", "index").rstrip(b"\r\n"))
    index_time = index_path.stat().st_mtime_ns
    data = _git_bytes(root, "ls-files", "--debug", "-z")
    pattern = re.compile(rb"  ctime: (\d+):(\d+)\n  mtime: (\d+):(\d+)\n"
                         rb"  dev: (\d+)\tino: (\d+)\n  uid: (\d+)\tgid: (\d+)\n"
                         rb"  size: (\d+)\tflags: ([0-9a-fA-F]+)\n")
    found, position = {}, 0
    while position < len(data):
        end = data.find(b"\0", position)
        if end < 0 or not (match := pattern.match(data, end + 1)):
            return {}  # Unknown metadata never makes a raw mismatch clean.
        name = os.fsdecode(data[position:end])
        position = match.end()
        csec, cnsec, msec, mnsec, dev, ino, uid, gid, size = map(int, match.groups()[:9])
        if int(match[10], 16):
            continue
        path = root / name
        if not path.parent.resolve().is_relative_to(root.resolve()):
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size & 0xffffffff != size:
            continue
        # A zero nanosecond field can come from a Git build with second-resolution stat data.
        def same_time(actual, seconds, nanos):
            return actual == seconds * 1_000_000_000 + nanos if nanos else actual // 1_000_000_000 == seconds

        if not (same_time(info.st_ctime_ns, csec, cnsec) and same_time(info.st_mtime_ns, msec, mnsec)):
            continue
        if os.name != "nt" and any(actual & 0xffffffff != saved for actual, saved in (
                (info.st_dev, dev), (info.st_ino, ino), (info.st_uid, uid), (info.st_gid, gid))):
            continue
        found[name] = (msec * 1_000_000_000 + mnsec < index_time if mnsec else
                       msec < index_time // 1_000_000_000)
    return found


def _tracked_dirty(root: pathlib.Path) -> list[str]:
    """Compare tree/index identities and raw bytes or unchanged checkout stats, without conversion."""
    tree, index = _tree_files(root, "HEAD"), _index_files(root)
    stages = _index_state(root)
    checkout_stats = _index_stat_matches(root)
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
            if old and current and old[0] == current[0] and name in checkout_stats:
                # Racy entries need a content check. An exact CRLF checkout of an LF blob is safe
                # even at coarse timestamp resolution; arbitrary filter output is never trusted here.
                if checkout_stats[name] or (b"\r" not in old[1] and current[1].replace(b"\r\n", b"\n") == old[1]):
                    continue
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
        current = _file_snapshot(root, name)
        gitlink_changed = gitlink and tree.get(name) != index.get(name)
        if (gitlink and current and stat.S_ISDIR(current[0]) and not (root / name).is_symlink()
                and (root / name).resolve().is_relative_to(root.resolve())):
            # Tracked submodules can keep .git as a pointer outside this directory. Read their HEAD identity
            # separately; user-owned directory fingerprints never follow that pointer or invoke Git.
            gitlink_changed = gitlink_changed or _head(root / name) != (tree.get(name) or index[name])[1]
        if old != current or gitlink_changed:
            tracked.append(name)
    user = tuple(name for name, snapshot in untracked.items()
                 if name in tracked or _file_snapshot(root, name) != snapshot)
    return _Changes(tuple(name for name in tracked if name not in untracked),
                    tuple(name for name in _untracked(root) if name not in untracked), user)


def _has_credential(text: str, redact: Callable[[str], str]) -> bool:
    return any(redact(candidate) != candidate or any(pattern.search(candidate) for rule, pattern in CONTENT_RULES
                                                    if rule in ("key", "env-secret"))
               for candidate in (text, text.replace("\r\n", "\n")))


def _safe_inputs(value, redact: Callable[[str], str], code: str = "OAH_FIX_SENSITIVE_INPUT") -> None:
    """Scan original strings before retention, without replacing any accepted intent bytes."""
    if isinstance(value, str):
        if _has_credential(value, redact):
            raise ReviewError(code, _ERROR_DIAGNOSTICS[code])
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _safe_inputs(key, redact, code)
            _safe_inputs(item, redact, code)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _safe_inputs(item, redact, code)


def _path_labels(names: Iterable[str], redact: Callable[[str], str]) -> list[str]:
    return [f"[file {position}]" for position, _name in enumerate(names, 1)]


def _scan_files(root: pathlib.Path, before: str, changes: _Changes,
                untracked: Mapping[str, tuple[int, bytes] | None],
                redact: Callable[[str], str]) -> tuple[dict[str, str], dict[str, str], dict]:
    """Scan raw paths and capture both raw sides once; only scanned inputs may enter the patch."""
    found, unsupported, captured = {}, {}, {}
    tree, index = _tree_files(root, before), _index_files(root)
    for position, name in enumerate(changes.files, 1):
        if _has_credential(name, redact):
            found[name] = f"[file {position}]"
            continue
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
        if any(_has_credential(text, redact) for text in texts):
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
    """Fixed commit guidance; the dispatcher chooses and stages new paths after inspecting git diff."""
    # New paths must be chosen and staged by the dispatcher after inspecting git diff.
    return (["git add -u", 'git commit -m "Correct review findings" -m "Agent-Engine: codex"'], [])


# The correction run ------------------------------------------------------------------------------


def correct(request: Request, *, cwd: pathlib.Path, env: Mapping[str, str], keychain=None,
            rules: Sequence[Mapping] | None = None, temp_root: pathlib.Path | None = None) -> Result:
    """Run one Codex correction of the task's latest review; refusals raise ReviewError before any launch."""
    return _guarded(env, lambda redact: _correct(request, cwd=cwd, env=env, keychain=keychain, rules=rules,
                                                 temp_root=temp_root, redact=redact), cwd=cwd)


def _correct(request: Request, *, cwd, env, keychain, rules, temp_root, redact) -> Result:
    t = _task(request.task, cwd, env, LAUNCHED)
    binding = t.lease.binding()
    try:
        committed_text = (_committed_reader(t.root)(oah_intent.source_path(binding, t.root))
                          if binding is not None else None)
    except oah_intent.SourceError:
        raise ReviewError("OAH_INTENT_NOT_CURRENT", _ERROR_DIAGNOSTICS["OAH_INTENT_NOT_CURRENT"]) from None
    binding = _check_intent(t, LAUNCHED, committed_text=committed_text)
    config = load_config(t.root)
    settings = call.Settings.validated(config.section("stages", call.STAGE_DEFAULTS),
                                       config.section("engines", call.ENGINE_DEFAULTS),
                                       config.section("models", call.models.DEFAULTS))
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
    with _task_lock(t, create=False):
        h = _history(t, capture=True)
        _safe_inputs((dataclasses.asdict(h.records), h.index_bytes.decode("utf-8"), h.index_lines), redact)
        _check_history(t, h, LAUNCHED)
        round_log = receipts.read_round_log(t.state, t.name)
        receipt = _source(t, h, LAUNCHED, round_log=round_log)
        before = _head(t.root)
        _check_tree(t, receipt, before, LAUNCHED, redact)
        _check_review_intent(t, receipt, binding, LAUNCHED, current_text=committed_text)
        findings = _select(t, receipt, h, request.findings, LAUNCHED)
        class_ids, classes = _classes(h, findings)
        attempt_id = h.next_id()
        user_names = _untracked(t.root)
        directories = [name for name in user_names if not (t.root / name).is_symlink() and (t.root / name).is_dir()]
        ignored = _user_files(t.root, (*directories, *_ignored(t.root)))
        untracked = {name: _file_snapshot(t.root, name) for name in (*user_names, *ignored)}
        ignored_baseline = _ignored_state(t.root, ignored)
        # Compare discovery with checkout bytes, so an untouched CRLF/encoded file stays unchanged.
        baseline = {name: _file_snapshot(t.root, name) for name in _tree_files(t.root, before)}
        instructions = round_instructions(
            task=t.name, attempt_id=attempt_id, receipt=receipt.name.removesuffix(".json"),
            receipt_digest=receipt.digest, base=receipt.record["base"], reviewed=receipt.record["head"], head=before,
            findings=findings, classes=classes)
        tools = call_cli.intent_tools()
        packet = call.read_packet(t.root, oah_intent.source_path(binding, t.root), parse_intent=tools.parse,
                                  read_source=lambda _path: committed_text)
        if any(_has_credential(value, redact) for value in packet.scope):
            raise ReviewError("OAH_FIX_SENSITIVE_SCOPE", _ERROR_DIAGNOSTICS["OAH_FIX_SENSITIVE_SCOPE"])
        bound = call.dispatch_binding(t.state, t.name)
        rules = json.loads(json.dumps(load_rules() if rules is None else list(rules)))
        metadata = (dataclasses.asdict(request), dataclasses.asdict(settings), committed_text, bound, rules,
                    dataclasses.asdict(h.records), h.index_bytes.decode("utf-8"), round_log,
                    findings, classes, instructions, tools.footer, briefs.VERIFY_FOOTER)
        _safe_inputs((config.raw, metadata), redact)

        scratch = pathlib.Path(tempfile.mkdtemp(prefix="oah-fix-", dir=temp_root))
        try:
            final = scratch / "last-message.md"
            with call._correction_inputs(lambda value: _safe_inputs(value, redact), state=t.state,
                                         lease_json=t.lease_json, bound=bound, packet=packet, config=config,
                                         metadata_json=json.dumps(metadata)) as inputs:
                prepared = call.prepare(
                    call.CallRequest(engine=ENGINE, stage=STAGE, task=t.name, tier=request.tier,
                                     round_instructions=instructions, timeout_s=request.timeout_s,
                                     ephemeral=True, retain_worker_text=False),
                    settings, root=t.root, state_dir=t.state, env=launch_env, footer=tools.footer,
                    has_footer=tools.has_footer, parse_intent=tools.parse,
                    keychain=keychain)
                attempt = _Attempt(t, attempt_id, receipt, findings, class_ids, before, prepared, binding, redact,
                                   baseline=baseline, index_baseline=_index_state(t.root), ignored_baseline=ignored_baseline)
                attempt.history = h
                with _stop_signals(), inputs.publication():
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
    ignored_baseline: Mapping[str, tuple[int, int, str] | None] = dataclasses.field(default_factory=dict)
    history: _History | None = None

    @property
    def finding_ids(self) -> list[str]:
        return [item["finding_id"] for item in self.findings]

    @property
    def run_id(self) -> str:
        return self.prepared.run_id

    def _publish(self, kind: str, fields: dict, summary: dict | None = None) -> None:
        record = _attempt_record(f"attempt-{kind}", self.t.name, self.attempt_id, fields)
        name = f"{self.attempt_id}-{kind}.json"
        digest = receipts.publish(self.t.state, self.t.name, name, record, summary=summary)
        self.published.append({"kind": f"attempt-{kind}", "digest": digest})

    def _end(self, outcome: str, evidence: str) -> None:
        publication = call._CORRECTION_PUBLICATION.get()
        if publication is not None:
            if outcome == "applied":
                # Binding requires this manifest; a failure must take the failed-settlement path.
                write_text_atomic(self.prepared.run.file_path("correction.json"),
                                  publication.artifacts["correction.json"])
            for name, text in publication.artifacts.items():
                if outcome == "applied" and name == "correction.json":
                    continue
                try:
                    write_text_atomic(self.prepared.run.file_path(name), text)
                except Exception:
                    pass  # Optional diagnostics never prevent settlement.
            run = self.prepared.run
            try:
                if outcome in ("failed", "aborted") and run.read().get("status") not in runs.TERMINAL:
                    code = evidence.partition(":")[0]
                    run.result(status="failed", error=code if code in _ERROR_DIAGNOSTICS else "OAH_FIX_FAILED")
                run._write_result(run.read())
            except Exception:
                pass  # A lifecycle diagnostic write must not prevent the terminal receipt.
        self._publish("outcome", {"outcome": outcome, "after_head": None,
                                  "evidence": self.redact(evidence)[:2000] or "(no evidence)",
                                  "finished_at": _iso(), "run_id": self.run_id}, {"outcome": outcome})
        self.ended = True

    def run(self, *, launch_env, rules, final: pathlib.Path,
            untracked: Mapping[str, tuple[int, bytes] | None]) -> Result:
        with _stop_signals():
            return self._run(launch_env=launch_env, rules=rules, final=final, untracked=untracked)

    def _run(self, *, launch_env, rules, final: pathlib.Path,
             untracked: Mapping[str, tuple[int, bytes] | None]) -> Result:
        start = {"source_receipt": self.receipt.name, "source_receipt_digest": self.receipt.digest,
                 "finding_ids": self.finding_ids, "class_ids": self.class_ids, "before_head": self.before,
                 "engine": ENGINE, "manual": False, "started_at": _iso(), "run_id": self.run_id}
        fingerprint = None
        try:
            with _held_signals() as held, _publication(self.t, self.history):
                self._publish("start", start)
                publication = call._CORRECTION_PUBLICATION.get()
                if publication is not None:
                    publication.publish_run()
            if held:
                raise KeyboardInterrupt("stop signal while recording the start; Codex was never launched")
            try:
                print(f"OAH_FIX_STARTED: task {self.t.name}; wait for the correction, then inspect git diff.",
                      file=sys.stderr, flush=True)
            except (OSError, ValueError):
                raise ReviewError("OAH_FIX_STREAM_FAILED", _ERROR_DIAGNOSTICS["OAH_FIX_STREAM_FAILED"]) from None
            with call._correction_cleanup():
                try:
                    run = call.execute(self.prepared, base_env=launch_env, rules=rules, redact=self.redact)
                except BaseException:
                    # Inspection is best-effort on exceptions: terminal publication must still happen.
                    try:
                        fingerprint = _worktree_fingerprint(self.t.root)
                    except Exception:
                        pass
                    raise
            fingerprint = _worktree_fingerprint(self.t.root)
            judged = self._judge(run, final, untracked)
            result = self._result(judged)
            with _held_signals() as held:
                self._check_fingerprint(fingerprint)
                with _publication(self.t, self.history):
                    self._end(judged.outcome, judged.evidence)
            result.records = list(self.published)
            if held:
                result.warnings.append(f"{INTERRUPTED}: a stop signal arrived during terminal publication; the outcome "
                                       f"of {self.attempt_id} is durable. Inspect the worktree before continuing")
            self._show(judged)
            return result
        except BaseException as exc:
            if not self.published or self.ended or isinstance(exc, _PublicationConflict):
                raise
            interrupted = isinstance(exc, KeyboardInterrupt)
            with _held_signals():
                if fingerprint is not None:
                    try:
                        self._check_fingerprint(fingerprint)
                    except ReviewError as changed:
                        if changed.code == "OAH_FIX_WORKTREE_CHANGED":
                            exc, interrupted = changed, False
                    except Exception:
                        pass
                code, diagnostic = _error_diagnostic(exc)
                with _publication(self.t, self.history):
                    self._end("aborted" if interrupted else "failed", diagnostic)
            if interrupted:
                raise FixInterrupted(diagnostic[len(code) + 2:]) from None
            raise ReviewError(code, diagnostic[len(code) + 2:]) from None

    def _show(self, judged):
        print(f"{OUTCOME_CODES[judged.outcome]}: task {self.t.name}; changed paths "
              f"{json.dumps(judged.changes.files, ensure_ascii=True)}; inspect git diff before continuing.",
              file=sys.stderr, flush=True)
        if judged.patch:
            print(f"{OUTCOME_CODES[judged.outcome]}: scanned diff follows; inspect it before staging.\n"
                  + "\n".join(judged.patch.splitlines()[:DIFF_LINES]), file=sys.stderr, flush=True)

    def _check_fingerprint(self, expected: str) -> None:
        if _worktree_fingerprint(self.t.root) != expected:
            code = "OAH_FIX_WORKTREE_CHANGED"
            diagnostic = f"{code}: " + _ERROR_DIAGNOSTICS[code]
            # The captured patch/guidance no longer describe the final tree. Retain only fixed diagnostics.
            try:
                _artifact_text(self.prepared.run, "correction.patch", code + "\n")
                _write_manifest(self.prepared.run, {
                    "attempt_id": self.attempt_id, "before_head": self.before, "outcome": "failed",
                    "changed": [], "new_files": [], "user_files": [], "commands": [], "out_of_scope": [],
                    "changes_sha256": receipts.digest(diagnostic.encode("utf-8"))})
            except Exception:
                pass  # Failed optional artifacts cannot prevent the terminal outcome.
            raise ReviewError(code, _ERROR_DIAGNOSTICS[code])

    def _judge(self, run: dict, final: pathlib.Path, untracked: Mapping[str, tuple[int, bytes] | None]) -> "_Judged":
        root, rid = self.t.root, self.run_id
        launch_effort = run.get("launch_effort")
        if launch_effort not in (*ORDINARY, *BEHAVIOUR_CHANGING):
            launch_effort = "unverified"
        header = run.get("header_check")
        if header not in ("match", "downgraded", "mismatch"):
            header = "unverified"
        verified = "verified" if run.get("effort_verified") is True else "unverified"
        effort = f"effort {launch_effort} (header {header}, {verified})"
        status = run.get("status") if run.get("status") in runs.TERMINAL else "unverified"
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
            patch = reason + "\n"
            _artifact_text(self.prepared.run, "correction.patch", "OAH_FIX_FAILED\n")
            _write_manifest(self.prepared.run, {
                "attempt_id": self.attempt_id, "before_head": self.before, "outcome": "failed",
                "changed": list(positions), "new_files": [], "user_files": [],
                "changes_sha256": receipts.digest(patch.encode("utf-8")), "commands": [], "out_of_scope": []})
            return _Judged("failed", reason, reason, _Changes(positions, ()), patch, effort, [], [], [], "", head)
        changes = _changes(root, self.before, untracked, baseline=self.baseline)
        ignored_state = _ignored_state(root, _user_files(root, self.ignored_baseline.keys() | set(_ignored(root))))
        ignored_changes = {name for name, snapshot in ignored_state.items()
                           if snapshot != self.ignored_baseline.get(name)}
        changes = _Changes(changes.tracked, changes.new,
                           tuple(dict.fromkeys((*changes.user, *sorted(ignored_changes)))))
        sensitive_paths = any(_has_credential(name, self.redact) for name in changes.files)
        sensitive, unsupported, captured = _scan_files(root, self.before, changes, untracked, self.redact)
        ignored_positions = {name: f"[file {n}]" for n, name in enumerate(changes.files, 1) if name in ignored_changes}
        withheld = {**sensitive, **unsupported, **ignored_positions}
        safe_changes = _Changes(*(tuple(name for name in group if name not in withheld)
                                   for group in (changes.tracked, changes.new, changes.user)))
        raw = (b"" if sensitive_paths or unsupported else
               _patch(root, self.before, safe_changes, untracked, captured=captured))
        content_size = len(raw)
        if sensitive:
            raw += (f"OAH_FIX_SENSITIVE_CONTENT: diff withheld for {_ids(list(sensitive.values()))} because raw "
                    "filenames or file contents contain a credential; remove credentials or restore the affected files "
                    "before retrying oah fix\n").encode("utf-8")
        ignored_reason = (f"OAH_FIX_USER_FILES_CHANGED: diff withheld for {_ids(list(ignored_positions.values()))} "
                          "because preexisting untracked or ignored user files changed; all commit guidance was "
                          "withheld. Inspect the worktree and restore or keep those files yourself before retrying oah fix")
        if ignored_positions:
            raw += (ignored_reason + "\n").encode("utf-8")
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
        # Packet values stay out of terminal diagnostics too; keep their scanned bytes in accounting only.
        display_raw = raw
        packet_name = oah_intent.source_path(self.binding, root).relative_to(root).as_posix()
        if packet_name in safe_changes.files:
            display_changes = _Changes(*(tuple(name for name in group if name != packet_name)
                                         for group in (safe_changes.tracked, safe_changes.new, safe_changes.user)))
            display_raw = (b"" if sensitive_paths or unsupported else
                           _patch(root, self.before, display_changes, untracked, captured=captured)) + raw[content_size:]
        # Git can octal-escape filenames; withhold those headers rather than retaining encoded credentials.
        patch = (f"OAH_FIX_SENSITIVE_PATH: diff withheld for {_ids(list(sensitive.values()))} because a filename "
                 "contains a credential; "
                 "rename or discard the affected files before retrying oah fix\n" if sensitive_paths else
                 self.redact(display_raw.decode("utf-8", errors="replace")))
        digest = receipts.digest(raw)
        message = ""  # Worker text is drained and hashed only, never displayed or retained.
        files = changes.files
        positions = _path_labels(files, self.redact)
        named = _ids(positions[:20]) + (f" and {len(files) - 20} more" if len(files) > 20 else "")
        # Reconstruct the diagnostic from an approved code, never a saved module error's text.
        error_code = str(run.get("error") or "").partition(":")[0]
        _code, error = _error_diagnostic(ReviewError(error_code, ""))
        if head != self.before:
            outcome = "failed"
            reason = (f"HEAD moved from {self.before} to {head} during the run, so its result cannot be attributed to "
                      f"{self.attempt_id}")
            evidence = f"codex run {rid} ended ({status}), but {reason}"
        elif status != "finished":
            outcome, reason = "failed", error
            evidence = f"codex run {rid} {status}: {error}; {effort}"
        elif files:
            outcome, reason = "applied", None
            evidence = f"codex run {rid} finished and changed {len(files)} file(s): {named}; changes {digest}; {effort}"
        else:
            outcome, reason = "no-change", None
            evidence = f"codex run {rid} finished without changing any file; {effort}"
        if outcome == "applied" and changes.user:
            outcome = "failed"
            reason = ("Codex changed preexisting untracked files owned by the user; automatic commit guidance was "
                      "withheld. Inspect git diff and restore or keep those files yourself before retrying")
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        if ignored_positions:
            outcome, reason = "failed", ignored_reason
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        if outcome == "applied" and sensitive_paths:
            outcome = "failed"
            reason = ("OAH_FIX_SENSITIVE_PATH: a changed filename contains a credential; commit guidance "
                      "was withheld. Rename or discard the affected files, then run oah fix again")
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        if sensitive:
            outcome = "failed"
            reason = (f"OAH_FIX_SENSITIVE_CONTENT: raw filenames or contents of {_ids(list(sensitive.values()))} contain a "
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
        if any(_has_credential(command, self.redact) for command in commands):
            outcome, commands = "failed", []
            reason = ("OAH_FIX_SENSITIVE_COMMAND: commit guidance contains a credential and was withheld; "
                      "remove sensitive commit metadata before correcting again")
            evidence = f"codex run {rid} failed: {reason}; changes {digest}; {effort}"
        _artifact_text(self.prepared.run, "correction.patch", OUTCOME_CODES[outcome] + "\n")
        positions = {name: f"[file {n}]" for n, name in enumerate(changes.files, 1)}
        _write_manifest(self.prepared.run, {
            "attempt_id": self.attempt_id, "before_head": self.before, "outcome": outcome,
            "changed": [positions[n] for n in changes.tracked], "new_files": [positions[n] for n in changes.new],
            "user_files": [positions[n] for n in changes.user],
            "new_file_digests": [hashlib.sha256(os.fsencode(n)).hexdigest() for n in changes.new],
            "changes_sha256": digest,
            "commands": commands, "out_of_scope": [positions[n] for n in beyond]})
        return _Judged(outcome, evidence, reason, changes, patch, effort, commands, unquoted, beyond, message, head)

    def _result(self, judged: "_Judged") -> Result:
        t = self.t
        warnings: list[str] = []
        positions = {name: f"[file {n}]" for n, name in enumerate(judged.changes.files, 1)}
        if judged.beyond:
            code = "OAH_FIX_OUT_OF_SCOPE"
            warnings.append(f"{code}: " + _WARNING_DIAGNOSTICS[code].format(
                count=len(judged.beyond), positions=_ids([positions[name] for name in judged.beyond])))
        if judged.changes.user:
            code = "OAH_FIX_USER_FILES_CHANGED"
            warnings.append(f"{code}: " + _WARNING_DIAGNOSTICS[code].format(
                count=len(judged.changes.user), positions=_ids([positions[name] for name in judged.changes.user])))
        found = oah_intent.binding_state(self.binding, t.root)
        if found != "current":
            code = "OAH_FIX_INTENT_CHANGED"
            warnings.append(f"{code}: " + _WARNING_DIAGNOSTICS[code].format(state=found))
        code = "OAH_FIX_UNQUOTABLE_PATH"
        warnings += [f"{code}: " + _WARNING_DIAGNOSTICS[code].format(positions=positions[name])
                     for name in judged.unquoted]
        code = OUTCOME_CODES[judged.outcome]
        lines = [f"{code}: {len(judged.changes.files)} changed file(s); inspect git diff before continuing.",
                 f"{code}: {judged.effort}; inspect the worktree before continuing."]
        if judged.reason:
            lines.append(f"{code}: {judged.reason}; inspect git diff before retrying.")
        message = {
            "applied": "the worktree changed; inspect git diff, stage the intended files and commit with "
                       "Agent-Engine: codex, then run oah fix --bind --task <task>.",
            "no-change": "no files changed; correct and commit the findings manually or retry oah fix --task <task>.",
            "failed": "the correction failed and its changes do not count; inspect git diff and resolve the cause "
                      "before retrying oah fix --task <task>.",
        }[judged.outcome]
        if judged.outcome == "failed" and judged.reason:
            message = judged.reason + "; " + message
        if judged.outcome == "failed" and positions:
            message += f" Changed file positions: {_ids(list(positions.values())[:20])}; inspect git diff."
        if judged.head != self.before:
            message += " Inspect git log before retrying."
        if judged.outcome == "applied":
            lines.extend(f"{code}: after staging the intended files, run {command}." for command in judged.commands)
        return Result(code, 0 if judged.outcome == "applied" else 1, f"{code}: {message}", lines=lines,
                      warnings=warnings, attempt_id=self.attempt_id, outcome=judged.outcome,
                      records=list(self.published), run_id=self.run_id,
                      commands=list(judged.commands) if judged.outcome == "applied" else [],
                      next_command="oah fix --bind --task <task>" if judged.outcome == "applied" else None)


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
    return _guarded(env, lambda redact: _bind(task, cwd=cwd, env=env, engine=engine, redact=redact), cwd=cwd)


def _bind(task: str, *, cwd, env, engine, redact) -> Result:
    if engine is not None and engine not in receipts.ENGINES:
        raise ReviewError("OAH_USAGE", _ERROR_DIAGNOSTICS["OAH_USAGE"])
    t = _task(task, cwd, env, RECORDED)
    with _task_lock(t):
        h = _history(t, capture=True)
        _safe_inputs((dataclasses.asdict(h.records), h.index_bytes.decode("utf-8"), h.index_lines), redact)
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
        _safe_inputs(saved, redact)
        if head == before:
            commands = saved.get("commands") or []
            if any(_has_credential(command, redact) for command in commands):
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
        left = [name for name in untracked if hashlib.sha256(os.fsencode(name)).hexdigest()
                in saved.get("new_file_digests", [])]
        if dirty or left:
            listed = _ids(_path_labels(dirty[:10] + [f"{name} (new, not committed)" for name in left[:10]], redact))
            raise ReviewError("OAH_FIX_DIRTY_TREE", f"the worktree still has uncommitted changes ({listed}). {RECORDED}. "
                              f"Commit what belongs to {attempt.attempt_id}'s correction and discard the rest, then "
                              f"run oah fix --bind --task {t.name} again")
        commits = _attribution(t, before, head, written_by, RECORDED,
                               replaced=None if engine else start["engine"])
        evidence = f"bound to {head} by oah fix --bind: {len(commits)} commit(s) {_authored(commits)}"
        if written_by != start["engine"]:
            evidence += (f"; written by {written_by}, not by the attempt's engine {start['engine']}: the commits "
                         f"replace its changes (--engine {written_by})")
        record = _attempt_record("attempt-head", t.name, attempt.attempt_id,
                                 {"after_head": head, "evidence": redact(evidence), "bound_at": _iso(),
                                  "engine": written_by})
        name = f"{attempt.attempt_id}-head.json"
        with _held_signals(), _publication(t, h):
            digest = receipts.publish(t.state, t.name, name, record)
    return Result("OAH_FIX_BOUND", 0, "OAH_FIX_BOUND: the committed correction is bound; run oah review "
                  "--task <task> for the next round.",
                  lines=[f"OAH_FIX_BOUND: {len(commits)} commit(s), head {head}; run oah review next."],
                  attempt_id=attempt.attempt_id, outcome=attempt.outcome,
                  records=[{"kind": "attempt-head", "digest": digest}], next_command="oah review --task <task>")


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
                saved = runs._captured_state(run.file_path("correction.json"))[1]
    except (runs.RunError, OSError):
        pass  # Never echo malformed metadata or storage exception text.
    if (not isinstance(saved, dict) or saved.get("attempt_id") != start["attempt_id"]
            or saved.get("before_head") != start["before_head"] or saved.get("outcome") != "applied"
            or not isinstance(saved.get("changes_sha256"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", saved["changes_sha256"])
            or not isinstance(saved.get("new_file_digests"), list)
            or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                   for item in saved.get("new_file_digests", []))
            or any(not isinstance(saved.get(field), list)
                   or any(not isinstance(item, str) or not item for item in saved[field])
                   for field in ("changed", "new_files", "user_files", "commands", "out_of_scope"))
            or len(saved["new_file_digests"]) != len(saved["new_files"])):
        raise ReviewError("OAH_FIX_MANIFEST_INVALID", f"the automatic correction manifest for {start['attempt_id']} "
                          f"is missing or invalid. {RECORDED}. Restore the original run metadata, then rerun "
                          f"oah fix --bind --task {t.name}; oah fix --abort settles only unfinished attempts")
    return saved


def record_manual(task: str, *, engine: str, cwd: pathlib.Path, env: Mapping[str, str],
                  findings: Sequence[str] = ()) -> Result:
    """Record a correction that `claude` or `human` already committed, exactly like a Codex attempt."""
    return _guarded(env, lambda redact: _manual(task, engine=engine, cwd=cwd, env=env, findings=tuple(findings),
                                                redact=redact), cwd=cwd)


def _manual(task: str, *, engine, cwd, env, findings, redact) -> Result:
    if engine not in MANUAL_ENGINES:
        raise ReviewError("OAH_USAGE", f"--engine must be one of {', '.join(MANUAL_ENGINES)} for a manual correction; "
                          "a Codex correction is run with oah fix --task <task>")
    t = _task(task, cwd, env, RECORDED)
    binding = _check_intent(t, RECORDED)
    with _task_lock(t):
        h = _history(t, capture=True)
        _safe_inputs((dataclasses.asdict(h.records), h.index_bytes.decode("utf-8"), h.index_lines), redact)
        _check_history(t, h, RECORDED)
        round_log = receipts.read_round_log(t.state, t.name)
        _safe_inputs((round_log, findings), redact)
        receipt = _source(t, h, RECORDED, round_log=round_log)
        head = _head(t.root)
        _check_tree(t, receipt, head, RECORDED, redact)
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
        evidence = (f"{engine} corrected {len(ids)} finding(s) by hand in {len(commits)} commit(s) after {before[:12]} "
                    "(oah fix --manual)")
        now = _iso()
        records = [
            ("start", {"source_receipt": receipt.name, "source_receipt_digest": receipt.digest, "finding_ids": ids,
                       "class_ids": class_ids, "before_head": before, "engine": engine, "manual": True,
                       "started_at": now}, None),
            ("outcome", {"outcome": "applied", "after_head": None, "evidence": redact(evidence), "finished_at": now},
             {"outcome": "applied"}),
            ("head", {"after_head": head, "evidence": redact(f"bound to {head} by oah fix --manual: "
                                                             f"{_authored(commits)}"), "bound_at": now,
                      "engine": engine}, None),
        ]
        published = []
        _safe_inputs(records, redact)
        with _held_signals(), _publication(t, h):
            for kind, fields, summary in records:
                name = f"{attempt_id}-{kind}.json"
                record = _attempt_record(f"attempt-{kind}", t.name, attempt_id, fields)
                published.append({"kind": f"attempt-{kind}", "digest": receipts.publish(t.state, t.name, name, record,
                                                                           summary=summary)})
    return Result("OAH_FIX_RECORDED", 0, "OAH_FIX_RECORDED: the manual correction is recorded and bound; "
                  "run oah review --task <task> for the next round.",
                  lines=[f"OAH_FIX_RECORDED: {len(commits)} commit(s), head {head}; run oah review next."],
                  attempt_id=attempt_id, outcome="applied", records=published,
                  next_command="oah review --task <task>")


def abort(task: str, *, cwd: pathlib.Path, env: Mapping[str, str]) -> Result:
    """Record `aborted` for attempts whose `oah fix` stopped without an outcome; nothing else changes."""
    return _guarded(env, lambda redact: _abort(task, cwd=cwd, env=env, redact=redact), cwd=cwd)


def _abort(task: str, *, cwd, env, redact) -> Result:
    t = _task(task, cwd, env, RECORDED)
    with _task_lock(t):
        h = _history(t, capture=True)
        _safe_inputs((dataclasses.asdict(h.records), h.index_bytes.decode("utf-8"), h.index_lines), redact)
        unfinished = h.unfinished()
        if not unfinished:
            waiting = h.waiting()
            hint = (f" {waiting[0].attempt_id} waits for its commit instead: commit it, then run oah fix --bind --task "
                    f"{t.name}." if waiting else "")
            raise ReviewError("OAH_FIX_NOTHING_TO_ABORT", f"task {t.name} has no unfinished correction attempt. "
                              f"{RECORDED}.{hint}")
        problems = list(h.records.problems)
        for item in h.attempts:
            # Only the landed reader's missing-outcome diagnostic is expected during abort.
            problems.extend(problem for problem in item.problems if not (
                item in unfinished and problem.startswith(
                    f"{item.attempt_id} is unfinished: it has a start record but no outcome,")))
        if problems:
            raise ReviewError("OAH_FIX_HISTORY_INVALID", _ERROR_DIAGNOSTICS["OAH_FIX_HISTORY_INVALID"])
        planned = []
        for attempt in unfinished:
            start = h.start(attempt)
            run, status = None, "unknown"
            if isinstance(start.get("run_id"), str):
                try:
                    found = runs.find_run(t.state, start["run_id"])
                    data = found.read()
                    _safe_inputs(data, redact)
                    task_digest = "sha256:" + hashlib.sha256(t.name.encode("utf-8")).hexdigest()
                    if (found.task != t.name or data.get("task") not in (t.name, task_digest)
                            or data.get("run_id") != start["run_id"]
                            or data.get("engine") != ENGINE or start.get("engine") != ENGINE
                            or data.get("stage") != STAGE):
                        raise ReviewError("OAH_FIX_HISTORY_INVALID", _ERROR_DIAGNOSTICS["OAH_FIX_HISTORY_INVALID"])
                    try:
                        data = runs._correction_record(data, runs.RESULT_FIELDS)
                    except runs.RunError:
                        raise StateCorrupt("OAH_STATE_CORRUPT: invalid correction run fields; restore the run "
                                           "metadata before retrying") from None
                    run = call._CorrectionRun(id=found.id, task=found.task, path=found.path, record=data)
                    status = data.get("status", "unknown")
                    if status not in (*runs.TERMINAL, "created", "prepared", "queued", "running"):
                        status = "unknown"
                except runs.RunError:
                    run, status = None, "missing"
            evidence = f"recorded by oah fix --abort; interrupted attempt; its run was last {status}"
            fields = {"outcome": "aborted", "after_head": None, "evidence": evidence, "finished_at": _iso()}
            if isinstance(start.get("run_id"), str):
                fields["run_id"] = start["run_id"]
            name = f"{attempt.attempt_id}-outcome.json"
            record = _attempt_record("attempt-outcome", t.name, attempt.attempt_id, fields)
            planned.append((name, record, run, status))
        _safe_inputs([record for _name, record, _run, _status in planned], redact)
        published = []
        with _held_signals(), _publication(t, h):
            for name, record, run, status in planned:
                published.append({"kind": "attempt-outcome", "digest": receipts.publish(
                    t.state, t.name, name, record, summary={"outcome": "aborted"})})
                if run is not None and status not in runs.TERMINAL:
                    run.result(status="lost", error="OAH_RUN_LOST: the correction stopped; inspect git diff before retrying.")
        ids = _ids(item.attempt_id for item in unfinished)
    return Result("OAH_FIX_ABORTED", 0, f"OAH_FIX_ABORTED: {ids} {'is' if len(unfinished) == 1 else 'are'} recorded "
                  "as aborted. An interrupted correction may have left changes in the leased worktree: inspect it, then "
                  "commit or discard them before the next oah fix or oah review",
                  attempt_id=unfinished[-1].attempt_id, outcome="aborted", records=published)
