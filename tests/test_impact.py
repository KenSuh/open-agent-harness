# 영향 지도 검사 — 파일 종류 분류, 어휘 바늘 매칭, 명령 결속 판정, git grep, 질의 순위·상한·게이트·범위 경고를 본다.
from __future__ import annotations

import json
import pathlib
import unittest

from oah.impact import kinds, lexical
from tests.helpers import git_commit, make_git_repo


class KindsTest(unittest.TestCase):
    def test_classify_kinds(self):
        cases = {
            ".github/workflows/ci.yml": "workflow",
            "Makefile": "workflow",
            "db/migrations/001_init.sql": "migration",
            "schema/extra.sql": "migration",
            "tests/test_a.py": "test",
            "pkg/a_test.go": "test",
            "web/app.spec.ts": "test",
            "pyproject.toml": "manifest",
            "web/package.json": "manifest",
            "requirements-dev.txt": "manifest",
            "conf/app.yaml": "config",
            "settings.toml": "config",
            "README.md": "doc",
            "docs/guide/intro.html": "doc",
            "src/oah/cli.py": "code",
        }
        for path, kind in cases.items():
            with self.subTest(path=path):
                self.assertEqual(kinds.classify(path), kind)

    def test_glob_double_star(self):
        self.assertTrue(kinds.glob_match("**/*.sql", "a.sql"))
        self.assertTrue(kinds.glob_match("**/*.sql", "a/b/c.sql"))
        self.assertTrue(kinds.glob_match("docs/**", "docs/a/b.md"))
        self.assertFalse(kinds.glob_match("docs/**", "docsx/a.md"))
        self.assertFalse(kinds.glob_match("*.md", "a/b.md"))
        self.assertTrue(kinds.glob_match("test_?.py", "test_a.py"))
        self.assertFalse(kinds.glob_match("**/*.SQL", "a.sql"), "matching is case-sensitive")


class LexicalTest(unittest.TestCase):
    def test_needles_skip_short_parts(self):
        self.assertEqual(lexical.needles_for("src/oah/impact.py"),
                         {"src/oah/impact.py": "path", "impact.py": "basename", "impact": "stem"})
        self.assertEqual(lexical.needles_for("a.c"), {"a.c": "path"})
        self.assertEqual(lexical.needles_for("x/ab.py"), {"x/ab.py": "path", "ab.py": "basename"})
        self.assertEqual(lexical.needles_for("Makefile"), {"Makefile": "path"})
        self.assertEqual(lexical.needles_for("bad\nname.py"), {})

    def test_find_needles_boundaries(self):
        needles = {"src/oah/impact.py", "impact.py", "impact", "PLAN.md"}
        self.assertEqual(lexical.find_needles("see src/oah/impact.py:12 now", needles),
                         {"src/oah/impact.py", "impact.py", "impact"})
        self.assertEqual(lexical.find_needles("run ./x/src/oah/impact.py.", needles),
                         {"src/oah/impact.py", "impact.py", "impact"})
        self.assertEqual(lexical.find_needles(r"open src\oah\impact.py", needles),
                         {"src/oah/impact.py", "impact.py", "impact"})
        self.assertEqual(lexical.find_needles("from oah import impact_cli", needles), set())
        self.assertEqual(lexical.find_needles("the impactful thing", needles), set())
        self.assertEqual(lexical.find_needles("see [[impact]] and `docs/PLAN.md`", needles),
                         {"impact", "PLAN.md"})

    def test_find_identifiers_case(self):
        names = {"users", "audit_log"}
        self.assertEqual(lexical.find_identifiers('db.query("SELECT * FROM Users")', names), {"users"})
        self.assertEqual(lexical.find_identifiers("public.audit_log.id", names), {"audit_log"})
        self.assertEqual(lexical.find_identifiers("superusers and audit_logs", names), set())

    def test_classify_command_pin_exec(self):
        cases = [
            ("run: python3 tools/x.py --flag", "tools/x.py", "exec"),
            ("python -X utf8 tools/x.py", "x.py", "exec"),
            ("./tools/x.py arg", "tools/x.py", "exec"),
            ("- tools/x.py", "tools/x.py", "exec"),
            ('"test": "node scripts/check.js"', "scripts/check.js", "exec"),
            ("powershell -File tools\\x.ps1", "tools/x.ps1", "exec"),
            ("sha256sum tools/x.py", "tools/x.py", "pin"),
            ("git hash-object tools/x.py", "tools/x.py", "pin"),
            ("grep -c needle tools/x.py && echo ok", "tools/x.py", "pin"),
            ("echo ok && grep -q x tools/x.py", "tools/x.py", "pin"),
            ("python tools/y.py | grep tools/x.py", "tools/x.py", "pin"),
            ("python tools/x.py | grep ok", "tools/x.py", "exec"),
            ("see tools/x.py for details", "tools/x.py", None),
            ("python tools/other.py", "tools/x.py", None),
        ]
        for text, needle, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(lexical.classify_command(text, needle), expected)

    def test_grep_reads_ref_not_worktree(self):
        repo = make_git_repo(self)
        first = git_commit(repo, {"a b/doc.md": "see x.py here\r\nnothing\n", "x.py": "x\n"})
        git_commit(repo, {"a b/doc.md": "nothing\n"})
        (repo / "late.md").write_text("x.py uncommitted\n", encoding="utf-8")
        self.assertEqual(lexical.grep(repo, first, ["x.py"]), [("a b/doc.md", 1, "see x.py here")])
        self.assertEqual(lexical.grep(repo, "HEAD", ["x.py"]), [])
        self.assertEqual(lexical.grep(repo, first, ["X.PY"], ignore_case=True),
                         [("a b/doc.md", 1, "see x.py here")])
        self.assertEqual(lexical.grep(repo, first, ["x.py"], paths=["x.py"]), [])
        self.assertEqual(lexical.grep(repo, first, []), [])


if __name__ == "__main__":
    unittest.main()
