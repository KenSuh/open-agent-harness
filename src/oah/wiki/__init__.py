# 위키 패키지 공개 API — 설정·오류 타입을 내보내고, finish·훅이 부르는 선택형 자동화 maintain(root, on=...) 을 둔다.
from __future__ import annotations

import datetime
import os
import pathlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from oah.wiki.base import DEFAULTS, WikiError, WikiSettings

STEPS = ("log", "rollover", "lint", "hygiene")

__all__ = ["DEFAULTS", "STEPS", "MaintainResult", "WikiError", "WikiSettings", "maintain"]


@dataclass
class MaintainResult:
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def maintain(root: os.PathLike | str, *, on: Iterable[str], log_title: str | None = None, log_body: str = "",
             settings: WikiSettings | None = None, env: Mapping[str, str] | None = None,
             today: datetime.date | None = None, now: float | None = None) -> MaintainResult:
    """Opt-in wiki upkeep for `oah finish` and hooks. Runs only the named steps, in the order of STEPS.

    `log` adds a dated entry (needs log_title), `rollover` moves old entries only when log.md is over the
    trigger, `lint` and `hygiene` add one summary line each. A failing step is reported in `errors` and
    the remaining steps still run. Nothing is ever deleted, merged or moved besides whole log entries.
    """
    wanted = set(on)
    unknown = wanted - set(STEPS)
    if unknown:
        raise ValueError(f"unknown wiki maintenance steps: {', '.join(sorted(unknown))}; use {', '.join(STEPS)}")
    if "log" in wanted and not (log_title or "").strip():
        raise ValueError("the log step needs log_title")
    result = MaintainResult()
    if not wanted:
        return result
    from oah.atomic import StateCorrupt, StorageError
    from oah.lockfile import LockTimeout
    from oah.paths import GitMissing, NotARepo
    from oah.wiki.store import wiki_for

    failures = (WikiError, LockTimeout, StateCorrupt, StorageError, GitMissing, NotARepo)
    try:
        wiki = wiki_for(pathlib.Path(root), settings or WikiSettings(), os.environ if env is None else env)
    except failures as exc:
        result.errors.append(str(exc))
        return result
    for step in STEPS:
        if step not in wanted:
            continue
        try:
            if step == "log":
                entry = wiki.log(log_title, body=log_body, today=today)
                result.lines.append(f"OAH_WIKI_LOGGED {entry.date} {entry.title}")
            elif step == "rollover":
                result.lines.append(wiki.rollover(today=today).line)
            elif step == "lint":
                result.lines.append(wiki.lint(now=now).summary())
            else:
                result.lines.append(wiki.hygiene(now=now))
        except failures as exc:
            result.errors.append(str(exc))
    return result
