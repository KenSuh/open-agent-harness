# 스킬 탐색 제공자 — seed 목록·설치된 스킬·skills.sh 검색을 한 경계로 묶고, 공개 용어만 내보내며, 결과를 닫힌 결과값과 캐시로 남긴다.
"""Discovery providers: `seed` (the stage catalog), `installed` (skills on this machine) and `skills-sh`.

The external search is one bounded GET of https://skills.sh/api/search?q=<query> through C's vetted `web.get`
(no proxy, vetted addresses, no redirect, ≤1 MiB, 15 s). Only public terms leave the machine: a template query
whose every word is in `stages.PUBLIC_QUERY_TERMS`, or a lead's explicit PublicQueryChoiceV1. A whole-URL privacy
check runs as an extra refusal. Every outcome is stored; only `ok` and `zero-results` are successes. A hit is a
search result, never a candidate for delivery by itself.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
import sys
import urllib.parse
from typing import Callable, Mapping, Sequence

from oah import skillsel
from oah.design import stages
from oah.design.sources import web
from oah.design.sources.state import StateProblem, loads
from oah.discovery import store

PARSER = "skills-sh-search/v1"
PROVIDER = "skills-sh"
ENDPOINT = "https://skills.sh/api/search?q="
POLICY = web.WebPolicy(max_bytes=1 << 20, timeout=15.0, max_redirects=0, allowed_types=("application/json",),
                       redirects="none")
GETTER: Callable[..., web.WebResult] = web.get
OUTCOMES = ("ok", "zero-results", "timeout", "rate-limited", "http-error", "schema-mismatch", "blocked", "offline",
            "disabled", "unsupported-source")
SUCCESS = ("ok", "zero-results")
PROVIDERS = ("seed", "installed", "skills-sh", "none")
DEFAULTS = skillsel.DEFAULTS["discovery"]
QUERY_LIMIT = 60
HIT_LIMIT = 50
NOTICE = ("oah: skill discovery searches skills.sh (https://skills.sh) with public technology words only, such as "
          "`postgresql schema design`; your task text, design text and file names are never sent. Turn it off "
          "with `[skills.discovery] enabled = false` in .oah/config.toml.")
_EMAIL = re.compile(r"[^\s@/]+@[^\s@/]+\.[A-Za-z]{2,}")
_SECRETISH = re.compile(r"(?:[A-Za-z0-9+/_-]{32,}|[0-9a-f]{24,})")


class DiscoveryFailed(Exception):
    """OAH_SKILL_DISCOVERY_FAILED(<outcome>) — exit 69; the flow continues with seeds, reviews and research."""

    code = "OAH_SKILL_DISCOVERY_FAILED"

    def __init__(self, outcome: str, detail: str) -> None:
        self.outcome = outcome
        super().__init__(f"{self.code}({outcome}): {detail}")


def settings_of(skills_section: Mapping) -> dict:
    given = dict(skills_section.get("discovery") or {})
    merged = dict(DEFAULTS, **given)
    if merged["provider"] not in PROVIDERS:
        raise ValueError(f"[skills.discovery] provider is one of {', '.join(PROVIDERS)}")
    if not isinstance(merged["enabled"], bool):
        raise ValueError("[skills.discovery] enabled is true or false")
    if not isinstance(merged["max_queries"], int) or not 0 <= merged["max_queries"] <= 16:
        raise ValueError("[skills.discovery] max_queries is a whole number from 0 to 16")
    if not isinstance(merged["cache_hours"], (int, float)) or merged["cache_hours"] < 0:
        raise ValueError("[skills.discovery] cache_hours is a number of hours, 0 or more")
    return merged


def is_public(query: str) -> bool:
    """Every word of an automatic query is a catalogued public term (a positive boundary, not a scanner)."""
    words = query.split()
    return bool(words) and all(word in stages.PUBLIC_QUERY_TERMS for word in words) and len(query) <= QUERY_LIMIT


def outgoing_problem(url: str) -> str | None:
    """An extra whole-URL refusal (e-mail addresses, secret-like strings); None when the URL may leave."""
    decoded = urllib.parse.unquote(url)
    if _EMAIL.search(decoded):
        return "an e-mail address"
    query = urllib.parse.urlsplit(decoded).query
    if _SECRETISH.search(query):
        return "a secret-like string"
    return None


def parse(body: bytes) -> tuple[str, list[dict]]:
    """skills-sh-search/v1: an object holding one list of results whose entries carry string name and source."""
    try:
        data = loads(body, "the skills.sh answer")
    except StateProblem:
        return "schema-mismatch", []
    if not isinstance(data, dict):
        return "schema-mismatch", []
    lists = [value for value in data.values() if isinstance(value, list)]
    if len(lists) != 1:
        return "schema-mismatch", []
    entries = lists[0]
    if not entries:
        return "zero-results", []
    hits = []
    for entry in entries[:HIT_LIMIT]:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("source"), str) \
                and entry["name"] and entry["source"] and len(entry["name"]) <= 200 and len(entry["source"]) <= 300:
            hits.append({"name": entry["name"], "source": entry["source"]})
    return ("ok", hits) if hits else ("schema-mismatch", [])


def _failure_outcome(exc: web.WebFailure) -> tuple[str, str]:
    code = exc.code
    if code == "timeout":
        return "timeout", "the search did not answer within 15 seconds"
    if code == "rate-limited":
        return "rate-limited", "skills.sh asked oah to wait" + (f" (retry after {exc.retry_after:.0f})"
                                                                 if isinstance(exc.retry_after, (int, float)) else "")
    if code in ("http-error", "not-found", "gone"):
        return "http-error", f"HTTP {exc.http}"
    if code == "proxy-configured":
        return "blocked", "proxy: a proxy would carry the request, so oah cannot vet the destination"
    if code == "private-destination":
        return "blocked", "address: the host resolves to a private or local address"
    if code in ("insecure-url", "redirect-off-origin", "too-many-redirects", "moved"):
        return "blocked", f"policy: {code}"
    if code in ("oversize", "content-type", "content-encoding", "bad-response"):
        return "schema-mismatch", f"the answer was not a small JSON document ({code})"
    return "offline", f"the search could not be reached ({code})"


@dataclasses.dataclass
class Search:
    """One query sent (or not) to the external provider, and what came back."""

    provider: str
    query: str
    query_id: str
    outcome: str
    detail: str
    hits: list[dict]
    checked_at: str
    cached: bool = False
    requested: bool = False
    origin: str = "template"          # template | choice
    choice: str | None = None

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


def _fresh(record: Mapping, now: dt.datetime, hours: float) -> bool:
    try:
        checked = dt.datetime.fromisoformat(str(record["checked_at"]))
    except (KeyError, ValueError):
        return False
    return record.get("outcome") in SUCCESS and now - checked < dt.timedelta(hours=hours)


def cached(query: str, *, state_dir, settings: Mapping, now: dt.datetime, origin: str = "template",
           choice: str | None = None) -> Search | None:
    """The fresh stored success for this exact query, when the external search is on; it sends nothing."""
    if not settings["enabled"] or settings["provider"] != PROVIDER:
        return None
    query_id = store.query_id(PROVIDER, query, PARSER)
    record = store.get_query(state_dir, query_id)
    if record is None or not _fresh(record, now, float(settings["cache_hours"])):
        return None
    return Search(PROVIDER, query, query_id, record["outcome"], record["detail"], list(record["hits"]),
                  record["checked_at"], cached=True, origin=origin, choice=choice)


def search(query: str, *, state_dir, settings: Mapping, now: dt.datetime, admitted, origin: str = "template",
           choice: str | None = None, notify: Callable[[str], None] | None = None,
           purpose: Mapping | None = None) -> Search:
    """One search through the external provider (or its fresh cache); never raises for a provider failure.

    Only a caller holding skillpack's Admitted value may search: a taskless exploration (E-DR3-F01) or a task's own
    dispatch (DESIGN §4.1), and a request goes out only within that admission's explicit request budget. Any other
    call refuses before the cache, the notice or the transport."""
    from oah import skillpack

    if not isinstance(admitted, skillpack.Admitted):
        raise ValueError("providers.search runs only under skillpack's admission (inspect, explore or a dispatch)")
    query_id = store.query_id(PROVIDER, query, PARSER)
    at = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    if not settings["enabled"] or settings["provider"] != PROVIDER:
        return Search(PROVIDER, query, query_id, "disabled", "external skill search is turned off", [], at,
                      origin=origin, choice=choice)
    if origin == "template" and not is_public(query):
        return Search(PROVIDER, query, query_id, "blocked", "policy: the query holds a word that is not a catalogued "
                      "public term", [], at, origin=origin, choice=choice)
    fresh = cached(query, state_dir=state_dir, settings=settings, now=now, origin=origin, choice=choice)
    if fresh is not None:
        return fresh
    url = ENDPOINT + urllib.parse.quote(query, safe="")
    problem = outgoing_problem(url)
    if problem is not None:
        return Search(PROVIDER, query, query_id, "blocked", f"policy: the request would carry {problem}", [], at,
                      origin=origin, choice=choice)
    if admitted.allows_requests() < 1:
        raise ValueError("this operation was admitted with no request budget; nothing is sent")
    if not store.notice_shown(state_dir):
        (notify or (lambda text: print(text, file=sys.stderr)))(NOTICE)
        store.record_notice(state_dir, PROVIDER, at)
    try:
        result = GETTER(url, policy=POLICY, headers={"Accept": "application/json"})
        outcome, hits = parse(result.body)
        detail = f"{len(hits)} result(s)" if outcome == "ok" else ("no results" if outcome == "zero-results"
                                                                   else "the answer did not match " + PARSER)
    except web.WebFailure as exc:
        (outcome, detail), hits = _failure_outcome(exc), []
    found = Search(PROVIDER, query, query_id, outcome, detail, hits, at, requested=True, origin=origin, choice=choice)
    store.put_query(state_dir, {"version": 1, "query_id": query_id, "provider": PROVIDER, "query": query,
                                "parser": PARSER, "checked_at": at, "outcome": outcome, "detail": detail,
                                "count": len(hits), "hits": hits, "origin": origin, "choice": choice,
                                "purpose": dict(purpose or {})})
    return found


def seed_candidates(stage: str | None, check: str | None, record: Mapping | None) -> list[dict]:
    """The catalog seeds of a design stage, with whether their condition holds for the recorded decisions."""
    out = []
    for seed in stages.SEED_SKILLS:
        if stage is not None and stage not in seed.stages:
            continue
        holds, why = (stages.explain_condition(seed.condition, record) if record is not None else (None, "no design"))
        out.append({"provider": "seed", "seed": seed.id, "skill": seed.skill, "mode": seed.mode,
                    "condition": seed.condition, "holds": holds, "why": why, "repo": seed.repo,
                    "research_pin": seed.commit, "path": seed.path})
    return out


def installed_candidates(terms: Sequence[str], roots) -> list[dict]:
    """Installed skills whose name or description mentions one of the terms (read-only)."""
    words = {term.lower() for term in terms if len(term) > 2 and term.lower() not in ("design", "patterns", "skill",
                                                                                         "skills", "best", "practices")}
    if not words:
        return []
    out = []
    for skill in skillsel.installed(roots).skills.values():
        text = f"{skill.name} {skill.description}".lower()
        if any(word in text for word in words):
            out.append({"provider": "installed", "skill": skill.name, "origin": skill.origin, "label": skill.label,
                        "digest16": skill.digest})
    return out
