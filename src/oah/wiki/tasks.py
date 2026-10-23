# 작업 단위 위키 유지 표지 — 작업 ID 로 로그 항목(숨은 표지 줄)과 index.md 의 작업 줄을 찾아, 롤오버 뒤에도 중복 없이 한 번만 넣게 한다.
from __future__ import annotations

import re

from oah.lease import TASK_RE
from oah.wiki import log as log_module
from oah.wiki.base import EXIT_REFUSED, WikiError
from oah.wiki.markdown import FenceTracker, split_lines, strip_eol

INDEX_LABEL = "Tasks"


def check_task(task: str) -> str:
    if not TASK_RE.fullmatch(task or ""):
        raise WikiError("OAH_TASK_INVALID", f"{task!r}: use letters, digits, '.', '_' or '-' (up to 64, starting "
                        "with a letter or digit), the same task id as `oah start --task`", EXIT_REFUSED)
    return task


def marker(task: str) -> str:
    """The hidden line that ties a log entry to its task; Obsidian does not render HTML comments."""
    return f"<!-- oah-task: {task} -->"


def entry_has_marker(entry: log_module.Entry, task: str) -> bool:
    """Only a column-zero marker outside structural fences identifies maintenance."""
    wanted = marker(task)
    fences = FenceTracker()
    for line in split_lines(entry.data):
        text = line.decode("utf-8")
        if not fences.feed(text) and strip_eol(text).rstrip() == wanted:
            return True
    return False


def task_entries(task: str, log_data: bytes, archives: dict[str, bytes]) -> list[tuple[str, log_module.Entry]]:
    """(file name, entry) for every entry of log.md and the archives that carries the task's marker."""
    found = []
    for name, data in [("log.md", log_data), *sorted(archives.items())]:
        for entry in log_module.parse_log(data, name).entries:
            if entry_has_marker(entry, task):
                found.append((name, entry))
    return found


def index_line(task: str, title: str, limit: int) -> str:
    plain = re.sub(r"[\[\]|]", "", title).strip()
    prefix = f"- task {task}: "
    if limit < len(prefix) + 1:
        raise WikiError("OAH_CONFIG_INVALID", f"[wiki] index_line_limit={limit} cannot hold task {task!r}. "
                        f"Set index_line_limit to at least {len(prefix) + 1} in .oah/config.toml and retry.")
    room = limit - len(prefix)
    title = plain if len(plain) <= room else plain[:room - 1].rstrip() + "…"
    return prefix + title


def index_lines(index_data: bytes, task: str) -> int:
    """How many `- task <task>:` lines index.md has outside fenced code."""
    prefix = f"- task {task}:"
    fences = FenceTracker()
    count = 0
    for line in split_lines(index_data):
        text = line.decode("utf-8", errors="replace")
        if not fences.feed(text) and strip_eol(text).startswith(prefix):
            count += 1
    return count
