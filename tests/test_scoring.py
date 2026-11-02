# 점수 규칙 검사 — 손으로 만든 점수표로 단·프로필별 선택을 고정하고, 세대·모델군·공통 과제·자료 손상과 벤치마크 도구의 출력을 본다.
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import pathlib
import re
import unittest
from unittest import mock

from oah import scoring
from oah.engines.effort import EngineError
from oah.models import Entry, ModelInfo
from oah.routing import TIERS
from tests.helpers import make_tmp_dir

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_benchmarks", ROOT / "tools" / "build_benchmarks.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

PROFILES = ("performance", "balanced", "efficient")
CREDIT = "Epoch AI, a credit line."
# Prices stay local to the tests, so that neither a price update nor a data refresh can move these results.
KNOWN = {"schema": 1, "prices": {"date": "2026-11-02", "unit": "USD", "source": "vendor pricing pages"}, "engines": {
    "codex": {"identity": r"^gpt-(?P<major>[0-9]{1,3})(?:\.(?P<minor>[0-9]{1,3}))?-(?P<family>[a-z]{2,16})$",
              "families": {"luna": [0.1, 0.5], "terra": [1, 5], "sol": [2, 10], "astra": [10, 50]}},
    "claude": {"aliases": {"haiku": [0.1, 0.5], "sonnet": [2, 10], "opus": [4, 20], "fable": [10, 50]}}}}
TASKS = ("task-a", "task-b", "task-c", "task-d")


def scored(name: str, family: str, score: float, cost: float | None, generation=(6, 0)) -> scoring.Scored:
    return scoring.Scored(name, family, generation, score, cost)


def hand_built(engine: str, *models: scoring.Scored) -> scoring.ScoreSet:
    return scoring.ScoreSet(engine, "Epoch AI", "2026-11-02", CREDIT, tuple(models), {})


CODEX = hand_built("codex", scored("gpt-luna", "luna", 9.7, 0.6), scored("gpt-terra", "terra", 86.9, 6),
                   scored("gpt-sol", "sol", 96.6, 12), scored("gpt-astra", "astra", 96.8, 60))
CLAUDE = hand_built("claude", scored("haiku", "haiku", 9.6, 0.6), scored("fable", "fable", 72.6, 60),
                    scored("sonnet", "sonnet", 72.8, 12), scored("opus", "opus", 90.1, 24))
# The worked table of the brief: (tier, profile) -> (codex family, claude alias), with no floor.
TABLE = {
    ("low", "performance"): ("gpt-terra", "sonnet"), ("low", "balanced"): ("gpt-luna", "haiku"),
    ("low", "efficient"): ("gpt-luna", "haiku"),
    ("medium", "performance"): ("gpt-terra", "sonnet"), ("medium", "balanced"): ("gpt-terra", "sonnet"),
    ("medium", "efficient"): ("gpt-luna", "haiku"),
    ("high", "performance"): ("gpt-astra", "opus"), ("high", "balanced"): ("gpt-terra", "sonnet"),
    ("high", "efficient"): ("gpt-terra", "sonnet"),
    ("max", "performance"): ("gpt-astra", "opus"), ("max", "balanced"): ("gpt-sol", "opus"),
    ("max", "efficient"): ("gpt-terra", "sonnet"),
}


def entry(engine: str, *names: str, hidden: tuple[str, ...] = ()) -> Entry:
    efforts = ("low", "medium", "high")
    models = [ModelInfo(name, efforts) for name in names] + [ModelInfo(name, efforts, listed=False) for name in hidden]
    return Entry(engine, "1.0.0", "2026-11-02T00:00:00Z", tuple(models), "test")


def data(scores: dict) -> dict:
    return {"schema": 1, "source": "Epoch AI", "date": "2026-11-02", "license": "CC BY 4.0",
            "url": "https://epoch.ai/benchmarks/use-this-data", "credit": CREDIT, "scores": scores}


def flat(value: float, tasks=TASKS) -> dict:
    return {task: value for task in tasks}


def codex_set(names, scores, *, allowed=(), hidden=()) -> scoring.ScoreSet:
    return scoring.score_set("codex", entry("codex", *names, hidden=hidden), allowed=allowed, data=data(scores),
                             known=KNOWN)


def claude_set(names, scores, *, allowed=()) -> scoring.ScoreSet:
    return scoring.score_set("claude", entry("claude", *names), allowed=allowed, data=data(scores), known=KNOWN)


def names_of(scores: scoring.ScoreSet) -> set[str]:
    return {model.model for model in scores.models}


class WorkedTableTest(unittest.TestCase):
    def test_the_worked_table(self):
        for (tier, profile), (codex_model, claude_model) in TABLE.items():
            with self.subTest(tier=tier, profile=profile):
                self.assertEqual(scoring.choose(CODEX, tier, "low", profile).model, codex_model)
                self.assertEqual(scoring.choose(CLAUDE, tier, "low", profile).model, claude_model)

    def test_a_costlier_model_with_a_lower_score_is_never_chosen(self):
        # Fable 5.1 scores below Opus 5.5 and costs more: the owner's example, in every cell of the table.
        for tier in TIERS:
            for profile in PROFILES:
                with self.subTest(tier=tier, profile=profile):
                    self.assertNotEqual(scoring.choose(CLAUDE, tier, "low", profile).model, "fable")

    def test_the_choice_follows_the_scores_with_no_table(self):
        # Swap the scores of sol and terra and the same cell moves, with nothing else edited.
        moved = hand_built("codex", scored("gpt-luna", "luna", 9.7, 0.6), scored("gpt-terra", "terra", 40.0, 6),
                           scored("gpt-sol", "sol", 96.6, 12), scored("gpt-astra", "astra", 96.8, 60))
        self.assertEqual(scoring.choose(CODEX, "high", "low", "balanced").model, "gpt-terra")
        self.assertEqual(scoring.choose(moved, "high", "low", "balanced").model, "gpt-sol")

    def test_the_choice_reports_its_steps_and_the_score_it_needed(self):
        choice = scoring.choose(CODEX, "low", "high", "efficient")
        self.assertEqual((choice.step, choice.floor_step, choice.source), (2, 2, "Epoch AI"))
        self.assertAlmostEqual(choice.required, 9.7 + 2 / 3 * (96.8 - 9.7))


class FloorTest(unittest.TestCase):
    def test_a_floor_lifts_the_step_in_every_profile(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                self.assertEqual(scoring.choose(CODEX, "low", "high", profile).step, 2)

    def test_no_profile_goes_below_the_floor(self):
        for tier in TIERS:
            for floor in TIERS:
                for profile in PROFILES:
                    with self.subTest(tier=tier, floor=floor, profile=profile):
                        choice = scoring.choose(CODEX, tier, floor, profile)
                        self.assertGreaterEqual(choice.step, TIERS.index(floor))
                        self.assertTrue(scoring.clears(CODEX, choice.model, TIERS.index(floor)))

    def test_a_model_inside_the_margin_never_passes_as_the_floor(self):
        # Required at step 2 is 66.7. The cheap model sits inside the 2 percent margin below it, so balanced may use it
        # above the floor but a floor has no margin: choose must not pick what the admission check would refuse.
        margin = hand_built("codex", scored("m-low", "luna", 0.0, 1), scored("m-near", "terra", 66.0, 2),
                            scored("m-top", "sol", 100.0, 3))
        self.assertEqual(scoring.choose(margin, "high", "low", "balanced").model, "m-near")
        self.assertFalse(scoring.clears(margin, "m-near", 2))
        self.assertEqual(scoring.choose(margin, "high", "high", "balanced").model, "m-top")

    def test_clears_has_no_margin_and_reads_every_step(self):
        self.assertTrue(scoring.clears(CODEX, "gpt-astra", 3))
        self.assertFalse(scoring.clears(CODEX, "gpt-sol", 3), "the 2 percent margin is not part of a floor")
        self.assertTrue(scoring.clears(CODEX, "gpt-luna", 0))
        self.assertFalse(scoring.clears(CODEX, "gpt-luna", 1))
        self.assertFalse(scoring.clears(CODEX, "gpt-unknown", 0), "no score clears nothing")

    def test_the_top_scorer_always_clears_the_top_step_despite_rounding(self):
        for lowest, highest in ((9.7, 96.8), (0.1, 0.3), (1 / 3, 2 / 3), (72.6, 90.1), (0.0, 99.0)):
            with self.subTest(lowest=lowest, highest=highest):
                pair = hand_built("codex", scored("m-low", "luna", lowest, 1), scored("m-top", "sol", highest, 2))
                self.assertEqual(scoring.required(pair, 3), highest)
                self.assertEqual(scoring.required(pair, 0), lowest)
                self.assertEqual(scoring.choose(pair, "max", "low", "performance").model, "m-top")


class ChoiceOrderTest(unittest.TestCase):
    def test_the_cheapest_clearing_model_wins_and_a_tie_takes_the_lower_score(self):
        tie = hand_built("codex", scored("m-a", "luna", 90.0, 5), scored("m-b", "terra", 80.0, 5),
                         scored("m-c", "sol", 100.0, 9))
        self.assertEqual(scoring.choose(tie, "low", "low", "balanced").model, "m-b")

    def test_an_unknown_cost_loses_to_a_known_one(self):
        unknown = hand_built("codex", scored("m-a", "luna", 90.0, None), scored("m-b", "terra", 95.0, 5))
        self.assertEqual(scoring.choose(unknown, "low", "low", "balanced").model, "m-b")

    def test_equal_models_break_the_tie_by_name_so_a_choice_never_flips(self):
        twins = hand_built("codex", scored("m-b", "terra", 90.0, 5), scored("m-a", "luna", 90.0, 5))
        self.assertEqual(scoring.choose(twins, "low", "low", "balanced").model, "m-a")


class ScoreSetTest(unittest.TestCase):
    ALL = {"gpt-6-luna": flat(0.1), "gpt-6.1-terra": flat(0.8), "gpt-6-sol": flat(0.85), "gpt-6.1-sol": flat(0.9),
           "gpt-6-astra": flat(0.95)}

    def test_every_field_of_a_score_set(self):
        scores = codex_set(("gpt-6-luna", "gpt-6.1-sol"), self.ALL)
        self.assertEqual((scores.engine, scores.source, scores.date, scores.credit),
                         ("codex", "Epoch AI", "2026-11-02", CREDIT))
        best = next(model for model in scores.models if model.model == "gpt-6.1-sol")
        self.assertEqual((best.family, best.generation, best.score, best.cost), ("sol", (6, 1), 100.0, 12))
        self.assertEqual(next(m for m in scores.models if m.model == "gpt-6-luna").score, 0.0)

    def test_a_score_is_the_mean_of_each_common_task_scaled_between_the_candidates(self):
        scores = codex_set(("gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol"),
                           {"gpt-6-luna": {"task-a": 0.2, "task-b": 0.0, "task-c": 0.5},
                            "gpt-6.1-terra": {"task-a": 0.4, "task-b": 0.5, "task-c": 0.5},
                            "gpt-6.1-sol": {"task-a": 1.0, "task-b": 1.0, "task-c": 0.5}})
        by_name = {model.model: model.score for model in scores.models}
        self.assertAlmostEqual(by_name["gpt-6-luna"], 0.0)
        self.assertAlmostEqual(by_name["gpt-6.1-terra"], (25.0 + 50.0 + 0.0) / 3)
        self.assertAlmostEqual(by_name["gpt-6.1-sol"], (100.0 + 100.0 + 0.0) / 3)

    def test_an_older_generation_is_superseded(self):
        scores = codex_set(("gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna"), self.ALL)
        self.assertEqual(scores.left_out, {"gpt-6-sol": "superseded"})
        self.assertEqual(names_of(scores), {"gpt-6.1-sol", "gpt-6-luna"})

    def test_generations_compare_as_numbers_not_text(self):
        scores = codex_set(("gpt-6.9-sol", "gpt-6.10-sol", "gpt-6-luna"),
                           {"gpt-6.9-sol": flat(0.9), "gpt-6.10-sol": flat(0.8), "gpt-6-luna": flat(0.1)})
        self.assertEqual(scores.left_out, {"gpt-6.9-sol": "superseded"})

    def test_a_newer_model_without_a_score_waits_and_the_family_keeps_its_model(self):
        scores = codex_set(("gpt-6.1-sol", "gpt-6.2-sol", "gpt-6-luna"), self.ALL)
        self.assertEqual(scores.left_out, {"gpt-6.2-sol": "unscored"})
        self.assertIn("gpt-6.1-sol", names_of(scores))
        self.assertEqual(scores.stays, {"gpt-6.2-sol": "gpt-6.1-sol"})

    def test_a_model_gone_from_the_entry_is_not_chosen(self):
        scores = codex_set(("gpt-6-sol", "gpt-6-luna"), self.ALL)
        self.assertEqual(names_of(scores), {"gpt-6-sol", "gpt-6-luna"})
        self.assertEqual(scores.left_out, {})

    def test_models_outside_the_known_families_are_never_candidates(self):
        scores = codex_set(("gpt-6-luna", "gpt-6.1-sol", "codex-auto-review", "gpt-reserve", "gpt-7-nova"),
                           {**self.ALL, "gpt-7-nova": flat(1.0), "codex-auto-review": flat(1.0)},
                           hidden=("gpt-6-astra",))
        self.assertEqual(names_of(scores), {"gpt-6-luna", "gpt-6.1-sol"})
        self.assertEqual(scores.left_out, {"codex-auto-review": "unknown-family", "gpt-reserve": "unknown-family",
                                           "gpt-7-nova": "unknown-family"})
        for tier in TIERS:
            for profile in PROFILES:
                self.assertNotIn(scoring.choose(scores, tier, "low", profile).model,
                                 ("codex-auto-review", "gpt-reserve", "gpt-7-nova", "gpt-6-astra"))

    def test_a_model_id_must_match_the_identity_rule_exactly(self):
        for name in ("gpt-6-SOL", "gpt-6.1.2-sol", "gpt-6.1-sol\n", " gpt-6-sol", "gpt-6-s", "gpt-١-sol", "gpt-6-sol-x"):
            with self.subTest(name=name):
                scores = codex_set((name, "gpt-6-luna"), {name: flat(1.0), "gpt-6-luna": flat(0.1)})
                self.assertEqual(scores.left_out, {name: "unknown-family"})

    def test_common_tasks_are_recomputed_over_the_models_that_compete(self):
        # Only the superseded model lacks task-d. It must not take task-d away from the models that remain.
        thin = {task: 0.5 for task in TASKS[:3]}
        scores = codex_set(("gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-terra"),
                           {"gpt-5.6-sol": thin, "gpt-6.1-sol": flat(1.0), "gpt-6-luna": flat(0.0),
                            "gpt-6.1-terra": {**flat(0.5, TASKS[:3]), "task-d": 1.0}})
        self.assertEqual(scores.left_out, {"gpt-5.6-sol": "superseded"})
        by_name = {model.model: model.score for model in scores.models}
        self.assertAlmostEqual(by_name["gpt-6.1-terra"], (50 + 50 + 50 + 100) / 4)

    def test_fewer_than_three_common_tasks_score_nothing(self):
        two = TASKS[:2]
        with self.assertRaises(EngineError) as caught:
            codex_set(("gpt-6-luna", "gpt-6.1-sol"), {"gpt-6-luna": flat(0.1, two), "gpt-6.1-sol": flat(0.9, two)})
        self.assertEqual(caught.exception.code, "OAH_MODEL_NO_SCORES")
        self.assertIn(".oah/config.toml", str(caught.exception))

    def test_one_model_with_a_thin_record_is_unscored_and_the_rest_compete(self):
        scores = codex_set(("gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol"),
                           {"gpt-6-luna": flat(0.1, TASKS[:2]), "gpt-6.1-terra": flat(0.5), "gpt-6.1-sol": flat(0.9)})
        self.assertEqual(scores.left_out, {"gpt-6-luna": "unscored"})
        self.assertEqual(names_of(scores), {"gpt-6.1-terra", "gpt-6.1-sol"})

    def test_weakest_coverage_goes_first_and_a_tie_drops_the_older_generation(self):
        # sol has two generations with no shared task. Coverage ties, so the older one drops and the newer keeps its score.
        scores = codex_set(("gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-luna"),
                           {"gpt-5.6-sol": flat(0.9, ("t1", "t2", "t3")), "gpt-6.1-sol": flat(0.9, ("t4", "t5", "t6")),
                            "gpt-6-luna": flat(0.1, ("t1", "t2", "t3", "t4", "t5", "t6"))})
        self.assertEqual(scores.left_out, {"gpt-5.6-sol": "superseded"})
        self.assertEqual(names_of(scores), {"gpt-6.1-sol", "gpt-6-luna"})

    def test_all_scores_equal(self):
        scores = codex_set(("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"),
                           {"gpt-6-luna": flat(0.5), "gpt-6.1-sol": flat(0.5), "gpt-6-astra": flat(0.5)})
        self.assertEqual({model.score for model in scores.models}, {0.0})
        for tier in TIERS:
            for profile in PROFILES:
                with self.subTest(tier=tier, profile=profile):
                    self.assertEqual(scoring.choose(scores, tier, "low", profile).model, "gpt-6-luna")

    def test_a_one_model_engine(self):
        scores = codex_set(("gpt-6.1-sol",), self.ALL)
        for tier in TIERS:
            for floor in TIERS:
                for profile in PROFILES:
                    self.assertEqual(scoring.choose(scores, tier, floor, profile).model, "gpt-6.1-sol")
        self.assertTrue(scoring.clears(scores, "gpt-6.1-sol", 3))

    def test_allowed_filters_last_and_leaves_the_scale_alone(self):
        wide = codex_set(("gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol"), self.ALL)
        narrow = codex_set(("gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol"), self.ALL,
                           allowed=("gpt-6.1-terra", "gpt-6.1-sol"))
        self.assertEqual(narrow.left_out, {"gpt-6-luna": "not-allowed"})
        self.assertEqual({model.model: model.score for model in narrow.models},
                         {model.model: model.score for model in wide.models if model.model != "gpt-6-luna"})

    def test_nothing_allowed_is_a_named_failure(self):
        with self.assertRaises(EngineError) as caught:
            codex_set(("gpt-6-luna",), self.ALL, allowed=("gpt-6.1-sol",))
        self.assertEqual(caught.exception.code, "OAH_MODEL_NO_SCORES")

    def test_an_empty_or_absent_entry_is_a_named_failure(self):
        for empty in (None, entry("codex")):
            with self.subTest(empty=empty), self.assertRaises(EngineError) as caught:
                scoring.score_set("codex", empty, allowed=(), data=data(self.ALL), known=KNOWN)
            self.assertEqual(caught.exception.code, "OAH_MODEL_NO_SCORES")

    def test_a_left_out_model_keeps_a_score_on_the_same_scale_for_floor_checks_only(self):
        scores = codex_set(("gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra"),
                           {**self.ALL, "gpt-5.6-sol": flat(0.1)}, allowed=("gpt-6.1-sol", "gpt-6-astra"))
        self.assertEqual(scores.left_out, {"gpt-5.6-sol": "superseded", "gpt-6-luna": "not-allowed"})
        self.assertEqual(scoring.score_of(scores, "gpt-5.6-sol"), 0.0)
        self.assertEqual(scoring.score_of(scores, "gpt-6-luna"), 0.0)
        self.assertFalse(scoring.clears(scores, "gpt-5.6-sol", 1))
        self.assertIsNone(scoring.score_of(scores, "gpt-unknown"))
        for tier in TIERS:
            self.assertNotIn(scoring.choose(scores, tier, "low", "balanced").model, ("gpt-5.6-sol", "gpt-6-luna"))


class ClaudeAliasTest(unittest.TestCase):
    SCORES = {"claude-opus-4-8": flat(0.99), "claude-opus-5": flat(0.2), "claude-opus-5-5": flat(0.9),
              "claude-sonnet-5-5": flat(0.7), "claude-fable-5": flat(0.1), "claude-fable-5-1": flat(0.6),
              "claude-haiku-5-5": flat(0.1)}

    def test_an_alias_is_its_family_newest_generation_in_the_data(self):
        scores = claude_set(("opus", "sonnet", "fable", "haiku"), self.SCORES)
        by_name = {model.model: model for model in scores.models}
        self.assertEqual(scores.left_out, {})
        self.assertEqual(by_name["opus"].generation, (5, 5))
        self.assertEqual(by_name["fable"].generation, (5, 1))
        self.assertEqual(by_name["opus"].score, 100.0, "the old opus-4-8 score must not stand in for the alias")
        self.assertEqual(by_name["opus"].cost, 24)
        self.assertEqual(by_name["opus"].family, "opus")

    def test_an_alias_without_any_data_is_unscored(self):
        scores = claude_set(("opus", "sonnet", "haiku"), {k: v for k, v in self.SCORES.items() if "haiku" not in k})
        self.assertEqual(scores.left_out, {"haiku": "unscored"})

    def test_a_full_name_or_unknown_alias_is_outside_the_known_families(self):
        scores = claude_set(("opus", "sonnet", "claude-opus-5-5", "mythos"), self.SCORES)
        self.assertEqual(scores.left_out, {"claude-opus-5-5": "unknown-family", "mythos": "unknown-family"})

    def test_a_dated_data_row_is_not_a_generation(self):
        scores = claude_set(("opus", "sonnet", "haiku"), {**self.SCORES, "claude-opus-4-20250514": flat(1.0)})
        self.assertEqual(next(m for m in scores.models if m.model == "opus").generation, (5, 5))


class FailedSetTest(unittest.TestCase):
    def test_a_failed_read_scores_nothing_and_names_its_cause_only_when_a_choice_needs_it(self):
        failed = scoring.failed("codex", EngineError("OAH_MODEL_NO_SCORES", "the file is damaged; pin one"))
        self.assertEqual((failed.engine, failed.models, failed.left_out), ("codex", (), {}))
        self.assertIsNone(scoring.score_of(failed, "gpt-6.1-sol"))
        self.assertFalse(scoring.clears(failed, "gpt-6.1-sol", 0))
        with self.assertRaises(EngineError) as caught:
            scoring.choose(failed, "high", "low", "balanced")
        self.assertEqual(str(caught.exception), "OAH_MODEL_NO_SCORES: the file is damaged; pin one")


class BandTest(unittest.TestCase):
    SET = hand_built("codex", scored("m-low", "luna", 0.0, 1), scored("m-mid", "terra", 40.0, 2),
                     scored("m-next", "sol", 60.0, 3), scored("m-high", "astra", 90.0, 4),
                     scored("m-top", "nova", 100.0, 5))

    def names(self, tier, floor="low", profile="balanced"):
        return scoring.band(self.SET, scoring.choose(self.SET, tier, floor, profile))

    def test_the_band_runs_from_the_step_to_the_next_one(self):
        self.assertEqual(self.names("medium"), ("m-mid", "m-next"))

    def test_a_model_at_the_next_step_is_offered_so_a_judge_may_move_one_step_up(self):
        self.assertEqual(self.names("high"), ("m-high", "m-top"))

    def test_the_rule_choice_is_always_offered_even_alone(self):
        gap = hand_built("codex", scored("m-low", "luna", 0.0, 1), scored("m-high", "sol", 90.0, 2),
                         scored("m-top", "astra", 100.0, 3))
        self.assertEqual(scoring.band(gap, scoring.choose(gap, "medium", "low", "balanced")), ("m-high",))

    def test_the_top_step_has_no_ceiling(self):
        self.assertEqual(self.names("max"), ("m-top",))
        near = hand_built("codex", scored("m-low", "luna", 0.0, 1), scored("m-near", "sol", 99.0, 2),
                          scored("m-top", "astra", 100.0, 9))
        self.assertEqual(scoring.band(near, scoring.choose(near, "max", "low", "balanced")), ("m-near", "m-top"))

    def test_a_judge_can_never_go_below_the_step(self):
        for tier in TIERS:
            for floor in TIERS:
                for profile in PROFILES:
                    choice = scoring.choose(self.SET, tier, floor, profile)
                    with self.subTest(tier=tier, floor=floor, profile=profile):
                        for name in scoring.band(self.SET, choice):
                            self.assertTrue(name == choice.model or scoring.clears(self.SET, name, choice.step))
                            self.assertTrue(scoring.clears(self.SET, name, choice.floor_step))


class ChangeStepTest(unittest.TestCase):
    def test_each_step_has_one_choice_for_a_profile(self):
        self.assertEqual([scoring.at_step(CODEX, step, "balanced").model for step in range(4)],
                         ["gpt-luna", "gpt-terra", "gpt-terra", "gpt-sol"])
        self.assertEqual(scoring.at_step(CODEX, 3, "performance").model, "gpt-astra")


def fails_with_no_scores(test: unittest.TestCase, reader, content) -> None:
    def packaged(_name):
        if isinstance(content, Exception):
            raise content
        return content

    with mock.patch.object(scoring, "_packaged", side_effect=packaged):
        with test.assertRaises(EngineError) as caught:
            reader()
    test.assertEqual(caught.exception.code, "OAH_MODEL_NO_SCORES")
    test.assertNotIn("Traceback", str(caught.exception))
    test.assertIn(".oah/config.toml", str(caught.exception), "the failure names the one-line pin")


class PackagedDataTest(unittest.TestCase):
    def test_the_shipped_files_are_valid_and_carry_their_credit(self):
        known, bundled = scoring.families(), scoring.bundled()
        self.assertEqual(set(known["engines"]), {"codex", "claude"})
        self.assertEqual(set(known["engines"]["codex"]["families"]), {"luna", "terra", "sol", "astra"})
        self.assertEqual(set(known["engines"]["claude"]["aliases"]), {"haiku", "sonnet", "opus", "fable"})
        self.assertEqual((bundled["source"], bundled["license"]), ("Epoch AI", "CC BY 4.0"))
        self.assertIn("https://epoch.ai/benchmarks/use-this-data", bundled["credit"])
        self.assertTrue(set(tool.TASKS) >= {task for tasks in bundled["scores"].values() for task in tasks},
                        "external tasks keep their own licence and stay out")

    def test_missing_or_damaged_files_are_a_named_failure(self):
        good = json.loads(scoring._packaged("benchmarks.json").decode("utf-8"))
        for content in (FileNotFoundError("gone"), OSError("unreadable"), b"", b"not json", b"[]", b"\xff\xfe",
                        b'{"schema": 1, "source": NaN}', b"[" * 200000,
                        json.dumps({**good, "schema": 2}).encode(), json.dumps({**good, "scores": {}}).encode(),
                        json.dumps({**good, "scores": []}).encode(), json.dumps({**good, "source": ""}).encode(),
                        json.dumps({**good, "date": "soon"}).encode(), json.dumps({**good, "credit": 5}).encode(),
                        json.dumps({**good, "scores": {"gpt-6-sol": {"task a": 2.0}}}).encode(),
                        json.dumps({**good, "scores": {"gpt-6-sol": {"task a": -0.1}}}).encode(),
                        json.dumps({**good, "scores": {"gpt-6-sol": {"task a": True}}}).encode(),
                        json.dumps({**good, "scores": {"gpt-6-sol": {"task a": "1"}}}).encode(),
                        json.dumps({**good, "scores": {"gpt-6-sol": []}}).encode(),
                        json.dumps({**good, "scores": {"gpt-6-sol": {"": 0.5}}}).encode(),
                        json.dumps({**good, "scores": {"../evil": {"task a": 0.5}}}).encode(),
                        b'{"schema": 1, "scores": {"gpt-6-sol": {"task a": 1e999}}}'):
            with self.subTest(content=content if isinstance(content, Exception) else content[:60]):
                fails_with_no_scores(self, scoring.bundled, content)

    def test_a_damaged_families_file_is_a_named_failure(self):
        good = json.loads(scoring._packaged("families.json").decode("utf-8"))

        def edited(**codex):
            copy = json.loads(json.dumps(good))
            copy["engines"]["codex"].update(codex)
            return json.dumps(copy).encode()

        without_claude = json.loads(json.dumps(good))
        del without_claude["engines"]["claude"]
        for content in (FileNotFoundError("gone"), b"{", b"[]", json.dumps({**good, "schema": 2}).encode(),
                        json.dumps({**good, "engines": []}).encode(), json.dumps(without_claude).encode(),
                        edited(identity="("), edited(identity="^gpt-(?P<family>[a-z]+)$"), edited(identity=5),
                        edited(families={}), edited(families=[]), edited(families={"sol": [2]}),
                        edited(families={"sol": [2, "x"]}), edited(families={"sol": [-1, 2]}),
                        edited(families={"sol": [True, 2]}), edited(families={"BAD NAME": [1, 2]}),
                        edited(families={"sol": [1e999, 2]})):
            with self.subTest(content=content if isinstance(content, Exception) else content[-60:]):
                fails_with_no_scores(self, scoring.families, content)

    def test_score_set_passes_on_a_failed_read(self):
        with mock.patch.object(scoring, "_packaged", side_effect=FileNotFoundError("gone")):
            with self.assertRaises(EngineError) as caught:
                scoring.score_set("codex", entry("codex", "gpt-6.1-sol"), allowed=())
        self.assertEqual(caught.exception.code, "OAH_MODEL_NO_SCORES")

    def test_the_files_are_read_as_bytes_so_the_locale_never_decodes_them(self):
        calls = []
        real = scoring._packaged

        def spy(name):
            calls.append(name)
            return real(name)

        with mock.patch.object(scoring, "_packaged", side_effect=spy):
            scoring.families()
            scoring.bundled()
        self.assertEqual(calls, ["families.json", "benchmarks.json"])
        self.assertIsInstance(real("families.json"), bytes)


HEADER = ["id_runs", "model", "Notes", "started_at", "Status", "mean_score", "original_task_name"]
TASK_A, TASK_B = "Chess Puzzles", "EBR-bench"


def rows_csv(*rows: tuple, newline: str = "\n") -> bytes:
    def field(value: str) -> str:
        return '"' + value.replace('"', '""') + '"' if any(mark in value for mark in ('"', ",", "\n")) else value

    return newline.join(",".join(field(value) for value in row) for row in (tuple(HEADER), *rows)).encode("utf-8") + \
        newline.encode("utf-8")


SAMPLE = rows_csv(
    ("1", "gpt-6.1-sol_max", "a note\nover two lines, with a comma", "2026-10-01T00:00:00.000Z", "Success", "0.5", TASK_A),
    # The later row wins even when it comes first in the file; a failed run and a blank mean never count.
    ("2", "gpt-6.1-sol_max", "older", "2026-09-01T00:00:00.000Z", "Success", "0.25", TASK_B),
    ("3", "gpt-6.1-sol_max", "newer", "2026-09-02T00:00:00.000Z", "Success", "0.75", TASK_B),
    ("4", "gpt-6.1-sol_max", "failed", "2026-09-03T00:00:00.000Z", "Error", "0.99", TASK_B),
    ("5", "gpt-6.1-sol_max", "blank", "2026-09-04T00:00:00.000Z", "Success", "", TASK_B),
    ("6", "gpt-6.1-sol_max", "not finite", "2026-09-05T00:00:00.000Z", "Success", "nan", TASK_B),
    ("7", "gpt-6.1-sol_max", "infinite", "2026-09-06T00:00:00.000Z", "Success", "inf", TASK_B),
    ("8", "claude-opus-5-5_max", "line one\r\nline two", "2026-10-01T00:00:00.000Z", "Success", "1", TASK_A),
    # Not an allow-listed task, not at max effort, or not a model the pattern knows: all stay out.
    ("9", "claude-opus-5-5_max", "external", "2026-10-01T00:00:00.000Z", "Success", "0.5", "SWE-Bench verified"),
    ("10", "gpt-6.1-sol_high", "other effort", "2026-10-01T00:00:00.000Z", "Success", "0.5", TASK_A),
    ("11", "gemini-3_max", "other vendor", "2026-10-01T00:00:00.000Z", "Success", "0.5", TASK_A))
SNAPSHOT = (b'{\n  "schema": 1,\n  "source": "Epoch AI",\n  "date": "2026-11-02",\n  "license": "CC BY 4.0",\n'
            b'  "url": "https://epoch.ai/benchmarks/use-this-data",\n'
            b'  "credit": "Epoch AI, \'Capabilities & benchmarking\'. Published online at epoch.ai. Retrieved from '
            b'\'https://epoch.ai/benchmarks/use-this-data\'. Accessed 2 Nov 2026.",\n'
            b'  "scores": {\n    "claude-opus-5-5": {\n      "Chess Puzzles": 1.0\n    },\n'
            b'    "gpt-6.1-sol": {\n      "Chess Puzzles": 0.5,\n      "EBR-bench": 0.75\n    }\n  }\n}\n')


class BuildToolTest(unittest.TestCase):
    def build(self, content: bytes, *extra: str) -> tuple[int, pathlib.Path, str]:
        work = make_tmp_dir(self)
        source, output = work / "epoch.csv", work / "out" / "benchmarks.json"
        source.write_bytes(content)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tool.main([str(source), "--date", "2026-11-02", "--output", str(output), *extra])
        return code, output, err.getvalue()

    def test_the_output_bytes(self):
        code, output, err = self.build(SAMPLE)
        self.assertEqual(code, 0, err)
        self.assertEqual(output.read_bytes(), SNAPSHOT)

    def test_windows_line_endings_give_the_same_bytes(self):
        code, output, err = self.build(rows_csv(*self._rows(), newline="\r\n"))
        self.assertEqual(code, 0, err)
        self.assertEqual(output.read_bytes(), SNAPSHOT)

    def _rows(self):
        import csv

        rows = list(csv.reader(io.StringIO(SAMPLE.decode("utf-8"), newline="")))
        return [tuple(row) for row in rows[1:]]

    def test_the_output_passes_the_runtime_validation(self):
        code, output, err = self.build(SAMPLE)
        self.assertEqual(code, 0, err)
        with mock.patch.object(scoring, "_packaged", return_value=output.read_bytes()):
            self.assertEqual(sorted(scoring.bundled()["scores"]), ["claude-opus-5-5", "gpt-6.1-sol"])

    def test_the_allow_list_is_the_contract_and_nothing_else(self):
        self.assertEqual(tool.TASKS, ("FrontierMath-Tiers-1-3-v2-Private", "FrontierMath-Tier-4-v2-Private",
                                      "Mystery Game Puzzles", "Furniture Assembly", "Chess Puzzles", "EBR-bench",
                                      "OTIS Mock AIME 2024-2025"))

    def test_a_score_outside_the_scale_stops_the_build(self):
        code, output, err = self.build(rows_csv(
            ("1", "gpt-6.1-sol_max", "", "2026-10-01T00:00:00.000Z", "Success", "87.5", TASK_A)))
        self.assertEqual(code, 1)
        self.assertIn("87.5", err)
        self.assertFalse(output.exists())

    def test_a_file_without_the_columns_stops_the_build(self):
        code, output, err = self.build(b"a,b\n1,2\n")
        self.assertEqual(code, 1)
        self.assertIn("original_task_name", err)
        self.assertFalse(output.exists())

    def test_a_missing_file_and_a_file_with_no_rows_stop_the_build(self):
        work = make_tmp_dir(self)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tool.main([str(work / "none.csv"), "--date", "2026-11-02", "--output", str(work / "x.json")])
        self.assertEqual(code, 1)
        self.assertIn("none.csv", err.getvalue())
        code, output, err_text = self.build(rows_csv(("1", "gemini-3_max", "", "2026-10-01T00:00:00Z", "Success", "1", TASK_A)))
        self.assertEqual(code, 1)
        self.assertIn("no rows", err_text)
        self.assertFalse(output.exists())

    def test_an_invalid_date_stops_the_build(self):
        work = make_tmp_dir(self)
        source = work / "epoch.csv"
        source.write_bytes(SAMPLE)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tool.main([str(source), "--date", "2 Nov", "--output", str(work / "x.json")])
        self.assertEqual(code, 1)
        self.assertIn("YYYY-MM-DD", err.getvalue())

    def test_the_model_pattern_and_the_mean_parser(self):
        self.assertIsNone(re.compile(tool.MODEL).fullmatch("gpt-6-sol_max\n"))
        self.assertTrue(math.isfinite(tool.parse_mean("0.5")))
        self.assertIsNone(tool.parse_mean("nan"))
        self.assertIsNone(tool.parse_mean(""))
