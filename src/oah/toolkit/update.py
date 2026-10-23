# 스킬 자동 갱신 — 허용 목록의 업스트림을 받아 라이선스·적재·개인정보·수상한 변경을 검사한 뒤에만 핀을 올리고, 실패하면 마지막 정상 핀을 지킨다.
"""Keep skills current, safely.

Where each kind of copy changes:

| copy                                        | changed by                                              |
|---------------------------------------------|---------------------------------------------------------|
| kit cache (`<state>/toolkit/cache/`)        | the automatic check, or `oah skills update`             |
| project `.claude/skills`, `.agents/skills`  | only `oah skills update --project`, under its own lease |
| user `~/.claude/skills`, `~/.agents/skills` | only `oah skills update --global`, with confirmation    |
| the kit's own `skills/`                     | only `oah skills update --vendored` (kit maintainers)   |

Each run first prepares: it fetches every item's tracked ref once, stages it, and checks each new copy
(same SPDX id and license text digest, the skills load, no key shapes or home paths plus gitleaks when
installed, nothing suspicious). A copy that fails keeps its last good pin. Then it publishes inside a
guard (for shared copies: the state lock, with no lease active) and swaps the passing copies in; a
failed swap restores the old copy. unlazy is never updated here; the gates lane pins it.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Callable, ContextManager, Iterator, Mapping, Sequence

from oah.atomic import append_jsonl, read_json, write_json_atomic, write_text_atomic
from oah.engines.proc import Resolved, UnsupportedBatchShim, resolve_command
from oah.lease import Store
from oah.lockfile import LockTimeout, StateLock
from oah.toolkit.catalog import Bundled, Catalog, CatalogError, Upstream, read_upstream
from oah.toolkit.checks import DiffSummary, diff_summary, identify_license, privacy_problems, suspicious, text_digest
from oah.toolkit.fetch import FetchError, StageError, Tree, copy_tree, fetch, manifest_problems, stage_item
from oah.toolkit.install import (KIT_MARK, _identity, build_copy, installed_executables,
                                 installed_skills, marker_of, publication_lock, swap_in, target_dirs)
from oah.toolkit.skillmeta import MARKER_FILE, UPSTREAM_FILE, load_problems

DEFAULTS = {"update_interval_days": 1, "auto_update": True, "size_jump_ratio": 2.0,
            "size_jump_min_bytes": 262144, "pins": []}
NEVER_UPDATED = frozenset({"unlazy"})
STATE_FOLDER = "toolkit"
STATE_FILE = "updates.json"
LOG_FILE = "updates.jsonl"
CACHE_FOLDER = "cache"
LOCK_TIMEOUT = 10.0

Fetcher = Callable[..., Tree]
Guard = Callable[[], ContextManager[None]]


class LeasesActive(Exception):
    code = "OAH_SKILLS_LEASES_ACTIVE"

    def __init__(self, holders: Sequence[str]) -> None:
        self.holders = list(holders)
        super().__init__(f"{self.code}: tasks {', '.join(holders)} hold leases; shared skill copies change only "
                         "when no session is working. Run this again after oah finish, or use --check to look "
                         "without changing anything")


class CheckNotDue(Exception):
    """Another check published while this check was preparing."""


@dataclass(frozen=True)
class Destination:
    """One copy of an item that an update may replace."""

    item: str
    kind: str                       # "installed" (one skill folder) or "item" (a whole item: cache or kit)
    path: pathlib.Path              # where the new copy is published
    baseline: pathlib.Path | None   # the last good copy to compare with; None: fetch the catalog pin
    skill: str                      # catalog skill folder for installed copies, "" for items
    commit: str
    spdx: str
    license_sha256: str
    executables: tuple[str, ...]
    level: str                      # "project", "global", "cache" or "kit"


@dataclass
class UpdateResult:
    item: str
    status: str   # current, available, blocked, advanced, seeded, rolled-back, failed, deferred, pinned
    destination: str = ""
    old_commit: str = ""
    new_commit: str = ""
    diff: DiffSummary | None = None
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"{self.status:11} {self.item}"]
        if self.old_commit or self.new_commit:
            parts.append(f"{self.old_commit[:12] or '-'} -> {self.new_commit[:12] or '-'}")
        if self.destination:
            parts.append(f"at {self.destination}")
        if self.diff is not None:
            parts.append(f"({self.diff.describe()})")
        return " ".join(parts) + "".join(f"\n            - {reason}" for reason in self.reasons)

    def to_json(self) -> dict:
        data = {"item": self.item, "status": self.status, "destination": self.destination,
                "old_commit": self.old_commit, "new_commit": self.new_commit, "reasons": list(self.reasons)}
        if self.diff is not None:
            data["diff"] = {"files_changed": self.diff.files_changed, "added": self.diff.added,
                            "removed": self.diff.removed}
        return data


def cache_dir(state_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(state_dir) / STATE_FOLDER / CACHE_FOLDER


def installed_destinations(catalog: Catalog, *, project_root: pathlib.Path, home: pathlib.Path,
                           level: str) -> list[Destination]:
    """Every skill folder oah installed from a bundled item at level 'project' or 'global'."""
    folders = target_dirs("both", "project" if level == "project" else "user", project_root=project_root, home=home)
    names = {entry.name for entry in catalog.bundled}
    found = []
    for _, folder in installed_skills(folders):
        marker = marker_of(folder)
        if not marker or marker.get("kit") != KIT_MARK or marker.get("item") not in names:
            continue
        try:
            found.append(Destination(item=marker["item"], kind="installed", path=folder, baseline=folder,
                                     skill=marker["skill"], commit=marker["commit"], spdx=marker["spdx"],
                                     license_sha256=marker["license_sha256"],
                                     executables=tuple(marker.get("executables", [])), level=level))
        except (KeyError, TypeError):
            continue
    return found


def _item_destination(entry: Bundled, path: pathlib.Path, baseline: pathlib.Path | None, level: str) -> Destination:
    if baseline is None:
        return Destination(entry.name, "item", path, None, "", entry.commit, entry.spdx, "", (), level)
    record = read_upstream(baseline)
    return Destination(entry.name, "item", path, baseline, "", record.commit, record.spdx, record.license_sha256,
                       record.executables, level)


def vendored_destinations(catalog: Catalog, kit_skills: pathlib.Path) -> list[Destination]:
    return [_item_destination(entry, kit_skills / entry.name, kit_skills / entry.name, "kit")
            for entry in catalog.bundled if (kit_skills / entry.name / UPSTREAM_FILE).is_file()]


def cache_destinations(catalog: Catalog, *, state_dir: pathlib.Path,
                       bundle_dir: pathlib.Path | None) -> list[Destination]:
    """One per bundled item. A missing cache entry compares with the bundled copy, or the fetched pin."""
    found = []
    for entry in catalog.bundled:
        cached = cache_dir(state_dir) / entry.name
        if (cached / UPSTREAM_FILE).is_file():
            baseline: pathlib.Path | None = cached
        elif bundle_dir is not None and (bundle_dir / entry.name / UPSTREAM_FILE).is_file():
            baseline = bundle_dir / entry.name
        else:
            baseline = None
        found.append(_item_destination(entry, cached, baseline, "cache"))
    return found


def _new_copy(destination: Destination, entry: Bundled, staged: pathlib.Path, record: Upstream,
              out: pathlib.Path, now: dt.datetime) -> pathlib.Path:
    if destination.kind == "item":
        copy_tree(staged, out)
        return out
    stamp = now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return build_copy(staged, entry, destination.skill, record, out, installed_at=stamp)


def _executables(destination: Destination, record: Upstream, entry: Bundled) -> list[str]:
    if destination.kind == "item":
        return list(record.executables)
    return installed_executables(entry, destination.skill, record)


def _verify(destination: Destination, entry: Bundled) -> Callable[[pathlib.Path], list[str]]:
    def check(folder: pathlib.Path) -> list[str]:
        if destination.kind == "installed":
            return load_problems(folder)
        problems = manifest_problems(folder, read_upstream(folder))
        for relative in entry.skills:
            skill = folder if relative == "." else folder.joinpath(*relative.split("/"))
            problems.extend(load_problems(skill, item_root=folder))
        return problems
    return check


def _checks(destination: Destination, entry: Bundled, record: Upstream, new: pathlib.Path, baseline: pathlib.Path,
            privacy: list[str], settings: Mapping) -> list[str]:
    reasons = list(privacy)
    name = record.license_file if destination.kind == "item" else record.license_file.rsplit("/", 1)[-1]
    try:
        license_bytes = (new / name).read_bytes()
    except OSError:
        return reasons + [f"the license file {record.license_file} is missing from the new copy"]
    reasons += suspicious(old_spdx=destination.spdx, new_spdx=identify_license(license_bytes.decode("utf-8", "replace")),
                          old_license=destination.license_sha256, new_license=text_digest(license_bytes),
                          old_executables=destination.executables, new_executables=_executables(destination, record, entry),
                          old_dir=baseline, new_dir=new,
                          size_jump_ratio=float(settings["size_jump_ratio"]),
                          size_jump_min_bytes=int(settings["size_jump_min_bytes"]))
    reasons += _verify(destination, entry)(new)
    return reasons


def _write_catalog_commit(catalog_path: pathlib.Path, item: str, commit: str) -> None:
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    for entry in data["bundled"]:
        if entry["name"] == item:
            entry["commit"] = commit
    write_text_atomic(catalog_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def no_guard() -> Iterator[None]:
    yield


def run_update(*, catalog: Catalog, destinations: Sequence[Destination], check_only: bool, settings: Mapping,
               now: dt.datetime, gitleaks: Resolved | None, fetcher: Fetcher = fetch,
               catalog_path: pathlib.Path | None = None, guard: Guard = no_guard,
               after_publish: Callable[[list[UpdateResult]], None] | None = None,
               home: pathlib.Path | None = None) -> list[UpdateResult]:
    """Snapshot preparation inputs and publish passing copies inside guard().

    guard holds the repository state lock, before the per-user lock for global copies.
    after_publish runs inside the same guard once publication ends (never for --check).
    """
    results: list[UpdateResult] = []
    ready: list[tuple[UpdateResult, Destination, Bundled, pathlib.Path]] = []
    inputs = {path for d in destinations for path in (d.path, d.baseline) if path is not None}
    if catalog_path is not None:
        inputs.add(catalog_path)
    publication_home = (home if home is not None else pathlib.Path.home()) if any(
        d.level == "global" for d in destinations) else None

    def user_lock() -> ContextManager[None]:
        return publication_lock(publication_home) if publication_home is not None else no_guard()

    with no_guard() if check_only else guard(), no_guard() if check_only else user_lock():
        identities = {path: _identity(path) for path in inputs}
        by_item: dict[str, list[Destination]] = {}
        for destination in destinations:
            if destination.baseline is not None:
                if destination.kind == "installed":
                    record = marker_of(destination.baseline) or {}
                    identity = (record.get("commit"), record.get("spdx"), record.get("license_sha256"),
                                tuple(record.get("executables", ())))
                else:
                    record = read_upstream(destination.baseline)
                    identity = (record.commit, record.spdx, record.license_sha256, record.executables)
                if identity != (destination.commit, destination.spdx, destination.license_sha256, destination.executables):
                    results.append(UpdateResult(destination.item, "deferred", str(destination.path), destination.commit,
                                                reasons=["baseline changed before preparation; run the update again"]))
                    continue
            by_item.setdefault(destination.item, []).append(destination)
        if results:
            return [UpdateResult(d.item, "deferred", str(d.path), d.commit,
                                 reasons=["baseline changed before preparation; run the update again"])
                    for d in destinations]
    with tempfile.TemporaryDirectory(prefix="oah-update-") as scratch_text:
        scratch = pathlib.Path(scratch_text)
        for number, (item, group) in enumerate(by_item.items()):
            entry = catalog.find(item)
            if not isinstance(entry, Bundled):
                continue
            if item in NEVER_UPDATED:
                results.extend(UpdateResult(item, "pinned", str(d.path), d.commit, d.commit, None,
                                            ["unlazy is pinned by the gates lane and never updated here"])
                               for d in group)
                continue
            if item in settings.get("pins", ()):
                results.extend(UpdateResult(item, "pinned", str(d.path), d.commit, d.commit, None,
                                            ["held by [skills] pins in .oah/config.toml; remove it there to update"])
                               for d in group)
                continue
            work = scratch / str(number)
            try:
                tree = fetcher(entry.repository, entry.ref, work / "fetch", paths=entry.fetch_paths())
                if any(d.baseline is None for d in group):
                    pin = fetcher(entry.repository, entry.commit, work / "pin-fetch", paths=entry.fetch_paths())
                    stage_item(entry, pin, work / "pin", fetched=now.date())
                    group = [d if d.baseline is not None else _item_destination(entry, d.path, work / "pin", d.level)
                             for d in group]
            except (FetchError, CatalogError, StageError) as exc:
                results.extend(UpdateResult(item, "failed", str(d.path), d.commit, "", None, [str(exc)]) for d in group)
                continue
            stale = []
            for d in group:
                if d.commit == tree.commit and d.path.exists():
                    results.append(UpdateResult(item, "current", str(d.path), d.commit, tree.commit))
                else:
                    stale.append(d)
            if not stale:
                continue
            try:
                record = stage_item(entry, tree, work / "staged", fetched=now.date())
            except StageError as exc:
                reason = f"license changed: {exc}" if "reads as" in str(exc) else str(exc)
                results.extend(UpdateResult(item, "blocked" if check_only else "rolled-back", str(d.path), d.commit,
                                            tree.commit, None, [reason] + ([] if check_only else [f"kept {d.commit[:12]}"]))
                               for d in stale)
                continue
            privacy = privacy_problems(work / "staged", gitleaks=gitleaks, ignore=(UPSTREAM_FILE,))
            for index, destination in enumerate(stale):
                assert destination.baseline is not None
                new = _new_copy(destination, entry, work / "staged", record, work / f"new-{index}", now)
                ignore = (UPSTREAM_FILE,) if destination.kind == "item" else (MARKER_FILE,)
                summary = diff_summary(destination.baseline, new, ignore=ignore)
                reasons = _checks(destination, entry, record, new, destination.baseline, privacy, settings)
                result = UpdateResult(item, "", str(destination.path), destination.commit, tree.commit, summary)
                results.append(result)
                if reasons:
                    result.status = "blocked" if check_only else "rolled-back"
                    result.reasons = reasons + ([] if check_only else [f"kept {destination.commit[:12]}"])
                elif check_only:
                    result.status = "available"
                    if not destination.path.exists():
                        result.reasons = [f"not in {destination.level} yet; the next update copies it there"]
                else:
                    ready.append((result, destination, entry, new))
        if check_only:
            return results
        try:
            with guard(), user_lock():
                if any(_identity(path) != identity for path, identity in identities.items()):
                    for result in results:
                        if result.status not in ("", "current"):
                            continue
                        result.status = "deferred"
                        result.reasons = ["destination or baseline changed during preparation; run the update again"]
                    return results
                for result, destination, entry, new in ready:
                    seeding = not destination.path.exists()
                    try:
                        swap_in(new, destination.path, verify=_verify(destination, entry))
                    except Exception as exc:  # swap_in restored the last good copy
                        result.status = "rolled-back"
                        result.reasons = [f"could not replace the copy: {exc}", f"restored {destination.commit[:12]}"]
                        continue
                    if destination.level == "kit" and catalog_path is not None:
                        _write_catalog_commit(catalog_path, result.item, result.new_commit)
                    result.status = "seeded" if seeding and result.old_commit == result.new_commit else "advanced"
                if after_publish is not None:
                    after_publish(results)
        except LeasesActive as exc:
            for result, *_ in ready:
                result.status = "deferred"
                result.reasons = [f"waiting for active leases: {', '.join(exc.holders)}"]
            raise
    return results


def active_leases(state_dir: pathlib.Path) -> list[str]:
    """Tasks holding a lease that has not expired; read-only, takes no lock."""
    return [lease.task for lease, stale in Store(state_dir).snapshot() if not stale]


@contextlib.contextmanager
def idle_state(state_dir: pathlib.Path) -> Iterator[None]:
    """Hold the state lock and refuse while any lease is active, so no lease starts mid-publication."""
    with StateLock(Store(state_dir).lock_path, timeout=LOCK_TIMEOUT):
        holders = active_leases(state_dir)
        if holders:
            raise LeasesActive(holders)
        yield


def _state_path(state_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(state_dir) / STATE_FOLDER / STATE_FILE


def read_state(state_dir: pathlib.Path) -> dict:
    data = read_json(_state_path(state_dir), {})
    return data if isinstance(data, dict) else {}


def _parse_time(text: object) -> dt.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def due(state: Mapping, now: dt.datetime, interval_days: float) -> bool:
    last = _parse_time(state.get("last_check"))
    return last is None or now - last >= dt.timedelta(days=interval_days) or last > now


def _stamp(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def log_results(state_dir: pathlib.Path, now: dt.datetime, results: Sequence[UpdateResult], why: str) -> None:
    """Append every result that is not 'current' to the update log: old and new commits, when and why."""
    path = pathlib.Path(state_dir) / STATE_FOLDER / LOG_FILE
    for result in results:
        if result.status != "current":
            append_jsonl(path, dict(result.to_json(), ts=_stamp(now), why=why))


def record_run(state_dir: pathlib.Path, now: dt.datetime, results: Sequence[UpdateResult], why: str = "automatic") -> None:
    """The cache check's run record (its last_check drives the interval) plus the update log."""
    write_json_atomic(_state_path(state_dir), {"last_check": _stamp(now), "why": why,
                                               "results": [result.to_json() for result in results]})
    log_results(state_dir, now, results, why)


def find_gitleaks(search_path: str | None = None) -> Resolved | None:
    try:
        return resolve_command("gitleaks", search_path)
    except UnsupportedBatchShim:
        return None


def refresh_cache(*, catalog: Catalog, state_dir: pathlib.Path, bundle_dir: pathlib.Path | None,
                  settings: Mapping, now: dt.datetime, check_only: bool = False, gitleaks: Resolved | None = None,
                  fetcher: Fetcher = fetch, why: str = "automatic") -> list[UpdateResult]:
    """Bring the kit cache up to date; publication and the run record happen under the state lock with no
    lease active. Raises LeasesActive (nothing written) when a lease is active at publication."""
    initial_state = read_state(state_dir)
    destinations = cache_destinations(catalog, state_dir=state_dir, bundle_dir=bundle_dir)

    @contextlib.contextmanager
    def publication_guard() -> Iterator[None]:
        with idle_state(state_dir):
            if why == "automatic":
                current_state = read_state(state_dir)
                if current_state != initial_state or not due(current_state, now, float(settings["update_interval_days"])):
                    raise CheckNotDue()
            yield

    return run_update(catalog=catalog, destinations=destinations, check_only=check_only, settings=settings,
                      now=now, gitleaks=gitleaks, fetcher=fetcher, guard=publication_guard,
                      after_publish=lambda results: record_run(state_dir, now, results, why))


def scheduled_update(*, catalog: Catalog, state_dir: pathlib.Path, bundle_dir: pathlib.Path | None,
                     settings: Mapping, now: dt.datetime, gitleaks: Resolved | None = None,
                     fetcher: Fetcher = fetch) -> tuple[str, list[UpdateResult]]:
    """The automatic check: refreshes only the kit cache, at most once per interval, never under a lease.

    Returns (state, results); state is 'disabled', 'not-due', 'deferred', 'busy' or 'ran'. A deferred
    check records nothing, so the next mutating command tries again.
    """
    if not settings.get("auto_update", True):
        return "disabled", []
    interval = float(settings["update_interval_days"])
    if not due(read_state(state_dir), now, interval):
        return "not-due", []
    holders = active_leases(state_dir)
    if holders:
        return "deferred", [UpdateResult("*", "deferred", reasons=[f"waiting for active leases: {', '.join(holders)}"])]
    try:
        results = refresh_cache(catalog=catalog, state_dir=state_dir, bundle_dir=bundle_dir, settings=settings,
                                now=now, gitleaks=gitleaks, fetcher=fetcher)
    except LeasesActive as exc:
        return "deferred", [UpdateResult("*", "deferred", reasons=[f"waiting for active leases: {', '.join(exc.holders)}"])]
    except LockTimeout:
        return "busy", []
    except CheckNotDue:
        return "not-due", []
    return "ran", results
