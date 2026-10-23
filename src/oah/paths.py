# 저장소·상태 경로 — git 으로 작업 트리 루트, 모든 워크트리가 공유하는 common dir, 상태 폴더를 찾는다.
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Mapping

from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim, resolve_command, run_command

STATE_ENV = "OAH_STATE_DIR"
GIT_TIMEOUT = 30.0


class NotARepo(Exception):
    code = "OAH_NOT_A_REPO"


class GitMissing(Exception):
    code = "OAH_GIT_MISSING"


@dataclass(frozen=True)
class RepoInfo:
    root: pathlib.Path
    common_dir: pathlib.Path
    branch: str | None
    head: str | None


_git: Resolved | None = None


def git_argv() -> tuple[str, ...]:
    """The argv that runs git, resolved once per process."""
    global _git
    if _git is None:
        try:
            found = resolve_command("git")
        except UnsupportedBatchShim as exc:
            raise GitMissing(str(exc)) from exc
        if found is None:
            raise GitMissing(f"{GitMissing.code}: git was not found on PATH")
        _git = found
    return _git.argv


def run_git(cwd: pathlib.Path, *args: str) -> RunResult:
    return run_command(git_argv() + ("-C", str(cwd), *args), timeout=GIT_TIMEOUT)


def _output(result: RunResult) -> str | None:
    text = result.stdout.strip()
    return text if result.returncode == 0 and text else None


def repo_info(cwd: pathlib.Path) -> RepoInfo:
    """Describe the repository that contains cwd; raises NotARepo outside a repository."""
    top = _output(run_git(cwd, "rev-parse", "--show-toplevel"))
    if top is None:
        raise NotARepo(f"{NotARepo.code}: {cwd} is not inside a git repository")
    root = pathlib.Path(top)
    common = _output(run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common is None:
        raise NotARepo(f"{NotARepo.code}: cannot find the git common directory of {root}")
    branch = _output(run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD"))
    head = _output(run_git(root, "rev-parse", "--verify", "--quiet", "HEAD"))
    return RepoInfo(root=root, common_dir=pathlib.Path(common), branch=branch, head=head)


def state_dir(info: RepoInfo, env: Mapping[str, str]) -> pathlib.Path:
    """Where leases and events live: OAH_STATE_DIR, else <git common dir>/oah."""
    override = env.get(STATE_ENV)
    return pathlib.Path(override) if override else info.common_dir / "oah"
