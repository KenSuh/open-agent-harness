# 리스 범위 규칙 검사 — 경로 정규화 표, 겹침 표(대소문자·유니코드 정규화 포함), 범위 밖 파일 목록을 본다.
from __future__ import annotations

import os
import pathlib
import unicodedata
import unittest
from unittest.mock import patch

from oah.scope import ScopeError, normalize_scope, outside, overlaps
from tests.helpers import directory_alias, make_git_repo, make_tmp_dir


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

    def test_windows_drive_relative_and_stream_scopes_are_refused_before_resolution(self):
        paths = ("c:x", "C:", "c:src/a.py", "src/a.py:zone")
        with patch("oah.scope._relative_to_root", side_effect=AssertionError("resolved invalid raw scope")):
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaises(ScopeError) as caught:
                        normalize_scope([path], self.root)
                    self.assertEqual(caught.exception.code, "OAH_SCOPE_INVALID")

    def test_absolute_stream_scopes_are_refused_after_resolution(self):
        for path in (str(self.root / "src" / "a.py:zone"), "//server/share/src/a.py:zone"):
            with self.subTest(path=path), patch("oah.scope._relative_to_root", return_value="src/a.py:zone") as resolve:
                with self.assertRaises(ScopeError) as caught:
                    normalize_scope([path], self.root)
                self.assertEqual(caught.exception.code, "OAH_SCOPE_INVALID")
                resolve.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "POSIX repository paths containing colons")
    def test_absolute_scope_under_repository_with_colon(self):
        repo = make_git_repo(self)
        root = repo.rename(repo.with_name("my:repo"))
        (root / "src").mkdir()
        self.assertEqual(normalize_scope([str(root / "src")], root), ("src",))
        (root / "src" / "a:b").write_text("colon filename", encoding="utf-8")
        with self.assertRaises(ScopeError) as caught:
            normalize_scope([str(root / "src" / "a:b")], root)
        self.assertEqual(caught.exception.code, "OAH_SCOPE_INVALID")

    def test_windows_absolute_and_unc_scopes_reach_resolution(self):
        for path in (pathlib.PureWindowsPath("C:/repo/src"), pathlib.PureWindowsPath("//server/share/src")):
            with self.subTest(path=path), patch("oah.scope._relative_to_root", return_value="src") as resolve:
                self.assertTrue(path.drive)
                self.assertTrue(path.root)
                self.assertEqual(normalize_scope([str(path)], self.root), ("src",))
                resolve.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "native Windows drive-relative paths")
    def test_native_windows_drive_relative_scopes_are_refused(self):
        for path in (self.root.drive + "x", self.root.drive, self.root.drive + "src/a.py", "src/a.py:zone",
                     str(self.root / "src" / "a.py:zone")):
            with self.subTest(path=path), self.assertRaises(ScopeError):
                normalize_scope([path], self.root)
        self.assertEqual(normalize_scope([str(self.root / "src")], self.root), ("src",))

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
        for path in ("link/a.py", str(self.root / "link" / "a.py")):
            with self.subTest(path=path):
                with self.assertRaises(ScopeError) as caught:
                    normalize_scope([path], self.root)
                self.assertEqual(caught.exception.code, "OAH_SCOPE_INVALID")
        if os.name == "nt":
            self.assertEqual(normalize_scope(["link/../a.py"], self.root), ("a.py",))
        else:
            with self.assertRaises(ScopeError) as caught:
                normalize_scope(["link/../a.py"], self.root)
            self.assertEqual(caught.exception.code, "OAH_SCOPE_INVALID")

    def test_parent_after_inward_alias_follows_native_open_semantics(self):
        (self.root / "deep" / "dir").mkdir(parents=True)
        directory_alias(self.root / "deep" / "dir", self.root / "link")
        expected = ("a.py",) if os.name == "nt" else ("deep/a.py",)
        actual = normalize_scope(["link/../a.py"], self.root)
        self.assertEqual(actual, expected)
        self.assertEqual(overlaps(actual, ("deep",)), os.name != "nt")

    @unittest.skipUnless(os.name == "nt", "native Windows junction parent semantics")
    def test_windows_junction_parent_opens_lexical_path(self):
        other = self.root.parent / "other"
        other.mkdir()
        directory_alias(other, self.root / "link")
        (self.root / "a.py").write_text("inside", encoding="utf-8")
        self.assertEqual((self.root / "link" / ".." / "a.py").read_text(encoding="utf-8"), "inside")
        self.assertEqual(normalize_scope(["link/../a.py"], self.root), ("a.py",))

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
    def test_git_paths_are_literal_except_for_case_and_unicode_comparison(self):
        decomposed = unicodedata.normalize("NFD", "문서/a.md")
        changed = ["SRC/a.py", "src\\a.py", "src/a.py:zone", "src/link", "docs/link", decomposed]
        self.assertEqual(outside(("src", "문서"), changed), ["src\\a.py", "docs/link"])

    def test_repo_scope_does_not_inspect_changed_entries(self):
        def changed():
            raise AssertionError("repo scope must be checked before reading changed entries")
            yield

        self.assertEqual(outside(("repo",), changed()), [])
        with patch("pathlib.Path.resolve", side_effect=AssertionError("repo scope must not resolve paths")):
            self.assertEqual(outside(("repo",), changed(), root=pathlib.Path("missing")), [])

    def test_outside_resolves_parent_aliases_and_keeps_final_entries_literal(self):
        root = make_tmp_dir(self)
        (root / "src").mkdir()
        (root / "docs").mkdir()
        directory_alias(root / "src", root / "link")
        directory_alias(root / "docs", root / "other")
        directory_alias(root / "src", root / "docs" / "link")
        changed = ["link/a.py", "docs/link", "other/b.md", "link/missing/a.py"]
        self.assertEqual(outside(("src/a.py",), changed), changed)
        for scope in (("src/a.py",), ("src",)):
            with self.subTest(scope=scope):
                self.assertEqual(outside(scope, changed, root=root), changed)
                self.assertEqual(outside(scope, iter(changed + ["src/a.py"]), root=root), changed[1:])

    def test_outside_canonical_twin_comparison_folds_case_and_unicode(self):
        root = make_tmp_dir(self)
        (root / "src").mkdir()
        directory_alias(root / "src", root / "link")
        decomposed = unicodedata.normalize("NFD", "문서.py")
        changed = ["link/문서.py", f"SRC/{decomposed}"]
        self.assertEqual(outside(("src",), changed, root=root), [])

    def test_outside_refuses_changes_under_directory_replaced_by_alias(self):
        for variant in ("deleted", "modified"):
            with self.subTest(variant=variant):
                root = make_tmp_dir(self)
                (root / "src").mkdir()
                (root / "src" / "a.py").write_text("unchanged\n", encoding="utf-8")
                if variant == "modified":
                    (root / "src" / "readme.md").write_text("unchanged target\n", encoding="utf-8")
                (root / "docs").mkdir()
                (root / "docs" / "readme.md").write_text("original\n", encoding="utf-8")
                (root / "docs" / "readme.md").unlink()
                (root / "docs").rmdir()
                directory_alias(root / "src", root / "docs")
                changed = ["docs/readme.md", "docs/a.py"]
                self.assertEqual(outside(("src",), changed, root=root), changed)

    def test_outside_ignores_parents_resolved_outside_root(self):
        root = make_tmp_dir(self)
        target = make_tmp_dir(self)
        (target / "src").mkdir()
        directory_alias(target / "src", root / "link")
        self.assertEqual(outside(("src",), ["link/a.py"], root=root), ["link/a.py"])

    @unittest.skipUnless(os.name == "posix", "POSIX parent symlink loop")
    def test_outside_parent_symlink_loop_does_not_raise(self):
        root = make_tmp_dir(self)
        (root / "loop").symlink_to("loop", target_is_directory=True)
        self.assertEqual(outside(("src",), ["loop/a.py"], root=root), ["loop/a.py"])

    def test_outside_parent_os_error_does_not_raise(self):
        root = make_tmp_dir(self)
        with patch("pathlib.Path.resolve", side_effect=OSError("unavailable parent")):
            self.assertEqual(outside(("src",), ["src/a.py", "link/a.py"], root=root), ["link/a.py"])

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
