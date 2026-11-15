# 준비 문맥 회귀 — 저장소 루트·목표·알 수 없는 버전과 일반 실행의 비교를 실제 명령 경계에서 확인한다.
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.helpers import git_commit
from tests.test_skill_delivery import DeliveryTask, PLAIN


class PreparationContextTest(DeliveryTask, unittest.TestCase):
    def command(self, *argv, cwd=None):
        from oah import cli

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(cwd or self.repo), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_A1_plans_from_a_subdirectory_use_the_project_root(self):
        self.build_skills(PLAIN)
        sub = self.repo / "subdir"
        sub.mkdir()
        for command in ("skills", "capabilities"):
            with self.subTest(command=command):
                result = self.command(command, "plan", "--task", "t1", "--engine", "claude", "--stage",
                                      "initial", "--json", cwd=sub)
                self.assertEqual(result[0], 0, result[1] + result[2])
                self.assertNotIn("Traceback", result[2])
        self.assertFalse(self.record.exists())
        self.assertEqual(self.runs(), [])

    def test_A2_capped_effort_qualifies_the_same_target_that_the_cli_launches(self):
        self.build_skills(PLAIN)
        path = self.repo / ".oah/config.toml"
        git_commit(self.repo, {".oah/config.toml": path.read_text(encoding="utf-8") +
            '\n[engines.claude.effort_caps]\n"opus"="medium"\n'}, "capped target fixture")

        result = self.command("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "high",
                              "--no-rerank", "--json")

        self.assertEqual(result[0], 0, result[1] + result[2])
        run = self.runs()[-1]
        target = json.loads((run / "skills.json").read_bytes())["prepared_e"]["facts"]["target"]
        launched = json.loads((run / "result.json").read_bytes())
        self.assertEqual((target["model"], target["effort"]), ("opus", "medium"))
        self.assertEqual((launched["resolved_model"], launched["launch_effort"]),
                         (target["model"], target["effort"]))
        from oah.discovery import budget, target_profiles

        earlier = {path: path.read_bytes() for path in (self.record, run / "skills.json", run / "result.json")}
        with mock.patch.object(budget, "_profile_source", return_value=budget.PinnedSource(
                target_profiles.DATA, budget.SOURCE_PIN)):
            refused = self.command("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "high",
                                   "--no-rerank", "--json")
        self.assertEqual(refused[0], 1, refused[1] + refused[2])
        self.assertIn("OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown)", refused[2])
        self.assertNotIn("Traceback", refused[2])
        self.assertEqual({path: path.read_bytes() for path in earlier}, earlier)
        self.assertEqual(self.runs(), [run])

    def test_A4_orca_unknown_cli_version_stays_unknown_through_worker_validation(self):
        from oah import call, call_cli, models, runs
        from oah.launchers import orca
        from oah.secrets import NoKeychain

        self.build_skills(PLAIN)
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(models, "probe_version", return_value=None):
            prepared = call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                route="orca", no_rerank=True), call_cli.load_settings(self.repo), root=self.repo,
                state_dir=self.state_dir, env=self.env, footer=tools.footer, has_footer=tools.has_footer,
                keychain=NoKeychain())
            self.assertIsNone(prepared.cli_version)
            client = mock.Mock(version="1.4.237")
            client.json.return_value = {"terminal": {"handle": "term_fixture"}}
            orca.start(client, prepared, orca.Placement(self.repo, "fixture", False))
            launch = json.loads(prepared.run.launch_path.read_bytes())
            self.assertIsNone(launch["cli_version"])
            for version in (None, runs.UNVERIFIED):
                prepared.run.launch_path.write_text(json.dumps(dict(launch, cli_version=version)), encoding="utf-8")
                with runs.WorkerRun(prepared.run) as queued:
                    worker = orca.worker_prepared(queued, self.repo, call_cli.load_settings(self.repo), "initial")
                    self.assertIsNone(worker.cli_version)
                    call.check_prepared(worker)
        self.assertFalse(self.record.exists())


class OrdinaryContextTest(unittest.TestCase):
    def test_an_unmanaged_initial_does_not_create_an_original_pointer(self):
        from tests.test_call import Project
        from tests.test_e_c10_boundaries import cli_result

        project = Project(self, config="\n[models]\nfetch_scores=false\n")
        project.bind()

        code, out, err = cli_result(project.repo, project.env, "call", "claude", "--stage", "initial",
                                    "--task", "t1", "--tier", "low", "--no-rerank", "--json")

        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["status"], "finished")
        self.assertEqual(list(project.state.glob("skills/originals/tasks/*.json")), [])
        prompt = project.state / "runs" / "t1" / project.runs()[0] / "prompt.md"
        self.assertEqual(project.recorded()["stdin"], prompt.read_text(encoding="utf-8"))

    def test_A5_non_intent_packet_parse_change_does_not_add_an_unmanaged_launch_refusal(self):
        from oah import call
        from tests.test_call import Project

        project = Project(self, config="\n[models]\nfetch_scores=false\n")
        project.bind()
        prepared = project.prepare(engine="claude", stage="initial", tier="low", no_rerank=True)
        packet = project.repo / ".oah/tasks/t1/PACKET.md"
        packet.write_text(packet.read_text(encoding="utf-8") +
            "\n## Plan\n- initial: kind=invalid intensity=low reasoning=low risk=low\n", encoding="utf-8")
        before = prepared.run.prompt_path.read_bytes()

        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo):
            result = call.execute(prepared, base_env=project.env)

        self.assertEqual(result["status"], "finished", result)
        self.assertEqual(prepared.run.prompt_path.read_bytes(), before)
        self.assertEqual(project.recorded()["stdin"].encode("utf-8"), before)
        self.assertIsNone(getattr(prepared, "e_digest", None))
