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
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Sequence, TypeVar, cast

from oah.rerank.base import TIERS, Candidate, ProviderError, _raise_if_interrupted, _sanitize_interruption
from oah.secrets import Secret

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
_Failure = str | BaseException | None


def _outcome(action: Callable[..., tuple[_T | None, str | None]],
             *args, **kwargs) -> tuple[_T | None, _Failure]:
    """Finish sensitive work here; never return an exception or its traceback to the caller."""
    try:
        try:
            return action(*args, **kwargs)
        except BaseException as exc:
            try:
                _raise_if_interrupted(exc)
            except BaseException as interruption:
                return None, _sanitize_interruption(interruption)
            if isinstance(exc, BaseExceptionGroup) or not isinstance(exc, Exception):
                # The sanitizer also contains cleanup failures; do not clear its input a second time.
                return None, _sanitize_interruption(exc)
            try:
                if isinstance(exc, ProviderError):
                    return None, exc.code
                return None, "internal"
            finally:
                try:
                    traceback.clear_frames(BaseException.__traceback__.__get__(exc))
                except BaseException:
                    pass  # Discard cleanup failures along with the original private exception.
    finally:
        del action, args, kwargs


def _finish(result: _T | None, code: _Failure) -> _T:
    """Raise only after sensitive helpers returned and their callers dropped every input."""
    try:
        _raise_if_interrupted(code)
        if isinstance(code, BaseException):
            raise code.with_traceback(None) from None
        if code is not None:
            raise ProviderError(code).with_traceback(None) from None
        return cast(_T, result)
    finally:
        del result, code


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
    number = None
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except BaseException as exc:
            _raise_if_interrupted(exc)
            if not isinstance(exc, (OverflowError, TypeError, ValueError)):
                raise
            return None
        return number if math.isfinite(number) else None
    finally:
        del value, number


def _answers(body: object) -> Mapping | None:
    answers = None
    try:
        answers = body.get("answers") if isinstance(body, Mapping) else None
        return answers if isinstance(answers, Mapping) else None
    finally:
        del body, answers


def parse_rerank_response(body: object, count: int) -> list[float]:
    result = code = None
    try:
        try:
            result, code = _outcome(_parse_rerank_response, body, count)
        finally:
            del body, count
        return _finish(result, code)
    finally:
        del result, code


def _parse_rerank_response(body: object, count: int) -> tuple[list[float] | None, str | None]:
    answers = answer = None
    try:
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
    finally:
        del body, count, answers, answer


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
    result = code = None
    try:
        try:
            result, code = _outcome(_parse_advice_response, body)
        finally:
            del body
        return _finish(result, code)
    finally:
        del result, code


def _parse_advice_response(body: object) -> tuple[tuple[str, float | None] | None, str | None]:
    answers = answer = None
    try:
        answers = _answers(body)
        answer = answers.get("tier") if answers is not None else None
        if not isinstance(answer, Mapping) or answer.get("type") != "choice" or answer.get("choice") not in TIERS:
            return None, "bad-response"
        return (answer["choice"], _number(answer.get("confidence"))), None
    finally:
        del body, answers, answer


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the Authorization header to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib signature
        try:
            return None
        finally:
            del self, req, fp, code, msg, headers, newurl


def _check_url(url: str) -> str | None:
    parts = host = None
    try:
        try:
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname
        except BaseException as exc:
            _raise_if_interrupted(exc)
            if not isinstance(exc, ValueError):
                raise
            parts = None
        if parts is None:
            return "insecure-url"
        if parts.scheme == "https" and host:
            return
        if parts.scheme == "http" and host in LOOPBACK_HOSTS:
            return
        return "insecure-url"
    finally:
        del url, parts, host


def post_json(url: str, payload: Mapping, *, key: Secret, timeout: float) -> object:
    """POST payload as JSON and return the parsed reply; any failure raises ProviderError(code)."""
    result = code = None
    try:
        try:
            result, code = _outcome(_post_json, url, payload, key=key, timeout=timeout)
        finally:
            del url, payload, key, timeout
        return _finish(result, code)
    finally:
        del result, code


def _post_json(url: str, payload: Mapping, *, key: Secret, timeout: float) -> tuple[object, str | None]:
    credential = data = raw = request = opener = response = None
    try:
        code = _check_url(url)
        if code is not None:
            return None, code
        # Bearer credentials must be visible ASCII, with no whitespace or control characters.
        # Reject them before http.client can include a rejected header value in an exception.
        if not isinstance(key, Secret):
            return None, "invalid-header"
        credential = key.reveal()
        if not credential or any(not 0x21 <= ord(char) <= 0x7e for char in credential):
            return None, "invalid-header"
        data = None
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except BaseException as exc:
            _raise_if_interrupted(exc)
            if not isinstance(exc, (TypeError, ValueError)):
                raise
        if data is None:
            return None, "bad-request"
        from oah import __version__

        code: str | None = None
        raw = b""
        try:
            request = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {credential}",
                "User-Agent": f"open-agent-harness/{__version__}",
            })
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(RESPONSE_LIMIT + 1)
        except BaseException as exc:
            _raise_if_interrupted(exc)
            if isinstance(exc, urllib.error.HTTPError):
                code = STATUS_CODES.get(exc.code, f"http-{exc.code}")
                exc.close()
            elif isinstance(exc, urllib.error.URLError):
                code = "timeout" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "network"
            elif isinstance(exc, (socket.timeout, TimeoutError)):
                code = "timeout"
            elif isinstance(exc, (OSError, http.client.HTTPException, ValueError, OverflowError)):
                code = "network"
            else:
                raise
        if code is not None:
            return None, code
        if len(raw) > RESPONSE_LIMIT:
            return None, "bad-response"
        try:
            return json.loads(raw.decode("utf-8")), None
        except BaseException as exc:
            _raise_if_interrupted(exc)
            # Includes UnicodeDecodeError, JSONDecodeError and oversized JSON integers.
            if not isinstance(exc, ValueError):
                raise
        return None, "bad-response"
    finally:
        del url, payload, key, timeout, credential, data, raw, request, opener, response


class TypeSafeProvider:
    name = "typesafe"

    @staticmethod
    def _settings(config: Mapping) -> Mapping:
        settings = None
        try:
            settings = config.get("typesafe")
            return settings if isinstance(settings, Mapping) else {}
        finally:
            del config, settings

    def key_name(self, config: Mapping) -> str:
        try:
            return str(self._settings(config).get("key_name") or DEFAULT_KEY_NAME)
        finally:
            del self, config

    def _post(self, payload: dict, key: Secret, config: Mapping) -> object:
        result = code = None
        try:
            try:
                result, code = _outcome(self._post_result, payload, key, config)
            finally:
                del self, payload, key, config
            return _finish(result, code)
        finally:
            del result, code

    def _post_result(self, payload: dict, key: Secret, config: Mapping) -> tuple[object, str | None]:
        settings = timeout = None
        try:
            settings = self._settings(config)
            timeout = _number(config.get("timeout_seconds")) or 10.0
            return _post_json(str(settings.get("url") or DEFAULT_URL), payload, key=key, timeout=timeout)
        finally:
            del self, payload, key, config, settings, timeout

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
              config: Mapping) -> list[float]:
        result = code = None
        try:
            try:
                result, code = _outcome(self._score, kind, query, candidates, key=key, config=config)
            finally:
                del self, kind, query, candidates, key, config
            return _finish(result, code)
        finally:
            del result, code

    def _score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
               config: Mapping) -> tuple[list[float] | None, str | None]:
        model = body = None
        try:
            model = str(self._settings(config).get("model") or DEFAULT_MODEL)
            body, code = self._post_result(build_rerank_request(model, kind, query, candidates), key, config)
            if code is not None:
                return None, code
            return _parse_rerank_response(body, len(candidates))
        finally:
            del self, kind, query, candidates, key, config, model, body

    def advise(self, axes: Mapping[str, str], task: str, *, key: Secret,
               config: Mapping) -> tuple[str, float | None]:
        result = code = None
        try:
            try:
                result, code = _outcome(self._advise, axes, task, key=key, config=config)
            finally:
                del self, axes, task, key, config
            return _finish(result, code)
        finally:
            del result, code

    def _advise(self, axes: Mapping[str, str], task: str, *, key: Secret,
                config: Mapping) -> tuple[tuple[str, float | None] | None, str | None]:
        model = body = None
        try:
            model = str(self._settings(config).get("model") or DEFAULT_MODEL)
            body, code = self._post_result(build_advice_request(model, axes, task), key, config)
            if code is not None:
                return None, code
            return _parse_advice_response(body)
        finally:
            del self, axes, task, key, config, model, body

    def __repr__(self) -> str:
        return "TypeSafeProvider()"
