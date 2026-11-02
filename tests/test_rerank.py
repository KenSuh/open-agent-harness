# oah.rerank 테스트 — 동의·키 관문 순서, 순서만 바꾸는 계약, 가짜 HTTP 서버로 TypeSafe 요청 형식과 키 비노출을 확인한다.
from __future__ import annotations

import contextlib
import copy
import email.message
import http.client
import http.server
import io
import json
import os
import pathlib
import secrets as token_source
import socket
import sys
import threading
import time
import traceback
import types
import unittest
import urllib.request
import urllib.response
from unittest import mock

from oah import rerank, routing
from oah.config import ConfigError, load_config
from oah.rerank import base, typesafe
from oah.secrets import Secret
from tests.helpers import make_git_repo, make_tmp_dir


def fake_key() -> str:
    """A key-shaped value generated at runtime; never a real credential."""
    return "fake-" + token_source.token_hex(16)


class NoKeychain:
    def lookup(self, name: str) -> str | None:
        return None


def config(**overrides) -> dict:
    merged = copy.deepcopy(rerank.DEFAULTS)
    for key, value in overrides.items():
        if isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def enabled(provider: str = "fake", **overrides) -> dict:
    return config(enabled=True, provider=provider, **overrides)


def candidates(*names: str) -> list[rerank.Candidate]:
    return [rerank.Candidate(name, f"about {name}") for name in names]


def names(result: rerank.RerankResult) -> list[str]:
    return [item.name for item in result.order]


class FakeProvider:
    name = "fake"

    def __init__(self, scores=None, advice=("high", 0.8), error: Exception | None = None) -> None:
        self.scores = scores or {}
        self.advice_answer = advice
        self.error = error
        self.calls: list[tuple] = []

    def key_name(self, config: dict) -> str:
        return "FAKE_RERANK_KEY"

    def score(self, kind, query, items, *, key, config):
        self.calls.append(("score", kind, query, tuple(items), key))
        if self.error is not None:
            raise self.error
        if callable(self.scores):
            return self.scores(items)
        return [self.scores[item.name] for item in items]

    def advise(self, axes, task, *, key, config):
        self.calls.append(("advise", dict(axes), task, key))
        if self.error is not None:
            raise self.error
        return self.advice_answer


class GatingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = fake_key()
        self.provider = FakeProvider({"a": 0.1, "b": 0.9, "c": 0.5})
        self.providers = {"fake": self.provider}
        self.items = candidates("a", "b", "c")

    def run_rerank(self, cfg: dict, *, env=None, brief_disabled=False) -> rerank.RerankResult:
        return rerank.rerank("impact", self.items, config=cfg, query="q", brief_disabled=brief_disabled,
                             env={} if env is None else env, keychain=NoKeychain(), providers=self.providers)

    def test_disabled_by_brief_wins(self) -> None:
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key}, brief_disabled=True)
        self.assertEqual(result.status, "unused(disabled-by-brief)")
        self.assertEqual(names(result), ["a", "b", "c"])
        self.assertEqual(self.provider.calls, [])

    def test_key_alone_never_enables(self) -> None:
        for cfg in (config(), config(provider="fake"), config(enabled=False, provider="fake")):
            with self.subTest(cfg=cfg):
                result = self.run_rerank(cfg, env={"FAKE_RERANK_KEY": self.key})
                self.assertEqual(result.status, "unused(not-enabled)")
                self.assertFalse(result.used)
        self.assertEqual(self.provider.calls, [])

    def test_enabled_must_be_true_not_truthy(self) -> None:
        result = self.run_rerank(config(enabled="yes", provider="fake"), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "unused(not-enabled)")

    def test_enabled_without_provider_is_not_configured(self) -> None:
        result = self.run_rerank(config(enabled=True), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "unused(not-configured)")
        self.assertEqual(self.provider.calls, [])

    def test_unknown_provider(self) -> None:
        result = self.run_rerank(enabled("nope"), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "unused(unknown-provider)")

    def test_enabled_without_key_is_no_key(self) -> None:
        result = self.run_rerank(enabled(), env={})
        self.assertEqual(result.status, "unused(no key)")
        self.assertEqual(self.provider.calls, [])

    def test_used_reorders_by_score(self) -> None:
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "used")
        self.assertTrue(result.used)
        self.assertEqual(names(result), ["b", "c", "a"])
        self.assertEqual(dict(result.scores), {"a": 0.1, "b": 0.9, "c": 0.5})
        self.assertEqual(self.provider.calls[0][4].reveal(), self.key)

    def test_public_apis_pass_only_opaque_keys_to_providers(self) -> None:
        with mock.patch.object(Secret, "reveal", side_effect=AssertionError("transport only")):
            result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
            advice = rerank.advise_difficulty(AdviceTest.AXES, "task", config=enabled(),
                                            env={"FAKE_RERANK_KEY": self.key},
                                            keychain=NoKeychain(), providers=self.providers)
        self.assertTrue(result.used)
        self.assertEqual(advice.status, "used")
        for call in self.provider.calls:
            self.assertIsInstance(call[-1], Secret)
            self.assertNotIn(self.key, repr(call))

    def test_ties_keep_input_order(self) -> None:
        self.provider.scores = {"a": 0.5, "b": 0.5, "c": 0.9}
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(names(result), ["c", "a", "b"])

    def test_exact_builtin_result_types_are_accepted(self) -> None:
        self.provider.scores = lambda items: (1, 3.0, 2)
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual((result.status, names(result)), ("used", ["b", "c", "a"]))
        for confidence in (1, 0.5, None):
            with self.subTest(confidence=confidence):
                self.provider.advice_answer = ("high", confidence)
                advice = rerank.advise_difficulty(AdviceTest.AXES, "task", config=enabled(),
                    env={"FAKE_RERANK_KEY": self.key}, keychain=NoKeychain(), providers=self.providers)
                self.assertEqual((advice.status, advice.confidence), ("used", confidence))

    def test_bad_scores_keep_input_order(self) -> None:
        bad = [
            lambda items: [0.5, 0.5],                      # too few: would drop a row
            lambda items: [0.5, 0.5, 0.5, 0.5],            # too many
            lambda items: [0.5, float("nan"), 0.1],        # not finite
            lambda items: [0.5, True, 0.1],                # not a number
            lambda items: {"a": 1.0},                      # not a sequence
            lambda items: None,
        ]
        for scores in bad:
            with self.subTest():
                self.provider.scores = scores
                result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
                self.assertEqual(result.status, "unused(error:bad-response)")
                self.assertEqual(names(result), ["a", "b", "c"])

    def test_provider_error_records_code(self) -> None:
        self.provider.error = rerank.ProviderError("timeout")
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "unused(error:timeout)")
        self.assertEqual(names(result), ["a", "b", "c"])

    def test_oversized_integer_score_keeps_input_order(self) -> None:
        self.provider.scores = {"a": 0.1, "b": 10**400, "c": 0.5}
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "unused(error:bad-response)")
        self.assertEqual(names(result), ["a", "b", "c"])
        self.assertEqual(result.scores, ())

    def test_unexpected_exception_is_internal_error(self) -> None:
        self.provider.error = RuntimeError(f"boom {self.key}")
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "unused(error:internal)")
        self.assertNotIn(self.key, json.dumps(result.to_record()))

    def test_overflow_candidates_keep_their_place(self) -> None:
        self.items = candidates("a", "b", "c", "d")
        self.provider.scores = {"a": 0.1, "b": 0.9}
        result = self.run_rerank(enabled(max_candidates=2), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "used")
        self.assertEqual(names(result), ["b", "a", "c", "d"])
        self.assertEqual(len(self.provider.calls[0][3]), 2)

    def test_empty_candidates_make_no_call(self) -> None:
        self.items = []
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.status, "unused(no-candidates)")
        self.assertEqual(self.provider.calls, [])

    def test_duplicate_names_raise(self) -> None:
        self.items = candidates("a", "a")
        with self.assertRaises(ValueError):
            self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})

    def test_keychain_is_consulted(self) -> None:
        class Chain:
            def lookup(inner, name: str) -> str | None:
                return self.key if name == "FAKE_RERANK_KEY" else None

        result = rerank.rerank("impact", self.items, config=enabled(), env={}, keychain=Chain(),
                               providers=self.providers)
        self.assertEqual(result.status, "used")

    def test_record_shape(self) -> None:
        result = self.run_rerank(enabled(), env={"FAKE_RERANK_KEY": self.key})
        self.assertEqual(result.to_record(), {
            "rerank": "used", "provider": "fake", "order": ["b", "c", "a"],
            "scores": {"a": 0.1, "b": 0.9, "c": 0.5},
        })
        unused = self.run_rerank(config(), env={})
        self.assertEqual(unused.to_record(), {"rerank": "unused(not-enabled)", "provider": None,
                                              "order": ["a", "b", "c"], "scores": {}})


class AdviceTest(unittest.TestCase):
    AXES = {"kind": "implementation", "intensity": "medium", "reasoning": "high", "risk": "normal",
            "tier": "high"}

    def setUp(self) -> None:
        self.key = fake_key()
        self.provider = FakeProvider()
        self.env = {"FAKE_RERANK_KEY": self.key}

    def advise(self, cfg: dict, **kwargs) -> rerank.Advice:
        kwargs.setdefault("env", self.env)
        return rerank.advise_difficulty(self.AXES, "add a flag", config=cfg, keychain=NoKeychain(),
                                        providers={"fake": self.provider}, **kwargs)

    def test_advice_never_lowers_tier(self) -> None:
        for answer in ("low", "medium", "high", "max"):
            with self.subTest(answer=answer):
                self.provider.advice_answer = (answer, 0.7)
                advice = self.advise(enabled())
                self.assertEqual(advice.status, "used")
                self.assertEqual(advice.advisory_tier, answer)
                self.assertEqual(advice.tier, "high")
                self.assertEqual(advice.rule_tier, "high")

    def test_advice_record_sits_next_to_rule(self) -> None:
        self.provider.advice_answer = ("max", 0.6)
        self.assertEqual(self.advise(enabled()).to_record(), {
            "tier": "high", "tier_source": "rules", "advice": "used", "provider": "fake",
            "advisory_tier": "max", "advisory_confidence": 0.6,
        })

    def test_advice_gating(self) -> None:
        self.assertEqual(self.advise(enabled(), brief_disabled=True).status, "unused(disabled-by-brief)")
        self.assertEqual(self.advise(config()).status, "unused(not-enabled)")
        self.assertEqual(self.advise(config(enabled=True)).status, "unused(not-configured)")
        self.assertEqual(self.advise(enabled(), env={}).status, "unused(no key)")
        self.assertEqual(self.provider.calls, [])
        unused = self.advise(config())
        self.assertIsNone(unused.advisory_tier)
        self.assertEqual(unused.tier, "high")

    def test_advice_bad_answers(self) -> None:
        for answer in (("huge", 0.5), ("high", "x"), ("high", 10**400), None, ("high",)):
            with self.subTest(answer=answer):
                self.provider.advice_answer = answer
                advice = self.advise(enabled())
                self.assertEqual(advice.status, "unused(error:bad-response)")
                self.assertIsNone(advice.advisory_tier)
                self.assertEqual(advice.tier, "high")

    def test_advice_provider_error(self) -> None:
        self.provider.error = rerank.ProviderError("unauthorized")
        self.assertEqual(self.advise(enabled()).status, "unused(error:unauthorized)")

    def test_advice_needs_a_rule_tier(self) -> None:
        with self.assertRaises(ValueError):
            rerank.advise_difficulty({"kind": "docs"}, "x", config=config(), env={})

    def test_call_signature_uses_repository_defaults(self) -> None:
        root = make_git_repo(self)
        (root / ".oah").mkdir()
        nested = root / "src"
        nested.mkdir()
        axes = routing.Axes("review", "low", "medium", "normal")
        for text in (None, '[rerank]\nenabled = false\nprovider = "fake"\n'):
            if text is not None:
                (root / ".oah" / "config.toml").write_text(text, encoding="utf-8")
            with self.subTest(config=text), contextlib.chdir(nested), \
                    mock.patch.dict(os.environ, self.env), \
                    mock.patch("oah.secrets.get", side_effect=AssertionError("disabled advice read a key")):
                advice = rerank.advise_difficulty(axes, "add a flag", root=root)
                self.assertEqual(advice.status, "unused(not-configured)")
                self.assertEqual(advice.tier, routing.recommend(axes).tier)
                self.assertIsNone(advice.advisory_tier)

    def test_call_signature_loads_repository_rerank_settings(self) -> None:
        root = make_git_repo(self)
        (root / ".oah").mkdir()
        (root / ".oah" / "config.toml").write_text(
            '[rerank]\nenabled = true\nprovider = "fake"\n', encoding="utf-8")
        nested = root / "src"
        nested.mkdir()
        axes = routing.Axes("implementation", "medium", "high", "normal")
        self.provider.advice_answer = ("low", 0.7)
        with contextlib.chdir(nested), mock.patch.dict(os.environ, self.env), \
                mock.patch.dict(rerank.PROVIDERS, {"fake": self.provider}):
            advice = rerank.advise_difficulty(axes, "add a flag", root=root)
        self.assertEqual(advice.status, "used")
        self.assertEqual(advice.tier, routing.recommend(axes).tier)
        self.assertEqual((advice.advisory_tier, advice.confidence), ("low", 0.7))
        self.assertEqual(self.provider.calls[0][:3], (
            "advise", dict(axes.to_json(), tier=advice.rule_tier), "add a flag"))
        self.assertNotIn(self.key, json.dumps(advice.to_record()))

    def test_routing_axes_with_explicit_config_need_no_repository(self) -> None:
        axes = routing.Axes("security", "low", "low", "normal")
        with contextlib.chdir(make_tmp_dir(self)):
            advice = rerank.advise_difficulty(axes, "add a flag", config={}, env={}, keychain=NoKeychain())
        self.assertEqual(advice.status, "unused(not-enabled)")
        self.assertEqual(advice.tier, routing.recommend(axes).tier)

    def test_explicit_root_loads_rerank_settings_outside_repository(self) -> None:
        root = make_git_repo(self)
        (root / ".oah").mkdir()
        (root / ".oah" / "config.toml").write_text(
            '[rerank]\nenabled = true\nprovider = "fake"\n', encoding="utf-8")
        axes = routing.Axes("review", "low", "medium", "normal")
        with contextlib.chdir(make_tmp_dir(self)):
            advice = rerank.advise_difficulty(axes, "add a flag", root=root, env=self.env,
                                            keychain=NoKeychain(), providers={"fake": self.provider})
        self.assertEqual(advice.status, "used")
        self.assertEqual(advice.tier, routing.recommend(axes).tier)
        self.assertEqual(advice.advisory_tier, "high")
        self.assertEqual(len(self.provider.calls), 1)

    def test_advice_without_config_or_root_ignores_current_repository(self) -> None:
        root = make_git_repo(self)
        (root / ".oah").mkdir()
        (root / ".oah" / "config.toml").write_text(
            '[rerank]\nenabled = true\nprovider = "fake"\n', encoding="utf-8")
        axes = routing.Axes("review", "low", "medium", "normal")
        with contextlib.chdir(root), \
                mock.patch("oah.secrets.get", side_effect=AssertionError("unconfigured advice read a key")):
            advice = rerank.advise_difficulty(axes, "add a flag", providers={"fake": self.provider})
        self.assertEqual(advice.status, "unused(not-configured)")
        self.assertEqual(self.provider.calls, [])


class FakeServer:
    """A loopback HTTP server that records requests and answers with `respond(path, body)`."""

    def __init__(self, test: unittest.TestCase, respond) -> None:
        self.requests: list[dict] = []
        self.respond = respond
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                server.requests.append({"path": self.path, "headers": dict(self.headers.items()),
                                        "body": json.loads(raw.decode("utf-8"))})
                status, payload, delay, extra = server.respond(self.path, server.requests[-1]["body"])
                if delay:
                    time.sleep(delay)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    for name, value in extra.items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str = "/v1/systemone") -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"


def noul_answers(scores: dict[str, float]):
    """A responder giving each candidate question the noul named in `scores`."""
    def respond(path, body):
        answers = {}
        for qid in body["questions"]:
            if qid == "tier":
                answers[qid] = {"type": "choice", "choice": "low", "confidence": 0.9,
                                "probabilities": {"low": 0.9, "medium": 0.1}}
                continue
            index = int(qid[1:])
            name = body["state"]["candidates"][index]["name"]
            answers[qid] = {"type": "noul", "noul": scores[name]}
        return 200, {"model": "jev-test", "answers": answers,
                     "usage": {"input_tokens": 10, "output_tokens": 1}}, 0, {}
    return respond


def typesafe_config(url: str, **overrides) -> dict:
    cfg = enabled("typesafe", **overrides)
    cfg["typesafe"] = dict(cfg["typesafe"], url=url)
    return cfg


class TypeSafeHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = fake_key()
        self.env = {"TYPESAFE_API_KEY": self.key}

    def run_typesafe(self, server: FakeServer, items, **overrides) -> rerank.RerankResult:
        return rerank.rerank("impact", items, config=typesafe_config(server.url(), **overrides),
                             query="which files does this change touch?", env=self.env, keychain=NoKeychain())

    def test_enabled_with_key_against_fake_server(self) -> None:
        server = FakeServer(self, noul_answers({"a": 0.2, "b": 0.7, "c": 0.4}))
        result = self.run_typesafe(server, candidates("a", "b", "c"))
        self.assertEqual(result.status, "used")
        self.assertEqual(result.provider, "typesafe")
        self.assertEqual(names(result), ["b", "c", "a"])
        self.assertEqual(len(server.requests), 1)

    def test_typesafe_request_shape_and_auth(self) -> None:
        server = FakeServer(self, noul_answers({"a": 0.2, "b": 0.7}))
        self.run_typesafe(server, candidates("a", "b"))
        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/systemone")
        headers = {name.lower(): value for name, value in request["headers"].items()}
        self.assertEqual(headers["authorization"], f"Bearer {self.key}")
        self.assertEqual(headers["content-type"], "application/json")
        body = request["body"]
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(sorted(body["questions"]), ["c0", "c1"])
        for qid, question in body["questions"].items():
            self.assertEqual(question["type"], "noul")
            self.assertIn(f"candidates[{qid[1:]}]", question["instructions"])
            self.assertEqual(sorted(question["criteria"]), ["false", "true"])

    def test_typesafe_sends_only_disclosed_fields(self) -> None:
        server = FakeServer(self, noul_answers({"a": 0.2, "b": 0.7}))
        items = [rerank.Candidate("a", "x" * 500), rerank.Candidate("b", "")]
        self.run_typesafe(server, items)
        body = server.requests[0]["body"]
        self.assertEqual(sorted(body), ["model", "questions", "state"])
        self.assertEqual(sorted(body["state"]), ["candidates", "query", "task_kind"])
        self.assertEqual(body["state"]["task_kind"], "impact")
        self.assertEqual(body["state"]["candidates"],
                         [{"name": "a", "description": "x" * typesafe.DESCRIPTION_LIMIT},
                          {"name": "b", "description": ""}])
        self.assertNotIn(self.key, json.dumps(body))

    def test_typesafe_http_errors_map_to_codes(self) -> None:
        cases = {401: "unauthorized", 422: "http-422", 429: "rate-limited", 500: "http-500",
                 529: "overloaded"}
        for status, code in cases.items():
            with self.subTest(status=status):
                server = FakeServer(self, lambda path, body, s=status: (s, {"detail": "no"}, 0, {}))
                result = self.run_typesafe(server, candidates("a", "b"))
                self.assertEqual(result.status, f"unused(error:{code})")
                self.assertEqual(names(result), ["a", "b"])

    def test_typesafe_never_follows_redirects(self) -> None:
        def respond(path, body):
            if path == "/elsewhere":
                return 200, {}, 0, {}
            return 307, {}, 0, {"Location": "/elsewhere"}
        server = FakeServer(self, respond)
        result = self.run_typesafe(server, candidates("a", "b"))
        self.assertEqual(result.status, "unused(error:http-307)")
        self.assertEqual([request["path"] for request in server.requests], ["/v1/systemone"])

    def test_proxy_environment_cannot_redirect_credentials(self) -> None:
        endpoint = FakeServer(self, lambda path, body: (200, {}, 0, {}))
        proxy = FakeServer(self, lambda path, body: (200, {}, 0, {}))
        environment = {name: proxy.url() for name in
                       ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
        environment.update(no_proxy="", NO_PROXY="")
        with mock.patch.dict(os.environ, environment), \
                mock.patch("urllib.request.proxy_bypass", return_value=False):
            typesafe.post_json(endpoint.url(), {}, key=Secret(self.key), timeout=5)
        self.assertEqual(proxy.requests, [])
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(endpoint.requests[0]["headers"]["Authorization"], f"Bearer {self.key}")

    def test_proxy_environment_does_not_change_transport_destination(self) -> None:
        destinations = []

        def http_open(handler, request):
            destinations.append(request.host)
            response = urllib.response.addinfourl(io.BytesIO(b"{}"), email.message.Message(),
                                                 request.full_url, 200)
            response.msg = "OK"
            return response

        with mock.patch.dict(os.environ, {"http_proxy": "http://proxy.invalid:8080", "no_proxy": ""}), \
                mock.patch("urllib.request.proxy_bypass", return_value=False), \
                mock.patch.object(urllib.request.HTTPHandler, "http_open", http_open):
            typesafe.post_json("http://127.0.0.1:9876/v1/systemone", {}, key=Secret(self.key), timeout=5)
        self.assertEqual(destinations, ["127.0.0.1:9876"])

    def test_typesafe_success_preserves_public_return_values(self) -> None:
        body = {"answers": {"c0": {"type": "noul", "noul": 0.7},
                            "tier": {"type": "choice", "choice": "max", "confidence": 0.8}}}
        url = "http://127.0.0.1:9876/v1/systemone"
        cfg = typesafe_config(url)
        provider = typesafe.TypeSafeProvider()

        def http_open(handler, request):
            response = urllib.response.addinfourl(io.BytesIO(json.dumps(body).encode()),
                                                 email.message.Message(), request.full_url, 200)
            response.msg = "OK"
            return response

        with mock.patch.object(urllib.request.HTTPHandler, "http_open", http_open):
            self.assertEqual(typesafe.post_json(url, {}, key=Secret(self.key), timeout=5), body)
            self.assertEqual(provider._post({}, Secret(self.key), cfg), body)
            self.assertEqual(provider.score("impact", "q", candidates("a"), key=Secret(self.key), config=cfg), [0.7])
            self.assertEqual(provider.advise(AdviceTest.AXES, "task", key=Secret(self.key), config=cfg), ("max", 0.8))
        self.assertEqual(typesafe.parse_rerank_response(body, 1), [0.7])
        self.assertEqual(typesafe.parse_advice_response(body), ("max", 0.8))

    def test_typesafe_bad_bodies(self) -> None:
        bodies = [b"not json", {"answers": {}}, {"answers": {"c0": {"type": "noul", "noul": 2.0},
                                                           "c1": {"type": "noul", "noul": 0.1}}},
                  {"answers": {"c0": {"type": "choice", "choice": "x"}, "c1": {"type": "noul", "noul": 0.1}}}]
        for payload in bodies:
            with self.subTest(payload=payload):
                server = FakeServer(self, lambda path, body, p=payload: (200, p, 0, {}))
                result = self.run_typesafe(server, candidates("a", "b"))
                self.assertEqual(result.status, "unused(error:bad-response)")

    def test_oversized_integer_response_is_bad_response(self) -> None:
        body = {"answers": {"c0": {"type": "noul", "noul": 10**400}}}
        with self.assertRaises(rerank.ProviderError) as caught:
            typesafe.parse_rerank_response(body, 1)
        self.assertEqual(caught.exception.code, "bad-response")

    def test_typesafe_timeout(self) -> None:
        server = FakeServer(self, lambda path, body: (200, {}, 2.0, {}))
        started = time.monotonic()
        result = self.run_typesafe(server, candidates("a", "b"), timeout_seconds=0.3)
        self.assertEqual(result.status, "unused(error:timeout)")
        self.assertLess(time.monotonic() - started, 1.8)

    def test_typesafe_network_error(self) -> None:
        server = FakeServer(self, noul_answers({}))
        url = server.url()
        server.close()
        result = rerank.rerank("impact", candidates("a"), config=typesafe_config(url), env=self.env,
                               keychain=NoKeychain())
        self.assertEqual(result.status, "unused(error:network)")

    def test_typesafe_refuses_plain_http_remote(self) -> None:
        for url in ("http://example.invalid/v1/systemone", "ftp://example.invalid/x", "file:///etc/hosts"):
            with self.subTest(url=url):
                result = rerank.rerank("impact", candidates("a"), config=typesafe_config(url), env=self.env,
                                       keychain=NoKeychain())
                self.assertEqual(result.status, "unused(error:insecure-url)")

    def test_typesafe_custom_key_name(self) -> None:
        server = FakeServer(self, noul_answers({"a": 0.2}))
        cfg = typesafe_config(server.url())
        cfg["typesafe"]["key_name"] = "OTHER_TS_KEY"
        result = rerank.rerank("impact", candidates("a"), config=cfg, env=self.env, keychain=NoKeychain())
        self.assertEqual(result.status, "unused(no key)")
        result = rerank.rerank("impact", candidates("a"), config=cfg, env={"OTHER_TS_KEY": self.key},
                               keychain=NoKeychain())
        self.assertEqual(result.status, "used")

    def test_typesafe_advice(self) -> None:
        def respond(path, body):
            question = body["questions"]["tier"]
            assert question["type"] == "choice", question
            assert sorted(question["criteria"]) == ["high", "low", "max", "medium"], question
            return 200, {"model": "jev-test", "usage": {"input_tokens": 1, "output_tokens": 1},
                         "answers": {"tier": {"type": "choice", "choice": "max", "confidence": 0.65,
                                              "probabilities": {"low": 0.05, "medium": 0.1, "high": 0.2,
                                                                "max": 0.65}}}}, 0, {}
        server = FakeServer(self, respond)
        axes = {"kind": "migration", "intensity": "high", "reasoning": "high", "risk": "high", "tier": "high"}
        advice = rerank.advise_difficulty(axes, "x" * 20_000, config=typesafe_config(server.url()), env=self.env,
                                          keychain=NoKeychain())
        self.assertEqual((advice.status, advice.advisory_tier, advice.confidence, advice.tier, advice.truncated),
                         ("used", "max", 0.65, "high", True))
        body = server.requests[0]["body"]
        self.assertEqual(sorted(body["state"]), ["axes", "work"])
        self.assertEqual(body["state"]["axes"], {"kind": "migration", "intensity": "high",
                                                 "reasoning": "high", "risk": "high"})
        self.assertEqual(len(body["state"]["work"]), typesafe.WORK_LIMIT)
        self.assertIn("`work`", body["questions"]["tier"]["instructions"])



JUDGE_REQUEST = {"stage": "review", "tier": "high", "task": "never sent",
                 "axes": {"kind": "review", "intensity": "low", "reasoning": "high", "risk": "normal",
                          "tier": "high"}}
JUDGE_PAIRS = [("m-strong", "high"), ("m-strong", "max"), ("m-mid", "high")]


def choice_answer(choice):
    def respond(path, body):
        return 200, {"model": "jev-test", "usage": {"input_tokens": 1, "output_tokens": 1},
                     "answers": {"pair": {"type": "choice", "choice": choice, "confidence": 0.7,
                                          "probabilities": {f"c{i}": int(choice == f"c{i}")
                                                            for i in range(len(JUDGE_PAIRS))}}}}, 0, {}
    return respond


class TypeSafeModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = fake_key()
        self.env = {"TYPESAFE_API_KEY": self.key}

    def judge(self, server: FakeServer, **overrides) -> rerank.ModelJudgement:
        return rerank.judge_model(JUDGE_REQUEST, JUDGE_PAIRS, config=typesafe_config(server.url(), **overrides),
                                  env=self.env, keychain=NoKeychain())

    def test_request_carries_only_axes_tier_and_candidates(self) -> None:
        server = FakeServer(self, choice_answer("c0"))
        self.judge(server)
        body = server.requests[0]["body"]
        self.assertEqual(sorted(body["state"]), ["axes", "candidates", "stage", "tier"])
        self.assertEqual(body["state"]["axes"], {"kind": "review", "intensity": "low", "reasoning": "high",
                                                 "risk": "normal"})
        self.assertEqual((body["state"]["stage"], body["state"]["tier"]), ("review", "high"))
        self.assertEqual(body["state"]["candidates"], [{"model": m, "effort": e} for m, e in JUDGE_PAIRS])
        question = body["questions"]["pair"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(sorted(question["criteria"]), ["c0", "c1", "c2"])
        self.assertNotIn("never sent", json.dumps(body))
        self.assertEqual(server.requests[0]["headers"]["Authorization"], f"Bearer {self.key}")

    def test_answer_maps_back_to_the_pair(self) -> None:
        judged = self.judge(FakeServer(self, choice_answer("c2")))
        self.assertEqual((judged.status, judged.proposal, judged.provider), ("used", ("m-mid", "high"), "typesafe"))

    def test_unknown_choice_is_bad_response(self) -> None:
        for answer in ("c9", "m-mid", None):
            with self.subTest(answer=answer):
                judged = self.judge(FakeServer(self, choice_answer(answer)))
                self.assertEqual((judged.status, judged.proposal), ("unused(error:bad-response)", None))

    def test_timeout_is_unused(self) -> None:
        def slow(path, body):
            return 200, {"answers": {}}, 1.0, {}
        judged = self.judge(FakeServer(self, slow), timeout_seconds=0.2)
        self.assertEqual((judged.status, judged.proposal), ("unused(error:timeout)", None))

class KeyLeakTest(unittest.TestCase):
    def assert_private_values_absent(self, values, key: str, response_text: str) -> None:
        pending = list(values)
        seen = set()
        while pending:
            value = pending.pop()
            if id(value) in seen:
                continue
            seen.add(id(value))
            for private in (key, response_text):
                self.assertFalse(private in repr(value), "provider frame retains private data")
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, (tuple, list, set, frozenset)):
                pending.extend(value)
            elif isinstance(value, types.FunctionType):
                pending.extend(cell.cell_contents for cell in (value.__closure__ or ()))
                pending.extend((value.__defaults__, value.__kwdefaults__))
            elif not callable(value) and not isinstance(value, types.ModuleType) and hasattr(value, "__dict__"):
                pending.append(vars(value))

    def assert_sanitized_failure(self, action, code: str | None, key: str, response_text: str,
                                 exception_type=rerank.ProviderError) -> BaseException:
        failure = None
        try:
            action()
        except BaseException as exc:
            failure = exc
        self.assertIsNotNone(failure, "the provider must refuse the failed request")
        self.assertIs(type(failure), exception_type)
        pending = [failure]
        seen = set()
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            attributes = {name: getattr(error, name) for name in dir(error)
                          if name != "__traceback__" and not callable(getattr(error, name))}
            # Every package frame is in scope; fixtures and callers deliberately hold keys.
            frames = []
            current = error.__traceback__
            provider_root = pathlib.Path(rerank.__file__).parent.parent
            while current is not None:
                if pathlib.Path(current.tb_frame.f_code.co_filename).is_relative_to(provider_root):
                    frames.append(current)
                    self.assert_private_values_absent(current.tb_frame.f_locals.values(), key, response_text)
                current = current.tb_next
            provider_traceback = None
            for frame in reversed(frames):
                provider_traceback = types.TracebackType(provider_traceback, frame.tb_frame,
                                                        frame.tb_lasti, frame.tb_lineno)
            texts = (repr(error), repr(error.args), repr(attributes),
                     "".join(traceback.format_exception(error)),
                     "".join(traceback.TracebackException(type(error), error, provider_traceback,
                                                         capture_locals=True).format()))
            for text in texts:
                for private in (key, response_text):
                    self.assertFalse(private in text, "exception exposes credential or response data")
            pending.extend(item for item in attributes.values() if isinstance(item, BaseException))
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
        if exception_type is rerank.ProviderError:
            self.assertEqual(failure.code, code)
        elif not isinstance(failure, BaseExceptionGroup):
            self.assertEqual(failure.args, ())
        self.assertTrue(failure.__context__ is None, "provider errors must have no exception context")
        self.assertTrue(failure.__cause__ is None, "provider errors must have no exception cause")
        return failure

    def check_interrupted_request(self, exception_type, expected_type=None) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = response_text + key
        endpoint = "http://127.0.0.1:9876/v1/systemone"
        cfg = typesafe_config(endpoint)
        provider = typesafe.TypeSafeProvider()
        body = {"echo": echo, "answers": {"c0": {"type": "noul", "noul": 0.5},
                                          "tier": {"type": "choice", "choice": "high"}}}

        class InterruptedBytes(bytes):
            def decode(self, *args, **kwargs):
                raise exception_type(echo)

        class InterruptedBody(dict):
            def get(self, *args, **kwargs):
                raise exception_type(echo)

        for phase in ("transport", "read", "decode", "json", "answers"):
            with self.subTest(phase=phase):
                def http_open(handler, request):
                    if phase == "transport":
                        raise exception_type(echo)
                    response = urllib.response.addinfourl(
                        io.BytesIO(json.dumps(body).encode()), email.message.Message(), endpoint, 200)
                    response.msg = echo
                    if phase == "read":
                        response.read = mock.Mock(side_effect=exception_type(echo))
                    elif phase == "decode":
                        response.read = mock.Mock(return_value=InterruptedBytes(echo.encode()))
                    return response

                loads = json.loads

                def parse_json(raw):
                    if phase == "json":
                        raise exception_type(echo)
                    return InterruptedBody(body) if phase == "answers" else loads(raw)

                actions = [lambda: provider.score("impact", "q", candidates("a"), key=Secret(key), config=cfg),
                           lambda: provider.advise(AdviceTest.AXES, "task", key=Secret(key), config=cfg),
                           lambda: rerank.rerank("impact", candidates("a"), config=cfg,
                                                env={"TYPESAFE_API_KEY": key}, keychain=NoKeychain()),
                           lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=cfg,
                                                           env={"TYPESAFE_API_KEY": key},
                                                           keychain=NoKeychain())]
                if phase != "answers":
                    actions.extend((lambda: typesafe.post_json(endpoint, {}, key=Secret(key), timeout=5),
                                    lambda: provider._post({}, Secret(key), cfg)))
                else:
                    actions.extend((lambda: typesafe.parse_rerank_response(InterruptedBody(body), 1),
                                    lambda: typesafe.parse_advice_response(InterruptedBody(body))))
                with mock.patch.object(urllib.request.HTTPHandler, "http_open", http_open), \
                        mock.patch.object(json, "loads", parse_json):
                    for action in actions:
                        self.assert_sanitized_failure(action, None, key, response_text,
                                                      expected_type or exception_type)

    def test_keyboard_interrupt_has_no_private_traceback(self) -> None:
        self.check_interrupted_request(KeyboardInterrupt)

    def test_other_base_exceptions_have_no_private_traceback(self) -> None:
        for exception_type in (SystemExit, GeneratorExit):
            with self.subTest(exception_type=exception_type.__name__):
                self.check_interrupted_request(exception_type)

    def check_classified_failure(self, factory, code="internal", interruption_type=None,
                                 *, source="call") -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = key + response_text
        cfg = typesafe_config("http://127.0.0.1:9876/v1/systemone")
        accesses = []

        def fail(*args, **kwargs):
            accesses.append("fail")
            error = factory(echo)
            error.add_note(echo)
            error.private = echo
            error.__cause__ = ValueError(echo)
            error.__context__ = ValueError(echo)
            raise error

        class FailingProvider:
            name = "fake"

            def key_name(self, config):
                return "FAKE_RERANK_KEY"

            score = staticmethod(fail)
            advise = staticmethod(fail)

        class FailingBody(dict):
            get = fail

        class UntrustedNumber(float):
            __float__ = fail

        provider = typesafe.TypeSafeProvider()
        if source == "key-name":
            FailingProvider.key_name = fail
        actions = {
            "rerank": lambda: rerank.rerank("impact", candidates("a"), config=enabled(),
                                            env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(),
                                            providers={"fake": FailingProvider()}),
            "advice": lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=enabled(),
                                                      env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(),
                                                      providers={"fake": FailingProvider()}),
            "typesafe-score": lambda: provider.score("impact", "q", candidates("a"),
                                                      key=Secret(key), config=cfg),
            "typesafe-advice": lambda: provider.advise(AdviceTest.AXES, "task", key=Secret(key), config=cfg),
            "post": lambda: provider._post({}, Secret(key), cfg),
            "post-json": lambda: typesafe.post_json(cfg["typesafe"]["url"], {}, key=Secret(key), timeout=5),
            "parse-rerank": lambda: typesafe.parse_rerank_response(FailingBody(), 1),
            "parse-advice": lambda: typesafe.parse_advice_response(FailingBody()),
        }
        if source == "transport":
            actions["rerank"] = lambda: rerank.rerank("impact", candidates("a"), config=cfg,
                env={"TYPESAFE_API_KEY": key}, keychain=NoKeychain())
            actions["advice"] = lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=cfg,
                env={"TYPESAFE_API_KEY": key}, keychain=NoKeychain())
        if source in ("validation", "result", "provider-name"):
            number = UntrustedNumber(0.5) if source == "result" else 0.5
            if source == "result":
                number.private = echo
            FailingProvider.score = staticmethod(lambda *args, **kwargs: [number])
            FailingProvider.advise = staticmethod(lambda *args, **kwargs: ("high", number))
            if source == "provider-name":
                FailingProvider.name = property(fail)
            actions = {api: action for api, action in actions.items() if api in ("rerank", "advice")}
        for api, action in actions.items():
            if source == "validation":
                patcher = mock.patch.object(rerank, "_valid_scores", fail)
            elif source == "transport":
                patcher = mock.patch.object(urllib.request.HTTPHandler, "http_open", fail)
            else:
                patcher = mock.patch.object(typesafe, "_post_json", fail)
            with self.subTest(api=api), patcher:
                frames = []

                def capture(frame, event, arg):
                    if event == "return" and frame.f_globals.get("__name__", "").startswith("oah.rerank"):
                        frames.append(frame)

                previous_profile = sys.getprofile()
                try:
                    sys.setprofile(capture)
                    if interruption_type is not None or api not in ("rerank", "advice"):
                        self.assert_sanitized_failure(action, code, key, response_text,
                                                      interruption_type or rerank.ProviderError)
                    else:
                        result = None
                        failure_text = ""
                        try:
                            result = action()
                        except BaseException as exc:
                            # Inspect the actual chain before reporting a failed status contract.
                            failure_text = "".join(traceback.format_exception(exc))
                        self.assertFalse(key in failure_text or response_text in failure_text,
                                         "provider exception chain exposes private data")
                        self.assertIsNotNone(result, "ordinary provider failures must return a status")
                        self.assertEqual(result.status, f"unused(error:{code})")
                        self.assert_private_values_absent((result.to_record(),), key, response_text)
                        if api == "rerank":
                            self.assertEqual(names(result), ["a"])
                        else:
                            self.assertEqual(result.tier, AdviceTest.AXES["tier"])
                finally:
                    sys.setprofile(previous_profile)
                self.assertTrue(frames)
                for frame in frames:
                    with self.subTest(helper=frame.f_code.co_name):
                        self.assert_private_values_absent(frame.f_locals.values(), key, response_text)
        if source == "result":
            self.assertEqual(accesses, [], "result validation must never call provider hooks")

    def test_provider_error_groups_are_sanitized_before_reading_code(self) -> None:
        class Group(ExceptionGroup, rerank.ProviderError):
            pass

        for populated in (False, True):
            def factory(echo):
                error = Group(echo, [ValueError(echo)])
                if populated:
                    error.code = "timeout"
                return error

            with self.subTest(populated=populated):
                self.check_classified_failure(factory)

    def test_provider_failure_classification_matrix(self) -> None:
        accesses = []

        class MissingCode(rerank.ProviderError):
            pass

        class InvalidCode(rerank.ProviderError):
            pass

        class RaisingCode(Exception):
            @property
            def code(self):
                private = BaseException.__dict__["__dict__"].__get__(self).get("private")
                if private:
                    accesses.append("raising-code")
                    raise ValueError(private)
                return "internal"

        class PrivateCode(Exception):
            @property
            def code(self):
                private = BaseException.__dict__["__dict__"].__get__(self).get("private")
                if private:
                    accesses.append("private-code")
                    return private
                return "internal"

        bases = (KeyboardInterrupt, SystemExit, GeneratorExit, BaseExceptionGroup, ExceptionGroup,
                 rerank.ProviderError, MissingCode, InvalidCode, RaisingCode, PrivateCode,
                 OSError, ValueError)
        kinds = [(base.__name__, base) for base in bases]
        for first in bases:
            for second in bases:
                if first is second:
                    continue
                try:
                    mixed = type("MixedFailure", (first, second), {})
                except TypeError:
                    continue  # Python forbids conflicting layouts or inconsistent MROs.
                kinds.append((f"{first.__name__}+{second.__name__}", mixed))

        for label, kind in kinds:
            is_group = issubclass(kind, BaseExceptionGroup)
            interruption = next((base for base in (KeyboardInterrupt, SystemExit, GeneratorExit)
                                 if issubclass(kind, base)), None)
            expected_type = None
            if interruption is not None:
                try:
                    BaseException.__new__(kind)
                    expected_type = kind
                except TypeError:
                    expected_type = interruption
            valid_code = (issubclass(kind, rerank.ProviderError) and
                          not issubclass(kind, (MissingCode, InvalidCode, RaisingCode, PrivateCode)))
            code = "timeout" if valid_code and not is_group and interruption is None else "internal"
            for shape in ("direct", "grouped", "nested"):
                def factory(echo):
                    if is_group:
                        error = BaseExceptionGroup.__new__(kind, echo, [ValueError(echo)])
                        BaseException.__init__(error, echo, [ValueError(echo)])
                    else:
                        allocator = OSError if issubclass(kind, OSError) else BaseException
                        error = allocator.__new__(kind)
                        BaseException.__init__(error, echo)
                    attributes = BaseException.__dict__["__dict__"].__get__(error)
                    attributes["private"] = echo
                    if valid_code:
                        attributes["code"] = "timeout"
                    elif issubclass(kind, InvalidCode):
                        attributes["code"] = echo
                    if shape != "direct":
                        error = BaseExceptionGroup(echo, [ValueError(echo), error])
                    if shape == "nested":
                        error = BaseExceptionGroup(echo, [BaseExceptionGroup(echo, [error, error]),
                                                          SystemExit(echo)])
                    return error

                expected = expected_type
                if expected is None and shape == "nested":
                    expected = SystemExit
                for source in ("call", "validation", "result"):
                    with self.subTest(kind=label, shape=shape, source=source):
                        self.check_classified_failure(
                            factory, "bad-response" if source == "result" else
                            code if shape == "direct" else "internal",
                            None if source == "result" else expected, source=source)
                        self.assertEqual(accesses, [], "classification must never evaluate code properties")

    def test_provider_name_failures_use_the_same_classifier(self) -> None:
        for kind in (rerank.ProviderError, ValueError, KeyboardInterrupt, SystemExit, GeneratorExit):
            def factory(echo):
                return kind("timeout" if kind is rerank.ProviderError else echo)

            with self.subTest(kind=kind.__name__):
                self.check_classified_failure(factory, "timeout" if kind is rerank.ProviderError else "internal",
                                              kind if kind in base._INTERRUPTIONS else None,
                                              source="provider-name")

    def test_provider_results_reject_subclasses_without_running_hooks(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        accesses = []

        def fail(*args, **kwargs):
            accesses.append("hook")
            error = rerank.ProviderError("timeout")
            error.private = key + response_text
            error.__cause__ = ValueError(key + response_text)
            raise error

        class HookedList(list):
            __len__ = __iter__ = __getitem__ = fail

        class HookedTuple(tuple):
            __len__ = __iter__ = __getitem__ = fail

        class HookedInt(int):
            __float__ = fail

        class HookedText(str):
            __eq__ = __hash__ = fail

        class Impostor:
            __class__ = property(fail)

        pairs = (
            (HookedList([0.5]), ("high", 0.5), True, False),
            (HookedTuple((0.5,)), HookedTuple(("high", 0.5)), True, True),
            ([HookedInt(1)], ("high", HookedInt(1)), True, True),
            ([Impostor()], ("high", Impostor()), True, True),
            ([0.5], (HookedText("high"), 0.5), False, True),
        )
        for scores, answer, bad_scores, bad_advice in pairs:
            provider = FakeProvider(scores=lambda items: scores, advice=answer)
            for api, action, invalid in (
                ("rerank", lambda: rerank.rerank("impact", candidates("a"), config=enabled(),
                    env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(), providers={"fake": provider}),
                 bad_scores),
                ("advice", lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=enabled(),
                    env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(), providers={"fake": provider}),
                 bad_advice),
            ):
                with self.subTest(api=api, scores_type=type(scores).__name__, answer_type=type(answer).__name__):
                    result = None
                    try:
                        result = action()
                    except BaseException:
                        pass
                    self.assertIsNotNone(result, "invalid provider values must return a status")
                    self.assertEqual(result.status, "unused(error:bad-response)" if invalid else "used")
                    self.assert_private_values_absent((result.to_record(),), key, response_text)
        self.assertEqual(accesses, [], "result validation must use exact builtin types")

    def test_provider_text_results_reject_subclasses_without_running_hooks(self) -> None:
        key = fake_key()
        accesses = []

        def fail(*args, **kwargs):
            accesses.append("hook")
            raise ValueError(key)

        class HookedText(str):
            __eq__ = __hash__ = fail

        for field in ("name", "key-name"):
            class TextProvider(FakeProvider):
                name = HookedText("fake") if field == "name" else "fake"

                def key_name(self, config):
                    return HookedText("FAKE_RERANK_KEY") if field == "key-name" else "FAKE_RERANK_KEY"

            provider = TextProvider({"a": 0.5})
            for action in (
                lambda: rerank.rerank("impact", candidates("a"), config=enabled(),
                    env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(), providers={"fake": provider}),
                lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=enabled(),
                    env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(), providers={"fake": provider}),
            ):
                with self.subTest(field=field, api=action.__code__.co_firstlineno):
                    result = None
                    try:
                        result = action()
                    except BaseException:
                        pass
                    self.assertIsNotNone(result, "invalid provider text must return a status")
                    self.assertEqual(result.status, "unused(error:bad-response)")
                    self.assertEqual(provider.calls, [])
        self.assertEqual(accesses, [], "provider text must be an exact builtin string")

    def test_non_interruption_base_exception_is_internal(self) -> None:
        self.check_classified_failure(BaseException)

    def test_provider_code_lookup_never_runs_attribute_or_string_hooks(self) -> None:
        accesses = []

        class Hooked(rerank.ProviderError):
            def __getattribute__(self, name):
                if name in ("code", "__dict__", "__class__"):
                    accesses.append(name)
                    raise ValueError("private attribute hook")
                return super().__getattribute__(name)

        class HookedString(str):
            def __format__(self, spec):
                accesses.append("format")
                raise ValueError(str.__str__(self))

        for code_kind in ("plain", "subclass", "missing", "non-string"):
            def factory(echo):
                error = BaseException.__new__(Hooked)
                BaseException.__init__(error, echo)
                attributes = BaseException.__dict__["__dict__"].__get__(error)
                if code_kind == "plain":
                    attributes["code"] = "timeout"
                elif code_kind == "subclass":
                    attributes["code"] = HookedString(echo)
                elif code_kind == "non-string":
                    attributes["code"] = 123
                return error

            with self.subTest(code_kind=code_kind):
                self.check_classified_failure(factory, "timeout" if code_kind == "plain" else "internal")
                self.assertEqual(accesses, [], "classification must bypass provider attribute hooks")

    def test_provider_code_lookup_never_runs_dictionary_hooks(self) -> None:
        accesses = []

        class HookedDict(dict):
            def get(self, *args):
                accesses.append("get")
                raise ValueError("private dictionary hook")

            def items(self):
                accesses.append("items")
                raise ValueError("private dictionary hook")

        class CollidingKey:
            def __hash__(self):
                return hash("code")

            def __eq__(self, other):
                accesses.append("eq")
                raise ValueError("private equality hook")

        for colliding in (False, True):
            def factory(echo):
                error = BaseException.__new__(rerank.ProviderError)
                BaseException.__init__(error, echo)
                error.__dict__ = HookedDict({CollidingKey(): echo} if colliding else {"code": "timeout"})
                return error

            with self.subTest(colliding=colliding):
                self.check_classified_failure(factory, "internal" if colliding else "timeout")
                self.assertEqual(accesses, [], "classification must bypass dictionary and equality hooks")

    def test_provider_key_name_failures_use_the_same_classifier(self) -> None:
        for interruption in (None, KeyboardInterrupt, SystemExit, GeneratorExit):
            def factory(echo):
                if interruption is not None:
                    return BaseExceptionGroup(echo, [ValueError(echo), interruption(echo)])
                error = rerank.ProviderError("timeout")
                error.private = echo
                return error

            with self.subTest(interruption=interruption):
                self.check_classified_failure(factory, "timeout", interruption, source="key-name")

    def test_invalid_provider_key_names_do_not_survive_in_gate_frames(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)

        class InvalidKeyName(FakeProvider):
            def key_name(self, config):
                return key + response_text

        frames = []

        def capture(frame, event, arg):
            if event == "return" and frame.f_globals.get("__name__", "").startswith("oah."):
                frames.append(frame)

        previous_profile = sys.getprofile()
        try:
            sys.setprofile(capture)
            for action in (
                lambda: rerank.rerank("impact", candidates("a"), config=enabled(),
                    env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(),
                    providers={"fake": InvalidKeyName()}),
                lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=enabled(),
                    env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(),
                    providers={"fake": InvalidKeyName()}),
            ):
                self.assertEqual(action().status, "unused(error:key-name)")
        finally:
            sys.setprofile(previous_profile)
        self.assertTrue(frames)
        for frame in frames:
            with self.subTest(helper=frame.f_code.co_name):
                self.assert_private_values_absent(frame.f_locals.values(), key, response_text)

    def test_transport_groups_and_provider_errors_reach_the_shared_classifier(self) -> None:
        for first, second in ((ExceptionGroup, rerank.ProviderError), (ExceptionGroup, ValueError),
                              (rerank.ProviderError, OSError), (OSError, rerank.ProviderError),
                              (rerank.ProviderError, ValueError), (ValueError, rerank.ProviderError)):
            kind = type("MixedTransportFailure", (first, second), {})
            for grouped in (False, True):
                def factory(echo):
                    if issubclass(kind, BaseExceptionGroup):
                        error = BaseExceptionGroup.__new__(kind, echo, [ValueError(echo)])
                    else:
                        allocator = OSError if issubclass(kind, OSError) else BaseException
                        error = allocator.__new__(kind)
                        BaseException.__init__(error, echo)
                    BaseException.__dict__["__dict__"].__get__(error)["code"] = "timeout"
                    return ExceptionGroup(echo, [error]) if grouped else error

                code = "internal" if grouped or issubclass(kind, BaseExceptionGroup) else "timeout"
                with self.subTest(first=first.__name__, second=second.__name__, grouped=grouped):
                    self.check_classified_failure(factory, code, source="transport")

    def test_classification_failures_are_internal_or_fresh_interruptions(self) -> None:
        pattern = base._CODE_RE
        for phase in ("code", "cleanup"):
            for interruption in (None, KeyboardInterrupt, SystemExit, GeneratorExit):
                for grouped in (False, True):
                    private = None

                    def factory(echo):
                        nonlocal private
                        private = echo
                        error = BaseException.__new__(rerank.ProviderError)
                        BaseException.__init__(error, echo)
                        error.code = "timeout"
                        return error

                    def fail_classification(*args):
                        error = (interruption or ValueError)(private)
                        if grouped:
                            error = BaseExceptionGroup(private, [ValueError(private), error])
                        raise error

                    def fullmatch(code):
                        if code == "timeout":
                            fail_classification()
                        return pattern.fullmatch(code)

                    patcher = (mock.patch.object(base, "_CODE_RE", mock.Mock(fullmatch=fullmatch))
                               if phase == "code" else mock.patch.object(traceback, "clear_frames",
                                                                          fail_classification))
                    with self.subTest(phase=phase, interruption=interruption, grouped=grouped), patcher:
                        self.check_classified_failure(factory, "internal", interruption)

    def test_interruption_sanitizing_does_not_validate_provider_codes(self) -> None:
        pattern = mock.Mock()
        pattern.fullmatch.side_effect = ValueError("code validation failed")
        with mock.patch.object(base, "_CODE_RE", pattern):
            self.check_classified_failure(KeyboardInterrupt, interruption_type=KeyboardInterrupt)
        pattern.fullmatch.assert_not_called()

    def test_interruption_groups_propagate_through_public_apis(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = key + response_text
        cfg = typesafe_config("http://127.0.0.1:9876/v1/systemone")

        def http_open(handler, request):
            error = BaseExceptionGroup(echo, [KeyboardInterrupt(echo),
                BaseExceptionGroup(echo, [SystemExit(echo), ValueError(echo)])])
            error.add_note(echo)
            error.private = echo
            raise error

        for action in (
            lambda: rerank.rerank("impact", candidates("a"), config=cfg,
                                 env={"TYPESAFE_API_KEY": key}, keychain=NoKeychain()),
            lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=cfg,
                                            env={"TYPESAFE_API_KEY": key}, keychain=NoKeychain()),
        ):
            with self.subTest(api=action.__code__.co_firstlineno), \
                    mock.patch.object(urllib.request.HTTPHandler, "http_open", http_open):
                self.assert_sanitized_failure(action, None, key, response_text, KeyboardInterrupt)

    def check_group_failure(self, depth, exception_type, *, width=1, sanitizer_failure=False,
                           sibling_type=None, expected_type=None) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = key + response_text
        endpoint = "http://127.0.0.1:9876/v1/systemone"
        cfg = typesafe_config(endpoint)

        def fail(*args, **kwargs):
            error = exception_type(echo)
            if sibling_type is not None:
                error = BaseExceptionGroup(echo, [error, sibling_type(echo)])
            for _ in range(depth):
                error = BaseExceptionGroup(echo, [error] * width)
                error.add_note(echo)
                error.private = echo
            raise error

        class InterruptedProvider:
            name = "fake"

            def key_name(self, config):
                return "FAKE_RERANK_KEY"

            score = staticmethod(fail)
            advise = staticmethod(fail)

        provider = typesafe.TypeSafeProvider()
        actions = {
            "transport": lambda: typesafe.post_json(endpoint, {}, key=Secret(key), timeout=5),
            "score": lambda: provider.score("impact", "q", candidates("a"), key=Secret(key), config=cfg),
            "advice": lambda: provider.advise(AdviceTest.AXES, "task", key=Secret(key), config=cfg),
        }
        for source in ("transport", "plugin"):
            settings = cfg if source == "transport" else enabled()
            env = {"TYPESAFE_API_KEY" if source == "transport" else "FAKE_RERANK_KEY": key}
            providers = None if source == "transport" else {"fake": InterruptedProvider()}
            actions[source + "-rerank"] = lambda settings=settings, env=env, providers=providers: \
                rerank.rerank("impact", candidates("a"), config=settings, env=env,
                              keychain=NoKeychain(), providers=providers)
            actions[source + "-advice"] = lambda settings=settings, env=env, providers=providers: \
                rerank.advise_difficulty(AdviceTest.AXES, "task", config=settings, env=env,
                                        keychain=NoKeychain(), providers=providers)
        expected = (exception_type if issubclass(exception_type, (KeyboardInterrupt, SystemExit, GeneratorExit))
                    else rerank.ProviderError)
        if expected_type is not None:
            expected = expected_type
        for api, action in actions.items():
            with self.subTest(api=api):
                frames = []

                def capture(frame, event, arg):
                    if event == "return" and frame.f_globals.get("__name__", "").startswith("oah.rerank"):
                        frames.append(frame)

                clear_frames = traceback.clear_frames

                def clear(tb):
                    if sanitizer_failure:
                        raise RuntimeError(echo)
                    clear_frames(tb)

                previous_profile = sys.getprofile()
                try:
                    sys.setprofile(capture)
                    with mock.patch.object(urllib.request.HTTPHandler, "http_open", fail), \
                            mock.patch.object(traceback, "clear_frames", clear):
                        if expected is rerank.ProviderError and api.endswith(("-rerank", "-advice")):
                            result = None
                            try:
                                result = action()
                            except BaseException:
                                pass
                            self.assertIsNotNone(result, "ordinary provider failures must return a status")
                            self.assertEqual(result.status, "unused(error:internal)")
                        else:
                            self.assert_sanitized_failure(action, "internal", key, response_text, expected)
                finally:
                    sys.setprofile(previous_profile)
                self.assertTrue(frames)
                for frame in frames:
                    with self.subTest(helper=frame.f_code.co_name):
                        self.assert_private_values_absent(frame.f_locals.values(), key, response_text)

    def test_deep_interruption_groups_do_not_leak_or_get_swallowed(self) -> None:
        for depth in (600, 5000):
            for exception_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
                with self.subTest(depth=depth, interruption=exception_type.__name__):
                    self.check_group_failure(depth, exception_type)

    def test_mixed_group_allocation_failure_preserves_keyboard_interrupt(self) -> None:
        class StopNow(KeyboardInterrupt, OSError):
            pass

        self.check_group_failure(0, StopNow, sibling_type=KeyboardInterrupt,
                                 expected_type=KeyboardInterrupt)

    def test_provider_error_constructor_frames_drop_private_inputs(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        frames = []

        def capture(frame, event, arg):
            if event == "return" and frame.f_code is rerank.ProviderError.__init__.__code__:
                frames.append(frame)

        previous_profile = sys.getprofile()
        try:
            sys.setprofile(capture)
            error = rerank.ProviderError(key + response_text)
        finally:
            sys.setprofile(previous_profile)
        self.assertEqual(error.code, "provider")
        self.assertTrue(frames)
        for frame in frames:
            self.assert_private_values_absent(frame.f_locals.values(), key, response_text)

    def test_multiple_inheritance_interruptions_are_never_ordinary_errors(self) -> None:
        class ProviderErrorWithCode(rerank.ProviderError):
            code = "internal"

        class OrdinaryError(Exception):
            pass

        for base in (KeyboardInterrupt, SystemExit, GeneratorExit):
            for ordinary in (rerank.ProviderError, ProviderErrorWithCode, OSError, ValueError,
                             OrdinaryError):
                # SystemExit and OSError have incompatible built-in instance layouts.
                if base is SystemExit and ordinary is OSError:
                    continue
                interruption = type("MixedInterruption", (base, ordinary), {})
                with self.subTest(base=base.__name__, ordinary=ordinary.__name__):
                    expected = base if ordinary is OSError else interruption
                    self.check_interrupted_request(interruption, expected)
                    for depth in (0, 1, 3, 600):
                        with self.subTest(depth=depth):
                            self.check_group_failure(depth, interruption, expected_type=expected)

    def test_gate_interruptions_are_fresh_and_numeric_subclasses_are_rejected(self) -> None:
        class Interrupted(KeyboardInterrupt, ValueError):
            pass

        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = key + response_text

        class InterruptedNumber(float):
            def __new__(cls, value):
                number = super().__new__(cls, value)
                number.private = echo
                return number

            def __float__(self):
                raise Interrupted(echo)

        class InterruptedProvider:
            name = "fake"

            def key_name(self, config):
                if phase == "gate":
                    raise Interrupted(echo)
                return "FAKE_RERANK_KEY"

            def score(self, *args, **kwargs):
                return [InterruptedNumber(0.5)]

            def advise(self, *args, **kwargs):
                return "high", InterruptedNumber(0.5)

        for phase in ("gate", "validation"):
            for action in (
                lambda: rerank.rerank("impact", candidates("a"), config=enabled(),
                                     env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(),
                                     providers={"fake": InterruptedProvider()}),
                lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=enabled(),
                                                env={"FAKE_RERANK_KEY": key}, keychain=NoKeychain(),
                                                providers={"fake": InterruptedProvider()}),
            ):
                with self.subTest(phase=phase, api=action.__code__.co_firstlineno):
                    if phase == "gate":
                        self.assert_sanitized_failure(action, None, key, response_text, Interrupted)
                    else:
                        result = action()
                        self.assertEqual(result.status, "unused(error:bad-response)")
                        self.assert_private_values_absent((result.to_record(),), key, response_text)

    def test_wide_and_shared_groups_preserve_interruptions(self) -> None:
        for exception_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(interruption=exception_type.__name__):
                self.check_group_failure(1, exception_type, width=5000)
        # Shared subgroups must not expand exponentially while finding the fallback type.
        self.check_group_failure(600, GeneratorExit, width=2)

    def test_deep_groups_without_control_flow_use_fixed_provider_error(self) -> None:
        for depth in (600, 5000):
            with self.subTest(depth=depth):
                self.check_group_failure(depth, RuntimeError)

    def test_sanitizer_failure_discards_original_exception_and_context(self) -> None:
        for depth in (0, 3, 600, 5000):
            for exception_type in (KeyboardInterrupt, SystemExit, GeneratorExit, RuntimeError):
                with self.subTest(depth=depth, interruption=exception_type.__name__):
                    self.check_group_failure(depth, exception_type, sanitizer_failure=True)

    def test_nested_groups_raise_the_first_interruption(self) -> None:
        for exception_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(interruption=exception_type.__name__):
                self.check_group_failure(32, exception_type)

    def test_interruption_subclass_needing_arguments_is_not_swallowed(self) -> None:
        class NeedsArgument(KeyboardInterrupt):
            def __init__(self, message):
                super().__init__(message)

        self.check_interrupted_request(NeedsArgument)

    def test_public_frames_keep_keys_opaque_during_transport(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        cfg = typesafe_config("http://127.0.0.1:9876/v1/systemone")
        public_frames = []

        def http_open(handler, request):
            frame = sys._getframe(1)
            while frame is not None:
                if frame.f_globals.get("__name__") == rerank.__name__:
                    public_frames.append(frame.f_code.co_name)
                    self.assert_private_values_absent(frame.f_locals.values(), key, response_text)
                frame = frame.f_back
            raise KeyboardInterrupt(key + response_text)

        with mock.patch.object(urllib.request.HTTPHandler, "http_open", http_open):
            for action in (
                lambda: rerank.rerank("impact", candidates("a"), config=cfg,
                                     env={"TYPESAFE_API_KEY": key}, keychain=NoKeychain()),
                lambda: rerank.advise_difficulty(AdviceTest.AXES, "task", config=cfg,
                                                env={"TYPESAFE_API_KEY": key}, keychain=NoKeychain()),
            ):
                self.assert_sanitized_failure(action, None, key, response_text, KeyboardInterrupt)
        self.assertIn("rerank", public_frames)
        self.assertIn("advise_difficulty", public_frames)

    def test_sensitive_frames_clear_inputs_on_every_exit(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = response_text + key
        endpoint = "http://127.0.0.1:9876/v1/systemone"
        cfg = typesafe_config(endpoint)
        provider = typesafe.TypeSafeProvider()
        body = {"echo": echo, "answers": {"c0": {"type": "noul", "noul": 0.5},
                                          "tier": {"type": "choice", "choice": "high"}}}
        for exception_type in (None, RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(exception_type=exception_type):
                frames = []

                def capture(frame, event, arg):
                    if event == "return" and frame.f_globals.get("__name__") == typesafe.__name__:
                        frames.append(frame)

                def http_open(handler, request):
                    if exception_type is not None:
                        raise exception_type(echo)
                    response = urllib.response.addinfourl(
                        io.BytesIO(json.dumps(body).encode()), email.message.Message(), endpoint, 200)
                    response.msg = echo
                    return response

                previous_profile = sys.getprofile()
                try:
                    sys.setprofile(capture)
                    with mock.patch.object(urllib.request.HTTPHandler, "http_open", http_open):
                        for action in (
                            lambda: typesafe.post_json(endpoint, {}, key=Secret(key), timeout=5),
                            lambda: provider._post({}, Secret(key), cfg),
                            lambda: provider.score("impact", "q", candidates("a"), key=Secret(key), config=cfg),
                            lambda: provider.advise(AdviceTest.AXES, "task", key=Secret(key), config=cfg),
                            lambda: typesafe.parse_rerank_response(body, 1),
                            lambda: typesafe.parse_advice_response(body),
                        ):
                            try:
                                action()
                            except BaseException:
                                pass
                finally:
                    sys.setprofile(previous_profile)
                self.assertTrue(frames)
                for frame in frames:
                    with self.subTest(helper=frame.f_code.co_name):
                        self.assert_private_values_absent(frame.f_locals.values(), key, response_text)

    def test_malformed_response_errors_have_no_chain(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = (response_text + key).encode("utf-8")
        for label, payload in (("utf8", echo + b"\xff"), ("json", b'{"echo":"' + echo + b'",}')):
            with self.subTest(case=label):
                server = FakeServer(self, lambda path, body, p=payload: (200, p, 0, {}))
                self.assert_sanitized_failure(
                    lambda: typesafe.post_json(server.url(), {}, key=Secret(key), timeout=5),
                    "bad-response", key, response_text)

    def test_response_parser_failures_have_no_private_frames(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = response_text + key
        for answers in (None, {}, {"c0": {"type": "choice", "choice": echo},
                                  "tier": {"type": "noul", "noul": echo}},
                        {"c0": {"type": "noul", "noul": echo},
                         "tier": {"type": "choice", "choice": echo}}):
            with self.subTest():
                body = {"echo": echo, "answers": answers}
                for action in (lambda: typesafe.parse_rerank_response(body, 1),
                               lambda: typesafe.parse_advice_response(body)):
                    self.assert_sanitized_failure(action, "bad-response", key, response_text)

    def test_every_failure_path_has_only_sanitized_exception_data(self) -> None:
        key = fake_key()
        response_text = "private-response-" + token_source.token_hex(16)
        echo = response_text + key
        provider = typesafe.TypeSafeProvider()
        axes = AdviceTest.AXES
        cases = [
            ("bad-header", "invalid-header", 200, {}, 0, key + "\n", {}, 5, None),
            ("bad-url", "insecure-url", 200, {}, 0, key, {}, 5, f"https://{echo}\uff0f.invalid"),
            ("bad-request", "bad-request", 200, {}, 0, key, {echo: b"bytes"}, 5, None),
            ("bad-request-encoding", "bad-request", 200, {}, 0, key, {echo: "\ud800"}, 5, None),
            ("http-error", "unauthorized", 401, {"echo": echo}, 0, key, {}, 5, None),
            ("http-rate-limit", "rate-limited", 429, {"echo": echo}, 0, key, {}, 5, None),
            ("http-overloaded", "overloaded", 529, {"echo": echo}, 0, key, {}, 5, None),
            ("http-other", "http-422", 422, {"echo": echo}, 0, key, {}, 5, None),
            ("timeout", "timeout", 200, {"echo": echo}, 0.3, key, {}, 0.05, None),
            ("url-timeout", "timeout", 200, {}, 0, key, {}, 5, None),
            ("network", "network", 200, {}, 0, key, {}, 5, None),
            ("os-error", "network", 200, {}, 0, key, {}, 5, None),
            ("http-exception", "network", 200, {}, 0, key, {}, 5, None),
            ("transport-value-error", "network", 200, {}, 0, key, {}, 5, None),
            ("unexpected-error", "internal", 200, {}, 0, key, {}, 5, None),
            ("bad-utf8", "bad-response", 200, echo.encode() + b"\xff", 0, key, {}, 5, None),
            ("bad-json", "bad-response", 200, echo.encode(), 0, key, {}, 5, None),
            ("bad-shape", "bad-response", 200, {"echo": echo}, 0, key, {}, 5, None),
            ("number-overflow", "bad-response", 200,
             {"echo": echo, "answers": {"c0": {"type": "noul", "noul": 10**400},
                                        "tier": {"type": "choice", "choice": echo}}},
             0, key, {}, 5, None),
            ("json-integer-overflow", "bad-response", 200,
             ('{"echo":"' + echo + '","number":' + "9" * 5000 + '}').encode(),
             0, key, {}, 5, None),
            ("response-overflow", "bad-response", 200,
             (echo + "x" * typesafe.RESPONSE_LIMIT).encode(), 0, key, {}, 5, None),
            ("transport-overflow", "network", 200, {"echo": echo}, 0, key, {}, 10**400, None),
            ("invalid-port", "network", 200, {}, 0, key, {}, 5, f"http://127.0.0.1:{echo}"),
        ]
        for label, code, status, body, delay, credential, payload, timeout, url in cases:
            with self.subTest(case=label):
                endpoint = url or "http://127.0.0.1:9876/v1/systemone"

                def http_open(handler, request):
                    if delay:
                        raise socket.timeout(echo)
                    if label == "network":
                        raise urllib.error.URLError(echo)
                    if label == "url-timeout":
                        raise urllib.error.URLError(socket.timeout(echo))
                    errors = {"os-error": OSError, "http-exception": http.client.HTTPException,
                              "transport-value-error": ValueError, "unexpected-error": RuntimeError}
                    if label in errors:
                        raise errors[label](echo)
                    if label == "transport-overflow":
                        raise OverflowError(echo)
                    if label == "invalid-port":
                        # Run the real port validation without opening a socket.
                        urllib.parse.urlsplit(request.full_url).port
                    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                    response = urllib.response.addinfourl(io.BytesIO(data), email.message.Message(),
                                                         request.full_url, status)
                    response.msg = echo
                    return response

                with mock.patch.object(urllib.request.HTTPHandler, "http_open", http_open):
                    if label not in ("bad-shape", "number-overflow"):
                        cfg = typesafe_config(endpoint, timeout_seconds=timeout)
                        for action in (
                            lambda: typesafe.post_json(endpoint, payload, key=Secret(credential), timeout=timeout),
                            lambda: provider._post(payload, Secret(credential), cfg),
                        ):
                            self.assert_sanitized_failure(action, code, key, response_text)
                    # Shape and score validation happen in the provider after transport succeeds.
                    if label not in ("bad-request", "bad-request-encoding", "transport-overflow"):
                        cfg = typesafe_config(endpoint, timeout_seconds=timeout)
                        for action in (
                            lambda: provider.score("impact", "q", candidates("a"), key=Secret(credential), config=cfg),
                            lambda: provider.advise(axes, "task", key=Secret(credential), config=cfg),
                        ):
                            self.assert_sanitized_failure(action, code, key, response_text)

    def test_post_json_rejects_malformed_credentials_without_echoing(self) -> None:
        key = fake_key()
        for suffix in ("\n", "\r\nInjected: value", "\n value", "\x00", "\x7f", "Ł"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(rerank.ProviderError) as caught:
                    typesafe.post_json("http://127.0.0.1:1", {}, key=Secret(key + suffix), timeout=0.1)
                self.assertEqual(caught.exception.code, "invalid-header")
                self.assertNotIn(key, str(caught.exception))
                self.assertNotIn(key, repr(caught.exception))
                self.assertIsNone(caught.exception.__context__)

    def test_key_never_in_record_repr_or_errors(self) -> None:
        key = fake_key()
        env = {"TYPESAFE_API_KEY": key}

        def echo(path, body):
            return 401, {"detail": "bad key", "echo": "anything"}, 0, {}
        responders = [noul_answers({"a": 0.1, "b": 0.2}), echo, lambda path, body: (200, b"{", 0, {})]
        for respond in responders:
            server = FakeServer(self, respond)
            result = rerank.rerank("impact", candidates("a", "b"), config=typesafe_config(server.url()),
                                   env=env, keychain=NoKeychain())
            for text in (repr(result), str(result), json.dumps(result.to_record())):
                self.assertNotIn(key, text)
            advice = rerank.advise_difficulty({"kind": "docs", "intensity": "low", "reasoning": "low",
                                               "risk": "low", "tier": "low"}, "t",
                                              config=typesafe_config(server.url()), env=env,
                                              keychain=NoKeychain())
            for text in (repr(advice), json.dumps(advice.to_record())):
                self.assertNotIn(key, text)
        self.assertNotIn(key, repr(rerank.PROVIDERS))

    def test_provider_error_text_has_no_key(self) -> None:
        key = fake_key()
        server = FakeServer(self, lambda path, body: (401, {"detail": key}, 0, {}))
        with self.assertRaises(rerank.ProviderError) as caught:
            typesafe.post_json(server.url(), {"model": "m", "state": "s", "questions": {}}, key=Secret(key), timeout=5)
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertNotIn(key, str(caught.exception))
        self.assertNotIn(key, repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)


class ConfigTest(unittest.TestCase):
    def test_defaults_validate_with_windows_newline_translation(self) -> None:
        write_text = pathlib.Path.write_text

        def windows_write(path, data, *args, **kwargs):
            kwargs.setdefault("newline", "\r\n")
            return write_text(path, data, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "write_text", windows_write):
            self.test_defaults_validate_with_config_loader()

    def test_windows_path_in_literal_toml_string(self) -> None:
        root = make_tmp_dir(self)
        (root / ".oah").mkdir()
        path = r"D:\oah-cache\skills"
        (root / ".oah" / "config.toml").write_text(
            f"[paths]\ncache = '{path}'\n", encoding="utf-8")
        self.assertEqual(load_config(root).section("paths", {"cache": ""})["cache"], path)

    def test_defaults_validate_with_config_loader(self) -> None:
        root = make_tmp_dir(self)
        (root / ".oah").mkdir()
        (root / ".oah" / "config.toml").write_text(
            '[rerank]\r\nenabled = true\r\nprovider = "typesafe"\r\ntimeout_seconds = 5\r\n'
            '[rerank.typesafe]\r\nmodel = "jev-latest"\r\nkey_name = "TYPESAFE_API_KEY"\r\n',
            encoding="utf-8", newline="")
        section = load_config(root).section("rerank", rerank.DEFAULTS)
        self.assertIs(section["enabled"], True)
        self.assertEqual(section["timeout_seconds"], 5.0)
        self.assertEqual(section["typesafe"]["url"], rerank.DEFAULTS["typesafe"]["url"])
        (root / ".oah" / "config.toml").write_text('[rerank]\nkey = "x"\n', encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_config(root).section("rerank", rerank.DEFAULTS)
        self.assertEqual(caught.exception.code, "OAH_CONFIG_UNKNOWN_KEY")

    def test_default_url_is_the_documented_endpoint(self) -> None:
        self.assertEqual(rerank.DEFAULTS["typesafe"]["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertIs(rerank.DEFAULTS["enabled"], False)
        self.assertEqual(rerank.DEFAULTS["provider"], "")


INTENT = ("<!-- OAH_INTENT:BEGIN -->\n## Intent\nIntent-ID: t1\nRevision: 2\nStatus: accepted\n"
          "### Problem\nReports miss files.\n### Desired outcome\nEvery file is listed.\n"
          "### Success conditions\n- AC1: the report lists every file\n### Constraints\nStandard library only.\n"
          "### Non-goals\nNo new report format.\n### Open questions\nWhich folders count?\n"
          "<!-- OAH_INTENT:END -->\n")


def key_shaped() -> str:
    """A value the privacy scanner's key rule flags, built at run time; never a real credential."""
    return "s" + "k-" + token_source.token_hex(16)


class WorkTextTest(unittest.TestCase):
    """What Jev reads to judge difficulty: the Intent's five parts and the Scope paths, nothing else."""

    def test_the_five_intent_parts_then_the_scope_paths(self) -> None:
        work = rerank.intent_work(INTENT, ("src/report.py", "docs/report.md"))
        self.assertEqual(work.text, "### Problem\nReports miss files.\n### Desired outcome\nEvery file is listed.\n"
                                    "### Success conditions\n- AC1: the report lists every file\n### Constraints\n"
                                    "Standard library only.\n### Non-goals\nNo new report format.\n"
                                    "### Scope\nsrc/report.py\ndocs/report.md")
        for left_out in ("Intent-ID", "Revision", "Status", "Open questions", "Which folders count?", "OAH_INTENT"):
            self.assertNotIn(left_out, work.text)
        self.assertEqual((work.truncated, work.secret), (False, None))

    def test_a_comment_inside_a_part_keeps_the_lines_after_it(self) -> None:
        commented = INTENT.replace("### Success conditions\n", "### Success conditions\n<!-- Outcomes only,\n"
                                                                "one bullet each. -->\n")
        work = rerank.intent_work(commented, ())
        self.assertIn("### Success conditions\n<!-- Outcomes only,\none bullet each. -->\n- AC1: the report lists "
                      "every file\n### Constraints", work.text)
        self.assertNotIn("OAH_INTENT", work.text)

    def test_crlf_intent_reads_the_same(self) -> None:
        self.assertEqual(rerank.intent_work(INTENT.replace("\n", "\r\n"), ("a.py",)),
                         rerank.intent_work(INTENT, ("a.py",)))

    def test_a_long_intent_is_cut_to_the_limit_from_the_end_scope_first(self) -> None:
        long = INTENT.replace("Reports miss files.", "y" * 40_000)
        work = rerank.intent_work(long, ("src/never-sent.py",))
        self.assertEqual(rerank.WORK_LIMIT, 16_000)
        self.assertEqual(len(work.text), 16_000)
        self.assertTrue(work.truncated)
        self.assertTrue(work.text.startswith("### Problem\nyyy"))
        self.assertNotIn("src/never-sent.py", work.text)
        self.assertEqual(rerank.intent_work(INTENT, ("a.py",)).truncated, False)

    def test_a_long_scope_is_cut_before_the_intent(self) -> None:
        paths = [f"src/module_{number:05}.py" for number in range(2_000)]
        work = rerank.intent_work(INTENT, paths)
        self.assertEqual(len(work.text), 16_000)
        self.assertTrue(work.truncated)
        self.assertIn("### Non-goals\nNo new report format.\n### Scope\nsrc/module_00000.py", work.text)

    def test_the_cut_never_splits_a_multibyte_character(self) -> None:
        for char in ("가", "\U0001F600", "é"):
            with self.subTest(char=char):
                work = rerank.request_work(char * 20_000, ())
                self.assertEqual(len(work.text), 16_000)
                payload = json.dumps({"work": work.text}, ensure_ascii=False).encode("utf-8")
                self.assertEqual(json.loads(payload.decode("utf-8"))["work"], work.text)
                self.assertTrue(work.text.endswith(char))

    def test_request_text_then_scope_paths(self) -> None:
        work = rerank.request_work("Add a dark mode to the settings page.\n", ("src/settings.py",))
        self.assertEqual(work.text, "### Request\nAdd a dark mode to the settings page.\n### Scope\nsrc/settings.py")
        self.assertEqual(rerank.request_work("Only text.", ()).text, "### Request\nOnly text.")

    def test_a_secret_shaped_value_names_its_part_never_the_match(self) -> None:
        secret = key_shaped()
        home = "/" + "Us" + "ers/" + "someone/notes.txt"
        cases = (
            (rerank.intent_work(INTENT.replace("Reports miss files.", f"Use the key {secret}."), ("a.py",)),
             "the Intent text"),
            (rerank.intent_work(INTENT, ("src/a.py", f"src/{secret}.py")), "the Scope paths"),
            (rerank.intent_work(INTENT, (home,)), "the Scope paths"),
            (rerank.request_work(f"Rotate {secret} today.", ()), "the request text"),
        )
        for work, part in cases:
            with self.subTest(part=part):
                self.assertEqual(work.secret, part)
                self.assertNotIn(secret, work.secret)

    def test_a_secret_beyond_the_cut_still_counts(self) -> None:
        secret = key_shaped()
        work = rerank.intent_work(INTENT.replace("No new report format.", "z" * 20_000 + " " + secret), ())
        self.assertTrue(work.truncated)
        self.assertEqual(work.secret, "the Intent text")

    def test_a_commit_sha_is_not_a_secret(self) -> None:
        sha = token_source.token_hex(20)
        work = rerank.intent_work(INTENT.replace("Reports miss files.", f"Since {sha} reports miss files."), ())
        self.assertIsNone(work.secret)
        self.assertIn(sha, work.text)


class DifficultyRequestTest(unittest.TestCase):
    AXES = {"kind": "implementation", "intensity": "medium", "reasoning": "high", "risk": "normal", "tier": "high"}

    def setUp(self) -> None:
        self.key = fake_key()
        self.provider = FakeProvider(advice=("max", 0.9))
        self.env = {"FAKE_RERANK_KEY": self.key}
        self.state = make_tmp_dir(self) / "state"
        self.notices: list[str] = []

    def advise(self, work, cfg: dict | None = None, **kwargs) -> rerank.Advice:
        kwargs.setdefault("state_dir", self.state)
        return rerank.advise_difficulty(self.AXES, work, config=enabled() if cfg is None else cfg, env=self.env,
                                        keychain=NoKeychain(), providers={"fake": self.provider},
                                        notify=self.notices.append, **kwargs)

    def test_the_provider_receives_the_work_text_and_the_four_axes(self) -> None:
        work = rerank.intent_work(INTENT, ("src/report.py",))
        advice = self.advise(work)
        self.assertEqual((advice.status, advice.advisory_tier, advice.confidence, advice.truncated),
                         ("used", "max", 0.9, False))
        (call,) = self.provider.calls
        self.assertEqual(call[1], self.AXES)
        self.assertEqual(call[2], work.text)

    def test_a_cut_is_recorded(self) -> None:
        work = rerank.intent_work(INTENT.replace("Reports miss files.", "y" * 40_000), ())
        advice = self.advise(work)
        self.assertTrue(advice.truncated)
        self.assertEqual(len(self.provider.calls[0][2]), rerank.WORK_LIMIT)

    def test_secret_shaped_text_sends_nothing(self) -> None:
        secret = key_shaped()
        for work, part in ((rerank.intent_work(INTENT.replace("Reports miss files.", secret), ()), "the Intent text"),
                           (rerank.intent_work(INTENT, (f"src/{secret}",)), "the Scope paths")):
            with self.subTest(part=part):
                advice = self.advise(work)
                self.assertEqual((advice.status, advice.note), ("unused(secret)", part))
                self.assertIsNone(advice.advisory_tier)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.notices, [])
        self.assertFalse(self.state.exists(), "nothing was sent, so the notice was not recorded")

    def test_empty_text_sends_nothing(self) -> None:
        advice = self.advise(rerank.request_work("  \n", ()))
        self.assertEqual(advice.status, "unused(no-text)")
        self.assertEqual(self.provider.calls, [])

    def test_off_stops_every_request_before_the_text_is_read(self) -> None:
        secret = key_shaped()
        for cfg in (config(enabled=False, provider="fake"), config()):
            with self.subTest(cfg=cfg):
                advice = self.advise(rerank.intent_work(INTENT, (f"src/{secret}",)), cfg)
                self.assertEqual(advice.status, "unused(not-enabled)")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.notices, [])

    def test_the_notice_is_printed_once_per_project(self) -> None:
        work = rerank.intent_work(INTENT, ("a.py",))
        self.advise(work)
        self.advise(work)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.notices, [rerank.SCOPE_NOTICE])
        self.assertTrue(rerank.SCOPE_NOTICE.startswith("OAH_RERANK_SCOPE_WIDENED: Jev now receives this task's "
                                                       "Intent text and Scope paths (up to 16,000 characters)"))
        self.assertIn("set enabled = false under [rerank] to stop it", rerank.SCOPE_NOTICE)
        marker = self.state / rerank.NOTICE_MARKER
        self.assertTrue(marker.is_file())
        marker.unlink()
        self.advise(work)
        self.assertEqual(self.notices, [rerank.SCOPE_NOTICE] * 2)

    def test_an_unreadable_marker_means_not_yet_shown(self) -> None:
        work = rerank.intent_work(INTENT, ("a.py",))
        self.state.mkdir(parents=True)
        (self.state / rerank.NOTICE_MARKER).mkdir()  # a folder in its place cannot be read as the marker
        advice = self.advise(work)
        self.assertEqual(advice.status, "used")
        self.assertEqual(self.notices, [rerank.SCOPE_NOTICE])

    def test_an_unwritable_state_folder_does_not_block(self) -> None:
        blocked = make_tmp_dir(self) / "not-a-folder"
        blocked.write_text("a file where the state folder should be", encoding="utf-8")
        work = rerank.intent_work(INTENT, ("a.py",))
        for _ in range(2):
            advice = self.advise(work, state_dir=blocked)
            self.assertEqual(advice.status, "used")
        self.assertEqual(self.notices, [rerank.SCOPE_NOTICE] * 2, "not recorded, so it is shown again")

    def test_a_correction_shows_the_notice_but_writes_nothing(self) -> None:
        self.advise(rerank.intent_work(INTENT, ("a.py",)), record_notice=False)
        self.assertEqual(self.notices, [rerank.SCOPE_NOTICE])
        self.assertFalse(self.state.exists())

    def test_without_a_state_folder_no_notice_is_shown(self) -> None:
        self.advise(rerank.intent_work(INTENT, ("a.py",)), state_dir=None)
        self.assertEqual(self.notices, [])
        self.assertEqual(len(self.provider.calls), 1)

    def test_typesafe_receives_only_the_work_and_the_axes(self) -> None:
        sha = token_source.token_hex(20)
        server = FakeServer(self, lambda path, body: (200, {"answers": {"tier": {
            "type": "choice", "choice": "high", "confidence": 0.7}}}, 0, {}))
        work = rerank.intent_work(INTENT.replace("Reports miss files.", f"Since {sha}, reports miss files."),
                                  ("src/report.py",))
        advice = rerank.advise_difficulty(self.AXES, work, config=typesafe_config(server.url()),
                                          env={"TYPESAFE_API_KEY": self.key}, keychain=NoKeychain(),
                                          state_dir=self.state, notify=self.notices.append)
        self.assertEqual((advice.status, advice.advisory_tier), ("used", "high"))
        body = server.requests[0]["body"]
        self.assertEqual(sorted(body["state"]), ["axes", "work"])
        self.assertEqual(body["state"]["work"], work.text)
        self.assertIn(sha, body["state"]["work"])
        self.assertEqual(body["state"]["axes"], {name: self.AXES[name] for name in rerank.AXES})

    def test_typesafe_payload_keeps_whole_characters_at_the_cut(self) -> None:
        server = FakeServer(self, lambda path, body: (200, {"answers": {"tier": {
            "type": "choice", "choice": "high", "confidence": 0.7}}}, 0, {}))
        work = rerank.request_work("\U0001F600" * 20_000, ())
        advice = rerank.advise_difficulty(self.AXES, work, config=typesafe_config(server.url()),
                                          env={"TYPESAFE_API_KEY": self.key}, keychain=NoKeychain())
        self.assertEqual(advice.status, "used")
        sent = server.requests[0]["body"]["state"]["work"]
        self.assertEqual(sent, work.text)
        self.assertEqual(len(sent), rerank.WORK_LIMIT)


class DifficultyDecisionTest(unittest.TestCase):
    def advice(self, tier: str | None, confidence: float | None, status: str = "used") -> rerank.Advice:
        return rerank.Advice("medium", tier, confidence, status, "fake")

    def test_jev_tier_counts_at_or_above_the_threshold(self) -> None:
        for jev, expected in (("high", "high"), ("max", "max"), ("medium", "medium"), ("low", "low")):
            with self.subTest(jev=jev):
                decision = rerank.decide(self.advice(jev, 0.6), floor_tier="low", threshold=0.6)
                self.assertEqual((decision.tier, decision.used, decision.status, decision.raised),
                                 (expected, True, "used", False))

    def test_a_tier_below_the_floor_is_raised_to_it(self) -> None:
        decision = rerank.decide(self.advice("low", 0.9), floor_tier="medium", threshold=0.6)
        self.assertEqual((decision.tier, decision.used, decision.raised), ("medium", True, True))

    def test_below_the_threshold_or_no_confidence_keeps_the_rule_tier(self) -> None:
        for confidence, status in ((0.59, "below-threshold 0.59"), (None, "below-threshold none"),
                                   (0.0, "below-threshold 0")):
            with self.subTest(confidence=confidence):
                decision = rerank.decide(self.advice("max", confidence), floor_tier="low", threshold=0.6)
                self.assertEqual((decision.tier, decision.used, decision.status), ("medium", False, status))

    def test_an_unused_judgement_keeps_the_rule_tier(self) -> None:
        for status in ("unused(no key)", "unused(error:timeout)", "unused(secret)"):
            with self.subTest(status=status):
                decision = rerank.decide(self.advice(None, None, status), floor_tier="low", threshold=0.6)
                self.assertEqual((decision.tier, decision.used, decision.status), ("medium", False, status))

    def test_the_threshold_is_a_finite_number_from_0_to_1(self) -> None:
        self.assertEqual(rerank.DEFAULTS["difficulty_min_confidence"], 0.6)
        self.assertEqual(rerank.min_confidence(rerank.DEFAULTS), 0.6)
        for value in (0, 0.0, 0.25, 1, 1.0):
            with self.subTest(value=value):
                self.assertEqual(rerank.min_confidence(config(difficulty_min_confidence=value)), float(value))
        for value in (-0.1, 1.01, float("nan"), float("inf"), 10**400, True, "0.6", None):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                rerank.min_confidence(config(difficulty_min_confidence=value))
            self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
            self.assertIn("difficulty_min_confidence", str(caught.exception))

    def test_the_threshold_is_read_from_the_config_file(self) -> None:
        root = make_tmp_dir(self)
        (root / ".oah").mkdir()
        for text, expected in (("0.75", 0.75), ("1", 1.0), ("nan", None), ("1.5", None), ('"high"', None)):
            with self.subTest(text=text):
                (root / ".oah" / "config.toml").write_text(f"[rerank]\ndifficulty_min_confidence = {text}\n",
                                                           encoding="utf-8")
                if expected is None:
                    with self.assertRaises(ConfigError) as caught:
                        rerank.min_confidence(load_config(root).section("rerank", rerank.DEFAULTS))
                    self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
                else:
                    self.assertEqual(rerank.min_confidence(load_config(root).section("rerank", rerank.DEFAULTS)),
                                     expected)


if __name__ == "__main__":
    unittest.main()
