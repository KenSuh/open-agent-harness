# 리뷰 스냅샷 — 저장소를 --no-local 로 복제해 head 에 detached 로 두고 원격을 지운 뒤, 에이전트 지시 파일을 모든 깊이에서 대소문자 무시로 걷어 내며, 끝나면 실패해도 지운다.
"""The sanitized tree a reviewer works in (PLAN 5.9 step 3).

`make(source, head, base=...)` clones the source repository with `git clone --no-local --no-checkout
--template=` into a fresh temporary folder, checks `head` out detached, removes the `origin` remote and
deletes every tracked agent instruction path from the working tree: files named like `STRIPPED_FILES`
and everything under folders named like `STRIPPED_DIRS`, at any depth and in any letter case (a
case-insensitive file system loads `agents.md` as `AGENTS.md`). The index keeps them, so
`git show <head>:<path>` still reads them. Hooks never run: the clone copies no template and every git call
sets an empty `core.hooksPath`. Git context variables such as `GIT_DIR` are not passed on.
Checkout disables line-ending conversion and overrides conversion attributes in `.git/info/attributes`,
so regular files retain their committed blob bytes regardless of user or repository attributes and filters.

The temporary folder is removed when the block ends, also on exceptions and interrupts. A folder that
cannot be removed is reported through `warnings` instead of hiding the original error.

A design-bound review passes `dispose`: then ONLY that callback decides, and a retained, malformed, raised or
interrupted callback never falls back to `remove`. `capture_retirement_plan`, `validate_retirement_plan` and the
guarded `remove(parent, expected_plan=..., progress=...)` are the complete paged inventory (SC4-P4-R-I) for an
origin-bearing review: create-only linked pages of at most 8192 entries and 1 MiB each, validated in full before
the first deletion, removed entry by entry through no-link directory handles with progress before and after.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from typing import Iterable, Iterator, Mapping, Sequence

from oah.engines.proc import run_command
from oah.paths import git_argv
from oah.review import ReviewError

STRIPPED_FILES = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md")
STRIPPED_DIRS = (".claude", ".codex", ".agents")
CLONE_TIMEOUT = 600.0
GIT_TIMEOUT = 120.0
_FILES = frozenset(name.casefold() for name in STRIPPED_FILES)
_DIRS = frozenset(name.casefold() for name in STRIPPED_DIRS)
_GIT_CONTEXT = frozenset(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                          "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX"))
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclasses.dataclass(frozen=True)
class Snapshot:
    tree: pathlib.Path
    head: str
    stripped: tuple[str, ...]
    parent: pathlib.Path | None = None  # the B-created container (clone, controls and output slot)
    created: dict[str, int | str] | None = None


def stripped(paths: Iterable[str]) -> list[str]:
    """The repository paths a snapshot removes, sorted."""
    out = []
    for path in paths:
        parts = path.split("/")
        if parts[-1].casefold() in _FILES or any(part.casefold() in _DIRS for part in parts):
            out.append(path)
    return sorted(out)


def _env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() not in _GIT_CONTEXT}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(hooks: pathlib.Path, *args: str, cwd: pathlib.Path | None = None, timeout: float = GIT_TIMEOUT) -> str:
    argv = [*git_argv(), "-c", f"core.hooksPath={hooks}", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
            "-c", f"core.attributesFile={os.devnull}"]
    if cwd is not None:
        argv += ["-C", str(cwd)]
    result = run_command([*argv, *args], timeout=timeout, env=_env())
    if result.timed_out or result.returncode != 0:
        reason = "it timed out" if result.timed_out else (result.stderr.strip() or f"exit {result.returncode}")
        raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", f"git {args[0]} failed while building the review snapshot "
                          f"({reason}). Nothing was reviewed; check that the commits exist, then run oah review again")
    return result.stdout


def _tracked_paths(tree: pathlib.Path, entries: Iterable[str]) -> list[str]:
    """Check committed links before stripping paths, including Windows' plain-file link checkout."""
    paths = []
    root = tree.resolve()
    for entry in entries:
        if not entry:
            continue
        info, name = entry.split("\t", 1)
        paths.append(name)
        if not info.startswith("120000 "):
            continue
        path = tree / name
        try:
            target = (path.resolve() if path.is_symlink() else
                      (path.parent / os.fsdecode(path.read_bytes())).resolve())
            safe = target.is_relative_to(root) and not target.is_dir()
        except (OSError, ValueError, RuntimeError):
            safe = False
        if not safe:
            raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", "a tracked symbolic link names a directory, leaves "
                              "the snapshot or cannot be resolved safely; replace it with regular committed "
                              "source, then run oah review again")
    return paths


def _strip(tree: pathlib.Path, paths: Iterable[str]) -> None:
    roots: set[pathlib.Path] = set()
    for path in paths:
        parts = path.split("/")
        folder = next((n for n, part in enumerate(parts) if part.casefold() in _DIRS), None)
        roots.add(tree.joinpath(*(parts if folder is None else parts[:folder + 1])))
    for target in sorted(roots, key=lambda item: len(item.parts)):
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir() and not remove(target):
            raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", f"could not strip {target} from the review snapshot; "
                              "nothing was reviewed. Check the folder's permissions and run oah review again")


def remove(path: pathlib.Path, *, expected_plan: "ValidatedPlan | None" = None, progress=None, guard=None) -> bool:
    """Remove a folder tree, clearing read-only flags (Windows git objects); True when it is gone.

    With `expected_plan` (a ValidatedPlan) it unlinks only that plan's exact unchanged entries, deepest first,
    calling `progress(entry, "intent")` before and `progress(entry, observed)` after each, and stops (False) at the
    first unexpected, replaced or unowned entry or failed progress write. It never falls back to rmtree."""
    if expected_plan is not None:
        return _guarded_remove(path, expected_plan, progress, guard)
    def retry(function, target, _error) -> None:
        try:
            os.chmod(target, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
            function(target)
        except OSError:
            pass  # the final existence check reports it

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    except OSError:
        pass
    return not os.path.lexists(path)


@contextlib.contextmanager
def make(source: pathlib.Path, head: str, *, base: str, temp_root: pathlib.Path | None = None,
         warnings: list[str] | None = None, dispose=None) -> Iterator[Snapshot]:
    """A sanitized clone of `source` at `head` for the duration of the block.

    `dispose(parent, created)` (design-bound reviews): the exclusive finalizer. It returns (disposition, why) with
    disposition disposed | legacy-disposed | disposed-noncounting | retained; only the first two can count a round.
    Anything else, or an exception, retains the folder."""
    for name, value in (("head", head), ("base", base)):
        if not _SHA.fullmatch(value or ""):
            raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", f"the {name} {value!r} is not a full commit SHA")
    if dispose is not None:
        from oah.design import task_context as tc

        try:
            temp_root = tc._trusted_root(pathlib.Path(temp_root) if temp_root is not None else
                                         pathlib.Path(tempfile.gettempdir()))
        except (OSError, ValueError, tc.RootUnavailable) as exc:
            raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", "the design snapshot temporary base cannot be anchored; "
                              "check its path, permissions and local filesystem, then retry") from exc
    parent = pathlib.Path(tempfile.mkdtemp(prefix="oah-review-", dir=temp_root))
    created = None
    identity_failure = None
    try:
        if dispose is not None:
            from oah.design import task_context as tc

            try:
                created = _container_object(parent, empty=True)
            except (OSError, ValueError, tc.InstallRefused, tc.RootUnavailable, Retained) as exc:
                raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", "the design snapshot root cannot be anchored; "
                                  "check its path, permissions and local filesystem, then retry") from exc
        hooks = parent / "no-hooks"
        hooks.mkdir()
        tree = parent / "tree"
        _git(hooks, "clone", "--quiet", "--no-local", "--no-checkout", "--template=", "--", str(source), str(tree),
             timeout=CLONE_TIMEOUT)
        info = tree / ".git" / "info"
        info.mkdir(exist_ok=True)
        # info/attributes outranks all .gitattributes, including nested files. Unset every
        # checkout conversion: text/eol, external smudge filters, $Id$ and encoding conversion.
        (info / "attributes").write_bytes(b"* -text -filter -ident -working-tree-encoding\n")
        _git(hooks, "checkout", "--quiet", "--detach", head, cwd=tree)
        found = _git(hooks, "rev-parse", "--verify", "HEAD^{commit}", cwd=tree).strip()
        if found != head:
            raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", f"the snapshot is at {found}, not the reviewed head {head}")
        _git(hooks, "cat-file", "-e", f"{base}^{{commit}}", cwd=tree)
        _git(hooks, "remote", "remove", "origin", cwd=tree)
        paths = _tracked_paths(tree, _git(hooks, "ls-files", "--stage", "-z", cwd=tree).split("\0"))
        removed = stripped(paths)
        _strip(tree, removed)
        yield Snapshot(tree=tree, head=head, stripped=tuple(removed), parent=parent,
                       created=dict(created) if created is not None else None)
    except CreationRetained as exc:
        identity_failure = str(exc)
        raise
    finally:
        if dispose is None:
            if not remove(parent) and warnings is not None:
                warnings.append(f"OAH_REVIEW_SNAPSHOT_LEFT: the review snapshot {parent} could not be removed; "
                                "delete it by hand once no reviewer process uses it")
        else:
            if created is None or identity_failure is not None:
                outcome = ("retained", identity_failure or "the allocation's original directory identity could not be verified")
            else:
                try:
                    outcome = dispose(parent, created)
                except (OSError, ValueError, RuntimeError, Retained, PlanUnavailable) as exc:
                    outcome = ("retained", f"the disposal check failed ({type(exc).__name__})")
            if not (isinstance(outcome, tuple) and len(outcome) == 2
                    and outcome[0] in ("disposed", "legacy-disposed", "disposed-noncounting", "retained")):
                outcome = ("retained", "the disposal check returned no valid result")
            if outcome[0] == "retained" and warnings is not None:
                warnings.append(f"OAH_REVIEW_SNAPSHOT_RETAINED: the review snapshot {parent} was kept "
                                f"({outcome[1]}); this round does not count")
            if outcome[0] == "disposed-noncounting" and warnings is not None:
                warnings.append("OAH_REVIEW_SNAPSHOT_INTERRUPTED: retirement completed after an interrupted "
                                "delete; its absence acknowledgment does not count the original round")


# Complete bounded retirement inventory (SC4-P4-R-I) -------------------------------------------------------------

PAGE_BYTES = 1024 * 1024
PAGE_ENTRIES = 8192
UINT64 = 2 ** 64 - 1
RETIREMENT_DIR = "design-retirement"


class Entries:
    def __init__(self, inventory: "Inventory", *, remaining: bool = False):
        self.inventory, self.remaining = inventory, remaining

    def __len__(self) -> int:
        where = " WHERE removed=0" if self.remaining else ""
        return self.inventory.db.execute("SELECT count(*) FROM entry" + where).fetchone()[0]

    def __iter__(self) -> Iterator[dict]:
        where = " WHERE removed=0" if self.remaining else ""
        cursor = self.inventory.db.execute("SELECT raw FROM entry" + where + " ORDER BY depth, spelling")
        for row in cursor:
            yield json.loads(row[0])

    def at(self, path: Sequence[str]) -> dict | None:
        row = self.inventory.db.execute("SELECT raw FROM entry WHERE path=?", (_canonical(list(path)),)).fetchone()
        return json.loads(row[0]) if row is not None else None


class Inventory:
    def __init__(self, directory: pathlib.Path):
        self.scratch = tempfile.TemporaryDirectory(prefix="census-", dir=directory)
        self.db = sqlite3.connect(str(pathlib.Path(self.scratch.name) / "inventory.sqlite3"))
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("CREATE TABLE entry(path BLOB PRIMARY KEY, folded BLOB UNIQUE, depth INTEGER NOT NULL, spelling BLOB NOT NULL, "
                        "raw BLOB NOT NULL, page INTEGER NOT NULL, position INTEGER NOT NULL, "
                        "seen INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE INDEX removal_order ON entry(depth, spelling)")
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.db.close()
            self.scratch.cleanup()

    def __del__(self):
        if hasattr(self, "closed"):
            self.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def add(self, item: dict, *, page: int = 0, position: int = 0) -> None:
        if not isinstance(item, dict):
            raise ValueError("the census entry is not an object")
        path = item.get("path")
        if (not isinstance(path, list) or not path or len(path) > 64 or
                any(not isinstance(p, str) or not p or p in (".", "..") or "/" in p or "\\" in p or "\0" in p
                    for p in path)):
            raise ValueError("the census contains an invalid path")
        fields = {"path", "type", "mode", "object", "size", "sha256", "nlink"}
        if (set(item) not in (fields, fields | {"security"}) or item.get("type") not in ("dir", "file")
                or type(item.get("mode")) is not int or not 0 <= item["mode"] <= 0o7777 or
                not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("object"))) or
                ("security" in item and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item["security"])))):
            raise ValueError("the census object schema is malformed")
        if item["type"] == "file":
            if not _uint64(item["size"]) or item["nlink"] != 1 or \
                    not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item["sha256"])):
                raise ValueError("the census file observation is malformed")
        elif item["size"] is not None or item["nlink"] is not None or \
                (item["type"] == "dir" and item["sha256"] is not None) or \
                (item["type"] == "link" and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item["sha256"]))):
            raise ValueError("the census directory or link observation is malformed")
        raw = _canonical(item)
        if len(raw) > 1024 * 1024:
            raise ValueError("one census entry exceeds its byte bound")
        try:
            folded = _canonical([part.casefold() for part in path]) if os.name == "nt" else None
            self.db.execute("INSERT INTO entry(path,folded,depth,spelling,raw,page,position) VALUES(?,?,?,?,?,?,?)",
                            (_canonical(path), folded, -len(path), os.fsencode("/".join(path)), raw, page, position))
        except sqlite3.IntegrityError as exc:
            raise ValueError("the complete census repeats a path") from exc

    def remove_slot(self, path) -> None:
        self.db.execute("DELETE FROM entry WHERE path=?", (_canonical(list(path)),))

    def original_order(self) -> Iterator[dict]:
        for row in self.db.execute("SELECT raw FROM entry ORDER BY page, position"):
            yield json.loads(row[0])

    def observe(self, item: dict) -> None:
        key = _canonical(item["path"])
        row = self.db.execute("SELECT raw,seen,removed FROM entry WHERE path=?", (key,)).fetchone()
        if row is None:
            raise ValueError("the complete census contains an unplanned entry")
        if row[1] or row[2] or json.loads(row[0]) != item:
            raise ValueError("the complete census contains a repeated, removed or changed object")
        self.db.execute("UPDATE entry SET seen=1 WHERE path=?", (key,))

    def removed(self, paths: Iterable[tuple]) -> None:
        for path in paths:
            if not path:
                continue
            changed = self.db.execute("UPDATE entry SET removed=1 WHERE path=?", (_canonical(list(path)),)).rowcount
            if changed != 1:
                raise ValueError("recorded progress names an unplanned entry")

    def complete(self) -> bool:
        return self.db.execute("SELECT 1 FROM entry WHERE seen=0 AND removed=0 LIMIT 1").fetchone() is None

    def entries(self, *, remaining=False) -> Entries:
        return Entries(self, remaining=remaining)


class PlanUnavailable(Exception):
    """The complete plan cannot be captured (storage, permissions, resources); known before any origin/payment."""


class Retained(Exception):
    """Validation or guarded removal stopped; the precise remainder stays."""


class CreationRetained(Retained):
    pass


def _canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _object(info: os.stat_result) -> dict:
    return {"dev": info.st_dev, "ino": info.st_ino, "type": stat.S_IFMT(info.st_mode)}


def _object_digest(info: os.stat_result) -> str:
    return "sha256:" + hashlib.sha256(_canonical([info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
                                                 info.st_uid, info.st_gid])).hexdigest()


def _container_object(path: pathlib.Path, *, empty: bool = False) -> dict[str, int | str]:
    from oah.design import task_context as tc

    if os.name != "nt":
        descriptor = tc._continuity_open(path)
        try:
            created = _container_stat(os.fstat(descriptor))
            if empty:
                with os.scandir(descriptor) as entries:
                    if next(entries, None) is not None:
                        raise Retained("the new snapshot container already has unowned entries")
            if _container_stat(os.lstat(path)) != created:
                raise Retained("the snapshot container changed while it was anchored")
            return created
        finally:
            os.close(descriptor)
    native = tc._WindowsIO()
    held, anchors = native.open_anchor(path)
    try:
        info = native.info(held)
        if empty and native.names(held, limit=1):
            raise Retained("the new snapshot container already has unowned entries")
        return _native_container(info)
    finally:
        for handle in reversed(anchors):
            native.close(handle)


def _container_stat(info) -> dict:
    return dict(_object(info), mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)


def _native_container(info) -> dict:
    return {"dev": info["dev"], "ino": info["ino"], "type": stat.S_IFDIR,
            "mode": info["mode"], "security": info["uid"]}


def require_creation_identity(parent: pathlib.Path, created: dict[str, int | str] | None,
                              *recorded: dict[str, int | str] | None, current: bool = True) -> dict[str, int | str]:
    from oah.design import task_context as tc

    if (not isinstance(created, dict) or not {"dev", "ino", "type", "mode"}.issubset(created)
            or any(type(created[key]) is not int for key in ("dev", "ino", "type", "mode"))
            or created["type"] != stat.S_IFDIR
            or not ((set(created) == {"dev", "ino", "type", "mode", "uid", "gid"}
                     and type(created["uid"]) is int and type(created["gid"]) is int)
                    or (set(created) == {"dev", "ino", "type", "mode", "security"}
                        and isinstance(created["security"], str) and created["security"]))
            or any(identity != created for identity in recorded)):
        raise CreationRetained("the snapshot has no consistent original creation identity")
    try:
        if current and _container_object(parent) != created:
            raise CreationRetained("the snapshot folder is not the one this review created")
    except (OSError, ValueError, tc.InstallRefused, tc.RootUnavailable) as exc:
        raise CreationRetained("the snapshot creation identity cannot be verified") from exc
    return dict(created)


def _native_entry(native, handle, components) -> dict:
    before = native.info(handle)
    directory = before["type"] == "directory"
    kind = stat.S_IFDIR if directory else stat.S_IFREG
    digest = None if directory else "sha256:" + native.digest(handle, before["size"])
    if before != native.info(handle):
        raise PlanUnavailable("a native snapshot object changed during its complete read")
    return {"path": list(components), "type": "dir" if directory else "file", "mode": before["mode"],
            "object": "sha256:" + hashlib.sha256(_canonical([before["dev"], before["ino"], kind])).hexdigest(),
            "size": None if directory else before["size"], "sha256": digest,
            "nlink": None if directory else before["nlink"], "security": before["uid"]}


def _windows_census(parent, *, exclude=(), native=None):
    from oah.design import task_context as tc

    native = native if native is not None else tc._WindowsIO()
    root, anchors = native.open_anchor(parent)
    def walk(prefix, directory):
        if len(prefix) >= 64:
            raise PlanUnavailable("the native snapshot path depth exceeds its bound")
        for name in native.iter_names(directory):
            components = (*prefix, name)
            if any(components[:len(item)] == item for item in exclude):
                continue
            handle, _ = native.open_child(directory, name, directory=None, inspect=True, enumerated=True)
            try:
                item = _native_entry(native, handle, components)
                yield item
                if item["type"] == "dir":
                    yield from walk(components, handle)
            finally:
                native.close(handle)
    try:
        yield from walk((), root)
    except (OSError, ValueError, tc.InstallRefused) as exc:
        raise PlanUnavailable("the complete native snapshot census is unknown") from exc
    finally:
        for handle in reversed(anchors):
            native.close(handle)


def _order(entry: dict) -> tuple:
    """Child before parent: deeper first, then the exact native byte spelling; ties strictly increase."""
    return (-len(entry["path"]), os.fsencode("/".join(entry["path"])))


def _entry(components: list[str], info: os.stat_result, directory_fd: int, name: str) -> dict:
    mode = info.st_mode
    item = {"path": components, "mode": stat.S_IMODE(mode), "object": _object_digest(info), "size": None,
            "sha256": None, "nlink": info.st_nlink if stat.S_ISREG(mode) else None}
    if stat.S_ISDIR(mode):
        item["type"] = "dir"
    elif stat.S_ISLNK(mode):
        raise PlanUnavailable("the retirement inventory contains a symbolic link")
    elif stat.S_ISREG(mode):
        if info.st_nlink != 1:
            raise PlanUnavailable(f"{'/'.join(components)} has more than one link")
        item["type"] = "file"
        item["size"] = info.st_size
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        try:
            if not os.path.samestat(info, os.fstat(descriptor)):
                raise PlanUnavailable("the file changed while being opened")
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(descriptor, 1 << 20):
                size += len(chunk)
                if size > info.st_size:
                    raise PlanUnavailable("the file grew during its complete read")
                digest.update(chunk)
            after = os.fstat(descriptor)
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            def stamp(value):
                return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_uid, value.st_gid,
                        value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if stamp(info) != stamp(after) or stamp(info) != stamp(named) or size != info.st_size:
                raise PlanUnavailable("the file changed during its complete read")
        finally:
            os.close(descriptor)
        item["sha256"] = "sha256:" + digest.hexdigest()
    else:
        raise PlanUnavailable(f"{'/'.join(components)} is not a file, folder or link")
    return item


def _census(parent: pathlib.Path, *, exclude: tuple[tuple[str, ...], ...] = ()) -> Iterator[dict]:
    """Every entry under the anchored container (not the container itself), without following any link."""
    if os.name == "nt":
        yield from _windows_census(parent, exclude=exclude)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    def walk(prefix, descriptor):
        if len(prefix) >= 64:
            raise PlanUnavailable("the snapshot path depth exceeds its native inspection bound")
        with os.scandir(descriptor) as listing:
            for member in listing:
                name = member.name
                components = (*prefix, name)
                if any(components[:len(item)] == item for item in exclude):
                    continue
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                yield _entry(list(components), info, descriptor, name)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, flags, dir_fd=descriptor)
                    try:
                        yield from walk(components, child)
                    finally:
                        os.close(child)
    root = os.open(str(parent), flags)
    try:
        yield from walk((), root)
    except MemoryError:
        raise PlanUnavailable("the inventory exceeds the available memory") from None
    except OSError as exc:
        raise PlanUnavailable(f"the snapshot cannot be enumerated ({type(exc).__name__})") from None
    finally:
        os.close(root)


@dataclasses.dataclass(frozen=True)
class RetirementPlan:
    descriptor: dict
    descriptor_sha256: str
    pages_dir: pathlib.Path


def _write_page(directory: pathlib.Path, data: bytes) -> str:
    from oah.lease import persist_create_only

    name = hashlib.sha256(data).hexdigest()
    path = directory / f"{name}.json"
    try:
        persist_create_only(path, data)
    except OSError as exc:
        raise PlanUnavailable(f"page {name} cannot be persisted ({type(exc).__name__})") from None
    return name


def write_pages(allocation: dict, entries: Iterable[dict], pages_dir: pathlib.Path) -> dict:
    """Pack ordered entries into create-only canonical pages (each <= PAGE_BYTES including metadata and <=
    PAGE_ENTRIES whole entries, the byte accounting exact) and return the descriptor fields."""
    scan = hashlib.sha256()
    previous, ordinal, count, total = None, 0, 0, 0
    page: list[dict] = []

    def base() -> int:
        return len(_canonical({"version": 1, "allocation": allocation, "ordinal": ordinal, "previous": previous,
                               "entries": []}))

    def flush() -> None:
        nonlocal previous, ordinal, total, page
        data = _canonical({"version": 1, "allocation": allocation, "ordinal": ordinal, "previous": previous,
                           "entries": page})
        if len(data) > PAGE_BYTES or len(page) > PAGE_ENTRIES:
            raise PlanUnavailable("a page exceeds its bound")
        name = _write_page(pages_dir, data)
        previous = {"sha256": name, "bytes": len(data), "ordinal": ordinal}
        total += len(data)
        ordinal += 1
        if total > UINT64 or ordinal > UINT64:
            raise PlanUnavailable("the complete page totals exceed uint64")
        page = []

    size = base()
    for item in entries:
        encoded = _canonical(item)
        scan.update(encoded + b"\n")
        if page and (len(page) + 1 > PAGE_ENTRIES or size + 1 + len(encoded) > PAGE_BYTES):
            flush()
            size = base()
        if size + len(encoded) > PAGE_BYTES:
            raise PlanUnavailable(f"one entry is larger than a page ({'/'.join(item['path'])[:80]})")
        size += len(encoded) + (1 if page else 0)
        page.append(item)
        count += 1
        if count > UINT64:
            raise PlanUnavailable("the complete entry count exceeds uint64")
    flush()  # the last page (also the single empty page of an empty container)
    if previous is None:
        raise PlanUnavailable("the complete plan has no durable final page")
    return {"head": {"sha256": previous["sha256"], "bytes": previous["bytes"]}, "page_count": ordinal,
            "total_entries": count, "total_page_bytes": total, "complete_scan_digest": "sha256:" + scan.hexdigest()}


def capture_retirement_plan(allocation: dict, parent: pathlib.Path, state_dir: pathlib.Path, *,
                            output_slot: dict, exclude: tuple[tuple[str, ...], ...] = ()) -> RetirementPlan:
    """The complete prelaunch inventory of the B-created container as linked create-only pages; every page is
    durable before the descriptor that names it exists. Raises PlanUnavailable before any origin or payment."""
    try:
        created = require_creation_identity(parent, allocation.get("container_object"))
        allocation_id = hashlib.sha256(_canonical(allocation)).hexdigest()
        pages_dir = pathlib.Path(state_dir) / RETIREMENT_DIR / allocation_id / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        with Inventory(pages_dir.parent) as inventory:
            for entry in _census(parent, exclude=exclude):
                inventory.add(entry)
            require_creation_identity(parent, created, allocation.get("container_object"))
            fields = write_pages(allocation, inventory.entries(), pages_dir)
        _fsync(pages_dir)
        require_creation_identity(parent, created, allocation.get("container_object"))
        descriptor = {"version": 1, "allocation": allocation, **fields, "output_slot": output_slot,
                      "container_object": created}
        return RetirementPlan(descriptor, hashlib.sha256(_canonical(descriptor)).hexdigest(), pages_dir)
    except PlanUnavailable:
        raise
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise PlanUnavailable(f"the retirement pages cannot be written ({type(exc).__name__})") from None


def _fsync(directory: pathlib.Path) -> None:
    if os.name == "nt":
        from oah.design.task_context import _windows_flush_directory

        return _windows_flush_directory(directory)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def finalize_retirement_plan(plan: RetirementPlan, output_final: dict) -> dict:
    """FinalPlanV1: the immutable prelaunch descriptor plus the ONE designated output slot's final observation."""
    return {"version": 1, "base_plan": plan.descriptor, "base_plan_sha256": plan.descriptor_sha256,
            "output_slot_final": dict(output_final)}


def capture_output_slot(parent: pathlib.Path, components: list[str], *, producer: dict) -> dict:
    if (not components or any(not isinstance(p, str) or p in ("", ".", "..") or "/" in p or "\\" in p
                              for p in components)):
        raise Retained("the final output slot has invalid path components")
    if os.name == "nt":
        return _windows_output_slot(parent, components, producer=producer)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    opened = [os.open(parent, flags)]
    def parent_identity(info):
        return dict(_object(info), mode=stat.S_IMODE(info.st_mode), nlink=info.st_nlink,
                    uid=info.st_uid, gid=info.st_gid)

    parents = [parent_identity(os.fstat(opened[0]))]
    try:
        for folder in components[:-1]:
            opened.append(os.open(folder, flags, dir_fd=opened[-1]))
            parents.append(parent_identity(os.fstat(opened[-1])))
        try:
            info = os.stat(components[-1], dir_fd=opened[-1], follow_symlinks=False)
        except FileNotFoundError:
            return {"presence": "absent", "path": components, "parents": parents, "producer": dict(producer)}
        item = _entry(components, info, opened[-1], components[-1])
        if item["type"] != "file" or item["nlink"] != 1:
            raise Retained("the designated final output is not a single-link regular file")
        return dict(item, presence="present", parents=parents, producer=dict(producer))
    except (OSError, PlanUnavailable) as exc:
        raise Retained(f"the final owned output cannot be captured ({type(exc).__name__})") from None
    finally:
        for fd in reversed(opened):
            os.close(fd)


@dataclasses.dataclass(frozen=True)
class ValidatedPlan:
    final: dict
    entries: Entries
    parent: pathlib.Path
    all_entries: Entries
    pending_absent: bool = False


def _uint64(value) -> bool:
    return type(value) is int and 0 <= value <= UINT64


def read_evidence(path: pathlib.Path, limit: int) -> bytes:
    from oah.design import task_context as tc

    descriptor, native = None, None
    try:
        with contextlib.closing(tc.LiveRoots({"evidence-parent": path.parent})) as live:
            row = live.rows[0]
            if live.native is not None:
                native = live.native
                descriptor, _ = native.open_child(row["handles"][-1], path.name, directory=False)
                before = native.info(descriptor)
                raw = native.read(descriptor, limit)
                if before != native.info(descriptor) or not live.current():
                    raise Retained("the native evidence changed while reading")
            else:
                descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                     dir_fd=row["handles"][-1])
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                    raise Retained("the evidence is not a bounded single-link regular file")
                chunks, size = [], 0
                while chunk := os.read(descriptor, min(65536, limit + 1 - size)):
                    size += len(chunk)
                    if size > limit:
                        raise Retained("the evidence grew beyond its bound")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                named = os.stat(path.name, dir_fd=row["handles"][-1], follow_symlinks=False)
                def stamp(info):
                    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
                            info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if stamp(before) != stamp(os.fstat(descriptor)) or stamp(before) != stamp(named) or not live.current():
                    raise Retained("the evidence changed while reading")
            return raw
    except (OSError, tc.InstallRefused, ValueError) as exc:
        raise Retained("the anchored evidence cannot be read completely") from exc
    finally:
        if descriptor is not None:
            if native is not None:
                native.close(descriptor)
            else:
                os.close(descriptor)


def retirement_parent_proof(parent: pathlib.Path, nonce: str) -> dict[str, object]:
    from oah.design import task_context as tc

    with contextlib.closing(tc.LiveRoots({"retirement-surviving-parent": parent.parent})) as live:
        proof = live.facts(nonce)
        if not live.current():
            raise Retained("the surviving retirement parent changed")
        return proof


def _pending_retirement_entry(pending, final: dict[str, object], entries: Entries, removed) -> Mapping[str, object]:
    from oah.design import task_context as tc

    try:
        fact, predecessor = pending["fact"], pending["predecessor"]
        entry = next((item for index, item in enumerate(entries) if index == len(removed)),
                     {"path": [], "type": "root"})
        if (len(removed) > len(entries) or fact["plan_sha256"] != final["base_plan_sha256"]
                or fact["final_sha256"] != hashlib.sha256(_canonical(final)).hexdigest()
                or type(fact["cursor"]) is not int or fact["cursor"] != len(removed)
                or fact["observed"] != "intent" or fact["path"] != entry["path"]
                or fact["entry_sha256"] != hashlib.sha256(_canonical(entry)).hexdigest()
                or type(pending["sequence"]) is not int or type(predecessor["sequence"]) is not int
                or predecessor["sequence"] < 0 or pending["sequence"] != predecessor["sequence"] + 1
                or pending["record_digest"] != tc.observation_digest(pending["origin"], pending["sequence"],
                                                                   predecessor, "retirement-intent", fact)):
            raise Retained("the pending entry, plan or exact intent predecessor changed")
        return entry
    except (KeyError, TypeError, ValueError):
        raise Retained("the pending retirement proof is incomplete") from None


def _pending_root_absent(parent: pathlib.Path, final: dict[str, object], pending) -> bool:
    from oah.design import task_context as tc

    plan_digest = final.get("base_plan_sha256")
    if not isinstance(plan_digest, str):
        raise Retained("the pending root has no exact plan digest")
    with contextlib.closing(tc.LiveRoots({"retirement-surviving-parent": parent.parent})) as live:
        if (live.facts(plan_digest) != pending["fact"].get("surviving_parent")
                or not live.current()):
            raise Retained("the pending root absence has no exact surviving-parent proof")
        handle = live.rows[0]["handles"][-1]
        try:
            if live.native is None:
                os.stat(parent.name, dir_fd=handle, follow_symlinks=False)
            else:
                child, _ = live.native.open_child(handle, parent.name, directory=None, inspect=True)
                live.native.close(child)
        except FileNotFoundError:
            if live.current():
                return True
            raise Retained("the surviving retirement parent changed during the absence check") from None
        return False


def validate_retirement_plan(final: dict, parent: pathlib.Path, pages_dir: pathlib.Path, *,
                             removed: frozenset[tuple[str, ...]] = frozenset(), container_removed=False, pending=None) -> ValidatedPlan:
    """Read EVERY linked page (exact length/hash, schema, ordinals, linkage, order, uniqueness, allocation,
    totals, scan digest) and enumerate the whole current container; any extra, missing (without recorded
    progress), changed or unowned entry raises Retained before the first deletion."""
    descriptor = final.get("base_plan") or {}
    if (hashlib.sha256(_canonical(descriptor)).hexdigest() != final.get("base_plan_sha256")
            or descriptor.get("version") != 1
            or not all(_uint64(descriptor.get(name)) for name in ("page_count", "total_entries", "total_page_bytes"))):
        raise Retained("the retirement descriptor is malformed or does not match its sha256")
    pending_root = (pending is not None and pending.get("fact", {}).get("path") == []
                    and not os.path.lexists(parent))
    allocation = descriptor.get("allocation")
    if not isinstance(allocation, dict):
        raise Retained("the retirement plan has no creation allocation")
    require_creation_identity(parent, allocation.get("container_object"), descriptor.get("container_object"),
                              current=not container_removed and not pending_root)
    working = Inventory(pathlib.Path(pages_dir).parent)
    link, total_bytes = descriptor["head"], 0
    for expected_ordinal in range(descriptor["page_count"] - 1, -1, -1):
        if not isinstance(link, dict) or not isinstance(link.get("sha256"), str) or not _uint64(link.get("bytes")):
            raise Retained("a page link is missing or malformed")
        path = pathlib.Path(pages_dir) / f"{link['sha256']}.json"
        data = read_evidence(path, PAGE_BYTES)
        if hashlib.sha256(data).hexdigest() != link["sha256"] or len(data) != link["bytes"]:
            raise Retained(f"page {link['sha256']} was changed")
        try:
            from oah.design.task_context import strict_json
            page = strict_json(data.decode("utf-8"))
        except ValueError:
            raise Retained(f"page {link['sha256']} is not JSON") from None
        if (not isinstance(page, dict) or set(page) != {"version", "allocation", "ordinal", "previous", "entries"}
                or page["version"] != 1 or page["allocation"] != descriptor["allocation"]
                or page["ordinal"] != expected_ordinal or not isinstance(page["entries"], list)
                or len(page["entries"]) > PAGE_ENTRIES or _canonical(page) != data):
            raise Retained(f"page {link['sha256']} has the wrong schema, allocation or ordinal")
        if expected_ordinal == 0 and page["previous"] is not None:
            raise Retained("the page chain does not end at ordinal 0")
        if expected_ordinal and (not isinstance(page["previous"], dict)
                                 or page["previous"].get("ordinal") != expected_ordinal - 1):
            raise Retained("the page chain is not contiguous")
        total_bytes += len(data)
        try:
            for position, item in enumerate(page["entries"]):
                working.add(item, page=expected_ordinal, position=position)
        except (ValueError, sqlite3.Error) as exc:
            raise Retained("the immutable page has an invalid, repeated or unreadable entry") from exc
        link = page["previous"]
    scan = hashlib.sha256()
    previous = None
    for item in working.original_order():
        if previous is not None and _order(previous) >= _order(item):
            raise Retained("the planned entries are out of order or duplicated")
        scan.update(_canonical(item) + b"\n")
        previous = item
    if (len(working.entries()) != descriptor["total_entries"] or total_bytes != descriptor["total_page_bytes"]
            or "sha256:" + scan.hexdigest() != descriptor["complete_scan_digest"]):
        raise Retained("the pages do not hold the complete plan (entry count, byte total or scan digest)")
    slot = tuple((descriptor.get("output_slot") or {}).get("path") or ())
    final_slot = final.get("output_slot_final") or {}
    output_entry = None
    if final_slot.get("presence") == "present":
        producer = final_slot.get("producer")
        if (not isinstance(producer, dict) or producer.get("run") != descriptor["allocation"].get("run")
                or not isinstance(producer.get("checkpoint"), str) or not producer["checkpoint"]):
            raise Retained("the final output lacks its exact run and producer checkpoint")
        observed = final_slot
        if slot not in removed and not container_removed and (pending is None or pending["fact"]["path"] != list(slot)):
            observed = capture_output_slot(parent, list(slot), producer=producer)
            if observed != final_slot:
                raise Retained("the output slot changed after its final capture")
        output_entry = {key: value for key, value in observed.items() if key not in ("presence", "parents", "producer")}
    elif final_slot.get("presence") != "absent":
        raise Retained("the output slot's absence is not established")
    working.remove_slot(slot)
    if output_entry is not None:
        working.add(output_entry)
    pending_entry = _pending_retirement_entry(pending, final, working.entries(), removed) if pending is not None else None
    pending_absent = False
    try:
        working.removed(removed)
        if container_removed or pending_root:
            if os.path.lexists(parent) or len(working.entries(remaining=True)) != 0:
                raise Retained("the removed container has no exact complete durable progress")
            if pending_root:
                if pending_entry != {"path": [], "type": "root"} or not _pending_root_absent(parent, final, pending):
                    raise Retained("the missing container has no exact pending root proof")
                pending_absent = True
        else:
            for item in _census(parent):
                working.observe(item)
            if not working.complete() and pending is not None and pending_entry is not None and pending_entry["path"]:
                working.removed([tuple(pending["fact"]["path"])])
                pending_absent = True
            if not working.complete():
                raise Retained("a planned entry is missing without recorded progress")
    except (PlanUnavailable, ValueError, sqlite3.Error) as exc:
        raise Retained(f"the complete census does not match the immutable plan ({type(exc).__name__})") from exc
    return ValidatedPlan(json.loads(_canonical(final)), working.entries(remaining=True), pathlib.Path(parent),
                         working.entries(), pending_absent)


def _guarded_remove(parent: pathlib.Path, plan: ValidatedPlan, progress, guard=None) -> bool:
    if pathlib.Path(parent) != plan.parent or progress is None:
        return False
    if os.name == "nt":
        return _windows_guarded_remove(parent, plan, progress, guard)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    slot = tuple((plan.final["base_plan"].get("output_slot") or {}).get("path") or ())
    try:
        root = os.open(str(parent), flags)
    except OSError:
        return False
    try:
        descriptor = plan.final["base_plan"]
        created = require_creation_identity(parent, descriptor["allocation"].get("container_object"),
                                           descriptor.get("container_object"), _container_stat(os.fstat(root)))
        for item in plan.entries:
            *folders, name = item["path"]
            directory = root
            opened = []
            try:
                with guard(item) if guard is not None else contextlib.nullcontext():
                    require_creation_identity(parent, created)
                    prefix = []
                    for folder in folders:
                        prefix.append(folder)
                        ancestor = os.open(folder, flags, dir_fd=directory)
                        opened.append(ancestor)
                        expected = plan.all_entries.at(prefix)
                        if (expected is None or expected["type"] != "dir" or
                                _entry(prefix, os.fstat(ancestor), directory, folder) != expected or
                                _entry(prefix, os.stat(folder, dir_fd=directory, follow_symlinks=False),
                                       directory, folder) != expected):
                            raise Retained("an owned parent directory changed before deletion")
                        directory = ancestor
                    if progress(item, "intent") is False:
                        raise Retained("the delete intent could not be recorded")
                    current = _entry(item["path"], os.stat(name, dir_fd=directory, follow_symlinks=False), directory, name)
                    if current != item:
                        raise Retained(f"{'/'.join(item['path'])[:80]} changed before deletion")
                    if tuple(item["path"]) == slot:
                        observed = capture_output_slot(parent, list(slot),
                                                       producer=plan.final["output_slot_final"]["producer"])
                        if observed != plan.final["output_slot_final"]:
                            raise Retained("the output object or its parents changed before deletion")
                    if item["type"] == "dir":
                        os.rmdir(name, dir_fd=directory)
                    else:
                        os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                    try:
                        os.stat(name, dir_fd=directory, follow_symlinks=False)
                        observed = "present"
                    except FileNotFoundError:
                        observed = "absent"
                    if progress(item, observed) is False or observed != "absent":
                        raise Retained("the removal could not be observed or recorded")
            finally:
                for descriptor in reversed(opened):
                    os.close(descriptor)
    except (OSError, Retained, PlanUnavailable):
        return False
    finally:
        os.close(root)
    entry = {"path": [], "type": "root"}
    containing = None
    try:
        with guard(entry) if guard is not None else contextlib.nullcontext():
            if progress(entry, "intent") is False:
                return False
            require_creation_identity(parent, created)
            containing = os.open(parent.parent, flags)
            require_creation_identity(parent, created,
                _container_stat(os.stat(parent.name, dir_fd=containing, follow_symlinks=False)))
            os.rmdir(parent.name, dir_fd=containing)
            os.fsync(containing)
            return progress(entry, "absent") is not False and not os.path.lexists(parent)
    except (OSError, Retained):
        return False
    finally:
        if containing is not None:
            os.close(containing)


def _windows_output_slot(parent, components, *, producer, native=None, root_handle=None) -> dict:
    from oah.design import task_context as tc

    native = native if native is not None else tc._WindowsIO()
    anchors, opened = [], []
    try:
        root = root_handle
        if root is None:
            root, anchors = native.open_anchor(parent)
        directory = root
        parents = [_native_container(native.info(root))]
        for name in components[:-1]:
            directory, _ = native.open_child(directory, name, directory=True, inspect=True)
            opened.append(directory)
            parents.append(_native_container(native.info(directory)))
        try:
            leaf, _ = native.open_child(directory, components[-1], directory=False, inspect=True)
        except FileNotFoundError:
            return {"presence": "absent", "path": list(components), "parents": parents, "producer": dict(producer)}
        opened.append(leaf)
        item = _native_entry(native, leaf, components)
        if item["type"] != "file" or item["nlink"] != 1:
            raise Retained("the native output slot is not a single-link regular file")
        return dict(item, presence="present", parents=parents, producer=dict(producer))
    except (OSError, ValueError, PlanUnavailable, tc.InstallRefused) as exc:
        raise Retained("the complete native output observation is unknown") from exc
    finally:
        for handle in reversed([*anchors, *opened]):
            native.close(handle)


def _windows_guarded_remove(parent, plan, progress, guard=None, *, native=None) -> bool:
    from oah.design import task_context as tc

    native, root, anchors = native if native is not None else tc._WindowsIO(), None, []
    descriptor = plan.final["base_plan"]
    created = descriptor["allocation"].get("container_object")
    slot = tuple(plan.final["base_plan"]["output_slot"]["path"])
    try:
        root_path = tc._safe_windows_path(str(parent))
        containing, anchors = native.open_anchor(root_path.parent)
        root, _ = native.open_child(containing, root_path.name, directory=True, delete=True)
        require_creation_identity(parent, created, descriptor.get("container_object"),
                                  _native_container(native.info(root)), current=False)

        def check_root():
            named, _ = native.open_child(containing, root_path.name, directory=True, inspect=True)
            try:
                require_creation_identity(parent, created, _native_container(native.info(named)),
                                          _native_container(native.info(root)), current=False)
            finally:
                native.close(named)

        for item in plan.entries:
            handles, directory, leaf = [], root, None
            try:
                with guard(item) if guard is not None else contextlib.nullcontext():
                    check_root()
                    prefix = []
                    for name in item["path"][:-1]:
                        prefix.append(name)
                        directory, _ = native.open_child(directory, name, directory=True, inspect=True)
                        handles.append(directory)
                        expected = plan.all_entries.at(prefix)
                        if expected is None or _native_entry(native, directory, prefix) != expected:
                            raise Retained("a native owned parent directory changed before deletion")
                    leaf, _ = native.open_child(directory, item["path"][-1], directory=item["type"] == "dir",
                                                 delete=True)
                    handles.append(leaf)
                    if progress(item, "intent") is False or _native_entry(native, leaf, item["path"]) != item:
                        raise Retained("the exact native delete intent or owned object changed")
                    if tuple(item["path"]) == slot and _windows_output_slot(parent, list(slot),
                            producer=plan.final["output_slot_final"]["producer"], native=native,
                            root_handle=root) != plan.final["output_slot_final"]:
                        raise Retained("the native final output or its parents were replaced")
                    if item["type"] == "dir" and any(native.iter_names(leaf)):
                        raise Retained("the native directory is not empty")
                    native.delete(leaf)
                    native.close(leaf)
                    handles.pop()
                    native.flush(directory)
                    if any(name == item["path"][-1] for name in native.iter_names(directory)) or \
                            progress(item, "absent") is False:
                        raise Retained("the native removal is not durably observed absent")
            finally:
                for handle in reversed(handles):
                    native.close(handle)
        item = {"path": [], "type": "root"}
        with guard(item) if guard is not None else contextlib.nullcontext():
            check_root()
            if any(native.iter_names(root)) or progress(item, "intent") is False:
                raise Retained("the native final root is not empty or its intent is unavailable")
            native.delete(root)
            native.close(root)
            root = None
            native.flush(containing)
            if any(name == root_path.name for name in native.iter_names(containing)):
                raise Retained("the native final root is not observed absent")
            return progress(item, "absent") is not False
    except (OSError, ValueError, PlanUnavailable, Retained, tc.InstallRefused):
        return False
    finally:
        if root is not None:
            native.close(root)
        for handle in reversed(anchors):
            native.close(handle)
