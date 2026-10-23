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

import datetime as dt
import pathlib
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from oah.engines.proc import UnsupportedBatchShim, resolve_command
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
    action: str         # "install", "replace" or "conflict"
    outside_project: bool
    reason: str = ""


@dataclass(frozen=True)
class InstallPlan:
    copies: tuple[Copy, ...]
    linked: tuple[Linked, ...]
    fetch_needed: tuple[str, ...]

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
        for name in self.fetch_needed:
            lines.append(f"fetch    {name} from its upstream at the pinned commit (no bundled copy here)")
        for entry in self.linked:
            for step in entry.steps:
                kind = "run" if step.run is not None else "manual"
                lines.append(f"{kind:8} {entry.name}: {step.describe()}")
        return lines


def marker_of(folder: pathlib.Path) -> dict | None:
    return read_json_file(pathlib.Path(folder) / MARKER_FILE)


def _classify(dest: pathlib.Path, item: str) -> tuple[str, str]:
    if not dest.exists():
        return "install", ""
    marker = marker_of(dest)
    if marker is None or marker.get("kit") != KIT_MARK:
        return "conflict", "a folder oah did not install is already there; rename or remove it first"
    if marker.get("item") != item:
        return "conflict", f"oah installed it from {marker.get('item')}; uninstall that first"
    return "replace", ""


def plan_install(names: Sequence[str], *, catalog: Catalog, bundle_dir: pathlib.Path | None, target: str,
                 level: str, project_root: pathlib.Path, home: pathlib.Path) -> InstallPlan:
    """What `oah skills install` would do. Names may be bundled or linked; an unknown name is refused."""
    copies: list[Copy] = []
    linked: list[Linked] = []
    fetch_needed: list[str] = []
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
        if bundle_dir is None or not (bundle_dir / entry.name / UPSTREAM_FILE).is_file():
            fetch_needed.append(entry.name)
        for engine, folder in target_dirs(target, level, project_root=project_root, home=home):
            for relative in entry.skills:
                installed = skill_name(entry, relative)
                dest = folder / installed
                action, reason = _classify(dest, entry.name)
                if dest in seen:
                    action, reason = "conflict", f"{seen[dest]} also installs a skill named {installed}"
                seen[dest] = entry.name
                copies.append(Copy(entry.name, relative, installed, engine, dest, action,
                                   not _inside(folder, project_root), reason))
    return InstallPlan(tuple(copies), tuple(linked), tuple(fetch_needed))


def fetch_item(entry: Bundled, scratch: pathlib.Path, *, today: dt.date) -> pathlib.Path:
    """Stage a bundled item from its upstream at the pinned commit (for installs without skills/)."""
    tree = fetch(entry.repository, entry.commit, scratch / "fetch",
                 paths=tuple(p for p in (entry.subpath, entry.license_path, *entry.notice_paths) if p))
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
    prefix = "" if relative == "." else relative + "/"
    executables = sorted(e[len(prefix):] for e in record.executables if e.startswith(prefix))
    upstream_path = "/".join(p for p in (record.subpath, "" if relative == "." else relative) if p)
    write_marker(out, {"kit": KIT_MARK, "item": entry.name, "skill": relative, "repository": record.repository,
                       "subpath": upstream_path, "commit": record.commit, "spdx": record.spdx,
                       "license_file": record.license_file.rsplit("/", 1)[-1],
                       "license_sha256": record.license_sha256, "executables": executables,
                       "installed_at": installed_at})
    return out


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


def apply_install(plan: InstallPlan, *, catalog: Catalog, bundle_dir: pathlib.Path | None,
                  now: dt.datetime) -> list[pathlib.Path]:
    """Copy every planned skill. Refuses the whole plan when any copy conflicts."""
    if plan.conflicts:
        raise InstallError("OAH_SKILLS_INSTALL_CONFLICT",
                           "; ".join(f"{c.dest}: {c.reason}" for c in plan.conflicts))
    done: list[pathlib.Path] = []
    stamp = now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory(prefix="oah-skills-") as scratch_text:
        scratch = pathlib.Path(scratch_text)
        items: dict[str, tuple[pathlib.Path, Upstream]] = {}
        for copy in plan.copies:
            entry = catalog.find(copy.item)
            assert isinstance(entry, Bundled)
            if copy.item not in items:
                item_dir = (fetch_item(entry, scratch / copy.item, today=now.date()) if copy.item in plan.fetch_needed
                            else bundle_dir / copy.item)
                items[copy.item] = (item_dir, read_upstream(item_dir))
            item_dir, record = items[copy.item]
            staged = scratch / "copies" / f"{copy.engine}-{copy.item}-{copy.name}"
            staged.parent.mkdir(parents=True, exist_ok=True)
            build_copy(item_dir, entry, copy.skill, record, staged, installed_at=stamp)
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


def list_rows(*, catalog: Catalog, bundle_dir: pathlib.Path | None, project_root: pathlib.Path,
              home: pathlib.Path) -> list[Row]:
    rows: list[Row] = []
    for entry in catalog.bundled:
        location, commit = "(fetched from upstream on install)", entry.commit
        if bundle_dir is not None and (bundle_dir / entry.name / UPSTREAM_FILE).is_file():
            try:
                commit = read_upstream(bundle_dir / entry.name).commit
                location = str(bundle_dir / entry.name)
            except CatalogError as exc:
                location = f"(bundle unreadable: {exc})"
        rows.append(Row(entry.name, "bundled", _short(commit), entry.repository, entry.spdx, location))
    for entry in catalog.linked:
        rows.append(Row(entry.name, "linked", entry.version, entry.source, entry.license,
                        "install: " + " && ".join(step.describe() for step in entry.steps)))
    folders = (target_dirs("both", "project", project_root=project_root, home=home)
               + target_dirs("both", "user", project_root=project_root, home=home))
    for engine, folder in installed_skills(folders):
        try:
            info = read_skill(folder)
        except SkillError as exc:
            rows.append(Row(folder.name, f"installed ({engine}, unreadable)", "-", "-", "-", f"{folder}: {exc}"))
            continue
        origin = "oah" if (marker_of(folder) or {}).get("kit") == KIT_MARK else "other"
        rows.append(Row(info.name, f"installed ({engine}, {origin})", _short(info.version), info.source or "-",
                        info.license or "-", str(folder)))
    return rows

