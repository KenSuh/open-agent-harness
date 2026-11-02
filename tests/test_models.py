# 모델 카탈로그·선택 검사 — 고정·풀·품질 목록·점수 규칙의 우선순위, 옛 기본값 무시, 후보 범위, 메타데이터 유실, 새 설치 거부, oah models 출력을 본다.
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

from oah import models_cli, scoring
from oah.engines.effort import EngineError
from oah.engines import codex
from oah.models import (Catalog, Entry, ModelInfo, Pair, build_candidates, check_caps, map_tier,
                        resolve_base, unpooled_models, unpooled_warning, user_order)
from tests.helpers import make_git_repo, make_tmp_dir

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_CODEX = [sys.executable, str(FAKES / "fake_codex.py")]
FAKE_CLAUDE = [sys.executable, str(FAKES / "fake_claude.py")]
TIERS = ("low", "medium", "high", "max")
NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)


def write_cache(home: pathlib.Path, *models: tuple) -> None:
    home.mkdir(parents=True, exist_ok=True)
    entries = [{"slug": slug, "supported_reasoning_levels": [{"effort": e} for e in levels], "is_default": default}
               for slug, levels, default in models]
    (home / "models_cache.json").write_text(json.dumps({"models": entries}), encoding="utf-8")


class Fixture:
    def __init__(self, test: unittest.TestCase) -> None:
        self.root = make_tmp_dir(test)
        self.state = self.root / "state"
        self.home = self.root / "codex-home"
        self.catalog = Catalog(self.state)
        self.env = dict(os.environ, CODEX_HOME=str(self.home))
        for key in ("FAKE_CODEX_VERSION", "FAKE_CLAUDE_ALIASES", "FAKE_CLAUDE_EFFORTS"):
            self.env.pop(key, None)

    def fresh(self, engine: str, *, refresh: bool = False, now: dt.datetime = NOW, **env: str):
        base = FAKE_CODEX if engine == "codex" else FAKE_CLAUDE
        return self.catalog.ensure_fresh(engine, base=base, env=dict(self.env, **env), ttl_hours=24,
                                         refresh=refresh, now=now)


OLD_DEFAULT = ["opus", "sonnet", "haiku"]
CLAUDE_ORDER = ["opus", "sonnet", "fable"]


def scored_set(engine: str = "codex", **fields) -> scoring.ScoreSet:
    rows = {"codex": (("gpt-6-luna", "luna", (6, 0), 0.0, 0.6), ("gpt-6.1-terra", "terra", (6, 1), 89.0, 6),
                      ("gpt-6.1-sol", "sol", (6, 1), 99.0, 12), ("gpt-6-astra", "astra", (6, 0), 100.0, 60)),
            "claude": (("haiku", "haiku", (5, 5), 7.0, 0.6), ("sonnet", "sonnet", (5, 5), 75.0, 12),
                       ("opus", "opus", (5, 5), 100.0, 24), ("fable", "fable", (5, 1), 62.0, 60))}[engine]
    return scoring.ScoreSet(engine, "Epoch AI", "2026-11-02", "credit", tuple(scoring.Scored(*row) for row in rows),
                            fields.get("left_out", {}))


def listing(engine: str, *names: str, hidden: tuple[str, ...] = ()) -> Entry:
    models = [ModelInfo(name, ("low", "medium", "high", "max")) for name in names]
    models += [ModelInfo(name, ("low", "medium", "high", "max"), listed=False) for name in hidden]
    return Entry(engine, "1", "2026-11-02T00:00:00Z", tuple(models), "test")


CODEX_LIST = listing("codex", "gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra")
CLAUDE_LIST = listing("claude", "haiku", "sonnet", "opus", "fable")


def picked(engine: str, tier: str, *, entry=None, pin: str = "", order=(), scores=None, **fields) -> tuple[str, str]:
    entry = entry if entry is not None else (CODEX_LIST if engine == "codex" else CLAUDE_LIST)
    return map_tier(engine, tier, pin=pin, quality_order=list(order), entry=entry,
                    scores=scored_set(engine) if scores is None else scores, **fields)


class SelectionTest(unittest.TestCase):
    """The precedence of `map_tier`: a pin, then a pool, then quality_order, then the score rule."""

    def test_with_no_user_choice_the_model_comes_only_from_the_score_rule(self):
        for engine in ("codex", "claude"):
            for tier in TIERS:
                for profile in ("performance", "balanced", "efficient"):
                    with self.subTest(engine=engine, tier=tier, profile=profile):
                        expected = scoring.choose(scored_set(engine), tier, "low", profile).model
                        self.assertEqual(picked(engine, tier, profile=profile), (expected, "scored"))

    def test_the_default_flag_of_the_cli_does_not_choose(self):
        marked = Entry("codex", "1", "x", (ModelInfo("gpt-6-astra", ("low", "high"), True), *CODEX_LIST.models[:3]), "t")
        self.assertEqual(picked("codex", "low", entry=marked), ("gpt-6-luna", "scored"))

    def test_a_floor_lifts_the_scored_model(self):
        self.assertEqual(picked("codex", "low"), ("gpt-6-luna", "scored"))
        self.assertEqual(picked("codex", "low", floor_tier="high"), ("gpt-6.1-terra", "scored"))
        self.assertEqual(picked("codex", "low", floor_tier="high", profile="efficient"), ("gpt-6.1-terra", "scored"))

    def test_a_pin_wins_with_no_entry_and_no_scores(self):
        self.assertEqual(map_tier("codex", "max", pin="gpt-pinned", quality_order=["gpt-6-luna"], entry=None, scores=None),
                         ("gpt-pinned", "pin"))
        self.assertEqual(picked("codex", "low", pin="gpt-pinned", pools={"low": ["gpt-6-astra"]}),
                         ("gpt-pinned", "pin"))

    def test_a_pool_takes_its_first_listed_model_before_quality_order(self):
        pools = {"high": ["gpt-gone", "gpt-6-astra", "gpt-6-luna"]}
        self.assertEqual(picked("codex", "high", pools=pools, order=["gpt-6-luna"]), ("gpt-6-astra", "pool"))

    def test_a_tier_without_a_pool_goes_on_to_the_next_step(self):
        pools = {"high": ["gpt-6-astra"], "low": []}
        self.assertEqual(picked("codex", "low", pools=pools), ("gpt-6-luna", "scored"))
        self.assertEqual(picked("codex", "medium", pools=pools, order=["gpt-6-luna"]), ("gpt-6-luna", "quality_order"))

    def test_a_pool_skips_a_hidden_model(self):
        entry = listing("codex", "gpt-6-luna", hidden=("gpt-6-astra",))
        self.assertEqual(picked("codex", "high", entry=entry, pools={"high": ["gpt-6-astra", "gpt-6-luna"]}),
                         ("gpt-6-luna", "pool"))

    def test_a_configured_tier_with_none_listed_is_unresolved(self):
        for fields in ({"pools": {"high": ["gpt-gone"]}},
                       {"pools": {"high": ["gpt-6-astra"]}, "allowed": ["gpt-6-luna"]}):
            with self.subTest(fields=fields), self.assertRaises(EngineError) as caught:
                picked("codex", "high", **fields)
            self.assertEqual(caught.exception.code, "OAH_MODEL_UNRESOLVED")
            self.assertIn("tier_pools", str(caught.exception))
            self.assertIn(".oah/config.toml", str(caught.exception))

    def test_quality_order_needs_no_family_and_no_score(self):
        entry = listing("codex", "m-strong", "m-light", "gpt-6-luna")
        self.assertEqual(picked("codex", "high", entry=entry, order=["m-strong", "m-light"]),
                         ("m-strong", "quality_order"))
        self.assertEqual(map_tier("codex", "low", pin="", quality_order=["m-strong", "m-light"], entry=entry, scores=None),
                         ("m-light", "quality_order"))

    def test_quality_order_keeps_its_tier_positions(self):
        self.assertEqual([picked("codex", tier, order=["gpt-z", "gpt-6-astra", "gpt-6-luna"])[0] for tier in TIERS],
                         ["gpt-6-luna", "gpt-6-luna", "gpt-6-astra", "gpt-6-astra"])

    def test_quality_order_skips_a_hidden_model(self):
        entry = listing("codex", "gpt-6-luna", hidden=("gpt-6-astra",))
        self.assertEqual(picked("codex", "high", entry=entry, order=["gpt-6-astra", "gpt-6-luna"]),
                         ("gpt-6-luna", "quality_order"))

    def test_quality_order_naming_nothing_listed_falls_through_to_the_rule(self):
        self.assertEqual(picked("codex", "high", order=["gpt-z"]), ("gpt-6.1-terra", "scored"))

    def test_allowed_models_apply_to_pool_and_quality_order_picks(self):
        self.assertEqual(picked("codex", "high", pools={"high": ["gpt-6-astra", "gpt-6-luna"]}, allowed=["gpt-6-luna"]),
                         ("gpt-6-luna", "pool"))
        self.assertEqual(picked("codex", "high", order=["gpt-6-astra", "gpt-6-luna"], allowed=["gpt-6-luna"]),
                         ("gpt-6-luna", "quality_order"))

    def test_the_old_claude_default_counts_as_unset(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertEqual(picked("claude", tier, order=OLD_DEFAULT), picked("claude", tier))
                self.assertEqual(picked("claude", tier, order=OLD_DEFAULT)[1], "scored")
        self.assertEqual(user_order("claude", OLD_DEFAULT), [])

    def test_a_real_user_list_wins(self):
        for order in (["opus", "sonnet"], ["sonnet", "opus", "haiku"], ["opus", "sonnet", "haiku", "fable"]):
            with self.subTest(order=order):
                self.assertEqual(user_order("claude", order), order)
                self.assertEqual(picked("claude", "high", order=order), (order[0], "quality_order"))

    def test_the_old_default_is_only_a_claude_rule(self):
        self.assertEqual(user_order("codex", OLD_DEFAULT), OLD_DEFAULT)

    def test_no_usable_entry_names_the_one_line_pin(self):
        for entry in (None, Entry("codex", "1", None, (), "test"), listing("codex", hidden=("gpt-6-astra",))):
            with self.subTest(entry=entry), self.assertRaises(EngineError) as caught:
                map_tier("codex", "high", pin="", quality_order=["gpt-6-astra"], entry=entry, scores=scored_set(),
                         pools={"high": ["gpt-6-astra"]})
            self.assertEqual(caught.exception.code, "OAH_MODEL_CATALOG_UNAVAILABLE")
            self.assertIn('[engines.codex.tiers] high = { model = "<model>" }', str(caught.exception))

    def test_no_scores_is_a_named_failure_never_a_default(self):
        failed = scoring.failed("codex", EngineError(scoring.NO_SCORES, "the data is damaged; pin one"))
        for scores in (None, failed):
            with self.subTest(scores=scores), self.assertRaises(EngineError) as caught:
                map_tier("codex", "high", pin="", quality_order=[], entry=CODEX_LIST, scores=scores)
            self.assertEqual(caught.exception.code, "OAH_MODEL_NO_SCORES")
            self.assertIn(".oah/config.toml" if scores is None else "the data is damaged", str(caught.exception))


    def test_a_floor_out_of_reach_is_unresolved_and_a_step_out_of_reach_is_not(self):
        rows = (("gpt-6-luna", "low", 0.0, 0.6), ("gpt-6.1-terra", "terra", 89.0, 6))
        narrow = scoring.ScoreSet("codex", "Epoch AI", "2026-11-02", "credit",
                                  tuple(scoring.Scored(name, family, (6, 0), score, cost)
                                        for name, family, score, cost in rows), {}, scale=(0.0, 100.0))
        self.assertEqual(picked("codex", "max", scores=narrow), ("gpt-6.1-terra", "scored"))
        one = dataclasses.replace(narrow, models=narrow.models[:1])
        with self.assertRaises(EngineError) as caught:
            picked("codex", "high", scores=one, floor_tier="high")
        self.assertEqual(caught.exception.code, "OAH_MODEL_UNRESOLVED")
        self.assertIn("allowed_models", str(caught.exception))


class ClaudeMappingTest(unittest.TestCase):
    def test_claude_alias_mapping(self):
        fixture = Fixture(self)
        entry = fixture.fresh("claude").entry
        self.assertEqual([model.name for model in entry.models], ["fable", "opus", "sonnet"])
        self.assertEqual(entry.model("opus").efforts, ("low", "medium", "high", "xhigh", "max"))
        mapped = {tier: map_tier("claude", tier, pin="", quality_order=CLAUDE_ORDER, entry=entry, scores=None)
                  for tier in TIERS}
        self.assertEqual(mapped, {"low": ("fable", "quality_order"), "medium": ("sonnet", "quality_order"),
                                  "high": ("opus", "quality_order"), "max": ("opus", "quality_order")})

    def test_three_aliases_use_last_second_first(self):
        fixture = Fixture(self)
        entry = fixture.fresh("claude", FAKE_CLAUDE_ALIASES="haiku,sonnet,opus").entry
        self.assertEqual([map_tier("claude", t, pin="", quality_order=["opus", "sonnet", "haiku", "x"], entry=entry,
                                   scores=None)[0] for t in TIERS], ["haiku", "sonnet", "opus", "opus"])

    def test_one_alias_serves_every_tier(self):
        entry = Entry("claude", "1", "x", (ModelInfo("opus", ()),), "test")
        self.assertEqual({map_tier("claude", t, pin="", quality_order=CLAUDE_ORDER, entry=entry, scores=None)[0]
                          for t in TIERS}, {"opus"})

    def test_unreadable_help_is_a_named_failure_not_an_unconfirmed_alias(self):
        fixture = Fixture(self)
        freshness = fixture.fresh("claude", FAKE_CLAUDE_ALIASES="-")
        self.assertTrue(any(w.startswith("OAH_MODELS_REFRESH_FAILED") for w in freshness.warnings))
        with self.assertRaises(EngineError) as caught:
            map_tier("claude", "low", pin="", quality_order=CLAUDE_ORDER, entry=freshness.entry, scores=None)
        self.assertEqual(caught.exception.code, "OAH_MODEL_CATALOG_UNAVAILABLE")
        self.assertEqual(map_tier("claude", "low", pin="opus", quality_order=[], entry=freshness.entry, scores=None),
                         ("opus", "pin"))


class CodexMappingTest(unittest.TestCase):
    def test_a_changed_model_is_logged_once(self):
        fixture = Fixture(self)
        fixture.catalog.record_resolution("codex", "high", model="gpt-6-sol", effort="high", source="scored",
                                          why="first launch", now=NOW)
        changes = fixture.catalog.record_resolution("codex", "high", model="gpt-6.1-sol", effort="high", source="scored",
                                                    why="CLI version changed 0.163.0 -> 0.164.0",
                                                    now=NOW + dt.timedelta(hours=1))
        self.assertEqual(changes, [{"ts": "2026-10-23T13:00:00Z", "engine": "codex", "tier": "high",
                                    "field": "model", "old": "gpt-6-sol", "new": "gpt-6.1-sol",
                                    "why": "CLI version changed 0.163.0 -> 0.164.0"}])
        logged = fixture.catalog.changes()
        self.assertEqual([(c["field"], c["old"], c["new"]) for c in logged],
                         [("model", None, "gpt-6-sol"), ("effort", None, "high"), ("model", "gpt-6-sol", "gpt-6.1-sol")])
        # Recording the same values again logs nothing.
        self.assertEqual(fixture.catalog.record_resolution("codex", "high", model="gpt-6.1-sol", effort="high",
                                                           source="scored", why="again"), [])

    def test_version_change_and_ttl_refresh(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low"], True))
        self.assertEqual(fixture.fresh("codex").refreshed, "no catalog yet")
        write_cache(fixture.home, ("gpt-b", ["low"], True))
        same = fixture.fresh("codex", now=NOW + dt.timedelta(hours=23))
        self.assertIsNone(same.refreshed)
        self.assertEqual(same.entry.models[0].name, "gpt-a")
        expired = fixture.fresh("codex", now=NOW + dt.timedelta(hours=24))
        self.assertEqual(expired.refreshed, "catalog older than 24 h")
        self.assertEqual(expired.entry.models[0].name, "gpt-b")
        self.assertEqual(expired.previous.models[0].name, "gpt-a")
        write_cache(fixture.home, ("gpt-c", ["low"], True))
        forced = fixture.fresh("codex", refresh=True, now=NOW + dt.timedelta(hours=24, minutes=1))
        self.assertEqual((forced.refreshed, forced.entry.models[0].name), ("refresh requested", "gpt-c"))

    def test_a_probe_without_persist_writes_nothing(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low"], True))
        base = FAKE_CODEX
        freshness = fixture.catalog.ensure_fresh("codex", base=base, env=fixture.env, ttl_hours=24, now=NOW, persist=False)
        self.assertEqual((freshness.refreshed, freshness.entry.models[0].name), ("no catalog yet", "gpt-a"))
        self.assertFalse(fixture.state.exists(), "a read-only look creates no state at all")

    def test_user_quality_list_for_codex(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low"], True), ("gpt-b", ["low"], False))
        entry = fixture.fresh("codex").entry
        order = ["gpt-z", "gpt-b", "gpt-a"]
        self.assertEqual([map_tier("codex", t, pin="", quality_order=order, entry=entry, scores=None)[0]
                          for t in TIERS], ["gpt-a", "gpt-a", "gpt-b", "gpt-b"])

    def test_pin_wins_over_catalog(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low"], True))
        entry = fixture.fresh("codex").entry
        self.assertEqual(map_tier("codex", "max", pin="gpt-pinned", quality_order=["gpt-a"], entry=entry, scores=None),
                         ("gpt-pinned", "pin"))
        self.assertEqual(map_tier("claude", "low", pin="opus", quality_order=CLAUDE_ORDER, entry=None, scores=None),
                         ("opus", "pin"))

    def test_fresh_install_refuses(self):
        fixture = Fixture(self)
        freshness = fixture.fresh("codex")
        self.assertEqual(freshness.entry.models, ())
        self.assertTrue(any(w.startswith("OAH_MODELS_REFRESH_FAILED") for w in freshness.warnings))
        with self.assertRaises(EngineError) as ctx:
            map_tier("codex", "high", pin="", quality_order=[], entry=freshness.entry, scores=None)
        self.assertEqual(ctx.exception.code, "OAH_MODEL_CATALOG_UNAVAILABLE")
        self.assertIn(".oah/config.toml", str(ctx.exception))

    def test_lost_metadata_keeps_last_good_mapping(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low", "high"], True))
        fixture.fresh("codex")
        (fixture.home / "models_cache.json").unlink()
        freshness = fixture.fresh("codex", FAKE_CODEX_VERSION="0.164.0")
        self.assertIsNone(freshness.refreshed)
        self.assertTrue(any("keeping the catalog checked at 2026-10-23T12:00:00Z" in w for w in freshness.warnings))
        self.assertEqual(map_tier("codex", "high", pin="", quality_order=["gpt-a"], entry=freshness.entry, scores=None),
                         ("gpt-a", "quality_order"))
        self.assertEqual(freshness.entry.model("gpt-a").efforts, ("low", "high"))
        self.assertEqual(freshness.launch_version, "0.164.0")
        self.assertEqual(fixture.catalog.load("codex").cli_version, "0.163.0")


class ResolveBaseTest(unittest.TestCase):
    def test_paths_are_literal_and_missing_names_refuse(self):
        self.assertEqual(resolve_base("codex", FAKE_CODEX), FAKE_CODEX)
        empty = make_tmp_dir(self)
        with self.assertRaises(EngineError) as ctx:
            resolve_base("codex", ["codex"], search_path=str(empty))
        self.assertEqual(ctx.exception.code, "OAH_ENGINE_MISSING")
        with self.assertRaises(EngineError) as ctx:
            resolve_base("codex", [])
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")


def toml_list(argv: list[str]) -> str:
    return "[" + ", ".join(json.dumps(part) for part in argv) + "]"


def project_with_fakes(test: unittest.TestCase, extra: str = "") -> pathlib.Path:
    repo = make_git_repo(test)
    (repo / ".oah").mkdir()
    (repo / ".oah" / "config.toml").write_text(
        f"[engines.claude]\ncommand = {toml_list(FAKE_CLAUDE)}\n[engines.codex]\ncommand = {toml_list(FAKE_CODEX)}\n"
        + extra, encoding="utf-8")
    return repo


@contextlib.contextmanager
def inside(repo: pathlib.Path, env: dict):
    previous = os.getcwd()
    os.chdir(repo)
    try:
        with mock.patch.dict(os.environ, env):
            yield
    finally:
        os.chdir(previous)


def run_models(*argv: str) -> tuple[int, str, str]:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    models_cli.register(commands)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = models_cli.run(parser.parse_args(["models", *argv]))
    return code, out.getvalue(), err.getvalue()


class ModelsCliTest(unittest.TestCase):
    def test_models_cli_json(self):
        repo = project_with_fakes(self, 'quality_order = ["gpt-a"]\n[engines.codex.tiers.low]\nmodel = "gpt-pinned"\n')
        state, home = make_tmp_dir(self), make_tmp_dir(self)
        write_cache(home, ("gpt-a", ["low", "medium", "high", "xhigh", "max", "ultra"], True))
        with inside(repo, {"OAH_STATE_DIR": str(state), "CODEX_HOME": str(home)}):
            code, out, err = run_models("--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        codex_tiers = data["engines"]["codex"]["tiers"]
        self.assertEqual(codex_tiers["low"]["model"], "gpt-pinned")
        self.assertEqual(codex_tiers["low"]["model_source"], "pin")
        self.assertEqual((codex_tiers["max"]["model"], codex_tiers["max"]["effort"]), ("gpt-a", "max"))
        self.assertEqual(data["engines"]["codex"]["cli_version"], "0.163.0")
        self.assertEqual(data["engines"]["claude"]["tiers"]["high"]["model_source"], "scored")
        self.assertIn("gpt-a", [m["name"] for m in data["engines"]["codex"]["models"]])

    def test_shows_candidates_and_caps(self):
        repo = project_with_fakes(self, 'quality_order = ["m-strong", "m-mid"]\nallowed_models = ["m-strong", "m-mid"]\n'
                                        '[engines.codex.effort_caps]\n"m-strong" = "high"\n')
        state, home = make_tmp_dir(self), make_tmp_dir(self)
        write_cache(home, ("m-strong", ["low", "medium", "high", "xhigh", "max"], True),
                    ("m-mid", ["low", "medium", "high"], False))
        with inside(repo, {"OAH_STATE_DIR": str(state), "CODEX_HOME": str(home)}):
            code, out, err = run_models("--json")
            text_code, text, _ = run_models()
        self.assertEqual((code, text_code), (0, 0), err)
        codex_report = json.loads(out)["engines"]["codex"]
        self.assertEqual(codex_report["allowed_models"], ["m-strong", "m-mid"])
        self.assertEqual(codex_report["effort_caps"], {"m-strong": "high"})
        high = codex_report["tiers"]["high"]
        self.assertEqual((high["model"], high["effort"]), ("m-strong", "high"))
        self.assertEqual(high["floor"], {"tier": "high", "model": "m-strong", "effort": "high"})
        self.assertEqual(high["candidates"], [["m-strong", "high"]])
        low = codex_report["tiers"]["low"]
        self.assertEqual((low["model"], low["effort"]), ("m-mid", "low"))
        self.assertIn(["m-strong", "low"], low["candidates"])
        self.assertNotIn(["m-strong", "max"], low["candidates"])
        self.assertIn("allowed m-strong, m-mid", text)
        self.assertIn("caps m-strong=high", text)
        self.assertIn("candidates at floor low: m-strong (low, medium, high) · m-mid (low, medium, high)", text)

    def test_shows_tier_pools_and_why_model_choice_is_off(self):
        state, home = make_tmp_dir(self), make_tmp_dir(self)
        write_cache(home, ("m-strong", ["low", "medium", "high"], True), ("m-mid", ["low", "medium"], False),
                    ("m-light", ["low"], False), ("m-new", ["low", "medium", "high"], False))
        pooled = project_with_fakes(self, 'allowed_models = ["m-strong", "m-mid", "m-light", "m-new"]\n'
                                          '[engines.codex.tier_pools]\nlow = ["m-mid", "m-light"]\n'
                                          'medium = ["m-mid"]\nhigh = ["m-strong"]\nmax = ["m-strong"]\n')
        with inside(pooled, {"OAH_STATE_DIR": str(state), "CODEX_HOME": str(home)}):
            code, out, err = run_models("--json")
            _, text, _ = run_models()
        self.assertEqual(code, 0, err)
        report = json.loads(out)["engines"]["codex"]
        self.assertEqual(report["tier_pools"], {"low": ["m-mid", "m-light"], "medium": ["m-mid"], "high": ["m-strong"],
                                                "max": ["m-strong"]})
        tiers = report["tiers"]
        self.assertEqual([tiers[tier]["mode"] for tier in tiers], ["pools"] * 4)
        self.assertEqual(tiers["low"]["candidates"], [["m-mid", "low"], ["m-mid", "medium"], ["m-light", "low"]])
        self.assertEqual(tiers["medium"]["candidates"], [["m-mid", "medium"]])
        self.assertEqual(tiers["high"]["candidates"], [["m-strong", "high"]])
        for tier, row in tiers.items():
            with self.subTest(tier=tier):
                # Allowed but in no pool: its quality for any tier is unknown, so it is never a candidate.
                self.assertEqual(row["excluded"]["m-new"], "not-in-tier-pool")
                self.assertNotIn("m-new", {model for model, _ in row["candidates"]})
        self.assertIn("pools low: m-mid, m-light; medium: m-mid; high: m-strong; max: m-strong", text)
        self.assertIn("candidates at floor low: m-mid (low, medium) · m-light (low)", text)
        self.assertIn("m-new (not-in-tier-pool)", text)
        # Pools for one tier only: the other tiers fall to scores, and the judge has no pool to choose from.
        write_cache(home, ("gpt-6-luna", ["low", "medium", "high"], True), ("gpt-6.1-sol", ["low", "medium", "high"], False))
        off = project_with_fakes(self, '[engines.codex.tier_pools]\nhigh = ["gpt-6.1-sol"]\n')
        with inside(off, {"OAH_STATE_DIR": str(state), "CODEX_HOME": str(home)}):
            code, out, err = run_models("--json", "--refresh")
            _, text, _ = run_models()
        self.assertEqual(code, 0, err)
        low = json.loads(out)["engines"]["codex"]["tiers"]["low"]
        self.assertEqual((low["mode"], low["note"]), ("deterministic", "model choice off: no ranking and no tier pool for low"))
        self.assertIn("model choice off: no ranking and no tier pool for low", text)

    def test_models_cli_reports_a_tier_with_no_catalog(self):
        repo = project_with_fakes(self)
        state, home = make_tmp_dir(self), make_tmp_dir(self)
        with inside(repo, {"OAH_STATE_DIR": str(state), "CODEX_HOME": str(home)}):
            code, out, err = run_models()
        self.assertEqual(code, 0)
        self.assertIn("OAH_MODEL_CATALOG_UNAVAILABLE", out)
        self.assertIn("OAH_MODELS_REFRESH_FAILED", err)

    def test_cap_conflicts_report_the_same_uncapped_floor_as_calls(self):
        from tests import test_call as calls

        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                model = "m-default" if engine == "codex" else "opus"
                project = calls.Project(self, 'quality_order = ["m-default"]\n'
                                              f'[engines.{engine}.effort_caps]\n"{model}" = "medium"\n')
                write_cache(project.home, ("m-default", ["low", "medium", "high", "max"], True))
                with inside(project.repo, project.env):
                    code, out, err = run_models("--json")
                    _, text, _ = run_models()
                    with self.assertRaises(EngineError) as caught:
                        project.prepare(engine=engine, stage="review" if engine == "codex" else "initial",
                                        axes={"kind": "security"})
                self.assertEqual(code, 0, err)
                rows = json.loads(out)["engines"][engine]["tiers"]
                self.assertEqual(rows["high"], {"error": str(caught.exception)})
                self.assertIn("OAH_CONFIG_INVALID", rows["max"]["error"])
                self.assertIn("floor effort high", text)
                self.assertIn(".oah/config.toml", rows["high"]["error"])
                self.assertEqual(rows["medium"]["floor"]["effort"], "medium")
                self.assertFalse(project.record.exists())


ALL = ("low", "medium", "high", "xhigh", "max", "ultra")
JUDGE_ENTRY = Entry("codex", "0.163.0", "2026-10-23T12:00:00Z", (
    ModelInfo("m-strong", ALL, True),
    ModelInfo("m-mid", ("low", "medium", "high")),
    ModelInfo("m-light", ("low", "medium")),
    ModelInfo("m-hidden", ALL, listed=False),
    ModelInfo("m-extra", ("medium", "high")),
), "fixture")
RANKING = ["m-strong", "m-mid", "m-light"]


def candidates(*, ranking=RANKING, floor=("m-mid", "medium"), deterministic=("m-strong", "high"),
               allowed=(), caps=None, floor_tier="medium", tier="high", pools=None):
    return build_candidates("codex", entry=JUDGE_ENTRY, deterministic=Pair(*deterministic), tier=tier,
                            floor_tier=floor_tier, floor=Pair(*floor), quality_order=list(ranking),
                            allowed_models=list(allowed), effort_caps=dict(caps or {}), tier_pools=dict(pools or {}))


def scored_candidates(*, deterministic, floor, tier, floor_tier, ranking=(), caps=None, pools=None, scores=None,
                      profile="balanced"):
    return build_candidates("codex", entry=CODEX_LIST, deterministic=Pair(*deterministic), tier=tier,
                            floor_tier=floor_tier, floor=Pair(*floor), quality_order=list(ranking), allowed_models=[],
                            effort_caps=dict(caps or {}), tier_pools=dict(pools or {}),
                            scores=scored_set() if scores is None else scores, profile=profile)


def pairs(candidate_set) -> list[tuple[str, str]]:
    return [(pair.model, pair.effort) for pair in candidate_set.pairs]


class CandidateTableTest(unittest.TestCase):
    def test_hidden_models_are_parsed_and_kept_out_of_candidates(self):
        parsed = codex.parse_models_cache(json.dumps({"models": [
            {"slug": "m-a", "visibility": "list", "supported_reasoning_levels": ["low"]},
            {"slug": "m-b", "visibility": "hide", "supported_reasoning_levels": ["low"]},
            {"slug": "m-c", "hidden": True, "supported_reasoning_levels": ["low"]},
            {"slug": "m-d", "supported_reasoning_levels": ["low"]},
        ]}))
        self.assertEqual({slug: model.listed for slug, model in parsed.items()},
                         {"m-a": True, "m-b": False, "m-c": False, "m-d": True})
        self.assertNotIn("m-hidden", {pair.model for pair in candidates(ranking=[]).pairs})
        self.assertNotIn("m-hidden", {pair.model for pair in candidates(ranking=["m-hidden", *RANKING]).pairs})

    def test_catalog_round_trips_listed(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("m-a", ["low"], True))
        cache = json.loads((fixture.home / "models_cache.json").read_text(encoding="utf-8"))
        cache["models"].append({"slug": "m-b", "visibility": "hide", "supported_reasoning_levels": ["low"]})
        (fixture.home / "models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
        fixture.fresh("codex")
        entry = fixture.catalog.load("codex")
        self.assertEqual([(model.name, model.listed) for model in entry.models], [("m-a", True), ("m-b", False)])
        old = Entry.from_json("codex", {"models": [{"name": "m-a", "efforts": ["low"]}]})
        self.assertIsNone(old)
        for listed in (None, "false", 0, 1):
            with self.subTest(listed=listed):
                self.assertIsNone(Entry.from_json("codex", {
                    "models": [{"name": "m-a", "efforts": ["low"], "listed": listed}]}))

    def test_unexpired_legacy_catalog_never_qualifies_hidden_models(self):
        for readable_cache in (True, False):
            for mixed_visibility in (False, True):
                with self.subTest(readable_cache=readable_cache, mixed_visibility=mixed_visibility):
                    fixture = Fixture(self)
                    visible = {"name": "m-visible", "efforts": ["high"], "default": True}
                    if mixed_visibility:
                        visible["listed"] = True
                    legacy = {"codex": {"cli_version": "0.163.0", "checked_at": NOW.isoformat(),
                                        "source": "fixture", "models": [visible,
                                            {"name": "m-hidden", "efforts": ["high"]}]}}
                    fixture.catalog.dir.mkdir(parents=True)
                    fixture.catalog.catalog_path.write_bytes(json.dumps(legacy).encode("utf-8"))
                    if readable_cache:
                        fixture.home.mkdir()
                        (fixture.home / "models_cache.json").write_bytes(json.dumps({"models": [
                            {"slug": "m-visible", "supported_reasoning_levels": ["high"], "is_default": True},
                            {"slug": "m-hidden", "supported_reasoning_levels": ["high"], "visibility": "hide"},
                        ]}).encode("utf-8"))
                    freshness = fixture.fresh("codex", now=NOW + dt.timedelta(hours=1))
                    for ranking, pools in ((["m-hidden", "m-visible"], {}),
                                           ([], {"high": ["m-hidden", "m-visible"]})):
                        built = build_candidates("codex", entry=freshness.entry, tier="high", floor_tier="high",
                                                 deterministic=Pair("m-visible", "high"),
                                                 floor=Pair("m-visible", "high"), quality_order=ranking,
                                                 allowed_models=[], effort_caps={}, tier_pools=pools)
                        self.assertEqual(pairs(built), [("m-visible", "high")] if readable_cache else [])
                        self.assertEqual(built.reject_reason("m-hidden", "high"), "outside-candidates")
                    self.assertEqual(freshness.entry.cli_version, "0.163.0")
                    if readable_cache:
                        self.assertIsNotNone(freshness.refreshed)
                        self.assertFalse(fixture.catalog.load("codex").model("m-hidden").listed)
                        self.assertIsNone(fixture.fresh("codex", now=NOW + dt.timedelta(hours=2)).refreshed)
                    else:
                        self.assertTrue(any(w.startswith("OAH_MODELS_REFRESH_FAILED") for w in freshness.warnings))
                        self.assertIsNone(fixture.catalog.load("codex"))

    def test_candidate_table(self):
        rows = [
            ("ranked, floor medium", {}, [("m-strong", "medium"), ("m-strong", "high"), ("m-strong", "xhigh"),
                                          ("m-strong", "max"), ("m-mid", "medium"), ("m-mid", "high")]),
            ("ranked with a cap", {"caps": {"m-strong": "high"}},
             [("m-strong", "medium"), ("m-strong", "high"), ("m-mid", "medium"), ("m-mid", "high")]),
            ("ranked with an allow list", {"allowed": ["m-mid", "m-hidden"]}, [("m-mid", "medium"), ("m-mid", "high")]),
            ("ranked, floor low", {"floor": ("m-light", "low"), "floor_tier": "low"},
             [("m-strong", e) for e in ("low", "medium", "high", "xhigh", "max")]
             + [("m-mid", e) for e in ("low", "medium", "high")] + [("m-light", e) for e in ("low", "medium")]),
            ("no ranking: the model stays deterministic", {"ranking": [], "floor": ("m-strong", "medium")},
             [("m-strong", "medium"), ("m-strong", "high"), ("m-strong", "xhigh"), ("m-strong", "max")]),
            ("ranking names no listed model", {"ranking": ["m-hidden", "m-gone"], "floor": ("m-strong", "medium")},
             [("m-strong", "medium"), ("m-strong", "high"), ("m-strong", "xhigh"), ("m-strong", "max")]),
            ("floor model unranked: effort only", {"floor": ("m-extra", "medium"), "deterministic": ("m-mid", "high")},
             [("m-mid", "medium"), ("m-mid", "high")]),
            ("cap below the floor leaves nothing for that model",
             {"ranking": [], "floor": ("m-strong", "high"), "caps": {"m-strong": "medium"}}, []),
            # Revision 3: without a ranking, only models the user put in the floor tier's pool and the call tier's
            # pool are candidates (peers, no order inferred); allowed_models alone qualifies nothing.
            ("pools: floor and call tier pools both hold",
             {"ranking": [], "floor": ("m-strong", "medium"),
              "pools": {"medium": ["m-mid", "m-light", "m-strong"], "high": ["m-strong", "m-mid"]}},
             [("m-strong", e) for e in ("medium", "high", "xhigh", "max")] + [("m-mid", "medium"), ("m-mid", "high")]),
            ("a cap bounds a pool model",
             {"ranking": [], "floor": ("m-strong", "medium"), "tier": "medium",
              "pools": {"medium": ["m-mid", "m-extra"]}, "caps": {"m-extra": "medium"}},
             [("m-mid", "medium"), ("m-mid", "high"), ("m-extra", "medium")]),
            ("allowed_models filters a pool",
             {"ranking": [], "floor": ("m-strong", "medium"), "tier": "medium", "allowed": ["m-mid"],
              "pools": {"medium": ["m-mid", "m-light"]}}, [("m-mid", "medium"), ("m-mid", "high")]),
            ("hidden and unknown pool models are never candidates",
             {"ranking": [], "floor": ("m-strong", "medium"), "tier": "medium",
              "pools": {"medium": ["m-hidden", "m-typo", "m-light"]}}, [("m-light", "medium")]),
            ("a pool model below the floor effort is not a candidate",
             {"ranking": [], "floor": ("m-strong", "high"), "floor_tier": "high",
              "pools": {"high": ["m-mid", "m-light"]}}, [("m-mid", "high")]),
            ("no pool for the call tier: no judged candidates",
             {"ranking": [], "floor": ("m-strong", "medium"), "deterministic": ("m-strong", "medium"),
              "tier": "medium", "pools": {"low": ["m-light"], "high": ["m-mid"]}},
             []),
            ("allowed_models alone qualifies nothing",
             {"ranking": [], "floor": ("m-strong", "medium"), "deterministic": ("m-strong", "medium"),
              "tier": "medium", "allowed": ["m-mid", "m-light"]}, []),
        ]
        for name, options, expected in rows:
            with self.subTest(name):
                built = candidates(**options)
                self.assertEqual(pairs(built), expected)
                for pair in built.pairs:
                    self.assertIsNone(built.reject_reason(pair.model, pair.effort))

    def test_missing_or_empty_applicable_pool_has_no_candidates(self):
        for ranking in ([], RANKING):
            for absent in ("high", "max"):
                for empty in (False, True):
                    with self.subTest(ranking=ranking, absent=absent, empty=empty):
                        pools = {"high": ["m-mid"], "max": ["m-mid"]}
                        pools.pop(absent)
                        if empty:
                            pools[absent] = []
                        built = candidates(ranking=ranking, floor=("m-strong", "high"), floor_tier="high",
                                           tier="max", pools=pools)
                        self.assertEqual(pairs(built), [])
                        self.assertEqual(built.reject_reason("m-strong", "high"), "not-in-tier-pool")
                        self.assertEqual(built.reject_reason("m-mid", "high"), "not-in-tier-pool")

    def test_reject_reasons_table(self):
        ranked, unranked = candidates(caps={"m-strong": "high"}), candidates(ranking=[], floor=("m-strong", "medium"))
        allow = candidates(allowed=["m-strong"])
        pools = candidates(ranking=[], floor=("m-strong", "medium"), caps={"m-extra": "medium"},
                           pools={"medium": ["m-strong", "m-mid", "m-extra"], "high": ["m-strong", "m-extra"]})
        pinned_pool = candidates(ranking=[], floor=("m-strong", "medium"), allowed=["m-strong"],
                                 pools={"medium": ["m-strong", "m-extra"], "high": ["m-strong", "m-extra"]})
        rows = [
            (ranked, ("m-hidden", "high"), "outside-candidates"),
            (ranked, ("m-unknown", "high"), "outside-candidates"),
            (allow, ("m-mid", "high"), "not-allowed"),
            (ranked, ("m-light", "high"), "unsupported-effort"),
            (ranked, ("m-strong", "ultra"), "unsupported-effort"),
            (ranked, ("m-strong", "turbo"), "unsupported-effort"),
            (ranked, ("m-extra", "high"), "unranked-model"),
            (ranked, ("m-light", "medium"), "below-floor"),
            (ranked, ("m-strong", "low"), "below-floor"),
            (ranked, ("m-strong", "max"), "above-cap"),
            (unranked, ("m-mid", "high"), "outside-candidates"),
            (unranked, ("m-strong", "low"), "below-floor"),
            (ranked, ("m-mid", "high"), None),
            (pools, ("m-mid", "high"), "not-in-tier-pool"),
            (pools, ("m-light", "medium"), "not-in-tier-pool"),
            (pools, ("m-strong", "low"), "below-floor"),
            (pools, ("m-extra", "high"), "above-cap"),
            (pools, ("m-hidden", "high"), "outside-candidates"),
            (pools, ("m-strong", "ultra"), "unsupported-effort"),
            (pools, ("m-strong", "xhigh"), None),
            (pools, ("m-extra", "medium"), None),
            (pinned_pool, ("m-extra", "medium"), "not-allowed"),
        ]
        for built, (model, effort), rule in rows:
            with self.subTest(model=model, effort=effort):
                self.assertEqual(built.reject_reason(model, effort), rule)

    def test_candidate_set_json_names_its_inputs(self):
        data = candidates(caps={"m-strong": "high"}, allowed=["m-strong", "m-mid"]).to_json()
        self.assertEqual(data["deterministic"], {"model": "m-strong", "effort": "high"})
        self.assertEqual(data["floor"], {"tier": "medium", "model": "m-mid", "effort": "medium"})
        self.assertTrue(data["ranked"])
        self.assertEqual(data["mode"], "ranked")
        self.assertIsNone(data["note"])
        self.assertEqual(data["caps"], {"m-strong": "high"})
        self.assertEqual(data["allowed_models"], ["m-strong", "m-mid"])
        self.assertIn(["m-mid", "high"], data["candidates"])

    def test_a_scored_call_offers_the_score_band(self):
        built = scored_candidates(tier="medium", floor_tier="low", deterministic=("gpt-6.1-terra", "medium"),
                                  floor=("gpt-6-luna", "low"))
        self.assertEqual((built.mode, built.note, built.ranked), ("scored", None, False))
        self.assertEqual({pair.model for pair in built.pairs}, {"gpt-6.1-terra"},
                         "medium needs 33.3 and the next step 66.7: only the rule's choice is near it here")

    def test_the_band_reaches_one_step_up_and_never_below(self):
        built = scored_candidates(tier="high", floor_tier="medium", deterministic=("gpt-6.1-terra", "high"),
                                  floor=("gpt-6.1-terra", "medium"))
        self.assertEqual({pair.model for pair in built.pairs}, {"gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra"})
        self.assertIsNone(built.reject_reason("gpt-6.1-sol", "high"))
        self.assertEqual(built.reject_reason("gpt-6-luna", "high"), "outside-score-bound")

    def test_efficient_cannot_become_performance_through_the_band(self):
        # On these scores the bottom model scores 0, so terra is the rule's choice: the most score per cost.
        built = scored_candidates(tier="medium", floor_tier="low", profile="efficient",
                                  deterministic=("gpt-6.1-terra", "medium"), floor=("gpt-6.1-terra", "low"))
        self.assertEqual({pair.model for pair in built.pairs}, {"gpt-6.1-terra", "gpt-6-luna"})
        self.assertEqual(built.reject_reason("gpt-6.1-sol", "high"), "outside-score-bound")

    def test_the_band_keeps_the_model_the_rule_ran_when_the_effort_rules_moved_it(self):
        # A cap took terra out of the efficient rule, so sol is what runs: the judge must still be offered it.
        built = scored_candidates(tier="medium", floor_tier="medium", profile="efficient",
                                  deterministic=("gpt-6.1-sol", "medium"), floor=("gpt-6.1-sol", "medium"),
                                  caps={"gpt-6.1-terra": "low"})
        self.assertEqual({pair.model for pair in built.pairs}, {"gpt-6.1-sol"})

    def test_the_band_keeps_the_existing_effort_rules(self):
        built = scored_candidates(tier="high", floor_tier="medium", deterministic=("gpt-6.1-terra", "high"),
                                  floor=("gpt-6.1-terra", "medium"), caps={"gpt-6.1-sol": "medium"})
        self.assertEqual(built.reject_reason("gpt-6.1-sol", "high"), "above-cap")
        self.assertEqual(built.reject_reason("gpt-6.1-sol", "low"), "below-floor")
        self.assertEqual(built.reject_reason("gpt-6.1-sol", "ultra"), "unsupported-effort")
        self.assertIsNone(built.reject_reason("gpt-6.1-sol", "medium"))

    def test_a_user_ranking_or_pools_are_not_replaced_by_the_band(self):
        ranked = scored_candidates(ranking=["gpt-6.1-sol", "gpt-6-luna"], tier="high", floor_tier="low",
                                   deterministic=("gpt-6.1-sol", "high"), floor=("gpt-6-luna", "low"))
        self.assertEqual(ranked.mode, "ranked")
        pooled = scored_candidates(pools={"high": ["gpt-6-luna"]}, tier="high", floor_tier="high",
                                   deterministic=("gpt-6-luna", "high"), floor=("gpt-6-luna", "high"))
        self.assertEqual(pooled.mode, "pools")

    def test_the_old_claude_default_gets_the_band_too(self):
        built = build_candidates("claude", entry=CLAUDE_LIST, deterministic=Pair("sonnet", "high"), tier="high",
                                 floor_tier="medium", floor=Pair("sonnet", "medium"), quality_order=OLD_DEFAULT,
                                 allowed_models=[], effort_caps={}, tier_pools={}, scores=scored_set("claude"))
        self.assertEqual(built.mode, "scored")

    def test_no_scores_keeps_the_model_and_says_why(self):
        failed = scoring.failed("codex", EngineError(scoring.NO_SCORES, "damaged; pin one"))
        built = scored_candidates(scores=failed, tier="high", floor_tier="high", deterministic=("gpt-6-astra", "high"),
                                  floor=("gpt-6-astra", "high"))
        self.assertEqual((built.mode, built.note), ("deterministic", "model choice off: no ranking and no tier pools"))

    def test_pools_and_off_record_why(self):
        off = candidates(ranking=[], floor=("m-strong", "medium"))
        self.assertEqual((off.mode, off.note), ("deterministic", "model choice off: no ranking and no tier pools"))
        self.assertEqual(off.to_json()["note"], off.note)
        skipped = candidates(floor=("m-extra", "medium"), deterministic=("m-mid", "high"))
        self.assertEqual(skipped.note, "model choice off: quality_order does not rank m-extra and no tier pools")
        missing = candidates(ranking=[], floor=("m-strong", "medium"), pools={"medium": ["m-mid"]})
        self.assertEqual((missing.mode, missing.note), ("deterministic", "model choice off: no ranking and no tier pool for high"))
        pools = candidates(ranking=[], floor=("m-strong", "high"), floor_tier="high", caps={"m-extra": "medium"},
                           allowed=["m-strong", "m-mid", "m-light", "m-extra", "m-hidden"],
                           pools={"high": ["m-mid", "m-light", "m-extra", "m-hidden"]})
        self.assertEqual((pools.mode, pools.note), ("pools", None))
        self.assertEqual(pools.excluded, (("m-light", "below-floor"), ("m-extra", "above-cap"),
                                          ("m-hidden", "outside-candidates"), ("m-strong", "not-in-tier-pool")))
        data = pools.to_json()
        self.assertEqual((data["mode"], data["ranked"]), ("pools", False))
        self.assertEqual(data["tier_pools"], {"high": ["m-mid", "m-light", "m-extra", "m-hidden"]})
        self.assertEqual(data["excluded"], {"m-light": "below-floor", "m-extra": "above-cap",
                                            "m-hidden": "outside-candidates", "m-strong": "not-in-tier-pool"})
        self.assertEqual(data["candidates"], [["m-mid", "high"]])

    def test_check_caps_fresh_and_stale(self):
        self.assertEqual(check_caps("codex", {}, [], JUDGE_ENTRY, fresh=True), [])
        with self.assertRaises(EngineError) as ctx:
            check_caps("codex", {"m-typo": "high"}, [], JUDGE_ENTRY, fresh=True)
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")
        self.assertIn("m-typo", str(ctx.exception))
        self.assertIn("oah models", str(ctx.exception))
        warned = check_caps("codex", {"m-strong": "high", "m-typo": "low"}, [], JUDGE_ENTRY, fresh=True)
        self.assertEqual(len(warned), 1)
        self.assertTrue(warned[0].startswith("OAH_MODEL_CAP_UNKNOWN: "), warned)
        self.assertIn("m-typo", warned[0])
        stale = check_caps("codex", {"m-typo": "high"}, [], JUDGE_ENTRY, fresh=False)
        self.assertTrue(stale[0].startswith("OAH_MODEL_CAP_UNKNOWN: "), stale)
        hidden = check_caps("codex", {"m-hidden": "high", "m-mid": "high"}, [], JUDGE_ENTRY, fresh=True)
        self.assertIn("m-hidden", hidden[0])
        allowed = check_caps("codex", {}, ["m-mid", "m-typo"], JUDGE_ENTRY, fresh=True)
        self.assertEqual(len(allowed), 1)
        self.assertTrue(allowed[0].startswith("OAH_MODEL_ALLOWED_UNKNOWN: "), allowed)
        pooled = check_caps("codex", {}, [], JUDGE_ENTRY, fresh=True, pools={"high": ["m-mid", "m-typo"], "low": []})
        self.assertEqual(len(pooled), 1)
        self.assertTrue(pooled[0].startswith("OAH_MODEL_POOL_UNKNOWN: "), pooled)
        self.assertIn("tier_pools.high", pooled[0])
        self.assertIn("m-typo", pooled[0])


CODE = "OAH_MODEL_NEW_UNPOOLED"
POOLS = "[engines.codex.tier_pools]\n" + "".join(f'{tier} = ["m-old"]\n' for tier in TIERS)
LEVELS = ["low", "medium", "high"]


def notices(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(CODE)]


class UnpooledRuleTest(unittest.TestCase):
    def test_unpooled_models_table(self):
        table = [({}, []), (None, []), ({"low": [], "high": []}, ["m-extra", "m-light", "m-mid", "m-strong"]),
                 ({"high": ["m-strong"], "low": ["m-mid"]}, ["m-extra", "m-light"]),
                 ({"high": ["m-strong", "m-mid", "m-light", "m-extra"]}, []),
                 ({"high": ["m-hidden", "m-strong", "m-mid", "m-light"]}, ["m-extra"])]
        for pools, expected in table:
            with self.subTest(pools=pools):
                self.assertEqual(unpooled_models(JUDGE_ENTRY, pools), expected)
        self.assertEqual(unpooled_models(None, {"high": ["m-strong"]}), [])

    def test_only_plain_catalog_names_are_reported(self):
        entry = Entry("codex", "1", None, tuple(ModelInfo(name, ()) for name in
                      ("m-valid_1.2", "m-evil\x1b[2J", "m-new\nINJECTED", "x" * 129, "../model")), "test")
        self.assertEqual(unpooled_models(entry, {"high": []}), ["m-valid_1.2"])

    def test_the_warning_names_the_model_and_the_one_step(self):
        warning = unpooled_warning("codex", "m-extra")
        self.assertTrue(warning.startswith(f"{CODE}: "), warning)
        for text in ("m-extra", "[engines.codex.tier_pools]", ".oah/config.toml", "judge will not pick"):
            self.assertIn(text, warning)

    def test_check_caps_leaves_the_notice_to_check_catalog(self):
        for fresh in (True, False):
            with self.subTest(fresh=fresh):
                warned = check_caps("codex", {}, [], JUDGE_ENTRY, fresh=fresh, pools={"high": ["m-strong"]})
                self.assertEqual(warned, [])

    def test_build_candidates_never_offers_an_unpooled_model(self):
        pools = {"medium": ["m-mid"], "high": ["m-strong", "m-mid"]}
        table = [("ranked", dict(ranking=["m-extra", *RANKING])), ("pools", dict(ranking=[])),
                 ("deterministic", dict(ranking=[], deterministic=("m-extra", "high"), pools={"low": ["m-mid"]})),
                 ("pools", dict(ranking=[], allowed=["m-extra", "m-mid"]))]
        for mode, fields in table:
            with self.subTest(mode=mode, fields=fields):
                built = candidates(**{"pools": pools, **fields})
                self.assertEqual(built.mode, mode)
                self.assertNotIn("m-extra", {pair.model for pair in built.pairs})
                self.assertIsNotNone(built.reject_reason("m-extra", "high"))


class UnpooledNoticeTest(unittest.TestCase):
    def setUp(self):
        self.state, self.home = make_tmp_dir(self), make_tmp_dir(self)
        self.env = {"OAH_STATE_DIR": str(self.state), "CODEX_HOME": str(self.home)}
        write_cache(self.home, ("m-old", LEVELS, True), ("m-new", LEVELS, False))

    def cli(self, repo: pathlib.Path, *argv: str) -> tuple[int, str, str]:
        with inside(repo, self.env):
            return run_models(*argv)

    def test_new_model_in_text_and_json(self):
        repo = project_with_fakes(self, POOLS)
        code, out, err = self.cli(repo, "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)["engines"]["codex"]
        self.assertEqual(report["unpooled"], ["m-new"])
        self.assertEqual(list(report), ["cli_version", "checked_at", "source", "refreshed", "warnings", "models",
                                        "allowed_models", "effort_caps", "tier_pools", "tiers", "unpooled",
                                        "profile", "scores", "left_out"])
        self.assertEqual(len(notices("\n".join(report["warnings"]))), 1)
        self.assertIn("[engines.codex.tier_pools]", notices("\n".join(report["warnings"]))[0])
        self.assertEqual(len(notices(err)), 1)
        code, out, err = self.cli(repo)
        self.assertEqual(code, 0, err)
        self.assertEqual((out + err).count(CODE), 1, out + err)
        self.assertIn(".oah/config.toml", notices(err)[0])

    def test_no_notice_without_pools_for_hidden_or_pooled(self):
        cache = self.home / "models_cache.json"
        listed = cache.read_text(encoding="utf-8")
        data = json.loads(listed)
        data["models"][1]["visibility"] = "hide"
        both = '[engines.codex.tier_pools]\nhigh = ["m-old", "m-new"]\n'
        for extra, body in (("", listed), (POOLS, json.dumps(data)), (both, listed)):
            with self.subTest(extra=extra, body=body):
                cache.write_text(body, encoding="utf-8")
                code, out, err = self.cli(project_with_fakes(self, extra), "--json", "--refresh")
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["engines"]["codex"]["unpooled"], [])
                self.assertNotIn(CODE, out + err)

    def test_notice_survives_every_tier_failing_to_resolve(self):
        write_cache(self.home, ("m-a", LEVELS, False), ("m-b", LEVELS, False))
        repo = project_with_fakes(self, "[engines.codex.tier_pools]\n" + "".join(f'{tier} = ["m-gone"]\n' for tier in TIERS))
        code, out, err = self.cli(repo, "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)["engines"]["codex"]
        self.assertEqual({row.get("error", "").split(":")[0] for row in report["tiers"].values()},
                         {"OAH_MODEL_UNRESOLVED"})
        self.assertEqual(report["unpooled"], ["m-a", "m-b"])
        self.assertEqual(len(notices(err)), 2)

    def test_resolved_models_do_not_change_and_config_is_untouched(self):
        repo = project_with_fakes(self, POOLS)
        config = repo / ".oah" / "config.toml"
        before = config.read_bytes()
        write_cache(self.home, ("m-old", LEVELS, True))
        _, out, _ = self.cli(repo, "--json", "--refresh")
        without = json.loads(out)["engines"]["codex"]["tiers"]
        write_cache(self.home, ("m-old", LEVELS, True), ("m-new", LEVELS, False))
        _, out, _ = self.cli(repo, "--json", "--refresh")
        self.assertEqual(json.loads(out)["engines"]["codex"]["tiers"], without)
        self.assertEqual(config.read_bytes(), before)

    def test_reports_every_time_without_extra_state(self):
        repo = project_with_fakes(self, POOLS)
        config = repo / ".oah" / "config.toml"
        before = config.read_bytes()
        for argv in (("--refresh",), ("--refresh", "--json"), (), ("--json",), ()):
            code, _, err = self.cli(repo, *argv)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(notices(err)), 1)
        self.assertEqual({path.name for path in (self.state / "models").iterdir()},
                         {"catalog.json", "resolved.json", "changes.jsonl"})
        self.assertEqual(config.read_bytes(), before)

    def test_current_pools_apply_without_a_rebuild(self):
        self.cli(project_with_fakes(self), "--refresh")
        repo = project_with_fakes(self, POOLS)
        code, out, err = self.cli(repo, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(notices(err)), 1)
        self.assertIsNone(json.loads(out)["engines"]["codex"]["refreshed"])

    def test_empty_configured_pool_reports_every_listed_model(self):
        repo = project_with_fakes(self, '[engines.codex.tier_pools]\nhigh = []\n')
        code, out, err = self.cli(repo, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["engines"]["codex"]["unpooled"], ["m-new", "m-old"])
        self.assertEqual(len(notices(err)), 2)


def scored_project(test: unittest.TestCase, config: str = "", **fields):
    from tests import test_call as calls

    return calls.scored_project(test, config, **fields)


def run_in(project, *argv: str) -> tuple[int, str, str]:
    with inside(project.repo, project.env):
        return run_models(*argv)


def warned(warnings, code: str) -> list[str]:
    return [line for line in warnings if line.startswith(code + ":")]


class ScoreReportTest(unittest.TestCase):
    def report(self, *argv: str, **fields) -> dict:
        from tests import test_call as calls

        fields.setdefault("models", (*calls.SCORED_MODELS, "gpt-6-sol", "gpt-7-nova"))
        fields.setdefault("scores", calls.OLD_SCORES)
        project = scored_project(self, fields.pop("config", ""), **fields)
        code, out, err = run_in(project, "--json", *argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)["engines"]["codex"]

    def test_json_adds_the_profile_scores_and_what_was_left_out(self):
        report = self.report(hidden=("gpt-reserve",))
        self.assertEqual(report["profile"], "balanced")
        scores = report["scores"]
        self.assertEqual((scores["source"], scores["date"], scores["credit"]),
                         ("Epoch AI", "2026-11-02", "Epoch AI, a credit line."))
        self.assertEqual([row["model"] for row in scores["models"]],
                         ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"])
        self.assertEqual(scores["models"][0], {"model": "gpt-6-astra", "family": "astra", "score": 100.0, "cost": 60})
        self.assertEqual(report["left_out"], {"gpt-6-sol": "superseded", "gpt-7-nova": "unknown-family"})

    def test_every_tier_row_keeps_its_keys(self):
        row = self.report()["tiers"]["high"]
        self.assertEqual(list(row), ["model", "model_source", "requested_effort", "effort", "floor", "mode", "note",
                                     "candidates", "excluded"])
        self.assertEqual((row["model"], row["model_source"], row["mode"]), ("gpt-6.1-terra", "scored", "scored"))

    def test_the_text_report_credits_the_source_and_names_each_model_left_out(self):
        from tests import test_call as calls

        project = scored_project(self, models=(*calls.SCORED_MODELS, "gpt-6-sol"), scores=calls.OLD_SCORES)
        code, out, err = run_in(project)
        self.assertEqual(code, 0, err)
        self.assertIn("profile balanced", out)
        self.assertIn("scores Epoch AI 2026-11-02", out)
        self.assertIn("credit: Epoch AI, a credit line.", out)
        self.assertIn("gpt-6-astra 100.0", out)
        self.assertIn("left out: gpt-6-sol (superseded)", out)

    def test_the_profile_is_reported_and_moves_the_tiers(self):
        report = self.report(config='[models]\nprofile = "performance"\n')
        self.assertEqual(report["profile"], "performance")
        self.assertEqual(report["tiers"]["high"]["model"], "gpt-6-astra")

    def test_a_catalog_name_that_is_not_plain_is_never_echoed(self):
        from tests import test_call as calls

        bad = "gpt-6\x1b[2J-sol"
        report = self.report(models=(*calls.SCORED_MODELS, bad))
        self.assertNotIn(bad, report["left_out"])

    def test_damaged_data_is_named_with_no_traceback_and_no_default(self):
        from tests import test_call as calls

        project = calls.Project(self, "quality_order = []\n")
        calls.write_models(project.home)
        with mock.patch.object(scoring, "_packaged", side_effect=OSError("gone")):
            code, out, err = run_in(project, "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)["engines"]["codex"]
        self.assertIsNone(report["scores"])
        self.assertEqual(report["left_out"], {})
        self.assertEqual({row["error"].split(":")[0] for row in report["tiers"].values()}, {"OAH_MODEL_NO_SCORES"})
        self.assertTrue(warned(report["warnings"], "OAH_MODEL_NO_SCORES"))
        self.assertNotIn("Traceback", out + err)

    def test_the_old_default_list_is_reported_as_ignored_and_the_file_stays(self):
        from tests import test_call as calls

        project = calls.Project(self)
        config = project.repo / ".oah" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace(
            'quality_order = ["opus", "sonnet"]', 'quality_order = ["opus", "sonnet", "haiku"]'), encoding="utf-8")
        before = config.read_bytes()
        code, out, err = run_in(project, "--json")
        self.assertEqual(code, 0, err)
        claude_report = json.loads(out)["engines"]["claude"]
        self.assertEqual(len(warned(claude_report["warnings"], "OAH_MODEL_ORDER_IGNORED")), 1)
        self.assertIn("quality_order", warned(claude_report["warnings"], "OAH_MODEL_ORDER_IGNORED")[0])
        self.assertFalse(warned(json.loads(out)["engines"]["codex"]["warnings"], "OAH_MODEL_ORDER_IGNORED"))
        self.assertEqual(config.read_bytes(), before)

    def test_a_real_list_is_not_reported_as_ignored(self):
        from tests import test_call as calls

        code, out, err = run_in(calls.Project(self), "--json")
        self.assertEqual(code, 0, err)
        self.assertFalse(warned(json.loads(out)["engines"]["claude"]["warnings"], "OAH_MODEL_ORDER_IGNORED"))


class RebuildNoticeTest(unittest.TestCase):
    TWO = ("gpt-6-luna", "gpt-6.1-sol")
    NEWER = "gpt-6.2-sol"

    def project(self, *, config: str = "", models=TWO, order: str = "[]", extra: float | None = 0.99):
        from tests import test_call as calls

        scores = dict(calls.SCORES["scores"])
        if extra is not None:
            scores[self.NEWER] = dict.fromkeys(calls.FOUR, extra)
        project = scored_project(self, config, models=models, order=order, scores=dict(calls.SCORES, scores=scores))
        self.first = project.prepare(stage="correction", tier="high")
        return project

    def rebuild(self, project, models, *, version: str = "0.164.0", **fields):
        from tests import test_call as calls

        calls.write_models(project.home, models)
        return project.prepare(stage="correction", tier="high", env={"FAKE_CODEX_VERSION": version}, **fields)

    def test_a_newer_generation_warns_once_per_step_and_is_logged(self):
        project = self.project()
        prepared = self.rebuild(project, (*self.TWO, self.NEWER))
        changed = warned(prepared.warnings, "OAH_MODEL_CHANGED")
        self.assertEqual([line.split(";")[0] for line in changed],
                         [f"OAH_MODEL_CHANGED: codex step {step} now uses {self.NEWER} (was gpt-6.1-sol): newer generation"
                          for step in (1, 2, 3)])
        self.assertIn("oah models", changed[0])
        logged = [line for line in Catalog(project.state).changes() if "step" in line]
        self.assertEqual([(line["step"], line["field"], line["old"], line["new"], line["why"]) for line in logged],
                         [(step, "model", "gpt-6.1-sol", self.NEWER, "newer generation") for step in (1, 2, 3)])

    def test_it_appears_once_across_repeated_launches(self):
        project = self.project()
        self.rebuild(project, (*self.TWO, self.NEWER))
        for _ in range(3):
            prepared = project.prepare(stage="correction", tier="high", env={"FAKE_CODEX_VERSION": "0.164.0"})
            self.assertEqual(warned(prepared.warnings, "OAH_MODEL_CHANGED"), [])
        self.assertEqual(len([line for line in Catalog(project.state).changes() if "step" in line]), 3)

    def test_a_model_that_left_the_list_says_so(self):
        project = self.project(models=("gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra"))
        changed = warned(self.rebuild(project, ("gpt-6-luna", "gpt-6.1-sol")).warnings, "OAH_MODEL_CHANGED")
        self.assertTrue(changed)
        self.assertIn("gpt-6.1-terra is no longer listed", changed[0])

    def test_a_rebuild_that_changes_nothing_stays_silent(self):
        project = self.project()
        before = len(Catalog(project.state).changes())
        prepared = self.rebuild(project, self.TWO)
        self.assertEqual(warned(prepared.warnings, "OAH_MODEL_CHANGED"), [])
        self.assertEqual(len(Catalog(project.state).changes()), before)

    def test_oah_models_can_be_the_command_that_rebuilds(self):
        from tests import test_call as calls

        project = self.project()
        calls.write_models(project.home, (*self.TWO, self.NEWER))
        code, out, err = run_in(project, "--refresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("OAH_MODEL_CHANGED:"), 3)
        self.assertEqual(warned(project.prepare(stage="correction", tier="high").warnings, "OAH_MODEL_CHANGED"), [])

    def test_a_user_who_chooses_models_hears_nothing_about_the_rule(self):
        for config, order in (("", '["gpt-6.1-sol"]'),
                              ("".join(f'[engines.codex.tiers.{tier}]\nmodel = "gpt-6.1-sol"\n' for tier in TIERS), "[]")):
            with self.subTest(order=order):
                project = self.project(config=config, order=order)
                self.assertEqual(warned(self.rebuild(project, (*self.TWO, self.NEWER)).warnings, "OAH_MODEL_CHANGED"), [])

    def test_one_rebuild_reads_the_data_once_for_both_entries(self):
        import collections

        project = self.project()
        reads = collections.Counter()
        real = scoring._packaged

        def spy(name):
            reads[name] += 1
            return real(name)

        with mock.patch.object(scoring, "_packaged", side_effect=spy):
            self.rebuild(project, (*self.TWO, self.NEWER))
        self.assertEqual(reads["families.json"], 1)

    def test_a_rebuild_names_an_unknown_family(self):
        project = self.project()
        prepared = self.rebuild(project, (*self.TWO, "gpt-7-nova"))
        found = warned(prepared.warnings, "OAH_MODEL_UNKNOWN_FAMILY")
        self.assertEqual(len(found), 1)
        self.assertIn("gpt-7-nova", found[0])
        self.assertIn(".oah/config.toml", found[0])
        self.assertEqual(warned(project.prepare(stage="correction", tier="high",
                                                env={"FAKE_CODEX_VERSION": "0.164.0"}).warnings,
                                "OAH_MODEL_UNKNOWN_FAMILY"), [])

    def test_a_rebuild_names_an_unscored_model_and_the_model_that_stays(self):
        project = self.project(extra=None)
        found = warned(self.rebuild(project, (*self.TWO, self.NEWER)).warnings, "OAH_MODEL_UNSCORED")
        self.assertEqual(len(found), 1)
        self.assertIn(self.NEWER, found[0])
        self.assertIn("gpt-6.1-sol stays", found[0])

    def test_a_name_that_is_not_plain_is_left_out_of_rebuild_notices(self):
        project = self.project()
        bad = "gpt-7\x1b[2J-nova"
        prepared = self.rebuild(project, (*self.TWO, bad))
        self.assertFalse([line for line in prepared.warnings if "\x1b" in line])

    def test_a_correction_plans_the_change_lines_with_its_other_writes(self):
        from tests import test_call as calls
        from oah import call

        project = self.project()
        calls.write_models(project.home, (*self.TWO, self.NEWER))
        before = {path: path.read_bytes() for path in (project.state / "models").iterdir()}
        token = call._CORRECTION_INPUT_CHECK.set(lambda value: None)
        try:
            catalog = call._CorrectionCatalog(project.state)
            inputs_token = call._CORRECTION_INPUTS.set(mock.Mock(catalog=catalog))
            try:
                _, freshness = call.check_catalog("codex", project.settings(), state_dir=project.state,
                                                  env=project.env, refresh=True)
                catalog.record_resolution("codex", "high", model="gpt-6.2-sol", effort="high", source="scored", why="x")
            finally:
                call._CORRECTION_INPUTS.reset(inputs_token)
        finally:
            call._CORRECTION_INPUT_CHECK.reset(token)
        self.assertEqual(len(warned(freshness.warnings, "OAH_MODEL_CHANGED")), 3)
        self.assertEqual({path: path.read_bytes() for path in (project.state / "models").iterdir()}, before)
        planned = catalog.writes[catalog.changes_path]
        self.assertEqual(planned.count('"step"'), 3, "the resolution line must not overwrite the planned change lines")
        self.assertIn('"tier": "high"', planned)


class NoUserFileTest(unittest.TestCase):
    def test_nothing_here_edits_the_project_config(self):
        from tests import test_call as calls
        from tests import test_routing as routing_tests

        project = scored_project(self)
        config = project.repo / ".oah" / "config.toml"
        before = config.read_bytes()
        run_in(project, "--refresh")
        project.run(stage="review", tier="medium")
        calls.write_models(project.home, ("gpt-6-luna", "gpt-6.1-sol"))
        project.run(stage="review", tier="medium", env={"FAKE_CODEX_VERSION": "0.164.0"})
        routing_tests.recommend_in(project, "--kind", "review", "--intensity", "low", "--reasoning", "low",
                                   "--risk", "low", "--engine", "codex", "--stage", "review")
        self.assertEqual(config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
