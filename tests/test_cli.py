# oah 명령 진입점 검사 — --version, 도움말, 잘못된 명령, 명령 모듈 찾기, python -m oah, cp949 리다이렉트 출력을 본다.
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from oah import __version__
import oah
from oah.cli import build_parser, command_modules, main
from tests.helpers import make_tmp_dir, make_unlazy

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def run_oah(*args: str, env_extra: dict | None = None, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=SRC)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "oah", *args], capture_output=True, env=env, timeout=60,
                          cwd=str(cwd) if cwd is not None else None)


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
        self.assertEqual(result.stdout.decode("utf-8").strip(), "oah 0.1.0")

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
        # git, claude and codex; Node is only recommended because this checkout uses no gates.
        self.assertEqual(output.splitlines()[-1], "DOCTOR_MISSING required=3")

        as_json = run_oah("doctor", "--json", env_extra=env)
        self.assertEqual(as_json.returncode, 1, as_json.stderr)
        data = json.loads(as_json.stdout.decode("ascii"))
        self.assertEqual(data["ok"], False)

    def doctor_in_project(self, *, ledger: bool) -> tuple[str, dict]:
        work = make_tmp_dir(self)
        project = work / "project"
        (project / ".git").mkdir(parents=True)
        if ledger:
            (project / ".oah" / "tasks" / "demo").mkdir(parents=True)
            (project / ".oah" / "tasks" / "demo" / "GATES.md").write_text("# Gates: demo\n", encoding="utf-8")
        empty_path = work / "empty-path"
        empty_path.mkdir()
        env = {"PATH": str(empty_path), "HOME": str(work), "USERPROFILE": str(work)}
        text = run_oah("doctor", env_extra=env, cwd=project)
        as_json = run_oah("doctor", "--json", env_extra=env, cwd=project)
        node = next(row for row in json.loads(as_json.stdout.decode("ascii"))["tools"] if row["name"] == "node")
        return text.stdout.decode("utf-8").splitlines()[-1], node

    def test_doctor_counts_node_as_required_when_the_project_uses_gates(self):
        last, node = self.doctor_in_project(ledger=True)
        self.assertEqual(last, "DOCTOR_MISSING required=4")
        self.assertEqual(node["requirement"], "required")

    def test_doctor_counts_node_as_recommended_without_gates(self):
        last, node = self.doctor_in_project(ledger=False)
        self.assertEqual(last, "DOCTOR_MISSING required=3")
        self.assertEqual(node["requirement"], "recommended")

    def test_doctor_points_to_the_future_p5_unlazy_pin(self):
        work = make_tmp_dir(self)
        empty_path = work / "empty-path"
        empty_path.mkdir()
        env = {"PATH": str(empty_path), "HOME": str(work), "USERPROFILE": str(work)}
        text = run_oah("doctor", env_extra=env, cwd=work).stdout.decode("utf-8")
        self.assertIn("oah init --with-unlazy after lane P5 lands", text)
        self.assertNotIn("npx skills add", text)
        result = run_oah("doctor", "--json", env_extra=env, cwd=work)
        tool = next(row for row in json.loads(result.stdout)["tools"] if row["name"] == "unlazy")
        self.assertEqual(tool["status"], "missing")
        self.assertIn("contract-tested pin", tool["install"][0])
        self.assertNotIn("npx", " ".join(tool["install"]))


def add_command_module(test: unittest.TestCase, name: str, body: str) -> None:
    """Put a temporary oah.<name> module on the package path for the rest of the test."""
    folder = make_tmp_dir(test)
    (folder / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    patcher = mock.patch.object(oah, "__path__", [*oah.__path__, str(folder)])
    patcher.start()
    test.addCleanup(patcher.stop)
    test.addCleanup(sys.modules.pop, f"oah.{name}", None)


EXTRA = """
    \"\"\"Extra test commands.\"\"\"
    COMMANDS = ("extra",)


    def register(commands):
        extra = commands.add_parser("extra", help="an extra command")
        extra.add_argument("--code", type=int, default=7)


    def run(args):
        return args.code
"""


class DiscoveryTest(unittest.TestCase):
    def test_real_command_modules_are_discovered(self):
        names = [module.__name__ for module in command_modules()]
        self.assertEqual(names, sorted(names))
        self.assertIn("oah.doctor_cli", names)
        self.assertIn("oah.lease_cli", names)
        self.assertTrue(all(name.endswith("_cli") for name in names), names)
        commands = [command for module in command_modules() for command in module.COMMANDS]
        self.assertEqual(len(commands), len(set(commands)), commands)
        self.assertIn("doctor", commands)
        self.assertIn("start", commands)
        build_parser()  # raises RuntimeError when a module registers commands it does not claim

    def test_extra_module_is_discovered_and_dispatched(self):
        add_command_module(self, "zz_extra_cli", EXTRA)
        self.assertEqual(command_modules()[-1].__name__, "oah.zz_extra_cli")
        self.assertEqual(main(["extra"]), 7)
        self.assertEqual(main(["extra", "--code", "3"]), 3)

    def test_non_cli_modules_are_ignored(self):
        add_command_module(self, "zz_helper", "VALUE = 1\n")
        self.assertNotIn("oah.zz_helper", [module.__name__ for module in command_modules()])

    def test_duplicate_command_raises(self):
        add_command_module(self, "zz_clash_cli", EXTRA.replace('"extra"', '"doctor"'))
        with self.assertRaises(RuntimeError) as ctx:
            main(["doctor"])
        self.assertIn("doctor", str(ctx.exception))
        self.assertIn("oah.doctor_cli", str(ctx.exception))
        self.assertIn("oah.zz_clash_cli", str(ctx.exception))

    def test_missing_attribute_raises(self):
        add_command_module(self, "zz_broken_cli", "COMMANDS = (\"broken\",)\n\n\ndef register(commands):\n    pass\n")
        with self.assertRaises(RuntimeError) as ctx:
            main([])
        self.assertIn("oah.zz_broken_cli", str(ctx.exception))
        self.assertIn("run", str(ctx.exception))

    def test_registration_collision_raises(self):
        add_command_module(self, "zz_clash_cli", EXTRA.replace(
            'add_parser("extra",', 'add_parser("doctor",'))
        for build in (build_parser, lambda: main([])):
            with self.subTest(build=build):
                with self.assertRaises(RuntimeError) as ctx:
                    build()
                self.assertIn("oah.zz_clash_cli", str(ctx.exception))
                self.assertIn("doctor", str(ctx.exception))

    def test_registration_alias_collision_raises(self):
        add_command_module(self, "zz_clash_cli", EXTRA.replace(
            'help="an extra command"', 'aliases=["doctor"], help="an extra command"'))
        for build in (build_parser, lambda: main([])):
            with self.subTest(build=build):
                with self.assertRaises(RuntimeError) as ctx:
                    build()
                self.assertIn("oah.zz_clash_cli", str(ctx.exception))
                self.assertIn("doctor", str(ctx.exception))

    def test_unrelated_registration_value_error_propagates(self):
        add_command_module(self, "zz_broken_cli", EXTRA.replace(
            'extra = commands.add_parser("extra", help="an extra command")',
            'raise ValueError("invalid module setting")'))
        for build in (build_parser, lambda: main([])):
            with self.subTest(build=build):
                with self.assertRaisesRegex(ValueError, "^invalid module setting$"):
                    build()

    def test_unclaimed_registered_command_raises(self):
        add_command_module(self, "zz_sneaky_cli", EXTRA.replace(
            'help="an extra command")', 'help="an extra command")\n        commands.add_parser("hidden")'))
        with self.assertRaises(RuntimeError) as ctx:
            build_parser()
        self.assertIn("oah.zz_sneaky_cli", str(ctx.exception))
        self.assertIn("hidden", str(ctx.exception))

    def test_help_groups_commands_by_module(self):
        add_command_module(self, "zz_extra_cli", EXTRA)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([]), 0)
        text = out.getvalue()
        self.assertIn("doctor: doctor\n", text)
        self.assertIn("lease: start, renew, status, finish, release, log\n", text)
        self.assertIn("Extra test commands: extra\n", text)


if __name__ == "__main__":
    unittest.main()
