# effort 규칙 검사 — 지원 목록에서 effort 를 고르고(highest 는 ultra 를 건너뜀), 헤더 비교와 버전별 정규화가 낮춤·불일치·미확인을 정직하게 남기는지 본다.
from __future__ import annotations

import unittest

from oah.engines.effort import (UNVERIFIED, EngineError, HeaderCheck, compare_labels, highest, load_rules, normalize,
                                resolve, verify_inference)

RULES = [
    {"id": "test-codex-1.2", "engine": "codex", "cli_versions": ["1.2."], "models": ["gpt-test*"],
     "map": {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max", "ultra": "xhigh"}},
]


class ResolveTest(unittest.TestCase):
    def test_literal_on_the_list(self):
        resolution = resolve("high", ["low", "medium", "high"])
        self.assertEqual((resolution.requested, resolution.launch, resolution.metadata), ("high", "high", "listed"))

    def test_literal_not_listed_refuses(self):
        with self.assertRaises(EngineError) as ctx:
            resolve("max", ["low", "medium", "high"], model="m1")
        self.assertEqual(ctx.exception.code, "OAH_EFFORT_UNSUPPORTED")
        self.assertIn("low, medium, high", str(ctx.exception))
        self.assertIn(".oah/config.toml", str(ctx.exception))

    def test_literal_with_missing_metadata_passes_unchanged(self):
        for levels in (None, []):
            resolution = resolve("max", levels)
            self.assertEqual((resolution.launch, resolution.metadata), ("max", "missing"))

    def test_highest_skips_ultra(self):
        self.assertEqual(highest(["low", "medium", "high", "xhigh", "max", "ultra"]), "max")
        self.assertEqual(highest(["ultra", "high", "low"]), "high")
        resolution = resolve("highest", ["low", "medium", "high", "xhigh", "max", "ultra"])
        self.assertEqual((resolution.requested, resolution.launch), ("highest", "max"))

    def test_highest_ignores_unknown_labels(self):
        self.assertEqual(highest(["medium", "turbo"]), "medium")

    def test_highest_without_metadata_refuses(self):
        for levels in (None, [], ["ultra"], ["turbo"]):
            with self.subTest(levels=levels), self.assertRaises(EngineError) as ctx:
                resolve("highest", levels, model="m1")
            self.assertEqual(ctx.exception.code, "OAH_EFFORT_METADATA_MISSING")
            self.assertIn("never guesses", str(ctx.exception))
            self.assertIn("models --refresh", str(ctx.exception))

    def test_bad_label_refuses(self):
        with self.assertRaises(EngineError) as ctx:
            resolve('high" model="x', None)
        self.assertEqual(ctx.exception.code, "OAH_EFFORT_INVALID")


class HeaderTest(unittest.TestCase):
    def test_compare_labels(self):
        self.assertEqual(compare_labels("high", "high"), ("match", None))
        self.assertEqual(compare_labels("medium", "high"), ("downgraded", "OAH_EFFORT_DOWNGRADED"))
        self.assertEqual(compare_labels("max", "high"), ("mismatch", "OAH_EFFORT_MISMATCH"))
        self.assertEqual(compare_labels(None, "high"), ("unverified", None))

    def test_ultra_label_is_not_ordinal(self):
        # ultra is behaviour-changing: never "lower" or "higher" than an ordinary label.
        self.assertEqual(compare_labels("ultra", "max"), ("mismatch", "OAH_EFFORT_MISMATCH"))
        self.assertEqual(compare_labels("max", "ultra"), ("mismatch", "OAH_EFFORT_MISMATCH"))
        self.assertEqual(compare_labels("ultra", "ultra"), ("match", None))


def header(effort: str | None, launch: str) -> HeaderCheck:
    status, code = compare_labels(effort, launch)
    return HeaderCheck(header_effort=effort or UNVERIFIED, status=status, code=code, header_model=None)


class NormalizationTest(unittest.TestCase):
    def test_unmeasured_models_cannot_be_verified_by_shipped_rules(self):
        for model in ("gpt-test-a", "gpt-6.1-sol", "unmeasured-model"):
            for label in ("low", "medium", "high", "xhigh", "max"):
                with self.subTest(model=model, label=label):
                    check = verify_inference("codex", "0.163.0", model, label, header(label, label), load_rules())
                    self.assertFalse(check.effort_verified)
                    self.assertEqual(check.resolved_inference_effort, UNVERIFIED)

    def test_known_mapping(self):
        self.assertEqual(normalize("codex", "1.2.7", "gpt-test-a", "high", RULES), ("high", "test-codex-1.2"))
        self.assertEqual(normalize("codex", "1.2.7", "gpt-test-a", "ultra", RULES), ("xhigh", "test-codex-1.2"))

    def test_unknown_mapping_is_unverified(self):
        for args in (("codex", "1.3.0", "gpt-test-a", "high"), ("codex", "1.2.7", "other", "high"),
                     ("claude", "1.2.7", "gpt-test-a", "high"), ("codex", "1.2.7", "gpt-test-a", "turbo"),
                     ("codex", None, "gpt-test-a", "high")):
            with self.subTest(args=args):
                self.assertEqual(normalize(*args, RULES), (None, None))

    def test_matching_header_with_known_rule_is_verified(self):
        check = verify_inference("codex", "1.2.7", "gpt-test-a", "high", header("high", "high"), RULES)
        self.assertEqual((check.resolved_inference_effort, check.normalization_basis, check.code, check.effort_verified),
                         ("high", "test-codex-1.2", None, True))

    def test_ultra_normalizes(self):
        check = verify_inference("codex", "1.2.7", "gpt-test-a", "ultra", header("ultra", "ultra"), RULES)
        self.assertEqual((check.resolved_inference_effort, check.code, check.effort_verified), ("xhigh", None, True))

    def test_normalized_lower_is_downgraded(self):
        # The labels only "mismatch", but ultra normalizes to xhigh, which is lower than max.
        check = verify_inference("codex", "1.2.7", "gpt-test-a", "max", header("ultra", "max"), RULES)
        self.assertEqual((check.resolved_inference_effort, check.code, check.effort_verified),
                         ("xhigh", "OAH_EFFORT_DOWNGRADED", False))

    def test_unknown_normalization_is_unverified(self):
        check = verify_inference("codex", "9.9.9", "gpt-test-a", "high", header("high", "high"), RULES)
        self.assertEqual((check.resolved_inference_effort, check.code, check.effort_verified), (UNVERIFIED, None, False))
        self.assertTrue(check.normalization_basis.startswith("unverified:"))

    def test_unreadable_header_is_unverified(self):
        check = verify_inference("codex", "1.2.7", "gpt-test-a", "high", header(None, "high"), RULES)
        self.assertEqual((check.resolved_inference_effort, check.effort_verified), (UNVERIFIED, False))
        self.assertIn("header", check.normalization_basis)

    def test_shipped_rules_load_and_leave_ultra_unknown(self):
        rules = load_rules()
        self.assertEqual(rules, [])  # no version/model mapping has recorded evidence yet
        for rule in rules:
            self.assertEqual(set(rule) - {"id", "engine", "cli_versions", "models", "map", "note"}, set())
            self.assertNotIn("ultra", rule["map"])


if __name__ == "__main__":
    unittest.main()
