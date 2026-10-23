# 게이트 영향 연결 검사 — 실제 원장 파서와 무시된 원장을 통해 정확한 pin·exec 결속과 사전 점검을 확인한다.
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import unittest
from unittest.mock import patch

from oah import preflight
from oah.impact import ImpactError, gatebind
from tests.helpers import directory_alias, git_commit, git_env, make_git_repo, make_tmp_dir
from tests.test_impact_map import head, run_impact


class GateDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/check.py": "print('OK')\n",
                               ".gitignore": ".oah/tasks/*/GATES.md\nprivate-ledgers/\n"})
        self.ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text("- [ ] G1: the check runs\n  CHECK: python src/check.py\n  EXPECT: OK\n"
                               "  EVIDENCE: pending\n- [ ] G2: the source is pinned\n"
                               "  CHECK: oah check contains src/check.py print\n  EXPECT: src/check.py\n"
                               "  EVIDENCE: pending\n", encoding="utf-8")

    def test_ignored_ledger_contributes_exact_pin_and_exec_bindings(self):
        gates, status = gatebind.discover(self.repo)
        self.assertFalse(status.startswith("unavailable"), status)
        rows = gatebind.rows(gates, "src/check.py", min_needle=3)
        self.assertEqual([(row.source, row.binding) for row in rows],
                         [(".oah/tasks/t1/GATES.md#G1", "exec"), (".oah/tasks/t1/GATES.md#G2", "pin")])
        with patch.dict(os.environ):
            os.environ.pop("OAH_STATE_DIR", None)
            lines = preflight.impact_hook(self.repo, ("src/check.py",))
        self.assertIn("exec .oah/tasks/t1/GATES.md#G1 for src/check.py", lines)
        self.assertIn("pin .oah/tasks/t1/GATES.md#G2 for src/check.py", lines)
        queried = run_impact(self.repo, "src/check.py", "--strong-only")
        self.assertEqual(queried.returncode, 0, queried.stderr)
        self.assertIn(".oah/tasks/t1/GATES.md#G1", queried.stdout)

    def test_configured_ledger_globs_are_used_without_duplicate_gates(self):
        external = self.repo / "private-ledgers" / "GATES.md"
        external.parent.mkdir()
        external.write_text(self.ledger.read_text(encoding="utf-8"), encoding="utf-8")
        config = self.repo / ".oah" / "config.toml"
        config.write_text('[impact]\nledger_globs = ["private-ledgers/*.md", "private-ledgers/GATES.md"]\n',
                          encoding="utf-8")
        gates, _ = gatebind.discover(self.repo)
        self.assertEqual([gate.label for gate in gates],
                         ["private-ledgers/GATES.md#G1", "private-ledgers/GATES.md#G2"])

    def test_ref_gate_bindings_ignore_worktree_ledger_edits_and_deletions(self):
        relative = ".oah/tasks/t1/GATES.md"
        git_commit(self.repo, {".gitignore": "", relative: self.ledger.read_text(encoding="utf-8")})
        revision = head(self.repo)
        remote = make_tmp_dir(self) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, env=git_env())
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", str(remote)], check=True,
                       env=git_env())
        subprocess.run(["git", "-C", str(self.repo), "push", "-q", "origin", "HEAD:main"], check=True,
                       env=git_env())
        git_commit(self.repo, {"README.md": "later commit\n"})

        def report():
            result = run_impact(self.repo, "src/check.py", "--ref", revision, "--strong-only", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        baseline = report()
        expected = [(f"{relative}#G1", "exec"), (f"{relative}#G2", "pin")]
        self.assertEqual(sorted((row["source"], row["binding"]) for row in baseline["rows"]
                                if row["layer"] == "gates"), expected)
        for change in ("edited", "invalid", "deleted"):
            with self.subTest(change=change):
                if change == "deleted":
                    self.ledger.unlink()
                else:
                    text = ("- [ ] G3: live requirement\n  CHECK: python src/other.py\n  EXPECT: OK\n"
                            "  EVIDENCE: pending\n") if change == "edited" else (
                                "- [ ] G3: invalid\n  CHECK: python src/other.py\n")
                    self.ledger.write_text(text, encoding="utf-8")
                current = report()
                self.assertEqual(current["map"], baseline["map"])
                self.assertEqual(current["gates"], baseline["gates"])
                self.assertEqual(current["rows"], baseline["rows"])

    def test_revision_discovery_reads_blobs_with_configured_globs(self):
        text = self.ledger.read_text(encoding="utf-8")
        git_commit(self.repo, {".oah/config.toml": '[impact]\nledger_globs = ["checks/**/GATES[12].md", '
                                                '"checks/GATES1.md"]\n',
                               "checks/GATES1.md": text, "checks/nested/GATES2.md": text,
                               "unrelated/GATES1.md": text})
        revision = head(self.repo)
        (self.repo / "checks/GATES1.md").unlink()
        (self.repo / "checks/nested/GATES2.md").write_text("not a ledger", encoding="utf-8")
        gates, status = gatebind.discover(self.repo, rev=revision)
        self.assertEqual([gate.label for gate in gates], ["checks/GATES1.md#G1", "checks/GATES1.md#G2",
                                                        "checks/nested/GATES2.md#G1", "checks/nested/GATES2.md#G2"])
        self.assertTrue(status.startswith("4 gates from 2 ledgers"), status)

    def test_malformed_ledger_refuses_instead_of_omitting_requirements(self):
        self.ledger.write_text("- [ ] G1: the check runs\n  CHECK: python src/check.py\n", encoding="utf-8")
        with self.assertRaises(ImpactError) as caught:
            gatebind.discover(self.repo)
        self.assertEqual(caught.exception.code, "OAH_GATES_INVALID")
        with patch.dict(os.environ):
            os.environ.pop("OAH_STATE_DIR", None)
            with self.assertRaises(preflight.ImpactRefused):
                preflight.impact_hook(self.repo, ("src/check.py",))

    def test_discovery_refuses_a_ledger_reached_through_an_external_directory_link(self):
        outside = make_tmp_dir(self)
        (outside / "GATES.md").write_text(self.ledger.read_text(encoding="utf-8"), encoding="utf-8")
        directory_alias(outside, self.repo / "linked-ledgers")
        (self.repo / ".oah" / "config.toml").write_text(
            '[impact]\nledger_globs = ["linked-ledgers/GATES.md"]\n', encoding="utf-8")
        with self.assertRaises(ImpactError) as caught:
            gatebind.discover(self.repo)
        self.assertEqual(caught.exception.code, "OAH_GATES_INVALID")


if __name__ == "__main__":
    unittest.main()
