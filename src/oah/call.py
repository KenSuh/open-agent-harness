# 엔진 호출 핵심 — 단계·난이도로 엔진·단·모델·effort 를 정하고, 브리프와 실행 폴더를 만든 뒤 proc 로 띄우거나 백그라운드로 넘기며, 실제로 돈 값을 기록한다.
"""`oah call` and `oah wait` without the command line (PLAN 5.0, 5.5, A1, A6, A10, A14).

`prepare` decides everything before launch and writes the run folder:
- the stage's engine (`OAH_STAGE_ENGINE_MISMATCH` unless overridden, which is logged as an event);
- the tier: `--tier` (source `flag`), else the packet's `## Plan` line for this stage (`packet`), else the
  rule-scored axes through `routing.recommend` (`rules`, or `flag` when axes were given). The re-ranker's
  advisory judgement is recorded next to it and never changes the tier;
- the model through the catalog (`models.map_tier`) and the launch effort through the adapter;
- the judged model and effort (`judge_engine`, amendment 6): unless a tier model is explicitly pinned,
  an enabled re-ranker with a key picks one pair from
  `models.build_candidates`; the pair is used only when `CandidateSet.reject_reason` accepts it, and
  the record keeps `model_judge` (`used`, `rejected(<rule>)` or `unused(<reason>)`) and `model_choice`
  (with the candidate `mode`, the pool or allowed models left out and why, and why the model is not judged);
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
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Callable, Mapping, Sequence

import oah.intent as oah_intent
import oah.packet as oah_packet
from oah import briefs, launchers, models, routing, runs, skillsel
from oah.engines import claude, codex
from oah.engines.effort import (ORDINARY, EngineError, HeaderCheck, compare_labels, highest, load_rules, rank,
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
_CORRECTION_STATE_LOCK = ContextVar("oah_correction_state_lock", default=None)
_CORRECTION_PUBLICATION = ContextVar("oah_correction_publication", default=None)
DEFAULT_TASK = "adhoc"
MODEL_MISMATCH = "OAH_MODEL_MISMATCH"
STAGE_DEFAULTS = {"initial": "claude", "review": "codex", "correction": "codex"}
TIMEOUTS = {"low": 15, "medium": 30, "high": 60, "max": 90}
ENGINE_DEFAULTS = {
    "claude": {
        "command": ["claude"],
        "quality_order": ["opus", "sonnet", "haiku"],
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
_PLAN_LINE = re.compile(r"^\s*(?:[-*]\s*)?`?(initial|review|correction)`?\s*(?:→|->|:)\s*`?(claude|codex)`?\s*"
                        r"(?:→|->|:)\s*`?(low|medium|high|max)`?\b")
_PLAN_AXES = re.compile(r"^\s*[-*]\s*(initial|review|correction):\s+kind=(\S+)\s+intensity=(\S+)\s+"
                        r"reasoning=(\S+)\s+risk=(\S+)(?:\s|$)")


@dataclasses.dataclass(frozen=True)
class Settings:
    stages: dict
    engines: dict
    models: dict

    @classmethod
    def defaults(cls) -> "Settings":
        return cls.validated(dict(STAGE_DEFAULTS), json.loads(json.dumps(ENGINE_DEFAULTS)), dict(models.DEFAULTS))

    @classmethod
    def validated(cls, stages: dict, engines: dict, models_section: dict) -> "Settings":
        def invalid(message: str) -> EngineError:
            return EngineError("OAH_CONFIG_INVALID", f".oah/config.toml: {message}")

        for stage, engine in stages.items():
            if engine not in routing.ENGINES:
                raise invalid(f"stages.{stage} = {engine!r}; use one of: {', '.join(routing.ENGINES)}")
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


class _CorrectionCatalog(models.Catalog):
    """Resolve against captured bytes; start publication compares them before writing the plan."""

    def __init__(self, state_dir):
        super().__init__(state_dir)
        check = _CORRECTION_INPUT_CHECK.get()
        self.original = {}
        self.values = {}
        self.writes = {}
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

    def record_resolution(self, engine, tier, **fields):
        fields["why"] = "OAH_FIX_MODEL_RESOLUTION"
        data = self.resolved()
        old = data.get(engine, {}).get(tier, {}) if isinstance(data.get(engine), dict) else {}
        stamp = _iso(fields.get("now") or models.utc_now())
        changes = [{"ts": stamp, "engine": engine, "tier": tier, "field": field, "old": old.get(field),
                    "new": fields[field], "why": fields["why"]}
                   for field in ("model", "effort") if old.get(field) != fields[field]]
        if changes:
            text = self.original[self.changes_path] + "".join(
                json.dumps(change, ensure_ascii=False, sort_keys=True) + "\n" for change in changes)
            _CORRECTION_INPUT_CHECK.get()(text)
            self.writes[self.changes_path] = text
        if changes or old.get("source") != fields["source"]:
            data.setdefault(engine, {})[tier] = {key: fields[key] for key in ("model", "effort", "source")}
            data[engine][tier]["at"] = stamp
            self._plan_write(self.resolved_path, data)
        return changes


def _catalog(state_dir):
    inputs = _CORRECTION_INPUTS.get()
    return inputs.catalog if inputs is not None else models.Catalog(state_dir)


def check_catalog(engine: str, settings: Settings, *, state_dir: pathlib.Path, env: Mapping[str, str],
                  refresh: bool = False, now: dt.datetime | None = None) -> tuple[list[str], models.Freshness]:
    """The engine's argv prefix and its catalog entry, after the freshness check that runs before every launch."""
    base = models.resolve_base(engine, settings.engines[engine]["command"])
    freshness = _catalog(state_dir).ensure_fresh(engine, base=base, env=env,
                                                       ttl_hours=settings.models["ttl_hours"], refresh=refresh, now=now)
    return base, freshness


def _effective(engine: str, root: pathlib.Path, env: Mapping[str, str]) -> str | None:
    return codex.effective_model(root, codex.codex_home(env)) if engine == "codex" else None


def _deterministic(engine: str, tier: str, settings: Settings, entry: models.Entry | None,
                   effective: str | None):
    """(model, source, effort resolution) for one tier: A14's mapping, then A1's effort for that model."""
    section = settings.engines[engine]
    tier_settings = section["tiers"][tier]
    model, source = models.map_tier(engine, tier, pin=tier_settings["model"], quality_order=section["quality_order"],
                                    entry=entry, effective=effective)
    info = entry.model(model) if entry else None
    levels = info.efforts if info and info.efforts else None
    if engine == "codex":
        cache = {model: codex.CachedModel(model, levels, info.default)} if levels else None
        effort = codex.resolve_effort(model, tier_settings["effort"], cache=cache)
    else:
        effort = claude.resolve_effort(tier_settings["effort"], efforts=levels, model=model)
    return model, source, effort


def resolve_engine(engine: str, tier: str, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
                   env: Mapping[str, str], refresh: bool = False, now: dt.datetime | None = None,
                   checked: tuple[list[str], models.Freshness] | None = None) -> Resolution:
    """Model and effort for one engine and tier; the change log records anything that differs from last time."""
    section = settings.engines[engine]
    tier_settings = section["tiers"][tier]
    base, freshness = checked or check_catalog(engine, settings, state_dir=state_dir, env=env, refresh=refresh, now=now)
    catalog = _catalog(state_dir)
    entry = freshness.entry
    effective = _effective(engine, root, env)
    check = _CORRECTION_INPUT_CHECK.get()
    if check is not None:
        check((dataclasses.asdict(freshness), effective))
    fresh = bool(entry.models) and entry.checked_at is not None and not any(
        warning.startswith("OAH_MODELS_REFRESH_FAILED") for warning in freshness.warnings)
    cap_warnings = models.check_caps(engine, section["effort_caps"], section["allowed_models"], entry, fresh=fresh,
                                     pools=section["tier_pools"])
    model, source, effort = _deterministic(engine, tier, settings, entry, effective)
    cap = section["effort_caps"].get(model)
    if cap is not None:
        if rank(effort.launch) is None:
            raise EngineError("OAH_CONFIG_INVALID", f"[engines.{engine}.effort_caps] cannot bound effort "
                              f"{effort.launch} for {model}; set an ordinary tier effort or remove the cap in "
                              ".oah/config.toml")
        if rank(effort.launch) > rank(cap):
            launch = highest([level for level in effort.levels or (cap,)
                              if rank(level) is not None and rank(level) <= rank(cap)])
            if launch is None:
                raise EngineError("OAH_CONFIG_INVALID", f"[engines.{engine}.effort_caps] leaves no supported "
                                  f"effort for {model}; raise or remove its cap in .oah/config.toml")
            cap_warnings.append(f"OAH_EFFORT_CAPPED: {model} effort {effort.launch} is capped to {launch}; "
                                f"raise or remove its [engines.{engine}.effort_caps] entry in .oah/config.toml "
                                "to permit a higher effort")
            effort = dataclasses.replace(effort, launch=launch)
    why = f"{source} mapping for tier {tier} with {engine} CLI {freshness.launch_version or 'unknown'}"
    if freshness.refreshed:
        why += f"; catalog rebuilt ({freshness.refreshed})"
    changes = catalog.record_resolution(engine, tier, model=model, effort=effort.launch, source=source, why=why,
                                        now=now)
    return Resolution(engine=engine, tier=tier, base=tuple(base), cli_version=freshness.launch_version,
                      requested_model=tier_settings["model"], model=model, model_source=source,
                      requested_effort=effort.requested, launch_effort=effort.launch,
                      timeout_s=float(tier_settings["timeout_min"]) * 60,
                      warnings=(*freshness.warnings, *cap_warnings), changes=tuple(changes))


def _resolve_floor(resolution: Resolution, settings: Settings, *, floor_tier: str, entry: models.Entry | None,
                   root: pathlib.Path, env: Mapping[str, str]) -> models.Pair:
    """Resolve the uncapped floor and check the launch cap against it, for calls and model reports."""
    engine = resolution.engine
    cap = settings.engines[engine]["effort_caps"].get(resolution.model)
    try:
        model, _, effort = _deterministic(engine, floor_tier, settings, entry, _effective(engine, root, env))
    except EngineError:
        if cap is not None:
            raise EngineError("OAH_CONFIG_INVALID", f"[engines.{engine}.effort_caps] cannot be checked against "
                              f"the {floor_tier} floor; set a resolvable floor model and effort in .oah/config.toml") from None
        raise
    if cap is not None and (rank(effort.launch) is None or rank(cap) < rank(effort.launch)
                            or rank(resolution.launch_effort) < rank(effort.launch)):
        raise EngineError("OAH_CONFIG_INVALID", f"[engines.{engine}.effort_caps] for {resolution.model} cannot "
                          f"meet the {floor_tier} floor effort {effort.launch}; raise or remove its cap "
                          "in .oah/config.toml before calling again")
    return models.Pair(model, effort.launch)


def judge_engine(resolution: Resolution, settings: Settings, *, stage: str, axes: routing.Axes, pinned: bool,
                 entry: models.Entry | None, root: pathlib.Path, env: Mapping[str, str], brief_disabled: bool = False,
                 keychain=None) -> tuple[Resolution, dict]:
    """The call's model and effort after the re-ranker's judgement, and the `model_judge`/`model_choice` record.

    The deterministic resolution is kept unless the judged pair passes `CandidateSet.reject_reason`.
    A plugin failure never blocks a launch: it is recorded as `unused(error: <type>)`."""
    engine, tier = resolution.engine, resolution.tier
    deterministic = models.Pair(resolution.model, resolution.launch_effort)
    choice = {"deterministic": dict(deterministic.to_json(), source=resolution.model_source), "floor": None,
              "mode": None, "note": None, "candidates": [], "excluded": {}, "proposal": None, "provider": None,
              "requested": deterministic.to_json()}

    def record(status: str, chosen: Resolution = resolution) -> tuple[Resolution, dict]:
        choice["requested"] = {"model": chosen.model, "effort": chosen.launch_effort}
        check = _CORRECTION_INPUT_CHECK.get()
        if check is not None:
            check(choice)
        return chosen, {"model_judge": status, "model_choice": choice}

    section = settings.engines[engine]
    cap = section["effort_caps"].get(resolution.model)
    if pinned and cap is None:
        return record("unused(pinned)")
    floor_tier = routing.floor_tier(axes.kind)
    try:
        floor = _resolve_floor(resolution, settings, floor_tier=floor_tier, entry=entry, root=root, env=env)
    except EngineError:
        if cap is not None:
            raise
        return record("unused(floor-unresolved)")
    if pinned:
        return record("unused(pinned)")
    candidates = models.build_candidates(
        engine, entry=entry, deterministic=deterministic, tier=tier, floor_tier=floor_tier,
        floor=floor, quality_order=section["quality_order"],
        allowed_models=section["allowed_models"], effort_caps=section["effort_caps"],
        tier_pools=section["tier_pools"])
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
                                    env=env, keychain=keychain)
    except Exception as exc:  # a plugin failure must never block a launch
        return record(f"unused(error: {type(exc).__name__})")
    choice["provider"] = judged.provider
    if judged.status != "used" or judged.proposal is None:
        return record(judged.status)
    model, effort = judged.proposal
    choice["proposal"] = {"model": model, "effort": effort}
    rule = candidates.reject_reason(model, effort)
    if rule is not None:
        return record(f"rejected({rule})")
    return record("used", dataclasses.replace(resolution, model=model, model_source="judge", launch_effort=effort))


@dataclasses.dataclass(frozen=True)
class Packet:
    source: str  # repository-relative, with forward slashes
    intent: object | None  # has .section, .id, .revision, .digest
    section: str | None  # the intent section exactly as the source holds it
    scope: tuple[str, ...]
    plan: dict  # stage -> (engine, tier)
    rerank_disabled: bool = False  # the packet's Constraints turn the re-ranker off
    axes: dict[str, routing.Axes] = dataclasses.field(default_factory=dict)


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
    text = path.read_bytes().decode("utf-8")  # bytes, so CRLF stays byte-intact
    return text[1:] if text.startswith("\ufeff") else text


def read_packet(root: pathlib.Path, path: pathlib.Path, *, parse_intent: Callable[[str], object] | None,
                read_source: Callable[[pathlib.Path], str] | None = None) -> Packet:
    """The parts of a packet a call needs. oah.intent (lane P4a) reads the source safely and parses it."""
    try:
        text = (read_source or _read_text)(path)
    except Exception as exc:
        code = getattr(exc, "code", "OAH_PACKET_UNREADABLE")
        message = str(exc).split(": ", 1)[1] if str(exc).startswith(f"{code}: ") else str(exc)
        raise EngineError(code, f"{message}; check the path given with --packet") from exc
    try:
        intent = parse_intent(text) if parse_intent is not None else None
    except Exception as exc:
        raise EngineError("OAH_PACKET_INTENT_INVALID", f"{path}: {exc}; fix the packet's OAH_INTENT section "
                          "(`oah packet check` lists the problems)") from exc
    scope = []
    for line in _section_lines(text, "Scope"):
        match = re.match(r"\s*[-*]\s+`?([^`\s]+)`?", line)
        if match:
            scope.append(match.group(1))
    plan: dict = {}
    axes: dict[str, routing.Axes] = {}
    for line in _section_lines(text, "Plan"):
        match = _PLAN_LINE.match(line)
        if match:
            plan[match.group(1)] = (match.group(2), match.group(3))
        recorded = _PLAN_AXES.match(line)
        if recorded:
            values = routing.Axes(*recorded.groups()[1:])
            routing.recommend(values)  # validate the recorded values before they reach selection
            axes[recorded.group(1)] = values
    try:
        source = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        source = str(path)
    section = briefs.raw_section(text) if intent is not None else None
    return Packet(source=source, intent=intent, section=section, scope=tuple(scope), plan=plan,
                  rerank_disabled=oah_packet.rerank_disabled(text), axes=axes)


BINDING_NAME = "intent-binding.json"
SOURCE_MISMATCH = "OAH_INTENT_SOURCE_MISMATCH"


def _binding_path(state_dir: pathlib.Path, task: str) -> pathlib.Path:
    runs.task_directory(state_dir, task)
    return runs._contained_path(state_dir, "runs", task, BINDING_NAME)


def dispatch_binding(state_dir: pathlib.Path, task: str) -> dict | None:
    """The intent source bound at the task's first dispatch, or None."""
    from oah.atomic import read_json

    path = _binding_path(state_dir, task)
    data = (runs._captured_state(path)[1] if runs._CORRECTION_CAPTURE.get() is not None else read_json(path, None))
    return data if isinstance(data, dict) else None


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
            run = runs.Run(id=run_id, task=task, path=runs._contained_path(state_dir, "runs", task, run_id))
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
        # Both the working and committed intent were checked while capturing this snapshot.
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
def dispatch_guard(prepared: Prepared):
    """Revalidate the prepared lease and source under the state lock, through a short launch only."""
    from oah.lease import LeaseError, Store

    run = prepared.run
    publication = _CORRECTION_PUBLICATION.get()
    if publication is not None and publication.plan.prepared == prepared:
        # Fix authorized this immutable lease at start publication and owns the task review lock.
        publication.budget_check()
        yield
        return
    try:
        saved = run._read_json("lease.json")
        if not (isinstance(saved, dict) and isinstance(saved.get("id"), str) and saved["id"]
                and type(saved.get("generation")) is int and saved["generation"] >= 0):
            raise EngineError("OAH_RUN_LAUNCH_INVALID", "the run has no valid checked lease snapshot; "
                              "run oah call again to prepare it from the current lease")
        try:
            oah_intent.Binding.from_json(saved.get("intent"))
        except ValueError:
            raise EngineError("OAH_RUN_LAUNCH_INVALID", "the run has no valid checked intent binding; "
                              "run oah call again to prepare it from the current lease") from None
        with Store(run.path.parents[2]).checked(task=prepared.task, lease_id=saved["id"],
                                               generation=saved["generation"]) as live:
            _check_dispatch_lease(live, pathlib.Path(prepared.cwd), stage=prepared.stage)
            from oah.review import rounds
            _task_dispatch_gate(run.path.parents[2], prepared.task, pathlib.Path(live.worktree), stage=prepared.stage,
                                record=rounds._PAID_SPAWN.get(), run_id=prepared.run_id)
            yield
    except (LeaseError, EngineError) as exc:
        error = EngineError(exc.code, str(exc).split(": ", 1)[1] if prepared.retain_worker_text else "")
        _record_refusal(prepared, error)
        raise error from None


def _record_refusal(prepared: Prepared, error: EngineError) -> None:
    """Mark only the caller's own run refused; a queued Orca run belongs to the worker that claims it."""
    run = prepared.run
    if isinstance(run, runs.WorkerRun):
        data = run.read()
        if data.get("status") != "running" or data.get("pid") != os.getpid():
            if error.code == "OAH_REVIEW_SERIES_BUSY":
                return  # the lock holder may be the worker that claims this run next
            try:
                os.close(run._open_file("worker.claim", os.O_WRONLY | os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                return  # another worker claimed this run; its record is not ours to change
    run.result(status="refused", error=_recorded_error(error, prepared.retain_worker_text))


def check_prepared(prepared: Prepared) -> None:
    """Check a queue/launcher handoff; execute checks again at the engine's actual spawn."""
    with _paid_dispatch(prepared, record=False), dispatch_guard(prepared):
        pass


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

    @property
    def run(self) -> runs.Run:
        publication = _CORRECTION_PUBLICATION.get()
        if publication is not None and publication.plan.prepared == self:
            return publication.run
        return runs.Run(id=self.run_id, task=self.task, path=pathlib.Path(self.run_dir))

    def to_json(self) -> dict:
        data = dataclasses.asdict(self)
        if self.retain_worker_text:
            data.pop("retain_worker_text")  # Preserve the landed launch schema for ordinary calls and Orca.
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
    initial_record: dict | None = None
    model_writes: tuple[tuple[pathlib.Path, str], ...] = ()
    binding_record: str | None = None
    lease_record: str | None = None
    dispatch_record: str | None = None


@dataclasses.dataclass(frozen=True)
class _CorrectionSnapshot:
    lease_json: str
    inputs_json: str
    plan_json: str


@dataclasses.dataclass(frozen=True)
class _CorrectionRun(runs.Run):
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
    snapshot: _CorrectionSnapshot | None = None
    plan: _CallPlan | None = None
    run: _CorrectionRun | None = None
    artifacts: dict = dataclasses.field(default_factory=dict)

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
        # Only start publication depends on model inputs, including when a later write fails.
        for path in self.catalog.original:
            runs._CORRECTION_CAPTURE.get().pop(path, None)
        for path, text in plan.model_writes:
            write_text_atomic(path, text)
        runs._create_run(self.run, plan.initial_record)
        self.run.write_prompt(plan.prompt)
        if plan.skills_record is not None:
            write_json_atomic(self.run.file_path("skills.json"), plan.skills_record)
        self.run.result(**plan.result_fields)
        if plan.binding_record is not None:
            write_text_atomic(_binding_path(self.state, self.run.task), plan.binding_record)
            runs._CORRECTION_CAPTURE.get()[_binding_path(self.state, self.run.task)] = plan.binding_record.encode("utf-8")
        write_text_atomic(self.run.file_path("lease.json"), plan.lease_record)


def advise(axes: routing.Axes, task: str, *, root: pathlib.Path, brief_disabled: bool = False,
           env: Mapping[str, str] | None = None, keychain=None) -> str:
    """The re-ranker's advisory difficulty status (lane P3s); it never changes the tier."""
    try:
        rerank = importlib.import_module("oah.rerank")
    except ImportError:
        return "unused(not-configured)"
    try:
        inputs = _CORRECTION_INPUTS.get()
        config = {"config": inputs.config.section("rerank", rerank.DEFAULTS)} if inputs is not None else {}
        advice = rerank.advise_difficulty(axes, task, root=root, brief_disabled=brief_disabled, env=env, **config,
                                          keychain=keychain)
    except Exception as exc:  # a plugin failure must never block a launch
        return f"unused(error: {type(exc).__name__})"
    status = str(getattr(advice, "status", "unused(no status)"))
    tier = getattr(advice, "advisory_tier", None)
    return f"{status} tier={tier}" if status == "used" and tier else status


def _lease_scope(state_dir: pathlib.Path, task: str) -> tuple[str, ...]:
    from oah.lease import Store

    for lease, stale in Store(state_dir).snapshot():
        if lease.task == task and not stale:
            return tuple(lease.scope)
    return ()


@contextmanager
def _correction_inputs(check, *, state, lease_json, bound, packet, config, metadata_json, max_rounds=None,
                       budget_check=None):
    """Check correction brief inputs before creating a run; other callers keep their landed behavior."""
    token = _CORRECTION_INPUT_CHECK.set(check)
    try:
        inputs = _CorrectionInputs(state, lease_json, json.dumps(bound), packet, _CorrectionCatalog(state), config,
                                   metadata_json, max_rounds=max_rounds, budget_check=budget_check)
        snapshot = _CORRECTION_INPUTS.set(inputs)
        try:
            yield inputs
        finally:
            _CORRECTION_INPUTS.reset(snapshot)
    finally:
        _CORRECTION_INPUT_CHECK.reset(token)


def _brief_text(request, run_id, packet, scope, stored, footer, has_footer, warnings, selection, checked_lease):
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
                        skill_ids=[skill.id for skill in chosen.skills] if chosen is not None else ())


def _brief_skills(root, *, kind, stage, risk, scope, state_dir, env, keychain, brief_disabled, now):
    check = _CORRECTION_INPUT_CHECK.get()
    if check is None:
        return skillsel.choose(root, kind=kind, stage=stage, risk=risk, scope=scope, state_dir=state_dir,
                               env=env, keychain=keychain, brief_disabled=brief_disabled, now=now)
    from oah import rerank

    settings = _CORRECTION_INPUTS.get().config.section("skills", skillsel.config_defaults())
    kit = skillsel.kit_roots(state_dir, pins=settings["pins"])
    roots = skillsel.default_roots(root, home=skillsel.home_of(env), env=env, kit_cache=kit)
    # Corrections omit optional telemetry so even an unsafe attached skill leaves no selection record.
    selection = skillsel.select(kind, stage, risk, scope, roots=roots, settings=settings, state_dir=None,
                                repo_root=root, now=now)
    check(selection.brief_text())
    check(selection.to_record())
    config = _CORRECTION_INPUTS.get().config.section("rerank", rerank.DEFAULTS)
    return skillsel.rerank_skills(selection, config=config, query=f"kind={kind} stage={stage} risk={risk}",
                                 brief_disabled=brief_disabled, env=env, keychain=keychain)


def prepare(request: CallRequest, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
            env: Mapping[str, str], footer: str, has_footer: Callable[[str], bool],
            parse_intent: Callable[[str], object] | None = None,
            read_source: Callable[[pathlib.Path], str] | None = None, now: dt.datetime | None = None,
            keychain=None) -> Prepared:
    """Serialize task authorization and all optional paid preparation, before handing off a reserved run."""
    from oah.lockfile import LockTimeout, StateLock
    from oah.review import receipts, rounds

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
    planned_run = None
    if inputs is None:
        from oah.lease import Store

        planned_run = _PREPARATION_RUN.get() or runs._plan_run(
            state_dir, request.task, request.engine, request.stage, now=now)
        with Store(state_dir).checked(task=request.task, lease_id=holder.id, generation=holder.generation) as live:
            _check_dispatch_lease(live, root, stage=request.stage)
            _task_dispatch_gate(state_dir, request.task, pathlib.Path(live.worktree), stage=request.stage,
                                reserve=True, run_id=planned_run[0].id)
    axes = scored.axes if recorded is not None else None

    def check_budget():
        from oah.lease import LeaseError

        if inputs is not None:
            inputs.budget_check()
        else:
            try:
                with Store(state_dir).checked(task=request.task, lease_id=holder.id, generation=holder.generation) as live:
                    _task_dispatch_gate(state_dir, request.task, pathlib.Path(live.worktree), stage=request.stage,
                                        run_id=planned_run[0].id)
            except LeaseError as exc:
                raise EngineError(exc.code, str(exc).split(": ", 1)[1]) from None

    reasons = None
    advisory = "unused(tier-given)"
    if request.tier:
        tier, tier_source = request.tier, "flag"
    elif packet and request.stage in packet.plan:
        planned_engine, tier = packet.plan[request.stage]
        tier_source = "packet"
        if planned_engine != request.engine:
            warnings.append(f"OAH_PACKET_PLAN_ENGINE: the packet plans {planned_engine} for {request.stage}; "
                            f"its tier {tier} is used for {request.engine}")
    else:
        explicit = any(value is not None for value in request.axes.values())
        recommendation = routing.recommend(scored.axes)
        tier, axes = recommendation.tier, scored.axes
        tier_source = "flag" if explicit else "rules"
        reasons = list(scored.reasons) + list(recommendation.reasons)
        warnings.extend(scored.warnings)
        check_budget()
        advisory = advise(axes, request.task, root=root, brief_disabled=disabled, env=env, keychain=keychain)
    skill_axes = scored.axes
    check_budget()
    selection = _brief_skills(root, kind=skill_axes.kind, stage=request.stage, risk=skill_axes.risk, scope=scope,
                              state_dir=state_dir, env=env, keychain=keychain,
                              brief_disabled=disabled, now=now)
    checked = check_catalog(request.engine, settings, state_dir=state_dir, env=env, refresh=request.refresh_models,
                            now=now)
    resolution = resolve_engine(request.engine, tier, settings, root=root, state_dir=state_dir, env=env, now=now,
                                checked=checked)
    warnings.extend(resolution.warnings)
    pinned = bool(settings.engines[request.engine]["tiers"][tier]["model"])
    check_budget()
    resolution, judge = judge_engine(resolution, settings, stage=request.stage, axes=scored.axes, pinned=pinned,
                                     entry=checked[1].entry, root=root, env=env, brief_disabled=disabled,
                                     keychain=keychain)
    fields = dict(root=root, state_dir=state_dir, packet=packet, scope=scope, stored=stored, footer=footer,
                  has_footer=has_footer, resolution=resolution, tier=tier, tier_source=tier_source, axes=axes,
                  reasons=reasons, advisory=advisory, warnings=warnings, selection=selection, checked_lease=holder,
                  judge=judge)
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
                                        "max_rounds": inputs.max_rounds}))
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
        run = runs._create_run(*planned_run)
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
                    judge: dict | None = None) -> _CallPlan:
    chosen, skills_rerank = selection if selection is not None else (None, None)
    text = _brief_text(request, run.id, packet, scope, stored, footer, has_footer, warnings, selection, checked_lease)
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
    if skills_record is not None and _CORRECTION_INPUT_CHECK.get() is not None:
        skills_record = runs._correction_record(
            {"count": len(chosen.skills), "sha256": hashlib.sha256(
                json.dumps(skills_record, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
             "advisory": skills_rerank}, ("count", "sha256", "advisory"))
    prepared = Prepared(run_dir=str(run.path), run_id=run.id, task=request.task, engine=request.engine,
                    stage=request.stage, argv=tuple(argv), cwd=str(root), timeout_s=timeout_s,
                    env_additions=additions, model=resolution.model, launch_effort=resolution.launch_effort,
                    cli_version=resolution.cli_version, warnings=tuple(warnings),
                    retain_worker_text=request.retain_worker_text)
    return _CallPlan(prepared, text, result_fields, skills_record)


def _finish_prepare(request: CallRequest, settings: Settings, run: runs.Run, *, root: pathlib.Path,
                    state_dir: pathlib.Path, packet: Packet | None, scope: Sequence[str], stored: str | None,
                    footer: str, has_footer: Callable[[str], bool], resolution: Resolution, tier: str,
                    tier_source: str, axes: routing.Axes | None, reasons: list | None, advisory: str,
                    warnings: list[str], selection: tuple | None = None, checked_lease=None,
                    plan: _CallPlan | None = None, judge: dict | None = None) -> Prepared:
    if any(warning.startswith("OAH_STAGE_OVERRIDDEN") for warning in warnings):
        from oah.lease import Store

        Store(state_dir).log(task=request.task, agent=request.engine,
                             message=f"stage override: run {run.id} runs {request.engine} for stage {request.stage} "
                                     f"(configured: {settings.stages[request.stage]})")
    if plan is None:
        plan = _plan_prepare(request, settings, run, root=root, state_dir=state_dir, packet=packet, scope=scope,
                             stored=stored, footer=footer, has_footer=has_footer, resolution=resolution, tier=tier,
                             tier_source=tier_source, axes=axes, reasons=reasons, advisory=advisory,
                             warnings=warnings, selection=selection, checked_lease=checked_lease, judge=judge)
    run.write_prompt(plan.prompt)
    if plan.skills_record is not None:
        from oah.atomic import write_json_atomic

        write_json_atomic(run.file_path("skills.json"), plan.skills_record)
    run.result(**plan.result_fields)
    if packet is not None:
        record_dispatch_binding(state_dir, packet, request.task, run.id, checked_lease=checked_lease, root=root)
    return plan.prepared


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


def execute(prepared: Prepared, *, base_env: Mapping[str, str] | None = None,
            rules: Sequence[Mapping] | None = None, redact: Callable[[str], str] | None = None) -> dict:
    """Run a prepared call in the foreground and record what happened."""
    run = prepared.run
    env = dict(os.environ if base_env is None else base_env)
    env.update(prepared.env_additions)
    clean = redact or (lambda text: text)
    started = dt.datetime.now(dt.timezone.utc)
    run.result(status="running", started_at=_iso(started), pid=os.getpid())
    publication = _CORRECTION_PUBLICATION.get()
    prompt = (publication.plan.prompt if publication is not None and publication.plan.prepared == prepared else
              run.prompt_path.read_bytes().decode("utf-8"))  # the brief goes to stdin byte for byte
    if prepared.retain_worker_text:
        run.file_path("last-message.md")
    try:
        result = _run_prepared(prepared, env, prompt)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if prepared.retain_worker_text:
            run.write_logs("", "")
        return run.result(status="failed", finished_at=_iso(dt.datetime.now(dt.timezone.utc)),
                          error=(clean(f"OAH_ENGINE_LAUNCH_FAILED: {exc}") if prepared.retain_worker_text else
                                 _recorded_error(EngineError(getattr(exc, "code", "OAH_ENGINE_LAUNCH_FAILED"), ""), False)))
    except (KeyboardInterrupt, SystemExit):
        # _run_prepared already stopped the process tree; never leave the run looking alive.
        if prepared.retain_worker_text:
            run.write_logs("", "")
        run.result(status="failed", finished_at=_iso(dt.datetime.now(dt.timezone.utc)),
                   error="OAH_RUN_INTERRUPTED: the run was interrupted (Ctrl-C or a stop signal) and its process "
                         "tree was stopped; call the engine again when you are ready")
        raise
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
    if result.timed_out:
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
    return run.result(status=status, finished_at=_iso(finished), duration_s=round((finished - started).total_seconds(), 3),
                      exit_code=result.returncode, timed_out=result.timed_out, error=error, **fields)


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


def _kill_owned_tree(child):
    from oah.engines.proc import kill_tree

    tree = getattr(child, "_oah_posix_tree", None)
    if tree is not None:
        tree.kill()
    elif os.name == "nt" and getattr(child, "_oah_job", None) is not None:
        kill_tree(child)
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


def _stream_correction(child, prepared: Prepared, prompt: str):
    """Drain and hash exact worker bytes without displaying or retaining worker text."""
    from oah.engines.proc import REAP_SECONDS

    streams = {name: {"bytes": 0, "sha256": hashlib.sha256()} for name in ("stdout", "stderr")}
    header = bytearray()
    failed = []

    def drain(name, pipe):
        try:
            while data := pipe.read1(65536):
                streams[name]["bytes"] += len(data)
                streams[name]["sha256"].update(data)
                if name == "stderr":
                    header.extend(data)
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


def _run_prepared(prepared: Prepared, env: Mapping[str, str], prompt: str):
    """Use proc's job/tree handling, holding the lease lock only through process creation and resume."""
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
    try:
        with _paid_dispatch(prepared), dispatch_guard(prepared):
            with _deferred_creation_signals():
                try:
                    child = subprocess.Popen(list(prepared.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, cwd=prepared.cwd, env=dict(env), **options)
                except OSError as exc:
                    # Windows omits the executable; keep spawn errors out of the lease's state-I/O wrapper.
                    raise subprocess.SubprocessError(f"cannot launch {prepared.argv[0]}: {exc}. "
                                                     "Check the executable path and permissions, then retry") from None
                if job is not None:
                    try:
                        job.start(child)
                    except BaseException:
                        child.kill()
                        child.communicate(timeout=REAP_SECONDS)
                        raise
                else:
                    child._oah_posix_tree = _PosixTree(child)
        if not prepared.retain_worker_text:
            return _stream_correction(child, prepared, prompt)
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
                out, err = output[0] if output else (b"", b"")
                return RunResult(None, decode_output(out), decode_output(err), True)
            exchanged.wait(timeout=min(POLL_SECONDS, remaining))
        communicator.join(timeout=REAP_SECONDS)
        if errors:
            raise errors[0]
        out, err = output[0]
        if _CORRECTION_CLEANUP.get():
            # A completed correction must leave no owned descendants changing the tree during accounting.
            _kill_owned_tree(child)
        return RunResult(child.returncode, decode_output(out), decode_output(err), False)
    except BaseException:
        # Ctrl-C or a stop signal: the worker has its own session or job, so it would keep running.
        # An exited launcher can leave descendants holding the pipes and modifying the worktree.
        if child is not None:
            _kill_owned_tree(child)
            try:
                if communicator is not None and communicator.ident is not None:
                    communicator.join(timeout=REAP_SECONDS)
                else:
                    child.communicate(timeout=REAP_SECONDS)
            except Exception:
                pass
        raise
    finally:
        if child is not None and hasattr(child, "_oah_posix_tree"):
            child._oah_posix_tree.close()
        if job is not None:
            job.close()


_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); import oah.call as c; "
         "raise SystemExit(getattr(c, sys.argv[2])(sys.argv[3]))")
LAUNCH_TIMEOUT = 30.0


def _boot_argv(entry: str, run_dir: str) -> list[str]:
    package_root = str(pathlib.Path(__file__).resolve().parent.parent)
    return [sys.executable, "-c", _BOOT, package_root, entry, run_dir]


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
        if detail.startswith(("OAH_LEASE_CHANGED: ", "OAH_INTENT_NOT_CURRENT: ", "OAH_RUN_LAUNCH_INVALID: ",
                              "OAH_REVIEW_NEEDS_DECISION: ", "OAH_REVIEW_ROUNDS_EXHAUSTED: ",
                              "OAH_REVIEW_STATE_INVALID: ", "OAH_REVIEW_SERIES_BUSY: ")):
            code, message = detail.split(": ", 1)
            raise EngineError(code, message)
        if detail.startswith("OAH_RUN_DETACH_FAILED:"):
            run.result(status="failed", error=detail)
            raise EngineError("OAH_RUN_DETACH_FAILED", detail.split(": ", 1)[1])
        run.result(status="failed", error=f"OAH_RUN_LAUNCH_FAILED: {detail or 'no runner pid'}")
        raise EngineError("OAH_RUN_LAUNCH_FAILED", f"the background runner did not start ({detail or 'no pid'}); "
                          "run the same command without --background")
    if detail:
        print(detail, file=sys.stderr)
    run.result(status="queued", pid=int(text))
    return prepared.run_id


def _windows() -> bool:
    return os.name == "nt"


def _background_run(run_dir: str) -> runs.Run:
    path = pathlib.Path(run_dir)
    return runs.Run(id=path.name, task=path.parent.name, path=path)


def launch_main(run_dir: str) -> int:
    try:
        return _launch_main(run_dir)
    except EngineError as exc:
        print(exc, file=sys.stderr)
        return 1


def _launch_main(run_dir: str) -> int:
    """Entry point of the launcher: start the runner detached, print its pid and exit."""
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                     "close_fds": True}
    from oah.atomic import read_json

    run = _background_run(run_dir)
    saved = read_json(run.launch_path, {})
    require_detach = saved.get("require_detach", False)
    prepared = Prepared.from_json(saved)
    argv = _boot_argv("background_main", run_dir)
    if _windows():
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            with _paid_dispatch(prepared, record=False), dispatch_guard(prepared):
                child = subprocess.Popen(argv, creationflags=flags | 0x01000000, **options)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError as exc:
            if require_detach:
                print(f"OAH_RUN_DETACH_FAILED: Windows refused job breakaway ({exc}); retry without "
                      "--require-detach, or use a terminal whose job permits breakaway", file=sys.stderr)
                return 69
            warning = (f"OAH_RUN_DETACH_UNVERIFIED: Windows refused job breakaway ({exc}); closing this terminal "
                       "may stop the run. Keep it open and use oah wait, or retry with --require-detach "
                       "in a terminal whose job permits breakaway")
            run.result(detached=runs.UNVERIFIED, warnings=[*run.read().get("warnings", []), warning])
            print(warning, file=sys.stderr, flush=True)
            with _paid_dispatch(prepared, record=False), dispatch_guard(prepared):
                child = subprocess.Popen(argv, creationflags=flags, **options)
        else:
            run.result(detached="verified")
    else:
        with _paid_dispatch(prepared, record=False), dispatch_guard(prepared):
            child = subprocess.Popen(argv, start_new_session=True, **options)
        run.result(detached="verified")
    print(child.pid, flush=True)
    child.returncode = 0  # the runner outlives this launcher on purpose; nothing waits for it here
    return 0


def background_main(run_dir: str) -> int:
    """Entry point of the detached runner."""
    from oah.atomic import read_json

    prepared = Prepared.from_json(read_json(_background_run(run_dir).launch_path, None))
    try:
        execute(prepared)
    except EngineError as exc:
        print(exc, file=sys.stderr)
        return 1
    except BaseException as exc:
        prepared.run.result(status="failed", error=f"OAH_RUN_RUNNER_FAILED: {type(exc).__name__}: {exc}")
        raise
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


def wait(state_dir: pathlib.Path, run_id: str, *, timeout: float = WAIT_LIMIT_S, poll: float = 0.2,
         deadline: float | None = None) -> dict:
    """The final or current record after `timeout` (capped at 9 minutes).

    An optional entry deadline also bounds storage retries and lost-result publication, even when
    timeout is zero. Without it, storage keeps its existing retry budgets.
    """
    run = runs.find_run(state_dir, run_id, deadline=deadline)
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
