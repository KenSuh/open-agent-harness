# E 기록 읽기 — 파일 종류와 읽기·파싱 실패를 한 번 분류하고 실제 저장 장치 오류를 보존한다.
from __future__ import annotations

import errno
import math
import pathlib
import stat
from enum import StrEnum
from typing import Callable, Final, TypeAlias, TypeGuard, TypeVar

from oah.atomic import RETRY_SECONDS, StateCorrupt, StorageError, _retry_permission
from oah.design.sources.state import MAX_DEPTH, MAX_INT_DIGITS, StateProblem, loads

Json: TypeAlias = None | bool | int | float | str | list["Json"] | dict[str, "Json"]
LIMIT: Final = 2 << 20
T = TypeVar("T")


class Issue(StrEnum):
    MISSING = "missing"
    NOT_REGULAR = "not-regular"
    DENIED = "permission-denied"
    CONTENT = "invalid-content"
    IO = "io"


class Category(StrEnum):
    STALE = "stale"
    IO = "io"


class RecordRefusal(Exception):
    def __init__(self, path: pathlib.Path, issue: Issue, detail: str) -> None:
        self.path, self.issue, self.detail = path, issue, detail
        self.category = Category.IO if issue == Issue.IO else Category.STALE
        super().__init__(f"the recorded {path.name} {detail}")


InvalidRecord = RecordRefusal


def io_failure(path: pathlib.Path, exc: OSError) -> RecordRefusal:
    cause = exc.__cause__ if isinstance(exc, StorageError) and isinstance(exc.__cause__, OSError) else exc
    if isinstance(cause, FileNotFoundError) or cause.errno == errno.ENOENT:
        return InvalidRecord(path, Issue.MISSING, "is missing")
    if isinstance(cause, (NotADirectoryError, IsADirectoryError)) or cause.errno in (errno.ENOTDIR, errno.EISDIR):
        return InvalidRecord(path, Issue.NOT_REGULAR, "is not a regular file")
    if isinstance(cause, PermissionError) or cause.errno in (errno.EACCES, errno.EPERM):
        return InvalidRecord(path, Issue.DENIED, "cannot be read with the current permissions")
    return RecordRefusal(path, Issue.IO, f"cannot be read ({cause}); check permissions and free disk space, then retry")


def raise_io(path: pathlib.Path, exc: OSError) -> None:
    refusal = io_failure(path, exc)
    if refusal.category == Category.IO:
        raise refusal from exc


def _read(path: pathlib.Path, operation: Callable[[pathlib.Path], bytes], *, required: bool, limit: int | None,
          resolve: Callable[[], pathlib.Path] | None = None, parse: Callable[[bytes], T], regular: bool = True,
          deadline: float | None = None) -> T | None:
    def access() -> bytes:
        nonlocal path
        if regular:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise InvalidRecord(path, Issue.NOT_REGULAR, "is not a regular file")
            if limit is not None and info.st_size > limit:
                raise InvalidRecord(path, Issue.CONTENT, "exceeds its bounded record size")
        if resolve is not None:
            path = resolve()
        return operation(path)
    try:
        raw = _retry(access, deadline)
    except OSError as exc:
        failure = io_failure(path, exc)
        if failure.issue == Issue.MISSING and not required:
            return None
        raise failure from exc
    except StateCorrupt as exc:
        raise RecordRefusal(path, Issue.CONTENT, str(exc)) from None
    except StateProblem as exc:
        raise RecordRefusal(path, Issue.CONTENT, exc.detail) from None
    return _parse(path, raw, limit=limit, parse=parse)


def _parse(path: pathlib.Path, raw: bytes, *, limit: int | None, parse: Callable[[bytes], T]) -> T:
    try:
        if limit is not None and len(raw) > limit:
            raise RecordRefusal(path, Issue.CONTENT, "exceeds its bounded record size")
        return parse(raw)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError, RecursionError,
            StateCorrupt, StateProblem) as exc:
        raise RecordRefusal(path, Issue.CONTENT, f"has invalid content ({exc})") from None


def read_bytes(path: pathlib.Path, *, required: bool = True, limit: int | None = None,
               parse: Callable[[bytes], T] = bytes,
               resolve: Callable[[], pathlib.Path] | None = None) -> T | None:
    return _read(path, lambda source: source.read_bytes(), required=required, limit=limit, parse=parse, resolve=resolve)


def present(path: pathlib.Path) -> bool:
    return _read(path, lambda _source: b"", required=False, limit=None, parse=lambda _raw: True) is True


def _retry(operation: Callable[[], T], deadline: float | None) -> T:
    def attempt() -> T:
        try:
            return operation()
        except StorageError as exc:
            if isinstance(exc.__cause__, PermissionError):
                raise exc.__cause__ from None
            raise

    if deadline is None:
        return _retry_permission(attempt, RETRY_SECONDS)
    from oah.runs import _retry_until

    return _retry_until(deadline, RETRY_SECONDS, attempt)


def location(path: pathlib.Path, resolve: Callable[[], T], *, deadline: float | None = None) -> T:
    try:
        return _retry(resolve, deadline)
    except OSError as exc:
        raise io_failure(path, exc) from exc


def followed_bytes(path: pathlib.Path, *, limit: int, parse: Callable[[bytes], T]) -> T | None:
    def read(source):
        with open(source, "rb") as handle:
            return handle.read(limit + 1)

    return _read(path, read, required=True, limit=limit, parse=parse, regular=False)


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


def read_json(path: pathlib.Path, *, required: bool = True, limit: int | None = None,
              parse: Callable[[dict[str, Json]], T] = dict) -> T | None:
    return read_bytes(path, required=required, limit=limit, parse=lambda raw: parse(_object(json_bytes(raw, path), path)))


def captured_json(path: pathlib.Path, capture, *, parse: Callable[[dict[str, Json]], T] = dict) -> T | None:
    def raw(source):
        return capture(source)[0]

    def checked(data):
        return parse(_object(json_bytes(data, path), path))

    value = _read(path, raw, required=False, limit=None, parse=checked)
    if value is None:
        try:
            pending = _retry(lambda: raw(path), None)
        except OSError as exc:
            raise io_failure(path, exc) from exc
        if pending is not None:
            return _parse(path, pending, limit=None, parse=checked)
    return value


def read_rows(path: pathlib.Path) -> list[dict[str, Json]]:
    return read_bytes(path, required=False, parse=lambda raw: _rows(raw, path)) or []


def _rows(raw: bytes | None, path: pathlib.Path) -> list[dict[str, Json]]:
    if raw is None:
        return []
    if raw and not raw.endswith(b"\n"):
        raise InvalidRecord(path, Issue.CONTENT, "has an incomplete event line")
    rows = []
    for line in raw.split(b"\n")[:-1]:
        rows.append(_object(json_bytes(line, path), path))
    return rows


def run_json(run, name: str, *, required: bool = True,
             parse: Callable[[dict[str, Json]], T] = dict) -> T | None:
    from oah.runs import WorkerRun

    path = run.path / name
    limit = LIMIT if name == "skills.json" else None
    operation = lambda source: run._read_bytes(name) if isinstance(run, WorkerRun) else source.read_bytes()

    def checked(raw):
        data = _object(json_bytes(raw, path), path)
        if isinstance(run, WorkerRun) and name == "result.json" and (
                data.get("run_id") != run.id or data.get("task") != run.task or
                run._validated is not None and any(data.get(key) != run._validated.get(key) for key in (
                    "engine", "stage", "created_at", "cwd", "resolved_model", "launch_effort", "cli_version",
                    "override_stage"))):
            raise ValueError("the saved result identity changed")
        return parse(data)

    return _read(path, operation, required=required, limit=limit, resolve=lambda: run.file_path(name),
                 parse=checked, deadline=getattr(run, "deadline", None))


def run_rows(run, name: str) -> list[dict[str, Json]]:
    from oah.runs import WorkerRun

    path = run.path / name
    raw = _read(path, lambda source: run._read_bytes(name) if isinstance(run, WorkerRun) else source.read_bytes(),
                required=False, limit=None, resolve=lambda: run.file_path(name), parse=lambda raw: _rows(raw, path))
    return raw or []


def read_text(path: pathlib.Path, *, required: bool = True, limit: int | None = None) -> str | None:
    return read_bytes(path, required=required, limit=limit, parse=lambda raw: _text(raw, path))


def run_text(run) -> str:
    from oah.runs import WorkerRun

    path = run.path / "prompt.md"
    if isinstance(run, WorkerRun) and run._prompt is not None:
        raw = location(path, run.prompt_path.read_bytes)
        return _parse(path, raw, limit=None, parse=lambda raw: _text(raw, path))
    raw = _read(path, lambda source: run._read_bytes("prompt.md") if isinstance(run, WorkerRun) else source.read_bytes(),
                required=True, limit=None, resolve=lambda: run.file_path("prompt.md"), parse=lambda raw: _text(raw, path))
    assert raw is not None
    return raw


def _text(raw: bytes, path: pathlib.Path) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidRecord(path, Issue.CONTENT, "is not UTF-8") from None


def bind_worker(run, data) -> None:
    from oah.runs import _launch_invalid

    if data.get("run_id") != run.id or data.get("task") != run.task:
        raise _launch_invalid()
    prompt = run_text(run)
    run._validated = data
    run._prompt = prompt.encode("utf-8")
