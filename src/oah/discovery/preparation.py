# E 준비 신원 — 로컬 사실과 최종 전달 해시를 하나의 닫힌 기록 및 수정용 비문장 투영으로 묶는다.
from __future__ import annotations

import dataclasses
import json
from typing import Callable, Final, TypeAlias

from oah.discovery.store import digest

Json: TypeAlias = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
Record: TypeAlias = dict[str, Json]
KEYS: Final = frozenset({"version", "prep_id", "facts", "references", "profile", "envelope", "expires_at", "final",
                        "digest"})
FACT_KEYS: Final = frozenset({"operation", "task", "lease", "engine", "stage", "route", "needs", "selections",
                            "payloads", "not_delivered", "policy", "inventory", "relay_results", "target",
                            "request_budget", "references", "profile", "selectors", "access", "context"})
REFERENCE_KEYS: Final = frozenset({"needs", "skill_needs", "parent_inventory", "relay_results", "tier", "route"})
FINAL_KEYS: Final = frozenset({"bundle_digest", "capability_plan_digest", "render_digest"})
PROJECTION_KEYS: Final = frozenset({"version", "identity_digest", "prep_id", "admission_digest", "bundle_digest",
    "capability_plan_digest", "pack_id", "profile_digest", "render_digest", "task_digest", "need_digest",
    "selection_digest", "policy_digest", "inventory_digest", "relay_digest", "reference_digest", "context_digest",
    "target_digest", "payloads", "not_delivered", "engine", "stage", "route", "receiving", "counts", "envelope",
    "expires_at", "counted"})


def encoded(value: Record) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclasses.dataclass(frozen=True, slots=True)
class PreparedE:
    facts: str
    references: str
    profile: str
    envelope: str
    expires_at: str
    recheck: Callable[[], None] = dataclasses.field(compare=False, repr=False)
    final: str = "{}"

    def record(self) -> Record:
        facts = json.loads(self.facts)
        pre_result = {key: value for key, value in facts.items() if key not in ("relay_results", "capability_plan")}
        record = {"version": 1, "prep_id": digest(pre_result), "facts": facts,
                  "references": json.loads(self.references), "profile": json.loads(self.profile),
                  "envelope": json.loads(self.envelope), "expires_at": self.expires_at,
                  "final": json.loads(self.final)}
        return dict(record, digest=digest(record))

    def finish(self, final: Record, envelope: Record) -> PreparedE:
        self.recheck()
        return dataclasses.replace(self, final=encoded(final), envelope=encoded(envelope))


def parse(raw: Json) -> Record:
    if not isinstance(raw, dict) or set(raw) != KEYS or type(raw.get("version")) is not int or raw.get("version") != 1 \
            or not all(isinstance(raw.get(key), dict) for key in ("facts", "references", "profile", "envelope", "final")) \
            or not isinstance(raw.get("expires_at"), str) \
            or raw.get("digest") != digest({key: value for key, value in raw.items() if key != "digest"}):
        raise ValueError("the run's PreparedE record is missing, changed or malformed; prepare the run again")
    facts = raw["facts"]
    assert isinstance(facts, dict)
    references, final = raw["references"], raw["final"]
    if set(facts) != FACT_KEYS or not isinstance(references, dict) or set(references) != REFERENCE_KEYS \
            or not isinstance(final, dict) or set(final) != FINAL_KEYS:
        raise ValueError("the PreparedE facts, references or final projection have unknown or missing fields")
    context = facts["context"]
    operation = facts["operation"]
    if not isinstance(operation, dict) or (facts["route"] == "lead") != (operation.get("kind") != "dispatch") \
            or facts["route"] != references["route"]:
        raise ValueError("the PreparedE operation and route do not agree")
    if not isinstance(context, dict) or set(context) != {"root", "axes", "floor"} \
            or not isinstance(context["root"], str) or not context["root"]:
        raise ValueError("the PreparedE command context is incomplete")
    axes = context["axes"]
    if facts["route"] == "lead":
        if axes is not None or context["floor"] is not None:
            raise ValueError("a taskless preparation has no dispatch axes or floor")
    elif not isinstance(axes, dict) or set(axes) != {"kind", "intensity", "reasoning", "risk"} \
            or any(not isinstance(value, str) for value in axes.values()) \
            or context["floor"] not in ("low", "medium", "high", "max"):
        raise ValueError("the PreparedE request axes are incomplete")
    from oah.discovery.budget import moment

    moment(str(raw["expires_at"]))
    for key in ("needs", "selections", "payloads", "not_delivered", "relay_results"):
        rows = facts[key]
        if not isinstance(rows, list) or any(not isinstance(item, dict) and not (
                key == "selections" and item is None) for item in rows):
            raise ValueError(f"the PreparedE {key} must be complete recorded rows")
    recorded_needs, saved_needs, relay_refs = facts["needs"], references["needs"], references["relay_results"]
    if not isinstance(recorded_needs, list) or not isinstance(saved_needs, list) \
            or any(not isinstance(item, dict) or item not in recorded_needs for item in saved_needs) \
            or not isinstance(relay_refs, list) or any(not _hash(ref) for ref in relay_refs):
        raise ValueError("the PreparedE need or relay references are incomplete")
    if facts["route"] != "lead":
        from oah.capabilities import inventory as inventories

        if facts["route"] not in ("process", "orca", "native") or facts["engine"] not in ("claude", "codex") \
                or facts["stage"] not in ("initial", "correction", "review"):
            raise ValueError("the PreparedE dispatch target is incomplete")
        inventory = facts["inventory"]
        if not isinstance(inventory, dict) or set(inventory) != {"child", "parent"} \
                or not _hash(inventory["child"]):
            raise ValueError("the PreparedE inventory is incomplete")
        parent = inventory["parent"]
        if parent is not None:
            if not isinstance(parent, dict) or not isinstance(parent.get("observed_at"), str):
                raise ValueError("the PreparedE parent inventory is incomplete")
            inventories.check_parent(parent, now=moment(parent["observed_at"]))
        if (references["parent_inventory"] is not None and not isinstance(references["parent_inventory"], str)) \
                or bool(references["parent_inventory"]) != (parent is not None) \
                or sorted(references["relay_results"]) != sorted(row.get("id", "") for row in facts["relay_results"]):
            raise ValueError("the PreparedE inventory or relay references do not match their records")
        for row in facts["payloads"]:
            if not isinstance(row, dict) or row.get("kind") not in ("body", "auxiliary", "relay") \
                    or type(row.get("required")) is not bool or not _nonnegative(row.get("bytes")) \
                    or not _hash(row.get("sha256")) \
                    or row.get("mode") not in ("inline", "reference", "reference-only", "not-delivered"):
                raise ValueError("the PreparedE payload is incomplete")
        if not isinstance(facts["target"], dict) or not all(
                key in facts["target"] for key in ("model", "effort", "cli_version")):
            raise ValueError("the PreparedE target is incomplete")
    pre_result = {key: value for key, value in facts.items() if key not in ("relay_results", "capability_plan")}
    if raw["prep_id"] != digest(pre_result):
        raise ValueError("the run's PreparedE preparation identity changed; prepare the run again")
    return raw


def projection(record: Record) -> Record:
    checked = parse(record)
    facts, final = checked["facts"], checked["final"]
    assert isinstance(facts, dict) and isinstance(final, dict)
    items = facts.get("payloads", [])
    assert isinstance(items, list)
    payloads = []
    for row in items:
        assert isinstance(row, dict)
        size = row.get("bytes")
        if row.get("kind") not in ("body", "auxiliary", "relay") or type(row.get("required")) is not bool \
                or row.get("mode") not in ("inline", "reference", "reference-only", "not-delivered") \
                or not isinstance(size, int) or isinstance(size, bool) or size < 0 or not _hash(row.get("sha256")):
            raise ValueError("the PreparedE payload is not a closed digest/count/enum projection")
        payloads.append({key: row[key] for key in ("kind", "required", "bytes", "sha256", "mode")})
    reasons = facts.get("not_delivered", [])
    needs, selections, relays = facts.get("needs", []), facts.get("selections", []), facts.get("relay_results", [])
    assert isinstance(reasons, list) and isinstance(needs, list) and isinstance(selections, list) \
        and isinstance(relays, list)
    enums = {"engine": {"claude", "codex"}, "stage": {"initial", "correction", "review"},
             "route": {"process", "orca", "native"}}
    for key, allowed in enums.items():
        if facts.get(key) not in allowed:
            raise ValueError(f"the PreparedE {key} is not a supported enum")
    for key in ("bundle_digest", "capability_plan_digest", "render_digest"):
        value = final.get(key)
        if value is not None and not _hash(value):
            raise ValueError(f"the PreparedE {key} is not a digest")
    envelope = checked["envelope"]
    assert isinstance(envelope, dict)
    numeric = ("prompt_bytes", "deferred_required_bytes", "deferred_render_bytes", "optional_reserve_bytes",
               "upper_tokens", "available_tokens")
    if set(envelope) != {*numeric, "sha256"} or not _hash(envelope["sha256"]) \
            or any(not _nonnegative(envelope[key]) for key in numeric):
        raise ValueError("the PreparedE envelope is not a closed measurement")
    return {"version": 1, "identity_digest": checked["digest"], "prep_id": checked["prep_id"],
            "admission_digest": digest(facts), "bundle_digest": final.get("bundle_digest"),
            "capability_plan_digest": final.get("capability_plan_digest"),
            "pack_id": digest(final), "profile_digest": digest(checked["profile"]),
            "render_digest": final.get("render_digest"),
            "task_digest": digest({"task": facts.get("task"), "lease": facts.get("lease")}),
            "need_digest": digest(facts.get("needs")), "selection_digest": digest(facts.get("selections")),
            "policy_digest": digest(facts.get("policy")), "inventory_digest": digest(facts.get("inventory")),
            "relay_digest": digest(facts.get("relay_results")),
            "reference_digest": digest({"references": checked["references"], "access": facts.get("access")}),
            "context_digest": digest(facts["context"]),
            "target_digest": digest(facts.get("target")), "payloads": payloads,
            "not_delivered": [{"reason_digest": digest(reason)} for reason in reasons],
            "engine": facts.get("engine"), "stage": facts.get("stage"), "route": facts.get("route"),
            "receiving": {"delivery": "offered" if facts.get("route") == "native" else "prompt-written",
                          "reference_delivery": "required" if any(row["mode"] == "reference" for row in payloads)
                          else "not-needed", "read": "unverified", "application": "not-observed"},
            "counts": {"needs": len(needs), "selections": sum(item is not None for item in selections),
                       "relay_results": len(relays),
                       "payloads": len(payloads), "not_delivered": len(reasons)},
            "envelope": envelope, "expires_at": checked["expires_at"], "counted": False}


def check_projection(raw: Record) -> None:
    from oah.discovery.budget import moment

    if set(raw) != PROJECTION_KEYS or type(raw["version"]) is not int or raw["version"] != 1 \
            or raw["counted"] is not False:
        raise ValueError("the earlier correction projection is incomplete")
    nullable = {"bundle_digest", "capability_plan_digest", "render_digest"}
    for key in (key for key in PROJECTION_KEYS if key.endswith("digest") or key in ("prep_id", "pack_id")):
        if not _hash(raw[key]) and not (key in nullable and raw[key] is None):
            raise ValueError("the earlier correction projection has an invalid digest")
    if raw["engine"] not in ("claude", "codex") or raw["stage"] not in ("initial", "correction", "review") \
            or raw["route"] not in ("process", "orca", "native"):
        raise ValueError("the earlier correction projection has an invalid target")
    counts, envelope, receiving = raw["counts"], raw["envelope"], raw["receiving"]
    if not isinstance(counts, dict) or set(counts) != {"needs", "selections", "relay_results", "payloads", "not_delivered"} \
            or any(not _nonnegative(value) for value in counts.values()) \
            or not isinstance(envelope, dict) or set(envelope) != {"prompt_bytes", "deferred_required_bytes",
                "deferred_render_bytes", "optional_reserve_bytes", "upper_tokens", "available_tokens", "sha256"} \
            or not _hash(envelope["sha256"]) \
            or any(not _nonnegative(value) for key, value in envelope.items() if key != "sha256") \
            or not isinstance(receiving, dict) or set(receiving) != {"delivery", "reference_delivery", "read", "application"} \
            or receiving["delivery"] not in ("offered", "prompt-written") \
            or receiving["reference_delivery"] not in ("required", "not-needed") \
            or receiving["read"] != "unverified" or receiving["application"] != "not-observed":
        raise ValueError("the earlier correction projection has invalid counts or receiving fields")
    payloads, reasons = raw["payloads"], raw["not_delivered"]
    if not isinstance(payloads, list) or len(payloads) != counts["payloads"] \
            or not isinstance(reasons, list) or len(reasons) != counts["not_delivered"]:
        raise ValueError("the earlier correction projection has incomplete payloads")
    for row in payloads:
        if not isinstance(row, dict) or set(row) != {"kind", "required", "bytes", "sha256", "mode"} \
                or row["kind"] not in ("body", "auxiliary", "relay") or type(row["required"]) is not bool \
                or not _nonnegative(row["bytes"]) or not _hash(row["sha256"]) \
                or row["mode"] not in ("inline", "reference", "reference-only", "not-delivered"):
            raise ValueError("the earlier correction projection has an invalid payload")
    if any(not isinstance(row, dict) or set(row) != {"reason_digest"} or not _hash(row["reason_digest"])
           for row in reasons):
        raise ValueError("the earlier correction projection has an invalid omission")
    moment(str(raw["expires_at"]))


def correction_identity(record: Record) -> str:
    facts = parse(record)["facts"]
    if not isinstance(facts, dict):
        raise ValueError("a correction needs dispatch facts")
    inventory, context = facts["inventory"], facts["context"]
    if not isinstance(inventory, dict) or not isinstance(context, dict):
        raise ValueError("a correction needs a dispatch inventory and context")
    return digest({"task": facts["task"], "needs": facts["needs"], "selections": facts["selections"],
                   "policy": facts["policy"], "root": context["root"], "parent": inventory["parent"],
                   "relays": facts["relay_results"]})


def _hash(value: Json) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71 and all(
        char in "0123456789abcdef" for char in value[7:])


def _nonnegative(value: Json) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
