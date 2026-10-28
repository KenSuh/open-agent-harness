# 평가 작업 공간 — 시작 스냅샷의 트리만으로 커밋 하나짜리 새 저장소를 만들고, 변형 실행 전에 정답·원래 이력·금지 파일을 검사한다.
from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import unicodedata
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Iterable, Mapping, Sequence

from oah.engines.proc import RunResult, run_command
from oah.eval.case import Case, file_sha256
from oah.eval.commands import _ResolvedArgv
from oah.eval.errors import EXIT_ENVIRONMENT, EvalError
from oah.paths import GitMissing, git_argv

GIT_TIMEOUT = 300.0
BRANCH = "main"
COMMIT_MESSAGE = "Starting snapshot\n"
# A fixed identity and date make the starting commit identical for every variant of a case.
IDENTITY = {"GIT_AUTHOR_NAME": "oah eval", "GIT_AUTHOR_EMAIL": "eval@oah.invalid",
            "GIT_COMMITTER_NAME": "oah eval", "GIT_COMMITTER_EMAIL": "eval@oah.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00"}
NORMALIZE_LIMIT = 64 << 20
ORACLE_BLOCK_MIN = 64
ORACLE_NORMALIZATION = "utf-lf-rstrip-trim-blank/1"
# Bounded, process-local evidence only: never channel bytes or oracle locations.
_CHANNEL_PASSES: dict[tuple, None] = {}
_ROLL_BASE, _ROLL_MOD = 257, 1 << 64
_HEX = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
VARIANT_CHANNELS = ("git-blob", "working-file", "prompt", "argument", "environment", "reference", "config")
RUNTIME_ENV = frozenset(("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
                         "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ"))


def variant_environment(env: Mapping[str, str], ws: Workspace | None = None) -> dict[str, str]:
    result = {key.upper(): value for key, value in env.items() if key.upper() in RUNTIME_ENV}
    if ws is not None:
        result.update(OAH_EVAL_INPUTS=str(ws.inputs), OAH_EVAL_REQUIREMENT=str(ws.requirement),
                      OAH_EVAL_OUTPUT=str(ws.output))
    return result


@dataclass(frozen=True)
class Snapshot:
    case: Case
    tree: str | None                 # known before building for bundles only
    tree_objects: frozenset[str]     # the snapshot tree's own trees and blobs
    history_objects: frozenset[str]  # every other object the snapshot file holds (history and later commits)
    extract: pathlib.Path | None     # bare repository holding the bundle's objects
    snapshot_sha256: str


@dataclass(frozen=True)
class Workspace:
    root: pathlib.Path
    repo: pathlib.Path
    inputs: pathlib.Path
    requirement: pathlib.Path
    output: pathlib.Path
    commit: str


def _git(cwd: pathlib.Path, *args: str, stdin: str | None = None, env: dict | None = None) -> RunResult:
    try:
        argv = git_argv()
    except GitMissing as exc:
        raise EvalError("OAH_EVAL_GIT", f"{exc}; install git and run again", EXIT_ENVIRONMENT) from exc
    # Like review snapshots, do not let caller context override -C. Config and
    # trace variables are excluded too: they can redirect writes or record data.
    full_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    full_env.update(env or {})
    return run_command(argv + ("-C", str(cwd), *args), timeout=GIT_TIMEOUT, env=full_env, stdin_text=stdin)


def _git_ok(cwd: pathlib.Path, *args: str, stdin: str | None = None, env: dict | None = None) -> str:
    result = _git(cwd, *args, stdin=stdin, env=env)
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[-1]
        raise EvalError("OAH_EVAL_GIT", f"git {args[0]} failed in {cwd}: {detail}; check that git works and "
                        "the folder is writable, then run again", EXIT_ENVIRONMENT)
    return result.stdout


def _snapshot_error(case: Case, message: str) -> EvalError:
    return EvalError("OAH_EVAL_SNAPSHOT", f"case {case.id}: {message}")


def open_snapshot(case: Case, scratch: pathlib.Path) -> Snapshot:
    """Read the case's snapshot file once; for a bundle, unpack it into a bare repository under `scratch`."""
    scratch = pathlib.Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    sha = file_sha256(case.snapshot_file)
    commit = case.snapshot_commit
    if case.snapshot_format == "bundle":
        extract = scratch / "history.git"
        _git_ok(scratch, "init", "-q", "--bare", str(extract))
        fetched = _git(extract, "fetch", "-q", "--no-tags", str(case.snapshot_file), "+refs/*:refs/oah-eval/*")
        if fetched.returncode != 0:
            raise _snapshot_error(case, f"{case.snapshot_file} is not a readable git bundle "
                                        f"({fetched.stderr.strip() or 'git fetch failed'}); recreate it with "
                                        "git bundle create")
        if _git(extract, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            raise _snapshot_error(case, f"commit {commit} is not in {case.snapshot_file.name}; include it in the "
                                        "bundle or correct [snapshot] commit")
        tree = _git_ok(extract, "rev-parse", f"{commit}^{{tree}}").strip()
        tree_objects = frozenset(line.split()[0] for line in
                                 _git_ok(extract, "rev-list", "--objects", tree).splitlines() if line.strip())
        everything = frozenset(_git_ok(extract, "cat-file", "--batch-all-objects",
                                       "--batch-check=%(objectname)").split())
        return Snapshot(case, tree, tree_objects, everything - tree_objects, extract, sha)
    recorded = _archive_commit(case)
    if recorded is not None and recorded != commit:
        raise _snapshot_error(case, f"{case.snapshot_file.name} was made from commit {recorded}, not {commit}; "
                                    "correct [snapshot] commit or recreate the archive")
    return Snapshot(case, None, frozenset(), frozenset({commit}), None, sha)


def _archive_commit(case: Case) -> str | None:
    """The commit id that `git archive` records in a tar header or zip comment, when present."""
    try:
        if case.snapshot_format == "tar":
            with tarfile.open(case.snapshot_file, "r:*") as tar:
                comment = tar.pax_headers.get("comment", "")
        else:
            with zipfile.ZipFile(case.snapshot_file) as archive:
                comment = archive.comment.decode("utf-8", "replace")
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise _snapshot_error(case, f"{case.snapshot_file} is not a readable {case.snapshot_format} archive "
                                    f"({exc}); recreate it") from exc
    comment = comment.strip().lower()
    return comment if _HEX.match(comment) else None


def _git_metadata(name: str) -> bool:
    # HFS ignores format characters; Win32 folds case, trims dots/spaces and
    # accepts NTFS streams and the short name GIT~1 for .git.
    folded = "".join(c for c in name.casefold() if unicodedata.category(c) != "Cf")
    folded = folded.split(":", 1)[0].rstrip(" .")
    return folded == ".git" or re.fullmatch(r"git~[0-9]+", folded) is not None


def _member_path(case: Case, name: str) -> pathlib.PurePosixPath:
    pure = pathlib.PurePosixPath(name.replace("\\", "/"))
    if (pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name) or not pure.parts
            or any(_git_metadata(part) or ":" in part or not part.rstrip(" .") for part in pure.parts)):
        raise _snapshot_error(case, f"archive member {name!r} is outside the snapshot or is git metadata; "
                                    "recreate the archive with git archive")
    return pure


def _write_member(target: pathlib.Path, data: bytes, executable: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    if executable:
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _extract_archive(case: Case, repo: pathlib.Path) -> None:
    """Unpack regular files and folders only; links and special files are refused."""
    if case.snapshot_format == "tar":
        with tarfile.open(case.snapshot_file, "r:*") as tar:
            members = [(member, _member_path(case, member.name)) for member in tar.getmembers()]
            for member, _ in members:
                if not (member.isdir() or member.isfile()):
                    raise _snapshot_error(case, "archive holds a link or special file; "
                                                "use a bundle snapshot for repositories with links")
            for member, pure in members:
                target = repo.joinpath(*pure.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    handle = tar.extractfile(member)
                    _write_member(target, handle.read() if handle else b"", bool(member.mode & 0o111))
        return
    with zipfile.ZipFile(case.snapshot_file) as archive:
        members = [(info, _member_path(case, info.filename)) for info in archive.infolist()]
        for info, _ in members:
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise _snapshot_error(case, "archive holds a link or special file; use a bundle snapshot "
                                            "for repositories with links")
        for info, pure in members:
            target = repo.joinpath(*pure.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                _write_member(target, archive.read(info), bool((info.external_attr >> 16) & 0o111))


def _inject_config(repo: pathlib.Path, text: str) -> None:
    """Replace snapshot-controlled links before creating the injected file; never follow them."""
    def remove(path: pathlib.Path) -> None:
        if _is_link(path):
            # Junctions need rmdir on Windows; neither operation traverses the target.
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                path.rmdir()
        elif path.is_dir():
            remove_tree(path)
        else:
            path.unlink()

    try:
        # Only .oah and config.toml belong to the injection. Never replace the repo root.
        if not repo.is_dir() or any(_is_link(path) for path in (repo, *repo.parents)):
            raise OSError("unsafe repository ancestor")
        parent = repo / ".oah"
        if os.path.lexists(parent) and (_is_link(parent) or not parent.is_dir()):
            remove(parent)
        parent.mkdir(exist_ok=True)
        if _is_link(parent):
            raise OSError("unsafe config parent")
        config = parent / "config.toml"
        if os.path.lexists(config):
            remove(config)
        # EXCL also refuses a link when O_NOFOLLOW is unavailable (native Windows).
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(config, flags, 0o600), "wb") as handle:
            handle.write(text.encode("utf-8"))
    except (OSError, ValueError, RuntimeError):
        raise EvalError("OAH_EVAL_ISOLATION", "the injected config destination is unsafe or unwritable; "
                        "replace snapshot links and special files with regular directories/files, then run again") from None


def build_workspace(snapshot: Snapshot, root: pathlib.Path, *, config_text: str | None = None) -> Workspace:
    """A fresh repository with one parentless commit of the snapshot tree, plus inputs, requirement and output."""
    case = snapshot.case
    root = pathlib.Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise EvalError("OAH_EVAL_WORKSPACE", f"{root} is not empty; a workspace must start from an empty folder")
    repo = root / "repo"
    _git_ok(root, "init", "-q", "--template=", f"--initial-branch={BRANCH}", str(repo))
    _git_ok(repo, "config", "core.autocrlf", "false")
    if snapshot.extract is not None:
        listing = _git_ok(snapshot.extract, "rev-list", "--objects", snapshot.tree)
        ids = "".join(line.split()[0] + "\n" for line in listing.splitlines() if line.strip())
        _git_ok(snapshot.extract, "pack-objects", "-q", str(repo / ".git" / "objects" / "pack" / "pack"), stdin=ids)
        tree = snapshot.tree
    else:
        _extract_archive(case, repo)
        _git_ok(repo, "add", "-A", "-f")
        tree = _git_ok(repo, "write-tree").strip()
    commit = _git_ok(repo, "commit-tree", tree, stdin=COMMIT_MESSAGE, env=IDENTITY).strip()
    _git_ok(repo, "update-ref", f"refs/heads/{BRANCH}", commit)
    _git_ok(repo, "reset", "-q", "--hard", commit)
    if config_text is not None:
        _inject_config(repo, config_text)
    inputs = root / "inputs"
    for path in (case.inputs, case.requirement, case.prompt):
        if _is_link(path):
            raise EvalError("OAH_EVAL_ISOLATION", "a case input root, requirement or prompt is a link; replace "
                            "it with a regular file or directory inside the case folder, then run again")
    # Python 3.11 copytree follows Windows junctions even with symlinks=True.
    for path in _files(case.inputs):
        if _is_link(path) and (path.is_dir() or not path.is_symlink()):
            raise EvalError("OAH_EVAL_ISOLATION", "case inputs contain a directory link or reparse point; "
                            "replace it with regular input files, then run again")
    shutil.copytree(case.inputs, inputs, symlinks=True)
    requirement = root / f"requirement{case.requirement.suffix}"
    shutil.copyfile(case.requirement, requirement)
    output = root / "output"
    output.mkdir()
    return Workspace(root=root, repo=repo, inputs=inputs, requirement=requirement, output=output, commit=commit)


def _text_encodings(header: bytes, size: int) -> tuple[str, ...]:
    if header.startswith((b"\xff\xfe", b"\xfe\xff")):
        return ("utf-8-sig", "utf-16")
    return ("utf-8-sig", "utf-16-le", "utf-16-be") if size % 2 == 0 else ("utf-8-sig",)


def _decoded_texts(data: bytes) -> Iterable[str]:
    """The shared set of strict UTF-8 and UTF-16 channel decodings."""
    for encoding in _text_encodings(data[:4], len(data)):
        try:
            yield data.decode(encoding)
        except UnicodeError:
            # Invalid unmarked candidates can be binary; marked UTF-16 refuses
            # separately at the channel boundary.
            continue


def _binary_boundary(error: UnicodeDecodeError) -> tuple[str, int]:
    # A NUL separates maximal strictly decodable runs. Literal NULs cannot occur
    # in TOML or filesystem paths, so a match can never cross a malformed region.
    return "\0", error.end


codecs.register_error("oah_eval_binary_boundary", _binary_boundary)


def _canonical_chunks(open_text) -> Iterable[bytes]:
    """One canonical form, with bounded reads even for unbounded lines/whitespace.

    A lookahead reader measures each line; a replay reader emits its trimmed
    prefix. Neither stores a whole line or a run of trailing whitespace. Readers
    translate CRLF and CR to LF before this function sees them.
    """
    chunk_size = min(NORMALIZE_LIMIT, 1 << 20)
    with open_text() as lookahead, open_text() as replay:
        started = False
        blank_lines = 0
        length = trimmed = 0

        def consume(count: int, emit: bool = False) -> Iterable[bytes]:
            while count:
                part = replay.read(min(count, chunk_size))
                if not part:
                    raise OSError("incomplete channel")
                count -= len(part)
                if emit:
                    yield part.encode("utf-8")

        def line(newline: bool) -> Iterable[bytes]:
            nonlocal started, blank_lines, length, trimmed
            if trimmed:
                if started:
                    count = blank_lines + 1
                    while count:
                        size = min(count, chunk_size)
                        yield b"\n" * size
                        count -= size
                yield from consume(trimmed, True)
                yield from consume(length - trimmed)
                started, blank_lines = True, 0
            else:
                yield from consume(length)
                if started:
                    blank_lines += 1
            if newline:
                yield from consume(1)
            length = trimmed = 0

        for chunk in iter(lambda: lookahead.read(chunk_size), ""):
            parts = chunk.split("\n")
            for index, part in enumerate(parts):
                stripped = part.rstrip()
                if stripped:
                    trimmed = length + len(stripped)
                length += len(part)
                if index < len(parts) - 1:
                    yield from line(True)
        yield from line(False)


def _scan_chunks(parts: Iterable[bytes]) -> Iterable[bytes]:
    """Batch canonical lines so each scan advances by a bounded byte chunk."""
    size = min(NORMALIZE_LIMIT, 1 << 20)
    buffer = bytearray()
    for part in parts:
        buffer.extend(part)
        while len(buffer) >= size:
            yield bytes(buffer[:size])
            del buffer[:size]
    if buffer:
        yield bytes(buffer)


def _normalized_text(text: str) -> bytes:
    return b"".join(_canonical_chunks(lambda: io.StringIO(text, newline=None)))


def content_hashes(data: bytes) -> frozenset[str]:
    """Raw-byte identity plus the single canonical form of every valid decoding."""
    return frozenset({hashlib.sha256(data).hexdigest(),
                      *(hashlib.sha256(_normalized_text(text)).hexdigest() for text in _decoded_texts(data))})


def _rolling_hash(data: bytes) -> int:
    value = 0
    for byte in data:
        value = (value * _ROLL_BASE + byte) % _ROLL_MOD
    return value


def oracle_block_checks(data: bytes) -> list[dict]:
    """Freeze only normalized block fingerprints, never expected-answer text or its location."""
    blocks = {_normalized_text(text) for text in _decoded_texts(data)}
    # An alternate byte-order interpretation must not promote a short oracle
    # into embedded matching merely by expanding its UTF-8 representation.
    if any(len(block) < ORACLE_BLOCK_MIN for block in blocks):
        return []
    return [{"length": len(block), "rolling": _rolling_hash(block),
             "sha256": hashlib.sha256(block).hexdigest()} for block in sorted(blocks)
            if len(block) >= ORACLE_BLOCK_MIN]


def _contains_block(data: bytes, block: Mapping) -> bool:
    """Rabin-Karp substring search with a linear SHA-256 verification budget."""
    size = block["length"]
    if size < ORACLE_BLOCK_MIN or size > len(data):
        return False
    value = _rolling_hash(data[:size])
    # Uniform binary padding has only one distinct window; avoid a Python loop
    # over hundreds of megabytes when it cannot match the rolling fingerprint.
    if value != block["rolling"] and data.count(data[:1]) == len(data):
        return False
    power = pow(_ROLL_BASE, size - 1, _ROLL_MOD)
    verified = 0
    for start in range(len(data) - size + 1):
        if value == block["rolling"]:
            verified += size
            if verified > len(data):
                raise EvalError("OAH_EVAL_CHANNEL_UNCHECKED", "oracle block comparison exceeded its work limit; "
                                "simplify the channel input, then preregister and run again. No variant was started")
            if hashlib.sha256(data[start:start + size]).hexdigest() == block["sha256"]:
                return True
        if start + size < len(data):
            value = ((value - data[start] * power) * _ROLL_BASE + data[start + size]) % _ROLL_MOD
    return False


def _file_hashes(path: pathlib.Path) -> frozenset[str]:
    if path.stat().st_size > NORMALIZE_LIMIT:
        return frozenset({file_sha256(path)})
    return content_hashes(path.read_bytes())


def _is_link(path: pathlib.Path) -> bool:
    # Reparse points also cover Windows junctions on Python 3.11.
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _files(base: pathlib.Path) -> Iterable[pathlib.Path]:
    """Files, directories and links, without traversing links (including junctions)."""
    if _is_link(base) or not base.is_dir():
        yield base
        return
    for path in sorted(base.iterdir()):
        yield path
        if not _is_link(path) and path.is_dir():
            yield from _files(path)


def _safe_link(path: pathlib.Path, base: pathlib.Path, target: str | None = None) -> bool:
    try:
        if target is not None:
            # Treat Windows absolute/drive paths as external on every host.
            if pathlib.PureWindowsPath(target).drive or target.startswith(("/", "\\")):
                return False
            resolved = (path.parent / target.replace("\\", "/")).resolve()
        else:
            resolved = path.resolve()
        return resolved.is_relative_to(base.resolve()) and not resolved.is_dir()
    except (OSError, ValueError, RuntimeError):
        return False


def _history_container(path: pathlib.Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(128)
    return _history_header(header)


def _history_header(header: bytes) -> bool:
    if (header[:8] in (b"PACK\x00\x00\x00\x02", b"PACK\x00\x00\x00\x03")
            or header.startswith((b"# v2 git bundle\n", b"# v3 git bundle\n"))):
        return True
    # Loose Git objects can carry the same history without refs, HEAD or packs.
    # Bound decompression: a compressed input must not allocate unbounded memory.
    try:
        loose = zlib.decompressobj().decompress(header, 64)
    except zlib.error:
        return False
    return re.match(rb"(?:commit|tree|blob|tag) [0-9]+\x00", loose) is not None


def _path_bytes(value: str) -> bytes:
    value = _normalized_text(value).decode("utf-8").replace("\\", "/")
    return (value.casefold() if os.name == "nt" else value).encode("utf-8")


def oracle_path_checks(paths: Iterable[pathlib.Path]) -> list[dict]:
    """Retain oracle and canonical grader-folder fingerprints, never their locations."""
    checks = {}
    for path in paths:
        for value in (str(path), str(path.absolute()), str(path.resolve())):
            raw = _path_bytes(value)
            checks[raw] = {"length": len(raw), "separator": raw.find(b"/"),
                           "sha256": hashlib.sha256(raw).hexdigest()}
        folder = path.resolve().parent
        raw = _path_bytes(str(folder))
        checks[raw] = {"length": len(raw), "separator": raw.find(b"/"),
                       "sha256": hashlib.sha256(raw).hexdigest(), "folder": True,
                       "ancestors": [hashlib.sha256(_path_bytes(str(parent))).hexdigest()
                                     for parent in (folder, *folder.parents)]}
    return list(checks.values())


def check_grader_separation(folders: Iterable[pathlib.Path], checks: Sequence[Mapping]) -> None:
    """Grader folders and case/workspace folders must have no resolved containment in either direction."""
    for folder in folders:
        resolved = pathlib.Path(folder).resolve()
        ancestors = {hashlib.sha256(_path_bytes(str(parent))).hexdigest()
                     for parent in (resolved, *resolved.parents)}
        own = hashlib.sha256(_path_bytes(str(resolved))).hexdigest()
        if any(check.get("folder") and (check["sha256"] in ancestors or own in check["ancestors"])
               for check in checks):
            raise EvalError("OAH_EVAL_ORACLE_FOLDER", "a grader-only folder overlaps a case or workspace; "
                            "move oracles to a separate folder disjoint from every case and workspace, "
                            "then preregister again")


def check_variant_channels(ws: Workspace, *, oracle_hashes: Iterable[str] = (),
                           oracle_paths: Sequence[Mapping] = (), prompt: bytes = b"",
                           argv: Sequence[str] = (), env: Mapping[str, str] | None = None,
                           config_text: str | None = None, plan_dir: str | None = None,
                           oracle_blocks: Sequence[Mapping] = ()) -> dict[str, str]:
    """The single default-deny boundary for every enumerated variant channel.

    File references are literal argv values (also --key=value), and TOML/JSON config
    string values. Opaque/dynamic references refuse. Runtime discovery by a program
    is outside this contents scan, which is not a sandbox.
    """
    hashes = frozenset(oracle_hashes)
    scan_context = hashlib.sha256(json.dumps(
        [sorted(hashes), [dict(item) for item in oracle_blocks], [dict(item) for item in oracle_paths],
         os.name, NORMALIZE_LIMIT],
        sort_keys=True).encode("utf-8")).digest()
    check_grader_separation((ws.root,), oracle_paths)
    dependencies: dict[str, str] = {}
    visited: set[tuple[pathlib.Path, bool]] = set()

    executable_files: set[pathlib.Path] = set()
    boundary = ws.root.resolve()

    def refuse(channel: str, *, unchecked: bool = False, history: bool = False) -> None:
        code = "OAH_EVAL_CHANNEL_UNCHECKED" if unchecked else "OAH_EVAL_ISOLATION"
        reason = ("cannot be checked" if unchecked else "contains a Git history container" if history
                  else "exposes an oracle or an existing external path")
        raise EvalError(code, f"variant channel {channel} {reason}; remove the reference or supply readable "
                        "literal files and TOML/JSON configs, then preregister and run again. No variant was started") from None

    def fingerprint_match(raw: bytes) -> bool:
        # Keep locations out of preregistration and errors, including resolved aliases.
        normal = raw.replace(b"\\", b"/")
        if os.name == "nt":
            normal = normal.decode("utf-8", "surrogateescape").casefold().encode("utf-8", "surrogateescape")
        for fingerprint in oracle_paths:
            size, separator = fingerprint["length"], fingerprint["separator"]
            if separator < 0:
                starts = range(max(0, len(normal) - size + 1))
            else:
                starts = (match.start() - separator for match in re.finditer(b"/", normal)
                          if separator <= match.start() <= len(normal) - size + separator)
            if any(hashlib.sha256(normal[start:start + size]).hexdigest() == fingerprint["sha256"]
                   for start in starts):
                return True
        return False

    def fingerprints(channel: str, raw: bytes) -> None:
        if fingerprint_match(raw):
            refuse(channel)

    def literal(channel: str, token: str, base: pathlib.Path) -> None:
        # NAME=VALUE can be a filename or an assignment. Check both interpretations.
        values = (token, token.partition("=")[2]) if "=" in token else (token,)
        for value in values:
            if not value:
                continue
            path = pathlib.Path(value)
            if not ("/" in value or "\\" in value or value.startswith("~")
                    or pathlib.PureWindowsPath(value).drive
                    or (base / path).exists()):
                continue
            # Foreign absolute paths cannot be resolved using this host's path rules.
            if os.name != "nt" and pathlib.PureWindowsPath(value).drive:
                refuse(channel, unchecked=True)
            candidate = path if path.is_absolute() else base / path
            try:
                resolved = pathlib.Path(os.path.realpath(candidate, strict=True))
                exists = True
            except FileNotFoundError:
                resolved = pathlib.Path(os.path.realpath(candidate))
                exists = False
            fingerprints(channel, _path_bytes(str(resolved)))
            if (exists and channel != "environment" and not resolved.is_relative_to(boundary)
                    and resolved not in executable_files):
                refuse(channel)

    # Fourfold overlap also covers UTF-8 byte-length changes from Windows case
    # folding. It always retains at least the longest canonical oracle form.
    overlap = 4 * max((item["length"] for item in (*oracle_blocks, *oracle_paths)), default=1)

    min_block = min((item["length"] for item in oracle_blocks), default=float("inf"))
    min_path = min((item["length"] for item in oracle_paths), default=float("inf"))
    # Windows Unicode case folding can expand UTF-8 bytes; test the folded
    # length in fingerprint_match rather than discarding a possible short input.
    min_region = min(min_block, min_path / 3 if os.name == "nt" else min_path)

    def text_match(normal: bytes) -> bool:
        if len(normal) < min_region:
            return False
        return (any(_contains_block(normal, block) for block in oracle_blocks)
                or fingerprint_match(normal))

    def stream(open_binary, channel: str) -> str:
        """Scan without a size refusal; retain only fixed chunks and fingerprint overlap."""
        digest = hashlib.sha256()
        header, size = b"", 0
        with open_binary() as handle:
            for raw in iter(lambda: handle.read(min(NORMALIZE_LIMIT, 1 << 20)), b""):
                if not header:
                    header = raw[:4]
                size += len(raw)
                digest.update(raw)
        raw_digest = digest.hexdigest()
        if raw_digest in hashes:
            refuse(channel)
        # Always reread/hash the actual bytes. Reuse a pass only for identical
        # bytes and the complete identical oracle/path comparison context.
        cache_key = (raw_digest, scan_context)
        if cache_key in _CHANNEL_PASSES:
            return raw_digest
        encodings = _text_encodings(header, size)
        invalid = set()
        if size % 2 and (hashes or oracle_blocks or oracle_paths):
            # An odd binary can still contain even-length UTF-16 text regions.
            invalid.update(("utf-16-le", "utf-16-be"))
        for encoding in encodings if hashes or oracle_blocks or oracle_paths or "utf-16" in encodings else ():
            digest = hashlib.sha256()
            tail = b""
            matched = False

            @contextmanager
            def open_text():
                with open_binary() as binary:
                    with io.TextIOWrapper(binary, encoding=encoding, newline=None) as text:
                        yield text

            try:
                for normal in _scan_chunks(_canonical_chunks(open_text)):
                    digest.update(normal)
                    window = tail + normal
                    matched = text_match(window) or matched
                    tail = window[-overlap:]
            except UnicodeError:
                if encoding == "utf-16":
                    refuse(channel, unchecked=True)
                invalid.add(encoding)
                continue
            if matched or digest.hexdigest() in hashes:
                refuse(channel)
        # Do not drop an executable's valid text regions just because other
        # bytes are binary. Strictly decoded runs use the same canonicalizer;
        # malformed sequences are hard boundaries, never replacement text.
        for encoding in invalid:
            @contextmanager
            def open_regions():
                with open_binary() as binary:
                    with io.TextIOWrapper(binary, encoding=encoding, newline=None,
                                          errors="oah_eval_binary_boundary") as text:
                        yield text

            tail = b""
            for normal in _scan_chunks(_canonical_chunks(open_regions)):
                window = tail + normal
                if any(text_match(region) for region in window.strip(b"\0").split(b"\0")
                       if region and len(region) >= min_region):
                    refuse(channel)
                tail = window[-overlap:]
        tail = b""
        with open_binary() as handle:
            for raw in iter(lambda: handle.read(min(NORMALIZE_LIMIT, 1 << 20)), b""):
                window = tail + raw
                fingerprints(channel, window)
                tail = window[-overlap:]
        if len(_CHANNEL_PASSES) >= 128:
            _CHANNEL_PASSES.pop(next(iter(_CHANNEL_PASSES)), None)
        _CHANNEL_PASSES[cache_key] = None
        return raw_digest

    def check(channel: str, raw: bytes) -> None:
        stream(lambda: io.BytesIO(raw), channel)

    def read(path: pathlib.Path, channel: str, *, config: bool = False) -> tuple[bytes | None, str]:
        try:
            if not path.is_file():
                refuse(channel, unchecked=True)
            if path.stat().st_size > NORMALIZE_LIMIT:
                digest = stream(lambda: path.open("rb"), channel)
                # Config parsing still needs the literal document after its scan;
                # the size threshold is never a reason to refuse a channel.
                return (path.read_bytes() if config else None), digest
            raw = path.read_bytes()
            check(channel, raw)
            return raw, hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, RuntimeError):
            refuse(channel, unchecked=True)

    def reference(value: str, channel: str, base: pathlib.Path, *, required: bool = False,
                  destination: bool = False, config: bool = False) -> None:
        check(channel, value.encode("utf-8"))
        literal(channel, value, base)
        if not value:
            return
        path = pathlib.Path(value)
        if any(part in value for part in ("{", "}", "$", "*", "?", "\x00", "://")) or value.startswith("~"):
            refuse(channel, unchecked=True)
        pathlike = (required or path.is_absolute() or "/" in value or "\\" in value
                    or bool(pathlib.PureWindowsPath(value).drive))
        if not pathlike and not (base / path).exists():
            return
        if channel == "config" and not path.is_absolute():
            # Engine config loaders disagree about the base of relative paths.
            refuse(channel, unchecked=True)
        try:
            path = path if path.is_absolute() else base / path
            fingerprints(channel, _path_bytes(str(path.resolve())))
            if not path.exists():
                if destination:
                    return
                refuse(channel, unchecked=True)
            if path.is_dir():
                if not destination or any(path.iterdir()):
                    # A directory is an open-ended reference, not an enumerated file.
                    refuse(channel, unchecked=True)
                return
            resolved = path.resolve(strict=True)
            if path.suffix.lower() in (".yaml", ".yml", ".ini", ".cfg", ".conf"):
                refuse("config", unchecked=True)
            parse_config = config or path.suffix.lower() in (".toml", ".json")
            identity = (resolved, parse_config)
            if identity in visited:
                return
            visited.add(identity)
            raw, digest = read(path, channel, config=parse_config)
            name = ("{workspace}/" + path.relative_to(ws.root).as_posix()
                    if path.is_relative_to(ws.root) else str(path.absolute()))
            dependencies[name] = digest
            if parse_config:
                config_values(raw, path.parent)
        except (OSError, ValueError, RuntimeError):
            refuse(channel, unchecked=True)

    def config_values(raw: bytes, base: pathlib.Path) -> None:
        check("config", raw)
        try:
            text = raw.decode("utf-8-sig")
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                data = json.loads(text)
        except (UnicodeError, ValueError):
            refuse("config", unchecked=True)

        def walk(value, key: str = "") -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    check("config", str(key).encode("utf-8"))
                    walk(item, str(key))
            elif isinstance(value, list):
                for item in value:
                    walk(item, key)
            elif isinstance(value, str):
                reference(value, "config", base,
                          required=any(word in key.lower() for word in ("file", "path", "include", "config")),
                          config=any(word in key.lower() for word in ("include", "config")))
        walk(data)

    try:
        if argv:
            executable_files.add(pathlib.Path(os.path.realpath(ws.repo / argv[0], strict=True)))
            if (isinstance(argv, _ResolvedArgv) and argv.resolution.kind == "npm-shim"
                    and len(argv.resolution.argv) == 2 and tuple(argv[:2]) == argv.resolution.argv):
                # Only proc's verified npm entry script joins the executable exception.
                # It still passes content/path checks and is frozen by reference() below.
                executable_files.add(pathlib.Path(os.path.realpath(ws.repo / argv[1], strict=True)))
        check("prompt", prompt)
        for index, arg in enumerate(argv):
            flag, equal, value = arg.partition("=")
            is_option = re.fullmatch(r"--[A-Za-z][A-Za-z0-9_-]*|-[A-Za-z]", flag) is not None
            if flag in ("-c", "-e", "-m", "--eval", "--command") or arg.startswith("@"):
                refuse("argument", unchecked=True)
            if "\x00" in arg:
                refuse("argument", unchecked=True)
            check("argument", arg.encode("utf-8"))
            previous = argv[index - 1] if index else ""
            option = flag if is_option else (previous if re.fullmatch(
                r"--[A-Za-z][A-Za-z0-9_-]*|-[A-Za-z]", previous) else "")
            if arg.startswith("-"):
                if not is_option:
                    refuse("argument", unchecked=True)
                if not equal:
                    continue
            reference(value if is_option and equal else arg, "reference", ws.repo,
                      required=index == 0 or any(word in option.lower() for word in ("file", "config", "include")),
                      destination=option in ("--output", "--output-dir", "--marker", "--record-dir"),
                      config="config" in option.lower())
        for key, value in (env or {}).items():
            if key not in RUNTIME_ENV and key not in ("OAH_EVAL_INPUTS", "OAH_EVAL_REQUIREMENT", "OAH_EVAL_OUTPUT"):
                refuse("environment", unchecked=True)
            if "\x00" in key or "\x00" in value or "=" in key:
                refuse("environment", unchecked=True)
            check("environment", key.encode("utf-8"))
            check("environment", value.encode("utf-8"))
        if config_text is not None:
            config_values(config_text.encode("utf-8"), pathlib.Path(plan_dir) if plan_dir else ws.repo)
        listing = _git_ok(ws.repo, "cat-file", "--batch-all-objects",
                          "--batch-check=%(objectname) %(objecttype) %(objectsize)")
        for line in listing.splitlines():
            oid, kind, size = line.split()
            if kind != "blob":
                continue
            # Raw Git blobs use a pipe, never subprocess capture_output, so
            # retained/unreferenced large blobs have the same bounded scan.
            git_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}

            @contextmanager
            def blob_reader():
                process = subprocess.Popen([*git_argv(), "-C", str(ws.repo), "cat-file", "blob", oid],
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_env)
                try:
                    yield process.stdout
                    if process.wait(timeout=GIT_TIMEOUT):
                        refuse("git-blob", unchecked=True)
                finally:
                    process.stdout.close()
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=GIT_TIMEOUT)

            with blob_reader() as handle:
                header = handle.read(128)
                if _history_header(header):
                    refuse("git-blob", history=True)
                if int(size) <= NORMALIZE_LIMIT:
                    raw = header + handle.read(int(size) + 1)
                    if len(raw) != int(size):
                        refuse("git-blob", unchecked=True)
                    check("git-blob", raw)
                    continue
                # Drain before wait: a child must never block on a full pipe.
                count = len(header)
                for part in iter(lambda: handle.read(1 << 20), b""):
                    count += len(part)
                if count != int(size):
                    refuse("git-blob", unchecked=True)
            stream(blob_reader, "git-blob")
        for base in (ws.repo, ws.inputs, ws.requirement, ws.output):
            for path in _files(base):
                check("working-file", _path_bytes(str(path)))
                label = "working-file"
                if _is_link(path):
                    check(label, _path_bytes(os.readlink(path)))
                    if not _safe_link(path, base if base.is_dir() else ws.root):
                        refuse("working-file", unchecked=True)
                elif path.is_file():
                    read(path, label)
                elif not path.is_dir():
                    refuse("working-file", unchecked=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        refuse("unreadable", unchecked=True)
    return dependencies


def scan_workspace(ws: Workspace, snapshot: Snapshot, *, oracle_hashes: Iterable[str] = (),
                   prompt: bytes | None = None, oracle_paths: Sequence[Mapping] = (),
                   argv: Sequence[str] = (), env: Mapping[str, str] | None = None,
                   config_text: str | None = None, plan_dir: str | None = None,
                   oracle_blocks: Sequence[Mapping] = ()) -> dict:
    """Refuse oracle copies, history, unsafe links or forbidden inputs before launching."""
    oracle_hashes = frozenset(oracle_hashes)
    case = snapshot.case
    check_grader_separation((case.folder, ws.root), oracle_paths)
    problems: list[str] = []

    def check_content(shown: str, hashes: frozenset[str]) -> None:
        if snapshot.snapshot_sha256 in hashes:
            problems.append(f"a copy of the snapshot file, which holds the original history, at {shown}")

    def check_name(shown: str, relative: str) -> None:
        for pattern in case.forbidden:
            if fnmatchcase(relative, pattern) or fnmatchcase(relative.rsplit("/", 1)[-1], pattern):
                problems.append(f"{shown} matches the forbidden pattern {pattern!r}")

    # Match the actual UTF-8 stdin bytes, including the runner's BOM removal.
    if prompt is None:
        prompt = case.prompt.read_bytes().decode("utf-8-sig").encode("utf-8")
    check_content("prompt", content_hashes(prompt))
    check_name("prompt", case.prompt.relative_to(case.folder).as_posix())
    alternates = ws.repo / ".git" / "objects" / "info" / "alternates"
    if alternates.exists():
        problems.append("the repository borrows objects from elsewhere (.git/objects/info/alternates)")
    retained = [line.split() for line in _git_ok(ws.repo, "cat-file", "--batch-all-objects",
                                                "--batch-check=%(objectname) %(objecttype)").splitlines()]
    found = {oid for oid, _ in retained}
    leaked = sorted(found & snapshot.history_objects)
    if leaked:
        problems.append(f"{len(leaked)} object(s) from the original history, such as {leaked[0]}")
    # Config injection can remove checkout nodes; unreachable trees remain readable too.
    retained_names = set()
    for oid, kind in retained:
        if kind == "tree":
            for entry in _git_ok(ws.repo, "ls-tree", "-rtz", oid).split("\0"):
                if entry:
                    retained_names.add(entry.split("\t", 1)[1])
    for name in sorted(retained_names):
        check_name(f"repo/{name}", name)
    # core.symlinks=false on Windows materializes these as ordinary files.
    for entry in _git_ok(ws.repo, "ls-tree", "-rz", "HEAD").split("\0"):
        if not entry:
            continue
        info, name = entry.split("\t", 1)
        mode, _, oid = info.split()
        if mode == "120000":
            target = _git_ok(ws.repo, "cat-file", "blob", oid)
            if not _safe_link(ws.repo / name, ws.repo, target):
                problems.append("a committed link leaves the repository or names a directory")
    for label, base in (("repo", ws.repo), ("inputs", ws.inputs), ("requirement", ws.requirement)):
        for path in _files(base):
            relative = path.relative_to(base).as_posix() if base.is_dir() else path.name
            shown = f"{label}/{relative}" if base.is_dir() else relative
            if label == "repo" and relative.split("/", 1)[0] == ".git":
                if _is_link(path):
                    if not _safe_link(path, ws.repo):
                        problems.append("a link in repository metadata leaves the repository or names a directory")
                elif path.is_file():
                    check_content(shown, _file_hashes(path))
                continue
            check_name(shown, relative)
            if _is_link(path):
                if not _safe_link(path, base if base.is_dir() else ws.root):
                    problems.append(f"a link at {shown} leaves its workspace input or names a directory")
                # Never follow links when reading content or reporting a refusal.
                continue
            if _git_metadata(path.name) or (path.is_dir() and (path / "objects").is_dir()
                                           and ((path / "HEAD").is_file() or (path / "refs").is_dir())):
                problems.append(f"embedded Git repository metadata at {shown}")
            if path.is_file():
                check_content(shown, _file_hashes(path))
                if _history_container(path):
                    problems.append(f"a Git history container at {shown}")
    if problems:
        raise EvalError("OAH_EVAL_ISOLATION",
                        f"the workspace for case {case.id} is not isolated: " + "; ".join(problems) +
                        ". No variant was started. Remove these from the case folder or its snapshot, then run "
                        "again.")
    dependencies = check_variant_channels(ws, oracle_hashes=oracle_hashes, oracle_paths=oracle_paths,
                                          prompt=prompt, argv=argv, env=env, config_text=config_text,
                                          plan_dir=plan_dir, oracle_blocks=oracle_blocks)
    return {"objects": "checked", "oracle": "checked" if oracle_hashes else "not-checked",
            "forbidden_patterns": len(case.forbidden), "channels": list(VARIANT_CHANNELS),
            "channel_files": dependencies}


def remove_tree(path: pathlib.Path) -> None:
    """Delete a folder, including git's read-only object files on Windows."""
    def retry(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if not pathlib.Path(path).exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)
