# 외부 명령 해석·실행 검사 — npm shim 해석, .cmd 거부, 인자 그대로 전달, 시간 초과 시 프로세스 트리 종료.
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from oah.engines import proc as proc_module
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

        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
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

    def test_windows_lookup_searches_only_absolute_path_entries(self):
        work = make_tmp_dir(self)
        untrusted, first, second = (work / name for name in ("untrusted", "first", "second"))
        for folder in (untrusted, first, second, untrusted / "relative"):
            folder.mkdir(parents=True, exist_ok=True)
        for name in ("git", "codex", "node"):
            (untrusted / (name + ".exe")).write_bytes(b"")
            (untrusted / "relative" / (name + ".exe")).write_bytes(b"")
            (second / (name + ".exe")).write_bytes(b"")
        (untrusted / "codex.cmd").write_bytes(b"")
        (first / "codex.cmd").write_bytes(b"")
        (first / "codex.exe").write_bytes(b"")
        search = ";".join(("", ".", "relative", str(first), str(second)))
        with contextlib.chdir(untrusted), patch.dict(os.environ, PATHEXT=".exe;.cmd;.bat", PATH=search):
            for name in ("git", "node", "codex"):
                with self.subTest(name=name):
                    expected = (first if name == "codex" else second) / (name + ".exe")
                    self.assertEqual(proc_module._find_command(name, search, platform="win32"), str(expected))
                    self.assertEqual(proc_module._find_command(name, None, platform="win32"), str(expected))
                    self.assertIsNone(proc_module._find_command(name, ";.;relative", platform="win32"))
                    self.assertIsNone(proc_module._find_command(name, "", platform="win32"))
            self.assertEqual(proc_module._find_command("node.exe", search, platform="win32"),
                             str(second / "node.exe"))
            (first / "codex.exe").unlink()
            self.assertEqual(proc_module._find_command("codex", search, platform="win32"),
                             str(first / "codex.cmd"))

    def test_windows_lookup_accepts_quoted_absolute_path_entries(self):
        work = make_tmp_dir(self)
        installed = work / "Program Files" / "Tool"
        installed.mkdir(parents=True)
        tool = installed / "git.exe"
        tool.write_bytes(b"")
        with contextlib.chdir(work), patch.dict(os.environ, PATHEXT=".exe"):
            search = ';'.join(('"."', '"Program Files/Tool"', '"' + str(installed) + '"'))
            self.assertEqual(proc_module._find_command("git", search, platform="win32"), str(tool))
            self.assertIsNone(proc_module._find_command("git", '"Program Files/Tool"', platform="win32"))
            self.assertIsNone(proc_module._find_command("git", '"' + str(installed), platform="win32"))

    @unittest.skipUnless(os.name == "nt", "Windows quoted PATH behaviour")
    def test_windows_resolve_quoted_absolute_path_entry(self):
        installed = make_tmp_dir(self) / "Program Files" / "Tool"
        installed.mkdir(parents=True)
        tool = installed / "git.exe"
        tool.write_bytes(b"")
        resolved = resolve_command("git", search_path='"' + str(installed) + '"')
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.source, str(tool))
        self.assertEqual(resolved.argv, (str(tool),))

    def test_windows_lookup_preserves_on_disk_filename_case(self):
        installed = make_tmp_dir(self)
        native, shim = installed / "Git.eXe", installed / "git.CmD"
        native.write_bytes(b"")
        shim.write_bytes(b"")
        (installed / "git.COM").mkdir()  # A directory must not shadow an executable.
        with patch.dict(os.environ, PATHEXT=".COM;.EXE;.CMD"):
            self.assertEqual(proc_module._find_command("git", str(installed), platform="win32"), str(native))
            self.assertEqual(proc_module._find_command("GIT.EXE", str(installed), platform="win32"), str(native))
            self.assertEqual(proc_module._find_command(str(native), str(installed), platform="win32"), str(native))
            native.unlink()
            self.assertEqual(proc_module._find_command("git", str(installed), platform="win32"), str(shim))

    def test_npm_node_lookup_ignores_current_directory(self):
        work = make_tmp_dir(self)
        untrusted = work / "untrusted"
        untrusted.mkdir()
        (untrusted / "node.exe").write_bytes(b"")
        installed = work / "installed"
        installed.mkdir()
        (installed / "node.exe").write_bytes(b"")
        shim = npm_layout(work / "npm", "tool", "run.js", command="tool", script_rel="run.js")
        with contextlib.chdir(untrusted), patch.dict(os.environ, PATHEXT=".exe;.cmd"):
            result = classify(shim, "tool", platform="win32", search_path=str(installed))
            self.assertEqual(result.argv[0], str(installed / "node.exe"))

    @unittest.skipUnless(os.name == "nt", "Windows PATH behaviour")
    def test_windows_current_directory_tools_are_not_resolved(self):
        work = make_tmp_dir(self)
        untrusted, installed = work / "untrusted", work / "installed"
        untrusted.mkdir()
        installed.mkdir()
        for name in ("git", "codex", "node"):
            (untrusted / (name + ".exe")).write_bytes(b"")
            (installed / (name + ".exe")).write_bytes(b"")
        (untrusted / "codex.cmd").write_text("@echo off\r\n", encoding="utf-8")
        # Run the real Python binary from its installed directory, with an untrusted cwd.
        probe = (
            "import json, sys; from oah.engines.proc import resolve_command; "
            "tools = [resolve_command(name, sys.argv[1]) for name in ('git', 'codex', 'node')]; "
            "print(json.dumps([tool.source for tool in tools]))"
        )
        env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parents[1] / "src"))
        # First prove absent PATH tools aren't taken from cwd.
        absent_probe = (
            "from oah.engines.proc import resolve_command; import sys; "
            "assert all(resolve_command(n, sys.argv[1]) is None for n in ('git', 'codex', 'node'))"
        )
        empty = work / "empty"
        empty.mkdir()
        absent = subprocess.run([sys.executable, "-c", absent_probe, str(empty)], cwd=untrusted,
                                env=env, capture_output=True, timeout=30)
        self.assertEqual(absent.returncode, 0, absent.stderr)
        result = subprocess.run([sys.executable, "-c", probe, str(installed)], cwd=untrusted,
                                env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0]),
                         [str(installed / (name + ".exe")) for name in ("git", "codex", "node")])

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

    @unittest.skipUnless(os.name == "nt", "Windows Job Object lifetime")
    def test_timeout_kills_child_after_launcher_exits(self):
        work = make_tmp_dir(self)
        launcher_pid = work / "launcher.pid"
        child_pid = work / "child.pid"
        marker = work / "survived"
        child = (
            "import pathlib, sys, time; time.sleep(8); "
            "pathlib.Path(sys.argv[1]).write_text('alive'); time.sleep(30)"
        )
        launcher = (
            "import os, pathlib, subprocess, sys; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "p = subprocess.Popen([sys.executable, '-c', sys.argv[4], sys.argv[3]]); "
            "pathlib.Path(sys.argv[2]).write_text(str(p.pid)); print('launcher exited', flush=True)"
        )
        started = time.monotonic()
        result = run_command([sys.executable, "-c", launcher, str(launcher_pid), str(child_pid),
                              str(marker), child], timeout=3)
        pid = int(child_pid.read_text(encoding="utf-8"))
        try:
            self.assertTrue(result.timed_out)
            self.assertLess(time.monotonic() - started, 10)
            self.assertFalse(_pid_alive(int(launcher_pid.read_text(encoding="utf-8"))))
            deadline = time.monotonic() + 5
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_pid_alive(pid), "child survived its exited launcher")
            self.assertFalse(marker.exists())
            self.assertIn("launcher exited", result.stdout)
        finally:
            if _pid_alive(pid):
                os.kill(pid, 9)


class DecodeTest(unittest.TestCase):
    def test_decode_output_replaces_invalid_bytes(self):
        self.assertEqual(decode_output(b"ok \xff"), "ok \ufffd")

    def test_decode_output_cp949(self):
        self.assertEqual(decode_output("한글".encode("cp949"), "cp949"), "한글")


if __name__ == "__main__":
    unittest.main()
