# 프로젝트 도구 영향 지도 연산 검사 — 선택된 작업 트리의 상태·갱신·멱등성, 루트와 Git 메타데이터 검증, 저장 경로 링크 거부, 적대적 git 설정 무력화, 그래프 연결 상태를 임시 저장소에서 실제 git 으로 본다.
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import unittest
from unittest import mock

from oah import paths
from oah import project_tools_impact as impact
from oah.impact import mapstore, query
from oah.impact.defaults import configured
from tests.helpers import directory_alias, git_commit, git_env, make_git_repo, make_tmp_dir

BASE = {
    "src/app.py": "from lib import util\nprint('app')\n",
    "src/lib/util.py": "def helper():\n    return 1\n",
    "docs/guide.md": "See src/app.py and util.py.\n",
    "notes.txt": "plain text\n",
}
POSIX = os.name != "nt"


def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True,
                          encoding="utf-8", env=git_env(), timeout=120)


def tree(root: pathlib.Path) -> dict[str, bytes]:
    """Every regular file under root, .git included."""
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()}


class SelectedCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("OAH_STATE_DIR", None)
        self.repo = make_git_repo(self)

    def status(self, root: pathlib.Path | None = None, **options) -> impact.Outcome:
        return impact.status(str(root or self.repo), selected=True, **options)

    def refresh(self, root: pathlib.Path | None = None, **options) -> impact.Outcome:
        return impact.refresh(str(root or self.repo), selected=True, **options)

    def refused(self, outcome: impact.Outcome, code: str) -> None:
        self.assertFalse(outcome.ok)
        self.assertEqual((outcome.state, outcome.error["code"]), ("unavailable", code), outcome.reason)


class StatesTest(SelectedCase):
    def test_plain_folder_is_a_state_for_status_and_a_refusal_for_refresh(self):
        plain = make_tmp_dir(self)
        seen = self.status(plain)
        self.assertEqual((seen.ok, seen.state, seen.error, seen.git), (True, "not_a_repository", None, None))
        tried = self.refresh(plain)
        self.assertEqual((tried.ok, tried.state, tried.error["code"]), (False, "not_a_repository", "OAH_NOT_A_REPO"))
        self.assertEqual(sorted(plain.iterdir()), [])

    def test_a_subfolder_of_a_repository_is_not_the_selected_worktree(self):
        git_commit(self.repo, BASE)
        self.assertEqual(self.status(self.repo / "src").state, "not_a_repository")
        self.assertFalse((self.repo / ".git" / "oah").exists())

    def test_a_repository_without_a_first_commit_waits_for_it(self):
        seen = self.status()
        self.assertEqual((seen.ok, seen.state, seen.head, seen.branch), (True, "no_commit", None, "main"))
        tried = self.refresh()
        self.assertEqual((tried.ok, tried.state, tried.error["code"]), (False, "no_commit", "OAH_IMPACT_NO_COMMIT"))
        self.assertFalse((self.repo / ".git" / "oah").exists())

    def test_missing_then_built_then_untouched_then_stale_then_refreshed(self):
        head = git_commit(self.repo, BASE)
        missing = self.status()
        self.assertEqual((missing.ok, missing.state, missing.reason, missing.map), (True, "missing", "missing", None))
        self.assertEqual((missing.head, missing.store["source"]), (head, "git-common-dir"))
        store = pathlib.Path(missing.store["path"])
        self.assertTrue(store.is_relative_to(self.repo / ".git" / "oah" / "impact" / "maps"))
        self.assertFalse(store.exists())

        built = self.refresh()
        self.assertEqual((built.ok, built.state, built.performed, built.changed), (True, "fresh", "built", len(BASE)))
        self.assertEqual((built.map["commit"], built.map["includes_uncommitted"]), (head, False))
        recorded, stamp = store.read_bytes(), store.stat().st_mtime_ns

        again = self.refresh()
        self.assertEqual((again.state, again.performed, again.changed, again.reason), ("fresh", "none", 0, ""))
        self.assertEqual((store.read_bytes(), store.stat().st_mtime_ns), (recorded, stamp), "a fresh map is untouched")
        self.assertEqual(sorted(path.name for path in store.parent.iterdir()), [store.name])
        self.assertEqual(self.status().state, "fresh")

        (self.repo / "src" / "app.py").write_text("from lib import util\nprint('edited')\n", encoding="utf-8")
        (self.repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        stale = self.status()
        self.assertEqual((stale.ok, stale.state, stale.reason), (True, "stale", "working tree changed"))
        self.assertEqual(store.read_bytes(), recorded, "status never writes")
        refreshed = self.refresh()
        self.assertEqual((refreshed.state, refreshed.performed), ("fresh", "refreshed"))
        self.assertEqual((refreshed.map["dirty"], refreshed.map["untracked"], refreshed.map["includes_uncommitted"]),
                         (1, 1, True))

    def test_status_is_read_only(self):
        git_commit(self.repo, BASE)
        before = tree(self.repo)
        self.assertEqual(self.status().state, "missing")
        self.assertEqual(tree(self.repo), before)
        self.refresh()
        before = tree(self.repo)
        self.assertEqual(self.status().state, "fresh")
        self.assertEqual(tree(self.repo), before)

    def test_the_app_run_and_the_source_run_share_one_map(self):
        git_commit(self.repo, BASE)
        (self.repo / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        source = impact.refresh(self.repo)
        self.assertEqual((source.ok, source.performed), (True, "built"))
        selected = self.status()
        self.assertEqual((selected.state, selected.store, selected.map["manifest"], selected.map["tool"]),
                         ("fresh", source.store, source.map["manifest"], source.map["tool"]))
        (self.repo / "scratch.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual(self.refresh().performed, "refreshed")
        self.assertEqual(mapstore.status(self.repo, env={})[0].action, "fresh")
        self.assertEqual(impact.status(self.repo / "src").state, "fresh", "source mode still accepts a subfolder")

    def test_the_environment_and_the_git_pin_are_put_back(self):
        git_commit(self.repo, BASE)
        os.environ["GIT_OAH_PROBE"] = "kept for the caller"
        seen = {}

        def capture(root, **options):
            seen.update(argv=paths.git_argv(), env=dict(os.environ), options=options)
            return None, "missing"

        pin, env = paths._git, dict(os.environ)
        with mock.patch.object(mapstore, "status", side_effect=capture):
            self.assertEqual(self.status().state, "missing")
        self.assertIs(paths._git, pin)
        self.assertEqual(dict(os.environ), env)
        self.assertNotIn("GIT_OAH_PROBE", seen["env"])
        self.assertEqual((seen["env"]["GIT_ALLOW_PROTOCOL"], seen["env"]["GIT_NO_LAZY_FETCH"],
                          seen["env"]["GIT_CEILING_DIRECTORIES"]), (":", "1", str(self.repo.parent)))
        for setting in ("core.fsmonitor=false", "log.showSignature=false", "protocol.allow=never",
                        f"core.hooksPath={os.devnull}"):
            self.assertIn(setting, seen["argv"])
        self.assertEqual(seen["options"]["env"], {})
        self.assertLessEqual(seen["options"]["settings"]["git_timeout"], impact.MAX_GIT_TIMEOUT)

    def test_an_outcome_is_bounded_json(self):
        git_commit(self.repo, BASE)
        document = self.refresh().to_json()
        self.assertEqual(sorted(document), sorted([
            "schema", "tool", "action", "ok", "state", "reason", "performed", "changed", "root", "branch", "head",
            "store", "map", "graph", "runner", "git", "error", "warnings"]))
        self.assertEqual((document["schema"], document["tool"], document["action"]), (1, "impact", "refresh"))
        self.assertEqual(document["runner"]["name"], "oah-project-tools")
        long = impact.failure("status", "/x", "OAH_PROJECT_TOOLS_IO", "m" * 9000).to_json()
        self.assertEqual((len(long["reason"]), len(long["error"]["message"])), (impact.TEXT_LIMIT, impact.TEXT_LIMIT))
        json.dumps(document)


class WorktreeTest(SelectedCase):
    def test_each_worktree_has_its_own_map_in_the_shared_store(self):
        git_commit(self.repo, BASE)
        linked = make_tmp_dir(self) / "linked"
        git(self.repo, "worktree", "add", "-q", str(linked), "-b", "side")
        main, side = self.refresh(), self.refresh(linked)
        self.assertEqual((main.performed, side.performed, side.branch), ("built", "built", "side"))
        maps = self.repo / ".git" / "oah" / "impact" / "maps"
        self.assertEqual({pathlib.Path(main.store["path"]).parent, pathlib.Path(side.store["path"]).parent}, {maps})
        self.assertNotEqual(main.store["path"], side.store["path"])
        (linked / "src" / "app.py").write_text("print('only here')\n", encoding="utf-8")
        self.assertEqual((self.status().state, self.status(linked).state), ("fresh", "stale"))

    def test_a_pointer_that_the_repository_does_not_confirm_is_refused(self):
        git_commit(self.repo, BASE)
        linked = make_tmp_dir(self) / "linked"
        git(self.repo, "worktree", "add", "-q", str(linked), "-b", "side")
        forged, separate = make_tmp_dir(self) / "forged", make_tmp_dir(self) / "separate"
        for folder, pointer in ((forged, (linked / ".git").read_text(encoding="utf-8")),
                                (separate, f"gitdir: {(self.repo / '.git').as_posix()}\n")):
            folder.mkdir()
            (folder / ".git").write_text(pointer, encoding="utf-8")
            self.refused(self.refresh(folder), "OAH_PROJECT_TOOLS_GIT_LAYOUT")
            self.refused(self.status(folder), "OAH_PROJECT_TOOLS_GIT_LAYOUT")
        self.assertFalse((self.repo / ".git" / "oah").exists(), "nothing was written into the other repository")

    def test_a_linked_root_or_git_folder_is_refused(self):
        git_commit(self.repo, BASE)
        alias = make_tmp_dir(self) / "alias"
        directory_alias(self.repo, alias)
        self.refused(self.status(alias), "OAH_PROJECT_TOOLS_ROOT_INVALID")
        moved = make_tmp_dir(self) / "moved-git"
        shutil.move(str(self.repo / ".git"), str(moved))
        directory_alias(moved, self.repo / ".git")
        self.refused(self.refresh(), "OAH_PROJECT_TOOLS_GIT_LAYOUT")
        self.assertFalse((moved / "oah").exists())

    def test_a_relative_or_missing_root_is_refused(self):
        self.refused(impact.status("repo", selected=True), "OAH_PROJECT_TOOLS_ROOT_INVALID")
        self.refused(self.status(self.repo / "absent"), "OAH_PROJECT_TOOLS_ROOT_INVALID")
        file = self.repo / "file.txt"
        file.write_text("x\n", encoding="utf-8")
        self.refused(self.status(file), "OAH_PROJECT_TOOLS_ROOT_INVALID")

    def test_a_folder_that_looks_bare_is_refused(self):
        git_commit(self.repo, {**BASE, "HEAD": "ref: refs/heads/main\n", "objects/keep": "x\n"})
        self.refused(self.refresh(), "OAH_PROJECT_TOOLS_GIT_LAYOUT")
        self.assertFalse((self.repo / ".git" / "oah").exists())

    def test_a_redirected_worktree_is_refused(self):
        git_commit(self.repo, BASE)
        elsewhere = make_tmp_dir(self)
        git(self.repo, "config", "core.worktree", elsewhere.as_posix())
        outcome = self.refresh()
        self.assertFalse(outcome.ok)
        self.assertIn(outcome.error["code"], ("OAH_PROJECT_TOOLS_ROOT_MISMATCH", "OAH_NOT_A_REPO"))
        self.assertFalse((self.repo / ".git" / "oah").exists())


class StoreTest(SelectedCase):
    def test_a_store_folder_reached_through_a_link_is_never_written(self):
        git_commit(self.repo, BASE)
        outside = make_tmp_dir(self)
        directory_alias(outside, self.repo / ".git" / "oah")
        self.refused(self.refresh(), "OAH_PROJECT_TOOLS_UNSAFE_STORE")
        self.refused(self.status(), "OAH_PROJECT_TOOLS_UNSAFE_STORE")
        self.assertEqual(sorted(outside.iterdir()), [])

    @unittest.skipUnless(POSIX, "a file symlink needs no privilege on POSIX")
    def test_a_map_file_that_is_a_symlink_is_not_read_or_replaced(self):
        git_commit(self.repo, BASE)
        store = pathlib.Path(self.refresh().store["path"])
        target = make_tmp_dir(self) / "elsewhere.json"
        target.write_bytes(store.read_bytes())
        store.unlink()
        store.symlink_to(target)
        recorded = target.read_bytes()
        self.refused(self.status(), "OAH_PROJECT_TOOLS_UNSAFE_STORE")
        self.refused(self.refresh(), "OAH_PROJECT_TOOLS_UNSAFE_STORE")
        self.assertEqual(target.read_bytes(), recorded)

    def test_an_absolute_state_override_is_honoured_and_a_relative_one_refused(self):
        git_commit(self.repo, BASE)
        state = make_tmp_dir(self) / "state"
        os.environ["OAH_STATE_DIR"] = str(state)
        built = self.refresh()
        self.assertEqual((built.performed, built.store["source"]), ("built", "OAH_STATE_DIR"))
        self.assertTrue(pathlib.Path(built.store["path"]).is_relative_to(state))
        self.assertFalse((self.repo / ".git" / "oah").exists())
        os.environ["OAH_STATE_DIR"] = "relative-state"
        self.refused(self.refresh(), "OAH_PROJECT_TOOLS_UNSAFE_STORE")
        self.assertFalse(pathlib.Path("relative-state").exists())


class HostileRepositoryTest(SelectedCase):
    """A repository whose own configuration tries to start a program. Each control proves the vector is live."""

    def setUp(self):
        super().setUp()
        git_commit(self.repo, BASE)
        self.marker = make_tmp_dir(self) / "ran"
        self.tool = make_tmp_dir(self) / "tool.sh"
        self.tool.write_text(f"#!/bin/sh\n: > '{self.marker.as_posix()}'\ncat\n", encoding="utf-8")
        self.tool.chmod(0o755)

    def control(self, *args: str) -> None:
        """Without the safe profile this git command starts the program (asserted where it is a /bin/sh script)."""
        if not POSIX:
            return
        git(self.repo, *args, check=False)
        self.assertTrue(self.marker.exists(), "the control did not fire, so the test would prove nothing")
        self.marker.unlink()

    def protected(self) -> impact.Outcome:
        outcome = self.refresh()
        self.assertFalse(self.marker.exists(), "the repository's program ran")
        self.assertEqual(self.status().ok, outcome.ok)
        self.assertFalse(self.marker.exists(), "the repository's program ran")
        return outcome

    def test_fsmonitor_program_is_not_started(self):
        git(self.repo, "config", "core.fsmonitor", self.tool.as_posix())
        self.control("status")
        self.assertTrue(self.protected().ok)

    def test_filter_program_is_not_started(self):
        (self.repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
        # Same size, newer time stamp: git has to read the file to tell whether it changed, and that read
        # is what runs a clean filter.
        os.utime(self.repo / "notes.txt", (2_000_000_000, 2_000_000_000))
        git(self.repo, "config", "filter.evil.clean", self.tool.as_posix())
        git(self.repo, "config", "filter.evil.required", "true")
        self.control("status")
        for name in ("smudge", "process"):
            git(self.repo, "config", f"filter.evil.{name}", self.tool.as_posix())
        outcome = self.protected()
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.map["dirty"], 0, "the untouched content reads as clean without the filter")

    def test_signature_program_is_not_started(self):
        commit = git(self.repo, "cat-file", "commit", "HEAD").stdout
        header, _, message = commit.partition("\n\n")
        signed = (header + "\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n fake\n -----END PGP SIGNATURE-----\n\n"
                  + message)
        new = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-t", "commit", "-w", "--stdin"],
                             input=signed, check=True, capture_output=True, text=True, encoding="utf-8",
                             env=git_env()).stdout.strip()
        git(self.repo, "update-ref", "HEAD", new)
        git(self.repo, "config", "log.showSignature", "true")
        git(self.repo, "config", "gpg.program", self.tool.as_posix())
        self.control("log", "--no-renames", "--name-only", "-z", "--format=%x1e%H", "-n5", "HEAD", "--")
        self.assertTrue(self.protected().ok)

    def test_a_promised_object_is_not_fetched(self):
        tree_id = git(self.repo, "rev-parse", "HEAD^{tree}").stdout.strip()
        (self.repo / ".git" / "objects" / tree_id[:2] / tree_id[2:]).unlink()
        for name, value in (("extensions.partialClone", "origin"), ("remote.origin.promisor", "true"),
                            ("remote.origin.url", f"ext::sh -c touch% {self.marker.as_posix()}"),
                            ("protocol.ext.allow", "always")):
            git(self.repo, "config", name, value)
        self.control("log", "--name-only", "-n1", "HEAD", "--")
        # The protocol allow-list alone stops it, also on a git too old for GIT_NO_LAZY_FETCH.
        subprocess.run(["git", "-C", str(self.repo), "log", "--name-only", "-n1", "HEAD", "--"], capture_output=True,
                       env={**git_env(), "GIT_ALLOW_PROTOCOL": impact.SAFE_GIT_ENV["GIT_ALLOW_PROTOCOL"]}, timeout=60)
        self.assertFalse(self.marker.exists(), "the protocol allow-list did not stop the fetch")
        outcome = self.protected()
        if not outcome.ok:  # the tree is really gone, so git may refuse; it must do so without fetching
            self.assertRegex(outcome.error["code"], r"\AOAH_IMPACT_GIT_")

    def test_hooks_are_not_started_by_the_pinned_git(self):
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "post-commit", "post-index-change", "reference-transaction"):
            shutil.copy(self.tool, hooks / name)
            (hooks / name).chmod(0o755)
        self.control("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "control")
        layout = impact._git_layout(self.repo)
        with impact._safe_git(self.repo, layout, None, impact._Facts("refresh", str(self.repo))) as argv:
            done = subprocess.run([*argv, "-C", str(self.repo), "-c", "commit.gpgsign=false", "commit", "-q",
                                   "--allow-empty", "-m", "protected"], capture_output=True, text=True,
                                  env={**os.environ, **{k: v for k, v in git_env().items()
                                                        if k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.marker.exists(), "a hook ran")
        self.assertTrue(self.protected().ok)

    def test_a_nested_repository_program_is_not_started(self):
        nested = self.repo / "vendor" / "lib"
        nested.mkdir(parents=True)
        git(nested, "init", "-q", "-b", "main")
        (nested / "notes.txt").write_text("nested\n", encoding="utf-8")
        git(nested, "add", "-A")
        git(nested, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "nested")
        git(self.repo, "add", "vendor/lib")  # recorded as a gitlink: a populated submodule
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "submodule")
        self.assertIn("160000", git(self.repo, "ls-files", "-s", "vendor/lib").stdout)
        # Only the nested repository names the programs; the overrides of the outer run must still cover it.
        (nested / ".gitattributes").write_text("*.txt filter=nested\n", encoding="utf-8")
        os.utime(nested / "notes.txt", (2_000_000_000, 2_000_000_000))
        git(nested, "config", "filter.nested.clean", self.tool.as_posix())
        git(nested, "config", "core.fsmonitor", self.tool.as_posix())
        self.control("status")  # plain git status asks every populated submodule for its own status
        self.assertTrue(self.protected().ok)

    def test_a_settings_file_that_is_not_a_plain_file_is_refused(self):
        folder = self.repo / ".oah"
        folder.mkdir()
        (folder / "config.toml").mkdir()
        self.refused(self.refresh(), "OAH_PROJECT_TOOLS_UNSAFE_CONFIG")
        (folder / "config.toml").rmdir()
        if hasattr(os, "mkfifo"):
            os.mkfifo(folder / "config.toml")  # opening it for reading would wait for a writer forever
            self.refused(self.refresh(), "OAH_PROJECT_TOOLS_UNSAFE_CONFIG")
            self.refused(self.status(), "OAH_PROJECT_TOOLS_UNSAFE_CONFIG")
        self.assertFalse((self.repo / ".git" / "oah").exists())

    def test_inherited_git_variables_do_not_reach_git(self):
        os.environ.update(GIT_DIR=str(make_tmp_dir(self) / "no-such-git-dir"),
                          GIT_CONFIG_PARAMETERS=f"'core.fsmonitor={self.tool.as_posix()}'",
                          GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.fsmonitor",
                          GIT_CONFIG_VALUE_0=self.tool.as_posix())
        self.assertTrue(self.protected().ok)

    def test_a_filter_driver_that_cannot_be_switched_off_is_refused(self):
        git(self.repo, "config", "filter.a=b.clean", self.tool.as_posix())
        self.refused(self.refresh(), "OAH_PROJECT_TOOLS_UNSAFE_CONFIG")
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.repo / ".git" / "oah").exists())

    def test_repository_settings_stay_inside_the_run_limits(self):
        config = self.repo / ".oah" / "config.toml"
        config.parent.mkdir()
        config.write_text("[impact]\ngit_timeout = 99999.0\nlock_timeout = 99999.0\n", encoding="utf-8")
        settings = impact._bounded_settings(self.repo)
        self.assertEqual((settings["git_timeout"], settings["lock_timeout"]),
                         (impact.MAX_GIT_TIMEOUT, impact.MAX_LOCK_TIMEOUT))
        self.assertEqual(mapstore.tool_digest(settings), mapstore.tool_digest(configured(self.repo)))
        config.write_text('[impact]\nledger_globs = ["../*/GATES.md"]\n', encoding="utf-8")
        self.refused(self.refresh(), "OAH_PROJECT_TOOLS_UNSAFE_CONFIG")
        config.unlink()
        outside = make_tmp_dir(self) / "config.toml"
        outside.write_text("[impact]\nmin_needle = 9\n", encoding="utf-8")
        if POSIX:
            config.symlink_to(outside)
            self.refused(self.refresh(), "OAH_PROJECT_TOOLS_UNSAFE_CONFIG")


class TrustedGitTest(SelectedCase):
    def setUp(self):
        super().setUp()
        git_commit(self.repo, BASE)
        self.real = paths.git_argv()[0]

    def test_an_explicit_git_is_used_and_reported(self):
        if not os.path.isabs(self.real):
            self.skipTest("git was not resolved to an absolute path here")
        outcome = self.refresh(git=self.real)
        self.assertEqual((outcome.ok, outcome.git["path"]), (True, self.real))
        self.assertRegex(outcome.git["version"], r"\A\d+\.\d+")

    def test_an_explicit_git_that_is_not_trustworthy_is_refused(self):
        inside = self.repo / "bin" / ("git.exe" if os.name == "nt" else "git")
        inside.parent.mkdir()
        shutil.copy(self.real, inside)
        candidates = ["git", str(self.repo / "absent" / "git"), str(inside)]
        if POSIX and os.path.isabs(self.real):  # a link in the project to a real git: the project can repoint it
            (self.repo / "bin" / "git-link").symlink_to(self.real)
            candidates.append(str(self.repo / "bin" / "git-link"))
        for candidate in candidates:
            self.refused(self.status(git=candidate), "OAH_PROJECT_TOOLS_GIT_UNTRUSTED")
        self.assertFalse((self.repo / ".git" / "oah").exists())

    @unittest.skipUnless(POSIX, "the stand-in git is a /bin/sh script")
    def test_path_lookup_skips_the_project_and_old_gits_are_refused(self):
        marker = make_tmp_dir(self) / "ran"
        planted = self.repo / "bin" / "git"
        planted.parent.mkdir()
        planted.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 1\n", encoding="utf-8")
        planted.chmod(0o755)
        os.environ["PATH"] = os.pathsep.join([str(planted.parent), ".", os.environ["PATH"]])
        outcome = self.refresh()
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertFalse(pathlib.Path(outcome.git["path"]).is_relative_to(self.repo))
        self.assertFalse(marker.exists(), "a git inside the project was started")
        old = make_tmp_dir(self) / "git"
        old.write_text("#!/bin/sh\necho 'git version 2.30.9'\n", encoding="utf-8")
        old.chmod(0o755)
        self.refused(self.status(git=str(old)), "OAH_PROJECT_TOOLS_GIT_UNSUPPORTED")
        os.environ["PATH"] = str(make_tmp_dir(self))
        self.refused(self.status(), "OAH_GIT_MISSING")


class GraphTest(SelectedCase):
    def setUp(self):
        super().setUp()
        self.head = git_commit(self.repo, BASE)
        self.settings = configured(self.repo)

    def write(self, **top: object) -> None:
        document = {"nodes": [{"id": "app", "source_file": "src/app.py"}, {"id": "util", "source_file": "src/lib/util.py"},
                              {"id": "away", "source_file": "../outside.py"}, {"id": "root", "source_file": "/etc/hosts"}],
                    "links": [{"source": "app", "target": "util", "relation": "imports"}], **top}
        path = self.repo / "graphify-out" / "graph.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")

    def attachment(self) -> impact.GraphAttachment:
        return impact.graph_attachment(self.repo, self.settings, self.head)

    def test_states(self):
        self.assertEqual(self.attachment().state, "absent")
        self.write(built_at_commit=self.head)
        found = self.attachment()
        self.assertEqual((found.state, found.commit, found.nodes, found.edges), ("available", self.head, 2, 1))
        self.assertEqual(sorted(found.graph["files"].values()), ["src/app.py", "src/lib/util.py"])
        self.write(built_at_commit="b" * 40)
        self.assertEqual((self.attachment().state, self.attachment().commit, self.attachment().graph),
                         ("stale", "b" * 40, None))
        for unproven in ({}, {"built_at_commit": self.head[:12]}):
            self.write(**unproven)
            self.assertEqual((self.attachment().state, self.attachment().graph), ("unproven", None))
        (self.repo / "graphify-out" / "graph.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.attachment().state, "unproven")
        self.write(built_at_commit=self.head)
        self.assertEqual(impact.graph_attachment(self.repo, self.settings, None).state, "unproven")

    def test_only_a_plain_file_inside_the_project_is_read(self):
        self.write(built_at_commit=self.head)
        outside = make_tmp_dir(self) / "graph.json"
        shutil.copy(self.repo / "graphify-out" / "graph.json", outside)
        for name in ("../graph.json", outside.as_posix(), "graphify-out\\graph.json"):
            found = impact.graph_attachment(self.repo, {**self.settings, "graph_file": name}, self.head)
            self.assertEqual((found.state, found.graph), ("unproven", None), name)
        shutil.rmtree(self.repo / "graphify-out")
        directory_alias(outside.parent, self.repo / "graphify-out")
        self.assertEqual((self.attachment().state, self.attachment().graph), ("unproven", None))
        self.assertEqual(impact.graph_attachment(self.repo, self.settings, self.head, strict=False).state, "available")

    def test_status_reports_the_attachment_and_neighbours_stay_suggestions(self):
        self.write(built_at_commit=self.head)
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "graph")
        self.assertEqual(self.refresh().graph.state, "stale", "the graph was built one commit earlier")
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.write(built_at_commit=head)
        outcome = self.refresh()
        self.assertEqual((outcome.graph.state, outcome.to_json()["graph"]["commit"]), ("available", head))
        data = mapstore.load(pathlib.Path(outcome.store["path"]))["data"]
        with_graph = query.query(data, ["src/lib/util.py"], graph=outcome.graph.graph, settings=self.settings)
        without = query.query(data, ["src/lib/util.py"], graph=None, settings=self.settings)
        added = [row for row in with_graph if row not in without]
        self.assertEqual([(row.binding, row.layer, row.source) for row in added], [("mention", "graph", "src/app.py")])
        strong = [row for row in without if row.binding in query.STRONG]
        self.assertEqual([row for row in with_graph if row.binding in query.STRONG], strong)
        self.assertEqual(with_graph[:len(strong)], strong, "pin and exec rows keep their place")

    def test_the_loader_keeps_its_three_values_and_reads_graphify_commit_binding(self):
        self.write(built_at_commit=self.head)
        graph, verdict, reason = query.read_graph(self.repo, self.settings, self.head)
        self.assertEqual((verdict, sorted(graph)), ("analysed", ["edges", "files"]))
        self.assertIn(self.head[:12], reason)
        self.assertEqual(query.graph_identity(self.repo, self.settings, "c" * 40)[1:4:2], ("stale", self.head))
        self.write(commit="d" * 40, built_at_commit=self.head)
        self.assertEqual(query.graph_identity(self.repo, self.settings, self.head)[3], "d" * 40)


class ToolIdentityTest(unittest.TestCase):
    def test_line_endings_do_not_change_the_generator_identity(self):
        settings = configured(make_tmp_dir(self))
        expected = mapstore.tool_digest(settings)
        package = pathlib.Path(mapstore.__file__).parent
        converted = make_tmp_dir(self) / "impact"
        converted.mkdir()
        for source in package.glob("*.py"):
            (converted / source.name).write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        with mock.patch.object(mapstore, "__file__", str(converted / "mapstore.py")):
            self.assertEqual(mapstore.tool_digest(settings), expected)
            with (converted / "kinds.py").open("ab") as handle:
                handle.write(b"# changed\r\n")
            self.assertNotEqual(mapstore.tool_digest(settings), expected)


if __name__ == "__main__":
    unittest.main()
