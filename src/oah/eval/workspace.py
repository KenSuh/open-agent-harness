# 평가 작업 공간 — 시작 스냅샷의 트리만으로 커밋 하나짜리 새 저장소를 만들고, 변형 실행 전에 정답·원래 이력·금지 파일을 검사한다.
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import shutil
import stat
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Iterable

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
    full_env = dict(os.environ, **env) if env else None
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


def _member_path(case: Case, name: str) -> pathlib.PurePosixPath:
    pure = pathlib.PurePosixPath(name.replace("\\", "/"))
    if (pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name) or not pure.parts
            or ".git" in pure.parts):
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
            for member in tar.getmembers():
                pure = _member_path(case, member.name)
                target = repo.joinpath(*pure.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    handle = tar.extractfile(member)
                    _write_member(target, handle.read() if handle else b"", bool(member.mode & 0o111))
                else:
                    raise _snapshot_error(case, f"archive member {member.name!r} is a link or special file; "
                                                "use a bundle snapshot for repositories with links")
        return
    with zipfile.ZipFile(case.snapshot_file) as archive:
        for info in archive.infolist():
            pure = _member_path(case, info.filename)
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
    shutil.copytree(case.inputs, inputs)
    requirement = root / f"requirement{case.requirement.suffix}"
    shutil.copyfile(case.requirement, requirement)
    output = root / "output"
    output.mkdir()
    return Workspace(root=root, repo=repo, inputs=inputs, requirement=requirement, output=output, commit=commit)


def content_hashes(data: bytes) -> frozenset[str]:
    """SHA-256 of the bytes as given and with CRLF line endings turned into LF."""
    return frozenset({hashlib.sha256(data).hexdigest(), hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()})


def _file_hashes(path: pathlib.Path) -> frozenset[str]:
    if path.stat().st_size > NORMALIZE_LIMIT:
        return frozenset({file_sha256(path)})
    return content_hashes(path.read_bytes())


def _files(base: pathlib.Path) -> Iterable[pathlib.Path]:
    if base.is_file():
        yield base
        return
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def scan_workspace(ws: Workspace, snapshot: Snapshot, *, oracle_hashes: Iterable[str] = ()) -> dict:
    """Refuse with OAH_EVAL_ISOLATION when the workspace holds an oracle copy, history or a forbidden file."""
    oracle_hashes = frozenset(oracle_hashes)
    case = snapshot.case
    problems: list[str] = []
    alternates = ws.repo / ".git" / "objects" / "info" / "alternates"
    if alternates.exists():
        problems.append("the repository borrows objects from elsewhere (.git/objects/info/alternates)")
    found = set(_git_ok(ws.repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split())
    leaked = sorted(found & snapshot.history_objects)
    if leaked:
        problems.append(f"{len(leaked)} object(s) from the original history, such as {leaked[0]}")
    for label, base in (("repo", ws.repo), ("inputs", ws.inputs), ("requirement", ws.requirement)):
        for path in _files(base):
            relative = path.relative_to(base).as_posix() if base.is_dir() else path.name
            shown = f"{label}/{relative}" if base.is_dir() else relative
            hashes = _file_hashes(path)
            if hashes & oracle_hashes:
                problems.append(f"a copy of the oracle file at {shown}")
            if snapshot.snapshot_sha256 in hashes:
                problems.append(f"a copy of the snapshot file, which holds the original history, at {shown}")
            if label == "repo" and relative.split("/", 1)[0] == ".git":
                continue
            for pattern in case.forbidden:
                if fnmatchcase(relative, pattern) or fnmatchcase(path.name, pattern):
                    problems.append(f"{shown} matches the forbidden pattern {pattern!r}")
    if problems:
        raise EvalError("OAH_EVAL_ISOLATION",
                        f"the workspace for case {case.id} is not isolated: " + "; ".join(problems) +
                        ". No variant was started. Remove these from the case folder or its snapshot, then run "
                        "again.")
    return {"objects": "checked", "oracle": "checked" if oracle_hashes else "not-checked",
            "forbidden_patterns": len(case.forbidden)}


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
