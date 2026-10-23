# 게이트 결속 층 — 게이트의 CHECK·EXPECT 가 경로를 고정(pin)하거나 실행(exec)하는지 판정한다. 원장 읽기는 P5 의 oah.gates.parse_ledger 가 들어온 뒤 연결한다.
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Iterable

from oah.impact import lexical
from oah.impact.query import Row

UNAVAILABLE = "unavailable (this oah version does not read gate ledgers yet)"


@dataclass(frozen=True)
class GateRef:
    """One gate as the ledger parser reports it: `oah.gates.parse_ledger(path)` gates carry check and expect."""

    ledger: str  # repository-relative POSIX path of the ledger
    gate_id: str  # G1, G2, ...
    check: str
    expect: str

    @property
    def label(self) -> str:
        return f"{self.ledger}#{self.gate_id}"


def binding(gate: GateRef, needles: dict[str, str]) -> str | None:
    """pin when CHECK hashes or greps the path or EXPECT names it, exec when CHECK runs it, link when CHECK
    only names it, else None. Stems are too loose to bind a gate."""
    found = []
    for needle, via in needles.items():
        if via == "stem":
            continue
        if lexical.find_needles(gate.expect, {needle}):
            found.append("pin")
        if lexical.find_needles(gate.check, {needle}):
            found.append(lexical.classify_command(gate.check, needle) or "link")
    order = ("pin", "exec", "link")
    return min(found, key=order.index) if found else None


def rows(gates: Iterable[GateRef], target: str, *, min_needle: int) -> list[Row]:
    needles = lexical.needles_for(target, min_needle)
    out = []
    for gate in gates:
        bound = binding(gate, needles)
        if bound:
            out.append(Row(bound, "gates", "gate", gate.label, None, target, f"binding={bound}", 0))
    return out


def discover(root: pathlib.Path) -> tuple[list[GateRef], str]:
    """The gates of this repository and a status line.

    Lane P5 lands `oah.gates.parse_ledger`; until this layer is wired to it, no ledger is read.
    """
    return [], UNAVAILABLE
