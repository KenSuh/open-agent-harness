# 동의 기반 도구 설치 안내 — 도구를 확인하고, 설치 계획을 보여 주고, 도구마다 묻고 공식 명령만 실행하며, 로그인을 안내한다.
"""The guided tool setup.

Every collaborator is a parameter, so the logic runs against fakes in tests and against the real
machine from `python -m oah.setup`, the installers and `oah init`:
- `which` finds programs; `confirm` decides per step; `runner` runs one argv and returns its exit code;
- `status_runner` runs a sign-in status command and returns `(exit code or None, stdout)`.
Status output is used only to read a `loggedIn` flag. It is never printed or stored.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, MutableMapping, Sequence

from oah.engines.proc import UnsupportedBatchShim, is_batch, resolve_command, run_command
from oah.setup.tools import Argv, Tool, catalog, display, is_one_liner, needs
from oah.unlazy import find_unlazy_dir

MIN_PYTHON = (3, 11)
STATUS_TIMEOUT = 30.0
OS_LABELS = {"macos": "macOS", "windows": "Windows"}
HOST = re.compile(r"https?://([^/\s]+)")

Which = Callable[..., "str | None"]
Out = Callable[[str], None]
Ask = Callable[[str], "str | None"]  # like input(), but None at end of input
StatusRunner = Callable[[Argv], "tuple[int | None, str]"]


@dataclass(frozen=True)
class ToolStatus:
    tool: Tool
    present: bool
    path: str | None
    reason: str


@dataclass(frozen=True)
class Step:
    tool: Tool
    argv: Argv | None  # None: no command can run here, so the user installs from `url`
    display: str
    url: str
    note: str

    @property
    def manual(self) -> bool:
        return self.argv is None


@dataclass(frozen=True)
class StepResult:
    step: Step
    status: str  # "installed", "failed", "skipped" or "manual"


def _programs(tool: Tool) -> tuple[str, ...]:
    """The programs a tool puts on PATH once installed (Node also brings npm and npx)."""
    if tool.name == "node":
        return ("node", "npm", "npx")
    return (tool.check[0],) if tool.check else (tool.name,)


def check_tools(tools: Iterable[Tool], *, which: Which = shutil.which, env: Mapping[str, str] | None = None,
                home: pathlib.Path | None = None, repo_root: pathlib.Path | None = None,
                version_info: Sequence[int] | None = None) -> list[ToolStatus]:
    """Which tools are present. Only looks things up; runs nothing."""
    env = os.environ if env is None else env
    home = pathlib.Path.home() if home is None else home
    version_info = sys.version_info if version_info is None else version_info
    statuses = []
    for tool in tools:
        if tool.detect == "python":
            version = ".".join(str(part) for part in version_info[:3])
            ok = tuple(version_info[:2]) >= MIN_PYTHON
            statuses.append(ToolStatus(tool, ok, sys.executable if ok else None,
                                       f"Python {version}" if ok else f"Python {version} is older than 3.11"))
        elif tool.detect == "unlazy":
            folder, reason = find_unlazy_dir(repo_root, env, home)
            reason = reason.partition(" (install: ")[0]  # install advice comes from the pinned catalog
            statuses.append(ToolStatus(tool, folder is not None, str(folder) if folder else None, reason))
        else:
            found = which(tool.check[0])
            statuses.append(ToolStatus(tool, found is not None, found, "found" if found else "not found"))
    return statuses


def _one_liner_note(argv: Argv) -> str:
    match = HOST.search(argv[-1])
    host = match.group(1) if match else "the tool's site"
    return f"This downloads the official install script from {host} and runs it. It may add its folder to your PATH."


def plan_installs(statuses: Iterable[ToolStatus], *, os_name: str, which: Which = shutil.which) -> list[Step]:
    """One step per missing tool: the first official command whose program exists here, else a manual step.

    A program that an earlier step installs counts as available (uv before graphify).
    """
    provided: set[str] = set()
    steps = []
    for status in statuses:
        if status.present:
            continue
        tool = status.tool
        alternatives = tool.install.get(os_name, ())
        notes = [tool.note[os_name]] if os_name in tool.note else []
        chosen = next((argv for argv in alternatives
                       if needs(argv) in provided or which(needs(argv)) is not None), None)
        if chosen is not None:
            if is_one_liner(chosen):
                notes.append(_one_liner_note(chosen))
            steps.append(Step(tool, chosen, display(chosen), tool.url_for(os_name), " ".join(notes)))
            provided.update(_programs(tool))
        else:
            if alternatives:
                wanted = sorted({needs(argv) for argv in alternatives})
                notes.append(f"{' or '.join(wanted)} is not installed, so oah cannot run the "
                             "command here. Install it from the official page instead.")
            preferred = display(alternatives[0]) if alternatives else ""
            steps.append(Step(tool, None, preferred, tool.url_for(os_name), " ".join(notes)))
    return steps


def run_interactive(argv: Argv) -> int:
    """Run an install command attached to this terminal, so its own prompts reach the user directly."""
    resolved = resolve_command(argv[0])
    if resolved is None:
        raise FileNotFoundError(f"{argv[0]} was not found on PATH")
    if is_batch(resolved.argv[0]):
        raise UnsupportedBatchShim(resolved.argv[0], "batch files are never launched directly")
    # uv must not edit profiles or persistent PATH before the separate consent prompt.
    return subprocess.run(list(resolved.argv) + list(argv[1:]), check=False,
                          env=dict(os.environ, UV_NO_MODIFY_PATH="1")).returncode


def run_installs(steps: Iterable[Step], *, confirm: Callable[[Step], bool], runner: Callable[[Argv], int] = run_interactive,
                 refresh: Callable[[], None] | None = None, out: Out = print) -> list[StepResult]:
    """Ask for each step and run only the confirmed ones. PATH is refreshed after every install."""
    refresh = refresh_path if refresh is None else refresh
    results = []
    for step in steps:
        name = step.tool.name
        if step.manual:
            out(f"{name}: {step.note}" if step.note else f"{name}: install it from {step.url}")
            out(f"  Official page: {step.url}")
            results.append(StepResult(step, "manual"))
            continue
        if not confirm(step):
            out(f"{name}: skipped. To install it later, run: {step.display}")
            results.append(StepResult(step, "skipped"))
            continue
        out(f"Running: {step.display}")
        try:
            code = runner(step.argv)
        except (OSError, UnsupportedBatchShim) as exc:
            out(f"{name}: could not start the installer: {exc}. Install it from {step.url}, then run this "
                "setup again.")
            results.append(StepResult(step, "failed"))
            continue
        if code != 0:
            out(f"{name}: the installer exited with code {code}. Read its messages above, fix the cause, "
                "then run this setup again.")
            results.append(StepResult(step, "failed"))
            continue
        refresh()
        out(f"{name}: installed.")
        results.append(StepResult(step, "installed"))
    return results


def _read_registry_path() -> tuple[str, str]:
    import winreg  # Windows only

    values = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(root, key) as handle:
                values.append(str(winreg.QueryValueEx(handle, "Path")[0]))
        except OSError:
            values.append("")
    return values[0], values[1]


def _expand(entry: str, env: Mapping[str, str]) -> str:
    lowered = {key.lower(): value for key, value in env.items()}
    return re.sub(r"%([^%]+)%", lambda m: lowered.get(m.group(1).lower(), m.group(0)), entry)


def refresh_path(env: MutableMapping[str, str] | None = None, *, platform: str = sys.platform,
                 read_registry: Callable[[], tuple[str, str]] | None = None,
                 home: pathlib.Path | None = None) -> None:
    """Let this process see programs an installer just added.

    Windows: the machine and user Path from the registry, then entries only this process had.
    Elsewhere: ~/.local/bin (where uv and Claude Code install) is appended when it exists.
    Only this process's environment changes; no profile or registry value is written.
    """
    env = os.environ if env is None else env
    current = env.get("PATH", "")
    if platform == "win32":
        machine, user = (read_registry or _read_registry_path)()
        merged: list[str] = []
        seen: set[str] = set()
        for entry in [*machine.split(";"), *user.split(";"), *current.split(";")]:
            entry = _expand(entry.strip(), env)
            if entry and entry.lower() not in seen:
                seen.add(entry.lower())
                merged.append(entry)
        env["PATH"] = ";".join(merged)
        return
    local = (pathlib.Path.home() if home is None else home) / ".local" / "bin"
    if local.is_dir() and str(local) not in current.split(":"):
        env["PATH"] = f"{current}:{local}" if current else str(local)


def default_status_runner(argv: Argv) -> tuple[int | None, str]:
    try:
        resolved = resolve_command(argv[0])
        if resolved is None:
            return None, ""
        result = run_command(resolved.argv + tuple(argv[1:]), timeout=STATUS_TIMEOUT)
    except (OSError, UnsupportedBatchShim):
        return None, ""
    return (None if result.timed_out else result.returncode), result.stdout


def check_sign_in(tool: Tool, path: str, *, status_runner: StatusRunner = default_status_runner) -> bool:
    """True when the tool's own status command says it is signed in. Reads only a `loggedIn` flag."""
    assert tool.sign_in is not None
    # The resolver needs the catalog name to match an npm manifest, not the discovered .cmd path.
    # default_status_runner keeps its resolved argv (node + declared bin) for the status command.
    return signed_in(*status_runner((tool.check[0],) + tool.sign_in.status_args))


def signed_in(code: int | None, stdout: str) -> bool:
    """Exit code 0, and a JSON `loggedIn` flag (Claude Code's status) that is not false."""
    if code != 0:
        return False
    try:
        data = json.loads(stdout)
    except ValueError:
        return True
    if isinstance(data, dict) and "loggedIn" in data:
        return data["loggedIn"] is True
    return True


def walk_sign_ins(statuses: Iterable[ToolStatus], *, status_runner: StatusRunner = default_status_runner,
                  interactive: bool, ask: Ask, out: Out = print) -> list[str]:
    """Check each installed tool's sign-in and guide the user through it. Returns the tools still signed out."""
    pending = []
    for status in statuses:
        tool = status.tool
        if tool.sign_in is None or not status.present or status.path is None:
            continue
        if check_sign_in(tool, status.path, status_runner=status_runner):
            out(f"  {tool.name}: signed in")
            continue
        out(f"  {tool.name}: not signed in yet.")
        out("    1. Open a new terminal window.")
        out(f"    2. Run: {tool.sign_in.login}")
        out("    3. Finish signing in. Your password and keys stay with that tool; oah never sees them.")
        if not interactive:
            out(f"    Then run `{tool.sign_in.login}` whenever you are ready; `oah doctor` checks it.")
            pending.append(tool.name)
            continue
        while True:
            answer = ask(f"    Press Enter here when you have signed in to {tool.name}, or type s and Enter to skip: ")
            if answer is None or answer.strip().lower() == "s":
                out(f"    Skipped. Run `{tool.sign_in.login}` later; `oah doctor` checks it.")
                pending.append(tool.name)
                break
            if check_sign_in(tool, status.path, status_runner=status_runner):
                out(f"  {tool.name}: signed in")
                break
            out(f"    {tool.name} still reports that you are not signed in. Finish `{tool.sign_in.login}` first.")
    return pending


def _ask_input(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        return None


def _confirm_with(ask: Ask, out: Out) -> Callable[[Step], bool]:
    def confirm(step: Step) -> bool:
        tool = step.tool
        out("")
        out(f"Install {tool.name} ({tool.requirement}: {tool.purpose})?")
        out(f"  This runs: {step.display}")
        if step.note:
            out(f"  Note: {step.note}")
        out(f"  Official page: {step.url}")
        answer = ask("  Install now? [y/N] ")
        return answer is not None and answer.strip().lower() in ("y", "yes")

    return confirm


def _status_table(statuses: Sequence[ToolStatus], out: Out) -> None:
    width = max(len(s.tool.name) for s in statuses)
    for status in statuses:
        state = "found" if status.present else "missing"
        out(f"  {status.tool.name.ljust(width)}  {status.tool.requirement.ljust(11)}  {state}")


def guided_setup(*, os_name: str, yes: bool = False, dry_run: bool = False, tools: Sequence[Tool] | None = None,
                 which: Which = shutil.which, ask: Ask = _ask_input, out: Out = print,
                 runner: Callable[[Argv], int] = run_interactive,
                 status_runner: StatusRunner = default_status_runner, refresh: Callable[[], None] | None = None,
                 env: Mapping[str, str] | None = None, home: pathlib.Path | None = None,
                 repo_root: pathlib.Path | None = None) -> int:
    """Check, plan, ask per tool (or `yes`), install, re-check, then guide the sign-ins.

    Returns 0 when every required tool is present and signed in, else 1. A dry run only checks and
    prints the plan, and returns 0.
    """
    tools = catalog() if tools is None else list(tools)
    out(f"oah setup ({OS_LABELS.get(os_name, os_name)}){' - dry run, nothing will be installed' if dry_run else ''}")
    out("Checking tools:")
    statuses = check_tools(tools, which=which, env=env, home=home, repo_root=repo_root)
    _status_table(statuses, out)
    steps = plan_installs(statuses, os_name=os_name, which=which)
    out("")
    if not steps:
        out("Every tool is already installed.")
    else:
        out(f"Plan: {len(steps)} tool(s) to install.")
        for step in steps:
            how = step.display
            if step.manual:
                how = (f"no installer for it here; get it from the page below (official command: {step.display})"
                       if step.display else step.note or "Install it from the official page below.")
            out(f"  - {step.tool.name} ({step.tool.requirement}): {how}")
            if step.manual and step.display and step.note:
                out(f"      {step.note}")
            out(f"      {step.url}")
    if dry_run:
        out("")
        out("Sign-in (checked only):")
        walk_sign_ins(statuses, status_runner=status_runner, interactive=False, ask=ask, out=out)
        out(f"SETUP_DRY_RUN planned={len(steps)}")
        return 0
    if steps:
        confirm = (lambda step: True) if yes else _confirm_with(ask, out)
        out("")
        results = run_installs(steps, confirm=confirm, runner=runner, refresh=refresh, out=out)
        statuses = check_tools(tools, which=which, env=env, home=home, repo_root=repo_root)
        still = [r.step for r in results if r.status != "installed"]
        if still:
            out("")
            out("Not installed this time (install them any time later):")
            for step in still:
                out(f"  - {step.tool.name}: {step.display or 'see link'}  ({step.url})")
    out("")
    out("Sign-in:")
    pending = walk_sign_ins(statuses, status_runner=status_runner, interactive=not yes, ask=ask, out=out)
    missing = [s.tool.name for s in statuses if s.tool.required and not s.present]
    missing += [f"{s.tool.name}:sign-in" for s in statuses if s.tool.required and s.tool.name in pending]
    out("")
    if missing:
        out("Some required tools are not ready yet. Install or sign in to them as shown above, then run "
            "`oah doctor` to check again.")
        out(f"SETUP_INCOMPLETE missing={','.join(missing)}")
        return 1
    out("SETUP_OK")
    return 0
