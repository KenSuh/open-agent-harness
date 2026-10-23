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

    def test_gates_check_binds_with_real_unlazy(self):
        from oah.gates import check as check_mod
        from oah.gates.bindings import BindingStore
        from tests.helpers import git_commit, make_git_repo

        repo = make_git_repo(self)
        git_commit(repo, {"src/a.txt": "one\n"})
        ledger = repo / ".oah" / "tasks" / "t1" / "GATES.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("# Gates: t1\n\n- [ ] G1: the source file exists\n"
                          "  CHECK: node -e \"require('fs').statSync('src/a.txt'); console.log('present-' + 'ok')\"\n"
                          "  EXPECT: present-ok\n  EVIDENCE: pending\n", encoding="utf-8")
        store_dir = self.tmp / "approved-real"
        store_dir.mkdir(mode=0o700)
        env = dict(os.environ, UNLAZY_APPROVAL_DIR=str(store_dir))
        bindings = BindingStore(self.tmp / "state", "t1")
        report = check_mod.run_check(repo, ledger, bindings, unlazy_dir=self.unlazy, node=self.node, approve=True,
                                     env=env)
        self.assertEqual(report.actions, {"G1": "run"}, report.unlazy.stdout if report.unlazy else "")
        fresh = check_mod.freshness(repo, ledger, bindings, unlazy_dir=self.unlazy)
        self.assertEqual(fresh, {"G1": "fresh"})
        status = ledger_status(ledger, unlazy=self.unlazy, node=self.node, root=repo)
        self.assertEqual((status.verdict, status.source), ("met", "unlazy"))
        (repo / "src" / "a.txt").write_text("two\n", encoding="utf-8")
        self.assertEqual(check_mod.freshness(repo, ledger, bindings, unlazy_dir=self.unlazy), {"G1": "stale"})

    def test_live_copy_matches_the_pin(self):
        self.assertEqual(unlazy.verify_pin(self.unlazy), [])

    def test_pin_covers_the_call_closure(self):
        import re

        reference = re.compile(r"""["'](\.{1,2}/[\w./-]+\.mjs)["']""")
        seen, queue = set(), ["scripts/gate-check.mjs", "scripts/gate-lint.mjs", "scripts/stop-hook.mjs"]
        while queue:
            relative = queue.pop()
            if relative in seen:
                continue
            seen.add(relative)
            text = (self.unlazy / relative).read_text(encoding="utf-8")
            base = pathlib.PurePosixPath(relative).parent
            for target in reference.findall(text):
                queue.append(os.path.normpath(str(base / target)).replace(os.sep, "/"))
        self.assertEqual(seen, set(unlazy.PIN.files))


class PinTest(unittest.TestCase):
    def setUp(self):
        from tests.fixtures.gates.support import stub_pin

        self.tmp = make_tmp_dir(self)
        self.folder = make_unlazy_dir(self.tmp / "unlazy")
        self.pin = stub_pin(self.folder)

    def test_matching_checkout_passes(self):
        self.assertEqual(unlazy.verify_pin(self.folder, self.pin), [])

    def test_changed_script_is_refused(self):
        (self.folder / "scripts" / "lib" / "gates.mjs").write_text("// changed\n", encoding="utf-8")
        problems = unlazy.verify_pin(self.folder, self.pin)
        self.assertEqual(problems, ["scripts/lib/gates.mjs differs from the pinned copy"])
        (self.folder / "scripts" / "stop-hook.mjs").unlink()
        self.assertIn("scripts/stop-hook.mjs is missing", unlazy.verify_pin(self.folder, self.pin))

    def test_crlf_checkout_still_matches(self):
        path = self.folder / "scripts" / "gate-check.mjs"
        lf = path.read_bytes().replace(b"\r\n", b"\n")
        path.write_bytes(lf.replace(b"\n", b"\r\n"))
        self.assertEqual(unlazy.verify_pin(self.folder, self.pin), [])

    def test_pin_normalizes_only_crlf(self):
        path = self.folder / "scripts" / "gate-check.mjs"
        lf = path.read_bytes().replace(b"\r\n", b"\n")
        path.write_bytes(lf.replace(b"\n", b"\r\r\n"))
        self.assertIn("scripts/gate-check.mjs differs from the pinned copy",
                      unlazy.verify_pin(self.folder, self.pin))

    @unittest.skipUnless(os.name == "nt", "requires Git for Windows CRLF checkout")
    def test_git_crlf_checkout_matches(self):
        checkout = self.tmp / "crlf"
        subprocess.run(["git", "-c", "core.autocrlf=true", "clone", "--quiet", "--no-local",
                        str(self.folder), str(checkout)], check=True, capture_output=True)
        self.assertIn(b"\r\n", (checkout / "scripts" / "gate-check.mjs").read_bytes())
        self.assertEqual(unlazy.verify_pin(checkout, self.pin), [])

    def test_other_commit_is_refused(self):
        from tests.helpers import git_commit

        git_commit(self.folder, {"README.md": "newer\n"})
        problems = unlazy.verify_pin(self.folder, self.pin)
        self.assertEqual(len(problems), 1)
        self.assertIn(f"not the pinned {self.pin.commit[:12]}", problems[0])

    def test_copy_without_its_own_git_checkout_is_refused(self):
        import shutil
        from tests.helpers import make_git_repo

        loose = self.tmp / "loose"
        shutil.copytree(self.folder, loose, ignore=shutil.ignore_patterns(".git"))
        self.assertIn("not a git checkout", " ".join(unlazy.verify_pin(loose, self.pin)))
        nested = make_git_repo(self) / ".agents" / "skills" / "unlazy"
        shutil.copytree(self.folder, nested, ignore=shutil.ignore_patterns(".git"))
        self.assertIn("not a git checkout", " ".join(unlazy.verify_pin(nested, self.pin)))

    def test_require_pinned_names_the_install_command(self):
        with self.assertRaises(unlazy.UnlazyError) as caught:
            unlazy.require_pinned(self.folder)  # the real pin: a stand-in copy never matches it
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, "OAH_UNLAZY_UNPINNED")
        self.assertIn(f"git clone {unlazy.UPSTREAM_GIT}", message)
        self.assertIn(unlazy.PINNED_COMMIT, message)
        self.assertNotIn("npx", message)

    def test_install_commands_and_hints_pin_the_commit(self):
        target = self.tmp / "skills" / "unlazy"
        self.assertEqual(unlazy.install_commands(target), [
            ["git", "clone", "--quiet", "https://github.com/Leonxlnx/unlazy.git", str(target)],
            ["git", "-C", str(target), "checkout", "--quiet", "--detach", unlazy.PINNED_COMMIT]])
        for hint in (unlazy.INSTALL_HINT, unlazy.install_hint(target)):
            self.assertIn(unlazy.PINNED_COMMIT, hint)
            self.assertNotIn("npx", hint)
        self.assertEqual(unlazy.default_install_dir(self.tmp), self.tmp / ".agents" / "skills" / "unlazy")

    def test_pin_lists_every_script_oah_calls(self):
        for script in ("scripts/gate-check.mjs", "scripts/gate-lint.mjs", "scripts/stop-hook.mjs"):
            self.assertIn(script, unlazy.PIN.files)
        for digest in unlazy.PIN.files.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(unlazy.PIN.commit, unlazy.PINNED_COMMIT)

    def test_installed_pinned_copy_passes_find_tools(self):
        from unittest.mock import patch
        from oah.gates.context import find_tools

        env = {"OAH_UNLAZY_DIR": str(self.folder), "OAH_NODE": json.dumps([sys.executable])}
        with self.assertRaises(unlazy.UnlazyError) as caught:
            find_tools(None, env, self.tmp)
        self.assertEqual(caught.exception.code, "OAH_UNLAZY_UNPINNED")
        with patch.object(unlazy, "PIN", self.pin):
            self.assertTrue(find_tools(None, env, self.tmp).available)


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


# Inputs, bindings and `oah gates` -----------------------------------------------------------------

from oah import gates_cli  # noqa: E402
from oah.gates import check as gate_check_mod  # noqa: E402
from oah.gates.bindings import BindingStore  # noqa: E402
from oah.gates.inputs import InputsUncovered, collect  # noqa: E402
from oah.lease import TASK_RE  # noqa: E402
from tests.fixtures.gates.support import run_cli, stub_pin  # noqa: E402
from tests.helpers import git_commit, make_git_repo  # noqa: E402

RUNNABLE = """# Gates: t1

- [ ] G1: the module exists
  CHECK: oah check exists src/a.py
  EXPECT: OAH_CHECK_OK
  EVIDENCE: pending
"""

SPLIT = """# Gates: t1

- [ ] G1: the module exists
  CHECK: oah check exists src/a.py
  OAH-INPUTS: src
  EXPECT: OAH_CHECK_OK
  EVIDENCE: pending

- [x] G2: the docs were reviewed
  OAH-INPUTS: docs
  EVIDENCE: reviewed against the outline
"""


def pass_reply(ledger: pathlib.Path, text: str, ids=("G1",), extra_writes=None) -> dict:
    lines = "".join(f"  PASS GATES:{gate_id}: title\n" for gate_id in ids)
    writes = {str(ledger): text}
    writes.update(extra_writes or {})
    return {"write": writes, "stdout": f"{lines}GATES.md: 1 gates\nALL MET (1 met, reran: {len(ids)})\n", "code": 0}


class InputsTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n", "docs/readme.md": "hi\n", ".gitignore": "build/\n"})

    def digest(self, prefixes=None):
        return collect(self.repo).digest_for(prefixes)

    def test_manifest_covers_mode_bytes_add_delete(self):
        base = self.digest()
        (self.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
        changed = self.digest()
        self.assertNotEqual(base, changed)
        (self.repo / "src" / "new.py").write_text("", encoding="utf-8")
        added = self.digest()
        self.assertNotEqual(changed, added)
        (self.repo / "build").mkdir()
        (self.repo / "build" / "out.bin").write_bytes(b"x")
        self.assertEqual(added, self.digest())  # ignored files are not inputs
        (self.repo / "docs" / "readme.md").unlink()
        self.assertNotEqual(added, self.digest())

    @unittest.skipIf(os.name == "nt", "the executable bit is a POSIX file mode")
    def test_execution_mode_is_an_input(self):
        base = self.digest()
        path = self.repo / "src" / "a.py"
        path.chmod(path.stat().st_mode | 0o111)
        self.assertNotEqual(base, self.digest())

    def test_declared_prefixes_narrow_inputs(self):
        base_src, base_all = self.digest(("src",)), self.digest()
        (self.repo / "docs" / "readme.md").write_text("changed\n", encoding="utf-8")
        self.assertEqual(base_src, self.digest(("src",)))
        self.assertNotEqual(base_all, self.digest())
        self.assertNotEqual(self.digest(("src",)), self.digest(("src", "docs")))

    def test_generated_outputs_excluded(self):
        base = self.digest()
        for name in (".oah/tasks/t1/GATES.md", ".oah/tasks/t1/GATES.summary.md", ".unlazy/locks/x",
                     ".unlazy-hook-state.json"):
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("generated\n", encoding="utf-8")
        self.assertEqual(base, self.digest())

    def test_env_names_and_tools_are_inputs(self):
        manifest = collect(self.repo, env_names=("OAH_DEMO_VAR",), env={"OAH_DEMO_VAR": "1"})
        other = collect(self.repo, env_names=("OAH_DEMO_VAR",), env={"OAH_DEMO_VAR": "2"})
        self.assertNotEqual(manifest.digest_for(None), other.digest_for(None))
        tooled = collect(self.repo, tools={"node": "v1"})
        self.assertNotEqual(tooled.digest_for(None), collect(self.repo, tools={"node": "v2"}).digest_for(None))

    def test_execution_environment_is_mandatory(self):
        base_env = dict(os.environ)
        base = collect(self.repo, env=base_env).digest_for(None)
        for name, value in (("PATH", str(self.repo)), ("UNLAZY_SHELL", str(self.repo / "shell")),
                            ("OAH_NODE", json.dumps([sys.executable])), ("ComSpec", "other-cmd.exe"),
                            ("PATHEXT", ".EXE")):
            with self.subTest(name=name):
                self.assertNotEqual(base, collect(self.repo, env=dict(base_env, **{name: value})).digest_for(None))

    def test_runtime_bytes_are_inputs_even_at_same_path(self):
        import shutil

        runtime = self.repo.parent / ("runtime.exe" if os.name == "nt" else "runtime")
        for name in ("OAH_NODE", "UNLAZY_SHELL"):
            with self.subTest(name=name):
                shutil.copyfile(sys.executable, runtime)
                runtime.chmod(0o755)
                value = json.dumps([str(runtime)]) if name == "OAH_NODE" else str(runtime)
                env = dict(os.environ, **{name: value})
                base = collect(self.repo, env=env).digest_for(None)
                with runtime.open("ab") as handle:
                    handle.write(b"changed runtime")
                self.assertNotEqual(base, collect(self.repo, env=env).digest_for(None))

    def test_default_node_identity_uses_supplied_path(self):
        import shutil

        runtime = self.repo.parent / ("node.exe" if os.name == "nt" else "node")
        shutil.copyfile(sys.executable, runtime)
        runtime.chmod(0o755)
        env = dict(os.environ, PATH=str(runtime.parent), OAH_NODE="")
        base = collect(self.repo, env=env).digest_for(None)
        with runtime.open("ab") as handle:
            handle.write(b"changed node")
        self.assertNotEqual(base, collect(self.repo, env=env).digest_for(None))

    @unittest.skipUnless(os.name == "nt", "Windows environment names are case-insensitive")
    def test_windows_execution_environment_names_are_case_insensitive(self):
        env = dict(os.environ, OAH_NODE=json.dumps([sys.executable]))
        folded = {key.lower(): value for key, value in env.items()}
        self.assertEqual(collect(self.repo, env=env).digest_for(None), collect(self.repo, env=folded).digest_for(None))

    def test_submodule_inputs_are_refused_in_every_worktree_state(self):
        import shutil

        sub = make_git_repo(self)
        first = git_commit(sub, {"file.txt": "one\n"})
        git_commit(sub, {"file.txt": "two\n"})
        subprocess.run(["git", "-C", str(self.repo), "-c", "protocol.file.allow=always", "submodule", "add",
                        "--quiet", str(sub), "vendor/sub"], check=True, capture_output=True)
        live = self.repo / "vendor" / "sub"
        for state in ("clean", "dirty", "different-head", "missing"):
            with self.subTest(state=state):
                if state == "dirty":
                    (live / "file.txt").write_text("dirty\n", encoding="utf-8")
                elif state == "different-head":
                    subprocess.run(["git", "-C", str(live), "checkout", "--quiet", "--force", first], check=True)
                elif state == "missing":
                    shutil.rmtree(live)
                with self.assertRaises(InputsUncovered) as caught:
                    collect(self.repo)
                self.assertEqual(caught.exception.code, "OAH_GATES_INPUTS_UNCOVERED")


class GateCheckFlowTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n", "docs/readme.md": "hi\n"})
        self.tmp = make_tmp_dir(self)
        self.stub = StubNode(self.tmp / "stub")
        self.unlazy = make_unlazy_dir(self.tmp / "unlazy")
        self.ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        self.ledger.parent.mkdir(parents=True)
        self.store = BindingStore(self.tmp / "state", "t1")

    def check(self, approve=False):
        return gate_check_mod.run_check(self.repo, self.ledger, self.store, unlazy_dir=self.unlazy,
                                        node=self.stub.node, approve=approve, env=self.stub.env())

    def test_unchanged_check_with_changed_source_reverifies(self):
        self.ledger.write_text(RUNNABLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(RUNNABLE)))
        report = self.check()
        self.assertTrue(report.ok, report.lines)
        self.assertEqual(report.actions["G1"], "run")
        self.assertIn("--reverify", self.stub.calls("gate-check.mjs")[-1]["args"])
        self.assertEqual(len(self.stub.calls("gate-check.mjs")), 1)
        again = self.check()
        self.assertEqual(again.actions["G1"], "carry")
        self.assertEqual(len(self.stub.calls("gate-check.mjs")), 1)  # nothing affected, nothing run
        self.assertEqual(gate_check_mod.freshness(self.repo, self.ledger, self.store, unlazy_dir=self.unlazy,
                                                env=self.stub.env())["G1"],
                         "fresh")
        (self.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
        self.assertEqual(gate_check_mod.freshness(self.repo, self.ledger, self.store, unlazy_dir=self.unlazy,
                                                env=self.stub.env())["G1"], "stale")
        third = self.check()
        self.assertEqual(len(self.stub.calls("gate-check.mjs")), 2)
        self.assertIn("--reverify", self.stub.calls("gate-check.mjs")[-1]["args"])
        self.assertEqual(third.actions["G1"], "run")
        self.assertEqual(gate_check_mod.freshness(self.repo, self.ledger, self.store, unlazy_dir=self.unlazy,
                                                env=self.stub.env())["G1"], "fresh")

    def test_environment_changes_stale_bindings(self):
        self.ledger.write_text(RUNNABLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(RUNNABLE)))
        self.assertTrue(self.check().ok)
        for name, value in (("PATH", str(self.tmp)), ("UNLAZY_SHELL", str(self.tmp / "other-shell")),
                            ("OAH_NODE", json.dumps([sys.executable]))):
            with self.subTest(name=name):
                env = self.stub.env(**{name: value})
                self.assertEqual(gate_check_mod.freshness(self.repo, self.ledger, self.store,
                                                        unlazy_dir=self.unlazy, env=env), {"G1": "stale"})

    def test_unaffected_gate_gets_carry_record(self):
        self.ledger.write_text(SPLIT, encoding="utf-8")
        self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(SPLIT)))
        first = self.check()
        self.assertEqual((first.actions["G1"], first.actions["G2"]), ("run", "attest"))
        before = self.store.load()[gate_check_mod.ledger_key(self.repo, self.ledger)]["G2"]
        (self.repo / "src" / "a.py").write_text("a = 3\n", encoding="utf-8")
        second = self.check()
        self.assertEqual((second.actions["G1"], second.actions["G2"]), ("run", "carry"))
        carried = self.store.load()[gate_check_mod.ledger_key(self.repo, self.ledger)]["G2"]
        self.assertEqual(carried["kind"], "carry")
        self.assertEqual(carried["carry"]["old_inputs"], before["inputs"])
        self.assertEqual(carried["carry"]["new_inputs"], before["inputs"])
        self.assertEqual(carried["carry"]["evidence"], before["evidence"])

    def test_manual_gate_with_changed_inputs_is_stale_until_reattested(self):
        self.ledger.write_text(SPLIT, encoding="utf-8")
        self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(SPLIT)))
        self.check()
        (self.repo / "docs" / "readme.md").write_text("new text\n", encoding="utf-8")
        report = self.check()
        self.assertEqual(report.actions["G2"], "stale")
        self.assertFalse(report.ok)
        self.ledger.write_text(met_ledger(SPLIT).replace("reviewed against the outline", "re-reviewed"),
                               encoding="utf-8")
        self.assertEqual(self.check().actions["G2"], "attest")

    def test_inputs_changed_during_check_invalidate_run(self):
        self.ledger.write_text(RUNNABLE, encoding="utf-8")
        sneaky = {str(self.repo / "src" / "a.py"): "a = 99\n"}
        self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(RUNNABLE), extra_writes=sneaky))
        report = self.check()
        self.assertFalse(report.ok)
        self.assertEqual(report.actions["G1"], "invalidated")
        self.assertNotIn("G1", self.store.load().get(gate_check_mod.ledger_key(self.repo, self.ledger), {}))

    def test_changed_definition_or_input_declaration_is_not_bound(self):
        for text in (RUNNABLE.replace("src/a.py", "src/b.py"),
                     RUNNABLE.replace("  EXPECT:", "  OAH-INPUTS: docs\n  EXPECT:"),
                     RUNNABLE.replace("  EXPECT:", "  CWD: docs\n  EXPECT:"),
                     RUNNABLE.replace("the module exists", "a different outcome")):
            with self.subTest(text=text):
                self.ledger.write_text(RUNNABLE, encoding="utf-8")
                self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(text)))
                report = self.check()
                self.assertEqual(report.actions["G1"], "invalidated")
                self.assertNotIn("G1", self.store.load().get(gate_check_mod.ledger_key(self.repo, self.ledger), {}))

    def test_stale_result_overrides_pass_and_carry(self):
        text = SPLIT.replace("- [x] G2", "- [ ] G2").replace(
            "  OAH-INPUTS: docs\n  EVIDENCE: reviewed against the outline",
            "  CHECK: oah check exists docs/readme.md\n  OAH-INPUTS: docs\n"
            "  EXPECT: OAH_CHECK_OK\n  EVIDENCE: pending")
        for qualifier, emit_pass in (("GATES", False), ("GATES", True), ("my gates", False), ("my gates", True)):
            with self.subTest(qualifier=qualifier, emit_pass=emit_pass):
                self.ledger.write_text(text, encoding="utf-8")
                self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(text), ids=("G1", "G2")))
                self.assertTrue(self.check().ok)
                (self.repo / "docs" / "readme.md").write_text(f"{qualifier} {emit_pass}\n", encoding="utf-8")
                reply = pass_reply(self.ledger, met_ledger(text), ids=("G1", "G2"))
                prefix = f"  PASS {qualifier}:G1: title\n" if emit_pass else ""
                reply.update(stdout=prefix + f"  PASS {qualifier}:G2: title\n"
                             f"  STALE {qualifier}:G1: result not written\nUNMET: 1 (met: 1)\n", code=1)
                self.stub.reply("gate-check.mjs", reply)
                self.assertEqual(self.check().actions, {"G1": "invalidated", "G2": "run"})

    def test_custom_ledger_output_does_not_invalidate_itself(self):
        self.ledger = self.repo / "checks" / "main.md"
        self.ledger.parent.mkdir()
        self.ledger.write_text(RUNNABLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(RUNNABLE)))
        self.assertEqual(self.check().actions, {"G1": "run"})
        (self.repo / "src" / "a.py").write_text("changed\n", encoding="utf-8")
        changed_output = met_ledger(RUNNABLE).replace("output-sha256=" + "a" * 64, "output-sha256=" + "b" * 64)
        self.stub.reply("gate-check.mjs", pass_reply(self.ledger, changed_output))
        self.assertEqual(self.check().actions, {"G1": "run"})
        self.assertEqual(gate_check_mod.freshness(self.repo, self.ledger, self.store, unlazy_dir=self.unlazy,
                                                env=self.stub.env()), {"G1": "fresh"})
        self.ledger.write_text(changed_output.replace("src/a.py", "src/b.py"), encoding="utf-8")
        self.assertEqual(gate_check_mod.freshness(self.repo, self.ledger, self.store, unlazy_dir=self.unlazy,
                                                env=self.stub.env()), {"G1": "stale"})

    def test_unapproved_gate_is_not_bound(self):
        self.ledger.write_text(RUNNABLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"stdout": "APPROVAL REQUIRED GATES:G1\n    NOT RUN: inspect\n"
                                                     "GATES.md: 1 gates\nUNMET: 1 (met: 0)\n  GATES:G1\n",
                                           "code": 1})
        report = self.check()
        self.assertFalse(report.ok)
        self.assertEqual(report.actions["G1"], "not-run")
        self.assertTrue(report.approval_required)

    def test_unlazy_error_raises(self):
        self.ledger.write_text(RUNNABLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"stderr": "gate-check: boom\n", "code": 2})
        with self.assertRaises(gate_check_mod.GateRefused) as caught:
            self.check()
        self.assertEqual(caught.exception.code, "OAH_UNLAZY_FAILED")

    def test_invalid_ledger_refuses_before_running(self):
        self.ledger.write_text("# none\n", encoding="utf-8")
        with self.assertRaises(gate_check_mod.GateRefused) as caught:
            self.check()
        self.assertEqual(caught.exception.code, "OAH_GATES_INVALID")
        self.assertEqual(self.stub.calls(), [])


class GatesCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n"})
        self.tmp = make_tmp_dir(self)
        self.stub = StubNode(self.tmp / "stub")
        self.unlazy = make_unlazy_dir(self.tmp / "unlazy")
        self.state = self.tmp / "state"

    def env(self, unlazy=True):
        return self.stub.env(OAH_UNLAZY_DIR=str(self.unlazy if unlazy else self.tmp / "absent"),
                             OAH_STATE_DIR=str(self.state))

    def last_run_args(self):
        """Arguments of the last gate-check call that could run a CHECK (not a --status read)."""
        return [call for call in self.stub.calls("gate-check.mjs") if "--status" not in call["args"]][-1]["args"]

    def gates(self, *argv, unlazy=True):
        return run_cli(["gates", *argv], cwd=self.repo, env=self.env(unlazy), pin=stub_pin(self.unlazy))

    def test_new_writes_template_and_refuses_overwrite(self):
        made = self.gates("new", "t1")
        self.assertEqual(made.returncode, 0, made.stderr)
        ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        self.assertIn("OAH_GATES_CREATED", made.stdout)
        text = ledger.read_text(encoding="utf-8")
        self.assertIn("# Gates: t1", text)
        self.assertEqual(parse_text(text).errors, ())
        again = self.gates("new", "t1")
        self.assertEqual(again.returncode, 1)
        self.assertTrue(again.stderr.startswith("OAH_GATES_EXISTS"), again.stderr)
        self.assertEqual(self.gates("new", "../bad").returncode, 1)

    def test_template_checks_are_portable(self):
        text = gates_cli.template_text("demo")
        ledger = parse_text(text)
        self.assertEqual(ledger.errors, ())
        self.assertTrue(ledger.gates)
        for gate in ledger.gates:
            self.assertIsNotNone(gate.check, gate.id)
            self.assertIn(gate.check.split()[0], ("python", "node", "oah"), gate.check)
            for token in ("|", "&&", ";", ">", "<", "`", "$(", "%", "grep"):
                self.assertNotIn(token, gate.check, gate.check)
            self.assertRegex(gate.id, r"^G[1-9][0-9]*$")
        self.assertTrue(TASK_RE.fullmatch("demo"))

    def test_status_without_unlazy_is_structural_only(self):
        self.gates("new", "t1")
        status = self.gates("status", "t1", unlazy=False)
        self.assertEqual(status.returncode, 1, status.stdout + status.stderr)
        self.assertIn("STRUCTURAL ONLY", status.stdout)
        self.assertIn("G1", status.stdout)
        self.assertEqual(self.stub.calls(), [])

    def test_status_with_unlazy_delegates_and_reports_bindings(self):
        self.gates("new", "t1")
        self.stub.reply("gate-check.mjs", {"stdout": "  UNMET GATES:G1 (unchecked): x\nGATES.md: 3 gates\n"
                                                     "UNMET: 3 (met: 0)\n", "code": 1})
        status = self.gates("status", "t1")
        self.assertEqual(status.returncode, 1)
        self.assertIn("UNMET: 3", status.stdout)
        self.assertIn("OAH_GATES_STATUS verdict=unmet source=unlazy", status.stdout)
        self.assertEqual(self.stub.calls("gate-check.mjs")[-1]["args"][0], "--status")

    def test_check_without_unlazy_refuses(self):
        self.gates("new", "t1")
        refused = self.gates("check", "t1", unlazy=False)
        self.assertEqual(refused.returncode, 69)
        self.assertTrue(refused.stderr.startswith("OAH_UNLAZY_MISSING"), refused.stderr)

    def test_check_cli_passes_approve_only_when_asked(self):
        ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(RUNNABLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", pass_reply(ledger, met_ledger(RUNNABLE)))
        done = self.gates("check", "t1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("--approve", self.last_run_args())
        self.assertIn("OAH_GATES_CHECKED", done.stdout)
        ledger.write_text(RUNNABLE, encoding="utf-8")
        self.gates("check", "t1", "--approve")
        self.assertIn("--approve", self.last_run_args())

    def test_lint_delegates(self):
        self.gates("new", "t1")
        self.stub.reply("gate-lint.mjs", {"stdout": "LINT OK\n", "code": 0})
        linted = self.gates("lint", "t1", "--strict")
        self.assertEqual(linted.returncode, 0, linted.stderr)
        self.assertEqual(self.stub.calls("gate-lint.mjs")[-1]["args"][0], "--strict")
        structural = self.gates("lint", "t1", unlazy=False)
        self.assertIn("structural only", structural.stdout)
        self.assertEqual(structural.returncode, 0)

    def test_config_sets_timeout_and_rejects_unknown_keys(self):
        git_commit(self.repo, {".oah/config.toml": "[gates]\ncheck_timeout = 45\n"})
        ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(RUNNABLE, encoding="utf-8")
        self.stub.reply("gate-check.mjs", pass_reply(ledger, met_ledger(RUNNABLE)))
        self.gates("check", "t1")
        args = self.last_run_args()
        self.assertEqual(args[args.index("--timeout") + 1], "45")
        ledger.write_text(RUNNABLE, encoding="utf-8")
        self.gates("check", "t1", "--timeout", "7")
        args = self.last_run_args()
        self.assertEqual(args[args.index("--timeout") + 1], "7")
        (self.repo / ".oah" / "config.toml").write_text("[gates]\nnope = 1\n", encoding="utf-8")
        refused = self.gates("status", "t1")
        self.assertEqual(refused.returncode, 69)
        self.assertTrue(refused.stderr.startswith("OAH_CONFIG_UNKNOWN_KEY"), refused.stderr)

    def test_unpinned_unlazy_is_refused(self):
        self.assertEqual(self.gates("new", "t1").returncode, 0)  # writing a ledger needs no unlazy
        refused = run_cli(["gates", "status", "t1"], cwd=self.repo, env=self.env())  # the real pin
        self.assertEqual(refused.returncode, 69)
        self.assertTrue(refused.stderr.startswith("OAH_UNLAZY_UNPINNED"), refused.stderr)
        self.assertEqual(self.stub.calls(), [])

    def test_custom_ledger_path(self):
        custom = self.repo / "elsewhere.md"
        custom.write_text(RUNNABLE, encoding="utf-8")
        status = self.gates("status", "t1", "--ledger", "elsewhere.md", unlazy=False)
        self.assertIn("G1", status.stdout)
