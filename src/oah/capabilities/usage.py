# 사용량 보존 — 실패한 스트림에서도 관찰한 수치만 닫힌 요약으로 남기며 없는 합계는 알 수 없음으로 둔다.
from __future__ import annotations

import math
from typing import Literal, Mapping, NotRequired, TypeAlias, TypedDict

from oah.capabilities import digest

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "cost_usd")
ALIASES = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
           "cache_read_input_tokens": "cacheReadInputTokens", "cache_creation_input_tokens": "cacheCreationInputTokens",
           "cost_usd": "costUSD"}
Values = dict[str, int | float | None]
Json: TypeAlias = str | int | float | bool | None | list["Json"] | dict[str, "Json"]


class UsageSummary(TypedDict):
    version: int
    totals: Values
    observed: Values
    reported_result: bool
    observed_messages: int
    omitted_messages: int
    availability: Literal["available", "partial", "unknown"]
    digest: NotRequired[str]


def valid_summary(raw: Json) -> bool:
    if not isinstance(raw, dict) or set(raw) != set(UsageSummary.__annotations__) or type(raw["version"]) is not int \
            or raw["version"] != 1 or type(raw["reported_result"]) is not bool \
            or raw["availability"] not in ("available", "partial", "unknown") \
            or type(raw["observed_messages"]) is not int or not 0 <= raw["observed_messages"] <= 128 \
            or type(raw["omitted_messages"]) is not int or not 0 <= raw["omitted_messages"] <= 10**9:
        return False
    for key in ("totals", "observed"):
        row = raw[key]
        if not isinstance(row, dict) or set(row) != set(FIELDS) or values(row) != row:
            return False
    return raw["digest"] == digest({key: value for key, value in raw.items() if key != "digest"})


def values(raw: object) -> Values:
    source = raw if isinstance(raw, Mapping) else {}
    found: Values = {}
    for key in FIELDS:
        number = source.get(key, source.get(ALIASES[key]))
        limit = 10**12 if key == "cost_usd" else 10**15
        found[key] = (number if type(number) is int and 0 <= number <= limit else
                      number if key == "cost_usd" and type(number) is float and math.isfinite(number)
                      and 0 <= number <= 10**12 else None)
    return found


def combined(rows: list[Values]) -> Values:
    result = values(None)
    for row in rows:
        for key, value in row.items():
            if value is not None:
                previous = result[key]
                result[key] = value if previous is None else previous + value
    return result


class AvailableUsage:
    def __init__(self) -> None:
        self.messages: dict[str, Values] = {}
        self.total: Values = values(None)
        self.omitted = 0
        self.result = False

    def event(self, event: Mapping[str, object]) -> None:
        message = event.get("message")
        if event.get("type") == "assistant" and isinstance(message, Mapping):
            row = values(message.get("usage"))
            if not any(value is not None for value in row.values()):
                return
            identity = digest({"message": message.get("id"), "session": event.get("session_id")}) \
                if message.get("id") is not None else digest({"ordinal": len(self.messages) + self.omitted})
            if identity in self.messages or len(self.messages) < 128:
                previous = self.messages.get(identity, values(None))
                self.messages[identity] = {key: value if value is not None else previous[key]
                                           for key, value in row.items()}
            else:
                self.omitted += 1
        elif event.get("type") == "result":
            models = event.get("modelUsage")
            if isinstance(models, Mapping) and models:
                rows = [values(value) for value in list(models.values())[:128]]
                self.total = combined(rows)
                for key in FIELDS:
                    if any(row[key] is None for row in rows) or len(models) > 128:
                        self.total[key] = None
            else:
                self.total = values(event.get("usage"))
            cost = values({"cost_usd": event.get("total_cost_usd")})["cost_usd"]
            if cost is not None:
                self.total["cost_usd"] = cost
            self.result = True

    def record(self) -> UsageSummary:
        available: Literal["available", "partial", "unknown"] = (
                     "available" if self.result and all(value is not None for value in self.total.values()) else
                     "partial" if self.messages or any(value is not None for value in self.total.values()) else "unknown")
        record: UsageSummary = {"version": 1, "totals": dict(self.total),
                                     "observed": combined(list(self.messages.values())),
                                     "reported_result": self.result, "observed_messages": len(self.messages),
                                     "omitted_messages": self.omitted,
                                     "availability": available}
        record["digest"] = digest(record)
        return record
