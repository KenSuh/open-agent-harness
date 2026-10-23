# 재순위 공용 타입 — 후보, 제공자 오류, 제공자 계약을 정의해 제공자 모듈이 패키지 본체를 순환 참조하지 않게 한다.
from __future__ import annotations

import re
import traceback
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from oah.secrets import Secret

TIERS = ("low", "medium", "high", "max")
_CODE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


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


def _sanitize_interruption(error: BaseException) -> BaseException:
    """Preserve interruption types and group structure without retaining private exception data."""
    try:
        if isinstance(error, BaseExceptionGroup):
            return BaseExceptionGroup("OAH_RERANK_INTERRUPTED: retry the request when ready",
                                      [_sanitize_interruption(child) for child in error.exceptions])
        if isinstance(error, Exception):
            return ProviderError("internal")
        # Bypass subclass constructors: interruptions may require arguments containing private data.
        clean = BaseException.__new__(type(error))
        BaseException.__init__(clean)
        return clean
    finally:
        traceback.clear_frames(error.__traceback__)
        del error


class Provider(Protocol):
    """A re-ranker backend. It returns scores or advice; oah.rerank builds every order itself."""

    name: str

    def key_name(self, config: Mapping) -> str: ...

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
              config: Mapping) -> Sequence[float]: ...

    def advise(self, axes: Mapping[str, str], task: str, *, key: Secret,
               config: Mapping) -> tuple[str, float | None]: ...
