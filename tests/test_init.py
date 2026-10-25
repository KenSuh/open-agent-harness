# 안내형 oah init 핵심 검사 — 설정 렌더링·섹션 검증, 기본 브랜치 감지, 티어 에이전트, .gitignore 항목, 파일 계획(유지·--force 교체·위키 내용 보호)과 쓰기를 본다.
from __future__ import annotations

import datetime as dt
import os
import pathlib
import secrets as token_source
import subprocess
import unittest

from oah import call, init, intent, models, preflight, rerank, skillsel
from oah.config import parse_config
from oah.impact import defaults as impact_defaults
from oah.paths import repo_info, state_dir
from oah.wiki.base import WikiSettings
from oah.wiki.notes import parse_frontmatter
from oah.wiki.store import Wiki
from tests.helpers import directory_alias, git_commit, git_env, make_git_repo, make_tmp_dir

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
                "*.pem", "*.key", ".oah/secrets*", ".claude/settings.local.json")

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

    def test_never_writes_through_a_linked_folder(self):
        outside = make_tmp_dir(self) / "global-claude"
        outside.mkdir()
        directory_alias(outside, self.repo / ".claude")
        found = self.by_path(self.plan(force=True))
        for tier in ("low", "medium", "high", "max"):
            change = found[f".claude/agents/oah-{tier}.md"]
            self.assertEqual(change.action, "keep")
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


if __name__ == "__main__":
    unittest.main()
