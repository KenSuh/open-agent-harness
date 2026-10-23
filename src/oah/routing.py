# 라우팅 — 난이도 네 축의 점수표로 단(tier)을 고르고, 단계·범위·위험 경로로 축을 규칙 채점하며, 단계별 엔진을 강제한다.
"""Stage routing and difficulty scoring (PLAN 5.5, A10).

`recommend(axes)` keeps the source's scoring unchanged: a base tier from the summed axis scores and
three named triggers, then lifts by intensity and reasoning, then kind floors applied last. Every rule
that applied is listed in `Recommendation.reasons`.

`score_axes(...)` fills the axes by rules when nobody gave a tier: the stage names the kind, the
Scope size sets intensity, its file kinds set reasoning, and risk paths (workflows, migrations, SQL,
auth, security, billing) raise risk and reasoning to high. Explicit values override the rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

TIERS = ("low", "medium", "high", "max")
STAGES = ("initial", "review", "correction")
ENGINES = ("claude", "codex")

KIND_SCORES = {
    "quick": 1, "docs": 1, "research": 1,
    "review": 2, "implementation": 2, "refactor": 2, "broad-audit": 2,
    "adversarial-review": 3, "migration": 3, "security": 3, "high-risk-review": 3,
}
INTENSITY_SCORES = {"low": 0, "medium": 1, "high": 2, "max": 3}
REASONING_SCORES = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
RISK_SCORES = {"low": 0, "normal": 0, "high": 2, "critical": 4}

FLOOR_HIGH = ("broad-audit", "migration", "security", "high-risk-review")
FLOOR_MEDIUM = ("research", "review", "implementation", "refactor", "adversarial-review")

STAGE_KINDS = {"initial": "implementation", "review": "review", "correction": "implementation"}
DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc")
RISK_TOKENS = {"auth", "oauth", "authn", "authz", "security", "billing", "payment", "payments"}
INTENSITY_MEDIUM_FROM, INTENSITY_HIGH_FROM = 4, 13


class RoutingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Axes:
    kind: str
    intensity: str
    reasoning: str
    risk: str

    def to_json(self) -> dict:
        return {"kind": self.kind, "intensity": self.intensity, "reasoning": self.reasoning, "risk": self.risk}


@dataclass(frozen=True)
class Recommendation:
    tier: str
    score: int
    base: str
    reasons: tuple[str, ...]

    def to_json(self) -> dict:
        return {"tier": self.tier, "score": self.score, "base": self.base, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class ScoredAxes:
    axes: Axes
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())


def _check(axis: str, value: str, table: Mapping[str, int]) -> int:
    if value not in table:
        raise RoutingError("OAH_RECOMMEND_INVALID",
                           f"--{axis} {value!r} is not a known value; use one of: {', '.join(table)}")
    return table[value]


def _at_least(tier: str, floor: str) -> str:
    return tier if TIERS.index(tier) >= TIERS.index(floor) else floor


def recommend(axes: Axes) -> Recommendation:
    """The tier for these axes, from the scoring table, lifts and kind floors of PLAN 5.5."""
    score = (_check("kind", axes.kind, KIND_SCORES) + _check("intensity", axes.intensity, INTENSITY_SCORES)
             + _check("reasoning", axes.reasoning, REASONING_SCORES) + _check("risk", axes.risk, RISK_SCORES))
    rules = [
        ("max", [(axes.risk == "critical", "risk critical"), (axes.reasoning == "max", "reasoning max"),
                 (score >= 8, f"score {score} >= 8")]),
        ("high", [(axes.risk == "high", "risk high"), (axes.intensity == "max", "intensity max"),
                  (score >= 6, f"score {score} >= 6")]),
        ("medium", [(axes.intensity == "high", "intensity high"),
                    (axes.reasoning in ("high", "xhigh"), f"reasoning {axes.reasoning}"),
                    (score >= 3, f"score {score} >= 3")]),
    ]
    reasons: list[str] = []
    base = "low"
    for tier, conditions in rules:
        met = [name for condition, name in conditions if condition]
        if met:
            base = tier
            reasons.extend(f"base {tier}: {name}" for name in met)
            break
    else:
        reasons.append("base low")
    tier = base
    lifts = {"medium": "medium", "high": "high", "max": "max"}
    if axes.intensity in lifts:
        tier = _at_least(tier, lifts[axes.intensity])
        reasons.append(f"lift {lifts[axes.intensity]}: intensity {axes.intensity}")
    if axes.reasoning in ("high", "xhigh"):
        tier = _at_least(tier, "high")
        reasons.append(f"lift high: reasoning {axes.reasoning}")
    elif axes.reasoning == "max":
        tier = "max"
        reasons.append("lift max: reasoning max")
    if axes.kind in FLOOR_HIGH:
        tier = _at_least(tier, "high")
        reasons.append(f"floor high: kind {axes.kind}")
    elif axes.kind in FLOOR_MEDIUM:
        tier = _at_least(tier, "medium")
        reasons.append(f"floor medium: kind {axes.kind}")
    return Recommendation(tier=tier, score=score, base=base, reasons=tuple(reasons))


def _risk_reason(path: str) -> str | None:
    parts = [part for part in path.replace("\\", "/").lower().split("/") if part]
    if not parts:
        return None
    if "/".join(parts).startswith(".github/workflows/") or parts[:2] == [".github", "workflows"]:
        return f"risk path {path} (workflow)"
    if "migrations" in parts[:-1] or parts[-1] == "migrations":
        return f"risk path {path} (migration)"
    if parts[-1].endswith(".sql"):
        return f"risk path {path} (SQL)"
    for part in parts:
        tokens = set(re.split(r"[^a-z0-9]+", part))
        hit = tokens & RISK_TOKENS
        if hit:
            return f"risk path {path} ({sorted(hit)[0]})"
    return None


def score_axes(*, stage: str, kind: str | None, scope: Sequence[str],
               overrides: Mapping[str, str] | None = None) -> ScoredAxes:
    """The difficulty axes by rules (A10); `overrides` holds values the user gave explicitly."""
    if stage not in STAGE_KINDS:
        raise RoutingError("OAH_STAGE_UNKNOWN", f"stage {stage!r} is not known; use one of: {', '.join(STAGES)}")
    reasons: list[str] = []
    warnings: list[str] = []
    reasons.append(f"kind {kind} given explicitly" if kind else f"kind {STAGE_KINDS[stage]} from stage {stage}")
    kind = kind or STAGE_KINDS[stage]
    paths = [entry for entry in scope if entry]
    if not paths:
        warnings.append("OAH_SCOPE_EMPTY: this task has no Scope paths, so difficulty was scored without "
                        "file kinds; add paths to the packet's ## Scope or the lease, or pass --tier")
        intensity, reasoning = "low", "medium"
        reasons.append("intensity low: empty scope")
        reasons.append("reasoning medium: empty scope")
    elif "repo" in paths:
        intensity, reasoning = "high", "medium"
        reasons.append("intensity high: scope is the whole repository")
        reasons.append("reasoning medium: scope is the whole repository")
    else:
        count = len(paths)
        intensity = ("high" if count >= INTENSITY_HIGH_FROM else "medium" if count >= INTENSITY_MEDIUM_FROM
                     else "low")
        reasons.append(f"intensity {intensity}: {count} scope path(s)")
        docs_only = all(path.lower().endswith(DOC_SUFFIXES) for path in paths)
        reasoning = "low" if docs_only else "medium"
        reasons.append(f"reasoning {reasoning}: {'documents only' if docs_only else 'code or other files'}")
    risk = "normal"
    for path in paths:
        hit = _risk_reason(path)
        if hit:
            risk, reasoning = "high", "high" if REASONING_SCORES[reasoning] < REASONING_SCORES["high"] else reasoning
            reasons.append(hit)
    values = {"kind": kind, "intensity": intensity, "reasoning": reasoning, "risk": risk}
    for axis, value in (overrides or {}).items():
        if value is not None:
            values[axis] = value
            reasons.append(f"{axis} {value}: given explicitly")
    return ScoredAxes(axes=Axes(**values), reasons=tuple(reasons), warnings=tuple(warnings))


def check_stage(engine: str, stage: str, stages: Mapping[str, str], *, override: bool) -> list[str]:
    """Refuse an engine that is not configured for this stage, unless overridden (then warn)."""
    if engine not in ENGINES:
        raise RoutingError("OAH_ENGINE_UNKNOWN", f"engine {engine!r} is not known; use one of: {', '.join(ENGINES)}")
    if stage not in stages:
        raise RoutingError("OAH_STAGE_UNKNOWN", f"stage {stage!r} is not known; use one of: {', '.join(stages)}")
    expected = stages[stage]
    if engine == expected:
        return []
    if not override:
        raise RoutingError("OAH_STAGE_ENGINE_MISMATCH",
                           f"stage {stage} runs on {expected}, not {engine}; run `oah call {expected} --stage "
                           f"{stage}`, change [stages] in .oah/config.toml, or pass --override-stage (recorded)")
    return [f"OAH_STAGE_OVERRIDDEN: stage {stage} is configured for {expected}; running {engine} because "
            "--override-stage was given (recorded in the events)"]


def same_engine_warning(stages: Mapping[str, str]) -> str | None:
    """A warning when the initial implementation is reviewed by the engine that wrote it (one-engine setup)."""
    review = stages.get("review")
    if review is not None and review == stages.get("initial"):
        return (f"OAH_REVIEW_NOT_INDEPENDENT: {review} both writes and reviews code, so same-engine review "
                "is not independent; configure a different engine for [stages] review if you have one")
    return None
