# 원격 ref 광고 수집기 — git ls-remote 출력을 줄 수·바이트·시간 상한 안에서 파이프로 읽고, 형식이 틀리거나 잘리거나 넘치면 거부한다.
"""Bounded `git ls-remote` for source checks.

The kit's `run_command` buffers a child's whole stdout. A tag advertisement can be large, so this reads it through a
pipe with explicit caps (100,000 rows, 16 MiB, a deadline) and kills the process tree when one is exceeded. Git runs
with the kit's credential-free configuration and redirects off, so a moved repository fails instead of answering
from its new place. Only a complete, exit-zero, well-formed advertisement is returned; anything else is `RefError`
with a fixed code and no captured stderr text.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import os
import re
import subprocess
import tempfile
import threading
from typing import Mapping, Sequence

from oah.engines.proc import REAP_SECONDS, _WindowsJob, kill_tree
from oah.paths import git_argv
from oah.toolkit.catalog import CatalogError, validate_repository
from oah.toolkit.fetch import GIT_SAFE_CONFIG, _git_env

ROW_LIMIT = 100_000
BYTE_LIMIT = 16 << 20
TIMEOUT = 60.0
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REF = re.compile(r"HEAD|refs/[\x21-\x7e\u0080-\U0010ffff]{1,1000}")
CODES = ("ls-remote-failed", "ls-remote-timeout", "ls-remote-overflow", "ls-remote-malformed")


class RefError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True)
class Advertisement:
    raw: bytes
    rows: int
    started_at: str
    ended_at: str
    refs: Mapping[str, str]  # ref name -> object id, peeled entries as "<ref>^{}"

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse(raw: bytes, *, tags_only: bool, row_limit: int = ROW_LIMIT) -> dict[str, str]:
    """Ref name -> object id from exact ls-remote stdout; RefError('ls-remote-malformed') on any doubt."""
    if not raw:
        return {}
    if not raw.endswith(b"\n"):
        raise RefError("ls-remote-malformed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RefError("ls-remote-malformed") from None
    lines = text[:-1].split("\n")
    if len(lines) > row_limit:
        raise RefError("ls-remote-overflow")
    refs: dict[str, str] = {}
    width = None
    for line in lines:
        oid, tab, name = line.partition("\t")
        if not tab or not _OID.fullmatch(oid) or not _REF.fullmatch(name) or name in refs:
            raise RefError("ls-remote-malformed")
        if width is None:
            width = len(oid)
        if len(oid) != width or (tags_only and not name.startswith("refs/tags/")):
            raise RefError("ls-remote-malformed")
        refs[name] = oid
    for name in refs:
        if name.endswith("^{}") and name[:-3] not in refs:
            raise RefError("ls-remote-malformed")
    return refs


def ls_remote(repository: str, patterns: Sequence[str] = (), *, tags: bool = False, timeout: float = TIMEOUT,
              row_limit: int = ROW_LIMIT, byte_limit: int = BYTE_LIMIT) -> Advertisement:
    """One complete advertisement within the caps, or RefError."""
    try:
        validate_repository(repository)
    except CatalogError:
        raise RefError("ls-remote-failed") from None
    if timeout <= 0:
        raise RefError("ls-remote-timeout")
    argv = [*git_argv(), *GIT_SAFE_CONFIG, "-c", "http.followRedirects=false", "ls-remote"]
    if tags:
        argv.append("--tags")
    argv += ["--", repository, *patterns]
    started = _now()
    chunks: list[bytes] = []
    state = {"total": 0, "overflow": False}
    options: dict = {}
    job = None
    if os.name == "nt":
        job = _WindowsJob()
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x4  # CREATE_SUSPENDED
    else:
        options["start_new_session"] = True
    with _git_env() as env, tempfile.TemporaryDirectory(prefix="oah-ls-remote-") as scratch:
        proc = subprocess.Popen([str(part) for part in argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=scratch, env=env, **options)
        try:
            if job is not None:
                job.start(proc)

            def reader() -> None:
                while True:
                    block = proc.stdout.read(65536)
                    if not block:
                        return
                    state["total"] += len(block)
                    if state["total"] > byte_limit:
                        state["overflow"] = True
                        return
                    chunks.append(block)

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            thread.join(timeout)
            timed_out = thread.is_alive()
            if timed_out or state["overflow"]:
                kill_tree(proc)
            thread.join(REAP_SECONDS)
            try:
                code = proc.wait(timeout=REAP_SECONDS)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                code = None
        finally:
            if proc.poll() is None:
                kill_tree(proc)
            if proc.stdout is not None:
                proc.stdout.close()
            if job is not None:
                job.close()
    if timed_out:
        raise RefError("ls-remote-timeout")
    if state["overflow"]:
        raise RefError("ls-remote-overflow")
    if code != 0:
        raise RefError("ls-remote-failed")
    raw = b"".join(chunks)
    refs = parse(raw, tags_only=tags, row_limit=row_limit)
    return Advertisement(raw=raw, rows=len(refs), started_at=started, ended_at=_now(), refs=refs)


def tag_map(advertisement: Advertisement, prefix: str = "") -> dict[str, dict]:
    """Tag name -> {"object": oid, "peeled": commit or None} for tags starting with the literal prefix."""
    tags: dict[str, dict] = {}
    for name, oid in advertisement.refs.items():
        if not name.startswith("refs/tags/") or name.endswith("^{}"):
            continue
        tag = name[len("refs/tags/"):]
        if tag.startswith(prefix):
            tags[tag] = {"object": oid, "peeled": advertisement.refs.get(name + "^{}")}
    return dict(sorted(tags.items(), key=lambda item: item[0].encode("utf-8")))
