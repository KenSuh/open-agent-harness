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

    def test_directory_alias_records_canonical_and_lexical_spellings(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "link")
        for path in ("link/a.py", "./link/./a.py", "link\\a.py", str(self.root / "link" / "a.py")):
            with self.subTest(path=path):
                alias = normalize_scope([path], self.root)
                target = normalize_scope(["src/a.py"], self.root)
                self.assertEqual(alias, ("src/a.py", "link/a.py"))
                self.assertTrue(overlaps(alias, target))
        self.assertEqual(normalize_scope(["link"], self.root), ("src", "link"))
        self.assertEqual(normalize_scope(["link/a.py", "src/a.py", "LINK/A.PY"], self.root),
                         ("src/a.py", "link/a.py"))
        self.assertEqual(normalize_scope(["link/a.py", "repo"], self.root), ("repo",))

    def test_absolute_alias_accepts_given_and_resolved_repository_root(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "link")
        given_root = self.root.parent / "repo-link"
        directory_alias(self.root, given_root)
        for root in (self.root, given_root):
            for path_root in (root, root.resolve()):
                with self.subTest(root=root, path_root=path_root):
                    self.assertEqual(normalize_scope([str(path_root / "link" / "a.py")], root),
                                     ("src/a.py", "link/a.py"))

    def test_case_only_differences_do_not_record_an_alias(self):
        (self.root / "src").mkdir()
        for path in ("SRC/a.py", str(self.root / "SRC" / "a.py")):
            with self.subTest(path=path):
                scope = normalize_scope([path], self.root)
                self.assertEqual(len(scope), 1)
                self.assertEqual(scope[0].casefold(), "src/a.py")
        with patch("oah.scope._relative_to_root", return_value="src/a.py"):
            self.assertEqual(normalize_scope(["SRC/a.py"], self.root), ("src/a.py",))

    @unittest.skipUnless(os.name == "posix", "POSIX repository root with a symlink parent")
    def test_absolute_alias_accepts_resolved_root_below_lexical_root(self):
        (self.root / "deep" / "dir").mkdir(parents=True)
        directory_alias(self.root / "deep" / "dir", self.root / "link")
        given_root = self.root / "link" / ".."
        resolved_root = given_root.resolve()
        (resolved_root / "src").mkdir()
        directory_alias(resolved_root / "src", resolved_root / "alias")
        for root in (given_root, resolved_root):
            with self.subTest(root=root):
                self.assertEqual(normalize_scope([str(root / "alias" / "a.py")], given_root),
                                 ("src/a.py", "alias/a.py"))

    @unittest.skipUnless(os.name == "posix", "POSIX alias spelling containing a colon")
    def test_invalid_lexical_alias_is_not_recorded(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "ali:as")
        self.assertEqual(normalize_scope([str(self.root / "ali:as" / "a.py")], self.root), ("src/a.py",))

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
        for path in ("link/../a.py", str(self.root / "link" / ".." / "a.py")):
            with self.subTest(path=path):
                actual = normalize_scope([path], self.root)
                self.assertEqual(actual, expected)
                self.assertEqual(overlaps(actual, ("deep",)), os.name != "nt")
        (self.root / "deep" / "dir" / "sub").mkdir()
        for path in ("link/sub/../a.py", str(self.root / "link" / "sub" / ".." / "a.py")):
            with self.subTest(path=path):
                self.assertEqual(normalize_scope([path], self.root), ("deep/dir/a.py", "link/a.py"))
        if os.name == "posix":
            self.assertEqual(normalize_scope(["link/../../a.py"], self.root), ("a.py",))
        else:
            with self.assertRaises(ScopeError):
                normalize_scope(["link/../../a.py"], self.root)

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
            self.assertEqual(outside(("repo",), changed()), [])

    def test_outside_covers_only_recorded_alias_entries(self):
        root = make_tmp_dir(self)
        (root / "src").mkdir()
        directory_alias(root / "src", root / "link")
        scope = normalize_scope(["link/a.py"], root)
        changed = ["link/a.py", "src/a.py", "link/b.py"]
        self.assertEqual(outside(scope, iter(changed)), ["link/b.py"])
        self.assertEqual(outside(("src/a.py",), changed), ["link/a.py", "link/b.py"])

    def test_outside_recorded_alias_comparison_folds_case_and_unicode(self):
        decomposed = unicodedata.normalize("NFD", "문서.py")
        changed = [f"LINK/{decomposed}", "SRC/문서.py"]
        self.assertEqual(outside(("src",), changed), [changed[0]])
        self.assertEqual(outside(("src/문서.py", "link/문서.py"), changed), [])

    @unittest.skipUnless(os.name == "posix", "POSIX Git symlink entries")
    def test_outside_keeps_inward_outward_and_dangling_symlink_entries_literal(self):
        root = make_tmp_dir(self)
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("inside", encoding="utf-8")
        (root / "docs").mkdir()
        target = make_tmp_dir(self) / "a.py"
        target.write_text("outside", encoding="utf-8")
        for name, path in (("inward", "../src/a.py"), ("outward", target), ("dangling", "missing.py")):
            with self.subTest(name=name):
                (root / "docs" / name).symlink_to(path)
                changed = [f"docs/{name}"]
                self.assertEqual(outside(("src",), changed), changed)
                self.assertEqual(outside(("docs",), changed), [])

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
                self.assertEqual(outside(("src",), changed), changed)

    def test_outside_refuses_replacement_alias_with_changed_canonical_twin(self):
        root = make_tmp_dir(self)
        for name in ("src", "docs"):
            (root / name).mkdir()
            (root / name / "readme.md").write_text("original", encoding="utf-8")
        for name in ("src", "docs"):
            (root / name / "readme.md").unlink()
        (root / "docs").rmdir()
        directory_alias(root / "src", root / "docs")
        self.assertEqual(outside(("src",), ["src/readme.md", "docs/readme.md"]), ["docs/readme.md"])

    def test_outside_keeps_scope_literal_after_directory_is_retargeted(self):
        root = make_tmp_dir(self)
        (root / "src").mkdir()
        (root / "docs").mkdir()
        scope = normalize_scope(["src"], root)
        (root / "src").rmdir()
        directory_alias(root / "docs", root / "src")
        self.assertEqual(outside(scope, ["src/a.py", "docs/a.py"]), ["docs/a.py"])

    def test_outside_keeps_outward_alias_entries_literal(self):
        root = make_tmp_dir(self)
        target = make_tmp_dir(self)
        (target / "src").mkdir()
        directory_alias(target / "src", root / "link")
        self.assertEqual(outside(("src",), ["link/a.py"]), ["link/a.py"])
        self.assertEqual(outside(("link",), ["link/a.py"]), [])

    @unittest.skipUnless(os.name == "posix", "POSIX parent symlink loop")
    def test_outside_parent_symlink_loop_does_not_raise(self):
        root = make_tmp_dir(self)
        (root / "loop").symlink_to("loop", target_is_directory=True)
        self.assertEqual(outside(("src",), ["loop/a.py"]), ["loop/a.py"])

    def test_outside_never_resolves_entries_or_acquired_scope(self):
        with patch("pathlib.Path.resolve", side_effect=AssertionError("resolved a Git path or acquired scope")):
            self.assertEqual(outside(("src",), ["src/a.py", "link/a.py"]), ["link/a.py"])
            self.assertEqual(outside(("src/a.py", "link/a.py"), ["link/a.py", "docs/a.py"]), ["docs/a.py"])

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
            (("src/a.py", "link/a.py"), ("link",), True),
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
