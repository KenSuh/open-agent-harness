# Orca 실행기 — 준비된 실행을 Orca 워크트리 터미널에서 oah worker-run 으로 돌리고, 터미널을 9분 상한 안에서 따라가며 출력과 결과를 기록한다.
"""The optional Orca launcher for `oah call` (PLAN section 4 row 8, 5.5, A1, A6, A17).

Orca runs oah's own runner, never the engine: the terminal command is `oah worker-run <run-id>; exit`,
and the runner calls `oah.call.execute` on the prepared run, so the brief goes from `prompt.md` to the
engine's stdin byte for byte, the worker variables are set, a review stays read-only and the effort
checks run as in P3b. Orca never sees the brief text. The run id is the only value in that command and
passes P3b's safe-character rule; the runner finds the state folder from the terminal's working
directory (the worktree) through git, which is why an `OAH_STATE_DIR` override is refused.

Only commands documented for Orca 1.4.237 are used, always with `--json`: `--version`, `worktree show`,
`worktree create`, `terminal create`, `terminal wait --for exit`, `terminal read` and `terminal show`.
Orca's terminal API reports no model or effort, so `reported_model` and `reported_effort` are
`unverified`; the engine-level effort fields come from the runner. `wait` follows the terminal in
slices inside the 9-minute bound and copies new output into `terminal.log`; `result.json` stays the
source of truth, and a terminal that ends without a final record makes the run `lost`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import secrets
import time
from typing import Mapping, Sequence

from oah import briefs, call, runs
from oah.atomic import read_json, write_json_atomic, write_text_atomic
from oah.engines.effort import EngineError
from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command
from oah.paths import STATE_ENV

INSTALL_URL = "https://www.onorca.dev/docs/install"
UNAVAILABLE = "OAH_LAUNCHER_UNAVAILABLE"
FAILED = "OAH_LAUNCHER_FAILED"
PROBE_TIMEOUT = 15.0
CLI_TIMEOUT = 60.0
WORKTREE_TIMEOUT = 600.0  # Orca may run the repository's setup hooks while it creates a worktree
WAIT_SLICE_S = 30.0
READ_LIMIT = 200
READ_PAGES = 5
RUNNER = "worker-run"
TERMINAL_LOG = "terminal.log"
_WITHOUT = ("Nothing was launched. To run without Orca, drop --launcher orca or set "
            '[call] launcher = "process" in .oah/config.toml')
_ENVIRONMENT = frozenset(("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "LOCALAPPDATA", "APPDATA",
                          "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"))


def _environment(env: Mapping[str, str]) -> dict[str, str]:
    """Only OS paths and locale reach Orca; engine credentials stay in the worker's environment."""
    return {key: value for key, value in env.items() if key.upper() in _ENVIRONMENT}


class OrcaReplyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass(frozen=True)
class Orca:
    base: tuple[str, ...]
    version: str
    env: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", _environment(self.env))

    def json(self, *args: str, timeout: float = CLI_TIMEOUT) -> dict:
        """Run one documented command with --json and return its `result` object."""
        try:
            result = run_command([*self.base, *args, "--json"], timeout=timeout, env=self.env)
        except (OSError, ValueError) as exc:
            raise OrcaReplyError("cli-failed", str(exc)) from exc
        if result.timed_out:
            raise OrcaReplyError("cli-timeout", f"`orca {' '.join(args[:2])}` gave no answer within {timeout:g} s")
        try:
            data = json.loads(result.stdout)
        except ValueError:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise OrcaReplyError("unreadable", detail[-1][:300] if detail else f"exit code {result.returncode}") \
                from None
        if not isinstance(data, dict) or type(data.get("ok")) is not bool:
            raise OrcaReplyError("unreadable", "the reply has no boolean ok field")
        if data["ok"] is False:
            if result.returncode == 0:
                raise OrcaReplyError("cli-failed", "the error reply disagrees with exit code zero")
            error = data.get("error")
            if not isinstance(error, dict) or not all(isinstance(error.get(key), str) and error[key]
                                                     for key in ("code", "message")):
                raise OrcaReplyError("unreadable", "the reply has no error code and message")
            raise OrcaReplyError(error["code"], error["message"])
        if result.returncode != 0:
            raise OrcaReplyError("cli-failed", f"the success reply disagrees with exit code {result.returncode}")
        found = data.get("result")
        if not isinstance(found, dict):
            raise OrcaReplyError("unreadable", "the reply has no result object")
        return found


def find(command: Sequence[str], env: Mapping[str, str], search_path: str | None = None) -> Orca:
    """The configured orca command after `orca --version` succeeds; OAH_LAUNCHER_UNAVAILABLE otherwise."""
    if not command or not all(isinstance(part, str) and part for part in command):
        raise EngineError("OAH_CONFIG_INVALID", ".oah/config.toml: call.orca_command must be a non-empty list of "
                          'strings, such as ["orca"]')
    first = command[0]
    if "/" in first or "\\" in first:
        base = list(command)
    else:
        try:
            found = resolve_command(first, search_path)
        except UnsupportedBatchShim as exc:
            raise EngineError(UNAVAILABLE, f"{str(exc).split(': ', 1)[1]} {_WITHOUT}") from exc
        if found is None:
            raise EngineError(UNAVAILABLE, f"Orca's command-line tool `{first}` was not found on PATH. Install Orca "
                              f"from {INSTALL_URL} and turn on its command-line tool, or set [call] orca_command "
                              f"in .oah/config.toml. {_WITHOUT}")
        base = [*found.argv, *command[1:]]
    try:
        probe = run_command([*base, "--version"], timeout=PROBE_TIMEOUT, env=_environment(env))
        output = probe.stdout.strip().splitlines()
        version = output[0].strip() if output and probe.returncode == 0 and not probe.timed_out else ""
        detail = (probe.stderr.strip().splitlines() or [f"exit code {probe.returncode}"])[-1]
    except UnsupportedBatchShim as exc:
        raise EngineError(UNAVAILABLE, "Orca's configured command is a batch shim. Install a native executable "
                          "or configure an explicit [interpreter.exe, script] command in [call] orca_command, "
                          f"then retry. {_WITHOUT}") from exc
    except (OSError, ValueError) as exc:
        version, detail = "", str(exc)
    if not version:
        raise EngineError(UNAVAILABLE, f"`{first} --version` failed ({detail[:200]}), so Orca's command-line tool "
                          f"is not usable. Open Orca and turn on its command-line tool (install: {INSTALL_URL}), "
                          f"then retry. {_WITHOUT}")
    return Orca(tuple(base), version, dict(env))


def check_runner(env: Mapping[str, str], search_path: str | None = None) -> None:
    """The terminal must be able to start `oah worker-run` and find this repository's state folder."""
    if env.get(STATE_ENV):
        raise EngineError(UNAVAILABLE, f"{STATE_ENV} is set, but an Orca terminal starts from Orca's own "
                          "environment and cannot inherit it, so the worker would not find this run. Unset "
                          f"{STATE_ENV} for this call. {_WITHOUT}")
    try:
        found = resolve_command("oah", search_path)
    except UnsupportedBatchShim:
        found = None
    if found is None:
        raise EngineError(UNAVAILABLE, f"the Orca terminal starts the worker with `oah {RUNNER}`, but no native "
                          "`oah` is on PATH. Install oah so that `oah --version` works in a new terminal (see the "
                          f"README), then retry. {_WITHOUT}")


@dataclasses.dataclass(frozen=True)
class Placement:
    path: pathlib.Path
    worktree_id: str
    created: bool


def _show(orca: Orca, selector: str) -> dict:
    worktree = orca.json("worktree", "show", "--worktree", selector).get("worktree")
    if not isinstance(worktree, dict) or not all(isinstance(worktree.get(key), str) and worktree.get(key)
                                                 for key in ("id", "path")):
        raise OrcaReplyError("unreadable", "the reply has no result.worktree.id and path")
    host = worktree.get("hostId")
    if host not in (None, "local"):
        raise EngineError(FAILED, f"Orca runs the worktree {worktree['path']} on host {host}; the Orca launcher "
                          "supports only this computer. Use a local worktree, or run without --launcher orca")
    return worktree


def existing_worktree(orca: Orca, path: pathlib.Path) -> Placement:
    """The Orca worktree at a path P3b already resolved from --worktree."""
    try:
        worktree = _show(orca, f"path:{path}")
    except OrcaReplyError as exc:
        raise EngineError(FAILED, f"Orca does not show the worktree {path} ({exc}). Open the repository in Orca "
                          "so it lists this worktree, or call without --worktree to let Orca create one. "
                          "Nothing was launched") from exc
    return Placement(pathlib.Path(worktree["path"]), worktree["id"], False)


def new_worktree(orca: Orca, root: pathlib.Path, task: str) -> Placement:
    """An independent Orca worktree for this run, from the repository's default base."""
    try:
        repo = _show(orca, f"path:{root}").get("repoId")
        if not isinstance(repo, str) or not repo:
            raise OrcaReplyError("unreadable", "the reply has no result.worktree.repoId")
        name = f"oah-{task}-{secrets.token_hex(4)}"
        created = orca.json("worktree", "create", "--repo", f"id:{repo}", "--name", name, "--no-parent",
                            timeout=WORKTREE_TIMEOUT).get("worktree")
        worktree_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(worktree_id, str) or not worktree_id:
            raise OrcaReplyError("unreadable", "the reply has no result.worktree.id")
        worktree = _show(orca, f"id:{worktree_id}")
    except OrcaReplyError as exc:
        raise EngineError(FAILED, f"Orca could not create a worktree for this run ({exc}). Fix what Orca reports, "
                          "or pass --worktree with an existing worktree, then retry. Nothing was launched") from exc
    return Placement(pathlib.Path(worktree["path"]), worktree["id"], True)


def runner_command(run_id: str) -> str:
    """The command typed into the terminal; the run id is its only value (P3b's safe-character rule)."""
    return f"oah {RUNNER} {briefs.shell_word(run_id)}; exit"


def terminal_args(placement: Placement, title: str, command: str, *, windows: bool) -> list[str]:
    args = ["terminal", "create", "--worktree", f"id:{placement.worktree_id}", "--title", title, "--command", command]
    if windows:
        args += ["--shell", "powershell.exe"]  # Windows PowerShell 5.1 understands `;` and `exit`
    return args


def _launch_digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def worker_prepared(run: runs.Run, root: pathlib.Path, settings: call.Settings) -> call.Prepared:
    """Bind the saved execution to the located run before consuming any execution metadata."""
    data = run.read()
    saved = read_json(run.launch_path, None)
    launcher = data.get("launcher") or {}

    def invalid() -> runs.RunError:
        return runs.RunError("OAH_RUN_LAUNCH_INVALID", "the saved launch does not match this queued run; "
                             "start a new run with oah call")

    if not isinstance(saved, dict) or _launch_digest(saved) != launcher.get("launch_digest"):
        raise invalid()
    try:
        prepared = call.Prepared.from_json(saved)
    except (KeyError, TypeError, ValueError):
        raise invalid() from None
    if (prepared.run_id != run.id or prepared.task != run.task or prepared.run_dir != str(run.path) or
            prepared.engine not in call.routing.ENGINES or prepared.stage not in call.routing.STAGES or
            any(data.get(key) != saved[key] for key in ("run_id", "task", "engine", "stage", "cwd")) or
            data.get("command") != list(prepared.argv) or data.get("resolved_model") != prepared.model or
            data.get("launch_effort") != prepared.launch_effort or
            data.get("cli_version") != (prepared.cli_version or runs.UNVERIFIED) or
            data.get("read_only") is not (prepared.stage == "review") or
            pathlib.Path(prepared.cwd).resolve() != root.resolve() or
            launcher.get("worktree") != prepared.cwd):
        raise invalid()
    call.routing.check_stage(prepared.engine, prepared.stage, settings.stages,
                             override=data.get("override_stage") is True)
    additions = {"OAH_WORKER": "1", "OAH_RUN_ID": run.id, "OAH_TASK": run.task}
    if prepared.env_additions != additions:
        raise invalid()
    return dataclasses.replace(prepared, env_additions=additions)


def start(orca: Orca, prepared: call.Prepared, placement: Placement, *, windows: bool | None = None) -> dict:
    """Queue the prepared run with its launcher record and open its Orca terminal; returns the record."""
    run = prepared.run
    command = runner_command(prepared.run_id)
    launch = prepared.to_json()
    write_json_atomic(run.launch_path, launch)
    info = {"name": "orca", "orca_version": orca.version, "worktree": str(placement.path),
            "worktree_id": placement.worktree_id, "worktree_created": placement.created, "terminal": None,
            "terminal_process_id": None, "host_platform": runs.UNVERIFIED, "command": command,
            "worker_env": "env", "reported_model": runs.UNVERIFIED, "reported_effort": runs.UNVERIFIED,
            "terminal_cursor": "0", "launch_digest": _launch_digest(launch)}
    run.result(status="queued", launcher=info)
    windows = os.name == "nt" if windows is None else windows
    try:
        terminal = orca.json(*terminal_args(placement, f"oah {prepared.task} {prepared.stage}", command,
                                            windows=windows)).get("terminal")
        handle = terminal.get("handle") if isinstance(terminal, dict) else None
        if not isinstance(handle, str) or not handle:
            raise OrcaReplyError("unreadable", "the reply has no result.terminal.handle")
    except OrcaReplyError as exc:
        error = (f"{FAILED}: Orca could not open a terminal for run {prepared.run_id} ({exc}); the run was not "
                 "started. Check that Orca is open (`orca status`) and retry, or run without --launcher orca")
        run.result(status="failed", error=error)
        raise EngineError(FAILED, error.split(": ", 1)[1]) from exc
    pid = terminal.get("processId")
    info.update(terminal=handle, terminal_process_id=pid if isinstance(pid, int) else None,
                host_platform=str(terminal.get("hostPlatform") or runs.UNVERIFIED))
    return run.result(launcher=info)


def _append_log(run: runs.Run, lines: Sequence[str]) -> None:
    path = run.file_path(TERMINAL_LOG)
    before = path.read_bytes().decode("utf-8", errors="replace") if path.exists() else ""
    write_text_atomic(path, before + "".join(f"{line}\n" for line in lines))


def _failure(exc: OrcaReplyError) -> EngineError:
    return EngineError(FAILED, f"Orca could not follow the terminal ({exc}); check Orca's command-line tool "
                       "and retry oah wait, or run without --launcher orca")


_MISSING_TERMINAL = ("terminal_handle_stale", "selector_not_found")


def _capture(orca: Orca, run: runs.Run, handle: str, deadline: float) -> None:
    """Copy new terminal output into terminal.log, in at most READ_PAGES reads of READ_LIMIT lines."""
    info = dict(run.read().get("launcher") or {})
    cursor = start_cursor = str(info.get("terminal_cursor") or "0")
    lines: list[str] = []
    for _ in range(READ_PAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            terminal = orca.json("terminal", "read", "--terminal", handle, "--cursor", cursor, "--limit",
                                 str(READ_LIMIT), timeout=min(CLI_TIMEOUT, remaining)).get("terminal")
        except OrcaReplyError as exc:
            if exc.code not in (*_MISSING_TERMINAL, "cli-timeout", "timeout"):
                raise _failure(exc) from exc
            break
        if (not isinstance(terminal, dict) or not isinstance(terminal.get("tail"), list) or
                not all(isinstance(line, str) for line in terminal["tail"]) or
                not all(isinstance(terminal.get(key), str) and terminal[key]
                        for key in ("nextCursor", "latestCursor"))):
            raise _failure(OrcaReplyError("unreadable", "the reply has no terminal tail and cursors"))
        tail = terminal.get("tail")
        lines.extend(tail)
        following = terminal.get("nextCursor")
        if not isinstance(following, str) or following == cursor:
            break
        cursor = following
        if following == terminal.get("latestCursor"):
            break
    if lines:
        _append_log(run, lines)
    if cursor != start_cursor:
        run.result(launcher=dict(info, terminal_cursor=cursor))


def _preview(orca: Orca, run: runs.Run, handle: str, deadline: float) -> None:
    preview = None
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            terminal = orca.json("terminal", "show", "--terminal", handle,
                                 timeout=min(CLI_TIMEOUT, remaining)).get("terminal")
            if not isinstance(terminal, dict) or not isinstance(terminal.get("preview"), str):
                raise OrcaReplyError("unreadable", "the reply has no terminal preview string")
            preview = terminal["preview"]
        except OrcaReplyError as exc:
            if exc.code not in (*_MISSING_TERMINAL, "cli-timeout", "timeout"):
                raise _failure(exc) from exc
    if isinstance(preview, str) and preview:
        _append_log(run, ["--- terminal preview ---", *preview.splitlines()])


def _lost(orca: Orca, run: runs.Run, handle: str, reason: str, deadline: float) -> dict:
    _preview(orca, run, handle, deadline)
    return run.result(status="lost", error=f"OAH_RUN_LOST: the Orca terminal {handle} {reason} before the runner "
                      f"recorded a result; read {run.file_path(TERMINAL_LOG)} and call the engine again")


def wait(orca: Orca, state_dir: pathlib.Path, run_id: str, *, timeout: float = call.WAIT_LIMIT_S,
         slice_s: float = WAIT_SLICE_S) -> dict:
    """The run's record once it ends, or the current record after at most `timeout` (capped at 9 minutes)."""
    run = runs.find_run(state_dir, run_id)
    deadline = time.monotonic() + min(max(timeout, 0.0), call.WAIT_LIMIT_S)
    while True:
        data = run.read()
        if data.get("status") in runs.TERMINAL:
            return data
        handle = (data.get("launcher") or {}).get("terminal")
        if not handle:
            return run.result(status="lost", error=f"OAH_RUN_LOST: run {run_id} has no Orca terminal, so it never "
                              "started; call the engine again")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return data
        ms = max(1, int(min(remaining, slice_s) * 1000))
        problem = None
        try:
            reply = orca.json("terminal", "wait", "--terminal", handle, "--for", "exit", "--timeout-ms", str(ms),
                              timeout=min(remaining, ms / 1000 + 1.0))
            found = reply.get("wait")
            if (not isinstance(found, dict) or found.get("status") != "exited" or
                    found.get("satisfied") is not True or found.get("condition") != "exit"):
                raise OrcaReplyError("unreadable", "the reply has no satisfied exit wait object")
            exited = True
        except OrcaReplyError as exc:
            if exc.code not in (*_MISSING_TERMINAL, "cli-timeout", "timeout"):
                raise _failure(exc) from exc
            exited, problem = False, (None if exc.code in ("timeout", "cli-timeout") else exc)
        _capture(orca, run, handle, deadline)
        if not exited and problem is None:
            continue
        data = run.read()
        if data.get("status") in runs.TERMINAL:
            if exited:
                _preview(orca, run, handle, deadline)
            return data
        pid = data.get("pid")
        if problem is not None and isinstance(pid, int) and call._alive(pid):
            time.sleep(min(1.0, max(deadline - time.monotonic(), 0.0)))  # Orca lost the handle; the runner lives
            continue
        return _lost(orca, run, handle, "exited" if exited else f"is unknown to Orca ({problem.code})", deadline)


def wait_until_done(orca: Orca, state_dir: pathlib.Path, run_id: str) -> dict:
    """A foreground call: follow the terminal in bounded waits until the run ends."""
    while True:
        data = wait(orca, state_dir, run_id)
        if data.get("status") in runs.TERMINAL:
            return data
