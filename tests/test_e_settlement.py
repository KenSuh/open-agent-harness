# E 정산 회귀 시험 — 그대로인 선택은 한 번 정산되고 수정 작업에는 검증한 선택의 닫힌 증거가 이어진다.
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import unittest
from unittest import mock

from tests import test_capabilities as capability_fixture
from tests import test_fix as fix_fixture
from tests import test_skill_delivery as delivery_fixture
from tests import test_task_discovery as task_fixture
from tests.test_skill_delivery import DeliveryTask


class NativeSelectionTest(task_fixture.TaskRepo, unittest.TestCase):
    def test_unchanged_native_task_selection_settles_once_and_returns_the_same_receipt(self):
        from oah import init

        self.upstream("acme/db", task_fixture.mit_repo({"skills/mysql-migrate": task_fixture.skill_md(
            "mysql-migrate", task_fixture.MIGRATE_BODY)}))
        self.server.hits("mysql migrations constraints", ("mysql-migrate", "acme/db"))
        self.start_task()
        agents = self.home / ".claude" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "oah-low.md").write_bytes(init.agent_text("low", "haiku", "low"))
        self.find_task("--subtask", "migrate", "--tech", "database=mysql", out="needs.json")
        reviewed, _ = self.review_json("--github", "acme/db", "--path", "skills/mysql-migrate")
        assert reviewed is not None
        self.select("needs.json", reviewed["id"])
        code, out, err = self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low",
            "--launcher", "native", "--skill-needs", str(self.files / "needs.json"), "--json")
        self.assertEqual(code, 0, out + err)
        offered = json.loads(out)
        run = self.state_dir / "runs" / "t1" / offered["run"]
        reply = self.files / "reply.txt"
        reply.write_text("The selected MySQL migration skill was received.\n", encoding="utf-8")
        command = ("call", "settle", "--run", offered["run"], "--offer-digest", offered["message"]["sha256"],
            "--host-child-id", "child-1", "--outcome", "succeeded", "--result-file", str(reply), "--json")

        first = self.oah(*command)

        self.assertEqual(first[0], 0, first[1] + first[2])
        path = run / "native-settlement.json"
        before = path.read_bytes()
        again = self.oah(*command)
        self.assertEqual(again[0], 0, again[1] + again[2])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list(run.glob("native-settlement*.json"))), 1)
        self.assertFalse(self.record.exists())


class FrozenCorrectionTest(DeliveryTask, unittest.TestCase):
    def test_ordinary_non_design_selection_reaches_fix_only_as_closed_saved_evidence(self):
        from oah import call, call_cli
        from oah.discovery import policy_digest, providers, tasks
        from oah.secrets import NoKeychain

        project = fix_fixture.FixProject(self, config="\n[models]\nfetch_scores = false\n")
        body = "Prepare the ledger rows using the selected migration checklist.\n"
        self.upstream("acme/ledger", {"LICENSE": delivery_fixture.MIT_LICENSE,
            "skills/ledger/SKILL.md": delivery_fixture.skill_md("ledger", body)})
        reviewed = self.reviewed(project.state, "acme/ledger", "skills/ledger")
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo):
            packet = call.read_packet(project.repo, project.repo / ".oah/tasks/t1/PACKET.md",
                                      parse_intent=tools.parse, read_source=tools.read_source)
            task_design, _ = call._task_design(project.lease, packet, state_dir=project.state, stage="initial",
                                                task="t1", engine="codex")
            basis = call.task_basis(project.lease, task_design, packet, root=project.repo)
            policy = policy_digest(providers.settings_of({}))
            need = tasks.need(basis, dispatch_stage="initial", policy=policy, required=True)
            tasks.select(project.state, need, [reviewed["id"]], basis=basis)
            needs = project.work / "needs.json"
            needs.write_text(json.dumps({"version": 1, "task": "t1", "needs": [need]}), encoding="utf-8")
            ordinary = call.prepare(call.CallRequest(engine="codex", stage="initial", task="t1", tier="high",
                override_stage=True, skill_needs=str(needs), no_rerank=True), call_cli.load_settings(project.repo),
                root=project.repo, state_dir=project.state, env=project.env, footer=tools.footer,
                has_footer=tools.has_footer, keychain=NoKeychain())
            ordinary_result = call.execute(ordinary, base_env=project.env, rules=fix_fixture.RULES)
            self.assertEqual(ordinary_result["status"], "finished", ordinary_result)
            self.assertIn(body, project.fix_calls()[0]["prompt"])
            project.changes_requested()
            project.queue_fix({}, {"write": {"src/app.py": "print('corrected')\n"}})

            corrected = project.fix()

        self.assertEqual(corrected.code, "OAH_FIX_APPLIED", corrected.message)
        self.assertIn(body, project.fix_calls()[-1]["prompt"])
        records = [path for path in (project.state / "runs" / "t1").glob("*/skills.json")
                   if "identity_digest" in json.loads(path.read_bytes()).get("prepared_e", {})]
        self.assertEqual(len(records), 1)
        record = records[0]
        saved = json.loads(record.read_bytes())["prepared_e"]
        self.assertNotIn("facts", saved)
        self.assertNotIn("references", saved)
        self.assertEqual(saved["counts"]["needs"], 1)
        self.assertEqual(saved["counts"]["selections"], 1)
        self.assertTrue(saved["payloads"])
        for path in record.parent.rglob("*"):
            if path.is_file():
                self.assertNotIn(body.encode("utf-8"), path.read_bytes(), str(path))


class InventorySettlementTest(DeliveryTask, unittest.TestCase):
    def test_current_parent_approval_change_refuses_before_launch_and_keeps_the_prepared_record(self):
        from oah import call, call_cli
        from oah.capabilities import inventory
        from oah.secrets import NoKeychain

        self.build_skills(capability_fixture.DOCS)
        capability_fixture.claude_home(self.work / "home", self.repo)
        now = dt.datetime.now(dt.timezone.utc)
        entry = inventory.parent_entry("mcp:docs/lookup", schema=capability_fixture.SCHEMA, observed=now,
                                        effects=["read-network"])
        lead = capability_fixture.write(self.work / "lead.json", inventory.parent_inventory([entry], observed=now))
        planned = self.oah("capabilities", "plan", "--task", "t1", "--stage", "initial",
                           "--parent-inventory", str(lead), "--json")
        (request,) = json.loads(planned.stdout)["requests"]
        from oah.capabilities import plan

        stored, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"),
            invocation={"outcome": "ok", "at": request["created_at"], "arguments_digest": "sha256:" + "3" * 64},
            now=now)
        relay = stored["id"]
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
            prepared = call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                no_rerank=True, parent_inventory=str(lead), relay_results=(relay,)), call_cli.load_settings(self.repo),
                root=self.repo, state_dir=self.state_dir, env=self.env, footer=tools.footer,
                has_footer=tools.has_footer, keychain=NoKeychain())
            call.check_prepared(prepared)
            before = (prepared.run.path / "skills.json").read_bytes()
            original = lead.read_bytes()
            changes = (("approval", None, "denied"), ("schema", "sha256", "sha256:" + "9" * 64),
                       ("source", "host_session_digest", "sha256:" + "8" * 64),
                       ("permission", "known", False), ("source", "expires_at", "2020-01-01T00:00:00Z"))
            for field, child, value in changes:
                parent = json.loads(original)
                if child is None:
                    parent["entries"][0][field] = value
                else:
                    parent["entries"][0][field][child] = value
                parent["entries"][0]["entry_digest"] = inventory.entry_digest(parent["entries"][0])
                parent["digest"] = inventory.inventory_digest(parent)
                lead.write_text(json.dumps(parent), encoding="utf-8")
                with self.subTest(field=field, child=child), \
                        self.assertRaisesRegex(call.EngineError, "OAH_SKILL_NOT_DELIVERABLE\\(changed\\)"):
                    call.check_prepared(prepared)
                self.assertEqual((prepared.run.path / "skills.json").read_bytes(), before)
                self.assertFalse(self.record.exists())
                lead.write_bytes(original)

        self.assertEqual((prepared.run.path / "skills.json").read_bytes(), before)
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
