# 스킬 탐색 흐름 — 설계 단계·점검·기술 결정에서 공개 질의를 만들고, 예산 안에서 seed·설치·검색 결과를 모으며, 저장된 검토를 현재·낡음·미검토·제외로 나눈다.
"""Discovery: needs → public queries → provider outcomes; stored reviews → candidates.

A find need is one (design stage, check) and the technology values its query templates use. Queries come only from
`stages.QUERY_TEMPLATES` filled with enumerated public values. A template that needs an undecided, `none`, `other`
or `other:<id>` value sends nothing: that part of the need is `local-only(public-term-required)` and local evidence
(seeds, installed skills, stored reviews) still answers. At most `max_queries` requests run per call and the rest are
`deferred(budget)`; a fresh cached outcome sends nothing and costs no budget. A lead may add one explicit public query
for a need (PublicQueryChoiceV1): cooperative provenance the lead attests, not a scanner proving publicness. Hits are
search results only; nothing here installs, executes or delivers a skill.
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Callable, Mapping, Sequence

from oah.design import stages
from oah.discovery import providers, store

FIND_VERSION = 1
CHOICES, READS = "choices", "reads"
SOURCE_LIMIT = 300
_PLACEHOLDER = re.compile(r"\{([a-z-]+)\}")
CANDIDATE_STATES = ("current", "stale", "unreviewed-hit", "excluded")


class NeedInvalid(ValueError):
    """OAH_SKILLS_USAGE: a stage, check, technology label or public-query choice outside the closed grammar."""

    code = "OAH_SKILLS_USAGE"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


def parse_tech(pairs: Sequence[str]) -> dict[str, dict]:
    """`LABEL=VALUE` words as a decision view; `undecided` leaves the label out, as an undecided design does."""
    tech: dict[str, dict] = {}
    seen: set[str] = set()
    for pair in pairs:
        label, sep, value = pair.partition("=")
        if not sep or label not in stages.TECH_LABELS:
            raise NeedInvalid(f"--tech {pair!r}: use LABEL=VALUE with a label from {', '.join(stages.TECH_LABELS)}")
        if value != "undecided" and value not in stages.TECH_LABELS[label] and not stages.LOCAL_VALUE.fullmatch(value):
            raise NeedInvalid(f"--tech {pair!r}: {label} is one of {', '.join(stages.TECH_LABELS[label])}, undecided, "
                              "or other:<id> for a choice outside the list")
        if label in seen:
            raise NeedInvalid(f"--tech names {label} twice")
        seen.add(label)
        if value != "undecided":
            tech[label] = {"value": value}
    return tech


def _row(view: Mapping, stage: str) -> Mapping | None:
    return next((row for row in view.get("stages", ()) if isinstance(row, Mapping) and row.get("id") == stage), None)


def _check_status(row: Mapping | None, check: str) -> str | None:
    return next((item.get("status") for item in (row or {}).get("checks", ())
                 if isinstance(item, Mapping) and item.get("id") == check), None)


def _value(view: Mapping, label: str) -> str:
    entry = view.get("tech", {}).get(label)
    value = entry.get("value") if isinstance(entry, Mapping) else None
    return value if isinstance(value, str) else "undecided"


def _public_value(value: str) -> bool:
    return value not in ("undecided", "none", "other") and not stages.LOCAL_VALUE.fullmatch(value)


def needs_for(*, stage: str | None = None, check: str | None = None, tech: Mapping | None = None,
              record: Mapping | None = None) -> list[dict]:
    """Every (stage, check) with a query template that the design (or a technology-only view) still asks about."""
    if stage is not None and stage not in stages.BY_ID:
        raise NeedInvalid(f"--design-stage {stage!r} is one of {', '.join(stages.STAGE_IDS)}")
    if check is not None and (stage is None or check not in stages.all_checks(stage)):
        raise NeedInvalid("--check needs --design-stage and one of that stage's checks")
    view = record if record is not None else {"tech": dict(tech or {}), "stages": []}
    out = []
    for (each_stage, each_check), templates in stages.QUERY_TEMPLATES.items():
        if (stage and each_stage != stage) or (check and each_check != check):
            continue
        row = _row(view, each_stage)
        if (row or {}).get("applicability") == "not-applicable" or _check_status(row, each_check) == "not-applicable":
            continue
        condition = stages.BY_ID[each_stage].conditional.get(each_check)
        if condition is not None and stages.condition_holds(condition, view) is False:
            continue
        labels = sorted({label for template in templates for label in _PLACEHOLDER.findall(template)})
        values = {label: _value(view, label) for label in labels}
        queries, local = [], []
        for template in templates:
            missing = {label: values[label] for label in _PLACEHOLDER.findall(template)
                       if not _public_value(values[label])}
            text = template
            for label in _PLACEHOLDER.findall(template):
                text = text.replace("{" + label + "}", values[label])
            if missing or not providers.is_public(text):
                local.append({"template": template, "reason": "public-term-required", "labels": missing})
            else:
                queries.append({"text": text, "used": {label: values[label] for label in _PLACEHOLDER.findall(template)}})
        need = {"version": FIND_VERSION, "design_stage": each_stage, "check": each_check, "tech": values}
        out.append({**need, "id": store.digest(need), "queries": queries, "local": local})
    return out


def choose_public(state_dir, *, need_id: str, query: str, source: str, needs: Sequence[Mapping],
                  settings: Mapping) -> dict:
    """Store the lead's PublicQueryChoiceV1 for one current need; malformed or unbound choices refuse."""
    if need_id not in {need["id"] for need in needs}:
        raise NeedInvalid(f"--need {need_id} is not one of the needs this command found; run `oah skills find --json` "
                          "and copy a need id")
    if not isinstance(query, str) or not query.strip() or query != query.strip() or len(query) > providers.QUERY_LIMIT \
            or any(ord(char) < 32 for char in query):
        raise NeedInvalid(f"--public-query is one line of at most {providers.QUERY_LIMIT} characters")
    parts = urllib.parse.urlsplit(source or "")
    if parts.scheme != "https" or not parts.hostname or len(source) > SOURCE_LIMIT or "@" in parts.netloc:
        raise NeedInvalid("--public-source is the https address of the public page that uses this exact term")
    record = {"version": 1, "need_id": need_id, "exact_query": query, "public_source": source, "chosen_by": "lead",
              "policy_digest": policy_digest(settings)}
    _word, stored = store.put_record(state_dir, CHOICES, record)
    return stored


def policy_digest(settings: Mapping) -> str:
    return store.digest({"parser": providers.PARSER, "provider": settings.get("provider"),
                         "cache_hours": settings.get("cache_hours"), "max_queries": settings.get("max_queries"),
                         "license_allow": sorted(settings.get("license_allow") or [])})


def _need_outcome(searches: list[dict], need: Mapping, deferred: list[dict]) -> str:
    failures = [item["outcome"] for item in searches if item["outcome"] not in providers.SUCCESS]
    if failures:
        return failures[0]
    if any(item["outcome"] == "ok" for item in searches):
        return "ok"
    if searches:
        return "zero-results"
    if deferred:
        return "deferred"
    return "local-only" if need["local"] else "not-searched"


def find(needs: Sequence[Mapping], *, state_dir, settings: Mapping, now: dt.datetime, provider: str,
         view: Mapping | None, admitted, roots=(), choice: Mapping | None = None,
         notify: Callable[[str], None] | None = None) -> dict:
    """Run the needs' public queries within the admitted budget and gather seeds and installed skills.

    `admitted` is skillpack's Admitted value of a taskless operation (E-DR3-F01): `design-inspect` for a design's
    needs, `public-explore` for a bare search. Its explicit request budget is the only one, every external search
    carries it, and the report records the operation."""
    from oah import skillpack

    if not isinstance(admitted, skillpack.Admitted) or admitted.operation.kind not in skillpack.TASKLESS:
        raise ValueError("discovery.find runs only under a taskless admission (skillpack.inspect or explore)")
    budget, requests = admitted.allows_requests(), 0
    entries = []
    for need in needs:
        searches, deferred = [], []
        wanted = [(item["text"], "template", None) for item in need["queries"]]
        if choice is not None and choice["need_id"] == need["id"]:
            wanted.append((choice["exact_query"], "choice", choice["id"]))
        for text, origin, choice_id in (wanted if provider == "skills-sh" else ()):
            cached = providers.cached(text, state_dir=state_dir, settings=settings, now=now)
            if cached is None and settings["enabled"] and settings["provider"] == "skills-sh" and requests >= budget:
                deferred.append({"query": text, "origin": origin, "outcome": "deferred", "reason": "budget"})
                continue
            result = cached or providers.search(text, state_dir=state_dir, settings=settings, now=now, origin=origin,
                                                choice=choice_id, notify=notify, admitted=admitted,
                                                purpose={"design_stage": need["design_stage"], "check": need["check"]})
            requests += int(result.requested)
            searches.append(result.to_json())
        entries.append({"id": need["id"], "design_stage": need["design_stage"], "check": need["check"],
                        "tech": need["tech"], "queries": need["queries"], "local": need["local"],
                        "searches": searches, "deferred": deferred,
                        "outcome": _need_outcome(searches, need, deferred)})
    stages_seen = list(dict.fromkeys(need["design_stage"] for need in needs))
    seeds = []
    if provider in ("skills-sh", "seed"):
        for stage in stages_seen:
            seeds.extend(item for item in providers.seed_candidates(stage, None, view)
                         if item["seed"] not in {seed["seed"] for seed in seeds})
    installed = []
    if provider in ("skills-sh", "installed"):
        terms = sorted({word for need in needs for item in need["queries"] for word in item["text"].split()}
                       | {value for need in needs for value in need["tech"].values() if _public_value(value)})
        installed = providers.installed_candidates(terms, roots)
    failures = [entry["outcome"] for entry in entries if entry["outcome"] not in
                ("ok", "zero-results", "deferred", "local-only", "not-searched")]
    return {"version": FIND_VERSION, "operation": admitted.operation.to_json(), "provider": provider,
            "budget": budget, "requests": requests, "needs": entries, "seeds": seeds, "installed": installed,
            "outcome": failures[0] if failures else "ok"}


def _purpose_of(query: Mapping | None) -> tuple[str | None, str | None]:
    purpose = (query or {}).get("purpose") or {}
    return purpose.get("design_stage"), purpose.get("check")


def candidates(state_dir, needs: Sequence[Mapping], view: Mapping) -> list[dict]:
    """Stored reviews joined with the current needs, plus unreviewed hits of current queries (reads only)."""
    wanted = {item["text"]: need for need in needs for item in need["queries"]}
    current_needs = {need["id"]: need for need in needs}
    choices = {}
    for record in store.records(state_dir, CHOICES):
        if record.get("need_id") in current_needs:
            choices[record["exact_query"]] = current_needs[record["need_id"]]
    out, reviewed = [], set()
    for review in store.reviews(state_dir):
        candidate = review.get("candidate") or {}
        hit = candidate.get("hit") or {}
        stage = check = None
        if candidate.get("query_id"):
            query = store.get_query(state_dir, candidate["query_id"])
            reviewed.add((candidate["query_id"], hit.get("name"), hit.get("source")))
            text = (query or {}).get("query")
            need = wanted.get(text) or choices.get(text)
            if need is not None:
                state, reason, stage, check = "current", None, need["design_stage"], need["check"]
            else:
                state, reason = "stale", "decision-changed"
                stage, check = _purpose_of(query)
        elif candidate.get("seed") in stages.SEED_BY_ID:
            seed = stages.SEED_BY_ID[candidate["seed"]]
            applies = any((_row(view, each) or {}).get("applicability") != "not-applicable" for each in seed.stages)
            holds = stages.condition_holds(seed.condition, view)
            state, reason = ("current", None) if applies and holds is not False else ("stale", "decision-changed")
            stage = seed.stages[0]
        else:
            state, reason = "current", "explicit"
        if review.get("status") != "reviewed":
            state, reason = "excluded", f"rejected:{review.get('reason')}"
        elif (review.get("license") or {}).get("eligibility") != "eligible":
            state, reason = "excluded", "license"
        source = review.get("source") or {}
        out.append({"state": state, "reason": reason, "review": review["id"],
                    "skill": (review.get("frontmatter") or {}).get("name"), "design_stage": stage, "check": check,
                    "seed": candidate.get("seed"), "locator": source.get("locator"), "commit": source.get("commit"),
                    "path": source.get("path"), "license": (review.get("license") or {}).get("expression"),
                    "eligibility": (review.get("license") or {}).get("eligibility"),
                    "hosts": (review.get("compatibility") or {}).get("hosts")})
    for need in needs:
        for item in need["queries"]:
            query_id = store.query_id(providers.PROVIDER, item["text"], providers.PARSER)
            record = store.get_query(state_dir, query_id)
            if not record or record.get("outcome") != "ok":
                continue
            for number, hit in enumerate(record.get("hits", [])):
                if (query_id, hit.get("name"), hit.get("source")) in reviewed:
                    continue
                out.append({"state": "unreviewed-hit", "reason": None, "hit": f"{query_id}:{number}",
                            "skill": hit.get("name"), "source": hit.get("source"),
                            "design_stage": need["design_stage"], "check": need["check"]})
    return out


LINK_REFRESH_LIMIT = 16  # linked reviewed skills one refresh may check again; the rest are reported deferred
LINK_OUTCOMES = ("unchanged", "changed", "failed", "deferred")


def linked_reviews(record: Mapping, state_dir) -> list[dict]:
    """The coverage links that stand on a stored, matching review, once per review, in coverage order."""
    from oah.design import coverage

    out, seen = [], set()
    for link in coverage.links(record, evidence=store.ReviewEvidence(state_dir)):
        if link["status"] == "linked" and link.get("review") and link["review"] not in seen:
            seen.add(link["review"])
            out.append(link)
    return out


def refresh_links(admitted, record: Mapping, *, state_dir, allow: list[str], now: dt.datetime | None = None,
                  sources=None) -> list[dict]:
    """Check again only the reviewed skills the coverage `record` links (DESIGN §4.5, AC5), in coverage order and
    within the admission's request budget (each check is one collector fetch). An upstream whose pinned commit moved
    is reviewed at its new commit and returned as a proposal to link that review; the link and its pin never change
    here. A failed check is `failed`, never `unchanged`, and checks beyond the budget are `deferred`."""
    from oah import skillpack
    from oah.design import coverage
    from oah.discovery import review as reviews

    if not isinstance(admitted, skillpack.Admitted) or admitted.operation.kind not in ("design-inspect", "dispatch"):
        raise ValueError("refresh_links runs only under skillpack's admission of a design or a task")
    budget, spent, out = min(admitted.allows_requests(), LINK_REFRESH_LIMIT), 0, []
    for link in linked_reviews(record, state_dir):
        old = store.get_review(state_dir, link["review"])
        row = {"stage": link["stage"], "need_id": link.get("need_id"), "skill": link.get("skill"),
               "review": link["review"], "commit": (link.get("pin") or {}).get("commit"), "outcome": "deferred",
               "proposal": None, "detail": None, "environment": False}
        out.append(row)
        if spent >= budget:
            row["detail"] = f"over this refresh's budget of {budget} checks"
            continue
        spent += 1
        try:
            fresh, _word = reviews.review(state_dir, repo=reviews.repo_of(old["source"]["locator"]),
                                          path=old["source"]["path"], allow=allow, candidate=old["candidate"],
                                          now=now, sources=sources, require_fresh=True)
        except reviews.ReviewFailed as exc:
            row.update(outcome="failed", detail=str(exc), environment=exc.environment)
            continue
        if coverage.review_pin(fresh) == link.get("pin"):
            row["outcome"] = "unchanged"
            continue
        row.update(outcome="changed", proposal={
            "review": fresh["id"], "commit": fresh["source"]["commit"], "status": fresh["status"],
            "eligibility": fresh["license"]["eligibility"],
            "action": "read the new review, then link it in the coverage and accept the design to move the pin"})
    return out


def record_read(state_dir, *, review: Mapping, at: str) -> dict:
    """An oah-mediated read of a reviewed body by the lead (data only; it grants no invocation authority)."""
    _word, stored = store.put_record(state_dir, READS, {
        "version": 1, "review": review["id"], "skill_sha256": (review.get("source") or {}).get("skill_sha256"),
        "read_by": "lead", "at": at})
    return stored
