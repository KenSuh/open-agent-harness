# 명령 입력 파일 — 일반 파일 의미와 사용법 오류를 유지하고 실제 입출력 실패만 공통 소유자에게 보낸다.
from __future__ import annotations

import pathlib
from typing import Callable

from oah.atomic import RETRY_SECONDS, _retry_permission
from oah.discovery import records


def location(path: str | pathlib.Path) -> pathlib.Path:
    source = pathlib.Path(path)
    try:
        return _retry_permission(source.resolve, RETRY_SECONDS)
    except OSError as exc:
        records.raise_io(source, exc)
        raise


def read_bytes(path: str | pathlib.Path, *, limit: int | None = None) -> bytes:
    source = pathlib.Path(path)

    def read() -> bytes:
        with open(source, "rb") as handle:
            return handle.read(-1 if limit is None else limit + 1)

    try:
        return _retry_permission(read, RETRY_SECONDS)
    except OSError as exc:
        records.raise_io(source, exc)
        raise


def source_text(path: pathlib.Path, reader: Callable[[pathlib.Path], str]) -> str:
    from oah.intent import SourceError

    try:
        return reader(path)
    except (OSError, SourceError) as exc:
        cause = exc if isinstance(exc, OSError) else exc.__cause__
        if isinstance(cause, OSError):
            records.raise_io(path, cause)
        raise


def json_value(raw: bytes, name: str) -> object:
    from oah.design.sources.state import StateProblem, loads

    try:
        value = loads(raw, name)
    except StateProblem as exc:
        raise ValueError(exc.detail) from None
    if not records._json_value(value):
        raise ValueError("the input is not bounded JSON")
    return value
