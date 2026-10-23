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
"""
from __future__ import annotations

import re
from importlib import resources
from typing import Callable, Sequence

VERIFY_MARKER = "<!-- OAH_VERIFY_FOOTER -->"
VERIFY_FOOTER = resources.files("oah").joinpath("templates/engines/verify_footer.md").read_text(encoding="utf-8")
LEASE_STAGES = ("initial", "correction")
WORKER_LINE = ("You are a dispatched oah worker (OAH_WORKER=1, run {run_id}, task {task}). Skip any session-entry "
               "procedure and do only the task in this brief.")
_SAFE = re.compile(r"[A-Za-z0-9._/\\:@%+=-]+")

HasFooter = Callable[[str], bool]


class BriefError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _quote(word: str) -> str:
    return word if _SAFE.fullmatch(word) else '"' + word.replace('"', '\\"') + '"'


def lease_command(task: str, engine: str, packet_source: str) -> str:
    """The exact command that takes this task's lease and binds its intent."""
    return f"oah start --task {_quote(task)} --agent {_quote(engine)} --packet {_quote(packet_source)}"


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
    if stage in LEASE_STAGES:
        if packet_source and binding is not None:
            lines.append(f"- Lease: before editing, run exactly `{lease_command(task, engine, packet_source)}`. "
                         f"It binds INTENT {binding.id}@r{binding.revision} {binding.digest}.")
        elif scope:
            scopes = " ".join(f"--scope {_quote(path)}" for path in scope)
            lines.append(f"- Lease: before editing, run exactly `oah start --task {_quote(task)} --agent "
                         f"{_quote(engine)} {scopes}`.")
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
    text = _lf(stored)
    count = _count(text, marker)
    if count == 0:
        return _check_delivered(text.rstrip("\n") + "\n\n" + footer, footer, marker, has_footer)
    body, tail = text.rstrip("\n"), footer.rstrip("\n")
    if count == 1 and (body == tail or body.endswith("\n" + tail)):
        return _check_delivered(body + "\n", footer, marker, has_footer)
    problem = ("carries the intent footer more than once" if count > 1 else
               "carries an intent footer that is not the canonical one or is not at the end")
    raise BriefError("OAH_FOOTER_NOT_APPENDED", f"the stored brief {problem}; rebuild the brief instead of "
                     "reusing it. Nothing was dispatched")


def strip_intent_footer(brief: str, *, footer: str, has_footer: HasFooter) -> str:
    """The brief without its one intent footer, for comparing with a baseline built without it."""
    marker = _marker(footer, has_footer)
    footer = _lf(footer)
    text = _lf(brief)
    count = _count(text, marker)
    if count == 0:
        raise BriefError("OAH_FOOTER_MISSING", "the brief has no intent footer")
    if count > 1:
        raise BriefError("OAH_FOOTER_DUPLICATED", f"the brief has {count} intent footers; it must have exactly one")
    if not text.endswith("\n" + footer):
        raise BriefError("OAH_FOOTER_MISPLACED", "the intent footer is not the last part of the brief")
    return text[:-len(footer)].rstrip("\n")
