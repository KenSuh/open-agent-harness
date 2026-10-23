# 도구 목록 — oah 가 쓰는 필수·권장 도구마다 확인 명령, OS 별 공식 설치 명령, 공식 주소를 한곳에 둔다.
"""The single list of tools oah needs or recommends.

`oah doctor`, the guided setup (`python -m oah.setup`) and `oah init` all read this list. Install
commands are the official ones from each tool's own page (PLAN 6.1), in preference order: the first
alternative whose program exists on this machine is offered. A shell one-liner is stored as an explicit
interpreter argv so it runs without `shell=True`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

Argv = tuple[str, ...]

POWERSHELL_PREFIX: Argv = ("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command")
SH_PREFIX: Argv = ("/bin/sh", "-c")
# Words that PowerShell itself provides, so a one-liner starting with them needs nothing else.
POWERSHELL_BUILTINS = ("irm", "iex", "invoke-restmethod", "invoke-expression")

URL_GIT = "https://git-scm.com/install/mac"
URL_GIT_WIN = "https://git-scm.com/install/windows"
URL_PYTHON = "https://www.python.org/downloads/"
URL_UV = "https://docs.astral.sh/uv/getting-started/installation/"
URL_NODE = "https://nodejs.org/en/download"
URL_CLAUDE = "https://code.claude.com/docs/en/setup"
URL_CODEX = "https://github.com/openai/codex"
URL_UNLAZY = "https://github.com/Leonxlnx/unlazy"
URL_GRAPHIFY = "https://docs.graphify.com/installation"
URL_GH = "https://cli.github.com"
URL_GITLEAKS = "https://github.com/gitleaks/gitleaks"
URL_JQ = "https://jqlang.org/download/"
URL_RG = "https://github.com/BurntSushi/ripgrep#installation"

UNLAZY_COMMAND: Argv = ("oah", "init", "--with-unlazy")


def sh(text: str) -> Argv:
    """A POSIX shell one-liner, run by /bin/sh with the text as one argument."""
    return SH_PREFIX + (text,)


def powershell(text: str) -> Argv:
    """A PowerShell one-liner, run by Windows PowerShell with the text as one argument."""
    return POWERSHELL_PREFIX + (text,)


def _wrapped_text(argv: Argv) -> str | None:
    if argv[:2] == SH_PREFIX and len(argv) == 3:
        return argv[2]
    if argv[:len(POWERSHELL_PREFIX)] == POWERSHELL_PREFIX and len(argv) == len(POWERSHELL_PREFIX) + 1:
        return argv[-1]
    return None


def display(argv: Argv) -> str:
    """The command as the user should see it: a one-liner's own text, or the argv joined by spaces."""
    text = _wrapped_text(argv)
    return text if text is not None else " ".join(argv)


def needs(argv: Argv) -> str:
    """The program that must exist for this command: the first word of a one-liner, else argv[0]."""
    text = _wrapped_text(argv)
    if text is None:
        return argv[0]
    first = text.split()[0] if text.split() else argv[0]
    if argv[0] == "powershell" and first.lower() in POWERSHELL_BUILTINS:
        return "powershell"
    return first


def is_one_liner(argv: Argv) -> bool:
    return _wrapped_text(argv) is not None


@dataclass(frozen=True)
class SignIn:
    status_args: Argv  # appended to the tool's own executable; exit code 0 means signed in
    login: str  # the exact command the user runs in their own terminal


@dataclass(frozen=True)
class Tool:
    name: str
    required: bool
    purpose: str
    check: Argv  # check[0] is looked up on PATH; empty when `detect` is not "path"
    version_args: Argv
    install: Mapping[str, tuple[Argv, ...]]  # "macos" / "windows" -> alternatives in preference order
    url: str
    os_url: Mapping[str, str] = field(default_factory=dict)
    min_version: tuple[int, int, int] | None = None
    detect: str = "path"  # "path", "python" (the running interpreter) or "unlazy" (skill folder)
    sign_in: SignIn | None = None
    note: Mapping[str, str] = field(default_factory=dict)  # per OS, shown with the question
    required_for_gates: bool = False  # `oah doctor` counts it as required in a project that uses gates

    @property
    def requirement(self) -> str:
        return "required" if self.required else "recommended"

    def url_for(self, os_name: str) -> str:
        return self.os_url.get(os_name, self.url)


def _tool(name: str, required: bool, purpose: str, *, macos: tuple[Argv, ...], windows: tuple[Argv, ...],
          url: str, **extra) -> Tool:
    check = extra.pop("check", (name, "--version"))
    return Tool(name=name, required=required, purpose=purpose, check=check, version_args=("--version",),
                install={"macos": macos, "windows": windows}, url=url, **extra)


def catalog() -> list[Tool]:
    """Every required and recommended tool, in display order."""
    return [
        _tool("git", True, "version control",
              macos=(("brew", "install", "git"), ("xcode-select", "--install")),
              windows=(("winget", "install", "--id", "Git.Git", "-e", "--source", "winget"),),
              url=URL_GIT, os_url={"windows": URL_GIT_WIN},
              note={"macos": "xcode-select opens an install window; finish it, then run this setup again."}),
        _tool("python", True, "runs oah",
              macos=(("uv", "python", "install", "3.13"), ("brew", "install", "python@3.13")),
              windows=(("uv", "python", "install", "3.13"), ("winget", "install", "Python.Python.3.13")),
              url=URL_PYTHON, check=(), detect="python"),
        _tool("uv", False, "installs oah, graphify and Python",
              macos=(sh("curl -LsSf https://astral.sh/uv/install.sh | sh"),),
              windows=(powershell("irm https://astral.sh/uv/install.ps1 | iex"),),
              url=URL_UV),
        _tool("node", False, "runs unlazy's gate checks",
              # PLAN 6.1 names `brew install node@24`; that formula is keg-only and stays off PATH, so
              # the linked `node` formula (the current release, well above unlazy's Node 16) is used.
              macos=(("brew", "install", "node"),),
              windows=(("winget", "install", "OpenJS.NodeJS.LTS"),),
              url=URL_NODE, min_version=(16, 0, 0), required_for_gates=True),
        _tool("claude", True, "initial implementation (Claude Code)",
              macos=(sh("curl -fsSL https://claude.ai/install.sh | bash"),),
              windows=(powershell("irm https://claude.ai/install.ps1 | iex"),
                       ("winget", "install", "Anthropic.ClaudeCode")),
              url=URL_CLAUDE, sign_in=SignIn(("auth", "status"), "claude auth login")),
        _tool("codex", True, "review and correction (Codex CLI)",
              macos=(("brew", "install", "--cask", "codex"), ("npm", "install", "-g", "@openai/codex")),
              windows=(powershell("irm https://chatgpt.com/codex/install.ps1 | iex"),),
              url=URL_CODEX, sign_in=SignIn(("login", "status"), "codex login")),
        _tool("unlazy", False, "gate checks and the Stop hook",
              macos=(), windows=(),
              url=URL_UNLAZY, check=(), detect="unlazy",
              note={os_name: f"Run {display(UNLAZY_COMMAND)} after lane P5 lands to install unlazy at P5's "
                             "contract-tested pin, verified by commit and scripts digest. "
                             "This guided setup does not install it."
                    for os_name in ("macos", "windows")}),
        _tool("graphify", False, "knowledge graph",
              macos=(("uv", "tool", "install", "graphifyy"),), windows=(("uv", "tool", "install", "graphifyy"),),
              url=URL_GRAPHIFY),
        _tool("gh", False, "pull requests from the terminal",
              macos=(("brew", "install", "gh"),),
              windows=(("winget", "install", "--id", "GitHub.cli", "--source", "winget"),),
              url=URL_GH),
        _tool("gitleaks", False, "secret scan before push",
              macos=(("brew", "install", "gitleaks"),), windows=(powershell("scoop install gitleaks"),),
              url=URL_GITLEAKS),
        _tool("jq", False, "reads JSON output in scripts",
              macos=(("brew", "install", "jq"),), windows=(("winget", "install", "jqlang.jq"),),
              url=URL_JQ),
        _tool("rg", False, "fast code search (ripgrep)",
              macos=(("brew", "install", "ripgrep"),), windows=(("winget", "install", "BurntSushi.ripgrep.MSVC"),),
              url=URL_RG),
    ]


def os_name_for(platform: str) -> str:
    """'windows' on win32; 'macos' everywhere else (Linux is not supported but gets the macOS commands)."""
    return "windows" if platform == "win32" else "macos"
