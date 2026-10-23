# 기본 브랜치 지도 — 원격의 기본 브랜치를 읽기 전용으로 가져와 두 번째 지도를 갱신하고, 가져오지 못하면 마지막 가져온 ref 를 낡음(stale)으로 표기한다.
"""The remote's default branch, fetched before a query (A23).

`refresh` finds the remote (the current branch's upstream remote, else `origin`) and its default branch
(`refs/remotes/<remote>/HEAD`, else `git ls-remote --symref`, else `main`), fetches that one branch into
its remote-tracking ref and refreshes the default-branch map for the fetched commit.

The fetch is read-only towards the remote and writes only `refs/remotes/<remote>/<branch>` (and objects):
no tags, no `FETCH_HEAD`, no remote HEAD, no configuration, no maintenance. git uses the user's own setup;
oah reads, passes and stores no credential, turns interactive prompts off and never echoes git's error
text, which can contain a URL. Offline, disabled or failed, the last fetched ref is used and labelled
`stale(<reason>)`; with no fetched ref the result is `unavailable(<reason>)`. The network never fails a
query.
"""
from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import Mapping

from oah.engines.proc import run_command
from oah.impact import mapstore
from oah.impact.errors import ImpactError
from oah.paths import git_argv

FALLBACK_BRANCH = "main"
# No prompt may wait for an answer that cannot come: stdin is closed and the refresh runs unattended.
QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
_BAD_NAME = re.compile(r"[\s:~^?*\[\\]|\.\.|@\{|\A[-.]|[./]\Z|\.lock\Z|//")


@dataclass(frozen=True)
class DefaultBranch:
    state: str  # fresh, stale or unavailable
    reason: str  # why it is stale or unavailable; "" when fresh
    remote: str | None
    branch: str | None
    commit: str | None
    info: mapstore.MapInfo | None = None
    data: dict | None = None

    @property
    def ref(self) -> str | None:
        return f"refs/remotes/{self.remote}/{self.branch}" if self.remote and self.branch else None

    def label(self) -> str:
        if self.state == "fresh":
            return f"default-branch={self.commit}"
        return f"default-branch={self.state}({self.reason})"

    def details(self) -> str:
        """The label, plus the commit used when stale and the ref it came from."""
        parts = [self.label()]
        if self.state == "stale":
            parts.append(f"last-fetched={self.commit}")
        if self.commit and self.ref:
            parts.append(f"ref={self.remote}/{self.branch}")
        return " ".join(parts)

    def warning(self) -> str | None:
        if self.state == "fresh":
            return None
        fix = {"offline": "drop --offline when the network is back",
               "no-refresh": "drop --no-refresh to fetch it",
               "refresh_default_branch=false": "set [impact] refresh_default_branch = true in .oah/config.toml",
               "no remote": "add one with git remote add origin <url> if this project has a shared repository"}
        hint = fix.get(self.reason, f"run git fetch {self.remote or 'origin'} yourself to see why, then retry")
        if self.reason.startswith("map failed"):
            hint = "run oah impact refresh, then retry the query"
        if self.state == "stale":
            return (f"OAH_IMPACT_DEFAULT_BRANCH_STALE: the default branch {self.remote}/{self.branch} was not fetched "
                    f"({self.reason}), so impact used the last fetched commit {str(self.commit)[:12]}; checks added "
                    f"to the default branch since then are missing. To refresh it, {hint}.")
        return (f"OAH_IMPACT_DEFAULT_BRANCH_UNAVAILABLE: no fetched default branch to compare with "
                f"({self.reason}), so impact answered from the worktree map only. To add it, {hint}.")

    def to_json(self) -> dict:
        return {"state": self.state, "reason": self.reason, "remote": self.remote, "branch": self.branch,
                "ref": self.ref if self.commit else None, "commit": self.commit, "label": self.label(),
                "map": self.info.to_json() if self.info else None}


def _git(root: pathlib.Path, *args: str, timeout: float, env: Mapping[str, str] | None = None):
    return run_command((*git_argv(), "-C", str(root), *args), timeout=timeout, env=env)


def _text(root: pathlib.Path, *args: str, timeout: float) -> str | None:
    result = _git(root, *args, timeout=timeout)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _remotes(root: pathlib.Path, timeout: float) -> list[str]:
    return (_text(root, "remote", timeout=timeout) or "").splitlines()


def remote_for(root: pathlib.Path, timeout: float) -> str | None:
    """The current branch's upstream remote when it is a configured remote, else origin, else None."""
    remotes = _remotes(root, timeout)
    branch = _text(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=timeout)
    upstream = _text(root, "config", "--get", f"branch.{branch}.remote", timeout=timeout) if branch else None
    if upstream in remotes:
        return upstream
    return "origin" if "origin" in remotes else None


def _valid(branch: str) -> bool:
    return bool(branch) and not _BAD_NAME.search(branch)


def default_branch_name(root: pathlib.Path, remote: str, *, network: bool, timeout: float) -> tuple[str, str]:
    """(branch, reason): the remote's HEAD branch, from the local remote HEAD ref, else (network allowed) from
    `git ls-remote --symref`, else main. reason is non-empty when the remote could not be asked."""
    prefix = f"refs/remotes/{remote}/"
    local = _text(root, "symbolic-ref", "--quiet", prefix + "HEAD", timeout=timeout)
    if local and local.startswith(prefix) and _valid(local[len(prefix):]):
        return local[len(prefix):], ""
    if not network:
        return FALLBACK_BRANCH, ""
    result = _git(root, "ls-remote", "--symref", "--end-of-options", remote, "HEAD", timeout=timeout,
                  env={**os.environ, **QUIET_ENV})
    if result.timed_out:
        return FALLBACK_BRANCH, "fetch timed out"
    if result.returncode != 0:
        return FALLBACK_BRANCH, "fetch failed"
    for line in result.stdout.splitlines():
        match = re.match(r"ref: refs/heads/(\S+)\tHEAD\Z", line.rstrip("\r"))
        if match and _valid(match.group(1)):
            return match.group(1), ""
    return FALLBACK_BRANCH, ""


def fetch(root: pathlib.Path, remote: str, branch: str, timeout: float) -> str:
    """Fetch one branch into its remote-tracking ref; "" on success, else the reason."""
    argv = ("-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", f"remote.{remote}.followRemoteHEAD=never",
            "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--no-prune",
            "--end-of-options", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    result = _git(root, *argv, timeout=timeout, env={**os.environ, **QUIET_ENV})
    if result.timed_out:
        return "fetch timed out"
    return "" if result.returncode == 0 else "fetch failed"


def refresh(root: pathlib.Path, settings: Mapping[str, object], *, offline: bool = False, no_refresh: bool = False,
            env: Mapping[str, str] | None = None) -> DefaultBranch:
    """Fetch the default branch (unless told not to) and return its map, labelled; never raises for the network."""
    timeout = float(settings["git_timeout"])
    remote = remote_for(root, timeout)
    if remote is None:
        return DefaultBranch("unavailable", "no remote", None, None, None)
    if no_refresh:
        skip = "no-refresh"
    elif offline:
        skip = "offline"
    elif not settings["refresh_default_branch"]:
        skip = "refresh_default_branch=false"
    else:
        skip = ""
    branch, reason = default_branch_name(root, remote, network=not skip, timeout=timeout)
    if not skip and not reason:
        reason = fetch(root, remote, branch, timeout)
    reason = skip or reason
    ref = f"refs/remotes/{remote}/{branch}"
    commit = _text(root, "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}", timeout=timeout)
    if commit is None:
        return DefaultBranch("unavailable", reason or "nothing fetched", remote, branch, None)
    try:
        info, data = mapstore.ref_map(root, ref, refresh=not no_refresh, env=env, settings=settings)
    except ImpactError as exc:
        stale_map = no_refresh and exc.code == "OAH_IMPACT_STALE"
        return DefaultBranch("unavailable", "no-refresh" if stale_map else f"map failed: {exc.code}", remote,
                             branch, None)
    return DefaultBranch("stale" if reason else "fresh", reason, remote, branch, info.commit, info, data)
