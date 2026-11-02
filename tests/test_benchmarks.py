# 벤치마크 자료 검사 — Epoch AI CSV 해석이 도구와 같은지, 받은 점수 파일의 검증·출처 우선순위·네트워크 차단·키 노출 금지를 본다.
from __future__ import annotations

import datetime as dt
import io
import json
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

from oah import __version__, benchmarks, scoring
from oah.atomic import StorageError
from oah.models import Catalog, Entry, ModelInfo
from oah.secrets import NoKeychain, Secret
from tests.helpers import FakeOpener, FakeResponse, make_tmp_dir, shipped_defaults, use_opener
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


NOW = dt.datetime(2026, 11, 2, 9, 30, tzinfo=dt.timezone.utc)
EPOCH_URL = "https://epoch.ai/data/benchmarks.csv"
AA_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
KEY_NAME = "ARTIFICIAL_ANALYSIS_API_KEY"
AGENT = {"User-agent": f"oah/{__version__}"}
# A copy fetched the day before NOW, so that a rebuild at NOW is due to fetch again.
STALE_EPOCH = {**EPOCH, "date": "2026-11-01", "fetched_at": "2026-11-01T08:00:00Z",
               "credit": CREDIT.replace("Accessed 2 Nov 2026.", "Accessed 1 Nov 2026.")}
STALE_AA = {**AA, "date": "2026-11-01", "fetched_at": "2026-11-01T08:00:00Z"}
CSV_VALUES = {"gpt-6-luna": 0.1, "gpt-6.1-terra": 0.8, "gpt-6.1-sol": 0.9, "gpt-6-astra": 1.0,
              "claude-sonnet-5-5": 0.6, "claude-opus-5-5": 1.0, "claude-fable-5-1": 0.5}


def epoch_csv(values: dict, *, tasks=EPOCH_TASKS, newline: str = "\n") -> bytes:
    rows = [(f"{model}-{task}", f"{model}_max", "", "2026-10-01T00:00:00Z", "Success", str(value), task)
            for model, value in values.items() for task in tasks]
    return rows_csv(*rows, newline=newline)


def aa_row(creator: str, slug: str, coding: object = None, intelligence: object = None) -> dict:
    """One row as the Artificial Analysis API describes it (unconfirmed against a live reply), with made-up values."""
    evaluations = {name: value for name, value in (("artificial_analysis_coding_index", coding),
                                                   ("artificial_analysis_intelligence_index", intelligence))
                   if value is not None}
    return {"id": f"id-{slug}", "name": slug.upper(), "slug": slug,
            "model_creator": {"id": f"id-{creator}", "name": creator.title(), "slug": creator},
            "evaluations": evaluations, "pricing": {"price_1m_input_tokens": 1.0}}


def aa_body(*rows: dict) -> bytes:
    return json.dumps({"status": 200, "data": list(rows)}).encode("utf-8")


# Every codex family and the three listed Claude aliases, all with a coding index.
AA_ROWS = (aa_row("openai", "gpt-6-luna", 20), aa_row("openai", "gpt-6-1-terra-max", 40),
           aa_row("openai", "gpt-6-1-sol-max", 60), aa_row("openai", "gpt-6-astra-max", 70),
           aa_row("anthropic", "claude-sonnet-5-5", 50), aa_row("anthropic", "claude-opus-5-5", 60),
           aa_row("anthropic", "claude-fable-5-1", 65), aa_row("anthropic", "claude-haiku-5-5", 20))


def refreshed(catalog: Catalog, engine: str = "codex", *, env=None, keychain=None, session=None, now=NOW,
              fetch_scores: bool = True, fetch: bool = True):
    names = CODEX_MODELS if engine == "codex" else CLAUDE_MODELS
    return benchmarks.data_for(engine, entry(engine, *names), catalog=catalog,
                               settings={"fetch_scores": fetch_scores, "ttl_hours": 24.0}, known=scoring.families(),
                               session=session, rebuilt=fetch, fetch=fetch, env=env or {},
                               keychain=keychain or NoKeychain(), now=now)


def http_error(url: str, code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "status", headers or {}, io.BytesIO(b""))


def failed_notices(notices) -> list[str]:
    return [line for line in notices if line.startswith("OAH_SCORES_REFRESH_FAILED:")]


class EpochFetchTest(unittest.TestCase):
    def test_a_rebuild_fetches_epoch_and_stores_the_snapshot_fields(self):
        opener = use_opener(self, FakeOpener({EPOCH_URL: FakeResponse(epoch_csv(CSV_VALUES))}))
        catalog = state_with(self)
        selection = refreshed(catalog)
        stored = json.loads((catalog.dir / "benchmarks-epoch.json").read_bytes())
        self.assertEqual(stored, {"schema": 1, "source": "Epoch AI", "date": "2026-11-02", "license": "CC BY 4.0",
                                  "url": "https://epoch.ai/benchmarks/use-this-data", "credit": CREDIT,
                                  "scores": {model: dict.fromkeys(EPOCH_TASKS, value)
                                             for model, value in CSV_VALUES.items()},
                                  "fetched_at": "2026-11-02T09:30:00Z", "source_url": EPOCH_URL})
        self.assertEqual(selection.data, stored)
        self.assertEqual(opener.requests, [{"url": EPOCH_URL, "method": "GET", "headers": AGENT, "data": None,
                                            "timeout": 10.0}], "no body, no query string, no key, only the agent")
        self.assertEqual(failed_notices(selection.notices), [])

    def test_a_fetch_and_the_tool_give_the_same_scores(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                content = epoch_csv(CSV_VALUES, newline=newline)
                use_opener(self, FakeOpener({EPOCH_URL: FakeResponse(content)}))
                expected = {model: dict.fromkeys(EPOCH_TASKS, value) for model, value in CSV_VALUES.items()}
                self.assertEqual(refreshed(state_with(self)).data["scores"], expected)
                self.assertEqual(tool_scores(self, content), expected)

    def failures(self) -> dict[str, tuple[object, str]]:
        return {
            "a timeout": (urllib.error.URLError(socket.timeout("timed out")), "10 seconds"),
            "a read that times out": (lambda request: TimeoutBody(), "10 seconds"),
            "HTTP 500": (http_error(EPOCH_URL, 500), "HTTP 500"),
            "a redirect": (http_error(EPOCH_URL, 302, {"Location": "https://elsewhere.example/"}), "redirect"),
            "an oversized body": (FakeResponse(b"x" * 2048), "more than"),
            "an oversized length": (FakeResponse(b"", headers={"Content-Length": str(10 ** 9)}), "more than"),
            "invalid UTF-8": (FakeResponse(b"\xff\xfe" + epoch_csv(CSV_VALUES)), "UTF-8"),
            "no allowed task": (FakeResponse(epoch_csv(CSV_VALUES, tasks=("SWE-Bench verified",))), "no rows"),
            "another success status": (FakeResponse(epoch_csv(CSV_VALUES), status=203), "HTTP 203"),
        }

    def test_each_failure_keeps_the_last_good_file_and_warns_once(self):
        for name, (answer, said) in self.failures().items():
            limit = 1024 if "oversized" in name else benchmarks.LIMITS["epoch"]
            with self.subTest(name), mock.patch.dict(benchmarks.LIMITS, {"epoch": limit}):
                opener = use_opener(self, FakeOpener({EPOCH_URL: answer}))
                catalog = state_with(self, epoch=STALE_EPOCH)
                before = (catalog.dir / "benchmarks-epoch.json").read_bytes()
                session = benchmarks.Session()
                first, second = refreshed(catalog, session=session), refreshed(catalog, "claude", session=session)
                self.assertEqual((catalog.dir / "benchmarks-epoch.json").read_bytes(), before)
                self.assertEqual((first.data, second.data), (STALE_EPOCH, STALE_EPOCH), "the last good copy scores")
                warned = failed_notices((*first.notices, *second.notices))
                self.assertEqual(len(warned), 1, warned)
                self.assertTrue(warned[0].startswith("OAH_SCORES_REFRESH_FAILED: Epoch AI "), warned[0])
                self.assertIn(said, warned[0])
                self.assertIn("using the copy fetched 2026-11-01T08:00:00Z", warned[0])
                self.assertIn("fetch_scores = false", warned[0], "the warning names the way to stop it")
                self.assertEqual(len(opener.requests), 1, "one fetch per source per command")

    def test_a_failure_with_no_last_good_file_uses_the_snapshot(self):
        use_opener(self, FakeOpener({EPOCH_URL: http_error(EPOCH_URL, 503)}))
        selection = refreshed(state_with(self))
        self.assertIsNone(selection.data)
        self.assertIn("using the snapshot shipped with oah", failed_notices(selection.notices)[0])

    def test_a_copy_that_cannot_be_saved_is_not_used(self):
        use_opener(self, FakeOpener({EPOCH_URL: FakeResponse(epoch_csv(CSV_VALUES))}))
        catalog = state_with(self, epoch=STALE_EPOCH)
        with mock.patch.object(Catalog, "save_scores", side_effect=StorageError(catalog.dir, OSError("full"))):
            selection = refreshed(catalog)
        self.assertEqual(selection.data, STALE_EPOCH)
        self.assertIn("could not be saved", failed_notices(selection.notices)[0])

    def test_a_fresh_copy_is_fetched_again_only_after_ttl_hours(self):
        opener = use_opener(self, FakeOpener({EPOCH_URL: FakeResponse(epoch_csv(CSV_VALUES))}))
        catalog = state_with(self, epoch=EPOCH)
        self.assertEqual(refreshed(catalog, now=NOW + dt.timedelta(hours=23)).data, EPOCH)
        self.assertEqual(opener.requests, [])
        refreshed(catalog, now=NOW + dt.timedelta(hours=24))
        self.assertEqual(opener.urls(), [EPOCH_URL])

    def test_a_copy_dated_in_the_future_is_due(self):
        future = {**EPOCH, "date": "2026-12-01", "fetched_at": "2026-12-01T00:00:00Z",
                  "credit": CREDIT.replace("2 Nov", "1 Dec")}
        opener = use_opener(self, FakeOpener({EPOCH_URL: FakeResponse(epoch_csv(CSV_VALUES))}))
        refreshed(state_with(self, epoch=future))
        self.assertEqual(opener.urls(), [EPOCH_URL])

    def test_only_a_rebuild_with_fetch_scores_on_fetches(self):
        opener = use_opener(self)
        refreshed(state_with(self), fetch=False)
        refreshed(state_with(self), fetch_scores=False)
        self.assertEqual(opener.requests, [])

    def test_the_real_opener_follows_no_redirect_and_no_proxy(self):
        from tests import helpers

        opener = helpers.REAL_OPENER()
        kinds = [type(handler) for handler in opener.handlers]
        self.assertIn(benchmarks._NoRedirect, kinds)
        self.assertNotIn(urllib.request.HTTPCookieProcessor, kinds)
        # An empty ProxyHandler replaces the default one, so no proxy from the environment is used.
        self.assertEqual([handler for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler)],
                         [])
        self.assertIsNone(benchmarks._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x.example/"))


class TimeoutBody(FakeResponse):
    """A reply whose body stops arriving."""

    def read1(self, size: int = -1) -> bytes:
        raise TimeoutError("timed out")


class ArtificialAnalysisTest(unittest.TestCase):
    KEY = {KEY_NAME: "test-key-0f3a"}

    def test_with_no_key_nothing_is_sent_and_a_stored_copy_is_removed(self):
        opener = use_opener(self, FakeOpener({EPOCH_URL: FakeResponse(epoch_csv(CSV_VALUES))}))
        catalog = state_with(self, aa=STALE_AA)
        selection = refreshed(catalog)
        self.assertNotIn(AA_URL, opener.urls())
        self.assertFalse((catalog.dir / "benchmarks-aa.json").exists(), "a removed key stops the copy being used")
        self.assertEqual(source_of(selection), "Epoch AI")
        self.assertEqual(failed_notices(selection.notices), [], "a missing key is not a problem")

    def test_with_a_key_only_the_header_carries_it(self):
        opener = use_opener(self, FakeOpener({AA_URL: FakeResponse(aa_body(*AA_ROWS))}))
        catalog = state_with(self)
        selection = refreshed(catalog, env=self.KEY)
        sent = [request for request in opener.requests if request["url"] == AA_URL]
        self.assertEqual(sent, [{"url": AA_URL, "method": "GET", "headers": {**AGENT, "X-api-key": "test-key-0f3a"},
                                 "data": None, "timeout": 10.0}])
        self.assertEqual(source_of(selection), "Artificial Analysis")
        stored = json.loads((catalog.dir / "benchmarks-aa.json").read_bytes())
        self.assertEqual(stored, {
            "schema": 1, "source": "Artificial Analysis", "date": "2026-11-02", "url": "https://artificialanalysis.ai/",
            "credit": "Artificial Analysis, https://artificialanalysis.ai/", "min_tasks": 1,
            "scores": {"gpt-6-luna": {CODING: 0.2}, "gpt-6.1-terra": {CODING: 0.4}, "gpt-6.1-sol": {CODING: 0.6},
                       "gpt-6-astra": {CODING: 0.7}, "claude-sonnet-5-5": {CODING: 0.5},
                       "claude-opus-5-5": {CODING: 0.6}, "claude-fable-5-1": {CODING: 0.65},
                       "claude-haiku-5-5": {CODING: 0.2}},
            "fetched_at": "2026-11-02T09:30:00Z", "source_url": AA_URL})
        self.assertNotIn(b"pricing", (catalog.dir / "benchmarks-aa.json").read_bytes(), "nothing else is kept")

    def test_the_keychain_is_asked_only_right_before_a_fetch(self):
        keychain = mock.Mock(lookup=mock.Mock(return_value="test-key-0f3a"))
        use_opener(self, FakeOpener({AA_URL: FakeResponse(aa_body(*AA_ROWS))}))
        catalog = state_with(self)
        refreshed(catalog, keychain=keychain, fetch=False)
        self.assertEqual(keychain.lookup.call_count, 0, "no fetch, no lookup")
        refreshed(catalog, keychain=keychain)
        self.assertEqual(keychain.lookup.call_args_list, [mock.call(KEY_NAME)])
        refreshed(catalog, keychain=keychain, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(keychain.lookup.call_count, 1, "a fresh copy needs no key")

    def test_rows_match_by_creator_slug_and_effort(self):
        rows = (aa_row("openai", "gpt-6-1-sol", 40), aa_row("openai", "gpt-6-1-sol-xhigh", 50),
                aa_row("openai", "gpt-6-1-sol-max", 60), aa_row("openai", "gpt-6-1-terra", 20),
                aa_row("openai", "gpt-6-1-terra-xhigh", 30), aa_row("openai", "gpt-6-luna", 10),
                aa_row("openai", "gpt-6-astra-high", 90), aa_row("openai", "gpt-6-nova-max", 50),
                aa_row("openai", "gpt-6-luna-mini", 99), aa_row("someone", "gpt-6-astra-max", 80),
                aa_row("openai", "gpt-6-astra-max", 150), aa_row("anthropic", "claude-opus-5-5", 55),
                aa_row("anthropic", "claude-opus-5-5-thinking", 65), aa_row("anthropic", "claude-opus-5", 45),
                aa_row("anthropic", "claude-opus-5-20260101", 44), aa_row("anthropic", "claude-sonnet-5-5", 35),
                aa_row("anthropic", "claude-poet-5-5", 70), aa_row("openai", "claude-fable-5-1", 70),
                {"slug": "claude-fable-5-1", "model_creator": "anthropic"}, "not a row")
        self.assertEqual(benchmarks.aa_scores(json.loads(aa_body(*rows)), scoring.families()), {
            "gpt-6.1-sol": {CODING: 0.6}, "gpt-6.1-terra": {CODING: 0.3}, "gpt-6-luna": {CODING: 0.1},
            "claude-opus-5-5": {CODING: 0.65}, "claude-opus-5": {CODING: 0.45}, "claude-sonnet-5-5": {CODING: 0.35}})

    def test_an_engine_uses_the_intelligence_index_when_one_model_lacks_coding(self):
        rows = (aa_row("openai", "gpt-6-luna", 10, 30), aa_row("openai", "gpt-6-1-sol-max", 60, 70),
                aa_row("anthropic", "claude-opus-5-5", 55, 80), aa_row("anthropic", "claude-sonnet-5-5", None, 50),
                aa_row("anthropic", "claude-fable-5-1", 60, None))
        self.assertEqual(benchmarks.aa_scores(json.loads(aa_body(*rows)), scoring.families()), {
            "gpt-6-luna": {CODING: 0.1}, "gpt-6.1-sol": {CODING: 0.6},
            "claude-opus-5-5": {"Intelligence Index": 0.8}, "claude-sonnet-5-5": {"Intelligence Index": 0.5}})

    def test_replies_oah_cannot_use_keep_the_last_good_copy(self):
        cases = {"HTTP 401": (http_error(AA_URL, 401), "refused the key"),
                 "HTTP 429": (http_error(AA_URL, 429), "temporary"),
                 "a timeout": (urllib.error.URLError(socket.timeout("timed out")), "temporary"),
                 "a malformed body": (FakeResponse(b"{not json"), "cannot read"),
                 "deep nesting": (FakeResponse(b"[" * 100000 + b"]" * 100000), "cannot read"),
                 "no known model": (FakeResponse(aa_body(aa_row("openai", "gpt-6-nova", 50))), "no model"),
                 "a bare list": (FakeResponse(json.dumps([AA_ROWS[0]]).encode()), "cannot read")}
        for name, (answer, said) in cases.items():
            with self.subTest(name):
                use_opener(self, FakeOpener({AA_URL: answer}))
                catalog = state_with(self, aa=STALE_AA, epoch=EPOCH)
                selection = refreshed(catalog, env=self.KEY)
                self.assertEqual(selection.data, STALE_AA)
                warned = failed_notices(selection.notices)
                self.assertEqual(len(warned), 1, warned)
                self.assertTrue(warned[0].startswith("OAH_SCORES_REFRESH_FAILED: Artificial Analysis "), warned[0])
                self.assertIn(said, warned[0])
                self.assertNotIn("test-key-0f3a", warned[0])

    def test_a_key_a_header_cannot_carry_is_refused_before_any_request(self):
        opener = use_opener(self)
        selection = refreshed(state_with(self), env={KEY_NAME: "bad key\nwith a newline"})
        self.assertNotIn(AA_URL, opener.urls())
        self.assertNotIn("bad key", " ".join(selection.notices))
        self.assertTrue(failed_notices(selection.notices))

    def test_a_fetch_error_carries_no_context_that_could_hold_the_key(self):
        use_opener(self, FakeOpener({AA_URL: http_error(AA_URL, 401)}))
        with self.assertRaises(benchmarks.FetchFailed) as caught:
            benchmarks.fetch_aa(Secret("test-key-0f3a"), scoring.families(), NOW)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)


def models_cli_run(project, *argv: str) -> tuple[int, str, str]:
    from tests.test_models import run_in

    return run_in(project, *argv)


def recommend(project, *argv: str) -> tuple[int, str, str]:
    import argparse
    import contextlib

    from oah import recommend_cli
    from tests.test_models import inside

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    recommend_cli.register(commands)
    out, err = io.StringIO(), io.StringIO()
    with inside(project.repo, project.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = recommend_cli.run(parser.parse_args(["recommend", *argv]))
    return code, out.getvalue(), err.getvalue()


def doctor_text(project) -> tuple[str, str]:
    from oah import doctor
    from tests.test_models import inside

    with inside(project.repo, project.env):
        choices = doctor.model_choices(project.repo, dict(os_environ()))
    return doctor.render_text([], "darwin", model_choices=choices), doctor.render_json([], "darwin", model_choices=choices)


def os_environ():
    import os

    return os.environ


def project_with_key(test: unittest.TestCase, key: str | None, config: str = ""):
    """A project whose CLIs are fakes and whose key, if any, is in its environment; the OS keychain is never asked."""
    from oah import secrets as oah_secrets
    from tests.test_call import scored_project

    shipped_defaults(test)
    project = scored_project(test, config)
    project.env.pop(KEY_NAME, None)
    if key is not None:
        project.env[KEY_NAME] = key
    patcher = mock.patch.object(oah_secrets, "system_keychain", return_value=NoKeychain())
    patcher.start()
    test.addCleanup(patcher.stop)
    return project


def both_sources() -> dict:
    return {EPOCH_URL: FakeResponse(epoch_csv(CSV_VALUES)), AA_URL: FakeResponse(aa_body(*AA_ROWS))}


class FetchWhenTest(unittest.TestCase):
    """AC1 and AC6: a fetch happens at a catalog rebuild only, at most once per source per command."""

    def test_oah_models_refresh_fetches_each_source_once_for_both_engines(self):
        project = project_with_key(self, "test-key-0f3a")
        opener = use_opener(self, FakeOpener(both_sources()))
        config = (project.repo / ".oah" / "config.toml").read_bytes()
        code, out, err = models_cli_run(project, "--refresh", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(opener.urls()), sorted([EPOCH_URL, AA_URL]), "two engines rebuilt, one fetch each")
        report = json.loads(out)["engines"]
        self.assertEqual({engine: report[engine]["scores"]["source"] for engine in report},
                         {"codex": "Artificial Analysis", "claude": "Artificial Analysis"})
        self.assertEqual((project.repo / ".oah" / "config.toml").read_bytes(), config, "the user's file is untouched")
        self.assertEqual(sorted(path.name for path in (project.state / "models").glob("benchmarks-*.json")),
                         ["benchmarks-aa.json", "benchmarks-epoch.json"])

    def test_a_failed_fetch_is_not_tried_again_by_the_second_engine(self):
        project = project_with_key(self, "test-key-0f3a")
        opener = use_opener(self)  # refuses every request, so both copies stay due
        code, out, err = models_cli_run(project, "--refresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(opener.urls()), sorted([EPOCH_URL, AA_URL]))
        self.assertEqual(err.count("OAH_SCORES_REFRESH_FAILED: Epoch AI "), 1)
        self.assertEqual(err.count("OAH_SCORES_REFRESH_FAILED: Artificial Analysis "), 1)

    def test_paths_that_write_nothing_never_fetch(self):
        from oah import call
        from oah.models import Catalog

        project = project_with_key(self, "test-key-0f3a")
        opener = use_opener(self)  # refuses, so every copy stays absent and due
        config = (project.repo / ".oah" / "config.toml").read_bytes()
        # With no catalog yet, recommend rebuilds one in memory and must still neither fetch nor write.
        code, out, err = recommend(project, "--kind", "implementation", "--intensity", "low", "--reasoning", "low",
                                   "--risk", "low", "--engine", "codex", "--stage", "correction")
        self.assertEqual(code, 0, err)
        self.assertFalse((project.state / "models" / "catalog.json").exists())
        self.assertEqual(opener.requests, [])
        self.assertEqual(models_cli_run(project)[0], 0)
        opener.requests.clear()
        doctor_text(project)
        catalog = Catalog(project.state)
        call.stored_choices("codex", catalog.load("codex"), project.settings(), catalog=catalog)
        project.prepare(stage="correction", tier="high", env={KEY_NAME: "test-key-0f3a"})
        self.assertEqual(opener.requests, [], "recommend, doctor, stored choices and a launch with no rebuild")
        self.assertEqual((project.repo / ".oah" / "config.toml").read_bytes(), config)

    def test_a_correction_never_fetches(self):
        from oah import call

        project = project_with_key(self, "test-key-0f3a")
        opener = use_opener(self, FakeOpener(both_sources()))
        token = call._CORRECTION_INPUT_CHECK.set(lambda value: None)
        try:
            catalog = call._CorrectionCatalog(project.state)
            inputs_token = call._CORRECTION_INPUTS.set(mock.Mock(catalog=catalog))
            try:
                call.check_catalog("codex", project.settings(), state_dir=project.state, env=project.env, refresh=True)
            finally:
                call._CORRECTION_INPUTS.reset(inputs_token)
        finally:
            call._CORRECTION_INPUT_CHECK.reset(token)
        self.assertEqual(opener.requests, [], "oah fix decides on the bytes it captured and writes nothing")

    def test_fetch_scores_false_stops_every_fetch(self):
        project = project_with_key(self, "test-key-0f3a", "[models]\nfetch_scores = false\n")
        opener = use_opener(self, FakeOpener(both_sources()))
        code, out, err = models_cli_run(project, "--refresh", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(opener.requests, [])
        self.assertEqual(json.loads(out)["engines"]["codex"]["scores"]["credit"], "Epoch AI, a credit line.")

    def test_a_launch_that_rebuilds_fetches_through_the_same_check(self):
        project = project_with_key(self, None)
        opener = use_opener(self, FakeOpener(both_sources()))
        data = project.run(stage="correction", tier="high", axes={"kind": "quick"})
        self.assertEqual(opener.urls(), [EPOCH_URL], "no key: only Epoch AI is asked")
        self.assertEqual(data["admission"]["score_source"], "Epoch AI")
        self.assertTrue(data["admission"]["score_credit"].startswith("Epoch AI, 'Capabilities & benchmarking'"))


class KeyCanaryTest(unittest.TestCase):
    """AC2: the key goes into the x-api-key header and nowhere else, whatever the reply."""

    def test_the_key_reaches_only_the_request_header(self):
        import logging
        import secrets as token_source
        import subprocess
        import traceback

        canary = "canary" + token_source.token_hex(16)
        argv: list[str] = []
        real_popen = subprocess.Popen

        def spawn(args, *rest, **fields):
            argv.extend(str(part) for part in args)
            return real_popen(args, *rest, **fields)

        log = io.StringIO()
        handler = logging.StreamHandler(log)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        seen, errors = [], []
        replies = {"a fetch": FakeResponse(aa_body(*AA_ROWS)), "HTTP 401": http_error(AA_URL, 401),
                   "a timeout": urllib.error.URLError(socket.timeout("timed out")), "a malformed body": FakeResponse(b"{")}
        for name, reply in replies.items():
            with self.subTest(name):
                project = project_with_key(self, canary)
                opener = use_opener(self, FakeOpener({AA_URL: reply, EPOCH_URL: FakeResponse(epoch_csv(CSV_VALUES))}))
                with mock.patch("subprocess.Popen", side_effect=spawn):
                    for argv_models in (("--refresh",), ("--json",)):
                        seen.extend(models_cli_run(project, *argv_models)[1:])
                    seen.extend(doctor_text(project))
                    prepared = project.prepare(stage="correction", tier="high", refresh_models=True)
                    seen.extend(prepared.warnings)
                    try:
                        benchmarks.fetch_aa(Secret(canary), scoring.families(), NOW)
                    except Exception as exc:  # the text of every error a fetch can raise
                        errors.append("".join(traceback.format_exception(exc)) + repr(exc))
                self.assertIn(canary, [request["headers"].get("X-api-key") for request in opener.requests],
                              "the key did travel, in its header")
                for path in project.state.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(canary.encode("utf-8"), path.read_bytes(), path)
        self.assertGreaterEqual(len(errors), 3)
        for text in (*seen, *errors, log.getvalue(), " ".join(argv)):
            self.assertNotIn(canary, text)


class NetworkGuardTest(unittest.TestCase):
    """AC6: tests never reach the network."""

    def test_the_suite_never_calls_urllib_for_real(self):
        import urllib.request as request

        from tests import helpers

        self.assertIsNot(benchmarks._opener, helpers.REAL_OPENER, "the suite's default opener is a fake")
        project = project_with_key(self, "test-key-0f3a")
        with mock.patch.object(request, "urlopen", side_effect=AssertionError("urlopen")) as urlopen, \
                mock.patch.object(request.OpenerDirector, "open", side_effect=AssertionError("open")) as real_open:
            self.assertEqual(models_cli_run(project, "--refresh")[0], 0)
            project.prepare(stage="correction", tier="high", refresh_models=True)
            recommend(project, "--kind", "docs", "--intensity", "low", "--reasoning", "low", "--risk", "low",
                      "--engine", "codex")
            doctor_text(project)
        self.assertEqual((urlopen.call_count, real_open.call_count), (0, 0))

    def test_a_real_request_from_a_test_stops_before_the_network(self):
        from tests import helpers

        with self.assertRaises(helpers.RealNetworkRequest):
            urllib.request.urlopen(EPOCH_URL, timeout=1)
        with self.assertRaises(helpers.RealNetworkRequest):
            helpers.REAL_OPENER().open(urllib.request.Request(AA_URL), timeout=1)


class ScoreChangeTest(unittest.TestCase):
    def test_a_fetch_that_moves_the_choice_says_so_once(self):
        from oah.models import Catalog
        from tests.test_call import STORED_RANKING, write_stored_epoch

        project = project_with_key(self, None)
        stale = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(hours=30)
        write_stored_epoch(project.state, STORED_RANKING, fetched=stale)
        use_opener(self, FakeOpener({EPOCH_URL: http_error(EPOCH_URL, 503)}))
        project.prepare(stage="correction", tier="high")  # the first catalog: the stale copy stays, sol leads
        use_opener(self, FakeOpener({EPOCH_URL: FakeResponse(epoch_csv(CSV_VALUES))}))
        prepared = project.prepare(stage="correction", tier="high", env={"FAKE_CODEX_VERSION": "0.164.0"})
        changed = [line.split(";")[0] for line in prepared.warnings if line.startswith("OAH_MODEL_CHANGED:")]
        self.assertEqual(changed, [f"OAH_MODEL_CHANGED: codex step {step} now uses gpt-6.1-terra (was gpt-6.1-sol): "
                                   "the scores changed" for step in (1, 2)])
        self.assertEqual([line["why"] for line in Catalog(project.state).changes() if "step" in line],
                         ["the scores changed", "the scores changed"])
