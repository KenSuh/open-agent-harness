# 설계 단계 목록 — 아홉 설계 단계와 필수 확인 항목, 하나의 영향 그래프, 기술 라벨, 단계별 전문 스킬 seed, 닫힌 조건 문법을 담는다.
"""The nine design stages of `oah-design-stages/v1` and their pure helpers.

    stages.STAGES                    # catalog order: purpose ... confirm, with checks and prerequisites
    stages.reach_into("data")        # every stage whose change can reach data, data included (one graph)
    stages.condition_holds("tech:database=postgresql", record)   # True | False | None while undecided

The influence graph is one graph: the scheduling prerequisites, the additional invalidation edges and every
non-confirm stage -> confirm. Re-check after a changed decision is derived from it alone; there is no second table.
Seeds are hints with a research pin; oah has not verified them, and they never become requirements by themselves.
Nothing here reads files, runs programs or calls a model.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Mapping

CATALOG = "oah-design-stages/v1"


@dataclasses.dataclass(frozen=True)
class Stage:
    id: str
    names: Mapping[str, str]          # en, ko
    prerequisites: tuple[str, ...]    # the scheduling order the frontier uses ("asked after")
    may_be_na: bool                   # only access, server and payment
    checks: tuple[str, ...]           # required whenever the stage applies
    conditional: Mapping[str, str]    # check -> the condition under which it is required


STAGES: tuple[Stage, ...] = (
    Stage("purpose", {"en": "Purpose and scope", "ko": "목적·범위"}, (), False,
          ("users", "outcome", "first-scope", "terms", "open-decisions"), {}),
    Stage("behavior", {"en": "Behavior", "ko": "행동"}, ("purpose",), False,
          ("normal-flow", "failure-flow", "cancel-flow", "resume-flow", "user-result"), {}),
    Stage("data", {"en": "Data", "ko": "데이터"}, ("behavior",), False,
          ("entities", "ownership", "lifetime", "integrity"), {}),
    Stage("access", {"en": "Access rules", "ko": "권한"}, ("data",), True,
          ("subjects", "rules", "cross-owner-counterexample", "enforcement-point"), {}),
    Stage("server", {"en": "Server and external work", "ko": "서버·외부 작업"}, ("behavior", "data", "access"), True,
          ("responsibilities", "api-contract", "secrets", "external-effects"), {}),
    Stage("payment", {"en": "Payment (optional)", "ko": "결제(선택)"}, ("purpose", "behavior", "data", "access"), True,
          ("state-transitions", "failure-refund-cancel", "duplicate-out-of-order-missing", "server-confirmation",
           "reconciliation-owner"), {}),
    Stage("quality", {"en": "Quality and operations", "ko": "품질·운영"}, ("behavior",), False,
          ("platforms", "cost-performance", "observability", "deploy-rollback", "recovery-scenario"),
          {"accessibility": "tech:ui=web,mobile,desktop"}),
    Stage("materials", {"en": "Materials and languages", "ko": "재료·언어"},
          ("data", "access", "server", "payment", "quality"), False,
          ("stack-options", "sources-licenses", "adoption", "unverified", "first-scope-done", "module-boundaries"), {}),
    Stage("confirm", {"en": "Design confirmation", "ko": "설계 확인"},
          ("purpose", "behavior", "data", "access", "server", "payment", "quality", "materials"), False,
          ("intent-alignment", "first-implementation-scope", "completion-criteria", "packet-ready"), {}),
)
STAGE_IDS = tuple(stage.id for stage in STAGES)
BY_ID: Mapping[str, Stage] = {stage.id: stage for stage in STAGES}

# DESIGN §3.4: X -> the stages a semantic edit to X additionally affects.
EXTRA_EDGES: Mapping[str, tuple[str, ...]] = {
    "purpose": ("data", "access", "server", "payment", "quality", "materials"),
    "behavior": ("access", "server", "quality"),
    "data": ("access", "server", "quality", "materials"),
    "access": ("data", "server", "quality", "materials"),
    "server": ("payment", "quality", "materials"),
    "payment": ("data", "access", "server", "quality", "materials"),
    "quality": ("materials",),
    "materials": ("data", "access", "server", "payment", "quality"),
    "confirm": (),
}

TECH_LABELS: Mapping[str, tuple[str, ...]] = {
    "ui": ("web", "mobile", "desktop", "cli", "none"),
    "database": ("postgresql", "mysql", "sqlite", "mongodb", "none"),
    "platform": ("supabase", "firebase", "none"),
    "auth": ("password", "oauth", "passkey", "magic-link", "none"),
    "payment-provider": ("stripe", "toss", "paddle", "none"),
    "hosting": ("static", "server", "serverless", "local", "none"),
    "durable-workflow": ("temporal", "none"),
    "runtime": ("python", "node", "rust", "go", "java", "dotnet", "other"),
}
# The stages whose semantic input holds a tech label (its attachment roots).
TECH_ROOTS: Mapping[str, tuple[str, ...]] = {
    "database": ("data", "access", "server", "quality"),
    "auth": ("access", "server"),
    "payment-provider": ("payment", "server", "data"),
    "hosting": ("server", "quality"),
    "ui": ("behavior", "quality"),
    "platform": ("data", "access", "server"),
    "runtime": ("materials",),
    "durable-workflow": ("server", "quality"),
}
LOCAL_VALUE = re.compile(r"other:[a-z0-9]+(?:-[a-z0-9]+)*")  # local-only; never part of a public query
FIXED_RESOLVE_BY = ("before-implementation", "before-release", "after-first-release")


def _edges() -> dict[str, frozenset[str]]:
    edges: dict[str, set[str]] = {stage: set() for stage in STAGE_IDS}
    for stage in STAGES:
        for before in stage.prerequisites:
            edges[before].add(stage.id)
    for source, targets in EXTRA_EDGES.items():
        edges[source].update(targets)
    for stage in STAGE_IDS:
        if stage != "confirm":
            edges[stage].add("confirm")
    return {stage: frozenset(targets) for stage, targets in edges.items()}


INFLUENCE: Mapping[str, frozenset[str]] = _edges()


def dependents(stage: str) -> tuple[str, ...]:
    """Every stage reachable from `stage` (itself excluded unless a cycle leads back), in catalog order."""
    seen: set[str] = set()
    todo = list(INFLUENCE[stage])
    while todo:
        item = todo.pop()
        if item not in seen:
            seen.add(item)
            todo.extend(INFLUENCE[item])
    return tuple(item for item in STAGE_IDS if item in seen)


def reach_into(stage: str) -> tuple[str, ...]:
    """`stage` and every stage that can reach it; its confirmation basis hashes exactly these, sorted by catalog."""
    if stage not in BY_ID:
        raise ValueError(f"unknown design stage {stage!r}")
    seen = {stage}
    todo = [stage]
    while todo:
        item = todo.pop()
        for source, targets in INFLUENCE.items():
            if item in targets and source not in seen:
                seen.add(source)
                todo.append(source)
    return tuple(sorted(seen, key=lambda name: (name != stage, STAGE_IDS.index(name))))


def tech_labels_of(stage: str) -> tuple[str, ...]:
    return tuple(label for label, roots in TECH_ROOTS.items() if stage in roots)


@dataclasses.dataclass(frozen=True)
class Seed:
    id: str
    skill: str
    repo: str          # owner/name on GitHub
    commit: str        # the research pin (STAGE-SKILLS, checked 2026-11-03 by the package research, not by oah)
    path: str          # the skill folder
    stages: tuple[str, ...]
    condition: str
    mode: str          # interview | reference | optional-reference | conditional | user-only


_MATT = ("mattpocock/skills", "4ae2317ddb22b046e26455e4f9c9b3aaaf62212d")
_OBRA = ("obra/superpowers", "2ce8aa15d05bd57213e6f746f953aafb7ded7a8b")
_SUPABASE = ("supabase/agent-skills", "c9be0e931b7930f7d02126d04774d904c381e7d7")
_WSH = ("wshobson/agents", "7e2fb559cf4b824b27f1fec49fe88f7511066d26")
_STRIPE = ("stripe/ai", "5091f72943e6cb4ca439c98ee4783d6085de1318")

SEED_SKILLS: tuple[Seed, ...] = (
    Seed("M1", "grilling", *_MATT, "skills/productivity/grilling", ("purpose", "behavior"), "always", "interview"),
    Seed("M2", "domain-modeling", *_MATT, "skills/engineering/domain-modeling", ("purpose", "behavior", "data"),
         "always", "reference"),
    Seed("O1", "brainstorming", *_OBRA, "skills/brainstorming", ("behavior",), "always", "optional-reference"),
    Seed("S1", "supabase-postgres-best-practices", *_SUPABASE, "skills/supabase-postgres-best-practices", ("data",),
         "tech:database=postgresql", "conditional"),
    Seed("S2", "supabase", *_SUPABASE, "skills/supabase", ("access",), "tech:platform=supabase", "conditional"),
    Seed("W1", "auth-implementation-patterns", *_WSH,
         "plugins/developer-essentials/skills/auth-implementation-patterns", ("access",), "applies:access",
         "conditional"),
    Seed("W2", "api-design-principles", *_WSH, "plugins/backend-development/skills/api-design-principles",
         ("server",), "applies:server", "conditional"),
    Seed("W3", "secrets-management", *_WSH, "plugins/cicd-automation/skills/secrets-management", ("server",),
         "check:server.secrets!=not-applicable", "conditional"),
    Seed("W4", "workflow-orchestration-patterns", *_WSH,
         "plugins/backend-development/skills/workflow-orchestration-patterns", ("server",),
         "tech:durable-workflow=temporal", "reference"),
    Seed("P1", "stripe-best-practices", *_STRIPE, "skills/stripe-best-practices", ("payment",),
         "applies:payment AND tech:payment-provider=stripe", "conditional"),
    Seed("W5", "wcag-audit-patterns", *_WSH, "plugins/accessibility-compliance/skills/wcag-audit-patterns",
         ("quality",), "tech:ui=web,mobile,desktop", "conditional"),
    Seed("W6", "incident-runbook-templates", *_WSH, "plugins/incident-response/skills/incident-runbook-templates",
         ("quality",), "applies:server", "conditional"),
    Seed("M3", "research", *_MATT, "skills/engineering/research", ("materials",), "always", "reference"),
    Seed("M4", "codebase-design", *_MATT, "skills/engineering/codebase-design", ("materials",),
         "check:materials.module-boundaries!=not-applicable", "reference"),
    Seed("M5", "to-spec", *_MATT, "skills/engineering/to-spec", ("confirm",), "user-only", "user-only"),
    Seed("M6", "to-tickets", *_MATT, "skills/engineering/to-tickets", ("confirm",), "user-only", "user-only"),
    Seed("M7", "code-review", *_MATT, "skills/engineering/code-review", ("confirm",), "always", "conditional"),
    Seed("O2", "writing-plans", *_OBRA, "skills/writing-plans", ("confirm",), "always", "reference"),
)
SEED_BY_ID: Mapping[str, Seed] = {seed.id: seed for seed in SEED_SKILLS}
# Held, never linked as eligible: no license file, a README-only claim with mutable remote material, or a license
# the policy must allow first. They are named so a search hit for them is explained, not silently dropped.
HELD = {"better-auth": "no license file in the skill repository",
        "web-design-guidelines": "a README license claim only, and it reads mutable remote material",
        "frontend-design": "Apache-2.0; eligible only where the license policy allows Apache-2.0"}

# (design stage, check) -> query templates. A template whose placeholder label is undecided or local-only produces
# no query; every literal word is public (PUBLIC_QUERY_TERMS).
QUERY_TEMPLATES: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("purpose", "terms"): ("domain modeling",),
    ("behavior", "failure-flow"): ("error handling patterns",),
    ("data", "entities"): ("{database} schema design",),
    ("data", "integrity"): ("{database} migrations constraints",),
    ("data", "lifetime"): ("{database} backup recovery",),
    ("access", "rules"): ("{auth} authorization patterns", "{platform} row level security"),
    ("server", "api-contract"): ("api design principles",),
    ("server", "secrets"): ("secrets management",),
    ("server", "external-effects"): ("{durable-workflow} workflow orchestration",),
    ("payment", "state-transitions"): ("{payment-provider} webhooks",),
    ("payment", "failure-refund-cancel"): ("{payment-provider} refunds",),
    ("quality", "accessibility"): ("{ui} accessibility audit",),
    ("quality", "observability"): ("{hosting} observability",),
    ("quality", "recovery-scenario"): ("incident runbook",),
    ("materials", "stack-options"): ("{runtime} project structure",),
    ("materials", "module-boundaries"): ("codebase design modules",),
}
_PLACEHOLDER = re.compile(r"\{([a-z-]+)\}")
PUBLIC_QUERY_TERMS: frozenset[str] = frozenset(
    {word for templates in QUERY_TEMPLATES.values() for template in templates
     for word in _PLACEHOLDER.sub(" ", template).split()}
    | {value for values in TECH_LABELS.values() for value in values if value not in ("none", "other")}
    | {"skill", "skills", "best", "practices"})


def queries_for(stage: str, check: str, tech: Mapping[str, Mapping]) -> tuple[tuple[str, dict], ...]:
    """The public queries a (stage, check) asks for, with the tech values they used; none for undecided labels."""
    out = []
    for template in QUERY_TEMPLATES.get((stage, check), ()):
        used: dict[str, str] = {}
        text = template
        for label in _PLACEHOLDER.findall(template):
            entry = tech.get(label)
            value = entry.get("value") if isinstance(entry, Mapping) else None
            if not isinstance(value, str) or value in ("none", "other") or LOCAL_VALUE.fullmatch(value):
                break
            used[label] = value
            text = text.replace("{" + label + "}", value)
        else:
            out.append((text, used))
    return tuple(out)


# Conditions (closed grammar, AND only) --------------------------------------------------------------------------------

_CLAUSE = re.compile(r"always|user-only|applies:([a-z-]+)|tech:([a-z-]+)=([a-z0-9-]+(?:,[a-z0-9-]+)*)"
                     r"|check:([a-z-]+)\.([a-z-]+)!=not-applicable")


def parse_condition(text: object) -> tuple[tuple[str, ...], ...]:
    """The clauses of a condition, each (kind, args...); ValueError for anything outside the grammar."""
    if not isinstance(text, str) or not text or len(text) > 200:
        raise ValueError("a condition is a short text such as `always` or `tech:database=postgresql`")
    clauses = []
    for part in text.split(" AND "):
        match = _CLAUSE.fullmatch(part)
        if not match:
            raise ValueError(f"condition clause {part!r} is not always, user-only, applies:<stage>, "
                             "tech:<label>=<value>[,<value>] or check:<stage>.<check>!=not-applicable")
        if part in ("always", "user-only"):
            clauses.append((part,))
        elif match.group(1):
            if match.group(1) not in BY_ID:
                raise ValueError(f"condition names unknown stage {match.group(1)!r}")
            clauses.append(("applies", match.group(1)))
        elif match.group(2):
            label, values = match.group(2), tuple(match.group(3).split(","))
            if label not in TECH_LABELS or any(value not in TECH_LABELS[label] for value in values):
                raise ValueError(f"condition names an unknown tech label or value in {part!r}")
            clauses.append(("tech", label, *values))
        else:
            stage, check = match.group(4), match.group(5)
            if stage not in BY_ID or check not in all_checks(stage):
                raise ValueError(f"condition names unknown check {stage}.{check}")
            clauses.append(("check", stage, check))
    return tuple(clauses)


def all_checks(stage: str) -> tuple[str, ...]:
    item = BY_ID[stage]
    return (*item.checks, *item.conditional)


def _stage_entry(record: Mapping, stage: str) -> Mapping | None:
    for entry in record.get("stages", ()):
        if isinstance(entry, Mapping) and entry.get("id") == stage:
            return entry
    return None


def explain_condition(condition: str, record: Mapping) -> tuple[bool | None, str]:
    """(holds, reason) — holds is None while an input is undecided; the reason names the deciding input."""
    verdict: bool | None = True
    reasons: list[str] = []
    for clause in parse_condition(condition):
        kind = clause[0]
        if kind == "always":
            holds, why = True, "always"
        elif kind == "user-only":
            holds, why = False, "user-only: named for explicit use, never delivered automatically"
        elif kind == "applies":
            entry = _stage_entry(record, clause[1])
            value = "applies" if not BY_ID[clause[1]].may_be_na else (entry or {}).get("applicability", "undecided")
            holds = {"applies": True, "not-applicable": False}.get(value)
            why = f"{clause[1]} {'applies' if holds else 'is undecided' if holds is None else 'is not applicable'}"
        elif kind == "tech":
            entry = record.get("tech", {}).get(clause[1])
            value = entry.get("value") if isinstance(entry, Mapping) else None
            holds = None if value is None else value in clause[2:]
            why = (f"{clause[1]} is undecided" if value is None else f"{clause[1]} is {value}")
        else:
            entry = _stage_entry(record, clause[1]) or {}
            status = next((item.get("status") for item in entry.get("checks", ())
                           if isinstance(item, Mapping) and item.get("id") == clause[2]), "open")
            holds = None if status == "open" else status != "not-applicable"
            why = f"{clause[1]}.{clause[2]} is {status}"
        if holds is False:
            return False, why
        if holds is None:
            verdict = None
        reasons.append(why)
    return verdict, "; ".join(reasons)


def condition_holds(condition: str, record: Mapping) -> bool | None:
    return explain_condition(condition, record)[0]
