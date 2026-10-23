# 스킬 명령 검사 — 임시 프로젝트·HOME·키트로 oah skills list/install/update/schedule 의 계획 표시, 동의, 결과, 종료 코드를 확인한다.
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import plistlib
import sys
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from oah import skills_cli
from oah.engines.proc import RunResult
from oah.lease import Store
from oah.paths import repo_info
from oah.toolkit import read_skill
from oah.toolkit.catalog import Step
from oah.toolkit.fetch import fetch, stage_item
from oah.toolkit.schedule import TASK_NS, install_entry, plan_entry, remove_entry, task_xml
from oah.toolkit.catalog import load_catalog, read_upstream
from oah.toolkit.locate import locate_skill
from oah.toolkit.update import cache_dir
from tests.fixtures.toolkit.upstream import TODAY, bundled, commit, linked, make_upstream, write_catalog
from tests.helpers import make_git_repo, make_tmp_dir, write_posix_tool


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="oah")
    skills_cli.register(root.add_subparsers(dest="command"))
    return root


class CliFixture(unittest.TestCase):
    def setUp(self):
        self.upstream = make_upstream(self)
        work = make_tmp_dir(self)
        self.work = work
        self.kit = work / "kit"
        tree = fetch(str(self.upstream), "main", work / "scratch", paths=("skills/demo", "LICENSE"))
        stage_item(bundled(self.upstream), tree, self.kit / "skills" / "demo", fetched=TODAY)
        self.marker = work / "linked-ran"
        tool_dir = str(pathlib.Path(sys.executable).parent)
        self.runner_step = Step(run=(pathlib.Path(sys.executable).name, "-c",
                                     f"import pathlib; pathlib.Path({str(self.marker)!r}).write_text('ran')"))
        self.catalog = write_catalog(work, [bundled(self.upstream, commit_id=tree.commit)],
                                     [linked("helper", [self.runner_step]),
                                      linked("plugin-only", [Step(manual="/plugin install x@y")])])
        self.commit = tree.commit
        self.project = make_git_repo(self)
        self.home = work / "home"
        self.home.mkdir()
        self.state = work / "state"
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "OAH_KIT_DIR": str(self.kit),
               "OAH_STATE_DIR": str(self.state), "PATH": tool_dir + os.pathsep + os.environ.get("PATH", "")}
        for patcher in (mock.patch.dict(os.environ, env),
                        mock.patch("oah.toolkit.catalog.CATALOG_PATH", self.catalog),
                        mock.patch.object(skills_cli, "_interactive", lambda: False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)
        self.configure("auto_update = false")

    def configure(self, *lines: str) -> None:
        """Write the project's .oah/config.toml [skills] section."""
        path = self.project / ".oah" / "config.toml"
        path.parent.mkdir(exist_ok=True)
        path.write_text("[skills]\n" + "".join(line + "\n" for line in lines), encoding="utf-8")

    def oah(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = skills_cli.run(parser().parse_args(["skills", *argv]))
        return code, out.getvalue(), err.getvalue()


class ListInstallTest(CliFixture):
    def test_list_shows_bundled_linked_and_installed(self):
        self.assertEqual(self.oah("install", "demo", "--target", "claude")[0], 0)
        other = self.home / ".agents" / "skills" / "mine"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: mine\ndescription: my own\n---\n", encoding="utf-8")
        code, out, _ = self.oah("list", "--json")
        self.assertEqual(code, 0)
        rows = {(row["name"], row["status"]): row for row in json.loads(out)["skills"]}
        self.assertEqual(rows[("demo", "bundled")]["version"], self.commit[:12])
        self.assertEqual(rows[("demo", "bundled")]["license"], "MIT")
        self.assertIn("helper", {name for name, status in rows if status == "linked"})
        installed = rows[("demo", "installed (claude project, oah)")]
        self.assertEqual((installed["version"], installed["source"], installed["license"]),
                         (self.commit[:12], str(self.upstream), "MIT"))
        self.assertIn(("mine", "installed (codex global, other)"), rows)
        code, out, _ = self.oah("list")
        self.assertEqual(code, 0)
        self.assertIn("demo", out)

    def test_install_project_copies_skill_license_and_marker(self):
        code, out, _ = self.oah("install", "demo")
        self.assertEqual(code, 0, out)
        self.assertIn("Plan:", out)
        self.assertIn("OAH_SKILLS_LEASE task=oah-skills scope=.claude/skills,.agents/skills released", out)
        self.assertEqual(Store(self.state).snapshot(), [])
        for folder in (".claude", ".agents"):
            skill = self.project / folder / "skills" / "demo"
            self.assertEqual(sorted(p.name for p in skill.iterdir()),
                             [".oah-skill.json", "LICENSE", "SKILL.md", "ref.md"])
            info = read_skill(skill)
            self.assertEqual((info.name, info.version, info.license), ("demo", self.commit, "MIT"))
        self.assertFalse((self.home / ".claude").exists())

    def test_install_dry_run_writes_nothing(self):
        code, out, _ = self.oah("install", "--all", "--dry-run", "--user")
        self.assertEqual(code, 0)
        self.assertIn("OAH_SKILLS_DRY_RUN", out)
        self.assertIn("outside the project", out)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse((self.project / ".claude").exists())

    def test_install_user_needs_consent(self):
        code, _, err = self.oah("install", "demo", "--user", "--target", "codex")
        self.assertEqual(code, 1)
        self.assertIn("OAH_SKILLS_CONSENT_REQUIRED", err)
        self.assertIn("--yes", err)
        self.assertEqual(list(self.home.iterdir()), [])
        code, _, _ = self.oah("install", "demo", "--global", "--target", "codex", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue((self.home / ".agents" / "skills" / "demo" / "SKILL.md").is_file())

    def test_project_install_refuses_an_overlapping_lease(self):
        Store(self.state).start(task="other", agent="codex", scope=[".claude"], note="", ttl_hours=1.0,
                                info=repo_info(self.project))
        code, _, err = self.oah("install", "demo", "--target", "claude")
        self.assertEqual(code, 1)
        self.assertIn("OAH_LEASE_CONFLICT", err)
        self.assertFalse((self.project / ".claude" / "skills").exists())

    def test_install_refuses_foreign_folder(self):
        foreign = self.project / ".claude" / "skills" / "demo"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("mine\n", encoding="utf-8")
        code, out, err = self.oah("install", "demo")
        self.assertEqual(code, 1)
        self.assertIn("OAH_SKILLS_INSTALL_CONFLICT", err)
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "mine\n")
        self.assertFalse((self.project / ".agents").exists())
        self.assertEqual(self.oah("install", "demo", "--target", "codex")[0], 0)
        self.assertEqual(self.oah("install", "demo", "--target", "codex")[0], 0, "reinstalling replaces oah's copy")

    def test_install_unknown_name(self):
        code, _, err = self.oah("install", "nope")
        self.assertEqual(code, 1)
        self.assertIn("OAH_SKILLS_UNKNOWN", err)

    def test_install_linked_runs_official_command_with_consent(self):
        code, _, err = self.oah("install", "helper")
        self.assertEqual(code, 1)
        self.assertIn("OAH_SKILLS_CONSENT_REQUIRED", err)
        self.assertFalse(self.marker.exists())
        code, out, _ = self.oah("install", "helper", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "ran")
        self.assertIn("OAH_SKILL_LINKED helper", out)

    def test_linked_manual_steps_are_printed(self):
        code, out, _ = self.oah("install", "plugin-only")
        self.assertEqual(code, 0)
        self.assertIn("OAH_SKILL_MANUAL_STEP plugin-only: type this in the tool yourself: /plugin install x@y", out)

    def test_install_without_bundle_fetches_pinned_commit(self):
        commit(self.upstream, {"skills/demo/ref.md": "newer\n"})
        with mock.patch.dict(os.environ, {"OAH_KIT_DIR": str(self.work / "empty-kit")}):
            code, out, _ = self.oah("install", "demo", "--target", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("fetch", out)
        skill = self.project / ".claude" / "skills" / "demo"
        self.assertEqual((skill / "ref.md").read_text(encoding="utf-8"), "reference\n")
        self.assertEqual(read_skill(skill).version, self.commit)


class UpdateCommandTest(CliFixture):
    def installed_version(self) -> str:
        return read_skill(self.project / ".claude" / "skills" / "demo").version

    def test_update_project_check_then_apply_under_its_own_lease(self):
        self.assertEqual(self.oah("install", "demo", "--target", "claude")[0], 0)
        new = commit(self.upstream, {"skills/demo/ref.md": "reference\nmore\n"})
        Store(self.state).start(task="unrelated", agent="codex", scope=["src"], note="", ttl_hours=1.0,
                                info=repo_info(self.project))
        code, out, _ = self.oah("update", "--project", "--check")
        self.assertEqual(code, 0)
        self.assertIn("available", out)
        self.assertEqual(self.installed_version(), self.commit)
        code, out, _ = self.oah("update", "--project")
        self.assertEqual(code, 0, out)
        self.assertIn("advanced", out)
        self.assertIn("OAH_SKILLS_LEASE task=oah-skills", out)
        self.assertEqual(self.installed_version(), new)
        self.assertEqual([lease.task for lease, _ in Store(self.state).snapshot()], ["unrelated"])

    def test_plain_update_refreshes_only_the_cache(self):
        self.assertEqual(self.oah("install", "demo", "--target", "claude")[0], 0)
        new = commit(self.upstream, {"skills/demo/ref.md": "changed\n"})
        code, out, _ = self.oah("update", "--check")
        self.assertEqual(code, 0)
        self.assertFalse(cache_dir(self.state).exists())
        code, out, _ = self.oah("update")
        self.assertEqual(code, 0, out)
        self.assertEqual(read_upstream(cache_dir(self.state) / "demo").commit, new)
        self.assertEqual(self.installed_version(), self.commit)
        code, out, _ = self.oah("list", "--json")
        statuses = {(row["name"], row["status"]) for row in json.loads(out)["skills"]}
        self.assertIn(("demo", "cached"), statuses)

    def test_cache_update_refuses_while_lease_active_and_scheduled_defers(self):
        commit(self.upstream, {"skills/demo/ref.md": "changed\n"})
        Store(self.state).start(task="busy", agent="claude", scope=["src"], note="", ttl_hours=1.0,
                                info=repo_info(self.project))
        code, _, err = self.oah("update")
        self.assertEqual(code, 1)
        self.assertIn("OAH_SKILLS_LEASES_ACTIVE", err)
        self.configure("auto_update = true")
        code, out, _ = self.oah("update", "--scheduled")
        self.assertEqual(code, 0)
        self.assertIn("deferred", out)
        self.assertFalse((cache_dir(self.state) / "demo").exists())

    def test_global_update_needs_consent(self):
        self.assertEqual(self.oah("install", "demo", "--global", "--target", "codex", "--yes")[0], 0)
        commit(self.upstream, {"skills/demo/ref.md": "changed\n"})
        code, _, err = self.oah("update", "--global")
        self.assertEqual(code, 1)
        self.assertIn("OAH_SKILLS_CONSENT_REQUIRED", err)
        code, out, _ = self.oah("update", "--global", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("advanced", out)

    def test_mutating_command_runs_the_due_check(self):
        self.assertEqual(self.oah("install", "demo", "--target", "claude")[0], 0)
        new = commit(self.upstream, {"skills/demo/ref.md": "reference\nmore\n"})
        self.configure("auto_update = true")
        code, out, _ = self.oah("list")
        self.assertNotIn("OAH_SKILLS_AUTO_UPDATE", out, "read-only commands never update")
        self.assertFalse(cache_dir(self.state).exists())
        code, out, _ = self.oah("install", "plugin-only")
        self.assertEqual(code, 0)
        self.assertIn("OAH_SKILLS_AUTO_UPDATE kit cache checked 1 items", out)
        self.assertEqual(read_upstream(cache_dir(self.state) / "demo").commit, new)
        self.assertEqual(self.installed_version(), self.commit, "the automatic check never writes project pins")
        code, out, _ = self.oah("install", "plugin-only")
        self.assertNotIn("OAH_SKILLS_AUTO_UPDATE", out, "at most once per interval")

    def test_install_prefers_the_cache_copy(self):
        new = commit(self.upstream, {"skills/demo/ref.md": "cached\n"})
        self.assertEqual(self.oah("update")[0], 0)
        code, out, _ = self.oah("install", "demo", "--target", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn(str(cache_dir(self.state) / "demo"), out)
        self.assertEqual(self.installed_version(), new)

    def test_update_failed_check_exits_refused(self):
        self.assertEqual(self.oah("install", "demo", "--target", "claude")[0], 0)
        commit(self.upstream, {"skills/demo/SKILL.md": "---\nname: demo\n---\n"})
        code, out, _ = self.oah("update", "--project")
        self.assertEqual(code, 1)
        self.assertIn("rolled-back", out)
        self.assertEqual(self.installed_version(), self.commit)


class ConfigTest(CliFixture):
    def test_config_errors_exit_69(self):
        self.configure("auto_updates = true")
        code, _, err = self.oah("update")
        self.assertEqual(code, 69)
        self.assertIn("OAH_CONFIG_UNKNOWN_KEY", err)
        self.configure("update_interval_days = 0")
        code, _, err = self.oah("update")
        self.assertEqual(code, 69)
        self.assertIn("OAH_CONFIG_INVALID", err)
        self.configure("pins = [1]")
        self.assertEqual(self.oah("update")[0], 69)

    def test_pins_hold_an_item_and_the_log_records_why(self):
        self.assertEqual(self.oah("install", "demo", "--target", "claude")[0], 0)
        commit(self.upstream, {"skills/demo/ref.md": "changed\n"})
        self.configure("auto_update = false", 'pins = ["demo"]')
        code, out, _ = self.oah("update", "--project")
        self.assertEqual(code, 0, out)
        self.assertIn("pinned", out)
        self.assertEqual(read_skill(self.project / ".claude" / "skills" / "demo").version, self.commit)
        self.configure("auto_update = false")
        self.assertEqual(self.oah("update", "--project")[0], 0)
        log = [json.loads(line) for line in
               (self.state / "toolkit" / "updates.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(entry["status"], entry["why"]) for entry in log],
                         [("pinned", "update --project"), ("advanced", "update --project")])
        self.assertEqual(log[1]["old_commit"], self.commit)
        self.assertTrue(log[1]["ts"].endswith("Z"))
        self.assertEqual(log[1]["diff"]["files_changed"], 1)


class LocateTest(CliFixture):
    def test_precedence_is_project_then_cache_then_user_then_plugin(self):
        catalog = load_catalog()
        plugin = self.home / ".claude" / "plugins" / "cache" / "market" / "demo-plugin" / "1.0" / "skills" / "demo"
        plugin.mkdir(parents=True)
        (plugin / "SKILL.md").write_text("---\nname: demo\ndescription: plugin copy\n---\n", encoding="utf-8")

        def origins():
            return [found.origin for found in locate_skill("demo", catalog=catalog, project_root=self.project,
                                                           cache_dir=cache_dir(self.state), home=self.home)]

        self.assertEqual(origins(), ["plugin"])
        self.assertEqual(self.oah("install", "demo", "--global", "--target", "claude", "--yes")[0], 0)
        self.assertEqual(origins(), ["user", "plugin"])
        self.assertEqual(self.oah("update")[0], 0)
        self.assertEqual(origins(), ["cache", "user", "plugin"])
        self.assertEqual(self.oah("install", "demo", "--target", "codex")[0], 0)
        found = locate_skill("demo", catalog=catalog, project_root=self.project, cache_dir=cache_dir(self.state),
                             home=self.home)
        self.assertEqual([f.origin for f in found], ["project", "cache", "user", "plugin"])
        self.assertEqual(found[0].info.path, self.project / ".agents" / "skills" / "demo")


class ScheduleTest(unittest.TestCase):
    def entry(self, platform: str):
        root = make_tmp_dir(self)
        return plan_entry(project_root=root / "my project", home=root / "home", state_dir=root / "state",
                          platform=platform, python="/opt/py/bin/python3")

    def test_plist_runs_scheduled_update(self):
        entry = self.entry("darwin")
        self.assertEqual(entry.path.parent.name, "LaunchAgents")
        self.assertEqual(entry.argv[1:], ("-m", "oah", "skills", "update", "--scheduled"))
        data = plistlib.loads(skills_plist(entry))
        self.assertEqual(data["ProgramArguments"], list(entry.argv))
        self.assertEqual(data["WorkingDirectory"], str(entry.project_root))
        self.assertEqual(data["StartInterval"], 86400)

    def test_task_xml_runs_scheduled_update(self):
        entry = self.entry("win32")
        tree = ET.fromstring(task_xml(entry).split("\n", 1)[1])
        ns = {"t": TASK_NS}
        self.assertEqual(tree.find("t:Actions/t:Exec/t:Command", ns).text, "/opt/py/bin/python3")
        self.assertEqual(tree.find("t:Actions/t:Exec/t:Arguments", ns).text,
                         "-m oah skills update --scheduled")
        self.assertEqual(tree.find("t:Actions/t:Exec/t:WorkingDirectory", ns).text, str(entry.project_root))

    @unittest.skipIf(os.name == "nt", "fake launchctl is a POSIX shell script")
    def test_schedule_install_and_remove_with_fake_launchctl(self):
        entry = self.entry("darwin")
        tools = make_tmp_dir(self)
        calls = tools / "calls.txt"
        write_posix_tool(tools, "launchctl", f'echo "$@" >> "{calls}"')
        install_entry(entry, search_path=str(tools))
        self.assertTrue(entry.path.is_file())
        self.assertTrue(remove_entry(entry, search_path=str(tools)))
        self.assertFalse(entry.path.exists())
        self.assertFalse(remove_entry(entry, search_path=str(tools)))
        lines = calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["bootstrap", "bootout"])

    def test_failed_bootstrap_leaves_no_plist(self):
        entry = self.entry("darwin")
        calls: list = []

        def runner(argv, timeout):
            calls.append(argv)
            return RunResult(5, "", "denied", False)

        with mock.patch("oah.toolkit.schedule._tool", lambda name, path: (name,)):
            with self.assertRaises(Exception):
                install_entry(entry, runner=runner)
        self.assertFalse(entry.path.exists())

    @unittest.skipUnless(os.name == "nt", "Task Scheduler is Windows-only")
    def test_schtasks_xml_is_utf16(self):
        entry = self.entry("win32")
        seen = {}

        def runner(argv, timeout):
            if "/Create" in argv:
                xml_path = pathlib.Path(argv[argv.index("/XML") + 1])
                seen["bytes"] = xml_path.read_bytes()
            return RunResult(0, "", "", False)

        install_entry(entry, runner=runner)
        self.assertTrue(seen["bytes"].startswith(b"\xff\xfe"))
        self.assertIn("--scheduled", seen["bytes"].decode("utf-16"))
        self.assertTrue(remove_entry(entry, runner=runner))


def skills_plist(entry) -> bytes:
    from oah.toolkit.schedule import launchd_plist

    return launchd_plist(entry)


if __name__ == "__main__":
    unittest.main()
