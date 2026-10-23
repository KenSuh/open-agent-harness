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
        try:
            self.code = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "provider"
            super().__init__(f"OAH_RERANK_PROVIDER_ERROR: {self.code}")
        finally:
            del self, code


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
            elif issubclass(type(current), _INTERRUPTIONS):
                return type(current)
            elif issubclass(type(current), BaseExceptionGroup):
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
    exception_type = clean = fallback = None
    try:
        exception_type = _interruption_type(error)
        if exception_type is None:
            return ProviderError("internal")
        fallback = _interruption_fallback(exception_type)
        try:
            # Bypass subclass constructors and copy no arguments, notes or attributes.
            clean = BaseException.__new__(exception_type)
            BaseException.__init__(clean)
        except BaseException:
            clean = fallback
        return clean
    finally:
        del error, exception_type, clean, fallback


def _classify_failure(error: BaseException) -> str | BaseException:
    """Return only a validated code or a fresh interruption, never provider state or user attributes."""
    failure = "internal"
    attributes = name = code = None
    try:
        try:
            if _interruption_type(error) is not None:
                failure = _sanitize_interruption(error)
            elif issubclass(type(error), BaseExceptionGroup):
                pass  # Groups never have provider codes, regardless of their other bases.
            elif issubclass(type(error), ProviderError):
                attributes = BaseException.__dict__["__dict__"].__get__(error)
                # Bypass attribute/dict hooks and equality hooks on non-string dictionary keys.
                for name, code in dict.items(attributes):
                    if type(name) is str and name == "code":
                        if type(code) is str and _CODE_RE.fullmatch(code):
                            failure = code
                        break
        except BaseException as exc:
            failure = (_sanitize_interruption(exc) if _interruption_type(exc) is not None
                       else "internal")
        try:
            traceback.clear_frames(BaseException.__traceback__.__get__(error))
        except BaseException as exc:
            if not issubclass(type(failure), _INTERRUPTIONS):
                failure = (_sanitize_interruption(exc) if _interruption_type(exc) is not None
                           else "internal")
        return failure
    finally:
        del error, failure, attributes, name, code


def _raise_if_interrupted(error: object) -> None:
    """Give direct and grouped interruptions the shared classifier's fixed precedence."""
    failure = None
    try:
        if issubclass(type(error), BaseException):
            failure = _classify_failure(error)
            if type(failure) is not str:
                raise BaseException.with_traceback(failure, None) from None
    finally:
        del error, failure


class Provider(Protocol):
    """A re-ranker backend. It returns scores or advice; oah.rerank builds every order itself."""

    name: str

    def key_name(self, config: Mapping) -> str: ...

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
              config: Mapping) -> Sequence[float]: ...

    def advise(self, axes: Mapping[str, str], task: str, *, key: Secret,
               config: Mapping) -> tuple[str, float | None]: ...
