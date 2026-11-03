# 모델 선택 내보내기 검사 — 나이별 실행 기록이 내는 줄, 허용된 키만 나오는지, 읽지 못한 값·기록 전 값·해시 값의 표시, 쓰기·프로세스·네트워크 차단, CLI 와 문서를 본다.
from __future__ import annotations

import contextlib
import hashlib
import importlib
from importlib import resources
import io
import json
import os
import pathlib
import socket
import subprocess
import unittest
import urllib.request
from unittest import mock

from oah import models_cli, models_export, models_report, rerank, runs
from oah.paths import RepoInfo
from oah.rerank import typesafe
from tests.helpers import directory_alias, make_tmp_dir
from tests.test_models_report import MDR3, NEW, V010, V020, ages_state, choice, run_id, run_models, tree, write_run

REPO = pathlib.Path(__file__).resolve().parent.parent
TOP_KEYS = {"format", "run_id", "axes", "stage", "engine", "tier", "tier_source", "rule_tier", "jev_tier",
            "jev_confidence", "jev_status", "tier_floor_raised", "model_judge", "model_judge_confidence",
            "model_choice", "outcome", "unreadable", "not_recorded", "hashed"}
AXES_KEYS = {"kind", "intensity", "reasoning", "risk"}
CHOICE_KEYS = {"mode", "floor", "candidates", "excluded", "deterministic", "proposal", "requested"}
OUTCOME_KEYS = {"status", "exit_code", "timed_out", "duration_s"}
PAIR_KEYS = {"model", "effort"}
FLOOR_KEYS = {"tier", "model", "effort"}
EXCLUDED_KEYS = {"model", "rule"}
EMPTY = {"axes": None, "stage": None, "engine": None, "tier": None, "tier_source": None, "rule_tier": None,
         "jev_tier": None, "jev_confidence": None, "jev_status": None, "tier_floor_raised": None, "model_judge": None,
         "model_judge_confidence": None, "model_choice": None, "outcome": None}
AXES = {"kind": "implementation", "intensity": "medium", "reasoning": "high", "risk": "normal"}
NO_OUTCOME = {"status": "finished", "exit_code": None, "timed_out": None, "duration_s": None}
JEV = ["jev_confidence", "jev_status", "jev_tier", "rule_tier"]

# Report fixtures use synthetic names; export fixtures use packaged identifiers.
def known_models(record):
    data = json.dumps(record).replace("m-mid", "gpt-6.1-sol").replace("m-strong", "gpt-6-luna")
    return json.loads(data)

V010, V020, MDR3, NEW = map(known_models, (V010, V020, MDR3, NEW))

# The report text for the four-age folder at main 5c0bad1, before the export shared its reading code.
REPORT_TEXT = [
    "oah models report · 4 run folders: 4 read, 0 unreadable",
    "These counts show how tiers and models were chosen, not whether Jev's choices helped: no outcome or quality is "
    "compared.",
    "written before this oah recorded them: judge confidence 3 of 4, floor flag 3 of 4 · before Jev difficulty 2, "
    "before the model judge 1",
    "initial · claude: 1 run",
    "  tier source: rules 1",
    "  jev difficulty: not recorded 1",
    "  jev tier vs rule tier: not recorded 1",
    "  jev confidence: not recorded 1",
    "  model judge: not recorded 1",
    "  pair that ran vs deterministic pair: not recorded 1",
    "  judge confidence: not recorded 1",
    "  floor raised the tier: not recorded 1",
    "review · codex: 3 runs",
    "  tier source: rules 2 · jev 1",
    "  jev difficulty: used 1 · below-threshold 1 · not recorded 1",
    "  jev tier vs rule tier: used:above 1 · below-threshold:above 1 · not recorded 1",
    "  jev confidence: used 0.8 1 · below-threshold 0.5 1 · not recorded 1",
    "  model judge: used 2 · rejected(below-floor) 1",
    "  pair that ran vs deterministic pair: both 1 · same 1 · effort 1",
    "  judge confidence: used 0.7 1 · not recorded 2",
    "  floor raised the tier: false 1 · not recorded 2",
    "total: 4 runs",
    "  tier source: rules 3 · jev 1",
    "  jev difficulty: used 1 · below-threshold 1 · not recorded 2",
    "  jev tier vs rule tier: used:above 1 · below-threshold:above 1 · not recorded 2",
    "  jev confidence: used 0.8 1 · below-threshold 0.5 1 · not recorded 2",
    "  model judge: used 2 · rejected(below-floor) 1 · not recorded 1",
    "  pair that ran vs deterministic pair: both 1 · same 1 · effort 1 · not recorded 1",
    "  judge confidence: used 0.7 1 · not recorded 3",
    "  floor raised the tier: false 1 · not recorded 3",
]


def pair(model, effort) -> dict:
    return {"model": model, "effort": effort}


def exported_choice(deterministic, requested, **extra) -> dict:
    return {"mode": "ranked", "floor": None, "candidates": [], "excluded": [], "deterministic": pair(*deterministic),
            "proposal": None, "requested": pair(*requested), **extra}


def line(n: int, **values) -> dict:
    """The line a readable record yields, with every field and nothing named."""
    return {"format": 1, "run_id": run_id(n), **EMPTY, "outcome": NO_OUTCOME, "unreadable": {}, "not_recorded": [],
            "hashed": [], **values}


def unreadable_line(n: int, reason: str) -> dict:
    return {"format": 1, "run_id": run_id(n), **EMPTY, "unreadable": {"record": reason}, "not_recorded": [],
            "hashed": []}


def exported(state: pathlib.Path) -> list[dict]:
    return models_export.lines(state)


def one(test: unittest.TestCase, record, **kwargs) -> dict:
    state = make_tmp_dir(test)
    write_run(state, 1, record, **kwargs)
    lines = exported(state)
    test.assertEqual(len(lines), 1)
    return lines[0]


def keys_of(value: dict) -> None:
    """Fail when a line holds any key outside the export's fields, at any depth."""
    assert set(value) == TOP_KEYS, sorted(set(value) ^ TOP_KEYS)
    if value["axes"] is not None:
        assert set(value["axes"]) == AXES_KEYS, value["axes"]
    if value["outcome"] is not None:
        assert set(value["outcome"]) == OUTCOME_KEYS, value["outcome"]
    choice_value = value["model_choice"]
    if isinstance(choice_value, dict):
        assert set(choice_value) == CHOICE_KEYS, sorted(choice_value)
        for name in ("deterministic", "proposal", "requested"):
            assert choice_value[name] is None or isinstance(choice_value[name], str) or set(choice_value[name]) == PAIR_KEYS
        assert choice_value["floor"] is None or isinstance(choice_value["floor"], str) or set(choice_value["floor"]) == FLOOR_KEYS
        for entry in choice_value["candidates"] or ():
            assert entry is None or isinstance(entry, str) or set(entry) == PAIR_KEYS, entry
        for entry in choice_value["excluded"] or ():
            assert entry is None or set(entry) == EXCLUDED_KEYS, entry


class AgesTest(unittest.TestCase):
    """AC1, AC3: the four-age folder the report tests use, and the line each age yields."""

    def test_each_age_yields_its_line(self):
        state = make_tmp_dir(self)
        for n, record in enumerate((V010, V020, MDR3, NEW), 1):
            write_run(state, n, record)
        lines = exported(state)
        self.assertEqual(lines, [
            line(1, stage="initial", engine="claude", tier="high", tier_source="rules",
                 not_recorded=sorted([*JEV, "model_choice", "model_judge", "model_judge_confidence",
                                      "tier_floor_raised"])),
            line(2, stage="review", engine="codex", tier="medium", tier_source="rules", model_judge="used",
                 model_choice=exported_choice(("gpt-6.1-sol", "medium"), ("gpt-6-luna", "high")),
                 not_recorded=sorted([*JEV, "model_judge_confidence", "tier_floor_raised"])),
            line(3, stage="review", engine="codex", tier="high", tier_source="jev", rule_tier="medium",
                 jev_tier="high", jev_confidence=0.8, jev_status="used", model_judge="rejected(below-floor)",
                 model_choice=exported_choice(("gpt-6.1-sol", "high"), ("gpt-6.1-sol", "high")),
                 not_recorded=["model_judge_confidence", "tier_floor_raised"]),
            line(4, stage="review", engine="codex", tier="medium", tier_source="rules", rule_tier="medium",
                 jev_tier="max", jev_confidence=0.5, jev_status="below-threshold", model_judge="used",
                 model_choice=exported_choice(("gpt-6.1-sol", "medium"), ("gpt-6.1-sol", "high")), model_judge_confidence=0.72,
                 tier_floor_raised=False),
        ])
        for value in lines:
            keys_of(value)

    def test_a_full_record_exports_axes_outcome_and_candidates(self):
        record = dict(NEW, axes=AXES, exit_code=0, timed_out=False, duration_s=12.5,
                      model_choice=choice(("gpt-6.1-sol", "medium"), ("gpt-6-luna", "high"),
                                          floor={"tier": "medium", "model": "gpt-6.1-sol", "effort": "medium"},
                                          candidates=[["gpt-6-luna", "high"], ["gpt-6.1-sol", "medium"]],
                                          excluded={"gpt-6-sol": "not-in-tier-pool"},
                                          proposal={"model": "gpt-6-luna", "effort": "high"}))
        value = one(self, record)
        self.assertEqual(value["axes"], AXES)
        self.assertEqual(value["outcome"], {"status": "finished", "exit_code": 0, "timed_out": False,
                                            "duration_s": 12.5})
        self.assertEqual(value["model_choice"], {
            "mode": "ranked", "floor": {"tier": "medium", "model": "gpt-6.1-sol", "effort": "medium"},
            "candidates": [pair("gpt-6-luna", "high"), pair("gpt-6.1-sol", "medium")],
            "excluded": [{"model": "gpt-6-sol", "rule": "not-in-tier-pool"}],
            "deterministic": pair("gpt-6.1-sol", "medium"), "proposal": pair("gpt-6-luna", "high"),
            "requested": pair("gpt-6-luna", "high")})
        self.assertEqual((value["unreadable"], value["not_recorded"], value["hashed"]), ({}, [], []))
        keys_of(value)


class FieldsTest(unittest.TestCase):
    """AC2: only the decision fields and the outcome; nothing a person wrote or a machine path."""

    def test_rejected_proposal_identifiers_are_closed(self):
        selected = choice(("gpt-6.1-sol", "high"), ("gpt-6.1-sol", "high"),
                          proposal=pair("secret-project", "private-plan"))
        value = one(self, dict(NEW, model_judge="rejected(outside-candidates)", model_choice=selected))
        for field, original in (("model", "secret-project"), ("effort", "private-plan")):
            self.assertEqual(value["model_choice"]["proposal"][field],
                             "sha256:" + hashlib.sha256(original.encode()).hexdigest())
            self.assertIn("model_choice.proposal." + field, value["hashed"])
            self.assertNotIn(original, json.dumps(value))
        self.assertEqual(value["model_choice"]["deterministic"], pair("gpt-6.1-sol", "high"))
        keys_of(value)

    def test_error_codes_keep_only_known_codes(self):
        for status, expected in (("unused(error:timeout)", "unused(error:timeout)"),
                                 ("unused(error:http-500)", "unused(error:http-500)"),
                                 ("unused(error:bad-request)", "unused(error:bad-request)"),
                                 ("unused(error:http-5000)", "unused(error)"),
                                 ("unused(error:bad-gateway)", "unused(error)"),
                                 ("unused(error: SomeError)", "unused(error)")):
            value = one(self, dict(NEW, model_judge=status, model_judge_confidence=None))
            self.assertEqual(value["model_judge"], expected)
            keys_of(value)

    def test_packaged_model_names_and_unknown(self):
        root = resources.files("oah").joinpath("templates/engines")
        families, benchmarks, normalization = (json.loads(root.joinpath(name).read_text(encoding="utf-8"))
                                                for name in ("families.json", "benchmarks.json", "normalization.json"))
        groups = (set(families["engines"]["codex"]["families"]) | set(families["engines"]["claude"]["aliases"]),
                  set(benchmarks["scores"]),
                  {model for rule in normalization["rules"] for model in rule["models"]})
        self.assertEqual(models_export._model_names(), frozenset().union(*groups))
        for model in (next(iter(group)) for group in groups):
            with self.subTest(model=model):
                selected = choice((model, "high"), (model, "high"))
                self.assertEqual(one(self, dict(NEW, model_choice=selected))["model_choice"]["requested"]["model"], model)
        model = "unknown-private-model"
        selected = choice((model, "high"), (model, "high"))
        result = one(self, dict(NEW, model_choice=selected))
        self.assertEqual(result["model_choice"]["requested"]["model"],
                         "sha256:" + hashlib.sha256(model.encode()).hexdigest())

    def test_new_packaged_model_is_exported(self):
        original = models_export.scoring._packaged

        def packaged(name):
            data = original(name)
            if name == "benchmarks.json":
                document = json.loads(data)
                document["scores"]["new-packaged-model"] = {"coding": 0.5}
                return json.dumps(document).encode()
            return data

        models_export._model_names.cache_clear()
        self.addCleanup(models_export._model_names.cache_clear)
        with mock.patch.object(models_export.scoring, "_packaged", side_effect=packaged):
            selected = choice(("new-packaged-model", "high"), ("new-packaged-model", "high"))
            result = one(self, dict(NEW, model_choice=selected))
        self.assertEqual(result["model_choice"]["requested"]["model"], "new-packaged-model")

    def test_status_vocabulary_and_canaries(self):
        reasons = ("not-configured", "not-enabled", "tier-given", "no status", "secret", "pinned",
                   "floor-unresolved", "no-pool-for-tier", "disabled-by-brief", "unknown-provider", "no key",
                   "no-text", "no-candidates", "one-candidate", "too-many-candidates")
        for field in ("model_judge", "jev_status"):
            for reason in reasons:
                with self.subTest(field=field, reason=reason):
                    status = f"unused({reason})"
                    record = dict(NEW, **{field: status})
                    if field == "jev_status":
                        record.update(jev_tier=None, jev_confidence=None)
                    else:
                        record["model_judge_confidence"] = None
                    self.assertEqual(one(self, record)[field], status)
            for code in ("key-name", "bad-response", "internal", "provider", "timeout", "network", "invalid-header",
                         "unauthorized", "rate-limited", "overloaded"):
                with self.subTest(field=field, code=code):
                    self.assertEqual(one(self, dict(NEW, **{field: f"unused(error:{code})"}))[field], f"unused(error:{code})")
            for status, expected in (("unused(secret project)", "hash"),
                                     ("unused(error: SecretProjectError)", "unused(error)"),
                                     ("rejected(made-up)", "hash"),
                                     ("unused(error:secret-project)", "unused(error)")):
                with self.subTest(field=field, status=status):
                    value = one(self, dict(NEW, **{field: status}))
                    if expected == "hash":
                        expected = "sha256:" + hashlib.sha256(status.encode()).hexdigest()
                        self.assertEqual(value["unreadable"][field], "unknown-value")
                        self.assertIn(field, value["hashed"])
                    self.assertEqual(value[field], expected)
                    self.assertNotIn(status, json.dumps(value))

    def test_model_change_reader_failures_are_named(self):
        digest = "sha256:" + "a" * 64
        for requested, reason in ((None, "wrong-type"), ({"model": "gpt-6.1-sol"}, "wrong-type"),
                                  ({"model": digest, "effort": "high"}, "hashed")):
            with self.subTest(requested=requested):
                selected = choice(("gpt-6.1-sol", "high"), ("gpt-6.1-sol", "high"))
                selected["requested"] = requested
                value = one(self, dict(NEW, model_choice=selected))
                self.assertEqual(value["unreadable"]["model_choice"], reason)
                if reason == "hashed":
                    self.assertIn("model_choice.requested.model", value["hashed"])
                    self.assertEqual(value["model_choice"]["requested"]["model"], digest)
                else:
                    self.assertIsNone(value["model_choice"]["requested"])
                    self.assertEqual(value["unreadable"]["model_choice.requested"], reason)

    def test_canaries_never_reach_the_output(self):
        canaries = {
            "prompt": "CANARY-PROMPT write the secret plan", "path": "/srv/canary-user/private/project",
            "command": "canary-cli --dangerously-skip-permissions", "task": "canary-task-name",
            "warning": "OAH_CANARY_WARNING: canary warning text", "env": "CANARY_HOME=/canary/env/home",
            "note": "canary note text", "error": "OAH_CANARY_ERROR: canary error", "output": "canary worker output",
        }
        record = dict(NEW, axes=dict(AXES, prompt=canaries["prompt"]), command=[canaries["command"]],
                      cwd=canaries["path"], cli_path=canaries["path"] + "/bin/claude", warnings=[canaries["warning"]],
                      error=canaries["error"], worker_output=canaries["output"], advisory=canaries["note"],
                      tier_reasons=[canaries["prompt"]], launcher={"env": canaries["env"]},
                      environment={"CANARY_HOME": canaries["env"]}, brief=canaries["prompt"],
                      model_choice=choice(("gpt-6.1-sol", "medium"), ("gpt-6.1-sol", "high"), note=canaries["note"],
                                          provider=canaries["path"],
                                          floor={"tier": "medium", "model": "gpt-6.1-sol", "effort": "medium",
                                                 "why": canaries["note"]},
                                          candidates=[["gpt-6.1-sol", "high"], {"model": "gpt-6.1-sol", "effort": "medium",
                                                                          "note": canaries["prompt"]}],
                                          proposal={"model": "gpt-6.1-sol", "effort": "high", "why": canaries["prompt"]},
                                          excluded={"gpt-6-sol": "not-allowed"}),
                      exit_code=1, duration_s=3, timed_out=False)
        record["model_choice"]["deterministic"]["source"] = canaries["path"]
        state = make_tmp_dir(self)
        write_run(state, 1, record, task="canary-task-name")
        code, out, err = self.export(state)
        self.assertEqual((code, err), (0, ""))
        for name, canary in canaries.items():
            with self.subTest(canary=name):
                self.assertNotIn(canary, out)
        for fragment in ("canary", "CANARY", "/srv/", "--dangerously", "secret", "quality_order"):
            self.assertNotIn(fragment, out)
        value = json.loads(out)
        keys_of(value)
        self.assertEqual(value["model_choice"]["candidates"], [pair("gpt-6.1-sol", "high"), pair("gpt-6.1-sol", "medium")])
        self.assertEqual(value["unreadable"], {})

    def test_a_value_outside_its_closed_set_is_named_and_never_echoed(self):
        record = dict(NEW, tier="ultra-secret", tier_source="my own words", rule_tier="/tmp/x", jev_tier=3,
                      axes=dict(AXES, kind="a prompt in disguise", risk=["x"]), status="done with notes",
                      exit_code="0", timed_out="no", duration_s=-1,
                      model_judge="something the provider said",
                      model_choice=choice(("m mid with spaces", "medium"), ("gpt-6.1-sol", "HIGH effort"),
                                          mode="free text", candidates=[["gpt-6.1-sol"], "x", ["/etc/passwd", "high"]],
                                          excluded={"gpt-6-sol": "my reason", "/a/path": "not-allowed"}))
        value = one(self, record)
        self.assertEqual(value["unreadable"], {
            "tier": "unknown-value", "tier_source": "unknown-value", "rule_tier": "unknown-value",
            "jev_tier": "wrong-type", "axes.kind": "unknown-value", "axes.risk": "wrong-type",
            "outcome.status": "unknown-value", "outcome.exit_code": "wrong-type", "outcome.timed_out": "wrong-type",
            "outcome.duration_s": "wrong-type", "model_judge": "unknown-value",
            "model_choice.mode": "unknown-value", "model_choice.deterministic.model": "unknown-value",
            "model_choice.requested.effort": "unknown-value", "model_choice.candidates[0]": "wrong-type",
            "model_choice.candidates[1]": "wrong-type", "model_choice.candidates[2].model": "unknown-value",
            "model_choice.excluded[0].rule": "unknown-value", "model_choice.excluded[1].model": "unknown-value"})
        shown = json.dumps(value)
        for text in ("ultra-secret", "my own words", "/tmp/x", "disguise", "done with", "provider said", "m mid",
                     "HIGH", "free text", "/etc/passwd", "my reason", "/a/path"):
            self.assertNotIn(text, shown)
        digest = lambda text: "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        self.assertEqual(value["model_choice"]["candidates"],
                         [None, digest("x"), {"model": digest("/etc/passwd"), "effort": "high"}])
        self.assertEqual(value["model_choice"]["excluded"], [{"model": "gpt-6-sol", "rule": digest("my reason")},
                                                             {"model": digest("/a/path"), "rule": "not-allowed"}])
        self.assertEqual(value["tier"], digest("ultra-secret"))
        keys_of(value)

    def test_odd_numbers_never_stop_the_export(self):
        state = make_tmp_dir(self)
        write_run(state, 1, dict(NEW, duration_s=float("nan"), exit_code=True, jev_confidence=10**400))
        write_run(state, 2, dict(NEW, duration_s=10**400, exit_code=10**400))
        first, second = exported(state)
        self.assertEqual(first["unreadable"], {"outcome.duration_s": "wrong-type", "outcome.exit_code": "wrong-type",
                                               "jev_confidence": "wrong-type"})
        self.assertEqual((second["outcome"]["duration_s"], second["outcome"]["exit_code"]), (10**400, 10**400))
        self.assertEqual(second["unreadable"], {})

    def test_the_jev_status_keeps_only_its_word(self):
        value = one(self, dict(NEW, jev_status="below-threshold 0.5"))
        self.assertEqual(value["jev_status"], "below-threshold")

    def test_export(self):
        code, out, err = self.export(ages_state(self))
        self.assertEqual((code, err), (0, ""))
        for text in out.splitlines():
            keys_of(json.loads(text))

    def export(self, state: pathlib.Path) -> tuple[int, str, str]:
        return CliTest.export(self, state)


class NullsTest(unittest.TestCase):
    """AC3: every null that is not the record's own null is named with its reason, and every folder yields a line."""

    def test_each_unreadable_folder_yields_one_line_with_its_reason(self):
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
        (state / "runs" / "t1" / "intent-binding.json").write_text("{}\n", encoding="utf-8")
        (state / "runs" / "t1" / ".r20261101T120000Z-00000000.tmp").mkdir()
        (state / "runs" / "t1" / "notes.txt").write_text("x", encoding="utf-8")
        (state / "runs" / "README").write_text("x", encoding="utf-8")
        lines = exported(state)
        self.assertEqual(lines[:8], [unreadable_line(1, "missing"), unreadable_line(2, "invalid-json"),
                                     unreadable_line(3, "not-object"), unreadable_line(4, "stage:wrong-type"),
                                     unreadable_line(5, "engine:hashed"), unreadable_line(6, "invalid-json"),
                                     unreadable_line(7, "linked"), unreadable_line(8, "not-a-folder")])
        self.assertEqual([value["run_id"] for value in lines], [run_id(n) for n in range(1, 10)])
        self.assertEqual(lines[8]["unreadable"], {})
        self.assertEqual(len(lines), models_report.collect(state)["records"]["run_folders"])
        for value in lines:
            keys_of(value)

    def test_one_line_per_counted_folder_in_task_then_run_order(self):
        state = ages_state(self)
        write_run(state, 5, NEW, task="t0")
        write_run(state, 6, None, raw=b"{", task="t2")
        outside = make_tmp_dir(self)
        write_run(outside, 7, NEW, task="t3")
        directory_alias(outside / "runs" / "t3", state / "runs" / "t3")
        lines = exported(state)
        self.assertEqual([value["run_id"] for value in lines], [run_id(n) for n in (5, 1, 2, 3, 4, 6)])
        report = models_report.collect(state)["records"]
        self.assertEqual(len(lines), report["run_folders"])
        self.assertEqual(sum(1 for value in lines if "record" in value["unreadable"]),
                         sum(report["unreadable"].values()))

    def test_a_partial_feature_is_null_and_named_as_the_report_decides(self):
        # A judge confidence without its judge makes the confidence partial too, as the report decides.
        groups = {"jev_status": JEV, "jev_truncated": JEV,
                  "model_choice": ["model_choice", "model_judge", "model_judge_confidence"],
                  "tier_floor_raised": None, "model_judge_confidence": None}
        for missing, fields in groups.items():
            with self.subTest(missing=missing):
                record = dict(NEW)
                del record[missing]
                value = one(self, record)
                if fields is None:  # a one-key feature is absent, never partial
                    self.assertEqual(value["not_recorded"], [missing])
                    self.assertEqual(value["unreadable"], {})
                else:
                    self.assertEqual(value["unreadable"], dict.fromkeys(fields, "inconsistent"))
                    for name in fields:
                        self.assertIsNone(value[name])
        record = dict(NEW)
        del record["model_judge"], record["model_choice"]  # a confidence without the judge: the report's partial
        value = one(self, record)
        self.assertEqual(value["unreadable"], dict.fromkeys(["model_choice", "model_judge", "model_judge_confidence"],
                                                            "inconsistent"))

    def test_values_the_report_finds_inconsistent_are_null_and_named(self):
        cases = (
            (dict(NEW, jev_status="used", jev_tier=None), {"jev_tier": "inconsistent", "rule_tier": "inconsistent"}),
            (dict(NEW, jev_status="below-threshold none", jev_confidence=0.5), {"jev_confidence": "inconsistent"}),
            (dict(NEW, model_judge="unused(pinned)", model_judge_confidence=0.4),
             {"model_judge_confidence": "inconsistent"}),
        )
        for record, unreadable in cases:
            with self.subTest(unreadable=unreadable):
                value = one(self, record)
                self.assertEqual(value["unreadable"], unreadable)
                for name in unreadable:
                    self.assertIsNone(value[name])

    def test_the_report_and_the_export_agree_on_what_is_unreadable(self):
        state = make_tmp_dir(self)
        records = (dict(NEW, tier_floor_raised="yes"), dict(NEW, model_judge_confidence=1.5),
                   dict(NEW, jev_confidence="high"), dict(NEW, jev_status="used", jev_tier=None), NEW)
        for n, record in enumerate(records, 1):
            write_run(state, n, record)
        partial = models_report.collect(state)["records"]["partial"]
        self.assertEqual(partial, 4)
        self.assertEqual(sum(1 for value in exported(state) if value["unreadable"]), partial)

    def test_a_null_key_is_not_named(self):
        value = one(self, dict(NEW, model_judge=None, model_choice=None, model_judge_confidence=None,
                               jev_status=None, jev_tier=None, jev_confidence=None, rule_tier=None,
                               tier_floor_raised=None, tier_source=None, status="refused"))
        self.assertEqual((value["unreadable"], value["not_recorded"], value["hashed"]), ({}, [], []))
        self.assertEqual(value["outcome"]["status"], "refused")

    def test_a_missing_older_key_is_not_recorded(self):
        record = dict(V010)
        del record["axes"], record["tier_source"], record["exit_code"]
        value = one(self, record)
        self.assertIn("axes", value["not_recorded"])
        self.assertIn("tier_source", value["not_recorded"])
        self.assertIn("outcome.exit_code", value["not_recorded"])
        self.assertEqual(value["unreadable"], {})


class HashedTest(unittest.TestCase):
    """AC4: a value hashed in a correction record stays hashed and is named, never matched to a name."""

    def test_correction_records_keep_hashes_and_name_them(self):
        record = dict(NEW, stage="correction", axes=AXES, model_judge="unused(error: RuntimeError)",
                      model_judge_confidence=None,
                      model_choice=choice(("gpt-6.1-sol", "high"), ("gpt-6-luna", "high"),
                                          floor={"tier": "medium", "model": "gpt-6.1-sol", "effort": "high"},
                                          candidates=[["gpt-6.1-sol", "high"]], excluded={"gpt-5": "not-allowed"},
                                          proposal={"model": "gpt-6-luna", "effort": "high"}))
        hashed = runs._correction_record(record, runs.RESULT_FIELDS)
        value = one(self, hashed)
        self.assertEqual(value["hashed"], sorted([
            "model_judge", "model_choice.deterministic.model", "model_choice.requested.model",
            "model_choice.proposal.model", "model_choice.floor.model", "model_choice.candidates[0].model",
            "model_choice.excluded[0].model"]))
        choice_value = value["model_choice"]
        self.assertEqual(choice_value["deterministic"]["model"], hashed["model_choice"]["deterministic"]["model"])
        self.assertEqual(value["model_judge"], hashed["model_judge"])
        self.assertTrue(value["model_judge"].startswith("sha256:"))
        self.assertEqual(choice_value["excluded"], [{"model": next(iter(hashed["model_choice"]["excluded"])),
                                                     "rule": "not-allowed"}])
        self.assertEqual(choice_value["deterministic"]["effort"], "high")
        self.assertEqual(value["axes"], AXES)
        self.assertEqual(value["unreadable"], {})
        shown = json.dumps(value)
        for name in ("gpt-6.1-sol", "gpt-6-luna", "gpt-5", "RuntimeError"):
            self.assertNotIn(name, shown)

    def test_a_hashed_model_choice_stays_one_hash(self):
        digest = "sha256:" + "c" * 64
        value = one(self, dict(NEW, model_choice=digest))
        self.assertEqual((value["model_choice"], value["hashed"], value["unreadable"]),
                         (digest, ["model_choice"], {"model_choice": "hashed"}))


class Forbidden(BaseException):
    """The export reached for a process, the network, a provider or a write. BaseException, so nothing swallows it."""


def refuse(name):
    def refused(*args, **kwargs):
        raise Forbidden(f"the export used {name}")
    return refused


_REAL_IO_OPEN = io.open
_REAL_OS_OPEN = os.open


def read_only_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+"):
        raise Forbidden(f"the export opened {file} to write")
    return _REAL_IO_OPEN(file, mode, *args, **kwargs)


def read_only_os_open(path, flags, *args, **kwargs):
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
        raise Forbidden(f"the export opened {path} to write")
    return _REAL_OS_OPEN(path, flags, *args, **kwargs)


class NoSideEffectsTest(unittest.TestCase):
    """AC5: no provider, network, process or write is reachable from the export."""

    def guard(self):
        stack = contextlib.ExitStack()
        for target in ("subprocess.Popen", "subprocess.run", "os.system", "os.posix_spawn", "socket.socket",
                       "socket.create_connection", "urllib.request.urlopen", "os.replace", "os.rename", "os.mkdir",
                       "os.makedirs", "os.remove", "os.unlink", "os.rmdir", "os.symlink", "os.link", "os.utime",
                       "os.chmod", "os.truncate"):
            owner, _, name = target.rpartition(".")
            if hasattr(importlib.import_module(owner), name):  # os.posix_spawn is POSIX only
                stack.enter_context(mock.patch(target, refuse(target)))
        stack.enter_context(mock.patch("io.open", read_only_open))
        stack.enter_context(mock.patch("builtins.open", read_only_open))
        stack.enter_context(mock.patch("os.open", read_only_os_open))
        for owner, name in ((rerank, "judge_model"), (rerank, "advise_difficulty"), (rerank, "rerank"),
                            (typesafe.TypeSafeProvider, "_post_result"), (typesafe.TypeSafeProvider, "choose_model"),
                            (typesafe.TypeSafeProvider, "advise"), (urllib.request.OpenerDirector, "open")):
            stack.enter_context(mock.patch.object(owner, name, refuse(f"{owner.__name__}.{name}")))
        return stack

    def test_the_export_and_its_command_touch_nothing(self):
        state = ages_state(self)
        write_run(state, 9, None, raw=b"{")
        write_run(state, 10, runs._correction_record(dict(NEW, stage="correction"), runs.RESULT_FIELDS))
        before = tree(state)
        info = RepoInfo(root=state, common_dir=state, branch=None, head=None)
        out = io.StringIO()
        with self.guard():
            lines = models_export.lines(state)
            with mock.patch.object(models_cli, "repo_info", return_value=info), \
                    mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(state)}), contextlib.redirect_stdout(out):
                self.assertEqual(run_models("export")[0], 0)
        self.assertEqual(len(lines), 6)
        self.assertEqual(tree(state), before)

    def test_the_guard_catches_each_kind_of_use(self):
        folder = make_tmp_dir(self)
        with self.guard():
            for action in (lambda: subprocess.Popen(["git"]), lambda: socket.socket(),
                           lambda: urllib.request.urlopen("https://example.invalid"), lambda: os.replace("a", "b"),
                           lambda: rerank.judge_model({}, [], config={}),
                           lambda: (folder / "x").write_text("x", encoding="utf-8"),
                           lambda: open(folder / "y", "a", encoding="utf-8"),
                           lambda: os.open(folder / "z", os.O_WRONLY | os.O_CREAT)):
                with self.subTest(action=action), self.assertRaises(Forbidden):
                    action()
        self.assertEqual(list(folder.iterdir()), [])


class CliTest(unittest.TestCase):
    """AC1: `oah models export` prints one JSON line per run folder; the rest of `oah models` is unchanged."""

    def export(self, state: pathlib.Path, *argv: str, action: str = "export") -> tuple[int, str, str]:
        info = RepoInfo(root=state, common_dir=state, branch=None, head=None)
        with mock.patch.object(models_cli, "repo_info", return_value=info), \
                mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(state)}):
            return run_models(action, *argv)

    def test_one_sorted_ascii_json_object_per_line(self):
        state = ages_state(self)
        write_run(state, 9, None, raw=b"[]")
        code, out, err = self.export(state)
        self.assertEqual((code, err), (0, ""))
        texts = out.splitlines()
        self.assertEqual(len(texts), 5)
        self.assertTrue(out.endswith("\n"))
        for text, value in zip(texts, models_export.lines(state)):
            self.assertEqual(text, json.dumps(value, ensure_ascii=True, sort_keys=True))
            self.assertTrue(text.isascii())
            parsed = json.loads(text)
            self.assertEqual((parsed["format"], parsed["run_id"]), (models_export.FORMAT, value["run_id"]))
        self.assertEqual(json.loads(texts[-1])["unreadable"], {"record": "not-object"})

    def test_unreadable_package_data_hashes_models_and_report_still_works(self):
        state = ages_state(self)
        models_export._model_names.cache_clear()
        self.addCleanup(models_export._model_names.cache_clear)
        with mock.patch.object(models_export.scoring, "_packaged", side_effect=OSError("unreadable")):
            code, out, err = self.export(state)
            report_code, report_out, report_err = self.export(state, action="report")
        values = [json.loads(text) for text in out.splitlines()]
        self.assertEqual((code, len(values)), (0, 4))
        self.assertEqual(err, "Note: the model list could not be read; model names will be hashed.\n")
        self.assertEqual(values[1]["model_choice"]["requested"]["model"],
                         "sha256:" + hashlib.sha256(b"m-strong").hexdigest())
        self.assertIn("model_choice.requested.model", values[1]["hashed"])
        self.assertEqual((report_code, report_err), (0, ""))
        self.assertIn("4 run folders: 4 read", report_out)

    def test_no_run_records_prints_nothing(self):
        self.assertEqual(self.export(make_tmp_dir(self)), (0, "", ""))

    def test_outside_a_repository_is_an_environment_error(self):
        from oah.paths import NotARepo

        with mock.patch.object(models_cli, "repo_info", side_effect=NotARepo("OAH_NOT_A_REPO: not a repo")):
            code, out, err = run_models("export")
        self.assertEqual((code, out), (models_cli.EXIT_ENVIRONMENT, ""))
        self.assertIn("OAH_NOT_A_REPO", err)

    def test_a_linked_runs_folder_is_an_environment_error(self):
        state, outside = make_tmp_dir(self), make_tmp_dir(self)
        (outside / "runs").mkdir()
        directory_alias(outside / "runs", state / "runs")
        code, out, err = self.export(state)
        self.assertEqual((code, out), (models_cli.EXIT_ENVIRONMENT, ""))
        self.assertIn("OAH_RUN_PATH_UNSAFE", err)

    def test_refresh_does_not_apply_to_the_export(self):
        state = make_tmp_dir(self)
        info = RepoInfo(root=state, common_dir=state, branch=None, head=None)
        with mock.patch.object(models_cli, "repo_info", return_value=info):
            code, out, err = run_models("--refresh", "export")
        self.assertEqual((code, out), (models_cli.EXIT_USAGE, ""))
        self.assertIn("OAH_USAGE", err)
        self.assertIn("export", err)

    def test_the_parser_keeps_bare_models_and_report(self):
        from tests.test_models_report import parser

        for argv, expected in ((["models"], (None, False, False)), (["models", "--json"], (None, False, True)),
                               (["models", "--refresh", "--json"], (None, True, True)),
                               (["models", "report"], ("report", False, False)),
                               (["models", "report", "--json"], ("report", False, True)),
                               (["models", "export"], ("export", False, False))):
            with self.subTest(argv=argv):
                args = parser().parse_args(argv)
                self.assertEqual((args.models_action, args.refresh, args.json), expected)

    def test_bare_models_never_reads_run_records(self):
        with mock.patch.object(models_export, "lines", refuse("lines")), \
                mock.patch.object(models_cli, "repo_info", side_effect=Forbidden("bare models went on")):
            with self.assertRaises(Forbidden) as caught:
                run_models()
        self.assertEqual(str(caught.exception), "bare models went on")

    def test_the_report_is_unchanged_on_the_same_folder(self):
        state = ages_state(self)
        before = self.export(state, action="report"), self.export(state, "--json", action="report")
        self.assertEqual(self.export(state)[0], 0)
        after = self.export(state, action="report"), self.export(state, "--json", action="report")
        self.assertEqual(before, after)
        self.assertEqual(before[0][1].splitlines(), REPORT_TEXT)


class DocsTest(unittest.TestCase):
    """AC6: both languages say what a line holds and leaves out, what it cannot show, and what older runs lack."""

    def test_docs_describe_the_export(self):
        for page in ("docs/concepts.md", "docs/upgrade.md", "docs/ko/concepts.md", "docs/ko/upgrade.md"):
            with self.subTest(page=page):
                text = (REPO / page).read_text(encoding="utf-8")
                self.assertIn("oah models export", text)
        concepts = (REPO / "docs/concepts.md").read_text(encoding="utf-8")
        for phrase in ("not_recorded", "unreadable", "hashed", "not whether Jev helped", "no prompt",
                       "older runs"):
            self.assertIn(phrase, concepts)
        korean = (REPO / "docs/ko/concepts.md").read_text(encoding="utf-8")
        for phrase in ("not_recorded", "unreadable", "hashed", "Jev가 도움이 됐는지", "프롬프트"):
            self.assertIn(phrase, korean)


if __name__ == "__main__":
    unittest.main()
