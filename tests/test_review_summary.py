# oah review summary 검사 — 가짜 리뷰어 세 라운드와 직접 쓴 영수증으로 라운드·판정·digest·독립성·노력 검증·게이트·후속 과제·중요 경로 블록과 JSON 을 본다.
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import unittest

from oah.cli import build_parser
from oah.gates import summary as gate_summary
from oah.lease import Store
from oah.review import ReviewError, context, receipts, rounds, runner
from oah.review import settings as review_settings
from oah.review import summary as review_summary
from tests.fixtures.gates.support import run_cli
from tests.helpers import git_env, make_tmp_dir
from tests.test_review_cli import oah
from tests.test_review_rounds import Project, bug, git

SHA_A = "a" * 64
DECIDE = '--decision "<what you decided>"'
LIMIT = "[review]\nmax_rounds = 1\n"
LEDGER = ".oah/tasks/t1/GATES.md"


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


def shown(project) -> dict:
    done = summary_cli(project, "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def handoff_text(data: dict) -> str:
    start = data["markdown"].index("**Handoff")
    return data["markdown"][start:data["markdown"].index("| Round |")]


def spent_start(project, *, series_round: int = 2, base: str | None = None, attempt: int = 1) -> None:
    """Log one reviewer call of a round that did not count, as `oah review` logs it."""
    receipts.log_round(project.state, "t1", {
        "event": "start", "base": base or project.base, "head": project.head, "round": series_round,
        "series_round": series_round, "attempt": attempt,
        "run_id": f"r20261024T000000Z-{series_round:04x}{attempt:04x}",
        "extra_round": False, "extra_attempt": False, "decision": None, "max_rounds": None, "decision_for": None})


def gate_view(project, base: str):
    """The series, settings and budget `oah review` hands to the gate, built from the same calls."""
    records = receipts.load(project.state, "t1")
    log = receipts.read_round_log(project.state, "t1")
    series = rounds.series(records, log, base, head=project.head,
                           is_ancestor=lambda commit, tip: review_summary._is_ancestor(project.repo, commit, tip))
    conf = review_settings.from_config(runner.config_at(project.repo, base))
    budget = rounds.resolve_task_budget(series, conf, root=project.repo, state=project.state)
    return series, dataclasses.replace(conf, max_rounds=budget.limit), budget


def real_gate(project, *, base: str | None = None, decision: str | None = None, extra_round: bool = False,
              extra_attempt: bool = False):
    series, conf, budget = gate_view(project, base or project.base)
    return rounds.gate(series, head=project.head, decision=decision, extra_round=extra_round,
                       extra_attempt=extra_attempt, settings=conf, binding=project.binding, budget=budget)


def run_printed_review(project, command: str):
    """Parse the command and resolve its actual range before handing its flags to the real gate."""
    args = build_parser().parse_args(shlex.split(command)[1:])
    if args.command != "review" or args.task != "t1":
        raise AssertionError(command)
    remote, branch = runner._base_settings(runner.load_config(project.repo))
    span = context.resolve(project.repo, args.base, remote=remote, branch=branch)
    return real_gate(project, base=span.base, decision=args.decision, extra_round=args.extra_round,
                     extra_attempt=args.extra_attempt)


class HandoffConvergedTest(unittest.TestCase):
    def test_converged_markdown_and_json_carry_no_handoff(self):
        project = Project(self)
        publish_receipt(project, 1, findings=(finding(1, title="a note"),))
        entry = receipts.load(project.state, "t1").receipts()[-1]
        row = (f"| R1 | `{project.base[:12]}..{project.head[:12]}` | approve | converged | independent | "
               f"effort verified | `codex codex-cli 0.163.0` sha256 `{'c' * 12}` | `{entry.digest}` |")
        expected = (
            "<!-- oah review summary v1 -->\n## Review summary\n\n"
            f"- **Task:** `t1`\n- **Intent:** `{project.binding.line()}`\n- **Head:** `{project.head}`\n"
            f"- **Status:** converged at head `{project.head[:12]}` (R1.json, `{entry.digest}`)\n\n"
            "| Round | Range | Verdict | Outcome | Independence | Effort | Reviewer | Receipt |\n"
            f"|---|---|---|---|---|---|---|---|\n{row}\n\n"
            "**Gates:** no `.oah/tasks/t1/GATES.summary.md` committed at HEAD; run oah gates check t1, then commit "
            "it\n\n**Follow-ups (P2/P3):**\n- R1-F1 P2 tests `src/app.py:1` — `a note`\n\n"
            "**Critical paths touched:**\n- none\n")
        done = summary_cli(project)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, expected)
        data = shown(project)
        self.assertEqual(list(data), ["task", "head", "intent", "status", "rounds", "follow_ups", "critical_paths",
                                      "gates", "problems", "markdown"])
        self.assertEqual(data["markdown"], expected)
        self.assertNotIn("handoff", json.dumps(data).lower())


class HandoffResumeTest(unittest.TestCase):
    def test_waiting_on_a_decision(self):
        project = Project(self)
        publish_receipt(project, 1, outcome="needs-decision", findings=(finding(1, severity="P1"),))
        with self.assertRaises(ReviewError) as caught:
            real_gate(project)
        self.assertEqual(caught.exception.code, rounds.NEEDS_DECISION)
        resume = shown(project)["handoff"]["resume"]
        self.assertEqual(resume["command"], f"oah review --task t1 --base {project.base} {DECIDE}")
        self.assertIsNone(run_printed_review(project, resume["command"]))

    def test_requested_changes_prints_fix_and_fix_passes_its_gate(self):
        project = Project(self)
        publish_receipt(project, 1, outcome="changes-requested", findings=(finding(1, severity="P1"),))
        with self.assertRaises(ReviewError) as caught:
            real_gate(project)
        self.assertEqual(caught.exception.code, rounds.NEEDS_DECISION)
        resume = shown(project)["handoff"]["resume"]
        self.assertEqual(resume["command"], "oah fix --task t1")
        self.assertIsNone(resume["note"])
        self.assertEqual(build_parser().parse_args(shlex.split(resume["command"])[1:]).command, "fix")
        rounds.dispatch_gate(project.state, "t1", project.repo, stage="correction")

    def test_round_limit_prints_extra_round(self):
        project = Project(self, config=LIMIT)
        publish_receipt(project, 1)
        project.commit({"src/app.py": "print('v2')\n"})
        with self.assertRaises(ReviewError) as caught:
            real_gate(project)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {project.base} --extra-round")
        self.assertIsNone(run_printed_review(project, command))

    def test_a_limit_saved_only_in_the_task_log_still_counts(self):
        project = Project(self)
        publish_receipt(project, 1)
        project.commit({"src/app.py": "print('v2')\n"})
        receipts.log_round(project.state, "t1", {"event": "limit", "max_rounds": 1})
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {project.base} --extra-round")
        self.assertIsNone(run_printed_review(project, command))

    def test_requested_changes_at_the_limit_prints_a_command_that_passes(self):
        project = Project(self, config=LIMIT)
        publish_receipt(project, 1, outcome="changes-requested", findings=(finding(1, severity="P1"),))
        with self.assertRaises(ReviewError) as caught:
            rounds.dispatch_gate(project.state, "t1", project.repo, stage="correction")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")  # oah fix is refused at the limit
        with self.assertRaises(ReviewError) as caught:
            real_gate(project, extra_round=True)
        self.assertEqual(caught.exception.code, rounds.NEEDS_DECISION)  # and --extra-round alone is not enough
        resume = shown(project)["handoff"]["resume"]
        self.assertEqual(resume["command"], f"oah review --task t1 --base {project.base} --extra-round {DECIDE}")
        self.assertIn("oah fix is refused at the round limit", resume["note"])
        self.assertIn("oah fix --manual --engine claude|human --task t1", resume["note"])
        self.assertIsNone(run_printed_review(project, resume["command"]))

    def test_a_recorded_correction_at_the_limit_needs_only_the_extra_round(self):
        project = Project(self, config=LIMIT)
        publish_receipt(project, 1, outcome="changes-requested", findings=(finding(1, severity="P1"),))
        project.correct(1)
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {project.base} --extra-round")
        self.assertIsNone(run_printed_review(project, command))

    def test_decision_stop_at_the_limit_prints_both_flags(self):
        project = Project(self, config=LIMIT)
        publish_receipt(project, 1, outcome="needs-decision", findings=(finding(1, severity="P1"),))
        with self.assertRaises(ReviewError) as caught:
            real_gate(project, decision="go on")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {project.base} --extra-round {DECIDE}")
        self.assertIsNone(run_printed_review(project, command))

    def test_out_of_attempts_prints_extra_attempt_with_the_base(self):
        project = Project(self)
        publish_receipt(project, 1)
        project.commit({"src/app.py": "print('v2')\n"})
        spent_start(project)
        spent_start(project, attempt=2)
        with self.assertRaises(ReviewError) as caught:
            real_gate(project)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ATTEMPTS_EXHAUSTED")
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {project.base} --extra-attempt")
        self.assertIsNone(run_printed_review(project, command))

    def test_attempts_use_the_setting_pinned_to_the_base(self):
        project = Project(self, config="[review]\nround_attempts = 1\n")
        publish_receipt(project, 1)
        project.commit({"src/app.py": "print('v2')\n"})
        spent_start(project)
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {project.base} --extra-attempt")
        self.assertIsNone(run_printed_review(project, command))

    def test_attempts_spent_on_a_newer_base_are_seen(self):
        project = Project(self)
        publish_receipt(project, 1)
        newer = project.head
        project.commit({"src/app.py": "print('v2')\n"})
        spent_start(project, series_round=1, base=newer)
        spent_start(project, series_round=1, base=newer, attempt=2)
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {newer} --extra-attempt")
        self.assertIsNone(run_printed_review(project, command))

    def test_only_a_review_of_head_is_needed(self):
        project = Project(self)
        publish_receipt(project, 1)
        project.commit({"src/app.py": "print('v2')\n"})
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {project.base}")
        self.assertIsNone(run_printed_review(project, command))

    def test_last_explicit_base_is_kept_when_default_attempts_are_exhausted(self):
        project = Project(self)
        publish_receipt(project, 1)
        newer = project.head
        project.commit({"src/app.py": "print('v2')\n"})
        spent_start(project)
        spent_start(project, attempt=2)
        spent_start(project, base=newer, series_round=1)
        with self.assertRaises(ReviewError) as caught:
            run_printed_review(project, "oah review --task t1")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ATTEMPTS_EXHAUSTED")
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {newer}")
        self.assertIsNone(run_printed_review(project, command))

    def test_explicit_base_is_kept_when_default_resolution_is_unavailable(self):
        project = Project(self, config='[preflight]\ndefault_branch = "unavailable"\n')
        publish_receipt(project, 1)
        project.commit({"src/app.py": "print('v2')\n"})
        with self.assertRaises(ReviewError) as caught:
            run_printed_review(project, "oah review --task t1")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_BASE_INVALID")
        command = shown(project)["handoff"]["resume"]["command"]
        self.assertEqual(command, f"oah review --task t1 --base {project.base}")
        self.assertIsNone(run_printed_review(project, command))

    def test_broken_history_prints_no_command(self):
        project = Project(self)
        publish_receipt(project, 1)
        publish_receipt(project, 2, outcome="changes-requested", findings=(finding(2, severity="P1"),))
        target = project.state / "reviews" / "t1" / "R1.json"
        target.write_bytes(target.read_bytes().replace(b'"fixture"', b'"edited"'))
        with self.assertRaises(ReviewError):
            real_gate(project, decision="go on")  # a decision does not override invalid history
        data = shown(project)
        self.assertIsNone(data["handoff"]["resume"]["command"])
        self.assertIn("**Resume:** no command", handoff_text(data))

    def test_missing_lease_prerequisites_print_no_resume_and_complete_binding_guidance(self):
        for lease_state in ("released", "expired", "unbound"):
            for outcome in ("converged", "changes-requested"):
                with self.subTest(lease=lease_state, outcome=outcome):
                    project = Project(self)
                    publish_receipt(project, 1, outcome=outcome,
                                    findings=(finding(1, severity="P1"),) if outcome == "changes-requested" else ())
                    packet = (project.repo / project.binding.source).read_text(encoding="utf-8")
                    project.commit({"src/app.py": "print('v2')\n",
                                    project.binding.source: packet + "\n## Plan\n\n## Acceptance\n\n## Return\n"})
                    agent = project.lease.agent
                    if lease_state == "expired":
                        path = Store(project.state).leases_path
                        data = json.loads(path.read_text(encoding="utf-8"))
                        data["leases"][0]["expires_at"] = "2000-01-01T00:00:00Z"
                        path.write_text(json.dumps(data), encoding="utf-8")
                    else:
                        done = run_cli(["release", "--task", "t1", "--agent", agent, "--reason", "stopped"],
                                       cwd=project.repo, env=project.env)
                        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                        if lease_state == "unbound":
                            done = run_cli(["start", "--task", "t1", "--agent", agent, "--scope", "src"],
                                           cwd=project.repo, env=project.env)
                            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    for command in ("review", "fix"):
                        done = run_cli([command, "--task", "t1"], cwd=project.repo, env=project.env)
                        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                        self.assertIn("OAH_INTENT_NOT_CURRENT", done.stderr)
                    data = shown(project)
                    resume = data["handoff"]["resume"]
                    self.assertIsNone(resume["command"])
                    self.assertIn("**Resume:** no command", handoff_text(data))
                    self.assertIn("Prerequisite:", resume["note"])
                    guidance = resume["note"].split("`", 2)[1]
                    argv = shlex.split(guidance)[1:]
                    args = build_parser().parse_args(argv)
                    self.assertEqual((args.command, args.task, args.agent, args.packet),
                                     ("start", "t1", "<you>", ".oah/tasks/t1/PACKET.md"))
                    argv[argv.index("--agent") + 1] = agent
                    done = run_cli(argv, cwd=project.repo, env=project.env)
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    runner._check_intent(project.repo, runner._lease(project.state, "t1"))
                    resumed = shown(project)["handoff"]["resume"]["command"]
                    if outcome == "converged":
                        self.assertIsNone(run_printed_review(project, resumed))
                    else:
                        self.assertEqual(resumed, "oah fix --task t1")
                        rounds.dispatch_gate(project.state, "t1", project.repo, stage="correction")

    def test_summary_records_nothing(self):
        project = Project(self, config=LIMIT)
        publish_receipt(project, 1)
        project.commit({"src/app.py": "print('v2')\n"})
        log = project.folder() / receipts.ROUNDS_NAME
        self.assertFalse(log.exists())
        self.assertEqual(shown(project)["handoff"]["resume"]["command"],
                         f"oah review --task t1 --base {project.base} --extra-round")
        self.assertFalse(log.exists())  # a newly seen limit is recorded only by the gates that dispatch


class HandoffHeadTest(unittest.TestCase):
    def test_head_past_the_latest_receipt_is_named(self):
        project = Project(self)
        publish_receipt(project, 1, outcome="changes-requested", findings=(finding(1, severity="P1"),))
        reviewed = project.head
        project.commit({"src/app.py": "print('v2')\n"})
        data = shown(project)
        self.assertEqual(data["handoff"]["reviewed"], {"receipt": "R1.json", "round": 1, "head": reviewed,
                                                       "verdict": "request_changes"})
        self.assertEqual(data["handoff"]["head"], project.head)
        self.assertTrue(data["handoff"]["head_moved"])
        block = handoff_text(data)
        self.assertIn(f"**Last reviewed head:** `{reviewed}` (R1, verdict request_changes)", block)
        self.assertIn(f"HEAD is `{project.head}`, a different commit", block)
        self.assertNotRegex(block.lower(), "verified|approved")

    def test_head_at_the_latest_receipt_is_not_called_moved(self):
        project = Project(self)
        publish_receipt(project, 1, outcome="changes-requested", findings=(finding(1, severity="P1"),))
        data = shown(project)
        self.assertFalse(data["handoff"]["head_moved"])
        self.assertNotIn("different commit", handoff_text(data))

    def test_no_valid_receipt_says_so(self):
        project = Project(self)
        publish_receipt(project, 1)
        target = project.state / "reviews" / "t1" / "R1.json"
        target.write_bytes(target.read_bytes().replace(b'"fixture"', b'"edited"'))
        data = shown(project)
        self.assertIsNone(data["handoff"]["reviewed"])
        self.assertEqual(data["handoff"]["findings"], [])
        self.assertIn("**Last reviewed head:** none", handoff_text(data))


class HandoffFindingsTest(unittest.TestCase):
    def test_blocking_findings_are_quoted_and_lower_ones_left_out(self):
        project = Project(self)
        title = "ping @someone about `x` | and\nmore"
        publish_receipt(project, 1, outcome="changes-requested", findings=(
            finding(1, 1, severity="P0", title=title, location="src/a|b.py:2 @x", axis="security"),
            finding(1, 2, severity="P1", title="second"),
            finding(1, 3, severity="P2", title="minor-marker"),
            finding(1, 4, severity="P3", title="trivial-marker")))
        data = shown(project)
        self.assertEqual([(item["finding_id"], item["severity"]) for item in data["handoff"]["findings"]],
                         [("R1-F1", "P0"), ("R1-F2", "P1")])
        block = handoff_text(data)
        self.assertIn("- `R1-F1` P0 `security` `src/a|b.py:2 @x` — ``ping @someone about `x` | and more``", block)
        self.assertIn("- `R1-F2` P1 `tests` `src/app.py:1` — `second`", block)
        self.assertIn("latest receipt (R1.json)", block)
        self.assertNotRegex(block.lower(), r"\bopen\b")
        self.assertNotIn("minor-marker", block)
        self.assertNotIn("trivial-marker", block)

    def test_hostile_finding_ids_and_axes_cannot_escape_the_handoff_line(self):
        project = Project(self)
        hostile_id = "@someone [link](https://example.invalid) `id`\n\n## injected-id"
        hostile_axis = "@team [link](https://example.invalid) ``axis``\n\n## injected-axis"
        item = finding(1, severity="P1", axis=hostile_axis)
        item["finding_id"] = hostile_id
        publish_receipt(project, 1, outcome="changes-requested", findings=(item,))
        data = shown(project)
        self.assertEqual(data["handoff"]["findings"][0]["finding_id"], hostile_id)
        self.assertEqual(data["handoff"]["findings"][0]["axis"], hostile_axis)
        block = handoff_text(data)
        quoted_id = "``@someone [link](https://example.invalid) `id` ## injected-id``"
        quoted_axis = "```@team [link](https://example.invalid) ``axis`` ## injected-axis```"
        self.assertIn(f"  - {quoted_id} P1 {quoted_axis} `src/app.py:1` — `a follow-up`\n", block)
        remainder = block.replace(quoted_id, "").replace(quoted_axis, "")
        self.assertNotIn("@", remainder)
        self.assertNotIn("[link]", remainder)
        self.assertNotIn("\n## injected", block)

    def test_no_blocking_finding_says_none(self):
        project = Project(self)
        publish_receipt(project, 1, findings=(finding(1, title="minor-marker"),))
        project.commit({"src/app.py": "print('v2')\n"})
        data = shown(project)
        self.assertEqual(data["handoff"]["findings"], [])
        section = handoff_text(data).split("**P0/P1 findings", 1)[1].split("**Gates not met", 1)[0]
        self.assertIn("  - none\n", section)


class HandoffGatesTest(unittest.TestCase):
    MARKED = ("OWNS: src/owns-marker\n# Gates: heading-marker\n\n"
              "- [ ] G1: title-marker\n  CHECK: echo check-marker\n  EXPECT: expect-marker\n  EVIDENCE: pending\n"
              "- [x] G2: title-marker\n  CHECK: echo check-marker\n  EXPECT: expect-marker\n"
              "  EVIDENCE: evidence-marker\n"
              "- [x] G3: title-marker\n  EVIDENCE: evidence-marker\n"
              "- [x] G4: title-marker\n  EVIDENCE: pending\n"
              "- [ ] G5: title-marker\n\nABANDON: G5 reason-marker\n")

    def stopped(self, *declared: str) -> Project:
        project = Project(self)
        publish_receipt(project, 1, outcome="changes-requested", findings=(finding(1, severity="P1"),))
        if declared:
            Store(project.state).renew(task="t1", agent="claude", ttl_hours=1, gates=list(declared))
        return project

    def write(self, project: Project, relative: str, text: str) -> None:
        target = project.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def assert_private(self, project: Project, *secrets: str) -> None:
        text = summary_cli(project)
        as_json = summary_cli(project, "--json")
        self.assertEqual((text.returncode, as_json.returncode), (0, 0), text.stderr + as_json.stderr)
        for secret in ("-marker", str(project.repo), "GATES.md", *secrets):
            self.assertNotIn(secret, text.stdout)
            self.assertNotIn(secret, as_json.stdout)

    def test_states_are_listed_without_any_ledger_text(self):
        project = self.stopped()
        self.write(project, LEDGER, self.MARKED)
        data = shown(project)
        self.assertEqual(data["handoff"]["gates"], {"reading": "structural", "rerun": False,
                                                    "freshness_checked": False, "ledgers": [{
            "position": 1, "state": "read", "gates": [
                {"id": "G1", "state": "unmet"}, {"id": "G2", "state": "stale-unmet"},
                {"id": "G4", "state": "unmet-no-evidence"}, {"id": "G5", "state": "abandoned"}]}]})
        block = handoff_text(data)
        self.assertIn("  - `G1` unmet, `G2` stale-unmet, `G4` unmet-no-evidence, `G5` abandoned\n", block)
        self.assertNotIn("`G3`", block)
        self.assert_private(project)

    def test_the_reading_says_nothing_was_run_even_with_none_left(self):
        project = self.stopped()
        self.write(project, LEDGER, "# Gates: t1\n\n- [x] G1: done\n  EVIDENCE: a person looked\n")
        data = shown(project)
        self.assertEqual(data["handoff"]["gates"]["ledgers"][0]["gates"], [])
        block = handoff_text(data)
        self.assertIn("  - none in this reading\n", block)
        self.assertIn("structural reading", block)
        self.assertIn("Nothing was re-run", block)
        self.assertIn("evidence freshness was not checked", block)
        self.assertIn("a met box can still be stale", block)

    def test_a_missing_ledger_says_so(self):
        project = self.stopped()
        data = shown(project)
        self.assertEqual(data["handoff"]["gates"]["ledgers"], [{"position": 1, "state": "missing", "gates": []}])
        self.assertIn("  - the ledger is missing\n", handoff_text(data))
        self.assert_private(project)

    def test_an_unreadable_ledger_says_so_without_its_path(self):
        project = self.stopped()
        (project.repo / LEDGER).mkdir(parents=True)  # reading a folder fails with an error that names the path
        data = shown(project)
        self.assertEqual(data["handoff"]["gates"]["ledgers"], [{"position": 1, "state": "unreadable", "gates": []}])
        self.assertIn("  - the ledger could not be read\n", handoff_text(data))
        self.assert_private(project)

    def test_an_invalid_ledger_could_not_be_read_either(self):
        project = self.stopped()
        self.write(project, LEDGER, "- [ ] no id title-marker\n  CHECK: echo check-marker\n")
        data = shown(project)
        self.assertEqual(data["handoff"]["gates"]["ledgers"], [{"position": 1, "state": "unreadable", "gates": []}])
        self.assert_private(project)

    def test_two_ledgers_are_told_apart_by_position(self):
        second = "notes/path-marker/second.md"
        project = self.stopped(LEDGER, second, "gone/absent-marker.md")
        self.write(project, LEDGER, self.MARKED)
        self.write(project, second, "- [ ] G1: title-marker\n")
        data = shown(project)
        self.assertEqual([item["position"] for item in data["handoff"]["gates"]["ledgers"]], [1, 2, 3])
        self.assertEqual(data["handoff"]["gates"]["ledgers"][1]["gates"], [{"id": "G1", "state": "unmet"}])
        block = handoff_text(data)
        self.assertIn("  - ledger 1: `G1` unmet, ", block)
        self.assertIn("  - ledger 2: `G1` unmet\n", block)
        self.assertIn("  - ledger 3 is missing\n", block)
        self.assert_private(project, "second.md", "notes/")

    def test_the_task_worktree_holds_the_ledger(self):
        project = self.stopped()
        self.write(project, LEDGER, "- [ ] G1: title-marker\n")
        other = project.repo.parent / "other-checkout"
        git(project.repo, "worktree", "add", "-q", "--detach", str(other))
        self.addCleanup(git, project.repo, "worktree", "remove", "--force", str(other))
        done = run_cli(["review", "summary", "--task", "t1", "--json"], cwd=other, env=project.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout)["handoff"]["gates"]["ledgers"][0]["gates"],
                         [{"id": "G1", "state": "unmet"}])


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
