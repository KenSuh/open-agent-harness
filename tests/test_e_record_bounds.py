# 기록 경계 회귀 — 이전 준비의 잘못된 값과 사용량 합계 및 수신 대체 파일을 닫힌 형식으로 처리하는지 확인한다.
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from . import e_c6_fixture
from tests.test_skill_delivery import DeliveryTask, PLAIN, FAKES, build_reference_guide


class EarlierRecordTest(DeliveryTask, unittest.TestCase):
    def test_B6_malformed_earlier_records_are_typed_refusals_and_keep_the_original_bytes(self):
        from oah import call, cli

        project, ordinary, _need, _basis = e_c6_fixture.selected_ordinary(self)
        skills = ordinary.run.file_path("skills.json")
        result = ordinary.run.file_path("result.json")
        originals = {path: path.read_bytes() for path in (skills, result)}
        for name in ("prepared_e", "prepared_type", "skills_type", "result"):
            with self.subTest(record=name):
                if name in ("prepared_e", "prepared_type"):
                    changed = json.loads(originals[skills])
                    if name == "prepared_e":
                        changed["prepared_e"]["digest"] = "sha256:" + "0" * 64
                    else:
                        changed["prepared_e"] = []
                    skills.write_text(json.dumps(changed), encoding="utf-8")
                elif name == "skills_type":
                    skills.write_text("[]", encoding="utf-8")
                else:
                    result.write_text("[]", encoding="utf-8")
                kept = {path: path.read_bytes() for path in originals}
                inputs = mock.Mock(state=project.state)
                inputs.holder.return_value = project.lease
                try:
                    with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                            self.assertRaises(call.EngineError) as caught:
                        call._freeze_e(inputs, env=project.env)
                    self.assertEqual(caught.exception.code, "OAH_FIX_STATE_CHANGED")
                    out, err = io.StringIO(), io.StringIO()
                    with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = cli.main(["fix", "--task", "t1", "--tier", "high", "--json"])
                    self.assertEqual(code, 1, out.getvalue() + err.getvalue())
                    self.assertTrue(err.getvalue().startswith("OAH_"), err.getvalue())
                    self.assertNotIn("Traceback", err.getvalue())
                    self.assertEqual({path: path.read_bytes() for path in kept}, kept)
                    self.assertEqual(len(project.fix_calls()), 1)
                finally:
                    for path, data in originals.items():
                        path.write_bytes(data)


class ReceivingBoundsTest(DeliveryTask, unittest.TestCase):
    def test_B7_oversized_usage_becomes_unknown_and_the_child_receiving_record_is_written(self):
        from oah import call
        from oah.capabilities.usage import valid_summary
        from oah.launchers import native

        if os.name == "nt":
            self.build_skills(PLAIN)
        else:
            build_reference_guide(self)
        script = ("import json,os,sys\n"
            f"sys.path.insert(0,{str(FAKES)!r})\n"
            "import fake_claude,fakelib\n"
            "if 'stream-json' not in sys.argv: sys.exit(fake_claude.main(sys.argv[1:]))\n"
            "fakelib.record(sys.argv[1:],fakelib.read_stdin())\n"
            "events=[{'type':'system','subtype':'init','session_id':'s1','cwd':os.getcwd(),'tools':['Read'],"
            "'model':'claude-test','permissionMode':'acceptEdits','claude_code_version':'2.1.318'}]\n"
            "for i in range(2): events.append({'type':'assistant','session_id':'s1','message':{'id':str(i),"
            "'content':[],'usage':{'input_tokens':10**15,'output_tokens':1,'cost_usd':10**12}}})\n"
            "events.append({'type':'result','subtype':'success','is_error':False,'result':'done','session_id':'s1',"
            "'modelUsage':{'a':{'inputTokens':10**15,'outputTokens':1,'costUSD':10**12},"
            "'b':{'inputTokens':10**15,'outputTokens':1,'costUSD':10**12}}})\n"
            "for event in events: print(json.dumps(event),flush=True)\n")
        (self.work / "fake_stream_claude.py").write_text(script, encoding="utf-8")

        if os.name == "nt":
            from oah import skillpack
            from oah.engines.proc import RunResult

            prepared = self.prepare()
            argv = list(prepared.argv)
            argv.insert(1, "-B")
            argv[argv.index("--output-format") + 1] = "stream-json"
            child = subprocess.run(argv, cwd=self.repo, input=prepared.run.prompt_path.read_text(encoding="utf-8"),
                env=dict(self.env, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"), capture_output=True,
                text=True, encoding="utf-8", timeout=300)
            bundle = skillpack.bundle_of(prepared.run.path)
            assert bundle is not None
            call._conclude_skill_reads(prepared, bundle, child.stdout,
                RunResult(child.returncode, child.stdout, child.stderr, False), normalize=True)
            result = child
        else:
            result = self.call()

        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(self.record.exists())
        run = self.runs()[-1]
        self.assertNotEqual(json.loads((run / "result.json").read_bytes())["status"], "running")
        record = native.read_receiving(run)
        assert record is not None
        usage = record["available_usage"]
        self.assertTrue(valid_summary(usage))
        self.assertIsNone(usage["totals"]["input_tokens"])
        self.assertIsNone(usage["observed"]["input_tokens"])
        self.assertIsNone(usage["observed"]["cost_usd"])
        self.assertEqual(usage["observed"]["output_tokens"], 2)

    def test_B8_a_receiving_fallback_is_a_valid_receiving_record_or_is_absent(self):
        from oah.capabilities.inventory import EventSink
        from oah.discovery import gateway
        from oah.launchers import native

        self.build_skills(PLAIN)
        prepared = self.prepare()
        sink = EventSink(run=prepared.run_id, cwd=prepared.cwd, cli_version="2.1.318", permission_mode="acceptEdits",
                         planned={}, bundle_id=None, timeout_s=60)
        prepared.run.file_path("capabilities-receiving.json").mkdir()

        failure = gateway.receive(prepared.run, sink.finish(None, False))

        self.assertIsNotNone(failure)
        record = native.read_receiving(prepared.run.path)
        if record is not None:
            self.assertEqual(set(record), {*native.RECEIVING_KEYS, "available_usage"})
            self.assertEqual(record["run"], prepared.run_id)
            self.assertEqual(record["delivery"], "failed")
