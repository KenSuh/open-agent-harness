# 리뷰 요약 — 작업의 영수증·수렴 판정·커밋된 게이트 요약·바뀐 중요 경로를 읽어 PR 에 붙일 블록(글·JSON)을 만들고, 착지 검사가 쓰는 같은 읽기 함수를 내놓는다.
"""`oah review summary --task T` (PLAN 5.9) and the evidence readers `oah land` shares.

Readers
    `task_lease(state, task)` is the task's lease as recorded: the lease in `leases.json` (active or stale), else
    the last lease rebuilt from `events.jsonl`, with its intent binding and declared gate ledgers.
    `convergence(root, state, task, head, binding)` says whether the task's latest event approves exactly `head`
    for `binding`. `oah.review.rounds.gate` decides it, the same predicate `oah review` uses to skip a round.
    `committed_gate_summary(root, task, rev)` reads the gate statuses in `GATES.summary.md` as committed at `rev`
    (or, with `[gates] commit_ledgers`, the committed ledgers, read structurally).
Summary
    `build(root, state, task, env=...)` collects, in round order, every receipt of the task: verdict, outcome,
    digest, independence label, whether the effort was verified, and the reviewer's version and sha256 (never its
    path). It adds the status at HEAD, the gate summary at HEAD, the P2/P3 follow-ups of a converged latest
    receipt, the critical paths touched in that receipt's base..HEAD (`[review] critical_paths` as committed at the
    base, like the review reads them), and every history problem. `Summary.render()` fills
    `templates/review/summary.md`. Untrusted text (finding titles, locations, problems) is quoted in code spans, so
    a PR body cannot mention anyone or break the table.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import string
from importlib import resources
from typing import Mapping, Sequence

from oah import intent as oah_intent
from oah.atomic import StateCorrupt, read_jsonl
from oah.config import ConfigError
from oah.engines.proc import run_command
from oah.gates.context import LEDGER_NAME, SUMMARY_NAME
from oah.lease import TASK_RE, Store
from oah.paths import GIT_TIMEOUT, git_argv, repo_info, run_git
from oah.review import ReviewError, receipts, rounds
from oah.review import settings as review_settings

CONFIG_PATH = ".oah/config.toml"
SEVERITIES = ("P0", "P1", "P2", "P3")
_CLOSING = {"lease_finished": "finished", "lease_released": "released", "lease_expired": "expired"}
_SETTINGS = review_settings.from_section(review_settings.DEFAULTS)
_DEFAULT_STATUS_ORDER = ("met", "abandoned")


class SummaryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def check_task(task: str) -> str:
    if not TASK_RE.fullmatch(task or ""):
        raise ReviewError("OAH_TASK_INVALID", f"task {task!r} must match {TASK_RE.pattern}; choose a valid task name")
    return task


# Readers ------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TaskLease:
    open: bool  # a lease of the task is in leases.json (active or stale)
    binding: oah_intent.Binding | None
    gates: tuple[str, ...]
    worktree: str | None
    closed: str | None  # finished, released or expired once the lease is gone; None while it is open


def _binding(data: object) -> oah_intent.Binding | None:
    if data is None:
        return None
    try:
        return oah_intent.Binding.from_json(data)
    except ValueError:
        return None


def _paths(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def task_lease(state: pathlib.Path, task: str) -> TaskLease | None:
    """The task's lease from leases.json, else its last lease rebuilt from events.jsonl; None when never leased."""
    store = Store(state)
    for lease, _stale in store.snapshot():
        if lease.task == task:
            return TaskLease(True, _binding(lease.intent), tuple(lease.gates), lease.worktree, None)
    view: dict | None = None
    for event in read_jsonl(store.events_path):
        if event.get("task") != task:
            continue
        kind = event.get("event")
        if kind == "lease_started":
            view = {"binding": event.get("intent"), "gates": _paths(event.get("gates")),
                    "worktree": event.get("worktree") if isinstance(event.get("worktree"), str) else None,
                    "closed": None}
        elif view is None:
            continue
        elif kind in ("intent_bound", "intent_rebound", "lease_renewed"):
            if "intent" in event:
                view["binding"] = event["intent"]
            if "gates" in event:
                view["gates"] = _paths(event["gates"])
        elif kind in _CLOSING:
            view["closed"] = _CLOSING[kind]
    if view is None:
        return None
    return TaskLease(False, _binding(view["binding"]), view["gates"], view["worktree"], view["closed"])


@dataclasses.dataclass(frozen=True)
class Convergence:
    converged: bool
    receipt: receipts.Entry | None  # the task's latest receipt
    reason: str  # why the head is not converged; "" when it is


def _is_ancestor(root: pathlib.Path, commit: str, head: str) -> bool:
    return run_git(root, "merge-base", "--is-ancestor", commit, head).returncode == 0


def _why(latest: receipts.Entry, head: str, binding: oah_intent.Binding, log: Sequence[dict]) -> str:
    record = latest.record
    if record.get("outcome") == "changes-requested":
        return f"{latest.name} requested changes; correct them, commit, then run oah review --task {record['task']}"
    if record.get("outcome") == "needs-decision":
        return (f"{latest.name} stopped for a decision; decide, then run oah review --task {record['task']} "
                '--decision "<what you decided>"')
    if record.get("head") != head:
        return (f"{latest.name} approved head {str(record.get('head'))[:12]}, not HEAD {head[:12]}; run oah review "
                f"--task {record['task']} for the current head")
    if record.get("intent") != binding.to_json():
        return (f"{latest.name} approved another intent binding than {binding.line()}; run oah review --task "
                f"{record['task']} against the current binding")
    if any(line.get("round", 0) > record["round"] for line in log):
        return (f"a review round started after {latest.name}, so its approval no longer stands; finish that round "
                f"with oah review --task {record['task']}")
    return f"{latest.name} does not approve HEAD {head[:12]}; run oah review --task {record['task']}"


def convergence(root: pathlib.Path, state: pathlib.Path, task: str, head: str,
                binding: oah_intent.Binding | None) -> Convergence:
    """Whether the task's latest review event approves exactly `head` for `binding` (`rounds.gate`)."""
    try:
        records = receipts.load(state, task)
        log = receipts.read_round_log(state, task)
    except StateCorrupt as exc:
        return Convergence(False, None, f"the review records of task {task} cannot be read "
                                        f"({str(exc).split(': ', 1)[-1]}); repair them by hand")
    entries = records.receipts()
    latest = entries[-1] if entries else None
    if latest is None:
        problems = "; ".join(records.problems)
        return Convergence(False, None, f"task {task} has no valid review receipt" + (f" ({problems})" if problems
                                                                                    else "") +
                           f"; run oah review --task {task}")
    if binding is None:
        return Convergence(False, latest, f"task {task} has no intent binding in its lease records, so no receipt "
                                          "can be matched to a current intent")
    series = rounds.series(records, log, latest.record["base"], head=head,
                           is_ancestor=lambda commit, tip: _is_ancestor(root, commit, tip))
    if series.problems:
        return Convergence(False, latest, f"the review history of task {task} is incomplete or was changed: "
                           + "; ".join(series.problems) + ". Restore or complete the records")
    waiting = rounds.waiting(series)
    if waiting is not None:
        return Convergence(False, latest, waiting)
    try:
        verdict = rounds.gate(series, head=head, decision=None, extra_round=False, settings=_SETTINGS,
                              binding=binding)
    except ReviewError:
        verdict = None  # a refusal only explains why no round would be skipped
    if verdict == rounds.CONVERGED:
        return Convergence(True, latest, "")
    return Convergence(False, latest, _why(latest, head, binding, log))


def committed_bytes(root: pathlib.Path, rev: str, path: str) -> bytes | None:
    """The bytes of `path` as committed at `rev`, or None when it is not there."""
    if run_git(root, "cat-file", "-e", f"{rev}:{path}").returncode != 0:
        return None
    result = run_command([*git_argv(), "-C", str(root), "cat-file", "blob", f"{rev}:{path}"], timeout=GIT_TIMEOUT,
                         encoding="latin-1")  # latin-1 keeps every byte
    if result.timed_out or result.returncode != 0:
        raise ReviewError("OAH_REVIEW_GIT_FAILED", f"git could not read {path} at {rev[:12]} "
                          f"({result.stderr.strip() or 'no output'}); check the repository and retry")
    return result.stdout.encode("latin-1")


def summary_path(task: str) -> str:
    return f".oah/tasks/{task}/{SUMMARY_NAME}"


@dataclasses.dataclass(frozen=True)
class GateSummary:
    state: str  # committed, invalid, missing or ledgers
    verified: str | None  # unlazy or structural
    gates: tuple[tuple[str, str], ...]  # (gate id, status) in file order
    digest: str | None
    paths: tuple[str, ...] = ()

    def counts(self) -> str:
        found: dict[str, int] = {}
        for _gate, status in self.gates:
            found[status] = found.get(status, 0) + 1
        order = [s for s in _DEFAULT_STATUS_ORDER if s in found] + sorted(s for s in found if s not in
                                                                          _DEFAULT_STATUS_ORDER)
        return ", ".join(f"{found[status]} {status}" for status in order) or "no gates"

    def to_json(self) -> dict:
        return {"state": self.state, "verified": self.verified,
                "gates": [{"id": gate, "status": status} for gate, status in self.gates], "digest": self.digest}


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def parse_gate_summary(data: bytes) -> tuple[str | None, tuple[tuple[str, str], ...]] | None:
    """(verified, gates) of a GATES.summary.md, or None when it breaks the summary grammar."""
    from oah.gates import summary as gate_summary

    try:
        lines = data.decode("utf-8").replace("\r\n", "\n").split("\n")
    except UnicodeDecodeError:
        return None
    if lines and lines[-1] == "":
        lines.pop()
    if (len(lines) < 2 or lines[0] != gate_summary.HEADER or not lines[1].startswith("verified: ")
            or not all(gate_summary.LINE_RE.match(line) for line in lines)):
        return None
    gates = tuple((line.split(" ")[0], line.split(" ")[1]) for line in lines[2:]
                  if gate_summary.ID_RE.fullmatch(line.split(" ")[0]))
    return lines[1].split(": ", 1)[1], gates


def committed_gate_summary(root: pathlib.Path, task: str, rev: str, *, commit_ledgers: bool = False,
                           ledgers: Sequence[str] = ()) -> GateSummary:
    """The task's committed gate record at `rev`: GATES.summary.md, or the ledgers with `commit_ledgers`."""
    check_task(task)
    if commit_ledgers:
        from oah.gates import parse_text, structural_status

        paths = tuple(ledgers) or (f".oah/tasks/{task}/{LEDGER_NAME}",)
        gates: list[tuple[str, str]] = []
        identity = []
        for path in paths:
            data = committed_bytes(root, rev, path)
            if data is None:
                return GateSummary("missing", None, (), None, paths)
            status = structural_status(parse_text(data.decode("utf-8", errors="replace"), pathlib.Path(path)))
            gates += list(status.states)
            identity.append([path, hashlib.sha256(data).hexdigest()])
        return GateSummary("ledgers", "structural", tuple(gates),
                           _digest(json.dumps(identity, separators=(",", ":")).encode("utf-8")), paths)
    path = summary_path(task)
    data = committed_bytes(root, rev, path)
    if data is None:
        return GateSummary("missing", None, (), None, (path,))
    parsed = parse_gate_summary(data)
    if parsed is None:
        return GateSummary("invalid", None, (), _digest(data), (path,))
    return GateSummary("committed", parsed[0], parsed[1], _digest(data), (path,))


# Rendering helpers -------------------------------------------------------------------------------------


def code_span(text: object, limit: int = 200) -> str:
    """One-line markdown code span for untrusted text: no mention, link or table break can escape it."""
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[:limit - 1] + "…"
    if not flat:
        return "(none)"
    fence = "`" * (max((len(run) for run in re.findall(r"`+", flat)), default=0) + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{fence}{pad}{flat}{pad}{fence}"


def _cell(text: object) -> str:
    return code_span(text).replace("|", "\\|")


def independence_label(value: object) -> str:
    if value is True:
        return "independent"
    if value is False:
        return "not independent"
    return "independence unknown"


# The summary -------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Round:
    name: str
    digest: str
    number: int
    series_round: int
    base: str
    head: str
    verdict: str
    outcome: str
    independent: object
    effort_verified: bool
    reviewer: dict
    findings: dict
    stop_reason: str | None

    @classmethod
    def from_entry(cls, entry: receipts.Entry) -> "Round":
        record = entry.record
        reviewer = record.get("reviewer") if isinstance(record.get("reviewer"), dict) else {}
        counts = {severity: sum(1 for item in record["findings"] if item.get("severity") == severity)
                  for severity in SEVERITIES}
        return cls(entry.name, entry.digest, record["round"], record["series_round"], record["base"], record["head"],
                   record["verdict"], record["outcome"], record.get("independent", "unknown"),
                   reviewer.get("effort_verified") is True,
                   {"engine": reviewer.get("engine"), "cli_version": reviewer.get("cli_version"),
                    "cli_sha256": reviewer.get("cli_sha256")},
                   counts, record.get("decision_stop_reason"))

    def to_json(self) -> dict:
        return {"receipt": self.name, "digest": self.digest, "round": self.number, "series_round": self.series_round,
                "base": self.base, "head": self.head, "verdict": self.verdict, "outcome": self.outcome,
                "independent": self.independent, "independence": independence_label(self.independent),
                "effort_verified": self.effort_verified, "reviewer": dict(self.reviewer),
                "findings": dict(self.findings), "decision_stop_reason": self.stop_reason}

    def row(self) -> str:
        sha = str(self.reviewer.get("cli_sha256") or "unknown")[:12]
        name = " ".join(str(part) for part in (self.reviewer.get("engine"), self.reviewer.get("cli_version")) if part)
        reviewer = f"{_cell(name or 'reviewer')} sha256 {_cell(sha)}"
        effort = "effort verified" if self.effort_verified else "effort unverified"
        cells = [f"R{self.number}", f"`{self.base[:12]}..{self.head[:12]}`", self.verdict, self.outcome,
                 independence_label(self.independent), effort, reviewer, f"`{self.digest}`"]
        return "| " + " | ".join(cells) + " |"


@dataclasses.dataclass(frozen=True)
class Summary:
    task: str
    head: str
    intent: str | None
    convergence: Convergence
    rounds: tuple[Round, ...]
    follow_ups: tuple[dict, ...]
    critical: tuple[str, ...]
    gates: GateSummary
    problems: tuple[str, ...]

    def status(self) -> str:
        if self.convergence.converged:
            receipt = self.convergence.receipt
            return f"converged at head `{self.head[:12]}` ({receipt.name}, `{receipt.digest}`)"
        return "not converged: " + self.convergence.reason

    def gate_text(self) -> str:
        gates = self.gates
        where = code_span(gates.paths[0]) if gates.paths else "the gate record"
        if gates.state == "committed":
            return f"{gates.counts()} (verified by {gates.verified}) · `{SUMMARY_NAME}` `{gates.digest}`"
        if gates.state == "invalid":
            return (f"{where} at HEAD does not match the summary grammar; run oah gates check {self.task} again and "
                    "commit it")
        if gates.state == "ledgers":
            return f"{gates.counts()} (ledgers committed with [gates] commit_ledgers; structural reading)"
        return f"no {where} committed at HEAD; run oah gates check {self.task}, then commit it"

    def render(self) -> str:
        template = string.Template(resources.files("oah").joinpath("templates/review/summary.md")
                                   .read_text(encoding="utf-8"))
        follow_ups = [f"- {item['finding_id']} {item['severity']} {item['axis']} {code_span(item['location'])} — "
                      f"{code_span(item['title'])}" for item in self.follow_ups]
        problems = ("\n**History problems** (`oah land` refuses until they are resolved):\n" +
                    "\n".join(f"- {code_span(problem, 400)}" for problem in self.problems)) if self.problems else ""
        text = template.substitute(
            task=f"`{self.task}`", intent=f"`{self.intent}`" if self.intent else "no intent binding recorded",
            head=f"`{self.head}`", status=self.status(),
            rounds="\n".join(item.row() for item in self.rounds) or "| (none) | | | | | | | |",
            gates=self.gate_text(), follow_ups="\n".join(follow_ups) or "- none",
            critical="\n".join(f"- {code_span(path)}" for path in self.critical) or "- none", problems=problems)
        return text.rstrip("\n") + "\n"

    def to_json(self) -> dict:
        receipt = self.convergence.receipt
        return {"task": self.task, "head": self.head, "intent": self.intent,
                "status": {"converged": self.convergence.converged, "receipt": receipt.name if receipt else None,
                           "reason": self.convergence.reason},
                "rounds": [item.to_json() for item in self.rounds],
                "follow_ups": [dict(item) for item in self.follow_ups],
                "critical_paths": list(self.critical), "gates": self.gates.to_json(), "problems": list(self.problems),
                "markdown": self.render()}


def _follow_ups(latest: receipts.Entry | None) -> tuple[dict, ...]:
    if latest is None or latest.record.get("outcome") != "converged":
        return ()
    findings = {item.get("finding_id"): item for item in latest.record["findings"]}
    chosen = []
    for finding_id in latest.record.get("follow_ups") or []:
        item = findings.get(finding_id, {})
        chosen.append({"receipt": latest.name, "finding_id": finding_id, "severity": item.get("severity"),
                       "axis": item.get("axis"), "location": item.get("location"), "title": item.get("title")})
    return tuple(chosen)


def _critical(root: pathlib.Path, base: str, head: str) -> tuple[str, ...]:
    from oah.review import context
    from oah.review.runner import config_at

    changed = context.changed_files(root, base, head)
    conf = review_settings.from_config(config_at(root, base))
    found = context.critical(changed, conf.critical_paths)
    if CONFIG_PATH in changed and CONFIG_PATH not in found:
        found.append(CONFIG_PATH)
    return tuple(sorted(found))


def build(root: pathlib.Path, state: pathlib.Path, task: str, *, env: Mapping[str, str]) -> Summary:
    """Everything the summary block shows, read from the task's records and the commits at HEAD."""
    from oah.config import load_config
    from oah.gates.context import DEFAULTS as GATE_DEFAULTS

    check_task(task)
    head = repo_info(pathlib.Path(root)).head
    if head is None:
        raise SummaryError("OAH_REVIEW_SUMMARY_EMPTY", "this repository has no commits yet; commit the work, review "
                           f"it with oah review --task {task}, then ask for the summary")
    records = receipts.load(state, task)
    entries = records.receipts()
    if not entries and not records.problems:
        raise SummaryError("OAH_REVIEW_SUMMARY_EMPTY", f"task {task} has no review receipts yet; run oah review "
                           f"--task {task} first, then ask for the summary")
    problems = list(records.problems)
    lease = task_lease(state, task)
    binding = lease.binding if lease else None
    status = convergence(root, state, task, head, binding)
    latest = entries[-1] if entries else None
    critical: tuple[str, ...] = ()
    if latest is not None:
        try:
            critical = _critical(root, latest.record["base"], head)
        except (ReviewError, ConfigError) as exc:
            problems.append(f"critical paths could not be read: {exc}")
    gate_settings = load_config(pathlib.Path(root)).section("gates", GATE_DEFAULTS)
    gates = committed_gate_summary(root, task, head, commit_ledgers=bool(gate_settings["commit_ledgers"]),
                                   ledgers=lease.gates if lease else ())
    return Summary(task=task, head=head, intent=binding.line() if binding else None, convergence=status,
                   rounds=tuple(Round.from_entry(entry) for entry in entries), follow_ups=_follow_ups(latest),
                   critical=critical, gates=gates, problems=tuple(problems))
