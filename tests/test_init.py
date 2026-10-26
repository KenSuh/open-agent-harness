# 안내형 oah init 핵심 검사 — 설정 렌더링·섹션 검증, 기본 브랜치 감지, 티어 에이전트, .gitignore 항목, 파일 계획(유지·--force 교체·위키 내용 보호)과 쓰기를 본다.
from __future__ import annotations

import ast
import contextlib
import datetime as dt
import hashlib
import inspect
import io
import json
import os
import pathlib
import secrets as token_source
import stat
import subprocess
import sys
import unittest
from unittest import mock

from oah import call, hooks, init, intent, models, preflight, rerank, rules, skillsel, unlazy
from oah.config import parse_config
from oah.engines.proc import Resolved, RunResult
from oah.impact import defaults as impact_defaults
from oah.impact import mapstore
from oah.paths import repo_info, state_dir
from oah.setup import companions
from oah.wiki.base import WikiError, WikiSettings
from oah.wiki.notes import parse_frontmatter
from oah.wiki.store import Wiki
from tests.fixtures.gates.support import windows_sid
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

    def test_ignorecase_applies_to_existing_names_and_parents(self):
        for name, pattern in (("PRODUCTION.KEY", "*.key"),
                              (".OAH/TASKS/real/GATES.MD", ".oah/tasks/*/GATES.md"),
                              ("nested/.OBSIDIAN/WORKSPACE.JSON", "**/.obsidian/workspace*.json")):
            with self.subTest(name=name):
                repo = make_git_repo(self)
                git("config", "core.ignorecase", "true", cwd=repo)
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"# harmless fixture\r\n")
                (repo / ".gitignore").write_bytes(init.add_gitignore_entries(None, unlazy_copy=False) +
                                                 ("!" + name + "\n").encode("ascii"))
                exposed = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--", name],
                                         cwd=repo, env=git_env())
                self.assertEqual(exposed.returncode, 1)
                problems = init._ignore_problems(repo, unlazy_copy=False)
                self.assertTrue(any("required pattern " + pattern + " " in item for item in problems), problems)

    def test_case_sensitive_git_keeps_uppercase_paths_distinct(self):
        repo = make_git_repo(self)
        git("config", "core.ignorecase", "false", cwd=repo)
        (repo / "PRODUCTION.KEY").write_bytes(b"# harmless fixture\r\n")
        (repo / ".gitignore").write_bytes(init.add_gitignore_entries(None, unlazy_copy=False) +
                                         b"!PRODUCTION.KEY\n")
        self.assertEqual(init._ignore_problems(repo, unlazy_copy=False), [])


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

    def test_wiki_dir_rejects_escaping_and_windows_rooted_paths(self):
        outside = make_tmp_dir(self)
        (outside / "index.md").write_bytes(b"# Index\r\n\r\n## Notes\r\n")
        for folder in (str(outside), "../outside", "docs/../../outside", "..\\outside", "C:outside",
                       "C:/outside", "C:\\outside", "\\outside", "\\\\server\\share", ".", ""):
            with self.subTest(folder=folder):
                with self.assertRaises(init.InitError) as caught:
                    init.plan_files(self.repo, config=self.config, wiki_dir=folder, agents={}, graph=False,
                                    unlazy_copy=False, force=False, today=TODAY)
                self.assertEqual(caught.exception.code, "OAH_INIT_PATH_UNSAFE")
        self.assertEqual((outside / "index.md").read_bytes(), b"# Index\r\n\r\n## Notes\r\n")

    def test_wiki_dir_rejects_a_link_before_planning(self):
        outside = make_tmp_dir(self)
        directory_alias(outside, self.repo / "notes")
        with self.assertRaises(init.InitError) as caught:
            init.plan_files(self.repo, config=self.config, wiki_dir="notes/wiki", agents={}, graph=False,
                            unlazy_copy=False, force=False, today=TODAY)
        self.assertEqual(caught.exception.code, "OAH_INIT_PATH_UNSAFE")
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction path semantics")
    def test_windows_wiki_dir_refuses_junction_before_planning(self):
        self.test_wiki_dir_rejects_a_link_before_planning()


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

    def test_wiki_private_state_lock_publication_and_reentry(self):
        # Exercise the complete private-parent -> publish -> writable lock -> journal sequence.
        for _ in range(2):
            with init._wiki_session(self.repo, self.wiki) as check:
                check()
                self.assertTrue(self.wiki.lock_path.is_file())
                self.assertFalse(self.wiki.journal_path.exists())
                init._private_state_directory(self.wiki.state)
        with self.wiki.session():
            pass  # The landed writer must be able to acquire the very same lock afterward.

    def test_private_state_does_not_disguise_a_programming_error(self):
        with mock.patch.object(unlazy, "_verify_state_ancestry", side_effect=TypeError("wrong argument")):
            with self.assertRaises(TypeError):
                init._private_state_directory(self.repo)

    def test_wiki_opens_the_lock_without_reading_its_bytes(self):
        read = init._current

        def current(root, relative, folder):
            self.assertNotEqual(relative, ".lock", "a shared writable lock must be opened directly")
            return read(root, relative, folder)

        with mock.patch.object(init, "_current", side_effect=current):
            # Start with an existing lock so the only open is the shared writable lock open.
            unlazy._private_makedirs(self.wiki.state)
            self.wiki.lock_path.write_bytes(b"")
            with init._wiki_session(self.repo, self.wiki) as check:
                check()

    @unittest.skipUnless(os.name == "nt", "native Windows lock identity and sharing")
    def test_windows_wiki_lock_uses_handle_identity_and_checks_state_replacement(self):
        with init._wiki_session(self.repo, self.wiki) as check:
            fd = init._windows_read_fd(self.wiki.lock_path, writable=True)
            try:
                first = init._windows_file_identity(fd)
                check()
                self.assertEqual(init._windows_file_identity(fd), first)
                with self.assertRaises(PermissionError):
                    self.wiki.lock_path.rename(self.wiki.lock_path.with_name("moved-lock"))
                moved = self.wiki.state.with_name("moved-state")
                try:
                    self.wiki.state.rename(moved)
                except PermissionError:
                    pass  # The held lock can also prevent its containing directory's rename.
                else:
                    self.wiki.state.mkdir()
                    with self.assertRaises(OSError):
                        check()
                    self.wiki.state.rmdir()
                    moved.rename(self.wiki.state)
            finally:
                os.close(fd)

    def test_wiki_refuses_committable_state_before_any_publication(self):
        wiki = Wiki(self.repo, WikiSettings(), self.repo / "unplanned-state")
        with self.assertRaises(init.InitError) as caught:
            init.write_files(self.repo, self.plan(), wiki=wiki)
        self.assertEqual(caught.exception.code, "OAH_INIT_STATE_UNSAFE")
        self.assertFalse((self.repo / "unplanned-state").exists())
        self.assertFalse((self.repo / init.CONFIG_PATH).exists())

    def test_wiki_state_uses_the_same_portable_path_grammar(self):
        for name in ("NUL.txt", "folder:stream", "folder.", "folder ", "folder/../state"):
            with self.subTest(name=name):
                outside = make_tmp_dir(self)
                wiki = Wiki(self.repo, WikiSettings(), outside / name)
                with self.assertRaises(init.InitError) as caught:
                    init.write_files(self.repo, self.plan(), wiki=wiki)
                self.assertEqual(caught.exception.code, "OAH_INIT_STATE_UNSAFE")
                self.assertEqual(list(outside.iterdir()), [])
                self.assertFalse((self.repo / init.CONFIG_PATH).exists())

    def test_wiki_refuses_redirected_state_directory(self):
        unlazy._private_makedirs(self.wiki.state.parent)
        outside = make_tmp_dir(self)
        directory_alias(outside, self.wiki.state)
        with self.assertRaises(init.InitError) as caught:
            init.write_files(self.repo, self.plan(), wiki=self.wiki)
        self.assertEqual(caught.exception.code, "OAH_INIT_STATE_UNSAFE")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.repo / init.CONFIG_PATH).exists())

    @unittest.skipUnless(os.name == "nt", "native Windows state junction")
    def test_windows_wiki_refuses_state_junction(self):
        self.test_wiki_refuses_redirected_state_directory()

    @unittest.skipIf(os.name == "nt", "POSIX final-component symlinks")
    def test_wiki_refuses_linked_lock_or_journal(self):
        for name in (".lock", "rollover-journal.json"):
            with self.subTest(name=name):
                unlazy._private_makedirs(self.wiki.state)
                outside = make_tmp_dir(self) / "private"
                private = ("private-" + token_source.token_hex(16)).encode("ascii")
                outside.write_bytes(private)
                link = self.wiki.state / name
                link.symlink_to(outside)
                try:
                    with self.assertRaises(init.InitError) as caught:
                        init.write_files(self.repo, self.plan(), wiki=self.wiki)
                    self.assertEqual(caught.exception.code, "OAH_INIT_STATE_UNSAFE")
                    self.assertNotIn(private.decode("ascii"), str(caught.exception))
                    self.assertEqual(outside.read_bytes(), private)
                    self.assertFalse((self.repo / init.CONFIG_PATH).exists())
                finally:
                    link.unlink()

    def test_wiki_pending_journal_is_checked_without_disclosing_existing_bytes(self):
        unlazy._private_makedirs(self.wiki.state)
        identity = "private-" + token_source.token_hex(16)
        self.wiki.journal_path.write_bytes(json.dumps({"phase": identity, "reason": identity}).encode("ascii"))
        with self.assertRaises(WikiError) as caught:
            init.write_files(self.repo, self.plan(), wiki=self.wiki)
        self.assertEqual(caught.exception.code, "OAH_WIKI_RECOVERY_PENDING")
        self.assertNotIn(identity, str(caught.exception))
        self.assertFalse((self.repo / "docs/wiki").exists())

    def test_wiki_uses_the_landed_cross_process_lock(self):
        unlazy._private_makedirs(self.wiki.state)
        script = ("import pathlib,sys; sys.path.insert(0,sys.argv[1]); "
                  "from oah.wiki.store import Wiki; from oah.wiki.base import WikiSettings; "
                  "wiki=Wiki(pathlib.Path(sys.argv[2]),WikiSettings(),pathlib.Path(sys.argv[3])); "
                  "session=wiki.session(); session.__enter__(); print('ready',flush=True); "
                  "sys.stdin.readline(); session.__exit__(None,None,None)")
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", script,
                                    str(pathlib.Path(init.__file__).parent.parent), str(self.repo),
                                    str(self.wiki.state.parent)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline(), "ready\n")
            self.wiki.lock_timeout = 0.05
            with self.assertRaises(WikiError) as caught:
                init.write_files(self.repo, self.plan(), wiki=self.wiki)
            self.assertEqual(caught.exception.code, "OAH_WIKI_BUSY")
            self.assertFalse((self.repo / init.CONFIG_PATH).exists())
        finally:
            process.communicate("release\n", timeout=10)
        self.assertEqual(process.returncode, 0)

    @unittest.skipIf(os.name == "nt", "portable POSIX replacement permission rule")
    def test_replacement_refuses_a_permission_change_during_staging(self):
        path = self.repo / "private-settings"
        before = b"preserved settings\r\n"
        path.write_bytes(before)
        path.chmod(0o600)
        flush = os.fsync

        def change_permissions(fd):
            flush(fd)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                path.chmod(0o400)

        with mock.patch.object(os, "fsync", side_effect=change_permissions):
            self.assertFalse(init._publish(self.repo, path.name, before + b"addition\r\n", before))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)

    @unittest.skipIf(os.name == "nt", "POSIX state replacement while a directory is pinned")
    def test_wiki_rechecks_state_after_lock_acquisition(self):
        outside = make_tmp_dir(self)
        acquire = init._try_lock
        swapped = False

        def swap(fd):
            nonlocal swapped
            acquired = acquire(fd)
            if acquired and not swapped:
                swapped = True
                self.wiki.state.rename(self.wiki.state.with_name("wiki-saved"))
                directory_alias(outside, self.wiki.state)
            return acquired

        with mock.patch.object(init, "_try_lock", side_effect=swap):
            with self.assertRaises(init.InitError) as caught:
                init.write_files(self.repo, self.plan(), wiki=self.wiki)
        self.assertEqual(caught.exception.code, "OAH_INIT_STATE_UNSAFE")
        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.repo / init.CONFIG_PATH).exists())

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
    def test_windows_parent_identity_check_refuses_replacement(self):
        with self.assertRaises(OSError):
            with init._windows_parents(self.repo, init.CONFIG_PATH):
                (self.repo / ".oah").rename(self.repo / "moved")
                (self.repo / ".oah").mkdir()

    @unittest.skipUnless(os.name == "nt", "Windows publication handles")
    def test_windows_updates_recheck_parent_identity_before_publication(self):
        parent = self.repo / ".oah"
        parent.mkdir()
        target = parent / "config.toml"
        target.write_bytes(b"before\r\n")
        change = init.FileChange(init.CONFIG_PATH, "replace", "config", b"after\r\n", b"before\r\n")
        read = init._current
        changed = False

        def swap(root, relative, folder):
            nonlocal changed
            if not changed and root == self.repo and relative == init.CONFIG_PATH:
                changed = True
                parent.rename(self.repo / "moved")
                parent.mkdir()
                # Matching bytes cannot bless a different parent object.
                target.write_bytes(b"before\r\n")
            return read(root, relative, folder)

        with mock.patch.object(init, "_current", side_effect=swap):
            written, problems = init.write_files(self.repo, [change], wiki=self.wiki)
        self.assertTrue(changed)
        self.assertEqual(written, [])
        self.assertTrue(problems and problems[0].startswith("OAH_INIT_CHANGED"))
        self.assertEqual(target.read_bytes(), b"before\r\n")
        self.assertEqual((self.repo / "moved/config.toml").read_bytes(), b"before\r\n")

    @unittest.skipUnless(os.name == "nt", "Windows staging identity and safe cleanup")
    def test_windows_staging_replacement_is_neither_published_nor_cleaned(self):
        read = init._current
        swapped = []

        def swap(root, relative, folder):
            if not swapped and root == self.repo and relative == "published":
                staging, = self.repo.glob(".published.*.tmp")
                staging.rename(staging.with_name(staging.name + "-saved"))
                staging.mkdir()
                sentinel = staging / "file.tmp"
                sentinel.write_bytes(b"unrelated bytes\r\n\x00")
                swapped.append(sentinel)
            return read(root, relative, folder)

        with mock.patch.object(init, "_current", side_effect=swap):
            self.assertFalse(init._publish(self.repo, "published", b"planned bytes\r\n"))
        self.assertEqual(len(swapped), 1)
        self.assertEqual(swapped[0].read_bytes(), b"unrelated bytes\r\n\x00")
        self.assertFalse((self.repo / "published").exists())

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

    def test_updates_recheck_target_after_temporary_file_is_flushed(self):
        for action in ("update", "replace"):
            with self.subTest(action=action):
                target = self.repo / ".gitignore"
                target.write_bytes(b"before\r\n")
                change = init.FileChange(".gitignore", action, "gitignore", b"after\r\n", b"before\r\n")
                flush = os.fsync
                changed = False

                def edit(fd):
                    nonlocal changed
                    flush(fd)
                    if not changed and stat.S_ISREG(os.fstat(fd).st_mode):
                        changed = True
                        target.write_bytes(b"concurrent edit\r\n")

                with mock.patch.object(os, "fsync", side_effect=edit):
                    written, problems = init.write_files(self.repo, [change], wiki=self.wiki)
                self.assertTrue(changed)
                self.assertEqual(written, [])
                self.assertEqual(target.read_bytes(), b"concurrent edit\r\n")
                self.assertTrue(problems and problems[0].startswith("OAH_INIT_CHANGED"))

    @unittest.skipUnless(os.name != "nt", "POSIX parent replacement; Windows pins prevent the rename")
    def test_updates_refuse_parent_swap_during_publication(self):
        for action in ("update", "replace"):
            with self.subTest(action=action):
                parent = self.repo / ("parent-" + action)
                parent.mkdir()
                target = parent / "file.md"
                target.write_bytes(b"before\n")
                outside = make_tmp_dir(self)
                (outside / "file.md").write_bytes(b"outside\n")
                change = init.FileChange(f"{parent.name}/file.md", action, "templates", b"after\n", b"before\n")
                flush = os.fsync
                swapped = False

                def swap(fd):
                    nonlocal swapped
                    flush(fd)
                    if not swapped and stat.S_ISREG(os.fstat(fd).st_mode):
                        swapped = True
                        parent.rename(parent.with_name(parent.name + "-old"))
                        directory_alias(outside, parent)

                with mock.patch.object(os, "fsync", side_effect=swap):
                    written, problems = init.write_files(self.repo, [change], wiki=self.wiki)
                self.assertTrue(swapped)
                self.assertEqual((outside / "file.md").read_bytes(), b"outside\n")
                self.assertEqual(written, [])
                self.assertTrue(problems and problems[0].startswith("OAH_INIT_CHANGED"))

    def test_publication_rejects_nonlocal_paths_for_every_action(self):
        outside = make_tmp_dir(self) / "file.md"
        outside.write_bytes(b"before\n")
        for path in (str(outside), "../file.md", "C:relative.md", "C:/file.md", "..\\file.md", "\\file.md",
                     "NUL.txt", "dir./file.md", "dir /file.md", "dir/file.md:stream"):
            for action in ("create", "replace", "update"):
                with self.subTest(path=path, action=action):
                    change = init.FileChange(path, action, "templates", b"after\n",
                                             None if action == "create" else b"before\n")
                    written, problems = init.write_files(self.repo, [change], wiki=self.wiki)
                    self.assertEqual(written, [])
                    self.assertTrue(problems and problems[0].startswith("OAH_INIT_CHANGED"))
        self.assertEqual(outside.read_bytes(), b"before\n")



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

    @unittest.skipIf(os.name == "nt", "file symlinks need Windows developer privileges")
    def test_linked_local_rules_never_enter_agents(self):
        outside = make_tmp_dir(self) / "private.txt"
        secret = ("fake-" + token_source.token_hex(20)).encode("ascii")
        outside.write_bytes(secret)
        (self.repo / ".oah").mkdir()
        (self.repo / rules.LOCAL_RULES).symlink_to(outside)
        with self.assertRaises(init.InitError) as caught:
            init._sync_rules(self.repo, init.plan_rules(self.repo, lang=None))
        self.assertEqual(caught.exception.code, "OAH_INIT_PATH_UNSAFE")
        self.assertNotIn(secret.decode("ascii"), str(caught.exception))
        self.assertFalse((self.repo / "AGENTS.md").exists())

    def test_local_rules_parent_link_is_rejected(self):
        outside = make_tmp_dir(self)
        (outside / "rules.local.md").write_bytes(b"private source\r\n")
        directory_alias(outside, self.repo / ".oah")
        with self.assertRaises(init.InitError):
            init.plan_rules(self.repo, lang=None)
        self.assertFalse((self.repo / "AGENTS.md").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction path semantics")
    def test_windows_local_rules_parent_junction_is_rejected(self):
        self.test_local_rules_parent_link_is_rejected()

    def test_rules_planning_reads_nothing_through_path_read_bytes(self):
        (self.repo / ".oah").mkdir()
        (self.repo / rules.LOCAL_RULES).write_bytes(b"Use tabs.\r\n")
        (self.repo / "AGENTS.md").write_bytes(b"# Project\r\n")
        (self.repo / "CLAUDE.md").write_bytes(b"# Claude\r\n")
        # Landed pure rendering stays real; user reads must use init's pinned handles.
        real_read = pathlib.Path.read_bytes

        def read(path):
            if path.is_relative_to(self.repo):
                self.fail("unprotected user-file read")
            return real_read(path)

        with mock.patch.object(pathlib.Path, "read_bytes", read):
            step = init.plan_rules(self.repo, lang=None)
        self.assertIn(b"Use tabs.\r\n", step.planned.agents_after)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor-relative no-follow race")
    def test_read_refuses_a_local_rules_link_created_during_open(self):
        (self.repo / ".oah").mkdir()
        local = self.repo / rules.LOCAL_RULES
        local.write_bytes(b"Use tabs.\r\n")
        outside = make_tmp_dir(self) / "secret"
        outside.write_bytes(("fake-" + token_source.token_hex(20)).encode("ascii"))
        real_open = os.open
        swapped = False

        def swap(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == "rules.local.md" and not swapped:
                swapped = True
                local.unlink()
                local.symlink_to(outside)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(os, "open", side_effect=swap), self.assertRaises(init.InitError):
            init.plan_rules(self.repo, lang=None)
        self.assertTrue(swapped)
        self.assertFalse((self.repo / "AGENTS.md").exists())

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


class WindowsSecurityTest(unittest.TestCase):
    def setUp(self):
        # Substitute only the native SID APIs off Windows; the landed DACL verifier stays real.
        if os.name != "nt":
            self.enterContext(mock.patch.object(unlazy, "_windows_canonical_sid", side_effect=windows_sid))
            self.enterContext(mock.patch.object(unlazy, "_windows_user_sid",
                                               return_value="S-1-5-21-100-200-300-1001"))
        self.sid = unlazy._windows_user_sid()

    def test_windows_directory_pins_share_read_write_and_delete(self):
        # Check the real native call arguments, without mocking Windows filesystem semantics.
        for function in (init._pinned_directory,):
            tree = ast.parse(inspect.getsource(function))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == "create"]
            self.assertTrue(calls)
            for call in calls:
                with self.subTest(function=function.__name__):
                    share = compile(ast.Expression(call.args[2]), "<share mask>", "eval")
                    self.assertEqual(eval(share, {"__builtins__": {}}), 7)
        self.assertIn("pins.enter_context(_pinned_directory(path))", inspect.getsource(init._windows_parents))

    def test_windows_security_is_set_only_at_creation(self):
        tree = ast.parse(inspect.getsource(init))
        setters = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                   and node.attr in ("SetSecurityInfo", "SetFileSecurityW", "SetKernelObjectSecurity")]
        self.assertEqual(setters, [], "existing directory and file DACLs must never be rewritten")
        source = inspect.getsource(init._file_staging)
        self.assertIn("unlazy._private_directory(directory)", source)
        self.assertNotIn("_windows_file_security", source)
        self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr == "CreateDirectoryW"
                             for node in ast.walk(tree)), "private directories use the landed creator")
        creator = ast.parse(inspect.getsource(init._windows_file_security))
        calls = [node for node in ast.walk(creator) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "opener"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.literal_eval(calls[0].args[4]), 1, "CREATE_NEW is the only file creation path")
        self.assertIsInstance(calls[0].args[3], ast.Call, "SECURITY_ATTRIBUTES accompany creation")

    def test_private_windows_verification_reuses_landed_trusted_owner_rules(self):
        # Read-back ownership and inheritance spelling do not establish a new privacy policy.
        for owner in (self.sid, "BA", "SY"):
            for principal in ("OW", owner):
                descriptor = f"O:{owner}D:PAI(A;OICIID;FA;;;{principal})"
                with self.subTest(owner_kind="user" if owner == self.sid else owner, owner_rights=principal == "OW"):
                    init._verify_windows_private_descriptor(descriptor, directory=True)
        for descriptor in (f"O:WDD:P(A;OICI;FA;;;OW)",
                           f"O:{self.sid}D:(A;OICI;FA;;;OW)",
                           f"O:{self.sid}D:P(A;OICI;FA;;;OW)(A;;FR;;;WD)"):
            with self.assertRaises(OSError) as caught:
                init._verify_windows_private_descriptor(descriptor, directory=True)
            self.assertNotIn("S-1-", str(caught.exception), "permission errors must not expose account SIDs")

    @unittest.skipUnless(os.name == "nt", "native Windows publication sharing and creation")
    def test_windows_publication_uses_landed_private_directory_creation_without_rewriting(self):
        repo = make_git_repo(self)
        # Real helpers and real files: all temporary directories must originate in the landed creator.
        with mock.patch.object(unlazy, "_private_directory", wraps=unlazy._private_directory) as create:
            for name in (".git/hooks/c14", ".github/workflows/oah-ci.yml"):
                self.assertTrue(init._publish(repo, name, b"fixture\r\n\x00"))
                self.assertTrue(init._publish(repo, name, b"replacement\r\n", b"fixture\r\n\x00"))
                self.assertEqual((repo / name).read_bytes(), b"replacement\r\n")
        self.assertEqual(create.call_count, 4)

    def test_windows_security_accepts_trusted_owners_and_canonical_ace_principals(self):
        expected = (f"O:{self.sid}D:P(D;;GX;;;WD)(A;OICI;FA;;;{self.sid})"
                    "(A;;FR;;;SY)(A;;FR;;;BA)(A;IO;FR;;;OW)")
        aces = (f"(D;;GX;;;S-1-1-0)(A;OICIID;FA;;;{self.sid})"
                "(A;;FR;;;S-1-5-18)(A;;FR;;;S-1-5-32-544)(A;IO;FR;;;S-1-3-4)")
        for index, owner in enumerate((self.sid, "BA", "S-1-5-32-544", "SY", "S-1-5-18",
                                       "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")):
            with self.subTest(case=index):
                actual = f"O:{owner}D:PAI{aces}"
                self.assertTrue(init._windows_security_matches(actual, expected))
                self.assertTrue(init._windows_security_matches(expected, actual))

    def test_windows_security_refuses_foreign_owners_extra_grants_and_changed_aces(self):
        expected = f"O:{self.sid}D:P(D;;GX;;;WD)(A;OICI;FA;;;{self.sid})"
        for index, actual in enumerate((expected.replace(f"O:{self.sid}", "O:WD"),
                                        expected.replace("D:P", "D:"), expected + "(A;;FR;;;WD)",
                                        expected + "(A;;FR;;;BA)", expected.replace("GX", "GW"),
                                        expected.replace("D;;", "D;IO;"), expected.replace("OICI", "OI"),
                                        f"O:{self.sid}D:P(A;OICI;FA;;;{self.sid})(D;;GX;;;WD)",
                                        expected.replace(f";;;{self.sid})", ";;;BA)"),
                                        expected.replace("GX", "ZZ"), expected + "unparsed")):
            with self.subTest(case=index):
                self.assertFalse(init._windows_security_matches(actual, expected))
        # An untrusted expected owner cannot bless a read-back descriptor either.
        self.assertFalse(init._windows_security_matches(expected, expected.replace(f"O:{self.sid}", "O:WD")))

    def test_windows_security_compares_numeric_and_named_rights(self):
        expected = f"O:{self.sid}D:P(A;;FA;;;{self.sid})(A;;FRFX;;;SY)"
        actual = f"O:BAD:P(A;;0x1f01ff;;;{self.sid})(A;;0x1200a9;;;S-1-5-18)"
        self.assertTrue(init._windows_security_matches(actual, expected))
        self.assertFalse(init._windows_security_matches(actual.replace("0x1200a9", "0x1200ab"), expected))

    def test_frozen_windows_dacl_compares_permissions_without_inheritance_bookkeeping(self):
        before = f"O:{self.sid}D:AI(D;ID;GX;;;WD)(A;ID;FA;;;OW)"
        frozen = f"O:{self.sid}D:PAI(D;;GX;;;WD)(A;;FA;;;OW)"
        self.assertTrue(init._windows_security_matches(frozen, before))
        for index, changed in enumerate((frozen.replace("D:PAI", "D:AI"), frozen.replace("(D;;GX;;;WD)", ""),
                                         frozen + "(A;;FR;;;WD)", frozen.replace("GX", "GW"),
                                         frozen.replace(f"O:{self.sid}", "O:WD"),
                                         frozen.replace("D;;", "D;IO;"), frozen.replace("A;;", "A;OICI;"))):
            with self.subTest(case=index):
                self.assertFalse(init._windows_security_matches(changed, before))

    @unittest.skipUnless(os.name == "nt", "native Windows trusted-owner private state and publication")
    def test_windows_private_state_accepts_readback_owner_without_changing_its_dacl(self):
        repo = make_git_repo(self)
        common = repo_info(repo).common_dir
        destination = common / "oah/backups/run/hook"
        unlazy._private_makedirs(destination.parent)
        before = unlazy._windows_security_descriptor(destination.parent)
        init._private_state_directory(destination.parent)
        self.assertTrue(init._publish(common, "oah/backups/run/hook", b"private\r\n\x00fixture", private=True))
        self.assertEqual(destination.read_bytes(), b"private\r\n\x00fixture")
        self.assertTrue(unlazy._windows_security_descriptor(destination.parent) == before,
                        "the existing directory DACL must remain byte-identical")


class HookStepTest(unittest.TestCase):
    def test_windows_creation_requests_no_dacl_update_access(self):
        for function, opener, expected in ((init._pinned_directory, "create", 0x81),
                                            (init._windows_file_security, "opener", 0x40000000)):
            tree = ast.parse(inspect.getsource(function))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == opener]
            self.assertEqual(len(calls), 1)
            expression = compile(ast.Expression(calls[0].args[1]), "<access mask>", "eval")
            self.assertEqual(eval(expression, {"__builtins__": {}}), expected)
            self.assertEqual(expected & 0xE0000, 0, "no READ_CONTROL, WRITE_DAC or WRITE_OWNER updates")

    @unittest.skipUnless(os.name == "nt", "native Windows security API error codes")
    def test_windows_security_failures_keep_numeric_errors_without_paths(self):
        path = self.repo / "private-staging"
        sid = unlazy._windows_user_sid()
        descriptor = f"O:{sid}D:P(A;;FA;;;OW)"
        fd = init._windows_file_security(path, descriptor)
        os.close(fd)
        with self.assertRaises(OSError) as caught:
            init._windows_file_security(path, descriptor)
        error = caught.exception
        self.assertEqual(error.errno, 80)
        self.assertIsNone(error.filename)
        self.assertNotIn(str(path), str(error))
        self.assertFalse(sid in str(error), "security error exposed an account identifier")

    @unittest.skipUnless(os.name == "nt", "native Windows descriptor security at creation")
    def test_windows_staging_security_is_final_at_creation(self):
        with init._parents(self.repo, "published") as parent:
            with init._file_staging(self.repo / "published", parent) as (temp, folder):
                init._verify_windows_private_descriptor(
                    unlazy._windows_security_descriptor(temp.parent), directory=True)
                sid = unlazy._windows_user_sid()
                denied = f"O:{sid}D:P(D;;CC;;;WD)(A;;FA;;;OW)"
                fd = init._private_file(temp, folder, security=denied)
                try:
                    self.assertEqual(os.fstat(fd).st_size, 0)
                    self.assertTrue(init._windows_security_matches(unlazy._windows_security_descriptor(temp), denied))
                finally:
                    os.close(fd)
                    temp.unlink()

    @unittest.skipUnless(os.name == "nt", "native Windows inherited target DACL preservation")
    def test_windows_replacement_freezes_the_existing_effective_dacl(self):
        parent = self.repo / "settings"
        parent.mkdir()
        subprocess.run(["icacls.exe", str(parent), "/grant", "*S-1-1-0:(OI)(CI)R"],
                       check=True, capture_output=True)
        path = parent / "local.json"
        before = b'{"setting":"before"}\r\n'
        after = b'{"setting":"after"}\r\n'
        path.write_bytes(before)
        original = unlazy._windows_security_descriptor(path)
        self.assertTrue("ID" in original, "the fixture must have inherited ACEs")
        self.assertTrue(init._publish(self.repo, "settings/local.json", after, before))
        self.assertEqual(path.read_bytes(), after)
        self.assertTrue(init._windows_security_matches(unlazy._windows_security_descriptor(path), original))

    def test_publication_preserves_exact_bytes_for_replacement_and_private_backup(self):
        path = self.repo / "settings"
        before = b"private fixture\r\n\x00no final newline"
        path.write_bytes(before)
        common = repo_info(self.repo).common_dir
        self.assertTrue(init._publish(common, "oah/backups/run/1", before, private=True))
        after = before + b"\r\noah addition\r\n"
        self.assertTrue(init._publish(self.repo, path.name, after, before))
        self.assertEqual(path.read_bytes(), after)
        self.assertEqual((common / "oah/backups/run/1").read_bytes(), before)

    def test_publication_creates_empty_files_only_in_private_staging_directory(self):
        create = init._private_file
        observed = []
        existing = sorted(p.name for p in self.repo.iterdir())

        def private_file(temp, folder, **kwargs):
            self.assertNotEqual(temp.parent, self.repo)
            self.assertTrue(temp.parent.is_relative_to(self.repo))
            if os.name == "nt":
                descriptor = unlazy._windows_security_descriptor(temp.parent)
                self.assertIn("D:P", descriptor)
                init._verify_windows_private_descriptor(descriptor, directory=True)
            else:
                self.assertEqual(stat.S_IMODE(os.fstat(folder).st_mode), 0o700)
                if sys.platform == "darwin":
                    self.assertIn(init._macos_file_acl(folder).strip(), (b"", b"!#acl 1"))
            observed.append(temp)
            return create(temp, folder, **kwargs)

        with mock.patch.object(init, "_private_file", side_effect=private_file):
            self.assertTrue(init._publish(self.repo, "published", b"known bytes\r\n"))
        self.assertTrue(observed)
        self.assertEqual((self.repo / "published").read_bytes(), b"known bytes\r\n")
        self.assertEqual(sorted(p.name for p in self.repo.iterdir()), sorted([*existing, "published"]))

    @unittest.skipUnless(sys.platform == "darwin", "native macOS descriptor ACL race")
    def test_macos_staging_path_replacement_preserves_unrelated_acls(self):
        temp = self.repo / "empty-staging"
        unrelated = self.repo / "unrelated"
        unrelated.mkdir()
        child = unrelated / "keep.bin"
        child.write_bytes(b"unrelated bytes\r\n\x00")
        subprocess.run(["/bin/chmod", "+a", "everyone deny execute", str(child)],
                       check=True, capture_output=True)
        acl = subprocess.run(["/bin/ls", "-lde", str(child)], check=True, capture_output=True).stdout.splitlines()[1:]
        opener = os.open
        saved = temp.with_name("held-staging")

        def swap(path, flags, *args, **kwargs):
            fd = opener(path, flags, *args, **kwargs)
            if path == temp.name and flags & os.O_CREAT:
                temp.rename(saved)
                unrelated.rename(temp)
            return fd

        with init._parents(self.repo, temp.name) as folder:
            with mock.patch.object(os, "open", side_effect=swap):
                fd = init._private_file(temp, folder)
            os.close(fd)
        actual = subprocess.run(["/bin/ls", "-lde", str(temp / child.name)],
                                check=True, capture_output=True).stdout.splitlines()[1:]
        self.assertEqual(actual, acl)
        self.assertEqual((temp / child.name).read_bytes(), b"unrelated bytes\r\n\x00")

    @unittest.skipUnless(sys.platform == "darwin", "native macOS replacement ACLs")
    def test_macos_settings_replacement_removes_inheritance_before_preserved_bytes(self):
        self._settings_replacement_acl()

    @unittest.skipUnless(os.name == "nt", "native Windows replacement DACLs")
    def test_windows_settings_replacement_removes_inheritance_before_preserved_bytes(self):
        self._settings_replacement_acl()

    def _settings_replacement_acl(self):
        path = self.repo / init.CLAUDE_SETTINGS
        path.parent.mkdir()
        original = b'{"env":{"PRIVATE_VALUE":"fixture"}}\r\n'
        if os.name == "nt":
            subprocess.run(["icacls.exe", str(path.parent), "/grant", "*S-1-1-0:(OI)(CI)R"],
                           check=True, capture_output=True)
        else:
            subprocess.run(["/bin/chmod", "+a", "everyone allow read,readattr,readsecurity,file_inherit",
                            str(path.parent)], check=True, capture_output=True)
            self.addCleanup(subprocess.run, ["/bin/chmod", "-RN", str(path.parent)],
                            check=True, capture_output=True)
        path.write_bytes(original)
        if os.name == "nt":
            sid = unlazy._windows_user_sid()
            subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"],
                           check=True, capture_output=True)
        else:
            subprocess.run(["/bin/chmod", "-N", str(path)], check=True, capture_output=True)
            # An explicit denial must survive as well as the owner-only mode.
            subprocess.run(["/bin/chmod", "+a", "everyone deny execute", str(path)],
                           check=True, capture_output=True)
            path.chmod(0o600)
        opened = os.fdopen
        observed = []

        def check_permissions(fd, mode, *args, **kwargs):
            stream = opened(fd, mode, *args, **kwargs)
            if mode == "wb":
                # This callback runs before _publish can write the preserved settings bytes.
                for temp in path.parent.glob(".*.tmp/file.tmp"):
                    self.assertEqual(temp.stat().st_size, 0)
                    if os.name == "nt":
                        descriptor = unlazy._windows_security_descriptor(temp)
                        self.assertIn("D:P", descriptor)
                        self.assertFalse(";;;WD)" in descriptor or ";;;S-1-1-0)" in descriptor)
                    else:
                        acl = subprocess.run(["/bin/ls", "-lde", str(temp)], check=True,
                                             capture_output=True).stdout.splitlines()
                        self.assertEqual(len(acl), 1, "staging inherited an access grant")
                        self.assertEqual(stat.S_IMODE(temp.stat().st_mode), 0o600)
                    observed.append(temp)
            return stream

        with mock.patch.object(os, "fdopen", side_effect=check_permissions):
            installed, problems = init.install_hooks(self.repo, list(self.steps().values()), force=False)
        self.assertEqual(problems, [])
        self.assertIn("stop", installed)
        self.assertTrue(observed)
        self.assertEqual(json.loads(path.read_bytes())["env"], {"PRIVATE_VALUE": "fixture"})
        if os.name == "nt":
            descriptor = unlazy._windows_security_descriptor(path)
            self.assertIn("D:P", descriptor)
            self.assertFalse(";;;WD)" in descriptor or ";;;S-1-1-0)" in descriptor)
        else:
            acl = subprocess.run(["/bin/ls", "-lde", str(path)], check=True, capture_output=True).stdout
            self.assertIn(b"deny execute", acl)
            self.assertNotIn(b"allow", acl)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

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

    def test_forced_git_hook_diff_only_contains_canonical_additions(self):
        secret = "fake-" + token_source.token_hex(20)
        original = ("#!/bin/sh\r\n# Authorization: Bearer " + secret + "\r\n").encode("utf-8")
        path = hooks.git_hook_path(self.repo, "pre-push")
        path.write_bytes(original)
        step = self.steps(force=True)["pre-push"]
        self.assertNotIn(secret, repr(step))
        self.assertNotIn("Authorization", step.diff)
        self.assertIn("oah hook pre-push", step.diff)
        self.assertIn(f"replaces {len(original)} bytes (sha256 {hashlib.sha256(original).hexdigest()})", step.note)
        self.assertEqual(path.read_bytes(), original)

    def test_local_settings_backup_is_outside_the_worktree(self):
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
        self.assertFalse((self.repo / backup).exists())
        backups = list((repo_info(self.repo).common_dir / "oah/backups").glob("*/*"))
        self.assertEqual([item.read_bytes() for item in backups], [original])

    def test_forced_hook_backup_never_enters_the_worktree_or_index(self):
        git("config", "core.hooksPath", ".githooks", cwd=self.repo)
        path = self.repo / ".githooks/pre-push"
        path.parent.mkdir()
        secret = ("fake-" + token_source.token_hex(20)).encode("ascii")
        original = b"#!/bin/sh\r\n# " + secret + b"\r\n"
        path.write_bytes(original)
        ignore = self.repo / ".gitignore"
        ignore.write_bytes(ignore.read_bytes() + b".githooks/pre-push\n")
        step = self.steps(force=True)["pre-push"]
        installed, problems = init.install_hooks(self.repo, [step], force=True)
        self.assertEqual((installed, problems), (["pre-push"], []))
        self.assertFalse(path.with_name(path.name + hooks.BACKUP_SUFFIX).exists())
        backups = list((repo_info(self.repo).common_dir / "oah/backups").glob("*/*"))
        self.assertEqual([backup.read_bytes() for backup in backups], [original])
        git("add", "-A", cwd=self.repo)
        self.assertFalse(any(".oah.bak" in name for name in git("ls-files", cwd=self.repo).splitlines()))
        self.assertNotIn(secret.decode("ascii"), repr(step))

    def test_hook_backup_refuses_a_linked_state_folder(self):
        path = hooks.git_hook_path(self.repo, "pre-push")
        original = b"#!/bin/sh\r\n# private fixture\r\n"
        path.write_bytes(original)
        step = self.steps(force=True)["pre-push"]
        outside = make_tmp_dir(self)
        directory_alias(outside, repo_info(self.repo).common_dir / "oah")
        installed, problems = init.install_hooks(self.repo, [step], force=True)
        self.assertEqual(installed, [])
        self.assertTrue(problems)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(outside.iterdir()), [])

    def test_forced_hook_keeps_the_landed_legacy_backup_refusal(self):
        path = hooks.git_hook_path(self.repo, "pre-push")
        path.write_bytes(b"#!/bin/sh\r\n# foreign fixture\r\n")
        path.with_name(path.name + hooks.BACKUP_SUFFIX).write_bytes(b"legacy fixture\r\n")
        step = self.steps(force=True)["pre-push"]
        self.assertEqual(step.action, "refused")
        self.assertIn("OAH_HOOK_BACKUP_EXISTS", step.note)

    def test_hook_conflict_reports_the_private_backup_position_and_digest(self):
        path = hooks.git_hook_path(self.repo, "pre-push")
        original = b"#!/bin/sh\r\n# foreign fixture\r\n"
        path.write_bytes(original)
        step = self.steps(force=True)["pre-push"]
        flush = os.fsync

        def edit(fd):
            flush(fd)
            path.write_bytes(b"#!/bin/sh\r\n# concurrent edit\r\n")

        with mock.patch.object(os, "fsync", side_effect=edit):
            installed, problems = init.install_hooks(self.repo, [step], force=True)
        self.assertEqual(installed, [])
        self.assertIn(step.backup, "\n".join(problems))
        self.assertIn(hashlib.sha256(original).hexdigest(), "\n".join(problems))
        self.assertEqual((repo_info(self.repo).common_dir / "oah/backups" / step.backup).read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_hook_backup_refuses_a_public_run_directory(self):
        path = hooks.git_hook_path(self.repo, "pre-push")
        original = b"#!/bin/sh\n# private fixture\n"
        path.write_bytes(original)
        step = self.steps(force=True)["pre-push"]
        destination = repo_info(self.repo).common_dir / "oah/backups" / step.backup
        destination.parent.mkdir(parents=True, mode=0o755)
        destination.parent.chmod(0o755)
        installed, problems = init.install_hooks(self.repo, [step], force=True)
        self.assertEqual(installed, [])
        self.assertTrue(problems)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(destination.exists())

    @unittest.skipIf(os.name == "nt", "portable POSIX owner-only refusal; Windows DACL cases below")
    def test_private_state_adapter_refuses_existing_public_directory(self):
        folder = make_tmp_dir(self) / "state"
        folder.mkdir(mode=0o700)
        folder.chmod(0o755)
        with self.assertRaises(init.InitError) as caught:
            init._private_state_directory(folder)
        self.assertEqual(caught.exception.code, "OAH_INIT_STATE_UNSAFE")
        self.assertNotIn(str(folder), str(caught.exception))
        self.assertIn("rerun", str(caught.exception))
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755)

    @unittest.skipIf(os.name == "nt", "POSIX ancestry permissions")
    def test_backup_refuses_writable_state_ancestor(self):
        common = repo_info(self.repo).common_dir
        common.chmod(0o777)
        self.addCleanup(common.chmod, 0o755)
        self.assertFalse(init._publish(common, "oah/backups/run/hook", b"private\r\n", private=True))
        self.assertFalse((common / "oah/backups/run/hook").exists())

    @unittest.skipUnless(sys.platform == "darwin", "native macOS state ACLs")
    def test_macos_backup_refuses_existing_acl_grants(self):
        for position in ("oah", "backups", "run"):
            with self.subTest(position=position):
                repo = make_git_repo(self)
                common = repo_info(repo).common_dir
                destination = common / "oah/backups/run/hook"
                unlazy._private_makedirs(destination.parent)
                directory = {"oah": common / "oah", "backups": common / "oah/backups",
                             "run": destination.parent}[position]
                subprocess.run(["/bin/chmod", "+a", "everyone allow list,search,readattr,readsecurity",
                                str(directory)], check=True, capture_output=True)
                acl = subprocess.run(["/bin/ls", "-lde", str(directory)], check=True, capture_output=True).stdout
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                self.assertFalse(init._publish(common, "oah/backups/run/hook", b"private\r\n", private=True))
                self.assertFalse(destination.exists())
                self.assertEqual(subprocess.run(["/bin/ls", "-lde", str(directory)], check=True,
                                                capture_output=True).stdout, acl)

    @unittest.skipUnless(sys.platform == "darwin", "native macOS inherited ACL refusal")
    def test_macos_new_private_state_refuses_inherited_access(self):
        parent = make_tmp_dir(self)
        subprocess.run(["/bin/chmod", "+a", "everyone allow list,search,file_inherit,directory_inherit",
                        str(parent)], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["/bin/chmod", "-RN", str(parent)], check=True, capture_output=True)
        folder = parent / "state"
        with self.assertRaises(init.InitError) as caught:
            init._private_state_directory(folder, create=True)
        self.assertEqual(caught.exception.code, "OAH_INIT_STATE_UNSAFE")
        self.assertNotIn(str(parent), str(caught.exception))
        self.assertFalse(folder.exists())

    @unittest.skipUnless(sys.platform == "darwin", "native macOS inherited backup ACL refusal")
    def test_macos_backup_refuses_inherited_access_before_writing(self):
        common = repo_info(self.repo).common_dir
        subprocess.run(["/bin/chmod", "+a", "everyone allow list,search,file_inherit,directory_inherit",
                        str(common)], check=True, capture_output=True)
        self.assertFalse(init._publish(common, "oah/backups/run/hook", b"private\r\n", private=True))
        self.assertFalse((common / "oah/backups/run/hook").exists())

    @unittest.skipUnless(os.name == "nt", "native Windows protected owner-only DACL creation")
    def test_windows_private_directories_do_not_inherit_public_access(self):
        common = repo_info(self.repo).common_dir
        subprocess.run(["icacls.exe", str(common), "/grant", "*S-1-1-0:(OI)(CI)R"],
                       check=True, capture_output=True)
        path = hooks.git_hook_path(self.repo, "pre-push")
        original = b"#!/bin/sh\r\n# original hook\r\n"
        path.write_bytes(original)
        step = self.steps(force=True)["pre-push"]
        installed, problems = init.install_hooks(self.repo, [step], force=True)
        self.assertEqual(problems, [])
        self.assertEqual(installed, ["pre-push"])
        destination = common / "oah/backups" / step.backup
        self.assertEqual(destination.read_bytes(), original)
        for directory in (common / "oah", common / "oah/backups", destination.parent):
            descriptor = unlazy._windows_security_descriptor(directory)
            self.assertTrue("D:P" in descriptor, "state DACL must be protected")
            self.assertFalse(";;;WD)" in descriptor or ";;;S-1-1-0)" in descriptor,
                             "state DACL must exclude public access")
            init._private_state_directory(directory)

    @unittest.skipUnless(os.name == "nt", "native Windows existing private-state ACL refusal")
    def test_windows_hook_backup_refuses_existing_inherited_public_acl(self):
        common = repo_info(self.repo).common_dir
        subprocess.run(["icacls.exe", str(common), "/grant", "*S-1-1-0:(OI)(CI)R"],
                       check=True, capture_output=True)
        path = hooks.git_hook_path(self.repo, "pre-push")
        original = b"#!/bin/sh\r\n# keep original hook\r\n"
        path.write_bytes(original)
        step = self.steps(force=True)["pre-push"]
        destination = common / "oah/backups" / step.backup
        destination.parent.mkdir(parents=True)  # Ordinary mkdir inherits the permissive metadata ACL.
        descriptor = unlazy._windows_security_descriptor(destination.parent)
        installed, problems = init.install_hooks(self.repo, [step], force=True)
        self.assertEqual(installed, [])
        self.assertTrue(problems and problems[0].startswith("OAH_INIT_"), problems)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(destination.exists())
        self.assertTrue(unlazy._windows_security_descriptor(destination.parent) == descriptor,
                        "init must leave the inherited DACL unchanged")

    @unittest.skipUnless(os.name == "nt", "native Windows existing private-state ACL refusal")
    def test_windows_hook_backup_refuses_existing_public_state_directories(self):
        path = hooks.git_hook_path(self.repo, "pre-push")
        original = b"#!/bin/sh\r\n# keep original hook\r\n"
        path.write_bytes(original)
        for position in ("oah", "backups", "run"):
            with self.subTest(position=position):
                self.repo = make_git_repo(self)
                (self.repo / ".gitignore").write_bytes(init.add_gitignore_entries(None, unlazy_copy=False))
                path = hooks.git_hook_path(self.repo, "pre-push")
                path.write_bytes(original)
                step = self.steps(force=True)["pre-push"]
                common = repo_info(self.repo).common_dir
                destination = common / "oah/backups" / step.backup
                unlazy._private_makedirs(destination.parent)
                directory = {"oah": common / "oah", "backups": common / "oah/backups",
                             "run": destination.parent}[position]
                subprocess.run(["icacls.exe", str(directory), "/grant", "*S-1-1-0:(OI)(CI)R"],
                               check=True, capture_output=True)
                descriptor = unlazy._windows_security_descriptor(directory)
                installed, problems = init.install_hooks(self.repo, [step], force=True)
                self.assertEqual(installed, [])
                self.assertTrue(problems and problems[0].startswith("OAH_INIT_"), problems)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(destination.exists())
                self.assertTrue(unlazy._windows_security_descriptor(directory) == descriptor,
                                "init must leave the existing DACL unchanged")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_private_settings_and_staging_keep_private_modes(self):
        path = self.repo / init.CLAUDE_SETTINGS
        path.parent.mkdir()
        original = b'{"env":{"PRIVATE_VALUE":"fixture"}}\r\n'
        path.write_bytes(original)
        path.chmod(0o600)
        steps = list(self.steps().values())
        opened = []
        real_open = os.open

        def record(name, flags, mode=0o777, **kwargs):
            fd = real_open(name, flags, mode, **kwargs)
            if flags & os.O_CREAT:
                opened.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return fd

        old_umask = os.umask(0o022)
        try:
            with mock.patch.object(os, "open", side_effect=record):
                installed, problems = init.install_hooks(self.repo, steps, force=False)
        finally:
            os.umask(old_umask)
        self.assertEqual(problems, [])
        self.assertIn("stop", installed)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(opened)
        self.assertEqual(set(opened), {0o600})
        backups = list((repo_info(self.repo).common_dir / "oah/backups").glob("*/*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backups[0].parent.stat().st_mode), 0o700)

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

    def test_linked_worktree_requires_project_local_hooks(self):
        git_commit(self.repo, {"README.md": "hello\n"})
        worktree = make_tmp_dir(self) / "linked"
        git("worktree", "add", "--detach", str(worktree), cwd=self.repo)
        steps = {step.kind: step for step in init.plan_hooks(worktree, force=False)}
        self.assertEqual(steps["pre-commit"].action, "refused")
        git("config", "core.hooksPath", ".githooks", cwd=worktree)
        steps = {step.kind: step for step in init.plan_hooks(worktree, force=False)}
        self.assertEqual(steps["pre-commit"].action, "install")
        installed, problems = init.install_hooks(worktree, list(steps.values()), force=False)
        self.assertIn("pre-commit", installed)
        self.assertFalse([p for p in problems if "pre-commit" in p])

    def test_project_local_hook_directory_cannot_be_a_link(self):
        outside = make_tmp_dir(self)
        directory_alias(outside, self.repo / ".githooks")
        git("config", "core.hooksPath", ".githooks", cwd=self.repo)
        self.assertEqual(self.steps(force=True)["pre-commit"].action, "refused")

    def test_hooks_keep_a_concurrent_target_edit(self):
        path = hooks.git_hook_path(self.repo, "pre-push")
        step = self.steps()["pre-push"]
        flush = os.fsync
        changed = False

        def edit(fd):
            nonlocal changed
            flush(fd)
            if not changed and stat.S_ISREG(os.fstat(fd).st_mode):
                changed = True
                path.write_bytes(b"#!/bin/sh\n# concurrent\n")

        with mock.patch.object(os, "fsync", side_effect=edit):
            installed, problems = init.install_hooks(self.repo, [step], force=False)
        self.assertEqual(installed, [])
        self.assertTrue(problems and problems[0].startswith("OAH_INIT_CHANGED"))
        self.assertEqual(path.read_bytes(), b"#!/bin/sh\n# concurrent\n")


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

    def test_staging_identity_change_on_pin_exit_reports_changed(self):
        pinned = init._pinned_directory
        common = repo_info(self.repo).common_dir
        depth = 0
        swapped = []

        @contextlib.contextmanager
        def directory(path):
            nonlocal depth
            is_staging = path.parent == common / "oah/staging"
            with pinned(path) as held:
                if is_staging:
                    depth += 1
                try:
                    yield held
                    if is_staging and depth == 1:
                        path.rename(path.with_name(path.name + "-saved"))
                        path.mkdir()
                        swapped.append(path)
                        init._require_staging(path, held[1])
                finally:
                    if is_staging:
                        depth -= 1

        with mock.patch.object(init, "_pinned_directory", side_effect=directory):
            problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
        self.assertEqual(len(swapped), 1)
        self.assertTrue(problem.startswith("OAH_INIT_CHANGED"), problem)
        self.assertEqual(list(swapped[0].iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.target.parent.iterdir()), ["unlazy"])
        self.assertEqual(self.plan().action, "present")

    def test_unlazy_environment_is_an_allowlist_without_any_git_variables(self):
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home),
                       "LANG": "C", "LC_ALL": "C", "TMPDIR": str(self.home),
                       "GIT_DIR": "redirect", "GIT_WORK_TREE": "redirect", "GIT_INDEX_FILE": "redirect",
                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree",
                       "GIT_CONFIG_VALUE_0": "redirect", "GIT_EXEC_PATH": "redirect",
                       "GIT_CONFIG_PARAMETERS": "redirect", "GIT_CONFIG_GLOBAL": "redirect",
                       "GIT_CONFIG_SYSTEM": "redirect", "GIT_ALTERNATE_OBJECT_DIRECTORIES": "redirect",
                       "UNRELATED_SETTING": "discard"}
        with mock.patch.dict(os.environ, environment, clear=True):
            actual = init._unlazy_environment()
        self.assertEqual(actual, {key: environment[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")})

    def test_unlazy_git_redirectors_cannot_write_external_worktree_or_index(self):
        latest = git_commit(self.source, {"scripts/gate-check.mjs": b"// latest checkout\r\n"})
        outside = make_git_repo(self)
        git("fetch", str(self.source), latest, cwd=outside)
        git("checkout", "--detach", latest, cwd=outside)
        index = outside / ".git/index"
        original_index = index.read_bytes()
        original_files = {p.relative_to(outside).as_posix(): p.read_bytes()
                          for p in outside.rglob("*") if p.is_file()}
        run = init.run_command
        seen = []
        hostile = {"GIT_WORK_TREE": str(outside), "GIT_INDEX_FILE": str(index),
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree",
                   "GIT_CONFIG_VALUE_0": str(outside)}

        def command(argv, **kwargs):
            seen.append(dict(kwargs["env"]))
            result = run(argv, **kwargs)
            os.environ.update(hostile)
            os.environ["LC_ALL"] = "C"  # checkout must retain the original environment snapshot
            return result

        step = self.plan()
        with (mock.patch.object(init, "run_command", side_effect=command),
              mock.patch.object(init, "_unlazy_environment", wraps=init._unlazy_environment) as environment,
              mock.patch.dict(os.environ)):
            problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin)
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual({p.relative_to(outside).as_posix(): p.read_bytes()
                          for p in outside.rglob("*") if p.is_file()}, original_files)
        self.assertEqual(problem, "")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(environment.call_count, 1)
        self.assertEqual(unlazy.verify_pin(self.target, self.pin), [])

    def test_unlazy_inherited_redirectors_cannot_redirect_private_git_state(self):
        outside = make_git_repo(self)
        git_commit(outside, {"keep.bin": b"external bytes\r\n\x00"})
        before = {p.relative_to(outside).as_posix(): p.read_bytes()
                  for p in outside.rglob("*") if p.is_file()}
        step = self.plan()
        with mock.patch.dict(os.environ, {"GIT_DIR": str(outside / ".git"), "GIT_WORK_TREE": str(outside),
                                          "GIT_INDEX_FILE": str(outside / ".git/index")}):
            problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin)
        self.assertTrue({p.relative_to(outside).as_posix(): p.read_bytes()
                         for p in outside.rglob("*") if p.is_file()} == before, "external repository changed")
        self.assertEqual(problem, "")
        self.assertTrue((repo_info(self.repo).common_dir / "oah/staging").is_dir())

    def test_unlazy_never_records_the_callers_git_identity_or_publishes_unapproved_metadata(self):
        identity = "private-" + token_source.token_hex(16)
        email = identity + "@invalid"
        configuration = make_tmp_dir(self) / "gitconfig"
        configuration.write_bytes(f"[user]\n\tname = {identity}\n\temail = {email}\n".encode("ascii"))
        environment = {"GIT_CONFIG_GLOBAL": str(configuration), "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": identity, "GIT_AUTHOR_EMAIL": email,
                       "GIT_COMMITTER_NAME": identity, "GIT_COMMITTER_EMAIL": email}
        with mock.patch.dict(os.environ, environment):
            problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
        self.assertEqual(problem, "")
        staging, = (repo_info(self.repo).common_dir / "oah/staging").iterdir()
        for tree in (staging, self.target):
            for path in tree.rglob("*"):
                if path.is_file():
                    self.assertFalse(identity.encode("ascii") in path.read_bytes(),
                                     "Git recorded the caller's identity")
            self.assertFalse((tree / ".git/logs").exists(), "unnecessary reflogs must be disabled")
        metadata = {p.relative_to(self.target / ".git").as_posix()
                    for p in (self.target / ".git").rglob("*") if p.is_file()}
        self.assertEqual(metadata, {"HEAD", "config"})
        self.assertEqual(unlazy.verify_pin(self.target, self.pin), [])

    def test_every_unlazy_git_command_has_neutral_identity_and_environment(self):
        run = init.proc.run_command
        seen = []
        identity = "private-" + token_source.token_hex(16)

        def command(argv, **kwargs):
            self.assertIn("user.name=oah", argv)
            self.assertIn("user.email=oah@invalid", argv)
            self.assertIn("core.logAllRefUpdates=false", argv)
            self.assertFalse(any(key.upper().startswith("GIT_") for key in kwargs["env"]))
            seen.append(tuple(argv))
            return run(argv, **kwargs)

        with (mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": identity, "GIT_COMMITTER_EMAIL": identity}),
              mock.patch.object(init.proc, "run_command", side_effect=command)):
            self.assertEqual(init._verify_unlazy_pin(self.repo, self.source, self.pin), [])
        self.assertEqual(len(seen), 2)
        for argv in init.unlazy_commands(self.target, source=str(self.source), pin=self.pin):
            for setting in ("user.name=oah", "user.email=oah@invalid", "core.logAllRefUpdates=false"):
                self.assertIn(setting, argv)
            self.assertEqual(argv[argv.index("-C") + 1], str(self.target))
            if "checkout" in argv:
                self.assertIn("--git-dir=.git", argv)
                self.assertIn("--work-tree=.", argv)

    def test_clone_uses_private_git_state_and_publishes_only_file_bytes(self):
        common = repo_info(self.repo).common_dir
        run, publish = init.run_command, init._publish
        stages, publications = [], []

        def command(argv, **kwargs):
            staging = pathlib.Path(argv[6])
            self.assertTrue(staging.is_relative_to(common / "oah/staging"))
            self.assertFalse(staging.is_relative_to(self.target.parent))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(staging.parent.stat().st_mode), 0o700)
            stages.append(staging)
            return run(argv, **kwargs)

        def publication(root, relative, data, *args, **kwargs):
            if root == self.repo and relative.startswith(init.UNLAZY_PROJECT + "/"):
                if data is not None:  # directories are reserved exclusively through the same publication path
                    self.assertIsInstance(data, bytes)
                    publications.append(relative)
                self.assertIsNone(kwargs.get("source"))
            return publish(root, relative, data, *args, **kwargs)

        def directory_move(source, target, *args):
            self.fail("installation must not rename a directory into the worktree")

        with (mock.patch.object(init, "run_command", side_effect=command),
              mock.patch.object(init, "_publish", side_effect=publication),
              mock.patch.object(init, "_rename_exclusive", side_effect=directory_move),
              mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(self.repo / "unsafe-state")})):
            problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
        self.assertEqual(problem, "")
        self.assertEqual(len(stages), 2)
        self.assertEqual(stages[0], stages[1])
        self.assertTrue(publications)
        self.assertEqual(unlazy.verify_pin(self.target, self.pin), [])
        self.assertFalse((self.repo / "unsafe-state").exists())

    def test_install_preserves_empty_git_directories_from_a_detached_source(self):
        git("checkout", "--detach", self.commit, cwd=self.source)
        git("branch", "-D", "main", cwd=self.source)
        self.assertEqual(init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin), "")
        self.assertTrue((self.target / ".git/refs").is_dir())
        self.assertEqual(unlazy.verify_pin(self.target, self.pin), [])

    @unittest.skipIf(os.name == "nt", "POSIX directory replacement while its handle is open")
    def test_source_swap_at_publication_never_installs_replacement_bytes(self):
        run, publish, pinned = init.run_command, init._publish, init._pinned_directory
        staging = None
        swapped = False
        replacement = b"// unverified replacement\r\n"

        def swap():
            nonlocal swapped
            staging.rename(staging.with_name(staging.name + "-verified"))
            (staging / "scripts").mkdir(parents=True)
            (staging / "scripts/gate-check.mjs").write_bytes(replacement)
            swapped = True

        def command(argv, **kwargs):
            nonlocal staging
            staging = pathlib.Path(argv[6])
            return run(argv, **kwargs)

        @contextlib.contextmanager
        def directory(path, **kwargs):
            with pinned(path, **kwargs) as held:
                # The old directory-publication boundary: replace after its source handle opens.
                if path == staging and "delete" in kwargs and not swapped:
                    swap()
                yield held

        def publication(root, relative, data, *args, **kwargs):
            # The replacement design publishes frozen bytes, never this staging pathname.
            if (root == self.repo and relative.startswith(init.UNLAZY_PROJECT + "/")
                    and not swapped):
                swap()
            return publish(root, relative, data, *args, **kwargs)

        with (mock.patch.object(init, "run_command", side_effect=command),
              mock.patch.object(init, "_pinned_directory", side_effect=directory),
              mock.patch.object(init, "_publish", side_effect=publication)):
            problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
        self.assertTrue(swapped)
        self.assertTrue(problem.startswith("OAH_INIT_CHANGED"), problem)
        for path in self.target.rglob("*"):
            if path.is_file():
                self.assertNotEqual(path.read_bytes(), replacement)
        self.assertEqual((staging / "scripts/gate-check.mjs").read_bytes(), replacement)

    def test_cleanup_never_recursively_deletes_a_replaced_staging_path(self):
        run, remove = init.run_command, init.shutil.rmtree
        user_files = self.repo / "user-files"
        user_files.mkdir()
        sentinel = b"unrelated user data\r\n\x00"
        (user_files / "keep.bin").write_bytes(sentinel)
        stages = []

        def command(argv, **kwargs):
            stages.append(pathlib.Path(argv[6]))
            return run(argv, **kwargs)

        def cleanup(path, **kwargs):
            # Immediately after the old final identity check, replace it with a regular user directory.
            path.rename(path.with_name(path.name + "-original"))
            user_files.rename(path)
            remove(path, **kwargs)

        bad = unlazy.Pin(commit=self.commit, files={"scripts/gate-check.mjs": "0" * 64})
        with (mock.patch.object(init, "run_command", side_effect=command),
              mock.patch.object(init.shutil, "rmtree", side_effect=cleanup) as recursive):
            problem = init.install_unlazy(self.repo, self.plan(pin=bad), source=str(self.source), pin=bad)
        self.assertTrue(problem.startswith("OAH_INIT_"), problem)
        recursive.assert_not_called()
        self.assertEqual((user_files / "keep.bin").read_bytes(), sentinel)
        self.assertTrue(stages[0].is_dir())  # private state is retained for explicit manual cleanup

    def test_partial_install_is_not_discovered_as_verified(self):
        commit = git_commit(self.source, {"zz-extra.txt": b"additional pinned-checkout content\r\n"})
        pin = unlazy.Pin(commit=commit, files=self.pin.files)
        publish = init._publish

        def publication(root, relative, data, *args, **kwargs):
            if relative == init.UNLAZY_PROJECT + "/zz-extra.txt":
                return False
            return publish(root, relative, data, *args, **kwargs)

        with mock.patch.object(init, "_publish", side_effect=publication):
            problem = init.install_unlazy(self.repo, self.plan(pin=pin), source=str(self.source), pin=pin)
        self.assertTrue(problem.startswith("OAH_INIT_CHANGED"), problem)
        self.assertTrue(unlazy.verify_pin(self.target, pin))
        self.assertEqual(self.plan(pin=pin).action, "blocked")

    def test_capture_rejects_a_pinned_empty_script_removed_after_verification(self):
        commit = git_commit(self.source, {"scripts/gate-check.mjs": b""})
        pin = unlazy.Pin(commit=commit, files={**self.pin.files,
                                            "scripts/gate-check.mjs": hashlib.sha256(b"").hexdigest()})
        verify = init._verify_unlazy_pin

        def verification(root, folder, expected, **kwargs):
            problems = verify(root, folder, expected, **kwargs)
            if not problems:
                (folder / "scripts/gate-check.mjs").unlink()
            return problems

        step = self.plan(pin=pin)
        with mock.patch.object(init, "_verify_unlazy_pin", side_effect=verification):
            problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=pin)
        self.assertTrue(problem.startswith("OAH_INIT_CHANGED"), problem)
        self.assertFalse(self.target.exists())

    @unittest.skipIf(os.name == "nt", "POSIX staging pathname replacement")
    def test_staging_swap_cannot_redirect_checkout(self):
        self._staging_swap_cannot_redirect_checkout()

    @unittest.skipUnless(os.name == "nt", "Windows staging junction replacement")
    def test_windows_staging_swap_cannot_redirect_checkout(self):
        self._staging_swap_cannot_redirect_checkout()

    def _staging_swap_cannot_redirect_checkout(self):
        for boundary in ("before-clone", "after-clone", "before-checkout"):
            with self.subTest(boundary=boundary):
                self.repo = make_git_repo(self)
                outside = make_git_repo(self)
                git("fetch", str(self.source), self.commit, cwd=outside)
                git("checkout", "--detach", self.commit, cwd=outside)
                head = git_commit(outside, {"scripts/gate-check.mjs": b"// external repository\r\n"})
                external_bytes = (outside / "scripts/gate-check.mjs").read_bytes()
                run = init.run_command
                calls = 0

                def swap(staging):
                    staging.rename(staging.with_name(staging.name + "-saved"))
                    directory_alias(outside, staging)

                def command(argv, **kwargs):
                    nonlocal calls
                    calls += 1
                    if (calls == 1 and boundary == "before-clone"
                            or calls == 2 and boundary == "before-checkout"):
                        swap(pathlib.Path(argv[6]))
                    result = run(argv, **kwargs)
                    if calls == 1 and boundary == "after-clone":
                        swap(pathlib.Path(argv[6]))
                    return result

                with mock.patch.object(init, "run_command", side_effect=command):
                    try:
                        problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
                    except init.InitError as exc:
                        problem = str(exc)
                self.assertTrue(problem.startswith("OAH_INIT_"), problem)
                self.assertEqual(git("rev-parse", "HEAD", cwd=outside), head)
                self.assertEqual((outside / "scripts/gate-check.mjs").read_bytes(), external_bytes)
                self.assertFalse(self.target.exists())

    @unittest.skipUnless(os.name == "nt", "Windows staging identity during publication")
    def test_windows_staging_publication_refuses_source_replacement(self):
        run, publish = init.run_command, init._publish
        attempts = []
        staging = None

        def command(argv, **kwargs):
            nonlocal staging
            staging = pathlib.Path(argv[6])
            return run(argv, **kwargs)

        def publication(root, relative, data, *args, **kwargs):
            if not attempts:
                staging.rename(staging.with_name(staging.name + "-saved"))
                staging.mkdir()
                attempts.append(True)
            return publish(root, relative, data, *args, **kwargs)

        with (mock.patch.object(init, "run_command", side_effect=command),
              mock.patch.object(init, "_publish", side_effect=publication)):
            problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
        self.assertTrue(problem.startswith("OAH_INIT_CHANGED"), problem)
        self.assertEqual(len(attempts), 1)
        self.assertFalse((self.target / ".git/HEAD").exists())
        self.assertEqual(list(staging.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows staging pin during an external command")
    def test_windows_staging_command_denies_directory_replacement(self):
        staging = self.repo / "staging"
        staging.mkdir()
        script = ("import pathlib, sys\n"
                  "try:\n"
                  "    pathlib.Path(sys.argv[1]).rename(sys.argv[1] + '-moved')\n"
                  "except PermissionError:\n"
                  "    sys.exit(0)\n"
                  "sys.exit(1)\n")
        with init._pinned_directory(staging) as (_handle, identity):
            code = init._staging_command(str(staging), identity, [sys.executable, "-c", script, str(staging)])
        self.assertEqual(code, 0)
        self.assertTrue(staging.is_dir())

    def test_failed_git_diagnostics_are_discarded(self):
        secret = "fake-" + token_source.token_hex(20)
        for timed_out, code in ((False, 128), (True, None)):
            for operation in ("clone", "checkout"):
                with self.subTest(timed_out=timed_out, operation=operation):
                    result = RunResult(code, secret, "proxy Authorization: " + secret, timed_out)
                    results = [result] if operation == "clone" else [RunResult(0, "", "", False), result]
                    with mock.patch.object(init, "run_command", side_effect=results):
                        problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
                    self.assertNotIn(secret, problem)
                    self.assertIn("git " + operation, problem)
                    self.assertIn(f"exit code {code}", problem)
                    self.assertIn(f"timed out: {'yes' if timed_out else 'no'}", problem)
                    self.assertFalse(self.target.exists())

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
                self.assertTrue(step.note.startswith("OAH_INIT_PATH_UNSAFE:"), step.note)
                problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin)
                self.assertTrue(problem)
                self.assertTrue((outside / "unlazy/scripts/gate-check.mjs").is_file())
                self.assertFalse((outside / "unlazy.oah.bak").exists())

    def test_discovery_never_reclassifies_redirected_project_candidates(self):
        for relative in (init.UNLAZY_PROJECT, ".claude/skills/unlazy"):
            for component in ("copy", "ancestor"):
                with self.subTest(relative=relative, component=component):
                    self.repo = make_git_repo(self)
                    candidate = self.repo / relative
                    candidate.parent.mkdir(parents=True)
                    linked = init._linked
                    redirected = False

                    def redirect(root, name):
                        nonlocal redirected
                        result = linked(root, name)
                        if not redirected and name == ".claude/skills/unlazy":
                            redirected = True
                            if component == "copy":
                                directory_alias(self.source, candidate)
                            else:
                                candidate.parent.rename(candidate.parent.with_name("saved"))
                                outside = make_tmp_dir(self)
                                directory_alias(self.source, outside / "unlazy")
                                directory_alias(outside, candidate.parent)
                        return result

                    with mock.patch.object(init, "_linked", side_effect=redirect):
                        with self.assertRaises(init.InitError) as caught:
                            self.plan()
                    self.assertEqual(caught.exception.code, "OAH_INIT_PATH_UNSAFE")

    def test_project_redirection_after_pinned_script_reads_still_refuses(self):
        git("clone", "--quiet", str(self.source), str(self.target))
        read = init._read_user
        redirected = False
        last = f"{init.UNLAZY_PROJECT}/{list(self.pin.files)[-1]}"

        def redirect(root, relative):
            nonlocal redirected
            data = read(root, relative)
            if not redirected and relative == last:
                redirected = True
                self.target.rename(self.target.with_name("saved"))
                directory_alias(self.source, self.target)
            return data

        with mock.patch.object(init, "_read_user", side_effect=redirect):
            with self.assertRaises(init.InitError) as caught:
                self.plan()
        self.assertEqual(caught.exception.code, "OAH_INIT_PATH_UNSAFE")

    def test_noncanonical_root_keeps_project_discovery_pinned(self):
        (self.repo / "child").mkdir()
        root = self.repo / "child" / ".."
        self.target.parent.mkdir(parents=True)
        linked = init._linked
        redirected = False

        def redirect(boundary, relative):
            nonlocal redirected
            result = linked(boundary, relative)
            if not redirected and relative == ".claude/skills/unlazy":
                redirected = True
                directory_alias(self.source, self.target)
            return result

        with mock.patch.object(init, "_linked", side_effect=redirect):
            with self.assertRaises(init.InitError) as caught:
                init.plan_unlazy(root, env={}, home=self.home, pin=self.pin, force=False)
        self.assertEqual(caught.exception.code, "OAH_INIT_PATH_UNSAFE")

    @unittest.skipIf(os.name == "nt", "POSIX ancestry permissions; native Windows cases below")
    def test_staging_refuses_writable_state_ancestor_before_commands(self):
        common = repo_info(self.repo).common_dir
        common.chmod(0o777)
        self.addCleanup(common.chmod, 0o755)
        with mock.patch.object(init, "run_command") as command:
            problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
        self.assertTrue(problem.startswith("OAH_INIT_"), problem)
        command.assert_not_called()
        self.assertFalse(self.target.exists())

    @unittest.skipUnless(sys.platform == "darwin", "native macOS staging ACL refusal")
    def test_macos_staging_refuses_existing_acl_before_commands(self):
        staging = repo_info(self.repo).common_dir / "oah/staging"
        unlazy._private_makedirs(staging)
        subprocess.run(["/bin/chmod", "+a", "everyone allow list,search,readattr,readsecurity",
                        str(staging)], check=True, capture_output=True)
        acl = subprocess.run(["/bin/ls", "-lde", str(staging)], check=True, capture_output=True).stdout
        with mock.patch.object(init, "run_command") as command:
            problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
        self.assertTrue(problem.startswith("OAH_INIT_"), problem)
        command.assert_not_called()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(staging.iterdir()), [])
        self.assertEqual(subprocess.run(["/bin/ls", "-lde", str(staging)], check=True,
                                        capture_output=True).stdout, acl)

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

    def test_incompatible_project_copy_requires_manual_removal_even_with_force(self):
        make_unlazy(self.target)
        original = (self.target / "scripts/gate-check.mjs").read_bytes()
        for force in (False, True):
            with self.subTest(force=force):
                step = self.plan(force=force)
                self.assertEqual(step.action, "blocked")
                self.assertTrue(step.note.startswith("OAH_INIT_UNLAZY_EXISTS:"), step.note)
                self.assertIn(init.UNLAZY_PROJECT, step.note)
                self.assertIn("Remove", step.note)
                self.assertIn("rerun", step.note)
                self.assertFalse(step.backup)
                problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin)
                self.assertTrue(problem.startswith("OAH_INIT_"), problem)
        self.assertEqual((self.target / "scripts/gate-check.mjs").read_bytes(), original)
        self.assertFalse((repo_info(self.repo).common_dir / "oah").exists())

    def test_non_unlazy_directory_requires_manual_removal_even_with_force(self):
        self.target.mkdir(parents=True)
        original = b"user directory\r\n\x00"
        (self.target / "keep.bin").write_bytes(original)
        step = self.plan(force=True)
        self.assertEqual(step.action, "blocked")
        self.assertTrue(step.note.startswith("OAH_INIT_UNLAZY_EXISTS:"), step.note)
        self.assertIn(init.UNLAZY_PROJECT, step.note)
        self.assertFalse(step.backup)
        self.assertEqual((self.target / "keep.bin").read_bytes(), original)

    def test_claude_project_copy_requires_manual_removal(self):
        target = self.repo / ".claude/skills/unlazy"
        make_unlazy(target)
        step = self.plan(force=True)
        self.assertEqual(step.action, "blocked")
        self.assertTrue(step.note.startswith("OAH_INIT_UNLAZY_EXISTS:"), step.note)
        self.assertIn(".claude/skills/unlazy", step.note)
        self.assertFalse(step.backup)

    def test_directory_source_publication_refuses_hash_then_regular_directory_swap(self):
        make_unlazy(self.target)
        digest = init._tree_digest(self.target, root=self.repo)
        tree_digest = init._tree_digest
        saved = self.target.with_name("saved")
        original = (self.target / "scripts/gate-check.mjs").read_bytes()
        replacement = b"concurrent user tree\r\n\x00"
        swapped = False

        def swap(path, **kwargs):
            nonlocal swapped
            result = tree_digest(path, **kwargs)
            if path == self.target:
                path.rename(saved)
                path.mkdir(mode=0o755)
                (path / "keep.bin").write_bytes(replacement)
                swapped = True
            return result

        common = repo_info(self.repo).common_dir
        with mock.patch.object(init, "_tree_digest", side_effect=swap):
            published = init._publish(common, "oah/backups/run/unlazy", None, source=init.UNLAZY_PROJECT,
                                      source_root=self.repo, source_digest=digest, private=True)
        self.assertFalse(published)
        self.assertFalse((common / "oah/backups/run/unlazy").exists())
        if swapped:
            self.assertEqual((self.target / "keep.bin").read_bytes(), replacement)
            self.assertEqual((saved / "scripts/gate-check.mjs").read_bytes(), original)
        else:  # Refusing before hashing/moving is the safer path.
            self.assertEqual((self.target / "scripts/gate-check.mjs").read_bytes(), original)

    def test_project_pin_verification_rejects_linked_script_sources(self):
        outside = make_tmp_dir(self)
        (outside / "gate-check.mjs").write_bytes(("fake-" + token_source.token_hex(20)).encode("ascii"))
        self.target.mkdir(parents=True)
        directory_alias(outside, self.target / "scripts")
        with self.assertRaises(init.InitError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "OAH_INIT_PATH_UNSAFE")

    def test_legacy_replacement_plan_is_refused_without_staging(self):
        make_unlazy(self.target)
        step = init.UnlazyStep("replace", str(self.target), aside=str(self.target),
                               digest=init._tree_digest(self.target, root=self.repo), backup="run/unlazy")
        problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin)
        self.assertTrue(problem.startswith("OAH_INIT_UNLAZY_EXISTS:"), problem)
        self.assertTrue((self.target / "scripts" / "gate-check.mjs").is_file())
        self.assertEqual(sorted(path.name for path in self.target.parent.iterdir()), ["unlazy"])
        self.assertFalse((repo_info(self.repo).common_dir / "oah").exists())

    def test_replacement_refuses_a_copy_edited_since_planning(self):
        make_unlazy(self.target)
        step = self.plan(force=True)
        script = self.target / "scripts/gate-check.mjs"
        script.write_bytes(b"// concurrent\r\n")
        problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin)
        self.assertTrue(problem.startswith("OAH_INIT_UNLAZY_EXISTS:"), problem)
        self.assertEqual(script.read_bytes(), b"// concurrent\r\n")
        self.assertFalse(self.target.with_name("unlazy.oah.bak").exists())

    def test_replacement_tree_reads_refuse_a_swapped_ancestor(self):
        make_unlazy(self.target)
        outside = make_tmp_dir(self)
        external = outside / "skills/unlazy/scripts/gate-check.mjs"
        external.parent.mkdir(parents=True)
        external.write_bytes(("fake-" + token_source.token_hex(20)).encode("ascii"))
        script = self.target / "scripts/gate-check.mjs"
        real_stat = pathlib.Path.stat
        swapped = False

        def swap(path, *args, **kwargs):
            nonlocal swapped
            result = real_stat(path, *args, **kwargs)
            if path == script and not swapped:
                swapped = True
                (self.repo / ".agents").rename(self.repo / "saved-agents")
                directory_alias(outside, self.repo / ".agents")
            return result

        # Hash real files, but redirect an ancestor between metadata and the content read.
        with mock.patch.object(pathlib.Path, "stat", swap), self.assertRaises(init.InitError):
            init._tree_digest(self.target, root=self.repo)
        self.assertTrue(swapped)

    def test_install_refuses_parent_linked_during_clone(self):
        step = self.plan()
        outside = make_tmp_dir(self)
        run = init.run_command
        linked = False

        def clone(argv, **kwargs):
            nonlocal linked
            result = run(argv, **kwargs)
            if not linked:
                linked = True
                directory_alias(outside, self.repo / ".agents")
            return result

        with mock.patch.object(init, "run_command", side_effect=clone):
            problem = init.install_unlazy(self.repo, step, source=str(self.source), pin=self.pin)
        self.assertTrue(problem.startswith("OAH_INIT_CHANGED"), problem)
        self.assertEqual(list(outside.iterdir()), [])

    def test_incompatible_user_copy_gets_a_project_copy(self):
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        step = self.plan()
        self.assertEqual(step.action, "install")
        self.assertIn("not the pinned", step.note)

    def test_successful_install_retains_staging_for_manual_cleanup(self):
        self.assertEqual(init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin), "")
        staging, = (repo_info(self.repo).common_dir / "oah/staging").iterdir()
        self.assertEqual(unlazy.verify_pin(staging, self.pin), [])
        # Retaining even read-only Git objects requires no pathname-based permission repair or deletion.
        self.assertTrue(any(path.is_file() for path in (staging / ".git/objects").rglob("*")))

    @unittest.skipUnless(os.name == "nt", "native Windows staging DACLs")
    def test_windows_wiki_then_unlazy_keeps_state_private(self):
        common = repo_info(self.repo).common_dir
        subprocess.run(["icacls.exe", str(common), "/grant", "*S-1-1-0:(OI)(CI)R"],
                       check=True, capture_output=True)
        wiki = Wiki(self.repo, WikiSettings(), common / "oah")
        change = init.FileChange("docs/wiki/index.md", "create", "wiki", b"# Wiki\r\n")
        written, problems = init.write_files(self.repo, [change], wiki=wiki)
        self.assertEqual(written, [change])
        self.assertEqual(problems, [])
        self.assertEqual(init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin), "")
        init._private_state_directory(common / "oah")

    @unittest.skipUnless(os.name == "nt", "native Windows staging DACLs")
    def test_windows_staging_is_private_under_permissive_git_metadata(self):
        common = repo_info(self.repo).common_dir
        subprocess.run(["icacls.exe", str(common), "/grant", "*S-1-1-0:(OI)(CI)R"],
                       check=True, capture_output=True)
        self.assertEqual(init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin), "")
        staging, = (common / "oah/staging").iterdir()
        for directory in (common / "oah", staging.parent, staging):
            descriptor = unlazy._windows_security_descriptor(directory)
            self.assertTrue("D:P" in descriptor, "state DACL must be protected")
            self.assertFalse(";;;WD)" in descriptor or ";;;S-1-1-0)" in descriptor,
                             "state DACL must exclude public access")
            init._private_state_directory(directory)

    @unittest.skipUnless(os.name == "nt", "native Windows existing staging DACL refusal")
    def test_windows_staging_refuses_existing_permissive_directory(self):
        staging = repo_info(self.repo).common_dir / "oah/staging"
        unlazy._private_makedirs(staging)
        subprocess.run(["icacls.exe", str(staging), "/grant", "*S-1-1-0:(OI)(CI)F"],
                       check=True, capture_output=True)
        descriptor = unlazy._windows_security_descriptor(staging)
        # External commands are fakes and must never run when staging permissions are unsafe.
        with mock.patch.object(init, "run_command") as command:
            problem = init.install_unlazy(self.repo, self.plan(), source=str(self.source), pin=self.pin)
        self.assertTrue(problem.startswith("OAH_INIT_"), problem)
        command.assert_not_called()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(staging.iterdir()), [])
        self.assertTrue(unlazy._windows_security_descriptor(staging) == descriptor,
                        "init must leave the existing DACL unchanged")

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
        self.assertNotIn("name: mine", replaced.diff)
        self.assertIn("replaces", replaced.note)
        self.assertEqual(init.write_ci(replaced), "")
        self.assertEqual(self.target.read_text(encoding="utf-8"), step.text)

    def test_ci_rechecks_target_during_publication(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"name: before\r\n")
        step = init.plan_ci(self.repo, force=True, oses=["linux"], test_command=None)
        flush = os.fsync
        changed = False

        def edit(fd):
            nonlocal changed
            flush(fd)
            if not changed and stat.S_ISREG(os.fstat(fd).st_mode):
                changed = True
                self.target.write_bytes(b"name: concurrent\r\n")

        with mock.patch.object(os, "fsync", side_effect=edit):
            problem = init.write_ci(step)
        self.assertTrue(problem.startswith("OAH_INIT_CHANGED"), problem)
        self.assertEqual(self.target.read_bytes(), b"name: concurrent\r\n")

    def test_several_test_commands_are_skipped(self):
        (self.repo / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
        step = init.plan_ci(self.repo, force=False, oses=[], test_command=None)
        self.assertEqual(step.action, "skipped")
        self.assertIn("oah ci init", step.note)


class MapStepTest(unittest.TestCase):
    def test_map_git_diagnostics_never_reach_the_returned_reason(self):
        repo = make_git_repo(self)
        git_commit(repo, {"a.py": "pass\n"})
        secret = "fake-" + token_source.token_hex(20)
        with mock.patch.object(mapstore, "run_command", return_value=RunResult(128, "", secret, False)):
            for operation in (init.map_state, init.refresh_map):
                state, reason = operation(repo, {})
                self.assertEqual(state, "unavailable")
                self.assertNotIn(secret, reason)

    def test_external_runner_discards_stdout_stderr_and_exception_details(self):
        secret = "fake-" + token_source.token_hex(20)
        for timed_out, code in ((False, 0), (False, 2), (True, None)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(init, "resolve_command", return_value=
                    Resolved((sys.executable,), "test", "native")), mock.patch.object(init, "run_command",
                    return_value=RunResult(code, secret, secret, timed_out)):
                result = init._quiet_run(("fake-installer",), operation="tool installation")
            self.assertNotIn(secret, out.getvalue())
            self.assertEqual(result, 0 if code == 0 and not timed_out else 1)
            if result:
                self.assertIn(f"exit code {code}; timed out: {'yes' if timed_out else 'no'}", out.getvalue())

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
