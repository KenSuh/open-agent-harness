# 위키 패키지 공개 API — 설정·오류 타입을 내보내고, finish·훅이 부르는 선택형 자동화 maintain(root, on=...) 을 둔다.
from __future__ import annotations

import datetime
import os
import pathlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from oah.wiki.base import DEFAULTS, WikiError, WikiSettings
from oah.wiki.store import MaintenanceCheck

STEPS = ("log", "index", "rollover", "lint", "hygiene")
TASK_STEPS = ("log", "index", "rollover")

__all__ = ["DEFAULTS", "STEPS", "MaintainResult", "MaintenanceCheck", "WikiError", "WikiSettings", "maintain",
           "verify_maintenance"]


@dataclass
class MaintainResult:
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def maintain(root: os.PathLike | str, *, on: Iterable[str], task: str | None = None, log_title: str | None = None,
             log_body: str = "", settings: WikiSettings | None = None, env: Mapping[str, str] | None = None,
             today: datetime.date | None = None, now: float | None = None,
             lock_timeout: float = 10.0) -> MaintainResult:
    """Opt-in wiki upkeep for `oah finish` and hooks. Runs only the named steps, in the order of STEPS.

    `log` adds a dated entry (needs log_title), `rollover` moves old entries only when log.md is over the
    trigger, `lint` and `hygiene` add one summary line each. A failing step is reported in `errors` and
    the remaining steps still run. Nothing is ever deleted, merged or moved besides whole log entries.

    With `task`, the log, index and rollover steps become `oah wiki maintain --task`: idempotent by task
    id, done under one wiki lock, and recorded in <state>/wiki/maintenance.jsonl for verify_maintenance.
    `index` needs a task. log_title defaults to "Task <task>".
    """
    wanted = set(on)
    unknown = wanted - set(STEPS)
    if unknown:
        raise ValueError(f"unknown wiki maintenance steps: {', '.join(sorted(unknown))}; use {', '.join(STEPS)}")
    if task is not None:
        from oah.lease import TASK_RE

        if not TASK_RE.fullmatch(task):
            raise ValueError(f"invalid task id {task!r}")
    elif "index" in wanted:
        raise ValueError("the index step needs a task")
    elif "log" in wanted and not (log_title or "").strip():
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
        wiki = wiki_for(pathlib.Path(root), settings or WikiSettings(), os.environ if env is None else env,
                        lock_timeout=lock_timeout)
    except failures as exc:
        result.errors.append(str(exc))
        return result
    task_steps = tuple(step for step in TASK_STEPS if step in wanted) if task is not None else ()
    if task_steps:
        try:
            result.lines.extend(wiki.maintain_task(task, title=log_title, body=log_body, today=today,
                                                   steps=task_steps))
        except failures as exc:
            result.errors.append(str(exc))
    for step in STEPS:
        if step not in wanted or step in task_steps:
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


def verify_maintenance(root: os.PathLike | str, task: str, *, settings: WikiSettings | None = None,
                       env: Mapping[str, str] | None = None, now: float | None = None,
                       lock_timeout: float = 10.0) -> MaintenanceCheck:
    """Whether `oah wiki maintain --task <task>` is done and still current; `oah finish` refuses when not ok.

    Checks the task's log entry and index line and its latest maintenance record against the current
    wiki bytes, then runs read-only lint and hygiene (no baseline is stored). Never writes the wiki.
    """
    from oah.atomic import StateCorrupt, StorageError
    from oah.lease import TASK_RE
    from oah.paths import GitMissing, NotARepo
    from oah.wiki.store import wiki_for

    if not TASK_RE.fullmatch(task or ""):
        raise ValueError(f"invalid task id {task!r}")
    try:
        wiki = wiki_for(pathlib.Path(root), settings or WikiSettings(), os.environ if env is None else env,
                        lock_timeout=lock_timeout)
        return wiki.verify_task(task, now=now)
    except (WikiError, StateCorrupt, StorageError, GitMissing, NotARepo) as exc:
        return MaintenanceCheck(ok=False, problems=[str(exc)])
