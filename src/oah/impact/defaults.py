# 영향 지도 기본 설정 — 설정 파일 없이도 돌도록 모든 값을 여기 두고, 지도 내용에 영향을 주는 키만 도구 digest 에 넣는다.
from __future__ import annotations

import pathlib
from typing import Mapping

from oah.impact.kinds import DEFAULT_KINDS

DEFAULTS: dict[str, object] = {
    "history_limit": 500,         # commits read for co-change
    "cochange_commits": 20,       # the last N commits that touched a path
    "cochange_max_files": 50,     # larger commits (mass edits) are not co-change evidence
    "min_needle": 3,              # shorter file names and stems are not searched
    "max_ambiguity": 3,           # a file name or stem shared by more files than this is ignored
    "limit": 50,                  # link and mention rows shown; pin and exec rows are never capped
    "wiki_dirs": ["docs/wiki"],
    "graph_file": "graphify-out/graph.json",
    "db_globs": ["**/*.sql"],
    "git_timeout": 300.0,
    "lock_timeout": 120.0,
    "kinds": [[kind, list(globs)] for kind, globs in DEFAULT_KINDS],
}
# Settings that change what the map stores. The others only change how results are shown.
MAP_KEYS = ("history_limit", "min_needle", "kinds", "db_globs")


def effective(settings: Mapping[str, object] | None) -> dict[str, object]:
    merged = dict(DEFAULTS)
    merged.update(settings or {})
    _check(merged)
    return merged


def _check(settings: Mapping[str, object]) -> None:
    """Shapes that the config loader's type check cannot see: positive numbers and the kinds table."""
    from oah.impact.errors import ImpactError

    def fail(key: str, what: str) -> None:
        raise ImpactError("OAH_CONFIG_INVALID", f"[impact] {key} must be {what}; fix .oah/config.toml "
                          "or remove the key to use the default")

    for key in ("history_limit", "cochange_commits", "cochange_max_files", "min_needle", "max_ambiguity"):
        if not isinstance(settings[key], int) or isinstance(settings[key], bool) or settings[key] < 1:
            fail(key, "a positive integer")
    if not isinstance(settings["limit"], int) or isinstance(settings["limit"], bool) or settings["limit"] < 0:
        fail("limit", "zero or a positive integer")
    for key in ("git_timeout", "lock_timeout"):
        if not isinstance(settings[key], (int, float)) or isinstance(settings[key], bool) or settings[key] <= 0:
            fail(key, "a positive number of seconds")
    for key in ("wiki_dirs", "db_globs"):
        if not isinstance(settings[key], (list, tuple)) or not all(isinstance(v, str) for v in settings[key]):
            fail(key, "an array of strings")
    if not isinstance(settings["graph_file"], str):
        fail("graph_file", "a string")
    kinds = settings["kinds"]
    if not isinstance(kinds, (list, tuple)) or not all(
            isinstance(entry, (list, tuple)) and len(entry) == 2 and isinstance(entry[0], str)
            and isinstance(entry[1], (list, tuple)) and all(isinstance(g, str) for g in entry[1]) for entry in kinds):
        fail("kinds", 'an array of ["kind", ["glob", ...]] pairs')


def configured(root: pathlib.Path, settings: Mapping[str, object] | None = None) -> dict[str, object]:
    """Explicit settings, else the repository's [impact] section from .oah/config.toml, over DEFAULTS."""
    if settings is None:
        from oah.config import load_config

        settings = load_config(root).section("impact", DEFAULTS)
    return effective(settings)
