# 게이트 핵심 — 원장 파서와 상태 판정의 공개 API(parse_ledger, ledger_status)를 내보낸다.
"""Gate ledgers: the public API other lanes use.

- `parse_ledger(path) -> Ledger`: gates (id, title, CHECK, EXPECT, CWD, EVIDENCE, box state, declared
  OAH-INPUTS) and the `ABANDON:` lines. Pure: it only reads the file.
- `ledger_status(path, *, unlazy=..., node=...) -> LedgerStatus`: met, unmet, handoff or invalid,
  from unlazy's `--status` when given, else from the built-in structural parser.
"""
from oah.gates.ledger import Gate, Ledger, parse_ledger, parse_text
from oah.gates.status import LedgerStatus, ledger_status, structural_status

__all__ = ["Gate", "Ledger", "LedgerStatus", "ledger_status", "parse_ledger", "parse_text", "structural_status"]
