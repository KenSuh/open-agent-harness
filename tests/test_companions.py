# 선택 동반 앱 검사 — Orca·Aside 의 공식 설치, 동의 기반 연결 단계, 계정 정보 비노출, 선택 규칙 절, 문서를 확인한다.
from __future__ import annotations

import io
import os
import pathlib
import re
import secrets
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from oah.doctor import check_tools as doctor_check, exit_code, render_text
from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim
from oah.setup import companions as comp
from oah.setup import guide
from oah.setup import tools as cat
from tests.helpers import git_env, make_git_repo, make_tmp_dir, make_unlazy, write_posix_tool

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OS_NAMES = ("macos", "windows", "linux")
PLAIN_ORCA_CASK = re.compile(r"--cask\s+orca(?![\w/.-])")


def fake_which(*names: str):
    found = set(names)
    return lambda name, *args, **kwargs: f"/fake/bin/{name}" if name in found else None


class Answers:
    """Stand-in for input(): hands out answers in order and returns None (EOF) when they run out."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


class FakeProbe:
    """Exit codes per argv; records every check. Unknown checks fail with exit code 1."""

    def __init__(self, codes=None):
        self.codes = dict(codes or {})
        self.calls: list[tuple] = []

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), cwd))
        value = self.codes.get(tuple(argv), 1)
        return value() if callable(value) else value


class FakeRunner:
    def __init__(self, code=0, on_run=None):
        self.code = code
        self.on_run = on_run
        self.calls: list[tuple] = []

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), cwd))
        if self.on_run is not None:
            self.on_run(tuple(argv))
        return self.code


def plain_argvs(steps):
    return [step.argv for step in steps if step.argv is not None]


class CatalogTest(unittest.TestCase):
    def by_name(self):
        return {tool.name: tool for tool in comp.companion_tools()}

    def test_companion_entries(self):
        tools = self.by_name()
        self.assertEqual(list(tools), ["orca", "aside"])
        for tool in tools.values():
            with self.subTest(tool=tool.name):
                self.assertFalse(tool.required)
                self.assertTrue(tool.optional)
                self.assertEqual(tool.requirement, "optional")
                self.assertTrue(tool.purpose and "\n" not in tool.purpose)
                self.assertEqual(tool.check, (tool.name, "--version"))
                self.assertEqual(tool.version_args, ("--version",))
                for os_name in ("macos", "windows"):
                    self.assertTrue(tool.url_for(os_name).startswith("https://"))
                    self.assertTrue(tool.install.get(os_name) or tool.note.get(os_name))

    def test_install_per_os(self):
        orca, aside = self.by_name()["orca"], self.by_name()["aside"]
        self.assertEqual(orca.install["macos"], (("brew", "install", "--cask", "stablyai/orca/orca"),))
        self.assertEqual(orca.url_for("macos"), "https://www.onorca.dev/download")
        self.assertEqual(orca.install["windows"], (("winget", "install", "--id", "StablyAI.Orca", "-e"),))
        self.assertEqual(orca.url_for("windows"), "https://www.onorca.dev/download")
        self.assertEqual(orca.install["linux"], ())
        self.assertEqual(orca.url_for("linux"), "https://www.onorca.dev/docs/install")
        self.assertEqual(aside.install["macos"], (("brew", "install", "--cask", "aside"),))
        self.assertEqual(aside.url_for("macos"), "https://aside.com/download?os=mac")
        self.assertEqual(aside.install["windows"], ())
        self.assertEqual(aside.url_for("windows"), "https://aside.com/download?os=win")
        self.assertIn("installer", aside.note["windows"])
        self.assertEqual(aside.install["linux"], ())
        self.assertIn("does not support Linux", aside.note["linux"])

    def test_catalog_includes_companions_as_optional(self):
        tools = {tool.name: tool for tool in cat.catalog()}
        self.assertEqual([tool.name for tool in cat.catalog()][-2:], ["orca", "aside"])
        self.assertEqual((tools["orca"].requirement, tools["aside"].requirement), ("optional", "optional"))
        # The hook leaves P8's own entries as they were.
        self.assertEqual(tools["node"].requirement, "recommended")
        self.assertEqual(tools["git"].requirement, "required")

    def test_no_plan_contains_plain_orca_cask(self):
        texts = []
        for tool in cat.catalog():
            for os_name in OS_NAMES:
                for argv in tool.install.get(os_name, ()):
                    self.assertNotEqual(tuple(argv[-2:]), ("--cask", "orca"))
                    texts.append(cat.display(argv))
                texts.append(tool.note.get(os_name, ""))
        statuses = guide.check_tools([t for t in cat.catalog() if t.name == "orca"], which=fake_which())
        for os_name in ("macos", "windows"):
            for step in guide.plan_installs(statuses, os_name=os_name, which=fake_which("brew", "winget")):
                texts.append(step.display)
                self.assertNotEqual(tuple((step.argv or ())[-2:]), ("--cask", "orca"))
        for os_name in OS_NAMES:
            for step in comp.plan_steps(("orca", "aside"), os_name=os_name, project_root=ROOT,
                                        remote_url="https://github.com/someone/sample.git"):
                texts += [step.display, step.instruction]
        for text in texts:
            self.assertIsNone(PLAIN_ORCA_CASK.search(text), text)
        # The pattern itself catches the unrelated cask.
        self.assertIsNotNone(PLAIN_ORCA_CASK.search("brew install --cask orca"))
        self.assertIsNone(PLAIN_ORCA_CASK.search("brew install --cask stablyai/orca/orca"))


def doctor_fakes(present):
    def resolve(name, search_path=None):
        return Resolved((f"/fake/bin/{name}",), f"/fake/bin/{name}", "native") if name in present else None

    calls = []

    def run(argv, *, timeout, **kwargs):
        calls.append(tuple(argv))
        return RunResult(0, "v24.1.0\n", "", False)

    return resolve, run, calls


class DoctorAndGuideTest(unittest.TestCase):
    def setUp(self):
        self.home = make_tmp_dir(self) / "home"
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        self.base = {"git", "uv", "node", "claude", "codex", "graphify", "gh", "gitleaks", "jq", "rg"}

    def doctor(self, present):
        resolve, run, calls = doctor_fakes(present)
        statuses = doctor_check(search_path=None, env={}, home=self.home, repo_root=None, platform="darwin",
                                resolver=resolve, runner=run)
        return {s.name: s for s in statuses}, statuses, calls

    def test_doctor_lists_companions_as_optional(self):
        rows, statuses, _ = self.doctor(self.base)
        self.assertEqual((rows["orca"].requirement, rows["orca"].status), ("optional", "missing"))
        self.assertEqual((rows["aside"].requirement, rows["aside"].status), ("optional", "missing"))
        self.assertEqual(exit_code(statuses), 0)
        text = render_text(statuses, "darwin")
        self.assertIn("orca (optional): brew install --cask stablyai/orca/orca\n", text)
        self.assertIn("https://www.onorca.dev/download", text)
        self.assertIn("aside (optional): brew install --cask aside\n", text)
        self.assertTrue(text.endswith("DOCTOR_OK"))
        windows = render_text(statuses, "win32")
        self.assertIn("orca (optional): winget install --id StablyAI.Orca -e", windows)
        self.assertIn("https://aside.com/download?os=win", windows)

    def test_doctor_checks_present_companions_by_version(self):
        rows, statuses, calls = self.doctor(self.base | {"orca", "aside"})
        self.assertEqual((rows["orca"].status, rows["aside"].status), ("ok", "ok"))
        self.assertIn(("/fake/bin/orca", "--version"), calls)
        self.assertIn(("/fake/bin/aside", "--version"), calls)
        self.assertFalse([call for call in calls if call[0].endswith("aside") and call[1:] != ("--version",)])
        self.assertEqual(exit_code(statuses), 0)

    def test_guided_setup_offers_companions_as_optional(self):
        out = []
        runner = FakeRunner()
        which = fake_which(*self.base, "brew")
        code = guide.guided_setup(os_name="macos", which=which, ask=Answers("n", "n"), out=out.append,
                                  runner=lambda argv: runner(argv, None),
                                  status_runner=lambda argv: (0, '{"loggedIn": true}'),
                                  refresh=lambda: None, env={}, home=self.home)
        text = "\n".join(out)
        self.assertEqual(code, 0)
        self.assertIn("orca (optional): brew install --cask stablyai/orca/orca", text)
        self.assertIn("aside (optional): brew install --cask aside", text)
        self.assertIn("--yes skips it", text)
        self.assertEqual(runner.calls, [])

    def guided(self, *, yes=False, with_optional=(), answers=(), missing=("orca", "aside", "jq")):
        out, runner = [], FakeRunner()
        present = (self.base - set(missing)) | {"brew"}
        code = guide.guided_setup(os_name="macos", yes=yes, with_optional=with_optional, which=fake_which(*present),
                                  ask=Answers(*answers), out=out.append, runner=lambda argv: runner(argv, None),
                                  status_runner=lambda argv: (0, '{"loggedIn": true}'), refresh=lambda: None,
                                  env={}, home=self.home)
        return code, [argv for argv, _ in runner.calls], "\n".join(out)

    ORCA = ("brew", "install", "--cask", "stablyai/orca/orca")
    ASIDE = ("brew", "install", "--cask", "aside")
    JQ = ("brew", "install", "jq")

    def test_yes_never_installs_optional_apps(self):
        code, calls, text = self.guided(yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [self.JQ])  # the recommended tool in the plan still installs
        self.assertIn("orca: skipped", text)

    def test_named_optional_apps_install(self):
        _, calls, _ = self.guided(yes=True, with_optional=("orca",))
        self.assertEqual(calls, [self.JQ, self.ORCA])
        # Naming an app is its consent, so it is not asked about; the others still are.
        _, calls, _ = self.guided(with_optional=("aside",), answers=("n", "n"))
        self.assertEqual(calls, [self.ASIDE])

    def test_interactive_yes_installs_that_one_app(self):
        # Plan order: jq, orca, aside.
        _, calls, _ = self.guided(answers=("n", "y", "n"))
        self.assertEqual(calls, [self.ORCA])

    def test_unknown_optional_name_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.guided(yes=True, with_optional=("jq",))
        self.assertIn("OAH_SETUP_USAGE", str(caught.exception))


class StepsTest(unittest.TestCase):
    def setUp(self):
        self.root = make_tmp_dir(self) / "my project"
        self.root.mkdir()

    def steps(self, apps=("orca", "aside"), os_name="macos", remote="git@github.com:someone/sample.git"):
        return {step.key: step for step in comp.plan_steps(apps, os_name=os_name, project_root=self.root,
                                                            remote_url=remote)}

    def test_orca_steps(self):
        steps = self.steps(apps=("orca",))
        self.assertEqual(list(steps), ["orca-cli", "orca-skills", "orca-project"])
        cli = steps["orca-cli"]
        self.assertIsNone(cli.argv)
        self.assertIn("Settings", cli.instruction)
        self.assertIn("Orca CLI", cli.instruction)
        self.assertEqual(cli.verify, ("orca", "--version"))
        self.assertFalse(cli.global_write)
        skills = steps["orca-skills"]
        self.assertEqual(skills.argv, ("orca", "skills", "install", "--all"))
        self.assertTrue(skills.global_write)
        self.assertEqual(skills.needs, ("orca", "npx"))
        project = steps["orca-project"]
        self.assertEqual(project.argv, ("orca", "project", "setup-existing-folder", "--project", "github:someone/sample",
                                        "--host", "local", "--path", str(self.root), "--kind", "git"))
        self.assertTrue(project.global_write)
        self.assertIn(f'"{self.root}"', project.display)  # a path with a space is shown quoted

    def test_github_slug_forms(self):
        token = "tok" + secrets.token_hex(6)  # a remote URL can carry a token; it must never be shown
        with_token = "https://x-access-token:" + token + "@" + "github.com/someone/sample.git"
        cases = {
            "https://github.com/someone/sample.git": "someone/sample",
            "https://github.com/someone/sample": "someone/sample",
            "https://github.com/someone/sample/": "someone/sample",
            "http://github.com/someone/sample.git": "someone/sample",
            "git@github.com:someone/sample.git": "someone/sample",
            "ssh://git@github.com/some-one/sam.ple.git": "some-one/sam.ple",
            with_token: "someone/sample",
            "https://gitlab.com/someone/sample.git": None,
            "https://github.com/someone": None,
            "https://github.com.evil.example/someone/sample.git": None,
            "https://github.com/someone/sample/extra": None,
            "": None,
            None: None,
        }
        for url, slug in cases.items():
            with self.subTest(url=url):
                self.assertEqual(comp.github_slug(url), slug)
        out = []
        comp.print_plan(comp.plan_steps(("orca",), os_name="macos", project_root=self.root, remote_url=with_token),
                        out=out.append)
        self.assertIn("github:someone/sample", "\n".join(out))
        self.assertNotIn(token, "\n".join(out))

    def test_project_without_github_remote_is_an_instruction(self):
        for remote in (None, "https://gitlab.com/someone/sample.git"):
            with self.subTest(remote=remote):
                project = self.steps(apps=("orca",), remote=remote)["orca-project"]
                self.assertIsNone(project.argv)
                self.assertIn(str(self.root), project.instruction)
                self.assertIn("Orca", project.instruction)

    def test_aside_steps_macos(self):
        steps = self.steps(apps=("aside",))
        self.assertEqual(list(steps), ["aside-cli", "aside-login", "aside-mcp-claude", "aside-mcp-codex"])
        cli = steps["aside-cli"]
        self.assertEqual(cli.argv, ("/bin/sh", "-c", "curl -fsSL https://releases.aside.com/install.sh | bash"))
        self.assertEqual(cli.display, "curl -fsSL https://releases.aside.com/install.sh | bash")
        self.assertEqual(cli.source, "https://docs.aside.com/help/developers")
        self.assertEqual(cli.skip_if_found, "aside")
        self.assertTrue(cli.global_write)
        login = steps["aside-login"]
        self.assertIsNone(login.argv)
        self.assertIn("aside login", login.instruction)
        self.assertEqual(login.verify, ("aside", "account", "status"))
        claude = steps["aside-mcp-claude"]
        self.assertEqual(claude.argv, ("claude", "mcp", "add", "--transport", "stdio", "aside", "--", "aside", "mcp"))
        self.assertEqual(claude.skip_if, ("claude", "mcp", "get", "aside"))
        self.assertEqual(claude.needs, ("claude", "aside"))
        codex = steps["aside-mcp-codex"]
        self.assertEqual(codex.argv, ("codex", "mcp", "add", "aside", "--", "aside", "mcp"))
        self.assertEqual(codex.skip_if, ("codex", "mcp", "get", "aside"))
        self.assertTrue(claude.global_write and codex.global_write)

    def test_aside_steps_windows_use_template(self):
        cli = self.steps(apps=("aside",), os_name="windows")["aside-cli"]
        template = comp.TEMPLATES / "aside-cli-install.ps1"
        self.assertEqual(cli.argv, ("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(template)))
        text = template.read_text(encoding="utf-8")
        self.assertEqual(cli.display, text.strip().replace("\r\n", "\n"))
        self.assertIn("https://releases.aside.com/install.ps1", text)
        self.assertIn("Get-AuthenticodeSignature", text)
        self.assertIn("https://docs.aside.com/help/developers", text)
        self.assertTrue(text.isascii())
        self.assertEqual(cli.needs, ("powershell",))

    def test_aside_linux_is_unsupported(self):
        steps = self.steps(apps=("aside",), os_name="linux")
        self.assertEqual(list(steps), ["aside-cli"])
        self.assertIsNone(steps["aside-cli"].argv)
        self.assertIn("does not support Linux", steps["aside-cli"].instruction)
        orca = self.steps(apps=("orca",), os_name="linux")
        self.assertEqual(orca["orca-skills"].argv, ("orca", "skills", "install", "--all"))

    def test_plan_labels_global_steps(self):
        out = []
        steps = comp.plan_steps(("orca", "aside"), os_name="macos", project_root=self.root,
                                remote_url="https://github.com/someone/sample")
        comp.print_plan(steps, out=out.append)
        text = "\n".join(out)
        for step in steps:
            line = next(line for line in out if step.title in line)
            self.assertEqual("[global]" in line, step.global_write, line)
            if step.argv is not None:
                self.assertIn(step.display.splitlines()[0], text)
        self.assertIn("claude mcp add --transport stdio aside -- aside mcp", text)
        self.assertIn("Nothing runs until you answer y", text)


class RunStepsTest(unittest.TestCase):
    def setUp(self):
        self.root = make_tmp_dir(self)
        self.out: list[str] = []
        self.everything = fake_which("orca", "npx", "aside", "claude", "codex", "curl", "powershell")

    def run_steps(self, steps, *, answers=(), yes=False, which=None, probe=None, runner=None):
        self.probe = probe or FakeProbe()
        self.runner = runner or FakeRunner()
        self.ask = Answers(*answers)
        return {o.step.key: o.status for o in comp.run_steps(
            steps, project_root=self.root, yes=yes, which=which or self.everything, ask=self.ask,
            out=self.out.append, runner=self.runner, probe=self.probe, refresh=lambda: None)}

    def plan(self, apps=("orca", "aside"), os_name="macos"):
        return comp.plan_steps(apps, os_name=os_name, project_root=self.root,
                               remote_url="https://github.com/someone/sample.git")

    def test_declined_steps_run_nothing(self):
        for os_name in ("macos", "windows"):
            with self.subTest(os_name=os_name):
                which = fake_which("orca", "npx", "claude", "codex", "curl", "powershell")  # no aside CLI yet
                probe = FakeProbe({("orca", "--version"): 0})
                statuses = self.run_steps(self.plan(os_name=os_name), answers=("n",) * 10, which=which, probe=probe)
                self.assertEqual(self.runner.calls, [])
                self.assertEqual(statuses["orca-skills"], "declined")
                self.assertEqual(statuses["orca-project"], "declined")
                self.assertEqual(statuses["aside-cli"], "declined")
                self.assertEqual(statuses["aside-mcp-claude"], "blocked")  # needs the aside CLI
                mutating = [argv for argv, _ in self.probe.calls if "add" in argv or "install" in argv]
                self.assertEqual(mutating, [])

    def test_eof_counts_as_no(self):
        statuses = self.run_steps(self.plan(apps=("orca",)), answers=(), probe=FakeProbe({("orca", "--version"): 0}))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(statuses, {"orca-cli": "present", "orca-skills": "declined", "orca-project": "declined"})

    def test_accepted_steps_run_in_project_root(self):
        registered = set()
        probe = FakeProbe({("orca", "--version"): 0, ("aside", "account", "status"): 0,
                           ("claude", "mcp", "get", "aside"): lambda: 0 if "claude" in registered else 1,
                           ("codex", "mcp", "get", "aside"): lambda: 0 if "codex" in registered else 1})
        runner = FakeRunner(on_run=lambda argv: registered.add(argv[0]) if "mcp" in argv else None)
        statuses = self.run_steps(self.plan(), answers=("y",) * 10, probe=probe, runner=runner)
        self.assertEqual([argv for argv, _ in runner.calls], [
            ("orca", "skills", "install", "--all"),
            ("orca", "project", "setup-existing-folder", "--project", "github:someone/sample", "--host", "local",
             "--path", str(self.root), "--kind", "git"),
            ("claude", "mcp", "add", "--transport", "stdio", "aside", "--", "aside", "mcp"),
            ("codex", "mcp", "add", "aside", "--", "aside", "mcp"),
        ])
        self.assertTrue(all(cwd == self.root for _, cwd in runner.calls))
        self.assertTrue(all(cwd == self.root for _, cwd in probe.calls))
        self.assertEqual(statuses, {"orca-cli": "present", "orca-skills": "done", "orca-project": "done",
                                    "aside-cli": "present", "aside-login": "present", "aside-mcp-claude": "done",
                                    "aside-mcp-codex": "done"})
        self.assertEqual(len(self.ask.prompts), 4)
        self.assertTrue(all("[y/N]" in prompt for prompt in self.ask.prompts))
        text = "\n".join(self.out)
        self.assertIn("[global]", text)

    def test_yes_runs_only_listed_steps_without_asking(self):
        probe = FakeProbe({("orca", "--version"): 0})
        statuses = self.run_steps(self.plan(apps=("orca",)), yes=True, probe=probe)
        self.assertEqual(self.ask.prompts, [])
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(statuses["orca-skills"], "done")

    def test_aside_cli_installer_runs_with_consent(self):
        for os_name, program in (("macos", "/bin/sh"), ("windows", "powershell")):
            with self.subTest(os_name=os_name):
                steps = [s for s in self.plan(apps=("aside",), os_name=os_name) if s.key == "aside-cli"]
                statuses = self.run_steps(steps, answers=("y",), which=fake_which("curl", "powershell"),
                                          probe=FakeProbe({("aside", "--version"): 0}))
                self.assertEqual(statuses, {"aside-cli": "done"})
                self.assertEqual(self.runner.calls[0][0][0], program)
                statuses = self.run_steps(steps, answers=("y",), which=fake_which("curl", "powershell"))
                self.assertEqual(statuses, {"aside-cli": "pending"})  # installed, but not found yet
                self.assertIn("new terminal", self.out[-1])

    def test_existing_mcp_registration_is_skipped(self):
        probe = FakeProbe({("aside", "account", "status"): 0, ("claude", "mcp", "get", "aside"): 0,
                           ("codex", "mcp", "get", "aside"): 0})
        statuses = self.run_steps(self.plan(apps=("aside",)), answers=("y",) * 5, probe=probe)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(statuses["aside-mcp-claude"], "present")
        self.assertEqual(statuses["aside-mcp-codex"], "present")
        self.assertIn("already registered", "\n".join(self.out))

    def test_missing_program_blocks_step(self):
        statuses = self.run_steps(self.plan(apps=("aside",)), answers=("y",) * 5,
                                  which=fake_which("aside", "claude"), probe=FakeProbe({("aside", "account", "status"): 0}))
        self.assertEqual(statuses["aside-mcp-codex"], "blocked")
        self.assertNotIn(("codex", "mcp", "add", "aside", "--", "aside", "mcp"), [c for c, _ in self.runner.calls])
        self.assertIn("codex", "\n".join(self.out))
        self.assertIn("python -m oah.setup", "\n".join(self.out))

    def test_failed_command_is_reported(self):
        statuses = self.run_steps(self.plan(apps=("orca",)), answers=("y", "y"),
                                  probe=FakeProbe({("orca", "--version"): 0}), runner=FakeRunner(code=3))
        self.assertEqual(statuses["orca-skills"], "failed")
        self.assertIn("exited with code 3", "\n".join(self.out))

    def test_refused_batch_shim_is_a_failure(self):
        def refuse(argv, cwd):
            raise UnsupportedBatchShim("C:\\fake\\orca.cmd", "batch files are never launched directly")

        statuses = self.run_steps(self.plan(apps=("orca",)), answers=("y", "y"),
                                  probe=FakeProbe({("orca", "--version"): 0}), runner=refuse)
        self.assertEqual(statuses["orca-skills"], "failed")
        self.assertIn("OAH_UNSUPPORTED_BATCH_SHIM", "\n".join(self.out))

    def test_instruction_step_verifies_by_exit_code(self):
        enabled = []
        probe = FakeProbe({("orca", "--version"): lambda: 0 if enabled else 1})
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            if len(prompts) == 2:
                enabled.append(True)  # the user turns the CLI on before pressing Enter the second time
            return "" if len(prompts) <= 2 else None

        steps = [s for s in self.plan(apps=("orca",)) if s.key == "orca-cli"]
        result = comp.run_steps(steps, project_root=self.root, yes=False, which=self.everything, ask=answer,
                                out=self.out.append, runner=FakeRunner(), probe=probe, refresh=lambda: None)
        self.assertEqual(result[0].status, "done")
        text = "\n".join(self.out)
        self.assertIn("Settings", text)
        self.assertIn("did not pass yet", text)

    def test_instruction_step_can_be_skipped(self):
        statuses = self.run_steps(self.plan(apps=("orca",)), answers=("s",))
        self.assertEqual(statuses["orca-cli"], "pending")
        self.assertEqual(statuses["orca-skills"], "blocked")  # waits for the CLI step, though orca is on PATH
        self.assertEqual(self.runner.calls, [])

    def test_yes_checks_instruction_once(self):
        statuses = self.run_steps(self.plan(apps=("aside",)), yes=True, which=fake_which("aside"))
        self.assertEqual(statuses["aside-login"], "pending")
        self.assertEqual(self.ask.prompts, [])
        self.assertIn("aside login", "\n".join(self.out))

    def test_probe_output_is_never_shown(self):
        token = "tok_" + secrets.token_hex(12)
        account = "acct-" + secrets.token_hex(4)

        def noisy_probe(argv, cwd):
            print(f"Signed in as {account} token={token}")  # what a careless probe would leak
            return 0

        with redirect_stdout(io.StringIO()):
            self.run_steps(self.plan(apps=("aside",)), answers=("y",) * 5, probe=noisy_probe)
        joined = "\n".join(self.out)
        self.assertNotIn(token, joined)
        self.assertNotIn(account, joined)

    def test_default_probe_discards_output(self):
        token = "tok_" + secrets.token_hex(12)
        code = f"import sys; print('Signed in as {token}'); sys.stderr.write('{token}'); sys.exit(3)"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(comp.default_probe((sys.executable, "-c", code), self.root), 3)
        self.assertEqual(buffer.getvalue(), "")
        self.assertIsNone(comp.default_probe(("oah-no-such-program-" + secrets.token_hex(4),), self.root))

    def test_default_runner_runs_in_project_root(self):
        target = self.root / "cwd.txt"
        code = "import os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(os.getcwd(), encoding='utf-8')"
        self.assertEqual(comp.default_runner((sys.executable, "-c", code, str(target)), self.root), 0)
        self.assertEqual(pathlib.Path(target.read_text(encoding="utf-8")).resolve(), self.root.resolve())

    @unittest.skipUnless(os.name == "nt", "Windows batch-file rule")
    def test_default_runner_refuses_batch(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        marker = self.root / "ran.txt"
        (bin_dir / "oahfake.cmd").write_text(f"@echo off\r\necho ran> \"{marker}\"\r\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            with self.assertRaises(UnsupportedBatchShim):
                comp.default_runner(("oahfake",), self.root)
            self.assertIsNone(comp.default_probe(("oahfake",), self.root))
        self.assertFalse(marker.exists())

    def test_dry_run_runs_nothing(self):
        probe, runner, ask = FakeProbe(), FakeRunner(), Answers()
        result = comp.companion_setup(apps=("orca", "aside"), os_name="macos", project_root=self.root,
                                      remote_url="https://github.com/someone/sample", dry_run=True,
                                      which=self.everything, ask=ask, out=self.out.append, runner=runner,
                                      probe=probe, refresh=lambda: None)
        self.assertEqual((probe.calls, runner.calls, ask.prompts), ([], [], []))
        self.assertEqual(self.out[-1], "COMPANIONS_DRY_RUN steps=7")
        self.assertEqual(result.outcomes, [])

    def test_aside_registered_result(self):
        def setup(codes):
            return comp.companion_setup(apps=("aside",), os_name="macos", project_root=self.root, remote_url=None,
                                        yes=True, which=self.everything, ask=Answers(), out=self.out.append,
                                        runner=FakeRunner(code=1), probe=FakeProbe(codes), refresh=lambda: None)

        self.assertFalse(setup({("aside", "account", "status"): 0}).aside_registered)
        result = setup({("aside", "account", "status"): 0, ("codex", "mcp", "get", "aside"): 0})
        self.assertTrue(result.aside_registered)
        self.assertFalse(result.ok)  # the Claude Code registration failed
        self.assertTrue(self.out[-1].startswith("COMPANIONS_INCOMPLETE pending="))
        self.assertIn("aside-mcp-claude", self.out[-1])
        everything = setup({("aside", "account", "status"): 0, ("codex", "mcp", "get", "aside"): 0,
                            ("claude", "mcp", "get", "aside"): 0})
        self.assertTrue(everything.ok)
        self.assertEqual(self.out[-1], "COMPANIONS_OK")


FAKE_TOOL = """\
printf '%s|%s\\n' "$(basename "$0") $*" "$(pwd)" >> "{log}"
"""


@unittest.skipIf(os.name == "nt", "POSIX fake executables")
class CompanionsCliTest(unittest.TestCase):
    """python -m oah.setup.companions in a real process, with fake orca, aside, claude, codex and npx."""

    def setUp(self):
        work = make_tmp_dir(self)
        self.work = work
        self.repo = make_git_repo(self)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "https://github.com/someone/sample.git"],
                       check=True, env=git_env())
        self.bin = work / "bin"
        self.state = work / "state"
        self.state.mkdir()
        self.log = work / "calls.log"
        self.token = "tok_" + secrets.token_hex(12)
        self.account = "acct-" + secrets.token_hex(4)
        self.email = "user-" + secrets.token_hex(4) + "@" + "example.invalid"
        log = FAKE_TOOL.format(log=self.log)
        write_posix_tool(self.bin, "orca", log + 'if [ "$1" = "--version" ]; then echo 1.4.0; fi\nexit 0')
        write_posix_tool(self.bin, "npx", log + "exit 0")
        write_posix_tool(self.bin, "aside", log + (
            'if [ "$1" = "account" ]; then echo "Signed in as {email} ({account})"; '
            'echo "token={token}" >&2; fi\nexit 0').format(email=self.email, account=self.account, token=self.token))
        for engine in ("claude", "codex"):
            write_posix_tool(self.bin, engine, log + (
                'if [ "$2" = "get" ]; then echo "aside: aside mcp account={account}"; '
                '[ -f "{state}/{engine}" ] && exit 0; exit 1; fi\n'
                'if [ "$2" = "add" ]; then touch "{state}/{engine}"; fi\nexit 0').format(
                    account=self.account, state=self.state, engine=engine))
        self.home = work / "home"
        self.home.mkdir()

    def run_cli(self, *args, stdin=""):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("PYTHON", "OAH_"))}
        env.update(PATH=f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", PYTHONPATH=str(SRC), HOME=str(self.home),
                   PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-m", "oah.setup.companions", "--os", "macos", "--project",
                               str(self.repo), *args], input=stdin, capture_output=True, text=True,
                              encoding="utf-8", env=env, cwd=str(self.repo), timeout=60)

    def calls(self):
        if not self.log.exists():
            return []
        return [line.split("|")[0] for line in self.log.read_text(encoding="utf-8").splitlines()]

    def assert_no_account_data(self, result):
        for value in (self.token, self.account, self.email):
            self.assertNotIn(value, result.stdout)
            self.assertNotIn(value, result.stderr)
        for folder in (self.repo, self.home, self.state):
            for path in folder.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    for value in (self.token, self.account, self.email):
                        self.assertNotIn(value.encode(), data, path)

    def test_cli_declines_run_nothing(self):
        result = self.run_cli(stdin="n\n" * 10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(sorted(set(self.calls())), sorted({"orca --version", "aside account status",
                                                            "claude mcp get aside", "codex mcp get aside"}))
        self.assertIn("[global]", result.stdout)
        self.assertIn("orca skills install --all", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith(
            "COMPANIONS_INCOMPLETE pending=orca-skills,orca-project,aside-mcp-claude,aside-mcp-codex"))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_cli_yes_runs_listed_steps(self):
        result = self.run_cli("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertIn("orca skills install --all", calls)
        self.assertIn(f"orca project setup-existing-folder --project github:someone/sample --host local --path "
                      f"{self.repo} --kind git", calls)
        self.assertIn("claude mcp add --transport stdio aside -- aside mcp", calls)
        self.assertIn("codex mcp add aside -- aside mcp", calls)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        add = next(line for line in lines if line.startswith("claude mcp add"))
        self.assertEqual(pathlib.Path(add.split("|")[1]).resolve(), self.repo.resolve())
        self.assertTrue(result.stdout.rstrip().endswith("COMPANIONS_OK"))
        self.assert_no_account_data(result)

    def test_cli_never_prints_account_data(self):
        for stdin in ("y\n" * 10, "n\n" * 10):
            result = self.run_cli("--app", "aside", stdin=stdin)
            self.assert_no_account_data(result)
        self.assertIn("aside account status", self.calls())

    def test_cli_dry_run(self):
        result = self.run_cli("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue(result.stdout.rstrip().endswith("COMPANIONS_DRY_RUN steps=7"))

    def test_cli_usage_error(self):
        result = self.run_cli("--app", "bogus")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])


class RulesSectionTest(unittest.TestCase):
    def test_rules_section_absent_without_aside(self):
        self.assertIsNone(comp.rules_section(False))
        self.assertIsNone(comp.rules_section(False, lang="ko"))

    def test_rules_section_holds_three_rules(self):
        text = comp.rules_section(True)
        self.assertTrue(text.startswith(comp.RULES_BEGIN + "\n"))
        self.assertTrue(text.endswith(comp.RULES_END + "\n"))
        self.assertEqual((text.count(comp.RULES_BEGIN), text.count(comp.RULES_END)), (1, 1))
        rules = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(rules), 3)
        self.assertIn("Aside", rules[0])
        self.assertIn("web", rules[0])
        self.assertIn("data", rules[1])
        self.assertIn("instructions", rules[1])
        self.assertIn("payment", rules[2])
        self.assertIn("Never", rules[2])
        self.assertLess(len(text.encode("utf-8")), 1024)

    def test_rules_section_korean(self):
        text = comp.rules_section(True, lang="ko")
        self.assertTrue(text.startswith(comp.RULES_BEGIN + "\n") and text.endswith(comp.RULES_END + "\n"))
        rules = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(rules), 3)
        self.assertIn("Aside", rules[0])
        self.assertIn("데이터", rules[1])
        self.assertIn("결제", rules[2])
        with self.assertRaises(ValueError):
            comp.rules_section(True, lang="fr")


class DocsTest(unittest.TestCase):
    DOCS = (ROOT / "docs" / "companions.md", ROOT / "docs" / "ko" / "companions.md")

    def test_docs_name_official_sources(self):
        for doc in self.DOCS:
            with self.subTest(doc=doc.name):
                text = doc.read_text(encoding="utf-8")
                for needle in ("brew install --cask stablyai/orca/orca", "winget install --id StablyAI.Orca -e",
                               "https://www.onorca.dev/download", "https://www.onorca.dev/docs/install",
                               "brew install --cask aside", "https://aside.com/download?os=mac",
                               "https://aside.com/download?os=win", "https://docs.aside.com/help/developers",
                               "curl -fsSL https://releases.aside.com/install.sh | bash",
                               "claude mcp add --transport stdio aside -- aside mcp",
                               "codex mcp add aside -- aside mcp", "claude mcp remove aside",
                               "codex mcp remove aside", "orca skills install --all", "aside login",
                               "python -m oah.setup.companions"):
                    self.assertIn(needle, text)

    def test_docs_never_name_plain_orca_cask(self):
        for doc in self.DOCS:
            with self.subTest(doc=doc.name):
                self.assertIsNone(PLAIN_ORCA_CASK.search(doc.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
