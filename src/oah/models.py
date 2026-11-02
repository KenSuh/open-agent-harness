# 모델 카탈로그 — CLI 가 보고하는 모델·effort 목록을 상태 폴더에 두고 버전 변경·TTL 로 새로 읽으며, 고정·풀·품질 목록·점수 규칙 순으로 단을 모델에 잇고 바뀐 값을 기록한다.
"""Models follow updates (A14).

Catalog
    `Catalog(state_dir)` keeps `<state>/models/catalog.json`: per engine the CLI version, when it was
    checked, and the models the CLI reports (Codex: `models_cache.json` with each model's
    `supported_reasoning_levels`; Claude: the aliases and effort levels in `claude --help`, plus each known alias
    the help omits that the free alias check confirms, `oah.engines.claude_alias`).
    `ensure_fresh` runs before every launch: it probes `<cli> --version` and rebuilds the entry when
    the version changed, the TTL passed or a refresh was asked for. Entries missing explicit visibility
    metadata are invalidated first. A rebuild that reads no models keeps the last valid entry and warns
    with `OAH_MODELS_REFRESH_FAILED`; nothing is ever downgraded.

Mapping
    `map_tier` applies the precedence. A tier model (a pin) always wins. With no usable entry nothing else
    can run: `OAH_MODEL_CATALOG_UNAVAILABLE` names the one-line pin. Then `tier_pools`: the first listed pool
    model of the tier, and `OAH_MODEL_UNRESOLVED` when a configured tier has none. Then `quality_order`
    (strongest first, over listed models: low takes the last, medium the second or the only one, high and
    max the first), which needs no family and no score, so it is the way out when a family is renamed. If it names
    no listed model, the score rule in `oah.scoring` decides, and `OAH_MODEL_NO_SCORES` says so when it has no
    scores, or `OAH_MODEL_UNRESOLVED` when no allowed model reaches the floor.
    `allowed_models` filters pool and `quality_order` picks. The CLI's default flag and Codex's own
    configuration never choose a model. The old Claude template default
    `["opus", "sonnet", "haiku"]` counts as unset (`user_order`), so every existing config gets scores with
    no file edited.

Candidates (amendment 6)
    `build_candidates` makes the closed set a judge may pick from for one call: listed (never hidden)
    models, intersected with `allowed_models` (empty = all), each with its supported ordinary efforts at
    most the model's `effort_caps` entry. With a ranking (`quality_order` names listed models, including
    the floor tier's model), no model weaker than the floor tier's deterministic model; always, no
    effort below the floor tier's effort. With neither a user ranking nor pools, a scored call offers
    `scoring.band`: mode `scored`, the rule's choice and the models up to the next step's required score.
    Without a ranking (Revision 3), `tier_pools` declares which
    models the user qualifies for each tier: a model is a candidate only when it is in the pool of the
    call's floor tier and of the call's tier. Models in a pool are peers, in the user's order, with no
    rank inferred; `allowed_models` only filters and never qualifies a model. Pool models left out (below
    the floor effort, above a cap, hidden) and allowed models in no applicable pool are in `excluded`
    with the rule. Configured pools always require both memberships; a missing or empty applicable pool
    leaves no judged candidates. When a partial ranking or partial pools leave no other mode, only the
    deterministic model's effort is judged. `CandidateSet.reject_reason` is the one rule: the
    candidate list is every listed pair it accepts, and a judged answer is used only when it accepts
    that answer too.
    `check_caps` warns about capped, allowed or pooled models the catalog does not list, and refuses
    when a fresh catalog lists none of the capped models.

Unpooled notices
    `unpooled_models` names the listed models in no `tier_pools` entry. `check_catalog` in `oah.call` is
    the one place that decides when a launch hears of them: a rebuild adds
    `OAH_MODEL_NEW_UNPOOLED` warnings, and any other launch stays quiet. `oah models` reports them every
    time, and `oah doctor` computes them from the stored catalog and current pools. The rule compares
    the catalog with the pools, not with an earlier catalog, so the first rebuild with pools configured
    reports every unpooled model, not only new ones.
    A notice is advice: oah never edits `tier_pools`, and the judge never picks such a model.

Changes
    `Catalog.record_resolution` stores the last resolved model and effort per engine and tier in
    `resolved.json` and appends `{ts, engine, tier, field, old, new, why}` to `changes.jsonl` for each
    value that changed. A catalog rebuild compares the score rule's choice at each step under the old and
    new entry (`model_changes`): the rebuild happens once, so its warning and its `changes.jsonl` line
    need no new state and cannot repeat. A user whose pins, pools or `quality_order` leave no tier to the rule
    hears nothing about it. `score_notices` names what a rebuild left out and why.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
from dataclasses import dataclass, replace
from typing import Callable, Literal, Mapping, Sequence

from oah import scoring
from oah.atomic import (RETRY_SECONDS, StateCorrupt, StorageError, _retry_permission, append_jsonl, read_json,
                        read_jsonl, write_json_atomic)
from oah.engines import claude, claude_alias, codex
from oah.engines.effort import BEHAVIOUR_CHANGING, EngineError, highest, rank, resolve
from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command
from oah.lockfile import StateLock

DEFAULTS = {"ttl_hours": 24.0, "profile": scoring.DEFAULT_PROFILE, "fetch_scores": True}
PROBE_TIMEOUT = 30.0
UNRESOLVED = scoring.UNRESOLVED
CATALOG_UNAVAILABLE = "OAH_MODEL_CATALOG_UNAVAILABLE"
NEW_UNPOOLED = "OAH_MODEL_NEW_UNPOOLED"
OLD_CLAUDE_ORDER = ("opus", "sonnet", "haiku")  # the template default before scores: it counts as unset
CHANGED = "OAH_MODEL_CHANGED"
UNKNOWN_FAMILY = "OAH_MODEL_UNKNOWN_FAMILY"
UNSCORED = "OAH_MODEL_UNSCORED"
ORDER_IGNORED = "OAH_MODEL_ORDER_IGNORED"
PLAIN_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# A damaged catalog must not stop advisory notice lookup.
_NOTICE_FAILURES = (StateCorrupt, OSError, TypeError, ValueError, RecursionError)


@dataclass(frozen=True)
class ModelInfo:
    name: str
    efforts: tuple[str, ...]
    default: bool = False
    listed: bool = True  # False: the CLI hides it from its model list; never a judged candidate

    def to_json(self) -> dict:
        return {"name": self.name, "efforts": list(self.efforts), "default": self.default, "listed": self.listed}


@dataclass(frozen=True)
class Runnable:
    launch: str | None
    reason: Literal["unsupported", "cap-below-floor"] | None = None
    comparable: bool = False


def runnable(info: ModelInfo, tier_effort: str, floor_effort: str, cap: str | None) -> Runnable:
    """Resolve both efforts against this model, then clamp and check its own floor.

    Literal efforts without catalog metadata still pass through, as for existing pins. Unknown labels
    cannot be ordered; without a cap they retain the existing unverified-effort behavior.
    """
    try:
        launch = resolve(tier_effort, info.efforts, model=info.name).launch
        floor = resolve(floor_effort, info.efforts, model=info.name).launch
    except EngineError:
        return Runnable(None, "unsupported")
    launch_rank, floor_rank = rank(launch), rank(floor)
    if launch_rank is not None and floor_rank is not None and launch_rank < floor_rank:
        return Runnable(None, "unsupported")
    if cap is not None:
        cap_rank = rank(cap)
        if launch_rank is None or floor_rank is None or cap_rank is None:
            return Runnable(None, "unsupported")
        if launch_rank > cap_rank:
            launch = highest([level for level in info.efforts or (cap,)
                              if rank(level) is not None and rank(level) <= cap_rank])
            if launch is None:
                return Runnable(None, "cap-below-floor")
            launch_rank = rank(launch)
    if launch_rank is not None and floor_rank is not None and launch_rank < floor_rank:
        return Runnable(None, "cap-below-floor")
    return Runnable(launch, comparable=launch_rank is not None and floor_rank is not None)


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
                if not isinstance(item.get("listed"), bool):
                    return None  # Legacy catalogs cannot distinguish hidden models; refresh before reuse.
                efforts = tuple(e for e in item.get("efforts", ()) if isinstance(e, str))
                models.append(ModelInfo(item["name"], efforts, item.get("default") is True,
                                        item["listed"]))
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
    previous: Entry | None = None  # the entry a rebuild replaced
    scores: scoring.ScoreSet | None = None  # read once for this entry; `oah.call.check_catalog` fills it in


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_date(text: object) -> str:
    """The UTC date of a checked time, or empty when it is not a timestamp; the text itself is never shown."""
    if not isinstance(text, str):
        return ""
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return moment.astimezone(dt.timezone.utc).date().isoformat() if moment.utcoffset() is not None else ""
    except (ValueError, OverflowError):
        return ""


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


def probe_models(engine: str, base: Sequence[str], env: Mapping[str, str],
                 aliases: Sequence[str] = ()) -> tuple[tuple[ModelInfo, ...], str]:
    """The models the CLI reports now, and where they were read; an empty tuple means none were readable.

    For Claude, a known alias in `aliases` that the help omits is listed when the free alias check confirms it. A
    help text that names no alias stops here, so the last good entry stays in use."""
    if engine == "codex":
        home = codex.codex_home(env)
        cache = codex.read_models_cache(home) or {}
        return (tuple(ModelInfo(m.slug, m.levels, m.default, m.listed) for m in cache.values()),
                f"{codex.CACHE_NAME} in CODEX_HOME")
    result = run_command([*base, "--help"], timeout=PROBE_TIMEOUT, env=env)
    info = claude.parse_help(result.stdout if result.returncode == 0 else "")
    if not info.aliases:
        return (), "claude --help"
    confirmed = claude_alias.confirm(base, [alias for alias in aliases if alias not in info.aliases], env)
    return (tuple(ModelInfo(alias, info.efforts) for alias in (*info.aliases, *confirmed)),
            "claude --help + alias check" if confirmed else "claude --help")


class Catalog:
    def __init__(self, state_dir: pathlib.Path) -> None:
        self.state_dir = pathlib.Path(state_dir)
        self.dir = self.state_dir / "models"
        self.catalog_path = self.dir / "catalog.json"
        self.resolved_path = self.dir / "resolved.json"
        self.changes_path = self.dir / "changes.jsonl"
        self.lock_path = self.state_dir / ".lock"

    def score_bytes(self, name: str) -> bytes | None:
        """The raw bytes of one stored score file, or None when there is none; `oah.benchmarks` checks them."""
        path = self.dir / name
        try:
            return _retry_permission(path.read_bytes, RETRY_SECONDS)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(path, exc) from exc

    def save_scores(self, name: str, data: Mapping) -> None:
        """Store one fetched score file whole: a reader sees the old file or the new one, never part of one."""
        write_json_atomic(self.dir / name, data)

    def drop_scores(self, name: str) -> None:
        """Remove one stored score file; there being none is fine."""
        path = self.dir / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StorageError(path, exc) from exc

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
                     refresh: bool = False, now: dt.datetime | None = None, persist: bool = True,
                     aliases: Sequence[str] = ()) -> Freshness:
        """The catalog entry to use for this launch, rebuilt first when it is due; `persist=False` stores nothing.
        `aliases` are the known Claude aliases a rebuild may confirm when the help omits them."""
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
        models, source = probe_models(engine, base, env, aliases)
        if not models:
            kept = f"keeping the catalog checked at {stored.checked_at}" if stored else "no earlier catalog exists"
            warnings.append(f"OAH_MODELS_REFRESH_FAILED: {engine} reported no models ({source}); {kept}. Run the "
                            f"{engine} CLI once so it refreshes its model list, then `oah models --refresh`")
            if stored is not None:
                return Freshness(stored, None, tuple(warnings), version)
            return Freshness(Entry(engine, version, None, (), source), None, tuple(warnings), version)
        entry = Entry(engine, version, _iso(now), models, source)
        if persist:
            self._save(entry)
        return Freshness(entry, reason, tuple(warnings), version, previous=stored)

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

    def record_changes(self, engine: str, changes: Sequence[Mapping], *, now: dt.datetime | None = None) -> None:
        """Append one line per step whose rule choice a rebuild changed."""
        stamp = _iso(now or utc_now())
        with StateLock(self.lock_path):
            for change in changes:
                append_jsonl(self.changes_path, {"ts": stamp, "engine": engine, "field": "model", **change})

    def changes(self) -> list[dict]:
        return read_jsonl(self.changes_path)

    def unpooled_notices(self, pools: Mapping[str, Mapping[str, Sequence[str]]]) -> list[dict]:
        """Notices from the stored catalog and current pools, dated by each entry's checked time."""
        try:
            notices = []
            for engine, pool in sorted(pools.items()):
                if engine not in ("claude", "codex"):
                    continue
                entry = self.load(engine)
                for model in unpooled_models(entry, pool):
                    notices.append({"code": NEW_UNPOOLED, "engine": engine, "model": model,
                                    "date": _utc_date(entry.checked_at),
                                    "message": unpooled_warning(engine, model)})
            return notices
        except _NOTICE_FAILURES:
            return []


def _by_tier(names: Sequence[str], tier: str) -> str:
    if tier == "low":
        return names[-1]
    if tier == "medium":
        return names[1] if len(names) > 1 else names[0]
    return names[0]


def user_order(engine: str, quality_order: Sequence[str]) -> list[str]:
    """The user's own ranking; the old Claude template default is no choice of theirs, so it counts as unset."""
    return [] if engine == "claude" and tuple(quality_order) == OLD_CLAUDE_ORDER else list(quality_order)


def pin_line(engine: str, tier: str) -> str:
    return f'[engines.{engine}.tiers] {tier} = {{ model = "<model>" }}'


def map_tier(engine: str, tier: str, *, pin: str, quality_order: Sequence[str], entry: Entry | None,
             scores: scoring.ScoreSet | None, floor_tier: str = "low", profile: str = scoring.DEFAULT_PROFILE,
             pools: Mapping[str, Sequence[str]] | None = None, allowed: Sequence[str] = (),
             effort_rules: Callable[[str], bool] | None = None) -> tuple[str, str]:
    """(model, source) for one tier; source is pin, pool, quality_order or scored."""
    if pin:
        return pin, "pin"
    listed = sorted(_listed(entry))
    if not listed:
        raise EngineError(CATALOG_UNAVAILABLE, f"oah has no list of {engine} models, so nothing can be chosen for tier "
                          f"{tier}. Run the {engine} CLI once so it refreshes its model list, then `oah models "
                          f"--refresh`, or set {pin_line(engine, tier)} in .oah/config.toml")
    usable = [name for name in listed if not allowed or name in allowed]
    pool = (pools or {}).get(tier)
    if pool:
        named = [name for name in pool if name in usable]
        if not named:
            raise EngineError(UNRESOLVED, f"[engines.{engine}.tier_pools] {tier} names no model the {engine} CLI lists "
                              f"and allowed_models permits (it lists: {', '.join(listed)}). Fix that pool in "
                              f".oah/config.toml, or set {pin_line(engine, tier)}")
        runnable_names = [name for name in named if effort_rules is None or effort_rules(name)]
        if not runnable_names:
            raise scoring.FloorUnmet(UNRESOLVED, f"no model in [engines.{engine}.tier_pools] {tier} is runnable; "
                                     "change its tier effort or effort_caps in .oah/config.toml")
        return runnable_names[0], "pool"
    order = [name for name in user_order(engine, quality_order) if name in usable]
    if order:
        runnable_order = [name for name in order if effort_rules is None or effort_rules(name)]
        if not runnable_order:
            raise scoring.FloorUnmet(UNRESOLVED, f"no model in [engines.{engine}] quality_order is runnable; "
                                     "change its tier effort or effort_caps in .oah/config.toml")
        return _by_tier(runnable_order, tier), "quality_order"
    if scores is None:
        raise EngineError(scoring.NO_SCORES, f"no benchmark scores are available for {engine}; run `oah doctor`, or set "
                          f"{pin_line(engine, tier)} in .oah/config.toml")
    return scoring.choose(scores, tier, floor_tier, profile, usable=effort_rules).model, "scored"


def rule_decides(engine: str, section: Mapping, entry: Entry | None) -> bool:
    """Whether the score rule picks any tier; with a user ranking or every tier pinned or pooled, it picks none."""
    allowed = section["allowed_models"]
    listed = _listed(entry)
    order = [name for name in user_order(engine, section["quality_order"]) if name in listed
             and (not allowed or name in allowed)]
    return not order and any(not section["tiers"][tier]["model"] and not section["tier_pools"].get(tier)
                             for tier in section["tiers"])


def model_changes(before: scoring.ScoreSet, after: scoring.ScoreSet, profile: str, entry: Entry) -> list[dict]:
    """One row per step where the rule now chooses another model; no scores on either side compares nothing."""
    if not before.models or not after.models:
        return []
    known = {item.model for item in before.models}
    listed = _listed(entry)
    rows = []
    for step in range(scoring.TOP_STEP + 1):
        old, new = scoring.at_step(before, step, profile).model, scoring.at_step(after, step, profile).model
        if old != new:
            why = (f"{old} is no longer listed" if old not in listed else
                   "newer generation" if after.left_out.get(old) == "superseded" or new not in known else
                   "the scores changed" if _data_of(before) != _data_of(after) else
                   "the eligible models changed")
            rows.append({"step": step, "old": old, "new": new, "why": why})
    return rows


def _data_of(scores: scoring.ScoreSet) -> tuple[str, str, str, str]:
    """Which data a score set came from: its source, date, fetch time and credit."""
    return scores.source, scores.date, scores.fetched_at, scores.credit


def changed_warning(engine: str, change: Mapping) -> str:
    return (f"{CHANGED}: {engine} step {change['step']} now uses {change['new']} (was {change['old']}): {change['why']}; "
            f"run `oah models` to see the scores, or set a pin in [engines.{engine}.tiers] to keep a model")


def score_notices(engine: str, scores: scoring.ScoreSet) -> list[str]:
    """What a rebuild tells the user about listed models the rule left out; only plain names are shown."""
    notices = []
    for name, reason in sorted(scores.left_out.items()):
        if not PLAIN_MODEL.fullmatch(name):
            continue
        if reason == "unknown-family":
            notices.append(f"{UNKNOWN_FAMILY}: the {engine} CLI lists {name}, which is no family oah knows, so it is "
                           f"never chosen automatically. To use it, name it in [engines.{engine}] quality_order or a "
                           "tier in .oah/config.toml, or update oah")
        elif reason == "unscored":
            stays = scores.stays.get(name)
            kept = f", so {stays} stays" if stays else ""
            notices.append(f"{UNSCORED}: the {engine} CLI lists {name}, which has no usable score yet{kept}. It joins "
                           "when a score arrives; to use it now, name it in .oah/config.toml")
    return notices


def order_notice(engine: str, quality_order: Sequence[str]) -> str | None:
    """The one line `oah models` says when the old template default is in the file; the file stays as it is."""
    if user_order(engine, quality_order) == list(quality_order) or not quality_order:
        return None
    return (f"{ORDER_IGNORED}: [engines.{engine}] quality_order is the old default, so it was ignored and scores choose. "
            "Remove the line, or list other models in .oah/config.toml to rank them yourself")


def score_report(scores: scoring.ScoreSet, profile: str, *, left_out: Mapping[str, str] | None = None) -> dict:
    """The profile, the scores with their credit, and each listed model left out with its reason."""
    rows = [{"model": item.model, "family": item.family, "score": round(item.score, 1), "cost": item.cost}
            for item in scores.models]
    block = {"source": scores.source, "date": scores.date, "credit": scores.credit, "models": rows}
    if scores.fetched_at:
        block["fetched_at"] = scores.fetched_at  # last, so every earlier key keeps its place
    return {"profile": profile,
            "scores": block if rows else None,
            "left_out": {name: reason for name, reason in sorted({**scores.left_out, **(left_out or {})}.items())
                         if PLAIN_MODEL.fullmatch(name)}}


def offline_claude(allowed: Sequence[str] = ()) -> tuple[Entry | None, scoring.ScoreSet]:
    """Every known Claude alias with the packaged scores, for `oah init`, which reads no catalog and runs no CLI."""
    try:
        known = scoring.families()
        aliases = tuple(ModelInfo(alias, ()) for alias in known["engines"]["claude"]["aliases"])
    except EngineError as exc:
        return None, scoring.failed("claude", exc)
    entry = Entry("claude", None, None, aliases, "packaged aliases")
    try:
        return entry, scoring.score_set("claude", entry, allowed=allowed, known=known)
    except EngineError as exc:
        return entry, scoring.failed("claude", exc)


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
    # "ranked": quality_order ranks the models; "pools": the user's tier pools, unranked peers; "scored": the
    # score band; "deterministic": effort only without pools; no candidates when an applicable pool is missing.
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
    qualified: tuple[str, ...] = ()  # in both tiers' pools (pools mode), or in the score band (scored mode)
    admission_rule: Callable[[str, str], str | None] | None = None  # the call's model-specific review checks
    floor_effort: str | None = None  # the configured label, including highest; never another model's maximum

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
        if self.pools:
            pools = dict(self.pools)
            if any(model not in pools.get(tier, ()) for tier in (self.floor_tier, self.tier)):
                return "not-in-tier-pool"
        if self.ranked:
            if model not in self.order:
                return "unranked-model"
            if self.order.index(model) > self.order.index(self.floor.model):
                return "below-floor"
        elif self.mode == "pools":
            if model not in self.qualified:
                return "not-in-tier-pool"
        elif self.mode == "scored":
            if model not in self.qualified:
                return "outside-score-bound"
        elif model != self.deterministic.model:
            return "outside-candidates"
        result = runnable(info, effort, self.floor_effort or self.floor_tier, dict(self.caps).get(model))
        if result.reason == "cap-below-floor" or (result.reason is None and result.launch != effort):
            return "above-cap"
        if result.reason is not None:
            return "below-floor"
        return self.admission_rule(model, effort) if self.admission_rule is not None else None

    def to_json(self) -> dict:
        return {"deterministic": self.deterministic.to_json(),
                "floor": {"tier": self.floor_tier, **self.floor.to_json()}, "ranked": self.ranked,
                "mode": self.mode, "note": self.note, "allowed_models": list(self.allowed), "caps": dict(self.caps),
                "tier_pools": {tier: list(names) for tier, names in self.pools},
                "candidates": [[pair.model, pair.effort] for pair in self.pairs], "excluded": dict(self.excluded)}


def build_candidates(engine: str, *, entry: Entry | None, deterministic: Pair, tier: str, floor_tier: str,
                     floor: Pair, quality_order: Sequence[str], allowed_models: Sequence[str],
                     effort_caps: Mapping[str, str], tier_pools: Mapping[str, Sequence[str]],
                     scores: scoring.ScoreSet | None = None, profile: str = scoring.DEFAULT_PROFILE,
                     admission_rule: Callable[[str, str], str | None] | None = None,
                     tier_effort: str | None = None, floor_effort: str | None = None) -> CandidateSet:
    """The closed candidate set for one call (see Candidates above)."""
    listed = [model.name for model in entry.models if model.listed] if entry else []
    order = tuple(name for name in user_order(engine, quality_order) if name in listed)
    ranked = bool(order) and floor.model in order
    allowed = tuple(dict.fromkeys(allowed_models))
    pools = tuple((name, tuple(dict.fromkeys(tier_pools[name]))) for name in tier_pools)
    pool = dict(pools)
    applicable = tuple(dict.fromkeys((floor_tier, tier)))
    pooled = all(pool.get(name) for name in applicable)
    scored = scores is not None and bool(scores.models) and not order and not pools
    mode = "ranked" if ranked else "pools" if pooled else "scored" if scored else "deterministic"
    note = None
    if mode == "deterministic":
        missing = " and ".join(name for name in applicable if not pool.get(name))
        why = f"no tier pool for {missing}" if pools else "no tier pools"
        note = (f"model choice off: quality_order does not rank {floor.model} and {why}" if order
                else f"model choice off: no ranking and {why}")
    # Peers keep the call tier's pool order; a model must be in every applicable pool.
    qualified = tuple(name for name in pool.get(tier, ()) if all(name in pool[t] for t in applicable)) \
        if mode == "pools" else ()
    if mode == "scored":
        # The effort rules can move the rule's choice, so the model that would run is the one always offered.
        def usable(name: str) -> bool:
            info = entry.model(name) if entry is not None else None
            return info is not None and runnable(info, tier_effort or tier,
                                                  floor_effort or floor_tier, effort_caps.get(name)).reason is None

        qualified = scoring.band(scores, replace(scoring.choose(scores, tier, floor_tier, profile, usable=usable),
                                                 model=deterministic.model))
    rule = CandidateSet(engine, deterministic, floor_tier, floor, mode, order if ranked else (), allowed,
                        tuple(sorted(effort_caps.items())), entry, note=note, tier=tier, pools=pools,
                        qualified=qualified, admission_rule=admission_rule, floor_effort=floor_effort)
    names = list(order) if ranked else list(qualified) if mode in ("pools", "scored") else [deterministic.model]
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


def _listed(entry: Entry | None) -> set[str]:
    return {model.name for model in entry.models if model.listed} if entry else set()


def unpooled_models(entry: Entry | None, pools: Mapping[str, Sequence[str]] | None) -> list[str]:
    """The plain listed model names in no pool, sorted; nothing when no pools are configured.

    Listed long ago or just now, both count: this compares with the pools, not with an earlier catalog."""
    if not pools:
        return []
    pooled = {name for names in pools.values() for name in names}
    return sorted(name for name in _listed(entry) - pooled if PLAIN_MODEL.fullmatch(name))


def unpooled_warning(engine: str, model: str) -> str:
    return (f"{NEW_UNPOOLED}: the {engine} CLI lists {model}, which is in no [engines.{engine}.tier_pools] entry, so "
            f"the judge will not pick it. To qualify it, add the name to [engines.{engine}.tier_pools] in "
            ".oah/config.toml; oah will not add it for you")


def check_caps(engine: str, caps: Mapping[str, str], allowed: Sequence[str], entry: Entry | None, *,
               fresh: bool, pools: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    """Warnings for capped, allowed or pooled models the catalog does not list; refuse when a fresh catalog lists
    none of the capped models."""
    listed = _listed(entry)
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
