# TypeSafe Jev 어댑터 — 공개 API(POST /v1/systemone, Bearer 인증)의 요청 형식을 build/parse 함수에 가두고 urllib 로 시간 제한을 두고 보낸다.
"""TypeSafe Jev provider.

Follows the public API reference at docs.typesafe.ai (`api/system-one.md` and `api/errors.md`, read
2026-10-23): `POST https://api.typesafe.ai/v1/systemone` with `Authorization: Bearer <key>` and a
JSON body `{model, state, questions}`. Every request format lives in the `build_*` and `parse_*`
functions below, so an API change touches only them.

Data sent, and nothing else:
- re-rank: the task kind, the caller's query, and each candidate's name and description (cut to
  DESCRIPTION_LIMIT characters), with one yes/no (Noul) question per candidate in one request;
- difficulty advice: the four difficulty axes and the task summary (cut to TASK_LIMIT characters),
  with one Choice question over the tiers.

The key goes only into the Authorization header. Errors carry a short code, never response text.
"""
from __future__ import annotations

import http.client
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Sequence, TypeVar, cast

from oah.rerank.base import TIERS, Candidate, ProviderError

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_KEY_NAME = "TYPESAFE_API_KEY"
DESCRIPTION_LIMIT = 200
TASK_LIMIT = 500
RESPONSE_LIMIT = 4 * 1024 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
STATUS_CODES = {401: "unauthorized", 429: "rate-limited", 529: "overloaded"}

TIER_CRITERIA = {
    "low": "Routine and well specified: a small, low-risk change that a quick pass can finish.",
    "medium": "Ordinary work that needs care across a few files or some design judgement.",
    "high": "Hard work: several interacting parts, subtle correctness, or real risk if wrong.",
    "max": "The hardest work: critical risk, deep reasoning, or a change that is costly to undo.",
}

_T = TypeVar("_T")


def _outcome(action: Callable[..., tuple[_T | None, str | None]],
             *args, **kwargs) -> tuple[_T | None, str | None]:
    """Finish sensitive work here; never return an exception or its traceback to the caller."""
    try:
        return action(*args, **kwargs)
    except ProviderError as exc:
        return None, exc.code
    except Exception:  # An unexpected transport/parser failure must not expose its locals either.
        return None, "internal"


def _finish(result: _T | None, code: str | None) -> _T:
    """Raise only after sensitive helpers returned and their callers dropped every input."""
    if code is not None:
        raise ProviderError(code).with_traceback(None) from None
    return cast(_T, result)


def build_rerank_request(model: str, kind: str, query: str, candidates: Sequence[Candidate]) -> dict:
    state = {
        "task_kind": kind,
        "query": query,
        "candidates": [{"name": item.name, "description": item.description[:DESCRIPTION_LIMIT]}
                       for item in candidates],
    }
    questions = {
        f"c{index}": {
            "type": "noul",
            "instructions": f"Is `candidates[{index}]` relevant to `query` for a `task_kind` task?",
            "criteria": {
                "true": "The candidate directly helps answer or carry out the query.",
                "false": "The candidate is unrelated to the query, or only loosely related.",
            },
        }
        for index in range(len(candidates))
    }
    return {"model": model, "state": state, "questions": questions}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _answers(body: object) -> Mapping | None:
    answers = body.get("answers") if isinstance(body, Mapping) else None
    return answers if isinstance(answers, Mapping) else None


def parse_rerank_response(body: object, count: int) -> list[float]:
    result, code = _outcome(_parse_rerank_response, body, count)
    del body, count
    return _finish(result, code)


def _parse_rerank_response(body: object, count: int) -> tuple[list[float] | None, str | None]:
    answers = _answers(body)
    if answers is None:
        return None, "bad-response"
    scores = []
    for index in range(count):
        answer = answers.get(f"c{index}")
        if not isinstance(answer, Mapping) or answer.get("type") != "noul":
            return None, "bad-response"
        noul = _number(answer.get("noul"))
        if noul is None or not 0.0 <= noul <= 1.0:
            return None, "bad-response"
        scores.append(noul)
    return scores, None


def build_advice_request(model: str, axes: Mapping[str, str], task: str) -> dict:
    state = {
        "task": task[:TASK_LIMIT],
        "axes": {name: axes[name] for name in ("kind", "intensity", "reasoning", "risk") if name in axes},
    }
    questions = {
        "tier": {
            "type": "choice",
            "instructions": "Which effort tier does `task` need, given its difficulty `axes`?",
            "criteria": dict(TIER_CRITERIA),
        }
    }
    return {"model": model, "state": state, "questions": questions}


def parse_advice_response(body: object) -> tuple[str, float | None]:
    result, code = _outcome(_parse_advice_response, body)
    del body
    return _finish(result, code)


def _parse_advice_response(body: object) -> tuple[tuple[str, float | None] | None, str | None]:
    answers = _answers(body)
    answer = answers.get("tier") if answers is not None else None
    if not isinstance(answer, Mapping) or answer.get("type") != "choice" or answer.get("choice") not in TIERS:
        return None, "bad-response"
    return (answer["choice"], _number(answer.get("confidence"))), None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the Authorization header to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib signature
        return None


def _check_url(url: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        parts = None
    if parts is None:
        return "insecure-url"
    if parts.scheme == "https" and host:
        return
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return
    return "insecure-url"


def post_json(url: str, payload: Mapping, *, key: str, timeout: float) -> object:
    """POST payload as JSON and return the parsed reply; any failure raises ProviderError(code)."""
    result, code = _outcome(_post_json, url, payload, key=key, timeout=timeout)
    # URLs, payloads and config may contain echoed credentials too. Keep none in an error frame.
    del url, payload, key, timeout
    return _finish(result, code)


def _post_json(url: str, payload: Mapping, *, key: str, timeout: float) -> tuple[object, str | None]:
    code = _check_url(url)
    if code is not None:
        return None, code
    # Bearer credentials must be visible ASCII, with no whitespace or control characters.
    # Reject them before http.client can include a rejected header value in an exception.
    if not isinstance(key, str) or not key or any(not 0x21 <= ord(char) <= 0x7e for char in key):
        return None, "invalid-header"
    data = None
    try:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        pass
    if data is None:
        return None, "bad-request"
    from oah import __version__

    code: str | None = None
    raw = b""
    try:
        request = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": f"open-agent-harness/{__version__}",
        })
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(RESPONSE_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        code = STATUS_CODES.get(exc.code, f"http-{exc.code}")
        exc.close()
    except urllib.error.URLError as exc:
        code = "timeout" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "network"
    except (socket.timeout, TimeoutError):
        code = "timeout"
    except (OSError, http.client.HTTPException, ValueError, OverflowError):
        code = "network"
    if code is not None:
        return None, code
    if len(raw) > RESPONSE_LIMIT:
        return None, "bad-response"
    try:
        return json.loads(raw.decode("utf-8")), None
    except ValueError:  # Includes UnicodeDecodeError, JSONDecodeError and oversized JSON integers.
        pass
    return None, "bad-response"


class TypeSafeProvider:
    name = "typesafe"

    @staticmethod
    def _settings(config: Mapping) -> Mapping:
        settings = config.get("typesafe")
        return settings if isinstance(settings, Mapping) else {}

    def key_name(self, config: Mapping) -> str:
        return str(self._settings(config).get("key_name") or DEFAULT_KEY_NAME)

    def _post(self, payload: dict, key: str, config: Mapping) -> object:
        result, code = _outcome(self._post_result, payload, key, config)
        del self, payload, key, config
        return _finish(result, code)

    def _post_result(self, payload: dict, key: str, config: Mapping) -> tuple[object, str | None]:
        settings = self._settings(config)
        timeout = _number(config.get("timeout_seconds")) or 10.0
        return _post_json(str(settings.get("url") or DEFAULT_URL), payload, key=key, timeout=timeout)

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: str,
              config: Mapping) -> list[float]:
        result, code = _outcome(self._score, kind, query, candidates, key=key, config=config)
        del self, kind, query, candidates, key, config
        return _finish(result, code)

    def _score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: str,
               config: Mapping) -> tuple[list[float] | None, str | None]:
        model = str(self._settings(config).get("model") or DEFAULT_MODEL)
        body, code = self._post_result(build_rerank_request(model, kind, query, candidates), key, config)
        if code is not None:
            return None, code
        return _parse_rerank_response(body, len(candidates))

    def advise(self, axes: Mapping[str, str], task: str, *, key: str,
               config: Mapping) -> tuple[str, float | None]:
        result, code = _outcome(self._advise, axes, task, key=key, config=config)
        del self, axes, task, key, config
        return _finish(result, code)

    def _advise(self, axes: Mapping[str, str], task: str, *, key: str,
                config: Mapping) -> tuple[tuple[str, float | None] | None, str | None]:
        model = str(self._settings(config).get("model") or DEFAULT_MODEL)
        body, code = self._post_result(build_advice_request(model, axes, task), key, config)
        if code is not None:
            return None, code
        return _parse_advice_response(body)

    def __repr__(self) -> str:
        return "TypeSafeProvider()"
