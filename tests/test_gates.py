# 게이트 검사 — unlazy CLI 계약(가짜 node), 원장 파서·구조 상태, 입력 목록·바인딩, oah gates 명령을 본다.
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import unittest

from oah import unlazy
from oah.gates import ledger_status, parse_ledger
from oah.gates.ledger import definition_digest, gate_state, parse_text
from tests.fixtures.gates.support import StubNode, automatic_evidence, make_unlazy_dir, met_ledger
from tests.helpers import make_tmp_dir

SIMPLE = """# Gates: demo

- [ ] G1: the suite passes
  CHECK: python -m unittest
  EXPECT: /^OK/m
  EVIDENCE: pending

- [ ] G2: the wording is reviewed
  EVIDENCE: pending
"""


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_dir(self)
        self.stub = StubNode(self.tmp / "stub")
        self.unlazy = make_unlazy_dir(self.tmp / "unlazy")
        self.ledger = self.tmp / "GATES.md"
        self.ledger.write_text(SIMPLE, encoding="utf-8")

    def gate_check(self, mode, **options):
        return unlazy.gate_check(self.unlazy, self.ledger, node=self.stub.node, mode=mode, env=self.stub.env(),
                                 **options)

    def test_pinned_commit_is_full_sha(self):
        self.assertRegex(unlazy.PINNED_COMMIT, r"^[0-9a-f]{40}$")
        self.assertEqual(unlazy.PINNED_VERSION, "2.1.0")

    def test_status_argv_matches_contract(self):
        self.stub.reply("gate-check.mjs", {"stdout": "GATES.md: 2 gates\nALL MET (2 met)\n", "code": 0})
        result = self.gate_check("status", root=self.tmp)
        self.assertEqual(result.verdict, "met")
        call = self.stub.calls("gate-check.mjs")[-1]
        self.assertEqual(call["args"], ["--status", "--root", str(self.tmp), "--", str(self.ledger)])

    def test_reverify_argv_matches_contract(self):
        self.stub.reply("gate-check.mjs", {"stdout": "UNMET: 1 (met: 1, reran: 1)\n  GATES:G1\n", "code": 1})
        result = self.gate_check("reverify", root=self.tmp, cwd=self.tmp, timeout=30)
        self.assertEqual(result.verdict, "unmet")
        call = self.stub.calls("gate-check.mjs")[-1]
        self.assertEqual(call["args"], ["--reverify", "--timeout", "30", "--cwd", str(self.tmp), "--root",
                                        str(self.tmp), "--", str(self.ledger)])

    def test_check_without_approve_flag_never_passes_approve(self):
        self.stub.reply("gate-check.mjs", {"stdout": "ALL MET (1 met)\n", "code": 0})
        self.gate_check("run", root=self.tmp, cwd=self.tmp)
        self.gate_check("reverify", root=self.tmp, cwd=self.tmp)
        self.gate_check("status", root=self.tmp)
        for call in self.stub.calls("gate-check.mjs"):
            self.assertNotIn("--approve", call["args"])
        self.gate_check("reverify", root=self.tmp, cwd=self.tmp, approve=True)
        self.assertIn("--approve", self.stub.calls("gate-check.mjs")[-1]["args"])
        with self.assertRaises(ValueError):
            self.gate_check("status", root=self.tmp, approve=True)

    def test_verdicts_from_exit_and_tokens(self):
        table = [
            (0, "GATES.md: 2 gates\nALL MET (2 met)\n", "met"),
            (1, "UNMET: 1 (met: 1)\n  GATES:G2\n", "unmet"),
            (1, "HANDOFF REQUIRED: 1 abandoned (met: 1)\n  GATES:G2\n", "handoff"),
            (1, "HANDOFF REQUIRED: 1 abandoned (met: 0, unmet: 1)\n  GATES:G2\nUNMET: 1 (met: 0)\n", "unmet"),
            (0, "", "error"),
            (0, "UNMET: 1 (met: 0)\n", "error"),
            (1, "", "error"),
            (1, "  UNMET GATES:G1 (unchecked): x\n", "error"),
            (2, "gate-check: line 3: duplicate gate id G1\n", "error"),
            (3, "CLAIM REFUSED (1 conflict(s))\n", "error"),
            (None, "", "error"),
        ]
        for code, stdout, expected in table:
            with self.subTest(code=code, stdout=stdout):
                self.assertEqual(unlazy.parse_verdict(code, stdout), expected)

    def test_lint_argv_and_verdict(self):
        self.stub.reply("gate-lint.mjs", {"stdout": "LINT FINDINGS: 0 error(s), 1 warning(s)\n", "code": 1})
        result = unlazy.gate_lint(self.unlazy, self.ledger, node=self.stub.node, strict=True, env=self.stub.env())
        self.assertEqual(result.verdict, "unmet")
        self.assertEqual(self.stub.calls("gate-lint.mjs")[-1]["args"], ["--strict", "--", str(self.ledger)])
        self.stub.reply("gate-lint.mjs", {"stdout": "LINT OK\n", "code": 0})
        result = unlazy.gate_lint(self.unlazy, self.ledger, node=self.stub.node, env=self.stub.env())
        self.assertEqual(result.verdict, "met")
        self.assertEqual(self.stub.calls("gate-lint.mjs")[-1]["args"], ["--", str(self.ledger)])

    def test_stop_hook_gets_payload_on_stdin(self):
        reply = json.dumps({"decision": "block", "reason": "unlazy: 1 item(s) need work"})
        self.stub.reply("stop-hook.mjs", {"stdout": reply + "\n", "code": 0})
        payload = {"session_id": "s1", "cwd": str(self.tmp), "hook_event_name": "Stop"}
        result = unlazy.stop_hook(self.unlazy, payload, node=self.stub.node, env=self.stub.env())
        call = self.stub.calls("stop-hook.mjs")[-1]
        self.assertEqual(json.loads(call["stdin"]), payload)
        self.assertEqual(call["args"], [])
        self.assertEqual(json.loads(result.stdout), json.loads(reply))

    def test_missing_script_is_reported(self):
        (self.unlazy / "scripts" / "gate-lint.mjs").unlink()
        with self.assertRaises(unlazy.UnlazyError) as caught:
            unlazy.gate_lint(self.unlazy, self.ledger, node=self.stub.node, env=self.stub.env())
        self.assertEqual(caught.exception.code, "OAH_UNLAZY_INCOMPLETE")

    def test_node_override_must_be_a_json_list(self):
        self.assertEqual(unlazy.resolve_node({"OAH_NODE": json.dumps(["a", "b"])}), ("a", "b"))
        with self.assertRaises(unlazy.UnlazyError) as caught:
            unlazy.resolve_node({"OAH_NODE": "node --flag"})
        self.assertEqual(caught.exception.code, "OAH_NODE_INVALID")


@unittest.skipUnless(os.environ.get("OAH_TEST_UNLAZY_DIR"), "set OAH_TEST_UNLAZY_DIR to probe a real unlazy")
class PinnedUnlazyProbeTest(unittest.TestCase):
    """Probe a real unlazy install (PLAN 11): approvals outside the root work, inside the root are refused."""

    def setUp(self):
        from tests.helpers import require_node

        require_node(self)
        self.unlazy = pathlib.Path(os.environ["OAH_TEST_UNLAZY_DIR"])
        self.node = unlazy.resolve_node(os.environ)
        self.tmp = make_tmp_dir(self)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.ledger = self.repo / "GATES.md"
        self.ledger.write_text("# Gates: probe\n\n- [ ] G1: node runs\n"
                               "  CHECK: node -e \"console.log('probe-' + 'ok')\"\n  EXPECT: probe-ok\n"
                               "  EVIDENCE: pending\n", encoding="utf-8")

    def test_store_outside_root_runs_after_approval(self):
        store = self.tmp / "approved"
        store.mkdir(mode=0o700)
        env = dict(os.environ, UNLAZY_APPROVAL_DIR=str(store))
        result = unlazy.gate_check(self.unlazy, self.ledger, node=self.node, mode="run", approve=True,
                                   cwd=self.repo, root=self.repo, env=env)
        self.assertEqual(result.verdict, "met", result.stdout + result.stderr)
        self.assertIn("ALL MET (1 met)", result.stdout)

    def test_store_inside_root_is_refused(self):
        store = self.repo / ".git" / "oah-dev" / "unlazy-approved"
        store.mkdir(parents=True, mode=0o700)
        env = dict(os.environ, UNLAZY_APPROVAL_DIR=str(store))
        result = unlazy.gate_check(self.unlazy, self.ledger, node=self.node, mode="run", approve=True,
                                   cwd=self.repo, root=self.repo, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.verdict, "error")
        self.assertIn("outside the repository root", result.stderr)


class LedgerParserTest(unittest.TestCase):
    def test_parses_gates_attributes_and_abandon(self):
        text = SIMPLE + "\nABANDON: G2 owner unavailable\n"
        ledger = parse_text(text)
        self.assertEqual(ledger.errors, ())
        self.assertEqual([gate.id for gate in ledger.gates], ["G1", "G2"])
        first, second = ledger.gates
        self.assertEqual((first.title, first.check, first.expect, first.evidence, first.checked),
                         ("the suite passes", "python -m unittest", "/^OK/m", "pending", False))
        self.assertIsNone(second.check)
        self.assertEqual(ledger.abandoned, {"G2": "owner unavailable"})

    def test_crlf_equals_lf(self):
        lf = parse_text(SIMPLE)
        crlf = parse_text(SIMPLE.replace("\n", "\r\n"))
        self.assertEqual([(g.id, g.title, g.check, g.expect, g.evidence) for g in lf.gates],
                         [(g.id, g.title, g.check, g.expect, g.evidence) for g in crlf.gates])
        self.assertEqual(crlf.errors, ())

    def test_fenced_examples_ignored(self):
        text = SIMPLE + "\n````markdown\n- [ ] G9: example\n  CHECK: x\n```\n- [ ] G8: still fenced\n````\n"
        self.assertEqual([gate.id for gate in parse_text(text).gates], ["G1", "G2"])
        self.assertIn("unclosed fenced block", parse_text(SIMPLE + "\n```\n- [ ] G3: open\n").errors)

    def test_strict_errors_match_unlazy(self):
        cases = {
            "zero": ("# Gates: none\n", "ledger contains zero live gates"),
            "duplicate": (SIMPLE + "- [ ] G1: again\n", "duplicate gate id G1"),
            "partial": ("- [ ] G1: x\n  CHECK: python a.py\n", "runnable gates require both"),
            "unindented": ("- [ ] G1: x\nCHECK: python a.py\n", "unindented CHECK"),
            "indented abandon": (SIMPLE + "  ABANDON: G1 why\n", "indented ABANDON"),
            "unknown abandon": (SIMPLE + "ABANDON: G7 why\n", "ABANDON references unknown gate G7"),
            "blank reason": (SIMPLE + "ABANDON: G1\n", "needs a non-blank reason"),
            "no id": ("- [ ] just words\n", "explicit ID"),
            "orphan": ("# T\n  EVIDENCE: x\n- [ ] G1: y\n", "orphan EVIDENCE"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name):
                errors = parse_text(text).errors
                self.assertTrue(any(message in error for error in errors), errors)

    def test_definition_digest_matches_unlazy_vector(self):
        # Vectors computed with unlazy's gateDefinitionDigest at the pinned commit.
        self.assertEqual(definition_digest("python -m unittest", "/^OK/m", None),
                         "5521fb09c4113f05404bfcfdc6eacca1e7c2be20e4da3efb2415d7211756dd0e")
        self.assertEqual(definition_digest('node a.mjs "é" \t ', "ok", "pkg/sub"),
                         "d3e1ebfe63af21cb9f5abc5d0d3d18214c26eb74e2b8afaf5c65bd097b9201cf")
        self.assertIsNone(definition_digest(None, None, None))

    def test_state_rules(self):
        evidence = automatic_evidence("python -m unittest", "/^OK/m")
        def state(box, gate_evidence, runnable=True, abandon=False):
            body = "  CHECK: python -m unittest\n  EXPECT: /^OK/m\n" if runnable else ""
            text = f"- [{box}] G1: t\n{body}  EVIDENCE: {gate_evidence}\n" + ("ABANDON: G1 why\n" if abandon else "")
            ledger = parse_text(text)
            return gate_state(ledger.gates[0], ledger.abandoned)
        self.assertEqual(state(" ", "pending"), "unmet")
        self.assertEqual(state("x", evidence), "met")
        self.assertEqual(state("x", "pending"), "stale-unmet")
        self.assertEqual(state("x", "it passed, trust me"), "stale-unmet")
        other = automatic_evidence("python -m other", "/^OK/m")
        self.assertEqual(state("x", other), "stale-unmet")
        self.assertEqual(state("x", "pending", runnable=False), "unmet-no-evidence")
        self.assertEqual(state("x", "reviewed in issue 4", runnable=False), "met")
        self.assertEqual(state(" ", "pending", abandon=True), "abandoned")

    def test_oah_inputs_line(self):
        text = "- [ ] G1: t\n  CHECK: python a.py\n  OAH-INPUTS: src, tests/unit\n  EXPECT: ok\n  EVIDENCE: pending\n"
        ledger = parse_text(text)
        self.assertEqual(ledger.errors, ())
        self.assertEqual(ledger.gates[0].inputs, ("src", "tests/unit"))
        self.assertIsNone(parse_text(SIMPLE).gates[0].inputs)
        bad = parse_text(text.replace("src, tests/unit", "../outside"))
        self.assertTrue(any("OAH-INPUTS" in error for error in bad.errors))

    def test_parse_ledger_reads_utf8_file(self):
        path = make_tmp_dir(self) / "GATES.md"
        path.write_bytes(SIMPLE.replace("demo", "démo").encode("utf-8"))
        self.assertEqual(len(parse_ledger(path).gates), 2)
        missing = parse_ledger(path.parent / "nope.md")
        self.assertTrue(missing.errors)


class LedgerStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_dir(self)
        self.ledger = self.tmp / "GATES.md"
        self.stub = StubNode(self.tmp / "stub")
        self.unlazy = make_unlazy_dir(self.tmp / "unlazy")

    def test_structural_status_labels(self):
        self.ledger.write_text(SIMPLE, encoding="utf-8")
        status = ledger_status(self.ledger)
        self.assertEqual((status.verdict, status.source), ("unmet", "structural"))
        self.assertEqual(status.unmet, ("G1", "G2"))
        done = met_ledger(SIMPLE).replace("- [ ] G2: the wording is reviewed\n  EVIDENCE: pending",
                                          "- [x] G2: the wording is reviewed\n  EVIDENCE: reviewed")
        self.ledger.write_text(done, encoding="utf-8")
        self.assertEqual(ledger_status(self.ledger).verdict, "met")
        self.ledger.write_text(met_ledger(SIMPLE) + "\nABANDON: G2 owner unavailable\n", encoding="utf-8")
        status = ledger_status(self.ledger)
        self.assertEqual((status.verdict, status.abandoned), ("handoff", ("G2",)))
        self.ledger.write_text("# none\n", encoding="utf-8")
        self.assertEqual(ledger_status(self.ledger).verdict, "invalid")

    def test_unlazy_status_is_used_when_given(self):
        self.ledger.write_text(met_ledger(SIMPLE) + "\nABANDON: G2 why\n", encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"stdout": "HANDOFF REQUIRED: 1 abandoned (met: 1)\n  GATES:G2\n",
                                           "code": 1})
        status = ledger_status(self.ledger, unlazy=self.unlazy, node=self.stub.node, env=self.stub.env())
        self.assertEqual((status.verdict, status.source), ("handoff", "unlazy"))
        self.assertEqual(self.stub.calls("gate-check.mjs")[-1]["args"][0], "--status")

    def test_unlazy_status_disagreement_fails_closed(self):
        self.ledger.write_text(SIMPLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"stdout": "ALL MET (2 met)\n", "code": 0})
        status = ledger_status(self.ledger, unlazy=self.unlazy, node=self.stub.node, env=self.stub.env())
        self.assertEqual(status.verdict, "unmet")
        self.assertIn("disagree", status.detail)

    def test_unlazy_error_is_invalid(self):
        self.ledger.write_text(SIMPLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"stdout": "", "stderr": "gate-check: boom\n", "code": 2})
        status = ledger_status(self.ledger, unlazy=self.unlazy, node=self.stub.node, env=self.stub.env())
        self.assertEqual(status.verdict, "invalid")
        self.assertIn("boom", status.detail)


if __name__ == "__main__":
    unittest.main()
