# 점수 규칙 — 패키지에 실린 모델군·벤치마크 자료를 검증해 읽고, 한 호출의 점수 집합을 만들어 단·프로필이 요구하는 점수로 모델을 고른다.
"""How benchmark scores pick a model. Pure: no network, no state, and no I/O beyond the two packaged files.

Data
    `families()` reads `templates/engines/families.json`: which models count as a known family, and their
    list prices. `bundled()` reads `templates/engines/benchmarks.json`, built by `tools/build_benchmarks.py`
    from Epoch AI's published data. Every field is validated. A missing or damaged file raises
    `OAH_MODEL_NO_SCORES`, which names the one-line pin that gets past it.

Score set
    `score_set` turns one catalog entry into the `ScoreSet` of one call. The scale comes from the data alone,
    so what one machine lists or allows moves no score, required score or floor. It runs over every known
    family: each family's newest generation in the data, and a generation that would leave fewer than 3 common
    tasks gives way to the one before it, weakest coverage first, and on a tie the older generation. The tasks those
    models share are the common tasks. Each task is scaled from 0 to 100 between the lowest and highest of them,
    and a score is the mean.

    Listing and `allowed` then only decide what may be picked. A listed model that has every common task gets its
    score on that scale, and each family keeps its newest such generation, so a family never goes empty because a
    new model has no score yet. An older generation can stand in when the CLI does not list the newest one: it is
    scored like the rest, a task outside the scale counts as the end it passed, and it does not stretch the scale.
    A model with no score is unscored. Every other scored model, listed or not, keeps its score in `retired`,
    which only a floor check reads, so a pin is measured even with no catalog and nothing left to pick.

Rule
    `choose` numbers the tiers low 0 to max 3, adds the profile's offset, keeps the step in 0 to 3 and takes the
    larger of the step and the floor step. The required score sits step/3 of the way along the fixed scale.
    Balanced and efficient also accept a model within 2 percent of the scale below it, so a gap that small buys
    nothing. Performance has no such margin. A floor never has one: `clears` is the floor test, and `choose`
    applies it too, so the rule cannot pick a model the admission check would refuse.

    Performance and balanced take the cheapest model that clears, then the lower score, then the name, so a choice
    never flips. Efficient does not step down by itself: its lowest allowed step is one below the tier, and among
    the models that clear it and pass the effort rules (`usable`) it takes the most score per cost, then the lower
    score, the lower cost and the name. So a stronger model wins when it is the better value. In every profile,
    `usable` drops models the effort rules refuse before choosing, including in the strongest-model fallback.

    When nothing that may be picked reaches the step, for example on a machine that does not list the top of the
    scale, the strongest model that meets the floor runs. When nothing meets the floor, `FloorUnmet` says so, and
    a call turns it into `OAH_MODEL_BELOW_FLOOR` for a review.

    No table maps a difficulty to a model: the result follows the scores and moves when they move.
    `band` is what a judge may pick from: the rule's choice, and the models from the lowest allowed step's required
    score up to the next step's, so a judge moves at most one step up and never below that step.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Callable, Mapping, Sequence

from oah.engines.effort import EngineError
from oah.routing import ENGINES, TIERS

NO_SCORES = "OAH_MODEL_NO_SCORES"
UNRESOLVED = "OAH_MODEL_UNRESOLVED"
DEFAULT_PROFILE = "balanced"
PROFILES = {"performance": 1, "balanced": 0, "efficient": 0}  # steps added to the tier; efficient looks one lower too
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
    stays: Mapping[str, str] = field(default_factory=dict)  # an unscored model -> the one its family keeps using
    scale: tuple[float, float] | None = None  # lowest and highest of the fixed scale; a hand-built set uses `models`


@dataclass(frozen=True)
class Choice:
    model: str
    step: int
    floor_step: int
    required: float  # the score of `lowest_step`
    source: str
    lowest_step: int  # the lowest step this choice could come from: `step`, or for efficient one below the tier


@dataclass(frozen=True)
class _Candidate:
    model: str
    family: str
    generation: tuple[int, int]
    cost: float
    tasks: Mapping[str, float]


class FloorUnmet(EngineError):
    """No model that may be picked reaches the floor."""


def _unusable(name: str, why: str) -> EngineError:
    return EngineError(NO_SCORES, f"the packaged file templates/engines/{name} {why}, so scores cannot choose a model; "
                       f"reinstall oah, or {_PIN.format(engine='<engine>')}")


def _packaged(name: str) -> bytes:
    return resources.files("oah").joinpath(f"templates/engines/{name}").read_bytes()


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
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:  # a valid JSON integer may be too large to convert to a float
        return False


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


def _in_data(engine: str, spec: Mapping, scores: Mapping) -> list[_Candidate]:
    """Every known-family model with data, whatever a CLI lists; an alias stands for its newest generation."""
    found = (_candidate(engine, spec, name, scores) for name in (spec["aliases"] if engine == "claude" else scores))
    return [item for item in found if item is not None and item.tasks]


def _leaders(pool: Sequence[_Candidate]) -> list[_Candidate]:
    """The models of the scale: each family's newest generation, and one too thin gives way to the one before it."""
    queues: dict[str, list[_Candidate]] = {}
    for item in sorted(pool, key=lambda item: (item.generation, item.model), reverse=True):
        queues.setdefault(item.family, []).append(item)
    while True:
        front = [queue[0] for queue in queues.values() if queue]
        kept = _competing(front)
        if len(kept) == len(front):
            return kept
        for item in front:
            if item not in kept:
                queues[item.family].pop(0)


def score_set(engine: str, entry, *, allowed: Sequence[str], data: Mapping | None = None,
              known: Mapping | None = None) -> ScoreSet:
    """The fixed scale and the models one entry may pick; `data` and `known` let one read serve two entries."""
    known = families() if known is None else known
    data = bundled() if data is None else data
    spec = known["engines"][engine]
    in_data = _in_data(engine, spec, data["scores"])
    leaders = _leaders(in_data)
    none_listed = (f"no model the {engine} CLI lists has a usable score, so scores cannot choose one; run `oah models` "
                   f"to see why each was left out, or {_PIN.format(engine=engine)}")
    if not leaders:
        raise EngineError(NO_SCORES, none_listed)
    common = sorted(set.intersection(*(set(item.tasks) for item in leaders)))
    bounds = {task: (min(item.tasks[task] for item in leaders), max(item.tasks[task] for item in leaders))
              for task in common}

    def place(task: str, value: float) -> float:
        lowest, highest = bounds[task]
        return (min(max(value, lowest), highest) - lowest) / (highest - lowest) * 100 if highest > lowest else 0.0

    def record(item: _Candidate) -> Scored:
        score = sum(place(task, item.tasks[task]) for task in common) / len(common)
        return Scored(item.model, item.family, item.generation, score, item.cost)

    def has_tasks(item: _Candidate) -> bool:
        return all(task in item.tasks for task in common)

    left_out: dict[str, str] = {}
    stays: dict[str, str] = {}
    candidates: list[_Candidate] = []
    for model in entry.models if entry is not None else ():
        if model.listed:
            found = _candidate(engine, spec, model.name, data["scores"])
            if found is None:
                left_out[model.name] = "unknown-family"
            else:
                candidates.append(found)
    newest: dict[str, _Candidate] = {}
    for item in filter(has_tasks, candidates):
        best = newest.get(item.family)
        if best is None or (item.generation, item.model) > (best.generation, best.model):
            newest[item.family] = item
    for item in candidates:
        best = newest.get(item.family)
        if best is None or item.generation > best.generation:
            left_out[item.model] = "unscored"
            if best is not None:
                stays[item.model] = best.model
        elif item is not best:
            left_out[item.model] = "superseded"
    ranked = sorted(map(record, newest.values()), key=lambda item: (-item.score, item.model))
    picks = [item for item in ranked if not allowed or item.model in allowed]
    left_out.update((item.model, "not-allowed") for item in ranked if item not in picks)
    names = {item.model for item in picks}
    # Full Claude generation names are floor evidence too; eligibility still uses only the aliases above.
    if engine == "claude":
        for name, tasks in data["scores"].items():
            match = _CLAUDE_ID.fullmatch(name)
            if match is not None and match["family"] in spec["aliases"]:
                in_data.append(_Candidate(name, match["family"], (int(match["major"]), int(match["minor"] or 0)),
                                          sum(spec["aliases"][match["family"]]), tasks))
    retired = {item.model: record(item) for item in (*in_data, *candidates) if item.model not in names and has_tasks(item)}
    values = [record(item).score for item in leaders]
    nothing_allowed = (f"no model in [engines.{engine}] allowed_models has a usable score, so scores cannot choose one; "
                       f"run `oah models` to see why each was left out, or {_PIN.format(engine=engine)}")
    return ScoreSet(engine, data["source"], data["date"], data["credit"], tuple(picks), left_out, tuple(retired.values()),
                    "" if picks else nothing_allowed if ranked else none_listed, stays, (min(values), max(values)))


def failed(engine: str, error: EngineError) -> ScoreSet:
    """A score set with no models that keeps why, so that a pin still runs and only a choice fails."""
    return ScoreSet(engine, "", "", "", (), {}, failure=str(error).split(": ", 1)[-1])


def _no_scores(scores: ScoreSet) -> EngineError:
    return EngineError(NO_SCORES, scores.failure or f"no scores are available for {scores.engine}; "
                       f"{_PIN.format(engine=scores.engine)}")


def _bounds(scores: ScoreSet) -> tuple[float, float]:
    if scores.scale is not None:
        return scores.scale
    if not scores.models:
        raise _no_scores(scores)
    values = [model.score for model in scores.models]
    return min(values), max(values)


def required(scores: ScoreSet, step: int) -> float:
    """The score a step needs; the ends are exact, so the top scorer always clears the top step."""
    lowest, highest = _bounds(scores)
    return highest if step >= TOP_STEP else lowest + step / TOP_STEP * (highest - lowest)


def _reaches(scores: ScoreSet, value: float, step: int, slack: float = 0.0) -> bool:
    """Whether a score reaches a step. Step 0 asks nothing, even of an old model below the fixed scale."""
    return step <= 0 or value >= required(scores, step) - slack


def _floor_unmet(scores: ScoreSet, floor_step: int) -> FloorUnmet:
    tier = TIERS[floor_step]
    return FloorUnmet(UNRESOLVED, f"no model the {scores.engine} CLI lists and allowed_models permits reaches the {tier} "
                      f"floor, which needs a score of {required(scores, floor_step):.1f}, so nothing was launched or "
                      f"spent. Widen [engines.{scores.engine}] allowed_models in .oah/config.toml, or update the CLI if "
                      "it lists no stronger model; `oah models` shows each model's score")


def _price(model: Scored) -> tuple[bool, float]:
    return model.cost is None, model.cost or 0.0


def _value(model: Scored) -> float:
    """Score per cost: an unknown cost never wins on value, and a free model always does."""
    if model.cost is None:
        return -1.0
    return model.score / model.cost if model.cost > 0 else math.inf


def _pick(scores: ScoreSet, step: int, floor_step: int, profile: str,
          usable: Callable[[str], bool] | None = None) -> Choice:
    if not scores.models:
        raise _no_scores(scores)
    lowest, highest = _bounds(scores)
    efficient = profile == "efficient"
    base = max(step - 1, floor_step) if efficient else step
    slack = 0.0 if profile == "performance" else MARGIN * (highest - lowest)
    meets_floor = [model for model in scores.models if _reaches(scores, model.score, floor_step)]
    if not meets_floor:
        raise _floor_unmet(scores, floor_step)
    meets_floor = [model for model in meets_floor if usable is None or usable(model.model)]
    if not meets_floor:
        raise FloorUnmet(UNRESOLVED, f"no model reaching the {TIERS[floor_step]} floor is runnable; nothing was "
                         f"launched or spent. Change [engines.{scores.engine}.tiers] effort or "
                         f"[engines.{scores.engine}.effort_caps] in .oah/config.toml, or widen allowed_models; "
                         "`oah models` shows why each model was left out")
    fits = [model for model in meets_floor if _reaches(scores, model.score, base, slack)]
    if not fits:
        best = min(meets_floor, key=lambda model: (-model.score, *_price(model), model.model))
    elif efficient:
        best = min(fits, key=lambda model: (-_value(model), model.score, *_price(model), model.model))
    else:
        best = min(fits, key=lambda model: (*_price(model), model.score, model.model))
    return Choice(best.model, step, floor_step, required(scores, base), scores.source, base)


def choose(scores: ScoreSet, tier: str, floor_tier: str, profile: str, *,
           usable: Callable[[str], bool] | None = None) -> Choice:
    """The automatic model for a tier: the model the tier, floor and profile lead to; `usable` is the effort rules."""
    step = min(max(TIERS.index(tier) + PROFILES[profile], 0), TOP_STEP)
    floor_step = TIERS.index(floor_tier)
    return _pick(scores, max(step, floor_step), floor_step, profile, usable)


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
    return value is not None and _reaches(scores, value, step)


def band(scores: ScoreSet, choice: Choice) -> tuple[str, ...]:
    """What a judge may pick: the rule's choice, then the models from the lowest allowed step's score to the next's."""
    low = required(scores, choice.lowest_step)
    high = required(scores, choice.lowest_step + 1) if choice.lowest_step < TOP_STEP else math.inf
    return (choice.model, *(model.model for model in scores.models
                            if low <= model.score <= high and model.model != choice.model))
