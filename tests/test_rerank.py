# oah.rerank 테스트 — 동의·키 관문 순서, 순서만 바꾸는 계약, 가짜 HTTP 서버로 TypeSafe 요청 형식과 키 비노출을 확인한다.
from __future__ import annotations

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

from oah import rerank
from oah.config import ConfigError, load_config
from oah.rerank import typesafe
from oah.secrets import Secret
from tests.helpers import make_tmp_dir


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
        advice = rerank.advise_difficulty(axes, "x" * 900, config=typesafe_config(server.url()), env=self.env,
                                          keychain=NoKeychain())
        self.assertEqual((advice.status, advice.advisory_tier, advice.confidence, advice.tier),
                         ("used", "max", 0.65, "high"))
        body = server.requests[0]["body"]
        self.assertEqual(sorted(body["state"]), ["axes", "task"])
        self.assertEqual(body["state"]["axes"], {"kind": "migration", "intensity": "high",
                                                 "reasoning": "high", "risk": "high"})
        self.assertEqual(len(body["state"]["task"]), typesafe.TASK_LIMIT)


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
        for exception_type in (SystemExit, GeneratorExit, BaseException):
            with self.subTest(exception_type=exception_type.__name__):
                self.check_interrupted_request(exception_type)

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

    def test_gate_and_score_validation_interruptions_are_fresh(self) -> None:
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
                    self.assert_sanitized_failure(action, None, key, response_text, Interrupted)

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


if __name__ == "__main__":
    unittest.main()
