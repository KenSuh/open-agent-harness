# 운영체제 예약 항목 — 동의를 받은 뒤 macOS launchd 에이전트나 Windows 작업 스케줄러 작업으로 하루 한 번 예약 갱신을 돌리고, 깨끗이 지운다.
"""An optional daily OS entry that runs one oah command in one project: `oah skills update --scheduled`
(purpose "skills") or `oah sources check --scheduled` (purpose "sources").

The command itself decides whether a check is due (the skills update also waits while leases are active), and it
writes only oah's state folder, so the entry only needs to start it once a day. macOS uses a launchd agent in
~/Library/LaunchAgents; Windows uses a Task Scheduler task registered from XML, which keeps paths with spaces literal.

`inspect_entry` compares a canonical DefinitionV1 of what this oah would register with the definition the OS scheduler
reports for the loaded job (launchd `print` plus `print-disabled`, or the task XML): program, arguments, folder,
enablement, every trigger and action, outputs and run constraints. A plist file or a task name alone proves nothing,
and an unreadable required fact is `unverifiable`. Whether the job has run is separate evidence
(`os_run_observation`), and neither is proof that the OS timer fired.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import pathlib
import plistlib
import re
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
SKILLS_DESCRIPTION = "open-agent-harness: daily skills update check"
SKILLS_REMOVE_HINT = "oah skills schedule remove"

Runner = Callable[..., RunResult]


@dataclass(frozen=True)
class Purpose:
    """What one kind of OS entry runs, and how messages name it."""

    label_prefix: str
    task_prefix: str
    argv_tail: tuple[str, ...]
    log: tuple[str, ...]          # path parts under the state folder
    description: str
    remove_hint: str
    manual_hint: str
    explicit_enabled: bool = False   # write Trigger/Settings Enabled; the skills bytes stay as they were


PURPOSES = {
    "skills": Purpose(LABEL_PREFIX, TASK_PREFIX, ("skills", "update", "--scheduled"), ("toolkit", "scheduled-update.log"),
                      SKILLS_DESCRIPTION, SKILLS_REMOVE_HINT, "run oah skills update yourself from time to time"),
    "sources": Purpose("io.github.open-agent-harness.sources-check", "oah-sources-check",
                       ("sources", "check", "--scheduled"), ("sources", "scheduled-check.log"),
                       "open-agent-harness: daily design source check", "oah sources schedule remove",
                       "run oah sources check yourself from time to time", explicit_enabled=True),
}


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
    description: str = SKILLS_DESCRIPTION
    remove_hint: str = SKILLS_REMOVE_HINT
    purpose: str = "skills"

    def describe(self) -> list[str]:
        where = f"launchd agent {self.path}" if self.kind == "launchd" else f"Task Scheduler task {self.name}"
        return [f"{where}", f"  runs daily in {self.project_root}: {subprocess.list2cmdline(self.argv)}",
                f"  output goes to {self.log_path}"]


def _purpose(name: str) -> Purpose:
    try:
        return PURPOSES[name]
    except KeyError:
        raise ValueError(f"unknown schedule purpose {name!r}") from None


def entry_id(project_root: pathlib.Path) -> str:
    return hashlib.sha256(str(pathlib.Path(project_root).resolve()).encode("utf-8")).hexdigest()[:10]


def scheduled_argv(*, python: str = sys.executable, purpose: str = "skills") -> tuple[str, ...]:
    return (python, "-m", "oah", *_purpose(purpose).argv_tail)


def plan_entry(*, project_root: pathlib.Path, home: pathlib.Path, state_dir: pathlib.Path,
               platform: str = sys.platform, python: str = sys.executable, purpose: str = "skills") -> Entry:
    spec = _purpose(purpose)
    ident = entry_id(project_root)
    log = pathlib.Path(state_dir).joinpath(*spec.log)
    argv = scheduled_argv(python=python, purpose=purpose)
    if platform == "darwin":
        label = f"{spec.label_prefix}.{ident}"
        return Entry("launchd", label, pathlib.Path(home) / "Library" / "LaunchAgents" / f"{label}.plist", argv,
                     pathlib.Path(project_root), log, spec.description, spec.remove_hint, purpose)
    if platform == "win32":
        return Entry("schtasks", f"{spec.task_prefix}-{ident}", None, argv, pathlib.Path(project_root), log,
                     spec.description, spec.remove_hint, purpose)
    raise ScheduleError("OAH_SCHEDULE_UNSUPPORTED", f"no OS scheduler support on {platform}; {spec.manual_hint}")


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
    node(node(task, "RegistrationInfo"), "Description", entry.description)
    explicit = _purpose(entry.purpose).explicit_enabled
    trigger = node(node(task, "Triggers"), "CalendarTrigger")
    node(trigger, "StartBoundary", start.isoformat(timespec="seconds"))
    if explicit:
        node(trigger, "Enabled", "true")
    node(node(trigger, "ScheduleByDay"), "DaysInterval", "1")
    principal = node(node(task, "Principals"), "Principal")
    principal.set("id", "Author")
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    for tag, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                       ("StartWhenAvailable", "true"), ("ExecutionTimeLimit", "PT1H")):
        node(settings, tag, value)
    if explicit:
        node(settings, "Enabled", "true")
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
        raise ScheduleError("OAH_SCHEDULE_FAILED", f"{what} failed: {result.stderr.strip()[:300] or result.stdout.strip()[:300]}; "
                                                  "check the OS scheduler permissions and retry")


def _domain() -> str:
    return f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui"


def install_entry(entry: Entry, *, runner: Runner = run_command, search_path: str | None = None) -> None:
    entry.log_path.parent.mkdir(parents=True, exist_ok=True)
    if entry.kind == "launchd":
        assert entry.path is not None
        desired = launchd_plist(entry)
        if entry.path.exists():
            if entry.path.read_bytes() == desired:
                return
            raise ScheduleError("OAH_SCHEDULE_EXISTS", "a different scheduler plist already exists; "
                                                       f"run {entry.remove_hint} before installing again")
        entry.path.parent.mkdir(parents=True, exist_ok=True)
        argv = _tool("launchctl", search_path)
        entry.path.write_bytes(desired)
        try:
            _ok(runner((*argv, "bootstrap", _domain(), str(entry.path)), timeout=TOOL_TIMEOUT), "launchctl bootstrap")
        except BaseException:
            entry.path.unlink(missing_ok=True)
            raise
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
        _ok(runner((*_tool("launchctl", search_path), "bootout", _domain(), str(entry.path)), timeout=TOOL_TIMEOUT),
            "launchctl bootout")
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


# What the OS scheduler holds --------------------------------------------------------------------------------------

REGISTRATION_STATES = ("scheduled", "scheduled-different", "disabled", "unverifiable", "file-only", "absent",
                       "unsupported", "tool-missing")
OS_WAKEUP_SECONDS = DAY_SECONDS
PRINT_LIMIT = 256 * 1024
_IGNORED_ENVIRONMENT = {"XPC_SERVICE_NAME", "OSLogRateLimit"}
_TRIGGER_MARKERS = (("event triggers", "event"), ("sockets", "socket"), ("calendar", "calendar"),
                    ("keepalive", "keep-alive"), ("keep alive", "keep-alive"), ("watch paths", "watch-path"),
                    ("queue directories", "queue-directory"), ("start on mount", "mount"))


def _digest(data: object) -> str:
    import json

    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _observation(tool: str, text: str, at: str) -> dict:
    return {"tool": tool, "observed_at": at, "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def desired_definition(entry: Entry, *, anchor: str | None = None) -> dict:
    """DefinitionV1 of what this oah registers for `entry`; `anchor` is the installed task's StartBoundary."""
    base = {"version": 1, "purpose": entry.purpose, "scheduler": entry.kind, "name": entry.name,
            "program": entry.argv[0], "working_directory": str(entry.project_root), "enabled": True,
            "environment": {}}
    if entry.kind == "launchd":
        return {**base, "domain": _domain(), "arguments": list(entry.argv),
                "triggers": [{"type": "interval", "seconds": DAY_SECONDS}],
                "outputs": {"stdout": str(entry.log_path), "stderr": str(entry.log_path)},
                "constraints": {"process_type": "background"}}
    return {**base, "arguments": subprocess.list2cmdline(entry.argv[1:]),
            "context": {"action_context": "Author", "logon_type": "InteractiveToken", "run_level": "LeastPrivilege",
                        "user_id": _current_user_sid()},
            "triggers": [{"type": "calendar-daily", "days_interval": 1, "start_boundary": anchor, "enabled": True,
                          "end_boundary": None, "repetition": False, "execution_time_limit": "PT72H",
                          "random_delay": "PT0S"}],
            "outputs": None,
            "constraints": {"multiple_instances": "IgnoreNew", "disallow_start_if_on_batteries": False,
                            "start_when_available": True, "execution_time_limit": "PT1H",
                            "run_only_if_idle": False, "run_only_if_network_available": False,
                            "wake_to_run": False}}


def _current_user_sid() -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token, needed = wintypes.HANDLE(), wintypes.DWORD()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        return None
    try:
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value or needed.value > 65536:
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


@dataclass(frozen=True)
class Inspection:
    """What the OS scheduler holds for one entry, compared field by field with what this oah would register."""

    state: str                             # one of REGISTRATION_STATES
    reasons: tuple[str, ...] = ()          # mismatch reasons (scheduled-different) or why it is not verifiable
    desired: dict | None = None
    effective: dict | None = None
    missing: tuple[str, ...] = ()          # required effective facts the tool did not expose
    observations: tuple[dict, ...] = ()
    os_run: dict | None = None             # the scheduler's own run metadata, when it reports any

    def to_json(self) -> dict:
        return {"state": self.state, "reasons": list(self.reasons), "desired": self.desired,
                "desired_digest": None if self.desired is None else _digest(self.desired),
                "effective": self.effective,
                "effective_digest": None if self.effective is None else _digest(self.effective),
                "missing_facts": list(self.missing), "observations": list(self.observations), "os_run": self.os_run}


def _compare(desired: dict, effective: dict) -> list[str]:
    reasons = []
    for key in sorted(set(desired) | set(effective)):
        if key in ("version", "purpose", "scheduler", "name"):
            continue
        if desired.get(key) != effective.get(key):
            reasons.append(f"{key.replace('_', '-')}-differs")
    return reasons


_PRINT_VALUE = re.compile(r"^\s*([a-z][a-z0-9 ]*[a-z0-9])\s*=\s*(.*?)\s*$")


def parse_launchd_print(text: str, domain: str, label: str) -> tuple[dict, list[str], list[str]]:
    """(fields, missing facts, extra triggers) from bounded `launchctl print` text; blocks are kept as lists."""
    lines = text.splitlines()
    if len(text) > PRINT_LIMIT or not lines or lines[0].strip() != f"{domain}/{label} = {{" \
            or lines[-1].strip() != "}":
        return {}, ["print-format"], []
    found: dict = {}
    extra: list[str] = []
    depth_block: str | None = None
    block: list[str] = []
    for line in lines[1:-1]:
        stripped = line.strip()
        if depth_block is not None:
            if stripped == "}":
                found.setdefault(depth_block, block)
                depth_block, block = None, []
            else:
                block.append(stripped)
            continue
        match = re.match(r"^([a-z][a-z0-9 ]*[a-z0-9]) = \{$", stripped)
        if match:
            depth_block, block = match.group(1), []
            continue
        match = _PRINT_VALUE.match(line)
        if match and match.group(1) not in found:
            found[match.group(1)] = match.group(2)
    if depth_block is not None:
        return {}, ["print-format"], []
    for key in found:
        for marker, kind in _TRIGGER_MARKERS:
            if key.startswith(marker) and found[key] not in ([], "", "0"):
                extra.append(kind)
    properties = str(found.get("properties", "")).lower()
    for word, kind in (("runatload", "run-at-load"), ("keepalive", "keep-alive")):
        if word in properties:
            extra.append(kind)
    missing = [fact for fact, key in (("arguments", "arguments"), ("working-directory", "working directory"),
                                      ("run-interval", "run interval"), ("spawn-type", "spawn type"))
               if key not in found]
    return found, missing, sorted(set(extra))


def _launchd_effective(fields: dict, entry: Entry, extra: list[str], enabled: bool) -> dict:
    arguments = list(fields.get("arguments") or [])
    interval = re.fullmatch(r"(\d+) seconds?", str(fields.get("run interval", "")))
    environment = {}
    for line in fields.get("environment") or []:
        name, _, value = line.partition(" => ")
        if name not in _IGNORED_ENVIRONMENT:
            environment[name] = value
    triggers = [{"type": "interval", "seconds": int(interval.group(1))}] if interval else []
    triggers += [{"type": kind} for kind in extra]
    spawn = str(fields.get("spawn type", "")).split(" ", 1)[0].lower()
    return {"version": 1, "purpose": entry.purpose, "scheduler": "launchd", "name": entry.name,
            "domain": _domain(), "program": fields.get("program") or (arguments[0] if arguments else None),
            "arguments": arguments, "working_directory": fields.get("working directory"), "enabled": enabled,
            "environment": environment, "triggers": triggers,
            "outputs": {"stdout": fields.get("stdout path"), "stderr": fields.get("stderr path")},
            "constraints": {"process_type": spawn or None}}


def parse_disabled(text: str, label: str) -> bool | None:
    """True when `print-disabled` marks the label disabled, False when enabled or not listed; None if unreadable."""
    if len(text) > PRINT_LIMIT or "{" not in text:
        return None
    match = re.search(r'^\s*"' + re.escape(label) + r'"\s*=>\s*(\S+)\s*$', text, re.MULTILINE)
    if match is None:
        return False
    word = match.group(1).lower()
    if word in ("disabled", "true"):
        return True
    if word in ("enabled", "false"):
        return False
    return None


def _strip_declaration(text: str) -> str:
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿"), count=1)


_SETTINGS = {"MultipleInstancesPolicy": ("multiple_instances", str, "IgnoreNew"),
             "DisallowStartIfOnBatteries": ("disallow_start_if_on_batteries", bool, True),
             "StartWhenAvailable": ("start_when_available", bool, False),
             "ExecutionTimeLimit": ("execution_time_limit", str, "PT72H"),
             "RunOnlyIfIdle": ("run_only_if_idle", bool, False),
             "RunOnlyIfNetworkAvailable": ("run_only_if_network_available", bool, False),
             "WakeToRun": ("wake_to_run", bool, False)}
_BENIGN_SETTINGS = {"Enabled", "StopIfGoingOnBatteries", "AllowHardTerminate", "IdleSettings", "AllowStartOnDemand",
                    "Hidden", "Priority", "UseUnifiedSchedulingEngine", "DisallowStartOnRemoteAppSession",
                    "Compatibility"}


def _flag(text: str | None, default: bool) -> bool | None:
    if text is None:
        return default
    value = text.strip().lower()
    return True if value == "true" else False if value == "false" else None


def parse_task_xml(text: str, entry: Entry, now: dt.datetime) -> tuple[dict | None, list[str], list[str]]:
    """(effective definition, missing or unrecognized facts, mismatch-only facts) from exported task XML."""
    try:
        task = ET.fromstring(_strip_declaration(text))
    except ET.ParseError:
        return None, ["task-xml"], []
    ns = "{" + TASK_NS + "}"
    unknown: list[str] = []
    different: list[str] = []
    if task.tag != f"{ns}Task":
        return None, ["task-xml"], []
    for child in task:
        if child.tag not in {f"{ns}{tag}" for tag in ("RegistrationInfo", "Triggers", "Principals", "Settings",
                                                     "Actions", "Data")}:
            unknown.append(f"task-field:{child.tag.replace(ns, '')}")
    actions = task.find(f"{ns}Actions")
    execs = [] if actions is None else list(actions)
    if not execs or any(item.tag != f"{ns}Exec" for item in execs):
        (different if execs else unknown).append("actions-not-one-exec" if execs else "exec-action")
    if len(execs) > 1:
        different.append("actions-not-one-exec")
    exec_node = execs[0] if execs and execs[0].tag == f"{ns}Exec" else None
    values = {}
    if exec_node is not None:
        for item in exec_node:
            tag = item.tag.replace(ns, "")
            if tag not in ("Command", "Arguments", "WorkingDirectory"):
                unknown.append(f"exec-field:{tag}")
            values[tag] = item.text or ""
    triggers = task.find(f"{ns}Triggers")
    trigger_nodes = [] if triggers is None else list(triggers)
    effective_triggers = []
    for node in trigger_nodes:
        tag = node.tag.replace(ns, "")
        if tag != "CalendarTrigger":
            effective_triggers.append({"type": tag})
            continue
        fields = {item.tag.replace(ns, ""): item for item in node}
        if len(fields) != len(node):
            unknown.append("duplicate-trigger-field")
        for name in fields:
            if name not in ("StartBoundary", "EndBoundary", "Enabled", "ScheduleByDay", "Repetition",
                            "ExecutionTimeLimit", "RandomDelay"):
                unknown.append(f"trigger-field:{name}")
        start = (fields["StartBoundary"].text or "").strip() if "StartBoundary" in fields else None
        days = None
        by_day = fields.get("ScheduleByDay")
        if by_day is not None:
            days_text = by_day.findtext(f"{ns}DaysInterval")
            days = int(days_text) if days_text and days_text.strip().isdigit() else None
        end = (fields["EndBoundary"].text or "").strip() if "EndBoundary" in fields else None
        if end:
            try:
                expired = dt.datetime.fromisoformat(end.replace("Z", "+00:00")).replace(tzinfo=None) < \
                    now.replace(tzinfo=None)
            except ValueError:
                expired = True
            end = f"expired:{end}" if expired else end
        effective_triggers.append({"type": "calendar-daily" if by_day is not None else "calendar-other",
                                   "days_interval": days, "start_boundary": start,
                                   "enabled": _flag(fields["Enabled"].text if "Enabled" in fields else None, True),
                                   "end_boundary": end or None, "repetition": "Repetition" in fields,
                                   "execution_time_limit": (fields["ExecutionTimeLimit"].text or "").strip()
                                   if "ExecutionTimeLimit" in fields else "PT72H",
                                   "random_delay": (fields["RandomDelay"].text or "").strip()
                                   if "RandomDelay" in fields else "PT0S"})
    principals = task.find(f"{ns}Principals")
    principal_nodes = [] if principals is None else list(principals)
    context = {"action_context": None if actions is None else actions.get("Context"), "logon_type": None,
               "run_level": None, "user_id": None}
    if len(principal_nodes) != 1:
        unknown.append("principal")
    else:
        principal = principal_nodes[0]
        context["logon_type"] = principal.findtext(f"{ns}LogonType")
        context["run_level"] = principal.findtext(f"{ns}RunLevel") or "LeastPrivilege"
        context["user_id"] = principal.findtext(f"{ns}UserId")
        if not context["user_id"]:
            unknown.append("principal-user-id")
        for item in principal:
            if item.tag not in {f"{ns}{name}" for name in ("UserId", "GroupId", "LogonType", "RunLevel")}:
                unknown.append(f"principal-field:{item.tag.replace(ns, '')}")
        if principal.find(f"{ns}GroupId") is not None:
            different.append("principal-group")
        if principal.get("id") != context["action_context"]:
            different.append("action-context")
    settings = task.find(f"{ns}Settings")
    constraints = {}
    enabled = True
    for name, (key, kind, default) in _SETTINGS.items():
        text_value = None if settings is None else settings.findtext(f"{ns}{name}")
        constraints[key] = _flag(text_value, default) if kind is bool else (text_value or default)
    if settings is not None:
        enabled_flag = _flag(settings.findtext(f"{ns}Enabled"), True)
        enabled = bool(enabled_flag) if enabled_flag is not None else False
        if enabled_flag is None:
            unknown.append("settings-enabled")
        for item in settings:
            name = item.tag.replace(ns, "")
            if name not in _SETTINGS and name not in _BENIGN_SETTINGS:
                unknown.append(f"settings-field:{name}")
    if "Command" not in values:
        unknown.append("exec-command")
    effective = {"version": 1, "purpose": entry.purpose, "scheduler": "schtasks", "name": entry.name,
                 "program": values.get("Command"), "working_directory": values.get("WorkingDirectory") or None,
                 "enabled": enabled, "environment": {}, "arguments": values.get("Arguments", ""),
                 "context": context, "triggers": effective_triggers, "outputs": None, "constraints": constraints}
    return effective, sorted(set(unknown)), sorted(set(different))


def _anchor(effective: dict | None) -> str | None:
    """The installed daily StartBoundary when it is a valid anchor (the desired value is never a fresh now)."""
    for trigger in (effective or {}).get("triggers") or []:
        start = trigger.get("start_boundary")
        if trigger.get("type") == "calendar-daily" and isinstance(start, str):
            try:
                dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            except ValueError:
                return None
            return start
    return None


def inspect_entry(entry: Entry, *, runner: Runner = run_command, search_path: str | None = None,
                  now: Callable[[], dt.datetime] | None = None) -> Inspection:
    """Compare the loaded OS definition with DefinitionV1. Raises ScheduleError(OAH_SCHEDULE_TOOL_MISSING)."""
    clock = now or (lambda: dt.datetime.now(dt.timezone.utc))
    if entry.kind == "launchd":
        assert entry.path is not None
        desired = desired_definition(entry)
        argv = _tool("launchctl", search_path)
        file_state = "absent"
        if entry.path.exists():
            try:
                file_state = "match" if entry.path.read_bytes() == launchd_plist(entry) else "differs"
            except OSError:
                file_state = "unreadable"
        result = runner((*argv, "print", f"{_domain()}/{entry.name}"), timeout=TOOL_TIMEOUT)
        at = clock().isoformat(timespec="seconds")
        if result.timed_out:
            return Inspection("unverifiable", ("launchctl print did not answer in time",), desired,
                              missing=("print-timeout",))
        if result.returncode != 0:
            if file_state == "absent":
                return Inspection("absent", (), desired)
            return Inspection("file-only", (f"plist-{file_state}", "not-loaded"), desired)
        observations = [_observation("launchctl print", result.stdout, at)]
        fields, missing, extra = parse_launchd_print(result.stdout, _domain(), entry.name)
        runs = None
        if "runs" in fields or "last exit code" in fields:
            runs = {"status": "observed", "runs": fields.get("runs"), "last_exit": fields.get("last exit code"),
                    "observation": observations[0]}
        disabled = runner((*argv, "print-disabled", _domain()), timeout=TOOL_TIMEOUT)
        override = None
        if not disabled.timed_out and disabled.returncode == 0:
            observations.append(_observation("launchctl print-disabled", disabled.stdout, at))
            override = parse_disabled(disabled.stdout, entry.name)
        if override is None:
            missing.append("enablement")
        if missing:
            return Inspection("unverifiable", tuple(f"missing:{item}" for item in missing), desired, None,
                              tuple(missing), tuple(observations), runs)
        effective = _launchd_effective(fields, entry, extra, not override)
        if override:
            return Inspection("disabled", ("disabled-override",), desired, effective, (), tuple(observations), runs)
        reasons = _compare(desired, effective)
        if file_state != "match":
            reasons.append(f"plist-{file_state}")
        return Inspection("scheduled-different" if reasons else "scheduled", tuple(reasons), desired, effective, (),
                          tuple(observations), runs)
    argv = _tool("schtasks", search_path)
    result = runner((*argv, "/Query", "/TN", entry.name, "/XML"), timeout=TOOL_TIMEOUT)
    at = clock().isoformat(timespec="seconds")
    if result.timed_out:
        return Inspection("unverifiable", ("schtasks /Query did not answer in time",), desired_definition(entry),
                          missing=("query-timeout",))
    if result.returncode != 0:
        return Inspection("unverifiable", ("schtasks /Query failed",), desired_definition(entry),
                          missing=("query-failed",))
    observations = (_observation("schtasks /Query /XML", result.stdout, at),)
    effective, unknown, different = parse_task_xml(result.stdout, entry, clock())
    anchor = _anchor(effective)
    desired = desired_definition(entry, anchor=anchor)
    if desired["context"]["user_id"] is None:
        unknown.append("current-user-identity")
    if effective is None or unknown or anchor is None:
        missing = tuple(unknown or (["start-boundary"] if effective is not None else ["task-xml"]))
        return Inspection("unverifiable", tuple(f"missing:{item}" for item in missing), desired, effective, missing,
                          observations)
    if not effective["enabled"] or not all(item.get("enabled", True) for item in effective["triggers"]):
        return Inspection("disabled", ("disabled",), desired, effective, (), observations)
    reasons = sorted(set(_compare(desired, effective)) | set(different))
    return Inspection("scheduled-different" if reasons else "scheduled", tuple(reasons), desired, effective, (),
                      observations)


TASK_INFO_SCRIPT = ("$ErrorActionPreference='Stop';"
                    "$i=Get-ScheduledTaskInfo -TaskName $env:OAH_TASK_NAME;"
                    "$t=$null;if($i.LastRunTime -and $i.LastRunTime.Year -gt 2000){"
                    "$t=$i.LastRunTime.ToUniversalTime().ToString('o')};"
                    "[pscustomobject]@{last_run_utc=$t;last_result=[int64]$i.LastTaskResult;"
                    "missed_runs=[int64]$i.NumberOfMissedRuns}|ConvertTo-Json -Compress")


def os_run_observation(entry: Entry, inspection: Inspection, *, runner: Runner = run_command,
                       search_path: str | None = None, now: Callable[[], dt.datetime] | None = None) -> dict:
    """What the OS itself reports about past runs, bound to the observed definition; `unknown` when unavailable.

    launchd's runs/last exit come from the same `print` observation; Windows runs one fixed read-only PowerShell
    query with the task name passed as data in the environment. A run time never says which trigger caused it."""
    bound = {"name": entry.name, "effective_digest": None if inspection.effective is None
             else _digest(inspection.effective)}
    if entry.kind == "launchd":
        if inspection.os_run is None:
            return {"status": "unknown", "reason": "not-reported", "bound_to": bound}
        return {**inspection.os_run, "bound_to": bound}
    if inspection.state in ("absent", "file-only"):
        return {"status": "unknown", "reason": "no-task", "bound_to": bound}
    clock = now or (lambda: dt.datetime.now(dt.timezone.utc))
    try:
        argv = _tool("powershell", search_path)
    except ScheduleError:
        return {"status": "unknown", "reason": "tool-missing", "bound_to": bound}
    env = dict(os.environ, OAH_TASK_NAME=entry.name)
    result = runner((*argv, "-NoProfile", "-NonInteractive", "-Command", TASK_INFO_SCRIPT), timeout=TOOL_TIMEOUT,
                    env=env)
    if result.timed_out or result.returncode != 0:
        return {"status": "unknown", "reason": "query-failed", "bound_to": bound}
    import json

    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or set(data) != {"last_run_utc", "last_result", "missed_runs"} \
            or type(data["last_result"]) is not int or type(data["missed_runs"]) is not int \
            or not (data["last_run_utc"] is None or isinstance(data["last_run_utc"], str)):
        return {"status": "unknown", "reason": "unreadable", "bound_to": bound}
    return {"status": "observed", "last_run_utc": data["last_run_utc"], "last_result": data["last_result"],
            "missed_runs": data["missed_runs"], "bound_to": bound,
            "observation": _observation("Get-ScheduledTaskInfo", result.stdout,
                                        clock().isoformat(timespec="seconds"))}
