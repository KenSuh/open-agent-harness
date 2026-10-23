# 실행 기록 검사 — 폴더 배치, result.json 병합과 unverified 기본값, 알 수 없는 필드 거부, run id 찾기와 잘못된 id 거부를 본다.
from __future__ import annotations

import datetime as dt
import json
import unittest

from oah.runs import RESULT_FIELDS, UNVERIFIED, RunError, find_run, new_run
from tests.helpers import make_tmp_dir

NOW = dt.datetime(2026, 10, 23, 12, 0, 5, tzinfo=dt.timezone.utc)


class RunsTest(unittest.TestCase):
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
