# 모델 카탈로그 검사 — Claude 별칭·Codex 유효 기본값·CLI 기본 모델 채택과 기록·사용자 품질 목록·고정 우선·메타데이터 유실·새 설치 거부·oah models 출력을 본다.
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

from oah import models_cli
from oah.engines.effort import EngineError
from oah.engines import codex
from oah.models import (Catalog, Entry, ModelInfo, Pair, build_candidates, check_caps, map_tier,
                        resolve_base)
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


CLAUDE_ORDER = ["opus", "sonnet", "haiku"]


class ClaudeMappingTest(unittest.TestCase):
    def test_claude_alias_mapping(self):
        fixture = Fixture(self)
        entry = fixture.fresh("claude").entry
        self.assertEqual([model.name for model in entry.models], ["fable", "opus", "sonnet"])
        self.assertEqual(entry.model("opus").efforts, ("low", "medium", "high", "xhigh", "max"))
        mapped = {tier: map_tier("claude", tier, pin="", quality_order=CLAUDE_ORDER, entry=entry, effective=None)
                  for tier in TIERS}
        self.assertEqual(mapped, {"low": ("sonnet", "quality_order"), "medium": ("sonnet", "quality_order"),
                                  "high": ("opus", "quality_order"), "max": ("opus", "quality_order")})

    def test_three_aliases_use_last_second_first(self):
        fixture = Fixture(self)
        entry = fixture.fresh("claude", FAKE_CLAUDE_ALIASES="haiku,sonnet,opus").entry
        self.assertEqual([map_tier("claude", t, pin="", quality_order=CLAUDE_ORDER, entry=entry, effective=None)[0]
                          for t in TIERS], ["haiku", "sonnet", "opus", "opus"])

    def test_one_alias_serves_every_tier(self):
        entry = Entry("claude", "1", "x", (ModelInfo("opus", ()),), "test")
        self.assertEqual({map_tier("claude", t, pin="", quality_order=CLAUDE_ORDER, entry=entry, effective=None)[0]
                          for t in TIERS}, {"opus"})

    def test_unreadable_help_uses_configured_aliases_unconfirmed(self):
        fixture = Fixture(self)
        freshness = fixture.fresh("claude", FAKE_CLAUDE_ALIASES="-")
        self.assertTrue(any(w.startswith("OAH_MODELS_REFRESH_FAILED") for w in freshness.warnings))
        self.assertEqual(map_tier("claude", "low", pin="", quality_order=CLAUDE_ORDER, entry=freshness.entry,
                                  effective=None), ("haiku", "quality_order(unconfirmed)"))


class CodexMappingTest(unittest.TestCase):
    def test_codex_effective_default(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low", "high"], False), ("gpt-b", ["low"], False))
        entry = fixture.fresh("codex").entry
        for tier in TIERS:
            self.assertEqual(map_tier("codex", tier, pin="", quality_order=[], entry=entry, effective="gpt-cfg"),
                             ("gpt-cfg", "effective_config"))

    def test_new_default_model_adopted_and_logged(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low", "high", "max"], True))
        entry = fixture.fresh("codex").entry
        model, source = map_tier("codex", "high", pin="", quality_order=[], entry=entry, effective="gpt-cfg")
        self.assertEqual((model, source), ("gpt-a", "cli_default"))
        fixture.catalog.record_resolution("codex", "high", model=model, effort="high", source=source,
                                          why="first launch", now=NOW)
        # The CLI updates: a newer model is now marked as default.
        write_cache(fixture.home, ("gpt-a", ["low", "high", "max"], False), ("gpt-b", ["low", "high", "max"], True))
        freshness = fixture.fresh("codex", now=NOW + dt.timedelta(hours=1), FAKE_CODEX_VERSION="0.164.0")
        self.assertEqual(freshness.refreshed, "CLI version changed 0.163.0 -> 0.164.0")
        model, source = map_tier("codex", "high", pin="", quality_order=[], entry=freshness.entry, effective="gpt-cfg")
        self.assertEqual(model, "gpt-b")
        changes = fixture.catalog.record_resolution("codex", "high", model=model, effort="high", source=source,
                                                    why=freshness.refreshed, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(changes, [{"ts": "2026-10-23T13:00:00Z", "engine": "codex", "tier": "high",
                                    "field": "model", "old": "gpt-a", "new": "gpt-b",
                                    "why": "CLI version changed 0.163.0 -> 0.164.0"}])
        logged = fixture.catalog.changes()
        self.assertEqual([(c["field"], c["old"], c["new"]) for c in logged],
                         [("model", None, "gpt-a"), ("effort", None, "high"), ("model", "gpt-a", "gpt-b")])
        # Recording the same values again logs nothing.
        self.assertEqual(fixture.catalog.record_resolution("codex", "high", model="gpt-b", effort="high",
                                                           source=source, why="again"), [])

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
        write_cache(fixture.home, ("gpt-c", ["low"], True))
        forced = fixture.fresh("codex", refresh=True, now=NOW + dt.timedelta(hours=24, minutes=1))
        self.assertEqual((forced.refreshed, forced.entry.models[0].name), ("refresh requested", "gpt-c"))

    def test_user_quality_list_for_codex(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low"], True), ("gpt-b", ["low"], False))
        entry = fixture.fresh("codex").entry
        order = ["gpt-z", "gpt-b", "gpt-a"]
        self.assertEqual([map_tier("codex", t, pin="", quality_order=order, entry=entry, effective="gpt-cfg")[0]
                          for t in TIERS], ["gpt-a", "gpt-a", "gpt-b", "gpt-b"])
        # A listed model the CLI does not report is not adopted; the effective config is kept.
        self.assertEqual(map_tier("codex", "high", pin="", quality_order=["gpt-z"], entry=entry, effective="gpt-cfg"),
                         ("gpt-cfg", "effective_config"))

    def test_pin_wins_over_catalog(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low"], True))
        entry = fixture.fresh("codex").entry
        self.assertEqual(map_tier("codex", "max", pin="gpt-pinned", quality_order=["gpt-a"], entry=entry,
                                  effective="gpt-cfg"), ("gpt-pinned", "pin"))
        self.assertEqual(map_tier("claude", "low", pin="opus", quality_order=CLAUDE_ORDER, entry=None,
                                  effective=None), ("opus", "pin"))

    def test_fresh_install_refuses(self):
        fixture = Fixture(self)
        freshness = fixture.fresh("codex")
        self.assertEqual(freshness.entry.models, ())
        self.assertTrue(any(w.startswith("OAH_MODELS_REFRESH_FAILED") for w in freshness.warnings))
        with self.assertRaises(EngineError) as ctx:
            map_tier("codex", "high", pin="", quality_order=[], entry=freshness.entry, effective=None)
        self.assertEqual(ctx.exception.code, "OAH_MODEL_UNRESOLVED")
        self.assertIn(".oah/config.toml", str(ctx.exception))

    def test_lost_metadata_keeps_last_good_mapping(self):
        fixture = Fixture(self)
        write_cache(fixture.home, ("gpt-a", ["low", "high"], True))
        fixture.fresh("codex")
        (fixture.home / "models_cache.json").unlink()
        freshness = fixture.fresh("codex", FAKE_CODEX_VERSION="0.164.0")
        self.assertIsNone(freshness.refreshed)
        self.assertTrue(any("keeping the catalog checked at 2026-10-23T12:00:00Z" in w for w in freshness.warnings))
        self.assertEqual(map_tier("codex", "high", pin="", quality_order=[], entry=freshness.entry, effective=None),
                         ("gpt-a", "cli_default"))
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
        repo = project_with_fakes(self, '[engines.codex.tiers.low]\nmodel = "gpt-pinned"\n')
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
        self.assertEqual(data["engines"]["claude"]["tiers"]["high"]["model"], "opus")
        self.assertIn("gpt-a", [m["name"] for m in data["engines"]["codex"]["models"]])

    def test_models_cli_reports_unresolved_tier(self):
        repo = project_with_fakes(self)
        state, home = make_tmp_dir(self), make_tmp_dir(self)
        with inside(repo, {"OAH_STATE_DIR": str(state), "CODEX_HOME": str(home)}):
            code, out, err = run_models()
        self.assertEqual(code, 0)
        self.assertIn("OAH_MODEL_UNRESOLVED", out)
        self.assertIn("OAH_MODELS_REFRESH_FAILED", err)


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
               allowed=(), caps=None, floor_tier="medium"):
    return build_candidates("codex", entry=JUDGE_ENTRY, deterministic=Pair(*deterministic), floor_tier=floor_tier,
                            floor=Pair(*floor), quality_order=list(ranking), allowed_models=list(allowed),
                            effort_caps=dict(caps or {}))


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
        self.assertTrue(old.models[0].listed)

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
        ]
        for name, options, expected in rows:
            with self.subTest(name):
                built = candidates(**options)
                self.assertEqual(pairs(built), expected)
                for pair in built.pairs:
                    self.assertIsNone(built.reject_reason(pair.model, pair.effort))

    def test_reject_reasons_table(self):
        ranked, unranked = candidates(caps={"m-strong": "high"}), candidates(ranking=[], floor=("m-strong", "medium"))
        allow = candidates(allowed=["m-strong"])
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
        ]
        for built, (model, effort), rule in rows:
            with self.subTest(model=model, effort=effort):
                self.assertEqual(built.reject_reason(model, effort), rule)

    def test_candidate_set_json_names_its_inputs(self):
        data = candidates(caps={"m-strong": "high"}, allowed=["m-strong", "m-mid"]).to_json()
        self.assertEqual(data["deterministic"], {"model": "m-strong", "effort": "high"})
        self.assertEqual(data["floor"], {"tier": "medium", "model": "m-mid", "effort": "medium"})
        self.assertTrue(data["ranked"])
        self.assertEqual(data["caps"], {"m-strong": "high"})
        self.assertEqual(data["allowed_models"], ["m-strong", "m-mid"])
        self.assertIn(["m-mid", "high"], data["candidates"])

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


if __name__ == "__main__":
    unittest.main()
