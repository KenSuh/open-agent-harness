# 대상 예산 회귀 시험 — 시험 전용 근거 프로필의 경로·만료·예약량·전체 읽기 렌더를 확인한다.
from __future__ import annotations

import dataclasses
import copy
import datetime as dt
import hashlib
import json
import unittest
from unittest import mock

from oah.discovery import budget
from oah.discovery.store import digest
from tests import e_qualification

TARGET = {"engine": "claude", "route": "process", "cli_version": "2.1.318", "profile_id": "oah-low",
          "model": "fixture-haiku", "effort": "low", "platform": "fixture", "architecture": "fixture",
          "root_digest": digest("fixture root"), "config_digest": digest("fixture config")}
NOW = dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)


class TargetBudgetTest(unittest.TestCase):
    def profile(self) -> budget.TargetBudgetProfileV1:
        with mock.patch.object(budget, "_profile_source", e_qualification.source):
            return budget.qualify(TARGET, now=NOW)

    def test_test_only_qualified_route_measures_the_whole_render_and_all_reserves(self):
        profile = self.profile()
        deferred = (b"first\nsecond\n", b"x" * 70000)

        measured = budget.measure(profile, "inline", deferred=deferred, optional_bytes=17)

        self.assertEqual(measured["deferred_required_bytes"], sum(map(len, deferred)))
        self.assertEqual(measured["deferred_render_bytes"], sum(budget.read_render(data) for data in deferred))
        self.assertGreater(measured["deferred_render_bytes"], measured["deferred_required_bytes"])
        self.assertEqual(measured["upper_tokens"], 6 + measured["deferred_render_bytes"] + 17
                         + profile["estimator"]["fixed_render_tokens"]
                         + profile["estimator"]["tool_schema_upper_tokens"])
        self.assertEqual(measured["available_tokens"], profile["input_capacity_tokens"]
                         - profile["output_reserve_tokens"] - profile["host_reserve_tokens"]
                         - profile["existing_context_upper_tokens"])
        long_path = budget.measure(profile, "inline", deferred=(b"one\n",), read_paths=("가" * 1000,))
        self.assertGreater(long_path["deferred_render_bytes"], budget.read_render(b"one\n"))

    def test_shipped_registry_and_a_changed_exact_target_are_unknown(self):
        with self.assertRaises(budget.BudgetRefused) as shipped:
            budget.qualify(TARGET, now=NOW)
        self.assertEqual(shipped.exception.reason, "target-capacity-unknown")
        source = e_qualification.source(TARGET)
        with mock.patch.object(budget, "_profile_source", return_value=source):
            for key, value in (("route", "native"), ("cli_version", "2.1.317"), ("model", "other-model")):
                with self.subTest(field=key), self.assertRaises(budget.BudgetRefused) as changed:
                    budget.qualify(dict(TARGET, **{key: value}), now=NOW)
                self.assertEqual(changed.exception.reason, "target-capacity-unknown")

    def test_reachable_optional_rows_use_the_inventory_render_bound(self):
        from oah.capabilities import CLASSES, plan

        entries = [{"id": "mcp:fixture/" + str(index) + "x" * 240} for index in range(300)]
        child, parent = {"entries": entries[:200]}, {"entries": entries[100:]}
        bound = plan.optional_render_bound(child, parent)
        self.assertGreater(bound, 65536)
        for route in ("direct", "parent-relay"):
            for cls in CLASSES:
                for choice in ("chosen by Jev", "first in configuration order"):
                    rows = [dict(entry, route=route, required=False, reason=cls + ", " + choice) for entry in entries]
                    text = plan.block({"selected": rows}, {})
                    assert text is not None
                    self.assertLessEqual(len(("\n" + text).encode("utf-8")), bound)
        self.assertEqual(plan.optional_render_bound({"entries": []}, None), 0)
        with self.assertRaises(budget.BudgetRefused) as refused:
            budget.measure(self.profile(), "x" * 450000, optional_bytes=bound)
        self.assertEqual(refused.exception.reason, "brief")

    def test_profile_expiry_pin_schema_and_empty_capacity_refuse(self):
        original = json.loads(e_qualification.source(TARGET).raw)
        for field, value in (("expires_at", "2020-01-02T00:00:00Z"), ("observed_at", 123),
                             ("input_capacity_tokens", 1), ("version", True), ("evidence_ref", "no evidence")):
            document = json.loads(json.dumps(original))
            profile = document["profiles"][0]
            profile[field] = value
            profile["id"] = digest({key: item for key, item in profile.items() if key != "id"})
            raw = json.dumps(document).encode("utf-8")
            source = budget.PinnedSource(raw, hashlib.sha256(raw).hexdigest())
            with self.subTest(field=field), mock.patch.object(budget, "_profile_source", return_value=source), \
                    self.assertRaises(budget.BudgetRefused) as refused:
                budget.qualify(TARGET, now=NOW)
            self.assertEqual(refused.exception.reason, "target-capacity-unknown")
        source = e_qualification.source(TARGET)
        with mock.patch.object(budget, "_profile_source", return_value=dataclasses.replace(source, sha256="0" * 64)), \
                self.assertRaises(budget.BudgetRefused) as changed_pin:
            budget.qualify(TARGET, now=NOW)
        self.assertEqual(changed_pin.exception.reason, "target-capacity-unknown")

    def test_independent_limits_and_required_reads_cannot_be_omitted(self):
        profile = self.profile()
        cases: tuple[tuple[str, int, int, tuple[bytes, ...], str], ...] = (
            ("small", 196609, 0, (), "skills"), ("small", 0, 262145, (), "design"),
            ("x" * 400000, 0, 0, (b"x" * 150000,), "brief"), ("x" * 1000, 0, 0, (), "target-capacity"))
        for text, skills, design, deferred, reason in cases:
            current = copy.deepcopy(profile)
            if reason == "target-capacity":
                current["input_capacity_tokens"] = (profile["output_reserve_tokens"]
                    + profile["host_reserve_tokens"] + profile["existing_context_upper_tokens"] + 100)
            with self.subTest(reason=reason), self.assertRaises(budget.BudgetRefused) as refused:
                budget.measure(current, text, skill_bytes=skills, design_bytes=design, deferred=deferred)
            self.assertEqual(refused.exception.reason, reason)
            self.assertIn("measured totals", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
