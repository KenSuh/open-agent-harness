# 작업 폴더(scratch) 검사 — 사용자 전용 OAH 폴더만 증명해 쓰고, 안전하지 않으면 시스템 임시 폴더로 물러서지 않고 OAH_SCRATCH_UNSAFE 로 멈추는지 본다.
from __future__ import annotations

import contextlib
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock

from oah import init, scratch, unlazy
from tests.helpers import hostile_temp, make_private_dir, make_tmp_dir, scratch_base, scratch_env

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "oah"
SID = "S-1-5-21-1000-2000-3000-1001"
TRUSTED = f"O:{SID}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{SID})"
OWNER_ONLY = f"O:{SID}D:P(A;OICI;FA;;;OW)"
CANONICAL = {"OW": "S-1-3-4", "SY": "S-1-5-18", "BA": "S-1-5-32-544", "WD": "S-1-1-0", "AU": "S-1-5-11"}


def fresh_env(test: unittest.TestCase, platform: str | None = None) -> dict[str, str]:
    """A scratch variable under a new temporary folder whose base does not exist yet."""
    return scratch_env(make_tmp_dir(test) / "user", platform)


class RuleTest(unittest.TestCase):
    def test_the_base_follows_the_documented_rule_for_each_os(self):
        root = make_tmp_dir(self)
        home, local, cache = root / "home", root / "local", root / "cache"
        cases = [
            ("win32", {"LOCALAPPDATA": str(local), "HOME": str(home)}, local / "oah" / "scratch"),
            ("darwin", {"HOME": str(home), "XDG_CACHE_HOME": str(cache)},
             home / "Library" / "Caches" / "oah" / "scratch"),
            ("linux", {"HOME": str(home), "XDG_CACHE_HOME": str(cache)}, cache / "oah" / "scratch"),
            ("linux", {"HOME": str(home), "XDG_CACHE_HOME": "relative/cache"}, home / ".cache" / "oah" / "scratch"),
            ("linux", {"HOME": str(home)}, home / ".cache" / "oah" / "scratch"),
        ]
        for platform, env, expected in cases:
            with self.subTest(platform=platform, env=env):
                self.assertEqual(scratch.base_path(env, platform), expected)
                self.assertEqual(scratch_base(env, platform), expected)
        self.assertEqual(list(root.iterdir()), [], "working out the base creates nothing")

    def test_windows_needs_an_absolute_localappdata_and_never_guesses(self):
        for env in ({}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "relative\\local"}):
            with self.subTest(env=env), hostile_temp(self) as (shared, created):
                with self.assertRaises(scratch.ScratchError) as caught:
                    scratch.base(env, platform="win32")
                self.assertEqual(caught.exception.code, "OAH_SCRATCH_UNSAFE")
                self.assertIn("LOCALAPPDATA", str(caught.exception))
                self.assertEqual((created, list(shared.iterdir())), ([], []), "no fallback to the system temp")

    def test_a_relative_or_overlong_base_refuses_before_creating_anything(self):
        root = make_tmp_dir(self)
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.base({"HOME": "relative-home"}, platform="darwin")
        self.assertIn("absolute", str(caught.exception))
        deep = root.joinpath(*["d"] * 70)
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.base(scratch_env(deep))
        self.assertIn("too long", str(caught.exception))
        self.assertEqual(list(root.iterdir()), [])

    def test_the_refusal_is_an_oserror_naming_its_code_base_reason_and_fix(self):
        error = scratch.ScratchError(pathlib.Path("/srv/cache/oah/scratch"), "a parent is shared")
        self.assertIsInstance(error, OSError)
        self.assertEqual((error.code, error.base, error.reason),
                         ("OAH_SCRATCH_UNSAFE", str(pathlib.Path("/srv/cache/oah/scratch")), "a parent is shared"))
        self.assertTrue(str(error).startswith("OAH_SCRATCH_UNSAFE: "), str(error))
        self.assertIn(error.base, str(error))
        self.assertIn("a parent is shared", str(error))
        self.assertIn("then retry", str(error))
        self.assertEqual(str(error), f"OAH_SCRATCH_UNSAFE: {error.detail}")


class ProofTest(unittest.TestCase):
    def counting(self) -> list[pathlib.Path]:
        calls: list[pathlib.Path] = []
        real = unlazy._verify_state_ancestry

        def counted(folder, **kwargs):
            calls.append(pathlib.Path(folder))
            return real(folder, **kwargs)

        self.enterContext(mock.patch.object(unlazy, "_verify_state_ancestry", counted))
        return calls

    def test_a_private_base_is_created_owner_only_and_proven_once_per_process(self):
        env = fresh_env(self)
        calls = self.counting()
        first = scratch.base(env)
        self.assertEqual(first, scratch_base(env))
        self.assertEqual(calls, [first, first], "checked before and after creating")
        self.assertEqual(scratch.base(env), first)
        with mock.patch.dict(os.environ, env):
            scratch.make("oah-test-")
            with scratch.directory("oah-test-"):
                pass
        self.assertEqual(len(calls), 2, "the proven base is reused")
        if os.name == "nt":
            init._verify_windows_private_descriptor(unlazy._windows_security_descriptor(first), directory=True)
        else:
            for folder in (first, first.parent, first.parent.parent):
                info = folder.lstat()
                self.assertEqual((stat.S_IMODE(info.st_mode), info.st_uid), (0o700, os.getuid()), folder)
        shutil.rmtree(first)
        self.assertEqual(scratch.base(env), first)
        self.assertEqual(len(calls), 4, "a vanished base is proven again, never trusted from memory")

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_a_group_or_world_writable_parent_refuses_with_no_fallback(self):
        for mode in (0o777, 0o775):
            parent = make_tmp_dir(self) / "parent"
            parent.mkdir()
            parent.chmod(mode)
            env = scratch_env(parent / "user")
            for call in (lambda: scratch.make("oah-test-"), lambda: scratch.directory("oah-test-").cleanup()):
                with self.subTest(mode=oct(mode)), hostile_temp(self) as (shared, created), \
                        mock.patch.dict(os.environ, env):
                    with self.assertRaises(scratch.ScratchError) as caught:
                        call()
                    self.assertEqual((created, list(shared.iterdir())), ([], []), "no fallback to the system temp")
                message = str(caught.exception)
                self.assertTrue(message.startswith("OAH_SCRATCH_UNSAFE: "), message)
                self.assertEqual(caught.exception.base, str(scratch_base(env)))
                self.assertIn(str(scratch_base(env)), message)
                self.assertIn(f"{parent}: the directory permits group or other accounts to replace its children",
                              message)
                self.assertIn("chmod 700", message)
                self.assertEqual(list(parent.iterdir()), [], "nothing is created below the unsafe parent")

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_a_sticky_shared_parent_like_tmp_is_accepted(self):
        parent = make_tmp_dir(self) / "sticky"
        parent.mkdir()
        parent.chmod(0o1777)
        env = scratch_env(parent / "user")
        self.assertEqual(scratch.base(env), scratch_base(env))

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_an_existing_base_others_can_read_refuses_and_is_left_as_it_is(self):
        env = fresh_env(self)
        base = scratch_base(env)
        base.mkdir(parents=True)
        base.chmod(0o755)
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.base(env)
        self.assertIn("0755", str(caught.exception))
        self.assertIn(str(base), str(caught.exception))
        self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o755, "the proof never repairs a folder")

    def test_children_are_owner_only_and_the_context_manager_removes_its_folder(self):
        env = fresh_env(self)
        with mock.patch.dict(os.environ, env):
            child = scratch.make("oah-test-")
            with scratch.directory("oah-test-") as name:
                folder = pathlib.Path(name)
                self.assertTrue(folder.is_dir())
                self.assertEqual(folder.parent, scratch_base(env))
            self.assertFalse(os.path.lexists(folder))
        self.assertEqual(child.parent, scratch_base(env))
        self.assertTrue(child.name.startswith("oah-test-"))
        if os.name == "nt":
            unlazy._verify_windows_dacl(unlazy._windows_security_descriptor(child), unlazy._windows_user_sid(),
                                        snapshot=True)
        else:
            self.assertEqual((stat.S_IMODE(child.lstat().st_mode), child.lstat().st_uid), (0o700, os.getuid()))

    def test_a_passed_root_goes_through_the_same_proof(self):
        root = make_private_dir(self)
        self.assertEqual(scratch.make("oah-test-", root=root).parent, root)
        missing = make_tmp_dir(self) / "new-root"
        with scratch.directory("oah-test-", root=missing) as name:
            self.assertEqual(pathlib.Path(name).parent, missing)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(missing.stat().st_mode), 0o700, "a missing root is created owner-only")
            for mode in (0o777, 0o755):
                with self.subTest(mode=oct(mode)):
                    shared = make_tmp_dir(self) / "root"
                    shared.mkdir()
                    shared.chmod(mode)
                    with self.assertRaises(scratch.ScratchError) as caught:
                        scratch.make("oah-test-", root=shared)
                    self.assertIn(str(shared), str(caught.exception))
                    self.assertEqual(list(shared.iterdir()), [])


class WindowsSeamTest(unittest.TestCase):
    """The Windows rules through the landed verifier's seams, on every OS (`platform="win32"`)."""

    @contextlib.contextmanager
    def descriptors(self, overrides: dict[pathlib.Path, str]):
        with mock.patch.object(unlazy, "_windows_user_sid", return_value=SID), \
                mock.patch.object(unlazy, "_windows_canonical_sid", side_effect=lambda name: CANONICAL.get(name, name)), \
                mock.patch.object(unlazy, "_windows_security_descriptor",
                                  side_effect=lambda path: overrides.get(pathlib.Path(path), TRUSTED)):
            yield

    def test_an_everyone_delete_child_or_full_control_parent_refuses_before_creating(self):
        for rights in ("0x40", "FA"):  # FILE_DELETE_CHILD; full control
            local = make_tmp_dir(self) / "local"
            local.mkdir()
            hostile = f"O:{SID}D:(A;OICI;FA;;;{SID})(A;OICI;{rights};;;WD)"
            with self.subTest(rights=rights), hostile_temp(self) as (shared, created), \
                    self.descriptors({local: hostile}):
                with self.assertRaises(scratch.ScratchError) as caught:
                    scratch.base({"LOCALAPPDATA": str(local)}, platform="win32")
                self.assertEqual((created, list(shared.iterdir())), ([], []), "no fallback to the system temp")
            self.assertIn(f"{local}: SID S-1-1-0 grants rejected rights", str(caught.exception))
            self.assertEqual(list(local.iterdir()), [], "nothing is created below the unsafe parent")

    def test_an_everyone_write_grant_on_the_base_refuses_and_an_owner_only_base_is_accepted(self):
        local = make_tmp_dir(self) / "local"
        base = local / "oah" / "scratch"
        base.mkdir(parents=True)
        writable = f"O:{SID}D:P(A;OICI;FA;;;OW)(A;OICI;FW;;;WD)"
        with self.descriptors({base: writable}), self.assertRaises(scratch.ScratchError) as caught:
            scratch.base({"LOCALAPPDATA": str(local)}, platform="win32")
        self.assertIn("private Windows permissions cannot be verified", str(caught.exception))
        with self.descriptors({base: OWNER_ONLY}):
            self.assertEqual(scratch.base({"LOCALAPPDATA": str(local)}, platform="win32"), base)


@unittest.skipUnless(sys.platform == "darwin", "macOS shows '@' instead of '+' when a folder also has attributes")
class MacLibraryShapeTest(unittest.TestCase):
    """A real ~/Library carries an extended attribute and a deny-only ACL; the macOS base sits below it."""

    def library(self, entry: str) -> pathlib.Path:
        home = make_tmp_dir(self) / "home"
        library = home / "Library"
        library.mkdir(parents=True, mode=0o700)
        subprocess.run(["/usr/bin/xattr", "-w", "com.example.oah-test", "1", str(library)], check=True)
        subprocess.run(["/bin/chmod", "+a", entry, str(library)], check=True)
        self.addCleanup(subprocess.run, ["/bin/chmod", "-N", str(library)], check=False)
        listing = subprocess.run(["/bin/ls", "-lde", str(library)], capture_output=True, text=True, check=True,
                                 env=dict(os.environ, LC_ALL="C")).stdout.splitlines()
        self.assertTrue(listing[0].split()[0].endswith("@"), listing)  # the control: no '+' marker is shown
        self.assertEqual(len(listing), 2, listing)  # and the ACL entry is still listed
        return home

    def test_a_deny_only_acl_behind_the_attribute_marker_is_accepted(self):
        home = self.library("group:everyone deny delete")
        self.assertEqual(scratch.base({"HOME": str(home)}), scratch_base({"HOME": str(home)}))

    def test_an_access_grant_behind_the_attribute_marker_still_refuses(self):
        home = self.library("group:everyone allow add_subdirectory,delete_child")
        with self.assertRaises(scratch.ScratchError) as caught:
            scratch.base({"HOME": str(home)})
        self.assertIn("contains an access grant", str(caught.exception))
        self.assertFalse((home / "Library" / "Caches").exists())


class LeftoversTest(unittest.TestCase):
    def test_leftovers_are_counted_read_only(self):
        env = fresh_env(self)
        empty = scratch.leftovers(env)
        self.assertEqual(empty, {"base": str(scratch.base_path(env)), "exists": False, "count": 0, "bytes": 0,
                                 "unreadable": 0})
        self.assertFalse(os.path.lexists(scratch_base(env)), "counting never creates the base")
        with mock.patch.dict(os.environ, env):
            first, second = scratch.make("oah-fix-"), scratch.make("oah-review-")
        (first / "a.txt").write_bytes(b"x" * 10)
        (second / "sub").mkdir()
        (second / "sub" / "b.txt").write_bytes(b"y" * 5)
        report = scratch.leftovers(env)
        self.assertEqual((report["exists"], report["count"], report["bytes"], report["unreadable"]), (True, 2, 15, 0))
        self.assertTrue(first.is_dir() and second.is_dir(), "counting never removes anything")


class SourceTreeTest(unittest.TestCase):
    """Only oah/scratch.py creates temporary folders; the census scratch under private state (S14) stays."""

    ALLOWED = {("review/snapshot.py", "import tempfile"),
               ("review/snapshot.py", 'self.scratch = tempfile.TemporaryDirectory(prefix="census-", dir=directory)')}
    USE = re.compile(r"\btempfile\b|\bmkdtemp\b|\bmkstemp\b|\bgettempdir\b|TemporaryDirectory|NamedTemporaryFile")

    def test_no_module_but_scratch_uses_the_system_temp(self):
        hits = set()
        for path in sorted(SRC.rglob("*.py")):
            relative = path.relative_to(SRC).as_posix()
            if relative == "scratch.py":
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if self.USE.search(line):
                    hits.add((relative, line.strip()))
        self.assertEqual(hits, self.ALLOWED)

    def test_the_check_sees_aliases_and_the_module_default(self):
        for line in ("from tempfile import mkdtemp as make", "tempfile.tempdir = folder", "__import__('tempfile')",
                     "os.path.join(gettempdir(), name)", "with TemporaryDirectory() as folder:"):
            with self.subTest(line=line):
                self.assertIsNotNone(self.USE.search(line))


if __name__ == "__main__":
    unittest.main()
