# 작업과 설계의 결속 — 패킷 Design 구획, 작업 신원·기준(basis)·증거 키, 작업 설계 결속 기록(V2)과 결정, 타입별 사건 재생, 설계 출처, 작업 설계 상태 판정과 게이트·리뷰 증거를 한곳에서 정한다.
"""How a task is bound to the product design it implements (lane B, C8 design).

Identity and basis
    `TaskIdentity` (repository common identity, task ID, opaque instance token) names one logical undertaking;
    `TaskBasis` adds the project uid and design ID, the full accepted intent binding, the acquired normalized scope
    and the declared gates, and its digest excludes lease ID, generation, TTL, note, agent and timestamps.
    `evidence_key(identity, basis, binding)` is the key gate and review evidence is stamped with. Execution
    authority (lease ID, generation, expiry, worktree) is provenance only and never evidence.

Record
    `TaskBinding` (version 2) holds the identity, the basis digest, the bound accepted `DesignBinding`, the explicit
    carry decisions that keep earlier evidence counting, and an optional draft acknowledgment for exactly one
    newer working revision. Decisions are cooperative and keyed by basis; a changed basis clears them.

Events
    Every design-carrying event has `design_schema=2` and a kind-specific payload. `task_view(state, task)` replays
    one task's recognized events (open lease first): the current or closing record, the closing identity, the
    origin evidence and a typed problem for malformed data of that task only. Unknown kinds never supply a binding.

Judgement
    `judge(task, root=...)` reads A's lock-free `Context` once and returns one of `STATES`. Work needs `WORK`;
    a read-only inspection also allows `needs-review` and never converges while it is unresolved.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import secrets
import unicodedata
from typing import Mapping, Sequence

from oah.design.model import (BINDING_INVALID_CODE, CONTEXT_STALE_CODE, CORRUPT_CODE, FRESHNESS_UNKNOWN_CODE,
                              NOT_FOUND_CODE, PATH_INVALID_CODE, RECOVERY_PENDING_CODE, UNREGISTERED_EDIT_CODE,
                              DesignBinding, DesignError, canonical, check_digest, check_id, check_line,
                              check_revision)
from oah.wiki.base import EXIT_ENVIRONMENT, EXIT_REFUSED

SECTION_HEADING = "## Design"
SECTION_KEYS = ("design", "project uid", "revision", "digest")
DESIGN_SCHEMA = 2
MAX_DECISIONS = 64
STATES = ("unbound", "required", "current", "needs-review", "stale", "invalid", "edited", "unknown", "missing")
WORK = frozenset({"unbound", "current"})
INSPECT = frozenset({"unbound", "current", "needs-review"})
OPERATIONS = {"start": WORK, "dispatch": WORK, "fix": WORK, "finish": WORK, "land": WORK, "carry": WORK,
              "inspect": INSPECT}
ORIGIN_STATUSES = ("pending", "linked", "error")

SECTION_INVALID_CODE = "OAH_DESIGN_SECTION_INVALID"
BINDING_REQUIRED_CODE = "OAH_DESIGN_BINDING_REQUIRED"
NEEDS_REVIEW_CODE = "OAH_DESIGN_NEEDS_REVIEW"
PACKET_STALE_CODE = "OAH_DESIGN_PACKET_STALE"
NOT_ACCEPTED_CODE = "OAH_DESIGN_NOT_ACCEPTED"
SOURCE_MISMATCH_CODE = "OAH_DESIGN_SOURCE_MISMATCH"
CHANGED_CODE = "OAH_DESIGN_CHANGED"
ALREADY_CURRENT_CODE = "OAH_DESIGN_ALREADY_CURRENT"
ORIGIN_INVALID_CODE = "OAH_DESIGN_ORIGIN_INVALID"
ORIGIN_UNKNOWN_CODE = "OAH_DESIGN_ORIGIN_UNKNOWN"
WORKER_REFUSED_CODE = "OAH_DESIGN_WORKER_REFUSED"
RECORD_INVALID_CODE = "OAH_DESIGN_RECORD_INVALID"
RECORD_FULL_CODE = "OAH_DESIGN_RECORD_FULL"
INPUTS_CHANGED_CODE = "OAH_DESIGN_TASK_INPUTS_CHANGED"
STATE_CODES = {"required": BINDING_REQUIRED_CODE, "needs-review": NEEDS_REVIEW_CODE, "stale": CONTEXT_STALE_CODE,
               "invalid": BINDING_INVALID_CODE, "edited": UNREGISTERED_EDIT_CODE, "missing": NOT_FOUND_CODE,
               "unknown": FRESHNESS_UNKNOWN_CODE}
RECORD_LIMIT = 64 * 1024  # the lease reader's per-task design value limit

_AGENT = re.compile(r"[A-Za-z0-9._-]{1,32}")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?")
_UID = re.compile(r"[0-9a-f]{32}")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMENT = re.compile(r"<!--.*-->")
_BULLET = re.compile(r"- ([a-z][a-z ]*[a-z]):(.*)")
_REVISION = re.compile(r"[1-9][0-9]{0,8}")


def _digest(data: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(data)).hexdigest()


class SectionInvalid(DesignError):
    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(SECTION_INVALID_CODE, "the packet's ## Design section is invalid: " + "; ".join(problems) +
                         ". Rewrite it with oah packet design <task>, or remove it to keep the task without a design")


class RecordInvalid(Exception):
    """A lease's design value is task-local invalid evidence: never None, never legacy, never printed raw."""

    code = RECORD_INVALID_CODE

    def __init__(self, task: str, identity: str) -> None:
        self.task, self.identity = task, identity
        super().__init__(f"{RECORD_INVALID_CODE}: the design record of task {task} is invalid ({identity})")


# The packet section -----------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PacketDesign:
    """The design a packet's requirements were written from."""

    project: str
    uid: str
    design: str
    revision: int
    digest: str

    def binding(self) -> DesignBinding:
        return DesignBinding(self.project, self.uid, self.design, self.revision, self.digest)

    @classmethod
    def of(cls, binding: DesignBinding) -> "PacketDesign":
        return cls(binding.project, binding.uid, binding.design, binding.revision, binding.digest)

    def to_json(self) -> dict:
        return self.binding().to_json()

    @classmethod
    def from_json(cls, data: object) -> "PacketDesign":
        return cls.of(DesignBinding.from_json(data))  # ValueError on anything else

    def line(self) -> str:
        return self.binding().line()

    def lines(self) -> list[str]:
        return [SECTION_HEADING,
                "<!-- The product design this task implements; oah packet design <task> rewrites this section. -->",
                f"- design: {self.project}/{self.design}", f"- project uid: {self.uid}", f"- revision: {self.revision}",
                f"- digest: {self.digest}"]


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _section_span(lines: Sequence[str]) -> tuple[list[int], int | None]:
    """Heading rows of `## Design` outside the intent markers, and where the first section's body ends."""
    from oah.intent import BEGIN, END

    inside = False
    heads: list[int] = []
    for number, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if line == BEGIN:
            inside = True
        elif line == END:
            inside = False
        elif not inside and line.rstrip() == SECTION_HEADING:
            heads.append(number)
    if not heads:
        return heads, None
    end = len(lines)
    for number in range(heads[0] + 1, len(lines)):
        line = lines[number].rstrip("\r\n")
        if line.startswith(("## ", "# ")) or line in (BEGIN, END):
            end = number
            break
    return heads, end


def has_section(text: str) -> bool:
    """True when `text` has a `## Design` heading outside the intent markers (valid or not)."""
    return bool(_section_span(_lines(text))[0])


def parse_section(text: str) -> PacketDesign | None:
    """The packet's Design section, None when there is none; raises SectionInvalid listing every problem."""
    lines = _lines(text)
    heads, end = _section_span(lines)
    if not heads:
        return None
    problems: list[str] = []
    if len(heads) > 1:
        problems.append(f"{SECTION_HEADING} appears {len(heads)} times; keep one")
    found: dict[str, str] = {}
    for number in range(heads[0] + 1, end):
        line = lines[number].strip()
        if not line or _COMMENT.fullmatch(line):
            continue
        match = _BULLET.fullmatch(line)
        if match is None or match.group(1) not in SECTION_KEYS:
            problems.append(f"line {number + 1} is not one of the bullets {', '.join(k + ':' for k in SECTION_KEYS)}")
            continue
        key = match.group(1)
        if key in found:
            problems.append(f"{key} appears more than once")
            continue
        found[key] = match.group(2).strip()
    for key in SECTION_KEYS:
        if key not in found:
            problems.append(f"the {key} bullet is missing")
    project = design = uid = digest = None
    revision = None
    if "design" in found:
        project, _, design = found["design"].partition("/")
        try:
            check_id(project)
            check_id(design, "design")
        except DesignError:
            problems.append(f"design {found['design']!r} is not <project>/<design> with valid IDs")
    if "project uid" in found:
        uid = found["project uid"]
        if not _UID.fullmatch(uid):
            problems.append("project uid is not 32 hex characters")
    if "revision" in found:
        if _REVISION.fullmatch(found["revision"]):
            revision = int(found["revision"])
        else:
            problems.append(f"revision {found['revision']!r} is not a whole number from 1")
    if "digest" in found:
        digest = found["digest"]
        try:
            check_digest(digest)
        except DesignError:
            problems.append("digest is not sha256:<64 hex>")
    if problems:
        raise SectionInvalid(problems)
    return PacketDesign(project, uid, design, revision, digest)


def render_section(design: PacketDesign, eol: str = "\n") -> str:
    return eol.join(design.lines()) + eol


def replace_section(text: str, design: PacketDesign) -> str:
    """`text` with its Design section replaced (or appended), every other byte and the line endings kept."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    plain = [line.rstrip("\r\n") for line in lines]
    heads, end = _section_span(plain)
    if not heads:
        out = text
        if out and not out.endswith(("\n", "\r")):
            out += eol
        if out.strip() and not out.endswith(eol + eol):
            out += eol
        return out + render_section(design, eol)
    last = end
    while last > heads[0] + 1 and not plain[last - 1].strip():
        last -= 1
    ending = lines[last - 1][len(plain[last - 1]):] or eol
    rendered = eol.join(design.lines()) + ending
    return "".join(lines[:heads[0]]) + rendered + "".join(lines[last:])


_MISSING = object()


# Identity, basis and evidence keys ---------------------------------------------------------------------------


def repo_identity(root: pathlib.Path) -> str:
    """The repository's common-directory identity (every worktree shares it); moving the repository changes it."""
    from oah.paths import GitMissing, run_git

    try:
        found = run_git(pathlib.Path(root), "rev-parse", "--path-format=absolute", "--git-common-dir")
    except GitMissing:
        found = None
    if found is None or found.returncode != 0 or not found.stdout.strip():
        raise DesignError(FRESHNESS_UNKNOWN_CODE, f"the repository of {root} cannot be identified", EXIT_ENVIRONMENT)
    common = pathlib.Path(found.stdout.strip()).resolve()
    return "sha256:" + hashlib.sha256(unicodedata.normalize("NFC", common.as_posix()).encode("utf-8")).hexdigest()


def mint_instance() -> str:
    return secrets.token_hex(16)


@dataclasses.dataclass(frozen=True)
class TaskIdentity:
    repo: str
    task: str
    instance: str

    def to_json(self) -> dict:
        return {"repo_common_identity": self.repo, "task_id": self.task, "task_instance_id": self.instance}

    @classmethod
    def from_json(cls, data: object) -> "TaskIdentity":
        from oah.lease import TASK_RE

        if not isinstance(data, Mapping) or set(data) != {"repo_common_identity", "task_id", "task_instance_id"}:
            raise ValueError("a task identity holds exactly repo_common_identity, task_id and task_instance_id")
        repo, task, instance = data["repo_common_identity"], data["task_id"], data["task_instance_id"]
        if not (isinstance(repo, str) and _DIGEST.fullmatch(repo) and isinstance(task, str) and TASK_RE.fullmatch(task)
                and isinstance(instance, str) and _HEX32.fullmatch(instance)):
            raise ValueError("malformed task identity")
        return cls(repo, task, instance)


def normalize_paths(entries: Sequence[str]) -> list[str]:
    """NFC, casefolded, sorted and deduplicated, for identity only (the lease keeps its own spelling)."""
    return sorted({unicodedata.normalize("NFC", str(entry)).casefold() for entry in entries})


@dataclasses.dataclass(frozen=True)
class TaskBasis:
    """The exact task inputs evidence is valid for; lease ID, generation, TTL, note, agent and times are not in it."""

    identity: TaskIdentity
    project_uid: str
    design: str
    intent: dict  # oah.intent.Binding.to_json(), including source and AC IDs
    scope: tuple[str, ...]
    gates: tuple[str, ...]

    def to_json(self) -> dict:
        return {"version": 2, "task_identity": self.identity.to_json(), "project_uid": self.project_uid,
                "design_id": self.design, "intent": self.intent, "scope": list(self.scope), "gates": list(self.gates)}

    @property
    def digest(self) -> str:
        return _digest(self.to_json())

    @classmethod
    def of(cls, identity: TaskIdentity, *, project_uid: str, design: str, intent: Mapping | None,
           scope: Sequence[str], gates: Sequence[str]) -> "TaskBasis":
        return cls(identity, project_uid, design, dict(intent or {}), tuple(normalize_paths(scope)),
                   tuple(normalize_paths(gates)))


def evidence_key(identity: TaskIdentity, basis_digest: str, binding: DesignBinding) -> str:
    """TaskEvidenceKeyV2: what a gate run or review receipt of this task, inputs and design certifies."""
    return _digest({"version": 2, "task_identity": identity.to_json(), "task_basis_digest": basis_digest,
                    "design_binding": binding.to_json()})


def authority_of(lease) -> dict:
    """ExecutionAuthorityV1 of a lease: provenance for decisions, never evidence."""
    record = getattr(lease, "design", None)
    instance = record.get("task_identity", {}).get("task_instance_id") if isinstance(record, dict) else None
    return {"lease_id": lease.id, "generation": lease.generation, "expires_at": lease.expires_at,
            "worktree": lease.worktree, "task_instance_id": instance}


def _ref(snapshot) -> dict | None:
    return None if snapshot is None else {"revision": snapshot.revision, "digest": snapshot.digest}


def _check_ref(value: object, what: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"revision", "digest"}:
        raise ValueError(f"{what} must be {{revision, digest}}")
    try:
        return {"revision": check_revision(value["revision"]), "digest": check_digest(value["digest"])}
    except DesignError as exc:
        raise ValueError(f"malformed {what}: {exc}") from None


_AUTHORITY_KEYS = {"lease_id", "generation", "expires_at", "worktree", "task_instance_id"}


@dataclasses.dataclass(frozen=True)
class Decision:
    """One explicit cooperative decision: a carry edge (`from` -> `to`) or a draft acknowledgment of one working
    revision. Its meaning excludes the timestamp and lease incarnation; `authority` is provenance only."""

    kind: str  # carry | draft
    to: dict  # {revision, digest} of the binding the decision applies to
    source: dict | None  # carry: {revision, digest} it moves from; draft: None
    basis: str
    viewed_accepted: dict | None
    viewed_working: dict
    reason: str
    actor: str
    at: str
    decision_id: str
    authority: dict | None

    def to_json(self) -> dict:
        return {"kind": self.kind, "to": self.to, "from": self.source, "basis": self.basis,
                "viewed": {"accepted": self.viewed_accepted, "working": self.viewed_working}, "reason": self.reason,
                "actor": self.actor, "at": self.at, "decision_id": self.decision_id, "authority": self.authority}

    def semantic(self) -> dict:
        data = self.to_json()
        for name in ("at", "authority", "decision_id"):
            data.pop(name)
        return data

    @classmethod
    def from_json(cls, data: object) -> "Decision":
        keys = {"kind", "to", "from", "basis", "viewed", "reason", "actor", "at", "decision_id", "authority"}
        if not isinstance(data, Mapping) or set(data) != keys:
            raise ValueError("a design decision holds exactly " + ", ".join(sorted(keys)))
        kind = data["kind"]
        if kind not in ("carry", "draft"):
            raise ValueError("a design decision is a carry or a draft acknowledgment")
        viewed = data["viewed"]
        if not isinstance(viewed, Mapping) or set(viewed) != {"accepted", "working"}:
            raise ValueError("viewed holds exactly accepted and working")
        try:
            reason = check_line(data["reason"], "reason")
        except DesignError as exc:
            raise ValueError(str(exc)) from None
        actor, at, decision_id, basis = data["actor"], data["at"], data["decision_id"], data["basis"]
        if not (isinstance(actor, str) and _AGENT.fullmatch(actor) and isinstance(at, str) and _ISO.fullmatch(at)
                and isinstance(decision_id, str) and _HEX16.fullmatch(decision_id)
                and isinstance(basis, str) and _DIGEST.fullmatch(basis)):
            raise ValueError("malformed actor, time, decision ID or basis")
        authority = data["authority"]
        if authority is not None and (not isinstance(authority, Mapping) or set(authority) != _AUTHORITY_KEYS):
            raise ValueError("malformed authority provenance")
        source = _check_ref(data["from"], "from")
        if (kind == "carry") != (source is not None):
            raise ValueError("a carry names where it moves from; a draft acknowledgment does not")
        working = _check_ref(viewed["working"], "viewed working")
        if working is None:
            raise ValueError("a decision names the working revision that was read")
        return cls(kind, _check_ref(data["to"], "to"), source, basis, _check_ref(viewed["accepted"], "viewed accepted"),
                   working, reason, actor, at, decision_id, dict(authority) if authority is not None else None)


@dataclasses.dataclass(frozen=True)
class TaskBinding:
    """TaskBindingV2: what a task follows, for exact task inputs."""

    identity: TaskIdentity
    basis: str  # TaskBasis digest the record was made for
    binding: DesignBinding
    carried: tuple[Decision, ...] = ()
    draft_ack: Decision | None = None

    def to_json(self) -> dict:
        return {"version": 2, "task_identity": self.identity.to_json(), "basis": self.basis,
                "binding": self.binding.to_json(), "carried": [item.to_json() for item in self.carried],
                "draft_ack": self.draft_ack.to_json() if self.draft_ack is not None else None}

    @property
    def digest(self) -> str:
        return _digest(self.to_json())

    @classmethod
    def from_json(cls, data: object) -> "TaskBinding":
        """Rebuild a version-2 record; earlier or unknown shapes raise ValueError (they are not migrated)."""
        keys = {"version", "task_identity", "basis", "binding", "carried", "draft_ack"}
        if not isinstance(data, Mapping) or set(data) != keys or data.get("version") != 2:
            raise ValueError("a task design record is a version-2 object with exactly " + ", ".join(sorted(keys)))
        identity = TaskIdentity.from_json(data["task_identity"])
        basis = data["basis"]
        if not (isinstance(basis, str) and _DIGEST.fullmatch(basis)):
            raise ValueError("basis must be a sha256 digest")
        binding = DesignBinding.from_json(data["binding"])
        carried = data["carried"]
        if not isinstance(carried, list) or len(carried) > MAX_DECISIONS:
            raise ValueError(f"carried must be a list of at most {MAX_DECISIONS} decisions")
        items = tuple(Decision.from_json(item) for item in carried)
        if any(item.kind != "carry" for item in items):
            raise ValueError("carried holds carry decisions only")
        draft = None if data["draft_ack"] is None else Decision.from_json(data["draft_ack"])
        if draft is not None and draft.kind != "draft":
            raise ValueError("draft_ack holds a draft acknowledgment")
        return cls(identity, basis, binding, items, draft)

    def bindings(self) -> tuple[DesignBinding, ...]:
        """The bound binding and every binding a carry of the SAME basis moved from: the evidence set."""
        base = self.binding
        moved = [dataclasses.replace(base, revision=item.source["revision"], digest=item.source["digest"])
                 for item in self.carried if item.basis == self.basis]
        return (base, *moved)

    def keys(self) -> frozenset[str]:
        return frozenset(evidence_key(self.identity, self.basis, item) for item in self.bindings())

    @property
    def key(self) -> str:
        return evidence_key(self.identity, self.basis, self.binding)

    def holds(self, other: DesignBinding | None) -> bool:
        return other is not None and other in self.bindings()

    def same_design(self, other) -> bool:
        return (self.binding.project, self.binding.uid, self.binding.design) == (other.project, other.uid,
                                                                                  other.design)

    def fits(self) -> bool:
        return len(json.dumps(self.to_json(), sort_keys=True).encode("utf-8")) <= RECORD_LIMIT


def require_fits(record: TaskBinding, task: str) -> TaskBinding:
    if not record.fits() or len(record.carried) > MAX_DECISIONS:
        raise DesignError(RECORD_FULL_CODE, f"the design record of task {task} would exceed its {RECORD_LIMIT}-byte or "
                          f"{MAX_DECISIONS}-decision bound; nothing was changed. Finish the task, or rebind it to the "
                          "accepted revision with oah start --packet (a rebind starts a new evidence set)")
    return record


# Events: one task's typed view --------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TaskView:
    """One task as its events and open lease record it (B3)."""

    lease: object | None  # the open lease (active or stale), or None
    record: TaskBinding | None  # the open lease's record, else the last epoch's record
    problem: str | None  # malformed recognized design data of this task (never hidden by an earlier event)
    origin: tuple[str, ...]  # which events make the task design-origin
    unreadable: str | None  # the event log itself cannot be read
    identity: TaskIdentity | None  # the newest task identity seen
    closed: str | None  # finished | released | expired, when no lease is open
    closing: dict | None  # the finished epoch: lease_id, transaction_id, generation, scope, gates, worktree, intent
    epoch: dict | None  # the last epoch: lease_id, generation, scope, gates, worktree, intent, ended
    origin_packets: tuple[dict, ...] = ()  # PacketDesign JSON of design_origin marks, oldest first


def _event_record(event: Mapping, field: str = "design") -> TaskBinding | None:
    value = event.get(field)
    return None if value is None else TaskBinding.from_json(value)


def task_view(state: pathlib.Path, task: str, *, lease=_MISSING) -> TaskView:
    """Replay this task's recognized events in journal order; the open lease wins for the current record."""
    from oah.atomic import StateCorrupt, StorageError, read_jsonl

    if lease is _MISSING:
        from oah.lease import Store

        lease = next((item for item, _stale in Store(state).snapshot() if item.task == task), None)
    try:
        events = read_jsonl(pathlib.Path(state) / "events.jsonl")
    except (StateCorrupt, StorageError, RecursionError, ValueError, MemoryError) as exc:
        return TaskView(lease, None, None, (), f"the event log cannot be read ({type(exc).__name__})", None, None,
                        None, None)
    record: TaskBinding | None = None
    identity: TaskIdentity | None = None
    problem = None
    origin: list[str] = []
    packets: list[dict] = []
    epoch: dict | None = None
    closing: dict | None = None
    closed = None
    for position, event in enumerate(events, 1):
        if event.get("task") != task:
            continue
        kind = event.get("event")
        try:
            if kind == "design_origin":
                found = event.get("origin")
                if event.get("design_schema") != DESIGN_SCHEMA or not isinstance(found, Mapping):
                    raise ValueError("not a version-2 design_origin")
                if found.get("status") not in ORIGIN_STATUSES:
                    raise ValueError(f"status {found.get('status')!r}")
                PacketDesign.from_json(found.get("packet_design"))
                identity = TaskIdentity.from_json(event.get("task_identity"))
                origin.append(f"design_origin({found['status']})")
                packets.append(dict(found["packet_design"]))
                continue
            if kind == "lease_started":
                epoch = {"lease_id": event.get("lease_id"), "generation": event.get("generation"),
                         "scope": event.get("scope"), "gates": event.get("gates"), "worktree": event.get("worktree"),
                         "intent": event.get("intent"), "ended": None, "transaction_id": event.get("transaction_id"),
                         "output_baseline": event.get("design_output_baseline")}
                closing, closed = None, None
                record = _event_record(event) if event.get("design_schema") == DESIGN_SCHEMA else None
                if "design" in event and event.get("design_schema") != DESIGN_SCHEMA:
                    raise ValueError("a design payload without design_schema 2")
            elif kind in ("intent_bound", "intent_rebound", "lease_renewed"):
                if epoch is not None and event.get("lease_id") == epoch["lease_id"]:
                    for name in ("scope", "gates", "intent", "generation"):
                        if name in event:
                            epoch[name] = event[name]
                    if epoch.get("output_baseline") is None and event.get("design_output_baseline") is not None:
                        epoch["output_baseline"] = event["design_output_baseline"]
                if event.get("design_schema") == DESIGN_SCHEMA:
                    record = _event_record(event)
                elif record is not None and epoch is not None and event.get("lease_id") == epoch["lease_id"]:
                    raise ValueError(f"{kind} of a design-bound epoch carries no design record")
            elif kind == "lease_finished":
                if epoch is not None and event.get("lease_id") == epoch["lease_id"]:
                    epoch["ended"] = "finished"
                if event.get("design_schema") == DESIGN_SCHEMA:
                    record = _event_record(event)
                    closing = {"lease_id": event.get("lease_id"), "transaction_id": event.get("transaction_id"),
                               "generation": event.get("generation"), "scope": event.get("scope"),
                               "gates": event.get("declared_gates"), "worktree": event.get("worktree"),
                               "intent": event.get("bound_intent")}
                closed = "finished"
            elif kind in ("lease_released", "lease_expired"):
                if epoch is not None and event.get("lease_id") == epoch["lease_id"]:
                    epoch["ended"] = "released" if kind == "lease_released" else "expired"
                    epoch["end_transaction"] = event.get("transaction_id")
                closed = "released" if kind == "lease_released" else "expired"
                closing = None
            elif kind == "design_carried":
                if event.get("design_schema") != DESIGN_SCHEMA:
                    raise ValueError("not a version-2 design_carried")
                target = event.get("target")
                epoch_id = (closing or epoch or {}).get("lease_id")
                if (not isinstance(target, Mapping) or record is None or target.get("lease_id") != epoch_id
                        or target.get("expected_design_record_digest") != record.digest
                        or (target.get("closing_transaction_id") is not None
                            and (closing is None or target["closing_transaction_id"] != closing["transaction_id"]))):
                    raise ValueError("the carry does not target this task's current epoch and record")
                record = _event_record(event)
            else:
                continue  # unknown kinds and transport history never supply a binding
            if record is not None:
                identity = record.identity
                origin.append(str(kind))
        except (ValueError, RecursionError) as exc:
            problem = f"event {position} ({kind}) holds malformed design data: {str(exc)[:200]}"
    if lease is not None:
        try:
            current = lease.design_record()
        except RecordInvalid as exc:
            return TaskView(lease, None, f"{RECORD_INVALID_CODE}: {exc.identity}", tuple(dict.fromkeys(origin)) or
                            ("lease",), None, identity, None, None, epoch, tuple(packets))
        if current is not None:
            identity = current.identity
            origin.append("lease")
        return TaskView(lease, current, problem, tuple(dict.fromkeys(origin)), None, identity, None, None, epoch,
                        tuple(packets))
    return TaskView(None, record, problem, tuple(dict.fromkeys(origin)), None, identity, closed, closing, epoch,
                    tuple(packets))



@dataclasses.dataclass(frozen=True)
class Origin:
    kind: str  # packet | head | events | lease | invalid | unknown
    detail: str


def committed_text(root: pathlib.Path, source: str, rev: str = "HEAD") -> str | None:
    """The packet as committed at `rev`, or None when it is not committed there."""
    from oah.paths import GitMissing, run_git

    try:
        shown = run_git(pathlib.Path(root), "cat-file", "blob", f"{rev}:{source}")
    except GitMissing:
        return None
    return shown.stdout if shown.returncode == 0 else None


def origin(state: pathlib.Path, task: str, *, packet_text: str | None = None, root: pathlib.Path | None = None,
           source: str | None = None, view: TaskView | None = None) -> Origin | None:
    """Why the task must carry a design binding, or None for an origin-free (legacy) task."""
    if packet_text is not None and has_section(packet_text):
        return Origin("packet", "its packet has a ## Design section")
    if root is not None and source:
        committed = committed_text(root, source)
        if committed is not None and has_section(committed):
            return Origin("head", f"the packet committed at HEAD ({source}) has a ## Design section")
    view = view if view is not None else task_view(state, task)
    if view.unreadable is not None:
        return Origin("unknown", view.unreadable)
    if view.problem is not None:
        return Origin("invalid", view.problem)
    if view.origin:
        return Origin("events", "its events record a design (" + ", ".join(view.origin[:3]) + ")")
    return None


# Judgement ----------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TaskDesign:
    state: str
    code: str | None
    reason: str
    task: TaskBinding | None
    context: object | None = None  # oah.design.store.Context
    origin: Origin | None = None
    exit_code: int = EXIT_REFUSED

    @property
    def bound(self) -> bool:
        return self.task is not None

    def _revisions(self) -> tuple[dict | None, dict | None]:
        context = self.context
        if context is None:
            return None, None
        return _ref(context.accepted), _ref(context.current)

    def record(self) -> dict:
        accepted, working = self._revisions()
        context = self.context
        return {"version": 2, "state": self.state, "code": self.code,
                "task": self.task.to_json() if self.task is not None else None,
                "evidence_key": self.task.key if self.task is not None else None,
                "accepted": accepted, "working": working,
                "page_state": getattr(context, "page_state", None), "source": getattr(context, "source", None),
                "freshness": getattr(context, "freshness", None)}

    def fingerprint(self) -> str:
        accepted, working = self._revisions()
        return _digest({"state": self.state, "task": self.task.to_json() if self.task is not None else None,
                        "accepted": accepted, "working": working})

    def line(self) -> str:
        if self.task is None:
            return f"design={self.state}"
        return f"{self.task.binding.line()} state={self.state}"

    def next_step(self, task: str, agent: str = "<you>") -> str:
        project, design = ((self.task.binding.project, self.task.binding.design) if self.task is not None
                           else ("<project>", "<design>"))
        context = self.context
        working = (f"--revision {context.current.revision} --digest {context.current.digest}"
                   if context is not None else "--revision <N> --digest <digest>")
        carry = f'oah design carry {task} --agent {agent} {working} --reason "<why it does not change {task}>"'
        rebind = (f"accept the revision, rewrite the packet's Design section with oah packet design {task}, raise "
                  f"the intent Revision if the requirements changed, commit, then oah start --task {task} --agent "
                  f"{agent} --packet <packet>")
        if self.code == INPUTS_CHANGED_CODE:
            return (f"the task's intent, scope or gates changed since its design was bound; bind them together again "
                    f"with oah start --task {task} --agent {agent} --packet <packet> (earlier carries no longer apply)")
        if self.code == RECORD_INVALID_CODE:
            return ("the stored design record is invalid evidence (its original bytes are kept); release the lease and "
                    f"start the task again with oah start --task {task} --agent {agent} --packet <packet>")
        steps = {
            "required": f"restore the packet's ## Design section (oah packet design {task}), commit it and bind it "
                        f"with oah start --task {task} --agent {agent} --packet <packet>",
            "needs-review": f"read the newer draft with oah design task {task}; if it does not change this task, "
                            f"record that with {carry}; otherwise {rebind}",
            "stale": f"read the change with oah design task {task}; if it does not change this task, record that "
                     f"with {carry}; otherwise {rebind}",
            "invalid": f"restore the design records from git, or bind the right design with oah packet design {task} "
                       f"and oah start --packet",
            "edited": f"register the page edit with oah design save {project} {design} --from-wiki --expect-revision "
                      "<N> --expect-digest <digest> --reason <why> on the authority branch (then accept and carry or "
                      "rebind), or delete the page and run oah design reconcile to discard it; in another worktree, "
                      "restore the page with git checkout -- <page>",
            "missing": f"restore the design, or bind an existing one with oah packet design {task}",
            "unknown": "make the authority branch and the state folder readable (or run oah design recover when a "
                       "write is pending), then retry",
        }
        return steps.get(self.state, "nothing to do")

    def error(self, operation: str, task: str, agent: str = "<you>") -> DesignError:
        code = self.code or STATE_CODES.get(self.state, BINDING_INVALID_CODE)
        what = {"start": "this lease cannot bind", "dispatch": "nothing was launched", "fix": "nothing was launched",
                "finish": "the lease stays open", "land": "nothing lands", "inspect": "nothing was launched",
                "carry": "nothing was recorded"}.get(operation, "nothing was changed")
        named = self.task.binding.line() if self.task is not None else f"task {task}"
        step = self.next_step(task, agent)
        if operation == "start" and self.state == "needs-review" and self.context is not None:
            working = self.context.current
            step = (f"read the newer draft with oah design show {self.task.binding.project} {self.task.binding.design} "
                    f"--revision {working.revision}; if it does not change this task, start again with "
                    f"--draft-revision {working.revision} --draft-digest {working.digest} --draft-reason \"<why>\"; "
                    "otherwise accept it, rewrite the Design section with oah packet design <task>, commit and start "
                    "again")
        return DesignError(code, f"{named} is {self.state}: {self.reason}; {what}. Next: {step}", self.exit_code)

    def require(self, operation: str, *, task: str, agent: str = "<you>") -> None:
        if self.state not in OPERATIONS[operation]:
            raise self.error(operation, task, agent)


class _OneContext:
    """Feeds one already read Context to A's explain_binding, so a judgement reads the records once."""

    def __init__(self, context) -> None:
        self._context = context

    def context(self, project: str, design: str):
        return self._context


def _unknown(task: TaskBinding | None, reason: str, code: str = FRESHNESS_UNKNOWN_CODE, *, context=None,
             found: Origin | None = None) -> TaskDesign:
    return TaskDesign("unknown", code, reason, task, context, found, EXIT_ENVIRONMENT)


def read_context(binding: DesignBinding, *, root: pathlib.Path, store=None, env: Mapping[str, str] | None = None,
                 task: TaskBinding | None = None):
    """A's context of the design, or a TaskDesign saying why it could not be read (never current)."""
    try:
        if store is None:
            from oah.design.store import open_store

            store = open_store(root, env=env)
        return store.context(binding.project, binding.design)
    except DesignError as exc:
        if exc.code == NOT_FOUND_CODE:
            return TaskDesign("missing", NOT_FOUND_CODE, str(exc), task)
        if exc.code in (CORRUPT_CODE, PATH_INVALID_CODE):
            return TaskDesign("invalid", BINDING_INVALID_CODE, str(exc), task)
        return _unknown(task, str(exc), exc.code if exc.code == RECOVERY_PENDING_CODE else FRESHNESS_UNKNOWN_CODE)
    except Exception as exc:  # A limitation: a missing Git object may escape as another exception
        return _unknown(task, f"the design records could not be read ({type(exc).__name__})")


def basis_of(task: TaskBinding, *, intent: Mapping | None, scope: Sequence[str], gates: Sequence[str]) -> TaskBasis:
    return TaskBasis.of(task.identity, project_uid=task.binding.uid, design=task.binding.design, intent=intent,
                        scope=scope, gates=gates)


def judge(task: TaskBinding | None, *, root: pathlib.Path, found: Origin | None = None, store=None,
          env: Mapping[str, str] | None = None, basis: TaskBasis | None = None,
          invalid: str | None = None) -> TaskDesign:
    """The task's design state from the lock-free authority copy `root`'s repository reads.

    `basis` is the basis rederived from the actual lease/closing intent, scope and gates; a record made for other
    inputs is stale (OAH_DESIGN_TASK_INPUTS_CHANGED). `invalid` names a task-local invalid record."""
    if invalid is not None:
        return TaskDesign("invalid", RECORD_INVALID_CODE, invalid, None, None, found or Origin("lease", invalid))
    if task is None:
        if found is None:
            return TaskDesign("unbound", None, "no design is bound to this task", None)
        if found.kind == "unknown":
            return _unknown(None, f"whether this task follows a design is unknown: {found.detail}",
                            ORIGIN_UNKNOWN_CODE, found=found)
        code = ORIGIN_INVALID_CODE if found.kind == "invalid" else BINDING_REQUIRED_CODE
        return TaskDesign("required", code, f"the task follows a design ({found.detail}) but has no design binding",
                          None, None, found)
    context = read_context(task.binding, root=root, store=store, env=env, task=task)
    if isinstance(context, TaskDesign):
        return dataclasses.replace(context, origin=found)
    judged = judge_with_context(task, context)
    if basis is not None and basis.digest != task.basis and judged.state in ("current", "needs-review"):
        judged = TaskDesign("stale", INPUTS_CHANGED_CODE, "the task's intent, scope or gates are not the ones its "
                            "design binding was made for", task, context)
    return dataclasses.replace(judged, origin=found)


def judge_with_context(task: TaskBinding, context) -> TaskDesign:
    """The pure core: A decides revision, digest and page; B adds recovery, the newer draft and its acknowledgment."""
    from oah.design.store import explain_binding

    recovery = context.recovery
    if recovery.pending and (recovery.this_design or recovery.kind == "unreadable"):
        return _unknown(task, "an interrupted design write is pending; run oah design recover", RECOVERY_PENDING_CODE,
                        context=context)
    try:
        state, reason = explain_binding(task.binding, _OneContext(context), against="accepted")
    except DesignError as exc:  # A limitation: explain_binding may raise FRESHNESS_UNKNOWN
        return _unknown(task, str(exc), context=context)
    except Exception as exc:
        return _unknown(task, f"the binding could not be judged ({type(exc).__name__})", context=context)
    if state != "current":
        exit_code = EXIT_ENVIRONMENT if state == "unknown" else EXIT_REFUSED
        return TaskDesign(state, STATE_CODES.get(state), reason, task, context, None, exit_code)
    working = context.current
    if context.accepted is not None and working.revision > context.accepted.revision:
        ack = task.draft_ack
        if (ack is None or ack.basis != task.basis or ack.viewed_working != _ref(working)
                or ack.to != _ref(context.accepted)):
            return TaskDesign("needs-review", NEEDS_REVIEW_CODE,
                              f"revision {working.revision} ({working.digest}) is a newer draft than the accepted "
                              f"revision {context.accepted.revision}, and nobody has recorded for these task inputs "
                              "whether it changes this task", task, context)
        return TaskDesign("current", None, f"{reason}; the newer draft r{working.revision} was acknowledged "
                          f"({ack.reason})", task, context)
    return TaskDesign("current", None, reason, task, context)


# Moves: start, acknowledgment and carry ---------------------------------------------------------------------


def _history_line(context, revision: int, digest: str) -> bool:
    return any(info.revision == revision and info.digest == digest for info in context.revisions)


def draft_decision(context, *, basis: str, revision: int, digest: str, reason: str, actor: str, at: str,
                   authority: dict | None, task: str = "<task>") -> Decision | None:
    """A draft acknowledgment of exactly the present working revision, or None when there is no newer draft."""
    working, accepted = context.current, context.accepted
    if accepted is None or working.revision <= accepted.revision:
        return None
    if (revision, digest) != (working.revision, working.digest):
        raise DesignError(CHANGED_CODE, f"the newest revision is r{working.revision} {working.digest}, not the "
                          f"r{revision} {digest} you read; read it again with oah design task {task}")
    return Decision("draft", _ref(accepted), None, basis, _ref(accepted), _ref(working), reason, actor, at,
                    secrets.token_hex(8), authority)


def next_record(old: TaskBinding | None, section: PacketDesign, context, *, identity: TaskIdentity,
                basis: TaskBasis, draft: Decision | None = None) -> tuple[TaskBinding, str, list[str]]:
    """The record `oah start --packet` writes, the action (bound, kept or rebound) and warnings. Pure.

    A binding is created or moved only onto the accepted revision the packet names. A changed basis keeps the
    binding but clears every decision made for the old inputs."""
    warnings: list[str] = []
    project = context.project
    if project.uid != section.uid:
        raise DesignError(BINDING_INVALID_CODE, f"the packet names {section.project} with project uid {section.uid}, "
                          f"but {section.project} here has uid {project.uid}: another project with the same ID. Rewrite "
                          "the packet's Design section from this repository's design with oah packet design <task>")
    if not _history_line(context, section.revision, section.digest):
        raise DesignError(BINDING_INVALID_CODE, f"revision {section.revision} with digest {section.digest} is not in "
                          f"the history of {section.project}/{section.design}; rewrite the Design section with oah "
                          "packet design <task>")
    accepted = context.accepted
    if accepted is None:
        raise DesignError(NOT_ACCEPTED_CODE, f"{section.project}/{section.design} has no accepted revision; accept the "
                          f"revision the task implements (oah design accept {section.project} {section.design} "
                          f"--revision {section.revision} --digest {section.digest} --reason <who approved it>) first")
    if old is not None and not old.same_design(section):
        raise DesignError(SOURCE_MISMATCH_CODE, f"this task follows {old.binding.project}/{old.binding.design}, and the "
                          f"packet names {section.project}/{section.design}; a task follows one design. Restore the "
                          "packet's Design section, or start another task for the other design")
    accepted_binding = DesignBinding(project.id, project.uid, section.design, accepted.revision, accepted.digest)
    if old is not None and old.identity != identity:
        old = None  # another task instance never lends its decisions
    if old is None or section.revision > old.binding.revision:
        if section.revision > accepted.revision:
            raise DesignError(NOT_ACCEPTED_CODE, f"the packet names revision {section.revision}, a draft; the accepted "
                              f"revision is {accepted.revision}. Accept revision {section.revision} first, or rewrite "
                              "the Design section to the accepted revision with oah packet design <task>")
        if section.revision < accepted.revision:
            raise DesignError(PACKET_STALE_CODE, f"the packet was written from revision {section.revision}, but "
                              f"revision {accepted.revision} is accepted. Rewrite the Design section with oah packet "
                              "design <task> (and raise the intent Revision if the requirements changed), commit, then "
                              "start again")
        record = TaskBinding(identity, basis.digest, accepted_binding)
        action = "bound" if old is None else "rebound"
    elif old.holds(section.binding()) and old.binding.revision == accepted.revision:
        if old.basis == basis.digest:
            record, action = old, "kept"
        else:
            record, action = TaskBinding(identity, basis.digest, old.binding), "rebased"
            warnings.append(f"{INPUTS_CHANGED_CODE}: the task's intent, scope or gates changed, so earlier carries and "
                            "the draft acknowledgment no longer apply; evidence made for the old inputs stops counting")
    else:
        raise DesignError(PACKET_STALE_CODE, f"the packet names revision {section.revision}, which is not this task's "
                          f"binding (r{old.binding.revision}) or one carried to it, while revision {accepted.revision} "
                          f"is accepted. Record that the change does not affect the task with oah design carry, or "
                          "rewrite the Design section with oah packet design <task>, commit and start again")
    if draft is not None:
        if draft.basis != basis.digest or draft.to != _ref(accepted):
            raise DesignError(CHANGED_CODE, "the draft acknowledgment was made for other task inputs; read the design "
                              "again with oah design task <task>")
        record = dataclasses.replace(record, draft_ack=draft)
    return record, action, warnings


def carry(old: TaskBinding, context, *, basis: TaskBasis, revision: int, digest: str, reason: str, agent: str,
          at: str, authority: dict | None, task: str = "<task>") -> tuple[TaskBinding | None, str]:
    """Record that every change up to the working revision you read leaves this task unchanged. Pure.

    Only for the same task instance and basis (else OAH_DESIGN_TASK_INPUTS_CHANGED). From stale the binding moves to
    the accepted revision and the old one is carried; a newer draft is acknowledged. Never from invalid, edited,
    unknown or missing."""
    if old.basis != basis.digest or old.identity != basis.identity:
        raise DesignError(INPUTS_CHANGED_CODE, f"task {task}'s intent, scope or gates are not the ones its design "
                          "binding was made for, so a carry cannot keep its evidence; bind them together again with "
                          f"oah start --task {task} --agent {agent} --packet <packet>")
    judged = judge_with_context(old, context)
    working, accepted = context.current, context.accepted
    if judged.state == "current":
        return None, f"{old.binding.line()} is already current; nothing to carry"
    if judged.state not in ("stale", "needs-review"):
        raise judged.error("carry", task, agent)
    if (revision, digest) != (working.revision, working.digest):
        raise DesignError(CHANGED_CODE, f"the newest revision is r{working.revision} {working.digest}, not the "
                          f"r{revision} {digest} you read; read it again with oah design task {task}, then carry")
    if judged.state == "needs-review":
        ack = Decision("draft", _ref(accepted), None, old.basis, _ref(accepted), _ref(working), reason, agent, at,
                       secrets.token_hex(8), authority)
        return require_fits(dataclasses.replace(old, draft_ack=ack), task), "draft-acknowledged"
    moved = DesignBinding(old.binding.project, old.binding.uid, old.binding.design, accepted.revision,
                          accepted.digest)
    edge = Decision("carry", _ref(accepted), {"revision": old.binding.revision, "digest": old.binding.digest},
                    old.basis, _ref(accepted), _ref(working), reason, agent, at, secrets.token_hex(8), authority)
    ack = None
    if working.revision > accepted.revision:
        ack = Decision("draft", _ref(accepted), None, old.basis, _ref(accepted), _ref(working), reason, agent, at,
                       secrets.token_hex(8), authority)
    record = TaskBinding(old.identity, old.basis, moved, (*old.carried, edge), ack)
    return require_fits(record, task), "carried"


# Finish: exact generated design outputs (B2) ---------------------------------------------------------------


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def output_baseline(root: pathlib.Path, settings, binding: DesignBinding, *, head: str | None) -> dict:
    """What the bound project's outputs were at design-bound lease start; the list page tail is the user's part."""
    from oah.design import fs, pages

    root = pathlib.Path(root)
    project = binding.project
    try:
        record = fs.read_file(root / fs.project_record_rel(project))
        data = fs.read_file(root / fs.project_page_rel(settings.dir, project), limit=fs.PAGE_LIMIT)
        view = pages.read_project_page(data, project)
    except Exception as exc:  # an unreliable baseline grants no automatic exemption
        return {"version": 1, "reliable": False, "problem": type(exc).__name__}
    page = {"state": "missing" if data is None else view.state, "sha256": _sha(data),
            "tail_bytes": len(view.tail), "tail_sha256": _sha(view.tail), "eol": view.eol.decode("ascii")}
    return {"version": 1, "reliable": view.state in ("missing", "marked"), "project_uid": binding.uid,
            "project": project, "design": binding.design, "project_json_sha256": _sha(record),
            "wiki_dir": str(settings.dir), "head_at_start": head, "project_page": page}


def _registry_template() -> bytes:
    import importlib.resources

    return (importlib.resources.files("oah") / "templates" / "design" / "registry-readme.md").read_bytes() \
        .replace(b"\r\n", b"\n")


def _at_start(root: pathlib.Path, head: str | None, rel: str) -> bytes | None:
    from oah.engines.proc import run_command
    from oah.paths import GIT_TIMEOUT, git_argv, run_git

    if head is None:
        return None
    listed = run_git(root, "--literal-pathspecs", "ls-tree", "-z", "--full-tree", head, "--", rel)
    if listed.returncode != 0 or listed.timed_out:
        raise ValueError(f"{rel} at {head[:12]} has unknown tree membership")
    if listed.stdout == "":
        return None
    rows = listed.stdout.split("\0")
    if (len(rows) != 2 or rows[-1] != "" or "\t" not in rows[0]
            or rows[0].split("\t", 1)[1] != rel):
        raise ValueError(f"{rel} at {head[:12]} has ambiguous tree membership")
    exists = run_git(root, "cat-file", "-e", f"{head}:{rel}")
    if exists.returncode != 0 or exists.timed_out:
        raise ValueError(f"{rel} at {head[:12]} has unknown object availability")
    shown = run_command([*git_argv(), "-C", str(root), "cat-file", "blob", f"{head}:{rel}"], timeout=GIT_TIMEOUT,
                        encoding="latin-1")
    if shown.returncode != 0 or shown.timed_out:
        raise ValueError(f"{rel} at {head[:12]} cannot be read")
    return shown.stdout.encode("latin-1")


def generated_outputs(root: pathlib.Path, settings, record: TaskBinding, changed: Sequence[str],
                      baseline: Mapping | None) -> dict:
    """The generated-content equivalence report for changed design outputs (not proof of which process wrote them).

    Only the bound project's bound design records on its verified chain, A's registry README, the bound design's
    page and the bound project's list page may be exempt from ordinary scope, and only when their complete bytes
    equal what oah generates from verified records. project.json, other projects, deletions, orphans, unknown names,
    a changed user tail and a missing baseline stay ordinary."""
    from oah.design import fs, pages, store as design_store
    from oah.design.model import DesignError

    root = pathlib.Path(root)
    binding = record.binding
    project_id, design = binding.project, binding.design
    reader = fs.WorktreeReader(root, settings.dir)
    verified, ordinary = [], []
    head = (baseline or {}).get("head_at_start")
    loaded = {}

    def chain():
        if "record" not in loaded:
            project = design_store.load_project(reader, project_id)
            if project.uid != binding.uid:
                raise DesignError(BINDING_INVALID_CODE, "another project with the same ID")
            loaded["project"], loaded["record"] = project, design_store.load_design(reader, project, design)
        return loaded["project"], loaded["record"]

    prefix = fs.design_dir_rel(project_id, design) + "/"
    for rel in changed:
        if not fs.is_design_output(settings, rel):
            continue
        path = root / pathlib.PurePosixPath(rel)
        try:
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        except OSError:
            data = None
        if rel.endswith(("/index.md", "/log.md")) and not rel.startswith((fs.REGISTRY_DIR + "/",
                                                                           fs.pages_rel(settings.dir) + "/")):
            continue  # wiki maintenance keeps its own contract for index/log/archives
        reason, kind, revision = None, None, None
        try:
            if data is None:
                reason = "deleted or not a regular file"
            elif rel == fs.readme_rel():
                kind = "registry-readme"
                reason = None if data.replace(b"\r\n", b"\n") == _registry_template() else "not A's template"
            elif rel == fs.project_record_rel(project_id):
                reason = "project.json always needs explicit scope"
            elif rel.startswith(prefix):
                project, verified_record = chain()
                rest = rel[len(prefix):]
                parts = rest.split("/")
                if rest == "design.json":
                    kind = "pointer"
                elif len(parts) == 3 and parts[0] == "revisions" and fs.revision_number(parts[1]):
                    revision = fs.revision_number(parts[1])
                    info = verified_record.revision(revision)
                    if info is None:
                        reason = "a revision that is not on the verified chain"
                    elif parts[2] == "revision.json":
                        kind = "manifest"
                    elif parts[2].endswith(".md") and parts[2][:-3] in info.parts:
                        kind = "part"
                        design_store.read_texts(reader, info)
                    else:
                        reason = "a file the revision does not name"
                elif len(parts) == 2 and parts[0] == "acceptances" and fs.acceptance_number(parts[1]):
                    kind = "acceptance"
                    if fs.acceptance_number(parts[1]) not in {item.sequence for item in verified_record.acceptances}:
                        reason = "an acceptance that is not on the verified chain"
                else:
                    reason = "an unknown design record name"
                before = _at_start(root, head, rel) if reason is None and kind != "pointer" else None
                if reason is None and before is not None and before != data:
                    reason = "an existing historical record changed"
            elif rel == fs.design_page_rel(settings.dir, project_id, design):
                project, verified_record = chain()
                kind = "design-page"
                match = None
                for info in verified_record.chain:
                    texts = design_store.read_texts(reader, info)
                    if data == pages.render_design_page(project.id, project.title, info, texts).encode("utf-8"):
                        match = info.revision
                        break
                revision = match
                reason = None if match is not None else "not the page oah renders for any verified revision"
            elif rel == fs.project_page_rel(settings.dir, project_id):
                project, _verified = chain()
                kind = "project-page"
                page = (baseline or {}).get("project_page") or {}
                view = pages.read_project_page(data, project_id)
                if not (baseline or {}).get("reliable") or view.state != "marked":
                    reason = "no reliable start baseline or not an oah project page"
                elif page.get("state") == "missing" and view.tail:
                    reason = "a new project page with user text below its end marker"
                elif page.get("state") == "marked" and (_sha(view.tail) != page.get("tail_sha256")
                                                        or len(view.tail) != page.get("tail_bytes")
                                                        or view.eol.decode("ascii") != page.get("eol")):
                    reason = "the user's notes below the end marker changed"
                else:
                    rows = []
                    for item in sorted(design_store._design_names(reader, project_id)):
                        head_facts = design_store.load_head(reader, project, item)
                        rows.append(pages.list_line(project_id, item, head_facts.title, head_facts.revision,
                                                    head_facts.status, head_facts.accepted, head_facts.updated))
                    if data != pages.build_project_page(view, project, rows):
                        reason = "not the list oah builds from the verified records"
            else:
                reason = "an output of another project or design"
        except (DesignError, ValueError, OSError) as exc:
            reason = f"does not verify ({type(exc).__name__})"
        except Exception as exc:  # A limitation: a missing Git object may escape as another exception
            reason = f"does not verify ({type(exc).__name__})"
        if reason is None:
            proof = _digest({"path": rel, "kind": kind, "sha256": _sha(data), "binding": binding.to_json()})
            verified.append({"path": rel, "sha256": _sha(data), "bytes": len(data), "kind": kind,
                             "project_uid": binding.uid, "revision": revision, "proof_digest": proof})
        else:
            ordinary.append({"path": rel, "reason": reason})
    return {"version": 1, "verified": verified, "ordinary": ordinary,
            "baseline_digest": _digest(dict(baseline)) if baseline else None}


def judge_task(state: pathlib.Path, task: str, *, root: pathlib.Path, view: TaskView | None = None,
               packet_text: str | None = None, source: str | None = None) -> TaskDesign:
    """The task's design judgement, read-only: the open lease's record against its own inputs, else the closing
    (or last) epoch's record against that epoch's frozen intent, scope and gates; else the origin evidence."""
    view = view if view is not None else task_view(state, task)
    lease = view.lease
    if view.unreadable is not None:
        return judge(None, root=root, found=Origin("unknown", view.unreadable))
    if lease is not None and lease.design_invalid:
        return judge(None, root=root, invalid=lease.design_field.identity())
    if view.problem is not None:
        return judge(None, root=root, found=Origin("invalid", view.problem))
    if lease is not None:
        where = pathlib.Path(lease.worktree) if pathlib.Path(lease.worktree).is_dir() else root
        try:
            record = lease.design_record()
        except RecordInvalid as exc:
            return judge(None, root=where, invalid=exc.identity)
        if record is None:
            found = origin(state, task, packet_text=packet_text, root=where if source else None, source=source,
                           view=view)
            return judge(None, root=where, found=found)
        from oah.lease import utc_now

        if lease.expired(utc_now()):
            return TaskDesign("required", CHANGED_CODE, "the task's execution lease expired; resume it before "
                              "using design authority", record)
        problem = _intent_problem(record, lease.intent, where)
        if problem is not None:
            return problem
        return judge(record, root=where, basis=basis_of(record, intent=lease.intent, scope=lease.scope,
                                                        gates=lease.gates))
    if view.record is None:
        found = origin(state, task, packet_text=packet_text, root=root if source else None, source=source,
                       view=view)
        return judge(None, root=root, found=found)
    if view.closed in ("released", "expired"):
        # Only a finished epoch can be landed or carried closed; a released or expired one must be resumed first.
        return judge(None, root=root, found=Origin("closed", f"its last lease was {view.closed}, not finished; "
                                                               "resume it with oah start --packet"))
    epoch = view.closing or view.epoch or {}
    where = pathlib.Path(epoch["worktree"]) if epoch.get("worktree") and pathlib.Path(epoch["worktree"]).is_dir() \
        else root
    basis = basis_of(view.record, intent=epoch.get("intent"), scope=epoch.get("scope") or (),
                     gates=epoch.get("gates") or ())
    problem = _intent_problem(view.record, epoch.get("intent"), where)
    if problem is not None:
        return problem
    return judge(view.record, root=where, basis=basis)


def _intent_problem(record: TaskBinding, bound: Mapping | None, root: pathlib.Path) -> TaskDesign | None:
    from oah import intent

    try:
        binding = intent.Binding.from_json(bound) if bound is not None else None
        state, reason = intent.explain_binding(binding, root)
    except (ValueError, OSError) as exc:
        return _unknown(record, f"the actual task intent cannot be checked ({type(exc).__name__})")
    if state != "current":
        return TaskDesign("stale", INPUTS_CHANGED_CODE, f"the actual task intent is {state}: {reason}", record)
    return None


# Gate provenance and review evidence --------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Provenance:
    """What gate evidence of a design-origin task may certify: the current key and the carried keys."""

    current: str | None
    accepts: frozenset[str]
    problem: str | None = None
    state: str | None = None


def gate_provenance(state: pathlib.Path, task: str, *, root: pathlib.Path | None = None) -> Provenance | None:
    """None for an origin-free (legacy) task. Otherwise the authoritative judgement decides: anything but current
    accepts no key, so a stale, needs-review or unknown design never leaves a gate fresh."""
    view = task_view(state, task)
    if view.unreadable is not None:
        return Provenance(None, frozenset(), view.unreadable, "unknown")
    if view.record is None and not view.origin and view.problem is None:
        return None
    if view.problem is not None:  # the same rule as judge_task: a malformed event never leaves a record in force
        return Provenance(None, frozenset(), view.problem, "invalid")
    if view.record is None:
        return Provenance(None, frozenset(), "the task follows a design but has no binding", "required")
    lease = view.lease
    if lease is None and view.closed in ("released", "expired"):
        return Provenance(view.record.key, frozenset(), f"its last lease was {view.closed}, not finished",
                          "required")
    if lease is not None:
        where = pathlib.Path(lease.worktree)
    else:
        closing = view.closing or view.epoch or {}
        where = pathlib.Path(closing.get("worktree") or root or ".")
    judged = judge_task(state, task, root=root or where, view=view)
    if judged.state != "current":
        return Provenance(view.record.key, frozenset(), f"{judged.code}: {judged.reason}", judged.state)
    return Provenance(view.record.key, view.record.keys(), view.problem, judged.state)


@dataclasses.dataclass(frozen=True)
class ReviewEvidence:
    """Whether a review receipt counts for the task's design now: current state and a key it still holds."""

    state: str
    record: TaskBinding | None

    @staticmethod
    def receipt_key(receipt: Mapping) -> str | None:
        design = receipt.get("design")
        key = design.get("evidence_key") if isinstance(design, Mapping) else None
        return key if isinstance(key, str) and _DIGEST.fullmatch(key) else None

    def counts(self, receipt: Mapping) -> bool:
        return self.state == "current" and self.record is not None and self.receipt_key(receipt) in self.record.keys()

    def why(self, receipt: Mapping) -> str:
        if self.state != "current":
            return f"the task's design is {self.state}, so no review can complete it until that is resolved"
        if self.receipt_key(receipt) is None:
            return "the receipt records no design evidence key, so it reviewed no design"
        return "the receipt reviewed a design binding or task inputs this task no longer counts (it was rebound)"


def review_evidence(judged: TaskDesign) -> ReviewEvidence | None:
    """None for a legacy task (no design, no origin); else what review convergence must check."""
    if judged.state == "unbound":
        return None
    return ReviewEvidence(judged.state, judged.task)


def receipt_design(run_record: object) -> dict | None:
    """The part of a reviewer run's design.json a receipt keeps (identities and digests, no design text)."""
    if not isinstance(run_record, Mapping):
        return None
    task = run_record.get("task")
    binding = task.get("binding") if isinstance(task, Mapping) else None
    return {"binding": binding, "evidence_key": run_record.get("evidence_key"), "state": run_record.get("state"),
            "accepted": run_record.get("accepted"), "working": run_record.get("working"),
            "included": run_record.get("included"), "block_sha256": run_record.get("block_sha256"),
            "fingerprint": run_record.get("fingerprint")}
