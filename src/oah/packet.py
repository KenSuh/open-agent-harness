# 작업 패킷 — .oah/tasks/<task>/PACKET.md 를 템플릿에서 초안으로 만들고, 의도·필수 구획·Scope·과정 단계·GATES.md 구조를 검사한다.
"""Task packets: one file per task that holds the intent, scope, plan, acceptance and return format.

`create(root, task, plan=…, scope=…)` writes `.oah/tasks/<task>/PACKET.md` from the template as a draft
and never overwrites. `plan_text(StagePlan…)` writes the `## Plan` body that `oah packet new` scores: one
`- <stage> → <engine> → <tier>` line per stage (the format `oah call` reads), then the axes, the rule
reasons, the advisory judgement and the selected skills with their reasons and `rerank=` status. Skill
lines name the skill and its origin, never a path, because the packet is committed. `check(path)` reads it safely (`oah.intent.read_source`) and reports problems:

- errors: the intent section's own problems (`oah.intent.parse`), a missing or duplicated
  `## Scope`, `## Plan`, `## Acceptance` or `## Return` section, a status other than accepted
  (`OAH_INTENT_NOT_ACCEPTED`) when `require_accepted`, an `Intent-ID` that differs from the task
  (`OAH_INTENT_ID_CHANGED`), and a `GATES.md` beside the packet that does not parse
  structurally (`OAH_GATES_INVALID`);
- warnings: an empty Scope (`OAH_SCOPE_EMPTY`), a success condition that reads like a process
  step (`OAH_INTENT_PROCESS_STEP`), and template placeholders left in the intent
  (`OAH_PACKET_PLACEHOLDER`).

Scope entries are the top-level bullets of `## Scope`, without backticks; HTML comments are ignored.
"""
from __future__ import annotations

import dataclasses
import importlib.resources
import os
import pathlib
import re
from typing import Sequence

from oah import intent
from oah.intent import IntentInvalid, Problem, SourceError
from oah.lease import TASK_RE

PACKET_NAME = "PACKET.md"
GATES_NAME = "GATES.md"
REQUIRED_SECTIONS = ("Scope", "Plan", "Acceptance", "Return")
PLACEHOLDERS = ("<what is wrong or missing today>", "<what is true when this task is done>",
                "<checkable condition>")
_TASK_TOKEN = "{{TASK}}"
_PLAN_TOKEN = "{{PLAN}}"
_SCOPE_TOKEN = "{{SCOPE}}"
DEFAULT_PLAN = "- initial implementation → claude\n- review → codex\n- correction → codex\n"

# Phrases that describe how the work is done rather than what is true afterwards (PLAN 5.6, A7).
PROCESS_PATTERNS = (
    (re.compile(r"(?i)\b(take|takes|taking|took|hold|holds|holding|acquire[sd]?|get|gets|renew|renews)\s+"
                r"(?:a\s+|the\s+|its\s+|your\s+)?lease\b"), "taking a lease"),
    (re.compile(r"(?i)\blease\s+(?:is|was|gets)\s+(?:taken|held|acquired|renewed|released|finished)\b"),
     "taking a lease"),
    (re.compile(r"(?i)\b(?:commit|push|rebase)\s+(?:the|all|your|changes|work|it|to)\b"), "committing"),
    (re.compile(r"(?i)\b(?:is|are|be|been|get|gets)\s+(?:committed|pushed|merged|rebased)\b"), "committing"),
    (re.compile(r"(?i)\bsmall\s+(?:logical\s+)?commits\b"), "committing"),
    (re.compile(r"(?i)\b(?:open|opens|opened|create|creates|file|submit)\s+(?:a|the)\s+(?:PR|pull request)\b"),
     "a pull request"),
    (re.compile(r"(?i)\b(?:PR|pull request)\s+(?:is|was|gets)\s+(?:opened|created|merged)\b"), "a pull request"),
    (re.compile(r"(?i)\breview\s+rounds?\b"), "a review round"),
    (re.compile(r"(?i)\b(?:codex|code|the|a)\s+review\s+(?:passes|passed|is done|is complete|approves|finds)\b"),
     "a review round"),
    (re.compile(r"(?i)\b(?:pass|passes|passed|survive|survives|clear|clears)\s+(?:the\s+|a\s+|codex\s+)?review\b"),
     "a review round"),
    (re.compile(r"(?i)\b(?:is|are|gets|been)\s+(?:reviewed|approved)\b"), "a review round"),
    (re.compile(r"\bR[1-9]\b"), "a review round"),
    (re.compile(r"(?i)\bthe\s+return\s+(?:carries|includes|contains|lists|has|ends with)\b"), "the return format"),
    (re.compile(r"(?i)\breturn\s+format\b"), "the return format"),
    (re.compile(r"(?i)\b(?:run|runs|ran|call|calls|use|uses)\s+`?oah\s+(?:start|renew|finish|release)\b"),
     "an oah process command"),
)

_SECTION = re.compile(r"## +(\S+)")
_SCOPE_BULLET = re.compile(r"[-*+]\s+(.*\S)")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_GATE = re.compile(r"- \[([ xX])\] (G[1-9][0-9]*):\s*\S.*")
_GATE_FIELD = re.compile(r"\s+(CHECK|EXPECT|EVIDENCE):(.*)")
_ABANDON = re.compile(r"\s*ABANDON:\s*(G[1-9][0-9]*)\s+\S.*")
GATE_FIELDS = ("CHECK", "EXPECT", "EVIDENCE")


class PacketError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass(frozen=True)
class CheckResult:
    path: pathlib.Path | None
    intent: intent.Intent | None
    scope: tuple[str, ...]
    problems: tuple[Problem, ...]

    @property
    def errors(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.level == "error")

    @property
    def warnings(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.level == "warning")


def _check_task(task: str) -> None:
    if not TASK_RE.fullmatch(task or ""):
        raise PacketError("OAH_TASK_INVALID", f"{task!r}: use letters, digits, '.', '_' or '-' (max 64), "
                                              "starting with a letter or digit")


def task_dir(root: pathlib.Path, task: str) -> pathlib.Path:
    _check_task(task)
    return pathlib.Path(root) / ".oah" / "tasks" / task


def packet_path(root: pathlib.Path, task: str) -> pathlib.Path:
    return task_dir(root, task) / PACKET_NAME


def task_of(path: pathlib.Path) -> str | None:
    """The task name when `path` is `.oah/tasks/<task>/PACKET.md`, else None."""
    parts = pathlib.Path(path).parts
    if len(parts) >= 4 and parts[-1] == PACKET_NAME and parts[-3] == "tasks" and parts[-4] == ".oah":
        return parts[-2] if TASK_RE.fullmatch(parts[-2]) else None
    return None


def render(task: str, *, plan: str | None = None, scope: Sequence[str] = ()) -> str:
    """The packet template for `task`, as a draft, with this `## Plan` body and these Scope paths."""
    _check_task(task)
    template = importlib.resources.files("oah").joinpath("templates/packet/PACKET.md")
    text = template.read_text(encoding="utf-8").replace("\r\n", "\n").replace(_TASK_TOKEN, task)
    body = plan if plan is not None else DEFAULT_PLAN
    text = text.replace(_PLAN_TOKEN, body if body.endswith("\n") else body + "\n")
    return text.replace(_SCOPE_TOKEN, "".join(f"- {entry}\n" for entry in scope))


def create(root: pathlib.Path, task: str, *, plan: str | None = None, scope: Sequence[str] = ()) -> pathlib.Path:
    """Write the draft packet for `task`; refuses with OAH_PACKET_EXISTS rather than overwrite."""
    text = render(task, plan=plan, scope=scope)
    path = packet_path(root, task)
    if os.path.lexists(path):
        raise PacketError("OAH_PACKET_EXISTS", f"{path} already exists; edit it, and raise its Revision when "
                                               "the goal changes")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise PacketError("OAH_PACKET_EXISTS", f"{path} already exists; edit it instead") from exc
    return path


@dataclasses.dataclass(frozen=True)
class StagePlan:
    """One stage of `## Plan`: its engine, rule-scored axes and tier, and the skills chosen for it."""

    stage: str
    engine: str
    axes: object  # routing.Axes
    tier: str
    reasons: tuple[str, ...]
    advice: str
    selection: object  # skillsel.Selection
    rerank: str


def plan_text(plans: Sequence[StagePlan]) -> str:
    """The `## Plan` body: stage lines `oah call` reads, then how each tier and skill was chosen."""
    lines = [f"- {plan.stage} → {plan.engine} → {plan.tier}" for plan in plans]
    lines += ["", "Scored by rules when oah packet new made this packet. Change a tier above to override it; "
              "oah call records that as tier source packet."]
    for plan in plans:
        axes = plan.axes
        lines.append(f"- {plan.stage}: kind={axes.kind} intensity={axes.intensity} reasoning={axes.reasoning} "
                     f"risk={axes.risk} → {plan.tier} (advice={plan.advice})")
        lines.append(f"  - why: {'; '.join(plan.reasons)}")
        lines.append(f"  - skills (rerank={plan.rerank}):")
        for skill in plan.selection.skills:
            lines.append(f"    - `{skill.id}` ({skill.origin}): {'; '.join(skill.reasons)}")
        if not plan.selection.skills:
            lines.append("    - none selected")
        for item in plan.selection.unavailable:
            lines.append(f"    - wanted, not used: `{item.skill}`: {item.reason}")
        for warning in plan.selection.warnings:
            lines.append(f"    - warning: {warning}")
    return "\n".join(lines) + "\n"


# Checking ---------------------------------------------------------------------------------------


def _sections(lines: list[str]) -> dict[str, list[list[str]]]:
    """Level-2 sections outside the intent markers: name -> list of bodies (one per occurrence)."""
    found: dict[str, list[list[str]]] = {}
    current: list[str] | None = None
    inside = False
    for line in lines:
        if line == intent.BEGIN:
            inside, current = True, None
            continue
        if line == intent.END:
            inside = False
            continue
        if inside:
            continue
        match = _SECTION.match(line)
        if match:
            current = []
            found.setdefault(match.group(1), []).append(current)
        elif current is not None:
            current.append(line)
    return found


def scope_entries(body: list[str]) -> tuple[str, ...]:
    text = _COMMENT.sub("", "\n".join(body))
    entries = []
    for line in text.split("\n"):
        match = _SCOPE_BULLET.fullmatch(line.rstrip())
        if match:
            entry = match.group(1).strip().strip("`").strip()
            if entry and entry not in entries:
                entries.append(entry)
    return tuple(entries)


def process_step(text: str) -> str | None:
    """What a success condition looks like when it reads like a process step, else None."""
    for pattern, label in PROCESS_PATTERNS:
        if pattern.search(text):
            return label
    return None


_RERANK_OFF = re.compile(
    r"(?i)\b(?:no|without|disable[sd]?|turn(?:s|ed)?\s+off|switch(?:es|ed)?\s+off)\s+(?:the\s+|any\s+)?re-?ranker\b"
    r"|\bre-?ranker\s*(?::|=|is)\s*(?:off|disabled|no|none)\b"
    r"|\bre-?ranker\s+(?:off|disabled)\b")


def rerank_disabled(text: str) -> bool:
    """True when the intent's Constraints turn the re-ranker off (PLAN 5.6), outside HTML comments.

    "no network except the re-ranker …" names the re-ranker without turning it off, so it does not count.
    """
    section = intent.normalize_newlines(_COMMENT.sub("", text))
    lines = section.split("\n")
    try:
        start = lines.index("### Constraints") + 1
    except ValueError:
        return False
    for line in lines[start:]:
        if line.startswith(("### ", "## ")) or line == intent.END:
            break
        if _RERANK_OFF.search(line):
            return True
    return False


def check_gates(text: str) -> list[str]:
    """Structural issues in an unlazy gate ledger; empty when it parses."""
    issues: list[str] = []
    gates: dict[str, dict[str, int]] = {}
    current: str | None = None
    abandons: list[tuple[int, str]] = []
    for number, line in enumerate(intent.normalize_newlines(text).split("\n"), start=1):
        if line.startswith("- ["):
            match = _GATE.fullmatch(line.rstrip())
            if match is None:
                issues.append(f"line {number}: a gate line looks like '- [ ] G1: <result>' or '- [x] G1: <result>'")
                current = None
                continue
            current = match.group(2)
            if current in gates:
                issues.append(f"line {number}: {current} is used more than once")
            gates[current] = {name: 0 for name in GATE_FIELDS}
            continue
        if line.lstrip().startswith("ABANDON:"):
            match = _ABANDON.fullmatch(line.rstrip())
            if match is None:
                issues.append(f"line {number}: write 'ABANDON: G<n> <reason>'")
            else:
                abandons.append((number, match.group(1)))
            continue
        field = _GATE_FIELD.fullmatch(line.rstrip())
        if field is None:
            continue
        if current is None:
            issues.append(f"line {number}: {field.group(1)} appears before any gate line")
        else:
            gates[current][field.group(1)] += 1
    if not gates:
        issues.append("no gate lines ('- [ ] G1: <result>')")
    for gate, counts in gates.items():
        for name, count in counts.items():
            if count != 1:
                issues.append(f"{gate} has {count} {name} lines; it needs exactly one")
    for number, gate in abandons:
        if gate not in gates:
            issues.append(f"line {number}: ABANDON names {gate}, which is not a gate")
    return issues


def check_text(text: str, *, task: str | None = None, require_accepted: bool = True,
               gates_text: str | None = None, path: pathlib.Path | None = None) -> CheckResult:
    problems: list[Problem] = []
    parsed: intent.Intent | None = None
    try:
        parsed = intent.parse(text)
    except IntentInvalid as exc:
        problems.extend(exc.problems)
    lines = intent.normalize_newlines(text).split("\n")
    sections = _sections(lines)
    for name in REQUIRED_SECTIONS:
        count = len(sections.get(name, []))
        if count == 0:
            problems.append(Problem("OAH_PACKET_SECTION_MISSING", f"the ## {name} section is missing; add it "
                                                                  "after the intent section"))
        elif count > 1:
            problems.append(Problem("OAH_PACKET_SECTION_DUPLICATE", f"## {name} appears {count} times; keep one"))
    if parsed is not None:
        if require_accepted and parsed.status != "accepted":
            problems.append(Problem("OAH_INTENT_NOT_ACCEPTED",
                                    f"Status is {parsed.status}. Read the intent; when it says what you want, "
                                    "change Status to accepted (a draft cannot be bound to a lease)"))
        if task is not None and parsed.id != task:
            problems.append(Problem("OAH_INTENT_ID_CHANGED",
                                    f"Intent-ID is {parsed.id} but the task is {task}. Keep Intent-ID: {task}; a "
                                    "changed goal keeps its ID and raises Revision instead"))
        for ac_id, condition in parsed.conditions:
            label = process_step(condition)
            if label:
                problems.append(Problem("OAH_INTENT_PROCESS_STEP",
                                        f"{ac_id} reads like a process step ({label}): {condition!r}. Success "
                                        "conditions describe outcomes; move process steps outside the intent "
                                        "section, or ignore this if it is the product's own behaviour",
                                        "warning"))
        left = [holder for holder in PLACEHOLDERS if holder in parsed.section]
        if left:
            problems.append(Problem("OAH_PACKET_PLACEHOLDER",
                                    f"template text is still in the intent: {', '.join(left)}; replace it",
                                    "warning"))
    scope_bodies = sections.get("Scope", [])
    scope = scope_entries(scope_bodies[0]) if scope_bodies else ()
    if scope_bodies and not scope:
        problems.append(Problem("OAH_SCOPE_EMPTY",
                                "## Scope lists no paths. Add one bullet per path this task may write, or "
                                "'- repo' for the whole repository; oah start --packet refuses an empty Scope",
                                "warning"))
    if gates_text is not None:
        issues = check_gates(gates_text)
        if issues:
            problems.append(Problem("OAH_GATES_INVALID", f"{GATES_NAME}: " + "; ".join(issues)))
    return CheckResult(path=path, intent=parsed, scope=scope, problems=tuple(problems))


def check(path: pathlib.Path, *, task: str | None = None, require_accepted: bool = True) -> CheckResult:
    """Check a packet file and the GATES.md beside it, when present."""
    path = pathlib.Path(path)
    try:
        text = intent.read_source(path)
    except SourceError as exc:
        return CheckResult(path=path, intent=None, scope=(), problems=(Problem(exc.code, str(exc).split(": ", 1)[1]),))
    gates_path = path.parent / GATES_NAME
    gates_text = None
    gates_problem: list[Problem] = []
    if os.path.lexists(gates_path):
        try:
            gates_text = intent.read_source(gates_path)
        except SourceError as exc:
            gates_problem.append(Problem("OAH_GATES_INVALID", str(exc)))
    result = check_text(text, task=task, require_accepted=require_accepted, gates_text=gates_text, path=path)
    return dataclasses.replace(result, problems=result.problems + tuple(gates_problem))
