# oah doctor — 도구 목록의 필수·권장 도구가 설치됐는지 확인하고, 빠진 도구는 이 OS 의 공식 설치 명령으로 안내한다.
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim, resolve_command, run_command
from oah.setup.guide import signed_in
from oah.setup.tools import Tool, catalog, display, os_name_for
from oah.unlazy import find_unlazy_dir, unlazy_version

MIN_PYTHON = (3, 11)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

PLATFORM_LABELS = {"darwin": "macOS", "win32": "Windows"}


@dataclass
class ToolStatus:
    name: str
    requirement: str  # "required" or "recommended"
    status: str  # ok, missing, too-old, not-signed-in, unsupported-shim or error
    version: str | None
    source: str | None
    note: str


Resolver = Callable[..., "Resolved | None"]
Runner = Callable[..., RunResult]


def first_line(text: str) -> str | None:
    for line in ANSI.sub("", text).splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return None


def python_status(version_info: Sequence[int]) -> ToolStatus:
    version = ".".join(str(part) for part in version_info[:3])
    if tuple(version_info[:2]) >= MIN_PYTHON:
        return ToolStatus("python", "required", "ok", version, sys.executable, "runs oah")
    return ToolStatus("python", "required", "too-old", version, sys.executable, "oah needs Python 3.11 or newer")


def _check_tool(tool: Tool, *, search_path: str | None, timeout: float, resolver: Resolver,
                runner: Runner) -> ToolStatus:
    name, requirement, purpose = tool.name, tool.requirement, tool.purpose
    try:
        resolved = resolver(tool.check[0], search_path)
    except UnsupportedBatchShim as exc:
        return ToolStatus(name, requirement, "unsupported-shim", None, exc.path, str(exc))
    if resolved is None:
        return ToolStatus(name, requirement, "missing", None, None, purpose)
    try:
        result = runner(resolved.argv + tool.version_args, timeout=timeout)
    except OSError as exc:
        return ToolStatus(name, requirement, "error", None, resolved.source,
                          f"{purpose}; --version could not launch: {exc}")
    if result.timed_out:
        return ToolStatus(name, requirement, "error", None, resolved.source,
                          f"{purpose}; --version timed out after {timeout:g}s")
    version = first_line(result.stdout) or first_line(result.stderr)
    if result.returncode != 0:
        return ToolStatus(name, requirement, "error", version, resolved.source,
                          f"{purpose}; --version exited with code {result.returncode}")
    if tool.min_version is not None:
        match = SEMVER.search(version or "")
        if match is None or tuple(int(part) for part in match.groups()) < tool.min_version:
            wanted = ".".join(str(part) for part in tool.min_version)
            return ToolStatus(name, requirement, "too-old", version, resolved.source,
                              f"{purpose}; needs {wanted} or newer")
    if tool.sign_in is not None:
        # Only the exit code and a loggedIn flag are read; the status text is never shown or kept.
        command = f"'{name} {' '.join(tool.sign_in.status_args)}'"
        try:
            login = runner(resolved.argv + tool.sign_in.status_args, timeout=timeout)
        except OSError as exc:
            return ToolStatus(name, requirement, "error", version, resolved.source,
                              f"{purpose}; {command} could not launch: {exc}")
        if login.timed_out:
            return ToolStatus(name, requirement, "error", version, resolved.source, f"{purpose}; {command} timed out")
        if not signed_in(login.returncode, login.stdout):
            return ToolStatus(name, requirement, "not-signed-in", version, resolved.source,
                              f"{purpose}; not signed in, run: {tool.sign_in.login}")
        return ToolStatus(name, requirement, "ok", version, resolved.source, f"{purpose}; signed in")
    return ToolStatus(name, requirement, "ok", version, resolved.source, purpose)


def _check_unlazy(tool: Tool, *, env: Mapping[str, str], home: pathlib.Path,
                  repo_root: pathlib.Path | None) -> ToolStatus:
    folder, reason = find_unlazy_dir(repo_root, env, home)
    if folder is None:
        return ToolStatus(tool.name, tool.requirement, "missing", None, None, f"{tool.purpose}; {reason}")
    version = unlazy_version(folder) or "unknown version"
    return ToolStatus(tool.name, tool.requirement, "ok", version, str(folder), f"{tool.purpose}; {folder}")


def check_tools(*, search_path: str | None, env: Mapping[str, str], home: pathlib.Path,
                repo_root: pathlib.Path | None, platform: str, timeout: float = 10.0,
                resolver: Resolver = resolve_command, runner: Runner = run_command) -> list[ToolStatus]:
    """Check every catalog tool, in catalog order. `platform` only affects the install hints shown later."""
    statuses = []
    for tool in catalog():
        if tool.detect == "python":
            statuses.append(python_status(sys.version_info))
        elif tool.detect == "unlazy":
            statuses.append(_check_unlazy(tool, env=env, home=home, repo_root=repo_root))
        else:
            statuses.append(_check_tool(tool, search_path=search_path, timeout=timeout, resolver=resolver,
                                        runner=runner))
    return statuses


def exit_code(statuses: Sequence[ToolStatus]) -> int:
    return 0 if all(s.status == "ok" for s in statuses if s.requirement == "required") else 1


def _install_texts(name: str, platform: str) -> tuple[list[str], str]:
    os_name = os_name_for(platform)
    for tool in catalog():
        if tool.name == name:
            return [display(argv) for argv in tool.install.get(os_name, ())], tool.url_for(os_name)
    raise KeyError(name)


def render_text(statuses: Sequence[ToolStatus], platform: str) -> str:
    label = PLATFORM_LABELS.get(platform, f"{platform}, not a supported OS")
    rows = [("TOOL", "STATUS", "VERSION", "NOTE")]
    rows += [(s.name, s.status, s.version or "-", s.note) for s in statuses]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = [f"oah doctor ({label})"]
    for row in rows:
        lines.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:3])) + "  " + row[3])
    fixes = [s for s in statuses if s.status in ("missing", "too-old")]
    if fixes:
        lines += ["", "Install the missing tools:"]
        for status in fixes:
            commands, url = _install_texts(status.name, platform)
            optional = "" if status.requirement == "required" else f" ({status.requirement})"
            alternatives = "".join(f"   (or: {command})" for command in commands[1:])
            lines.append(f"  {status.name}{optional}: {commands[0] if commands else 'see'}{alternatives}")
            lines.append(f"      {url}")
    missing_required = sum(1 for s in statuses if s.requirement == "required" and s.status != "ok")
    lines += ["", "DOCTOR_OK" if missing_required == 0 else f"DOCTOR_MISSING required={missing_required}"]
    return "\n".join(lines)


def render_json(statuses: Sequence[ToolStatus], platform: str) -> str:
    rows = []
    for status in statuses:
        commands, url = _install_texts(status.name, platform)
        rows.append(dict(dataclasses.asdict(status), install=commands, url=url))
    payload = {"ok": exit_code(statuses) == 0, "platform": platform, "tools": rows}
    return json.dumps(payload, indent=2, ensure_ascii=True)


def _repo_root(search_path: str | None) -> pathlib.Path | None:
    try:
        git = resolve_command("git", search_path)
    except UnsupportedBatchShim:
        return None
    if git is None:
        return None
    try:
        result = run_command(git.argv + ("rev-parse", "--show-toplevel"), timeout=10)
    except OSError:
        return None
    top = result.stdout.strip()
    return pathlib.Path(top) if result.returncode == 0 and top else None


def doctor_main(*, json_output: bool) -> int:
    """`oah doctor` against the real machine."""
    statuses = check_tools(search_path=None, env=os.environ, home=pathlib.Path.home(),
                           repo_root=_repo_root(None), platform=sys.platform)
    print(render_json(statuses, sys.platform) if json_output else render_text(statuses, sys.platform))
    return exit_code(statuses)
