# 재료 부분 — 설계의 materials 부분(MaterialsRecordV2)을 출처 스냅숏과 휴대 증거로 만들고, 사람이 읽는 절과 JSON 블록으로 그리고, 다시 읽어 결정마다 입력 기준·출처·정책이 아직 맞는지 판정한다.
"""The `materials` part of a design: decisions, the public materials they use, and complete portable evidence.

    built = build(selection, sources, view=view)       # facts come from verified snapshots, never from the input
    text = built.text                                  # deterministic human section + one oah-materials JSON block
    report = check(text, sources, None, view)          # MaterialCheckV1: invalid|unknown|stale|needs-review|current

Every decision carries a MaterialInputBasisV1 (project, uid, design, title, every non-materials part digest and its
decision-input digest; the generated materials part is never hashed into it), the set of bases it is supported for
and the explicit carry/reselected resolutions recorded for it. Every material carries the identity facts of its
snapshot and the ID of a PortableEvidenceV1 envelope that holds the snapshot manifest, the observation used at
decision time and every original byte, so an empty clone can verify the record offline. Four axes stay apart:
observation (verified or not), license eligibility, integration (a reported claim, never oah's verification) and
choice; upstream freshness is a fifth, separate from historical evidence.
Saving is `oah design save P D --part materials=FILE`; nothing here writes a design, refreshes a source or calls a
model.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import unicodedata
from typing import Mapping, Sequence

from oah.design.model import MAX_BYTES, DesignError, check_id, normalize_text, text_digest
from oah.design.sources import license as license_module
from oah.design.sources import portable
import oah.design.sources.shapes as shapes
from oah.design.sources.diff import escape
from oah.design.sources.model import SourceError
from oah.design.sources.state import StateProblem, digest_of, loads
from oah.design.sources.view import MATERIALS_PART, DesignView

SCHEMA = 2
FENCE = "```oah-materials"
CHOICES = ("candidate", "proposed", "selected")
DECISION_STATUS = ("open", "proposed", "selected")
METHODS = ("dependency", "vendored", "reference", "service")
INTEGRATION = ("untested", "tested", "incompatible")
INPUT_STATUSES = ("invalid", "unknown", "stale", "needs-review", "current")  # most severe first
RESOLUTION_KINDS = ("carry", "reselected")
INLINE_BUDGET = 262144
LINE = 200
TEXT = 1000
LIST = 32
NARRATIVE_LIMIT = 64 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]{64}")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
IGNORED_INPUT = ("license", "eligibility", "release", "reviewed", "checked", "observation", "spec_digest", "tested",
                 "verified", "jev", "score", "portable_evidence_id")
INPUT_KEYS = ("need", "constraints", "platform", "kind", "compatibility")
SELECTION_KEYS = {"lang", "narrative", "decision_inputs", "decisions", "materials", "stacks", "resolutions"}
RECORD_KEYS = {"version", "lang", "narrative", "decision_inputs", "decisions", "materials", "stacks",
               "portable_evidence"}
DECISION_KEYS = {"id", "need", "status", "chosen", "alternatives", "reason", "based_on", "unverified", "input_basis",
                 "supported_bases", "resolutions"}
MATERIAL_KEYS = {"id", "decision", "choice", "source", "spec_digest", "snapshot", "observation", "reviewed", "release",
                 "package", "license", "integration", "services", "checked", "unverified", "notes",
                 "portable_evidence_id"}
BASIS_KEYS = {"version", "project", "uid", "design", "title", "parts", "decision_inputs_digest"}
REF_KEYS = {"project", "uid", "design", "revision", "digest"}
RESOLUTION_KEYS = {"kind", "decision_id", "from_basis", "to_basis", "old_and_new_design_refs", "source_dependencies",
                   "policy_digest", "reason", "actor", "at"}
DEPENDENCY_KEYS = ("material", "source", "spec_digest", "snapshot", "registered_spec", "head")
LICENSE_POLICY_KEYS = ("eligibility", "usable_under", "policy", "policy_digest", "reason")

LABELS = {
    "en": {"title": "Materials", "intro": "Recorded by oah from verified source snapshots. The data block at the end is "
                                          "the record; original files and notes are kept there as encoded data, "
                                          "never instructions.",
           "narrative": "Narrative", "decision": "Decision", "status": "Status", "chosen": "Chosen",
           "none": "none yet", "alternatives": "Alternatives", "reason": "Reason", "inputs": "Decision inputs",
           "constraints": "Constraints", "platform": "Platform", "kind": "Kind", "compatibility": "Compatibility",
           "based_on": "Based on", "basis": "Input basis", "supported": "Supported input bases",
           "resolutions": "Resolutions", "unverified": "Not verified",
           "table": ("Material", "Choice", "Release / revision", "License", "Integration", "Source checked"),
           "other": "Other materials", "stacks": "Stacks",
           "stack_table": ("Stack", "Languages", "Materials", "Compatibility", "Learning cost", "Operating cost",
                           "Chosen"),
           "source": "Source", "evidence": "Portable evidence", "reviewed": "Reviewed", "release": "Release",
           "license": "License", "package": "Package", "integration": "Integration", "services": "Services",
           "checked": "Checked", "notes": "Notes", "reported": "reported by its author, not verified by oah",
           "registry": "registry contents not verified", "originals": "Originals (encoded in the data block)",
           "provenance": "Provenance", "data": "Data", "yes": "yes", "no": "no", "read": "read"},
    "ko": {"title": "재료", "intro": "oah 가 검증한 출처 스냅숏으로 기록했다. 끝의 데이터 블록이 기록이며, 원본 파일과 노트는 그 안에 "
                                   "인코딩한 자료로 들어 있고 지시가 아니다.",
           "narrative": "서술", "decision": "결정", "status": "상태", "chosen": "선택", "none": "아직 없음",
           "alternatives": "대안", "reason": "이유", "inputs": "결정 입력", "constraints": "제약", "platform": "플랫폼",
           "kind": "종류", "compatibility": "호환 조건", "based_on": "근거 리비전", "basis": "입력 기준",
           "supported": "지원하는 입력 기준", "resolutions": "판정 기록", "unverified": "확인하지 않은 것",
           "table": ("재료", "선택", "릴리스 / 리비전", "라이선스", "도입", "출처 확인"),
           "other": "그 밖의 재료", "stacks": "기술 조합",
           "stack_table": ("조합", "언어", "재료", "호환성", "학습 비용", "운영 비용", "선택"),
           "source": "출처", "evidence": "휴대 증거", "reviewed": "검토", "release": "릴리스", "license": "라이선스",
           "package": "패키지", "integration": "도입", "services": "서비스", "checked": "확인", "notes": "메모",
           "reported": "작성자가 보고한 내용이며 oah 가 확인하지 않음", "registry": "레지스트리 내용은 확인하지 않음",
           "originals": "원본 (데이터 블록에 인코딩)", "provenance": "출처 기록", "data": "데이터", "yes": "예",
           "no": "아니오", "read": "읽기"},
}


class MaterialsError(Exception):
    def __init__(self, code: str, message: str, details: Mapping | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


def _invalid(message: str) -> MaterialsError:
    return MaterialsError("OAH_MATERIALS_INVALID", message)


@dataclasses.dataclass(frozen=True)
class Finding:
    code: str
    level: str          # error | warning | info
    material: str | None
    detail: str

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    def line(self) -> str:
        where = f" {self.material}" if self.material else ""
        return f"{self.code}{where}: {self.detail}"


@dataclasses.dataclass(frozen=True)
class Built:
    record: dict
    text: str
    warnings: tuple[Finding, ...]
    projection: dict


# Authored fields ------------------------------------------------------------------------------------------------


def _unsafe(text: str) -> bool:
    return any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127
               or unicodedata.category(c) in ("Cf", "Zl", "Zp") for c in text)


def _id(value: object, what: str) -> str:
    try:
        return check_id(value, what)
    except DesignError as exc:
        raise _invalid(str(exc).partition(": ")[2]) from None


def _line(value: object, what: str, limit: int = LINE, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    text = " ".join(value.split()) if isinstance(value, str) else None
    if text is None or (not text and not optional) or len(text) > limit or _unsafe(text):
        raise _invalid(f"{what} must be text of up to {limit} characters without control characters")
    return text


def _lines(value: object, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > LIST:
        raise _invalid(f"{what} must be a list of up to {LIST} lines")
    return [_line(item, what) for item in value]


def _digest(value: object, what: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise _invalid(f"{what} is a sha256:<hex> digest")
    return value


def _time(value: object, what: str, now: dt.datetime | None) -> str:
    moment = None
    if isinstance(value, str) and _ISO.fullmatch(value):
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment is None or (now is not None and moment > now + dt.timedelta(minutes=5)):
        raise _invalid(f"{what} is a UTC time that is not in the future")
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _narrative(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > NARRATIVE_LIMIT:
        raise _invalid(f"narrative is text of up to {NARRATIVE_LIMIT} bytes")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if _unsafe(text):
        raise _invalid("narrative holds control, format or line-separator characters")
    for line in text.split("\n"):
        if line.lstrip().casefold().startswith("<!-- oah") or line.lstrip().startswith(FENCE):
            raise _invalid("a narrative line starts with an oah marker or the materials fence; reword or indent it")
    return text


def _decision_inputs(value: object, ident: str) -> dict:
    if not isinstance(value, Mapping) or not set(value) <= set(INPUT_KEYS) or "need" not in value:
        raise _invalid(f"decision {ident}: decision_inputs has need and optionally constraints, platform, kind and "
                       "compatibility")
    return {"need": _line(value["need"], f"decision {ident} need", TEXT),
            "constraints": _lines(value.get("constraints"), f"decision {ident} constraints"),
            "platform": _lines(value.get("platform"), f"decision {ident} platform"),
            "kind": _line(value.get("kind"), f"decision {ident} kind", 64, optional=True),
            "compatibility": _lines(value.get("compatibility"), f"decision {ident} compatibility")}


def decision_inputs_digest(inputs: Mapping) -> str:
    return digest_of(dict(inputs))


def _evidence(value: object, what: str, status: str, now: dt.datetime | None) -> list[dict]:
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > 8:
        raise _invalid(f"{what}.evidence must be a list of up to 8 runs")
    runs = []
    for run in value:
        if not isinstance(run, Mapping) or not set(run) <= {"environment", "command", "exit", "at", "output_sha256"}:
            raise _invalid(f"{what}.evidence entries are objects with environment, command, exit, at, output_sha256")
        exit_code, output = run.get("exit"), run.get("output_sha256")
        if type(exit_code) is not int or not isinstance(output, str) or not _HEX.fullmatch(output):
            raise _invalid(f"{what}.evidence needs exit (a whole number) and output_sha256 (64 hex)")
        runs.append({"environment": _line(run.get("environment"), f"{what}.evidence.environment"),
                     "command": _line(run.get("command"), f"{what}.evidence.command", 500), "exit": exit_code,
                     "at": _time(run.get("at"), f"{what}.evidence.at", now), "output_sha256": output})
    if status == "tested" and (not runs or any(run["exit"] != 0 for run in runs)):
        raise _invalid(f"{what}: tested needs at least one recorded run, and every run must exit 0")
    if status == "incompatible" and not runs:
        raise _invalid(f"{what}: incompatible needs the recorded run that showed it")
    return runs


def _integration(value: object, what: str, now: dt.datetime | None) -> dict:
    value = value or {}
    if not isinstance(value, Mapping) or not set(value) <= {"method", "status", "evidence", "claim"}:
        raise _invalid(f"{what}.integration is an object with method, status and evidence")
    method, status = value.get("method", "dependency"), value.get("status", "untested")
    if method not in METHODS or status not in INTEGRATION or value.get("claim", "reported") != "reported":
        raise _invalid(f"{what}.integration: method is one of {', '.join(METHODS)}, status one of "
                       f"{', '.join(INTEGRATION)}")
    return {"method": method, "status": status, "evidence": _evidence(value.get("evidence"), what, status, now),
            "claim": "reported"}


def _package(value: object, what: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not set(value) <= {"ecosystem", "name", "version", "registry"} \
            or value.get("registry", "unverified") != "unverified":
        raise _invalid(f"{what}.package is an object with ecosystem, name and version")
    return {"ecosystem": _line(value.get("ecosystem"), f"{what}.package.ecosystem", 40),
            "name": _line(value.get("name"), f"{what}.package.name"),
            "version": _line(value.get("version"), f"{what}.package.version", 64), "registry": "unverified"}


def _stack(item: object, materials: Mapping, now: dt.datetime | None) -> dict:
    if not isinstance(item, Mapping) or not set(item) <= {"id", "languages", "materials", "compatibility", "evidence",
                                                          "learning_cost", "operating_cost", "reason", "chosen"}:
        raise _invalid("each stack is an object with id, languages, materials, compatibility, evidence, "
                       "learning_cost, operating_cost, reason and chosen")
    ident = _id(item.get("id"), "stack")
    languages = item.get("languages") or []
    if not isinstance(languages, list) or not 1 <= len(languages) <= 16:
        raise _invalid(f"stack {ident}: languages lists the 1 to 16 languages it uses")
    names = item.get("materials") or []
    if not isinstance(names, list) or any(_id(name, "material") not in materials for name in names):
        raise _invalid(f"stack {ident}: every material it names must be in the materials")
    compatibility = item.get("compatibility", "untested")
    if compatibility not in INTEGRATION:
        raise _invalid(f"stack {ident}: compatibility is one of {', '.join(INTEGRATION)}")
    chosen = item.get("chosen", False)
    if type(chosen) is not bool:
        raise _invalid(f"stack {ident}: chosen is true or false")
    return {"id": ident, "languages": [_line(name, f"stack {ident} language", 40) for name in languages],
            "materials": sorted(set(names)), "compatibility": compatibility,
            "evidence": _evidence(item.get("evidence"), f"stack {ident}", compatibility, now),
            "learning_cost": _line(item.get("learning_cost"), f"stack {ident} learning_cost", optional=True),
            "operating_cost": _line(item.get("operating_cost"), f"stack {ident} operating_cost", optional=True),
            "reason": _line(item.get("reason"), f"stack {ident} reason", TEXT, optional=True), "chosen": chosen}


def _ref(value: object, what: str) -> dict:
    if not isinstance(value, Mapping) or set(value) != REF_KEYS or type(value.get("revision")) is not int \
            or value["revision"] < 1:
        raise _invalid(f"{what} is a design reference with project, uid, design, revision and digest")
    return {"project": _id(value["project"], "project"), "uid": _line(value["uid"], f"{what} uid", 80),
            "design": _id(value["design"], "design"), "revision": value["revision"],
            "digest": _digest(value["digest"], f"{what} digest")}


def _dependency(value: object, what: str) -> dict:
    if not isinstance(value, Mapping) or set(value) != set(DEPENDENCY_KEYS):
        raise _invalid(f"{what} has {', '.join(DEPENDENCY_KEYS)}")
    return {"material": _id(value["material"], "material"), "source": _id(value["source"], "source"),
            "spec_digest": _digest(value["spec_digest"], f"{what} spec_digest"),
            "snapshot": _digest(value["snapshot"], f"{what} snapshot"),
            "registered_spec": _digest(value["registered_spec"], f"{what} registered_spec", optional=True),
            "head": _digest(value["head"], f"{what} head", optional=True)}


def _resolution(value: object, now: dt.datetime | None) -> dict:
    if not isinstance(value, Mapping) or set(value) != RESOLUTION_KEYS:
        raise _invalid(f"a resolution has exactly {', '.join(sorted(RESOLUTION_KEYS))}")
    if value["kind"] not in RESOLUTION_KINDS:
        raise _invalid("a resolution kind is carry or reselected")
    refs = value["old_and_new_design_refs"]
    if not isinstance(refs, Mapping) or set(refs) != {"old", "new"}:
        raise _invalid("old_and_new_design_refs has old and new design references")
    dependencies = value["source_dependencies"]
    if not isinstance(dependencies, list) or len(dependencies) > 64:
        raise _invalid("source_dependencies is a list")
    deps = sorted((_dependency(item, "a source dependency") for item in dependencies),
                  key=lambda item: (item["material"], item["source"]))
    return {"kind": value["kind"], "decision_id": _id(value["decision_id"], "decision"),
            "from_basis": _digest(value["from_basis"], "from_basis"), "to_basis": _digest(value["to_basis"], "to_basis"),
            "old_and_new_design_refs": {"old": _ref(refs["old"], "the old design"),
                                        "new": _ref(refs["new"], "the new design")},
            "source_dependencies": deps, "policy_digest": _digest(value["policy_digest"], "policy_digest"),
            "reason": _line(value["reason"], "a resolution reason", TEXT), "actor": _line(value["actor"], "actor"),
            "at": _time(value["at"], "a resolution time", now)}


def _basis_shape(value: object, what: str) -> dict:
    if not isinstance(value, Mapping) or set(value) != BASIS_KEYS or value.get("version") != 1 \
            or not isinstance(value.get("parts"), Mapping) or not isinstance(value.get("title"), str):
        raise _invalid(f"{what} is not a version-1 material input basis")
    parts = {}
    for name, digest in value["parts"].items():
        if not isinstance(name, str) or name == MATERIALS_PART:
            raise _invalid(f"{what} names the materials part or a malformed part")
        parts[name] = _digest(digest, f"{what} part {name}")
    return {"version": 1, "project": _id(value["project"], "project"), "uid": _line(value["uid"], f"{what} uid", 80),
            "design": _id(value["design"], "design"), "title": value["title"], "parts": dict(sorted(parts.items())),
            "decision_inputs_digest": _digest(value["decision_inputs_digest"], f"{what} decision_inputs_digest")}


# Input basis ----------------------------------------------------------------------------------------------------


def input_basis(view: DesignView, decision_inputs: Mapping, *, which: str = "current", title: str | None = None,
                parts: Mapping[str, str] | None = None) -> dict | None:
    """MaterialInputBasisV1 of one decision against the current (working) or accepted snapshot of `view`.

    `parts`/`title` give the caller's proposed non-materials part digests before a combined save; the materials part
    is never included, so a materials-only save leaves the basis equal. None when `which` has no snapshot."""
    snapshot = view.current if which == "current" else view.accepted
    if snapshot is None:
        return None
    digests = snapshot.parts if parts is None else parts
    return {"version": 1, "project": view.project, "uid": view.uid, "design": view.design,
            "title": snapshot.title if title is None else title,
            "parts": {name: digests[name] for name in sorted(digests) if name != MATERIALS_PART},
            "decision_inputs_digest": decision_inputs_digest(decision_inputs)}


# Facts from verified evidence -----------------------------------------------------------------------------------


def _policy_digest(allow: Sequence[str]) -> str:
    from oah.design.sources.check import policy_digest_of

    return policy_digest_of(allow)


def _facts(evidence: portable.Evidence, allow: Sequence[str]) -> dict:
    """Every identity, license and time fact of a material, derived from a verified envelope only."""
    manifest, spec = evidence.manifest, evidence.spec
    observed = manifest.get("observed") or {}
    document = manifest.get("document")
    release_info = observed.get("release") or {}
    if document:
        reviewed = {key: document.get(key) for key in ("url", "final_url", "content_type", "bytes", "sha256",
                                                       "text_sha256", "text_bytes")}
    else:
        reviewed = {"commit": observed.get("commit"), "ref": observed.get("ref"), "tree": release_info.get("tree"),
                    "tag_object": release_info.get("tag_object"),
                    "files": [{key: item.get(key) for key in ("path", "mode", "blob", "size", "sha256")}
                              for item in manifest.get("files") or []]}
    release = None
    if release_info:
        record = evidence.release_observation or {}
        release = {"id": release_info.get("id"), "tag": release_info.get("tag"),
                   "tag_object": release_info.get("tag_object"), "commit": release_info.get("commit"),
                   "tree": release_info.get("tree"), "channel": spec.get("channel"),
                   "prerelease": release_info.get("prerelease"), "created_at": record.get("created_at"),
                   "published_at": release_info.get("published_at"), "updated_at": record.get("updated_at"),
                   "html_url": record.get("html_url"), "metadata_digest": release_info.get("metadata_digest"),
                   "release_observation": release_info.get("release_observation"),
                   "notes": release_info.get("notes"), "selection": release_info.get("selection"),
                   "limitations": sorted(manifest.get("notices") or [])}
    license_record = None
    if not document:
        verdict = license_module.verdict(manifest.get("license"), evidence.read)
        result = license_module.eligibility(verdict, allow)
        evidence_files = (manifest.get("license") or {})
        strict = {item.get("path"): item for item in verdict.files}
        license_record = {
            "form": verdict.form, "expression": verdict.expression, "ids": list(verdict.ids),
            "alternatives": list(verdict.ids) if verdict.form == "choice" else [], "strict": verdict.strict,
            "eligibility": result.status, "usable_under": result.usable_under, "policy": list(result.policy),
            "policy_digest": _policy_digest(allow), "reason": result.reason,
            "evidence": [{"path": item["path"], "blob": item["blob"], "sha256": item["sha256"],
                          "spdx": (strict.get(item["path"]) or {}).get("spdx"),
                          "strict": bool((strict.get(item["path"]) or {}).get("strict"))}
                         for item in evidence_files.get("files") or []],
            "notices": [{"path": item["path"], "blob": item["blob"], "sha256": item["sha256"]}
                        for item in evidence_files.get("notices") or []],
            "declared": evidence_files.get("declared"), "scopes": list(evidence_files.get("scopes") or []),
            "problems": list(verdict.problems)}
    observation = evidence.observation
    checked = {"observation": None if observation is None else evidence_observation_digest(evidence),
               "attempted_at": None if observation is None else observation["attempted_at"],
               "observed_at": None if observation is None else observation["observed_at"],
               "verified_at": None if observation is None else observation["verified_at"],
               "confirmed_at": None if observation is None else observation["confirmed_at"],
               "historical": "verified" if manifest.get("verified") and observation is not None else "unverified"}
    return {"source": {"id": evidence.source_id, **dict(spec)}, "spec_digest": evidence.spec_digest,
            "snapshot": evidence.snapshot, "observation": "verified" if manifest.get("verified") else "unverified",
            "reviewed": reviewed, "release": release, "license": license_record, "checked": checked,
            "portable_evidence_id": evidence.ident}


def evidence_observation_digest(evidence: portable.Evidence) -> str | None:
    from oah.design.sources.state import record_digest

    return None if evidence.observation is None else record_digest(evidence.observation)


def _identity_view(material: Mapping) -> dict:
    """The part of a material that must equal what its envelope derives (policy-dependent fields aside)."""
    license_record = material.get("license")
    if license_record is not None:
        license_record = {key: value for key, value in license_record.items() if key not in LICENSE_POLICY_KEYS}
    checked = {key: value for key, value in (material.get("checked") or {}).items() if key != "freshness_when_recorded"}
    return {"source": material.get("source"), "spec_digest": material.get("spec_digest"),
            "snapshot": material.get("snapshot"), "observation": material.get("observation"),
            "reviewed": material.get("reviewed"), "release": material.get("release"), "license": license_record,
            "checked": checked, "portable_evidence_id": material.get("portable_evidence_id")}


# Build ----------------------------------------------------------------------------------------------------------


def _heads(sources, materials: Mapping[str, Mapping]) -> dict[str, tuple[str | None, str | None]]:
    """source id -> (registered spec digest, verified head snapshot), both None when unregistered or unreadable."""
    out: dict[str, tuple[str | None, str | None]] = {}
    if sources is None:
        return out
    try:
        registry, _raw = sources.registry()
    except (SourceError, StateProblem):
        return out
    for material in materials.values():
        if material["source"].get("provider") == "authored":
            continue
        source_id = material["source"]["id"]
        spec = registry.sources.get(source_id)
        if spec is None:
            out[source_id] = (None, None)
            continue
        try:
            head = sources.head(spec)
        except StateProblem:
            head = None
        out[source_id] = (spec.spec_digest, None if head is None or head.verified is None
                          else head.verified.snapshot)
    return out


def _dependencies(decision: Mapping, materials: Mapping[str, Mapping], heads: Mapping) -> list[dict]:
    chosen = decision.get("chosen")
    if chosen is None or chosen not in materials:
        return []
    material = materials[chosen]
    registered, head = heads.get(material["source"]["id"], (None, None))
    return [{"material": chosen, "source": material["source"]["id"], "spec_digest": material["spec_digest"],
             "snapshot": material["snapshot"], "registered_spec": registered, "head": head}]


def _previous(view: DesignView) -> dict | None:
    text = view.current.texts.get(MATERIALS_PART)
    if text is None:
        return None
    try:
        record = parse(text)
    except MaterialsError:
        return None
    return record if record.get("version") == SCHEMA else None


def _authored_source(value: Mapping, ident: str, now: dt.datetime | None) -> dict:
    keys = {"kind", "origin", "revision", "author", "at", "limitations", "evidence", "text"}
    if set(value) != keys or value.get("kind") not in ("authored", "local-unverified"):
        raise _invalid(f"material {ident}: an authored source has exactly {', '.join(sorted(keys))}")
    limitations = _lines(value["limitations"], "authored source limitations")
    text = _narrative(value["text"])
    if not limitations or not text:
        raise _invalid(f"material {ident}: an authored source needs substantive inline text and limitations")
    return {"id": ident, "provider": "authored", "kind": value["kind"],
            "locator": _line(value["origin"], "authored source origin", TEXT),
            "revision": _line(value["revision"], "authored source revision", optional=True),
            "author": _line(value["author"], "authored source author", optional=True),
            "at": None if value["at"] is None else _time(value["at"], "authored source time", now),
            "limitations": limitations, "evidence": _lines(value["evidence"], "authored source evidence"),
            "text": text}


def _authored_facts(source: Mapping) -> dict:
    return {"source": dict(source), "spec_digest": digest_of({key: value for key, value in source.items()
                                                            if key != "text"}),
            "snapshot": text_digest(source["text"]), "observation": "unverified", "reviewed": None,
            "release": None, "license": None, "portable_evidence_id": None,
            "checked": {"observation": None, "attempted_at": None, "observed_at": None, "verified_at": None,
                        "confirmed_at": None, "historical": "unverified", "freshness_when_recorded": "unknown"}}


def build(selection: Mapping, sources, *, view: DesignView, allow_unverified: bool = False,
          now: dt.datetime | None = None, title: str | None = None,
          parts: Mapping[str, str] | None = None) -> Built:
    """Record, rendered text, warnings and projection manifest. Identity, license and time facts come only from
    verified snapshots; a decision recorded for other inputs needs an explicit carry or reselected resolution."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(selection, Mapping):
        raise _invalid("the selection must be a JSON object with decisions, decision_inputs, materials and stacks")
    unknown = sorted(set(selection) - SELECTION_KEYS)
    if unknown:
        raise _invalid(f"the selection has unknown fields {', '.join(unknown)}")
    warnings: list[Finding] = []
    lang = selection.get("lang", "en")
    if lang not in LABELS:
        raise _invalid("lang is en or ko")
    narrative = _narrative(selection.get("narrative"))
    inputs_in = selection.get("decision_inputs") or {}
    decisions_in = selection.get("decisions") or []
    materials_in = selection.get("materials") or []
    stacks_in = selection.get("stacks") or []
    resolutions_in = selection.get("resolutions") or []
    if not isinstance(inputs_in, Mapping) or not all(isinstance(item, list) for item in
                                                      (decisions_in, materials_in, stacks_in, resolutions_in)):
        raise _invalid("decision_inputs is an object; decisions, materials, stacks and resolutions are lists")
    if len(decisions_in) > 64 or len(materials_in) > 64 or len(stacks_in) > 16 or len(resolutions_in) > 64:
        raise _invalid("at most 64 decisions, 64 materials, 16 stacks and 64 resolutions")
    allow = list(sources.settings["license_allow"])
    policy = _policy_digest(allow)
    previous = _previous(view)
    if view.current.texts.get(MATERIALS_PART) is not None and previous is None:
        warnings.append(Finding("OAH_MATERIALS_INPUT_IGNORED", "warning", None, "the saved materials part is not a "
                                "valid version-2 record; its decisions are recorded fresh"))
    previous_decisions = {item["id"]: item for item in (previous or {}).get("decisions", [])}
    previous_materials = {item["id"]: item for item in (previous or {}).get("materials", [])}
    registry, _raw = sources.registry()
    materials: dict[str, dict] = {}
    envelopes: dict[str, dict] = {}
    for item in materials_in:
        if not isinstance(item, Mapping):
            raise _invalid("each material is an object")
        ident = _id(item.get("id"), "material")
        if ident in materials:
            raise _invalid(f"material {ident} appears twice")
        for key in sorted(set(item) & set(IGNORED_INPUT)):
            warnings.append(Finding("OAH_MATERIALS_INPUT_IGNORED", "warning", ident,
                                    f"{key} comes from the verified snapshot, not from the input"))
        extra = sorted(set(item) - set(IGNORED_INPUT) - {"id", "source", "snapshot", "choice", "decision", "package",
                                                         "integration", "services", "unverified", "notes"})
        if extra:
            raise _invalid(f"material {ident} has unknown fields {', '.join(extra)}")
        spec, status, evidence = None, None, None
        if isinstance(item.get("source"), Mapping):
            if not allow_unverified or item.get("snapshot") is not None:
                raise _invalid(f"material {ident}: authored sources require --allow-unverified and no collector snapshot")
            facts = _authored_facts(_authored_source(item["source"], ident, now))
        else:
            source_id = _id(item.get("source"), "source")
            spec = registry.sources.get(source_id)
            if spec is None:
                raise _invalid(f"material {ident}: source {source_id} is not registered here; oah sources register it "
                               "first")
            status = sources.status(spec)
            if status.problem is not None:
                raise _invalid(f"material {ident}: the state of source {source_id} cannot be read ({status.problem})")
            wanted = item.get("snapshot")
            if wanted is not None:
                digest = _digest(wanted, f"material {ident} snapshot")
            elif status.head is not None and status.head.verified is not None:
                digest = status.head.verified.snapshot
            elif allow_unverified and status.head is not None and status.head.discovered is not None:
                digest = status.head.discovered.snapshot
            else:
                raise _invalid(f"material {ident}: source {source_id} has no verified snapshot yet; run oah sources check "
                               f"{source_id}, or pass --allow-unverified to record an unverified observation")
            try:
                envelope = sources.portable(spec, digest)
                evidence = portable.verify(envelope)
            except (SourceError, portable.PortableError) as exc:
                raise MaterialsError(getattr(exc, "code", "OAH_MATERIALS_INVALID"),
                                     f"material {ident}: {str(exc).partition(': ')[2]}") from None
            facts = _facts(evidence, allow)
        if facts["observation"] == "unverified" and not allow_unverified:
            raise _invalid(f"material {ident}: snapshot {facts['snapshot']} is an unverified observation; pass "
                           "--allow-unverified to record it as such")
        choice = item.get("choice", "candidate")
        if choice not in CHOICES:
            raise _invalid(f"material {ident}: choice is one of {', '.join(CHOICES)}")
        decision = None if item.get("decision") is None else _id(item.get("decision"), "decision")
        package = _package(item.get("package"), f"material {ident}")
        integration = _integration(item.get("integration"), f"material {ident}", now)
        unverified = []
        if evidence is None:
            unverified += facts["source"]["limitations"]
        elif facts["observation"] == "unverified":
            unverified.append("the snapshot is an unverified observation: "
                              + ", ".join(evidence.manifest.get("problems") or []))
        if package is not None:
            unverified.append("the published package was not compared with the source")
        if spec is not None and spec.provider == "github":
            unverified.append("transitive dependencies and their licenses were not reviewed")
        if integration["status"] == "untested":
            unverified.append("build and runtime compatibility were not tested")
        unverified += _lines(item.get("unverified"), f"material {ident} unverified")
        checked = dict(facts["checked"], freshness_when_recorded="unknown" if status is None else status.freshness)
        materials[ident] = {"id": ident, "decision": decision, "choice": choice, **facts, "checked": checked,
                            "package": package, "integration": integration,
                            "services": _lines(item.get("services"), f"material {ident} services"),
                            "unverified": unverified,
                            "notes": _line(item.get("notes"), f"material {ident} notes", TEXT, optional=True)}
        if evidence is not None:
            envelopes[evidence.ident] = envelope
    decision_inputs: dict[str, dict] = {}
    for key, value in inputs_in.items():
        ident = _id(key, "decision")
        decision_inputs[ident] = _decision_inputs(value, ident)
    resolutions: dict[str, list[dict]] = {}
    for item in resolutions_in:
        resolution = _resolution(item, now)
        resolutions.setdefault(resolution["decision_id"], []).append(resolution)
    heads = _heads(sources, materials)
    decisions: dict[str, dict] = {}
    for item in decisions_in:
        if not isinstance(item, Mapping) or not set(item) <= {"id", "need", "status", "chosen", "alternatives",
                                                              "reason", "unverified", "based_on"}:
            raise _invalid("each decision is an object with id, status, chosen, alternatives, reason and unverified")
        ident = _id(item.get("id"), "decision")
        if ident in decisions:
            raise _invalid(f"decision {ident} appears twice")
        if ident not in decision_inputs:
            raise _invalid(f"decision {ident} has no decision_inputs entry (need, constraints, platform, kind, "
                           "compatibility)")
        if "based_on" in item:
            warnings.append(Finding("OAH_MATERIALS_INPUT_IGNORED", "warning", None,
                                    f"decision {ident}: based_on is filled from the design, not the input"))
        need = decision_inputs[ident]["need"]
        if item.get("need") is not None and _line(item.get("need"), f"decision {ident} need", TEXT) != need:
            raise _invalid(f"decision {ident}: need differs from its decision_inputs need")
        status = item.get("status", "open")
        if status not in DECISION_STATUS:
            raise _invalid(f"decision {ident}: status is one of {', '.join(DECISION_STATUS)}")
        decisions[ident] = {"id": ident, "need": need, "status": status,
                            "chosen": None if item.get("chosen") is None else _id(item.get("chosen"), "material"),
                            "alternatives": sorted({_id(alt, "material") for alt in item.get("alternatives") or []}),
                            "reason": _line(item.get("reason"), f"decision {ident} reason", TEXT, optional=True),
                            "unverified": _lines(item.get("unverified"), f"decision {ident} unverified")}
    for ident in decision_inputs:
        if ident not in decisions:
            raise _invalid(f"decision_inputs names {ident}, which is not a decision")
    for ident in resolutions:
        if ident not in decisions:
            raise _invalid(f"a resolution names decision {ident}, which is not a decision")
    for ident, material in materials.items():
        decision = material["decision"]
        if decision is not None and decision not in decisions:
            raise _invalid(f"material {ident}: decision {decision} is not in the decisions")
        if material["choice"] == "selected" and decision is None:
            raise _invalid(f"material {ident}: a selected material names the decision it answers")
    for ident, decision in decisions.items():
        chosen = decision["chosen"]
        if decision["status"] == "selected":
            if chosen is None or chosen not in materials or materials[chosen]["decision"] != ident \
                    or materials[chosen]["choice"] != "selected":
                raise _invalid(f"decision {ident}: a selected decision names its chosen material, which is selected "
                               "for that decision")
        elif chosen is not None and chosen not in materials:
            raise _invalid(f"decision {ident}: chosen {chosen} is not in the materials")
        for alt in decision["alternatives"]:
            if alt not in materials:
                raise _invalid(f"decision {ident}: alternative {alt} is not in the materials")
        basis = input_basis(view, decision_inputs[ident], title=title, parts=parts)
        decision.update(_basis_history(ident, decision, basis, previous_decisions.get(ident),
                                       resolutions.get(ident, []), materials, previous_materials, heads, policy, view))
    stacks, seen = [], set()
    for item in stacks_in:
        stack = _stack(item, materials, now)
        if stack["id"] in seen:
            raise _invalid(f"stack {stack['id']} appears twice")
        seen.add(stack["id"])
        stacks.append(stack)
    record = {"version": SCHEMA, "lang": lang, "narrative": narrative,
              "decision_inputs": {key: decision_inputs[key] for key in sorted(decision_inputs)},
              "decisions": [_ordered(decisions[key]) for key in sorted(decisions)],
              "materials": [materials[key] for key in sorted(materials)],
              "stacks": sorted(stacks, key=lambda stack: stack["id"]),
              "portable_evidence": [envelopes[key] for key in sorted(envelopes)]}
    text = render(record)
    return Built(record, text, tuple(warnings), projection(text, record))


def _ordered(decision: Mapping) -> dict:
    return {key: decision[key] for key in sorted(DECISION_KEYS)}


def _basis_history(ident: str, decision: Mapping, basis: Mapping, previous: Mapping | None,
                   resolutions: Sequence[Mapping], materials: Mapping, previous_materials: Mapping, heads: Mapping,
                   policy: str, view: DesignView) -> dict:
    """input_basis, supported_bases, resolutions and based_on of one decision after this build."""
    current = digest_of(dict(basis))
    origin = view.current.ref.to_json()
    if previous is None:
        if resolutions:
            raise _invalid(f"decision {ident}: a resolution needs the decision recorded in the saved materials part")
        return {"input_basis": dict(basis), "supported_bases": [current], "resolutions": [], "based_on": origin}
    supported = list(previous["supported_bases"])
    history = list(previous["resolutions"])
    dependencies = _dependencies(decision, materials, heads)
    history_refs = {ref.revision: ref.digest for ref in view.history}
    for resolution in resolutions:
        if resolution["from_basis"] not in supported:
            raise _invalid(f"decision {ident}: from_basis is not one of the recorded supported bases")
        if resolution["to_basis"] != current:
            raise _invalid(f"decision {ident}: to_basis is not the decision's current input basis {current}")
        old, new = resolution["old_and_new_design_refs"]["old"], resolution["old_and_new_design_refs"]["new"]
        if new != view.current.ref.to_json():
            raise _invalid(f"decision {ident}: the new design reference is not the current revision")
        if old["uid"] != view.uid or old["design"] != view.design or history_refs.get(old["revision"]) != old["digest"]:
            raise _invalid(f"decision {ident}: the old design reference is not in this design's history")
        if resolution["source_dependencies"] != dependencies:
            raise _invalid(f"decision {ident}: source_dependencies differ from the chosen material's current source "
                           "identities")
        if resolution["policy_digest"] != policy:
            raise _invalid(f"decision {ident}: policy_digest is not the current license policy {policy}")
    reselected = [item for item in resolutions if item["kind"] == "reselected"]
    if reselected:
        return {"input_basis": dict(basis), "supported_bases": [current], "resolutions": history + list(resolutions),
                "based_on": origin}
    if resolutions:
        for name in {decision["chosen"], *decision["alternatives"]} - {None}:
            before, after = previous_materials.get(name), materials.get(name)
            if before is None or after is None or any(before.get(key) != after.get(key) for key in
                                                      ("spec_digest", "snapshot", "integration", "decision")):
                raise _invalid(f"decision {ident}: a carry keeps the chosen source bytes and integration claims; "
                               f"material {name} changed, so record a reselected resolution instead")
        if current not in supported:
            supported.append(current)
        return {"input_basis": dict(basis), "supported_bases": supported, "resolutions": history + list(resolutions),
                "based_on": previous["based_on"]}
    if current in supported:
        names = {previous.get("chosen"), *previous.get("alternatives", []),
                 decision.get("chosen"), *decision.get("alternatives", [])} - {None}
        changed = any(previous.get(key) != decision.get(key) for key in ("chosen", "alternatives", "status"))
        for name in names:
            before, after = previous_materials.get(name), materials.get(name)
            if before is None or after is None or any(before.get(key) != after.get(key) for key in
                                                      ("source", "spec_digest", "snapshot", "integration", "decision")):
                changed = True
            elif (before.get("license") or {}).get("policy_digest") != (after.get("license") or {}).get("policy_digest"):
                changed = True
        if changed:
            raise MaterialsError("OAH_MATERIALS_REVIEW_REQUIRED", f"decision {ident}: source, policy or selection "
                                 "changed; record an explicit carry or reselected resolution")
        return {"input_basis": dict(basis), "supported_bases": supported, "resolutions": history,
                "based_on": previous["based_on"]}
    raise MaterialsError("OAH_MATERIALS_REVIEW_REQUIRED", f"decision {ident} was recorded for other design inputs; "
                         "review the old and new design and record a carry or reselected resolution",
                         {"decision": ident, "current_basis": current, "supported_bases": supported})


# Render ---------------------------------------------------------------------------------------------------------


def _cell(text: object) -> str:
    return escape(str(text)).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _short(digest: str | None, size: int = 12) -> str:
    return "-" if not digest else digest.split(":", 1)[-1][:size]


def _license_cell(material: Mapping) -> str:
    record = material.get("license")
    if record is None:
        return "-"
    expression = record.get("expression") or "unknown"
    if record.get("form") == "choice":
        expression += " (choice)"
    status = record.get("eligibility")
    if status == "eligible-by-choice":
        status += f": usable under {record.get('usable_under')}"
    return f"{expression} · {status}"


def _revision_cell(material: Mapping) -> str:
    if material["source"].get("provider") == "authored":
        return f"{material['source']['kind']} · revision {material['source']['revision'] or 'unknown'}"
    release, reviewed = material.get("release"), material.get("reviewed") or {}
    if release:
        kind = "prerelease" if release.get("prerelease") else "stable"
        return f"{release.get('tag')} ({kind}) · commit {_short(release.get('commit'))}"
    if reviewed.get("commit"):
        return f"commit {_short(reviewed.get('commit'))}"
    return f"text {_short(reviewed.get('text_sha256'))}"


def _ref_text(ref: Mapping | None) -> str:
    if not ref:
        return "-"
    return f"{ref['project']}/{ref['design']} r{ref['revision']} {ref['digest']} (uid {ref['uid']})"


def _join(items: Sequence[str]) -> str:
    return "; ".join(items) if items else "-"


def render(record: Mapping) -> str:
    """Deterministic Markdown from every semantic field, ending in the one compact JSON data block.

    OAH_MATERIALS_EVIDENCE_TOO_LARGE when the whole normalized part would not stay under the design store's strict
    1 MiB bound."""
    labels = LABELS["ko" if record.get("lang") == "ko" else "en"]
    out = [f"# {labels['title']}", "", labels["intro"], ""]
    if record.get("narrative"):
        out += [f"## {labels['narrative']}", "", record["narrative"], ""]
    materials = {item["id"]: item for item in record.get("materials", [])}
    inputs = record.get("decision_inputs") or {}
    used = set()

    def table(rows: Sequence[Mapping]) -> None:
        header = labels["table"]
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "---|" * len(header))
        for material in rows:
            checked = material.get("checked") or {}
            when = (checked.get("confirmed_at") or checked.get("verified_at") or "-")[:10]
            integration = material.get("integration") or {}
            out.append("| " + " | ".join(_cell(value) for value in (
                material["id"], material["choice"], _revision_cell(material), _license_cell(material),
                f"{integration.get('method')} · {integration.get('status')}",
                f"{when} · {checked.get('historical')} · {material.get('observation')}")) + " |")
        out.append("")

    for decision in record.get("decisions", []):
        ident = decision["id"]
        out += [f"## {labels['decision']} {ident}: {decision['need']}", ""]
        out.append(f"{labels['status']}: {decision['status']}. {labels['chosen']}: "
                   f"{decision.get('chosen') or labels['none']}. {labels['alternatives']}: "
                   f"{', '.join(decision.get('alternatives') or []) or '-'}.")
        if decision.get("reason"):
            out.append(f"{labels['reason']}: {decision['reason']}")
        out.append("")
        given = inputs.get(ident) or {}
        out += [f"{labels['inputs']}:", f"- {labels['constraints']}: {_join(given.get('constraints') or [])}",
                f"- {labels['platform']}: {_join(given.get('platform') or [])}",
                f"- {labels['kind']}: {given.get('kind') or '-'}",
                f"- {labels['compatibility']}: {_join(given.get('compatibility') or [])}", ""]
        basis = decision["input_basis"]
        parts = ", ".join(f"{name} {digest}" for name, digest in basis["parts"].items()) or "-"
        out.append(f"- {labels['based_on']}: {_ref_text(decision.get('based_on'))}")
        out.append(f"- {labels['basis']}: {digest_of(basis)} (project {basis['project']} uid {basis['uid']}, "
                   f"design {basis['design']}, title \"{_cell(basis['title'])}\", parts {parts}, decision inputs "
                   f"{basis['decision_inputs_digest']})")
        out.append(f"- {labels['supported']}: {', '.join(decision['supported_bases'])}")
        for resolution in decision.get("resolutions") or []:
            refs = resolution["old_and_new_design_refs"]
            out.append(f"- {labels['resolutions']}: {resolution['kind']} {resolution['from_basis']} -> "
                       f"{resolution['to_basis']} by {resolution['actor']} at {resolution['at']}: "
                       f"{resolution['reason']}")
            out.append(f"  - old {_ref_text(refs['old'])}; new {_ref_text(refs['new'])}; policy "
                       f"{resolution['policy_digest']}")
            for item in resolution["source_dependencies"]:
                out.append(f"  - {item['material']} {item['source']} spec {item['spec_digest']} snapshot "
                           f"{item['snapshot']} registered {item['registered_spec'] or '-'} head {item['head'] or '-'}")
        out.append("")
        rows = [item for item in materials.values() if item.get("decision") == ident]
        used.update(item["id"] for item in rows)
        if rows:
            table(rows)
        if decision.get("unverified"):
            out.append(f"{labels['unverified']}:")
            out += [f"- {line}" for line in decision["unverified"]]
            out.append("")
    rest = [item for key, item in materials.items() if key not in used]
    if rest:
        out += [f"## {labels['other']}", ""]
        table(rest)
    for material in materials.values():
        out += _material_lines(material, labels)
    if record.get("stacks"):
        out += [f"## {labels['stacks']}", "", "| " + " | ".join(labels["stack_table"]) + " |",
                "|" + "---|" * len(labels["stack_table"])]
        for stack in record["stacks"]:
            out.append("| " + " | ".join(_cell(value) for value in (
                stack["id"], ", ".join(stack["languages"]), ", ".join(stack["materials"]) or "-",
                stack["compatibility"], stack.get("learning_cost") or "-", stack.get("operating_cost") or "-",
                labels["yes"] if stack.get("chosen") else labels["no"])) + " |")
        out.append("")
        for stack in record["stacks"]:
            if stack.get("reason"):
                out.append(f"- {stack['id']}: {stack['reason']}")
            for run in stack.get("evidence") or []:
                out.append(f"  - {stack['id']} {run['at']} `{run['command']}` exit {run['exit']} on "
                           f"{run['environment']} output sha256 {run['output_sha256']} ({labels['reported']})")
        out.append("")
    for envelope in record.get("portable_evidence", []):
        out += _envelope_lines(envelope, labels)
    data = canonical_json(record)
    out += [f"## {labels['data']}", "", FENCE, data, "```", ""]
    text = "\n".join(out).rstrip("\n") + "\n"
    measured = len(text.encode("utf-8"))
    if measured >= MAX_BYTES:
        raise MaterialsError("OAH_MATERIALS_EVIDENCE_TOO_LARGE", f"the materials part would be {measured} bytes; a "
                             f"design part must stay under {MAX_BYTES} bytes, so narrow the selected paths or split the "
                             "decisions across designs", _size_report(record, measured))
    return text


def canonical_json(record: Mapping) -> str:
    """Sorted-key compact JSON, ASCII-escaped so external text never reaches a viewer raw."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _material_lines(material: Mapping, labels: Mapping) -> list[str]:
    out = [f"### {material['id']}", ""]
    source = material["source"]
    scope = [source.get("track") or "-"]
    if source.get("tag_prefix"):
        scope.append(f"tag prefix {source['tag_prefix']}")
    if source.get("paths"):
        scope.append(f"paths {', '.join(source['paths'])}")
    if source.get("provider") == "authored":
        out += [f"- {labels['source']}: {source['kind']} ({source['locator']}); unverified, license unknown",
                f"- Provenance: author {source['author'] or 'unknown'}, revision {source['revision'] or 'unknown'}, "
                f"time {source['at'] or 'unknown'}; evidence {_join(source['evidence'])}",
                f"- Limitations: {_join(source['limitations'])}", "", source["text"], ""]
    else:
        out.append(f"- {labels['source']}: {source['id']} {source['locator']} ({', '.join(scope)}; channel "
               f"{source.get('channel')}, ref {source.get('ref') or '-'}, release order {source.get('release_order') or '-'}"
               f", selection {source.get('selection_policy') or '-'}, verification {source.get('verification_policy')})"
               f", spec {material['spec_digest']}, snapshot {material['snapshot']} ({material['observation']})")
    out.append(f"- {labels['evidence']}: {material['portable_evidence_id'] or 'none (unverified)'}")
    reviewed = material.get("reviewed") or {}
    if "files" in reviewed:
        out.append(f"- {labels['reviewed']}: commit {reviewed.get('commit') or '-'}, ref {reviewed.get('ref') or '-'}, "
                   f"tree {reviewed.get('tree') or '-'}, tag object {reviewed.get('tag_object') or '-'}")
        for item in reviewed["files"]:
            out.append(f"  - {escape(item['path'])} mode {item['mode']} blob {item['blob']} size {item['size']} "
                       f"sha256 {item['sha256']} ({labels['read']}: {material['portable_evidence_id']} "
                       f"sha256:{item['sha256']})")
    elif source.get("provider") != "authored":
        out.append(f"- {labels['reviewed']}: {escape(str(reviewed.get('final_url')))} ({reviewed.get('content_type')}, "
                   f"{reviewed.get('bytes')} bytes sha256 {reviewed.get('sha256')}; text {reviewed.get('text_bytes')} "
                   f"bytes sha256 {reviewed.get('text_sha256')}) ({labels['read']}: {material['portable_evidence_id']})")
    release = material.get("release")
    if release:
        notes = release.get("notes") or {}
        out.append(f"- {labels['release']}: {escape(str(release.get('tag')))} id {release.get('id')} ({release.get('channel')}"
                   f", prerelease {labels['yes'] if release.get('prerelease') else labels['no']}) commit "
                   f"{release.get('commit')} tag object {release.get('tag_object')} tree {release.get('tree')}; created "
                   f"{release.get('created_at')}, published {release.get('published_at') or '-'}, updated "
                   f"{release.get('updated_at') or '-'}; {escape(str(release.get('html_url')))}")
        out.append(f"  - metadata {release.get('metadata_digest')}, release observation "
                   f"{release.get('release_observation')}, selection {release.get('selection')}; notes "
                   + (f"{notes.get('bytes')} bytes sha256 {notes.get('sha256')} ({labels['read']}: "
                      f"{material['portable_evidence_id']} sha256:{notes.get('sha256')})" if notes else "none")
                   + (f"; limitations {', '.join(release['limitations'])}" if release.get("limitations") else ""))
    record = material.get("license")
    if record is not None:
        out.append(f"- {labels['license']}: {_license_cell(material)}; ids {', '.join(record.get('ids') or []) or '-'}"
                   f", alternatives {', '.join(record.get('alternatives') or []) or '-'}, strict "
                   f"{labels['yes'] if record.get('strict') else labels['no']}, declared "
                   f"{escape(str(record.get('declared') or '-'))}; policy {', '.join(record.get('policy') or []) or '-'} "
                   f"({record.get('policy_digest')}); {record.get('reason')}")
        for item in record.get("evidence") or []:
            out.append(f"  - {escape(item['path'])} blob {item['blob']} sha256 {item['sha256']} spdx "
                       f"{item.get('spdx') or '-'} strict {labels['yes'] if item.get('strict') else labels['no']} "
                       f"({labels['read']}: {material['portable_evidence_id']} sha256:{item['sha256']})")
        for item in record.get("notices") or []:
            out.append(f"  - notice {escape(item['path'])} blob {item['blob']} sha256 {item['sha256']} "
                       f"({labels['read']}: {material['portable_evidence_id']} sha256:{item['sha256']})")
        for scope_item in record.get("scopes") or []:
            out.append(f"  - scope {escape(str(scope_item.get('path')))}: {scope_item.get('level')} "
                       f"{', '.join(escape(str(name)) for name in scope_item.get('files') or []) or '-'}")
        for problem in record.get("problems") or []:
            out.append(f"  - {escape(str(problem))}")
    package = material.get("package")
    if package:
        out.append(f"- {labels['package']}: {package['ecosystem']} {package['name']} {package['version']} "
                   f"({labels['registry']})")
    integration = material.get("integration") or {}
    out.append(f"- {labels['integration']}: {integration.get('method')} · {integration.get('status')} "
               f"({labels['reported']})")
    for run in integration.get("evidence") or []:
        out.append(f"  - {run['at']} `{run['command']}` exit {run['exit']} on {run['environment']} output sha256 "
                   f"{run['output_sha256']}")
    if material.get("services"):
        out.append(f"- {labels['services']}: {', '.join(material['services'])}")
    checked = material.get("checked") or {}
    out.append(f"- {labels['checked']}: observation {checked.get('observation') or '-'}, attempted "
               f"{checked.get('attempted_at') or '-'}, observed {checked.get('observed_at') or '-'}, verified "
               f"{checked.get('verified_at') or '-'}, confirmed {checked.get('confirmed_at') or '-'} "
               f"({checked.get('historical')}; freshness when recorded {checked.get('freshness_when_recorded')})")
    if material.get("notes"):
        out.append(f"- {labels['notes']}: {material['notes']}")
    if material.get("unverified"):
        out.append(f"- {labels['unverified']}:")
        out += [f"  - {line}" for line in material["unverified"]]
    out.append("")
    return out


def _envelope_lines(envelope: Mapping, labels: Mapping) -> list[str]:
    source, snapshot, provenance = envelope["source"], envelope["snapshot"], envelope["provenance"]
    out = [f"### {labels['evidence']} {portable.envelope_id(envelope)}", "",
           f"- {labels['source']}: {source['id']} {source['locator']} spec {envelope['spec_digest']}, snapshot "
           f"{snapshot['digest']}",
           f"- {labels['provenance']}: {provenance['claim']}, registration {provenance['registration'] or '-'}, "
           f"observation {provenance['observation_digest'] or '-'}, release observation "
           f"{provenance['release_observation_digest'] or '-'}",
           f"- {labels['originals']}:"]
    for blob in envelope["blobs"]:
        out.append(f"  - {blob['kind']} {escape(blob['path_or_field'])}: {blob['bytes']} bytes, {blob['id']}")
    out.append("")
    return out


def _size_report(record: Mapping, measured: int) -> dict:
    rows = []
    for envelope in record.get("portable_evidence", []):
        blobs = envelope.get("blobs") or []
        rows.append({"envelope": portable.envelope_id(envelope), "source": envelope["source"]["id"],
                     "blobs": len(blobs), "original_bytes": sum(item["bytes"] for item in blobs),
                     "encoded_bytes": portable.payload_bytes(envelope)})
    rows.sort(key=lambda row: -row["encoded_bytes"])
    plan = [f"narrow the selected paths of source {row['source']} ({row['blobs']} originals, "
            f"{row['original_bytes']} bytes)" for row in rows[:5]]
    plan.append("or split the decisions across designs so each materials part stays under the bound")
    return {"measured_bytes": measured, "limit": MAX_BYTES, "complete": False, "evidence": rows, "plan": plan}


def projection(text: str, record: Mapping) -> dict:
    """The neutral projection manifest: part identity, semantic and original-payload byte counts per envelope."""
    stripped = dict(record, portable_evidence=[
        dict(envelope, blobs=[dict(blob, data="") for blob in envelope["blobs"]])
        for envelope in record.get("portable_evidence", [])])
    data = canonical_json(record)
    semantic = text.replace(data, canonical_json(stripped))
    semantic_bytes = semantic.encode("utf-8")
    return {"version": 1, "kind": "oah-materials-projection", "part_bytes": len(text.encode("utf-8")),
            "part_digest": text_digest(text), "semantic_bytes": len(semantic_bytes),
            "semantic_sha256": hashlib.sha256(semantic_bytes).hexdigest(),
            "original_payload_bytes": sum(portable.payload_bytes(item) for item in record.get("portable_evidence", [])),
            "envelopes": [{"id": portable.envelope_id(envelope), "source": envelope["source"]["id"],
                           "snapshot": envelope["snapshot"]["digest"],
                           "blobs": [{key: blob[key] for key in ("id", "kind", "path_or_field", "bytes", "sha256")}
                                     | {"encoded_bytes": len(blob["data"])} for blob in envelope["blobs"]]}
                          for envelope in record.get("portable_evidence", [])],
            "inline_budget": INLINE_BUDGET, "semantic_fits_inline": len(semantic_bytes) <= INLINE_BUDGET,
            "limit": MAX_BYTES}


# Parse ----------------------------------------------------------------------------------------------------------


def _data_block(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    starts = [index for index, line in enumerate(lines) if line == FENCE]
    if not starts:
        raise _invalid("the materials text has no ```oah-materials data block")
    start = starts[-1]
    if start + 2 >= len(lines) or lines[start + 2] != "```" or any(line.strip() for line in lines[start + 3:]):
        raise _invalid("the ```oah-materials data block is not one closed JSON line at the end of the part")
    return lines[start + 1]


def parse(text: str, *, material_errors: dict[str, str] | None = None) -> dict:
    """The record in a materials part (version 2, or a legacy version 1); OAH_MATERIALS_INVALID otherwise."""
    if not isinstance(text, str) or len(text.encode("utf-8")) >= MAX_BYTES:
        raise _invalid("the materials text must be under 1 MiB")
    try:
        record = loads(_data_block(text).encode("utf-8"), "the materials data block")
    except StateProblem as exc:
        raise _invalid(exc.detail) from None
    if isinstance(record, dict) and record.get("version") == 1:
        return _legacy(record)
    if not isinstance(record, dict) or record.get("version") != SCHEMA or set(record) != RECORD_KEYS:
        raise _invalid(f"the data block is not a version-{SCHEMA} materials record")
    _validate(record, material_errors=material_errors)
    return record


def _legacy(record: dict) -> dict:
    for key in ("decisions", "materials", "stacks"):
        if not isinstance(record.get(key), list):
            raise _invalid(f"the version-1 data block has no {key} list")
    for material in record["materials"]:
        if (not isinstance(material, dict) or not isinstance(material.get("source"), dict)
                or not isinstance(material.get("snapshot"), str) or not _DIGEST.fullmatch(material["snapshot"])
                or not isinstance(material.get("spec_digest"), str) or not _DIGEST.fullmatch(material["spec_digest"])):
            raise _invalid("a version-1 material has no source, snapshot or spec digest")
        _id(material.get("id"), "material")
        _id(material["source"].get("id"), "source")
    return record


def _same(stored: object, normalized: object, what: str) -> None:
    if stored != normalized:
        raise _invalid(f"{what} is not in its recorded form")


def _validate_material(material: Mapping, envelope_ids: list[str | None]) -> None:
    ident = material["id"]
    try:
        for field, schema in (("checked", shapes.CHECKED), ("reviewed", shapes.REVIEWED),
                              ("release", shapes.RELEASE), ("license", shapes.LICENSE)):
            shapes.validate(material[field], schema, f"material {ident}.{field}")
        source = material["source"]
        if not isinstance(source, dict):
            raise _invalid(f"material {ident}: source is an object")
        authored = source.get("provider") == "authored"
        if not authored:
            shapes.validate(source, shapes.SOURCE, f"material {ident}.source")
    except shapes.ShapeError as exc:
        raise _invalid(str(exc)) from None
    if material["choice"] not in CHOICES or material["observation"] not in ("verified", "unverified"):
        raise _invalid(f"material {ident}: choice or observation is not a known word")
    if material["decision"] is not None:
        _id(material["decision"], "decision")
    _same(material["integration"], _integration(material["integration"], f"material {ident}", None),
          f"material {ident} integration")
    _same(material["package"], _package(material["package"], f"material {ident}"), f"material {ident} package")
    _same(material["services"], _lines(material["services"], "services"), f"material {ident} services")
    _same(material["unverified"], _lines(material["unverified"], "unverified"), f"material {ident} unverified")
    _same(material["notes"], _line(material["notes"], "notes", TEXT, optional=True), f"material {ident} notes")
    if authored:
        normalized = _authored_source({"kind": source.get("kind"), "origin": source.get("locator"),
            **{key: source.get(key) for key in ("revision", "author", "at", "limitations", "evidence", "text")}},
            ident, None)
        _same(source, normalized, f"material {ident} authored source")
        _same(_identity_view(material), _identity_view(_authored_facts(normalized)),
              f"material {ident} authored facts")
    elif material["portable_evidence_id"] not in envelope_ids:
        raise _invalid(f"material {ident}: its portable evidence is not in the record")
    _id(source.get("id"), "source")
    _digest(material["spec_digest"], "spec_digest")
    _digest(material["snapshot"], "snapshot")


def _validate(record: Mapping, *, material_errors: dict[str, str] | None = None) -> None:
    """Closed shapes and referential integrity; authored fields must equal their own normalized form."""
    if record["lang"] not in LABELS:
        raise _invalid("lang is en or ko")
    _same(record["narrative"], _narrative(record["narrative"]), "narrative")
    inputs = record["decision_inputs"]
    if not isinstance(inputs, dict):
        raise _invalid("decision_inputs is an object")
    for key, value in inputs.items():
        _same(value, _decision_inputs(value, _id(key, "decision")), f"decision_inputs {key}")
    for key in ("decisions", "materials", "stacks", "portable_evidence"):
        if not isinstance(record[key], list):
            raise _invalid(f"{key} is a list")
    envelope_ids = [portable.envelope_id(item) if isinstance(item, Mapping) else None
                    for item in record["portable_evidence"]]
    if None in envelope_ids or envelope_ids != sorted(set(envelope_ids)):
        raise _invalid("portable_evidence is a sorted list of distinct envelopes")
    materials = {}
    invalid_materials = {}
    for material in record["materials"]:
        if not isinstance(material, dict) or set(material) != MATERIAL_KEYS:
            raise _invalid("a material does not have exactly the version-2 fields")
        ident = _id(material["id"], "material")
        if ident in materials:
            raise _invalid(f"material {ident} appears twice")
        materials[ident] = material
        try:
            _validate_material(material, envelope_ids)
        except MaterialsError as exc:
            if material_errors is None:
                raise
            invalid_materials[ident] = str(exc)
            material_errors[ident] = str(exc)
    decisions = {}
    for decision in record["decisions"]:
        if not isinstance(decision, dict) or set(decision) != DECISION_KEYS:
            raise _invalid("a decision does not have exactly the version-2 fields")
        ident = _id(decision["id"], "decision")
        if ident in decisions:
            raise _invalid(f"decision {ident} appears twice")
        decisions[ident] = decision
        if ident not in inputs or decision["need"] != inputs[ident]["need"]:
            raise _invalid(f"decision {ident}: its need differs from its decision inputs")
        if decision["status"] not in DECISION_STATUS:
            raise _invalid(f"decision {ident}: status is not a known word")
        _same(decision["reason"], _line(decision["reason"], "reason", TEXT, optional=True), f"decision {ident} reason")
        _same(decision["unverified"], _lines(decision["unverified"], "unverified"), f"decision {ident} unverified")
        _same(decision["based_on"], _ref(decision["based_on"], "based_on"), f"decision {ident} based_on")
        _same(decision["input_basis"], _basis_shape(decision["input_basis"], "input_basis"),
              f"decision {ident} input_basis")
        supported = decision["supported_bases"]
        if not isinstance(supported, list) or not supported or len(set(supported)) != len(supported):
            raise _invalid(f"decision {ident}: supported_bases is a list of distinct digests")
        for item in supported:
            _digest(item, "a supported basis")
        if not isinstance(decision["resolutions"], list):
            raise _invalid(f"decision {ident}: resolutions is a list")
        for item in decision["resolutions"]:
            _same(item, _resolution(item, None), f"decision {ident} resolution")
            if item["decision_id"] != ident:
                raise _invalid(f"decision {ident}: a resolution names another decision")
        alternatives = decision["alternatives"]
        if not isinstance(alternatives, list) or alternatives != sorted(set(alternatives)) \
                or any(alt not in materials for alt in alternatives):
            raise _invalid(f"decision {ident}: alternatives are sorted materials of this record")
        chosen = decision["chosen"]
        if chosen is not None and chosen not in materials:
            raise _invalid(f"decision {ident}: chosen is not a material of this record")
        if decision["status"] == "selected" and (chosen is None or (chosen not in invalid_materials
                and (materials[chosen]["decision"] != ident or materials[chosen]["choice"] != "selected"))):
            raise _invalid(f"decision {ident}: a selected decision names its selected material")
    if set(inputs) != set(decisions):
        raise _invalid("decision_inputs and decisions name different decisions")
    for ident, material in materials.items():
        if ident in invalid_materials:
            continue
        if material["decision"] is not None and material["decision"] not in decisions:
            raise _invalid(f"material {ident}: its decision is not in the record")
        if material["choice"] == "selected" and material["decision"] is None:
            raise _invalid(f"material {ident}: a selected material names its decision")
    seen = set()
    for stack in record["stacks"]:
        normalized = _stack(stack, materials, None)
        _same(stack, normalized, "a stack")
        if normalized["id"] in seen:
            raise _invalid(f"stack {normalized['id']} appears twice")
        seen.add(normalized["id"])
    if [item["id"] for item in record["materials"]] != sorted(materials) \
            or [item["id"] for item in record["decisions"]] != sorted(decisions) \
            or [item["id"] for item in record["stacks"]] != sorted(seen):
        raise _invalid("decisions, materials and stacks are sorted by id")


def refs_of(text: str) -> list[dict]:
    """What a design's materials reference: source ID, locator, spec digest and snapshot per material."""
    record = parse(text)
    refs = []
    for material in record["materials"]:
        if material["source"].get("provider") == "authored":
            continue
        ref = {"source": material["source"]["id"], "locator": material["source"].get("locator"),
               "spec_digest": material["spec_digest"], "snapshot": material["snapshot"]}
        if ref not in refs:
            refs.append(ref)
    return sorted(refs, key=lambda item: (item["source"], item["snapshot"]))


def resolution_template(text: str, sources, view: DesignView, decision_id: str, *, kind: str,
                        selection: Mapping | None = None) -> dict:
    """The exact identities of a carry or reselected resolution for the caller to review and complete.

    The caller fills reason, actor and at after it compared the old and new design bodies; build verifies these
    identities, never the semantic reason. With `selection`, the source dependency is the material that selection would
    choose (a reselection); otherwise it is the recorded chosen material (a carry keeps those bytes)."""
    if kind not in RESOLUTION_KINDS:
        raise _invalid("a resolution kind is carry or reselected")
    record = parse(text)
    if record.get("version") != SCHEMA:
        raise _invalid("a resolution needs a version-2 materials record")
    decision = next((item for item in record["decisions"] if item["id"] == decision_id), None)
    if decision is None:
        raise _invalid(f"decision {decision_id} is not in the materials record")
    materials = {item["id"]: item for item in record["materials"]}
    inputs = record["decision_inputs"][decision_id]
    if selection is not None:
        given = selection.get("decision_inputs", {}).get(decision_id)
        inputs = inputs if given is None else _decision_inputs(given, decision_id)
    heads = _heads(sources, materials)
    dependencies = _dependencies(decision, materials, heads)
    if selection is not None:
        chosen = next((item.get("chosen") for item in selection.get("decisions") or []
                       if isinstance(item, Mapping) and item.get("id") == decision_id), None)
        wanted = next((item for item in selection.get("materials") or []
                       if isinstance(item, Mapping) and item.get("id") == chosen), None)
        dependencies = []
        if wanted is not None and isinstance(wanted.get("source"), Mapping):
            facts = _authored_facts(_authored_source(wanted["source"], chosen, None))
            dependencies = [{"material": chosen, "source": chosen, "spec_digest": facts["spec_digest"],
                             "snapshot": facts["snapshot"], "registered_spec": None, "head": None}]
        elif wanted is not None:
            registry, _raw = sources.registry()
            spec = registry.sources.get(_id(wanted.get("source"), "source"))
            if spec is None:
                raise _invalid(f"material {chosen}: its source is not registered here")
            head = sources.head(spec)
            snapshot = wanted.get("snapshot") or (None if head is None or head.verified is None
                                                  else head.verified.snapshot)
            dependencies = [{"material": chosen, "source": spec.id, "spec_digest": spec.spec_digest,
                             "snapshot": snapshot, "registered_spec": spec.spec_digest,
                             "head": None if head is None or head.verified is None else head.verified.snapshot}]
    supported = decision["supported_bases"]
    accepted = input_basis(view, record["decision_inputs"][decision_id], which="accepted")
    old = (view.accepted.ref.to_json() if accepted is not None and digest_of(accepted) in supported
           else decision["based_on"])
    return {"kind": kind, "decision_id": decision_id, "from_basis": supported[-1],
            "to_basis": digest_of(input_basis(view, inputs)),
            "old_and_new_design_refs": {"old": old, "new": view.current.ref.to_json()},
            "source_dependencies": dependencies, "policy_digest": _policy_digest(sources.settings["license_allow"]),
            "reason": None, "actor": None, "at": None}


# Check ----------------------------------------------------------------------------------------------------------


def _worst(statuses: Sequence[str]) -> str:
    return min(statuses, key=INPUT_STATUSES.index) if statuses else "current"


def _chain_ok(decision: Mapping) -> bool:
    """supported_bases is exactly what the resolution history allows, and the latest basis is in it."""
    supported, history = decision["supported_bases"], decision["resolutions"]
    last = max((index for index, item in enumerate(history) if item["kind"] == "reselected"), default=-1)
    expected = [history[last]["to_basis"]] if last >= 0 else [supported[0]]
    for item in history[last + 1:]:
        if item["from_basis"] not in expected:
            return False
        if item["to_basis"] not in expected:
            expected.append(item["to_basis"])
    return expected == supported and digest_of(decision["input_basis"]) in supported


def check(text: str | None, sources=None, policy: Mapping | None = None, context: DesignView | None = None, *,
          unavailable: str | None = None) -> dict:
    """MaterialCheckV1 of one materials text against the checked design view, local evidence and current policy.

    Local only: verifies the embedded portable evidence, reads this clone's registry heads (no network, no lock, no
    save) and the design view the caller just read. `context=None` (with `unavailable=<code>`) keeps every decision
    unknown. Priority per decision: invalid, unknown, accepted-input stale, working-only needs-review, current;
    freshness, eligibility and integration are reported separately and never clear an input problem."""
    if policy is not None:
        allow = list(policy.get("license_allow") or [])
    elif sources is not None:
        allow = list(sources.settings["license_allow"])
    else:
        from oah.design.sources.check import DEFAULTS

        allow = list(DEFAULTS["license_allow"])
    policy_digest = _policy_digest(allow)
    view = context
    checked_view = {"accepted": None, "working": None, "authority_observation": {"status": "unavailable",
                                                                                 "code": unavailable}}
    if view is not None:
        checked_view = {"accepted": None if view.accepted is None else view.accepted.ref.to_json(),
                        "working": view.current.ref.to_json(),
                        "authority_observation": {"status": "read", "source": view.source,
                                                  "freshness": view.freshness, "page_state": view.page_state,
                                                  "recovery_pending": view.recovery_pending,
                                                  "acceptance": None if view.acceptance is None
                                                  else dict(view.acceptance)}}
    report = {"version": 1, "checked_view": checked_view, "materials_digest": None,
              "policy_digest": policy_digest, "source_reads": [], "decisions": [], "overall": "invalid",
              "problems": []}
    if text is None and view is None:
        report["overall"] = "unknown"
        report["problems"].append(f"authority-unavailable:{unavailable or 'unknown'}")
        return report
    try:
        normalized = normalize_text(text, "materials")
    except DesignError:
        report["problems"].append("materials-not-a-design-part")
        return report
    report["materials_digest"] = text_digest(normalized)
    material_errors: dict[str, str] = {}
    try:
        record = parse(normalized, material_errors=material_errors)
    except MaterialsError as exc:
        report["problems"].append(f"{exc.code}: {str(exc).partition(': ')[2]}")
        return report
    if record.get("version") == 1:
        report["decisions"] = [{"id": item.get("id"), "selection_status": item.get("status"),
                                "input_status": "unknown", "current_basis": None, "recorded_basis": None,
                                "execution_relevance": "selected" if item.get("status") == "selected"
                                else "not-selected", "problems": ["missing-input-basis"], "resolution_ref": None}
                               for item in record["decisions"] if isinstance(item, dict)]
        report["overall"] = "unknown"
        report["problems"].append("legacy-version-1-record")
        return report
    envelopes = {portable.envelope_id(item): item for item in record["portable_evidence"]}
    verified_envelopes, envelope_errors = {}, {}
    for key, envelope in envelopes.items():
        try:
            verified_envelopes[key] = portable.verify(envelope)
        except portable.PortableError as exc:
            envelope_errors[key] = f"portable-evidence-invalid: {str(exc).partition(': ')[2]}"
    try:
        edited = False if material_errors or envelope_errors else render(record) != normalized
    except MaterialsError:
        edited = True
    if edited:
        report["problems"].append("OAH_MATERIALS_EDITED")
    materials = {item["id"]: item for item in record["materials"]}
    heads = _heads(sources, {key: value for key, value in materials.items() if key not in material_errors})
    material_problems: dict[str, list[str]] = {}
    for ident, material in materials.items():
        problems: list[str] = []
        if ident in material_errors:
            problems.append(material_errors[ident])
            report["source_reads"].append({"material": ident, "source": None, "spec_digest": None,
                "snapshot": None, "envelope": None, "historical_evidence": "invalid", "historical_times": {},
                "recorded_eligibility": None, "eligibility": None, "integration": None, "registration": "unknown",
                "registered_head": None, "upstream_freshness": "unknown", "problems": problems})
            material_problems[ident] = problems
            continue
        read = {"material": ident, "source": material["source"]["id"], "spec_digest": material["spec_digest"],
                "snapshot": material["snapshot"], "envelope": material["portable_evidence_id"],
                "historical_evidence": "invalid", "historical_times": {key: (material["checked"] or {}).get(key)
                                                                      for key in ("verified_at", "confirmed_at")},
                "recorded_eligibility": (material.get("license") or {}).get("eligibility"),
                "eligibility": None, "integration": (material.get("integration") or {}).get("status"),
                "registration": "unknown", "registered_head": None, "upstream_freshness": portable.OFFLINE_FRESHNESS,
                "problems": problems}
        if material["source"].get("provider") == "authored":
            read.update(historical_evidence="unverified(authored)", eligibility="unknown",
                        registration="not-applicable(authored)")
            report["source_reads"].append(read)
            material_problems[ident] = problems
            continue
        evidence = verified_envelopes.get(material["portable_evidence_id"])
        if material["portable_evidence_id"] in envelope_errors:
            problems.append(envelope_errors[material["portable_evidence_id"]])
        if evidence is not None:
            facts = _facts(evidence, allow)
            same_policy = (material["license"] is not None and facts["license"] is not None
                           and material["license"]["policy_digest"] == facts["license"]["policy_digest"])
            if (_identity_view(material) != _identity_view(dict(material, **facts))
                    or same_policy and material["license"] != facts["license"]):
                problems.append("claim-unsupported")
            else:
                read["historical_evidence"] = portable.HISTORICAL
            if facts["license"] is not None:
                read["eligibility"] = facts["license"]["eligibility"]
        registered, head = heads.get(material["source"]["id"], (None, None))
        if sources is not None and material["source"]["id"] in heads:
            if registered is None:
                read["registration"] = "unregistered"
            elif registered != material["spec_digest"]:
                read["registration"] = "other-spec"
            else:
                read["registration"] = "same-spec"
                read["registered_head"] = head
                read["upstream_freshness"] = _freshness(sources, material["source"]["id"])
        report["source_reads"].append(read)
        material_problems[ident] = problems
    history_refs = {} if view is None else {ref.revision: ref.digest for ref in view.history}
    statuses = []
    for decision in record["decisions"]:
        ident = decision["id"]
        problems: list[str] = []
        status = "current"
        recorded = digest_of(decision["input_basis"])
        current_basis = None
        resolution_ref = None
        attached = [name for name, item in materials.items() if item["decision"] == ident]
        attached += [name for name in [decision["chosen"], *decision["alternatives"]] if name and name not in attached]
        bad_materials = [name for name in attached if material_problems.get(name)]
        inputs = record["decision_inputs"][ident]
        if edited:
            problems.append("materials-edited")
        if bad_materials:
            problems += [f"material-invalid:{name}" for name in sorted(bad_materials)]
        if decision_inputs_digest(inputs) != decision["input_basis"]["decision_inputs_digest"]:
            problems.append("decision-inputs-edited")
        if not _chain_ok(decision):
            problems.append("resolution-chain-broken")
        if view is not None:
            basis = decision["input_basis"]
            if basis["uid"] != view.uid or basis["design"] != view.design or decision["based_on"]["uid"] != view.uid \
                    or decision["based_on"]["design"] != view.design:
                problems.append("wrong-design-identity")
            based = decision["based_on"]
            if history_refs.get(based["revision"]) != based["digest"]:
                problems.append("same-revision-different-digest" if based["revision"] in history_refs
                                else "based-on-unknown-revision")
            for item in decision["resolutions"]:
                for ref in item["old_and_new_design_refs"].values():
                    if ref["uid"] != view.uid or history_refs.get(ref["revision"]) != ref["digest"]:
                        problems.append("resolution-ref-unknown")
        if problems:
            status = "invalid"
        elif view is None or view.problems():
            status = "unknown"
            problems.extend([f"authority-unavailable:{unavailable or 'unknown'}"] if view is None
                            else [f"authority-invalid:{problem}" for problem in view.problems()])
        else:
            current_basis = digest_of(input_basis(view, inputs))
            accepted = input_basis(view, inputs, which="accepted")
            if accepted is not None and digest_of(accepted) not in decision["supported_bases"]:
                status = "stale"
                problems.append("accepted-input-changed")
            elif current_basis not in decision["supported_bases"]:
                status = "needs-review"
                problems.append("working-input-changed")
            dependencies = _dependencies(decision, materials, heads)
            source_problems = []
            for item in dependencies:
                material = materials[item["material"]]
                if item["registered_spec"] is not None and item["registered_spec"] != item["spec_digest"]:
                    source_problems.append("source-spec-changed")
                elif item["head"] is not None and item["head"] != item["snapshot"]:
                    source_problems.append("source-snapshot-changed")
                license_record = material.get("license")
                if license_record is not None and license_record.get("policy_digest") != policy_digest:
                    source_problems.append("policy-changed")
            if source_problems:
                covering = next((item for item in reversed(decision["resolutions"])
                                 if item["source_dependencies"] == dependencies
                                 and item["policy_digest"] == policy_digest), None)
                if covering is not None:
                    resolution_ref = digest_of(covering)
                else:
                    problems += sorted(set(source_problems))
                    if status == "current":
                        status = "needs-review"
        statuses.append(status)
        report["decisions"].append({"id": ident, "selection_status": decision["status"], "input_status": status,
                                    "current_basis": current_basis, "recorded_basis": recorded,
                                    "execution_relevance": "selected" if decision["status"] == "selected"
                                    else "not-selected", "problems": problems, "resolution_ref": resolution_ref})
    if any(material_problems.values()):
        statuses.append("invalid")
    if edited:
        statuses.append("invalid")
    if view is not None and view.problems():
        statuses.append("unknown")
    report["overall"] = _worst(statuses)
    return report


def _freshness(sources, source_id: str) -> str:
    try:
        return sources.status(sources.spec(source_id)).freshness
    except (SourceError, StateProblem):
        return "unknown"
