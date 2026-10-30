# 작업 예산 관문 검사 — 실제 임시 저장소와 가짜 엔진으로 모든 호출 경로의 정지·계수·손상 거부를 확인한다.
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from oah import call, call_cli
from oah.engines.proc import RunResult
from oah.lockfile import StateLock
from oah.review import ReviewError, context, receipts, rounds, runner
from oah.secrets import NoKeychain
from tests.test_call import Project as CallProject
from tests.test_fix import FixProject, state_snapshot
from tests.test_review_cli import oah
from tests.test_review_rounds import bug, git


def prepare(project, stage="correction", **fields):
    tools = call_cli.intent_tools()
    return call.prepare(call.CallRequest(engine="codex", stage=stage, task="t1", **fields),
                        call_cli.load_settings(project.repo), root=project.repo, state_dir=project.state,
                        env=project.env, footer=tools.footer, has_footer=tools.has_footer,
                        keychain=NoKeychain())


def advance_budget_base(project):
    """Move the default merge-base from unlimited A to limited B; HEAD omits the limit."""
    config = project.repo / ".oah/config.toml"
    original = config.read_bytes()
    base = project.commit({".oah/config.toml": original + b"[review]\nmax_rounds = 1\n"})
    project.commit({".oah/config.toml": original})
    git(project.repo, "update-ref", "refs/remotes/origin/main", base)
    return base


@contextlib.contextmanager
def budget_git_fault(base, fault):
    """Fail only the historical budget read, leaving live Git and engine execution real."""
    marker = "private-fixture-marker"
    run_git, run_command = runner.run_git, runner.run_command

    def inspect(root, *args):
        path_probe = (args == ("cat-file", "-e", f"{base}:{runner.CONFIG_PATH}") or
                      args == ("ls-tree", "-z", base, "--", runner.CONFIG_PATH))
        object_probe = (args == ("cat-file", "-e", base) or
                        args == ("cat-file", "-e", f"{base}^{{commit}}") or
                        args == ("rev-parse", "--verify", "--quiet", "--end-of-options", f"{base}^{{commit}}"))
        if path_probe and fault not in ("blob", "object") or object_probe and fault == "object":
            if fault == "os-error":
                raise OSError(marker)
            return RunResult(None if fault == "timeout" else 1 if fault == "silent" else 128,
                             "" if fault == "silent" else marker,
                             "" if fault == "silent" else marker, fault == "timeout")
        return run_git(root, *args)

    def read(argv, **kwargs):
        if fault == "blob" and list(argv[-3:]) == ["cat-file", "blob", f"{base}:{runner.CONFIG_PATH}"]:
            return RunResult(128, marker, marker, False)
        return run_command(argv, **kwargs)

    with mock.patch.object(context, "run_git", side_effect=inspect), \
            mock.patch.object(runner, "run_git", side_effect=inspect), \
            mock.patch.object(runner, "run_command", side_effect=read):
        yield marker


def log_old_base(project, base, limit=None):
    receipts.log_round(project.state, "t1", {"event": "finish", "base": base, "round": 2,
                                             "series_round": 1, "counted": False,
                                             "code": "OAH_REVIEW_ENGINE_FAILED", "max_rounds": limit})


class BudgetGateTest(unittest.TestCase):
    def test_git_inspection_errors_stop_every_paid_path(self):
        for mode in ("foreground", "background", "fix", "review"):
            for fault in ("timeout", "error", "silent", "blob", "object", "os-error"):
                with self.subTest(mode=mode, fault=fault):
                    project = FixProject(self)
                    if mode == "fix":
                        project.changes_requested()
                    original = (project.repo / runner.CONFIG_PATH).read_bytes()
                    base = project.commit({runner.CONFIG_PATH: original + b"[review]\nmax_rounds = 1\n"})
                    project.commit({runner.CONFIG_PATH: original})
                    log_old_base(project, base)
                    before = (project.folder() / "rounds.jsonl").read_bytes()
                    terminal = io.StringIO()
                    with budget_git_fault(base, fault) as marker, contextlib.redirect_stderr(terminal):
                        with self.assertRaises(Exception) as caught:
                            if mode == "fix":
                                project.fix()
                            elif mode == "review":
                                project.review()
                            else:
                                pending = prepare(project, tier="low")
                                if mode == "foreground":
                                    call.execute(pending, base_env=project.env)
                                else:
                                    run_id = call.start_background(pending, base_env=project.env)
                                    call.wait(project.state, run_id, timeout=30)
                    self.assertEqual(getattr(caught.exception, "code", None), "OAH_REVIEW_GIT_FAILED")
                    self.assertIn("Git", str(caught.exception))
                    self.assertIn("retry", str(caught.exception))
                    self.assertNotIn(marker, str(caught.exception) + terminal.getvalue())
                    self.assertEqual((project.folder() / "rounds.jsonl").read_bytes(), before)
                    self.assertEqual((len(project.calls()), len(project.fix_calls())),
                                     (1 if mode == "fix" else 0, 0))
                    self.assertTrue(all(marker.encode() not in value for value in state_snapshot(project.state).values()
                                        if isinstance(value, bytes)))

    def test_confirmed_missing_base_keeps_saved_limits_on_every_paid_path(self):
        for mode in ("foreground", "background", "fix", "review"):
            for limit in (None, 1):
                with self.subTest(mode=mode, limit=limit):
                    project = FixProject(self)
                    if mode == "fix":
                        project.changes_requested()
                    else:
                        project.review()
                        project.commit({"src/next.py": "pass\n"})
                    missing = "f" * 40
                    log_old_base(project, missing, limit)
                    terminal = io.StringIO()

                    def action():
                        if mode == "fix":
                            return project.fix(terminal=terminal)
                        if mode == "review":
                            return project.review()
                        pending = prepare(project, tier="low")
                        if mode == "foreground":
                            return call.execute(pending, base_env=project.env)
                        run_id = call.start_background(pending, base_env=project.env)
                        result = call.wait(project.state, run_id, timeout=30)
                        self.assertEqual(result["status"], "finished", result.get("error"))

                    with contextlib.redirect_stderr(terminal):
                        if limit is not None:
                            with self.assertRaises(Exception) as caught:
                                action()
                            self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
                            self.assertIn(missing[:12], str(caught.exception) + terminal.getvalue())
                            self.assertEqual((len(project.calls()), len(project.fix_calls())), (1, 0))
                        else:
                            action()
                            self.assertIn("OAH_REVIEW_BASE_MISSING", terminal.getvalue())
                            self.assertIn(missing[:12], terminal.getvalue())
                            self.assertEqual(len(project.calls()) + len(project.fix_calls()), 2)
                    self.assertNotIn(missing, terminal.getvalue())

    def test_changed_default_base_limit_reaches_every_paid_path(self):
        for mode in ("initial", "correction", "review-call", "background", "fix", "review", "explicit-review"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                project.changes_requested()
                advance_budget_base(project)
                if mode in ("fix", "review", "explicit-review"):
                    action = project.fix if mode == "fix" else lambda: project.review(
                        **({"base": project.base} if mode == "explicit-review" else {}))
                    with self.assertRaises(ReviewError) as caught:
                        action()
                    self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
                else:
                    stage = "review" if mode == "review-call" else "correction" if mode == "background" else mode
                    args = ["call", "codex", "--stage", stage, "--task", "t1", "--tier", "low"]
                    if stage == "initial":
                        args.append("--override-stage")
                    if mode == "background":
                        args.append("--background")
                    done = oah(project, *args)
                    self.assertEqual(done.returncode, 1, done.stderr)
                    self.assertIn("OAH_REVIEW_ROUNDS_EXHAUSTED", done.stderr)
                self.assertIn(1, [line.get("max_rounds") for line in receipts.read_round_log(project.state, "t1")])
                self.assertEqual((len(project.calls()), len(project.fix_calls())), (1, 0))

    def test_changed_default_base_limit_is_rechecked_at_launch(self):
        for mode in ("foreground", "background", "background-handoff"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                project.review()
                pending = prepare(project, tier="low")
                checked = call.check_prepared

                def change_after_check(prepared):
                    checked(prepared)
                    advance_budget_base(project)

                if mode != "background-handoff":
                    advance_budget_base(project)
                with mock.patch.object(call, "check_prepared", side_effect=change_after_check
                                       if mode == "background-handoff" else checked):
                    with self.assertRaises(Exception) as caught:
                        launch = call.execute if mode == "foreground" else call.start_background
                        launch(pending, base_env=project.env)
                self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
                self.assertIn(1, [line.get("max_rounds") for line in receipts.read_round_log(project.state, "t1")])
                self.assertEqual((len(project.calls()), len(project.fix_calls())), (1, 0))

    def test_changed_default_base_limit_is_rechecked_between_paid_steps(self):
        for mode in ("call", "fix", "review"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                if mode == "fix":
                    project.changes_requested()
                else:
                    project.review()
                    project.commit({"src/next.py": "pass\n"})
                original = call._brief_skills

                def change_base(*args, **kwargs):
                    result = original(*args, **kwargs)
                    advance_budget_base(project)
                    return result

                action = project.fix if mode == "fix" else project.review if mode == "review" else lambda: prepare(project)
                with mock.patch.object(call, "_brief_skills", side_effect=change_base):
                    with self.assertRaises(Exception) as caught:
                        action()
                self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
                self.assertEqual((len(project.calls()), len(project.fix_calls())), (1, 0))

    def test_changed_default_base_allows_exactly_one_extra_round(self):
        project = FixProject(self)
        project.review()
        advance_budget_base(project)
        with self.assertRaises(Exception) as caught:
            prepare(project, tier="low")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        self.assertEqual(project.review(extra_round=True).code, "OAH_REVIEW_CONVERGED")
        self.assertIs(project.receipt(2)["extra_round"], True)
        # The same approval remains free, but the recorded override authorizes no later call.
        self.assertIn("already approved", project.review().message)
        with self.assertRaises(Exception) as caught:
            prepare(project, tier="low")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        project.commit({"src/next.py": "pass\n"})
        with self.assertRaises(ReviewError) as caught:
            project.review()
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        self.assertEqual((len(project.calls()), len(project.fix_calls())), (2, 0))

    def test_fix_limit_append_does_not_replace_changed_captured_bytes(self):
        project = FixProject(self)
        project.changes_requested()
        config = project.repo / ".oah/config.toml"
        project.commit({".oah/config.toml": config.read_bytes() + b"[review]\nmax_rounds = 2\n"})
        resolve = rounds.resolve_task_budget
        changed = []

        def change(*args, **kwargs):
            path = project.folder() / "rounds.jsonl"
            lines = [json.loads(line) for line in path.read_bytes().splitlines()]
            lines[-1]["message"] = "s" + "k-private-fixture-value-never-exported"
            path.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
            changed.append(state_snapshot(project.state))
            return resolve(*args, **kwargs)

        with mock.patch.object(rounds, "resolve_task_budget", side_effect=change):
            with self.assertRaises(ReviewError) as caught:
                project.fix()
        self.assertEqual(caught.exception.code, "OAH_FIX_STATE_CHANGED")
        self.assertNotIn("private-fixture", str(caught.exception))
        self.assertEqual(state_snapshot(project.state), changed[0])
        self.assertEqual(project.fix_calls(), [])

    def test_fix_scans_stop_text_before_persisting_a_new_limit(self):
        project = FixProject(self)
        project.changes_requested()
        config = project.repo / ".oah/config.toml"
        project.commit({".oah/config.toml": config.read_bytes() + b"[review]\nmax_rounds = 2\n"})
        path = project.folder() / "rounds.jsonl"
        lines = [json.loads(line) for line in path.read_bytes().splitlines()]
        marker = "s" + "k-private-fixture-value-never-exported"
        lines[-1]["message"] = marker
        path.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
        before = state_snapshot(project.state)
        with self.assertRaises(ReviewError) as caught:
            project.fix()
        self.assertEqual(caught.exception.code, "OAH_FIX_SENSITIVE_INPUT")
        self.assertNotIn(marker, str(caught.exception))
        self.assertEqual(state_snapshot(project.state), before)
        self.assertEqual(project.fix_calls(), [])

    def test_corrupt_log_at_fix_spawn_keeps_the_state_invalid_code(self):
        project = FixProject(self)
        project.changes_requested()
        execute = call.execute
        damaged = []

        def corrupt(*args, **kwargs):
            path = project.folder() / "rounds.jsonl"
            raw = path.read_bytes() + b'{"event":"unknown","message":{"private-fixture-marker":1}}\n'
            path.write_bytes(raw)
            damaged.append(raw)
            return execute(*args, **kwargs)

        with mock.patch.object(call, "execute", side_effect=corrupt):
            with self.assertRaises(ReviewError) as caught:
                project.fix()
        self.assertEqual(caught.exception.code, "OAH_REVIEW_STATE_INVALID")
        self.assertNotIn("private-fixture-marker", str(caught.exception))
        self.assertEqual((project.folder() / "rounds.jsonl").read_bytes(), damaged[0])
        self.assertEqual(project.fix_calls(), [])

    def test_base_budget_applies_to_calls_before_any_structured_review(self):
        project = FixProject(self, config="[review]\nmax_rounds = 1\n")
        config = project.repo / ".oah/config.toml"
        project.commit({".oah/config.toml": config.read_bytes().replace(b"max_rounds = 1", b"")})
        call.execute(prepare(project, stage="review", tier="low"), base_env=project.env)
        with self.assertRaises(Exception) as caught:
            prepare(project, tier="low")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")

    def test_new_limits_are_rechecked_between_paid_preparation_steps(self):
        for mode in ("call", "fix", "review"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                if mode == "fix":
                    project.changes_requested()
                else:
                    project.review()
                    project.commit({"src/next.py": "pass\n"})
                original = call._brief_skills

                def lower(*args, **kwargs):
                    result = original(*args, **kwargs)
                    config = project.repo / ".oah/config.toml"
                    project.commit({".oah/config.toml": config.read_bytes() + b"[review]\nmax_rounds = 1\n"})
                    return result

                action = project.fix if mode == "fix" else project.review if mode == "review" else lambda: prepare(project)
                with mock.patch.object(call, "_brief_skills", side_effect=lower):
                    with self.assertRaises(Exception) as caught:
                        action()
                self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
                self.assertEqual((len(project.calls()), len(project.fix_calls())), (1, 0))

    def test_corruption_between_paid_steps_blocks_every_managed_path(self):
        for mode in ("call", "fix", "review"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                if mode == "fix":
                    project.changes_requested()
                original = call._brief_skills
                damaged = []

                def corrupt(*args, **kwargs):
                    result = original(*args, **kwargs)
                    path = project.folder() / "rounds.jsonl"
                    raw = path.read_bytes() + b'{"event":"unknown","message":{"private-fixture-marker":1}}\n'
                    path.write_bytes(raw)
                    damaged.append(raw)
                    return result

                action = project.fix if mode == "fix" else project.review if mode == "review" else lambda: prepare(project)
                with mock.patch.object(call, "_brief_skills", side_effect=corrupt):
                    with self.assertRaises(Exception) as caught:
                        action()
                self.assertEqual(caught.exception.code, "OAH_REVIEW_STATE_INVALID")
                self.assertNotIn("private-fixture-marker", str(caught.exception))
                self.assertEqual((project.folder() / "rounds.jsonl").read_bytes(), damaged[0])
                self.assertEqual((len(project.calls()), len(project.fix_calls())), (1 if mode == "fix" else 0, 0))

    def test_valid_stop_text_is_not_interpolated_into_diagnostics(self):
        project = FixProject(self)
        project.review()
        path = project.folder() / "rounds.jsonl"
        lines = [json.loads(line) for line in path.read_bytes().splitlines()]
        lines[-1].update(counted=False, code="OAH_REVIEW_NEEDS_DECISION", message="private-fixture-marker")
        path.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
        done = oah(project, "review", "--task", "t1")
        self.assertIn("OAH_REVIEW_NEEDS_DECISION", done.stderr)
        self.assertNotIn("private-fixture-marker", done.stdout + done.stderr)

    def test_review_honours_a_limit_added_after_unlimited_reviews(self):
        project = FixProject(self)
        project.review()
        config = project.repo / ".oah/config.toml"
        project.commit({".oah/config.toml": config.read_bytes() + b"[review]\nmax_rounds = 1\n"})
        with self.assertRaises(ReviewError) as caught:
            project.review()
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        self.assertEqual(len(project.calls()), 1)

    def test_finish_stop_fields_are_validated_before_decision_override(self):
        project = FixProject(self)
        project.review()
        path = project.folder() / "rounds.jsonl"
        original = [json.loads(line) for line in path.read_bytes().splitlines()]
        for field in ("message", "receipt"):
            with self.subTest(field=field):
                lines = [dict(line) for line in original]
                lines[-1].update(code="OAH_REVIEW_NEEDS_DECISION", counted=False)
                lines[-1][field] = {"private-fixture-marker": "must never be echoed"}
                invalid = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
                path.write_bytes(invalid)
                with self.assertRaises(ReviewError) as caught:
                    receipts.read_round_log(project.state, "t1")
                self.assertEqual(caught.exception.code, "OAH_REVIEW_STATE_INVALID")
                done = oah(project, "review", "--task", "t1", "--decision", "continue")
                self.assertIn("OAH_REVIEW_STATE_INVALID", done.stderr)
                self.assertNotIn("private-fixture-marker", done.stdout + done.stderr)
                self.assertEqual(path.read_bytes(), invalid)

    def test_new_limit_is_shared_and_persisted_even_when_dispatch_refuses(self):
        project = FixProject(self)
        project.changes_requested()
        config = project.repo / ".oah/config.toml"
        original = config.read_bytes()
        project.commit({".oah/config.toml": original + b"[review]\nmax_rounds = 1\n"})
        for args in (("review", "--task", "t1"), ("fix", "--task", "t1"),
                     ("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low")):
            done = oah(project, *args)
            self.assertIn("OAH_REVIEW_ROUNDS_EXHAUSTED", done.stderr)
        self.assertTrue(any(line.get("max_rounds") == 1
                            for line in receipts.read_round_log(project.state, "t1")))
        project.commit({".oah/config.toml": original})
        with self.assertRaises(ReviewError) as caught:
            project.review(decision="continue without corrections")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        self.assertEqual(project.review(extra_round=True, decision="continue without corrections").code,
                         "OAH_REVIEW_CONVERGED")
        for args in (("fix", "--task", "t1"),
                     ("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low")):
            done = oah(project, *args)
            self.assertIn("OAH_REVIEW_ROUNDS_EXHAUSTED", done.stderr)
        self.assertEqual((len(project.calls()), len(project.fix_calls())), (2, 0))

    def test_round_log_schema_rejects_all_malformed_consumed_fields(self):
        project = FixProject(self)
        project.review()
        path = project.folder() / "rounds.jsonl"
        original = [json.loads(line) for line in path.read_bytes().splitlines()]
        mutations = [(field, value) for field in ("message", "receipt")
                     for value in ({"private-fixture-marker": "hidden"}, ["private-fixture-marker"], None, 17)]
        mutations += [("event", "private-fixture-marker"), ("task", "private-fixture-marker"),
                      ("head", []), ("outcome", {}), ("at", None), ("attempt", 0),
                      ("extra_attempt", 1), ("decision_for", []), ("unknown", "private-fixture-marker")]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                lines = [dict(line) for line in original]
                lines[-1].update(code="OAH_REVIEW_NEEDS_DECISION", counted=False)
                lines[-1][field] = value
                invalid = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
                path.write_bytes(invalid)
                for args in (("review", "--task", "t1", "--decision", "continue", "--extra-round"),
                             ("fix", "--task", "t1"),
                             ("call", "codex", "--stage", "review", "--task", "t1", "--tier", "low")):
                    done = oah(project, *args)
                    self.assertIn("OAH_REVIEW_STATE_INVALID", done.stderr)
                    self.assertNotIn("private-fixture-marker", done.stdout + done.stderr)
                    self.assertEqual(path.read_bytes(), invalid)
        self.assertEqual((len(project.calls()), len(project.fix_calls())), (1, 0))

    def test_structured_review_reservations_survive_failure_and_interruption(self):
        for interrupted in (False, "execute", "prepare"):
            with self.subTest(interrupted=interrupted):
                project = FixProject(self, config="[review]\nmax_rounds = 1\n")
                if interrupted:
                    with mock.patch.object(call, "advise" if interrupted == "prepare" else "execute",
                                           side_effect=KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            project.review(tier=None if interrupted == "prepare" else "high")
                else:
                    project.queue({"exit": 3})
                    self.assertEqual(project.review().code, "OAH_REVIEW_ENGINE_FAILED")
                with self.assertRaises(ReviewError) as caught:
                    project.review()
                self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
                self.assertEqual(project.review(extra_round=True).code, "OAH_REVIEW_CONVERGED")
                history = receipts.load(project.state, "t1")
                s = rounds.series(history, receipts.read_round_log(project.state, "t1"), project.base,
                                  head=project.head, is_ancestor=lambda a, b: True)
                self.assertEqual(s.counted_rounds, 2, "the successful receipt reconciles only its own reservation")

    def test_ordinary_calls_before_approval_do_not_disable_the_cache(self):
        for limit in ("", "[review]\nmax_rounds = 1\n"):
            with self.subTest(limit=limit):
                project = FixProject(self, config=limit)
                for stage in ("initial", "correction"):
                    call.execute(prepare(project, stage=stage, tier="low", override_stage=stage == "initial"),
                                 base_env=project.env)
                self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")
                for _ in range(2):
                    done = oah(project, "review", "--task", "t1", "--tier", "high")
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertIn("already approved", done.stdout)
                self.assertEqual(len(project.calls()), 1)
                if not limit:
                    call.execute(prepare(project, tier="low"), base_env=project.env)
                    self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")
                    self.assertEqual(len(project.calls()), 2, "later activity invalidates the old approval")
                    self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")
                    self.assertEqual(len(project.calls()), 2)

    def test_fix_persists_a_new_effective_limit_at_publication(self):
        project = FixProject(self)
        project.changes_requested()
        config = project.repo / ".oah/config.toml"
        original = config.read_bytes()
        project.commit({".oah/config.toml": original + b"[review]\nmax_rounds = 2\n"})
        self.assertEqual(project.fix().code, "OAH_FIX_NO_CHANGE")
        log = receipts.read_round_log(project.state, "t1")
        self.assertTrue(any(line.get("max_rounds") == 2 for line in log))
        project.commit({".oah/config.toml": original})
        call.execute(prepare(project, stage="review", tier="low"), base_env=project.env)
        done = oah(project, "call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("OAH_REVIEW_ROUNDS_EXHAUSTED", done.stderr)

    def test_paid_preparation_holds_the_task_lock_against_another_process(self):
        for name in ("advise", "_brief_skills", "judge_engine"):
            with self.subTest(preparation=name):
                project = CallProject(self, config="[review]\nmax_rounds = 1\n")
                project.bind()
                competing = []
                original = getattr(call, name)

                def advice(*args, **kwargs):
                    done = oah(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low")
                    competing.append(done)
                    return original(*args, **kwargs)

                with mock.patch.object(call, name, side_effect=advice):
                    pending = project.prepare()
                self.assertEqual(len(competing), 1)
                self.assertEqual(competing[0].returncode, 1, competing[0].stderr)
                self.assertIn("OAH_REVIEW_SERIES_BUSY", competing[0].stderr)
                self.assertFalse(project.record.exists())
                self.assertEqual(call.execute(pending, base_env=project.env)["status"], "finished")
                log = receipts.read_round_log(project.state, "t1")
                s = rounds.series(receipts.load(project.state, "t1"), log, "", head="", is_ancestor=lambda a, b: True)
                self.assertEqual(s.counted_rounds, 1, "preparation and engine launch share one reservation")

    def test_publication_without_a_finish_reconciles_and_caches_the_approval(self):
        project = FixProject(self, config="[review]\nmax_rounds = 1\n")
        call.execute(prepare(project, tier="low"), base_env=project.env)
        log_round = receipts.log_round

        def interrupted_finish(state, task, entry):
            if entry.get("event") != "finish":
                log_round(state, task, entry)

        with mock.patch.object(receipts, "log_round", side_effect=interrupted_finish):
            self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")
        log = receipts.read_round_log(project.state, "t1")
        self.assertFalse(any(line["event"] == "finish" for line in log))
        s = rounds.series(receipts.load(project.state, "t1"), log, project.base, head=project.head,
                          is_ancestor=lambda a, b: True)
        self.assertEqual(s.counted_rounds, 1)
        done = oah(project, "review", "--task", "t1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("already approved", done.stdout)
        self.assertEqual(len(project.calls()), 1)

    def test_interrupted_paid_preparation_keeps_its_reservation_on_restart(self):
        project = CallProject(self, config="[review]\nmax_rounds = 1\n")
        with mock.patch.object(call, "advise", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                project.prepare()
        done = oah(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("OAH_REVIEW_ROUNDS_EXHAUSTED", done.stderr)
        self.assertFalse(project.record.exists())

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
        log = receipts.read_round_log(project.state, "t2")
        self.assertEqual([line["event"] for line in log], ["limit", "reservation", "dispatch"])
        self.assertEqual(len({line["run_id"] for line in log if "run_id" in line}), 1)

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
        for mutation in ("empty", "truncated", "foreign", "decision", "max_rounds", "counted", "round", "run_id"):
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
                elif mutation == "run_id":
                    lines[0]["run_id"] = []
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
