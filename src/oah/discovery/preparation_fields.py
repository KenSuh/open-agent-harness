# 준비 기록 검증 — 동결할 기록의 각 필드와 시간·발주 모양을 사용 전에 확인한다.
from __future__ import annotations

from collections.abc import Collection
import datetime as dt

from oah import routing
from oah.capabilities import CapabilityRefused, inventory, plan
from oah.discovery import budget, tasks
from oah.discovery.preparation_schema import (FACT_KEYS, FINAL_KEYS, REFERENCE_KEYS, Json, Record, _hash, _nonnegative)
from oah.discovery.store import digest


def closed(value: Json, keys: Collection[str], label: str) -> Record:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"the PreparedE {label} has unknown or missing fields")
    return value


def rows(value: Json, label: str, *, nullable: bool = False) -> list[Record]:
    if not isinstance(value, list) or any(not isinstance(row, dict) and not (nullable and row is None)
                                          for row in value):
        raise ValueError(f"the PreparedE {label} must be complete recorded rows")
    return [row for row in value if isinstance(row, dict)]


def moment(value: Json) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("a PreparedE time must be an ISO time with a time zone")
    return budget.moment(value)


def target(value: Json) -> Record:
    found = closed(value, budget.TARGET_KEYS, "target")
    if any(not isinstance(entry, str) and not (key == "cli_version" and entry is None)
           for key, entry in found.items()) or found["engine"] not in routing.ENGINES \
            or found["route"] not in ("process", "orca", "native") \
            or not _hash(found["root_digest"]) or not _hash(found["config_digest"]):
        raise ValueError("the PreparedE target has invalid values")
    return found


def profile(value: Json, *, identity_only: bool) -> None:
    if value == {} and identity_only:
        return
    found = closed(value, budget.PROFILE_KEYS, "profile")
    target(found["target"])
    estimator = closed(found["estimator"], budget.ESTIMATOR_KEYS, "estimator")
    numbers = ("input_capacity_tokens", "output_reserve_tokens", "host_reserve_tokens",
               "existing_context_upper_tokens", "transport_budget_bytes", "skill_budget_bytes")
    if type(found["version"]) is not int or found["version"] != 1 \
            or found["source"] not in ("qualified-profile", "live-host") \
            or not _hash(found["evidence_ref"]) or not _hash(found["id"]) \
            or found["id"] != digest({key: entry for key, entry in found.items() if key != "id"}) \
            or any(not _nonnegative(found[key]) for key in numbers) \
            or any(found[key] == 0 for key in ("input_capacity_tokens", "transport_budget_bytes", "skill_budget_bytes")) \
            or estimator["id"] != "utf8-upper/v1" \
            or any(not _nonnegative(estimator[key]) for key in ("fixed_render_tokens", "tool_schema_upper_tokens")):
        raise ValueError("the PreparedE profile has invalid values")
    observed, expires = moment(found["observed_at"]), moment(found["expires_at"])
    if observed >= expires:
        raise ValueError("the PreparedE profile has no finite evidence window")


def validate(record: Record) -> None:
    facts = closed(record["facts"], FACT_KEYS, "facts")
    refs = closed(record["references"], REFERENCE_KEYS, "references")
    final = closed(record["final"], FINAL_KEYS, "final")
    operation = closed(facts["operation"], {"kind", "task", "lease", "design"}, "operation")
    lease = operation["lease"]
    if operation["kind"] != "dispatch" or not isinstance(operation["task"], str) or not operation["task"] \
            or operation["design"] is not None or not isinstance(lease, list) or len(lease) != 2 \
            or not isinstance(lease[0], str) or not lease[0] or not _nonnegative(lease[1]):
        raise ValueError("the PreparedE record needs a task-bearing dispatch operation")
    if facts["route"] not in ("process", "orca", "native") or facts["route"] != refs["route"] \
            or facts["engine"] not in routing.ENGINES or facts["stage"] not in routing.STAGES:
        raise ValueError("the PreparedE dispatch route, engine or stage is invalid")
    context = closed(facts["context"], {"root", "axes", "floor"}, "context")
    axes = closed(context["axes"], {"kind", "intensity", "reasoning", "risk"}, "axes")
    allowed = {"kind": routing.KIND_SCORES, "intensity": routing.INTENSITY_SCORES,
               "reasoning": routing.REASONING_SCORES, "risk": routing.RISK_SCORES}
    if not isinstance(context["root"], str) or not context["root"] or context["floor"] not in routing.TIERS \
            or any(not isinstance(entry, str) or entry not in allowed[key] for key, entry in axes.items()):
        raise ValueError("the PreparedE dispatch context is incomplete")
    for key in ("task", "policy", "references", "profile", "selectors"):
        if not _hash(facts[key]):
            raise ValueError(f"the PreparedE {key} is not a digest")
    if facts["access"] is not None and not _hash(facts["access"]) or not _nonnegative(facts["request_budget"]):
        raise ValueError("the PreparedE access or request budget is invalid")
    lease_facts = closed(facts["lease"], {"digest", "expires_at"}, "lease")
    if not _hash(lease_facts["digest"]):
        raise ValueError("the PreparedE lease digest is invalid")
    moment(lease_facts["expires_at"])
    moment(record["expires_at"])
    needs = rows(facts["needs"], "needs")
    for need in needs:
        closed(need, tasks.NEED_KEYS, "need")
        if not _hash(need["id"]) or need["id"] != digest({key: entry for key, entry in need.items() if key != "id"}):
            raise ValueError("the PreparedE need does not match its complete record")
    saved_needs = rows(refs["needs"], "need references")
    if any(need not in needs for need in saved_needs):
        raise ValueError("the PreparedE need references do not match the recorded needs")
    for selected in rows(facts["selections"], "selections", nullable=True):
        closed(selected, {"version", "id", "need_id", "packet_digest", "intent_digest", "lease", "review_ids",
                          "selected_by", "policy_digest"}, "selection")
        selected_lease = closed(selected["lease"], {"id", "generation"}, "selection lease")
        reviews = selected["review_ids"]
        if selected["version"] != 1 or selected["selected_by"] != "lead" \
                or not isinstance(selected_lease["id"], str) or not _nonnegative(selected_lease["generation"]) \
                or not isinstance(reviews, list) or not reviews or any(not _hash(ref) for ref in reviews) \
                or any(not _hash(selected[key]) for key in ("id", "need_id", "packet_digest", "intent_digest", "policy_digest")):
            raise ValueError("the PreparedE selection has invalid values")
    payloads = rows(facts["payloads"], "payloads")
    for payload in payloads:
        keys = {"kind", "required", "bytes", "sha256", "mode"}
        if payload.get("kind") in ("body", "auxiliary"):
            keys.add("review")
        closed(payload, keys, "payload")
        if payload["kind"] not in ("body", "auxiliary", "relay") or type(payload["required"]) is not bool \
                or not _nonnegative(payload["bytes"]) or not _hash(payload["sha256"]) \
                or "review" in payload and not _hash(payload["review"]) \
                or payload["mode"] not in ("inline", "reference", "reference-only", "not-delivered"):
            raise ValueError("the PreparedE payload has invalid values")
    rows(facts["not_delivered"], "omissions")
    inventories = closed(facts["inventory"], {"child", "parent"}, "inventory")
    if not _hash(inventories["child"]):
        raise ValueError("the PreparedE child inventory digest is invalid")
    parent = inventories["parent"]
    if parent is not None:
        if not isinstance(parent, dict):
            raise ValueError("the PreparedE parent inventory is incomplete")
        inventory.check_parent(parent, now=moment(parent.get("observed_at")))
    relays = rows(facts["relay_results"], "relay results")
    for relay in relays:
        try:
            plan.check_result(relay)
        except CapabilityRefused as exc:
            raise ValueError(str(exc)) from None
    relay_refs = refs["relay_results"]
    if not isinstance(relay_refs, list) or any(not _hash(ref) for ref in relay_refs) \
            or sorted({str(ref) for ref in relay_refs}) != sorted(str(row["id"]) for row in relays):
        raise ValueError("the PreparedE relay references do not match their records")
    if refs["tier"] not in routing.TIERS \
            or any(refs[key] is not None and (not isinstance(refs[key], str) or not refs[key])
                   for key in ("skill_needs", "parent_inventory")) \
            or bool(refs["parent_inventory"]) != (parent is not None):
        raise ValueError("the PreparedE source references are incomplete")
    target(facts["target"])
    identity_only = not payloads and all(final[key] is None for key in ("bundle_digest", "capability_plan_digest"))
    profile(record["profile"], identity_only=identity_only)
    if facts["profile"] != digest(record["profile"]):
        raise ValueError("the PreparedE profile digest changed")
    for value in final.values():
        if value is not None and not _hash(value):
            raise ValueError("the PreparedE final projection has an invalid digest")
    envelope = closed(record["envelope"], {"prompt_bytes", "deferred_required_bytes", "deferred_render_bytes",
                     "optional_reserve_bytes", "upper_tokens", "available_tokens", "sha256"}, "envelope")
    if not _hash(envelope["sha256"]) or any(not _nonnegative(value) for key, value in envelope.items() if key != "sha256"):
        raise ValueError("the PreparedE envelope has invalid measurements")
