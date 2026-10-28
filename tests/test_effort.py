# effort 규칙 검사 — 지원 목록에서 effort 를 고르고(highest 는 ultra 를 건너뜀), 헤더 비교와 버전별 정규화가 낮춤·불일치·미확인을 정직하게 남기는지 본다.
from __future__ import annotations

import copy
import json
import unittest
from importlib import resources

from oah.engines.effort import (PROVEN_LABELS, UNVERIFIED, EngineError, HeaderCheck, compare_labels, highest,
                                load_rules, normalize, parse_rules, resolve, verify_inference)

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
        self.assertTrue(rules)  # every shipped rule is exact and evidence-backed (intent nrm)
        for rule in rules:
            self.assertEqual(set(rule) - {"id", "engine", "match", "cli_versions", "models", "map", "evidence",
                                          "assumptions", "note"}, set())
            self.assertEqual(rule["match"], "exact")
            self.assertEqual(rule["evidence"]["kind"], "public-source")
            self.assertNotIn("ultra", rule["map"])


COMMIT = "6aaac694803004a63c6f393d153c96d22521a6b9"
SHIPPED_ID = "codex-0.164.0-gpt-6.1"
SHIPPED_BASIS = f"{SHIPPED_ID}: expected mapping from CLI source at {COMMIT}; not observed served effort"


class ShippedRuleTest(unittest.TestCase):
    """The shipped rule: exact Codex 0.164.0, exact models, proven labels only, honest basis."""

    def check(self, version, model, launch, effort):
        return verify_inference("codex", version, model, launch, header(effort, launch), load_rules())

    def test_proven_labels_verify_for_exact_version_and_models(self):
        for model in ("gpt-6.1-sol", "gpt-6.1-terra"):
            for label in ("low", "medium", "high", "xhigh", "max"):
                with self.subTest(model=model, label=label):
                    check = self.check("0.164.0", model, label, label)
                    self.assertEqual((check.resolved_inference_effort, check.normalization_basis, check.code,
                                      check.effort_verified), (label, SHIPPED_BASIS, None, True))
                    self.assertIn("not observed served effort", check.normalization_basis)

    def test_other_versions_stay_unverified(self):
        for version in ("0.164.1", "0.164.01", "0.164.0-dev", "0.164", "0.164.", "0.164.00", " 0.164.0", "",
                        None, "0.163.0", "1.0.164.0"):
            with self.subTest(version=version):
                check = self.check(version, "gpt-6.1-sol", "medium", "medium")
                self.assertEqual((check.resolved_inference_effort, check.effort_verified), (UNVERIFIED, False))
                self.assertTrue(check.normalization_basis.startswith("unverified:"))
                self.assertEqual(normalize("codex", version, "gpt-6.1-sol", "medium", load_rules()), (None, None))

    def test_other_models_and_unproven_labels_stay_unverified(self):
        for model, label in (("gpt-6.1-sol-mini", "medium"), ("gpt-6.1", "medium"), ("GPT-6.1-SOL", "medium"),
                             ("gpt-6.1-luna", "high"), ("gpt-6-sol", "high"), ("gpt-6.1-sol", "ultra"),
                             ("gpt-6.1-sol", "persistent"), ("gpt-6.1-terra", "none"),
                             ("gpt-6.1-terra", "minimal"), ("gpt-6.1-sol", "turbo")):
            with self.subTest(model=model, label=label):
                check = self.check("0.164.0", model, label, label)
                self.assertEqual((check.resolved_inference_effort, check.effort_verified), (UNVERIFIED, False))
        self.assertEqual(normalize("claude", "0.164.0", "gpt-6.1-sol", "high", load_rules()), (None, None))

    def test_downgraded_and_mismatched_headers_still_fail(self):
        check = self.check("0.164.0", "gpt-6.1-sol", "high", "medium")
        self.assertEqual((check.resolved_inference_effort, check.code, check.effort_verified),
                         ("medium", "OAH_EFFORT_DOWNGRADED", False))
        mismatched = header("ultra", "high")
        self.assertEqual((mismatched.status, mismatched.code), ("mismatch", "OAH_EFFORT_MISMATCH"))
        check = verify_inference("codex", "0.164.0", "gpt-6.1-sol", "high", mismatched, load_rules())
        self.assertFalse(check.effort_verified)
        mismatched = header("max", "high")  # higher configuration: a mismatch, never verified
        check = verify_inference("codex", "0.164.0", "gpt-6.1-sol", "high", mismatched, load_rules())
        self.assertEqual((mismatched.code, check.effort_verified), ("OAH_EFFORT_MISMATCH", False))
        check = self.check("0.164.0", "gpt-6.1-sol", "high", None)
        self.assertEqual((check.resolved_inference_effort, check.effort_verified), (UNVERIFIED, False))

    def test_exact_match_ignores_prefix_and_glob(self):
        exact = {"id": "x", "engine": "codex", "match": "exact", "cli_versions": ["0.164.", "*"],
                 "models": ["gpt-*", "*"], "map": {"high": "high"}}
        self.assertEqual(normalize("codex", "0.164.0", "gpt-6.1-sol", "high", [exact]), (None, None))
        self.assertEqual(normalize("codex", "*", "*", "high", [exact]), ("high", "x"))  # literal equality only
        for policy in ("prefix", "glob", "Exact", "", None, 1):
            with self.subTest(policy=policy):
                rule = dict(exact, match=policy, cli_versions=["0.164.0"], models=["gpt-6.1-sol"])
                self.assertEqual(normalize("codex", "0.164.0", "gpt-6.1-sol", "high", [rule]), (None, None))

    def test_legacy_rule_without_evidence_keeps_id_basis(self):
        check = verify_inference("codex", "1.2.7", "gpt-test-a", "high", header("high", "high"), RULES)
        self.assertEqual(check.normalization_basis, "test-codex-1.2")


def shipped_document() -> dict:
    text = resources.files("oah").joinpath("templates/engines/normalization.json").read_text(encoding="utf-8")
    return json.loads(text)


class ParseRulesTest(unittest.TestCase):
    """`load_rules` refuses any shipped rule that is not exact, evidence-backed and proven."""

    def refused(self, document, needle: str) -> None:
        with self.assertRaises(EngineError) as ctx:
            parse_rules(json.dumps(document))
        self.assertEqual(ctx.exception.code, "OAH_NORMALIZATION_RULE_INVALID")
        self.assertIn(needle, str(ctx.exception))
        self.assertIn("reinstall oah", str(ctx.exception))

    def mutated(self, change) -> dict:
        document = copy.deepcopy(shipped_document())
        change(document["rules"][0])
        return document

    def test_shipped_file_parses_and_matches_load_rules(self):
        document = shipped_document()
        self.assertEqual(parse_rules(json.dumps(document)), load_rules())
        self.assertEqual(document["version"], 2)
        (rule,) = load_rules()
        self.assertEqual((rule["id"], rule["cli_versions"], rule["models"]),
                         (SHIPPED_ID, ["0.164.0"], ["gpt-6.1-sol", "gpt-6.1-terra"]))
        self.assertEqual(rule["map"], {label: label for label in PROVEN_LABELS})
        self.assertEqual(PROVEN_LABELS, ("low", "medium", "high", "xhigh", "max"))
        evidence = rule["evidence"]
        self.assertEqual((evidence["kind"], evidence["repository"], evidence["commit"]),
                         ("public-source", "openai/codex", COMMIT))
        for reference in ("codex-rs/core/src/config/mod.rs:1039-1041", "codex-rs/core/src/client.rs:957-976",
                          "codex-rs/exec/src/event_processor_with_human_output.rs:461-469"):
            self.assertIn(reference, evidence["references"])
        self.assertEqual(set(rule["assumptions"]), {"explicit-supported-effort", "fresh-codex-exec-session",
                                                    "no-prior-runtime-effort-pin"})

    def test_evidence_less_rule_refuses(self):
        self.refused(self.mutated(lambda rule: rule.pop("evidence")), "evidence")
        for field in ("kind", "repository", "commit", "references"):
            with self.subTest(field=field):
                self.refused(self.mutated(lambda rule: rule["evidence"].pop(field)), "evidence")
        for field, value in (("kind", "observed"), ("repository", ""), ("commit", COMMIT[:12]),
                             ("commit", COMMIT.upper()), ("commit", "g" * 40), ("references", []),
                             ("references", ["codex-rs/core/src/client.rs"]), ("references", ["client.rs:x"])):
            with self.subTest(field=field, value=value):
                self.refused(self.mutated(lambda rule: rule["evidence"].__setitem__(field, value)), "evidence")
        self.refused(self.mutated(lambda rule: rule.pop("assumptions")), "assumptions")
        self.refused(self.mutated(lambda rule: rule.__setitem__("assumptions", ["fresh-codex-exec-session"])),
                     "assumptions")

    def test_wildcard_or_prefix_version_refuses(self):
        for versions in (["*"], ["0.164."], ["0.164"], ["0.164.*"], ["0.164.01"], ["0.164.0-dev"], [""], [],
                         "0.164.0", [0.164]):
            with self.subTest(versions=versions):
                self.refused(self.mutated(lambda rule: rule.__setitem__("cli_versions", versions)), "cli_versions")
        for match in (None, "prefix", "Exact"):
            with self.subTest(match=match):
                self.refused(self.mutated(lambda rule: rule.__setitem__("match", match)), "match")
        self.refused(self.mutated(lambda rule: rule.pop("match")), "match")

    def test_wildcard_model_refuses(self):
        for models in (["gpt-6.1-*"], ["*"], ["gpt-6.1-so?"], ["gpt-6.1-[st]*"], [""], [], "gpt-6.1-sol"):
            with self.subTest(models=models):
                self.refused(self.mutated(lambda rule: rule.__setitem__("models", models)), "models")

    def test_unproven_or_non_identity_label_refuses(self):
        for label, value in (("ultra", "xhigh"), ("persistent", "none"), ("none", "none"), ("minimal", "minimal"),
                             ("turbo", "turbo"), ("high", "xhigh")):
            with self.subTest(label=label):
                self.refused(self.mutated(lambda rule: rule["map"].__setitem__(label, value)), "map")
        self.refused(self.mutated(lambda rule: rule.__setitem__("map", {})), "map")

    def test_malformed_document_refuses(self):
        self.refused({"version": 1, "rules": []}, "version")
        self.refused({"rules": []}, "version")
        self.refused({"version": 2}, "rules")
        self.refused({"version": 2, "rules": {}}, "rules")
        self.refused(self.mutated(lambda rule: rule.__setitem__("extra", 1)), "extra")
        self.refused(self.mutated(lambda rule: rule.__setitem__("engine", "claude")), "engine")
        self.refused(self.mutated(lambda rule: rule.__setitem__("id", "")), "id")
        document = shipped_document()
        document["rules"].append(copy.deepcopy(document["rules"][0]))
        self.refused(document, "duplicate")
        with self.assertRaises(EngineError) as ctx:
            parse_rules("{not json")
        self.assertEqual(ctx.exception.code, "OAH_NORMALIZATION_RULE_INVALID")


if __name__ == "__main__":
    unittest.main()
