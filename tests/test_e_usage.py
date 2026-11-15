# E 사용량 회귀 시험 — 늦은 준비 거절과 잘못 끝난 스트림에서도 이미 관찰한 요청·사용량 수치를 보존한다.
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import unittest
from unittest import mock

from tests import test_skill_delivery as fixture
from tests.helpers import make_tmp_dir
from tests.test_rerank import FakeServer, fake_key
from tests.test_skill_delivery import DeliveryTask


class UsageFactsTest(unittest.TestCase):
    def test_a_changed_usage_field_cannot_be_saved_as_a_closed_receiving_record(self):
        from oah import skillpack
        from oah.capabilities.usage import AvailableUsage

        usage = AvailableUsage()
        usage.event({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 7}}})
        changed = json.loads(json.dumps(usage.record()))
        changed["observed"]["input_tokens"] = "unavailable"
        with self.assertRaisesRegex(ValueError, "not closed"):
            skillpack.stream_receiving("r1", {"items": [], "digest": None},
                {"rows": [], "whole": {}, "session": None}, target={}, at="2026-11-10T00:00:00Z", usage=changed)

    def test_a_summary_with_a_changed_digest_refuses_and_keeps_the_earlier_record(self):
        from oah import skillpack
        from oah.discovery import attempts, gateway
        from oah.discovery.store import digest

        state = make_tmp_dir(self)
        sent = []
        with contextlib.chdir(state), attempts.scope(state) as attempt:
            gateway.send(skillpack.explore(1), "skill-search", digest("fixture request"), lambda: sent.append(1))
        self.assertEqual(sent, [1])
        (path,) = (state / "skills" / "attempts").glob("*.json")
        before = path.read_bytes()
        attempt.rows[0]["outcome_digest"] = "not-a-digest"

        with self.assertRaisesRegex(ValueError, "closed-summary"):
            attempt.save()

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(before)["requests"][0]["requests"], 1)

    def test_partial_message_updates_keep_prior_fields_and_an_empty_result_has_unknown_usage(self):
        from oah.capabilities.usage import AvailableUsage

        usage = AvailableUsage()
        usage.event({"type": "result"})
        self.assertEqual(usage.record()["availability"], "unknown")
        usage.event({"type": "assistant", "message": {"id": "one", "usage": {"input_tokens": 7}}})
        usage.event({"type": "assistant", "message": {"id": "one", "usage": {"output_tokens": 3}}})
        saved = usage.record()
        self.assertEqual(saved["observed"]["input_tokens"], 7)
        self.assertEqual(saved["observed"]["output_tokens"], 3)
        self.assertEqual(saved["observed_messages"], 1)
        self.assertEqual(saved["availability"], "partial")

    def test_usage_before_a_malformed_stream_is_kept_and_totals_stay_unknown(self):
        from oah.capabilities.inventory import EventSink

        root = make_tmp_dir(self)
        sink = EventSink(run="r1", cwd=str(root), cli_version="2.1.318", permission_mode="acceptEdits",
                         planned={}, bundle_id=None, timeout_s=60)
        events = ({"type": "system", "subtype": "init", "session_id": "s1", "cwd": str(root), "tools": ["Read"],
                   "model": "claude-test", "permissionMode": "acceptEdits", "claude_code_version": "2.1.318"},
                  {"type": "assistant", "session_id": "s1", "message": {
                      "id": "message-1", "content": [], "usage": {"input_tokens": 7, "output_tokens": 3}}})
        data = b"".join((json.dumps(event) + "\n").encode("utf-8") for event in events) + b"{broken\n"

        self.assertEqual(sink.feed("stdout", data), "invalid-json")
        saved = sink.finish(1, True)

        self.assertIn("available_usage", saved)
        usage = saved["available_usage"]
        self.assertEqual(usage["observed"]["input_tokens"], 7)
        self.assertEqual(usage["observed"]["output_tokens"], 3)
        self.assertIsNone(usage["totals"]["input_tokens"])
        self.assertIsNone(usage["totals"]["output_tokens"])
        self.assertEqual(usage["availability"], "partial")
        self.assertEqual(sink.finish(1, True), saved)

    def test_no_reported_usage_is_unknown_and_repeated_message_updates_are_counted_once(self):
        from oah.capabilities.usage import AvailableUsage

        usage = AvailableUsage()
        self.assertEqual(usage.record()["availability"], "unknown")
        self.assertTrue(all(value is None for value in usage.record()["totals"].values()))
        for output in (1, 3):
            usage.event({"type": "assistant", "session_id": "s1", "message": {
                "id": "same-message", "usage": {"input_tokens": 7, "output_tokens": output}}})
        saved = usage.record()
        self.assertEqual(saved["observed_messages"], 1)
        self.assertEqual(saved["observed"]["input_tokens"], 7)
        self.assertEqual(saved["observed"]["output_tokens"], 3)


class AttemptEvidenceTest(DeliveryTask, unittest.TestCase):
    def test_a_raw_reference_stream_keeps_available_usage_before_result_normalization(self):
        from oah import call, skillpack
        from oah.engines.proc import RunResult
        from oah.launchers import native

        self.build_skills(fixture.PLAIN)
        prepared = self.prepare()
        bundle = skillpack.bundle_of(prepared.run.path)
        assert bundle is not None
        raw = json.dumps({"type": "assistant", "message": {"id": "m1", "usage": {
            "input_tokens": 7, "output_tokens": 3}}}) + "\n{broken\n"

        result, _warnings, failure = call._conclude_skill_reads(prepared, bundle, raw,
            RunResult(1, raw, "", False), normalize=True)

        self.assertEqual(result.stdout, "")
        assert failure is not None
        self.assertIn("OAH_ENGINE_FAILED", failure)
        record = native.read_receiving(prepared.run.path)
        assert record is not None
        self.assertIn("available_usage", record)
        usage = record["available_usage"]
        self.assertEqual((usage["observed"]["input_tokens"], usage["observed"]["output_tokens"]), (7, 3))
        self.assertTrue(all(value is None for value in usage["totals"].values()))
        self.assertEqual(usage["availability"], "partial")

    def test_a_late_prepare_refusal_keeps_the_started_skill_judge_summary(self):
        from oah import call
        from tests.helpers import git_commit

        self.build_skills(fixture.PLAIN, fixture.ALPHA, fixture.BETA)
        server = FakeServer(self, fixture.SkillFitAdviceTest.answers("beta"))
        path = self.repo / ".oah" / "config.toml"
        git_commit(self.repo, {".oah/config.toml": path.read_text(encoding="utf-8") +
            '\n[rerank]\nenabled=true\nprovider="typesafe"\n[rerank.typesafe]\nurl=' + json.dumps(server.url()) +
            '\n[rerank.judgement]\nenabled=true\n'}, "judge fixture")
        self.env["TYPESAFE_API_KEY"] = fake_key()
        prior = {path: path.read_bytes() for path in (self.state_dir / "skills" / "attempts").glob("*.json")}
        for raw in prior.values():
            self.assertRegex(json.loads(raw)["preparation_digest"], r"^sha256:[0-9a-f]{64}$")
        with mock.patch.object(call, "_brief_skills", side_effect=call.EngineError(
                "OAH_SKILLS_BUDGET_EXCEEDED(brief)", "the fixture refuses at final selection")):
            with self.assertRaisesRegex(call.EngineError, "OAH_SKILLS_BUDGET_EXCEEDED\\(brief\\)"):
                self.prepare(no_rerank=False)
        requests = [row for row in server.requests if row["body"].get("state", {}).get("purpose") == "skill-fit"]
        self.assertEqual(len(requests), 1)
        summaries = list(set((self.state_dir / "skills" / "attempts").glob("*.json")) - prior.keys())
        self.assertEqual(len(summaries), 1)
        summary = json.loads(summaries[0].read_bytes())
        self.assertTrue(summary["preparation_digest"].startswith("sha256:"))
        self.assertEqual(summary["usage"], "unknown")
        self.assertEqual([(row["kind"], row["requests"]) for row in summary["requests"]], [("skill-judge", 1)])
        self.assertEqual(summary["requests"][0]["state"], "returned")
        self.assertNotIn("name: beta", summaries[0].read_text(encoding="utf-8"))
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())
        for path, data in prior.items():
            self.assertEqual(path.read_bytes(), data)

    def test_failed_stream_saves_available_usage_before_output_normalization(self):
        build = getattr(fixture, "build_reference_guide", fixture.ReferenceDeliveryTest.build_guide)
        build(self)
        script = ("import json,os,sys,time\n"
                  f"sys.path.insert(0,{str(fixture.FAKES)!r})\n"
                  "sys.dont_write_bytecode=True\nimport fake_claude,fakelib\n"
                  "argv=sys.argv[1:]\n"
                  "if 'stream-json' not in argv: sys.exit(fake_claude.main(argv))\n"
                  "fakelib.record(argv,fakelib.read_stdin())\n"
                  "events=[{'type':'system','subtype':'init','session_id':'s1','cwd':os.getcwd(),'tools':['Read'],"
                  "'model':'claude-test','permissionMode':'acceptEdits','claude_code_version':'2.1.318'},"
                  "{'type':'assistant','session_id':'s1','message':{'id':'m1','content':[],"
                  "'usage':{'input_tokens':7,'output_tokens':3}}}]\n"
                  "for event in events: print(json.dumps(event),flush=True)\n"
                  "print('{broken',flush=True)\ntime.sleep(30)\n")
        (self.work / "fake_stream_claude.py").write_text(script, encoding="utf-8")

        result = self.call()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        if os.name == "nt":
            self.assertIn("OAH_SKILL_REFERENCE_UNAVAILABLE", result.stderr)
            self.assertFalse(self.record.exists())
        else:
            saved = json.loads((self.runs()[-1] / "capabilities-receiving.json").read_bytes())
            self.assertEqual(saved["available_usage"]["observed"]["input_tokens"], 7)
            self.assertEqual(saved["available_usage"]["observed"]["output_tokens"], 3)
            self.assertIsNone(saved["available_usage"]["totals"]["input_tokens"])


if __name__ == "__main__":
    unittest.main()
