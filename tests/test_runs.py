# 실행 기록 검사 — 폴더 배치, result.json 병합과 unverified 기본값, 알 수 없는 필드 거부, run id 찾기와 잘못된 id 거부를 본다.
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time
import unittest

from oah.lockfile import StateLock
from oah.runs import RESULT_FIELDS, UNVERIFIED, RunError, find_run, new_run
from tests.helpers import directory_alias, make_tmp_dir

NOW = dt.datetime(2026, 10, 23, 12, 0, 5, tzinfo=dt.timezone.utc)


class RunsTest(unittest.TestCase):
    def test_result_status_only_moves_forward(self):
        run = new_run(make_tmp_dir(self), "p3b", "codex", "review")
        run.result(status="running", pid=123)
        run.result(status="queued", pid=456)
        self.assertEqual((run.read()["status"], run.read()["pid"]), ("running", 123))
        finished = run.result(status="finished", exit_code=0)
        self.assertEqual(run.result(status="lost", error="stale observer"), finished)
        self.assertEqual(run.result(status="finished", header_effort="high")["header_effort"], "high")
        for status in (None, "unknown"):
            with self.subTest(status=status), self.assertRaises(RunError):
                run.result(status=status)

    def test_result_merges_use_the_cross_process_state_lock(self):
        state = make_tmp_dir(self)
        run = new_run(state, "p3b", "codex", "review")
        ready = state / "ready"
        script = ("import pathlib,sys; sys.path.insert(0,sys.argv[4]); from oah.runs import Run; "
                  "pathlib.Path(sys.argv[3]).touch(); "
                  "Run(sys.argv[1], 'p3b', pathlib.Path(sys.argv[2])).result(header_effort='high')")
        with StateLock(state / ".lock"):
            child = subprocess.Popen([sys.executable, "-c", script, run.id, str(run.path), str(ready),
                                      str(pathlib.Path(__file__).resolve().parents[1] / "src")])
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                time.sleep(0.15)
                self.assertIsNone(child.poll(), "result merge bypassed the state lock")
                # Emulate another writer in this critical section, without taking the lock twice.
                from oah.atomic import write_json_atomic
                write_json_atomic(run.result_path, dict(run.read(), launch_effort="high"))
            finally:
                self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual((run.read()["launch_effort"], run.read()["header_effort"]), ("high", "high"))

    def _assert_linked_directories_refused(self):
        state, outside = make_tmp_dir(self), make_tmp_dir(self)
        (state / "runs").mkdir()
        directory_alias(outside, state / "runs" / "p3b")
        with self.assertRaises(RunError):
            new_run(state, "p3b", "codex", "review")
        self.assertEqual(list(outside.iterdir()), [])
        escaped = new_run(make_tmp_dir(self), "other", "codex", "review")
        directory_alias(escaped.path, outside / escaped.id)
        with self.assertRaises(RunError):
            find_run(state, escaped.id)

    def test_linked_run_directory_cannot_redirect_lookup(self):
        state = make_tmp_dir(self)
        parent = state / "runs" / "p3b"
        parent.mkdir(parents=True)
        escaped = new_run(make_tmp_dir(self), "other", "codex", "review")
        directory_alias(escaped.path, parent / escaped.id)
        with self.assertRaises(RunError):
            find_run(state, escaped.id)

    @unittest.skipIf(os.name == "nt", "POSIX directory symlinks")
    def test_task_symlink_cannot_redirect_creation_or_lookup(self):
        self._assert_linked_directories_refused()

    @unittest.skipUnless(os.name == "nt", "native Windows junctions")
    def test_task_junction_cannot_redirect_creation_or_lookup(self):
        self._assert_linked_directories_refused()

    def test_runs_root_alias_cannot_escape_state(self):
        state, outside = make_tmp_dir(self), make_tmp_dir(self)
        directory_alias(outside, state / "runs")
        with self.assertRaises(RunError):
            new_run(state, "p3b", "codex", "review")
        with self.assertRaises(RunError):
            find_run(state, "r20000101T000000Z-00000000")
        self.assertEqual(list(outside.iterdir()), [])

    def test_new_run_layout(self):
        state = make_tmp_dir(self)
        run = new_run(state, "p3b", "codex", "review", now=NOW)
        self.assertRegex(run.id, r"^r20261023T120005Z-[0-9a-f]{8}$")
        self.assertEqual(run.path, state / "runs" / "p3b" / run.id)
        self.assertEqual(sorted(p.name for p in run.path.iterdir()), ["result.json"])
        self.assertEqual(list((state / "runs" / "p3b").iterdir()), [run.path])
        data = json.loads(run.result_path.read_text(encoding="utf-8"))
        self.assertEqual(set(data), set(RESULT_FIELDS))
        self.assertEqual((data["status"], data["engine"], data["stage"], data["created_at"]),
                         ("created", "codex", "review", "2026-10-23T12:00:05Z"))
        for name in ("launch_effort", "header_effort", "header_check", "resolved_inference_effort",
                     "normalization_basis", "cli_version", "resolved_model", "requested_effort"):
            self.assertEqual(data[name], UNVERIFIED)

    def test_result_merges_and_defaults_unverified(self):
        run = new_run(make_tmp_dir(self), "p3b", "codex", "review")
        run.write_prompt("hello\n")
        run.write_logs("out", "err")
        run.result(status="finished", exit_code=0, launch_effort="max")
        data = run.result(header_effort=None, duration_s=1.5)
        self.assertEqual((data["status"], data["exit_code"], data["launch_effort"], data["header_effort"]),
                         ("finished", 0, "max", UNVERIFIED))
        self.assertEqual(run.prompt_path.read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(run.stdout_path.read_text(encoding="utf-8"), "out")
        with self.assertRaises(RunError) as ctx:
            run.result(api_key="nope")
        self.assertEqual(ctx.exception.code, "OAH_RUN_FIELD_UNKNOWN")

    def test_find_run(self):
        state = make_tmp_dir(self)
        run = new_run(state, "p3b", "claude", "initial")
        other = new_run(state, "other", "codex", "review")
        self.assertEqual(find_run(state, run.id), run)
        self.assertEqual(find_run(state, other.id).task, "other")
        with self.assertRaises(RunError) as ctx:
            find_run(state, "r20000101T000000Z-00000000")
        self.assertEqual(ctx.exception.code, "OAH_RUN_NOT_FOUND")

    def test_bad_run_id_refuses(self):
        for bad in ("../x", "", "r1", "*"):
            with self.subTest(bad=bad), self.assertRaises(RunError) as ctx:
                find_run(make_tmp_dir(self), bad)
            self.assertEqual(ctx.exception.code, "OAH_RUN_ID_INVALID")
        with self.assertRaises(RunError) as ctx:
            new_run(make_tmp_dir(self), "../escape", "codex", "review")
        self.assertEqual(ctx.exception.code, "OAH_TASK_INVALID")


if __name__ == "__main__":
    unittest.main()
