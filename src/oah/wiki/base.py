# 위키 공통 바탕 — 설정 기본값과 WikiSettings, 오류 코드를 가진 WikiError, 파일 읽기·새 파일 게시 도우미를 둔다.
from __future__ import annotations

import os
import pathlib
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Mapping

from oah.atomic import RETRY_SECONDS, RETRY_SLEEP, StorageError

DEFAULTS: dict[str, object] = {
    "dir": "docs/wiki",
    "link_roots": [],
    "index_line_limit": 120,
    "stale_days": 180,
    "rollover_trigger_bytes": 512_000,
    "rollover_target_bytes": 409_600,
    "new_broken_links": "warn",
}
EXIT_REFUSED, EXIT_ENVIRONMENT = 1, 69


class WikiError(Exception):
    """A refusal or environment problem; the message starts with the OAH_* code."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class WikiSettings:
    dir: str = "docs/wiki"
    link_roots: tuple[str, ...] = field(default_factory=tuple)
    index_line_limit: int = 120
    stale_days: int = 180
    rollover_trigger_bytes: int = 512_000
    rollover_target_bytes: int = 409_600
    new_broken_links: str = "warn"

    def __post_init__(self) -> None:
        object.__setattr__(self, "link_roots", tuple(self.link_roots))
        problems = []
        if not isinstance(self.dir, str) or not self.dir.strip():
            problems.append("dir must be a folder path")
        for name in ("index_line_limit", "stale_days", "rollover_trigger_bytes", "rollover_target_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                problems.append(f"{name} must be a positive whole number")
        if self.new_broken_links not in ("warn", "block"):
            problems.append('new_broken_links must be "warn" or "block"')
        if not all(isinstance(item, str) and item for item in self.link_roots):
            problems.append("link_roots must be a list of folder paths")
        if problems:
            raise WikiError("OAH_CONFIG_INVALID", "[wiki] " + "; ".join(problems)
                            + ". Fix .oah/config.toml and retry.", EXIT_ENVIRONMENT)

    @classmethod
    def from_mapping(cls, section: Mapping[str, object]) -> "WikiSettings":
        merged = dict(DEFAULTS, **section)
        return cls(**{name: merged[name] for name in DEFAULTS})

    def lint_config(self) -> dict[str, object]:
        """The settings that change what lint counts; hygiene resets its baseline when they change."""
        data = asdict(self)
        return {name: (list(data[name]) if name == "link_roots" else data[name])
                for name in ("dir", "link_roots", "index_line_limit", "stale_days")}


def read_bytes(path: pathlib.Path) -> bytes | None:
    """The file's bytes, or None when it does not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageError(path, exc) from exc


def fsync_folder(folder: pathlib.Path) -> None:
    if os.name == "nt":
        return  # Windows cannot open a folder for fsync; NTFS journals the rename.
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_new(path: pathlib.Path, data: bytes) -> bool:
    """Durably create path with data, never replacing an existing file. False when path already exists.

    The bytes go to a temp file first, so a crash never leaves a partial file at path.
    """
    folder = path.parent
    temp = folder / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + RETRY_SECONDS
        while True:
            try:
                if os.name == "nt":
                    os.rename(temp, path)  # Windows rename refuses an existing target.
                else:
                    os.link(temp, path)  # link refuses an existing target; rename would replace it.
                break
            except FileExistsError:
                return False
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(RETRY_SLEEP)
        fsync_folder(folder)
        return True
    except OSError as exc:
        raise StorageError(path, exc) from exc
    finally:
        try:
            temp.unlink()
        except OSError:
            pass
