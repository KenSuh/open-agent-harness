from __future__ import annotations

import datetime as dt
import json
import unittest

from tests import test_capabilities as capabilities
from tests.test_skill_delivery import DeliveryTask


class RelayTimesTest(DeliveryTask, unittest.TestCase):
    def request(self):
        from oah import init
        from oah.capabilities import inventory

        self.build_skills(capabilities.DOCS)
        capabilities.claude_home(self.work / "home", self.repo)
        agents = self.work / "home/.claude/agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "oah-low.md").write_bytes(init.agent_text("low", "haiku", "low"))
        now = dt.datetime.now(dt.timezone.utc)
        entry = inventory.parent_entry("mcp:docs/lookup", schema=capabilities.SCHEMA, observed=now,
                                       effects=["read-network"])
        parent = capabilities.write(self.work / "parent.json", inventory.parent_inventory([entry], observed=now))
        result = self.oah("capabilities", "plan", "--task", "t1", "--stage", "initial", "--engine", "claude",
                          "--parent-inventory", str(parent), "--json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        request, = json.loads(result.stdout)["requests"]
        return request, parent, now

    def ingest_refusal(self, bad_time):
        request, _parent, _now = self.request()
        path = capabilities.write(self.work / "request.json", request)
        result = self.work / "result.txt"
        result.write_text(capabilities.RESULT, encoding="utf-8")
        invocation = capabilities.write(self.work / "invocation.json", {"outcome": "ok", "at": bad_time,
                                                                      "arguments_digest": "sha256:" + "3" * 64})
        before = {p: p.read_bytes() for name in ("relays", "relay-payloads")
                  for p in (self.state_dir / "skills" / name).glob("*.json")}

        refused = self.oah("capabilities", "relay", "--request", str(path), "--capability", request["capability_id"],
                           "--result-file", str(result), "--invocation", str(invocation), "--json")

        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("OAH_CAPABILITY_RELAY_REQUIRED(invalid)", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual({p: p.read_bytes() for name in ("relays", "relay-payloads")
                          for p in (self.state_dir / "skills" / name).glob("*.json")}, before)
        self.assertFalse(self.record.exists())

    def test_non_iso_invocation_time_refuses_at_relay_cli_without_storing(self):
        self.ingest_refusal("not-an-ISO-time")

    def test_naive_invocation_time_refuses_at_relay_cli_without_storing(self):
        self.ingest_refusal("2026-11-10T10:00:00")

    def stored_refusal(self, *, native):
        from oah.capabilities import plan
        from oah.discovery import store

        request, parent, now = self.request()
        record, _word = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capabilities.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        for bad_time in ("not-an-ISO-time", "2026-11-10T10:00:00"):
            with self.subTest(time=bad_time, native=native):
                changed = json.loads(json.dumps(record))
                changed["invocation"]["at"] = bad_time
                changed = store.with_id(changed)
                path = self.state_dir / "skills/relays" / (changed["id"][7:] + ".json")
                path.write_text(json.dumps(changed), encoding="utf-8")
                before = path.read_bytes()
                command = ["call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low",
                           "--parent-inventory", str(parent), "--relay-result", changed["id"], "--json"]
                if native:
                    command += ["--launcher", "native"]

                refused = self.oah(*command)

                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn("OAH_CAPABILITY_RELAY_REQUIRED(invalid)", refused.stderr)
                self.assertNotIn("Traceback", refused.stderr)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.runs(), [])
                self.assertFalse(self.record.exists())

    def test_stored_invalid_invocation_times_refuse_ordinary_call_without_traceback(self):
        self.stored_refusal(native=False)

    def test_stored_invalid_invocation_times_refuse_native_offer_without_traceback(self):
        self.stored_refusal(native=True)
