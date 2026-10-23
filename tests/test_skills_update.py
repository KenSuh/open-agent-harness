# 스킬 갱신 검사 — 로컬 가짜 업스트림으로 핀 올리기, 라이선스·적재·수상한 파일·개인정보 실패 시 되돌림, 리스 대기, 주기를 확인한다.
from __future__ import annotations

import datetime as dt
import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from oah.lease import Store
from oah.paths import repo_info
from oah.toolkit.catalog import Catalog, load_catalog, read_upstream
from oah.toolkit.fetch import fetch, stage_item
from oah.toolkit.install import InstallError, apply_install, destination_locks, plan_install, swap_in
from oah.toolkit.update import (DEFAULTS, LeasesActive, active_leases, cache_dir, idle_state,
                                installed_destinations, read_state, refresh_cache, run_update, scheduled_update,
                                vendored_destinations)
from tests.fixtures.toolkit.upstream import (APACHE_TEXT, MIT_TEXT, TODAY, bundled, commit, make_executable,
                                             make_upstream, skill_text, write_catalog)
from tests.helpers import directory_alias, make_git_repo, make_tmp_dir

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)


def snapshot(folder: pathlib.Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*")) if path.is_file()}


class UpdateFixture(unittest.TestCase):
    def setUp(self):
        self.upstream = make_upstream(self)
        work = make_tmp_dir(self)
        self.kit = work / "kit" / "skills"
        tree = fetch(str(self.upstream), "main", work / "scratch", paths=("skills/demo", "LICENSE"))
        stage_item(bundled(self.upstream), tree, self.kit / "demo", fetched=TODAY)
        self.first = tree.commit
        self.catalog_path = write_catalog(work, [bundled(self.upstream, commit_id=self.first)])
        self.catalog: Catalog = load_catalog(self.catalog_path)
        self.project = make_git_repo(self)
        self.home = work / "home"
        self.state = work / "state"
        plan = plan_install(["demo"], catalog=self.catalog, source_dirs=[self.kit], target="claude", level="project",
                            project_root=self.project, home=self.home)
        apply_install(plan, catalog=self.catalog, now=NOW)
        self.installed = self.project / ".claude" / "skills" / "demo"

    def destinations(self):
        return installed_destinations(self.catalog, project_root=self.project, home=self.home, level="project")

    def update(self, check_only=False, destinations=None):
        return run_update(catalog=self.catalog, destinations=destinations or self.destinations(),
                          check_only=check_only, settings=DEFAULTS, now=NOW, gitleaks=None)

    def assert_rolled_back(self, before: dict[str, bytes], fragment: str):
        results = self.update()
        self.assertEqual([r.status for r in results], ["rolled-back"], [r.describe() for r in results])
        self.assertTrue(any(fragment in reason for reason in results[0].reasons), results[0].reasons)
        self.assertEqual(snapshot(self.installed), before)
        self.assertEqual(json.loads((self.installed / ".oah-skill.json").read_text(encoding="utf-8"))["commit"],
                         self.first)


class UpdateTest(UpdateFixture):
    def test_generic_secret_assignment_blocks_without_gitleaks(self):
        before = snapshot(self.installed)
        assignment = "SERVICE_" + "PASSWORD=" + "aB3d" * 4 + "\n"
        commit(self.upstream, {"skills/demo/ref.md": assignment})
        self.assert_rolled_back(before, "env-secret")
        results = refresh_cache(catalog=self.catalog, state_dir=self.state, bundle_dir=self.kit,
                                settings=DEFAULTS, now=NOW, gitleaks=None)
        self.assertEqual([r.status for r in results], ["rolled-back"])
        self.assertFalse((cache_dir(self.state) / "demo").exists())

    def test_update_refuses_collection_and_null_metadata(self):
        before = snapshot(self.installed)
        for fields in ("name: []\ndescription: ok", "name: demo\ndescription: null",
                       "name: demo\ndescription: {a: b}", "name: demo\ndescription:\n  nested: value"):
            with self.subTest(fields=fields):
                commit(self.upstream, {"skills/demo/SKILL.md": f"---\n{fields}\n---\n"})
                self.assert_rolled_back(before, "OAH_SKILL_INVALID" if "{" in fields or "[]" in fields else "description")

    def test_support_executables_are_recorded_and_new_ones_refused(self):
        entry = dataclasses.replace(bundled(self.upstream, name="humanize-korean", subpath="",
                                            skills=("skills/demo",)),
                                    support={"skills/demo": ("scripts",)})
        commit(self.upstream, {"scripts/existing.sh": "echo old\n"})
        make_executable(self.upstream, "scripts/existing.sh")
        work = make_tmp_dir(self)
        tree = fetch(str(self.upstream), "main", work / "fetch")
        stage_item(entry, tree, work / "kit" / entry.name, fetched=TODAY)
        catalog = Catalog((dataclasses.replace(entry, commit=tree.commit),), ())
        plan = plan_install([entry.name], catalog=catalog, source_dirs=[work / "kit"], target="codex",
                            level="project", project_root=self.project, home=self.home)
        apply_install(plan, catalog=catalog, now=NOW)
        global_plan = plan_install([entry.name], catalog=catalog, source_dirs=[work / "kit"], target="codex",
                                   level="user", project_root=self.project, home=self.home)
        apply_install(global_plan, catalog=catalog, now=NOW)
        folder = self.project / ".agents" / "skills" / "demo"
        marker = json.loads((folder / ".oah-skill.json").read_text(encoding="utf-8"))
        self.assertIn("scripts/existing.sh", marker["executables"])
        before = snapshot(folder)
        commit(self.upstream, {"scripts/new-tool.sh": "echo new\n"})
        make_executable(self.upstream, "scripts/new-tool.sh")
        destinations = installed_destinations(catalog, project_root=self.project, home=self.home, level="project")
        destinations += installed_destinations(catalog, project_root=self.project, home=self.home, level="global")
        results = run_update(catalog=catalog, destinations=destinations, check_only=False, settings=DEFAULTS,
                             now=NOW, gitleaks=None)
        self.assertEqual([r.status for r in results], ["rolled-back", "rolled-back"])
        self.assertIn("new executable file scripts/new-tool.sh", results[0].reasons)
        self.assertEqual(snapshot(folder), before)

    def test_update_reports_current(self):
        self.assertEqual([r.status for r in self.update()], ["current"])

    def test_update_advances_pin(self):
        new = commit(self.upstream, {"skills/demo/ref.md": "reference\nmore\n"})
        results = self.update()
        self.assertEqual([r.status for r in results], ["advanced"], [r.describe() for r in results])
        self.assertEqual((results[0].old_commit, results[0].new_commit), (self.first, new))
        self.assertEqual((results[0].diff.files_changed, results[0].diff.added, results[0].diff.removed), (1, 1, 0))
        marker = json.loads((self.installed / ".oah-skill.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["commit"], new)
        self.assertEqual((self.installed / "ref.md").read_text(encoding="utf-8"), "reference\nmore\n")
        self.assertEqual((self.installed / "LICENSE").read_text(encoding="utf-8"), MIT_TEXT)
        locks = set(self.installed.parent.glob(".oah-publish-*.lock"))
        self.assertEqual(len(locks), 1)
        self.assertEqual(set(self.installed.parent.iterdir()), {self.installed, *locks})

    def test_check_only_writes_nothing(self):
        commit(self.upstream, {"skills/demo/ref.md": "changed\n"})
        before = snapshot(self.installed)
        results = self.update(check_only=True)
        self.assertEqual([r.status for r in results], ["available"])
        self.assertEqual(snapshot(self.installed), before)
        self.assertFalse(self.state.exists())

    def test_update_rolls_back_on_license_change(self):
        before = snapshot(self.installed)
        commit(self.upstream, {"LICENSE": APACHE_TEXT})
        self.assert_rolled_back(before, "license changed")

    def test_update_rolls_back_on_license_text_change(self):
        before = snapshot(self.installed)
        commit(self.upstream, {"LICENSE": MIT_TEXT.replace("2026", "2027")})
        self.assert_rolled_back(before, "license text changed")

    def test_update_rolls_back_on_failed_load_check(self):
        before = snapshot(self.installed)
        commit(self.upstream, {"skills/demo/SKILL.md": "---\nname: demo\n---\nno description\n"})
        self.assert_rolled_back(before, "description")
        commit(self.upstream, {"skills/demo/SKILL.md": skill_text("demo", body="See [x](gone.md).\n")})
        self.assert_rolled_back(before, "gone.md")

    def test_update_rolls_back_on_suspicious_file(self):
        before = snapshot(self.installed)
        commit(self.upstream, {"skills/demo/tool.bin": b"\0\x7fELF"})
        self.assert_rolled_back(before, "binary")
        commit(self.upstream, {"skills/demo/tool.bin": None, "skills/demo/run.sh": "echo hi\n"})
        make_executable(self.upstream, "skills/demo/run.sh")
        self.assert_rolled_back(before, "new executable file run.sh")
        commit(self.upstream, {"skills/demo/run.sh": None, "skills/demo/big.md": "x" * 400000 + "\n"})
        self.assert_rolled_back(before, "size grew")

    def test_update_rolls_back_on_privacy_hit(self):
        before = snapshot(self.installed)
        fake_key = "gh" + "p_" + "Z9y8X7w6" * 5
        commit(self.upstream, {"skills/demo/ref.md": f"token {fake_key}\n"})
        self.assert_rolled_back(before, "API key")

    def test_update_reports_fetch_failure(self):
        from tests.fixtures.toolkit.upstream import remove_tree

        remove_tree(self.upstream)
        results = self.update()
        self.assertEqual([r.status for r in results], ["failed"])
        self.assertIn("OAH_SKILLS_FETCH_FAILED", results[0].reasons[0])

    def test_vendored_update_advances_upstream_json_and_catalog(self):
        new = commit(self.upstream, {"skills/demo/ref.md": "v2\n"})
        destinations = vendored_destinations(self.catalog, self.kit)
        results = run_update(catalog=self.catalog, destinations=destinations, check_only=False, settings=DEFAULTS,
                             now=NOW, gitleaks=None, catalog_path=self.catalog_path)
        self.assertEqual([r.status for r in results], ["advanced"], [r.describe() for r in results])
        self.assertEqual(read_upstream(self.kit / "demo").commit, new)
        self.assertEqual(load_catalog(self.catalog_path).bundled[0].commit, new)


class SwapTest(unittest.TestCase):
    def test_cleanup_retries_a_read_only_file(self):
        import stat
        from tests.fixtures.toolkit.upstream import remove_readonly

        path = make_tmp_dir(self) / "object"
        path.write_bytes(b"object")
        path.chmod(stat.S_IREAD)
        calls = []

        def remove(name):
            calls.append(name)
            self.assertTrue(path.stat().st_mode & stat.S_IWRITE)
            path.unlink()

        remove_readonly(remove, str(path), (PermissionError, PermissionError("read-only"), None))
        self.assertEqual(calls, [str(path)])
        self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Git object files have the read-only attribute")
    def test_cleanup_removes_read_only_git_objects_on_windows(self):
        from tests.fixtures.toolkit.upstream import remove_tree

        repo = make_upstream(self)
        self.assertTrue(list((repo / ".git" / "objects").rglob("*")))
        remove_tree(repo)
        self.assertFalse(repo.exists())

    def test_failed_swap_restores_backup(self):
        root = make_tmp_dir(self)
        dest, new = root / "dest", root / "new"
        dest.mkdir()
        (dest / "a.md").write_text("old\n", encoding="utf-8")
        new.mkdir()
        (new / "a.md").write_text("new\n", encoding="utf-8")
        with self.assertRaises(InstallError):
            swap_in(new, dest, verify=lambda folder: ["it does not load"])
        self.assertEqual((dest / "a.md").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["dest"])


class DestinationLocksTest(unittest.TestCase):
    def test_lock_stays_in_real_parent_and_folds_destination_name(self):
        root = make_tmp_dir(self)
        parent = root / "skills"
        with destination_locks([parent / "Caf\u00e9"]):
            locks = list(parent.glob(".oah-publish-*.lock"))
            self.assertEqual(len(locks), 1, "publication locks must live beside the destination")
        with destination_locks([parent / "CAFE\u0301"]):
            self.assertEqual(list(parent.glob(".oah-publish-*.lock")), locks)
        self.assertTrue(locks[0].is_file(), "keep the lock entry after releasing it")
        self.assertFalse((root / ".oah").exists())


class ScheduledTest(UpdateFixture):
    def test_global_publication_is_serialized_across_repository_processes(self):
        self._concurrent_global_updates(self.home)

    def test_global_publication_unifies_case_aliases(self):
        self.home.mkdir()
        alias = self.home.with_name("HOME")
        if not alias.exists() or not alias.samefile(self.home):
            self.skipTest("requires a case-insensitive filesystem")
        self._concurrent_global_updates(alias)

    def test_global_publication_unifies_unicode_aliases(self):
        self.home = self.home.with_name("caf\u00e9")
        self.home.mkdir()
        alias = self.home.with_name("cafe\u0301")
        if not alias.exists() or not alias.samefile(self.home):
            self.skipTest("requires a normalization-insensitive filesystem")
        self._concurrent_global_updates(alias)

    @unittest.skipUnless(os.name == "nt", "Windows case aliases")
    def test_global_publication_unifies_windows_home_case_aliases(self):
        self.test_global_publication_unifies_case_aliases()

    def test_global_publication_unifies_directory_aliases(self):
        self.home.mkdir()
        alias = self.home.with_name("home-alias")
        directory_alias(self.home, alias)
        self._concurrent_global_updates(alias)

    def test_global_publication_unifies_mount_aliases_with_different_device_ids(self):
        self.home.mkdir()
        alias = self.home.with_name("other-mount")
        directory_alias(self.home, alias)
        self.assertTrue(alias.samefile(self.home))
        self._concurrent_global_updates(alias, different_device=True)

    def _concurrent_global_updates(self, alias_home, *, different_device=False):
        plan = plan_install(["demo"], catalog=self.catalog, source_dirs=[self.kit], target="claude", level="user",
                            project_root=self.project, home=self.home)
        apply_install(plan, catalog=self.catalog, now=NOW)
        older = commit(self.upstream, {"skills/demo/ref.md": "older candidate\n"})
        newer = commit(self.upstream, {"skills/demo/ref.md": "newer candidate\n"})
        other_project = make_git_repo(self)
        sync = make_tmp_dir(self)
        # Both real processes prepare from the same installed pin. Pause the first after its
        # publication identity read; the second may enter publication only after that point.
        script = '''
import contextlib, datetime as dt, json, os, pathlib, sys, time
import oah.toolkit.update as update
from oah.toolkit.catalog import load_catalog
from oah.toolkit.fetch import fetch
catalog_path, project, home, state, sync, name, candidate, different_device = sys.argv[1:]
project, home, state, sync = map(pathlib.Path, (project, home, state, sync))
catalog = load_catalog(pathlib.Path(catalog_path))
destinations = update.installed_destinations(catalog, project_root=project, home=home, level="global")
# A portable mount simulation: both paths reach the same real files, but the second
# client's stat reports a different device ID for their parent, as separate mounts can.
parent = destinations[0].path.parent.resolve()
original_stat = pathlib.Path.stat
def stat(path, *args, **kwargs):
    info = original_stat(path, *args, **kwargs)
    if different_device == "True" and name == "older" and path == parent:
        fields = list(info)
        fields[2] += 1
        return os.stat_result(fields)
    return info
pathlib.Path.stat = stat
(sync / (name + "-device")).write_text(str(parent.stat().st_dev), encoding="utf-8")
publishing = False
def wait_for(path):
    deadline = time.monotonic() + 20
    while not path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("publication synchronization timed out")
        time.sleep(.01)
@contextlib.contextmanager
def guard():
    global publishing
    with update.idle_state(state):
        (sync / (name + "-ready")).touch()
        wait_for(sync / (name + "-go"))
        publishing = True
        yield
original = update._identity
def identity(path):
    result = original(path)
    if publishing and path == destinations[0].path:
        (sync / (name + "-checked")).touch()
        if name == "newer":
            wait_for(sync / "release")
    return result
update._identity = identity
def pinned_fetch(repository, ref, scratch, **kwargs):
    return fetch(repository, candidate, scratch, **kwargs)
results = update.run_update(catalog=catalog, destinations=destinations, check_only=False,
    settings=update.DEFAULTS, now=dt.datetime.now(dt.timezone.utc), gitleaks=None,
    fetcher=pinned_fetch, guard=guard)
print(json.dumps([r.status for r in results]))
'''
        processes = []
        env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parents[1] / "src"))
        for name, candidate, project in (("newer", newer, self.project), ("older", older, other_project)):
            home = self.home if name == "newer" else alias_home
            process = subprocess.Popen([sys.executable, "-c", script, str(self.catalog_path), str(project),
                                        str(home), str(sync / (name + "-state")), str(sync), name, candidate,
                                        str(different_device)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            processes.append(process)
            self.addCleanup(lambda p=process: (p.kill(), p.communicate()) if p.poll() is None else None)

        def wait_for(name, timeout=20):
            deadline = time.monotonic() + timeout
            while not (sync / name).exists() and time.monotonic() < deadline:
                time.sleep(.01)
            return (sync / name).exists()

        try:
            self.assertTrue(wait_for("older-ready"))
            self.assertTrue(wait_for("newer-ready"))
            if different_device:
                self.assertNotEqual((sync / "older-device").read_text(encoding="utf-8"),
                                    (sync / "newer-device").read_text(encoding="utf-8"))
            (sync / "newer-go").touch()
            self.assertTrue(wait_for("newer-checked"))
            (sync / "older-go").touch()
            # With the shared lock, the second identity read waits until release. Without it,
            # both candidates pass validation against the same installed pin.
            if wait_for("older-checked", timeout=1):
                processes[1].wait(timeout=20)
        finally:
            (sync / "release").touch()
        statuses = []
        for process in processes:
            out, err = process.communicate(timeout=25)
            self.assertEqual(process.returncode, 0, err)
            statuses += json.loads(out)
        self.assertCountEqual(statuses, ["advanced", "deferred"])
        installed = self.home / ".claude" / "skills" / "demo"
        marker = json.loads((installed / ".oah-skill.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["commit"], newer)
        self.assertEqual((installed / "ref.md").read_text(encoding="utf-8"), "newer candidate\n")
        locks = list(installed.parent.glob(".oah-publish-*.lock"))
        self.assertEqual(len(locks), 1, "the destination lock belongs beside the destination")

    def test_install_and_update_share_global_publication_lock(self):
        self._install_during_update("global")

    def test_install_and_update_share_project_publication_lock(self):
        self._install_during_update("project")

    @unittest.skipIf(os.name == "nt", "POSIX directory symlink; Windows junction tested separately")
    def test_install_started_during_symlink_publication_gap_waits(self):
        self._install_during_update("project", linked=True)

    @unittest.skipUnless(os.name == "nt", "Windows directory junction")
    def test_install_started_during_junction_publication_gap_waits(self):
        self._install_during_update("project", linked=True)

    def _install_during_update(self, level, *, linked=False):
        if level == "global":
            plan = plan_install(["demo"], catalog=self.catalog, source_dirs=[self.kit], target="claude", level="user",
                                project_root=self.project, home=self.home)
            apply_install(plan, catalog=self.catalog, now=NOW)
        newer = commit(self.upstream, {"skills/demo/ref.md": "newer candidate\n"})
        sync = make_tmp_dir(self)
        if linked:
            target = self.project / "linked-pin"
            self.installed.rename(target)
            directory_alias(target, self.installed)
        elif level == "project":
            # The install prepares a missing project copy; another writer seeds the pin
            # before the update starts, so the install must later lose its absence CAS.
            self.installed.rename(sync / "project-pin")
        script = '''
import contextlib, datetime as dt, json, pathlib, sys, time
import oah.toolkit.install as install
import oah.toolkit.update as update
from oah.toolkit.catalog import load_catalog
catalog_path, kit, project, home, sync, level, operation = sys.argv[1:]
kit, project, home, sync = map(pathlib.Path, (kit, project, home, sync))
catalog = load_catalog(pathlib.Path(catalog_path))
now = dt.datetime.now(dt.timezone.utc)
def wait_for(name):
    deadline = time.monotonic() + 20
    while not (sync / name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("publication synchronization timed out: " + name)
        time.sleep(.01)
if operation == "install":
    (sync / "install-started").touch()
    plan = install.plan_install(["demo"], catalog=catalog, source_dirs=[kit], target="claude",
        level="user" if level == "global" else "project", project_root=project, home=home)
    original_build = install.build_copy
    def build(*args, **kwargs):
        result = original_build(*args, **kwargs)
        (sync / "install-prepared").touch()
        wait_for("install-go")
        return result
    install.build_copy = build
    original_swap = install.swap_in
    def swap(*args, **kwargs):
        (sync / "install-publishing").touch()
        return original_swap(*args, **kwargs)
    install.swap_in = swap
    try:
        install.apply_install(plan, catalog=catalog, now=now)
        print(json.dumps(["installed"]))
    except install.InstallError as exc:
        print(json.dumps([exc.code, str(exc)]))
else:
    destinations = update.installed_destinations(catalog, project_root=project, home=home, level=level)
    original_move = install.shutil.move
    def move(src, dest, *args, **kwargs):
        # swap_in has already renamed the old destination to its backup.
        (sync / "update-gap").touch()
        wait_for("release")
        return original_move(src, dest, *args, **kwargs)
    install.shutil.move = move
    results = update.run_update(catalog=catalog, destinations=destinations, check_only=False,
        settings=update.DEFAULTS, now=now, gitleaks=None)
    print(json.dumps([r.status for r in results]))
'''
        env = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parents[1] / "src"))
        processes = []

        def start(operation):
            process = subprocess.Popen([sys.executable, "-c", script, str(self.catalog_path), str(self.kit),
                                        str(self.project), str(self.home), str(sync), level, operation],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            processes.append(process)
            self.addCleanup(lambda p=process: (p.kill(), p.communicate()) if p.poll() is None else None)
            return process

        def wait_for(name, timeout=20):
            deadline = time.monotonic() + timeout
            while not (sync / name).exists() and time.monotonic() < deadline:
                time.sleep(.01)
            return (sync / name).exists()

        try:
            if linked:
                start("update")
                self.assertTrue(wait_for("update-gap"))
                installer = start("install")
                self.assertTrue(wait_for("install-started"))
            else:
                installer = start("install")
                self.assertTrue(wait_for("install-prepared"))
                if level == "project":
                    (sync / "project-pin").rename(self.installed)
                start("update")
                self.assertTrue(wait_for("update-gap"))
            (sync / "install-go").touch()
            entered_gap = wait_for("install-publishing", timeout=1)
            finished_in_gap = installer.poll() is not None
            prepared_in_gap = linked and (sync / "install-prepared").exists()
        finally:
            (sync / "release").touch()
            (sync / "install-go").touch()
        outputs = []
        for process in processes:
            out, err = process.communicate(timeout=25)
            self.assertEqual(process.returncode, 0, err)
            outputs.append(json.loads(out))
        if linked:
            outputs.reverse()  # The updater starts first in the linked-destination scenario.
        self.assertFalse(prepared_in_gap, "install must lock before reading a linked slot's absence")
        self.assertFalse(entered_gap, "install must wait through the update's rename gap")
        self.assertFalse(finished_in_gap, "install must revalidate after the update releases its lock")
        self.assertEqual(outputs[0][0], "OAH_SKILLS_INSTALL_CONFLICT")
        self.assertIn("oah skills update --project" if linked else "again", outputs[0][1])
        self.assertEqual(outputs[1], ["advanced"])
        installed = (self.home if level == "global" else self.project) / ".claude" / "skills" / "demo"
        marker = json.loads((installed / ".oah-skill.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["commit"], newer)
        self.assertEqual((installed / "ref.md").read_text(encoding="utf-8"), "newer candidate\n")
        self.assertEqual(sorted(p.name for p in installed.iterdir()),
                         [".oah-skill.json", "LICENSE", "SKILL.md", "ref.md"])

    def test_install_revalidates_absence_after_staging(self):
        import oah.toolkit.install as install

        plan = plan_install(["demo"], catalog=self.catalog, source_dirs=[self.kit], target="codex", level="project",
                            project_root=self.project, home=self.home)
        dest = plan.copies[0].dest
        original = install.build_copy

        def stage_then_create(*args, **kwargs):
            result = original(*args, **kwargs)
            dest.mkdir(parents=True)
            (dest / "mine.md").write_text("preserve me\n", encoding="utf-8")
            return result

        with mock.patch.object(install, "build_copy", stage_then_create):
            with self.assertRaises(InstallError) as caught:
                apply_install(plan, catalog=self.catalog, now=NOW)
        self.assertEqual(caught.exception.code, "OAH_SKILLS_INSTALL_CONFLICT")
        self.assertEqual(snapshot(dest), {"mine.md": b"preserve me\n"})

    def test_install_revalidates_changes_during_fetch(self):
        import oah.toolkit.install as install

        plan = plan_install(["demo"], catalog=self.catalog, source_dirs=[self.kit], target="claude", level="user",
                            project_root=self.project, home=self.home)
        apply_install(plan, catalog=self.catalog, now=NOW)
        dest = plan.copies[0].dest
        plan = plan_install(["demo"], catalog=self.catalog, source_dirs=[], target="claude", level="user",
                            project_root=self.project, home=self.home)
        original = install.fetch_item

        def fetch_then_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            (dest / "ref.md").write_text("concurrent edit\n", encoding="utf-8")
            return result

        with mock.patch.object(install, "fetch_item", fetch_then_edit):
            with self.assertRaises(InstallError):
                apply_install(plan, catalog=self.catalog, now=NOW)
        self.assertEqual((dest / "ref.md").read_text(encoding="utf-8"), "concurrent edit\n")

    def test_destination_metadata_changed_before_preparation_is_deferred(self):
        destinations = self.destinations()
        commit(self.upstream, {"skills/demo/ref.md": "new\n"})
        self.update()
        before = snapshot(self.installed)
        results = run_update(catalog=self.catalog, destinations=destinations, check_only=False,
                             settings=DEFAULTS, now=NOW, gitleaks=None)
        self.assertEqual([r.status for r in results], ["deferred"])
        self.assertEqual(snapshot(self.installed), before)

    def test_cache_baseline_changed_during_preparation_is_deferred(self):
        commit(self.upstream, {"skills/demo/ref.md": "candidate\n"})

        def fetch_then_edit(*args, **kwargs):
            tree = fetch(*args, **kwargs)
            (self.kit / "demo" / "ref.md").write_text("new baseline\n", encoding="utf-8")
            return tree

        state, results = self.scheduled(NOW, fetcher=fetch_then_edit)
        self.assertEqual([r.status for r in results], ["deferred"])
        self.assertFalse((cache_dir(self.state) / "demo").exists())
        self.assertEqual(read_state(self.state), {})

    def test_concurrent_scheduled_checks_publish_only_once(self):
        first_candidate = commit(self.upstream, {"skills/demo/ref.md": "older candidate\n"})
        prepared, resume = threading.Event(), threading.Event()
        outcome = []
        errors = []

        def slow_fetch(*args, **kwargs):
            tree = fetch(*args, **kwargs)
            prepared.set()
            if not resume.wait(20):
                raise RuntimeError("test publication did not resume")
            return tree

        def slow_check():
            try:
                outcome.append(self.scheduled(NOW, fetcher=slow_fetch))
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=slow_check)
        worker.start()
        try:
            self.assertTrue(prepared.wait(20))
            new = commit(self.upstream, {"skills/demo/ref.md": "newer candidate\n"})
            self.assertNotEqual(first_candidate, new)
            self.assertEqual(self.scheduled(NOW)[0], "ran")
        finally:
            resume.set()
            worker.join(25)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcome[0][0], "not-due")
        self.assertEqual(self.cached(), new)
        log = (self.state / "toolkit" / "updates.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(log), 1)

    def test_publication_discards_a_changed_destination_or_baseline(self):
        import contextlib

        commit(self.upstream, {"skills/demo/ref.md": "candidate\n"})
        before = snapshot(self.installed)

        @contextlib.contextmanager
        def change_before_publication():
            (self.installed / "ref.md").write_text("concurrent edit\n", encoding="utf-8")
            yield

        results = run_update(catalog=self.catalog, destinations=self.destinations(), check_only=False,
                             settings=DEFAULTS, now=NOW, gitleaks=None, guard=change_before_publication)
        self.assertEqual([r.status for r in results], ["deferred"])
        self.assertIn("changed", results[0].reasons[0])
        self.assertEqual((self.installed / "ref.md").read_text(encoding="utf-8"), "concurrent edit\n")
        self.assertEqual((self.installed / ".oah-skill.json").read_bytes(), before[".oah-skill.json"])

    def scheduled(self, now, **overrides):
        options = dict(catalog=self.catalog, state_dir=self.state, bundle_dir=self.kit, settings=DEFAULTS, now=now)
        options.update(overrides)
        return scheduled_update(**options)

    def cached(self) -> str:
        return read_upstream(cache_dir(self.state) / "demo").commit

    def test_automatic_update_writes_only_the_kit_cache(self):
        new = commit(self.upstream, {"skills/demo/ref.md": "reference\nmore\n"})
        project_before = snapshot(self.installed)
        kit_before = snapshot(self.kit)
        state, results = self.scheduled(NOW)
        self.assertEqual(state, "ran")
        self.assertEqual([r.status for r in results], ["advanced"], [r.describe() for r in results])
        self.assertEqual(self.cached(), new)
        self.assertEqual(snapshot(self.installed), project_before, "tracked project copies are pins")
        self.assertEqual(snapshot(self.kit), kit_before)

    def test_cache_seeds_from_bundle_and_from_pin(self):
        state, results = self.scheduled(NOW)
        self.assertEqual([r.status for r in results], ["seeded"])
        self.assertEqual(self.cached(), self.first)
        other = make_tmp_dir(self) / "state"
        results = refresh_cache(catalog=self.catalog, state_dir=other, bundle_dir=None, settings=DEFAULTS,
                                now=NOW, gitleaks=None)
        self.assertEqual([r.status for r in results], ["seeded"], [r.describe() for r in results])
        self.assertEqual(read_upstream(cache_dir(other) / "demo").commit, self.first)

    def test_cache_checks_compare_with_the_bundled_pin(self):
        commit(self.upstream, {"LICENSE": APACHE_TEXT})
        state, results = self.scheduled(NOW)
        self.assertEqual([r.status for r in results], ["rolled-back"])
        self.assertFalse((cache_dir(self.state) / "demo").exists())

    def test_update_defers_while_lease_active(self):
        commit(self.upstream, {"skills/demo/ref.md": "changed\n"})
        store = Store(self.state)
        store.start(task="busy", agent="claude", scope=["src"], note="", ttl_hours=1.0,
                    info=repo_info(self.project))
        state, results = self.scheduled(dt.datetime.now(dt.timezone.utc))
        self.assertEqual(state, "deferred")
        self.assertIn("busy", results[0].reasons[0])
        self.assertFalse(cache_dir(self.state).exists())
        self.assertEqual(read_state(self.state), {})
        self.assertEqual(active_leases(self.state), ["busy"])

        store.release(task="busy", agent="claude", reason="done", force=False)
        state, results = self.scheduled(dt.datetime.now(dt.timezone.utc))
        self.assertEqual(state, "ran")
        self.assertEqual([r.status for r in results], ["advanced"])

    def test_publication_defers_when_a_lease_starts_during_preparation(self):
        commit(self.upstream, {"skills/demo/ref.md": "changed\n"})

        def fetch_then_lease(*args, **kwargs):
            tree = fetch(*args, **kwargs)
            if not active_leases(self.state):
                Store(self.state).start(task="late", agent="codex", scope=["docs"], note="", ttl_hours=1.0,
                                        info=repo_info(self.project))
            return tree

        state, results = self.scheduled(NOW, fetcher=fetch_then_lease)
        self.assertEqual(state, "deferred")
        self.assertIn("late", results[0].reasons[0])
        self.assertFalse((cache_dir(self.state) / "demo").exists())
        self.assertEqual(read_state(self.state), {})

    def test_idle_state_holds_the_state_lock(self):
        from oah.lockfile import LockTimeout, StateLock

        with idle_state(self.state):
            with self.assertRaises(LockTimeout):
                with StateLock(Store(self.state).lock_path, timeout=0.2):
                    pass
        Store(self.state).start(task="busy", agent="claude", scope=["src"], note="", ttl_hours=1.0,
                                info=repo_info(self.project))
        with self.assertRaises(LeasesActive):
            with idle_state(self.state):
                self.fail("must not enter while a lease is active")

    def test_scheduled_runs_at_most_once_per_interval(self):
        self.assertEqual(DEFAULTS["update_interval_days"], 1)
        self.assertEqual(self.scheduled(NOW)[0], "ran")
        self.assertEqual(self.scheduled(NOW + dt.timedelta(hours=23))[0], "not-due")
        self.assertEqual(self.scheduled(NOW + dt.timedelta(days=1))[0], "ran")
        self.assertEqual(self.scheduled(NOW + dt.timedelta(days=30), settings=dict(DEFAULTS, auto_update=False))[0],
                         "disabled")

    def test_unlazy_is_never_updated(self):
        work = make_tmp_dir(self)
        catalog = load_catalog(write_catalog(work, [bundled(self.upstream, name="unlazy", commit_id=self.first)]))
        results = refresh_cache(catalog=catalog, state_dir=self.state, bundle_dir=None, settings=DEFAULTS,
                                now=NOW, gitleaks=None)
        self.assertEqual([r.status for r in results], ["pinned"])
        self.assertFalse((cache_dir(self.state) / "unlazy").exists())


if __name__ == "__main__":
    unittest.main()
