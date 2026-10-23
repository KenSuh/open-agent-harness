# 스킬 갱신 검사 — 로컬 가짜 업스트림으로 핀 올리기, 라이선스·적재·수상한 파일·개인정보 실패 시 되돌림, 리스 대기, 주기를 확인한다.
from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import unittest

from oah.lease import Store
from oah.paths import repo_info
from oah.toolkit.catalog import Catalog, load_catalog, read_upstream
from oah.toolkit.fetch import fetch, stage_item
from oah.toolkit.install import InstallError, apply_install, plan_install, swap_in
from oah.toolkit.update import (DEFAULTS, active_leases, installed_destinations, read_state, run_update,
                                scheduled_update, vendored_destinations)
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
        plan = plan_install(["demo"], catalog=self.catalog, bundle_dir=self.kit, target="claude", level="project",
                            project_root=self.project, home=self.home)
        apply_install(plan, catalog=self.catalog, bundle_dir=self.kit, now=NOW)
        self.installed = self.project / ".claude" / "skills" / "demo"

    def destinations(self):
        return installed_destinations(self.catalog, project_root=self.project, home=self.home, include_user=False)

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
        shutil.rmtree(self.upstream)
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
    def scheduled(self, now):
        return scheduled_update(catalog=self.catalog, state_dir=self.state, project_root=self.project,
                                home=self.home, settings=DEFAULTS, now=now)

    def test_update_defers_while_lease_active(self):
        commit(self.upstream, {"skills/demo/ref.md": "changed\n"})
        store = Store(self.state)
        store.start(task="busy", agent="claude", scope=["src"], note="", ttl_hours=1.0,
                    info=repo_info(self.project))
        before = snapshot(self.installed)
        state, results = self.scheduled(dt.datetime.now(dt.timezone.utc))
        self.assertEqual(state, "deferred")
        self.assertIn("busy", results[0].reasons[0])
        self.assertEqual(snapshot(self.installed), before)
        self.assertEqual(read_state(self.state), {})
        self.assertEqual(active_leases(self.state), ["busy"])

        store.release(task="busy", agent="claude", reason="done", force=False)
        state, results = self.scheduled(dt.datetime.now(dt.timezone.utc))
        self.assertEqual(state, "ran")
        self.assertEqual([r.status for r in results], ["advanced"])

    def test_scheduled_runs_at_most_once_per_interval(self):
        self.assertEqual(self.scheduled(NOW)[0], "ran")
        self.assertEqual(self.scheduled(NOW + dt.timedelta(days=6))[0], "not-due")
        self.assertEqual(self.scheduled(NOW + dt.timedelta(days=7))[0], "ran")
        self.assertEqual(scheduled_update(catalog=self.catalog, state_dir=self.state, project_root=self.project,
                                          home=self.home, settings=dict(DEFAULTS, auto_update=False),
                                          now=NOW + dt.timedelta(days=30))[0], "disabled")


if __name__ == "__main__":
    unittest.main()
