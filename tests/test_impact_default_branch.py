# 기본 브랜치 영향 검사 — 로컬 bare 원격만으로 가져오기·두 번째 지도·두 SHA 표기·낡음/없음 표기·읽기 전용 가져오기를 실제 프로세스로 본다.
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

from oah.engines.proc import run_command
from oah.impact import ImpactError, defaultbranch, ensure_fresh, mapstore, query
from oah.impact.defaults import DEFAULTS, MAP_KEYS, configured, effective
from oah.lockfile import StateLock
from tests.helpers import directory_alias, git_commit, git_env, make_git_repo, make_tmp_dir
from tests.test_impact_map import BASE, head, run_impact

NETWORK_GIT = re.compile(r"built-in: git (fetch|ls-remote)\b|upload-pack")
SHA = r"[0-9a-f]{40}"


def git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", env=git_env()).stdout.strip()


def git_bytes(cwd: pathlib.Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=git_env()).stdout


def refs(repo: pathlib.Path) -> dict[str, str]:
    """{ref: sha}, or {ref: "-> target"} for a symbolic ref, so a symref is not counted as moved with its target."""
    lines = git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(symref)").splitlines()
    found = {}
    for line in lines:
        name, _, rest = line.partition(" ")
        sha, _, target = rest.partition(" ")
        found[name] = f"-> {target.strip()}" if target.strip() else sha
    return found


def trace_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def repository_bytes(repo: pathlib.Path) -> dict[str, bytes]:
    """All user Git files, excluding oah's state folder."""
    common = pathlib.Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))

    def fail(error):
        raise error  # An incomplete snapshot cannot prove the user's repository stayed unchanged.

    files = {}
    for folder, directories, names in os.walk(common, onerror=fail):
        if pathlib.Path(folder) == common:
            directories[:] = [name for name in directories if name != "oah"]
        for name in names:
            path = pathlib.Path(folder) / name
            if path.is_file():
                files[path.relative_to(common).as_posix()] = path.read_bytes()
    return files


def fault_query(fx, fault: str, *args: str) -> subprocess.CompletedProcess:
    """Real CLI and Git processes; remove the advertised branch or time out a real child at fetch."""
    code = '''
import subprocess
import sys
from oah.cli import main
from oah.impact import defaultbranch
original = defaultbranch.run_command
def run(argv, **kwargs):
    if "fetch" in argv and sys.argv[1] == "timeout":
        return original((sys.executable, "-c", "import time; time.sleep(10)"), timeout=0.03)
    result = original(argv, **kwargs)
    if "ls-remote" in argv and "--get-url" not in argv and sys.argv[1] == "failure":
        subprocess.run(["git", "-C", sys.argv[2], "update-ref", "-d", "refs/heads/main"],
                       check=True, capture_output=True)
    return result
defaultbranch.run_command = run
raise SystemExit(main(["impact", *sys.argv[3:]]))
'''
    env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent / "src"))
    env.pop("OAH_STATE_DIR", None)
    return subprocess.run([sys.executable, "-c", code, fault, str(fx.bare), *args], cwd=fx.lane,
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)


class RemoteFixture:
    """A local bare remote seeded with BASE, a lane clone on its own branch, and a second clone that pushes."""

    def __init__(self, test: unittest.TestCase, default: str = "main", *, object_format: str = "sha1") -> None:
        self.tmp = make_tmp_dir(test)
        test.addCleanup(defaultbranch._remove, self.tmp)
        templates = getattr(test, "_remote_templates", None)
        if templates is not None:
            key = default, object_format
            if key not in templates:
                templates[key] = RemoteFixture(test._template_owner, default, object_format=object_format)
            shutil.copytree(templates[key].tmp, self.tmp, dirs_exist_ok=True)
            self.bare, self.lane, self.pusher = (self.tmp / name for name in ("remote.git", "lane", "pusher"))
            self.default = default
            for repo in (self.lane, self.pusher):
                git(repo, "remote", "set-url", "origin", str(self.bare))
            return
        seed = self.tmp / "seed"
        subprocess.run(["git", "init", "-q", "-b", default, f"--object-format={object_format}", str(seed)], check=True)
        git_commit(seed, BASE)
        self.bare = self.tmp / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(self.bare)], check=True, env=git_env())
        self.lane = self.tmp / "lane"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.lane)], check=True, env=git_env())
        git(self.lane, "switch", "-q", "-c", "lane")
        git_commit(self.lane, {"src/feature.py": "import tools.check\n"}, "lane work")
        self.pusher = self.tmp / "pusher"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.pusher)], check=True, env=git_env())
        self.default = default

    def advance(self, files: dict) -> str:
        """Commit on the remote's default branch after the lane branched."""
        sha = git_commit(self.pusher, files, "default branch moves on")
        git(self.pusher, "push", "-q", "origin", f"HEAD:{self.default}")
        return sha

    def map_files(self) -> list[pathlib.Path]:
        common = pathlib.Path(git(self.lane, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        return sorted((common / "oah" / "impact" / "maps").glob("*.default-branch.json"))


class RemoteTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Seed/clone once per class. Each test copies the complete small fixture, including its bare
        # remote, so destructive remote/configuration tests cannot affect another case.
        cls._template_owner = unittest.TestCase()
        cls.addClassCleanup(cls._template_owner.doCleanups)
        cls._remote_templates = {("main", "sha1"): RemoteFixture(cls._template_owner)}


RELEASE = {".github/workflows/release.yml": "steps:\n  - run: python tools/check.py --release\n"}


class SettingTest(unittest.TestCase):
    def test_refresh_setting_default_and_type(self):
        self.assertIs(DEFAULTS["refresh_default_branch"], True)
        self.assertNotIn("refresh_default_branch", MAP_KEYS, "the refresh does not change a map's content")
        with self.assertRaises(ImpactError) as caught:
            effective({"refresh_default_branch": "yes"})
        self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")


class MergeTest(unittest.TestCase):
    def test_merge_maps_dedupes_and_marks(self):
        def row(source, line, binding="exec", detail="path", score=1):
            return query.Row(binding, "lexical", "workflow", source, line, "t.py", detail, score)

        shared, mine = row("ci.yml", 2), row("Makefile", 4, "pin")
        theirs, stronger = row("release.yml", 2), row("docs/a.md", 3, "mention", "path, 2 lines", 2)
        rows, found = query.merge_maps([shared, mine, row("docs/a.md", 3, "mention")],
                                       [row("ci.yml", 2, score=9), theirs, stronger])
        self.assertEqual([(r.source, r.line) for r in rows],
                         [("Makefile", 4), ("ci.yml", 2), ("release.yml", 2), ("docs/a.md", 3)])
        self.assertIs(rows[1], shared, "the worktree row is kept")
        self.assertEqual([found[r] for r in rows], ["worktree", "both", "default-branch", "both"])


class NoRemoteTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {**BASE, ".oah/config.toml": "[impact]\nlock_timeout = 0.1\n"})

    def test_no_remotes_stops_selection_after_the_remote_list(self):
        calls = []
        original = defaultbranch._git

        def probe(root, *args, **kwargs):
            calls.append(args)
            return original(root, *args, **kwargs)

        with patch("oah.impact.defaultbranch._git", side_effect=probe):
            self.assertIsNone(defaultbranch.remote_for(self.repo, 1.0))
        self.assertEqual(calls, [("remote",)])

    def test_no_remote_does_not_wait_for_a_mirror_lock_or_fetch(self):
        trace = self.repo.parent / "trace.log"
        code = '''
import pathlib
from oah.impact import defaultbranch
from oah.impact.defaults import configured
root = pathlib.Path.cwd()
print(defaultbranch.refresh(root, configured(root)).label())
'''
        env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent / "src"),
                   GIT_TRACE=str(trace))
        env.pop("OAH_STATE_DIR", None)
        with defaultbranch._mirror_lock(self.repo, 1.0):
            api = subprocess.run([sys.executable, "-c", code], cwd=self.repo, env=env,
                                 capture_output=True, text=True, timeout=10)
            self.assertEqual(api.returncode, 0, api.stderr)
            self.assertEqual(api.stdout.strip(), "default-branch=unavailable(no remote)")
            cli = run_impact(self.repo, "tools/check.py", env_extra={"GIT_TRACE": str(trace)})
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertIn("default-branch=unavailable(no remote)", cli.stdout)
        self.assertNotRegex(trace_text(trace), NETWORK_GIT)

    def test_no_remote_is_unavailable(self):
        repo = self.repo
        result = run_impact(repo, "tools/check.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"maps: worktree={head(repo)} default-branch=unavailable(no remote)", result.stdout.splitlines())
        self.assertIn("OAH_IMPACT_DEFAULT_BRANCH_UNAVAILABLE", result.stderr)
        self.assertRegex(result.stdout, r"exec\s+lexical\s+workflow\s+\.github/workflows/ci\.yml:2")


class RemoteSelectionTest(RemoteTestCase):
    def setUp(self):
        self.fx = RemoteFixture(self)

    def test_upstream_remote_preferred(self):
        lane = self.fx.lane
        self.assertEqual(defaultbranch.remote_for(lane, 30.0), "origin", "no upstream: origin")
        git(lane, "remote", "add", "upstream", str(self.fx.bare))
        git(lane, "fetch", "-q", "upstream")
        git(lane, "branch", "-q", "--set-upstream-to", "upstream/main")
        self.assertEqual(defaultbranch.remote_for(lane, 30.0), "upstream")
        git(lane, "branch", "-q", "--unset-upstream")
        git(lane, "remote", "remove", "origin")
        self.assertIsNone(defaultbranch.remote_for(lane, 30.0), "no upstream and no origin")

    def test_no_symbolic_head_has_a_lookup_failure_reason(self):
        git(self.fx.lane, "remote", "set-head", "origin", "-d")
        git(self.fx.bare, "update-ref", "--no-deref", "HEAD", head(self.fx.pusher))
        self.assertEqual(defaultbranch.default_branch_name(self.fx.lane, "origin", network=True, timeout=30.0),
                         ("main", "default branch lookup failed"))

    def test_fetch_timeout_is_stale(self):
        fetched = defaultbranch.refresh(self.fx.lane, configured(self.fx.lane), env={}).commit

        def slow_fetch(argv, **kwargs):
            if "fetch" in argv:
                return run_command((sys.executable, "-c", "import time; time.sleep(10)"), timeout=0.03)
            return run_command(argv, **kwargs)

        with patch("oah.impact.defaultbranch.run_command", side_effect=slow_fetch):
            result = defaultbranch.refresh(self.fx.lane, configured(self.fx.lane), env={})
        self.assertEqual((result.state, result.reason, result.commit), ("stale", "fetch timed out", fetched))
        self.assertEqual(result.label(), "default-branch=stale(fetch timed out)")
        self.assertIn("OAH_IMPACT_DEFAULT_BRANCH_STALE", result.warning())

    def test_discovered_trunk_survives_failed_fetch(self):
        fx = RemoteFixture(self, default="trunk")
        git(fx.lane, "remote", "set-head", "origin", "-d")
        new = fx.advance(RELEASE)
        settings = configured(fx.lane)
        saved = defaultbranch.refresh(fx.lane, settings, env={})
        self.assertEqual((saved.state, saved.branch, saved.commit), ("fresh", "trunk", new))
        calls = []
        original = defaultbranch._generation_run

        def remove_after_discovery(mirror, argv, **kwargs):
            result = original(mirror, argv, **kwargs)
            if "ls-remote" in argv and "--get-url" not in argv:
                git(fx.bare, "update-ref", "-d", "refs/heads/trunk")
            if "fetch" in argv:
                calls.append(result.returncode)
            return result

        with patch("oah.impact.defaultbranch._generation_run", side_effect=remove_after_discovery):
            result = defaultbranch.refresh(fx.lane, settings, env={})
        self.assertTrue(calls and calls[0] != 0, "the real fetch failed after discovery succeeded")
        self.assertEqual((result.state, result.reason, result.branch, result.commit),
                         ("stale", "fetch failed", "trunk", new))
        self.assertEqual(result.data, saved.data)

    def test_skipped_refresh_starts_no_network_process(self):
        settings = configured(self.fx.lane)
        defaultbranch.refresh(self.fx.lane, settings, env={})
        for options, overrides in (({"offline": True}, {}), ({"no_refresh": True}, {}),
                                   ({}, {"refresh_default_branch": False})):
            with self.subTest(options=options, overrides=overrides):
                calls = []

                def probe(argv, **kwargs):
                    calls.extend(arg for arg in argv if arg in ("fetch", "ls-remote"))
                    return run_command(argv, **kwargs)

                with patch("oah.impact.defaultbranch.run_command", side_effect=probe):
                    result = defaultbranch.refresh(self.fx.lane, {**settings, **overrides}, env={}, **options)
                self.assertEqual(calls, [])
                self.assertEqual(result.state, "stale")

    def _assert_no_askpass(self, source: str) -> None:
        marker = self.fx.tmp / "askpass-called"
        marker.unlink(missing_ok=True)
        script = self.fx.tmp / "askpass probe.py"
        script.write_text("import pathlib\n" + f"pathlib.Path({str(marker)!r}).touch()\nraise SystemExit(1)\n",
                          encoding="utf-8")
        if os.name == "nt":
            askpass = self.fx.tmp / "askpass probe.cmd"
            askpass.write_text(f'@echo off\n"{sys.executable}" "{script}"\n', encoding="utf-8")
        else:
            askpass = self.fx.tmp / "askpass"
            askpass.write_text(f"#!{sys.executable}\n" + script.read_text(encoding="utf-8"), encoding="utf-8")
            askpass.chmod(0o755)
        command = str(askpass)
        if source == "core.askPass":
            git(self.fx.lane, "config", "core.askPass", command)
        calls = []
        original = defaultbranch._generation_run

        def probe(mirror, argv, **kwargs):
            action = next((a for a in ("ls-remote", "fetch") if a in argv), None)
            if action and "--get-url" not in argv:
                calls.append(action)
                # Exercise Git's real credential path with the network command's config and environment,
                # without contacting any host or returning a credential.
                result = run_command((*argv[:argv.index(action)], "-c", "credential.helper=", "credential", "fill"),
                                     timeout=1.0, env=kwargs["env"],
                                     stdin_text="protocol=https\nhost=credential.invalid\n\n")
                self.assertFalse(result.timed_out)
                self.assertNotEqual(result.returncode, 0)
            return original(mirror, argv, **kwargs)

        home = make_tmp_dir(self)
        config = home / ".gitconfig"
        config.touch()
        with patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
                                     "HOME": str(home)}):
            for name in ("GIT_ASKPASS", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
                os.environ.pop(name, None)
            if source != "core.askPass":
                os.environ[source] = command
            control_env = dict(os.environ)
            control_env.pop("GIT_TERMINAL_PROMPT", None)
            control = run_command(("git", "-C", str(self.fx.lane), "-c", "credential.helper=", "credential", "fill"),
                                  timeout=1.0, env=control_env,
                                  stdin_text="protocol=https\nhost=credential.invalid\n\n")
            control_exercised = not control.timed_out and marker.exists()
            marker.unlink(missing_ok=True)
            with patch("oah.impact.defaultbranch._generation_run", side_effect=probe):
                self.assertEqual(defaultbranch.default_branch_name(self.fx.lane, "origin", network=True,
                                                                  timeout=30.0), ("main", ""))
                self.assertEqual(defaultbranch.fetch(self.fx.lane, "origin", "main", 30.0), "")
        self.assertEqual(calls, ["ls-remote", "fetch"])
        self.assertFalse(marker.exists(), f"{source} must not run during discovery or fetch")
        if not control_exercised:
            self.skipTest(f"{source} control could not exercise askpass on this Git build; retry with another Git build")

    def test_discovery_and_fetch_disable_askpass(self):
        git(self.fx.lane, "remote", "set-head", "origin", "-d")
        for source in ("GIT_ASKPASS", "core.askPass", "SSH_ASKPASS"):
            with self.subTest(source=source):
                git(self.fx.lane, "config", "core.askPass", "")
                git(self.fx.lane, "config", "--unset", "core.askPass")
                self._assert_no_askpass(source)

    def test_askpass_control_ignores_inherited_prompt_and_config(self):
        home = self.fx.tmp / "inherited-home"
        home.mkdir()
        config = home / ".gitconfig"
        config.write_text("[core]\n\taskPass =\n[credential]\n\tinteractive = false\n", encoding="utf-8")
        git(self.fx.lane, "remote", "set-head", "origin", "-d")
        with patch.dict(os.environ, {"HOME": str(home), "GIT_CONFIG_GLOBAL": str(config),
                                     "GIT_TERMINAL_PROMPT": "0"}):
            self._assert_no_askpass("SSH_ASKPASS")

    def test_discovery_and_fetch_force_noninteractive_ssh(self):
        if shutil.which("ssh") is None:
            self.skipTest("OpenSSH is required to inspect its effective configuration")
        git(self.fx.lane, "remote", "set-head", "origin", "-d")
        git(self.fx.lane, "config", "core.sshCommand", "ssh -oBatchMode=no")
        calls = []

        def probe(argv, **kwargs):
            action = next((a for a in ("ls-remote", "fetch") if a in argv), None)
            if action and "--get-url" not in argv:
                env = kwargs["env"]
                config = run_command((*shlex.split(env["GIT_SSH_COMMAND"]), "-F", os.devnull, "-G", "oah.invalid"),
                                     timeout=5.0, env=env)
                self.assertEqual(config.returncode, 0, config.stderr)
                options = dict(line.split(None, 1) for line in config.stdout.splitlines())
                self.assertEqual(options["batchmode"], "yes")
                self.assertIn(options["stricthostkeychecking"], ("yes", "true"))
                self.assertEqual(options["numberofpasswordprompts"], "0")
                self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "never")
                self.assertEqual(env["GIT_SSH_VARIANT"], "ssh")
                calls.append(action)
            return run_command(argv, **kwargs)

        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -oBatchMode=no -oStrictHostKeyChecking=ask",
                                     "GIT_SSH_VARIANT": "plink"}):
            with patch("oah.impact.defaultbranch.run_command", side_effect=probe):
                defaultbranch.default_branch_name(self.fx.lane, "origin", network=True, timeout=30.0)
                self.assertEqual(defaultbranch.fetch(self.fx.lane, "origin", "main", 30.0), "")
        self.assertEqual(calls, ["ls-remote", "fetch"])

    @unittest.skipUnless(os.name == "nt", "Windows batch askpass")
    def test_windows_batch_askpass_is_disabled(self):
        git(self.fx.lane, "remote", "set-head", "origin", "-d")
        self._assert_no_askpass("GIT_ASKPASS")


class RefMapTest(RemoteTestCase):
    def setUp(self):
        self.fx = RemoteFixture(self)
        self.ref = "refs/remotes/origin/main"

    def test_ref_map_persisted_beside_worktree_map(self):
        info, _data = mapstore.ref_map(self.fx.lane, self.ref, env={})
        self.assertEqual((info.action, info.commit), ("built", git(self.fx.lane, "rev-parse", self.ref)))
        worktree = mapstore.ensure_fresh(self.fx.lane, env={})
        self.assertEqual(info.path.parent, worktree.path.parent)
        self.assertNotEqual(info.path, worktree.path)
        self.assertEqual(mapstore.ref_map(self.fx.lane, self.ref, env={})[0].action, "fresh")

    def test_ref_map_incremental_equals_full(self):
        mapstore.ref_map(self.fx.lane, self.ref, env={})
        new = self.fx.advance({**RELEASE, "docs/more.md": "tools/check.py again\n", "src/lib/util.py": None,
                               "src/app.py": "print('app v2')\n"})
        git(self.fx.lane, "fetch", "-q", "origin")
        info, data = mapstore.ref_map(self.fx.lane, self.ref, env={})
        self.assertEqual((info.action, info.commit), ("refreshed", new))
        full = mapstore.build_data(self.fx.lane, new, configured(self.fx.lane))
        self.assertEqual(mapstore.digest(data), mapstore.digest(full))
        self.assertEqual(info.result_digest, mapstore.digest(full))

    def test_ref_map_no_refresh_refuses_stale(self):
        with self.assertRaises(ImpactError) as caught:
            mapstore.ref_map(self.fx.lane, self.ref, refresh=False, env={})
        self.assertEqual(caught.exception.code, "OAH_IMPACT_STALE")

    def test_ensure_fresh_never_fetches(self):
        trace = self.fx.tmp / "trace.log"
        self.fx.advance(RELEASE)
        with patch.dict("os.environ", {"GIT_TRACE": str(trace)}):
            ensure_fresh(self.fx.lane, env={})
        self.assertIn("built-in: git", trace_text(trace), "the trace works")
        self.assertNotRegex(trace_text(trace), NETWORK_GIT)
        for action in ("build", "refresh", "status"):
            result = run_impact(self.fx.lane, action, env_extra={"GIT_TRACE": str(trace)})
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.fx.lane / ".git/oah/impact/mirrors").glob("*.git")), [])
        self.assertNotRegex(trace_text(trace), NETWORK_GIT)


class MirrorLifecycleTest(RemoteTestCase):
    def setUp(self):
        self.fx = RemoteFixture(self)
        self.settings = configured(self.fx.lane)

    def test_location_reuse_preserves_the_no_commit_refusal(self):
        repo = make_git_repo(self)
        git(repo, "remote", "add", "origin", str(self.fx.bare))
        result = defaultbranch.refresh(repo, configured(repo), env={})
        self.assertEqual((result.state, result.reason), ("unavailable", "map failed: OAH_IMPACT_NO_COMMIT"))

    def test_repository_locations_are_reused_only_within_one_refresh(self):
        with patch("oah.impact.mapstore.repo_info", wraps=mapstore.repo_info) as locate:
            first = defaultbranch.refresh(self.fx.lane, self.settings, env={})
            self.assertEqual(locate.call_count, 1)
            other_state = self.fx.tmp / "other state"
            second = defaultbranch.refresh(self.fx.lane, self.settings, env={"OAH_STATE_DIR": str(other_state)})
            self.assertEqual(locate.call_count, 2)
        self.assertEqual((first.state, second.state), ("fresh", "fresh"))
        self.assertEqual(first.commit, second.commit)
        self.assertNotEqual(first.info.path, second.info.path)
        self.assertTrue(second.info.path.is_relative_to(other_state))

    def test_conflicting_local_and_global_url_rewrites(self):
        other = RemoteFixture(self)
        other.advance({"wrong-repository.txt": "not the configured global destination\n"})
        expected = self.fx.advance(RELEASE)
        config = self.fx.tmp / "global-config"
        alias = "oah-test-remote:"
        stored = alias + "remote.git"
        git(self.fx.lane, "config", "--file", str(config),
            f"url.{self.fx.tmp.as_posix()}/.insteadOf", alias)
        # The longer local match wins if URL expansion happens in the user's checkout.
        git(self.fx.lane, "config", f"url.{other.bare.as_posix()}.insteadOf", stored)
        git(self.fx.lane, "remote", "set-url", "origin", stored)
        before = repository_bytes(self.fx.lane)
        result = run_impact(self.fx.lane, "tools/check.py", "--json",
                            env_extra={"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        branch = json.loads(result.stdout)["maps"]["default_branch"]
        self.assertEqual((branch["state"], branch["commit"]), ("fresh", expected))
        self.assertEqual(repository_bytes(self.fx.lane), before)

    def test_multiple_remote_urls_fetch_the_first_repository(self):
        expected = self.fx.advance(RELEASE)
        other = RemoteFixture(self, default="trunk")
        other.advance({"wrong-repository.txt": "the second URL is not a fetch destination\n"})
        git(self.fx.lane, "config", "--add", "remote.origin.url", str(other.bare))
        before = repository_bytes(self.fx.lane)
        result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((result.state, result.branch, result.commit), ("fresh", "main", expected))
        self.assertIn(".github/workflows/release.yml", result.data["files"])
        self.assertNotIn("wrong-repository.txt", result.data["files"])
        self.assertEqual(repository_bytes(self.fx.lane), before)

    def test_successful_fetches_publish_distinct_generations(self):
        saved = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        first = defaultbranch._mirror_path(self.fx.lane, {})
        result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        current = defaultbranch._mirror_path(self.fx.lane, {})
        self.assertEqual((result.state, result.commit, result.data), ("fresh", saved.commit, saved.data))
        self.assertNotEqual(current, first, "even an unchanged SHA must be fetched into a new generation")
        pointer = json.loads((current.parent / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer, {"generation": current.name, "commit": result.commit})
        self.assertFalse(first.exists(), "a completed, unused generation can be removed")

    def test_cleanup_refuses_an_unreadable_current_pointer(self):
        self.assertEqual(defaultbranch.fetch(self.fx.lane, "origin", "main", 30.0), "")
        current = defaultbranch._mirror_path(self.fx.lane)
        (current.parent / "current.json").write_text("invalid pointer\n", encoding="utf-8")
        self.assertFalse(defaultbranch._cleanup(self.fx.lane), "unknown current generations must never be deleted")
        self.assertTrue(current.exists())
        self.assertEqual(refs(current), {"refs/remotes/origin/main": head(self.fx.pusher)})

    def test_failed_fetch_generations_are_not_reused_or_published(self):
        saved = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        current = defaultbranch._mirror_path(self.fx.lane, {})
        recorded = self.fx.map_files()[0].read_bytes()
        attempts = []
        original = defaultbranch._fetch

        def fail(mirror, url, remote, branch, timeout):
            attempts.append(mirror)
            git(self.fx.bare, "update-ref", "-d", "refs/heads/main")
            return original(mirror, url, remote, branch, timeout)

        for _ in range(4):
            git(self.fx.bare, "update-ref", "refs/heads/main", saved.commit)
            with patch("oah.impact.defaultbranch._fetch", side_effect=fail):
                result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
            self.assertEqual((result.state, result.reason, result.commit), ("stale", "fetch failed", saved.commit))
            self.assertEqual(defaultbranch._mirror_path(self.fx.lane, {}), current)
            self.assertEqual(self.fx.map_files()[0].read_bytes(), recorded)
        self.assertEqual(len(set(attempts)), 4, "failed generations must never be retried")
        self.assertNotIn(current, attempts)
        self.assertLessEqual(len([p for p in current.parent.glob("mirror-*") if p.is_dir()]), 2)
        self.assertTrue(all(not p.exists() for p in attempts), "finished failures must not accumulate on disk")

    def test_map_failure_does_not_publish_the_fetched_generation(self):
        saved = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        current = defaultbranch._mirror_path(self.fx.lane, {})
        pointer = (current.parent / "current.json").read_bytes()
        recorded = self.fx.map_files()[0].read_bytes()
        self.fx.advance(RELEASE)
        error = ImpactError("OAH_IMPACT_STALE", "map inputs changed; retry the query")
        with patch("oah.impact.mapstore._ref_map", side_effect=error):
            result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((result.state, result.commit), ("stale", saved.commit))
        self.assertEqual((current.parent / "current.json").read_bytes(), pointer)
        self.assertEqual(self.fx.map_files()[0].read_bytes(), recorded)
        self.assertEqual(refs(current), {"refs/remotes/origin/main": saved.commit})
        self.assertEqual([p for p in current.parent.glob("mirror-*") if p.is_dir()], [current])

    def test_pointer_publication_failure_retains_and_recovers_the_recorded_generation(self):
        defaultbranch.refresh(self.fx.lane, self.settings, env={})
        old = defaultbranch._mirror_path(self.fx.lane, {})
        expected = self.fx.advance(RELEASE)
        with patch("oah.impact.defaultbranch._publish", side_effect=OSError("check cache permissions and retry")):
            result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((result.state, result.reason), ("stale", "fetch failed"))
        saved = defaultbranch.refresh(self.fx.lane, self.settings, offline=True, env={})
        self.assertEqual(saved.commit, expected)
        source = defaultbranch._snapshot_source(self.fx.lane, expected, {})
        self.assertNotEqual(source, old)
        self.assertEqual(refs(source), {"refs/remotes/origin/main": expected})
        self.assertEqual(len([p for p in old.parent.glob("mirror-*") if p.is_dir()]), 2)
        retried = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((retried.state, retried.commit), ("fresh", expected))
        self.assertEqual(len([p for p in old.parent.glob("mirror-*") if p.is_dir()]), 1)

    def test_a_live_generation_lease_blocks_cleanup_and_further_allocation(self):
        saved = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        current = defaultbranch._mirror_path(self.fx.lane, {})
        pending = current.parent / ("mirror-" + uuid.uuid4().hex)
        pending.mkdir()
        marker = pending / "in-use"
        marker.write_bytes(b"an active generation must not be touched")
        ready, release = self.fx.tmp / "lease-ready", self.fx.tmp / "release-lease"
        code = '''
import pathlib, sys, time
from oah.lockfile import StateLock
with StateLock(pathlib.Path(sys.argv[1])):
    pathlib.Path(sys.argv[2]).touch()
    while not pathlib.Path(sys.argv[3]).exists():
        time.sleep(0.01)
'''
        env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent / "src"))
        process = subprocess.Popen([sys.executable, "-c", code, str(pending.with_suffix(".lock")),
                                    str(ready), str(release)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cleanup = defaultbranch._cleanup
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "the other process must own the generation lease")
            with patch("oah.impact.defaultbranch._cleanup", side_effect=lambda root, env: cleanup(root, env, timeout=0)):
                for _ in range(3):
                    result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
                    self.assertEqual((result.state, result.reason, result.commit),
                                     ("stale", "cache cleanup pending", saved.commit))
                    self.assertEqual({p for p in current.parent.glob("mirror-*") if p.is_dir()}, {current, pending})
                    self.assertEqual(marker.read_bytes(), b"an active generation must not be touched")
        finally:
            release.touch()
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)
        self.assertTrue(cleanup(self.fx.lane, {}, timeout=0))
        self.assertFalse(pending.exists())
        self.assertTrue(current.exists())

    def test_interrupted_published_generation_read_keeps_offline_searches(self):
        git_commit(self.fx.lane, {"tools/future.py": "print('new in the lane')\n"})
        expected = self.fx.advance({**RELEASE,
            ".github/workflows/lane-check.yml": "steps:\n  - run: python tools/future.py\n"})
        saved = run_impact(self.fx.lane, "tools/check.py", "--json")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        mirror = defaultbranch._mirror_path(self.fx.lane, {})
        pointer = (mirror.parent / "current.json").read_bytes()
        recorded = self.fx.map_files()[0].read_bytes()
        absent = subprocess.run(["git", "-C", str(self.fx.lane), "cat-file", "-e", expected],
                                capture_output=True, env=git_env())
        self.assertNotEqual(absent.returncode, 0, "only the published generation has the fetched commit")
        ready, release = self.fx.tmp / "read-ready", self.fx.tmp / "release-read"
        # Keep the owner delayed beyond the interrupt handler's lease wait. Removing cancellation
        # merely because that wait expired would allow this interrupted command to run later.
        code = f'''
import pathlib, sys, time
sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parent.parent / "src")!r})
from oah.impact import defaultbranch
original = defaultbranch._owned_command
def delayed(*args, **kwargs):
    pathlib.Path({str(ready)!r}).touch()
    deadline = time.monotonic() + 30
    while not pathlib.Path({str(release)!r}).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("release the paused read and retry")
        time.sleep(0.01)
    return original(*args, **kwargs)
defaultbranch._owned_command = delayed
defaultbranch._generation_worker()
'''
        owners = []

        def interrupt(argv, **kwargs):
            command = list(argv)
            command[2] = code
            owner = subprocess.Popen(command, env=kwargs.get("env"), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            owners.append(owner)
            deadline = time.monotonic() + 10
            while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "the read owner must hold the generation lease")
            raise KeyboardInterrupt

        try:
            with patch("oah.impact.defaultbranch.run_command", side_effect=interrupt), \
                    patch("oah.impact.defaultbranch.REAP_SECONDS", 0.1):
                with self.assertRaises(KeyboardInterrupt):
                    mapstore.resolve_rev(mirror, expected)
        finally:
            release.touch()
            for owner in owners:
                out, err = owner.communicate(timeout=15)
                self.assertEqual(owner.returncode, 0, err)
                self.assertIsNone(json.loads(out)["returncode"], "the delayed read must remain cancelled")
        self.assertEqual((mirror.parent / "current.json").read_bytes(), pointer)
        self.assertEqual(self.fx.map_files()[0].read_bytes(), recorded)
        result = run_impact(self.fx.lane, "tools/future.py", "--offline", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        branch = report["maps"]["default_branch"]
        self.assertEqual((branch["state"], branch["reason"], branch["commit"]), ("stale", "offline", expected))
        checks = [r for r in report["rows"] if r["source"] == ".github/workflows/lane-check.yml"]
        self.assertTrue(checks, "the offline retry must search missing paths in the cached objects")
        self.assertTrue(all(r["found_in"] == "default-branch" for r in checks))
        self.assertNotIn("OAH_IMPACT_INCOMPLETE", result.stderr)
        self.assertFalse(any("OAH_IMPACT_INCOMPLETE" in w for w in report["warnings"]))

    @unittest.skipIf(os.name == "nt", "POSIX query interruption")
    def test_interrupted_fetch_cannot_affect_the_retry_generation(self):
        self._cancelled_fetch(signal.SIGINT)

    @unittest.skipIf(os.name == "nt", "POSIX inherited signal disposition")
    def test_interrupted_fetch_with_inherited_sigint_ignore(self):
        # POSIX background jobs can pass SIG_IGN through exec into both Python processes.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self._cancelled_fetch(signal.SIGINT)
        finally:
            signal.signal(signal.SIGINT, previous)

    @unittest.skipIf(os.name == "nt", "POSIX query termination")
    def test_terminated_query_cannot_leave_a_writer_in_the_retry_generation(self):
        self._cancelled_fetch(signal.SIGKILL)

    def test_fetch_interruption_without_console_cannot_affect_the_retry_generation(self):
        self._cancelled_fetch(None)

    @unittest.skipUnless(os.name == "nt", "Windows fetch cancellation and Job cleanup")
    def test_windows_interrupted_fetch_cannot_affect_the_retry_generation(self):
        self._cancelled_fetch(None)

    def _cancelled_fetch(self, cancellation):
        defaultbranch.refresh(self.fx.lane, self.settings, env={})
        saved = defaultbranch._mirror_path(self.fx.lane, {})
        pointer = (saved.parent / "current.json").read_bytes()
        recorded = self.fx.map_files()[0].read_bytes()
        self.fx.advance(RELEASE)
        ready, release = self.fx.tmp / "fetch-paused", self.fx.tmp / "release-fetch"
        entered = self.fx.tmp / "fetch-generation"
        interrupt = self.fx.tmp / "interrupt-query"
        hooks = self.fx.tmp / "hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        pause = ("import json, os, pathlib, sys, time; "
                 "remote_ref = any(' refs/remotes/' in line for line in sys.stdin); "
                 "sys.exit(0) if not remote_ref else None; "
                 f"ready = pathlib.Path({str(ready)!r}); pending = ready.with_suffix('.pending'); "
                 "pending.write_text(json.dumps([os.getpid(), os.getppid()])); pending.replace(ready); "
                 f"release = pathlib.Path({str(release)!r}); "
                 "exec('while not release.exists():\\n time.sleep(0.01)')")
        hook.write_text('#!/bin/sh\nif [ "$1" = prepared ]; then\nexec ' +
                        shlex.join([pathlib.Path(sys.executable).as_posix(), "-c", pause]) + "\nfi\n",
                        encoding="utf-8")
        hook.chmod(0o755)
        config = self.fx.tmp / "global-config"
        git(self.fx.lane, "config", "--file", str(config), "core.hooksPath", hooks.as_posix())
        code = '''
import json, pathlib, signal, subprocess, sys, time
from oah.impact import defaultbranch
from oah.impact.defaults import configured
if sys.platform != "win32":
    # Python preserves SIG_IGN inherited from a background test runner across exec.
    signal.signal(signal.SIGINT, signal.default_int_handler)
original = defaultbranch._fetch
def fetch(mirror, *args, **kwargs):
    pathlib.Path(sys.argv[1]).write_text(str(mirror), encoding="utf-8")
    return original(mirror, *args, **kwargs)
defaultbranch._fetch = fetch
owners = []
original_popen = subprocess.Popen
def popen(argv, **kwargs):
    owner = original_popen(argv, **kwargs)
    if "fetch" in argv:
        owners.append(owner)
    return owner
subprocess.Popen = popen
if sys.argv[2]:
    original_run = defaultbranch.run_command
    def run(argv, **kwargs):
        if "fetch" not in argv:
            return original_run(argv, **kwargs)
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}
        owner = subprocess.Popen(argv, env=kwargs.get("env"), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        deadline = time.monotonic() + 15
        while not pathlib.Path(sys.argv[2]).exists():
            if owner.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("wait for the prepared fetch before cancelling the query")
            time.sleep(0.01)
        raise KeyboardInterrupt
    defaultbranch.run_command = run
try:
    defaultbranch.refresh(pathlib.Path.cwd(), configured(pathlib.Path.cwd()), env={})
finally:
    for owner in owners:
        out, _ = owner.communicate(timeout=10)
        assert owner.returncode == 0 and json.loads(out)["returncode"] is None
        print("fetch owner stopped and reaped", flush=True)
'''
        env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent / "src"),
                   GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM="1")
        env.pop("OAH_STATE_DIR", None)
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        process = subprocess.Popen([sys.executable, "-c", code, str(entered),
                                    str(interrupt) if cancellation is None else ""], cwd=self.fx.lane, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        pids = []
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "the real fetch must hold a prepared Git ref transaction")
            pids = json.loads(ready.read_text(encoding="utf-8"))
            interrupted = pathlib.Path(entered.read_text(encoding="utf-8"))
            if cancellation is None:
                # Raise on the helper's main thread after Git pauses; console delivery is unreliable
                # on Windows CI while Python is blocked in communicate(). The real owner still has
                # to observe _generation_run's cancellation marker and stop/reap its Git process tree.
                interrupt.touch()
            else:
                process.send_signal(cancellation)
            out, _ = process.communicate(timeout=15)
            self.assertNotEqual(process.returncode, 0)
            if cancellation is None or cancellation == signal.SIGINT:
                self.assertEqual(out.strip(), b"fetch owner stopped and reaped")
            self.assertEqual((saved.parent / "current.json").read_bytes(), pointer)
            self.assertEqual(self.fx.map_files()[0].read_bytes(), recorded)
            expected = self.fx.advance({"after-interruption.txt": "only the retry may fetch this commit\n"})
            result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
            current = defaultbranch._mirror_path(self.fx.lane, {})
            self.assertEqual((result.state, result.commit), ("fresh", expected))
            self.assertNotEqual(current, interrupted, "a surviving fetch must never see a replacement at its path")
            release.touch()
            self.assertEqual(refs(current), {"refs/remotes/origin/main": expected})
            self.assertEqual(defaultbranch.refresh(self.fx.lane, self.settings, offline=True, env={}).commit, expected)
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=15)
            if os.name != "nt":
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_concurrent_queries_keep_their_own_fetched_sha(self):
        self._concurrent_queries("map")

    def test_active_query_keeps_mirror_until_its_last_search(self):
        self._concurrent_queries("search")

    def _concurrent_queries(self, point: str):
        git_commit(self.fx.lane, {"tools/future.py": "print('new in the lane')\n"})
        expected_a = self.fx.advance({**RELEASE,
            ".github/workflows/future.yml": "steps:\n  - run: python tools/future.py\n"})
        other = RemoteFixture(self)
        expected_b = other.advance({"repository-b.txt": "only in B\n"})
        ready, release = self.fx.tmp / "a-ready", self.fx.tmp / "release-a"
        fetched_b = self.fx.tmp / "b-fetched"
        blocked_b = self.fx.tmp / "b-blocked"
        code = '''
import json, pathlib, sys, time
from oah.impact import defaultbranch, mapstore
from oah import impact_cli
from oah import lockfile
from oah.impact.defaults import configured
root = pathlib.Path.cwd()
ready, release, fetched = map(pathlib.Path, sys.argv[2:5])
original_map, original_run = mapstore._ref_map, defaultbranch.run_command
original_hits = impact_cli._with_live_hits
original_lock = lockfile._try_lock
def try_lock(fd):
    acquired = original_lock(fd)
    if sys.argv[1] == "b" and not acquired:
        pathlib.Path(sys.argv[6]).touch()
    return acquired
lockfile._try_lock = try_lock
def pause():
    if sys.argv[1] == "a":
        ready.touch()
        deadline = time.monotonic() + 15
        while not release.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("release the paused query and retry")
            time.sleep(0.01)
def build(*args, **kwargs):
    if sys.argv[5] == "map":
        pause()
    return original_map(*args, **kwargs)
def hits(source, *args, **kwargs):
    if source != root and sys.argv[5] == "search":
        pause()
    return original_hits(source, *args, **kwargs)
def run(argv, **kwargs):
    result = original_run(argv, **kwargs)
    if sys.argv[1] == "b" and "fetch" in argv:
        fetched.touch()
    return result
mapstore._ref_map, defaultbranch.run_command = build, run
impact_cli._with_live_hits = hits
if sys.argv[5] == "search":
    from oah.cli import main
    raise SystemExit(main(["impact", "tools/future.py", "--json"]))
result = defaultbranch.refresh(root, configured(root), env={})
print(json.dumps({"state": result.state, "commit": result.commit, "files": sorted(result.data["files"])}))
'''
        env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent / "src"))
        env.pop("OAH_STATE_DIR", None)
        argv = [sys.executable, "-c", code]
        a = subprocess.Popen([*argv, "a", str(ready), str(release), str(fetched_b), point, str(blocked_b)], cwd=self.fx.lane,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        b = None
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and a.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "query A must finish its real fetch before query B starts")
            git(self.fx.lane, "remote", "set-url", "origin", str(other.bare))
            b = subprocess.Popen([*argv, "b", str(ready), str(release), str(fetched_b), point, str(blocked_b)], cwd=self.fx.lane,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            # Observe actual contention instead of sleeping to guess whether B reached the lock.
            deadline = time.monotonic() + 10
            while not blocked_b.exists() and not fetched_b.exists() and b.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(blocked_b.exists(), "query B must wait for A's mirror lock")
            self.assertFalse(fetched_b.exists(), "B must not fetch while A is still reading")
            release.touch()
            out_a, err_a = a.communicate(timeout=30)
            out_b, err_b = b.communicate(timeout=30)
            self.assertEqual(a.returncode, 0, err_a)
            self.assertEqual(b.returncode, 0, err_b)
        finally:
            release.touch()
            for process in (a, b):
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=30)
        result_a, result_b = json.loads(out_a), json.loads(out_b)
        if point == "search":
            self.assertIn((".github/workflows/future.yml", "exec", "default-branch"),
                          {(r["source"], r["binding"], r["found_in"]) for r in result_a["rows"]})
            for report, expected in ((result_a, expected_a), (result_b, expected_b)):
                branch = report["maps"]["default_branch"]
                self.assertEqual((branch["state"], branch["commit"]), ("fresh", expected))
            return
        self.assertEqual((result_a["state"], result_a["commit"]), ("fresh", expected_a))
        self.assertNotIn("repository-b.txt", result_a["files"])
        self.assertEqual((result_b["state"], result_b["commit"]), ("fresh", expected_b))
        self.assertIn("repository-b.txt", result_b["files"])

    def test_timed_out_fetch_generations_are_discarded_before_retry(self):
        saved = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        mirror = defaultbranch._mirror_path(self.fx.lane, {})
        marker = self.fx.tmp / "fetch-held-ref-lock"
        hooks = self.fx.tmp / "timeout-hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        pause = ("import pathlib, sys, time; " + f"p = pathlib.Path({str(marker)!r}); "
                 "remote_ref = any(' refs/remotes/' in line for line in sys.stdin); "
                 "p.touch() if remote_ref else None; time.sleep(30 if remote_ref else 0)")
        hook.write_text('#!/bin/sh\nif [ "$1" = prepared ]; then\nexec ' +
                        shlex.join([pathlib.Path(sys.executable).as_posix(), "-c", pause]) + "\nfi\n",
                        encoding="utf-8")
        hook.chmod(0o755)
        expected = self.fx.advance(RELEASE)
        config = self.fx.tmp / "timeout-config"
        git(self.fx.lane, "config", "--file", str(config), "core.hooksPath", hooks.as_posix())
        attempts = []
        original = defaultbranch._fetch

        def fetch(generation, *args, **kwargs):
            attempts.append(generation)
            return original(generation, *args[:-1], 0.5, **kwargs)

        for _ in range(3):
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}), \
                    patch("oah.impact.defaultbranch._fetch", side_effect=fetch):
                interrupted = defaultbranch.refresh(self.fx.lane, self.settings, env={})
            self.assertEqual((interrupted.state, interrupted.reason, interrupted.commit),
                             ("stale", "fetch timed out", saved.commit))
            self.assertEqual(defaultbranch._mirror_path(self.fx.lane, {}), mirror)
        self.assertTrue(marker.exists(), "a real Git fetch must reach its prepared ref transaction")
        self.assertEqual(len(set(attempts)), 3)
        self.assertTrue(all(not p.exists() for p in attempts), "completed timeouts must not accumulate on disk")
        retried = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((retried.state, retried.commit), ("fresh", expected))
        current = defaultbranch._mirror_path(self.fx.lane, {})
        self.assertNotIn(current, [mirror, *attempts])
        self.assertFalse(list(current.rglob("*.lock")))
        self.assertEqual(len([p for p in current.parent.glob("mirror-*") if p.is_dir()]), 1)

    def test_default_branch_change_discards_obsolete_refs_and_objects(self):
        old = self.fx.advance(RELEASE)
        defaultbranch.refresh(self.fx.lane, self.settings, env={})
        unrelated = make_git_repo(self)
        expected = git_commit(unrelated, {"trunk.txt": "an unrelated default history\n"})
        git(unrelated, "push", "-q", str(self.fx.bare), "HEAD:trunk")
        git(self.fx.bare, "symbolic-ref", "HEAD", "refs/heads/trunk")
        result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((result.state, result.branch, result.commit), ("fresh", "trunk", expected))
        mirror = defaultbranch._mirror_path(self.fx.lane, {})
        self.assertEqual(refs(mirror), {"refs/remotes/origin/trunk": expected})
        absent = subprocess.run(["git", "-C", str(mirror), "cat-file", "-e", old + "^{commit}"],
                                capture_output=True, env=git_env())
        self.assertNotEqual(absent.returncode, 0, "discard the obsolete history along with its ref")

    def test_remote_name_change_keeps_only_the_current_ref(self):
        expected = self.fx.advance(RELEASE)
        defaultbranch.refresh(self.fx.lane, self.settings, env={})
        git(self.fx.lane, "remote", "rename", "origin", "upstream")
        git(self.fx.lane, "branch", "--set-upstream-to", "upstream/main")
        result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((result.state, result.remote, result.commit), ("fresh", "upstream", expected))
        self.assertEqual(refs(defaultbranch._mirror_path(self.fx.lane, {})),
                         {"refs/remotes/upstream/main": expected})

    def test_invalid_mirror_is_replaced_instead_of_repaired(self):
        saved = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        mirror = defaultbranch._mirror_path(self.fx.lane, {})
        marker = mirror / "old-cache"
        marker.touch()
        (mirror / "HEAD").write_text("invalid HEAD\n", encoding="utf-8")
        result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((result.state, result.commit), ("fresh", saved.commit))
        self.assertFalse(marker.exists())

    def test_repository_snapshot_prunes_state_before_traversal(self):
        mirror = defaultbranch._mirror_path(self.fx.lane, {})
        mirror.parent.mkdir(parents=True)
        before = repository_bytes(self.fx.lane)
        directory_alias(self.fx.lane / ".git", mirror)
        code = '''
import pathlib, sys
from tests.test_impact_default_branch import repository_bytes
repo = pathlib.Path(sys.argv[1])
state = repo / ".git" / "oah"
def guard(event, args):
    if event == "os.scandir" and pathlib.Path(args[0]).is_relative_to(state):
        raise AssertionError("prune oah state before scanning repository files")
sys.addaudithook(guard)
repository_bytes(repo)
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.fx.lane)], capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(repository_bytes(self.fx.lane), before)
        config = self.fx.lane / ".git" / "config"
        config.write_bytes(config.read_bytes() + b"\n# snapshot must detect user config changes\n")
        self.assertNotEqual(repository_bytes(self.fx.lane), before)

    def test_mirror_alias_cannot_delete_or_modify_user_repository(self):
        mirror = defaultbranch._mirror_path(self.fx.lane, {})
        mirror.parent.mkdir(parents=True, exist_ok=True)
        before = repository_bytes(self.fx.lane)
        directory_alias(self.fx.lane / ".git", mirror)
        result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual(result.state, "fresh")
        self.assertEqual(repository_bytes(self.fx.lane), before)
        self.assertNotEqual(defaultbranch._mirror_path(self.fx.lane, {}), mirror)
        current = defaultbranch._mirror_path(self.fx.lane, {})
        self.assertEqual(current.parent, mirror.with_suffix(".generations"))
        self.assertEqual(result.info.path.parent, self.fx.lane / ".git/oah/impact/maps")
        self.assertFalse(defaultbranch._linked(current))
        self.assertFalse((current / "oah").exists(), "mirror Git directories must not contain oah state")
        self.assertFalse(any(defaultbranch._linked(p) for p in current.rglob("*")))
        self.assertTrue(defaultbranch._linked(mirror), "an unleased legacy cache is left untouched")

    def test_retiring_a_generation_alias_cannot_touch_its_target(self):
        defaultbranch.refresh(self.fx.lane, self.settings, env={})
        current = defaultbranch._mirror_path(self.fx.lane, {})
        alias = current.parent / ("mirror-" + uuid.uuid4().hex)
        before = repository_bytes(self.fx.lane)
        directory_alias(self.fx.lane / ".git", alias)
        self.assertTrue(defaultbranch._cleanup(self.fx.lane, {}))
        self.assertFalse(defaultbranch._linked(alias))
        self.assertEqual(repository_bytes(self.fx.lane), before)
        self.assertTrue(current.exists())

    @unittest.skipUnless(os.name == "nt", "Windows dangling directory junction")
    def test_windows_dangling_mirror_junction_is_discarded(self):
        defaultbranch.refresh(self.fx.lane, self.settings, env={})
        current = defaultbranch._mirror_path(self.fx.lane, {})
        mirror = current.parent / ("mirror-" + uuid.uuid4().hex)
        target = self.fx.tmp / "vanished-cache"
        target.mkdir()
        directory_alias(target, mirror)
        target.rmdir()
        self.assertTrue(defaultbranch._cleanup(self.fx.lane, {}))
        self.assertFalse(defaultbranch._linked(mirror))
        self.assertTrue(current.exists())

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions")
    def test_undiscardable_generation_bounds_cleanup_and_preserves_current(self):
        defaultbranch.refresh(self.fx.lane, self.settings, env={})
        mirror = defaultbranch._mirror_path(self.fx.lane, {})
        expected = self.fx.advance(RELEASE)
        mirror.chmod(0o500)
        try:
            if os.access(mirror, os.W_OK):
                self.skipTest("this user can bypass directory write permissions")
            saved = defaultbranch.refresh(self.fx.lane, self.settings, env={})
            self.assertEqual((saved.state, saved.commit), ("fresh", expected))
            current = defaultbranch._mirror_path(self.fx.lane, {})
            for _ in range(2):
                result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
                self.assertEqual((result.state, result.reason, result.commit),
                                 ("stale", "cache cleanup pending", expected))
                self.assertEqual(result.data, saved.data)
                self.assertIn("retry", result.warning())
                self.assertEqual({p for p in current.parent.glob("mirror-*") if p.is_dir()}, {mirror, current})
        finally:
            mirror.chmod(0o700)
        self.assertEqual(defaultbranch.refresh(self.fx.lane, self.settings, env={}).state, "fresh")
        self.assertFalse(mirror.exists())

    def test_url_change_replaces_cache_even_when_the_commit_is_unchanged(self):
        saved = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        replacement = self.fx.tmp / "replacement.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.fx.bare), str(replacement)],
                       check=True, env=git_env())
        mirror = defaultbranch._mirror_path(self.fx.lane, {})
        marker = mirror / "old-cache"
        marker.touch()
        git(self.fx.lane, "remote", "set-url", "origin", str(replacement))
        result = defaultbranch.refresh(self.fx.lane, self.settings, env={})
        self.assertEqual((result.state, result.commit), ("fresh", saved.commit))
        self.assertFalse(marker.exists())

    def test_failed_cache_replacement_keeps_the_recorded_map_usable(self):
        git_commit(self.fx.lane, {"tools/future.py": "print('new in the lane')\n"})
        expected = self.fx.advance({**RELEASE,
            ".github/workflows/future.yml": "steps:\n  - run: python tools/future.py\n"})
        saved = run_impact(self.fx.lane, "tools/check.py", "--json")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        recorded = self.fx.map_files()[0].read_bytes()
        git(self.fx.lane, "remote", "set-url", "origin", str(self.fx.tmp / "missing.git"))
        result = run_impact(self.fx.lane, "tools/check.py", "tools/future.py", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        branch = report["maps"]["default_branch"]
        self.assertEqual((branch["state"], branch["reason"], branch["commit"]), ("stale", "fetch failed", expected))
        self.assertEqual(self.fx.map_files()[0].read_bytes(), recorded)
        self.assertIn(".github/workflows/release.yml", {r["source"] for r in report["rows"]})
        self.assertIn(".github/workflows/future.yml", {r["source"] for r in report["rows"]})
        self.assertNotIn("OAH_IMPACT_INCOMPLETE", result.stderr)
        self.assertIn("retry", result.stderr)


class DefaultBranchCliTest(RemoteTestCase):
    def setUp(self):
        self.fx = RemoteFixture(self)
        self.lane = self.fx.lane
        self.trace = self.fx.tmp / "trace.log"

    def query(self, *args: str, trace: bool = True) -> subprocess.CompletedProcess:
        result = run_impact(self.lane, *args, env_extra={"GIT_TRACE": str(self.trace)} if trace else None)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def maps_line(self, result: subprocess.CompletedProcess) -> str:
        found = [line for line in result.stdout.splitlines() if line.startswith("maps: ")]
        self.assertEqual(len(found), 1, result.stdout)
        return found[0]

    def test_incremental_bundle_list_cannot_change_user_refs_or_config(self):
        self._incremental_bundle_list()

    def test_bundle_control_can_skip_without_skipping_repository_isolation(self):
        result = self._incremental_bundle_list(control_token="2")
        self.assertEqual(json.loads(result.stdout)["maps"]["default_branch"]["state"], "fresh")

    def _incremental_bundle_list(self, *, control_token="1"):
        base = head(self.fx.pusher)
        first, second = self.fx.tmp / "first.bundle", self.fx.tmp / "second.bundle"
        git(self.fx.pusher, "bundle", "create", str(first), "main")
        self.fx.advance(RELEASE)
        git(self.fx.pusher, "bundle", "create", str(second), f"^{base}", "main")
        bundle_list = self.fx.tmp / "bundles.list"
        bundle_list.write_text("[bundle]\nversion = 1\nmode = all\nheuristic = creationToken\n"
                               f'[bundle "first"]\nuri = {first.as_uri()}\ncreationToken = 1\n'
                               f'[bundle "second"]\nuri = {second.as_uri()}\ncreationToken = 2\n',
                               encoding="utf-8")
        git(self.lane, "config", "fetch.bundleURI", bundle_list.as_uri())
        git(self.lane, "config", "fetch.bundleCreationToken", "1")
        # Bundle refs prove the list was consumed; Git builds differ on updating the creation token.
        control = self.fx.tmp / "control.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.fx.tmp / "seed"), str(control)],
                       check=True, env=git_env())
        git(control, "config", "fetch.bundleURI", bundle_list.as_uri())
        git(control, "config", "fetch.bundleCreationToken", control_token)
        git(control, "fetch", "-q", str(self.fx.bare), "+refs/heads/main:refs/remotes/origin/main")
        with self.subTest(control="plain Git incremental bundle list"):
            if not any(r.startswith("refs/bundles/") for r in refs(control)):
                self.skipTest("plain Git created no bundle refs; rerun the control with an unconsumed "
                              "bundle list on a Git build supporting bundle URIs")
        # A skipped control must never skip the repository-isolation assertions.
        before = repository_bytes(self.lane)
        result = self.query("tools/check.py", "--json")
        self.assertEqual(json.loads(result.stdout)["maps"]["default_branch"]["state"], "fresh")
        self.assertEqual(repository_bytes(self.lane), before)
        return result

    def _nonfresh_result(self, saved, mode, reason):
        fx = self.fx
        commit = fx.advance(RELEASE)
        if saved:
            first = run_impact(fx.lane, "tools/check.py", "--json")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["maps"]["default_branch"]["commit"], commit)
        # Tracking refs, including a local remote HEAD, are never fallback authority.
        git(fx.lane, "update-ref", "refs/remotes/origin/main", head(fx.lane))
        options = []
        if mode in ("offline", "no-refresh"):
            options = ["--" + mode]
        elif mode == "disabled":
            (fx.lane / ".oah").mkdir()
            (fx.lane / ".oah/config.toml").write_text("[impact]\nrefresh_default_branch = false\n",
                                                     encoding="utf-8")
        elif mode == "lookup":
            git(fx.lane, "remote", "set-url", "origin", str(fx.tmp / "missing.git"))
        elif mode == "no-symref":
            git(fx.bare, "update-ref", "--no-deref", "HEAD", commit)
        elif mode == "no-remote":
            git(fx.lane, "remote", "remove", "origin")
        if mode == "no-refresh":
            built = run_impact(fx.lane, "build")
            self.assertEqual(built.returncode, 0, built.stderr)
        for output in ([], ["--json"]):
            if mode == "failure":
                git(fx.bare, "update-ref", "refs/heads/main", commit)
            result = (fault_query(fx, mode, "tools/check.py", *options, *output)
                      if mode in ("failure", "timeout") else
                      run_impact(fx.lane, "tools/check.py", *options, *output))
            self.assertEqual(result.returncode, 0, result.stderr)
            state = "stale" if saved else "unavailable"
            label = f"default-branch={state}({reason})"
            self.assertIn(label, result.stdout)
            self.assertIn("OAH_IMPACT_DEFAULT_BRANCH_" + state.upper(), result.stderr)
            if output:
                branch = json.loads(result.stdout)["maps"]["default_branch"]
                self.assertEqual((branch["state"], branch["reason"], branch["commit"]),
                                 (state, reason, commit if saved else None))
            elif saved:
                self.assertIn(f"last-fetched={commit} ref=origin/main", result.stdout)
                self.assertIn("release.yml:2", result.stdout)

    def _failed_new_default(self, fault, reason):
        fx = RemoteFixture(self, default="trunk")
        git(fx.lane, "remote", "set-head", "origin", "-d")
        saved = fx.advance(RELEASE)
        first = run_impact(fx.lane, "tools/check.py")
        self.assertEqual(first.returncode, 0, first.stderr)
        git(fx.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        for output in ([], ["--json"]):
            git(fx.bare, "update-ref", "refs/heads/main", head(fx.pusher))
            result = fault_query(fx, fault, "tools/check.py", *output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"default-branch=stale({reason})", result.stdout)
            self.assertIn("release.yml", result.stdout)
            if output:
                branch = json.loads(result.stdout)["maps"]["default_branch"]
                self.assertEqual((branch["branch"], branch["commit"]), ("trunk", saved))
            else:
                self.assertIn(f"last-fetched={saved} ref=origin/trunk", result.stdout)
        self.assertEqual(mapstore.load(fx.map_files()[0])["manifest"]["ref"], "refs/remotes/origin/trunk")

    def test_remote_default_is_confirmed_despite_an_old_local_head(self):
        self.query("tools/check.py")
        git(self.fx.pusher, "switch", "-q", "-c", "trunk")
        self.fx.default = "trunk"
        commit = self.fx.advance(RELEASE)
        git(self.fx.bare, "symbolic-ref", "HEAD", "refs/heads/trunk")
        before = repository_bytes(self.lane)
        for output in ([], ["--json"]):
            result = self.query("tools/check.py", *output)
            self.assertIn(f"default-branch={commit}", result.stdout)
            if output:
                branch = json.loads(result.stdout)["maps"]["default_branch"]
                self.assertEqual((branch["state"], branch["branch"]), ("fresh", "trunk"))
            else:
                self.assertIn("ref=origin/trunk", result.stdout)
        self.assertEqual(repository_bytes(self.lane), before)

    def test_fetch_uses_global_setup_but_ignores_repository_config(self):
        config = self.fx.tmp / "global-config"
        alias = "oah-local-remote"
        git(self.lane, "config", "--file", str(config), f"url.{self.fx.bare.as_posix()}.insteadOf", alias)
        git(self.lane, "remote", "set-url", "origin", alias)
        git(self.lane, "config", "remote.origin.uploadpack", "oah-no-such-upload-pack")
        commit = self.fx.advance(RELEASE)
        before = repository_bytes(self.lane)
        result = run_impact(self.lane, "tools/check.py", "--json", env_extra={"GIT_CONFIG_GLOBAL": str(config)})
        self.assertEqual(result.returncode, 0, result.stderr)
        branch = json.loads(result.stdout)["maps"]["default_branch"]
        self.assertEqual((branch["state"], branch["commit"]), ("fresh", commit))
        self.assertEqual(repository_bytes(self.lane), before)
        mirrors = [p for p in defaultbranch._generation_home(self.lane, {}).glob("mirror-*") if p.is_dir()]
        self.assertEqual(len(mirrors), 1)
        self.assertEqual(git(mirrors[0], "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(git(mirrors[0], "rev-parse", "refs/remotes/origin/main"), commit)
        self.assertNotIn(alias.encode(), (mirrors[0] / "config").read_bytes())
        self.assertFalse((mirrors[0] / "FETCH_HEAD").exists())

    def test_remote_url_cannot_enter_output_state_or_trace(self):
        sensitive = uuid.uuid4().hex
        self.fx.advance(RELEASE)
        remote = self.fx.tmp / (sensitive + ".git")
        self.fx.bare.rename(remote)
        trace2 = self.fx.tmp / "trace2.json"
        config = self.fx.tmp / "global-config"
        git(self.lane, "config", "--file", str(config), "core.logAllRefUpdates", "always")
        for destination in (remote, self.fx.tmp / (sensitive + "-missing.git")):
            git(self.lane, "remote", "set-url", "origin", str(destination))
            result = run_impact(self.lane, "tools/check.py", "--json",
                env_extra={"GIT_TRACE": str(self.trace), "GIT_TRACE2_EVENT": str(trace2),
                           "GIT_CONFIG_GLOBAL": str(config)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sensitive in result.stdout + result.stderr + trace_text(self.trace) + trace_text(trace2),
                             "the remote URL must not enter output or Git traces")
        state = self.fx.map_files()[0].parent.parent
        for path in state.rglob("*"):
            if path.is_file():
                self.assertFalse(sensitive.encode() in path.read_bytes(), "the remote URL must not enter state")

    def test_mirror_preserves_object_format(self):
        fx = RemoteFixture(self, object_format="sha256")
        commit = fx.advance(RELEASE)
        self.assertEqual(len(commit), 64)
        result = run_impact(fx.lane, "tools/check.py", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        branch = json.loads(result.stdout)["maps"]["default_branch"]
        self.assertEqual((branch["state"], branch["commit"]), ("fresh", commit))

    def test_relative_remote_and_state_override_are_isolated(self):
        commit = self.fx.advance(RELEASE)
        git(self.lane, "remote", "set-url", "origin", "../remote.git")
        state = self.fx.tmp / "private state"
        common = self.lane / ".git"
        before = repository_bytes(self.lane)
        result = run_impact(self.lane, "tools/check.py", "--json", env_extra={
            "OAH_STATE_DIR": str(state), "GIT_DIR": str(common), "GIT_COMMON_DIR": str(common),
            "GIT_WORK_TREE": str(self.lane), "GIT_OBJECT_DIRECTORY": str(common / "objects")})
        self.assertEqual(result.returncode, 0, result.stderr)
        branch = json.loads(result.stdout)["maps"]["default_branch"]
        self.assertEqual((branch["state"], branch["commit"]), ("fresh", commit))
        self.assertEqual(len(list((state / "impact/mirrors").glob("*.generations"))), 1)
        self.assertEqual(len(list((state / "impact/maps").glob("*.default-branch.json"))), 1)
        self.assertEqual(repository_bytes(self.lane), before)

    def test_incomplete_mirror_cannot_fall_back_to_the_user_repository(self):
        mirror = defaultbranch._mirror_path(self.lane, {})
        mirror.mkdir(parents=True)
        (mirror / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.fx.advance(RELEASE)
        before = repository_bytes(self.lane)
        result = self.query("tools/check.py", "--json")
        self.assertEqual(repository_bytes(self.lane), before)
        self.assertEqual(json.loads(result.stdout)["maps"]["default_branch"]["state"], "fresh")

    def test_missing_target_searches_the_mirror_at_the_recorded_sha(self):
        git_commit(self.lane, {"tools/future.py": "print('new in this lane')\n"})
        commit = self.fx.advance({".github/workflows/future.yml": "steps:\n  - run: python tools/future.py\n"})
        for options in ([], ["--offline"]):
            result = self.query("tools/future.py", "--json", *options)
            report = json.loads(result.stdout)
            self.assertEqual(report["maps"]["default_branch"]["commit"], commit)
            self.assertIn((".github/workflows/future.yml", "exec", "default-branch"),
                          {(r["source"], r["binding"], r["found_in"]) for r in report["rows"]})

    def test_pre_mirror_saved_map_remains_usable_after_lookup_failure(self):
        commit = self.fx.advance({".github/workflows/future.yml": "steps:\n  - run: python tools/future.py\n"})
        git(self.lane, "fetch", "-q", "origin")
        mapstore.ref_map(self.lane, "refs/remotes/origin/main", env={})
        git_commit(self.lane, {"tools/future.py": "print('new in this lane')\n"})
        for options in (["--offline"], []):
            git(self.lane, "remote", "set-url", "origin", str(self.fx.tmp / "missing.git"))
            result = self.query("tools/future.py", "--json", *options)
            report = json.loads(result.stdout)
            self.assertEqual(report["maps"]["default_branch"]["commit"], commit)
            self.assertEqual(report["maps"]["default_branch"]["state"], "stale")
            self.assertIn((".github/workflows/future.yml", "exec", "default-branch"),
                          {(r["source"], r["binding"], r["found_in"]) for r in report["rows"]})

    def test_two_maps_show_new_default_branch_check(self):
        before = refs(self.lane)
        new = self.fx.advance(RELEASE)
        result = self.query("tools/check.py")
        self.assertEqual(self.maps_line(result), f"maps: worktree={head(self.lane)} default-branch={new} ref=origin/main")
        self.assertRegex(result.stdout, r"(?m)^exec\s+lexical\s+workflow\s+\.github/workflows/release\.yml:2\s+"
                                        r"tools/check\.py\s+path\s+\[default-branch only\]$")
        self.assertEqual(len(re.findall(r"(?m)^exec\s+lexical\s+workflow\s+\.github/workflows/ci\.yml:2\s", result.stdout)),
                         1, "a row in both maps is printed once")
        self.assertNotRegex(result.stdout, r"ci\.yml:2 .*default-branch only")
        self.assertEqual(refs(self.lane), before)
        self.assertEqual(len(self.fx.map_files()), 1)
        stored = json.loads(self.fx.map_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(stored["commit"], new)
        self.assertNotRegex(trace_text(self.trace), NETWORK_GIT, "URL-bearing Git commands suppress tracing")

        report = json.loads(self.query("tools/check.py", "--json").stdout)
        self.assertEqual(report["label"], f"worktree={head(self.lane)} default-branch={new}")
        branch = report["maps"]["default_branch"]
        self.assertEqual((branch["state"], branch["commit"], branch["ref"]), ("fresh", new, "refs/remotes/origin/main"))
        self.assertEqual(report["maps"]["worktree"]["commit"], head(self.lane))
        found = {(r["source"], r["line"]): r["found_in"] for r in report["rows"]}
        self.assertEqual(found[(".github/workflows/release.yml", 2)], "default-branch")
        self.assertEqual(found[(".github/workflows/ci.yml", 2)], "both")
        self.assertEqual(found[("src/feature.py", 1)], "worktree")
        keys = [(r["binding"], r["layer"], r["source"], r["line"], r["target"]) for r in report["rows"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_changed_since_reruns_default_branch_check(self):
        base = git(self.lane, "merge-base", "HEAD", "origin/main")
        git_commit(self.lane, {"tools/check.py": "print('check v2')\n"})
        self.fx.advance(RELEASE)
        result = self.query("--changed-since", base)
        self.assertIn("re-run: .github/workflows/release.yml:2 (exec tools/check.py)", result.stdout.splitlines())

    def test_unreachable_remote_is_stale(self):
        self.query("tools/check.py")
        fetched = git(self.lane, "rev-parse", "refs/remotes/origin/main")
        self.fx.advance(RELEASE)
        git(self.lane, "remote", "set-url", "origin", str(self.fx.tmp / "gone.git"))
        result = self.query("tools/check.py")
        self.assertEqual(self.maps_line(result), f"maps: worktree={head(self.lane)} default-branch=stale(fetch failed) "
                                                 f"last-fetched={fetched} ref=origin/main")
        self.assertIn("OAH_IMPACT_DEFAULT_BRANCH_STALE", result.stderr)
        self.assertNotIn("release.yml", result.stdout)
        self.assertNotIn(str(self.fx.tmp / "gone.git"), result.stderr, "git's own error text is not echoed")

    def test_offline_runs_no_fetch(self):
        self.query("tools/check.py", trace=False)
        fetched = git(self.lane, "rev-parse", "refs/remotes/origin/main")
        self.fx.advance(RELEASE)
        result = self.query("tools/check.py", "--offline")
        self.assertEqual(self.maps_line(result), f"maps: worktree={head(self.lane)} default-branch=stale(offline) "
                                                 f"last-fetched={fetched} ref=origin/main")
        self.assertIn("OAH_IMPACT_DEFAULT_BRANCH_STALE", result.stderr)
        self.assertIn("built-in: git", trace_text(self.trace), "the trace works")
        self.assertNotRegex(trace_text(self.trace), NETWORK_GIT)
        report = json.loads(self.query("tools/check.py", "--offline", "--json").stdout)
        self.assertEqual(report["label"], f"worktree={head(self.lane)} default-branch=stale(offline)")
        self.assertEqual(report["maps"]["default_branch"]["commit"], fetched)

    def test_refresh_off_runs_no_fetch(self):
        self.query("tools/check.py", trace=False)
        (self.lane / ".oah").mkdir()
        (self.lane / ".oah" / "config.toml").write_text("[impact]\nrefresh_default_branch = false\n", encoding="utf-8")
        result = self.query("tools/check.py")
        self.assertIn("default-branch=stale(refresh_default_branch=false)", self.maps_line(result))
        self.assertNotRegex(trace_text(self.trace), NETWORK_GIT)

    def test_bad_refresh_setting_is_refused(self):
        (self.lane / ".oah").mkdir()
        (self.lane / ".oah" / "config.toml").write_text('[impact]\nrefresh_default_branch = "yes"\n', encoding="utf-8")
        result = run_impact(self.lane, "tools/check.py")
        self.assertEqual(result.returncode, 69, result.stderr)
        self.assertIn("OAH_CONFIG_INVALID", result.stderr)

    def test_no_refresh_runs_no_fetch(self):
        built = run_impact(self.lane, "build")
        self.assertEqual(built.returncode, 0, built.stderr)
        result = self.query("tools/check.py", "--no-refresh")
        self.assertIn("default-branch=unavailable(no-refresh)", self.maps_line(result))
        self.query("tools/check.py", trace=False)
        fetched = git(self.lane, "rev-parse", "refs/remotes/origin/main")
        result = self.query("tools/check.py", "--no-refresh")
        self.assertIn(f"default-branch=stale(no-refresh) last-fetched={fetched}", self.maps_line(result))
        self.assertNotRegex(trace_text(self.trace), NETWORK_GIT)

    def test_never_fetched_is_unavailable(self):
        repo = make_git_repo(self)
        git_commit(repo, BASE)
        git(repo, "remote", "add", "origin", str(self.fx.bare))
        result = run_impact(repo, "tools/check.py", "--offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"maps: worktree={head(repo)} default-branch=unavailable(offline)", result.stdout.splitlines())
        online = run_impact(repo, "tools/check.py")
        self.assertEqual(online.returncode, 0, online.stderr)
        self.assertRegex(online.stdout, rf"default-branch={SHA} ref=origin/main")

    def test_ls_remote_finds_default_branch(self):
        fx = RemoteFixture(self, default="trunk")
        git(fx.lane, "remote", "set-head", "origin", "-d")
        new = fx.advance(RELEASE)
        result = run_impact(fx.lane, "tools/check.py", env_extra={"GIT_TRACE": str(self.trace)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"default-branch={new} ref=origin/trunk", result.stdout)
        self.assertNotRegex(trace_text(self.trace), NETWORK_GIT, "URL-bearing Git commands suppress tracing")
        self.assertNotIn("refs/remotes/origin/HEAD", refs(fx.lane), "the remote HEAD ref is not written")

    def _saved_trunk(self, mode, reason):
        fx = RemoteFixture(self, default="trunk")
        git(fx.lane, "remote", "set-head", "origin", "-d")
        new = fx.advance(RELEASE)
        online = run_impact(fx.lane, "tools/check.py", "--json")
        self.assertEqual(online.returncode, 0, online.stderr)
        self.assertEqual(json.loads(online.stdout)["maps"]["default_branch"]["commit"], new)
        self.assertNotIn("refs/remotes/origin/HEAD", refs(fx.lane))
        options = [mode] if mode.startswith("--") else []
        if mode == "disabled":
            (fx.lane / ".oah").mkdir()
            (fx.lane / ".oah/config.toml").write_text("[impact]\nrefresh_default_branch = false\n",
                                                     encoding="utf-8")
        elif mode == "failed":
            git(fx.lane, "remote", "set-url", "origin", str(fx.tmp / "gone.git"))
        result = run_impact(fx.lane, "tools/check.py", "--json", *options)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        branch = report["maps"]["default_branch"]
        self.assertEqual((branch["state"], branch["reason"], branch["branch"], branch["commit"]),
                         ("stale", reason, "trunk", new))
        self.assertIn(".github/workflows/release.yml", {r["source"] for r in report["rows"]})

    def test_fetch_is_read_only(self):
        marker = self.fx.tmp / "helper-called"
        git(self.lane, "config", "credential.helper", f"!f() {{ echo called >> '{marker.as_posix()}'; }}; f")
        common = pathlib.Path(git(self.lane, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        config_before = git_bytes(self.lane, "config", "--list", "--show-origin")
        local_before = (common / "config").read_bytes()
        fetch_head = (common / "FETCH_HEAD").read_bytes() if (common / "FETCH_HEAD").exists() else None
        refs_before = refs(self.lane)
        self.fx.advance({**RELEASE, "tag-me.txt": "x\n"})
        git(self.fx.pusher, "tag", "v9")
        git(self.fx.pusher, "push", "-q", "origin", "v9")
        self.query("tools/check.py")
        self.assertEqual(git_bytes(self.lane, "config", "--list", "--show-origin"), config_before)
        self.assertEqual((common / "config").read_bytes(), local_before)
        self.assertEqual((common / "FETCH_HEAD").read_bytes() if (common / "FETCH_HEAD").exists() else None, fetch_head)
        self.assertFalse(marker.exists(), "no credential helper ran")
        after = refs(self.lane)
        changed = {name for name in set(refs_before) | set(after) if refs_before.get(name) != after.get(name)}
        self.assertEqual(changed, set())

    def test_fetch_ignores_configured_refmaps(self):
        git(self.lane, "config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/remotes/mirror/*")
        git(self.lane, "config", "--add", "remote.origin.fetch", "+refs/heads/main:refs/heads/mapped-default")
        before = refs(self.lane)
        new = self.fx.advance(RELEASE)
        self.query("tools/check.py")
        after = refs(self.lane)
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        self.assertEqual(changed, set())
        self.assertEqual(mapstore.load(self.fx.map_files()[0])["commit"], new)

    def test_default_branch_coverage_warnings_are_visible(self):
        link = "tools/excluded.py"
        self.fx.advance({link: "check.py\n", "db/unparsed.sql": "CREATE UNRECOGNISED widget;\n"})
        blob = git(self.fx.pusher, "rev-parse", f"HEAD:{link}")
        # Store a real Git symlink entry without requiring OS symlink privileges (including on Windows).
        git(self.fx.pusher, "update-index", "--cacheinfo", f"120000,{blob},{link}")
        git(self.fx.pusher, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "exclude symlink input")
        git(self.fx.pusher, "push", "-q", "origin", "HEAD:main")
        new = head(self.fx.pusher)
        for options in ((), ("--json",)):
            with self.subTest(options=options):
                result = self.query("tools/check.py", *options)
                for code, path in (("OAH_IMPACT_INCOMPLETE", link), ("OAH_IMPACT_SQL_UNPARSED", "db/unparsed.sql")):
                    messages = [line for line in result.stderr.splitlines() if code in line]
                    self.assertEqual(len(messages), 1, result.stderr)
                    self.assertIn("default-branch", messages[0])
                    self.assertIn(new, messages[0])
                    self.assertIn(path, messages[0])
                    self.assertRegex(messages[0], "replace|review")
                if options:
                    report = json.loads(result.stdout)
                    self.assertEqual(report["map"]["warnings"], [])
                    self.assertEqual(report["warnings"], result.stderr.splitlines())
                    self.assertEqual(len(report["warnings"]), 2)

    def test_db_entity_new_in_lane_or_only_on_default_branch(self):
        git_commit(self.lane, {"db/001_users.sql": "CREATE TABLE users (id int);\n",
                               "src/users.py": "SELECT id FROM users\n"})
        self.fx.advance({"db/002_orders.sql": "CREATE TABLE orders (id int);\n"})
        report = json.loads(self.query("db_table:users", "--json").stdout)
        self.assertTrue(report["rows"])
        self.assertEqual({r["found_in"] for r in report["rows"]}, {"worktree"})
        report = json.loads(self.query("db_table:orders", "--json").stdout)
        self.assertEqual({(r["source"], r["found_in"]) for r in report["rows"]},
                         {("db/002_orders.sql", "default-branch")})
        missing = run_impact(self.lane, "db_table:nowhere")
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn("OAH_IMPACT_UNKNOWN_ENTITY", missing.stderr)


def _nonfresh_case(saved, mode, reason):
    def test(self):
        self._nonfresh_result(saved, mode, reason)
    return test


for _saved in (False, True):
    for _mode, _reason in (("offline", "offline"), ("disabled", "refresh_default_branch=false"),
                           ("no-refresh", "no-refresh"), ("lookup", "fetch failed"),
                           ("no-symref", "default branch lookup failed"), ("failure", "fetch failed"),
                           ("timeout", "fetch timed out"), ("no-remote", "no remote")):
        setattr(DefaultBranchCliTest, f"test_nonfresh_{'saved' if _saved else 'unfetched'}_{_mode.replace('-', '_')}",
                _nonfresh_case(_saved, _mode, _reason))


def _branch_case(helper, mode, reason):
    def test(self):
        getattr(self, helper)(mode, reason)
    return test


for _mode, _reason in (("failure", "fetch failed"), ("timeout", "fetch timed out")):
    setattr(DefaultBranchCliTest, f"test_failed_new_default_{_mode}",
            _branch_case("_failed_new_default", _mode, _reason))
for _mode, _reason in (("--offline", "offline"), ("--no-refresh", "no-refresh"),
                       ("disabled", "refresh_default_branch=false"), ("failed", "fetch failed")):
    setattr(DefaultBranchCliTest, f"test_saved_trunk_{_mode.lstrip('-').replace('-', '_')}",
            _branch_case("_saved_trunk", _mode, _reason))


if __name__ == "__main__":
    unittest.main()
