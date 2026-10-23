# 스킬 자동 갱신 — 허용 목록의 업스트림을 받아 라이선스·적재·개인정보·수상한 변경을 검사한 뒤에만 핀을 올리고, 실패하면 마지막 정상 핀으로 되돌린다.
"""Keep bundled and installed skills current, safely.

For each item the tracked ref is fetched once and staged. Each destination (an installed skill folder,
or the kit's own `skills/<item>/` with `--vendored`) then gets its new copy only if every check passes:
same SPDX id and license text digest, the skill loads, no key shapes or home paths (plus gitleaks when
installed), and nothing suspicious (new executable, binary blob, size jump). Anything else keeps the
last good pin and is reported. Automatic runs wait while any lease is active.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from oah.atomic import read_json, write_json_atomic, write_text_atomic
from oah.engines.proc import Resolved, UnsupportedBatchShim, resolve_command
from oah.lease import Store
from oah.lockfile import LockTimeout, StateLock
from oah.toolkit.catalog import Bundled, Catalog, CatalogError, Upstream, read_upstream
from oah.toolkit.checks import DiffSummary, diff_summary, identify_license, privacy_problems, suspicious, text_digest
from oah.toolkit.fetch import FetchError, StageError, Tree, copy_tree, fetch, stage_item
from oah.toolkit.install import KIT_MARK, build_copy, installed_skills, marker_of, swap_in, target_dirs
from oah.toolkit.skillmeta import MARKER_FILE, UPSTREAM_FILE, load_problems

DEFAULTS = {"update_interval_days": 7, "auto_update": True, "size_jump_ratio": 2.0,
            "size_jump_min_bytes": 262144}
STATE_FOLDER = "toolkit"
STATE_FILE = "updates.json"
LOCK_TIMEOUT = 2.0

Fetcher = Callable[..., Tree]


@dataclass(frozen=True)
class Destination:
    """One copy of an item that an update may replace."""

    item: str
    kind: str              # "installed" or "vendored"
    path: pathlib.Path
    skill: str             # catalog skill folder for installed copies, "" for vendored
    commit: str
    spdx: str
    license_sha256: str
    executables: tuple[str, ...]
    level: str             # "project", "user" or "kit"


@dataclass
class UpdateResult:
    item: str
    status: str            # current, available, blocked, advanced, rolled-back, failed, deferred
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
        line = " ".join(parts)
        return line + "".join(f"\n            - {reason}" for reason in self.reasons)

    def to_json(self) -> dict:
        data = {"item": self.item, "status": self.status, "destination": self.destination,
                "old_commit": self.old_commit, "new_commit": self.new_commit, "reasons": list(self.reasons)}
        if self.diff is not None:
            data["diff"] = {"files_changed": self.diff.files_changed, "added": self.diff.added,
                            "removed": self.diff.removed}
        return data


def installed_destinations(catalog: Catalog, *, project_root: pathlib.Path, home: pathlib.Path,
                           include_user: bool) -> list[Destination]:
    """Every skill folder oah installed from a bundled item, at project level and optionally user level."""
    folders = target_dirs("both", "project", project_root=project_root, home=home)
    levels = {folder: "project" for _, folder in folders}
    if include_user:
        user = target_dirs("both", "user", project_root=project_root, home=home)
        folders += user
        levels.update({folder: "user" for _, folder in user})
    names = {entry.name for entry in catalog.bundled}
    found = []
    for _, folder in installed_skills(folders):
        marker = marker_of(folder)
        if not marker or marker.get("kit") != KIT_MARK or marker.get("item") not in names:
            continue
        try:
            found.append(Destination(item=marker["item"], kind="installed", path=folder, skill=marker["skill"],
                                     commit=marker["commit"], spdx=marker["spdx"],
                                     license_sha256=marker["license_sha256"],
                                     executables=tuple(marker.get("executables", [])), level=levels[folder.parent]))
        except (KeyError, TypeError):
            continue
    return found


def vendored_destinations(catalog: Catalog, kit_skills: pathlib.Path) -> list[Destination]:
    found = []
    for entry in catalog.bundled:
        folder = kit_skills / entry.name
        if not (folder / UPSTREAM_FILE).is_file():
            continue
        record = read_upstream(folder)
        found.append(Destination(item=entry.name, kind="vendored", path=folder, skill="", commit=record.commit,
                                 spdx=record.spdx, license_sha256=record.license_sha256,
                                 executables=record.executables, level="kit"))
    return found


def _new_copy(destination: Destination, entry: Bundled, staged: pathlib.Path, record: Upstream,
              out: pathlib.Path, now: dt.datetime) -> pathlib.Path:
    if destination.kind == "vendored":
        copy_tree(staged, out)
        return out
    stamp = now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return build_copy(staged, entry, destination.skill, record, out, installed_at=stamp)


def _executables(destination: Destination, record: Upstream) -> list[str]:
    if destination.kind == "vendored":
        return list(record.executables)
    prefix = "" if destination.skill == "." else destination.skill + "/"
    return [name[len(prefix):] for name in record.executables if name.startswith(prefix)]


def _verify(destination: Destination, entry: Bundled) -> Callable[[pathlib.Path], list[str]]:
    def check(folder: pathlib.Path) -> list[str]:
        if destination.kind == "installed":
            return load_problems(folder)
        problems: list[str] = []
        for relative in entry.skills:
            skill = folder if relative == "." else folder.joinpath(*relative.split("/"))
            problems.extend(load_problems(skill, item_root=folder))
        return problems
    return check


def _checks(destination: Destination, entry: Bundled, record: Upstream, new: pathlib.Path, privacy: list[str],
            settings: Mapping) -> list[str]:
    reasons = list(privacy)
    license_path = new / (record.license_file if destination.kind == "vendored"
                          else record.license_file.rsplit("/", 1)[-1])
    try:
        license_bytes = license_path.read_bytes()
    except OSError:
        return reasons + [f"the license file {record.license_file} is missing from the new copy"]
    reasons += suspicious(old_spdx=destination.spdx, new_spdx=identify_license(license_bytes.decode("utf-8", "replace")),
                          old_license=destination.license_sha256, new_license=text_digest(license_bytes),
                          old_executables=destination.executables, new_executables=_executables(destination, record),
                          old_dir=destination.path, new_dir=new,
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


def run_update(*, catalog: Catalog, destinations: Sequence[Destination], check_only: bool, settings: Mapping,
               now: dt.datetime, gitleaks: Resolved | None, fetcher: Fetcher = fetch,
               catalog_path: pathlib.Path | None = None) -> list[UpdateResult]:
    """Fetch each item once, check every destination's new copy, and advance only the ones that pass."""
    results: list[UpdateResult] = []
    by_item: dict[str, list[Destination]] = {}
    for destination in destinations:
        by_item.setdefault(destination.item, []).append(destination)
    for item, group in by_item.items():
        entry = catalog.find(item)
        if not isinstance(entry, Bundled):
            continue
        with tempfile.TemporaryDirectory(prefix="oah-update-") as scratch_text:
            scratch = pathlib.Path(scratch_text)
            try:
                tree = fetcher(entry.repository, entry.ref, scratch / "fetch",
                               paths=tuple(p for p in (entry.subpath, entry.license_path, *entry.notice_paths) if p))
            except (FetchError, CatalogError) as exc:
                results.extend(UpdateResult(item, "failed", str(d.path), d.commit, "", None, [str(exc)]) for d in group)
                continue
            stale = [d for d in group if d.commit != tree.commit]
            results.extend(UpdateResult(item, "current", str(d.path), d.commit, tree.commit)
                           for d in group if d.commit == tree.commit)
            if not stale:
                continue
            try:
                record = stage_item(entry, tree, scratch / "staged", fetched=now.date())
            except StageError as exc:
                reason = str(exc)
                if "reads as" in reason:
                    reason = f"license changed: {reason}"
                status = "blocked" if check_only else "rolled-back"
                results.extend(UpdateResult(item, status, str(d.path), d.commit, tree.commit, None,
                                            [reason, f"kept {d.commit[:12]}"] if not check_only else [reason])
                               for d in stale)
                continue
            privacy = privacy_problems(scratch / "staged", gitleaks=gitleaks, ignore=(UPSTREAM_FILE,))
            for number, destination in enumerate(stale):
                new = _new_copy(destination, entry, scratch / "staged", record, scratch / f"new-{number}", now)
                ignore = (UPSTREAM_FILE,) if destination.kind == "vendored" else (MARKER_FILE,)
                summary = diff_summary(destination.path, new, ignore=ignore)
                reasons = _checks(destination, entry, record, new, privacy, settings)
                result = UpdateResult(item, "", str(destination.path), destination.commit, tree.commit, summary)
                if reasons:
                    result.status = "blocked" if check_only else "rolled-back"
                    result.reasons = reasons + ([] if check_only else [f"kept {destination.commit[:12]}"])
                elif check_only:
                    result.status = "available"
                else:
                    try:
                        swap_in(new, destination.path, verify=_verify(destination, entry))
                        if destination.kind == "vendored" and catalog_path is not None:
                            _write_catalog_commit(catalog_path, item, tree.commit)
                        result.status = "advanced"
                    except Exception as exc:  # the swap restored the last good copy
                        result.status = "rolled-back"
                        result.reasons = [f"could not replace the copy: {exc}",
                                          f"restored {destination.commit[:12]}"]
                results.append(result)
    return results


def active_leases(state_dir: pathlib.Path) -> list[str]:
    """Tasks holding a lease that has not expired; read-only, takes no lock."""
    return [lease.task for lease, stale in Store(state_dir).snapshot() if not stale]


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


def record_run(state_dir: pathlib.Path, now: dt.datetime, results: Sequence[UpdateResult]) -> None:
    write_json_atomic(_state_path(state_dir), {
        "last_check": now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "results": [result.to_json() for result in results]})


def find_gitleaks(search_path: str | None = None) -> Resolved | None:
    try:
        return resolve_command("gitleaks", search_path)
    except UnsupportedBatchShim:
        return None


def scheduled_update(*, catalog: Catalog, state_dir: pathlib.Path, project_root: pathlib.Path,
                     home: pathlib.Path, settings: Mapping, now: dt.datetime, include_user: bool = False,
                     gitleaks: Resolved | None = None, fetcher: Fetcher = fetch) -> tuple[str, list[UpdateResult]]:
    """The automatic check: at most once per interval, never while a lease is active.

    Returns (state, results) where state is 'disabled', 'not-due', 'deferred', 'busy' or 'ran'.
    A deferred check does not record a run, so the next mutating command tries again.
    """
    if not settings.get("auto_update", True):
        return "disabled", []
    if not due(read_state(state_dir), now, float(settings["update_interval_days"])):
        return "not-due", []
    holders = active_leases(state_dir)
    if holders:
        return "deferred", [UpdateResult("*", "deferred", reasons=[f"waiting for active leases: {', '.join(holders)}"])]
    try:
        with StateLock(pathlib.Path(state_dir) / STATE_FOLDER / ".lock", timeout=LOCK_TIMEOUT):
            if not due(read_state(state_dir), now, float(settings["update_interval_days"])):
                return "not-due", []
            destinations = installed_destinations(catalog, project_root=project_root, home=home,
                                                  include_user=include_user)
            results = run_update(catalog=catalog, destinations=destinations, check_only=False, settings=settings,
                                 now=now, gitleaks=gitleaks, fetcher=fetcher)
            record_run(state_dir, now, results)
            return "ran", results
    except LockTimeout:
        return "busy", []

