# 작성 엔진 판정 — base..head 커밋마다 Agent-Engine 꼬리말(없으면 Claude Code 의 Co-Authored-By: Claude)로 엔진을 정하고, 리뷰어와 비교해 independent 를 true·false·unknown 으로 붙인다.
"""Who wrote what (PLAN 5.9).

Each commit in base..head gets one engine: a single valid `Agent-Engine: claude|codex|human` trailer wins
(case-insensitive); without one, Claude Code's `Co-Authored-By: Claude …` trailer means `claude`;
anything else, including several or unknown values, is `unknown`. git parses the trailers, so text that is
not in the final trailer block does not count.

`independence(engines, reviewer)` is `False` when the reviewer engine wrote any commit, `"unknown"` when any
commit is unknown (or there are none), and `True` only when every commit has a known engine that differs from
the reviewer. That is an honest label for the chosen engine order, not an error.
"""
from __future__ import annotations

import pathlib
import re
from typing import Iterable, Sequence

from oah.paths import run_git
from oah.review import ReviewError

ENGINES = ("claude", "codex", "human")
UNKNOWN = "unknown"
_CLAUDE = re.compile(r"claude\b", re.IGNORECASE)
_FORMAT = ("--format=%H%x1f%(trailers:key=Agent-Engine,valueonly,separator=%x1e)%x1f"
           "%(trailers:key=Co-authored-by,valueonly,separator=%x1e)%x1d")


def classify(agent_values: Sequence[str], coauthors: Sequence[str]) -> str:
    values = {value.strip().lower() for value in agent_values if value.strip()}
    if values:
        engine = next(iter(values))
        return engine if len(values) == 1 and engine in ENGINES else UNKNOWN
    return "claude" if any(_CLAUDE.match(value.strip()) for value in coauthors) else UNKNOWN


def commit_engines(root: pathlib.Path, base: str, head: str) -> list[tuple[str, str]]:
    """(commit, engine) for every commit in base..head, newest first."""
    result = run_git(root, "log", "--no-show-signature", _FORMAT, f"{base}..{head}", "--")
    if result.returncode != 0:
        raise ReviewError("OAH_REVIEW_GIT_FAILED", f"git log could not list the commits in {base}..{head} "
                          f"({result.stderr.strip() or 'no output'}); check the repository and retry")
    found = []
    for record in result.stdout.split("\x1d"):
        record = record.strip("\r\n")
        if not record:
            continue
        sha, agents, coauthors = (record.split("\x1f") + ["", ""])[:3]
        found.append((sha.strip(), classify(agents.split("\x1e"), coauthors.split("\x1e"))))
    return found


def independence(engines: Iterable[str], reviewer: str) -> bool | str:
    engines = list(engines)
    if reviewer in engines:
        return False
    if not engines or UNKNOWN in engines:
        return UNKNOWN
    return True
