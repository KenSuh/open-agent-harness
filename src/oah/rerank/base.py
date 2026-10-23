# 재순위 공용 타입 — 후보, 제공자 오류, 제공자 계약을 정의해 제공자 모듈이 패키지 본체를 순환 참조하지 않게 한다.
from __future__ import annotations

import re
import traceback
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from oah.secrets import Secret

TIERS = ("low", "medium", "high", "max")
_CODE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_INTERRUPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


@dataclass(frozen=True)
class Candidate:
    """One row to order. Only `name` and `description` are ever sent to a provider."""

    name: str
    description: str = ""


class ProviderError(Exception):
    """A provider failure, reported as `unused(error:<code>)`. It carries a code only, never text."""

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "provider"
        super().__init__(f"OAH_RERANK_PROVIDER_ERROR: {self.code}")


def _interruption_type(error: BaseException) -> type[BaseException] | None:
    """Find the first interruption in depth-first order without rebuilding or formatting groups."""
    pending = [iter((error,))]
    seen = set()
    current = None
    try:
        while pending:
            current = next(pending[-1], None)
            if current is None:
                pending.pop()
            elif id(current) in seen:
                continue
            elif isinstance(current, _INTERRUPTIONS):
                return type(current)
            elif isinstance(current, BaseExceptionGroup):
                seen.add(id(current))
                pending.append(iter(BaseExceptionGroup.exceptions.__get__(current)))
        return None
    finally:
        del error, pending, seen, current


def _interruption_fallback(exception_type: type[BaseException]) -> BaseException:
    """Allocate only a built-in base, even when a subclass needs an incompatible allocator."""
    for base in _INTERRUPTIONS:
        if issubclass(exception_type, base):
            return base()
    return ProviderError("internal")


def _sanitize_interruption(error: BaseException) -> BaseException:
    """Propagate one fresh interruption, never a group or an original exception's private state."""
    exception_type = clean = None
    fallback = ProviderError("internal")
    try:
        exception_type = _interruption_type(error)
        if exception_type is None:
            if isinstance(error, (Exception, BaseExceptionGroup)):
                return fallback
            exception_type = type(error)
        fallback = _interruption_fallback(exception_type)
        try:
            # Bypass subclass constructors and copy no arguments, notes or attributes.
            clean = BaseException.__new__(exception_type)
            BaseException.__init__(clean)
        except BaseException:
            clean = fallback
        try:
            traceback.clear_frames(BaseException.__traceback__.__get__(error))
        except BaseException:
            pass  # Cleanup failure must not replace the selected interruption.
        return clean
    finally:
        del error, exception_type, clean, fallback


class Provider(Protocol):
    """A re-ranker backend. It returns scores or advice; oah.rerank builds every order itself."""

    name: str

    def key_name(self, config: Mapping) -> str: ...

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
              config: Mapping) -> Sequence[float]: ...

    def advise(self, axes: Mapping[str, str], task: str, *, key: Secret,
               config: Mapping) -> tuple[str, float | None]: ...
