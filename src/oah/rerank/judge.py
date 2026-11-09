# 출처·재료 판정 요청 — 목적마다 닫힌 허용 목록과 태그 붙은 필드(검증한 공개 스냅숏 조각·고정 라벨·허락받은 설계 원문)로만 요청을 만들고, 게이트·문자열 검사·신원 재확인을 거쳐 선택을 조언으로만 남긴다.
"""Judgement requests for design sources and materials, with per-purpose public provenance.

Every outgoing value is one of three tagged fields, never a caller string:

- `PublicSnapshotField` - a bounded UTF-8 slice of a verified public snapshot (GitHub or official https document),
  read and rehashed from this clone's source store by `public_field`; the caller names the slice, never its text;
- `PublicLabel` - a value from a closed oah enum (`LABELS`);
- `DesignTextField` - a bounded slice of the design's own text (`design_field`), sent only when
  `[rerank.judgement] send_design_text = true`;
- `TaskTextField` - a bounded slice of the task's accepted packet intent (`task_field`, read from a trusted
  `TaskView`), accepted only by the `capability-needs` purpose and sent only when
  `[rerank.judgement] send_task_text = true`.

Instructions and criteria come from closed versioned rubrics (`RUBRICS`); options are opaque keys. No question
forces an answer: a `ChoiceRubric` asks for one option together with a yes/no question on whether any option fits,
so the outcome is `chosen`, `no-match` or `uncertain`; a `LabelRubric` asks one yes/no question per label, so
several labels or none can hold (`matched`, `no-match` or `uncertain`). `THRESHOLDS` are the provider's suggested
starting points, not calibrated values, and are part of the recorded identity. Gate order:
purpose, rubric and the purpose's closed field allowlist (`unused(scope)`), a purpose's required roles
(`unused(insufficient-input)`), the design-text and task-text permissions (`unused(design-text-off)`,
`unused(task-text-off)`) and required authored inputs (`unused(insufficient-input)`); then `[rerank.judgement] enabled`
(`unused(judgement-off)`); then every outgoing string, templates included, is screened with the privacy scanner's
content rules plus an e-mail rule (`unused(secret)`); then the shared rerank gate (brief, provider, key, breaker).
Nothing is sent before every gate passes. A result records its full input identity (constructor and rubric
versions, snapshot and design refs, slice digests, policy, provider and requested model) and is advisory: `recheck`
rederives every identity before a consumer applies it or a cached result. No judgement grants authority,
eligibility, tested status, installation, candidate dropping or carry.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import pathlib
import re
import sys
from typing import Callable, Mapping, Sequence

from oah import secrets
from oah.capabilities import CLASSES as CAPABILITY_CLASSES
from oah.rerank import USED, _call, _gate, circuit, unused
from oah.rerank.base import BAD_RESPONSE, JudgeReply, Provider, ProviderError
from oah.rerank.judgement_policy import DEFAULTS, settings as judgement_settings
from oah.rerank.typesafe import DEFAULT_MODEL, PROBABILITY_SUM_TOLERANCE, SCORE_TOLERANCE
from oah.toolkit.checks import CONTENT_RULES

CONSTRUCTOR_VERSION = "judge-fields-v4"
FIELD_BYTES = 4096
TOTAL_CHARACTERS = 16_000
OPTION_LIMIT = 50
# A yes/no answer at or above `yes` reads as yes, at or below `no` as no, and between as uncertain; a chosen option
# also needs at least `choice_min`. Starting points from the provider's guidance, not calibrated product values.
THRESHOLDS = {key: DEFAULTS[key] for key in ("yes", "no", "choice_min", "level_min")}
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
    "skill-provider": ("seed", "installed", "skills-sh"),
    "capability-class": CAPABILITY_CLASSES,
}
DESIGN_LABELS = ("design-stage", "design-check", "public-tech")  # oah's design vocabularies, read on first use


def label_values(enum_id: str) -> tuple[str, ...]:
    """The values of one closed label enum: `LABELS`, or the design stages, `stage.check` ids and public technology
    words of the design catalog (loaded here so this module imports no design code at import time)."""
    if enum_id in LABELS:
        return LABELS[enum_id]
    if enum_id not in DESIGN_LABELS:
        return ()
    from oah.design import stages

    if enum_id == "design-stage":
        return stages.STAGE_IDS
    if enum_id == "design-check":
        return tuple(f"{stage.id}.{check}" for stage in stages.STAGES for check in (*stage.checks, *stage.conditional))
    return tuple(sorted(stages.PUBLIC_QUERY_TERMS))


# purpose -> (public roles: allowed tags), (authored roles), whether authored input is mandatory
PURPOSES = {
    "source-change-impact": ({"release_note_excerpt": "snapshot", "document_excerpt": "snapshot",
                              "version_delta": "label:version-delta",
                              "role": "label:role", "old_release_note_excerpt": "snapshot",
                              "new_release_note_excerpt": "snapshot", "from_tag": "snapshot",
                              "to_tag": "snapshot"}, {"relevance"}, False),
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
    "design-stage": ({"stage": "label:design-stage"}, {"answers", "unresolved", "context"}, True),
    "skill-fit": ({"skill_excerpt": "snapshot", "compatibility_excerpt": "snapshot",
                   "design_stage": "label:design-stage", "check": "label:design-check", "tech": "label:public-tech",
                   "platform": "label:platform"}, {"need"}, False),
    "capability-needs": ({"capability_class": "label:capability-class", "capability_excerpt": "snapshot"},
                         {"task_need", "task_context"}, True),
}
# purpose -> (shared roles, roles every option needs) that must be present, else unused(insufficient-input)
REQUIRED_ROLES = {"skill-fit": (("design_stage", "check"), ("skill_excerpt",)),
                  "capability-needs": (("task_need",), ())}
TASK_TEXT_PURPOSES = frozenset({"capability-needs"})  # their authored roles take task text, never design text
# E's new rubrics carry their own exact allowlist (DESIGN §7.1, §7.3 step 1): (shared roles, roles every option has
# under its `oN:` prefix), each with its one allowed tag. A role in the wrong place, an unknown role or another tag
# is unused(scope). The legacy rubrics keep their purpose's allowlist unchanged.
RUBRIC_ROLES = {
    "skill-search/v1": ({"technology": "label:technology", "public_tech": "label:public-tech",
                         "query": "design-text"},
                        {"provider": "label:skill-provider", "public_tech": "label:public-tech",
                         "candidate_description": "snapshot"}),
    "design-stage/v1": ({"answers": "design-text", "unresolved": "design-text", "context": "design-text"},
                        {"stage": "label:design-stage"}),
    "capability-select/v1": ({"capability_class": "label:capability-class", "task_need": "task-text",
                              "task_context": "task-text"},
                             {"capability_excerpt": "snapshot"}),
}
# rubric -> (shared roles, per-option roles) that must be present, over REQUIRED_ROLES of its purpose
RUBRIC_REQUIRED = {"skill-search/v1": ((), ("provider",)),
                   "design-stage/v1": ((), ("stage",)),
                   "capability-select/v1": (("capability_class", "task_need"), ("capability_excerpt",))}
# rubric -> the most options one request may carry (DESIGN §7.1.1: at most eight qualified entries per need)
RUBRIC_OPTIONS = {"capability-select/v1": 8}

@dataclasses.dataclass(frozen=True)
class ChoiceRubric:
    """One option, asked together with a yes/no question on whether any option fits at all."""
    purpose: str
    instructions: str
    options: Mapping[str, str] | None   # fixed option criteria; None means opaque keys o0..oN
    fits: tuple[str, str, str]          # the companion yes/no question: instructions, true, false


@dataclasses.dataclass(frozen=True)
class LabelRubric:
    """One yes/no question per label, so several labels or none can hold; `{meaning}` is the label's meaning."""
    purpose: str
    instructions: str
    true: str
    false: str
    labels: Mapping[str, str]           # label -> meaning


@dataclasses.dataclass(frozen=True)
class OptionNoulRubric:
    """One yes/no question per opaque option o0..oN, so each candidate is judged on its own `oN:` fields; a reply
    that leaves an option out is `unused(error:dropped)`."""
    purpose: str
    instructions: str                   # `{option}` is the option key
    true: str
    false: str


@dataclasses.dataclass(frozen=True)
class ScoreRubric:
    purpose: str
    instructions: str
    levels: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ImpactRubric:
    purpose: str
    labels: LabelRubric
    urgency: ScoreRubric


RUBRICS: dict[str, ChoiceRubric | LabelRubric | OptionNoulRubric | ScoreRubric | ImpactRubric] = {
    "source-change-assessment/v1": ImpactRubric("source-change-impact", LabelRubric(
        "source-change-impact", "Do the public change excerpts show {meaning}?",
        "The excerpts give concrete evidence of {meaning}.",
        "The excerpts show no evidence of {meaning}; do not infer it merely from the version number.",
        {"may_break_users": "a compatibility change that may break existing users",
         "security_fix": "a security vulnerability being fixed",
         "drops_platform": "support for a platform being removed"}),
        ScoreRubric("source-change-impact", "How urgently should a user of this source review this public change?",
                    ("Can wait for routine review", "Review before the next update", "Review now for a serious risk"))),
    "source-change-impact/v2": ChoiceRubric(
        "source-change-impact",
        "How much does the public change between the old and new snapshot excerpts matter to a design that uses "
        "this source?",
        {"none": "Nothing in the excerpts changes behavior, interfaces or terms.",
         "review": "Something changed that a person should read before relying on it.",
         "reselect": "The change is large enough that the choice should be made again."},
        ("Do the excerpts show what changed between the two snapshots?",
         "The excerpts describe a change, such as a fix, a new feature, a removal or new terms.",
         "The excerpts describe no change, for example both are the same license text or an unrelated passage.")),
    "material-classes/v2": LabelRubric(
        "material-classes", "Is this public material about {meaning}?",
        "The material is about {meaning}, as its main subject or as one of several.",
        "The material is about other things and mentions {meaning} only in passing, if at all.",
        {"storage": "keeping data, such as databases, files, caches or object stores",
         "network": "talking over a network, such as HTTP, sockets, messaging or remote calls",
         "ui": "what people see and use, such as screens, widgets, layout or styling",
         "auth": "who may do what, such as sign-in, sessions or permissions",
         "build": "turning code into something that runs, such as compiling, bundling or packaging",
         "test": "checking that code works, such as test runners, fixtures or coverage",
         "docs": "explaining the software to people, such as guides, references or examples"}),
    "material-fit/v2": ChoiceRubric(
        "material-fit", "Which option's public evidence fits the stated role and platform best?", None,
        ("Does any option's public evidence fit the stated role and platform?",
         "At least one option's evidence shows that it can fill the role on the platform.",
         "No option's evidence covers the role or the platform, for example every option targets another "
         "platform.")),
    "candidate-priority/v2": ChoiceRubric(
        "candidate-priority", "Which option should be looked at first, from its public evidence?", None,
        ("Does any option's public evidence give a reason to look at it?",
         "At least one option's evidence shows something worth checking, such as a matching feature or recent work.",
         "No option's evidence shows anything worth checking, for example every excerpt is empty or boilerplate.")),
    "search-path/v2": ChoiceRubric(
        "search-path", "Which handler should look for candidates first?", None,
        ("Can any of these handlers find candidates for the technology described?",
         "At least one handler covers where this kind of technology is published.",
         "None of the handlers covers it, for example the technology publishes neither releases nor documents.")),
    "stack-comparison/v2": ChoiceRubric(
        "stack-comparison", "Which option's components fit the runtime and platform best?", None,
        ("Do any option's components fit the runtime and platform?",
         "At least one option's components run on the runtime and the platform.",
         "No option's components run there, for example every option needs another runtime.")),
    "next-stage/v2": ChoiceRubric(
        "next-stage", "Which stage should come next?",
        {label: f"Go to the {label} stage." for label in LABELS["stage"]},
        ("Does any of these stages fit as the next step?",
         "One of the stages follows from the answers and the unresolved decisions.",
         "None of the stages fits, for example the work waits on a decision these stages do not cover.")),
    "skill-fit/v1": OptionNoulRubric(
        "skill-fit", "Does {option}:skill_excerpt give concrete guidance for the check named in check, at the design "
        "stage named in design_stage?",
        "The excerpt explains actions or constraints for that check.",
        "The excerpt mentions the area without usable guidance for that check."),
    "skill-search/v1": ChoiceRubric(
        "search-path", "Which permitted provider or template covers this public technology need?", None,
        ("Does any permitted provider or template cover this public technology need?",
         "At least one option covers where skills for this public technology are published.",
         "None of the options covers it, for example every option is about another technology.")),
    "design-stage/v1": ChoiceRubric(
        "design-stage", "Which candidate design stage covers the unresolved decision in context?", None,
        ("Does any candidate stage cover that unresolved decision?",
         "At least one candidate's stage or its checks cover the unresolved decision.",
         "The decision is outside every candidate stage.")),
    "capability-select/v1": ChoiceRubric(
        "capability-needs", "Which candidate capability's documented function fits task_need?", None,
        ("Does any candidate's documented function fit task_need?",
         "At least one excerpt describes a function needed for the bounded task.",
         "The excerpts describe functions outside that task, even if their capability class is relevant.")),
    "capability-needs/v1": LabelRubric(
        "capability-needs", "Does task_need require {meaning}?",
        "Accomplishing this bounded task needs {meaning}.",
        "The task does not need {meaning}: it is unrelated or merely convenient.",
        {"library-docs": "reading current documentation of a library or API",
         "web-search": "searching the public web",
         "web-browse": "opening and reading specific web pages",
         "code-host": "working with a code hosting service, such as its repositories or pull requests",
         "issue-tracker": "reading or changing issues in an issue tracker",
         "database": "querying or changing a database",
         "design-files": "reading design files, such as mockups or prototypes",
         "cloud-console": "operating a cloud provider's console or resources",
         "payment-docs": "reading a payment provider's documentation",
         "messaging": "reading or sending messages in a chat or messaging service",
         "file-storage": "reading or writing files in a storage service",
         "browser-automation": "driving a web browser, such as clicking and filling forms",
         "diagramming": "drawing or reading diagrams"}),
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


@dataclasses.dataclass(frozen=True)
class TaskTextField:
    task_ref: Mapping
    source: str
    offset: int
    length: int
    bytes_digest: str
    text: str = dataclasses.field(repr=False, compare=False, default="")

    def identity(self) -> dict:
        return {"tag": "task-text", "task_ref": dict(self.task_ref), "source": self.source, "offset": self.offset,
                "length": self.length, "bytes_digest": self.bytes_digest}


@dataclasses.dataclass(frozen=True)
class TaskView:
    """The task text a TaskTextField may slice, bound to the task, packet and intent the caller verified: only the
    accepted packet's intent section (`intent`). Build it from a checked packet with `of`, never from caller text."""
    ref: Mapping
    texts: Mapping[str, str]

    @classmethod
    def of(cls, task: str, packet) -> "TaskView":
        section = packet.section or ""
        intent = packet.intent
        ref = {"task": task, "source": packet.source, "intent": f"{intent.id}@r{intent.revision}",
               "intent_digest": intent.digest,
               "section_sha256": "sha256:" + hashlib.sha256(section.encode("utf-8")).hexdigest()}
        return cls(ref, {"intent": section})


Field = PublicSnapshotField | PublicLabel | DesignTextField | TaskTextField


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
    if path_or_field == "release-tag":
        tag = (((manifest.get("observed") or {}).get("release") or {}).get("tag"))
        if not isinstance(tag, str) or not tag:
            raise JudgeError("OAH_JUDGE_NOT_PUBLIC", "the verified snapshot has no release tag")
        return tag.encode("utf-8")
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


def public_prefix(sources, snapshot_ref: str, path_or_field: str, limit: int = FIELD_BYTES) -> PublicSnapshotField:
    """The longest prefix of a verified public file that is at most `limit` bytes and ends on a whole character."""
    data = _public_bytes(sources, snapshot_ref, path_or_field)
    size = min(len(data), limit)
    while 0 < size < len(data) and data[size] & 0xC0 == 0x80:
        size -= 1  # data[size] continues a character, so the prefix would cut it
    text, digest = _slice(data, 0, size, path_or_field)
    return PublicSnapshotField(snapshot_ref, path_or_field, 0, size, digest, text)


def label(enum_id: str, value: str) -> PublicLabel:
    if value not in label_values(enum_id):
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


def _task_bytes(task_view, source: str) -> bytes:
    if not isinstance(task_view, TaskView) or source not in task_view.texts:
        raise JudgeError("OAH_JUDGE_FIELD_INVALID", f"{source} is not task text of a verified task view (only the "
                         "packet's intent section is)")
    return task_view.texts[source].encode("utf-8")


def task_field(task_view, source: str, offset: int, length: int) -> TaskTextField:
    """A slice of the task's accepted packet intent: authored, so it needs send_task_text to be sent."""
    text, digest = _slice(_task_bytes(task_view, source), offset, length, source)
    return TaskTextField(dict(task_view.ref), source, offset, length, digest, text)


@dataclasses.dataclass(frozen=True)
class Request:
    purpose: str
    rubric: str
    fields: tuple[tuple[str, object], ...]     # (role, tagged field); per-option roles are "<option>:<role>"
    options: tuple[str, ...] = ()              # opaque keys when the rubric has no fixed options
    mandatory: frozenset[str] = frozenset()
    input_ref: str | None = None


@dataclasses.dataclass(frozen=True)
class Judgement:
    status: str                    # used | unused(<reason>)
    purpose: str
    identity: Mapping | None
    outcome: str | None = None     # chosen | matched | no-match | uncertain, read under the recorded THRESHOLDS
    choice: str | None = None      # the option, only when the outcome is chosen
    labels: Mapping[str, str] | None = None   # label -> yes | no | uncertain, for a label rubric
    answers: Mapping | None = None            # the validated answers the outcome was read from
    provider: str | None = None
    applied: str = "not-applied"   # a consumer sets applied only after `recheck` passes
    answered_model: str | None = None
    assessed_at: str | None = None
    score_level: str | None = None

    def to_record(self) -> dict:
        return {"version": 1, "kind": "oah-judgement", "status": self.status, "purpose": self.purpose,
                "identity": None if self.identity is None else dict(self.identity), "outcome": self.outcome,
                "choice": self.choice, "labels": None if self.labels is None else dict(self.labels),
                "answers": None if self.answers is None else json.loads(json.dumps(self.answers)),
                "provider": self.provider, "applied": self.applied, "answered_model": self.answered_model,
                "assessed_at": self.assessed_at, "score_level": self.score_level}


def _questions(request: Request) -> dict[str, dict] | None:
    """The closed question set for the request, keyed by question id, or None when the options do not fit."""
    rubric = RUBRICS.get(request.rubric)
    if isinstance(rubric, ImpactRubric):
        if request.options:
            return None
        labels = rubric.labels
        questions = {name: {"type": "noul", "instructions": labels.instructions.replace("{meaning}", meaning),
                            "criteria": {"true": labels.true.replace("{meaning}", meaning),
                                         "false": labels.false.replace("{meaning}", meaning)}}
                     for name, meaning in labels.labels.items()}
        questions["urgency"] = {"type": "score", "instructions": rubric.urgency.instructions,
                                "criteria": list(rubric.urgency.levels)}
        return questions
    if isinstance(rubric, ScoreRubric):
        return None if request.options else {"score": {"type": "score", "instructions": rubric.instructions,
                                                        "criteria": list(rubric.levels)}}
    if isinstance(rubric, OptionNoulRubric):
        keys = request.options
        if not 2 <= len(keys) <= OPTION_LIMIT or keys != tuple(f"o{index}" for index in range(len(keys))):
            return None
        return {key: {"type": "noul", "instructions": rubric.instructions.replace("{option}", key),
                      "criteria": {"true": rubric.true, "false": rubric.false}} for key in keys}
    if isinstance(rubric, LabelRubric):
        if request.options:
            return None
        return {label: {"type": "noul", "instructions": rubric.instructions.replace("{meaning}", meaning),
                        "criteria": {"true": rubric.true.replace("{meaning}", meaning),
                                     "false": rubric.false.replace("{meaning}", meaning)}}
                for label, meaning in rubric.labels.items()}
    if not isinstance(rubric, ChoiceRubric):
        return None
    if rubric.options is not None:
        if request.options:
            return None
        options = dict(rubric.options)
    else:
        keys = request.options
        if not 2 <= len(keys) <= OPTION_LIMIT or keys != tuple(f"o{index}" for index in range(len(keys))):
            return None
        options = {key: f"Option {key}: the fields whose role starts with `{key}:`." for key in keys}
    instructions, true, false = rubric.fits
    return {"choice": {"type": "choice", "instructions": rubric.instructions, "criteria": options},
            "fits": {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}}


def _tag_ok(expected: str | None, value: object) -> bool:
    if expected == "snapshot":
        return type(value) is PublicSnapshotField
    if expected == "design-text":
        return type(value) is DesignTextField
    if expected == "task-text":
        return type(value) is TaskTextField
    return bool(expected and expected.startswith("label:") and type(value) is PublicLabel
                and value.enum_id == expected[6:] and value.value in label_values(value.enum_id))


def _rubric_scope_ok(request: Request) -> tuple[bool, bool, bool]:
    """The exact allowlist of one of E's rubrics: every role in its place with its one tag (RUBRIC_ROLES), and no
    more options than the rubric takes (RUBRIC_OPTIONS)."""
    shared, per_option = RUBRIC_ROLES[request.rubric]
    has_design = has_task = False
    if len(request.options) > RUBRIC_OPTIONS.get(request.rubric, OPTION_LIMIT):
        return False, has_design, has_task
    for role, value in request.fields:
        if not isinstance(role, str):
            return False, has_design, has_task
        option, sep, bare = role.partition(":")
        expected = (per_option.get(bare) if option in request.options else None) if sep else shared.get(option)
        if not _tag_ok(expected, value):
            return False, has_design, has_task
        has_design = has_design or expected == "design-text"
        has_task = has_task or expected == "task-text"
    return True, has_design, has_task


def _scope_ok(request: Request) -> tuple[bool, bool, bool]:
    """(allowed, has design text, has task text) against the purpose's closed allowlist; tags must match exactly.
    A purpose in TASK_TEXT_PURPOSES takes task text in its authored roles and never design text; every other purpose
    the reverse. One of E's rubrics in RUBRIC_ROLES uses its own exact allowlist instead."""
    if request.rubric in RUBRIC_ROLES:
        return _rubric_scope_ok(request)
    public, authored, _required = PURPOSES[request.purpose]
    authored_type = TaskTextField if request.purpose in TASK_TEXT_PURPOSES else DesignTextField
    has_design = has_task = False
    for role, value in request.fields:
        if not isinstance(role, str):
            return False, has_design, has_task
        option, sep, bare = role.partition(":")
        if sep:
            if option not in request.options:
                return False, has_design, has_task
        else:
            bare = option
        expected = public.get(bare)
        if expected == "snapshot" and type(value) is PublicSnapshotField:
            continue
        if expected and expected.startswith("label:") and type(value) is PublicLabel \
                and value.enum_id == expected[6:] and value.value in label_values(value.enum_id):
            continue
        if bare in authored and type(value) is authored_type:
            has_design = has_design or authored_type is DesignTextField
            has_task = has_task or authored_type is TaskTextField
            continue
        return False, has_design, has_task
    return True, has_design, has_task


def _missing_required(request: Request) -> bool:
    shared, per_option = RUBRIC_REQUIRED.get(request.rubric) or REQUIRED_ROLES.get(request.purpose, ((), ()))
    roles = {role for role, _value in request.fields}
    return not (set(shared) <= roles and all(f"{option}:{role}" in roles for option in request.options
                                              for role in per_option))


def identity_of(request: Request, *, policy: str, provider: str | None, model: str | None,
                settings: Mapping | None = None) -> dict:
    effective = judgement_settings({}) if settings is None else dict(settings)
    payload = {"state": {"purpose": request.purpose, "fields": {role: value.text for role, value in request.fields}},
               "questions": _questions(request)}
    request_digest = "sha256:" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {"version": 1, "constructor": CONSTRUCTOR_VERSION, "purpose": request.purpose, "rubric": request.rubric,
            "options": list(request.options),
            "mandatory": sorted(request.mandatory), "input_ref": request.input_ref,
            "fields": [{"role": role, **value.identity()} for role, value in request.fields],
            "policy": policy, "provider": provider, "requested_model": model,
            "settings": effective, "thresholds": {key: effective[key] for key in THRESHOLDS},
            "request_digest": request_digest}


def _flagged(text: str) -> bool:
    return bool(_EMAIL.search(text)) or any(pattern.search(text) for _rule, pattern in CONTENT_RULES)


def _resolved(request: Request, sources, view, task_view=None) -> Request | None:
    fields = []
    for role, value in request.fields:
        canonical = value
        if type(value) in (PublicSnapshotField, DesignTextField, TaskTextField):
            try:
                if type(value) is PublicSnapshotField and sources is not None:
                    canonical = public_field(sources, value.snapshot_ref, value.path_or_field, value.offset, value.length)
                elif type(value) is DesignTextField and view is not None:
                    if dict(value.design_ref) != view.current.ref.to_json():
                        return None
                    canonical = design_field(view, value.part_or_decision_input, value.offset, value.length)
                elif type(value) is TaskTextField and isinstance(task_view, TaskView):
                    if dict(value.task_ref) != dict(task_view.ref):
                        return None  # another task, packet or intent than the one verified now
                    canonical = task_field(task_view, value.source, value.offset, value.length)
                else:
                    return None
                if canonical.identity() != value.identity() or canonical.text != value.text:
                    return None
            except JudgeError:
                return None
        fields.append((role, canonical))
    return dataclasses.replace(request, fields=tuple(fields))


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclasses.dataclass
class Budget:
    remaining: int

    def reserve(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for key, child in value.items() for item in (key, child) for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for child in value for text in _strings(child)]
    return []


def _notice(state_dir: pathlib.Path | None, kind: str, notify: Callable[[str], None] | None) -> None:
    if state_dir is None:
        return
    from oah.atomic import StorageError
    from oah.design.sources.state import StateProblem
    from oah.lockfile import LockTimeout
    from oah.rerank.judgement_cache import CacheState

    try:
        state = CacheState(state_dir)
        state.root.mkdir(parents=True, exist_ok=True)
        name = f"notice-{kind}.json"
        with state.lock():
            if state.read_json(name, 1024) == {"version": 1, "shown": True}:
                return
            text = {"design": "OAH_RERANK_JUDGEMENT_DESIGN_NOTICE: permitted design excerpts also go to the judge.",
                    "task": "OAH_RERANK_JUDGEMENT_TASK_NOTICE: bounded excerpts of the task's packet intent also go "
                            "to the judge.",
                    "public": "OAH_RERANK_JUDGEMENT_NOTICE: verified public excerpts and fixed labels go to the "
                              "judge."}[kind]
            (notify or (lambda value: print(value, file=sys.stderr)))(text)
            state.write_json(name, {"version": 1, "shown": True})
    except (OSError, ValueError, StorageError, StateProblem, LockTimeout):
        return


def judge(request: Request, *, config: Mapping, policy: str, sources=None, view=None, brief_disabled: bool = False,
          env: Mapping[str, str] | None = None, keychain: secrets.Keychain | None = None,
          providers: Mapping[str, Provider] | None = None, state_dir: pathlib.Path | None = None,
          budget: Budget | None = None, notify: Callable[[str], None] | None = None,
          task_view: TaskView | None = None) -> Judgement:
    """One advisory outcome for a closed purpose, or `unused(<reason>)` with nothing sent.

    With `sources` (and `view` for design text, `task_view` for task text) every slice is read again from its
    verified origin before sending."""
    provider = key = None
    try:
        if request.purpose not in PURPOSES or request.rubric not in RUBRICS \
                or RUBRICS[request.rubric].purpose != request.purpose:
            return Judgement(unused("scope"), str(request.purpose), None)
        questions = _questions(request)
        if request.mandatory and (questions is None or not request.mandatory.issubset(
                (questions.get("choice") or {}).get("criteria") or {})):
            raise ValueError("a Choice request may not omit a mandatory candidate")
        if len({role for role, _value in request.fields}) != len(request.fields):
            return Judgement(unused("scope"), request.purpose, None)
        allowed, has_design, has_task = _scope_ok(request)
        if questions is None or not allowed:
            return Judgement(unused("scope"), request.purpose, None)
        if _missing_required(request):
            return Judgement(unused("insufficient-input"), request.purpose, None)
        settings = judgement_settings(config)
        if has_design and settings.get("send_design_text") is not True:
            return Judgement(unused("design-text-off"), request.purpose, None)
        if has_task and settings.get("send_task_text") is not True:
            return Judgement(unused("task-text-off"), request.purpose, None)
        if PURPOSES[request.purpose][2] and not (has_design or has_task):
            return Judgement(unused("insufficient-input"), request.purpose, None)
        resolved = _resolved(request, sources, view, task_view)
        if resolved is None:
            return Judgement(unused("scope"), request.purpose, None)
        request = resolved
        if settings.get("enabled") is not True:
            return Judgement(unused("judgement-off"), request.purpose, None)
        typesafe_settings = config.get("typesafe")
        model = str(typesafe_settings.get("model") or "") if isinstance(typesafe_settings, Mapping) else None
        if config.get("provider") == "typesafe" and not model:
            model = DEFAULT_MODEL
        state = {"purpose": request.purpose, "fields": {role: value.text for role, value in request.fields}}
        strings = _strings({"model": model, "state": state, "questions": questions})
        if any(len(question["instructions"]) > 500 or any(len(text) > 200 for text in _strings(question["criteria"]))
               for question in questions.values()):
            return Judgement(unused("too-large"), request.purpose, None)
        if len(questions) > settings["max_questions"] or sum(len(item) for item in strings) > settings["max_state_chars"]:
            return Judgement(unused("too-large"), request.purpose, None)
        if any(_flagged(item) for item in strings):
            return Judgement(unused("secret"), request.purpose, None)
        breaker = circuit.Breaker(state_dir, circuit.limits(config))
        provider, key, status = _gate(config, brief_disabled=brief_disabled, env=env, keychain=keychain,
                                      providers=providers)
        env = keychain = providers = None
        if provider is None:
            return Judgement(status, request.purpose, None)
        if not callable(getattr(provider, "judge", None)):
            return Judgement(unused("unsupported"), request.purpose, None)
        if getattr(provider, "name", None) == "typesafe" and not model:
            model = DEFAULT_MODEL
        identity = identity_of(request, policy=policy, provider=getattr(provider, "name", None), model=model,
                               settings=settings)
        from oah.rerank import judgement_cache

        moment = _utc_now()
        key_id = cache_key(identity)
        cached = judgement_cache.load(state_dir, key_id, identity, moment, float(settings["cache_hours"]))
        if cached is not None and not recheck(cached, sources=sources, view=view, policy=policy,
                                              provider=identity["provider"], model=model, config=config,
                                              task_view=task_view):
            try:
                answers = _valid_answers(cached["answers"], questions)
                outcome, choice, labels, level = _read(RUBRICS[request.rubric], answers, identity["thresholds"])
                return Judgement("used(cache)", request.purpose, identity, outcome, choice, labels, answers,
                                 identity["provider"], answered_model=cached["answered_model"],
                                 assessed_at=cached["assessed_at"], score_level=level)
            except (KeyError, ProviderError):
                pass
        if budget is not None and not budget.reserve():
            return Judgement(unused("budget"), request.purpose, identity)
        blocked = breaker.refusal() or breaker.reserve()
        if blocked is not None:
            return Judgement(unused(blocked), request.purpose, identity)
        _notice(state_dir, "public", notify)
        if has_design:
            _notice(state_dir, "design", notify)
        if has_task:
            _notice(state_dir, "task", notify)
        raw, status = _call(lambda: provider.judge(state, questions, key=key, config=config))
        if status == USED and isinstance(RUBRICS[request.rubric], OptionNoulRubric) and isinstance(raw, Mapping) \
                and set(raw) < set(questions):
            status = unused("error:dropped")  # the reply leaves a candidate out: no rank from a partial answer
        reply = None
        if status == USED:
            reply, status = _call(lambda: _valid_reply(raw, questions))
        raw = None
        breaker.record(circuit.outcome(status))
        if status != USED:
            return Judgement(status, request.purpose, identity, provider=identity["provider"])
        answers, answered_model = reply
        outcome, choice, labels, level = _read(RUBRICS[request.rubric], answers, identity["thresholds"])
        result = Judgement(USED, request.purpose, identity, outcome, choice, labels, answers, identity["provider"],
                           answered_model=answered_model, assessed_at=_utc_now().isoformat(), score_level=level)
        judgement_cache.save(state_dir, key_id, result.to_record(), _utc_now())
        return result
    finally:
        del provider, key


def _probability(value: object) -> float:
    if type(value) not in (int, float) or not 0.0 <= value <= 1.0:   # also refuses NaN and infinities
        raise ProviderError(BAD_RESPONSE)
    return float(value)


def _valid_answers(answers: object, questions: Mapping[str, Mapping]) -> dict[str, dict]:
    """Exactly one well-formed answer per asked question, whatever the provider; anything else is a bad response."""
    if not isinstance(answers, Mapping) or set(answers) != set(questions):
        raise ProviderError(BAD_RESPONSE)
    valid = {}
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, Mapping) or answer.get("type") != question["type"]:
            raise ProviderError(BAD_RESPONSE)
        if question["type"] == "noul":
            if set(answer) != {"type", "noul"}:
                raise ProviderError(BAD_RESPONSE)
            valid[name] = {"type": "noul", "noul": _probability(answer.get("noul"))}
            continue
        if question["type"] == "score":
            criteria = question["criteria"]
            keys = tuple(str(index) for index in range(len(criteria)))
            legend = {str(index): text for index, text in enumerate(criteria)}
            probabilities = answer.get("probabilities")
            score = answer.get("score")
            if set(answer) != {"type", "score", "confidence", "legend", "probabilities"} or \
                    answer.get("legend") != legend or not isinstance(probabilities, Mapping) or \
                    set(probabilities) != set(keys) or type(score) not in (int, float) or \
                    not math.isfinite(score) or not 0 <= score <= len(criteria) - 1:
                raise ProviderError(BAD_RESPONSE)
            scores = {key: _probability(probabilities[key]) for key in keys}
            if abs(math.fsum(scores.values()) - 1) > PROBABILITY_SUM_TOLERANCE or \
                    abs(math.fsum(int(key) * value for key, value in scores.items()) - score) > SCORE_TOLERANCE:
                raise ProviderError(BAD_RESPONSE)
            valid[name] = {"type": "score", "score": float(score), "confidence": _probability(answer.get("confidence")),
                           "legend": legend, "probabilities": scores}
            continue
        if question["type"] != "choice":
            raise ProviderError(BAD_RESPONSE)
        keys = tuple(question["criteria"])
        probabilities, choice = answer.get("probabilities"), answer.get("choice")
        if set(answer) != {"type", "choice", "confidence", "probabilities"} or \
                not isinstance(probabilities, Mapping) or set(probabilities) != set(keys) or choice not in keys:
            raise ProviderError(BAD_RESPONSE)
        scores = {key: _probability(probabilities[key]) for key in keys}
        if abs(math.fsum(scores.values()) - 1.0) > PROBABILITY_SUM_TOLERANCE or scores[choice] != max(scores.values()):
            raise ProviderError(BAD_RESPONSE)
        confidence = answer.get("confidence")
        valid[name] = {"type": "choice", "choice": choice, "probabilities": scores,
                       "confidence": _probability(confidence)}
    return valid


def _valid_reply(reply: object, questions: Mapping) -> tuple[dict, str | None]:
    model = reply.answered_model if isinstance(reply, JudgeReply) else None
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", model)):
        raise ProviderError(BAD_RESPONSE)
    return _valid_answers(reply, questions), model


def _reading(noul: float, thresholds: Mapping = THRESHOLDS) -> str:
    return "yes" if noul >= thresholds["yes"] else "no" if noul <= thresholds["no"] else "uncertain"


def _read(rubric, answers: Mapping[str, Mapping], thresholds: Mapping = THRESHOLDS) -> tuple:
    """(outcome, chosen option, label readings): only a decisive answer names an option."""
    if isinstance(rubric, (ImpactRubric, ScoreRubric)):
        labels = None if isinstance(rubric, ScoreRubric) else {
            label: _reading(answers[label]["noul"], thresholds) for label in rubric.labels.labels}
        answer = answers["score" if isinstance(rubric, ScoreRubric) else "urgency"]
        scores = answer["probabilities"]
        top = max(scores, key=scores.get)
        level = top if scores[top] >= thresholds["level_min"] and list(scores.values()).count(scores[top]) == 1 else None
        decisive = level is not None or (labels is not None and "yes" in labels.values())
        return ("matched" if decisive else "uncertain"), None, labels, level
    if isinstance(rubric, OptionNoulRubric):
        labels = {key: _reading(answer["noul"], thresholds) for key, answer in answers.items()}
        readings = set(labels.values())
        outcome = "matched" if "yes" in readings else "no-match" if readings == {"no"} else "uncertain"
        return outcome, None, labels, None
    if isinstance(rubric, LabelRubric):
        labels = {label: _reading(answers[label]["noul"], thresholds) for label in rubric.labels}
        readings = set(labels.values())
        return ("matched" if "yes" in readings else "no-match" if readings == {"no"} else "uncertain"), None, labels, None
    fits, choice = _reading(answers["fits"]["noul"], thresholds), answers["choice"]
    if fits == "no":
        return "no-match", None, None, None
    if fits == "uncertain" or choice["probabilities"][choice["choice"]] < thresholds["choice_min"]:
        return "uncertain", None, None, None
    return "chosen", choice["choice"], None, None


def recheck(judgement: Judgement | Mapping, *, sources, view=None, policy: str, provider: str | None,
            model: str | None, config: Mapping | None = None, task_view: TaskView | None = None) -> list[str]:
    """Why a result or cached record may not be applied now ([] when every identity still holds)."""
    from oah.design.sources.state import StateProblem

    record = judgement.to_record() if isinstance(judgement, Judgement) else dict(judgement)
    identity = record.get("identity")
    if record.get("status") not in (USED, "used(cache)") or not isinstance(identity, Mapping):
        return ["not-used"]
    if config is not None and not config.get("enabled", False):
        return ["disabled"]
    reasons = []
    if identity.get("constructor") != CONSTRUCTOR_VERSION:
        reasons.append("constructor-changed")
    if identity.get("policy") != policy:
        reasons.append("policy-changed")
    if identity.get("provider") != provider or identity.get("requested_model") != model:
        reasons.append("provider-changed")
    settings = judgement_settings({} if config is None else config)
    if identity.get("thresholds") != {key: settings[key] for key in THRESHOLDS}:
        reasons.append("thresholds-changed")
    compared = set(settings) if config is not None else set(settings) - {"enabled", "send_design_text",
                                                                          "send_task_text"}
    if any((identity.get("settings") or {}).get(key) != settings[key] for key in compared):
        reasons.append("settings-changed")
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
            elif field.get("tag") == "task-text":
                if not isinstance(task_view, TaskView) or dict(field.get("task_ref") or {}) != dict(task_view.ref):
                    reasons.append(f"task-changed:{field.get('role')}")
                    continue
                data = _task_bytes(task_view, field["source"])
                digest = _slice(data, field["offset"], field["length"], field["source"])[1]
            elif field.get("tag") == "public-label":
                if field.get("value") not in label_values(str(field.get("enum_id"))):
                    reasons.append(f"label-changed:{field.get('role')}")
                continue
            else:
                reasons.append(f"field-unknown:{field.get('role')}")  # an unknown tag is never skipped
                continue
        except (JudgeError, StateProblem, KeyError, TypeError, AttributeError):
            reasons.append(f"field-unavailable:{field.get('role')}")
            continue
        if digest != field.get("bytes_digest"):
            reasons.append(f"field-changed:{field.get('role')}")
    return reasons


@dataclasses.dataclass(frozen=True)
class Applied:
    applied: bool
    reason: str
    outcomes: Mapping
    policy_digest: str | None


def apply(judgement: Judgement | Mapping, *, current_input_ref: str, config: Mapping, sources=None,
          view=None, request: Request | None = None, policy: str | None = None,
          task_view: TaskView | None = None) -> Applied:
    record = judgement.to_record() if isinstance(judgement, Judgement) else dict(judgement)
    identity = record.get("identity") or {}
    if record.get("status") not in (USED, "used(cache)"):
        status = str(record.get("status") or "unused(not-used)")
        return Applied(False, status[7:-1] if status.startswith("unused(") else "not-used", {}, None)
    if not identity or identity.get("input_ref") != current_input_ref:
        return Applied(False, "stale-input", {}, None)
    effective_policy = policy if policy is not None else identity.get("policy")
    model = (config.get("typesafe") or {}).get("model")
    provider = config.get("provider")
    if provider == "typesafe" and not model:
        model = DEFAULT_MODEL
    reasons = recheck(record, sources=sources, view=view, policy=effective_policy, provider=provider,
                      model=model, config=config, task_view=task_view)
    if reasons:
        return Applied(False, reasons[0], {}, None)
    if request is not None:
        resolved = _resolved(request, sources, view, task_view)
        if resolved is None or identity_of(resolved, policy=effective_policy, provider=provider, model=model,
                                           settings=judgement_settings(config)) != identity:
            return Applied(False, "stale-input", {}, None)
    rubric = RUBRICS.get(identity.get("rubric"))
    if rubric is None:
        return Applied(False, "rubric-changed", {}, None)
    if request is None:
        request = Request(identity["purpose"], identity["rubric"], (), tuple(identity.get("options") or ()))
    questions = _questions(request)
    if questions is None:
        return Applied(False, "rubric-changed", {}, None)
    try:
        answers = _valid_answers(record.get("answers"), questions)
        outcome, choice, labels, level = _read(rubric, answers, identity["thresholds"])
    except (ProviderError, KeyError, TypeError):
        return Applied(False, "bad-response", {}, None)
    if outcome == "uncertain":
        return Applied(False, "uncertain", {}, None)
    outcomes = {"outcome": outcome, "choice": choice, "labels": labels, "urgency": level}
    return Applied(True, "current", outcomes, cache_key(identity))


def cache_key(identity: Mapping) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(dict(identity), sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False).encode("utf-8")).hexdigest()
