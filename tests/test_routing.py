# 라우팅 검사 — 난이도 점수표·기본 단·끌어올림·종류 하한·규칙 기반 축 점수·단계 엔진 강제·oah recommend 출력을 본다.
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import secrets as token_source
import unittest
from unittest import mock

from oah import recommend_cli, rerank
from oah.engines.effort import EngineError
from oah.paths import NotARepo
from oah.secrets import NoKeychain
from oah.routing import (INTENSITY_SCORES, KIND_SCORES, REASONING_SCORES, RISK_SCORES, Axes, RoutingError,
                         check_stage, floor_tier, recommend, review_floor, same_engine_warning, score_axes)

from tests.helpers import make_git_repo, make_tmp_dir

STAGES = {"initial": "claude", "review": "codex", "correction": "codex"}

# (kind, intensity, reasoning, risk) -> tier, plus the reason that must appear (one row per rule).
TABLE = [
    # Base tier rules.
    (("quick", "low", "low", "critical"), "max", "base max: risk critical"),
    (("quick", "low", "max", "low"), "max", "base max: reasoning max"),
    (("adversarial-review", "high", "xhigh", "low"), "max", "base max: score 8 >= 8"),
    (("quick", "low", "low", "high"), "high", "base high: risk high"),
    (("quick", "max", "low", "low"), "max", "base high: intensity max"),
    (("migration", "high", "medium", "low"), "high", "base high: score 6 >= 6"),
    (("quick", "high", "low", "low"), "high", "base medium: intensity high"),
    (("quick", "low", "high", "low"), "high", "base medium: reasoning high"),
    (("quick", "low", "xhigh", "low"), "high", "base medium: reasoning xhigh"),
    (("quick", "medium", "medium", "low"), "medium", "base medium: score 3 >= 3"),
    (("quick", "low", "low", "low"), "low", "base low"),
    (("docs", "low", "medium", "normal"), "low", "base low"),
    # Lifts.
    (("docs", "medium", "low", "low"), "medium", "lift medium: intensity medium"),
    (("docs", "high", "low", "low"), "high", "lift high: intensity high"),
    (("docs", "max", "low", "low"), "max", "lift max: intensity max"),
    (("docs", "low", "high", "low"), "high", "lift high: reasoning high"),
    (("docs", "low", "xhigh", "low"), "high", "lift high: reasoning xhigh"),
    (("docs", "low", "max", "low"), "max", "lift max: reasoning max"),
    # Kind floors, applied last.
    (("broad-audit", "low", "low", "low"), "high", "floor high: kind broad-audit"),
    (("migration", "low", "low", "low"), "high", "floor high: kind migration"),
    (("security", "low", "low", "low"), "high", "floor high: kind security"),
    (("high-risk-review", "low", "low", "low"), "high", "floor high: kind high-risk-review"),
    (("research", "low", "low", "low"), "medium", "floor medium: kind research"),
    (("review", "low", "low", "low"), "medium", "floor medium: kind review"),
    (("implementation", "low", "low", "low"), "medium", "floor medium: kind implementation"),
    (("refactor", "low", "low", "low"), "medium", "floor medium: kind refactor"),
    (("adversarial-review", "low", "low", "low"), "medium", "base medium: score 3 >= 3"),
    # A floor never lowers a higher tier.
    (("research", "low", "low", "critical"), "max", "base max: risk critical"),
]


class ScoringTableTest(unittest.TestCase):
    def test_scoring_table_covers_every_rule(self):
        for axes, tier, reason in TABLE:
            with self.subTest(axes=axes):
                result = recommend(Axes(*axes))
                self.assertEqual(result.tier, tier)
                self.assertIn(reason, result.reasons)

    def test_every_rule_reason_is_covered(self):
        reasons = {reason for _, _, reason in TABLE}
        for name in ("risk critical", "reasoning max", "score 8 >= 8"):
            self.assertIn(f"base max: {name}", reasons)
        for name in ("risk high", "intensity max", "score 6 >= 6"):
            self.assertIn(f"base high: {name}", reasons)
        for name in ("intensity high", "reasoning high", "reasoning xhigh", "score 3 >= 3"):
            self.assertIn(f"base medium: {name}", reasons)
        for kind in ("broad-audit", "migration", "security", "high-risk-review"):
            self.assertIn(f"floor high: kind {kind}", reasons)
        for kind in ("research", "review", "implementation", "refactor"):
            self.assertIn(f"floor medium: kind {kind}", reasons)

    def test_every_axis_value_scores(self):
        self.assertEqual(KIND_SCORES, {
            "quick": 1, "docs": 1, "research": 1,
            "review": 2, "implementation": 2, "refactor": 2, "broad-audit": 2, "packet": 2,
            "adversarial-review": 3, "migration": 3, "security": 3, "high-risk-review": 3, "design": 3})
        self.assertEqual(INTENSITY_SCORES, {"low": 0, "medium": 1, "high": 2, "max": 3})
        self.assertEqual(REASONING_SCORES, {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4})
        self.assertEqual(RISK_SCORES, {"low": 0, "normal": 0, "high": 2, "critical": 4})
        result = recommend(Axes("security", "medium", "medium", "high"))
        self.assertEqual(result.score, 3 + 1 + 1 + 2)

    def test_invalid_value_refuses(self):
        for axes in (("nope", "low", "low", "low"), ("quick", "huge", "low", "low"),
                     ("quick", "low", "deep", "low"), ("quick", "low", "low", "extreme")):
            with self.subTest(axes=axes), self.assertRaises(RoutingError) as ctx:
                recommend(Axes(*axes))
            self.assertEqual(ctx.exception.code, "OAH_RECOMMEND_INVALID")
            self.assertIn("use one of", str(ctx.exception))


class RuleAxesTest(unittest.TestCase):
    def test_rules_score_axes(self):
        docs = score_axes(stage="initial", kind=None, scope=["README.md", "docs/guide.md"])
        self.assertEqual(docs.axes, Axes("implementation", "low", "low", "normal"))
        review = score_axes(stage="review", kind=None, scope=["src/a.py"])
        self.assertEqual(review.axes, Axes("review", "low", "medium", "normal"))
        correction = score_axes(stage="correction", kind=None, scope=[f"src/m{i}.py" for i in range(5)])
        self.assertEqual(correction.axes, Axes("implementation", "medium", "medium", "normal"))
        large = score_axes(stage="initial", kind="refactor", scope=[f"src/m{i}.py" for i in range(13)])
        self.assertEqual(large.axes, Axes("refactor", "high", "medium", "normal"))
        whole = score_axes(stage="initial", kind=None, scope=["repo"])
        self.assertEqual(whole.axes, Axes("implementation", "high", "medium", "normal"))

    def test_risk_paths_raise_risk_and_reasoning(self):
        for path in (".github/workflows/ci.yml", "db/migrations/001_init.sql", "schema.sql",
                     "src/auth/login.py", "src/oauth.py", "app/security/policy.ts", "billing/invoice.py"):
            with self.subTest(path=path):
                result = score_axes(stage="initial", kind=None, scope=[path])
                self.assertEqual(result.axes.risk, "high")
                self.assertEqual(result.axes.reasoning, "high")
                self.assertTrue(any(path in reason for reason in result.reasons))
        plain = score_axes(stage="initial", kind=None, scope=["src/author.py", "docs/authoring.md"])
        self.assertEqual(plain.axes.risk, "normal")

    def test_backslash_and_crlf_free_paths(self):
        result = score_axes(stage="initial", kind=None, scope=[".github\\workflows\\ci.yml"])
        self.assertEqual(result.axes.risk, "high")

    def test_overrides_win(self):
        result = score_axes(stage="initial", kind=None, scope=["src/a.py"],
                            overrides={"risk": "critical", "intensity": "max"})
        self.assertEqual(result.axes, Axes("implementation", "max", "medium", "critical"))

    def test_empty_scope_warns(self):
        result = score_axes(stage="initial", kind=None, scope=[])
        self.assertEqual(result.axes, Axes("implementation", "low", "medium", "normal"))
        self.assertTrue(result.warnings)
        self.assertTrue(result.warnings[0].startswith("OAH_SCOPE_EMPTY:"))

    def test_unknown_stage_refuses(self):
        with self.assertRaises(RoutingError) as ctx:
            score_axes(stage="deploy", kind=None, scope=["a.py"])
        self.assertEqual(ctx.exception.code, "OAH_STAGE_UNKNOWN")


class StageTest(unittest.TestCase):
    def test_stage_mismatch_refuses(self):
        with self.assertRaises(RoutingError) as ctx:
            check_stage("claude", "review", STAGES, override=False)
        self.assertEqual(ctx.exception.code, "OAH_STAGE_ENGINE_MISMATCH")
        self.assertIn("--override-stage", str(ctx.exception))
        self.assertEqual(check_stage("codex", "review", STAGES, override=False), [])

    def test_override_stage_warns(self):
        warnings = check_stage("claude", "review", STAGES, override=True)
        self.assertTrue(warnings[0].startswith("OAH_STAGE_OVERRIDDEN:"))

    def test_unknown_stage_or_engine(self):
        with self.assertRaises(RoutingError) as ctx:
            check_stage("codex", "deploy", STAGES, override=False)
        self.assertEqual(ctx.exception.code, "OAH_STAGE_UNKNOWN")
        with self.assertRaises(RoutingError) as ctx:
            check_stage("gemini", "review", STAGES, override=True)
        self.assertEqual(ctx.exception.code, "OAH_ENGINE_UNKNOWN")

    def test_same_engine_warning(self):
        self.assertIsNone(same_engine_warning(STAGES))
        warning = same_engine_warning({"initial": "codex", "review": "codex", "correction": "codex"})
        self.assertTrue(warning.startswith("OAH_REVIEW_NOT_INDEPENDENT:"))
        self.assertIn("not independent", warning)


def run_recommend(*argv: str) -> tuple[int, str, str]:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    recommend_cli.register(commands)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = recommend_cli.run(parser.parse_args(["recommend", *argv]))
    return code, out.getvalue(), err.getvalue()


class RecommendCliTest(unittest.TestCase):
    def test_recommend_cli_json(self):
        code, out, _ = run_recommend("--kind", "security", "--intensity", "low", "--reasoning", "low",
                                     "--risk", "low", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["tier"], "high")
        self.assertEqual(data["axes"], {"kind": "security", "intensity": "low", "reasoning": "low", "risk": "low"})
        self.assertIn("floor high: kind security", data["reasons"])

    def test_recommend_cli_text(self):
        code, out, _ = run_recommend("--kind", "quick", "--intensity", "low", "--reasoning", "low",
                                     "--risk", "low")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OAH_RECOMMEND tier=low score=1"))

    def test_design_without_a_request_is_max_and_packet_keeps_its_rule_tier(self):
        for kind, tier in (("design", "max"), ("packet", "medium")):
            for flags in ((), ("--json",)):
                with self.subTest(kind=kind, flags=flags):
                    forbidden = AssertionError("without a request this path must stay offline")
                    with mock.patch.object(rerank, "advise_difficulty", side_effect=forbidden) as judge, \
                            mock.patch("oah.paths.repo_info", side_effect=forbidden) as repo:
                        code, out, err = run_recommend("--kind", kind, "--intensity", "low", "--reasoning", "low",
                                                       "--risk", "low", *flags)
                    self.assertEqual((code, err), (0, ""))
                    self.assertEqual((judge.call_count, repo.call_count), (0, 0))
                    if flags:
                        data = json.loads(out)
                        self.assertEqual(data["tier"], tier)
                        if kind == "design":
                            self.assertEqual(data["reason"], "design unjudged: unused(no-request)")
                            self.assertEqual(data["jev"]["status"], "unused(no-request)")
                        else:
                            self.assertNotIn("jev", data)
                    else:
                        self.assertTrue(out.startswith(f"OAH_RECOMMEND tier={tier}"), out)
                        if kind == "design":
                            self.assertIn("design unjudged: unused(no-request)", out)

    def test_recommend_cli_rejects_bad_values(self):
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command")
        recommend_cli.register(commands)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["recommend", "--kind", "nope", "--intensity", "low", "--reasoning", "low",
                               "--risk", "low"])
        self.assertEqual(ctx.exception.code, 2)


def recommend_in(project, *argv: str) -> tuple[int, str, str]:
    previous = os.getcwd()
    os.chdir(project.repo)
    try:
        with mock.patch.dict(os.environ, project.env, clear=True):
            return run_recommend(*argv)
    finally:
        os.chdir(previous)


AXES = ("--kind", "review", "--intensity", "low", "--reasoning", "low", "--risk", "low")
QUICK = ("--kind", "quick", "--intensity", "low", "--reasoning", "low", "--risk", "low")


def tree(folder: pathlib.Path) -> dict:
    return {path: path.read_bytes() if path.is_file() else None for path in sorted(folder.rglob("*"))}


class RecommendModelTest(unittest.TestCase):
    def project(self, config: str = "", **fields):
        from tests import test_call as calls

        return calls.scored_project(self, config, **fields)

    def test_it_prints_the_model_effort_source_and_floor(self):
        code, out, err = recommend_in(self.project(), *AXES, "--engine", "codex", "--stage", "review")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("OAH_RECOMMEND tier=medium"), out)
        line = next(item for item in out.splitlines() if item.startswith("OAH_RECOMMEND_MODEL"))
        for text in ("engine=codex", "stage=review", "tier=medium", "model=gpt-6.1-terra", "effort=medium",
                     "source=scored", "floor=medium", "profile=balanced", "scores=Epoch AI 2026-11-02"):
            self.assertIn(text, line)
        self.assertIn("Epoch AI, a credit line.", out)

    def test_json_carries_the_same(self):
        code, out, err = recommend_in(self.project(), *AXES, "--engine", "codex", "--stage", "review", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["tier"], "medium")
        self.assertEqual(data["model"], {
            "engine": "codex", "stage": "review", "tier": "medium", "model": "gpt-6.1-terra", "effort": "medium",
            "source": "scored", "floor_tier": "medium", "floor_checked": True, "family": "terra", "profile": "balanced",
            "score_source": "Epoch AI", "score_date": "2026-11-02", "score_credit": "Epoch AI, a credit line."})

    def test_low_axes_are_lifted_like_a_real_review(self):
        code, out, err = recommend_in(self.project(), *QUICK, "--engine", "codex", "--stage", "review", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["tier"], data["model"]["tier"], data["model"]["floor_tier"]), ("low", "medium", "medium"))

    def test_the_engine_alone_uses_the_kind_floor(self):
        code, out, err = recommend_in(self.project(), *QUICK, "--engine", "codex", "--json")
        self.assertEqual(code, 0, err)
        model = json.loads(out)["model"]
        self.assertEqual((model["stage"], model["floor_tier"], model["model"]), (None, "low", "gpt-6-luna"))

    def test_the_stage_alone_finds_its_engine(self):
        code, out, err = recommend_in(self.project(), *AXES, "--stage", "review", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["model"]["engine"], "codex")

    def test_a_new_kind_gets_a_model_too(self):
        project = self.project()
        for kind, floor, tier in (("design", "high", "max"), ("packet", "medium", "medium")):
            for preview in (("--engine", "codex"), ("--stage", "review"),
                            ("--engine", "codex", "--stage", "review")):
                for flags in ((), ("--json",)):
                    with self.subTest(kind=kind, preview=preview, flags=flags):
                        forbidden = AssertionError("a model preview without a request must not ask Jev")
                        with mock.patch.object(rerank, "advise_difficulty", side_effect=forbidden) as judge:
                            code, out, err = recommend_in(project, "--kind", kind, "--intensity", "low",
                                                          "--reasoning", "low", "--risk", "low", *preview, *flags)
                        self.assertEqual(code, 0, err)
                        self.assertEqual(judge.call_count, 0)
                        if flags:
                            data = json.loads(out)
                            self.assertEqual((data["tier"], data["model"]["tier"], data["model"]["floor_tier"]),
                                             (tier, tier, floor))
                            if kind == "design":
                                self.assertEqual(data["reason"], "design unjudged: unused(no-request)")
                        else:
                            self.assertTrue(out.startswith(f"OAH_RECOMMEND tier={tier}"), out)
                            model = next(line for line in out.splitlines() if line.startswith("OAH_RECOMMEND_MODEL"))
                            self.assertIn(f"tier={tier}", model)
                            self.assertIn(f"floor={floor}", model)
                            if kind == "design":
                                self.assertIn("design unjudged: unused(no-request)", out)

    def test_it_refuses_exactly_what_a_call_would_refuse(self):
        project = self.project('[engines.codex.tiers.medium]\nmodel = "gpt-6-luna"\n')
        code, out, err = recommend_in(project, *AXES, "--engine", "codex", "--stage", "review")
        self.assertEqual(code, 1, err)
        self.assertIn("OAH_MODEL_BELOW_FLOOR", err)
        with self.assertRaises(EngineError) as caught:
            project.prepare(stage="review")
        self.assertEqual(caught.exception.code, "OAH_MODEL_BELOW_FLOOR")

    def test_a_floor_no_allowed_model_reaches_refuses_like_a_call(self):
        project = self.project('allowed_models = ["gpt-6-luna"]\n')
        code, out, err = recommend_in(project, *AXES, "--engine", "codex", "--stage", "review")
        self.assertEqual(code, 1, err)
        self.assertIn("OAH_MODEL_BELOW_FLOOR", err)
        self.assertIn("allowed_models", err)
        code, out, err = recommend_in(project, "--kind", "security", "--intensity", "low", "--reasoning", "low",
                                      "--risk", "low", "--engine", "codex")
        self.assertEqual(code, 1, err)
        self.assertIn("OAH_MODEL_UNRESOLVED", err)

    def test_it_writes_nothing_to_a_state_folder_that_does_not_exist(self):
        project = self.project()
        code, out, err = recommend_in(project, *AXES, "--engine", "codex", "--stage", "review")
        self.assertEqual(code, 0, err)
        self.assertFalse(project.state.exists(), "not even the folder")
        self.assertFalse(project.record.exists(), "it launches nothing")

    def test_it_writes_nothing_over_a_stale_catalog(self):
        from tests import test_call as calls

        project = self.project()
        project.prepare(stage="correction", tier="high")
        before = tree(project.state)
        calls.write_models(project.home, ("gpt-6-luna", "gpt-6.1-sol"))
        project.env["FAKE_CODEX_VERSION"] = "0.164.0"
        for flags in (("--engine", "codex"), ("--engine", "codex", "--stage", "review"), ("--stage", "correction")):
            with self.subTest(flags=flags):
                code, out, err = recommend_in(project, *AXES, *flags)
                self.assertEqual(code, 0, err)
                self.assertEqual(tree(project.state), before)
                self.assertNotIn("OAH_MODEL_CHANGED", out + err)

    def test_it_uses_the_list_the_cli_reports_now(self):
        from tests import test_call as calls

        project = self.project()
        project.prepare(stage="correction", tier="high")
        calls.write_models(project.home, ("gpt-6-luna", "gpt-6.1-sol"))
        project.env["FAKE_CODEX_VERSION"] = "0.164.0"
        code, out, err = recommend_in(project, *AXES, "--engine", "codex", "--stage", "review", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["model"]["model"], "gpt-6.1-sol")

    def test_a_missing_repository_is_a_message_not_a_traceback(self):
        missing = NotARepo("OAH_NOT_A_REPO: this folder is not inside a git repository")
        with mock.patch("oah.paths.repo_info", side_effect=missing):
            code, out, err = run_recommend(*AXES, "--engine", "codex")
        self.assertEqual(code, 1)
        self.assertIn("OAH_NOT_A_REPO", err)

    def test_axes_alone_still_need_no_repository_and_read_nothing(self):
        folder = make_tmp_dir(self)
        previous = os.getcwd()
        os.chdir(folder)
        try:
            with mock.patch("oah.paths.repo_info", side_effect=AssertionError("no repository needed")):
                code, out, err = run_recommend(*AXES)
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, err)
        self.assertNotIn("OAH_RECOMMEND_MODEL", out)


class FloorTierTest(unittest.TestCase):
    def test_floor_tier_by_kind(self):
        for kind in KIND_SCORES:
            with self.subTest(kind=kind):
                expected = recommend(Axes(kind, "low", "low", "low")).tier
                self.assertEqual(floor_tier(kind), expected)
        self.assertEqual(floor_tier("migration"), "high")
        self.assertEqual(floor_tier("review"), "medium")
        self.assertEqual(floor_tier("quick"), "low")



class ReviewFloorTest(unittest.TestCase):
    def test_medium_for_an_ordinary_scope(self):
        for scope in ((), ("src/app.py",), ("docs/a.md", "README.md"), ("repo",)):
            with self.subTest(scope=scope):
                self.assertEqual(review_floor(scope, "review"), "medium")

    def test_a_risky_path_lifts_the_floor_to_high_with_no_flag(self):
        for path in ("src/auth/login.py", ".github/workflows/ci.yml", "db/migrations/001.py", "schema.sql",
                     "src/billing.py"):
            with self.subTest(path=path):
                self.assertEqual(review_floor(("src/app.py", path), "review"), "high")

    def test_a_kind_can_raise_the_floor_but_never_lower_it(self):
        self.assertEqual(review_floor(("src/app.py",), "quick"), "medium")
        self.assertEqual(review_floor(("src/auth/login.py",), "quick"), "high")
        self.assertEqual(review_floor(("src/app.py",), "security"), "high")
        self.assertEqual(review_floor(("src/app.py",), "high-risk-review"), "high")

    def test_a_packet_scores_like_implementation(self):
        self.assertEqual(KIND_SCORES["packet"], KIND_SCORES["implementation"])
        self.assertEqual(floor_tier("packet"), "medium")
        self.assertEqual(recommend(Axes("packet", "low", "low", "normal")).tier, "medium")

    def test_a_design_is_high_leverage(self):
        self.assertEqual(KIND_SCORES["design"], KIND_SCORES["migration"])
        self.assertEqual(floor_tier("design"), "high")
        result = recommend(Axes("design", "low", "low", "normal"))
        self.assertEqual((result.tier, result.base), ("high", "medium"))
        self.assertIn("floor high: kind design", result.reasons)


class RecommendRequestTest(unittest.TestCase):
    """`oah recommend --request-file` asks Jev about design and packet work before an Intent exists."""

    JEV_ON = '[rerank]\nenabled = true\nprovider = "fake"\n'

    def setUp(self):
        from tests.test_call import Judge

        self.repo = make_git_repo(self)
        work = make_tmp_dir(self)
        self.state = work / "state"
        self.request = work / "request.md"
        self.canary = "Canary request " + token_source.token_hex(8)
        self.request.write_text(f"{self.canary}: add a dark mode to the settings page.\n", encoding="utf-8")
        self.key = "fake-" + token_source.token_hex(16)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("OAH_", "FAKE_"))}
        self.env.update(OAH_STATE_DIR=str(self.state), FAKE_RERANK_KEY=self.key, HOME=str(work / "home"),
                        USERPROFILE=str(work / "home"))
        self.judge = Judge(("max", 0.9))
        self.configure(self.JEV_ON)

    def configure(self, text: str) -> None:
        (self.repo / ".oah").mkdir(exist_ok=True)
        (self.repo / ".oah" / "config.toml").write_text(text, encoding="utf-8")

    def ask(self, *argv: str) -> tuple[int, str, str]:
        previous = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, self.env, clear=True), \
                    mock.patch.dict(rerank.PROVIDERS, {"fake": self.judge}), \
                    mock.patch("oah.secrets.system_keychain", return_value=NoKeychain()):
                return run_recommend(*argv)
        finally:
            os.chdir(previous)

    def design(self, *extra: str) -> tuple[int, str, str]:
        return self.ask("--kind", "design", "--task", "demo", "--request-file", str(self.request), *extra)

    def record(self, kind: str = "design") -> dict:
        return json.loads((self.state / "difficulty" / "demo" / f"{kind}.json").read_text(encoding="utf-8"))

    def test_design_used_runs_at_jev_tier_never_below_high(self):
        for answer, tier in ((("max", 0.9), "max"), (("medium", 0.9), "high")):
            with self.subTest(answer=answer):
                self.judge.answers = [answer]
                code, out, err = self.design("--json")
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                self.assertEqual((data["tier"], data["rule_tier"], data["jev"]["status"], data["jev"]["tier"],
                                  data["jev"]["confidence"], data["tier_source"]),
                                 (tier, "high", "used", answer[0], 0.9, "jev"))
                self.assertEqual(self.record()["tier"], tier)
        self.assertEqual(self.judge.sent[0][1], f"### Request\n{self.canary}: add a dark mode to the settings page.")

    def test_scope_paths_follow_the_request_and_the_text_output_says_why(self):
        code, out, err = self.design("--scope", "src/settings.py", "--scope", "docs/settings.md")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.judge.sent[0][1].endswith("### Scope\nsrc/settings.py\ndocs/settings.md"))
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("OAH_RECOMMEND tier=max rule_tier=high jev=used"), first)
        self.assertIn("\n  jev max at confidence 0.9\n", out)
        self.assertIn("OAH_RERANK_SCOPE_WIDENED", err)

    def test_unjudged_design_runs_at_max(self):
        secret = "s" + "k-" + token_source.token_hex(16)
        cases = (("no key", lambda: self.env.pop("FAKE_RERANK_KEY"), "unused(no key)"),
                 ("off", lambda: self.configure('[rerank]\nenabled = false\nprovider = "fake"\n'),
                  "unused(not-enabled)"),
                 ("secret", lambda: self.request.write_text(f"Rotate {secret} tonight.\n", encoding="utf-8"),
                  "unused(secret)"),
                 ("below threshold", lambda: setattr(self.judge, "answers", [("medium", 0.3)]),
                  "below-threshold 0.3"))
        for name, change, status in cases:
            with self.subTest(case=name):
                self.setUp()
                change()
                code, out, err = self.design("--json")
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                self.assertEqual((data["tier"], data["rule_tier"], data["jev"]["status"], data["tier_source"]),
                                 ("max", "high", status, "rules"))
                self.assertEqual(data["reason"], f"design unjudged: {status}")
                self.assertEqual((self.record()["tier"], self.record()["jev_status"]), ("max", status))
                self.assertNotIn(secret, out + err)
                if name == "secret":
                    self.assertEqual(self.judge.sent, [])
                    self.assertIn("OAH_RERANK_SECRET", err)
                    self.assertIn("the request text", err)

    def test_unjudged_packet_keeps_the_rule_tier(self):
        self.env.pop("FAKE_RERANK_KEY")
        code, out, err = self.ask("--kind", "packet", "--task", "demo", "--request-file", str(self.request), "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["tier"], data["rule_tier"], data["jev"]["status"]), ("medium", "medium", "unused(no key)"))
        self.assertEqual(self.record("packet")["tier"], "medium")
        self.judge.answers = [("high", 0.9)]
        self.env["FAKE_RERANK_KEY"] = self.key
        code, out, err = self.ask("--kind", "packet", "--task", "demo", "--request-file", str(self.request), "--json")
        self.assertEqual(json.loads(out)["tier"], "high")

    def test_the_record_holds_no_request_text_scope_or_key(self):
        code, out, err = self.design("--scope", "src/secret-plan.py")
        self.assertEqual(code, 0, err)
        record = self.record()
        self.assertEqual(set(record), {"kind", "rule_tier", "jev_tier", "jev_confidence", "jev_status", "tier", "at",
                                       "oah_version"})
        self.assertEqual((record["kind"], record["jev_status"], record["tier"]), ("design", "used", "max"))
        for path in self.state.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                for needle in (self.canary, "secret-plan", self.key):
                    self.assertNotIn(needle.encode("utf-8"), data, f"{needle} reached {path.name}")

    def test_a_request_over_the_bound_is_refused(self):
        self.request.write_bytes(b"x" * (1024 * 1024 + 1))
        code, out, err = self.design()
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_RECOMMEND_REQUEST_TOO_LARGE"), err)
        self.assertEqual(self.judge.sent, [])
        self.assertFalse(self.state.exists())

    def test_an_unreadable_request_is_refused(self):
        for name, prepare in (("missing", lambda: self.request.unlink()),
                              ("not utf-8", lambda: self.request.write_bytes(b"caf\xe9\n"))):
            with self.subTest(case=name):
                self.setUp()
                prepare()
                code, out, err = self.design()
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("OAH_RECOMMEND_REQUEST_UNREADABLE"), err)
                self.assertEqual(self.judge.sent, [])

    def test_usage_mistakes_name_the_fix(self):
        cases = (("--kind", "implementation", "--task", "demo", "--request-file", str(self.request)),
                 ("--kind", "design", "--request-file", str(self.request)),
                 ("--kind", "design", "--task", "../demo", "--request-file", str(self.request)),
                 ("--kind", "review", "--intensity", "low"),
                 ("--kind", "review", "--intensity", "low", "--reasoning", "low", "--risk", "low", "--task", "demo"))
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = self.ask(*argv)
                self.assertEqual(code, 2, err)
                self.assertTrue(err.startswith("OAH_RECOMMEND_USAGE"), err)
                self.assertIn("--request-file", err)
        self.assertEqual(self.judge.sent, [])

    def test_an_invalid_threshold_refuses(self):
        self.configure(self.JEV_ON + "difficulty_min_confidence = 2\n")
        code, out, err = self.design()
        self.assertEqual(code, 1)
        self.assertIn("OAH_CONFIG_INVALID", err)
        self.assertEqual(self.judge.sent, [])

    def test_no_network_without_a_request_file(self):
        spy = mock.Mock(side_effect=AssertionError("recommend asked Jev without --request-file"))
        with mock.patch.object(rerank, "advise_difficulty", spy):
            code, out, err = self.ask(*AXES, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual((spy.call_count, self.judge.sent), (0, []))
        self.assertNotIn("jev", json.loads(out))
        self.assertFalse(self.state.exists())

    def test_the_model_follows_the_final_tier(self):
        from tests import test_call as calls

        project = calls.scored_project(self, self.JEV_ON)
        project.env.update(FAKE_RERANK_KEY=self.key)
        self.repo, self.env, self.state = project.repo, project.env, project.state
        code, out, err = self.design("--engine", "codex", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["tier"], data["model"]["tier"]), ("max", "max"))


if __name__ == "__main__":
    unittest.main()
