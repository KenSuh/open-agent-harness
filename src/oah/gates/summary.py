# 경로 없는 게이트 요약 — 검증된 G<n> 아이디, 고정 상태값, sha256 다이제스트만 허용 목록 문법으로 써서 내보낸다.
"""GATES.summary.md: the committed, path-free record of a finished task's gates.

Grammar (one item per line, nothing else):

    # oah gates summary v1
    verified: unlazy|structural
    ledger: <sha256>                       (one per ledger, followed by its gates)
    G<n> met|abandoned binding=run|attest|carry|none title=<sha256> check=<sha256|none>
         expect=<sha256|none> evidence=<sha256> inputs=<sha256|none>      (on one line)

No raw titles, commands, expectations, paths, task labels or other free text are exported. The writer
validates every field and every line against this allowlist and refuses anything else.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Sequence

HEADER = "# oah gates summary v1"
HEX = r"[0-9a-f]{64}"
ID_RE = re.compile(r"G[1-9][0-9]{0,3}")
HEX_RE = re.compile(HEX)
STATUSES = ("met", "abandoned")
BINDINGS = ("run", "attest", "carry", "none")
VERIFIED = ("unlazy", "structural")
LINE_RE = re.compile(
    r"^(# oah gates summary v1|verified: (unlazy|structural)|ledger: " + HEX + r"|"
    r"G[1-9][0-9]{0,3} (met|abandoned) binding=(run|attest|carry|none) title=" + HEX +
    r" check=(" + HEX + r"|none) expect=(" + HEX + r"|none) evidence=" + HEX + r" inputs=(" + HEX + r"|none))$")


class SummaryRejected(Exception):
    code = "OAH_SUMMARY_FIELD_REJECTED"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


@dataclasses.dataclass(frozen=True)
class Entry:
    gate_id: str
    status: str
    binding: str
    title: str
    check: str | None
    expect: str | None
    evidence: str
    inputs: str | None


def _digest(name: str, value: str | None, *, optional: bool) -> str:
    if value is None and optional:
        return "none"
    if not isinstance(value, str) or not HEX_RE.fullmatch(value):
        raise SummaryRejected(f"field {name} is not a sha256 digest")
    return value


def _line(entry: Entry) -> str:
    if not isinstance(entry.gate_id, str) or not ID_RE.fullmatch(entry.gate_id):
        raise SummaryRejected("a gate id is not of the form G<n>; rename the gates in the ledger to G1, G2, ... "
                              "(the summary exports only validated ids)")
    if entry.status not in STATUSES:
        raise SummaryRejected(f"gate {entry.gate_id} has a status outside {', '.join(STATUSES)}")
    if entry.binding not in BINDINGS:
        raise SummaryRejected(f"gate {entry.gate_id} has a binding outside {', '.join(BINDINGS)}")
    return (f"{entry.gate_id} {entry.status} binding={entry.binding} "
            f"title={_digest('title', entry.title, optional=False)} "
            f"check={_digest('check', entry.check, optional=True)} "
            f"expect={_digest('expect', entry.expect, optional=True)} "
            f"evidence={_digest('evidence', entry.evidence, optional=False)} "
            f"inputs={_digest('inputs', entry.inputs, optional=True)}")


def render(ledgers: Sequence[tuple[str, Sequence[Entry]]], *, verified: str) -> str:
    """The summary text for (ledger digest, entries) pairs. Raises SummaryRejected on any other field."""
    if verified not in VERIFIED:
        raise SummaryRejected("verified must be unlazy or structural")
    lines = [HEADER, f"verified: {verified}"]
    for ledger_digest, entries in ledgers:
        lines.append(f"ledger: {_digest('ledger', ledger_digest, optional=False)}")
        lines.extend(_line(entry) for entry in entries)
    for line in lines:
        if not LINE_RE.match(line):
            raise SummaryRejected("a summary line does not match the allowlist grammar")
    return "\n".join(lines) + "\n"
