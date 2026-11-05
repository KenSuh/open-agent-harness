# 제안 평가 — 명시 요청만 공개 출처의 고정 질문을 평가하며 A·출처 신원을 재확인한 조언과 불변 근거를 분리해 보관한다.
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TYPE_CHECKING

from oah.design.sources import association
from oah.design.sources.state import StateProblem, digest_of, hex_of, is_hex
from oah.design.sources.view import DesignView, ViewUnavailable
from oah.rerank import judge
from oah.rerank.base import Provider

if TYPE_CHECKING:
    from oah.design.sources.check import Proposal, Sources

MAX_ASSESSMENTS = 64
INDEX_LIMIT = 1 << 20
INDEX_ENTRIES = 4096


def unassessed(reason: str = "not-assessed") -> dict[str, Any]:
    return {"jev": f"unused({reason})", "applied": f"no({reason})", "labels": [], "urgency": None,
            "record": None}


def _index(sources: Sources) -> dict[str, str]:
    data = sources.state.read_json("assessment-index.json", INDEX_LIMIT)
    if data is None:
        return {}
    if not isinstance(data, dict) or set(data) != {"version", "assessments"} or data["version"] != 1 \
            or not isinstance(data["assessments"], dict) or len(data["assessments"]) > INDEX_ENTRIES \
            or any(not is_hex(key) or hex_of(value) is None for key, value in data["assessments"].items()):
        raise StateProblem("bad-record", "assessment-index.json", "is not a bounded assessment index")
    return data["assessments"]


def _basis(sources: Sources, proposal: Proposal,
           reader: Callable[[str, str], DesignView]) -> tuple[dict[str, Any], DesignView]:
    view = reader(proposal.project, proposal.design)
    entry = sources._designs().get(association.key_of(proposal.uid, proposal.design))
    if not association.well_formed(entry) or association.digest(entry) != proposal.association \
            or not association.matches_view(entry, view, policy=sources.policy_digest())["matches"]:
        raise ViewUnavailable("OAH_SOURCES_ASSOCIATION_UNKNOWN")
    current = next((item for item in sources.proposals(design=(proposal.uid, proposal.design))[0]
                    if item.key == proposal.key), None)
    if current is None or current.status != proposal.status or current.status != "open":
        raise ViewUnavailable("OAH_SOURCES_PROPOSAL_CHANGED")
    spec = sources.spec(proposal.source)
    status = sources.status(spec)
    if status.head is None or status.head.verified is None \
            or status.head.verified.snapshot != proposal.to_snapshot:
        raise ViewUnavailable("OAH_SOURCES_PROPOSAL_UNKNOWN")
    fields = current.to_json()
    fields.pop("assessment", None)
    return {"proposal": fields, "binding": entry["binding"], "materials": entry["materials"],
            "registration": spec.registration, "spec_digest": spec.spec_digest, "policy": sources.policy_digest(),
            "freshness": status.freshness}, view


def _role(view: DesignView, source: str) -> str:
    from oah.design import materials

    roles = []
    for snapshot in (view.current, view.accepted):
        if snapshot is None or "materials" not in snapshot.texts:
            continue
        record = materials.parse(snapshot.texts["materials"])
        if record is None:
            continue
        for item in record["materials"]:
            if (item.get("source") or {}).get("id") == source and item.get("choice") in ("selected", "alternative"):
                roles.append("chosen" if item["choice"] == "selected" else "alternative")
    return "chosen" if "chosen" in roles else "alternative" if roles else "dependency"


def request_for(sources: Sources, proposal: Proposal, basis: Mapping[str, Any], view: DesignView) -> judge.Request:
    fields = []
    for snapshot, prefix in ((proposal.from_snapshot, "old"), (proposal.to_snapshot, "new")):
        for path, role in (("release-tag", "from_tag" if prefix == "old" else "to_tag"),
                           ("release-notes", prefix + "_release_note_excerpt")):
            try:
                data = judge._public_bytes(sources, snapshot, path)
                size = min(len(data), 4000)
                while size and (data[size:size + 1] and data[size] & 0xC0 == 0x80):
                    size -= 1
                if size:
                    fields.append((role, judge.public_field(sources, snapshot, path, 0, size)))
            except judge.JudgeError:
                continue
    if not any(role.endswith("note_excerpt") for role, _field in fields):
        for snapshot in (proposal.from_snapshot, proposal.to_snapshot):
            try:
                data = judge._public_bytes(sources, snapshot, "document-text")
                size = min(len(data), 4000)
                while size and (data[size:size + 1] and data[size] & 0xC0 == 0x80):
                    size -= 1
                if size:
                    fields.append(("document_excerpt", judge.public_field(sources, snapshot, "document-text", 0, size)))
                    break
            except judge.JudgeError:
                continue
    delta = next((word[7:] for word in proposal.labels if word in
                  ("semver-major", "semver-minor", "semver-patch")), "metadata-only" if
                 proposal.kind == "release-metadata-changed" else "unknown")
    fields.extend((("version_delta", judge.label("version-delta", delta)),
                   ("role", judge.label("role", _role(view, proposal.source)))))
    return judge.Request("source-change-impact", "source-change-assessment/v1", tuple(fields),
                         input_ref=digest_of(dict(basis)))


def _view(record: Mapping[str, Any], applied: judge.Applied, digest: str) -> dict[str, Any]:
    labels = []
    if applied.applied:
        for name, reading in (applied.outcomes.get("labels") or {}).items():
            if reading == "yes":
                labels.append({"label": name, "probability": record["answers"][name]["noul"]})
    return {"jev": record["status"], "applied": "yes" if applied.applied else f"no({applied.reason})",
            "labels": labels, "urgency": applied.outcomes.get("urgency") if applied.applied else None,
            "record": digest}


def assess(sources: Sources, proposals: Sequence[Proposal], *, reader: Callable[[str, str], DesignView],
           config: Mapping[str, Any], env: Mapping[str, str] | None = None, keychain=None,
           providers: Mapping[str, Provider] | None = None) -> list[Proposal]:
    rows = []
    budget = judge.Budget(8)
    for index, proposal in enumerate(proposals):
        if index >= MAX_ASSESSMENTS or proposal.status != "open":
            rows.append(dataclasses.replace(proposal, assessment=unassessed("budget" if index >= MAX_ASSESSMENTS
                                                                         else "dismissed")))
            continue
        basis, view = _basis(sources, proposal, reader)
        request = request_for(sources, proposal, basis, view)
        if not any(type(field) is judge.PublicSnapshotField and role.endswith("excerpt") for role, field in request.fields):
            result = judge.Judgement("unused(insufficient-input)", request.purpose, None)
        else:
            result = judge.judge(request, config=config, policy=sources.policy_digest(), sources=sources,
                                 state_dir=sources.state_dir, budget=budget, env=env, keychain=keychain, providers=providers)
        latest, latest_view = _basis(sources, proposal, reader)
        latest_request = request_for(sources, proposal, latest, latest_view)
        applied = judge.apply(result, current_input_ref=digest_of(latest), config=config, sources=sources,
                              request=latest_request, policy=sources.policy_digest())
        if applied.applied and latest["freshness"] != "fresh":
            applied = judge.Applied(False, "source-not-fresh", {}, None)
        record = {"version": 1, "kind": "oah-proposal-assessment", "proposal": proposal.key,
                  "input_ref": digest_of(basis), "basis": basis, "judgement": result.to_record(),
                  "applied": dataclasses.asdict(applied), "at": sources.now().isoformat()}
        with sources.state.lock():
            current, _current_view = _basis(sources, proposal, reader)
            if current != latest:
                raise ViewUnavailable("OAH_SOURCES_PROPOSAL_CHANGED")
            pointers = _index(sources)
            if len(pointers) >= INDEX_ENTRIES and proposal.key not in pointers:
                raise StateProblem("oversize", "assessment-index.json", "assessment index is full; evidence is retained")
            digest = sources.store_assessment(record)
            pointers[proposal.key] = digest
            sources.state.write_json("assessment-index.json", {"version": 1, "assessments": pointers})
        rows.append(dataclasses.replace(proposal, assessment=_view(result.to_record(), applied, digest)))
    return rows


def recorded(sources: Sources, proposals: Sequence[Proposal], *, reader: Callable[[str, str], DesignView],
             config: Mapping[str, Any]) -> list[Proposal]:
    pointers = _index(sources)
    rows = []
    for proposal in proposals:
        if proposal.status != "open":
            rows.append(dataclasses.replace(proposal, assessment=unassessed("dismissed")))
            continue
        digest = pointers.get(proposal.key)
        record = None if digest is None else sources.assessment(digest)
        if digest is None or not isinstance(record, dict):
            rows.append(dataclasses.replace(proposal, assessment=unassessed()))
            continue
        if set(record) != {"version", "kind", "proposal", "input_ref", "basis", "judgement", "applied", "at"} \
                or record.get("version") != 1 or record.get("kind") != "oah-proposal-assessment" \
                or record.get("proposal") != proposal.key or not isinstance(record.get("basis"), Mapping) \
                or not isinstance(record.get("judgement"), Mapping) \
                or record.get("input_ref") != digest_of(record["basis"]):
            raise StateProblem("bad-record", "assessment-index.json", "assessment evidence has an unknown shape")
        basis, view = _basis(sources, proposal, reader)
        request = request_for(sources, proposal, basis, view)
        applied = judge.apply(record["judgement"], current_input_ref=digest_of(basis), config=config, sources=sources,
                              request=request, policy=sources.policy_digest())
        if applied.applied and basis["freshness"] != "fresh":
            applied = judge.Applied(False, "source-not-fresh", {}, None)
        rows.append(dataclasses.replace(proposal, assessment=_view(record["judgement"], applied, digest)))
    return rows
