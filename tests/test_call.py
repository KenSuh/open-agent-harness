# oah call·wait 검사 — 가짜 엔진으로 실행 폴더 전체, 작업자 환경, 단계 강제·우회 기록, 읽기 전용 리뷰, 단 출처, 헤더 낮춤, 시간 초과 시 프로세스 트리 종료, 백그라운드와 대기를 본다.
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import re
import secrets as token_source
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

from oah import call, call_cli, intent, models, models_cli, rerank, runs, scoring
from oah.briefs import VERIFY_FOOTER, VERIFY_MARKER, BriefError, raw_section
from oah.config import ConfigError
from oah.engines.effort import EngineError
from oah.lease import Store
from oah.paths import repo_info
from oah.review import receipts
from oah.routing import RoutingError
from oah.runs import RESULT_FIELDS, UNVERIFIED, find_run, new_run
from oah.secrets import NoKeychain
from tests.helpers import SHIPPED_DEFAULTS, directory_alias, git_commit, make_git_repo, make_tmp_dir, shipped_defaults

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_CODEX = [sys.executable, str(FAKES / "fake_codex.py")]
FAKE_CLAUDE = [sys.executable, str(FAKES / "fake_claude.py")]
MARKER = "<!-- OAH_INTENT_FOOTER -->"
FOOTER = f"{MARKER}\n## Intent rules\n- End with INTENT and AC lines.\n"
SECTION = ("<!-- OAH_INTENT:BEGIN -->\n## Intent\nIntent-ID: t1\nRevision: 2\nStatus: accepted\n"
           "### Problem\nReports miss files.\n### Desired outcome\nEvery file is listed.\n"
           "### Success conditions\n- AC1: the report lists every file\n### Constraints\n"
           "### Non-goals\n### Open questions\n<!-- OAH_INTENT:END -->\n")


def has_footer(text: str) -> bool:
    return any(line == MARKER for line in text.replace("\r\n", "\n").split("\n"))


def parse_intent(text: str) -> intent.Intent:
    """Use the landed parser for launch fixtures as well as binding checks."""
    return intent.parse(text)


def toml_list(argv: list[str]) -> str:
    return "[" + ", ".join(json.dumps(part) for part in argv) + "]"


class Project:
    """A temporary repository whose config points at the fake engines."""

    def __init__(self, test: unittest.TestCase, config: str = "") -> None:
        self.test = test
        self.repo = make_git_repo(test)
        git_commit(self.repo, {"README.md": "x\n"})
        work = make_tmp_dir(test)
        self.state, self.home, self.record = work / "state", work / "codex-home", work / "record.json"
        self.home.mkdir()
        (self.home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-test-a", "is_default": True,
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        ]}), encoding="utf-8")
        (self.repo / ".oah").mkdir()
        # These fixtures name their models, so that no launch test depends on the bundled scores.
        order = "" if "quality_order" in config else 'quality_order = ["gpt-test-a"]\n'
        (self.repo / ".oah" / "config.toml").write_text(
            f"[engines.claude]\ncommand = {toml_list(FAKE_CLAUDE)}\nquality_order = [\"opus\", \"sonnet\"]\n"
            f"[engines.codex]\ncommand = {toml_list(FAKE_CODEX)}\n{order}" + config, encoding="utf-8")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("FAKE_", "OAH_")) and key != "CLAUDE_CONFIG_DIR"}
        self.env.update(CODEX_HOME=str(self.home), FAKE_RECORD=str(self.record), OAH_STATE_DIR=str(self.state),
                        HOME=str(work / "home"), USERPROFILE=str(work / "home"))

    def settings(self) -> call.Settings:
        return call_cli.load_settings(self.repo)

    def bind(self, task: str = "t1", *, root: pathlib.Path | None = None, path: pathlib.Path | None = None):
        """Establish a real accepted lease for positive launch tests; never refresh an existing binding."""
        root = root or self.repo
        store = Store(self.state)
        holder = next((lease for lease, _ in store.snapshot() if lease.task == task), None)
        if holder is not None and holder.binding() is not None:
            return holder
        path = path or root / f".oah/tasks/{task}/PACKET.md"
        path = path if path.is_absolute() else root / path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SECTION.replace("Intent-ID: t1", f"Intent-ID: {task}"), encoding="utf-8", newline="")
        bound = intent.parse(intent.read_source(path)).bind(path.relative_to(root).as_posix())
        if holder is not None:
            return store.rebind(task=task, agent=holder.agent, binding=bound, ttl_hours=1)[0]
        return store.start(task=task, agent="codex", scope=(f".oah/tasks/{task}",), note="launch fixture",
                           ttl_hours=1, info=repo_info(root), intent=bound)

    def prepare(self, engine: str = "codex", stage: str = "review", env: dict | None = None, **fields):
        fields.setdefault("task", "t1")
        self.bind(fields["task"], path=fields.get("packet"))
        request = call.CallRequest(engine=engine, stage=stage, **fields)
        return call.prepare(request, self.settings(), root=self.repo, state_dir=self.state,
                            env=dict(self.env, **(env or {})), footer=FOOTER, has_footer=has_footer,
                            parse_intent=parse_intent, read_source=intent.read_source, keychain=NoKeychain())

    def run(self, engine: str = "codex", stage: str = "review", env: dict | None = None, **fields) -> dict:
        prepared = self.prepare(engine, stage, env=env, **fields)
        return call.execute(prepared, base_env=dict(self.env, **(env or {})))

    def recorded(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def packet(self, body: str = "", *, section: str = SECTION, name: str = ".oah/tasks/t1/PACKET.md",
               newline: str = "\n") -> pathlib.Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((section + body).replace("\n", newline).encode("utf-8"))
        return path

    def runs(self) -> list[str]:
        folder = self.state / "runs" / "t1"
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []


SCORES = {"schema": 1, "source": "Epoch AI", "date": "2026-11-02", "license": "CC BY 4.0",
          "url": "https://epoch.ai/benchmarks/use-this-data", "credit": "Epoch AI, a credit line.",
          "scores": {"gpt-6-luna": dict.fromkeys(("t1", "t2", "t3", "t4"), 0.1),
                     "gpt-6.1-terra": dict.fromkeys(("t1", "t2", "t3", "t4"), 0.8),
                     "gpt-6.1-sol": dict.fromkeys(("t1", "t2", "t3", "t4"), 0.95),
                     "gpt-6-astra": dict.fromkeys(("t1", "t2", "t3", "t4"), 1.0)}}
SCORED_MODELS = ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def write_models(home: pathlib.Path, models=SCORED_MODELS, hidden=()) -> None:
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": slug, "visibility": "hide" if slug in hidden else "list",
         "supported_reasoning_levels": [{"effort": effort} for effort in EFFORTS]} for slug in (*models, *hidden)]}),
        encoding="utf-8")


def scored_project(test: unittest.TestCase, config: str = "", *, models=SCORED_MODELS, order: str = "[]", hidden=(),
                   scores: dict | None = None) -> Project:
    """A project with no user choice of model, so the score rule decides, on scores no data refresh can move."""
    project = Project(test, f"quality_order = {order}\n" + config)
    write_models(project.home, models, hidden)
    patcher = mock.patch.object(scoring, "bundled", return_value=SCORES if scores is None else scores)
    patcher.start()
    test.addCleanup(patcher.stop)
    return project


# A stored Epoch AI copy that ranks the four models apart from SCORES: here sol clears the high step, terra does not.
STORED_RANKING = {"gpt-6-luna": 0.1, "gpt-6.1-terra": 0.2, "gpt-6.1-sol": 0.95, "gpt-6-astra": 1.0}
STORED_TASKS = ("Chess Puzzles", "EBR-bench", "Mystery Game Puzzles", "Furniture Assembly")  # allow-listed


def write_stored_epoch(state: pathlib.Path, ranking: dict, *, fetched: dt.datetime | None = None) -> pathlib.Path:
    """A fresh stored copy, as a fetch writes it, so that no rebuild is due to fetch it again."""
    fetched = fetched or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    date = fetched.date()
    data = {"schema": 1, "source": "Epoch AI", "date": date.isoformat(), "license": "CC BY 4.0",
            "url": "https://epoch.ai/benchmarks/use-this-data",
            "credit": ("Epoch AI, 'Capabilities & benchmarking'. Published online at epoch.ai. Retrieved from "
                       "'https://epoch.ai/benchmarks/use-this-data'. Accessed "
                       f"{date.day} {('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')[date.month - 1]} {date.year}."),
            "scores": {model: dict.fromkeys(STORED_TASKS, value) for model, value in ranking.items()},
            "fetched_at": fetched.strftime("%Y-%m-%dT%H:%M:%SZ"), "source_url": "https://epoch.ai/data/benchmarks.csv"}
    path = state / "models" / "benchmarks-epoch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class ScoredCallTest(unittest.TestCase):
    def test_every_profile_can_launch_a_capped_model_with_its_own_highest_floor(self):
        for profile in ("performance", "balanced", "efficient"):
            with self.subTest(profile=profile):
                extra = (f'[models]\nprofile = "{profile}"\n'
                         '[engines.codex.tiers.medium]\neffort = "highest"\n'
                         '[engines.codex.tiers.high]\neffort = "highest"\n'
                         '[engines.codex.effort_caps]\n' + ''.join(
                             f'"{name}" = "{"high" if name == "gpt-6.1-sol" else "low"}"\n' for name in SCORED_MODELS))
                project = scored_project(self, extra)
                cache = json.loads((project.home / "models_cache.json").read_bytes())
                for info in cache["models"]:
                    if info["slug"] == "gpt-6.1-sol":
                        info["supported_reasoning_levels"] = [{"effort": e} for e in ("low", "medium", "high")]
                (project.home / "models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
                data = project.run(stage="review", tier="high")
                self.assertEqual((data["resolved_model"], data["launch_effort"]), ("gpt-6.1-sol", "high"))
                self.assertTrue(data["admission"]["floor_checked"])

    def test_the_rule_picks_the_codex_model_at_every_tier(self):
        rows = {"low": "gpt-6-luna", "medium": "gpt-6.1-terra", "high": "gpt-6.1-terra", "max": "gpt-6-astra"}
        for tier, model in rows.items():
            with self.subTest(tier=tier):
                project = scored_project(self)
                data = project.run(stage="correction", tier=tier, axes={"kind": "quick"})
                self.assertEqual((data["status"], data["resolved_model"], data["model_source"]),
                                 ("finished", model, "scored"))

    def test_the_profile_moves_the_choice_with_no_other_edit(self):
        rows = {"performance": "gpt-6-astra", "balanced": "gpt-6.1-terra", "efficient": "gpt-6.1-terra"}
        for profile, model in rows.items():
            with self.subTest(profile=profile):
                project = scored_project(self, f'[models]\nprofile = "{profile}"\n')
                self.assertEqual(project.run(stage="correction", tier="high")["resolved_model"], model)

    def test_efficient_takes_the_most_score_per_cost_with_no_step_down(self):
        rows = {"low": "gpt-6.1-terra", "medium": "gpt-6.1-terra", "high": "gpt-6.1-terra", "max": "gpt-6.1-terra"}
        for tier, model in rows.items():
            with self.subTest(tier=tier):
                project = scored_project(self, '[models]\nprofile = "efficient"\n')
                data = project.run(stage="correction", tier=tier, axes={"kind": "quick"})
                self.assertEqual((data["tier"], data["resolved_model"]), (tier, model))

    def test_efficient_passes_over_a_model_its_cap_keeps_below_the_floor(self):
        cap = '[engines.codex.effort_caps]\n"gpt-6.1-terra" = "low"\n'
        project = scored_project(self, '[models]\nprofile = "efficient"\n' + cap)
        data = project.run(stage="correction", tier="high", axes={"kind": "security"})
        self.assertEqual((data["status"], data["resolved_model"], data["launch_effort"]),
                         ("finished", "gpt-6.1-sol", "high"))

    def test_every_profile_skips_a_cap_below_the_floor(self):
        cap = '[engines.codex.effort_caps]\n"gpt-6.1-terra" = "low"\n'
        for profile, expected in (("balanced", "gpt-6.1-sol"), ("performance", "gpt-6-astra")):
            with self.subTest(profile=profile):
                project = scored_project(self, f'[models]\nprofile = "{profile}"\n' + cap)
                data = project.run(stage="correction", tier="high", axes={"kind": "security"})
                self.assertEqual((data["status"], data["resolved_model"]), ("finished", expected))

    def test_efficient_still_clamps_a_capped_model_that_reaches_the_floor(self):
        cap = '[engines.codex.effort_caps]\n"gpt-6.1-terra" = "medium"\n'
        project = scored_project(self, '[models]\nprofile = "efficient"\n' + cap)
        data = project.run(stage="correction", tier="high", axes={"kind": "quick"})
        self.assertEqual((data["resolved_model"], data["launch_effort"]), ("gpt-6.1-terra", "medium"))

    def test_the_effort_rules_are_what_a_launch_would_refuse(self):
        entry = models.Entry("codex", "1", "x", (models.ModelInfo("gpt-a", ("medium", "high")),), "test")

        def passes(tier: str, floor: str, cap: str | None = None) -> bool:
            settings = call.Settings.defaults()
            settings.engines["codex"]["effort_caps"].update({"gpt-a": cap} if cap else {})
            return call._passes_effort_rules("codex", tier, floor, "gpt-a", settings, entry)

        self.assertFalse(passes("high", "low"), "the model must support its configured floor effort too")
        self.assertFalse(passes("low", "low"), "the tier's own effort must be listed")
        self.assertTrue(passes("high", "medium"))
        self.assertFalse(passes("high", "medium", "low"), "the cap keeps it below the floor")
        self.assertTrue(passes("high", "medium", "medium"), "a cap that reaches the floor only clamps")
        self.assertFalse(passes("medium", "high", "max"), "with a cap, the launch effort must reach the floor too")

    def test_every_kind_of_call_is_chosen_by_difficulty(self):
        project = scored_project(self)
        easy = project.run(stage="correction", axes={"kind": "docs", "intensity": "low", "reasoning": "low", "risk": "low"})
        hard = project.run(stage="correction", axes={"kind": "security", "reasoning": "max"})
        self.assertEqual((easy["tier"], easy["resolved_model"]), ("low", "gpt-6-luna"))
        self.assertEqual((hard["tier"], hard["resolved_model"]), ("max", "gpt-6-astra"))

    def test_an_explicit_non_review_tier_can_be_below_its_kind_floor(self):
        for stage, engine in (("correction", "codex"), ("initial", "claude")):
            for source in ("flag", "packet"):
                with self.subTest(stage=stage, source=source):
                    project = scored_project(self)
                    if source == "packet":
                        project.packet(f"## Plan\n- {stage} → {engine} → low\n")
                    fields = {"tier": "low"} if source == "flag" else {}
                    data = project.run(engine=engine, stage=stage, axes={"kind": "security"}, **fields)
                    self.assertEqual((data["status"], data["tier"], data["tier_source"], data["launch_effort"]),
                                     ("finished", "low", source, "low"))
                    self.assertEqual(data["admission"]["floor_tier"], "low")
                    self.assertEqual(data["model_choice"]["floor"]["tier"], "low")

    def test_the_codex_configuration_and_default_flag_no_longer_choose(self):
        project = scored_project(self)
        (project.home / "config.toml").write_text('model = "gpt-6-astra"\n', encoding="utf-8")
        cache = json.loads((project.home / "models_cache.json").read_text(encoding="utf-8"))
        next(item for item in cache["models"] if item["slug"] == "gpt-6-astra")["is_default"] = True
        (project.home / "models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
        data = project.run(stage="correction", tier="low", axes={"kind": "quick"})
        self.assertEqual((data["resolved_model"], data["model_source"]), ("gpt-6-luna", "scored"))

    def test_an_unreadable_codex_configuration_no_longer_stops_a_scored_call(self):
        project = scored_project(self)
        (project.home / "config.toml").write_text("not toml [", encoding="utf-8")
        self.assertEqual(project.run(stage="correction", tier="low", axes={"kind": "quick"})["status"], "finished")

    def test_claude_is_chosen_from_scores_with_the_old_default_list_in_the_file(self):
        project = Project(self, '[engines.claude.tiers.max]\neffort = "max"\n')
        config = project.repo / ".oah" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace('quality_order = ["opus", "sonnet"]',
                                                                     'quality_order = ["opus", "sonnet", "haiku"]'),
                          encoding="utf-8")
        before = config.read_bytes()
        scores = dict(SCORES, scores={"claude-opus-5-5": dict.fromkeys(("t1", "t2", "t3"), 1.0),
                                      "claude-sonnet-5-5": dict.fromkeys(("t1", "t2", "t3"), 0.2),
                                      "claude-fable-5-1": dict.fromkeys(("t1", "t2", "t3"), 0.1)})
        with mock.patch.object(scoring, "bundled", return_value=scores):
            low = project.run(engine="claude", stage="initial", tier="low", axes={"kind": "quick"})
            high = project.run(engine="claude", stage="initial", tier="high", axes={"kind": "quick"})
        self.assertEqual((low["resolved_model"], low["model_source"]), ("sonnet", "scored"))
        self.assertEqual((high["resolved_model"], high["model_source"]), ("opus", "scored"))
        self.assertEqual(config.read_bytes(), before)

    def test_effort_caps_still_clamp_a_scored_model(self):
        project = scored_project(self, '[engines.codex.effort_caps]\n"gpt-6.1-terra" = "medium"\n')
        data = project.run(stage="correction", tier="high", axes={"kind": "quick"})
        self.assertEqual((data["resolved_model"], data["launch_effort"], data["requested_effort"]),
                         ("gpt-6.1-terra", "medium", "high"))
        self.assertTrue(any(warning.startswith("OAH_EFFORT_CAPPED:") for warning in data["warnings"]))

    def test_there_is_no_built_in_effort_cap(self):
        project = scored_project(self)
        data = project.run(stage="correction", tier="max", axes={"kind": "quick"})
        self.assertEqual(data["launch_effort"], "max")

    def test_a_launch_scores_with_the_stored_copy_and_names_its_source(self):
        shipped_defaults(self)
        project = scored_project(self)
        write_stored_epoch(project.state, STORED_RANKING)
        data = project.run(stage="correction", tier="high", axes={"kind": "quick"})
        self.assertEqual(data["resolved_model"], "gpt-6.1-sol", "the stored copy ranks sol first; the snapshot terra")
        self.assertEqual(data["admission"]["score_source"], "Epoch AI")
        self.assertTrue(data["admission"]["score_credit"].startswith("Epoch AI, 'Capabilities & benchmarking'"))

    def test_a_tampered_stored_copy_counts_as_absent_and_the_snapshot_scores(self):
        shipped_defaults(self)
        project = scored_project(self)
        path = write_stored_epoch(project.state, STORED_RANKING)
        path.write_bytes(path.read_bytes().replace(b'"credit": "Epoch AI', b'"credit": "Someone else'))
        data = project.run(stage="correction", tier="high", axes={"kind": "quick"})
        self.assertEqual((data["resolved_model"], data["admission"]["score_credit"]),
                         ("gpt-6.1-terra", "Epoch AI, a credit line."))
        self.assertTrue(any(warning.startswith("OAH_SCORES_FILE_INVALID:") for warning in data["warnings"]))

    def test_fetch_scores_false_ignores_the_stored_copy(self):
        shipped_defaults(self)
        project = scored_project(self, "[models]\nfetch_scores = false\n")
        write_stored_epoch(project.state, STORED_RANKING)
        data = project.run(stage="correction", tier="high", axes={"kind": "quick"})
        self.assertEqual((data["resolved_model"], data["admission"]["score_credit"]),
                         ("gpt-6.1-terra", "Epoch AI, a credit line."))

    def test_fetch_scores_is_on_by_default_and_takes_only_true_or_false(self):
        self.assertIs(SHIPPED_DEFAULTS["fetch_scores"], True, "the tests run with it off unless they ask")
        shipped_defaults(self)
        self.assertIs(Project(self).settings().models["fetch_scores"], True)
        self.assertIs(Project(self, "[models]\nfetch_scores = false\n").settings().models["fetch_scores"], False)
        with self.assertRaises(ConfigError) as caught:
            Project(self, '[models]\nfetch_scores = "no"\n').settings()
        self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
        self.assertIn("fetch_scores", str(caught.exception))
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(EngineError) as caught:
                call.Settings.validated({}, {}, {"ttl_hours": 24.0, "profile": "balanced", "fetch_scores": value})
            self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
            self.assertIn("models.fetch_scores", str(caught.exception))

    def test_an_unknown_profile_is_refused_like_any_bad_setting(self):
        project = Project(self, '[models]\nprofile = "fast"\n')
        with self.assertRaises(EngineError) as caught:
            project.settings()
        self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
        self.assertIn("models.profile", str(caught.exception))
        self.assertIn("performance, balanced, efficient", str(caught.exception))
        project = Project(self, "[models]\nprofile = 3\n")
        with self.assertRaises(ConfigError) as caught:
            project.settings()
        self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")


FLOOR = "OAH_MODEL_BELOW_FLOOR"
FOUR = ("t1", "t2", "t3", "t4")
OLD_SCORES = dict(SCORES, scores={**SCORES["scores"], "gpt-6-sol": dict.fromkeys(FOUR, 0.9),
                                  "gpt-5.6-luna": dict.fromkeys(FOUR, 0.1)})


def spent(project, task: str = "t1") -> list[dict]:
    """Everything the budget counts: no line means no round, attempt or reservation was spent."""
    return [line for line in receipts.read_round_log(project.state, task) if line["event"] != "limit"]


class AdmissionTest(unittest.TestCase):
    def test_capped_pin_with_highest_floor_needs_no_other_model_or_scores(self):
        for stage in ("review", "correction"):
            with self.subTest(stage=stage):
                extra = ('[engines.codex.tiers.medium]\neffort = "highest"\n'
                         '[engines.codex.tiers.high]\nmodel = "gpt-6.1-sol"\neffort = "highest"\n'
                         '[engines.codex.effort_caps]\n"gpt-6.1-sol" = "high"\n')
                project = scored_project(self, extra)
                cache = json.loads((project.home / "models_cache.json").read_bytes())
                for info in cache["models"]:
                    if info["slug"] == "gpt-6.1-sol":
                        info["supported_reasoning_levels"] = [{"effort": e} for e in ("low", "medium", "high")]
                (project.home / "models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
                with mock.patch.object(scoring, "bundled", side_effect=EngineError(scoring.NO_SCORES, "damaged")):
                    data = project.run(stage=stage, tier="high", axes={"kind": "review"})
                self.assertEqual((data["status"], data["resolved_model"], data["launch_effort"]),
                                 ("finished", "gpt-6.1-sol", "high"))
                self.assertEqual(data["model_judge"], "unused(pinned)")

    def test_none_runnable_refuses_without_spending_in_every_profile(self):
        caps = '[engines.codex.effort_caps]\n' + ''.join(f'"{name}" = "low"\n' for name in SCORED_MODELS)
        for profile in ("performance", "balanced", "efficient"):
            for stage, code in (("review", FLOOR), ("correction", "OAH_MODEL_UNRESOLVED")):
                with self.subTest(profile=profile, stage=stage):
                    project = scored_project(self, f'[models]\nprofile = "{profile}"\n' + caps)
                    message = self.refused(project, code, stage=stage, tier="high", axes={"kind": "security"})
                    self.assertIn("effort_caps", message)

    def refused(self, project: Project, code: str = FLOOR, **fields) -> str:
        fields.setdefault("stage", "review")
        with self.assertRaises(EngineError) as caught:
            project.prepare(**fields)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(project.runs(), [], "a refused call leaves no run")
        self.assertEqual(spent(project), [], "a refused call spends nothing")
        self.assertFalse(project.record.exists())
        return str(caught.exception)

    def test_a_tier_below_the_review_floor_refuses_and_says_what_to_do(self):
        for fields, floor in (({"tier": "low"}, "medium"),
                              ({"tier": "low", "axes": {"kind": "quick"}}, "medium"),
                              ({"tier": "low", "axes": {"kind": "security"}}, "high")):
            with self.subTest(fields=fields):
                message = self.refused(scored_project(self), **fields)
                self.assertIn("tier low", message)
                self.assertIn(floor, message)

    def test_a_planned_tier_below_the_floor_refuses(self):
        project = scored_project(self)
        project.packet("## Scope\n- src/app.py\n## Plan\n- review → codex → low\n")
        self.refused(project)

    def test_low_axes_are_lifted_to_the_floor(self):
        low = {"kind": "quick", "intensity": "low", "reasoning": "low", "risk": "low"}
        for name, fields in (("flags", {"axes": low}), ("packet", {})):
            with self.subTest(name):
                project = scored_project(self)
                if name == "packet":
                    project.packet("## Scope\n- src/app.py\n## Plan\n- review: kind=quick intensity=low reasoning=low "
                                   "risk=low\n")
                data = project.run(stage="review", **fields)
                self.assertEqual((data["status"], data["tier"], data["launch_effort"]), ("finished", "medium", "medium"))
                self.assertEqual(data["resolved_model"], "gpt-6.1-terra")
                self.assertTrue(any("floor medium" in reason for reason in data["tier_reasons"]), data["tier_reasons"])
                self.assertEqual(data["admission"]["floor_tier"], "medium")

    def test_a_risky_path_lifts_the_floor_with_no_flag(self):
        body = "## Scope\n- src/auth/login.py\n"
        project = scored_project(self)
        project.packet(body)
        data = project.run(stage="review")
        self.assertEqual((data["tier"], data["admission"]["floor_tier"]), ("high", "high"))
        project = scored_project(self)
        project.packet(body)
        self.refused(project, tier="medium")

    def test_a_caller_cannot_talk_a_risky_scope_out_of_its_floor(self):
        low = "- review: kind=quick intensity=low reasoning=low risk=low\n"
        project = scored_project(self)
        project.packet(f"## Scope\n- src/auth/login.py\n## Plan\n{low}")
        data = project.run(stage="review")
        self.assertEqual((data["tier"], data["admission"]["floor_tier"]), ("high", "high"))
        project = scored_project(self)
        project.packet("## Scope\n- src/auth/login.py\n")
        data = project.run(stage="review", axes={"kind": "docs", "risk": "low", "reasoning": "low", "intensity": "low"})
        self.assertEqual((data["tier"], data["admission"]["floor_tier"]), ("high", "high"))

    def test_a_kind_raises_the_floor_and_cannot_lower_it(self):
        self.refused(scored_project(self), tier="medium", axes={"kind": "security"})
        data = scored_project(self).run(stage="review", tier="medium", axes={"kind": "quick"})
        self.assertEqual(data["admission"]["floor_tier"], "medium")

    def test_a_low_pin_refuses(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-6-luna"\n')
        self.assertIn("gpt-6-luna", self.refused(project, tier="medium"))

    def test_a_low_pool_refuses(self):
        project = scored_project(self, '[engines.codex.tier_pools]\nmedium = ["gpt-6-luna"]\n')
        self.assertIn("gpt-6-luna", self.refused(project, tier="medium"))

    def test_a_low_quality_order_refuses(self):
        self.refused(scored_project(self, order='["gpt-6-luna"]'), tier="medium")

    def test_the_efficient_profile_is_lifted_to_the_floor(self):
        project = scored_project(self, '[models]\nprofile = "efficient"\n')
        data = project.run(stage="review", tier="medium")
        self.assertEqual((data["tier"], data["resolved_model"]), ("medium", "gpt-6.1-terra"))
        self.assertTrue(data["admission"]["floor_checked"])

    def test_a_profile_never_lowers_a_risky_floor(self):
        for profile in ("efficient", "balanced", "performance"):
            with self.subTest(profile=profile):
                project = scored_project(self, f'[models]\nprofile = "{profile}"\n')
                project.packet("## Scope\n- src/auth/login.py\n")
                data = project.run(stage="review", tier="high")
                self.assertTrue(data["admission"]["floor_checked"])
                self.assertIn(data["resolved_model"], ("gpt-6.1-terra", "gpt-6-astra"))

    def test_an_effort_below_the_floor_refuses(self):
        project = scored_project(self, '[engines.codex.tiers.max]\neffort = "low"\n')
        self.assertIn("effort low", self.refused(project, tier="max"))

    def test_a_model_the_user_named_with_no_score_runs_with_a_warning(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-7-nova"\n',
                                 models=(*SCORED_MODELS, "gpt-7-nova"))
        data = project.run(stage="review", tier="medium")
        self.assertEqual((data["status"], data["resolved_model"]), ("finished", "gpt-7-nova"))
        self.assertIs(data["admission"]["floor_checked"], False)
        self.assertTrue(any(warning.startswith("OAH_MODEL_FLOOR_UNCHECKED:") for warning in data["warnings"]))

    def test_a_scored_review_records_a_checked_floor(self):
        data = scored_project(self).run(stage="review", tier="high")
        self.assertEqual(data["admission"], {
            "family": "terra", "floor_tier": "medium", "floor_checked": True, "profile": "balanced",
            "score_source": "Epoch AI", "score_date": "2026-11-02", "score_credit": "Epoch AI, a credit line."})
        self.assertFalse([warning for warning in data["warnings"] if warning.startswith("OAH_MODEL_FLOOR")])

    def test_a_superseded_model_the_user_named_runs_and_warns(self):
        project = scored_project(self, '[engines.codex.tiers.high]\nmodel = "gpt-6-sol"\n',
                                 models=(*SCORED_MODELS, "gpt-6-sol"), scores=OLD_SCORES)
        data = project.run(stage="review", tier="high")
        self.assertEqual((data["status"], data["resolved_model"], data["model_source"]), ("finished", "gpt-6-sol", "pin"))
        warned = [warning for warning in data["warnings"] if warning.startswith("OAH_MODEL_SUPERSEDED:")]
        self.assertEqual(len(warned), 1, data["warnings"])
        self.assertIn("gpt-6-sol", warned[0])
        self.assertIn("gpt-6.1-sol", warned[0])
        self.assertTrue(data["admission"]["floor_checked"], "its score on the same scale still answers the floor")

    def test_a_superseded_pin_is_no_way_under_the_floor(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-5.6-luna"\n',
                                 models=(*SCORED_MODELS, "gpt-5.6-luna"), scores=OLD_SCORES)
        self.refused(project, tier="medium")

    def test_a_hidden_superseded_pin_is_still_measured(self):
        for model, refuses in (("gpt-5.6-luna", True), ("gpt-6-sol", False)):
            with self.subTest(model=model):
                project = scored_project(self, f'[engines.codex.tiers.medium]\nmodel = "{model}"\n',
                                         hidden=(model,), scores=OLD_SCORES)
                if refuses:
                    self.refused(project, tier="medium")
                else:
                    self.assertTrue(project.run(tier="medium")["admission"]["floor_checked"])

    def test_a_superseded_pin_is_still_measured_without_a_catalog(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-5.6-luna"\n', scores=OLD_SCORES)
        (project.home / "models_cache.json").unlink()
        self.refused(project, tier="medium")

    def test_a_full_claude_generation_pin_with_data_is_still_measured(self):
        scores = {**SCORES, "scores": {**SCORES["scores"],
                  "claude-haiku-4-5": dict.fromkeys(FOUR, 0.1),
                  "claude-haiku-5-5": dict.fromkeys(FOUR, 0.2),
                  "claude-sonnet-5-5": dict.fromkeys(FOUR, 0.8),
                  "claude-opus-5-5": dict.fromkeys(FOUR, 1.0)}}
        project = scored_project(self, '[engines.claude.tiers.medium]\nmodel = "claude-haiku-4-5"\n', scores=scores)
        self.refused(project, tier="medium", engine="claude", override_stage=True)

    def test_a_pin_outside_allowed_models_is_no_way_under_the_floor(self):
        project = scored_project(self, 'allowed_models = ["gpt-6.1-terra", "gpt-6-astra"]\n'
                                       '[engines.codex.tiers.medium]\nmodel = "gpt-6-luna"\n')
        self.refused(project, tier="medium")

    def test_a_narrow_allow_list_cannot_let_a_low_model_pass_a_review(self):
        project = scored_project(self, 'allowed_models = ["gpt-6-luna"]\n')
        message = self.refused(project, tier="medium")
        self.assertIn("allowed_models", message)
        self.assertIn("medium", message)

    def test_a_pin_stays_measured_when_the_allow_list_leaves_nothing_to_pick(self):
        project = scored_project(self, 'allowed_models = ["gpt-7-nova"]\n'
                                       '[engines.codex.tiers.medium]\nmodel = "gpt-6-luna"\n')
        self.assertIn("gpt-6-luna", self.refused(project, tier="medium"))

    def test_a_pin_stays_measured_with_no_catalog(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-6-luna"\n')
        (project.home / "models_cache.json").unlink()
        self.assertIn("gpt-6-luna", self.refused(project, tier="medium"))

    def test_a_floor_no_allowed_model_reaches_is_unresolved_outside_a_review(self):
        project = scored_project(self, 'allowed_models = ["gpt-6-luna"]\n')
        message = self.refused(project, "OAH_MODEL_UNRESOLVED", stage="correction", tier="high", axes={"kind": "security"})
        self.assertIn("allowed_models", message)

    def test_a_narrow_allow_list_still_runs_where_no_floor_stands(self):
        project = scored_project(self, 'allowed_models = ["gpt-6.1-terra"]\n')
        data = project.run(stage="correction", tier="max", axes={"kind": "quick"})
        self.assertEqual((data["status"], data["resolved_model"], data["model_source"]),
                         ("finished", "gpt-6.1-terra", "scored"))

    def test_a_model_this_machine_does_not_list_still_sets_the_review_floor(self):
        # Without astra and sol on the list, terra would have topped the scale, and luna reached a medium floor with it.
        project = scored_project(self, models=("gpt-6-luna",))
        self.assertIn("allowed_models", self.refused(project, tier="medium"))

    def test_floors_only_bind_reviews(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-6-luna"\n')
        data = project.run(stage="correction", tier="medium")
        self.assertEqual((data["status"], data["resolved_model"]), ("finished", "gpt-6-luna"))
        self.assertIs(data["admission"]["floor_checked"], False)
        self.assertFalse([warning for warning in data["warnings"] if warning.startswith("OAH_MODEL_FLOOR")])

    def test_a_pin_a_fresh_codex_catalog_no_longer_lists_refuses(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-6-gone"\n')
        message = self.refused(project, "OAH_MODEL_NOT_LISTED", stage="correction", tier="medium")
        self.assertIn("gpt-6-gone", message)
        self.assertIn("oah models", message)

    def test_a_refusal_never_echoes_a_pin_that_is_not_a_plain_name(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "bad\\u001b[2Jname"\n')
        message = self.refused(project, "OAH_MODEL_NOT_LISTED", stage="correction", tier="medium")
        self.assertNotIn("bad", message)
        self.assertNotIn("\x1b", message)

    def test_a_hidden_pin_is_still_known_to_the_cli_so_it_keeps_working(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-reserve"\n', hidden=("gpt-reserve",))
        self.assertEqual(project.run(stage="correction", tier="medium")["resolved_model"], "gpt-reserve")

    def test_a_stale_catalog_cannot_judge_a_pin(self):
        project = scored_project(self, '[engines.codex.tiers.medium]\nmodel = "gpt-6-gone"\n'
                                       '[engines.codex.tiers.low]\nmodel = "gpt-6-luna"\n')
        project.prepare(stage="correction", tier="low", axes={"kind": "quick"})
        (project.home / "models_cache.json").unlink()
        data = project.run(stage="correction", tier="medium", env={"FAKE_CODEX_VERSION": "0.164.0"})
        self.assertEqual((data["status"], data["resolved_model"]), ("finished", "gpt-6-gone"))
        self.assertTrue(any(warning.startswith("OAH_MODELS_REFRESH_FAILED") for warning in data["warnings"]))

    def test_claude_pins_are_not_checked_against_the_list(self):
        project = Project(self, '[engines.claude.tiers.high]\nmodel = "claude-opus-5-5"\n')
        data = project.run(engine="claude", stage="initial", tier="high")
        self.assertEqual((data["status"], data["resolved_model"]), ("finished", "claude-opus-5-5"))

    def test_no_catalog_refuses_with_the_one_line_pin_and_a_pin_still_runs(self):
        project = scored_project(self)
        (project.home / "models_cache.json").unlink()
        message = self.refused(project, "OAH_MODEL_CATALOG_UNAVAILABLE", stage="correction", tier="high")
        self.assertIn('[engines.codex.tiers] high = { model = "<model>" }', message)
        project = scored_project(self, '[engines.codex.tiers.high]\nmodel = "gpt-6.1-sol"\n')
        (project.home / "models_cache.json").unlink()
        self.assertEqual(project.run(stage="correction", tier="high")["status"], "finished")

    def test_a_failed_refresh_with_a_last_good_entry_warns_as_before(self):
        project = scored_project(self)
        project.prepare(stage="correction", tier="high")
        (project.home / "models_cache.json").unlink()
        data = project.run(stage="correction", tier="high", env={"FAKE_CODEX_VERSION": "0.164.0"})
        self.assertEqual((data["status"], data["resolved_model"]), ("finished", "gpt-6.1-terra"))
        self.assertTrue(any(warning.startswith("OAH_MODELS_REFRESH_FAILED") for warning in data["warnings"]))

    def test_no_usable_scores_refuse_by_name_and_nothing_runs(self):
        for damage in ({"side_effect": FileNotFoundError("gone")}, {"return_value": b"not json"}):
            with self.subTest(damage=damage), mock.patch.object(scoring, "_packaged", **damage):
                project = Project(self, "quality_order = []\n")
                write_models(project.home)
                self.assertIn(".oah/config.toml", self.refused(project, "OAH_MODEL_NO_SCORES", stage="correction",
                                                               tier="high"))

    def test_damaged_numbers_refuse_automatic_calls_but_do_not_block_pins(self):
        packaged = scoring._packaged
        for name in ("benchmarks.json", "families.json"):
            for value in (10 ** 400, True, float("inf")):
                with self.subTest(name=name, value=value):
                    damaged = json.loads(packaged(name))
                    if name == "benchmarks.json":
                        damaged["scores"]["gpt-6.1-sol"] = {"task-a": value}
                    else:
                        damaged["engines"]["codex"]["families"]["sol"][0] = value
                    raw = json.dumps(damaged).encode()
                    with mock.patch.object(scoring, "_packaged", side_effect=lambda path: raw if path == name
                                           else packaged(path)):
                        project = Project(self, "quality_order = []\n")
                        write_models(project.home)
                        self.refused(project, "OAH_MODEL_NO_SCORES", tier="medium")
                        project = Project(self, 'quality_order = []\n[engines.codex.tiers.medium]\n'
                                                'model = "gpt-6.1-sol"\n')
                        write_models(project.home)
                        result = project.run(tier="medium")
                        self.assertEqual(result["status"], "finished")
                        self.assertFalse(result["admission"]["floor_checked"])

    def test_a_catalog_of_unknown_families_refuses_and_never_guesses(self):
        project = scored_project(self, models=("gpt-7-nova", "codex-auto-review"))
        self.refused(project, "OAH_MODEL_NO_SCORES", stage="correction", tier="high")

    def test_a_pin_pool_or_list_still_runs_with_no_usable_scores(self):
        rows = (("pin", 'quality_order = []\n[engines.codex.tiers.high]\nmodel = "gpt-6.1-sol"\n'),
                ("pool", 'quality_order = []\n[engines.codex.tier_pools]\nhigh = ["gpt-6.1-sol"]\n'),
                ("quality_order", 'quality_order = ["gpt-6.1-sol"]\n'))
        for source, config in rows:
            with self.subTest(source=source), mock.patch.object(scoring, "_packaged", side_effect=OSError("gone")):
                project = Project(self, config)
                write_models(project.home)
                data = project.run(stage="review", tier="high")
                self.assertEqual((data["status"], data["model_source"]), ("finished", source))
                self.assertIs(data["admission"]["floor_checked"], False)
                self.assertIsNone(data["admission"]["score_source"])

    def test_a_refused_call_can_be_followed_by_a_good_one(self):
        project = scored_project(self)
        self.refused(project, tier="low")
        self.assertEqual(project.run(stage="review", tier="medium")["status"], "finished")
        self.assertEqual([line["event"] for line in spent(project)], ["reservation", "dispatch"])


class AdmissionPathTest(unittest.TestCase):
    """`admit` runs on the final pair of every launch path, and one launch reads the data once."""

    def test_expected_validation_errors_at_the_read_become_named_score_failures(self):
        settings = Project(self).settings()
        for reader in ("families", "bundled", "score_set"):
            for error in (ValueError, TypeError, OverflowError, RecursionError):
                with self.subTest(reader=reader, error=error), mock.patch.object(scoring, reader, side_effect=error):
                    scores, previous = call.read_scores("codex", None, settings)
                    self.assertIsNone(previous)
                    with self.assertRaises(EngineError) as caught:
                        scoring.choose(scores, "medium", "low", "balanced")
                    self.assertEqual(caught.exception.code, "OAH_MODEL_NO_SCORES")
                    self.assertIn(".oah/config.toml", str(caught.exception))

    def test_foreground_and_background_calls(self):
        for extra in ((), ("--background",)):
            with self.subTest(extra=extra):
                project = scored_project(self)
                argv = ("call", "codex", "--stage", "review", "--task", "t1", "--tier", "medium", *extra)
                with mock.patch.object(call, "admit", wraps=call.admit) as spy:
                    code, out, err = run_cli(project, *argv)
                self.assertEqual(code, 0, err)
                self.assertEqual(spy.call_count, 2)
                self.assertEqual(spy.call_args.args[0].model, "gpt-6.1-terra")

    def test_each_prepared_call_passes_it_before_and_after_the_judge(self):
        project = scored_project(self)
        with mock.patch.object(call, "admit", wraps=call.admit) as spy:
            project.prepare(stage="review", tier="medium")
            project.prepare(stage="correction", tier="medium")
        self.assertEqual(spy.call_count, 4)

    def test_admit_sees_the_judged_pair_not_the_rule_choice(self):
        project = scored_project(self, '[rerank]\nenabled = true\nprovider = "fake"\n')
        project.env["FAKE_JUDGE_KEY"] = "fake-key"

        class Judge:
            name = "fake"

            def key_name(self, config):
                return "FAKE_JUDGE_KEY"

            def choose_model(self, request, candidates, *, key, config):
                return ("gpt-6.1-sol", "high")

        with contextlib.chdir(project.repo), mock.patch.dict(rerank.PROVIDERS, {"fake": Judge()}), \
                mock.patch.object(call, "admit", wraps=call.admit) as spy:
            data = project.run(stage="review", tier="high")
        self.assertEqual(data["model_judge"], "used")
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(spy.call_args_list[0].args[0].model, "gpt-6.1-terra")
        self.assertEqual((spy.call_args.args[0].model, spy.call_args.args[0].model_source), ("gpt-6.1-sol", "judge"))
        self.assertEqual(data["admission"]["family"], "sol")

    def test_refusal_precedes_the_judge_and_reservation(self):
        project = scored_project(self)
        with mock.patch.object(call, "judge_engine") as judge, self.assertRaises(EngineError) as caught:
            project.prepare(tier="low")
        self.assertEqual(caught.exception.code, FLOOR)
        judge.assert_not_called()
        self.assertEqual(spent(project), [])

    def test_an_interrupted_judge_keeps_its_reservation_on_retry(self):
        project = scored_project(self, '[rerank]\nenabled = true\nprovider = "fake"\n'
                                       '[review]\nmax_rounds = 1\n')
        project.env["FAKE_JUDGE_KEY"] = "fake-key"

        class Judge:
            name = "fake"

            def key_name(self, config):
                return "FAKE_JUDGE_KEY"

            def choose_model(self, request, candidates, *, key, config):
                raise KeyboardInterrupt

        with contextlib.chdir(project.repo), mock.patch.dict(rerank.PROVIDERS, {"fake": Judge()}):
            with self.assertRaises(KeyboardInterrupt):
                project.prepare(tier="medium")
        self.assertEqual([line["event"] for line in spent(project)], ["reservation"])
        with mock.patch.object(call, "judge_engine") as judge, self.assertRaises(EngineError) as caught:
            project.prepare(tier="medium")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_ROUNDS_EXHAUSTED")
        judge.assert_not_called()
        self.assertEqual([line["event"] for line in spent(project)], ["reservation"])

    def test_an_unexpected_final_refusal_keeps_the_judges_spend(self):
        project = scored_project(self, '[review]\nmax_rounds = 1\n')
        admitted = call.admit
        checks = []

        def check(*args, **kwargs):
            checks.append(True)
            if len(checks) == 2:
                raise EngineError(FLOOR, "unexpected final refusal")
            return admitted(*args, **kwargs)

        with mock.patch.object(call, "admit", side_effect=check), self.assertRaises(EngineError) as caught:
            project.prepare(tier="medium")
        self.assertEqual(caught.exception.code, FLOOR)
        self.assertEqual([line["event"] for line in spent(project)], ["reservation"])

    def test_a_refused_foreground_or_background_call_spends_nothing(self):
        for extra in ((), ("--background",)):
            with self.subTest(extra=extra):
                project = scored_project(self)
                code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low",
                                         *extra)
                self.assertEqual(code, 1, err)
                self.assertIn(FLOOR, err)
                self.assertEqual(spent(project), [])
                self.assertEqual(project.runs(), [])

    def test_a_refused_call_at_the_round_limit_is_named_as_the_refusal(self):
        project = scored_project(self, "[review]\nmax_rounds = 1\n")
        for _ in range(3):
            code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low")
            self.assertEqual(code, 1, err)
            self.assertIn(FLOOR, err)
        self.assertEqual(project.run(stage="review", tier="medium")["status"], "finished")

    def test_one_launch_reads_the_data_and_the_catalog_once(self):
        project = Project(self, "quality_order = []\n")
        write_models(project.home)
        project.prepare(stage="correction", tier="high", axes={"kind": "quick"})  # the first read builds the catalog
        reads = collections.Counter()
        real = scoring._packaged

        def spy(name):
            reads[name] += 1
            return real(name)

        load = models.Catalog.load
        with mock.patch.object(scoring, "_packaged", side_effect=spy), \
                mock.patch.object(models.Catalog, "load", autospec=True, side_effect=load) as catalog:
            project.prepare(stage="correction", tier="high", axes={"kind": "quick"})
        self.assertEqual(reads, {"families.json": 1, "benchmarks.json": 1})
        self.assertEqual(catalog.call_count, 1)

    def test_old_run_words_stay_readable_and_new_words_are_closed(self):
        for word in ("cli_default", "effective_config", "quality_order(unconfirmed)", "pool", "scored", "quality_order"):
            with self.subTest(word=word):
                self.assertEqual(runs._correction_record({"model_source": word}, ("model_source",))["model_source"], word)
        for word in ("design", "packet", "performance", "balanced", "efficient", "rejected(outside-score-bound)",
                     "outside-score-bound"):
            with self.subTest(word=word):
                self.assertEqual(runs._correction_record({"advisory_word": word}, ("advisory_word",))["advisory_word"], word)

    def test_an_old_run_record_still_reads_and_waits(self):
        project = Project(self)
        data = project.run(tier="medium")
        old = dict(data, model_source="quality_order(unconfirmed)")
        old.pop("admission")
        (project.state / "runs" / "t1" / data["run_id"] / "result.json").write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(call.wait(project.state, data["run_id"], timeout=0)["model_source"], "quality_order(unconfirmed)")


class RunFolderTest(unittest.TestCase):
    def test_packet_fixture_helpers_keep_exact_line_endings(self):
        project = Project(self)
        project.bind()
        self.assertEqual((project.repo / ".oah/tasks/t1/PACKET.md").read_bytes(), SECTION.encode("utf-8"))
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                path = project.packet(newline=newline)
                self.assertEqual(path.read_bytes(), SECTION.replace("\n", newline).encode("utf-8"))

    def test_legacy_spec_lease_commands_warn_in_every_stage(self):
        project = Project(self)
        spec = project.repo / "legacy-spec.md"
        for stage, engine in (("initial", "claude"), ("correction", "codex"), ("review", "codex")):
            for command in ("start", "renew", "finish"):
                with self.subTest(stage=stage, command=command):
                    spec.write_bytes((f"Before proceeding, run `oah {command} --task t1`.\n" + SECTION).encode("utf-8"))
                    code, _, err = run_cli(project, "call", engine, "--stage", stage, "--task", "t1",
                                           "--tier", "medium", "--brief-file", str(spec))
                    self.assertEqual(code, 0, err)
                    self.assertIn("OAH_WORKER_LEASE_COMMAND", err)
                    self.assertIn("remove", err)
                    self.assertIn(f"oah {command}", project.recorded()["stdin"])

    def test_windows_spec_lease_commands_warn_in_every_stage(self):
        project = Project(self)
        spec = project.repo / "windows-spec.md"
        for stage, engine in (("initial", "claude"), ("correction", "codex"), ("review", "codex")):
            for executable in ("oah.exe", "OAH.EXE", "OaH.ExE"):
                for command in ("start", "renew", "finish"):
                    with self.subTest(stage=stage, executable=executable, command=command):
                        instruction = f"Run `{executable} {command} --task t1`.\n"
                        spec.write_bytes((instruction + SECTION).encode("utf-8"))
                        code, _, err = run_cli(project, "call", engine, "--stage", stage, "--task", "t1",
                                               "--tier", "medium", "--brief-file", str(spec))
                        self.assertEqual(code, 0, err)
                        self.assertIn("OAH_WORKER_LEASE_COMMAND", err)
                        self.assertIn(instruction, project.recorded()["stdin"])

    def test_stored_spec_gets_the_current_worker_envelope(self):
        project = Project(self)
        data = project.run(tier="medium", stored_brief="Review this reusable spec.\n" + SECTION)
        prompt = project.recorded()["stdin"]
        self.assertTrue(prompt.startswith("You are a dispatched oah worker (OAH_WORKER=1, run " + data["run_id"]))
        self.assertIn("Stage: review · Engine: codex", prompt)
        self.assertIn("## Round instructions\nReview this reusable spec.", prompt)
        self.assertEqual(prompt.count(VERIFY_MARKER), 1)
        self.assertTrue(prompt.endswith(VERIFY_FOOTER + "\n" + FOOTER))
        self.assertNotIn("oah start", prompt)

    def test_reused_implementation_brief_refreshes_review_envelope(self):
        project = Project(self)
        project.packet("## Scope\n- src/a.py\n")
        initial = project.prepare(engine="claude", stage="initial", tier="medium", round_instructions="Inspect AC1.")
        stored = initial.run.prompt_path.read_bytes().decode("utf-8")
        data = project.run(tier="medium", stored_brief=stored, round_instructions="Review the current diff.")
        prompt = project.recorded()["stdin"]
        self.assertTrue(prompt.startswith("You are a dispatched oah worker (OAH_WORKER=1, run " + data["run_id"]))
        self.assertNotIn(initial.run_id, prompt)
        self.assertNotIn("Stage: initial", prompt)
        self.assertNotIn("Agent-Engine", prompt)
        self.assertNotIn("oah start", prompt)
        self.assertEqual(prompt.count(SECTION), 1)
        self.assertIn("Inspect AC1.", prompt)
        self.assertIn("Review the current diff.", prompt)
        self.assertEqual(prompt.count(VERIFY_MARKER), 1)

    def test_stored_review_spec_with_lease_instruction_warns(self):
        project = Project(self)
        prepared = project.prepare(tier="medium", stored_brief="Run oah start --task old --agent claude before reviewing.\n"
                                   + SECTION)
        self.assertTrue(any(warning.startswith("OAH_WORKER_LEASE_COMMAND") for warning in prepared.warnings))
        self.assertFalse(project.record.exists())

    def test_refresh_failure_keeps_launch_version_separate_from_catalog(self):
        project = Project(self)
        project.prepare(tier="medium")
        (project.home / "models_cache.json").unlink()
        measured_old_rule = [{"id": "old-version-only", "engine": "codex", "cli_versions": ["0.163."],
                              "models": ["gpt-test-a"], "map": {"low": "low"}}]
        for version, expected in (("0.170.0", "0.170.0"), ("unknown", None)):
            with self.subTest(version=version):
                prepared = project.prepare(tier="medium", env={"FAKE_CODEX_VERSION": version})
                self.assertEqual(prepared.cli_version, expected)
                data = call.execute(prepared, base_env=project.env, rules=measured_old_rule)
                self.assertEqual(data["cli_version"], expected or UNVERIFIED)
                self.assertEqual(data["resolved_model"], "gpt-test-a")
                self.assertFalse(data["effort_verified"])
                self.assertEqual(data["resolved_inference_effort"], UNVERIFIED)
        self.assertEqual(call.models.Catalog(project.state).load("codex").cli_version, "0.163.0")

    def test_call_writes_complete_run_folder(self):
        project = Project(self)
        data = project.run(tier="high", round_instructions="Review the change.")
        run = find_run(project.state, data["run_id"])
        self.assertEqual(sorted(p.name for p in run.path.iterdir()),
                         ["last-message.md", "lease.json", "prompt.md", "result.json", "skills.json", "stderr.log",
                          "stdout.log"])
        self.assertEqual(set(data), set(RESULT_FIELDS))
        expected = {"status": "finished", "exit_code": 0, "timed_out": False, "engine": "codex", "stage": "review",
                    "cli_version": "0.163.0", "resolved_model": "gpt-test-a", "model_source": "quality_order",
                    "requested_effort": "high", "launch_effort": "high", "header_effort": "high",
                    "header_check": "match", "resolved_inference_effort": UNVERIFIED,
                    "normalization_basis": "unverified: no normalization rule for codex 0.163.0 gpt-test-a effort high",
                    "effort_verified": False, "tier": "high",
                    "tier_source": "flag", "read_only": True, "error": None, "advisory": "unused(tier-given)"}
        self.assertEqual({key: data[key] for key in expected}, expected)
        self.assertIsInstance(data["duration_s"], float)
        self.assertIn("read-only", data["command"])
        self.assertNotIn("workspace-write", data["command"])
        self.assertIn("fake codex done", run.stdout_path.read_text(encoding="utf-8"))
        self.assertIn("reasoning effort: high", run.stderr_path.read_text(encoding="utf-8"))

    def test_worker_environment(self):
        project = Project(self)
        data = project.run(tier="medium", round_instructions="Do it.")
        record = project.recorded()
        self.assertEqual({key: record["env"].get(key) for key in call.WORKER_ENV},
                         {"OAH_WORKER": "1", "OAH_RUN_ID": data["run_id"], "OAH_TASK": "t1"})
        prompt = find_run(project.state, data["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertEqual(record["stdin"], prompt)
        self.assertTrue(prompt.startswith("You are a dispatched oah worker (OAH_WORKER=1, run " + data["run_id"]))
        self.assertTrue(prompt.endswith(FOOTER))
        self.assertEqual(pathlib.Path(record["cwd"]).resolve(), project.repo.resolve())

    def test_highest_resolves_from_supported_levels(self):
        data = Project(self).run(tier="max")
        self.assertEqual((data["requested_effort"], data["launch_effort"], data["header_check"]),
                         ("highest", "max", "match"))

    def test_correction_is_writable(self):
        data = Project(self).run(stage="correction", tier="medium")
        self.assertIn("workspace-write", data["command"])
        self.assertFalse(data["read_only"])

    def test_stored_brief_delivery(self):
        project = Project(self)
        data = project.run(tier="medium", stored_brief="An old spec that mentions " + MARKER + " inline.\n" + SECTION)
        prompt = find_run(project.state, data["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertIn("## Round instructions\nAn old spec", prompt)
        self.assertTrue(prompt.endswith(FOOTER))


class RoutingTest(unittest.TestCase):
    def test_settings_defaults(self):
        shipped_defaults(self)
        repo = make_git_repo(self)
        settings = call_cli.load_settings(repo)
        self.assertEqual(settings.stages, {"initial": "claude", "review": "codex", "correction": "codex"})
        claude, codex = settings.engines["claude"], settings.engines["codex"]
        self.assertEqual((claude["command"], claude["quality_order"], claude["permission_mode"]),
                         (["claude"], [], "acceptEdits"))
        self.assertEqual((codex["command"], codex["quality_order"]), (["codex"], []))
        self.assertEqual({tier: (row["model"], row["effort"], row["timeout_min"]) for tier, row in claude["tiers"].items()},
                         {"low": ("", "low", 15), "medium": ("", "medium", 30), "high": ("", "high", 60),
                          "max": ("", "max", 90)})
        self.assertEqual({tier: row["effort"] for tier, row in codex["tiers"].items()},
                         {"low": "low", "medium": "medium", "high": "high", "max": "highest"})
        self.assertEqual(settings.models, {"ttl_hours": 24.0, "profile": "balanced", "fetch_scores": True})
        self.assertEqual(settings, call.Settings.defaults())

    def test_settings_from_config_and_bad_values(self):
        project = Project(self, '[stages]\ninitial = "codex"\n[engines.codex.tiers.high]\nmodel = "gpt-pin"\n')
        settings = project.settings()
        self.assertEqual(settings.stages["initial"], "codex")
        self.assertEqual(settings.engines["codex"]["tiers"]["high"], {"model": "gpt-pin", "effort": "high",
                                                                      "timeout_min": 60})
        bad = Project(self, '[stages]\nreview = "gemini"\n')
        with self.assertRaises(EngineError) as ctx:
            bad.settings()
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")

    def test_single_engine_setup_warns(self):
        project = Project(self, '[stages]\ninitial = "codex"\nreview = "codex"\ncorrection = "codex"\n')
        data = project.run(stage="review", tier="medium")
        self.assertTrue(any(w.startswith("OAH_REVIEW_NOT_INDEPENDENT") for w in data["warnings"]))
        default = Project(self).run(stage="review", tier="medium")
        self.assertFalse(any(w.startswith("OAH_REVIEW_NOT_INDEPENDENT") for w in default["warnings"]))

    def test_stage_mismatch_refuses(self):
        project = Project(self)
        with self.assertRaises(RoutingError) as ctx:
            project.prepare(engine="claude", stage="review", tier="medium")
        self.assertEqual(ctx.exception.code, "OAH_STAGE_ENGINE_MISMATCH")
        self.assertFalse((project.state / "runs").exists())

    def test_override_stage_is_recorded(self):
        project = Project(self)
        data = project.run(engine="claude", stage="review", tier="high", override_stage=True)
        self.assertTrue(data["override_stage"])
        self.assertTrue(any(w.startswith("OAH_STAGE_OVERRIDDEN") for w in data["warnings"]))
        events = [json.loads(line) for line in (project.state / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["event"], "note")
        self.assertIn(f"stage override: run {data['run_id']} runs claude for stage review", events[-1]["message"])

    def test_review_is_read_only_claude(self):
        project = Project(self)
        data = project.run(engine="claude", stage="review", tier="high", override_stage=True)
        argv = project.recorded()["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertTrue(data["read_only"])

    def test_claude_initial_records_unobservable_effort(self):
        project = Project(self)
        data = project.run(engine="claude", stage="initial", tier="high")
        argv = project.recorded()["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual((data["resolved_model"], data["model_source"], data["launch_effort"]),
                         ("opus", "quality_order", "high"))
        self.assertEqual((data["effort_observable"], data["header_check"], data["resolved_inference_effort"],
                          data["effort_verified"]), (False, UNVERIFIED, UNVERIFIED, False))
        self.assertEqual(data["observed_models"], ["claude-opus-fake"])
        self.assertIn("does not report", data["normalization_basis"])

    def test_claude_reported_effort_is_observed(self):
        project = Project(self)
        env = {"FAKE_CLAUDE_REPORT_EFFORT": "high"}
        data = project.run(engine="claude", stage="initial", tier="high", env=env)
        self.assertEqual((data["effort_observable"], data["header_check"], data["resolved_inference_effort"]),
                         (True, "match", "high"))

    def test_tier_sources_rules_packet_flag(self):
        project = Project(self)
        rules = project.run(stage="review")
        self.assertEqual((rules["tier_source"], rules["tier"]), ("rules", "medium"))
        self.assertEqual(rules["axes"], {"kind": "review", "intensity": "low", "reasoning": "medium", "risk": "normal"})
        self.assertIn("Scope: .oah/tasks/t1", project.recorded()["stdin"])
        self.assertEqual(rules["advisory"], "unused(not-configured)")
        flagged = project.run(stage="review", axes={"risk": "critical"})
        self.assertEqual((flagged["tier_source"], flagged["tier"]), ("flag", "max"))
        project.packet("## Scope\n- src/a.py\n## Plan\n- review → codex → high\n")
        packet = project.run(stage="review")
        self.assertEqual((packet["tier_source"], packet["tier"], packet["axes"]), ("packet", "high", None))
        prompt = find_run(project.state, packet["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertIn(SECTION, prompt)
        self.assertIn("Scope: .oah/tasks/t1", prompt)  # the lease still limits this worker's writes
        self.assertNotIn("oah start", prompt)
        correction = project.run(stage="correction")
        prompt = find_run(project.state, correction["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertEqual(correction["tier_source"], "rules")
        self.assertNotIn("oah start", prompt)
        self.assertIn("lease belongs to the dispatcher", prompt)
        self.assertIn("Binding: INTENT t1@r2 sha256:", prompt)

    def test_rules_use_lease_scope_and_risk_paths(self):
        project = Project(self)
        Store(project.state).start(task="t1", agent="codex", scope=[".github/workflows/ci.yml"], note="",
                                   ttl_hours=1, info=repo_info(project.repo))
        data = project.run(stage="correction")
        self.assertEqual(data["axes"]["risk"], "high")
        self.assertEqual(data["tier"], "high")
        self.assertTrue(any("workflow" in reason for reason in data["tier_reasons"]))

    def test_advice_never_changes_tier(self):
        project = Project(self)
        for status, tier in (("used", "max"), ("used", "low")):
            advice = types.SimpleNamespace(status=status, advisory_tier=tier)
            module = types.SimpleNamespace(advise_difficulty=lambda axes, task, *, root, advice=advice, **kw: advice)
            with mock.patch.dict(sys.modules, {"oah.rerank": module}):
                data = project.run(stage="review")
            self.assertEqual((data["tier"], data["advisory"]), ("medium", f"used tier={tier}"))

        def broken(axes, task, *, root, **kw):
            raise RuntimeError("provider down")

        with mock.patch.dict(sys.modules, {"oah.rerank": types.SimpleNamespace(advise_difficulty=broken)}):
            data = project.run(stage="review")
        self.assertEqual((data["tier"], data["advisory"]), ("medium", "unused(error: RuntimeError)"))
        no_key = types.SimpleNamespace(
            advise_difficulty=lambda axes, task, *, root, **kw: types.SimpleNamespace(status="unused(no key)"))
        with mock.patch.dict(sys.modules, {"oah.rerank": no_key}):
            self.assertEqual(project.run(stage="review")["advisory"], "unused(no key)")

    def test_real_advice_records_provider_tier_without_changing_launch_tier(self):
        project = Project(self, '[rerank]\nenabled = true\nprovider = "fake"\n')
        provider = types.SimpleNamespace(name="fake", key_name=lambda config: "FAKE_RERANK_KEY",
                                         advise=mock.Mock())
        project.env["FAKE_RERANK_KEY"] = "fake-" + token_source.token_hex(16)
        with contextlib.chdir(project.repo), \
                mock.patch.dict(rerank.PROVIDERS, {"fake": provider}):
            for tier in ("max", "low"):
                with self.subTest(advisory_tier=tier):
                    provider.advise.reset_mock()
                    provider.advise.return_value = (tier, 0.8)
                    data = project.run(stage="review")
                    self.assertEqual((data["tier"], data["tier_source"], data["advisory"]),
                                     ("medium", "rules", f"used tier={tier}"))
                    self.assertEqual(data["launch_effort"], "medium")
                    provider.advise.assert_called_once()

    def test_fresh_install_refuses_before_launch(self):
        project = Project(self)
        (project.home / "models_cache.json").unlink()
        with self.assertRaises(EngineError) as ctx:
            project.prepare(tier="high")
        self.assertEqual(ctx.exception.code, "OAH_MODEL_CATALOG_UNAVAILABLE")
        self.assertFalse((project.state / "runs").exists())
        self.assertFalse(project.record.exists())

    def test_missing_footer_refuses_dispatch(self):
        project = Project(self)
        request = call.CallRequest(engine="codex", stage="review", task="t1", tier="medium")
        with self.assertRaises(BriefError) as ctx:
            call.prepare(request, project.settings(), root=project.repo, state_dir=project.state, env=project.env,
                         footer="", has_footer=has_footer)
        self.assertEqual(ctx.exception.code, "OAH_FOOTER_NOT_APPENDED")
        self.assertFalse((project.state / "runs").exists())


NON_ASCII = SECTION.replace("Reports miss files.", "한글 목표 — 이모지 \U0001f600 그대로.")


class IntentLineageTest(unittest.TestCase):
    def test_unsafe_packet_can_be_renamed_and_retried_for_the_same_task(self):
        project = Project(self)
        source = project.packet(name="my packets/PACKET.md")
        with self.assertRaises(BriefError) as ctx:
            project.prepare(stage="correction", tier="medium", packet=source)
        self.assertEqual(ctx.exception.code, "OAH_UNSAFE_ARGUMENT")
        self.assertIsNone(call.dispatch_binding(project.state, "t1"))
        self.assertFalse(project.record.exists())

        renamed = source.parent.with_name("my-packets")
        source.parent.rename(renamed)
        path = renamed / source.name
        path.write_text(path.read_text(encoding="utf-8").replace("Revision: 2", "Revision: 3"), encoding="utf-8", newline="")
        Store(project.state).rebind(task="t1", agent="codex", ttl_hours=1,
                                    binding=intent.parse(intent.read_source(path)).bind("my-packets/PACKET.md"))
        data = project.run(stage="correction", tier="medium", packet=renamed / source.name)
        self.assertEqual(data["status"], "finished")
        self.assertEqual(call.dispatch_binding(project.state, "t1")["source"], "my-packets/PACKET.md")
        self.assertIn("Packet: my-packets/PACKET.md", project.recorded()["stdin"])

    def test_stored_brief_delivers_source_intent_bytes_on_stdin(self):
        project = Project(self)
        source = project.packet("## Scope\n- src/a.py\n", section=NON_ASCII, newline="\r\n")
        section = raw_section(source.read_bytes().decode("utf-8"))
        stored = "Reusable spec\r\n" + section + FOOTER.replace("\n", "\r\n")
        data = project.run(tier="medium", stored_brief=stored)
        delivered = project.recorded()["stdin"].encode("utf-8")
        self.assertEqual(raw_section(delivered.decode("utf-8")).encode("utf-8"), section.encode("utf-8"))
        self.assertEqual(find_run(project.state, data["run_id"]).prompt_path.read_bytes(), delivered)

    def test_stored_brief_with_changed_intent_line_endings_refuses(self):
        project = Project(self)
        project.packet(newline="\r\n")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(tier="medium", stored_brief=SECTION)
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")

    def test_linked_task_directory_cannot_redirect_intent_binding(self):
        project = Project(self)
        project.packet()
        outside = make_tmp_dir(self)
        (project.state / "runs").mkdir(parents=True)
        directory_alias(outside, project.state / "runs" / "t1")
        with self.assertRaises(call.runs.RunError):
            project.prepare(tier="medium")
        self.assertEqual(list(outside.iterdir()), [])

    def test_section_stays_byte_intact_with_non_ascii_and_crlf(self):
        project = Project(self)
        project.packet("## Scope\n- src/a.py\n", section=NON_ASCII, newline="\r\n")
        data = project.run(stage="review", tier="medium")
        raw = NON_ASCII.replace("\n", "\r\n")
        prompt_bytes = find_run(project.state, data["run_id"]).prompt_path.read_bytes()
        self.assertEqual(prompt_bytes.count(raw.encode("utf-8")), 1)
        self.assertEqual(project.recorded()["stdin"].count(raw), 1)
        prompt = prompt_bytes.decode("utf-8")
        self.assertEqual(sum(1 for line in prompt.replace("\r\n", "\n").split("\n") if line == MARKER), 1)
        self.assertTrue(prompt.endswith(FOOTER))

    def test_swapped_source_refused_on_later_round(self):
        project = Project(self)
        project.packet()
        first = project.run(stage="review", tier="medium")
        binding = json.loads((project.state / "runs" / "t1" / "intent-binding.json").read_text(encoding="utf-8"))
        self.assertEqual((binding["source"], binding["id"], binding["revision"]), (".oah/tasks/t1/PACKET.md", "t1", 2))
        other = project.packet(name="elsewhere/PACKET.md")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="medium", packet=other)
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        self.assertIn("new revision of the bound packet", str(ctx.exception))
        self.assertIn("--packet .oah/tasks/t1/PACKET.md", str(ctx.exception))
        self.assertEqual(project.runs(), [first["run_id"]])

    def test_in_place_edit_refused_and_new_revision_advances(self):
        project = Project(self)
        path = project.packet()
        project.run(stage="review", tier="medium")
        path.write_text(SECTION.replace("Reports miss files.", "edited"), encoding="utf-8", newline="")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="medium")
        self.assertEqual(ctx.exception.code, "OAH_INTENT_NOT_CURRENT")
        self.assertIn("digest differs", str(ctx.exception))
        path.write_text(SECTION.replace("Revision: 2", "Revision: 3"), encoding="utf-8", newline="")
        Store(project.state).rebind(task="t1", agent="codex", ttl_hours=1,
                                    binding=intent.parse(intent.read_source(path)).bind(".oah/tasks/t1/PACKET.md"))
        data = project.run(stage="correction", tier="medium")
        self.assertEqual(data["status"], "finished")
        binding = json.loads((project.state / "runs" / "t1" / "intent-binding.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["revision"] for entry in binding["history"]], [2, 3])
        path.write_text(SECTION, encoding="utf-8", newline="")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="medium")
        self.assertEqual(ctx.exception.code, "OAH_INTENT_NOT_CURRENT")
        self.assertIn("revision is 2, bound 3", str(ctx.exception))

    def test_later_round_reuses_bound_source(self):
        project = Project(self)
        custom = project.packet("## Scope\n- docs/x.md\n", name="plans/T1.md")
        project.run(stage="review", tier="medium", packet=custom)
        data = project.run(stage="correction", tier="medium")
        prompt = find_run(project.state, data["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertIn("Packet: plans/T1.md", prompt)
        self.assertIn("Scope: .oah/tasks/t1", prompt)
        custom.unlink()
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="medium")
        self.assertEqual(ctx.exception.code, "OAH_INTENT_NOT_CURRENT")

    def test_stored_brief_with_other_intent_refused(self):
        project = Project(self)
        project.packet()
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="review", tier="medium", stored_brief=SECTION.replace("Revision: 2", "Revision: 1"))
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        with self.assertRaises(EngineError):
            project.prepare(stage="review", tier="medium", stored_brief="no intent here\n")
        data = project.run(stage="review", tier="medium", stored_brief="Reused.\n\n" + SECTION)
        self.assertEqual(data["status"], "finished")
        self.assertEqual(project.runs(), [data["run_id"]])


def add_worktree(repo: pathlib.Path, path: pathlib.Path, branch: str) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(path)], check=True,
                   capture_output=True)


class WorktreeSelectorTest(unittest.TestCase):
    def setUp(self):
        self.project = Project(self)
        git_commit(self.project.repo, {}, "config")
        base = make_tmp_dir(self)
        self.first, self.second = base / "a" / "wt", base / "b" / "wt"
        add_worktree(self.project.repo, self.first, "lane-x")
        add_worktree(self.project.repo, self.second, "lane-y")
        for task in ("t1", "adhoc"):
            self.project.bind(task, root=self.first)

    def test_mutating_selector_refuses_a_sibling_of_the_leased_tree(self):
        source = ".oah/tasks/t1/PACKET.md"
        packet = self.second / source
        packet.parent.mkdir(parents=True, exist_ok=True)
        packet.write_bytes((self.first / source).read_bytes())
        before = (self.project.state / "leases.json").read_bytes()
        for stage, engine in (("initial", "claude"), ("correction", "codex")):
            with self.subTest(stage=stage):
                code, _, err = run_cli(self.project, "call", engine, "--stage", stage, "--task", "t1",
                                       "--tier", "medium", "--worktree", str(self.second))
                self.assertEqual(code, 1, err)
                self.assertIn("OAH_WORKTREE_LEASE_MISMATCH", err)
                self.assertIn("--worktree", err)
                self.assertFalse(self.project.record.exists())
                self.assertEqual((self.project.state / "leases.json").read_bytes(), before)

    def test_advice_uses_target_worktree_rerank_settings(self):
        config = (self.project.repo / ".oah" / "config.toml").read_text(encoding="utf-8")
        self.project.env["FAKE_RERANK_KEY"] = "fake-" + token_source.token_hex(16)
        provider = types.SimpleNamespace(name="fake", key_name=lambda config: "FAKE_RERANK_KEY",
                                         advise=mock.Mock(return_value=("medium", 0.8)))
        for invoking_enabled, target_enabled in ((True, False), (False, True)):
            with self.subTest(invoking_enabled=invoking_enabled, target_enabled=target_enabled):
                for root, enabled in ((self.project.repo, invoking_enabled), (self.first, target_enabled)):
                    (root / ".oah" / "config.toml").write_text(
                        config + f'[rerank]\nenabled = {str(enabled).lower()}\nprovider = "fake"\n',
                        encoding="utf-8")
                provider.advise.reset_mock()
                with mock.patch.dict(rerank.PROVIDERS, {"fake": provider}):
                    code, out, err = run_cli(self.project, "call", "codex", "--stage", "review", "--task", "t1",
                                             "--worktree", str(self.first), "--json")
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                self.assertEqual((data["tier"], data["advisory"]),
                                 ("medium", "used tier=medium" if target_enabled else "unused(not-configured)"))
                self.assertEqual(pathlib.Path(self.project.recorded()["cwd"]).resolve(), self.first.resolve())
                if target_enabled:
                    provider.advise.assert_called_once()
                else:
                    provider.advise.assert_not_called()

    def test_ambiguous_selector_refused_before_launch(self):
        code, _, err = run_cli(self.project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "medium",
                               "--worktree", "wt")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_WORKTREE_UNRESOLVED"))
        self.assertIn("nothing was launched", err)
        stored = [json.loads(path.read_text(encoding="utf-8")) for path in (self.project.state / "requests").glob("*.json")]
        self.assertEqual({item["worktree"] for item in stored}, {str(self.first.resolve()), str(self.second.resolve())})
        self.assertFalse(self.project.record.exists())
        self.assertEqual(self.project.runs(), [])
        retry = next(line.strip() for line in err.splitlines() if line.strip().startswith("oah call "))
        code, out, err = run_cli(self.project, *retry.split()[1:])
        self.assertEqual(code, 0, err)
        self.assertEqual(pathlib.Path(self.project.recorded()["cwd"]).resolve(), self.first.resolve())

    def test_branch_and_unresolvable_selectors(self):
        root = call.resolve_worktree("lane-y", self.project.repo, retry=lambda path: f"retry {path}")
        self.assertEqual(root.resolve(), self.second.resolve())
        with self.assertRaises(EngineError) as ctx:
            call.resolve_worktree("nope", self.project.repo, retry=lambda path: f"retry {path}")
        self.assertEqual(ctx.exception.code, "OAH_WORKTREE_UNRESOLVED")
        self.assertIn(f"retry {self.first.resolve()}", str(ctx.exception))
        self.assertEqual(call.resolve_worktree(str(self.first), self.project.repo, retry=str).resolve(),
                         self.first.resolve())

    def test_retry_reproduces_stored_request_and_free_text_exactly(self):
        text = "quotes ' \" ’ $ ` % ! spaces 한글\r\nsecond line\n"
        for source in ("message", "file", "stdin"):
            with self.subTest(source=source):
                prompt = self.project.repo / "round instructions.txt"
                spec = self.project.repo / "reused brief.txt"
                prompt.write_bytes(text.encode("utf-8"))
                stored = "Reusable spec.\r\n" + SECTION
                spec.write_bytes(stored.encode("utf-8"))
                extra = {"message": ["--message", text], "file": ["--prompt-file", str(prompt)],
                         "stdin": ["--prompt-file", "-"]}[source]
                stdin = types.SimpleNamespace(buffer=io.BytesIO(text.encode("utf-8")))
                with mock.patch.object(call_cli.sys, "stdin", stdin):
                    code, _, err = run_cli(self.project, "call", "codex", "--stage", "review", "--task", "t1",
                                           "--tier", "medium", "--timeout-seconds", "19", "--brief-file", str(spec),
                                           "--worktree", "wt", *extra)
                self.assertEqual(code, 1, err)
                retry = next(line.strip() for line in err.splitlines() if line.strip().startswith("oah call "))
                words = retry.split()
                self.assertEqual(words[:6], ["oah", "call", "codex", "--stage", "review", "--request-id"])
                self.assertRegex(words[6], r"^q[0-9a-f]{24}$")
                saved = json.loads((self.project.state / "requests" / (words[6] + ".json")).read_text(encoding="utf-8"))
                self.assertEqual(saved["message"], text)
                prompt.unlink()
                spec.unlink()
                with mock.patch.object(call, "prepare", wraps=call.prepare) as prepare:
                    code, _, err = run_cli(self.project, *words[1:])
                self.assertEqual(code, 0, err)
                request = prepare.call_args.args[0]
                self.assertEqual(request.round_instructions, text)
                self.assertEqual(request.stored_brief, stored)
                self.assertEqual((request.task, request.tier, request.timeout_s), ("t1", "medium", 19))
                self.assertIn(text, self.project.recorded()["stdin"])

    def test_saved_request_preserves_saved_or_explicit_require_detach(self):
        for saved_required, retry_required in ((False, True), (True, False), (False, False)):
            with self.subTest(saved_required=saved_required, retry_required=retry_required):
                code, _, err = run_cli(self.project, "call", "codex", "--stage", "review", "--tier", "medium",
                                       "--background", "--worktree", "wt",
                                       *(["--require-detach"] if saved_required else []))
                self.assertEqual(code, 1, err)
                retry = next(line.strip() for line in err.splitlines() if line.strip().startswith("oah call "))
                with mock.patch.object(call, "start_background", return_value="test-run") as start:
                    code, _, err = run_cli(self.project, *retry.split()[1:],
                                           *(["--require-detach"] if retry_required else []))
                self.assertEqual(code, 0, err)
                self.assertEqual(start.call_args.kwargs["require_detach"], saved_required or retry_required)

    def test_retry_refuses_unsafe_embedded_value(self):
        args = argparse.Namespace(engine="codex’;Write-Output-INJECTED", stage="review")
        with self.assertRaises(BriefError) as ctx:
            call_cli.retry_command(args, "safe/path")
        self.assertEqual(ctx.exception.code, "OAH_UNSAFE_ARGUMENT")


def real_packet(task: str = "t1", *, revision: int = 1, goal: str = "the report lists every file",
                scope: str = "src/app.py", constraints: str = "") -> str:
    """A packet the real oah.intent parser accepts."""
    lines = [intent.BEGIN, "## Intent", f"Intent-ID: {task}", f"Revision: {revision}", "Status: accepted",
             "### Problem", "Reports miss files.", "### Desired outcome", "Every file is listed.",
             "### Success conditions", f"- AC1: {goal}", "### Constraints", *([constraints] if constraints else []),
             "### Non-goals", "### Open questions", intent.END, "## Scope", f"- {scope}", "## Plan",
             "## Acceptance", "## Return", ""]
    return "\n".join(lines)


class LeaseBindingTest(unittest.TestCase):
    """`oah call` and the lease's intent binding (lane P4b): refusal, bound source, skills in the brief."""

    def setUp(self):
        self.project = Project(self)
        skills = self.project.home.parent / "home" / ".claude" / "skills"
        for name, description in (("test-driven-development", "write the test first"),
                                  ("verification-before-completion", "evidence before claims"),
                                  ("receiving-code-review", "check each finding")):
            folder = skills / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nBody.\n",
                                             encoding="utf-8")

    def prepare(self, **fields):
        fields.setdefault("task", "t1")
        fields.setdefault("tier", "medium")
        stage = fields.pop("stage", "correction")
        request = call.CallRequest(engine="codex", stage=stage, **fields)
        return call.prepare(request, self.project.settings(), root=self.project.repo, state_dir=self.project.state,
                            env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer,
                            parse_intent=intent.parse, read_source=intent.read_source, keychain=NoKeychain())

    def bind(self, name: str = ".oah/tasks/t1/PACKET.md", **packet) -> pathlib.Path:
        path = self.project.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(real_packet(**packet), encoding="utf-8", newline="")
        binding = intent.parse(path.read_text(encoding="utf-8")).bind(name)
        Store(self.project.state).start(task="t1", agent="claude", scope=("src",), note="", ttl_hours=1,
                                        info=repo_info(self.project.repo), intent=binding)
        return path

    def prompt(self, prepared) -> str:
        return pathlib.Path(prepared.run_dir, "prompt.md").read_bytes().decode("utf-8")

    def test_packet_fixture_keeps_the_source_bytes_used_by_briefs(self):
        path = self.bind()
        self.assertEqual(path.read_bytes(), real_packet().encode("utf-8"))
        prepared = self.prepare()
        self.assertIn(raw_section(real_packet()).encode("utf-8"), prepared.run.prompt_path.read_bytes())

    def test_dispatched_worker_finish_is_refused_and_dispatcher_lease_survives(self):
        self.bind()
        prepared = self.prepare()
        returned = Store(self.project.state).inspect("t1").binding().line() + "\nAC1: met — checked\n"
        command = [sys.executable, "-m", "oah", "finish", "--task", "t1", "--agent", "claude",
                   "--summary", "worker tried finish", "--return", "-", "--allow-out-of-scope"]
        worker = ("import json, subprocess, sys; sys.stdin.read(); "
                  f"result = subprocess.run({command!r}, input={returned!r}, capture_output=True, "
                  "text=True, encoding='utf-8'); "
                  "print(json.dumps({'code': result.returncode, 'out': result.stdout, 'err': result.stderr}))")
        prepared = call.dataclasses.replace(prepared, argv=(sys.executable, "-c", worker))
        paths = [self.project.state / name for name in ("leases.json", "events.jsonl")]
        before = [path.read_bytes() for path in paths]
        env = dict(self.project.env, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent / "src"))
        result = call.execute(prepared, base_env=env)
        self.assertEqual(result["status"], "finished", result)
        child = json.loads(prepared.run.stdout_path.read_text(encoding="utf-8"))
        self.assertEqual(child["code"], 1, child)
        self.assertTrue(child["err"].startswith("OAH_WORKER_LEASE_REFUSED"), child)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(Store(self.project.state).inspect("t1").agent, "claude")

    def test_mutating_dispatch_accepts_case_alias_of_leased_directory(self):
        self.bind()
        alias = self.project.repo.with_name(self.project.repo.name.upper())
        if not alias.is_dir() or not os.path.samefile(alias, self.project.repo):
            self.skipTest("this filesystem does not provide a case alias")
        for stage, engine in (("initial", "claude"), ("correction", "codex")):
            with self.subTest(stage=stage):
                request = call.CallRequest(engine=engine, stage=stage, task="t1", tier="medium")
                prepared = call.prepare(request, self.project.settings(), root=alias, state_dir=self.project.state,
                                        env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer,
                                        keychain=NoKeychain())
                self.assertEqual(call.execute(prepared, base_env=self.project.env)["status"], "finished")

    def test_mutating_dispatch_refuses_distinct_case_named_directories(self):
        self.bind()
        other = self.project.repo.with_name(self.project.repo.name.upper())
        if other.exists():
            self.skipTest("this filesystem does not support distinct case names")
        add_worktree(self.project.repo, other, "case-distinct")
        target = other / ".oah/tasks/t1/PACKET.md"
        target.parent.mkdir(parents=True)
        target.write_bytes((self.project.repo / ".oah/tasks/t1/PACKET.md").read_bytes())
        self.assertFalse(os.path.samefile(other, self.project.repo))
        request = call.CallRequest(engine="codex", stage="correction", task="t1", tier="medium")
        with self.assertRaises(EngineError) as caught:
            call.prepare(request, self.project.settings(), root=other, state_dir=self.project.state,
                         env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer)
        self.assertEqual(caught.exception.code, "OAH_WORKTREE_LEASE_MISMATCH")
        self.assertEqual(self.project.runs(), [])

    def test_mutating_prepare_refuses_a_sibling_with_the_same_intent(self):
        path = self.bind()
        sibling = make_tmp_dir(self) / "sibling"
        add_worktree(self.project.repo, sibling, "sibling")
        target = sibling / path.relative_to(self.project.repo)
        target.parent.mkdir(parents=True)
        target.write_bytes(path.read_bytes())
        request = call.CallRequest(engine="codex", stage="correction", task="t1", tier="medium")
        with self.assertRaises(EngineError) as caught:
            call.prepare(request, self.project.settings(), root=sibling, state_dir=self.project.state,
                         env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer)
        self.assertEqual(caught.exception.code, "OAH_WORKTREE_LEASE_MISMATCH")
        self.assertEqual(self.project.runs(), [])

    def test_mutating_launch_refuses_an_out_of_scope_edit_in_a_sibling(self):
        path = self.bind()
        sibling = make_tmp_dir(self) / "sibling"
        add_worktree(self.project.repo, sibling, "sibling")
        target = sibling / path.relative_to(self.project.repo)
        target.parent.mkdir(parents=True)
        target.write_bytes(path.read_bytes())
        prepared = self.prepare()
        worker = (sys.executable, "-c", "import pathlib, sys; sys.stdin.read(); "
                  "pathlib.Path('outside.py').write_bytes(b'escaped scope\\n')")
        prepared = call.dataclasses.replace(prepared, cwd=str(sibling), argv=worker)
        with self.assertRaises(EngineError) as caught:
            call.execute(prepared, base_env=self.project.env)
        self.assertEqual(caught.exception.code, "OAH_WORKTREE_LEASE_MISMATCH")
        self.assertEqual(prepared.run.read()["status"], "refused")
        self.assertFalse((sibling / "outside.py").exists())
        self.assertFalse((self.project.repo / "outside.py").exists())

    def test_prepare_refuses_lease_changes_during_engine_resolution(self):
        for change in ("release", "replace", "rebind", "renew", "gates", "scope", "source"):
            with self.subTest(change=change):
                self.project = Project(self)
                path = self.bind()
                store = Store(self.project.state)
                checked = store.inspect("t1")
                resolve = call.resolve_engine

                def changed(*args, **kwargs):
                    resolution = resolve(*args, **kwargs)
                    if change in ("release", "replace"):
                        store.release(task="t1", agent="claude", reason="resolution race", force=False)
                        if change == "replace":
                            store.start(task="t1", agent="claude", scope=checked.scope, note="replacement",
                                        ttl_hours=1, info=repo_info(self.project.repo), intent=checked.binding())
                    elif change == "rebind":
                        store.rebind(task="t1", agent="claude", binding=checked.binding(), ttl_hours=1)
                    elif change == "source":
                        path.write_text(real_packet(goal="changed during resolution"), encoding="utf-8", newline="")
                    else:
                        extra = {"gates": ["checks/late.md"]} if change == "gates" else (
                            {"scope": ["src", "tests"]} if change == "scope" else {})
                        store.renew(task="t1", agent="claude", ttl_hours=1, **extra)
                    return resolution

                with mock.patch.object(call, "resolve_engine", side_effect=changed):
                    with self.assertRaises(EngineError) as caught:
                        self.prepare()
                self.assertEqual(caught.exception.code,
                                 "OAH_INTENT_NOT_CURRENT" if change == "source" else "OAH_LEASE_CHANGED")
                self.assertIn("again", str(caught.exception))
                self.assertIsNone(call.dispatch_binding(self.project.state, "t1"))
                self.assertFalse(self.project.record.exists())

    def test_execute_and_background_dispatch_refuse_changed_leases(self):
        for launch in ("foreground", "background", "runner"):
            for change in ("renew", "release", "source"):
                with self.subTest(launch=launch, change=change):
                    self.project = Project(self)
                    path = self.bind()
                    prepared = self.prepare()
                    store = Store(self.project.state)
                    if change == "renew":
                        store.renew(task="t1", agent="claude", ttl_hours=1)
                    elif change == "release":
                        store.release(task="t1", agent="claude", reason="queued race", force=False)
                    else:
                        path.write_text(real_packet(goal="changed while queued"), encoding="utf-8", newline="")
                    expected = "OAH_INTENT_NOT_CURRENT" if change == "source" else "OAH_LEASE_CHANGED"
                    if launch == "runner":
                        prepared.run.launch_path.write_text(json.dumps(prepared.to_json()), encoding="utf-8")
                        result = subprocess.run(call._boot_argv("background_main", prepared.run_dir),
                                                cwd=self.project.repo, env=self.project.env, capture_output=True,
                                                text=True, encoding="utf-8", timeout=30)
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn(expected, result.stderr)
                    else:
                        function = call.execute if launch == "foreground" else call.start_background
                        with self.assertRaises(EngineError) as caught:
                            function(prepared, base_env=self.project.env)
                        self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(prepared.run.read()["status"], "refused")
                    self.assertFalse(self.project.record.exists())

    def test_background_launcher_preserves_a_refusal_after_queuing(self):
        self.bind()
        prepared = self.prepare()
        launch = subprocess.run

        def changed(*args, **kwargs):
            Store(self.project.state).renew(task="t1", agent="claude", ttl_hours=1)
            return launch(*args, **kwargs)

        with mock.patch.object(call.subprocess, "run", side_effect=changed):
            with self.assertRaises(EngineError) as caught:
                call.start_background(prepared, base_env=self.project.env)
        self.assertEqual(caught.exception.code, "OAH_LEASE_CHANGED")
        self.assertEqual(prepared.run.read()["status"], "refused")
        self.assertFalse(self.project.record.exists())

    def test_spawn_holds_the_state_lock_but_engine_execution_releases_it(self):
        self.bind()
        prepared = self.prepare()
        script = (
            "import pathlib, sys\n"
            "from oah.lease import Store\n"
            "from oah.lockfile import LockTimeout, StateLock\n"
            "try:\n"
            "    store = Store(pathlib.Path(sys.argv[1]))\n"
            "    with StateLock(store.lock_path, timeout=0.15):\n"
            "        assert store.snapshot()[0][0].task == 't1'\n"
            "except LockTimeout:\n"
            "    print('locked')\n"
            "else:\n"
            "    print('unlocked')\n")
        argv = (sys.executable, "-c", script, str(self.project.state))
        prepared = call.dataclasses.replace(prepared, argv=argv)
        popen = subprocess.Popen
        environment = dict(self.project.env, PYTHONPATH=str(FAKES.parent.parent / "src"))

        def spawning(*args, **kwargs):
            contender = popen(argv, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = contender.communicate(timeout=10)
            self.assertEqual(contender.returncode, 0, err.decode("utf-8"))
            self.assertEqual(out.strip(), b"locked", "the actual spawn must hold the lease's state lock")
            return popen(*args, **kwargs)

        with mock.patch.object(call.subprocess, "Popen", side_effect=spawning):
            result = call.execute(prepared, base_env=environment)
        self.assertEqual(result["status"], "finished", result.get("error"))
        self.assertIn("unlocked", prepared.run.stdout_path.read_text(encoding="utf-8"))

    def test_call_refuses_stale_and_missing_binding(self):
        path = self.bind()
        self.assertTrue(self.prompt(self.prepare()))
        path.write_text(real_packet(goal="a different goal"), encoding="utf-8", newline="")
        for case in ("stale", "missing"):
            with self.subTest(case=case):
                if case == "missing":
                    path.unlink()
                before = self.project.runs()
                with self.assertRaises(EngineError) as ctx:
                    self.prepare(stage="review")
                self.assertEqual(ctx.exception.code, "OAH_INTENT_NOT_CURRENT")
                self.assertIn(f"is {case}", str(ctx.exception))
                self.assertIn("oah start --task t1 --agent claude --packet .oah/tasks/t1/PACKET.md", str(ctx.exception))
                self.assertEqual(self.project.runs(), before)

    def test_call_requires_a_current_lease_binding(self):
        for leased in (False, True):
            for packet_status in (None, "accepted", "draft"):
                with self.subTest(leased=leased, packet_status=packet_status):
                    project = Project(self)
                    if packet_status is not None:
                        project.packet(section=real_packet().replace("Status: accepted",
                                                                    f"Status: {packet_status}"))
                    if leased:
                        Store(project.state).start(task="t1", agent="claude", scope=("src",), note="",
                                                    ttl_hours=1, info=repo_info(project.repo))
                    request = call.CallRequest(engine="codex", stage="review", task="t1", tier="medium")
                    with self.assertRaises(EngineError) as caught:
                        call.prepare(request, project.settings(), root=project.repo, state_dir=project.state,
                                     env=project.env, footer=intent.FOOTER, has_footer=intent.has_footer,
                                     parse_intent=intent.parse, read_source=intent.read_source,
                                     keychain=NoKeychain())
                    self.assertEqual(caught.exception.code, "OAH_INTENT_NOT_CURRENT")
                    self.assertIn("oah start --task t1", str(caught.exception))
                    self.assertEqual(project.runs(), [])
                    self.assertFalse(project.record.exists())

    def test_call_refuses_a_bound_source_changed_to_draft(self):
        path = self.bind()
        path.write_text(path.read_text(encoding="utf-8").replace("Status: accepted", "Status: draft"),
                        encoding="utf-8", newline="")
        with self.assertRaises(EngineError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "OAH_INTENT_NOT_CURRENT")
        self.assertEqual(self.project.runs(), [])

    def test_dispatch_history_does_not_replace_a_live_lease(self):
        self.bind()
        self.prepare()
        before = self.project.runs()
        Store(self.project.state).release(task="t1", agent="claude", reason="task stopped", force=False)
        self.assertIsNotNone(call.dispatch_binding(self.project.state, "t1"))
        with self.assertRaises(EngineError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "OAH_INTENT_NOT_CURRENT")
        self.assertEqual(self.project.runs(), before)

    def test_explicit_draft_cannot_bypass_checks_when_parser_is_omitted(self):
        self.bind()
        draft = self.project.packet(name="plans/draft.md", section=real_packet().replace("Status: accepted",
                                                                                       "Status: draft"))
        request = call.CallRequest(engine="codex", stage="review", task="t1", tier="medium", packet=draft)
        with self.assertRaises(EngineError) as caught:
            call.prepare(request, self.project.settings(), root=self.project.repo, state_dir=self.project.state,
                         env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer,
                         keychain=NoKeychain())
        self.assertEqual(caught.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        self.assertEqual(self.project.runs(), [])

    def test_call_uses_lease_binding_source(self):
        self.bind("plans/T1.md")
        prompt = self.prompt(self.prepare())
        self.assertIn("- Packet: plans/T1.md", prompt)
        self.assertEqual(prompt.count(intent.BEGIN), 1)
        other = self.project.repo / ".oah" / "tasks" / "t1" / "PACKET.md"
        other.parent.mkdir(parents=True)
        other.write_text(real_packet(), encoding="utf-8", newline="")
        with self.assertRaises(EngineError) as ctx:
            self.prepare(packet=other)
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        self.assertIn("plans/T1.md", str(ctx.exception))

    def test_brief_lists_skills_with_reasons_and_rerank(self):
        self.bind()
        prepared = self.prepare()
        prompt = self.prompt(prepared)
        self.assertIn("- Skills: test-driven-development, receiving-code-review, verification-before-completion "
                      "(reasons in ## Skills below)", prompt)
        skills = prompt.split("## Skills\n", 1)[1].split(intent.BEGIN, 1)[0]
        self.assertIn("Skill order: rerank=unused(not-enabled)", skills)
        self.assertIn("- `test-driven-development` (user): `~/.claude/skills/test-driven-development/SKILL.md`", skills)
        self.assertIn("  - why: code changes are written test-first (kind implementation, stage correction)", skills)
        self.assertIn("  - why: corrections check each review finding before acting on it (stage correction)", skills)
        record = json.loads(pathlib.Path(prepared.run_dir, "skills.json").read_text(encoding="utf-8"))
        self.assertEqual(record["rerank"], "unused(not-enabled)")
        self.assertEqual([skill["id"] for skill in record["skills"]],
                         ["test-driven-development", "receiving-code-review", "verification-before-completion"])

    def test_brief_keeps_intent_section_and_footer_byte_intact_with_skills(self):
        path = self.bind()
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        Store(self.project.state).release(task="t1", agent="claude", reason="rebind CRLF copy", force=False)
        binding = intent.parse(path.read_bytes().decode("utf-8")).bind(".oah/tasks/t1/PACKET.md")
        Store(self.project.state).start(task="t1", agent="claude", scope=("src",), note="", ttl_hours=1,
                                        info=repo_info(self.project.repo), intent=binding)
        prompt = self.prompt(self.prepare())
        section = raw_section(path.read_bytes().decode("utf-8"))
        self.assertEqual(prompt.count(section), 1)
        self.assertIn("\r\n", section)
        self.assertTrue(prompt.endswith(intent.FOOTER))
        self.assertEqual(sum(line == intent.FOOTER_MARKER for line in prompt.replace("\r\n", "\n").split("\n")), 1)
        self.assertLess(prompt.index("## Skills"), prompt.index(section))

    def test_no_rerank_flag_and_packet_constraint(self):
        project = self.project
        (project.repo / ".oah" / "config.toml").write_text(
            (project.repo / ".oah" / "config.toml").read_text(encoding="utf-8")
            + '[rerank]\nenabled = true\nprovider = "fake"\n', encoding="utf-8")
        project.env["FAKE_RERANK_KEY"] = "fake-" + token_source.token_hex(16)
        provider = types.SimpleNamespace(name="fake", key_name=lambda config: "FAKE_RERANK_KEY",
                                         score=mock.Mock(side_effect=lambda kind, query, items, *, key, config:
                                                         list(range(len(items)))),
                                         advise=mock.Mock(return_value=("max", 0.8)))
        path = self.bind()
        with mock.patch.dict(rerank.PROVIDERS, {"fake": provider}):
            used = self.prompt(self.prepare(tier=None))
            self.assertIn("Skill order: rerank=used", used)
            order = used.split("- Skills: ", 1)[1].split(" (reasons", 1)[0]
            self.assertEqual(order, "verification-before-completion, receiving-code-review, test-driven-development")
            provider.score.assert_called_once()
            provider.advise.assert_called_once()
            provider.score.reset_mock()
            provider.advise.reset_mock()
            disabled = self.prepare(tier=None, no_rerank=True)
            self.assertIn("rerank=unused(disabled-by-brief)", self.prompt(disabled))
            self.assertEqual(disabled.run.read()["advisory"], "unused(disabled-by-brief)")
            provider.score.assert_not_called()
            provider.advise.assert_not_called()
            path.write_text(real_packet(revision=2, constraints="No re-ranker for this task."), encoding="utf-8", newline="")
            Store(project.state).rebind(task="t1", agent="claude", ttl_hours=1,
                                        binding=intent.parse(path.read_text(encoding="utf-8")).bind(
                                            ".oah/tasks/t1/PACKET.md"))
            disabled = self.prepare(tier=None)
            self.assertIn("rerank=unused(disabled-by-brief)", self.prompt(disabled))
            self.assertEqual(disabled.run.read()["advisory"], "unused(disabled-by-brief)")
            provider.score.assert_not_called()
            provider.advise.assert_not_called()
        secret = project.env["FAKE_RERANK_KEY"]
        for path in project.state.rglob("*"):
            if path.is_file():
                self.assertFalse(secret.encode() in path.read_bytes(), f"runtime key leaked into {path.name}")


class HeaderTest(unittest.TestCase):
    def test_header_downgrade_fails_run(self):
        data = Project(self).run(tier="high", env={"FAKE_CODEX_HEADER_EFFORT": "medium"})
        self.assertEqual((data["status"], data["header_check"], data["header_code"]),
                         ("failed", "downgraded", "OAH_EFFORT_DOWNGRADED"))
        self.assertTrue(data["error"].startswith("OAH_EFFORT_DOWNGRADED"))
        self.assertFalse(data["effort_verified"])

    def test_header_mismatch_fails_run(self):
        data = Project(self).run(tier="high", env={"FAKE_CODEX_HEADER_EFFORT": "max"})
        self.assertEqual((data["status"], data["header_code"]), ("failed", "OAH_EFFORT_MISMATCH"))

    def test_unreadable_header_is_unverified_not_a_match(self):
        data = Project(self).run(tier="high", env={"FAKE_CODEX_HEADER_EFFORT": "-"})
        self.assertEqual((data["status"], data["header_effort"], data["header_check"],
                          data["resolved_inference_effort"], data["effort_verified"]),
                         ("finished", UNVERIFIED, UNVERIFIED, UNVERIFIED, False))

    def test_unknown_cli_version_is_unverified(self):
        data = Project(self).run(tier="high", env={"FAKE_CODEX_VERSION": "0.170.0"})
        self.assertEqual((data["header_check"], data["resolved_inference_effort"], data["effort_verified"]),
                         ("match", UNVERIFIED, False))

    def test_engine_failure(self):
        data = Project(self).run(tier="medium", env={"FAKE_EXIT": "3"})
        self.assertEqual((data["status"], data["exit_code"]), ("failed", 3))
        self.assertTrue(data["error"].startswith("OAH_ENGINE_FAILED"))


def wait_until_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not call._alive(pid):
            return True
        time.sleep(0.1)
    return False


class TimeoutAndBackgroundTest(unittest.TestCase):
    def test_denied_breakaway_fallback_completes_a_real_background_run(self):
        project = Project(self)
        prepared = project.prepare(tier="medium")
        popen = subprocess.Popen

        def spawn(*args, **kwargs):
            flags = kwargs.pop("creationflags", 0)
            if flags & 0x01000000:
                raise PermissionError("breakaway denied")
            if os.name == "nt":
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            return popen(*args, **kwargs)

        def launch(argv, **kwargs):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(call, "_windows", return_value=True), \
                    mock.patch.object(call.subprocess, "Popen", side_effect=spawn), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = call.launch_main(prepared.run_dir)
            return subprocess.CompletedProcess(argv, code, out.getvalue().encode(), err.getvalue().encode())

        with mock.patch.object(call.subprocess, "run", side_effect=launch), \
                mock.patch.dict(os.environ, project.env, clear=True), contextlib.redirect_stderr(io.StringIO()) as err:
            run_id = call.start_background(prepared, base_env=project.env)
        data = call.wait(project.state, run_id, timeout=30)
        self.assertEqual((data["status"], data["detached"]), ("finished", "unverified"))
        self.assertIn("closing this terminal may stop the run", err.getvalue())
        self.assertTrue(any("--require-detach" in warning for warning in data["warnings"]))

    def test_runner_can_finish_before_launcher_returns(self):
        project = Project(self)
        prepared = project.prepare(tier="medium")
        launch = subprocess.run

        def complete_before_return(*args, **kwargs):
            result = launch(*args, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(call.wait(project.state, prepared.run_id, timeout=20)["status"], "finished")
            return result

        with mock.patch.object(call.subprocess, "run", side_effect=complete_before_return):
            call.start_background(prepared, base_env=project.env)
        data = call.wait(project.state, prepared.run_id, timeout=0)
        self.assertEqual((data["status"], data["exit_code"]), ("finished", 0))
        self.assertIsNotNone(data["finished_at"])

    def test_windows_breakaway_denial_falls_back_and_records_unverified(self):
        project = Project(self)
        prepared = project.prepare(tier="medium")
        from oah.atomic import write_json_atomic
        write_json_atomic(prepared.run.launch_path, dict(prepared.to_json(), require_detach=False))
        child = types.SimpleNamespace(pid=12345, returncode=None)
        real_popen = subprocess.Popen
        launcher = call._boot_argv("background_main", prepared.run_dir)

        def spawn(argv, **kwargs):
            if list(argv) != launcher:
                return real_popen(argv, **kwargs)
            if kwargs["creationflags"] & 0x01000000:
                raise PermissionError("breakaway denied")
            return child

        with mock.patch.object(call, "_windows", return_value=True), \
                mock.patch.object(call.subprocess, "Popen", side_effect=spawn) as popen, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = call.launch_main(prepared.run_dir)
        self.assertEqual(code, 0)
        launches = [item for item in popen.call_args_list if list(item.args[0]) == launcher]
        self.assertEqual(len(launches), 2)
        self.assertTrue(launches[0].kwargs["creationflags"] & 0x01000000)
        self.assertFalse(launches[1].kwargs["creationflags"] & 0x01000000)
        data = prepared.run.read()
        self.assertEqual(data["detached"], "unverified")
        self.assertIn("closing this terminal may stop the run", data["warnings"][-1])
        self.assertIn("--require-detach", err.getvalue())

    def test_require_detach_refuses_when_windows_denies_breakaway(self):
        project = Project(self)
        prepared = project.prepare(tier="medium")
        from oah.atomic import write_json_atomic
        write_json_atomic(prepared.run.launch_path, dict(prepared.to_json(), require_detach=True))
        real_popen = subprocess.Popen
        launcher = call._boot_argv("background_main", prepared.run_dir)

        def spawn(argv, **kwargs):
            if list(argv) == launcher:
                raise PermissionError("breakaway denied")
            return real_popen(argv, **kwargs)

        with mock.patch.object(call, "_windows", return_value=True), \
                mock.patch.object(call.subprocess, "Popen", side_effect=spawn) as popen, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = call.launch_main(prepared.run_dir)
        self.assertEqual(code, call_cli.EXIT_ENVIRONMENT)
        self.assertEqual(sum(list(item.args[0]) == launcher for item in popen.call_args_list), 1)
        self.assertEqual(prepared.run.read()["detached"], "unverified")
        self.assertIn("OAH_RUN_DETACH_FAILED", err.getvalue())
        self.assertIn("without --require-detach", err.getvalue())

    def test_verified_detachment_requires_successful_launch(self):
        project = Project(self)
        prepared = project.prepare(tier="medium")
        from oah.atomic import write_json_atomic
        write_json_atomic(prepared.run.launch_path, dict(prepared.to_json(), require_detach=True))
        real_popen = subprocess.Popen
        launcher = call._boot_argv("background_main", prepared.run_dir)

        def spawn(argv, **kwargs):
            if list(argv) == launcher:
                return types.SimpleNamespace(pid=12345)
            return real_popen(argv, **kwargs)

        with mock.patch.object(call, "_windows", return_value=True), \
                mock.patch.object(call.subprocess, "Popen", side_effect=spawn), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(call.launch_main(prepared.run_dir), 0)
        self.assertEqual(prepared.run.read()["detached"], "verified")

    @unittest.skipUnless(os.name == "nt", "native Windows denied-breakaway job")
    def test_background_finishes_inside_job_that_denies_breakaway(self):
        from oah.engines.proc import run_command

        project = Project(self)
        for required in (False, True):
            with self.subTest(require_detach=required):
                prepared = project.prepare(tier="medium")
                script = ("import sys; sys.path.insert(0,sys.argv[1]); from oah import call; "
                          "from oah.atomic import read_json; from pathlib import Path; "
                          "p=call.Prepared.from_json(read_json(Path(sys.argv[2]),None)); "
                          f"call.start_background(p,require_detach={required!r}); "
                          "data=call.wait(Path(p.run_dir).parents[2],p.run_id,timeout=30); "
                          "raise SystemExit(0 if data['status']=='finished' else 1)")
                from oah.atomic import write_json_atomic
                write_json_atomic(prepared.run.launch_path, prepared.to_json())
                result = run_command([sys.executable, "-c", script, str(pathlib.Path(call.__file__).parent.parent),
                                      str(prepared.run.launch_path)], timeout=45, env=project.env)
                self.assertEqual(prepared.run.read()["detached"], "unverified")
                if required:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("OAH_RUN_DETACH_FAILED", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(prepared.run.read()["status"], "finished")
                    self.assertIn("closing this terminal may stop the run", result.stderr)

    @unittest.skipUnless(os.name == "nt", "native Windows kill-on-close jobs")
    def test_background_survives_originating_job_close(self):
        import ctypes
        from ctypes import wintypes
        from oah.atomic import write_json_atomic
        from oah.engines.proc import _WindowsJob

        project = Project(self)
        prepared = project.prepare(tier="medium", timeout_s=30)
        write_json_atomic(prepared.run.launch_path, prepared.to_json())
        ready = make_tmp_dir(self) / "origin-ready"
        # Give the originating process a kill-on-close job that explicitly permits breakaway.
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        job = _WindowsJob()
        self.addCleanup(job.close)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        self.assertTrue(job.api.SetInformationJobObject(job.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        script = ("import pathlib,sys,time; sys.path.insert(0,sys.argv[3]); "
                  "from oah import call; from oah.atomic import read_json; "
                  "p=call.Prepared.from_json(read_json(pathlib.Path(sys.argv[1]),None)); "
                  "call.start_background(p); pathlib.Path(sys.argv[2]).touch(); time.sleep(60)")
        origin = subprocess.Popen([sys.executable, "-c", script, str(prepared.run.launch_path), str(ready),
                                   str(pathlib.Path(call.__file__).resolve().parent.parent)],
                                  env=dict(project.env, FAKE_SLEEP="3"), creationflags=0x4,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: origin.kill() if origin.poll() is None else None)
        self.addCleanup(origin.stderr.close)
        try:
            job.start(origin)
            deadline = time.monotonic() + 20
            while not ready.exists() and origin.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not ready.exists():
                origin.kill()
                self.fail(origin.communicate(timeout=10)[1].decode("utf-8", errors="replace"))
            data = prepared.run.read()
            if data["detached"] == "unverified":
                self.assertEqual(call.wait(project.state, prepared.run_id, timeout=30)["status"], "finished")
                self.skipTest("The host job denies breakaway; fallback completed but survival is unverified")
            self.assertEqual(data["detached"], "verified")
            self.assertNotIn(data["status"], ("finished", "failed"))
        finally:
            job.close()  # kills the origin; the detached runner must outlive it
        origin.wait(timeout=10)
        self.assertEqual(call.wait(project.state, prepared.run_id, timeout=30)["status"], "finished")

    def test_timeout_kills_process_tree(self):
        project = Project(self)
        pidfile = make_tmp_dir(self) / "child.pid"
        env = {"FAKE_CHILD_PIDFILE": str(pidfile), "FAKE_SLEEP": "60"}
        started = time.monotonic()
        data = project.run(tier="medium", env=env, timeout_s=2)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((data["status"], data["timed_out"], data["exit_code"]), ("timed_out", True, None))
        self.assertTrue(data["error"].startswith("OAH_RUN_TIMEOUT"))
        grandchild = int(pidfile.read_text(encoding="utf-8"))
        self.assertTrue(wait_until_gone(grandchild), f"grandchild {grandchild} survived the timeout")

    def test_background_and_wait(self):
        project = Project(self)
        prepared = project.prepare(tier="medium")
        run_id = call.start_background(prepared, base_env=project.env)
        self.assertIn(find_run(project.state, run_id).read()["status"], ("queued", "running", "finished"))
        data = call.wait(project.state, run_id, timeout=60)
        self.assertEqual(data["status"], "finished", data.get("error"))
        self.assertEqual(call.wait(project.state, run_id, timeout=0), data)
        record = project.recorded()
        self.assertEqual(record["env"]["OAH_RUN_ID"], run_id)

    def test_wait_is_bounded_and_can_be_called_again(self):
        project = Project(self)
        prepared = project.prepare(tier="medium", env={"FAKE_SLEEP": "4"}, timeout_s=30)
        run_id = call.start_background(prepared, base_env=dict(project.env, FAKE_SLEEP="4"))
        started = time.monotonic()
        first = call.wait(project.state, run_id, timeout=0.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn(first["status"], ("queued", "running"))
        self.assertEqual(call.WAIT_LIMIT_S, 540)
        final = call.wait(project.state, run_id, timeout=60)
        self.assertEqual(final["status"], "finished", final.get("error"))

    @unittest.skipUnless(os.name == "nt", "Windows process query")
    def test_alive_reports_windows_processes(self):
        self.assertTrue(call._alive(os.getpid()))
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.assertFalse(call._alive(gone.pid))

    def test_lost_runner(self):
        state = make_tmp_dir(self)
        run = new_run(state, "t1", "codex", "review")
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        run.result(status="running", pid=gone.pid)
        data = call.wait(state, run.id, timeout=5)
        self.assertEqual(data["status"], "lost")
        self.assertTrue(data["error"].startswith("OAH_RUN_LOST"))


def run_cli(project: Project, *argv: str) -> tuple[int, str, str]:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    call_cli.register(commands)
    args = parser.parse_args(list(argv))
    if args.command == "call" and not args.worktree and not args.request_id:
        project.bind(args.task, path=pathlib.Path(args.packet) if args.packet else None)
    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    os.chdir(project.repo)
    try:
        with mock.patch.dict(os.environ, project.env, clear=True), \
                mock.patch.object(call_cli, "intent_tools",
                                  return_value=call_cli.IntentTools(FOOTER, has_footer, parse_intent, call._read_text)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = call_cli.run(args)
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class CallCliTest(unittest.TestCase):
    def test_require_detach_flag_is_forwarded_and_requires_background(self):
        project = Project(self)
        with mock.patch.object(call, "start_background", return_value="test-run") as start:
            code, _, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "medium",
                                   "--background", "--require-detach")
        self.assertEqual(code, 0, err)
        self.assertTrue(start.call_args.kwargs["require_detach"])
        code, _, err = run_cli(project, "call", "codex", "--stage", "review", "--require-detach")
        self.assertEqual(code, 2)
        self.assertIn("--background", err)

    def test_detachment_refusal_is_an_environment_error(self):
        project = Project(self)
        with mock.patch.object(call, "start_background", side_effect=EngineError(
                "OAH_RUN_DETACH_FAILED", "job breakaway denied; retry without --background")):
            code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "medium",
                                     "--background")
        self.assertEqual(code, call_cli.EXIT_ENVIRONMENT)
        self.assertIn("without --background", err)
        self.assertNotIn("OAH_RUN_STARTED", out)

    def test_cli_call_json(self):
        project = Project(self)
        code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "medium",
                                 "--message", "hello", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["status"], data["tier"], data["tier_source"]), ("finished", "medium", "flag"))
        self.assertIn("hello", project.recorded()["stdin"])

    def test_cli_background_then_wait(self):
        project = Project(self)
        code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "medium",
                                 "--background")
        self.assertEqual(code, 0, err)
        run_id = out.split("run=", 1)[1].split()[0]
        self.assertIn(f"oah wait {run_id}", out)
        code, out, err = run_cli(project, "wait", run_id, "--timeout-seconds", "60")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"OAH_RUN_FINISHED run={run_id}"))

    def test_cli_refusals(self):
        project = Project(self)
        code, _, err = run_cli(project, "call", "claude", "--stage", "review", "--tier", "medium")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_STAGE_ENGINE_MISMATCH"))
        code, _, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "medium", "--risk", "high")
        self.assertEqual(code, 2)
        code, _, err = run_cli(project, "wait", "nonsense")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_RUN_ID_INVALID"))

    def test_cli_config_error_is_environment(self):
        project = Project(self, "[engines.gemini]\ncommand = [\"x\"]\n")
        code, _, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "medium")
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_CONFIG_UNKNOWN_KEY"))

    @unittest.skipIf(importlib.util.find_spec("oah.intent"), "oah.intent exists, so the footer is available")
    def test_cli_without_intent_module_refuses(self):
        project = Project(self)
        parser = argparse.ArgumentParser()
        call_cli.register(parser.add_subparsers(dest="command"))
        err = io.StringIO()
        previous = os.getcwd()
        os.chdir(project.repo)
        try:
            with mock.patch.dict(os.environ, project.env, clear=True), contextlib.redirect_stderr(err):
                code = call_cli.run(parser.parse_args(["call", "codex", "--stage", "review", "--tier", "medium"]))
        finally:
            os.chdir(previous)
        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue().startswith("OAH_FOOTER_NOT_APPENDED"))


class UnpooledNoticeTest(unittest.TestCase):
    """Only a launch that rebuilt the catalog names a listed model in no pool; `oah models` always does."""

    POOLS = "[engines.codex.tier_pools]\n" + "".join(f'{tier} = ["gpt-test-a"]\n'
                                                     for tier in ("low", "medium", "high", "max"))

    def project(self) -> Project:
        project = Project(self, self.POOLS)
        cache = project.home / "models_cache.json"
        data = json.loads(cache.read_text(encoding="utf-8"))
        data["models"].append({"slug": "gpt-test-new", "supported_reasoning_levels": [{"effort": "low"}]})
        cache.write_text(json.dumps(data), encoding="utf-8")
        return project

    def run_models(self, project: Project, *argv: str) -> tuple[int, str, str]:
        parser = argparse.ArgumentParser()
        models_cli.register(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["models", *argv])
        out, err = io.StringIO(), io.StringIO()
        previous = os.getcwd()
        os.chdir(project.repo)
        try:
            with mock.patch.dict(os.environ, project.env, clear=True), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = models_cli.run(args)
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    def test_launch_warns_only_on_rebuild_and_keeps_the_resolved_model(self):
        project = self.project()
        config = project.repo / ".oah" / "config.toml"
        before = config.read_bytes()
        prepared = project.prepare(tier="medium")
        found = [warning for warning in prepared.warnings if warning.startswith("OAH_MODEL_NEW_UNPOOLED: ")]
        self.assertEqual(len(found), 1, prepared.warnings)
        self.assertIn("gpt-test-new", found[0])
        self.assertIn("[engines.codex.tier_pools]", found[0])
        self.assertEqual(prepared.model, "gpt-test-a")
        for env, rebuilt in (({}, False), ({}, False), ({"FAKE_CODEX_VERSION": "0.164.0"}, True),
                             ({"FAKE_CODEX_VERSION": "0.164.0"}, False)):
            with self.subTest(env=env, rebuilt=rebuilt):
                prepared = project.prepare(tier="medium", env=env)
                self.assertEqual(prepared.model, "gpt-test-a")
                self.assertEqual(any(w.startswith("OAH_MODEL_NEW_UNPOOLED: ") for w in prepared.warnings), rebuilt)
        self.assertEqual({path.name for path in (project.state / "models").iterdir()},
                         {"catalog.json", "resolved.json", "changes.jsonl"})
        self.assertEqual(config.read_bytes(), before)

    def test_an_unrefreshed_launch_is_quiet_and_oah_models_still_reports(self):
        project = self.project()
        argv = ("call", "codex", "--stage", "review", "--task", "t1", "--tier", "medium", "--message", "hello")
        code, out, err = run_cli(project, *argv)
        self.assertEqual(code, 0, err)
        self.assertEqual((out + err).count("OAH_MODEL_NEW_UNPOOLED"), 1, out + err)
        code, out, err = run_cli(project, *argv)
        self.assertEqual(code, 0, err)
        self.assertNotIn("OAH_MODEL_NEW_UNPOOLED", out + err)
        for extra, unpooled in (((), None), (("--json",), ["gpt-test-new"])):
            with self.subTest(extra=extra):
                code, out, err = self.run_models(project, *extra)
                self.assertEqual(code, 0, err)
                self.assertEqual((out + err).count("OAH_MODEL_NEW_UNPOOLED:"), 1 + (unpooled is not None), out + err)
                if unpooled is not None:
                    self.assertEqual(json.loads(out)["engines"]["codex"]["unpooled"], unpooled)

    def test_empty_configured_pool_warns_on_rebuild(self):
        project = self.project()
        config = project.repo / ".oah" / "config.toml"
        pools = re.sub(r'^(low|medium|high|max) = \["gpt-test-a"\]', r"\1 = []", config.read_text(encoding="utf-8"),
                       flags=re.MULTILINE)
        config.write_text(pools, encoding="utf-8")
        prepared = project.prepare(tier="medium")
        self.assertEqual(prepared.model, "gpt-test-a")
        found = [warning for warning in prepared.warnings if warning.startswith("OAH_MODEL_NEW_UNPOOLED: ")]
        self.assertTrue(any("gpt-test-new" in warning for warning in found), prepared.warnings)
        self.assertTrue(any("gpt-test-a" in warning for warning in found), prepared.warnings)

    def test_correction_refresh_keeps_catalog_writes_sealed(self):
        project = self.project()
        project.prepare(tier="medium")
        before = {path: path.read_bytes() for path in (project.state / "models").iterdir()}
        token = call._CORRECTION_INPUT_CHECK.set(lambda value: None)
        try:
            catalog = call._CorrectionCatalog(project.state)
            inputs_token = call._CORRECTION_INPUTS.set(mock.Mock(catalog=catalog))
            try:
                _, freshness = call.check_catalog("codex", project.settings(), state_dir=project.state,
                                                  env=project.env, refresh=True)
            finally:
                call._CORRECTION_INPUTS.reset(inputs_token)
        finally:
            call._CORRECTION_INPUT_CHECK.reset(token)
        self.assertTrue(any(w.startswith("OAH_MODEL_NEW_UNPOOLED: ") for w in freshness.warnings))
        self.assertEqual(set(catalog.writes), {catalog.catalog_path})
        self.assertEqual({path: path.read_bytes() for path in (project.state / "models").iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
