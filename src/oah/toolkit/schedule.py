# 운영체제 예약 항목 — 동의를 받은 뒤 macOS launchd 에이전트나 Windows 작업 스케줄러 작업으로 하루 한 번 예약 갱신을 돌리고, 깨끗이 지운다.
"""An optional daily OS entry that runs `oah skills update --scheduled` in one project.

The command itself decides whether a check is due and waits while leases are active, and it writes
only the kit cache in oah's state folder, so the entry only needs to start it once a day. macOS uses a launchd agent in ~/Library/LaunchAgents; Windows uses a
Task Scheduler task registered from XML, which keeps paths with spaces literal.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import pathlib
import plistlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

from oah.engines.proc import RunResult, UnsupportedBatchShim, resolve_command, run_command

LABEL_PREFIX = "io.github.open-agent-harness.skills-update"
TASK_PREFIX = "oah-skills-update"
DAY_SECONDS = 86400
TOOL_TIMEOUT = 60.0
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

Runner = Callable[..., RunResult]


class ScheduleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Entry:
    kind: str                     # "launchd" or "schtasks"
    name: str                     # launchd label or task name
    path: pathlib.Path | None     # the plist file (launchd only)
    argv: tuple[str, ...]
    project_root: pathlib.Path
    log_path: pathlib.Path

    def describe(self) -> list[str]:
        where = f"launchd agent {self.path}" if self.kind == "launchd" else f"Task Scheduler task {self.name}"
        return [f"{where}", f"  runs daily in {self.project_root}: {subprocess.list2cmdline(self.argv)}",
                f"  output goes to {self.log_path}"]


def entry_id(project_root: pathlib.Path) -> str:
    return hashlib.sha256(str(pathlib.Path(project_root).resolve()).encode("utf-8")).hexdigest()[:10]


def scheduled_argv(*, python: str = sys.executable) -> tuple[str, ...]:
    return (python, "-m", "oah", "skills", "update", "--scheduled")


def plan_entry(*, project_root: pathlib.Path, home: pathlib.Path, state_dir: pathlib.Path,
               platform: str = sys.platform, python: str = sys.executable) -> Entry:
    ident = entry_id(project_root)
    log = pathlib.Path(state_dir) / "toolkit" / "scheduled-update.log"
    argv = scheduled_argv(python=python)
    if platform == "darwin":
        label = f"{LABEL_PREFIX}.{ident}"
        return Entry("launchd", label, pathlib.Path(home) / "Library" / "LaunchAgents" / f"{label}.plist", argv,
                     pathlib.Path(project_root), log)
    if platform == "win32":
        return Entry("schtasks", f"{TASK_PREFIX}-{ident}", None, argv, pathlib.Path(project_root), log)
    raise ScheduleError("OAH_SCHEDULE_UNSUPPORTED", f"no OS scheduler support on {platform}; "
                                                    "run oah skills update yourself from time to time")


def launchd_plist(entry: Entry) -> bytes:
    return plistlib.dumps({
        "Label": entry.name, "ProgramArguments": list(entry.argv), "WorkingDirectory": str(entry.project_root),
        "StartInterval": DAY_SECONDS, "RunAtLoad": False, "ProcessType": "Background",
        "StandardOutPath": str(entry.log_path), "StandardErrorPath": str(entry.log_path)})


def task_xml(entry: Entry, *, start: dt.datetime | None = None) -> str:
    """Task Scheduler XML; Command, Arguments and WorkingDirectory are separate fields, so no shell parses them."""
    start = start or dt.datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    ET.register_namespace("", TASK_NS)

    def node(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
        child = ET.SubElement(parent, f"{{{TASK_NS}}}{tag}")
        if text is not None:
            child.text = text
        return child

    task = ET.Element(f"{{{TASK_NS}}}Task", {"version": "1.2"})
    node(node(task, "RegistrationInfo"), "Description", "open-agent-harness: daily skills update check")
    trigger = node(node(task, "Triggers"), "CalendarTrigger")
    node(trigger, "StartBoundary", start.isoformat(timespec="seconds"))
    node(node(trigger, "ScheduleByDay"), "DaysInterval", "1")
    principal = node(node(task, "Principals"), "Principal")
    principal.set("id", "Author")
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    for tag, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                       ("StartWhenAvailable", "true"), ("ExecutionTimeLimit", "PT1H")):
        node(settings, tag, value)
    actions = node(task, "Actions")
    actions.set("Context", "Author")
    action = node(actions, "Exec")
    node(action, "Command", entry.argv[0])
    node(action, "Arguments", subprocess.list2cmdline(entry.argv[1:]))
    node(action, "WorkingDirectory", str(entry.project_root))
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(task, encoding="unicode")


def _tool(name: str, search_path: str | None) -> tuple[str, ...]:
    try:
        found = resolve_command(name, search_path)
    except UnsupportedBatchShim as exc:
        raise ScheduleError("OAH_SCHEDULE_TOOL_MISSING", str(exc)) from exc
    if found is None:
        raise ScheduleError("OAH_SCHEDULE_TOOL_MISSING", f"{name} was not found; the OS scheduler is unavailable")
    return found.argv


def _ok(result: RunResult, what: str) -> None:
    if result.timed_out or result.returncode != 0:
        raise ScheduleError("OAH_SCHEDULE_FAILED", f"{what} failed: {result.stderr.strip()[:300] or result.stdout.strip()[:300]}")


def _domain() -> str:
    return f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui"


def install_entry(entry: Entry, *, runner: Runner = run_command, search_path: str | None = None) -> None:
    entry.log_path.parent.mkdir(parents=True, exist_ok=True)
    if entry.kind == "launchd":
        assert entry.path is not None
        entry.path.parent.mkdir(parents=True, exist_ok=True)
        entry.path.write_bytes(launchd_plist(entry))
        result = runner((*_tool("launchctl", search_path), "bootstrap", _domain(), str(entry.path)), timeout=TOOL_TIMEOUT)
        if result.timed_out or result.returncode != 0:
            entry.path.unlink(missing_ok=True)
            _ok(result, "launchctl bootstrap")
        return
    with tempfile.TemporaryDirectory(prefix="oah-task-") as scratch:
        xml_path = pathlib.Path(scratch) / "task.xml"
        xml_path.write_text(task_xml(entry), encoding="utf-16")
        _ok(runner((*_tool("schtasks", search_path), "/Create", "/TN", entry.name, "/XML", str(xml_path), "/F"),
                   timeout=TOOL_TIMEOUT), "schtasks /Create")


def remove_entry(entry: Entry, *, runner: Runner = run_command, search_path: str | None = None) -> bool:
    """Remove the entry; False when there was nothing to remove."""
    if entry.kind == "launchd":
        assert entry.path is not None
        if not entry.path.exists():
            return False
        runner((*_tool("launchctl", search_path), "bootout", _domain(), str(entry.path)), timeout=TOOL_TIMEOUT)
        entry.path.unlink()
        return True
    if not entry_installed(entry, runner=runner, search_path=search_path):
        return False
    _ok(runner((*_tool("schtasks", search_path), "/Delete", "/TN", entry.name, "/F"), timeout=TOOL_TIMEOUT),
        "schtasks /Delete")
    return True


def entry_installed(entry: Entry, *, runner: Runner = run_command, search_path: str | None = None) -> bool:
    if entry.kind == "launchd":
        return entry.path is not None and entry.path.exists()
    result = runner((*_tool("schtasks", search_path), "/Query", "/TN", entry.name), timeout=TOOL_TIMEOUT)
    return result.returncode == 0 and not result.timed_out
