# 리스 범위 규칙 검사 — 경로 정규화 표, 겹침 표(대소문자·유니코드 정규화 포함), 범위 밖 파일 목록을 본다.
from __future__ import annotations

import os
import unicodedata
import unittest

from oah.scope import ScopeError, normalize_scope, outside, overlaps
from tests.helpers import directory_alias, make_tmp_dir


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        self.root = make_tmp_dir(self) / "repo"
        self.root.mkdir()

    def test_scope_normalization_table(self):
        cases = [
            (["src\\a.py"], ("src/a.py",)),
            (["./src/../src/a.py"], ("src/a.py",)),
            (["src/"], ("src",)),
            (["src//lib///x.py"], ("src/lib/x.py",)),
            ([str(self.root / "docs" / "x.md")], ("docs/x.md",)),
            (["."], ("repo",)),
            (["repo", "src"], ("repo",)),
            (["src", "SRC", "docs"], ("src", "docs")),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_scope(raw, self.root), expected)

    def test_invalid_scopes(self):
        for raw in (["../x"], ["src/../../x"], [str(self.root.parent / "other")], [""], ["   "], ["c:x"]):
            with self.subTest(raw=raw):
                with self.assertRaises(ScopeError) as ctx:
                    normalize_scope(raw, self.root)
                self.assertEqual(ctx.exception.code, "OAH_SCOPE_INVALID")

    def test_empty_scope_is_refused(self):
        with self.assertRaises(ScopeError) as ctx:
            normalize_scope([], self.root)
        self.assertEqual(ctx.exception.code, "OAH_SCOPE_EMPTY")

    def test_directory_alias_has_the_target_scope(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "link")
        for path in ("link/a.py", str(self.root / "link" / "a.py")):
            with self.subTest(path=path):
                alias = normalize_scope([path], self.root)
                target = normalize_scope(["src/a.py"], self.root)
                self.assertEqual(alias, target)
                self.assertTrue(overlaps(alias, target))

    def test_alias_escape_is_refused_after_resolution(self):
        other = self.root.parent / "other"
        other.mkdir()
        directory_alias(other, self.root / "link")
        for path in ("link/a.py", str(self.root / "link" / "a.py"), "link/../a.py"):
            with self.subTest(path=path):
                with self.assertRaises(ScopeError) as caught:
                    normalize_scope([path], self.root)
                self.assertEqual(caught.exception.code, "OAH_SCOPE_INVALID")

    def test_changed_paths_resolve_aliases_too(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "link")
        self.assertEqual(outside(("src",), ["link/a.py"], root=self.root), [])
        self.assertEqual(outside(("link",), ["src/a.py"], root=self.root), [])

    def test_windows_ambiguous_components_are_refused_portably(self):
        for path in ("src/a.py.", "src/a.py ", "src./a.py", "src /a.py", "src/.. /a.py"):
            with self.subTest(path=path):
                with self.assertRaises(ScopeError) as caught:
                    normalize_scope([path], self.root)
                self.assertEqual(caught.exception.code, "OAH_SCOPE_INVALID")

    @unittest.skipUnless(os.name == "nt", "native Windows trailing-dot and space aliases")
    def test_native_windows_aliases_are_refused(self):
        (self.root / "src").mkdir()
        target = self.root / "src" / "a.py"
        target.write_text("same file", encoding="utf-8")
        for path in ("src/a.py.", "src/a.py ", "src./a.py"):
            with self.subTest(path=path):
                self.assertEqual((self.root / path).read_text(encoding="utf-8"), "same file")
                with self.assertRaises(ScopeError):
                    normalize_scope([path], self.root)


class OverlapTest(unittest.TestCase):
    def test_overlap_table(self):
        cases = [
            (("src",), ("src/a.py",), True),
            (("src/a.py",), ("src",), True),
            (("src",), ("srcx",), False),
            (("SRC",), ("src/a.py",), True),
            (("repo",), ("docs",), True),
            (("docs",), ("repo",), True),
            (("docs",), ("src",), False),
            (("docs", "tests"), ("src", "tests/unit"), True),
        ]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(overlaps(a, b), expected)

    def test_overlap_ignores_unicode_normalization(self):
        composed = unicodedata.normalize("NFC", "문서/한글")
        decomposed = unicodedata.normalize("NFD", "문서/한글")
        self.assertNotEqual(composed, decomposed)
        self.assertTrue(overlaps((composed,), (decomposed + "/a.md",)))

    def test_outside_lists_only_files_beyond_scope(self):
        changed = ["src/a.py", "docs/b.md", "srcx/c.py", "SRC/d.py"]
        self.assertEqual(outside(("src",), changed), ["docs/b.md", "srcx/c.py"])
        self.assertEqual(outside(("repo",), changed), [])


if __name__ == "__main__":
    unittest.main()
