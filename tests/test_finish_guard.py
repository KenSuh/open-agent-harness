# 종료 가드 검사 — 충족·미충족·포기·구조 전용·낡은 입력·없는 바인딩에서 oah finish 가 하는 일과 경로 없는 요약을 본다.
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

from oah.gates import guard, summary
from oah.gates.context import GateTools
from tests.fixtures.gates.support import SRC, StubNode, make_unlazy_dir, met_ledger, run_cli
from tests.helpers import git_commit, make_git_repo, make_tmp_dir

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "gates"
HEX = r"[0-9a-f]{64}"
SUMMARY_LINE = re.compile(
    r"^(# oah gates summary v1|verified: (unlazy|structural)|ledger: " + HEX + r"|"
    r"G[1-9][0-9]{0,3} (met|abandoned) binding=(run|attest|carry|none) title=" + HEX +
    r" check=(" + HEX + r"|none) expect=(" + HEX + r"|none) evidence=" + HEX + r" inputs=(" + HEX + r"|none))$")

RUNNABLE = """# Gates: t1

- [ ] G1: the module exists
  CHECK: oah check exists src/a.py
  EXPECT: OAH_CHECK_OK
  EVIDENCE: pending
"""

WITH_MANUAL = RUNNABLE + """
- [ ] G2: the owner signs off
  EVIDENCE: pending

ABANDON: G2 the owner is away this week
"""

ALL_MET = {"stdout": "GATES.md: 1 gates\nALL MET (1 met)\n", "code": 0}
UNMET = {"stdout": "  UNMET GATES:G1 (unchecked): x\nGATES.md: 1 gates\nUNMET: 1 (met: 0)\n  GATES:G1\n", "code": 1}
HANDOFF = {"stdout": "GATES.md: 2 gates\nHANDOFF REQUIRED: 1 abandoned (met: 1)\n  GATES:G2\n", "code": 1}


def passing(ledger: pathlib.Path, text: str, *, extra: dict | None = None) -> dict:
    writes = {str(ledger): met_ledger(text)}
    writes.update(extra or {})
    return {"write": writes, "stdout": "  PASS GATES:G1: x\nGATES.md: 1 gates\nALL MET (1 met, reran: 1)\n", "code": 0}


class FinishGuardTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n", "docs/readme.md": "hi\n"})
        self.tmp = make_tmp_dir(self)
        self.stub = StubNode(self.tmp / "stub")
        self.unlazy = make_unlazy_dir(self.tmp / "unlazy")
        self.state = self.tmp / "state"
        self.ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        self.summary = self.ledger.parent / "GATES.summary.md"

    def env(self, unlazy=True):
        return self.stub.env(OAH_UNLAZY_DIR=str(self.unlazy if unlazy else self.tmp / "absent"),
                             OAH_STATE_DIR=str(self.state))

    def oah(self, *argv, unlazy=True):
        return subprocess.run([sys.executable, "-m", "oah", *argv], cwd=self.repo, capture_output=True, text=True,
                              encoding="utf-8", env=self.env(unlazy), timeout=120)

    def start(self, *extra):
        started = self.oah("start", "--task", "t1", "--agent", "claude", "--scope", "src", *extra)
        self.assertEqual(started.returncode, 0, started.stderr)

    def finish(self, *extra, unlazy=True):
        return self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", *extra, unlazy=unlazy)

    def gates_check(self):
        return run_cli("gates_cli", ["gates", "check", "t1"], cwd=self.repo, env=self.env())

    def write_ledger(self, text):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(text, encoding="utf-8")

    def lease_active(self):
        return "t1" in self.oah("status").stdout

    def events(self):
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_finish_met_writes_summary(self):
        self.start()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), ALL_MET)
        checked = self.gates_check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        (self.repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")  # same bytes: still fresh
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("gates=met", finished.stdout)
        text = self.summary.read_text(encoding="utf-8")
        self.assertTrue(all(SUMMARY_LINE.match(line) for line in text.splitlines()), text)
        self.assertRegex(text, r"(?m)^G1 met binding=run ")
        self.assertIn("verified: unlazy", text)
        self.assertFalse(self.lease_active())
        self.assertEqual(self.events()[-1]["gates"]["outcome"], "met")
        self.assertTrue(self.events()[-1]["gates"]["verified"])

    def test_finish_unmet_refuses(self):
        self.start()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", UNMET)
        refused = self.finish()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_UNMET"), refused.stderr)
        self.assertIn("G1", refused.stderr)
        self.assertIn("oah gates check t1", refused.stderr)
        self.assertTrue(self.lease_active())
        self.assertFalse(self.summary.exists())

    def test_finish_abandoned_reports_handoff(self):
        self.start()
        self.write_ledger(WITH_MANUAL)
        self.stub.reply("gate-check.mjs", passing(self.ledger, WITH_MANUAL), HANDOFF)
        self.gates_check()
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("gates=handoff", finished.stdout)
        self.assertIn("OAH_GATES_HANDOFF_REQUIRED", finished.stdout)
        self.assertIn("G2", finished.stdout)
        self.assertNotIn("the owner is away", finished.stdout)
        self.assertRegex(self.summary.read_text(encoding="utf-8"), r"(?m)^G2 abandoned binding=none ")
        self.assertEqual(self.events()[-1]["gates"]["handoff"], ["G2"])

    def test_finish_structural_needs_flag(self):
        self.start()
        self.write_ledger(met_ledger(RUNNABLE))
        refused = self.finish(unlazy=False)
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_UNVERIFIED"), refused.stderr)
        self.assertIn("--allow-structural", refused.stderr)
        self.assertTrue(self.lease_active())

    def test_finish_structural_flag_records_unverified(self):
        self.start()
        self.write_ledger(met_ledger(RUNNABLE))
        finished = self.finish("--allow-structural", unlazy=False)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("gates=structural", finished.stdout)
        self.assertIn("OAH_GATES_UNVERIFIED", finished.stdout)
        event = self.events()[-1]["gates"]
        self.assertEqual((event["outcome"], event["verified"]), ("structural", False))
        self.assertIn("verified: structural", self.summary.read_text(encoding="utf-8"))
        self.assertEqual(self.stub.calls(), [])

    def test_finish_structural_unmet_refuses_even_with_flag(self):
        self.start()
        self.write_ledger(RUNNABLE)
        refused = self.finish("--allow-structural", unlazy=False)
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_UNMET"), refused.stderr)

    def test_finish_rejects_stale_binding(self):
        self.start()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), ALL_MET)
        self.gates_check()
        (self.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")  # unlazy still says met
        refused = self.finish()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_STALE"), refused.stderr)
        self.assertIn("G1", refused.stderr)
        self.assertTrue(self.lease_active())

    def test_finish_rejects_missing_binding(self):
        self.start()
        self.write_ledger(met_ledger(RUNNABLE))
        self.stub.reply("gate-check.mjs", ALL_MET)
        refused = self.finish()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_STALE"), refused.stderr)
        self.assertIn("missing", refused.stderr)

    def test_failing_rerun_blocks_finish(self):
        self.start()
        self.write_ledger(RUNNABLE)
        failing = {"write": {str(self.ledger): RUNNABLE},
                   "stdout": "  FAIL GATES:G1: x\nGATES.md: 1 gates\nUNMET: 1 (met: 0, reran: 1)\n", "code": 1}
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), failing, UNMET)
        self.gates_check()
        (self.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
        rerun = self.gates_check()
        self.assertEqual(rerun.returncode, 1)
        self.assertIn("OAH_GATE G1 failed", rerun.stdout)
        refused = self.finish()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_UNMET"), refused.stderr)

    def test_inputs_changed_during_check_block_finish(self):
        self.start()
        self.write_ledger(RUNNABLE)
        sneaky = {str(self.repo / "src" / "a.py"): "a = 3\n"}
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE, extra=sneaky), ALL_MET)
        self.assertEqual(self.gates_check().returncode, 1)
        refused = self.finish()
        self.assertTrue(refused.stderr.startswith("OAH_GATES_STALE"), refused.stderr)

    def test_summary_rejects_non_g_ids(self):
        self.start()
        self.write_ledger(met_ledger(RUNNABLE.replace("G1:", "tests-311:")))
        refused = self.finish("--allow-structural", unlazy=False)
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_SUMMARY_FIELD_REJECTED"), refused.stderr)
        self.assertTrue(self.lease_active())

    def test_declared_gates_are_used_and_must_exist(self):
        self.start("--gates", "checks/main.md")
        status = json.loads(self.oah("status", "--json").stdout)
        self.assertEqual(status["leases"][0]["gates"], ["checks/main.md"])
        refused = self.finish()
        self.assertTrue(refused.stderr.startswith("OAH_GATES_MISSING"), refused.stderr)
        custom = self.repo / "checks" / "main.md"
        custom.parent.mkdir()
        custom.write_text(met_ledger(RUNNABLE), encoding="utf-8")
        self.assertIn("gates=structural", self.finish("--allow-structural", unlazy=False).stdout)

    def test_gates_path_outside_repo_is_refused(self):
        refused = self.oah("start", "--task", "t1", "--agent", "claude", "--scope", "src", "--gates", "../x.md")
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_PATH_INVALID"), refused.stderr)

    def test_no_ledger_finishes_with_gates_none(self):
        self.start()
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("gates=none", finished.stdout)

    def test_gate_artifacts_do_not_count_against_scope(self):
        self.start()
        self.write_ledger(met_ledger(RUNNABLE))
        self.summary.write_text("# oah gates summary v1\n", encoding="utf-8")
        finished = self.finish("--allow-structural", unlazy=False)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.ledger.unlink()
        self.summary.unlink()
        other = self.repo / ".oah" / "tasks" / "t2" / "GATES.summary.md"
        other.parent.mkdir(parents=True)
        other.write_text("x\n", encoding="utf-8")
        self.start()
        refused = self.finish()
        self.assertTrue(refused.stderr.startswith("OAH_SCOPE_EXCEEDED"), refused.stderr)
        self.assertIn(".oah/tasks/t2/GATES.summary.md", refused.stderr)


class SummaryPrivacyTest(unittest.TestCase):
    """Every free-text field carries a path or a personal identifier; only ids, enums and digests survive."""

    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n"})
        self.tmp = make_tmp_dir(self)
        self.planted = {
            "HOME": "/" + "Us" + "ers/someone-private",
            "WINHOME": "C:\\" + "Us" + "ers\\someone-private",
            "EMAIL": "someone.private" + "@" + "example.com",
            "NAME": "Someone Private",
            "TASK": "private-task-label",
        }
        text = (FIXTURES / "private_fields.md").read_text(encoding="utf-8")
        for key, value in self.planted.items():
            text = text.replace("{" + key + "}", value)
        self.text = text
        self.ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        self.ledger.parent.mkdir(parents=True)

    def assertPrivate(self, rendered):
        lines = rendered.splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, SUMMARY_LINE)
        lowered = rendered.lower()
        for value in self.planted.values():
            for piece in [value, *re.split(r"[\\/ .@-]+", value)]:
                if len(piece) >= 4:
                    self.assertNotIn(piece.lower(), lowered, piece)
        for word in ("import", "review", "signs", "away", "notes", "project", "check", "node"):
            self.assertNotIn(word, lowered.replace("check=", ""), word)

    def test_summary_keeps_only_ids_enums_and_digests(self):
        stub = StubNode(self.tmp / "stub")
        unlazy = make_unlazy_dir(self.tmp / "unlazy")
        from oah.gates.bindings import BindingStore
        from oah.gates import check as check_mod

        self.ledger.write_text(self.text, encoding="utf-8")
        store = BindingStore(self.tmp / "state", "t1")
        stub.reply("gate-check.mjs", {"write": {str(self.ledger): met_ledger(self.text)},
                                      "stdout": "  PASS GATES:G1: x\nHANDOFF REQUIRED: 1 abandoned (met: 2)\n",
                                      "code": 1},
                   {"stdout": "HANDOFF REQUIRED: 1 abandoned (met: 2)\n  GATES:G3\n", "code": 1})
        report = check_mod.run_check(self.repo, self.ledger, store, unlazy_dir=unlazy, node=stub.node,
                                     env=stub.env())
        self.assertEqual(report.actions, {"G1": "run", "G2": "attest", "G3": "abandoned"})
        tools = GateTools(unlazy, stub.node, "")
        result = guard.evaluate(self.repo, "t1", [], self.tmp / "state", tools=tools, allow_structural=False,
                                env=stub.env())
        self.assertEqual(result.outcome, "handoff")
        self.assertPrivate(result.summary_text)
        self.assertIn("binding=run", result.summary_text)
        self.assertIn("binding=attest", result.summary_text)

    def test_structural_summary_is_private_too(self):
        self.ledger.write_text(met_ledger(self.text), encoding="utf-8")
        tools = GateTools(None, None, "unlazy not found")
        result = guard.evaluate(self.repo, "t1", [], self.tmp / "state", tools=tools, allow_structural=True)
        self.assertEqual(result.outcome, "structural")
        self.assertPrivate(result.summary_text)

    def test_commit_ledgers_skips_summary(self):
        self.ledger.write_text(met_ledger(self.text), encoding="utf-8")
        tools = GateTools(None, None, "unlazy not found")
        result = guard.evaluate(self.repo, "t1", [], self.tmp / "state", tools=tools, allow_structural=True,
                                commit_ledgers=True)
        self.assertIsNone(result.summary_text)

    def test_writer_rejects_fields_outside_the_allowlist(self):
        good = summary.Entry(gate_id="G1", status="met", binding="run", title="a" * 64, check="b" * 64,
                             expect="c" * 64, evidence="d" * 64, inputs="e" * 64)
        self.assertRegex(summary.render([("f" * 64, [good])], verified="unlazy"), r"(?m)^G1 met binding=run")
        bad_values = {"gate_id": "G1 /tmp/x", "status": "met-ish", "binding": "manual", "title": "/" + "Us" + "ers/x",
                      "check": "A" * 64, "inputs": "e" * 63}
        for field, value in bad_values.items():
            with self.subTest(field=field):
                entry = summary.Entry(**{**good.__dict__, field: value})
                with self.assertRaises(summary.SummaryRejected) as caught:
                    summary.render([("f" * 64, [entry])], verified="unlazy")
                self.assertEqual(caught.exception.code, "OAH_SUMMARY_FIELD_REJECTED")
        with self.assertRaises(summary.SummaryRejected):
            summary.render([("not-a-digest", [good])], verified="unlazy")
        with self.assertRaises(summary.SummaryRejected):
            summary.render([("f" * 64, [good])], verified="trusted")


if __name__ == "__main__":
    unittest.main()
