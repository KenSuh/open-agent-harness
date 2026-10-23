# 기본 브랜치 지도 — 원격의 기본 브랜치를 읽기 전용으로 가져와 두 번째 지도를 갱신하고, 가져오지 못하면 마지막 가져온 ref 를 낡음(stale)으로 표기한다.
"""The remote's default branch, fetched before a query (A23).

`refresh` confirms the remote's symbolic HEAD on every online query, then fetches that one branch into
a private bare repository in oah's state folder. The user's refs, objects and configuration are never
fetch destinations. Git uses normal system/global configuration and credentials, without loading the
user repository's local configuration. No remote URL, FETCH_HEAD, reflog or Git error text is stored.

Only a confirmed default branch fetched successfully in this run is fresh. Every other case uses the
last recorded map's branch and SHA, labelled stale; with no recorded snapshot it is unavailable.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
from dataclasses import dataclass
from typing import Mapping

from oah.atomic import write_json_atomic
from oah.engines.proc import run_command
from oah.impact import mapstore
from oah.impact.errors import ImpactError
from oah.lockfile import StateLock
from oah.paths import git_argv, repo_info, state_dir

FALLBACK_BRANCH = "main"
# No prompt may wait for an answer that cannot come: stdin is closed and the refresh runs unattended.
QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
             "SSH_ASKPASS_REQUIRE": "never", "GIT_SSH_VARIANT": "ssh",
             "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oNumberOfPasswordPrompts=0 -oStrictHostKeyChecking=yes"}
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
               "no remote": "add one with git remote add origin <url> if this project has a shared repository",
               "default branch lookup failed": f"check git ls-remote --symref {self.remote or 'origin'} HEAD, "
                                               "restore the remote's symbolic HEAD, then retry"}
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


def _network_git(root: pathlib.Path, *args: str, timeout: float):
    return _git(root, "--git-dir", str(root), "-c", "core.askPass=", *args, timeout=timeout,
                env={**mapstore._isolated_git_env(), **QUIET_ENV})


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


def _saved_branch(root: pathlib.Path, remote: str, env: Mapping[str, str] | None = None) -> str:
    ref = mapstore._last_default_ref(root, env)
    prefix = f"refs/remotes/{remote}/"
    if ref and ref.startswith(prefix) and _valid(ref[len(prefix):]):
        return ref[len(prefix):]
    return FALLBACK_BRANCH


def _mirror_path(root: pathlib.Path, env: Mapping[str, str] | None = None) -> pathlib.Path:
    info = repo_info(root)
    state = state_dir(info, os.environ if env is None else env)
    key = mapstore.map_path(state, info.root).stem
    # Resolve the parent only: a disposable cache must never resolve to and delete a linked repository.
    return (state / "impact" / "mirrors").resolve() / (key + ".git")


def _mirror_lock(root: pathlib.Path, timeout: float, env: Mapping[str, str] | None = None) -> StateLock:
    # Outside the disposable directory: deleting a cache must not replace its coordination lock.
    return StateLock(_mirror_path(root, env).with_suffix(".lock"), timeout=timeout)


def _source(mirror: pathlib.Path) -> dict:
    try:
        source = json.loads((mirror / "oah-source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return source if isinstance(source, dict) else {}


def _linked(mirror: pathlib.Path) -> bool:
    try:
        entry = mirror.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(entry.st_mode) or bool(getattr(entry, "st_file_attributes", 0) &
                                             getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _usable(mirror: pathlib.Path, timeout: float) -> bool:
    if _linked(mirror):
        return False
    source = _source(mirror)
    if not mapstore._bare(mirror) or not isinstance(source.get("url"), str) \
            or not re.fullmatch(r"[0-9a-f]{64}", source["url"]) or any(mirror.rglob("*.lock")):
        return False
    result = _network_git(mirror, "rev-parse", "--is-bare-repository", timeout=timeout)
    if result.returncode != 0 or result.stdout.strip() != "true":
        return False
    result = _network_git(mirror, "for-each-ref", "--format=%(refname)", timeout=timeout)
    ref = source.get("ref")
    return result.returncode == 0 and result.stdout.splitlines() == ([ref] if ref else [])


def _reset(root: pathlib.Path, mirror: pathlib.Path, timeout: float) -> str:
    """Discard a cache under its mirror lock; never repair Git state left by an interrupted process."""
    object_format = _text(root, "rev-parse", "--show-object-format", timeout=timeout)
    if object_format not in ("sha1", "sha256"):
        return "fetch failed"

    def writable(operation, path, _error):
        # Git object files are read-only; Windows requires this before they can be removed.
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        operation(path)

    if os.name == "nt" and _linked(mirror) and not mirror.is_symlink():
        mirror.rmdir()  # Remove a directory junction itself, never its target.
    elif mirror.is_file() or mirror.is_symlink():
        mirror.unlink()
    elif mirror.exists():
        shutil.rmtree(mirror, onerror=writable if os.name == "nt" else None)
    mirror.mkdir(parents=True, mode=0o700)
    result = _network_git(mirror, "init", "--bare", "--quiet", "--template=",
                          f"--object-format={object_format}", str(mirror), timeout=timeout)
    return "fetch timed out" if result.timed_out else ("" if result.returncode == 0 else "fetch failed")


def _prepare(root: pathlib.Path, remote: str, timeout: float,
             env: Mapping[str, str] | None = None) -> tuple[pathlib.Path, str | None, str]:
    """Read the stored URL and prepare a disposable cache. The caller holds the mirror lock."""
    mirror = _mirror_path(root, env)
    result = _git(root, "config", "--get", f"remote.{remote}.url", timeout=timeout,
                  env=mapstore._isolated_git_env())
    stored_url = result.stdout.strip() if result.returncode == 0 else ""
    if not stored_url:
        return mirror, None, "fetch timed out" if result.timed_out else "fetch failed"
    source = _source(mirror) if _usable(mirror, timeout) else {}
    if not source:
        reason = _reset(root, mirror, timeout)
        if reason:
            return mirror, None, reason
    # --get-url resolves aliases without contacting the remote or saving a mirror remote.
    result = _network_git(mirror, "ls-remote", "--get-url", "--end-of-options", stored_url, timeout=timeout)
    url = result.stdout.strip() if result.returncode == 0 else ""
    if not url:
        return mirror, None, "fetch timed out" if result.timed_out else "fetch failed"
    # Local relative URLs are relative to the user's checkout, not the mirror. Leave Git URL schemes,
    # scp syntax and remote-helper syntax intact so system/global URL rewrites still work.
    if not pathlib.Path(url).is_absolute() and not re.match(r"[^/\\]+:", url):
        url = str((root / pathlib.Path(url).expanduser()).resolve())
    identity = mapstore.digest([stored_url, url])  # A URL may contain credentials; persist only its digest.
    if source.get("url") != identity:
        if source:
            reason = _reset(root, mirror, timeout)
            if reason:
                return mirror, None, reason
        write_json_atomic(mirror / "oah-source.json", {"url": identity, "ref": None})
    return mirror, url, ""


def _select_branch(root: pathlib.Path, mirror: pathlib.Path, remote: str, branch: str, timeout: float) -> str:
    source = _source(mirror)
    ref = f"refs/remotes/{remote}/{branch}"
    if source.get("ref") not in (None, ref):
        reason = _reset(root, mirror, timeout)
        if reason:
            return reason
    source["ref"] = ref
    write_json_atomic(mirror / "oah-source.json", source)
    return ""


def default_branch_name(root: pathlib.Path, remote: str, *, network: bool, timeout: float) -> tuple[str, str]:
    """(branch, reason): confirm the remote's symbolic HEAD, or return the saved name with a reason."""
    return _branch_name(root, remote, network=network, timeout=timeout, fallback=_saved_branch(root, remote))


def _branch_name(root: pathlib.Path, remote: str, *, network: bool, timeout: float,
                fallback: str) -> tuple[str, str]:
    if not network:
        return fallback, "default branch lookup failed"
    with _mirror_lock(root, timeout):
        mirror, url, reason = _prepare(root, remote, timeout)
        return (fallback, reason) if reason else _discover(mirror, url, fallback, timeout)


def _discover(mirror: pathlib.Path, url: str, fallback: str, timeout: float) -> tuple[str, str]:
    result = _network_git(mirror, "ls-remote", "--symref", "--end-of-options", url, "HEAD", timeout=timeout)
    if result.timed_out:
        return fallback, "fetch timed out"
    if result.returncode != 0:
        return fallback, "fetch failed"
    for line in result.stdout.splitlines():
        match = re.match(r"ref: refs/heads/(\S+)\tHEAD\Z", line.rstrip("\r"))
        if match and _valid(match.group(1)):
            return match.group(1), ""
    return fallback, "default branch lookup failed"


def fetch(root: pathlib.Path, remote: str, branch: str, timeout: float) -> str:
    """Fetch one branch into oah's private bare mirror; "" on success, else the reason."""
    with _mirror_lock(root, timeout):
        mirror, url, reason = _prepare(root, remote, timeout)
        if not reason:
            reason = _select_branch(root, mirror, remote, branch, timeout)
        return reason or _fetch(mirror, url, remote, branch, timeout)


def _fetch(mirror: pathlib.Path, url: str, remote: str, branch: str, timeout: float) -> str:
    # FETCH_HEAD and reflogs can contain the URL, so suppress both even inside the private mirror.
    argv = ("-c", "core.logAllRefUpdates=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
            "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--no-prune",
            "--end-of-options", url, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    result = _network_git(mirror, *argv, timeout=timeout)
    if result.timed_out:
        return "fetch timed out"
    return "" if result.returncode == 0 else "fetch failed"


def refresh(root: pathlib.Path, settings: Mapping[str, object], *, offline: bool = False, no_refresh: bool = False,
            env: Mapping[str, str] | None = None) -> DefaultBranch:
    """Fetch the default branch (unless told not to) and return its map, labelled; never raises for the network."""
    with _mirror_lock(root, float(settings["lock_timeout"]), env):
        return _refresh(root, settings, offline=offline, no_refresh=no_refresh, env=env)


def _refresh(root: pathlib.Path, settings: Mapping[str, object], *, offline: bool = False, no_refresh: bool = False,
             env: Mapping[str, str] | None = None) -> DefaultBranch:
    """The locked refresh; CLI queries keep this lock until their last mirror read finishes."""
    timeout = float(settings["git_timeout"])
    remote = remote_for(root, timeout)
    saved = mapstore._last_default_map(root, env)
    previous = None
    if saved:
        info, stored = saved
        ref = stored["manifest"].get("ref", "")
        name = stored["manifest"].get("remote")
        if not name and isinstance(ref, str) and ref.startswith("refs/remotes/"):
            name = ref.removeprefix("refs/remotes/").partition("/")[0]
        prefix = f"refs/remotes/{name}/"
        if isinstance(ref, str) and name and _valid(name) and ref.startswith(prefix) and _valid(ref[len(prefix):]):
            previous = DefaultBranch("stale", "", name, ref[len(prefix):], info.commit, info, stored["data"])

    def fallback(reason: str, branch: str | None = None) -> DefaultBranch:
        if previous:
            return DefaultBranch("stale", reason, previous.remote, previous.branch, previous.commit,
                                 previous.info, previous.data)
        return DefaultBranch("unavailable", reason, remote, branch, None)

    if remote is None:
        return fallback("no remote")
    if no_refresh:
        skip = "no-refresh"
    elif offline:
        skip = "offline"
    elif not settings["refresh_default_branch"]:
        skip = "refresh_default_branch=false"
    else:
        skip = ""
    if skip:
        return fallback(skip)
    try:
        mirror, url, reason = _prepare(root, remote, timeout, env)
        if reason:
            return fallback(reason)
        branch, reason = _discover(mirror, url, previous.branch if previous else FALLBACK_BRANCH, timeout)
        if not reason:
            reason = _select_branch(root, mirror, remote, branch, timeout)
        if not reason:
            reason = _fetch(mirror, url, remote, branch, timeout)
    except OSError:
        return fallback("fetch failed")
    if reason:
        return fallback(reason, branch)
    ref = f"refs/remotes/{remote}/{branch}"
    try:
        sha = mapstore.resolve_rev(mirror, ref, timeout=timeout)
        info, data = mapstore._ref_map(root, ref, source=mirror, env=env, settings=settings, remote=remote, commit=sha)
    except ImpactError as exc:
        return fallback(f"map failed: {exc.code}", branch)
    return DefaultBranch("fresh", "", remote, branch, info.commit, info, data)
