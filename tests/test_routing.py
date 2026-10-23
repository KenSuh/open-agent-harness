# 라우팅 검사 — 난이도 점수표·기본 단·끌어올림·종류 하한·규칙 기반 축 점수·단계 엔진 강제·oah recommend 출력을 본다.
from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest

from oah import recommend_cli
from oah.routing import (INTENSITY_SCORES, KIND_SCORES, REASONING_SCORES, RISK_SCORES, Axes, RoutingError,
                         check_stage, recommend, same_engine_warning, score_axes)

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
            "review": 2, "implementation": 2, "refactor": 2, "broad-audit": 2,
            "adversarial-review": 3, "migration": 3, "security": 3, "high-risk-review": 3})
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

    def test_recommend_cli_rejects_bad_values(self):
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command")
        recommend_cli.register(commands)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["recommend", "--kind", "nope", "--intensity", "low", "--reasoning", "low",
                               "--risk", "low"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
