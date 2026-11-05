# 안전한 출력 파일 — 검증한 저장소 루트 아래 이미 있는 평범한 디렉터리에, 링크·재분석 지점·기존 파일을 거부하고 고정한 디렉터리 기술자로만 새 정규 파일을 만든다.
"""Guarded create-only output for `--out FILE` (stdout stays the default).

The destination is a new regular file beneath the verified repository root, in a directory that already exists.
The lexical path is checked first (no traversal, no absolute path outside the root, no drive, device, UNC, reserved
Windows name, alternate stream, trailing dot or space, control character or case collision), then every component
from the root anchor down is inspected with lstat and opened as a directory descriptor with O_NOFOLLOW whose identity
must match. The bytes go to an anonymous staging object, are fsynced, and appear under the final name through an
operation on the owned file descriptor: an APFS
clone on macOS or an anonymous-file hard link on Linux. Cleanup closes that descriptor and never unlinks a public
temporary name. Filesystems without a supported object-bound operation refuse. Native Windows uses pinned handles
and handle-based rename and cleanup. Validation failures create nothing.
"""
from __future__ import annotations

import errno
import contextlib
import ctypes
import os
import pathlib
import stat
import sys
import unicodedata
from typing import Callable, Iterator

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
    drive = len(name) >= 2 and name[1] == ":" and name[0].isalpha()
    if drive and (name[2:3] != "/" or not name.startswith(root.as_posix().rstrip("/") + "/")):
        raise _invalid("drive", "a drive path must be absolute and inside this repository")
    if "\\" in name:
        raise _invalid("backslash", "use / between folders")
    rest = name
    if name.startswith("/") or drive:
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


def _directory_path(fd: int) -> pathlib.Path:
    try:
        if sys.platform == "darwin":
            import fcntl

            name = os.fsdecode(fcntl.fcntl(fd, 50, bytes(PATH_BYTES)).split(b"\0", 1)[0])
        elif sys.platform.startswith("linux"):
            name = os.readlink(f"/proc/self/fd/{fd}")
        else:
            raise OSError(errno.ENOTSUP, "directory handle path is unavailable")
        path = pathlib.Path(name)
        if _identity(os.stat(path, follow_symlinks=False)) != _identity(os.fstat(fd)):
            raise OSError(errno.ESTALE, "directory handle moved")
        return path
    except OSError:
        raise OutputError(UNVERIFIABLE_CODE, "directory-path-unavailable", "the pinned directory's current path "
                          "cannot be established") from None


@contextlib.contextmanager
def _anonymous(parent: int) -> Iterator[int]:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            libc.tmpfile.argtypes = []
            libc.tmpfile.restype = ctypes.c_void_p
            libc.fileno.argtypes = [ctypes.c_void_p]
            libc.fileno.restype = ctypes.c_int
            libc.fclose.argtypes = [ctypes.c_void_p]
            libc.fclose.restype = ctypes.c_int
        except AttributeError:
            raise OutputError(UNVERIFIABLE_CODE, "anonymous-file-unavailable", "anonymous staging is unavailable") from None
        stream = libc.tmpfile()
        if not stream:
            raise OutputError(UNVERIFIABLE_CODE, "anonymous-file-unavailable", "anonymous staging cannot be created")
        try:
            fd = libc.fileno(stream)
            if fd < 0:
                raise OutputError(UNVERIFIABLE_CODE, "anonymous-file-unavailable", "anonymous staging has no descriptor")
            yield fd
        finally:
            libc.fclose(stream)
    elif sys.platform.startswith("linux") and hasattr(os, "O_TMPFILE"):
        try:
            fd = os.open(".", os.O_RDWR | os.O_TMPFILE | getattr(os, "O_CLOEXEC", 0), 0o644, dir_fd=parent)
        except OSError:
            raise OutputError(UNVERIFIABLE_CODE, "anonymous-file-unavailable", "this filesystem cannot create "
                              "anonymous staging") from None
        try:
            yield fd
        finally:
            os.close(fd)
    else:
        raise OutputError(UNVERIFIABLE_CODE, "anonymous-file-unavailable", "this platform has no supported anonymous "
                          "staging operation")


def _publish(handle: int, parent: int, final: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == "darwin":
            operation = libc.fclonefileat
            operation.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            operation.restype = ctypes.c_int
            result = operation(handle, parent, os.fsencode(final), 0)
        else:
            operation = libc.linkat
            operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            operation.restype = ctypes.c_int
            result = operation(handle, b"", parent, os.fsencode(final), 0x1000)
            if result and ctypes.get_errno() in (errno.EPERM, errno.EINVAL, errno.ENOENT):
                result = operation(-100, os.fsencode(f"/proc/self/fd/{handle}"), parent, os.fsencode(final), 0x400)
        if result:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise OutputError(EXISTS_CODE, "final-exists", "the output appeared while writing; it was not replaced")
            raise OutputError(UNVERIFIABLE_CODE, "object-publication-unavailable", "the filesystem cannot publish "
                              "the owned staging object without replacement")
    except AttributeError:
        raise OutputError(UNVERIFIABLE_CODE, "object-publication-unavailable", "the required descriptor publication "
                          "API is unavailable") from None


def write_new(root: os.PathLike | str, name: str, data: bytes, *, fault: Callable[[str], None] = _no_fault,
              windows: bool | None = None) -> pathlib.Path:
    """Create `name` (relative to the verified `root`) holding `data`; the final path of any existing type refuses."""
    root = pathlib.Path(root)
    parts = components(root, name)
    if (os.name == "nt") if windows is None else windows:
        from oah.design.sources import output_windows

        return output_windows.write_new(root, parts, data, fault)
    needed = (os.open, os.stat)
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
        _directory_path(fd)
        with _anonymous(fd) as handle:
            os.fchmod(handle, 0o644)
            view = memoryview(data)
            while view:
                written = os.write(handle, view)
                if written <= 0:
                    raise OutputError(UNVERIFIABLE_CODE, "write-failed", "the staging write did not advance")
                view = view[written:]
            os.fsync(handle)
            fault("temporary-written")
            destination = _directory_path(fd)
            try:
                destination.relative_to(_directory_path(opened[0]))
            except ValueError:
                raise OutputError(UNVERIFIABLE_CODE, "parent-moved-outside", "the pinned parent moved outside "
                                  "the repository") from None
            _publish(handle, fd, final)
        os.fsync(fd)
        return destination / final
    finally:
        for item in reversed(opened):
            os.close(item)
