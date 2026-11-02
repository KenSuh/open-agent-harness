# 도구 업데이트 명령 검사 — 실제 git 저장소와 가짜 도구만 보이는 환경에서 oah update·status·--after-finish 의 출력과 종료 코드를 본다.
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import subprocess
import unittest
from unittest import mock

from oah import paths, update_cli
from oah.cli import build_parser, main
from oah.toolupdate import records
from tests.helpers import ToolUpdateTestCase, guard_subprocesses, isolate_git_home, make_git_repo, make_tmp_dir
from tests.test_toolupdate import FakeToolsCase


class UpdateCliTest(FakeToolsCase):
    """`oah update` in a repository; the environment holds only the fake bin folder on PATH."""

    def setUp(self):
        super().setUp()
        self.repo = make_git_repo(self)
        self.switch("[skills]\nauto_update = false\n[updates]\nafter_finish = true\ntimeout_seconds = 30\n")
        self.enterContext(mock.patch.dict(os.environ, self.fake.env, clear=True))

    def switch(self, text: str) -> None:
        config = self.repo / ".oah" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text, encoding="utf-8")

    def allowed(self, argv: list[str]) -> bool:
        return super().allowed(argv) or (bool(argv) and argv[0] in ("git", self.git))

    def oah(self, *args: str, cwd: pathlib.Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(cwd or self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def fake_launches(self) -> list[list[str]]:
        return [argv for argv in self.launched if argv[0] not in ("git", self.git)]

    def test_update_runs_a_pass_now_and_exits_by_outcome(self):
        code, out, _err = self.oah("update")
        self.assertEqual(code, 0, out)
        self.assertIn("OAH_UPDATE_FINISHED: outcome complete", out)
        self.fake.set("codex", update={"stdout": "maybe\n"})
        code, out, _err = self.oah("update")  # manual passes ignore the interval
        self.assertEqual(code, 1, out)
        self.assertIn("oah: Codex 0.166.0 (unverified); run codex update yourself and read what it says", out)

    def test_update_honours_the_off_switch(self):
        self.switch("[updates]\nafter_finish = false\n")
        code, out, _err = self.oah("update")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("OAH_UPDATE_SKIPPED: tool updates are off"), out)
        self.assertEqual(self.fake_launches(), [])
        self.assertFalse((self.fake.home / ".oah").exists())

    def test_update_outside_a_repository(self):
        outside = make_tmp_dir(self)
        # The test TMPDIR may sit inside a checkout; stop git's search at the temporary folder.
        os.environ["GIT_CEILING_DIRECTORIES"] = str(outside.parent)
        code, _out, err = self.oah("update", cwd=outside)
        self.assertEqual(code, 69)
        self.assertIn("OAH_NOT_A_REPO", err)
        self.assertEqual(self.fake_launches(), [])

    def test_status_runs_no_subprocess(self):
        code, out, _err = self.oah("update", "status")
        self.assertEqual((code, out), (0, "oah: oah's skills keep their own daily check (oah skills update)\n"
                                          "oah: no pass has run yet; run oah update, or set after_finish = true "
                                          "under [updates]\n"))
        self.oah("update")
        launched = len(self.launched)
        code, out, _err = self.oah("update", "status")
        self.assertEqual(code, 0)
        self.assertIn("outcome complete", out)
        self.assertIn("oah: Claude Code 2.1.316 (current)", out)
        self.assertEqual(len(self.launched), launched)

    def test_after_finish_entry_writes_its_log_and_always_exits_0(self):
        code, out, _err = self.oah("update", "--after-finish", "--repo", str(self.repo))
        self.assertEqual(code, 0)
        log = records.log_path().read_text("utf-8")
        self.assertIn("OAH_UPDATE_FINISHED: outcome complete", log)
        self.assertEqual(records.read_state()["trigger"], "after-finish")
        code, _out, _err = self.oah("update", "--after-finish", "--repo", str(self.repo))  # not due
        self.assertEqual(code, 0)
        self.assertEqual(records.log_path().read_text("utf-8"), log)

    def test_after_finish_with_the_switch_off_writes_nothing(self):
        self.switch("[skills]\nauto_update = false\n")
        code, _out, _err = self.oah("update", "--after-finish", "--repo", str(self.repo))
        self.assertEqual(code, 0)
        self.assertFalse((self.fake.home / ".oah").exists())
        self.assertEqual(self.fake_launches(), [])


class RepositorySetupIsolationTest(ToolUpdateTestCase):
    def test_setup_ignores_a_hostile_global_git_hook_and_guards_setup_commands(self):
        from tests.test_lease_cli import FinishStartsToolUpdatesTest
        from tests.test_doctor import ToolUpdateDoctorTest

        folder = make_tmp_dir(self)
        isolated = isolate_git_home(self, folder / "hostile-home")
        git = paths.git_argv()[0]
        guard_subprocesses(self, lambda argv: bool(argv) and argv[0] in ("git", git))
        hooks = folder / "hostile-hooks"
        hooks.mkdir()
        marker = folder / "hook-ran"
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\nprintf ran > '{marker.as_posix()}'\nexit 91\n", encoding="utf-8")
        hook.chmod(0o755)
        hostile = folder / "hostile.gitconfig"
        hostile.write_text(f'[core]\n    hooksPath = "{hooks.as_posix()}"\n', encoding="utf-8")
        for case_type, method in ((UpdateCliTest, "test_status_runs_no_subprocess"),
                                  (FinishStartsToolUpdatesTest, "test_another_active_lease_starts_nothing"),
                                  (ToolUpdateDoctorTest, "test_off_by_absence_is_mentioned_once")):
            with self.subTest(case=case_type.__name__):
                seen = []
                real_popen = subprocess.Popen

                def observe(args, *rest, **options):
                    seen.append((list(args), dict(options.get("env") or os.environ)))
                    return real_popen(args, *rest, **options)

                case = case_type(method)
                with mock.patch.dict(os.environ, dict(isolated, GIT_CONFIG_GLOBAL=str(hostile), GIT_CONFIG_COUNT="0")), \
                        mock.patch("subprocess.Popen", observe):
                    try:
                        case.setUp()
                        self.assertFalse(marker.exists(), "repository setup executed a global Git hook")
                        self.assertTrue(seen)
                        for argv, env in seen:
                            self.assertIn(argv, case.launched, "the guard was not active during setup")
                            self.assertNotEqual(env["GIT_CONFIG_GLOBAL"], str(hostile))
                            self.assertNotEqual(env["HOME"], isolated["HOME"])
                            self.assertEqual(env["HOME"], env["USERPROFILE"])
                            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
                            self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.hooksPath")
                            for key in ("GIT_TEMPLATE_DIR", "GIT_CONFIG_VALUE_0", "XDG_CONFIG_HOME"):
                                self.assertEqual(list(pathlib.Path(env[key]).iterdir()), [])
                            self.assertEqual(pathlib.Path(env["GIT_CONFIG_GLOBAL"]).read_bytes(), b"")
                    finally:
                        case.doCleanups()

    def test_every_update_test_class_installs_isolation_before_fixtures(self):
        import inspect
        import re
        from tests import test_doctor, test_lease_cli, test_toolupdate, test_update_cli

        for module in (test_toolupdate, test_update_cli, test_lease_cli, test_doctor):
            for name, case_type in vars(module).items():
                if not inspect.isclass(case_type) or not issubclass(case_type, unittest.TestCase):
                    continue
                if case_type.__module__ != module.__name__:
                    continue
                methods = unittest.defaultTestLoader.getTestCaseNames(case_type)
                if not methods:
                    continue
                if module in (test_lease_cli, test_doctor) and not re.search(
                        r"toolupdate|update_cli", inspect.getsource(case_type)):
                    continue
                with self.subTest(module=module.__name__, case=name):
                    self.assertTrue(getattr(case_type, "toolupdate_isolation", False),
                                    f"{name} lacks tool-update isolation")
                    case = case_type(methods[0])
                    try:
                        case.setUp()
                        case.assert_update_isolation()
                    finally:
                        case.doCleanups()


class RegistrationTest(ToolUpdateTestCase):
    def test_command_is_registered_and_the_detached_entry_is_hidden(self):
        self.assertEqual(update_cli.COMMANDS, ("update",))
        parser = build_parser()
        update = parser._subparsers._group_actions[0].choices["update"]
        text = " ".join(update.format_help().split())
        self.assertNotIn("--after-finish", text)
        self.assertNotIn("--repo", text)
        self.assertIn("MCP servers are never changed", text)
        args = parser.parse_args(["update", "--after-finish", "--repo", "x"])
        self.assertEqual((args.after_finish, args.repo, args.action), (True, "x", None))
        self.assertEqual(parser.parse_args(["update", "status"]).action, "status")


if __name__ == "__main__":
    unittest.main()
