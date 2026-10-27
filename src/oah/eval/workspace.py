# 평가 작업 공간 — 시작 스냅샷의 트리만으로 커밋 하나짜리 새 저장소를 만들고, 변형 실행 전에 정답·원래 이력·금지 파일을 검사한다.
from __future__ import annotations

import hashlib
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
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Iterable, Mapping, Sequence

from oah.engines.proc import RunResult, run_command
from oah.eval.case import Case, file_sha256
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


def build_workspace(snapshot: Snapshot, root: pathlib.Path, *, config_text: str | None = None) -> Workspace:
    """A fresh repository with one parentless commit of the snapshot tree, plus inputs, requirement and output."""
    case = snapshot.case
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise EvalError("OAH_EVAL_WORKSPACE", f"{root} is not empty; a workspace must start from an empty folder")
    repo = root / "repo"
    _git_ok(root, "init", "-q", f"--initial-branch={BRANCH}", str(repo))
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
        config = repo / ".oah" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(config_text.encode("utf-8"))
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


def content_hashes(data: bytes) -> frozenset[str]:
    """SHA-256 of original bytes and UTF-8 BOM/CRLF-normalized copies."""
    plain = data.removeprefix(b"\xef\xbb\xbf")
    return frozenset(hashlib.sha256(value).hexdigest() for value in
                     (data, data.replace(b"\r\n", b"\n"), plain, plain.replace(b"\r\n", b"\n")))


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
    value = value.replace("\\", "/")
    return (value.casefold() if os.name == "nt" else value).encode("utf-8")


def oracle_path_checks(paths: Iterable[pathlib.Path]) -> list[dict]:
    """Retain exact/supplied and resolved path fingerprints, never oracle locations."""
    checks = {}
    for path in paths:
        for value in (str(path), str(path.absolute()), str(path.resolve())):
            raw = _path_bytes(value)
            checks[raw] = {"length": len(raw), "separator": raw.find(b"/"),
                           "sha256": hashlib.sha256(raw).hexdigest()}
    return list(checks.values())


def check_variant_channels(ws: Workspace, *, oracle_hashes: Iterable[str] = (),
                           oracle_paths: Sequence[Mapping] = (), prompt: bytes = b"",
                           argv: Sequence[str] = (), env: Mapping[str, str] | None = None,
                           config_text: str | None = None, plan_dir: str | None = None) -> dict[str, str]:
    """The single default-deny boundary for every enumerated variant channel.

    File references are literal argv values (also --key=value), and TOML/JSON config
    string values. Opaque/dynamic references refuse. Runtime discovery by a program
    is outside this contents scan, which is not a sandbox.
    """
    hashes = frozenset(oracle_hashes)
    dependencies: dict[str, str] = {}
    visited: set[tuple[pathlib.Path, bool]] = set()

    def refuse(channel: str, *, unchecked: bool = False) -> None:
        code = "OAH_EVAL_CHANNEL_UNCHECKED" if unchecked else "OAH_EVAL_ISOLATION"
        reason = "cannot be checked" if unchecked else "exposes an oracle's contents or location"
        raise EvalError(code, f"variant channel {channel} {reason}; remove the reference or supply readable "
                        "literal files and TOML/JSON configs, then preregister and run again. No variant was started")

    def check(channel: str, raw: bytes) -> None:
        if len(raw) > NORMALIZE_LIMIT:
            refuse(channel, unchecked=True)
        if content_hashes(raw) & hashes:
            refuse(channel)
        # Hash candidate substrings, so a path embedded in prose, code or --flag=value
        # is checked without keeping the path itself in the frozen registration.
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
                refuse(channel)

    def read(path: pathlib.Path, channel: str) -> bytes:
        try:
            if not path.is_file() or path.stat().st_size > NORMALIZE_LIMIT:
                refuse(channel, unchecked=True)
            raw = path.read_bytes()
            check(channel, raw)
            return raw
        except (OSError, ValueError, RuntimeError):
            refuse(channel, unchecked=True)

    def reference(value: str, channel: str, base: pathlib.Path, *, required: bool = False,
                  destination: bool = False, config: bool = False) -> None:
        check(channel, value.encode("utf-8"))
        if not value:
            return
        path = pathlib.Path(value)
        if any(part in value for part in ("{", "}", "$", "*", "?", "\x00", "://")) or value.startswith("~"):
            refuse(channel, unchecked=True)
        pathlike = (required or path.is_absolute() or "/" in value or "\\" in value or value.startswith(".")
                    or bool(path.suffix))
        if not pathlike and not (base / path).exists():
            return
        if channel == "config" and not path.is_absolute():
            # Engine config loaders disagree about the base of relative paths.
            refuse(channel, unchecked=True)
        try:
            path = path if path.is_absolute() else base / path
            check(channel, _path_bytes(str(path.resolve())))
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
            raw = read(path, channel)
            name = ("{workspace}/" + path.relative_to(ws.root).as_posix()
                    if path.is_relative_to(ws.root) else str(path.absolute()))
            dependencies[name] = hashlib.sha256(raw).hexdigest()
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
        check("prompt", prompt)
        for index, arg in enumerate(argv):
            check(f"argument[{index}]", arg.encode("utf-8"))
            if "\x00" in arg:
                refuse("argument", unchecked=True)
            if arg in ("-c", "-m", "--eval", "--command") or arg.startswith("@"):
                refuse("argument", unchecked=True)
            flag, equal, value = arg.partition("=")
            previous = argv[index - 1] if index else ""
            option = flag if equal else previous
            if arg.startswith("-") and not equal:
                if not re.fullmatch(r"--[A-Za-z][A-Za-z0-9_-]*|-[A-Za-z]", arg):
                    refuse("argument", unchecked=True)
                continue
            reference(value if equal else arg, "reference", ws.repo,
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
            if int(size) > NORMALIZE_LIMIT:
                refuse("git-blob", unchecked=True)
            # run_command decodes output; raw blobs must bypass text decoding and filters.
            git_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
            result = subprocess.run([*git_argv(), "-C", str(ws.repo), "cat-file", "blob", oid],
                                    capture_output=True, env=git_env, timeout=GIT_TIMEOUT)
            if result.returncode or len(result.stdout) != int(size):
                refuse("git-blob", unchecked=True)
            check("git-blob", result.stdout)
        for base in (ws.repo, ws.inputs, ws.requirement, ws.output):
            for path in _files(base):
                check("working-file", _path_bytes(str(path)))
                label = "working-file " + path.relative_to(ws.root).as_posix()
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
                   config_text: str | None = None, plan_dir: str | None = None) -> dict:
    """Refuse oracle copies, history, unsafe links or forbidden inputs before launching."""
    oracle_hashes = frozenset(oracle_hashes)
    case = snapshot.case
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
    found = set(_git_ok(ws.repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split())
    leaked = sorted(found & snapshot.history_objects)
    if leaked:
        problems.append(f"{len(leaked)} object(s) from the original history, such as {leaked[0]}")
    # core.symlinks=false on Windows materializes these as ordinary files.
    for entry in _git_ok(ws.repo, "ls-tree", "-rz", "HEAD").split("\0"):
        if not entry:
            continue
        info, name = entry.split("\t", 1)
        mode, _, oid = info.split()
        if mode == "120000":
            check_name(f"repo/{name}", name)
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
                                          plan_dir=plan_dir)
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
