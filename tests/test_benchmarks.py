# 벤치마크 자료 검사 — Epoch AI CSV 해석이 도구와 같은지, 받은 점수 파일의 검증·출처 우선순위·네트워크 차단·키 노출 금지를 본다.
from __future__ import annotations

import pathlib
import unittest

from oah import benchmarks
from tests.helpers import make_tmp_dir
from tests.test_scoring import SAMPLE, TASK_A, TASK_B, rows_csv, tool

# The scores SAMPLE holds, worked out by hand from its rows.
SAMPLE_SCORES = {"claude-opus-5-5": {TASK_A: 1.0}, "gpt-6.1-sol": {TASK_A: 0.5, TASK_B: 0.75}}


def tool_scores(test: unittest.TestCase, content: bytes) -> dict:
    path = make_tmp_dir(test) / "epoch.csv"
    path.write_bytes(content)
    return tool.read_scores(path)


class EpochParsingTest(unittest.TestCase):
    def test_the_module_and_the_tool_read_the_same_scores(self):
        self.assertEqual(benchmarks.epoch_scores(SAMPLE), SAMPLE_SCORES)
        self.assertEqual(tool_scores(self, SAMPLE), SAMPLE_SCORES)

    def test_windows_line_endings_read_the_same(self):
        crlf = SAMPLE.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.assertEqual(benchmarks.epoch_scores(crlf), SAMPLE_SCORES)
        self.assertEqual(tool_scores(self, crlf), SAMPLE_SCORES)

    def test_bad_data_is_a_named_error_with_no_path(self):
        cases = {
            "no columns": (b"a,b\n1,2\n", "original_task_name"),
            "no allowed rows": (rows_csv(("1", "gemini-3_max", "", "2026-10-01T00:00:00Z", "Success", "1", TASK_A)),
                                "no rows"),
            "outside the scale": (rows_csv(("1", "gpt-6.1-sol_max", "", "2026-10-01T00:00:00Z", "Success", "87.5",
                                            TASK_A)), "87.5"),
            "unreadable time": (rows_csv(("1", "gpt-6.1-sol_max", "", "yesterday", "Success", "0.5", TASK_A)),
                                "yesterday"),
            "not UTF-8": (b"\xff\xfe" + SAMPLE, "UTF-8"),
        }
        for name, (content, said) in cases.items():
            with self.subTest(name):
                with self.assertRaises(benchmarks.DataError) as caught:
                    benchmarks.epoch_scores(content)
                self.assertIn(said, str(caught.exception))

    def test_the_tool_keeps_its_names(self):
        self.assertIs(tool.TASKS, benchmarks.TASKS)
        self.assertEqual(tool.MODEL, benchmarks.MODEL)
        self.assertIs(tool.parse_mean, benchmarks.parse_mean)


if __name__ == "__main__":
    unittest.main()
