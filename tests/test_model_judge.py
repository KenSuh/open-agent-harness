# 호출별 모델·effort 판정 검사 — 설정 키 검증, 후보·고정·규칙 검증, Jev 판정의 used·rejected·unused, 가짜 Codex 종단 실행과 헤더 모델 불일치 거부를 본다.
from __future__ import annotations

import secrets as token_source
import unittest

from oah import call, call_cli, rerank
from oah.engines.effort import EngineError
from oah.secrets import NoKeychain
from tests import test_call as calls

KEY_NAME = "FAKE_JUDGE_KEY"
REQUEST = {"stage": "review", "tier": "high", "task": "not for the provider", "scope": ["src/secret_path.py"],
           "axes": {"kind": "review", "intensity": "low", "reasoning": "high", "risk": "normal", "tier": "high"}}
PAIRS = (("m-strong", "high"), ("m-strong", "max"), ("m-mid", "high"))


def fake_key() -> str:
    """A key-shaped value generated at runtime; never a real credential."""
    return "fake-" + token_source.token_hex(16)


class FakeJudge:
    """A re-ranker provider whose model answer (or error) the test sets."""

    name = "fake"

    def __init__(self, answer=("m-strong", "max"), error: Exception | None = None) -> None:
        self.answer, self.error, self.calls = answer, error, []

    def key_name(self, config) -> str:
        return KEY_NAME

    def choose_model(self, request, candidates, *, key, config):
        self.calls.append((request, tuple(candidates)))
        if self.error is not None:
            raise self.error
        return self.answer


def judge_config(**overrides) -> dict:
    return dict(rerank.DEFAULTS, enabled=True, provider="fake", **overrides)


class JudgeGateTest(unittest.TestCase):
    def judge(self, provider, *, config=None, pairs=PAIRS, env=None, disabled=False) -> rerank.ModelJudgement:
        env = {KEY_NAME: fake_key()} if env is None else env
        return rerank.judge_model(REQUEST, pairs, config=judge_config() if config is None else config,
                                  brief_disabled=disabled, env=env, keychain=NoKeychain(),
                                  providers={"fake": provider})

    def test_used_answer_and_the_data_the_provider_gets(self):
        provider = FakeJudge()
        judged = self.judge(provider)
        self.assertEqual((judged.status, judged.proposal, judged.provider), ("used", ("m-strong", "max"), "fake"))
        request, candidates = provider.calls[0]
        self.assertEqual(request, {"stage": "review", "tier": "high", "axes": {
            "kind": "review", "intensity": "low", "reasoning": "high", "risk": "normal"}})
        self.assertEqual(candidates, PAIRS)

    def test_gate_and_candidate_count_statuses(self):
        rows = [
            ("disabled-by-brief", {"disabled": True}),
            ("not-enabled", {"config": dict(rerank.DEFAULTS)}),
            ("no key", {"env": {}}),
            ("no-candidates", {"pairs": ()}),
            ("one-candidate", {"pairs": PAIRS[:1]}),
            ("too-many-candidates", {"config": judge_config(max_candidates=2)}),
        ]
        for reason, options in rows:
            with self.subTest(reason):
                provider = FakeJudge()
                judged = self.judge(provider, **options)
                self.assertEqual((judged.status, judged.proposal), (f"unused({reason})", None))
                self.assertEqual(provider.calls, [])

    def test_errors_and_malformed_answers_are_unused(self):
        rows = [
            (FakeJudge(error=rerank.ProviderError("timeout")), "unused(error:timeout)"),
            (FakeJudge(error=rerank.ProviderError("rate-limited")), "unused(error:rate-limited)"),
            (FakeJudge(error=RuntimeError("boom")), "unused(error:internal)"),
            (FakeJudge(answer=("m-strong",)), "unused(error:bad-response)"),
            (FakeJudge(answer=["m-strong", "max"]), "unused(error:bad-response)"),
            (FakeJudge(answer=("m-strong", 3)), "unused(error:bad-response)"),
            (FakeJudge(answer="m-strong max"), "unused(error:bad-response)"),
            (FakeJudge(answer=("m-strong", "max", "extra")), "unused(error:bad-response)"),
            (object(), "unused(error:internal)"),
        ]
        for provider, status in rows:
            with self.subTest(status=status, provider=provider):
                if not hasattr(provider, "key_name"):
                    provider = type("NoJudge", (), {"name": "fake", "key_name": lambda self, config: KEY_NAME})()
                judged = self.judge(provider)
                self.assertEqual((judged.status, judged.proposal), (status, None))


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
