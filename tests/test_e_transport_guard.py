# 전송 직전 회귀 — 준비 기록 저장 뒤의 거절과 판단·CLI·실행 실패의 원래 사유를 확인한다.
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.helpers import make_tmp_dir
from tests.test_design_judgement import JudgementCase, config as judge_config
from tests.test_skill_delivery import DeliveryTask, PLAIN


class LastGuardTest(unittest.TestCase):
    def test_policy_change_during_first_attempt_save_refuses_with_zero_requests(self):
        from oah import atomic, skillpack
        from oah.discovery import gateway
        from oah.discovery.store import digest

        root = make_tmp_dir(self)
        config = root / ".oah/config.toml"
        config.parent.mkdir()
        config.write_text('[skills.discovery]\nlicense_allow=["MIT"]\n', encoding="utf-8")
        sent = []
        real = atomic.write_json_atomic

        def save(path, record, **options):
            result = real(path, record, **options)
            if path.parent.name == "attempts":
                config.write_text('[skills.discovery]\nlicense_allow=["Apache-2.0"]\n', encoding="utf-8")
            return result

        with contextlib.chdir(root):
            admitted = skillpack.explore(1)
            with mock.patch.object(atomic, "write_json_atomic", save), \
                    self.assertRaises(skillpack.PackRefused) as caught:
                gateway.send(admitted, "skill-search", digest("fixture-summary"), lambda: sent.append(1), state=root)
        self.assertEqual(caught.exception.reason, "changed")
        self.assertEqual(sent, [])
        rows = [row for path in (root / "skills/attempts").glob("*.json")
                for row in json.loads(path.read_text(encoding="utf-8"))["requests"]]
        self.assertTrue(all(row["requests"] == 0 for row in rows), rows)


class JudgeGuardTest(JudgementCase):
    def request(self):
        from oah.rerank import judge

        return judge.Request("skill-fit", "skill-fit/v1", (("o0:skill_excerpt", self.notes()),
            ("o1:skill_excerpt", self.notes()), ("design_stage", judge.label("design-stage", "data")),
            ("check", judge.label("design-check", "data.entities"))), ("o0", "o1"))

    def test_changed_genuine_admission_at_judge_entry_stays_typed_even_when_disabled(self):
        from oah import skillpack

        root = make_tmp_dir(self)
        path = root / ".oah/config.toml"
        path.parent.mkdir()
        for enabled in (True, False):
            with self.subTest(enabled=enabled), contextlib.chdir(root):
                path.write_text("[skills]\n", encoding="utf-8")
                self.admitted = skillpack.explore(1)
                path.write_text("[skills]\n# fixture policy changed at judge entry\n", encoding="utf-8")
                with self.assertRaises(skillpack.PackRefused) as caught:
                    self.run_judge(self.request(), cfg=judge_config(enabled=enabled))
                self.assertEqual(caught.exception.reason, "changed")
                self.assertEqual(self.provider.calls, [])

    def test_changed_genuine_admission_at_presend_stays_typed(self):
        from oah import skillpack
        from oah.rerank import judge

        root = make_tmp_dir(self)
        path = root / ".oah/config.toml"
        path.parent.mkdir()
        path.write_text("[skills]\n", encoding="utf-8")
        real = judge._notice

        def notice(*args, **options):
            path.write_text("[skills]\n# fixture policy changed at notice\n", encoding="utf-8")
            return real(*args, **options)

        with contextlib.chdir(root):
            self.admitted = skillpack.explore(1)
            with mock.patch.object(judge, "_notice", notice), self.assertRaises(skillpack.PackRefused) as caught:
                self.run_judge(self.request())
        self.assertEqual(caught.exception.reason, "changed")
        self.assertEqual(self.provider.calls, [])

    def test_changed_admission_refuses_even_with_a_successful_cached_judgement(self):
        from oah import skillpack
        from oah.rerank.base import JudgeReply

        root = make_tmp_dir(self)
        path = root / ".oah/config.toml"
        path.parent.mkdir()
        path.write_text("[skills]\n", encoding="utf-8")
        original = self.provider.judge

        def reply(*args, **options):
            return JudgeReply(original(*args, **options), "jev-1.14.0")

        with contextlib.chdir(root), mock.patch.object(self.provider, "judge", side_effect=reply):
            self.admitted = skillpack.explore(1)
            request = self.request()
            first = self.run_judge(request, state_dir=root)
            self.assertEqual(first.status, "used")
            cached = self.run_judge(request, state_dir=root)
            self.assertEqual(cached.status, "used(cache)")
            calls = list(self.provider.calls)
            path.write_text("[skills]\n# fixture policy changed before cache reuse\n", encoding="utf-8")
            with self.assertRaises(skillpack.PackRefused) as caught:
                self.run_judge(request, state_dir=root)
        self.assertEqual(caught.exception.reason, "changed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.provider.calls, calls)


class FailureSurfaceTest(DeliveryTask, unittest.TestCase):
    def test_launch_failure_keeps_receiving_write_failure_in_the_run_error(self):
        from oah import atomic, call, call_cli
        from oah.capabilities import inventory

        self.build_skills(PLAIN)
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
            prepared = call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                no_rerank=True), call_cli.load_settings(self.repo), root=self.repo, state_dir=self.state_dir,
                env=self.env, footer=tools.footer, has_footer=tools.has_footer)
            sink = mock.Mock(observation={"stopped": False, "quiescent": True})
            original = atomic.write_json_atomic

            def write(path, value, **options):
                if path.name == inventory.RECEIVING_NAME:
                    raise PermissionError("fixture receiving write refused")
                return original(path, value, **options)

            for failure in (OSError("fixture launch failure"), KeyboardInterrupt()):
                with self.subTest(failure=type(failure).__name__), \
                        mock.patch.object(call, "_capability_sink", return_value=sink), \
                        mock.patch.object(call, "_run_prepared", side_effect=failure), \
                        mock.patch.object(atomic, "write_json_atomic", side_effect=write):
                    if isinstance(failure, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            call.execute(prepared, base_env=self.env)
                        result = prepared.run.read()
                    else:
                        result = call.execute(prepared, base_env=self.env)
                    self.assertEqual(result["status"], "failed")
                    self.assertIn("OAH_CAPABILITY_RECEIVING_FAILED", result["error"])
                    self.assertIn("PermissionError", result["error"])
                    self.assertFalse(prepared.run.file_path(inventory.RECEIVING_NAME).exists())

    def test_design_cli_prints_a_gateway_refusal_without_traceback(self):
        from oah import cli, design_cli, skillpack
        from oah.discovery import attempts, gateway
        from oah.discovery.store import digest

        self.build_skills(PLAIN)
        config = self.repo / ".oah/config.toml"
        sent = []

        def handler(_args, _store):
            admitted = skillpack.explore(1)
            config.write_text(config.read_text(encoding="utf-8") +
                              "\n# fixture changed immediately before design transport\n", encoding="utf-8")
            with attempts.scope(self.state_dir):
                gateway.send(admitted, "skill-judge", digest("fixture-design"), lambda: sent.append(1))
            return 0

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.dict(design_cli.HANDLERS, {"status": handler}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["design", "status", "acme/booking"])
        self.assertEqual(code, 1, out.getvalue() + err.getvalue())
        self.assertTrue(err.getvalue().startswith("OAH_SKILL_NOT_DELIVERABLE(changed)"), err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(sent, [])
