# 스킬 위치 우선순위 — 프로젝트 사본, 키트 캐시, 사용자·플러그인 사본 순으로 같은 이름의 스킬을 찾는다(선택 단계가 재사용).
"""Where a skill is read from, in order of precedence:

1. the project's own copies (`.claude/skills/<name>`, `.agents/skills/<name>`), which are tracked pins;
2. the kit cache in oah's state folder, which the automatic check keeps current;
3. user copies (`~/.claude/skills`, `~/.agents/skills`) and plugin copies
   (`~/.claude/plugins/cache`, `~/.codex/plugins/cache`).

Each candidate is read with `read_skill`, so the selector always sees the installed version.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

from oah.toolkit.catalog import Catalog, item_skill_dirs, skill_name
from oah.toolkit.install import SKILL_FOLDERS
from oah.toolkit.skillmeta import SKILL_FILE, SkillError, SkillInfo, read_skill

PLUGIN_CACHES = ((".claude", "plugins", "cache"), (".codex", "plugins", "cache"))
PLUGIN_DEPTH = 6


@dataclass(frozen=True)
class Located:
    origin: str   # "project", "cache", "user" or "plugin"
    info: SkillInfo


def _plugin_copies(root: pathlib.Path, name: str) -> list[pathlib.Path]:
    found = []
    if not root.is_dir():
        return found
    for skill_file in sorted(root.rglob(SKILL_FILE)):
        parts = skill_file.relative_to(root).parts
        if len(parts) <= PLUGIN_DEPTH and len(parts) >= 3 and parts[-3] == "skills" and parts[-2] == name:
            found.append(skill_file.parent)
    return found


def candidates(name: str, *, catalog: Catalog, project_root: pathlib.Path, cache_dir: pathlib.Path | None,
               home: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """Every folder that may hold skill name, best first."""
    folders: list[tuple[str, pathlib.Path]] = []
    for engine in ("claude", "codex"):
        folders.append(("project", pathlib.Path(project_root).joinpath(*SKILL_FOLDERS[engine], name)))
    if cache_dir is not None:
        for entry in catalog.bundled:
            for relative, folder in zip(entry.skills, item_skill_dirs(cache_dir / entry.name, entry)):
                if skill_name(entry, relative) == name:
                    folders.append(("cache", folder))
    for engine in ("claude", "codex"):
        folders.append(("user", pathlib.Path(home).joinpath(*SKILL_FOLDERS[engine], name)))
    for parts in PLUGIN_CACHES:
        folders.extend(("plugin", folder) for folder in _plugin_copies(pathlib.Path(home).joinpath(*parts), name))
    return folders


def locate_skill(name: str, *, catalog: Catalog, project_root: pathlib.Path, cache_dir: pathlib.Path | None,
                 home: pathlib.Path) -> list[Located]:
    """The readable copies of skill name, in precedence order; the first one wins."""
    found = []
    for origin, folder in candidates(name, catalog=catalog, project_root=project_root, cache_dir=cache_dir,
                                     home=home):
        if not (folder / SKILL_FILE).is_file():
            continue
        try:
            found.append(Located(origin, read_skill(folder)))
        except SkillError:
            continue
    return found
