# 작업 폴더(scratch) — OAH 가 만드는 임시 작업 폴더를 사용자 전용 OAH 폴더 아래에만 두고 그 폴더를 프로세스마다 한 번 증명하며, 안전하지 않으면 시스템 임시 폴더로 물러서지 않고 OAH_SCRATCH_UNSAFE 로 멈춘다.
"""OAH scratch lives only under a user-private OAH folder, never the system temp.

The base is per user:

    Windows   %LOCALAPPDATA%\\oah\\scratch
    macOS     ~/Library/Caches/oah/scratch
    others    ${XDG_CACHE_HOME:-~/.cache}/oah/scratch   (a relative XDG_CACHE_HOME is ignored, as the XDG spec says)

The first use in a process proves the base with the landed verifiers, in this order: the path is absolute and bounded;
unlazy's ancestry check (no other account can replace a component that exists); the missing components are created
owner-only; the ancestry check again over the whole chain; then the base itself must be owner-only (init's Windows
descriptor check, or mode 0700 and owned by this user on POSIX). A failure raises ScratchError (OAH_SCRATCH_UNSAFE),
naming the base, the reason and the fix. Nothing falls back to the system temp and nothing is repaired: an existing
folder with the wrong owner or permissions is refused, never changed.

Each child is a fresh mkdtemp folder inside the proven base: mode 0700 on POSIX; on Windows it keeps the base's
owner-only DACL (newer Pythons give it their own protected owner, SYSTEM and Administrators DACL). A `root=` (the
review and correction test seam) goes through the same proof. A folder that a stopped run could not remove stays until
someone deletes it; `oah doctor` lists them (`leftovers`), and nothing here deletes them.
"""
from __future__ import annotations

import os
import pathlib
import stat
import sys
import tempfile
import threading
from typing import Mapping

from oah import unlazy

CODE = "OAH_SCRATCH_UNSAFE"
MAX_PARTS, MAX_BYTES = 64, 1024  # the bounds of the design task-context roots
_REPARSE_POINT = 0x400
_proven: set[str] = set()
_lock = threading.Lock()


def _fix(platform: str) -> str:
    if platform == "win32":
        return ("make sure only your account, SYSTEM and Administrators can change that folder and the folders "
                "above it and that only your account can use the scratch folder itself, or point LOCALAPPDATA at "
                "your own AppData\\Local folder, then retry")
    return ("make that folder and the folders above it belong to your account (or root) with no write access for "
            "other accounts, and the scratch folder itself owner-only (chmod 700), then retry")


class ScratchError(OSError):
    """The scratch base, or a passed root, cannot be proven private; nothing was created in the system temp."""

    code = CODE

    def __init__(self, base: os.PathLike | str, reason: str, platform: str | None = None) -> None:
        self.base = str(base)
        self.reason = reason
        self.detail = (f"cannot use the private scratch folder {self.base}: {reason}; "
                       f"{_fix(sys.platform if platform is None else platform)}")
        super().__init__(f"{CODE}: {self.detail}")


def _home(env: Mapping[str, str], platform: str) -> pathlib.Path:
    home = env.get("HOME", "")
    if home:
        return pathlib.Path(home)
    try:
        import pwd

        return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError) as exc:
        raise ScratchError("~", f"HOME is not set and the account's home cannot be read ({exc})", platform) from exc


def base_path(env: Mapping[str, str] | None = None, platform: str | None = None) -> pathlib.Path:
    """Where this user's scratch base is, by the rule above; it creates and proves nothing."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        local = env.get("LOCALAPPDATA", "")
        if not local or not pathlib.Path(local).is_absolute():
            raise ScratchError("%LOCALAPPDATA%\\oah\\scratch", "LOCALAPPDATA is not set to an absolute folder",
                               platform)
        return pathlib.Path(local) / "oah" / "scratch"
    if platform == "darwin":
        folder = _home(env, platform) / "Library" / "Caches" / "oah" / "scratch"
    else:
        cache = env.get("XDG_CACHE_HOME", "")
        root = pathlib.Path(cache) if cache and os.path.isabs(cache) else _home(env, platform) / ".cache"
        folder = root / "oah" / "scratch"
    if not folder.is_absolute():
        raise ScratchError(folder, "the folder is not an absolute path; set HOME to your home folder", platform)
    return folder


def _owner_only(folder: pathlib.Path, platform: str) -> None:
    if platform == "win32":
        from oah import init  # imported here: init itself imports much of oah

        init._verify_windows_private_descriptor(unlazy._windows_security_descriptor(folder), directory=True)
        return
    info = folder.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("the folder is a link or not a directory")
    if info.st_uid != os.getuid():
        raise OSError("the folder is not owned by your account")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o700:
        raise OSError(f"the folder's mode is {mode:04o}, but only your account may use it (0700)")


def _proven_folder(folder: pathlib.Path, platform: str) -> pathlib.Path:
    """Prove `folder` once per process (creating what is missing owner-only) and return its resolved path."""
    if not folder.is_absolute():
        raise ScratchError(folder, "the folder is not an absolute path", platform)
    too_long = f"the path is too long or not normalized (at most {MAX_PARTS} parts and {MAX_BYTES} bytes)"
    if ".." in folder.parts or len(folder.parts) > MAX_PARTS or len(os.fsencode(folder)) > MAX_BYTES:
        raise ScratchError(folder, too_long, platform)
    try:
        resolved = pathlib.Path(os.path.realpath(folder))
    except (OSError, ValueError) as exc:
        raise ScratchError(folder, f"the path cannot be resolved ({exc})", platform) from exc
    if len(resolved.parts) > MAX_PARTS or len(os.fsencode(resolved)) > MAX_BYTES:
        raise ScratchError(resolved, too_long, platform)
    key = str(resolved)
    with _lock:
        if key in _proven and os.path.isdir(resolved) and not os.path.islink(resolved):
            return resolved
        _proven.discard(key)
        try:
            unlazy._verify_state_ancestry(resolved, platform=platform)
            unlazy._private_makedirs(resolved)
            unlazy._verify_state_ancestry(resolved, platform=platform)
            _owner_only(resolved, platform)
        except unlazy.UnlazyError as exc:
            raise ScratchError(resolved, getattr(exc, "reason", None) or str(exc), platform) from exc
        except OSError as exc:
            raise ScratchError(resolved, str(exc), platform) from exc
        _proven.add(key)
    return resolved


def base(env: Mapping[str, str] | None = None, platform: str | None = None) -> pathlib.Path:
    """The proven scratch base, created owner-only on first use."""
    platform = sys.platform if platform is None else platform
    return _proven_folder(base_path(env, platform), platform)


def _parent(root: os.PathLike | str | None) -> str:
    return str(base() if root is None else _proven_folder(pathlib.Path(root), sys.platform))


def make(prefix: str, *, root: os.PathLike | str | None = None) -> pathlib.Path:
    """A new, empty, owner-only folder in the proven base (or the proven `root`); the caller removes it."""
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=_parent(root)))


def directory(prefix: str, *, root: os.PathLike | str | None = None,
              ignore_cleanup_errors: bool = False) -> tempfile.TemporaryDirectory:
    """`make` as a TemporaryDirectory: `with directory(prefix) as name:` yields the path text and removes the folder."""
    return tempfile.TemporaryDirectory(prefix=prefix, dir=_parent(root), ignore_cleanup_errors=ignore_cleanup_errors)


def leftovers(env: Mapping[str, str] | None = None, platform: str | None = None) -> dict:
    """What is left in the scratch base, for `oah doctor`: the base, whether it exists, how many entries sit directly
    in it, the total size of the regular files below them and how many entries could not be read.

    Read only: it never creates, proves or removes anything, and it follows no link or junction."""
    folder = base_path(env, platform)
    report = {"base": str(folder), "exists": False, "count": 0, "bytes": 0, "unreadable": 0}
    try:
        with os.scandir(folder) as found:
            entries = list(found)
    except FileNotFoundError:
        return report
    except OSError:
        report.update(exists=True, unreadable=1)
        return report
    report.update(exists=True, count=len(entries))
    while entries:
        entry = entries.pop()
        try:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
                continue
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(entry.path) as found:
                    entries.extend(found)
            elif stat.S_ISREG(info.st_mode):
                report["bytes"] += info.st_size
        except OSError:
            report["unreadable"] += 1
    return report
