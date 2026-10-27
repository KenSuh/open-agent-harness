# 호출별 모델·effort 판정 검사 — 설정 키 검증, 후보·고정·규칙 검증, Jev 판정의 used·rejected·unused, 가짜 Codex 종단 실행과 헤더 모델 불일치 거부를 본다.
from __future__ import annotations

import contextlib
import json
import secrets as token_source
import unittest
from unittest import mock

from oah import call, call_cli, rerank
from oah.engines.effort import EngineError
from oah.secrets import NoKeychain
from tests import test_call as calls
from tests import test_fix as fix_tests
from tests import test_launcher_orca as orca_tests

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


ALL = ("low", "medium", "high", "xhigh", "max", "ultra")
RANKING = '["m-strong", "m-mid", "m-light"]'
JEV_ON = '[rerank]\nenabled = true\nprovider = "fake"\n'


def write_catalog(home) -> None:
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "m-strong", "is_default": True, "supported_reasoning_levels": [{"effort": e} for e in ALL]},
        {"slug": "m-mid", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")]},
        {"slug": "m-light", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium")]},
        {"slug": "m-hidden", "visibility": "hide", "supported_reasoning_levels": [{"effort": e} for e in ALL]},
    ]}), encoding="utf-8")


def judged_project(test: unittest.TestCase, *, ranked: bool = True, extra: str = "", jev: bool = True,
                   key: bool = True) -> calls.Project:
    # The fixture's config ends inside [engines.codex], so its keys follow without a header.
    text = (f"quality_order = {RANKING}\n" if ranked else "") + extra + (JEV_ON if jev else "")
    project = calls.Project(test, text)
    write_catalog(project.home)
    if key:
        project.env[KEY_NAME] = fake_key()
    return project


@contextlib.contextmanager
def judge(project: calls.Project, provider: FakeJudge):
    with contextlib.chdir(project.repo), mock.patch.dict(rerank.PROVIDERS, {"fake": provider}):
        yield


def launched(project: calls.Project) -> tuple[str, str]:
    argv = project.recorded()["argv"]
    effort = next(item for item in argv if item.startswith("model_reasoning_effort="))
    return argv[argv.index("-m") + 1], effort.split("=", 1)[1].strip('"')


class JudgeCallTest(unittest.TestCase):
    """A review call scores kind review, tier medium, floor medium: deterministic m-mid at medium."""

    def test_used_answer_is_recorded_beside_the_deterministic_choice(self):
        project = judged_project(self)
        provider = FakeJudge(("m-strong", "high"))
        with judge(project, provider):
            data = project.run(stage="review")
        self.assertEqual((data["status"], data["tier"], data["tier_source"]), ("finished", "medium", "rules"))
        self.assertEqual((data["resolved_model"], data["launch_effort"], data["model_source"]),
                         ("m-strong", "high", "judge"))
        self.assertEqual(data["model_judge"], "used")
        choice = data["model_choice"]
        self.assertEqual(choice["deterministic"], {"model": "m-mid", "effort": "medium", "source": "quality_order"})
        self.assertEqual(choice["floor"], {"tier": "medium", "model": "m-mid", "effort": "medium"})
        self.assertEqual(choice["proposal"], {"model": "m-strong", "effort": "high"})
        self.assertEqual(choice["requested"], {"model": "m-strong", "effort": "high"})
        self.assertEqual(choice["provider"], "fake")
        self.assertIn(["m-mid", "high"], choice["candidates"])
        self.assertNotIn(["m-light", "medium"], choice["candidates"])
        self.assertEqual(launched(project), ("m-strong", "high"))
        self.assertEqual((data["header_model"], data["header_effort"], data["header_check"]),
                         ("m-strong", "high", "match"))
        request, candidates = provider.calls[0]
        self.assertEqual(sorted(request), ["axes", "stage", "tier"])
        self.assertEqual(request["axes"]["kind"], "review")

    def test_rejected_answers_keep_the_deterministic_choice(self):
        rows = [
            (("m-light", "medium"), "below-floor", ""),
            (("m-strong", "low"), "below-floor", ""),
            (("m-strong", "max"), "above-cap", '[engines.codex.effort_caps]\n"m-strong" = "high"\n'),
            (("m-mid", "xhigh"), "unsupported-effort", ""),
            (("m-strong", "ultra"), "unsupported-effort", ""),
            (("m-hidden", "high"), "outside-candidates", ""),
            (("m-unknown", "high"), "outside-candidates", ""),
            (("m-strong", "high"), "not-allowed", 'allowed_models = ["m-mid"]\n'),
        ]
        for answer, rule, extra in rows:
            with self.subTest(answer=answer):
                project = judged_project(self, extra=extra)
                with judge(project, FakeJudge(answer)):
                    data = project.run(stage="review")
                self.assertEqual(data["status"], "finished")
                self.assertEqual(data["model_judge"], f"rejected({rule})")
                self.assertEqual((data["resolved_model"], data["launch_effort"], data["model_source"]),
                                 ("m-mid", "medium", "quality_order"))
                self.assertEqual(data["model_choice"]["proposal"], {"model": answer[0], "effort": answer[1]})
                self.assertEqual(data["model_choice"]["requested"], {"model": "m-mid", "effort": "medium"})
                self.assertEqual(launched(project), ("m-mid", "medium"))

    def test_unused_cases_keep_the_deterministic_choice(self):
        rows = [
            ("no key", {"key": False}, FakeJudge(), {}),
            ("not-enabled", {"jev": False}, FakeJudge(), {}),
            ("disabled-by-brief", {}, FakeJudge(), {"no_rerank": True}),
            ("error:timeout", {}, FakeJudge(error=rerank.ProviderError("timeout")), {}),
            ("error:bad-response", {}, FakeJudge(answer=("m-strong",)), {}),
            ("error:bad-response", {}, FakeJudge(answer=None), {}),
            ("one-candidate", {"extra": '[engines.codex.effort_caps]\n"m-mid" = "medium"\n'
                                        '"m-strong" = "low"\n'}, FakeJudge(), {}),
        ]
        for reason, options, provider, fields in rows:
            with self.subTest(reason=reason):
                project = judged_project(self, **options)
                with judge(project, provider):
                    data = project.run(stage="review", **fields)
                self.assertEqual(data["status"], "finished")
                self.assertEqual(data["model_judge"], f"unused({reason})")
                self.assertEqual((data["resolved_model"], data["launch_effort"]), ("m-mid", "medium"))
                self.assertIsNone(data["model_choice"]["proposal"])
                self.assertEqual(launched(project), ("m-mid", "medium"))

    def test_pins_are_never_judged(self):
        rows = [
            ("config tier model", '[engines.codex.tiers.medium]\nmodel = "m-light"\n', {}, ("m-light", "medium")),
            ("--tier", "", {"tier": "high"}, ("m-strong", "high")),
            ("packet plan tier", "", {"plan": "- review → codex → high\n"}, ("m-strong", "high")),
        ]
        for name, extra, fields, expected in rows:
            with self.subTest(name):
                project = judged_project(self, extra=extra)
                plan = fields.pop("plan", None)
                if plan is not None:
                    project.packet("## Scope\n- src/a.py\n## Plan\n" + plan)
                provider = FakeJudge(("m-strong", "max"))
                with judge(project, provider):
                    data = project.run(stage="review", **fields)
                self.assertEqual(data["model_judge"], "unused(pinned)")
                self.assertEqual((data["resolved_model"], data["launch_effort"]), expected)
                self.assertEqual(launched(project), expected)
                self.assertEqual(provider.calls, [])

    def test_without_a_ranking_only_the_effort_is_judged(self):
        for answer, status, expected in ((("m-strong", "xhigh"), "used", ("m-strong", "xhigh")),
                                         (("m-mid", "high"), "rejected(outside-candidates)", ("m-strong", "medium"))):
            with self.subTest(answer=answer):
                project = judged_project(self, ranked=False)
                with judge(project, FakeJudge(answer)):
                    data = project.run(stage="review")
                self.assertEqual(data["model_judge"], status)
                self.assertEqual(data["model_choice"]["deterministic"]["source"], "cli_default")
                self.assertEqual({pair[0] for pair in data["model_choice"]["candidates"]}, {"m-strong"})
                self.assertEqual(launched(project), expected)

    def test_unknown_cap_models_warn_or_refuse(self):
        project = judged_project(self, extra='[engines.codex.effort_caps]\n"m-typo" = "high"\n')
        with judge(project, FakeJudge()), self.assertRaises(EngineError) as ctx:
            project.prepare(stage="review")
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")
        self.assertIn("m-typo", str(ctx.exception))
        project = judged_project(self, extra='[engines.codex.effort_caps]\n"m-typo" = "high"\n"m-mid" = "high"\n')
        with judge(project, FakeJudge(("m-mid", "high"))):
            data = project.run(stage="review")
        self.assertTrue(any(w.startswith("OAH_MODEL_CAP_UNKNOWN") for w in data["warnings"]), data["warnings"])
        self.assertEqual(data["model_judge"], "used")

    def test_claude_initial_follows_the_judged_alias(self):
        project = judged_project(self)
        with judge(project, FakeJudge(("opus", "high"))):
            data = project.run(engine="claude", stage="initial")
        self.assertEqual(data["status"], "finished")
        self.assertEqual((data["model_judge"], data["resolved_model"], data["launch_effort"]),
                         ("used", "opus", "high"))
        argv = project.recorded()["argv"]
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]), ("opus", "high"))
        self.assertEqual(data["header_model"], "unverified")


class JudgeEndToEndTest(unittest.TestCase):
    def test_fake_codex_receives_the_judged_pair(self):
        project = judged_project(self)
        with judge(project, FakeJudge(("m-strong", "xhigh"))):
            code, out, err = calls.run_cli(project, "call", "codex", "--stage", "review", "--task", "t1")
        self.assertEqual(code, 0, err)
        self.assertEqual(launched(project), ("m-strong", "xhigh"))
        self.assertIn("resolved_model=m-strong launch_effort=xhigh", out)
        self.assertIn("model_judge=used", out)
        data = calls.find_run(project.state, out.split("run=", 1)[1].split()[0]).read()
        self.assertEqual((data["model_judge"], data["resolved_model"], data["launch_effort"], data["header_model"]),
                         ("used", "m-strong", "xhigh", "m-strong"))

    def test_header_model_mismatch_refuses(self):
        project = judged_project(self)
        with judge(project, FakeJudge(("m-strong", "high"))):
            data = project.run(stage="review", env={"FAKE_CODEX_HEADER_MODEL": "m-other"})
        self.assertEqual(data["status"], "failed")
        self.assertEqual((data["header_model"], data["header_code"]), ("m-other", "OAH_MODEL_MISMATCH"))
        self.assertTrue(data["error"].startswith("OAH_MODEL_MISMATCH: "), data["error"])
        self.assertIn("m-strong", data["error"])
        self.assertEqual(launched(project), ("m-strong", "high"))

    def test_missing_header_model_is_unverified_not_a_match(self):
        project = judged_project(self, jev=False)
        data = project.run(stage="review", env={"FAKE_CODEX_HEADER_MODEL": "-"})
        self.assertEqual((data["status"], data["header_model"], data["header_code"]), ("finished", "unverified", None))

    def test_orca_runner_path_refuses_model_mismatch(self):
        project = orca_tests.OrcaProject(self)
        prepared = project.queued()
        code, out, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage,
                                     env={"FAKE_CODEX_HEADER_MODEL": "m-other"})
        data = prepared.run.read()
        self.assertEqual(data["status"], "failed", out + err)
        self.assertEqual((data["header_model"], data["header_code"]), ("m-other", "OAH_MODEL_MISMATCH"))
        self.assertNotEqual(code, 0)



class FixPathTest(unittest.TestCase):
    """`oah fix` prepares its Codex correction through call.prepare, so it gets the same choice and record.

    Fix hands prepare the corrector's credential-free environment, so its re-ranker key comes from the
    keychain, as for the advisory tier and skill re-ranking; the fixture's keychain holds a fake key."""

    def corrected(self, provider: FakeJudge, **fields):
        project = fix_tests.FixProject(self, config=f"quality_order = {RANKING}\n" + JEV_ON)
        write_catalog(project.home)
        project.changes_requested()
        project.queue_fix({"write": fix_tests.FIXED})
        key = fake_key()
        with contextlib.chdir(project.repo), mock.patch.dict(rerank.PROVIDERS, {"fake": provider}), \
                mock.patch.object(NoKeychain, "lookup", lambda self, name: key if name == KEY_NAME else None):
            result = project.fix(**fields)
        argv = project.fix_calls()[0]["argv"]
        effort = next(item for item in argv if item.startswith("model_reasoning_effort="))
        data = json.loads((project.run_folder(result) / "result.json").read_text(encoding="utf-8"))
        return result, (argv[argv.index("-m") + 1], effort.split("=", 1)[1].strip('"')), data

    def test_fix_launches_the_judged_pair_and_records_it(self):
        result, launched_pair, data = self.corrected(FakeJudge(("m-strong", "high")), tier=None)
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        self.assertEqual(launched_pair, ("m-strong", "high"))
        self.assertEqual((data["model_judge"], data["model_source"], data["launch_effort"]), ("used", "judge", "high"))

    def test_fix_keeps_the_deterministic_pair_when_rejected_or_pinned(self):
        for fields, status, expected in (({"tier": None}, "rejected(below-floor)", ("m-mid", "medium")),
                                         ({"tier": "high"}, "unused(pinned)", ("m-strong", "high"))):
            with self.subTest(status=status):
                result, launched_pair, data = self.corrected(FakeJudge(("m-light", "low")), **fields)
                self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
                self.assertEqual(launched_pair, expected)
                self.assertEqual(data["model_judge"], status)


if __name__ == "__main__":
    unittest.main()
