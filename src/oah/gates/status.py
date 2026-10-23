# 원장 상태 판정 — unlazy 의 --status 판정과 내장 구조 파서를 합쳐 met·unmet·handoff·invalid 를 돌려준다(아무것도 쓰거나 실행하지 않는다).
from __future__ import annotations

import dataclasses
import pathlib
from typing import Mapping, Sequence

from oah import unlazy as unlazy_mod
from oah.gates.ledger import Ledger, gate_state, parse_ledger

STRUCTURAL_LABEL = "structural only"


@dataclasses.dataclass(frozen=True)
class LedgerStatus:
    verdict: str  # met | unmet | handoff | invalid
    source: str  # unlazy | structural
    states: tuple[tuple[str, str], ...]  # (gate id, state) in ledger order
    unmet: tuple[str, ...]
    abandoned: tuple[str, ...]
    errors: tuple[str, ...]
    detail: str = ""
    output: str = ""  # unlazy's own report, when unlazy gave the verdict

    @property
    def structural_only(self) -> bool:
        return self.source == "structural"


def structural_status(ledger: Ledger) -> LedgerStatus:
    """The verdict the built-in parser alone can give. It is labelled structural only."""
    if ledger.errors:
        return LedgerStatus("invalid", "structural", (), (), (), ledger.errors, "; ".join(ledger.errors[:3]))
    states = tuple((gate.id, gate_state(gate, ledger.abandoned)) for gate in ledger.gates)
    unmet = tuple(gate_id for gate_id, state in states if state not in ("met", "abandoned"))
    abandoned = tuple(gate_id for gate_id, state in states if state == "abandoned")
    verdict = "unmet" if unmet else "handoff" if abandoned else "met"
    return LedgerStatus(verdict, "structural", states, unmet, abandoned, ())


def ledger_status(path: pathlib.Path, *, unlazy: pathlib.Path | None = None, node: Sequence[str] | None = None,
                  env: Mapping[str, str] | None = None, root: pathlib.Path | None = None) -> LedgerStatus:
    """Status of one ledger, without executing a CHECK or writing anything.

    With `unlazy` (its folder) and `node`, the verdict comes from `gate-check.mjs --status`. If unlazy
    and the built-in parser disagree, the result fails closed as unmet. Without unlazy the result is
    the structural verdict (`source == "structural"`).
    """
    ledger = parse_ledger(path)
    structural = structural_status(ledger)
    if unlazy is None or node is None or structural.verdict == "invalid":
        return structural
    result = unlazy_mod.gate_check(unlazy, pathlib.Path(path), node=node, mode="status", env=env,
                                   root=root or pathlib.Path(path).parent)
    output = "\n".join(part.rstrip("\n") for part in (result.stdout, result.stderr) if part.strip())
    if result.verdict == "error":
        message = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}").splitlines()
        return LedgerStatus("invalid", "unlazy", structural.states, structural.unmet, structural.abandoned,
                            tuple(message[:3]), "unlazy gate-check failed: " + " ".join(message[:3]), output)
    if result.verdict != structural.verdict:
        return LedgerStatus("unmet", "unlazy", structural.states, structural.unmet or tuple(
            gate_id for gate_id, _ in structural.states), structural.abandoned, (),
            f"unlazy says {result.verdict} but the built-in parser says {structural.verdict}; "
            "they disagree, so oah treats the ledger as unmet. Update unlazy or oah.", output)
    return dataclasses.replace(structural, source="unlazy", output=output)
