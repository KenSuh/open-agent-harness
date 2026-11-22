# 엔진 호출 핵심 — 단계·난이도로 엔진·단·모델·effort 를 정하고, 브리프와 실행 폴더를 만든 뒤 proc 로 띄우거나 백그라운드로 넘기며, 실제로 돈 값을 기록한다.
"""`oah call` and `oah wait` without the command line (PLAN 5.0, 5.5, A1, A6, A10, A14).

`prepare` decides everything before launch and writes the run folder:
- the stage's engine (`OAH_STAGE_ENGINE_MISMATCH` unless overridden, which is logged as an event);
- the tier: `--tier` (source `flag`), else the packet's `## Plan` line for this stage (`packet`), else the
  rule-scored axes through `routing.recommend` (`rules`, or `flag` when axes were given), lifted to a review's
  floor. Unless the tier was given (`--tier`, or a plan tier the user edited away from the rule tier
  `oah packet new` recorded beside it), Jev then judges the accepted Intent and the Scope paths (`judge_difficulty`,
  after the budget gate): at or above `[rerank] difficulty_min_confidence` its tier runs (`jev`), raised to the
  call's floor tier when lower; otherwise the rule tier runs. A review's floor stays scored from its scope alone.
  The record keeps `rule_tier` and the `jev_*` fields beside `tier`, and `tier_floor_raised` says whether a
  floor (the review floor on the rules path, or the floor under Jev's tier) raised it; a correction asks Jev once;
- the model through the catalog and the scores read once (`check_catalog`, `models.map_tier`), and the launch
  effort through the adapter;
- the judged model and effort (`judge_engine`, amendment 6): unless a tier model is explicitly pinned,
  an enabled re-ranker with a key picks one pair from
  `models.build_candidates`; the pair is used only when `CandidateSet.reject_reason` accepts it, and
  the record keeps `model_judge` (`used`, `rejected(<rule>)` or `unused(<reason>)`), the judge's
  `model_judge_confidence` (null when its answer carried none) and `model_choice`
  (with the candidate `mode`, the pool or allowed models left out and why, and why the model is not judged);
- the one admission check (`admit`) on the final model and effort. Every launch path passes it here: foreground,
  background, Orca, review and fix all prepare through this function, and a queued run replays the admitted
  record. Selection, the judge and `admit` run before the reservation or a review's round start, so a
  refusal has spent nothing and there is nothing to release. Everything after them keeps its reservation;
- the brief (`briefs.build`) and the argv; a review is always read-only.
`execute` refuses a Codex header model other than the launched one (`OAH_MODEL_MISMATCH`), like the
effort check, and holds the lease lock through process creation and uses `oah.engines.proc`'s job/tree handling
(prompt on stdin, the process tree killed on timeout), recording the exit code, logs, header check and
inference normalization. It waits in `POLL_SECONDS` slices, so Ctrl-C is handled while the worker runs on
every OS; an interruption stops the worker's process tree and records the run as failed
(`OAH_RUN_INTERRUPTED`) before it propagates. `start_background`
hands the same prepared run to a detached Python runner and returns at once; `wait` polls the record
for at most `WAIT_LIMIT_S` seconds and can be called again.

A task must have a lease bound to an intent (`oah start --packet`) and dispatches only while that binding is
`current` (`OAH_INTENT_NOT_CURRENT` otherwise), and only from the bound source. Every brief carries the
skills `oah.skillsel.choose` selected for it, with a reason each and the `rerank=` status; the selection is
also kept as `skills.json` in the run folder. `lease.json` carries the checked lease identity, generation
and binding, so queued runners refuse any intervening lease change before launching the engine.

The first dispatch of a task that has a packet records its intent source in
`runs/<task>/intent-binding.json`. Later rounds reuse that source; a different source, another Intent-ID,
an in-place edit or an older revision refuses with `OAH_INTENT_SOURCE_MISMATCH`, and only a raised Revision
of the same Intent-ID in the same source moves the binding forward. `resolve_worktree` turns a path,
branch or worktree name into exactly one worktree before any intent is read.

A task that follows a design (`oah.design.binding`) is judged from the lease's worktree before the budget gate:
initial and correction stages need the design `current`, a review stage (read-only) also runs while a newer draft
is unresolved. The brief carries `## Design context` with every part of the accepted revision and of a newer
draft, or nothing launches (`OAH_DESIGN_CONTEXT_TOO_LARGE`); the run folder keeps `design.json`.
`dispatch_guard` judges again at the launch point and refuses `OAH_DESIGN_CHANGED` when the judgement moved, and
`execute` records a change made while the engine ran as the warning `OAH_DESIGN_CHANGED_DURING_RUN` and in
`design-after.json`; the next checkpoint (finish, review, call, land) refuses until it is carried or rebound.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import importlib
import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import TYPE_CHECKING, Callable, Collection, Mapping, Sequence

if TYPE_CHECKING:
    from oah.capabilities.plan import FrozenInputs
    from oah.discovery.preparation import PreparedE, Record as ERecord
    from oah.skillpack import Admitted as SkillAdmission

import oah.intent as oah_intent
import oah.packet as oah_packet
from oah import benchmarks, briefs, launchers, models, routing, runs, scoring, skillsel
from oah.atomic import StorageError
from oah.config import Config
from oah.discovery import boundaries, inputs as input_files, records
from oah.engines import claude, codex
from oah.engines.effort import (ORDINARY, EngineError, HeaderCheck, compare_labels, load_rules,
                                verify_inference)
from oah.engines.proc import run_command

WORKER_ENV = ("OAH_WORKER", "OAH_RUN_ID", "OAH_TASK")
ISOLATED_CODEX_FLAGS = ("--ignore-user-config", "--ignore-rules")  # review rounds (PLAN 5.9 step 6)
WAIT_LIMIT_S = 540.0
POLL_SECONDS = 0.5  # Windows delivers Ctrl-C to Python only between waits, so a running worker is polled
_CORRECTION_CLEANUP = ContextVar("oah_correction_cleanup", default=False)
_CORRECTION_INPUT_CHECK = ContextVar("oah_correction_input_check", default=None)
_CORRECTION_INPUTS = ContextVar("oah_correction_inputs", default=None)
_PREPARATION_RUN = ContextVar("oah_preparation_run", default=None)
_PREPARATION_START = ContextVar("oah_preparation_start", default=None)  # `oah review` starts its round after admission
_CORRECTION_STATE_LOCK = ContextVar("oah_correction_state_lock", default=None)
_CORRECTION_PUBLICATION = ContextVar("oah_correction_publication", default=None)
_REFERENCE_RUN = ContextVar("oah_reference_run", default=None)  # the ORIGINAL runner's ReferenceRun, if any
_DESIGN_RUN = ContextVar("oah_design_run", default=False)  # design-bound: stop owned descendants on every return
DEFAULT_TASK = "adhoc"
MODEL_MISMATCH = "OAH_MODEL_MISMATCH"
BELOW_FLOOR = "OAH_MODEL_BELOW_FLOOR"
NOT_LISTED = "OAH_MODEL_NOT_LISTED"
SUPERSEDED = "OAH_MODEL_SUPERSEDED"
FLOOR_UNCHECKED = "OAH_MODEL_FLOOR_UNCHECKED"
STAGE_DEFAULTS = routing.STAGE_DEFAULTS
TIMEOUTS = {"low": 15, "medium": 30, "high": 60, "max": 90}
ENGINE_DEFAULTS = {
    "claude": {
        "command": ["claude"],
        "quality_order": [],
        "allowed_models": [],
        "effort_caps": {},
        "tier_pools": {},
        "permission_mode": claude.DEFAULT_PERMISSION_MODE,
        "tools": list(claude.DEFAULT_TOOLS),
        "tiers": {tier: {"model": "", "effort": tier, "timeout_min": TIMEOUTS[tier]} for tier in routing.TIERS},
    },
    "codex": {
        "command": ["codex"],
        "quality_order": [],
        "allowed_models": [],
        "effort_caps": {},
        "tier_pools": {},
        "tiers": {tier: {"model": "", "effort": "highest" if tier == "max" else tier,
                         "timeout_min": TIMEOUTS[tier]} for tier in routing.TIERS},
    },
}
_PLAN_LINE = re.compile(r"^\s*(?:[-*]\s*)?`?(initial|review|correction)`?\s*(?:→|->|:)\s*"
                        rf"`?({'|'.join(routing.STAGE_OWNERS)})`?\s*"
                        r"(?:→|->|:)\s*`?(low|medium|high|max)`?\b")
_PLAN_AXES = re.compile(r"^\s*[-*]\s*(initial|review|correction):\s+kind=(\S+)\s+intensity=(\S+)\s+"
                        r"reasoning=(\S+)\s+risk=(\S+)(?:\s|$)")
# The rule tier `oah packet new` wrote beside the axes: `- <stage>: kind=… → <tier> (advice=…)`.
_PLAN_RULE = re.compile(r"^\s*[-*]\s*(initial|review|correction):\s+kind=\S+\s+intensity=\S+\s+reasoning=\S+\s+"
                        r"risk=\S+\s+(?:→|->)\s*(low|medium|high|max)\s+\(advice=")
TIER_GIVEN = "unused(tier-given)"
SECRET_HELD = "OAH_RERANK_SECRET"  # this name ends in neither KEY nor SECRET, which the privacy scan flags


@dataclasses.dataclass(frozen=True)
class Settings:
    stages: Mapping[str, str]
    engines: dict
    models: dict

    @classmethod
    def defaults(cls) -> "Settings":
        return cls.validated(dict(STAGE_DEFAULTS), json.loads(json.dumps(ENGINE_DEFAULTS)), dict(models.DEFAULTS))

    @classmethod
    def from_config(cls, config: Config) -> Settings:
        """Read task settings from the config snapshot the caller has already selected."""
        return cls.validated(routing.stage_settings(config), config.section("engines", ENGINE_DEFAULTS),
                             config.section("models", models.DEFAULTS))

    @classmethod
    def validated(cls, stages: Mapping[str, str], engines: dict, models_section: dict) -> "Settings":
        def invalid(message: str) -> EngineError:
            return EngineError("OAH_CONFIG_INVALID", f".oah/config.toml: {message}")

        for stage, engine in stages.items():
            if engine not in routing.STAGE_OWNERS:
                raise invalid(f"stages.{stage} = {engine!r}; use one of: {', '.join(routing.STAGE_OWNERS)}")
        for engine, section in engines.items():
            if not all(isinstance(item, str) for item in section["quality_order"]):
                raise invalid(f"engines.{engine}.quality_order must list model names as strings")
            if not all(isinstance(item, str) and item for item in section["allowed_models"]):
                raise invalid(f"engines.{engine}.allowed_models must list model names as strings, or be empty "
                              "to allow every model the CLI lists")
            for tier, names in section["tier_pools"].items():
                if tier not in routing.TIERS:
                    raise invalid(f"engines.{engine}.tier_pools.{tier} is not a tier; use one of: "
                                  f"{', '.join(routing.TIERS)}")
                if not isinstance(names, list) or not all(isinstance(item, str) and item for item in names):
                    raise invalid(f"engines.{engine}.tier_pools.{tier} must list model names as strings, such as "
                                  f'{tier} = ["<model>"], or be empty')
            for model, cap in section["effort_caps"].items():
                if cap not in ORDINARY:
                    raise invalid(f"engines.{engine}.effort_caps.{model} = {cap!r} is not an effort level; use one "
                                  f"of: {', '.join(ORDINARY)}, or remove the entry")
            for tier, values in section["tiers"].items():
                if values["timeout_min"] <= 0:
                    raise invalid(f"engines.{engine}.tiers.{tier}.timeout_min must be a positive number of minutes")
        if models_section["ttl_hours"] <= 0:
            raise invalid("models.ttl_hours must be a positive number of hours")
        if models_section["profile"] not in scoring.PROFILES:
            raise invalid(f"models.profile = {models_section['profile']!r}; use one of: {', '.join(scoring.PROFILES)}")
        if type(models_section["fetch_scores"]) is not bool:
            raise invalid("models.fetch_scores must be true or false")
        return cls(stages=stages, engines=engines, models=models_section)


@dataclasses.dataclass(frozen=True)
class Resolution:
    engine: str
    tier: str
    base: tuple[str, ...]
    cli_version: str | None
    requested_model: str
    model: str
    model_source: str
    requested_effort: str
    launch_effort: str
    timeout_s: float
    warnings: tuple[str, ...]
    changes: tuple[dict, ...]


@dataclasses.dataclass(frozen=True)
class Admission:
    """What the admission check found; a run and a review receipt keep it."""

    family: str | None
    floor_tier: str
    floor_checked: bool
    profile: str
    score_source: str | None
    score_date: str | None
    score_credit: str | None
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {name: value for name, value in dataclasses.asdict(self).items() if name != "warnings"}


class _ScorePath(type(pathlib.Path())):
    """Publication compares the same bounded score bytes that decided the correction's choice."""

    def read_bytes(self):
        return models._score_bytes(self)


class _CorrectionCatalog(models.Catalog):
    """Resolve against captured bytes; start publication compares them before writing the plan.

    A stored score file is captured when the choice first reads it, so with `[models] fetch_scores = false` it is
    no input. `oah.benchmarks` checks the bytes as on any read, and a correction never fetches."""

    def __init__(self, state_dir):
        super().__init__(state_dir)
        check = _CORRECTION_INPUT_CHECK.get()
        self.original = {}
        self.values = {}
        self.writes = {}
        self.scores = {}
        for path in (self.catalog_path, self.resolved_path, self.changes_path):
            raw, data = runs._captured_state(path, jsonl=path == self.changes_path, default={})
            text = raw.decode("utf-8")
            check(text)
            check(data)
            self.original[path] = text
            self.values[path] = data

    def load(self, engine):
        data = self.values[self.catalog_path]
        return models.Entry.from_json(engine, data.get(engine)) if isinstance(data, dict) else None

    def score_bytes(self, name):
        path = self.dir / name
        if path not in self.scores:
            raw = super().score_bytes(name)
            self.scores[path] = raw
            if (capture := runs._CORRECTION_CAPTURE.get()) is not None:
                capture[_ScorePath(path)] = raw  # compared at start publication, like the catalog
        return self.scores[path]

    def _save(self, entry):
        data = json.loads(json.dumps(self.values[self.catalog_path]))
        data = data if isinstance(data, dict) else {}
        data[entry.engine] = entry.to_json()
        self._plan_write(self.catalog_path, data)

    def resolved(self):
        data = json.loads(json.dumps(self.values[self.resolved_path]))
        return data if isinstance(data, dict) else {}

    def _plan_write(self, path, data):
        _CORRECTION_INPUT_CHECK.get()(data)
        self.writes[path] = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _plan_changes(self, changes):
        text = self.writes.get(self.changes_path, self.original[self.changes_path]) + "".join(
            json.dumps(change, ensure_ascii=False, sort_keys=True) + "\n" for change in changes)
        _CORRECTION_INPUT_CHECK.get()(text)
        self.writes[self.changes_path] = text

    def record_changes(self, engine, changes, *, now=None):
        stamp = _iso(now or models.utc_now())
        self._plan_changes({"ts": stamp, "engine": engine, "field": "model", **change,
                            "why": "OAH_FIX_MODEL_RESOLUTION"} for change in changes)

    def record_resolution(self, engine, tier, **fields):
        fields["why"] = "OAH_FIX_MODEL_RESOLUTION"
        data = self.resolved()
        old = data.get(engine, {}).get(tier, {}) if isinstance(data.get(engine), dict) else {}
        stamp = _iso(fields.get("now") or models.utc_now())
        changes = [{"ts": stamp, "engine": engine, "tier": tier, "field": field, "old": old.get(field),
                    "new": fields[field], "why": fields["why"]}
                   for field in ("model", "effort") if old.get(field) != fields[field]]
        if changes:
            self._plan_changes(changes)
        if changes or old.get("source") != fields["source"]:
            data.setdefault(engine, {})[tier] = {key: fields[key] for key in ("model", "effort", "source")}
            data[engine][tier]["at"] = stamp
            self._plan_write(self.resolved_path, data)
        return changes


def _catalog(state_dir):
    inputs = _CORRECTION_INPUTS.get()
    return inputs.catalog if inputs is not None else models.Catalog(state_dir)


_READ_FAILURES = (EngineError, ValueError, TypeError, OverflowError, RecursionError)


def read_scores(engine: str, entry: models.Entry | None, settings: Settings, *,
                previous: models.Entry | None = None, data: Mapping | None = None, known: Mapping | None = None,
                before: Mapping | None = None) -> tuple[scoring.ScoreSet, scoring.ScoreSet | None]:
    """The score sets of an entry and of the entry it replaced, from one read of the data.

    `data` and `before` are what `benchmarks.data_for` chose for them, and None is the snapshot shipped with oah.
    A failure is kept in the set, not raised: a pin still runs and only a choice fails."""
    allowed = settings.engines[engine]["allowed_models"]

    def failed(error: Exception) -> scoring.ScoreSet:
        if not isinstance(error, EngineError):
            error = scoring._unusable("benchmarks.json or families.json", "has invalid data")
        return scoring.failed(engine, error)

    try:
        known = scoring.families() if known is None else known
        packaged = scoring.bundled() if data is None or (previous is not None and before is None) else None
    except _READ_FAILURES as exc:
        return failed(exc), None

    def one(item: models.Entry | None, chosen: Mapping | None) -> scoring.ScoreSet:
        try:
            return scoring.score_set(engine, item, allowed=allowed, data=chosen or packaged, known=known)
        except _READ_FAILURES as exc:
            return failed(exc)

    return one(entry, data), one(previous, before) if previous is not None else None


def _families() -> Mapping | None:
    """The packaged family rules, read once per check; None when they cannot be read, which `read_scores` reports."""
    try:
        return scoring.families()
    except _READ_FAILURES:
        return None


def check_catalog(engine: str, settings: Settings, *, state_dir: pathlib.Path, env: Mapping[str, str],
                  refresh: bool = False, now: dt.datetime | None = None, persist: bool = True,
                  session: benchmarks.Session | None = None, keychain=None) -> tuple[list[str], models.Freshness]:
    """The engine's argv prefix, its catalog entry and that entry's scores: the one read of both for a launch.

    A rebuild warns about the listed models in no pool; this is the one place that decides when a
    launch hears of them, so a launch that rebuilt nothing stays quiet. `persist=False` writes nothing.
    A rebuild that may write also refreshes the stored scores (`benchmarks.data_for`); `oah fix` never fetches, so
    a correction decides on the bytes it captured. `session` is the command's view of the stored score files, shared
    when one command checks both engines, and `keychain` is where an Artificial Analysis key may be found."""
    base = models.resolve_base(engine, settings.engines[engine]["command"])
    catalog = _catalog(state_dir)
    known = _families()
    aliases = tuple(known["engines"]["claude"]["aliases"]) if engine == "claude" and known is not None else ()
    freshness = catalog.ensure_fresh(engine, base=base, env=env, ttl_hours=settings.models["ttl_hours"],
                                     refresh=refresh, now=now, persist=persist, aliases=aliases)
    rebuilt = bool(freshness.refreshed) and persist
    previous = freshness.previous if rebuilt else None
    chosen = benchmarks.data_for(engine, freshness.entry, catalog=catalog, settings=settings.models, known=known,
                                 session=session, previous=previous, rebuilt=rebuilt,
                                 fetch=rebuilt and _CORRECTION_INPUTS.get() is None, env=env, keychain=keychain, now=now)
    scores, before = read_scores(engine, freshness.entry, settings, previous=previous, data=chosen.data, known=known,
                                 before=chosen.before)
    notices: list[str] = list(chosen.notices)
    if rebuilt:
        section = settings.engines[engine]
        unpooled = models.unpooled_models(freshness.entry, section["tier_pools"])
        notices += [models.unpooled_warning(engine, name) for name in unpooled]
        notices += models.score_notices(engine, scores)
        if before is not None and models.rule_decides(engine, section, freshness.entry):
            changes = models.model_changes(before, scores, settings.models["profile"], freshness.entry)
            if changes:
                catalog.record_changes(engine, changes, now=now)
            notices += [models.changed_warning(engine, change) for change in changes]
    return base, dataclasses.replace(freshness, warnings=(*freshness.warnings, *notices), scores=scores)


def stored_choices(engine: str, entry: models.Entry, settings: Settings, *, catalog: models.Catalog,
                   session: benchmarks.Session | None = None) -> dict:
    """What `oah models` shows, from a stored entry and the stored score files alone: no CLI runs, nothing is fetched
    and nothing is recorded."""
    known = _families()
    chosen = benchmarks.data_for(engine, entry, catalog=catalog, settings=settings.models, known=known, session=session)
    scores = read_scores(engine, entry, settings, data=chosen.data, known=known)[0]
    freshness = models.Freshness(entry, None, (), scores=scores)
    tiers: dict[str, dict] = {}
    for tier in routing.TIERS:
        try:
            model, source = _selected(engine, tier, settings, freshness, floor_tier=tier)
            _tier_effort(engine, tier, model, source, settings, entry, floor_tier=tier, stage=None)
            tiers[tier] = {"model": _shown(model), "source": source}
        except EngineError as exc:
            tiers[tier] = {"error": str(exc)}
    return {"engine": engine, **models.score_report(scores, settings.models["profile"],
                                                    left_out=_effort_left_out(engine, settings, freshness)),
            "tiers": tiers, "notices": list(chosen.notices)}


def _fresh(freshness: models.Freshness) -> bool:
    entry = freshness.entry
    return bool(entry.models) and entry.checked_at is not None and not any(
        warning.startswith("OAH_MODELS_REFRESH_FAILED") for warning in freshness.warnings)


def _runnable(engine: str, tier: str, floor_tier: str, model: str, settings: Settings,
              entry: models.Entry | None, *, effort: str | None = None) -> models.Runnable:
    section = settings.engines[engine]
    info = (entry.model(model) if entry else None) or models.ModelInfo(model, ())
    return models.runnable(info, effort if effort is not None else section["tiers"][tier]["effort"],
                           section["tiers"][floor_tier]["effort"], section["effort_caps"].get(model))


def _passes_effort_rules(engine: str, tier: str, floor_tier: str, model: str, settings: Settings,
                         entry: models.Entry | None) -> bool:
    """The shared model-specific effort rule, used by automatic selection."""
    return _runnable(engine, tier, floor_tier, model, settings, entry).reason is None


def _effort_left_out(engine: str, settings: Settings, freshness: models.Freshness) -> dict[str, str]:
    """Tier-specific effort exclusions for model reports, without changing the score set or its scale."""
    left_out = {}
    for item in freshness.scores.models:
        reasons = []
        for tier in routing.TIERS:
            result = _runnable(engine, tier, tier, item.model, settings, freshness.entry)
            if result.reason is not None:
                reasons.append(f"{tier}: {result.reason}")
        if reasons:
            left_out[item.model] = f"not-runnable ({', '.join(reasons)})"
    return left_out


def _tier_effort(engine: str, tier: str, model: str, source: str, settings: Settings, entry: models.Entry | None, *,
                 floor_tier: str, stage: str | None) -> models.Runnable:
    """The tier's effort on this model, as every call resolves it, or the refusal a call would give."""
    section = settings.engines[engine]
    effort = _runnable(engine, tier, floor_tier, model, settings, entry)
    if effort.reason is not None:
        code = "OAH_CONFIG_INVALID" if source == "pin" or section["effort_caps"].get(model) is not None else (
            BELOW_FLOOR if stage == "review" else scoring.UNRESOLVED)
        raise EngineError(code, f"{_shown(model)} cannot run effort {section['tiers'][tier]['effort']} at the "
                          f"{floor_tier} floor effort ({effort.reason}); change [engines.{engine}.tiers] effort or "
                          f"[engines.{engine}.effort_caps] in .oah/config.toml")
    return effort


def _selected(engine: str, tier: str, settings: Settings, freshness: models.Freshness, *,
              floor_tier: str = "low") -> tuple[str, str]:
    section = settings.engines[engine]

    def effort_rules(model: str) -> bool:
        return _passes_effort_rules(engine, tier, floor_tier, model, settings, freshness.entry)

    return models.map_tier(engine, tier, pin=section["tiers"][tier]["model"], quality_order=section["quality_order"],
                           entry=freshness.entry, scores=freshness.scores, floor_tier=floor_tier,
                           profile=settings.models["profile"], pools=section["tier_pools"],
                           allowed=section["allowed_models"], effort_rules=effort_rules)


def resolve_engine(engine: str, tier: str, settings: Settings, *, state_dir: pathlib.Path,
                   env: Mapping[str, str], refresh: bool = False, now: dt.datetime | None = None,
                   checked: tuple[list[str], models.Freshness] | None = None, floor_tier: str = "low",
                   record: bool = True, stage: str | None = None) -> Resolution:
    """Model and effort for one engine and tier; the change log records anything that differs from last time."""
    section = settings.engines[engine]
    tier_settings = section["tiers"][tier]
    base, freshness = checked or check_catalog(engine, settings, state_dir=state_dir, env=env, refresh=refresh, now=now)
    catalog = _catalog(state_dir)
    entry = freshness.entry
    check = _CORRECTION_INPUT_CHECK.get()
    if check is not None:
        check(dataclasses.asdict(freshness))
    cap_warnings = models.check_caps(engine, section["effort_caps"], section["allowed_models"], entry,
                                     fresh=_fresh(freshness), pools=section["tier_pools"])
    try:
        model, source = _selected(engine, tier, settings, freshness, floor_tier=floor_tier)
    except scoring.FloorUnmet as exc:
        if stage != "review":
            raise
        raise EngineError(BELOW_FLOOR, f"tier {tier} effort {tier_settings['effort']}: "
                          + str(exc).split(": ", 1)[1]) from None
    cap = section["effort_caps"].get(model)
    effort = _tier_effort(engine, tier, model, source, settings, entry, floor_tier=floor_tier, stage=stage)
    info = entry.model(model) or models.ModelInfo(model, ())
    uncapped = models.runnable(info, tier_settings["effort"], section["tiers"][floor_tier]["effort"], None) if cap else effort
    if effort.launch != uncapped.launch:
        cap_warnings.append(f"OAH_EFFORT_CAPPED: {model} effort {uncapped.launch} is capped to {effort.launch}; "
                            f"raise or remove its [engines.{engine}.effort_caps] entry in .oah/config.toml "
                            "to permit a higher effort")
    why = f"{source} mapping for tier {tier} with {engine} CLI {freshness.launch_version or 'unknown'}"
    if freshness.refreshed:
        why += f"; catalog rebuilt ({freshness.refreshed})"
    changes = catalog.record_resolution(engine, tier, model=model, effort=effort.launch, source=source, why=why,
                                        now=now) if record else []
    return Resolution(engine=engine, tier=tier, base=tuple(base), cli_version=freshness.launch_version,
                      requested_model=tier_settings["model"], model=model, model_source=source,
                      requested_effort=tier_settings["effort"], launch_effort=effort.launch,
                      timeout_s=float(tier_settings["timeout_min"]) * 60,
                      warnings=(*freshness.warnings, *cap_warnings), changes=tuple(changes))


def _resolve_floor(resolution: Resolution, settings: Settings, *, floor_tier: str,
                   freshness: models.Freshness) -> models.Pair:
    """The floor model used only for the user's quality_order ranking and report metadata."""
    engine = resolution.engine
    section = settings.engines[engine]
    # This is ranking metadata, not a launch: caps must not move a user's quality_order floor.
    model, _ = models.map_tier(engine, floor_tier, pin=section["tiers"][floor_tier]["model"],
                               quality_order=section["quality_order"], entry=freshness.entry,
                               scores=freshness.scores, floor_tier=floor_tier, profile=settings.models["profile"],
                               pools=section["tier_pools"], allowed=section["allowed_models"])
    info = freshness.entry.model(model) or models.ModelInfo(model, ())
    requested = section["tiers"][floor_tier]["effort"]
    effort = models.runnable(info, requested, requested, None)
    if effort.reason is not None:
        raise EngineError(scoring.UNRESOLVED, "the floor model is not runnable; change its tier effort or "
                          "effort_caps in .oah/config.toml")
    return models.Pair(model, effort.launch)


def judge_engine(resolution: Resolution, settings: Settings, *, stage: str, axes: routing.Axes, pinned: bool,
                 freshness: models.Freshness, root: pathlib.Path, env: Mapping[str, str], floor_tier: str,
                 brief_disabled: bool = False, keychain=None,
                 state_dir: pathlib.Path | None = None, admitted=None) -> tuple[Resolution, dict]:
    """The call's model and effort after the re-ranker's judgement, and the `model_judge`/`model_choice` record.

    The deterministic resolution is kept unless the judged pair passes `CandidateSet.reject_reason`.
    A plugin failure never blocks a launch: it is recorded as `unused(error: <type>)`. With `state_dir`, the
    re-ranker's breaker there applies; a correction passes none."""
    engine, tier = resolution.engine, resolution.tier
    deterministic = models.Pair(resolution.model, resolution.launch_effort)
    choice = {"deterministic": dict(deterministic.to_json(), source=resolution.model_source), "floor": None,
              "mode": None, "note": None, "candidates": [], "excluded": {}, "proposal": None, "provider": None,
              "requested": deterministic.to_json()}

    def record(status: str, chosen: Resolution = resolution,
               confidence: float | None = None) -> tuple[Resolution, dict]:
        choice["requested"] = {"model": chosen.model, "effort": chosen.launch_effort}
        check = _CORRECTION_INPUT_CHECK.get()
        if check is not None:
            check(choice)
        return chosen, {"model_judge": status, "model_judge_confidence": confidence, "model_choice": choice}

    section = settings.engines[engine]
    if admitted is not None:
        from oah import skillpack

        skillpack.require(admitted)
        return record("unused(target-profile)")
    if pinned:
        return record("unused(pinned)")
    try:
        floor = _resolve_floor(resolution, settings, floor_tier=floor_tier, freshness=freshness)
    except EngineError:
        return record("unused(floor-unresolved)")

    def review_rule(model: str, effort: str) -> str | None:
        proposed = dataclasses.replace(resolution, model=model, model_source="judge", launch_effort=effort)
        try:
            _check_admission(proposed, settings, stage=stage, tier=tier, floor_tier=floor_tier, freshness=freshness)
        except EngineError:
            return "below-floor"
        return None

    candidates = models.build_candidates(
        engine, entry=freshness.entry, deterministic=deterministic, tier=tier, floor_tier=floor_tier,
        floor=floor, quality_order=section["quality_order"],
        allowed_models=section["allowed_models"], effort_caps=section["effort_caps"],
        tier_pools=section["tier_pools"], scores=freshness.scores, profile=settings.models["profile"],
        tier_effort=section["tiers"][tier]["effort"], floor_effort=section["tiers"][floor_tier]["effort"],
        admission_rule=review_rule if stage == "review" else None)
    choice["floor"] = {"tier": floor_tier, **floor.to_json()}
    choice["candidates"] = [[pair.model, pair.effort] for pair in candidates.pairs]
    choice.update(mode=candidates.mode, note=candidates.note, excluded=dict(candidates.excluded))
    if section["tier_pools"] and any(not section["tier_pools"].get(t) for t in (floor_tier, tier)):
        return record("unused(no-pool-for-tier)")
    try:
        rerank = importlib.import_module("oah.rerank")
        inputs = _CORRECTION_INPUTS.get()
        source = inputs.config if inputs is not None else None
        if source is None:
            from oah.config import load_config

            source = load_config(root)
        judged = rerank.judge_model({"stage": stage, "tier": tier, "axes": axes.to_json()},
                                    [(pair.model, pair.effort) for pair in candidates.pairs],
                                    config=source.section("rerank", rerank.DEFAULTS), brief_disabled=brief_disabled,
                                    env=env, keychain=keychain, state_dir=state_dir)
    except Exception as exc:  # a plugin failure must never block a launch
        return record(f"unused(error: {type(exc).__name__})")
    choice["provider"] = judged.provider
    if judged.status != "used" or judged.proposal is None:
        return record(judged.status)
    model, effort = judged.proposal
    choice["proposal"] = {"model": model, "effort": effort}
    rule = candidates.reject_reason(model, effort)
    if rule is not None:
        return record(f"rejected({rule})", confidence=judged.confidence)
    return record("used", dataclasses.replace(resolution, model=model, model_source="judge", launch_effort=effort),
                  confidence=judged.confidence)


def _shown(model: str) -> str:
    """A model name for a message: only a plain name is echoed, since a pin is text from a config file."""
    return model if models.PLAIN_MODEL.fullmatch(model) else "this model"


def _call_floor(stage: str | None, scope: Sequence[str], kind: str) -> str:
    """The floor tier of a call. A review's comes from its scope alone, so no caller input can lower it."""
    return routing.review_floor(scope, kind) if stage == "review" else routing.floor_tier(kind)


def _launch_floor(stage: str, tier: str, floor_tier: str) -> str:
    return floor_tier if stage == "review" else min(tier, floor_tier, key=routing.TIERS.index)


def _review_floor_checked(resolution: Resolution, settings: Settings, *, tier: str, floor_tier: str,
                          freshness: models.Freshness, warnings: list[str], comparable: bool) -> bool:
    """Refuse a review below its floor; False when the user's named model leaves nothing to check against."""
    engine, model, effort = resolution.engine, resolution.model, resolution.launch_effort
    scores = freshness.scores
    next_step = (f"Nothing was launched or spent. Use a tier of {floor_tier} or higher, or change the pin, pool, "
                 "quality_order or allowed_models in .oah/config.toml; `oah models` shows each model's score")
    if routing.TIERS.index(tier) < routing.TIERS.index(floor_tier):
        raise EngineError(BELOW_FLOOR, f"tier {tier} is below the {floor_tier} floor of this review. {next_step}")
    step = routing.TIERS.index(floor_tier)
    value = scoring.score_of(scores, model)
    if value is not None and not scoring.clears(scores, model, step):
        raise EngineError(BELOW_FLOOR, f"{_shown(model)} scores {value:.1f}, below the {scoring.required(scores, step):.1f} "
                          f"the {floor_tier} floor of this review needs. {next_step}")
    if value is None or not comparable:
        warnings.append(f"{FLOOR_UNCHECKED}: {_shown(model)} has no score or an effort oah cannot rank, so the "
                        f"{floor_tier} floor of this review was not checked; the receipt says so. Run `oah models` to "
                        "see the scored models, or name one in .oah/config.toml")
        return False
    return True


def preview(engine: str, stage: str | None, tier: str, kind: str, settings: Settings, *, state_dir: pathlib.Path,
            env: Mapping[str, str]) -> tuple[dict, tuple[str, ...]]:
    """What a call at this tier would use, through the same selection and `admit`; it launches and writes nothing."""
    routing.require_runtime(engine, stage)
    floor_tier = _call_floor(stage, (), kind)
    if stage == "review" and routing.TIERS.index(tier) < routing.TIERS.index(floor_tier):
        tier = floor_tier
    checked = check_catalog(engine, settings, state_dir=state_dir, env=env, persist=False)
    resolution = resolve_engine(engine, tier, settings, state_dir=state_dir, env=env, checked=checked,
                                floor_tier=floor_tier, record=False, stage=stage)
    admitted = admit(resolution, settings, stage=stage, tier=tier, floor_tier=floor_tier, freshness=checked[1])
    return ({"engine": engine, "stage": stage, "tier": tier, "model": _shown(resolution.model),
             "effort": resolution.launch_effort, "source": resolution.model_source, **admitted.to_json()},
            (*resolution.warnings, *admitted.warnings))


def admit(resolution: Resolution, settings: Settings, *, stage: str | None, tier: str, floor_tier: str,
          freshness: models.Freshness) -> Admission:
    """The one check on the final model and effort; it reads nothing, on the entry and scores of this call.

    The rule's pair is checked before any spend, and the final pair again after the paid judge.
    A selection that cannot start at all has
    already raised its named failure in `models.map_tier` (no catalog, no scores, an empty pool, a floor no
    allowed model reaches). A floor is checked on the model's own score on the fixed scale, so a pin of a superseded,
    not-allowed or unlisted model cannot get under it."""
    return _check_admission(resolution, settings, stage=stage, tier=tier, floor_tier=floor_tier, freshness=freshness)


def _check_admission(resolution: Resolution, settings: Settings, *, stage: str | None, tier: str, floor_tier: str,
                     freshness: models.Freshness) -> Admission:
    """Shared, read-free checks for admission, judge candidates and proposals, on each model's own effort."""
    engine, model = resolution.engine, resolution.model
    scores = freshness.scores
    warnings: list[str] = []
    # The Claude CLI accepts full names it does not print, and a hidden Codex model is still one it knows.
    if (engine == "codex" and resolution.model_source == "pin" and _fresh(freshness)
            and freshness.entry.model(model) is None):
        raise EngineError(NOT_LISTED, f"the codex CLI no longer lists {_shown(model)}, the model pinned for this tier, "
                          "so nothing was launched or spent. Run `oah models` to see the listed models, then change "
                          "the pin in .oah/config.toml")
    effort = _runnable(engine, tier, floor_tier, model, settings, freshness.entry,
                       effort=resolution.launch_effort)
    if effort.reason is not None or effort.launch != resolution.launch_effort:
        code = "OAH_CONFIG_INVALID" if resolution.model_source == "pin" else (
            BELOW_FLOOR if stage == "review" else scoring.UNRESOLVED)
        raise EngineError(code, f"{_shown(model)} effort {resolution.launch_effort} cannot meet the {floor_tier} "
                          f"floor effort or effort_caps ({effort.reason or 'above-cap'}); change "
                          f"[engines.{engine}.tiers] effort or [engines.{engine}.effort_caps] in .oah/config.toml")
    if scores.left_out.get(model) == "superseded":
        old = scoring.scored(scores, model)
        family = next((item for item in scores.models if old is not None and item.family == old.family), None)
        newer = f" {family.model} is the newer generation." if family is not None else ""
        warnings.append(f"{SUPERSEDED}: {_shown(model)} was named in .oah/config.toml, so it runs, but it is superseded."
                        f"{newer} Run `oah models`, and change that line when you choose to")
    checked = stage == "review" and _review_floor_checked(resolution, settings, tier=tier, floor_tier=floor_tier,
                                                          freshness=freshness, warnings=warnings,
                                                          comparable=effort.comparable)
    found = scoring.scored(scores, model)
    return Admission(found.family if found else None, floor_tier, checked, settings.models["profile"],
                     scores.source or None, scores.date or None, scores.credit or None, tuple(warnings))


@dataclasses.dataclass(frozen=True)
class Packet:
    source: str  # repository-relative, with forward slashes
    intent: object | None  # has .section, .id, .revision, .digest
    section: str | None  # the intent section exactly as the source holds it
    scope: tuple[str, ...]
    plan: dict  # stage -> (engine, tier)
    rerank_disabled: bool = False  # the packet's Constraints turn the re-ranker off
    axes: dict[str, routing.Axes] = dataclasses.field(default_factory=dict)
    planned: dict[str, str] = dataclasses.field(default_factory=dict)  # stage -> rule tier `oah packet new` recorded
    design: object | None = None  # oah.design.binding.PacketDesign: the packet's ## Design section


def _section_lines(text: str, heading: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            inside = re.match(rf"## {re.escape(heading)}\b", line) is not None
            continue
        if inside:
            out.append(line)
    return out


def _read_text(path: pathlib.Path) -> str:
    text = input_files.read_bytes(path).decode("utf-8")  # bytes, so CRLF stays byte-intact
    return text[1:] if text.startswith("\ufeff") else text


def read_packet(root: pathlib.Path, path: pathlib.Path, *, parse_intent: Callable[[str], object] | None,
                read_source: Callable[[pathlib.Path], str] | None = None, validate_plan: bool = True) -> Packet:
    """The parts of a packet a call needs. oah.intent (lane P4a) reads the source safely and parses it."""
    try:
        text = input_files.source_text(path, read_source or _read_text)
    except (OSError, oah_intent.SourceError, ValueError) as exc:
        code = getattr(exc, "code", "OAH_PACKET_UNREADABLE")
        message = str(exc).split(": ", 1)[1] if str(exc).startswith(f"{code}: ") else str(exc)
        raise EngineError(code, f"{message}; check the path given with --packet") from exc
    try:
        intent = parse_intent(text) if parse_intent is not None else None
    except (oah_intent.IntentInvalid, ValueError, TypeError) as exc:
        raise EngineError("OAH_PACKET_INTENT_INVALID", f"{path}: {exc}; fix the packet's OAH_INTENT section "
                          "(`oah packet check` lists the problems)") from exc
    scope = []
    for line in _section_lines(text, "Scope"):
        match = re.match(r"\s*[-*]\s+`?([^`\s]+)`?", line)
        if match:
            scope.append(match.group(1))
    plan: dict = {}
    axes: dict[str, routing.Axes] = {}
    planned: dict[str, str] = {}
    for line in _section_lines(text, "Plan"):
        match = _PLAN_LINE.match(line)
        if match:
            plan[match.group(1)] = (match.group(2), match.group(3))
        recorded = _PLAN_AXES.match(line)
        if recorded:
            values = routing.Axes(*recorded.groups()[1:])
            try:
                routing.recommend(values)
            except routing.RoutingError:
                if validate_plan:
                    raise
                continue
            axes[recorded.group(1)] = values
        rule = _PLAN_RULE.match(line)
        if rule:
            planned.setdefault(rule.group(1), rule.group(2))
    try:
        source = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        source = str(path)
    section = briefs.raw_section(text) if intent is not None else None
    from oah.design.binding import SectionInvalid, parse_section

    try:
        design = parse_section(text)
    except SectionInvalid as exc:
        raise EngineError(exc.code, f"{path}: {str(exc).split(': ', 1)[1]}") from None
    return Packet(source=source, intent=intent, section=section, scope=tuple(scope), plan=plan,
                  rerank_disabled=oah_packet.rerank_disabled(text), axes=axes, planned=planned, design=design)


BINDING_NAME = "intent-binding.json"
SOURCE_MISMATCH = "OAH_INTENT_SOURCE_MISMATCH"


def _binding_path(state_dir: pathlib.Path, task: str) -> pathlib.Path:
    runs.task_directory(state_dir, task)
    return records.location(state_dir / "runs" / task / BINDING_NAME)


def dispatch_binding(state_dir: pathlib.Path, task: str) -> dict | None:
    """The intent source bound at the task's first dispatch, or None."""
    path = _binding_path(state_dir, task)
    data = (records.captured_json(path, runs._captured_state, parse=_parse_dispatch_binding)
            if runs._CORRECTION_CAPTURE.get() is not None
            else records.read_json(path, required=False, parse=_parse_dispatch_binding))
    return data if isinstance(data, dict) else None


def _parse_dispatch_binding(data):
    if (not isinstance(data.get("source"), str) or not data["source"]
            or not isinstance(data.get("id"), str) or not oah_intent.ID_RE.fullmatch(data["id"])
            or type(data.get("revision")) is not int or data["revision"] < 1
            or not isinstance(data.get("digest"), str) or not oah_intent.DIGEST_RE.fullmatch(data["digest"])
            or not isinstance(data.get("history", []), list)):
        raise ValueError("the dispatch binding needs its source, id, revision, digest and history")
    return data


def _parse_lease_snapshot(saved):
    if (not isinstance(saved.get("id"), str) or not saved["id"] or type(saved.get("generation")) is not int
            or saved["generation"] < 0):
        raise ValueError("the lease snapshot needs its id and generation")
    oah_intent.Binding.from_json(saved.get("intent"))
    return saved


def check_dispatch_binding(bound: dict | None, packet: Packet, task: str) -> bool:
    """Refuse a swapped intent; True when this dispatch moves the binding (first dispatch or a new revision)."""
    intent = packet.intent
    if intent is None:
        return False
    if bound is None:
        return True
    fix = (f"To change the goal, raise Revision of Intent-ID {bound['id']} in {bound['source']} and accept it again; "
           "never point a task at a different intent source")
    line = f"INTENT {bound['id']}@r{bound['revision']} {bound['digest']}"
    if packet.source != bound["source"]:
        raise EngineError(SOURCE_MISMATCH, f"task {task} was first dispatched with intent source {bound['source']} "
                          f"({line}); this dispatch reads {packet.source}. {fix}. To continue, run the command again "
                          f"with --packet {briefs.shell_word(bound['source'])} or without --packet")
    if intent.id != bound["id"]:
        raise EngineError(SOURCE_MISMATCH, f"{packet.source} now holds Intent-ID {intent.id}, but task {task} is bound "
                          f"to {line}. {fix}")
    if intent.revision < bound["revision"] or (intent.revision == bound["revision"] and intent.digest != bound["digest"]):
        what = "an older revision" if intent.revision < bound["revision"] else "the same revision with edited content"
        raise EngineError(SOURCE_MISMATCH, f"{packet.source} holds {what} (INTENT {intent.id}@r{intent.revision} "
                          f"{intent.digest}) than the binding {line}. {fix}")
    return intent.revision > bound["revision"]


def record_dispatch_binding(state_dir: pathlib.Path, packet: Packet, task: str, run_id: str, *,
                            checked_lease=None, root: pathlib.Path | None = None) -> None:
    """Bind (or advance) the task's intent source under the state lock, re-checking against a racing dispatch."""
    from oah.atomic import write_json_atomic
    from oah.lease import LeaseError, Store

    checked_lease = checked_lease or bound_lease(state_dir, task)
    if checked_lease is None:
        raise EngineError("OAH_LEASE_CHANGED", "the checked lease was released; inspect oah status and "
                          "run oah call again")
    try:
        with Store(state_dir).checked(task=task, lease_id=checked_lease.id,
                                      generation=checked_lease.generation) as live:
            from oah.discovery.runfiles import Run

            run = Run(id=run_id, task=task, path=runs._contained_path(state_dir, "runs", task, run_id))
            _check_dispatch_lease(live, root or pathlib.Path(live.worktree), stage=run.read()["stage"])
            bound = dispatch_binding(state_dir, task)
            if check_dispatch_binding(bound, packet, task):
                intent = packet.intent
                history = list(bound.get("history", [])) if bound else []
                history.append({"revision": intent.revision, "digest": intent.digest, "run_id": run_id})
                write_json_atomic(_binding_path(state_dir, task),
                                  {"source": packet.source, "id": intent.id, "revision": intent.revision,
                                   "digest": intent.digest, "history": history})
            write_json_atomic(runs._contained_path(state_dir, "runs", task, run_id, "lease.json"),
                              {"id": live.id, "generation": live.generation, "intent": live.intent})
    except LeaseError as exc:
        raise EngineError(exc.code, str(exc).split(": ", 1)[1]) from None


def bound_lease(state_dir: pathlib.Path, task: str):
    """The task's active lease when it is bound to an intent (`oah start --packet`), else None."""
    from oah.lease import Store

    for lease, stale in Store(state_dir).snapshot():
        if lease.task == task and lease.intent is not None and not stale:
            return lease
    return None


def check_lease_binding(lease) -> None:
    """Refuse to dispatch for a bound task whose intent is not current (PLAN 5.6, A7)."""
    binding = lease.binding()
    state, reason = oah_intent.explain_binding(binding, pathlib.Path(lease.worktree))
    if state != "current":
        raise EngineError("OAH_INTENT_NOT_CURRENT",
                          f"task {lease.task} is bound to {binding.line()} and that intent is {state}: {reason}. "
                          f"Nothing was launched. Restore {binding.source} as it was bound, or raise its Revision, "
                          f"accept it and rebind with oah start --task {lease.task} --agent {lease.agent} --packet "
                          f"{binding.source}; then call again")


def _check_dispatch_lease(lease, root: pathlib.Path, *, stage: str) -> None:
    from oah.lease import utc_now

    if lease.expired(utc_now()):
        raise EngineError("OAH_LEASE_CHANGED", "the checked lease expired; start a new lease with "
                          "oah start --packet and run oah call again")
    if stage != "review":
        try:
            same_worktree = os.path.samefile(root, lease.worktree)
        except OSError:
            same_worktree = False
        if not same_worktree:
            raise EngineError("OAH_WORKTREE_LEASE_MISMATCH", "a mutating worker must run in the dispatcher's leased "
                              f"worktree ({lease.worktree}). Nothing was launched. Run oah call again with "
                              "--worktree set to that exact path")
    inputs = _CORRECTION_INPUTS.get()
    if inputs is not None:
        check_lease_binding(lease)
        _check_lease_source(lease.binding(), inputs.packet, lease.task)
        return
    check_lease_binding(lease)
    # A read-only review may use another tree; its intent must still match the lease.
    check_lease_binding(dataclasses.replace(lease, worktree=str(root)))


@contextmanager
def _paid_dispatch(prepared, *, record=True):
    """Serialize ordinary paid starts with reviews; managed review/fix already owns this lock."""
    from oah.lockfile import LockTimeout, StateLock
    from oah.review import receipts, rounds

    managed = rounds._MANAGED_DISPATCH.get()
    if _CORRECTION_PUBLICATION.get() is not None or managed is not None and managed["task"] == prepared.task:
        yield
        return
    lock = StateLock(receipts.task_lock_path(prepared.run.path.parents[2], prepared.task), timeout=0)
    try:
        lock.__enter__()
    except LockTimeout:
        error = EngineError("OAH_REVIEW_SERIES_BUSY", "another review or paid start owns this task; wait for it to "
                            "finish, then retry oah call")
        _record_refusal(prepared, error)
        raise error from None
    token = rounds._PAID_SPAWN.set(record)
    try:
        yield
    finally:
        rounds._PAID_SPAWN.reset(token)
        lock.__exit__(None, None, None)


def _task_dispatch_gate(state, task, root, *, stage, record=False, run_id=None, reserve=False):
    from oah.review import ReviewError, rounds

    managed = rounds._MANAGED_DISPATCH.get()
    base = None
    if managed is not None and managed["task"] == task:
        root, base = managed["root"] or root, managed["base"]
        record = reserve = False  # The structured start already reserved this run.
    try:
        rounds.dispatch_gate(state, task, root, stage=stage, record=record, run_id=run_id, reserve=reserve, base=base)
    except ReviewError as exc:
        raise EngineError(exc.code, str(exc).split(": ", 1)[1]) from None


@contextmanager
def dispatch_guard(prepared: Prepared, *, env=None):
    """Revalidate the prepared lease and source under the state lock, through a short launch only."""
    from oah.lease import LeaseError, Store

    run = prepared.run
    publication = _CORRECTION_PUBLICATION.get()
    if publication is not None and publication.plan.prepared == prepared:
        publication.budget_check()
        if publication.plan.design_record is None:
            holder = publication.holder()
            if publication.e_prepared is None:
                from oah.lockfile import StateLock

                with StateLock(Store(publication.state).lock_path, timeout=0):
                    _check_e(prepared, holder, publication=publication, env=env)
                    yield holder
                return
            try:
                with Store(publication.state).checked(task=holder.task, lease_id=holder.id,
                                                      generation=holder.generation) as live:
                    _check_dispatch_lease(live, pathlib.Path(prepared.cwd), stage=prepared.stage)
                    _check_e(prepared, live, publication=publication, env=env)
                    yield live
            except LeaseError as exc:
                raise EngineError("OAH_SKILL_NOT_DELIVERABLE(changed)",
                                  "the correction's current task lease changed; prepare again") from exc
            return
        if publication.launch_guard is None:
            raise EngineError("OAH_RUN_LAUNCH_INVALID", "the correction has no current target comparison")
        with publication.launch_guard() as live:
            _check_dispatch_lease(live, pathlib.Path(prepared.cwd), stage=prepared.stage)
            _check_e(prepared, live, publication=publication, env=env)
            sealed = publication.plan.design_record
            try:
                _check_design_unchanged(prepared, live, json.loads(sealed) if sealed else None)
            except EngineError as exc:
                _record_refusal(prepared, exc)
                raise
            yield live
        return
    from oah.launchers import native

    from oah.discovery import records

    if records.present(run.path / native.OFFER_NAME):
        raise EngineError("OAH_RUN_LAUNCH_INVALID", f"run {prepared.run_id} is a native offer, not a dispatch "
                          "authorization; nothing was launched. Settle it with oah call settle, or prepare a new run "
                          "with oah call")
    try:
        saved = records.run_json(run, "lease.json", parse=_parse_lease_snapshot)
        store = Store(run.path.parents[2])
        with store.checked(task=prepared.task, lease_id=saved["id"], generation=saved["generation"]) as live:
            _check_dispatch_lease(live, pathlib.Path(prepared.cwd), stage=prepared.stage)
            from oah.review import rounds
            _task_dispatch_gate(run.path.parents[2], prepared.task, pathlib.Path(live.worktree), stage=prepared.stage,
                                record=rounds._PAID_SPAWN.get(), run_id=prepared.run_id)
            # The judgement closest to the spawn: local Git and file reads only, under the short launch lock.
            expected = live.design is not None or live.design_invalid
            if expected or records.present(run.path / DESIGN_NAME):
                design_saved = _read_design(prepared)
                _check_design_unchanged(prepared, live, design_saved)
            _check_skill_bundle(prepared, run, live.id, env=env)
            _check_e(prepared, live, env=env)
            reference = _REFERENCE_RUN.get()
            if reference is not None and reference.run_id == prepared.run_id:
                from oah.design.model import DesignError

                try:
                    reference.spawn_intent(store)
                except DesignError as exc:
                    raise _design_error(exc) from None
                reference.held_store = store
                try:
                    yield live
                finally:
                    reference.held_store = None
            else:
                yield live
    except (LeaseError, EngineError) as exc:
        error = EngineError(exc.code, str(exc).split(": ", 1)[1] if prepared.retain_worker_text else "")
        _record_refusal(prepared, error)
        raise error from None


def _check_skill_bundle(prepared: Prepared, run, lease_id: str, *, env=None) -> None:
    """Local only, under the launch lock: every delivered skill text still has its recorded bytes, and a task
    selection's item is still the lead's current selection for its need under this lease (DESIGN §4.1)."""
    from oah import skillpack
    from oah.config import ConfigError
    from oah.discovery import tasks

    try:
        bundle = skillpack.bundle_of(run.path)
    except ValueError as exc:
        raise EngineError("OAH_RUN_LAUNCH_INVALID", f"{exc}; run oah call again to prepare it") from None
    if bundle is None:
        return
    state_dir = run.path.parents[2]
    try:
        roots = skillpack.environment_roots(pathlib.Path(prepared.cwd), state_dir,
                                            env if env is not None else os.environ)
    except ConfigError as exc:
        raise EngineError(f"{skillpack.NOT_DELIVERABLE_CODE}(changed)",
                          f"{exc}; nothing was launched and the earlier E record is kept; prepare again") from None
    problems = (skillpack.recheck(bundle, state_dir=state_dir) + skillpack.recheck_installed(bundle, roots)
                + skillpack.recheck_references(bundle, pathlib.Path(prepared.cwd)))
    problems += [f"{item['skill']}: the lead's task selection for need {item['need_id'][7:19]} changed since prepare"
                 for item in bundle.get("items", []) if item.get("stage") is None
                 and not tasks.still_selected(state_dir, item["need_id"], item["review"], lease_id=lease_id)]
    if problems:
        raise EngineError(f"{skillpack.NOT_DELIVERABLE_CODE}(changed)", "; ".join(problems)
                          + "; nothing was launched. Prepare the run again with oah call")


def _pack_error(exc) -> EngineError:
    """A skill admission refusal as the CLI prints it: `CODE(reason): detail`, the code first."""
    from oah.discovery import boundaries

    failure = boundaries.mapped("admission", exc)
    assert isinstance(failure, EngineError)
    return failure


@contextmanager
def _admission_refusals():
    from oah.discovery import boundaries

    with boundaries.refusals("admission"):
        yield


def prepared_e_digest(run) -> str | None:
    from oah import skillpack
    from oah.discovery import boundaries, preparation, records

    with boundaries.refusals("launch"):
        saved = records.run_json(run, "skills.json", required=False, parse=skillpack.parse_record)
        if saved is None and records.run_json(run, "capabilities.json", required=False) is not None:
            raise EngineError("OAH_RUN_LAUNCH_INVALID", "managed capabilities need a complete PreparedE record")
        if saved is None or isinstance(saved, dict) and saved.get("prepared_e") is None and not saved.get("bundle"):
            return None
        return saved["prepared_e"]["digest"]


def _check_e(prepared, holder, *, publication=None, native_record=None, env=None, before_spend=True,
             correction=False, frozen: FrozenCheck | None = None):
    from oah import skillpack
    from oah.capabilities import plan as capability_plan
    from oah.call_cli import load_settings
    from oah.discovery import boundaries, budget, preparation, records
    from oah.discovery.store import digest
    from oah.config import ConfigError
    from oah.packet import PacketError

    state_dir, root = prepared.run.path.parents[2], pathlib.Path(prepared.cwd)
    env = os.environ if env is None else env

    def check_absence(skills):
        if holder is None or holder.binding() is None:
            return
        request = CallRequest(engine=prepared.engine, stage=prepared.stage, task=prepared.task,
                              retain_worker_text=prepared.retain_worker_text)
        try:
            packet = read_packet(root, root / holder.binding().source, parse_intent=oah_intent.parse,
                                  read_source=oah_intent.read_source, validate_plan=False)
            _check_lease_source(holder.binding(), packet, prepared.task)
            task_design, design = _task_design(holder, packet, state_dir=state_dir, stage=prepared.stage,
                                               task=prepared.task, engine=prepared.engine)
            _admit_skills(request, holder, task_design, root=root, state_dir=state_dir, env=env,
                          native=native_record is not None,
                          brief_disabled=(skills or {}).get("rerank") == "unused(disabled-by-brief)",
                          packet=packet, design=design, advise=False, expected=False)
        except (ConfigError, PacketError, OSError, ValueError) as exc:
            if isinstance(exc, StorageError):
                raise
            return
        except EngineError as exc:
            if exc.code.startswith(("OAH_SKILL_", "OAH_SKILLS_", "OAH_CAPABILITY_")):
                raise
            return

    boundary = "freeze" if publication is not None or correction else "settle" if native_record is not None else "launch"
    with boundaries.refusals(boundary):
        if publication is not None:
            live = publication.e_prepared
            if prepared.e_digest is None and live is None:
                check_absence(publication.plan.skills_record)
                return
            if live is None:
                raise ValueError("the correction's E preparation was not captured")
            live.recheck()
            record = live.record()
            if publication.plan.skills_record.get("prepared_e") != preparation.projection(record):
                raise ValueError("the correction's closed E record changed")
        else:
            saved = (frozen.skills if frozen is not None else records.run_json(
                prepared.run, "skills.json", required=prepared.e_digest is not None,
                parse=lambda data: skillpack.parse_record(data, required=prepared.e_digest is not None)))
            raw = (saved or {}).get("prepared_e")
            if prepared.e_digest is None and raw is None:
                if (saved or {}).get("bundle") or records.present(prepared.run.path / "capabilities.json"):
                    raise EngineError("OAH_RUN_LAUNCH_INVALID", "managed content needs a complete PreparedE record; prepare again")
                check_absence(saved)
                return
            record = frozen.record if frozen is not None else raw
            frozen_inputs = (frozen.inputs if frozen is not None else capability_plan.frozen_inputs(
                record, state_dir=state_dir) if not before_spend else None)
            facts = record["facts"]
            if not isinstance(facts, dict):
                raise ValueError("the prepared facts are incomplete")
            context = facts["context"]
            if not isinstance(context, dict):
                raise ValueError("the prepared dispatch context is incomplete")
            recorded_path = context["root"]
            if not isinstance(recorded_path, str):
                raise ValueError("the prepared command root is incomplete")
            recorded_root = pathlib.Path(recorded_path)
            if recorded_root.resolve() != root.resolve():
                raise ValueError("the prepared command root changed")
            root = recorded_root
            if before_spend and dt.datetime.now(dt.timezone.utc) >= budget.moment(record["expires_at"]):
                raise ValueError("the original preparation evidence expired")
            refs, facts = record["references"], record["facts"]
            if not isinstance(refs, dict) or not isinstance(facts, dict):
                raise ValueError("the preparation references are malformed")
            run_data = (frozen.result if frozen is not None else records.run_json(prepared.run, "result.json"))
            recorded_axes = context["axes"]
            if not isinstance(recorded_axes, dict):
                raise ValueError("the prepared dispatch axes are incomplete")
            axes = {key: str(value) for key, value in recorded_axes.items()}
            request = CallRequest(engine=prepared.engine, stage=prepared.stage, task=prepared.task, axes=axes,
                                  override_stage=run_data.get("override_stage") is True,
                                  tier=refs["tier"], skill_needs=refs["skill_needs"],
                                  parent_inventory=refs["parent_inventory"], relay_results=tuple(refs["relay_results"]),
                                  route=refs["route"], retain_worker_text=prepared.retain_worker_text)
            packet = read_packet(root, root / holder.binding().source, parse_intent=oah_intent.parse,
                                  read_source=oah_intent.read_source)
            _check_lease_source(holder.binding(), packet, prepared.task)
            task_design, design = _task_design(holder, packet, state_dir=state_dir, stage=prepared.stage,
                                               task=prepared.task, engine=prepared.engine)
            current = _admit_skills(
                request, holder, task_design, root=root, state_dir=state_dir, env=env,
                native=refs["route"] == "native", brief_disabled=True, packet=packet, frozen_needs=refs["needs"],
                target_check=lambda: _e_target(request, refs["tier"], root=root, state_dir=state_dir,
                                               env=env, native=refs["route"] == "native"),
                design=design, advise=False, expected=True, spent_record=record if not before_spend else None,
                spent_inputs=frozen_inputs)
            if current is None or current.admitted is None or current.admitted.prepared is None \
                    or current.admitted.prepared.record()["facts"] != facts:
                raise ValueError("the complete current local E identity changed")
            bundle = (saved or {}).get("bundle")
            if record["final"]["bundle_digest"] != (bundle or {}).get("digest"):
                raise ValueError("the delivered bundle changed its task, lease, engine, stage or payloads")
            caps = (frozen.capabilities if frozen is not None else records.run_json(
                prepared.run, "capabilities.json",
                required=record["final"]["capability_plan_digest"] is not None))
            if record["final"]["capability_plan_digest"] != (digest(caps) if caps is not None else None):
                raise ValueError("the capability record changed")
        if prepared.e_digest != record["digest"]:
            raise ValueError("the complete prepared E record changed")
        facts = record["facts"]
        if facts["selectors"] != _e_selectors(env):
            raise ValueError("the actual child configuration selectors changed")
        lease = facts["operation"]["lease"]
        if facts["engine"] != prepared.engine or facts["stage"] != prepared.stage \
                or facts["operation"]["task"] != prepared.task or lease[0] != holder.id \
                or before_spend and lease[1] != holder.generation:
            raise ValueError("the task, lease generation, engine or stage changed")
        if any(facts["target"][key] != value for key, value in (
                ("model", prepared.model), ("effort", prepared.launch_effort), ("cli_version", prepared.cli_version))):
            raise ValueError("the final launch model, effort or CLI identity changed")
        if publication is not None:
            text = publication.plan.prompt
        elif frozen is not None:
            text = frozen.prompt
        else:
            text = records.run_text(prepared.run)
        if record["final"]["render_digest"] != skillpack._sha(text.encode("utf-8")):
            raise ValueError("the exact final render changed")


def _e_target(request, tier, *, root, state_dir, env, native=False):
    import platform

    from oah.call_cli import load_settings
    from oah.discovery.store import digest

    settings = load_settings(root)
    if native:
        return _native_target(request.engine, tier, settings, root=root, env=env)
    checked = check_catalog(request.engine, settings, state_dir=state_dir, env=env, persist=False)
    basis = _native_basis(request, settings, root=root, state_dir=state_dir, allow_reference=True)
    resolution = resolve_engine(request.engine, tier, settings, state_dir=state_dir, env=env, checked=checked,
                                floor_tier=_launch_floor(request.stage, tier, basis.floor_tier),
                                stage=request.stage, record=False)
    path = pathlib.Path(resolution.base[0]).resolve()
    with path.open("rb") as handle:
        executable = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    driver = None
    if len(resolution.base) > 1 and pathlib.Path(resolution.base[1]).suffix == ".py":
        with pathlib.Path(resolution.base[1]).open("rb") as handle:
            driver = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    return {"engine": request.engine, "route": request.route, "cli_version": resolution.cli_version,
            "profile_id": f"oah-{tier}", "model": resolution.model, "effort": resolution.launch_effort,
            "platform": sys.platform, "architecture": platform.machine() or "unknown",
            "root_digest": digest(str(root.resolve())),
            "config_digest": digest({"settings": dataclasses.asdict(settings), "base": resolution.base,
                                      "executable": executable, "driver": driver})}


def _e_selectors(env):
    from oah.discovery.store import digest

    return digest({key: env.get(key) for key in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME")})


def _e_design_bytes(design):
    if design is None:
        return 0
    if design.transport is not None:
        return sum(len(item.data) for item in design.transport.files) + len(design.transport.manifest_bytes)
    return len(design.text.encode("utf-8"))


def _e_deferred(admitted, design, *, state_dir, root):
    from oah import skillpack

    payloads = []
    if design is not None and design.transport is not None:
        payloads.extend(item.data for item in design.transport.files)
        payloads.append(design.transport.manifest_bytes)
    _block, bundle = skillpack.finalize(admitted)
    for _item, aux in skillpack.references(bundle) if bundle else ():
        _real, data = skillpack._referenced(root, aux)
        if data is None or skillpack._sha(data) != aux["sha256"]:
            raise skillpack.PackRefused(skillpack.REFERENCE_UNAVAILABLE_CODE, "changed",
                                       "the required referenced bytes cannot be read here")
        payloads.append(data)
    return tuple(payloads)


def _e_reference_access(request, *, root, env, target, paths):
    from oah import skillpack
    from oah.call_cli import load_settings
    from oah.design import task_context
    from oah.engines.claude import READ_ONLY_TOOLS

    settings = load_settings(root)
    base = models.resolve_base(request.engine, settings.engines[request.engine]["command"])
    tools = READ_ONLY_TOOLS if request.stage == "review" else tuple(settings.engines["claude"]["tools"])
    operation = "inspect" if request.stage == "review" else "work"
    problem = task_context.qualify_profile(request.engine, operation, executable=base[0],
                                           cli_version=target["cli_version"], tools=tools)
    if problem:
        raise skillpack.PackRefused(skillpack.REFERENCE_UNAVAILABLE_CODE, "profile", problem)
    access = task_context.collect_process_policy(root, env, operation=operation, data_paths=paths,
                                                 argv=tuple(base), cli_version=target["cli_version"])
    if not access["launch_eligible"]:
        raise skillpack.PackRefused(skillpack.REFERENCE_UNAVAILABLE_CODE, "access",
                                   "the current local read/access policy is not verified")
    return access["policy_sha256"]


def _e_reference_paths(bundle, design, root):
    from oah import skillpack

    paths = [str(root / aux["reference"]["path"]) for _item, aux in skillpack.references(bundle)] if bundle else []
    if design is not None and design.transport is not None:
        paths.extend(str(root / design.transport.relative(item.name)) for item in design.transport.files)
        paths.append(str(root / design.transport.relative(design.transport.manifest)))
    return paths


def task_basis(holder, task_design, packet: Packet, *, root: pathlib.Path):
    """The current, verified facts a task's skill needs are built from (DESIGN §4.8): the packet bytes read now and
    checked against the intent section the call already verified, the lease, the kind and risk per dispatch stage from
    the packet and its scope (never from command-line overrides, so the lead's needs file and the call agree), and the
    accepted coverage of a design-bound task."""
    from oah.design import coverage
    from oah.discovery import tasks

    try:
        text = input_files.source_text(pathlib.Path(root) / packet.source, oah_intent.read_source)
    except oah_intent.SourceError as exc:
        raise tasks.NeedsInvalid("stale", f"the packet cannot be read again ({exc})") from None
    section = briefs.raw_section(text)
    if (packet.intent is None or section is None or packet.section is None
            or oah_intent.normalize_newlines(section) != oah_intent.normalize_newlines(packet.section)):
        raise tasks.NeedsInvalid("stale", "the packet's intent section changed while this call read it")
    scope = tuple(packet.scope or holder.scope)
    axes = {}
    for stage in coverage.DISPATCH_STAGES:
        recorded = packet.axes.get(stage)
        given = recorded.to_json() if recorded is not None else {}
        scored = routing.score_axes(stage=stage, kind=given.pop("kind", None), scope=scope, overrides=given)
        axes[stage] = (scored.axes.kind, scored.axes.risk)
    intent = packet.intent
    return tasks.TaskBasis(task=holder.task, text=text,
                           intent={"id": intent.id, "revision": intent.revision, "digest": intent.digest},
                           scope=scope, axes=axes, lease=(holder.id, holder.generation),
                           design=tasks.design_of(task_design),
                           bound=task_design is not None and getattr(task_design, "task", None) is not None)


def task_needs(basis, *, stage: str, policy: str, needs_file: str | None, recorded: bool = False) -> list[dict]:
    """The task's verified needs for one dispatch stage: the lead's file re-resolved, or the ones oah derives."""
    from oah.discovery import inputs, records, tasks

    if needs_file:
        if recorded:
            path = pathlib.Path(needs_file)
            data = records.followed_bytes(path, limit=tasks.NEEDS_BYTES,
                parse=lambda raw: records._object(records.json_bytes(raw, path), path))
            needs = tasks.resolve_needs(data, basis, policy=policy)
        else:
            try:
                raw = inputs.read_bytes(needs_file, limit=tasks.NEEDS_BYTES)
            except OSError as exc:
                raise tasks.NeedsInvalid("malformed", f"cannot read the needs file: {exc.strerror or exc}") from None
            needs = tasks.parse_needs(raw, basis, policy=policy)
        assert needs is not None
    else:
        needs = tasks.derive(basis, dispatch_stage=stage, policy=policy)
    return [item for item in needs if item["dispatch_stage"] == stage]


def _discovery_notices(task: str, found: Sequence[Mapping], *, bound: Collection[str]) -> None:
    """One line for each need that waits on the lead: reviewed proposals, a decision changed, a failed search.
    `bound` holds the needs only an accepted coverage link can serve (tasks.design_bound)."""
    for entry in found:
        short = entry["need_id"][7:19]
        gaps = {gap["code"] for gap in entry["gaps"]}
        if entry["proposed_reviews"]:
            how = ("link one in the design's coverage and accept the design" if entry["need_id"] in bound
                   else "choose with oah skills select --needs FILE --review ID")
            print(f"OAH_SKILL_DISCOVERY {task} need {short}: {len(entry['proposed_reviews'])} reviewed skill(s) "
                  f"proposed, not delivered by this run; {how}", file=sys.stderr)
        if "reselect" in gaps:
            print(f"OAH_SKILL_DISCOVERY {task} need {short}: the design's technology changed since its linked skill "
                  "was found; review one for the new decision and link it again", file=sys.stderr)
        if "provider-failed" in gaps:
            print(f"OAH_SKILL_DISCOVERY {task} need {short}: the skill search failed ({entry['outcome']}); linked "
                  "skills, installed skills and stored reviews still apply", file=sys.stderr)


def _admit_skills(request, holder, task_design, *, root: pathlib.Path, state_dir: pathlib.Path,
                  env: Mapping[str, str], native: bool = False, keychain=None,
                  brief_disabled: bool = False, packet: Packet | None = None,
                  design_text: str | None = None, target=None, target_check=None, preview=None,
                  design=None, frozen_needs=None, advise: bool = True,
                  expected: bool | None = None, query_needs=None, query_file: str | None = None,
                  spent_record=None, spent_inputs: FrozenInputs | None = None) -> SkillPack | None:
    """Lane E's single admission (DESIGN §5.1, §6.4): local and deterministic; a refusal spends and reserves nothing.
    A SkillPack when accepted coverage links or task selections deliver skill texts, capabilities apply or task
    discovery ran, else None (the brief stays byte-identical). The skill admission, the capability plan's required
    needs, dependencies and relays, and the brief floor (B's design block, `design_text`, with the skill and
    capability blocks) are all checked before any task discovery or Jev request; the plan is built again after
    advice, with its class needs, and the exact brief is checked at the end, as finalization's recheck. Task
    discovery runs only after admission, within the call's [skills.discovery] request budget and never for a
    correction (frozen inputs). Only an ordinary run that keeps its output takes a file by reference (execute
    observes the reads) and asks skill-fit advice, after admission; a native offer or a correction never does.
    `expected` compares a boundary's recorded E presence; None is initial preparation, without a prior record."""
    from oah import discovery, skillpack
    from oah.capabilities import inventory, plan as capability_plan
    from oah.config import ConfigError, load_config
    from oah.discovery import budget, gateway, original, preparation, providers, tasks
    from oah.discovery.store import digest

    inputs = _CORRECTION_INPUTS.get() if expected is not False else None
    frozen_record = spent_record if spent_record is not None else (inputs.e_record if inputs is not None else None)
    frozen_inputs = spent_inputs if spent_record is not None else (inputs.e_inputs if inputs is not None else None)
    settings = (inputs.config.section("skills", skillsel.config_defaults()) if inputs is not None
                else skillsel.settings_for(root))
    roots = skillsel.default_roots(root, home=skillsel.home_of(env), env=env,
                                   kit_cache=skillsel.kit_roots(state_dir, pins=settings["pins"]),
                                   codex_plugin_cache=settings["codex_plugin_cache"])
    basis, needs, found_settings = None, [], None
    if packet is not None:
        try:
            found_settings = providers.settings_of(settings)
        except ValueError as exc:
            raise EngineError("OAH_CONFIG_INVALID", f"{exc}; nothing was launched") from None
        with _admission_refusals():
            basis = task_basis(holder, task_design, packet, root=root)
            if frozen_needs is None and inputs is not None:
                frozen_needs = inputs.e_needs
            needs = ([tasks.verify(item, basis, policy=discovery.policy_digest(found_settings))
                      for item in frozen_needs] if frozen_needs is not None else
                     task_needs(basis, stage=request.stage, policy=discovery.policy_digest(found_settings),
                                needs_file=request.skill_needs, recorded=expected is not None))
    search_needs = needs if query_needs is None else query_needs
    delivery_needs = (task_needs(basis, stage=request.stage, policy=discovery.policy_digest(found_settings), needs_file=None)
                      if frozen_needs is not None and basis is not None and basis.design is not None else needs)
    answerable = [item for item in search_needs if tasks.answerable(item)]
    section = (inputs.config.section("capabilities", capability_plan.DEFAULTS) if inputs is not None
               else load_config(root).section("capabilities", capability_plan.DEFAULTS))
    capability_settings = capability_plan.check_settings(section)
    selected = (original.selections(state_dir, basis, discovery.policy_digest(found_settings))
                if basis is not None else [])
    managed_task = basis is not None and original.managed(basis, selected)
    recorded_axes = packet.axes.get(request.stage) if packet is not None else None
    given = recorded_axes.to_json() if recorded_axes is not None else {}
    given.update({axis: value for axis, value in request.axes.items() if value is not None})
    scored = routing.score_axes(stage=request.stage, kind=given.pop("kind", None),
                               scope=packet.scope or holder.scope, overrides=given)
    context = {"root": str(root.resolve()), "axes": scored.axes.to_json(),
               "floor": _call_floor(request.stage, packet.scope or holder.scope, scored.axes.kind)}

    def capture(value):
        current = dt.datetime.now(dt.timezone.utc)
        current_target = target_check() if target_check is not None else target
        live = bound_lease(state_dir, request.task)
        if live is None or (live.id, live.generation) != (holder.id, holder.generation):
            raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "changed", "the task lease changed; "
                                       "prepare again")
        live_packet = read_packet(root, root / packet.source, parse_intent=oah_intent.parse,
                                  read_source=oah_intent.read_source)
        _check_lease_source(live.binding(), live_packet, request.task)
        live_design, _block = _task_design(live, live_packet, state_dir=state_dir, stage=request.stage,
                                           task=request.task, engine=request.engine)
        live_basis = task_basis(live, live_design, live_packet, root=root)
        current_policy = skillsel.settings_for(root)
        current_found = providers.settings_of(current_policy)
        current_needs = ([tasks.verify(item, live_basis, policy=discovery.policy_digest(current_found))
                          for item in frozen_needs] if frozen_needs is not None else
                         task_needs(live_basis, stage=request.stage, policy=discovery.policy_digest(current_found),
                                    needs_file=request.skill_needs, recorded=True))
        current_queries = ([tasks.verify(item, live_basis, policy=discovery.policy_digest(current_found))
                            for item in query_needs] if query_needs is not None else [])
        if query_file is not None and task_needs(live_basis, stage=request.stage,
                policy=discovery.policy_digest(current_found), needs_file=query_file, recorded=True) != list(query_needs):
            raise tasks.NeedsInvalid("stale", "the search needs file changed; prepare the operation again")
        policy = discovery.policy_digest(current_found)
        identity_needs = original.needs(live_basis, list(current_needs) + current_queries, policy)
        selections = original.selections(state_dir, live_basis, policy)
        if selections != selected:
            raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "changed",
                "the task selections changed before original preparation publication; prepare again")
        child = inventory.child(request.engine, root=root, env=env, home=skillsel.home_of(env), now=current,
                                classes=capability_settings["classes"])
        if frozen_inputs is not None:
            parent, loaded = frozen_inputs.parent, frozen_inputs.relays
            relays = [row for row, _text in loaded]
        else:
            parent = inventory.read_parent(request.parent_inventory, now=current,
                                           recorded=True) if request.parent_inventory else None
            relays = [capability_plan.load(state_dir, ref, now=current)[0]
                      for ref in sorted(set(request.relay_results))]
        bodies = []
        for item in value.items:
            bodies.append({"kind": "body", "required": item["required"], "bytes": item["bytes"],
                           "sha256": item["sha256"], "mode": "inline", "review": item["review"]})
            bodies.extend({"kind": "auxiliary", "required": aux["required"], "bytes": aux["bytes"],
                           "sha256": aux["sha256"], "mode": aux["mode"], "review": item["review"]}
                          for aux in item["aux"])
        bodies.extend({"kind": "relay", "required": True, "bytes": row["payload"]["bytes"],
                       "sha256": row["payload"]["sha256"], "mode": "inline"} for row in relays)
        config = root / ".oah" / "config.toml"
        paths = _e_reference_paths(skillpack.finalize(value)[1], design, root)
        access = (_e_reference_access(request, root=root, env=env, target=current_target, paths=paths)
                  if paths else None)
        operation = value.operation.to_json()
        lease = {"digest": digest(live.to_json()), "expires_at": live.expires_at}
        if frozen_record is not None:
            original_lease = frozen_record["facts"]["operation"]["lease"]
            if live.id != original_lease[0]:
                raise ValueError("the original lease id changed")
            live_basis = dataclasses.replace(live_basis, lease=(live.id, original_lease[1]))
            if spent_record is not None:
                operation["lease"] = original_lease
                lease = frozen_record["facts"]["lease"]
        return {"operation": operation, "task": digest(dataclasses.asdict(live_basis)), "lease": lease,
                "engine": request.engine, "stage": request.stage, "route": "native" if native else request.route,
                "context": context,
                "needs": identity_needs,
                "selections": selections, "payloads": sorted(bodies, key=digest),
                "not_delivered": list(value.not_delivered),
                "policy": digest({"skills": current_policy, "capabilities": load_config(root).section(
                    "capabilities", capability_plan.DEFAULTS),
                    "config": skillpack._sha(config.read_bytes()) if config.is_file() else None}),
                "selectors": _e_selectors(env),
                "access": access,
                "inventory": {"child": inventory.stable_digest(child), "parent": parent}, "relay_results": relays,
                "target": current_target,
                "request_budget": value.operation.max_queries,
                "references": digest({"parent": request.parent_inventory, "relay": list(request.relay_results),
                                       "needs": request.skill_needs,
                                       **({"query_file": records.followed_bytes(pathlib.Path(query_file),
                                                limit=tasks.NEEDS_BYTES, parse=skillpack._sha)}
                                          if query_file else {})})}

    def prepare_e(value):
        nonlocal target
        if spent_record is not None:
            facts = capture(value)
            facts["profile"] = digest(spent_record["profile"])

            def refuse_spend():
                raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "admission",
                                           "a settlement identity check grants no new request; prepare again")

            return preparation.PreparedE(json.dumps(facts, sort_keys=True), json.dumps(spent_record["references"]),
                json.dumps(spent_record["profile"]), json.dumps(spent_record["envelope"]),
                spent_record["expires_at"], refuse_spend, json.dumps(spent_record["final"]))
        capabilities, capability_block = capability_plan.for_task(
            holder=holder, task=request.task, task_design=task_design, engine=request.engine, stage=request.stage,
            items=value.items, root=root, env=env, state_dir=state_dir, parent_path=request.parent_inventory,
            relay_refs=request.relay_results, now=dt.datetime.now(dt.timezone.utc), settings=capability_settings,
            frozen=frozen_inputs, parent_recorded=expected is not None)
        needed = bool(value.items or value.not_delivered or capabilities is not None
                      or request.skill_needs is not None or request.parent_inventory or request.relay_results
                      or query_needs is not None)
        config = None
        task_advice = bool(needs and not native and request.stage != "review" and request.retain_worker_text
                           and not brief_disabled)
        if expected is not True and not needed and not answerable and task_advice:
            try:
                from oah.rerank import DEFAULTS as RERANK_DEFAULTS

                config = (inputs.config if inputs is not None else load_config(root)).section("rerank", RERANK_DEFAULTS)
            except (ImportError, AttributeError, ConfigError):
                config = None
        active = gateway.reachable(value.operation.kind, managed=needed, discovery=bool(answerable),
                                   task_advice=task_advice, config=config, env=env, keychain=keychain)
        if expected is not True and not active and not managed_task:
            return None
        if expected is False:
            raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "changed",
                                       "current managed E inputs need a new preparation")
        target = target_check() if target_check is not None else target
        profile = budget.qualify(target or {}, now=dt.datetime.now(dt.timezone.utc)) if active else {}
        block, _bundle = skillpack.finalize(value)
        deferred = _e_deferred(value, design, state_dir=state_dir, root=root)
        paths = _e_reference_paths(_bundle, design, root)
        if paths:
            _e_reference_access(request, root=root, env=env, target=target, paths=paths)
        text = preview(SkillPack(block, _bundle, capability_block, capabilities)) if preview is not None else ""
        skill_growth = sum((len(str(len(value.items))) - len(str(index))) * (1 + len(item["aux"]))
                           for index, item in enumerate(value.items, 1))
        child = inventory.child(request.engine, root=root, env=env, home=skillsel.home_of(env),
                                now=dt.datetime.now(dt.timezone.utc), classes=capability_settings["classes"])
        parent = (frozen_inputs.parent if frozen_inputs is not None
                  else inventory.read_parent(request.parent_inventory, now=dt.datetime.now(dt.timezone.utc),
                                             recorded=True)
                  if request.parent_inventory else None)
        optional_bytes = skill_growth + capability_plan.optional_render_bound(child, parent)
        measurement = budget.measure(profile, text, deferred=deferred,
                                     skill_bytes=len((block or "").encode("utf-8")) + skill_growth,
                                     design_bytes=_e_design_bytes(design), optional_bytes=optional_bytes, read_paths=paths) \
            if profile else preparation.identity_envelope(text)
        facts = capture(value)
        facts["profile"] = digest(profile)
        expires = min(*([profile["expires_at"]] if profile else []), facts["lease"]["expires_at"],
                      _iso(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=300)),
                      *(([facts["inventory"]["parent"]["expires_at"]]
                         if frozen_record is None and facts["inventory"]["parent"] else [])),
                      *([row["expires_at"] for row in facts["relay_results"]] if frozen_record is None else []),
                      key=budget.moment)
        original = digest(facts)

        def recheck():
            if frozen_record is None and dt.datetime.now(dt.timezone.utc) >= budget.moment(expires):
                raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "changed", "the preparation evidence "
                                           "expired; prepare again")
            current = capture(value)
            current["profile"] = digest(budget.qualify(current["target"], now=dt.datetime.now(dt.timezone.utc))
                                        if profile else {})
            if digest(current) != original:
                raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "changed", "the complete local E "
                                           "identity changed; prepare again")
            problems = skillpack.recheck(_bundle, state_dir=state_dir) if _bundle else []
            if _bundle:
                problems += skillpack.recheck_installed(_bundle, roots) + skillpack.recheck_references(_bundle, root)
            if problems:
                raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "changed", "; ".join(problems))

        references = {"needs": needs, "skill_needs": request.skill_needs, "parent_inventory": request.parent_inventory,
                      "relay_results": list(request.relay_results), "tier": target["profile_id"].removeprefix("oah-"),
                      "route": "native" if native else request.route}
        return preparation.PreparedE(json.dumps(facts, sort_keys=True), json.dumps(references), json.dumps(profile),
                                     json.dumps(measurement), expires, recheck, json.dumps({
                                         "bundle_digest": _bundle["digest"] if _bundle else None,
                                         "capability_plan_digest": digest(capabilities) if capabilities else None,
                                         "render_digest": skillpack._sha(text.encode("utf-8"))}))

    with _admission_refusals():
        admitted = skillpack.admit(skillpack.Operation("dispatch", task=request.task,
                                                       lease=(holder.id, holder.generation),
                                                       max_queries=found_settings["max_queries"]
                                                       if answerable or query_needs is not None else 0),
                                   task_design=task_design, state_dir=state_dir, engine=request.engine,
                                   stage=request.stage, roots=roots,
                                   receiving_root=root if not native and request.retain_worker_text else None,
                                   needs=delivery_needs, policy=settings, scope=packet.scope or holder.scope,
                                   prepare=prepare_e)

    if admitted.prepared is None:
        return None
    if spent_record is not None:
        block, bundle = skillpack.finalize(admitted)
        return SkillPack(block, bundle, None, None, admitted=admitted)
    if inputs is not None and inputs.e_frozen is not None \
            and preparation.correction_identity(admitted.prepared.record()) != inputs.e_frozen:
        raise EngineError("OAH_FIX_STATE_CHANGED", "the new admission differs from the earlier frozen selection; "
                          "its record is kept; prepare again")

    def plan_capabilities(class_needs=(), class_status=None, advise=None):
        with _admission_refusals():
            return capability_plan.for_task(
                holder=holder, task=request.task, task_design=task_design, engine=request.engine, stage=request.stage,
                items=admitted.items, root=root, env=env, state_dir=state_dir, parent_path=request.parent_inventory,
                relay_refs=request.relay_results, now=dt.datetime.now(dt.timezone.utc), settings=capability_settings,
                class_needs=class_needs, class_status=class_status, advise=advise, frozen=frozen_inputs,
                parent_recorded=True)

    early_capabilities = plan_capabilities()[1]  # required capabilities admit here: a missing relay refuses now
    early_block = skillpack.finalize(admitted)[0]  # advice only reorders, so these sizes are final
    if early_block is not None or early_capabilities is not None:
        try:
            skillpack.check_parts(design_text, early_block, early_capabilities)
        except skillpack.PackRefused as exc:
            raise _pack_error(exc) from None
    found = []
    if answerable and advise:
        found = tasks.prepare_task(admitted, answerable, state_dir=state_dir, settings=found_settings,
                                   now=dt.datetime.now(dt.timezone.utc), basis=basis,
                                   notify=lambda text: print(text, file=sys.stderr), reuse=True)
        _discovery_notices(request.task, found,
                           bound={item["id"] for item in answerable if tasks.design_bound(basis, item)})
    discovered = tuple({key: value for key, value in entry.items()
                        if key not in ("searches", "deferred", "local", "public", "reused")} for entry in found)
    advice = None
    if admitted.items and not native and request.retain_worker_text and advise:
        admitted, advice = skillpack.rank(admitted, advise=_skill_fit_advisor(
            root=root, state_dir=state_dir, env=env, keychain=keychain, brief_disabled=brief_disabled,
            admitted=admitted))
    block, bundle = skillpack.finalize(admitted)
    class_needs, capability_advise = (), None
    class_status = ("unused(native)" if native else "unused(review-isolated)" if request.stage == "review"
                    else None)
    if needs and class_status is None and request.retain_worker_text and advise:
        class_needs, class_status, capability_advise = _capability_advice(
            root=root, state_dir=state_dir, env=env, keychain=keychain, brief_disabled=brief_disabled, basis=basis,
            packet=packet, task=request.task, needs=needs, admitted=admitted)
    capabilities, capability_block = plan_capabilities(class_needs, class_status, capability_advise)
    if block is None and capability_block is None and not discovered and admitted.prepared is None:
        return None
    check = _CORRECTION_INPUT_CHECK.get()
    if check is not None:
        check([block, capability_block])
    return SkillPack(block, bundle, capability_block, capabilities, advice, discovered, admitted)


SKILL_EXCERPT_BYTES = 4000  # the start of each reviewed SKILL.md a skill-fit judgement may read


def _skill_fit_advisor(*, root: pathlib.Path, state_dir: pathlib.Path, env: Mapping[str, str], keychain,
                       brief_disabled: bool, admitted):
    """The advise callable of skillpack.rank: one skill-fit/v1 judgement over two or more optional items of one
    check, each with a verified public excerpt of its reviewed SKILL.md (DESIGN §7.1). A score is applied only after
    the judge's own recheck passes; every other outcome keeps the deterministic order. Nothing is read until rank
    finds such a group."""

    def advise(items: list[dict]) -> dict:
        from oah.config import load_config
        from oah.design.sources.state import StateProblem
        from oah.discovery import review as reviews, store
        from oah.rerank import DEFAULTS as RERANK_DEFAULTS, judge

        def unapplied(status: str, reason: str | None = None) -> dict:
            return {"status": status, "applied": False, "reason": reason, "scores": None}

        stage, check = items[0]["stage"], items[0]["check"]
        if not check:
            return unapplied("unused(insufficient-input)")
        config = load_config(root).section("rerank", RERANK_DEFAULTS)
        sources = reviews.skill_sources(state_dir, [])  # excerpts come from verified snapshots; nothing is reviewed
        policy = sources.policy_digest()
        fields, options = [], []
        try:
            for index, item in enumerate(items):
                source = (store.get_review(state_dir, item["review"]) or {}).get("source") or {}
                fields.append((f"o{index}:skill_excerpt", judge.public_prefix(
                    sources, str(source.get("snapshot")), f"{source.get('path')}/SKILL.md", SKILL_EXCERPT_BYTES)))
                options.append(f"o{index}")
            fields += [("design_stage", judge.label("design-stage", stage)),
                       ("check", judge.label("design-check", f"{stage}.{check}"))]
        except (judge.JudgeError, StateProblem, KeyError, TypeError, AttributeError):
            return unapplied("unused(scope)")  # an excerpt that cannot be read from a verified snapshot is not sent
        input_ref = "sha256:" + hashlib.sha256(json.dumps([stage, check, [item["sha256"] for item in items]])
                                               .encode("utf-8")).hexdigest()
        request = judge.Request("skill-fit", "skill-fit/v1", tuple(fields), tuple(options), input_ref=input_ref)
        result = judge.judge(request, config=config, policy=policy, sources=sources, brief_disabled=brief_disabled,
                              env=env, keychain=keychain, state_dir=state_dir, admitted=admitted)
        if not result.status.startswith("used"):
            return unapplied(result.status)
        applied = judge.apply(result, current_input_ref=input_ref, config=config, sources=sources, request=request,
                              policy=policy, admitted=admitted)
        if not applied.applied:
            return unapplied(result.status, applied.reason)
        return {"status": result.status, "applied": True, "reason": None,
                "scores": [result.answers[key]["noul"] for key in options]}

    return advise


def _capability_advice(*, root: pathlib.Path, state_dir: pathlib.Path, env: Mapping[str, str], keychain,
                       brief_disabled: bool, basis, packet: Packet, task: str, needs: Sequence[Mapping], admitted):
    """(class needs, class status, entry advise) for the capability plan (DESIGN §6.3.4, §7.1.1). One
    capability-needs/v1 judgement per task need without a design, over that need's own slice of the packet's intent
    section, infers its optional capability classes; a class counts only after the judge's recheck. Choosing an entry
    among two or more qualified ones is a separate capability-select/v1 Choice that the plan asks with the same task
    slice. Task text leaves only with send_task_text, and nothing here is asked under a lock."""
    from oah.capabilities import digest as capability_digest
    from oah.config import load_config
    from oah.design.sources import open_sources
    from oah.rerank import DEFAULTS as RERANK_DEFAULTS, judge

    eligible = [item for item in needs if item["design"] is None and item["requirement"]["source"] == "packet-intent"]
    if not eligible:
        return (), None, None
    config = load_config(root).section("rerank", RERANK_DEFAULTS)
    sources = open_sources(root)
    policy = sources.policy_digest()
    task_view = judge.TaskView.of(task, packet)
    start, _total = basis.section("packet-intent")
    data = task_view.texts["intent"].encode("utf-8")

    def ask(request, input_ref: str):
        result = judge.judge(request, config=config, policy=policy, sources=sources, brief_disabled=brief_disabled,
                             env=env, keychain=keychain, state_dir=state_dir, task_view=task_view, admitted=admitted)
        if not result.status.startswith("used"):
            return result, None
        return result, judge.apply(result, current_input_ref=input_ref, config=config, sources=sources,
                                   request=request, policy=policy, task_view=task_view, admitted=admitted)

    class_needs, statuses = [], []
    for item in eligible:
        span = item["requirement"]
        offset, size = span["offset"] - start, min(span["length"], judge.FIELD_BYTES)
        while 0 < size < span["length"] and data[offset + size] & 0xC0 == 0x80:
            size -= 1  # never cut a character
        try:
            field = judge.task_field(task_view, "intent", offset, size)
        except judge.JudgeError:
            statuses.append("unused(scope)")
            continue
        input_ref = capability_digest({"need": item["id"], "task_need": field.identity()})
        result, applied = ask(judge.Request("capability-needs", "capability-needs/v1", (("task_need", field),),
                                            input_ref=input_ref), input_ref)
        statuses.append(result.status)
        if applied is None or not applied.applied:
            continue
        for cls in sorted(label for label, reading in (applied.outcomes.get("labels") or {}).items()
                          if reading == "yes"):
            class_needs.append({"id": capability_digest({"class": cls, "need": item["id"], "origin": "jev"}),
                                "class": cls, "capability_id": None, "required": False, "origin": "jev",
                                "by": [f"need:{item['id'][7:19]}"], "advice_ref": applied.policy_digest,
                                "task_need": field})
    status = "used" if any(item.startswith("used") for item in statuses) else (statuses[0] if statuses else None)

    def advise(need: Mapping, request: Mapping) -> dict:
        fields = [("capability_class", judge.label("capability-class", request["class"])),
                  ("task_need", need["task_need"])]
        fields += [(f"{key}:capability_excerpt", field) for key, field in request["fields"].items()]
        input_ref = capability_digest({"options": request["option_map_digest"],
                                       "task_need": need["task_need"].identity()})
        result, applied = ask(judge.Request("capability-needs", "capability-select/v1", tuple(fields),
                                            tuple(request["fields"]), input_ref=input_ref), input_ref)
        ref = judge.cache_key(result.identity) if result.identity else None
        if applied is None:
            return {"status": result.status, "applied": False, "choice": None, "reason": None, "judgement_ref": ref}
        chosen = applied.applied and applied.outcomes.get("outcome") == "chosen"
        return {"status": result.status, "applied": chosen, "choice": applied.outcomes.get("choice") if chosen else None,
                "reason": None if chosen else (applied.outcomes.get("outcome") if applied.applied else applied.reason),
                "judgement_ref": ref}

    return tuple(class_needs), status, advise


def _record_refusal(prepared: Prepared, error: EngineError | StorageError) -> None:
    """Mark only the caller's own run refused; a queued Orca run belongs to the worker that claims it."""
    run = prepared.run
    if boundaries.dispatch_kind() is not None or (
            isinstance(error, boundaries.CommandRefusal) and error.dispatched is not None):
        return
    if isinstance(error, boundaries.CommandRefusal) and error.record_path == run.path / "result.json":
        run.write_logs("", _recorded_error(error, False) + "\n")
        return
    if isinstance(run, runs.WorkerRun):
        from oah.discovery import records

        data = records.run_json(run, "result.json")
        if data.get("status") != "running" or data.get("pid") != os.getpid():
            if error.code == "OAH_REVIEW_SERIES_BUSY":
                return  # the lock holder may be the worker that claims this run next
            try:
                os.close(run._open_file("worker.claim", os.O_WRONLY | os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                return  # another worker claimed this run; its record is not ours to change
    run.result(status="refused", error=_recorded_error(error, prepared.retain_worker_text))


@boundaries.entry("launch")
def check_prepared(prepared: Prepared) -> None:
    """Check a queue/launcher handoff; execute checks again at the engine's actual spawn."""
    try:
        with boundaries.command("launch"), _paid_dispatch(prepared, record=False), dispatch_guard(prepared):
            pass
    except boundaries.CommandRefusal as exc:
        _record_refusal(prepared, exc)
        raise


def _check_lease_source(binding, packet: Packet | None, task: str) -> None:
    if (packet is None or packet.intent is None or packet.source != binding.source
            or getattr(packet.intent, "digest", None) != binding.digest):
        source = packet.source if packet is not None else "no packet"
        raise EngineError(SOURCE_MISMATCH, f"task {task}'s lease is bound to {binding.line()} from {binding.source}, "
                          f"but this dispatch reads {source}. Run the command again without --packet, or "
                          f"with --packet {binding.source}; a changed goal is a new revision of the bound packet")


def _stored_brief_matches(stored: str, packet: Packet | None) -> None:
    if packet is None or packet.intent is None:
        return
    section = briefs.raw_section(stored)
    if section != (packet.section or packet.intent.section):
        found = "no intent section" if section is None else "a different intent section"
        raise EngineError(SOURCE_MISMATCH, f"the stored brief carries {found}, not INTENT {packet.intent.id}@r"
                          f"{packet.intent.revision} {packet.intent.digest} from {packet.source}; rebuild the brief "
                          "from the bound packet instead of reusing it. A changed goal needs a new revision of the "
                          "same Intent-ID, not a different source")


DESIGN_NAME = "design.json"
DESIGN_AFTER_NAME = "design-after.json"
DESIGN_CHANGED_DURING_RUN = "OAH_DESIGN_CHANGED_DURING_RUN"


def _design_error(exc: Exception) -> EngineError:
    code = getattr(exc, "code", "OAH_DESIGN_BINDING_INVALID")
    text = str(exc)
    return EngineError(code, text.split(": ", 1)[1] if text.startswith(f"{code}: ") else text)


def _task_design(holder, packet: Packet | None, *, state_dir: pathlib.Path, stage: str, task: str,
                 engine: str = "claude"):
    """The task's design judgement and the brief block, judged from the lease's worktree (a review's root is a
    snapshot without authority refs) against the basis rederived from the lease. Raises EngineError before anything
    is spent or reserved."""
    from oah.design import binding as design
    from oah.design import task_context
    from oah.lease_cli import lease_design

    judged = lease_design(holder, state_dir, source=packet.source if packet is not None else None)
    if judged.state == "unbound" and packet is not None and packet.design is not None:
        judged = design.judge(None, root=pathlib.Path(holder.worktree),
                              found=design.Origin("packet", "its packet has a ## Design section"))
    record = judged.task
    try:
        if record is not None and (packet is None or packet.design is None or not record.same_design(packet.design)
                                   or not record.holds(packet.design.binding())):
            named = packet.design.line() if packet is not None and packet.design is not None else "no design"
            raise design.DesignError(design.SOURCE_MISMATCH_CODE, f"the packet this dispatch reads names {named}, but "
                                     f"task {task} follows {record.binding.line()}; nothing was launched. Rebind with "
                                     f"oah start --task {task} --agent {holder.agent} --packet <packet>, or restore "
                                     "the packet's Design section")
        judged.require("inspect" if stage == "review" else "dispatch", task=task, agent=holder.agent)
        if record is None:
            return judged, None
        # Whole-inline, or (Claude only) a reference plan built once the run is allocated; Codex and native
        # Windows refuse here when the design needs references.
        block = task_context.select(judged, task=task, engine=engine)
    except design.DesignError as exc:
        raise _design_error(exc) from None
    return judged, block


def _reference_design(task_design, block, request, *, root: pathlib.Path, run_id: str | None):
    """The reference-transport block for a design whose whole parts do not fit (Claude only, see select)."""
    from oah.design import task_context

    if run_id is None:
        raise _design_error(task_context.DesignError(task_context.TOO_LARGE_CODE, f"task {request.task}: "
                            f"{block.plan}; nothing was launched"))
    try:
        transport = task_context.prepare_transport(
            task_design, task=request.task, run=run_id, root=root, engine=request.engine,
            operation="inspect" if request.stage == "review" else "work",
            root_role="review-snapshot" if request.stage == "review" and request.isolated else "holder-worktree")
    except task_context.DesignError as exc:
        raise _design_error(exc) from None
    return transport.block


def _reference_profile_problem(request, settings: Settings, resolution: "Resolution") -> str | None:
    from oah.design import task_context
    from oah.engines.claude import READ_ONLY_TOOLS

    tools = READ_ONLY_TOOLS if request.stage == "review" else tuple(settings.engines["claude"]["tools"])
    return task_context.qualify_profile(request.engine, "inspect" if request.stage == "review" else "work",
                                        executable=resolution.base[0], cli_version=resolution.cli_version,
                                        tools=tools)


def _argv_tools(argv: Sequence[str]) -> tuple[str, ...]:
    return tuple(argv[argv.index("--tools") + 1].split(",")) if "--tools" in argv else ()


def _design_bound(prepared: "Prepared") -> bool:
    saved = _read_design(prepared)
    return isinstance(saved, dict) and saved.get("task") is not None


def _design_check_record(saved: object, digest_only: bool) -> dict:
    keys = ("version", "state", "fingerprint", "task") if not digest_only else ("version", "state", "fingerprint")
    if not (isinstance(saved, dict) and all(name in saved for name in keys) and saved.get("version") in (1, 2)
            and isinstance(saved.get("fingerprint"), str)):
        raise ValueError("the run's design record is missing or malformed")
    if not digest_only and saved["task"] is not None:
        from oah.design import binding as design

        design.TaskBinding.from_json(saved["task"])
    if saved.get("transport") is not None:
        from oah.design import task_context

        if not isinstance(saved["transport"], dict) or saved["transport"].get("selection") != task_context.SELECTION:
            raise ValueError("the run's transport plan is malformed")
    return saved


def _read_design(prepared: "Prepared") -> dict | None:
    return records.run_json(prepared.run, DESIGN_NAME, required=False,
        parse=lambda data: _design_check_record(data, digest_only=not prepared.retain_worker_text))


def _check_design_unchanged(prepared: "Prepared", live, saved: object) -> None:
    """At the launch point: the design judged now must equal the one the brief was built from."""
    from oah.design import binding as design
    from oah.lease_cli import lease_design

    if saved is None:
        if live.design is not None or live.design_invalid:
            raise EngineError("OAH_RUN_LAUNCH_INVALID", "the run was prepared without the design its lease now follows; "
                              "nothing was launched. Run oah call again")
        return
    judged = lease_design(live, prepared.run.path.parents[2])
    if judged.state == "unbound":
        judged = design.judge(None, root=pathlib.Path(live.worktree),
                              found=design.Origin("lease", "the run was prepared with a design"))
    allowed = design.OPERATIONS["inspect" if prepared.stage == "review" else "dispatch"]
    if judged.state not in allowed or judged.fingerprint() != saved["fingerprint"]:
        raise EngineError(design.CHANGED_CODE, f"the design of task {prepared.task} changed between preparation and "
                          f"launch (now {judged.line()}); nothing was launched. Read it with oah design task "
                          f"{prepared.task}, then run oah call again")


def _design_after(prepared: "Prepared") -> str | None:
    """After the engine ran, record whether its design moved; stored-record refusals keep their category."""
    from oah.design import binding as design

    saved = _read_design(prepared)
    if not isinstance(saved, dict) or not isinstance(saved.get("task"), dict):
        return None
    try:
        record = design.TaskBinding.from_json(saved["task"])
        judged = design.judge(record, root=pathlib.Path(saved.get("worktree") or prepared.cwd))
        state, fingerprint = judged.state, judged.fingerprint()
        accepted, working = judged.record()["accepted"], judged.record()["working"]
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError, OSError, design.DesignError) as exc:
        state, fingerprint, accepted, working = f"unknown({type(exc).__name__})", None, None, None
    changed = fingerprint != saved.get("fingerprint")
    path = prepared.run.file_path(DESIGN_AFTER_NAME)
    try:
        from oah.atomic import write_json_atomic

        write_json_atomic(path,
                          {"version": 1, "state": state, "fingerprint": fingerprint, "accepted": accepted,
                           "working": working, "changed": changed})
    except OSError:
        pass
    if not changed:
        return None
    return (f"{DESIGN_CHANGED_DURING_RUN}: the design of task {prepared.task} changed while this run worked (now "
            f"{state}); its result is evidence for the design it was given only. Read it with oah design task "
            f"{prepared.task} before finishing")


def resolve_worktree(selector: str, root: pathlib.Path, *, retry: Callable[[str], str]) -> pathlib.Path:
    """The one worktree of this repository named by a path, a branch or a worktree folder name."""
    from oah.paths import run_git

    listed = run_git(root, "worktree", "list", "--porcelain", "-z")
    if listed.returncode != 0:
        raise EngineError("OAH_WORKTREE_UNRESOLVED", f"git could not list the worktrees of {root}: "
                          f"{listed.stderr.strip()}")
    worktrees: list[tuple[pathlib.Path, str | None]] = []
    for record in listed.stdout.split("\0\0"):
        fields = [field for field in record.split("\0") if field]
        path = next((f[len("worktree "):] for f in fields if f.startswith("worktree ")), None)
        if path is None:
            continue
        branch = next((f[len("branch "):] for f in fields if f.startswith("branch ")), None)
        worktrees.append((pathlib.Path(path), branch))
    target = pathlib.Path(selector).expanduser()
    target = target if target.is_absolute() else pathlib.Path.cwd() / target
    matches: list[pathlib.Path] = []
    for path, branch in worktrees:
        by_path = target.exists() and path.exists() and os.path.samefile(path, target)
        by_branch = branch is not None and selector in (branch, branch.removeprefix("refs/heads/"))
        if (by_path or by_branch or selector == path.name) and path not in matches:
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    candidates = matches or [path for path, _ in worktrees]
    commands = "\n".join(f"  {path}\n    {retry(str(path))}" for path in candidates)
    if matches:
        raise EngineError("OAH_WORKTREE_UNRESOLVED", f"--worktree {selector!r} matches {len(matches)} worktrees; "
                          f"nothing was launched. Retry with the exact path:\n{commands}")
    raise EngineError("OAH_WORKTREE_UNRESOLVED", f"--worktree {selector!r} names no worktree of this repository; "
                      f"nothing was launched. Retry with one of:\n{commands}")


@dataclasses.dataclass(frozen=True)
class CallRequest:
    engine: str
    stage: str
    task: str = DEFAULT_TASK
    tier: str | None = None
    axes: Mapping[str, str | None] = dataclasses.field(default_factory=dict)
    override_stage: bool = False
    round_instructions: str = ""
    stored_brief: str | None = None
    packet: pathlib.Path | None = None
    timeout_s: float | None = None
    output_schema: str | None = None
    ephemeral: bool = False
    refresh_models: bool = False
    skills: tuple[tuple[str, str], ...] = ()
    no_rerank: bool = False  # the brief turns the re-ranker off (--no-rerank)
    isolated: bool = False  # Codex ignores the user's config.toml and execpolicy rules (oah review)
    output_last_message: str | None = None  # review filters temporary output before retaining it
    retain_worker_text: bool = True  # oah fix streams output without creating logs or a last-message file
    parent_inventory: str | None = None  # the lead's InventoryV1 file (--parent-inventory)
    relay_results: tuple[str, ...] = ()  # stored parent relay results (--relay-result)
    skill_needs: str | None = None  # the lead's TaskSkillNeedV1 file (--skill-needs), re-resolved before use
    route: str = "process"


@dataclasses.dataclass(frozen=True)
class SkillPack:
    """Lane E's admitted delivery for one dispatch: skill texts and capabilities, each optional."""

    block: str | None = None
    bundle: dict | None = None
    capability_block: str | None = None
    capabilities: dict | None = None
    advice: dict | None = None  # skill-fit advice: kept in skills.json beside the bundle, never inside it
    discovery: tuple[dict, ...] = ()  # TaskDiscoveryV1 records of this call's task needs: proposals, never items
    admitted: SkillAdmission | None = dataclasses.field(default=None, compare=False, repr=False)

    @property
    def delivers(self) -> bool:
        return self.block is not None or self.capability_block is not None


@dataclasses.dataclass(frozen=True)
class Prepared:
    run_dir: str
    run_id: str
    task: str
    engine: str
    stage: str
    argv: tuple[str, ...]
    cwd: str
    timeout_s: float
    env_additions: dict
    model: str
    launch_effort: str
    cli_version: str | None
    warnings: tuple[str, ...]
    retain_worker_text: bool = dataclasses.field(default=True, kw_only=True)
    e_digest: str | None = dataclasses.field(default=None, kw_only=True)

    @property
    def run(self) -> runs.Run:
        publication = _CORRECTION_PUBLICATION.get()
        if publication is not None and publication.plan.prepared == self:
            return publication.run
        from oah.discovery.runfiles import Run

        return Run(id=self.run_id, task=self.task, path=pathlib.Path(self.run_dir))

    def to_json(self) -> dict:
        data = dataclasses.asdict(self)
        if self.retain_worker_text:
            data.pop("retain_worker_text")  # Preserve the landed launch schema for ordinary calls and Orca.
        if self.e_digest is None:
            data.pop("e_digest")
        data["argv"] = list(self.argv)
        data["warnings"] = list(self.warnings)
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Prepared":
        fields = dict(data, argv=tuple(data["argv"]), warnings=tuple(data["warnings"]))
        fields.pop("require_detach", None)  # launch.json also stores this launcher-only option
        return cls(**fields)


@dataclasses.dataclass(frozen=True)
class _CallPlan:
    prepared: Prepared
    prompt: str
    result_fields: dict
    skills_record: dict | None
    stored_prompt: str | None = None
    initial_record: dict | None = None
    model_writes: tuple[tuple[pathlib.Path, str], ...] = ()
    binding_record: str | None = None
    lease_record: str | None = None
    dispatch_record: str | None = None
    design_record: str | None = None  # a correction's digest-only design.json, compared again at its launch


@dataclasses.dataclass(frozen=True)
class _CorrectionSnapshot:
    lease_json: str
    inputs_json: str
    plan_json: str


from oah.discovery.runfiles import Run as _RecordRun


@dataclasses.dataclass(frozen=True)
class _CorrectionRun(_RecordRun):
    """Only dispatcher-authored lifecycle values are read after publication begins."""

    record: dict = dataclasses.field(default_factory=dict, kw_only=True, compare=False)

    def read(self):
        return json.loads(json.dumps(self.record))

    def _write_result(self, data):
        data["task"] = "sha256:" + hashlib.sha256(self.task.encode("utf-8")).hexdigest()
        closed = runs._correction_record(data, runs.RESULT_FIELDS)
        data.clear()
        data.update(closed)
        if _CORRECTION_STATE_LOCK.get() == self.path.parents[2]:
            super()._write_result(data)
            if (capture := runs._CORRECTION_CAPTURE.get()) is not None:
                capture[self.result_path] = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
                                             + "\n").encode("utf-8")
        self.record.clear()
        self.record.update(data)

    @contextmanager
    def _result_lock(self):
        yield  # Lifecycle changes stay in memory between the two locked publications.


@dataclasses.dataclass(frozen=True, slots=True)
class FrozenCheck:
    skills: ERecord
    record: ERecord
    inputs: FrozenInputs
    result: ERecord
    capabilities: ERecord | None
    prompt: str


@dataclasses.dataclass
class _CorrectionInputs:
    state: pathlib.Path
    lease_json: str
    bound_json: str
    packet: Packet
    catalog: _CorrectionCatalog
    config: object
    metadata_json: str
    max_rounds: int | None = None
    budget_check: Callable[[], None] | None = None
    launch_guard: Callable | None = None
    snapshot: _CorrectionSnapshot | None = None
    plan: _CallPlan | None = None
    run: _CorrectionRun | None = None
    artifacts: dict = dataclasses.field(default_factory=dict)
    difficulty: object | None = None  # Jev's answer, asked once: the correction decides its tier on it
    notice_pending: bool = False  # published only after the correction's durable start
    e_needs: list | None = None
    e_prepared: PreparedE | None = None
    e_source: dict | None = None
    e_frozen: str | None = None
    e_record: dict | None = None
    e_inputs: FrozenInputs | None = None

    def notice(self, text: str) -> None:
        from oah.rerank import _print_notice

        self.notice_pending = True
        _print_notice(text)

    def holder(self):
        from oah.lease import Lease

        record = self.snapshot.lease_json if self.snapshot is not None else self.lease_json
        return Lease.from_json(json.loads(record), self.state / "leases.json")

    @contextmanager
    def publication(self):
        """Make the immutable call plan available; fix owns the short publication locks."""
        token = _CORRECTION_PUBLICATION.set(self)
        try:
            yield
        finally:
            _CORRECTION_PUBLICATION.reset(token)

    def publish_run(self):
        """Consume only the scanned plan, after the attempt start is durable."""
        from oah.atomic import write_json_atomic, write_text_atomic
        from oah.review import receipts

        plan = self.plan
        self.budget_check()
        receipts._log_round_unlocked(self.state, self.run.task, json.loads(plan.dispatch_record))
        if self.notice_pending:
            try:
                from oah.rerank import _record_notice

                _record_notice(self.state)
            except Exception:
                pass  # notice persistence must never block a correction
            self.notice_pending = False
        # Only start publication depends on model inputs, including when a later write fails.
        for path in (*self.catalog.original, *self.catalog.scores):
            runs._CORRECTION_CAPTURE.get().pop(path, None)
        for path, text in plan.model_writes:
            write_text_atomic(path, text)
        runs._create_run(self.run, plan.initial_record)
        self.run.write_prompt(plan.stored_prompt if plan.stored_prompt is not None else plan.prompt)
        if plan.skills_record is not None:
            write_json_atomic(self.run.file_path("skills.json"), plan.skills_record)
        self.run.result(**plan.result_fields)
        if plan.binding_record is not None:
            write_text_atomic(_binding_path(self.state, self.run.task), plan.binding_record)
            runs._CORRECTION_CAPTURE.get()[_binding_path(self.state, self.run.task)] = plan.binding_record.encode("utf-8")
        if plan.design_record is not None:
            write_text_atomic(self.run.file_path(DESIGN_NAME), plan.design_record)
        write_text_atomic(self.run.file_path("lease.json"), plan.lease_record)


def advise(axes: routing.Axes, scope: Sequence[str], *, root: pathlib.Path, state_dir: pathlib.Path | None = None,
           brief_disabled: bool = False, env: Mapping[str, str] | None = None, keychain=None) -> str:
    """The re-ranker's advisory difficulty status for a new packet's plan, recorded and never applied.

    No Intent exists yet, so it reads the Scope paths alone: never the task name."""
    try:
        rerank = importlib.import_module("oah.rerank")
    except ImportError:
        return "unused(not-configured)"
    try:
        inputs = _CORRECTION_INPUTS.get()
        config = {"config": inputs.config.section("rerank", rerank.DEFAULTS)} if inputs is not None else {}
        advice = rerank.advise_difficulty(axes, rerank.scope_work(scope), root=root, brief_disabled=brief_disabled,
                                          env=env, **config, keychain=keychain, state_dir=state_dir)
    except Exception as exc:  # a plugin failure must never block a launch
        return f"unused(error: {type(exc).__name__})"
    status = str(getattr(advice, "status", "unused(no status)"))
    tier = getattr(advice, "advisory_tier", None)
    return f"{status} tier={tier}" if status == "used" and tier else status


@dataclasses.dataclass(frozen=True)
class Difficulty:
    """Jev's judgement of a call's difficulty and the tier it gives; `used` means `tier_source=jev`."""

    rule_tier: str | None
    tier: str
    used: bool
    status: str  # jev_status: "used", "unused(<reason>)" or "below-threshold <confidence>"
    jev_tier: str | None = None
    confidence: float | None = None
    truncated: bool | None = None
    raised: bool = False  # Jev's tier was below the floor tier and runs at the floor
    note: str | None = None  # for unused(secret): the part that held it, in plain words

    def record(self) -> dict:
        return {"rule_tier": self.rule_tier, "jev_tier": self.jev_tier, "jev_confidence": self.confidence,
                "jev_status": self.status, "jev_truncated": self.truncated}

    @property
    def advisory(self) -> str:
        """The landed `advisory` summary: the status, with Jev's tier when it gave one."""
        return f"{self.status} tier={self.jev_tier}" if self.jev_tier else self.status

    def reasons(self, floor_tier: str) -> list[str]:
        if not self.used:
            return []
        reasons = [f"jev {self.jev_tier} at confidence {self.confidence:.3g}"]
        if self.raised:
            reasons.append(f"floor {floor_tier}: jev asked {self.jev_tier}")
        return reasons


def secret_warning(note: str | None, where: str) -> str:
    """Which part held secret-shaped text, in plain words; never the text itself."""
    return (f"{SECRET_HELD}: Jev was not asked how hard this task is: a privacy rule flagged "
            f"{note or 'the text'} as holding a key or a private path, so nothing was sent and the rule tier runs. "
            f"Remove that text from {where}, or keep the rule tier")


def judge_difficulty(axes: routing.Axes, *, section: str | None, request_text: str | None = None,
                     scope: Sequence[str], rule_tier: str, floor_tier: str, root: pathlib.Path,
                     state_dir: pathlib.Path, brief_disabled: bool = False, env: Mapping[str, str] | None = None,
                     keychain=None, admitted=None) -> Difficulty:
    """Jev's tier for this work (the accepted Intent section, or a request before one exists, then the Scope paths).

    Never blocked by Jev: a failure is a status and the rule tier runs. An invalid `[rerank]` table refuses with
    `OAH_CONFIG_INVALID` before anything is sent. A correction asks once and keeps the answer with its inputs."""
    from oah.config import ConfigError, load_config

    if admitted is not None:
        from oah import skillpack

        skillpack.require(admitted)
        return Difficulty(rule_tier, rule_tier, False, "unused(target-profile)")
    try:
        rerank = importlib.import_module("oah.rerank")
        defaults = rerank.DEFAULTS
    except Exception:  # an absent or broken re-ranker is no re-ranker
        return Difficulty(rule_tier, rule_tier, False, "unused(not-configured)")
    inputs = _CORRECTION_INPUTS.get()
    try:
        config = (inputs.config if inputs is not None else load_config(root)).section("rerank", defaults)
        threshold = rerank.min_confidence(config)
    except ConfigError as exc:
        raise EngineError(exc.code, str(exc).split(": ", 1)[1] + ". Nothing was sent or launched") from None
    except Exception as exc:  # a plugin failure must never block a launch
        return Difficulty(rule_tier, rule_tier, False, f"unused(error: {type(exc).__name__})")
    advice = inputs.difficulty if inputs is not None else None
    if advice is None:
        try:
            work = (rerank.request_work(request_text, scope) if section is None
                    else rerank.intent_work(section, scope))
            advice = rerank.advise_difficulty(dict(axes.to_json(), tier=rule_tier), work, config=config,
                                              brief_disabled=brief_disabled, env=env, keychain=keychain,
                                              state_dir=state_dir, record_notice=inputs is None,
                                              notify=inputs.notice if inputs is not None else None)
        except Exception as exc:  # a plugin failure must never block a launch
            return Difficulty(rule_tier, rule_tier, False, f"unused(error: {type(exc).__name__})")
        if inputs is not None:
            inputs.difficulty = advice
    try:
        decision = rerank.decide(advice, floor_tier=floor_tier, threshold=threshold)
    except Exception as exc:  # a plugin failure must never block a launch
        return Difficulty(rule_tier, rule_tier, False, f"unused(error: {type(exc).__name__})")
    return Difficulty(rule_tier, decision.tier, decision.used, decision.status, getattr(advice, "advisory_tier", None),
                      getattr(advice, "confidence", None), getattr(advice, "truncated", None), decision.raised,
                      getattr(advice, "note", None))


def _lease_scope(state_dir: pathlib.Path, task: str) -> tuple[str, ...]:
    from oah.lease import Store

    for lease, stale in Store(state_dir).snapshot():
        if lease.task == task and not stale:
            return tuple(lease.scope)
    return ()


@contextmanager
def _correction_inputs(check, *, state, lease_json, bound, packet, config, metadata_json, max_rounds=None,
                       budget_check=None, launch_guard=None):
    """Check correction brief inputs before creating a run; other callers keep their landed behavior."""
    token = _CORRECTION_INPUT_CHECK.set(check)
    try:
        inputs = _CorrectionInputs(state, lease_json, json.dumps(bound), packet, _CorrectionCatalog(state), config,
                                   metadata_json, max_rounds=max_rounds, budget_check=budget_check,
                                   launch_guard=launch_guard)
        snapshot = _CORRECTION_INPUTS.set(inputs)
        try:
            yield inputs
        finally:
            _CORRECTION_INPUTS.reset(snapshot)
    finally:
        _CORRECTION_INPUT_CHECK.reset(token)


def _freeze_e(inputs, *, env):
    from oah.discovery import boundaries, records

    with boundaries.command("freeze"), boundaries.refusals("freeze"):
        _freeze_e_checked(inputs, env=env)


def _freeze_e_checked(inputs, *, env):
    from oah import discovery, skillpack
    from oah.discovery import original, preparation, providers, records
    from oah.capabilities import plan as capability_plan

    holder = inputs.holder()

    def needs_frozen():
        judged, _design = _task_design(holder, inputs.packet, state_dir=inputs.state, stage="correction",
                                       task=holder.task, engine="codex")
        basis = task_basis(holder, judged, inputs.packet, root=pathlib.Path(holder.worktree))
        settings = inputs.config.section("skills", skillsel.config_defaults())
        policy = discovery.policy_digest(providers.settings_of(settings))
        return original.managed(basis, original.selections(inputs.state, basis, policy))

    pointer = original.lookup(inputs.state, holder.task)
    if isinstance(pointer, original.MissingReason):
        if needs_frozen():
            raise original.OriginalUnavailable(pointer)
        return
    path = runs.task_directory(inputs.state, holder.task) / pointer["run_id"]
    run = runs.Run(path.name, holder.task, path)
    saved = records.run_json(run, "skills.json", parse=lambda data: skillpack.parse_record(data, required=True))
    if not isinstance(saved, dict):
        raise ValueError("the original skills record is missing or not a JSON object")
    record = saved["prepared_e"]
    if record["digest"] != pointer["digest"]:
        raise ValueError("the original complete preparation differs from its task pointer")
    facts, refs = record["facts"], record["references"]
    result = records.run_json(run, "result.json")
    if not isinstance(result, dict) or result.get("status") not in ("running", "finished", "failed"):
        raise ValueError("the original managed run no longer records a spent result")
    capabilities = records.run_json(run, "capabilities.json",
                                     required=record["final"]["capability_plan_digest"] is not None)
    if capabilities is not None and not isinstance(capabilities, dict):
        raise ValueError("the original capability record is not a JSON object")
    prompt = records.run_text(run)
    assert prompt is not None
    frozen_inputs = capability_plan.frozen_inputs(record, state_dir=inputs.state)
    source = Prepared(run_dir=str(path), run_id=path.name, task=holder.task, engine=facts["engine"],
                      stage=facts["stage"], argv=(), cwd=holder.worktree, timeout_s=300, env_additions={},
                      model=facts["target"]["model"], launch_effort=facts["target"]["effort"],
                      cli_version=facts["target"]["cli_version"], warnings=(), e_digest=record["digest"])
    _check_e(source, holder, env=env, before_spend=False, correction=True,
             frozen=FrozenCheck(saved, record, frozen_inputs, result, capabilities, prompt))
    inputs.e_needs = refs["needs"]
    inputs.e_source = refs
    inputs.e_frozen = preparation.correction_identity(record)
    inputs.e_record = record
    inputs.e_inputs = frozen_inputs


def _brief_text(request, run_id, packet, scope, stored, footer, has_footer, warnings, selection, checked_lease,
                design=None, skill_pack=None, native_canary=None):
    intent = packet.intent if packet else None
    section = (packet.section or getattr(intent, "section", None)) if packet else None
    instructions = request.round_instructions
    if stored is not None:
        section = section if section is not None else briefs.raw_section(stored)
        content = briefs.reusable_content(stored, stage=request.stage, footer=footer, has_footer=has_footer)
        warnings.extend(briefs.reusable_warnings(content))
        instructions = "\n\n".join(part for part in (content, instructions) if part)
    chosen, skills_rerank = selection if selection is not None else (None, None)
    return briefs.build(run_id=run_id, task=request.task, stage=request.stage, engine=request.engine,
                        scope=list(checked_lease.scope) if checked_lease is not None else list(scope),
                        intent_section=section, binding=intent,
                        packet_source=packet.source if packet else None,
                        round_instructions=instructions, skills=request.skills, footer=footer, has_footer=has_footer,
                        skills_block=chosen.brief_text(rerank=skills_rerank) if chosen is not None else None,
                        skill_ids=[skill.id for skill in chosen.skills] if chosen is not None else (),
                        design_block=design.text if design is not None else None,
                        skill_texts_block=skill_pack.block if skill_pack is not None else None,
                        capability_block=skill_pack.capability_block if skill_pack is not None else None,
                        design_line=design.preamble if design is not None else None,
                        design_data=(design.transport.relative("").rstrip("/") if design is not None
                                     and design.transport is not None else None),
                        native_canary=native_canary)


def _brief_skills(root, *, kind, stage, risk, scope, state_dir, env, keychain, brief_disabled, now, admitted=None):
    if admitted is not None:
        from oah import skillpack

        skillpack.require(admitted)
    check = _CORRECTION_INPUT_CHECK.get()
    if check is None:
        return skillsel.choose(root, kind=kind, stage=stage, risk=risk, scope=scope, state_dir=state_dir,
                               env=env, keychain=keychain, brief_disabled=brief_disabled, now=now, admitted=admitted)
    from oah import rerank

    settings = _CORRECTION_INPUTS.get().config.section("skills", skillsel.config_defaults())
    kit = skillsel.kit_roots(state_dir, pins=settings["pins"])
    roots = skillsel.default_roots(root, home=skillsel.home_of(env), env=env, kit_cache=kit,
                                   codex_plugin_cache=settings["codex_plugin_cache"])
    # Corrections omit optional telemetry so even an unsafe attached skill leaves no selection record.
    selection = skillsel.select(kind, stage, risk, scope, roots=roots, settings=settings, state_dir=None,
                                repo_root=root, now=now)
    check(selection.brief_text())
    check(selection.to_record())
    config = _CORRECTION_INPUTS.get().config.section("rerank", rerank.DEFAULTS)
    return skillsel.rerank_skills(selection, config=config, query=f"kind={kind} stage={stage} risk={risk}",
                                 brief_disabled=brief_disabled, env=env, keychain=keychain, admitted=admitted)


@boundaries.entry("admission")
def prepare(request: CallRequest, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
            env: Mapping[str, str], footer: str, has_footer: Callable[[str], bool],
            parse_intent: Callable[[str], object] | None = None,
            read_source: Callable[[pathlib.Path], str] | None = None, now: dt.datetime | None = None,
            keychain=None) -> Prepared:
    """Serialize task authorization and all optional paid preparation, before handing off a reserved run."""
    from oah.lockfile import LockTimeout, StateLock
    from oah.review import receipts, rounds
    from oah.discovery import attempts

    managed_review = rounds._MANAGED_DISPATCH.get()
    managed = (_CORRECTION_INPUTS.get() is not None
               or managed_review is not None and managed_review["task"] == request.task)
    lock = None if managed else StateLock(receipts.task_lock_path(state_dir, request.task), timeout=0)
    if lock is not None:
        try:
            lock.__enter__()
        except LockTimeout:
            raise EngineError("OAH_REVIEW_SERIES_BUSY", "another review or paid preparation owns this task; wait "
                              "for it to finish, then retry oah call") from None
    try:
        with attempts.scope(state_dir), _admission_refusals():
            return _prepare(request, settings, root=root, state_dir=state_dir, env=env, footer=footer,
                            has_footer=has_footer, parse_intent=parse_intent, read_source=read_source, now=now,
                            keychain=keychain)
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _prepare(request: CallRequest, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
             env: Mapping[str, str], footer: str, has_footer: Callable[[str], bool],
             parse_intent: Callable[[str], object] | None = None,
             read_source: Callable[[pathlib.Path], str] | None = None, now: dt.datetime | None = None,
             keychain=None) -> Prepared:
    """Decide engine, tier, model and effort; write the run folder and brief. Raises before any launch.

    `keychain` is passed to the re-ranker's key lookup (`oah.secrets`); None means the OS keychain."""
    root = pathlib.Path(root)
    check = _CORRECTION_INPUT_CHECK.get()
    if check is not None:
        check(dataclasses.asdict(settings))
    briefs.check_footer(footer, has_footer)
    warnings = list(routing.check_stage(request.engine, request.stage, settings.stages,
                                        override=request.override_stage))
    same = routing.same_engine_warning(settings.stages)
    if same:
        warnings.append(same)
    inputs = _CORRECTION_INPUTS.get()
    if inputs is not None:
        _freeze_e(inputs, env=env)
    if inputs is not None and inputs.e_source is not None:
        request = dataclasses.replace(request, parent_inventory=inputs.e_source["parent_inventory"],
                                      relay_results=tuple(inputs.e_source["relay_results"]))
    holder = inputs.holder() if inputs is not None else bound_lease(state_dir, request.task)
    if holder is None:
        raise EngineError("OAH_INTENT_NOT_CURRENT", f"task {request.task} has no active lease with an intent binding. "
                          "Nothing was launched. Accept the packet, then bind it with "
                          f"oah start --task {request.task} --agent {request.engine} --packet "
                          f".oah/tasks/{request.task}/PACKET.md (use your packet's path); then call again")
    _check_dispatch_lease(holder, root, stage=request.stage)
    leased = holder.binding()
    bound = json.loads(inputs.bound_json) if inputs is not None else dispatch_binding(state_dir, request.task)
    default_source = bound["source"] if bound else leased.source
    packet_path = request.packet or root / default_source
    packet = inputs.packet if inputs is not None else read_packet(
        root, packet_path, parse_intent=parse_intent or oah_intent.parse,
        read_source=read_source or oah_intent.read_source)
    _check_lease_source(leased, packet, request.task)
    check_dispatch_binding(bound, packet, request.task)
    # Before the budget gate, so a design refusal spends and reserves nothing. Lane E reads `task_design` (the
    # binding, its state and the accepted texts) for its stage and decision based selection.
    task_design, design = _task_design(holder, packet, state_dir=state_dir, stage=request.stage, task=request.task,
                                       engine=request.engine)
    stored = None
    if request.stored_brief is not None:
        stored = briefs.deliver(request.stored_brief, footer=footer, has_footer=has_footer)
        _stored_brief_matches(stored, packet)
    scope = packet.scope or tuple(holder.scope)
    disabled = request.no_rerank or packet.rerank_disabled
    recorded = packet.axes.get(request.stage)
    given = recorded.to_json() if recorded is not None else {}
    given.update({axis: value for axis, value in request.axes.items() if value is not None})
    scored = routing.score_axes(stage=request.stage, kind=given.pop("kind", None), scope=scope, overrides=given)
    floor_tier = _call_floor(request.stage, scope, scored.axes.kind)
    planned_run = None
    if inputs is None:
        from oah.lease import Store

        planned_run = _PREPARATION_RUN.get() or runs._plan_run(
            state_dir, request.task, request.engine, request.stage, now=now)
    if design is not None and not design.delivered:
        design = _reference_design(task_design, design, request, root=root, run_id=planned_run[0].id
                                   if planned_run is not None else None)
    planned_tier = (request.tier or (packet.plan[request.stage][1] if request.stage in packet.plan else
                                    routing.recommend(scored.axes).tier))
    if request.stage == "review" and routing.TIERS.index(planned_tier) < routing.TIERS.index(floor_tier):
        planned_tier = floor_tier

    def preview(pack):
        current_settings = skillsel.settings_for(root)
        roots = skillpack.environment_roots(root, state_dir, env)
        chosen = skillsel.select(scored.axes.kind, request.stage, scored.axes.risk, scope, roots=roots,
                                 settings=current_settings, state_dir=None, repo_root=root, now=now)
        # ProviderError codes are bounded to 40 ASCII characters; other selection statuses are shorter.
        status_bound = "unused(error:" + "x" * 40 + ")"
        return _brief_text(request, planned_run[0].id if planned_run else "run-00000000-000000000000",
                            packet, scope, stored, footer, has_footer, [], (chosen, status_bound), holder, design, pack)

    from oah import skillpack

    skill_pack = _admit_skills(
        request, holder, task_design, root=root, state_dir=state_dir, env=env, keychain=keychain,
        brief_disabled=disabled, packet=packet, design_text=design.text if design is not None else None,
        target_check=lambda: _e_target(request, planned_tier, root=root, state_dir=state_dir, env=env),
        preview=preview, design=design)
    axes = scored.axes if recorded is not None else None
    # `oah review` passed its own gate under the task lock, with its decision and extra round, which only a
    # logged start carries. Until that start, the gate below would not know them.
    start_round = _PREPARATION_START.get()
    gated = start_round is None

    def check_budget():
        from oah.lease import LeaseError

        if inputs is not None:
            inputs.budget_check()
        elif gated:
            try:
                with Store(state_dir).checked(task=request.task, lease_id=holder.id, generation=holder.generation) as live:
                    _task_dispatch_gate(state_dir, request.task, pathlib.Path(live.worktree), stage=request.stage,
                                        run_id=planned_run[0].id)
            except LeaseError as exc:
                raise EngineError(exc.code, str(exc).split(": ", 1)[1]) from None

    if inputs is None:  # `oah fix` passed the same gate when it captured its inputs
        check_budget()
    reasons = None
    rule_tier = None
    floor_raised = False  # the tier that runs was raised to the floor tier (`tier_floor_raised`)
    if request.tier:
        tier, tier_source = request.tier, "flag"
    elif packet and request.stage in packet.plan:
        planned_engine, tier = packet.plan[request.stage]
        tier_source = "packet"
        if planned_engine != request.engine:
            warnings.append(f"OAH_PACKET_PLAN_ENGINE: the packet plans {planned_engine} for {request.stage}; "
                            f"its tier {tier} is used for {request.engine}")
        # The plan tier is the rule tier unless the user edited it away from the one recorded beside it.
        recorded_rule = packet.planned.get(request.stage)
        rule_tier = tier if recorded_rule in (None, tier) else recorded_rule
    else:
        explicit = any(value is not None for value in request.axes.values())
        recommendation = routing.recommend(scored.axes)
        tier, axes = recommendation.tier, scored.axes
        tier_source = "flag" if explicit else "rules"
        reasons = list(scored.reasons) + list(recommendation.reasons)
        warnings.extend(scored.warnings)
        if request.stage == "review" and routing.TIERS.index(tier) < routing.TIERS.index(floor_tier):
            tier, floor_raised = floor_tier, True
            reasons.append(f"floor {floor_tier}: this review's scope")
        rule_tier = tier
    if rule_tier is not None and rule_tier == tier:
        # Jev judges before the model is chosen, after the budget gate, so a refused budget sends nothing.
        check_budget()
        difficulty = judge_difficulty(scored.axes, section=getattr(packet.intent, "section", "") or "", scope=scope,
                                      rule_tier=tier, floor_tier=floor_tier, root=root, state_dir=state_dir,
                                      brief_disabled=disabled, env=env, keychain=keychain,
                                      **({"admitted": skill_pack.admitted} if skill_pack is not None else {}))
        if difficulty.status == "unused(not-enabled)":  # a disabled re-ranker reads as before at this seam
            difficulty = dataclasses.replace(difficulty, status="unused(not-configured)")
        if difficulty.used:
            tier, tier_source, floor_raised = difficulty.tier, "jev", difficulty.raised
            reasons = [*(reasons or ()), *difficulty.reasons(floor_tier)]
        if difficulty.status == "unused(secret)":
            warnings.append(secret_warning(difficulty.note, f"the packet {packet.source}"))
    else:
        difficulty = Difficulty(rule_tier, tier, False, TIER_GIVEN)
    floor_tier = _launch_floor(request.stage, tier, floor_tier)
    # Admit the rule's pair before reserving any spend, then account durably before the paid judge.
    checked = check_catalog(request.engine, settings, state_dir=state_dir, env=env, refresh=request.refresh_models,
                            now=now, keychain=keychain)
    resolution = resolve_engine(request.engine, tier, settings, state_dir=state_dir, env=env, now=now,
                                checked=checked, floor_tier=floor_tier, stage=request.stage)
    warnings.extend(resolution.warnings)
    pinned = bool(settings.engines[request.engine]["tiers"][tier]["model"])
    admit(resolution, settings, stage=request.stage, tier=tier, floor_tier=floor_tier, freshness=checked[1])
    if design is not None and design.transport is not None:
        # The reference profile is known once the executable and its version are resolved, before any paid judge.
        problem = _reference_profile_problem(request, settings, resolution)
        if problem is not None:
            from oah.design import task_context

            raise EngineError(task_context.REFERENCE_UNAVAILABLE_CODE, f"task {request.task}: the design needs "
                              f"reference delivery and {problem}; nothing was launched")
    check_budget()
    if inputs is None:
        if start_round is not None:
            start_round()
            gated = True
        with Store(state_dir).checked(task=request.task, lease_id=holder.id, generation=holder.generation) as live:
            _check_dispatch_lease(live, root, stage=request.stage)
            _task_dispatch_gate(state_dir, request.task, pathlib.Path(live.worktree), stage=request.stage,
                                reserve=True, run_id=planned_run[0].id)
    resolution, judge = judge_engine(resolution, settings, stage=request.stage, axes=scored.axes, pinned=pinned,
                                     freshness=checked[1], root=root, env=env, floor_tier=floor_tier,
                                     brief_disabled=disabled, keychain=keychain,
                                     state_dir=state_dir if inputs is None else None,
                                     **({"admitted": skill_pack.admitted} if skill_pack is not None else {}))
    admitted = admit(resolution, settings, stage=request.stage, tier=tier, floor_tier=floor_tier, freshness=checked[1])
    warnings.extend(admitted.warnings)
    judge = {**judge, "admission": admitted.to_json(), **difficulty.record(), "tier_floor_raised": floor_raised}
    advisory = difficulty.advisory
    skill_axes = scored.axes
    check_budget()
    try:
        selection = _brief_skills(root, kind=skill_axes.kind, stage=request.stage, risk=skill_axes.risk, scope=scope,
                                  state_dir=state_dir, env=env, keychain=keychain,
                                  brief_disabled=disabled, now=now,
                                  **({"admitted": skill_pack.admitted} if skill_pack is not None else {}))
    except skillpack.PackRefused as exc:
        raise _pack_error(exc) from None
    check_budget()
    if task_design.state == "needs-review":
        warnings.append(f"{task_design.code}: {task_design.task.binding.line()} has a newer draft "
                        f"r{task_design.context.current.revision} that nobody has checked; this read-only run sees "
                        "both, and its result cannot complete the task until the draft is carried or rebound")
    fields = dict(root=root, state_dir=state_dir, packet=packet, scope=scope, stored=stored, footer=footer,
                  has_footer=has_footer, resolution=resolution, tier=tier, tier_source=tier_source, axes=axes,
                  reasons=reasons, advisory=advisory, warnings=warnings, selection=selection, checked_lease=holder,
                  judge=judge, design=design, skill_pack=skill_pack)
    plan = None
    check = _CORRECTION_INPUT_CHECK.get()
    if check is not None:
        run, record = runs._plan_run(state_dir, request.task, request.engine, request.stage, now=now)
        record["task"] = "sha256:" + hashlib.sha256(request.task.encode("utf-8")).hexdigest()
        record = runs._correction_record(record, runs.RESULT_FIELDS)
        # Build the real command, brief and metadata once, without creating a run or flushing writes.
        advance = check_dispatch_binding(bound, packet, request.task)
        binding_record = None
        if advance:
            intent = packet.intent
            history = list(bound.get("history", [])) if bound else []
            history.append({"revision": intent.revision, "digest": intent.digest, "run_id": run.id})
            binding_record = json.dumps({"source": packet.source, "id": intent.id, "revision": intent.revision,
                                         "digest": intent.digest, "history": history}) + "\n"
        plan = dataclasses.replace(
            _plan_prepare(request, settings, run, **fields), initial_record=record,
            model_writes=tuple(inputs.catalog.writes.items()), binding_record=binding_record,
            lease_record=json.dumps(runs._correction_record(
                {"id": holder.id, "generation": holder.generation, "intent": holder.intent},
                ("id", "generation", "intent"))) + "\n",
            dispatch_record=json.dumps({"event": "dispatch", "stage": request.stage, "run_id": run.id,
                                        "max_rounds": inputs.max_rounds}),
            design_record=None if design is None else json.dumps(
                {"version": 1, "state": design.record["state"], "fingerprint": design.record["fingerprint"],
                 "block_sha256": design.record["block_sha256"], "block_bytes": design.record["block_bytes"],
                 "inline_complete": design.record["inline_complete"],
                 "evidence_key": design.record["evidence_key"]}) + "\n")
        captured = {"metadata": json.loads(inputs.metadata_json), "lease": json.loads(inputs.lease_json),
                    "binding": bound, "settings": dataclasses.asdict(settings), "packet": dataclasses.asdict(packet),
                    "config": json.loads(json.dumps(inputs.config.raw, default=
                        lambda value: dict(value) if isinstance(value, Mapping) else str(value))),
                    "models": [(str(path), text) for path, text in inputs.catalog.original.items()],
                    "resolution": dataclasses.asdict(resolution)}
        inputs.snapshot = _CorrectionSnapshot(inputs.lease_json, json.dumps(captured),
                                               json.dumps(dataclasses.asdict(plan), default=str))
        check(json.loads(inputs.snapshot.inputs_json))
        check(json.loads(inputs.snapshot.plan_json))
        # Reconstitute the publication solely from immutable serialized values, never live inputs.
        sealed = json.loads(inputs.snapshot.plan_json)
        sealed["prepared"] = Prepared.from_json(sealed["prepared"])
        sealed["model_writes"] = tuple((pathlib.Path(path), text) for path, text in sealed["model_writes"])
        inputs.plan = _CallPlan(**sealed)
        inputs.run = _CorrectionRun(id=run.id, task=run.task, path=run.path, record=dict(plan.initial_record))
        return plan.prepared
    else:
        from oah.discovery.runfiles import Run

        planned, record = planned_run
        run = runs._create_run(Run(planned.id, planned.task, planned.path), record)
    try:
        return _finish_prepare(request, settings, run, plan=plan, **fields)
    except (EngineError, briefs.BriefError) as exc:
        run.result(status="refused", error=_recorded_error(exc, request.retain_worker_text), warnings=warnings)
        raise


def _plan_prepare(request: CallRequest, settings: Settings, run: runs.Run, *, root: pathlib.Path,
                    state_dir: pathlib.Path, packet: Packet | None, scope: Sequence[str], stored: str | None,
                    footer: str, has_footer: Callable[[str], bool], resolution: Resolution, tier: str,
                    tier_source: str, axes: routing.Axes | None, reasons: list | None, advisory: str,
                    warnings: list[str], selection: tuple | None = None, checked_lease=None,
                    judge: dict | None = None, design=None, skill_pack: SkillPack | None = None) -> _CallPlan:
    chosen, skills_rerank = selection if selection is not None else (None, None)
    text = _brief_text(request, run.id, packet, scope, stored, footer, has_footer, warnings, selection, checked_lease,
                       design, skill_pack)
    if skill_pack is not None and skill_pack.delivers:
        from oah import skillpack

        try:
            skillpack.check_brief(text)
        except skillpack.PackRefused as exc:
            raise _pack_error(exc) from None
    read_only = request.stage == "review"
    if request.engine == "codex":
        argv = codex.build_command(resolution.base, workdir=str(root), read_only=read_only, model=resolution.model,
                                   effort=resolution.launch_effort,
                                   output_last_message=request.output_last_message or str(run.file_path("last-message.md")),
                                   output_schema=request.output_schema, ephemeral=request.ephemeral)
        if request.isolated:
            argv[-1:-1] = ISOLATED_CODEX_FLAGS  # before the stdin marker
        if not request.retain_worker_text:
            # Codex already streams its final message to stdout; never request a second copy on disk.
            output = next(i for i in reversed(range(len(argv))) if argv[i] == "-o")
            del argv[output:output + 2]
    else:
        section = settings.engines["claude"]
        argv = claude.build_command(resolution.base, model=resolution.model, effort=resolution.launch_effort,
                                    permission_mode=section["permission_mode"], tools=section["tools"],
                                    read_only=read_only)
        if design is not None and design.transport is not None:
            # SC4-P1: the actual child's tool events are observed from stream-json; the result line is unchanged.
            at = argv.index("--output-format")
            argv[at + 1] = "stream-json"
            argv.insert(at + 2, "--verbose")
        elif skill_pack is not None and skill_pack.bundle is not None and skillpack.references(skill_pack.bundle):
            # Q8 (DESIGN §7.4): a planned skill reference read is observed from stream-json; only these two tokens
            # change, and execute keeps the result line json mode prints.
            at = argv.index("--output-format")
            argv[at + 1] = "stream-json"
            argv.insert(at + 2, "--verbose")
    additions = {"OAH_WORKER": "1", "OAH_RUN_ID": run.id, "OAH_TASK": request.task}
    timeout_s = float(request.timeout_s) if request.timeout_s else resolution.timeout_s
    result_fields = dict(status="prepared", command=list(argv) if request.retain_worker_text else None,
               cwd=str(root) if request.retain_worker_text else None, read_only=read_only,
               cli_path=resolution.base[0] if request.retain_worker_text else None, cli_version=resolution.cli_version,
               requested_model=resolution.requested_model or None, resolved_model=resolution.model,
               model_source=resolution.model_source, requested_effort=resolution.requested_effort,
               launch_effort=resolution.launch_effort,
               axes=axes.to_json() if axes else None, tier=tier, tier_source=tier_source, tier_reasons=reasons,
               advisory=advisory, override_stage=request.override_stage, warnings=warnings, **(judge or {}))
    if _CORRECTION_INPUT_CHECK.get() is not None:
        result_fields = runs._correction_record(result_fields, runs.RESULT_FIELDS)
    skills_record = {"rerank": skills_rerank, **chosen.to_record()} if chosen is not None else None
    if skill_pack is not None and skill_pack.bundle is not None and _CORRECTION_INPUT_CHECK.get() is None:
        skills_record = {**(skills_record or {}), "bundle": skill_pack.bundle}
        if skill_pack.advice is not None:
            skills_record["skill_fit"] = skill_pack.advice
    if skill_pack is not None and skill_pack.discovery and _CORRECTION_INPUT_CHECK.get() is None:
        skills_record = {**(skills_record or {}), "task_discovery": list(skill_pack.discovery)}
    if skills_record is not None and _CORRECTION_INPUT_CHECK.get() is not None:
        skills_record = runs._correction_record(
            {"count": len(chosen.skills), "sha256": hashlib.sha256(
                json.dumps(skills_record, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
             "advisory": skills_rerank}, ("count", "sha256", "advisory"))
    e_record, stored_prompt = None, None
    if skill_pack is not None and skill_pack.admitted is not None and skill_pack.admitted.prepared is not None:
        from oah.discovery import preparation
        from oah.discovery.store import digest

        live = skill_pack.admitted.prepared
        with _admission_refusals():
            from oah import skillpack

            live = skillpack.finish(skill_pack.admitted, text,
                deferred=_e_deferred(skill_pack.admitted, design, state_dir=state_dir, root=root),
                design_bytes=_e_design_bytes(design), capabilities=skill_pack.capabilities,
                read_paths=_e_reference_paths(skill_pack.bundle, design, root))
        e_record = live.record()
        inputs = _CORRECTION_INPUTS.get()
        published = e_record
        if inputs is not None:
            inputs.e_prepared = live
            published = preparation.projection(e_record)
            stored_prompt = _brief_text(request, run.id, packet, scope, stored, footer, has_footer, [], selection,
                                         checked_lease, design, None)
            stored_prompt += "\n## Managed preparation\n" + json.dumps(published, sort_keys=True) + "\n"
        skills_record = {**(skills_record or {}), "prepared_e": published}
    prepared = Prepared(run_dir=str(run.path), run_id=run.id, task=request.task, engine=request.engine,
                    stage=request.stage, argv=tuple(argv), cwd=str(root), timeout_s=timeout_s,
                    env_additions=additions, model=resolution.model, launch_effort=resolution.launch_effort,
                    cli_version=resolution.cli_version, warnings=tuple(warnings),
                    retain_worker_text=request.retain_worker_text,
                    e_digest=e_record["digest"] if e_record is not None else None)
    return _CallPlan(prepared, text, result_fields, skills_record, stored_prompt=stored_prompt)


def _finish_prepare(request: CallRequest, settings: Settings, run: runs.Run, *, root: pathlib.Path,
                    state_dir: pathlib.Path, packet: Packet | None, scope: Sequence[str], stored: str | None,
                    footer: str, has_footer: Callable[[str], bool], resolution: Resolution, tier: str,
                    tier_source: str, axes: routing.Axes | None, reasons: list | None, advisory: str,
                    warnings: list[str], selection: tuple | None = None, checked_lease=None,
                    plan: _CallPlan | None = None, judge: dict | None = None, design=None,
                    skill_pack: SkillPack | None = None) -> Prepared:
    if any(warning.startswith("OAH_STAGE_OVERRIDDEN") for warning in warnings):
        from oah.lease import Store

        Store(state_dir).log(task=request.task, agent=request.engine,
                             message=f"stage override: run {run.id} runs {request.engine} for stage {request.stage} "
                                     f"(configured: {settings.stages[request.stage]})")
    if plan is None:
        plan = _plan_prepare(request, settings, run, root=root, state_dir=state_dir, packet=packet, scope=scope,
                             stored=stored, footer=footer, has_footer=has_footer, resolution=resolution, tier=tier,
                             tier_source=tier_source, axes=axes, reasons=reasons, advisory=advisory,
                             warnings=warnings, selection=selection, checked_lease=checked_lease, judge=judge,
                             design=design, skill_pack=skill_pack)
    run.write_prompt(plan.prompt)
    from oah.discovery import original

    original.publish(run, plan.skills_record, stage=request.stage)
    if skill_pack is not None and skill_pack.capabilities is not None:
        from oah.atomic import write_json_atomic

        write_json_atomic(run.file_path("capabilities.json"), skill_pack.capabilities)
    if design is not None:
        from oah.atomic import write_json_atomic

        write_json_atomic(run.file_path(DESIGN_NAME), dict(design.record, worktree=checked_lease.worktree))
        if design.transport is not None:
            from oah.design import task_context

            task_context.freeze(run.path, design.transport)
    run.result(**plan.result_fields)
    if packet is not None:
        record_dispatch_binding(state_dir, packet, request.task, run.id, checked_lease=checked_lease, root=root)
    return plan.prepared


# Lane E native route (DESIGN §5.5, §5.8): the common pure preflight, then a non-counting publication. ---------------
@dataclasses.dataclass(frozen=True)
class NativeOffer:
    """What `oah call --launcher native` hands the lead: the offer, the exact message and the host's parameters."""

    run_id: str
    run_dir: str
    offer: dict
    message: str
    host: dict
    tier: str
    tier_source: str
    warnings: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class _NativeBasis:
    holder: object
    packet: Packet
    task_design: object
    design: object
    scope: tuple[str, ...]
    scored: object
    recorded: bool
    floor_tier: str
    warnings: list


def _native_basis(request: CallRequest, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
                  parse_intent=None, read_source=None, allow_reference: bool = False) -> _NativeBasis:
    """The common pure preflight's reads in `_prepare`'s order (lease, packet, binding, design), spending nothing."""
    warnings = list(routing.check_stage(request.engine, request.stage, settings.stages,
                                        override=request.override_stage))
    same = routing.same_engine_warning(settings.stages)
    if same:
        warnings.append(same)
    holder = bound_lease(state_dir, request.task)
    if holder is None:
        raise EngineError("OAH_INTENT_NOT_CURRENT", f"task {request.task} has no active lease with an intent binding. "
                          "Nothing was offered. Accept the packet, then bind it with "
                          f"oah start --task {request.task} --agent {request.engine} --packet "
                          f".oah/tasks/{request.task}/PACKET.md (use your packet's path); then offer again")
    _check_dispatch_lease(holder, root, stage=request.stage)
    leased = holder.binding()
    bound = dispatch_binding(state_dir, request.task)
    packet = read_packet(root, request.packet or root / (bound["source"] if bound else leased.source),
                         parse_intent=parse_intent or oah_intent.parse,
                         read_source=read_source or oah_intent.read_source)
    _check_lease_source(leased, packet, request.task)
    check_dispatch_binding(bound, packet, request.task)
    task_design, design = _task_design(holder, packet, state_dir=state_dir, stage=request.stage, task=request.task,
                                       engine=request.engine)
    if design is not None and not design.delivered and not allow_reference:
        from oah.design import task_context

        raise EngineError(task_context.REFERENCE_UNAVAILABLE_CODE, f"task {request.task}: the design does not fit the "
                          "brief whole, and a native subagent's reads cannot be observed; use the process launcher. "
                          "Nothing was offered")
    scope = tuple(packet.scope or holder.scope)
    recorded = packet.axes.get(request.stage)
    given = recorded.to_json() if recorded is not None else {}
    given.update({axis: value for axis, value in request.axes.items() if value is not None})
    scored = routing.score_axes(stage=request.stage, kind=given.pop("kind", None), scope=scope, overrides=given)
    return _NativeBasis(holder, packet, task_design, design, scope, scored, recorded is not None,
                        _call_floor(request.stage, scope, scored.axes.kind), warnings)


def _native_tier(request: CallRequest, basis: _NativeBasis) -> tuple[str, str, list | None, routing.Axes | None]:
    """The flag, packet or rule tier, as `_prepare` decides it before any judge; Jev is not asked."""
    packet, scored, warnings = basis.packet, basis.scored, basis.warnings
    axes = scored.axes if basis.recorded else None
    if request.tier:
        return request.tier, "flag", None, axes
    if request.stage in packet.plan:
        planned_engine, tier = packet.plan[request.stage]
        if planned_engine != request.engine:
            warnings.append(f"OAH_PACKET_PLAN_ENGINE: the packet plans {planned_engine} for {request.stage}; "
                            f"its tier {tier} is used for {request.engine}")
        return tier, "packet", None, axes
    explicit = any(value is not None for value in request.axes.values())
    recommendation = routing.recommend(scored.axes)
    tier = recommendation.tier
    reasons = list(scored.reasons) + list(recommendation.reasons)
    warnings.extend(scored.warnings)
    if request.stage == "review" and routing.TIERS.index(tier) < routing.TIERS.index(basis.floor_tier):
        tier = basis.floor_tier
        reasons.append(f"floor {basis.floor_tier}: this review's scope")
    return tier, "flag" if explicit else "rules", reasons, scored.axes


def admit_task_discovery(request: CallRequest, query_needs, *, root: pathlib.Path, state_dir: pathlib.Path,
                         env: Mapping[str, str], query_file: str | None = None) -> SkillPack:
    from oah import call_cli, skillpack

    settings = call_cli.load_settings(root)
    basis = _native_basis(request, settings, root=root, state_dir=state_dir, allow_reference=True)
    tier = _native_tier(request, basis)[0]
    run = runs._plan_run(state_dir, request.task, request.engine, request.stage)[0]
    design = basis.design
    if design is not None and not design.delivered:
        design = _reference_design(basis.task_design, design, request, root=root, run_id=run.id)
    tools = call_cli.intent_tools()

    def preview(pack):
        chosen = skillsel.select(basis.scored.axes.kind, request.stage, basis.scored.axes.risk, basis.scope,
                                 roots=skillpack.environment_roots(root, state_dir, env),
                                 settings=skillsel.settings_for(root), state_dir=None, repo_root=root)
        return _brief_text(request, run.id, basis.packet, basis.scope, None, tools.footer, tools.has_footer, [],
                           (chosen, "unused(error:" + "x" * 40 + ")"), basis.holder, design, pack)

    pack = _admit_skills(request, basis.holder, basis.task_design, root=root, state_dir=state_dir, env=env,
        packet=basis.packet, design_text=design.text if design is not None else None, design=design,
        target_check=lambda: _e_target(request, tier, root=root, state_dir=state_dir, env=env),
        preview=preview, advise=False, query_needs=query_needs, query_file=query_file)
    assert pack is not None
    return pack


def _native_agent(tier: str, *, root: pathlib.Path, env: Mapping[str, str]) -> pathlib.Path | None:
    """The registered Claude subagent for the tier: the project's `.claude/agents/oah-<tier>.md`, else the user's."""
    user = pathlib.Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else skillsel.home_of(env) / ".claude"
    for folder in (root / ".claude" / "agents", user / "agents"):
        if (folder / f"oah-{tier}.md").is_file():
            return folder / f"oah-{tier}.md"
    return None


def _native_target(engine: str, tier: str, settings: Settings, *, root: pathlib.Path,
                   env: Mapping[str, str]) -> dict:
    """TargetIdentity of the lead's host. Claude spawns the registered oah-<tier> agent; Codex picks its own role."""
    import platform

    from oah.launchers import native

    config = root / ".oah" / "config.toml"
    agent_bytes = None
    if engine == "claude":
        path = _native_agent(tier, root=root, env=env)
        if path is None:
            raise EngineError(native.HOST_UNSUPPORTED_CODE, f"the native route spawns the Claude subagent oah-{tier}, "
                              f"and no .claude/agents/oah-{tier}.md exists in this repository or your Claude folder; "
                              "run oah init to write the tier agents, then offer again. Nothing was offered")
        from oah.init import agent_values

        agent_bytes = path.read_bytes()
        model, effort = agent_values(agent_bytes) or ("unknown", "unknown")
    else:
        values = settings.engines[engine]["tiers"][tier]
        model, effort = values["model"] or "host-selected", values["effort"]
    return {"engine": engine, "route": "native", "cli_version": None, "profile_id": f"oah-{tier}", "model": model,
            "effort": effort, "platform": sys.platform, "architecture": platform.machine() or "unknown",
            "root_digest": native.sha(str(root.resolve()).encode("utf-8")),
            "config_digest": native.digest({"config": native.sha(config.read_bytes()) if config.is_file() else None,
                                            "agent": native.sha(agent_bytes) if agent_bytes is not None else None})}


def _native_identities(request: CallRequest, basis: _NativeBasis, bundle: Mapping | None, target: Mapping,
                       prepared_e=None, *, lease_generation: int | None = None) -> dict:
    """The task, design, target and budget identities an offer is made under and a settlement re-resolves."""
    from oah import skillpack
    from oah.launchers import native

    packet, holder, intent = basis.packet, basis.holder, basis.holder.binding()
    task = {"task": request.task, "subtask": None,
            "packet_digest": native.digest({"source": packet.source, "section": packet.section,
                                            "scope": list(packet.scope),
                                            "plan": {stage: list(pair) for stage, pair in packet.plan.items()},
                                            "rerank_disabled": packet.rerank_disabled,
                                            "axes": {stage: axes.to_json() for stage, axes in packet.axes.items()},
                                            "planned": dict(packet.planned)}),
            "intent": {"id": intent.id, "revision": intent.revision, "digest": intent.digest},
            "scope_digest": native.digest(list(basis.scope)), "dispatch_stage": request.stage,
            "kind": basis.scored.axes.kind, "risk": basis.scored.axes.risk,
            "lease": {"id": holder.id, "generation": holder.generation if lease_generation is None
                       else lease_generation}}
    design = None
    record = getattr(basis.task_design, "task", None)
    if record is not None:
        binding = record.binding
        accepted = getattr(getattr(basis.task_design, "context", None), "accepted", None)
        coverage = accepted.texts.get("coverage") if accepted is not None else None
        design = {"ref": {"project": binding.project, "id": binding.design, "revision": binding.revision,
                          "digest": binding.digest},
                  "fingerprint": native.digest(basis.design.record.get("fingerprint") if basis.design is not None
                                               else None),
                  "coverage_basis": native.sha((coverage or "").encode("utf-8"))}
    budget = native.digest({"skill_budget": skillpack.SKILL_BUDGET, "brief_budget": skillpack.BRIEF_BUDGET,
                            "items": skillpack.ITEM_LIMIT})
    if prepared_e is not None:
        budget = native.digest(prepared_e["profile"])
    bundle_id = bundle["digest"] if bundle is not None else None
    prep_id = native.digest({"operation": "dispatch", "task": task, "design": design, "target": dict(target),
                             "budget_profile": budget, "bundle": bundle_id,
                             **({"prepared_e": prepared_e["digest"]} if prepared_e is not None else {})})
    return {"task": task, "design": design, "target": dict(target), "budget_profile": budget, "bundle_id": bundle_id,
            "prep_id": prep_id}


@boundaries.entry("admission")
def prepare_native(request: CallRequest, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
                   env: Mapping[str, str], footer: str, has_footer: Callable[[str], bool],
                   parse_intent: Callable[[str], object] | None = None,
                   read_source: Callable[[pathlib.Path], str] | None = None,
                   now: dt.datetime | None = None) -> NativeOffer:
    from oah.discovery import attempts

    with attempts.scope(state_dir), _admission_refusals():
        return _prepare_native(request, settings, root=root, state_dir=state_dir, env=env, footer=footer,
                               has_footer=has_footer, parse_intent=parse_intent, read_source=read_source, now=now)


def _prepare_native(request: CallRequest, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
                   env: Mapping[str, str], footer: str, has_footer: Callable[[str], bool],
                   parse_intent: Callable[[str], object] | None = None,
                   read_source: Callable[[pathlib.Path], str] | None = None,
                   now: dt.datetime | None = None) -> NativeOffer:
    """`oah call --launcher native`: the common pure preflight and skill admission, then one offer under a planned run.

    It asks no paid model or difficulty judge, reserves nothing, logs no round start, starts no engine and never
    finishes, releases or lands anything. A refusal offers nothing and creates no run folder. The run's status stays
    `prepared`; `native-offer.json` holds the offered or settled state and cannot authorize a process or Orca run."""
    from oah import skillpack
    from oah.atomic import write_json_atomic
    from oah.launchers import native

    root = pathlib.Path(root)
    now = now or dt.datetime.now(dt.timezone.utc)
    briefs.check_footer(footer, has_footer)
    basis = _native_basis(request, settings, root=root, state_dir=state_dir, parse_intent=parse_intent,
                          read_source=read_source)
    stored = None
    if request.stored_brief is not None:
        stored = briefs.deliver(request.stored_brief, footer=footer, has_footer=has_footer)
        _stored_brief_matches(stored, basis.packet)
    tier, tier_source, reasons, axes = _native_tier(request, basis)
    target = _native_target(request.engine, tier, settings, root=root, env=env)
    selection = _brief_skills(root, kind=basis.scored.axes.kind, stage=request.stage, risk=basis.scored.axes.risk,
                              scope=basis.scope, state_dir=state_dir, env=env, keychain=None, brief_disabled=True,
                              now=now)
    run, record = runs._plan_run(state_dir, request.task, request.engine, request.stage, now=now)
    from oah.discovery.runfiles import Run

    run = Run(run.id, run.task, run.path)

    def preview(pack):
        token = native.canary(run.id, pack.bundle["digest"] if pack.bundle else None)
        return _brief_text(request, run.id, basis.packet, basis.scope, stored, footer, has_footer, [], selection,
                            basis.holder, basis.design, pack, native_canary=token)

    skill_pack = _admit_skills(
        request, basis.holder, basis.task_design, root=root, state_dir=state_dir, env=env, native=True,
        packet=basis.packet, design_text=basis.design.text if basis.design is not None else None,
        target=target, target_check=lambda: _e_target(request, tier, root=root, state_dir=state_dir, env=env,
                                                      native=True), preview=preview, design=basis.design)
    bundle = skill_pack.bundle if skill_pack is not None else None
    identities = _native_identities(request, basis, bundle, target)
    token = native.canary(run.id, identities["bundle_id"])
    warnings = basis.warnings
    message = _brief_text(request, run.id, basis.packet, basis.scope, stored, footer, has_footer, warnings, selection,
                          basis.holder, basis.design, skill_pack, native_canary=token)
    if skill_pack is not None and skill_pack.delivers:
        try:
            skillpack.check_brief(message)
        except skillpack.PackRefused as exc:
            raise _pack_error(exc) from None
    e_record = None
    if skill_pack is not None and skill_pack.admitted is not None and skill_pack.admitted.prepared is not None:
        live = skill_pack.admitted.prepared
        with _admission_refusals():
            live = skillpack.finish(skill_pack.admitted, message,
                deferred=_e_deferred(skill_pack.admitted, basis.design, state_dir=state_dir, root=root),
                design_bytes=_e_design_bytes(basis.design), capabilities=skill_pack.capabilities,
                read_paths=_e_reference_paths(skill_pack.bundle, basis.design, root))
            e_record = live.record()
        identities = _native_identities(request, basis, bundle, target, e_record)
    pack_id = native.digest({"prep_id": identities["prep_id"], "bundle": identities["bundle_id"],
                             "capabilities": (native.digest(skill_pack.capabilities)
                                              if skill_pack is not None and skill_pack.capabilities is not None
                                              else None),
                             "message": native.sha(message.encode("utf-8"))})
    offer = native.offer_record(run_id=run.id, bundle_id=identities["bundle_id"], pack_id=pack_id,
                                prep_id=identities["prep_id"], task=identities["task"], design=identities["design"],
                                target=target, budget_profile=identities["budget_profile"], message=message,
                                token=token, now=now)
    description = f"oah {request.task} {request.stage} {run.id}"
    params = native.host_params(request.engine, profile_id=target["profile_id"], description=description,
                                message=message)
    from oah.lease import Store

    with Store(state_dir).checked(task=request.task, lease_id=basis.holder.id, generation=basis.holder.generation):
        if e_record is not None:
            try:
                live.recheck()
            except skillpack.PackRefused as exc:
                raise _pack_error(exc) from None
        else:
            _admit_skills(request, basis.holder, basis.task_design, root=root, state_dir=state_dir, env=env,
                          native=True, brief_disabled=True, packet=basis.packet, design=basis.design,
                          advise=False, expected=False)
        runs._create_run(run, record)
        try:
            run.write_prompt(message)
            chosen, skills_rerank = selection
            skills_record = {"rerank": skills_rerank, **chosen.to_record()}
            if bundle is not None:
                skills_record["bundle"] = bundle
            if e_record is not None:
                skills_record["prepared_e"] = e_record
            if skill_pack is not None and skill_pack.discovery:
                skills_record["task_discovery"] = list(skill_pack.discovery)
            from oah.discovery import original

            original.publish(run, skills_record, stage=request.stage)
            if skill_pack is not None and skill_pack.capabilities is not None:
                write_json_atomic(run.file_path("capabilities.json"), skill_pack.capabilities)
            if basis.design is not None:
                write_json_atomic(run.file_path(DESIGN_NAME), dict(basis.design.record, worktree=basis.holder.worktree))
            fields = dict(status="prepared", read_only=request.stage == "review", axes=axes.to_json() if axes else None,
                          tier=tier, tier_source=tier_source, tier_reasons=reasons, override_stage=request.override_stage,
                          warnings=warnings, launcher={"name": "native", "offer": native.OFFER_NAME},
                          jev_status="unused(native)")
            run._write_result(dict(records.run_json(run, "result.json"), **fields))
            offer = native.publish_offer(run.path, offer)
        except (EngineError, briefs.BriefError, runs.RunError) as exc:
            run._write_result(dict(records.run_json(run, "result.json"), status="refused", error=_recorded_error(exc, True), warnings=warnings))
            raise
    host = {"tool": "Agent" if request.engine == "claude" else "native-subagent", "params": params}
    return NativeOffer(run.id, str(run.path), offer, message, host, tier, tier_source, tuple(warnings))


@dataclasses.dataclass(frozen=True)
class NativeSettlement:
    run_id: str
    settlement: dict
    receiving: dict
    offer: dict
    existing: bool


def _native_differences(offer: Mapping, current: Mapping) -> list[str]:
    names = {"task": "task, intent, packet, scope or lease", "design": "design", "target": "host target or config",
             "budget_profile": "budget profile", "bundle_id": "delivered skill texts or their pins",
             "prep_id": "preparation"}
    return [label for key, label in names.items() if offer[key] != current[key]]


@boundaries.entry("settle")
def settle_native(*, state_dir: pathlib.Path, root: pathlib.Path, env: Mapping[str, str], settings: Settings,
                  run_id: str, offer_digest: str, host_child_id: str, outcome: str, result_path: pathlib.Path,
                  token: str | None, parse_intent=None, read_source=None,
                  now: dt.datetime | None = None) -> NativeSettlement:
    from oah.launchers import native
    from oah.lease import LeaseError, Store

    try:
        run = runs.find_run(state_dir, run_id)
    except runs.RunError as exc:
        if exc.code != "OAH_RUN_NOT_FOUND":
            raise
        raise EngineError(native.UNMANAGED_RUN_CODE,
                          f"{run_id} has no native offer in this state folder; offer a native run first") from None
    offer = _native_offer_for_settle(run)
    if offer is None:
        raise EngineError(native.UNMANAGED_RUN_CODE, "this run has no native offer")
    current = bound_lease(state_dir, run.task)
    if current is None:
        raise EngineError(native.STALE_CODE, "the task lease is no longer current; the earlier offer is kept")
    try:
        with Store(state_dir).checked(task=run.task, lease_id=current.id, generation=current.generation):
            return _settle_native_checked(state_dir=state_dir, root=root, env=env, settings=settings, run_id=run_id,
                offer_digest=offer_digest, host_child_id=host_child_id, outcome=outcome, result_path=result_path,
                token=token, parse_intent=parse_intent, read_source=read_source, now=now)
    except LeaseError as exc:
        raise EngineError(native.STALE_CODE, f"{exc}; the earlier offer is kept; offer again with oah call --launcher native") from None


def _native_offer_for_settle(run):
    from oah.discovery import boundaries, records
    from oah.launchers import native

    with boundaries.refusals("settle"):
        offer = native.read_offer(run.path)
        if offer is None:
            result = records.run_json(run, "result.json")
            if isinstance(result, dict) and isinstance(result.get("launcher"), dict) \
                    and result["launcher"].get("name") == "native":
                return native.read_offer(run.path, required=True)
        return offer


def _settle_native_checked(*, state_dir: pathlib.Path, root: pathlib.Path, env: Mapping[str, str], settings: Settings,
                  run_id: str, offer_digest: str, host_child_id: str, outcome: str, result_path: pathlib.Path,
                  token: str | None, parse_intent=None, read_source=None,
                  now: dt.datetime | None = None) -> NativeSettlement:
    from oah.discovery import boundaries, records

    with boundaries.refusals("settle"):
        return _settle_native_operation(state_dir=state_dir, root=root, env=env, settings=settings, run_id=run_id,
            offer_digest=offer_digest, host_child_id=host_child_id, outcome=outcome, result_path=result_path,
            token=token, parse_intent=parse_intent, read_source=read_source, now=now)


def _settle_native_operation(*, state_dir: pathlib.Path, root: pathlib.Path, env: Mapping[str, str], settings: Settings,
                  run_id: str, offer_digest: str, host_child_id: str, outcome: str, result_path: pathlib.Path,
                  token: str | None, parse_intent=None, read_source=None,
                  now: dt.datetime | None = None) -> NativeSettlement:
    """`oah call settle`: attest which host child received the offered message, after re-resolving every identity.

    The identities are re-resolved before an identical retry returns its first receipt, so a settled offer cannot
    become current again after drift. Settling never counts a round, finishes a task, releases a lease, registers a
    review or infers that a skill was applied."""
    from oah import skillpack
    from oah.capabilities import plan as capability_plan
    from oah.discovery import boundaries, preparation, records
    from oah.launchers import native

    now = now or dt.datetime.now(dt.timezone.utc)
    root = pathlib.Path(root)
    with boundaries.refusals("settle"):
        run = runs.find_run(state_dir, run_id)
        offer = _native_offer_for_settle(run)
    if offer is None:
        raise EngineError(native.UNMANAGED_RUN_CODE, f"{run_id} has no native offer in this state folder; only a run "
                          "offered with oah call --launcher native can be settled")
    # The offer's kind and risk came from the packet, the scope and any axis flags; the packet and scope are compared
    # below, so the flags are carried rather than asked for again.
    request = CallRequest(engine=offer["target"]["engine"], stage=offer["task"]["dispatch_stage"], task=run.task,
                          tier=offer["target"]["profile_id"].removeprefix("oah-"),
                          axes={"kind": offer["task"]["kind"], "risk": offer["task"]["risk"]})
    problems = []
    if now >= dt.datetime.fromisoformat(offer["expires_at"].replace("Z", "+00:00")):
        problems.append(f"the offer expired at {offer['expires_at']}")
    with boundaries.refusals("settle"):
        prompt = records.run_text(run)
        assert prompt is not None
        if native.sha(prompt.encode("utf-8")) != offer["message"]["sha256"]:
            problems.append("the run's brief changed after the offer")
        basis = _native_basis(request, settings, root=root, state_dir=state_dir, parse_intent=parse_intent,
                              read_source=read_source)
        saved = records.run_json(run, "skills.json", required=False, parse=skillpack.parse_record)
        if saved is not None and not isinstance(saved, dict):
            raise ValueError("the offered skills record is not a JSON object")
        e_record = saved.get("prepared_e") if saved is not None else None
        bundle = skillpack.parse_bundle(saved["bundle"]) if saved is not None and saved.get("bundle") else None
        capabilities = records.run_json(run, "capabilities.json", required=False)
        if e_record is None and (bundle is not None or capabilities is not None):
            raise EngineError(native.STALE_CODE, "managed content needs a complete PreparedE record; offer again")
        frozen = None
        if e_record is not None:
            result = records.run_json(run, "result.json")
            frozen = FrozenCheck(saved, e_record, capability_plan.frozen_inputs(e_record, state_dir=state_dir),
                                 result, capabilities, prompt)
        prepared = Prepared(run_dir=str(run.path), run_id=run.id, task=run.task, engine=request.engine,
                            stage=request.stage, argv=(), cwd=str(root), timeout_s=300, env_additions={},
                            model=offer["target"]["model"], launch_effort=offer["target"]["effort"],
                            cli_version=None, warnings=(), e_digest=e_record["digest"] if e_record is not None else None)
        _check_e(prepared, basis.holder, native_record=offer, env=env, before_spend=False, frozen=frozen)
        target = _native_target(request.engine, request.tier, settings, root=root, env=env)
        current = _native_identities(request, basis, bundle, target, e_record,
                                     lease_generation=offer["task"]["lease"]["generation"])
        problems.extend(f"the {label} changed" for label in _native_differences(offer, current))
    if problems or offer["state"] == "stale":
        reasons = "; ".join(problems) or "it was marked stale by an earlier settle"
        raise EngineError(native.STALE_CODE, f"run {run_id}'s native offer is stale: {reasons}. Its record is kept as "
                          "evidence of the old offer; offer again with oah call --launcher native")
    if offer_digest != offer["message"]["sha256"]:
        raise native.ProtocolRefused(native.CONFLICT_CODE, f"{offer_digest} is not the digest of run {run_id}'s offered "
                          f"message ({offer['message']['sha256']}); pass the digest the offer printed")
    result = native.read_result(result_path)
    stated = native.claim(offer, host_child_id=host_child_id, task_fingerprint=native.digest(current["task"]),
                          result=result, outcome=outcome, token=token)
    receiving = native.receiving_record(run.id, offer, bundle, records.run_rows(run, skillpack.READS_NAME),
                                        now=now)
    settlement, receiving, existing = native.record_settlement(run.path, offer, stated, receiving, now)
    updated_offer = {**offer, "state": f"settled-{settlement['outcome']}", "settlement": settlement["id"]}
    return NativeSettlement(run.id, settlement, receiving, updated_offer, existing)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _effort_fields(prepared: Prepared, stdout: str, stderr: str, rules: Sequence[Mapping]) -> dict:
    if prepared.engine == "codex":
        header = codex.check_header(stderr, prepared.launch_effort)
        inference = verify_inference("codex", prepared.cli_version, prepared.model, prepared.launch_effort, header,
                                     rules)
        code = inference.code or header.code
        if header.header_model is not None and header.header_model != prepared.model:
            code = MODEL_MISMATCH  # the effective model is checked first, like the effort (amendment 6)
        return {"header_model": header.header_model or runs.UNVERIFIED,
                "header_effort": header.header_effort, "header_check": header.status, "header_code": code,
                "resolved_inference_effort": inference.resolved_inference_effort,
                "normalization_basis": inference.normalization_basis, "effort_verified": inference.effort_verified,
                "effort_observable": header.status != "unverified"}
    observed = claude.observe(stdout)
    # Claude Code takes aliases that it resolves itself, so a requested alias cannot be compared with the
    # full names in modelUsage; the effective model stays unverified (observed_models keeps them).
    fields = {"observed_models": list(observed.models), "effort_observable": observed.effort_observable,
              "header_model": runs.UNVERIFIED}
    if not observed.effort_observable:
        return dict(fields, header_effort=runs.UNVERIFIED, header_check=runs.UNVERIFIED, header_code=None,
                    resolved_inference_effort=runs.UNVERIFIED,
                    normalization_basis="unverified: Claude Code's result JSON does not report the inference effort",
                    effort_verified=False)
    status, code = compare_labels(observed.effort, prepared.launch_effort)
    check = HeaderCheck(observed.effort, status, code, None)
    return dict(fields, header_effort=check.header_effort, header_check=check.status, header_code=code,
                resolved_inference_effort=observed.effort,
                normalization_basis="observed: Claude Code's result JSON reports the effort",
                effort_verified=status == "match")


def _recorded_error(exc: BaseException, retain_worker_text: bool) -> str:
    if retain_worker_text:
        return str(exc)
    # Corrections share the CLI's fixed diagnostic vocabulary; ordinary calls keep their diagnostics.
    from oah.review.fix import _error_diagnostic
    return _error_diagnostic(exc)[1]


def _finish_inline_delivery(prepared: Prepared) -> None:
    from oah.atomic import write_json_atomic
    from oah.design import binding as design, task_context

    publication = _CORRECTION_PUBLICATION.get()
    if publication is not None:
        saved = json.loads(publication.plan.design_record) if publication.plan.design_record else None
    else:
        saved = _read_design(prepared)
    if not isinstance(saved, dict) or not saved.get("inline_complete") or saved.get("transport"):
        return
    if publication is None:
        after = prepared.run._read_json(DESIGN_AFTER_NAME)
        if after.get("changed") is not False:
            return
    else:
        try:
            _check_design_unchanged(prepared, publication.holder(), saved)
        except EngineError as exc:
            if exc.code != design.CHANGED_CODE:
                raise
            raise EngineError("OAH_DESIGN_CHANGED_AFTER_RUN", "the run completed, but delivery is not marked because "
                              "the design changed during the run; read it with oah design task "
                              f"{prepared.task} before retrying") from None
    saved["receiving"] = dict(task_context._whole_receiving(), delivered=True,
                              provided=[{"inline_bytes": saved["block_bytes"], "references": 0}])
    saved["delivered"] = True
    if publication is not None:
        publication.artifacts[DESIGN_NAME] = json.dumps(saved, sort_keys=True) + "\n"
    else:
        write_json_atomic(prepared.run.file_path(DESIGN_NAME), saved)


def _launch_inputs(prepared, publication):
    from oah.capabilities import plan as capability_plan
    from oah.discovery import boundaries, preparation, records

    run = prepared.run
    with boundaries.refusals("launch"):
        if prepared.e_digest is not None and publication is None:
            result = records.run_json(run, "result.json")
            if not isinstance(result, dict):
                raise ValueError("the prepared result is not a JSON object")
            if publication is None:
                from oah import skillpack

                saved = records.run_json(run, "skills.json", parse=lambda data: skillpack.parse_record(data, required=True))
                if not isinstance(saved, dict):
                    raise ValueError("the prepared skills record is not a JSON object")
                record = saved["prepared_e"]
                capability_plan.frozen_inputs(record, state_dir=run.path.parents[2])
                records.run_json(run, "capabilities.json", required=record["final"]["capability_plan_digest"] is not None)
        bundle = _streamed_skill_bundle(prepared)
        prompt = (publication.plan.prompt if publication is not None and publication.plan.prepared == prepared
                  else records.run_text(run))
        plan = records.run_json(run, "capabilities.json", required=False) if bundle is not None else None
        return bundle, prompt, _capability_sink(prepared, bundle, plan)


@boundaries.entry("launch")
def execute(prepared: Prepared, *, base_env: Mapping[str, str] | None = None,
            rules: Sequence[Mapping] | None = None, redact: Callable[[str], str] | None = None) -> dict:
    try:
        publication = _CORRECTION_PUBLICATION.get()
        boundary: boundaries.Boundary = (
            "freeze" if publication is not None and publication.plan is not None
            and publication.plan.prepared == prepared else "launch")
        with boundaries.command(boundary):
            return _execute(prepared, base_env=base_env, rules=rules, redact=redact)
    except EngineError as exc:
        _record_refusal(prepared, exc)
        raise


def _execute(prepared: Prepared, *, base_env: Mapping[str, str] | None = None,
             rules: Sequence[Mapping] | None = None, redact: Callable[[str], str] | None = None) -> dict:
    """Run a prepared call in the foreground and record what happened."""
    from oah.design.task_context import RootUnavailable

    run = prepared.run
    env = dict(os.environ if base_env is None else base_env)
    env.update(prepared.env_additions)
    clean = redact or (lambda text: text)
    publication = _CORRECTION_PUBLICATION.get()
    try:
        skill_bundle, prompt, planned_sink = _launch_inputs(prepared, publication)
        design_bound = _design_bound(prepared)
    except EngineError as exc:
        _record_refusal(prepared, exc)
        raise
    started = dt.datetime.now(dt.timezone.utc)
    run.result(status="running", started_at=_iso(started), pid=os.getpid())
    if prepared.retain_worker_text:
        run.file_path("last-message.md")
    reference = _start_reference(prepared, env)
    sink = planned_sink if reference is None else None
    tokens = (_REFERENCE_RUN.set(reference), _DESIGN_RUN.set(design_bound))
    try:
        result = _run_prepared(prepared, env, prompt, sink=sink)
    except StorageError as exc:
        _end_reference_failed(reference)
        _record_refusal(prepared, exc)
        raise
    except (OSError, ValueError, subprocess.SubprocessError, RootUnavailable) as exc:
        if isinstance(exc, OSError) and boundaries.dispatch_kind() is not None:
            _end_reference_failed(reference)
            raise boundaries.mapped("launch", exc) from None
        observation_failure = _conclude_observation(prepared, sink)
        if prepared.retain_worker_text:
            run.write_logs("", "")
        extra = _end_reference_failed(reference)
        error = (clean(f"OAH_ENGINE_LAUNCH_FAILED: {exc}") if prepared.retain_worker_text else
                 _recorded_error(EngineError(getattr(exc, "code", "OAH_ENGINE_LAUNCH_FAILED"), ""), False))
        if observation_failure:
            error += "; " + (clean(observation_failure) if prepared.retain_worker_text else
                             _recorded_error(EngineError(observation_failure.split(":", 1)[0], ""), False))
        return run.result(status="failed", finished_at=_iso(dt.datetime.now(dt.timezone.utc)), error=error,
                          **({"warnings": [*run.read().get("warnings", []), *extra]} if extra else {}))
    except (KeyboardInterrupt, SystemExit):
        observation_failure = _conclude_observation(prepared, sink)
        # _run_prepared already stopped the process tree; never leave the run looking alive.
        if prepared.retain_worker_text:
            run.write_logs("", "")
        _end_reference_failed(reference)
        error = ("OAH_RUN_INTERRUPTED: the run was interrupted (Ctrl-C or a stop signal) and its process "
                 "tree was stopped; call the engine again when you are ready")
        if observation_failure:
            error += "; " + (clean(observation_failure) if prepared.retain_worker_text else
                             _recorded_error(EngineError(observation_failure.split(":", 1)[0], ""), False))
        run.result(status="failed", finished_at=_iso(dt.datetime.now(dt.timezone.utc)), error=error)
        raise
    except EngineError:
        _end_reference_failed(reference)
        raise
    finally:
        for var, token in zip((_REFERENCE_RUN, _DESIGN_RUN), tokens):
            var.reset(token)
    transport_warnings, transport_failure = [], None
    raw_stdout = result.stdout  # the skill reads below come from the stream as the engine wrote it
    observation_failure = _conclude_observation(prepared, sink)
    skill_warnings, skill_failure = [], None
    receiving_bundle = skill_bundle
    if receiving_bundle is None and reference is not None and prepared.e_digest is not None:
        from oah import skillpack

        receiving_bundle = skillpack.bundle_of(prepared.run.path) or {"items": [], "digest": None}
    if receiving_bundle is not None:
        result, skill_warnings, skill_failure = _conclude_skill_reads(prepared, receiving_bundle, raw_stdout, result,
            normalize=reference is None,
            usage=sink.observation["available_usage"] if sink is not None and sink.observation is not None else None)
    if reference is not None:
        concluded = False
        accounting_problem = "transport accounting did not complete"
        try:
            stdout, transport_warnings, transport_failure = reference.conclude(
                result.stdout, returncode=result.returncode, timed_out=result.timed_out)
            result = dataclasses.replace(result, stdout=stdout)
            concluded = True
        except (OSError, ValueError) as exc:
            accounting_problem = f"transport accounting failed ({type(exc).__name__})"
        finally:
            try:
                if not concluded:
                    try:
                        if prepared.retain_worker_text:
                            run.write_logs("", "")
                    finally:
                        run.result(status="failed", finished_at=_iso(dt.datetime.now(dt.timezone.utc)),
                                   error=f"OAH_DESIGN_ACCOUNTING_FAILED: {accounting_problem}; inspect oah design task "
                                         f"{prepared.task} --run {prepared.run_id} before retrying")
            finally:
                reference.release()
        if not concluded:
            return run.read()
    finished = dt.datetime.now(dt.timezone.utc)
    if prepared.retain_worker_text:
        run.write_logs(clean(result.stdout), clean(result.stderr))
    # Validate the actual header first: redaction must never turn a mismatch into an unreadable header.
    fields = _effort_fields(prepared, result.stdout, result.stderr, load_rules() if rules is None else rules)
    if not prepared.retain_worker_text:
        # Persist typed observations only, never arbitrary header text (including an unknown effort label).
        from oah.engines.effort import ORDINARY, BEHAVIOUR_CHANGING
        if fields.get("header_effort") not in (*ORDINARY, *BEHAVIOUR_CHANGING, prepared.launch_effort):
            fields["header_effort"] = runs.UNVERIFIED
            fields["normalization_basis"] = "unverified: the header effort is unknown"
        if fields.get("header_model") != prepared.model:
            fields["header_model"] = runs.UNVERIFIED  # the mismatch stays in header_code
        fields["worker_output"] = result.worker_output
    fields = {name: clean(value) if isinstance(value, str) else value for name, value in fields.items()}
    if observation_failure is not None:
        status, error = "failed", observation_failure  # the live check stopped it or could not be kept
    elif result.timed_out:
        status, error = "timed_out", (f"OAH_RUN_TIMEOUT: the engine ran longer than {prepared.timeout_s:g} s and its "
                                      "process tree was stopped; raise the tier's timeout_min or split the task")
    elif result.returncode != 0:
        status, error = "failed", (f"OAH_ENGINE_FAILED: {prepared.engine} exited with {result.returncode}; read "
                                   f"{run.stderr_path}")
        if not prepared.retain_worker_text:
            error = (f"OAH_ENGINE_FAILED: {prepared.engine} exited with {result.returncode}; inspect the live "
                     "terminal output and retry the correction")
    elif fields.get("header_code") == MODEL_MISMATCH:
        status, error = "failed", (f"{MODEL_MISMATCH}: the engine reported model {fields['header_model']} for the "
                                   f"requested model {prepared.model}; this run does not count. Check the Codex "
                                   "configuration (a profile or model override) and call again")
    elif fields.get("header_code"):
        status, error = "failed", (f"{fields['header_code']}: the engine reported effort {fields['header_effort']} "
                                   f"(normalized {fields['resolved_inference_effort']}) for launch effort "
                                   f"{prepared.launch_effort}; this run does not count")
    else:
        status, error = "finished", None
    if transport_failure is not None and status == "finished":
        status, error = "failed", transport_warnings[0]  # exposed design data: this run does not count
    if skill_failure is not None and status == "finished":
        status, error = "failed", skill_failure  # an E receiving failure never counts as success (DESIGN §7.4)
    if prepared.retain_worker_text:
        moved = _design_after(prepared)  # corrections keep their closed vocabulary; their next checkpoint re-judges
        if moved is not None or transport_warnings or skill_warnings:
            fields["warnings"] = [*run.read().get("warnings", []), *transport_warnings, *skill_warnings,
                                  *([moved] if moved is not None else [])]
    completed = run.result(status=status, finished_at=_iso(finished), duration_s=round((finished - started).total_seconds(), 3),
                           exit_code=result.returncode, timed_out=result.timed_out, error=error, **fields)
    if status == "finished" and reference is None:
        _finish_inline_delivery(prepared)
    return completed


def _conclude_observation(prepared: "Prepared", sink) -> str | None:
    """Keep the closed receiving projection of a live-read stream (DESIGN §6.5) and say why the run cannot count: the
    live check stopped it, its process tree did not go quiet, or the projection could not be written."""
    if sink is None or sink.observation is None:
        return None
    from oah.discovery import gateway

    observation = sink.observation
    failure = gateway.receive(prepared.run, observation)
    if failure is not None:
        return (f"OAH_CAPABILITY_RECEIVING_FAILED: the stream observation of run {prepared.run_id} could not be "
                f"recorded ({failure}); this run does not count. Check the run folder, then call again")
    if observation["stopped"]:
        return (f"OAH_ENGINE_FAILED: claude's stream-json output failed the live check ({observation['reason']}: "
                f"{observation['detail']}); oah stopped its process tree and this run does not count")
    if not observation["quiescent"]:
        return ("OAH_ENGINE_FAILED: claude's process tree did not go quiet after the run; this run does not count. "
                "Check for leftover processes before calling again")
    return None


def _streamed_skill_bundle(prepared: "Prepared") -> dict | None:
    """The run's skill bundle when it sends files by reference, whose reads execute observes; else None."""
    if prepared.engine != "claude" or not prepared.retain_worker_text:
        return None
    from oah import skillpack

    bundle = skillpack.bundle_of(prepared.run.path)
    return bundle if bundle is not None and skillpack.references(bundle) else None


def _conclude_skill_reads(prepared: "Prepared", bundle: Mapping, raw: str, result, *, normalize: bool, usage=None):
    """(result, warnings, failure): pair the referenced reads in the raw stream and record them in
    skills-receiving.json. When E switched the output mode (`normalize`), stdout becomes the result line json mode
    prints, and a stream without one fails the run. Unread references leave delivery incomplete, not the run."""
    from oah import skillpack
    from oah.atomic import write_json_atomic
    from oah.design import task_context

    line, events, problem = task_context.parse_stream(raw)
    if usage is None:
        from oah.capabilities.usage import AvailableUsage

        available = AvailableUsage()
        for event in events:
            available.event(event)
        usage = available.record()
    observed = skillpack.observe_reads(events, root=pathlib.Path(prepared.cwd), bundle=bundle)
    record = skillpack.stream_receiving(prepared.run_id, bundle, observed,
                                        target={"engine": prepared.engine, "model": prepared.model,
                                                "effort": prepared.launch_effort},
                                        at=_iso(dt.datetime.now(dt.timezone.utc)), usage=usage)
    warnings, failure = [], None
    if record["reference_delivery"] != "complete":
        unread = [f"{item['skill']} {aux['reference']['path']}" for item, aux in skillpack.references(bundle)
                  if aux["required"] and not observed["whole"].get((item["n"], aux["path"]))]
        warnings.append(f"skills: run {prepared.run_id} did not read every required skill reference whole "
                        f"({', '.join(unread)}); its edits stay, but skill delivery is incomplete "
                        f"({skillpack.RECEIVING_NAME})")
    if normalize and problem is not None:
        failure = (f"OAH_ENGINE_FAILED: claude's stream-json output is incomplete ({problem}); this run does not "
                   "count")
    try:
        from oah.launchers import native

        native.valid_receiving(record, run_id=prepared.run_id)
        write_json_atomic(prepared.run.file_path(skillpack.RECEIVING_NAME), record)
    except OSError as exc:
        failure = (f"OAH_SKILL_RECEIVING_FAILED: the skill reads of run {prepared.run_id} could not be recorded "
                   f"({type(exc).__name__}); this run does not count. Check the run folder, then call again")
    if normalize:
        result = dataclasses.replace(result, stdout=line)
    return result, warnings, failure


def _start_reference(prepared: "Prepared", env: Mapping[str, str]):
    """Register the run's transport origin and install its DATA (SC4-P3) in the process that will own Popen; a
    refusal is recorded on the run and raised with nothing launched."""
    if prepared.engine != "claude" or not prepared.retain_worker_text:
        return None
    from oah.design import task_context
    from oah.design.model import DesignError
    from oah.lease import LeaseError

    reference = None
    try:
        saved = _read_design(prepared)
        plan = saved.get("transport") if saved is not None else None
        if plan is not None:
            lease = records.run_json(prepared.run, "lease.json", parse=_parse_lease_snapshot)
            reference = task_context.ReferenceRun(run_dir=prepared.run.path, task=prepared.task,
                run_id=prepared.run_id, root=pathlib.Path(prepared.cwd), argv=prepared.argv, stage=prepared.stage,
                cli_version=prepared.cli_version, env=env, tools=_argv_tools(prepared.argv),
                plan=plan, lease_snapshot=lease or {})
        if reference is not None:
            reference.prepare()
    except (DesignError, LeaseError, OSError) as exc:
        if reference is not None:
            reference.release()
        code = getattr(exc, "code", task_context.REFERENCE_UNAVAILABLE_CODE)
        text = str(exc)
        error = EngineError(code, text.split(": ", 1)[1] if text.startswith(code + ": ") else text)
        _record_refusal(prepared, error)
        raise error from None
    return reference


def _end_reference_failed(reference) -> list[str]:
    if reference is None:
        return []
    try:
        return reference.conclude_failed()
    except Exception as exc:  # accounting must never hide the original failure
        return [f"{type(exc).__name__}: the design data could not be accounted after the failure"]
    finally:
        reference.release()


@contextmanager
def _correction_cleanup():
    """Opt `oah fix` into stopping descendants before post-run accounting; ordinary calls stay unchanged."""
    token = _CORRECTION_CLEANUP.set(True)
    try:
        yield
    finally:
        _CORRECTION_CLEANUP.reset(token)


@contextmanager
def _deferred_creation_signals():
    """Defer installed stop handlers until Popen and Windows job assignment return ownership."""
    pending = []
    saved = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                handler = signal.getsignal(number)
                if callable(handler):
                    saved[number] = handler
                    signal.signal(number, lambda signum, frame: pending.append((signum, frame)))
    try:
        yield
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)
        if pending:
            number, frame = pending[0]
            saved[number](number, frame)


class _StreamFailed(subprocess.SubprocessError):
    code = "OAH_FIX_STREAM_FAILED"


class _CleanupFailed(subprocess.SubprocessError):
    code = "OAH_FIX_CLEANUP_FAILED"


class _PosixTree:
    """Remember reachable descendants across setsid/parent exit, without reading process command lines."""

    def __init__(self, child):
        self.child = child
        self.owned = {}
        self.stopped = threading.Event()
        self.error = True
        self.thread = None
        # Keep even initialization failures on identity-checked cleanup, never the PID/group fallback.
        child._oah_posix_tree = self
        info = self._process_info(child.pid)
        if info is not None:
            self.owned[child.pid] = info[2]
        self._scan(self._processes())
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.error = False
        try:
            self.thread.start()
        except Exception:
            self.error = True
            raise

    def _process_info(self, pid):
        """Read one PID's current birth identity, independently of descendant enumeration."""
        if sys.platform == "darwin":
            import ctypes
            import errno
            import struct

            # libproc's fixed proc_bsdinfo ABI supplies PPID, status and microsecond birth identity.
            # Read neither argv nor environment; process names in the struct are never decoded.
            api = getattr(self, "_api", None)
            if api is None:
                self._api = api = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                api.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                            ctypes.c_void_p, ctypes.c_int)
                api.proc_listchildpids.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
            info = ctypes.create_string_buffer(136)
            ctypes.set_errno(0)
            if api.proc_pidinfo(pid, 3, 0, info, len(info)) != len(info):  # PROC_PIDTBSDINFO
                if ctypes.get_errno() not in (errno.ESRCH, errno.ENOENT):
                    raise OSError  # Unreadable ownership metadata must not silently drop a live worker.
                return None  # exited/reaped between enumeration and inspection
            parent = struct.unpack_from("=I", info, 16)[0]
            status = {4: "T", 5: "Z"}.get(struct.unpack_from("=I", info, 4)[0], "R")
            return parent, status, struct.unpack_from("=QQ", info, 120)
        try:
            fields = pathlib.Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
            return int(fields[1]), fields[0].decode("ascii"), int(fields[19])
        except (FileNotFoundError, ProcessLookupError):
            return None

    def _processes(self):
        rows = {}
        if sys.platform == "darwin":
            import ctypes

            pending = set(self.owned) | {self.child.pid}
            seen = set()
            while pending:
                pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                info = self._process_info(pid)
                if info is None:
                    continue
                rows[pid] = info
                api = self._api
                size = api.proc_listchildpids(pid, None, 0)
                if size < 0:
                    raise OSError
                while True:
                    children = (ctypes.c_int * (size + 32))()
                    used = api.proc_listchildpids(pid, children, ctypes.sizeof(children))
                    if used < 0:
                        raise OSError
                    if used < len(children):
                        pending.update(child for child in children[:used] if child)
                        break
                    size = used * 2
            return rows
        # Linux test/development hosts expose the same numeric metadata in procfs.
        for directory in pathlib.Path("/proc").iterdir():
            if directory.name.isdigit():
                pid = int(directory.name)
                info = self._process_info(pid)
                if info is not None:
                    rows[pid] = info
        return rows

    def _scan(self, rows, *, initial=False):
        if initial and self.child.pid in rows:
            self.owned[self.child.pid] = rows[self.child.pid][2]
        live = {pid for pid, started in self.owned.items() if pid in rows and rows[pid][2] == started}
        while True:
            added = {pid for pid, (parent, _status, _started) in rows.items() if parent in live and pid not in live}
            if not added:
                return live
            live.update(added)
            self.owned.update((pid, rows[pid][2]) for pid in added)

    def _watch(self):
        while not self.stopped.wait(0.01):
            try:
                self._scan(self._processes())
            except Exception:
                self.error = True
                return

    def close(self):
        self.stopped.set()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join()

    def _signal(self, pids, number):
        for pid in pids:
            try:
                info = self._process_info(pid)
                if info is None:
                    continue
                if pid not in self.owned or info[2] != self.owned[pid]:
                    self.error = True
                    continue
                os.kill(pid, number)
            except ProcessLookupError:
                pass
            except Exception:
                self.error = True

    def kill(self):
        from oah.engines.proc import REAP_SECONDS

        try:
            self.close()
            # Freeze every reachable member before killing any parent, then enumerate again. This
            # prevents a surviving parent from creating more children or orphaning known descendants.
            deadline = time.monotonic() + REAP_SECONDS
            while True:
                rows = self._processes()
                live = self._scan(rows)
                active = {pid for pid in live if not rows[pid][1].startswith("Z")}
                self._signal(active, signal.SIGSTOP)
                if all("T" in rows[pid][1] for pid in active):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError
            self._signal(active, signal.SIGKILL)
            while True:
                rows = self._processes()
                active = {pid for pid in self._scan(rows) if not rows[pid][1].startswith("Z")}
                if not active:
                    break
                self._signal(active, signal.SIGKILL)
                if time.monotonic() >= deadline:
                    raise TimeoutError
                time.sleep(0.01)
            if self.error:
                raise RuntimeError
        except Exception:
            # Enumeration may fail while individual identities remain readable. Never signal a
            # remembered PID (or a process group) without re-establishing that member's ownership.
            self._signal(self.owned, signal.SIGKILL)
            raise _CleanupFailed("OAH_FIX_CLEANUP_FAILED: process-tree cleanup could not be verified; inspect "
                                 "running workers and the worktree before retrying") from None


def _windows_job_state(child) -> dict:
    import ctypes as ct

    job = getattr(child, "_oah_job", None)
    if job is None or job.handle is None or not getattr(child, "_handle", None):
        raise _CleanupFailed("OAH_FIX_CLEANUP_FAILED: the original Windows process and Job are not held")
    api = job.api
    for name, args, result in (
        ("QueryInformationJobObject", [ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_uint32,
                                       ct.POINTER(ct.c_uint32)], ct.c_int32),
        ("IsProcessInJob", [ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_int32)], ct.c_int32),
        ("GetProcessId", [ct.c_void_p], ct.c_uint32),
        ("GetProcessTimes", [ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_void_p], ct.c_int32),
    ):
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    process, member = int(child._handle), ct.c_int32()
    accounting, size = ct.create_string_buffer(48), ct.c_uint32()
    times = [ct.c_uint64() for _ in range(4)]
    if (api.GetProcessId(process) != child.pid or not api.IsProcessInJob(process, job.handle, ct.byref(member))
            or member.value != 1 or not api.GetProcessTimes(process, *(ct.byref(value) for value in times)) or
            not times[0].value or not api.QueryInformationJobObject(job.handle, 1, accounting, 48, ct.byref(size))
            or size.value != 48):
        raise _CleanupFailed("OAH_FIX_CLEANUP_FAILED: Windows process identity or Job accounting is unknown")
    import struct
    identity = {"pid": child.pid, "creation": times[0].value, "job_handle": int(job.handle)}
    digest = "sha256:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    old = getattr(child, "_oah_windows_identity", digest)
    if old != digest:
        raise _CleanupFailed("OAH_FIX_CLEANUP_FAILED: the original Windows process or Job was replaced")
    child._oah_windows_identity = digest
    return {"identity_digest": digest, "active": struct.unpack_from("<I", accounting.raw, 40)[0]}


def _kill_owned_tree(child):

    tree = getattr(child, "_oah_posix_tree", None)
    if tree is not None:
        tree.kill()
    elif os.name == "nt" and getattr(child, "_oah_job", None) is not None:
        from oah.engines.proc import REAP_SECONDS

        _windows_job_state(child)
        child._oah_job.terminate()
        child.wait(timeout=REAP_SECONDS)
        deadline = time.monotonic() + REAP_SECONDS
        while _windows_job_state(child)["active"] != 0:
            if time.monotonic() >= deadline:
                raise _CleanupFailed("OAH_FIX_CLEANUP_FAILED: the original Windows Job still has active processes")
            time.sleep(0.01)
    else:
        raise _CleanupFailed("OAH_FIX_CLEANUP_FAILED: process-tree cleanup could not be verified; inspect "
                             "running workers and the worktree before retrying")


@dataclasses.dataclass(frozen=True)
class _StreamedResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    worker_output: dict


def _stream_correction(child, prepared: Prepared, prompt: str, *, sink=None):
    """Drain and hash exact worker bytes without displaying or retaining worker text. A `sink` (EventSinkV1, DESIGN
    §6.5) is fed the same bytes and its stop request stops the owned tree; no correction row turns one on yet."""
    from oah.engines.proc import REAP_SECONDS

    streams = {name: {"bytes": 0, "sha256": hashlib.sha256()} for name in ("stdout", "stderr")}
    header = bytearray()
    failed = []
    lock, stopped = threading.Lock(), threading.Event()

    def drain(name, pipe):
        try:
            while data := pipe.read1(65536):
                streams[name]["bytes"] += len(data)
                streams[name]["sha256"].update(data)
                if name == "stderr":
                    header.extend(data)
                if sink is not None:
                    with lock:
                        if sink.feed(name, data) is not None:
                            stopped.set()
        except (OSError, ValueError):
            failed.append(True)
        finally:
            pipe.close()

    def feed():
        try:
            child.stdin.write(prompt.encode("utf-8"))
            child.stdin.close()
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=drain, args=(name, getattr(child, name)), daemon=True)
               for name in streams]
    writer = threading.Thread(target=feed, daemon=True)
    started = []
    timed_out = False
    try:
        for thread in (*readers, writer):
            thread.start()
            started.append(thread)
        deadline = time.monotonic() + prepared.timeout_s
        while child.poll() is None:
            if failed:
                raise _StreamFailed("OAH_FIX_STREAM_FAILED: output pipe failed; retry the correction")
            if sink is not None:
                with lock:
                    if sink.overdue() is not None:
                        stopped.set()
            if stopped.is_set():
                break  # the finally below stops the owned tree while the child is alive
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                child.wait(timeout=min(POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                pass
    finally:
        # Also stop descendants that outlived their launcher or still hold either output pipe open.
        _kill_owned_tree(child)
        child.wait(timeout=REAP_SECONDS)
        for thread in started:
            thread.join(timeout=REAP_SECONDS)
        output = {name: {"bytes": item["bytes"], "sha256": item["sha256"].hexdigest()}
                  for name, item in streams.items()}
        prepared.run.result(worker_output=output)
    if failed or any(thread.is_alive() for thread in started):
        raise subprocess.SubprocessError("OAH_FIX_STREAM_FAILED: output pipe failed; retry the correction")
    return _StreamedResult(None if timed_out else child.returncode, "", header.decode("utf-8", errors="replace"),
                           timed_out, output)


def _capability_sink(prepared: "Prepared", bundle: Mapping | None, plan):
    """EventSinkV1 for the one row where E itself switched Claude to stream-json: skill references to read and no
    design transport (DESIGN §6.5, §7.4). Every other row keeps its output unread by E; None there."""
    if bundle is None or "stream-json" not in prepared.argv:
        return None
    from oah.capabilities import inventory
    planned = {row["id"]: row for row in plan.get("selected", [])} if isinstance(plan, dict) else {}
    argv = list(prepared.argv)
    mode = argv[argv.index("--permission-mode") + 1] if "--permission-mode" in argv[:-1] else None
    return inventory.EventSink(run=prepared.run_id, cwd=prepared.cwd, cli_version=prepared.cli_version,
                               permission_mode=mode, planned=planned, bundle_id=bundle.get("digest"),
                               timeout_s=prepared.timeout_s)


def _drain_observed(child, prepared: Prepared, prompt: str, sink):
    """(RunResult, quiescent) for a run whose stream E reads live (DESIGN §6.5). One byte-drain thread per output pipe
    and one stdin writer through EOF replace communicate: stdout still reaches the result and header checks whole,
    and the same bytes feed the run's EventSinkV1. A stop request, a missing init after its deadline or the run
    timeout stops the owned tree at once, while the child is alive; the sink is finished once, after quiescence."""
    from oah.engines.proc import REAP_SECONDS, RunResult, decode_output

    chunks = {"stdout": [], "stderr": []}
    lock, wake, failed = threading.Lock(), threading.Event(), []

    def drain(name, pipe):
        try:
            while data := pipe.read1(65536):
                chunks[name].append(data)
                with lock:
                    if sink.feed(name, data) is not None:
                        wake.set()
        except (OSError, ValueError):
            failed.append(name)
        finally:
            pipe.close()
            wake.set()

    def write():
        try:
            child.stdin.write(prompt.encode("utf-8"))
        except (OSError, ValueError):
            pass  # a child that stops reading keeps its own result, as with communicate
        finally:
            try:
                child.stdin.close()
            except (OSError, ValueError):
                pass

    readers = [threading.Thread(target=drain, args=(name, getattr(child, name)), daemon=True) for name in chunks]
    threads = [*readers, threading.Thread(target=write, daemon=True)]
    started, stop, timed_out, decided = [], None, False, False
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
        deadline = time.monotonic() + prepared.timeout_s
        while True:
            with lock:
                stop = sink.overdue()
            if stop is not None or failed:
                break
            if child.poll() is not None and not any(thread.is_alive() for thread in readers):
                break  # exited, and both pipes reached EOF
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            wake.wait(timeout=min(POLL_SECONDS, remaining))
            wake.clear()
        decided = True
    finally:
        try:
            if not decided or stop is not None or failed or timed_out:
                _kill_owned_tree(child)  # B's tree/Job path, before anything else is concluded
            child.wait(timeout=REAP_SECONDS)
        finally:
            for thread in started:
                thread.join(timeout=REAP_SECONDS)
            quiescent = decided and len(started) == len(threads) and not any(t.is_alive() for t in started)
            returncode = None if timed_out else child.returncode
            with lock:
                sink.finish(returncode, quiescent)
    if failed:
        raise subprocess.SubprocessError("an output pipe of the engine failed; call the engine again")
    out, err = b"".join(chunks["stdout"]), b"".join(chunks["stderr"])
    return RunResult(returncode, decode_output(out), decode_output(err), timed_out), quiescent


def _run_prepared(prepared: Prepared, env: Mapping[str, str], prompt: str, *, sink=None):
    """Use proc's job/tree handling, holding the lease lock only through process creation and resume. A `sink`
    (EventSinkV1) reads the stream live through `_drain_observed` instead of communicate (DESIGN §6.5)."""
    from oah.engines.proc import (REAP_SECONDS, RunResult, UnsupportedBatchShim, _WindowsJob, decode_output,
                                  is_batch)

    if not prepared.argv:
        raise ValueError("empty command")
    if is_batch(prepared.argv[0]):
        raise UnsupportedBatchShim(prepared.argv[0], "batch files are never launched directly")
    job = _WindowsJob() if os.name == "nt" else None
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x4} if job is not None else
               {"start_new_session": True})
    child = None
    communicator = None
    draining = False
    reference = _REFERENCE_RUN.get()
    reference = reference if reference is not None and reference.run_id == prepared.run_id else None
    design_run = _DESIGN_RUN.get()
    inline_roots = None
    # P5-F: what the owning runner actually established, appended before the tree/Job below are released.
    outcome = {"kind": "aborted", "returncode": None, "timed_out": False, "quiescent": False}
    try:
        with _paid_dispatch(prepared), dispatch_guard(prepared, env=env) as live:
            if design_run and reference is None:
                from oah.design import task_context

                inline_roots = task_context.LiveRoots(task_context.current_role_roots(
                    live, prepared.run.path.parents[2], pathlib.Path(prepared.cwd)))
                if not inline_roots.current():
                    raise EngineError("OAH_RUN_LAUNCH_INVALID", "the actual design run role roots changed before launch")
            with _deferred_creation_signals():
                try:
                    child = subprocess.Popen(list(prepared.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, cwd=prepared.cwd, env=dict(env), **options)
                except OSError as exc:
                    # Windows omits the executable; keep spawn errors out of the lease's state-I/O wrapper.
                    raise subprocess.SubprocessError(f"cannot launch {prepared.argv[0]}: {exc}. "
                                                     "Check the executable path and permissions, then retry") from None
                boundaries.dispatched("engine")
                if reference is not None:
                    reference.child = child
                if job is not None:
                    try:
                        job.start(child)
                        _windows_job_state(child)
                    except BaseException:
                        child.kill()
                        child.communicate(timeout=REAP_SECONDS)
                        raise
                else:
                    child._oah_posix_tree = _PosixTree(child)
                if reference is not None:
                    reference.running(reference.held_store, child)
        if not prepared.retain_worker_text:
            streamed = _stream_correction(child, prepared, prompt, sink=sink)
            outcome.update(kind="failed" if streamed.timed_out or (sink is not None and sink.reason) else
                           "exit-observed", returncode=streamed.returncode, timed_out=streamed.timed_out,
                           quiescent=True)
            return streamed
        if sink is not None:
            draining = True
            result, quiescent = _drain_observed(child, prepared, prompt, sink)
            failed = result.timed_out or sink.reason is not None
            if not failed and (_CORRECTION_CLEANUP.get() or design_run):
                _kill_owned_tree(child)  # as below: no owned descendant keeps changing the tree after the run
            outcome.update(kind="failed" if failed else "exit-observed", returncode=result.returncode,
                           timed_out=result.timed_out, quiescent=quiescent)
            return result
        output, errors = [], []
        exchanged = threading.Event()

        def exchange():
            try:
                output.append(child.communicate(input=prompt.encode("utf-8")))
            except BaseException as exc:
                errors.append(exc)
            finally:
                exchanged.set()

        # A single communicate owns stdin through EOF. Retrying it after a polling timeout can
        # stop POSIX stdin delivery mid-prompt; poll the communication thread instead.
        deadline = time.monotonic() + prepared.timeout_s
        communicator = threading.Thread(target=exchange, daemon=True)
        communicator.start()
        while not exchanged.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_owned_tree(child)
                communicator.join(timeout=REAP_SECONDS)
                outcome.update(kind="failed", timed_out=True, quiescent=not communicator.is_alive())
                out, err = output[0] if output else (b"", b"")
                return RunResult(None, decode_output(out), decode_output(err), True)
            exchanged.wait(timeout=min(POLL_SECONDS, remaining))
        communicator.join(timeout=REAP_SECONDS)
        if errors:
            raise errors[0]
        out, err = output[0]
        if _CORRECTION_CLEANUP.get() or design_run:
            # A completed correction or design-bound run must leave no owned descendants changing the tree
            # during accounting (redirected background servers are stopped too).
            _kill_owned_tree(child)
            outcome["quiescent"] = True
        outcome.update(kind="exit-observed", returncode=child.returncode)
        return RunResult(child.returncode, decode_output(out), decode_output(err), False)
    except BaseException:
        # Ctrl-C or a stop signal: the worker has its own session or job, so it would keep running.
        # An exited launcher can leave descendants holding the pipes and modifying the worktree.
        if child is not None:
            _kill_owned_tree(child)
            outcome.update(kind="aborted", quiescent=True)
            try:
                if communicator is not None and communicator.ident is not None:
                    communicator.join(timeout=REAP_SECONDS)
                elif not draining:  # _drain_observed joins its own threads
                    child.communicate(timeout=REAP_SECONDS)
            except Exception:
                pass
        raise
    finally:
        if reference is not None:
            reference.finish_process(outcome)  # before the original tree/Job proof is released
        if child is not None and hasattr(child, "_oah_posix_tree"):
            child._oah_posix_tree.close()
        if job is not None:
            job.close()
        if inline_roots is not None:
            inline_roots.close()


_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); import oah.call as c; "
         "raise SystemExit(getattr(c, sys.argv[2])(sys.argv[3]))")
LAUNCH_TIMEOUT = 30.0


def _boot_argv(entry: str, run_dir: str) -> list[str]:
    package_root = str(pathlib.Path(__file__).resolve().parent.parent)
    return [sys.executable, "-c", _BOOT, package_root, entry, run_dir]


@boundaries.entry("launch")
def start_background(prepared: Prepared, *, base_env: Mapping[str, str] | None = None,
                     require_detach: bool = False) -> str:
    """Start a detached runner for a prepared call and return its run id at once.

    A short-lived launcher starts the runner in its own session (POSIX) or as a detached process
    (Windows, breaking away from the caller's job when allowed) and exits, so the caller keeps no child.
    """
    run = prepared.run
    from oah.atomic import write_json_atomic

    check_prepared(prepared)
    write_json_atomic(run.launch_path, dict(prepared.to_json(), require_detach=require_detach))
    run.result(status="queued")
    env = dict(os.environ if base_env is None else base_env)
    launched = subprocess.run(_boot_argv("launch_main", prepared.run_dir), stdin=subprocess.DEVNULL,
                              capture_output=True, cwd=prepared.cwd, env=env, timeout=LAUNCH_TIMEOUT)
    text = launched.stdout.decode("utf-8", errors="replace").strip()
    detail = launched.stderr.decode("utf-8", errors="replace").strip()
    if launched.returncode != 0 or not text.isdigit():
        refusal = _launcher_refusal(text)
        if refusal is not None:
            if refusal.dispatched is not None:
                boundaries.dispatched(refusal.dispatched)
                raise refusal
            saved = records.run_json(run, "result.json")
            if not isinstance(saved, dict) or saved.get("status") != "refused":
                _record_refusal(prepared, refusal)
            raise refusal
        run.result(status="failed", error=f"OAH_RUN_LAUNCH_FAILED: {detail or 'no runner pid'}")
        raise EngineError("OAH_RUN_LAUNCH_FAILED", f"the background runner did not start ({detail or 'no pid'}); "
                          "run the same command without --background")
    boundaries.dispatched("runner")
    if detail:
        print(detail, file=sys.stderr)
    run.result(status="queued", pid=int(text))
    return prepared.run_id


def _windows() -> bool:
    return os.name == "nt"


def _background_run(run_dir: str) -> runs.Run:
    from oah.discovery.runfiles import Run

    path = pathlib.Path(run_dir)
    return Run(id=path.name, task=path.parent.name, path=path)


@boundaries.entry("launch")
def launch_main(run_dir: str) -> int:
    try:
        return _launch_main(run_dir)
    except (EngineError, StorageError) as exc:
        exit_code = getattr(exc, "exit_code", 69 if isinstance(exc, StorageError) else 1)
        message = str(exc).removeprefix(exc.code + ": ")
        if len(message) > 8192:
            message = message[:8192] + "... (diagnostic shortened)"
        print(json.dumps({"version": 1, "code": exc.code, "exit": exit_code, "message": message,
                          "dispatched": boundaries.dispatch_kind()}, ensure_ascii=True))
        print(exc, file=sys.stderr)
        return exit_code


def _launcher_refusal(text: str) -> boundaries.CommandRefusal | None:
    if len(text.encode("utf-8")) > 131_072:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not (isinstance(data, dict) and set(data) == {"version", "code", "exit", "message", "dispatched"}
            and data["version"] == 1 and type(data["exit"]) is int and data["exit"] in (1, 69)
            and isinstance(data["code"], str) and re.fullmatch(r"OAH_[A-Z_]+(?:\([a-z-]*\))?", data["code"])
            and isinstance(data["message"], str)):
        return None
    happened: boundaries.Dispatch | None
    match data["dispatched"]:
        case "engine":
            happened = "engine"
        case "runner":
            happened = "runner"
        case None:
            happened = None
        case _:
            return None
    return boundaries.CommandRefusal(data["code"], data["message"], data["exit"], dispatched=happened)


def _launch_main(run_dir: str) -> int:
    prepared = None
    try:
        with boundaries.command("launch"):
            """Entry point of the launcher: start the runner detached, print its pid and exit."""
            options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                             "close_fds": True}
            from oah.discovery import records

            run = _background_run(run_dir)
            loaded = records.run_json(run, "launch.json", parse=lambda row: (row, Prepared.from_json(row)))
            assert loaded is not None
            saved, prepared = loaded
            require_detach = saved.get("require_detach", False)
            argv = _boot_argv("background_main", run_dir)
            if _windows():
                flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                try:
                    with _paid_dispatch(prepared, record=False), dispatch_guard(prepared):
                        child = subprocess.Popen(argv, creationflags=flags | 0x01000000, **options)  # CREATE_BREAKAWAY_FROM_JOB
                        boundaries.dispatched("runner")
                except StorageError:
                    raise
                except OSError as exc:
                    if require_detach:
                        raise boundaries.CommandRefusal("OAH_RUN_DETACH_FAILED", f"Windows refused job breakaway "
                            f"({exc}); retry without --require-detach, or use a terminal whose job permits breakaway", 69) from None
                    warning = (f"OAH_RUN_DETACH_UNVERIFIED: Windows refused job breakaway ({exc}); closing this terminal "
                               "may stop the run. Keep it open and use oah wait, or retry with --require-detach "
                               "in a terminal whose job permits breakaway")
                    run.result(detached=runs.UNVERIFIED, warnings=[*run.read().get("warnings", []), warning])
                    print(warning, file=sys.stderr, flush=True)
                    with _paid_dispatch(prepared, record=False), dispatch_guard(prepared):
                        child = subprocess.Popen(argv, creationflags=flags, **options)
                        boundaries.dispatched("runner")
                else:
                    run.result(detached="verified")
            else:
                with _paid_dispatch(prepared, record=False), dispatch_guard(prepared):
                    child = subprocess.Popen(argv, start_new_session=True, **options)
                boundaries.dispatched("runner")
                run.result(detached="verified")
            print(child.pid, flush=True)
            child.returncode = 0  # the runner outlives this launcher on purpose; nothing waits for it here
            return 0

    except EngineError as exc:
        if prepared is not None:
            _record_refusal(prepared, exc)
        raise


def background_main(run_dir: str) -> int:
    """Entry point of the detached runner."""
    from oah.discovery import records

    try:
        with boundaries.command("launch"):
            prepared = records.run_json(_background_run(run_dir), "launch.json", parse=Prepared.from_json)
            assert prepared is not None
            execute(prepared)
    except (EngineError, StorageError) as exc:
        print(exc, file=sys.stderr)
        return getattr(exc, "exit_code", 69 if isinstance(exc, StorageError) else 1)
    return 0


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = (wintypes.HANDLE,), wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@boundaries.entry("wait")
def wait(state_dir: pathlib.Path, run_id: str, *, timeout: float = WAIT_LIMIT_S, poll: float = 0.2,
         deadline: float | None = None) -> dict:
    """The final or current record after `timeout` (capped at 9 minutes).

    An optional entry deadline also bounds storage retries and lost-result publication, even when
    timeout is zero. Without it, storage keeps its existing retry budgets.
    """
    from oah.discovery.runfiles import Run

    saved = runs.find_run(state_dir, run_id, deadline=deadline)
    run = Run(saved.id, saved.task, saved.path, deadline=deadline)
    stop = time.monotonic() + min(max(timeout, 0.0), WAIT_LIMIT_S)
    if deadline is not None:
        stop = min(stop, deadline)
    data = None
    try:
        while True:
            data = run.read()
            if data.get("status") in runs.TERMINAL:
                return data
            pid = data.get("pid")
            if data.get("status") in ("queued", "running") and isinstance(pid, int) and not _alive(pid):
                data = run.read()
                if data.get("status") not in runs.TERMINAL:
                    return run.result(status="lost", error=f"OAH_RUN_LOST: the runner (pid {pid}) exited without "
                                      "recording a result; read the run's logs and call the engine again")
                return data
            if time.monotonic() >= stop:
                return data
            time.sleep(poll if deadline is None else min(poll, max(stop - time.monotonic(), 0.0)))
    except launchers.DeadlineExpired:
        if data is None:
            raise
        return data  # Leave lost-result publication for the next wait when the budget expires.
