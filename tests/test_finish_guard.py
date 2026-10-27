# 종료 가드 검사 — 충족·미충족·포기·구조 전용·낡은 입력·없는 바인딩에서 oah finish 가 하는 일과 경로 없는 요약을 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from oah.gates import guard, summary
from oah.gates.context import GateTools
from oah.wiki.base import WikiSettings
from oah.wiki.store import MaintenanceCheck, maintenance_output_paths
from tests.fixtures.gates.support import StubNode, make_unlazy_dir, met_ledger, run_cli, stub_pin
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

    def oah(self, *argv, unlazy=True, pinned=True):
        return run_cli(list(argv), cwd=self.repo, env=self.env(unlazy), pin=stub_pin(self.unlazy) if pinned else None)

    def start(self, *extra):
        started = self.oah("start", "--task", "t1", "--agent", "claude", "--scope", "src", *extra)
        self.assertEqual(started.returncode, 0, started.stderr)

    def finish(self, *extra, unlazy=True):
        return self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", *extra, unlazy=unlazy)

    def gates_check(self):
        return run_cli(["gates", "check", "t1"], cwd=self.repo, env=self.env(), pin=stub_pin(self.unlazy))

    def write_ledger(self, text):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(text, encoding="utf-8")

    def lease_active(self):
        return "t1" in self.oah("status").stdout

    def events(self):
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_gates_check_writes_summary_and_finish_only_verifies_it(self):
        self.start()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), ALL_MET)
        checked = self.gates_check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("OAH_GATES_SUMMARY_WRITTEN .oah/tasks/t1/GATES.summary.md", checked.stdout)
        text = self.summary.read_text(encoding="utf-8")
        self.assertTrue(all(SUMMARY_LINE.match(line) for line in text.splitlines()), text)
        self.assertRegex(text, r"(?m)^G1 met binding=run ")
        self.assertIn("verified: unlazy", text)
        (self.repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")  # same bytes: still fresh
        before = self.summary.read_bytes()
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("gates=met", finished.stdout)
        self.assertEqual(self.summary.read_bytes(), before)
        self.assertFalse(self.lease_active())
        self.assertEqual(self.events()[-1]["gates"]["outcome"], "met")
        self.assertTrue(self.events()[-1]["gates"]["verified"])

    def test_finish_refuses_missing_summary(self):
        self.start()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), ALL_MET)
        self.gates_check()
        self.summary.unlink()
        refused = self.finish()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_SUMMARY_MISSING"), refused.stderr)
        self.assertIn("oah gates check t1", refused.stderr)
        self.assertFalse(self.summary.exists())
        self.assertTrue(self.lease_active())

    def test_finish_refuses_stale_summary(self):
        self.start()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), ALL_MET)
        self.gates_check()
        self.summary.write_text(self.summary.read_text(encoding="utf-8").replace("binding=run", "binding=carry"),
                                encoding="utf-8")
        refused = self.finish()
        self.assertTrue(refused.stderr.startswith("OAH_GATES_SUMMARY_STALE"), refused.stderr)
        crlf = self.summary.read_text(encoding="utf-8").replace("binding=carry", "binding=run").replace("\n", "\r\n")
        self.summary.write_bytes(crlf.encode("utf-8"))
        self.assertEqual(self.finish().returncode, 0)  # a CRLF checkout of the same summary still matches

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
        self.assertFalse(self.summary.exists())  # finish writes nothing tracked
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

    def test_submodule_refusal_blocks_finish_and_stop(self):
        from oah import hook_stop, unlazy

        self.start()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), ALL_MET)
        self.assertEqual(self.gates_check().returncode, 0)
        sub = make_git_repo(self)
        git_commit(sub, {"module.py": "a = 1\n"})
        subprocess.run(["git", "-C", str(self.repo), "-c", "protocol.file.allow=always", "submodule", "add",
                        "--quiet", str(sub), "vendor/sub"], check=True, capture_output=True)
        refused = self.finish()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_INPUTS_UNCOVERED"), refused.stderr)
        with patch.object(unlazy, "PIN", stub_pin(self.unlazy)):
            decision = hook_stop.decide({"cwd": str(self.repo)}, env=self.env(), home=self.tmp)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("OAH_GATES_INPUTS_UNCOVERED", decision["reason"])

    def test_failing_rerun_blocks_finish(self):
        self.start()
        self.write_ledger(RUNNABLE)
        failing = {"write": {str(self.ledger): RUNNABLE},
                   "stdout": "  FAIL GATES:G1: x\nGATES.md: 1 gates\nUNMET: 1 (met: 0, reran: 1)\n", "code": 1}
        # Replies in call order: check (pass), its summary status, re-check (fail), finish's status.
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), ALL_MET, failing, UNMET)
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
        text = RUNNABLE.replace("G1:", "tests-311:")
        self.write_ledger(text)
        self.stub.reply("gate-check.mjs", {"write": {str(self.ledger): met_ledger(text)},
                                           "stdout": "  PASS GATES:tests-311: x\nALL MET (1 met)\n", "code": 0})
        checked = self.gates_check()
        self.assertEqual(checked.returncode, 1)
        self.assertTrue(checked.stderr.startswith("OAH_SUMMARY_FIELD_REJECTED"), checked.stderr)
        self.assertFalse(self.summary.exists())
        refused = self.finish()
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

    def test_multiple_custom_ledgers_keep_bindings_with_varying_output(self):
        from oah.gates import check as check_mod
        from oah.gates.bindings import BindingStore
        from oah import hook_stop, unlazy

        self.enterContext(patch.object(unlazy, "PIN", stub_pin(self.unlazy)))
        self.start("--gates", "checks/main.md", "--gates", "checks/other.md")
        ledgers = [self.repo / "checks" / name for name in ("main.md", "other.md")]
        ledgers[0].parent.mkdir()
        for path in ledgers:
            path.write_text(RUNNABLE, encoding="utf-8")
        store = BindingStore(self.state, "t1")
        for fingerprint in ("b", "c"):
            (self.repo / "src" / "a.py").write_text(f"a = '{fingerprint}'\n", encoding="utf-8")
            for path in ledgers:
                text = met_ledger(RUNNABLE).replace("output-sha256=" + "a" * 64,
                                                  "output-sha256=" + fingerprint * 64)
                reply = passing(path, text)
                reply["write"][str(path)] = text
                self.stub.reply("gate-check.mjs", reply)
                checked = check_mod.run_check(self.repo, path, store, unlazy_dir=self.unlazy,
                                              node=self.stub.node, env=self.env())
                self.assertEqual(checked.actions, {"G1": "run"})
            self.stub.reply("gate-check.mjs", ALL_MET)
            tools = GateTools(self.unlazy, self.stub.node, "")
            report = guard.evaluate(self.repo, "t1", ["checks/main.md", "checks/other.md"], self.state,
                                    tools=tools, allow_structural=False, env=self.env())
            guard.write_summary(self.repo, "t1", report.summary_text)
            with patch.object(unlazy, "PIN", stub_pin(self.unlazy)):
                self.assertIsNone(hook_stop.decide({"cwd": str(self.repo)}, env=self.env(), home=self.tmp))
        self.assertEqual(self.finish().returncode, 0)

    def test_gates_path_outside_repo_is_refused(self):
        refused = self.oah("start", "--task", "t1", "--agent", "claude", "--scope", "src", "--gates", "../x.md")
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_GATES_PATH_INVALID"), refused.stderr)

    def test_unpinned_unlazy_refuses_only_when_a_ledger_governs(self):
        self.start()
        self.write_ledger(met_ledger(RUNNABLE))
        refused = self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", pinned=False)
        self.assertEqual(refused.returncode, 69)
        self.assertTrue(refused.stderr.startswith("OAH_UNLAZY_UNPINNED"), refused.stderr)
        self.ledger.unlink()
        finished = self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", pinned=False)
        self.assertEqual(finished.returncode, 0, finished.stderr)

    def test_no_ledger_finishes_with_gates_none(self):
        self.start()
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("gates=none", finished.stdout)

    def test_commit_ledgers_config_skips_summary(self):
        git_commit(self.repo, {".oah/config.toml": "[gates]\ncommit_ledgers = true\n"})
        self.start()
        self.write_ledger(RUNNABLE)
        self.stub.reply("gate-check.mjs", passing(self.ledger, RUNNABLE), ALL_MET)
        self.assertIn("OAH_GATES_SUMMARY_SKIPPED", self.gates_check().stdout)
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertFalse(self.summary.exists())

    def test_invalid_gates_config_is_an_environment_error(self):
        git_commit(self.repo, {".oah/config.toml": "[gates]\ncommit_ledgers = \"yes\"\n"})
        self.start()
        refused = self.finish()
        self.assertEqual(refused.returncode, 69)
        self.assertTrue(refused.stderr.startswith("OAH_CONFIG_INVALID"), refused.stderr)

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


def real_check(ok, outputs=(), problems=()):
    """The real MaintenanceCheck, its record shaped as `oah wiki maintain` writes it, so the two cannot drift."""
    record = {"task": "t1", "outputs": dict(outputs)} if ok else None
    return MaintenanceCheck(ok=ok, problems=list(problems), record=record)


class MaintenanceCheckOutputsTest(unittest.TestCase):
    """MaintenanceCheck.outputs is the one reader of record["outputs"]; a malformed record gives None."""

    DIGEST = "0" * 64

    def test_outputs_are_sorted_pairs(self):
        check = MaintenanceCheck(ok=True, record={"outputs": {"docs/wiki/log.md": self.DIGEST,
                                                              "docs/wiki/index.md": self.DIGEST}})
        self.assertEqual(check.outputs, [("docs/wiki/index.md", self.DIGEST), ("docs/wiki/log.md", self.DIGEST)])
        self.assertEqual(MaintenanceCheck(ok=True, record={"outputs": {}}).outputs, [])

    def test_shared_definition_accepts_task_pages_independently_of_archive_names(self):
        settings = WikiSettings(dir="notes")
        task_pages = ("task-t1.md", "log_archive_001.md")
        paths = maintenance_output_paths(settings, archives=("log_archive_100.md",), task_pages=task_pages)
        self.assertEqual(paths, {"notes/log.md", "notes/index.md", "notes/log_archive_100.md",
                                 "notes/task-t1.md", "notes/log_archive_001.md"})
        check = MaintenanceCheck(ok=True, settings=settings, task_pages=task_pages,
                                 record={"outputs": {path: self.DIGEST for path in paths}})
        self.assertEqual(check.outputs, sorted((path, self.DIGEST) for path in paths))
        restricted = maintenance_output_paths(settings, archives=task_pages)
        self.assertEqual(restricted, {"notes/log.md", "notes/index.md"})

    def test_malformed_outputs_are_none(self):
        for record in (None, [], {}, {"outputs": None}, {"outputs": [["docs/wiki/log.md", self.DIGEST]]},
                       {"outputs": {"docs/wiki/log.md": 1}}, {"outputs": {"docs/wiki/log.md": "abc"}},
                       {"outputs": {"docs/wiki/log.md": self.DIGEST.upper().replace("0", "A")}},
                       {"outputs": {"": self.DIGEST}}):
            with self.subTest(record=record):
                self.assertIsNone(MaintenanceCheck(ok=True, record=record).outputs)

    def test_mixed_output_keys_are_none(self):
        check = MaintenanceCheck(ok=True, record={"outputs": {"docs/wiki/log.md": self.DIGEST, 1: self.DIGEST}})
        self.assertIsNone(check.outputs)

    def test_nul_output_path_is_none(self):
        check = MaintenanceCheck(ok=True, record={"outputs": {"docs/wiki/log\x00.md": self.DIGEST}})
        self.assertIsNone(check.outputs)

    def test_only_plain_allowlisted_output_paths_are_accepted(self):
        class StringSubclass(str):
            pass

        paths = ("docs/wiki/log\x00.md", "docs/wiki/\ud800.md", "docs/wiki/\udfff.md",
                 "/docs/wiki/log.md", "../docs/wiki/log.md", "docs/wiki/../log.md",
                 "docs/wiki/./log.md", "docs//wiki/log.md", "docs\\wiki\\log.md",
                 "C:/docs/wiki/log.md", "C:docs/wiki/log.md", "\\\\server\\wiki\\log.md",
                 "docs/wiki/page.md", "docs/wiki/log.md\n", "docs/wiki/log.md/extra",
                 "docs/wiki/log_archive_1.md", "docs/wiki/log_archive_001.md",
                 "docs/wiki/log_archive_00.md", "docs/wiki/log_archive_000.md",
                 "docs/wiki/log_archive_010.md", "docs/wiki/log_archive_100.md\n",
                 "docs/wiki/log_archive_\u0660\u0661.md", None, 1, b"docs/wiki/log.md",
                 StringSubclass("docs/wiki/log.md"))
        for path in paths:
            with self.subTest(path=path):
                outputs = {"docs/wiki/index.md": self.DIGEST, path: self.DIGEST}
                self.assertIsNone(MaintenanceCheck(ok=True, record={"outputs": outputs}).outputs)

        for path in ("docs/wiki/log.md", "docs/wiki/index.md", "docs/wiki/log_archive_01.md",
                     "docs/wiki/log_archive_99.md", "docs/wiki/log_archive_100.md",
                     "docs/wiki/log_archive_999.md", "docs/wiki/log_archive_1000.md"):
            with self.subTest(valid_path=path):
                check = MaintenanceCheck(ok=True, record={"outputs": {path: self.DIGEST}})
                self.assertEqual(check.outputs, [(path, self.DIGEST)])


class WikiMaintenanceTest(unittest.TestCase):
    """finish exempts only verified `oah wiki maintain` outputs; a stub verify_maintenance returns real checks."""

    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n", "docs/wiki/log.md": "# Log\n", "docs/wiki/index.md": "# Index\n",
                               "docs/wiki/log_archive_01.md": "# Old\n", "docs/wiki/page.md": "x\n"})
        self.state = make_tmp_dir(self) / "state"
        self.env = dict(os.environ, OAH_STATE_DIR=str(self.state), OAH_UNLAZY_DIR=str(self.state / "absent"))
        self.check = real_check(True)
        self.calls = []

        def verify_maintenance(root, task, *, settings):
            self.calls.append((pathlib.Path(root), task))
            return self.check

        fake = types.ModuleType("oah.wiki")
        fake.verify_maintenance = verify_maintenance
        patcher = patch.dict(sys.modules, {"oah.wiki": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        started = self.oah("start", "--task", "t1", "--agent", "claude", "--scope", "src")
        self.assertEqual(started.returncode, 0, started.stderr)

    def oah(self, *argv):
        return run_cli(list(argv), cwd=self.repo, env=self.env)

    def finish(self, *extra):
        return self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", *extra)

    def write(self, name, text):
        path = self.repo / name
        path.write_text(text, encoding="utf-8")
        return [name, hashlib.sha256(path.read_bytes()).hexdigest()]

    def last_event(self):
        lines = (self.state / "events.jsonl").read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1])

    def test_unavailable_verifier_requires_recorded_override(self):
        with patch.dict(sys.modules, {"oah.wiki": None}):
            refused = self.finish()
            self.assertEqual(refused.returncode, 1)
            self.assertTrue(refused.stderr.startswith("OAH_WIKI_MAINTENANCE_MISSING"), refused.stderr)
            self.assertIn("--allow-missing-wiki-maintenance", refused.stderr)
            allowed = self.finish("--allow-missing-wiki-maintenance")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        record = self.last_event()["wiki_maintenance"]
        self.assertTrue(record["override"])
        self.assertEqual(record["task"], "t1")
        self.assertTrue(record["problems"])

    def test_verified_outputs_are_exempt_and_recorded(self):
        outputs = [self.write("docs/wiki/log.md", "# Log\n- t1\n"), self.write("docs/wiki/index.md", "# Index\nnew\n"),
                   self.write("docs/wiki/log_archive_02.md", "# Archive\n")]
        self.check = real_check(True, outputs)
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(self.calls, [(self.repo, "t1")])
        self.assertEqual(self.last_event()["wiki_maintenance"], {"task": "t1", "exempt": sorted(outputs)})

    def test_other_wiki_edits_still_need_scope(self):
        outputs = [self.write("docs/wiki/log.md", "# Log\n- t1\n")]
        self.write("docs/wiki/page.md", "edited\n")
        self.check = real_check(True, outputs)
        refused = self.finish()
        self.assertTrue(refused.stderr.startswith("OAH_SCOPE_EXCEEDED"), refused.stderr)
        self.assertIn("docs/wiki/page.md", refused.stderr)
        self.assertNotIn("docs/wiki/log.md", refused.stderr)

    def test_digest_mismatch_and_existing_archive_are_not_exempt(self):
        stale = self.write("docs/wiki/log.md", "# Log\n- t1\n")
        self.write("docs/wiki/log.md", "# Log\n- changed after maintain\n")
        archive = self.write("docs/wiki/log_archive_01.md", "# Old, rewritten\n")
        self.check = real_check(True, [stale, archive])
        refused = self.finish()
        self.assertTrue(refused.stderr.startswith("OAH_SCOPE_EXCEEDED"), refused.stderr)
        self.assertIn("docs/wiki/log.md", refused.stderr)
        self.assertIn("docs/wiki/log_archive_01.md", refused.stderr)

    def test_missing_maintenance_refuses_unless_overridden(self):
        self.check = real_check(False, problems=["no maintenance record for t1"])
        refused = self.finish()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_WIKI_MAINTENANCE_MISSING"), refused.stderr)
        self.assertIn("oah wiki maintain --task t1", refused.stderr)
        finished = self.finish("--allow-missing-wiki-maintenance")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("OAH_WIKI_MAINTENANCE_SKIPPED", finished.stdout)
        self.assertEqual(self.last_event()["wiki_maintenance"],
                         {"task": "t1", "override": True, "problems": ["no maintenance record for t1"]})

    def test_ok_check_without_readable_outputs_refuses_by_name(self):
        self.write("docs/wiki/log.md", "# Log\n- t1\n")
        self.check = MaintenanceCheck(ok=True, record={"task": "t1", "outputs": ["docs/wiki/log.md"]})
        refused = self.finish()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_WIKI_MAINTENANCE_MISSING"), refused.stderr)
        self.assertIn("oah wiki maintain --task t1", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        self.check = MaintenanceCheck(ok=True)
        self.assertTrue(self.finish().stderr.startswith("OAH_WIKI_MAINTENANCE_MISSING"))

    def test_no_wiki_folder_means_no_check(self):
        import shutil

        shutil.rmtree(self.repo / "docs" / "wiki")
        git_commit(self.repo, {}, "drop wiki")
        self.check = real_check(False)
        self.oah("renew", "--task", "t1", "--agent", "claude", "--scope", "src", "--scope", "docs")
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(self.calls, [])


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
        from oah import unlazy as unlazy_mod

        self.enterContext(patch.object(unlazy_mod, "PIN", stub_pin(unlazy)))
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

    def test_structural_result_exports_no_summary(self):
        self.ledger.write_text(met_ledger(self.text), encoding="utf-8")
        tools = GateTools(None, None, "unlazy not found")
        result = guard.evaluate(self.repo, "t1", [], self.tmp / "state", tools=tools, allow_structural=True)
        self.assertEqual(result.outcome, "structural")
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
