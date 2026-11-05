# 판정 설정 — 닫힌 설정과 유한 임계값·질문 수·본문·캐시 한도를 검증해 요청과 재검증이 같은 정책을 쓰게 한다.
from __future__ import annotations

import math
from typing import Mapping

from oah.config import ConfigError

DEFAULTS = {"enabled": False, "send_design_text": False, "yes": 0.7, "no": 0.3, "choice_min": 0.6,
            "level_min": 0.5, "cache_hours": 24.0, "max_questions": 32, "max_state_chars": 16000}


def settings(config: Mapping[str, object]) -> dict[str, bool | float | int]:
    raw = config.get("judgement", {})
    if not isinstance(raw, Mapping) or set(raw) - set(DEFAULTS):
        raise ConfigError("OAH_CONFIG_INVALID", "rerank.judgement must be a table with only supported settings")
    result = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if type(default) is bool:
            valid = type(value) is bool
        elif key in ("max_questions", "max_state_chars"):
            least, most = (1, 64) if key == "max_questions" else (1000, 32000)
            valid = type(value) is int and least <= value <= most
        else:
            most = 720 if key == "cache_hours" else 1
            valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= most
        if not valid:
            raise ConfigError("OAH_CONFIG_INVALID", f"rerank.judgement.{key} is outside its supported type or range")
        result[key] = value
    if result["no"] >= result["yes"]:
        raise ConfigError("OAH_CONFIG_INVALID", "rerank.judgement.no must be less than yes")
    return result
