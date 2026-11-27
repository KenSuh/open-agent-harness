# 키 조회의 유일한 경로 — 환경 변수를 먼저 보고, 없으면 macOS 키체인 또는 Windows 자격 증명 관리자에서 실행 시점에만 읽는다.
"""The one documented key lookup (A17).

    oah.secrets.get("TYPESAFE_API_KEY") -> Secret | None

1. The environment variable of that name, when it is set and not empty.
2. The OS keychain, under service `oah` and account `<name>`:
   - macOS: the built-in `security` tool, run as
     `find-generic-password -s oah -a <name> -w`. Store a key with
     `add-generic-password -s oah -a <name> -w` (it prompts for the value).
   - Windows: Credential Manager, a generic credential with target `oah:<name>`. Store one with
     `cmdkey /generic:oah:<name> /user:oah /pass` (it prompts for the value).
     Credential blobs must use UTF-16-LE, as cmdkey does; other encodings are unsupported.
3. Otherwise None. Callers then leave the feature off and report `unused(no key)`.

`find(name)` reads the OS key store alone and says what happened: present, missing, locked, denied, unsupported
or error. `store(name, secret)` and `remove(name)` write under the same service and account, and the value never
passes through a command line. On macOS the built-in tool takes it on standard input, so the item belongs to the
tool that reads it: an item another program adds through Security.framework is one that reader cannot read
without an approval prompt. On Windows `CredWriteW` writes a UTF-16-LE generic blob. `read_handoff(stream)`
takes one credential from a parent process over a pipe, bounded and never echoed.

oah writes a key only to the OS key store, and only on that explicit request: never to a file, an exception
message or a `repr` in this module. Never paste a key into a project file.
"""
from __future__ import annotations

import os
import re
import sys
from typing import BinaryIO, Mapping, Protocol

from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command

SERVICE = "oah"
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
SERVICE_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")
MAC_TOOL_PATH = "/usr/bin"
LOOKUP_TIMEOUT = 10.0
CRED_TYPE_GENERIC = 1
CRED_PERSIST_SESSION, CRED_PERSIST_LOCAL_MACHINE = 1, 2
CRED_BLOB_LIMIT = 2560  # bytes: CRED_MAX_CREDENTIAL_BLOB_SIZE
ERROR_ACCESS_DENIED, ERROR_NOT_FOUND, ERROR_NO_SUCH_LOGON_SESSION = 5, 1168, 1312
VALUE_LIMIT = 1024  # characters in one stored credential
HANDOFF_LIMIT = 4096  # bytes read from a handoff stream before it is refused

# What the device key store answered. Only `present` comes with a value.
PRESENT, MISSING, LOCKED, DENIED, UNSUPPORTED, FAILED = "present", "missing", "locked", "denied", "unsupported", "error"
ADDED, UPDATED, REMOVED = "added", "updated", "removed"

# The macOS `security` tool exits with the low byte of the Security.framework result (OSStatus).
_MAC_EXIT_STATES = {44: MISSING,  # errSecItemNotFound (-25300)
                    36: LOCKED, 29: LOCKED,  # errSecInteractionNotAllowed (-25308), errSecInteractionRequired (-25315)
                    51: DENIED, 128: DENIED}  # errSecAuthFailed (-25293), errSecUserCanceled (-128)
# The tool's interactive mode splits a line on spaces and reads quotes and backslashes; every other visible ASCII
# character arrives as written.
MAC_VALUE_RE = re.compile(r"[!#-&(-\[\]-~]{1,%d}" % VALUE_LIMIT)


class Keychain(Protocol):
    def lookup(self, name: str) -> str | None: ...


class KeyStoreError(Exception):
    """The device key store refused: `state` is locked, denied, unsupported or error. It never holds a value."""

    def __init__(self, state: str) -> None:
        self.state = state if state in (LOCKED, DENIED, UNSUPPORTED) else FAILED
        super().__init__(f"OAH_KEY_STORE: {self.state}")


class Secret:
    """An opaque runtime credential. Only the transport should explicitly reveal its value."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("OAH_SECRET_INVALID: supply a non-empty runtime credential")
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def __format__(self, spec: str) -> str:
        raise TypeError("OAH_SECRET_FORMAT: keep the credential opaque; reveal only in the transport")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("OAH_SECRET_PICKLE: keep credentials in the environment or OS keychain")

    def __reduce__(self):
        raise TypeError("OAH_SECRET_PICKLE: keep credentials in the environment or OS keychain")


def check_name(name: str) -> None:
    """Key names look like environment variable names; anything else is a programming error."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"OAH_SECRET_NAME_INVALID: key names use letters, digits and _ "
                         f"(at most 64), like TYPESAFE_API_KEY; got {name!r}")


def get(name: str, *, env: Mapping[str, str] | None = None, keychain: Keychain | None = None) -> Secret | None:
    """The key called `name` from the environment, then the OS keychain; None when neither has it."""
    environment = value = chain = None
    try:
        check_name(name)
        environment = os.environ if env is None else env
        value = environment.get(name)
        if value:
            return Secret(value)
        chain = system_keychain() if keychain is None else keychain
        value = chain.lookup(name)
        return Secret(value) if value else None
    finally:
        del name, env, keychain, environment, value, chain


def check_service(service: str) -> None:
    """The key store namespace: `oah` in use, another name only for an isolated self-test."""
    if not isinstance(service, str) or not SERVICE_RE.fullmatch(service):
        raise ValueError("OAH_SECRET_SERVICE_INVALID: a key store service uses letters, digits, '.', '_' and '-' "
                         "(at most 64), starting with a letter")


def check_value(value: str) -> None:
    """A stored credential is one line of visible text; the message never repeats the value."""
    try:
        if (not isinstance(value, str) or not value or len(value) > VALUE_LIMIT
                or any(ord(char) < 0x20 or ord(char) == 0x7f or 0xd800 <= ord(char) <= 0xdfff for char in value)):
            raise ValueError(f"OAH_SECRET_INVALID: a stored credential is one line of at most {VALUE_LIMIT} "
                             "characters, with no control characters")
    finally:
        del value


def find(name: str, *, keychain: Keychain | None = None) -> tuple[str, Secret | None]:
    """(state, key) from the device key store alone, never the environment.

    The state is present, missing, locked, denied, unsupported or error; the key is set only when present. A
    keychain without `probe` answers through `lookup`: present or missing."""
    chain = probe = value = None
    try:
        check_name(name)
        chain = system_keychain() if keychain is None else keychain
        probe = getattr(chain, "probe", None)
        if callable(probe):
            state, value = probe(name)
        else:
            value = chain.lookup(name)
            state = PRESENT if value else MISSING
        if state == PRESENT:
            return (PRESENT, Secret(value)) if isinstance(value, str) and value else (MISSING, None)
        return (state if state in (MISSING, LOCKED, DENIED, UNSUPPORTED) else FAILED), None
    finally:
        del name, keychain, chain, probe, value


def store(name: str, secret: Secret, *, keychain: Keychain | None = None) -> str:
    """Add the device key `name`, or replace it, through the key store's own API: `added` or `updated`.

    Raises KeyStoreError(state) when the store is locked, refuses, or has no write API here."""
    chain = writer = value = failure = None
    try:
        check_name(name)
        if not isinstance(secret, Secret):
            raise ValueError("OAH_SECRET_INVALID: hand the credential over as an oah.secrets.Secret")
        value = secret.reveal()
        check_value(value)
        chain = system_keychain() if keychain is None else keychain
        writer = getattr(chain, "store", None)
        if not callable(writer):
            raise KeyStoreError(UNSUPPORTED)
        try:
            result = writer(name, value)
        except (KeyStoreError, ValueError):  # a refusal, or a value this store cannot hold; neither repeats it
            raise
        except Exception:
            failure = KeyStoreError(FAILED)
        if failure is None and result in (ADDED, UPDATED):
            return result
    finally:
        del name, secret, keychain, chain, writer, value
    raise (failure or KeyStoreError(FAILED)) from None


def remove(name: str, *, keychain: Keychain | None = None) -> str:
    """Delete the device key `name`: `removed`, or `missing` when there was none."""
    chain = eraser = failure = None
    try:
        check_name(name)
        chain = system_keychain() if keychain is None else keychain
        eraser = getattr(chain, "remove", None)
        if not callable(eraser):
            raise KeyStoreError(UNSUPPORTED)
        try:
            result = eraser(name)
        except KeyStoreError:
            raise
        except Exception:
            failure = KeyStoreError(FAILED)
        if failure is None and result in (REMOVED, MISSING):
            return result
    finally:
        del name, keychain, chain, eraser
    raise (failure or KeyStoreError(FAILED)) from None


def read_handoff(stream: BinaryIO, *, limit: int = HANDOFF_LIMIT) -> Secret:
    """One credential handed over on a byte stream, such as a parent process's pipe to standard input.

    It reads at most `limit` bytes of UTF-8, one line, to end of input. A terminal is refused before any read,
    so nothing typed is echoed. A refusal names the rule, never the bytes."""
    reader = data = piece = text = problem = None
    try:
        reader = getattr(stream, "buffer", stream)
        if not callable(getattr(reader, "read", None)):
            raise ValueError("OAH_SECRET_HANDOFF: hand the credential over on a byte stream")
        if any(callable(getattr(item, "isatty", None)) and item.isatty() for item in (stream, reader)):
            raise ValueError("OAH_SECRET_HANDOFF: hand the credential over on a pipe, not a terminal")
        data = bytearray()
        while len(data) <= limit:
            piece = reader.read(limit + 1 - len(data))
            if not piece:
                break
            if not isinstance(piece, (bytes, bytearray)):
                raise ValueError("OAH_SECRET_HANDOFF: the stream must give bytes, not text")
            data += piece
        if len(data) > limit:
            problem = f"the credential is longer than {limit} bytes"
        else:
            try:
                text = bytes(data).decode("utf-8")
            except UnicodeDecodeError:
                problem = "the credential is not UTF-8"  # the decoding error itself would carry the bytes
        if problem is None:
            text = text[:-1] if text.endswith("\n") else text
            text = text[:-1] if text.endswith("\r") else text
            try:
                check_value(text)
                return Secret(text)
            except ValueError:
                problem = "the credential must be one line with no control characters"
    finally:
        for index in range(len(data or ())):
            data[index] = 0
        del stream, reader, data, piece, text
    raise ValueError(f"OAH_SECRET_HANDOFF: {problem}") from None


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

    def probe(self, name: str) -> tuple[str, str | None]:
        return UNSUPPORTED, None

    def __repr__(self) -> str:
        return "NoKeychain()"


class MacKeychain:
    """The macOS login keychain, read and written through the built-in `security` tool.

    `lookup` reads any failure as no key; `probe` tells a missing key from a locked keychain or a refusal.
    `store` hands the value to the tool on standard input (its interactive mode), never on a command line, so the
    item belongs to the tool that reads it and no approval prompt follows. `service` is `oah` in use; an isolated
    self-test names another."""

    def __init__(self, search_path: str | None = MAC_TOOL_PATH, timeout: float = LOOKUP_TIMEOUT, *,
                 service: str = SERVICE) -> None:
        check_service(service)
        self.search_path = search_path
        self.timeout = timeout
        self.service = service

    def _tool(self, *arguments: str, feed: str | None = None) -> tuple[str, str]:
        """(state, output) of one tool run: `present` for exit 0, else what its exit status means."""
        try:
            tool = resolve_command("security", self.search_path)
        except UnsupportedBatchShim:
            return UNSUPPORTED, ""
        if tool is None:
            return UNSUPPORTED, ""
        try:
            result = run_command(tool.argv + arguments, timeout=self.timeout, stdin_text=feed)
        except OSError:
            return FAILED, ""
        finally:
            del feed
        if result.timed_out:
            return LOCKED, ""  # it waited for an unlock or an approval that nobody gave
        if result.returncode != 0:
            return _MAC_EXIT_STATES.get(result.returncode, FAILED), ""
        return PRESENT, result.stdout

    def probe(self, name: str) -> tuple[str, str | None]:
        check_name(name)
        state, value = self._tool("find-generic-password", "-s", self.service, "-a", name, "-w")
        if state != PRESENT:
            return state, None
        value = value[:-1] if value.endswith("\n") else value
        value = value.rstrip("\r")
        return (PRESENT, value) if value else (MISSING, None)

    def lookup(self, name: str) -> str | None:
        return self.probe(name)[1]

    def store(self, name: str, value: str) -> str:
        state = found = None
        try:
            check_name(name)
            if not isinstance(value, str) or not MAC_VALUE_RE.fullmatch(value):
                raise ValueError("OAH_SECRET_INVALID: a credential stored on macOS uses visible ASCII characters "
                                 f"(at most {VALUE_LIMIT}) with no space, quote or backslash")
            # Without a default keychain (a redirected home folder, say) the tool would ask the user to reset
            # their keychains, so the value is not handed over.
            state = self._tool("default-keychain")[0]
            if state == FAILED:
                state = UNSUPPORTED
            if state == PRESENT:
                # Attributes only: whether an item exists, without reading its value.
                state = self._tool("find-generic-password", "-s", self.service, "-a", name)[0]
            if state in (PRESENT, MISSING):
                existed = state == PRESENT
                state = self._tool("-i", feed=f"add-generic-password -U -s {self.service} -a {name} -w {value}\n")[0]
                if state == PRESENT:
                    state, found = self.probe(name)
                    if state == PRESENT and found == value:
                        return UPDATED if existed else ADDED
                    state = state if state in (LOCKED, DENIED) else FAILED
        finally:
            del value, found
        raise KeyStoreError(state)

    def remove(self, name: str) -> str:
        check_name(name)
        state = self._tool("delete-generic-password", "-s", self.service, "-a", name)[0]
        if state == PRESENT:
            return REMOVED
        if state == MISSING:
            return MISSING
        raise KeyStoreError(state)

    def __repr__(self) -> str:
        return f"MacKeychain(search_path={self.search_path!r})"


def decode_blob(data: bytes) -> str | None:
    """Decode a UTF-16-LE Credential Manager blob (cmdkey's format); never guess its encoding."""
    if not data:
        return None
    try:
        text = data.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return None
    return text or None


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
    """Windows Credential Manager: a generic credential with target `oah:<name>`.

    `service` is `oah` in use; an isolated self-test names another and keeps its entries to the session."""

    _STATES = {ERROR_NOT_FOUND: MISSING, ERROR_ACCESS_DENIED: DENIED, ERROR_NO_SUCH_LOGON_SESSION: LOCKED}

    def __init__(self, service: str = SERVICE, persist: int = CRED_PERSIST_LOCAL_MACHINE) -> None:
        check_service(service)
        self.service = service
        self.persist = persist

    @staticmethod
    def _library():
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi32.CredDeleteW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        return advapi32

    def probe(self, name: str) -> tuple[str, str | None]:
        check_name(name)
        if os.name != "nt":
            return UNSUPPORTED, None
        advapi32 = self._library()
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not advapi32.CredReadW(f"{self.service}:{name}", CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return self._STATES.get(ctypes.get_last_error(), FAILED), None
        try:
            credential = pointer.contents
            size = credential.CredentialBlobSize
            if not size or not credential.CredentialBlob:
                return MISSING, None
            value = decode_blob(ctypes.string_at(credential.CredentialBlob, size))
            return (PRESENT, value) if value else (FAILED, None)
        finally:
            advapi32.CredFree(pointer)

    def lookup(self, name: str) -> str | None:
        return self.probe(name)[1]  # ERROR_NOT_FOUND (1168) or any other failure: no key.

    def store(self, name: str, value: str) -> str:
        blob = buffer = None
        try:
            check_name(name)
            if os.name != "nt":
                raise KeyStoreError(UNSUPPORTED)
            blob = value.encode("utf-16-le")
            if len(blob) > CRED_BLOB_LIMIT:
                raise KeyStoreError(FAILED)
            advapi32 = self._library()
            target = f"{self.service}:{name}"
            pointer = ctypes.POINTER(_CREDENTIALW)()
            existed = bool(advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)))
            if existed:
                advapi32.CredFree(pointer)
            credential = _CREDENTIALW()
            credential.Type = CRED_TYPE_GENERIC
            credential.TargetName = target
            credential.UserName = self.service
            buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
            credential.CredentialBlobSize = len(blob)
            credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            credential.Persist = self.persist
            if not advapi32.CredWriteW(ctypes.byref(credential), 0):
                raise KeyStoreError(self._STATES.get(ctypes.get_last_error(), FAILED))
            return UPDATED if existed else ADDED
        finally:
            if buffer is not None:
                ctypes.memset(buffer, 0, len(buffer))
            del value, blob, buffer

    def remove(self, name: str) -> str:
        check_name(name)
        if os.name != "nt":
            raise KeyStoreError(UNSUPPORTED)
        if self._library().CredDeleteW(f"{self.service}:{name}", CRED_TYPE_GENERIC, 0):
            return REMOVED
        state = self._STATES.get(ctypes.get_last_error(), FAILED)
        if state == MISSING:
            return MISSING
        raise KeyStoreError(state)

    def __repr__(self) -> str:
        return "WindowsCredentials()"
