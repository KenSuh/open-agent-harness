# 키 조회의 유일한 경로 — 환경 변수를 먼저 보고, 없으면 macOS 키체인 또는 Windows 자격 증명 관리자에서 실행 시점에만 읽는다.
"""The one documented key lookup (A17).

    oah.secrets.get("TYPESAFE_API_KEY") -> str | None

1. The environment variable of that name, when it is set and not empty.
2. The OS keychain, under service `oah` and account `<name>`:
   - macOS: the built-in `security` tool, run as
     `find-generic-password -s oah -a <name> -w`. Store a key with
     `add-generic-password -s oah -a <name> -w` (it prompts for the value).
   - Windows: Credential Manager, a generic credential with target `oah:<name>`. Store one with
     `cmdkey /generic:oah:<name> /user:oah /pass` (it prompts for the value).
3. Otherwise None. Callers then leave the feature off and report `unused(no key)`.

Keys are never written anywhere by oah, never put in an exception message, and never shown by any
`repr` in this module. Never paste a key into a project file.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Mapping, Protocol

from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command

SERVICE = "oah"
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
MAC_TOOL_PATH = "/usr/bin"
LOOKUP_TIMEOUT = 10.0
CRED_TYPE_GENERIC = 1
ERROR_NOT_FOUND = 1168


class Keychain(Protocol):
    def lookup(self, name: str) -> str | None: ...


def check_name(name: str) -> None:
    """Key names look like environment variable names; anything else is a programming error."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"OAH_SECRET_NAME_INVALID: key names use letters, digits and _ "
                         f"(at most 64), like TYPESAFE_API_KEY; got {name!r}")


def get(name: str, *, env: Mapping[str, str] | None = None, keychain: Keychain | None = None) -> str | None:
    """The key called `name` from the environment, then the OS keychain; None when neither has it."""
    check_name(name)
    environment = os.environ if env is None else env
    value = environment.get(name)
    if value:
        return value
    chain = system_keychain() if keychain is None else keychain
    found = chain.lookup(name)
    return found or None


def system_keychain() -> Keychain:
    if os.name == "nt":
        return WindowsCredentials()
    if sys.platform == "darwin":
        return MacKeychain()
    return NoKeychain()


class NoKeychain:
    """Platforms without a supported keychain: only the environment is read."""

    def lookup(self, name: str) -> str | None:
        return None

    def __repr__(self) -> str:
        return "NoKeychain()"


class MacKeychain:
    """The macOS login keychain through the `security` tool; any failure reads as no key."""

    def __init__(self, search_path: str | None = MAC_TOOL_PATH, timeout: float = LOOKUP_TIMEOUT) -> None:
        self.search_path = search_path
        self.timeout = timeout

    def lookup(self, name: str) -> str | None:
        check_name(name)
        try:
            tool = resolve_command("security", self.search_path)
        except UnsupportedBatchShim:
            return None
        if tool is None:
            return None
        try:
            result = run_command(tool.argv + ("find-generic-password", "-s", SERVICE, "-a", name, "-w"),
                                 timeout=self.timeout)
        except OSError:
            return None
        if result.timed_out or result.returncode != 0:
            return None
        value = result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout
        return value.rstrip("\r") or None

    def __repr__(self) -> str:
        return f"MacKeychain(search_path={self.search_path!r})"


def decode_blob(data: bytes) -> str | None:
    """A Credential Manager blob as text: UTF-16-LE (what cmdkey writes) or UTF-8; None when neither."""
    if not data:
        return None
    candidates: list[str] = []
    if len(data) % 2 == 0 and all(byte == 0 for byte in data[1::2]):
        candidates.append("utf-16-le")
    candidates.append("utf-8")
    if len(data) % 2 == 0:
        candidates.append("utf-16-le")
    for encoding in candidates:
        try:
            text = data.decode(encoding).rstrip("\x00")
        except UnicodeDecodeError:
            continue
        return text or None
    return None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentials:
    """Windows Credential Manager: a generic credential with target `oah:<name>`."""

    def lookup(self, name: str) -> str | None:
        check_name(name)
        if os.name != "nt":
            return None
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        cred_read = advapi32.CredReadW
        cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
        cred_read.restype = wintypes.BOOL
        cred_free = advapi32.CredFree
        cred_free.argtypes = [ctypes.c_void_p]
        cred_free.restype = None
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not cred_read(f"{SERVICE}:{name}", CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return None  # ERROR_NOT_FOUND (1168) or any other failure: no key.
        try:
            credential = pointer.contents
            size = credential.CredentialBlobSize
            if not size or not credential.CredentialBlob:
                return None
            return decode_blob(ctypes.string_at(credential.CredentialBlob, size))
        finally:
            cred_free(pointer)

    def __repr__(self) -> str:
        return "WindowsCredentials()"
