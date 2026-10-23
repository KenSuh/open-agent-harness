# 위키 기본 검사 — 마크다운 도우미, 머리말, 노트 생성·색인, 스캐폴드 템플릿, wiki 명령과 maintain 을 본다.
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
import pathlib
import subprocess
import unittest

from oah import wiki, wiki_cli
from oah.wiki import markdown, notes
from oah.wiki.base import WikiError, WikiSettings
from oah.wiki.store import Wiki
from tests.helpers import git_commit, make_git_repo, make_tmp_dir

TODAY = datetime.date(2026, 10, 23)


def scaffold(test: unittest.TestCase, repo: pathlib.Path | None = None) -> pathlib.Path:
    """A repository with the packaged wiki scaffold written to docs/wiki; returns the repo root."""
    repo = repo or make_git_repo(test)
    for rel, data in notes.scaffold_files(TODAY).items():
        path = repo / "docs" / "wiki" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return repo


def make_wiki(test: unittest.TestCase, repo: pathlib.Path, **settings) -> Wiki:
    state = make_tmp_dir(test) / "state"
    return Wiki(repo, WikiSettings(**settings), state)


class MarkdownTest(unittest.TestCase):
    def test_split_lines_keeps_crlf_and_lone_cr(self):
        self.assertEqual(markdown.split_lines(b"a\r\nb\rc\nd"), [b"a\r\n", b"b\rc\n", b"d"])
        self.assertEqual(markdown.split_lines(b""), [])
        self.assertEqual(markdown.split_lines(b"x\n"), [b"x\n"])

    def test_fence_tracker_rules(self):
        cases = [
            (["```", "x", "```"], [True, True, True]),
            (["   ~~~py", "x", "~~~~", "y"], [True, True, True, False]),
            (["````", "```", "````"], [True, True, True]),
            (["    ```", "x"], [False, False]),          # four spaces: indented code, not a fence
            (["```a`b", "x"], [False, False]),          # backtick info string with a backtick
            (["~~~", "``` ", "~~~ x", "~~~"], [True, True, True, True]),
        ]
        for lines, expected in cases:
            with self.subTest(lines=lines):
                tracker = markdown.FenceTracker()
                self.assertEqual([tracker.feed(line) for line in lines], expected)

    def test_mask_code_spans_blocks_and_indented_code(self):
        text = ("a [[keep]] `[[span]]` b\n"
                "```\n[[fenced]]\n```\n"
                "\n    [[indented]]\n"
                "- item\n\n    [[list continuation]]\n"
                "``x\n[[multi]] y``\n")
        targets = [link.target for link in markdown.wikilinks(text)]
        self.assertEqual(targets, ["keep", "list continuation"])

    def test_wikilink_forms(self):
        text = "[[a]] ![[b.png]] [[c|alias]] [[d#head]] [[e#^block|x]] [[#local]] [[f\\|g]]\n\n[[h]]"
        links = markdown.wikilinks(text)
        self.assertEqual([link.target for link in links], ["a", "b.png", "c", "d", "e", "f", "h"])
        self.assertEqual([link.line for link in links], [1, 1, 1, 1, 1, 1, 3])
        self.assertTrue(links[1].embed)


class FrontmatterTest(unittest.TestCase):
    def test_frontmatter_subset(self):
        text = ("﻿---\r\ntitle: \"A: b\"\r\ndate: 2026-10-23\r\nsources:\r\n  - src/a.py@abc\r\n"
                "  - 'https://example.com/x'\r\ntags: [one, \"two\"]\r\n# comment\r\nempty:\r\n---\r\nbody")
        self.assertEqual(notes.parse_frontmatter(text), {
            "title": "A: b", "date": "2026-10-23", "sources": ["src/a.py@abc", "https://example.com/x"],
            "tags": ["one", "two"], "empty": ""})
        self.assertIsNone(notes.parse_frontmatter("no frontmatter"))
        self.assertIsNone(notes.parse_frontmatter("---\ntitle: x\n"))          # never closed
        self.assertIsNone(notes.parse_frontmatter("---\nnot a key\n---\n"))   # malformed line

    def test_slugify_table(self):
        cases = {
            "Hello World": "hello-world",
            "  Café: notes / v2?  ": "café-notes-v2",
            "로그 정리 규칙": "로그-정리-규칙",
            "a|b#c^d[e]": "a-b-c-d-e",
            "CON": "con-note",
            "lpt1.txt": "lpt1.txt-note",
            "trailing dots...": "trailing-dots",
        }
        for title, slug in cases.items():
            with self.subTest(title=title):
                self.assertEqual(notes.slugify(title), slug)
        for bad in ["", "   ", "???", "a\nb", "x\x00"]:
            with self.subTest(bad=bad), self.assertRaises(WikiError) as ctx:
                notes.slugify(notes.validate_title(bad))
            self.assertEqual(ctx.exception.code, "OAH_WIKI_TITLE_INVALID")


class NewNoteTest(unittest.TestCase):
    def test_new_note_creates_and_indexes(self):
        repo = scaffold(self)
        w = make_wiki(self, repo)
        path = w.new("Retry policy", sources=["src/a.py@abc1234"], tags=["ops"], today=TODAY)
        self.assertEqual(path, repo / "docs" / "wiki" / "retry-policy.md")
        meta = notes.parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(meta["title"], "Retry policy")
        self.assertEqual(meta["date"], "2026-10-23")
        self.assertEqual(meta["sources"], ["src/a.py@abc1234"])
        self.assertEqual(meta["tags"], ["ops"])
        index = (repo / "docs" / "wiki" / "index.md").read_text(encoding="utf-8")
        lines = index.splitlines()
        self.assertIn("- [[retry-policy|Retry policy]]", lines)
        self.assertLess(lines.index("Notes:"), lines.index("- [[retry-policy|Retry policy]]"))
        self.assertLess(lines.index("- [[retry-policy|Retry policy]]"), lines.index("Decisions:"))

    def test_new_note_title_needs_quoting(self):
        repo = scaffold(self)
        path = make_wiki(self, repo).new('Say "hi": now', today=TODAY)
        self.assertEqual(notes.parse_frontmatter(path.read_text(encoding="utf-8"))["title"], 'Say "hi": now')

    def test_new_note_refuses_existing_name(self):
        repo = scaffold(self)
        w = make_wiki(self, repo)
        w.new("Retry policy", today=TODAY)
        index_before = (repo / "docs" / "wiki" / "index.md").read_bytes()
        for title in ["Retry Policy", "retry policy", "Index", "Log"]:
            with self.subTest(title=title), self.assertRaises(WikiError) as ctx:
                w.new(title, today=TODAY)
            self.assertEqual(ctx.exception.code, "OAH_WIKI_NOTE_EXISTS")
        self.assertEqual((repo / "docs" / "wiki" / "index.md").read_bytes(), index_before)

    def test_new_note_decision_kind(self):
        repo = scaffold(self)
        path = make_wiki(self, repo).new("Use SQLite", kind="decision", today=TODAY)
        self.assertEqual(path, repo / "docs" / "wiki" / "decisions" / "use-sqlite.md")
        meta = notes.parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertIn("decision", meta["tags"])
        index = (repo / "docs" / "wiki" / "index.md").read_text(encoding="utf-8").splitlines()
        self.assertGreater(index.index("- [[use-sqlite|Use SQLite]]"), index.index("Decisions:"))

    def test_new_note_keeps_index_crlf(self):
        repo = scaffold(self)
        index = repo / "docs" / "wiki" / "index.md"
        index.write_bytes(b"Catalog.\r\n\r\nNotes:\r\n- [[wiki-guide]]\r\n\r\nTail\r\n")
        make_wiki(self, repo).new("Crlf note", today=TODAY)
        self.assertEqual(index.read_bytes(),
                         b"Catalog.\r\n\r\nNotes:\r\n- [[wiki-guide]]\r\n- [[crlf-note|Crlf note]]\r\n\r\nTail\r\n")

    def test_new_note_adds_missing_label(self):
        repo = scaffold(self)
        index = repo / "docs" / "wiki" / "index.md"
        index.write_bytes(b"Catalog.")
        make_wiki(self, repo).new("First", today=TODAY)
        self.assertEqual(index.read_bytes(), b"Catalog.\n\nNotes:\n- [[first|First]]\n")

    def test_new_note_prefers_wiki_template(self):
        repo = scaffold(self)
        (repo / "docs" / "wiki" / "templates" / "note.md").write_text(
            "---\ntitle: {{title}}\ndate: {{date}}\nsources: []\ntags: []\n---\nCUSTOM {{title}}\n",
            encoding="utf-8")
        path = make_wiki(self, repo).new("Mine", today=TODAY)
        self.assertTrue(path.read_text(encoding="utf-8").endswith("CUSTOM Mine\n"))

    def test_new_without_wiki_is_environment_error(self):
        repo = make_git_repo(self)
        with self.assertRaises(WikiError) as ctx:
            make_wiki(self, repo).new("x", today=TODAY)
        self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("OAH_WIKI_MISSING", 69))


class ScaffoldTest(unittest.TestCase):
    def test_scaffold_has_no_workspace_files(self):
        files = notes.scaffold_files(TODAY)
        for required in ["index.md", "log.md", "wiki-guide.md", "templates/note.md", "templates/decision.md",
                         ".obsidian/app.json", ".obsidian/core-plugins.json", ".obsidian/templates.json"]:
            self.assertIn(required, files)
        self.assertFalse([name for name in files if "workspace" in name])
        self.assertEqual(json.loads(files[".obsidian/templates.json"])["folder"], "templates")
        for name, data in files.items():
            if name.endswith(".json"):
                json.loads(data.decode("utf-8"))
        packaged = notes.TEMPLATES / "obsidian"
        self.assertFalse([p for p in packaged.iterdir() if "workspace" in p.name])

    def test_scaffold_log_has_marker_and_parseable_entry(self):
        from oah.wiki import log
        parsed = log.parse_log(notes.scaffold_files(TODAY)["log.md"], "log.md")
        self.assertEqual([(e.date, e.title) for e in parsed.entries], [("2026-10-23", "Wiki created")])

    def test_gitignore_snippet_ignores_workspace_state(self):
        repo = make_git_repo(self)
        (repo / ".gitignore").write_text(notes.gitignore_snippet(), encoding="utf-8")
        for rel, expected in [("docs/wiki/.obsidian/workspace.json", True),
                              ("docs/wiki/.obsidian/workspace-mobile.json", True),
                              ("docs/wiki/.obsidian/app.json", False)]:
            result = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", rel])
            self.assertEqual(result.returncode == 0, expected, rel)


def run_cli(repo: pathlib.Path, *argv: str, state: pathlib.Path | None = None) -> tuple[int, str, str]:
    parser = argparse.ArgumentParser(prog="oah")
    wiki_cli.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    old_cwd, old_env = os.getcwd(), os.environ.get("OAH_STATE_DIR")
    os.chdir(repo)
    if state is not None:
        os.environ["OAH_STATE_DIR"] = str(state)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wiki_cli.run(args)
    finally:
        os.chdir(old_cwd)
        if old_env is None:
            os.environ.pop("OAH_STATE_DIR", None)
        else:
            os.environ["OAH_STATE_DIR"] = old_env
    return code, out.getvalue(), err.getvalue()


class WikiCliTest(unittest.TestCase):
    def setUp(self):
        self.state = make_tmp_dir(self) / "state"

    def test_commands_tuple(self):
        self.assertEqual(wiki_cli.COMMANDS, ("wiki",))

    def test_cli_new_log_lint_roundtrip(self):
        repo = scaffold(self)
        code, out, err = run_cli(repo, "wiki", "new", "Cache rules", "--source", "src/c.py@abc1234",
                                 state=self.state)
        self.assertEqual(code, 0, err)
        self.assertIn("cache-rules.md", out)
        code, out, err = run_cli(repo, "wiki", "log", "Cache rules added", "--body", "See [[cache-rules]].",
                                 state=self.state)
        self.assertEqual(code, 0, err)
        log_text = (repo / "docs" / "wiki" / "log.md").read_text(encoding="utf-8")
        self.assertIn("Cache rules added\n\nSee [[cache-rules]].\n", log_text)
        code, out, err = run_cli(repo, "wiki", "lint", state=self.state)
        self.assertEqual(code, 0, out + err)
        self.assertIn("OAH_WIKI_LINT_CLEAN", out)
        code, out, err = run_cli(repo, "wiki", "lint", "--json", state=self.state)
        self.assertEqual(json.loads(out)["problems"], [])

    def test_cli_exit_codes(self):
        repo = scaffold(self)
        (repo / "docs" / "wiki" / "bad.md").write_text("no frontmatter [[nowhere]]\n", encoding="utf-8")
        code, out, _ = run_cli(repo, "wiki", "lint", state=self.state)
        self.assertEqual(code, 1)
        self.assertIn("broken_link", out)
        code, _, err = run_cli(repo, "wiki", "new", "???", state=self.state)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_WIKI_TITLE_INVALID"))
        bare = make_git_repo(self)
        code, _, err = run_cli(bare, "wiki", "lint", state=self.state)
        self.assertEqual(code, 69)
        self.assertIn("oah init", err)
        code, out, _ = run_cli(repo, "wiki", "recover", state=self.state)
        self.assertEqual(code, 0)
        self.assertIn("nothing to recover", out)
        code, out, _ = run_cli(repo, "wiki", "rollover", state=self.state)
        self.assertEqual(code, 0)
        self.assertIn("OAH_WIKI_ROLLOVER_NOOP", out)
        code, out, _ = run_cli(repo, "wiki", "hygiene", state=self.state)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OAH_WIKI_HYGIENE "))


class MaintainTest(unittest.TestCase):
    def test_maintain_runs_only_named_steps(self):
        repo = scaffold(self)
        env = {"OAH_STATE_DIR": str(make_tmp_dir(self) / "state")}
        before = (repo / "docs" / "wiki" / "log.md").read_bytes()
        result = wiki.maintain(repo, on={"lint"}, env=env, today=TODAY)
        self.assertEqual((repo / "docs" / "wiki" / "log.md").read_bytes(), before)
        self.assertEqual(len(result.lines), 1)
        self.assertTrue(result.lines[0].startswith("OAH_WIKI_LINT"))
        self.assertEqual(result.errors, [])
        result = wiki.maintain(repo, on={"log", "hygiene"}, log_title="Task t finished", env=env, today=TODAY)
        self.assertIn(b"## 2026-10-23 Task t finished", (repo / "docs" / "wiki" / "log.md").read_bytes())
        self.assertEqual([line.split()[0] for line in result.lines], ["OAH_WIKI_LOGGED", "OAH_WIKI_HYGIENE"])
        self.assertEqual(wiki.maintain(repo, on=set(), env=env).lines, [])
        with self.assertRaises(ValueError):
            wiki.maintain(repo, on={"delete"}, env=env)
        with self.assertRaises(ValueError):
            wiki.maintain(repo, on={"log"}, env=env)   # a log step needs a title

    def test_maintain_rollover_only_over_threshold(self):
        repo = scaffold(self)
        env = {"OAH_STATE_DIR": str(make_tmp_dir(self) / "state")}
        settings = WikiSettings(rollover_trigger_bytes=2000, rollover_target_bytes=1000)
        result = wiki.maintain(repo, on={"rollover"}, env=env, settings=settings, today=TODAY)
        self.assertTrue(result.lines[0].startswith("OAH_WIKI_ROLLOVER_NOOP"))
        for i in range(20):
            wiki.maintain(repo, on={"log"}, log_title=f"entry {i}", log_body="x" * 80, env=env,
                          settings=settings, today=TODAY)
        result = wiki.maintain(repo, on={"rollover"}, env=env, settings=settings, today=TODAY)
        self.assertTrue(result.lines[0].startswith("OAH_WIKI_ROLLED_OVER"), result.lines)
        self.assertTrue((repo / "docs" / "wiki" / "log_archive_01.md").is_file())

    def test_maintain_reports_errors_per_step(self):
        repo = make_git_repo(self)
        env = {"OAH_STATE_DIR": str(make_tmp_dir(self) / "state")}
        result = wiki.maintain(repo, on={"lint", "hygiene"}, env=env)
        self.assertEqual(result.lines, [])
        self.assertTrue(all(error.startswith("OAH_WIKI_MISSING") for error in result.errors))
        self.assertEqual(len(result.errors), 2)


if __name__ == "__main__":
    unittest.main()
