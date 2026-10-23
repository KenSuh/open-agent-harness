# oah doctor — 필수·권장·선택 도구가 설치됐는지 확인하고, 빠진 도구는 이 OS 의 공식 설치 명령으로 안내한다.
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim, resolve_command, run_command
from oah.unlazy import INSTALL_HINT as UNLAZY_INSTALL, find_unlazy_dir, unlazy_version

MIN_PYTHON = (3, 11)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

URL_GIT_MAC = "https://git-scm.com/install/mac"
URL_GIT_WIN = "https://git-scm.com/install/windows"
URL_UV = "https://docs.astral.sh/uv/getting-started/installation/"
URL_NODE = "https://nodejs.org/en/download"
URL_CLAUDE = "https://code.claude.com/docs/en/setup"
URL_CODEX = "https://github.com/openai/codex"
URL_UNLAZY = "https://github.com/Leonxlnx/unlazy"
URL_GRAPHIFY = "https://docs.graphify.com/installation"
URL_GH = "https://cli.github.com"
URL_GITLEAKS = "https://github.com/gitleaks/gitleaks"
URL_PYTHON = "https://www.python.org/downloads/"

PLATFORM_LABELS = {"darwin": "macOS", "win32": "Windows"}


@dataclass(frozen=True)
class InstallHint:
    command: str
    url: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    requirement: str  # "required", "recommended" or "optional"
    purpose: str
    version_args: tuple[str, ...] = ("--version",)
    min_version: tuple[int, int, int] | None = None
    hints: Mapping[str, InstallHint] = field(default_factory=dict)


@dataclass
class ToolStatus:
    name: str
    requirement: str
    status: str  # ok, missing, too-old, not-signed-in, unsupported-shim or error
    version: str | None
    source: str | None
    note: str


def _same_everywhere(command: str, url: str) -> dict[str, InstallHint]:
    return {"darwin": InstallHint(command, url), "win32": InstallHint(command, url)}


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("git", "required", "version control", hints={
        "darwin": InstallHint("xcode-select --install   (or: brew install git)", URL_GIT_MAC),
        "win32": InstallHint("winget install --id Git.Git -e --source winget", URL_GIT_WIN)}),
    ToolSpec("uv", "recommended", "installs oah and graphify", hints={
        "darwin": InstallHint("curl -LsSf https://astral.sh/uv/install.sh | sh", URL_UV),
        "win32": InstallHint('powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"',
                             URL_UV)}),
    ToolSpec("node", "required", "runs unlazy's gate checks", min_version=(16, 0, 0), hints={
        "darwin": InstallHint("brew install node@24", URL_NODE),
        "win32": InstallHint("winget install OpenJS.NodeJS.LTS", URL_NODE)}),
    ToolSpec("claude", "required", "initial implementation", hints={
        "darwin": InstallHint("curl -fsSL https://claude.ai/install.sh | bash", URL_CLAUDE),
        "win32": InstallHint("irm https://claude.ai/install.ps1 | iex", URL_CLAUDE)}),
    ToolSpec("codex", "required", "review and correction", hints={
        "darwin": InstallHint("brew install --cask codex", URL_CODEX),
        "win32": InstallHint('powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"',
                             URL_CODEX)}),
    ToolSpec("graphify", "optional", "knowledge graph", hints=_same_everywhere("uv tool install graphifyy", URL_GRAPHIFY)),
    ToolSpec("gh", "optional", "pull requests from the terminal", hints={
        "darwin": InstallHint("brew install gh", URL_GH),
        "win32": InstallHint("winget install --id GitHub.cli --source winget", URL_GH)}),
    ToolSpec("gitleaks", "optional", "secret scan before push", hints={
        "darwin": InstallHint("brew install gitleaks", URL_GITLEAKS),
        "win32": InstallHint("scoop install gitleaks", URL_GITLEAKS)}),
)

UNLAZY = ToolSpec("unlazy", "required", "gate checks and the Stop hook",
                  hints=_same_everywhere(UNLAZY_INSTALL, URL_UNLAZY))
PYTHON = ToolSpec("python", "required", "runs oah", hints=_same_everywhere("uv python install 3.13", URL_PYTHON))

# Display order: python right after git, unlazy right after codex.
ORDER = ("git", "python", "uv", "node", "claude", "codex", "unlazy", "graphify", "gh", "gitleaks")

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


def _check_tool(spec: ToolSpec, *, search_path: str | None, timeout: float, resolver: Resolver,
                runner: Runner) -> ToolStatus:
    try:
        resolved = resolver(spec.name, search_path)
    except UnsupportedBatchShim as exc:
        return ToolStatus(spec.name, spec.requirement, "unsupported-shim", None, exc.path, str(exc))
    if resolved is None:
        return ToolStatus(spec.name, spec.requirement, "missing", None, None, spec.purpose)
    result = runner(resolved.argv + spec.version_args, timeout=timeout)
    if result.timed_out:
        return ToolStatus(spec.name, spec.requirement, "error", None, resolved.source,
                          f"{spec.purpose}; --version timed out after {timeout:g}s")
    version = first_line(result.stdout) or first_line(result.stderr)
    if result.returncode != 0:
        return ToolStatus(spec.name, spec.requirement, "error", version, resolved.source,
                          f"{spec.purpose}; --version exited with code {result.returncode}")
    if spec.min_version is not None:
        match = SEMVER.search(version or "")
        if match is None or tuple(int(part) for part in match.groups()) < spec.min_version:
            wanted = ".".join(str(part) for part in spec.min_version)
            return ToolStatus(spec.name, spec.requirement, "too-old", version, resolved.source,
                              f"{spec.purpose}; needs {wanted} or newer")
    if spec.name == "codex":
        login = runner(resolved.argv + ("login", "status"), timeout=timeout)
        if login.timed_out:
            return ToolStatus(spec.name, spec.requirement, "error", version, resolved.source,
                              f"{spec.purpose}; 'codex login status' timed out")
        if login.returncode != 0:
            return ToolStatus(spec.name, spec.requirement, "not-signed-in", version, resolved.source,
                              f"{spec.purpose}; not signed in, run: codex login")
        return ToolStatus(spec.name, spec.requirement, "ok", version, resolved.source, f"{spec.purpose}; signed in")
    return ToolStatus(spec.name, spec.requirement, "ok", version, resolved.source, spec.purpose)


def _check_unlazy(*, env: Mapping[str, str], home: pathlib.Path, repo_root: pathlib.Path | None) -> ToolStatus:
    folder, reason = find_unlazy_dir(repo_root, env, home)
    if folder is None:
        return ToolStatus(UNLAZY.name, UNLAZY.requirement, "missing", None, None, f"{UNLAZY.purpose}; {reason}")
    version = unlazy_version(folder) or "unknown version"
    return ToolStatus(UNLAZY.name, UNLAZY.requirement, "ok", version, str(folder), f"{UNLAZY.purpose}; {folder}")


def check_tools(*, search_path: str | None, env: Mapping[str, str], home: pathlib.Path,
                repo_root: pathlib.Path | None, platform: str, timeout: float = 10.0,
                resolver: Resolver = resolve_command, runner: Runner = run_command) -> list[ToolStatus]:
    """Check every tool oah uses. `platform` only affects the install hints shown later."""
    found: dict[str, ToolStatus] = {}
    for spec in TOOLS:
        found[spec.name] = _check_tool(spec, search_path=search_path, timeout=timeout, resolver=resolver,
                                       runner=runner)
    found["python"] = python_status(sys.version_info)
    found["unlazy"] = _check_unlazy(env=env, home=home, repo_root=repo_root)
    return [found[name] for name in ORDER]


def exit_code(statuses: Sequence[ToolStatus]) -> int:
    return 0 if all(s.status == "ok" for s in statuses if s.requirement == "required") else 1


def _spec(name: str) -> ToolSpec:
    for spec in TOOLS + (UNLAZY, PYTHON):
        if spec.name == name:
            return spec
    raise KeyError(name)


def _hint(spec: ToolSpec, platform: str) -> InstallHint | None:
    return spec.hints.get(platform) or spec.hints.get("darwin")


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
            hint = _hint(_spec(status.name), platform)
            if hint is None:
                continue
            optional = "" if status.requirement == "required" else f" ({status.requirement})"
            lines.append(f"  {status.name}{optional}: {hint.command}")
            lines.append(f"      {hint.url}")
    missing_required = sum(1 for s in statuses if s.requirement == "required" and s.status != "ok")
    lines += ["", "DOCTOR_OK" if missing_required == 0 else f"DOCTOR_MISSING required={missing_required}"]
    return "\n".join(lines)


def render_json(statuses: Sequence[ToolStatus], platform: str) -> str:
    payload = {"ok": exit_code(statuses) == 0, "platform": platform,
               "tools": [dataclasses.asdict(s) for s in statuses]}
    return json.dumps(payload, indent=2, ensure_ascii=True)


def _repo_root(search_path: str | None) -> pathlib.Path | None:
    try:
        git = resolve_command("git", search_path)
    except UnsupportedBatchShim:
        return None
    if git is None:
        return None
    result = run_command(git.argv + ("rev-parse", "--show-toplevel"), timeout=10)
    top = result.stdout.strip()
    return pathlib.Path(top) if result.returncode == 0 and top else None


def doctor_main(*, json_output: bool) -> int:
    """`oah doctor` against the real machine."""
    statuses = check_tools(search_path=None, env=os.environ, home=pathlib.Path.home(),
                           repo_root=_repo_root(None), platform=sys.platform)
    print(render_json(statuses, sys.platform) if json_output else render_text(statuses, sys.platform))
    return exit_code(statuses)
