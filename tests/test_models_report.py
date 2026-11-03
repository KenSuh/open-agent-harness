# 모델 선택 보고 검사 — 실행 기록의 나이별 계수, 정정 기록의 해시 비교, 읽지 못한 기록의 사유, 쓰기·프로세스·네트워크 차단, CLI 출력을 본다.
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import socket
import subprocess
import unittest
import urllib.request
from unittest import mock

from oah import models_cli, models_report, rerank, runs
from oah.paths import RepoInfo
from oah.rerank import typesafe
from tests.helpers import directory_alias, make_tmp_dir

# The run-record keys each oah age wrote (git show v0.1.0:src/oah/runs.py, v0.2.0, and main before this change).
V010_FIELDS = (
    "run_id", "task", "engine", "stage", "status", "created_at", "started_at", "finished_at", "duration_s",
    "exit_code", "timed_out", "pid", "command", "cwd", "read_only", "cli_path", "cli_version",
    "requested_model", "resolved_model", "model_source", "observed_models",
    "requested_effort", "launch_effort", "header_effort", "header_check", "header_code",
    "resolved_inference_effort", "normalization_basis", "effort_verified", "effort_observable",
    "axes", "tier", "tier_source", "tier_reasons", "advisory", "override_stage", "warnings", "error", "detached",
    "launcher", "worker_output",
)
V020_FIELDS = V010_FIELDS + ("model_judge", "model_choice", "header_model")
MDR3_FIELDS = V020_FIELDS + ("admission", "rule_tier", "jev_tier", "jev_confidence", "jev_status", "jev_truncated")
NEW_FIELDS = ("model_judge_confidence", "tier_floor_raised")

ZERO = [0] * 10


def run_id(n: int) -> str:
    return f"r20261101T1200{n:02d}Z-{n:08x}"


def shaped(fields, **values) -> dict:
    record = {name: None for name in fields}
    record.update(warnings=[], status="finished", task="t1", **values)
    return record


def choice(deterministic, requested, **extra) -> dict:
    return {"deterministic": {"model": deterministic[0], "effort": deterministic[1], "source": "quality_order"},
            "requested": {"model": requested[0], "effort": requested[1]}, "floor": None, "mode": "ranked",
            "note": None, "candidates": [], "excluded": {}, "proposal": None, "provider": "fake", **extra}


V010 = shaped(V010_FIELDS, engine="claude", stage="initial", tier="high", tier_source="rules",
              advisory="unused(not-configured)")
V020 = shaped(V020_FIELDS, engine="codex", stage="review", tier="medium", tier_source="rules", model_judge="used",
              model_choice=choice(("m-mid", "medium"), ("m-strong", "high")))
MDR3 = shaped(MDR3_FIELDS, engine="codex", stage="review", tier="high", tier_source="jev", rule_tier="medium",
              jev_tier="high", jev_confidence=0.8, jev_status="used", jev_truncated=False,
              model_judge="rejected(below-floor)", model_choice=choice(("m-mid", "high"), ("m-mid", "high")))
NEW = shaped(runs.RESULT_FIELDS, engine="codex", stage="review", tier="medium", tier_source="rules",
             rule_tier="medium", jev_tier="max", jev_confidence=0.5, jev_status="below-threshold 0.5",
             jev_truncated=False, model_judge="used", model_choice=choice(("m-mid", "medium"), ("m-mid", "high")),
             model_judge_confidence=0.72, tier_floor_raised=False)


def write_run(state: pathlib.Path, n: int, record, *, task: str = "t1", raw: bytes | None = None) -> pathlib.Path:
    folder = state / "runs" / task / run_id(n)
    folder.mkdir(parents=True)
    if raw is not None or record is not None:
        data = raw if raw is not None else json.dumps(dict(record, run_id=run_id(n), task=task)).encode("utf-8")
        (folder / "result.json").write_bytes(data)
    return folder


def ages_state(test: unittest.TestCase) -> pathlib.Path:
    state = make_tmp_dir(test)
    for n, record in enumerate((V010, V020, MDR3, NEW), 1):
        write_run(state, n, record)
    # Dispatch keeps its Intent binding beside the run folders; it is no run.
    (state / "runs" / "t1" / "intent-binding.json").write_text("{}\n", encoding="utf-8")
    return state


def tally_sum(tally: dict) -> int:
    """Every bucket of one count; it must equal the runs of its group, so nothing is dropped."""
    total = 0
    for key, value in tally.items():
        if key == "values":
            total += sum(value.values())
        elif key == "unreadable":
            total += sum(value.values())
        elif isinstance(value, list):
            total += sum(value)
        else:
            total += value
    return total


class AgesTest(unittest.TestCase):
    """AC3, AC4, AC5, AC8: one record of each age and what each yields."""

    def test_each_age_yields_its_counts(self):
        report = models_report.collect(ages_state(self))
        self.assertEqual(report["format"], 1)
        self.assertEqual(report["records"], {"run_folders": 4, "read": 4, "partial": 0, "unreadable": {},
                                             "skipped": {}})
        totals = report["totals"]
        self.assertEqual(totals["runs"], 4)
        self.assertEqual(totals["tier_source"], {"values": {"rules": 3, "jev": 1}, "none": 0, "not_recorded": 0,
                                                 "unreadable": {}})
        self.assertEqual(totals["jev_status"], {"values": {"used": 1, "below-threshold": 1}, "none": 0,
                                                "not_recorded": 2, "unreadable": {}})
        self.assertEqual(totals["jev_vs_rule"], {"values": {"used:above": 1, "below-threshold:above": 1}, "none": 0,
                                                 "not_recorded": 2, "unreadable": {}})
        used, below = list(ZERO), list(ZERO)
        used[8], below[5] = 1, 1
        self.assertEqual(totals["jev_confidence"], {"used": used, "below-threshold": below, "no_confidence": 0,
                                                    "not_answered": 0, "not_recorded": 2, "unreadable": {}})
        self.assertEqual(totals["model_judge"], {"values": {"used": 2, "rejected(below-floor)": 1}, "none": 0,
                                                 "not_recorded": 1, "unreadable": {}})
        self.assertEqual(totals["model_change"], {"values": {"both": 1, "same": 1, "effort": 1}, "none": 0,
                                                  "not_recorded": 1, "unreadable": {}})
        judge_used = list(ZERO)
        judge_used[7] = 1
        self.assertEqual(totals["model_judge_confidence"], {"used": judge_used, "rejected": list(ZERO),
                                                            "no_confidence": 0, "not_answered": 0, "not_recorded": 3,
                                                            "unreadable": {}})
        self.assertEqual(totals["tier_floor_raised"], {"values": {"false": 1}, "none": 0, "not_recorded": 3,
                                                       "unreadable": {}})
        self.assertEqual(report["written_before"], {"model_judge": 1, "jev_difficulty": 2,
                                                    "model_judge_confidence": 3, "tier_floor_raised": 3})

    def test_groups_by_stage_and_engine_add_up_to_the_totals(self):
        report = models_report.collect(ages_state(self))
        self.assertEqual([(group["stage"], group["engine"], group["counts"]["runs"]) for group in report["groups"]],
                         [("initial", "claude", 1), ("review", "codex", 3)])
        for group in [*report["groups"], {"stage": "total", "engine": "", "counts": report["totals"]}]:
            counts = group["counts"]
            for name in models_report.DIMENSIONS:
                with self.subTest(group=group["stage"], count=name):
                    self.assertEqual(tally_sum(counts[name]), counts["runs"])

    def test_floor_raised_and_judge_confidence_buckets(self):
        state = make_tmp_dir(self)
        write_run(state, 1, dict(NEW, tier_floor_raised=True, model_judge="rejected(above-cap)",
                                 model_judge_confidence=1.0, model_choice=choice(("m", "high"), ("m", "high"))))
        write_run(state, 2, dict(NEW, model_judge="used", model_judge_confidence=None))
        write_run(state, 3, dict(NEW, model_judge="unused(pinned)", model_judge_confidence=None,
                                 tier_floor_raised=None, jev_status="unused(tier-given)", jev_tier=None,
                                 jev_confidence=None, rule_tier=None))
        write_run(state, 4, dict(NEW, model_judge=None, model_choice=None, model_judge_confidence=None,
                                 tier_source=None, jev_status=None, jev_tier=None, jev_confidence=None,
                                 rule_tier=None, tier_floor_raised=None, status="refused"))
        totals = models_report.collect(state)["totals"]
        rejected = list(ZERO)
        rejected[9] = 1
        self.assertEqual(totals["model_judge_confidence"], {"used": list(ZERO), "rejected": rejected,
                                                            "no_confidence": 1, "not_answered": 2,
                                                            "not_recorded": 0, "unreadable": {}})
        self.assertEqual(totals["tier_floor_raised"], {"values": {"true": 1, "false": 1}, "none": 2,
                                                       "not_recorded": 0, "unreadable": {}})
        self.assertEqual(totals["model_judge"]["values"], {"rejected(above-cap)": 1, "used": 1, "unused(pinned)": 1})
        self.assertEqual(totals["model_judge"]["none"], 1)
        self.assertEqual(totals["jev_vs_rule"]["values"], {"below-threshold:above": 2, "no-jev-tier": 1})
        self.assertEqual(totals["jev_vs_rule"]["none"], 1)
        self.assertEqual(totals["jev_confidence"]["not_answered"], 2)

    def test_confidence_bands_are_tenths(self):
        for value, band in ((0.0, 0), (0.0999, 0), (0.1, 1), (0.3, 3), (0.7, 7), (0.95, 9), (1.0, 9), (1, 9)):
            with self.subTest(value=value):
                self.assertEqual(models_report.band(value), band)


class CorrectionTest(unittest.TestCase):
    """AC6: correction records keep the new fields readable and compare hashed names only for same or different."""

    def correction(self, deterministic, requested) -> dict:
        record = dict(NEW, model_choice=choice(deterministic, requested), model_judge_confidence=0.35,
                      tier_floor_raised=True, stage="correction")
        return runs._correction_record(record, runs.RESULT_FIELDS)

    def test_hashed_model_names_compare_for_same_or_different_only(self):
        state = make_tmp_dir(self)
        same = self.correction(("gpt-6.1-sol", "high"), ("gpt-6.1-sol", "high"))
        other = self.correction(("gpt-6.1-sol", "high"), ("gpt-6-luna", "high"))
        self.assertTrue(same["model_choice"]["requested"]["model"].startswith("sha256:"))
        self.assertEqual((same["model_judge_confidence"], same["tier_floor_raised"]), (0.35, True))
        write_run(state, 1, same)
        write_run(state, 2, other)
        report = models_report.collect(state)
        totals = report["totals"]
        self.assertEqual(totals["model_change"]["values"], {"same": 1, "model": 1})
        self.assertEqual(totals["model_change"]["unreadable"], {})
        band = list(ZERO)
        band[3] = 2
        self.assertEqual(totals["model_judge_confidence"]["used"], band)
        self.assertEqual(totals["tier_floor_raised"]["values"], {"true": 2})
        self.assertEqual([(g["stage"], g["engine"]) for g in report["groups"]], [("correction", "codex")])
        shown = json.dumps(report) + "\n".join(models_report.text(report))
        self.assertNotIn("sha256:", shown)
        self.assertNotIn("gpt-6", shown)

    def test_a_hashed_value_needed_for_a_count_is_unreadable_not_zero(self):
        state = make_tmp_dir(self)
        hashed = runs._correction_record(dict(NEW, model_judge="unused(error: RuntimeError)",
                                              model_judge_confidence=None), runs.RESULT_FIELDS)
        self.assertTrue(hashed["model_judge"].startswith("sha256:"))
        write_run(state, 1, hashed)
        one_side = dict(NEW, model_choice=choice(("sha256:" + "a" * 64, "high"), ("m-mid", "high")))
        write_run(state, 2, one_side)
        report = models_report.collect(state)
        totals = report["totals"]
        self.assertEqual(totals["model_judge"]["unreadable"], {"hashed": 1})
        self.assertEqual(totals["model_judge"]["values"], {"used": 1})
        self.assertEqual(totals["model_judge_confidence"]["unreadable"], {"hashed": 1})
        self.assertEqual(totals["model_change"]["unreadable"], {"hashed": 1})
        self.assertEqual(report["records"]["read"], 2)
        self.assertEqual(report["records"]["partial"], 2)


class UnreadableTest(unittest.TestCase):
    """AC2: every run folder counts once, as read or unreadable with its reason; nothing stops the report."""

    def test_each_unreadable_case_counts_once_with_its_reason(self):
        state = make_tmp_dir(self)
        write_run(state, 1, None)  # a run folder with no result.json
        write_run(state, 2, None, raw=b"{not json")
        write_run(state, 3, None, raw=b"[]")
        write_run(state, 4, dict(NEW, stage=5))
        write_run(state, 5, dict(NEW, engine="sha256:" + "b" * 64))
        write_run(state, 6, None, raw=b"\xff\xfe")
        elsewhere = write_run(make_tmp_dir(self), 7, NEW)
        directory_alias(elsewhere, state / "runs" / "t1" / run_id(7))
        (state / "runs" / "t1" / run_id(8)).write_text("{}", encoding="utf-8")  # a file where a folder belongs
        write_run(state, 9, NEW)
        write_run(state, 10, dict(NEW, tier_floor_raised="yes", model_choice="sha256:" + "c" * 64,
                                  model_judge_confidence=1.5, jev_confidence="high"))
        (state / "runs" / "t1" / "intent-binding.json").write_text("{}\n", encoding="utf-8")
        (state / "runs" / "t1" / ".r20261101T120000Z-00000000.tmp").mkdir()
        (state / "runs" / "t1" / "notes.txt").write_text("x", encoding="utf-8")
        (state / "runs" / "README").write_text("x", encoding="utf-8")
        report = models_report.collect(state)
        records = report["records"]
        self.assertEqual(records["run_folders"], 10)
        self.assertEqual(records["unreadable"], {"missing": 1, "invalid-json": 2, "not-object": 1,
                                                 "stage:wrong-type": 1, "engine:hashed": 1, "linked": 1,
                                                 "not-a-folder": 1})
        self.assertEqual((records["read"], records["partial"]), (2, 1))
        self.assertEqual(records["read"] + sum(records["unreadable"].values()), records["run_folders"])
        self.assertEqual(records["skipped"], {"being-created": 1, "unrecognized": 2})
        totals = report["totals"]
        self.assertEqual(totals["runs"], 2)
        self.assertEqual(totals["tier_floor_raised"]["unreadable"], {"wrong-type": 1})
        self.assertEqual(totals["model_change"]["unreadable"], {"hashed": 1})
        self.assertEqual(totals["model_judge_confidence"]["unreadable"], {"wrong-type": 1})
        self.assertEqual(totals["jev_confidence"]["unreadable"], {"wrong-type": 1})
        for name in models_report.DIMENSIONS:
            with self.subTest(count=name):
                self.assertEqual(tally_sum(totals[name]), 2)
        lines = models_report.text(report)
        self.assertIn("8 unreadable", lines[0])

    def test_a_linked_task_folder_is_named_and_not_read(self):
        state = make_tmp_dir(self)
        outside = make_tmp_dir(self)
        write_run(outside, 1, NEW, task="t2")
        (state / "runs").mkdir()
        directory_alias(outside / "runs" / "t2", state / "runs" / "t2")
        write_run(state, 2, NEW)
        report = models_report.collect(state)
        self.assertEqual(report["records"]["run_folders"], 1)
        self.assertEqual(report["records"]["skipped"], {"linked-task-folder": 1})

    def test_no_runs_folder_reports_nothing_yet(self):
        report = models_report.collect(make_tmp_dir(self))
        self.assertEqual(report["records"]["run_folders"], 0)
        self.assertEqual(report["groups"], [])
        self.assertEqual(report["totals"]["runs"], 0)
        self.assertIn("no run records", "\n".join(models_report.text(report)))

    def test_a_linked_runs_folder_refuses(self):
        state, outside = make_tmp_dir(self), make_tmp_dir(self)
        (outside / "runs").mkdir()
        directory_alias(outside / "runs", state / "runs")
        with self.assertRaises(runs.RunError) as caught:
            models_report.collect(state)
        self.assertEqual(caught.exception.code, "OAH_RUN_PATH_UNSAFE")


class Forbidden(BaseException):
    """The report reached for a process, the network, a provider or a write. BaseException, so nothing swallows it."""


def tree(folder: pathlib.Path) -> dict:
    return {str(path.relative_to(folder)): (path.is_symlink(), path.is_dir(),
                                            None if path.is_dir() else path.read_bytes(), path.lstat().st_mtime_ns)
            for path in sorted(folder.rglob("*"))}


def refuse(name):
    def refused(*args, **kwargs):
        raise Forbidden(f"the report used {name}")
    return refused


class NoSideEffectsTest(unittest.TestCase):
    """AC7: no provider, network, process or write is reachable from the report."""

    def guard(self):
        stack = contextlib.ExitStack()
        for target in ("subprocess.Popen", "socket.socket", "socket.create_connection", "urllib.request.urlopen",
                       "os.replace", "os.rename", "os.mkdir", "os.makedirs", "os.remove", "os.unlink"):
            stack.enter_context(mock.patch(target, refuse(target)))
        for owner, name in ((rerank, "judge_model"), (rerank, "advise_difficulty"), (rerank, "rerank"),
                            (typesafe.TypeSafeProvider, "_post_result"), (typesafe.TypeSafeProvider, "choose_model"),
                            (typesafe.TypeSafeProvider, "advise"), (urllib.request.OpenerDirector, "open")):
            stack.enter_context(mock.patch.object(owner, name, refuse(f"{owner.__name__}.{name}")))
        return stack

    def test_the_report_and_its_command_touch_nothing(self):
        state = ages_state(self)
        write_run(state, 9, None, raw=b"{")
        before = tree(state)
        info = RepoInfo(root=state, common_dir=state, branch=None, head=None)
        out = io.StringIO()
        with self.guard():
            report = models_report.collect(state)
            models_report.text(report)
            with mock.patch.object(models_cli, "repo_info", return_value=info), \
                    mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(state)}), contextlib.redirect_stdout(out):
                for argv in (["report"], ["report", "--json"]):
                    self.assertEqual(run_models(*argv)[0], 0)
        self.assertEqual(tree(state), before)

    def test_the_guard_catches_each_kind_of_use(self):
        with self.guard():
            for action in (lambda: subprocess.Popen(["git"]), lambda: socket.socket(),
                           lambda: urllib.request.urlopen("https://example.invalid"), lambda: os.replace("a", "b"),
                           lambda: rerank.judge_model({}, [], config={})):
                with self.subTest(action=action), self.assertRaises(Forbidden):
                    action()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    models_cli.register(root.add_subparsers(dest="command"))
    return root


def run_models(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = models_cli.run(parser().parse_args(["models", *argv]))
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    """AC1, AC8: `oah models report` and its JSON; bare `oah models` parses as before."""

    def report(self, state: pathlib.Path, *argv: str) -> tuple[int, str, str]:
        info = RepoInfo(root=state, common_dir=state, branch=None, head=None)
        with mock.patch.object(models_cli, "repo_info", return_value=info), \
                mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(state)}):
            return run_models("report", *argv)

    def test_text_names_groups_totals_and_what_the_counts_cannot_show(self):
        code, out, err = self.report(ages_state(self))
        self.assertEqual((code, err), (0, ""))
        self.assertIn(models_report.NOTE, out)
        self.assertIn("not whether Jev", out)
        self.assertIn("initial · claude: 1 run", out)
        self.assertIn("review · codex: 3 runs", out)
        self.assertIn("total: 4 runs", out)
        self.assertIn("written before this oah recorded them: judge confidence 3 of 4, floor flag 3 of 4", out)
        self.assertIn("jev confidence: used 0.8 1 · below-threshold 0.5 1", out)
        self.assertNotIn("0.0 0", out, "the text lists only bands that hold a record")

    def test_json_is_one_object_with_a_format_number(self):
        code, out, _ = self.report(ages_state(self), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["format"], models_report.FORMAT)
        self.assertEqual(data["note"], models_report.NOTE)
        self.assertEqual(data["bands"], ["0.0", "0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9"])
        self.assertEqual(data["written_before"]["tier_floor_raised"], 3)
        self.assertEqual(data, json.loads(json.dumps(models_report.collect(pathlib.Path(data["state"])))))

    def test_unreadable_records_still_print_and_exit_zero(self):
        state = ages_state(self)
        write_run(state, 9, None, raw=b"[]")
        code, out, _ = self.report(state)
        self.assertEqual(code, 0)
        self.assertIn("1 unreadable (not-object 1)", out)

    def test_outside_a_repository_is_an_environment_error(self):
        from oah.paths import NotARepo

        with mock.patch.object(models_cli, "repo_info", side_effect=NotARepo("OAH_NOT_A_REPO: not a repo")):
            code, out, err = run_models("report")
        self.assertEqual((code, out), (models_cli.EXIT_ENVIRONMENT, ""))
        self.assertIn("OAH_NOT_A_REPO", err)

    def test_a_linked_runs_folder_is_an_environment_error(self):
        state, outside = make_tmp_dir(self), make_tmp_dir(self)
        (outside / "runs").mkdir()
        directory_alias(outside / "runs", state / "runs")
        code, out, err = self.report(state)
        self.assertEqual((code, out), (models_cli.EXIT_ENVIRONMENT, ""))
        self.assertIn("OAH_RUN_PATH_UNSAFE", err)

    def test_refresh_does_not_apply_to_the_report(self):
        state = make_tmp_dir(self)
        info = RepoInfo(root=state, common_dir=state, branch=None, head=None)
        with mock.patch.object(models_cli, "repo_info", return_value=info):
            code, out, err = run_models("--refresh", "report")
        self.assertEqual((code, out), (models_cli.EXIT_USAGE, ""))
        self.assertIn("OAH_USAGE", err)
        self.assertFalse((state / "runs").exists())

    def test_bare_models_parses_as_before(self):
        self.assertEqual(models_cli.COMMANDS, ("models",))
        for argv, expected in ((["models"], (None, False, False)), (["models", "--json"], (None, False, True)),
                               (["models", "--refresh", "--json"], (None, True, True)),
                               (["models", "report"], ("report", False, False)),
                               (["models", "report", "--json"], ("report", False, True))):
            with self.subTest(argv=argv):
                args = parser().parse_args(argv)
                self.assertEqual((args.models_action, args.refresh, args.json), expected)

    def test_bare_models_never_reads_run_records(self):
        with mock.patch.object(models_report, "collect", refuse("collect")), \
                mock.patch.object(models_cli, "repo_info", side_effect=Forbidden("bare models went on")):
            with self.assertRaises(Forbidden) as caught:
                run_models()
        self.assertEqual(str(caught.exception), "bare models went on")


if __name__ == "__main__":
    unittest.main()
