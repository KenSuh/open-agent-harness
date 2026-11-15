# 준비 기록 스키마 — 파서와 검증기가 순환 의존 없이 같은 필드와 값 형식을 사용한다.
from __future__ import annotations

from typing import Final, TypeAlias

Json: TypeAlias = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
Record: TypeAlias = dict[str, Json]
FACT_KEYS: Final = frozenset({"operation", "task", "lease", "engine", "stage", "route", "needs", "selections",
                            "payloads", "not_delivered", "policy", "inventory", "relay_results", "target",
                            "request_budget", "references", "profile", "selectors", "access", "context"})
REFERENCE_KEYS: Final = frozenset({"needs", "skill_needs", "parent_inventory", "relay_results", "tier", "route"})
FINAL_KEYS: Final = frozenset({"bundle_digest", "capability_plan_digest", "render_digest"})


def _hash(value: Json) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71 and all(
        char in "0123456789abcdef" for char in value[7:])


def _nonnegative(value: Json) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
