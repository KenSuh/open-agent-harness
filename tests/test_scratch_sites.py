# 작업 폴더(scratch) 자리별 검사 — S1~S13 의 각 자리가 공유 시스템 임시 폴더가 아니라 증명된 사용자 전용 폴더 아래에 작업 폴더를 만드는지, 그 폴더를 증명할 수 없으면 다른 곳을 쓰지 않고 OAH_SCRATCH_UNSAFE 로 멈추는지 본다.
from __future__ import annotations

import contextlib
import datetime as dt
import gc
import io
import os
import pathlib
import shutil
import sys
import unittest

from tests.helpers import (git_commit, git_env, hostile_temp, make_git_repo, make_tmp_dir, scratch_base,
                           unprovable_folder, unsafe_scratch)
from tests.test_design_sources import LICENSE, SourceCase, git


class SiteCase(unittest.TestCase):
    """Each site runs while TMPDIR, TEMP, TMP and tempfile.tempdir name a shared folder any account may write.

    These tests import no oah.scratch, so on the old code they fail by assertion, not by import."""

    def assert_scratch(self, created: list[pathlib.Path], shared: pathlib.Path, *prefixes: str) -> None:
        base = scratch_base()
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertTrue([path for path in created if path.name.startswith(prefix)],
                                f"no {prefix} folder was created")
        self.assertEqual([str(path) for path in created if path.parent != base], [],
                         f"every scratch folder sits directly in the proven base {base}")
        self.assertEqual(sorted(shared.iterdir()), [], "the shared system temp stays unused")


class ReviewSites(SiteCase):
    def test_s1_review_snapshot_with_and_without_a_design_disposal(self):
        from oah.review import snapshot

        repo = make_git_repo(self)
        base = git_commit(repo, {"README.md": "base\n"})
        head = git_commit(repo, {"src/app.py": "print('hi')\n"})

        def dispose(parent, _created):
            shutil.rmtree(parent)
            return ("disposed", None)

        for disposal in (None, dispose):
            with self.subTest(design=disposal is not None), hostile_temp(self) as (shared, created):
                with snapshot.make(repo, head, base=base, dispose=disposal) as snap:
                    self.assertEqual((snap.tree / "src/app.py").read_bytes(), b"print('hi')\n")
                self.assert_scratch(created, shared, "oah-review-")

    def test_s12_correction_scratch(self):
        from oah.review import fix
        from oah.secrets import NoKeychain
        from tests.test_fix import FIXED, FixProject
        from tests.test_review_rounds import RULES

        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        with hostile_temp(self) as (shared, created), contextlib.redirect_stderr(io.StringIO()):
            result = fix.correct(fix.Request(task="t1", tier="high"), cwd=project.repo, env=project.env,
                                 keychain=NoKeychain(), rules=RULES)
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        self.assert_scratch(created, shared, "oah-fix-")


class ToolkitSites(SiteCase):
    def test_s2_task_scheduler_definition_is_written_in_scratch(self):
        from oah.toolkit import schedule

        root = make_tmp_dir(self)
        empty = root / "no-tools"
        empty.mkdir()
        entry = schedule.Entry(kind="schtasks", name="oah-test-task", path=None, argv=("oah", "skills", "update"),
                               project_root=root, log_path=root / "log.txt")
        with hostile_temp(self) as (shared, created):
            with self.assertRaises(schedule.ScheduleError) as caught:  # schtasks is looked up after the definition
                schedule.install_entry(entry, search_path=str(empty))
            self.assertEqual(caught.exception.code, "OAH_SCHEDULE_TOOL_MISSING")
        self.assert_scratch(created, shared, "oah-task-")

    def test_s3_skill_install_stages_in_scratch(self):
        from oah.toolkit import install

        with hostile_temp(self) as (shared, created):
            done = install.apply_install(install.InstallPlan((), (), {}), catalog=None,
                                         now=dt.datetime.now(dt.timezone.utc))
        self.assertEqual(done, [])
        self.assert_scratch(created, shared, "oah-skills-")

    def test_s4_skill_update_stages_in_scratch(self):
        from oah.toolkit import update

        with hostile_temp(self) as (shared, created):
            results = update.run_update(catalog=None, destinations=[], check_only=True, settings={},
                                        now=dt.datetime.now(dt.timezone.utc), gitleaks=None)
        self.assertEqual(results, [])
        self.assert_scratch(created, shared, "oah-update-")

    def test_s8_isolated_git_home_is_scratch(self):
        from oah.toolkit import fetch

        with hostile_temp(self) as (shared, created):
            with fetch._git_env() as env:
                self.assertTrue(pathlib.Path(env["HOME"]).is_dir())
        self.assert_scratch(created, shared, "oah-git-home-")

    def test_s13_gitleaks_report_is_written_in_scratch(self):
        from oah.engines.proc import Resolved, RunResult
        from oah.toolkit import checks

        folder = make_tmp_dir(self)
        seen: list[tuple[str, ...]] = []

        def runner(argv, timeout):
            seen.append(tuple(argv))
            return RunResult(0, "", "", False)

        with hostile_temp(self) as (shared, created):
            problems = checks.gitleaks_problems(folder, Resolved(("gitleaks",), "test", "native"), runner=runner)
        self.assertEqual(problems, [])
        report = pathlib.Path(seen[0][seen[0].index("--report-path") + 1])
        self.assertEqual(report.parent.parent, scratch_base())
        self.assert_scratch(created, shared, "oah-gitleaks-")


class EvalSites(SiteCase):
    def test_s5_eval_preregistration_run_and_grade_use_scratch(self):
        from oah.eval import grade
        from tests.test_eval import Env, fixture_copy, oracle_path, run_fixture

        root, env = fixture_copy(self), Env(self)  # the variants keep the TMPDIR they had before the test
        with hostile_temp(self) as (shared, created):
            run_dir = run_fixture(self, root, env, frozen=True)
            grade.grade_run(run_dir, [oracle_path(root)], env=env.env)
        self.assert_scratch(created, shared, "oah-eval-prereg-", "oah-eval-src-", "oah-eval-ws-", "oah-eval-grade-")


class EngineSites(SiteCase):
    def test_s6_free_alias_check_runs_in_scratch(self):
        from oah.engines import claude_alias

        env = dict(os.environ)
        with hostile_temp(self) as (shared, created):
            result = claude_alias._run([sys.executable, "-c", "pass"], "haiku", env)
        self.assertIsNotNone(result)
        self.assert_scratch(created, shared, "oah-alias-")

    def test_s7_land_private_objects_live_in_scratch(self):
        from oah import land

        repo = make_git_repo(self)
        env = git_env()
        with hostile_temp(self) as (shared, created):
            context = land._GitContext(repo, env)
            folder = context.source
        self.assert_scratch(created, shared, "oah-land-")
        self.assertEqual(folder.parent, scratch_base())
        del context
        gc.collect()
        self.assertFalse(folder.exists())

    def test_s11_state_capture_copies_through_scratch(self):
        from oah import runs

        path = make_tmp_dir(self) / "record.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        with hostile_temp(self) as (shared, created):
            raw, data = runs._captured_state(path)
        self.assertEqual((raw, data), (b'{"a": 1}', {"a": 1}))
        self.assert_scratch(created, shared, "oah-state-")


class DesignSourceSites(SourceCase, SiteCase):
    def test_s9_ls_remote_runs_in_scratch(self):
        from oah.design.sources import gitref

        self.commit({"src/lib.rs": "public\n"})
        self.tag("v1.0.0")
        with hostile_temp(self) as (shared, created):
            advertisement = gitref.ls_remote(str(self.upstream), tags=True)
        self.assertIn("refs/tags/v1.0.0", advertisement.refs)
        self.assert_scratch(created, shared, "oah-ls-remote-", "oah-git-home-")

    def test_s10_commit_track_fetch_runs_in_scratch(self):
        self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})
        branch = git(self.upstream, "branch", "--show-current").strip()
        sources = self.sources()
        spec = self.register(sources, track="commit", ref=branch)
        with hostile_temp(self) as (shared, created):
            result = sources.check_one(spec)
        self.assertEqual(result.result, "changed")
        self.assert_scratch(created, shared, "oah-source-")

    def test_s10_release_fetch_runs_in_scratch(self):
        self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})
        self.tag("v1.0.0")
        self.github.add("v1.0.0")
        sources = self.sources()
        spec = self.register(sources)
        with hostile_temp(self) as (shared, created):
            result = sources.check_one(spec)
        self.assertEqual(result.result, "changed")
        self.assert_scratch(created, shared, "oah-source-")


class RefusalCase(unittest.TestCase):
    """When the scratch base cannot be proven, each site stops with OAH_SCRATCH_UNSAFE, raised as the error the site
    already uses for its own failures, and unsafe_scratch shows that no folder was made anywhere instead."""

    def assert_refused(self, exc: BaseException, family: type[BaseException]) -> None:
        self.assertIsInstance(exc, family)
        self.assertEqual(getattr(exc, "code", None), "OAH_SCRATCH_UNSAFE", str(exc))


class ReviewRefusals(RefusalCase):
    def test_s1_review_snapshot_refuses_an_unsafe_base_and_an_unsafe_temp_root(self):
        from oah.review import ReviewError, snapshot

        repo = make_git_repo(self)
        base = git_commit(repo, {"README.md": "base\n"})
        head = git_commit(repo, {"src/app.py": "print('hi')\n"})
        with unsafe_scratch(self), self.assertRaises(ReviewError) as caught:
            with snapshot.make(repo, head, base=base):
                pass
        self.assert_refused(caught.exception, ReviewError)
        root = unprovable_folder(self)
        with hostile_temp(self) as (shared, created), self.assertRaises(ReviewError) as caught:
            with snapshot.make(repo, head, base=base, temp_root=root):  # the base is fine; the passed root is not
                pass
        self.assert_refused(caught.exception, ReviewError)
        self.assertEqual((created, sorted(shared.iterdir())), ([], []))
        self.assertFalse(os.path.lexists(root))

    def test_s12_correction_refuses_before_launching(self):
        from oah.review import ReviewError, fix
        from oah.secrets import NoKeychain
        from tests.test_fix import FIXED, FixProject
        from tests.test_review_rounds import RULES

        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        with unsafe_scratch(self), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(ReviewError) as caught:
            fix.correct(fix.Request(task="t1", tier="high"), cwd=project.repo, env=project.env,
                        keychain=NoKeychain(), rules=RULES)
        self.assert_refused(caught.exception, ReviewError)


class ToolkitRefusals(RefusalCase):
    def test_s2_task_scheduler_refuses_before_writing_a_definition(self):
        from oah.toolkit import schedule

        root = make_tmp_dir(self)
        empty = root / "no-tools"
        empty.mkdir()
        entry = schedule.Entry(kind="schtasks", name="oah-test-task", path=None, argv=("oah", "skills", "update"),
                               project_root=root, log_path=root / "log.txt")
        with unsafe_scratch(self), self.assertRaises(schedule.ScheduleError) as caught:
            schedule.install_entry(entry, search_path=str(empty))
        self.assert_refused(caught.exception, schedule.ScheduleError)

    def test_s3_s4_skill_install_and_update_refuse_before_staging(self):
        from oah.toolkit import install, update

        now = dt.datetime.now(dt.timezone.utc)
        attempts = (("install", lambda: install.apply_install(install.InstallPlan((), (), {}), catalog=None, now=now)),
                    ("update", lambda: update.run_update(catalog=None, destinations=[], check_only=True, settings={},
                                                         now=now, gitleaks=None)))
        for name, attempt in attempts:
            with self.subTest(site=name):
                with unsafe_scratch(self), self.assertRaises(install.InstallError) as caught:
                    attempt()
                self.assert_refused(caught.exception, install.InstallError)

    def test_s8_s13_isolated_git_home_and_gitleaks_report_refuse(self):
        from oah.engines.proc import Resolved, RunResult
        from oah.toolkit import checks, fetch

        folder = make_tmp_dir(self)
        seen: list[tuple[str, ...]] = []

        def runner(argv, timeout):
            seen.append(tuple(argv))
            return RunResult(0, "", "", False)

        def git_home():
            with fetch._git_env():
                pass

        attempts = (("git home", git_home),
                    ("gitleaks", lambda: checks.gitleaks_problems(folder, Resolved(("gitleaks",), "test", "native"),
                                                                  runner=runner)))
        for name, attempt in attempts:
            with self.subTest(site=name):
                with unsafe_scratch(self), self.assertRaises(OSError) as caught:
                    attempt()
                self.assert_refused(caught.exception, OSError)
        self.assertEqual(seen, [], "gitleaks never ran")


class EvalRefusals(RefusalCase):
    def test_s5_preregistration_run_and_grade_stop_as_an_environment_problem(self):
        from oah.eval import grade, plan
        from oah.eval.errors import EXIT_ENVIRONMENT, EvalError
        from tests.test_eval import Env, fixture_copy, oracle_path, run_fixture

        root, env = fixture_copy(self), Env(self)
        run_dir = run_fixture(self, root, env)  # a finished run to grade, made while the base is fine
        frozen = root / "prereg.json"
        attempts = (("plan", lambda: plan.preregister(plan.load_plan(root / "plan.toml"), [oracle_path(root)], frozen)),
                    ("runner", lambda: run_fixture(self, root, env)),
                    ("grade", lambda: grade.grade_run(run_dir, [oracle_path(root)], env=env.env)))
        for name, attempt in attempts:
            with self.subTest(site=name):
                with unsafe_scratch(self), self.assertRaises(EvalError) as caught:
                    attempt()
                self.assert_refused(caught.exception, EvalError)
                self.assertEqual(caught.exception.exit_code, EXIT_ENVIRONMENT)
        self.assertFalse(frozen.exists(), "no frozen plan was written")


class EngineRefusals(RefusalCase):
    def test_s6_free_alias_check_raises_instead_of_leaving_the_alias_out(self):
        from oah.engines import claude_alias

        with unsafe_scratch(self), self.assertRaises(OSError) as caught:
            claude_alias._run([sys.executable, "-c", "pass"], "haiku", dict(os.environ))
        self.assert_refused(caught.exception, OSError)

    def test_s7_land_refuses_before_making_private_objects(self):
        from oah import land

        repo = make_git_repo(self)
        env = git_env()
        with unsafe_scratch(self), self.assertRaises(land.LandError) as caught:
            land._GitContext(repo, env)
        self.assert_refused(caught.exception, land.LandError)

    def test_s11_state_capture_refuses(self):
        from oah import runs

        path = make_tmp_dir(self) / "record.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        with unsafe_scratch(self), self.assertRaises(OSError) as caught:
            runs._captured_state(path)
        self.assert_refused(caught.exception, OSError)


class DesignSourceRefusals(SourceCase, RefusalCase):
    def test_s9_ls_remote_refuses(self):
        from oah.design.sources import gitref

        self.commit({"src/lib.rs": "public\n"})
        self.tag("v1.0.0")
        with unsafe_scratch(self), self.assertRaises(OSError) as caught:
            gitref.ls_remote(str(self.upstream), tags=True)
        self.assert_refused(caught.exception, OSError)

    def test_s10_a_source_check_fails_and_falls_back_to_nothing(self):
        # The source check turns every observer error into a failed attempt (design/sources/check.py, outside this
        # lane), so the code itself is not shown here; the check still fails and makes no folder anywhere.
        self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})
        branch = git(self.upstream, "branch", "--show-current").strip()
        sources = self.sources()
        spec = self.register(sources, track="commit", ref=branch)
        with unsafe_scratch(self):
            result = sources.check_one(spec)
        self.assertEqual(result.result, "failed")


if __name__ == "__main__":
    unittest.main()
