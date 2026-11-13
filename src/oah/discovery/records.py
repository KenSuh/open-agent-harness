# E 기록 읽기 — 파일 종류와 읽기·파싱 실패를 한 번 분류하고 실제 저장 장치 오류를 보존한다.
from __future__ import annotations

import errno
import math
import pathlib
import stat
from contextlib import contextmanager
from enum import StrEnum
from typing import Callable, Final, Iterator, TypeAlias, TypeGuard, TypeVar

from oah.atomic import StorageError
from oah.design.sources.state import MAX_DEPTH, MAX_INT_DIGITS, StateProblem, loads

Json: TypeAlias = None | bool | int | float | str | list["Json"] | dict[str, "Json"]
LIMIT: Final = 2 << 20
T = TypeVar("T")


class Issue(StrEnum):
    MISSING = "missing"
    NOT_REGULAR = "not-regular"
    DENIED = "permission-denied"
    CONTENT = "invalid-content"


class InvalidRecord(ValueError):
    def __init__(self, path: pathlib.Path, issue: Issue, detail: str) -> None:
        self.path, self.issue, self.detail = path, issue, detail
        super().__init__(f"the recorded {path.name} {detail}")


@contextmanager
def content(path: pathlib.Path) -> Iterator[None]:
    try:
        yield
    except InvalidRecord:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise InvalidRecord(path, Issue.CONTENT, f"has invalid content ({type(exc).__name__})") from None


def io_failure(path: pathlib.Path, exc: OSError) -> InvalidRecord | StorageError:
    cause = exc.__cause__ if isinstance(exc, StorageError) and isinstance(exc.__cause__, OSError) else exc
    if isinstance(cause, FileNotFoundError) or cause.errno == errno.ENOENT:
        return InvalidRecord(path, Issue.MISSING, "is missing")
    if isinstance(cause, (NotADirectoryError, IsADirectoryError)) or cause.errno in (errno.ENOTDIR, errno.EISDIR):
        return InvalidRecord(path, Issue.NOT_REGULAR, "is not a regular file")
    if isinstance(cause, PermissionError) or cause.errno in (errno.EACCES, errno.EPERM):
        return InvalidRecord(path, Issue.DENIED, "cannot be read with the current permissions")
    return exc if isinstance(exc, StorageError) else StorageError(path, exc)


def _read(path: pathlib.Path, operation: Callable[[], T], *, required: bool, limit: int | None) -> T | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InvalidRecord(path, Issue.NOT_REGULAR, "is not a regular file")
        if limit is not None and info.st_size > limit:
            raise InvalidRecord(path, Issue.CONTENT, "exceeds its bounded record size")
        return operation()
    except OSError as exc:
        failure = io_failure(path, exc)
        if isinstance(failure, InvalidRecord) and failure.issue == Issue.MISSING and not required:
            return None
        if failure is exc:
            raise
        raise failure from exc


def read_bytes(path: pathlib.Path, *, required: bool = True, limit: int | None = None) -> bytes | None:
    return _read(path, path.read_bytes, required=required, limit=limit)


def json_bytes(raw: bytes, path: pathlib.Path) -> object:
    try:
        value = loads(raw, path.name)
    except StateProblem as exc:
        raise InvalidRecord(path, Issue.CONTENT, exc.detail) from None
    if not _json_value(value):
        raise InvalidRecord(path, Issue.CONTENT, "contains a non-JSON value")
    return value


def _json_value(value, depth: int = 1) -> TypeGuard[Json]:
    if depth > MAX_DEPTH:
        return False
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, int):
        return -(10 ** MAX_INT_DIGITS) < value < 10 ** MAX_INT_DIGITS
    if isinstance(value, float):
        return math.isfinite(value)
    return (isinstance(value, list) and all(_json_value(item, depth + 1) for item in value)
            or isinstance(value, dict) and all(isinstance(key, str) and _json_value(item, depth + 1)
                                               for key, item in value.items()))


def _json_object(value: object) -> TypeGuard[dict[str, Json]]:
    return isinstance(value, dict) and _json_value(value)


def _object(value: object, path: pathlib.Path) -> dict[str, Json]:
    if not _json_object(value):
        raise InvalidRecord(path, Issue.CONTENT, "is not a bounded JSON object")
    return value


def read_json(path: pathlib.Path, *, required: bool = True, limit: int | None = None) -> dict[str, Json] | None:
    raw = read_bytes(path, required=required, limit=limit)
    return _object(json_bytes(raw, path), path) if raw is not None else None


def read_rows(path: pathlib.Path) -> list[dict[str, Json]]:
    raw = read_bytes(path, required=False)
    if raw is None:
        return []
    if raw and not raw.endswith(b"\n"):
        raise InvalidRecord(path, Issue.CONTENT, "has an incomplete event line")
    rows = []
    for line in raw.split(b"\n")[:-1]:
        rows.append(_object(json_bytes(line, path), path))
    return rows


def run_json(run, name: str, *, required: bool = True) -> dict[str, Json] | None:
    from oah.runs import WorkerRun

    path = run.file_path(name)
    limit = LIMIT if name == "skills.json" else None
    if isinstance(run, WorkerRun):
        return _read(path, lambda: _object(run._read_json(name), path), required=required, limit=limit)
    return read_json(path, required=required, limit=limit)


def read_text(path: pathlib.Path, *, required: bool = True, limit: int | None = None) -> str | None:
    raw = read_bytes(path, required=required, limit=limit)
    if raw is None:
        return None
    return _text(raw, path)


def run_text(run) -> str:
    path = run.file_path("prompt.md")
    raw = _read(path, run.prompt_path.read_bytes, required=True, limit=None)
    assert raw is not None
    return _text(raw, path)


def _text(raw: bytes, path: pathlib.Path) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidRecord(path, Issue.CONTENT, "is not UTF-8") from None
