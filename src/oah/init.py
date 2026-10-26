# 안내형 oah init 의 핵심 — 프로젝트 환경(설정·템플릿·위키·티어 에이전트·.graphifyignore·.gitignore 등)을 데이터로 계획하고, 보여 준 계획 그대로만 쓴다.
"""Guided `oah init` (PLAN 6.5, 7): plan the project environment as data, then write exactly that plan.

Every planned file is a `FileChange`: `create` (missing), `replace` (differs, and `--force` was given),
`update` (managed content added in place: missing `.gitignore` entries, the guide's index line), `keep`
(exists and stays as it is), `unchanged` (already what init would write) or `blocked` (a link is on its
path: nothing is written through a link, which could reach a file outside the project). The wiki's
`index.md` and `log.md` are content: they are never replaced, not even with `--force`. A file that changed
after the plan was shown is skipped with `OAH_INIT_CHANGED`. A blocked file, a refused hook or a skipped
CI starter leaves the run incomplete. Settings are parameters, so this module never reads the config.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import errno
import fnmatch
import hashlib
import importlib
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import sys
import time
from contextlib import ExitStack, contextmanager, nullcontext
from typing import Callable, Mapping, NamedTuple

from oah import call, ci, hooks, intent, models, rerank, routing, rules, skillsel, unlazy
from oah.atomic import StateCorrupt, StorageError
from oah.config import Config, ConfigError, parse_config
from oah.engines import proc
from oah.engines.effort import EngineError
from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command
from oah.impact import mapstore
from oah.impact.errors import ImpactError
from oah.journal import PendingTransaction
from oah.lockfile import LockTimeout, StateLock, _try_lock
from oah.paths import GitMissing, NotARepo, git_argv, repo_info, run_git, state_dir
from oah.setup import companions, guide
from oah.setup.tools import Tool, catalog, display, os_name_for
from oah.wiki import notes
from oah.wiki.base import DEFAULTS as WIKI_DEFAULTS, WikiError, WikiSettings
from oah.wiki.log import parse_log
from oah.wiki.markdown import eol_of
from oah.wiki.store import Wiki

CONFIG_PATH = ".oah/config.toml"
TEMPLATES_DIR = ".oah/templates"
AGENTS_DIR = ".claude/agents"
GRAPHIFYIGNORE = ".graphifyignore"
GITIGNORE = ".gitignore"
UNLAZY_PROJECT = ".agents/skills/unlazy"
WIKI_CONTENT = ("index.md", "log.md")  # the wiki's own content: kept, never replaced
GUIDE_SLUG, GUIDE_TITLE = "wiki-guide", "How this wiki works"
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69
WRITES = ("create", "replace", "update")
KEEP_NOTES = {CONFIG_PATH: "your settings stay as they are; --force rewrites them with oah's defaults"}
TIER_WORK = {
    "low": "quick lookups, docs and short summaries",
    "medium": "routine implementation, refactors, research and reviews of one area",
    "high": "broad audits, migrations, security work, high-risk reviews and reasoning across many files",
    "max": "critical-risk work and the hardest reasoning",
}
NETWORK_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # compatibility; not inherited by Git
UNLAZY_GIT_OPTIONS = ("-c", "user.name=oah", "-c", "user.email=oah@invalid",
                      "-c", "core.logAllRefUpdates=false", "-c", "init.templateDir=",
                      "-c", "core.hooksPath=.git/oah-no-hooks")
CLONE_TIMEOUT = 600.0
RENAME_RETRY_SECONDS = 2.0
CLAUDE_SETTINGS = ".claude/settings.local.json"
LINK_REPARSE_TAGS = (getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
                     getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003))  # Windows symlinks and junctions
MAP_FAILURES = (ImpactError, ConfigError, LockTimeout, StateCorrupt, StorageError, GitMissing, NotARepo,
                PendingTransaction, OSError)
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_PLAIN = re.compile(r"[A-Za-z0-9._-]+")
_GUIDE_LINK = re.compile(r"\[\[" + GUIDE_SLUG + r"(?:[|#\]])")
_WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])\Z", re.IGNORECASE)


class InitError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass(frozen=True)
class FileChange:
    path: str  # repository-relative, POSIX
    action: str  # create | replace | update | keep | unchanged | blocked
    group: str  # config | templates | wiki | agents | graph | gitignore
    data: bytes | None = None  # the bytes to write, for create, replace and update
    before: bytes | None = None  # the bytes found while planning
    note: str = ""


# Questions -------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Question:
    key: str
    title: str
    what: str
    prompt: str
    default: bool | None  # what Enter means; None: the user must type y or n
    recorded: bool = False  # the answer lives in .oah/config.toml, so it is asked only when init writes that file

    @property
    def flag_yes(self) -> str:
        return f"--with-{self.key.replace('_', '-')}"

    @property
    def flag_no(self) -> str:
        return f"--without-{self.key.replace('_', '-')}"


@dataclasses.dataclass(frozen=True)
class Answer:
    value: bool
    source: str  # flag | asked | config | default


QUESTIONS = (
    Question("agents", "Tier agents", "Writes .claude/agents/oah-low, -medium, -high and -max.md: Claude Code "
             "subagents whose model and effort follow the difficulty tier in .oah/config.toml.",
             "Add the tier agents?", True),
    Question("hooks", "Hooks", "Adds oah's hooks: the Claude Code Stop and SessionStart hooks in "
             ".claude/settings.local.json, and the git pre-commit, post-commit, post-merge and pre-push hooks. "
             "The plan shows each change first.", "Install the hooks?", True),
    Question("unlazy", "unlazy, the gate checker", "Downloads unlazy from GitHub into .agents/skills/unlazy at the "
             "commit oah is tested with, and checks its commit and file digests. Gates need it; it runs on "
             "Node.js.", "Install unlazy?", True),
    Question("graph", "Knowledge graph", "Writes .graphifyignore for graphify (install graphify with "
             "`uv tool install graphifyy`); build the graph later with `oah graph update`.",
             "Write .graphifyignore?", False),
    Question("ci", "CI starter", "Writes .github/workflows/oah-ci.yml: your tests on GitHub Actions plus a secret "
             "scan, shown in full first. Actions minutes on a private repository can cost money.",
             "Write the CI starter?", False),
    Question("orca", "Orca (optional app)", "Orca runs coding agents side by side in isolated git worktrees. "
             "Installs the app from its official source and asks before each setup step.", "Set up Orca?", False),
    Question("aside", "Aside (optional app)", "Aside is a browser with an AI agent that Claude Code and Codex use "
             "for web tasks through MCP. Installs it from its official source and asks before each setup step.",
             "Set up Aside?", False),
    Question("jev", "TypeSafe Jev re-ranker (recommended)", "Jev re-orders the skills and impact results that oah's "
             "fixed rules chose; it never adds or drops one. Cost: TypeSafe may charge for each request. Data sent: "
             "the task kind, the query, and each candidate's name and description (up to 200 characters); for "
             "difficulty advice, the four difficulty axes and the task summary (up to 500 characters). Your key is "
             "read at run time from ${TYPESAFE_API_KEY} or the OS keychain; oah never asks for it or stores it, and "
             "without it Jev stays off. Your answer is recorded in .oah/config.toml.",
             "Turn on Jev? Yes is recommended. Type y or n:", None, recorded=True),
    Question("skill_updates", "Skill updates", "Keeps oah's own skill cache current automatically, checked at most "
             "once a day. Your project's skill folders never change on their own.",
             "Turn on automatic skill updates?", True, recorded=True),
)
ASK_ATTEMPTS = 5


def _ask(question: Question, ask, out) -> bool:
    out("")
    out(f"{question.title} ({question.flag_yes} or {question.flag_no})")
    out(f"  {question.what}")
    choices = "" if question.default is None else " [Y/n]" if question.default else " [y/N]"
    for _attempt in range(ASK_ATTEMPTS):
        reply = ask(f"  {question.prompt}{choices} ")
        if reply is None:
            raise InitError("OAH_INIT_NO_ANSWER", "the input ended before every question had an answer, so nothing "
                            f"was changed. Run oah init again, or answer this one with {question.flag_yes} or "
                            f"{question.flag_no}")
        text = reply.strip().lower()
        if text in ("y", "yes"):
            return True
        if text in ("n", "no"):
            return False
        if not text and question.default is not None:
            return question.default
        out("  Please type y or n.")
    raise InitError("OAH_INIT_NO_ANSWER", f"no y or n for {question.title}, so nothing was changed. Run oah init "
                    f"again, or pass {question.flag_yes} or {question.flag_no}")


def resolve_answers(given: Mapping[str, bool | None], *, recorded: Mapping[str, bool] | None, interactive: bool,
                    dry_run: bool, ask, out) -> dict[str, Answer]:
    """One answer per question, in QUESTIONS order: the flag; for a recorded question, the existing config
    (`recorded`, None when init writes the config); else the user's answer, or in a dry run the default
    (Jev: off). A real run without a terminal refuses with OAH_INIT_ANSWERS_REQUIRED instead of guessing."""
    answers: dict[str, Answer] = {}
    open_questions = []
    for question in QUESTIONS:
        if question.recorded and recorded is not None:
            answers[question.key] = Answer(bool(recorded[question.key]), "config")
        elif given.get(question.key) is not None:
            answers[question.key] = Answer(bool(given[question.key]), "flag")
        else:
            open_questions.append(question)
    if open_questions and not interactive and not dry_run:
        flags = "; ".join(f"{question.flag_yes} or {question.flag_no}" for question in open_questions)
        raise InitError("OAH_INIT_ANSWERS_REQUIRED", "oah init has no terminal to ask in, and these questions have no "
                        f"answer: {', '.join(question.title for question in open_questions)}. Nothing was changed. "
                        f"Run it again with one flag for each: {flags}. `oah init --dry-run` shows the plan first.",
                        EXIT_USAGE)
    for question in open_questions:
        answers[question.key] = (Answer(bool(question.default), "default") if dry_run
                                 else Answer(_ask(question, ask, out), "asked"))
    return {question.key: answers[question.key] for question in QUESTIONS}


# Templates -------------------------------------------------------------------------------------------

def _template(name: str, **values: str) -> str:
    """templates/init/<name> with every {{NAME}} filled in one pass (a value is never re-read)."""
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], notes.packaged("init", name))


def render_config(*, branch: str, how: str, jev: bool, skill_updates: bool, today: dt.date) -> str:
    """`.oah/config.toml` with the owner's defaults (PLAN 5.5), the default branch and the two recorded answers."""
    rerank_text = _template("rerank-on.toml" if jev else "rerank-off.toml", DATE=today.isoformat())
    return _template("config.toml", DEFAULT_BRANCH=json.dumps(branch, ensure_ascii=False), BRANCH_HOW=how,
                     AUTO_UPDATE="true" if skill_updates else "false", RERANK=rerank_text)


def _yaml(value: str) -> str:
    return value if _PLAIN.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def agent_files(engines: Mapping) -> dict[str, bytes]:
    """`.claude/agents/oah-<tier>.md` per tier: model by A14's mapping (a tier pin wins), effort from the tier."""
    claude = engines["claude"]
    files = {}
    for tier in routing.TIERS:
        values = claude["tiers"][tier]
        try:
            model, _source = models.map_tier("claude", tier, pin=values["model"],
                                             quality_order=claude["quality_order"], entry=None, effective=None)
        except EngineError:
            model = "inherit"  # nothing resolves: the subagent uses the session's model
        description = (f"oah {tier} tier: {TIER_WORK[tier]}. Model {model}, effort {values['effort']}, "
                       "from .oah/config.toml.")
        text = _template("agent.md", NAME=f"oah-{tier}", DESCRIPTION=json.dumps(description, ensure_ascii=False),
                         MODEL=_yaml(model), EFFORT=_yaml(values["effort"]), TIER=tier, FOOTER=intent.FOOTER)
        files[f"{AGENTS_DIR}/oah-{tier}.md"] = text.encode("utf-8")
    return files


def _gitignore_groups(unlazy_copy: bool) -> list[tuple[str, list[str]]]:
    """(comment line, entries) groups: ledgers and secrets (A17), Obsidian UI state (A8d), unlazy's copy."""
    groups: list[tuple[str, list[str]]] = []
    for text in (notes.packaged("init", "gitignore.txt"), notes.gitignore_snippet()):
        comment, entries = "", []
        for line in text.split("\n"):
            if line.startswith("#"):
                if entries:
                    groups.append((comment, entries))
                comment, entries = line, []
            elif line.strip():
                entries.append(line.strip())
        if entries:
            groups.append((comment, entries))
    if unlazy_copy:
        groups.append(("# The project copy of unlazy that oah init installed; it is a git checkout of its own.",
                       [UNLAZY_PROJECT + "/"]))
    return groups


def add_gitignore_entries(data: bytes | None, *, unlazy_copy: bool) -> bytes | None:
    """data with the missing entries appended in its line ending; None when nothing is missing."""
    present = {line.strip() for line in (data or b"").decode("utf-8").splitlines()}
    block: list[str] = []
    for comment, entries in _gitignore_groups(unlazy_copy):
        missing = [entry for entry in entries if entry not in present]
        if missing:
            block += [comment, *missing]
    if not block:
        return None
    eol = eol_of(data).decode("ascii") if data else "\n"
    out = data or b""
    if out and not out.endswith(b"\n"):
        out += eol.encode("ascii")
    if out.strip() and not out.endswith((eol * 2).encode("ascii")):
        out += eol.encode("ascii")
    return out + (eol.join(block) + eol).encode("utf-8")


def _ignore_problems(root: pathlib.Path, *, unlazy_copy: bool) -> list[str]:
    """Ask Git about effective rules, probing required patterns and their existing/nested locations."""
    index = run_git(root, "ls-files", "--stage", "-z")
    others = run_git(root, "ls-files", "--others", "-z")
    case = run_git(root, "config", "--type=bool", "--get", "core.ignorecase")
    if index.returncode != 0 or others.returncode != 0 or case.returncode not in (0, 1):
        return ["OAH_INIT_IGNORE_UNVERIFIED: the index could not be checked. Check repository permissions, "
                "then run oah init again"]
    # Git folds ASCII filename bytes, independently of Python's host-platform matching rules.
    folding = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    normalize = (lambda value: value.translate(folding)) if case.stdout.strip() == "true" else (lambda value: value)
    submodules = {row.partition("\t")[2] for row in index.stdout.split("\0") if row.startswith("160000 ")}
    folders = {}

    def walk_error(exc):
        raise exc

    try:
        for folder, dirs, files in os.walk(root, onerror=walk_error, followlinks=False):
            relative = pathlib.Path(folder).relative_to(root).as_posix()
            if relative in submodules:
                dirs[:] = []  # Git's indexed gitlinks, never an arbitrary .git-named entry
                continue
            dirs[:] = [name for name in dirs if name != ".git" and not _is_link(pathlib.Path(folder) / name)]
            folders[relative] = (dirs[:], files)
    except OSError:
        return ["OAH_INIT_IGNORE_UNVERIFIED: ignore protection could not be checked. Check directory permissions, "
                "then run oah init again"]

    # Keep actual spellings from both Git and the walk; never read any candidate's contents.
    for name in others.stdout.split("\0"):
        path = pathlib.PurePosixPath(name)
        parent = path.parent.as_posix()
        if name and parent in folders and path.name not in folders[parent][0] + folders[parent][1]:
            folders[parent][1].append(path.name)

    problems = []
    for _comment, entries in _gitignore_groups(unlazy_copy):
        for pattern in entries:
            parent, _sep, leaf = pattern.rstrip("/").rpartition("/")
            sample = pattern.replace("**/", "").replace("*", "oah-init-probe").rstrip("/")
            probes = {sample + ("/oah-init-probe" if pattern.endswith("/") else "")}
            for folder, (dirs, files) in folders.items():
                path = pathlib.PurePosixPath(folder)
                matched_path, matched_parent = pathlib.PurePosixPath(normalize(folder)), normalize(parent)
                matches_parent = (normalize(folder) == matched_parent or (parent and (
                    matched_path.match(matched_parent)
                    and (parent.startswith("**/") or len(path.parts) == len(parent.split("/"))))))
                if parent.startswith("**/") and normalize(folder) == matched_parent[3:]:
                    matches_parent = True
                if not parent or matches_parent:
                    # Existing matching names also catch specific negations such as !production.key.
                    for name in dirs + files:
                        if fnmatch.fnmatchcase(normalize(name), normalize(leaf)):
                            probes.add((path / name).as_posix() + ("/oah-init-probe" if pattern.endswith("/") else ""))
                    if matches_parent or not parent:
                        probes.add((path / leaf.replace("*", "oah-init-probe")).as_posix()
                                   + ("/oah-init-probe" if pattern.endswith("/") else ""))
            if any(run_git(root, "check-ignore", "--no-index", "-q", "--", probe).returncode != 0
                   for probe in sorted(probes)):
                problems.append(f"OAH_INIT_IGNORE_UNPROTECTED: required pattern {pattern} does not protect every "
                                "checked path. Fix overriding negations in the root or nested .gitignore files, "
                                "then run oah init again")
    return problems


# The default branch ----------------------------------------------------------------------------------

def default_branch(root: pathlib.Path) -> tuple[str, str]:
    """(branch, how it was found), without the network: the remote's HEAD, its main or master, a local
    main or master, the current branch, else main."""
    def git(*args: str) -> str:
        result = run_git(pathlib.Path(root), *args)
        return result.stdout.strip() if result.returncode == 0 else ""

    remotes = git("remote").split()
    remote = "origin" if "origin" in remotes else remotes[0] if len(remotes) == 1 else None
    if remote is not None:
        head = git("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        if head.startswith(remote + "/") and len(head) > len(remote) + 1:
            return head[len(remote) + 1:], f"from {remote}/HEAD"
        for name in ("main", "master"):
            if git("rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{name}"):
                return name, f"from {remote}/{name}"
    for name in ("main", "master"):
        if git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name, f"the local {name} branch"
    current = git("symbolic-ref", "--quiet", "--short", "HEAD")
    if current:
        return current, "the current branch"
    return "main", "a default, because HEAD names no branch"


# Planning and writing files --------------------------------------------------------------------------

def _is_link(path: pathlib.Path) -> bool:
    """A symlink, or on Windows a junction or symlink reparse point (os.path.isjunction needs Python 3.12)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in LINK_REPARSE_TAGS


def _linked(root: pathlib.Path, relative: str) -> str | None:
    """The first existing link (symlink or junction) on the way from root to the file, if any."""
    current = root
    for part in pathlib.PurePosixPath(relative).parts:
        current = current / part
        if _is_link(current):
            return current.relative_to(root).as_posix()
    return None


def _relative(relative: str) -> str:
    """Use the same local path grammar on both supported platforms, before any filesystem lookup."""
    relative = os.fspath(relative)
    windows = pathlib.PureWindowsPath(relative)
    parts = pathlib.PurePosixPath(relative.replace("\\", "/")).parts
    if (not parts or windows.drive or windows.root or ".." in parts
            or any(":" in part or part.endswith((" ", ".")) or "\x00" in part
                   or _WINDOWS_DEVICE.fullmatch(part.partition(".")[0].rstrip(" ")) for part in parts)):
        raise InitError("OAH_INIT_PATH_UNSAFE", "a destination is not a project-relative path. Set [wiki] dir "
                        "to a folder inside this worktree, without a drive, root or '..', then run oah init again",
                        EXIT_ENVIRONMENT)
    return "/".join(parts)


def _wiki_dir(root: pathlib.Path, relative: str) -> str:
    relative = _relative(relative)
    if _linked(root, relative) is not None:
        raise InitError("OAH_INIT_PATH_UNSAFE", "[wiki] dir contains a link or junction. Choose a regular "
                        "folder inside this worktree, then run oah init again", EXIT_ENVIRONMENT)
    return relative


def _private_state_directory(path: pathlib.Path, *, create: bool = False) -> None:
    """Reuse unlazy's private creation/permission checks without exposing its path/SID diagnostics."""
    try:
        if create:
            unlazy._verify_state_ancestry(path.parent)
            unlazy._private_directory(path)
        # Shared state parents contain landed modules' files and retained Git checkouts. Prove this
        # directory's ownership/ACLs and its ancestry without imposing snapshot modes on those siblings.
        unlazy._verify_state_ancestry(path)
        if os.name == "nt":
            _verify_windows_private_descriptor(unlazy._windows_security_descriptor(path), directory=True)
        else:
            if create:
                unlazy._verify_snapshot_permissions(path)
            if stat.S_IMODE(path.lstat().st_mode) != 0o700:
                raise OSError("state directory is not owner-only")
    except (OSError, unlazy.UnlazyError):
        raise InitError("OAH_INIT_STATE_UNSAFE", "private Git state permissions cannot be verified. Restrict "
                        "the state directory to your account, then rerun oah init", EXIT_ENVIRONMENT) from None


@contextmanager
def _windows_parents(root: pathlib.Path, relative: str, *, create_parents: bool = True, private: bool = False,
                     private_from: pathlib.Path | None = None):
    """Hold every no-follow parent without delete sharing through all pathname operations."""
    parent = (root / relative).absolute().parent
    with ExitStack() as pins:
        for path in (*reversed(parent.parents), parent):
            restricted = private or (private_from is not None and path.is_relative_to(private_from))
            if create_parents and path.is_relative_to(root):
                if restricted:
                    if not path.exists():
                        unlazy._private_directory(path)
                else:
                    path.mkdir(exist_ok=True)
            pins.enter_context(_pinned_directory(path))
            if restricted and path != root and path.is_relative_to(root):
                _private_state_directory(path)
        yield


@contextmanager
def _parents(root: pathlib.Path, relative: str, *, create_parents: bool = True, private: bool = False,
             private_from: pathlib.Path | None = None):
    """Keep every parent pinned until the caller has published and cleaned up."""
    if _linked(root, relative) is not None:
        raise OSError("linked publication path")
    if os.name == "nt":
        with _windows_parents(root, relative, create_parents=create_parents, private=private,
                              private_from=private_from):
            yield None
        return
    handles = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    path = root
    try:
        for part in pathlib.PurePosixPath(relative).parts[:-1]:
            path = path / part
            restricted = private or (private_from is not None and path.is_relative_to(private_from))
            if create_parents:
                try:
                    os.mkdir(part, 0o700 if restricted else 0o777, dir_fd=handles[-1])
                except FileExistsError:
                    pass
            handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            if restricted:
                _private_state_directory(path)
                actual, pinned = path.lstat(), os.fstat(handles[-1])
                if (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise OSError("private state directory changed")
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            os.close(handle)


def _current(root: pathlib.Path, relative: str, folder: int | None) -> bytes | None:
    """Read a regular target through its pinned parent; never follow its final component."""
    if _linked(root, relative) is not None:
        raise OSError("linked publication path")
    path = root / relative
    if folder is not None:
        parent = os.stat(path.parent, follow_symlinks=False)
        pinned = os.fstat(folder)
        if (parent.st_dev, parent.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise OSError("publication parent changed")
    try:
        fd = (_windows_read_fd(path) if os.name == "nt" else
              os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or _is_link(path):
            raise OSError("non-regular publication target")
        return handle.read()


def _windows_read_fd(path: pathlib.Path, *, writable: bool = False) -> int:
    """Open the final component itself, rejecting reparse points before any byte is read."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    info.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create(str(path), 0xC0000000 if writable else 0x80000000, 3 if writable else 1,
                    None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = (wintypes.DWORD * 2)()
        if not info(handle, 9, attributes, ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes[0] & (0x400 | 0x10):
            raise OSError("linked or non-file read target")
        fd = msvcrt.open_osfhandle(handle, (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    return fd  # fd now owns the Windows handle.


def _windows_file_identity(fd: int) -> bytes:
    """Use one native identity representation, independent of Python/CRT stat differences."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    info.restype = wintypes.BOOL
    identity = (ctypes.c_ubyte * 24)()  # FILE_ID_INFO: volume serial and 128-bit file id
    if not info(msvcrt.get_osfhandle(fd), 18, identity, ctypes.sizeof(identity)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bytes(identity)


def _read_user(root: pathlib.Path, relative: str) -> bytes | None:
    """Read only worktree-local regular files, without creating missing parent directories."""
    relative = _relative(relative)
    try:
        with _parents(root, relative, create_parents=False) as folder:
            return _current(root, relative, folder)
    except FileNotFoundError:
        return None
    except OSError:
        raise InitError("OAH_INIT_PATH_UNSAFE", "a project input is linked, changed or cannot be read safely. "
                        "Replace links and junctions with regular files and check permissions, then run "
                        "oah init again", EXIT_ENVIRONMENT) from None


def _load_config(root: pathlib.Path) -> Config:
    data = _read_user(root, CONFIG_PATH)
    return Config(path=None, raw={}) if data is None else parse_config(data, path=root / CONFIG_PATH)


def _tree_digest(path: pathlib.Path, *, root: pathlib.Path) -> str:
    """Identity of an installation being moved; linked entries are never eligible for publication."""
    digest = hashlib.sha256()

    def visit(item: pathlib.Path, name: str) -> None:
        if _is_link(item):
            raise OSError("linked installation entry")
        info = item.stat(follow_symlinks=False)
        digest.update(json.dumps([name, stat.S_IMODE(info.st_mode)], ensure_ascii=True).encode("ascii"))
        if stat.S_ISDIR(info.st_mode):
            digest.update(b"directory\0")
            for child in sorted(item.iterdir()):
                visit(child, name + "/" + child.name)
        elif stat.S_ISREG(info.st_mode):
            data = _read_user(root, item.relative_to(root).as_posix())
            if data is None:
                raise OSError("installation entry disappeared")
            digest.update(hashlib.sha256(data).digest())
        else:
            raise OSError("non-regular installation entry")

    visit(path, "")
    return digest.hexdigest()


def _rename_exclusive(source, target, source_folder: int | None, folder: int | None) -> None:
    """Atomic no-replace rename for installation directories (ordinary POSIX rename can clobber one)."""
    if os.name == "nt":
        _rename(source, target)  # Windows rename is exclusive; pinned parents keep both paths stable.
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameatx_np if sys.platform == "darwin" else libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_folder, os.fsencode(source), folder, os.fsencode(target),
              4 if sys.platform == "darwin" else 1):  # RENAME_EXCL / RENAME_NOREPLACE
        raise OSError(ctypes.get_errno(), "exclusive installation publication failed")


@contextmanager
def _pinned_directory(path: pathlib.Path, *, private_staging: bool = False):
    """Deny directory replacement; only private Git-state staging may share delete access."""
    if os.name != "nt":
        handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(handle)
            yield handle, f"{info.st_dev}:{info.st_ino}"
        finally:
            os.close(handle)
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    info.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    def open_directory():
        handle = create(str(path), 0x81, 7 if private_staging else 3, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = (wintypes.DWORD * 2)()
            identity = (ctypes.c_ubyte * 24)()  # FILE_ID_INFO: volume serial and 128-bit file id
            if (not info(handle, 9, attributes, ctypes.sizeof(attributes))
                    or not info(handle, 18, identity, ctypes.sizeof(identity))):
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes[0] & 0x400 or not attributes[0] & 0x10:
                raise OSError("linked or non-directory path")
            return handle, bytes(identity).hex()
        except BaseException:
            close(handle)
            raise

    handle, identity = open_directory()
    try:
        yield handle, identity
        current, actual = open_directory()
        try:
            if actual != identity:
                raise OSError("pinned directory changed")
        finally:
            close(current)
    finally:
        close(handle)


def _macos_file_acl(fd: int, value: bytes | None = None) -> bytes:
    """Read/copy the effective extended ACL through a held file, never its pathname."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd.argtypes, libc.acl_get_fd.restype = (ctypes.c_int,), ctypes.c_void_p
    libc.acl_from_text.argtypes, libc.acl_from_text.restype = (ctypes.c_char_p,), ctypes.c_void_p
    libc.acl_init.argtypes, libc.acl_init.restype = (ctypes.c_int,), ctypes.c_void_p
    libc.acl_to_text.argtypes, libc.acl_to_text.restype = (ctypes.c_void_p, ctypes.c_void_p), ctypes.c_void_p
    libc.acl_set_fd.argtypes = (ctypes.c_int, ctypes.c_void_p)
    libc.acl_free.argtypes = (ctypes.c_void_p,)
    acl = (libc.acl_get_fd(fd) if value is None else
           libc.acl_from_text(value) if value else libc.acl_init(0))
    if not acl:
        if value is None and ctypes.get_errno() == errno.ENOENT:
            return b""
        raise OSError("cannot read file ACL")
    try:
        if value is not None and libc.acl_set_fd(fd, acl):
            raise OSError("cannot preserve file ACL")
        text = libc.acl_to_text(acl, None)
        if not text:
            raise OSError("cannot verify file ACL")
        try:
            return ctypes.string_at(text)
        finally:
            libc.acl_free(text)
    finally:
        libc.acl_free(acl)


def _windows_security_matches(actual: str, expected: str) -> bool:
    """Require trusted owners and a protected DACL with identical ordered effective ACEs.

    Freezing inheritance can change AI/AR control bits and inherited-ACE provenance (ID),
    without changing any grant or denial. Resolve owner/ACE aliases through the landed SID helper;
    the landed verifier owns the trusted-owner set. No other ACE flag, right or principal is ignored.
    """
    # Same standard/file rights vocabulary as the landed verifier, including SDDL's hex spelling.
    rights_masks = {"CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "RP": 0x10, "WP": 0x20,
                    "DT": 0x40, "LO": 0x80, "CR": 0x100, "SD": 0x10000, "RC": 0x20000,
                    "WD": 0x40000, "WO": 0x80000, "FA": 0x1F01FF, "FR": 0x120089,
                    "FW": 0x120116, "FX": 0x1200A0, "GA": 0x10000000, "GX": 0x20000000,
                    "GW": 0x40000000, "GR": 0x80000000}

    def effective(descriptor: str) -> list[tuple]:
        aces = []
        for ace in re.findall(r"\(([^()]*)\)", descriptor):
            kind, flags, rights, object_type, inherited_type, principal = ace.split(";")
            if re.fullmatch(r"0x[0-9a-fA-F]+", rights):
                mask = int(rights, 16)
            else:
                tokens = [rights[i:i + 2] for i in range(0, len(rights), 2)]
                if not tokens or any(token not in rights_masks for token in tokens):
                    raise OSError("unverified DACL rights")
                mask = 0
                for token in tokens:
                    mask |= rights_masks[token]
            aces.append((kind, flags.replace("ID", ""), mask, object_type, inherited_type,
                         unlazy._windows_canonical_sid(principal)))
        return aces

    try:
        sid = unlazy._windows_canonical_sid(unlazy._windows_user_sid())
        unlazy._verify_windows_dacl(actual, sid, protected=True)
        unlazy._verify_windows_dacl(expected, sid)
        return effective(actual) == effective(expected)
    except OSError:
        return False  # Never forward the landed verifier's account identifiers or descriptor bytes.


def _verify_windows_private_descriptor(descriptor: str, *, directory: bool) -> None:
    """Use landed owner/DACL verification, retaining init's refusal of foreign read grants."""
    try:
        sid = unlazy._windows_user_sid()
        unlazy._verify_windows_dacl(descriptor, sid, snapshot=True, directory=directory, protected=True)
        owner = unlazy._windows_canonical_sid(re.match(r"O:([^:()]+)D:", descriptor)[1])
        # The landed verifier proves write integrity; backups additionally forbid disclosure to any
        # other principal. Do not compare creator/read-back SDDL, owner aliases or inheritance spelling.
        for ace in re.findall(r"\(([^()]*)\)", descriptor):
            kind, _flags, _rights, _object, _inherited, principal = ace.split(";")
            if kind == "A" and unlazy._windows_canonical_sid(principal) not in {sid, owner, "S-1-3-4"}:
                raise OSError("private DACL grants access to another principal")
    except OSError:
        raise OSError("private Windows permissions cannot be verified") from None


def _windows_file_security(path: pathlib.Path, descriptor: str) -> int:
    """Create a file with its final protected DACL; never rewrite existing object security."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # The landed verifier supplies the trusted-owner and supported-DACL rules for replacements too.
    try:
        unlazy._verify_windows_dacl(descriptor, unlazy._windows_user_sid(), protected=True, directory=False)
    except OSError:
        raise OSError("replacement Windows permissions cannot be verified") from None
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = (ctypes.c_void_p,), ctypes.c_void_p
    security = ctypes.c_void_p()
    if not convert(descriptor, 1, ctypes.byref(security), None):
        raise OSError(ctypes.get_last_error(), "cannot prepare file security")
    try:
        class Attributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

        attributes = Attributes(ctypes.sizeof(Attributes), security, False)
        opener = kernel.CreateFileW
        opener.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Attributes),
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        opener.restype = wintypes.HANDLE
        # Only writing is needed: a preserved denial of FILE_READ_DATA must not prevent staging.
        handle = opener(str(path), 0x40000000, 1, ctypes.byref(attributes), 1, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "cannot create private staging file")
        try:
            return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle(handle)
            raise
    finally:
        kernel.LocalFree(security)


def _private_file(temp: pathlib.Path, folder: int | None, *, security: str | None = None) -> int:
    """Establish and verify owner-only permissions before any preserved bytes reach staging."""
    if os.name == "nt":
        sid = unlazy._windows_user_sid()
        expected = security or f"O:{sid}D:P(A;;FA;;;OW)"
        fd = _windows_file_security(temp, expected)
    else:
        fd = os.open(temp.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    try:
        if os.name == "nt":
            actual = unlazy._windows_security_descriptor(temp)
            if security is None:
                _verify_windows_private_descriptor(actual, directory=False)
            elif not _windows_security_matches(actual, expected):
                raise OSError("replacement security differs from planned DACL")
        elif sys.platform == "darwin":
            if unlazy._macos_mount_flags(temp) & 0x00200000:  # MNT_IGNORE_OWNERSHIP
                raise OSError("staging volume does not enforce ownership")
            _macos_file_acl(fd, b"")
            if _macos_file_acl(fd).strip() not in (b"", b"!#acl 1"):
                raise OSError("staging ACL is not empty")
        if os.name != "nt" and stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise OSError("staging mode is not owner-only")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _file_staging(path: pathlib.Path, folder: int | None):
    """Keep staging files under a private directory, with security established at creation."""
    directory = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    name = directory if folder is None else directory.name
    if os.name == "nt":
        unlazy._private_directory(directory)
        identity = ""
        try:
            with _pinned_directory(directory) as (_handle, identity):
                _verify_windows_private_descriptor(unlazy._windows_security_descriptor(directory), directory=True)
                yield directory / "file.tmp", None
        finally:
            if identity:
                _require_staging(directory, identity)
                os.rmdir(directory)
        return
    os.mkdir(name, 0o700, dir_fd=folder)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError("staging directory is not owner-only")
            if sys.platform == "darwin":
                _macos_file_acl(descriptor, b"")
                if _macos_file_acl(descriptor).strip() not in (b"", b"!#acl 1"):
                    raise OSError("staging directory ACL is not empty")
            yield directory / "file.tmp", descriptor
        finally:
            os.close(descriptor)
    finally:
        os.rmdir(name, dir_fd=folder)  # Never recurse into a replaced directory.


def _file_restrictions(root: pathlib.Path, relative: str, folder: int | None) -> tuple[int, bytes | str] | None:
    path = root / relative
    try:
        fd = (_windows_read_fd(path) if os.name == "nt" else
              os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("non-regular replacement")
        if os.name != "nt" and info.st_uid != os.getuid():
            raise OSError("replacement owner differs")
        if os.name == "nt":
            original = unlazy._windows_security_descriptor(path)
            # Snapshot the protected effective DACL that publication will copy, even if this source
            # still inherits. Rechecks use the same structural comparator rather than SDDL spelling.
            acl = re.sub(r"D:(?:P|AI|AR)*", "D:P", original, count=1)
            if not _windows_security_matches(acl, original):
                raise OSError("replacement permissions cannot be verified")
        else:
            acl = _macos_file_acl(fd) if sys.platform == "darwin" else b""
        return stat.S_IMODE(info.st_mode), acl
    finally:
        os.close(fd)


def _publish(root: pathlib.Path, relative: str, data: bytes | None, before: bytes | None = None, *,
             executable: bool = False, source: str | None = None, source_digest: str = "",
             private: bool = False, source_root: pathlib.Path | None = None,
             source_identity: str = "") -> bool:
    """The only file publication path: local destination, pinned parents, final digest check, atomic publish."""
    if source is not None:
        return False  # No directory may be moved out of or into the worktree (owner M).
    root = pathlib.Path(root).absolute()
    try:
        relative = _relative(relative)
        with _parents(root, relative, private=private) as folder:
            path = root / relative
            name = path if folder is None else path.name
            if os.name == "nt":
                with _pinned_directory(path.parent) as (_handle, parent_identity):
                    pass
            if data is None:
                # Reserve a new installation exclusively, without merging into a concurrent directory.
                if os.name == "nt":
                    unlazy._private_directory(path)
                    _verify_windows_private_descriptor(unlazy._windows_security_descriptor(path), directory=True)
                    _require_staging(path.parent, parent_identity)
                else:
                    os.mkdir(name, 0o700, dir_fd=folder)
                    if private:
                        _private_state_directory(path)
                if folder is not None:
                    os.fsync(folder)
                return True
            with _file_staging(path, folder) as (temp, staging_folder):
                temporary = temp if folder is None else temp.name
                if os.name == "nt":
                    with _pinned_directory(temp.parent) as (_handle, staging_identity):
                        pass
                try:
                    restrictions = _file_restrictions(root, relative, folder)
                    fd = (_private_file(temp, staging_folder, security=restrictions[1])
                          if os.name == "nt" and restrictions is not None else _private_file(temp, staging_folder))
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                        if os.name != "nt":
                            mode = (restrictions[0] if restrictions is not None
                                    else 0o700 if executable else 0o600)
                            os.fchmod(handle.fileno(), mode)
                            if sys.platform == "darwin" and restrictions is not None:
                                _macos_file_acl(handle.fileno(), restrictions[1])
                                if _macos_file_acl(handle.fileno()) != restrictions[1]:
                                    raise OSError("replacement ACL changed")
                        os.fsync(handle.fileno())
                    current = _current(root, relative, folder)
                    if ((current is None) != (before is None) or (current is not None and
                            hashlib.sha256(current).digest() != hashlib.sha256(before).digest())):
                        return False
                    current_restrictions = _file_restrictions(root, relative, folder)
                    if os.name == "nt" and current_restrictions is not None and restrictions is not None:
                        if (current_restrictions[0] != restrictions[0] or not
                                _windows_security_matches(current_restrictions[1], restrictions[1])):
                            return False
                    elif current_restrictions != restrictions:
                        return False
                    if _current(root, relative, folder) != current:
                        return False
                    if os.name == "nt":
                        _require_staging(path.parent, parent_identity)
                        _require_staging(temp.parent, staging_identity)
                    if before is not None:
                        os.replace(temporary, name, src_dir_fd=staging_folder, dst_dir_fd=folder)
                    elif os.name == "nt":
                        os.rename(temporary, name)  # Windows refuses an existing target.
                    else:
                        os.link(temporary, name, src_dir_fd=staging_folder, dst_dir_fd=folder, follow_symlinks=False)
                    if folder is not None:
                        os.fsync(folder)
                    return True
                finally:
                    if os.name == "nt":
                        _require_staging(path.parent, parent_identity)
                        _require_staging(temp.parent, staging_identity)
                    try:
                        os.unlink(temporary, dir_fd=staging_folder)
                    except FileNotFoundError:
                        pass
    except (OSError, InitError, unlazy.UnlazyError):
        return False


def _publish_new(root: pathlib.Path, relative: str, data: bytes) -> bool:
    """Compatibility entry point for an exclusive create."""
    return _publish(root, relative, data)


def _wiki_path(wiki_dir: str, name: str) -> str:
    return (pathlib.PurePosixPath(wiki_dir.replace("\\", "/")) / name).as_posix()


def plan_files(root: pathlib.Path, *, config: bytes, wiki_dir: str, agents: Mapping[str, bytes], graph: bool,
               unlazy_copy: bool, force: bool, today: dt.date) -> list[FileChange]:
    """Every file init manages, with what would happen to it. Reads only."""
    root = pathlib.Path(root)
    wiki_dir = _wiki_dir(root, wiki_dir)
    changes: list[FileChange] = []

    def owned(path: str, data: bytes, group: str) -> FileChange:
        path = _relative(path)
        link = _linked(root, path)
        before = None if link is not None else _read_user(root, path)
        if link is not None:
            change = FileChange(path, "blocked", group, None, before,
                                f"{link} is a link; oah never writes through links, which could reach files outside "
                                "the project")
        elif before is None:
            change = FileChange(path, "create", group, data)
        elif before == data:
            change = FileChange(path, "unchanged", group, None, before)
        elif force:
            change = FileChange(path, "replace", group, data, before)
        else:
            change = FileChange(path, "keep", group, None, before,
                                KEEP_NOTES.get(path, "exists; pass --force to replace it"))
        changes.append(change)
        return change

    owned(CONFIG_PATH, config, "config")
    readme = _template("templates-readme.md", WIKI_DIR=wiki_dir.replace("\\", "/")).encode("utf-8")
    for name, data in (("PACKET.md", notes.packaged("packet", "PACKET.md").encode("utf-8")),
                       ("GATES.md", notes.packaged("gates", "GATES.md").encode("utf-8")), ("README.md", readme)):
        owned(f"{TEMPLATES_DIR}/{name}", data, "templates")
    scaffold = notes.scaffold_files(today)
    guide_missing = _read_user(root, _wiki_path(wiki_dir, f"{GUIDE_SLUG}.md")) is None
    for name in WIKI_CONTENT:
        changes.append(_plan_wiki_content(root, _wiki_path(wiki_dir, name), scaffold[name], guide_missing))
    for name, data in scaffold.items():
        if name not in WIKI_CONTENT:
            owned(_wiki_path(wiki_dir, name), data, "wiki")
    for path, data in agents.items():
        owned(path, data, "agents")
    if graph:
        owned(GRAPHIFYIGNORE, _template("graphifyignore.txt", WIKI_DIR=wiki_dir.replace("\\", "/")).encode("utf-8"),
              "graph")
    changes.append(_plan_gitignore(root, unlazy_copy))
    return changes


def _plan_wiki_content(root: pathlib.Path, path: str, fresh: bytes, guide_missing: bool) -> FileChange:
    link = _linked(root, path)
    before = None if link is not None else _read_user(root, path)
    if link is not None:
        return FileChange(path, "blocked", "wiki", None, before, f"{link} is a link; oah never writes through links")
    if before is None:
        return FileChange(path, "create", "wiki", fresh)
    if before == fresh:
        return FileChange(path, "unchanged", "wiki", None, before)
    keep = "wiki content; init never replaces it"
    if path.endswith("/index.md") and guide_missing:
        try:
            if not _GUIDE_LINK.search(before.decode("utf-8")):
                updated = notes.add_to_index(before, "Notes", notes.index_item(GUIDE_SLUG, GUIDE_TITLE))
                return FileChange(path, "update", "wiki", updated, before, f"add the line for {GUIDE_SLUG}.md")
        except (UnicodeDecodeError, WikiError):
            return FileChange(path, "keep", "wiki", None, before,
                              f"{keep}; it is not UTF-8 text, so add [[{GUIDE_SLUG}]] to it by hand")
    if path.endswith("/log.md"):
        try:
            parse_log(before, "log.md")
        except WikiError:
            return FileChange(path, "keep", "wiki", None, before,
                              f"{keep}; its log format is invalid; repair it before running oah wiki commands")
    return FileChange(path, "keep", "wiki", None, before, keep)


def _plan_gitignore(root: pathlib.Path, unlazy_copy: bool) -> FileChange:
    if _is_link(root / GITIGNORE):
        return FileChange(GITIGNORE, "blocked", "gitignore", None, None,
                          "it is a link; oah never writes through links")
    before = _read_user(root, GITIGNORE)
    try:
        data = add_gitignore_entries(before, unlazy_copy=unlazy_copy)
    except UnicodeDecodeError:
        missing = [entry for _comment, entries in _gitignore_groups(unlazy_copy) for entry in entries]
        return FileChange(GITIGNORE, "keep", "gitignore", None, before,
                          f"it is not UTF-8 text, so add these entries by hand: {', '.join(missing)}")
    if data is None:
        return FileChange(GITIGNORE, "unchanged", "gitignore", None, before)
    what = "gate ledgers, Obsidian's UI state and local secret files stay out of git"
    if before is None:
        return FileChange(GITIGNORE, "create", "gitignore", data, None, what)
    added = len(data.decode("utf-8").splitlines()) - len(before.decode("utf-8").splitlines())
    return FileChange(GITIGNORE, "update", "gitignore", data, before, f"append {added} lines so {what}")


def _wiki_state_path(root: pathlib.Path, state: pathlib.Path) -> pathlib.Path:
    """State is a planned destination, outside committable files and without redirected components."""
    try:
        root = pathlib.Path(root).absolute()
        if ".." in state.parts:
            raise OSError("traversing state path")
        state = state.absolute()
        common = _backup_root(root)
        if state.is_relative_to(root) and not state.is_relative_to(common):
            raise OSError("committable wiki state")
        anchor = pathlib.Path(state.anchor)
        relative = state.relative_to(anchor).as_posix()
        if _relative(relative) != relative:
            raise OSError("noncanonical wiki state path")
        if any(_linked(anchor, relative + suffix) is not None
               for suffix in ("", "/.lock", "/rollover-journal.json")):
            raise OSError("redirected wiki state")
        unlazy._verify_state_ancestry(state)
        return state
    except (OSError, unlazy.UnlazyError, InitError, ValueError):
        raise InitError("OAH_INIT_STATE_UNSAFE", "wiki state is linked, inside the worktree or cannot be "
                        "accessed safely. Set OAH_STATE_DIR to a private directory outside the worktree, "
                        "remove state links and check permissions, then rerun oah init", EXIT_ENVIRONMENT) from None


@contextmanager
def _wiki_session(root: pathlib.Path, wiki):
    """Adapt Wiki.session's lock/recovery protocol to pinned directory and file descriptors."""
    state = _wiki_state_path(root, wiki.state)
    anchor = pathlib.Path(state.anchor)
    private_from = state.parent
    while not private_from.parent.exists():
        private_from = private_from.parent
    try:
        with _parents(anchor, (state.relative_to(anchor) / ".init-state").as_posix(),
                      private_from=private_from) as folder:
            if os.name == "nt":
                with _pinned_directory(state) as (_handle, state_identity):
                    pass
            def open_lock() -> int:
                return (_windows_read_fd(state / ".lock", writable=True) if os.name == "nt" else
                        os.open(".lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder))

            # Read-only share modes conflict with the landed writer's open Windows lock. Open it
            # read/write directly, without reading its bytes; only a missing lock is published.
            try:
                fd = open_lock()
            except FileNotFoundError:
                if not _publish(state, ".lock", b""):
                    raise OSError("wiki lock changed")
                fd = open_lock()
            lock = StateLock(state / ".lock", timeout=wiki.lock_timeout)
            lock._fd = fd

            def check() -> None:
                if _linked(anchor, (state.relative_to(anchor) / ".lock").as_posix()) is not None:
                    raise OSError("wiki lock redirected")
                if os.name == "nt":
                    _require_staging(state, state_identity)
                    current_fd = _windows_read_fd(state / ".lock", writable=True)
                    try:
                        if _windows_file_identity(current_fd) != held:
                            raise OSError("wiki lock replaced")
                    finally:
                        os.close(current_fd)
                else:
                    actual = (state / ".lock").lstat()
                    if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (held.st_dev, held.st_ino):
                        raise OSError("wiki lock replaced")
                if folder is not None:
                    actual, pinned = state.lstat(), os.fstat(folder)
                    if (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino):
                        raise OSError("wiki state replaced")

            acquired = False
            try:
                held = _windows_file_identity(fd) if os.name == "nt" else os.fstat(fd)
                check()
                deadline = time.monotonic() + lock.timeout
                while not _try_lock(fd):
                    if time.monotonic() >= deadline:
                        raise WikiError("OAH_WIKI_BUSY", "another oah wiki command holds the wiki lock. "
                                        "Wait for it to finish, then retry.")
                    time.sleep(lock.poll)
                acquired = True
                check()
                if _current(state, "rollover-journal.json", folder) is not None:
                    raise WikiError("OAH_WIKI_RECOVERY_PENDING", "an interrupted wiki rollover needs recovery. "
                                    "Run `oah wiki recover`, then retry oah init.", EXIT_ENVIRONMENT)
                yield check
            finally:
                if acquired:
                    lock.__exit__(None, None, None)
                else:
                    os.close(fd)
    except (OSError, unlazy.UnlazyError):
        raise InitError("OAH_INIT_STATE_UNSAFE", "wiki state changed or cannot be accessed safely. "
                        "Remove state links, check private state permissions, then rerun oah init",
                        EXIT_ENVIRONMENT) from None


def write_files(root: pathlib.Path, changes: list[FileChange], *, wiki) -> tuple[list[FileChange], list[str]]:
    """Write the planned creates, replacements and updates; (written, problems). Wiki files are written
    under the wiki lock. A path that changed since the plan is skipped with OAH_INIT_CHANGED."""
    root = pathlib.Path(root)
    written: list[FileChange] = []
    problems: list[str] = []

    def write(change: FileChange) -> None:
        if not _publish(root, change.path, change.data, None if change.action == "create" else change.before):
            problems.append(f"OAH_INIT_CHANGED: {change.path} changed after the plan was shown, so init left it "
                            "as it is. Run oah init again to see the new plan.")
            return
        written.append(change)

    pending = [change for change in changes if change.action in WRITES]
    wiki_changes = [change for change in pending if change.group == "wiki"]
    with _wiki_session(root, wiki) if wiki_changes else nullcontext(lambda: None) as check:
        for group in (False, True):
            for change in pending:
                if (change.group == "wiki") != group:
                    continue
                check()
                write(change)
    return written, problems


# Managed blocks ----------------------------------------------------------------------------------------

class RulesStep(NamedTuple):
    lang: str
    agents: str  # create | update | unchanged
    claude: str  # create | update | unchanged
    block_bytes: int
    planned: rules.Plan | None = None


def _rules_plan(root: pathlib.Path, *, lang: str | None = None) -> rules.Plan:
    """Adapt rules.plan's algorithm with pinned input reads; all rendering/validation stays in rules."""
    root = pathlib.Path(root)
    local_bytes = _read_user(root, rules.LOCAL_RULES.as_posix())
    local = "".join(rules._decode(local_bytes, str(rules.LOCAL_RULES))) if local_bytes is not None else ""
    agents = _read_user(root, "AGENTS.md")
    if agents is None:
        lang = lang or "en"
        block = rules.render_block(lang, local, "\n")
        after = (block + "\n").encode("utf-8")
    else:
        lines = rules._decode(agents, "AGENTS.md")
        span = rules.find_block(lines)
        if span is None:
            lang = lang or "en"
            eol = eol_of(agents).decode("ascii")
            block = rules.render_block(lang, local, eol)
            head = agents if not agents or agents.endswith(b"\n") else agents + eol.encode("ascii")
            gap = eol.encode("ascii") if head.strip() and not head.endswith(eol.encode("ascii") * 2) else b""
            after = head + gap + (block + eol).encode("utf-8")
        else:
            begin, end = span
            found = rules._LANG.match(rules.strip_eol(lines[begin + 1])) if begin + 1 < end else None
            lang = lang or (found.group(1) if found and found.group(1) in rules.LANGS else "en")
            eol = "\r\n" if lines[begin].endswith("\r\n") else "\n" if lines[begin].endswith("\n") \
                else eol_of(agents).decode("ascii")
            block = rules.render_block(lang, local, eol)
            tail = lines[end][len(rules.strip_eol(lines[end])):]
            after = ("".join(lines[:begin]) + block + tail + "".join(lines[end + 1:])).encode("utf-8")
    rules.find_block(rules._decode(after, "AGENTS.md"))
    size = len(block.encode("utf-8"))
    if size >= rules.BLOCK_LIMIT:
        raise rules.RulesError("OAH_RULES_BLOCK_TOO_LARGE", f"the managed block would be {size} bytes, over the "
                               f"{rules.BLOCK_LIMIT}-byte limit. Shorten {rules.LOCAL_RULES} (link a wiki note for "
                               "long project rules) and retry. Nothing was written.")
    if len(after) > rules.AGENTS_BUDGET:
        raise rules.RulesError("OAH_RULES_BUDGET", f"AGENTS.md would be {len(after)} bytes, over Codex's "
                               f"{rules.AGENTS_BUDGET}-byte project-doc limit. Move long sections into docs/wiki/ "
                               "and link them, then retry. Nothing was written.")
    claude = _read_user(root, "CLAUDE.md")
    if claude is None:
        claude_after = (rules.IMPORT + "\n").encode("ascii")
    else:
        lines = rules._decode(claude, "CLAUDE.md")
        count = sum(1 for line in lines if rules.strip_eol(line).strip() == rules.IMPORT)
        if count > 1:
            raise rules.RulesError("OAH_RULES_MARKER_DUPLICATE", f"CLAUDE.md has {count} {rules.IMPORT} lines; keep "
                                   "exactly one and run `oah rules sync` again. Nothing was written.")
        eol = eol_of(claude)
        claude_after = claude if count else (claude + (b"" if not claude or claude.endswith(b"\n") else eol)
                                           + rules.IMPORT.encode("ascii") + eol)
    return rules.Plan(lang, agents, after, claude, claude_after, size)


def plan_rules(root: pathlib.Path, *, lang: str | None) -> RulesStep:
    """What `rules.sync` would do to AGENTS.md and CLAUDE.md; raises rules.RulesError for broken markers."""
    found = _rules_plan(pathlib.Path(root), lang=lang)

    def action(before: bytes | None, after: bytes) -> str:
        return "create" if before is None else "unchanged" if before == after else "update"

    return RulesStep(found.lang, action(found.agents_before, found.agents_after),
                     action(found.claude_before, found.claude_after), found.block_bytes, found)


def _sync_rules(root: pathlib.Path, step: RulesStep) -> rules.SyncResult:
    """Keep rules' validation and formatting, publishing only the bytes the initial plan approved."""
    found = step.planned
    if found is None:
        raise InitError("OAH_INIT_CHANGED", "no rules publication was planned; run oah init again")
    changed = []
    for name, before, after in (("AGENTS.md", found.agents_before, found.agents_after),
                                ("CLAUDE.md", found.claude_before, found.claude_after)):
        if before != after:
            if not _publish(root, name, after, before):
                raise InitError("OAH_INIT_CHANGED", f"{name} changed; run oah init again to see the new plan")
            changed.append(name)
    return rules.SyncResult(found.lang, tuple(changed), len(found.agents_after), found.block_bytes)


def add_aside_rules(root: pathlib.Path, *, lang: str) -> str:
    """Put the Aside web rules (A20) into .oah/rules.local.md once, so the rules block carries them.

    Returns "added", "present" or "skipped: <why>". The file keeps its bytes and line ending."""
    return _write_aside_local(pathlib.Path(root), _plan_aside_local(pathlib.Path(root), lang=lang))


def _plan_aside_local(root: pathlib.Path, *, lang: str) -> FileChange:
    """Capture the conditional local publication before confirmation, including its original bytes."""
    relative = rules.LOCAL_RULES.as_posix()
    link = _linked(root, relative)
    if link is not None:
        return FileChange(relative, "blocked", "rules", note="OAH_INIT_CHANGED: rules path is linked; "
                          "remove the link and run oah init again")
    before = _read_user(root, relative)
    text, eol = "", "\n"
    if before is not None:
        try:
            text = before.decode("utf-8")
        except UnicodeDecodeError:
            return FileChange(relative, "blocked", "rules", note="OAH_INIT_CHANGED: local rules are not UTF-8; "
                              "save them as UTF-8 and run oah init again")
        if any(line.rstrip() == companions.RULES_BEGIN for line in text.split("\n")):
            return FileChange(relative, "unchanged", "rules", before, before)
        eol = eol_of(before).decode("ascii")
    if text and not text.endswith("\n"):
        text += eol
    if text.strip() and not text.endswith(eol * 2):
        text += eol
    data = (text + companions.rules_section(True, lang=lang).replace("\n", eol)).encode("utf-8")
    return FileChange(relative, "create" if before is None else "update", "rules", data, before,
                      _replacement_note(before))


def _write_aside_local(root: pathlib.Path, change: FileChange) -> str:
    if change.action == "blocked":
        return "skipped: " + change.note
    if change.action == "unchanged":
        if _linked(root, change.path) is None and _read_user(root, change.path) == change.before:
            return "present"
    elif _publish(root, change.path, change.data, change.before):
        return "added"
    return "skipped: OAH_INIT_CHANGED: rules changed; run oah init again"


def _plan_aside(root: pathlib.Path, step: RulesStep) -> tuple[FileChange, RulesStep]:
    local = _plan_aside_local(root, lang=step.lang)
    if local.action in ("blocked", "unchanged"):
        return local, step
    found = step.planned
    lines = found.agents_after.decode("utf-8").splitlines(keepends=True)
    begin, end = rules.find_block(lines)
    eol = "\r\n" if lines[begin].endswith("\r\n") else "\n"
    block = rules.render_block(step.lang, local.data.decode("utf-8"), eol)
    after = ("".join(lines[:begin]) + block + lines[end][len(lines[end].rstrip("\r\n")):] +
             "".join(lines[end + 1:])).encode("utf-8")
    size = len(block.encode("utf-8"))
    if size >= rules.BLOCK_LIMIT or len(after) > rules.AGENTS_BUDGET:
        raise rules.RulesError("OAH_RULES_BUDGET", "the Aside rules would exceed the rules budget. Shorten "
                               ".oah/rules.local.md or AGENTS.md, then run oah init again")
    planned = dataclasses.replace(found, agents_after=after, block_bytes=size)
    action = "create" if found.agents_before is None else "unchanged" if found.agents_before == after else "update"
    return local, RulesStep(step.lang, action, step.claude, size, planned)


# Hooks -------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class HookStep:
    kind: str
    action: str  # install | installed | keep | refused | unavailable
    diff: str = ""
    path: str = ""
    note: str = ""
    before: bytes | None = dataclasses.field(default=None, repr=False)
    data: bytes | None = dataclasses.field(default=None, repr=False)
    backup: str = ""  # run/position below <git common dir>/oah/backups, never a worktree file


def _shown_path(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _local_settings_problem(root: pathlib.Path, *, require_ignored: bool) -> str:
    paths = (CLAUDE_SETTINGS, CLAUDE_SETTINGS + hooks.BACKUP_SUFFIX)
    tracked = run_git(root, "ls-files", "-z", "--", *paths)
    if tracked.returncode != 0 or tracked.stdout:
        return ("OAH_INIT_HOOK_SETTINGS_UNSAFE: local settings or their backup may be tracked. Remove them from "
                "the index with git rm --cached, keep them ignored, then run oah init again")
    for path in paths:
        if _linked(root, path) is not None:
            return "OAH_INIT_HOOK_SETTINGS_UNSAFE: local settings or their backup is a link; remove the link and retry"
        if require_ignored and run_git(root, "check-ignore", "-q", "--", path).returncode != 0:
            return ("OAH_INIT_HOOK_SETTINGS_UNSAFE: local settings and backups must both be ignored before hook "
                    "installation. Add .claude/settings.local.json and .claude/settings.local.json.oah.bak to "
                    ".gitignore, remove any overriding negation, then run oah init again")
    return ""


def _git_hook_problem(root: pathlib.Path, kind: str) -> str:
    path = pathlib.Path(os.path.abspath(hooks.git_hook_path(root, kind)))
    try:
        relative = _relative(path.relative_to(root).as_posix())
        if not _is_link(root) and _linked(root, relative) is None:
            return ""
    except (ValueError, InitError):
        pass
    return ("OAH_INIT_HOOK_LOCATION_UNSAFE: git hooks must be inside this worktree. Select a project-local folder "
            "with `git config core.hooksPath .githooks`, then run oah init again")


def _claude_hook_diff(kind: str) -> str:
    """Show only the known addition; existing settings, commands and matchers are private."""
    event, matcher = hooks.CLAUDE_KINDS[kind]
    group = {"matcher": matcher} if matcher else {}
    group["hooks"] = [{"type": "command", "command": hooks.command_for(kind)}]
    text = json.dumps({"hooks": {event: [group]}}, indent=2) + "\n"
    return "".join(difflib.unified_diff([], text.splitlines(keepends=True),
                                       fromfile="/dev/null", tofile=f"b/{CLAUDE_SETTINGS} (hook addition only)"))


def _replacement_note(data: bytes | None) -> str:
    return f"replaces {len(data)} bytes (sha256 {hashlib.sha256(data).hexdigest()})" if data is not None else ""


def plan_hooks(root: pathlib.Path, *, force: bool) -> list[HookStep]:
    """Every hook oah offers (PLAN 5.12) with the diff its install would make. Writes nothing."""
    root = pathlib.Path(root)
    steps = []
    backup_run = secrets.token_hex(16)
    settings_before, settings_data = None, None
    for kind in hooks.KINDS:
        if not hooks.hook_available(kind):
            steps.append(HookStep(kind, "unavailable", note=f"this oah version does not provide `oah hook {kind}` yet"))
            continue
        link = _linked(root, CLAUDE_SETTINGS) if kind in hooks.CLAUDE_KINDS else None
        if link is not None:
            steps.append(HookStep(kind, "refused", note=f"{link} is a link; oah never writes through links, which "
                                                        "could reach your global Claude Code settings"))
            continue
        try:
            problem = (_local_settings_problem(root, require_ignored=False) if kind in hooks.CLAUDE_KINDS
                       else _git_hook_problem(root, kind))
            if problem:
                steps.append(HookStep(kind, "refused", note=problem))
                continue
            path = root / CLAUDE_SETTINGS if kind in hooks.CLAUDE_KINDS else hooks.git_hook_path(root, kind)
            original = _read_user(root, path.relative_to(root).as_posix())
            if kind in hooks.CLAUDE_KINDS:
                if settings_data is None:
                    settings_before, settings_data = _hook_settings(original)
                unchanged = hooks._claude_installed(settings_data, kind)
            else:
                # Match the landed installer's encoding refusal, without forwarding user bytes.
                if original is not None:
                    original.decode("utf-8")
                unchanged = original == hooks.git_hook_text(kind).encode("utf-8")
                if not unchanged and original is not None and not force:
                    raise hooks.HookError("OAH_HOOK_FOREIGN", "a foreign hook requires --force")
                if not unchanged and original is not None and os.path.lexists(str(path) + hooks.BACKUP_SUFFIX):
                    raise hooks.HookError("OAH_HOOK_BACKUP_EXISTS", "move the legacy backup away and retry")
        except hooks.HookError as exc:
            if exc.code == "OAH_HOOK_FOREIGN":
                steps.append(HookStep(kind, "keep", note="a hook oah did not write is there; pass --force to replace "
                                                         "it (restore the private state-folder backup by hand "
                                                         "if you later remove the hook)"))
            else:
                steps.append(HookStep(kind, "refused", note=f"{exc.code}: the hook cannot be safely planned; "
                                      "check its settings and backup, then run oah init again"))
            continue
        except UnicodeDecodeError:
            steps.append(HookStep(kind, "refused", note="OAH_HOOKS_SETTINGS_INVALID: the hook is not UTF-8; "
                                  "save it as UTF-8, then run oah init again"))
            continue
        action = "installed" if unchanged else "install"
        diff, note, before, data, backup = "", "", None, None, ""
        if action == "install":
            before = original
            note = _replacement_note(before)
            diff = (_claude_hook_diff(kind) if kind in hooks.CLAUDE_KINDS else "".join(difflib.unified_diff(
                [], hooks.git_hook_text(kind).splitlines(keepends=True), fromfile="/dev/null",
                tofile=f"b/{_shown_path(root, path)} (canonical hook only)")))
            if kind in hooks.CLAUDE_KINDS:
                event, matcher = hooks.CLAUDE_KINDS[kind]
                group = {"matcher": matcher} if matcher else {}
                group["hooks"] = [{"type": "command", "command": hooks.command_for(kind)}]
                settings_data.setdefault("hooks", {}).setdefault(event, []).append(group)
                data = hooks._render(settings_data, settings_before).encode("utf-8")
                # Subsequent additions are planned against the preceding planned publication.
                before = next((item.data for item in reversed(steps) if item.kind in hooks.CLAUDE_KINDS
                               and item.action == "install"), before)
            else:
                data = hooks.git_hook_text(kind).encode("utf-8")
            if original is not None and (kind in hooks.GIT_KINDS or not any(
                    item.kind in hooks.CLAUDE_KINDS and item.action == "install" for item in steps)):
                backup = f"{backup_run}/{len(steps) + 1}"
                note += (f"; private backup position {len(steps) + 1} in <git common dir>/oah/backups/<run>/ "
                         f"(sha256 {hashlib.sha256(original).hexdigest()}); restore by hand if needed")
        steps.append(HookStep(kind, action, diff, _shown_path(root, path), note, before, data, backup))
    return steps


def _hook_settings(before: bytes | None) -> tuple[str | None, dict]:
    """Adapt the landed settings parser to bytes already read under init's no-follow rules."""
    try:
        text = before.decode("utf-8") if before is not None else None
        data = json.loads(text) if text is not None else {}
    except (UnicodeDecodeError, ValueError):
        raise hooks.HookError("OAH_HOOKS_SETTINGS_INVALID", "local settings must be UTF-8 JSON; "
                              "fix them by hand, then run oah init again") from None
    entries = data.get("hooks", {}) if isinstance(data, dict) else None
    if not (isinstance(entries, dict) and all(isinstance(groups, list) and all(
            isinstance(group, dict) for group in groups) for groups in entries.values())):
        raise hooks.HookError("OAH_HOOKS_SETTINGS_INVALID", "local settings have an invalid hooks object; "
                              "fix it by hand, then run oah init again")
    return text, data


def _backup_root(root: pathlib.Path, *, env: Mapping[str, str] | None = None) -> pathlib.Path:
    """Only Git's own state folder may hold preserved user bytes; never honor an in-tree state override."""
    if env is None:
        common = repo_info(root).common_dir.absolute()
    else:
        result = _unlazy_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env)
        if result.returncode != 0 or result.timed_out or not result.stdout.strip():
            raise OSError("Git state could not be located")
        common = pathlib.Path(result.stdout.strip()).absolute()
    # Refuse redirected Git metadata too, before handing its path to the publication function.
    if _linked(pathlib.Path(common.anchor), common.relative_to(common.anchor).as_posix()) is not None:
        raise OSError("linked Git state")
    if common.is_relative_to(root) and common.relative_to(root).parts[:1] != (".git",):
        raise OSError("Git state is inside the committable worktree")
    return common


def _backup_hook(root: pathlib.Path, step: HookStep) -> bool:
    try:
        return _publish(_backup_root(root), f"oah/backups/{step.backup}", step.before, private=True)
    except OSError:
        return False


def install_hooks(root: pathlib.Path, steps: list[HookStep], *, force: bool) -> tuple[list[str], list[str]]:
    """Install the planned hooks; (installed kinds, problems)."""
    root = pathlib.Path(root)
    installed, problems = [], []
    for step in steps:
        if step.action != "install":
            continue
        if step.kind in hooks.CLAUDE_KINDS and _linked(root, CLAUDE_SETTINGS) is not None:
            problems.append(f"OAH_INIT_CHANGED: {CLAUDE_SETTINGS} became a link after the plan was shown; "
                            f"the {step.kind} hook was not installed")
            continue
        try:
            problem = (_local_settings_problem(root, require_ignored=True) if step.kind in hooks.CLAUDE_KINDS
                       else _git_hook_problem(root, step.kind))
            if problem:
                problems.append(problem)
                continue
            path = (root / CLAUDE_SETTINGS if step.kind in hooks.CLAUDE_KINDS
                    else hooks.git_hook_path(root, step.kind))
            if step.path != _shown_path(root, path):
                problems.append(f"OAH_INIT_CHANGED: the {step.kind} hook destination changed; run oah init again")
                continue
            base = root
            relative = path.relative_to(base).as_posix()
            backup_note = (f" Inspect the planned backup at <git common dir>/oah/backups/{step.backup} "
                           f"(sha256 {hashlib.sha256(step.before).hexdigest()})." if step.backup else "")
            if step.before is not None:
                if step.kind in hooks.GIT_KINDS and not force:
                    problems.append("OAH_INIT_CHANGED: a foreign hook requires --force; run oah init again")
                    continue
                if step.kind in hooks.GIT_KINDS and os.path.lexists(str(path) + hooks.BACKUP_SUFFIX):
                    problems.append("OAH_INIT_CHANGED: a legacy hook backup appeared; move it away and run "
                                    "oah init again")
                    continue
                if step.backup and (not _backup_hook(root, step)):
                    problems.append("OAH_INIT_CHANGED: the private hook backup could not be published safely; "
                                    "check Git state permissions and run oah init again." + backup_note)
                    continue
            if step.data is None or not _publish(base, relative, step.data, step.before,
                                                 executable=step.kind in hooks.GIT_KINDS):
                problems.append(f"OAH_INIT_CHANGED: the {step.kind} hook changed; run oah init again." + backup_note)
                continue
        except hooks.HookError as exc:
            problems.append(f"{exc.code}: the {step.kind} hook was not installed; check its location and settings, "
                            "then run oah init again")
            continue
        installed.append(step.kind)
    return installed, problems


# unlazy -------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class UnlazyStep:
    action: str  # present | install | blocked (legacy replace plans are refused)
    folder: str = ""  # the copy discovery found, when there is one
    note: str = ""
    aside: str = ""  # retained for compatibility; init never moves an existing copy
    digest: str = ""
    backup: str = ""


def _unlazy_exists(relative: str) -> str:
    return (f"OAH_INIT_UNLAZY_EXISTS: an existing directory at {relative} cannot be replaced. "
            "Remove it yourself, then rerun oah init --with-unlazy")


def _verify_unlazy_pin(root: pathlib.Path, folder: pathlib.Path, pin: unlazy.Pin, *,
                       env: Mapping[str, str] | None = None) -> list[str]:
    """Adapt the landed pin check's project reads; external tool installations keep their landed check."""
    env = _unlazy_environment() if env is None else env
    if not folder.is_relative_to(root):
        problems = []
        for relative, digest in pin.files.items():
            try:
                if unlazy.file_digest(folder / relative) != digest:
                    problems.append("a script differs from the pin")
            except OSError:
                problems.append("a script is missing")
        top = _unlazy_git(folder, "rev-parse", "--show-toplevel", env=env)
        head = _unlazy_git(folder, "rev-parse", "--verify", "--quiet", "HEAD", env=env)
        if (top.returncode != 0 or not top.stdout.strip()
                or pathlib.Path(top.stdout.strip()).resolve() != folder.resolve()
                or head.returncode != 0 or head.stdout.strip() != pin.commit):
            problems.append("the checkout or commit does not match the pin")
        return problems
    relative_folder = folder.relative_to(root)
    try:
        with _parents(root, (relative_folder / ".init-pin").as_posix(), create_parents=False) as held:
            original = folder.lstat()

            def check_directory() -> None:
                actual = folder.lstat()
                pinned = os.fstat(held) if held is not None else original
                if (_linked(root, relative_folder.as_posix()) is not None
                        or (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino)):
                    raise OSError("project installation changed")

            check_directory()
            problems = []
            for relative, digest in pin.files.items():
                data = _read_user(root, (relative_folder / relative).as_posix())
                if data is None or hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() != digest:
                    problems.append("a script is missing or differs from the pin")
            check_directory()
            top = _unlazy_git(folder, "rev-parse", "--show-toplevel", env=env)
            head = _unlazy_git(folder, "rev-parse", "--verify", "--quiet", "HEAD", env=env)
            check_directory()
            if (top.returncode != 0 or not top.stdout.strip()
                    or pathlib.Path(top.stdout.strip()).absolute() != folder.absolute()
                    or head.returncode != 0 or head.stdout.strip() != pin.commit):
                problems.append("the checkout or commit does not match the pin")
            return problems
    except OSError:
        raise InitError("OAH_INIT_PATH_UNSAFE", "a project unlazy path is linked, changed or unreadable. "
                        "Remove links and junctions and check permissions, then run oah init again",
                        EXIT_ENVIRONMENT) from None


def _find_unlazy_dir(root: pathlib.Path, env: Mapping[str, str],
                     home: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """Keep discovery's lexical origin: project probes use the same pinned reads as verification."""
    override = env.get(unlazy.ENV_VAR)
    candidates = ([pathlib.Path(override)] if override else
                  [base / relative for base in (root, home)
                   for relative in (UNLAZY_PROJECT, ".claude/skills/unlazy")])
    for candidate in candidates:
        folder = pathlib.Path(os.path.abspath(candidate))
        if folder.is_relative_to(root):
            # Never resolve a project path into an external installation, even during a dry run.
            relative = (folder.relative_to(root) / "scripts/gate-check.mjs").as_posix()
            present = _read_user(root, relative) is not None
        else:
            present = unlazy._is_unlazy(folder)
            if present:
                folder = folder.resolve()
        if present:
            return folder, f"from {unlazy.ENV_VAR}" if override else "found"
    return None, (f"{unlazy.ENV_VAR} has no scripts/gate-check.mjs" if override else "unlazy not found")


def plan_unlazy(root: pathlib.Path, *, env: Mapping[str, str], home: pathlib.Path, pin: unlazy.Pin,
                force: bool) -> UnlazyStep:
    """Whether the pinned unlazy (5.8) is in place, and what installing the project copy would do."""
    root = pathlib.Path(os.path.abspath(root))
    target = root / UNLAZY_PROJECT
    for relative in (UNLAZY_PROJECT, ".claude/skills/unlazy"):
        if _linked(root, relative) is not None:
            return UnlazyStep("blocked", note="OAH_INIT_PATH_UNSAFE: a project unlazy path is a link; remove "
                                              "the link, then run "
                                              "oah init --with-unlazy again")
    folder, reason = _find_unlazy_dir(root, env, home)
    override = unlazy.ENV_VAR if env.get(unlazy.ENV_VAR) else None
    short = pin.commit[:12]
    if folder is None:
        if override:
            return UnlazyStep("blocked", note=f"{override} is set but names no unlazy ({reason}). Clear {override}, "
                                              "then run oah init --with-unlazy again")
        if target.exists() or target.is_symlink():
            return UnlazyStep("blocked", str(target), _unlazy_exists(UNLAZY_PROJECT))
        return UnlazyStep("install", note=f"downloads unlazy into {UNLAZY_PROJECT} at commit {short}")
    problems = _verify_unlazy_pin(root, folder, pin)
    if not problems:
        return UnlazyStep("present", str(folder), f"{folder} is the pinned commit {short}, with matching file digests")
    why = "its commit or script digests do not match the pin"
    if override:
        return UnlazyStep("blocked", str(folder), f"{override} names {folder}, which is not the pinned unlazy ({why}). "
                                                  f"Clear {override}, then run oah init --with-unlazy again")
    project = {root / UNLAZY_PROJECT, root / ".claude/skills/unlazy"}
    if folder in project:
        where = _shown_path(root, folder)
        return UnlazyStep("blocked", str(folder), _unlazy_exists(where))
    return UnlazyStep("install", str(folder), f"your copy at {folder} is not the pinned one ({why}); init installs "
                                              f"the pinned project copy into {UNLAZY_PROJECT}, which oah uses first")


def unlazy_commands(target: pathlib.Path, *, source: str, pin: unlazy.Pin) -> list[list[str]]:
    """unlazy.install_commands with git resolved through oah.paths and the clone source swappable."""
    commands = unlazy.install_commands(target, pin)
    if commands[0][:4] != ["git", "clone", "--quiet", unlazy.UPSTREAM_GIT]:
        raise RuntimeError("unlazy.install_commands no longer starts with the clone oah.init expects; update it")
    commands[0][3] = source
    commands[0] = ["git", "-C", str(target), *commands[0][1:-1], "."]
    commands[1][3:3] = ["--git-dir=.git", "--work-tree=."]
    return [[*git_argv(), *UNLAZY_GIT_OPTIONS, *argv[1:]] for argv in commands]


def _unlazy_environment() -> dict[str, str]:
    """Snapshot only executable lookup, home, locale and temporary-directory settings."""
    allowed = {"PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LANG", "LANGUAGE",
               "TEMP", "TMP", "TMPDIR"}
    return {key: os.environ[key] for key in os.environ
            if key.upper() in allowed or key.upper().startswith("LC_")}


def _unlazy_git(folder: pathlib.Path, *args: str, env: Mapping[str, str] | None = None) -> proc.RunResult:
    return proc.run_command([*git_argv(), *UNLAZY_GIT_OPTIONS, "-C", str(folder), *args],
                            timeout=30.0, env=_unlazy_environment() if env is None else env)


def _rename(source: pathlib.Path, target: pathlib.Path) -> None:
    deadline = time.monotonic() + RENAME_RETRY_SECONDS
    while True:
        try:
            os.rename(source, target)
            return
        except PermissionError:  # Windows: an indexer or antivirus holds a file for a moment
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _staging_command(path: str, expected: str, argv: list[str]) -> int:
    """Child entry: bind Git's working directory before it can write, without a pathname race."""
    try:
        with _pinned_directory(pathlib.Path(path), private_staging=True) as (handle, identity):
            if identity != expected:
                return 125
            if os.name == "nt":
                import subprocess

                previous = os.getcwd()
                try:
                    os.chdir(path)
                    _require_staging(pathlib.Path.cwd(), expected, private_staging=True)
                    return subprocess.call(argv, cwd=".")
                finally:
                    os.chdir(previous)
            os.fchdir(handle)
            os.execvp(argv[0], argv)  # Git uses only '.'; a rename cannot redirect its working directory
    except OSError:
        return 125


def _require_staging(path: pathlib.Path, expected: str, *, private_staging: bool = False) -> None:
    with _pinned_directory(path, private_staging=private_staging) as (_handle, identity):
        if identity != expected:
            raise OSError("staging directory changed")


def _staged_files(path: pathlib.Path, handle: int) -> tuple[dict[str, tuple[bytes, bool]], list[str]]:
    """Capture regular files and directory names from the held source; never reopen them at publication."""
    files = {}
    directories = []

    def visit(folder: pathlib.Path, relative: str, descriptor: int) -> None:
        names = sorted(os.listdir(descriptor) if os.name != "nt" else os.listdir(folder))
        for name in names:
            if not relative and name == ".git":
                continue  # No captured Git metadata is approved for publication.
            child = _relative(f"{relative}/{name}" if relative else name)
            if os.name == "nt":
                # The caller holds a non-reparse source identity and rechecks it at publication.
                with _parents(path, child, create_parents=False):
                    item = path / child
                    if _is_link(item):
                        raise OSError("linked staged entry")
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        directories.append(child)
                        with _pinned_directory(item) as (held, _identity):
                            visit(item, child, held)
                    elif stat.S_ISREG(info.st_mode):
                        data = _read_user(path, child)
                        if data is None:
                            raise OSError("staged entry disappeared")
                        files[child] = data, bool(info.st_mode & stat.S_IXUSR)
                    else:
                        raise OSError("non-regular staged entry")
            else:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                try:
                    info = os.fstat(fd)
                    if stat.S_ISDIR(info.st_mode):
                        directories.append(child)
                        visit(folder / name, child, fd)
                    elif stat.S_ISREG(info.st_mode):
                        with os.fdopen(os.dup(fd), "rb") as stream:
                            files[child] = stream.read(), bool(info.st_mode & stat.S_IXUSR)
                    else:
                        raise OSError("non-regular staged entry")
                finally:
                    os.close(fd)

    visit(path, "", handle)
    return files, directories


def install_unlazy(root: pathlib.Path, step: UnlazyStep, *, source: str = unlazy.UPSTREAM_GIT,
                   pin: unlazy.Pin = unlazy.PIN, timeout: float = CLONE_TIMEOUT) -> str:
    """Clone and verify in private Git state, then publish the captured files individually.

    Returns "" on success, else the problem. Existing project directories require manual removal.
    Staging is retained for manual cleanup; init never recursively deletes a replaceable pathname."""
    root = pathlib.Path(root)
    environment = _unlazy_environment()
    try:
        if step.action == "replace" or step.note.startswith("OAH_INIT_UNLAZY_EXISTS:"):
            relative = (".claude/skills/unlazy" if pathlib.Path(step.folder) == root / ".claude/skills/unlazy"
                        else UNLAZY_PROJECT)
            return _unlazy_exists(relative)
        if step.action != "install":
            return "OAH_INIT_UNLAZY_FAILED: installation was not planned; remove any links and run oah init again"
        for relative in (UNLAZY_PROJECT, ".claude/skills/unlazy"):
            if _linked(root, relative) is not None:
                raise OSError("linked installation path")
        # Ignore OAH_STATE_DIR: external tree writers must stay in Git's own private metadata storage.
        common = _backup_root(root, env=environment)
        staging_relative = f"oah/staging/{secrets.token_hex(16)}"
        staging = common / staging_relative
        with _parents(common, staging_relative, private=True) as parent:
            if os.name == "nt":
                _private_state_directory(staging, create=True)
            else:
                os.mkdir(staging.name, 0o700, dir_fd=parent)
                _private_state_directory(staging)
            try:
                with _pinned_directory(staging, private_staging=True) as (_handle, identity):
                    for operation, argv in zip(("clone", "checkout"), unlazy_commands(staging, source=source, pin=pin),
                                                strict=True):
                        argv[len(git_argv()) + len(UNLAZY_GIT_OPTIONS) + 1] = "."
                        _require_staging(staging, identity, private_staging=True)
                        # The child reopens without following links, checks the held identity and fchdirs on
                        # POSIX. Windows binds and verifies its cwd before starting the external command.
                        command = [sys.executable, "-I", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                                   "from oah.init import _staging_command; "
                                   "sys.exit(_staging_command(sys.argv[2], sys.argv[3], sys.argv[4:]))",
                                   str(pathlib.Path(__file__).resolve().parent.parent), str(staging), identity, *argv]
                        result = run_command(command, timeout=timeout, env=environment)
                        _require_staging(staging, identity, private_staging=True)
                        if result.timed_out or result.returncode != 0:
                            detail = f"exit code {result.returncode}; timed out: {'yes' if result.timed_out else 'no'}"
                            return (f"OAH_INIT_UNLAZY_FAILED: git {operation} failed ({detail}). Check your "
                                    "network and git, then run oah init --with-unlazy again")
                    with _pinned_directory(staging, private_staging=True) as (_verification_handle, actual):
                        if actual != identity:
                            raise OSError("staging directory changed")
                        if _verify_unlazy_pin(staging, staging, pin, env=environment):
                            return ("OAH_INIT_UNLAZY_FAILED: the downloaded copy is not the pinned unlazy; check the "
                                    "source and pin, then run oah init --with-unlazy again. Nothing was installed")
                        files, directories = _staged_files(staging, _verification_handle)
                        # The landed verifier only needs a detached HEAD and a recognizable checkout.
                        # Never publish objects (commit authors), config/remotes, index, refs or reflogs.
                        head = _read_user(staging, ".git/HEAD")
                        files[".git/HEAD"] = head or b"", False
                        files[".git/config"] = b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n", False
                        directories += [".git", ".git/objects", ".git/refs"]
                        # Verify the bytes we will actually publish, including the detached checkout identity.
                        if (files.get(".git/HEAD", (b"", False))[0].replace(b"\r\n", b"\n")
                                != (pin.commit + "\n").encode("ascii") or any(
                                name not in files or hashlib.sha256(files[name][0].replace(b"\r\n", b"\n"))
                                .hexdigest() != digest for name, digest in pin.files.items())):
                            raise OSError("captured installation does not match the pin")
                        _require_staging(staging, identity, private_staging=True)
                        if not _publish(root, UNLAZY_PROJECT, None):
                            raise OSError("installation destination changed")
                        for name in directories:
                            _require_staging(staging, identity, private_staging=True)
                            if not _publish(root, f"{UNLAZY_PROJECT}/{name}", None):
                                raise OSError("installation directory changed")
                        # The landed verifier needs a checkout of its own. Publish its detached HEAD last,
                        # so a partial install cannot be discovered as the completed pinned checkout.
                        for name in sorted(files, key=lambda name: (name == ".git/HEAD", name)):
                            data, executable = files[name]
                            _require_staging(staging, identity, private_staging=True)
                            if not _publish(root, f"{UNLAZY_PROJECT}/{name}", data, executable=executable):
                                raise OSError("installation file changed")
                        _require_staging(staging, identity, private_staging=True)
                        if _verify_unlazy_pin(root, root / UNLAZY_PROJECT, pin, env=environment):
                            raise OSError("published installation does not match the pin")
                        return ""
            except (OSError, InitError):
                return ("OAH_INIT_CHANGED: the unlazy source or destination changed; inspect the project copy "
                        "and any private Git state backup. Staging remains in <git common dir>/oah/staging/ "
                        "for manual cleanup; run oah init --with-unlazy again after resolving the conflict")
    except (OSError, InitError):
        return ("OAH_INIT_UNLAZY_FAILED: an installation path changed, is linked or cannot be written safely. "
                "Remove links and check permissions, then run oah init --with-unlazy again")


# The CI starter ------------------------------------------------------------------------------------------

def _ci_plan(root: pathlib.Path, oses, test_command) -> ci.CiPlan:
    """Use the landed detectors/rendering with a path adapter for their project input reads."""
    detector = importlib.import_module("oah.ci.detect")

    class InputPath(type(root)):
        def read_bytes(self):
            data = _read_user(root, self.relative_to(root).as_posix())
            if data is None:
                raise FileNotFoundError("missing project input")
            return data

    found = (tuple(item for item in (detector._python(InputPath(root)), detector._node(InputPath(root)),
                                     detector._go(InputPath(root)), detector._rust(InputPath(root)))
                   if item is not None)
             if test_command is None or isinstance(test_command, str) or test_command is ci.PLACEHOLDER else ())
    chosen = ci.parse_oses(oses) if oses else (ci.current_os(),)
    existing_bytes = _read_user(root, ci.WORKFLOW_PATH.as_posix())
    try:
        existing = None if existing_bytes is None else existing_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ci.CiError("OAH_CI_TARGET_INVALID", "the workflow must be UTF-8; fix it and retry") from None
    detection = None
    if isinstance(test_command, str) or test_command is ci.PLACEHOLDER:
        steps = (ci.Step("Run tests", ci.check_test_command(test_command)),) if isinstance(test_command, str) else ()
        setups = [item.ecosystem for item in found]
    else:
        if test_command is None and len(found) > 1:
            raise ci.CiError("OAH_CI_AMBIGUOUS", "several ecosystems need an explicit choice; run oah ci init")
        detection = test_command or (found[0] if found else None)
        steps = detection.steps if detection else ()
        setups = [detection.ecosystem] if detection else []
    return ci.CiPlan(root, root / ci.WORKFLOW_PATH, chosen, detection, steps, not steps,
                     ci.render(chosen, setups, steps), existing)


@dataclasses.dataclass(frozen=True)
class CiStep:
    action: str  # create | replace | keep | unchanged | skipped
    text: str = ""
    diff: str = ""
    note: str = ""
    plan: ci.CiPlan | None = None


def plan_ci(root: pathlib.Path, *, force: bool, oses: list[str], test_command) -> CiStep:
    """The starter workflow (A21) through oah.ci: shown in full, written only on the user's answer."""
    try:
        found = _ci_plan(pathlib.Path(root), oses or None, test_command)
    except ci.CiError as exc:
        if exc.code == "OAH_CI_AMBIGUOUS":
            return CiStep("skipped", note="this project matches several kinds of tests; run `oah ci init` to pick one")
        return CiStep("skipped", note=f"{exc.code}: the workflow cannot be safely planned; check its path and "
                      "project configuration, then run oah ci init")
    if found.up_to_date:
        return CiStep("unchanged", found.text, plan=found)
    if found.existing is not None:
        note = _replacement_note(found.existing.encode("utf-8"))
        diff = "".join(difflib.unified_diff([], found.text.splitlines(keepends=True), fromfile="/dev/null",
                                          tofile=f"b/{ci.WORKFLOW_PATH} (canonical workflow only)"))
        if force:
            return CiStep("replace", found.text, diff, note, found)
        return CiStep("keep", found.text, diff, note + "; run `oah ci init` to review the change, or pass --force", found)
    note = "its 'Run tests (edit me)' step fails until you put your test command in it" if found.placeholder else ""
    return CiStep("create", found.text, note=note, plan=found)


def write_ci(step: CiStep) -> str:
    """Write the planned workflow; "" on success, else the problem (the file changed since it was shown)."""
    found = step.plan
    if found is None or step.action not in ("create", "replace"):
        return "OAH_INIT_CHANGED: no CI publication was planned; run oah init again"
    before = found.existing.encode("utf-8") if found.existing is not None else None
    if not _publish(found.root, ci.WORKFLOW_PATH, step.text.encode("utf-8"), before):
        return "OAH_INIT_CHANGED: the CI workflow or its parents changed; run oah init again"
    return ""


# The impact map ------------------------------------------------------------------------------------------

def map_state(root: pathlib.Path, env: Mapping[str, str]) -> tuple[str, str]:
    """(missing | stale | fresh | unavailable, reason). Reads only."""
    try:
        info, reason = mapstore.status(pathlib.Path(root), env=env)
    except MAP_FAILURES as exc:
        return "unavailable", _map_problem(exc)
    if info is None:
        return "missing", reason
    return info.action, reason


def refresh_map(root: pathlib.Path, env: Mapping[str, str]) -> tuple[str, str]:
    """Build or refresh the persisted worktree map (A11): (built | refreshed | fresh | unavailable, detail)."""
    try:
        info = mapstore.ensure_fresh(pathlib.Path(root), env=env)
    except MAP_FAILURES as exc:
        return "unavailable", _map_problem(exc)
    return info.action, f"commit={info.commit[:12]} result={info.result_digest[:12]}"


def _map_problem(exc: Exception) -> str:
    if getattr(exc, "code", None) == "OAH_IMPACT_NO_COMMIT":
        return "OAH_IMPACT_NO_COMMIT: make a first commit, then run oah impact build."
    return "OAH_INIT_IMPACT_UNAVAILABLE: impact map checking or building failed; run oah impact build to retry."


def origin_url(root: pathlib.Path) -> str | None:
    """The origin remote's URL, used only to name the project for Orca. Never printed or stored."""
    result = run_git(pathlib.Path(root), "remote", "get-url", "origin")
    return (result.stdout.strip() or None) if result.returncode == 0 else None


# The machine --------------------------------------------------------------------------------------------

def _input(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        return None


def _quiet_run(argv, *, operation: str, cwd: pathlib.Path | None = None) -> int:
    """External installers never inherit output streams; report only fixed operation and result metadata."""
    code, timed_out = None, False
    try:
        resolved = resolve_command(argv[0])
        if resolved is not None:
            result = run_command(resolved.argv + tuple(argv[1:]), timeout=CLONE_TIMEOUT,
                                 cwd=str(cwd) if cwd is not None else None,
                                 env=dict(os.environ, UV_NO_MODIFY_PATH="1"))
            code, timed_out = result.returncode, result.timed_out
    except (OSError, UnsupportedBatchShim):
        pass
    if code == 0 and not timed_out:
        return 0
    print(f"OAH_INIT_TOOL_FAILED: {operation} failed (exit code {code}; timed out: {'yes' if timed_out else 'no'}). "
          "Check the tool in your own terminal, then run oah init again")
    return 1


@dataclasses.dataclass
class Machine:
    """Everything init touches outside the project: tests swap each one for a fake."""
    which: Callable[..., str | None]
    install_runner: Callable[[tuple[str, ...]], int]
    status_runner: Callable[[tuple[str, ...]], tuple[int | None, str]]
    refresh: Callable[[], None]
    companion_runner: Callable[[tuple[str, ...], pathlib.Path], int]
    companion_probe: Callable[[tuple[str, ...], pathlib.Path], int | None]
    ask: Callable[[str], str | None]
    interactive: bool
    env: Mapping[str, str]
    home: pathlib.Path
    os_name: str  # macos | windows: which official install commands the tool catalog offers
    companion_os: str  # macos | windows | linux
    today: dt.date
    unlazy_source: str = unlazy.UPSTREAM_GIT
    unlazy_pin: unlazy.Pin = unlazy.PIN

    @classmethod
    def real(cls) -> "Machine":
        return cls(which=shutil.which, install_runner=lambda argv: _quiet_run(argv, operation="tool installation"),
                   status_runner=guide.default_status_runner, refresh=guide.refresh_path,
                   companion_runner=lambda argv, cwd: _quiet_run(argv, operation="companion setup", cwd=cwd),
                   companion_probe=companion_probe, ask=_input,
                   interactive=sys.stdin is not None and sys.stdin.isatty(), env=os.environ, home=pathlib.Path.home(),
                   os_name=os_name_for(sys.platform), companion_os=companions.os_for(sys.platform),
                   today=dt.date.today())


def _no_answer(prompt: str) -> None:
    """The `ask` of a run without a terminal: every question stays unanswered; stdin is never read."""
    return None


# The plan -----------------------------------------------------------------------------------------------

def _companion_steps(apps, *, os_name: str, root: pathlib.Path) -> tuple[companions.CompanionStep, ...]:
    steps = companions.plan_steps(apps, os_name=os_name, project_root=root, remote_url=origin_url(root))
    found = []
    for step in steps:
        check = None
        if step.key == "orca-skills":
            check = ("orca", "skills", "installed", "--json")
        elif step.key == "orca-project" and step.argv is not None:
            project = step.argv[step.argv.index("--project") + 1]
            check = ("orca", "project", "setups", "--project", project, "--host", "local", "--json")
        if check is not None:
            step = dataclasses.replace(step, skip_if=check, verify=check,
                                       already="the requested Orca setup is already present")
        found.append(step)
    return tuple(found)


def companion_probe(argv, cwd: pathlib.Path) -> int | None:
    """Init's Orca presence checks: 0 present, 1 missing, 2 unverified; discard all tool output."""
    if argv[:3] not in (("orca", "skills", "installed"), ("orca", "project", "setups")):
        return companions.default_probe(argv, cwd)

    def read(command):
        try:
            resolved = resolve_command(command[0])
            if resolved is None:
                return None
            result = run_command(resolved.argv + tuple(command[1:]), timeout=companions.PROBE_TIMEOUT, cwd=str(cwd))
            if result.returncode != 0 or result.timed_out:
                return None
            data = json.loads(result.stdout)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, UnsupportedBatchShim):
            return None

    data = read(argv)
    if data is None or data.get("ok") is not True or not isinstance(data.get("result"), dict):
        return 2
    if argv[1] == "skills":
        bundled = read(("orca", "skills", "list", "--json"))
        topics = bundled.get("topics") if bundled else None
        installed = data["result"].get("skills")
        if not isinstance(topics, list) or not topics or not isinstance(installed, list):
            return 2
        if any(not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]
               for row in (*topics, *installed)):
            return 2
        wanted = {row["name"] for row in topics}
        return 0 if wanted <= {row["name"] for row in installed} else 1
    setups = data["result"].get("setups")
    if not isinstance(setups, list):
        return 2
    if not setups:
        return 1
    project = argv[argv.index("--project") + 1]
    for row in setups:
        if not isinstance(row, dict) or not all(isinstance(row.get(key), str)
                                              for key in ("projectId", "hostId", "path", "setupState")):
            return 2
        if (row["projectId"].casefold() == project.casefold() and row["hostId"] == "local"
                and os.path.normcase(os.path.abspath(row["path"])) == os.path.normcase(os.path.abspath(cwd))
                and row["setupState"] == "ready"):
            return 0
    return 2  # a conflicting or unfinished registration needs a human; never replace it automatically


def recorded_answers(config: Config) -> dict[str, bool]:
    """The Jev and skill-update answers an existing .oah/config.toml records."""
    chosen = config.section("rerank", rerank.DEFAULTS)
    skills = config.section("skills", skillsel.config_defaults())
    return {"jev": chosen["enabled"] is True and chosen["provider"] == "typesafe",
            "skill_updates": skills["auto_update"] is True}


@dataclasses.dataclass(frozen=True)
class Plan:
    root: pathlib.Path
    state: pathlib.Path
    answers: dict[str, Answer]
    notes: tuple[str, ...]
    branch: str
    how: str
    wiki: WikiSettings
    tools: tuple[Tool, ...]
    tool_steps: tuple[guide.Step, ...]
    files: tuple[FileChange, ...]
    rules: RulesStep
    hooks: tuple[HookStep, ...]
    unlazy: UnlazyStep | None
    ci: CiStep | None
    apps: tuple[str, ...]
    companion_steps: tuple[companions.CompanionStep, ...]
    map: tuple[str, str]  # map_state before anything is written
    unlazy_runs: tuple[str, ...] = ()  # the commands an unlazy install runs, as shown
    aside_local: FileChange | None = None
    aside_rules: RulesStep | None = None

    @property
    def changes(self) -> int:
        """Planned writes and installs; companion steps are checks that act only when something is missing."""
        count = sum(1 for change in self.files if change.action in WRITES)
        rules_step = self.aside_rules or self.rules
        count += (rules_step.agents != "unchanged") + (rules_step.claude != "unchanged")
        count += int(self.aside_local is not None and self.aside_local.action in WRITES)
        count += sum(1 for step in self.hooks if step.action == "install")
        count += int(self.unlazy is not None and self.unlazy.action in ("install", "replace"))
        count += int(self.ci is not None and self.ci.action in ("create", "replace"))
        return count + sum(1 for step in self.tool_steps if not step.manual)

    @property
    def map_action(self) -> str:
        state, _reason = self.map
        if state == "unavailable":
            return "unavailable"
        if state == "missing":
            return "build"
        return "refresh" if state == "stale" or self.changes else "fresh"


def build_plan(root: pathlib.Path, answers: dict[str, Answer], *, given: Mapping[str, bool | None], machine: Machine,
               lang: str | None, force: bool, existing: Config | None) -> Plan:
    """Everything init would do, from read-only lookups. `existing` is the project's config, or None when
    --force replaces one that does not parse."""
    root = pathlib.Path(root)
    state = _wiki_state_path(root, state_dir(repo_info(root), machine.env) / "wiki").parent
    values = {key: answer.value for key, answer in answers.items()}
    branch, how = default_branch(root)
    config = render_config(branch=branch, how=how, jev=values["jev"], skill_updates=values["skill_updates"],
                           today=machine.today).encode("utf-8")
    has_config = existing is not None and existing.path is not None
    effective = existing if has_config and not force else parse_config(config)
    engines = effective.section("engines", call.ENGINE_DEFAULTS)
    wiki = WikiSettings.from_mapping(effective.section("wiki", WIKI_DEFAULTS))
    wiki = dataclasses.replace(wiki, dir=_wiki_dir(root, wiki.dir))
    ci_section = effective.section("ci", ci.DEFAULTS)
    apps = tuple(app for app in companions.APPS if values[app])
    tools = tuple(tool for tool in catalog() if tool.name != "unlazy" and (not tool.optional or tool.name in apps))
    statuses = guide.check_tools(tools, which=machine.which, env=machine.env, home=machine.home, repo_root=root)
    unlazy_step = (plan_unlazy(root, env=machine.env, home=machine.home, pin=machine.unlazy_pin, force=force)
                   if values["unlazy"] else None)
    unlazy_copy = unlazy_step is not None and (unlazy_step.action in ("install", "replace") or (
        unlazy_step.action == "present"
        and pathlib.Path(unlazy_step.folder).resolve() == (root / UNLAZY_PROJECT).resolve()))
    files = plan_files(root, config=config, wiki_dir=wiki.dir, agents=agent_files(engines) if values["agents"] else {},
                       graph=values["graph"], unlazy_copy=unlazy_copy, force=force, today=machine.today)
    rules_step = plan_rules(root, lang=lang)
    aside_local, aside_rules = _plan_aside(root, rules_step) if "aside" in apps else (None, None)
    notes_found = []
    for question in QUESTIONS:
        answer, flag = answers[question.key], given.get(question.key)
        if answer.source == "config" and flag is not None and flag != answer.value:
            used = question.flag_yes if flag else question.flag_no
            notes_found.append(f"{used} was not applied: .oah/config.toml already records this answer, and init "
                               "does not edit an existing config. Edit the file, or pass --force to rewrite it.")
    return Plan(
        root=root, state=state, answers=answers, notes=tuple(notes_found),
        branch=branch, how=how, wiki=wiki, tools=tools,
        tool_steps=tuple(guide.plan_installs(statuses, os_name=machine.os_name, which=machine.which)),
        files=tuple(files), rules=rules_step, aside_local=aside_local, aside_rules=aside_rules,
        hooks=tuple(plan_hooks(root, force=force)) if values["hooks"] else (), unlazy=unlazy_step,
        # Existing command text is private (J/A17). Let CI detect its own command or use its fixed placeholder.
        ci=plan_ci(root, force=force, oses=list(ci_section["os"]), test_command=None)
        if values["ci"] else None,
        apps=apps, companion_steps=_companion_steps(apps, os_name=machine.companion_os, root=root) if apps else (),
        map=map_state(root, machine.env),
        unlazy_runs=tuple(" ".join(["git", *argv[len(git_argv()):]]) for argv in unlazy_commands(
            pathlib.Path("<git common dir>/oah/staging/<run>"), source=machine.unlazy_source, pin=machine.unlazy_pin))
        if unlazy_step is not None and unlazy_step.action in ("install", "replace") else ())


def _indented(text: str, prefix: str) -> list[str]:
    return [f"{prefix}{line}" for line in text.rstrip("\n").split("\n")] if text else []


def render_plan(plan: Plan) -> list[str]:
    """The plan as printed, the same for --dry-run and a real run."""
    lines = [f"Plan for {plan.root}:"]
    names = {question.key: question.key.replace("_", "-") for question in QUESTIONS}
    lines += _wrap("  Answers: ", ", ".join(f"{names[key]} {'yes' if answer.value else 'no'}"
                                            for key, answer in plan.answers.items()))
    sources = []
    for source, label in (("flag", "your flags"), ("asked", "your answers"), ("config", ".oah/config.toml"),
                          ("default", "defaults, which a real run asks about")):
        keys = [names[key] for key, answer in plan.answers.items() if answer.source == source]
        if keys:
            sources.append(f"{label} ({', '.join(keys)})")
    lines += _wrap("  From: ", "; ".join(sources))
    lines += [f"  Note: {note}" for note in plan.notes]
    lines.append("Tools (each installs only after you confirm it, or with --yes; an app you chose above needs no "
                 "second answer):")
    if not plan.tool_steps:
        lines.append("  - every tool oah uses is installed")
    for step in plan.tool_steps:
        how = (f"install it yourself from {step.url}" if step.manual else step.display)
        lines.append(f"  - {step.tool.name} ({step.tool.requirement}: {step.tool.purpose}): {how}")
        if step.note:
            lines.append(f"      {step.note}")
    lines.append("Sign-ins (each tool's own status command; oah never sees a password, key or token):")
    for tool in plan.tools:
        if tool.sign_in is not None:
            status = display((tool.check[0],) + tool.sign_in.status_args)
            lines.append(f"  - {tool.name}: checked with `{status}`; when you are signed out, init shows "
                         f"`{tool.sign_in.login}` to run in your own terminal")
    lines.append("Files:")
    for change in plan.files:
        if change.group != "gitignore":
            lines.append(_file_line(change))
    lines.append("Managed content (changed in place; everything else in these files stays as it is):")
    for name, action in (("AGENTS.md", plan.rules.agents), ("CLAUDE.md", plan.rules.claude)):
        what = (f"the oah rules block ({plan.rules.lang}, {plan.rules.block_bytes} bytes)" if name == "AGENTS.md"
                else "the @AGENTS.md import line")
        lines.append(f"  {action:<10} {name}: {what}")
    lines += [_file_line(change) for change in plan.files if change.group == "gitignore"]
    if "aside" in plan.apps:
        lines.append(f"  if Aside gets registered, its web rules go into {rules.LOCAL_RULES} and the AGENTS.md block")
        lines.append(_file_line(plan.aside_local))
        lines.append(f"  {plan.aside_rules.agents:<10} AGENTS.md: conditional Aside block "
                     f"({plan.aside_rules.block_bytes} bytes); original planned file required")
    lines.append("Hooks:")
    if not plan.answers["hooks"].value:
        lines.append("  none chosen; `oah hooks install <kind>` adds one later")
    for step in plan.hooks:
        where = f" ({step.path})" if step.path else ""
        lines.append(f"  {step.action:<10} {step.kind}{where}{': ' + step.note if step.note else ''}")
        lines += _indented(step.diff, "             | ")
    lines.append("unlazy:")
    if plan.unlazy is None:
        lines.append("  not chosen; gate commands need it (`oah init --with-unlazy` installs it later)")
    else:
        lines.append(f"  {plan.unlazy.action:<10} {plan.unlazy.note}")
        if plan.unlazy.backup:
            lines.append(f"             backup position unlazy (sha256 {plan.unlazy.digest}); restore by hand if needed")
        for command in plan.unlazy_runs:
            lines.append(f"             runs: {command}")
        if plan.unlazy_runs:
            lines.append("             (private 0700 Git state staging; verified files are published individually. "
                         "Staging is retained in <git common dir>/oah/staging/ for manual cleanup)")
    lines.append("CI starter:")
    if plan.ci is None:
        lines.append("  not chosen; `oah ci init` writes it later")
    else:
        lines.append(f"  {plan.ci.action:<10} {ci.WORKFLOW_PATH}{': ' + plan.ci.note if plan.ci.note else ''}")
        if plan.ci.action in ("create", "replace"):
            lines += _indented(plan.ci.diff if plan.ci.action == "replace" else plan.ci.text, "             | ")
    lines.append("Companion apps:")
    if not plan.apps:
        lines.append("  none chosen")
    for number, step in enumerate(plan.companion_steps, 1):
        label = " [global: changes files in your user folder]" if step.global_write else ""
        lines.append(f"  {number}. {step.app}: {step.title}{label}; asks first")
        if step.argv is not None:
            lines += _indented(step.display, "       runs: ")
        else:
            lines.append(f"       you do it: {step.instruction}")
        lines.append(f"       source: {step.source}")
        if step.key in ("orca-skills", "orca-project") and step.skip_if is not None:
            lines.append(f"       check first: {companions.show(step.skip_if)}; skip when already present; "
                         "leave pending if unverified")
    jev = plan.answers["jev"].value
    lines.append("Settings:")
    lines.append(f"  default branch: {plan.branch} ({plan.how})")
    lines.append(f"  Jev re-ranker: {'on, with your consent' if jev else 'off'}")
    updates = plan.answers["skill_updates"].value
    lines.append(f"  skill updates: {'on, checked at most once a day' if updates else 'off'}")
    state, reason = plan.map
    action = plan.map_action
    lines.append("Impact map:")
    lines.append({"build": "  build the first map after everything above is written",
                  "refresh": f"  refresh it after everything above is written ({reason})",
                  "fresh": "  fresh; nothing to do",
                  "unavailable": f"  unavailable: {reason} Init still finishes the rest."}[action])
    return lines


def _file_line(change: FileChange) -> str:
    return f"  {change.action:<10} {change.path}{': ' + change.note if change.note else ''}"


def _wrap(prefix: str, text: str, width: int = 110) -> list[str]:
    words, lines, current = text.split(" "), [], prefix
    for word in words:
        if len(current) + len(word) > width and current.strip():
            lines.append(current.rstrip())
            current = " " * len(prefix)
        current += word + " "
    lines.append(current.rstrip())
    return lines


# Applying the plan --------------------------------------------------------------------------------------

@dataclasses.dataclass
class Outcome:
    changed: int = 0
    pending: list[str] = dataclasses.field(default_factory=list)
    impact: str = ""
    impact_detail: str = ""


def _confirm_tool(step: guide.Step, machine: Machine, yes: bool, out) -> bool:
    if step.tool.optional:
        return True  # the user chose this app with its own question
    if yes:
        return True
    if not machine.interactive:
        return False
    out(f"Install {step.tool.name} ({step.tool.requirement}: {step.tool.purpose})?")
    out(f"  This runs: {step.display}")
    out(f"  Official page: {step.url}")
    reply = machine.ask("  Install now? [y/N] ")
    return reply is not None and reply.strip().lower() in ("y", "yes")


def apply_plan(plan: Plan, machine: Machine, *, yes: bool, force: bool, out) -> Outcome:
    """Do the plan in order: tools, sign-ins, files, managed content, hooks, CI, unlazy, companion apps, map."""
    root = plan.root
    outcome = Outcome()
    ask = machine.ask if machine.interactive else _no_answer
    if plan.tool_steps:
        out("")
        out("Tools:")
        results = guide.run_installs(plan.tool_steps, confirm=lambda step: _confirm_tool(step, machine, yes, out),
                                     runner=machine.install_runner, refresh=machine.refresh, out=out)
        outcome.changed += sum(1 for result in results if result.status == "installed")
    statuses = guide.check_tools(plan.tools, which=machine.which, env=machine.env, home=machine.home, repo_root=root)
    out("")
    out("Sign-in:")
    signed_out = guide.walk_sign_ins(statuses, status_runner=machine.status_runner,
                                     interactive=machine.interactive and not yes, ask=ask, out=out)
    outcome.pending += [status.tool.name for status in statuses if status.tool.required and not status.present]
    outcome.pending += [f"{status.tool.name}:sign-in" for status in statuses
                        if status.tool.required and status.tool.name in signed_out]
    written, problems = write_files(root, list(plan.files), wiki=Wiki(root, plan.wiki, plan.state))
    try:
        # Aside chooses between two preconfirmed alternatives after registration, publishing rules once.
        synced = (_sync_rules(root, plan.rules) if "aside" not in plan.apps
                  else rules.SyncResult(plan.rules.lang, (), 0, plan.rules.block_bytes))
    except InitError as exc:
        problems.append(str(exc))
        synced = rules.SyncResult(plan.rules.lang, (), 0, plan.rules.block_bytes)
    if written or problems or synced.changed:
        out("")
        out("Files:")
    for change in written:
        out(f"  {VERBS[change.action]} {change.path}")
    for name in synced.changed:
        out(f"  updated {name} (the oah rules block)" if name == "AGENTS.md" else f"  updated {name} (@AGENTS.md)")
    blocked = [change for change in plan.files if change.action == "blocked"]
    if blocked and not (written or problems or synced.changed):
        out("")
        out("Files:")
    for change in blocked:
        out(f"  not written: {change.path}: {change.note}")
    for problem in problems:
        out(f"  {problem}")
    outcome.changed += len(written) + len(synced.changed)
    if problems or blocked:
        outcome.pending.append("files")
    installed, problems = install_hooks(root, list(plan.hooks), force=force)
    problems += [f"hook {step.kind} was not installed: {step.note}" for step in plan.hooks if step.action == "refused"]
    if installed or problems:
        out("")
        out("Hooks:")
    for kind in installed:
        out(f"  installed hook {kind}")
    for step in plan.hooks:
        if step.kind in installed and step.backup:
            out(f"  backup <git common dir>/oah/backups/{step.backup} "
                f"(sha256 {hashlib.sha256(step.before).hexdigest()})")
    for problem in problems:
        out(f"  {problem}")
    outcome.changed += len(installed)
    if problems:
        outcome.pending.append("hooks")
    if plan.ci is not None and plan.ci.action in ("create", "replace", "skipped"):
        out("")
        out("CI starter:")
        problem = write_ci(plan.ci) if plan.ci.action != "skipped" else f"not written: {plan.ci.note}"
        out(f"  {problem}" if problem else f"  wrote {ci.WORKFLOW_PATH}")
        if problem:
            outcome.pending.append("ci")
        else:
            outcome.changed += 1
    _apply_unlazy(plan, machine, outcome, out)
    _apply_companions(plan, machine, outcome, yes=yes, ask=ask, out=out)
    unlazy_copy = plan.unlazy is not None and (plan.unlazy.action in ("install", "replace") or
                                              pathlib.Path(plan.unlazy.folder) == root / UNLAZY_PROJECT)
    problems = _ignore_problems(root, unlazy_copy=unlazy_copy)
    for problem in problems:
        out(problem)
    if problems:
        outcome.pending.append("gitignore")
    outcome.impact, outcome.impact_detail = refresh_map(root, machine.env)
    out("")
    if outcome.impact == "unavailable":
        out(f"OAH_INIT_IMPACT_UNAVAILABLE: {outcome.impact_detail} The rest of init is done; once that is fixed, "
            "build the map with `oah impact build`.")
    else:
        out(f"Impact map: {outcome.impact} ({outcome.impact_detail})")
    return outcome


VERBS = {"create": "created", "replace": "replaced", "update": "updated"}


def _apply_unlazy(plan: Plan, machine: Machine, outcome: Outcome, out) -> None:
    step = plan.unlazy
    if step is None or step.action == "present":
        return
    out("")
    out("unlazy:")
    if step.action == "blocked":
        out(f"  {step.note}" if step.note.startswith("OAH_") else f"  OAH_INIT_UNLAZY_FAILED: {step.note}")
        outcome.pending.append("unlazy")
        return
    out(f"  installing unlazy into {UNLAZY_PROJECT} from {machine.unlazy_source} ...")
    problem = install_unlazy(plan.root, step, source=machine.unlazy_source, pin=machine.unlazy_pin)
    if problem:
        out(f"  {problem}")
        if step.backup:
            out(f"  OAH_INIT_BACKUP: inspect the planned backup at <git common dir>/oah/backups/{step.backup} "
                f"(sha256 {step.digest}) before retrying")
        outcome.pending.append("unlazy")
        return
    out(f"  installed unlazy into {UNLAZY_PROJECT} at commit {machine.unlazy_pin.commit[:12]}, verified")
    if step.backup:
        out(f"  backup <git common dir>/oah/backups/{step.backup} (sha256 {step.digest})")
    outcome.changed += 1


def _apply_companions(plan: Plan, machine: Machine, outcome: Outcome, *, yes: bool, ask, out) -> None:
    if not plan.companion_steps:
        return
    out("")
    out("Companion apps:")
    found = []
    for app in plan.apps:
        steps = [step for step in plan.companion_steps if step.app == app]
        if app == "orca":
            outcomes = _run_orca_steps(steps, plan.root, machine, ask=ask, out=out)
        else:
            outcomes = companions.run_steps(steps, project_root=plan.root, yes=False, which=machine.which,
                                            ask=ask, out=out, runner=machine.companion_runner,
                                            probe=machine.companion_probe, refresh=machine.refresh)
        found += outcomes
        outcome.changed += sum(1 for item in outcomes if item.status == "done")
        if any(item.status not in companions.OK_STATUSES for item in outcomes):
            outcome.pending.append(app)
    if "aside" in plan.apps:
        step = plan.rules
        if companions.CompanionResult(found).aside_registered:
            status = _write_aside_local(plan.root, plan.aside_local)
            if status not in ("added", "present"):
                out(f"OAH_INIT_CHANGED: Aside rules were {status}; run oah init again to see the new plan")
                outcome.pending.append("files")
                return
            outcome.changed += int(status == "added")
            step = plan.aside_rules
        try:
            synced = _sync_rules(plan.root, step)
        except InitError as exc:
            out(str(exc))
            outcome.pending.append("files")
            return
        for name in synced.changed:
            out(f"  updated {name} (the planned rules)")
        outcome.changed += len(synced.changed)


def _run_orca_steps(steps, root: pathlib.Path, machine: Machine, *, ask, out):
    """Add conservative presence checks without changing the landed companion setup contract."""
    outcomes, statuses = [], {}
    for step in steps:
        if step.requires is not None and statuses.get(step.requires) not in ("done", "present"):
            status = "blocked"
        elif step.key in ("orca-skills", "orca-project") and step.skip_if is not None:
            state = machine.companion_probe(step.skip_if, root)
            if state == 0:
                status = "present"
            elif state != 1:
                status = "pending"
                out(f"OAH_INIT_ORCA_UNVERIFIED: cannot verify {step.key}; no change was made. Open Orca, "
                    f"check `{companions.show(step.skip_if)}`, then run oah init again")
            else:
                status = ""  # definitely missing: the landed runner still asks for this command's consent
        else:
            status = ""
        if status:
            outcomes.append(companions.StepOutcome(step, status))
        else:
            # The check above is authoritative for this step; do not let a second unknown probe fall through.
            run_step = dataclasses.replace(step, skip_if=None) if step.argv is not None else step
            outcomes += companions.run_steps([run_step], project_root=root, yes=False, which=machine.which,
                                             ask=ask, out=out, runner=machine.companion_runner,
                                             probe=machine.companion_probe, refresh=machine.refresh)
        statuses[step.key] = outcomes[-1].status
    return outcomes
