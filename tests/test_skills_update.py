# 스킬 갱신 검사 — 로컬 가짜 업스트림으로 핀 올리기, 라이선스·적재·수상한 파일·개인정보 실패 시 되돌림, 리스 대기, 주기를 확인한다.
from __future__ import annotations

import datetime as dt
import dataclasses
import json
import os
import pathlib
import shutil
import threading
import unittest
from unittest import mock

from oah.lease import Store
from oah.paths import repo_info
from oah.toolkit.catalog import Catalog, load_catalog, read_upstream
from oah.toolkit.fetch import fetch, stage_item
from oah.toolkit.install import InstallError, apply_install, plan_install, swap_in
from oah.toolkit.update import (DEFAULTS, LeasesActive, active_leases, cache_dir, idle_state,
                                installed_destinations, read_state, refresh_cache, run_update, scheduled_update,
                                vendored_destinations)
from tests.fixtures.toolkit.upstream import (APACHE_TEXT, MIT_TEXT, TODAY, bundled, commit, make_executable,
                                             make_upstream, skill_text, write_catalog)
from tests.helpers import make_git_repo, make_tmp_dir

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
        self.assertEqual([p.name for p in self.installed.parent.iterdir()], ["demo"])

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


class ScheduledTest(UpdateFixture):
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
