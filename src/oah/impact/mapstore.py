# 영향 지도 저장 — 작업 트리의 어휘 참조·DB 그래프·공동 변경 이력을 입력 매니페스트(HEAD·인덱스·더러운/추적 안 된 파일·원장·설정·도구)와 결과 digest 로 묶어 저장하고, 낡으면 증분 갱신하거나 거부한다.
"""The persisted impact map.

One JSON file per worktree under `<state>/impact/maps/`. The map describes the working tree: tracked and
untracked (not ignored) files, with dirty edits as they are on disk. Its freshness identity is a manifest
of every input it binds:
- `worktree` (which checkout) and `head` (the commit);
- `index`, the digest of `git ls-files -s`, so staged changes count;
- `dirty` and `untracked`, the byte digest of each such file (`deleted` for a removed tracked file);
- `ledgers`, the bytes of every gate ledger, ignored ones included;
- `config`, the bytes of `.oah/config.toml`;
- `tool`, which digests the oah version, the map schema, this package's source and the settings in MAP_KEYS.
The map also keeps `result`, the digest of its data. Any manifest or result difference makes it stale; a
stale map is refreshed or refused with OAH_IMPACT_STALE, never answered from silently. The manifest is
taken before and after every build: when the inputs moved meanwhile the build is retried, and refused as
stale when they keep moving.

Incremental refresh compares per-path input keys (index blob for clean files, byte digest otherwise). It
keeps hits from unchanged files, re-reads changed files for the needles both maps share, and searches the
whole tree for new needles only. The result equals a full build byte for byte.
"""
from __future__ import annotations

import datetime
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable, Mapping

import oah
from oah.atomic import write_text_atomic
from oah.config import CONFIG_RELATIVE_PATH
from oah.engines.proc import run_command
from oah.impact import kinds as kinds_module
from oah.impact import lexical, sqlplugin
from oah.impact.defaults import MAP_KEYS, configured
from oah.impact.errors import EXIT_USAGE, ImpactError
from oah.lockfile import StateLock
from oah.paths import git_argv, repo_info, state_dir

MAP_SCHEMA = 2
STRONG_KINDS = ("workflow", "manifest", "config")
PATHS_PER_CALL = 8000  # characters of pathspec per git call, well under the Windows command-line limit
BUILD_ATTEMPTS = 3
# Most specific first: an untracked config file is also a working-tree input, but "config changed" says more.
MANIFEST_ORDER = (("worktree", "worktree changed"), ("head", "HEAD moved"), ("index", "index changed"),
                  ("ledgers", "ledgers changed"), ("config", "config changed"), ("dirty", "working tree changed"),
                  ("untracked", "working tree changed"), ("excluded_paths", "excluded inputs changed"),
                  ("tool", "tool changed"))


@dataclass(frozen=True)
class MapInfo:
    commit: str
    tool_digest: str
    result_digest: str
    path: pathlib.Path
    action: str  # fresh, stale, built, refreshed or ephemeral
    changed: int = 0
    reason: str = ""
    manifest_digest: str = ""
    # Not a constructor parameter: keep the public MapInfo signature compatible.
    excluded_paths: tuple[str, ...] = field(default=(), init=False)
    sql_unparsed: tuple[dict, ...] = field(default=(), init=False)

    def warnings(self) -> list[str]:
        return coverage_warnings(self.excluded_paths) + [
            f"OAH_IMPACT_SQL_UNPARSED: {region['file']}:{region['line']}: {region['reason']}; "
            "review this SQL region manually for dependencies before selecting checks"
            for region in self.sql_unparsed]

    def to_json(self) -> dict:
        return {"commit": self.commit, "manifest": self.manifest_digest, "tool": self.tool_digest,
                "result": self.result_digest, "action": self.action, "changed": self.changed, "reason": self.reason,
                "excluded_paths": list(self.excluded_paths), "sql_unparsed": list(self.sql_unparsed),
                "warnings": self.warnings()}


def coverage_warnings(paths: Iterable[str]) -> list[str]:
    excluded = sorted(set(paths))
    return (["OAH_IMPACT_INCOMPLETE: incomplete coverage; excluded symlink inputs: " +
             ", ".join(excluded) + "; replace these symlinks with regular repository files and run oah impact refresh"]
            if excluded else [])


def _coverage(info: MapInfo, paths: Iterable[str], db: Mapping | None = None) -> MapInfo:
    object.__setattr__(info, "excluded_paths", tuple(sorted(set(paths))))
    object.__setattr__(info, "sql_unparsed", tuple((db or {}).get("unparsed", [])))
    return info


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
    argv = (*git_argv(), "--literal-pathspecs", "--no-optional-locks", "-c", "core.quotepath=false",
            "-C", str(root), *args)
    result = run_command(argv, timeout=timeout, encoding=encoding, stdin_text=stdin_text)
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


def _head(root: pathlib.Path, timeout: float) -> str:
    sha = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", timeout=timeout, ok=(0, 1)).strip()
    if not sha:
        raise ImpactError("OAH_IMPACT_NO_COMMIT", "this repository has no commits yet; make a first commit, "
                          "then run oah impact build")
    return sha


def list_files(root: pathlib.Path, rev: str, settings: Mapping[str, object]) -> dict[str, str]:
    """Tracked blobs at rev with their kinds (submodules are skipped)."""
    return _tree_files(root, rev, settings)[0]


def _tree_files(root: pathlib.Path, rev: str, settings: Mapping[str, object]) -> tuple[dict[str, str], list[str]]:
    output = _git(root, "ls-tree", "-r", "-z", "--full-tree", rev, timeout=settings["git_timeout"])
    files, excluded = {}, []
    for entry in output.split("\0"):
        meta, _, path = entry.partition("\t")
        if path and meta.split(" ")[1:2] == ["blob"]:
            if meta.startswith("120000 "):
                excluded.append(path)
            else:
                files[path] = kinds_module.classify(path, settings["kinds"])
    return files, sorted(excluded)


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


def _safe_worktree_path(root: pathlib.Path, path: str) -> bool:
    """Do not follow file or directory symlinks, or leave the repository."""
    base = root.resolve()
    candidate = base / path
    try:
        parts = candidate.relative_to(base).parts
        if ".." in parts or not candidate.resolve().is_relative_to(base):
            return False
        for part in parts:
            base /= part
            if base.is_symlink():
                return False
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def read_worktree(root: pathlib.Path, paths: list[str]) -> dict[str, str]:
    contents = {}
    for path in paths:
        if not _safe_worktree_path(root, path):
            continue
        try:
            contents[path] = (root / path).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue  # removed since the manifest was taken; the after-build manifest catches it
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


# --- the input manifest --------------------------------------------------------------------------------

def _file_key(path: pathlib.Path) -> str | None:
    try:
        if path.is_symlink():
            return "sha256:" + hashlib.sha256(os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _status(root: pathlib.Path, timeout: float) -> tuple[set[str], set[str]]:
    """(tracked paths whose working copy differs from the index, untracked paths), without taking index locks."""
    output = _git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--no-renames",
                  "--ignore-submodules=all", timeout=timeout)
    dirty, untracked = set(), set()
    entries = iter(output.split("\0"))
    for entry in entries:
        if entry.startswith("1 "):
            fields = entry.split(" ", 8)
            if fields[1][1] != ".":
                dirty.add(fields[8])
        elif entry.startswith("2 "):
            dirty.add(entry.split(" ", 9)[9])
            next(entries, None)  # the original path of a rename
        elif entry.startswith("u "):
            dirty.add(entry.split(" ", 10)[10])
        elif entry.startswith("? ") and not entry.endswith("/"):
            untracked.add(entry[2:])
    return dirty, untracked


def _ledgers(root: pathlib.Path, settings: Mapping[str, object]) -> dict[str, str]:
    found = {}
    for pattern in settings["ledger_globs"]:
        # pathlib's literal final-component selector omits dangling symlinks. Force a directory
        # listing for that component, then apply the original pattern to its entries.
        terminal = pathlib.PurePosixPath(pattern).name
        listing = pattern + "*" if not any(char in terminal for char in "*?[") else pattern
        for path in root.glob(listing):
            relative = path.relative_to(root).as_posix()
            # Parent components have already been matched by pathlib. Recheck only the
            # expanded terminal component, with pathlib's character classes and OS casing.
            if not fnmatch.fnmatch(path.name, terminal):
                continue
            key = ("excluded-symlink" if not _safe_worktree_path(root, relative) else
                   _file_key(path)) if path.is_symlink() or path.is_file() else None
            if key:
                found[relative] = key
    return dict(sorted(found.items()))


def _outside(root: pathlib.Path, folder: pathlib.Path | None):
    """A predicate that drops paths under folder (the state folder, when it lives inside the worktree)."""
    try:
        prefix = pathlib.Path(folder).resolve().relative_to(root.resolve()).as_posix() if folder else None
    except ValueError:
        prefix = None
    if not prefix or prefix == ".":
        return lambda path: True
    return lambda path: path != prefix and not path.startswith(prefix + "/")


def take_manifest(root: pathlib.Path, settings: Mapping[str, object],
                  state: pathlib.Path | None = None) -> tuple[dict, dict[str, str]]:
    """(manifest, inputs): the identity of everything the map reads, and the input key of every file it reads.

    Paths under the state folder are never inputs, so storing the map cannot make it stale.
    """
    timeout, keep = settings["git_timeout"], _outside(root, state)
    head = _head(root, timeout)
    listing = _git(root, "ls-files", "-s", "-z", timeout=timeout)
    inputs = {}
    for entry in listing.split("\0"):
        meta, _, path = entry.partition("\t")
        fields = meta.split(" ")
        if path and len(fields) == 3 and fields[0] != "160000" and keep(path):
            inputs[path] = "blob:" + fields[1]
    dirty_paths, untracked_paths = _status(root, timeout)
    dirty, untracked = {}, {}
    for path in sorted(filter(keep, dirty_paths)):
        key = _file_key(root / path) if _safe_worktree_path(root, path) else "excluded-symlink"
        dirty[path] = key or "deleted"
        if key:
            inputs[path] = key
        else:
            inputs.pop(path, None)
    for path in sorted(filter(keep, untracked_paths)):
        key = _file_key(root / path) if _safe_worktree_path(root, path) else "excluded-symlink"
        if key:
            untracked[path] = inputs[path] = key
    config = _file_key(root.joinpath(*CONFIG_RELATIVE_PATH.parts))
    ledgers = _ledgers(root, settings)
    excluded = sorted(path for path in set(inputs) | set(ledgers) if not _safe_worktree_path(root, path))
    manifest = {"worktree": hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest(), "head": head,
                "index": hashlib.sha256(listing.encode("utf-8", "surrogateescape")).hexdigest(),
                "dirty": dirty, "untracked": untracked, "ledgers": ledgers, "excluded_paths": excluded,
                "config": config, "tool": tool_digest(settings)}
    return manifest, {path: key for path, key in sorted(inputs.items()) if _safe_worktree_path(root, path)}


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


def _grep_lines(root: pathlib.Path, rev: str | None, needles: set[str], paths: list[str] | None, timeout: float,
                ignore_case: bool = False) -> list[tuple[str, int, str]]:
    if not needles:
        return []
    if paths is None:
        return lexical.grep(root, rev, sorted(needles), ignore_case=ignore_case, timeout=timeout)
    lines = []
    for chunk in _chunks(paths):
        lines.extend(lexical.grep(root, rev, sorted(needles), paths=chunk, ignore_case=ignore_case, timeout=timeout))
    return lines


def scan_lexical(root: pathlib.Path, rev: str | None, needles: set[str], files: Mapping[str, str],
                 paths: list[str] | None, timeout: float) -> dict[str, list[list]]:
    """{needle: [[source, line, flag]]}; flag is pin, exec, wikilink or empty. rev None is the working tree."""
    hits: dict[str, list[list]] = {}
    for source, number, text in _grep_lines(root, rev, needles, paths, timeout):
        if source not in files:
            continue
        for needle in lexical.find_needles(text, needles):
            hits.setdefault(needle, []).append([source, number, _flag(files[source], text, needle)])
    return hits


def scan_db(root: pathlib.Path, rev: str | None, names: set[str], files: Mapping[str, str],
            paths: list[str] | None, timeout: float) -> dict[str, list[list]]:
    """{name: [[source, line]]} for DB entity names used as identifiers anywhere, ignoring case."""
    hits: dict[str, list[list]] = {}
    for source, number, text in _grep_lines(root, rev, names, paths, timeout, ignore_case=True):
        if source not in files:
            continue
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


def _db_graph(root: pathlib.Path, rev: str | None, files: Mapping[str, str],
              settings: Mapping[str, object]) -> dict | None:
    sql = db_files(files, settings)
    if not sql:
        return None
    contents = read_worktree(root, sql) if rev is None else read_blobs(root, rev, sql, settings["git_timeout"])
    return sqlplugin._extract(contents, settings["sql_dialect"]) if contents else None


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


def _classified(paths: Iterable[str], settings: Mapping[str, object]) -> dict[str, str]:
    return {path: kinds_module.classify(path, settings["kinds"]) for path in sorted(paths)}


def build_data(root: pathlib.Path, rev: str | None, settings: Mapping[str, object], *,
               inputs: Mapping[str, str] | None = None, head: str | None = None) -> dict:
    """The map's data: at commit rev, or (rev None) over the working tree's inputs with history from head."""
    timeout, minimum = settings["git_timeout"], settings["min_needle"]
    if rev is not None:
        files, excluded = _tree_files(root, rev, settings)
    else:
        files, excluded = _classified(inputs or {}, settings), []
    graph = _db_graph(root, rev, files, settings)
    return _finish({
        "files": files,
        "excluded_paths": excluded,
        "hits": scan_lexical(root, rev, needle_set(files, minimum), files, None, timeout),
        "db": graph,
        "db_hits": scan_db(root, rev, db_names(graph, minimum), files, None, timeout),
        "history": read_history(root, rev or head, settings["history_limit"], timeout),
    })


def refresh_data(root: pathlib.Path, old: Mapping, old_inputs: Mapping[str, str], inputs: Mapping[str, str],
                 head: str, settings: Mapping[str, object]) -> tuple[dict, int]:
    """The working-tree map for inputs, from the map built for old_inputs."""
    timeout, minimum = settings["git_timeout"], settings["min_needle"]
    changed = {path for path in set(old_inputs) | set(inputs) if old_inputs.get(path) != inputs.get(path)}
    files = _classified(inputs, settings)
    existing = sorted(path for path in changed if path in files)
    before, after = needle_set(old["files"], minimum), needle_set(files, minimum)
    hits = _carry(old["hits"], before & after, changed)
    _merge(hits, scan_lexical(root, None, before & after, files, existing, timeout))
    _merge(hits, scan_lexical(root, None, after - before, files, None, timeout))
    graph = old["db"]
    if any(path in changed for path in db_files(set(old["files"]) | set(files), settings)):
        graph = _db_graph(root, None, files, settings)
    old_names, new_names = db_names(old["db"], minimum), db_names(graph, minimum)
    db_hits = _carry(old["db_hits"], old_names & new_names, changed)
    _merge(db_hits, scan_db(root, None, old_names & new_names, files, existing, timeout))
    _merge(db_hits, scan_db(root, None, new_names - old_names, files, None, timeout))
    data = _finish({"files": files, "hits": hits, "db": graph, "db_hits": db_hits,
                    "history": read_history(root, head, settings["history_limit"], timeout)})
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


def _context(root: pathlib.Path, env: Mapping[str, str] | None,
             settings: Mapping[str, object] | None) -> tuple[pathlib.Path, pathlib.Path, dict]:
    """(worktree root, map file, settings). The map file sits at <state>/impact/maps/<key>.json."""
    info = repo_info(pathlib.Path(root))
    if info.head is None:
        raise ImpactError("OAH_IMPACT_NO_COMMIT", "this repository has no commits yet; make a first commit, "
                          "then run oah impact build")
    state = state_dir(info, os.environ if env is None else env)
    return info.root, map_path(state, info.root), configured(info.root, settings)


def _state(path: pathlib.Path) -> pathlib.Path:
    return path.parent.parent.parent


def _judge(path: pathlib.Path, manifest: Mapping) -> tuple[dict | None, str, bool]:
    """(stored map, reason it is stale or "", whether it can be refreshed incrementally)."""
    try:
        stored = load(path)
    except FileNotFoundError:
        return None, "missing", False
    except (OSError, ValueError):
        return None, "unreadable", False
    if stored.get("schema") != MAP_SCHEMA or not isinstance(stored.get("manifest"), dict) \
            or not isinstance(stored.get("inputs"), dict):
        return None, "older map format", False
    if stored.get("tool") != manifest["tool"] or stored["manifest"].get("tool") != manifest["tool"]:
        return stored, "tool changed", False
    if stored.get("result") != digest(stored["data"]) or stored.get("manifest_digest") != digest(stored["manifest"]):
        return stored, "result digest mismatch", False
    for key, reason in MANIFEST_ORDER:
        if stored["manifest"].get(key) != manifest[key]:
            if key == "head":
                reason = f"HEAD moved (map {str(stored['manifest'].get('head'))[:12]}, HEAD {manifest['head'][:12]})"
            return stored, reason, key != "worktree"
    return stored, "", True


def _info(stored: Mapping, path: pathlib.Path, action: str, changed: int = 0, reason: str = "") -> MapInfo:
    return _coverage(MapInfo(stored["manifest"]["head"], stored["tool"], stored["result"], path, action, changed, reason,
                             stored["manifest_digest"]), stored["data"].get("excluded_paths", []),
                     stored["data"].get("db"))


def status(root: pathlib.Path, *, env: Mapping[str, str] | None = None,
           settings: Mapping[str, object] | None = None) -> tuple[MapInfo | None, str]:
    """Read only: (info with action fresh or stale, reason). Creates nothing and takes no index lock."""
    top, path, settings = _context(root, env, settings)
    manifest, _inputs = take_manifest(top, settings, _state(path))
    stored, reason, _incremental = _judge(path, manifest)
    if stored is None:
        return None, reason
    return _info(stored, path, "stale" if reason else "fresh", reason=reason), reason or "fresh"


def _write(path: pathlib.Path, manifest: dict, inputs: Mapping[str, str], data: dict) -> dict:
    stored = {"schema": MAP_SCHEMA, "commit": manifest["head"], "tool": manifest["tool"], "manifest": manifest,
              "manifest_digest": digest(manifest), "inputs": dict(inputs), "result": digest(data),
              "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "data": data}
    write_text_atomic(path, canonical(stored) + "\n")
    return stored


def _update(top: pathlib.Path, path: pathlib.Path, settings: Mapping[str, object] | None, *,
            force: bool) -> tuple[MapInfo, dict]:
    initial_config = _file_key(top.joinpath(*CONFIG_RELATIVE_PATH.parts))
    with StateLock(path.parent.parent / ".lock", timeout=float(configured(top, settings)["lock_timeout"])):
        for _attempt in range(BUILD_ATTEMPTS):
            config = _file_key(top.joinpath(*CONFIG_RELATIVE_PATH.parts))
            if settings is not None and config != initial_config:
                raise ImpactError("OAH_IMPACT_STALE", "config changed while the impact map was built with "
                                  "explicit settings; reload settings and run oah impact refresh")
            current = configured(top, settings)
            manifest, inputs = take_manifest(top, current, _state(path))
            if manifest["config"] != config:
                continue  # config moved while settings were loaded; restart with its new bytes
            stored, reason, incremental = _judge(path, manifest)
            if not force and stored is not None and not reason:
                return _info(stored, path, "fresh"), stored["data"]
            if not force and stored is not None and incremental:
                data, changed = refresh_data(top, stored["data"], stored["inputs"], inputs, manifest["head"],
                                             current)
                action = "refreshed"
            else:
                data = build_data(top, None, current, inputs=inputs, head=manifest["head"])
                changed, action = len(data["files"]), "built"
            data["excluded_paths"] = manifest["excluded_paths"]
            if take_manifest(top, current, _state(path))[0] == manifest:
                return _info(_write(path, manifest, inputs, data), path, action, changed, reason or "forced"), data
    raise ImpactError("OAH_IMPACT_STALE", f"files kept changing while the impact map was built "
                      f"({BUILD_ATTEMPTS} attempts), so it was not stored; let your edits settle, then run "
                      "oah impact refresh.")


def build(root: pathlib.Path, *, env: Mapping[str, str] | None = None,
          settings: Mapping[str, object] | None = None) -> MapInfo:
    """A full build of the working tree, replacing any stored map."""
    top, path, _current = _context(root, env, settings)
    return _update(top, path, settings, force=True)[0]


def fresh_map(root: pathlib.Path, *, refresh: bool = True, env: Mapping[str, str] | None = None,
              settings: Mapping[str, object] | None = None) -> tuple[MapInfo, dict]:
    """(info, data) for a map that matches the current inputs. Stale maps are refreshed, or refused when
    refresh is False."""
    top, path, current = _context(root, env, settings)
    if refresh:
        return _update(top, path, settings, force=False)
    manifest, _inputs = take_manifest(top, current, _state(path))
    stored, reason, _incremental = _judge(path, manifest)
    if stored is not None and not reason:
        return _info(stored, path, "fresh"), stored["data"]
    hint = "run oah impact build" if stored is None else "run oah impact refresh"
    raise ImpactError("OAH_IMPACT_STALE", f"the impact map is {reason}; {hint} (or drop --no-refresh) "
                      "and retry. oah never answers from a stale map.")


def ensure_fresh(root: pathlib.Path, *, refresh: bool = True, env: Mapping[str, str] | None = None,
                 settings: Mapping[str, object] | None = None) -> MapInfo:
    """Make sure the map matches the working tree: the call preflight, packet new, review and finish make first."""
    return fresh_map(root, refresh=refresh, env=env, settings=settings)[0]


def snapshot(root: pathlib.Path, rev: str, *, settings: Mapping[str, object] | None = None) -> tuple[MapInfo, dict]:
    """An in-memory map at any commit (for --ref); nothing is stored and the working tree is not read."""
    top = repo_info(pathlib.Path(root)).root
    settings = configured(top, settings)
    sha = resolve_rev(top, rev)
    data = build_data(top, sha, settings)
    info = MapInfo(sha, tool_digest(settings), digest(data), pathlib.Path(), "ephemeral", len(data["files"]),
                   manifest_digest=digest({"head": sha, "tool": tool_digest(settings)}))
    return _coverage(info, data["excluded_paths"], data.get("db")), data


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
    for warning in info.warnings():
        print(warning, file=sys.stderr)
    return 0
