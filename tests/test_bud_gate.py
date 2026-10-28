# 작업 예산 관문 검사 — 실제 임시 저장소와 가짜 엔진으로 모든 호출 경로의 정지·계수·손상 거부를 확인한다.
from __future__ import annotations

import json
import unittest
from unittest import mock

from oah import call, call_cli
from oah.lockfile import StateLock
from oah.review import ReviewError, receipts
from oah.secrets import NoKeychain
from tests.test_call import Project as CallProject
from tests.test_fix import FixProject
from tests.test_review_cli import oah
from tests.test_review_rounds import bug, git


def prepare(project, stage="correction", **fields):
    tools = call_cli.intent_tools()
    return call.prepare(call.CallRequest(engine="codex", stage=stage, task="t1", **fields),
                        call_cli.load_settings(project.repo), root=project.repo, state_dir=project.state,
                        env=project.env, footer=tools.footer, has_footer=tools.has_footer,
                        keychain=NoKeychain())


class BudgetGateTest(unittest.TestCase):
    def test_every_paid_path_honours_task_stops(self):
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                project = FixProject(self, config="" if stopped else "[review]\nmax_rounds = 1\n")
                project.queue({"findings": [bug(**({"fix_kind": "design"} if stopped else {}))]})
                project.review()
                code = "OAH_REVIEW_NEEDS_DECISION" if stopped else "OAH_REVIEW_ROUNDS_EXHAUSTED"
                for stage in ("initial", "review", "correction"):
                    for background in (False, True):
                        args = ["call", "codex", "--stage", stage, "--task", "t1", "--tier", "low"]
                        if stage == "initial":
                            args.append("--override-stage")
                        if background:
                            args.append("--background")
                        done = oah(project, *args)
                        self.assertEqual(done.returncode, 1, done.stderr)
                        self.assertIn(code, done.stderr)
                done = oah(project, "fix", "--task", "t1")
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn(code, done.stderr)
                with self.assertRaises(ReviewError) as caught:
                    project.review()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual((len(project.calls()), len(project.fix_calls())), (1, 0))

    def test_decision_clears_stop_until_the_next_stop(self):
        project = FixProject(self)
        project.queue({"findings": [bug(fix_kind="design")]}, {"exit": 3},
                      {"findings": [bug(fix_kind="design")]})
        project.review()
        self.assertEqual(project.review(decision="keep this design").code, "OAH_REVIEW_ENGINE_FAILED")
        run = call.execute(prepare(project, tier="low"), base_env=project.env)
        self.assertEqual(run["status"], "finished")
        self.assertEqual(len(project.fix_calls()), 1)
        self.assertEqual(project.review().code, "OAH_REVIEW_NEEDS_DECISION")
        done = oah(project, "call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low")
        self.assertIn("OAH_REVIEW_NEEDS_DECISION", done.stderr)
        self.assertEqual(len(project.fix_calls()), 1)

    def test_a_new_base_does_not_reset_the_task_limit(self):
        project = FixProject(self, config="[review]\nmax_rounds = 1\n")
        project.review()
        other_base = project.head
        project.commit({"src/more.py": "pass\n"})
        with self.assertRaises(ReviewError) as caught:
            project.review(base=other_base)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        self.assertEqual(len(project.calls()), 1)
        self.assertEqual(project.review(base=other_base, extra_round=True).code, "OAH_REVIEW_CONVERGED")
        project.commit({"src/more.py": "x = 1\n"})
        with self.assertRaises(ReviewError) as caught:
            project.review()
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        self.assertEqual(len(project.calls()), 2)

    def test_raw_review_kinds_count_and_survive_restart(self):
        for kind in ("review", "adversarial-review", "high-risk-review"):
            with self.subTest(kind=kind):
                project = CallProject(self, config="[review]\nmax_rounds = 1\n")
                project.run(stage="review", axes={"kind": kind})
                done = oah(project, "call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low")
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn("OAH_REVIEW_ROUNDS_EXHAUSTED", done.stderr)
                self.assertEqual(len(project.runs()), 1)

    def test_raw_review_counts_toward_structured_review_limit(self):
        project = FixProject(self, config="[review]\nmax_rounds = 1\n")
        call.execute(prepare(project, stage="review", tier="low"), base_env=project.env)
        with self.assertRaises(ReviewError) as caught:
            project.review()
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        self.assertEqual(len(project.calls()), 1)
        self.assertEqual(project.review(extra_round=True).code, "OAH_REVIEW_CONVERGED")
        with self.assertRaises(Exception) as caught:
            prepare(project, tier="low")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")

    def test_background_review_is_counted_once_at_engine_start(self):
        project = CallProject(self, config="[review]\nmax_rounds = 2\n")
        project.bind()
        done = oah(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low", "--background")
        self.assertEqual(done.returncode, 0, done.stderr)
        run_id = done.stdout.split("run=", 1)[1].split()[0]
        waited = oah(project, "wait", run_id, "--timeout-seconds", "60")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        log = receipts.read_round_log(project.state, "t1")
        self.assertEqual(len([line for line in log if line["event"] == "dispatch"]), 1)
        project.run(stage="review", tier="max")
        with self.assertRaises(Exception) as caught:
            project.prepare(stage="correction", tier="low")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")

    def test_a_stop_during_background_handoff_keeps_its_named_code(self):
        project = CallProject(self)
        pending = project.prepare(tier="low")
        checked = call.check_prepared

        def stop_after_check(prepared):
            checked(prepared)
            receipts.log_round(project.state, "t1", {"event": "finish", "base": git(project.repo, "rev-parse", "HEAD"),
                                                     "round": 1, "series_round": 1, "counted": False,
                                                     "code": "OAH_REVIEW_NEEDS_DECISION"})

        with mock.patch.object(call, "check_prepared", side_effect=stop_after_check):
            with self.assertRaises(Exception) as caught:
                call.start_background(pending, base_env=project.env)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_NEEDS_DECISION")
        self.assertFalse(project.record.exists())

    def test_a_busy_task_refuses_and_records_zero_engine_calls(self):
        project = CallProject(self)
        pending = project.prepare(tier="low")
        with StateLock(receipts.task_lock_path(project.state, "t1")):
            with self.assertRaises(Exception) as caught:
                call.execute(pending, base_env=project.env)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_SERIES_BUSY")
        self.assertEqual(pending.run.read()["status"], "refused")
        self.assertFalse(project.record.exists())

    def test_the_budget_belongs_to_one_task(self):
        project = CallProject(self, config="[review]\nmax_rounds = 1\n")
        project.run(tier="low")
        project.run(task="t2", tier="low")
        self.assertEqual(len(project.runs()), 1)
        self.assertEqual(len(receipts.read_round_log(project.state, "t2")), 1)

    def test_recorded_limit_survives_config_without_limit(self):
        project = FixProject(self, config="[review]\nmax_rounds = 1\n")
        project.review()
        config = project.repo / ".oah/config.toml"
        project.commit({".oah/config.toml": config.read_text(encoding="utf-8").replace("max_rounds = 1", "")})
        with self.assertRaises(Exception) as caught:
            prepare(project, tier="low")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        other_base = project.head
        project.commit({"src/next.py": "pass\n"})
        with self.assertRaises(ReviewError) as caught:
            project.review(base=other_base)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        self.assertEqual(len(project.calls()), 1)

    def test_prepared_call_is_rechecked_before_foreground_or_background_launch(self):
        for background in (False, True):
            with self.subTest(background=background):
                project = FixProject(self)
                pending = prepare(project, tier="low")
                project.queue({"findings": [bug(fix_kind="design")]})
                project.review()
                with self.assertRaises(Exception) as caught:
                    if background:
                        call.start_background(pending, base_env=project.env)
                    else:
                        call.execute(pending, base_env=project.env)
                self.assertEqual(caught.exception.code, "OAH_REVIEW_NEEDS_DECISION")
                self.assertEqual(len(project.fix_calls()), 0)

    def test_invalid_saved_round_state_refuses_without_changing_bytes(self):
        for mutation in ("empty", "truncated", "foreign", "decision", "max_rounds", "counted", "round"):
            with self.subTest(mutation=mutation):
                project = FixProject(self)
                project.changes_requested()
                path = project.folder() / "rounds.jsonl"
                lines = [json.loads(line) for line in path.read_bytes().splitlines()]
                if mutation == "foreign":
                    lines[0]["task"] = "foreign"
                elif mutation == "decision":
                    lines[0]["decision"] = True
                elif mutation == "max_rounds":
                    lines[0]["max_rounds"] = "1"
                elif mutation == "counted":
                    lines[1]["counted"] = "false"
                elif mutation == "round":
                    lines[0]["round"] = True
                invalid = (b'{"event":' if mutation == "truncated" else
                           b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines))
                if mutation == "empty":
                    invalid = b""
                path.write_bytes(invalid)
                for args in (("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low"),
                             ("fix", "--task", "t1"), ("review", "--task", "t1", "--decision", "continue")):
                    done = oah(project, *args)
                    self.assertNotEqual(done.returncode, 0, done.stdout)
                    self.assertIn("OAH_REVIEW_STATE_INVALID", done.stderr)
                    self.assertEqual(path.read_bytes(), invalid)
                self.assertEqual((len(project.calls()), len(project.fix_calls())), (1, 0))

    def test_invalid_state_diagnostic_does_not_echo_its_contents(self):
        project = FixProject(self)
        project.changes_requested()
        secret = b"".join((b"sk-", b"private-fixture-value-never-exported"))
        path = project.folder() / "rounds.jsonl"
        invalid = b'{"event":"start","task":"' + secret + b'"}\n'
        path.write_bytes(invalid)
        for args in (("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low"),
                     ("review", "--task", "t1"), ("fix", "--task", "t1")):
            done = oah(project, *args)
            self.assertIn("OAH_REVIEW_STATE_INVALID", done.stderr)
            self.assertNotIn(secret.decode(), done.stdout + done.stderr)
            self.assertEqual(path.read_bytes(), invalid)


if __name__ == "__main__":
    unittest.main()
