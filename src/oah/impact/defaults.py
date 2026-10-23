# 영향 지도 기본 설정 — 설정 파일 없이도 돌도록 모든 값을 여기 두고, 지도 내용에 영향을 주는 키만 도구 digest 에 넣는다.
from __future__ import annotations

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
    return merged
