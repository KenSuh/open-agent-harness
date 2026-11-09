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
                            "request_budget", "references", "profile", "selectors", "access"})
REFERENCE_KEYS: Final = frozenset({"needs", "skill_needs", "parent_inventory", "relay_results", "tier", "route"})
FINAL_KEYS: Final = frozenset({"bundle_digest", "capability_plan_digest", "render_digest"})


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
    from oah.discovery.budget import moment

    moment(str(raw["expires_at"]))
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


def _hash(value: Json) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71 and all(
        char in "0123456789abcdef" for char in value[7:])


def _nonnegative(value: Json) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
