# 설계 파일 접근 — ID 로만 경로를 조립하고, 링크·재분석 지점·저장소 밖 경로·긴 Windows 경로를 거부하며, 작업 트리와 git ref 를 같은 읽기 규약으로 읽는다.
"""Paths and reads for design records and pages.

Every path is built from validated IDs under two fixed roots: `.oah/designs` (the records) and `<wiki>/designs`
(the pages). Below those roots no existing component may be a symbolic link, a junction or another reparse point,
and the resolved path must stay inside the repository. Reads use `lstat`, `O_NOFOLLOW` and a same-file check, like
`oah.intent.read_source`. A reader is anything with `read(rel)`, `read_many(rels)` and `list_dir(rel)` over
repository-relative POSIX paths; `WorktreeReader` reads files and `GitRefReader` reads one committed ref.
"""
from __future__ import annotations

import errno
import os
import pathlib
import stat
import time
from typing import Iterable, Protocol

from oah.design.model import PATH_INVALID_CODE, DesignError

REGISTRY_DIR = ".oah/designs"
PAGES_DIR_NAME = "designs"
RECORD_LIMIT = 4 * 1024 * 1024
PAGE_LIMIT = 32 * 1024 * 1024
TEMP_ROOM = 25  # what the temp file of an atomic write adds to its target's path length
WINDOWS_PATH_LIMIT = 259
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


class RefUnavailable(Exception):
    """The authority ref, or the records on it, cannot be read."""


# Layout -------------------------------------------------------------------------------------------


def project_record_rel(project: str) -> str:
    return f"{REGISTRY_DIR}/{project}/project.json"


def design_dir_rel(project: str, design: str) -> str:
    return f"{REGISTRY_DIR}/{project}/{design}"


def pointer_rel(project: str, design: str) -> str:
    return f"{design_dir_rel(project, design)}/design.json"


def revisions_rel(project: str, design: str) -> str:
    return f"{design_dir_rel(project, design)}/revisions"


def manifest_rel(project: str, design: str, revision: int) -> str:
    return f"{revisions_rel(project, design)}/r{revision}/revision.json"


def part_rel(project: str, design: str, revision: int, part: str) -> str:
    return f"{revisions_rel(project, design)}/r{revision}/{part}.md"


def acceptances_rel(project: str, design: str) -> str:
    return f"{design_dir_rel(project, design)}/acceptances"


def acceptance_rel(project: str, design: str, sequence: int) -> str:
    return f"{acceptances_rel(project, design)}/a{sequence}.json"


def readme_rel() -> str:
    return f"{REGISTRY_DIR}/README.md"


def pages_rel(wiki_dir: str) -> str:
    return (pathlib.PurePosixPath(wiki_dir) / PAGES_DIR_NAME).as_posix()


def project_page_rel(wiki_dir: str, project: str) -> str:
    return f"{pages_rel(wiki_dir)}/{project}.md"


def design_page_rel(wiki_dir: str, project: str, design: str) -> str:
    return f"{pages_rel(wiki_dir)}/{project}/{stem(project, design)}.md"


def stem(project: str, design: str) -> str:
    """The design page's file name stem; `--` never comes out of `oah wiki new`'s slugs."""
    return f"{project}--{design}"


def revision_number(name: str) -> int | None:
    """N for a folder named r<N>, else None."""
    if len(name) > 1 and name[0] == "r" and name[1:].isdigit() and name[1] != "0":
        return int(name[1:])
    return None


def acceptance_number(name: str) -> int | None:
    """K for a file named a<K>.json, else None."""
    if name.startswith("a") and name.endswith(".json"):
        digits = name[1:-5]
        if digits.isdigit() and digits[0] != "0":
            return int(digits)
    return None


def design_output_paths(settings) -> tuple[str, ...]:
    """What a design write may change, as repository-relative prefixes (folders end with '/') and files.

    `settings` is a WikiSettings already resolved with `for_root`. This never touches the file system; lane B uses
    it to decide how `oah finish` treats these paths.
    """
    wiki = pathlib.PurePosixPath(settings.dir)
    return (f"{REGISTRY_DIR}/", f"{pages_rel(settings.dir)}/", (wiki / "index.md").as_posix(),
            (wiki / "log.md").as_posix())


def is_design_output(settings, rel: str) -> bool:
    key = rel.replace("\\", "/").casefold()
    for entry in design_output_paths(settings):
        entry = entry.casefold()
        if (key.startswith(entry) and entry.endswith("/")) or key == entry:
            return True
    return False


# Checks -------------------------------------------------------------------------------------------


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def windows_limit() -> int | None:
    """The longest path a write may use on this machine; None when no limit applies."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        if ctypes.windll.ntdll.RtlAreLongPathsEnabled():
            return None
    except (AttributeError, OSError):
        pass
    return WINDOWS_PATH_LIMIT


def guarded_path(root: pathlib.Path, rel: str, guard: str, *, writing: bool = False,
                 limit: int | None = None) -> pathlib.Path:
    """`root / rel`, refusing links at or below `guard` (a prefix of rel) and paths that leave the repository.

    With `writing`, a path whose longest temp file would pass the Windows limit is refused as well.
    """
    root = pathlib.Path(root)
    pure = pathlib.PurePosixPath(rel)
    guard_parts = pathlib.PurePosixPath(guard).parts
    if pure.parts[:len(guard_parts)] != guard_parts or ".." in pure.parts or pure.is_absolute():
        raise DesignError(PATH_INVALID_CODE, f"{rel} is not below {guard}")
    target = root.joinpath(*pure.parts)
    try:
        resolved = target.resolve(strict=False)
        inside = resolved.is_relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError):
        inside = False
    if not inside:
        raise DesignError(PATH_INVALID_CODE, f"{rel} resolves outside the repository; replace the link on its way "
                          "with a plain folder, then retry")
    current = root.joinpath(*guard_parts[:-1]) if len(guard_parts) > 1 else root
    for part in pure.parts[len(guard_parts) - 1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            break
        except OSError as exc:
            raise DesignError(PATH_INVALID_CODE, f"cannot inspect {current}: {exc}") from exc
        if _is_link(info):
            shown = current.relative_to(root).as_posix()
            raise DesignError(PATH_INVALID_CODE, f"{shown} is a link or junction; oah writes design files only "
                              "through plain folders. Replace it with a folder, then retry")
    if writing:
        limit = windows_limit() if limit is None else limit
        if limit is not None and len(str(target)) + TEMP_ROOM > limit:
            raise DesignError(PATH_INVALID_CODE, f"{target} is {len(str(target))} characters; with the temporary "
                              f"file oah writes beside it, it passes the {limit}-character Windows path limit. Move "
                              "the repository to a shorter folder, use shorter project and design IDs, or enable "
                              "long paths in Windows")
    return target


# Reads --------------------------------------------------------------------------------------------


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    if (a.st_size, a.st_mtime_ns, stat.S_IFMT(a.st_mode)) != (b.st_size, b.st_mtime_ns, stat.S_IFMT(b.st_mode)):
        return False
    return not (a.st_ino and b.st_ino and (a.st_ino, a.st_dev) != (b.st_ino, b.st_dev))


def _read_once(path: pathlib.Path, limit: int) -> bytes | None:
    try:
        before = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise DesignError(PATH_INVALID_CODE, f"cannot inspect {path}: {exc}") from exc
    if _is_link(before):
        raise DesignError(PATH_INVALID_CODE, f"{path} is a link; oah reads design files only as plain files. Replace "
                          "it with the real file, then retry")
    if not stat.S_ISREG(before.st_mode):
        raise DesignError(PATH_INVALID_CODE, f"{path} is not a regular file")
    if before.st_size > limit:
        raise DesignError(PATH_INVALID_CODE, f"{path} is {before.st_size} bytes, over the {limit}-byte limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise _Changed() from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DesignError(PATH_INVALID_CODE, f"{path} became a link while it was read") from exc
        raise DesignError(PATH_INVALID_CODE, f"cannot read {path}: {exc}") from exc
    try:
        if not _same(before, os.fstat(fd)):
            raise _Changed()
        chunks, remaining = [], limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        again = os.lstat(path)
    except OSError:
        raise _Changed() from None
    if len(data) != before.st_size or not _same(before, after) or not _same(before, again):
        raise _Changed()
    return data


class _Changed(Exception):
    pass


def read_file(path: pathlib.Path, *, limit: int = RECORD_LIMIT) -> bytes | None:
    """A plain file's bytes, None when absent; links and files that keep changing are refused."""
    for _ in range(20):
        try:
            return _read_once(pathlib.Path(path), limit)
        except _Changed:
            time.sleep(0.02)
    from oah.wiki.base import WikiError

    raise WikiError("OAH_WIKI_BUSY", f"{path} kept changing while oah read it; wait for the other writer, then retry")


def list_names(path: pathlib.Path) -> list[str] | None:
    """The entry names of a plain folder, None when it is absent or not a folder."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise DesignError(PATH_INVALID_CODE, f"cannot inspect {path}: {exc}") from exc
    if _is_link(info):
        raise DesignError(PATH_INVALID_CODE, f"{path} is a link or junction; replace it with a plain folder")
    if not stat.S_ISDIR(info.st_mode):
        return None
    try:
        return sorted(entry.name for entry in os.scandir(path))
    except OSError as exc:
        raise DesignError(PATH_INVALID_CODE, f"cannot list {path}: {exc}") from exc


class Reader(Protocol):
    def read(self, rel: str) -> bytes | None: ...

    def read_many(self, rels: Iterable[str]) -> dict[str, bytes | None]: ...

    def list_dir(self, rel: str) -> list[str] | None: ...


class WorktreeReader:
    """Reads the files of one worktree, refusing links below `.oah/designs` and `<wiki>/designs`."""

    def __init__(self, root: pathlib.Path, wiki_dir: str | None = None) -> None:
        self.root = pathlib.Path(root)
        self.guards = [REGISTRY_DIR] + ([pages_rel(wiki_dir)] if wiki_dir is not None else [])

    def _path(self, rel: str) -> pathlib.Path:
        parts = pathlib.PurePosixPath(rel).parts
        for guard in self.guards:
            guard_parts = pathlib.PurePosixPath(guard).parts
            if parts[:len(guard_parts)] == guard_parts:
                return guarded_path(self.root, rel, guard)
        raise DesignError(PATH_INVALID_CODE, f"{rel} is not a design path")

    def read(self, rel: str, *, limit: int = RECORD_LIMIT) -> bytes | None:
        return read_file(self._path(rel), limit=limit)

    def read_many(self, rels: Iterable[str]) -> dict[str, bytes | None]:
        return {rel: self.read(rel) for rel in rels}

    def list_dir(self, rel: str) -> list[str] | None:
        return list_names(self._path(rel))


class GitRefReader:
    """Reads the records under `prefix` as committed on `ref`, with `git ls-tree` and `git cat-file --batch`."""

    def __init__(self, root: pathlib.Path, ref: str, prefix: str = REGISTRY_DIR) -> None:
        from oah.paths import GitMissing, run_git

        self.root = pathlib.Path(root)
        self.ref = ref
        try:
            resolved = run_git(self.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except GitMissing as exc:
            raise RefUnavailable(str(exc)) from exc
        commit = resolved.stdout.strip()
        if resolved.returncode != 0 or not commit:
            raise RefUnavailable(f"{ref} does not name a commit here")
        self.commit = commit
        listed = self._git("ls-tree", "-r", "-z", "--full-tree", "--name-only", commit, "--", prefix)
        self.files = {item.decode("utf-8", "surrogateescape") for item in listed.split(b"\0") if item}
        self._cache: dict[str, bytes] = {}

    def _git(self, *args: str, stdin: bytes | None = None) -> bytes:
        from oah.engines.proc import run_command
        from oah.paths import GIT_TIMEOUT, GitMissing, git_argv

        try:
            result = run_command(git_argv() + ("-C", str(self.root), *args), timeout=GIT_TIMEOUT,
                                 encoding="latin-1", stdin_text=None if stdin is None else stdin.decode("latin-1"))
        except GitMissing as exc:
            raise RefUnavailable(str(exc)) from exc
        if result.returncode != 0:
            raise RefUnavailable(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.encode("latin-1")

    def read(self, rel: str) -> bytes | None:
        return self.read_many([rel])[rel]

    def read_many(self, rels: Iterable[str]) -> dict[str, bytes | None]:
        rels = list(rels)
        wanted = [rel for rel in rels if rel in self.files and rel not in self._cache and "\n" not in rel]
        if wanted:
            out = self._git("cat-file", "--batch",
                            stdin=b"".join(f"{self.commit}:{rel}\n".encode("utf-8") for rel in wanted))
            position = 0
            for rel in wanted:
                end = out.index(b"\n", position)
                header = out[position:end].split(b" ")
                position = end + 1
                if len(header) != 3 or header[1] != b"blob":
                    raise RefUnavailable(f"{self.ref}:{rel} is not a file")
                size = int(header[2])
                if size > PAGE_LIMIT:
                    raise RefUnavailable(f"{self.ref}:{rel} is too large")
                self._cache[rel] = out[position:position + size]
                position += size + 1
        return {rel: self._cache.get(rel) for rel in rels}

    def list_dir(self, rel: str) -> list[str] | None:
        prefix = rel.rstrip("/") + "/"
        names = {name[len(prefix):].split("/", 1)[0] for name in self.files if name.startswith(prefix)}
        return sorted(names) if names else None
