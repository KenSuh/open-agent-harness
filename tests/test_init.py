# 안내형 oah init 핵심 검사 — 설정 렌더링·섹션 검증, 기본 브랜치 감지, 티어 에이전트, .gitignore 항목, 파일 계획(유지·--force 교체·위키 내용 보호)과 쓰기를 본다.
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import secrets as token_source
import subprocess
import sys
import unittest
from unittest import mock

from oah import call, hooks, init, intent, models, preflight, rerank, rules, skillsel, unlazy
from oah.config import parse_config
from oah.engines.proc import Resolved, RunResult
from oah.impact import defaults as impact_defaults
from oah.paths import repo_info, state_dir
from oah.setup import companions
from oah.wiki.base import WikiSettings
from oah.wiki.notes import parse_frontmatter
from oah.wiki.store import Wiki
from tests.helpers import directory_alias, git_commit, git_env, make_git_repo, make_tmp_dir, make_unlazy

TODAY = dt.date(2026, 10, 25)
TIER_BEGIN = "<!-- OAH:TIER-RULES:BEGIN v1 -->"
TIER_END = "<!-- OAH:TIER-RULES:END -->"


class NoKeychain:
    def lookup(self, name: str) -> str | None:
        return None


def git(*args: str, cwd: pathlib.Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=git_env()).stdout.strip()


def sections(text: str) -> dict:
    """Load the rendered config through every section a landed command reads."""
    config = parse_config(text.encode("utf-8"))
    return {
        "call": call.Settings.validated(config.section("stages", call.STAGE_DEFAULTS),
                                        config.section("engines", call.ENGINE_DEFAULTS),
                                        config.section("models", models.DEFAULTS)),
        "preflight": config.section("preflight", preflight.DEFAULTS),
        "skills": config.section("skills", skillsel.config_defaults()),
        "rerank": config.section("rerank", rerank.DEFAULTS),
        "impact": impact_defaults.effective(config.section("impact", impact_defaults.DEFAULTS)),
    }


class ConfigTest(unittest.TestCase):
    def test_config_validates_against_every_section(self):
        for jev in (True, False):
            for updates in (True, False):
                with self.subTest(jev=jev, updates=updates):
                    text = init.render_config(branch="trunk", how="from origin/HEAD", jev=jev,
                                              skill_updates=updates, today=TODAY)
                    found = sections(text)
                    self.assertEqual(found["call"].stages,
                                     {"initial": "claude", "review": "codex", "correction": "codex"})
                    claude = found["call"].engines["claude"]
                    self.assertEqual(claude["quality_order"], ["opus", "sonnet", "haiku"])
                    self.assertEqual({tier: claude["tiers"][tier]["effort"] for tier in claude["tiers"]},
                                     {"low": "low", "medium": "medium", "high": "high", "max": "max"})
                    codex = found["call"].engines["codex"]
                    self.assertEqual(codex["quality_order"], [])
                    self.assertEqual(codex["tiers"]["max"]["effort"], "highest")
                    self.assertEqual(found["preflight"]["default_branch"], "trunk")
                    self.assertIs(found["skills"]["auto_update"], updates)
                    self.assertEqual(found["skills"]["update_interval_days"], 1)
                    self.assertIs(found["rerank"]["enabled"], jev)
                    self.assertEqual(found["rerank"]["provider"], "typesafe" if jev else "")
                    self.assertIs(found["impact"]["rerank"], jev)
                    self.assertIn("2026-10-25", text)
                    self.assertNotIn("{{", text)

    def test_branch_name_is_quoted_safely(self):
        text = init.render_config(branch='odd"name\\x', how="the current branch", jev=False, skill_updates=True,
                                  today=TODAY)
        self.assertEqual(sections(text)["preflight"]["default_branch"], 'odd"name\\x')

    def test_key_alone_never_enables_jev(self):
        key = "fake-" + token_source.token_hex(16)
        env = {"TYPESAFE_API_KEY": key}
        off = sections(init.render_config(branch="main", how="", jev=False, skill_updates=True, today=TODAY))
        result = rerank.rerank("skills", [rerank.Candidate("a", "x")], config=off["rerank"], env=env,
                               keychain=NoKeychain())
        self.assertEqual(result.status, "unused(not-enabled)")
        on = sections(init.render_config(branch="main", how="", jev=True, skill_updates=True, today=TODAY))
        result = rerank.rerank("skills", [rerank.Candidate("a", "x")], config=on["rerank"], env={},
                               keychain=NoKeychain())
        self.assertEqual(result.status, "unused(no key)")


class DefaultBranchTest(unittest.TestCase):
    def test_remote_head_symref(self):
        work = make_tmp_dir(self)
        source = make_git_repo(self)
        git("checkout", "-q", "-b", "trunk", cwd=source)
        git_commit(source, {"a.txt": "a\n"})
        git("clone", "-q", str(source), str(work / "clone"))
        self.assertEqual(init.default_branch(work / "clone"), ("trunk", "from origin/HEAD"))

    def test_remote_branch_without_head(self):
        source = make_git_repo(self)
        git("checkout", "-q", "-b", "master", cwd=source)
        git_commit(source, {"a.txt": "a\n"})
        repo = make_git_repo(self)
        git("remote", "add", "origin", str(source), cwd=repo)
        git("fetch", "-q", "origin", cwd=repo)
        git("update-ref", "--no-deref", "-d", "refs/remotes/origin/HEAD", cwd=repo)  # newer git records origin/HEAD on fetch
        self.assertEqual(init.default_branch(repo), ("master", "from origin/master"))

    def test_local_main_beats_the_current_branch(self):
        repo = make_git_repo(self)
        git_commit(repo, {"a.txt": "a\n"})
        git("checkout", "-q", "-b", "feature", cwd=repo)
        self.assertEqual(init.default_branch(repo), ("main", "the local main branch"))

    def test_unborn_current_branch(self):
        repo = make_tmp_dir(self) / "repo"
        git("init", "-q", "-b", "trunk", str(repo))
        self.assertEqual(init.default_branch(repo), ("trunk", "the current branch"))


class AgentFilesTest(unittest.TestCase):
    def frontmatter(self, data: bytes) -> dict:
        return parse_frontmatter(data.decode("utf-8"))

    def test_agent_files(self):
        files = init.agent_files(call.ENGINE_DEFAULTS)
        self.assertEqual(sorted(files), [".claude/agents/oah-high.md", ".claude/agents/oah-low.md",
                                         ".claude/agents/oah-max.md", ".claude/agents/oah-medium.md"])
        found = {path: self.frontmatter(data) for path, data in files.items()}
        self.assertEqual({path.rsplit("/", 1)[1]: (meta["name"], meta["model"], meta["effort"])
                          for path, meta in found.items()},
                         {"oah-low.md": ("oah-low", "haiku", "low"), "oah-medium.md": ("oah-medium", "sonnet", "medium"),
                          "oah-high.md": ("oah-high", "opus", "high"), "oah-max.md": ("oah-max", "opus", "max")})
        for path, data in files.items():
            with self.subTest(path=path):
                text = data.decode("utf-8")
                self.assertTrue(found[path]["description"])
                lines = text.split("\n")
                self.assertEqual(lines.count(TIER_BEGIN), 1)
                self.assertEqual(lines.count(TIER_END), 1)
                block = text[text.index(TIER_BEGIN):text.index(TIER_END)]
                self.assertTrue(intent.has_footer(block))
                self.assertIn(intent.FOOTER, block)
                self.assertNotIn("{{", text)

    def test_tier_pin_wins_and_unresolved_inherits(self):
        engines = {"claude": dict(call.ENGINE_DEFAULTS["claude"], tiers={
            tier: dict(values, model="pinned-model" if tier == "medium" else "")
            for tier, values in call.ENGINE_DEFAULTS["claude"]["tiers"].items()})}
        files = init.agent_files(engines)
        self.assertEqual(self.frontmatter(files[".claude/agents/oah-medium.md"])["model"], "pinned-model")
        self.assertEqual(self.frontmatter(files[".claude/agents/oah-low.md"])["model"], "haiku")
        engines["claude"]["quality_order"] = []
        files = init.agent_files(engines)
        self.assertEqual(self.frontmatter(files[".claude/agents/oah-high.md"])["model"], "inherit")
        self.assertEqual(self.frontmatter(files[".claude/agents/oah-medium.md"])["model"], "pinned-model")


class GitignoreTest(unittest.TestCase):
    EXPECTED = (".oah/tasks/*/GATES.md", "**/.obsidian/workspace*.json", "**/.obsidian/workspace", ".env", ".env.*",
                "*.pem", "*.key", ".oah/secrets*", ".claude/settings.local.json", ".claude/settings.local.json.oah.bak")

    def entries(self, data: bytes) -> list[str]:
        return [line.strip() for line in data.decode("utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]

    def test_new_file_gets_every_entry(self):
        data = init.add_gitignore_entries(None, unlazy_copy=False)
        self.assertEqual(sorted(self.entries(data)), sorted(self.EXPECTED))
        self.assertNotIn(b"\r", data)
        self.assertIsNone(init.add_gitignore_entries(data, unlazy_copy=False))

    def test_gitignore_adds_only_missing_entries_in_file_line_ending(self):
        original = b"node_modules/\r\n.env\r\n# mine\r\n  *.pem  \r\ndist"
        data = init.add_gitignore_entries(original, unlazy_copy=False)
        self.assertTrue(data.startswith(original + b"\r\n"))
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        found = self.entries(data)
        self.assertEqual(found.count(".env"), 1)
        self.assertEqual(found.count("*.pem"), 1)
        self.assertEqual(sorted(set(found) - {"node_modules/", "dist"}), sorted(self.EXPECTED))
        self.assertIsNone(init.add_gitignore_entries(data, unlazy_copy=False))

    def test_unlazy_copy_entry_only_when_installed(self):
        base = init.add_gitignore_entries(None, unlazy_copy=False)
        self.assertNotIn(".agents/skills/unlazy/", self.entries(base))
        data = init.add_gitignore_entries(base, unlazy_copy=True)
        self.assertEqual(self.entries(data)[-1], ".agents/skills/unlazy/")
        self.assertIsNone(init.add_gitignore_entries(data, unlazy_copy=True))


class PlanFilesTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        self.config = init.render_config(branch="main", how="the current branch", jev=False, skill_updates=True,
                                         today=TODAY).encode("utf-8")

    def plan(self, *, force: bool = False, agents: bool = True, graph: bool = True, unlazy_copy: bool = False):
        return init.plan_files(self.repo, config=self.config, wiki_dir="docs/wiki",
                               agents=init.agent_files(call.ENGINE_DEFAULTS) if agents else {}, graph=graph,
                               unlazy_copy=unlazy_copy, force=force, today=TODAY)

    def by_path(self, changes) -> dict:
        return {change.path: change for change in changes}

    def test_plan_files_fresh_repository(self):
        found = self.by_path(self.plan())
        expected = {".oah/config.toml", ".oah/templates/PACKET.md", ".oah/templates/GATES.md",
                    ".oah/templates/README.md", "docs/wiki/index.md", "docs/wiki/log.md", "docs/wiki/wiki-guide.md",
                    "docs/wiki/templates/note.md", "docs/wiki/templates/decision.md",
                    "docs/wiki/.obsidian/app.json", "docs/wiki/.obsidian/core-plugins.json",
                    "docs/wiki/.obsidian/templates.json", ".claude/agents/oah-low.md",
                    ".claude/agents/oah-medium.md", ".claude/agents/oah-high.md", ".claude/agents/oah-max.md",
                    ".graphifyignore", ".gitignore"}
        self.assertEqual(set(found), expected)
        self.assertEqual({change.action for change in found.values()}, {"create"})
        self.assertEqual(found[".oah/config.toml"].data, self.config)
        log = found["docs/wiki/log.md"].data.decode("utf-8")
        self.assertIn("<!-- OAH:LOG_INSERT_BELOW -->", log)
        self.assertIn("## 2026-10-25 Wiki created", log)
        self.assertIn("docs/wiki/log.md", found[".graphifyignore"].data.decode("utf-8"))
        self.assertIn(".oah/tasks/*/GATES.md", found[".gitignore"].data.decode("utf-8"))

    def test_options_leave_their_files_out(self):
        found = self.by_path(self.plan(agents=False, graph=False))
        self.assertFalse([path for path in found if path.startswith(".claude/agents/")])
        self.assertNotIn(".graphifyignore", found)

    def test_plan_files_keeps_existing_and_force_replaces(self):
        git_commit(self.repo, {
            ".oah/config.toml": "[stages]\ninitial = \"codex\"\n",
            "docs/wiki/index.md": b"My catalog\r\n\r\nNotes:\r\n- [[mine]]\r\n",
            "docs/wiki/log.md": "my own log\n",
            ".claude/agents/oah-low.md": "custom agent\n",
        })
        same = init.agent_files(call.ENGINE_DEFAULTS)[".claude/agents/oah-high.md"]
        (self.repo / ".claude" / "agents" / "oah-high.md").write_bytes(same)
        for force in (False, True):
            with self.subTest(force=force):
                found = self.by_path(self.plan(force=force))
                self.assertEqual(found[".oah/config.toml"].action, "replace" if force else "keep")
                self.assertEqual(found[".claude/agents/oah-low.md"].action, "replace" if force else "keep")
                self.assertEqual(found[".claude/agents/oah-high.md"].action, "unchanged")
                self.assertEqual(found["docs/wiki/log.md"].action, "keep")
                index = found["docs/wiki/index.md"]
                self.assertEqual(index.action, "update")
                self.assertEqual(index.data, b"My catalog\r\n\r\nNotes:\r\n- [[mine]]\r\n"
                                             b"- [[wiki-guide|How this wiki works]]\r\n")
                self.assertEqual(found["docs/wiki/wiki-guide.md"].action, "create")

    def test_index_that_already_links_the_guide_is_kept(self):
        git_commit(self.repo, {"docs/wiki/index.md": "Notes:\n- [[wiki-guide]]\n"})
        self.assertEqual(self.by_path(self.plan())["docs/wiki/index.md"].action, "keep")

    def test_force_never_replaces_wiki_content(self):
        git_commit(self.repo, {"docs/wiki/index.md": "Notes:\n- [[wiki-guide]]\n- [[mine]]\n",
                               "docs/wiki/wiki-guide.md": "my guide\n", "docs/wiki/log.md": "my log\n"})
        found = self.by_path(self.plan(force=True))
        self.assertEqual((found["docs/wiki/index.md"].action, found["docs/wiki/log.md"].action), ("keep", "keep"))
        self.assertEqual(found["docs/wiki/wiki-guide.md"].action, "replace")

    def test_is_link_detects_symlinks_and_junctions(self):
        target = make_tmp_dir(self) / "target"
        target.mkdir()
        directory_alias(target, self.repo / "alias")
        (self.repo / "plain").mkdir()
        self.assertTrue(init._is_link(self.repo / "alias"))
        self.assertFalse(init._is_link(self.repo / "plain"))
        self.assertFalse(init._is_link(self.repo / "missing"))

    def test_never_writes_through_a_linked_folder(self):
        outside = make_tmp_dir(self) / "global-claude"
        outside.mkdir()
        directory_alias(outside, self.repo / ".claude")
        found = self.by_path(self.plan(force=True))
        for tier in ("low", "medium", "high", "max"):
            change = found[f".claude/agents/oah-{tier}.md"]
            self.assertEqual(change.action, "blocked")
            self.assertIn("link", change.note)
        self.assertEqual(list(outside.iterdir()), [])

    def test_wiki_dir_is_configurable(self):
        found = self.by_path(init.plan_files(self.repo, config=self.config, wiki_dir="notes/wiki", agents={},
                                             graph=True, unlazy_copy=False, force=False, today=TODAY))
        self.assertIn("notes/wiki/log.md", found)
        self.assertIn("notes/wiki/log.md", found[".graphifyignore"].data.decode("utf-8"))
        self.assertIn("notes/wiki/templates/", found[".oah/templates/README.md"].data.decode("utf-8"))


class WriteFilesTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        info = repo_info(self.repo)
        self.wiki = Wiki(info.root, WikiSettings(), state_dir(info, {}))
        self.config = init.render_config(branch="main", how="", jev=False, skill_updates=True,
                                         today=TODAY).encode("utf-8")

    def plan(self, *, force: bool = False):
        return init.plan_files(self.repo, config=self.config, wiki_dir="docs/wiki", agents={}, graph=False,
                               unlazy_copy=False, force=force, today=TODAY)

    def test_write_files_creates_every_planned_file(self):
        changes = self.plan()
        written, problems = init.write_files(self.repo, changes, wiki=self.wiki)
        self.assertEqual(problems, [])
        self.assertEqual(len(written), len(changes))
        for change in changes:
            self.assertEqual((self.repo / change.path).read_bytes(), change.data, change.path)
        again = {change.path: change.action for change in self.plan()}
        self.assertEqual(set(again), {change.path for change in changes})
        self.assertEqual(set(again.values()), {"unchanged"})

    def test_write_files_never_clobbers(self):
        changes = self.plan()
        late = self.repo / ".oah" / "config.toml"
        late.parent.mkdir(parents=True)
        late.write_text("[stages]\n", encoding="utf-8")
        written, problems = init.write_files(self.repo, changes, wiki=self.wiki)
        self.assertEqual(late.read_text(encoding="utf-8"), "[stages]\n")
        self.assertTrue(any(problem.startswith("OAH_INIT_CHANGED") and ".oah/config.toml" in problem
                            for problem in problems), problems)
        self.assertNotIn(".oah/config.toml", [change.path for change in written])

    def test_creates_refuse_parents_linked_after_planning(self):
        changes = self.plan()
        changes.append(init.FileChange(".claude/agents/oah-low.md", "create", "agents", b"agent\n"))
        outside = make_tmp_dir(self)
        for name in (".oah", "docs", ".claude"):
            directory_alias(outside, self.repo / name)
        written, problems = init.write_files(self.repo, changes, wiki=self.wiki)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(any(change.path.startswith((".oah/", "docs/")) for change in written))
        self.assertTrue(problems and all(p.startswith("OAH_INIT_CHANGED") for p in problems))

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_windows_create_refuses_late_junction(self):
        self.test_creates_refuse_parents_linked_after_planning()

    @unittest.skipUnless(os.name == "nt", "Windows directory handle sharing")
    def test_windows_publication_pins_parents_against_replacement(self):
        with init._windows_parents(self.repo, init.CONFIG_PATH):
            result = subprocess.run([sys.executable, "-c", "import os, sys; os.rename(sys.argv[1], sys.argv[2])",
                                     str(self.repo / ".oah"), str(self.repo / "moved")], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((self.repo / ".oah").is_dir())

    def test_replace_skips_a_file_edited_after_the_plan(self):
        late = self.repo / ".oah" / "config.toml"
        late.parent.mkdir(parents=True)
        late.write_text("[stages]\n", encoding="utf-8")
        changes = [change for change in self.plan(force=True) if change.path == ".oah/config.toml"]
        self.assertEqual(changes[0].action, "replace")
        late.write_text("[stages]\ninitial = \"claude\"\n", encoding="utf-8")
        written, problems = init.write_files(self.repo, changes, wiki=self.wiki)
        self.assertEqual(written, [])
        self.assertEqual(late.read_text(encoding="utf-8"), "[stages]\ninitial = \"claude\"\n")
        self.assertTrue(problems and problems[0].startswith("OAH_INIT_CHANGED"))



class RulesStepTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)

    def test_plan_rules_states(self):
        step = init.plan_rules(self.repo, lang=None)
        self.assertEqual((step.lang, step.agents, step.claude), ("en", "create", "create"))
        rules.sync(self.repo)
        self.assertEqual(init.plan_rules(self.repo, lang=None)[1:3], ("unchanged", "unchanged"))
        self.assertEqual(init.plan_rules(self.repo, lang="ko")[:3], ("ko", "update", "unchanged"))
        (self.repo / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
        self.assertEqual(init.plan_rules(self.repo, lang=None)[1:3], ("update", "update"))

    def test_broken_markers_refuse(self):
        (self.repo / "AGENTS.md").write_text(f"{rules.BEGIN}\nno end\n", encoding="utf-8")
        with self.assertRaises(rules.RulesError):
            init.plan_rules(self.repo, lang=None)

    def test_aside_rules_section_added_once(self):
        self.assertEqual(init.add_aside_rules(self.repo, lang="en"), "added")
        local = self.repo / ".oah" / "rules.local.md"
        self.assertEqual(local.read_text(encoding="utf-8").count(companions.RULES_BEGIN), 1)
        self.assertEqual(init.add_aside_rules(self.repo, lang="en"), "present")
        local.write_bytes(b"Use tabs.\r\nno final newline")
        self.assertEqual(init.add_aside_rules(self.repo, lang="ko"), "added")
        data = local.read_bytes()
        self.assertTrue(data.startswith(b"Use tabs.\r\nno final newline\r\n"))
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        rules.sync(self.repo)
        agents = (self.repo / "AGENTS.md").read_text(encoding="utf-8")
        block = agents[agents.index(rules.BEGIN):agents.index(rules.END)]
        self.assertIn(companions.RULES_BEGIN, block)


class HookStepTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        (self.repo / ".gitignore").write_bytes(init.add_gitignore_entries(None, unlazy_copy=False))

    def steps(self, force: bool = False) -> dict:
        return {step.kind: step for step in init.plan_hooks(self.repo, force=force)}

    def test_plan_hooks_shows_diffs_and_respects_foreign_hooks(self):
        found = self.steps()
        self.assertEqual(found["impact-hint"].action, "unavailable")
        for kind in ("stop", "session-start", "pre-commit", "post-commit", "post-merge", "pre-push"):
            self.assertEqual(found[kind].action, "install", kind)
            self.assertIn(f"oah hook {kind}", found[kind].diff)
        foreign = hooks.git_hook_path(self.repo, "pre-commit")
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        found = self.steps()
        self.assertEqual(found["pre-commit"].action, "keep")
        self.assertIn("--force", found["pre-commit"].note)
        self.assertEqual(self.steps(force=True)["pre-commit"].action, "install")
        installed, problems = init.install_hooks(self.repo, list(found.values()), force=False)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(installed), ["post-commit", "post-merge", "pre-push", "session-start", "stop"])
        self.assertEqual(foreign.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        again = self.steps()
        self.assertEqual({kind: again[kind].action for kind in installed}, {kind: "installed" for kind in installed})

    def test_claude_hooks_never_write_through_a_linked_folder(self):
        outside = make_tmp_dir(self) / "global-claude"
        outside.mkdir()
        directory_alias(outside, self.repo / ".claude")
        found = self.steps()
        self.assertEqual((found["stop"].action, found["session-start"].action), ("refused", "refused"))
        init.install_hooks(self.repo, list(found.values()), force=False)
        self.assertEqual(list(outside.iterdir()), [])

    def test_settings_diff_only_contains_oah_hooks(self):
        secret = "fake-" + token_source.token_hex(20)
        path = self.repo / init.CLAUDE_SETTINGS
        path.parent.mkdir()
        path.write_bytes(json.dumps({"env": {"ANTHROPIC_API_KEY": secret}, "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo " + secret}]}]}}
        ).encode("utf-8"))
        for step in self.steps().values():
            self.assertNotIn(secret, str(step))
            self.assertNotIn("ANTHROPIC_API_KEY", step.diff)
        self.assertIn("oah hook stop", self.steps()["stop"].diff)

    def test_local_settings_backup_is_ignored_before_install(self):
        path = self.repo / init.CLAUDE_SETTINGS
        path.parent.mkdir()
        secret = "fake-" + token_source.token_hex(20)
        original = json.dumps({"env": {"ANTHROPIC_API_KEY": secret}}).encode("utf-8")
        path.write_bytes(original)
        (self.repo / ".gitignore").write_bytes(init.add_gitignore_entries(None, unlazy_copy=False))
        installed, problems = init.install_hooks(self.repo, list(self.steps().values()), force=False)
        self.assertEqual(problems, [])
        self.assertIn("stop", installed)
        backup = init.CLAUDE_SETTINGS + hooks.BACKUP_SUFFIX
        self.assertEqual(git("check-ignore", backup, cwd=self.repo), backup)
        git("add", "-A", cwd=self.repo)
        self.assertNotIn(backup, git("ls-files", cwd=self.repo).splitlines())
        self.assertEqual((self.repo / backup).read_bytes(), original)

    def test_hooks_refuse_missing_backup_ignore_or_a_linked_backup(self):
        steps = list(self.steps().values())
        ignore = self.repo / ".gitignore"
        ignore.write_bytes(ignore.read_bytes() + b"!.claude/settings.local.json.oah.bak\n")
        installed, problems = init.install_hooks(self.repo, steps, force=False)
        self.assertNotIn("stop", installed)
        self.assertTrue(any(p.startswith("OAH_INIT_HOOK_SETTINGS_UNSAFE") for p in problems))
        self.assertFalse((self.repo / init.CLAUDE_SETTINGS).exists())

    def test_hooks_refuse_tracked_settings_backup(self):
        backup = init.CLAUDE_SETTINGS + hooks.BACKUP_SUFFIX
        steps = list(self.steps().values())
        (self.repo / backup).parent.mkdir(exist_ok=True)
        (self.repo / backup).write_bytes(b"{}\n")
        git("add", "-f", backup, cwd=self.repo)
        installed, problems = init.install_hooks(self.repo, steps, force=False)
        self.assertNotIn("stop", installed)
        self.assertTrue(problems)
        self.assertEqual(self.steps()["stop"].action, "refused")
        self.assertFalse((self.repo / init.CLAUDE_SETTINGS).exists())

    def test_git_hooks_refuse_shared_external_destination(self):
        outside = make_tmp_dir(self)
        git("config", "core.hooksPath", str(outside), cwd=self.repo)
        steps = self.steps(force=True)
        for kind in hooks.GIT_KINDS:
            self.assertEqual(steps[kind].action, "refused")
        init.install_hooks(self.repo, list(steps.values()), force=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_git_hooks_recheck_destination_after_planning(self):
        steps = list(self.steps().values())
        outside = make_tmp_dir(self)
        git("config", "core.hooksPath", str(outside), cwd=self.repo)
        installed, problems = init.install_hooks(self.repo, steps, force=False)
        self.assertFalse(set(installed) & set(hooks.GIT_KINDS))
        self.assertTrue(problems)
        self.assertEqual(list(outside.iterdir()), [])

    def test_project_local_custom_hooks_are_allowed(self):
        git("config", "core.hooksPath", ".githooks", cwd=self.repo)
        self.assertEqual(self.steps()["pre-commit"].action, "install")

    def test_linked_worktree_can_use_repository_hooks(self):
        git_commit(self.repo, {"README.md": "hello\n"})
        worktree = make_tmp_dir(self) / "linked"
        git("worktree", "add", "--detach", str(worktree), cwd=self.repo)
        steps = {step.kind: step for step in init.plan_hooks(worktree, force=False)}
        self.assertEqual(steps["pre-commit"].action, "install")

    def test_project_local_hook_directory_cannot_be_a_link(self):
        outside = make_tmp_dir(self)
        directory_alias(outside, self.repo / ".githooks")
        git("config", "core.hooksPath", ".githooks", cwd=self.repo)
        self.assertEqual(self.steps(force=True)["pre-commit"].action, "refused")


class UnlazyStepTest(unittest.TestCase):
    FILES = {"scripts/gate-check.mjs": "// gate check\n", "scripts/lib/gates.mjs": "// gates\n"}

    def setUp(self):
        self.source = make_git_repo(self)
        self.commit = git_commit(self.source, self.FILES, "unlazy")
        self.pin = unlazy.Pin(commit=self.commit, files={rel: unlazy.file_digest(self.source / rel)
                                                         for rel in self.FILES})
        self.repo = make_git_repo(self)
        self.home = make_tmp_dir(self) / "home"
        self.home.mkdir()
        self.target = self.repo / ".agents" / "skills" / "unlazy"

    def plan(self, *, force: bool = False, env: dict | None = None, pin=None):
        return init.plan_unlazy(self.repo, env=env or {}, home=self.home, pin=pin or self.pin, force=force)

    def test_unlazy_install_from_local_source(self):
        step = self.plan()
        self.assertEqual(step.action, "install")
        self.assertEqual(init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin), "")
        self.assertEqual(unlazy.verify_pin(self.target, self.pin), [])
        self.assertEqual(sorted(path.name for path in self.target.parent.iterdir()), ["unlazy"])
        self.assertEqual(self.plan().action, "present")

    def test_linked_project_skill_parents_are_blocked_even_with_force(self):
        for directory in (".agents", ".claude"):
            with self.subTest(directory=directory):
                self.repo = make_git_repo(self)
                outside = make_tmp_dir(self)
                make_unlazy(outside / "unlazy")
                parent = self.repo / directory
                parent.mkdir(exist_ok=True)
                directory_alias(outside, parent / "skills")
                step = self.plan(force=True)
                self.assertEqual(step.action, "blocked")
                problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin)
                self.assertTrue(problem)
                self.assertTrue((outside / "unlazy/scripts/gate-check.mjs").is_file())
                self.assertFalse((outside / "unlazy.oah.bak").exists())

    def test_install_revalidates_late_linked_parent(self):
        step = self.plan()
        outside = make_tmp_dir(self)
        (self.repo / ".agents").mkdir()
        directory_alias(outside, self.target.parent)
        self.assertTrue(init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin))
        self.assertEqual(list(outside.iterdir()), [])

    def test_replacement_revalidates_late_linked_copy(self):
        make_unlazy(self.target)
        step = self.plan(force=True)
        outside = make_tmp_dir(self) / "unlazy"
        self.target.rename(outside)
        directory_alias(outside, self.target)
        self.assertTrue(init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin))
        self.assertTrue((outside / "scripts/gate-check.mjs").is_file())
        self.assertFalse(outside.with_name("unlazy.oah.bak").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_windows_unlazy_refuses_junction(self):
        self.test_install_revalidates_late_linked_parent()

    def test_failed_install_leaves_nothing_behind(self):
        for pin in (unlazy.Pin(commit="0" * 40, files=self.pin.files),
                    unlazy.Pin(commit=self.commit, files={"scripts/gate-check.mjs": "0" * 64})):
            with self.subTest(pin=pin):
                problem = init.install_unlazy(self.repo, self.plan(pin=pin), source=str(self.source), pin=pin)
                self.assertTrue(problem)
                self.assertFalse(self.target.exists())
                parent = self.target.parent
                self.assertEqual(list(parent.iterdir()) if parent.exists() else [], [])

    def test_incompatible_project_copy_needs_force(self):
        make_unlazy(self.target)
        self.assertEqual(self.plan().action, "blocked")
        self.assertIn("--force", self.plan().note)
        step = self.plan(force=True)
        self.assertEqual(step.action, "replace")
        self.assertEqual(init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin), "")
        self.assertEqual(unlazy.verify_pin(self.target, self.pin), [])
        backup = self.target.with_name("unlazy.oah.bak")
        self.assertTrue((backup / "scripts" / "gate-check.mjs").is_file())

    def test_failed_replacement_puts_the_old_copy_back(self):
        make_unlazy(self.target)
        bad = unlazy.Pin(commit="0" * 40, files=self.pin.files)
        step = self.plan(force=True, pin=bad)
        self.assertEqual(step.action, "replace")
        self.assertTrue(init.install_unlazy(self.repo, step, source=str(self.source), pin=bad))
        self.assertTrue((self.target / "scripts" / "gate-check.mjs").is_file())
        self.assertEqual(sorted(path.name for path in self.target.parent.iterdir()), ["unlazy"])

    def test_incompatible_user_copy_gets_a_project_copy(self):
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        step = self.plan()
        self.assertEqual(step.action, "install")
        self.assertIn("not the pinned", step.note)

    def test_remove_tree_clears_read_only_files(self):
        # git writes its object files read-only; Windows refuses to delete those without clearing the flag
        folder = make_tmp_dir(self) / "clone"
        (folder / "objects").mkdir(parents=True)
        locked = folder / "objects" / "pack"
        locked.write_text("x", encoding="utf-8")
        locked.chmod(0o444)
        init._remove_tree(folder)
        self.assertFalse(folder.exists())

    def test_override_must_be_cleared(self):
        empty = make_tmp_dir(self)
        self.assertEqual(self.plan(env={"OAH_UNLAZY_DIR": str(empty)}).action, "blocked")
        make_unlazy(empty / "copy")
        step = self.plan(env={"OAH_UNLAZY_DIR": str(empty / "copy")})
        self.assertEqual(step.action, "blocked")
        self.assertIn("OAH_UNLAZY_DIR", step.note)


class CiStepTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        (self.repo / "go.mod").write_text("module x\n", encoding="utf-8")
        self.target = self.repo / ".github" / "workflows" / "oah-ci.yml"

    def test_plan_ci_states(self):
        step = init.plan_ci(self.repo, force=False, oses=["linux"], test_command=None)
        self.assertEqual(step.action, "create")
        self.assertIn("go test ./...", step.text)
        self.assertEqual(init.write_ci(step), "")
        self.assertEqual(self.target.read_text(encoding="utf-8"), step.text)
        self.assertEqual(init.plan_ci(self.repo, force=False, oses=["linux"], test_command=None).action, "unchanged")
        self.target.write_text("name: mine\n", encoding="utf-8")
        kept = init.plan_ci(self.repo, force=False, oses=["linux"], test_command=None)
        self.assertEqual(kept.action, "keep")
        self.assertIn("oah ci init", kept.note)
        replaced = init.plan_ci(self.repo, force=True, oses=["linux"], test_command=None)
        self.assertEqual(replaced.action, "replace")
        self.assertIn("-name: mine", replaced.diff)
        self.assertEqual(init.write_ci(replaced), "")
        self.assertEqual(self.target.read_text(encoding="utf-8"), step.text)

    def test_several_test_commands_are_skipped(self):
        (self.repo / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
        step = init.plan_ci(self.repo, force=False, oses=[], test_command=None)
        self.assertEqual(step.action, "skipped")
        self.assertIn("oah ci init", step.note)


class MapStepTest(unittest.TestCase):
    def test_map_state_and_refresh(self):
        repo = make_git_repo(self)
        state, reason = init.map_state(repo, {})
        self.assertEqual(state, "unavailable")
        self.assertIn("OAH_IMPACT_NO_COMMIT", reason)
        self.assertEqual(init.refresh_map(repo, {})[0], "unavailable")
        git_commit(repo, {"a.py": "print('a')\n"})
        self.assertEqual(init.map_state(repo, {})[0], "missing")
        self.assertEqual(init.refresh_map(repo, {})[0], "built")
        self.assertEqual(init.map_state(repo, {})[0], "fresh")
        self.assertEqual(init.refresh_map(repo, {})[0], "fresh")
        git_commit(repo, {"b.py": "print('b')\n"})
        self.assertEqual(init.map_state(repo, {})[0], "stale")
        self.assertEqual(init.refresh_map(repo, {})[0], "refreshed")


class OrcaProbeTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)

    def probe(self, argv, responses):
        def run(command, **kwargs):
            data = responses[tuple(command[1:])]
            return RunResult(0, json.dumps(data), "", False)
        with mock.patch.object(init, "resolve_command", return_value=Resolved(("orca",), "orca", "native")), \
                mock.patch.object(init, "run_command", side_effect=run):
            return init.companion_probe(argv, self.repo)

    def test_guides_require_every_bundled_name(self):
        argv = ("orca", "skills", "installed", "--json")
        data = {("skills", "list", "--json"): {"topics": [{"name": "orca-cli"}, {"name": "computer-use"}]},
                argv[1:]: {"ok": True, "result": {"skills": [{"name": "orca-cli"}]}}}
        self.assertEqual(self.probe(argv, data), 1)
        data[argv[1:]]["result"]["skills"].append({"name": "computer-use"})
        self.assertEqual(self.probe(argv, data), 0)
        data[argv[1:]] = {"ok": False, "error": {"message": "fake-" + token_source.token_hex(20)}}
        self.assertEqual(self.probe(argv, data), 2)

    def test_registration_requires_matching_project_host_path_and_ready_state(self):
        argv = ("orca", "project", "setups", "--project", "github:example/sample", "--host", "local", "--json")
        row = {"projectId": "github:example/sample", "hostId": "local", "path": str(self.repo),
               "setupState": "ready"}
        data = {argv[1:]: {"ok": True, "result": {"setups": [row]}}}
        self.assertEqual(self.probe(argv, data), 0)
        row["path"] = str(self.repo.parent)
        self.assertEqual(self.probe(argv, data), 2)
        data[argv[1:]]["result"]["setups"] = []
        self.assertEqual(self.probe(argv, data), 1)



class Replies:
    """A scripted `ask`: returns the next reply (None means end of input) and records each prompt."""

    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str | None:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


ALL_KEYS = ("agents", "hooks", "unlazy", "graph", "ci", "orca", "aside", "jev", "skill_updates")


class AnswersTest(unittest.TestCase):
    def resolve(self, given: dict, *, ask=None, recorded=None, interactive=True, dry_run=False):
        lines: list[str] = []
        ask = ask or Replies()
        answers = init.resolve_answers(given, recorded=recorded, interactive=interactive, dry_run=dry_run,
                                       ask=ask, out=lines.append)
        return answers, ask, "\n".join(lines)

    def test_questions_and_flags(self):
        self.assertEqual(tuple(question.key for question in init.QUESTIONS), ALL_KEYS)
        flags = {question.key: (question.flag_yes, question.flag_no) for question in init.QUESTIONS}
        self.assertEqual(flags["agents"], ("--with-agents", "--without-agents"))
        self.assertEqual(flags["skill_updates"], ("--with-skill-updates", "--without-skill-updates"))

    def test_flags_answer_without_asking(self):
        given = {key: key in ("agents", "jev") for key in ALL_KEYS}
        answers, ask, _ = self.resolve(given, interactive=False)
        self.assertEqual(ask.prompts, [])
        self.assertEqual({key: (answer.value, answer.source) for key, answer in answers.items()},
                         {key: (key in ("agents", "jev"), "flag") for key in ALL_KEYS})

    def test_enter_takes_the_default(self):
        answers, ask, _ = self.resolve({}, ask=Replies("", "", "", "", "", "", "", "n", ""))
        self.assertEqual(len(ask.prompts), 9)
        self.assertEqual({key: answer.value for key, answer in answers.items()},
                         {"agents": True, "hooks": True, "unlazy": True, "graph": False, "ci": False, "orca": False,
                          "aside": False, "jev": False, "skill_updates": True})
        self.assertEqual({answer.source for answer in answers.values()}, {"asked"})

    def test_invalid_answer_asks_again(self):
        given = {key: True for key in ALL_KEYS if key != "graph"}
        answers, ask, out = self.resolve(given, ask=Replies("maybe", "YES"))
        self.assertTrue(answers["graph"].value)
        self.assertEqual(len(ask.prompts), 2)
        self.assertIn("y or n", out)

    def test_jev_question_requires_typed_answer(self):
        given = {key: False for key in ALL_KEYS if key != "jev"}
        answers, ask, out = self.resolve(given, ask=Replies("", "y"))
        self.assertEqual(answers["jev"], init.Answer(True, "asked"))
        self.assertEqual(len(ask.prompts), 2)
        lowered = (out + ask.prompts[0]).lower()
        for words in ("recommended", "charge", "200 characters", "never adds or drops", "typesafe_api_key"):
            self.assertIn(words, lowered)

    def test_recorded_answers_come_from_existing_config(self):
        given = {key: True for key in ALL_KEYS if key not in ("jev", "skill_updates")}
        answers, ask, _ = self.resolve(given, recorded={"jev": False, "skill_updates": True}, interactive=False)
        self.assertEqual(ask.prompts, [])
        self.assertEqual((answers["jev"], answers["skill_updates"]),
                         (init.Answer(False, "config"), init.Answer(True, "config")))

    def test_noninteractive_refusal_lists_missing_flags(self):
        ask = Replies("y")
        with self.assertRaises(init.InitError) as caught:
            self.resolve({"agents": True, "hooks": False}, ask=ask, interactive=False)
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("OAH_INIT_ANSWERS_REQUIRED", 2))
        message = str(caught.exception)
        for key in ALL_KEYS[2:]:
            flag = key.replace("_", "-")
            self.assertIn(f"--with-{flag} or --without-{flag}", message)
        self.assertNotIn("--with-agents", message)
        self.assertEqual(ask.prompts, [])

    def test_dry_run_uses_defaults_without_asking(self):
        answers, ask, _ = self.resolve({"ci": True}, interactive=False, dry_run=True)
        self.assertEqual(ask.prompts, [])
        self.assertEqual(answers["ci"], init.Answer(True, "flag"))
        self.assertEqual(answers["jev"], init.Answer(False, "default"))
        self.assertEqual(answers["agents"], init.Answer(True, "default"))

    def test_end_of_input_refuses(self):
        with self.assertRaises(init.InitError) as caught:
            self.resolve({}, ask=Replies("y", None))
        self.assertEqual(caught.exception.code, "OAH_INIT_NO_ANSWER")


if __name__ == "__main__":
    unittest.main()
