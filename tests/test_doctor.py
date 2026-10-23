# oah doctor 검사 — 가짜 해석기·실행기로 설치 상태 판정, 설치 안내, 요약 줄, 종료 코드를 확인한다.
from __future__ import annotations

import io
import json
import unittest

from oah.console import make_safe
from oah.doctor import check_tools, exit_code, python_status, render_json, render_text
from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim
from tests.helpers import make_tmp_dir, make_unlazy

OK_VERSIONS = {
    "git": "git version 2.50.1",
    "uv": "uv 0.12.17",
    "node": "v24.6.0",
    "claude": "2.1.309 (Claude Code)",
    "codex": "codex-cli 0.163.0",
    "graphify": "graphify 0.9.93",
    "gh": "gh version 2.91.0 (2026-04-22)",
    "gitleaks": "gitleaks version 8.30.1",
}


def ok(stdout: str = "", stderr: str = "", code: int = 0) -> RunResult:
    return RunResult(code, stdout, stderr, False)


class FakeTools:
    """A strict stand-in for the resolver and runner: unexpected command lines fail the test."""

    def __init__(self):
        self.resolved = {name: Resolved((f"/fake/bin/{name}",), f"/fake/bin/{name}", "native")
                         for name in OK_VERSIONS}
        self.results = {(f"/fake/bin/{name}", "--version"): ok(version + "\n")
                        for name, version in OK_VERSIONS.items()}
        self.results[("/fake/bin/codex", "login", "status")] = ok("Logged in using ChatGPT\n")
        self.calls = []

    def resolve(self, name, search_path=None):
        value = self.resolved.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    def run(self, argv, *, timeout, **kwargs):
        key = tuple(argv)
        self.calls.append(key)
        if key not in self.results:
            raise AssertionError(f"unexpected command line {key}")
        return self.results[key]


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.home = make_tmp_dir(self) / "home"
        self.unlazy = make_unlazy(self.home / ".agents" / "skills" / "unlazy", version="2.1.0")
        self.tools = FakeTools()

    def check(self, platform="darwin"):
        return check_tools(search_path=None, env={}, home=self.home, repo_root=None, platform=platform,
                           resolver=self.tools.resolve, runner=self.tools.run)

    def by_name(self, statuses):
        return {s.name: s for s in statuses}

    def test_all_present_reports_ok(self):
        statuses = self.check()
        self.assertEqual([s.name for s in statuses],
                         ["git", "python", "uv", "node", "claude", "codex", "unlazy", "graphify", "gh",
                          "gitleaks"])
        self.assertTrue(all(s.status == "ok" for s in statuses), [(s.name, s.status) for s in statuses])
        self.assertEqual(exit_code(statuses), 0)
        self.assertEqual(render_text(statuses, "darwin").splitlines()[-1], "DOCTOR_OK")
        self.assertEqual(self.by_name(statuses)["git"].version, "git version 2.50.1")

    def test_missing_required_tool_exits_one_and_prints_hint(self):
        self.tools.resolved["codex"] = None
        statuses = self.check(platform="win32")
        self.assertEqual(self.by_name(statuses)["codex"].status, "missing")
        self.assertEqual(exit_code(statuses), 1)
        text = render_text(statuses, "win32")
        self.assertIn('powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"', text)
        self.assertIn("https://github.com/openai/codex", text)
        self.assertEqual(text.splitlines()[-1], "DOCTOR_MISSING required=1")

    def test_macos_hint_differs_from_windows_hint(self):
        self.tools.resolved["codex"] = None
        text = render_text(self.check(platform="darwin"), "darwin")
        self.assertIn("brew install --cask codex", text)
        self.assertNotIn("install.ps1", text)

    def test_node_too_old(self):
        self.tools.results[("/fake/bin/node", "--version")] = ok("v14.21.3\n")
        statuses = self.check()
        self.assertEqual(self.by_name(statuses)["node"].status, "too-old")
        self.assertEqual(exit_code(statuses), 1)

    def test_codex_not_signed_in(self):
        self.tools.results[("/fake/bin/codex", "login", "status")] = ok("Not logged in\n", code=1)
        statuses = self.check()
        codex = self.by_name(statuses)["codex"]
        self.assertEqual(codex.status, "not-signed-in")
        self.assertIn("codex login", codex.note)
        self.assertEqual(exit_code(statuses), 1)

    def test_optional_missing_still_ok(self):
        for name in ("graphify", "gh", "gitleaks"):
            self.tools.resolved[name] = None
        statuses = self.check()
        self.assertEqual(exit_code(statuses), 0)
        text = render_text(statuses, "darwin")
        self.assertIn("brew install gitleaks", text)
        self.assertEqual(text.splitlines()[-1], "DOCTOR_OK")

    def test_version_from_stderr_with_ansi(self):
        self.tools.results[("/fake/bin/gh", "--version")] = ok("", "\x1b[32mgh version 2.91.0\x1b[0m\n")
        self.assertEqual(self.by_name(self.check())["gh"].version, "gh version 2.91.0")

    def test_timeout_is_reported_not_fatal(self):
        self.tools.results[("/fake/bin/claude", "--version")] = RunResult(None, "", "", True)
        statuses = self.by_name(self.check())
        self.assertEqual(statuses["claude"].status, "error")
        self.assertIn("timed out", statuses["claude"].note)
        self.assertEqual(statuses["codex"].status, "ok")

    def test_unsupported_shim_is_reported(self):
        self.tools.resolved["codex"] = UnsupportedBatchShim("C:/tools/codex.cmd", "no package")
        statuses = self.check(platform="win32")
        codex = self.by_name(statuses)["codex"]
        self.assertEqual(codex.status, "unsupported-shim")
        self.assertIn("OAH_UNSUPPORTED_BATCH_SHIM", codex.note)
        self.assertEqual(exit_code(statuses), 1)

    def test_npm_shim_tool_reports_version(self):
        self.tools.resolved["codex"] = Resolved(("C:/npm/node.exe", "C:/npm/codex.js"), "C:/npm/codex.cmd",
                                                "npm-shim")
        self.tools.results[("C:/npm/node.exe", "C:/npm/codex.js", "--version")] = ok("codex-cli 0.163.0\n")
        self.tools.results[("C:/npm/node.exe", "C:/npm/codex.js", "login", "status")] = ok("Logged in\n")
        codex = self.by_name(self.check(platform="win32"))["codex"]
        self.assertEqual((codex.status, codex.version, codex.source),
                         ("ok", "codex-cli 0.163.0", "C:/npm/codex.cmd"))

    def test_unlazy_status_from_folder(self):
        unlazy = self.by_name(self.check())["unlazy"]
        self.assertEqual((unlazy.status, unlazy.version), ("ok", "2.1.0"))

    def test_unlazy_missing_prints_install_hint(self):
        empty_home = make_tmp_dir(self) / "empty-home"
        empty_home.mkdir()
        statuses = check_tools(search_path=None, env={}, home=empty_home, repo_root=None, platform="darwin",
                               resolver=self.tools.resolve, runner=self.tools.run)
        self.assertEqual(self.by_name(statuses)["unlazy"].status, "missing")
        self.assertIn("npx skills add Leonxlnx/unlazy", render_text(statuses, "darwin"))
        self.assertEqual(exit_code(statuses), 1)

    def test_python_status(self):
        self.assertEqual(python_status((3, 11, 0)).status, "ok")
        self.assertEqual(python_status((3, 10, 12)).status, "too-old")

    def test_json_shape(self):
        data = json.loads(render_json(self.check(), "darwin"))
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["platform"], "darwin")
        self.assertEqual(set(data["tools"][0]), {"name", "requirement", "status", "version", "source", "note"})

    def test_render_survives_cp949_with_replacement_chars(self):
        self.tools.results[("/fake/bin/gh", "--version")] = ok("gh \ufffd 버전 \U0001f600\n")
        raw = io.BytesIO()
        stream = make_safe(io.TextIOWrapper(raw, encoding="cp949"))
        stream.write(render_text(self.check(), "darwin"))
        stream.flush()
        self.assertIn("\\ufffd", raw.getvalue().decode("cp949"))


if __name__ == "__main__":
    unittest.main()
