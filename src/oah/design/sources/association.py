# 설계-출처 연결 캐시 — 프로젝트 uid·설계 키로 현재·수락 참조와 자료 다이제스트, 출처 등록·명세·스냅숏을 세대 CAS 로 단조 공개하고, 소비자가 현재 보기와 정확히 맞는지 다시 확인하게 한다.
"""AssociationV1, its monotonic publication rule and the consumer recheck.

An association says which registered sources a design's materials reference, for exactly one current and accepted
pair. It is advisory: a consumer rereads A and the source/policy dependencies and calls `matches_view` before using
facts, proposals or recommendations built on it.

Publication compares the candidate with the stored entry: a lower current revision is superseded; the same current
revision with an older acceptance cannot roll a newer one back; the same revision with a different digest (either
identity) is invalid and keeps the last good entry with a bounded conflict note; only a newer pair, or the exact same
pair with fresher source observations, publishes. An entry carrying a conflict note is not current for any consumer
until a later verified reread publishes the pair again. The caller holds the sources lock and passes the generation it
captured; a moved generation is recomputed against the entry actually stored, never forced through.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from oah.design.sources.state import digest_of
from oah.design.sources.view import DesignView

VERSION = 1
KIND = "oah-source-association"
DECISIONS = ("publish", "refresh", "same", "superseded", "invalid")


def key_of(uid: str, design: str) -> str:
    return f"{uid}/{design}"


def build(view: DesignView, sources: Sequence[Mapping], *, input_basis: Mapping, generation: int,
          observed_at: str) -> dict:
    """A candidate association for exactly this view; `sources` are the materials' source references."""
    return {"version": VERSION, "kind": KIND, "project_uid": view.uid, "design": view.design, "project": view.project,
            "current": {"revision": view.current.ref.revision, "digest": view.current.ref.digest},
            "accepted": None if view.accepted is None else {
                "revision": view.accepted.ref.revision, "digest": view.accepted.ref.digest,
                "acceptance": None if view.acceptance is None else {
                    "sequence": view.acceptance["sequence"], "record": view.acceptance["record"]}},
            "materials": {"current": view.materials("current"), "accepted": view.materials("accepted")},
            "input_basis": dict(input_basis),
            "sources": sorted((dict(item) for item in sources), key=lambda item: (item["id"], item["snapshot"] or "")),
            "generation": generation, "observed_at": observed_at, "conflict": None}


def digest(entry: Mapping) -> str:
    return digest_of({key: value for key, value in entry.items() if key not in ("generation", "observed_at")})


def well_formed(entry: object) -> bool:
    if not isinstance(entry, Mapping) or entry.get("version") != VERSION or entry.get("kind") != KIND:
        return False
    current = entry.get("current")
    if not isinstance(current, Mapping) or type(current.get("revision")) is not int:
        return False
    if not isinstance(entry.get("sources"), list) or type(entry.get("generation")) is not int:
        return False
    accepted = entry.get("accepted")
    return accepted is None or (isinstance(accepted, Mapping) and type(accepted.get("revision")) is int)


def decide(existing: Mapping | None, candidate: Mapping) -> str:
    """publish | refresh | same | superseded | invalid for a candidate against the stored entry."""
    if existing is None:
        return "publish"
    old, new = existing["current"], candidate["current"]
    if new["revision"] < old["revision"]:
        return "superseded"
    if new["revision"] == old["revision"] and new["digest"] != old["digest"]:
        return "invalid"
    old_acc, new_acc = existing.get("accepted"), candidate.get("accepted")
    if old_acc is not None:
        if new_acc is None:
            return "superseded"
        old_seq = (old_acc.get("acceptance") or {}).get("sequence", 0)
        new_seq = (new_acc.get("acceptance") or {}).get("sequence", 0)
        if new_seq < old_seq or new_acc["revision"] < old_acc["revision"]:
            return "superseded"
        if new_seq == old_seq and (new_acc["revision"] != old_acc["revision"] or new_acc["digest"] != old_acc["digest"]
                                   or new_acc.get("acceptance") != old_acc.get("acceptance")):
            return "invalid"
    if new["revision"] > old["revision"] or (new_acc != old_acc):
        return "publish"
    same_inputs = all(candidate.get(key) == existing.get(key) for key in ("materials", "input_basis", "sources"))
    return "same" if same_inputs and existing.get("conflict") is None else "refresh"


def with_conflict(existing: Mapping, candidate: Mapping, at: str) -> dict:
    """The last good entry kept, with a bounded note of the conflicting candidate identity."""
    entry = dict(existing)
    entry["conflict"] = {"at": at, "current": dict(candidate["current"]),
                         "accepted": None if candidate.get("accepted") is None else dict(candidate["accepted"])}
    entry["generation"] = existing["generation"] + 1
    return entry


def matches_view(report: Mapping, view: DesignView, source_reads: Mapping[str, Mapping] | None = None,
                 policy: str | None = None) -> dict:
    """Whether a RefreshReport (or association) still describes `view` and the current source/policy reads.

    `source_reads` maps source ID -> {"registration", "spec_digest", "verified"}; `policy` is the current license
    policy digest. Returns {"matches": bool, "reasons": [...]}; any reason means discard the cached facts."""
    reasons = []
    binding = report.get("binding") or {}
    identity = view.identity()
    if binding.get("current") != identity["current"]:
        reasons.append("current-changed")
    if binding.get("accepted") != identity["accepted"]:
        reasons.append("accepted-changed")
    if (binding.get("current") or {}).get("uid") != view.uid:
        reasons.append("uid-changed")
    materials = report.get("materials") or {}
    if materials.get("current") != view.materials("current") or materials.get("accepted") != view.materials(
            "accepted"):
        reasons.append("materials-changed")
    if view.recovery_pending:
        reasons.append("recovery-pending")
    if report.get("conflict") is not None or (report.get("association") or {}).get("status") == "invalid":
        reasons.append("association-conflict")
    if source_reads is not None:
        for item in report.get("sources") or []:
            read = source_reads.get(item.get("id"))
            if read is None or any(read.get(key) != item.get(key)
                                   for key in ("registration", "spec_digest", "verified")):
                reasons.append(f"source-changed:{item.get('id')}")
    if policy is not None and report.get("policy") != policy:
        reasons.append("policy-changed")
    return {"matches": not reasons, "reasons": reasons}
