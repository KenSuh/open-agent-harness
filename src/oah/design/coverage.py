# 설계 coverage 부분 — 아홉 단계의 확인 상태·기술 라벨·스킬 의무·공백을 기록하고, 사람이 읽는 절과 JSON 블록으로 그리고, 다시 읽어 단계 상태·재확인·완료를 계산한다.
"""The `coverage` part of a design: CoverageRecordV1 for the nine stages of `oah-design-stages/v1`.

    record = coverage.parse(text)                       # OAH_COVERAGE_INVALID | OAH_COVERAGE_EDITED
    report = coverage.evaluate(record, snapshot.texts)  # stages, complete, blocking, frontier, digests, wanted
    new = coverage.apply(record, envelope, texts=..., ref=...)   # a closed CoverageEditV1, validated in full first
    text = coverage.render(new)                         # a human section plus one ```oah-coverage JSON line

Stage status is derived, never asserted. A confirmation records the basis of the stage: the fresh semantic input
digests of the stage and of every stage that can reach it in the one influence graph (stages.reach_into). Every read
recomputes that map from the selected snapshot (answer regions are re-hashed, never trusted from the record), so a
changed decision re-checks exactly the stages it can reach, whether or not anyone wrote a recheck note. One answered
issue changes one stage; `complete` needs all nine. Nothing here writes a design, reads the network or calls a
model; saving is A's `oah design save P D --part coverage=FILE`.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from typing import Mapping, Sequence

from oah.design import stages as catalog
from oah.design.model import DesignError, utc_now, iso
from oah.design.sources.diff import escape
from oah.design.sources.state import StateProblem, loads
from oah.wiki.base import WikiError

SCHEMA = 1
PART = "coverage"
FENCE = "```oah-coverage"
MAX_BYTES = 256 * 1024
MAX_OPS = 256
MAX_EDIT_BYTES = 256 * 1024
MAX_NEEDS, MAX_GAPS, MAX_AUX = 64, 64, 128
NOTE_LIMIT = 200
TEXT_LIMIT = 4096
LIST_LIMIT = 32
INVALID_CODE = "OAH_COVERAGE_INVALID"
EDITED_CODE = "OAH_COVERAGE_EDITED"
INCOMPLETE_CODE = "OAH_COVERAGE_INCOMPLETE"

CHECK_STATUSES = ("open", "answered", "proposed", "deferred", "not-applicable")
APPLICABILITY = ("applies", "not-applicable", "undecided")
IMPACTS = ("minor", "outcome")
LINK_STATUSES = ("linked", "omitted", "unavailable", "gap")
MODES = ("interview", "reference", "optional-reference", "conditional", "user-only")
GAP_HANDLERS = ("oah-questions", "official-docs", "research")
RESOLUTIONS = ("skill", "gap", "unresolved")
DISPATCH_STAGES = ("initial", "correction", "review")
HOSTS = ("claude", "codex")
INVOCATIONS = ("model", "user-only")
NON_ANSWER_PARTS = ("coverage", "materials")

RECORD_KEYS = {"version", "catalog", "lang", "tech", "stages", "discovery"}
TECH_KEYS = {"value", "decision", "note"}
STAGE_KEYS = {"id", "applicability", "reason", "deferral", "checks", "confirmation", "recheck", "needs", "skills",
              "gaps"}
DEFERRAL_KEYS = {"reason", "resolve_by", "impact"}
CHECK_KEYS = {"id", "status", "impact", "note", "decision", "reason", "resolve_by", "answer_refs"}
REF_KEYS = {"part", "region", "sha256"}
CONFIRMATION_KEYS = {"revision", "basis", "at"}
RECHECK_KEYS = {"because", "from", "to", "noted_at"}
NEED_KEYS = {"version", "id", "design_stage", "check", "condition", "required", "required_by", "scope",
             "dispatch_stages", "resolution", "review", "gap", "auxiliary", "host", "invocation"}
NEED_GAP_KEYS = {"handler", "reason", "source_refs", "decision_ref"}
AUX_KEYS = {"path", "required", "sha256"}
LINK_KEYS = {"skill", "need_id", "required", "required_by", "seed", "review", "installed", "condition", "mode", "status",
             "reason", "pin", "license", "invocation"}
INSTALLED_KEYS = {"origin", "digest16"}
PIN_KEYS = {"locator", "commit", "path", "sha256", "lf_sha256"}
LICENSE_KEYS = {"expression", "eligibility"}
INVOCATION_KEYS = {"model", "user_only"}
GAP_KEYS = {"version", "id", "need_id", "check", "handled_by", "note", "source_refs", "decision_ref"}
DISCOVERY_KEYS = {"design_stage", "check", "tech", "query_ids", "last"}
LAST_KEYS = {"provider", "outcome", "at", "result_ref"}
ENVELOPE_KEYS = {"version", "expected", "ops"}
EXPECTED_KEYS = {"project", "design", "revision", "digest"}
OPS: Mapping[str, set[str]] = {
    "set-applicability": {"op", "stage", "value", "reason"},
    "set-check": {"op", "stage", "check", "status", "impact", "note", "decision", "reason", "resolve_by",
                  "answer_refs"},
    "defer-stage": {"op", "stage", "deferral"},
    "confirm-stage": {"op", "stage", "user_confirmation"},
    "set-tech": {"op", "label", "value", "decision", "note"},
    "link-skill": {"op", "stage", "need", "link"},
    "unlink-skill": {"op", "stage", "need_id", "skill", "reason"},
    "add-gap": {"op", "stage", "need_id", "check", "handled_by", "note", "source_refs", "decision_ref"},
    "clear-recheck": {"op", "stage"},
}

_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_ID = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HEADING = re.compile(r"### \[decision:([a-z-]+)\.([a-z-]+)\]")

LABELS = {
    "en": {"title": "# Coverage", "intro": "The nine design stages, as recorded. Live status, re-checks and blocking "
           "items: run `oah design coverage` for this design.",
           "head": "| Stage | Status | Open items | Specialist skills | Gaps |",
           "not-applicable": "not applicable", "deferred": "deferred", "unreviewed": "unreviewed",
           "confirmed": "confirmed", "in-progress": "in progress", "none": "-", "tech": "Technology",
           "confirmed_at": "confirmed at revision {revision}", "na": "not applicable: {reason}",
           "deferred_line": "deferred ({resolve_by}, {impact} impact): {reason}", "open_line": "{count} open"},
    "ko": {"title": "# 설계 범위 점검", "intro": "아홉 설계 단계의 기록이다. 지금 상태와 재확인, 막힌 항목은 이 설계에 "
           "`oah design coverage` 를 실행해 본다.",
           "head": "| 단계 | 상태 | 남은 항목 | 전문 스킬 | 공백 |",
           "not-applicable": "해당 없음", "deferred": "보류", "unreviewed": "검토 전", "confirmed": "확인됨",
           "in-progress": "진행 중", "none": "-", "tech": "기술",
           "confirmed_at": "{revision}번째 판에서 확인함", "na": "해당 없음 — {reason}",
           "deferred_line": "보류({resolve_by}, 영향 {impact}) — {reason}", "open_line": "남은 항목 {count}개"},
}


class CoverageError(WikiError):
    """A coverage refusal (exit 1); the message starts with its OAH_COVERAGE_* code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, 1)


def _invalid(message: str) -> CoverageError:
    return CoverageError(INVALID_CODE, message)


class Evidence:
    """Where reviewed skills and verified sources are looked up; the default finds nothing."""

    def review(self, digest: str) -> Mapping | None:
        return None

    def source(self, digest: str) -> Mapping | None:
        return None


NO_EVIDENCE = Evidence()


def review_pin(review: Mapping) -> dict | None:
    """The pin a link to this ReviewedSkillV1 must carry: its locator, commit, folder and SKILL.md digests."""
    source = review.get("source") if isinstance(review, Mapping) else None
    if not isinstance(source, Mapping):
        return None
    return {"locator": source.get("locator"), "commit": source.get("commit"), "path": source.get("path"),
            "sha256": source.get("skill_sha256"), "lf_sha256": source.get("skill_lf_sha256")}


def review_license(review: Mapping) -> dict | None:
    license_ = review.get("license") if isinstance(review, Mapping) else None
    if not isinstance(license_, Mapping):
        return None
    return {"expression": license_.get("expression"), "eligibility": license_.get("eligibility")}


# Field validators ---------------------------------------------------------------------------------------------------

def _keys(value: object, keys: set[str], what: str) -> Mapping:
    if not isinstance(value, dict) or set(value) != keys:
        have = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise _invalid(f"{what} must have exactly the keys {sorted(keys)} (it has {have})")
    return value


def _text(value: object, what: str, *, limit: int = TEXT_LIMIT, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
        raise _invalid(f"{what} must be one line of text")
    if len(value.encode("utf-8")) > limit or (limit == NOTE_LIMIT and len(value) > NOTE_LIMIT):
        raise _invalid(f"{what} is longer than {limit} bytes")
    return value


def _note(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value) or len(value) > NOTE_LIMIT:
        raise _invalid(f"{what} must be one line of at most {NOTE_LIMIT} characters")
    return value


def _ident(value: object, what: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 64 or not _ID.fullmatch(value):
        raise _invalid(f"{what} must be an ID of lowercase letters, digits and single - _ . separators")
    return value


def _hash(value: object, what: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise _invalid(f"{what} must be sha256: and 64 lowercase hex digits")
    return value


def _when(value: object, what: str) -> str:
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        raise _invalid(f"{what} must be a UTC time such as 2026-11-09T07:00:00+00:00")
    return value


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(f"{what} must be true or false")
    return value


def _list(value: object, what: str, limit: int = LIST_LIMIT) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise _invalid(f"{what} must be a list of at most {limit} items")
    return value


def _stage(value: object, what: str = "stage") -> str:
    if value not in catalog.BY_ID:
        raise _invalid(f"{what} {value!r} is not one of {', '.join(catalog.STAGE_IDS)}")
    return value  # type: ignore[return-value]


def _check_of(stage: str, value: object) -> str:
    if value not in catalog.all_checks(stage):
        raise _invalid(f"{value!r} is not a check of {stage}; its checks are {', '.join(catalog.all_checks(stage))}")
    return value  # type: ignore[return-value]


def _resolve_by(value: object, what: str) -> str:
    if value in catalog.FIXED_RESOLVE_BY or value in catalog.BY_ID:
        return value  # type: ignore[return-value]
    raise _invalid(f"{what} must be {', '.join(catalog.FIXED_RESOLVE_BY)} or a stage ID")


def _relpath(value: object, what: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512 or "\\" in value or value.startswith("/")
            or _CONTROL.search(value) or re.match(r"[A-Za-z]:", value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise _invalid(f"{what} must be a normalized relative POSIX path")
    return value


def _ref(value: object, what: str, *, allow_missing_part: bool = False) -> dict:
    ref = _keys(value, REF_KEYS, what)
    part = ref["part"]
    if not isinstance(part, str) or not _ID.fullmatch(part) or len(part) > 32:
        raise _invalid(f"{what}: part must be a part ID")
    if part in NON_ANSWER_PARTS:
        raise _invalid(f"{what}: the {part} part cannot supply an answer; name an authored part")
    region = ref["region"]
    if region != "whole":
        stage, _, check = str(region).partition(".")
        if stage not in catalog.BY_ID or check not in catalog.all_checks(stage):
            raise _invalid(f"{what}: region must be whole or <stage>.<check> (not {region!r})")
    _hash(ref["sha256"], f"{what} sha256")
    return dict(ref)


def need_id(need: Mapping) -> str:
    """The stable obligation identity of a SkillNeedV1 (its mutable resolution, review and gap excluded)."""
    identity = {key: need.get(key) for key in ("version", "design_stage", "check", "condition", "required",
                                                "required_by", "scope", "dispatch_stages", "auxiliary", "host",
                                                "invocation")}
    return _digest(identity)


def gap_id(gap: Mapping) -> str:
    return _digest({key: value for key, value in gap.items() if key != "id"})


def _digest(data: object) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_need(need: object, stage: str) -> dict:
    need = _keys(need, NEED_KEYS, "a skill need")
    if need["version"] != 1:
        raise _invalid("a skill need has version 1")
    if need["design_stage"] not in (None, stage):
        raise _invalid(f"a need stored under {stage} names design stage {need['design_stage']!r}")
    if need["check"] is not None:
        _check_of(stage, need["check"])
    try:
        catalog.parse_condition(need["condition"])
    except ValueError as exc:
        raise _invalid(f"need condition: {exc}") from None
    required = _bool(need["required"], "need required")
    required_by = [_ident(item, "need required_by") for item in _list(need["required_by"], "need required_by", 16)]
    if required and not required_by:
        raise _invalid("a required need names what requires it (required_by)")
    for item in _list(need["scope"], "need scope"):
        _relpath(item, "need scope path")
    stages = _list(need["dispatch_stages"], "need dispatch_stages", 3)
    if not stages or any(item not in DISPATCH_STAGES for item in stages) or stages != sorted(set(stages),
                                                                                             key=DISPATCH_STAGES.index):
        raise _invalid("need dispatch_stages is a nonempty list from initial, correction, review in that order")
    hosts = _list(need["host"], "need host", 2)
    if not hosts or any(item not in HOSTS for item in hosts) or hosts != sorted(set(hosts), key=HOSTS.index):
        raise _invalid("need host is a nonempty list from claude, codex in that order")
    if need["invocation"] not in INVOCATIONS:
        raise _invalid("need invocation is model or user-only")
    for item in _list(need["auxiliary"], "need auxiliary", MAX_AUX):
        aux = _keys(item, AUX_KEYS, "a need auxiliary file")
        _relpath(aux["path"], "auxiliary path")
        _bool(aux["required"], "auxiliary required")
        _hash(aux["sha256"], "auxiliary sha256", optional=not aux["required"])
    resolution = need["resolution"]
    if resolution not in RESOLUTIONS:
        raise _invalid("need resolution is skill, gap or unresolved")
    if resolution == "unresolved" and (need["review"] is not None or need["gap"] is not None):
        raise _invalid("an unresolved need has no review and no gap")
    if resolution == "skill" and (need["gap"] is not None or _hash(need["review"], "need review") is None):
        raise _invalid("a need resolved by a skill names its review and has no gap")
    if resolution == "gap":
        if need["review"] is not None:
            raise _invalid("a need resolved by a gap has no review")
        gap = _keys(need["gap"], NEED_GAP_KEYS, "a need's gap")
        if gap["handler"] not in GAP_HANDLERS:
            raise _invalid(f"gap handler is one of {', '.join(GAP_HANDLERS)}")
        _text(gap["reason"], "gap reason")
        refs = [_hash(item, "gap source ref") for item in _list(gap["source_refs"], "gap source refs")]
        if not refs and gap["handler"] != "oah-questions":
            raise _invalid("only an oah-questions gap may have no source refs")
        _ref(gap["decision_ref"], "gap decision_ref")
    if need["id"] != need_id(need):
        raise _invalid(f"need id {need['id']!r} is not the hash of its obligation fields")
    return need


def _validate_link(link: object, stage: str, needs: Mapping[str, Mapping]) -> dict:
    link = _keys(link, LINK_KEYS, "a skill link")
    _ident(link["skill"], "link skill")
    need = needs.get(link["need_id"])
    if need is None:
        raise _invalid(f"link {link['skill']} points at need {link['need_id']!r}, which is not stored in {stage}")
    for key in ("required", "required_by", "condition", "review"):
        if link[key] != need[key]:
            raise _invalid(f"link {link['skill']} and its need disagree on {key}")
    if need["resolution"] != "skill":
        raise _invalid(f"link {link['skill']}: its need is resolved by {need['resolution']}, not a skill")
    if link["seed"] is not None and link["seed"] not in catalog.SEED_BY_ID:
        raise _invalid(f"link {link['skill']}: unknown seed {link['seed']!r}")
    if link["mode"] not in MODES or link["status"] not in LINK_STATUSES:
        raise _invalid(f"link {link['skill']}: mode or status outside the closed lists")
    _text(link["reason"], "link reason", optional=True)
    if link["installed"] is not None:
        installed = _keys(link["installed"], INSTALLED_KEYS, "link installed")
        _ident(installed["origin"], "installed origin")
        if not isinstance(installed["digest16"], str) or not _HEX16.fullmatch(installed["digest16"]):
            raise _invalid("installed digest16 is 16 lowercase hex digits")
    if link["pin"] is not None:
        pin = _keys(link["pin"], PIN_KEYS, "link pin")
        _text(pin["locator"], "pin locator", limit=512)
        if not isinstance(pin["commit"], str) or not _HEX40.fullmatch(pin["commit"]):
            raise _invalid("pin commit is 40 lowercase hex digits")
        _relpath(pin["path"], "pin path")
        _hash(pin["sha256"], "pin sha256")
        _hash(pin["lf_sha256"], "pin lf_sha256")
    if link["license"] is not None:
        license_ = _keys(link["license"], LICENSE_KEYS, "link license")
        _text(license_["expression"], "license expression", limit=200)
        _ident(license_["eligibility"], "license eligibility")
    invocation = _keys(link["invocation"], INVOCATION_KEYS, "link invocation")
    if invocation["model"] is not None:
        _bool(invocation["model"], "invocation model")
    if _bool(invocation["user_only"], "invocation user_only") != (need["invocation"] == "user-only"):
        raise _invalid(f"link {link['skill']}: user_only disagrees with its need's invocation")
    if link["status"] == "linked" and link["review"] is None:
        raise _invalid(f"link {link['skill']} is linked without a review")
    return link


def _validate_gap(gap: object, stage: str, needs: Mapping[str, Mapping]) -> dict:
    gap = _keys(gap, GAP_KEYS, "a coverage gap")
    if gap["version"] != 1:
        raise _invalid("a coverage gap has version 1")
    _check_of(stage, gap["check"])
    need = needs.get(gap["need_id"])
    if need is None or need["resolution"] != "gap":
        raise _invalid(f"gap {gap['id']!r} points at a need that is not resolved by a gap in {stage}")
    if gap["handled_by"] not in GAP_HANDLERS:
        raise _invalid(f"gap handled_by is one of {', '.join(GAP_HANDLERS)}")
    _text(gap["note"], "gap note")
    refs = [_hash(item, "gap source ref") for item in _list(gap["source_refs"], "gap source refs")]
    _ref(gap["decision_ref"], "gap decision_ref")
    mirror = {"handler": gap["handled_by"], "reason": gap["note"], "source_refs": refs,
              "decision_ref": gap["decision_ref"]}
    if need["gap"] != mirror:
        raise _invalid(f"gap {gap['id']!r} does not mirror its need's gap exactly")
    if gap["id"] != gap_id(gap):
        raise _invalid(f"gap id {gap['id']!r} is not the hash of the gap")
    return gap


def _validate_check(check: object, stage: str, expected: str) -> dict:
    check = _keys(check, CHECK_KEYS, f"check {stage}.{expected}")
    if check["id"] != expected:
        raise _invalid(f"{stage} lists check {check['id']!r} where {expected!r} belongs (catalog order)")
    status = check["status"]
    where = f"{stage}.{expected}"
    if status not in CHECK_STATUSES:
        raise _invalid(f"{where}: status is one of {', '.join(CHECK_STATUSES)}")
    if check["impact"] is not None and check["impact"] not in IMPACTS:
        raise _invalid(f"{where}: impact is minor or outcome")
    _note(check["note"], f"{where} note")
    _ident(check["decision"], f"{where} decision", optional=True)
    _text(check["reason"], f"{where} reason", optional=True)
    if check["resolve_by"] is not None:
        _resolve_by(check["resolve_by"], f"{where} resolve_by")
    refs = [_ref(item, f"{where} answer ref") for item in _list(check["answer_refs"], f"{where} answer refs")]
    if status in ("answered", "proposed") and not refs:
        raise _invalid(f"{where}: an {status} check names the authored text that answers it (answer_refs)")
    if status in ("open", "not-applicable", "deferred") and refs:
        raise _invalid(f"{where}: a {status} check has no answer refs")
    if status == "proposed" and check["impact"] is None:
        raise _invalid(f"{where}: a proposal says its impact (minor or outcome)")
    if status == "deferred" and (check["reason"] is None or check["resolve_by"] is None or check["impact"] is None):
        raise _invalid(f"{where}: a deferral needs a reason, resolve_by and impact")
    if status == "not-applicable" and check["reason"] is None:
        raise _invalid(f"{where}: not-applicable needs a reason")
    if status != "deferred" and check["resolve_by"] is not None:
        raise _invalid(f"{where}: only a deferred check has resolve_by")
    return check


def _validate(record: object) -> dict:
    record = _keys(record, RECORD_KEYS, "the coverage record")
    if record["version"] != SCHEMA or record["catalog"] != catalog.CATALOG:
        raise _invalid(f"the coverage record is version {SCHEMA} of {catalog.CATALOG}")
    if record["lang"] not in LABELS:
        raise _invalid("the coverage lang is en or ko")
    tech = record["tech"]
    if not isinstance(tech, dict):
        raise _invalid("tech is an object of label -> {value, decision, note}")
    for label, entry in tech.items():
        if label not in catalog.TECH_LABELS:
            raise _invalid(f"unknown tech label {label!r}; labels are {', '.join(catalog.TECH_LABELS)}")
        entry = _keys(entry, TECH_KEYS, f"tech {label}")
        _tech_value(label, entry["value"])
        _ident(entry["decision"], f"tech {label} decision", optional=True)
        _note(entry["note"], f"tech {label} note")
    rows = _list(record["stages"], "stages", len(catalog.STAGES))
    if [row.get("id") if isinstance(row, dict) else None for row in rows] != list(catalog.STAGE_IDS):
        raise _invalid(f"stages must be exactly {', '.join(catalog.STAGE_IDS)} in that order")
    total = {"needs": 0, "gaps": 0, "aux": 0}
    for row in rows:
        row = _keys(row, STAGE_KEYS, f"stage {row.get('id')}")
        stage = row["id"]
        item = catalog.BY_ID[stage]
        if row["applicability"] not in APPLICABILITY:
            raise _invalid(f"{stage}: applicability is applies, not-applicable or undecided")
        if not item.may_be_na and row["applicability"] != "applies":
            raise _invalid(f"{stage} always applies; only access, server and payment may be not-applicable")
        if row["applicability"] == "not-applicable":
            _text(row["reason"], f"{stage} not-applicable reason")
        elif row["reason"] is not None:
            raise _invalid(f"{stage}: only a not-applicable stage has a reason")
        if row["deferral"] is not None:
            deferral = _keys(row["deferral"], DEFERRAL_KEYS, f"{stage} deferral")
            _text(deferral["reason"], f"{stage} deferral reason")
            _resolve_by(deferral["resolve_by"], f"{stage} deferral resolve_by")
            if deferral["impact"] not in IMPACTS:
                raise _invalid(f"{stage} deferral impact is minor or outcome")
        checks = _list(row["checks"], f"{stage} checks")
        if len(checks) != len(catalog.all_checks(stage)):
            raise _invalid(f"{stage} lists checks {', '.join(catalog.all_checks(stage))}, in that order")
        for check, expected in zip(checks, catalog.all_checks(stage)):
            _validate_check(check, stage, expected)
        if row["confirmation"] is not None:
            confirmation = _keys(row["confirmation"], CONFIRMATION_KEYS, f"{stage} confirmation")
            if not isinstance(confirmation["revision"], int) or isinstance(confirmation["revision"], bool) \
                    or confirmation["revision"] < 1:
                raise _invalid(f"{stage} confirmation revision is a positive number")
            basis = confirmation["basis"]
            if not isinstance(basis, dict) or set(basis) != set(catalog.reach_into(stage)):
                raise _invalid(f"{stage} confirmation basis names exactly {', '.join(catalog.reach_into(stage))}")
            for name, digest in basis.items():
                _hash(digest, f"{stage} confirmation basis {name}")
            _when(confirmation["at"], f"{stage} confirmation at")
        for note in _list(row["recheck"], f"{stage} recheck", 64):
            note = _keys(note, RECHECK_KEYS, f"{stage} recheck note")
            _text(note["because"], "recheck because", limit=200)
            _text(note["from"], "recheck from", limit=200, optional=True)
            _text(note["to"], "recheck to", limit=200, optional=True)
            _when(note["noted_at"], "recheck noted_at")
        needs: dict[str, Mapping] = {}
        for need in _list(row["needs"], f"{stage} needs", MAX_NEEDS):
            need = _validate_need(need, stage)
            if need["id"] in needs:
                raise _invalid(f"{stage} stores need {need['id']} twice")
            needs[need["id"]] = need
            total["aux"] += len(need["auxiliary"])
        total["needs"] += len(needs)
        links = [_validate_link(link, stage, needs) for link in _list(row["skills"], f"{stage} skills", MAX_NEEDS)]
        if len({(link["need_id"], link["skill"]) for link in links}) != len(links):
            raise _invalid(f"{stage} lists one skill link twice")
        gaps = [_validate_gap(gap, stage, needs) for gap in _list(row["gaps"], f"{stage} gaps", MAX_GAPS)]
        total["gaps"] += len(gaps)
        gap_needs = [gap["need_id"] for gap in gaps]
        if len(set(gap_needs)) != len(gap_needs):
            raise _invalid(f"{stage} stores two gaps for one need")
        for need in needs.values():
            if need["resolution"] == "gap" and need["id"] not in gap_needs:
                raise _invalid(f"{stage}: need {need['id']} is resolved by a gap that is not stored")
            if need["resolution"] == "gap" and any(link["need_id"] == need["id"] for link in links):
                raise _invalid(f"{stage}: need {need['id']} has both a gap and a skill link")
    if total["needs"] > MAX_NEEDS or total["gaps"] > MAX_GAPS or total["aux"] > MAX_AUX:
        raise _invalid(f"the record holds more than {MAX_NEEDS} needs, {MAX_GAPS} gaps or {MAX_AUX} auxiliary files")
    for entry in _list(record["discovery"], "discovery", 64):
        entry = _keys(entry, DISCOVERY_KEYS, "a discovery entry")
        _stage(entry["design_stage"], "discovery design_stage")
        _check_of(entry["design_stage"], entry["check"])
        if not isinstance(entry["tech"], dict):
            raise _invalid("discovery tech is an object")
        for label, value in entry["tech"].items():
            if label not in catalog.TECH_LABELS:
                raise _invalid(f"discovery tech names unknown label {label!r}")
            _tech_value(label, value)
        for item in _list(entry["query_ids"], "discovery query_ids", 8):
            _hash(item, "discovery query id")
        if entry["last"] is not None:
            last = _keys(entry["last"], LAST_KEYS, "discovery last")
            _ident(last["provider"], "discovery provider")
            _ident(last["outcome"], "discovery outcome")
            _when(last["at"], "discovery at")
            _hash(last["result_ref"], "discovery result_ref", optional=True)
    return record


def _tech_value(label: str, value: object) -> str:
    if isinstance(value, str) and (value in catalog.TECH_LABELS[label] or catalog.LOCAL_VALUE.fullmatch(value)):
        return value
    raise _invalid(f"tech {label} is one of {', '.join(catalog.TECH_LABELS[label])} or other:<word> (not {value!r})")


# Record, text ---------------------------------------------------------------------------------------------------------

def empty(lang: str = "en") -> dict:
    """A record with nine unreviewed stages: what a design without a coverage part reads as."""
    if lang not in LABELS:
        raise _invalid("the coverage lang is en or ko")
    rows = []
    for stage in catalog.STAGES:
        rows.append({"id": stage.id, "applicability": "undecided" if stage.may_be_na else "applies", "reason": None,
                     "deferral": None, "confirmation": None, "recheck": [], "needs": [], "skills": [], "gaps": [],
                     "checks": [{"id": check, "status": "open", "impact": None, "note": None, "decision": None,
                                 "reason": None, "resolve_by": None, "answer_refs": []}
                                for check in catalog.all_checks(stage.id)]})
    return {"version": SCHEMA, "catalog": catalog.CATALOG, "lang": lang, "tech": {}, "stages": rows, "discovery": []}


def canonical_json(record: Mapping) -> str:
    """Sorted-key compact JSON, ASCII-escaped so recorded text never reaches a viewer raw."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _cell(text: object) -> str:
    return escape(str(text)).replace("\\", "\\\\").replace("|", "\\|")


def _required_checks(record: Mapping, row: Mapping) -> list[str]:
    stage = catalog.BY_ID[row["id"]]
    out = list(stage.checks)
    for check, condition in stage.conditional.items():
        if catalog.condition_holds(condition, record) is not False:
            out.append(check)
    return out


def _recorded_status(row: Mapping) -> str:
    if row["applicability"] == "not-applicable":
        return "not-applicable"
    if row["deferral"] is not None:
        return "deferred"
    if row["confirmation"] is not None:
        return "confirmed"
    if all(check["status"] == "open" for check in row["checks"]):
        return "unreviewed"
    return "in-progress"


def _open_items(record: Mapping, row: Mapping) -> list[str]:
    if row["applicability"] == "not-applicable":
        return []
    required = _required_checks(record, row)
    out = []
    for check in row["checks"]:
        if check["id"] not in required or check["status"] == "not-applicable":
            continue
        if check["status"] == "open" or (check["status"] in ("proposed", "deferred") and check["impact"] == "outcome"):
            out.append(check["id"])
    return out


def render(record: Mapping) -> str:
    """The deterministic human section in the record's language, ending in one ```oah-coverage JSON line."""
    labels = LABELS[record["lang"]]
    out = [labels["title"], "", labels["intro"], "", labels["head"], "|---|---|---|---|---|"]
    lines = []
    for row in record["stages"]:
        stage = catalog.BY_ID[row["id"]]
        status = _recorded_status(row)
        items = _open_items(record, row)
        skills = ", ".join(f"{link['skill']} ({link['status']})" for link in row["skills"]) or labels["none"]
        gaps = ", ".join(f"{gap['check']} ({gap['handled_by']})" for gap in row["gaps"]) or labels["none"]
        out.append(f"| {_cell(stage.names[record['lang']])} | {labels[status]} | "
                   f"{_cell(', '.join(items)) if items else labels['none']} | {_cell(skills)} | {_cell(gaps)} |")
        if status == "not-applicable":
            detail = labels["na"].format(reason=row["reason"])
        elif status == "deferred":
            detail = labels["deferred_line"].format(**row["deferral"])
        elif status == "confirmed":
            detail = labels["confirmed_at"].format(revision=row["confirmation"]["revision"])
        else:
            detail = labels["open_line"].format(count=len(items))
        lines.append(f"- {_cell(stage.names[record['lang']])}: {_cell(detail)}")
    out += ["", *lines]
    if record["tech"]:
        tech = "; ".join(f"{label} = {record['tech'][label]['value']}" for label in sorted(record["tech"]))
        out += ["", f"{labels['tech']}: {_cell(tech)}"]
    out += ["", FENCE, canonical_json(record), "```"]
    return "\n".join(out) + "\n"


def _data_block(text: str) -> str:
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if line == FENCE]
    if not starts:
        raise _invalid("the coverage text has no ```oah-coverage data block")
    start = starts[-1]
    if start + 2 >= len(lines) or lines[start + 2] != "```" or any(line.strip() for line in lines[start + 3:]):
        raise _invalid("the ```oah-coverage data block is not one closed JSON line at the end of the part")
    return lines[start + 1]


def parse(text: str) -> dict:
    """The record in a coverage part; OAH_COVERAGE_INVALID, or OAH_COVERAGE_EDITED when the human section was edited."""
    if not isinstance(text, str):
        raise _invalid("the coverage part is text")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("﻿"):
        normalized = normalized[1:]
    normalized = normalized.rstrip("\n") + "\n"
    if len(normalized.encode("utf-8")) > MAX_BYTES:
        raise _invalid(f"the coverage part is over {MAX_BYTES} bytes")
    try:
        record = loads(_data_block(normalized).encode("utf-8"), "the coverage data block")
    except StateProblem as exc:
        raise _invalid(exc.detail) from None
    record = _validate(record)
    if render(record) != normalized:
        raise CoverageError(EDITED_CODE, "the human section of the coverage part differs from its JSON block; fold "
                            "the intended change into the JSON with `oah design coverage edit`, then save again")
    return record


def read(texts: Mapping[str, str], lang: str = "en") -> dict:
    """The record of a snapshot's coverage part, or nine unreviewed stages when it has none."""
    text = texts.get(PART)
    return empty(lang) if text is None else parse(text)


# Answer regions, materials, semantic inputs --------------------------------------------------------------------------

def _region_body(text: str, region: str) -> str | None:
    """The exact bytes after the unique `### [decision:<region>]` line, up to the next decision heading or the end."""
    heading = f"### [decision:{region}]"
    lines = text.split("\n")
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    starts = [index for index, line in enumerate(lines) if line == heading]
    if len(starts) != 1:
        return None
    begin = min(offsets[starts[0]] + len(heading) + 1, len(text))
    end = next((offsets[index] for index in range(starts[0] + 1, len(lines))
                if lines[index].startswith("### [decision:")), len(text))
    return text[begin:end]


def resolve_ref(texts: Mapping[str, str], ref: Mapping) -> str | None:
    """The fresh sha256 of an AnswerRefV1's complete LF-normalized body, or None when it cannot be resolved."""
    part = ref.get("part")
    if part in NON_ANSWER_PARTS or part not in texts:
        return None
    text = texts[part].replace("\r\n", "\n").replace("\r", "\n")
    body = text if ref.get("region") == "whole" else _region_body(text, str(ref.get("region")))
    if body is None:
        return None
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def materials_projection(texts: Mapping[str, str]) -> dict | None:
    """E's semantic view of the materials part: decision inputs and chosen fields; None when it cannot be read."""
    from oah.design import materials

    text = texts.get("materials")
    if text is None:
        return {"state": "absent"}
    try:
        record = materials.parse(text)
        decisions = [{"id": item.get("id"), "status": item.get("status"), "chosen": item.get("chosen")}
                     for item in record.get("decisions", [])]
        chosen = [{"id": item.get("id"), "decision": item.get("decision"), "choice": item.get("choice"),
                   "source": item.get("source"), "spec_digest": item.get("spec_digest")}
                  for item in record.get("materials", [])]
        return {"state": "parsed", "decision_inputs": _digest(record.get("decision_inputs")), "decisions": decisions,
                "materials": chosen}
    except (materials.MaterialsError, DesignError, AttributeError, TypeError, ValueError):
        return None


def _effective(need: Mapping, links: Sequence[Mapping], gaps: Sequence[Mapping]) -> str:
    if need["resolution"] == "skill":
        return "skill" if any(link["need_id"] == need["id"] and link["status"] == "linked" for link in links) \
            else "unresolved"
    if need["resolution"] == "gap":
        return "gap" if any(gap["need_id"] == need["id"] for gap in gaps) else "unresolved"
    return "unresolved"


def _link_view(link: Mapping, record: Mapping, evidence: Evidence) -> dict:
    """A stored link re-evaluated: its condition and its review are checked again on every read."""
    holds, why = catalog.explain_condition(link["condition"], record)
    status, reason = link["status"], link["reason"]
    if holds is False:
        status, reason = "omitted", f"condition-false: {why}"
    elif holds is None and link["required"]:
        status, reason = "unavailable", f"condition-unknown: {why}"
    elif status == "linked":
        review = evidence.review(link["review"]) if link["review"] else None
        if review is None:
            status, reason = "unavailable", "review-missing: the reviewed record is not in this state folder"
        elif link["pin"] is not None and review_pin(review) != link["pin"]:
            status, reason = "unavailable", "tampered: the link's pin differs from its review"
        elif review_license(review) != link["license"] or (link["license"] or {}).get("eligibility") != "eligible":
            status, reason = "unavailable", "license: the link's license differs from its review or is not eligible"
    return {**link, "status": status, "reason": reason}


def _stage_input(record: Mapping, row: Mapping, texts: Mapping[str, str], projection: dict | None,
                 evidence: Evidence) -> tuple[str | None, list[str]]:
    problems: list[str] = []
    unknown = False
    checks = []
    for check in row["checks"]:
        answers = []
        for ref in check["answer_refs"]:
            fresh = resolve_ref(texts, ref)
            if fresh is None:
                problems.append(f"answer-missing: {row['id']}.{check['id']} ({ref['part']}:{ref['region']})")
                unknown = True
                continue
            if fresh != ref["sha256"]:
                problems.append(f"answer-changed: {row['id']}.{check['id']}")
            answers.append({"part": ref["part"], "region": ref["region"], "sha256": fresh})
        checks.append({"id": check["id"], "status": check["status"], "impact": check["impact"],
                       "reason": check["reason"], "resolve_by": check["resolve_by"], "decision": check["decision"],
                       "answers": answers})
    tech = {label: {"value": record["tech"][label]["value"], "decision": record["tech"][label]["decision"]}
            for label in catalog.tech_labels_of(row["id"]) if label in record["tech"]}
    links = [_link_view(link, record, evidence) for link in row["skills"]]
    needs = []
    for need in row["needs"]:
        holds, _ = catalog.explain_condition(need["condition"], record)
        needs.append({"need": need, "applies": holds, "effective": _effective(need, links, row["gaps"])})
    gaps = []
    for gap in row["gaps"]:
        sources = []
        for ref in gap["source_refs"]:
            found = evidence.source(ref) or evidence.review(ref)
            if found is None:
                problems.append(f"gap-source-missing: {gap['check']}")
                unknown = True
            else:
                sources.append(_digest(found))
        decision = resolve_ref(texts, gap["decision_ref"])
        if decision is None:
            problems.append(f"gap-decision-missing: {gap['check']}")
            unknown = True
        gaps.append({"gap": gap, "sources": sources, "decision": decision})
    semantic = {"catalog": catalog.CATALOG, "stage": row["id"], "applicability": row["applicability"],
                "reason": row["reason"], "deferral": row["deferral"], "checks": checks, "tech": tech,
                "needs": needs, "gaps": gaps,
                "links": [{key: link[key] for key in ("skill", "need_id", "required", "review", "installed", "pin",
                                                      "license", "invocation", "status", "condition", "mode")}
                          for link in links]}
    if row["id"] == "materials":
        if projection is None:
            problems.append("unknown-materials")
            unknown = True
        semantic["materials"] = projection
    return (None if unknown else _digest(semantic)), problems


def digests(record: Mapping, texts: Mapping[str, str], *, evidence: Evidence = NO_EVIDENCE
            ) -> dict[str, tuple[str | None, list[str]]]:
    """Every stage's fresh SemanticStageInputV1 digest (None when an input is unknown) and its problems."""
    projection = materials_projection(texts)
    return {row["id"]: _stage_input(record, row, texts, projection, evidence) for row in record["stages"]}


def basis(stage: str, fresh: Mapping[str, tuple[str | None, list[str]]]) -> dict[str, str] | None:
    out = {}
    for name in catalog.reach_into(stage):
        digest = fresh[name][0]
        if digest is None:
            return None
        out[name] = digest
    return dict(sorted(out.items()))


# Evaluation -----------------------------------------------------------------------------------------------------------

def _seed_views(record: Mapping, row: Mapping, links: Sequence[Mapping]) -> list[dict]:
    out = []
    linked = {link["seed"] for link in links if link["seed"]} | {link["skill"] for link in links}
    for seed in catalog.SEED_SKILLS:
        if row["id"] not in seed.stages or seed.id in linked or seed.skill in linked:
            continue
        holds, why = catalog.explain_condition(seed.condition, record)
        if row["applicability"] == "not-applicable":
            holds, why = False, f"{row['id']} is not applicable"
        if holds is False:
            status, reason = "omitted", f"condition-false: {why}"
        elif holds is None:
            status, reason = "omitted", f"condition-unknown: {why}"
        else:
            status, reason = "unavailable", ("no reviewed or installed copy is linked yet; `oah skills find` and "
                                             "`oah skills review` can supply one")
        out.append({"seed": seed.id, "skill": seed.skill, "mode": seed.mode, "condition": seed.condition,
                    "status": status, "reason": reason, "required": False,
                    "research_pin": f"{seed.repo}@{seed.commit}:{seed.path}"})
    return out


def _wanted(record: Mapping, row: Mapping) -> list[dict]:
    if row["applicability"] == "not-applicable":
        return []
    required = _required_checks(record, row)
    out = []
    for check in row["checks"]:
        if check["id"] not in required or check["status"] == "not-applicable":
            continue
        for query, used in catalog.queries_for(row["id"], check["id"], record["tech"]):
            out.append({"design_stage": row["id"], "check": check["id"], "query": query, "tech": used})
    return out


def _requirements(record: Mapping, row: Mapping, links: Sequence[Mapping]) -> list[str]:
    """What keeps a stage from being confirmable now (empty when every requirement is met)."""
    stage = row["id"]
    if row["applicability"] == "undecided":
        return [f"{stage}: applicability undecided"]
    required = _required_checks(record, row)
    out = []
    for check in row["checks"]:
        if check["id"] not in required:
            continue
        if check["status"] == "open":
            out.append(f"{stage}.{check['id']}: open")
        elif check["status"] in ("proposed", "deferred") and check["impact"] == "outcome":
            out.append(f"{stage}.{check['id']}: {check['status']} with outcome impact")
    for need in row["needs"]:
        holds, why = catalog.explain_condition(need["condition"], record)
        if holds is False or not need["required"]:
            continue
        if holds is None:
            out.append(f"{stage}: need {need['id'][7:19]} has an unknown condition ({why})")
        elif _effective(need, links, row["gaps"]) == "unresolved":
            out.append(f"{stage}: need {need['id'][7:19]} is unresolved")
    return out


def evaluate(record: Mapping, texts: Mapping[str, str], *, evidence: Evidence = NO_EVIDENCE) -> dict:
    """Derived stage status, recheck, blocking items, frontier and completeness for one snapshot's texts."""
    fresh = digests(record, texts, evidence=evidence)
    rows = {row["id"]: row for row in record["stages"]}
    views: dict[str, dict] = {}
    for stage in catalog.STAGE_IDS:
        row = rows[stage]
        links = [_link_view(link, record, evidence) for link in row["skills"]]
        current = basis(stage, fresh)
        confirmation = row["confirmation"]
        reasons: list[str] = []
        if confirmation is not None:
            for name in catalog.reach_into(stage):
                digest, problems = fresh[name]
                if digest is None:
                    reasons.extend(problem for problem in problems if problem == "unknown-materials")
                    reasons.append(f"unknown-input: {name}")
                elif confirmation["basis"].get(name) != digest:
                    reasons.append(f"input-changed: {name}")
            reasons.extend(problem for problem in fresh[stage][1] if problem.startswith("answer-changed"))
        requirements = _requirements(record, row, links)
        recheck = row["applicability"] != "not-applicable" and confirmation is not None and (
            current is None or current != confirmation["basis"])
        if row["applicability"] == "not-applicable":
            status = "not-applicable"
        elif row["deferral"] is not None:
            status = "deferred"
        elif confirmation is None and all(check["status"] == "open" for check in row["checks"]):
            status = "unreviewed"
        elif confirmation is not None and not recheck and not requirements:
            status = "confirmed"
        else:
            status = "in-progress"
        blocking: list[str] = []
        if status == "deferred":
            blocking.append(f"{stage}: deferred ({row['deferral']['resolve_by']}): {row['deferral']['reason']}")
        elif status not in ("confirmed", "not-applicable"):
            blocking.append(f"{stage}: not confirmed")
        if recheck:
            blocking.append(f"{stage}: re-check needed ({', '.join(dict.fromkeys(reasons)) or 'inputs changed'})")
        if status != "not-applicable":
            blocking.extend(item for item in requirements if item not in blocking)
        views[stage] = {
            "id": stage, "name": catalog.BY_ID[stage].names[record["lang"]], "status": status, "recheck": recheck,
            "recheck_reasons": list(dict.fromkeys(reasons)) if recheck else [],
            "applicability": row["applicability"], "reason": row["reason"], "deferral": row["deferral"],
            "open": _open_items(record, row), "blocking": blocking,
            "skills": [*links, *_seed_views(record, row, links)], "gaps": list(row["gaps"]),
            "needs": [dict(need, applies=catalog.condition_holds(need["condition"], record),
                           effective=_effective(need, links, row["gaps"])) for need in row["needs"]],
            "digest": fresh[stage][0], "problems": fresh[stage][1],
            "confirmation": confirmation}
    outcome_open = [f"{stage}.{check}" for stage in catalog.STAGE_IDS for check in views[stage]["open"]
                    if rows[stage]["applicability"] != "not-applicable"
                    and next(item for item in rows[stage]["checks"] if item["id"] == check)["impact"] == "outcome"]
    resolved = {stage for stage, view in views.items()
                if view["status"] in ("confirmed", "not-applicable", "deferred") and not view["recheck"]}
    frontier = [stage for stage in catalog.STAGE_IDS if stage not in resolved
                and all(before in resolved for before in catalog.BY_ID[stage].prerequisites)]
    complete = (all(view["status"] in ("confirmed", "not-applicable") for view in views.values())
                and not any(view["recheck"] for view in views.values()) and not outcome_open)
    blocking = [item for stage in catalog.STAGE_IDS for item in views[stage]["blocking"]]
    return {"catalog": catalog.CATALOG, "complete": complete, "stages": [views[stage] for stage in catalog.STAGE_IDS],
            "blocking": blocking, "frontier": frontier,
            "digests": {stage: fresh[stage][0] for stage in catalog.STAGE_IDS},
            "wanted": [item for stage in catalog.STAGE_IDS for item in _wanted(record, rows[stage])]}


def choose_stage(report: Mapping, *, advise=None) -> dict:
    """The next design stage for the design flow (DESIGN §7.1, design-stage/v1): the frontier's first stage in catalog
    order, unless an admitted Choice over two or more frontier stages picked another one and passed its recheck. One
    stage is taken without asking (`unused(single-option)`); an empty frontier takes none (`unused(no-candidate)`) and
    says whether the design is complete or blocked, since an empty frontier alone is not a finished design.
    `advise(options)` receives only the opaque map {"o0": stage, ...} in catalog order and returns {"status",
    "applied", "choice", "reason"}; a choice outside the map is never matched to a stage by similarity."""
    frontier = list(report["frontier"])
    first = frontier[0] if frontier else None
    choice = {"version": 1, "frontier": frontier, "stage": first, "fallback": first, "jev": None, "applied": False,
              "reason": None, "complete": bool(report["complete"])}
    if not frontier:
        return dict(choice, jev="unused(no-candidate)", reason="complete" if report["complete"] else "blocked")
    if len(frontier) == 1:
        return dict(choice, jev="unused(single-option)")
    if advise is None:
        return dict(choice, jev="unused(judgement-off)")
    options = {f"o{index}": stage for index, stage in enumerate(frontier)}
    advice = advise(dict(options))
    picked = options.get(advice.get("choice")) if advice.get("applied") is True else None
    if picked is None:
        return dict(choice, jev=advice["status"], reason=advice.get("reason") or "not-applied")
    return dict(choice, stage=picked, jev=advice["status"], applied=True)


def needs(record: Mapping, texts: Mapping[str, str], *, evidence: Evidence = NO_EVIDENCE) -> list[dict]:
    """Every stored need, re-resolved: its applicability now and its effective resolution."""
    out = []
    for row in record["stages"]:
        links = [_link_view(link, record, evidence) for link in row["skills"]]
        for need in row["needs"]:
            holds, why = catalog.explain_condition(need["condition"], record)
            out.append({"need": copy.deepcopy(need), "applies": holds, "why": why,
                        "effective": _effective(need, links, row["gaps"]),
                        "links": [link for link in links if link["need_id"] == need["id"]],
                        "gap": next((gap for gap in row["gaps"] if gap["need_id"] == need["id"]), None)})
    return out


def links(record: Mapping, *, evidence: Evidence = NO_EVIDENCE) -> list[dict]:
    return [dict(_link_view(link, record, evidence), stage=row["id"]) for row in record["stages"]
            for link in row["skills"]]


# Edits ----------------------------------------------------------------------------------------------------------------

def check_envelope(envelope: object) -> dict:
    """A CoverageEditV1 with every operation's shape checked, before anything is applied."""
    envelope = _keys(envelope, ENVELOPE_KEYS, "the coverage edit")
    if envelope["version"] != 1:
        raise _invalid("the coverage edit is version 1")
    expected = _keys(envelope["expected"], EXPECTED_KEYS, "the coverage edit's expected design")
    for key in ("project", "design"):
        if not isinstance(expected[key], str) or not _ID.fullmatch(expected[key]):
            raise _invalid(f"expected {key} must be an ID")
    if not isinstance(expected["revision"], int) or isinstance(expected["revision"], bool) or expected["revision"] < 1:
        raise _invalid("expected revision is a positive number")
    _hash(expected["digest"], "expected digest")
    ops = _list(envelope["ops"], "ops", MAX_OPS)
    for index, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise _invalid(f"op {index + 1}: op is one of {', '.join(OPS)}")
        _keys(op, OPS[op["op"]], f"op {index + 1} ({op['op']})")
        if "stage" in op:
            _stage(op["stage"], f"op {index + 1} stage")
    confirmed = {op["stage"] for op in ops if op["op"] == "confirm-stage"}
    for op in ops:
        if op["op"] == "clear-recheck" and op["stage"] not in confirmed:
            raise _invalid(f"clear-recheck {op['stage']} is valid only with confirm-stage {op['stage']} in the same "
                           "edit; it never repairs a mismatch by itself")
    return envelope


def _row(record: dict, stage: str) -> dict:
    return next(row for row in record["stages"] if row["id"] == stage)


def _check_ref_now(texts: Mapping[str, str], ref: Mapping, where: str) -> None:
    fresh = resolve_ref(texts, ref)
    if fresh is None:
        raise _invalid(f"{where}: {ref['part']}:{ref['region']} is not in this revision (a missing part, or a "
                       "missing or repeated `### [decision:<stage>.<check>]` heading)")
    if fresh != ref["sha256"]:
        raise _invalid(f"{where}: {ref['part']}:{ref['region']} has sha256 {fresh}, not {ref['sha256']}; read the "
                       "design again")


def _confirm(record: dict, stage: str, texts: Mapping[str, str], revision: int, evidence: Evidence,
             now: dt.datetime) -> None:
    row = _row(record, stage)
    if row["applicability"] == "not-applicable":
        raise _invalid(f"{stage} is not applicable; there is nothing to confirm")
    if row["deferral"] is not None:
        raise _invalid(f"{stage} is deferred; clear the deferral (defer-stage with null) before confirming it")
    for check in row["checks"]:
        for ref in check["answer_refs"]:
            fresh = resolve_ref(texts, ref)
            if fresh is None:
                raise _invalid(f"confirm {stage}: the answer of {stage}.{check['id']} ({ref['part']}:{ref['region']}) "
                               "is not in this revision")
            ref["sha256"] = fresh  # the explicit confirmation refreshes the stored expected hashes
    links = [_link_view(link, record, evidence) for link in row["skills"]]
    problems = _requirements(record, row, links)
    if problems:
        raise _invalid(f"confirm {stage}: not confirmable yet: {'; '.join(problems)}")
    if stage == "confirm":
        report = evaluate(record, texts, evidence=evidence)
        waiting = [view["id"] for view in report["stages"] if view["id"] != "confirm"
                   and not (view["status"] in ("confirmed", "not-applicable") and not view["recheck"])
                   and not (view["status"] == "deferred" and view["deferral"]["impact"] == "minor")]
        if waiting:
            raise _invalid(f"confirm the design only after every other stage is resolved; waiting: {', '.join(waiting)}")
    fresh = digests(record, texts, evidence=evidence)
    current = basis(stage, fresh)
    if current is None:
        unknown = sorted({problem for name in catalog.reach_into(stage) for problem in fresh[name][1]})
        raise _invalid(f"confirm {stage}: an input it depends on cannot be read ({'; '.join(unknown) or 'unknown'})")
    row["confirmation"] = {"revision": revision, "basis": current, "at": iso(now)}


def _need_and_link(op: Mapping, stage: str, evidence: Evidence) -> tuple[dict, dict]:
    need = _validate_need(copy.deepcopy(op["need"]), stage)
    if need["resolution"] != "skill":
        raise _invalid("link-skill stores a need resolved by the skill it links")
    link = _validate_link(copy.deepcopy(op["link"]), stage, {need["id"]: need})
    review = evidence.review(link["review"]) if link["review"] else None
    if review is None:
        raise _invalid(f"link-skill {link['skill']}: review {link['review']} is not a reviewed skill in this state "
                       "folder; review it first (`oah skills review`)")
    if link["pin"] != review_pin(review):
        raise _invalid(f"link-skill {link['skill']}: the pin differs from its review")
    if link["license"] != review_license(review):
        raise _invalid(f"link-skill {link['skill']}: the license differs from its review")
    if (review.get("license") or {}).get("eligibility") != "eligible":
        raise _invalid(f"link-skill {link['skill']}: its license is not eligible under the policy")
    hosts = (review.get("compatibility") or {}).get("hosts") or []
    if any(host not in hosts for host in need["host"]):
        raise _invalid(f"link-skill {link['skill']}: the review supports {', '.join(hosts) or 'no host'}, not "
                       f"{', '.join(need['host'])}")
    model_invocable = (review.get("frontmatter") or {}).get("model_invocable")
    if model_invocable is False and need["invocation"] != "user-only":
        raise _invalid(f"link-skill {link['skill']}: the skill disables model invocation; its need is user-only")
    return need, link


def apply(record: Mapping, envelope: object, *, texts: Mapping[str, str], ref: Mapping, evidence: Evidence = NO_EVIDENCE,
          now: dt.datetime | None = None) -> dict:
    """A new record: `envelope` (CoverageEditV1) applied to `record`, all or nothing.

    `ref` is the design the edit must have been built from: {project, design, revision, digest} re-resolved from A.
    """
    envelope = check_envelope(envelope)
    expected = envelope["expected"]
    if any(expected[key] != ref[key] for key in EXPECTED_KEYS):
        from oah.design.model import CONTEXT_STALE_CODE

        raise DesignError(CONTEXT_STALE_CODE, f"the edit was built from {expected['project']}/{expected['design']} "
                          f"r{expected['revision']} {expected['digest']}, but the design is now r{ref['revision']} "
                          f"{ref['digest']}; read the coverage again and rebuild the edit")
    now = now or utc_now()
    before = digests(record, texts, evidence=evidence)
    work = copy.deepcopy(dict(record))
    confirmed: set[str] = set()
    for index, op in enumerate(envelope["ops"]):
        where = f"op {index + 1} ({op['op']})"
        kind = op["op"]
        if kind == "set-tech":
            label = op["label"]
            if label not in catalog.TECH_LABELS:
                raise _invalid(f"{where}: unknown tech label {label!r}")
            _ident(op["decision"], f"{where} decision", optional=True)
            _note(op["note"], f"{where} note")
            if op["value"] == "undecided":
                work["tech"].pop(label, None)
            else:
                work["tech"][label] = {"value": _tech_value(label, op["value"]), "decision": op["decision"],
                                       "note": op["note"]}
            continue
        row = _row(work, op["stage"])
        stage = op["stage"]
        if kind == "set-applicability":
            if op["value"] not in APPLICABILITY:
                raise _invalid(f"{where}: value is applies, not-applicable or undecided")
            if not catalog.BY_ID[stage].may_be_na and op["value"] != "applies":
                raise _invalid(f"{where}: {stage} always applies; only access, server and payment may be "
                               "not-applicable")
            if op["value"] == "not-applicable":
                row["reason"] = _text(op["reason"], f"{where} reason")
            elif op["reason"] is not None:
                raise _invalid(f"{where}: only not-applicable takes a reason")
            else:
                row["reason"] = None
            row["applicability"] = op["value"]
        elif kind == "set-check":
            check = next((item for item in row["checks"] if item["id"] == _check_of(stage, op["check"])), None)
            assert check is not None
            new = {key: copy.deepcopy(op[key]) for key in CHECK_KEYS if key != "id"}
            new["id"] = op["check"]
            _validate_check(new, stage, op["check"])
            for answer in new["answer_refs"]:
                _check_ref_now(texts, answer, where)
            check.update(new)
        elif kind == "defer-stage":
            if op["deferral"] is not None:
                deferral = _keys(op["deferral"], DEFERRAL_KEYS, f"{where} deferral")
                _text(deferral["reason"], f"{where} deferral reason")
                _resolve_by(deferral["resolve_by"], f"{where} deferral resolve_by")
                if deferral["impact"] not in IMPACTS:
                    raise _invalid(f"{where}: deferral impact is minor or outcome")
            row["deferral"] = copy.deepcopy(op["deferral"])
        elif kind == "confirm-stage":
            confirmation = _keys(op["user_confirmation"], {"source_ref", "confirmed"}, f"{where} user_confirmation")
            _hash(confirmation["source_ref"], f"{where} source_ref")
            if confirmation["confirmed"] is not True:
                raise _invalid(f"{where}: a confirmation records the user's yes (confirmed: true)")
            _confirm(work, stage, texts, int(expected["revision"]), evidence, now)
            confirmed.add(stage)
        elif kind == "clear-recheck":
            row["recheck"] = []
        elif kind == "link-skill":
            need, link = _need_and_link(op, stage, evidence)
            row["needs"] = [item for item in row["needs"] if item["id"] != need["id"]] + [need]
            row["skills"] = [item for item in row["skills"]
                             if not (item["need_id"] == need["id"] and item["skill"] == link["skill"])] + [link]
            row["gaps"] = [gap for gap in row["gaps"] if gap["need_id"] != need["id"]]
        elif kind == "unlink-skill":
            _text(op["reason"], f"{where} reason")
            before_count = len(row["skills"])
            row["skills"] = [item for item in row["skills"]
                             if not (item["need_id"] == op["need_id"] and item["skill"] == op["skill"])]
            if len(row["skills"]) == before_count:
                raise _invalid(f"{where}: {op['skill']} is not linked to need {op['need_id']} in {stage}")
            # The need stays, unchanged: a stored skill resolution without its link blocks until relinked or gapped.
        elif kind == "add-gap":
            need = next((item for item in row["needs"] if item["id"] == op["need_id"]), None)
            if need is None:
                raise _invalid(f"{where}: need {op['need_id']} is not stored in {stage}")
            _check_of(stage, op["check"])
            if need["check"] not in (None, op["check"]):
                raise _invalid(f"{where}: need {op['need_id']} belongs to {stage}.{need['check']}, not {op['check']}")
            if op["handled_by"] not in GAP_HANDLERS:
                raise _invalid(f"{where}: handled_by is one of {', '.join(GAP_HANDLERS)}")
            refs = [_hash(item, f"{where} source ref") for item in _list(op["source_refs"], f"{where} source refs")]
            for item in refs:
                if evidence.source(item) is None and evidence.review(item) is None:
                    raise _invalid(f"{where}: source ref {item} is not a verified source or review in this state "
                                   "folder")
            decision = _ref(op["decision_ref"], f"{where} decision_ref")
            _check_ref_now(texts, decision, where)
            note = _text(op["note"], f"{where} note")
            if not refs and op["handled_by"] != "oah-questions":
                raise _invalid(f"{where}: only an oah-questions gap may have no source refs")
            need["resolution"], need["review"] = "gap", None
            need["gap"] = {"handler": op["handled_by"], "reason": note, "source_refs": refs, "decision_ref": decision}
            gap = {"version": 1, "need_id": need["id"], "check": op["check"], "handled_by": op["handled_by"],
                   "note": note, "source_refs": refs, "decision_ref": decision}
            gap = {"id": gap_id(gap), **gap}
            row["gaps"] = [item for item in row["gaps"] if item["need_id"] != need["id"]] + [gap]
            row["skills"] = [item for item in row["skills"] if item["need_id"] != need["id"]]
    work = _validate(work)
    after = digests(work, texts, evidence=evidence)
    changed = [name for name in catalog.STAGE_IDS if before[name][0] != after[name][0]]
    for row in work["stages"]:
        if row["confirmation"] is None or row["id"] in confirmed:
            continue
        reach = set(catalog.reach_into(row["id"]))
        for name in changed:
            if name in reach:
                note = {"because": f"input:{name}", "from": (before[name][0] or "unknown")[:19],
                        "to": (after[name][0] or "unknown")[:19], "noted_at": iso(now)}
                if not any(item["because"] == note["because"] and item["to"] == note["to"] for item in row["recheck"]):
                    row["recheck"] = [*row["recheck"], note][-64:]
    text = render(work)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise _invalid(f"the new coverage part would be over {MAX_BYTES} bytes")
    return work


def summary(envelope: Mapping) -> str:
    """A short, deterministic save reason for an edit."""
    parts = []
    for op in envelope["ops"]:
        target = op.get("stage") or op.get("label") or ""
        if op["op"] == "set-check":
            target = f"{op['stage']}.{op['check']}"
        parts.append(f"{op['op']} {target}".strip())
    text = "Coverage: " + (", ".join(dict.fromkeys(parts)) or "re-render")
    return text if len(text) <= 160 else text[:157] + "..."
