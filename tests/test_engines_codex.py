# Codex 어댑터 검사 — models_cache 파싱, 유효 설정의 모델 해석(프로필 포함), 명령줄, 가짜 codex 헤더의 일치·낮춤·불일치·미확인을 본다.
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest

from oah.engines import codex
from oah.engines.effort import EngineError
from oah.engines.proc import run_command
from tests.helpers import make_tmp_dir

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_CODEX = [sys.executable, str(FAKES / "fake_codex.py")]


def cache_text(*models: dict, crlf: bool = False) -> str:
    text = json.dumps({"fetched_at": "2026-10-23T00:00:00Z", "client_version": "0.163.0", "models": list(models)},
                      indent=1)
    return text.replace("\n", "\r\n") if crlf else text


def model(slug: str, levels, **extra) -> dict:
    return dict({"slug": slug, "supported_reasoning_levels": [{"effort": level, "description": "d"} for level in levels]},
                **extra)


class ModelsCacheTest(unittest.TestCase):
    def test_parse_models_cache(self):
        parsed = codex.parse_models_cache(cache_text(
            model("gpt-test-a", ["low", "medium", "high", "xhigh", "max", "ultra"]),
            {"slug": "gpt-test-b", "supported_reasoning_levels": ["low", "high"], "is_default": True},
            {"slug": "gpt-test-c"}, crlf=True))
        self.assertEqual(parsed["gpt-test-a"].levels, ("low", "medium", "high", "xhigh", "max", "ultra"))
        self.assertFalse(parsed["gpt-test-a"].default)
        self.assertEqual(parsed["gpt-test-b"].levels, ("low", "high"))
        self.assertTrue(parsed["gpt-test-b"].default)
        self.assertEqual(parsed["gpt-test-c"].levels, ())

    def test_parse_rejects_non_json(self):
        with self.assertRaises(ValueError):
            codex.parse_models_cache("{not json")
        self.assertEqual(codex.parse_models_cache(json.dumps({"models": "nope"})), {})

    def test_read_models_cache(self):
        home = make_tmp_dir(self)
        self.assertIsNone(codex.read_models_cache(home))
        (home / "models_cache.json").write_text(cache_text(model("gpt-test-a", ["low"])), encoding="utf-8")
        self.assertEqual(list(codex.read_models_cache(home)), ["gpt-test-a"])
        (home / "models_cache.json").write_text("garbage", encoding="utf-8")
        self.assertIsNone(codex.read_models_cache(home))

    def test_codex_home(self):
        self.assertEqual(codex.codex_home({"CODEX_HOME": "/x/y"}), pathlib.Path("/x/y"))
        self.assertEqual(codex.codex_home({}).name, ".codex")


class ResolveEffortTest(unittest.TestCase):
    def setUp(self):
        self.cache = codex.parse_models_cache(cache_text(
            model("gpt-test-a", ["low", "medium", "high", "xhigh", "max", "ultra"]), {"slug": "gpt-test-c"}))

    def test_resolve_effort_from_cache(self):
        self.assertEqual(codex.resolve_effort("gpt-test-a", "highest", cache=self.cache).launch, "max")
        self.assertEqual(codex.resolve_effort("gpt-test-a", "xhigh", cache=self.cache).launch, "xhigh")
        with self.assertRaises(EngineError) as ctx:
            codex.resolve_effort("gpt-test-a", "minimal", cache=self.cache)
        self.assertEqual(ctx.exception.code, "OAH_EFFORT_UNSUPPORTED")

    def test_missing_metadata(self):
        for name, cache in (("gpt-test-a", None), ("absent", self.cache), ("gpt-test-c", self.cache)):
            with self.subTest(name=name):
                literal = codex.resolve_effort(name, "max", cache=cache)
                self.assertEqual((literal.launch, literal.metadata), ("max", "missing"))
                with self.assertRaises(EngineError) as ctx:
                    codex.resolve_effort(name, "highest", cache=cache)
                self.assertEqual(ctx.exception.code, "OAH_EFFORT_METADATA_MISSING")


class EffectiveModelTest(unittest.TestCase):
    def test_effective_config_layers_and_profile(self):
        root = make_tmp_dir(self)
        home, project = root / "codex-home", root / "project"
        home.mkdir()
        (project / ".codex").mkdir(parents=True)
        self.assertIsNone(codex.effective_model(project, home))
        (home / "config.toml").write_text('model = "gpt-user"\r\n[profiles.deep]\r\nmodel = "gpt-deep"\r\n',
                                          encoding="utf-8")
        self.assertEqual(codex.effective_model(project, home), "gpt-user")
        (project / ".codex" / "config.toml").write_text('model = "gpt-project"\n', encoding="utf-8")
        self.assertEqual(codex.effective_model(project, home), "gpt-project")
        (project / ".codex" / "config.toml").write_text('model = "gpt-project"\nprofile = "deep"\n', encoding="utf-8")
        self.assertEqual(codex.effective_model(project, home), "gpt-deep")

    def test_selected_profile_without_model_keeps_base(self):
        root = make_tmp_dir(self)
        (root / "config.toml").write_text('model = "gpt-user"\nprofile = "fast"\n[profiles.fast]\n', encoding="utf-8")
        self.assertEqual(codex.effective_model(root / "nowhere", root), "gpt-user")

    def test_invalid_config_refuses(self):
        root = make_tmp_dir(self)
        (root / "config.toml").write_text("model = \n", encoding="utf-8")
        with self.assertRaises(EngineError) as ctx:
            codex.effective_model(root, root)
        self.assertEqual(ctx.exception.code, "OAH_CODEX_CONFIG_INVALID")


class CommandTest(unittest.TestCase):
    def test_build_command_work(self):
        argv = codex.build_command(["codex"], workdir="/w", read_only=False, model="gpt-test-a", effort="high",
                                   output_last_message="/r/last.md")
        self.assertEqual(argv, ["codex", "exec", "-C", "/w", "-s", "workspace-write", "-m", "gpt-test-a",
                                "-c", 'model_reasoning_effort="high"', "-o", "/r/last.md", "-"])

    def test_review_is_read_only_codex(self):
        argv = codex.build_command(["node.exe", "codex.js"], workdir="/w", read_only=True, model="m", effort="max",
                                   output_last_message="/o", output_schema="/s.json", ephemeral=True)
        self.assertEqual(argv[:6], ["node.exe", "codex.js", "exec", "-C", "/w", "-s"])
        self.assertEqual(argv[6], "read-only")
        self.assertEqual(argv[-6:], ["--output-schema", "/s.json", "-o", "/o", "--ephemeral", "-"])
        self.assertNotIn("workspace-write", argv)

    def test_command_refuses_bad_values(self):
        with self.assertRaises(EngineError):
            codex.build_command(["codex"], workdir="/w", read_only=True, model="m", effort='x"; y="z',
                                output_last_message="/o")
        with self.assertRaises(EngineError):
            codex.build_command(["codex"], workdir="/w", read_only=True, model="", effort="high",
                                output_last_message="/o")

    def test_parse_version(self):
        self.assertEqual(codex.parse_version("codex-cli 0.163.0\n"), "0.163.0")
        self.assertIsNone(codex.parse_version("something else"))


def run_fake(test: unittest.TestCase, header: str | None, launch: str = "high") -> str:
    work = make_tmp_dir(test)
    env = dict(os.environ)
    env.pop("FAKE_CODEX_HEADER_EFFORT", None)
    if header is not None:
        env["FAKE_CODEX_HEADER_EFFORT"] = header
    argv = codex.build_command(FAKE_CODEX, workdir=str(work), read_only=True, model="gpt-test-a", effort=launch,
                               output_last_message=str(work / "last.md"))
    result = run_command(argv, timeout=30, env=env, stdin_text="review this")
    test.assertEqual(result.returncode, 0, result.stderr)
    return result.stderr


class HeaderWithFakeCodexTest(unittest.TestCase):
    def test_matching_header(self):
        check = codex.check_header(run_fake(self, None), "high")
        self.assertEqual((check.header_effort, check.status, check.code, check.header_model),
                         ("high", "match", None, "gpt-test-a"))

    def test_lower_header_is_downgraded(self):
        check = codex.check_header(run_fake(self, "medium"), "high")
        self.assertEqual((check.status, check.code), ("downgraded", "OAH_EFFORT_DOWNGRADED"))

    def test_other_mismatch(self):
        check = codex.check_header(run_fake(self, "max"), "high")
        self.assertEqual((check.status, check.code), ("mismatch", "OAH_EFFORT_MISMATCH"))

    def test_ultra_header_is_a_mismatch_not_a_rank(self):
        check = codex.check_header(run_fake(self, "ultra", launch="max"), "max")
        self.assertEqual((check.status, check.code), ("mismatch", "OAH_EFFORT_MISMATCH"))

    def test_unreadable_header(self):
        check = codex.check_header(run_fake(self, "-"), "high")
        self.assertEqual((check.header_effort, check.status, check.code), ("unverified", "unverified", None))

    def test_crlf_header(self):
        text = "OpenAI Codex\r\n--------\r\nmodel: gpt-x\r\nreasoning effort: high\r\n--------\r\n"
        check = codex.check_header(text, "high")
        self.assertEqual((check.header_effort, check.status, check.header_model), ("high", "match", "gpt-x"))


if __name__ == "__main__":
    unittest.main()
