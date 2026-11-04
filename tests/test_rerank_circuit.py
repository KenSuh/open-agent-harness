# Jev 차단기 검사 — 네 가지 장애형 실패만 세기, 열림과 창 뒤 한 번의 요청, UTC 하루 상한, 파일 모양·원자적 쓰기·손상 처리, 세 쓰임새의 상태 폴더 전달, doctor 표시, 설정 거절, 닫힌 낱말 목록을 본다.
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import secrets as token_source
import tomllib
import unittest
from importlib import resources
from unittest import mock

from oah import call, models_export, rerank, routing, runs, skillsel
from oah.config import ConfigError, load_config
from oah.engines.effort import EngineError
from oah.rerank import circuit
from oah.secrets import NoKeychain
from tests import test_model_judge as judge_tests
from tests.helpers import make_git_repo, make_tmp_dir
from tests.test_models_export import one
from tests.test_models_report import NEW

KEY_NAME = "FAKE_BREAKER_KEY"
NOW = dt.datetime(2026, 11, 4, 9, 30, 15, 250000, tzinfo=dt.timezone.utc)
AXES = {"kind": "implementation", "intensity": "medium", "reasoning": "high", "risk": "normal", "tier": "medium"}
PAIRS = (("m-strong", "high"), ("m-mid", "medium"))
JEV_ON = '[rerank]\nenabled = true\nprovider = "fake"\n'


def fake_key() -> str:
    """A key-shaped value generated at runtime; never a real credential."""
    return "fake-" + token_source.token_hex(16)


def jev(**overrides) -> dict:
    return dict(rerank.DEFAULTS, enabled=True, provider="fake", **overrides)


def ceil_second(moment: dt.datetime) -> dt.datetime:
    return moment.replace(microsecond=0) + dt.timedelta(seconds=1 if moment.microsecond else 0)


class Provider:
    """A fake Jev for all three uses. Each request takes the next queued error (None answers normally)."""

    name = "fake"

    def __init__(self, *errors: BaseException | None, advice=("high", 0.9)) -> None:
        self.errors, self.advice, self.calls = list(errors), advice, []

    def key_name(self, config) -> str:
        return KEY_NAME

    def _request(self, use: str) -> None:
        self.calls.append(use)
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error

    def score(self, kind, query, candidates, *, key, config):
        self._request("score")
        return [float(index) for index, _item in enumerate(candidates)]

    def advise(self, axes, work, *, key, config):
        self._request("advise")
        return self.advice

    def choose_model(self, request, candidates, *, key, config):
        self._request("choose_model")
        return candidates[0]


class BreakerCase(unittest.TestCase):
    """The three uses called directly, with a fixed clock and one state folder."""

    def setUp(self) -> None:
        self.state = make_tmp_dir(self) / "state"
        self.path = self.state / circuit.FILE_NAME
        self.now = NOW
        self.key = fake_key()
        self.env = {KEY_NAME: self.key}
        self.provider = Provider()
        self.config = jev()
        clock = mock.patch.object(circuit, "utc_now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def skills(self, state_dir: object = "state", **config) -> rerank.RerankResult:
        return rerank.rerank("skills", [rerank.Candidate("a", "first"), rerank.Candidate("b", "second")],
                             config=dict(self.config, **config), query="kind=docs stage=initial risk=normal",
                             env=self.env, keychain=NoKeychain(), providers={"fake": self.provider},
                             state_dir=self.state if state_dir == "state" else state_dir)

    def advice(self, state_dir: object = "state", **kwargs) -> rerank.Advice:
        return rerank.advise_difficulty(AXES, "Add a flag to the report.", config=self.config, env=self.env,
                                        keychain=NoKeychain(), providers={"fake": self.provider},
                                        state_dir=self.state if state_dir == "state" else state_dir,
                                        notify=lambda text: None, **kwargs)

    def judge(self, state_dir: object = "state") -> rerank.ModelJudgement:
        return rerank.judge_model({"stage": "review", "tier": "high", "axes": {}}, PAIRS, config=self.config,
                                  env=self.env, keychain=NoKeychain(), providers={"fake": self.provider},
                                  state_dir=self.state if state_dir == "state" else state_dir)

    def uses(self):
        return (("skills", lambda: self.skills().status), ("advice", lambda: self.advice().status),
                ("judge", lambda: self.judge().status))

    def saved(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, **fields) -> None:
        data = {"failures": 0, "last_failure": None, "open_until": None, "day": None, "requests": 0, **fields}
        self.state.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")


class CountTest(BreakerCase):
    def test_only_timeout_network_429_and_529_count(self):
        for kind in ("timeout", "network", "rate-limited", "overloaded"):
            with self.subTest(kind=kind):
                self.write(failures=2, last_failure="timeout")
                self.provider.errors = [rerank.ProviderError(kind)]
                self.assertEqual(self.skills().status, f"unused(error:{kind})")
                self.assertEqual((self.saved()["failures"], self.saved()["last_failure"]), (3, kind))
        for code in ("unauthorized", "bad-response", "http-500", "http-404", "invalid-header", "insecure-url",
                     "bad-request", "provider", "key-name"):
            with self.subTest(code=code):
                self.write(failures=2, last_failure="timeout")
                before = self.path.read_bytes()
                self.provider.errors = [rerank.ProviderError(code)]
                self.assertEqual(self.skills().status, f"unused(error:{code})")
                self.assertEqual(self.path.read_bytes(), before)
        self.write(failures=2, last_failure="timeout")
        self.provider.errors = [RuntimeError("an internal failure")]
        self.assertEqual(self.judge().status, "unused(error:internal)")
        self.assertEqual(self.saved()["failures"], 2)

    def test_a_success_resets_the_count(self):
        for name, use in self.uses():
            with self.subTest(use=name):
                self.write(failures=4, last_failure="network")
                self.assertEqual(use(), "used")
                self.assertEqual(self.saved(), {"failures": 0, "last_failure": None, "open_until": None, "day": None,
                                                "requests": 0})

    def test_an_answer_below_the_threshold_leaves_the_count(self):
        for confidence in (0.3, None):
            with self.subTest(confidence=confidence):
                self.write(failures=3, last_failure="timeout")
                before = self.path.read_bytes()
                self.provider.advice = ("high", confidence)
                self.assertEqual(self.advice().status, "used")
                self.assertEqual(self.path.read_bytes(), before)
        self.provider.advice = ("high", 0.6)  # at the threshold counts as used
        self.advice()
        self.assertEqual(self.saved()["failures"], 0)

    def test_no_key_or_off_sends_nothing_and_leaves_the_count(self):
        self.write(failures=3, last_failure="timeout")
        before = self.path.read_bytes()
        self.env = {}
        self.assertEqual(self.skills().status, "unused(no key)")
        self.env = {KEY_NAME: self.key}
        self.config = dict(rerank.DEFAULTS, provider="fake")
        self.assertEqual(self.judge().status, "unused(not-enabled)")
        self.assertEqual(self.advice(brief_disabled=True).status, "unused(disabled-by-brief)")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_failures_from_every_use_add_up_then_every_use_stops_without_a_request(self):
        timeout, network, limited = (rerank.ProviderError(code) for code in ("timeout", "network", "rate-limited"))
        self.provider.errors = [timeout, timeout, network, network, limited]
        statuses = [self.skills().status, self.skills().status, self.advice().status, self.advice().status]
        self.assertEqual(self.saved()["open_until"], None)
        statuses.append(self.judge().status)
        self.assertEqual(statuses, ["unused(error:timeout)"] * 2 + ["unused(error:network)"] * 2
                         + ["unused(error:rate-limited)"])
        self.assertEqual(self.saved()["open_until"], circuit.stamp(ceil_second(NOW) + dt.timedelta(seconds=600)))
        sent = len(self.provider.calls)
        for name, use in self.uses():
            with self.subTest(use=name):
                self.assertEqual(use(), "unused(circuit-open)")
        self.assertEqual(rerank.rerank("skills", [], config=self.config, env=self.env, keychain=NoKeychain(),
                                       providers={"fake": self.provider}, state_dir=self.state).status,
                         "unused(circuit-open)")
        self.assertEqual(len(self.provider.calls), sent, "an open breaker sends no request")

    def test_after_the_window_one_request_decides(self):
        until = ceil_second(NOW) + dt.timedelta(seconds=600)
        self.write(failures=5, last_failure="overloaded", open_until=circuit.stamp(until))
        self.now = until - dt.timedelta(seconds=1)
        self.assertEqual(self.judge().status, "unused(circuit-open)")
        self.now = until + dt.timedelta(seconds=3, microseconds=500)
        self.provider.errors = [rerank.ProviderError("timeout")]
        self.assertEqual(self.judge().status, "unused(error:timeout)")
        self.assertEqual(self.saved()["open_until"], circuit.stamp(ceil_second(self.now) + dt.timedelta(seconds=600)),
                         "a counted failure opens it again for a whole window")
        self.assertEqual(self.skills().status, "unused(circuit-open)")
        self.now += dt.timedelta(seconds=602)
        self.assertEqual(self.advice().status, "used")
        self.assertEqual(self.saved()["failures"], 0)
        self.assertEqual([self.skills().status, self.judge().status], ["used", "used"])
        self.assertEqual(self.provider.calls, ["choose_model", "advise", "score", "choose_model"])

    def test_the_threshold_and_the_window_come_from_the_config(self):
        self.config = jev(circuit_failures=2, circuit_open_seconds=30)
        self.provider.errors = [rerank.ProviderError("network")] * 2
        self.judge()
        self.assertIsNone(self.saved()["open_until"])
        self.judge()
        self.assertEqual(self.saved()["open_until"], circuit.stamp(ceil_second(NOW) + dt.timedelta(seconds=30)))

    def test_without_a_state_folder_nothing_is_read_or_written(self):
        self.write(failures=9, last_failure="timeout", open_until=circuit.stamp(NOW + dt.timedelta(seconds=60)))
        before = self.path.read_bytes()
        self.provider.errors = [rerank.ProviderError("timeout")] * 9
        with mock.patch.object(circuit, "read", side_effect=AssertionError("no breaker file is read")), \
                mock.patch.object(circuit, "write_text_atomic", side_effect=AssertionError("none is written")):
            for _ in range(3):
                statuses = [self.skills(None).status, self.advice(None).status, self.judge(None).status]
                self.assertEqual(statuses, ["unused(error:timeout)"] * 3)
        self.assertEqual(len(self.provider.calls), 9)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_correction_preparation_reads_but_never_writes(self):
        self.provider.errors = [rerank.ProviderError("timeout")] * 6
        for _ in range(6):
            self.assertEqual(self.advice(record_notice=False).status, "unused(error:timeout)")
        self.assertFalse(self.state.exists())
        self.write(failures=5, last_failure="timeout", open_until=circuit.stamp(NOW + dt.timedelta(seconds=60)))
        self.assertEqual(self.advice(record_notice=False).status, "unused(circuit-open)")


class DailyLimitTest(BreakerCase):
    def test_the_limit_stops_requests_until_the_next_utc_day(self):
        self.config = jev(max_requests_per_day=3)
        self.provider.errors = [None, rerank.ProviderError("unauthorized"), None]
        self.assertEqual([self.skills().status, self.judge().status, self.advice().status],
                         ["used", "unused(error:unauthorized)", "used"])
        self.assertEqual((self.saved()["day"], self.saved()["requests"]), ("2026-11-04", 3))
        for name, use in self.uses():
            with self.subTest(use=name):
                self.assertEqual(use(), "unused(daily-limit)")
        self.assertEqual(len(self.provider.calls), 3)
        self.now = dt.datetime(2026, 11, 4, 23, 59, 59, tzinfo=dt.timezone.utc)
        self.assertEqual(self.judge().status, "unused(daily-limit)")
        self.now = dt.datetime(2026, 11, 5, 0, 0, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(self.judge().status, "used")
        self.assertEqual((self.saved()["day"], self.saved()["requests"]), ("2026-11-05", 1))

    def test_failures_count_as_requests(self):
        self.config = jev(max_requests_per_day=2, circuit_failures=9)
        self.provider.errors = [rerank.ProviderError("timeout")] * 2
        self.skills()
        self.skills()
        self.assertEqual(self.skills().status, "unused(daily-limit)")
        self.assertEqual(len(self.provider.calls), 2)

    def test_off_by_default_and_a_healthy_jev_writes_nothing(self):
        for _ in range(4):
            for _name, use in self.uses():
                self.assertEqual(use(), "used")
        self.assertEqual(len(self.provider.calls), 12)
        self.assertFalse(self.path.exists(), "nothing changed, so no breaker file was written")


class FileTest(BreakerCase):
    def test_the_file_holds_only_counts_utc_times_and_kinds(self):
        self.config = jev(max_requests_per_day=50)
        self.provider.errors = [rerank.ProviderError("overloaded")] * 5
        for _ in range(5):
            self.judge()
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw), {"failures": 5, "last_failure": "overloaded",
                                           "open_until": circuit.stamp(ceil_second(NOW) + dt.timedelta(seconds=600)),
                                           "day": "2026-11-04", "requests": 5})
        for text in (self.key, KEY_NAME, "m-strong", "review", "fake", "http", rerank.DEFAULT_URL):
            self.assertNotIn(text, raw)

    def test_each_update_is_written_atomically(self):
        from oah import atomic

        with mock.patch.object(atomic.os, "replace", wraps=os.replace) as replace:
            self.provider.errors = [rerank.ProviderError("timeout")]
            self.judge()
        (source, target), _ = replace.call_args
        self.assertEqual(pathlib.Path(target), self.path)
        self.assertEqual(pathlib.Path(source).parent, self.state)
        self.assertNotEqual(pathlib.Path(source).name, circuit.FILE_NAME)
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), [circuit.FILE_NAME])

    def test_a_malformed_file_counts_as_closed_and_is_replaced_at_the_next_update(self):
        bad = ["{", "[]", "{}", json.dumps({"failures": -1, "last_failure": None, "open_until": None, "day": None,
                                            "requests": 0}),
               json.dumps({"failures": 9, "last_failure": "unauthorized", "open_until": None, "day": None,
                           "requests": 0}),
               json.dumps({"failures": 9, "last_failure": None, "open_until": "2026-13-40T99:00:00Z", "day": None,
                           "requests": 0}),
               json.dumps({"failures": True, "last_failure": None, "open_until": None, "day": None, "requests": 0}),
               json.dumps({"failures": 9, "last_failure": None, "open_until": None, "day": None, "requests": 0,
                           "note": "x"}),
               "[" * 3000, "\udcff"]
        for text in bad:
            with self.subTest(text=text[:40]):
                self.state.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(text.encode("utf-8", "surrogateescape"))
                calls = len(self.provider.calls)
                self.assertEqual(self.judge().status, "used")
                self.assertEqual(len(self.provider.calls), calls + 1)
                self.assertEqual(self.saved(), {"failures": 0, "last_failure": None, "open_until": None, "day": None,
                                                "requests": 0})

    def test_an_unreadable_file_counts_as_closed(self):
        self.path.mkdir(parents=True)  # a folder in its place cannot be read or replaced
        self.provider.errors = [rerank.ProviderError("timeout")] * 7
        for _ in range(7):
            self.assertEqual(self.skills().status, "unused(error:timeout)")
        self.assertTrue(self.path.is_dir())

    def test_a_window_longer_than_the_setting_counts_as_over(self):
        self.write(failures=5, last_failure="timeout", open_until=circuit.stamp(NOW + dt.timedelta(days=2)))
        self.assertEqual(self.judge().status, "used")
        self.config = jev(circuit_open_seconds=3 * 24 * 3600)
        self.write(failures=5, last_failure="timeout", open_until=circuit.stamp(NOW + dt.timedelta(days=2)))
        self.assertEqual(self.judge().status, "unused(circuit-open)")

    def test_each_update_reads_the_file_again(self):
        first = circuit.Breaker(self.state, circuit.Limits())
        second = circuit.Breaker(self.state, circuit.Limits())
        first.record("timeout")
        second.record("network")
        first.record(None)
        self.assertEqual((self.saved()["failures"], self.saved()["last_failure"]), (2, "network"))


class PassThroughTest(unittest.TestCase):
    """Each use gets the state folder from its caller: skill selection, difficulty advice and the model judge."""

    def setUp(self) -> None:
        self.root = make_git_repo(self)
        (self.root / ".oah").mkdir()
        (self.root / ".oah" / "config.toml").write_text(JEV_ON, encoding="utf-8")
        work = make_tmp_dir(self)
        self.state = work / "state"
        self.env = {KEY_NAME: fake_key(), "HOME": str(work / "home"), "USERPROFILE": str(work / "home")}
        self.provider = Provider()
        providers = mock.patch.dict(rerank.PROVIDERS, {"fake": self.provider})
        providers.start()
        self.addCleanup(providers.stop)

    def open_breaker(self, state: pathlib.Path) -> None:
        state.mkdir(parents=True, exist_ok=True)
        until = circuit.stamp(circuit.utc_now() + dt.timedelta(seconds=300))
        (state / circuit.FILE_NAME).write_text(json.dumps({"failures": 5, "last_failure": "timeout",
                                                           "open_until": until, "day": None, "requests": 0}),
                                               encoding="utf-8")

    def test_skill_selection_passes_its_state_folder(self):
        def chosen(state_dir):
            return skillsel.choose(self.root, kind="docs", stage="initial", risk="normal", scope=("README.md",),
                                   state_dir=state_dir, env=self.env, keychain=NoKeychain())[1]

        self.open_breaker(self.state)
        self.assertEqual(chosen(self.state), "unused(circuit-open)")
        self.assertNotEqual(chosen(None), "unused(circuit-open)")

    def test_difficulty_advice_passes_its_state_folder(self):
        self.open_breaker(self.state)
        axes = routing.Axes("implementation", "low", "medium", "normal")
        with contextlib.redirect_stderr(io.StringIO()):
            judged = call.judge_difficulty(axes, section=None, request_text="Add a flag to the report.",
                                           scope=("src/a.py",), rule_tier="medium", floor_tier="low", root=self.root,
                                           state_dir=self.state, env=self.env, keychain=NoKeychain())
            advised = call.advise(axes, ("src/a.py",), root=self.root, state_dir=self.state, env=self.env,
                                  keychain=NoKeychain())
        self.assertEqual((judged.status, judged.tier), ("unused(circuit-open)", "medium"))
        self.assertEqual(advised, "unused(circuit-open)")
        self.assertEqual(self.provider.calls, [])

    def test_the_model_judge_of_a_call_counts_and_stops(self):
        project = judge_tests.judged_project(self)
        project.state.mkdir(parents=True, exist_ok=True)
        (project.state / circuit.FILE_NAME).write_text(json.dumps(
            {"failures": 4, "last_failure": "timeout", "open_until": None, "day": None, "requests": 0}),
            encoding="utf-8")
        provider = judge_tests.FakeJudge(error=rerank.ProviderError("timeout"))
        with judge_tests.judge(project, provider), contextlib.redirect_stderr(io.StringIO()):
            data = project.run(stage="review")
        self.assertEqual(data["model_judge"], "unused(error:timeout)")
        saved = json.loads((project.state / circuit.FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual((saved["failures"], saved["last_failure"]), (5, "timeout"))
        self.assertIsNotNone(saved["open_until"])
        opened = judge_tests.judged_project(self)
        self.open_breaker(opened.state)
        provider = judge_tests.FakeJudge(("m-strong", "high"))
        with judge_tests.judge(opened, provider), contextlib.redirect_stderr(io.StringIO()):
            data = opened.run(stage="review")
        self.assertEqual((data["status"], data["model_judge"]), ("finished", "unused(circuit-open)"))
        self.assertEqual(provider.calls, [])
        self.assertEqual((data["resolved_model"], data["launch_effort"]), ("m-mid", "medium"))

    def test_a_correction_passes_no_state_folder_to_the_model_judge(self):
        fix_tests = judge_tests.fix_tests
        project = fix_tests.FixProject(self, config=f"quality_order = {judge_tests.RANKING}\n" + judge_tests.JEV_ON)
        judge_tests.write_catalog(project.home)
        project.changes_requested()
        project.queue_fix({"write": fix_tests.FIXED})
        project.state.mkdir(parents=True, exist_ok=True)
        path = project.state / circuit.FILE_NAME
        path.write_text(json.dumps({"failures": 1, "last_failure": "network", "open_until": None, "day": None,
                                    "requests": 0}), encoding="utf-8")
        before = path.read_bytes()
        key = fake_key()
        provider = judge_tests.FakeJudge(error=rerank.ProviderError("timeout"))
        with contextlib.chdir(project.repo), mock.patch.dict(rerank.PROVIDERS, {"fake": provider}), \
                mock.patch.object(NoKeychain, "lookup", lambda self, name: key if name == judge_tests.KEY_NAME else None), \
                contextlib.redirect_stderr(io.StringIO()):
            result = project.fix(tier=None)
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        data = json.loads((project.run_folder(result) / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(data["model_judge"], "unused(error:timeout)")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(path.read_bytes(), before, "a correction records nothing in the breaker")


class ConfigTest(unittest.TestCase):
    def section(self, text: str) -> dict:
        root = make_tmp_dir(self)
        (root / ".oah").mkdir()
        (root / ".oah" / "config.toml").write_text(f"[rerank]\n{text}\n", encoding="utf-8")
        return load_config(root).section("rerank", rerank.DEFAULTS)

    def test_defaults(self):
        self.assertEqual({key: rerank.DEFAULTS[key] for key in circuit.DEFAULTS},
                         {"circuit_failures": 5, "circuit_open_seconds": 600, "max_requests_per_day": 0})
        self.assertEqual(circuit.limits(rerank.DEFAULTS), circuit.Limits(5, 600, 0))
        template = resources.files("oah").joinpath("templates/skills/config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(template)["rerank"]
        self.assertEqual({key: parsed[key] for key in circuit.DEFAULTS}, circuit.DEFAULTS)

    def test_a_non_integer_or_too_small_value_is_refused_by_name(self):
        rows = [("circuit_failures = 0", "circuit_failures"), ("circuit_failures = -3", "circuit_failures"),
                ("circuit_open_seconds = 0", "circuit_open_seconds"),
                ("circuit_open_seconds = 40000000", "circuit_open_seconds"),
                ("max_requests_per_day = -1", "max_requests_per_day"),
                ("circuit_failures = 1.5", "circuit_failures"), ('circuit_failures = "5"', "circuit_failures"),
                ("circuit_open_seconds = true", "circuit_open_seconds"),
                ("max_requests_per_day = 2.0", "max_requests_per_day"),
                ("max_requests_per_day = [1]", "max_requests_per_day")]
        for text, key in rows:
            with self.subTest(text=text):
                with self.assertRaises(ConfigError) as caught:
                    rerank.min_confidence(self.section(text))
                self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
                self.assertIn(f"rerank.{key}", str(caught.exception))
        self.assertEqual(rerank.min_confidence(self.section("max_requests_per_day = 0\ncircuit_failures = 1")), 0.6)

    def test_every_use_refuses_before_its_gate_and_sends_nothing(self):
        provider = Provider()
        config = jev(circuit_failures=0)
        env, providers = {KEY_NAME: fake_key()}, {"fake": provider}
        for name, use in (
                ("skills", lambda: rerank.rerank("skills", [rerank.Candidate("a")], config=config, env=env,
                                                 keychain=NoKeychain(), providers=providers)),
                ("advice", lambda: rerank.advise_difficulty(AXES, "work", config=config, env=env,
                                                            keychain=NoKeychain(), providers=providers)),
                ("judge", lambda: rerank.judge_model({}, PAIRS, config=config, env=env, keychain=NoKeychain(),
                                                     providers=providers))):
            with self.subTest(use=name), self.assertRaises(ConfigError) as caught:
                use()
            self.assertIn("rerank.circuit_failures", str(caught.exception))
        self.assertEqual(provider.calls, [])

    def test_oah_call_refuses_before_anything_is_sent(self):
        root = make_git_repo(self)
        (root / ".oah").mkdir()
        (root / ".oah" / "config.toml").write_text(JEV_ON + "circuit_open_seconds = 0\n", encoding="utf-8")
        provider = Provider()
        with mock.patch.dict(rerank.PROVIDERS, {"fake": provider}), self.assertRaises(EngineError) as caught:
            call.judge_difficulty(routing.Axes("implementation", "low", "medium", "normal"), section=None,
                                  request_text="Add a flag.", scope=(), rule_tier="medium", floor_tier="low",
                                  root=root, state_dir=make_tmp_dir(self), env={KEY_NAME: fake_key()},
                                  keychain=NoKeychain())
        self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
        self.assertIn("rerank.circuit_open_seconds", str(caught.exception))
        self.assertIn("Nothing was sent or launched", str(caught.exception))
        self.assertEqual(provider.calls, [])


class DoctorBreakerTest(unittest.TestCase):
    """`oah doctor` reads the breaker from its file and `[rerank]` alone: no request, nothing written."""

    def setUp(self) -> None:
        self.repo = make_git_repo(self)
        (self.repo / ".oah").mkdir()
        self.config(JEV_ON)
        self.state = make_tmp_dir(self) / "state"
        self.env = {"OAH_STATE_DIR": str(self.state)}
        self.now = NOW
        clock = mock.patch.object(circuit, "utc_now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def config(self, text: str) -> None:
        (self.repo / ".oah" / "config.toml").write_text(text, encoding="utf-8")

    def write(self, **fields) -> None:
        data = {"failures": 0, "last_failure": None, "open_until": None, "day": None, "requests": 0, **fields}
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / circuit.FILE_NAME).write_text(json.dumps(data), encoding="utf-8")

    def line(self) -> str | None:
        import oah.doctor as doctor

        forbid = AssertionError("doctor sends no Jev request")
        with mock.patch.dict(rerank.PROVIDERS, {"fake": Provider(forbid, forbid)}), \
                mock.patch.object(rerank, "rerank", side_effect=forbid), \
                mock.patch.object(circuit, "write_text_atomic", side_effect=AssertionError("doctor writes nothing")):
            found = doctor.rerank_breaker(self.repo, self.env)
        lines = [text for text in doctor._breaker_lines(found) if text]
        self.assertLessEqual(len(lines), 1)
        return lines[0] if lines else None

    def test_closed_open_and_daily_limit(self):
        prefix = "Jev breaker (read from its file; no request): "
        self.assertEqual(self.line(), prefix + "closed")
        self.write(failures=2, last_failure="network")
        self.assertEqual(self.line(), prefix + "closed (2 of 5 failures in a row so far)")
        self.write(failures=5, last_failure="rate-limited", open_until="2026-11-04T09:40:16Z")
        self.assertEqual(self.line(), prefix + "open until 2026-11-04T09:40:16Z, after 5 timeout, network, 429 or 529 "
                                               "failures in a row (the last: rate-limited); until then Jev is not asked")
        self.now = dt.datetime(2026, 11, 4, 9, 41, tzinfo=dt.timezone.utc)
        self.assertEqual(self.line(), prefix + "closed (its open window is over; the next Jev request decides whether "
                                               "it opens again)")
        self.config(JEV_ON + "max_requests_per_day = 20\n")
        self.write(day="2026-11-04", requests=20)
        self.assertEqual(self.line(), prefix + "daily limit reached (20 of 20 requests this UTC day); Jev is asked "
                                               "again from the next UTC day")
        self.write(day="2026-11-03", requests=20)
        self.assertEqual(self.line(), prefix + "closed; 0 of 20 requests this UTC day")
        (self.state / circuit.FILE_NAME).write_text("{", encoding="utf-8")
        self.assertIn("its file could not be read, so it counts as closed", self.line())

    def test_nothing_is_shown_when_jev_is_off_and_there_is_no_file(self):
        self.config("")
        self.assertIsNone(self.line())
        self.write(failures=1, last_failure="timeout")
        self.assertEqual(self.line(), "Jev breaker (read from its file; no request): closed (1 of 5 failures in a "
                                      "row so far)")

    def test_a_bad_value_is_named_by_its_code(self):
        self.config(JEV_ON + "circuit_failures = 0\n")
        self.assertEqual(self.line(), "Jev breaker: not read (OAH_CONFIG_INVALID); fix [rerank] in .oah/config.toml")

    def test_doctor_prints_it_and_keeps_the_json_shape(self):
        import oah.doctor as doctor

        self.write(failures=5, last_failure="timeout", open_until="2026-11-04T09:40:16Z")

        def run(json_output: bool) -> str:
            out = io.StringIO()
            with mock.patch.object(doctor, "check_tools", return_value=[doctor.python_status((3, 12, 0))]), \
                    mock.patch.object(doctor, "project_root", return_value=self.repo), \
                    mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
                self.assertEqual(doctor.doctor_main(json_output=json_output), 0)
            return out.getvalue()

        self.assertIn("Jev breaker (read from its file; no request): open until 2026-11-04T09:40:16Z", run(False))
        self.assertNotIn("breaker", run(True))


class WordListTest(unittest.TestCase):
    def test_models_export_keeps_the_new_statuses(self):
        for field in ("model_judge", "jev_status"):
            for status in ("unused(circuit-open)", "unused(daily-limit)"):
                with self.subTest(field=field, status=status):
                    record = dict(NEW, **{field: status})
                    if field == "jev_status":
                        record.update(jev_tier=None, jev_confidence=None)
                    else:
                        record["model_judge_confidence"] = None
                    self.assertEqual(one(self, record)[field], status)

    def test_correction_records_keep_the_new_statuses(self):
        for status in ("unused(circuit-open)", "unused(daily-limit)"):
            with self.subTest(status=status):
                for field in ("jev_status", "model_judge"):
                    self.assertEqual(runs._correction_record({field: status}, runs.RESULT_FIELDS), {field: status})


if __name__ == "__main__":
    unittest.main()
