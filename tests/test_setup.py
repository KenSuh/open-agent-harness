# oah.setup 검사 — 도구 목록의 완결성, 설치 계획·동의·실행, 로그인 안내, python -m oah.setup 실행을 확인한다.
from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from oah.engines import proc
from oah.engines.proc import UnsupportedBatchShim
from oah.setup import guide
from oah.setup import tools as cat
from tests.helpers import make_tmp_dir, make_unlazy, npm_layout, require_node

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
OS_NAMES = ("macos", "windows")


class CatalogTest(unittest.TestCase):
    def test_catalog_is_complete(self):
        tools = cat.catalog()
        self.assertEqual(len({t.name for t in tools}), len(tools))
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.purpose)
                if tool.detect == "path":
                    self.assertTrue(tool.check and tool.check[0])
                for os_name in OS_NAMES:
                    self.assertTrue(tool.url_for(os_name).startswith("https://"))
                    for argv in tool.install.get(os_name, ()):
                        self.assertTrue(argv and all(isinstance(part, str) and part for part in argv))
                        if argv[0] not in ("/bin/sh", "powershell"):
                            # Plain argv lists are shown joined by spaces, so no part may contain one.
                            self.assertFalse(any(" " in part for part in argv), argv)
                # Manual pointers have no executable alternatives and explain what to do next.
                for os_name in OS_NAMES:
                    self.assertTrue(tool.install.get(os_name) or tool.note.get(os_name))

    def test_catalog_lists_amendment_tools(self):
        names = [t.name for t in cat.catalog()]
        self.assertEqual(names, ["git", "python", "uv", "node", "claude", "codex", "unlazy", "graphify", "gh",
                                 "gitleaks", "jq", "rg"])

    def test_required_tools(self):
        required = {t.name for t in cat.catalog() if t.required}
        self.assertEqual(required, {"git", "python", "claude", "codex"})

    def test_one_liners_use_explicit_interpreters(self):
        tools = {t.name: t for t in cat.catalog()}
        self.assertEqual(tools["uv"].install["macos"][0],
                         ("/bin/sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"))
        self.assertEqual(tools["uv"].install["windows"][0],
                         ("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                          "irm https://astral.sh/uv/install.ps1 | iex"))
        self.assertEqual(tools["claude"].install["macos"][0],
                         ("/bin/sh", "-c", "curl -fsSL https://claude.ai/install.sh | bash"))
        self.assertEqual(tools["claude"].install["windows"][0][-1], "irm https://claude.ai/install.ps1 | iex")
        self.assertEqual(tools["codex"].install["windows"][0][-1], "irm https://chatgpt.com/codex/install.ps1 | iex")
        self.assertEqual(tools["git"].install["windows"][0], ("winget", "install", "--id", "Git.Git", "-e",
                                                              "--source", "winget"))
        # Shell one-liners are shown exactly as the official page prints them.
        self.assertEqual(cat.display(tools["uv"].install["macos"][0]),
                         "curl -LsSf https://astral.sh/uv/install.sh | sh")
        self.assertEqual(cat.display(tools["gh"].install["macos"][0]), "brew install gh")

    def test_needs_table(self):
        cases = {
            ("brew", "install", "gh"): "brew",
            cat.sh("curl -LsSf https://astral.sh/uv/install.sh | sh"): "curl",
            cat.powershell("irm https://astral.sh/uv/install.ps1 | iex"): "powershell",
            cat.powershell("scoop install gitleaks"): "scoop",
        }
        for argv, wanted in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(cat.needs(argv), wanted)

    def test_node_is_required_only_with_gates(self):
        tools = {t.name: t for t in cat.catalog()}
        self.assertEqual([name for name, t in tools.items() if t.required_for_gates], ["node"])
        self.assertFalse(tools["node"].required)
        self.assertEqual(tools["node"].install["macos"], (("brew", "install", "node"),))

    def test_sign_in_commands(self):
        tools = {t.name: t for t in cat.catalog()}
        self.assertEqual((tools["claude"].sign_in.status_args, tools["claude"].sign_in.login),
                         (("auth", "status"), "claude auth login"))
        self.assertEqual((tools["codex"].sign_in.status_args, tools["codex"].sign_in.login),
                         (("login", "status"), "codex login"))
        self.assertEqual([t.name for t in tools.values() if t.sign_in], ["claude", "codex"])

    def test_unlazy_has_only_a_manual_pointer_to_the_p5_pin(self):
        tool = next(t for t in cat.catalog() if t.name == "unlazy")
        for os_name in OS_NAMES:
            with self.subTest(os_name=os_name):
                self.assertEqual(tool.install[os_name], ())
                self.assertIn("oah init --with-unlazy", tool.note[os_name])
                self.assertIn("P5", tool.note[os_name])
                self.assertIn("contract-tested pin", tool.note[os_name])
                self.assertIn("after", tool.note[os_name])

    def test_os_name_for(self):
        self.assertEqual(cat.os_name_for("darwin"), "macos")
        self.assertEqual(cat.os_name_for("win32"), "windows")
        self.assertEqual(cat.os_name_for("linux"), "macos")



def fake_which(*present: str):
    table = {name: f"/fake/bin/{name}" for name in present}
    return lambda name, *args, **kwargs: table.get(name)


def by_name(items, attr="tool"):
    return {getattr(item, attr).name: item for item in items}


class Answers:
    """Stand-in for input(): hands out answers in order and returns None (EOF) when they run out."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


class CheckAndPlanTest(unittest.TestCase):
    def setUp(self):
        self.home = make_tmp_dir(self) / "home"
        self.home.mkdir()

    def check(self, which, **kwargs):
        return guide.check_tools(cat.catalog(), which=which, env={}, home=self.home, **kwargs)

    def test_check_tools_uses_which(self):
        statuses = by_name(self.check(fake_which("git", "claude")))
        self.assertEqual((statuses["git"].present, statuses["git"].path), (True, "/fake/bin/git"))
        self.assertFalse(statuses["codex"].present)
        self.assertIsNone(statuses["codex"].path)

    def test_check_tools_python_and_unlazy(self):
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        statuses = by_name(self.check(fake_which(), version_info=(3, 10, 4)))
        self.assertFalse(statuses["python"].present)
        self.assertTrue(statuses["unlazy"].present)
        statuses = by_name(self.check(fake_which(), version_info=(3, 11, 0)))
        self.assertTrue(statuses["python"].present)

    def test_unlazy_detection_does_not_repeat_the_old_unpinned_install_hint(self):
        status = by_name(self.check(fake_which()))["unlazy"]
        self.assertFalse(status.present)
        self.assertIn("not found", status.reason)
        self.assertNotIn("npx", status.reason)

    def test_plan_picks_first_available_alternative(self):
        statuses = self.check(fake_which())
        steps = by_name(guide.plan_installs(statuses, os_name="macos", which=fake_which("brew", "xcode-select")))
        self.assertEqual(steps["git"].argv, ("brew", "install", "git"))
        steps = by_name(guide.plan_installs(statuses, os_name="macos", which=fake_which("xcode-select")))
        self.assertEqual(steps["git"].argv, ("xcode-select", "--install"))
        self.assertIn("install window", steps["git"].note)

    def test_plan_falls_back_to_manual_url(self):
        steps = by_name(guide.plan_installs(self.check(fake_which()), os_name="macos", which=fake_which()))
        node = steps["node"]
        self.assertIsNone(node.argv)
        self.assertTrue(node.manual)
        self.assertEqual(node.url, "https://nodejs.org/en/download")
        self.assertEqual(node.display, "brew install node")
        self.assertIn("brew", node.note)

    def test_plan_counts_programs_installed_earlier(self):
        statuses = self.check(fake_which())
        steps = by_name(guide.plan_installs(statuses, os_name="windows", which=fake_which("powershell", "winget")))
        self.assertEqual(steps["uv"].argv[-1], "irm https://astral.sh/uv/install.ps1 | iex")
        self.assertEqual(steps["graphify"].argv, ("uv", "tool", "install", "graphifyy"))
        self.assertTrue(steps["unlazy"].manual)
        self.assertIn("official install script from astral.sh", steps["uv"].note)
        self.assertTrue(steps["gitleaks"].manual)  # scoop is not installed and nothing installs it
        names = [step.tool.name for step in guide.plan_installs(statuses, os_name="windows",
                                                                 which=fake_which("powershell", "winget"))]
        self.assertLess(names.index("uv"), names.index("graphify"))
        self.assertLess(names.index("node"), names.index("unlazy"))

    def test_plan_skips_present_tools(self):
        everything = fake_which(*(t.check[0] for t in cat.catalog() if t.check))
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        self.assertEqual(guide.plan_installs(self.check(everything), os_name="macos", which=everything), [])


class RunInstallsTest(unittest.TestCase):
    def test_uv_install_subprocesses_disable_profile_changes(self):
        work = make_tmp_dir(self)
        probe = work / "installer.py"
        output = work / "env.txt"
        probe.write_text("import os, pathlib\npathlib.Path(os.environ['OAH_ENV_PROBE']).write_text("
                         "os.environ.get('UV_NO_MODIFY_PATH', ''), encoding='utf-8')\n", encoding="utf-8")
        uv = next(t for t in cat.catalog() if t.name == "uv")
        resolved = proc.Resolved((sys.executable, str(probe)), "test installer", "native")
        for os_name in OS_NAMES:
            with self.subTest(os_name=os_name), mock.patch.dict(
                    os.environ, OAH_ENV_PROBE=str(output), UV_NO_MODIFY_PATH="0"), mock.patch.object(
                    guide, "resolve_command", return_value=resolved):
                self.assertEqual(guide.run_interactive(uv.install[os_name][0]), 0)
                self.assertEqual(output.read_text(encoding="utf-8"), "1")
                self.assertEqual(os.environ["UV_NO_MODIFY_PATH"], "0")

    def steps(self):
        statuses = guide.check_tools(cat.catalog(), which=fake_which("python3"), env={},
                                     home=make_tmp_dir(self))
        return [s for s in guide.plan_installs(statuses, os_name="macos", which=fake_which("brew", "curl"))
                if s.tool.name in ("gh", "jq", "rg")]

    def test_run_installs_skips_declined_steps(self):
        calls = []
        results = guide.run_installs(self.steps(), confirm=lambda step: step.tool.name == "jq",
                                     runner=lambda argv: calls.append(argv) or 0, refresh=lambda: None,
                                     out=lambda text: None)
        self.assertEqual(calls, [("brew", "install", "jq")])
        self.assertEqual({r.step.tool.name: r.status for r in results},
                         {"gh": "skipped", "jq": "installed", "rg": "skipped"})

    def test_run_installs_reports_failure_and_continues(self):
        lines = []

        def runner(argv):
            if argv == ("brew", "install", "gh"):
                return 3
            if argv == ("brew", "install", "jq"):
                raise FileNotFoundError("brew vanished")
            return 0

        results = guide.run_installs(self.steps(), confirm=lambda step: True, runner=runner, refresh=lambda: None,
                                     out=lines.append)
        self.assertEqual([r.status for r in results], ["failed", "failed", "installed"])
        text = "\n".join(lines)
        self.assertIn("exited with code 3", text)
        self.assertIn("brew vanished", text)
        self.assertIn("Running: brew install ripgrep", text)

    def test_run_installs_skips_manual_steps(self):
        statuses = guide.check_tools(cat.catalog(), which=fake_which(), env={}, home=make_tmp_dir(self))
        steps = [s for s in guide.plan_installs(statuses, os_name="macos", which=fake_which()) if s.tool.name == "node"]
        lines = []
        results = guide.run_installs(steps, confirm=lambda step: self.fail("manual steps are never asked"),
                                     runner=lambda argv: self.fail("manual steps never run"), refresh=lambda: None,
                                     out=lines.append)
        self.assertEqual(results[0].status, "manual")
        self.assertIn("https://nodejs.org/en/download", "\n".join(lines))

    def test_run_installs_refreshes_path_after_each_install(self):
        events = []
        guide.run_installs(self.steps(), confirm=lambda step: True,
                           runner=lambda argv: events.append(argv[-1]) or (1 if argv[-1] == "gh" else 0),
                           refresh=lambda: events.append("refresh"), out=lambda text: None)
        self.assertEqual(events, ["gh", "jq", "refresh", "ripgrep", "refresh"])

    def test_default_runner_passes_argv_literally(self):
        out = make_tmp_dir(self) / "argv.json"
        code = ("import json, sys; open(sys.argv[1], 'w', encoding='utf-8')"
                ".write(json.dumps(sys.argv[2:]))")
        tricky = ["a & b", "|", "<x>", "^%PATH%!", "$(echo hi)", "'quoted'", '"dq"']
        self.assertEqual(guide.run_interactive((sys.executable, "-c", code, str(out), *tricky)), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), tricky)

    def test_default_runner_refuses_batch_files(self):
        shim = make_tmp_dir(self) / "tool.cmd"
        shim.write_text("@echo off\r\n", encoding="utf-8")
        shim.chmod(0o755)
        with self.assertRaises(UnsupportedBatchShim):
            guide.run_interactive((str(shim), "install"))

    def test_default_runner_reports_missing_program(self):
        with self.assertRaises(FileNotFoundError):
            guide.run_interactive(("oah-no-such-program-here", "install"))


class RefreshPathTest(unittest.TestCase):
    def test_refresh_path_windows_discovers_uv_without_persistent_path_changes(self):
        home = make_tmp_dir(self)
        for relative in (".local/bin", ".cargo/bin"):
            with self.subTest(relative=relative):
                folder = home / relative
                folder.mkdir(parents=True)
                uv = folder / "uv.exe"
                uv.write_bytes(b"")
                env = {"PATH": r"C:\session"}
                registry = (r"C:\Windows", r"C:\tools")
                guide.refresh_path(env, platform="win32", home=home, read_registry=lambda: registry)
                guide.refresh_path(env, platform="win32", home=home, read_registry=lambda: registry)
                self.assertEqual(env["PATH"].split(";"), [*registry, r"C:\session", str(folder)])
                self.assertEqual(registry, (r"C:\Windows", r"C:\tools"))
                uv.unlink()

    def test_refresh_path_merges_registry_values(self):
        env = {"PATH": r"C:\py;C:\Windows", "SystemRoot": r"C:\Windows", "USERPROFILE": r"C:\home\someone"}
        guide.refresh_path(env, platform="win32",
                           read_registry=lambda: (r"%SystemRoot%;%SystemRoot%\System32", r"%USERPROFILE%\.local\bin;"))
        self.assertEqual(env["PATH"], r"C:\Windows;C:\Windows\System32;C:\home\someone\.local\bin;C:\py")

    @unittest.skipUnless(os.name == "posix", "POSIX PATH and local-bin layout")
    def test_refresh_path_adds_local_bin_on_posix(self):
        home = make_tmp_dir(self)
        env = {"PATH": "/usr/bin:/bin"}
        guide.refresh_path(env, platform="darwin", home=home)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")  # no ~/.local/bin yet
        (home / ".local" / "bin").mkdir(parents=True)
        guide.refresh_path(env, platform="darwin", home=home)
        guide.refresh_path(env, platform="darwin", home=home)
        self.assertEqual(env["PATH"], "/usr/bin:/bin:" + str(home / ".local" / "bin"))

    def test_refresh_path_windows_uses_registry_instead_of_posix_local_bin(self):
        home = make_tmp_dir(self)
        (home / ".local" / "bin").mkdir(parents=True)
        env = {"PATH": r"C:\session"}
        guide.refresh_path(env, platform="win32", home=home,
                           read_registry=lambda: (r"C:\Windows", r"C:\tools"))
        self.assertEqual(env["PATH"], r"C:\Windows;C:\tools;C:\session")

    @unittest.skipUnless(os.name == "nt", "reads the Windows registry")
    def test_refresh_path_reads_real_registry(self):
        env = {"PATH": "", "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        guide.refresh_path(env, platform="win32")
        self.assertIn("system32", env["PATH"].lower())
        self.assertNotIn("%", env["PATH"].split(";")[0])


class SignInTest(unittest.TestCase):
    def setUp(self):
        self.tools = {t.name: t for t in cat.catalog()}
        self.secret = "someone" + "@" + "example.invalid"

    def test_sign_in_uses_status_command_exit_code(self):
        calls = []

        def status(argv):
            calls.append(argv)
            return (0, "Logged in using ChatGPT\n") if argv[-2:] == ("login", "status") else (1, "")

        self.assertTrue(guide.check_sign_in(self.tools["codex"], "/x/codex", status_runner=status))
        self.assertFalse(guide.check_sign_in(self.tools["claude"], "/x/claude", status_runner=status))
        self.assertEqual(calls, [("codex", "login", "status"), ("claude", "auth", "status")])
        self.assertFalse(guide.check_sign_in(self.tools["codex"], "/x/codex", status_runner=lambda argv: (None, "")))

    def test_npm_sign_in_resolves_the_catalog_name_and_runs_the_declared_bin(self):
        prefix = make_tmp_dir(self) / "npm with spaces"
        shim = npm_layout(prefix, "@openai/codex", {"codex": "bin/status.py"}, command="codex",
                          script_rel="bin/status.py", local_node=True,
                          script_body="import sys\nassert sys.argv[1:] == ['login', 'status']\n"
                                      "print('signed in')\n")
        calls = []

        def resolve(name):
            return proc.classify(shim, name, platform="win32", search_path=None)

        def run(argv, **kwargs):
            calls.append(argv)
            # Python stands in for node, but the real manifest, resolver and child process run.
            return proc.run_command((sys.executable,) + argv[1:], **kwargs)

        with mock.patch.object(guide, "resolve_command", side_effect=resolve), mock.patch.object(
                guide, "run_command", side_effect=run):
            self.assertTrue(guide.check_sign_in(self.tools["codex"], str(shim)))
        self.assertEqual(calls, [(str(prefix / "node.exe"),
                                 str(prefix / "node_modules" / "@openai" / "codex" / "bin" / "status.py"),
                                 "login", "status")])

    @unittest.skipUnless(os.name == "nt", "native Windows npm discovery and execution")
    def test_windows_npm_sign_in_uses_node_without_launching_the_shim(self):
        node = require_node(self)
        prefix = make_tmp_dir(self) / "npm space & 도구"
        shim = npm_layout(prefix, "@openai/codex", {"codex": "bin/status.js"}, command="codex",
                          script_rel="bin/status.js",
                          script_body="process.exit(process.argv.slice(2).join(' ') === 'login status' ? 0 : 1);",
                          shim_body='@echo off\r\necho ran> "%~dp0shim-ran.txt"\r\n')
        # Discovery may preserve PATHEXT's uppercase suffix even for a lowercase filename.
        shim.rename(shim.with_suffix(".CMD"))
        with mock.patch.dict(os.environ, PATH=os.pathsep.join((str(prefix), str(pathlib.Path(node).parent)))):
            status = guide.check_tools([self.tools["codex"]])[0]
            self.assertEqual(os.path.normcase(status.path), os.path.normcase(str(shim)))
            self.assertTrue(guide.check_sign_in(status.tool, status.path))
        self.assertFalse((prefix / "shim-ran.txt").exists())

    def test_sign_in_reads_logged_in_flag_only(self):
        out = json.dumps({"loggedIn": False, "email": self.secret})
        self.assertFalse(guide.check_sign_in(self.tools["claude"], "c", status_runner=lambda argv: (0, out)))
        out = json.dumps({"loggedIn": True, "email": self.secret})
        self.assertTrue(guide.check_sign_in(self.tools["claude"], "c", status_runner=lambda argv: (0, out)))

    def statuses(self, *present):
        return guide.check_tools(cat.catalog(), which=fake_which(*present), env={}, home=make_tmp_dir(self))

    def test_sign_in_never_prints_status_output(self):
        lines = []
        out = json.dumps({"loggedIn": True, "email": self.secret})
        guide.walk_sign_ins(self.statuses("claude", "codex"), status_runner=lambda argv: (0, out),
                            interactive=False, ask=Answers(), out=lines.append)
        guide.walk_sign_ins(self.statuses("claude", "codex"), status_runner=lambda argv: (1, self.secret),
                            interactive=False, ask=Answers(), out=lines.append)
        self.assertNotIn(self.secret, "\n".join(lines))

    def test_walkthrough_names_command_and_rechecks(self):
        state = {"codex": [1, 1, 0], "claude": [0]}

        def status(argv):
            name = pathlib.PurePosixPath(argv[0]).name
            return state[name].pop(0), ""

        lines, ask = [], Answers("", "")
        pending = guide.walk_sign_ins(self.statuses("claude", "codex"), status_runner=status, interactive=True,
                                      ask=ask, out=lines.append)
        self.assertEqual(pending, [])
        text = "\n".join(lines)
        self.assertIn("claude: signed in", text)
        self.assertIn("codex login", text)
        self.assertIn("new terminal", text)
        self.assertEqual(len(ask.prompts), 2)
        self.assertEqual(state["codex"], [])

    def test_walkthrough_skip_and_yes_do_not_block(self):
        never = lambda argv: (1, "")  # noqa: E731
        ask = Answers("s")
        pending = guide.walk_sign_ins(self.statuses("codex"), status_runner=never, interactive=True, ask=ask,
                                      out=lambda text: None)
        self.assertEqual((pending, len(ask.prompts)), (["codex"], 1))
        ask = Answers()
        pending = guide.walk_sign_ins(self.statuses("codex"), status_runner=never, interactive=True, ask=ask,
                                      out=lambda text: None)
        self.assertEqual(pending, ["codex"])  # end of input counts as skip
        ask = Answers("")
        pending = guide.walk_sign_ins(self.statuses("codex", "claude"), status_runner=never, interactive=False,
                                      ask=ask, out=lambda text: None)
        self.assertEqual((pending, ask.prompts), (["claude", "codex"], []))


class GuidedSetupTest(unittest.TestCase):
    def setUp(self):
        self.home = make_tmp_dir(self) / "home"
        self.home.mkdir()
        self.lines: list[str] = []
        self.calls: list[tuple] = []

    def setup(self, which, **kwargs):
        options = dict(os_name="macos", yes=False, dry_run=False, which=which, ask=Answers(), out=self.lines.append,
                       runner=lambda argv: self.calls.append(argv) or 0, status_runner=lambda argv: (0, ""),
                       refresh=lambda: None, env={}, home=self.home)
        options.update(kwargs)
        return guide.guided_setup(**options)

    def test_dry_run_never_calls_runner(self):
        code = self.setup(fake_which("brew", "curl"), dry_run=True, yes=True,
                          ask=lambda prompt: self.fail("a dry run never asks"))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        text = "\n".join(self.lines)
        self.assertIn("curl -fsSL https://claude.ai/install.sh | bash", text)
        self.assertEqual(self.lines[-1], "SETUP_DRY_RUN planned=11")

    def test_guided_setup_asks_per_tool_and_shows_exact_text(self):
        which = fake_which("git", "brew", "curl", "claude", "codex", "python3")
        ask = Answers("y", "n")
        code = self.setup(which, ask=ask)
        self.assertEqual(self.calls, [("/bin/sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh")])
        text = "\n".join(self.lines)
        self.assertIn("This runs: curl -LsSf https://astral.sh/uv/install.sh | sh", text)
        self.assertIn("This runs: brew install node", text)
        self.assertTrue(all(prompt.strip().endswith("[y/N]") for prompt in ask.prompts[:2]))
        # Required tools are present and signed in; only recommended ones were skipped.
        self.assertEqual((code, self.lines[-1]), (0, "SETUP_OK"))

    def test_guided_setup_yes_runs_without_asking(self):
        which = fake_which("brew", "curl")
        code = self.setup(which, yes=True, ask=lambda prompt: self.fail("--yes never asks"))
        self.assertIn(("brew", "install", "git"), self.calls)
        self.assertIn(("/bin/sh", "-c", "curl -fsSL https://claude.ai/install.sh | bash"), self.calls)
        # The fake runner installs nothing, so the re-check still finds the required tools missing.
        self.assertEqual(code, 1)
        self.assertEqual(self.lines[-1], "SETUP_INCOMPLETE missing=git,claude,codex")

    def test_windows_setup_refreshes_new_uv_before_confirmed_graphify_install(self):
        tools = [tool for tool in cat.catalog() if tool.name in ("uv", "graphify")]
        env = {"PATH": str(self.home / "empty-bin")}
        uv = self.home / ".local" / "bin" / "uv.exe"
        calls = []

        def which(name):
            if name == "powershell":
                return "powershell"
            if name == "uv" and str(uv.parent) in env["PATH"].split(";") and uv.is_file():
                return str(uv)
            return None

        def runner(argv):
            calls.append(argv)
            if argv == tools[0].install["windows"][0]:
                uv.parent.mkdir(parents=True)
                uv.write_bytes(b"")
                return 0
            self.assertEqual(argv, ("uv", "tool", "install", "graphifyy"))
            # A child process must inherit the directory even though the registry never changed.
            result = subprocess.run([sys.executable, "-c",
                                     "import os, pathlib; "
                                     "assert any((pathlib.Path(p) / 'uv.exe').is_file() "
                                     "for p in os.environ['PATH'].split(';'))"],
                                    env=dict(os.environ, PATH=env["PATH"]), capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return 0

        code = self.setup(which, os_name="windows", tools=tools, yes=True, runner=runner, env=env,
                          refresh=lambda: guide.refresh_path(env, platform="win32", home=self.home,
                                                             read_registry=lambda: ("", "")))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(which("uv"), str(uv))

    def test_windows_setup_discovers_existing_uv_before_planning(self):
        uv = self.home / ".local" / "bin" / "uv.exe"
        uv.parent.mkdir(parents=True)
        uv.write_bytes(b"")
        env = {"PATH": ""}

        def which(name):
            if name == "powershell":
                return "powershell"
            if name == "uv" and str(uv.parent) in env["PATH"].split(";"):
                return str(uv)
            return None

        code = self.setup(which, os_name="windows", yes=True,
                          tools=[tool for tool in cat.catalog() if tool.name in ("uv", "graphify")],
                          refresh=lambda: guide.refresh_path(env, platform="win32", home=self.home,
                                                             read_registry=lambda: ("", "")))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [("uv", "tool", "install", "graphifyy")])

    def test_unlazy_manual_pointer_is_shown_without_running_an_installer(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                self.lines.clear()
                self.calls.clear()
                code = self.setup(fake_which("npx"), tools=[next(t for t in cat.catalog() if t.name == "unlazy")],
                                  yes=True, dry_run=dry_run)
                self.assertEqual(code, 0)
                self.assertEqual(self.calls, [])
                text = "\n".join(self.lines)
                self.assertIn("oah init --with-unlazy", text)
                self.assertIn("contract-tested pin", text)
                self.assertNotIn("npx skills add", text)

    def test_guided_setup_reports_required_tool_not_signed_in(self):
        which = fake_which(*(t.check[0] for t in cat.catalog() if t.check))
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        code = self.setup(which, yes=True, status_runner=lambda argv: (1, ""))
        self.assertEqual((code, self.lines[-1]), (1, "SETUP_INCOMPLETE missing=claude:sign-in,codex:sign-in"))
        self.assertEqual(self.calls, [])

    def test_module_main_dry_run_for_windows_plan(self):
        work = make_tmp_dir(self)
        env = {key: value for key, value in os.environ.items() if key.upper() not in ("PATH", "PYTHONPATH")}
        env.update(PATH=str(work / "empty-bin"), PYTHONPATH=str(SRC), HOME=str(work), USERPROFILE=str(work),
                   PYTHONDONTWRITEBYTECODE="1")
        for git_found in (False, True):
            with self.subTest(git_found=git_found):
                available = {name: name for name in ("winget", "powershell")}
                if git_found:
                    available["git"] = "git"
                # Patch before importing setup: Windows refreshes PATH from the registry, so an
                # empty process PATH alone cannot keep installed tools out of this test.
                script = (
                    "import runpy, sys\n"
                    "from unittest import mock\n"
                    "sys.argv = ['oah.setup'] + sys.argv[1:]\n"
                    f"available = {available!r}\n"
                    "with mock.patch('shutil.which', side_effect=available.get):\n"
                    "    runpy.run_module('oah.setup', run_name='__main__')\n"
                )
                result = subprocess.run([sys.executable, "-c", script, "--os", "windows", "--dry-run"],
                                        env=env, capture_output=True, timeout=60, cwd=str(work))
                self.assertEqual(result.returncode, 0, result.stderr)
                text = result.stdout.decode("utf-8")
                self.assertIn(["git", "required", "found" if git_found else "missing"],
                              [line.split() for line in text.splitlines()])
                command = "winget install --id Git.Git -e --source winget"
                if git_found:
                    self.assertNotIn("  - git (required):", text)
                    self.assertNotIn(command, text)
                else:
                    self.assertIn(f"  - git (required): {command}", text.splitlines())
                self.assertIn("irm https://chatgpt.com/codex/install.ps1 | iex", text)
                self.assertTrue(text.rstrip().splitlines()[-1].startswith("SETUP_DRY_RUN planned="))
                self.assertEqual(list(work.iterdir()), [])

    def test_module_main_rejects_unknown_option(self):
        env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-m", "oah.setup", "--bogus"], env=env, capture_output=True,
                                timeout=60)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
