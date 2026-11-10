from __future__ import annotations

import collections
import contextlib
import io
import json
import os
import types
import unittest
from unittest import mock

from tests.helpers import git_commit
from tests.test_capabilities import DOCS
from tests.test_skill_delivery import DeliveryTask, FAKES, PLAIN, link_op, skill_md


class TaskAdmissionTest(DeliveryTask, unittest.TestCase):
    def find(self):
        from oah import cli

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["skills", "find", "--task", "t1", "--json"])
        return code, out.getvalue(), err.getvalue()

    def mysql_skill(self):
        def linked(record):
            return [{"op": "set-tech", "label": "database", "value": "mysql", "decision": None, "note": None},
                    link_op(record)]

        self.build_skills((*PLAIN[:3], linked))

    def test_complete_test_profile_task_find_sends_through_gateway(self):
        from oah.design.sources.web import WebResult
        from oah.discovery import gateway, providers

        self.mysql_skill()
        earlier = {p: p.read_bytes() for p in (self.state_dir / "skills/attempts").glob("*.json")}
        with mock.patch.object(providers, "GETTER", return_value=WebResult(200, "https://skills.sh", {},
                b'{"skills": []}')) as transport, mock.patch.object(gateway, "send", wraps=gateway.send) as gate:
            code, out, err = self.find()
        self.assertEqual(code, 0, out + err)
        self.assertEqual((transport.call_count, gate.call_count, json.loads(out)["requests"]), (1, 1, 1))
        paths = set((self.state_dir / "skills/attempts").glob("*.json")) - earlier.keys()
        self.assertEqual(len(paths), 1)
        summary = json.loads(paths.pop().read_text(encoding="utf-8"))
        self.assertEqual([(r["kind"], r["state"], r["requests"]) for r in summary["requests"]],
                         [("skill-search", "returned", 1)])
        self.assertRegex(summary["preparation_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual({p: p.read_bytes() for p in earlier}, earlier)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())

    def test_real_empty_registry_refuses_task_find_before_gateway_or_search(self):
        from oah.discovery import budget, gateway, providers, target_profiles

        self.mysql_skill()
        with mock.patch.object(budget, "_profile_source", return_value=budget.PinnedSource(
                target_profiles.DATA, budget.SOURCE_PIN)), mock.patch.object(providers, "GETTER") as transport, \
                mock.patch.object(gateway, "send", wraps=gateway.send) as gate:
            code, out, err = self.find()
        self.assertEqual(code, 1, out + err)
        self.assertIn("OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown)", err)
        self.assertEqual((transport.call_count, gate.call_count), (0, 0))
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())

    def test_required_unavailable_capability_refuses_task_find_before_search(self):
        from oah import cli
        from oah.design.sources.web import WebResult
        from oah.discovery import providers

        def linked(record):
            return [{"op": "set-tech", "label": "database", "value": "mysql", "decision": None, "note": None},
                    link_op(record)]

        self.build_skills((*DOCS[:3], linked))
        ordinary = self.call()
        earlier = {p: p.read_bytes() for p in (self.state_dir / "skills/attempts").glob("*.json")}
        sent = []

        def reply(url, **_options):
            sent.append(url)
            return WebResult(200, url, {"content-type": "application/json"}, b'{"skills": []}')

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(providers, "GETTER", reply), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["skills", "find", "--task", "t1", "--json"])

        self.assertEqual(ordinary.returncode, 1)
        self.assertIn("OAH_CAPABILITY_UNAVAILABLE(not-configured)", ordinary.stderr)
        self.assertEqual(sent, [])
        self.assertEqual(code, 1, out.getvalue() + err.getvalue())
        self.assertIn("OAH_CAPABILITY_UNAVAILABLE(not-configured)", err.getvalue())
        self.assertEqual({p: p.read_bytes() for p in earlier}, earlier)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())


class SkillOrderTest(DeliveryTask, unittest.TestCase):
    def order(self, *, change_before: bool):
        from oah import call, rerank

        self.build_skills(PLAIN)
        config = self.repo / ".oah/config.toml"
        additions = {f".claude/skills/{name}/SKILL.md": skill_md(name, "Use the local checklist.\n")
                     for name in ("test-driven-development", "verification-before-completion")}
        additions[".oah/config.toml"] = config.read_text(encoding="utf-8") + \
            '\n[rerank]\nenabled=true\nprovider="fake"\n'
        git_commit(self.repo, additions, "skill order fixture")
        self.env["FAKE_RERANK_KEY"] = "fixture-local-key"
        earlier = {p: p.read_bytes() for p in (self.state_dir / "skills/attempts").glob("*.json")}
        sent = []

        def change():
            config.write_text(config.read_text(encoding="utf-8") + "\n# fixture policy changed at order step\n",
                              encoding="utf-8")

        def score(kind, query, items, *, key, config):
            sent.append((kind, len(items)))
            if not change_before:
                change()
            return list(range(len(items)))

        provider = types.SimpleNamespace(name="fake", key_name=lambda _config: "FAKE_RERANK_KEY", score=score)
        real = call._brief_skills

        def at_order(*args, **kwargs):
            if change_before:
                change()
            return real(*args, **kwargs)

        with mock.patch.dict(rerank.PROVIDERS, {"fake": provider}), mock.patch.object(call, "_brief_skills", at_order):
            with self.assertRaisesRegex(call.EngineError, r"OAH_SKILL_NOT_DELIVERABLE\(changed\)"):
                self.prepare(no_rerank=False)
        self.assertEqual({p: p.read_bytes() for p in earlier}, earlier)
        self.assertFalse(self.record.exists())
        paths = set((self.state_dir / "skills/attempts").glob("*.json")) - earlier.keys()
        return sent, [json.loads(p.read_text(encoding="utf-8")) for p in paths]

    def test_policy_change_before_skill_order_sends_nothing_and_keeps_earlier_records(self):
        sent, summaries = self.order(change_before=True)
        self.assertEqual(sent, [])
        self.assertEqual(summaries, [])
        self.assertEqual(self.runs(), [])

    def test_started_skill_order_keeps_its_summary_after_late_refusal(self):
        sent, summaries = self.order(change_before=False)
        self.assertEqual(sent, [("skills", 2)])
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertRegex(summary["preparation_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(summary["usage"], "unknown")
        self.assertEqual([(r["kind"], r["state"], r["requests"]) for r in summary["requests"]],
                         [("skill-order", "returned", 1)])
        self.assertFalse(summary["counted"])
        self.assertEqual(self.runs(), [])


class ReceivingFailureTest(DeliveryTask, unittest.TestCase):
    def test_first_receiving_write_failure_keeps_known_usage_before_normalization(self):
        from oah import call
        from oah.engines import claude

        self.build_skills(PLAIN)
        script = ("import json,os,sys,time\n"
            f"sys.path.insert(0,{str(FAKES)!r})\n"
            "sys.dont_write_bytecode=True\nimport fake_claude,fakelib\n"
            "if 'stream-json' not in sys.argv: sys.exit(fake_claude.main(sys.argv[1:]))\n"
            "fakelib.record(sys.argv[1:],fakelib.read_stdin())\n"
            "events=[{'type':'system','subtype':'init','session_id':'s1','cwd':os.getcwd(),'tools':['Read'],"
            "'model':'claude-test','permissionMode':'acceptEdits','claude_code_version':'2.1.318'},"
            "{'type':'assistant','session_id':'s1','message':{'id':'m1','content':[],"
            "'usage':{'input_tokens':7,'output_tokens':3}}}]\n"
            "for event in events: print(json.dumps(event),flush=True)\n"
            "print('{broken',flush=True)\ntime.sleep(10)\n")
        (self.work / "fake_stream_claude.py").write_text(script, encoding="utf-8")
        command = claude.build_command

        def stream_command(*args, **options):
            argv = command(*args, **options)
            at = argv.index("--output-format")
            argv[at + 1] = "stream-json"
            argv.insert(at + 2, "--verbose")
            return argv

        with mock.patch.object(claude, "build_command", stream_command):
            prepared = self.prepare()
        bundle = self.bundle(prepared.run.path)
        assert bundle is not None
        self.assertEqual(bundle["budget"]["reference_bytes"], 0)
        earlier = prepared.run.file_path("skills.json").read_bytes()
        real = call._conclude_observation

        def block_sidecar(prepared, sink):
            prepared.run.file_path("capabilities-receiving.json").mkdir()
            return real(prepared, sink)

        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(call, "_streamed_skill_bundle", return_value=bundle), \
                mock.patch.object(call, "_conclude_observation", block_sidecar):
            result = call.execute(prepared, base_env=self.env)

        self.assertEqual(result["status"], "failed")
        self.assertIn("OAH_CAPABILITY_RECEIVING_FAILED", result["error"])
        record = json.loads(prepared.run.file_path("skills-receiving.json").read_text(encoding="utf-8"))
        self.assertIn("available_usage", record)
        usage = record["available_usage"]
        self.assertEqual((usage["observed"]["input_tokens"], usage["observed"]["output_tokens"]), (7, 3))
        self.assertIsNone(usage["totals"]["input_tokens"])
        self.assertIsNone(usage["totals"]["output_tokens"])
        self.assertEqual(usage["availability"], "partial")
        from oah.launchers import native

        self.assertEqual(native.read_receiving(prepared.run.path), record)
        self.assertEqual(prepared.run.file_path("skills.json").read_bytes(), earlier)


class OrdinaryReachabilityTest(unittest.TestCase):
    def test_unavailable_new_advice_keeps_prompt_judges_and_one_reservation(self):
        from oah import call, call_cli
        from oah.review import receipts
        from oah.secrets import NoKeychain
        from tests.test_call import Project

        project = Project(self, config="\n[models]\nfetch_scores=false\n[rerank]\nenabled=true\n"
                          "[rerank.judgement]\nenabled=false\nsend_task_text=false\n")
        project.bind()
        tools = call_cli.intent_tools()
        observations = []
        config = project.repo / ".oah/config.toml"
        for enabled in (False, True):
            if enabled:
                config.write_text(config.read_text(encoding="utf-8").replace("enabled=false", "enabled=true"),
                                  encoding="utf-8")
            counts = collections.Counter()
            before = receipts.read_round_log(project.state, "t1")
            with contextlib.ExitStack() as held:
                held.enter_context(mock.patch.dict(os.environ, project.env, clear=True))
                held.enter_context(contextlib.chdir(project.repo))
                for name in ("judge_difficulty", "judge_engine", "_task_dispatch_gate"):
                    real = getattr(call, name)

                    def counted(*args, name=name, real=real, **kwargs):
                        label = (name + (":reserve" if kwargs.get("reserve") else ":check")
                                 if name == "_task_dispatch_gate" else name)
                        counts[label] += 1
                        return real(*args, **kwargs)

                    held.enter_context(mock.patch.object(call, name, counted))
                error = None
                prepared = None
                try:
                    prepared = call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low"),
                        project.settings(), root=project.repo, state_dir=project.state, env=project.env,
                        footer=tools.footer, has_footer=tools.has_footer, keychain=NoKeychain())
                except call.EngineError as exc:
                    error = str(exc)
                self.assertIsNone(error)
                assert prepared is not None
            prompt = prepared.run.prompt_path.read_bytes().replace(prepared.run_id.encode("utf-8"), b"<RUN>")
            observations.append((prompt, dict(counts), [r["event"] for r in
                                 receipts.read_round_log(project.state, "t1")[len(before):]]))
            self.assertIsNone(prepared.e_digest)
        self.assertEqual(observations[1], observations[0])
        self.assertEqual(observations[0][1], {"_task_dispatch_gate:check": 4,
                                            "_task_dispatch_gate:reserve": 1, "judge_engine": 1})
        self.assertEqual(observations[0][2], ["reservation"])
