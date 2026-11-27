# 프로젝트 도구의 영향 지도 연산 — 선택한 작업 트리 하나의 지도 상태를 읽거나 갱신한다. 앱이 고른 대상은 루트·Git 메타데이터·저장 경로·자식 환경을 검증한 안전 프로필 안에서만 git 을 부른다.
"""The Impact Map operation behind Team Workflow (project tools).

Two entries, `status` (read only) and `refresh` (build the first map, then keep it fresh), each for exactly one
worktree and entirely offline. Both call the existing map store (`oah.impact.mapstore.status` and
`ensure_fresh`), so the map file, its lock and its freshness rules are the ones `oah impact` already uses:
`<state>/impact/maps/<worktree key>.json`, where `<state>` is `OAH_STATE_DIR` or else `<git common dir>/oah`.

Source mode (`selected=False`) is the old behaviour for a project nobody selected in the app: the ambient
environment, the git found on PATH and the repository's own configuration are trusted, exactly as in the CLI.

App-selected mode (`selected=True`) treats the folder's contents, `.git/config` included, as untrusted:

- Root. An absolute, real folder that directly holds `.git`: a real folder (main worktree), or the pointer file of
  a standard linked worktree whose `<common>/worktrees/<id>` points back at it. A symlink or junction, a subfolder
  of a repository, a submodule or separate git dir and a bare-looking folder are refused. git must then report the
  same worktree root and common dir.
- Git. One explicit executable outside the project (`--git`, or the first `git` on an absolute PATH entry),
  version 2.31 or newer, pinned for the whole operation.
- Child configuration. Every git child gets `-c` overrides, which no repository, user or system file can outrank:
  fsmonitor off, hooks path at the null device, pager `cat`, signature display off, no credential helper, no
  transport, no submodule recursion, no automatic maintenance, and every configured filter driver emptied.
- Child environment. Every `GIT_*` variable is dropped except the user's own config selectors
  (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_NOSYSTEM`); discovery stops at the root's parent, optional
  locks, prompts, lazy fetches, tracing and every transport protocol are off. The user's global and system git
  configuration still applies, so ignore rules and line-ending settings match the source CLI.
- Variables that still steer a run: `OAH_STATE_DIR` (honoured when absolute, refused when relative), `PATH` (only to
  find git when `--git` is absent), the home and config variables git reads, and the temporary folder.
- Store. `oah`, `oah/impact`, `oah/impact/maps`, the map file and the lock must not be symlinks or junctions.
- Repository settings. `.oah/config.toml` cannot raise the time limits or name paths outside the project.

Nothing here runs a repository script, hook, filter, fsmonitor, pager, shell or network transport. A caller that
needs a wall-clock limit ends the process: the map is replaced atomically and the operating system releases the lock.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import re
import stat
import sys
from dataclasses import dataclass, field
from typing import Iterator, Mapping

import oah
from oah import paths
from oah.config import CONFIG_RELATIVE_PATH
from oah.engines.proc import Resolved, is_batch, run_command
from oah.impact import mapstore, query
from oah.impact.defaults import configured

RUNNER_NAME = "oah-project-tools"
SCHEMA = 1
STATES = ("fresh", "stale", "missing", "no_commit", "not_a_repository", "unavailable")
GRAPH_STATES = ("absent", "available", "stale", "unproven")
MIN_GIT = (2, 31)
MAX_GIT_TIMEOUT = 900.0   # seconds one git call may take, whatever the repository's settings ask for
MAX_LOCK_TIMEOUT = 120.0
PROBE_TIMEOUT = 30.0
POINTER_LIMIT = 4096      # bytes of a .git pointer file
CONFIG_LIMIT = 1 << 20    # bytes of .oah/config.toml
GRAPH_LIMIT = 64 << 20    # bytes of the graph file
FILTER_LIMIT = 64         # filter drivers one run will neutralise
LIST_LIMIT = 100
WARNING_LIMIT = 20
TEXT_LIMIT = 2000
KEPT_GIT_ENV = frozenset({"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"})
SAFE_GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_PAGER": "cat",
    # An allow-list that names no protocol, so it denies every transport and outranks protocol.*.allow. A lone
    # colon rather than "": Windows drops a variable that is set to the empty string.
    "GIT_ALLOW_PROTOCOL": ":",
    "GIT_TRACE": "0", "GIT_TRACE2": "0", "GIT_TRACE2_EVENT": "0", "GIT_TRACE2_PERF": "0",
}
SAFE_GIT_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", os.devnull),
    ("core.pager", "cat"),
    ("core.virtualFilesystem", ""),
    ("credential.helper", ""),
    ("gc.auto", "0"),
    ("log.showSignature", "false"),
    ("maintenance.auto", "false"),
    ("protocol.allow", "never"),
    ("status.submoduleSummary", "false"),
    ("submodule.recurse", "false"),
)
FILTER_SETTINGS = (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))
NOT_A_REPOSITORY = "this folder is not a Git repository; run git init here and make a first commit"
NO_COMMIT = "this repository has no commits yet; make a first commit"


class ProjectToolsError(Exception):
    """A refusal with a stable code; the operation turns it into a failed Outcome."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class GraphAttachment:
    """What an existing graphify graph file is worth for this commit. Never an input of the map."""

    state: str   # absent, available, stale or unproven
    detail: str
    file: str
    commit: str | None = None  # the full commit id the graph records, when it records one
    nodes: int = 0
    edges: int = 0
    # {"files": {node: path}, "edges": [[a, b, relation]]} for query.query(graph=...), only when available. Its
    # neighbours become mention rows; they never replace or reorder a pin or exec row.
    graph: dict | None = field(default=None, compare=False, repr=False)

    def to_json(self) -> dict:
        return {"state": self.state, "detail": _clip(self.detail), "file": _clip(self.file), "commit": self.commit,
                "nodes": self.nodes, "edges": self.edges}


@dataclass(frozen=True)
class Outcome:
    action: str | None
    ok: bool
    state: str
    reason: str = ""
    performed: str = "none"   # none, built or refreshed
    changed: int = 0
    root: str = ""
    branch: str | None = None
    head: str | None = None
    store: dict | None = None
    map: dict | None = None
    graph: GraphAttachment | None = None
    git: dict | None = None
    error: dict | None = None
    warnings: tuple[str, ...] = ()
    tool: str | None = "impact"

    def to_json(self) -> dict:
        return {"schema": SCHEMA, "tool": self.tool, "action": self.action, "ok": self.ok, "state": self.state,
                "reason": _clip(self.reason), "performed": self.performed, "changed": self.changed,
                "root": self.root, "branch": self.branch, "head": self.head, "store": self.store, "map": self.map,
                "graph": self.graph.to_json() if self.graph is not None else None,
                "runner": {"name": RUNNER_NAME, "version": oah.__version__,
                           "frozen": bool(getattr(sys, "frozen", False))},
                "git": self.git, "error": self.error,
                "warnings": [_clip(text, 500) for text in self.warnings[:WARNING_LIMIT]]}


@dataclass
class _Facts:
    """What a run has learned so far, so a refusal still reports it."""

    action: str | None
    root: str
    branch: str | None = None
    head: str | None = None
    store: dict | None = None
    git: dict | None = None

    def outcome(self, *, ok: bool, state: str, **fields: object) -> Outcome:
        return Outcome(action=self.action, ok=ok, state=state, root=self.root, branch=self.branch, head=self.head,
                       store=self.store, git=self.git, **fields)

    def failed(self, code: str, message: str, state: str = "unavailable") -> Outcome:
        return self.outcome(ok=False, state=state, reason=message,
                            error={"code": code, "message": _clip(message)})

    def prerequisite(self, state: str, code: str, message: str) -> Outcome:
        """Something the user has to do first: a state for status, a refusal for refresh."""
        if self.action == "status":
            return self.outcome(ok=True, state=state, reason=message)
        return self.failed(code, message, state)


@dataclass(frozen=True)
class _Layout:
    common: pathlib.Path  # the git common dir this root proves to belong to
    linked: bool


def status(root: str | os.PathLike, *, selected: bool = False, git: str | None = None) -> Outcome:
    """Read only: where the worktree's map stands. Creates nothing, locks nothing, never raises a refusal."""
    return _operate("status", root, selected, git)


def refresh(root: str | os.PathLike, *, selected: bool = False, git: str | None = None) -> Outcome:
    """Build the first map or bring the stored one up to date. A fresh map is left untouched."""
    return _operate("refresh", root, selected, git)


def failure(action: str | None, root: str, code: str, message: str, *, tool: str | None = "impact") -> Outcome:
    """A failed Outcome for a caller that could not start the operation at all."""
    return Outcome(action=action, ok=False, state="unavailable", reason=message, root=root, tool=tool,
                   error={"code": code, "message": _clip(message)})


def _operate(action: str, root: str | os.PathLike, selected: bool, git: str | None) -> Outcome:
    facts = _Facts(action, os.fspath(root))
    try:
        if selected:
            return _selected_run(facts, git)
        return _source_run(facts)
    except ProjectToolsError as exc:
        return facts.failed(exc.code, exc.message)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.startswith("OAH_"):  # the typed errors of the map store and its helpers
            return facts.failed(code, str(exc).removeprefix(f"{code}: "))
        if isinstance(exc, OSError):
            return facts.failed("OAH_PROJECT_TOOLS_IO", f"{exc.filename or 'a file'}: {exc.strerror or exc}")
        raise


def _source_run(facts: _Facts) -> Outcome:
    try:
        info = paths.repo_info(pathlib.Path(facts.root))
    except paths.NotARepo:
        return facts.prerequisite("not_a_repository", paths.NotARepo.code, NOT_A_REPOSITORY)
    facts.root = str(info.root)
    return _map_step(facts, info, None, None, strict=False)


def _selected_run(facts: _Facts, git: str | None) -> Outcome:
    root = _bind_root(facts.root)
    facts.root = str(root)
    layout = _git_layout(root)
    if layout is None:
        return facts.prerequisite("not_a_repository", paths.NotARepo.code, NOT_A_REPOSITORY)
    if mapstore._bare(root):  # the store would take such a folder for a bare mirror and run git differently
        raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_LAYOUT", f"{root} holds a HEAD file and an objects folder "
                                "at its top, like a bare repository; open the project folder that holds .git")
    with _safe_git(root, layout, git, facts) as argv:
        try:
            info = paths.repo_info(root)
        except paths.NotARepo:
            probe = run_command((*argv, "-C", str(root), "rev-parse", "--show-toplevel"), timeout=PROBE_TIMEOUT)
            detail = _first_line(probe.stderr)
            raise ProjectToolsError(paths.NotARepo.code, f"git does not accept {root} as a repository"
                                    + (f": {detail}" if detail else "")) from None
        if not _same(info.root, root) or not _same(info.common_dir, layout.common):
            raise ProjectToolsError("OAH_PROJECT_TOOLS_ROOT_MISMATCH", f"git reports worktree {info.root} and "
                                    f"common dir {info.common_dir}, not the selected {root} and {layout.common}; "
                                    "open the folder that holds this project's .git")
        env = _store_env()
        state = paths.state_dir(info, env)
        _verify_store(state, mapstore.map_path(state, info.root), default=not env)
        return _map_step(facts, info, env, _bounded_settings(root), strict=True)


def _map_step(facts: _Facts, info: paths.RepoInfo, env: Mapping[str, str] | None,
              settings: dict | None, *, strict: bool) -> Outcome:
    where = os.environ if env is None else env
    facts.branch, facts.head = info.branch, info.head
    facts.store = {"path": str(mapstore.map_path(paths.state_dir(info, where), info.root)),
                   "source": paths.STATE_ENV if where.get(paths.STATE_ENV) else "git-common-dir"}
    if info.head is None:
        return facts.prerequisite("no_commit", "OAH_IMPACT_NO_COMMIT", NO_COMMIT)
    if facts.action == "status":
        found, reason = mapstore.status(info.root, env=env, settings=settings)
        state, performed, changed = ("missing" if found is None else found.action), "none", 0
        reason = "" if state == "fresh" else reason
    else:
        found = mapstore.ensure_fresh(info.root, refresh=True, env=env, settings=settings)
        state, changed, reason = "fresh", found.changed, found.reason
        performed = "none" if found.action == "fresh" else found.action
    graph = graph_attachment(info.root, configured(info.root) if settings is None else settings, info.head,
                             strict=strict)
    return facts.outcome(ok=True, state=state, reason=reason, performed=performed, changed=changed,
                         map=_map_summary(found) if found is not None else None, graph=graph)


def _map_summary(info: mapstore.MapInfo) -> dict:
    """The stored map in a bounded shape: its identity, and whether uncommitted work went into it."""
    summary = {"commit": info.commit, "updated_at": None, "manifest": info.manifest_digest,
               "tool": info.tool_digest, "result": info.result_digest, "dirty": 0, "untracked": 0,
               "includes_uncommitted": False, "excluded_paths": list(info.excluded_paths[:LIST_LIMIT]),
               "excluded_total": len(info.excluded_paths), "sql_unparsed": len(info.sql_unparsed),
               "warnings": [_clip(text, 500) for text in info.warnings()[:WARNING_LIMIT]]}
    try:
        stored = mapstore.load(info.path)
    except (OSError, ValueError):
        return summary
    manifest = stored.get("manifest") if isinstance(stored.get("manifest"), dict) else {}
    dirty, untracked = (len(manifest.get(name) or ()) for name in ("dirty", "untracked"))
    updated = stored.get("updated_at")
    summary.update(updated_at=updated if isinstance(updated, str) else None, dirty=dirty, untracked=untracked,
                   includes_uncommitted=bool(dirty or untracked))
    return summary


# --- the graphify attachment ---------------------------------------------------------------------------

def graph_attachment(root: pathlib.Path, settings: Mapping, head: str | None, *, strict: bool = True) -> GraphAttachment:
    """Read an existing graph file, never build one. Strict mode only reads a regular file inside the project."""
    name = str(settings["graph_file"])
    path = pathlib.Path(root) / name
    if strict:
        problem = _unsafe_relative(name)
        if problem:
            return GraphAttachment("unproven", f"graph_file {problem}, so it is not read", name)
        path = pathlib.Path(root).joinpath(*pathlib.PurePosixPath(name).parts)
        link = _linked_component(pathlib.Path(root), pathlib.PurePosixPath(name).parts)
        if link is not None:
            return GraphAttachment("unproven", f"{link} is a symlink or junction, so the graph is not read", name)
    try:
        found = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return GraphAttachment("absent", f"{name} does not exist", name)
    except OSError as exc:
        return GraphAttachment("unproven", f"{name} cannot be read ({exc.strerror or exc})", name)
    if not stat.S_ISREG(found.st_mode):
        return GraphAttachment("unproven", f"{name} is not a regular file", name)
    if found.st_size > GRAPH_LIMIT:
        return GraphAttachment("unproven", f"{name} is larger than {GRAPH_LIMIT} bytes, so it is not read", name)
    graph, verdict, reason, recorded = query.graph_identity(root, settings, head)
    if graph is None:
        return GraphAttachment("unproven", reason, name)
    files = {node: file for node, file in graph["files"].items() if _unsafe_relative(file) is None}
    counts = {"nodes": len(files), "edges": len(graph["edges"])}
    if verdict == "analysed" and head is not None:
        return GraphAttachment("available", reason, name, recorded, graph={"files": files, "edges": graph["edges"]},
                               **counts)
    if verdict == "stale":
        return GraphAttachment("stale", reason, name, recorded, **counts)
    return GraphAttachment("unproven", reason, name, recorded, **counts)


# --- the selected root ---------------------------------------------------------------------------------

def _bind_root(raw: str) -> pathlib.Path:
    if not _absolute(raw):
        raise ProjectToolsError("OAH_PROJECT_TOOLS_ROOT_INVALID", f"--root {raw!r} is not an absolute path")
    try:
        found = os.lstat(raw)
    except OSError as exc:
        raise ProjectToolsError("OAH_PROJECT_TOOLS_ROOT_INVALID",
                                f"{raw} cannot be opened ({exc.strerror or exc})") from None
    if _is_link(found):
        raise ProjectToolsError("OAH_PROJECT_TOOLS_ROOT_INVALID", f"{raw} is a symlink or junction; "
                                "open the real project folder")
    if not stat.S_ISDIR(found.st_mode):
        raise ProjectToolsError("OAH_PROJECT_TOOLS_ROOT_INVALID", f"{raw} is not a folder")
    return pathlib.Path(os.path.realpath(raw))


def _git_layout(root: pathlib.Path) -> _Layout | None:
    """Where this root's git metadata lives; None when the folder holds no .git at all."""
    dot = root / ".git"
    try:
        found = os.lstat(dot)
    except FileNotFoundError:
        return None
    if _is_link(found):
        raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_LAYOUT", f"{dot} is a symlink or junction; "
                                "open a project whose .git is a real folder or a worktree pointer file")
    if stat.S_ISDIR(found.st_mode):
        if os.path.lexists(dot / "commondir"):
            raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_LAYOUT",
                                    f"{dot} redirects its common dir elsewhere; this layout is not supported")
        return _Layout(dot, False)
    if not stat.S_ISREG(found.st_mode):
        raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_LAYOUT", f"{dot} is neither a folder nor a pointer file")
    pointer = _pointer(dot)
    if not pointer.startswith("gitdir: "):
        raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_LAYOUT", f"{dot} is not a git worktree pointer file")
    gitdir = _resolved(root, pointer.removeprefix("gitdir: "))
    common = gitdir.parent.parent
    unsupported = ProjectToolsError(
        "OAH_PROJECT_TOOLS_GIT_LAYOUT", f"{dot} points at {gitdir}, which is not a linked worktree of a "
        "repository that points back at this folder (a submodule or separate git dir is not supported); "
        "open the main project folder or a worktree made with git worktree add")
    if gitdir.parent.name != "worktrees" or not gitdir.is_dir() or not common.is_dir():
        raise unsupported
    try:
        mutual = (_same(_resolved(gitdir, _pointer(gitdir / "commondir")), common)
                  and _same(_resolved(gitdir, _pointer(gitdir / "gitdir")), dot))
    except ProjectToolsError:
        mutual = False
    if not mutual:
        raise unsupported
    return _Layout(common, True)


def _pointer(path: pathlib.Path) -> str:
    """The one line of a small pointer file (.git, commondir or gitdir)."""
    try:
        found = os.lstat(path)
        if _is_link(found) or not stat.S_ISREG(found.st_mode) or found.st_size > POINTER_LIMIT:
            raise ValueError("not a small regular file")
        text = path.read_bytes().decode("utf-8").rstrip("\r\n")
        if not text or "\n" in text or "\0" in text:
            raise ValueError("not one line")
    except (OSError, ValueError) as exc:
        raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_LAYOUT", f"{path} cannot be read as a git pointer "
                                f"({exc})") from None
    return text


def _resolved(base: pathlib.Path, text: str) -> pathlib.Path:
    return pathlib.Path(os.path.realpath(base / text))


# --- the safe git profile ------------------------------------------------------------------------------

@contextlib.contextmanager
def _safe_git(root: pathlib.Path, layout: _Layout, git: str | None, facts: _Facts) -> Iterator[tuple[str, ...]]:
    """Pin one trusted git with its -c overrides and a scrubbed environment for every git call of this process;
    both are put back on exit."""
    saved_env, saved_pin = dict(os.environ), paths._git
    try:
        for name in list(os.environ):
            if name.upper().startswith("GIT_") and name.upper() not in KEPT_GIT_ENV:
                del os.environ[name]
        os.environ.update(SAFE_GIT_ENV)
        if root.parent != root:
            os.environ["GIT_CEILING_DIRECTORIES"] = str(root.parent)
        _frozen_child_fixes()
        program = _trusted_git(git, root, layout)
        facts.git = {"path": program, "version": _git_version(program)}
        base = _git_argv(program, SAFE_GIT_CONFIG)
        argv = _git_argv(program, SAFE_GIT_CONFIG + tuple(
            (f"filter.{driver}.{name}", value) for driver in _filter_drivers(base, root)
            for name, value in FILTER_SETTINGS))
        paths._git = Resolved(argv=argv, source=program, kind="native")
        if paths.git_argv() != argv:
            raise ProjectToolsError("OAH_PROJECT_TOOLS_INTERNAL", "the trusted git could not be pinned")
        yield argv
    finally:
        paths._git = saved_pin
        os.environ.clear()
        os.environ.update(saved_env)


def _git_argv(program: str, config: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    return (program, *(part for name, value in config for part in ("-c", f"{name}={value}")))


def _frozen_child_fixes() -> None:
    """A frozen runner must not hand its private library folder to the programs it starts."""
    if not getattr(sys, "frozen", False):
        return
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(None)
    elif sys.platform.startswith("linux"):
        original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
        os.environ.pop("LD_LIBRARY_PATH", None)
        if original is not None:
            os.environ["LD_LIBRARY_PATH"] = original


def _trusted_git(explicit: str | None, root: pathlib.Path, layout: _Layout) -> str:
    project = [root] + ([layout.common.parent] if layout.linked and layout.common.name == ".git" else [])

    def problem(candidate: str) -> str | None:
        if not _absolute(candidate):
            return "is not an absolute path"
        real = os.path.realpath(candidate)
        if is_batch(candidate) or is_batch(real) or (sys.platform == "win32" and not real.lower().endswith(".exe")):
            return "is not a native executable"
        if not os.path.isfile(real) or (sys.platform != "win32" and not os.access(real, os.X_OK)):
            return "is not an executable file"
        # Both the file itself and the path as written: a link inside the project could be pointed elsewhere later.
        if any(_inside(path, folder) for path in (real, os.path.abspath(candidate)) for folder in project):
            return "is inside the selected project"
        return None

    if explicit is not None:
        found = problem(explicit)
        if found:
            raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_UNTRUSTED", f"--git {explicit!r} {found}; pass the "
                                    "absolute path of an installed git")
        return explicit
    name = "git.exe" if sys.platform == "win32" else "git"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip('"')
        if entry and _absolute(entry) and problem(os.path.join(entry, name)) is None:
            return os.path.join(entry, name)
    raise ProjectToolsError(paths.GitMissing.code, "git was not found on an absolute PATH entry outside the "
                            "project; install git or pass --git")


def _git_version(program: str) -> str:
    try:
        result = run_command((program, "--version"), timeout=PROBE_TIMEOUT)
    except OSError as exc:
        raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_UNTRUSTED",
                                f"{program} could not be started ({exc.strerror or exc})") from None
    match = re.match(r"git version ((\d+)\.(\d+)\S*)", result.stdout) if result.returncode == 0 else None
    if match is None:
        raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_UNSUPPORTED", f"{program} did not answer --version like git")
    if (int(match.group(2)), int(match.group(3))) < MIN_GIT:
        raise ProjectToolsError("OAH_PROJECT_TOOLS_GIT_UNSUPPORTED", f"{program} is git {match.group(1)}; "
                                "git 2.31 or newer is required")
    return match.group(1)


def _filter_drivers(argv: tuple[str, ...], root: pathlib.Path) -> list[str]:
    """Every filter driver any configuration file defines here. Reading configuration runs nothing."""
    result = run_command((*argv, "-C", str(root), "config", "-z", "--name-only", "--get-regexp",
                          r"^filter\..*\.(clean|smudge|process|required)$"), timeout=PROBE_TIMEOUT)
    if result.timed_out or result.returncode not in (0, 1):
        raise ProjectToolsError("OAH_PROJECT_TOOLS_UNSAFE_CONFIG", "git could not read this repository's "
                                f"configuration: {_first_line(result.stderr) or 'no answer in time'}")
    drivers: list[str] = []
    for entry in result.stdout.split("\0"):
        driver = entry.removeprefix("filter.").rpartition(".")[0]
        if not entry.startswith("filter.") or not driver or driver in drivers:
            continue
        if "=" in driver or len(driver) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in driver):
            raise ProjectToolsError("OAH_PROJECT_TOOLS_UNSAFE_CONFIG", f"the filter driver {driver[:80]!r} in "
                                    "this repository's git configuration cannot be switched off safely; "
                                    "remove or rename it")
        drivers.append(driver)
    if len(drivers) > FILTER_LIMIT:
        raise ProjectToolsError("OAH_PROJECT_TOOLS_UNSAFE_CONFIG", f"this repository's git configuration "
                                f"defines {len(drivers)} filter drivers; at most {FILTER_LIMIT} are handled")
    return drivers


# --- the store and the repository's settings -----------------------------------------------------------

def _store_env() -> dict[str, str]:
    """The explicit store choice handed to the map store: an absolute OAH_STATE_DIR, or nothing."""
    override = os.environ.get(paths.STATE_ENV)
    if not override:
        return {}
    if not _absolute(override):
        raise ProjectToolsError("OAH_PROJECT_TOOLS_UNSAFE_STORE", f"{paths.STATE_ENV}={override!r} is relative, "
                                "so the store would depend on the current folder; set an absolute path or unset it")
    return {paths.STATE_ENV: os.path.realpath(override)}


def _verify_store(state: pathlib.Path, map_file: pathlib.Path, *, default: bool) -> None:
    """Nothing the store reads or writes may be reached through a symlink or junction."""
    chain = [(state, True)] if default else []
    chain += [(state / "impact", True), (state / "impact" / "maps", True), (map_file, False),
              (state / "impact" / ".lock", False)]
    for path, folder in chain:
        try:
            found = os.lstat(path)
        except FileNotFoundError:
            continue
        if _is_link(found) or not (stat.S_ISDIR if folder else stat.S_ISREG)(found.st_mode):
            raise ProjectToolsError("OAH_PROJECT_TOOLS_UNSAFE_STORE", f"{path} is not a plain "
                                    f"{'folder' if folder else 'file'} (a symlink, junction or other kind); "
                                    "remove it and retry")


def _bounded_settings(root: pathlib.Path) -> dict:
    """The repository's [impact] settings, read only from a plain file and kept inside this run's limits."""
    parts = CONFIG_RELATIVE_PATH.parts
    for count in range(1, len(parts) + 1):
        path, last = root.joinpath(*parts[:count]), count == len(parts)
        try:
            found = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            break
        # A pipe or device in place of the file would never finish reading.
        if _is_link(found) or last and (not stat.S_ISREG(found.st_mode) or found.st_size > CONFIG_LIMIT):
            raise ProjectToolsError("OAH_PROJECT_TOOLS_UNSAFE_CONFIG", f"{path} is not a plain file or folder (a "
                                    f"symlink, junction or other kind), or is larger than {CONFIG_LIMIT} bytes; "
                                    "replace it with a plain file")
        if not stat.S_ISDIR(found.st_mode):
            break
    settings = configured(root)
    settings["git_timeout"] = min(float(settings["git_timeout"]), MAX_GIT_TIMEOUT)
    settings["lock_timeout"] = min(float(settings["lock_timeout"]), MAX_LOCK_TIMEOUT)
    for pattern in settings["ledger_globs"]:
        problem = _unsafe_relative(pattern)
        if problem:
            raise ProjectToolsError("OAH_PROJECT_TOOLS_UNSAFE_CONFIG", f"[impact] ledger_globs entry {pattern!r} "
                                    f"{problem}; keep ledger patterns inside the project")
    return settings


# --- small helpers -------------------------------------------------------------------------------------

def _is_link(found: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point (junctions included)."""
    return stat.S_ISLNK(found.st_mode) or bool(
        getattr(found, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _absolute(text: str) -> bool:
    if os.name == "nt":
        drive, tail = os.path.splitdrive(text)
        return bool(drive) and tail.startswith(("\\", "/"))
    return text.startswith("/")


def _same(first: os.PathLike | str, second: os.PathLike | str) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _inside(path: str, folder: pathlib.Path) -> bool:
    """Whether a parent folder of this path, as written, is the folder (compared by identity, not by name)."""
    return any(_same(parent, folder) for parent in pathlib.Path(path).parents)


def _unsafe_relative(text: str) -> str | None:
    """Why a repository-supplied path or pattern may not be used, or None when it stays inside the project."""
    if not text or "\0" in text:
        return "is empty or holds a NUL"
    if "\\" in text or text.startswith("/") or re.match(r"[A-Za-z]:", text):
        return "is absolute or uses a backslash"
    if ".." in pathlib.PurePosixPath(text).parts:
        return "leaves the project"
    return None


def _linked_component(root: pathlib.Path, parts: tuple[str, ...]) -> pathlib.Path | None:
    """The first existing component under root that is a symlink or junction."""
    path = root
    for part in parts:
        path = path / part
        try:
            if _is_link(os.lstat(path)):
                return path
        except OSError:
            return None
    return None


def _first_line(text: str) -> str:
    return _clip(next((line.strip() for line in text.splitlines() if line.strip()), ""), 300)


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"
