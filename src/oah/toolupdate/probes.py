# 도구 업데이트 판독 — CLI 를 argv 로만 실행해 엄격한 UTF-8 로 읽고, 도움말·버전·문구표·플러그인 목록·Codex 설정과 플러그인 지문으로 결과를 판정한다.
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

Codex: Git marketplaces and installed plugins come from `$CODEX_HOME/config.toml` (`[marketplaces.<name>]`
with `source_type` and `last_revision`; `[plugins."<name>@<marketplace>"]`), because the CLI's own list
output reports no revision. Each installed plugin's files under `plugins/cache/<marketplace>/<name>/`
are digested read-only, without following links; oah never computes or writes Codex's hook approval.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import stat
import tomllib
from typing import Mapping

from oah.engines import claude as claude_engine
from oah.engines import codex as codex_engine
from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command
from oah.toolupdate import records

REPLACEMENT = "�"
PLUGIN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}@[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
REVISION_RE = re.compile(r"[0-9A-Za-z._-]{1,128}")
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
    # `codex plugin marketplace upgrade` (codex-cli 0.166.0).
    "codex-marketplace-current": re.compile(r"Marketplace `?(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)`? is already up to date"),
    "codex-marketplaces-current": re.compile(r"All configured Git marketplaces are already up to date|"
                                             r"marketplaces; all are already up to date"),
    # `claude plugin update` without --json, or with a text error: a marketplace-declared command waits.
    "claude-plugin-confirmation": re.compile(r"marketplace-declared command"),
}
CURRENT_PHRASES = {"claude": ("claude-current",), "codex": ("codex-current-homebrew", "codex-current-npm")}


def said(name: str, text: str) -> bool:
    return PHRASES[name].search(text) is not None


def verdict(before: str | None, after: str | None, *, said_current: bool, returncode: int | None,
            timed_out: bool) -> str:
    """AC5: the version read after decides `updated`; the tool's own answer plus an unchanged version, `current`."""
    if timed_out:
        return "failed"
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
    return re.search(rf"(?m)^\s+(?:-[A-Za-z], )?{re.escape(flag)}\b", text or "") is not None


class Cli:
    """One CLI resolved once on the pass's PATH; every command it runs is an argv list with a timeout."""

    def __init__(self, name: str, label: str, *, env: Mapping[str, str], timeout: float, cwd: pathlib.Path) -> None:
        self.name, self.label = name, label
        self.env, self.timeout, self.cwd = env, timeout, cwd
        self.base: tuple[str, ...] | None = None
        self.problem = ""
        self._help: dict[tuple[str, ...], str | None] = {}
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

    def help(self, *path: str) -> str | None:
        if path not in self._help:
            ran = self.run(*path, "--help")
            self._help[path] = ran.stdout if ran.ok else None
        return self._help[path]

    def lists(self, *path: str) -> bool:
        """True when `<cli> <parent> --help` lists the last word of `path` as a command."""
        text = self.help(*path[:-1])
        return text is not None and path[-1] in listed_commands(text)

    def has_flag(self, path: tuple[str, ...], flag: str) -> bool:
        text = self.help(*path)
        return text is not None and lists_flag(text, flag)


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


# Codex ---------------------------------------------------------------------------------------------

class CodexUnreadable(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class CodexView:
    marketplaces: dict[str, str | None]  # Git marketplace -> last_revision, None when none is recorded
    plugins: dict[str, pathlib.Path]     # installed plugin id -> its cache folder


def codex_home(env: Mapping[str, str]) -> pathlib.Path:
    """CODEX_HOME as engines.codex resolves it, with this package's home function."""
    override = env.get(codex_engine.CODEX_HOME_ENV)
    return pathlib.Path(override) if override else records.home() / ".codex"


def read_codex(home: pathlib.Path) -> CodexView:
    """Git marketplaces and installed plugins from Codex's own config.toml; raises CodexUnreadable."""
    try:
        data = tomllib.loads((home / "config.toml").read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        data = {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise CodexUnreadable() from None
    marketplaces, plugins = data.get("marketplaces", {}), data.get("plugins", {})
    if not isinstance(marketplaces, dict) or not isinstance(plugins, dict):
        raise CodexUnreadable()
    found: dict[str, str | None] = {}
    for name, entry in marketplaces.items():
        if not isinstance(entry, dict) or not NAME_RE.fullmatch(name) or entry.get("source_type") != "git":
            continue
        revision = entry.get("last_revision")
        found[name] = revision if isinstance(revision, str) and REVISION_RE.fullmatch(revision) else None
    installed = {}
    for ident in plugins:
        if PLUGIN_ID_RE.fullmatch(ident):
            name, _, market = ident.rpartition("@")
            installed[ident] = home / "plugins" / "cache" / market / name
    return CodexView(found, installed)


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def plugin_digest(folder: pathlib.Path) -> str | None:
    """A digest of every name and byte under a plugin's cache folder, links recorded but never followed."""
    digest = hashlib.sha256()
    pending = [(folder, "")]
    try:
        if not folder.is_dir():
            return None
        while pending:
            current, relative = pending.pop()
            with os.scandir(current) as scan:
                entries = sorted(scan, key=lambda item: item.name)
            for entry in entries:
                name = f"{relative}/{entry.name}" if relative else entry.name
                key = name.encode("utf-8", "surrogateescape")
                if _is_link(entry):
                    target = os.readlink(entry.path) if entry.is_symlink() else ""
                    digest.update(b"L\0" + key + b"\0" + target.encode("utf-8", "surrogateescape") + b"\0")
                elif entry.is_dir(follow_symlinks=False):
                    digest.update(b"D\0" + key + b"\0")
                    pending.append((pathlib.Path(entry.path), name))
                elif entry.is_file(follow_symlinks=False):
                    content = hashlib.sha256()
                    with open(entry.path, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1 << 20), b""):
                            content.update(chunk)
                    digest.update(b"F\0" + key + b"\0" + content.digest())
                else:
                    digest.update(b"O\0" + key + b"\0")
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


HOOK_FILES = ("hooks/hooks.json", "hooks.json")
MANIFESTS = (".codex-plugin/plugin.json", ".claude-plugin/plugin.json", "plugin.json")


def declares_hooks(folder: pathlib.Path) -> bool:
    """A hooks file, or a `hooks` entry in a plugin manifest, in any version folder; unreadable counts as yes."""
    try:
        versions = sorted(path for path in folder.iterdir() if path.is_dir())
        for version in versions:
            if any((version / relative).is_file() for relative in HOOK_FILES):
                return True
            for relative in MANIFESTS:
                manifest = version / relative
                if not manifest.is_file():
                    continue
                try:
                    data = json.loads(manifest.read_bytes().decode("utf-8-sig"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    return True
                if not isinstance(data, dict) or data.get("hooks"):
                    return True
    except OSError:
        return True
    return False


def marketplace_said_current(text: str, name: str) -> bool:
    if said("codex-marketplaces-current", text):
        return True
    return any(match.group("name") == name for match in PHRASES["codex-marketplace-current"].finditer(text))
