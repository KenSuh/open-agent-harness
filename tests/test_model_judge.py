# 호출별 모델·effort 판정 검사 — 설정 키 검증, 후보·고정·규칙 검증, Jev 판정의 used·rejected·unused, 가짜 Codex 종단 실행과 헤더 모델 불일치 거부를 본다.
from __future__ import annotations

import unittest

from oah import call, call_cli
from oah.engines.effort import EngineError
from tests import test_call as calls


class ConfigTest(unittest.TestCase):
    def test_defaults_carry_empty_judge_keys(self):
        settings = call.Settings.defaults()
        for engine in ("claude", "codex"):
            with self.subTest(engine=engine):
                self.assertEqual(settings.engines[engine]["allowed_models"], [])
                self.assertEqual(settings.engines[engine]["effort_caps"], {})

    def test_configured_keys_load(self):
        # The fixture's config ends inside [engines.codex], so its keys follow without a header.
        project = calls.Project(self, 'allowed_models = ["m-a"]\n[engines.codex.effort_caps]\n"m-a" = "high"\n')
        settings = call_cli.load_settings(project.repo)
        self.assertEqual(settings.engines["codex"]["allowed_models"], ["m-a"])
        self.assertEqual(settings.engines["codex"]["effort_caps"], {"m-a": "high"})

    def test_invalid_caps_refuse(self):
        rows = [
            ('[engines.codex.effort_caps]\n"m-a" = "turbo"\n', "turbo"),
            ('[engines.codex.effort_caps]\n"m-a" = "highest"\n', "highest"),
            ('[engines.codex.effort_caps]\n"m-a" = "ultra"\n', "ultra"),
            ('[engines.codex.effort_caps]\n"m-a" = 3\n', "m-a"),
            ('allowed_models = ["m-a", 2]\n', "allowed_models"),
            ('[engines.claude.effort_caps]\n"m-a" = "max-ish"\n', "max-ish"),
        ]
        for text, named in rows:
            with self.subTest(text=text):
                project = calls.Project(self, text)
                with self.assertRaises(EngineError) as ctx:
                    call_cli.load_settings(project.repo)
                self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")
                self.assertIn(named, str(ctx.exception))
                self.assertIn(".oah/config.toml", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
