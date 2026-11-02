# 도구 업데이트 패스 — 스위치·작업 중 여부·잠금을 먼저 확인하고 단계를 돌려 기록하며, oah finish 뒤에는 기다리지 않는 분리 프로세스로 시작한다.
"""One tool-update pass, and the three ways in: `oah finish`, `oah update`, `oah update status`/`oah doctor`.

Order in every entry point: a dispatched worker (OAH_WORKER=1) and the off switch stop everything before any
network request, program change or record. Then the machine-wide lock (`~/.oah/updates/pass.lock`, short
timeout: a second pass says OAH_UPDATE_BUSY), then readiness: no lease that is not stale and no run whose
result.json is created, prepared, queued or running (an unreadable run record counts as in flight). Only
then does the pass record its start and run the steps.

`start_after_finish` is what `oah finish` calls once its own lines are printed: it never raises, never
waits and never reads the child's output. The child (`python -m oah update --after-finish --repo <root>`)
checks everything again under the lock and writes its lines to `~/.oah/updates/pass.log`.

The pass never lists, signals or stops processes. Running Claude Code and Codex sessions keep the binaries
and plugins they started with, which every `updated` result says.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
from typing import Callable, Mapping, TextIO

from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError
from oah.journal import PendingTransaction
from oah.lease import Store
from oah.lockfile import LockTimeout, StateLock
from oah.runs import RUN_ID_RE, TERMINAL
from oah.toolupdate import records, steps
from oah.toolupdate.probes import Cli
from oah.toolupdate.steps import RESTART_NOTE, label

PASS_LOCK_TIMEOUT = 1.0
IN_FLIGHT = ("created", "prepared", "queued", "running")
# Windows flags for a child that outlives `oah finish` and its console.
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_popen = subprocess.Popen  # the one place the background pass is started; tests replace it
STARTED = "OAH_UPDATE_STARTED: checking for tool updates in the background; see oah update status"
MODELS_NOTE = "model lists refresh at the next oah call"
RANK = ("claude", "codex", "claude-marketplaces", "claude-plugins", "claude-plugin", "codex-marketplaces",
        "codex-marketplace", "codex-plugin", "oah-skills")

Out = Callable[[str], None]


def _order(name: str) -> tuple[int, str]:
    kind = name.partition(":")[0]
    return RANK.index(kind) if kind in RANK else len(RANK), name


# Readiness -----------------------------------------------------------------------------------------

def readiness(state: pathlib.Path) -> str | None:
    """None when no lease is active and no run is in flight in this repository; else why not."""
    try:
        holders = sorted(lease.task for lease, stale in Store(state).snapshot() if not stale)
    except (StateCorrupt, StorageError, PendingTransaction, OSError, ValueError) as exc:
        return f"the lease records cannot be read ({getattr(exc, 'code', type(exc).__name__)})"
    if holders:
        return f"task {', '.join(holders)} holds a lease"
    runs = state / "runs"
    try:
        if not runs.is_dir():
            return None
        for task in sorted(runs.iterdir()):
            if not task.is_dir() or task.is_symlink():
                continue
            for run in sorted(task.iterdir()):
                if run.name.endswith(".tmp") and run.name.startswith("."):
                    return f"a run of task {task.name} is being created"
                if not RUN_ID_RE.fullmatch(run.name):
                    continue
                status = _run_status(run)
                if status not in TERMINAL:
                    shown = status if status in IN_FLIGHT else "unreadable"
                    return f"run {run.name} of task {task.name} is {shown}"
    except OSError as exc:
        return f"the run records cannot be read ({type(exc).__name__})"
    return None


def _run_status(run: pathlib.Path) -> str | None:
    try:
        if run.is_symlink() or not run.is_dir():
            return None
        data = json.loads((run / "result.json").read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    status = data.get("status") if isinstance(data, dict) else None
    return status if isinstance(status, str) else None


# The pass ------------------------------------------------------------------------------------------

def _redactor(env: Mapping[str, str]) -> Callable[[str], str]:
    from oah.review.runner import credential_redactor

    return credential_redactor(env)


def _show(version: str | None) -> str:
    return version.removeprefix("sha256:")[:12] if version and version.startswith("sha256:") else version or ""


def component_line(name: str, entry: Mapping) -> str:
    """One plain line for a component."""
    before, after, status = _show(entry["before"]), _show(entry["after"]), entry["status"]
    if status == "updated":
        versions = f" {before} -> {after}"
    else:
        versions = f" {after or before}" if after or before else ""
    line = f"oah: {label(name)}{versions} ({status})"
    return f"{line}; {entry['next_step']}" if entry["next_step"] else line


def summary_line(outcome: str, components: Mapping[str, Mapping]) -> str:
    step = ("nothing else to do" if outcome == "complete"
            else "follow the steps above; oah update status shows them again")
    return f"OAH_UPDATE_FINISHED: outcome {outcome} ({records.counts(components)}); {step}"


def _load_state(out: Out) -> dict | None:
    try:
        return records.read_state()
    except records.StateUnavailable as exc:
        out(str(exc))
        return None


def run_pass(root: pathlib.Path, state: pathlib.Path, *, env: Mapping[str, str], trigger: str, out: Out,
             log: pathlib.Path | None = None, now: Callable[[], dt.datetime] = records.utc_now,
             kit: steps.KitStep | None = steps.kit_skills) -> str:
    """Run one pass; returns skipped, busy, deferred, complete or needs-attention."""
    if env.get("OAH_WORKER") == "1":
        out("OAH_UPDATE_SKIPPED: dispatched workers never update tools; the dispatcher's oah finish starts the pass")
        return "skipped"
    try:
        settings = records.read_settings(root)
    except ConfigError as exc:
        out(f"OAH_UPDATE_SKIPPED: {exc}. Tool updates stay off until [updates] can be read")
        return "skipped"
    if not settings.after_finish:
        out("OAH_UPDATE_SKIPPED: tool updates are off for this repository; set after_finish = true under [updates] "
            "in .oah/config.toml to turn them on")
        return "skipped"
    try:
        lock = StateLock(records.lock_path(), timeout=PASS_LOCK_TIMEOUT)
        lock.__enter__()
    except LockTimeout:
        out("OAH_UPDATE_BUSY: another update pass is running; see oah update status when it finishes")
        return "busy"
    except OSError as exc:
        out(f"OAH_UPDATE_SKIPPED: {records.lock_path()} cannot be locked ({type(exc).__name__}); check that "
            "folder's permissions, then run oah update")
        return "skipped"
    lines = _Lines(out, log)
    try:
        return _locked_pass(root, state, settings, env=env, trigger=trigger, out=lines, now=now, kit=kit)
    finally:
        lines.close()
        lock.__exit__(None, None, None)


class _Lines:
    """Print each line; with a log, keep the lines and start the log only once this pass goes ahead."""

    def __init__(self, out: Out, log: pathlib.Path | None) -> None:
        self.out, self.log = out, log
        self.pending: list[str] = []
        self.handle: TextIO | None = None

    def __call__(self, line: str) -> None:
        self.out(line)
        if self.handle is not None:
            self.handle.write(line + "\n")
            self.handle.flush()
        else:
            self.pending.append(line)

    def start_log(self) -> None:
        if self.log is None or self.handle is not None:
            return
        self.handle = open(self.log, "w", encoding="utf-8", newline="\n")
        self.handle.write("".join(line + "\n" for line in self.pending))
        self.handle.flush()

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def _locked_pass(root: pathlib.Path, state: pathlib.Path, settings: records.Settings, *, env: Mapping[str, str],
                 trigger: str, out: _Lines, now: Callable[[], dt.datetime], kit: steps.KitStep | None) -> str:
    redact = _redactor(env)
    previous = _load_state(out)
    started = now()
    if trigger == "after-finish" and not records.due(previous, settings, started):
        out(f"OAH_UPDATE_SKIPPED: a pass started less than {settings.interval_hours:g} hours ago; oah update runs "
            "one now")
        return "skipped"
    out.start_log()
    reason = readiness(state)
    if reason is not None:  # nothing ran, so nothing is recorded; the next finish or oah update tries again
        out(f"OAH_UPDATE_DEFERRED: {redact(reason)}, so nothing was run; the pass starts after the next oah finish "
            "that leaves no work in flight, or run oah update then")
        return "deferred"
    base = previous or {"last_finished": None, "outcome": None, "components": {}, "models_refresh": []}
    records.write_state({"version": records.FORMAT, "last_started": records.stamp(started), "trigger": trigger,
                         "last_finished": base["last_finished"], "outcome": base["outcome"],
                         "components": base["components"], "models_refresh": base["models_refresh"]})
    out("oah: checking Claude Code, Codex, their plugins and oah's kit skills; this can take a few minutes")
    workdir = records.updates_dir()
    context = steps.Context(env=env, checked_at=records.stamp(started),
                            claude=Cli("claude", "Claude Code", env=env, timeout=settings.timeout_seconds, cwd=workdir),
                            codex=Cli("codex", "Codex", env=env, timeout=settings.timeout_seconds, cwd=workdir))
    found: dict[str, dict] = {}
    found.update(steps.cli_update(context, context.claude))
    found.update(steps.cli_update(context, context.codex))
    found.update(steps.claude_plugins(context))
    found.update(steps.codex_plugins(context))
    if kit is not None:
        found.update(kit(root, state, env, started, context.checked_at))
    components = {}
    for name, entry in records.redacted(found, redact).items():
        if records.COMPONENT_RE.fullmatch(name):
            components[name] = entry
    refresh = [engine for engine in records.ENGINES if components.get(engine, {}).get("status") == "updated"]
    result = records.outcome(components)
    finished = records.stamp(now())
    records.write_state({"version": records.FORMAT, "last_started": records.stamp(started), "trigger": trigger,
                         "last_finished": finished, "outcome": result, "components": components,
                         "models_refresh": refresh})
    records.append_run({"version": records.FORMAT, "started": records.stamp(started), "finished": finished,
                        "trigger": trigger, "outcome": result, "components": components, "models_refresh": refresh})
    for name in sorted(components, key=_order):
        if components[name]["status"] != "current":
            out(component_line(name, components[name]))
    if refresh:
        out(f"oah: {MODELS_NOTE} ({', '.join(steps.LABELS[engine] for engine in refresh)} changed)")
    out(summary_line(result, components))
    return result


# After oah finish ----------------------------------------------------------------------------------

def pass_argv(root: pathlib.Path) -> list[str]:
    return [sys.executable, "-m", "oah", "update", "--after-finish", "--repo", str(root)]


def detach_options(os_name: str) -> dict:
    """Start the child in its own session (POSIX) or detached process group without a window (Windows)."""
    if os_name == "nt":
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def start_after_finish(root: pathlib.Path, state: pathlib.Path, env: Mapping[str, str], *, out: Out = print,
                       now: Callable[[], dt.datetime] = records.utc_now, os_name: str = os.name) -> bool:
    """Start the background pass when the switch is on, nothing is in flight and a pass is due.

    Never raises and never waits; `oah finish` keeps its own lines and exit code whatever happens here."""
    try:
        if env.get("OAH_WORKER") == "1":
            return False
        try:
            settings = records.read_settings(root)
        except ConfigError as exc:
            out(f"OAH_UPDATE_SKIPPED: {exc}. Tool updates stay off until [updates] can be read")
            return False
        if not settings.after_finish or readiness(state) is not None:
            return False
        try:
            previous = records.read_state()
        except records.StateUnavailable:
            previous = None  # an unreadable record is no reason to skip
        if not records.due(previous, settings, now()):
            return False
        _popen(pass_argv(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
               cwd=str(root), close_fds=True, **detach_options(os_name))
    except Exception as exc:  # the finish already succeeded; this must never change that
        out(f"OAH_UPDATE_SKIPPED: the background update pass could not start ({type(exc).__name__}); run oah "
            "update to update now")
        return False
    out(STARTED)
    return True


# Reading the last pass -----------------------------------------------------------------------------

def status_lines() -> list[str]:
    """The last pass, from the records only: no subprocess, no network, no write."""
    lines: list[str] = []
    try:
        record = records.read_state()
    except records.StateUnavailable as exc:
        lines.append(str(exc))
        runs = records.read_runs()
        if not runs:
            return lines + ["oah: no readable pass record; run oah update"]
        last = runs[-1]
        record = {"last_started": last["started"], "trigger": last["trigger"], "last_finished": last["finished"],
                  "outcome": last["outcome"], "components": last["components"],
                  "models_refresh": last["models_refresh"]}
    if record is None:
        return lines + ["oah: no pass has run yet; run oah update, or set after_finish = true under [updates]"]
    started, finished = records.parse_stamp(record["last_started"]), records.parse_stamp(record["last_finished"])
    if finished is None or finished < started:
        lines.append(f"oah: a pass started at {record['last_started']} has not finished; it is running, or it "
                     "stopped early (oah update runs a new one)")
    if finished is None:
        return lines
    components = record["components"]
    lines.append(f"oah: last pass finished {record['last_finished']} ({record['trigger']}), outcome "
                 f"{record['outcome']} ({records.counts(components)})")
    lines += [component_line(name, components[name]) for name in sorted(components, key=_order)]
    if record["models_refresh"]:
        changed = ", ".join(steps.LABELS[engine] for engine in record["models_refresh"])
        lines.append(f"oah: {MODELS_NOTE} ({changed} changed)")
    if any(entry["status"] == "updated" for entry in components.values()):
        lines.append(f"oah: {RESTART_NOTE}")
    return lines


def doctor_notices(root: pathlib.Path | None) -> list[dict]:
    """Offline notices for oah doctor: the switch when it is off by absence, and the last pass's unsettled items."""
    notices: list[dict] = []
    if root is None or not (root / ".oah" / "config.toml").is_file():
        return notices
    try:
        settings = records.read_settings(root)
    except ConfigError as exc:
        return [{"code": exc.code, "message": f"[updates] cannot be read ({exc.code}); fix .oah/config.toml"}]
    if not settings.explicit:
        notices.append({"code": "OAH_UPDATE_OFF", "message": "tool updates are off: .oah/config.toml has no "
                        "after_finish under [updates]. Set it to true to keep Claude Code, Codex, their plugins and "
                        "oah's skills up to date after your work finishes (oah update then runs a pass now), or to "
                        "false to keep them off and stop this notice"})
    try:
        record = records.read_state()
    except records.StateUnavailable as exc:
        return notices + [{"code": exc.code, "message": str(exc)}]
    for name, entry in sorted((record or {}).get("components", {}).items(), key=lambda item: _order(item[0])):
        if entry["status"] not in records.SETTLED:
            notices.append({"code": "OAH_UPDATE_UNSETTLED", "component": name, "status": entry["status"],
                            "message": component_line(name, entry).removeprefix("oah: ")})
    return notices
