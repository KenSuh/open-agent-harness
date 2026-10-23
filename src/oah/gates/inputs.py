# 게이트 입력 목록 — 추적·무시되지 않은 미추적 파일의 경로·실행 모드·바이트를 해시해 게이트마다 입력 다이제스트를 만든다.
"""Per-gate input manifests.

A manifest lists every tracked and non-ignored untracked file of a worktree as (path, mode, sha256).
Deleted tracked files stay listed with mode `deleted`, so additions and deletions both change the
digest. Generated gate outputs are never inputs. The environment record (Python version, platform,
tool versions and the values of configured environment names) is part of every digest, but only its
hash is ever stored.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import os
import pathlib
import platform
import stat
import sys
from typing import Mapping, Sequence

from oah.paths import GitMissing, run_git

GENERATED = (".oah/tasks/*/GATES.md", ".oah/tasks/*/GATES.summary.md", ".unlazy/*", ".unlazy-hook-state.json")
CHUNK = 1024 * 1024


@dataclasses.dataclass(frozen=True)
class Manifest:
    files: tuple[tuple[str, str, str], ...]  # (path, mode, sha256) sorted by path
    environment: tuple[tuple[str, str | None], ...]

    def digest_for(self, prefixes: Sequence[str] | None) -> str:
        """The digest of the files under `prefixes` (all files when None) plus the environment."""
        selected = [entry for entry in self.files if prefixes is None or _under(entry[0], prefixes)]
        payload = {"v": 1, "prefixes": sorted(prefixes) if prefixes is not None else "*",
                   "environment": [list(item) for item in self.environment], "files": [list(e) for e in selected]}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _under(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def _generated(path: str, extra: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in (*GENERATED, *extra))


def _git_list(root: pathlib.Path, *args: str) -> str:
    result = run_git(root, *args)
    if result.returncode != 0:
        raise GitMissing(f"{GitMissing.code}: git failed while listing gate inputs in {root}: "
                         f"{result.stderr.strip()}")
    return result.stdout


def _hash_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry(root: pathlib.Path, name: str, index_mode: str | None, index_sha: str | None) -> tuple[str, str]:
    path = root / pathlib.PurePosixPath(name)
    if index_mode == "160000":
        return "160000", index_sha or ""  # a submodule: its recorded commit
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "deleted", ""
    except NotADirectoryError:
        return "deleted", ""
    if stat.S_ISLNK(info.st_mode):
        return "120000", hashlib.sha256(os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()
    if stat.S_ISDIR(info.st_mode):
        return "040000", ""
    if os.name == "nt":
        mode = index_mode if index_mode in ("100644", "100755") else "100644"
    else:
        mode = "100755" if info.st_mode & 0o111 else "100644"
    return mode, _hash_file(path)


def collect(root: pathlib.Path, *, env_names: Sequence[str] = (), env: Mapping[str, str] | None = None,
            tools: Mapping[str, str | None] | None = None, extra_excludes: Sequence[str] = ()) -> Manifest:
    """Hash every tracked and non-ignored untracked file under `root` (a worktree root)."""
    root = pathlib.Path(root)
    env = os.environ if env is None else env
    index: dict[str, tuple[str, str]] = {}
    for record in _git_list(root, "ls-files", "-z", "--stage").split("\0"):
        if record:
            meta, _, name = record.partition("\t")
            mode, sha, _stage = meta.split(" ")
            index.setdefault(name, (mode, sha))
    names = set(index)
    names.update(name for name in _git_list(root, "ls-files", "-z", "--others", "--exclude-standard").split("\0")
                 if name)
    files = []
    for name in sorted(names):
        if _generated(name, extra_excludes):
            continue
        mode, digest = _entry(root, name, *index.get(name, (None, None)))
        files.append((name, mode, digest))
    environment: dict[str, str | None] = {"python": platform.python_version(), "platform": sys.platform}
    for key, value in (tools or {}).items():
        environment[f"tool:{key}"] = value
    for name in env_names:
        value = env.get(name)
        environment[f"env:{name}"] = None if value is None else hashlib.sha256(value.encode("utf-8")).hexdigest()
    return Manifest(files=tuple(files), environment=tuple(sorted(environment.items())))
