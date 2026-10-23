# oah.setup 검사 — 도구 목록의 완결성, 설치 계획·동의·실행, 로그인 안내, python -m oah.setup 실행을 확인한다.
from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import unittest

from oah.engines.proc import UnsupportedBatchShim
from oah.setup import guide
from oah.setup import tools as cat
from tests.helpers import make_tmp_dir, make_unlazy

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
                # Every tool can be installed by command on both OSes; node on macOS without
                # Homebrew falls back to the download page at plan time.
                self.assertTrue(tool.install.get("macos") and tool.install.get("windows"))

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
        self.assertEqual(tools["unlazy"].install["windows"][0], ("npx", "skills", "add", "Leonxlnx/unlazy"))
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

    def test_sign_in_commands(self):
        tools = {t.name: t for t in cat.catalog()}
        self.assertEqual((tools["claude"].sign_in.status_args, tools["claude"].sign_in.login),
                         (("auth", "status"), "claude auth login"))
        self.assertEqual((tools["codex"].sign_in.status_args, tools["codex"].sign_in.login),
                         (("login", "status"), "codex login"))
        self.assertEqual([t.name for t in tools.values() if t.sign_in], ["claude", "codex"])

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
        self.assertEqual(node.display, "brew install node@24")
        self.assertIn("brew", node.note)

    def test_plan_counts_programs_installed_earlier(self):
        statuses = self.check(fake_which())
        steps = by_name(guide.plan_installs(statuses, os_name="windows", which=fake_which("powershell", "winget")))
        self.assertEqual(steps["uv"].argv[-1], "irm https://astral.sh/uv/install.ps1 | iex")
        self.assertEqual(steps["graphify"].argv, ("uv", "tool", "install", "graphifyy"))
        self.assertEqual(steps["unlazy"].argv, ("npx", "skills", "add", "Leonxlnx/unlazy"))
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
    def test_refresh_path_merges_registry_values(self):
        env = {"PATH": r"C:\py;C:\Windows", "SystemRoot": r"C:\Windows", "USERPROFILE": r"C:\home\someone"}
        guide.refresh_path(env, platform="win32",
                           read_registry=lambda: (r"%SystemRoot%;%SystemRoot%\System32", r"%USERPROFILE%\.local\bin;"))
        self.assertEqual(env["PATH"], r"C:\Windows;C:\Windows\System32;C:\home\someone\.local\bin;C:\py")

    def test_refresh_path_adds_local_bin_on_posix(self):
        home = make_tmp_dir(self)
        env = {"PATH": "/usr/bin:/bin"}
        guide.refresh_path(env, platform="darwin", home=home)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")  # no ~/.local/bin yet
        (home / ".local" / "bin").mkdir(parents=True)
        guide.refresh_path(env, platform="darwin", home=home)
        guide.refresh_path(env, platform="darwin", home=home)
        self.assertEqual(env["PATH"], "/usr/bin:/bin:" + str(home / ".local" / "bin"))

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
        self.assertEqual(calls, [("/x/codex", "login", "status"), ("/x/claude", "auth", "status")])
        self.assertFalse(guide.check_sign_in(self.tools["codex"], "/x/codex", status_runner=lambda argv: (None, "")))

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
        self.assertIn("This runs: brew install node@24", text)
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
        result = subprocess.run([sys.executable, "-m", "oah.setup", "--os", "windows", "--dry-run"], env=env,
                                capture_output=True, timeout=60, cwd=str(work))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.decode("utf-8")
        self.assertIn("winget install --id Git.Git -e --source winget", text)
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
