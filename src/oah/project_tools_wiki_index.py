# 프로젝트 위키 색인 어댑터 — 고른 위키 노트의 커밋 스냅샷을 사용자 상태 폴더의 격리된 로컬 gbrain·루프백 Ollama 색인으로 만들고, 못 쓰면 까닭과 함께 텍스트 검색으로 물러선다.
"""An optional local semantic index for the project wiki, with a text-search fallback.

What it reads. Only Markdown notes of the selected wiki folder, taken from a committed snapshot (HEAD, or a full
commit id that is an ancestor of HEAD). Sub-folders are included recursively, so decisions/ is never missed. Never
read: names starting with a dot, raw/, templates/, archive folders, index.md and log.md at the wiki root,
log_archive_* and index_archive_* files, files that are not .md, symbolic links and submodules; a wiki folder that
is itself hidden or inside such a folder is refused. The list is bounded (Limits) and every blob is checked against
its Git object id before it is mirrored.

Where it writes. Nothing in the repository. Each worktree has one folder in the per-user state folder:

    <OAH_HOME or ~/.oah>/state/wiki-index/<id>/        id = worktree_id(canonical worktree root)
        current.json        the last good index (pointer and summary); absent until a build completes
        pending.json        a build that started and has not completed (it can be continued); absent otherwise
        generations/<name>/ manifest.json, mirror/ (the notes, exact bytes), home/ (the tool's PGLite brain)

A build always works in a generation of its own, and current.json moves only after the tool reports every passage
embedded. So a failed or interrupted build never replaces the last good index, and the next build of the same input
continues the same generation. The price: a change of the selected notes embeds all of them again.

What it runs. The explicitly configured gbrain command and nothing else: an absolute executable outside the
worktree, never a shell or a batch file, started in a folder of the generation with an environment built from an
allow-list (no provider credential, proxy or tool setting is inherited), its home inside the generation, and
self-upgrade, startup hooks, autopilot install and telemetry switched off. The tool never sees the repository: it
reads the mirror, which holds only the selected notes under names this module made up, so no hook, plugin or
configuration file of the project can reach it. Embeddings come from an Ollama server that already runs at a
literal loopback address and from a model that server already has. This module never installs, starts, stops,
pulls or deletes anything, and it refuses a model the server marks as remote. It does not confine the tool's own
network use with an operating-system sandbox; it gives the tool no credential and no address but the loopback one.

Calls. status() only reads (Git objects and the files above): it sends no request, runs no tool and creates no
file, folder or lock, so it is safe when a project is merely opened. probe(), build() and search() are explicit
actions. Every call returns a JSON-ready dict with "schema" and "ok"; an expected stop is data,
{"ok": false, "code": "OAH_WIKI_INDEX_...", "message": ...}, never an exception. search() answers from the index
when it can and otherwise from text_search() over the working tree, and says why in "fallback". A search result
names the note's repository-relative path, its title, the nearest heading and the 1-based line (for a result of the
index: the line, inside the passage the tool returned, that shares the most words with the query); "match" is
"strong" or "weak" as the tool rates it (None when it does not say) and "text" for a text result. A score is the
tool's own number: compare it only with the other scores of the same answer.

Without importing Python: "python -m oah.project_tools_wiki_index" reads one JSON request {"action": ..., ...} on
standard input and writes one JSON result on standard output (exit 0 when "ok", 1 when refused, 2 for a bad request;
3 with a traceback on standard error and nothing on standard output for a defect of this module).
"""
from __future__ import annotations

import datetime
import hashlib
import http.client
import ipaddress
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable, Iterator, Mapping, Sequence

from oah import scratch
from oah.atomic import StateCorrupt, read_json, unlink_durable, write_json_atomic
from oah.config import ConfigError, load_config
from oah.engines.proc import RunResult, UnsupportedBatchShim, is_batch, run_command
from oah.lockfile import LockTimeout, StateLock
from oah.paths import GitMissing, NotARepo, git_argv, repo_info
from oah.wiki.base import DEFAULTS as WIKI_DEFAULTS
from oah.wiki.base import WikiError, WikiSettings
from oah.wiki.log import ARCHIVE_PREFIX
from oah.wiki.markdown import FenceTracker, strip_eol
from oah.wiki.notes import frontmatter_span, parse_frontmatter

__all__ = ["Limits", "LocalTools", "Selection", "Timeouts", "build", "handle", "main", "probe", "search", "status",
           "text_search", "user_state_root", "worktree_id"]

SCHEMA = 1
RULE = 1  # the selection rule below; a change makes every existing index stale
STATE_FOLDER = "wiki-index"
SOURCE_ID = "oah-wiki"
PHASES = ("mirror", "brain", "sync", "embed", "verify")
MAX_EMBED_ROUNDS = 200
PROBE_TEXT = "oah wiki index connection check"

CODE_USAGE = "OAH_WIKI_INDEX_USAGE"
CODE_NOT_A_REPO = "OAH_WIKI_INDEX_NOT_A_REPO"
CODE_GIT = "OAH_WIKI_INDEX_GIT"
CODE_NO_SNAPSHOT = "OAH_WIKI_INDEX_NO_SNAPSHOT"
CODE_SELECTION = "OAH_WIKI_INDEX_SELECTION"
CODE_NO_NOTES = "OAH_WIKI_INDEX_NO_NOTES"
CODE_TOO_LARGE = "OAH_WIKI_INDEX_TOO_LARGE"
CODE_SOURCE_UNVERIFIED = "OAH_WIKI_INDEX_SOURCE_UNVERIFIED"
CODE_TOOL_NOT_CONFIGURED = "OAH_WIKI_INDEX_TOOL_NOT_CONFIGURED"
CODE_TOOL_MISSING = "OAH_WIKI_INDEX_TOOL_MISSING"
CODE_TOOL_REJECTED = "OAH_WIKI_INDEX_TOOL_REJECTED"
CODE_TOOL_FAILED = "OAH_WIKI_INDEX_TOOL_FAILED"
CODE_NOT_LOCAL = "OAH_WIKI_INDEX_NOT_LOCAL"
CODE_OLLAMA_DOWN = "OAH_WIKI_INDEX_OLLAMA_DOWN"
CODE_MODEL_MISSING = "OAH_WIKI_INDEX_MODEL_MISSING"
CODE_MODEL_UNUSABLE = "OAH_WIKI_INDEX_MODEL_UNUSABLE"
CODE_MODEL_CHANGED = "OAH_WIKI_INDEX_MODEL_CHANGED"
CODE_NOT_BUILT = "OAH_WIKI_INDEX_NOT_BUILT"
CODE_INCOMPLETE = "OAH_WIKI_INDEX_INCOMPLETE"
CODE_BUSY = "OAH_WIKI_INDEX_BUSY"
CODE_STATE = "OAH_WIKI_INDEX_STATE"

_SKIP_FOLDERS = {"raw": "raw", "templates": "template", "archive": "archive", "archives": "archive"}
_NAVIGATION = ("index.md", "log.md")
_ARCHIVE_PREFIXES = (ARCHIVE_PREFIX, "index_archive_")
_REGULAR_MODES = ("100644", "100755")
_REPARSE_POINT = 0x400
_GENERATION = re.compile(r"g-[A-Za-z0-9_]{1,40}")
_FULL_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "cmd", "powershell", "pwsh", "env",
                     "wscript", "cscript"})
_SCRIPT_SUFFIXES = (".cmd", ".bat", ".ps1", ".sh", ".bash", ".zsh", ".vbs", ".wsf")
_SECRET_NAME = re.compile(r"api_?key|token|secret|password", re.IGNORECASE)
_WINDOWS_PASS = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
                 "OS")
_TOOL_SWITCHES = {"GBRAIN_SELF_UPGRADE_MODE": "off", "GBRAIN_SKIP_STARTUP_HOOKS": "1",
                  "GBRAIN_NO_AUTOPILOT_INSTALL": "1", "GBRAIN_TELEMETRY": "off", "DO_NOT_TRACK": "1", "NO_COLOR": "1",
                  "NO_PROXY": "127.0.0.1,::1,localhost", "no_proxy": "127.0.0.1,::1,localhost"}
_GIT_READ = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull)
_GIT_MIRROR = ("-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.attributesFile=" + os.devnull,
               "-c", "init.templateDir=", "-c", "commit.gpgsign=false", "-c", "gc.auto=0")
# The whole Git configuration a child sees (GIT_CONFIG_GLOBAL; the system file is switched off): bytes are kept as
# they are, and no hook, attribute file, signing program or background maintenance runs.
_GIT_CONFIG = ("[core]\n\tautocrlf = false\n\tsafecrlf = false\n\tfsmonitor = false\n\thooksPath = {null}\n"
               "\tattributesFile = {null}\n[gc]\n\tauto = 0\n[commit]\n\tgpgsign = false\n[maintenance]\n"
               "\tauto = false\n")
_MIRROR_AUTHOR = {"GIT_AUTHOR_NAME": "oah wiki index", "GIT_AUTHOR_EMAIL": "wiki-index@oah.invalid",
                  "GIT_COMMITTER_NAME": "oah wiki index", "GIT_COMMITTER_EMAIL": "wiki-index@oah.invalid"}


class Refusal(Exception):
    """An expected stop. code is OAH_WIKI_INDEX_*; message says what happened and what to do next."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Limits:
    """The bounds of one snapshot and one answer."""

    max_files: int = 2000
    max_file_bytes: int = 1_048_576
    max_total_bytes: int = 67_108_864
    max_path_bytes: int = 512
    max_results: int = 20
    snippet_chars: int = 240
    max_query_chars: int = 512


@dataclass(frozen=True)
class Timeouts:
    """Seconds allowed for one step."""

    git: float = 120.0
    http: float = 5.0
    probe_embed: float = 120.0
    tool_version: float = 30.0
    tool_init: float = 300.0
    tool_source: float = 120.0
    tool_sync: float = 1800.0
    tool_embed: float = 3600.0
    tool_stats: float = 120.0
    tool_query: float = 60.0
    lock: float = 15.0


@dataclass(frozen=True)
class Selection:
    """The wiki folder to index, as a repository-relative POSIX path."""

    wiki_dir: str


@dataclass(frozen=True)
class LocalTools:
    """This machine's tools. They come from user-local state, never from a file of the repository."""

    gbrain: tuple[str, ...]  # the command: an absolute executable outside the worktree, then fixed arguments
    ollama_url: str  # a literal loopback address with a port, such as http://127.0.0.1:11434
    embedding_model: str  # a model the Ollama server already has, such as bge-m3
    embedding_dimensions: int | None = None  # None: measured from the running server


@dataclass(frozen=True)
class _Place:
    root: pathlib.Path  # the canonical worktree root
    common_dir: pathlib.Path
    head: str | None
    index_dir: pathlib.Path | None  # None when the per-user state folder is unknown
    problem: Refusal | None


@dataclass(frozen=True)
class _Entry:
    path: str
    oid: str
    size: int


@dataclass(frozen=True)
class _Snapshot:
    commit: str
    wiki_dir: str
    object_format: str
    entries: tuple[_Entry, ...]
    digest: str
    total_bytes: int
    skipped: dict


@dataclass(frozen=True)
class _Model:
    name: str
    resolved: str
    digest: str
    dimensions: int | None


@dataclass(frozen=True)
class _Generation:
    path: pathlib.Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def mirror(self) -> pathlib.Path:
        return self.path / "mirror"

    @property
    def home(self) -> pathlib.Path:
        return self.path / "home"

    @property
    def work(self) -> pathlib.Path:
        return self.path / "work"

    @property
    def tmp(self) -> pathlib.Path:
        return self.path / "tmp"

    @property
    def manifest(self) -> pathlib.Path:
        return self.path / "manifest.json"

    @property
    def git_config(self) -> pathlib.Path:
        return self.path / "gitconfig"


@dataclass(frozen=True)
class _Run:
    command: tuple[str, ...]
    base: str  # the Ollama address
    gen: _Generation
    timeouts: Timeouts


_EXPECTED = (Refusal, NotARepo, GitMissing, StateCorrupt, LockTimeout, ConfigError, WikiError, UnsupportedBatchShim,
             OSError)


def _as_refusal(exc: BaseException) -> Refusal:
    if isinstance(exc, Refusal):
        return exc
    if isinstance(exc, NotARepo):
        return Refusal(CODE_NOT_A_REPO, "the folder is not inside a Git repository. Open a project that Git tracks, "
                       "or use text search.")
    if isinstance(exc, (GitMissing, UnsupportedBatchShim)):
        return Refusal(CODE_GIT, f"Git cannot be run ({exc}). Install Git and retry.")
    if isinstance(exc, LockTimeout):
        return Refusal(CODE_BUSY, "another command is using this wiki index. Wait for it to finish, then retry.")
    if isinstance(exc, (ConfigError, WikiError)):
        return Refusal(CODE_SELECTION, f"the project's wiki settings cannot be used ({exc}). Fix .oah/config.toml "
                       "or pass the wiki folder, then retry.")
    return Refusal(CODE_STATE, f"the index folder cannot be used ({exc}). Fix the cause and retry; the last good "
                   "index, if any, was not changed.")


def _refused(action: str, exc: BaseException, **extra: object) -> dict:
    refusal = _as_refusal(exc)
    return {"schema": SCHEMA, "ok": False, "action": action, "code": refusal.code, "message": refusal.message,
            **extra}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- where things are ----

def user_state_root(env: Mapping[str, str] | None = None) -> pathlib.Path:
    """<OAH_HOME or <home>/.oah>/state: the per-user state folder, by the rule the desktop app uses. Creates nothing."""
    env = os.environ if env is None else env
    explicit = env.get("OAH_HOME", "")
    if explicit:
        home = pathlib.Path(explicit)
        return (home if home.is_absolute() else pathlib.Path.cwd() / home) / "state"
    name = "USERPROFILE" if os.name == "nt" else "HOME"
    value = env.get(name, "")
    if not value or not pathlib.Path(value).is_absolute():
        raise Refusal(CODE_STATE, f"{name} is not set to an absolute folder, so the per-user index folder is unknown. "
                      f"Set {name} or OAH_HOME, then retry.")
    return pathlib.Path(value) / ".oah" / "state"


def worktree_id(root: os.PathLike | str) -> str:
    """The 16 hexadecimal digits naming one worktree's index folder: the SHA-256 of its canonical path, taken over
    the native path bytes on POSIX and the UTF-16 code units (little-endian) on Windows."""
    text = os.fspath(root)
    data = text.encode("utf-16-le") if os.name == "nt" else os.fsencode(text)
    return hashlib.sha256(data).hexdigest()[:16]


def _place(worktree: os.PathLike | str, state_root: os.PathLike | str | None,
           env: Mapping[str, str] | None, *, stateless: bool = False) -> _Place:
    if not isinstance(worktree, (str, os.PathLike)) or not os.fspath(worktree):
        raise Refusal(CODE_USAGE, "worktree must be the project folder's path")
    info = repo_info(pathlib.Path(worktree))
    root = pathlib.Path(os.path.realpath(info.root))
    index_dir, problem = None, None
    try:
        if stateless:
            base = None
        elif state_root is None:
            base = user_state_root(env)
        else:
            if not isinstance(state_root, (str, os.PathLike)) or not os.fspath(state_root):
                raise Refusal(CODE_USAGE, "state_root must be an absolute folder path")
            base = pathlib.Path(state_root)
            if not base.is_absolute():
                raise Refusal(CODE_USAGE, "state_root must be an absolute folder path")
        if base is not None:
            index_dir = base / STATE_FOLDER / worktree_id(root)
    except Refusal as exc:
        if exc.code == CODE_USAGE:
            raise
        problem = exc
    return _Place(root, pathlib.Path(os.path.realpath(info.common_dir)), info.head, index_dir, problem)


def _selection(place: _Place, selection: object) -> Selection:
    if selection is None:
        directory = WikiSettings.from_mapping(load_config(place.root).section("wiki", WIKI_DEFAULTS)).dir
    elif isinstance(selection, Selection):
        directory = selection.wiki_dir
    elif isinstance(selection, Mapping):
        unknown = sorted(set(selection) - {"wiki_dir"})
        if unknown:
            raise Refusal(CODE_USAGE, f"unknown selection key {unknown[0]}; the only key is wiki_dir")
        directory = selection.get("wiki_dir")
    else:
        raise Refusal(CODE_USAGE, 'selection must be an object such as {"wiki_dir": "docs/wiki"}')
    if not isinstance(directory, str):
        raise Refusal(CODE_USAGE, "selection.wiki_dir must be a folder path inside the repository")
    parts = directory.replace("\\", "/").strip("/").split("/")
    bad = (not directory.strip() or directory.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", directory)
           or any(part in ("", ".", "..") or _unsafe_name(part) for part in parts)
           or parts[0].startswith(("-", ":")))
    if bad:
        raise Refusal(CODE_SELECTION, f"{directory!r} is not a folder inside this repository. Give the wiki folder "
                      "as a repository-relative path such as docs/wiki.")
    if any(part.startswith(".") for part in parts):
        raise Refusal(CODE_SELECTION, f"{directory!r} is a hidden folder, and hidden material is never indexed or "
                      "searched. Keep the wiki in a folder whose name does not start with a dot.")
    if any(part.casefold() in _SKIP_FOLDERS for part in parts):
        raise Refusal(CODE_SELECTION, f"{directory!r} is in a raw, templates or archive folder, and that material "
                      "is never indexed or searched. Select the wiki folder itself.")
    return Selection("/".join(parts))


def _unsafe_name(part: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in part) or "\\" in part


def _skip_reason(parts: Sequence[str]) -> str | None:
    """Why a wiki-relative path is not a note to index or search, or None when it is one."""
    *folders, name = parts
    if any(part.startswith(".") for part in parts):
        return "hidden"
    for folder in folders:
        reason = _SKIP_FOLDERS.get(folder.casefold())
        if reason is not None:
            return reason
    lowered = name.casefold()
    if not lowered.endswith(".md"):
        return "attachment"
    if lowered.startswith(_ARCHIVE_PREFIXES):
        return "archive"
    if not folders and lowered in _NAVIGATION:
        return "navigation"
    return None


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _inside(path: os.PathLike | str, folder: os.PathLike | str) -> bool:
    """Whether path is folder or lies below it: by name (as given and resolved) or because one of its existing
    ancestors is that very folder, which also covers another spelling on a case-insensitive file system."""
    for resolve in (os.path.abspath, os.path.realpath):
        inner, outer = os.path.normcase(resolve(path)), os.path.normcase(resolve(folder))
        try:
            if os.path.commonpath([inner, outer]) == outer:
                return True
        except ValueError:
            continue  # different drives
    current = os.path.abspath(path)
    while True:
        try:
            if os.path.samefile(current, folder):
                return True
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


# ---- the committed snapshot ----

def _git(root: pathlib.Path, *args: str, timeout: float, stdin: str | None = None) -> RunResult:
    """Read-only Git on the project: no replacement object, no fetch of a missing object, a path taken literally;
    latin-1 keeps every output byte."""
    env = dict(os.environ)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
               GIT_LITERAL_PATHSPECS="1")
    return run_command(git_argv() + _GIT_READ + ("-C", str(root), *args), timeout=timeout, env=env,
                       encoding="latin-1", stdin_text=stdin)


def _resolve_commit(place: _Place, commit: object, timeouts: Timeouts) -> str:
    if place.head is None:
        raise Refusal(CODE_NO_SNAPSHOT, "this repository has no commit yet. Commit the wiki first; only committed "
                      "notes are indexed (text search reads the files as they are).")
    if commit is None or commit == place.head:
        return place.head
    if not isinstance(commit, str) or not _FULL_ID.fullmatch(commit):
        raise Refusal(CODE_NO_SNAPSHOT, "commit must be a full commit id (40 or 64 hexadecimal digits)")
    named = _git(place.root, "rev-parse", "--verify", "--quiet", commit + "^{commit}", timeout=timeouts.git)
    result = _git(place.root, "merge-base", "--is-ancestor", commit, place.head, timeout=timeouts.git)
    if named.stdout.strip() != commit or result.timed_out or result.returncode != 0:
        raise Refusal(CODE_NO_SNAPSHOT, f"{commit} is not HEAD or one of its ancestors in this worktree. Index HEAD, "
                      "or a commit this branch contains.")
    return commit


def _blob_oid(data: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format, usedforsecurity=False)
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def _snapshot(place: _Place, commit: str, selection: Selection, limits: Limits, timeouts: Timeouts) -> _Snapshot:
    prefix = selection.wiki_dir + "/"
    result = _git(place.root, "ls-tree", "-r", "-z", "-l", "--full-tree", commit, "--", prefix, timeout=timeouts.git)
    if result.timed_out or result.returncode != 0:
        raise Refusal(CODE_GIT, f"Git could not list {selection.wiki_dir} at {commit[:12]} "
                      f"({_tail(result.stderr)}). Check the repository and retry.")
    records = [record for record in result.stdout.encode("latin-1").split(b"\0") if record]
    if not records:
        raise Refusal(CODE_SELECTION, f"{selection.wiki_dir} has no committed file at {commit[:12]}. Commit the "
                      "wiki, or point the selection at the folder that holds it.")
    entries: list[_Entry] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for record in records:
        meta, _, raw_name = record.partition(b"\t")
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 4:
            skip("other")
            continue
        mode, kind, oid, size = fields
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            skip("unsafe_name")
            continue
        if not name.startswith(prefix):
            skip("other")
            continue
        parts = name[len(prefix):].split("/")
        if mode == "120000":
            skip("symlink")
        elif mode == "160000":
            skip("submodule")
        elif kind != "blob" or mode not in _REGULAR_MODES or not size.isdigit():
            skip("other")
        elif any(not part or _unsafe_name(part) for part in parts):
            skip("unsafe_name")
        elif (reason := _skip_reason(parts)) is not None:
            skip(reason)
        elif len(name.encode("utf-8")) > limits.max_path_bytes:
            skip("path_too_long")
        elif int(size) == 0:
            skip("empty")
        elif int(size) > limits.max_file_bytes:
            skip("too_large")
        else:
            entries.append(_Entry(name, oid, int(size)))
    entries.sort(key=lambda entry: entry.path)
    total = sum(entry.size for entry in entries)
    if len(entries) > limits.max_files or total > limits.max_total_bytes:
        raise Refusal(CODE_TOO_LARGE, f"{selection.wiki_dir} has {len(entries)} notes ({total} bytes) at "
                      f"{commit[:12]}; the index takes at most {limits.max_files} notes and "
                      f"{limits.max_total_bytes} bytes. Select a smaller wiki folder, or use text search.")
    if not entries:
        raise Refusal(CODE_NO_NOTES, f"{selection.wiki_dir} has no note to index at {commit[:12]} (only navigation, "
                      "archive, raw, hidden or non-Markdown files). Add a note and commit it.")
    digest = hashlib.sha256(f"oah-wiki-index {RULE}\n{selection.wiki_dir}\n".encode("utf-8"))
    for entry in entries:
        digest.update(f"{entry.oid} {entry.path}\n".encode("utf-8"))
    return _Snapshot(commit, selection.wiki_dir, "sha256" if len(commit) == 64 else "sha1", tuple(entries),
                     digest.hexdigest(), total, dict(sorted(skipped.items())))


def _read_blobs(place: _Place, snapshot: _Snapshot, timeouts: Timeouts) -> Iterator[tuple[_Entry, bytes]]:
    """Every selected blob, checked: the bytes hash to the object id the tree names."""
    batch: list[_Entry] = []
    size = 0
    for entry in snapshot.entries:
        batch.append(entry)
        size += entry.size
        if size >= 8_388_608 or len(batch) >= 500:
            yield from _read_batch(place, batch, snapshot.object_format, timeouts)
            batch, size = [], 0
    if batch:
        yield from _read_batch(place, batch, snapshot.object_format, timeouts)


def _read_batch(place: _Place, batch: Sequence[_Entry], object_format: str,
                timeouts: Timeouts) -> Iterator[tuple[_Entry, bytes]]:
    result = _git(place.root, "cat-file", "--batch", timeout=timeouts.git,
                  stdin="".join(entry.oid + "\n" for entry in batch))
    if result.timed_out or result.returncode != 0:
        raise Refusal(CODE_GIT, f"Git could not read the notes ({_tail(result.stderr)}). Check the repository and "
                      "retry.")
    data = result.stdout.encode("latin-1")
    offset = 0
    for entry in batch:
        end = data.find(b"\n", offset)
        header = data[offset:end].split() if end >= 0 else []
        length = int(header[2]) if len(header) == 3 and header[2].isdigit() else -1
        body = data[end + 1:end + 1 + length] if length >= 0 else b""
        if (length != entry.size or len(body) != length or header[0] != entry.oid.encode("ascii")
                or header[1] != b"blob" or _blob_oid(body, object_format) != entry.oid):
            raise Refusal(CODE_SOURCE_UNVERIFIED, f"{entry.path} did not read back as the committed blob "
                          f"{entry.oid[:12]}, so nothing was indexed. Run git fsck on the repository, then retry.")
        offset = end + 1 + length + 1
        yield entry, body


def _tail(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[-limit:] or "no message"


# ---- this machine's tools ----

def _local_tools(value: object) -> LocalTools | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        unknown = sorted(set(value) - {"gbrain", "ollama_url", "embedding_model", "embedding_dimensions"})
        if unknown:
            raise Refusal(CODE_USAGE, f"unknown tools key {unknown[0]}; the keys are gbrain, ollama_url, "
                          "embedding_model and embedding_dimensions")
        command = value.get("gbrain")
        if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
            raise Refusal(CODE_USAGE, 'tools.gbrain must be a list such as ["/absolute/path/to/gbrain"]')
        value = LocalTools(tuple(command), value.get("ollama_url"), value.get("embedding_model"),
                           value.get("embedding_dimensions"))
    if not isinstance(value, LocalTools):
        raise Refusal(CODE_USAGE, "tools must be an object with gbrain, ollama_url and embedding_model")
    dimensions = value.embedding_dimensions
    if (not value.gbrain or len(value.gbrain) > 16
            or not all(isinstance(part, str) and part and len(part) <= 1024 and not _unsafe_name(part.replace("\\", "/"))
                       for part in value.gbrain)
            or not isinstance(value.ollama_url, str) or not isinstance(value.embedding_model, str)
            or not (dimensions is None or (isinstance(dimensions, int) and not isinstance(dimensions, bool)
                                           and 1 <= dimensions <= 65536))):
        raise Refusal(CODE_USAGE, "tools needs gbrain (a list of up to 16 plain strings), ollama_url and "
                      "embedding_model (strings) and, optionally, embedding_dimensions (a whole number)")
    return LocalTools(tuple(value.gbrain), value.ollama_url, value.embedding_model, dimensions)


def _qualify_command(command: Sequence[str], place: _Place) -> tuple[str, ...]:
    """The configured command, or a refusal: an absolute, existing executable outside the worktree and its Git
    folder, not a shell and not a script a shell would run, with no argument that reaches into the worktree."""
    program = command[0]
    name = pathlib.PurePath(program).name.casefold()
    stem = name[:-4] if name.endswith(".exe") else name
    if not os.path.isabs(program) or ".." in pathlib.PurePath(program).parts:
        raise Refusal(CODE_TOOL_REJECTED, f"{program!r} is not an absolute path. Configure the full path of the "
                      "gbrain executable.")
    if stem in _SHELLS or is_batch(name) or name.endswith(_SCRIPT_SUFFIXES):
        raise Refusal(CODE_TOOL_REJECTED, f"{program} is a shell or a shell script, which is never run. Configure "
                      "the tool's own executable, for example the bun executable followed by gbrain's cli.ts.")
    named = [program] + [text for part in command[1:] for text in (part, part.partition("=")[2])
                         if text and os.path.isabs(text)]  # an argument, or the value of --option=value
    for folder in (place.root, place.common_dir):
        for part in named:
            if _inside(part, folder):
                raise Refusal(CODE_TOOL_REJECTED, f"{part} is inside the project ({folder}). A tool is only run "
                              "from outside the project it indexes; install it elsewhere and configure that path.")
    if any(".." in pathlib.PurePath(part).parts for part in command[1:]):
        raise Refusal(CODE_TOOL_REJECTED, "an argument of the gbrain command contains '..'. Use absolute paths.")
    try:
        info = os.stat(program)
    except OSError:
        raise Refusal(CODE_TOOL_MISSING, f"{program} does not exist. Install gbrain yourself or correct the "
                      "configured path; nothing is installed for you.") from None
    if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and not os.access(program, os.X_OK)):
        raise Refusal(CODE_TOOL_REJECTED, f"{program} is not an executable file. Configure the gbrain executable.")
    return tuple(command)


def _loopback_url(text: str) -> str:
    """http://<literal loopback address>:<port>, or a refusal. A name such as localhost is not accepted: only a
    literal address proves the request stays on this machine."""
    problem = Refusal(CODE_NOT_LOCAL, f"{text!r} is not a literal loopback address with a port. Use an address such "
                      "as http://127.0.0.1:11434 (not a host name, not another machine).")
    if text != text.strip() or "%" in text or any(ord(char) < 33 or ord(char) == 127 for char in text):
        raise problem
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
        address = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        raise problem from None
    if (parts.scheme != "http" or not address.is_loopback or port is None or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise problem
    return f"http://[{address.compressed}]:{port}" if address.version == 6 else f"http://{address}:{port}"


def _model_names(name: str) -> tuple[str, str]:
    if len(name) > 128 or not _MODEL.fullmatch(name):
        raise Refusal(CODE_USAGE, f"{name!r} is not a model name such as bge-m3 or nomic-embed-text:latest")
    tag = name.rpartition(":")[2] if ":" in name else "latest"
    if tag == "cloud" or tag.endswith("-cloud"):
        raise Refusal(CODE_NOT_LOCAL, f"{name} names a cloud model. Choose an embedding model stored on this "
                      "machine.")
    return name, (name if ":" in name else name + ":latest")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ollama(base: str, path: str, payload: Mapping | None, timeout: float, *, bad_status: str = CODE_OLLAMA_DOWN,
            limit: int = 8_388_608) -> object:
    """One request to the loopback Ollama server: no proxy, no redirect, a bounded JSON answer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise Refusal(bad_status, f"{base}{path} answered HTTP {exc.code}. Check that an Ollama server with this "
                      "model runs at that address.") from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise Refusal(CODE_OLLAMA_DOWN, f"nothing answered at {base} ({getattr(exc, 'reason', None) or exc!r}). "
                      "Start Ollama yourself or correct the address; nothing is started for you.") from None
    try:
        if len(body) > limit:
            raise ValueError("answer too large")
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise Refusal(CODE_OLLAMA_DOWN, f"{base}{path} did not answer like an Ollama server. Check the "
                      "address.") from None


def _ollama_version(base: str, timeouts: Timeouts) -> str:
    answer = _ollama(base, "/api/version", None, timeouts.http)
    version = answer.get("version") if isinstance(answer, Mapping) else None
    if not isinstance(version, str) or not version:
        raise Refusal(CODE_OLLAMA_DOWN, f"{base} did not answer like an Ollama server. Check the address.")
    return version[:40]


def _ollama_model(base: str, name: str, timeouts: Timeouts) -> _Model:
    """The model as the running server lists it. It must already be there and be stored on this machine."""
    given, resolved = _model_names(name)
    answer = _ollama(base, "/api/tags", None, timeouts.http)
    models = answer.get("models") if isinstance(answer, Mapping) else None
    if not isinstance(models, list):
        raise Refusal(CODE_OLLAMA_DOWN, f"{base} did not list its models like an Ollama server. Check the address.")
    found = next((item for item in models if isinstance(item, Mapping)
                  and resolved in (item.get("name"), item.get("model"))), None)
    if found is None:
        raise Refusal(CODE_MODEL_MISSING, f"the Ollama server at {base} has no model {resolved}. Pull it yourself "
                      f"(ollama pull {given}) or choose a model it has; models are never downloaded for you.")
    digest = found.get("digest")
    digest = digest.removeprefix("sha256:") if isinstance(digest, str) else ""
    size = found.get("size")
    if (found.get("remote_host") or found.get("remote_model") or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        raise Refusal(CODE_NOT_LOCAL, f"{resolved} is not stored on this machine (the server lists it as remote or "
                      "without local content). Choose a local embedding model.")
    capabilities = found.get("capabilities")
    if isinstance(capabilities, list) and "embedding" not in capabilities:
        raise Refusal(CODE_MODEL_UNUSABLE, f"{resolved} is not an embedding model. Choose an embedding model such "
                      "as bge-m3.")
    return _Model(given, resolved, digest, None)


def _measure(base: str, model: _Model, timeouts: Timeouts) -> int:
    """The vector length the model really produces, from one fixed sentence that is not project text."""
    answer = _ollama(base, "/api/embed", {"model": model.name, "input": PROBE_TEXT}, timeouts.probe_embed,
                     bad_status=CODE_MODEL_UNUSABLE)
    vectors = answer.get("embeddings") if isinstance(answer, Mapping) else None
    first = vectors[0] if isinstance(vectors, list) and vectors else None
    if not isinstance(first, list) or not first or not all(isinstance(x, (int, float)) for x in first):
        raise Refusal(CODE_MODEL_UNUSABLE, f"{model.resolved} did not return an embedding. Choose an embedding "
                      "model.")
    return len(first)


def _checked_model(base: str, tools: LocalTools, timeouts: Timeouts) -> tuple[str, _Model]:
    version = _ollama_version(base, timeouts)
    model = _ollama_model(base, tools.embedding_model, timeouts)
    dimensions = _measure(base, model, timeouts)
    if tools.embedding_dimensions not in (None, dimensions):
        raise Refusal(CODE_MODEL_UNUSABLE, f"{model.resolved} produces vectors of {dimensions} numbers, not the "
                      f"configured {tools.embedding_dimensions}. Correct embedding_dimensions or leave it unset.")
    return version, replace(model, dimensions=dimensions)


# ---- running the tool in its own place ----

def _child_env(home: pathlib.Path, tmp: pathlib.Path, search: Sequence[pathlib.Path | str],
               extra: Mapping[str, str]) -> dict[str, str]:
    """The whole environment of a child: built here, nothing inherited but the names Windows itself needs."""
    env: dict[str, str] = {}
    folders = [str(folder) for folder in search]
    if os.name == "nt":
        for key, value in os.environ.items():
            if key.upper() in _WINDOWS_PASS:
                env[key] = value
        system = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        folders += [os.path.join(system, "System32"), system]
        env.update(USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                   LOCALAPPDATA=str(home / "AppData" / "Local"))
    else:
        folders += ["/usr/bin", "/bin"]
    env.update(HOME=str(home), PATH=os.pathsep.join(dict.fromkeys(folders)), TMPDIR=str(tmp), TMP=str(tmp),
               TEMP=str(tmp), LC_ALL="C", XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_DATA_HOME=str(home / ".local" / "share"), XDG_STATE_HOME=str(home / ".local" / "state"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0")
    env.update(extra)
    if os.name == "nt":  # there a name is the same whatever its case, so each is given once
        env = {name: env[name] for name in {key.upper(): key for key in env}.values()}
    return env


def _git_folders() -> tuple[str, ...]:
    """Where Git is, for the child's PATH (the tool reads the mirror through Git)."""
    program = git_argv()[0]
    return (os.path.dirname(program),) if os.path.isabs(program) else ()


def _private(folder: pathlib.Path) -> pathlib.Path:
    """folder, created owner-only when missing and proven private by the proof oah.scratch gives a root."""
    probe = scratch.make("probe-", root=folder)
    os.rmdir(probe)
    return probe.parent


def _remove_tree(path: pathlib.Path) -> None:
    """Remove a folder this module made. Git writes read-only object files, which Windows will not delete as is."""
    def retry(function: Callable, target: str, _info: object) -> None:
        os.chmod(target, stat.S_IRWXU)
        function(target)

    if not os.path.lexists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _write_git_config(gen: _Generation) -> None:
    gen.git_config.write_bytes(_GIT_CONFIG.format(null=os.devnull).encode("ascii"))


def _run_tool(run: _Run, args: Sequence[str], timeout: float) -> RunResult:
    gen = run.gen
    for folder in (gen.home, gen.work):
        folder.mkdir(mode=0o700, exist_ok=True)
    if not gen.git_config.exists():
        _write_git_config(gen)
    extra = dict(_TOOL_SWITCHES, GBRAIN_HOME=str(gen.home), OLLAMA_BASE_URL=run.base + "/v1",
                 GIT_CONFIG_GLOBAL=str(gen.git_config))
    search = (pathlib.Path(run.command[0]).parent, *_git_folders())
    with scratch.directory("run-", root=gen.tmp, ignore_cleanup_errors=True) as tmp:
        try:
            return run_command((*run.command, *args), timeout=timeout, cwd=str(gen.work),
                               env=_child_env(gen.home, pathlib.Path(tmp), search, extra))
        except FileNotFoundError:
            raise Refusal(CODE_TOOL_MISSING, f"{run.command[0]} could not be started. Install gbrain yourself or "
                          "correct the configured path.") from None
        except (OSError, UnsupportedBatchShim) as exc:
            raise Refusal(CODE_TOOL_FAILED, f"{run.command[0]} could not be started ({exc}). Check the configured "
                          "command.") from None


def _tool_ok(run: _Run, args: Sequence[str], timeout: float, code: str = CODE_TOOL_FAILED) -> RunResult:
    result = _run_tool(run, args, timeout)
    what = " ".join(args[:2])
    if result.timed_out:
        raise Refusal(code, f"gbrain {what} did not finish in {timeout:g} seconds and was stopped. Retry; a "
                      "stopped build continues where it stopped.")
    if result.returncode != 0:
        raise Refusal(code, f"gbrain {what} failed (exit {result.returncode}): "
                      f"{_tail(result.stderr or result.stdout)}. Fix the cause and retry.")
    return result


def _parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):  # a banner or a warning may come before the answer
        if line.lstrip().startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _tool_json(run: _Run, args: Sequence[str], timeout: float, code: str = CODE_TOOL_FAILED) -> tuple[object, str]:
    result = _tool_ok(run, args, timeout, code)
    data = _parse_json(result.stdout)
    if data is None:
        raise Refusal(code, f"gbrain {' '.join(args[:2])} did not answer with JSON. This gbrain version may not be "
                      "supported; check it and retry.")
    return data, result.stderr


def _version(run: _Run) -> str:
    """The first line of the tool's --version."""
    lines = _tool_ok(run, ("--version",), run.timeouts.tool_version).stdout.strip().splitlines()
    return " ".join(lines[0].split())[:80] if lines else ""


def _probe_version(command: tuple[str, ...], base: str, timeouts: Timeouts) -> str:
    """The tool's version, asked in a throwaway private folder so a connection test keeps nothing."""
    with scratch.directory("wiki-index-version-", ignore_cleanup_errors=True) as folder:
        gen = _Generation(pathlib.Path(folder))
        gen.home.mkdir()
        _private(gen.tmp)
        return _version(_Run(command, base, gen, timeouts))


# ---- stored state ----

def _load_state(index_dir: pathlib.Path | None) -> tuple[dict | None, dict | None]:
    """(the last good index, the build that has not completed); either may be None. Reads only."""
    if index_dir is None:
        return None, None
    active = _record(index_dir / "current.json")
    if not (isinstance(active, dict) and active.get("schema") == SCHEMA and _generation_name(active)
            and isinstance(active.get("model"), dict) and isinstance(active.get("manifest_digest"), str)
            and (index_dir / "generations" / active["generation"] / "manifest.json").is_file()):
        active = None
    pending = _record(index_dir / "pending.json")
    if not (isinstance(pending, dict) and pending.get("schema") == SCHEMA and _generation_name(pending)
            and isinstance(pending.get("fingerprint"), str)):
        pending = None
    if pending is not None and active is not None and pending["generation"] == active["generation"]:
        pending = None
    return active, pending


def _record(path: pathlib.Path) -> object:
    """A state file's content, or None when it is absent or unreadable: a damaged record is the same as none, so
    the next build replaces it instead of stopping on it for ever."""
    try:
        return read_json(path, None)
    except (StateCorrupt, OSError):
        return None


def _generation_name(record: Mapping) -> bool:
    name = record.get("generation")
    return isinstance(name, str) and _GENERATION.fullmatch(name) is not None


def _view(record: Mapping | None, keys: Sequence[str]) -> dict | None:
    return None if record is None else {key: record.get(key) for key in keys}


_ACTIVE_KEYS = ("generation", "commit", "wiki_dir", "manifest_digest", "files", "bytes", "pages", "chunks",
                "unindexed", "model", "ollama_url", "tool", "built_at")
_PENDING_KEYS = ("generation", "commit", "wiki_dir", "manifest_digest", "files", "phase", "done", "started_at",
                 "updated_at", "attempts", "error")


def _freshness(active: Mapping | None, pending: Mapping | None, snapshot: _Snapshot | Refusal,
               tools: LocalTools | None) -> tuple[str, list[str]]:
    """not_built, incomplete (a build started and no index completed yet), ready, or stale with the reasons."""
    if active is None:
        return ("incomplete" if pending is not None else "not_built"), []
    reasons = []
    if isinstance(snapshot, Refusal):
        reasons.append("source_unavailable")
    elif snapshot.wiki_dir != active.get("wiki_dir"):
        reasons.append("selection_changed")
    elif snapshot.digest != active.get("manifest_digest") or active.get("rule") != RULE:
        reasons.append("content_changed")
    if tools is not None:
        model = active["model"]
        try:
            if _model_names(tools.embedding_model)[1] != model.get("resolved"):
                reasons.append("model_changed")
        except Refusal:
            reasons.append("model_changed")
        if tools.embedding_dimensions not in (None, model.get("dimensions")):
            reasons.append("dimensions_changed")
    return ("stale" if reasons else "ready"), reasons


def _source_view(snapshot: _Snapshot | Refusal) -> dict:
    if isinstance(snapshot, Refusal):
        return {"error": {"code": snapshot.code, "message": snapshot.message}}
    return {"commit": snapshot.commit, "wiki_dir": snapshot.wiki_dir, "manifest_digest": snapshot.digest,
            "files": len(snapshot.entries), "bytes": snapshot.total_bytes, "skipped": snapshot.skipped}


def _describe(place: _Place, selection: Selection, tools: LocalTools | None, limits: Limits,
              timeouts: Timeouts) -> dict:
    try:
        snapshot: _Snapshot | Refusal = _snapshot(place, _resolve_commit(place, None, timeouts), selection, limits,
                                                  timeouts)
    except Refusal as exc:
        snapshot = exc
    active, pending = _load_state(place.index_dir)
    state, reasons = _freshness(active, pending, snapshot, tools)
    if place.problem is not None:
        reasons = [*reasons, "state_unavailable"]
    elif active is None and os.path.lexists(place.index_dir / "current.json"):
        reasons = [*reasons, "state_unreadable"]  # there was an index and its record or folder is damaged
    return {"state": state, "reasons": reasons, "worktree": str(place.root), "head": place.head,
            "source": _source_view(snapshot), "active": _view(active, _ACTIVE_KEYS),
            "pending": _view(pending, _PENDING_KEYS),
            "state_dir": None if place.index_dir is None else str(place.index_dir)}


def _defaults(limits: Limits | None, timeouts: Timeouts | None) -> tuple[Limits, Timeouts]:
    return (Limits() if limits is None else limits), (Timeouts() if timeouts is None else timeouts)


def status(worktree: os.PathLike | str, *, selection: object = None, tools: object = None,
           state_root: os.PathLike | str | None = None, env: Mapping[str, str] | None = None,
           limits: Limits | None = None, timeouts: Timeouts | None = None) -> dict:
    """What is indexed and whether it is current. Reads only: no request, no tool, nothing created."""
    try:
        limits, timeouts = _defaults(limits, timeouts)
        place = _place(worktree, state_root, env)
        return {"schema": SCHEMA, "ok": True, "action": "status",
                **_describe(place, _selection(place, selection), _local_tools(tools), limits, timeouts)}
    except _EXPECTED as exc:
        return _refused("status", exc)


def probe(worktree: os.PathLike | str, tools: object, *, state_root: os.PathLike | str | None = None,
          env: Mapping[str, str] | None = None, timeouts: Timeouts | None = None) -> dict:
    """Test the connection: the command is acceptable and runs, Ollama answers at the loopback address, and the
    model is there, local and produces vectors. Keeps nothing: no index file is written."""
    try:
        timeouts = _defaults(None, timeouts)[1]
        place = _place(worktree, state_root, env)
        local = _local_tools(tools)
        if local is None:
            raise Refusal(CODE_TOOL_NOT_CONFIGURED, "no local tools are configured. Give the gbrain command, the "
                          "Ollama address and the embedding model.")
        command = _qualify_command(local.gbrain, place)
        base = _loopback_url(local.ollama_url)
        version, model = _checked_model(base, local, timeouts)
        return {"schema": SCHEMA, "ok": True, "action": "probe",
                "tool": {"command": list(command), "version": _probe_version(command, base, timeouts)},
                "ollama": {"url": base, "version": version},
                "model": {"name": model.name, "resolved": model.resolved, "digest": model.digest,
                          "dimensions": model.dimensions, "local": True}}
    except _EXPECTED as exc:
        return _refused("probe", exc)


# ---- building ----

def _fingerprint(snapshot: _Snapshot, model: _Model) -> str:
    """What one index is an index of: the selected notes and the embedding model. A new commit that leaves the
    selected notes alone keeps the fingerprint, so it needs no rebuild."""
    text = json.dumps({"rule": RULE, "wiki_dir": snapshot.wiki_dir, "manifest": snapshot.digest,
                       "model": model.resolved, "model_digest": model.digest, "dimensions": model.dimensions},
                      sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _discard(generations: pathlib.Path, name: object) -> None:
    """Remove one generation folder by name; a link in its place is left alone."""
    if not isinstance(name, str) or not _GENERATION.fullmatch(name):
        return
    target = generations / name
    try:
        info = os.lstat(target)
    except OSError:
        return
    if stat.S_ISDIR(info.st_mode) and not _is_link(info):
        _remove_tree(target)


def _mirror_git(run: _Run, *args: str) -> RunResult:
    gen = run.gen
    extra = dict(_MIRROR_AUTHOR, GIT_CONFIG_GLOBAL=str(gen.git_config))
    with scratch.directory("run-", root=gen.tmp, ignore_cleanup_errors=True) as tmp:
        result = run_command(git_argv() + _GIT_MIRROR + ("-C", str(gen.mirror), *args), timeout=run.timeouts.git,
                             cwd=str(gen.mirror), env=_child_env(gen.home, pathlib.Path(tmp), _git_folders(), extra))
    if result.timed_out or result.returncode != 0:
        raise Refusal(CODE_GIT, f"Git could not prepare the mirror ({args[0]}: {_tail(result.stderr)}). Retry.")
    return result


def _phase_mirror(place: _Place, snapshot: _Snapshot, run: _Run) -> dict:
    """Write the selected notes, exact bytes, under names the tool cannot confuse, and commit them."""
    gen = run.gen
    _remove_tree(gen.mirror)
    _remove_tree(gen.home)
    gen.mirror.mkdir(mode=0o700)
    gen.home.mkdir(mode=0o700)
    _write_git_config(gen)
    salt = secrets.token_hex(8)
    entries, names = [], set()
    for entry, data in _read_blobs(place, snapshot, run.timeouts):
        name = "w" + hashlib.sha256(f"{salt}\0{entry.path}".encode("utf-8")).hexdigest()[:24]
        if name in names:
            raise Refusal(CODE_STATE, "two notes received the same mirror name. Retry the build.")
        names.add(name)
        (gen.mirror / f"{name}.md").write_bytes(data)
        entries.append({"path": entry.path, "oid": entry.oid, "size": entry.size, "id": name})
    _mirror_git(run, "init", "-q", "--initial-branch=main")
    _mirror_git(run, "add", "-A", "--", ".")
    _mirror_git(run, "commit", "-q", "--no-verify", "-m", f"wiki snapshot {snapshot.commit}")
    head = _mirror_git(run, "rev-parse", "HEAD").stdout.strip()
    manifest = {"schema": SCHEMA, "rule": RULE, "digest": snapshot.digest, "salt": salt, "mirror_commit": head,
                "source": {"worktree": str(place.root), "commit": snapshot.commit, "wiki_dir": snapshot.wiki_dir,
                           "object_format": snapshot.object_format},
                "entries": entries}
    write_json_atomic(gen.manifest, manifest)
    return manifest


def _holds_credential(value: object, depth: int = 0) -> bool:
    if not isinstance(value, Mapping) or depth > 8:
        return False
    return any((item and not isinstance(item, Mapping) and _SECRET_NAME.search(str(key)))
               or _holds_credential(item, depth + 1) for key, item in value.items())


def _verify_brain(gen: _Generation, model: _Model) -> None:
    """The tool's own record must say what was asked: a PGLite database inside this generation and this Ollama
    model, with no remote database and no stored credential. Anything else is refused."""
    config = gen.home / ".gbrain" / "config.json"
    try:
        data = json.loads(config.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        data = None
    problem = None
    if not isinstance(data, dict):
        problem = "it wrote no readable configuration"
    elif data.get("engine") != "pglite" or data.get("database_url"):
        problem = "its database is not a local PGLite database"
    elif not isinstance(data.get("database_path"), str) or not _inside(data["database_path"], gen.home):
        problem = "its database is not inside the index folder"
    elif data.get("embedding_model") != f"ollama:{model.name}":
        problem = f"its embedding model is {data.get('embedding_model')!r}, not ollama:{model.name}"
    elif str(data.get("embedding_dimensions")) != str(model.dimensions):
        problem = f"its vector length is {data.get('embedding_dimensions')!r}, not {model.dimensions}"
    elif _holds_credential(data):
        problem = "it stored a credential"
    if problem is not None:
        raise Refusal(CODE_NOT_LOCAL, f"gbrain did not create a verifiably local index: {problem}. Nothing was "
                      "indexed; check the configured gbrain command.")


def _phase_brain(run: _Run, model: _Model) -> None:
    gen = run.gen
    _remove_tree(gen.home)
    gen.home.mkdir(mode=0o700)
    _tool_ok(run, ("init", "--pglite", "--non-interactive", "--no-git", "--skip-embed-check",
                   "--embedding-model", f"ollama:{model.name}", "--embedding-dimensions", str(model.dimensions),
                   "--search-mode", "conservative"), run.timeouts.tool_init)
    _verify_brain(gen, model)
    _tool_ok(run, ("sources", "add", SOURCE_ID, "--path", str(gen.mirror), "--name", SOURCE_ID, "--json"),
             run.timeouts.tool_source)
    _tool_ok(run, ("sources", "mirror-readonly", SOURCE_ID), run.timeouts.tool_source)
    _tool_ok(run, ("sources", "default", SOURCE_ID), run.timeouts.tool_source)


def _phase_sync(run: _Run, manifest: Mapping) -> None:
    data, _ = _tool_json(run, ("sync", "--source", SOURCE_ID, "--no-pull", "--json", "--no-hard-deadline"),
                         run.timeouts.tool_sync, CODE_INCOMPLETE)
    source = data
    if isinstance(data, Mapping) and isinstance(data.get("sources"), list) and data["sources"]:
        source = data["sources"][0]
    if not isinstance(source, Mapping):
        raise Refusal(CODE_INCOMPLETE, "gbrain sync answered in a form this version does not know. Check the "
                      "gbrain version and retry.")
    watermark = source.get("watermark")
    through = watermark.get("indexed_through") if isinstance(watermark, Mapping) else None
    indexed = (through.get("commit") if isinstance(through, Mapping) else None) or source.get("target_commit")
    if indexed and indexed != manifest["mirror_commit"]:
        raise Refusal(CODE_INCOMPLETE, f"gbrain indexed {str(indexed)[:12]}, not the mirrored snapshot "
                      f"{manifest['mirror_commit'][:12]}. Retry the build.")


def _count(data: object, key: str) -> int:
    value = data.get(key) if isinstance(data, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _phase_embed(run: _Run, beat: Callable[[], None]) -> None:
    """Embed what is not embedded yet, round by round; what a stopped round finished stays finished."""
    for _ in range(MAX_EMBED_ROUNDS):
        data, _ = _tool_json(run, ("embed", "--stale", "--source", SOURCE_ID, "--json"), run.timeouts.tool_embed,
                             CODE_INCOMPLETE)
        beat()
        if _count(data, "failures"):
            raise Refusal(CODE_INCOMPLETE, f"{_count(data, 'failures')} passages could not be embedded. Check that "
                          "Ollama is running with the model, then build again; it continues where it stopped.")
        if not _count(data, "embedded"):
            return
    raise Refusal(CODE_INCOMPLETE, "embedding did not finish in the allowed rounds. Build again; it continues "
                  "where it stopped.")


def _phase_verify(run: _Run, manifest: Mapping) -> tuple[int, int, dict | None]:
    """(pages, passages, the notes the tool made no page of). Every passage must be embedded."""
    data, _ = _tool_json(run, ("stats", "--json"), run.timeouts.tool_stats, CODE_INCOMPLETE)
    pages, chunks, embedded = (_count(data, key) for key in ("page_count", "chunk_count", "embedded_count"))
    if chunks <= 0:
        raise Refusal(CODE_INCOMPLETE, f"gbrain made no passage from the {len(manifest['entries'])} selected notes "
                      f"({pages} became pages), so there is nothing to search by meaning. Text search still works.")
    if embedded != chunks:
        raise Refusal(CODE_INCOMPLETE, f"{embedded} of {chunks} passages are embedded, so the index is not "
                      "complete. Build again; it continues where it stopped.")
    return pages, chunks, _unindexed(run, manifest)


def _unindexed(run: _Run, manifest: Mapping) -> dict | None:
    """The selected notes that are not pages of the index, or None when the tool cannot say. gbrain holds a note
    back, for example, when its frontmatter declares a slug of its own; such a note is found by text search only."""
    entries = manifest["entries"]
    try:
        data, _ = _tool_json(run, ("list", "--json", "--limit", str(len(entries) + 100)), run.timeouts.tool_stats)
    except Refusal:
        return None
    if not isinstance(data, list):
        return None
    indexed = {row["slug"].rsplit("/", 1)[-1] for row in data
               if isinstance(row, Mapping) and isinstance(row.get("slug"), str)}
    missing = [entry["path"] for entry in entries if entry["id"] not in indexed]
    return {"count": len(missing), "paths": missing[:50]}


def _prune(index_dir: pathlib.Path, generations: pathlib.Path, keep: str, timeouts: Timeouts) -> None:
    """Remove the generations the new index replaced, unless a search still uses one (then the next build does)."""
    try:
        with StateLock(index_dir / "query.lock", timeout=timeouts.lock):
            for child in sorted(generations.iterdir()):
                if child.name != keep:
                    _discard(generations, child.name)
    except (LockTimeout, OSError):
        pass


def _build(worktree: os.PathLike | str, tools: object, selection: object, commit: object,
           state_root: os.PathLike | str | None, env: Mapping[str, str] | None, limits: Limits,
           timeouts: Timeouts, progress: Callable[[dict], None] | None, rebuild: object) -> dict:
    if not isinstance(rebuild, bool):
        raise Refusal(CODE_USAGE, "rebuild must be true or false")
    place = _place(worktree, state_root, env)
    local = _local_tools(tools)
    if local is None:
        raise Refusal(CODE_TOOL_NOT_CONFIGURED, "no local tools are configured. Give the gbrain command, the Ollama "
                      "address and the embedding model.")
    chosen = _selection(place, selection)
    if place.index_dir is None:
        raise place.problem or Refusal(CODE_STATE, "the per-user index folder is unknown")
    command = _qualify_command(local.gbrain, place)
    base = _loopback_url(local.ollama_url)
    snapshot = _snapshot(place, _resolve_commit(place, commit, timeouts), chosen, limits, timeouts)
    _, model = _checked_model(base, local, timeouts)
    fingerprint = _fingerprint(snapshot, model)
    generations = _private(place.index_dir / "generations")
    index_dir = generations.parent
    try:
        with StateLock(index_dir / "build.lock", timeout=0.0):
            changed, resumed = _build_locked(place, index_dir, generations, snapshot, model, fingerprint, command,
                                             base, timeouts, progress, rebuild)
    except LockTimeout:
        raise Refusal(CODE_BUSY, "another build of this wiki index is running. Wait for it to finish.") from None
    return {"schema": SCHEMA, "ok": True, "action": "build", "changed": changed, "resumed": resumed,
            **_describe(place, chosen, local, limits, timeouts)}


def _build_locked(place: _Place, index_dir: pathlib.Path, generations: pathlib.Path, snapshot: _Snapshot,
                  model: _Model, fingerprint: str, command: tuple[str, ...], base: str, timeouts: Timeouts,
                  progress: Callable[[dict], None] | None, rebuild: bool) -> tuple[bool, bool]:
    active, pending = _load_state(index_dir)
    pending_file = index_dir / "pending.json"
    if active is not None and active.get("fingerprint") == fingerprint and not rebuild:
        if pending is not None:  # a stopped build of other input: its folder is not a good index, drop it
            _discard(generations, pending["generation"])
            unlink_durable(pending_file)
        return False, False
    resumed = (pending is not None and pending.get("fingerprint") == fingerprint
               and (generations / pending["generation"]).is_dir())
    if resumed:
        gen = _Generation(generations / pending["generation"])
        done = [phase for phase in PHASES if phase in (pending.get("done") or [])]
        record = dict(pending)
    else:
        if pending is not None:
            _discard(generations, pending["generation"])
        gen = _Generation(scratch.make("g-", root=generations))
        done = []
        record = {"schema": SCHEMA, "generation": gen.name, "fingerprint": fingerprint, "started_at": _now(),
                  "attempts": 0}
    _private(gen.tmp)
    record.update(commit=snapshot.commit, wiki_dir=snapshot.wiki_dir, manifest_digest=snapshot.digest,
                  files=len(snapshot.entries), attempts=int(record.get("attempts") or 0) + 1, error=None)
    run = _Run(command, base, gen, timeouts)

    def note(phase: str) -> None:
        record.update(phase=phase, done=list(done), updated_at=_now())
        write_json_atomic(pending_file, record)
        if progress is not None:
            progress({"phase": phase, "done": list(done), "generation": gen.name})

    try:
        manifest = read_json(gen.manifest, None) if "mirror" in done else None
        if not (isinstance(manifest, dict) and manifest.get("digest") == snapshot.digest
                and isinstance(manifest.get("mirror_commit"), str)):
            done.clear()  # without its manifest a generation cannot be continued: start its steps again
            note("mirror")
            manifest = _phase_mirror(place, snapshot, run)
            done.append("mirror")
        if "brain" not in done:
            del done[1:]
            note("brain")
            _phase_brain(run, model)
            done.append("brain")
        if "sync" not in done:
            note("sync")
            _phase_sync(run, manifest)
            done.append("sync")
        note("embed")
        _phase_embed(run, lambda: note("embed"))
        note("verify")
        pages, chunks, unindexed = _phase_verify(run, manifest)
        version = _version(run)
    except _EXPECTED as exc:
        refusal = _as_refusal(exc)
        record.update(done=list(done), updated_at=_now(), error={"code": refusal.code, "message": refusal.message})
        write_json_atomic(pending_file, record)
        raise refusal from None
    write_json_atomic(index_dir / "current.json", {
        "schema": SCHEMA, "rule": RULE, "generation": gen.name, "fingerprint": fingerprint,
        "worktree": str(place.root), "commit": snapshot.commit, "wiki_dir": snapshot.wiki_dir,
        "manifest_digest": snapshot.digest, "files": len(snapshot.entries), "bytes": snapshot.total_bytes,
        "pages": pages, "chunks": chunks, "unindexed": unindexed, "ollama_url": base, "built_at": _now(),
        "model": {"name": model.name, "resolved": model.resolved, "digest": model.digest,
                  "dimensions": model.dimensions},
        "tool": {"command": list(command), "version": version}})
    unlink_durable(pending_file)
    _prune(index_dir, generations, gen.name, timeouts)
    return True, resumed


def build(worktree: os.PathLike | str, tools: object, *, selection: object = None, commit: str | None = None,
          rebuild: bool = False, state_root: os.PathLike | str | None = None,
          env: Mapping[str, str] | None = None, limits: Limits | None = None, timeouts: Timeouts | None = None,
          progress: Callable[[dict], None] | None = None) -> dict:
    """Create or refresh the index of the selected notes at a committed snapshot.

    The last good index stays in use until the new one is complete; a stopped build is continued by the next call
    with the same input. An index that is current is left as it is ("changed": false) unless rebuild is true, which
    builds it anew beside the old one (the way to repair an index whose files were damaged). A refusal also carries
    "state", "active" and "pending" when they can be read.

    progress, when given, is called with {"phase", "done", "generation"} as the build moves on. An exception it
    raises stops the build and comes out of this call; the build can be continued later."""
    limits, timeouts = _defaults(limits, timeouts)
    try:
        return _build(worktree, tools, selection, commit, state_root, env, limits, timeouts, progress, rebuild)
    except _EXPECTED as exc:
        view = status(worktree, selection=selection, tools=tools, state_root=state_root, env=env, limits=limits,
                      timeouts=timeouts)
        keep = {key: view[key] for key in ("state", "reasons", "active", "pending", "state_dir") if view.get("ok")}
        return _refused("build", exc, **keep)


# ---- reading notes: title, heading, line ----

def _note_lines(text: str) -> list[str]:
    return [strip_eol(line + "\n") for line in text.split("\n")]


def _outline(lines: Sequence[str]) -> tuple[int, list[tuple[int, int, str]]]:
    """(the index of the first line of the body that has text, the headings outside code fences as (index, level,
    text))."""
    span = frontmatter_span(list(lines))
    body = span[1] + 1 if span is not None else 0
    start = next((index for index in range(body, len(lines)) if lines[index].strip()), min(body, len(lines) - 1))
    fence = FenceTracker()
    headings = []
    for index in range(body, len(lines)):
        if fence.feed(lines[index]):
            continue
        match = _HEADING.match(lines[index])
        if match is not None and match.group(2):
            headings.append((index, len(match.group(1)), match.group(2)))
    return start, headings


def _title(path: str, text: str, headings: Sequence[tuple[int, int, str]]) -> str:
    front = parse_frontmatter(text)
    declared = front.get("title") if front else None
    if isinstance(declared, str) and declared.strip():
        return declared.strip()[:200]
    first = next((text_ for _, level, text_ in headings if level == 1), None)
    return (first or pathlib.PurePosixPath(path).stem)[:200]


def _heading_at(headings: Sequence[tuple[int, int, str]], index: int) -> str:
    found = ""
    for position, _, text in headings:
        if position > index:
            break
        found = text
    return found[:200]


def _snippet(lines: Sequence[str], index: int, limit: int) -> str:
    words: list[str] = []
    size = 0
    for line in lines[index:index + 12]:
        words.extend(line.split())
        size = sum(len(word) + 1 for word in words)
        if size > limit:
            break
    text = " ".join(words)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _find_passage(lines: Sequence[str], passage: str) -> tuple[int, int] | None:
    """(the index of the passage's first line, how many lines it has), found by its first substantial lines; None
    when the note does not hold it."""
    own = [line.strip() for line in passage.split("\n")]
    while own and not own[-1]:
        own.pop()
    while own and not own[0]:
        own.pop(0)
    anchors = [(position, line[:80]) for position, line in enumerate(own) if len(line) >= 12][:6]
    if not anchors:
        anchors = [(position, line[:80]) for position, line in enumerate(own) if len(line) >= 3][:1]
    for number, (position, probe) in enumerate(anchors):
        starts = [index for index, line in enumerate(lines) if probe in line]
        if len(starts) > 1 and number + 1 < len(anchors):  # a repeated line: the next one tells which it is
            gap, following = anchors[number + 1][0] - position, anchors[number + 1][1]
            starts = ([index for index in starts if index + gap < len(lines) and following in lines[index + gap]]
                      or starts)
        if starts:
            first = starts[0] - position
            if first >= 0 and all(own[offset] == lines[first + offset].strip() for offset in range(position)):
                return first, len(own)  # the short lines before it (a heading, mostly) belong to the passage
            return starts[0], len(own) - position
    return None


def _query_terms(query: str) -> list[str]:
    return [term for term in dict.fromkeys(query.casefold().split()) if len(term) >= 2][:16]


def _best_line(lines: Sequence[str], first: int, length: int, terms: Sequence[str]) -> int:
    """The line of a passage that shares the most words with the query; the passage's first line when none does.
    A tool returns whole passages, and the words asked for may stand far below the passage's start."""
    best, most = first, 0
    for index in range(first, min(first + length, len(lines))):
        folded = lines[index].casefold()
        shared = sum(1 for term in terms if term in folded)
        if shared > most:
            best, most = index, shared
    return best


def _safe_path(root: pathlib.Path, relative: str, *, folder: bool) -> pathlib.Path | None:
    """root/relative when every component is a real folder (and the last a real file unless folder), else None."""
    current = root
    parts = relative.split("/")
    for position, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return None
        last = position == len(parts) - 1
        wanted = stat.S_ISDIR if (folder or not last) else stat.S_ISREG
        if _is_link(info) or not wanted(info.st_mode):
            return None
    return current


def _same_in_worktree(place: _Place, path: str, index: int, line: str, limits: Limits) -> bool:
    """Whether the working-tree file still has this text on this line."""
    target = _safe_path(place.root, path, folder=False)
    try:
        if target is None or target.stat().st_size > limits.max_file_bytes:
            return False
        lines = _note_lines(target.read_bytes().decode("utf-8", "replace"))
    except OSError:
        return False
    return index < len(lines) and lines[index] == line


# ---- text search over the working tree ----

def _working_notes(place: _Place, selection: Selection, limits: Limits) -> tuple[list[tuple[str, pathlib.Path]], bool]:
    """The notes as they are on disk now, by the same rule as the snapshot: no link is followed."""
    top = _safe_path(place.root, selection.wiki_dir, folder=True)
    if top is None:
        return [], False
    found: list[tuple[str, pathlib.Path]] = []
    stack: list[tuple[tuple[str, ...], pathlib.Path]] = [((), top)]
    while stack:
        parts, current = stack.pop()
        try:
            with os.scandir(current) as listing:
                children = sorted(listing, key=lambda child: child.name)
        except OSError:
            continue
        folders = []
        for child in children:
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                continue
            name = (*parts, child.name)
            if _is_link(info) or _unsafe_name(child.name):
                continue
            if stat.S_ISDIR(info.st_mode):
                if _skip_reason((*name, "note.md")) is None:
                    folders.append((name, pathlib.Path(child.path)))
            elif (stat.S_ISREG(info.st_mode) and _skip_reason(name) is None
                  and 0 < info.st_size <= limits.max_file_bytes):
                if len(found) >= limits.max_files:
                    return found, True
                found.append(("/".join((selection.wiki_dir, *name)), pathlib.Path(child.path)))
        stack.extend(reversed(folders))
    return found, False


def _clean_query(query: object, limits: Limits) -> str:
    if not isinstance(query, str):
        raise Refusal(CODE_USAGE, "query must be text")
    text = " ".join(query.split()).lstrip("-").strip()  # one line, and never something a tool reads as an option
    if not text:
        raise Refusal(CODE_USAGE, "query is empty. Give a few words to look for.")
    return text[:limits.max_query_chars]


def _result_limit(limit: object, limits: Limits) -> int:
    if limit is None:
        return limits.max_results
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise Refusal(CODE_USAGE, "limit must be a whole number of at least 1")
    return min(limit, limits.max_results)


def _text_hits(place: _Place, selection: Selection, query: str, count: int,
               limits: Limits) -> tuple[list[dict], bool, int]:
    terms = list(dict.fromkeys(term.casefold() for term in query.split()))[:8]
    phrase = query.casefold()
    notes, truncated = _working_notes(place, selection, limits)
    hits: list[dict] = []
    read = 0
    for path, target in notes:
        try:
            data = target.read_bytes()
        except OSError:
            continue
        read += len(data)
        if read > limits.max_total_bytes:
            truncated = True
            break
        text = data.decode("utf-8", "replace")
        lines = _note_lines(text)
        start, headings = _outline(lines)
        folded = [line.casefold() for line in lines]
        matched = [{term for term in terms if term in line} for line in folded]
        in_path = {term for term in terms if term in path.casefold()}
        covered = set().union(in_path, *matched)
        if not covered:
            continue
        ranked = sorted((index for index, found in enumerate(matched) if found),
                        key=lambda index: (-len(matched[index]), phrase not in folded[index], index))[:3]
        if not ranked:  # only the file name matches: point at the first line of the body
            ranked = [min(start, len(lines) - 1)]
        title = _title(path, text, headings)
        for index in ranked:
            score = 0.6 * len(covered) / len(terms) + 0.4 * len(matched[index]) / len(terms)
            if len(terms) > 1 and phrase in folded[index]:
                score += 0.05
            hits.append({"path": path, "title": title, "heading": _heading_at(headings, index), "line": index + 1,
                         "located": True, "match": "text", "current": True,
                         "snippet": _snippet(lines, index, limits.snippet_chars),
                         "score": round(min(score, 1.0), 4)})
    hits.sort(key=lambda hit: (-hit["score"], hit["path"], hit["line"]))
    return hits[:count], truncated or len(hits) > count, len(notes)


def text_search(worktree: os.PathLike | str, query: str, *, selection: object = None, limit: int | None = None,
                limits: Limits | None = None) -> dict:
    """Look for the words in the notes as they are on disk now. It needs no tool and no index, and it writes nothing."""
    try:
        limits = _defaults(limits, None)[0]
        place = _place(worktree, None, None, stateless=True)
        chosen = _selection(place, selection)
        text = _clean_query(query, limits)
        results, truncated, notes = _text_hits(place, chosen, text, _result_limit(limit, limits), limits)
        return {"schema": SCHEMA, "ok": True, "action": "text_search", "mode": "text", "query": text,
                "results": results, "truncated": truncated, "notes": notes}
    except _EXPECTED as exc:
        return _refused("text_search", exc)


# ---- search: the index when it can answer, the files otherwise ----

def _stored_model(active: Mapping, tools: LocalTools) -> Mapping:
    """The model the index was built with, or a refusal when the configured one is another."""
    stored = active["model"]
    if (_model_names(tools.embedding_model)[1] != stored.get("resolved")
            or tools.embedding_dimensions not in (None, stored.get("dimensions"))):
        raise Refusal(CODE_MODEL_CHANGED, f"the index was built with {stored.get('resolved')} "
                      f"({stored.get('dimensions')} numbers per vector) and the configured model differs. Build "
                      "the index again; until then the notes are searched as text.")
    return stored


def _index_hits(place: _Place, tools: LocalTools | None, query: str, count: int, limits: Limits,
                timeouts: Timeouts) -> dict:
    """Results from the last good index, or a Refusal that says why it cannot answer now."""
    if tools is None:
        raise Refusal(CODE_TOOL_NOT_CONFIGURED, "no local search tools are configured on this machine, so the "
                      "notes were searched as text.")
    command = _qualify_command(tools.gbrain, place)
    base = _loopback_url(tools.ollama_url)
    if place.index_dir is None:
        raise place.problem or Refusal(CODE_STATE, "the per-user index folder is unknown")
    active, pending = _load_state(place.index_dir)
    if active is None:
        if pending is not None:
            raise Refusal(CODE_INCOMPLETE, "the wiki index was started and is not complete yet. Build it again to "
                          "continue; until then the notes are searched as text.")
        raise Refusal(CODE_NOT_BUILT, "the wiki index has not been built. Build it to search by meaning; until "
                      "then the notes are searched as text.")
    _stored_model(active, tools)
    served = _ollama_model(base, tools.embedding_model, timeouts)
    generations = _private(place.index_dir / "generations")
    try:
        # One search at a time, and no build removes a generation while a search reads it.
        with StateLock(generations.parent / "query.lock", timeout=timeouts.lock):
            active, _ = _load_state(generations.parent)
            if active is None:
                raise Refusal(CODE_NOT_BUILT, "the wiki index is no longer there. Build it again; until then the "
                              "notes are searched as text.")
            if served.digest != _stored_model(active, tools).get("digest"):
                raise Refusal(CODE_MODEL_CHANGED, f"{served.resolved} changed on the Ollama server since the index "
                              "was built. Build the index again; until then the notes are searched as text.")
            gen = _Generation(generations / active["generation"])
            _private(gen.tmp)
            return _answer(place, _Run(command, base, gen, timeouts), active, query, count, limits)
    except LockTimeout:
        raise Refusal(CODE_BUSY, "another search is using the wiki index. Retry in a moment.") from None


# How gbrain rates a row, in two words a surface can act on: a search by meaning always returns its nearest notes,
# so "weak" marks the ones that are merely nearest. A value this table does not know gives None.
_MATCH = {"exact_title_match": "strong", "high_vector_match": "strong", "keyword_exact": "strong",
          "weak_semantic": "weak"}


def _answer(place: _Place, run: _Run, active: Mapping, query: str, count: int, limits: Limits) -> dict:
    gen = run.gen
    manifest = _record(gen.manifest)
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or manifest.get("digest") != active.get("manifest_digest"):
        raise Refusal(CODE_STATE, "the index folder does not match its record. Rebuild the index.")
    by_id = {entry["id"]: entry for entry in entries
             if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("path"), str)}
    data, stderr = _tool_json(run, ("query", query, "--json", "--source-id", SOURCE_ID, "--no-expand",
                                   "--limit", str(min(50, count * 3)), "--autocut", "false",
                                   "--token-budget", "100000", "--snippet-chars", "400"), run.timeouts.tool_query)
    degraded = list(data["degraded"]) if isinstance(data, Mapping) and isinstance(data.get("degraded"), list) else []
    degraded += re.findall(r"\[gbrain degraded: ([^\]\n]{1,120})\]", stderr)
    rows = data.get("results") if isinstance(data, Mapping) else data
    if not isinstance(rows, list):
        raise Refusal(CODE_TOOL_FAILED, "gbrain query answered in a form this version does not know. Check the "
                      "gbrain version; until then the notes are searched as text.")
    source = manifest.get("source")
    object_format = source.get("object_format") if isinstance(source, Mapping) else None
    notes: dict[str, tuple[str, list[str], int, list]] = {}
    results: list[dict] = []
    seen: set[tuple[str, int]] = set()
    dropped = 0
    terms = _query_terms(query)
    for row in rows:
        slug = row.get("slug") if isinstance(row, Mapping) else None
        entry = by_id.get(slug.rsplit("/", 1)[-1]) if isinstance(slug, str) else None
        if entry is None:
            dropped += 1  # a page this index cannot tie to one note is never guessed at
            continue
        path = entry["path"]
        if path not in notes:
            try:
                content = (gen.mirror / f"{entry['id']}.md").read_bytes()
            except OSError:
                content = b""
            if object_format not in ("sha1", "sha256") or _blob_oid(content, object_format) != entry.get("oid"):
                raise Refusal(CODE_STATE, "a mirrored note no longer matches the indexed snapshot. Rebuild the "
                              "index.")
            text = content.decode("utf-8", "replace")
            lines = _note_lines(text)
            notes[path] = (text, lines, *_outline(lines))
        text, lines, start, headings = notes[path]
        passage = next((row[key] for key in ("chunk_text", "snippet", "text", "content")
                        if isinstance(row.get(key), str) and row[key].strip()), "")
        found = _find_passage(lines, passage)
        index = min(start, len(lines) - 1) if found is None else _best_line(lines, *found, terms)
        if (path, index) in seen:
            continue
        seen.add((path, index))
        score = row.get("score")
        results.append({
            "path": path, "title": _title(path, text, headings), "heading": _heading_at(headings, index),
            "line": index + 1, "located": found is not None, "match": _MATCH.get(row.get("evidence")),
            "current": _same_in_worktree(place, path, index, lines[index], limits),
            "snippet": (_snippet(lines, index, limits.snippet_chars) if found is not None
                        else " ".join(passage.split())[:limits.snippet_chars]),
            "score": round(float(score), 6) if isinstance(score, (int, float)) and not isinstance(score, bool)
            else None})
    return {"results": results[:count], "truncated": len(results) > count, "dropped": dropped,
            "degraded": [str(item)[:120] for item in degraded][:8]}


def search(worktree: os.PathLike | str, query: str, *, tools: object = None, selection: object = None,
           limit: int | None = None, state_root: os.PathLike | str | None = None, fallback: str = "text",
           env: Mapping[str, str] | None = None, limits: Limits | None = None,
           timeouts: Timeouts | None = None) -> dict:
    """Search the wiki: by meaning from the last good index when it can answer ("mode": "semantic"), and otherwise
    as text over the files on disk ("mode": "text"), with the reason in "fallback". fallback="none" returns no text
    results (mode "none") so the caller can run its own search.

    Lines of a semantic result are lines of the indexed snapshot ("index" names its commit and whether it is
    stale); "current" says whether the working-tree file still has that text on that line."""
    try:
        limits, timeouts = _defaults(limits, timeouts)
        if fallback not in ("text", "none"):
            raise Refusal(CODE_USAGE, 'fallback must be "text" or "none"')
        place = _place(worktree, state_root, env)
        chosen = _selection(place, selection)
        local = _local_tools(tools)
        text = _clean_query(query, limits)
        count = _result_limit(limit, limits)
        answer = {"schema": SCHEMA, "ok": True, "action": "search", "query": text}
        try:
            described = _describe(place, chosen, local, limits, timeouts)
            active = described["active"] or {}
            unindexed = active.get("unindexed")
            index = {"state": described["state"], "reasons": described["reasons"], "commit": active.get("commit"),
                     "built_at": active.get("built_at"), "pending": described["pending"] is not None,
                     "unindexed": unindexed.get("count") if isinstance(unindexed, Mapping) else None}
        except _EXPECTED as exc:
            index = {"state": "unknown", "reasons": [_as_refusal(exc).code], "commit": None, "built_at": None,
                     "pending": False, "unindexed": None}
        try:
            return {**answer, "mode": "semantic", "fallback": None, "index": index,
                    **_index_hits(place, local, text, count, limits, timeouts)}
        except _EXPECTED as exc:
            why = _as_refusal(exc)
        answer.update(fallback={"code": why.code, "message": why.message}, index=index)
        if fallback == "none":
            return {**answer, "mode": "none", "results": [], "truncated": False}
        results, truncated, _ = _text_hits(place, chosen, text, count, limits)
        return {**answer, "mode": "text", "results": results, "truncated": truncated}
    except _EXPECTED as exc:
        return _refused("search", exc)


# ---- one JSON request in, one JSON result out ----

_REQUEST_KEYS = {
    "status": ("selection", "tools", "state_root"),
    "probe": ("tools", "state_root"),
    "build": ("tools", "selection", "commit", "rebuild", "state_root"),
    "search": ("query", "tools", "selection", "limit", "state_root", "fallback"),
    "text_search": ("query", "selection", "limit"),
}
_MAX_REQUEST_BYTES = 1_048_576


def handle(request: object) -> dict:
    """Answer one request object: {"action": one of status, probe, build, search, text_search, "worktree": ..., and
    that action's keyword arguments}."""
    action = request.get("action") if isinstance(request, Mapping) else None
    if action not in _REQUEST_KEYS:
        return _refused(str(action), Refusal(CODE_USAGE, "the request must be a JSON object whose action is one of "
                                             + ", ".join(_REQUEST_KEYS)))
    unknown = sorted(set(request) - {"action", "worktree", *_REQUEST_KEYS[action]})
    worktree = request.get("worktree")
    if unknown or not isinstance(worktree, str) or not worktree:
        return _refused(action, Refusal(CODE_USAGE, f"unknown key {unknown[0]} for {action}" if unknown
                                        else "worktree must be the project folder's path"))
    options = {key: request[key] for key in _REQUEST_KEYS[action] if key in request}
    if action == "status":
        return status(worktree, **options)
    if action == "probe":
        return probe(worktree, options.pop("tools", None), **options)
    if action == "build":
        return build(worktree, options.pop("tools", None), **options)
    if action == "search":
        return search(worktree, options.pop("query", None), **options)
    return text_search(worktree, options.pop("query", None), **options)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments:
        sys.stderr.write("usage: python -m oah.project_tools_wiki_index  (one JSON request on standard input)\n")
        return 2
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > _MAX_REQUEST_BYTES:
            raise ValueError("the request is too large")
        request = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        request = None
        result = _refused("request", Refusal(CODE_USAGE, f"standard input is not one JSON object ({exc})"))
    if request is not None:
        try:
            result = handle(request)
        except Exception:  # a defect: the caller gets no result to mistake for an answer
            import traceback

            traceback.print_exc()
            return 3
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True, sort_keys=True).encode("ascii") + b"\n")
    sys.stdout.buffer.flush()
    return 0 if result.get("ok") else 2 if result.get("code") == CODE_USAGE else 1


if __name__ == "__main__":
    raise SystemExit(main())
