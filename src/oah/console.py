# 콘솔 출력 안전화 — stdout/stderr 의 인코딩은 그대로 두고, 표현할 수 없는 문자만 이스케이프한다.
from __future__ import annotations

import io
import sys
from typing import TextIO

ERRORS = "backslashreplace"


def make_safe(stream: TextIO) -> TextIO:
    """Return a stream that keeps its encoding but never raises UnicodeEncodeError."""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors=ERRORS)
        return stream
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        return io.TextIOWrapper(buffer, encoding=encoding, errors=ERRORS, line_buffering=True)
    return stream


def configure_stdio() -> None:
    """Make sys.stdout and sys.stderr safe before anything is printed."""
    if sys.stdout is not None:
        sys.stdout = make_safe(sys.stdout)
    if sys.stderr is not None:
        sys.stderr = make_safe(sys.stderr)
