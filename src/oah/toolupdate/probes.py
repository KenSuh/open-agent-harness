# 도구 업데이트 판독 — CLI 를 argv 로만 실행해 엄격한 UTF-8 로 읽고, 도움말·버전·문구표·플러그인 목록·Codex 플러그인 설치 여부로 결과를 판정한다.
"""What the pass reads from the tools, and the one rule that turns it into a status.

`Cli.run` goes through `engines.proc.run_command` with the pass's timeout; output that is not strict UTF-8
(the decoder had to replace a byte) marks the read unclean, and an unclean read never supplies a version or
an "up to date" answer. A command is used only when the CLI's own `--help` lists it (`listed_commands`).

`verdict` is AC5: `updated` only when the version read after is readable and differs from the one read
before (whatever the exit code); `current` only when the tool said it is up to date, the exit code is 0
and the version is readable and unchanged; otherwise `failed` (a timeout, or a non-zero exit) or
`unverified`.

PHRASES is the one table of answers the CLIs print; each entry has a test. Claude Code plugin updates
answer in JSON (`updateOutcome`, `refreshFailed`, `failureCode`, `shownCommand`) and are read from fields.

Codex plugins are only noticed from the installed entries in config.toml, read-only. No Codex plugin
or marketplace command is run and no hook approval is read or stored. Claude marketplace revisions
come from the installed clones named by known_marketplaces.json.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import tomllib
from typing import Callable, Mapping

from oah.engines import claude as claude_engine
from oah.engines import codex as codex_engine
from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command
from oah.toolupdate import records

REPLACEMENT = "�"
PLUGIN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}@[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,99}")  # anything else is not a readable version
# Claude Code lists these pseudo-marketplaces for plugins that `claude plugin update` cannot update.
SPECIAL_MARKETPLACES = frozenset({"inline", "skills-dir", "synced", "builtin"})
# `claude plugin update --json` keeps `shownCommand` for these failures: a person must confirm the command.
CONFIRMATION_CODES = frozenset({"command_source_refused", "command_source_declined", "entry_helper_unconfirmed",
                                "entry_helper_declined"})
PLUGIN_CURRENT_OUTCOME = "up_to_date"  # `updateOutcome` of a plugin that is already at its latest version

PHRASES = {
    # `claude update` (Claude Code 2.1.316 prints "Claude is up to date!"; older builds named Claude Code).
    "claude-current": re.compile(r"^Claude(?: Code)? is up to date\b", re.M),
    # `codex update` runs the installer it was installed with; Homebrew and npm say so in their own words.
    "codex-current-homebrew": re.compile(r"the latest version is already installed"),
    "codex-current-npm": re.compile(r"^up to date, audited\b", re.M),
    "claude-marketplaces-current": re.compile(r"^All marketplaces are (?:already )?up to date[.!]?$", re.M),
    # `claude plugin update` without --json, or with a text error: a marketplace-declared command waits.
    "claude-plugin-confirmation": re.compile(r"marketplace-declared command"),
}
CURRENT_PHRASES = {"claude": ("claude-current",), "codex": ("codex-current-homebrew", "codex-current-npm")}


def said(name: str, text: str) -> bool:
    return PHRASES[name].search(text) is not None


def verdict(before: str | None, after: str | None, *, said_current: bool, returncode: int | None,
            timed_out: bool, clean: bool = True) -> str:
    """AC5: the version read after decides `updated`; the tool's own answer plus an unchanged version, `current`."""
    if timed_out:
        return "failed"
    if not clean:
        return "unverified"
    if before is not None and after is not None and after != before:
        return "updated"
    if returncode != 0:
        return "failed"
    if said_current and before is not None and after is not None:
        return "current"
    return "unverified"


# Running a CLI -------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Ran:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    problem: str = ""

    @property
    def clean(self) -> bool:
        """Both streams were strict UTF-8; otherwise nothing in them is trusted."""
        return REPLACEMENT not in self.stdout and REPLACEMENT not in self.stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.clean

    @property
    def text(self) -> str:
        return f"{self.stdout}\n{self.stderr}" if self.clean else ""


def listed_commands(text: str) -> set[str]:
    """The command names (aliases included) in the `Commands:` block of commander or clap help."""
    names: set[str] = set()
    inside, indent = False, None
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.startswith(" ") and line.strip():
            inside, indent = line.strip() in ("Commands:", "Subcommands:"), None
            continue
        if not inside or not line.strip():
            if inside and names:
                inside = False  # a blank line ends the block
            continue
        width = len(line) - len(line.lstrip(" "))
        indent = width if indent is None else indent
        if width == indent:
            names.update(name for name in line.split()[0].split("|") if name)
    return names


def lists_flag(text: str, flag: str) -> bool:
    """True when an option line names `flag`; commander indents those by 2 spaces and clap by 2 or 6, while a
    wrapped description that merely mentions the flag is indented further."""
    return re.search(rf"(?m)^ {{2,6}}(?:-[A-Za-z], )?{re.escape(flag)}\b", text or "") is not None


@dataclasses.dataclass(frozen=True)
class Capability:
    """Listed, absent from clean help, or unknown with the reason the help could not be read."""

    listed: bool | None
    reason: str = ""
    timed_out: bool = False


class Cli:
    """One CLI resolved once on the pass's PATH; every command it runs is an argv list with a timeout."""

    def __init__(self, name: str, label: str, *, env: Mapping[str, str], timeout: float, cwd: pathlib.Path) -> None:
        self.name, self.label = name, label
        self.env, self.timeout, self.cwd = env, timeout, cwd
        self.base: tuple[str, ...] | None = None
        self.problem = ""
        self._help: dict[tuple[str, ...], Ran] = {}
        try:
            found = resolve_command(name, env.get("PATH", ""))
        except UnsupportedBatchShim:
            self.problem = (f"{label} is installed as a batch file oah does not run; install its native executable "
                            "(oah doctor shows how), then run oah update")
            return
        if found is None:
            self.problem = f"{label} is not installed; install it (oah doctor prints the command), then run oah update"
            return
        self.base = found.argv

    def run(self, *args: str) -> Ran:
        assert self.base is not None
        result = run_command([*self.base, *args], timeout=self.timeout, cwd=str(self.cwd), env=self.env)
        return Ran(result.returncode, result.stdout, result.stderr, result.timed_out)

    def version(self) -> str | None:
        """The installed version from `<cli> --version`, read with the existing version parsers."""
        ran = self.run("--version")
        if not ran.ok:
            return None
        parse = codex_engine.parse_version if self.name == "codex" else claude_engine.parse_version
        return parse(ran.stdout)

    def help(self, *path: str) -> Ran:
        if path not in self._help:
            try:
                self._help[path] = self.run(*path, "--help")
            except OSError as exc:
                self._help[path] = Ran(None, "", "", False, type(exc).__name__)
        return self._help[path]

    def _probe_help(self, path: tuple[str, ...], matches: Callable[[str], bool]) -> Capability:
        ran = self.help(*path)
        command = " ".join((self.name, *path, "--help"))
        step = f"run {command} yourself to check support, then run oah update"
        if ran.timed_out:
            return Capability(None, f"help timed out; {step}", timed_out=True)
        if not ran.clean:
            return Capability(None, f"help was not clean UTF-8; {step}")
        if ran.problem:
            return Capability(None, f"help could not be read ({ran.problem}); {step}")
        if not ran.ok:
            return Capability(None, f"help could not be read (exit {ran.returncode}); {step}")
        return Capability(matches(ran.stdout))

    def lists(self, *path: str) -> Capability:
        """Whether clean `<cli> <parent> --help` lists the last word, or why that is unknown."""
        return self._probe_help(path[:-1], lambda text: path[-1] in listed_commands(text))

    def has_flag(self, path: tuple[str, ...], flag: str) -> Capability:
        return self._probe_help(path, lambda text: lists_flag(text, flag))


# Claude Code plugins -------------------------------------------------------------------------------

def user_plugins(stdout: str) -> dict[str, str | None] | None:
    """`claude plugin list --json`: plugins installed at user scope, id -> version (None when unknown)."""
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    if isinstance(data, dict):
        data = data.get("installed")
    if not isinstance(data, list):
        return None
    found: dict[str, str | None] = {}
    for entry in data:
        if not isinstance(entry, dict) or entry.get("scope") != "user":
            continue
        ident = entry.get("id")
        if (not isinstance(ident, str) or not PLUGIN_ID_RE.fullmatch(ident)
                or ident.rpartition("@")[2] in SPECIAL_MARKETPLACES):
            continue
        version = entry.get("version")
        readable = isinstance(version, str) and VERSION_RE.fullmatch(version) and version != "unknown"
        found[ident] = version if readable else None
    return found


def plugin_result(stdout: str) -> dict | None:
    """The one JSON result line `claude plugin update --json` prints for an update."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict) and data.get("command") == "update":
            return data
    return None


def plugin_said_current(result: Mapping | None) -> bool:
    return (result is not None and result.get("outcome") == "ok"
            and result.get("updateOutcome") == PLUGIN_CURRENT_OUTCOME and not result.get("refreshFailed"))


def plugin_needs_confirmation(result: Mapping | None, text: str) -> bool:
    if result is not None and result.get("outcome") == "failed" and (
            result.get("shownCommand") is not None or result.get("failureCode") in CONFIRMATION_CODES):
        return True
    return said("claude-plugin-confirmation", text)


# Installed marketplace revisions and the read-only Codex plugin notice ------------------------------

def claude_marketplace_version(env: Mapping[str, str]) -> str | None:
    """Digest the installed Git revisions; missing, unreadable or non-Git sources stay unverified."""
    config = pathlib.Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else records.home() / ".claude"
    try:
        data = json.loads((config / "plugins" / "known_marketplaces.json").read_bytes().decode("utf-8-sig"))
        if not isinstance(data, dict):
            return None
        revisions = []
        for name, entry in sorted(data.items()):
            if not isinstance(entry, dict) or not isinstance(entry.get("installLocation"), str):
                return None
            git = pathlib.Path(entry["installLocation"]) / ".git"
            revision = (git / "HEAD").read_text(encoding="utf-8").strip()
            if revision.startswith("ref: "):
                ref = pathlib.PurePosixPath(revision[5:])
                if ref.is_absolute() or ".." in ref.parts or not ref.parts or ref.parts[0] != "refs":
                    return None
                try:
                    revision = git.joinpath(*ref.parts).read_text(encoding="utf-8").strip()
                except FileNotFoundError:
                    packed = (git / "packed-refs").read_text(encoding="utf-8").splitlines()
                    revision = next((line.split()[0] for line in packed
                                     if len(line.split()) == 2 and line.split()[1] == str(ref)), "")
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) is None:
                return None
            revisions.append(f"{name}={revision}")
        return "sha256:" + hashlib.sha256("\n".join(revisions).encode("utf-8")).hexdigest()
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def codex_home(env: Mapping[str, str]) -> pathlib.Path:
    """CODEX_HOME as engines.codex resolves it, with this package's home function."""
    override = env.get(codex_engine.CODEX_HOME_ENV)
    return pathlib.Path(override) if override else records.home() / ".codex"


def codex_plugins_installed(env: Mapping[str, str]) -> bool | None:
    """Installed [plugins."name@marketplace"] entries; unreadable means unknown, which also needs the notice."""
    path = codex_home(env) / "config.toml"
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict) or any(not PLUGIN_ID_RE.fullmatch(name) or not isinstance(entry, dict)
                                            for name, entry in plugins.items()):
        return None
    return bool(plugins)
