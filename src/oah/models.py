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

Changes
    `Catalog.record_resolution` stores the last resolved model and effort per engine and tier in
    `resolved.json` and appends `{ts, engine, tier, field, old, new, why}` to `changes.jsonl` for each
    value that changed.
"""
from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import dataclass
from typing import Mapping, Sequence

from oah.atomic import append_jsonl, read_json, write_json_atomic
from oah.engines import claude, codex
from oah.engines.effort import EngineError
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

    def to_json(self) -> dict:
        return {"name": self.name, "efforts": list(self.efforts), "default": self.default}


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
                models.append(ModelInfo(item["name"], efforts, item.get("default") is True))
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
        return (tuple(ModelInfo(m.slug, m.levels, m.default) for m in cache.values()),
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
