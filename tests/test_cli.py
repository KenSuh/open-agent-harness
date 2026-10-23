# oah 명령 진입점 검사 — --version, 도움말, 잘못된 명령, python -m oah, cp949 리다이렉트 출력을 본다.
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest

from oah import __version__
from oah.cli import main
from tests.helpers import make_tmp_dir, make_unlazy

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def run_oah(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=SRC)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "oah", *args], capture_output=True, env=env, timeout=60)


class CliTest(unittest.TestCase):
    def test_version_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue(), f"oah {__version__}\n")

    def test_no_command_prints_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([]), 0)
        self.assertIn("doctor", out.getvalue())

    def test_unknown_command_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            main(["no-such-command"])
        self.assertEqual(ctx.exception.code, 2)

    def test_python_m_oah_version(self):
        result = run_oah("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8").strip(), "oah 0.1.0.dev0")

    def test_cli_under_cp949_io_encoding(self):
        work = make_tmp_dir(self)
        home = work / "홈 \U0001f600"
        make_unlazy(home / ".agents" / "skills" / "unlazy")
        empty_path = work / "empty-path"
        empty_path.mkdir()
        env = {"PYTHONIOENCODING": "cp949", "PATH": str(empty_path), "HOME": str(home), "USERPROFILE": str(home)}

        text = run_oah("doctor", env_extra=env)
        self.assertEqual(text.returncode, 1, text.stderr)
        self.assertNotIn(b"UnicodeEncodeError", text.stderr)
        output = text.stdout.decode("cp949")
        self.assertIn("\\U0001f600", output)
        self.assertEqual(output.splitlines()[-1], "DOCTOR_MISSING required=4")

        as_json = run_oah("doctor", "--json", env_extra=env)
        self.assertEqual(as_json.returncode, 1, as_json.stderr)
        data = json.loads(as_json.stdout.decode("ascii"))
        self.assertEqual(data["ok"], False)


if __name__ == "__main__":
    unittest.main()
