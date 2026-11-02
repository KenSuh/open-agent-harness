# 점수 규칙 — 패키지에 실린 모델군·벤치마크 자료를 검증해 읽고, 한 호출의 점수 집합을 만들어 단·프로필이 요구하는 점수로 모델을 고른다.
"""How benchmark scores pick a model. Pure: no network, no state, and no I/O beyond the two packaged files.

Data
    `families()` reads `templates/engines/families.json`: which models count as a known family, and their
    list prices. `bundled()` reads `templates/engines/benchmarks.json`, built by `tools/build_benchmarks.py`
    from Epoch AI's published data. Every field is validated. A missing or damaged file raises
    `OAH_MODEL_NO_SCORES`, which names the one-line pin that gets past it.

Score set
    `score_set` turns one catalog entry into the `ScoreSet` of one call. Listed models that match the
    family rules are candidates. The tasks every candidate has are the common tasks; a candidate that would
    leave fewer than 3 is unscored, weakest coverage first, and on a tie the older generation goes first. Each
    family keeps its newest generation with a score, so a family never goes empty because a new model
    has no score yet. Steps 2 and 3 then run once more over the models that survive, because the
    common tasks must be those of the models that compete. Each task is scaled from 0 to 100 between the
    lowest and highest of them, and the score is the mean. `allowed` filters last, so it never moves the scale.
    Models left out keep a score on the same scale where they have the tasks: only floors read it.

Rule
    `choose` numbers the tiers low 0 to max 3, adds the profile's offset, keeps the step in 0 to 3 and takes the
    larger of the step and the floor step. The required score sits step/3 of the way from the lowest to the
    highest eligible score. Balanced and efficient also accept a model within 2 percent of that range below it, so
    a gap that small buys nothing. Performance has no such margin. A floor never has one: `clears` is the floor
    test, and `choose` applies it too, so the rule cannot pick a model the admission check would refuse. The
    cheapest model that clears wins, then the lower score, then the name, so a choice never flips.

    No table maps a difficulty to a model: the result follows the scores and moves when they move.
    `band` is what a judge may pick from: the rule's choice, and the models from this step's required
    score up to the next step's, so a judge moves at most one step up and never below the rule's step.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Mapping, Sequence

from oah.engines.effort import EngineError
from oah.routing import ENGINES, TIERS

NO_SCORES = "OAH_MODEL_NO_SCORES"
DEFAULT_PROFILE = "balanced"
PROFILES = {"performance": 1, "balanced": 0, "efficient": -1}
MARGIN = 0.02
MIN_COMMON_TASKS = 3
TOP_STEP = len(TIERS) - 1
SCHEMA = 1
_PIN = 'pin a model with [engines.{engine}.tiers] <tier> = {{ model = "<model>" }} in .oah/config.toml'
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,79}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CLAUDE_ID = re.compile(r"claude-(?P<family>[a-z]{2,16})-(?P<major>[0-9]{1,3})(?:-(?P<minor>[0-9]{1,3}))?")


@dataclass(frozen=True)
class Scored:
    model: str
    family: str
    generation: tuple[int, int]
    score: float
    cost: float | None


@dataclass(frozen=True)
class ScoreSet:
    """One engine, one launch: read once and passed down."""

    engine: str
    source: str
    date: str
    credit: str
    models: tuple[Scored, ...]
    left_out: Mapping[str, str]  # listed model -> superseded | unknown-family | unscored | not-allowed
    retired: tuple[Scored, ...] = ()  # left-out models on the same scale; only a floor check reads these
    failure: str = ""  # why no scores could be read, kept until a choice needs them


@dataclass(frozen=True)
class Choice:
    model: str
    step: int
    floor_step: int
    required: float
    source: str


@dataclass(frozen=True)
class _Candidate:
    model: str
    family: str
    generation: tuple[int, int]
    cost: float
    tasks: Mapping[str, float]


def _unusable(name: str, why: str) -> EngineError:
    return EngineError(NO_SCORES, f"the packaged file templates/engines/{name} {why}, so scores cannot choose a model; "
                       f"reinstall oah, or {_PIN.format(engine='<engine>')}")


def _packaged(name: str) -> bytes:
    return resources.files("oah").joinpath("templates", "engines", name).read_bytes()


def _reject_constant(constant: str) -> None:
    raise ValueError(constant)


def _load(name: str) -> dict:
    try:
        data = json.loads(_packaged(name).decode("utf-8"), parse_constant=_reject_constant)
    except OSError:
        raise _unusable(name, "cannot be read") from None
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise _unusable(name, "is not valid JSON") from None
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != SCHEMA:
        raise _unusable(name, f"does not have schema {SCHEMA}")
    return data


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _prices_valid(prices: object) -> bool:
    return isinstance(prices, dict) and bool(prices) and all(
        isinstance(name, str) and _NAME.fullmatch(name) and isinstance(price, list) and len(price) == 2
        and all(_number(item) and item >= 0 for item in price) for name, price in prices.items())


def _identity_valid(identity: object) -> bool:
    if not isinstance(identity, str):
        return False
    try:
        return {"major", "minor", "family"} <= set(re.compile(identity).groupindex)
    except re.error:
        return False


def families() -> Mapping:
    """The packaged family rules, validated; a bad file raises `OAH_MODEL_NO_SCORES`."""
    name = "families.json"
    data = _load(name)
    engines = data.get("engines")
    if not isinstance(engines, dict) or not isinstance(data.get("prices"), dict):
        raise _unusable(name, "has no engines or prices")
    codex, claude = engines.get("codex"), engines.get("claude")
    if (set(engines) != set(ENGINES) or not isinstance(codex, dict) or not isinstance(claude, dict)
            or not _identity_valid(codex.get("identity")) or not _prices_valid(codex.get("families"))
            or not _prices_valid(claude.get("aliases"))):
        raise _unusable(name, "has an invalid engine entry")
    return data


def bundled() -> Mapping:
    """The packaged benchmark scores, validated; a bad file raises `OAH_MODEL_NO_SCORES`."""
    name = "benchmarks.json"
    data = _load(name)
    for key in ("source", "license", "url", "credit"):
        if not (isinstance(data.get(key), str) and data[key].strip()):
            raise _unusable(name, f"has no {key}")
    if not (isinstance(data.get("date"), str) and _DATE.fullmatch(data["date"])):
        raise _unusable(name, "has no date")
    scores = data.get("scores")
    if not isinstance(scores, dict) or not scores:
        raise _unusable(name, "has no scores")
    for model, tasks in scores.items():
        if not (_NAME.fullmatch(model) and isinstance(tasks, dict) and tasks):
            raise _unusable(name, "has an invalid model entry")
        for task, value in tasks.items():
            if not (_TASK.fullmatch(task) and _number(value) and 0 <= value <= 1):
                raise _unusable(name, "has an invalid score")
    return data


def _newest_claude(family: str, scores: Mapping) -> tuple[tuple[int, int], Mapping[str, float]]:
    """The generation and tasks of an alias: the newest `claude-<family>-<major>[-<minor>]` in the data."""
    found = []
    for model, tasks in scores.items():
        match = _CLAUDE_ID.fullmatch(model)
        if match is not None and match["family"] == family:
            found.append(((int(match["major"]), int(match["minor"] or 0)), tasks))
    return max(found, key=lambda item: item[0], default=((0, 0), {}))


def _candidate(engine: str, spec: Mapping, name: str, scores: Mapping) -> _Candidate | None:
    if engine == "claude":
        price = spec["aliases"].get(name)
        if price is None:
            return None
        generation, tasks = _newest_claude(name, scores)
        return _Candidate(name, name, generation, sum(price), tasks)
    match = re.compile(spec["identity"]).fullmatch(name)
    price = spec["families"].get(match["family"]) if match is not None else None
    if price is None:
        return None
    generation = (int(match["major"]), int(match["minor"] or 0))
    return _Candidate(name, match["family"], generation, sum(price), scores.get(name, {}))


def _competing(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    """Drop candidates, weakest coverage first, until at least 3 tasks are common to all that remain."""
    kept = list(candidates)
    while kept and len(set.intersection(*(set(item.tasks) for item in kept))) < MIN_COMMON_TASKS:
        kept.remove(min(kept, key=lambda item: (len(item.tasks), item.generation, item.model)))
    return kept


def score_set(engine: str, entry, *, allowed: Sequence[str], data: Mapping | None = None,
              known: Mapping | None = None) -> ScoreSet:
    """The eligible models of one catalog entry; `data` and `known` let one read serve two entries."""
    known = families() if known is None else known
    data = bundled() if data is None else data
    spec = known["engines"][engine]
    left_out: dict[str, str] = {}
    candidates: list[_Candidate] = []
    for model in entry.models if entry is not None else ():
        if model.listed:
            found = _candidate(engine, spec, model.name, data["scores"])
            if found is None:
                left_out[model.name] = "unknown-family"
            else:
                candidates.append(found)
    newest: dict[str, _Candidate] = {}
    for item in _competing(candidates):
        best = newest.get(item.family)
        if best is None or (item.generation, item.model) > (best.generation, best.model):
            newest[item.family] = item
    for item in candidates:
        best = newest.get(item.family)
        if best is None or item.generation > best.generation:
            left_out[item.model] = "unscored"
        elif item is not best:
            left_out[item.model] = "superseded"
    competing = list(newest.values())
    if not competing:
        raise EngineError(NO_SCORES, f"no model the {engine} CLI lists has a usable score, so scores cannot choose "
                          f"one; run `oah models` to see why each was left out, or {_PIN.format(engine=engine)}")
    common = sorted(set.intersection(*(set(item.tasks) for item in competing)))
    bounds = {task: (min(item.tasks[task] for item in competing), max(item.tasks[task] for item in competing))
              for task in common}

    def scale(task: str, value: float) -> float:
        lowest, highest = bounds[task]
        return (value - lowest) / (highest - lowest) * 100 if highest > lowest else 0.0

    def record(item: _Candidate) -> Scored:
        score = sum(scale(task, item.tasks[task]) for task in common) / len(common)
        return Scored(item.model, item.family, item.generation, score, item.cost)

    ranked = sorted(map(record, competing), key=lambda item: (-item.score, item.model))
    retired = [record(item) for item in candidates if item not in competing and set(common) <= set(item.tasks)]
    if allowed:
        left_out.update((item.model, "not-allowed") for item in ranked if item.model not in allowed)
        retired += [item for item in ranked if item.model not in allowed]
        ranked = [item for item in ranked if item.model in allowed]
    if not ranked:
        raise EngineError(NO_SCORES, f"no model in [engines.{engine}] allowed_models has a usable score, so scores "
                          f"cannot choose one; run `oah models` to see why each was left out, or "
                          f"{_PIN.format(engine=engine)}")
    return ScoreSet(engine, data["source"], data["date"], data["credit"], tuple(ranked), left_out, tuple(retired))


def failed(engine: str, error: EngineError) -> ScoreSet:
    """A score set with no models that keeps why, so that a pin still runs and only a choice fails."""
    return ScoreSet(engine, "", "", "", (), {}, failure=str(error).split(": ", 1)[-1])


def _bounds(scores: ScoreSet) -> tuple[float, float]:
    if not scores.models:
        raise EngineError(NO_SCORES, scores.failure or f"no scores are available for {scores.engine}; "
                          f"{_PIN.format(engine=scores.engine)}")
    values = [model.score for model in scores.models]
    return min(values), max(values)


def required(scores: ScoreSet, step: int) -> float:
    """The score a step needs; the ends are exact, so the top scorer always clears the top step."""
    lowest, highest = _bounds(scores)
    return highest if step >= TOP_STEP else lowest + step / TOP_STEP * (highest - lowest)


def _pick(scores: ScoreSet, step: int, floor_step: int, profile: str) -> Choice:
    lowest, highest = _bounds(scores)
    need = required(scores, step)
    slack = 0.0 if profile == "performance" else MARGIN * (highest - lowest)
    floor = required(scores, floor_step)
    fits = [model for model in scores.models if model.score >= need - slack and model.score >= floor]
    best = min(fits, key=lambda model: (model.cost is None, model.cost or 0.0, model.score, model.model))
    return Choice(best.model, step, floor_step, need, scores.source)


def choose(scores: ScoreSet, tier: str, floor_tier: str, profile: str) -> Choice:
    """The automatic model for a tier: the cheapest model that clears the step the tier, floor and profile set."""
    step = min(max(TIERS.index(tier) + PROFILES[profile], 0), TOP_STEP)
    floor_step = TIERS.index(floor_tier)
    return _pick(scores, max(step, floor_step), floor_step, profile)


def at_step(scores: ScoreSet, step: int, profile: str) -> Choice:
    """The rule's choice at one step, with no floor: what a catalog rebuild compares."""
    return _pick(scores, step, 0, profile)


def score_of(scores: ScoreSet, model: str) -> float | None:
    """The model's score, also for a left-out model named by the user, or None when it has none."""
    found = scored(scores, model)
    return found.score if found is not None else None


def scored(scores: ScoreSet, model: str) -> Scored | None:
    return next((item for item in (*scores.models, *scores.retired) if item.model == model), None)


def clears(scores: ScoreSet, model: str, step: int) -> bool:
    """Whether a model reaches a step's required score. A floor has no margin, in any profile."""
    value = score_of(scores, model)
    return value is not None and bool(scores.models) and value >= required(scores, step)


def band(scores: ScoreSet, choice: Choice) -> tuple[str, ...]:
    """What a judge may pick: the rule's choice, then the models from this step's score up to the next step's."""
    low = required(scores, choice.step)
    high = required(scores, choice.step + 1) if choice.step < TOP_STEP else math.inf
    return (choice.model, *(model.model for model in scores.models
                            if low <= model.score <= high and model.model != choice.model))
