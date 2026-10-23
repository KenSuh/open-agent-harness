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
from oah.models import Catalog, Entry, ModelInfo, map_tier, resolve_base
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


if __name__ == "__main__":
    unittest.main()
