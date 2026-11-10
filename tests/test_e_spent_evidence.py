# 지출 후 근거 회귀 — 정산은 제안의 창을 쓰고 새 수정은 도구 근거를 다시 요구하는지 확인한다.
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import unittest
from unittest import mock

from tests import e_c6_fixture, test_capabilities as capability_fixture
from tests.test_skill_delivery import DeliveryTask


def clock(at):
    class Later(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz)
    return Later


class SpentEvidenceTest(DeliveryTask, unittest.TestCase):
    def offered_parent_relay(self):
        from oah import init
        from oah.capabilities import inventory, plan

        self.build_skills(capability_fixture.DOCS)
        capability_fixture.claude_home(self.work / "home", self.repo)
        folder = self.work / "home/.claude/agents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "oah-low.md").write_bytes(init.agent_text("low", "haiku", "low"))
        now = dt.datetime.now(dt.timezone.utc)
        entry = inventory.parent_entry("mcp:docs/lookup", schema=capability_fixture.SCHEMA, observed=now,
                                       effects=["read-network"])
        lead = capability_fixture.write(self.work / "lead.json", inventory.parent_inventory([entry], observed=now))
        planned = self.oah("capabilities", "plan", "--task", "t1", "--stage", "initial", "--engine", "claude",
                           "--parent-inventory", str(lead), "--json")
        self.assertEqual(planned.returncode, 1, planned.stdout + planned.stderr)
        self.assertIn("OAH_CAPABILITY_RELAY_REQUIRED", planned.stderr)
        request, = json.loads(planned.stdout)["requests"]
        stored, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        offer = self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low", "--launcher",
                         "native", "--parent-inventory", str(lead), "--relay-result", stored["id"], "--json")
        self.assertEqual(offer.returncode, 0, offer.stdout + offer.stderr)
        offered = json.loads(offer.stdout)
        run = self.state_dir / "runs/t1" / offered["run"]
        reply = self.work / "reply.txt"
        reply.write_text("Received the documentation result.\n", encoding="utf-8")
        command = ["call", "settle", "--run", run.name, "--offer-digest", offered["message"]["sha256"],
                   "--host-child-id", "child-one", "--outcome", "succeeded", "--result-file", str(reply), "--json"]
        return now, run, command

    def settle_at(self, command, at):
        from oah import call, cli

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(call.dt, "datetime", clock(at)), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(command)
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())

    def test_first_settle_with_parent_and_relay_after_seven_minutes_uses_offer_window(self):
        now, run, command = self.offered_parent_relay()
        earlier = {name: (run / name).read_bytes() for name in ("skills.json", "capabilities.json", "prompt.md")}
        self.settle_at(command, now + dt.timedelta(minutes=7))
        self.assertEqual({name: (run / name).read_bytes() for name in earlier}, earlier)
        self.assertFalse(self.record.exists())

    def test_idempotent_settle_with_parent_and_relay_after_seven_minutes_keeps_first_receipt(self):
        now, run, command = self.offered_parent_relay()
        self.settle_at(command, now + dt.timedelta(minutes=2))
        before = (run / "native-settlement.json").read_bytes()
        self.settle_at(command, now + dt.timedelta(minutes=7))
        self.assertEqual((run / "native-settlement.json").read_bytes(), before)
        self.assertFalse(self.record.exists())

    def selected_parent(self):
        from oah import call
        from oah.capabilities import inventory
        from tests.helpers import make_tmp_dir

        now = dt.datetime.now(dt.timezone.utc)
        parent = capability_fixture.write(make_tmp_dir(self) / "lead.json", inventory.parent_inventory([], observed=now))
        real = call.prepare

        def prepare(request, *args, **options):
            if request.stage == "initial":
                request = dataclasses.replace(request, parent_inventory=str(parent))
            return real(request, *args, **options)

        with mock.patch.object(call, "prepare", prepare):
            project, ordinary, _need, _basis = e_c6_fixture.selected_ordinary(self)
        return project, ordinary, now, parent

    def test_correction_after_seven_minutes_requires_the_parent_tools_to_be_read_again(self):
        from oah import call
        from tests.test_fix import ReviewError

        project, ordinary, now, _parent = self.selected_parent()
        before = ordinary.run.file_path("skills.json").read_bytes()
        calls = project.fix_calls()
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                mock.patch.object(call.dt, "datetime", clock(now + dt.timedelta(minutes=7))), \
                self.assertRaises(ReviewError) as caught:
            project.fix()
        self.assertEqual(caught.exception.code, "OAH_CAPABILITY_INVENTORY_UNKNOWN")
        self.assertIn("read the tools again", str(caught.exception))
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(ordinary.run.file_path("skills.json").read_bytes(), before)

    def test_correction_with_refreshed_parent_evidence_keeps_the_frozen_selection(self):
        from oah import call
        from oah.capabilities import inventory

        project, ordinary, now, parent = self.selected_parent()
        before = ordinary.run.file_path("skills.json").read_bytes()
        later = now + dt.timedelta(minutes=7)
        capability_fixture.write(parent, inventory.parent_inventory([], observed=later))
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                mock.patch.object(call.dt, "datetime", clock(later)):
            result = project.fix()
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        self.assertIn("Prepare the selected ledger rows.", project.fix_calls()[-1]["prompt"])
        self.assertEqual(ordinary.run.file_path("skills.json").read_bytes(), before)
