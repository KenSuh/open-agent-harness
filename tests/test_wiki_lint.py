# 위키 점검 검사 — 실제 문제만 세는지(링크 루트·코드 속 링크·아카이브 색인·커밋 날짜), index.md 배너·줄 길이, hygiene 증감과 기준선을 본다.
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import unittest
from unittest import mock

from oah.wiki import notes
from oah.wiki.base import WikiError, WikiSettings
from oah.wiki.store import Wiki
from tests.helpers import git_env, make_git_repo, make_tmp_dir

TODAY = datetime.date(2026, 10, 23)
NOW = datetime.datetime(2026, 10, 23, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


def note(title: str, body: str = "", sources: str = '["https://example.com/a"]') -> str:
    return f"---\ntitle: {json.dumps(title)}\ndate: 2026-10-01\nsources: {sources}\ntags: []\n---\n{body}\n"


class LintCase(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        self.wiki_dir = self.repo / "docs" / "wiki"
        for rel, data in notes.scaffold_files(TODAY).items():
            path = self.wiki_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.state = make_tmp_dir(self) / "state"

    def wiki(self, **settings) -> Wiki:
        return Wiki(self.repo, WikiSettings(**settings), self.state)

    def write(self, rel: str, text: str) -> None:
        path = self.wiki_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def index(self, *names: str) -> None:
        self.write("index.md", "Notes:\n- [[wiki-guide]]\n" + "".join(f"- [[{n}]]\n" for n in names))

    def kinds(self, report, path: str | None = None) -> list[str]:
        return [p.kind for p in report.problems if path is None or p.path == path]

    def commit(self, rel: str, text: str, when: str) -> None:
        self.write(rel, text)
        env = dict(git_env(), GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", rel],
                       check=True, env=env)


class LintTest(LintCase):
    def test_write_keeps_exact_utf8_and_line_endings(self):
        for text in ("가나다\n", "가나다\r\n", "a\r\nb\nc\r"):
            with self.subTest(text=text):
                self.write("fixture.md", text)
                self.assertEqual((self.wiki_dir / "fixture.md").read_bytes(), text.encode("utf-8"))

    def test_uncertain_code_links_are_reported(self):
        for text in ("```\ncode\n  ```\n[[real]]\n", "  ```\ncode\n```\n[[real]]\n",
                     "`open\n` [[real]] `\nclose`\n", "<div>\n```\n\n[[real]]\n```\n",
                     "---\n```\n---\n[[real]]\n```\n", "\\` [[real]] `\n", "```\n[[real]]\n",
                     "<!doctype html\n```\n>\n\n[[real]]\n```\n",
                     "<!dOcTyPe html\n```\n>\n\n[[real]]\n```\n",
                     "<![cdata[\n```\n]]>\n\n[[real]]\n```\n",
                     "text <!-- ` --> [[real]] <!-- ` -->\n",
                     "<https://example.org/`> [[real]] <https://example.org/`>\n",
                     "text <span>\n`[[real]]`\n", "2 < 3 ``[[real]]``\n",
                     "text <span\nattribute='value'>\n\n```\n[[real]]\n```\n",
                     "[ref]: /url\n\"possible title\"\n\n```\n[[real]]\n```\n"):
            for eol in ("\n", "\r\n"):
                with self.subTest(eol=eol, text=text):
                    self.write("index.md", text.replace("\n", eol))
                    report = self.wiki().lint(now=NOW)
                    self.assertEqual([(p.line, p.detail) for p in report.problems
                                      if p.path == "index.md" and p.kind == "broken_link"],
                                     [(text[:text.index("[[real]]")].count("\n") + 1,
                                       "[[real]] does not resolve")])

    def test_plain_column_zero_fence_still_hides_link(self):
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=eol):
                self.write("index.md", eol.join(("```", "[[hidden]]", "```", "")))
                self.assertFalse(any(p.kind == "broken_link" for p in self.wiki().lint(now=NOW).problems))

    def test_scaffold_passes_lint(self):
        report = self.wiki().lint(now=NOW)
        self.assertEqual(report.problems, [])
        self.assertEqual(report.summary(), "OAH_WIKI_LINT_CLEAN notes=1")
        self.assertEqual(self.wiki().lint(now=NOW, index=True).problems, [])
        self.commit("x.txt", "x", "2026-10-01T00:00:00Z")
        self.assertEqual(self.wiki().lint(now=NOW).problems, [])

    def test_link_roots_resolve(self):
        outside = make_tmp_dir(self) / "memory"
        (outside / "deep").mkdir(parents=True)
        (outside / "deep" / "agent-memory.md").write_text("x", encoding="utf-8")
        (self.repo / "notes-elsewhere").mkdir()
        (self.repo / "notes-elsewhere" / "plan.md").write_text("x", encoding="utf-8")
        self.write("a.md", note("A", "[[agent-memory]] [[plan]] [[deep/agent-memory]]"))
        self.index("a")
        self.assertEqual(self.kinds(self.wiki().lint(now=NOW)), ["broken_link"] * 3)
        settings = {"link_roots": [str(outside), "notes-elsewhere"]}
        self.assertEqual(self.wiki(**settings).lint(now=NOW).problems, [])

    def test_link_resolution_forms(self):
        self.write("sub/deep note.md", note("Deep"))
        self.write("img.png", "png")
        self.write("a.md", note("A", "[[deep note]] [[sub/deep note]] [[Deep Note.md]] ![[img.png]] [[#self]] "
                                     "[[deep note#Heading|alias]] [[index]] [[log]]"))
        self.index("a", "deep note")
        self.assertEqual(self.wiki().lint(now=NOW).problems, [])

    def test_top_level_fences_and_same_line_code_spans_ignored(self):
        body = ("Inline `[[nope1]]` and ``[[nope2]]``.\n\n```\n[[nope3]]\n```\n\n~~~md\n[[nope4]]\n~~~\n\n"
                "    [[nope5]]\n\nReal [[gone]].\n")
        self.write("a.md", note("A", body))
        self.index("a")
        report = self.wiki().lint(now=NOW)
        self.assertEqual([(p.kind, p.detail) for p in report.problems],
                         [("broken_link", f"[[{target}]] does not resolve") for target in ("nope5", "gone")])

    def test_links_in_container_fences_are_candidates(self):
        body = ("> ~~~md\n> [[hidden quote]]\n> ~~~\n\n"
                "- outer\n  - ~~~\n    [[hidden list]]\n\n    [[hidden after blank]]\n    ~~~\n\n"
                "[[gone]]\n")
        self.write("a.md", note("A", body))
        self.index("a")
        report = self.wiki().lint(now=NOW)
        self.assertEqual([(p.kind, p.detail) for p in report.problems],
                         [("broken_link", f"[[{target}]] does not resolve") for target in
                          ("hidden quote", "hidden list", "hidden after blank", "gone")])

    def test_archive_index_counts_as_index(self):
        self.write("from-log-index.md", note("From log index"))
        self.write("from-index-archive.md", note("From index archive"))
        self.write("from-log-entry.md", note("From log entry"))
        log_text = (self.wiki_dir / "log.md").read_text(encoding="utf-8").replace(
            "<!-- OAH:ARCHIVE_INDEX:END -->", "| [[from-log-index]] |\n<!-- OAH:ARCHIVE_INDEX:END -->")
        self.write("log.md", log_text + "\nSee [[from-log-entry]].\n")
        self.write("index_archive_01.md", "- [[from-index-archive]]\n")
        report = self.wiki().lint(now=NOW)
        unindexed = [p.path for p in report.problems if p.kind == "unindexed"]
        self.assertEqual(unindexed, ["from-log-entry.md"])
        self.assertNotIn("index_archive_01.md", [p.path for p in report.problems])

    def test_stale_uses_commit_date_not_mtime(self):
        self.index("old", "fresh", "uncommitted")
        self.commit("scaffold.txt", "x", "2026-10-20T00:00:00Z")
        self.commit("old.md", note("Old"), "2020-01-01T00:00:00Z")
        self.commit("fresh.md", note("Fresh"), "2026-10-13T00:00:00Z")
        self.write("uncommitted.md", note("Uncommitted"))
        os.utime(self.wiki_dir / "old.md", (NOW, NOW))
        ancient = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
        os.utime(self.wiki_dir / "fresh.md", (ancient, ancient))
        os.utime(self.wiki_dir / "uncommitted.md", (ancient, ancient))
        report = self.wiki().lint(now=NOW)
        self.assertEqual([p.path for p in report.problems if p.kind == "stale"], ["old.md"])
        self.assertEqual(self.wiki(stale_days=5000).lint(now=NOW).problems, [])

    def test_each_problem_kind(self):
        self.write("no-front.md", "just text [[missing-target]]\n")
        self.write("no-source.md", note("No source", sources="[]"))
        self.write("no-title.md", "---\ndate: 2026-10-01\nsources: [x]\n---\n")
        self.write("dup-a.md", note("Same"))
        self.write("dup-b.md", note("same "))
        self.write("orphan.md", note("Orphan"))
        self.write("linked.md", note("Linked", "[[orphan]]"))
        self.index("no-front", "no-source", "no-title", "dup-a", "dup-b", "linked")
        report = self.wiki().lint(now=NOW)
        self.assertEqual(report.counts(), {
            "broken_links": 1, "unindexed": 1, "missing_sources": 1, "missing_frontmatter": 2, "orphans": 0,
            "duplicate_titles": 2, "stale": 0, "banners": 0, "overlong_lines": 0})
        self.assertEqual(self.kinds(report, "orphan.md"), ["unindexed"])
        self.write("index.md", "Notes:\n- [[wiki-guide]]\n")
        report = self.wiki().lint(now=NOW)
        self.assertIn("orphan", self.kinds(report, "dup-a.md"))
        self.assertNotIn("orphan", self.kinds(report, "orphan.md"))

    def test_system_raw_and_templates_are_not_notes(self):
        self.write("raw/imported.md", "no frontmatter [[dangling]]\n")
        self.write("templates/extra.md", "{{title}} [[dangling]]\n")
        self.write("log_archive_01.md", "# Log archive 01\n<!-- OAH:LOG_INSERT_BELOW -->\n## 2020-01-01 a\n[[gone]]\n")
        self.write("note.md", note("Note", "[[imported]] [[extra]]"))
        self.index("note")
        self.assertEqual(self.wiki().lint(now=NOW).problems, [])

    def test_lint_never_writes_wiki(self):
        self.write("a.md", "broken [[x]]\n")
        before = {p: p.read_bytes() for p in self.wiki_dir.rglob("*") if p.is_file()}
        self.wiki().lint(now=NOW)
        self.wiki().lint(now=NOW, index=True)
        self.wiki().hygiene(now=NOW)
        after = {p: p.read_bytes() for p in self.wiki_dir.rglob("*") if p.is_file()}
        self.assertEqual(after, before)


class IndexLintTest(LintCase):
    def banners(self, text: str, **settings) -> list[int]:
        self.write("index.md", text)
        report = self.wiki(**settings).lint(now=NOW, index=True)
        return [p.line for p in report.problems if p.kind == "banner"]

    def test_index_banner_patterns(self):
        lines = ["# H", "###### H", "####### seven", "#nospace", "  ## padded  ", "---", "***", "___",
                 "- - -", "= = =", "--", "-*-", "-- -", "- [[wiki-guide]]", "Notes:", "* * * *", "----x"]
        expected = [1, 2, 5, 6, 7, 8, 9, 10, 12, 13, 16]
        self.assertEqual(self.banners("\n".join(lines) + "\n"), expected)

    def test_index_fenced_banners_ignored(self):
        text = "---\ntitle: Index\n---\n- [[wiki-guide]]\n```\n# inside\n---\n```\n~~~\n## also\n~~~\n# out\n"
        self.assertEqual(self.banners(text), [12])

    def test_index_line_limit_boundary(self):
        lines = ["a" * 120, "b" * 121, "가" * 120, "나" * 121, "- [[wiki-guide]]"]
        self.write("index.md", "\r\n".join(lines) + "\r\n")
        report = self.wiki().lint(now=NOW, index=True)
        self.assertEqual([p.line for p in report.problems if p.kind == "overlong_line"], [2, 4])
        report = self.wiki(index_line_limit=16).lint(now=NOW, index=True)
        self.assertEqual([p.line for p in report.problems if p.kind == "overlong_line"], [1, 2, 3, 4])
        self.write("index.md", "c" * 16 + "\n- [[wiki-guide]]\n")
        report = self.wiki(index_line_limit=16).lint(now=NOW, index=True)
        self.assertEqual([p.kind for p in report.problems], [])

    def test_index_reports_unindexed_with_normal_rules(self):
        self.write("lost.md", note("Lost", "[[wiki-guide]]"))
        self.write("broken.md", "[[nowhere]]\n")
        self.index("broken")
        report = self.wiki().lint(now=NOW, index=True)
        self.assertEqual([(p.kind, p.path) for p in report.problems], [("unindexed", "lost.md")])

    def test_index_candidates_written_outside_wiki(self):
        self.write("index.md", "# Banner\n- [[wiki-guide]]\n")
        before = {p: p.read_bytes() for p in self.wiki_dir.rglob("*") if p.is_file()}
        self.wiki().lint(now=NOW, index=True)
        self.assertEqual({p: p.read_bytes() for p in self.wiki_dir.rglob("*") if p.is_file()}, before)
        data = json.loads((self.state / "wiki" / "index-candidates.json").read_text(encoding="utf-8"))
        self.assertEqual([(c["kind"], c["line"]) for c in data["candidates"]], [("banner", 1)])
        self.assertFalse(str(self.state).startswith(str(self.wiki_dir)))


class HygieneTest(LintCase):
    def line(self, **settings) -> dict[str, str]:
        out = self.wiki(**settings).hygiene(now=NOW)
        self.assertTrue(out.startswith("OAH_WIKI_HYGIENE "))
        self.assertNotIn("\n", out)
        return dict(part.split("=", 1) for part in out.split()[1:])

    def test_hygiene_first_run_na(self):
        values = self.line()
        self.assertEqual(list(values), ["broken_links", "unindexed", "missing_sources", "missing_frontmatter",
                                        "orphans", "duplicate_titles", "stale", "banners", "overlong_lines"])
        self.assertTrue(all(value == "0(n/a)" for value in values.values()), values)
        stored = json.loads((self.state / "wiki" / "hygiene.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["config"]["index_line_limit"], 120)

    def test_hygiene_signed_deltas(self):
        self.line()
        self.write("a.md", "[[x]] [[y]]\n")
        self.write("index.md", "# Banner\n- [[wiki-guide]]\n- [[a]]\n")
        values = self.line()
        self.assertEqual(values["broken_links"], "2(+2)")
        self.assertEqual(values["banners"], "1(+1)")
        self.assertEqual(values["stale"], "0(+0)")
        (self.wiki_dir / "a.md").write_text("[[x]]\n", encoding="utf-8")
        values = self.line()
        self.assertEqual(values["broken_links"], "1(-1)")
        self.assertEqual(values["banners"], "1(+0)")

    def test_hygiene_config_change_resets(self):
        self.line()
        values = self.line(index_line_limit=10)
        self.assertTrue(all(value.endswith("(n/a)") for value in values.values()), values)
        values = self.line(index_line_limit=10)
        self.assertTrue(all(value.endswith("(+0)") for value in values.values()), values)
        self.assertTrue(all(value.endswith("(n/a)") for value in self.line(link_roots=["x"]).values()))

    def test_hygiene_failed_scan_keeps_baseline(self):
        self.line()
        baseline = (self.state / "wiki" / "hygiene.json").read_bytes()
        (self.wiki_dir / "index.md").write_text("# Banner\n", encoding="utf-8")
        def broken_git(root, wiki):
            raise WikiError("OAH_GIT_FAILED", "git log failed", 69)

        with mock.patch("oah.wiki.lint.commit_times", broken_git), self.assertRaises(WikiError):
            self.wiki().hygiene(now=NOW)
        self.assertEqual((self.state / "wiki" / "hygiene.json").read_bytes(), baseline)
        self.write("x.md", "[[gone]]\n")
        journal = self.state / "wiki" / "rollover-journal.json"
        journal.write_text("{}", encoding="utf-8")
        with self.assertRaises(WikiError):
            self.wiki().hygiene(now=NOW)
        self.assertEqual((self.state / "wiki" / "hygiene.json").read_bytes(), baseline)


if __name__ == "__main__":
    unittest.main()
