# 테스트 공용 도우미 — 임시 폴더, POSIX 가짜 도구, npm 설치 배치(layout), 점수 받기용 가짜 연결과 네트워크 차단, 작업 폴더(scratch) 격리를 만든다.
from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import pathlib
import shutil
import socket
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping
from unittest import mock


def make_tmp_dir(test: unittest.TestCase) -> pathlib.Path:
    """A fresh temporary folder that is removed when the test ends."""
    path = pathlib.Path(tempfile.mkdtemp(prefix="oah-test-")).resolve()
    test.addCleanup(shutil.rmtree, path, True)
    return path


def private_folder(path: pathlib.Path) -> pathlib.Path:
    """Create `path` owner-only (mode 0700, or a protected owner-only Windows DACL) and return it."""
    from oah import unlazy

    unlazy._private_directory(path)
    return path


def make_private_dir(test: unittest.TestCase) -> pathlib.Path:
    """A fresh owner-only folder, such as a `temp_root=` that must pass the scratch proof, removed when the test ends."""
    return private_folder(make_tmp_dir(test) / "private")


# The per-user scratch base (oah.scratch) is computed from one variable per OS. The test package points it at a
# private folder of its own (isolate_scratch), so no test writes into the developer's real cache.
SCRATCH_RUN: pathlib.Path | None = None


def scratch_env(root: pathlib.Path, platform: str | None = None) -> dict[str, str]:
    """The one variable that moves the scratch base under `root` on this platform."""
    platform = sys.platform if platform is None else platform
    name = "LOCALAPPDATA" if platform == "win32" else "HOME" if platform == "darwin" else "XDG_CACHE_HOME"
    return {name: str(root)}


def scratch_base(env: Mapping[str, str] | None = None, platform: str | None = None) -> pathlib.Path:
    """The documented scratch base for `env`, worked out here independently of oah.scratch and resolved."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        root = pathlib.Path(env["LOCALAPPDATA"])
    elif platform == "darwin":
        root = pathlib.Path(env["HOME"]) / "Library" / "Caches"
    else:
        cache = env.get("XDG_CACHE_HOME", "")
        root = pathlib.Path(cache) if cache and os.path.isabs(cache) else pathlib.Path(env["HOME"]) / ".cache"
    return pathlib.Path(os.path.realpath(root / "oah" / "scratch"))


def isolate_scratch() -> pathlib.Path:
    """Point this test process's scratch base at a private folder that is removed when the process exits.

    Child processes inherit the variable, so their scratch lands there too."""
    global SCRATCH_RUN
    if SCRATCH_RUN is None:
        SCRATCH_RUN = pathlib.Path(tempfile.mkdtemp(prefix="oah-scratch-run-")).resolve()
        atexit.register(shutil.rmtree, SCRATCH_RUN, True)
        os.environ.update(scratch_env(SCRATCH_RUN))
    return SCRATCH_RUN


@contextlib.contextmanager
def hostile_temp(test: unittest.TestCase):
    """Point every system-temp lookup (TMPDIR, TEMP, TMP and tempfile.tempdir) at a shared folder any account may
    write, and record every folder tempfile creates meanwhile. Yields (shared folder, resolved created folders)."""
    shared = make_tmp_dir(test) / "shared-temp"
    shared.mkdir()
    if os.name != "nt":
        shared.chmod(0o777)  # world-writable and not sticky: another account could swap any child
    created: list[pathlib.Path] = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        name = real(*args, **kwargs)
        created.append(pathlib.Path(os.path.realpath(name)))
        return name

    with mock.patch.dict(os.environ, {"TMPDIR": str(shared), "TEMP": str(shared), "TMP": str(shared)}), \
            mock.patch.object(tempfile, "tempdir", str(shared)), mock.patch.object(tempfile, "mkdtemp", recording):
        yield shared, created


def unprovable_folder(test: unittest.TestCase) -> pathlib.Path:
    """A folder the scratch proof must refuse before creating anything. On POSIX it would sit in a folder any account
    may write that is not sticky; on Windows it is relative, as a LOCALAPPDATA of "relative-folder" would be."""
    if os.name == "nt":
        return pathlib.Path("relative-folder")
    shared = make_tmp_dir(test) / "open"
    shared.mkdir()
    shared.chmod(0o777)
    return shared / "folder"


@contextlib.contextmanager
def unsafe_scratch(test: unittest.TestCase):
    """The scratch base moves into an unprovable_folder while every system-temp lookup names a shared folder.

    Afterwards nothing may exist in either place: no folder was created at all, so nothing fell back."""
    folder = unprovable_folder(test)
    with hostile_temp(test) as (shared, created), mock.patch.dict(os.environ, scratch_env(folder)):
        yield
    test.assertEqual(created, [], "no folder was created anywhere")
    test.assertEqual(sorted(shared.iterdir()), [], "the shared system temp stays unused")
    test.assertFalse(os.path.lexists(folder), "nothing was created where the unsafe base would be")


def directory_alias(target: pathlib.Path, link: pathlib.Path) -> None:
    """A real directory symlink, or a Windows junction (which needs no symlink privilege)."""
    if os.name == "nt":
        import subprocess

        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def write_posix_tool(folder: pathlib.Path, name: str, body: str) -> pathlib.Path:
    """An executable /bin/sh script named `name` (POSIX only)."""
    folder.mkdir(parents=True, exist_ok=True)
    tool = folder / name
    tool.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tool


def npm_layout(prefix: pathlib.Path, package: str, bin_field, *, command: str,
               script_rel: str, script_body: str = "", local_node: bool = False,
               shim_body: str = "@echo off\r\n") -> pathlib.Path:
    """Lay out a Windows npm install: <prefix>/<command>.cmd plus node_modules/<package>/.

    Returns the shim path. `bin_field` is written to package.json as given.
    """
    package_dir = prefix / "node_modules" / pathlib.PurePosixPath(package)
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "package.json").write_text(
        json.dumps({"name": package, "version": "1.0.0", "bin": bin_field}), encoding="utf-8")
    script = package_dir / pathlib.PurePosixPath(script_rel)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(script_body, encoding="utf-8")
    shim = prefix / f"{command}.cmd"
    shim.write_text(shim_body, encoding="utf-8", newline="")
    if local_node:
        (prefix / "node.exe").write_bytes(b"")
    return shim


def require_node(test: unittest.TestCase) -> str:
    """Return the real node executable, or skip — or fail when OAH_REQUIRE_NODE_TESTS=1."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("OAH_REQUIRE_NODE_TESTS") == "1":
            test.fail("node is required for this test (OAH_REQUIRE_NODE_TESTS=1)")
        test.skipTest("node is not installed")
    return node


def make_unlazy(folder: pathlib.Path, version: str | None = "2.1.0") -> pathlib.Path:
    """A minimal unlazy install: scripts/gate-check.mjs plus an optional package.json version."""
    (folder / "scripts").mkdir(parents=True)
    (folder / "scripts" / "gate-check.mjs").write_text("", encoding="utf-8")
    if version is not None:
        (folder / "package.json").write_text(json.dumps({"name": "unlazy-skill", "version": version}),
                                             encoding="utf-8")
    return folder


NOREPLY_EMAIL = "1234+someone" + "@" + "users.noreply.github.com"


def git_env() -> dict[str, str]:
    """An environment that gives commits a fixed, allowlisted identity."""
    return dict(os.environ, GIT_AUTHOR_NAME="Someone", GIT_AUTHOR_EMAIL=NOREPLY_EMAIL,
                GIT_COMMITTER_NAME="Someone", GIT_COMMITTER_EMAIL=NOREPLY_EMAIL)


def isolate_git_home(test: unittest.TestCase, home: pathlib.Path) -> dict[str, str]:
    """Opt in to a temporary home and Git configuration, including empty templates and disabled hooks."""
    home.mkdir(parents=True, exist_ok=True)
    folders = {name: home / name for name in ("xdg", "git-templates", "git-hooks")}
    for folder in folders.values():
        folder.mkdir(exist_ok=True)
    config = home / "gitconfig-empty"
    config.write_text("", encoding="utf-8")
    isolated = {"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(folders["xdg"]),
                "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TEMPLATE_DIR": str(folders["git-templates"]), "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(folders["git-hooks"])}
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(isolated)
    test.enterContext(mock.patch.dict(os.environ, env, clear=True))
    return isolated


_TOOLUPDATE_ALLOWLISTS = []


def guard_subprocesses(test: unittest.TestCase, allowed) -> list[list[str]]:
    """Guard setup as well as the operation; a swallowed refusal still fails at cleanup."""
    import subprocess

    launched, violations = [], []
    real_popen = subprocess.Popen

    def guarded(args, *rest, **options):
        argv = [str(part) for part in args] if isinstance(args, (list, tuple)) else [str(args)]
        launched.append(argv)
        if not allowed(argv):
            violations.append(argv)
            raise AssertionError(f"a test tried to start a program outside its allowlist: {argv[:1]}")
        return real_popen(args, *rest, **options)

    test.enterContext(mock.patch("subprocess.Popen", guarded))
    _TOOLUPDATE_ALLOWLISTS.append(allowed)
    test.addCleanup(_TOOLUPDATE_ALLOWLISTS.remove, allowed)
    test.addCleanup(lambda: test.assertEqual(violations, []))
    return launched


@contextlib.contextmanager
def toolupdate_module_isolation():
    """A baseline guard before any class setup, including a setup that forgets its base class."""
    import subprocess

    from oah import paths
    from oah.toolupdate import records
    from oah.toolupdate import run as passes

    baseline = unittest.TestCase()
    try:
        baseline.home = make_tmp_dir(baseline) / "home"
        isolate_git_home(baseline, baseline.home)
        baseline.enterContext(mock.patch.object(records, "home", return_value=baseline.home))
        baseline.enterContext(mock.patch.object(pathlib.Path, "home", return_value=baseline.home))
        baseline.enterContext(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(baseline.home / ".codex"), "CLAUDE_CONFIG_DIR": str(baseline.home / ".claude")}))
        for name in ("OAH_WORKER", "OAH_STATE_DIR", "OAH_KIT_DIR"):
            os.environ.pop(name, None)
        git = paths.git_argv()[0]
        real_popen = subprocess.Popen

        def guarded(args, *rest, **options):
            argv = [str(part) for part in args] if isinstance(args, (list, tuple)) else [str(args)]
            allowed = bool(argv) and (argv[0] in ("git", git) or
                                      any(check(argv) for check in _TOOLUPDATE_ALLOWLISTS))
            if not allowed or options.get("shell"):
                raise AssertionError(f"a test tried to start a program outside its allowlist: {argv[:1]}")
            return real_popen(args, *rest, **options)

        baseline.enterContext(mock.patch("subprocess.Popen", guarded))
        baseline.enterContext(mock.patch.object(passes, "_popen", guarded))
        yield baseline
    finally:
        baseline.doCleanups()


class ToolUpdateTestCase(unittest.TestCase):
    """Install update-test isolation before a subclass creates any repository or other fixture."""

    toolupdate_isolation = True

    def setUp(self):
        import subprocess

        from oah import paths
        from oah.toolupdate import records
        from oah.toolupdate import run as passes

        self.home = make_tmp_dir(self) / "home"
        self.isolated_env = isolate_git_home(self, self.home)
        self.git = paths.git_argv()[0]
        self.launched = guard_subprocesses(self, self.allowed)
        self._toolupdate_guard = subprocess.Popen
        self.enterContext(mock.patch.object(passes, "_popen", subprocess.Popen))
        self.enterContext(mock.patch.object(records, "home", return_value=self.home))
        self.enterContext(mock.patch.object(pathlib.Path, "home", return_value=self.home))
        self.enterContext(mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex"),
                                                      "CLAUDE_CONFIG_DIR": str(self.home / ".claude")}))
        for name in ("OAH_WORKER", "OAH_STATE_DIR", "OAH_KIT_DIR"):
            os.environ.pop(name, None)
        self._toolupdate_isolated_home = self.home
        self.addCleanup(self.assert_update_isolation)

    def allowed(self, argv: list[str]) -> bool:
        return bool(argv) and argv[0] in ("git", self.git)

    def assert_update_isolation(self):
        import subprocess

        self.assertTrue(hasattr(self, "_toolupdate_isolated_home"), "setUp did not install tool-update isolation")
        self.assertEqual(pathlib.Path(os.environ["HOME"]), self._toolupdate_isolated_home)
        self.assertTrue(self._toolupdate_isolated_home.is_relative_to(pathlib.Path(tempfile.gettempdir()).resolve()))
        self.assertEqual(os.environ["USERPROFILE"], os.environ["HOME"])
        self.assertEqual(os.environ["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertTrue(hasattr(self, "launched"), "subprocess guard was not installed")
        self.assertIs(subprocess.Popen, self._toolupdate_guard, "subprocess guard was replaced")


def make_git_repo(test: unittest.TestCase) -> pathlib.Path:
    """A fresh repository on branch main, with no commits."""
    import subprocess

    repo = make_tmp_dir(test) / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    return repo


def git_commit(repo: pathlib.Path, files: dict, message: str = "change") -> str:
    """Write (str/bytes) or delete (None) files, commit everything, and return the new HEAD."""
    import subprocess

    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=git_env())
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", message],
                   check=True, env=git_env())
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


class FakeResponse(io.BytesIO):
    """What a fake opener answers: a body, an HTTP status and headers."""

    def __init__(self, body: bytes = b"", status: int = 200, headers: dict | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = dict(headers or {})


class FakeOpener:
    """Stands in for the opener of `oah.benchmarks`: it records each request and answers from `answers`, which maps a
    URL to a response, an exception to raise, or a function of the request. Anything else is refused as offline."""

    def __init__(self, answers: dict | None = None) -> None:
        self.answers = dict(answers or {})
        self.requests: list[dict] = []

    def open(self, request, timeout=None):
        self.requests.append({"url": request.full_url, "method": request.get_method(),
                              "headers": dict(request.header_items()), "data": request.data, "timeout": timeout})
        answer = self.answers.get(request.full_url, urllib.error.URLError("tests never reach the network"))
        if callable(answer) and not isinstance(answer, BaseException):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def urls(self) -> list[str]:
        return [request["url"] for request in self.requests]


def use_opener(test: unittest.TestCase, opener: FakeOpener | None = None) -> FakeOpener:
    """Give every score fetch of this test `opener` (a refusing one by default) and return it as a spy."""
    from oah import benchmarks

    opener = opener if opener is not None else FakeOpener()
    patcher = mock.patch.object(benchmarks, "_opener", lambda: opener)
    patcher.start()
    test.addCleanup(patcher.stop)
    return opener


class RealNetworkRequest(BaseException):
    """A test reached for the real network. A BaseException, so that no `except Exception` in oah swallows it."""


REAL_OPENER = None  # the production opener factory of `oah.benchmarks`, kept for the guard test
SHIPPED_DEFAULTS: dict = {}  # `oah.models.DEFAULTS` as oah ships them, before `block_network`
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def block_network() -> None:
    """Keep every test offline (AC6).

    Tests that do not test fetching run with `[models] fetch_scores` off by default, so a first catalog build in a test
    neither asks for data nor warns that it could not; a test of fetching calls `shipped_defaults`. A fetch gets a
    refusing fake opener unless a test installs its own, and a request to any host but this machine raises
    RealNetworkRequest instead of leaving it."""
    global REAL_OPENER
    from oah import benchmarks, models

    if REAL_OPENER is not None:
        return
    SHIPPED_DEFAULTS.update(models.DEFAULTS)
    models.DEFAULTS["fetch_scores"] = False
    REAL_OPENER = benchmarks._opener
    benchmarks._opener = FakeOpener
    real_open = urllib.request.OpenerDirector.open

    def open_loopback_only(self, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else fullurl
        host = urllib.parse.urlsplit(url).hostname
        if host not in LOOPBACK:
            raise RealNetworkRequest(f"a test tried to reach {host}; install a fake opener (tests.helpers.use_opener)")
        return real_open(self, fullurl, data, timeout)

    urllib.request.OpenerDirector.open = open_loopback_only


def shipped_defaults(test: unittest.TestCase) -> None:
    """Run this test with the model defaults oah ships, so a catalog rebuild fetches scores (through a fake opener)."""
    from oah import models

    patcher = mock.patch.dict(models.DEFAULTS, SHIPPED_DEFAULTS)
    patcher.start()
    test.addCleanup(patcher.stop)
