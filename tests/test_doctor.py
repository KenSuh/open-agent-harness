# oah doctor 검사 — 가짜 해석기·실행기로 설치 상태 판정, 설치 안내, 요약 줄, 종료 코드를 확인한다.
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from oah.console import make_safe
from oah.doctor import check_tools, exit_code, gates_in_use, project_root, python_status, render_json, render_text
from oah.engines.effort import EngineError
from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim, run_command
from oah.models import Catalog, Entry, ModelInfo
from oah.setup import catalog, display
from tests.helpers import (ToolUpdateTestCase, make_git_repo, make_tmp_dir, make_unlazy, scratch_env,
                           shipped_defaults)


def setUpModule():
    """No doctor test reads the real ~/.oah/updates: the tool-update records live in a temporary home."""
    from oah.toolupdate import records

    home = pathlib.Path(__import__("tempfile").mkdtemp(prefix="oah-doctor-home-")).resolve()
    patcher = mock.patch.object(records, "home", return_value=home)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(shutil.rmtree, home, True)

OK_VERSIONS = {
    "git": "git version 2.50.1",
    "uv": "uv 0.12.17",
    "node": "v24.6.0",
    "claude": "2.1.309 (Claude Code)",
    "codex": "codex-cli 0.163.0",
    "graphify": "graphify 0.9.93",
    "gh": "gh version 2.91.0 (2026-04-22)",
    "gitleaks": "gitleaks version 8.30.1",
    "jq": "jq-1.8.1",
    "rg": "ripgrep 14.1.1",
    "orca": "1.4.237",
    "aside": "1.26.1010.1739",
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
        self.results[("/fake/bin/claude", "auth", "status")] = ok('{"loggedIn": true}\n')
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
                          "gitleaks", "jq", "rg", "orca", "aside"])
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
        self.assertIn("codex: irm https://chatgpt.com/codex/install.ps1 | iex", text)
        self.assertIn("https://github.com/openai/codex", text)
        self.assertEqual(text.splitlines()[-1], "DOCTOR_MISSING required=1")

    def test_macos_hint_differs_from_windows_hint(self):
        self.tools.resolved["codex"] = None
        text = render_text(self.check(platform="darwin"), "darwin")
        self.assertIn("codex: brew install --cask codex   (or: npm install -g @openai/codex)", text)
        self.assertNotIn("install.ps1", text)

    def test_node_too_old(self):
        self.tools.results[("/fake/bin/node", "--version")] = ok("v14.21.3\n")
        statuses = self.check()
        self.assertEqual(self.by_name(statuses)["node"].status, "too-old")
        self.assertIn("node (recommended): brew install node\n", render_text(statuses, "darwin"))
        self.assertEqual(exit_code(statuses), 0)  # node is recommended, not required

    def test_codex_not_signed_in(self):
        self.tools.results[("/fake/bin/codex", "login", "status")] = ok("Not logged in\n", code=1)
        statuses = self.check()
        codex = self.by_name(statuses)["codex"]
        self.assertEqual(codex.status, "not-signed-in")
        self.assertIn("codex login", codex.note)
        self.assertEqual(exit_code(statuses), 1)

    def project(self, *, ledger=False, config=None):
        root = make_tmp_dir(self) / "project"
        (root / ".git").mkdir(parents=True)
        if ledger:
            (root / ".oah" / "tasks" / "demo").mkdir(parents=True)
            (root / ".oah" / "tasks" / "demo" / "GATES.md").write_text("# Gates: demo\n", encoding="utf-8")
        if config is not None:
            (root / ".oah").mkdir(exist_ok=True)
            (root / ".oah" / "config.toml").write_text(config, encoding="utf-8")
        return root

    def check_in(self, root):
        return check_tools(search_path=None, env={}, home=self.home, repo_root=root, platform="darwin",
                           resolver=self.tools.resolve, runner=self.tools.run)

    def test_node_required_when_tasks_have_gate_ledgers(self):
        self.tools.resolved["node"] = None
        statuses = self.check_in(self.project(ledger=True))
        node = self.by_name(statuses)["node"]
        self.assertEqual((node.requirement, node.status), ("required", "missing"))
        self.assertIn("this project uses gates", node.note)
        self.assertEqual(exit_code(statuses), 1)
        self.assertEqual(render_text(statuses, "darwin").splitlines()[-1], "DOCTOR_MISSING required=1")

    def test_node_required_when_config_enables_gates(self):
        self.tools.resolved["node"] = None
        cases = {"[gates]\nenabled = true\n": "required", "[gates]\ncommit_ledgers = true\n": "required",
                 "[gates]\nenabled = false\n": "recommended", "[stages]\ninitial = \"claude\"\n": "recommended",
                 "[gates\nbroken": "recommended"}
        for config, wanted in cases.items():
            with self.subTest(config=config):
                statuses = self.check_in(self.project(config=config))
                self.assertEqual(self.by_name(statuses)["node"].requirement, wanted)
                self.assertEqual(exit_code(statuses), 1 if wanted == "required" else 0)

    def test_node_recommended_without_a_project(self):
        self.tools.resolved["node"] = None
        for root in (None, self.project()):
            with self.subTest(root=root):
                statuses = self.check_in(root)
                self.assertEqual(self.by_name(statuses)["node"].requirement, "recommended")
                self.assertEqual(exit_code(statuses), 0)

    def test_gates_in_use_table(self):
        self.assertFalse(gates_in_use(None))
        self.assertFalse(gates_in_use(self.project()))
        self.assertTrue(gates_in_use(self.project(ledger=True)))
        self.assertTrue(gates_in_use(self.project(config="[gates]\n")))
        self.assertFalse(gates_in_use(self.project(config="[gates]\nenabled = false\n")))

    def test_project_root_walks_up_to_oah_or_git(self):
        empty_path = str(make_tmp_dir(self))  # no git here, so the walk is used
        root = self.project(ledger=True)
        inner = root / "src" / "deep"
        inner.mkdir(parents=True)
        self.assertEqual(project_root(empty_path, cwd=inner), root)
        plain = self.project()
        self.assertEqual(project_root(empty_path, cwd=plain), plain)
        if shutil.which("git"):
            repo = make_git_repo(self)
            (repo / "sub").mkdir()
            self.assertEqual(project_root(None, cwd=repo / "sub").resolve(), repo.resolve())

    def test_claude_not_signed_in(self):
        self.tools.results[("/fake/bin/claude", "auth", "status")] = ok('{"loggedIn": false}\n')
        statuses = self.check()
        claude = self.by_name(statuses)["claude"]
        self.assertEqual(claude.status, "not-signed-in")
        self.assertIn("claude auth login", claude.note)
        self.assertEqual(exit_code(statuses), 1)

    def test_recommended_missing_still_ok(self):
        for name in ("uv", "node", "graphify", "gh", "gitleaks", "jq", "rg"):
            self.tools.resolved[name] = None
        empty_home = make_tmp_dir(self) / "empty-home"
        empty_home.mkdir()
        statuses = check_tools(search_path=None, env={}, home=empty_home, repo_root=None, platform="darwin",
                               resolver=self.tools.resolve, runner=self.tools.run)
        self.assertEqual(exit_code(statuses), 0)
        self.assertEqual({s.name for s in statuses if s.status == "missing"},
                         {"uv", "node", "unlazy", "graphify", "gh", "gitleaks", "jq", "rg"})
        text = render_text(statuses, "darwin")
        for hint in ("brew install gitleaks", "brew install jq", "brew install ripgrep",
                     "Run oah init --with-unlazy after lane P5 lands to install unlazy at P5's "
                     "contract-tested pin, verified by commit and scripts digest.",
                     "curl -LsSf https://astral.sh/uv/install.sh | sh"):
            self.assertIn(hint, text)
        self.assertNotIn("npx skills add", text)
        self.assertEqual(text.splitlines()[-1], "DOCTOR_OK")

    def test_missing_tool_hint_comes_from_catalog(self):
        for name in OK_VERSIONS:
            self.tools.resolved[name] = None
        for platform, os_name in (("darwin", "macos"), ("win32", "windows")):
            text = render_text(self.check(platform=platform), platform)
            for tool in catalog():
                if tool.detect != "path":
                    continue
                with self.subTest(platform=platform, tool=tool.name):
                    if tool.install[os_name]:
                        self.assertIn(display(tool.install[os_name][0]), text)
                    else:  # a link-only entry (Aside on Windows) shows its note instead
                        self.assertIn(tool.note[os_name], text)
                    self.assertIn(tool.url_for(os_name), text)

    def test_json_lists_every_catalog_tool_with_install(self):
        self.tools.resolved["jq"] = None
        data = json.loads(render_json(self.check(platform="win32"), "win32"))
        self.assertEqual([row["name"] for row in data["tools"]], [tool.name for tool in catalog()])
        jq = next(row for row in data["tools"] if row["name"] == "jq")
        self.assertEqual((jq["status"], jq["requirement"], jq["install"], jq["url"]),
                         ("missing", "recommended", ["winget install jqlang.jq"], "https://jqlang.org/download/"))

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

    def test_version_launch_error_is_reported_and_checks_continue(self):
        broken = make_tmp_dir(self) / ("broken.exe" if os.name == "nt" else "broken")
        broken.write_bytes(b"not an executable\n")
        broken.chmod(0o755)
        good = (sys.executable, "-c", "print('tool 24.0.0')")

        def resolver(name, search_path=None):
            argv = (str(broken),) if name == "git" else good
            return Resolved(argv, argv[0], "native")

        statuses = check_tools(search_path=None, env={}, home=self.home, repo_root=None,
                               platform=sys.platform, resolver=resolver)
        found = self.by_name(statuses)
        self.assertEqual(found["git"].status, "error")
        self.assertEqual(found["git"].source, str(broken))
        self.assertIn("--version", found["git"].note)
        self.assertEqual(found["gitleaks"].status, "ok")
        self.assertFalse(json.loads(render_json(statuses, sys.platform))["ok"])

    def test_login_launch_error_is_reported_and_checks_continue(self):
        work = make_tmp_dir(self)
        missing = work / "missing-executable"

        def resolver(name, search_path=None):
            return Resolved((sys.executable,), sys.executable, "native") if name == "codex" else None

        def runner(argv, **kwargs):
            # Both checks use real subprocesses, including the failed login launch.
            command = (str(missing),) if argv[-2:] == ("login", "status") else argv
            return run_command(command, **kwargs)

        statuses = check_tools(search_path=None, env={}, home=self.home, repo_root=None,
                               platform=sys.platform, resolver=resolver, runner=runner)
        found = self.by_name(statuses)
        self.assertEqual(found["codex"].status, "error")
        self.assertTrue(found["codex"].version.startswith("Python "))
        self.assertIn("login status", found["codex"].note)
        self.assertEqual(found["gitleaks"].status, "missing")
        self.assertFalse(json.loads(render_json(statuses, sys.platform))["ok"])

    def test_doctor_json_survives_broken_git(self):
        work = make_tmp_dir(self)
        tool = work / ("git.exe" if os.name == "nt" else "git")
        tool.write_bytes(b"not an executable\n")
        tool.chmod(0o755)
        env = dict(os.environ, PATH=str(work), PYTHONPATH=str(pathlib.Path(__file__).resolve().parents[1] / "src"))
        result = subprocess.run([sys.executable, "-m", "oah", "doctor", "--json"],
                                env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(next(item for item in data["tools"] if item["name"] == "git")["status"], "error")

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
        self.assertIn("Run oah init --with-unlazy after lane P5 lands to install unlazy at P5's "
                      "contract-tested pin, verified by commit and scripts digest.",
                      render_text(statuses, "darwin"))
        self.assertNotIn("npx skills add", render_text(statuses, "darwin"))
        self.assertEqual(exit_code(statuses), 0)  # unlazy is recommended, not required

    def test_python_status(self):
        self.assertEqual(python_status((3, 11, 0)).status, "ok")
        self.assertEqual(python_status((3, 10, 12)).status, "too-old")

    def test_json_shape(self):
        data = json.loads(render_json(self.check(), "darwin"))
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["platform"], "darwin")
        self.assertEqual(set(data["tools"][0]),
                         {"name", "requirement", "status", "version", "source", "note", "install", "url"})

    def test_render_survives_cp949_with_replacement_chars(self):
        self.tools.results[("/fake/bin/gh", "--version")] = ok("gh \ufffd 버전 \U0001f600\n")
        raw = io.BytesIO()
        stream = make_safe(io.TextIOWrapper(raw, encoding="cp949"))
        stream.write(render_text(self.check(), "darwin"))
        stream.flush()
        self.assertIn("\\ufffd", raw.getvalue().decode("cp949"))


class McpNoticeDoctorTest(unittest.TestCase):
    """oah doctor shows the last recorded MCP pin notice per entry, from updates.jsonl, offline."""

    def setUp(self):
        from tests.fixtures.toolkit.registry import forbid_network

        forbid_network(self)
        self.repo = make_git_repo(self)
        self.state = make_tmp_dir(self) / "state"
        self.log = self.state / "toolkit" / "updates.jsonl"
        self.log.parent.mkdir(parents=True)

    def write_log(self, *records):
        self.log.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def notice(self, item="context7", old="4.4.4", new="4.5.0", ts="2026-10-27T09:00:00Z"):
        return {"item": item, "kind": "mcp", "status": "notice", "old_version": old, "new_version": new,
                "reasons": [], "ts": ts, "why": "update"}

    def doctor(self, *, json_output=False):
        import oah.doctor as doctor

        out = io.StringIO()
        statuses = [python_status((3, 12, 0))]
        with mock.patch.object(doctor, "check_tools", return_value=statuses), \
                mock.patch.object(doctor, "project_root", return_value=self.repo), \
                mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(self.state)}), \
                contextlib.redirect_stdout(out):
            code = doctor.doctor_main(json_output=json_output)
        return code, out.getvalue()

    def test_doctor_shows_last_mcp_notice_without_network(self):
        self.write_log(self.notice(new="4.4.9", ts="2026-10-26T09:00:00Z"),
                       {"item": "context7", "kind": "mcp", "status": "unused(offline)", "old_version": "4.4.4",
                        "new_version": "", "reasons": [], "ts": "2026-10-26T10:00:00Z", "why": "automatic"},
                       self.notice(), {"item": "demo", "status": "advanced", "old_commit": "a" * 40,
                                       "new_commit": "b" * 40, "reasons": [], "ts": "2026-10-27T09:00:00Z"})
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("context7", out)
        self.assertIn("4.4.4 -> 4.5.0", out)
        self.assertNotIn("4.4.9", out)
        self.assertIn("MCP config", out)
        code, out = self.doctor(json_output=True)
        notices = json.loads(out)["mcp_notices"]
        self.assertEqual([(n["item"], n["pinned"], n["latest"]) for n in notices], [("context7", "4.4.4", "4.5.0")])

    def test_doctor_hides_notice_once_the_pin_is_bumped(self):
        from oah.toolkit.catalog import load_catalog

        pinned = {entry.name: entry.version for entry in load_catalog().mcp}["context7"]
        self.write_log(self.notice(old="4.4.0", new=pinned))
        code, out = self.doctor(json_output=True)
        self.assertEqual(json.loads(out)["mcp_notices"], [])
        _, text = self.doctor()
        self.assertNotIn("MCP", text)

    def test_doctor_ignores_a_corrupt_or_tampered_log(self):
        for body in ("{not json\n", json.dumps(self.notice(new="9.9.9 \x1b[2J")) + "\n",
                     json.dumps(self.notice(item="unknown-server")) + "\n", "\xff\n"):
            with self.subTest(body=body):
                self.log.write_text(body, encoding="utf-8")
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                self.assertNotIn("9.9.9", out)
                self.assertNotIn("unknown-server", out)
                self.assertNotIn("\x1b", out)

    def test_doctor_without_a_log_shows_no_mcp_block(self):
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertNotIn("MCP", out)

    def test_doctor_skips_non_string_notice_items(self):
        for item in ([], {}):
            with self.subTest(item=item):
                self.write_log(self.notice(item=item))
                code, out = self.doctor(json_output=True)
                self.assertEqual(code, 0, out)
                self.assertEqual(json.loads(out)["mcp_notices"], [])
                self.write_log(self.notice(), self.notice(item=item, new="9.9.9"))
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                self.assertIn("4.4.4 -> 4.5.0", out)
                self.assertNotIn("9.9.9", out)


class ModelChoiceDoctorTest(unittest.TestCase):
    """oah doctor shows what `oah models` shows, from local state alone: no network and no engine CLI."""

    SCORES = {"schema": 1, "source": "Epoch AI", "date": "2026-11-02", "license": "CC BY 4.0", "url": "https://x",
              "credit": "Epoch AI, a credit line.",
              "scores": {"gpt-6-luna": dict.fromkeys(("t1", "t2", "t3"), 0.1),
                         "gpt-6.1-sol": dict.fromkeys(("t1", "t2", "t3"), 1.0),
                         "gpt-6-sol": dict.fromkeys(("t1", "t2", "t3"), 0.9)}}

    def setUp(self):
        from tests.fixtures.toolkit.registry import forbid_network

        forbid_network(self)
        self.repo = make_git_repo(self)
        self.state = make_tmp_dir(self) / "state"
        self.catalog = Catalog(self.state)
        config = self.repo / ".oah" / "config.toml"
        config.parent.mkdir()
        config.write_text('[engines.codex]\ncommand = ["/no/such/codex"]\n[models]\nprofile = "efficient"\n',
                          encoding="utf-8")
        efforts = ("low", "medium", "high", "xhigh", "max")
        self.catalog._save(Entry("codex", "1.0.0", "2026-10-27T09:00:00Z", (
            ModelInfo("gpt-6-luna", efforts), ModelInfo("gpt-6.1-sol", efforts), ModelInfo("gpt-6-sol", efforts),
            ModelInfo("gpt-7-nova", efforts)), "fixture"))
        patcher = mock.patch("oah.scoring.bundled", return_value=self.SCORES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def doctor(self, *, json_output=False):
        import oah.doctor as doctor

        out = io.StringIO()
        with mock.patch.object(doctor, "check_tools", return_value=[python_status((3, 12, 0))]), \
                mock.patch.object(doctor, "project_root", return_value=self.repo), \
                mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(self.state)}), \
                contextlib.redirect_stdout(out):
            code = doctor.doctor_main(json_output=json_output)
        return code, out.getvalue()

    def test_json_shows_the_choice_the_scores_and_each_model_left_out(self):
        code, out = self.doctor(json_output=True)
        self.assertEqual(code, 0, out)
        row = next(item for item in json.loads(out)["model_choices"] if item["engine"] == "codex")
        self.assertEqual(row["profile"], "efficient")
        self.assertEqual((row["scores"]["source"], row["scores"]["date"], row["scores"]["credit"]),
                         ("Epoch AI", "2026-11-02", "Epoch AI, a credit line."))
        self.assertEqual(row["left_out"], {"gpt-6-sol": "superseded", "gpt-7-nova": "unknown-family"})
        self.assertEqual(row["tiers"]["low"], {"model": "gpt-6.1-sol", "source": "scored"}, "luna scores 0 here")
        self.assertEqual(row["tiers"]["max"], {"model": "gpt-6.1-sol", "source": "scored"})

    def test_text_shows_the_same(self):
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        for text in ("no network used", "profile efficient", "scores Epoch AI 2026-11-02", "Epoch AI, a credit line.",
                     "gpt-6-sol (superseded)", "gpt-7-nova (unknown-family)", "low gpt-6.1-sol"):
            self.assertIn(text, out)

    def test_the_engine_clis_are_never_run(self):
        import oah.doctor as doctor

        with mock.patch("oah.models.run_command", side_effect=AssertionError("no engine CLI")), \
                mock.patch("oah.engines.proc.run_command", side_effect=AssertionError("no engine CLI")):
            found = doctor.model_choices(self.repo, {"OAH_STATE_DIR": str(self.state)})
        self.assertEqual([row["engine"] for row in found], ["codex"])

    def test_an_engine_with_no_stored_catalog_is_skipped(self):
        import oah.doctor as doctor

        self.assertEqual(doctor.model_choices(None, {}), [])
        self.assertEqual([row["engine"] for row in doctor.model_choices(self.repo, {"OAH_STATE_DIR": str(self.state)})],
                         ["codex"])

    def test_a_corrupt_catalog_shows_nothing_and_does_not_abort(self):
        self.catalog.catalog_path.write_text("{", encoding="utf-8")
        code, out = self.doctor(json_output=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["model_choices"], [])

    def test_damaged_data_is_shown_by_name(self):
        with mock.patch("oah.scoring.bundled", side_effect=EngineError("OAH_MODEL_NO_SCORES", "damaged; pin one")):
            code, out = self.doctor(json_output=True)
        self.assertEqual(code, 0, out)
        row = json.loads(out)["model_choices"][0]
        self.assertIsNone(row["scores"])
        self.assertEqual(row["tiers"]["high"], {"error": "OAH_MODEL_NO_SCORES: damaged; pin one"})

    def stored(self, name: str, content: object) -> None:
        path = self.state / "models" / name
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))

    def test_a_stored_copy_shows_its_source_date_fetch_time_and_credit(self):
        from tests.test_call import write_stored_epoch

        shipped_defaults(self)
        fetched = __import__("datetime").datetime(2026, 11, 1, 8, 0, tzinfo=__import__("datetime").timezone.utc)
        write_stored_epoch(self.state, {"gpt-6-luna": 0.1, "gpt-6.1-sol": 1.0, "gpt-6-sol": 0.9}, fetched=fetched)
        code, out = self.doctor(json_output=True)
        self.assertEqual(code, 0, out)
        scores = next(item for item in json.loads(out)["model_choices"] if item["engine"] == "codex")["scores"]
        self.assertEqual(list(scores), ["source", "date", "credit", "models", "fetched_at"], "fetched_at comes last")
        self.assertEqual((scores["source"], scores["date"], scores["fetched_at"]),
                         ("Epoch AI", "2026-11-01", "2026-11-01T08:00:00Z"))
        self.assertTrue(scores["credit"].endswith("Accessed 1 Nov 2026."))
        _, text = self.doctor()
        self.assertIn("scores from: Epoch AI, fetched 2026-11-01T08:00:00Z", text)
        self.assertIn("Accessed 1 Nov 2026.", text)

    def test_the_snapshot_says_it_shipped_with_oah(self):
        _, text = self.doctor()
        self.assertIn("scores from: the Epoch AI snapshot shipped with oah (2026-11-02)", text)

    def test_a_corrupt_stored_file_is_reported_and_the_snapshot_scores(self):
        shipped_defaults(self)
        self.stored("benchmarks-epoch.json", b"{")
        code, out = self.doctor(json_output=True)
        self.assertEqual(code, 0, out)
        row = next(item for item in json.loads(out)["model_choices"] if item["engine"] == "codex")
        self.assertEqual(row["scores"]["credit"], "Epoch AI, a credit line.")
        self.assertEqual([notice.split(":")[0] for notice in row["notices"]], ["OAH_SCORES_FILE_INVALID"])
        _, text = self.doctor()
        self.assertIn("OAH_SCORES_FILE_INVALID", text)
        self.assertIn("benchmarks-epoch.json", text)

    def test_artificial_analysis_data_shows_its_credit_and_no_key_is_asked(self):
        from oah import secrets

        shipped_defaults(self)
        # Made-up values: no Artificial Analysis data is in a test.
        self.stored("benchmarks-aa.json", {
            "schema": 1, "source": "Artificial Analysis", "date": "2026-11-01", "url": "https://artificialanalysis.ai/",
            "credit": "Artificial Analysis, https://artificialanalysis.ai/", "min_tasks": 1,
            "scores": {"gpt-6-luna": {"Coding Index": 0.2}, "gpt-6.1-sol": {"Coding Index": 0.7}},
            "fetched_at": "2026-11-01T08:00:00Z", "source_url": "https://artificialanalysis.ai/api/v2/data/llms/models"})
        with mock.patch.object(secrets, "get", side_effect=AssertionError("oah doctor looks up no key")):
            code, text = self.doctor()
        self.assertEqual(code, 0, text)
        self.assertIn("scores from: Artificial Analysis with your key, fetched 2026-11-01T08:00:00Z", text)
        self.assertIn("credit: Artificial Analysis, https://artificialanalysis.ai/", text)

    def test_a_name_that_is_not_plain_is_never_echoed(self):
        self.catalog._save(Entry("codex", "1.0.0", "2026-10-27T09:00:00Z", (
            ModelInfo("gpt-6-luna", ("high",)), ModelInfo("gpt-6\x1b[2J", ("high",))), "fixture"))
        _, out = self.doctor()
        self.assertNotIn("\x1b", out)

    def test_doctor_writes_nothing(self):
        before = {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        self.doctor()
        self.assertEqual({path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}, before)


class ModelNoticeDoctorTest(unittest.TestCase):
    """oah doctor derives unpooled-model notices from the stored catalog and current config."""

    CONFIG = ('[engines.codex]\ncommand = ["/no/such/codex"]\n'
              '[engines.codex.tier_pools]\nhigh = ["m-old"]\n')

    def setUp(self):
        from tests.fixtures.toolkit.registry import forbid_network

        forbid_network(self)
        self.repo = make_git_repo(self)
        self.state = make_tmp_dir(self) / "state"
        self.catalog = Catalog(self.state)
        self.config = self.repo / ".oah" / "config.toml"
        self.config.parent.mkdir()
        self.config.write_text(self.CONFIG, encoding="utf-8")
        self.store(ModelInfo("m-old", ("high",)), ModelInfo("m-new", ("high",)))

    def store(self, *models, checked_at="2026-10-27T09:00:00Z"):
        self.catalog._save(Entry("codex", "1.0.0", checked_at, models, "fixture"))

    def doctor(self, *, json_output=False):
        import oah.doctor as doctor

        out = io.StringIO()
        with mock.patch.object(doctor, "check_tools", return_value=[python_status((3, 12, 0))]), \
                mock.patch.object(doctor, "project_root", return_value=self.repo), \
                mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(self.state)}), \
                contextlib.redirect_stdout(out):
            code = doctor.doctor_main(json_output=json_output)
        return code, out.getvalue()

    def shown(self):
        code, out = self.doctor(json_output=True)
        self.assertEqual(code, 0, out)
        return json.loads(out)["model_notices"]

    def test_shows_the_notice_in_text_and_json_with_the_date(self):
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("OAH_MODEL_NEW_UNPOOLED", out)
        self.assertIn("m-new", out)
        self.assertIn("2026-10-27", out)
        self.assertIn("checked 2026-10-27", out)
        self.assertNotIn("logged", out)
        self.assertIn("not only new ones", out)
        self.assertIn("[engines.codex.tier_pools]", out)
        self.assertIn(".oah/config.toml", out)
        _, raw = self.doctor(json_output=True)
        data = json.loads(raw)
        # This config has no [updates] after_finish, so the off-by-absence notice joins the report.
        self.assertEqual(list(data), ["ok", "platform", "tools", "mcp_notices", "model_notices", "model_choices",
                                      "update_notices", "scratch"])
        self.assertEqual(set(data["scratch"]), {"base", "exists", "count", "bytes", "unreadable"})
        notice = data["model_notices"][0]
        self.assertEqual((notice["code"], notice["engine"], notice["model"], notice["date"]),
                         ("OAH_MODEL_NEW_UNPOOLED", "codex", "m-new", "2026-10-27"))
        self.assertIn(".oah/config.toml", notice["message"])

    def test_git_is_allowed_to_locate_state_but_engine_clis_are_forbidden(self):
        import oah.doctor as doctor

        popen = subprocess.Popen

        def only_git(argv, *args, **kwargs):
            self.assertIn(pathlib.Path(argv[0]).name.lower(), ("git", "git.exe"), argv)
            return popen(argv, *args, **kwargs)

        forbidden = AssertionError("notice lookup must not refresh or run an engine CLI")
        # Git is allowed for the existing repo_info/state_dir lookup; do not mock repo_info.
        with mock.patch("subprocess.Popen", side_effect=only_git) as spawned, \
                mock.patch("os.system", side_effect=forbidden), \
                mock.patch.object(Catalog, "ensure_fresh", side_effect=forbidden):
            found = doctor.model_notices(self.repo, {"OAH_STATE_DIR": str(self.state)})
        self.assertTrue(spawned.called)
        self.assertEqual([(item["engine"], item["model"]) for item in found], [("codex", "m-new")])

    def test_notice_date_tracks_the_stored_catalog_checked_at(self):
        self.store(ModelInfo("m-new", ("high",)), checked_at="2026-10-28T01:00:00+02:00")
        self.assertEqual([item["date"] for item in self.shown()], ["2026-10-27"])
        self.store(ModelInfo("m-new", ("high",)), checked_at="2026-10-29T09:00:00Z")
        self.assertEqual([item["date"] for item in self.shown()], ["2026-10-29"])

    def test_a_missing_catalog_or_no_project_shows_nothing(self):
        import oah.doctor as doctor

        self.catalog.catalog_path.unlink()
        self.assertEqual(self.shown(), [])
        _, text = self.doctor()
        self.assertNotIn("OAH_MODEL_NEW_UNPOOLED", text)
        self.assertEqual(doctor.model_notices(None, {}), [])

    def test_a_corrupt_catalog_shows_nothing(self):
        for raw in (b"{not json\n", b"\xff\n", b"[" * 200000):
            with self.subTest(raw=raw[:30]):
                self.catalog.catalog_path.write_bytes(raw)
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                self.assertNotIn("OAH_MODEL_NEW_UNPOOLED", out)
                self.assertEqual(self.shown(), [])

    def test_a_catalog_name_that_is_not_plain_is_never_echoed(self):
        for name in ("m-evil \x1b[2J", "m-new\nINJECTED", "x" * 129, ["m-new"], {}):
            with self.subTest(name=name):
                self.store(ModelInfo("m-old", ("high",)), ModelInfo(name, ("high",)))
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                for text in ("evil", "INJECTED", "\x1b", "OAH_MODEL_NEW_UNPOOLED"):
                    self.assertNotIn(text, out)
                self.assertEqual(self.shown(), [])

    def test_a_tampered_date_keeps_the_notice_and_drops_the_text(self):
        for ts in ("<date \x1b[2J>", 7, None, "2026-10-27"):
            with self.subTest(ts=ts):
                self.store(ModelInfo("m-new", ("high",)), checked_at=ts)
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                self.assertNotIn("\x1b", out)
                self.assertNotIn("<date", out)
                self.assertEqual([item["date"] for item in self.shown()], [""])

    def test_a_model_since_pooled_gone_or_hidden_shows_nothing(self):
        self.config.write_text(self.CONFIG.replace('["m-old"]', '["m-old", "m-new"]'), encoding="utf-8")
        self.assertEqual(self.shown(), [])
        self.config.write_text(self.CONFIG, encoding="utf-8")
        self.assertEqual(len(self.shown()), 1)
        self.store(ModelInfo("m-old", ("high",)))
        self.assertEqual(self.shown(), [])
        self.store(ModelInfo("m-old", ("high",)), ModelInfo("m-new", ("high",), listed=False))
        self.assertEqual(self.shown(), [])

    def test_pools_removed_shows_nothing(self):
        self.config.write_text('[engines.codex]\ncommand = ["/no/such/codex"]\n', encoding="utf-8")
        self.assertEqual(self.shown(), [])

    def test_empty_configured_pool_reports_every_listed_model(self):
        self.config.write_text(self.CONFIG.replace('["m-old"]', '[]'), encoding="utf-8")
        self.assertEqual([item["model"] for item in self.shown()], ["m-new", "m-old"])

    def test_missing_config_shows_nothing(self):
        self.config.unlink()
        self.assertEqual(self.shown(), [])

    def assert_notice_lookup_failure(self, *, json_output, error):
        code, out = self.doctor(json_output=json_output)
        self.assertEqual(code, 0, out)
        self.assertNotIn("OAH_MODEL_NEW_UNPOOLED", out)
        if json_output:
            data = json.loads(out)
            self.assertEqual(data["model_notices"], [])
            self.assertEqual(data["model_notices_error"], error)
        else:
            self.assertEqual(out.splitlines()[-1], "DOCTOR_OK")
            self.assertIn(f"Model notices could not be read ({error}); run oah models to see why.\n", out)
        return out

    def test_overflowing_model_ttl_does_not_abort_doctor(self):
        self.catalog.catalog_path.unlink()
        self.config.write_text("[models]\nttl_hours = " + "9" * 400 + "\n", encoding="utf-8")
        for json_output in (False, True):
            with self.subTest(json_output=json_output):
                out = self.assert_notice_lookup_failure(json_output=json_output, error="OverflowError")
                self.assertNotIn("9" * 400, out)
                self.assertNotIn("int too large", out)

    def test_unexpected_model_notice_lookup_error_does_not_abort_doctor(self):
        with mock.patch("oah.doctor.model_notices", side_effect=RuntimeError("PRIVATE\n\x1b[2J")):
            for json_output in (False, True):
                with self.subTest(json_output=json_output):
                    out = self.assert_notice_lookup_failure(json_output=json_output, error="RuntimeError")
                    self.assertNotIn("PRIVATE", out)
                    self.assertNotIn("\x1b", out)

    def test_a_bad_catalog_or_config_shows_nothing(self):
        bad = {"not json": "{not json", "bad efforts": json.dumps(
            {"codex": {"models": [{"name": "m-new", "listed": True, "efforts": 5}]}})}
        for name, body in bad.items():
            with self.subTest(name=name):
                self.catalog.catalog_path.write_text(body, encoding="utf-8")
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                self.assertNotIn("OAH_MODEL_NEW_UNPOOLED", out)
        self.catalog.catalog_path.unlink()
        self.store(ModelInfo("m-old", ("high",)), ModelInfo("m-new", ("high",)))
        for body in ('[engines.codex.tier_pools]\nhuge = ["m-old"]\n', "not toml ["):
            with self.subTest(body=body):
                self.config.write_text(body, encoding="utf-8")
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                self.assertNotIn("OAH_MODEL_NEW_UNPOOLED", out)

    def test_doctor_leaves_the_config_and_stored_state_alone(self):
        before = self.config.read_bytes(), {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        self.doctor()
        self.doctor(json_output=True)
        self.assertEqual((self.config.read_bytes(),
                          {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}), before)


if __name__ == "__main__":
    unittest.main()



class ToolUpdateDoctorTest(ToolUpdateTestCase):
    """AC1: doctor names the off-by-absence switch once and the last pass's unsettled results, offline."""

    def setUp(self):
        from oah.toolupdate import records

        super().setUp()
        self.records = records
        self.repo = make_git_repo(self)
        self.config = self.repo / ".oah" / "config.toml"
        self.config.parent.mkdir()

    def doctor(self, *, json_output=False):
        import oah.doctor as doctor

        out = io.StringIO()
        spawned = mock.Mock(side_effect=AssertionError("oah doctor starts no program for tool updates"))
        with mock.patch.object(doctor, "check_tools", return_value=[python_status((3, 12, 0))]), \
                mock.patch.object(doctor, "project_root", return_value=self.repo), \
                mock.patch.object(doctor, "model_notices", return_value=[]), \
                mock.patch.object(doctor, "model_choices", return_value=[]), \
                mock.patch.object(doctor, "agent_notices", return_value=[]), \
                mock.patch.object(doctor, "mcp_notices", return_value=[]), \
                mock.patch("subprocess.Popen", spawned), contextlib.redirect_stdout(out):
            code = doctor.doctor_main(json_output=json_output)
        return code, out.getvalue()

    def test_off_by_absence_is_mentioned_once(self):
        self.config.write_text("[skills]\nauto_update = true\n", encoding="utf-8")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("tool updates are off"), 1)
        self.assertIn("oah update", out)
        self.assertTrue(out.rstrip().endswith("DOCTOR_OK"), out)
        _code, raw = self.doctor(json_output=True)
        self.assertEqual([notice["code"] for notice in json.loads(raw)["update_notices"]], ["OAH_UPDATE_OFF"])

    def test_explicit_switch_and_no_project_config_are_quiet(self):
        self.config.write_text("[updates]\nafter_finish = false\n", encoding="utf-8")
        code, out = self.doctor()
        self.assertNotIn("Tool updates", out)
        self.config.unlink()
        _code, raw = self.doctor(json_output=True)
        self.assertNotIn("update_notices", json.loads(raw))

    def test_unsettled_results_one_line_each(self):
        self.config.write_text("[updates]\nafter_finish = true\n", encoding="utf-8")
        component = self.records.component
        stamp = "2026-11-03T00:00:00Z"
        record = {"version": 1, "last_started": stamp, "trigger": "manual", "last_finished": stamp,
                  "outcome": "needs-attention", "models_refresh": [], "components": {
                      "claude": component("current", "2.1.316", "2.1.316", "", stamp),
                      "codex": component("unverified", "0.166.0", "0.166.0", "run codex update yourself", stamp),
                      "claude-marketplaces": component("current", "abc", "abc", "", stamp),
                      "claude-plugins": component("current", "abc", "abc", "", stamp)}}
        self.records.updates_dir().mkdir(parents=True)
        self.records.state_path().write_text(json.dumps(record), encoding="utf-8")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("  Codex 0.166.0 (unverified); run codex update yourself", out)
        self.assertNotIn("Claude Code 2.1.316 (current)", out)

    def test_unreadable_record_is_named_and_the_report_goes_on(self):
        self.config.write_text("[updates]\nafter_finish = true\n", encoding="utf-8")
        self.records.updates_dir().mkdir(parents=True)
        self.records.state_path().write_text("{torn", encoding="utf-8")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("OAH_UPDATE_STATE_UNAVAILABLE", out)
        self.assertTrue(out.rstrip().endswith("DOCTOR_OK"), out)

class DoctorPinTest(unittest.TestCase):
    def test_a_pin_its_model_cannot_run_is_the_same_error_in_doctor_and_a_call(self):
        import oah.doctor as doctor
        from tests.test_call import Project

        project = Project(self, '[engines.codex.tiers.high]\nmodel = "gpt-test-a"\neffort = "minimal"\n')
        with self.assertRaises(EngineError) as caught:
            project.prepare(stage="correction", tier="high")
        self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
        choices = doctor.model_choices(project.repo, project.env)
        row = next(item for item in choices if item["engine"] == "codex")
        self.assertEqual(row["tiers"]["high"]["error"].split(":")[0], "OAH_CONFIG_INVALID")
        self.assertIn("high OAH_CONFIG_INVALID", doctor.render_text([], "darwin", model_choices=choices))
        self.assertEqual(row["tiers"]["medium"], {"model": "gpt-test-a", "source": "quality_order"}, "only that tier")


class AgentFileDoctorTest(unittest.TestCase):
    """oah doctor names an edited subagent file, and a stale one when no session-start hook will update it."""

    def setUp(self):
        network = mock.patch("socket.create_connection", side_effect=AssertionError("no network"))
        self.network = network.start()
        self.addCleanup(network.stop)
        self.addCleanup(self.network.assert_not_called)
        self.repo = make_git_repo(self)
        self.state = make_tmp_dir(self) / "state"
        Catalog(self.state)._save(Entry("claude", "2.1.316", "2026-11-02T00:00:00Z",
                                        (ModelInfo("sonnet", ("low", "medium", "high", "max")),), "claude --help"))
        self.agents = self.repo / ".claude" / "agents"
        self.agents.mkdir(parents=True)
        self.env = {"OAH_STATE_DIR": str(self.state)}

    def notices(self) -> list[dict]:
        import oah.doctor as doctor

        return doctor.agent_notices(self.repo, self.env)

    def test_an_edited_file_is_named_with_the_manual_step(self):
        from oah import init

        (self.agents / "oah-high.md").write_bytes(init.agent_text("high", "sonnet", "high") + b"- mine\n")
        found = self.notices()
        self.assertEqual([(item["code"], item["file"]) for item in found],
                         [("OAH_AGENT_FILE_EDITED", ".claude/agents/oah-high.md")])
        self.assertIn("left alone", found[0]["message"])
        self.assertIn("oah init --with-agents", found[0]["message"])

    def test_a_stale_generated_file_is_named_only_without_the_hook(self):
        from oah import hooks, init

        (self.agents / "oah-max.md").write_bytes(init.agent_text("max", "opus", "max"))
        found = self.notices()
        self.assertEqual([(item["code"], item["file"]) for item in found],
                         [("OAH_AGENT_FILE_STALE", ".claude/agents/oah-max.md")])
        for text in ("opus", "sonnet", "oah init --with-agents", "oah hooks install session-start"):
            self.assertIn(text, found[0]["message"])
        hooks.install("session-start", self.repo, dry_run=False)
        self.assertEqual(self.notices(), [], "the hook updates it at the next session start")

    def test_current_files_or_none_say_nothing(self):
        import oah.doctor as doctor
        from oah import init

        self.assertEqual(self.notices(), [])
        (self.agents / "oah-max.md").write_bytes(init.agent_text("max", "sonnet", "max"))
        self.assertEqual(self.notices(), [])
        self.assertEqual(doctor.agent_notices(None, {}), [])

    def test_doctor_shows_them_offline_and_writes_nothing(self):
        import oah.doctor as doctor
        from oah import init

        path = self.agents / "oah-high.md"
        path.write_bytes(init.agent_text("high", "sonnet", "high") + b"- mine\n")
        before = path.read_bytes()
        forbidden = AssertionError("doctor stays offline and runs no engine CLI")
        for json_output in (False, True):
            out = io.StringIO()
            with mock.patch.object(doctor, "check_tools", return_value=[python_status((3, 12, 0))]), \
                    mock.patch.object(doctor, "project_root", return_value=self.repo), \
                    mock.patch.dict(os.environ, self.env), \
                    mock.patch("oah.models.run_command", side_effect=forbidden) as run, \
                    mock.patch.object(Catalog, "ensure_fresh", side_effect=forbidden) as fresh, \
                    contextlib.redirect_stdout(out):
                code = doctor.doctor_main(json_output=json_output)
            run.assert_not_called()
            fresh.assert_not_called()
            self.assertEqual(code, 0, out.getvalue())
            if json_output:
                self.assertEqual([item["code"] for item in json.loads(out.getvalue())["agent_notices"]],
                                 ["OAH_AGENT_FILE_EDITED"])
            else:
                self.assertIn("OAH_AGENT_FILE_EDITED: .claude/agents/oah-high.md", out.getvalue())
        self.assertEqual(path.read_bytes(), before)


class ScratchDoctorTest(unittest.TestCase):
    """oah doctor lists what stopped runs left in the private scratch base (count, size and base), offline; it starts
    no program and never creates, proves or deletes anything there."""

    def setUp(self):
        self.repo = make_git_repo(self)
        self.user = make_tmp_dir(self) / "user"
        self.user.mkdir()
        self.env = scratch_env(self.user)
        # the documented base for that variable: %LOCALAPPDATA%, ~/Library/Caches or $XDG_CACHE_HOME, then oah/scratch
        self.base = (self.user / "Library" / "Caches" if sys.platform == "darwin" else self.user) / "oah" / "scratch"

    def doctor(self, *, json_output=False):
        import oah.doctor as doctor

        out = io.StringIO()
        spawned = mock.Mock(side_effect=AssertionError("oah doctor starts no program to count scratch leftovers"))
        with mock.patch.object(doctor, "check_tools", return_value=[python_status((3, 12, 0))]), \
                mock.patch.object(doctor, "project_root", return_value=self.repo), \
                mock.patch.object(doctor, "model_notices", return_value=[]), \
                mock.patch.object(doctor, "model_choices", return_value=[]), \
                mock.patch.object(doctor, "agent_notices", return_value=[]), \
                mock.patch.object(doctor, "mcp_notices", return_value=[]), \
                mock.patch.object(doctor, "update_notices", return_value=[]), \
                mock.patch.object(doctor, "reviewer_pin", return_value=None), \
                mock.patch.object(doctor, "rerank_breaker", return_value=None), \
                mock.patch.dict(os.environ, self.env), \
                mock.patch("subprocess.Popen", spawned), contextlib.redirect_stdout(out):
            code = doctor.doctor_main(json_output=json_output)
        self.assertEqual(spawned.call_count, 0)
        return code, out.getvalue()

    def tree(self):
        return sorted(str(path.relative_to(self.user)) for path in self.user.rglob("*"))

    def test_an_absent_base_is_named_and_not_created(self):
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn(f"\nScratch folder: {self.base} (no leftovers)\n", out)
        self.assertTrue(out.rstrip().endswith("DOCTOR_OK"), out)
        _code, raw = self.doctor(json_output=True)
        self.assertEqual(json.loads(raw)["scratch"],
                         {"base": str(self.base), "exists": False, "count": 0, "bytes": 0, "unreadable": 0})
        self.assertEqual(self.tree(), [], "oah doctor creates nothing")

    def test_leftovers_are_counted_sized_and_kept(self):
        (self.base / "oah-fix-abc" / "deep").mkdir(parents=True)
        (self.base / "oah-fix-abc" / "deep" / "a.bin").write_bytes(b"a" * 1536)
        (self.base / "oah-review-def").mkdir()
        (self.base / "oah-review-def" / "b.bin").write_bytes(b"b" * 512)
        if os.name != "nt":  # a link inside a leftover is neither followed nor counted
            outside = self.user / "outside.bin"
            outside.write_bytes(b"c" * 4096)
            (self.base / "oah-review-def" / "link").symlink_to(outside)
        before = self.tree()
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn(f"\nScratch folder: {self.base}\n  2 leftovers, 2.0 KiB, from runs that stopped before cleaning "
                      "up; oah deletes nothing here, so remove them once no oah command is running\n", out)
        self.assertTrue(out.rstrip().endswith("DOCTOR_OK"), out)
        _code, raw = self.doctor(json_output=True)
        self.assertEqual(json.loads(raw)["scratch"],
                         {"base": str(self.base), "exists": True, "count": 2, "bytes": 2048, "unreadable": 0})
        self.assertEqual(self.tree(), before, "oah doctor deletes nothing")

    def test_a_base_that_cannot_be_worked_out_is_named_and_the_report_goes_on(self):
        self.env = {"LOCALAPPDATA" if sys.platform == "win32" else "HOME": "relative-folder", "XDG_CACHE_HOME": ""}
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn(" cannot be used (OAH_SCRATCH_UNSAFE: ", out)
        self.assertTrue(out.rstrip().endswith("DOCTOR_OK"), out)
        _code, raw = self.doctor(json_output=True)
        self.assertEqual(json.loads(raw)["scratch"]["error"], "OAH_SCRATCH_UNSAFE")
        self.assertEqual(self.tree(), [])

    def test_unreadable_base_is_unknown_in_text(self):
        self.unreadable_report(json_output=False)

    def test_unreadable_base_is_unknown_in_json(self):
        self.unreadable_report(json_output=True)

    def unreadable_report(self, *, json_output):
        retained = self.base / "oah-review-retained" / "keep.txt"
        retained.parent.mkdir(parents=True)
        retained.write_bytes(b"retained test contents\n")
        before = retained.read_bytes()
        recorded_mode = self.base.stat().st_mode
        original = os.scandir

        def listing(path):
            if pathlib.Path(path) == self.base:
                raise PermissionError("test-owned base listing is unreadable")
            return original(path)

        if os.name != "nt":
            self.base.chmod(0)
        try:
            with mock.patch("os.scandir", listing) if os.name == "nt" else contextlib.nullcontext():
                code, out = self.doctor(json_output=json_output)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.base.stat().st_mode, recorded_mode if os.name == "nt" else recorded_mode & ~0o7777)
        finally:
            if os.name != "nt":
                self.base.chmod(recorded_mode)
        self.assertEqual(retained.read_bytes(), before)
        if json_output:
            self.assertEqual(json.loads(out)["scratch"],
                             {"base": str(self.base), "exists": True, "count": 0, "bytes": 0, "unreadable": 1})
        else:
            self.assertIn(f"Scratch folder: {self.base} cannot be fully read", out)
            self.assertIn("0 direct entries", out)
            self.assertIn("0 bytes", out)
            self.assertIn("1 unreadable", out)
            self.assertNotIn("(no leftovers)", out)
