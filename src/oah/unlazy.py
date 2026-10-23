# unlazy 연동 — 설치 위치 찾기(OAH_UNLAZY_DIR, 프로젝트, 사용자 스킬 폴더), 고정 커밋·스크립트 다이제스트 검증, gate-check·gate-lint·stop-hook 호출 계약을 맡는다.
from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Mapping, Sequence

from oah.engines.proc import RunResult, UnsupportedBatchShim, resolve_command, run_command

ENV_VAR = "OAH_UNLAZY_DIR"
PINNED_COMMIT = "16671491f6679ad9378f52604d3bc2415b4120c7"
UPSTREAM = "https://github.com/Leonxlnx/unlazy"
UPSTREAM_GIT = UPSTREAM + ".git"
# The only install instruction oah prints: a git checkout of the pinned commit (never an unversioned install).
INSTALL_HINT = (f"git clone {UPSTREAM_GIT} <skills folder>/unlazy, then "
                f"git -C <skills folder>/unlazy checkout --detach {PINNED_COMMIT}")


def _is_unlazy(folder: pathlib.Path) -> bool:
    return (folder / "scripts" / "gate-check.mjs").is_file()


def find_unlazy_dir(repo_root: pathlib.Path | None, env: Mapping[str, str],
                    home: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """Return (folder, reason). An OAH_UNLAZY_DIR value is the only folder checked."""
    override = env.get(ENV_VAR)
    if override:
        folder = pathlib.Path(override).resolve()
        if _is_unlazy(folder):
            return folder, f"from {ENV_VAR}"
        return None, f"{ENV_VAR} has no scripts/gate-check.mjs"
    candidates: list[pathlib.Path] = []
    if repo_root is not None:
        candidates += [repo_root / ".agents" / "skills" / "unlazy", repo_root / ".claude" / "skills" / "unlazy"]
    candidates += [home / ".agents" / "skills" / "unlazy", home / ".claude" / "skills" / "unlazy"]
    for folder in candidates:
        if _is_unlazy(folder):
            return folder.resolve(), "found"
    return None, f"unlazy not found (install: {INSTALL_HINT})"


def unlazy_version(unlazy_dir: pathlib.Path) -> str | None:
    """The version recorded in unlazy's package.json, or None."""
    try:
        manifest = json.loads((unlazy_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) else None


# The pinned CLI contract ------------------------------------------------------------------------
#
# Written against unlazy at PINNED_COMMIT (package version 2.1.0, an unreleased source target):
#
#   node scripts/gate-check.mjs [--status | --reverify] [--approve] [--timeout S] [--cwd DIR]
#                               [--root DIR] -- <ledger>
#     exit 0 and a line "ALL MET (<n> met...)"                      -> met
#     exit 1 and a line "UNMET: <n> (...)"                          -> unmet (wins over a handoff)
#     exit 1 and a line "HANDOFF REQUIRED: <n> abandoned (...)"     -> handoff
#     exit 2 usage, parse or infrastructure; exit 3 lease conflict  -> error
#     --status never executes, approves or writes; it lists "  UNMET <stem>:<id> (<reason>): <title>".
#     Without an approval a run prints "APPROVAL REQUIRED" and "NOT RUN" and executes nothing.
#   node scripts/gate-lint.mjs [--strict] [--json] -- <ledger>
#     exit 0 "LINT OK" -> met; exit 1 "LINT FINDINGS" (strict) -> unmet; exit 2 -> error.
#   node scripts/stop-hook.mjs   (Claude Code Stop payload on stdin)
#     prints nothing, {"systemMessage": ...} or {"decision": "block", "reason": ...}; always exits 0.
#
# oah passes --approve only when the user asked for it, so running a CHECK always goes through
# unlazy's own approval flow. oah never executes CHECK lines itself.

PINNED_VERSION = "2.1.0"
NODE_ENV = "OAH_NODE"
NODE_HINT = "install Node.js from https://nodejs.org (the LTS installer), then open a new terminal"
STATUS_TIMEOUT = 120.0
RUN_TIMEOUT = 3600.0
MODES = {"status": "--status", "run": None, "reverify": "--reverify"}


class UnlazyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


# The pin --------------------------------------------------------------------------------------------
#
# A discovered unlazy is used only when it is a git checkout of PINNED_COMMIT and every script oah calls,
# with everything those scripts load, has the pinned sha256 (of LF-normalized bytes, so a Windows
# checkout with core.autocrlf still matches). A new unlazy commit is adopted only by updating PIN here
# and passing the contract tests (tests/test_gates.py, ContractTest and PinnedUnlazyProbeTest).

@dataclass(frozen=True)
class Pin:
    commit: str
    files: Mapping[str, str]  # path inside the unlazy folder -> sha256 of its LF-normalized bytes


PIN = Pin(commit=PINNED_COMMIT, files=types.MappingProxyType({
    "scripts/gate-check.mjs": "29dee3acfdf566c24eedace85163a4e7a66eda01b712c5b4b76a99eae1eda8a1",
    "scripts/gate-lint.mjs": "12fedaa4ad7bcfacf6da8d66f4ead0d186115e03fe941d3d20efd361dec7ee7e",
    "scripts/stop-hook.mjs": "f18f2f189a3be2ff1bcdd01b09eea0ac53edcf9a3f9d9b1ba64817661ce4b683",
    "scripts/lib/gates.mjs": "12758e27c415e619a93148fb71cd217d0c1b81de39fe932b2dda344fac778809",
    "scripts/lib/dispatch.mjs": "101cc06361f2eae77946d5fa1d9dd5dd9effcfc039f376967565951909ad1baf",
    "scripts/lib/process-tree.mjs": "2b6c28613b2686e3afaa32a0196eaa3c67fafa283fbbba11df872f4deb993682",
    "scripts/lib/check-supervisor.mjs": "2bf15a383c0e64f77024820fedf9da8045c02cca9051ec0e6444e85814eb8c3b",
    "scripts/lib/regex-worker.mjs": "76a9448f3c1b15b4904239608bd50381ee609e1edf0c6ac281d3f30473b36e0b",
}))


def default_install_dir(home: pathlib.Path) -> pathlib.Path:
    """Where `oah init` installs unlazy for a user: the user's agent skills folder."""
    return pathlib.Path(home) / ".agents" / "skills" / "unlazy"


def install_commands(target: pathlib.Path, pin: Pin | None = None) -> list[list[str]]:
    """The argv lists that install the pinned unlazy into `target` (run them in order, without a shell)."""
    pin = pin or PIN
    return [["git", "clone", "--quiet", UPSTREAM_GIT, str(target)],
            ["git", "-C", str(target), "checkout", "--quiet", "--detach", pin.commit]]


def install_hint(target: pathlib.Path, pin: Pin | None = None) -> str:
    """The same commands as one line a person can copy."""
    pin = pin or PIN
    return f'git clone {UPSTREAM_GIT} "{target}", then git -C "{target}" checkout --detach {pin.commit}'


def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify_pin(folder: pathlib.Path, pin: Pin | None = None) -> list[str]:
    """Why `folder` is not the pinned unlazy; an empty list means it matches by commit and digests."""
    from oah.paths import GitMissing, run_git

    pin = pin or PIN
    folder = pathlib.Path(folder)
    problems = []
    for relative, digest in pin.files.items():
        path = folder / pathlib.PurePosixPath(relative)
        try:
            actual = file_digest(path)
        except OSError:
            problems.append(f"{relative} is missing")
            continue
        if actual != digest:
            problems.append(f"{relative} differs from the pinned copy")
    try:
        top = run_git(folder, "rev-parse", "--show-toplevel")
        head = run_git(folder, "rev-parse", "--verify", "--quiet", "HEAD")
    except GitMissing:
        return problems + ["git is missing, so its commit cannot be confirmed"]
    top_path = pathlib.Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else None
    if top_path is None or top_path.resolve() != folder.resolve():
        problems.append("it is not a git checkout of its own, so its commit cannot be confirmed")
    elif head.stdout.strip() != pin.commit:
        problems.append(f"it is at commit {head.stdout.strip()[:12] or 'none'}, not the pinned {pin.commit[:12]}")
    return problems


def require_pinned(folder: pathlib.Path, pin: Pin | None = None) -> None:
    """Refuse a discovered unlazy that is not the pinned one, with the command that installs it."""
    pin = pin or PIN
    problems = verify_pin(folder, pin)
    if problems:
        raise UnlazyError("OAH_UNLAZY_UNPINNED", f"{folder} is not the unlazy oah is pinned to "
                                                 f"({'; '.join(problems)}). Move that folder aside, then install "
                                                 f"the pinned copy: {install_hint(folder, pin)}")


@dataclass(frozen=True)
class UnlazyResult:
    returncode: int | None
    stdout: str
    stderr: str
    verdict: str  # met | unmet | handoff | error


def _summary_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line and not line[0].isspace()]


def parse_verdict(returncode: int | None, stdout: str) -> str:
    """Map gate-check's exit code and summary lines to met, unmet, handoff or error."""
    lines = _summary_lines(stdout)
    if returncode == 0:
        return "met" if any(line.startswith("ALL MET (") for line in lines) and not any(
            line.startswith(("UNMET: ", "HANDOFF REQUIRED: ")) for line in lines) else "error"
    if returncode == 1:
        if any(line.startswith("UNMET: ") for line in lines):
            return "unmet"
        if any(line.startswith("HANDOFF REQUIRED: ") for line in lines):
            return "handoff"
    return "error"


def resolve_node(env: Mapping[str, str]) -> tuple[str, ...] | None:
    """The argv that runs node: OAH_NODE (a JSON list) when set, else node from PATH, else None."""
    override = env.get(NODE_ENV)
    if override:
        try:
            value = json.loads(override)
        except ValueError:
            value = None
        if not (isinstance(value, list) and value and all(isinstance(part, str) and part for part in value)):
            raise UnlazyError("OAH_NODE_INVALID", f"{NODE_ENV} must be a JSON list such as "
                                                  '["C:\\\\Program Files\\\\nodejs\\\\node.exe"]')
        return tuple(value)
    try:
        found = resolve_command("node", search_path=env.get("PATH", ""))
    except UnsupportedBatchShim as exc:
        raise UnlazyError("OAH_NODE_INVALID", str(exc)) from exc
    return found.argv if found is not None else None


def _script(unlazy_dir: pathlib.Path, name: str) -> pathlib.Path:
    script = pathlib.Path(unlazy_dir).resolve() / "scripts" / name
    if not script.is_file():
        raise UnlazyError("OAH_UNLAZY_INCOMPLETE", f"{unlazy_dir} has no scripts/{name}; install the pinned copy: "
                                                   f"{install_hint(unlazy_dir)}")
    return script


def _run(argv: list[str], *, env: Mapping[str, str] | None, timeout: float, cwd: pathlib.Path | None = None,
         stdin_text: str | None = None) -> RunResult:
    try:
        return run_command(argv, timeout=timeout, env=env, cwd=str(cwd) if cwd else None, stdin_text=stdin_text)
    except UnsupportedBatchShim as exc:
        raise UnlazyError("OAH_NODE_INVALID", str(exc)) from exc
    except OSError as exc:
        raise UnlazyError("OAH_NODE_MISSING", f"cannot start {argv[0]}: {exc}; {NODE_HINT}") from exc


def _private_directory(folder: pathlib.Path) -> None:
    """Create the snapshot with owner-only access, atomically on both supported platforms."""
    if os.name != "nt":
        folder.mkdir(mode=0o700)
        return
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.DWORD))
    convert.restype = wintypes.BOOL
    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    create = kernel.CreateDirectoryW
    create.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes))
    create.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # Protected DACL: only the object's owner has full access; files and subdirectories inherit it.
    if not convert(f"O:{_windows_user_sid()}D:P(A;OICI;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not create(str(folder), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _permission_refused(reason: str) -> UnlazyError:
    return UnlazyError("OAH_UNLAZY_SNAPSHOT_PERMISSIONS", f"cannot prove the unlazy snapshot is private: {reason}; "
                       "fix the reported path's ownership and permissions to allow only your account, "
                       "or set OAH_STATE_DIR to a private local state folder under trusted parents, then retry")


def _private_makedirs(folder: pathlib.Path) -> None:
    missing = []
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for directory in reversed(missing):
        _private_directory(directory)


def _strip_snapshot_acls(folder: pathlib.Path) -> None:
    if sys.platform == "darwin":
        try:
            result = run_command(["/bin/chmod", "-RN", str(folder)], timeout=STATUS_TIMEOUT)
            if result.returncode != 0 or result.timed_out:
                raise OSError("chmod -RN could not remove inherited ACLs")
        except OSError as exc:
            raise _permission_refused(f"{folder}: {exc}") from exc


def _windows_user_sid() -> str:
    """Read the process token's user SID, without localized account names or shell commands."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = (), wintypes.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = (wintypes.HANDLE,), wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = (ctypes.c_void_p,), ctypes.c_void_p
    open_token = advapi.OpenProcessToken
    open_token.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    open_token.restype = wintypes.BOOL
    get_token = advapi.GetTokenInformation
    get_token.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD))
    get_token.restype = wintypes.BOOL
    convert = advapi.ConvertSidToStringSidW
    convert.argtypes, convert.restype = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), wintypes.BOOL
    token = wintypes.HANDLE()
    if not open_token(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):  # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        get_token(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if ctypes.get_last_error() != 122 or size.value < ctypes.sizeof(ctypes.c_void_p):
            raise OSError("cannot size the current user's token")
        data = ctypes.create_string_buffer(size.value)
        if not get_token(token, 1, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p)).contents
        text = ctypes.c_void_p()
        if not convert(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def _windows_canonical_sid(principal: str) -> str:
    """Resolve SDDL aliases through Windows, including machine- and domain-relative SIDs."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    parse = advapi.ConvertStringSidToSidW
    parse.argtypes, parse.restype = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)), wintypes.BOOL
    convert = advapi.ConvertSidToStringSidW
    convert.argtypes, convert.restype = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = (ctypes.c_void_p,), ctypes.c_void_p
    sid = ctypes.c_void_p()
    if not parse(principal, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        text = ctypes.c_void_p()
        if not convert(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.LocalFree(sid)


def _windows_security_descriptor(path: pathlib.Path) -> str:
    """Return the actual owner and DACL as SDDL; unreadable security descriptors refuse execution."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi.GetFileSecurityW
    get_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD))
    get_security.restype = wintypes.BOOL
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = (ctypes.c_void_p,), ctypes.c_void_p
    size = wintypes.DWORD()
    get_security(str(path), 5, None, 0, ctypes.byref(size))  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    if ctypes.get_last_error() != 122 or not size.value:
        raise OSError("cannot size the snapshot's security descriptor")
    descriptor = ctypes.create_string_buffer(size.value)
    if not get_security(str(path), 5, descriptor, size, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    text = ctypes.c_void_p()
    if not convert(descriptor, 1, 5, ctypes.byref(text), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.wstring_at(text)
    finally:
        kernel.LocalFree(text)


def _macos_mount_flags(path: pathlib.Path) -> int:
    """Read Darwin's statfs64 flags, including MNT_IGNORE_OWNERSHIP (noowners)."""
    import ctypes

    class Statfs(ctypes.Structure):
        _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
                    ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
                    ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                    ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2),
                    ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
                    ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                    ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                    ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                    ("f_reserved", ctypes.c_uint32 * 7)]

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    statfs = libc.statfs64
    statfs.argtypes, statfs.restype = (ctypes.c_char_p, ctypes.POINTER(Statfs)), ctypes.c_int
    info = Statfs()
    if statfs(os.fsencode(path), ctypes.byref(info)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(path))
    return info.f_flags


def _verify_windows_dacl(sddl: str, sid: str, *, snapshot: bool = False,
                         directory: bool = True, protected: bool = False) -> None:
    """Trust standard Windows owners; reject foreign replacement or snapshot-write rights."""
    trusted = {sid, "S-1-5-18", "S-1-5-32-544",
               "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
    match = re.fullmatch(r"O:([^:()]+)D:((?:P|AI|AR)*)(\(.+\))", sddl or "")
    if not match:
        raise OSError("the owner or DACL cannot be parsed")
    owner = _windows_canonical_sid(match[1])
    if owner not in trusted:
        raise OSError(f"untrusted owner SID {owner} has implicit WRITE_DAC rights")
    if protected and "P" not in match[2]:
        raise OSError("the snapshot root DACL is not protected from inheritance")
    aces = re.findall(r"\(([^()]*)\)", match[3])
    if "".join(f"({ace})" for ace in aces) != match[3]:
        raise OSError("the DACL cannot be parsed")
    rights_masks = {"CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "RP": 0x10, "WP": 0x20,
                    "DT": 0x40, "LO": 0x80, "CR": 0x100, "SD": 0x10000, "RC": 0x20000,
                    "WD": 0x40000, "WO": 0x80000, "FA": 0x1F01FF, "FR": 0x120089,
                    "FW": 0x120116, "FX": 0x1200A0, "GA": 0x10000000, "GX": 0x20000000,
                    "GW": 0x40000000, "GR": 0x80000000}
    for ace in aces:
        fields = ace.split(";")
        if (len(fields) != 6 or fields[0] not in ("A", "D") or fields[3] or fields[4]
                or not re.fullmatch(r"(?:OI|CI|NP|IO|ID|SA|FA)*", fields[1])):
            raise OSError("the DACL has an unsupported entry")
        principal = _windows_canonical_sid(fields[5])
        # Inherit-only CREATOR OWNER (S-1-3-0) is a standard default grant.
        # No inherit-only entry or denial grants access to the object being checked.
        if fields[0] == "D" or "IO" in fields[1] or principal in trusted or principal == "S-1-3-4":
            continue
        rights = fields[2]
        if re.fullmatch(r"0x[0-9a-fA-F]+", rights):
            mask = int(rights, 16)
        else:
            tokens = [rights[i:i + 2] for i in range(0, len(rights), 2)]
            if not tokens or any(token not in rights_masks for token in tokens):
                raise OSError(f"SID {principal} grants unverified rights {rights!r}")
            mask = 0
            for token in tokens:
                mask |= rights_masks[token]
        rejected = [name for bit, name in ((0x10000, "DELETE"), (0x40, "FILE_DELETE_CHILD"),
                    (0x40000, "WRITE_DAC"), (0x80000, "WRITE_OWNER"), (0x10000000, "GENERIC_ALL"))
                    if mask & bit]
        if snapshot and (mask & 0x40000000 or mask & 0x120116 == 0x120116):
            rejected.append("GENERIC_WRITE")
        # On files, the directory's create-child bits instead allow rewriting verified bytes.
        if snapshot and not directory and mask & 0x116:
            rejected.append("FILE_WRITE_DATA/APPEND_DATA/WRITE_EA/WRITE_ATTRIBUTES")
        if mask & ~0xF01F01FF:  # Only known generic, standard and file-specific rights are verified.
            rejected.append("unverified rights")
        if rejected:
            raise OSError(f"SID {principal} grants rejected rights {rights!r} ({', '.join(rejected)})")


def _verify_state_ancestry(folder: pathlib.Path, *, platform: str | None = None) -> None:
    """Prove that no other account can replace any component leading to state.

    Trusted Windows system owners are allowed throughout; POSIX state must belong to this user.
    A POSIX sticky ancestor protects its trusted child against rename, but state itself may not be
    group/other writable. Unknown ACLs fail closed rather than guessing effective access.
    """
    platform = sys.platform if platform is None else platform
    path = folder
    try:
        if platform == "win32":
            sid = _windows_user_sid()
            if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
                raise OSError("cannot identify the current user's SID")
        chain = (*reversed(folder.parents), folder)
        for path in chain:
            try:
                info = path.lstat()
            except FileNotFoundError:
                return  # The remaining suffix will be created privately, then verified again.
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise OSError("a directory component is a symlink or reparse point")
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("a directory component is not a directory")
            if platform == "win32":
                _verify_windows_dacl(_windows_security_descriptor(path), sid)
            else:
                # Query every component: a nested mount can synthesize this caller's st_uid even
                # when the volume above it enforces ownership. Modes alone then prove no privacy.
                if platform == "darwin" and _macos_mount_flags(path) & 0x00200000:  # MNT_IGNORE_OWNERSHIP
                    raise OSError("the volume ignores ownership (noowners); choose a state folder on a volume "
                                  "that honours ownership using OAH_STATE_DIR")
                if info.st_uid not in ({os.getuid()} if path == folder else {0, os.getuid()}):
                    raise OSError("the directory is not owned by your account or a trusted system ancestor")
                if info.st_mode & 0o022:
                    # Sticky ancestors protect the trusted child checked on the next iteration.
                    # Missing children are created exclusively and checked before any copy/launch.
                    if not info.st_mode & stat.S_ISVTX or path == folder:
                        raise OSError("the directory permits group or other accounts to replace its children")
                if platform == "darwin":
                    result = run_command(["/bin/ls", "-lde", str(path)], timeout=STATUS_TIMEOUT,
                                         env=dict(os.environ, LC_ALL="C"))
                    lines = result.stdout.splitlines()
                    if (result.returncode != 0 or result.timed_out or result.stderr or not lines
                            or not lines[0].split()
                            or not re.fullmatch(r"d[rwxStTs-]{9}[@+]?", lines[0].split()[0])
                            or ("+" in lines[0].split()[0]) != (len(lines) > 1)
                            or any(not re.fullmatch(r"\s*\d+: \S+(?: inherited)? deny [a-z_,]+", line)
                                   for line in lines[1:])):
                        raise OSError("the directory ACL is unreadable or contains an access grant")
                elif platform.startswith("linux"):
                    for name in ("system.posix_acl_access", "system.posix_acl_default"):
                        try:
                            os.getxattr(path, name, follow_symlinks=False)
                        except OSError as exc:
                            if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                                raise
                        else:
                            raise OSError("the directory has an unverified POSIX ACL")
                else:
                    raise OSError("directory ACL verification is unavailable on this platform")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise _permission_refused(f"{path}: {exc}") from exc


def _verify_snapshot_permissions(folder: pathlib.Path, *, platform: str | None = None) -> None:
    platform = sys.platform if platform is None else platform
    _verify_state_ancestry(folder.parent, platform=platform)
    if platform not in ("darwin", "win32"):
        return
    path = folder
    try:
        if platform == "win32":
            sid = _windows_user_sid()
            if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
                raise OSError("cannot identify the current user's SID")
        for path in (folder, *folder.rglob("*")):
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:  # reparse point
                raise OSError("a snapshot object is a link")
            if platform == "darwin":
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise OSError("a snapshot object is not owner-only")
                result = run_command(["/bin/ls", "-lde", str(path)], timeout=STATUS_TIMEOUT,
                                     env=dict(os.environ, LC_ALL="C"))
                lines = result.stdout.splitlines()
                # -e lists every ACL entry. A '+' mode marker or any extra/unknown output refuses.
                if (result.returncode != 0 or result.timed_out or result.stderr or len(lines) != 1
                        or not lines[0].split() or not re.fullmatch(r"[d-][rwxStTs-]{9}@?", lines[0].split()[0])):
                    raise OSError("cannot confirm that every snapshot ACL was removed")
            else:
                _verify_windows_dacl(_windows_security_descriptor(path), sid, snapshot=True,
                                     directory=stat.S_ISDIR(info.st_mode), protected=path == folder)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise _permission_refused(f"{path}: {exc}") from exc


@contextmanager
def _snapshot(folder: pathlib.Path, env: Mapping[str, str], anchor: pathlib.Path):
    """Copy the entire pinned closure into private state, verify the copy, keep it through execution.

    No copy is reused: each invocation checks its own bytes and removes them after the child exits.
    The discovered paths may change during copying, but only a copy matching every pin can execute.
    """
    from oah.paths import NotARepo, repo_info, run_git, state_dir

    pin = PIN
    require_pinned(folder, pin)
    commit = run_git(folder, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip()
    if env.get("OAH_STATE_DIR"):
        state = pathlib.Path(env["OAH_STATE_DIR"])
    else:
        try:
            info = repo_info(anchor)
        except NotARepo:
            info = repo_info(folder)
        state = state_dir(info, env)
    try:
        # Do not resolve away links before checking the components actually used by Node.
        state = state.absolute()
        _verify_state_ancestry(state)
        _private_makedirs(state)
        _verify_state_ancestry(state)
        copy = state / f"unlazy-{secrets.token_hex(16)}"
        _private_directory(copy)
        try:
            _strip_snapshot_acls(copy)
            _verify_snapshot_permissions(copy)
            if any(copy.iterdir()):
                raise _permission_refused(f"{copy}: the fresh snapshot is not empty")
            for relative in pin.files:
                target = copy / relative
                _private_makedirs(target.parent)
                # Copy bytes, never symlinks, hard links, git metadata, or unpinned neighboring code.
                with target.open("xb") as handle:
                    target.chmod(0o600)
                    handle.write((folder / relative).read_bytes())
            record = copy / "pin.json"
            record.write_text(json.dumps({"commit": commit}), encoding="utf-8")
            record.chmod(0o600)
            _verify_snapshot_permissions(copy)
            if (json.loads(record.read_text(encoding="utf-8")) != {"commit": pin.commit}
                    or any(file_digest(copy / relative) != digest for relative, digest in pin.files.items())):
                raise UnlazyError("OAH_UNLAZY_UNPINNED", "unlazy changed while being copied; install the pinned "
                                  f"copy and retry: {install_hint(folder, pin)}")
            _verify_snapshot_permissions(copy)
            yield copy
        finally:
            shutil.rmtree(copy)
    except OSError as exc:
        raise UnlazyError("OAH_UNLAZY_SNAPSHOT_FAILED", f"cannot prepare or protect the private unlazy copy: {exc}; "
                          "set OAH_STATE_DIR to a private writable state folder and retry") from exc


def _execute(folder: pathlib.Path, name: str, args: Sequence[str], *, node: Sequence[str],
             env: Mapping[str, str] | None, timeout: float, anchor: pathlib.Path,
             cwd: pathlib.Path | None = None, stdin_text: str | None = None) -> RunResult:
    env = dict(os.environ if env is None else env)
    options = [value for key, value in env.items()
               if (key.upper() if os.name == "nt" else key) == "NODE_OPTIONS"]
    if any(options):
        raise UnlazyError("OAH_NODE_OPTIONS_REFUSED", "Clear NODE_OPTIONS before running gates or the Stop hook; "
                          "unverified Node preloads and runtime flags are not permitted")
    _script(folder, name)  # preserve the missing-entry-point diagnostic
    with _snapshot(pathlib.Path(folder), env, anchor) as copy:
        return _run([*node, str(_script(copy, name)), *args], env=env, timeout=timeout, cwd=cwd,
                    stdin_text=stdin_text)


def gate_check(unlazy_dir: pathlib.Path, ledger: pathlib.Path, *, node: Sequence[str], mode: str,
               approve: bool = False, cwd: pathlib.Path | None = None, root: pathlib.Path | None = None,
               timeout: int | None = None, env: Mapping[str, str] | None = None,
               process_timeout: float | None = None) -> UnlazyResult:
    """Run gate-check.mjs on one ledger. `mode` is status, run or reverify."""
    if mode not in MODES:
        raise ValueError(f"unknown gate-check mode {mode!r}")
    if mode == "status" and (approve or cwd is not None or timeout is not None):
        raise ValueError("--status takes no --approve, --cwd or --timeout")
    argv = []
    if MODES[mode]:
        argv.append(MODES[mode])
    if approve:
        argv.append("--approve")
    if timeout is not None:
        argv += ["--timeout", str(int(timeout))]
    if cwd is not None:
        argv += ["--cwd", str(cwd)]
    if root is not None:
        argv += ["--root", str(root)]
    argv += ["--", str(ledger)]
    limit = process_timeout or (STATUS_TIMEOUT if mode == "status" else RUN_TIMEOUT)
    result = _execute(unlazy_dir, "gate-check.mjs", argv, node=node, env=env, timeout=limit,
                      anchor=root or cwd or ledger.parent)
    verdict = "error" if result.timed_out else parse_verdict(result.returncode, result.stdout)
    return UnlazyResult(result.returncode, result.stdout, result.stderr, verdict)


def gate_lint(unlazy_dir: pathlib.Path, ledger: pathlib.Path, *, node: Sequence[str], strict: bool = False,
              env: Mapping[str, str] | None = None) -> UnlazyResult:
    """Run gate-lint.mjs on one ledger; it never executes a CHECK."""
    argv = (["--strict"] if strict else []) + ["--", str(ledger)]
    result = _execute(unlazy_dir, "gate-lint.mjs", argv, node=node, env=env, timeout=STATUS_TIMEOUT,
                      anchor=ledger.parent)
    lines = _summary_lines(result.stdout)
    if result.returncode == 0 and any(line.startswith("LINT OK") for line in lines):
        verdict = "met"
    elif result.returncode == 1 and any(line.startswith("LINT FINDINGS") for line in lines):
        verdict = "unmet"
    else:
        verdict = "error"
    return UnlazyResult(result.returncode, result.stdout, result.stderr, verdict)


def stop_hook(unlazy_dir: pathlib.Path, payload: Mapping, *, node: Sequence[str],
              env: Mapping[str, str] | None = None, cwd: pathlib.Path | None = None) -> UnlazyResult:
    """Run unlazy's own Stop hook with the Claude Code payload on stdin."""
    result = _execute(unlazy_dir, "stop-hook.mjs", (), node=node, env=env, timeout=STATUS_TIMEOUT,
                      anchor=cwd or pathlib.Path(payload.get("cwd") or pathlib.Path.cwd()),
                      cwd=cwd, stdin_text=json.dumps(dict(payload)))
    verdict = "met" if result.returncode == 0 and not result.timed_out else "error"
    return UnlazyResult(result.returncode, result.stdout, result.stderr, verdict)
