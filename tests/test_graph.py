# graph 명령 검사 — 가짜 graphify 로 인자·종료 코드 전달, 설치 안내, 보고서 읽기, Windows 배치 shim 거부를 본다.
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import unittest

from oah import graph, graph_cli
from oah.cli import _command_table, build_parser, command_modules, main
from tests.helpers import make_git_repo, make_tmp_dir, write_posix_tool

FAKE = 'printf "cwd=%s\\n" "$PWD"; for a in "$@"; do printf "arg=%s\\n" "$a"; done; echo "to stderr" >&2; exit ${FAKE_EXIT:-0}'


def path_without_graphify(front: pathlib.Path | None) -> str:
    """The current PATH minus folders holding a real graphify, with an optional folder in front."""
    names = ("graphify", "graphify.exe", "graphify.cmd", "graphify.bat")
    kept = [entry for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and not any((pathlib.Path(entry) / name).exists() for name in names)]
    return os.pathsep.join(([str(front)] if front else []) + kept)


def run_cli(cwd: pathlib.Path, path_dir: pathlib.Path | None, *argv: str, env: dict | None = None):
    out, err = io.StringIO(), io.StringIO()
    saved = {key: os.environ.get(key) for key in ("PATH", "FAKE_EXIT")}
    old = os.getcwd()
    os.chdir(cwd)
    os.environ["PATH"] = path_without_graphify(path_dir)
    os.environ.update(env or {})
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
    finally:
        os.chdir(old)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, out.getvalue(), err.getvalue()


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)

    def test_commands_tuple(self):
        self.assertEqual(graph_cli.COMMANDS, ("graph",))
        self.assertIs(_command_table(command_modules())["graph"], graph_cli)

    def test_missing_graphify_hint(self):
        for argv in (("graph", "update"), ("graph", "query", "what calls x"), ("graph", "report")):
            with self.subTest(argv=argv):
                code, out, err = run_cli(self.repo, None, *argv)
                self.assertEqual(code, 69)
                self.assertTrue(err.startswith("OAH_GRAPH_UNAVAILABLE"), err)
                self.assertIn("uv tool install graphifyy", err)

    @unittest.skipIf(os.name == "nt", "the fake graphify is a POSIX shell script")
    def test_passthrough_arguments_and_exit_code(self):
        tools = make_tmp_dir(self)
        write_posix_tool(tools, "graphify", FAKE)
        sub = self.repo / "src"
        sub.mkdir()
        code, out, err = run_cli(sub, tools, "graph", "query", "who calls a & b; $(x)", "--budget", "50",
                                 env={"FAKE_EXIT": "4"})
        self.assertEqual(code, 4)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"cwd={self.repo}")
        self.assertEqual(lines[1:], ["arg=query", "arg=who calls a & b; $(x)", "arg=--budget", "arg=50"])
        self.assertIn("to stderr", err)
        code, out, _ = run_cli(self.repo, tools, "graph", "path", "A", "B")
        self.assertEqual((code, out.splitlines()[1:]), (0, ["arg=path", "arg=A", "arg=B"]))
        code, out, _ = run_cli(self.repo, tools, "graph", "explain", "Node X")
        self.assertEqual(out.splitlines()[1:], ["arg=explain", "arg=Node X"])

    @unittest.skipIf(os.name == "nt", "the fake graphify is a POSIX shell script")
    def test_update_defaults_to_repo_root(self):
        tools = make_tmp_dir(self)
        write_posix_tool(tools, "graphify", FAKE)
        code, out, _ = run_cli(self.repo, tools, "graph", "update")
        self.assertEqual((code, out.splitlines()[1:]), (0, ["arg=update", "arg=."]))
        code, out, _ = run_cli(self.repo, tools, "graph", "update", "src", "--no-cluster")
        self.assertEqual(out.splitlines()[1:], ["arg=update", "arg=src", "arg=--no-cluster"])

    def test_leading_options_preserve_all_arguments(self):
        extra = ["--no-cluster", "--budget=50", "src with spaces", "--", "한글", "$(x)"]
        for command in ("update", "query", "path", "explain"):
            with self.subTest(command=command):
                args = build_parser().parse_args(["graph", command, *extra])
                self.assertEqual(args.args, extra)

    @unittest.skipIf(os.name == "nt", "the fake graphify is a POSIX shell script")
    def test_leading_options_reach_graphify(self):
        tools = make_tmp_dir(self)
        write_posix_tool(tools, "graphify", FAKE)
        code, out, err = run_cli(self.repo, tools, "graph", "update", "--no-cluster", "src with spaces")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[1:], ["arg=update", "arg=--no-cluster", "arg=src with spaces"])

    def test_report_reads_graph_report(self):
        tools = make_tmp_dir(self)
        if os.name != "nt":
            write_posix_tool(tools, "graphify", FAKE)
            code, out, err = run_cli(self.repo, tools, "graph", "report")
            self.assertEqual(code, 1)
            self.assertIn("oah graph update", err)
        (self.repo / "graphify-out").mkdir()
        (self.repo / "graphify-out" / "GRAPH_REPORT.md").write_text("# Report\n가나다\n", encoding="utf-8")
        code, out, _ = run_cli(self.repo, tools, "graph", "report")
        self.assertEqual((code, out), (0, "# Report\n가나다\n"))

    def test_nothing_runs_without_an_explicit_command(self):
        self.assertEqual(graph.SUBCOMMANDS, ("update", "query", "path", "explain", "report"))
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["graph"])
        self.assertEqual(ctx.exception.code, 2)

    @unittest.skipUnless(os.name == "nt", "Windows batch shims")
    def test_windows_batch_shim_refused(self):
        tools = make_tmp_dir(self)
        (tools / "graphify.cmd").write_text("@echo off\r\necho hi\r\n", encoding="utf-8")
        code, _, err = run_cli(self.repo, tools, "graph", "query", "x")
        self.assertEqual(code, 69)
        self.assertIn("OAH_UNSUPPORTED_BATCH_SHIM", err)


if __name__ == "__main__":
    unittest.main()
