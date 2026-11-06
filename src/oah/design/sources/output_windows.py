# Windows 출력 — 재분석 없는 고정 디렉터리 핸들과 소유 파일 핸들로 생성·쓰기·이름 변경·실패 정리를 수행한다.
from __future__ import annotations

import ctypes
from ctypes import wintypes
import dataclasses
import ntpath
import os
from pathlib import Path
import secrets
from typing import Callable

from oah.design.sources.output import EXISTS_CODE, PATH_INVALID_CODE, UNVERIFIABLE_CODE, OutputError

REPARSE = 0x400
DIRECTORY = 0x10


@dataclasses.dataclass(frozen=True)
class Info:
    path: str
    volume: int
    file_id: bytes
    attributes: int


class Native:
    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise OutputError(UNVERIFIABLE_CODE, "windows-api-unavailable", "native Windows handle APIs are unavailable")
        try:
            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            signatures = {
                "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
                "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
                "GetFileType": ([wintypes.HANDLE], wintypes.DWORD),
                "GetFileInformationByHandleEx": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                   wintypes.DWORD], wintypes.BOOL),
                "GetFinalPathNameByHandleW": ([wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                                                wintypes.DWORD], wintypes.DWORD),
                "GetVolumeInformationByHandleW": ([wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                    wintypes.LPWSTR, wintypes.DWORD], wintypes.BOOL),
                "GetDriveTypeW": ([wintypes.LPCWSTR], wintypes.UINT),
                "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                 ctypes.c_void_p], wintypes.BOOL),
                "FlushFileBuffers": ([wintypes.HANDLE], wintypes.BOOL),
                "SetFileInformationByHandle": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wintypes.DWORD], wintypes.BOOL),
            }
            for name, (arguments, result) in signatures.items():
                function = getattr(self.api, name)
                function.argtypes, function.restype = arguments, result
        except (AttributeError, OSError):
            raise OutputError(UNVERIFIABLE_CODE, "windows-api-unavailable", "required Windows handle APIs are unavailable") from None

    def _ok(self, success: object) -> None:
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    def _open(self, path: str, *, directory: bool) -> int:
        access = 0x81 if directory else 0xC0010000
        flags = 0x02200000 if directory else 0x00200080
        share = 3 if directory else 1
        handle = self.api.CreateFileW(path, access, share, None, 3 if directory else 1, flags, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def open_directory(self, path: str) -> int:
        return self._open(path, directory=True)

    def create_file(self, path: str) -> int:
        return self._open(path, directory=False)

    def info(self, handle: int) -> Info:
        class AttributeTag(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

        class FileId(ctypes.Structure):
            _fields_ = [("volume", ctypes.c_ulonglong), ("identifier", ctypes.c_ubyte * 16)]

        if self.api.GetFileType(handle) != 1:
            raise OutputError(UNVERIFIABLE_CODE, "not-disk", "output handles must belong to a local disk")
        attributes, identity = AttributeTag(), FileId()
        self._ok(self.api.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)))
        self._ok(self.api.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)))
        buffer = ctypes.create_unicode_buffer(2048)
        size = self.api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not 0 < size < len(buffer):
            raise OutputError(UNVERIFIABLE_CODE, "handle-path-unavailable", "the final handle path cannot be inspected")
        path = buffer.value
        if path.startswith("\\\\?\\"):
            path = path[4:]
        return Info(path, identity.volume, bytes(identity.identifier), attributes.attributes)

    def volume(self, handle: int) -> None:
        name, filesystem = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(64)
        serial, component, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        self._ok(self.api.GetVolumeInformationByHandleW(handle, name, len(name), ctypes.byref(serial),
            ctypes.byref(component), ctypes.byref(flags), filesystem, len(filesystem)))
        drive = ntpath.splitdrive(self.info(handle).path)[0]
        if filesystem.value.upper() not in ("NTFS", "REFS") or not drive or self.api.GetDriveTypeW(drive + "\\") != 3:
            raise OutputError(UNVERIFIABLE_CODE, "filesystem-unsupported", "output requires a local NTFS or ReFS disk "
                              "with persistent handle identities and sharing enforcement")

    def names(self, handle: int) -> list[str]:
        return os.listdir(self.info(handle).path)

    def write(self, handle: int, data: bytes) -> None:
        for offset in range(0, len(data), 65536):
            chunk = data[offset:offset + 65536]
            buffer = ctypes.create_string_buffer(chunk)
            position = 0
            while position < len(chunk):
                written = wintypes.DWORD()
                self._ok(self.api.WriteFile(handle, ctypes.byref(buffer, position), len(chunk) - position,
                                           ctypes.byref(written), None))
                if not 0 < written.value <= len(chunk) - position:
                    raise OutputError(UNVERIFIABLE_CODE, "write-failed", "the handle write did not advance")
                position += written.value

    def flush(self, handle: int) -> None:
        self._ok(self.api.FlushFileBuffers(handle))

    def rename(self, handle: int, destination: str) -> None:
        class Rename(ctypes.Structure):
            _fields_ = [("replace", wintypes.BOOLEAN), ("root", wintypes.HANDLE),
                        ("length", wintypes.DWORD), ("name", wintypes.WCHAR * 1)]

        name = destination.encode("utf-16-le")
        size = Rename.name.offset + len(name)
        buffer = ctypes.create_string_buffer(max(size, ctypes.sizeof(Rename)))
        information = Rename.from_buffer(buffer)
        information.replace, information.root, information.length = False, None, len(name)
        ctypes.memmove(ctypes.addressof(buffer) + Rename.name.offset, name, len(name))
        self._ok(self.api.SetFileInformationByHandle(handle, 3, buffer, size))

    def discard(self, handle: int) -> None:
        delete = wintypes.BOOLEAN(True)
        self._ok(self.api.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)))

    def close(self, handle: int) -> None:
        self.api.CloseHandle(handle)


def _normal(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _inspect(api: Native, handle: int, path: Path, *, directory: bool, volume: int | None) -> Info:
    info = api.info(handle)
    if info.attributes & REPARSE:
        raise OutputError(PATH_INVALID_CODE, "link", "an output component is a reparse point")
    if bool(info.attributes & DIRECTORY) != directory:
        raise OutputError(PATH_INVALID_CODE, "not-directory" if directory else "not-regular", "the handle has an "
                          "unexpected file type")
    if _normal(info.path) != _normal(str(path)) or not any(info.file_id) or (volume is not None and info.volume != volume):
        raise OutputError(UNVERIFIABLE_CODE, "identity-changed", "the handle path, volume or identity is not the "
                          "requested pinned object")
    return info


def _case(api: Native, handle: int, name: str) -> None:
    names = api.names(handle)
    aliases = [item for item in names if item.casefold() == name.casefold()]
    if aliases and (name not in aliases or len(aliases) > 1):
        raise OutputError(PATH_INVALID_CODE, "case-collision", "the output name collides with another spelling")


def write_new(root: Path, parts: list[str], data: bytes, fault: Callable[[str], None]) -> Path:
    api = Native()
    directories: list[tuple[int, Path, Info]] = []
    temporary, published = None, False
    try:
        path = root
        handle = api.open_directory(str(path))
        try:
            info = _inspect(api, handle, path, directory=True, volume=None)
            api.volume(handle)
        except BaseException:
            api.close(handle)
            raise
        directories.append((handle, path, info))
        for part in parts[:-1]:
            _case(api, handle, part)
            path = path / part
            child = api.open_directory(str(path))
            try:
                info = _inspect(api, child, path, directory=True, volume=directories[0][2].volume)
            except BaseException:
                api.close(child)
                raise
            directories.append((child, path, info))
            handle = child
        _case(api, handle, parts[-1])
        if parts[-1] in api.names(handle):
            raise OutputError(EXISTS_CODE, "final-exists", "the output already exists; choose a new file name")
        fault("parent-pinned")
        for directory, expected_path, expected_info in directories:
            if _inspect(api, directory, expected_path, directory=True, volume=expected_info.volume) != expected_info:
                raise OutputError(UNVERIFIABLE_CODE, "identity-changed", "a pinned output directory changed")
        temporary_path = path / f".{parts[-1]}.oah-{secrets.token_hex(8)}.tmp"
        temporary = api.create_file(str(temporary_path))
        owned = _inspect(api, temporary, temporary_path, directory=False, volume=directories[0][2].volume)
        api.write(temporary, data)
        api.flush(temporary)
        fault("temporary-written")
        for directory, expected_path, expected_info in directories:
            if _inspect(api, directory, expected_path, directory=True, volume=expected_info.volume) != expected_info:
                raise OutputError(UNVERIFIABLE_CODE, "identity-changed", "a pinned output directory changed")
        destination = path / parts[-1]
        api.rename(temporary, str(destination))
        published = True
        actual = _inspect(api, temporary, destination, directory=False, volume=owned.volume)
        if actual.file_id != owned.file_id:
            raise OutputError(UNVERIFIABLE_CODE, "identity-changed", "the published handle no longer names our file")
        return destination
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        if code in (80, 183):
            raise OutputError(EXISTS_CODE, "final-exists", "the output appeared while writing; it was not replaced") from None
        if code in (2, 3):
            raise OutputError(PATH_INVALID_CODE, "missing-parent", "an output parent does not exist") from None
        raise OutputError(UNVERIFIABLE_CODE, "windows-handle-refused", "Windows cannot establish the required "
                          "handle, filesystem or sharing guarantees") from None
    finally:
        try:
            if temporary is not None and not published:
                try:
                    api.discard(temporary)
                except OSError:
                    raise OutputError(UNVERIFIABLE_CODE, "cleanup-refused", "Windows refused cleanup through the "
                                      "owned file handle") from None
        finally:
            if temporary is not None:
                api.close(temporary)
            for handle, _path, _info in reversed(directories):
                api.close(handle)
