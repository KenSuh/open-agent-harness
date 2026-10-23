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
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Mapping

from oah.atomic import write_json_atomic
from oah.engines.proc import REAP_SECONDS, RunResult, _WindowsJob, decode_output, kill_tree, run_command
from oah.impact import mapstore
from oah.impact.errors import ImpactError
from oah.lockfile import LockTimeout, StateLock
from oah.paths import git_argv, repo_info, state_dir

FALLBACK_BRANCH = "main"
# No prompt may wait for an answer that cannot come: stdin is closed and the refresh runs unattended.
QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
             "SSH_ASKPASS_REQUIRE": "never", "GIT_SSH_VARIANT": "ssh",
             "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oNumberOfPasswordPrompts=0 -oStrictHostKeyChecking=yes"}
_BAD_NAME = re.compile(r"[\s:~^?*\[\\]|\.\.|@\{|\A[-.]|[./]\Z|\.lock\Z|//")
_GENERATION = re.compile(r"mirror-[0-9a-f]{32}\Z")


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
               "cache cleanup pending": "wait for the earlier query to stop, check impact cache permissions, then retry",
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
    argv = (*git_argv(), "-C", str(root), "--git-dir", str(root), "-c", "core.askPass=", *args)
    return _generation_run(root, argv, timeout=timeout, env={**mapstore._isolated_git_env(), **QUIET_ENV})


def _generation_run(mirror: pathlib.Path, argv, *, timeout: float, env=None, encoding="utf-8", stdin_text=None):
    """A separate owner keeps the generation leased until Git and its descendants have stopped."""
    code = (f"import sys; sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[2])!r}); "
            "from oah.impact.defaultbranch import _generation_worker; _generation_worker()")
    command = (sys.executable, "-c", code, str(mirror), str(os.getpid()), str(timeout), encoding, *argv)
    try:
        result = run_command(command, timeout=timeout + 2 * REAP_SECONDS + 1, env=env, stdin_text=stdin_text,
                             encoding=encoding)
    except BaseException:
        # run_command's POSIX session survives KeyboardInterrupt. Ask its owner to stop and reap Git
        # before releasing our mirror lock; a hard parent exit is detected by the owner itself.
        try:
            (mirror / "oah-cancel").touch()
        except OSError:
            pass
        try:
            with StateLock(mirror.with_suffix(".lock"), timeout=REAP_SECONDS + 1):
                pass
        except (OSError, LockTimeout):
            pass  # A still-leased generation is retained and blocks allocation on the next query.
        raise
    if result.timed_out or result.returncode != 0:
        return RunResult(None, "", "", result.timed_out)
    try:
        return RunResult(**json.loads(result.stdout))
    except (TypeError, ValueError):
        return RunResult(None, "", "", False)


def _generation_worker() -> None:
    """Private subprocess entry point. Never print exceptions or a command containing a remote URL."""
    mirror, parent, timeout, encoding = pathlib.Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    result = RunResult(None, "", "", False)
    try:
        with StateLock(mirror.with_suffix(".lock"), timeout=REAP_SECONDS) as lease:
            if mirror.is_dir() and not _linked(mirror) and not (mirror / "oah-retired").exists():
                result = _owned_command(mirror, parent, sys.argv[5:], timeout, encoding, lease._fd)
    except BaseException:
        pass
    print(json.dumps(result.__dict__))


def _owned_command(mirror: pathlib.Path, parent: int, argv: list[str], timeout: float, encoding: str,
                   lease_fd: int) -> RunResult:
    data = sys.stdin.buffer.read()
    job = _WindowsJob() if os.name == "nt" else None
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x4} if job else {"start_new_session": True}
    if not job:
        options["pass_fds"] = (lease_fd,)  # Keep the lease even if the owner itself is killed.
    proc = None
    try:
        if os.getppid() != parent or (mirror / "oah-cancel").exists():
            return RunResult(None, "", "", False)
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if data else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        if job:
            job.start(proc)
        deadline = time.monotonic() + timeout
        first = True
        while True:
            expired = time.monotonic() >= deadline
            if expired or os.getppid() != parent or (mirror / "oah-cancel").exists():
                kill_tree(proc)
                proc.communicate(timeout=REAP_SECONDS)
                return RunResult(None, "", "", expired)
            try:
                out, err = proc.communicate(input=data if first and data else None,
                                            timeout=min(0.05, max(0.001, deadline - time.monotonic())))
                return RunResult(proc.returncode, decode_output(out, encoding), decode_output(err, encoding), False)
            except subprocess.TimeoutExpired:
                first = False
    finally:
        if proc is not None:
            kill_tree(proc)
            proc.communicate(timeout=REAP_SECONDS)
        if job:
            job.close()


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


def _legacy_mirror(root: pathlib.Path, env: Mapping[str, str] | None = None) -> pathlib.Path:
    info = repo_info(root)
    state = state_dir(info, os.environ if env is None else env)
    key = mapstore.map_path(state, info.root).stem
    # Resolve the parent only: never follow a cache alias into the user's repository.
    return (state / "impact" / "mirrors").resolve() / (key + ".git")


def _generation_home(root: pathlib.Path, env: Mapping[str, str] | None = None) -> pathlib.Path:
    return _legacy_mirror(root, env).with_suffix(".generations")


def _mirror_path(root: pathlib.Path, env: Mapping[str, str] | None = None) -> pathlib.Path:
    home = _generation_home(root, env)
    try:
        pointer = json.loads((home / "current.json").read_text(encoding="utf-8"))
        name = pointer.get("generation")
        if isinstance(name, str) and _GENERATION.fullmatch(name) and not _linked(home):
            return home / name
    except (OSError, ValueError, AttributeError):
        pass
    return _legacy_mirror(root, env)  # Read-only compatibility with maps recorded before generations.


def _mirror_lock(root: pathlib.Path, timeout: float, env: Mapping[str, str] | None = None) -> StateLock:
    return StateLock(_legacy_mirror(root, env).with_suffix(".lock"), timeout=timeout)


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


def _remove(mirror: pathlib.Path) -> None:
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


def _snapshot_source(root: pathlib.Path, commit: str, env: Mapping[str, str] | None = None) -> pathlib.Path:
    current = _mirror_path(root, env)
    if _source(current).get("commit") == commit:
        return current
    home = _generation_home(root, env)
    try:
        if home.is_dir() and not _linked(home):
            for candidate in sorted(home.iterdir()):
                if _GENERATION.fullmatch(candidate.name) and not _linked(candidate) \
                        and _source(candidate).get("commit") == commit:
                    return candidate
    except OSError:
        pass
    return current


def _cleanup(root: pathlib.Path, env: Mapping[str, str] | None = None, *, timeout: float = REAP_SECONDS + 1) -> bool:
    """Retire unused generations under their own leases; a live writer is never a disposal signal."""
    try:
        return _collect(root, env, timeout)
    except OSError:
        return False


def _collect(root: pathlib.Path, env: Mapping[str, str] | None, timeout: float) -> bool:
    home = _generation_home(root, env)
    if _linked(home):
        return False
    if not home.exists():
        return True
    current = _mirror_path(root, env)
    try:
        (home / "current.json").lstat()
    except FileNotFoundError:
        pass
    else:
        if current.parent != home:
            return False  # An unreadable pointer cannot prove that any generation is non-current.
    keep = {current}
    saved = mapstore._last_default_map(root, env)
    if saved:
        keep.add(_snapshot_source(root, saved[0].commit, env))
    complete = True
    names = {p.name.removesuffix(".lock") for p in home.iterdir()
             if _GENERATION.fullmatch(p.name.removesuffix(".lock"))}
    for name in sorted(names):
        mirror = home / name
        if mirror in keep:
            continue
        lease = mirror.with_suffix(".lock")
        try:
            with StateLock(lease, timeout=timeout):
                if mirror.is_dir() and not _linked(mirror):
                    (mirror / "oah-retired").touch()
                _remove(mirror)
            lease.unlink(missing_ok=True)
        except (OSError, LockTimeout):
            complete = False
    return complete


def _prepare(root: pathlib.Path, remote: str, timeout: float,
             env: Mapping[str, str] | None = None) -> tuple[pathlib.Path, str | None, str]:
    """Read the first stored fetch URL and initialize a generation that will never be reused."""
    home = _generation_home(root, env)
    mirror = home / ("mirror-" + uuid.uuid4().hex)
    result = _git(root, "config", "--null", "--get-all", f"remote.{remote}.url", timeout=timeout,
                  env=mapstore._isolated_git_env())
    stored_url = result.stdout.split("\0", 1)[0] if result.returncode == 0 else ""
    if not stored_url:
        return mirror, None, "fetch timed out" if result.timed_out else "fetch failed"
    # Recover the map/pointer publication gap, or a standalone fetch newer than the saved map.
    # Do this only before allocating: the last recorded snapshot becomes the cleanup anchor again.
    saved = mapstore._last_default_map(root, env)
    if saved:
        snapshot = _snapshot_source(root, saved[0].commit, env)
        if snapshot != _mirror_path(root, env) and _source(snapshot).get("commit") == saved[0].commit:
            _publish(snapshot, saved[0].commit)
    if not _cleanup(root, env):
        return mirror, None, "cache cleanup pending"
    object_format = _text(root, "rev-parse", "--show-object-format", timeout=timeout)
    if object_format not in ("sha1", "sha256"):
        return mirror, None, "fetch failed"
    mirror.mkdir(parents=True, mode=0o700)
    result = _network_git(mirror, "init", "--bare", "--quiet", "--template=",
                          f"--object-format={object_format}", str(mirror), timeout=timeout)
    if result.returncode != 0:
        return mirror, None, "fetch timed out" if result.timed_out else "fetch failed"
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
    write_json_atomic(mirror / "oah-source.json", {"url": identity, "ref": None})
    return mirror, url, ""


def _select_branch(root: pathlib.Path, mirror: pathlib.Path, remote: str, branch: str, timeout: float) -> str:
    source = _source(mirror)
    ref = f"refs/remotes/{remote}/{branch}"
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
        try:
            mirror, url, reason = _prepare(root, remote, timeout)
            return (fallback, reason) if reason else _discover(mirror, url, fallback, timeout)
        finally:
            _cleanup(root)


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
        try:
            mirror, url, reason = _prepare(root, remote, timeout)
            if not reason:
                reason = _select_branch(root, mirror, remote, branch, timeout)
            reason = reason or _fetch(mirror, url, remote, branch, timeout)
            if not reason:
                sha = _capture(mirror, remote, branch, timeout)
                _publish(mirror, sha)
            return reason
        finally:
            _cleanup(root)


def _capture(mirror: pathlib.Path, remote: str, branch: str, timeout: float) -> str:
    sha = mapstore.resolve_rev(mirror, f"refs/remotes/{remote}/{branch}", timeout=timeout)
    write_json_atomic(mirror / "oah-source.json", {**_source(mirror), "commit": sha})
    return sha


def _publish(mirror: pathlib.Path, sha: str) -> None:
    write_json_atomic(mirror.parent / "current.json", {"generation": mirror.name, "commit": sha})


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
        if reason:
            return fallback(reason, branch)
        ref = f"refs/remotes/{remote}/{branch}"
        sha = _capture(mirror, remote, branch, timeout)
        info, data = mapstore._ref_map(root, ref, source=mirror, env=env, settings=settings, remote=remote, commit=sha)
        _publish(mirror, sha)
        return DefaultBranch("fresh", "", remote, branch, info.commit, info, data)
    except OSError:
        return fallback("fetch failed")
    except ImpactError as exc:
        return fallback(f"map failed: {exc.code}", branch)
    finally:
        _cleanup(root, env)
