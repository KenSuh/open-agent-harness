# 선택적 AI 재순위 인터페이스 — 사용자 동의와 키가 모두 있을 때만 제공자를 부르고, 순서만 바꾸며 결과마다 used/unused(사유)를 남긴다.
"""Optional AI re-ranking (A2, A9, A10).

    section = load_config(root).section("rerank", rerank.DEFAULTS)
    result = rerank.rerank("impact", candidates, config=section, query=question)
    result.status   # "used" or "unused(<reason>)"

A provider runs only when every gate passes, checked in this order:
1. `unused(disabled-by-brief)`: the caller passed `brief_disabled=True` (`--no-rerank`, or the packet);
2. `unused(not-enabled)`: `[rerank] enabled` is not `true` (the default is off);
3. `unused(not-configured)`: `[rerank] provider` is empty; `unused(unknown-provider)` names none known;
4. `unused(no key)`: `oah.secrets.get(<the provider's key name>)` found nothing.
A key alone never enables a provider. A provider failure reports `unused(error:<code>)`.

A provider only scores or advises. `rerank` builds the order itself, so the result is always a
permutation of the input: no row is dropped, added or duplicated. Rows beyond `max_candidates` are
not sent and keep their place after the re-ordered ones.

Difficulty: `advise_difficulty(axes, work, ...)` asks the provider which tier a task needs, from the four axes
and a `Work` text: the five parts of the task's accepted Intent (`intent_work`) or the user's request before an
Intent exists (`request_work`), then the Scope paths, cut to `WORK_LIMIT` characters from the end. Never source
code, diffs, file contents or stored values. After the gate, empty text reports `unused(no-text)`, and text a
privacy-scanner content rule flags reports `unused(secret)` with `Advice.note` naming the part in plain words,
never the match; either way nothing is sent. The first request of a project prints `SCOPE_NOTICE` once, through
a marker file in the state folder. `Advice` holds the answer as given (`Advice.tier` is still the rule tier it
was asked about); `decide(advice, floor_tier=..., threshold=...)` turns it into the tier to run: Jev's tier at or
above `[rerank] difficulty_min_confidence` (`min_confidence`), raised to the floor tier when lower, else the rule
tier with the reason.
For `oah call`, `advise_difficulty(axes, work, root=root)` accepts `routing.Axes` and loads the launch
target's re-ranker config; a disabled re-ranker reports `unused(not-configured)` at this seam.

`judge_model(request, candidates, config=...)` asks the provider for one (model, effort) pair from a
closed candidate list (amendment 6). The provider receives only the stage, the tier, the four axes and
the candidate pairs. After the same gate it reports `unused(no-candidates)`, `unused(one-candidate)` or
`unused(too-many-candidates)` (more than `max_candidates`) without a request. The answer must be a
tuple of two strings, else `unused(error:bad-response)`; whether it is one of the candidates is
`oah.models`' rule, which the caller applies.
"""
from __future__ import annotations

import math
import pathlib
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from oah import routing, secrets
from oah.config import ConfigError, load_config
from oah.intent import END as INTENT_END
from oah.rerank.base import (TIERS, Candidate, Provider, ProviderError, _INTERRUPTIONS,
                             _classify_failure, _raise_if_interrupted)
from oah.rerank.typesafe import DEFAULT_KEY_NAME, DEFAULT_MODEL, DEFAULT_URL, WORK_LIMIT, TypeSafeProvider
from oah.toolkit.checks import CONTENT_RULES

__all__ = ["DEFAULTS", "PROVIDERS", "TIERS", "WORK_LIMIT", "Advice", "Candidate", "Decision", "ModelJudgement",
           "Provider", "ProviderError", "RerankResult", "Work", "advise_difficulty", "decide", "intent_work",
           "judge_model", "min_confidence", "request_work", "rerank", "scope_work"]

DEFAULTS = {
    "enabled": False,
    "provider": "",
    "timeout_seconds": 10.0,
    "max_candidates": 50,
    "difficulty_min_confidence": 0.6,
    "typesafe": {
        "url": DEFAULT_URL,
        "model": DEFAULT_MODEL,
        "key_name": DEFAULT_KEY_NAME,
    },
}

PROVIDERS: dict[str, Provider] = {"typesafe": TypeSafeProvider()}

USED = "used"
INTENT_PARTS = ("Problem", "Desired outcome", "Success conditions", "Constraints", "Non-goals")
INTENT_TEXT, SCOPE_PATHS, REQUEST_TEXT = "the Intent text", "the Scope paths", "the request text"
SCOPE_WIDENED = "OAH_RERANK_SCOPE_WIDENED"
SCOPE_NOTICE = (f"{SCOPE_WIDENED}: Jev now receives this task's Intent text and Scope paths (up to 16,000 "
                "characters) to judge its difficulty; set enabled = false under [rerank] to stop it.")
NOTICE_MARKER = "rerank-scope-notice"  # in the state folder; present once the notice was shown and a request sent


def unused(reason: str) -> str:
    return f"unused({reason})"


@dataclass(frozen=True)
class Work:
    """The text Jev judges difficulty from: the user's own Intent or request, then Scope paths, nothing else.

    `secret` names, in plain words, the part a privacy-scanner content rule flagged; the match is never kept."""

    text: str
    truncated: bool = False
    secret: str | None = None


def _flagged(text: str) -> bool:
    # The scanner's content rules only: an Intent may quote commit SHAs, so no entropy heuristic.
    return any(pattern.search(text) for _rule, pattern in CONTENT_RULES)


def _work(parts: Sequence[tuple[str, str, Sequence[str]]]) -> Work:
    """(part name, heading, lines) in order; the text is cut from its end, so the last part goes first."""
    if not any(body for _name, _heading, body in parts):
        return Work("")  # headings alone describe no work
    lines: list[str] = []
    secret = None
    for name, heading, body in parts:
        if secret is None and _flagged("\n".join(body)):
            secret = name
        lines.append(f"### {heading}")
        lines.extend(body)
    text = "\n".join(lines)
    return Work(text[:WORK_LIMIT], len(text) > WORK_LIMIT, secret)


def _body(lines: Sequence[str]) -> list[str]:
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return body


def _scope(scope: Sequence[str]) -> list[tuple[str, str, Sequence[str]]]:
    paths = [str(path) for path in scope if str(path).strip()]
    return [(SCOPE_PATHS, "Scope", paths)] if paths else []


def intent_work(section: str, scope: Sequence[str]) -> Work:
    """The five Intent parts, in order with their headings, then the Scope paths one per line.

    `section` is the accepted Intent section; its fields, Open questions and markers are left out."""
    found: dict[str, list[str]] = {}
    current = None
    for line in section.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("### "):
            current = line[4:].strip()
            found.setdefault(current, [])
        elif line.rstrip() == INTENT_END or line.startswith(("# ", "## ")):
            current = None  # a comment inside a part is part of its text; only the END marker ends the section
        elif current is not None:
            found[current].append(line)
    parts = [(INTENT_TEXT, heading, _body(found.get(heading, ()))) for heading in INTENT_PARTS]
    return _work([*parts, *_scope(scope)])


def request_work(text: str, scope: Sequence[str]) -> Work:
    """The user's request as written, before an Intent exists, then the Scope paths one per line."""
    body = _body(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return _work([(REQUEST_TEXT, "Request", body), *_scope(scope)])


def scope_work(scope: Sequence[str]) -> Work:
    """The Scope paths alone, one per line: what a new packet has before its Intent is written."""
    return _work(_scope(scope))


def _plain(text: str) -> Work:
    """A plain string, sent as written under the same limit and checks."""
    if not text.strip():
        return Work("")
    return Work(text[:WORK_LIMIT], len(text) > WORK_LIMIT, REQUEST_TEXT if _flagged(text) else None)


def _notice_shown(state_dir: pathlib.Path) -> bool:
    """A missing or unreadable marker means "not yet shown", never an error."""
    try:
        return (state_dir / NOTICE_MARKER).read_text(encoding="utf-8").startswith(SCOPE_WIDENED)
    except (OSError, ValueError):
        return False


def _record_notice(state_dir: pathlib.Path) -> None:
    from oah.atomic import write_text_atomic

    try:
        write_text_atomic(state_dir / NOTICE_MARKER, f"{SCOPE_WIDENED} shown\n", retry_seconds=0)
    except OSError:
        pass  # an unwritable state folder never blocks a call; the notice shows again next time


def _print_notice(text: str) -> None:
    try:
        print(text, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def min_confidence(config: Mapping) -> float:
    """`[rerank] difficulty_min_confidence`: Jev's tier counts only at or above it."""
    value = config.get("difficulty_min_confidence", DEFAULTS["difficulty_min_confidence"])
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise ConfigError("OAH_CONFIG_INVALID", f".oah/config.toml: rerank.difficulty_min_confidence = {value!r} must be "
                          "a number from 0 to 1, such as 0.6; change it, or remove it to use the default 0.6")
    return float(value)


@dataclass(frozen=True)
class Decision:
    """The tier to run after Jev's judgement: `used` when Jev's tier counts (`tier_source=jev`)."""

    tier: str
    used: bool
    status: str  # "used", "unused(<reason>)" or "below-threshold <confidence>"
    raised: bool = False  # Jev's tier was below the floor tier and was raised to it


def decide(advice: "Advice", *, floor_tier: str, threshold: float) -> Decision:
    """Jev's tier at or above the threshold, never below the floor tier; otherwise the rule tier and why."""
    if advice.status != USED or advice.advisory_tier not in TIERS:
        return Decision(advice.rule_tier, False, advice.status)
    if advice.confidence is None or advice.confidence < threshold:
        shown = "none" if advice.confidence is None else format(advice.confidence, ".3g")
        return Decision(advice.rule_tier, False, f"below-threshold {shown}")
    raised = TIERS.index(advice.advisory_tier) < TIERS.index(floor_tier)
    return Decision(floor_tier if raised else advice.advisory_tier, True, USED, raised)


@dataclass(frozen=True)
class RerankResult:
    order: tuple[Candidate, ...]
    status: str
    provider: str | None = None
    scores: tuple[tuple[str, float], ...] = ()

    @property
    def used(self) -> bool:
        return self.status == USED

    def to_record(self) -> dict:
        return {"rerank": self.status, "provider": self.provider,
                "order": [item.name for item in self.order], "scores": dict(self.scores)}


@dataclass(frozen=True)
class Advice:
    rule_tier: str
    advisory_tier: str | None
    confidence: float | None
    status: str
    provider: str | None = None
    truncated: bool = False  # the work text was cut to WORK_LIMIT characters
    note: str | None = None  # for unused(secret): the part that held it, in plain words

    @property
    def tier(self) -> str:
        """The rule tier this advice was asked about; `decide` turns the advice into the tier to run."""
        return self.rule_tier

    def to_record(self) -> dict:
        return {"tier": self.rule_tier, "tier_source": "rules", "advice": self.status,
                "provider": self.provider, "advisory_tier": self.advisory_tier,
                "advisory_confidence": self.confidence}


@dataclass(frozen=True)
class ModelJudgement:
    status: str
    proposal: tuple[str, str] | None = None
    provider: str | None = None
    confidence: float | None = None  # the judge's confidence in its proposal, when its answer carried one


AXES = ("kind", "intensity", "reasoning", "risk")


def _gate(config: Mapping, *, brief_disabled: bool, env: Mapping[str, str] | None,
          keychain: secrets.Keychain | None,
          providers: Mapping[str, Provider] | None) -> tuple[Provider | None, secrets.Secret | None, str]:
    """(provider, key, status): provider and key are set only when every gate passed."""
    provider = key = key_name = None
    try:
        if brief_disabled:
            return None, None, unused("disabled-by-brief")
        if config.get("enabled") is not True:
            return None, None, unused("not-enabled")
        name = config.get("provider")
        if not isinstance(name, str) or not name.strip():
            return None, None, unused("not-configured")
        provider = (PROVIDERS if providers is None else providers).get(name.strip())
        if provider is None:
            return None, None, unused("unknown-provider")
        key_name, status = _call(lambda: _valid_text(provider.key_name(config)))
        if status != USED:
            return None, None, status
        try:
            key = secrets.get(key_name, env=env, keychain=keychain)
        except BaseException as exc:
            _raise_if_interrupted(exc)
            if not isinstance(exc, ValueError):
                raise
            return None, None, unused("error:key-name")
        if not key:
            return None, None, unused("no key")
        return provider, key, USED
    finally:
        del config, brief_disabled, env, keychain, providers, provider, key, key_name


def _call(action: Callable[[], object]) -> tuple[object, str]:
    """Run a provider call and validate its result; consume only classified failure codes."""
    failure = None
    try:
        try:
            return action(), USED
        except BaseException as exc:
            failure = _classify_failure(exc)
        if type(failure) is str:
            return None, unused(f"error:{failure}")
        raise BaseException.with_traceback(failure, None) from None
    finally:
        del action, failure


def _valid_text(value: object) -> str:
    try:
        if type(value) is not str:
            raise ProviderError("bad-response")
        return value
    finally:
        del value


def _valid_scores(scores: object, count: int) -> list[float]:
    if (type(scores) is not list and type(scores) is not tuple) or len(scores) != count:
        raise ProviderError("bad-response")
    result = []
    for value in scores:
        if type(value) is not int and type(value) is not float:
            raise ProviderError("bad-response")
        number = None
        try:
            number = float(value)
        except OverflowError:
            pass  # Only exact built-in numbers reach conversion; no provider hook can run.
        if number is None or not math.isfinite(number):
            raise ProviderError("bad-response")
        result.append(number)
    return result


def _valid_advice(answer: object) -> tuple[str, float | None]:
    if type(answer) is not tuple or len(answer) != 2:
        raise ProviderError("bad-response")
    tier, confidence = answer
    if type(tier) is not str or tier not in TIERS:
        raise ProviderError("bad-response")
    if confidence is not None:
        confidence = _valid_scores([confidence], 1)[0]
    return tier, confidence


def _valid_pair(answer: object) -> tuple[str, str, float | None]:
    """A (model, effort) proposal, optionally followed by a confidence from 0 to 1 or None."""
    confidence = None
    try:
        if (type(answer) is not tuple or len(answer) not in (2, 3)
                or any(type(item) is not str for item in answer[:2])):
            raise ProviderError("bad-response")
        if len(answer) == 3 and answer[2] is not None:
            confidence = _valid_scores([answer[2]], 1)[0]
            if not 0.0 <= confidence <= 1.0:
                raise ProviderError("bad-response")
        return answer[0], answer[1], confidence
    finally:
        del answer, confidence


def rerank(kind: str, candidates: Sequence[Candidate], *, config: Mapping, query: str = "",
           brief_disabled: bool = False, env: Mapping[str, str] | None = None,
           keychain: secrets.Keychain | None = None,
           providers: Mapping[str, Provider] | None = None) -> RerankResult:
    """Re-order candidates when the user enabled a provider and its key exists; else keep the order."""
    provider = key = failure = None
    try:
        items = tuple(candidates)
        if len({item.name for item in items}) != len(items):
            raise ValueError("rerank candidates need unique names")
        provider, key, status = _gate(config, brief_disabled=brief_disabled, env=env, keychain=keychain,
                                      providers=providers)
        env = keychain = providers = None
        if provider is None:
            return RerankResult(items, status)
        provider_name, status = _call(lambda: _valid_text(provider.name))
        if status != USED:
            return RerankResult(items, status)
        if not items:
            return RerankResult(items, unused("no-candidates"), provider_name)
        limit = config.get("max_candidates")
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else len(items)
        sent, rest = items[:limit], items[limit:]
        scores, status = _call(lambda: _valid_scores(
            provider.score(kind, query, sent, key=key, config=config), len(sent)))
        if status != USED:
            return RerankResult(items, status, provider_name)
        ranked = sorted(range(len(sent)), key=lambda index: -scores[index])
        order = tuple(sent[index] for index in ranked) + rest
        return RerankResult(order, USED, provider_name,
                            tuple((item.name, score) for item, score in zip(sent, scores)))
    except _INTERRUPTIONS + (BaseExceptionGroup,) as exc:
        failure = _classify_failure(exc)
    finally:
        del kind, candidates, config, query, brief_disabled, env, keychain, providers, provider, key
    if type(failure) is str:
        raise ProviderError(failure) from None
    raise BaseException.with_traceback(failure, None) from None


def advise_difficulty(axes: Mapping[str, str] | routing.Axes, work: Work | str, *, config: Mapping | None = None,
                      root: pathlib.Path | None = None, brief_disabled: bool = False,
                      env: Mapping[str, str] | None = None, keychain: secrets.Keychain | None = None,
                      providers: Mapping[str, Provider] | None = None, state_dir: pathlib.Path | None = None,
                      notify: Callable[[str], None] | None = None, record_notice: bool = True) -> Advice:
    """Jev's tier for the work, beside the rule tier from routing.Axes or axes["tier"]; `decide` applies it.

    Omitted config loads root's [rerank] section for `oah call`; without root it uses disabled defaults. A plain
    string is sent as written, under the same limit and checks. With `state_dir`, the first request of the project
    prints `SCOPE_NOTICE` through `notify` (stderr by default) and, unless `record_notice` is false, records that it
    was shown.
    """
    provider = key = answer = confidence = failure = None
    try:
        if isinstance(axes, routing.Axes):
            axes = dict(axes.to_json(), tier=routing.recommend(axes).tier)
        rule_tier = axes.get("tier")
        if rule_tier not in TIERS:
            raise ValueError(f"advise_difficulty needs axes['tier'] set to one of {', '.join(TIERS)}")
        work = work if isinstance(work, Work) else _plain(str(work))
        implicit_config = config is None
        if implicit_config:
            config = load_config(root).section("rerank", DEFAULTS) if root is not None else DEFAULTS
        provider, key, status = _gate(config, brief_disabled=brief_disabled, env=env, keychain=keychain,
                                      providers=providers)
        env = keychain = providers = None
        if provider is None:
            if implicit_config and status == unused("not-enabled"):
                status = unused("not-configured")
            return Advice(rule_tier, None, None, status, truncated=work.truncated)
        provider_name, status = _call(lambda: _valid_text(provider.name))
        if status != USED:
            return Advice(rule_tier, None, None, status, truncated=work.truncated)
        if not work.text.strip():
            return Advice(rule_tier, None, None, unused("no-text"), provider_name)
        if work.secret is not None:
            return Advice(rule_tier, None, None, unused("secret"), provider_name, work.truncated, work.secret)
        shown = state_dir is None or _notice_shown(pathlib.Path(state_dir))
        if not shown:
            (notify or _print_notice)(SCOPE_NOTICE)
        try:
            answer, status = _call(lambda: _valid_advice(provider.advise(axes, work.text, key=key, config=config)))
        finally:
            if not shown and record_notice:
                _record_notice(pathlib.Path(state_dir))
        if status != USED:
            return Advice(rule_tier, None, None, status, provider_name, work.truncated)
        tier, confidence = answer
        return Advice(rule_tier, tier, confidence, USED, provider_name, work.truncated)
    except _INTERRUPTIONS + (BaseExceptionGroup,) as exc:
        failure = _classify_failure(exc)
    finally:
        del axes, work, config, root, brief_disabled, env, keychain, providers, provider, key, answer, confidence
    if type(failure) is str:
        raise ProviderError(failure) from None
    raise BaseException.with_traceback(failure, None) from None


def judge_model(request: Mapping[str, object], candidates: Sequence[tuple[str, str]], *, config: Mapping,
                brief_disabled: bool = False, env: Mapping[str, str] | None = None,
                keychain: secrets.Keychain | None = None,
                providers: Mapping[str, Provider] | None = None) -> ModelJudgement:
    """One (model, effort) proposal from the provider, for the caller to validate against its rules."""
    provider = key = answer = failure = None
    try:
        axes = request.get("axes") if isinstance(request.get("axes"), Mapping) else {}
        sent = {"stage": str(request.get("stage", "")), "tier": str(request.get("tier", "")),
                "axes": {name: str(axes[name]) for name in AXES if name in axes}}
        pairs = tuple((str(model), str(effort)) for model, effort in candidates)
        provider, key, status = _gate(config, brief_disabled=brief_disabled, env=env, keychain=keychain,
                                      providers=providers)
        env = keychain = providers = None
        if provider is None:
            return ModelJudgement(status)
        provider_name, status = _call(lambda: _valid_text(provider.name))
        if status != USED:
            return ModelJudgement(status)
        limit = config.get("max_candidates")
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else len(pairs)
        if not pairs:
            return ModelJudgement(unused("no-candidates"), None, provider_name)
        if len(pairs) == 1:
            return ModelJudgement(unused("one-candidate"), None, provider_name)
        if len(pairs) > limit:
            return ModelJudgement(unused("too-many-candidates"), None, provider_name)
        answer, status = _call(lambda: _valid_pair(provider.choose_model(sent, pairs, key=key, config=config)))
        if status != USED:
            return ModelJudgement(status, None, provider_name)
        return ModelJudgement(USED, answer[:2], provider_name, answer[2])
    except _INTERRUPTIONS + (BaseExceptionGroup,) as exc:
        failure = _classify_failure(exc)
    finally:
        del request, candidates, config, brief_disabled, env, keychain, providers, provider, key, answer
    if type(failure) is str:
        raise ProviderError(failure) from None
    raise BaseException.with_traceback(failure, None) from None
