# 문맥 기록 회귀 — 명령의 루트와 축 및 이전 준비가 재검사에서도 보존되는지 확인한다.
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests import e_c6_fixture, test_e_collector_gateway as collector_fixture
from tests.test_skill_discovery import DiscoveryRepo
from tests.test_skill_delivery import DeliveryTask, PLAIN


class TasklessRootTest(DiscoveryRepo, unittest.TestCase):
    collect = collector_fixture.CollectorGatewayTest.collect

    def skills(self, *args, expect=0):
        sub = self.repo / "subdirectory"
        sub.mkdir(exist_ok=True)
        result = self.oah("skills", *args, cwd=sub)
        if expect is not None:
            self.assertEqual(result[0], expect, result[1] + result[2])
        return result

    def test_review_from_subdirectory_refuses_policy_change_at_git_url(self):
        code, out, err, trace, summaries = self.collect("url")
        self.assertEqual(code, 1, out + err)
        self.assertIn("OAH_SKILL_NOT_DELIVERABLE(changed)", err)
        self.assertEqual(trace, [])
        self.assertEqual(summaries, [])

    def test_taskless_admission_rechecks_its_recorded_root_after_cwd_changes(self):
        from oah import skillpack

        self.config('[skills.discovery]\nlicense_allow=["MIT"]\n')
        sub = self.repo / "subdirectory"
        sub.mkdir()
        for kind in ("explore", "inspect"):
            with self.subTest(kind=kind), contextlib.chdir(sub):
                admitted = (skillpack.explore(1) if kind == "explore" else
                            skillpack.inspect(("fixture", "design", 1, "sha256:" + "a" * 64), max_queries=1))
                with contextlib.chdir(self.home):
                    admitted.require()
                    config = self.repo / ".oah/config.toml"
                    before = config.read_bytes()
                    config.write_text('[skills.discovery]\nlicense_allow=["Apache-2.0"]\n', encoding="utf-8")
                    try:
                        with self.assertRaises(skillpack.PackRefused) as caught:
                            admitted.require()
                        self.assertEqual(caught.exception.reason, "changed")
                    finally:
                        config.write_bytes(before)


class RecordedAxesTest(DeliveryTask, unittest.TestCase):
    def test_managed_config_and_packet_parse_changes_are_typed_refusals_before_launch(self):
        from oah import call, call_cli

        self.build_skills(PLAIN)
        config, packet = self.repo / ".oah/config.toml", self.repo / ".oah/tasks/t1/PACKET.md"
        earlier = {path: path.read_bytes() for path in (config, packet)}
        tools = call_cli.intent_tools()
        for change in ("config", "packet"):
            with self.subTest(change=change), mock.patch.dict(os.environ, self.env, clear=True), \
                    contextlib.chdir(self.repo):
                for path, content in earlier.items():
                    path.write_bytes(content)
                prepared = call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                    no_rerank=True), call_cli.load_settings(self.repo), root=self.repo, state_dir=self.state_dir,
                    env=self.env, footer=tools.footer, has_footer=tools.has_footer)
                kept = prepared.run.file_path("skills.json").read_bytes()
                if change == "config":
                    config.write_text(config.read_text(encoding="utf-8") +
                                      '\n[skills]\nmax_queries="many"\n', encoding="utf-8")
                else:
                    packet.write_text(packet.read_text(encoding="utf-8") +
                        "\n## Plan\n- initial: kind=invalid intensity=low reasoning=low risk=low\n", encoding="utf-8")
                with self.assertRaises(call.EngineError) as caught:
                    call.execute(prepared, base_env=self.env)
                self.assertEqual(caught.exception.code, "OAH_SKILL_NOT_DELIVERABLE(changed)")
                self.assertFalse(self.record.exists())
                self.assertEqual(prepared.run.file_path("skills.json").read_bytes(), kept)

    def test_packet_tier_and_explicit_docs_kind_finish_with_the_prepared_target(self):
        from oah import cli

        self.build_skills(PLAIN)
        config = self.repo / ".oah/config.toml"
        config.write_text(config.read_text(encoding="utf-8") +
            '\n[engines.claude.effort_caps]\n"opus"="low"\n"sonnet"="low"\n', encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["call", "claude", "--stage", "initial", "--task", "t1", "--kind", "docs",
                             "--no-rerank", "--json"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        run = self.runs()[-1]
        result = json.loads((run / "result.json").read_text(encoding="utf-8"))
        facts = json.loads((run / "skills.json").read_text(encoding="utf-8"))["prepared_e"]["facts"]
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["launch_effort"], "low")
        self.assertEqual((facts["target"]["model"], facts["target"]["effort"]),
                         (result["resolved_model"], result["launch_effort"]))
        self.assertTrue(self.record.exists())

    def test_hash_only_preparation_on_a_selected_run_refuses_correction_without_a_child(self):
        from tests.test_fix import ReviewError

        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        path = original.run.file_path("skills.json")
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["prepared_e"] = {"identity_digest": saved["prepared_e"]["digest"]}
        path.write_text(json.dumps(saved), encoding="utf-8")
        kept, calls = path.read_bytes(), project.fix_calls()
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                self.assertRaises(ReviewError) as caught:
            project.fix()
        self.assertEqual(caught.exception.code, "OAH_FIX_STATE_CHANGED")
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(path.read_bytes(), kept)

    def test_extra_run_folders_do_not_block_the_pointed_original(self):
        from oah import cli
        from oah.review import fix
        from tests.test_fix import RULES

        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        for index in range(1025):
            (original.run.path.parent / f"r{index:05d}").mkdir()
        kept = {path: path.read_bytes() for path in original.run.path.iterdir() if path.is_file()}
        calls = project.fix_calls()
        real = fix.correct
        out, err = io.StringIO(), io.StringIO()

        def correct(request, **options):
            return real(request, **options, rules=RULES, temp_root=project.temp_root)

        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                mock.patch.object(fix, "correct", correct), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["fix", "--task", "t1", "--tier", "high", "--json"])

        result = json.loads(out.getvalue())
        self.assertEqual((code, result["code"]), (0, "OAH_FIX_APPLIED"), str(result) + err.getvalue())
        self.assertEqual(len(project.fix_calls()), len(calls) + 1)
        self.assertIn("Prepare the selected ledger rows.", project.fix_calls()[-1]["prompt"])
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)


class OrdinaryAndCodeTest(unittest.TestCase):
    def test_unmanaged_packet_read_and_parse_errors_keep_the_existing_launch_behavior(self):
        from oah import call
        from tests.test_call import Project

        for change in ("unreadable", "invalid-design"):
            with self.subTest(change=change):
                project = Project(self, config="\n[models]\nfetch_scores=false\n")
                project.bind()
                prepared = project.prepare(engine="claude", stage="initial", tier="low", no_rerank=True)
                packet = project.repo / ".oah/tasks/t1/PACKET.md"
                if change == "invalid-design":
                    packet.write_text(packet.read_text(encoding="utf-8") +
                                      "\n## Design\n- nonsense: x\n", encoding="utf-8")
                original = call.read_packet

                def read(*args, **options):
                    if change == "unreadable":
                        packet.unlink()
                    return original(*args, **options)

                prompt = prepared.run.prompt_path.read_bytes()
                with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                        mock.patch.object(call, "read_packet", side_effect=read):
                    result = call.execute(prepared, base_env=project.env)
                self.assertEqual(result["status"], "finished", result)
                self.assertEqual(project.recorded()["stdin"].encode("utf-8"), prompt)

    def test_invalid_skills_config_after_unmanaged_preparation_finishes_as_main(self):
        from oah import call
        from tests.test_call import Project

        project = Project(self, config="\n[models]\nfetch_scores=false\n")
        project.bind()
        prepared = project.prepare(engine="claude", stage="initial", tier="low", no_rerank=True)
        config = project.repo / ".oah/config.toml"
        config.write_text(config.read_text(encoding="utf-8") + '\n[skills]\nmax_queries="many"\n', encoding="utf-8")
        prompt = prepared.run.prompt_path.read_bytes()
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo):
            result = call.execute(prepared, base_env=project.env)
        self.assertEqual(result["status"], "finished", result)
        self.assertEqual(project.recorded()["stdin"].encode("utf-8"), prompt)
