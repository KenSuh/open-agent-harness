# 선택적 AI 재순위 인터페이스 — 사용자 동의와 키가 모두 있을 때만 제공자를 부르고, 순서만 바꾸며 결과마다 used/unused(사유)를 남긴다.
"""Optional AI re-ranking (A2, A9, A10).

    section = load_config(root).section("rerank", rerank.DEFAULTS)
    result = rerank.rerank("impact", candidates, config=section, query=question)
    result.status   # "used" or "unused(<reason>)"

A provider runs only when every gate passes, checked in this order:
1. `unused(disabled-by-brief)`: the caller passed `brief_disabled=True` (`--no-rerank`, or the packet);
2. `unused(not-enabled)`: `[rerank] enabled` is not `true` (the default is off);
3. `unused(not-configured)`: `[rerank] provider` is empty; `unused(unknown-provider)` names none known;
4. `unused(no key)`: `oah.secrets.get(<the provider's key name>)` found nothing.
A key alone never enables a provider. A provider failure reports `unused(error:<code>)`.

A provider only scores or advises. `rerank` builds the order itself, so the result is always a
permutation of the input: no row is dropped, added or duplicated. Rows beyond `max_candidates` are
not sent and keep their place after the re-ordered ones. `advise_difficulty` records the provider's
tier next to the rule tier and never changes it: `Advice.tier` is the rule tier.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from oah import secrets
from oah.rerank.base import TIERS, Candidate, Provider, ProviderError
from oah.rerank.typesafe import DEFAULT_KEY_NAME, DEFAULT_MODEL, DEFAULT_URL, TypeSafeProvider

__all__ = ["DEFAULTS", "PROVIDERS", "TIERS", "Advice", "Candidate", "Provider", "ProviderError",
           "RerankResult", "advise_difficulty", "rerank"]

DEFAULTS = {
    "enabled": False,
    "provider": "",
    "timeout_seconds": 10.0,
    "max_candidates": 50,
    "typesafe": {
        "url": DEFAULT_URL,
        "model": DEFAULT_MODEL,
        "key_name": DEFAULT_KEY_NAME,
    },
}

PROVIDERS: dict[str, Provider] = {"typesafe": TypeSafeProvider()}

USED = "used"


def unused(reason: str) -> str:
    return f"unused({reason})"


@dataclass(frozen=True)
class RerankResult:
    order: tuple[Candidate, ...]
    status: str
    provider: str | None = None
    scores: tuple[tuple[str, float], ...] = ()

    @property
    def used(self) -> bool:
        return self.status == USED

    def to_record(self) -> dict:
        return {"rerank": self.status, "provider": self.provider,
                "order": [item.name for item in self.order], "scores": dict(self.scores)}


@dataclass(frozen=True)
class Advice:
    rule_tier: str
    advisory_tier: str | None
    confidence: float | None
    status: str
    provider: str | None = None

    @property
    def tier(self) -> str:
        """The tier to apply: always the rule result. The advice is recorded, never applied downward."""
        return self.rule_tier

    def to_record(self) -> dict:
        return {"tier": self.rule_tier, "tier_source": "rules", "advice": self.status,
                "provider": self.provider, "advisory_tier": self.advisory_tier,
                "advisory_confidence": self.confidence}


def _gate(config: Mapping, *, brief_disabled: bool, env: Mapping[str, str] | None,
          keychain: secrets.Keychain | None,
          providers: Mapping[str, Provider] | None) -> tuple[Provider | None, str | None, str]:
    """(provider, key, status): provider and key are set only when every gate passed."""
    if brief_disabled:
        return None, None, unused("disabled-by-brief")
    if config.get("enabled") is not True:
        return None, None, unused("not-enabled")
    name = config.get("provider")
    if not isinstance(name, str) or not name.strip():
        return None, None, unused("not-configured")
    provider = (PROVIDERS if providers is None else providers).get(name.strip())
    if provider is None:
        return None, None, unused("unknown-provider")
    try:
        key = secrets.get(provider.key_name(config), env=env, keychain=keychain)
    except ValueError:
        return None, None, unused("error:key-name")
    if not key:
        return None, None, unused("no key")
    return provider, key, USED


def _call(action: Callable[[], object]) -> tuple[object, str]:
    """Run a provider call; failures become a status code and never carry exception text."""
    try:
        return action(), USED
    except ProviderError as exc:
        return None, unused(f"error:{exc.code}")
    except Exception:  # noqa: BLE001 - an optional plugin must never break the command that asked.
        return None, unused("error:internal")


def _valid_scores(scores: object, count: int) -> list[float] | None:
    if isinstance(scores, (str, bytes, Mapping)) or not isinstance(scores, Sequence) or len(scores) != count:
        return None
    result = []
    for value in scores:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        result.append(float(value))
    return result


def rerank(kind: str, candidates: Sequence[Candidate], *, config: Mapping, query: str = "",
           brief_disabled: bool = False, env: Mapping[str, str] | None = None,
           keychain: secrets.Keychain | None = None,
           providers: Mapping[str, Provider] | None = None) -> RerankResult:
    """Re-order candidates when the user enabled a provider and its key exists; else keep the order."""
    items = tuple(candidates)
    if len({item.name for item in items}) != len(items):
        raise ValueError("rerank candidates need unique names")
    provider, key, status = _gate(config, brief_disabled=brief_disabled, env=env, keychain=keychain,
                                  providers=providers)
    if provider is None:
        return RerankResult(items, status)
    if not items:
        return RerankResult(items, unused("no-candidates"), provider.name)
    limit = config.get("max_candidates")
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else len(items)
    sent, rest = items[:limit], items[limit:]
    raw, status = _call(lambda: provider.score(kind, query, sent, key=key, config=config))
    if status != USED:
        return RerankResult(items, status, provider.name)
    scores = _valid_scores(raw, len(sent))
    if scores is None:
        return RerankResult(items, unused("error:bad-response"), provider.name)
    ranked = sorted(range(len(sent)), key=lambda index: -scores[index])
    order = tuple(sent[index] for index in ranked) + rest
    return RerankResult(order, USED, provider.name,
                        tuple((item.name, score) for item, score in zip(sent, scores)))


def advise_difficulty(axes: Mapping[str, str], task: str, *, config: Mapping, brief_disabled: bool = False,
                      env: Mapping[str, str] | None = None, keychain: secrets.Keychain | None = None,
                      providers: Mapping[str, Provider] | None = None) -> Advice:
    """An advisory AI tier recorded next to the rule tier in `axes["tier"]`, which it never changes."""
    rule_tier = axes.get("tier")
    if rule_tier not in TIERS:
        raise ValueError(f"advise_difficulty needs axes['tier'] set to one of {', '.join(TIERS)}")
    provider, key, status = _gate(config, brief_disabled=brief_disabled, env=env, keychain=keychain,
                                  providers=providers)
    if provider is None:
        return Advice(rule_tier, None, None, status)
    answer, status = _call(lambda: provider.advise(axes, task, key=key, config=config))
    if status != USED:
        return Advice(rule_tier, None, None, status, provider.name)
    if not isinstance(answer, tuple) or len(answer) != 2 or answer[0] not in TIERS:
        return Advice(rule_tier, None, None, unused("error:bad-response"), provider.name)
    tier, confidence = answer
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not math.isfinite(confidence)):
        return Advice(rule_tier, None, None, unused("error:bad-response"), provider.name)
    return Advice(rule_tier, tier, None if confidence is None else float(confidence), USED, provider.name)
