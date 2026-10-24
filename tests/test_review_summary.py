# oah review summary 검사 — 가짜 리뷰어 세 라운드와 직접 쓴 영수증으로 라운드·판정·digest·독립성·노력 검증·게이트·후속 과제·중요 경로 블록과 JSON 을 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import unittest

from oah.gates import summary as gate_summary
from oah.lease import Store
from oah.review import receipts
from oah.review import summary as review_summary
from tests.fixtures.gates.support import run_cli
from tests.helpers import git_env, make_tmp_dir
from tests.test_review_cli import oah
from tests.test_review_rounds import Project, bug, git

SHA_A = "a" * 64


def finding(number: int, k: int = 1, *, severity: str = "P2", title: str = "a follow-up",
            location: str = "src/app.py:1", axis: str = "tests") -> dict:
    return {"finding_id": f"R{number}-F{k}", "class_id": f"R{number}-C{k}", "severity": severity, "axis": axis,
            "title": title, "detail": "the mechanism", "location": location, "in_scope": True, "fix_kind": "local",
            "prior_refs": []}


def publish_receipt(project, number: int, *, head: str | None = None, base: str | None = None, binding=None,
                    outcome: str = "converged", findings: tuple = (), independent=True, effort_verified: bool = True,
                    cli_path: str = "codex", task: str = "t1") -> str:
    """Publish one receipt the way `oah review` shapes it, without running a reviewer."""
    findings = list(findings)
    blocking = any(item["severity"] in ("P0", "P1") for item in findings)
    binding = binding or project.binding
    record = {
        "schema": 1, "kind": "receipt", "task": task, "round": number, "series_round": number,
        "base": base or project.base, "head": head or project.head, "base_label": "merge-base with main",
        "intent": binding.to_json(), "intent_digest": binding.digest,
        "reviewer": {"engine": "codex", "cli_path": cli_path, "cli_version": "codex-cli 0.163.0",
                     "cli_sha256": "c" * 64, "model": "gpt-test-a", "effort": "high", "requested_effort": "high",
                     "header_effort": "high", "header_check": "match", "resolved_inference_effort": "high",
                     "normalization_basis": "test", "effort_verified": effort_verified, "tier": "high",
                     "tier_source": "flag", "run_id": f"run-{number}"},
        "author_engines": ["claude"], "authorship": [], "independent": independent,
        "verdict": "request_changes" if blocking else "approve", "summary": "fixture",
        "findings": findings, "classes": [{"class_id": item["class_id"], "mechanism": "m", "rule": "r"}
                                          for item in findings],
        "outcome": outcome, "follow_ups": [item["finding_id"] for item in findings] if outcome == "converged" else [],
        "decision_stop_reason": "a design change" if outcome == "needs-decision" else None, "decision": None,
        "extra_round": False, "extra_attempt": False, "history": [], "history_digest": receipts.history_digest([]),
        "canary_verified": True, "canary_scope": "token file only", "db_impact": "db-impact: none",
        "db_query": None, "impact": {}, "critical_paths": [], "stripped": [], "intent_return": "", "evidence": "",
        "prompt_sha256": "sha256:" + SHA_A, "output_sha256": "sha256:" + SHA_A,
        "output_schema_sha256": "sha256:" + SHA_A, "started_at": "2026-10-24T00:00:00Z",
        "finished_at": "2026-10-24T00:01:00Z"}
    return receipts.publish(project.state, task, f"R{number}.json", record, summary={
        "round": number, "series_round": number, "base": record["base"], "head": record["head"],
        "verdict": record["verdict"], "outcome": outcome})


def summary_cli(project, *args: str):
    return run_cli(["review", "summary", "--task", "t1", *args], cwd=project.repo, env=project.env)


class SummaryEndToEndTest(unittest.TestCase):
    def test_three_round_series_prints_every_round(self):
        project = Project(self)
        project.commit({".github/workflows/ci.yml": "on: pull_request\n"}, "add ci")
        project.queue({"findings": [bug()]}, {"findings": [bug()]},
                      {"findings": [{"severity": "P2", "axis": "tests", "title": "add a regression test",
                                     "location": "src/app.py:1"}]})
        first = project.review(rules=[])  # an unknown normalization: the round counts, its effort is unverified
        self.assertEqual(first.code, "OAH_REVIEW_CHANGES_REQUESTED", first.message)
        project.correct(1)
        second = project.review()
        self.assertEqual(second.code, "OAH_REVIEW_CHANGES_REQUESTED", second.message)
        project.correct(2)
        third = project.review()
        self.assertEqual(third.code, "OAH_REVIEW_CONVERGED", third.message)
        digests = {entry.name: entry.digest for entry in receipts.load(project.state, "t1").receipts()}

        text = summary_cli(project)
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        shown = summary_cli(project, "--json")
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        data = json.loads(shown.stdout)

        self.assertEqual([row["receipt"] for row in data["rounds"]], ["R1.json", "R2.json", "R3.json"])
        self.assertEqual([row["verdict"] for row in data["rounds"]], ["request_changes", "request_changes", "approve"])
        self.assertEqual([row["outcome"] for row in data["rounds"]], ["changes-requested", "changes-requested",
                                                                      "converged"])
        self.assertEqual([row["independent"] for row in data["rounds"]], [True, False, False])
        self.assertEqual([row["effort_verified"] for row in data["rounds"]], [False, True, True])
        for row in data["rounds"]:
            self.assertEqual(row["digest"], digests[row["receipt"]])
            self.assertIn(row["digest"], text.stdout)
            self.assertIn(row["independence"], text.stdout)
            self.assertIn(row["reviewer"]["cli_sha256"][:12], text.stdout)
            self.assertNotIn("cli_path", row["reviewer"])
        self.assertIn("| R1 |", text.stdout)
        self.assertIn("| R3 |", text.stdout)
        self.assertIn("request_changes", text.stdout)
        self.assertIn("effort unverified", text.stdout)
        self.assertIn("effort verified", text.stdout)
        self.assertIn("not independent", text.stdout)
        self.assertTrue(data["status"]["converged"], data["status"])
        self.assertEqual(data["status"]["receipt"], "R3.json")
        self.assertIn(f"converged at head `{project.head[:12]}`", text.stdout)
        self.assertEqual([(item["finding_id"], item["severity"]) for item in data["follow_ups"]], [("R3-F1", "P2")])
        self.assertIn("R3-F1", text.stdout)
        self.assertIn("`add a regression test`", text.stdout)
        self.assertEqual(data["critical_paths"], [".github/workflows/ci.yml"])
        self.assertIn("`.github/workflows/ci.yml`", text.stdout)
        self.assertEqual(data["intent"], project.binding.line())
        self.assertEqual(data["head"], project.head)
        self.assertEqual(data["markdown"], text.stdout)  # --json carries exactly the printed block
        receipt = project.receipt(3)
        self.assertNotIn(receipt["reviewer"]["cli_path"], text.stdout + shown.stdout)


class SummaryTest(unittest.TestCase):
    def test_summary_digest_binds_committed_blob_across_checkout_line_endings(self):
        project = Project(self)
        path = ".oah/tasks/t1/GATES.summary.md"
        # Use the renderer's grammar, preserving exactly the bytes sent to git.
        lf = gate_summary.render([], verified="unlazy").encode("utf-8")
        for committed in (lf, lf.replace(b"\n", b"\r\n")):
            with self.subTest(committed_crlf=b"\r\n" in committed):
                project.commit({path: committed})  # * -text preserves both committed forms
                blob = subprocess.run(["git", "-C", str(project.repo), "cat-file", "blob", f"HEAD:{path}"],
                                      check=True, capture_output=True, env=git_env()).stdout
                self.assertEqual(blob, committed)
                (project.repo / path).write_bytes(lf.replace(b"\n", b"\r\n"))
                data = review_summary.committed_gate_summary(project.repo, "t1", project.head)
                self.assertEqual(data.digest, "sha256:" + hashlib.sha256(blob).hexdigest())
        git(project.repo, "config", "core.autocrlf", "true")
        project.commit({".gitattributes": b"* -text\n.oah/tasks/t1/GATES.summary.md text\n", path: lf})
        blob = subprocess.run(["git", "-C", str(project.repo), "cat-file", "blob", f"HEAD:{path}"],
                              check=True, capture_output=True, env=git_env()).stdout
        self.assertEqual(blob, lf)
        (project.repo / path).unlink()
        git(project.repo, "checkout", "--", path)
        self.assertEqual((project.repo / path).read_bytes(), lf.replace(b"\n", b"\r\n"))
        self.assertEqual(review_summary.committed_gate_summary(project.repo, "t1", project.head).digest,
                         "sha256:" + hashlib.sha256(blob).hexdigest())

    @unittest.skipUnless(os.name == "nt", "Windows committed CRLF summary")
    def test_windows_summary_digest_uses_committed_bytes(self):
        project = Project(self)
        path = ".oah/tasks/t1/GATES.summary.md"
        text = gate_summary.render([], verified="unlazy")
        project.commit({path: text.replace("\n", "\r\n").encode("utf-8")})
        blob = subprocess.run(["git", "-C", str(project.repo), "cat-file", "blob", f"HEAD:{path}"],
                              check=True, capture_output=True, env=git_env()).stdout
        self.assertEqual(blob, text.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(review_summary.committed_gate_summary(project.repo, "t1", project.head).digest,
                         "sha256:" + hashlib.sha256(blob).hexdigest())

    def test_summary_reports_gate_summary_at_head(self):
        project = Project(self)
        publish_receipt(project, 1)
        data = json.loads(summary_cli(project, "--json").stdout)
        self.assertEqual(data["gates"]["state"], "missing")
        self.assertIn("no `.oah/tasks/t1/GATES.summary.md` committed at HEAD", data["markdown"])

        entry = gate_summary.Entry
        text = gate_summary.render([(SHA_A, [entry("G1", "met", "run", SHA_A, SHA_A, SHA_A, SHA_A, SHA_A),
                                            entry("G2", "abandoned", "none", SHA_A, None, None, SHA_A, None)])],
                                   verified="unlazy")
        path = ".oah/tasks/t1/GATES.summary.md"
        project.commit({path: text.encode("utf-8")}, "gates")
        blob = subprocess.run(["git", "-C", str(project.repo), "cat-file", "blob", f"HEAD:{path}"],
                              check=True, capture_output=True, env=git_env()).stdout
        self.assertEqual(blob, text.encode("utf-8"))
        data = json.loads(summary_cli(project, "--json").stdout)
        self.assertEqual(data["gates"]["state"], "committed")
        self.assertEqual(data["gates"]["verified"], "unlazy")
        self.assertEqual(data["gates"]["gates"], [{"id": "G1", "status": "met"}, {"id": "G2", "status": "abandoned"}])
        self.assertEqual(data["gates"]["digest"], "sha256:" + hashlib.sha256(blob).hexdigest())
        self.assertIn("1 met, 1 abandoned (verified by unlazy)", data["markdown"])
        self.assertFalse(data["status"]["converged"])  # the summary commit moved HEAD past R1

        project.commit({path: text.replace("verified: unlazy", "verified: maybe").encode("utf-8")}, "edit")
        data = json.loads(summary_cli(project, "--json").stdout)
        self.assertEqual(data["gates"]["state"], "invalid")
        self.assertIn("does not match the summary grammar", data["markdown"])

        ledger = "# Gates: t1\n\n- [ ] G1: works\n  CHECK: oah check exists src/app.py\n  EXPECT: OAH_CHECK_OK\n" \
                 "  EVIDENCE: pending\n"
        project.commit({".oah/config.toml": (project.repo / ".oah/config.toml").read_text(encoding="utf-8")
                        + "[gates]\ncommit_ledgers = true\n", ".oah/tasks/t1/GATES.md": ledger}, "ledgers")
        data = json.loads(summary_cli(project, "--json").stdout)
        self.assertEqual(data["gates"]["state"], "ledgers")
        self.assertEqual(data["gates"]["gates"], [{"id": "G1", "status": "unmet"}])
        self.assertEqual(data["gates"]["verified"], "structural")

    def test_summary_quotes_untrusted_text(self):
        project = Project(self)
        title = "ping @someone about `x` | and\nmore"
        publish_receipt(project, 1, findings=(finding(1, title=title, location="src/a|b.py:2 @x"),))
        text = summary_cli(project).stdout
        self.assertIn("``ping @someone about `x` | and more``", text)
        self.assertIn("`src/a|b.py:2 @x`", text)
        for line in text.splitlines():
            self.assertNotRegex(line.replace("``ping @someone about `x` | and more``", "")
                                .replace("`src/a|b.py:2 @x`", ""), r"@(someone|x)\b", line)

    def test_summary_omits_reviewer_path(self):
        project = Project(self)
        private = str(make_tmp_dir(self) / "home" / "tools" / "codex")
        publish_receipt(project, 1, cli_path=private)
        shown = summary_cli(project, "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn(private, shown.stdout)
        self.assertNotIn(private, summary_cli(project).stdout)

    def test_summary_empty_task_refuses(self):
        project = Project(self)
        done = summary_cli(project)
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("OAH_REVIEW_SUMMARY_EMPTY:"), done.stderr)
        self.assertIn("oah review --task t1", done.stderr)
        self.assertEqual(done.stdout, "")
        shown = summary_cli(project, "--json")
        self.assertEqual(json.loads(shown.stdout)["code"], "OAH_REVIEW_SUMMARY_EMPTY")
        bad = run_cli(["review", "summary", "--task", "../x"], cwd=project.repo, env=project.env)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("OAH_TASK_INVALID", bad.stderr)

    def test_summary_lists_history_problems(self):
        project = Project(self)
        publish_receipt(project, 1)
        path = project.state / "reviews" / "t1" / "R1.json"
        path.write_bytes(path.read_bytes().replace(b'"fixture"', b'"edited"'))
        shown = summary_cli(project, "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        data = json.loads(shown.stdout)
        self.assertEqual(data["rounds"], [])
        self.assertTrue(any("R1.json no longer matches its indexed digest" in item for item in data["problems"]))
        self.assertFalse(data["status"]["converged"])
        self.assertIn("History problems", data["markdown"])

    def test_status_names_why_the_head_is_not_converged(self):
        project = Project(self)
        publish_receipt(project, 1, outcome="changes-requested", findings=(finding(1, severity="P1"),))
        data = json.loads(summary_cli(project, "--json").stdout)
        self.assertFalse(data["status"]["converged"])
        self.assertIn("R1.json requested changes", data["status"]["reason"])
        old = project.head
        project.commit({"src/app.py": "print('v2')\n"})
        publish_receipt(project, 2, head=old)
        data = json.loads(summary_cli(project, "--json").stdout)
        self.assertIn(f"R2.json approved head {old[:12]}, not HEAD {project.head[:12]}", data["status"]["reason"])

    def test_task_lease_rebuilds_finished_lease_from_events(self):
        project = Project(self)
        store = Store(project.state)
        store.renew(task="t1", agent="claude", ttl_hours=1, gates=[".oah/tasks/t1/GATES.md"])
        found = review_summary.task_lease(project.state, "t1")
        self.assertEqual((found.open, found.closed, found.binding, found.gates),
                         (True, None, project.binding, (".oah/tasks/t1/GATES.md",)))
        answer = project.repo.parent / "return.md"
        answer.write_text(f"{project.binding.line()}\nAC1: met — listed\nAC2: met — explained\n", encoding="utf-8")
        (project.repo / ".oah/tasks/t1/GATES.md").write_text(
            "# Gates: t1\n\n- [x] G1: the owner approved\n  EVIDENCE: approved in review\n", encoding="utf-8")
        done = run_cli(["finish", "--task", "t1", "--agent", "claude", "--summary", "done", "--return", str(answer),
                        "--allow-structural"], cwd=project.repo, env=project.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        found = review_summary.task_lease(project.state, "t1")
        self.assertEqual((found.open, found.closed, found.binding, found.gates),
                         (False, "finished", project.binding, (".oah/tasks/t1/GATES.md",)))
        self.assertEqual(pathlib.Path(found.worktree).resolve(), project.repo.resolve())
        self.assertIsNone(review_summary.task_lease(project.state, "other"))


class SummaryCliTest(unittest.TestCase):
    def test_review_still_requires_task_and_runs_rounds(self):
        project = Project(self)
        done = oah(project, "review")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--task", done.stderr)
        done = oah(project, "review", "summary")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--task", done.stderr)
        self.assertIn("summary", oah(project, "review", "--help").stdout)
        done = oah(project, "review", "--task", "t1", "--tier", "high", "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout)["code"], "OAH_REVIEW_CONVERGED")
        self.assertEqual(git(project.repo, "rev-parse", "HEAD"), project.head)


if __name__ == "__main__":
    unittest.main()
