# 도구 업데이트 명령 검사 — 실제 git 저장소와 가짜 도구만 보이는 환경에서 oah update·status·--after-finish 의 출력과 종료 코드를 본다.
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import unittest
from unittest import mock

from oah import paths, update_cli
from oah.cli import build_parser, main
from oah.toolupdate import records
from tests.helpers import make_git_repo, make_tmp_dir
from tests.test_toolupdate import FakeToolsCase


class UpdateCliTest(FakeToolsCase):
    """`oah update` in a repository; the environment holds only the fake bin folder on PATH."""

    def setUp(self):
        self.git = paths.git_argv()[0]  # resolved before the guard and the cleared environment
        self.repo = make_git_repo(self)
        super().setUp()
        self.switch("[skills]\nauto_update = false\n[updates]\nafter_finish = true\ntimeout_seconds = 30\n")
        self.enterContext(mock.patch.dict(os.environ, self.fake.env, clear=True))

    def switch(self, text: str) -> None:
        config = self.repo / ".oah" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text, encoding="utf-8")

    def allowed(self, argv: list[str]) -> bool:
        return super().allowed(argv) or (bool(argv) and argv[0] == self.git)

    def oah(self, *args: str, cwd: pathlib.Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(cwd or self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def fake_launches(self) -> list[list[str]]:
        return [argv for argv in self.launched if argv[0] != self.git]

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
        self.assertEqual((code, out), (0, "oah: no pass has run yet; run oah update, or set after_finish = true "
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


class RegistrationTest(unittest.TestCase):
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
