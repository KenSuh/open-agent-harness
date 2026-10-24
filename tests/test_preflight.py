# 사전 점검 검사 — 임시 bare 원격을 두고 기준 최신성·작업 트리·리스 겹침·최근 커밋·패킷·영향·프로젝트 검사와 판정·종료 코드를 본다.
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from oah import cli, intent, preflight, preflight_cli
from oah.lease import Store
from oah.paths import repo_info, state_dir
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", env=git_env()).stdout.strip()


def accepted_packet(task: str, scope: str = "- src\n", status: str = "accepted") -> str:
    return "\n".join([intent.BEGIN, "## Intent", f"Intent-ID: {task}", "Revision: 1", f"Status: {status}",
                      "### Problem", "### Desired outcome", "### Success conditions",
                      "- AC1: the report lists every file", "### Constraints", "### Non-goals",
                      "### Open questions", intent.END, "## Scope", scope.rstrip("\n"), "## Plan",
                      "## Acceptance", "## Return"]) + "\n"


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n", "docs/readme.md": "hi\n"}, "first")
        self.remote = self.repo.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main")
        self.info = repo_info(self.repo)
        self.state = state_dir(self.info, {})

    def other_clone(self) -> pathlib.Path:
        other = self.repo.parent / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        return other

    def push_change(self, files: dict, message: str) -> str:
        other = self.repo.parent / "other"
        if not other.exists():
            self.other_clone()
        git_commit(other, files, message)
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "--short", "HEAD")

    def run_checks(self, **kwargs) -> preflight.Report:
        kwargs.setdefault("touch", ("src/a.py",))
        return preflight.run_checks(repo_info(self.repo), state=self.state, **kwargs)

    def check(self, report: preflight.Report, name: str) -> preflight.Check:
        return next(check for check in report.checks if check.name == name)

    def write_packet(self, task: str, text: str) -> pathlib.Path:
        path = self.repo / ".oah" / "tasks" / task / "PACKET.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        (self.repo / ".git" / "info" / "exclude").write_text(".oah/\n", encoding="utf-8")
        return path

    def test_up_to_date_clean_proceeds(self):
        report = self.run_checks()
        self.assertEqual([c.name for c in report.checks],
                         ["base", "dirty", "leases", "recent", "impact", "packet", "project"])
        self.assertEqual(report.verdict, "PROCEED", report.render())
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(self.check(report, "base").level, "ok")
        self.assertIn("origin/main", self.check(report, "base").message)
        self.assertEqual(report.impact, "unavailable")
        self.assertIn("impact=unavailable", report.render().splitlines())
        with patch("oah.preflight._network_git") as network, \
                patch("oah.preflight._git", wraps=preflight._git) as local:
            offline = self.run_checks(offline=True)
        self.assertEqual(offline.verdict, "PROCEED_WITH_WARNINGS", offline.render())
        self.assertEqual(self.check(offline, "base").level, "warn")
        network.assert_not_called()
        self.assertTrue(any(call.args[1:] == ("rev-list", "--count", "HEAD..refs/remotes/origin/main")
                            for call in local.call_args_list))

    def test_behind_remote_aborts_with_rebase_command(self):
        self.push_change({"docs/other.md": "x\n"}, "elsewhere")
        for offline in (False, True):
            with self.subTest(offline=offline):
                report = self.run_checks(offline=offline)
                base = self.check(report, "base")
                self.assertEqual(base.level, "abort", report.render())
                source = "last fetched " if offline else ""
                self.assertIn(f"1 commit behind {source}origin/main", base.message)
                self.assertIn("git rebase origin/main", base.message)
                self.assertEqual((report.verdict, report.exit_code), ("ABORT", 2))

    def test_ahead_of_remote_is_fine(self):
        git_commit(self.repo, {"src/b.py": "b = 1\n"}, "local work")
        for offline, level in ((False, "ok"), (True, "warn")):
            with self.subTest(offline=offline):
                self.assertEqual(self.check(self.run_checks(offline=offline), "base").level, level)

    def test_unreachable_remote_aborts_unless_offline(self):
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "remote", "set-url", "origin", str(self.repo.parent / "gone.git"))
        report = self.run_checks()
        self.assertEqual(self.check(report, "base").level, "abort", report.render())
        self.assertIn("--offline", self.check(report, "base").message)
        offline = self.run_checks(offline=True)
        self.assertEqual(self.check(offline, "base").level, "warn", offline.render())
        self.assertIn("last fetched origin/main", self.check(offline, "base").message)
        self.assertEqual(offline.verdict, "PROCEED_WITH_WARNINGS")
        self.assertTrue(any("--offline" in line for line in offline.not_proven))

    def test_offline_still_sees_known_behind(self):
        self.push_change({"docs/other.md": "x\n"}, "elsewhere")
        git(self.repo, "fetch", "-q", "origin")
        self.assertEqual(self.check(self.run_checks(offline=True), "base").level, "abort")

    def test_no_remote_warns(self):
        git(self.repo, "remote", "remove", "origin")
        for settings in ({}, {"remote": "typo"}):
            for offline in (False, True):
                with self.subTest(settings=settings, offline=offline):
                    report = self.run_checks(settings=settings, offline=offline)
                    self.assertEqual(self.check(report, "base").level, "warn", report.render())
                    self.assertIn("no remote", self.check(report, "base").message)
                    self.assertEqual(report.exit_code, 0)

    def test_unknown_configured_remote_aborts(self):
        for offline in (False, True):
            with self.subTest(offline=offline):
                report = self.run_checks(settings={"remote": "typo"}, offline=offline)
                self.assertEqual(self.check(report, "base").level, "abort", report.render())
                self.assertEqual((report.verdict, report.exit_code), ("ABORT", 2))
                self.assertIn("typo", self.check(report, "base").message)
                self.assertIn("[preflight] remote", self.check(report, "base").message)
                if offline:
                    self.assertIn("without --offline", self.check(report, "base").message)

    def test_remote_inspection_failure_aborts(self):
        real_git = preflight._git

        def fail_remote(ctx, *args):
            return (1, "", "cannot inspect remotes") if args == ("remote",) else real_git(ctx, *args)

        for offline in (False, True):
            with self.subTest(offline=offline), patch("oah.preflight._git", side_effect=fail_remote):
                report = self.run_checks(offline=offline)
                self.assertEqual(self.check(report, "base").level, "abort", report.render())
                self.assertEqual((report.verdict, report.exit_code), ("ABORT", 2))
                if offline:
                    self.assertIn("without --offline", self.check(report, "base").message)

    def test_empty_remote_warns(self):
        empty = self.repo.parent / "empty.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(empty)], check=True)
        git(self.repo, "remote", "set-url", "origin", str(empty))
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        for offline, level in ((True, "abort"), (False, "warn")):
            with self.subTest(offline=offline):
                report = self.run_checks(offline=offline)
                base = self.check(report, "base")
                self.assertEqual(base.level, level, report.render())
                self.assertIn("no default branch", base.message)
                if offline:
                    self.assertIn("without --offline", base.message)

    def test_configured_branch_never_fetched_aborts_offline(self):
        # Re-adding the remote leaves its real main branch present only on the remote.
        git(self.repo, "remote", "remove", "origin")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        for offline, level in ((True, "abort"), (False, "ok")):
            with self.subTest(offline=offline):
                report = self.run_checks(offline=offline, settings={"default_branch": "main"})
                base = self.check(report, "base")
                self.assertEqual(base.level, level, report.render())
                self.assertEqual(report.exit_code, 2 if offline else 0)
                if offline:
                    self.assertIn("not known locally", base.message)
                    self.assertIn("git fetch origin", base.message)
                    self.assertIn("without --offline", base.message)

    def test_unborn_head_requires_comparison_offline(self):
        repo = make_git_repo(self)
        git(repo, "remote", "add", "origin", str(self.remote))
        git(repo, "fetch", "-q", "origin")
        for offline, level in ((False, "warn"), (True, "abort")):
            with self.subTest(offline=offline):
                report = preflight.run_checks(repo_info(repo), state=repo.parent / "state", touch=("src",),
                                              offline=offline, settings={"default_branch": "main"})
                base = self.check(report, "base")
                self.assertEqual(base.level, level, report.render())
                self.assertIn("no commits", base.message)
                if offline:
                    self.assertIn("without --offline", base.message)

    def test_default_branch_discovery_failure_aborts(self):
        git(self.repo, "remote", "remove", "origin")
        git(self.repo, "remote", "add", "origin", str(self.repo.parent / "gone.git"))
        for offline in (False, True):
            with self.subTest(offline=offline):
                report = self.run_checks(offline=offline)
                base = self.check(report, "base")
                self.assertEqual(base.level, "abort", report.render())
                self.assertEqual(report.exit_code, 2)
                self.assertIn("without --offline" if offline else "cannot reach origin", base.message)

    def test_fetch_failure_uses_cached_comparison_only_offline(self):
        git(self.repo, "remote", "set-url", "origin", str(self.repo.parent / "gone.git"))
        for offline, level in ((False, "abort"), (True, "warn")):
            with self.subTest(offline=offline):
                report = self.run_checks(offline=offline, settings={"default_branch": "main"})
                base = self.check(report, "base")
                self.assertEqual(base.level, level, report.render())
                self.assertIn("last fetched origin/main" if offline else "cannot fetch origin/main",
                              base.message)

    def test_tracking_ref_lost_after_fetch(self):
        real_network_git = preflight._network_git

        def fetch_then_lose_ref(ctx, *args):
            result = real_network_git(ctx, *args)
            if args[0] == "fetch" and result[0] == 0:
                git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
            return result

        with patch("oah.preflight._network_git", side_effect=fetch_then_lose_ref):
            report = self.run_checks(settings={"default_branch": "main"})
        self.assertEqual(self.check(report, "base").level, "warn", report.render())
        self.assertIn("not known locally", self.check(report, "base").message)
        offline = self.run_checks(offline=True, settings={"default_branch": "main"})
        self.assertEqual(self.check(offline, "base").level, "abort", offline.render())

    def test_comparison_failure_aborts_in_both_modes(self):
        real_git = preflight._git

        def fail_comparison(ctx, *args):
            if args[0] == "rev-list":
                # Run git against a missing HEAD to exercise a real comparison failure.
                return real_git(ctx, "rev-list", "--count", "refs/heads/missing.." + ctx.ref)
            return real_git(ctx, *args)

        for offline in (False, True):
            with self.subTest(offline=offline), patch("oah.preflight._git", side_effect=fail_comparison):
                report = self.run_checks(offline=offline)
                base = self.check(report, "base")
                self.assertEqual(base.level, "abort", report.render())
                self.assertIn("could not compare HEAD", base.message)
                if offline:
                    self.assertIn("without --offline", base.message)

    def test_configured_branch_and_remote(self):
        git(self.repo, "remote", "rename", "origin", "upstream")
        git(self.repo, "remote", "add", "mirror", str(self.remote))
        for offline in (False, True):
            with self.subTest(offline=offline):
                report = self.run_checks(offline=offline)
                self.assertEqual(self.check(report, "base").level, "abort", report.render())
                self.assertEqual((report.verdict, report.exit_code), ("ABORT", 2))
                self.assertIn("[preflight] remote", self.check(report, "base").message)
                if offline:
                    self.assertIn("without --offline", self.check(report, "base").message)
        report = self.run_checks(settings={"remote": "upstream", "default_branch": "main"})
        self.assertEqual(self.check(report, "base").level, "ok", report.render())
        self.assertIn("upstream/main", self.check(report, "base").message)

    def test_dirty_tree_warns(self):
        (self.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("n\n", encoding="utf-8")
        report = self.run_checks()
        dirty = self.check(report, "dirty")
        self.assertEqual(dirty.level, "warn")
        self.assertIn("2 uncommitted", dirty.message)
        self.assertIn("src/a.py", " ".join(dirty.details))
        self.assertEqual(report.verdict, "PROCEED_WITH_WARNINGS")

    def test_overlapping_lease_aborts_and_stale_warns(self):
        store = Store(self.state)
        store.start(task="other", agent="codex", scope=("src",), note="", ttl_hours=1, info=self.info)
        report = self.run_checks(touch=("src/a.py",))
        leases = self.check(report, "leases")
        self.assertEqual(leases.level, "abort", report.render())
        self.assertIn("other", leases.message)
        self.assertEqual(self.check(self.run_checks(touch=("docs",)), "leases").level, "ok")

        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
        stale_dir = make_tmp_dir(self) / "state"
        Store(stale_dir, now=lambda: past).start(task="old", agent="codex", scope=("src",), note="",
                                                 ttl_hours=1, info=self.info)
        stale = preflight.run_checks(repo_info(self.repo), state=stale_dir, touch=("src/a.py",))
        self.assertEqual(self.check(stale, "leases").level, "warn", stale.render())
        self.assertIn("STALE", self.check(stale, "leases").message)

    def test_own_lease_does_not_conflict(self):
        Store(self.state).start(task="mine", agent="claude", scope=("src",), note="", ttl_hours=1, info=self.info)
        self.write_packet("mine", accepted_packet("mine"))
        report = self.run_checks(task="mine")
        self.assertEqual(self.check(report, "leases").level, "ok", report.render())

    def test_empty_compare_scope_warns(self):
        report = self.run_checks(touch=())
        leases = self.check(report, "leases")
        self.assertEqual(leases.level, "warn")
        self.assertIn("OAH_SCOPE_EMPTY", leases.message)

    def test_invalid_touch_path_aborts(self):
        report = self.run_checks(touch=("../outside",))
        self.assertEqual(self.check(report, "leases").level, "abort")
        self.assertIn("OAH_SCOPE_INVALID", self.check(report, "leases").message)

    def test_recent_commits_touching_scope_warn(self):
        Store(self.state).start(task="t", agent="claude", scope=("src",), note="", ttl_hours=1, info=self.info)
        self.write_packet("t", accepted_packet("t"))
        touching = self.push_change({"src/a.py": "a = 3\n"}, "change a")
        self.push_change({"docs/x.md": "x\n"}, "docs only")
        git(self.repo, "pull", "-q", "--rebase", "origin", "main")
        report = self.run_checks(task="t", touch=())
        recent = self.check(report, "recent")
        self.assertEqual(recent.level, "warn", report.render())
        self.assertIn("1 commit", recent.message)
        self.assertEqual(len(recent.details), 1)
        self.assertIn(touching, recent.details[0])
        self.assertIn("change a", recent.details[0])

    def test_recent_commits_since_packet_edit(self):
        path = self.write_packet("t", accepted_packet("t"))
        old = time.time() - 3600
        os.utime(path, (old, old))
        self.push_change({"src/a.py": "a = 4\n"}, "change a again")
        git(self.repo, "pull", "-q", "--rebase", "origin", "main")
        recent = self.check(self.run_checks(task="t"), "recent")
        self.assertEqual(recent.level, "warn")
        self.assertIn("change a again", recent.details[0])

    def test_recent_skipped_without_base(self):
        self.assertEqual(self.check(self.run_checks(), "recent").level, "info")

    def test_packet_must_be_accepted_with_acs(self):
        missing = self.run_checks(task="nothing")
        self.assertEqual(self.check(missing, "packet").level, "abort")
        self.assertIn("oah packet new nothing", self.check(missing, "packet").message)
        self.write_packet("t", accepted_packet("t", status="draft"))
        draft = self.run_checks(task="t")
        self.assertEqual(self.check(draft, "packet").level, "abort")
        self.assertIn("OAH_INTENT_NOT_ACCEPTED", " ".join(self.check(draft, "packet").details))
        self.write_packet("t", accepted_packet("t").replace("- AC1: the report lists every file\n", ""))
        self.assertEqual(self.check(self.run_checks(task="t"), "packet").level, "abort")
        self.write_packet("t", accepted_packet("t"))
        good = self.run_checks(task="t")
        self.assertEqual(self.check(good, "packet").level, "ok", good.render())
        self.write_packet("t", accepted_packet("t", scope=""))
        empty_scope = self.run_checks(task="t", touch=())
        self.assertEqual(self.check(empty_scope, "packet").level, "warn")
        self.assertIn("OAH_SCOPE_EMPTY", " ".join(self.check(empty_scope, "packet").details))

    def test_packet_scope_feeds_lease_overlap(self):
        Store(self.state).start(task="other", agent="codex", scope=("src/a.py",), note="", ttl_hours=1,
                                info=self.info)
        self.write_packet("t", accepted_packet("t"))
        self.assertEqual(self.check(self.run_checks(task="t", touch=()), "leases").level, "abort")

    def test_project_checks_hook(self):
        ok = [sys.executable, "-c", "print('migration numbers free')"]
        failing = [sys.executable, "-c", "import sys; print('number 7 is taken'); sys.exit(3)"]
        report = self.run_checks(project_checks=[ok])
        self.assertEqual(self.check(report, "project").level, "ok", report.render())
        report = self.run_checks(project_checks=[ok, failing])
        project = self.check(report, "project")
        self.assertEqual(project.level, "abort")
        self.assertIn("exit 3", project.message)
        self.assertIn("number 7 is taken", " ".join(project.details))
        absent = self.run_checks(project_checks=[["oah-no-such-tool-xyz"]])
        self.assertEqual(self.check(absent, "project").level, "abort")
        self.assertIn("not found", self.check(absent, "project").message)
        none = self.run_checks()
        self.assertEqual(self.check(none, "project").level, "info")
        self.assertTrue(any("[preflight].checks" in line for line in none.not_proven))

    def test_project_check_runs_in_repository_root(self):
        probe = [sys.executable, "-c", "import os, sys; sys.exit(0 if os.path.isdir('src') else 4)"]
        self.assertEqual(self.check(self.run_checks(project_checks=[probe]), "project").level, "ok")

    def test_impact_hook(self):
        seen = []

        def impact(root, paths):
            seen.append((root, paths))
            return ["gates#G3 binds src/a.py"]

        report = self.run_checks(impact=impact)
        self.assertEqual(seen, [(self.info.root, ("src/a.py",))])
        self.assertEqual(report.impact, "used")
        self.assertIn("gates#G3 binds src/a.py", self.check(report, "impact").details)
        self.assertEqual(self.check(report, "impact").level, "info")

        def broken(root, paths):
            raise RuntimeError("index is corrupt")

        failed = self.run_checks(impact=broken)
        self.assertEqual(self.check(failed, "impact").level, "warn")
        self.assertTrue(failed.impact.startswith("failed("))
        self.assertEqual(self.run_checks(impact=impact, touch=()).impact, "skipped(no --touch paths)")

    def test_impact_refusal_aborts(self):
        def stale(root, paths):
            raise preflight.ImpactRefused("OAH_IMPACT_STALE", "the map moved while it was built")

        report = self.run_checks(impact=stale)
        check = self.check(report, "impact")
        self.assertEqual((check.level, report.verdict, report.impact), ("abort", "ABORT", "stale"))
        self.assertTrue(check.message.startswith("OAH_IMPACT_STALE: the map moved"), check.message)

        def empty(root, paths):
            raise preflight.ImpactUnavailable("no commits yet")

        report = self.run_checks(impact=empty)
        self.assertEqual((self.check(report, "impact").level, report.impact), ("info", "unavailable(no commits yet)"))

    def test_refresh_impact_refuses_or_reports_unavailable(self):
        from oah.impact import ImpactError

        def raising(error):
            def ensure(root):
                raise error
            return ensure

        for error in (ImpactError("OAH_IMPACT_STALE", "inputs kept moving"), OSError(5, "disk error"),
                      ImpactError("OAH_IMPACT_LOCKED", "busy")):
            with self.subTest(error=error), self.assertRaises(preflight.ImpactRefused) as caught:
                preflight.refresh_impact(self.repo, ensure=raising(error))
            self.assertEqual(caught.exception.code, "OAH_IMPACT_STALE")
            self.assertIn("oah impact refresh", str(caught.exception))
        with self.assertRaises(preflight.ImpactUnavailable):
            preflight.refresh_impact(self.repo, ensure=raising(ImpactError("OAH_IMPACT_NO_COMMIT", "none")))

    def test_touch_lists_strong_rows_from_fresh_map(self):
        git_commit(self.repo, {".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n      - run: python src/a.py\n"})
        lines = preflight.impact_hook(self.repo, ("src/a.py",))
        self.assertTrue(lines[0].startswith("map "), lines)
        self.assertIn("exec .github/workflows/ci.yml:4 for src/a.py", lines)
        self.assertTrue(preflight.impact_hook(self.repo, ())[0].startswith("map fresh at "))

    def test_output_ends_with_not_proven_list(self):
        report = self.run_checks()
        lines = report.render().rstrip("\n").splitlines()
        heading = lines.index("Not proven by this preflight:")
        self.assertTrue(lines[heading - 1].startswith("OAH_PREFLIGHT verdict=PROCEED"))
        self.assertGreater(len(lines) - heading - 1, 2)
        self.assertTrue(all(line.startswith("- ") for line in lines[heading + 1:]))
        self.assertTrue(any("impact=unavailable" in line for line in lines[heading + 1:]))

    def test_fetch_never_prompts(self):
        captured = {}
        real = preflight.run_command

        def spy(argv, **kwargs):
            if "fetch" in argv or "ls-remote" in argv:
                captured["env"] = kwargs.get("env")
            return real(argv, **kwargs)

        with patch("oah.preflight.run_command", side_effect=spy):
            self.run_checks()
        self.assertEqual(captured["env"]["GIT_TERMINAL_PROMPT"], "0")


def run_cli(cwd: pathlib.Path, *argv: str, env: dict | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    previous = pathlib.Path.cwd()
    os.chdir(cwd)
    try:
        with patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class PreflightCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n"})
        self.remote = self.repo.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main")

    def test_cli_refreshes_impact_without_touch(self):
        import oah.impact

        calls = []
        real = oah.impact.ensure_fresh

        def spy(root, **kwargs):
            calls.append((pathlib.Path(root), kwargs))
            return real(root, **kwargs)

        with patch.object(oah.impact, "ensure_fresh", spy):
            code, out, err = run_cli(self.repo, "preflight", "--offline")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([root.resolve() for root, _ in calls], [self.repo.resolve()])
        impact = next(line for line in out.splitlines() if line.lstrip().startswith("[info]") and "impact:" in line)
        self.assertIn("the impact map is current", impact)

    def test_cli_without_commits_reports_impact_unavailable(self):
        empty = make_git_repo(self)
        code, out, err = run_cli(empty, "preflight", "--offline")
        self.assertIn("impact=unavailable(no commits yet)", out.splitlines(), out + err)

    def test_cli_exit_codes(self):
        self.assertEqual(preflight_cli.COMMANDS, ("preflight",))
        code, out, err = run_cli(self.repo, "preflight", "--touch", "src/a.py", "src")
        self.assertEqual(code, 0, out + err)
        self.assertIn("OAH_PREFLIGHT verdict=PROCEED", out)
        self.assertTrue(out.rstrip("\n").splitlines()[-1].startswith("- "))
        code, out, _ = run_cli(self.repo, "preflight", "--task", "missing", "--touch", "src")
        self.assertEqual(code, 2)
        self.assertIn("OAH_PREFLIGHT verdict=ABORT", out)
        git(self.repo, "remote", "set-url", "origin", str(self.repo.parent / "gone.git"))
        self.assertEqual(run_cli(self.repo, "preflight", "--touch", "src")[0], 2)
        self.assertEqual(run_cli(self.repo, "preflight", "--offline", "--touch", "src")[0], 0)

    def write_config(self, text: str) -> None:
        (self.repo / ".oah").mkdir(exist_ok=True)
        (self.repo / ".oah" / "config.toml").write_text(text, encoding="utf-8")
        (self.repo / ".git" / "info" / "exclude").write_text(".oah/\n", encoding="utf-8")

    def test_commands_are_discovered(self):
        commands = cli.build_parser()._subparsers._group_actions[0].choices
        for name in ("packet", "intent", "preflight"):
            self.assertIn(name, commands)

    def test_cli_runs_configured_project_checks(self):
        script = self.repo.parent / "check_numbers.py"
        script.write_text("import sys\nprint('migration 7 is taken')\nsys.exit(5)\n", encoding="utf-8")
        # A JSON array of strings is also a valid TOML array, with the same backslash escapes.
        self.write_config(f"[preflight]\nchecks = [{json.dumps([sys.executable, str(script)])}]\n")
        code, out, err = run_cli(self.repo, "preflight", "--touch", "src")
        self.assertEqual(code, 2, out + err)
        self.assertIn("[abort] project:", out)
        self.assertIn("migration 7 is taken", out)
        self.write_config('[preflight]\ncheck_timeout = 30\nchecks = []\n')
        code, out, _ = run_cli(self.repo, "preflight", "--touch", "src")
        self.assertEqual(code, 0, out)
        self.assertIn("no project checks configured", out)

    def test_cli_configured_remote_and_branch(self):
        git(self.repo, "remote", "rename", "origin", "upstream")
        git(self.repo, "remote", "add", "mirror", str(self.remote))
        self.write_config('[preflight]\nremote = "upstream"\ndefault_branch = "main"\n')
        code, out, _ = run_cli(self.repo, "preflight", "--touch", "src")
        self.assertEqual(code, 0, out)
        self.assertIn("up to date with upstream/main", out)

    def test_cli_config_errors_exit_69(self):
        cases = {
            "unknown key": ('[preflight]\nremotes = "x"\n', "OAH_CONFIG_UNKNOWN_KEY"),
            "wrong type": ("[preflight]\nfetch_timeout = \"soon\"\n", "OAH_CONFIG_INVALID"),
            "bad check entry": ('[preflight]\nchecks = ["python check.py"]\n', "OAH_CONFIG_INVALID"),
            "empty check entry": ("[preflight]\nchecks = [[]]\n", "OAH_CONFIG_INVALID"),
        }
        for name, (text, code_name) in cases.items():
            with self.subTest(name):
                self.write_config(text)
                code, _, err = run_cli(self.repo, "preflight", "--touch", "src")
                self.assertEqual(code, 69)
                self.assertTrue(err.startswith(code_name), err)

    def test_cli_outside_repository(self):
        outside = make_tmp_dir(self)
        code, _, err = run_cli(outside, "preflight", env={"GIT_CEILING_DIRECTORIES": str(outside.parent)})
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_NOT_A_REPO"), err)


if __name__ == "__main__":
    unittest.main()
