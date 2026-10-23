# 재순위 공용 타입 — 후보, 제공자 오류, 제공자 계약을 정의해 제공자 모듈이 패키지 본체를 순환 참조하지 않게 한다.
from __future__ import annotations

import re
import traceback
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from oah.secrets import Secret

TIERS = ("low", "medium", "high", "max")
_CODE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_GROUP_DEPTH_LIMIT = 64
_GROUP_COUNT_LIMIT = 1024
_INTERRUPTED = "OAH_RERANK_INTERRUPTED: retry the request when ready"


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


def _interruption_fallback(error: BaseException) -> BaseException:
    """Find control flow even beyond the rebuild bounds, without copying or formatting the group."""
    pending = [iter((error,))]
    seen = set()
    current = clean = None
    try:
        while pending:
            current = next(pending[-1], None)
            if current is None:
                pending.pop()
            elif id(current) in seen:
                continue
            elif isinstance(current, (KeyboardInterrupt, SystemExit)):
                clean = BaseException.__new__(type(current))
                BaseException.__init__(clean)
                return clean
            elif isinstance(current, BaseExceptionGroup):
                seen.add(id(current))
                pending.append(iter(BaseExceptionGroup.exceptions.__get__(current)))
        return ProviderError("internal")
    finally:
        del error, pending, seen, current, clean


def _rebuild_interruption(error: BaseException) -> BaseException:
    """Rebuild in postorder with bounded depth and node count, using only base-class descriptors."""
    pending = [(error, 0, False)]
    rebuilt = []
    current = children = clean = None
    count = 1
    try:
        while pending:
            current, depth, expanded = pending.pop()
            if expanded:
                size = len(BaseExceptionGroup.exceptions.__get__(current))
                children = rebuilt[-size:]
                del rebuilt[-size:]
                rebuilt.append(BaseExceptionGroup(_INTERRUPTED, children))
                continue
            if depth > _GROUP_DEPTH_LIMIT:
                raise ProviderError("internal")
            traceback.clear_frames(BaseException.__traceback__.__get__(current))
            if isinstance(current, BaseExceptionGroup):
                children = BaseExceptionGroup.exceptions.__get__(current)
                count += len(children)
                if count > _GROUP_COUNT_LIMIT:
                    raise ProviderError("internal")
                pending.append((current, depth, True))
                for index in range(len(children) - 1, -1, -1):
                    pending.append((children[index], depth + 1, False))
            elif isinstance(current, Exception):
                rebuilt.append(ProviderError("internal"))
            else:
                # Bypass subclass constructors, which may require private arguments.
                clean = BaseException.__new__(type(current))
                BaseException.__init__(clean)
                rebuilt.append(clean)
        return rebuilt[0]
    finally:
        del error, pending, rebuilt, current, children, clean


def _sanitize_interruption(error: BaseException) -> BaseException:
    """Return only fresh exceptions, including when rebuilding or clearing frames fails."""
    fallback = ProviderError("internal")
    try:
        try:
            fallback = _interruption_fallback(error)
            return _rebuild_interruption(error)
        except BaseException:
            pass  # Discard the failed sanitizer and its original exception context entirely.
        return fallback
    finally:
        del error, fallback


class Provider(Protocol):
    """A re-ranker backend. It returns scores or advice; oah.rerank builds every order itself."""

    name: str

    def key_name(self, config: Mapping) -> str: ...

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
              config: Mapping) -> Sequence[float]: ...

    def advise(self, axes: Mapping[str, str], task: str, *, key: Secret,
               config: Mapping) -> tuple[str, float | None]: ...
