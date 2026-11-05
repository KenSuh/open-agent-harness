# 출처 상태 저장 — 상태 폴더 아래 경로 배치, 짧은 잠금 하나, 크기·깊이 제한 JSON 읽기, 심볼릭 링크 거부, 내용 주소 파일의 생성 전용 쓰기와 읽을 때 다시 해시하기를 맡는다.
"""Where source observations live: `<state>/sources/`, never inside the working tree.

    registry.json                 registered sources (one file, replaced atomically)
    heads/<id>.json               one source's pointers, times and history (replaced atomically: the commit point)
    heads/removed/<id>.<reg>.json the last head of a removed registration
    snapshots/<h2>/<hex>.json     immutable observation manifests, named by the sha256 of their bytes
    blobs/<h2>/<hex>              immutable bytes (license texts, selected files, documents, release notes)
    designs.json                  which design revision referenced which snapshot (a rebuildable cache)
    dismissals.json               proposals a person dismissed, with the reason
    assessments/<h2>/<hex>.json   immutable Jev assessments of proposals
    schedule.json                 the last scheduled run

Every read enforces what writes promise: names are validated, a symbolic link or anything but a regular file is
refused, sizes are bounded, JSON is parsed with depth, number and duplicate-key limits, and content-addressed files
are hashed again against their names. A bad file is a typed `StateProblem` for that file only; its bytes are kept.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import stat
from typing import Mapping

from oah.atomic import StorageError, write_json_atomic
from oah.lockfile import StateLock
from oah.wiki.base import publish_new

FOLDER = "sources"
LOCK_NAME = "sources.lock"
LOCK_TIMEOUT = 10.0
MAX_DEPTH = 32
MAX_INT_DIGITS = 20
REGISTRY_LIMIT = 1 << 20
HEAD_LIMIT = 256 * 1024
MANIFEST_LIMIT = 1 << 20
DESIGNS_LIMIT = 4 << 20
DISMISSALS_LIMIT = 1 << 20
ASSESSMENT_LIMIT = 256 * 1024
SCHEDULE_LIMIT = 64 * 1024
BLOB_LIMIT = 8 << 20
_HEX = re.compile(r"[0-9a-f]{64}")
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")


class StateProblem(Exception):
    """One state file oah cannot use. `code` is a closed word; the file's bytes are never changed."""

    def __init__(self, code: str, rel: str, detail: str) -> None:
        self.code, self.rel, self.detail = code, rel, detail
        super().__init__(f"OAH_SOURCES_STATE_CORRUPT: {rel}: {detail}; oah never repairs or resets it. Move the "
                         "file aside to start that part again")


def _pairs(items: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in items]
    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice")
    return dict(items)


def _constant(name: str) -> object:
    raise ValueError(f"{name} is not a finite number")


def _int(text: str) -> int:
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise ValueError("an integer is too long")
    return int(text)


def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("a number is not finite")
    return value


def _depth_ok(value: object, limit: int) -> bool:
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > limit:
            return False
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return True


def loads(data: bytes, rel: str, *, max_depth: int = MAX_DEPTH) -> object:
    """Strict, bounded JSON: UTF-8, no duplicate keys, no NaN/Infinity, short integers, limited nesting."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise StateProblem("not-utf8", rel, "is not UTF-8") from None
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_int=_int,
                           parse_float=_float)
    except RecursionError:
        raise StateProblem("too-deep", rel, "nests too deeply") from None
    except ValueError as exc:
        raise StateProblem("not-json", rel, f"is not valid JSON ({str(exc)[:80]})") from None
    if not _depth_ok(value, max_depth):
        raise StateProblem("too-deep", rel, f"nests deeper than {max_depth} levels")
    return value


def canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest_of(data: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(data)).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_bytes(data: Mapping) -> bytes:
    """The stored bytes of an immutable record; its name is their sha256 (`record_digest`)."""
    return (json.dumps(dict(data), ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False) + "\n"
            ).encode("utf-8")


def record_digest(data: Mapping) -> str:
    return "sha256:" + sha256_hex(record_bytes(data))


def is_hex(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX.fullmatch(value))


def hex_of(digest: object) -> str | None:
    """The hex part of a `sha256:<hex>` digest, or None."""
    if isinstance(digest, str) and digest.startswith("sha256:") and is_hex(digest[7:]):
        return digest[7:]
    return None


def _is_link(path: pathlib.Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


class State:
    """The sources folder of one clone's state folder."""

    def __init__(self, state_dir: os.PathLike | str) -> None:
        self.root = pathlib.Path(state_dir) / FOLDER

    def lock(self) -> StateLock:
        return StateLock(self.root / LOCK_NAME, timeout=LOCK_TIMEOUT)

    def path(self, rel: str) -> pathlib.Path:
        """The path of rel below the sources folder; refuses odd names and symbolic links on the way."""
        parts = rel.split("/")
        if not parts or any(not _PART.fullmatch(part) or part in (".", "..") for part in parts):
            raise StateProblem("bad-name", rel, "is not a name oah writes")
        path = self.root
        if _is_link(path):
            raise StateProblem("symlink", FOLDER, "is a symbolic link")
        for part in parts:
            path = path / part
            if _is_link(path):
                raise StateProblem("symlink", rel, "is or passes through a symbolic link")
        return path

    def read(self, rel: str, limit: int) -> bytes | None:
        """The bytes of a regular file, or None when it does not exist."""
        path = self.path(rel)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if _is_link(path):
                raise StateProblem("symlink", rel, "is a symbolic link") from None
            raise StorageError(path, exc) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise StateProblem("not-a-file", rel, "is not a regular file")
            if info.st_size > limit:
                raise StateProblem("too-large", rel, f"is larger than {limit} bytes")
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise StateProblem("too-large", rel, f"is larger than {limit} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def read_json(self, rel: str, limit: int) -> object | None:
        data = self.read(rel, limit)
        return None if data is None else loads(data, rel)

    def write_json(self, rel: str, data: Mapping) -> None:
        """Replace a mutable state file atomically (the caller holds the lock)."""
        path = self.path(rel)
        write_json_atomic(path, data)

    def exists(self, rel: str) -> bool:
        path = self.path(rel)
        return os.path.lexists(path)

    # content-addressed files ------------------------------------------------------------------------

    @staticmethod
    def _addressed(kind: str, hex_digest: str, suffix: str = "") -> str:
        return f"{kind}/{hex_digest[:2]}/{hex_digest}{suffix}"

    def put_blob(self, data: bytes) -> str:
        """Store bytes create-only; returns their sha256 hex. An existing file must hold the same bytes."""
        hex_digest = sha256_hex(data)
        rel = self._addressed("blobs", hex_digest)
        path = self.path(rel)
        if not publish_new(path, data):
            if self.read(rel, BLOB_LIMIT) != data:
                raise StateProblem("digest-mismatch", rel, "holds other bytes than its name says")
        return hex_digest

    def get_blob(self, hex_digest: str, limit: int = BLOB_LIMIT) -> bytes | None:
        if not is_hex(hex_digest):
            raise StateProblem("bad-name", str(hex_digest)[:80], "is not a sha256 name")
        rel = self._addressed("blobs", hex_digest)
        data = self.read(rel, limit)
        if data is not None and sha256_hex(data) != hex_digest:
            raise StateProblem("digest-mismatch", rel, "holds other bytes than its name says")
        return data

    def put_record(self, kind: str, data: Mapping) -> str:
        """Store an immutable JSON record (snapshots, assessments) create-only; returns `sha256:<hex>`."""
        body = record_bytes(data)
        hex_digest = sha256_hex(body)
        rel = self._addressed(kind, hex_digest, ".json")
        path = self.path(rel)
        if not publish_new(path, body):
            if self.read(rel, MANIFEST_LIMIT) != body:
                raise StateProblem("digest-mismatch", rel, "holds other bytes than its name says")
        return "sha256:" + hex_digest

    def get_record(self, kind: str, digest: str, limit: int = MANIFEST_LIMIT) -> dict | None:
        hex_digest = hex_of(digest)
        if hex_digest is None:
            raise StateProblem("bad-name", str(digest)[:80], "is not a sha256 digest")
        rel = self._addressed(kind, hex_digest, ".json")
        data = self.read(rel, limit)
        if data is None:
            return None
        if sha256_hex(data) != hex_digest:
            raise StateProblem("digest-mismatch", rel, "holds other bytes than its name says")
        value = loads(data, rel)
        if not isinstance(value, dict):
            raise StateProblem("bad-record", rel, "is not a JSON object")
        return value
