# 안전한 출력 파일 — 검증한 저장소 루트 아래 이미 있는 평범한 디렉터리에, 링크·재분석 지점·기존 파일을 거부하고 고정한 디렉터리 기술자로만 새 정규 파일을 만든다.
"""Guarded create-only output for `--out FILE` (stdout stays the default).

The destination is a new regular file beneath the verified repository root, in a directory that already exists.
The lexical path is checked first (no traversal, no absolute path outside the root, no drive, device, UNC, reserved
Windows name, alternate stream, trailing dot or space, control character or case collision), then every component
from the root anchor down is inspected with lstat and opened as a directory descriptor with O_NOFOLLOW whose identity
must match. The bytes go to a random temporary file created O_EXCL|O_NOFOLLOW through the final parent descriptor,
are fsynced, and appear under the final name through a no-replace hard link on the same descriptor; a renamed or
swapped path cannot redirect the write. Native Windows refuses with OAH_MATERIALS_OUTPUT_UNVERIFIABLE until its
handle-based path has platform evidence. Validation failures create nothing; cleanup only removes this call's own
temporary file through the pinned parent.
"""
from __future__ import annotations

import errno
import os
import pathlib
import secrets
import stat
import unicodedata
from typing import Callable

COMPONENT_BYTES = 255
PATH_BYTES = 1024
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10)),
             "conin$", "conout$"}
EXISTS_CODE = "OAH_MATERIALS_OUTPUT_EXISTS"
PATH_INVALID_CODE = "OAH_MATERIALS_OUTPUT_PATH_INVALID"
UNVERIFIABLE_CODE = "OAH_MATERIALS_OUTPUT_UNVERIFIABLE"


class OutputError(Exception):
    def __init__(self, code: str, reason: str, message: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {message} ({reason})")


def _invalid(reason: str, message: str) -> OutputError:
    return OutputError(PATH_INVALID_CODE, reason, message)


def _no_fault(step: str) -> None:
    return None


def components(root: pathlib.Path, name: str) -> list[str]:
    """The lexical components of `name` below `root`; OutputError(PATH_INVALID) before anything is opened."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise _invalid("empty", "give --out a file name")
    if name.startswith(("\\\\", "//")) or name.startswith("\\\\?\\") or name.startswith("\\\\.\\"):
        raise _invalid("unc-or-device", "UNC and device paths are not output destinations")
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        raise _invalid("drive", "drive-letter paths are not output destinations; give a path inside the repository")
    if "\\" in name:
        raise _invalid("backslash", "use / between folders")
    rest = name
    if name.startswith("/"):
        anchor = root.as_posix().rstrip("/") + "/"
        if not name.startswith(anchor):
            raise _invalid("outside-root", "an absolute --out must be inside this repository")
        rest = name[len(anchor):]
    parts = rest.split("/")
    out = []
    for part in parts:
        if part in ("", "."):
            raise _invalid("empty-component", "--out has an empty or '.' folder name")
        if part == "..":
            raise _invalid("traversal", "--out may not use '..'")
        if any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) in ("Cf", "Zl", "Zp") for c in part):
            raise _invalid("control-character", "--out holds control or format characters")
        if ":" in part:
            raise _invalid("alternate-stream", "':' in a file name is a Windows stream or drive")
        if part[-1] in ". ":
            raise _invalid("trailing-dot-or-space", "a name ending in '.' or ' ' is not portable")
        if part.split(".", 1)[0].casefold() in _RESERVED:
            raise _invalid("reserved-name", f"{part!r} is a reserved Windows device name")
        if len(part.encode("utf-8")) > COMPONENT_BYTES - 32:
            raise _invalid("too-long", "a name in --out is too long")
        out.append(part)
    if len((root.as_posix() + "/" + "/".join(out)).encode("utf-8")) > PATH_BYTES - 64:
        raise _invalid("too-long", "the --out path is too long")
    return out


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_case(fd: int, part: str) -> None:
    names = os.listdir(fd)
    if part not in names and any(item.casefold() == part.casefold() for item in names):
        raise _invalid("case-collision", f"{part!r} differs only in case from an existing name")
    if part in names and sum(1 for item in names if item.casefold() == part.casefold()) > 1:
        raise _invalid("case-collision", f"{part!r} is ambiguous with another name that differs only in case")


def write_new(root: os.PathLike | str, name: str, data: bytes, *, fault: Callable[[str], None] = _no_fault,
              windows: bool | None = None) -> pathlib.Path:
    """Create `name` (relative to the verified `root`) holding `data`; the final path of any existing type refuses."""
    root = pathlib.Path(root)
    parts = components(root, name)
    if (os.name == "nt") if windows is None else windows:
        raise OutputError(UNVERIFIABLE_CODE, "windows-handles-unqualified", "--out is not verified on native Windows "
                          "yet; print to stdout and redirect, or save with oah design save from a file you create")
    needed = (os.open, os.link, os.unlink, os.stat)
    if not all(item in os.supports_dir_fd for item in needed) or os.listdir not in os.supports_fd \
            or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OutputError(UNVERIFIABLE_CODE, "no-dir-fd", "this platform cannot pin directories for --out")
    directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        anchor = os.lstat(root)
    except OSError:
        raise _invalid("root-unreadable", "the repository root cannot be inspected") from None
    if stat.S_ISLNK(anchor.st_mode) or not stat.S_ISDIR(anchor.st_mode):
        raise _invalid("link", "the repository root is reached through a link")
    try:
        fd = os.open(root, directory)
    except OSError:
        raise OutputError(UNVERIFIABLE_CODE, "root-open-failed", "the repository root cannot be opened") from None
    opened = [fd]
    try:
        if _identity(os.fstat(fd)) != _identity(anchor):
            raise OutputError(UNVERIFIABLE_CODE, "identity-changed", "the repository root changed while it was opened")
        for part in parts[:-1]:
            _check_case(fd, part)
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise _invalid("missing-parent", f"folder {part!r} does not exist; create it first") from None
            if stat.S_ISLNK(info.st_mode):
                raise _invalid("link", f"folder {part!r} is a link")
            if not stat.S_ISDIR(info.st_mode):
                raise _invalid("not-directory", f"{part!r} is not a folder")
            try:
                child = os.open(part, directory, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _invalid("link", f"folder {part!r} changed into a link or file") from None
                raise OutputError(UNVERIFIABLE_CODE, "parent-open-failed", f"folder {part!r} cannot be opened") \
                    from None
            opened.append(child)
            if _identity(os.fstat(child)) != _identity(info):
                raise OutputError(UNVERIFIABLE_CODE, "identity-changed", f"folder {part!r} changed while it was "
                                  "opened")
            fd = child
        final = parts[-1]
        _check_case(fd, final)
        try:
            os.stat(final, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OutputError(EXISTS_CODE, "final-exists", f"{name} already exists; choose a new file name")
        fault("parent-pinned")
        temp = f".{final}.oah-{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            handle = os.open(temp, flags, 0o644, dir_fd=fd)
        except OSError:
            raise OutputError(UNVERIFIABLE_CODE, "temporary-create-failed", "the temporary file cannot be created") \
                from None
        try:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(handle, view)
                    view = view[written:]
                os.fsync(handle)
            finally:
                os.close(handle)
            fault("temporary-written")
            try:
                os.link(temp, final, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                raise OutputError(EXISTS_CODE, "final-exists", f"{name} appeared while writing; it was not "
                                  "replaced") from None
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass
        os.fsync(fd)
        return root.joinpath(*parts)
    finally:
        for item in reversed(opened):
            os.close(item)
