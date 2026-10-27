# 호출별 모델·effort 판정 검사 — 설정 키 검증, 후보·고정·규칙 검증, Jev 판정의 used·rejected·unused, 가짜 Codex 종단 실행과 헤더 모델 불일치 거부를 본다.
from __future__ import annotations

import contextlib
import hashlib
import json
import secrets as token_source
import unittest
from importlib import resources
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

    def test_template_documents_caps_for_every_launch(self):
        template = resources.files("oah").joinpath("templates/init/config.toml").read_text(encoding="utf-8")
        self.assertEqual(template.count("The highest effort any call may use"), 2)
        self.assertEqual(template.count("including for model pins and without Jev"), 2)
        self.assertEqual(template.count("floor effort refuses with OAH_CONFIG_INVALID"), 2)
        self.assertNotIn("never lowered by a cap", template)

    def test_configured_keys_load(self):
        # The fixture's config ends inside [engines.codex], so its keys follow without a header.
        project = calls.Project(self, 'allowed_models = ["m-a"]\n[engines.codex.effort_caps]\n"m-a" = "high"\n')
        settings = call_cli.load_settings(project.repo)
        self.assertEqual(settings.engines["codex"]["allowed_models"], ["m-a"])
        self.assertEqual(settings.engines["codex"]["effort_caps"], {"m-a": "high"})

    def test_tier_pools_load_and_default_empty(self):
        self.assertEqual(call.Settings.defaults().engines["codex"]["tier_pools"], {})
        self.assertEqual(call.Settings.defaults().engines["claude"]["tier_pools"], {})
        project = calls.Project(self, '[engines.codex.tier_pools]\nhigh = ["m-a", "m-b"]\n')
        settings = call_cli.load_settings(project.repo)
        self.assertEqual(settings.engines["codex"]["tier_pools"], {"high": ["m-a", "m-b"]})

    def test_invalid_caps_refuse(self):
        rows = [
            ('[engines.codex.effort_caps]\n"m-a" = "turbo"\n', "turbo"),
            ('[engines.codex.effort_caps]\n"m-a" = "highest"\n', "highest"),
            ('[engines.codex.effort_caps]\n"m-a" = "ultra"\n', "ultra"),
            ('[engines.codex.effort_caps]\n"m-a" = 3\n', "m-a"),
            ('allowed_models = ["m-a", 2]\n', "allowed_models"),
            ('[engines.claude.effort_caps]\n"m-a" = "max-ish"\n', "max-ish"),
            ('[engines.codex.tier_pools]\nhuge = ["m-a"]\n', "huge"),
            ('[engines.codex.tier_pools]\nhigh = "m-a"\n', "tier_pools.high"),
            ('[engines.codex.tier_pools]\nhigh = ["m-a", 3]\n', "tier_pools.high"),
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
        {"slug": "m-extra", "supported_reasoning_levels": [{"effort": e} for e in ("medium", "high")]},
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

    def test_tier_selectors_still_judge_pool_peers(self):
        pools = '[engines.codex.tier_pools]\nhigh = ["m-strong", "m-extra"]\n'
        for selector in ("flag", "packet"):
            with self.subTest(selector=selector):
                project = judged_project(self, ranked=False, extra=pools)
                fields = {"axes": {"kind": "security"}}
                if selector == "flag":
                    fields["tier"] = "high"
                else:
                    project.packet("## Scope\n- src/a.py\n## Plan\n- review → codex → high\n")
                provider = FakeJudge(("m-extra", "high"))
                with judge(project, provider):
                    data = project.run(stage="review", **fields)
                self.assertEqual((data["tier"], data["tier_source"]), ("high", selector))
                self.assertEqual(data["model_judge"], "used")
                self.assertEqual(launched(project), ("m-extra", "high"))
                self.assertEqual(len(provider.calls), 1)

    def test_caps_bind_deterministic_fallbacks_and_model_pins(self):
        for mode in ("disabled", "rejected", "pinned", "used"):
            with self.subTest(mode=mode):
                extra = '[engines.codex.effort_caps]\n"m-strong" = "medium"\n'
                if mode == "pinned":
                    extra += '[engines.codex.tiers.high]\nmodel = "m-strong"\n'
                project = judged_project(self, extra=extra, jev=mode != "disabled")
                provider = FakeJudge(("m-strong", "high") if mode == "rejected" else ("m-mid", "high"))
                with judge(project, provider):
                    data = project.run(stage="review", axes={"reasoning": "high"})
                expected = ("m-mid", "high") if mode == "used" else ("m-strong", "medium")
                self.assertEqual(launched(project), expected)
                self.assertEqual(data["requested_effort"], "high")
                self.assertEqual(data["model_choice"]["deterministic"]["effort"], "medium")
                self.assertTrue(any(w.startswith("OAH_EFFORT_CAPPED:") for w in data["warnings"]))
                self.assertEqual(data["model_judge"], {"disabled": "unused(not-enabled)",
                    "rejected": "rejected(above-cap)", "pinned": "unused(pinned)", "used": "used"}[mode])

    def test_cap_below_kind_floor_refuses_before_launch_in_every_mode(self):
        for mode in ("disabled", "rejected", "pinned", "used"):
            with self.subTest(mode=mode):
                extra = '[engines.codex.effort_caps]\n"m-strong" = "medium"\n'
                if mode == "pinned":
                    extra += '[engines.codex.tiers.high]\nmodel = "m-strong"\n'
                project = judged_project(self, extra=extra, jev=mode != "disabled")
                provider = FakeJudge(("m-extra", "high"))
                with judge(project, provider), self.assertRaises(EngineError) as caught:
                    project.run(stage="review", axes={"kind": "security"})
                self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
                self.assertIn("effort_caps", str(caught.exception))
                self.assertIn("floor", str(caught.exception))
                self.assertIn(".oah/config.toml", str(caught.exception))
                self.assertEqual(provider.calls, [])
                self.assertFalse(project.record.exists())
                self.assertEqual(project.runs(), [])

    def test_clamp_uses_a_supported_effort_below_the_cap(self):
        project = judged_project(self, extra='[engines.codex.effort_caps]\n"m-strong" = "xhigh"\n', jev=False)
        catalog = json.loads((project.home / "models_cache.json").read_bytes())
        catalog["models"][0]["supported_reasoning_levels"] = [{"effort": e} for e in ("low", "medium", "high", "max")]
        (project.home / "models_cache.json").write_bytes(json.dumps(catalog).encode("utf-8"))
        data = project.run(stage="review", axes={"reasoning": "max"})
        self.assertEqual((data["requested_effort"], data["launch_effort"]), ("highest", "high"))
        self.assertEqual(launched(project), ("m-strong", "high"))

    def test_claude_cap_binds_a_pinned_model(self):
        project = judged_project(self, extra='[engines.claude.effort_caps]\n"opus" = "medium"\n'
                                 '[engines.claude.tiers.high]\nmodel = "opus"\n', jev=False)
        data = project.run(engine="claude", stage="initial", axes={"reasoning": "high"})
        self.assertEqual((data["requested_effort"], data["launch_effort"]), ("high", "medium"))
        argv = project.recorded()["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")

    def test_cap_refuses_when_no_supported_effort_can_meet_the_floor(self):
        project = judged_project(self, extra='[engines.codex.effort_caps]\n"m-strong" = "medium"\n', jev=False)
        catalog = json.loads((project.home / "models_cache.json").read_bytes())
        catalog["models"][0]["supported_reasoning_levels"] = [{"effort": e} for e in ("low", "high", "max")]
        (project.home / "models_cache.json").write_bytes(json.dumps(catalog).encode("utf-8"))
        with self.assertRaises(EngineError) as caught:
            project.run(stage="review", axes={"reasoning": "high"})
        self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
        self.assertFalse(project.record.exists())

    def test_cap_does_not_guess_an_order_for_behaviour_changing_effort(self):
        project = judged_project(self, extra='[engines.codex.effort_caps]\n"m-strong" = "high"\n'
                                 '[engines.codex.tiers.high]\neffort = "ultra"\n', jev=False)
        with self.assertRaises(EngineError) as caught:
            project.run(stage="review", axes={"reasoning": "high"})
        self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
        self.assertFalse(project.record.exists())

    def test_missing_or_empty_applicable_pool_skips_judging(self):
        for ranked, cap in ((False, None), (False, "high"), (True, None)):
            for absent in ("high", "max"):
                for empty in (False, True):
                    with self.subTest(ranked=ranked, cap=cap, absent=absent, empty=empty):
                        pools = {"high": ["m-extra"], "max": ["m-extra"]}
                        pools.pop(absent)
                        if empty:
                            pools[absent] = []
                        extra = '[engines.codex.tier_pools]\n' + "".join(
                            f"{tier} = {json.dumps(names)}\n" for tier, names in pools.items())
                        if cap is not None:
                            extra += f'[engines.codex.effort_caps]\n"m-strong" = "{cap}"\n'
                        project = judged_project(self, ranked=ranked, extra=extra)
                        provider = FakeJudge(("m-strong", "high"))
                        with judge(project, provider):
                            data = project.run(stage="review", tier="max", axes={"kind": "security"})
                        self.assertEqual(data["model_judge"], "unused(no-pool-for-tier)")
                        self.assertEqual(data["model_choice"]["candidates"], [])
                        self.assertIsNone(data["model_choice"]["proposal"])
                        self.assertEqual(provider.calls, [])
                        self.assertEqual(launched(project), ("m-strong", cap or "max"))

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
                self.assertEqual((data["model_choice"]["mode"], data["model_choice"]["note"]),
                                 ("deterministic", "model choice off: no ranking and no tier pools"))
                self.assertEqual(launched(project), expected)

    def test_tier_pools_qualify_peers_for_a_high_floor_call(self):
        # AC8 (Revision 3): a security review has floor high; without a ranking the deterministic choice is the
        # CLI default at high. Only models the user put in the high pool are candidates, as unordered peers.
        pools = '[engines.codex.tier_pools]\nhigh = ["m-strong", "m-extra"]\nmax = ["m-strong", "m-extra"]\n'
        rows = [
            ("a pool peer other than the deterministic model", ("m-extra", "high"), "", "used", ("m-extra", "high")),
            ("the other peer at a higher effort", ("m-strong", "xhigh"), "", "used", ("m-strong", "xhigh")),
            ("allowed and supports high, but not in the high pool", ("m-mid", "high"), "",
             "rejected(not-in-tier-pool)", ("m-strong", "high")),
            ("allowed, in no pool at all", ("m-light", "medium"), 'allowed_models = ["m-strong", "m-extra", "m-light"]\n',
             "rejected(not-in-tier-pool)", ("m-strong", "high")),
            ("below the floor effort", ("m-strong", "medium"), "", "rejected(below-floor)", ("m-strong", "high")),
            ("hidden", ("m-hidden", "high"), "", "rejected(outside-candidates)", ("m-strong", "high")),
            ("capped at medium", ("m-extra", "high"), '[engines.codex.effort_caps]\n"m-extra" = "medium"\n',
             "rejected(above-cap)", ("m-strong", "high")),
        ]
        for name, answer, extra, status, expected in rows:
            with self.subTest(name):
                # allowed_models belongs to [engines.codex], so it precedes the tables.
                text = extra + pools if extra.startswith("allowed_models") else pools + extra
                project = judged_project(self, ranked=False, extra=text)
                provider = FakeJudge(answer)
                with judge(project, provider):
                    data = project.run(stage="review", axes={"kind": "security"})
                self.assertEqual(data["status"], "finished")
                self.assertEqual((data["tier"], data["model_judge"]), ("high", status))
                choice = data["model_choice"]
                self.assertEqual(choice["deterministic"], {"model": "m-strong", "effort": "high", "source": "cli_default"})
                self.assertEqual(choice["floor"], {"tier": "high", "model": "m-strong", "effort": "high"})
                self.assertEqual((choice["mode"], choice["note"]), ("pools", None))
                capped = "effort_caps" in extra
                self.assertEqual(choice["candidates"], [["m-strong", e] for e in ("high", "xhigh", "max")]
                                 + ([] if capped else [["m-extra", "high"]]))
                self.assertEqual(choice["excluded"], {"m-extra": "above-cap"} if capped
                                 else {"m-light": "not-in-tier-pool"} if "m-light" in extra else {})
                self.assertEqual(set(provider.calls[0][1]), {tuple(pair) for pair in choice["candidates"]})
                self.assertEqual((data["resolved_model"], data["launch_effort"]), expected)
                self.assertEqual(data["model_source"], "judge" if status == "used" else "cli_default")
                self.assertEqual(launched(project), expected)

    def test_a_pin_overrides_any_pool(self):
        project = judged_project(self, ranked=False, extra='[engines.codex.tier_pools]\nhigh = ["m-extra"]\n'
                                 '[engines.codex.tiers.high]\nmodel = "m-strong"\n')
        provider = FakeJudge(("m-extra", "high"))
        with judge(project, provider):
            data = project.run(stage="review", tier="high")
        self.assertEqual(data["model_judge"], "unused(pinned)")
        self.assertEqual(launched(project), ("m-strong", "high"))
        self.assertEqual(provider.calls, [])

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

    def corrected(self, provider: FakeJudge, *, step=None, **fields):
        project = fix_tests.FixProject(self, config=f"quality_order = {RANKING}\n" + JEV_ON)
        write_catalog(project.home)
        project.changes_requested()
        project.queue_fix({"write": fix_tests.FIXED} if step is None else step)
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

    def test_fix_persists_structured_choice_evidence(self):
        result, _, data = self.corrected(FakeJudge(("m-strong", "high")), tier=None)
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        hashed = lambda value: "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        choice = data["model_choice"]
        self.assertIsInstance(choice, dict)
        self.assertEqual(choice["deterministic"], {"model": hashed("m-mid"), "effort": "medium", "source": "quality_order"})
        self.assertEqual(choice["floor"], {"tier": "medium", "model": hashed("m-mid"), "effort": "medium"})
        self.assertEqual(choice["proposal"], {"model": hashed("m-strong"), "effort": "high"})
        self.assertEqual(choice["requested"], choice["proposal"])
        self.assertIn([hashed("m-strong"), "high"], choice["candidates"])
        self.assertEqual((choice["mode"], choice["provider"]), ("ranked", hashed("fake")))

    def test_fix_preserves_provider_timeout_status(self):
        result, _, data = self.corrected(FakeJudge(error=rerank.ProviderError("timeout")), tier=None)
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        self.assertEqual(data["model_judge"], "unused(error:timeout)")

    def test_fix_preserves_model_mismatch_code(self):
        # An earlier model line exercises the real header parser without changing the corrector fixture.
        result, _, data = self.corrected(FakeJudge(("m-strong", "high")), tier=None,
                                        step={"header_workdir": "unused\r\nmodel: m-other"})
        self.assertEqual(result.code, "OAH_FIX_FAILED", result.message)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["header_code"], "OAH_MODEL_MISMATCH")
        self.assertEqual(data["error"], "OAH_MODEL_MISMATCH")

    def test_explicit_correction_tier_still_uses_judged_pair(self):
        result, launched_pair, data = self.corrected(FakeJudge(("m-mid", "high")), tier="high")
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        self.assertEqual((data["tier_source"], data["model_judge"]), ("flag", "used"))
        self.assertEqual(launched_pair, ("m-mid", "high"))

    def test_fix_keeps_the_deterministic_pair_when_rejected(self):
        for fields, status, expected in (({"tier": None}, "rejected(below-floor)", ("m-mid", "medium")),
                                         ({"tier": "high"}, "rejected(below-floor)", ("m-strong", "high"))):
            with self.subTest(status=status):
                result, launched_pair, data = self.corrected(FakeJudge(("m-light", "low")), **fields)
                self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
                self.assertEqual(launched_pair, expected)
                self.assertEqual(data["model_judge"], status)


if __name__ == "__main__":
    unittest.main()
