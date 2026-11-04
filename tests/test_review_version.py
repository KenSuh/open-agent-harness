# 리뷰어 버전 고정 검사 — [review] codex_version 읽기, --version 한 번 실행과 거절 문구, 비었을 때 추가 프로세스 없음, doctor 의 고정값 줄을 확인한다.
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

from oah.config import ConfigError
from oah.engines.proc import run_command
from oah.review import ReviewError, runner, settings
from tests.helpers import make_git_repo, make_tmp_dir
from tests.test_review_rounds import FAKE_REVIEWER, Project

# A stand-in reviewer CLI for `--version` alone: argv[1] picks what it prints.
FAKE_VERSION_CLI = """\
import sys
import time

mode = sys.argv[1]
if mode == "sleep":
    time.sleep(60)
elif mode == "fail":
    print("codex-cli 0.163.0")
    sys.exit(3)
else:
    sys.stdout.write({
        "ok": "codex-cli 0.163.0\\n",
        "padded": "\\n   \\n  codex-cli 0.163.0  \\nsecond line\\n",
        "newer": "codex-cli 0.164.0-alpha.1\\n",
        "hostile": "codex-cli 0.163.0 \\x1b[31m; see the attached page\\n",
        "empty": "",
    }[mode])
"""


def write_config(root: pathlib.Path, text: str) -> None:
    (root / ".oah").mkdir(exist_ok=True)
    (root / ".oah" / "config.toml").write_text(text, encoding="utf-8")


class VersionSettingTest(unittest.TestCase):
    def test_unset_by_default_and_read_stripped(self):
        root = make_tmp_dir(self)
        self.assertIsNone(settings.load(root).codex_version)
        write_config(root, '[review]\ncodex_version = ""\n')
        self.assertIsNone(settings.load(root).codex_version)
        write_config(root, '[review]\ncodex_version = "  codex-cli 0.163.0 "\n')
        self.assertEqual(settings.load(root).codex_version, "codex-cli 0.163.0")

    def test_a_non_string_or_a_second_line_is_refused_by_name(self):
        root = make_tmp_dir(self)
        for text in ("codex_version = 163", "codex_version = true", 'codex_version = "codex-cli 0.163.0\\nx"',
                     'codex_version = "codex-cli\\t0.163.0"'):
            with self.subTest(text=text):
                write_config(root, f"[review]\n{text}\n")
                with self.assertRaises(ConfigError) as caught:
                    settings.load(root)
                self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
                self.assertIn("review.codex_version", str(caught.exception))


class CheckVersionTest(unittest.TestCase):
    def setUp(self):
        self.cli = make_tmp_dir(self) / "fake_version_cli.py"
        self.cli.write_text(FAKE_VERSION_CLI, encoding="utf-8")
        self.env = runner.reviewer_env(os.environ)

    def base(self, mode: str) -> list[str]:
        return [sys.executable, str(self.cli), mode]

    def refused(self, mode: str, pinned: str = "codex-cli 0.163.0") -> str:
        with self.assertRaises(ReviewError) as caught:
            runner.check_version(self.base(mode), pinned, self.env)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_VERSION_MISMATCH")
        message = str(caught.exception)
        self.assertIn("nothing was launched", message)
        self.assertIn("If you updated Codex on purpose", message)
        return message

    def test_the_first_non_empty_line_stripped_must_equal_the_pin(self):
        runner.check_version(self.base("ok"), "codex-cli 0.163.0", self.env)
        runner.check_version(self.base("padded"), "codex-cli 0.163.0", self.env)

    def test_a_different_version_is_named_with_the_value_to_set(self):
        message = self.refused("newer")
        self.assertIn("reports codex-cli 0.164.0-alpha.1", message)
        self.assertIn('set codex_version = "codex-cli 0.164.0-alpha.1"', message)
        self.assertIn("[review] codex_version pins codex-cli 0.163.0", message)

    def test_output_that_is_not_a_plain_version_line_is_never_repeated(self):
        for mode in ("hostile", "empty", "fail"):
            with self.subTest(mode=mode):
                message = self.refused(mode)
                self.assertIn("could not be read", message)
                self.assertIn("set codex_version to the first line of codex --version", message)
                self.assertNotIn("attached page", message)
                self.assertNotIn("\x1b", message)
        message = self.refused("ok", pinned="see the attached page")
        self.assertIn("pins another value", message)
        self.assertNotIn("attached page", message)

    def test_a_missing_program_or_a_hang_counts_as_unreadable(self):
        with self.assertRaises(ReviewError) as caught:
            runner.check_version([str(self.cli.parent / "no-such-codex")], "codex-cli 0.163.0", self.env)
        self.assertIn("could not be read", str(caught.exception))
        with mock.patch.object(runner, "VERSION_TIMEOUT", 1.0):
            self.assertIn("could not be read", self.refused("sleep"))

    def test_one_run_with_version_in_the_given_environment_for_ten_seconds(self):
        with mock.patch.object(runner, "run_command", wraps=run_command) as spy:
            runner.check_version(self.base("ok"), "codex-cli 0.163.0", self.env)
        self.assertEqual(spy.call_count, 1)
        args, kwargs = spy.call_args
        self.assertEqual(args, ([*self.base("ok"), "--version"],))
        self.assertEqual(kwargs, {"timeout": 10.0, "env": self.env})


class VersionRoundTest(unittest.TestCase):
    """`oah review` with the fake reviewer, which prints `codex-cli 0.163.0` for `--version`."""

    def test_a_matching_pin_reviews_after_one_version_run_in_the_reviewer_environment(self):
        project = Project(self, config='[review]\ncodex_version = "codex-cli 0.163.0"\n')
        with mock.patch.object(runner, "run_command", wraps=run_command) as spy:
            self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")
        self.assertEqual(spy.call_count, 1)
        args, kwargs = spy.call_args
        self.assertEqual(args[0][-1], "--version")
        self.assertEqual(os.path.normcase(args[0][1]), os.path.normcase(os.path.abspath(FAKE_REVIEWER)))
        self.assertEqual(kwargs, {"timeout": 10.0, "env": runner.reviewer_env(project.env)})

    def test_unset_runs_no_version_process(self):
        project = Project(self)
        with mock.patch.object(runner, "run_command", wraps=run_command) as spy:
            self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")
        spy.assert_not_called()

    def test_a_different_version_refuses_before_any_round_or_call(self):
        project = Project(self, config='[review]\ncodex_version = "codex-cli 0.162.0"\n')
        error = project.refused("OAH_REVIEW_VERSION_MISMATCH")
        self.assertIn("reports codex-cli 0.163.0, but [review] codex_version pins codex-cli 0.162.0", str(error))
        self.assertIn('set codex_version = "codex-cli 0.163.0"', str(error))
        self.assertEqual(project.round_log(), [])

    def test_the_sha256_pin_is_checked_first(self):
        project = Project(self, config='[review]\ncodex_sha256 = "' + "0" * 64 + '"\ncodex_version = "codex-cli 0.1.0"\n')
        with mock.patch.object(runner, "run_command", wraps=run_command) as spy:
            project.refused("OAH_REVIEW_BINARY_MISMATCH")
        spy.assert_not_called()


class DoctorPinTest(unittest.TestCase):
    """`oah doctor` prints the values to paste, read from disk; it never starts the reviewer CLI."""

    def setUp(self):
        self.repo = make_git_repo(self)
        self.entry = make_tmp_dir(self) / "codex.js"
        self.entry.write_bytes(b"// a reviewer entry file\n")
        write_config(self.repo, f"[engines.codex]\ncommand = {json.dumps([sys.executable, str(self.entry)])}\n")

    def pin(self) -> dict:
        import oah.doctor as doctor

        forbid = AssertionError("doctor must not run the reviewer CLI")
        with mock.patch("oah.models.run_command", side_effect=forbid), \
                mock.patch("oah.engines.proc.run_command", side_effect=forbid), \
                mock.patch.object(runner, "run_command", side_effect=forbid), \
                mock.patch("subprocess.Popen", side_effect=forbid):
            return doctor.reviewer_pin(self.repo)

    def test_the_entry_file_and_its_sha256_come_from_the_file(self):
        found = self.pin()
        self.assertEqual(found, {"path": os.path.realpath(self.entry),
                                 "sha256": hashlib.sha256(self.entry.read_bytes()).hexdigest(), "error": None})

    def test_text_labels_the_values_for_both_settings(self):
        from oah.doctor import python_status, render_text

        found = self.pin()
        statuses = [python_status((3, 12, 0))]
        text = render_text(statuses, "darwin", reviewer_pin=found)
        self.assertIn(f"  codex entry: {found['path']}\n", text)
        self.assertIn(f"  sha256: {found['sha256']} (the value for [review] codex_sha256)\n", text)
        self.assertIn("[review] codex_version takes the first line of `codex --version`", text)
        self.assertEqual(text.splitlines()[-1], "DOCTOR_OK")
        self.assertNotIn("Reviewer pin", render_text(statuses, "darwin"))

    def test_entry_filenames_with_newline_or_escape_are_shown_on_one_safe_line(self):
        from oah.doctor import python_status, render_text

        for character, escaped in (("\n", r"\n"), ("\x1b", r"\x1b")):
            with self.subTest(character=escaped):
                self.entry = self.entry.parent / f"codex{character}forged.js"
                try:
                    self.entry.write_bytes(b"// reviewer\n")
                except (OSError, ValueError):
                    self.skipTest("the file system refuses control characters in file names")
                write_config(self.repo, f"[engines.codex]\ncommand = "
                             f"{json.dumps([sys.executable, str(self.entry)])}\n")
                found = self.pin()
                self.assertIsNone(found["error"])
                text = render_text([python_status((3, 12, 0))], "darwin", reviewer_pin=found)
                self.assertIn(f"  codex entry: {found['path'].replace(character, escaped)}\n", text)
                self.assertNotIn(str(self.entry), text)
                self.assertEqual(len([line for line in text.splitlines() if "codex entry:" in line]), 1)

    def test_entry_display_escapes_all_diagnostic_control_characters(self):
        from oah.doctor import _pin_lines

        controls = "".join(chr(value) for value in (*range(32), *range(127, 160), 0x2028, 0x2029))
        line = _pin_lines({"path": "entry" + controls, "sha256": "a" * 64, "error": None})[2]
        self.assertEqual(line, "  codex entry: entry" + "".join(ascii(char)[1:-1] for char in controls))

    def test_a_missing_entry_is_reported_by_code(self):
        from oah.doctor import python_status, render_text

        self.entry.unlink()
        found = self.pin()
        self.assertEqual(found, {"path": None, "sha256": None, "error": "OAH_ENGINE_MISSING"})
        text = render_text([python_status((3, 12, 0))], "darwin", reviewer_pin=found)
        self.assertIn("could not be read (OAH_ENGINE_MISSING)", text)
        self.assertIn("[review] codex_version takes the first line of `codex --version`", text)

    def doctor(self, *, json_output: bool) -> str:
        import oah.doctor as doctor

        out = io.StringIO()
        with mock.patch.object(doctor, "check_tools", return_value=[doctor.python_status((3, 12, 0))]), \
                mock.patch.object(doctor, "project_root", return_value=self.repo), \
                mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(make_tmp_dir(self))}), \
                mock.patch.object(runner, "run_command", side_effect=AssertionError("no reviewer CLI")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(doctor.doctor_main(json_output=json_output), 0)
        return out.getvalue()

    def test_doctor_prints_the_pin_and_keeps_the_json_shape(self):
        text = self.doctor(json_output=False)
        self.assertIn("Reviewer pin (read from disk; oah doctor never runs the reviewer CLI):", text)
        self.assertIn(hashlib.sha256(self.entry.read_bytes()).hexdigest(), text)
        data = json.loads(self.doctor(json_output=True))
        self.assertNotIn("reviewer_pin", data)
        self.assertNotIn(hashlib.sha256(self.entry.read_bytes()).hexdigest(), json.dumps(data))


if __name__ == "__main__":
    unittest.main()
