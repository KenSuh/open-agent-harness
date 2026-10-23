# Stop 훅 검사 — 리스가 없을 때의 unlazy 인계, 미충족·낡은 바인딩에서의 차단 JSON, 충족 시 허용, 진행 없음 해제, 내부 오류 허용을 본다.
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

from oah import hook_stop, unlazy as unlazy_mod
from oah.gates import check as check_mod
from oah.gates.bindings import BindingStore
from oah.lease import Store
from oah.paths import repo_info
from tests.fixtures.gates.support import StubNode, make_unlazy_dir, met_ledger, run_cli, stub_pin
from tests.helpers import git_commit, make_git_repo, make_tmp_dir

RUNNABLE = """# Gates: t1

- [ ] G1: the module exists
  CHECK: oah check exists src/a.py
  EXPECT: OAH_CHECK_OK
  EVIDENCE: pending
"""
ALL_MET = {"stdout": "GATES.md: 1 gates\nALL MET (1 met)\n", "code": 0}
UNMET = {"stdout": "  UNMET GATES:G1 (unchecked): x\nGATES.md: 1 gates\nUNMET: 1 (met: 0)\n  GATES:G1\n", "code": 1}


class StopHookTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n"})
        self.tmp = make_tmp_dir(self)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.stub = StubNode(self.tmp / "stub")
        self.unlazy = make_unlazy_dir(self.tmp / "unlazy")
        self.state = self.tmp / "state"
        self.ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"

    def env(self, unlazy=True):
        return self.stub.env(OAH_UNLAZY_DIR=str(self.unlazy if unlazy else self.tmp / "absent"),
                             OAH_STATE_DIR=str(self.state))

    def decide(self, unlazy=True, session="s1", **options):
        payload = {"session_id": session, "cwd": str(self.repo), "hook_event_name": "Stop", "stop_hook_active": False}
        with patch.object(unlazy_mod, "PIN", stub_pin(self.unlazy)):
            return hook_stop.decide(payload, env=self.env(unlazy), home=self.home, **options)

    def lease(self, worktree=None):
        info = repo_info(worktree or self.repo)
        Store(self.state).start(task="t1", agent="claude", scope=("src",), note="", ttl_hours=1, info=info)

    def write_ledger(self, text):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(text, encoding="utf-8")

    def check(self):
        return check_mod.run_check(self.repo, self.ledger, BindingStore(self.state, "t1"), unlazy_dir=self.unlazy,
                                   node=self.stub.node, env=self.env())

    def test_no_lease_and_no_unlazy_allows(self):
        self.assertIsNone(self.decide(unlazy=False))

    def test_no_lease_hands_over_to_unlazy(self):
        block = {"decision": "block", "reason": "unlazy: 1 gate/ledger/dispatch item(s) need work"}
        self.stub.reply("stop-hook.mjs", {"stdout": json.dumps(block) + "\n", "code": 0})
        self.assertEqual(self.decide(), block)
        call = self.stub.calls("stop-hook.mjs")[-1]
        self.assertEqual(json.loads(call["stdin"])["session_id"], "s1")

    def test_relative_handover_from_subdirectory_runs_verified_copy(self):
        subdir = self.repo / "subdir"
        subdir.mkdir()
        verified = make_unlazy_dir(subdir / "unlazy")
        git_commit(verified, {"scripts/stop-hook.mjs":
                            'print(\'{"systemMessage": "verified copy"}\')\n'})
        attacker = self.repo / "unlazy" / "scripts"
        attacker.mkdir(parents=True)
        (attacker / "gate-check.mjs").write_text("", encoding="utf-8")
        (attacker / "stop-hook.mjs").write_text(
            'print(\'{"systemMessage": "unverified copy"}\')\n', encoding="utf-8")
        env = dict(self.env(), OAH_UNLAZY_DIR="unlazy", OAH_NODE=json.dumps([sys.executable]))
        result = run_cli(["hook", "stop"], cwd=subdir, env=env, pin=stub_pin(verified),
                         stdin=json.dumps({"cwd": str(subdir), "session_id": "relative"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"systemMessage": "verified copy"})

    def test_no_lease_skips_handover_when_unlazy_hook_is_installed(self):
        settings = self.repo / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "node \"/opt/skills/unlazy/scripts/stop-hook.mjs\" --unlazy-hook-v2"}]}]}}),
            encoding="utf-8")
        self.assertIsNone(self.decide())
        self.assertEqual(self.stub.calls("stop-hook.mjs"), [])

    def test_unpinned_unlazy_is_not_run_on_hand_over(self):
        payload = {"session_id": "s1", "cwd": str(self.repo), "hook_event_name": "Stop"}
        decision = hook_stop.decide(payload, env=self.env(), home=self.home)  # the real pin
        self.assertIn("OAH_UNLAZY_UNPINNED", decision["systemMessage"])
        self.assertNotIn("decision", decision)
        self.assertEqual(self.stub.calls("stop-hook.mjs"), [])

    def test_handover_output_that_is_not_a_decision_is_ignored(self):
        self.stub.reply("stop-hook.mjs", {"stdout": "garbage\n", "code": 0})
        self.assertIsNone(self.decide())

    def test_lease_without_ledger_allows(self):
        self.lease()
        self.assertIsNone(self.decide())

    def test_unmet_ledger_blocks_with_json(self):
        self.lease()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", UNMET)
        decision = self.decide()
        self.assertEqual(decision["decision"], "block")
        self.assertIn("G1", decision["reason"])
        self.assertIn("oah gates check t1", decision["reason"])
        self.assertEqual(set(decision), {"decision", "reason"})
        self.assertEqual(self.stub.calls("stop-hook.mjs"), [])

    def test_met_and_fresh_allows(self):
        self.lease()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", {"write": {str(self.ledger): met_ledger(RUNNABLE)},
                                           "stdout": "  PASS GATES:G1: x\nALL MET (1 met)\n", "code": 0}, ALL_MET)
        self.assertTrue(self.check().ok)
        self.assertIsNone(self.decide())

    def test_stale_binding_blocks(self):
        self.lease()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", {"write": {str(self.ledger): met_ledger(RUNNABLE)},
                                           "stdout": "  PASS GATES:G1: x\nALL MET (1 met)\n", "code": 0}, ALL_MET)
        self.check()
        (self.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
        decision = self.decide()
        self.assertEqual(decision["decision"], "block")
        self.assertIn("G1 (stale)", decision["reason"])

    def test_handoff_allows_with_message(self):
        self.lease()
        text = RUNNABLE + "\n- [ ] G2: owner signs off\n  EVIDENCE: pending\n\nABANDON: G2 owner away\n"
        self.write_ledger(text)
        self.stub.reply("gate-check.mjs", {"write": {str(self.ledger): met_ledger(text)},
                                           "stdout": "  PASS GATES:G1: x\nHANDOFF REQUIRED: 1 abandoned (met: 1)\n",
                                           "code": 1},
                        {"stdout": "HANDOFF REQUIRED: 1 abandoned (met: 1)\n  GATES:G2\n", "code": 1})
        self.check()
        decision = self.decide()
        self.assertNotIn("decision", decision)
        self.assertIn("HANDOFF REQUIRED", decision["systemMessage"])
        self.assertIn("G2", decision["systemMessage"])
        self.assertNotIn("owner away", decision["systemMessage"])

    def test_structural_only(self):
        self.lease()
        self.write_ledger(RUNNABLE)
        self.assertEqual(self.decide(unlazy=False)["decision"], "block")
        self.write_ledger(met_ledger(RUNNABLE))
        decision = self.decide(unlazy=False)
        self.assertNotIn("decision", decision)
        self.assertIn("structural only", decision["systemMessage"])

    def test_progress_guard_releases(self):
        self.lease()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", UNMET)
        decisions = [self.decide(max_blocks=3) for _ in range(4)]
        self.assertEqual([d.get("decision") for d in decisions], ["block"] * 3 + [None])
        self.assertIn("releasing after 3 blocks without progress", decisions[-1]["systemMessage"])
        self.assertEqual(self.decide(session="s2", max_blocks=3)["decision"], "block")  # counters are per session
        self.write_ledger(RUNNABLE + "\n- [ ] G2: another outcome\n  EVIDENCE: pending\n")  # progress resets
        self.assertEqual(self.decide(max_blocks=3)["decision"], "block")

    def test_config_sets_stop_max_blocks(self):
        git_commit(self.repo, {".oah/config.toml": "[gates]\nstop_max_blocks = 1\n"})
        self.lease()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", UNMET)
        self.assertEqual(self.decide()["decision"], "block")
        self.assertIn("releasing after 1 blocks", self.decide()["systemMessage"])

    def test_internal_error_allows_stop(self):
        self.lease()
        self.write_ledger(RUNNABLE)
        with patch.object(hook_stop.guard, "assess", side_effect=RuntimeError("boom")):
            decision = self.decide()
        self.assertNotIn("decision", decision)
        self.assertIn("could not check", decision["systemMessage"])
        self.assertIn("boom", decision["systemMessage"])

    def test_lease_on_other_worktree_does_not_govern(self):
        import subprocess

        other = make_tmp_dir(self) / "wt"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "side", str(other)], check=True)
        self.lease(worktree=other)
        self.write_ledger(RUNNABLE)
        self.assertIsNone(self.decide(unlazy=False))

    def test_outside_a_repository_allows(self):
        payload = {"session_id": "s1", "cwd": str(self.tmp), "hook_event_name": "Stop"}
        self.assertIsNone(hook_stop.decide(payload, env=self.env(unlazy=False), home=self.home))

    def test_cli_reads_stdin_and_prints_decision_json(self):
        self.lease()
        self.write_ledger(RUNNABLE)
        payload = json.dumps({"session_id": "s1", "cwd": str(self.repo), "hook_event_name": "Stop"})
        result = run_cli(["hook", "stop"], cwd=self.repo, env=self.env(unlazy=False), stdin=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")
        garbage = run_cli(["hook", "stop"], cwd=self.repo, env=self.env(unlazy=False), stdin="{nope")
        self.assertEqual(garbage.returncode, 0)


if __name__ == "__main__":
    unittest.main()
