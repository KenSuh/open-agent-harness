# 원자적 상태 쓰기 — 같은 폴더의 임시 파일에 쓰고 fsync 뒤 os.replace 로 바꾸며, Windows 의 일시적 PermissionError 는 다시 시도한다.
"""Crash-safe state files.

Readers see either the old file or the new one, never a partial write. On Windows another process
holding the file open makes os.replace fail with PermissionError for a moment, so replace and read
retry briefly. A file that does not parse is reported, never reset.
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import time
from typing import Callable, Mapping, TypeVar

RETRY_SECONDS = 2.0
RETRY_SLEEP = 0.05

T = TypeVar("T")


class StateCorrupt(Exception):
    code = "OAH_STATE_CORRUPT"


class StorageError(OSError):
    code = "OAH_STATE_IO"

    def __init__(self, path: pathlib.Path, cause: OSError) -> None:
        self.path = pathlib.Path(path)
        super().__init__(f"{self.code}: cannot access {self.path}: {cause}. "
                         "Check permissions and free disk space, then retry.")


def _retry_permission(operation: Callable[[], T], retry_seconds: float) -> T:
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            return operation()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(RETRY_SLEEP)


def _fsync_folder(folder: pathlib.Path) -> None:
    if os.name == "nt":
        return  # Windows cannot open a folder for fsync; NTFS journals the rename.
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_text_atomic(path: pathlib.Path, text: str, *, retry_seconds: float = RETRY_SECONDS) -> None:
    path = pathlib.Path(path)
    folder = path.parent
    temp = folder / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _retry_permission(lambda: os.replace(temp, path), retry_seconds)
        _fsync_folder(folder)
    except BaseException as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise StorageError(path, exc) from exc
        raise


def write_json_atomic(path: pathlib.Path, data: object, *, retry_seconds: float = RETRY_SECONDS) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic(path, text, retry_seconds=retry_seconds)


def read_json(path: pathlib.Path, default: object) -> object:
    """The parsed file, or default when it does not exist. Raises StateCorrupt when it does not parse."""
    path = pathlib.Path(path)
    try:
        raw = _retry_permission(path.read_bytes, RETRY_SECONDS)
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise StorageError(path, exc) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateCorrupt(f"{StateCorrupt.code}: {path} is not valid UTF-8 JSON ({exc}). "
                           "oah never resets it; repair or remove the file by hand.") from exc


def read_jsonl(path: pathlib.Path) -> list[dict]:
    """Validate every line, including its terminating newline; never repair a torn append."""
    path = pathlib.Path(path)
    try:
        raw = _retry_permission(path.read_bytes, RETRY_SECONDS)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StorageError(path, exc) from exc
    try:
        if raw and not raw.endswith(b"\n"):
            raise ValueError("incomplete final event line (missing newline)")
        records = [json.loads(line.decode("utf-8")) for line in raw.split(b"\n")[:-1]]
        if any(not isinstance(record, dict) for record in records):
            raise ValueError("an event line is not a JSON object")
        return records
    except (UnicodeDecodeError, ValueError) as exc:
        raise StateCorrupt(f"{StateCorrupt.code}: {path} is not valid UTF-8 JSONL ({exc}). "
                           "Repair the event log by hand; oah preserves its bytes.") from exc


def append_jsonl(path: pathlib.Path, record: Mapping) -> None:
    """Validate and append under the state lock; replayed transaction ids are idempotent."""
    path = pathlib.Path(path)
    records = read_jsonl(path)
    transaction_id = record.get("transaction_id")
    matches = [item for item in records if transaction_id and item.get("transaction_id") == transaction_id]
    if matches and (len(matches) != 1 or matches[0] != dict(record)):
        raise StateCorrupt(f"{StateCorrupt.code}: {path} has conflicting events for transaction {transaction_id}")
    line = (json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-sync an already published event before clearing its journal, even if a prior fsync failed.
        with open(path, "ab") as handle:
            if not matches:
                handle.write(line)
                handle.flush()
            os.fsync(handle.fileno())
        _fsync_folder(path.parent)
    except OSError as exc:
        raise StorageError(path, exc) from exc


def unlink_durable(path: pathlib.Path) -> None:
    """Remove a completed journal and persist its directory entry."""
    try:
        path.unlink()
        _fsync_folder(path.parent)
    except OSError as exc:
        raise StorageError(path, exc) from exc
