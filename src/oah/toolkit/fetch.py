# 업스트림 받기·스테이징 — 공개 저장소를 자격 증명 없이 얕게 받아, 지정 하위 경로·라이선스를 바이트 그대로 옮기고 UPSTREAM.json 을 쓴다.
"""Fetch a public upstream at a ref or commit and stage one bundled item from it.

Git runs with prompts and credential helpers turned off, so a fetch can never ask for or send a
credential. Files are read from the object store (`git cat-file`), never from a checkout, so neither
the upstream's .gitattributes nor the user's autocrlf can change a byte.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import shutil
import stat
from dataclasses import dataclass

from oah.atomic import write_json_atomic
from oah.engines.proc import RunResult, run_command
from oah.paths import git_argv
from oah.toolkit.catalog import Bundled, CatalogError, Upstream, validate_repository
from oah.toolkit.checks import identify_license, text_digest
from oah.toolkit.skillmeta import MARKER_FILE, UPSTREAM_FILE

FETCH_TIMEOUT = 600.0
GIT_SAFE_CONFIG = ("-c", "credential.helper=", "-c", "core.askPass=", "-c", "core.autocrlf=false",
                   "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                   "-c", "protocol.file.allow=always")


class FetchError(Exception):
    code = "OAH_SKILLS_FETCH_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class StageError(Exception):
    code = "OAH_SKILLS_STAGE_REFUSED"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


def git_env() -> dict[str, str]:
    """The caller's environment with every way of prompting for or supplying credentials disabled."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "GCM_")) or key in ("GIT_EXEC_PATH",)}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="", GCM_INTERACTIVE="never",
               GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(repo: pathlib.Path, *args: str, timeout: float = FETCH_TIMEOUT, stdin_text: str | None = None) -> RunResult:
    return run_command((*git_argv(), *GIT_SAFE_CONFIG, "-C", str(repo), *args), timeout=timeout,
                       env=git_env(), stdin_text=stdin_text)


def _checked(result: RunResult, what: str) -> str:
    if result.timed_out:
        raise FetchError(f"{what} timed out; check your network connection and retry")
    if result.returncode != 0:
        raise FetchError(f"{what} failed: {result.stderr.strip()[:300]}; check the network and that "
                         "the repository is public, then retry")
    return result.stdout


@dataclass(frozen=True)
class Tree:
    """One fetched commit in a scratch repository."""

    repo: pathlib.Path
    commit: str

    def entries(self, path: str) -> list[tuple[str, str, str]]:
        """(mode, blob id, repository path) for every entry at or below path ('' is the whole tree)."""
        args = ["ls-tree", "-r", "-z", "--full-tree", self.commit]
        if path:
            args += ["--", path]
        out = _checked(_git(self.repo, *args), f"listing {path or 'the tree'}")
        found = []
        for record in out.split("\0"):
            if not record:
                continue
            meta, name = record.split("\t", 1)
            mode, kind, blob = meta.split()
            if path and name != path and not name.startswith(path.rstrip("/") + "/"):
                continue
            found.append((mode, blob if kind == "blob" else "", name))
        return found

    def read(self, blob: str) -> bytes:
        result = run_command((*git_argv(), *GIT_SAFE_CONFIG, "-C", str(self.repo), "cat-file", "blob", blob),
                             timeout=FETCH_TIMEOUT, env=git_env(), encoding="latin-1")
        _checked(result, f"reading blob {blob}")
        return result.stdout.encode("latin-1")


def fetch(repository: str, ref: str, scratch: pathlib.Path, *, paths: tuple[str, ...] = (),
          timeout: float = FETCH_TIMEOUT) -> Tree:
    """Fetch ref (a branch, tag or full commit id) of a public repository into an empty scratch folder."""
    validate_repository(repository)
    scratch = pathlib.Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    _checked(_git(scratch, "init", "-q"), "git init")
    _checked(_git(scratch, "remote", "add", "origin", repository), "git remote add")
    _checked(_git(scratch, "fetch", "--quiet", "--no-tags", "--depth", "1", "--filter=blob:none",
                  "origin", ref, timeout=timeout), f"fetching {ref} from {repository}")
    commit = _checked(_git(scratch, "rev-parse", "--verify", "FETCH_HEAD^{commit}"), "reading FETCH_HEAD").strip()
    if paths:
        # A sparse checkout downloads the needed blobs in one request; the files themselves are
        # read back from the object store, so the checkout's line endings are irrelevant.
        _checked(_git(scratch, "sparse-checkout", "set", "--no-cone", *[f"/{p}" for p in paths]),
                 "git sparse-checkout")
        _checked(_git(scratch, "checkout", "-q", "--detach", commit, timeout=timeout), "fetching file contents")
    return Tree(scratch, commit)


def _write(path: pathlib.Path, data: bytes, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if executable and os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def stage_item(entry: Bundled, tree: Tree, dest: pathlib.Path, *, fetched: dt.date) -> Upstream:
    """Copy the item's subpath, license and notices from tree into an empty dest; write UPSTREAM.json.

    Refuses symlinks, submodules, reserved names, a license that is not the expected SPDX id, and a
    license file that would collide with a different file of the same name.
    """
    dest = pathlib.Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise StageError(f"{dest} is not empty")
    prefix = entry.subpath.rstrip("/") + "/" if entry.subpath else ""
    entries = tree.entries(entry.subpath)
    if not entries:
        raise StageError(f"{entry.repository} has no {entry.subpath or 'files'} at {tree.commit}")
    if entry.include:
        wanted = [prefix + path for path in entry.include]
        for path in wanted:
            if not any(name == path or name.startswith(path + "/") for _, _, name in entries):
                raise StageError(f"{entry.repository} has no {path} at {tree.commit}")
        entries = [e for e in entries if any(e[2] == path or e[2].startswith(path + "/") for path in wanted)]
    files: dict[str, str] = {}
    executables: list[str] = []
    contents: dict[str, bytes] = {}
    for mode, blob, name in entries:
        local = name[len(prefix):] if prefix else name
        if mode == "120000":
            raise StageError(f"{name} is a symbolic link; oah does not bundle links")
        if mode == "160000" or not blob:
            raise StageError(f"{name} is a submodule; oah does not bundle submodules")
        if local.split("/")[-1] in (UPSTREAM_FILE, MARKER_FILE):
            raise StageError(f"{name} uses a file name oah reserves")
        contents[local] = tree.read(blob)
        files[local] = blob
        if mode == "100755":
            executables.append(local)

    def extra(path: str) -> str:
        found = [(mode, blob) for mode, blob, name in tree.entries(path) if name == path]
        if not found or not found[0][1]:
            raise StageError(f"{entry.repository} has no {path} at {tree.commit}")
        if not prefix:
            local = path
        elif path.startswith(prefix):
            local = path[len(prefix):]
        else:
            local = path.rsplit("/", 1)[-1]
        data = tree.read(found[0][1])
        if local in contents and contents[local] != data:
            raise StageError(f"{path} would overwrite a different {local} in the item")
        contents[local], files[local] = data, found[0][1]
        if found[0][0] == "100755":
            executables.append(local)
        return local

    license_file = extra(entry.license_path)
    notice_files = tuple(extra(path) for path in entry.notice_paths)
    license_text = contents[license_file].decode("utf-8", errors="replace")
    spdx = identify_license(license_text)
    if spdx != entry.spdx:
        raise StageError(f"{entry.license_path} at {tree.commit} reads as {spdx or 'an unrecognized license'}, "
                         f"not {entry.spdx}; the item is not bundled until its license is checked again")
    dest.mkdir(parents=True, exist_ok=True)
    for local, data in contents.items():
        _write(dest.joinpath(*local.split("/")), data, local in executables)
    record = Upstream(name=entry.name, repository=entry.repository, subpath=entry.subpath, ref=entry.ref,
                      commit=tree.commit, spdx=spdx, license_path=entry.license_path, license_file=license_file,
                      license_sha256=text_digest(contents[license_file]), notice_files=notice_files,
                      executables=tuple(sorted(set(executables))), files=files, fetched=fetched.isoformat(),
                      include=entry.include)
    write_json_atomic(dest / UPSTREAM_FILE, record.to_json())
    return record


def copy_tree(source: pathlib.Path, dest: pathlib.Path) -> None:
    """Copy a folder, keeping executable bits, refusing symbolic links."""
    for path in sorted(pathlib.Path(source).rglob("*")):
        if path.is_symlink():
            raise StageError(f"{path} is a symbolic link; oah does not copy links")
    shutil.copytree(source, dest, symlinks=False)


def write_marker(folder: pathlib.Path, data: dict) -> None:
    write_json_atomic(pathlib.Path(folder) / MARKER_FILE, data)


def read_json_file(path: pathlib.Path) -> dict | None:
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
