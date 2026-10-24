# 리뷰 스냅샷 — 저장소를 --no-local 로 복제해 head 에 detached 로 두고 원격을 지운 뒤, 에이전트 지시 파일을 모든 깊이에서 대소문자 무시로 걷어 내며, 끝나면 실패해도 지운다.
"""The sanitized tree a reviewer works in (PLAN 5.9 step 3).

`make(source, head, base=...)` clones the source repository with `git clone --no-local --no-checkout
--template=` into a fresh temporary folder, checks `head` out detached, removes the `origin` remote and
deletes every tracked agent instruction path from the working tree: files named like `STRIPPED_FILES`
and everything under folders named like `STRIPPED_DIRS`, at any depth and in any letter case (a
case-insensitive file system loads `agents.md` as `AGENTS.md`). The index keeps them, so
`git show <head>:<path>` still reads them. Hooks never run: the clone copies no template and every git call
sets an empty `core.hooksPath`. Git context variables such as `GIT_DIR` are not passed on.

The temporary folder is removed when the block ends, also on exceptions and interrupts. A folder that
cannot be removed is reported through `warnings` instead of hiding the original error.
"""
from __future__ import annotations

import contextlib
import dataclasses
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
    argv = [*git_argv(), "-c", f"core.hooksPath={hooks}"]
    if cwd is not None:
        argv += ["-C", str(cwd)]
    result = run_command([*argv, *args], timeout=timeout, env=_env())
    if result.timed_out or result.returncode != 0:
        reason = "it timed out" if result.timed_out else (result.stderr.strip() or f"exit {result.returncode}")
        raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", f"git {args[0]} failed while building the review snapshot "
                          f"({reason}). Nothing was reviewed; check that the commits exist, then run oah review again")
    return result.stdout


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


def remove(path: pathlib.Path) -> bool:
    """Remove a folder tree, clearing read-only flags (Windows git objects); True when it is gone."""
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
         warnings: list[str] | None = None) -> Iterator[Snapshot]:
    """A sanitized clone of `source` at `head` for the duration of the block."""
    for name, value in (("head", head), ("base", base)):
        if not _SHA.fullmatch(value or ""):
            raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", f"the {name} {value!r} is not a full commit SHA")
    parent = pathlib.Path(tempfile.mkdtemp(prefix="oah-review-", dir=temp_root))
    try:
        hooks = parent / "no-hooks"
        hooks.mkdir()
        tree = parent / "tree"
        _git(hooks, "clone", "--quiet", "--no-local", "--no-checkout", "--template=", "--", str(source), str(tree),
             timeout=CLONE_TIMEOUT)
        _git(hooks, "checkout", "--quiet", "--detach", head, cwd=tree)
        found = _git(hooks, "rev-parse", "--verify", "HEAD^{commit}", cwd=tree).strip()
        if found != head:
            raise ReviewError("OAH_REVIEW_SNAPSHOT_FAILED", f"the snapshot is at {found}, not the reviewed head {head}")
        _git(hooks, "cat-file", "-e", f"{base}^{{commit}}", cwd=tree)
        _git(hooks, "remote", "remove", "origin", cwd=tree)
        removed = stripped(path for path in _git(hooks, "ls-files", "-z", cwd=tree).split("\0") if path)
        _strip(tree, removed)
        yield Snapshot(tree=tree, head=head, stripped=tuple(removed))
    finally:
        if not remove(parent) and warnings is not None:
            warnings.append(f"OAH_REVIEW_SNAPSHOT_LEFT: the review snapshot {parent} could not be removed; delete it "
                            "by hand once no reviewer process uses it")
