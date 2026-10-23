# 리스 트랜잭션 저널 — 상태와 이벤트를 먼저 기록하고 잠금 안에서 재실행하여 중단된 게시를 한 번만 완료한다.
"""One durable outbox transaction, replayed by the next writer under the state lock."""
from __future__ import annotations

import pathlib
import secrets
from typing import Callable, Mapping

from oah.atomic import StateCorrupt, append_jsonl, read_json, read_jsonl, unlink_durable, write_json_atomic


class PendingTransaction(Exception):
    code = "OAH_STATE_PENDING"


class Journal:
    def __init__(self, state_dir: pathlib.Path) -> None:
        self.path = state_dir / "transaction.json"
        self.state_path = state_dir / "leases.json"
        self.events_path = state_dir / "events.jsonl"

    def _load(self) -> dict | None:
        missing = object()
        data = read_json(self.path, missing)
        if data is missing:
            return None
        if not (isinstance(data, dict) and data.get("version") == 1
                and isinstance(data.get("transaction_id"), str) and data["transaction_id"]
                and isinstance(data.get("state"), dict) and isinstance(data.get("event"), dict)
                and data["event"].get("transaction_id") == data["transaction_id"]):
            raise StateCorrupt(f"{StateCorrupt.code}: {self.path} is a malformed lease transaction")
        return data

    def check_pending(self) -> None:
        if self._load() is not None:
            raise PendingTransaction(f"{PendingTransaction.code}: {self.path} needs recovery. "
                                     "The next mutating oah command replays it under the state lock.")

    def recover(self, validate: Callable[[object], object]) -> None:
        """The caller holds the lock, including throughout validation and publication."""
        transaction = self._load()
        if transaction is not None:
            missing = object()
            live = read_json(self.state_path, missing)
            if live is not missing:
                validate(live)
            validate(transaction["state"])
            # A prior replace may be visible even though its directory sync never completed.
            write_json_atomic(self.path, transaction)
            self._publish(transaction)
        else:
            read_jsonl(self.events_path)

    def commit(self, state: dict, event: Mapping) -> None:
        read_jsonl(self.events_path)
        transaction_id = secrets.token_hex(16)
        transaction = {"version": 1, "transaction_id": transaction_id, "state": state,
                       "event": dict(event, transaction_id=transaction_id)}
        write_json_atomic(self.path, transaction)
        self._publish(transaction)

    def _publish(self, transaction: dict) -> None:
        event = transaction["event"]
        records = read_jsonl(self.events_path)
        matches = [item for item in records if item.get("transaction_id") == transaction["transaction_id"]]
        if matches and (len(matches) != 1 or matches[0] != event):
            raise StateCorrupt(f"{StateCorrupt.code}: {self.events_path} conflicts with {self.path}")
        write_json_atomic(self.state_path, transaction["state"])
        append_jsonl(self.events_path, event)
        unlink_durable(self.path)
