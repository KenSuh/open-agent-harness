# 외부 명령 해석·실행 검사 — npm shim 해석, .cmd 거부, 인자 그대로 전달, 시간 초과 시 프로세스 트리 종료.
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import unittest

from oah.engines.proc import (
    UnsupportedBatchShim,
    classify,
    decode_output,
    resolve_command,
    run_command,
)
from tests.helpers import make_tmp_dir, npm_layout, require_node, write_posix_tool


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ResolveTest(unittest.TestCase):
    def test_resolve_returns_none_when_missing(self):
        empty = make_tmp_dir(self)
        self.assertIsNone(resolve_command("oah-no-such-tool", search_path=str(empty)))

    @unittest.skipIf(os.name == "nt", "POSIX executables")
    def test_posix_executable_runs_directly(self):
        folder = make_tmp_dir(self) / "sp ace 도구"
        write_posix_tool(folder, "echoargs", 'for a in "$@"; do printf "%s\\n" "$a"; done')
        resolved = resolve_command("echoargs", search_path=str(folder))
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.kind, "native")
        result = run_command(resolved.argv + ("a b", "c&d", "é한"), timeout=10)
        self.assertEqual(result.stdout.splitlines(), ["a b", "c&d", "é한"])

    def test_npm_shim_resolves_to_node_and_bin_script(self):
        prefix = make_tmp_dir(self) / "npm"
        shim = npm_layout(prefix, "@openai/codex", {"codex": "bin/codex.js"}, command="codex",
                          script_rel="bin/codex.js", local_node=True)
        resolved = classify(shim, "codex", platform="win32", search_path=None)
        self.assertEqual(resolved.kind, "npm-shim")
        self.assertEqual(resolved.source, str(shim))
        self.assertEqual(resolved.argv, (
            str(prefix / "node.exe"),
            str(prefix / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"),
        ))

    def test_npm_shim_with_string_bin(self):
        prefix = make_tmp_dir(self) / "npm"
        shim = npm_layout(prefix, "lintkit", "cli.js", command="lintkit", script_rel="cli.js",
                          local_node=True)
        resolved = classify(shim, "lintkit", platform="win32", search_path=None)
        self.assertEqual(resolved.argv[1], str(prefix / "node_modules" / "lintkit" / "cli.js"))

    def test_npm_shim_in_project_bin_folder(self):
        project = make_tmp_dir(self) / "project"
        package_dir = project / "node_modules" / "tool"
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(json.dumps({"name": "tool", "bin": {"tool": "run.js"}}),
                                                  encoding="utf-8")
        (package_dir / "run.js").write_text("", encoding="utf-8")
        bin_dir = project / "node_modules" / ".bin"
        bin_dir.mkdir()
        shim = bin_dir / "tool.cmd"
        shim.write_text("@echo off\r\n", encoding="utf-8")
        (bin_dir / "node.exe").write_bytes(b"")
        resolved = classify(shim, "tool", platform="win32", search_path=None)
        self.assertEqual(resolved.argv[1], str(package_dir / "run.js"))

    def test_unrecognized_batch_shim_is_refused(self):
        folder = make_tmp_dir(self)
        shim = folder / "foo.cmd"
        shim.write_text("@echo off\r\n", encoding="utf-8")
        with self.assertRaises(UnsupportedBatchShim) as ctx:
            classify(shim, "foo", platform="win32", search_path=None)
        self.assertEqual(ctx.exception.code, "OAH_UNSUPPORTED_BATCH_SHIM")
        self.assertIn("native", str(ctx.exception))

    def test_ambiguous_npm_shim_is_refused(self):
        prefix = make_tmp_dir(self) / "npm"
        npm_layout(prefix, "codex-a", {"codex": "a.js"}, command="codex", script_rel="a.js", local_node=True)
        shim = npm_layout(prefix, "codex-b", {"codex": "b.js"}, command="codex", script_rel="b.js")
        with self.assertRaises(UnsupportedBatchShim) as ctx:
            classify(shim, "codex", platform="win32", search_path=None)
        self.assertIn("several", str(ctx.exception))

    def test_npm_shim_without_node_is_refused(self):
        prefix = make_tmp_dir(self) / "npm"
        shim = npm_layout(prefix, "tool", {"tool": "run.js"}, command="tool", script_rel="run.js")
        empty = make_tmp_dir(self)
        with self.assertRaises(UnsupportedBatchShim) as ctx:
            classify(shim, "tool", platform="win32", search_path=str(empty))
        self.assertIn("node", str(ctx.exception))

    def test_bin_script_outside_node_modules_is_refused(self):
        prefix = make_tmp_dir(self) / "npm"
        (prefix / "evil.js").parent.mkdir(parents=True, exist_ok=True)
        (prefix / "evil.js").write_text("", encoding="utf-8")
        shim = npm_layout(prefix, "tool", {"tool": "../../evil.js"}, command="tool", script_rel="run.js",
                          local_node=True)
        with self.assertRaises(UnsupportedBatchShim):
            classify(shim, "tool", platform="win32", search_path=None)

    @unittest.skipUnless(os.name == "nt", "Windows batch-shim behaviour")
    def test_windows_npm_shim_keeps_metacharacters_literal(self):
        node = require_node(self)
        work = make_tmp_dir(self)
        prefix = work / "sp ace & ^ % ! ( ) 도구"
        npm_layout(prefix, "argecho", {"argecho": "argecho.js"}, command="argecho", script_rel="argecho.js",
                   script_body="process.stdout.write(JSON.stringify(process.argv.slice(2)));",
                   shim_body='@echo off\r\necho executed> "%~dp0shim-ran.txt"\r\n')
        search_path = os.pathsep.join([str(prefix), str(pathlib.Path(node).parent)])
        resolved = resolve_command("argecho", search_path=search_path)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.kind, "npm-shim")
        args = ["a&b", "c|d", "<e>", "f^g", "%PATH%", "!x!", "(y)", "& echo pwned > marker.txt"]
        result = run_command(resolved.argv + tuple(args), timeout=60, cwd=str(work))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), args)
        self.assertFalse((work / "marker.txt").exists())
        self.assertFalse((prefix / "shim-ran.txt").exists())


class RunTest(unittest.TestCase):
    def test_run_command_refuses_batch_argv(self):
        with self.assertRaises(UnsupportedBatchShim):
            run_command(["C:/tools/tool.CMD", "--version"], timeout=5)

    def test_returncode_and_stderr_are_captured(self):
        result = run_command([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"],
                             timeout=30)
        self.assertEqual((result.returncode, result.stderr, result.timed_out), (3, "bad", False))

    def test_stdin_text_is_passed_as_utf8(self):
        echo = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
        result = run_command([sys.executable, "-c", echo], timeout=30, stdin_text="프롬프트 \U0001f600")
        self.assertEqual(result.stdout, "프롬프트 \U0001f600")

    def test_timeout_kills_process_tree(self):
        work = make_tmp_dir(self)
        pid_file = work / "child.pid"
        spawner = work / "spawner.py"
        spawner.write_text(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        result = run_command([sys.executable, str(spawner), str(pid_file)], timeout=3)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertLess(time.monotonic() - started, 30)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(_pid_alive(child_pid), "the grandchild survived the timeout")


class DecodeTest(unittest.TestCase):
    def test_decode_output_replaces_invalid_bytes(self):
        self.assertEqual(decode_output(b"ok \xff"), "ok \ufffd")

    def test_decode_output_cp949(self):
        self.assertEqual(decode_output("한글".encode("cp949"), "cp949"), "한글")


if __name__ == "__main__":
    unittest.main()
