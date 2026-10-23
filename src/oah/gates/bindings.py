# 게이트 바인딩 저장소 — 게이트마다 정의·제목·증거·입력 다이제스트를 런타임 상태에 원자적으로 기록한다.
"""Bindings between gate evidence and the inputs it was checked against.

`<state_dir>/gates/<task>/bindings.json` holds, per ledger and gate id:
`{kind: run|attest|carry, definition, title, evidence, inputs, prefixes, recorded_at, carry?}`.
Every value except `kind`, `prefixes` and `recorded_at` is a sha256 hex digest, so no path, command
or evidence text is stored here. Writes take the state lock and replace the file atomically.
"""
from __future__ import annotations

import pathlib

from oah.atomic import StateCorrupt, read_json, write_json_atomic
from oah.lockfile import StateLock

SCHEMA_VERSION = 1
KINDS = ("run", "attest", "carry")


class BindingStore:
    def __init__(self, state_dir: pathlib.Path, task: str, *, lock_timeout: float = 10.0) -> None:
        self.state_dir = pathlib.Path(state_dir)
        self.task = task
        self.path = self.state_dir / "gates" / task / "bindings.json"
        self.lock_timeout = lock_timeout

    def load(self) -> dict[str, dict[str, dict]]:
        data = read_json(self.path, {"version": SCHEMA_VERSION, "ledgers": {}})
        if not (isinstance(data, dict) and data.get("version") == SCHEMA_VERSION
                and isinstance(data.get("ledgers"), dict)
                and all(isinstance(gates, dict) for gates in data["ledgers"].values())):
            raise StateCorrupt(f"{StateCorrupt.code}: {self.path} is not a version-{SCHEMA_VERSION} binding file; "
                               "remove it and run oah gates check again")
        return data["ledgers"]

    def replace(self, ledger_key: str, entries: dict[str, dict]) -> None:
        """Replace one ledger's bindings under the state lock."""
        with StateLock(self.state_dir / ".lock", timeout=self.lock_timeout):
            ledgers = self.load()
            ledgers[ledger_key] = entries
            write_json_atomic(self.path, {"version": SCHEMA_VERSION, "ledgers": ledgers})
