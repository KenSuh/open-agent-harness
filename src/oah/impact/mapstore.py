# 영향 지도 저장 — 커밋 시점의 어휘 참조·DB 그래프·공동 변경 이력을 상태 폴더에 신원(커밋·도구·결과 digest)과 함께 저장하고, 낡으면 증분 갱신하거나 거부한다.
"""The persisted impact map.

One JSON file per worktree under `<state>/impact/maps/`. Its identity is `{commit, tool, result}`:
- `commit` is the HEAD the map describes;
- `tool` digests the oah version, the map schema, this package's source and the settings in MAP_KEYS;
- `result` digests the canonical JSON of the map's data.
A map is fresh only when all three match. A stale map is refreshed (incrementally when only the commit
moved) or refused with OAH_IMPACT_STALE; it is never answered from silently.

Incremental refresh keeps hits from unchanged files, re-reads changed files for the needles both maps
share, and searches the whole tree for new needles only. The result equals a full build byte for byte.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Iterable, Mapping

import oah
from oah.atomic import write_text_atomic
from oah.engines.proc import run_command
from oah.impact import kinds as kinds_module
from oah.impact import lexical, sqlplugin
from oah.impact.defaults import MAP_KEYS, configured
from oah.impact.errors import EXIT_USAGE, ImpactError
from oah.lockfile import StateLock
from oah.paths import git_argv, repo_info, state_dir

MAP_SCHEMA = 1
STRONG_KINDS = ("workflow", "manifest", "config")
PATHS_PER_CALL = 8000  # characters of pathspec per git call, well under the Windows command-line limit


@dataclass(frozen=True)
class MapInfo:
    commit: str
    tool_digest: str
    result_digest: str
    path: pathlib.Path
    action: str  # fresh, stale, built, refreshed or ephemeral
    changed: int = 0
    reason: str = ""

    def to_json(self) -> dict:
        return {"commit": self.commit, "tool": self.tool_digest, "result": self.result_digest,
                "action": self.action, "changed": self.changed, "reason": self.reason}


def canonical(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(data: object) -> str:
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def tool_digest(settings: Mapping[str, object]) -> str:
    source = hashlib.sha256()
    for path in sorted(pathlib.Path(__file__).parent.glob("*.py")):
        source.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest({"version": oah.__version__, "schema": MAP_SCHEMA, "source": source.hexdigest(),
                   "settings": {key: settings[key] for key in MAP_KEYS}})


# --- git -----------------------------------------------------------------------------------------------

def _git(root: pathlib.Path, *args: str, timeout: float, encoding: str = "utf-8",
         stdin_text: str | None = None, ok: Iterable[int] = (0,)) -> str:
    result = run_command((*git_argv(), "--literal-pathspecs", "-c", "core.quotepath=false", "-C", str(root), *args),
                         timeout=timeout, encoding=encoding, stdin_text=stdin_text)
    if result.timed_out:
        raise ImpactError("OAH_IMPACT_GIT_TIMEOUT", f"git {args[0]} took longer than {timeout:g}s in {root}; "
                          "raise [impact] git_timeout and retry")
    if result.returncode not in ok:
        raise ImpactError("OAH_IMPACT_GIT_FAILED", f"git {args[0]} failed in {root}: {result.stderr.strip()}")
    return result.stdout


def resolve_rev(root: pathlib.Path, rev: str, timeout: float = 30.0) -> str:
    sha = _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}",
               timeout=timeout, ok=(0, 1)).strip()
    if not sha:
        raise ImpactError("OAH_IMPACT_BAD_REF", f"{rev!r} is not a commit in this repository; "
                          "check the name with git log", EXIT_USAGE)
    return sha


def list_files(root: pathlib.Path, rev: str, settings: Mapping[str, object]) -> dict[str, str]:
    """Tracked blobs at rev with their kinds (submodules are skipped)."""
    output = _git(root, "ls-tree", "-r", "-z", "--full-tree", rev, timeout=settings["git_timeout"])
    files = {}
    for entry in output.split("\0"):
        meta, _, path = entry.partition("\t")
        if path and meta.split(" ")[1:2] == ["blob"]:
            files[path] = kinds_module.classify(path, settings["kinds"])
    return files


def read_blobs(root: pathlib.Path, rev: str, paths: list[str], timeout: float) -> dict[str, str]:
    """File contents at rev, through one `git cat-file --batch`."""
    paths = [path for path in paths if "\n" not in path]
    if not paths:
        return {}
    request = "".join(f"{rev}:{path}\n" for path in paths).encode("utf-8").decode("latin-1")
    raw = _git(root, "cat-file", "--batch", timeout=timeout, encoding="latin-1", stdin_text=request)
    raw_bytes, contents, pos = raw.encode("latin-1"), {}, 0
    for path in paths:
        header_end = raw_bytes.index(b"\n", pos)
        fields = raw_bytes[pos:header_end].split(b" ")
        pos = header_end + 1
        if len(fields) == 3 and fields[1] == b"blob":
            size = int(fields[2])
            contents[path] = raw_bytes[pos:pos + size].decode("utf-8", errors="replace")
            pos += size + 1
    return contents


def read_history(root: pathlib.Path, rev: str, limit: int, timeout: float) -> list[list]:
    """[[sha, [files]]], newest first, for the last `limit` commits reachable from rev."""
    output = _git(root, "log", "--no-renames", "--name-only", "-z", "--format=%x1e%H", f"-n{int(limit)}", rev,
                  "--", timeout=timeout)
    history = []
    for chunk in output.split("\x1e")[1:]:
        sha, _, rest = chunk.partition("\0")
        names = sorted({name for name in rest.removeprefix("\n").split("\0") if name})
        if names:
            history.append([sha.strip(), names])
    return history


def diff_names(root: pathlib.Path, old: str, new: str, timeout: float) -> list[str] | None:
    """Paths that differ between two commits (renames as delete plus add), or None when old is gone."""
    if not _commit_exists(root, old, timeout):
        return None
    output = _git(root, "diff", "--no-renames", "--name-only", "-z", old, new, "--", timeout=timeout)
    return sorted({name for name in output.split("\0") if name})


def _commit_exists(root: pathlib.Path, rev: str, timeout: float) -> bool:
    result = run_command((*git_argv(), "-C", str(root), "cat-file", "-e", rev + "^{commit}"), timeout=timeout)
    return result.returncode == 0


# --- scanning ------------------------------------------------------------------------------------------

def _chunks(paths: list[str]) -> Iterable[list[str]]:
    chunk, size = [], 0
    for path in paths:
        if chunk and size + len(path) > PATHS_PER_CALL:
            yield chunk
            chunk, size = [], 0
        chunk.append(path)
        size += len(path) + 1
    if chunk:
        yield chunk


def needle_set(files: Iterable[str], min_needle: int) -> set[str]:
    needles: set[str] = set()
    for path in files:
        needles.update(lexical.needles_for(path, min_needle))
    return needles


def _flag(kind: str, text: str, needle: str) -> str:
    if kind in STRONG_KINDS:
        strong = lexical.classify_command(text, needle)
        if strong:
            return strong
    if re.search(r"\[\[" + re.escape(needle) + r"(?:\]\]|\||#)", text):
        return "wikilink"
    return ""


def _grep_lines(root: pathlib.Path, rev: str, needles: set[str], paths: list[str] | None, timeout: float,
                ignore_case: bool = False) -> list[tuple[str, int, str]]:
    if not needles:
        return []
    if paths is None:
        return lexical.grep(root, rev, sorted(needles), ignore_case=ignore_case, timeout=timeout)
    lines = []
    for chunk in _chunks(paths):
        lines.extend(lexical.grep(root, rev, sorted(needles), paths=chunk, ignore_case=ignore_case, timeout=timeout))
    return lines


def scan_lexical(root: pathlib.Path, rev: str, needles: set[str], files: Mapping[str, str],
                 paths: list[str] | None, timeout: float) -> dict[str, list[list]]:
    """{needle: [[source, line, flag]]}; flag is pin, exec, wikilink or empty."""
    hits: dict[str, list[list]] = {}
    for source, number, text in _grep_lines(root, rev, needles, paths, timeout):
        kind = files.get(source, "code")
        for needle in lexical.find_needles(text, needles):
            hits.setdefault(needle, []).append([source, number, _flag(kind, text, needle)])
    return hits


def scan_db(root: pathlib.Path, rev: str, names: set[str], paths: list[str] | None,
            timeout: float) -> dict[str, list[list]]:
    """{name: [[source, line]]} for DB entity names used as identifiers anywhere, ignoring case."""
    hits: dict[str, list[list]] = {}
    for source, number, text in _grep_lines(root, rev, names, paths, timeout, ignore_case=True):
        for name in lexical.find_identifiers(text, names):
            hits.setdefault(name, []).append([source, number])
    return hits


def db_files(files: Iterable[str], settings: Mapping[str, object]) -> list[str]:
    return sorted(path for path in files if any(kinds_module.glob_match(g, path) for g in settings["db_globs"]))


def db_names(graph: Mapping | None, min_needle: int) -> set[str]:
    if not graph:
        return set()
    return {info["name"] for entity, info in graph["entities"].items()
            if entity.startswith(("db_table:", "db_routine:")) and len(info["name"]) >= min_needle}


def _db_graph(root: pathlib.Path, rev: str, files: Mapping[str, str], settings: Mapping[str, object]) -> dict | None:
    sql = db_files(files, settings)
    return sqlplugin.extract(read_blobs(root, rev, sql, settings["git_timeout"])) if sql else None


def _carry(old: Mapping[str, list], keep: set[str], changed: set[str]) -> dict[str, list[list]]:
    carried = {}
    for needle in keep:
        rows = [row for row in old.get(needle, []) if row[0] not in changed]
        if rows:
            carried[needle] = rows
    return carried


def _merge(into: dict[str, list[list]], extra: Mapping[str, list[list]]) -> None:
    for needle, rows in extra.items():
        into.setdefault(needle, []).extend(rows)


def _finish(data: dict) -> dict:
    for section in ("hits", "db_hits"):
        data[section] = {key: sorted({tuple(row) for row in rows}) for key, rows in sorted(data[section].items())}
        data[section] = {key: [list(row) for row in rows] for key, rows in data[section].items()}
    return data


def build_data(root: pathlib.Path, rev: str, settings: Mapping[str, object]) -> dict:
    timeout, minimum = settings["git_timeout"], settings["min_needle"]
    files = list_files(root, rev, settings)
    graph = _db_graph(root, rev, files, settings)
    return _finish({
        "files": files,
        "hits": scan_lexical(root, rev, needle_set(files, minimum), files, None, timeout),
        "db": graph,
        "db_hits": scan_db(root, rev, db_names(graph, minimum), None, timeout),
        "history": read_history(root, rev, settings["history_limit"], timeout),
    })


def refresh_data(root: pathlib.Path, old: Mapping, old_rev: str, rev: str,
                 settings: Mapping[str, object]) -> tuple[dict, int] | None:
    """The map at rev from the map at old_rev, or None when old_rev is no longer available."""
    timeout, minimum = settings["git_timeout"], settings["min_needle"]
    names = diff_names(root, old_rev, rev, timeout)
    if names is None:
        return None
    changed = set(names)
    files = list_files(root, rev, settings)
    existing = [path for path in names if path in files]
    before, after = needle_set(old["files"], minimum), needle_set(files, minimum)
    hits = _carry(old["hits"], before & after, changed)
    _merge(hits, scan_lexical(root, rev, before & after, files, existing, timeout))
    _merge(hits, scan_lexical(root, rev, after - before, files, None, timeout))
    graph = old["db"]
    if any(path in changed for path in db_files(set(old["files"]) | set(files), settings)):
        graph = _db_graph(root, rev, files, settings)
    old_names, new_names = db_names(old["db"], minimum), db_names(graph, minimum)
    db_hits = _carry(old["db_hits"], old_names & new_names, changed)
    _merge(db_hits, scan_db(root, rev, old_names & new_names, existing, timeout))
    _merge(db_hits, scan_db(root, rev, new_names - old_names, None, timeout))
    data = _finish({"files": files, "hits": hits, "db": graph, "db_hits": db_hits,
                    "history": read_history(root, rev, settings["history_limit"], timeout)})
    return data, len(changed)


# --- persistence ---------------------------------------------------------------------------------------

def map_path(state: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    key = hashlib.sha256(str(pathlib.Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return state / "impact" / "maps" / f"{key}.json"


def load(path: pathlib.Path) -> dict:
    """The stored map; FileNotFoundError when missing, ValueError when unreadable."""
    try:
        stored = json.loads(pathlib.Path(path).read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(stored, dict) or not isinstance(stored.get("data"), dict):
        raise ValueError("not an impact map")
    return stored


def _context(root: pathlib.Path, env: Mapping[str, str] | None) -> tuple[pathlib.Path, str, pathlib.Path]:
    info = repo_info(pathlib.Path(root))
    if info.head is None:
        raise ImpactError("OAH_IMPACT_NO_COMMIT", "this repository has no commits yet; make a first commit, "
                          "then run oah impact build")
    state = state_dir(info, os.environ if env is None else env)
    return info.root, info.head, map_path(state, info.root)


def _judge(path: pathlib.Path, head: str, tool: str) -> tuple[dict | None, str]:
    try:
        stored = load(path)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unreadable"
    if stored.get("schema") != MAP_SCHEMA:
        return None, "older map format"
    if stored.get("tool") != tool:
        return stored, "tool changed"
    if stored.get("result") != digest(stored["data"]):
        return stored, "result digest mismatch"
    if stored.get("commit") != head:
        return stored, f"behind HEAD (map {str(stored.get('commit'))[:12]}, HEAD {head[:12]})"
    return stored, ""


def _info(stored: Mapping, path: pathlib.Path, action: str, changed: int = 0, reason: str = "") -> MapInfo:
    return MapInfo(stored["commit"], stored["tool"], stored["result"], path, action, changed, reason)


def status(root: pathlib.Path, *, env: Mapping[str, str] | None = None,
           settings: Mapping[str, object] | None = None) -> tuple[MapInfo | None, str]:
    """Read only: (info with action fresh or stale, reason). Creates nothing."""
    top, head, path = _context(root, env)
    settings = configured(top, settings)
    stored, reason = _judge(path, head, tool_digest(settings))
    if stored is None:
        return None, reason
    return _info(stored, path, "stale" if reason else "fresh", reason=reason), reason or "fresh"


def _write(path: pathlib.Path, commit: str, tool: str, data: dict) -> dict:
    stored = {"schema": MAP_SCHEMA, "commit": commit, "tool": tool, "result": digest(data),
              "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "data": data}
    write_text_atomic(path, canonical(stored) + "\n")
    return stored


def _update(root: pathlib.Path, *, env: Mapping[str, str] | None, settings: Mapping[str, object] | None,
            force: bool) -> tuple[MapInfo, dict]:
    top, head, path = _context(root, env)
    settings = configured(top, settings)
    tool = tool_digest(settings)
    with StateLock(path.parent.parent / ".lock", timeout=float(settings["lock_timeout"])):
        stored, reason = _judge(path, head, tool)
        if not force and stored is not None and not reason:
            return _info(stored, path, "fresh"), stored["data"]
        if not force and stored is not None and reason.startswith("behind HEAD"):
            refreshed = refresh_data(top, stored["data"], stored["commit"], head, settings)
            if refreshed is not None:
                data, changed = refreshed
                return _info(_write(path, head, tool, data), path, "refreshed", changed, reason), data
        data = build_data(top, head, settings)
        return _info(_write(path, head, tool, data), path, "built", len(data["files"]), reason or "forced"), data


def build(root: pathlib.Path, *, env: Mapping[str, str] | None = None,
          settings: Mapping[str, object] | None = None) -> MapInfo:
    """A full build at HEAD, replacing any stored map."""
    return _update(root, env=env, settings=settings, force=True)[0]


def fresh_map(root: pathlib.Path, *, refresh: bool = True, env: Mapping[str, str] | None = None,
              settings: Mapping[str, object] | None = None) -> tuple[MapInfo, dict]:
    """(info, data) for a map that matches HEAD. Stale maps are refreshed, or refused when refresh is False."""
    top, head, path = _context(root, env)
    settings = configured(top, settings)
    stored, reason = _judge(path, head, tool_digest(settings))
    if stored is not None and not reason:
        return _info(stored, path, "fresh"), stored["data"]
    if not refresh:
        hint = "run oah impact build" if stored is None else "run oah impact refresh"
        raise ImpactError("OAH_IMPACT_STALE", f"the impact map is {reason}; {hint} (or drop --no-refresh) "
                          "and retry. oah never answers from a stale map.")
    return _update(root, env=env, settings=settings, force=False)


def ensure_fresh(root: pathlib.Path, *, refresh: bool = True, env: Mapping[str, str] | None = None,
                 settings: Mapping[str, object] | None = None) -> MapInfo:
    """Make sure the map matches HEAD: the call preflight, packet new, review and finish make first."""
    return fresh_map(root, refresh=refresh, env=env, settings=settings)[0]


def snapshot(root: pathlib.Path, rev: str, *, settings: Mapping[str, object] | None = None) -> tuple[MapInfo, dict]:
    """An in-memory map at any commit (for --ref); nothing is stored."""
    top = repo_info(pathlib.Path(root)).root
    settings = configured(top, settings)
    sha = resolve_rev(top, rev)
    data = build_data(top, sha, settings)
    return MapInfo(sha, tool_digest(settings), digest(data), pathlib.Path(), "ephemeral", len(data["files"])), data


def run_hook(name: str) -> int:
    """Refresh the map after a commit or merge; one line out, never a traceback."""
    from oah.atomic import StateCorrupt, StorageError
    from oah.config import ConfigError
    from oah.lockfile import LockTimeout
    from oah.paths import GitMissing, NotARepo

    try:
        info = ensure_fresh(pathlib.Path.cwd())
    except (ImpactError, ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, StorageError,
            OSError) as exc:
        print(f"{exc}\noah {name}: the impact map was not refreshed; your git operation itself is fine. "
              "Run oah impact refresh to retry.", file=sys.stderr)
        return 69
    print(f"oah {name}: map {info.action} at {info.commit[:12]} ({info.changed} changed files)")
    return 0
