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
from oah import unlazy
from oah.engines.proc import UnsupportedBatchShim, resolve_command

GENERATED = (".oah/tasks/*/GATES.md", ".oah/tasks/*/GATES.summary.md", ".unlazy/*", ".unlazy-hook-state.json")
CHUNK = 1024 * 1024
EXECUTION_ENV = ("PATH", "UNLAZY_SHELL", "OAH_NODE", "ComSpec", "PATHEXT", "NODE_OPTIONS", "NODE_PATH")


class InputsUncovered(Exception):
    code = "OAH_GATES_INPUTS_UNCOVERED"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(f"{self.code}: {message}")


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
        raise InputsUncovered(f"submodule {name} has live inputs that oah cannot verify; move the task to a "
                              "worktree without submodules before checking or finishing its gates")
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


def _command_identity(argv: Sequence[str], env: Mapping[str, str]) -> str:
    """Hash the resolved executable and file arguments, without running unverified tools."""
    try:
        resolved = resolve_command(argv[0], search_path=env.get("PATH", ""))
    except UnsupportedBatchShim as exc:
        raise InputsUncovered(f"cannot identify runtime {argv[0]}: {exc}; install a native executable "
                              "and run oah gates check again") from exc
    parts = [*(resolved.argv if resolved else (argv[0],)), *argv[1:]]
    identity = []
    for part in parts:
        path = pathlib.Path(part)
        if path.is_file():
            identity.append([str(path.resolve()), path.stat().st_mode, _hash_file(path)])
        else:
            identity.append([part, None, None])
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def _shell(env: Mapping[str, str]) -> str:
    """Mirror unlazy's shell selection, including its PATH and Windows extension search."""
    default = (env.get("ComSpec") or "cmd.exe") if os.name == "nt" else "/bin/sh"
    requested = env.get("UNLAZY_SHELL") or default
    if "/" in requested or "\\" in requested:
        return str(pathlib.Path(requested).resolve())
    names = [requested]
    if os.name == "nt" and not pathlib.Path(requested).suffix:
        extensions = (env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";")
        names += [requested + ext.lower() for ext in extensions if ext]
        names += [requested + ext.upper() for ext in extensions if ext]
    for directory in env.get("PATH", "").split(os.pathsep):
        if directory:
            for name in names:
                path = pathlib.Path(directory) / name
                if path.is_file():
                    return str(path.resolve())
    return requested


def collect(root: pathlib.Path, *, env_names: Sequence[str] = (), env: Mapping[str, str] | None = None,
            tools: Mapping[str, str | None] | None = None, extra_excludes: Sequence[str] = (),
            node: Sequence[str] | None = None) -> Manifest:
    """Hash every tracked and non-ignored untracked file under `root` (a worktree root)."""
    root = pathlib.Path(root)
    env = os.environ if env is None else env
    if os.name == "nt":
        # Windows environment names are case-insensitive, including for a plain caller-supplied dict.
        folded = {key.upper(): value for key, value in env.items()}
        env = {**{key: value for key, value in env.items() if not key.upper().startswith("NODE_")},
               **{key: value for key, value in folded.items() if key.startswith("NODE_")},
               **{key: folded[key.upper()] for key in (*EXECUTION_ENV, *env_names)
                  if key.upper() in folded}}
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
    environment["runtime:python"] = _command_identity((sys.executable,), env)
    environment["runtime:shell"] = _command_identity((_shell(env),), env)
    node = node if node is not None else unlazy.resolve_node(env)
    environment["runtime:node"] = _command_identity(node, env) if node else None
    # Include new Node runtime switches without needing an oah release for each new variable.
    node_env = [name for name in env if name.startswith("NODE_")]
    for name in set((*EXECUTION_ENV, *env_names, *node_env)):
        value = env.get(name)
        environment[f"env:{name}"] = None if value is None else hashlib.sha256(value.encode("utf-8")).hexdigest()
    return Manifest(files=tuple(files), environment=tuple(sorted(environment.items())))
