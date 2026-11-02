# 도구 업데이트 판정 — 확인·부재·미확인 근거를 받아 모든 구성요소의 상태와 이유·다음 단계를 한곳에서 정한다.
"""Pure component decisions. Unknown evidence always prevents a settled or unsupported result."""
from __future__ import annotations

import dataclasses
from typing import Literal

UPDATED, CURRENT = "updated", "current"
UNVERIFIED, NEEDS_CONFIRMATION, UNSUPPORTED, FAILED = "unverified", "needs-confirmation", "unsupported", "failed"
STATUSES = (UPDATED, CURRENT, UNVERIFIED, NEEDS_CONFIRMATION, UNSUPPORTED, FAILED)
SETTLED = (UPDATED, CURRENT)
COMPLETE = (*SETTLED, UNSUPPORTED)

# These describe the command's answer, not a component's status.
UP_TO_DATE, CHANGED, CONFIRMATION = "up-to-date", "changed", "confirmation-required"
RESTART_NOTE = ("sessions already running keep the version and plugins they started with; restart them to use "
                "the new ones")


@dataclasses.dataclass(frozen=True)
class Evidence:
    state: Literal["confirmed", "absent", "unknown"]
    value: object = None
    reason: str = ""
    timed_out: bool = False


def confirmed(value: object) -> Evidence:
    return Evidence("confirmed", value=value)


def absent(reason: str = "") -> Evidence:
    return Evidence("absent", reason=reason)


def unknown(reason: str, timed_out: bool = False) -> Evidence:
    return Evidence("unknown", reason=reason, timed_out=timed_out)


def combine(items: tuple[Evidence, ...], value: object) -> Evidence:
    """Keep every unknown (including any timeout) when folding inventory or command evidence."""
    unknowns = [item for item in items if item.state == "unknown"]
    if unknowns:
        return unknown("; ".join(dict.fromkeys(item.reason for item in unknowns)),
                       timed_out=any(item.timed_out for item in unknowns))
    missing = [item for item in items if item.state == "absent"]
    if missing:
        return absent("; ".join(dict.fromkeys(item.reason for item in missing)))
    return confirmed(value)


@dataclasses.dataclass(frozen=True)
class ComponentEvidence:
    kind: Literal["cli", "marketplace", "plugin", "plugins"]
    command: str
    cli: Evidence
    # None means not probed because a prerequisite stopped the step; it is never proof of absence.
    help: Evidence | None = None
    before: Evidence | None = None
    after: Evidence | None = None
    update: Evidence | None = None
    inventory: Evidence | None = None
    attempted: bool = False
    unsupported_step: str = ""


def _version(item: Evidence | None) -> str | None:
    if item is None or item.state != "confirmed" or not isinstance(item.value, str):
        return None
    return item.value if item.value.strip() and "�" not in item.value else None


def decide(evidence: ComponentEvidence) -> tuple[str, str, str]:
    """The sole status chooser, with timeouts and other unknowns ahead of absence or version changes."""
    fields = (evidence.cli, evidence.help, evidence.before, evidence.after, evidence.update, evidence.inventory)
    unknowns = [item for item in fields if item is not None and item.state == "unknown"]
    timeouts = [item for item in unknowns if item.timed_out]
    command = evidence.command
    if timeouts:
        if evidence.attempted:
            reason = "timed out; the tool may be partly updated"
            return FAILED, reason, f"{reason}; run {command} yourself to check it"
        if evidence.help is not None and evidence.help.timed_out:
            reason = evidence.help.reason
        elif any(item is not None and item.timed_out for item in (evidence.before, evidence.after)):
            reason = "version check timed out"
        else:
            reason = timeouts[0].reason
        return FAILED, reason, f"{reason}; run {command} yourself to check it"
    if unknowns:
        reason = "; ".join(dict.fromkeys(item.reason for item in unknowns))
        if evidence.cli.state == "unknown":
            return UNVERIFIED, reason, evidence.cli.reason
        if evidence.help is not None and evidence.help.state == "unknown":
            return UNVERIFIED, reason, reason
        return UNVERIFIED, reason, f"run {command} yourself and read what it says; {reason}"
    if evidence.cli.state == "absent":
        reason = evidence.cli.reason or "the CLI is not installed"
        return UNSUPPORTED, reason, reason
    if evidence.help is not None and evidence.help.state == "absent":
        reason = evidence.help.reason or "the command is absent from clean help"
        return UNSUPPORTED, reason, evidence.unsupported_step or reason

    if evidence.kind == "plugins" and evidence.inventory is not None and evidence.inventory.state == "absent":
        reason = evidence.inventory.reason or "no Claude Code plugins installed"
        return UNSUPPORTED, reason, reason

    supported = evidence.cli.state == "confirmed" and evidence.help is not None and evidence.help.state == "confirmed"
    inventory_known = (evidence.kind not in ("plugin", "plugins") or
                       (evidence.inventory is not None and evidence.inventory.state == "confirmed"))
    before, after = _version(evidence.before), _version(evidence.after)
    answered = evidence.update is not None and evidence.update.state == "confirmed"
    if supported and inventory_known and answered:
        confirmation = evidence.kind in ("plugin", "plugins") and evidence.update.value == CONFIRMATION
        if evidence.kind == "plugins" and confirmation:
            return NEEDS_CONFIRMATION, "a plugin's marketplace-declared command needs confirmation", f"run: {command}"
        if before is not None and after is not None and before != after:
            return UPDATED, "the installed version changed", RESTART_NOTE
        if confirmation:
            return NEEDS_CONFIRMATION, "the marketplace-declared command needs confirmation", f"run: {command}"
        if before is not None and after is not None and evidence.update.value == UP_TO_DATE:
            return CURRENT, "the readable version is unchanged and up to date", ""
    reason = "the evidence does not verify the installed result"
    return UNVERIFIED, reason, f"run {command} yourself and read what it says"
