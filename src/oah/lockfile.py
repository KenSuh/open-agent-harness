# 상태 잠금 — macOS 는 fcntl.flock, Windows 는 msvcrt.locking 으로 프로세스 사이 배타 잠금을 짧게 잡는다.
"""One exclusive, cross-process lock per state folder.

The operating system releases the lock when the holding process exits, so a crashed command can
never leave a stale lock behind. Only lock contention is retried; any other OS error is raised.
"""
from __future__ import annotations

import errno
import os
import pathlib
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_CONTENTION = {errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLOCK", errno.EAGAIN)}


class LockTimeout(Exception):
    code = "OAH_STATE_LOCK_TIMEOUT"


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _CONTENTION:
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


class StateLock:
    """Hold the state lock for one short critical section: `with StateLock(path): ...`."""

    def __init__(self, path: pathlib.Path, timeout: float = 10.0, poll: float = 0.05) -> None:
        self.path = pathlib.Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fd: int | None = None

    def __enter__(self) -> "StateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        try:
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{LockTimeout.code}: another oah command holds {self.path}; "
                                      f"waited {self.timeout:g}s")
                time.sleep(self.poll)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc_info: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)
