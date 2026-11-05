# 출처·재료 판정 요청 — 목적마다 닫힌 허용 목록과 태그 붙은 필드(검증한 공개 스냅숏 조각·고정 라벨·허락받은 설계 원문)로만 요청을 만들고, 게이트·문자열 검사·신원 재확인을 거쳐 선택을 조언으로만 남긴다.
"""Judgement requests for design sources and materials, with per-purpose public provenance.

Every outgoing value is one of three tagged fields, never a caller string:

- `PublicSnapshotField` - a bounded UTF-8 slice of a verified public snapshot (GitHub or official https document),
  read and rehashed from this clone's source store by `public_field`; the caller names the slice, never its text;
- `PublicLabel` - a value from a closed oah enum (`LABELS`);
- `DesignTextField` - a bounded slice of the design's own text (`design_field`), sent only when
  `[rerank.judgement] send_design_text = true`.

Instructions and criteria come from closed versioned rubrics (`RUBRICS`); options are opaque keys. Gate order:
purpose, rubric and the purpose's closed field allowlist (`unused(scope)`), the design-text permission
(`unused(design-text-off)`) and required inputs (`unused(insufficient-input)`); then `[rerank.judgement] enabled`
(`unused(judgement-off)`); then the shared rerank gate (brief, provider, key, breaker); then every outgoing string,
templates included, is screened with the privacy scanner's content rules plus an e-mail rule (`unused(secret)`).
Nothing is sent before every gate passes. A result records its full input identity (constructor and rubric
versions, snapshot and design refs, slice digests, policy, provider and requested model) and is advisory: `recheck`
rederives every identity before a consumer applies it or a cached result. No judgement grants authority,
eligibility, tested status, installation, candidate dropping or carry.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
from typing import Mapping, Sequence

from oah import secrets
from oah.rerank import USED, _call, _gate, circuit, unused
from oah.rerank.base import BAD_RESPONSE, Provider, ProviderError
from oah.toolkit.checks import CONTENT_RULES

CONSTRUCTOR_VERSION = "judge-fields-v1"
FIELD_BYTES = 4096
TOTAL_CHARACTERS = 16_000
OPTION_LIMIT = 20
DEFAULTS = {"enabled": False, "send_design_text": False}
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

LABELS = {
    "role": ("chosen", "alternative", "candidate", "dependency"),
    "version-delta": ("major", "minor", "patch", "prerelease", "metadata-only", "unknown"),
    "platform": ("macos", "windows", "linux", "web", "ios", "android", "any"),
    "runtime": ("python", "node", "rust", "go", "java", "dotnet", "other"),
    "handler": ("github-release", "github-commit", "official-doc"),
    "technology": ("library", "framework", "service", "tool", "document"),
    "category": ("storage", "network", "ui", "auth", "build", "test", "docs", "other"),
    "stage": ("start", "resume", "revise", "recommend", "implement", "review"),
}

# purpose -> (public roles: allowed tags), (authored roles), whether authored input is mandatory
PURPOSES = {
    "source-change-impact": ({"release_note_excerpt": "snapshot", "document_excerpt": "snapshot",
                              "file_excerpt": "snapshot", "version_delta": "label:version-delta",
                              "role": "label:role"}, {"relevance"}, False),
    "material-classes": ({"manifest_excerpt": "snapshot", "document_excerpt": "snapshot",
                          "category": "label:category"}, {"categories", "need"}, False),
    "material-fit": ({"capability_excerpt": "snapshot", "compatibility_excerpt": "snapshot", "role": "label:role",
                      "platform": "label:platform"}, {"need", "constraints", "acceptance", "decision"}, False),
    "candidate-priority": ({"evidence_excerpt": "snapshot"}, {"preference", "risk"}, False),
    "search-path": ({"handler": "label:handler", "technology": "label:technology",
                     "candidate_description": "snapshot"}, {"query"}, False),
    "stack-comparison": ({"component_excerpt": "snapshot", "runtime": "label:runtime", "platform": "label:platform"},
                         {"stack_description", "assumptions", "tradeoffs"}, False),
    "next-stage": ({"stage": "label:stage"}, {"answers", "unresolved", "context"}, True),
}

RUBRICS = {
    "source-change-impact/v1": ("source-change-impact", "How much does the public change between the old and new "
                                "snapshot excerpts matter to a design that uses this source?",
                                {"none": "Nothing in the excerpts changes behavior, interfaces or terms.",
                                 "review": "Something changed that a person should read before relying on it.",
                                 "reselect": "The change is large enough that the choice should be made again."}),
    "material-classes/v1": ("material-classes", "Which category best describes this public material?",
                            {label: f"The material is mainly about {label}." for label in LABELS["category"]}),
    "material-fit/v1": ("material-fit", "Which option's public evidence fits the stated role and platform best?", None),
    "candidate-priority/v1": ("candidate-priority", "Which option should be looked at first, from its public "
                              "evidence?", None),
    "search-path/v1": ("search-path", "Which handler should look for candidates first?", None),
    "stack-comparison/v1": ("stack-comparison", "Which option's components fit the runtime and platform best?", None),
    "next-stage/v1": ("next-stage", "Which stage should come next?",
                      {label: f"Go to the {label} stage." for label in LABELS["stage"]}),
}


class JudgeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass(frozen=True)
class PublicSnapshotField:
    snapshot_ref: str
    path_or_field: str
    offset: int
    length: int
    bytes_digest: str
    text: str = dataclasses.field(repr=False, compare=False, default="")

    def identity(self) -> dict:
        return {"tag": "public-snapshot", "snapshot_ref": self.snapshot_ref, "path_or_field": self.path_or_field,
                "offset": self.offset, "length": self.length, "bytes_digest": self.bytes_digest}


@dataclasses.dataclass(frozen=True)
class PublicLabel:
    enum_id: str
    value: str

    @property
    def text(self) -> str:
        return self.value

    def identity(self) -> dict:
        return {"tag": "public-label", "enum_id": self.enum_id, "value": self.value}


@dataclasses.dataclass(frozen=True)
class DesignTextField:
    design_ref: Mapping
    part_or_decision_input: str
    offset: int
    length: int
    bytes_digest: str
    text: str = dataclasses.field(repr=False, compare=False, default="")

    def identity(self) -> dict:
        return {"tag": "design-text", "design_ref": dict(self.design_ref),
                "part_or_decision_input": self.part_or_decision_input, "offset": self.offset, "length": self.length,
                "bytes_digest": self.bytes_digest}


Field = PublicSnapshotField | PublicLabel | DesignTextField


def _slice(data: bytes, offset: int, length: int, what: str) -> tuple[str, str]:
    if type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= FIELD_BYTES \
            or offset + length > len(data):
        raise JudgeError("OAH_JUDGE_FIELD_INVALID", f"{what}: the slice must lie inside the text and be 1 to "
                         f"{FIELD_BYTES} bytes")
    part = data[offset:offset + length]
    try:
        text = part.decode("utf-8")
    except UnicodeDecodeError:
        raise JudgeError("OAH_JUDGE_FIELD_INVALID", f"{what}: the slice is not whole UTF-8 text") from None
    return text, hashlib.sha256(part).hexdigest()


def _public_bytes(sources, snapshot_ref: str, path_or_field: str) -> bytes:
    manifest = sources.snapshot(snapshot_ref)
    if manifest is None or manifest.get("verified") is not True:
        raise JudgeError("OAH_JUDGE_NOT_PUBLIC", f"{snapshot_ref} is not a verified snapshot in this clone")
    spec = manifest.get("spec") or {}
    if spec.get("provider") not in ("github", "http") or not str(spec.get("locator", "")).startswith("https://"):
        raise JudgeError("OAH_JUDGE_NOT_PUBLIC", f"{snapshot_ref} does not come from a supported public collector")
    sha = None
    if path_or_field == "release-notes":
        sha = (((manifest.get("observed") or {}).get("release") or {}).get("notes") or {}).get("sha256")
    elif path_or_field == "document-text":
        sha = (manifest.get("document") or {}).get("text_sha256")
    else:
        names = {item["path"]: item["sha256"] for item in manifest.get("files") or []}
        names.update({item["path"]: item["sha256"] for item in (manifest.get("license") or {}).get("files") or []})
        sha = names.get(path_or_field)
    data = None if sha is None else sources.blob(sha)
    if data is None:
        raise JudgeError("OAH_JUDGE_NOT_PUBLIC", f"{path_or_field} is not stored bytes of {snapshot_ref}")
    return data


def public_field(sources, snapshot_ref: str, path_or_field: str, offset: int, length: int) -> PublicSnapshotField:
    """A slice of a verified public snapshot; the outgoing text is read here, never supplied by the caller."""
    text, digest = _slice(_public_bytes(sources, snapshot_ref, path_or_field), offset, length, path_or_field)
    return PublicSnapshotField(snapshot_ref, path_or_field, offset, length, digest, text)


def label(enum_id: str, value: str) -> PublicLabel:
    if enum_id not in LABELS or value not in LABELS[enum_id]:
        raise JudgeError("OAH_JUDGE_FIELD_INVALID", f"{enum_id}={value!r} is not a fixed oah label")
    return PublicLabel(enum_id, value)


def _design_bytes(view, part_or_decision_input: str) -> bytes:
    kind, _, name = part_or_decision_input.partition(":")
    if kind == "part" and name and name != "materials" and name in view.current.texts:
        return view.current.texts[name].encode("utf-8")
    if kind == "decision" and name:
        from oah.design import materials

        text = view.current.texts.get("materials")
        record = None if text is None else materials.parse(text)
        inputs = None if record is None or record.get("version") != materials.SCHEMA else \
            record["decision_inputs"].get(name)
        if inputs is not None:
            return json.dumps(inputs, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raise JudgeError("OAH_JUDGE_FIELD_INVALID", f"{part_or_decision_input} is not a design part (part:<name>) or "
                     "decision input (decision:<id>) of the current revision")


def design_field(view, part_or_decision_input: str, offset: int, length: int) -> DesignTextField:
    """A slice of the design's own current text: authored, so it needs send_design_text to be sent."""
    text, digest = _slice(_design_bytes(view, part_or_decision_input), offset, length, part_or_decision_input)
    return DesignTextField(view.current.ref.to_json(), part_or_decision_input, offset, length, digest, text)


@dataclasses.dataclass(frozen=True)
class Request:
    purpose: str
    rubric: str
    fields: tuple[tuple[str, object], ...]     # (role, tagged field); per-option roles are "<option>:<role>"
    options: tuple[str, ...] = ()              # opaque keys when the rubric has no fixed options


@dataclasses.dataclass(frozen=True)
class Judgement:
    status: str                    # used | unused(<reason>)
    purpose: str
    identity: Mapping | None
    choice: str | None = None
    confidence: float | None = None
    provider: str | None = None
    applied: str = "not-applied"   # a consumer sets applied only after `recheck` passes

    def to_record(self) -> dict:
        return {"version": 1, "kind": "oah-judgement", "status": self.status, "purpose": self.purpose,
                "identity": None if self.identity is None else dict(self.identity), "choice": self.choice,
                "confidence": self.confidence, "provider": self.provider, "applied": self.applied}


def _options(request: Request) -> dict[str, str] | None:
    entry = RUBRICS.get(request.rubric)
    if entry is None:
        return None
    fixed = entry[2]
    if fixed is not None:
        return None if request.options else dict(fixed)
    keys = request.options
    if not 2 <= len(keys) <= OPTION_LIMIT or keys != tuple(f"o{index}" for index in range(len(keys))):
        return None
    return {key: f"Option {key}: the fields whose role starts with `{key}:`." for key in keys}


def _scope_ok(request: Request) -> tuple[bool, bool]:
    """(allowed, has authored fields) against the purpose's closed allowlist; tags must match exactly."""
    public, authored, _required = PURPOSES[request.purpose]
    has_authored = False
    for role, value in request.fields:
        if not isinstance(role, str):
            return False, has_authored
        option, sep, bare = role.partition(":")
        if sep:
            if option not in request.options:
                return False, has_authored
        else:
            bare = option
        expected = public.get(bare)
        if expected == "snapshot" and type(value) is PublicSnapshotField:
            continue
        if expected and expected.startswith("label:") and type(value) is PublicLabel \
                and value.enum_id == expected[6:] and value.value in LABELS.get(value.enum_id, ()):
            continue
        if bare in authored and type(value) is DesignTextField:
            has_authored = True
            continue
        return False, has_authored
    return True, has_authored


def identity_of(request: Request, *, policy: str, provider: str | None, model: str | None) -> dict:
    return {"version": 1, "constructor": CONSTRUCTOR_VERSION, "purpose": request.purpose, "rubric": request.rubric,
            "options": list(request.options),
            "fields": [{"role": role, **value.identity()} for role, value in request.fields],
            "policy": policy, "provider": provider, "requested_model": model}


def _flagged(text: str) -> bool:
    return bool(_EMAIL.search(text)) or any(pattern.search(text) for _rule, pattern in CONTENT_RULES)


def _fields_hold(request: Request, sources, view) -> bool:
    """Every slice's text matches its digest and, given the store and view, the bytes they hold now."""
    for _role, value in request.fields:
        if type(value) in (PublicSnapshotField, DesignTextField):
            if hashlib.sha256(value.text.encode("utf-8")).hexdigest() != value.bytes_digest:
                return False
            try:
                if type(value) is PublicSnapshotField and sources is not None:
                    data = _public_bytes(sources, value.snapshot_ref, value.path_or_field)
                elif type(value) is DesignTextField and view is not None:
                    if dict(value.design_ref) != view.current.ref.to_json():
                        return False
                    data = _design_bytes(view, value.part_or_decision_input)
                else:
                    continue
                if _slice(data, value.offset, value.length, "field")[1] != value.bytes_digest:
                    return False
            except JudgeError:
                return False
    return True


def judge(request: Request, *, config: Mapping, policy: str, sources=None, view=None, brief_disabled: bool = False,
          env: Mapping[str, str] | None = None, keychain: secrets.Keychain | None = None,
          providers: Mapping[str, Provider] | None = None, state_dir: pathlib.Path | None = None) -> Judgement:
    """One advisory choice for a closed purpose, or `unused(<reason>)` with nothing sent.

    With `sources` (and `view` for design text) every slice is read again from the rehashed store before sending."""
    provider = key = None
    try:
        if request.purpose not in PURPOSES or request.rubric not in RUBRICS \
                or RUBRICS[request.rubric][0] != request.purpose:
            return Judgement(unused("scope"), str(request.purpose), None)
        options = _options(request)
        allowed, has_authored = _scope_ok(request)
        if options is None or not allowed or not _fields_hold(request, sources, view):
            return Judgement(unused("scope"), request.purpose, None)
        settings = config.get("judgement") if isinstance(config.get("judgement"), Mapping) else DEFAULTS
        if has_authored and settings.get("send_design_text") is not True:
            return Judgement(unused("design-text-off"), request.purpose, None)
        if PURPOSES[request.purpose][2] and not has_authored:
            return Judgement(unused("insufficient-input"), request.purpose, None)
        if settings.get("enabled") is not True:
            return Judgement(unused("judgement-off"), request.purpose, None)
        breaker = circuit.Breaker(state_dir, circuit.limits(config))
        provider, key, status = _gate(config, brief_disabled=brief_disabled, env=env, keychain=keychain,
                                      providers=providers)
        env = keychain = providers = None
        if provider is None:
            return Judgement(status, request.purpose, None)
        typesafe_settings = config.get("typesafe")
        model = str(typesafe_settings.get("model") or "") if isinstance(typesafe_settings, Mapping) else None
        identity = identity_of(request, policy=policy, provider=getattr(provider, "name", None), model=model)
        instructions = RUBRICS[request.rubric][1]
        state = {"purpose": request.purpose, "fields": {role: value.text for role, value in request.fields}}
        strings = [instructions, *options.values(), *state["fields"].values(), *state["fields"]]
        if sum(len(item) for item in strings) > TOTAL_CHARACTERS:
            return Judgement(unused("too-large"), request.purpose, identity)
        if any(_flagged(item) for item in strings):
            return Judgement(unused("secret"), request.purpose, identity)
        blocked = breaker.refusal() or breaker.reserve()
        if blocked is not None:
            return Judgement(unused(blocked), request.purpose, identity)
        answer, status = _call(lambda: _valid_choice(provider.judge(state, instructions, options, key=key,
                                                                    config=config), options))
        breaker.record(circuit.outcome(status))
        if status != USED:
            return Judgement(status, request.purpose, identity, provider=identity["provider"])
        return Judgement(USED, request.purpose, identity, answer[0], answer[1], identity["provider"])
    finally:
        del provider, key


def _valid_choice(answer: object, options: Mapping[str, str]) -> tuple[str, float | None]:
    if type(answer) is not tuple or len(answer) != 2 or answer[0] not in options:
        raise ProviderError(BAD_RESPONSE)
    confidence = answer[1]
    if confidence is not None and (type(confidence) not in (int, float) or not 0.0 <= confidence <= 1.0):
        raise ProviderError(BAD_RESPONSE)
    return answer[0], None if confidence is None else float(confidence)


def recheck(judgement: Judgement | Mapping, *, sources, view=None, policy: str, provider: str | None,
            model: str | None) -> list[str]:
    """Why a result or cached record may not be applied now ([] when every identity still holds)."""
    record = judgement.to_record() if isinstance(judgement, Judgement) else dict(judgement)
    identity = record.get("identity")
    if record.get("status") != USED or not isinstance(identity, Mapping):
        return ["not-used"]
    reasons = []
    if identity.get("constructor") != CONSTRUCTOR_VERSION:
        reasons.append("constructor-changed")
    if identity.get("policy") != policy:
        reasons.append("policy-changed")
    if identity.get("provider") != provider or identity.get("requested_model") != model:
        reasons.append("provider-changed")
    for field in identity.get("fields") or []:
        try:
            if field.get("tag") == "public-snapshot":
                data = _public_bytes(sources, field["snapshot_ref"], field["path_or_field"])
                digest = _slice(data, field["offset"], field["length"], field["path_or_field"])[1]
            elif field.get("tag") == "design-text":
                if view is None or dict(field.get("design_ref") or {}) != view.current.ref.to_json():
                    reasons.append(f"design-changed:{field.get('role')}")
                    continue
                data = _design_bytes(view, field["part_or_decision_input"])
                digest = _slice(data, field["offset"], field["length"], field["part_or_decision_input"])[1]
            else:
                continue
        except (JudgeError, KeyError, TypeError):
            reasons.append(f"field-unavailable:{field.get('role')}")
            continue
        if digest != field.get("bytes_digest"):
            reasons.append(f"field-changed:{field.get('role')}")
    return reasons


def cache_key(identity: Mapping) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(dict(identity), sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False).encode("utf-8")).hexdigest()
