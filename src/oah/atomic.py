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
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _retry_permission(lambda: os.replace(temp, path), retry_seconds)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
    _fsync_folder(folder)


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
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateCorrupt(f"{StateCorrupt.code}: {path} is not valid UTF-8 JSON ({exc}). "
                           "oah never resets it; repair or remove the file by hand.") from exc


def append_jsonl(path: pathlib.Path, record: Mapping) -> None:
    """Append one JSON object as one line. Call it while holding the state lock."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
