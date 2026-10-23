# 재순위 공용 타입 — 후보, 제공자 오류, 제공자 계약을 정의해 제공자 모듈이 패키지 본체를 순환 참조하지 않게 한다.
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

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


class Provider(Protocol):
    """A re-ranker backend. It returns scores or advice; oah.rerank builds every order itself."""

    name: str

    def key_name(self, config: Mapping) -> str: ...

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: str,
              config: Mapping) -> Sequence[float]: ...

    def advise(self, axes: Mapping[str, str], task: str, *, key: str,
               config: Mapping) -> tuple[str, float | None]: ...
