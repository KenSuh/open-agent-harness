# oah init 명령 검사 — 실제 명령 찾기로 임시 git 저장소에서 가짜 도구와 함께 돌려, 계획 먼저·항상 만드는 것·질문·도구와 로그인·영향 지도·멱등·훅·덮어쓰기 금지·비대화형 거부를 본다.
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import secrets as token_source
import subprocess
import sys
import unittest
from unittest import mock

from oah import init, init_cli, unlazy
from oah.cli import build_parser, command_modules, main
from oah.impact import mapstore
from tests.helpers import directory_alias, git_commit, git_env, make_git_repo, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
TODAY = dt.date(2026, 10, 25)
ALL_TOOLS = ("git", "uv", "node", "npm", "npx", "claude", "codex", "graphify", "gh", "gitleaks", "jq", "rg", "brew")
STATUS_MARKER = "STATUS-OUTPUT-MARKER-7f3a"  # what a sign-in status command printed; init must never show it


class WikiStateBoundaryTest(unittest.TestCase):
    def test_in_worktree_state_override_refuses_during_planning(self):
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                repo = committed_repo(self)
                tools = FakeTools()
                machine = make_machine(self, tools, env={"OAH_STATE_DIR": str(repo / "unplanned-state")})
                before = snapshot(repo)
                code, out, err = run_init(repo, machine, *flags(), *(["--dry-run"] if dry_run else []))
                self.assertEqual(code, init.EXIT_ENVIRONMENT)
                self.assertIn("OAH_INIT_STATE_UNSAFE", err)
                self.assertEqual(snapshot(repo), before)
                self.assertEqual(tools.installs, [])
                self.assertEqual(tools.status_calls, [])
                self.assertNotIn("End of plan.", out)


class Replies:
    """A scripted `ask`: the next reply (None once they run out, like end of input), recording each prompt."""

    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str | None:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


class FakeTools:
    """Fake programs: `which`, the install runner, the sign-in status runner and the companion runner/probe."""

    def __init__(self, present=ALL_TOOLS, signed_out: dict | None = None) -> None:
        self.present = set(present)
        self.signed_out = dict(signed_out or {})  # tool -> how many status checks still report "signed out"
        self.installs: list[tuple] = []
        self.status_calls: list[tuple] = []
        self.companion_runs: list[tuple] = []

    def which(self, name, *args, **kwargs):
        return f"/fake/bin/{name}" if name in self.present else None

    def install(self, argv) -> int:
        self.installs.append(tuple(argv))
        self.present.add(argv[-1].rsplit("/", 1)[-1])
        return 0

    def status(self, argv):
        self.status_calls.append(tuple(argv))
        if self.signed_out.get(argv[0], 0) > 0:
            self.signed_out[argv[0]] -= 1
            return 1, f"Not logged in {STATUS_MARKER}"
        return 0, json.dumps({"loggedIn": True, "note": STATUS_MARKER})

    def companion_run(self, argv, cwd) -> int:
        self.companion_runs.append(tuple(argv))
        return 0

    def probe(self, argv, cwd):
        return 1


def fake_unlazy(test: unittest.TestCase) -> tuple[str, unlazy.Pin]:
    """A local stand-in for unlazy's repository and the pin that matches it (no network)."""
    files = {"scripts/gate-check.mjs": "// gate check\n", "scripts/lib/gates.mjs": "// gates\n"}
    source = make_git_repo(test)
    commit = git_commit(source, files, "unlazy")
    return str(source), unlazy.Pin(commit=commit, files={rel: unlazy.file_digest(source / rel) for rel in files})


def make_machine(test: unittest.TestCase, tools: FakeTools, *, interactive: bool = False, replies=(),
                 env: dict | None = None) -> init.Machine:
    if not hasattr(test, "fake_unlazy"):
        test.fake_unlazy = fake_unlazy(test)  # one upstream per test, so a second run sees the same pin
    source, pin = test.fake_unlazy
    home = make_tmp_dir(test) / "home"
    home.mkdir()
    return init.Machine(which=tools.which, install_runner=tools.install, status_runner=tools.status,
                        refresh=lambda: None, companion_runner=tools.companion_run, companion_probe=tools.probe,
                        ask=Replies(*replies), interactive=interactive, env={} if env is None else env, home=home,
                        os_name="macos", companion_os="macos", today=TODAY, unlazy_source=source, unlazy_pin=pin)


def flags(**chosen: bool) -> list[str]:
    """--with-<x> for every question named True here, --without-<x> for all the others."""
    return [question.flag_yes if chosen.get(question.key) else question.flag_no for question in init.QUESTIONS]


def run_init(cwd: pathlib.Path, machine: init.Machine, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.chdir(cwd), mock.patch.object(init_cli, "make_machine", return_value=machine), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["init", *args])
    return code, out.getvalue(), err.getvalue()


def snapshot(root: pathlib.Path, *, with_git: bool = True) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and (with_git or ".git" not in path.relative_to(root).parts)}


def plan_block(text: str) -> str:
    text = text.replace("\r\n", "\n")  # Native Windows console output carries CRLF even through a pipe.
    start = text.index("\nPlan for ")
    return text[start:text.index("\nEnd of plan.", start)]


def last_line(text: str) -> str:
    return text.strip().splitlines()[-1]


def committed_repo(test: unittest.TestCase) -> pathlib.Path:
    repo = make_git_repo(test)
    git_commit(repo, {"README.md": "hello\n", "src/app.py": "print('app')\n"}, "first")
    return repo


class DiscoveryTest(unittest.TestCase):
    def test_init_is_discovered(self):
        self.assertIn("oah.init_cli", [module.__name__ for module in command_modules()])
        args = build_parser().parse_args(["init", "--lang", "ko", "--with-agents", "--without-hooks", "--yes",
                                          "--dry-run", "--force"])
        self.assertEqual((args.command, args.lang, args.agents, args.hooks, args.graph, args.yes, args.dry_run,
                          args.force), ("init", "ko", True, False, None, True, True, True))

    def test_with_and_without_conflict(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["init", "--with-ci", "--without-ci"])
        self.assertEqual(caught.exception.code, 2)


class PlanFirstTest(unittest.TestCase):
    def test_foreign_hook_pat_is_hidden_before_force_confirmation(self):
        repo = committed_repo(self)
        credential = "gl" + "pat-" + token_source.token_hex(10)
        original = ("#!/bin/sh\nexport GITLAB_PAT=" + credential + "\n").encode("ascii")
        (repo / ".git/hooks/pre-push").write_bytes(original)
        machine = make_machine(self, FakeTools(), interactive=True, replies=("n",))
        code, out, err = run_init(repo, machine, "--force", *flags(hooks=True))
        self.assertEqual(code, 1)
        self.assertFalse(credential in out + err, "force preview exposed a provider credential")
        self.assertIn(init._replacement_note(original), out)
        self.assertEqual((repo / ".git/hooks/pre-push").read_bytes(), original)

    """AC1: the full plan comes first; --dry-run prints it and writes, installs and runs nothing."""

    def _assert_internal_rules_link_cli(self):
        repo = committed_repo(self)
        (repo / "AGENTS.md").write_bytes(b"# shared guidance\n")
        (repo / "CLAUDE.md").symlink_to("AGENTS.md")
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 0, out + err)
        self.assertIn("CLAUDE.md -> AGENTS.md", plan_block(out))
        self.assertTrue((repo / "CLAUDE.md").is_symlink())
        before = snapshot(repo)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 0, out + err)
        self.assertIn("OAH_INIT_UNCHANGED impact=fresh", out)
        self.assertEqual(snapshot(repo), before)

    @unittest.skipIf(os.name == "nt", "POSIX file symlinks")
    def test_internal_rules_link_plan_and_second_run(self):
        self._assert_internal_rules_link_cli()

    @unittest.skipUnless(os.name == "nt", "Windows file symlinks (CI developer mode)")
    def test_windows_internal_rules_link_plan_and_second_run(self):
        self._assert_internal_rules_link_cli()

    def test_existing_text_diff_precedes_force_confirmation_and_is_not_stored(self):
        repo = committed_repo(self)
        # Keep both sides free of long token-alphabet runs (J″ also catches long paths).
        relative = "docs/wiki/.obsidian/app.json"
        target = repo / relative
        target.parent.mkdir(parents=True)
        original = b'{\r\n  "myOption": true\r\n}\r\n'
        target.write_bytes(original)
        before = snapshot(repo)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--dry-run", "--force",
                                  *flags(graph=True))
        self.assertEqual(code, 0, err)
        self.assertIn("--- a/" + relative, out)
        self.assertIn("+++ b/" + relative, out)
        self.assertIn('-  "myOption": true', out)
        self.assertEqual(snapshot(repo), before)
        self.assertFalse((repo / ".git/oah").exists())

        machine = make_machine(self, FakeTools(), interactive=True)
        observed = []

        def decline(prompt):
            observed.append((prompt, sys.stdout.getvalue()))
            return "n"

        machine.ask = decline
        code, out, err = run_init(repo, machine, "--force", *flags(graph=True))
        self.assertEqual(code, 1, out + err)
        self.assertIn("Apply this plan?", observed[0][0])
        self.assertIn('-  "myOption": true', observed[0][1])
        self.assertEqual(snapshot(repo), before)

        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--force", *flags(graph=True))
        self.assertEqual(code, 0, out + err)
        for name, data in snapshot(repo / ".git/oah").items():
            self.assertNotIn(b'"myOption"', data, name)
            self.assertNotIn(("--- a/" + relative).encode("ascii"), data, name)

    def test_terminal_diff_falls_back_for_credentials_binary_and_invalid_utf8(self):
        fixtures = (b"# API_KEY=" + ("fake-" + token_source.token_hex(20)).encode("ascii") + b"\n",
                    b"# Authorization: Bearer " + ("fake-" + token_source.token_hex(20)).encode("ascii") + b"\n",
                    json.dumps({"Authorization": "fake-" + token_source.token_hex(20)}).encode("ascii"),
                    json.dumps({"cookie": "fake-" + token_source.token_hex(20)}).encode("ascii"),
                    b"private-binary\x00payload\n", b"private-undecodable\xff\n")
        for original in fixtures:
            with self.subTest(kind=fixtures.index(original)):
                repo = committed_repo(self)
                (repo / ".graphifyignore").write_bytes(original)
                before = snapshot(repo)
                code, out, err = run_init(repo, make_machine(self, FakeTools()), "--dry-run", "--force",
                                          *flags(graph=True))
                self.assertEqual(code, 0, err)
                self.assertIn(init._replacement_note(original), out)
                self.assertNotIn(original.decode("utf-8", errors="replace").strip(), out + err)
                self.assertNotIn("--- a/.graphifyignore", out)
                self.assertEqual(snapshot(repo), before)

    def test_terminal_diffs_cover_managed_rules_hooks_and_ci(self):
        repo = committed_repo(self)
        (repo / "AGENTS.md").write_bytes(b"# project guidance\n")
        (repo / ".git/hooks/pre-push").write_bytes(b"#!/bin/sh\n# custom push check\n")
        workflow = repo / ".github/workflows/oah-ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_bytes(b"# custom workflow\n")
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--dry-run", "--force",
                                  *flags(hooks=True, ci=True))
        self.assertEqual(code, 0, err)
        for path in ("AGENTS.md", ".git/hooks/pre-push"):
            self.assertIn("--- a/" + path, out)
        self.assertIn("-# custom push check", out)
        # CI's pinned SHA values trigger J″ even when the existing comment is safe.
        self.assertIn(init._replacement_note(workflow.read_bytes()), out)
        self.assertNotIn("--- a/.github/workflows/oah-ci.yml", out)
        self.assertNotIn("-# custom workflow", out)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--dry-run", *flags(hooks=True, ci=True))
        self.assertEqual(code, 0, err)
        self.assertIn("--- a/.git/hooks/pre-push", out)
        self.assertIn("-# custom push check", out)

    def test_dry_run_refuses_project_redirection_during_discovery(self):
        repo = committed_repo(self)
        tools = FakeTools()
        machine = make_machine(self, tools)
        source = pathlib.Path(machine.unlazy_source)
        candidate = repo / init.UNLAZY_PROJECT
        candidate.parent.mkdir(parents=True)
        linked = init._linked
        redirected = False

        def redirect(root, name):
            nonlocal redirected
            result = linked(root, name)
            if not redirected and name == ".claude/skills/unlazy":
                redirected = True
                directory_alias(source, candidate)
            return result

        before = snapshot(source)
        with mock.patch.object(init, "_linked", side_effect=redirect):
            code, out, err = run_init(repo, machine, "--dry-run", *flags(unlazy=True))
        self.assertEqual(code, 69, out + err)
        self.assertIn("OAH_INIT_PATH_UNSAFE", err)
        self.assertNotIn(str(source), out + err)
        self.assertEqual(snapshot(source), before)
        self.assertFalse((repo / ".git/oah").exists())
        self.assertEqual((tools.installs, tools.status_calls, machine.ask.prompts), ([], [], []))

    def test_dry_run_prints_plan_and_changes_nothing(self):
        repo = committed_repo(self)
        tools = FakeTools(present=set(ALL_TOOLS) - {"gh"})
        machine = make_machine(self, tools)
        before = snapshot(repo)
        code, out, err = run_init(repo, machine, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(snapshot(repo), before)
        self.assertFalse((repo / ".git" / "oah").exists())
        self.assertEqual((tools.installs, tools.status_calls, machine.ask.prompts), ([], [], []))
        plan = plan_block(out)
        for words in ("brew install gh", "claude auth status", "codex login status", ".oah/config.toml",
                      "docs/wiki/log.md", "AGENTS.md", "CLAUDE.md", ".gitignore", "oah hook stop",
                      "oah hook session-start", "Impact map", "build"):
            self.assertIn(words, plan, words)
        self.assertTrue(last_line(out).startswith("OAH_INIT_DRY_RUN changes="), out)

    def test_dry_run_plan_equals_real_plan(self):
        repo = committed_repo(self)
        chosen = flags(agents=True, hooks=True, graph=True, skill_updates=True)
        code, dry, err = run_init(repo, make_machine(self, FakeTools()), "--dry-run", *chosen)
        self.assertEqual(code, 0, err)
        code, real, err = run_init(repo, make_machine(self, FakeTools()), *chosen)
        self.assertEqual(code, 0, err)
        self.assertEqual(plan_block(dry), plan_block(real))
        self.assertLess(real.index("End of plan."), real.index("created .oah/config.toml"))

    def test_dry_run_hook_plan_never_discloses_settings_secrets(self):
        repo = committed_repo(self)
        secret = "fake-" + token_source.token_hex(20)
        path = repo / init.CLAUDE_SETTINGS
        path.parent.mkdir()
        path.write_bytes(json.dumps({"env": {"ANTHROPIC_API_KEY": secret}}).encode("utf-8"))
        before = snapshot(repo)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--dry-run", *flags(hooks=True))
        self.assertEqual(code, 0, err)
        self.assertNotIn(secret, out + err)
        self.assertNotIn("ANTHROPIC_API_KEY", out + err)
        self.assertEqual(snapshot(repo), before)

    def test_forced_dry_run_never_discloses_foreign_git_hook(self):
        repo = committed_repo(self)
        secret = "fake-" + token_source.token_hex(20)
        path = repo / ".git/hooks/pre-push"
        path.write_bytes(("#!/bin/sh\n# Authorization: Bearer " + secret + "\n").encode("utf-8"))
        before = snapshot(repo)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--force", "--dry-run", *flags(hooks=True))
        self.assertEqual(code, 0, err)
        self.assertNotIn(secret, out + err)
        self.assertIn("replaces", out)
        self.assertIn("sha256", out)
        self.assertIn("oah hook pre-push", out)
        self.assertEqual(snapshot(repo), before)

    def test_existing_config_diagnostics_do_not_echo_private_keys(self):
        repo = committed_repo(self)
        secret = "fake-" + token_source.token_hex(20)
        path = repo / init.CONFIG_PATH
        path.parent.mkdir()
        path.write_bytes(("[wiki]\n" + json.dumps(secret) + " = true\n").encode("utf-8"))
        before = snapshot(repo)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--dry-run", *flags())
        self.assertEqual(code, 69)
        self.assertNotIn(secret, out + err)
        self.assertIn("OAH_CONFIG_UNKNOWN_KEY", err)
        self.assertEqual(snapshot(repo), before)

    def test_existing_wiki_dir_cannot_escape_checkout(self):
        repo = committed_repo(self)
        outside = make_tmp_dir(self)
        index = outside / "index.md"
        index.write_bytes(b"# Index\r\n\r\n## Notes\r\n")
        config = init.render_config(branch="main", how="", jev=False, skill_updates=False, today=TODAY)
        config += "\n[wiki]\ndir = " + json.dumps(str(outside)) + "\n"
        (repo / ".oah").mkdir()
        (repo / init.CONFIG_PATH).write_bytes(config.encode("utf-8"))
        before = snapshot(repo)
        for extra in ((), ("--dry-run",)):
            code, out, err = run_init(repo, make_machine(self, FakeTools()), *extra, *flags())
            self.assertEqual(code, 69, out + err)
            self.assertIn("OAH_INIT_PATH_UNSAFE", err)
            self.assertEqual(index.read_bytes(), b"# Index\r\n\r\n## Notes\r\n")
            self.assertFalse((outside / "wiki-guide.md").exists())
            self.assertEqual(snapshot(repo), before)

    def test_windows_aside_plan_shows_every_installer_line_and_source(self):
        repo = committed_repo(self)
        machine = make_machine(self, FakeTools())
        machine.companion_os = machine.os_name = "windows"
        code, out, err = run_init(repo, machine, "--dry-run", "--yes", *flags(aside=True))
        self.assertEqual(code, 0, err)
        from oah.setup import companions
        installer = companions.plan_steps(["aside"], os_name="windows", project_root=repo, remote_url=None)[0]
        plan = plan_block(out)
        for line in installer.display.splitlines():
            self.assertIn(line, plan)
        self.assertIn(installer.source, plan)
        self.assertEqual(machine.ask.prompts, [])


class DryRunSubprocessTest(unittest.TestCase):
    def test_plan_block_compares_the_same_text_with_native_console_newlines(self):
        text = "header\n\nPlan for repo:\nFiles:\n  create     .oah/config.toml\nEnd of plan.\n"
        self.assertEqual(plan_block(text.replace("\n", "\r\n")), plan_block(text))

    def test_python_m_oah_init_dry_run(self):
        repo = committed_repo(self)
        before = snapshot(repo)
        env = {key: value for key, value in os.environ.items() if key not in ("OAH_STATE_DIR", "OAH_WORKER")}
        env.update(PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-m", "oah", "init", "--dry-run"], cwd=repo, env=env,
                                capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        out = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        plan = plan_block(out)
        for section in ("\nTools (", "\nSign-ins (", "\nFiles:\n  create     .oah/config.toml",
                        "\nManaged content", "AGENTS.md: the oah rules block", "\nHooks:",
                        "\nImpact map:\n  build the first map"):
            self.assertIn(section, plan, section)
        self.assertTrue(last_line(out).startswith("OAH_INIT_DRY_RUN changes="), out)
        self.assertEqual(snapshot(repo), before)


class AlwaysCreatedTest(unittest.TestCase):
    """AC2: config with the owner's defaults, templates, rules block, @AGENTS.md, the wiki, .gitignore entries."""

    def test_fresh_repository_gets_the_environment(self):
        repo = committed_repo(self)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags(skill_updates=True))
        self.assertEqual(code, 0, err + out)
        config = (repo / ".oah" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('initial = "claude"', config)
        self.assertIn('default_branch = "main"', config)
        for name in ("PACKET.md", "GATES.md", "README.md"):
            self.assertTrue((repo / ".oah" / "templates" / name).is_file(), name)
        self.assertIn("<!-- OAH:RULES:BEGIN v1 -->", (repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("@AGENTS.md", (repo / "CLAUDE.md").read_text(encoding="utf-8").splitlines())
        for name in ("index.md", "log.md", "wiki-guide.md", "templates/note.md", "templates/decision.md",
                     ".obsidian/app.json"):
            self.assertTrue((repo / "docs" / "wiki" / name).is_file(), name)
        for path in (".oah/tasks/t1/GATES.md", "docs/wiki/.obsidian/workspace.json", ".env", "certs/server.key",
                     ".claude/settings.local.json"):
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=repo, env=git_env())
            self.assertEqual(result.returncode, 0, f"{path} should be ignored")
        result = subprocess.run(["git", "check-ignore", "-q", ".oah/tasks/t1/PACKET.md"], cwd=repo, env=git_env())
        self.assertEqual(result.returncode, 1)
        self.assertTrue(last_line(out).startswith("OAH_INIT_DONE"), out)


class QuestionsTest(unittest.TestCase):
    """AC3: one question per option; flags answer without asking; Jev needs explicit consent."""

    def test_all_flags_complete_without_prompting(self):
        repo = committed_repo(self)
        machine = make_machine(self, FakeTools())
        code, out, err = run_init(repo, machine, *flags(agents=True, graph=True))
        self.assertEqual(code, 0, err)
        self.assertEqual(machine.ask.prompts, [])
        self.assertTrue((repo / ".claude" / "agents" / "oah-max.md").is_file())
        self.assertTrue((repo / ".graphifyignore").is_file())
        self.assertFalse((repo / ".claude" / "settings.local.json").exists())

    def test_interactive_answers_shape_the_environment(self):
        repo = committed_repo(self)
        # agents, hooks, unlazy, graph, ci, orca, aside, jev, skill updates; then "Apply this plan?"
        replies = ("n", "n", "n", "y", "", "", "", "n", "", "y")
        machine = make_machine(self, FakeTools(), interactive=True, replies=replies)
        code, out, err = run_init(repo, machine)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(machine.ask.prompts), 10)
        self.assertFalse((repo / ".claude" / "agents").exists())
        self.assertTrue((repo / ".graphifyignore").is_file())
        self.assertIn("enabled = false", (repo / ".oah" / "config.toml").read_text(encoding="utf-8"))

    def test_declining_the_plan_changes_nothing(self):
        repo = committed_repo(self)
        before = snapshot(repo)
        machine = make_machine(self, FakeTools(), interactive=True, replies=("n",))
        code, out, err = run_init(repo, machine, *flags())
        self.assertEqual(code, 1)
        self.assertIn("OAH_INIT_DECLINED", err)
        self.assertEqual(snapshot(repo), before)

    def test_key_alone_never_enables_jev(self):
        repo = committed_repo(self)
        key = "fake-" + token_source.token_hex(16)
        env = {}
        env["TYPESAFE_API_KEY"] = key
        with mock.patch("oah.secrets.get", side_effect=AssertionError("init must never look a key up")):
            code, out, err = run_init(repo, make_machine(self, FakeTools(), env=env), *flags(skill_updates=True))
        self.assertEqual(code, 0, err)
        config = (repo / ".oah" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[rerank]\nenabled = false", config)
        self.assertNotIn(key, out + err)
        self.assertFalse([path for path, data in snapshot(repo).items() if key.encode() in data])

    def test_consent_turns_jev_on_and_is_recorded(self):
        repo = committed_repo(self)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags(jev=True))
        self.assertEqual(code, 0, err)
        config = (repo / ".oah" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('[rerank]\nenabled = true\nprovider = "typesafe"', config)
        self.assertIn("2026-10-25", config)

    def test_noninteractive_without_flags_refuses(self):
        repo = committed_repo(self)
        before = snapshot(repo)
        tools = FakeTools(present=set(ALL_TOOLS) - {"gh"})
        machine = make_machine(self, tools)
        for extra in ((), ("--yes",)):
            with self.subTest(extra=extra):
                code, out, err = run_init(repo, machine, *extra, "--with-agents")
                self.assertEqual(code, 2)
                self.assertIn("OAH_INIT_ANSWERS_REQUIRED", err)
                self.assertIn("--with-hooks or --without-hooks", err)
                self.assertNotIn("--with-agents or", err)
                self.assertEqual(snapshot(repo), before)
                self.assertEqual((tools.installs, tools.status_calls, machine.ask.prompts), ([], [], []))


class ToolsAndSignInTest(unittest.TestCase):
    """AC4: tools and sign-ins go through oah.setup; only confirmed tools install; no secret is seen."""

    def test_installs_only_confirmed_tools(self):
        repo = committed_repo(self)
        tools = FakeTools(present=set(ALL_TOOLS) - {"gh", "jq"})
        replies = ("y", "y", "n")  # apply the plan; install gh; not jq
        machine = make_machine(self, tools, interactive=True, replies=replies)
        code, out, err = run_init(repo, machine, *flags(skill_updates=True))
        self.assertEqual(code, 0, err)
        self.assertEqual(tools.installs, [("brew", "install", "gh")])

    def test_yes_installs_missing_tools_but_not_unselected_apps(self):
        repo = committed_repo(self)
        tools = FakeTools(present=set(ALL_TOOLS) - {"gh", "jq"})
        code, out, err = run_init(repo, make_machine(self, tools), "--yes", *flags())
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(tools.installs), [("brew", "install", "gh"), ("brew", "install", "jq")])

    def test_selected_app_installs_and_its_steps_wait_for_consent(self):
        repo = committed_repo(self)
        tools = FakeTools()
        code, out, err = run_init(repo, make_machine(self, tools), *flags(orca=True))
        self.assertEqual(tools.installs, [("brew", "install", "--cask", "stablyai/orca/orca")])
        self.assertEqual(tools.companion_runs, [])  # no terminal and no --yes: each step stays pending
        self.assertEqual(code, 1)
        self.assertIn("OAH_INIT_INCOMPLETE", last_line(out))
        self.assertIn("orca", last_line(out))

    def test_yes_does_not_consent_to_companion_global_steps(self):
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                repo = committed_repo(self)
                tools = FakeTools(present=(*ALL_TOOLS, "aside"))
                def probe(argv, cwd):
                    return 1 if "mcp" in argv else 0
                machine = make_machine(self, tools, interactive=interactive, replies=("y", "n", "n"))
                machine.companion_probe = probe
                code, out, err = run_init(repo, machine, "--yes", *flags(aside=True))
                self.assertEqual(tools.companion_runs, [])
                self.assertEqual(code, 1, err + out)
                if interactive:
                    self.assertEqual(machine.ask.prompts.count("  Run it now? [y/N] "), 2)

    def test_companion_global_steps_still_run_with_explicit_consent(self):
        repo = committed_repo(self)
        tools = FakeTools(present=(*ALL_TOOLS, "aside"))
        machine = make_machine(self, tools, interactive=True, replies=("y", "y", "y"))
        machine.companion_probe = lambda argv, cwd: 1 if "mcp" in argv else 0
        code, out, err = run_init(repo, machine, "--yes", *flags(aside=True))
        self.assertEqual(code, 0, err + out)
        self.assertEqual(tools.companion_runs, [
            ("claude", "mcp", "add", "--scope", "user", "--transport", "stdio", "aside", "--", "aside", "mcp"),
            ("codex", "mcp", "add", "aside", "--", "aside", "mcp")])

    def test_repeated_orca_setup_checks_presence_before_mutation(self):
        repo = committed_repo(self)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/sample.git"],
                       cwd=repo, check=True, env=git_env())
        tools = FakeTools(present=(*ALL_TOOLS, "orca"))
        satisfied = set()
        def probe(argv, cwd):
            if tuple(argv[:3]) in (("orca", "skills", "installed"), ("orca", "project", "setups")):
                return 0 if argv[1] in satisfied else 1
            return 0
        def run(argv, cwd):
            tools.companion_runs.append(tuple(argv))
            satisfied.add(argv[1])
            return 0
        machine = make_machine(self, tools, interactive=True, replies=("y", "y", "y", "y"))
        machine.companion_probe = probe
        machine.companion_runner = run
        code, out, err = run_init(repo, machine, *flags(orca=True))
        self.assertEqual(code, 0, err + out)
        self.assertEqual(len(tools.companion_runs), 2)
        before = snapshot(repo)
        code, out, err = run_init(repo, machine, *flags(orca=True))
        self.assertEqual(code, 0, err + out)
        self.assertEqual(len(tools.companion_runs), 2)
        self.assertEqual(snapshot(repo), before)
        self.assertEqual(last_line(out), "OAH_INIT_UNCHANGED impact=fresh")

    def test_unverified_orca_presence_never_triggers_mutation(self):
        repo = committed_repo(self)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/sample.git"],
                       cwd=repo, check=True, env=git_env())
        tools = FakeTools(present=(*ALL_TOOLS, "orca"))
        machine = make_machine(self, tools, interactive=True, replies=("y", "y", "y"))
        machine.companion_probe = lambda argv, cwd: 0 if "--version" in argv else 2
        code, out, err = run_init(repo, machine, "--yes", *flags(orca=True))
        self.assertEqual(code, 1, err + out)
        self.assertIn("OAH_INIT_ORCA_UNVERIFIED", out)
        self.assertEqual(tools.companion_runs, [])

    def test_sign_in_walk_uses_status_commands(self):
        repo = committed_repo(self)
        tools = FakeTools(signed_out={"claude": 1})
        replies = ("y", "")  # apply the plan; press Enter after signing in
        code, out, err = run_init(repo, make_machine(self, tools, interactive=True, replies=replies), *flags())
        self.assertEqual(code, 0, err)
        self.assertIn("claude auth login", out)
        self.assertIn(("claude", "auth", "status"), tools.status_calls)
        self.assertNotIn(STATUS_MARKER, out + err)

    def test_signed_out_without_terminal_is_incomplete(self):
        repo = committed_repo(self)
        tools = FakeTools(signed_out={"codex": 9})
        code, out, err = run_init(repo, make_machine(self, tools), *flags())
        self.assertEqual(code, 1)
        self.assertIn("codex login", out)
        self.assertIn("pending=codex:sign-in", last_line(out))
        self.assertTrue((repo / ".oah" / "config.toml").is_file())
        self.assertNotIn(STATUS_MARKER, out + err)


class ImpactTest(unittest.TestCase):
    """AC5: the first persisted map is built after scaffolding; a failure is reported, never fatal."""

    def test_map_built_after_scaffolding(self):
        repo = committed_repo(self)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags(agents=True))
        self.assertEqual(code, 0, err)
        self.assertIn("impact=built", last_line(out))
        info, reason = mapstore.status(repo, env={})
        self.assertEqual((info.action, reason), ("fresh", "fresh"))

    def test_no_commit_repository_marks_map_unavailable(self):
        repo = make_git_repo(self)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 0, err)
        self.assertIn("impact=unavailable", last_line(out))
        self.assertIn("OAH_IMPACT_NO_COMMIT", out + err)
        self.assertIn("oah impact build", out + err)
        self.assertTrue((repo / ".oah" / "config.toml").is_file())
        git_commit(repo, {"README.md": "hello\n"}, "first")
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 0, err)
        self.assertIn("changed=0 impact=built", last_line(out))


class IdempotentTest(unittest.TestCase):
    def test_unlazy_directory_alias_init_is_complete_and_idempotent(self):
        repo = committed_repo(self)
        target = repo / "local-agents"
        target.mkdir()
        directory_alias(target, repo / ".agents")
        machine = make_machine(self, FakeTools())
        code, out, err = run_init(repo, machine, *flags(unlazy=True))
        self.assertEqual(code, 0, out + err)
        self.assertIn(".agents/skills/unlazy -> local-agents/skills/unlazy", out)
        self.assertEqual(unlazy.verify_pin(target / "skills/unlazy", machine.unlazy_pin), [])
        self.assertEqual(subprocess.run(["git", "check-ignore", "-q", "--", "local-agents/skills/unlazy/"],
                                        cwd=repo).returncode, 0)
        before = snapshot(repo)
        code, out, err = run_init(repo, machine, *flags(unlazy=True))
        self.assertEqual(code, 0, out + err)
        self.assertIn("OAH_INIT_UNCHANGED impact=fresh", out)
        self.assertEqual(snapshot(repo), before)

    """AC6: a second run changes nothing; after a commit it only refreshes the map."""

    def chosen(self) -> list[str]:
        return flags(agents=True, hooks=True, unlazy=True, graph=True, ci=True, skill_updates=True)

    def test_second_run_changes_nothing(self):
        repo = committed_repo(self)
        (repo / "go.mod").write_text("module x\n", encoding="utf-8")
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *self.chosen())
        self.assertEqual(code, 0, err + out)
        before = snapshot(repo)
        machine = make_machine(self, FakeTools())
        code, out, err = run_init(repo, machine, *self.chosen())
        self.assertEqual(code, 0, err)
        self.assertEqual(last_line(out), "OAH_INIT_UNCHANGED impact=fresh")
        self.assertIn("Nothing to change", out)
        self.assertEqual(snapshot(repo), before)

    def test_second_run_after_commit_only_refreshes_map(self):
        repo = committed_repo(self)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *self.chosen())
        self.assertEqual(code, 0, err + out)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=git_env())
        no_hooks = make_tmp_dir(self)  # the hooks init installed would call `oah`, which is not on this PATH
        subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={no_hooks}", "commit", "-q", "-m",
                        "oah init"], cwd=repo, check=True, env=git_env())
        before = snapshot(repo, with_git=False)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *self.chosen())
        self.assertEqual(code, 0, err)
        self.assertEqual(last_line(out), "OAH_INIT_DONE changed=0 impact=refreshed")
        self.assertEqual(snapshot(repo, with_git=False), before)
        self.assertEqual(mapstore.status(repo, env={})[0].action, "fresh")


class HooksTest(unittest.TestCase):
    """AC7: --with-hooks offers the SessionStart hook with the others, after showing the diff."""

    def test_with_hooks_shows_diff_then_installs_session_start(self):
        repo = committed_repo(self)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags(hooks=True))
        self.assertEqual(code, 0, err)
        diff_at = out.index('"command": "oah hook session-start"')
        self.assertLess(diff_at, out.index("End of plan."))
        self.assertLess(out.index("End of plan."), out.index("installed hook session-start"))
        settings = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        commands = [hook["command"] for group in settings["hooks"]["SessionStart"] for hook in group["hooks"]]
        self.assertEqual(commands, ["oah hook session-start"])
        pre_push = repo / ".git" / "hooks" / "pre-push"
        self.assertIn("oah hook pre-push", pre_push.read_text(encoding="utf-8"))


class NoOverwriteTest(unittest.TestCase):
    def test_rules_preserve_an_edit_made_after_the_plan(self):
        repo = committed_repo(self)
        path = repo / "AGENTS.md"
        path.write_bytes(b"# Before\r\n")
        tools = FakeTools()
        machine = make_machine(self, tools)
        status = machine.status_runner

        def edit(argv):
            path.write_bytes(b"# Concurrent edit\r\n")
            return status(argv)

        machine.status_runner = edit
        code, out, err = run_init(repo, machine, *flags())
        self.assertEqual(code, 1, out + err)
        self.assertIn("OAH_INIT_CHANGED", out)
        self.assertEqual(path.read_bytes(), b"# Concurrent edit\r\n")

    """Existing files keep their user content; only managed blocks and missing entries change."""

    def test_existing_files_keep_user_content(self):
        repo = committed_repo(self)
        originals = {
            "AGENTS.md": b"# Team rules\r\n\r\nUse tabs.\r\n",
            "CLAUDE.md": b"# Claude notes\r\n",
            ".gitignore": b"node_modules/\r\n.env\r\n",
            "docs/wiki/index.md": b"Our catalog\r\n\r\nNotes:\r\n- [[design]]\r\n",
            ".oah/config.toml": b"[stages]\r\ninitial = \"claude\"\r\n",
        }
        for name, data in originals.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_bytes(data)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 0, err)
        for name, data in originals.items():
            current = (repo / name).read_bytes()
            self.assertTrue(current.startswith(data), name)
            self.assertEqual(current.count(b"\n"), current.count(b"\r\n"), name)
        self.assertEqual((repo / ".oah" / "config.toml").read_bytes(), originals[".oah/config.toml"])
        self.assertIn(b"<!-- OAH:RULES:BEGIN v1 -->\r\n", (repo / "AGENTS.md").read_bytes())
        self.assertTrue((repo / "CLAUDE.md").read_bytes().endswith(b"@AGENTS.md\r\n"))
        gitignore = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(gitignore.count(".env"), 1)
        self.assertIn("[[wiki-guide|How this wiki works]]", (repo / "docs/wiki/index.md").read_text(encoding="utf-8"))
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--force", *flags())
        self.assertEqual(code, 0, err)
        self.assertIn('review = "codex"', (repo / ".oah" / "config.toml").read_text(encoding="utf-8"))
        self.assertTrue((repo / "docs/wiki/index.md").read_bytes().startswith(originals["docs/wiki/index.md"]))


class CorrectionC3Test(unittest.TestCase):
    def test_linked_config_refuses_even_with_force_without_displaying_its_bytes(self):
        repo = committed_repo(self)
        outside = make_tmp_dir(self)
        secret = "fake-" + token_source.token_hex(20)
        (outside / "config.toml").write_bytes(('# ' + secret + '\n').encode("ascii"))
        directory_alias(outside, repo / ".oah")
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--force", *flags())
        self.assertEqual(code, 69, out + err)
        self.assertIn("OAH_INIT_PATH_UNSAFE", err)
        self.assertNotIn(secret, out + err)
        self.assertFalse((repo / "AGENTS.md").exists())

    @unittest.skipIf(os.name == "nt", "file symlinks need Windows developer privileges")
    def test_ci_manifest_link_is_not_read_by_init(self):
        repo = committed_repo(self)
        outside = make_tmp_dir(self) / "private.json"
        secret = "fake-" + token_source.token_hex(20)
        outside.write_bytes(json.dumps({"scripts": {"test": "echo " + secret}}).encode("ascii"))
        (repo / "package.json").symlink_to(outside)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags(ci=True))
        self.assertEqual(code, 69, out + err)
        self.assertIn("OAH_INIT_PATH_UNSAFE", err)
        self.assertNotIn(secret, out + err)
        self.assertFalse((repo / ".github/workflows/oah-ci.yml").exists())

    def test_root_negation_in_an_empty_nested_folder_leaves_init_incomplete(self):
        for folder in ("docs", "empty/deeper", "docs/wiki/templates"):
            with self.subTest(folder=folder):
                repo = committed_repo(self)
                if folder != "docs/wiki/templates":  # this directory must be visited after init creates it
                    (repo / folder).mkdir(parents=True)
                original = init.add_gitignore_entries(None, unlazy_copy=False) + (
                    "!" + folder + "/*.key\n").encode("ascii")
                (repo / ".gitignore").write_bytes(original)
                code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
                self.assertEqual(code, 1, out + err)
                self.assertIn("OAH_INIT_IGNORE_UNPROTECTED: required pattern *.key", out)
                self.assertIn("OAH_INIT_INCOMPLETE", last_line(out))
                self.assertEqual((repo / ".gitignore").read_bytes(), original)
                self.assertTrue((repo / folder).is_dir())
                self.assertFalse(list((repo / folder).glob("*.key")))

    def test_required_ignore_negations_leave_init_incomplete(self):
        patterns = [entry for _comment, entries in init._gitignore_groups(False) for entry in entries]
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                repo = committed_repo(self)
                original = (init.add_gitignore_entries(None, unlazy_copy=False) +
                            ("!" + pattern + "\n").encode("utf-8"))
                (repo / ".gitignore").write_bytes(original)
                code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
                self.assertEqual(code, 1, out + err)
                self.assertIn("OAH_INIT_IGNORE_UNPROTECTED", out)
                self.assertIn(pattern, out)
                self.assertIn("OAH_INIT_INCOMPLETE", last_line(out))
                self.assertEqual((repo / ".gitignore").read_bytes(), original)

    def test_nested_ignore_negations_leave_init_incomplete(self):
        cases = (("nested/.gitignore", b"!.env\n", ".env"),
                 ("nested/.gitignore", b"!*.key\n", "*.key"),
                 (".oah/tasks/real/.gitignore", b"!GATES.md\n", ".oah/tasks/*/GATES.md"),
                 ("docs/wiki/.obsidian/.gitignore", b"!workspace*.json\n", "**/.obsidian/workspace*.json"))
        for name, data, pattern in cases:
            with self.subTest(name=name, pattern=pattern):
                repo = committed_repo(self)
                path = repo / name
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
                code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
                self.assertEqual(code, 1, out + err)
                self.assertIn("OAH_INIT_IGNORE_UNPROTECTED", out)
                self.assertIn(pattern, out)
                self.assertIn("OAH_INIT_INCOMPLETE", last_line(out))
                self.assertEqual(path.read_bytes(), data)

    def test_ignore_verification_uses_rules_even_for_tracked_paths(self):
        repo = committed_repo(self)
        git_commit(repo, {".env": "# harmless tracked fixture\n"})
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("OAH_INIT_IGNORE_UNPROTECTED", out)

    def test_ignore_verification_stays_in_the_parent_repository(self):
        repo = committed_repo(self)
        dependency = repo / "dependency"
        subprocess.run(["git", "init", "-q", str(dependency)], check=True, env=git_env())
        head = git_commit(dependency, {".gitignore": "!.env\n", "fixture.key": "# harmless fixture\n"})
        subprocess.run(["git", "update-index", "--add", "--cacheinfo", "160000", head, "dependency"],
                       cwd=repo, check=True, env=git_env())
        (repo / ".gitignore").write_bytes(init.add_gitignore_entries(None, unlazy_copy=False))
        self.assertEqual(init._ignore_problems(repo, unlazy_copy=False), [])

    def test_a_git_named_file_cannot_hide_a_nested_ignore_conflict(self):
        repo = committed_repo(self)
        folder = repo / "nested"
        folder.mkdir()
        (folder / ".git").write_bytes(b"not a git repository\n")
        (folder / ".gitignore").write_bytes(b"!.env\n")
        (repo / ".gitignore").write_bytes(init.add_gitignore_entries(None, unlazy_copy=False))
        problems = init._ignore_problems(repo, unlazy_copy=False)
        self.assertTrue(any("required pattern .env " in problem for problem in problems), problems)

    def test_existing_secret_name_negation_is_checked_without_reading_its_bytes(self):
        repo = committed_repo(self)
        path = repo / "nested/production.key"
        path.parent.mkdir()
        secret = ("fake-" + token_source.token_hex(20)).encode("utf-8")
        path.write_bytes(secret)
        (repo / ".gitignore").write_bytes(init.add_gitignore_entries(None, unlazy_copy=False) +
                                         b"!nested/production.key\n")
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 1, err + out)
        self.assertIn("OAH_INIT_IGNORE_UNPROTECTED: required pattern *.key", out)
        self.assertNotIn(secret.decode("utf-8"), out + err)
        self.assertEqual(path.read_bytes(), secret)

    def test_ignorecase_secret_negation_leaves_init_incomplete(self):
        repo = committed_repo(self)
        subprocess.run(["git", "config", "core.ignorecase", "true"], cwd=repo, check=True, env=git_env())
        secret = ("fake-" + token_source.token_hex(20)).encode("ascii")
        path = repo / "PRODUCTION.KEY"
        path.write_bytes(secret)
        original = init.add_gitignore_entries(None, unlazy_copy=False) + b"!PRODUCTION.KEY\n"
        (repo / ".gitignore").write_bytes(original)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 1, out + err)
        self.assertIn("OAH_INIT_IGNORE_UNPROTECTED: required pattern *.key", out)
        self.assertIn("OAH_INIT_INCOMPLETE", last_line(out))
        self.assertNotIn(secret.decode("ascii"), out + err)
        self.assertEqual(path.read_bytes(), secret)
        self.assertEqual((repo / ".gitignore").read_bytes(), original)

    def test_existing_ci_command_never_enters_plan_workflow_or_state(self):
        for detected in (False, True):
            with self.subTest(detected=detected):
                repo = committed_repo(self)
                secret = "fake-" + token_source.token_hex(20)
                command = "private-test-runner --password=" + secret
                config = repo / init.CONFIG_PATH
                config.parent.mkdir()
                config.write_bytes(('[ci]\ntest_command = ' + json.dumps(command) + '\n').encode("utf-8"))
                if detected:
                    (repo / "go.mod").write_bytes(b"module example.test/sample\n")
                machine = make_machine(self, FakeTools())
                before = snapshot(repo)
                code, dry, err = run_init(repo, machine, "--dry-run", *flags(ci=True))
                self.assertEqual(code, 0, err)
                self.assertEqual(snapshot(repo), before)
                self.assertNotIn(command, dry + err)
                self.assertNotIn(secret, dry + err)
                code, out, err = run_init(repo, machine, *flags(ci=True))
                self.assertEqual(code, 0, err + out)
                self.assertEqual(plan_block(dry), plan_block(out))
                workflow = (repo / ".github/workflows/oah-ci.yml").read_bytes()
                if detected:
                    self.assertIn(b"go test ./...", workflow)
                else:
                    self.assertIn(b"Run tests (edit me)", workflow)
                for path, data in snapshot(repo).items():
                    if path != init.CONFIG_PATH:
                        self.assertNotIn(secret.encode("utf-8"), data, path)
                self.assertNotIn(secret, out + err)
                self.assertIn(command.encode("utf-8"), config.read_bytes())

    def test_aside_preserves_rules_changed_after_confirmation(self):
        for name in ("AGENTS.md", ".oah/rules.local.md"):
            for existed in (False, True):
                with self.subTest(name=name, existed=existed):
                    repo = committed_repo(self)
                    path = repo / name
                    if existed:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"# Original\r\n")
                    machine = make_machine(self, FakeTools(present=(*ALL_TOOLS, "aside")))
                    machine.companion_probe = lambda argv, cwd: 0
                    status = machine.status_runner

                    def edit(argv):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"# Concurrent edit\r\n")
                        return status(argv)

                    machine.status_runner = edit
                    code, out, err = run_init(repo, machine, *flags(aside=True))
                    self.assertEqual(code, 1, out + err)
                    self.assertIn("OAH_INIT_CHANGED", out)
                    self.assertEqual(path.read_bytes(), b"# Concurrent edit\r\n")
                    self.assertIn("OAH_INIT_INCOMPLETE", last_line(out))

    def test_aside_apply_uses_only_preconfirmed_publications(self):
        repo = committed_repo(self)
        machine = make_machine(self, FakeTools(present=(*ALL_TOOLS, "aside")))
        machine.companion_probe = lambda argv, cwd: 0
        given = {question.key: question.key == "aside" for question in init.QUESTIONS}
        answers = {key: init.Answer(value, "flag") for key, value in given.items()}
        plan = init.build_plan(repo, answers, given=given, machine=machine, lang=None, force=False, existing=None)
        with mock.patch.object(init.rules, "plan", side_effect=AssertionError("apply must never replan")):
            outcome = init.apply_plan(plan, machine, yes=False, force=False, out=lambda line: None)
        self.assertEqual(outcome.pending, [])
        from oah import rules
        from oah.setup import companions
        self.assertIn(companions.RULES_BEGIN, (repo / rules.LOCAL_RULES).read_text(encoding="utf-8"))
        self.assertEqual(rules.check(repo), [])
        before = snapshot(repo)
        code, out, err = run_init(repo, machine, *flags(aside=True))
        self.assertEqual(code, 0, err + out)
        self.assertEqual(snapshot(repo), before)
        self.assertEqual(last_line(out), "OAH_INIT_UNCHANGED impact=fresh")

    def test_conditional_aside_rules_match_landed_rules_with_existing_content(self):
        from oah import rules
        for eol in (b"\n", b"\r\n"):
            with self.subTest(eol=eol):
                repo = committed_repo(self)
                local = repo / rules.LOCAL_RULES
                local.parent.mkdir()
                original = b"Use tabs." + eol + b"No final newline"
                local.write_bytes(original)
                (repo / "AGENTS.md").write_bytes(b"# Project" + eol)
                rules.sync(repo, lang="ko")
                agents = repo / "AGENTS.md"
                agents.write_bytes(agents.read_bytes().rstrip(b"\r\n"))
                machine = make_machine(self, FakeTools(present=(*ALL_TOOLS, "aside")))
                machine.companion_probe = lambda argv, cwd: 0
                code, out, err = run_init(repo, machine, *flags(aside=True))
                self.assertEqual(code, 0, out + err)
                self.assertTrue(local.read_bytes().startswith(original + eol))
                self.assertFalse(agents.read_bytes().endswith(b"\n"))
                self.assertEqual(rules.check(repo), [])


class BlockedTest(unittest.TestCase):
    """A requested file or hook that init cannot write leaves the run incomplete, never silently done."""

    def test_existing_unlazy_refusal_starts_with_code_and_preserves_directory(self):
        repo = committed_repo(self)
        target = repo / init.UNLAZY_PROJECT
        target.mkdir(parents=True)
        original = b"concurrent user directory\r\n\x00"
        (target / "keep.bin").write_bytes(original)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--force", *flags(unlazy=True))
        self.assertEqual(code, 1, err + out)
        self.assertTrue(any(line.strip().startswith("OAH_INIT_UNLAZY_EXISTS:") for line in out.splitlines()), out)
        self.assertIn("Remove it yourself, then rerun oah init --with-unlazy", out)
        self.assertEqual((target / "keep.bin").read_bytes(), original)
        self.assertFalse((repo / ".git/oah/backups").exists())

    def test_linked_claude_folder_blocks_agents_and_claude_hooks(self):
        repo = committed_repo(self)
        outside = make_tmp_dir(self) / "global-claude"
        outside.mkdir()
        directory_alias(outside, repo / ".claude")
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags(agents=True, hooks=True))
        self.assertEqual(code, 1, err)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("not written: .claude/agents/oah-low.md", out)
        self.assertIn("hook stop was not installed", out)
        token = last_line(out)
        self.assertTrue(token.startswith("OAH_INIT_INCOMPLETE"), token)
        self.assertIn("files", token)
        self.assertIn("hooks", token)
        self.assertIn("oah hook pre-push", (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8"))

    def test_invalid_claude_settings_leave_hooks_pending(self):
        repo = committed_repo(self)
        settings = repo / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        settings.write_text("{not json", encoding="utf-8")
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags(hooks=True))
        self.assertEqual(code, 1, err)
        self.assertIn("OAH_HOOKS_SETTINGS_INVALID", out)
        self.assertIn("pending=hooks", last_line(out))
        self.assertEqual(settings.read_text(encoding="utf-8"), "{not json")

    def test_ambiguous_ci_leaves_ci_pending(self):
        repo = committed_repo(self)
        git_commit(repo, {"go.mod": "module x\n", "package.json": '{"scripts": {"test": "jest"}}'})
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags(ci=True))
        self.assertEqual(code, 1, err)
        self.assertIn("oah ci init", out)
        self.assertIn("pending=ci", last_line(out))
        self.assertFalse((repo / ".github").exists())


class WhereTest(unittest.TestCase):
    def test_init_from_subdirectory_writes_at_worktree_root(self):
        repo = committed_repo(self)
        code, out, err = run_init(repo / "src", make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 0, err)
        self.assertTrue((repo / ".oah" / "config.toml").is_file())
        self.assertFalse((repo / "src" / ".oah").exists())

    def test_linked_worktree_gets_its_own_files_and_map(self):
        repo = committed_repo(self)
        tree = make_tmp_dir(self) / "tree"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(tree)], cwd=repo, check=True, env=git_env())
        code, out, err = run_init(tree, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 0, err)
        self.assertTrue((tree / ".oah" / "config.toml").is_file())
        self.assertFalse((repo / ".oah").exists())
        self.assertEqual(mapstore.status(tree, env={})[0].action, "fresh")
        self.assertIsNone(mapstore.status(repo, env={})[0])



class RefusalTest(unittest.TestCase):
    """Refusals happen before anything is written, and say what to do next."""

    def test_outside_a_repository_refuses(self):
        plain = make_tmp_dir(self)
        machine = make_machine(self, FakeTools())
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(plain.parent)}):
            code, out, err = run_init(plain, machine, *flags())
        self.assertEqual(code, 69)
        self.assertIn("OAH_NOT_A_REPO", err)
        self.assertIn("git init", err)
        self.assertEqual(list(plain.iterdir()), [])

    def test_invalid_config_needs_force(self):
        repo = committed_repo(self)
        config = repo / ".oah" / "config.toml"
        config.parent.mkdir()
        config.write_text("[stages\n", encoding="utf-8")
        before = snapshot(repo)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 69)
        self.assertIn("OAH_CONFIG_INVALID", err)
        self.assertEqual(snapshot(repo), before)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), "--force", *flags())
        self.assertEqual(code, 0, err)
        self.assertIn('initial = "claude"', config.read_text(encoding="utf-8"))

    def test_broken_rules_markers_refuse_before_writing(self):
        repo = committed_repo(self)
        (repo / "AGENTS.md").write_text("<!-- OAH:RULES:BEGIN v1 -->\nno end marker\n", encoding="utf-8")
        before = snapshot(repo)
        code, out, err = run_init(repo, make_machine(self, FakeTools()), *flags())
        self.assertEqual(code, 1)
        self.assertIn("OAH_RULES_MARKER_BROKEN", err)
        self.assertEqual(snapshot(repo), before)

    def test_end_of_input_during_questions_writes_nothing(self):
        repo = committed_repo(self)
        before = snapshot(repo)
        code, out, err = run_init(repo, make_machine(self, FakeTools(), interactive=True, replies=("y",)))
        self.assertEqual(code, 1)
        self.assertIn("OAH_INIT_NO_ANSWER", err)
        self.assertEqual(snapshot(repo), before)


if __name__ == "__main__":
    unittest.main()
