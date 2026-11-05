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
import stat
import sys
import tempfile
from typing import Iterable, Iterator

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


def remove(path: pathlib.Path, *, expected_plan: "ValidatedPlan | None" = None, progress=None) -> bool:
    """Remove a folder tree, clearing read-only flags (Windows git objects); True when it is gone.

    With `expected_plan` (a ValidatedPlan) it unlinks only that plan's exact unchanged entries, deepest first,
    calling `progress(entry, "intent")` before and `progress(entry, observed)` after each, and stops (False) at the
    first unexpected, replaced or unowned entry or failed progress write. It never falls back to rmtree."""
    if expected_plan is not None:
        return _guarded_remove(path, expected_plan, progress)
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
    disposition disposed | legacy-disposed | retained; anything else, or an exception, retains the folder."""
    for name, value in (("head", head), ("base", base)):
        if not _SHA.fullmatch(value or ""):
            raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", f"the {name} {value!r} is not a full commit SHA")
    parent = pathlib.Path(tempfile.mkdtemp(prefix="oah-review-", dir=temp_root))
    created = _object(os.lstat(parent))
    try:
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
        yield Snapshot(tree=tree, head=head, stripped=tuple(removed), parent=parent)
    finally:
        if dispose is None:
            if not remove(parent) and warnings is not None:
                warnings.append(f"OAH_REVIEW_SNAPSHOT_LEFT: the review snapshot {parent} could not be removed; "
                                "delete it by hand once no reviewer process uses it")
        else:
            try:
                outcome = dispose(parent, created)
            except BaseException as exc:  # an interrupted or failing finalizer retains; it never falls back
                outcome = ("retained", f"the disposal check failed ({type(exc).__name__})")
            if not (isinstance(outcome, tuple) and len(outcome) == 2
                    and outcome[0] in ("disposed", "legacy-disposed", "retained")):
                outcome = ("retained", "the disposal check returned no valid result")
            if outcome[0] == "retained" and warnings is not None:
                warnings.append(f"OAH_REVIEW_SNAPSHOT_RETAINED: the review snapshot {parent} was kept "
                                f"({outcome[1]}); this round does not count")


# Complete bounded retirement inventory (SC4-P4-R-I) -------------------------------------------------------------

PAGE_BYTES = 1024 * 1024
PAGE_ENTRIES = 8192
UINT64 = 2 ** 64 - 1
RETIREMENT_DIR = "design-retirement"


class PlanUnavailable(Exception):
    """The complete plan cannot be captured (storage, permissions, resources); known before any origin/payment."""


class Retained(Exception):
    """Validation or guarded removal stopped; the precise remainder stays."""


def _canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _object(info: os.stat_result) -> dict:
    return {"dev": info.st_dev, "ino": info.st_ino, "type": stat.S_IFMT(info.st_mode)}


def _object_digest(info: os.stat_result) -> str:
    return "sha256:" + hashlib.sha256(_canonical([info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)])).hexdigest()


def _order(entry: dict) -> tuple:
    """Child before parent: deeper first, then the exact native byte spelling; ties strictly increase."""
    return (-len(entry["path"]), os.fsencode("/".join(entry["path"])))


def _entry(components: list[str], info: os.stat_result, directory_fd: int, name: str) -> dict:
    mode = info.st_mode
    item = {"path": components, "mode": stat.S_IMODE(mode), "object": _object_digest(info), "size": None,
            "sha256": None}
    if stat.S_ISDIR(mode):
        item["type"] = "dir"
    elif stat.S_ISLNK(mode):
        item["type"] = "link"
        item["sha256"] = "sha256:" + hashlib.sha256(os.fsencode(os.readlink(name, dir_fd=directory_fd))).hexdigest()
    elif stat.S_ISREG(mode):
        if info.st_nlink != 1:
            raise PlanUnavailable(f"{'/'.join(components)} has more than one link")
        item["type"] = "file"
        item["size"] = info.st_size
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        try:
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1 << 20):
                digest.update(chunk)
        finally:
            os.close(descriptor)
        item["sha256"] = "sha256:" + digest.hexdigest()
    else:
        raise PlanUnavailable(f"{'/'.join(components)} is not a file, folder or link")
    return item


def _census(parent: pathlib.Path, *, exclude: tuple[tuple[str, ...], ...] = ()) -> list[dict]:
    """Every entry under the anchored container (not the container itself), without following any link."""
    if os.name == "nt":
        raise PlanUnavailable("guarded retirement is unsupported on native Windows (windows-process-profile-pending)")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    entries: list[dict] = []
    stack = [((), os.open(str(parent), flags))]
    try:
        while stack:
            prefix, descriptor = stack.pop()
            try:
                for name in sorted(os.listdir(descriptor)):
                    components = (*prefix, name)
                    if any(components[:len(item)] == item for item in exclude):
                        continue
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    entries.append(_entry(list(components), info, descriptor, name))
                    if stat.S_ISDIR(info.st_mode):
                        stack.append((components, os.open(name, flags, dir_fd=descriptor)))
            finally:
                os.close(descriptor)
    except MemoryError:
        raise PlanUnavailable("the inventory exceeds the available memory") from None
    except OSError as exc:
        raise PlanUnavailable(f"the snapshot cannot be enumerated ({type(exc).__name__})") from None
    finally:
        for _prefix, descriptor in stack:
            try:
                os.close(descriptor)
            except OSError:
                pass
    entries.sort(key=_order)
    return entries


@dataclasses.dataclass(frozen=True)
class RetirementPlan:
    descriptor: dict
    descriptor_sha256: str
    pages_dir: pathlib.Path


def _write_page(directory: pathlib.Path, data: bytes) -> str:
    name = hashlib.sha256(data).hexdigest()
    path = directory / f"{name}.json"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
    except FileExistsError:
        with open(path, "rb") as handle:
            if handle.read(PAGE_BYTES + 1) != data:
                raise PlanUnavailable(f"page {name} exists with other bytes; it is never overwritten") from None
        return name
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return name


def write_pages(allocation: dict, entries: list[dict], pages_dir: pathlib.Path) -> dict:
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
    flush()  # the last page (also the single empty page of an empty container)
    return {"head": {"sha256": previous["sha256"], "bytes": previous["bytes"]}, "page_count": ordinal,
            "total_entries": count, "total_page_bytes": total, "complete_scan_digest": "sha256:" + scan.hexdigest()}


def capture_retirement_plan(allocation: dict, parent: pathlib.Path, state_dir: pathlib.Path, *,
                            output_slot: dict, exclude: tuple[tuple[str, ...], ...] = ()) -> RetirementPlan:
    """The complete prelaunch inventory of the B-created container as linked create-only pages; every page is
    durable before the descriptor that names it exists. Raises PlanUnavailable before any origin or payment."""
    try:
        entries = _census(parent, exclude=exclude)
        allocation_id = hashlib.sha256(_canonical(allocation)).hexdigest()
        pages_dir = pathlib.Path(state_dir) / RETIREMENT_DIR / allocation_id / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        fields = write_pages(allocation, entries, pages_dir)
        _fsync(pages_dir)
        descriptor = {"version": 1, "allocation": allocation, **fields, "output_slot": output_slot}
        return RetirementPlan(descriptor, hashlib.sha256(_canonical(descriptor)).hexdigest(), pages_dir)
    except PlanUnavailable:
        raise
    except OSError as exc:
        raise PlanUnavailable(f"the retirement pages cannot be written ({type(exc).__name__})") from None


def _fsync(directory: pathlib.Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def finalize_retirement_plan(plan: RetirementPlan, output_final: dict) -> dict:
    """FinalPlanV1: the immutable prelaunch descriptor plus the ONE designated output slot's final observation."""
    return {"version": 1, "base_plan": plan.descriptor, "base_plan_sha256": plan.descriptor_sha256,
            "output_slot_final": dict(output_final)}


@dataclasses.dataclass(frozen=True)
class ValidatedPlan:
    final: dict
    entries: tuple[dict, ...]  # in removal order (child before parent)
    parent: pathlib.Path


def _uint64(value) -> bool:
    return type(value) is int and 0 <= value <= UINT64


def validate_retirement_plan(final: dict, parent: pathlib.Path, pages_dir: pathlib.Path, *,
                             removed: frozenset = frozenset()) -> ValidatedPlan:
    """Read EVERY linked page (exact length/hash, schema, ordinals, linkage, order, uniqueness, allocation,
    totals, scan digest) and enumerate the whole current container; any extra, missing (without recorded
    progress), changed or unowned entry raises Retained before the first deletion."""
    descriptor = final.get("base_plan") or {}
    if (hashlib.sha256(_canonical(descriptor)).hexdigest() != final.get("base_plan_sha256")
            or descriptor.get("version") != 1
            or not all(_uint64(descriptor.get(name)) for name in ("page_count", "total_entries", "total_page_bytes"))):
        raise Retained("the retirement descriptor is malformed or does not match its sha256")
    pages, link, total_bytes = [], descriptor["head"], 0
    for expected_ordinal in range(descriptor["page_count"] - 1, -1, -1):
        if not isinstance(link, dict) or not isinstance(link.get("sha256"), str) or not _uint64(link.get("bytes")):
            raise Retained("a page link is missing or malformed")
        path = pathlib.Path(pages_dir) / f"{link['sha256']}.json"
        try:
            descriptor_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(descriptor_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size != link["bytes"] or info.st_size > PAGE_BYTES:
                    raise Retained(f"page {link['sha256']} has the wrong size or type")
                data = b""
                while chunk := os.read(descriptor_fd, 1 << 20):
                    data += chunk
            finally:
                os.close(descriptor_fd)
        except OSError:
            raise Retained(f"page {link['sha256']} is missing or unreadable") from None
        if hashlib.sha256(data).hexdigest() != link["sha256"] or len(data) != link["bytes"]:
            raise Retained(f"page {link['sha256']} was changed")
        try:
            page = json.loads(data)
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
        pages.append(page)
        link = page["previous"]
    pages.reverse()
    entries = [item for page in pages for item in page["entries"]]
    scan = hashlib.sha256()
    for left, right in zip(entries, entries[1:]):
        if _order(left) >= _order(right):
            raise Retained("the planned entries are out of order or duplicated")
    for item in entries:
        scan.update(_canonical(item) + b"\n")
    if (len(entries) != descriptor["total_entries"] or total_bytes != descriptor["total_page_bytes"]
            or "sha256:" + scan.hexdigest() != descriptor["complete_scan_digest"]):
        raise Retained("the pages do not hold the complete plan (entry count, byte total or scan digest)")
    slot = tuple((descriptor.get("output_slot") or {}).get("path") or ())
    try:
        current = {tuple(item["path"]): item for item in _census(parent)}
    except PlanUnavailable as exc:
        raise Retained(str(exc)) from None
    planned = {tuple(item["path"]): item for item in entries}
    for path, item in current.items():
        if path == slot:
            final_slot = final.get("output_slot_final") or {}
            if final_slot.get("presence") != "present" or item.get("sha256") != final_slot.get("sha256"):
                raise Retained("the output slot changed after its final capture")
            continue
        expected = planned.get(path)
        if expected is None:
            raise Retained(f"an unplanned entry exists ({'/'.join(path)[:80]})")
        for name in ("type", "mode", "size", "sha256", "object"):
            if expected.get(name) != item.get(name):
                raise Retained(f"{'/'.join(path)[:80]} changed ({name})")
    for path in planned:
        if path not in current and path not in removed:
            raise Retained(f"a planned entry is missing without recorded progress ({'/'.join(path)[:80]})")
    return ValidatedPlan(final, tuple(item for item in entries if tuple(item["path"]) not in removed),
                         pathlib.Path(parent))


def _guarded_remove(parent: pathlib.Path, plan: ValidatedPlan, progress) -> bool:
    if pathlib.Path(parent) != plan.parent or progress is None:
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    slot = tuple((plan.final["base_plan"].get("output_slot") or {}).get("path") or ())
    try:
        root = os.open(str(parent), flags)
    except OSError:
        return False
    try:
        if slot and (plan.final.get("output_slot_final") or {}).get("presence") == "present":
            entries = [{"path": list(slot), "type": "file", "slot": True}, *plan.entries]
        else:
            entries = list(plan.entries)
        for item in entries:
            *folders, name = item["path"]
            directory = root
            opened = []
            try:
                for folder in folders:
                    directory = os.open(folder, flags, dir_fd=directory)
                    opened.append(directory)
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not item.get("slot") and _object_digest(info) != item["object"]:
                    raise Retained(f"{'/'.join(item['path'])[:80]} was replaced")
                if progress(item, "intent") is False:
                    raise Retained("the delete intent could not be recorded")
                if item["type"] == "dir":
                    os.rmdir(name, dir_fd=directory)
                else:
                    os.unlink(name, dir_fd=directory)
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
    except (OSError, Retained):
        return False
    finally:
        os.close(root)
    entry = {"path": [], "type": "root"}
    if progress(entry, "intent") is False:
        return False
    try:
        os.rmdir(parent)
    except OSError:
        return False
    return progress(entry, "absent") is not False and not os.path.lexists(parent)
