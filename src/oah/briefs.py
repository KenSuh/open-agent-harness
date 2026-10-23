# 작업 지시서 — 작업자 줄·서문·의도 구획 원문·라운드 지시·검증 꼬리말·의도 꼬리말의 고정 배치로 브리프를 만들고, 저장된 브리프에는 정본 꼬리말을 붙인다.
"""Worker briefs with one fixed layout (C2):

1. the worker line (A6): the first line says this is a dispatched worker and to skip session entry;
2. the preamble: task, stage, engine, scope, rules, packet, the lease command, skills, return format;
3. the intent section, verbatim (A7);
4. the round instructions;
5. the verify footer (`VERIFY_FOOTER`, the verify-what-changed rule);
6. the intent footer, last.

Only implementation and correction briefs carry a lease command and the `Agent-Engine: <engine>` commit
trailer (5.9); with a packet the lease command is the exact
`oah start --packet <source>` command and names the binding it will create (C1). Review briefs never
carry one. A footer counts only when its marker is a whole line at line start (C4); the intent
footer and its `has_footer` test come from `oah.intent` and are passed in. `deliver` reuses a stored
brief or spec: it appends the canonical footer, keeps one already canonical footer, and otherwise
refuses with `OAH_FOOTER_NOT_APPENDED` (C3). `strip_intent_footer` removes exactly one intent footer at
its fixed position, for comparing a brief with an older baseline.

`intent_footer()` is the one shared intent footer, the same for every engine; lane P7b installs it into
each generated role definition. `raw_section(text)` cuts the intent section out of a packet exactly as
`oah.intent.read_source` returned it (line endings and all), so briefs carry it byte-intact.
"""
from __future__ import annotations

import os
import re
from importlib import resources
from typing import Callable, Sequence

INTENT_BEGIN = "<!-- OAH_INTENT:BEGIN -->"
INTENT_END = "<!-- OAH_INTENT:END -->"
VERIFY_MARKER = "<!-- OAH_VERIFY_FOOTER -->"
VERIFY_FOOTER = resources.files("oah").joinpath("templates/engines/verify_footer.md").read_text(encoding="utf-8")
LEASE_STAGES = ("initial", "correction")
WORKER_LINE = ("You are a dispatched oah worker (OAH_WORKER=1, run {run_id}, task {task}). Skip any session-entry "
               "procedure and do only the task in this brief.")
_COMMAND_SAFE = re.compile(r"[A-Za-z0-9._/-]+")

HasFooter = Callable[[str], bool]


class BriefError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def intent_footer() -> str:
    """The shared intent footer for every brief and role definition, from oah.intent (lane P4a)."""
    try:
        from oah import intent
    except ImportError as exc:
        raise BriefError("OAH_FOOTER_NOT_APPENDED", "this oah build has no canonical intent footer (oah.intent is "
                         "missing), so no worker brief can be delivered; update or reinstall oah") from exc
    return intent.FOOTER


def has_intent_footer(text: str) -> bool:
    """True when the shared footer's marker is a whole line at line start (C4)."""
    from oah import intent

    return intent.has_footer(text)


def raw_section(text: str) -> str | None:
    """The intent section from the BEGIN line through the END line, byte for byte; None unless exactly one."""
    lines = text.splitlines(keepends=True)
    begins = [n for n, line in enumerate(lines) if line.rstrip("\r\n") == INTENT_BEGIN]
    ends = [n for n, line in enumerate(lines) if line.rstrip("\r\n") == INTENT_END]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        return None
    section = "".join(lines[begins[0]:ends[0] + 1])
    return section if section.endswith(("\n", "\r")) else section + "\n"


def shell_word(word: str) -> str:
    """A command value that needs no quoting in either POSIX sh or PowerShell."""
    if not _COMMAND_SAFE.fullmatch(word):
        raise BriefError("OAH_UNSAFE_ARGUMENT", f"cannot embed {word!r} in a generated command; rename the "
                         "task, engine, packet or scope value to use only A-Z, a-z, 0-9, dot, underscore, "
                         "slash and hyphen, then retry")
    return word


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def lease_command(task: str, engine: str, packet_source: str) -> str:
    """The exact command that takes this task's lease and binds its intent."""
    return f"oah start --task {shell_word(task)} --agent {shell_word(engine)} --packet {shell_word(packet_source)}"


def _marker(footer: str, has_footer: HasFooter) -> str:
    footer = _lf(footer or "")
    marker = footer.split("\n", 1)[0]
    if not footer.strip() or not has_footer(footer) or not marker:
        raise BriefError("OAH_FOOTER_NOT_APPENDED", "the canonical intent footer is not available, so the brief "
                         "cannot be delivered; update oah, which ships the footer in oah.intent")
    return marker


def check_footer(footer: str, has_footer: HasFooter) -> None:
    """Refuse with OAH_FOOTER_NOT_APPENDED before anything is written when no canonical footer is available."""
    _marker(footer, has_footer)


def _count(text: str, marker: str) -> int:
    return sum(1 for line in _lf(text).split("\n") if line == marker)


def _check_delivered(text: str, footer: str, marker: str, has_footer: HasFooter) -> str:
    if not has_footer(text) or _count(text, marker) != 1 or not text.endswith(_lf(footer)):
        raise BriefError("OAH_FOOTER_NOT_APPENDED", "the delivered brief does not end with exactly one canonical "
                         "intent footer; nothing was dispatched")
    return text


def build(*, run_id: str, task: str, stage: str, engine: str, scope: Sequence[str], intent_section: str | None,
          binding: object | None, packet_source: str | None, round_instructions: str,
          skills: Sequence[tuple[str, str]] = (), footer: str, has_footer: HasFooter) -> str:
    """The brief text, in the fixed layout. `binding` has `.id`, `.revision` and `.digest`."""
    marker = _marker(footer, has_footer)
    footer = _lf(footer)
    scope_text = ", ".join(scope) if scope else (
        "(empty: OAH_SCOPE_EMPTY, no paths were declared; stop and ask the lead for a scope before editing)")
    lines = [WORKER_LINE.format(run_id=run_id, task=task), "", "## Brief",
             f"- Task: {task} · Stage: {stage} · Engine: {engine}",
             f"- Scope: {scope_text}",
             "- Rules: stay inside the scope; never push, merge or open pull requests; write the evidence for "
             "every gate into the task's ledger; if you are blocked, stop and say so honestly.",
             f"- Packet: {packet_source or 'none'}"]
    if os.name == "nt":
        lines.append("- Shell: run the commands in this brief in PowerShell.")
    if stage in LEASE_STAGES:
        if packet_source and binding is not None:
            lines.append(f"- Lease: before editing, run exactly `{lease_command(task, engine, packet_source)}`. "
                         f"It binds INTENT {binding.id}@r{binding.revision} {binding.digest}.")
        elif scope:
            scopes = " ".join(f"--scope {shell_word(path)}" for path in scope)
            lines.append(f"- Lease: before editing, run exactly `oah start --task {shell_word(task)} --agent "
                         f"{shell_word(engine)} {scopes}`.")
    if stage in LEASE_STAGES:
        lines.append(f"- Commits: end every commit message with the trailer `Agent-Engine: {engine}`.")
    if skills:
        lines.append("- Skills: " + "; ".join(f"{name} ({reason})" for name, reason in skills))
    else:
        lines.append("- Skills: none were selected for this brief.")
    lines.append("- Return: a short summary, every command you ran with its result, and the INTENT and AC lines "
                 "the intent rules below describe.")
    section = intent_section if intent_section is not None else (
        "No intent section: this run has no packet, so there are no success conditions to report against.\n")
    parts = ["\n".join(lines) + "\n", section if section.endswith("\n") else section + "\n",
             "## Round instructions\n" + (round_instructions.rstrip("\n") or "(none)") + "\n",
             VERIFY_FOOTER if VERIFY_FOOTER.endswith("\n") else VERIFY_FOOTER + "\n", footer]
    return _check_delivered("\n".join(parts), footer, marker, has_footer)


def deliver(stored: str, *, footer: str, has_footer: HasFooter) -> str:
    """A stored brief or spec, delivered with exactly one canonical intent footer at the end."""
    marker = _marker(footer, has_footer)
    footer = _lf(footer)
    text = stored
    count = _count(text, marker)
    if count == 0:
        separator = "\n" if text.endswith(("\n", "\r")) else "\n\n"
        return _check_delivered(text + separator + footer, footer, marker, has_footer)
    offset = _line_offset(text, marker)
    if count == 1 and _lf(text[offset:]).rstrip("\n") == footer.rstrip("\n"):
        return _check_delivered(text[:offset] + footer, footer, marker, has_footer)
    problem = ("carries the intent footer more than once" if count > 1 else
               "carries an intent footer that is not the canonical one or is not at the end")
    raise BriefError("OAH_FOOTER_NOT_APPENDED", f"the stored brief {problem}; rebuild the brief instead of "
                     "reusing it. Nothing was dispatched")


def _line_offset(text: str, marker: str) -> int:
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.rstrip("\r\n") == marker:
            return offset
        offset += len(line)
    return -1


def reusable_content(stored: str, *, stage: str, footer: str, has_footer: HasFooter) -> str:
    """Keep reusable instructions, discarding the previous dispatch's managed envelope."""
    text = deliver(stored, footer=footer, has_footer=has_footer)
    body = text[:_line_offset(text, _marker(footer, has_footer))]
    verify = _line_offset(body, VERIFY_MARKER)
    generated = body.startswith("You are a dispatched oah worker (")
    if verify >= 0:
        if _count(body, VERIFY_MARKER) != 1 or _lf(body[verify:]).rstrip("\n") != VERIFY_FOOTER.rstrip("\n"):
            raise BriefError("OAH_FOOTER_NOT_APPENDED", "the stored verify footer is not canonical or is "
                             "duplicated; rebuild the spec before dispatching")
        body = body[:verify]
    if generated:
        heading = "## Round instructions"
        offset = _line_offset(body, heading)
        if offset < 0 or _count(body, heading) != 1 or verify < 0:
            raise BriefError("OAH_BRIEF_STALE", "the stored worker envelope cannot be refreshed safely; "
                             "provide a plain spec or rebuild the brief from its packet")
        body = body[offset + len(heading):].lstrip("\r\n")
    else:
        section = raw_section(body)
        if section is not None:
            body = body.replace(section, "", 1)
    if stage == "review" and re.search(r"\boah\s+start\b", body):
        raise BriefError("OAH_BRIEF_STALE", "the reusable review instructions carry a lease command; "
                         "remove that command from the spec and retry the review")
    return body.strip("\r\n")


def strip_intent_footer(brief: str, *, footer: str, has_footer: HasFooter) -> str:
    """The brief without its one intent footer, for comparing with a baseline built without it."""
    marker = _marker(footer, has_footer)
    footer = _lf(footer)
    text = brief
    count = _count(text, marker)
    if count == 0:
        raise BriefError("OAH_FOOTER_MISSING", "the brief has no intent footer")
    if count > 1:
        raise BriefError("OAH_FOOTER_DUPLICATED", f"the brief has {count} intent footers; it must have exactly one")
    offset = _line_offset(text, marker)
    if offset <= 0 or _lf(text[offset:]) != footer:
        raise BriefError("OAH_FOOTER_MISPLACED", "the intent footer is not the last part of the brief")
    return text[:offset].rstrip("\r\n")
