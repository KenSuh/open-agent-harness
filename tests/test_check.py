# oah check 검사 — exists/contains/absent/json 의 성공 토큰·실패 메시지·종료 코드와 sh·cmd.exe 를 거친 실행을 본다.
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from oah import check_cli, gates_cli
from oah.gates.ledger import parse_text
from tests.fixtures.gates.support import SRC, build_args
from tests.helpers import make_tmp_dir


def cmd_command(shell, line):
    # cmd /s needs literal outer quotes; list2cmdline's backslash escapes are for the C runtime.
    return f'{subprocess.list2cmdline([shell])} /d /s /c "{line}"'


def run_check(cwd, *argv):
    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_cli.run(build_args(["check", *argv]))
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = make_tmp_dir(self)
        (self.dir / "src").mkdir()
        (self.dir / "src" / "a.py").write_text("print('hello')\n", encoding="utf-8")
        (self.dir / "notes.txt").write_bytes("café line one\r\nline two\r\n".encode("utf-8"))
        (self.dir / "data.json").write_text(json.dumps({"name": "demo", "items": [1, {"ok": True}]}),
                                            encoding="utf-8")

    def assertOk(self, result, kind):
        code, out, err = result
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"OAH_CHECK_OK {kind}"), out)

    def assertFailed(self, result, fragment):
        code, out, err = result
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("OAH_CHECK_FAILED"), err)
        self.assertIn(fragment, err)
        self.assertNotIn("OAH_CHECK_OK", out + err)

    def test_exists(self):
        self.assertOk(run_check(self.dir, "exists", "src/a.py", "src"), "exists")
        self.assertFailed(run_check(self.dir, "exists", "src/a.py", "src/missing.py"), "src/missing.py")

    def test_contains(self):
        self.assertOk(run_check(self.dir, "contains", "src/a.py", "hello"), "contains")
        self.assertFailed(run_check(self.dir, "contains", "src/a.py", "goodbye"), "goodbye")
        self.assertOk(run_check(self.dir, "contains", "src/a.py", r"print\('h\w+'\)", "--regex"), "contains")
        self.assertFailed(run_check(self.dir, "contains", "nope.txt", "x"), "nope.txt")

    def test_contains_reads_utf8_and_crlf(self):
        self.assertOk(run_check(self.dir, "contains", "notes.txt", "café line one"), "contains")
        self.assertOk(run_check(self.dir, "contains", "notes.txt", "^line two$", "--regex"), "contains")

    def test_absent(self):
        self.assertOk(run_check(self.dir, "absent", "src/b.py"), "absent")
        self.assertFailed(run_check(self.dir, "absent", "src/a.py"), "src/a.py")
        self.assertOk(run_check(self.dir, "absent", "src/a.py", "TODO"), "absent")
        self.assertFailed(run_check(self.dir, "absent", "src/a.py", "hello"), "hello")

    def test_absent_text_requires_file(self):
        self.assertFailed(run_check(self.dir, "absent", "src/missing.py", "TODO"), "does not exist")

    def test_json_key_path_and_equals(self):
        self.assertOk(run_check(self.dir, "json", "data.json"), "json")
        self.assertOk(run_check(self.dir, "json", "data.json", "--key", "items.1.ok", "--equals", "true"), "json")
        self.assertOk(run_check(self.dir, "json", "data.json", "--key", "name", "--equals", '"demo"'), "json")
        self.assertFailed(run_check(self.dir, "json", "data.json", "--key", "items.5"), "items.5")
        self.assertFailed(run_check(self.dir, "json", "data.json", "--key", "name", "--equals", '"other"'),
                          "name")
        self.assertFailed(run_check(self.dir, "json", "notes.txt"), "not valid JSON")

    def test_bad_equals_literal_is_usage(self):
        code, _, err = run_check(self.dir, "json", "data.json", "--equals", "{nope")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("OAH_CHECK_USAGE"), err)

    def test_failure_output_never_contains_success_token(self):
        for argv in (("exists", "x"), ("contains", "src/a.py", "OAH_CHECK_OK"), ("absent", "src"),
                     ("json", "src/a.py")):
            with self.subTest(argv=argv):
                code, out, err = run_check(self.dir, *argv)
                self.assertEqual(code, 1)
                self.assertNotIn("OAH_CHECK_OK", out + err)

    def test_runs_through_platform_shell(self):
        line = f'"{sys.executable}" -m oah check contains notes.txt "line two"'
        if os.name == "nt":
            shell = os.environ.get("ComSpec", "cmd.exe")
            argv = cmd_command(shell, line)
        else:
            argv = ["/bin/sh", "-c", line]
        result = subprocess.run(argv, cwd=self.dir, capture_output=True, text=True, encoding="utf-8", timeout=60,
                                env=dict(os.environ, PYTHONPATH=SRC))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OAH_CHECK_OK contains", result.stdout)

    def test_cmd_wrapper_keeps_literal_quote_wrapping(self):
        shell = r"C:\Windows\System32\cmd.exe"
        line = r'"C:\Program Files\Python\python.exe" -m oah check contains notes.txt "line two"'
        self.assertEqual(cmd_command(shell, line), f'{shell} /d /s /c "{line}"')

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
    def test_generated_check_runs_through_cmd(self):
        (self.dir / "README.md").write_text("## Usage\n", encoding="utf-8")
        # Stand in for the installed console entry point, keeping the generated CHECK verbatim.
        shim = self.dir / "oah.cmd"
        shim.write_text(f'@"{sys.executable}" -m oah %*\r\n', encoding="utf-8", newline="")
        line = parse_text(gates_cli.template_text("t1")).gates[0].check
        result = subprocess.run(cmd_command(os.environ.get("ComSpec", "cmd.exe"), line), cwd=self.dir,
                                capture_output=True, text=True, encoding="utf-8", timeout=60,
                                env=dict(os.environ, PYTHONPATH=SRC))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OAH_CHECK_OK contains", result.stdout)


if __name__ == "__main__":
    unittest.main()
