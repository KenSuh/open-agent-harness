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
from oah.paths import repo_info, state_dir
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
TODAY = dt.date(2026, 10, 25)
ALL_TOOLS = ("git", "uv", "node", "npm", "npx", "claude", "codex", "graphify", "gh", "gitleaks", "jq", "rg", "brew")
STATUS_MARKER = "STATUS-OUTPUT-MARKER-7f3a"  # what a sign-in status command printed; init must never show it


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
    """AC1: the full plan comes first; --dry-run prints it and writes, installs and runs nothing."""

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


class DryRunSubprocessTest(unittest.TestCase):
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
