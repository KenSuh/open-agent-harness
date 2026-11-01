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
from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim, run_command
from oah.models import Catalog, Entry, ModelInfo
from oah.paths import repo_info
from oah.setup import catalog, display
from tests.helpers import make_git_repo, make_tmp_dir, make_unlazy

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


class ModelNoticeDoctorTest(unittest.TestCase):
    """oah doctor shows the logged unpooled-model notices that still hold, from local state only."""

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
        self.write_log(self.notice())

    def store(self, *models):
        self.catalog._save(Entry("codex", "1.0.0", "2026-10-27T09:00:00Z", models, "fixture"))

    def write_log(self, *records):
        self.catalog.unpooled_path.write_text("".join(json.dumps(record) + "\n" for record in records),
                                              encoding="utf-8")

    def notice(self, engine="codex", model="m-new", ts="2026-10-27T09:00:00Z"):
        return {"ts": ts, "engine": engine, "model": model}

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
        self.assertIn("not only new ones", out)
        self.assertIn("[engines.codex.tier_pools]", out)
        self.assertIn(".oah/config.toml", out)
        _, raw = self.doctor(json_output=True)
        data = json.loads(raw)
        self.assertEqual(list(data), ["ok", "platform", "tools", "mcp_notices", "model_notices"])
        notice = data["model_notices"][0]
        self.assertEqual((notice["code"], notice["engine"], notice["model"], notice["date"]),
                         ("OAH_MODEL_NEW_UNPOOLED", "codex", "m-new", "2026-10-27"))
        self.assertIn(".oah/config.toml", notice["message"])

    def test_shows_it_with_no_cli_on_path_and_nothing_spawned(self):
        import oah.doctor as doctor

        info = repo_info(self.repo)
        forbidden = AssertionError("a notice must not start a process")
        with mock.patch("oah.paths.repo_info", return_value=info), \
                mock.patch("subprocess.Popen", side_effect=forbidden), \
                mock.patch("subprocess.run", side_effect=forbidden), \
                mock.patch("os.system", side_effect=forbidden), \
                mock.patch.dict(os.environ, {"PATH": ""}):
            found = doctor.model_notices(self.repo, {"OAH_STATE_DIR": str(self.state), "PATH": ""})
        self.assertEqual([(item["engine"], item["model"]) for item in found], [("codex", "m-new")])

    def test_the_last_record_per_engine_and_model_wins(self):
        self.write_log(self.notice(ts="2026-10-20T09:00:00Z"), self.notice(ts="2026-10-27T09:00:00Z"))
        self.assertEqual([item["date"] for item in self.shown()], ["2026-10-27"])

    def test_a_missing_log_or_no_project_shows_nothing(self):
        import oah.doctor as doctor

        self.catalog.unpooled_path.unlink()
        self.assertEqual(self.shown(), [])
        _, text = self.doctor()
        self.assertNotIn("OAH_MODEL_NEW_UNPOOLED", text)
        self.assertEqual(doctor.model_notices(None, {}), [])

    def test_a_corrupt_log_shows_nothing(self):
        for raw in (b"{not json\n", b"\xff\n", b'{"engine": "codex", "model": "m-new"}', b"[" * 200000 + b"\n"):
            with self.subTest(raw=raw[:30]):
                self.catalog.unpooled_path.write_bytes(raw)
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                self.assertNotIn("OAH_MODEL_NEW_UNPOOLED", out)
                self.assertEqual(self.shown(), [])

    def test_a_tampered_record_is_dropped_and_never_echoed(self):
        table = [self.notice(engine="evil-engine \x1b[2J"), self.notice(engine="gemini"), self.notice(engine=[]),
                 self.notice(engine="claude"), self.notice(model="m-evil \x1b[2J"),
                 self.notice(model="m-new\nINJECTED"), self.notice(model=["m-new"]), self.notice(model={})]
        for record in table:
            with self.subTest(record=record):
                self.write_log(record)
                code, out = self.doctor()
                self.assertEqual(code, 0, out)
                for text in ("evil", "gemini", "INJECTED", "\x1b", "OAH_MODEL_NEW_UNPOOLED"):
                    self.assertNotIn(text, out)
                self.assertEqual(self.shown(), [])

    def test_a_name_that_is_not_plain_stays_out_even_when_the_catalog_lists_it(self):
        name = "m-evil\x1b[2J"
        self.store(ModelInfo("m-old", ("high",)), ModelInfo(name, ("high",)))
        self.write_log(self.notice(model=name))
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertNotIn("m-evil", out)
        self.assertEqual(self.shown(), [])

    def test_a_tampered_date_keeps_the_notice_and_drops_the_text(self):
        for ts in ("<date \x1b[2J>", 7, None, "2026-10-27"):
            with self.subTest(ts=ts):
                self.write_log(self.notice(ts=ts))
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

    def test_doctor_leaves_the_config_and_the_log_alone(self):
        before = self.config.read_bytes(), self.catalog.unpooled_path.read_bytes()
        self.doctor()
        self.doctor(json_output=True)
        self.assertEqual((self.config.read_bytes(), self.catalog.unpooled_path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
