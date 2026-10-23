# 기본 브랜치 영향 검사 — 로컬 bare 원격만으로 가져오기·두 번째 지도·두 SHA 표기·낡음/없음 표기·읽기 전용 가져오기를 실제 프로세스로 본다.
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from oah.engines.proc import RunResult, run_command
from oah.impact import ImpactError, defaultbranch, ensure_fresh, mapstore, query
from oah.impact.defaults import DEFAULTS, MAP_KEYS, configured, effective
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir
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


class RemoteFixture:
    """A local bare remote seeded with BASE, a lane clone on its own branch, and a second clone that pushes."""

    def __init__(self, test: unittest.TestCase, default: str = "main") -> None:
        self.tmp = make_tmp_dir(test)
        seed = self.tmp / "seed"
        subprocess.run(["git", "init", "-q", "-b", default, str(seed)], check=True)
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


class RemoteSelectionTest(unittest.TestCase):
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

    def test_fetch_timeout_is_stale(self):
        fetched = git(self.fx.lane, "rev-parse", "refs/remotes/origin/main")

        def slow_fetch(argv, **kwargs):
            if "fetch" in argv:
                return RunResult(None, "", "", True)
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

        def remove_after_discovery(argv, **kwargs):
            result = run_command(argv, **kwargs)
            if "ls-remote" in argv:
                git(fx.bare, "update-ref", "-d", "refs/heads/trunk")
            if "fetch" in argv:
                calls.append(result.returncode)
            return result

        with patch("oah.impact.defaultbranch.run_command", side_effect=remove_after_discovery):
            result = defaultbranch.refresh(fx.lane, settings, env={})
        self.assertTrue(calls and calls[0] != 0, "the real fetch failed after discovery succeeded")
        self.assertEqual((result.state, result.reason, result.branch, result.commit),
                         ("stale", "fetch failed", "trunk", new))
        self.assertEqual(result.data, saved.data)

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

        def probe(argv, **kwargs):
            action = next((a for a in ("ls-remote", "fetch") if a in argv), None)
            if action:
                calls.append(action)
                # Exercise Git's real credential path with the network command's config and environment,
                # without contacting any host or returning a credential.
                result = run_command((*argv[:argv.index(action)], "-c", "credential.helper=", "credential", "fill"),
                                     timeout=5.0, env=kwargs["env"],
                                     stdin_text="protocol=https\nhost=credential.invalid\n\n")
                self.assertFalse(result.timed_out)
                self.assertNotEqual(result.returncode, 0)
            return run_command(argv, **kwargs)

        with patch.dict(os.environ):
            for name in ("GIT_ASKPASS", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
                os.environ.pop(name, None)
            if source != "core.askPass":
                os.environ[source] = command
            control = run_command(("git", "-C", str(self.fx.lane), "-c", "credential.helper=", "credential", "fill"),
                                  timeout=5.0, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                                  stdin_text="protocol=https\nhost=credential.invalid\n\n")
            self.assertFalse(control.timed_out)
            self.assertTrue(marker.exists(), "the offline probe must exercise the configured askpass")
            marker.unlink()
            with patch("oah.impact.defaultbranch.run_command", side_effect=probe):
                self.assertEqual(defaultbranch.default_branch_name(self.fx.lane, "origin", network=True,
                                                                  timeout=30.0), ("main", ""))
                self.assertEqual(defaultbranch.fetch(self.fx.lane, "origin", "main", 30.0), "")
        self.assertEqual(calls, ["ls-remote", "fetch"])
        self.assertFalse(marker.exists(), f"{source} must not run during discovery or fetch")

    def test_discovery_and_fetch_disable_askpass(self):
        git(self.fx.lane, "remote", "set-head", "origin", "-d")
        for source in ("GIT_ASKPASS", "core.askPass", "SSH_ASKPASS"):
            with self.subTest(source=source):
                git(self.fx.lane, "config", "core.askPass", "")
                git(self.fx.lane, "config", "--unset", "core.askPass")
                self._assert_no_askpass(source)

    def test_discovery_and_fetch_force_noninteractive_ssh(self):
        if shutil.which("ssh") is None:
            self.skipTest("OpenSSH is required to inspect its effective configuration")
        git(self.fx.lane, "remote", "set-head", "origin", "-d")
        git(self.fx.lane, "config", "core.sshCommand", "ssh -oBatchMode=no")
        calls = []

        def probe(argv, **kwargs):
            action = next((a for a in ("ls-remote", "fetch") if a in argv), None)
            if action:
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


class RefMapTest(unittest.TestCase):
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
        result = run_impact(self.fx.lane, "refresh", env_extra={"GIT_TRACE": str(trace)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotRegex(trace_text(trace), NETWORK_GIT)


class DefaultBranchCliTest(unittest.TestCase):
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

    def test_two_maps_show_new_default_branch_check(self):
        new = self.fx.advance(RELEASE)
        result = self.query("tools/check.py")
        self.assertEqual(self.maps_line(result), f"maps: worktree={head(self.lane)} default-branch={new} ref=origin/main")
        self.assertRegex(result.stdout, r"(?m)^exec\s+lexical\s+workflow\s+\.github/workflows/release\.yml:2\s+"
                                        r"tools/check\.py\s+path\s+\[default-branch only\]$")
        self.assertEqual(len(re.findall(r"(?m)^exec\s+lexical\s+workflow\s+\.github/workflows/ci\.yml:2\s", result.stdout)),
                         1, "a row in both maps is printed once")
        self.assertNotRegex(result.stdout, r"ci\.yml:2 .*default-branch only")
        self.assertEqual(git(self.lane, "rev-parse", "refs/remotes/origin/main"), new)
        self.assertEqual(len(self.fx.map_files()), 1)
        stored = json.loads(self.fx.map_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(stored["commit"], new)
        self.assertRegex(trace_text(self.trace), r"built-in: git .*fetch .*--no-write-fetch-head")

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

    def test_no_remote_is_unavailable(self):
        repo = make_git_repo(self)
        git_commit(repo, BASE)
        result = run_impact(repo, "tools/check.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"maps: worktree={head(repo)} default-branch=unavailable(no remote)", result.stdout.splitlines())
        self.assertIn("OAH_IMPACT_DEFAULT_BRANCH_UNAVAILABLE", result.stderr)
        self.assertRegex(result.stdout, r"exec\s+lexical\s+workflow\s+\.github/workflows/ci\.yml:2")

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
        self.assertRegex(trace_text(self.trace), r"built-in: git .*ls-remote")
        self.assertNotIn("refs/remotes/origin/HEAD", refs(fx.lane), "the remote HEAD ref is not written")

    def test_saved_trunk_survives_skipped_or_failed_discovery(self):
        for mode, reason in (("--offline", "offline"), ("--no-refresh", "no-refresh"),
                             ("disabled", "refresh_default_branch=false"), ("failed", "fetch failed")):
            with self.subTest(mode=mode):
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
        new = self.fx.advance({**RELEASE, "tag-me.txt": "x\n"})
        git(self.fx.pusher, "tag", "v9")
        git(self.fx.pusher, "push", "-q", "origin", "v9")
        self.query("tools/check.py")
        self.assertEqual(git_bytes(self.lane, "config", "--list", "--show-origin"), config_before)
        self.assertEqual((common / "config").read_bytes(), local_before)
        self.assertEqual((common / "FETCH_HEAD").read_bytes() if (common / "FETCH_HEAD").exists() else None, fetch_head)
        self.assertFalse(marker.exists(), "no credential helper ran")
        after = refs(self.lane)
        changed = {name for name in set(refs_before) | set(after) if refs_before.get(name) != after.get(name)}
        self.assertEqual(changed, {"refs/remotes/origin/main"})
        self.assertEqual(after["refs/remotes/origin/main"], new)

    def test_fetch_ignores_configured_refmaps(self):
        git(self.lane, "config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/remotes/mirror/*")
        git(self.lane, "config", "--add", "remote.origin.fetch", "+refs/heads/main:refs/heads/mapped-default")
        before = refs(self.lane)
        new = self.fx.advance(RELEASE)
        self.query("tools/check.py")
        after = refs(self.lane)
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        self.assertEqual(changed, {"refs/remotes/origin/main"})
        self.assertEqual(after["refs/remotes/origin/main"], new)

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


if __name__ == "__main__":
    unittest.main()
