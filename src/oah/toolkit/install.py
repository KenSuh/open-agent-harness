# 스킬 설치·목록 — 번들 스킬을 Claude·Codex 스킬 폴더에 라이선스·설치 표시와 함께 복사하고, 연결 항목은 공식 설치 명령을 돌리며, 목록을 만든다.
"""Install bundled skills into Claude Code and Codex skill folders, and list what is where.

| engine | project            | user (needs consent) |
|--------|--------------------|----------------------|
| claude | .claude/skills/    | ~/.claude/skills/    |
| codex  | .agents/skills/    | ~/.agents/skills/    |

Every installed folder carries the item's license files and a `.oah-skill.json` marker, which is its
pin. A folder oah did not install is never replaced.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import pathlib
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, ContextManager, Iterable, Iterator, Mapping, Sequence

from oah.engines.proc import UnsupportedBatchShim, resolve_command
from oah.lockfile import LockTimeout, StateLock
from oah.toolkit.catalog import (Bundled, Catalog, CatalogError, Linked, Upstream, item_skill_dirs, read_upstream,
                                 skill_name)
from oah.toolkit.fetch import copy_tree, fetch, read_json_file, stage_item, write_marker
from oah.toolkit.skillmeta import MARKER_FILE, SKILL_FILE, UPSTREAM_FILE, SkillError, load_problems, read_skill

ENGINES = ("claude", "codex")
SKILL_FOLDERS = {"claude": (".claude", "skills"), "codex": (".agents", "skills")}
LINKED_TIMEOUT = 900.0
KIT_MARK = "open-agent-harness"


class InstallError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def target_dirs(target: str, level: str, *, project_root: pathlib.Path, home: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """(engine, skills folder) for target 'claude', 'codex' or 'both' at level 'project' or 'user'."""
    engines = ENGINES if target == "both" else (target,)
    base = pathlib.Path(project_root) if level == "project" else pathlib.Path(home)
    return [(engine, base.joinpath(*SKILL_FOLDERS[engine])) for engine in engines]


def _inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(pathlib.Path(root).resolve())
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Copy:
    item: str
    skill: str          # the catalog's relative skill folder
    name: str           # the installed folder name
    engine: str
    dest: pathlib.Path
    action: str         # "install", "keep", "replace" or "conflict"
    outside_project: bool
    reason: str = ""


@dataclass(frozen=True)
class InstallPlan:
    copies: tuple[Copy, ...]
    linked: tuple[Linked, ...]
    sources: Mapping[str, pathlib.Path | None]   # item -> folder to copy from; None: fetch the pin
    publication_home: pathlib.Path | None = None   # all skill copies share this home's publication lock

    @property
    def conflicts(self) -> tuple[Copy, ...]:
        return tuple(copy for copy in self.copies if copy.action == "conflict")

    @property
    def needs_consent(self) -> bool:
        return (any(copy.outside_project for copy in self.copies)
                or any(step.run is not None for entry in self.linked for step in entry.steps))

    def describe(self) -> list[str]:
        lines = []
        for copy in self.copies:
            where = "outside the project" if copy.outside_project else "in the project"
            note = f" ({copy.reason})" if copy.reason else ""
            lines.append(f"{copy.action:8} {copy.item}/{copy.name} -> {copy.dest} [{copy.engine}, {where}]{note}")
        for name, source in self.sources.items():
            lines.append(f"source   {name}: " + (str(source) if source is not None
                                                 else "fetched from its upstream at the pinned commit"))
        for entry in self.linked:
            for step in entry.steps:
                kind = "run" if step.run is not None else "manual"
                lines.append(f"{kind:8} {entry.name}: {step.describe()}")
        return lines


def marker_of(folder: pathlib.Path) -> dict | None:
    return read_json_file(pathlib.Path(folder) / MARKER_FILE)


def _classify(dest: pathlib.Path, item: str, *, project: bool = False, commit: str = "") -> tuple[str, str]:
    if not dest.exists():
        return "install", ""
    marker = marker_of(dest)
    if marker is None or marker.get("kit") != KIT_MARK:
        return "conflict", "a folder oah did not install is already there; rename or remove it first"
    if marker.get("item") != item:
        return "conflict", f"oah installed it from {marker.get('item')}; uninstall that first"
    if project:
        if marker.get("commit") != commit:
            return "conflict", "project copies are pins; run oah skills update --project to change this version"
        return "keep", "existing project pin is preserved; use oah skills update --project to update it"
    return "replace", ""


def item_source(name: str, folders: Sequence[pathlib.Path | None]) -> pathlib.Path | None:
    """The first of folders (kit cache, then bundled skills) that holds item name."""
    for folder in folders:
        if folder is not None and (folder / name / UPSTREAM_FILE).is_file():
            return folder / name
    return None


def plan_install(names: Sequence[str], *, catalog: Catalog, source_dirs: Sequence[pathlib.Path | None], target: str,
                 level: str, project_root: pathlib.Path, home: pathlib.Path) -> InstallPlan:
    """What `oah skills install` would do. Names may be bundled or linked; an unknown name is refused.

    Each bundled item is copied from the first source folder that holds it (the kit cache, then the
    bundled skills); with neither, it is fetched from its upstream at the pinned commit.
    """
    copies: list[Copy] = []
    linked: list[Linked] = []
    sources: dict[str, pathlib.Path | None] = {}
    seen: dict[pathlib.Path, str] = {}
    for name in names:
        entry = catalog.find(name)
        if entry is None:
            known = ", ".join(sorted(e.name for e in (*catalog.bundled, *catalog.linked)))
            raise InstallError("OAH_SKILLS_UNKNOWN", f"{name} is not in the kit's toolkit; "
                                                     f"run oah skills list (known: {known})")
        if isinstance(entry, Linked):
            linked.append(entry)
            continue
        sources[entry.name] = item_source(entry.name, source_dirs)
        source = sources[entry.name]
        source_commit = read_upstream(source).commit if source is not None else entry.commit
        for engine, folder in target_dirs(target, level, project_root=project_root, home=home):
            for relative in entry.skills:
                installed = skill_name(entry, relative)
                dest = folder / installed
                action, reason = _classify(dest, entry.name, project=level == "project", commit=source_commit)
                if dest in seen:
                    action, reason = "conflict", f"{seen[dest]} also installs a skill named {installed}"
                seen[dest] = entry.name
                copies.append(Copy(entry.name, relative, installed, engine, dest, action,
                                   not _inside(folder, project_root), reason))
    return InstallPlan(tuple(copies), tuple(linked), sources, pathlib.Path(home))


def fetch_item(entry: Bundled, scratch: pathlib.Path, *, today: dt.date) -> pathlib.Path:
    """Stage a bundled item from its upstream at the pinned commit (for installs without skills/)."""
    tree = fetch(entry.repository, entry.commit, scratch / "fetch",
                 paths=entry.fetch_paths())
    stage_item(entry, tree, scratch / "item", fetched=today)
    return scratch / "item"


def build_copy(item_dir: pathlib.Path, entry: Bundled, relative: str, record: Upstream, out: pathlib.Path,
               *, installed_at: str) -> pathlib.Path:
    """Write the folder an installed skill gets: the skill, the license files and the marker."""
    source = item_skill_dirs(item_dir, entry)[entry.skills.index(relative)]
    if not (source / SKILL_FILE).is_file():
        raise InstallError("OAH_SKILLS_BUNDLE_BROKEN", f"{source} has no {SKILL_FILE}; reinstall oah")
    copy_tree(source, out)
    stray = out / UPSTREAM_FILE
    if stray.exists():
        stray.unlink()
    for name in (record.license_file, *record.notice_files):
        target = out / name.rsplit("/", 1)[-1]
        if not target.exists():
            shutil.copyfile(item_dir.joinpath(*name.split("/")), target)
    for folder in entry.support.get(relative, ()):
        # Upstream's own copy install puts these runtime folders inside the skill; files stay unchanged.
        target = out / folder.rsplit("/", 1)[-1]
        if not target.exists():
            copy_tree(item_dir.joinpath(*folder.split("/")), target)
    executables = installed_executables(entry, relative, record)
    upstream_path = "/".join(p for p in (record.subpath, "" if relative == "." else relative) if p)
    write_marker(out, {"kit": KIT_MARK, "item": entry.name, "skill": relative, "repository": record.repository,
                       "subpath": upstream_path, "commit": record.commit, "spdx": record.spdx,
                       "license_file": record.license_file.rsplit("/", 1)[-1],
                       "license_sha256": record.license_sha256, "executables": executables,
                       "installed_at": installed_at})
    return out


def installed_executables(entry: Bundled, relative: str, record: Upstream) -> list[str]:
    """Map every copied skill, support and license executable to its installed path."""
    prefix = "" if relative == "." else relative + "/"
    found = {name[len(prefix):] for name in record.executables if name.startswith(prefix)}
    for folder in entry.support.get(relative, ()):
        found.update(folder.rsplit("/", 1)[-1] + "/" + name[len(folder) + 1:]
                     for name in record.executables if name.startswith(folder + "/"))
    found.update(name.rsplit("/", 1)[-1] for name in (record.license_file, *record.notice_files)
                 if name in record.executables)
    return sorted(found)


@contextlib.contextmanager
def publication_lock(home: pathlib.Path) -> Iterator[None]:
    """Serialize all of this user's skill publications, independent of level or destination aliases.

    Acquire any repository state lock first; never acquire another lock while this is held.
    Keep the lock file after release so every process opens the same file.
    """
    try:
        with StateLock(pathlib.Path(home) / ".oah" / "skills-publish.lock"):
            yield
    except LockTimeout as exc:
        raise InstallError("OAH_SKILLS_PUBLISH_BUSY",
                           "another skill install or update is publishing; wait for it to finish, "
                           "then retry the oah skills command") from exc


def _identity(path: pathlib.Path) -> tuple:
    """Bytes, modes and link/directory identity of a preparation input, including absence."""
    if not path.exists() and not path.is_symlink():
        return ("absent",)
    paths = [path, *sorted(path.rglob("*"))] if path.is_dir() and not path.is_symlink() else [path]
    found = []
    for child in paths:
        info = child.lstat()
        kind = "link" if child.is_symlink() else "directory" if child.is_dir() else "file"
        data = os.readlink(child).encode("utf-8") if kind == "link" else child.read_bytes() if kind == "file" else b""
        found.append((child.relative_to(path).as_posix(), kind, info.st_dev, info.st_ino,
                      info.st_mode, hashlib.sha256(data).hexdigest()))
    return tuple(found)


def swap_in(new: pathlib.Path, dest: pathlib.Path, *, verify: Callable[[pathlib.Path], list[str]] | None = None) -> None:
    """Replace dest with new; the old folder comes back if the move or the verification fails."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = dest.parent / f".{dest.name}.oah-old-{secrets.token_hex(4)}"
    had_old = dest.exists()
    if had_old:
        dest.rename(backup)
    try:
        shutil.move(str(new), str(dest))
        problems = verify(dest) if verify is not None else []
        if problems:
            raise InstallError("OAH_SKILLS_VERIFY_FAILED", "; ".join(problems))
    except BaseException:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        if had_old:
            backup.rename(dest)
        raise
    if had_old:
        shutil.rmtree(backup, ignore_errors=True)


def apply_install(plan: InstallPlan, *, catalog: Catalog, now: dt.datetime,
                  guard: Callable[[], ContextManager[None]] = contextlib.nullcontext) -> list[pathlib.Path]:
    """Copy every planned skill. Refuses the whole plan when any copy conflicts.

    guard holds the repository state lock for preparation snapshots and publication.
    All copies additionally take the home's publication lock, always after guard.
    """
    if plan.conflicts:
        raise InstallError("OAH_SKILLS_INSTALL_CONFLICT",
                           "; ".join(f"{c.dest}: {c.reason}" for c in plan.conflicts))
    done: list[pathlib.Path] = []
    stamp = now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    def user_lock() -> ContextManager[None]:
        return publication_lock(plan.publication_home if plan.publication_home is not None else pathlib.Path.home())

    with tempfile.TemporaryDirectory(prefix="oah-skills-") as scratch_text:
        scratch = pathlib.Path(scratch_text)
        items: dict[str, tuple[pathlib.Path, Upstream, tuple | None]] = {}
        for copy in plan.copies:
            if copy.action == "keep":
                continue
            entry = catalog.find(copy.item)
            assert isinstance(entry, Bundled)
            source = plan.sources.get(copy.item)
            with guard(), user_lock():
                identity = _identity(copy.dest)
                source_identity = (items[copy.item][2] if copy.item in items
                                   else _identity(source) if source is not None else None)
            if copy.item not in items:
                item_dir = plan.sources.get(copy.item) or fetch_item(entry, scratch / copy.item, today=now.date())
                items[copy.item] = (item_dir, read_upstream(item_dir), source_identity)
            item_dir, record, _ = items[copy.item]
            staged = scratch / "copies" / f"{copy.engine}-{copy.item}-{copy.name}"
            staged.parent.mkdir(parents=True, exist_ok=True)
            build_copy(item_dir, entry, copy.skill, record, staged, installed_at=stamp)
            with guard(), user_lock():
                if (_identity(copy.dest) != identity
                        or source is not None and _identity(source) != source_identity):
                    raise InstallError("OAH_SKILLS_INSTALL_CONFLICT",
                                       f"{copy.dest} or its source changed during preparation; run oah skills install again")
                action, reason = _classify(copy.dest, copy.item, project=not copy.outside_project, commit=record.commit)
                if action == "conflict":
                    raise InstallError("OAH_SKILLS_INSTALL_CONFLICT", reason)
                if action == "keep":
                    continue
                swap_in(staged, copy.dest, verify=lambda folder: load_problems(folder))
            done.append(copy.dest)
    return done


Interactive = Callable[[Sequence[str], float], int]


def run_interactive(argv: Sequence[str], timeout: float) -> int:
    """Run an installer with the user's terminal attached, so it can ask its own questions."""
    try:
        return subprocess.run(list(argv), timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return 124


def run_linked(entry: Linked, *, runner: Interactive = run_interactive, search_path: str | None = None,
               timeout: float = LINKED_TIMEOUT) -> list[str]:
    """Run a linked item's official install steps in order; manual steps are returned for the user."""
    manual: list[str] = []
    for step in entry.steps:
        if step.run is None:
            manual.append(step.manual or "")
            continue
        try:
            found = resolve_command(step.run[0], search_path)
        except UnsupportedBatchShim as exc:
            raise InstallError("OAH_SKILLS_TOOL_MISSING", str(exc)) from exc
        if found is None:
            raise InstallError("OAH_SKILLS_TOOL_MISSING",
                               f"{step.run[0]} is not installed; install it first (oah doctor shows how), "
                               f"then run oah skills install {entry.name} again")
        code = runner((*found.argv, *step.run[1:]), timeout)
        if code != 0:
            raise InstallError("OAH_SKILLS_LINKED_FAILED",
                               f"{step.describe()} exited with {code}; read its output above, fix it, and retry")
    return manual


@dataclass(frozen=True)
class Row:
    name: str
    status: str
    version: str
    source: str
    license: str
    location: str

    def to_json(self) -> dict:
        return dict(self.__dict__)


def _short(commit: str | None) -> str:
    return commit[:12] if commit and len(commit) == 40 else (commit or "-")


def installed_skills(folders: Iterable[tuple[str, pathlib.Path]]) -> list[tuple[str, pathlib.Path]]:
    """(engine, skill folder) for every folder with a SKILL.md in the given skills folders."""
    found = []
    for engine, folder in folders:
        if not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and (child / SKILL_FILE).is_file():
                found.append((engine, child))
    return found


def _item_row(entry: Bundled, folder: pathlib.Path, status: str) -> Row:
    try:
        return Row(entry.name, status, _short(read_upstream(folder).commit), entry.repository, entry.spdx, str(folder))
    except CatalogError as exc:
        return Row(entry.name, status, "-", entry.repository, entry.spdx, f"(unreadable: {exc})")


def list_rows(*, catalog: Catalog, bundle_dir: pathlib.Path | None, cache_dir: pathlib.Path | None,
              project_root: pathlib.Path, home: pathlib.Path) -> list[Row]:
    """Bundled, cached, linked and installed skills; reads only."""
    rows: list[Row] = []
    for entry in catalog.bundled:
        bundled_copy = item_source(entry.name, [bundle_dir])
        rows.append(_item_row(entry, bundled_copy, "bundled") if bundled_copy is not None else
                    Row(entry.name, "bundled", _short(entry.commit), entry.repository, entry.spdx,
                        "(fetched from upstream on install)"))
        cached = item_source(entry.name, [cache_dir])
        if cached is not None:
            rows.append(_item_row(entry, cached, "cached"))
    for entry in catalog.linked:
        rows.append(Row(entry.name, "linked", entry.version, entry.source, entry.license,
                        "install: " + " && ".join(step.describe() for step in entry.steps)))
    for level, label in (("project", "project"), ("user", "global")):
        for engine, folder in installed_skills(target_dirs("both", level, project_root=project_root, home=home)):
            try:
                info = read_skill(folder)
            except SkillError as exc:
                rows.append(Row(folder.name, f"installed ({engine} {label}, unreadable)", "-", "-", "-",
                                f"{folder}: {exc}"))
                continue
            origin = "oah" if (marker_of(folder) or {}).get("kit") == KIT_MARK else "other"
            rows.append(Row(info.name, f"installed ({engine} {label}, {origin})", _short(info.version),
                            info.source or "-", info.license or "-", str(folder)))
    return rows
