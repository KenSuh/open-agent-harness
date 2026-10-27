# 모델 카탈로그 — CLI 가 보고하는 모델·effort 목록을 상태 폴더에 두고 버전 변경·TTL 로 새로 읽으며, 단을 품질 순서로 모델에 잇고 바뀐 값을 기록한다.
"""Models follow updates (A14).

Catalog
    `Catalog(state_dir)` keeps `<state>/models/catalog.json`: per engine the CLI version, when it was
    checked, and the models the CLI reports (Codex: `models_cache.json` with each model's
    `supported_reasoning_levels`; Claude: the aliases and effort levels in `claude --help`).
    `ensure_fresh` runs before every launch: it probes `<cli> --version` and rebuilds the entry when
    the version changed, the TTL passed or a refresh was asked for. A rebuild that reads no models keeps
    the last good entry and warns with `OAH_MODELS_REFRESH_FAILED`; nothing is ever downgraded.

Mapping
    `map_tier` applies the precedence: an explicit tier model (a pin) always wins; then the automatic
    mapping (the engine's strongest-first `quality_order` filtered to reported models: low takes the
    last, medium the second or the only one, high and max the first; for Codex without a user list,
    a model the CLI marks as default); then the effective configuration (Codex's config files). Nothing
    resolvable raises `OAH_MODEL_UNRESOLVED`.

Candidates (amendment 6)
    `build_candidates` makes the closed set a judge may pick from for one call: listed (never hidden)
    models, intersected with `allowed_models` (empty = all), each with its supported ordinary efforts at
    most the model's `effort_caps` entry. With a ranking (`quality_order` names listed models, including
    the floor tier's model), no model weaker than the floor tier's deterministic model; always, no
    effort below the floor tier's effort. Without a ranking (Revision 3), `tier_pools` declares which
    models the user qualifies for each tier: a model is a candidate only when it is in the pool of the
    call's floor tier and of the call's tier. Models in a pool are peers, in the user's order, with no
    rank inferred; `allowed_models` only filters and never qualifies a model. Pool models left out (below
    the floor effort, above a cap, hidden) and allowed models in no applicable pool are in `excluded`
    with the rule. Without a ranking and without both pools, the model stays the deterministic one,
    only the effort is judged, and `note` says why. `CandidateSet.reject_reason` is the one rule: the
    candidate list is every listed pair it accepts, and a judged answer is used only when it accepts
    that answer too.
    `check_caps` warns about capped, allowed or pooled models the catalog does not list, and refuses
    when a fresh catalog lists none of the capped models.

Changes
    `Catalog.record_resolution` stores the last resolved model and effort per engine and tier in
    `resolved.json` and appends `{ts, engine, tier, field, old, new, why}` to `changes.jsonl` for each
    value that changed.
"""
from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from oah.atomic import append_jsonl, read_json, write_json_atomic
from oah.engines import claude, codex
from oah.engines.effort import BEHAVIOUR_CHANGING, EngineError, rank
from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command
from oah.lockfile import StateLock

DEFAULTS = {"ttl_hours": 24.0}
PROBE_TIMEOUT = 30.0
UNRESOLVED = "OAH_MODEL_UNRESOLVED"


@dataclass(frozen=True)
class ModelInfo:
    name: str
    efforts: tuple[str, ...]
    default: bool = False
    listed: bool = True  # False: the CLI hides it from its model list; never a judged candidate

    def to_json(self) -> dict:
        return {"name": self.name, "efforts": list(self.efforts), "default": self.default, "listed": self.listed}


@dataclass(frozen=True)
class Entry:
    engine: str
    cli_version: str | None
    checked_at: str | None
    models: tuple[ModelInfo, ...]
    source: str

    def to_json(self) -> dict:
        return {"cli_version": self.cli_version, "checked_at": self.checked_at, "source": self.source,
                "models": [model.to_json() for model in self.models]}

    @classmethod
    def from_json(cls, engine: str, data: object) -> "Entry | None":
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return None
        models = []
        for item in data["models"]:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                efforts = tuple(e for e in item.get("efforts", ()) if isinstance(e, str))
                models.append(ModelInfo(item["name"], efforts, item.get("default") is True,
                                        item.get("listed") is not False))
        version, checked = data.get("cli_version"), data.get("checked_at")
        return cls(engine, version if isinstance(version, str) else None,
                   checked if isinstance(checked, str) else None, tuple(models), str(data.get("source", "")))

    def model(self, name: str) -> ModelInfo | None:
        return next((model for model in self.models if model.name == name), None)


@dataclass(frozen=True)
class Freshness:
    entry: Entry
    refreshed: str | None  # why the entry was rebuilt now, or None
    warnings: tuple[str, ...]
    launch_version: str | None = None  # the current probe, independent of a retained catalog entry


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_base(engine: str, command: Sequence[str], search_path: str | None = None) -> list[str]:
    """The argv prefix that runs an engine: a bare name is found on PATH; a path is used literally."""
    if not command or not all(isinstance(part, str) and part for part in command):
        raise EngineError("OAH_CONFIG_INVALID", f"[engines.{engine}] command must be a non-empty list of strings, "
                          f'such as ["{engine}"]')
    first = command[0]
    if "/" in first or "\\" in first:
        return list(command)
    try:
        found = resolve_command(first, search_path)
    except UnsupportedBatchShim as exc:
        raise EngineError(exc.code, str(exc).split(": ", 1)[1]) from exc
    if found is None:
        raise EngineError("OAH_ENGINE_MISSING", f"{first} was not found on PATH; install it (`oah doctor` prints "
                          f"the install command) or set [engines.{engine}] command in .oah/config.toml")
    return [*found.argv, *command[1:]]


def probe_version(engine: str, base: Sequence[str], env: Mapping[str, str]) -> str | None:
    result = run_command([*base, "--version"], timeout=PROBE_TIMEOUT, env=env)
    if result.returncode != 0:
        return None
    return (codex.parse_version if engine == "codex" else claude.parse_version)(result.stdout)


def probe_models(engine: str, base: Sequence[str], env: Mapping[str, str]) -> tuple[tuple[ModelInfo, ...], str]:
    """The models the CLI reports now, and where they were read; an empty tuple means none were readable."""
    if engine == "codex":
        home = codex.codex_home(env)
        cache = codex.read_models_cache(home) or {}
        return (tuple(ModelInfo(m.slug, m.levels, m.default, m.listed) for m in cache.values()),
                f"{codex.CACHE_NAME} in CODEX_HOME")
    result = run_command([*base, "--help"], timeout=PROBE_TIMEOUT, env=env)
    info = claude.parse_help(result.stdout if result.returncode == 0 else "")
    return tuple(ModelInfo(alias, info.efforts) for alias in info.aliases), "claude --help"


class Catalog:
    def __init__(self, state_dir: pathlib.Path) -> None:
        self.state_dir = pathlib.Path(state_dir)
        self.dir = self.state_dir / "models"
        self.catalog_path = self.dir / "catalog.json"
        self.resolved_path = self.dir / "resolved.json"
        self.changes_path = self.dir / "changes.jsonl"
        self.lock_path = self.state_dir / ".lock"

    def load(self, engine: str) -> Entry | None:
        data = read_json(self.catalog_path, {})
        return Entry.from_json(engine, data.get(engine)) if isinstance(data, dict) else None

    def _save(self, entry: Entry) -> None:
        with StateLock(self.lock_path):
            data = read_json(self.catalog_path, {})
            data = data if isinstance(data, dict) else {}
            data[entry.engine] = entry.to_json()
            write_json_atomic(self.catalog_path, data)

    def ensure_fresh(self, engine: str, *, base: Sequence[str], env: Mapping[str, str], ttl_hours: float,
                     refresh: bool = False, now: dt.datetime | None = None) -> Freshness:
        """The catalog entry to use for this launch, rebuilt first when it is due."""
        now = now or utc_now()
        stored = self.load(engine)
        version = probe_version(engine, base, env)
        warnings: list[str] = []
        if version is None:
            warnings.append(f"OAH_MODELS_VERSION_UNKNOWN: `{engine} --version` gave no version, so the catalog "
                            "cannot tell whether the CLI changed; run `oah doctor` to check the installation")
        reason = None
        if refresh:
            reason = "refresh requested"
        elif stored is None:
            reason = "no catalog yet"
        elif stored.cli_version != version:
            reason = f"CLI version changed {stored.cli_version} -> {version}"
        elif stored.checked_at is None or now - dt.datetime.fromisoformat(
                stored.checked_at.replace("Z", "+00:00")) >= dt.timedelta(hours=ttl_hours):
            reason = f"catalog older than {ttl_hours:g} h"
        if reason is None:
            return Freshness(stored, None, tuple(warnings), version)
        models, source = probe_models(engine, base, env)
        if not models:
            kept = f"keeping the catalog checked at {stored.checked_at}" if stored else "no earlier catalog exists"
            warnings.append(f"OAH_MODELS_REFRESH_FAILED: {engine} reported no models ({source}); {kept}. Run the "
                            f"{engine} CLI once so it refreshes its model list, then `oah models --refresh`")
            if stored is not None:
                return Freshness(stored, None, tuple(warnings), version)
            return Freshness(Entry(engine, version, None, (), source), None, tuple(warnings), version)
        entry = Entry(engine, version, _iso(now), models, source)
        self._save(entry)
        return Freshness(entry, reason, tuple(warnings), version)

    def resolved(self) -> dict:
        data = read_json(self.resolved_path, {})
        return data if isinstance(data, dict) else {}

    def record_resolution(self, engine: str, tier: str, *, model: str, effort: str, source: str, why: str,
                          now: dt.datetime | None = None) -> list[dict]:
        """Store the resolved model and effort; log each changed value. Returns the logged changes."""
        stamp = _iso(now or utc_now())
        changes: list[dict] = []
        with StateLock(self.lock_path):
            data = self.resolved()
            old = data.get(engine, {}).get(tier, {}) if isinstance(data.get(engine), dict) else {}
            for field, new in (("model", model), ("effort", effort)):
                if old.get(field) != new:
                    change = {"ts": stamp, "engine": engine, "tier": tier, "field": field, "old": old.get(field),
                              "new": new, "why": why}
                    append_jsonl(self.changes_path, change)
                    changes.append(change)
            if changes or old.get("source") != source:
                data.setdefault(engine, {})[tier] = {"model": model, "effort": effort, "source": source,
                                                     "at": stamp}
                write_json_atomic(self.resolved_path, data)
        return changes

    def changes(self) -> list[dict]:
        from oah.atomic import read_jsonl

        return read_jsonl(self.changes_path)


def _by_tier(names: Sequence[str], tier: str) -> str:
    if tier == "low":
        return names[-1]
    if tier == "medium":
        return names[1] if len(names) > 1 else names[0]
    return names[0]


def map_tier(engine: str, tier: str, *, pin: str, quality_order: Sequence[str], entry: Entry | None,
             effective: str | None) -> tuple[str, str]:
    """(model, source) for one tier; source is pin, quality_order, cli_default or effective_config."""
    if pin:
        return pin, "pin"
    available = [model.name for model in entry.models] if entry else []
    if quality_order:
        listed = [name for name in quality_order if name in available]
        if listed:
            return _by_tier(listed, tier), "quality_order"
        if engine == "claude" and not available:
            # Claude Code resolves these aliases itself; the CLI's list could not be read.
            return _by_tier(list(quality_order), tier), "quality_order(unconfirmed)"
    elif engine == "codex":
        defaults = [model.name for model in entry.models if model.default] if entry else []
        if defaults:
            return defaults[0], "cli_default"
    if effective:
        return effective, "effective_config"
    reported = f" (it reports: {', '.join(available)})" if available else ""
    raise EngineError(UNRESOLVED, f"no {engine} model could be resolved for tier {tier}: no tier model is set, "
                      f"the CLI reports no model from quality_order or marked as default{reported}, and no default "
                      f"is configured. Set [engines.{engine}.tiers] {tier} = {{ model = \"<model>\" }} in "
                      f".oah/config.toml, or list a reported model in [engines.{engine}] quality_order")


@dataclass(frozen=True)
class Pair:
    """One (model, effort) choice."""

    model: str
    effort: str

    def to_json(self) -> dict:
        return {"model": self.model, "effort": self.effort}


@dataclass(frozen=True)
class CandidateSet:
    """The closed (model, effort) pairs a judge may pick from for one call, and the rule that made them."""

    engine: str
    deterministic: Pair
    floor_tier: str
    floor: Pair
    # "ranked": quality_order ranks the models; "pools": the user's tier pools, unranked peers;
    # "deterministic": the effort only.
    mode: str
    order: tuple[str, ...]  # the ranked listed models, strongest first (empty without a ranking)
    allowed: tuple[str, ...]
    caps: tuple[tuple[str, str], ...]
    entry: Entry | None
    pairs: tuple[Pair, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()  # pool or allowed models with no candidate pair, and the rule why
    note: str | None = None  # why the model is not judged ("model choice off: ...")
    tier: str = ""
    pools: tuple[tuple[str, tuple[str, ...]], ...] = ()  # the configured tier pools, as given
    qualified: tuple[str, ...] = ()  # in both the floor tier's and the call tier's pool (pools mode)

    @property
    def ranked(self) -> bool:
        return self.mode == "ranked"

    def reject_reason(self, model: str, effort: str) -> str | None:
        """None when (model, effort) is a candidate, else the rule it breaks."""
        info = self.entry.model(model) if self.entry is not None and isinstance(model, str) else None
        if info is None or not info.listed:
            return "outside-candidates"
        if self.allowed and model not in self.allowed:
            return "not-allowed"
        if effort not in info.efforts or effort in BEHAVIOUR_CHANGING or rank(effort) is None:
            return "unsupported-effort"
        if self.ranked:
            if model not in self.order:
                return "unranked-model"
            if self.order.index(model) > self.order.index(self.floor.model):
                return "below-floor"
        elif self.mode == "pools":
            if model not in self.qualified:
                return "not-in-tier-pool"
        elif model != self.deterministic.model:
            return "outside-candidates"
        floor_rank = rank(self.floor.effort)
        if floor_rank is None:
            if effort != self.deterministic.effort:
                return "outside-candidates"
        elif rank(effort) < floor_rank:
            return "below-floor"
        cap = dict(self.caps).get(model)
        if cap is not None and rank(cap) is not None and rank(effort) > rank(cap):
            return "above-cap"
        return None

    def to_json(self) -> dict:
        return {"deterministic": self.deterministic.to_json(),
                "floor": {"tier": self.floor_tier, **self.floor.to_json()}, "ranked": self.ranked,
                "mode": self.mode, "note": self.note, "allowed_models": list(self.allowed), "caps": dict(self.caps),
                "tier_pools": {tier: list(names) for tier, names in self.pools},
                "candidates": [[pair.model, pair.effort] for pair in self.pairs], "excluded": dict(self.excluded)}


def build_candidates(engine: str, *, entry: Entry | None, deterministic: Pair, tier: str, floor_tier: str,
                     floor: Pair, quality_order: Sequence[str], allowed_models: Sequence[str],
                     effort_caps: Mapping[str, str], tier_pools: Mapping[str, Sequence[str]]) -> CandidateSet:
    """The closed candidate set for one call (see Candidates above)."""
    listed = [model.name for model in entry.models if model.listed] if entry else []
    order = tuple(name for name in quality_order if name in listed)
    ranked = bool(order) and floor.model in order
    allowed = tuple(dict.fromkeys(allowed_models))
    pools = tuple((name, tuple(dict.fromkeys(tier_pools[name]))) for name in tier_pools)
    pool = dict(pools)
    applicable = tuple(dict.fromkeys((floor_tier, tier)))
    pooled = all(pool.get(name) for name in applicable)
    mode = "ranked" if ranked else "pools" if pooled else "deterministic"
    note = None
    if mode == "deterministic":
        missing = " and ".join(name for name in applicable if not pool.get(name))
        why = f"no tier pool for {missing}" if pools else "no tier pools"
        note = (f"model choice off: quality_order does not rank {floor.model} and {why}" if order
                else f"model choice off: no ranking and {why}")
    # Peers keep the call tier's pool order; a model must be in every applicable pool.
    qualified = tuple(name for name in pool.get(tier, ()) if all(name in pool[t] for t in applicable)) \
        if mode == "pools" else ()
    rule = CandidateSet(engine, deterministic, floor_tier, floor, mode, order if ranked else (), allowed,
                        tuple(sorted(effort_caps.items())), entry, note=note, tier=tier, pools=pools,
                        qualified=qualified)
    names = list(order) if ranked else list(qualified) if mode == "pools" else [deterministic.model]
    found: list[Pair] = []
    excluded: list[tuple[str, str]] = []
    others = [name for name in dict.fromkeys([*(n for t in applicable for n in pool.get(t, ())), *allowed])
              if name not in names] if mode == "pools" else []
    for name in [*names, *others]:
        info = entry.model(name) if entry is not None else None
        efforts = info.efforts if info is not None else ()
        reasons = [rule.reject_reason(name, effort) for effort in efforts]
        if name in names:
            found += [Pair(name, effort) for effort, reason in zip(efforts, reasons) if reason is None]
        if mode == "pools" and None not in reasons:
            # The first rule that applies: unlisted, not allowed, not qualified, then a cap only when some
            # effort met the floor, then the floor.
            why = next((r for r in ("outside-candidates", "not-allowed", "not-in-tier-pool", "above-cap",
                                    "below-floor", "unsupported-effort") if r in reasons), "outside-candidates")
            excluded.append((name, why if info is not None and info.listed else "outside-candidates"))
    found.sort(key=lambda pair: (names.index(pair.model), rank(pair.effort)))
    return replace(rule, pairs=tuple(found), excluded=tuple(excluded))


def check_caps(engine: str, caps: Mapping[str, str], allowed: Sequence[str], entry: Entry | None, *,
               fresh: bool, pools: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    """Warnings for capped, allowed or pooled models the catalog does not list; refuse when a fresh catalog lists
    none of the capped models."""
    listed = {model.name for model in entry.models if model.listed} if entry else set()
    warnings: list[str] = []
    unknown = sorted(name for name in caps if name not in listed)
    if unknown and fresh and len(unknown) == len(caps):
        raise EngineError("OAH_CONFIG_INVALID", f"[engines.{engine}.effort_caps] names {', '.join(unknown)}, but the "
                          f"{engine} CLI lists none of them; run `oah models` to see the listed models, then fix or "
                          "remove those entries in .oah/config.toml")
    if unknown:
        warnings.append(f"OAH_MODEL_CAP_UNKNOWN: [engines.{engine}.effort_caps] names {', '.join(unknown)}, which "
                        f"the {engine} CLI does not list now; those caps apply only if it lists them again. Run "
                        "`oah models --refresh` to check, or remove them from .oah/config.toml")
    missing = [name for name in allowed if name not in listed]
    if missing:
        warnings.append(f"OAH_MODEL_ALLOWED_UNKNOWN: [engines.{engine}] allowed_models names {', '.join(missing)}, "
                        f"which the {engine} CLI does not list now, so the judge cannot pick them. Run "
                        "`oah models --refresh` to check, or remove them from .oah/config.toml")
    for tier, names in (pools or {}).items():
        unlisted = [name for name in names if name not in listed]
        if unlisted:
            warnings.append(f"OAH_MODEL_POOL_UNKNOWN: [engines.{engine}.tier_pools] {tier} names "
                            f"{', '.join(unlisted)}, which the {engine} CLI does not list now, so the judge cannot "
                            f"pick them for tier_pools.{tier}. Run `oah models --refresh` to check, or remove them "
                            "from .oah/config.toml")
    return warnings
