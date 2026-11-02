# 벤치마크 자료 검사 — Epoch AI CSV 해석이 도구와 같은지, 받은 점수 파일의 검증·출처 우선순위·네트워크 차단·키 노출 금지를 본다.
from __future__ import annotations

import json
import pathlib
import unittest
from unittest import mock

from oah import benchmarks, scoring
from oah.models import Catalog, Entry, ModelInfo
from tests.helpers import make_tmp_dir
from tests.test_scoring import SAMPLE, TASK_A, TASK_B, rows_csv, tool

# The scores SAMPLE holds, worked out by hand from its rows.
SAMPLE_SCORES = {"claude-opus-5-5": {TASK_A: 1.0}, "gpt-6.1-sol": {TASK_A: 0.5, TASK_B: 0.75}}
EPOCH_TASKS = ("Chess Puzzles", "EBR-bench", "Mystery Game Puzzles")
CREDIT = ("Epoch AI, 'Capabilities & benchmarking'. Published online at epoch.ai. Retrieved from "
          "'https://epoch.ai/benchmarks/use-this-data'. Accessed 2 Nov 2026.")
CODEX_MODELS = ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra")
CLAUDE_MODELS = ("sonnet", "opus", "fable")


def epoch_file(scores: dict) -> dict:
    """A stored Epoch AI copy, written out by hand."""
    return {"schema": 1, "source": "Epoch AI", "date": "2026-11-02", "license": "CC BY 4.0",
            "url": "https://epoch.ai/benchmarks/use-this-data", "credit": CREDIT, "scores": scores,
            "fetched_at": "2026-11-02T09:30:00Z", "source_url": "https://epoch.ai/data/benchmarks.csv"}


def aa_file(scores: dict) -> dict:
    """A stored Artificial Analysis copy, written out by hand. The values are made up: no real data is in a test."""
    return {"schema": 1, "source": "Artificial Analysis", "date": "2026-11-02", "url": "https://artificialanalysis.ai/",
            "credit": "Artificial Analysis, https://artificialanalysis.ai/", "min_tasks": 1, "scores": scores,
            "fetched_at": "2026-11-02T09:30:00Z",
            "source_url": "https://artificialanalysis.ai/api/v2/data/llms/models"}


# Made-up values on every codex family and three Claude generations; terra is the cheapest model that clears.
EPOCH = epoch_file({**{model: dict.fromkeys(EPOCH_TASKS, value) for model, value in
                       zip(CODEX_MODELS, (0.1, 0.8, 0.9, 1.0))},
                    **{f"claude-{alias}-5-5": dict.fromkeys(EPOCH_TASKS, value) for alias, value in
                       zip(("haiku", "sonnet", "opus"), (0.1, 0.6, 1.0))},
                    "claude-fable-5-1": dict.fromkeys(EPOCH_TASKS, 0.5)})
CODING = "Coding Index"
AA = aa_file({**{model: {CODING: value} for model, value in zip(CODEX_MODELS, (0.2, 0.4, 0.6, 0.7))},
              **{f"claude-{alias}-5-5": {CODING: value} for alias, value in
                 zip(("haiku", "sonnet", "opus"), (0.2, 0.5, 0.6))},
              "claude-fable-5-1": {CODING: 0.65}})


def entry(engine: str, *names: str) -> Entry:
    return Entry(engine, "1.0.0", "2026-11-02T00:00:00Z", tuple(ModelInfo(name, ("low", "medium", "high"))
                                                                for name in names), "test")


def state_with(test: unittest.TestCase, **files: object) -> Catalog:
    """A state folder holding the given score files, as raw bytes or as JSON."""
    catalog = Catalog(make_tmp_dir(test) / "state")
    catalog.dir.mkdir(parents=True)
    for source, content in files.items():
        raw = content if isinstance(content, bytes) else json.dumps(content).encode("utf-8")
        (catalog.dir / benchmarks.FILES[source]).write_bytes(raw)
    return catalog


def chosen(catalog: Catalog, engine: str, *, fetch_scores: bool = True, rebuilt: bool = False, session=None):
    names = CODEX_MODELS if engine == "codex" else CLAUDE_MODELS
    return benchmarks.data_for(engine, entry(engine, *names), catalog=catalog,
                               settings={"fetch_scores": fetch_scores, "ttl_hours": 24.0},
                               known=scoring.families(), rebuilt=rebuilt, session=session)


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


def source_of(selection) -> str | None:
    return selection.data["source"] if selection.data is not None else None


class StoredFileTest(unittest.TestCase):
    """AC3: a stored file is validated on every read, and a bad one counts as absent with a notice."""

    def tampered(self) -> dict[str, bytes]:
        good = json.dumps(EPOCH)
        cases = {
            "a score of 2": json.dumps(epoch_file({"gpt-6.1-sol": dict.fromkeys(EPOCH_TASKS, 2)})),
            "a NaN": good.replace('"Chess Puzzles": 0.9', '"Chess Puzzles": NaN', 1),
            "a 300-digit number": good.replace('"Chess Puzzles": 0.9', '"Chess Puzzles": ' + "9" * 300, 1),
            "a model name with a slash": json.dumps(epoch_file({"gpt-6.1-sol/../x": dict.fromkeys(EPOCH_TASKS, 0.5)})),
            "a missing credit": json.dumps({key: value for key, value in EPOCH.items() if key != "credit"}),
            "nesting 10,000 deep": '{"schema": 1, "scores": ' + "[" * 10000 + "]" * 10000 + "}",
        }
        for name, text in cases.items():
            self.assertNotEqual(text, good, name)
        return {name: text.encode("utf-8") for name, text in cases.items()}

    def test_the_hand_written_files_are_valid(self):
        for source, content in (("epoch", EPOCH), ("aa", AA)):
            with self.subTest(source=source):
                data, problem = benchmarks.read_stored(source, json.dumps(content).encode("utf-8"))
                self.assertIsNone(problem)
                self.assertEqual(data, content)
        self.assertEqual(benchmarks.read_stored("epoch", None), (None, None), "no file is no problem")

    def test_a_tampered_epoch_file_counts_as_absent_and_the_snapshot_scores(self):
        for name, raw in self.tampered().items():
            with self.subTest(name):
                self.assertIsNone(benchmarks.read_stored("epoch", raw)[0])
                catalog = state_with(self, epoch=raw)
                selection = chosen(catalog, "codex")
                self.assertIsNone(selection.data, "None means the snapshot shipped with oah")
                notices = [line for line in selection.notices if line.startswith("OAH_SCORES_FILE_INVALID:")]
                self.assertEqual(len(notices), 1, selection.notices)
                self.assertIn(str(catalog.dir / "benchmarks-epoch.json"), notices[0])
                self.assertNotIn("9" * 40, notices[0], "a notice never echoes the file")

    def test_a_tampered_artificial_analysis_file_counts_as_absent(self):
        cases = {"a score of 2": aa_file({"gpt-6.1-sol": {CODING: 2}}),
                 "two tasks": aa_file({"gpt-6.1-sol": {CODING: 0.5, "Intelligence Index": 0.5}}),
                 "a min_tasks of 3": {**AA, "min_tasks": 3},
                 "a min_tasks of true": {**AA, "min_tasks": True},
                 "a foreign credit": {**AA, "credit": "someone else"},
                 "an unknown field": {**AA, "raw": "anything"}}
        for name, content in cases.items():
            with self.subTest(name):
                catalog = state_with(self, aa=content, epoch=EPOCH)
                selection = chosen(catalog, "codex")
                self.assertEqual(source_of(selection), "Epoch AI")
                self.assertTrue(any(line.startswith("OAH_SCORES_FILE_INVALID:") for line in selection.notices))

    def test_a_notice_is_said_once_per_command(self):
        catalog = state_with(self, epoch=b"not json")
        session = benchmarks.Session()
        first = chosen(catalog, "codex", session=session)
        second = chosen(catalog, "claude", session=session)
        self.assertEqual(len(first.notices) + len(second.notices), 1)


class PrecedenceTest(unittest.TestCase):
    def test_artificial_analysis_beats_epoch_beats_the_snapshot(self):
        for files, expected in (({"aa": AA, "epoch": EPOCH}, "Artificial Analysis"), ({"epoch": EPOCH}, "Epoch AI"),
                                ({}, None)):
            with self.subTest(files=sorted(files)):
                for engine in ("codex", "claude"):
                    self.assertEqual(source_of(chosen(state_with(self, **files), engine)), expected, engine)

    def test_fetch_scores_false_uses_the_snapshot_with_both_files_present(self):
        catalog = state_with(self, aa=AA, epoch=EPOCH)
        for engine in ("codex", "claude"):
            selection = chosen(catalog, engine, fetch_scores=False, rebuilt=True)
            self.assertIsNone(selection.data)
            self.assertEqual(selection.notices, ())

    def test_a_partial_artificial_analysis_file_falls_back_for_that_engine_only(self):
        partial = aa_file({name: rows for name, rows in AA["scores"].items() if name != "gpt-6.1-sol"})
        catalog = state_with(self, aa=partial, epoch=EPOCH)
        codex = chosen(catalog, "codex", rebuilt=True)
        self.assertEqual(source_of(codex), "Epoch AI", "a partial set never mixes with Epoch")
        unused = [line for line in codex.notices if line.startswith("OAH_SCORES_AA_UNUSED:")]
        self.assertEqual(len(unused), 1)
        self.assertIn("gpt-6.1-sol", unused[0])
        self.assertEqual(source_of(chosen(catalog, "claude", rebuilt=True)), "Artificial Analysis")

    def test_the_unused_notice_comes_once_per_rebuild_only(self):
        partial = aa_file({name: rows for name, rows in AA["scores"].items() if name != "gpt-6.1-sol"})
        catalog = state_with(self, aa=partial, epoch=EPOCH)
        self.assertEqual(chosen(catalog, "codex").notices, (), "a launch that rebuilt nothing stays quiet")

    def test_an_older_listed_generation_need_not_be_scored(self):
        catalog = state_with(self, aa=AA, epoch=EPOCH)
        selection = benchmarks.data_for("codex", entry("codex", *CODEX_MODELS, "gpt-5.6-sol"), catalog=catalog,
                                        settings={"fetch_scores": True, "ttl_hours": 24.0}, known=scoring.families(),
                                        rebuilt=True)
        self.assertEqual(source_of(selection), "Artificial Analysis")

    def test_one_index_scales_from_the_weakest_to_the_strongest(self):
        catalog = state_with(self, aa=AA)
        selection = chosen(catalog, "codex")
        scores = scoring.score_set("codex", entry("codex", *CODEX_MODELS), allowed=(), data=selection.data,
                                   known=scoring.families())
        self.assertEqual({item.model: round(item.score, 1) for item in scores.models},
                         {"gpt-6-luna": 0.0, "gpt-6.1-terra": 40.0, "gpt-6.1-sol": 80.0, "gpt-6-astra": 100.0})
        self.assertEqual((scores.source, scores.date, scores.credit, scores.fetched_at),
                         ("Artificial Analysis", "2026-11-02", "Artificial Analysis, https://artificialanalysis.ai/",
                          "2026-11-02T09:30:00Z"))
