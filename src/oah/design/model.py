# 설계 기록 모델 — ID 규칙, 본문 정규화, 내용·목록·사건 다이제스트, 기록 JSON 검증, 결속(DesignBinding), 진단과 오류 코드를 둔다.
"""What a design record is, independent of where it is stored.

IDs are lowercase ASCII words joined by single hyphens, so every ID is one safe path component on macOS and
Windows. Text parts are normalized once (UTF-8, LF, one final newline) and stored in that form; every digest is
`sha256:` plus the hex digest of canonical JSON (sorted keys, no spaces), computed from parsed data so a CRLF
checkout never changes it. The content digest covers the project uid, the IDs, the title and the part digests.
The manifest digest covers a whole revision manifest, which names its parent's manifest digest: a hash chain.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import re
import secrets
from typing import Mapping

from oah.intent import DIGEST_RE, MAX_BYTES
from oah.lease import TASK_RE
from oah.wiki.base import EXIT_ENVIRONMENT, EXIT_REFUSED, WikiError

SCHEMA_VERSION = 1
ID_MAX = 40
PART_MAX = 32
MAX_PARTS = 16
LINE_MAX = 200
FACT_MAX = 60
DESIGN_PART = "design"
REQUEST_PART = "request"
RESERVED_PARTS = ("design", "request", "materials", "coverage")
ORIGIN_NEW, ORIGIN_SAVE, ORIGIN_WIKI = "new", "save", "wiki-edit"
EVENT_KINDS = ("registered", "renamed", "created", "saved", "accepted")

NOT_FOUND_CODE = "OAH_DESIGN_NOT_FOUND"
EXISTS_CODE = "OAH_DESIGN_EXISTS"
ID_INVALID_CODE = "OAH_DESIGN_ID_INVALID"
INPUT_INVALID_CODE = "OAH_DESIGN_INPUT_INVALID"
CONTEXT_STALE_CODE = "OAH_DESIGN_CONTEXT_STALE"
UNREGISTERED_EDIT_CODE = "OAH_DESIGN_UNREGISTERED_EDIT"
REVISION_CONFLICT_CODE = "OAH_DESIGN_REVISION_CONFLICT"
NOT_AUTHORITY_CODE = "OAH_DESIGN_NOT_AUTHORITY"
PAGE_FOREIGN_CODE = "OAH_DESIGN_PAGE_FOREIGN"
BINDING_INVALID_CODE = "OAH_DESIGN_BINDING_INVALID"
FRESHNESS_UNKNOWN_CODE = "OAH_DESIGN_FRESHNESS_UNKNOWN"
RECOVERY_PENDING_CODE = "OAH_DESIGN_RECOVERY_PENDING"
RECOVERY_CONFLICT_CODE = "OAH_DESIGN_RECOVERY_CONFLICT"
JOURNAL_CORRUPT_CODE = "OAH_DESIGN_JOURNAL_CORRUPT"
CORRUPT_CODE = "OAH_DESIGN_CORRUPT"
PATH_INVALID_CODE = "OAH_DESIGN_PATH_INVALID"
PAGE_STALE_CODE = "OAH_DESIGN_PAGE_STALE"
PAGE_MISSING_CODE = "OAH_DESIGN_PAGE_MISSING"
LIST_STALE_CODE = "OAH_DESIGN_LIST_STALE"
ORPHAN_CODE = "OAH_DESIGN_ORPHAN_RECORD"
BEHIND_CODE = "OAH_DESIGN_BEHIND"

_ENVIRONMENT_CODES = {RECOVERY_PENDING_CODE, RECOVERY_CONFLICT_CODE, JOURNAL_CORRUPT_CODE, CORRUPT_CODE,
                      PATH_INVALID_CODE, FRESHNESS_UNKNOWN_CODE}
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_DEVICE_NAMES = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
_UID = re.compile(r"[0-9a-f]{32}")
_HEX = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+")


class DesignError(WikiError):
    """A design refusal; the message starts with the OAH_DESIGN_* code."""

    def __init__(self, code: str, message: str, exit_code: int | None = None) -> None:
        if exit_code is None:
            exit_code = EXIT_ENVIRONMENT if code in _ENVIRONMENT_CODES else EXIT_REFUSED
        super().__init__(code, message, exit_code)


def corrupt(where: str, problem: str) -> DesignError:
    return DesignError(CORRUPT_CODE, f"{where}: {problem}. oah never repairs design records; restore the file from "
                       "git (for example `git checkout -- <path>`) or from the authority branch, then retry")


# IDs and single-line fields ------------------------------------------------------------------------


def check_id(value: object, what: str = "project", limit: int = ID_MAX) -> str:
    """A project, design or part ID, or OAH_DESIGN_ID_INVALID."""
    if (not isinstance(value, str) or len(value) > limit or not _ID.fullmatch(value)
            or value in _DEVICE_NAMES):
        raise DesignError(ID_INVALID_CODE, f"{what} ID {value!r}: use lowercase letters and digits joined by single "
                          f"hyphens, up to {limit} characters, such as academy-booking (not a Windows device name "
                          "such as con or nul)")
    return value


def check_part(value: object) -> str:
    return check_id(value, "part", PART_MAX)


def check_event(value: object | None) -> str:
    """A caller's event ID (the task ID syntax), or a new random one."""
    if value is None:
        return secrets.token_hex(8)
    if not isinstance(value, str) or not TASK_RE.fullmatch(value):
        raise DesignError(ID_INVALID_CODE, f"event ID {value!r}: use letters, digits, '.', '_' or '-' (up to 64, "
                          "starting with a letter or digit)")
    return value


def check_revision(value: object, what: str = "revision") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 999_999_999:
        raise DesignError(ID_INVALID_CODE, f"{what} {value!r}: use a whole number from 1")
    return value


def check_digest(value: object, what: str = "digest") -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise DesignError(ID_INVALID_CODE, f"{what} {value!r}: use the sha256:<64 hex> digest that oah design "
                          "status or show printed")
    return value


def check_line(value: object, what: str, limit: int = LINE_MAX) -> str:
    """A one-line text field such as a title or a reason, stripped."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or _CONTROL.search(text) or len(text) > limit:
        raise DesignError(INPUT_INVALID_CODE, f"give the {what} as one line of up to {limit} characters, without "
                          "tabs or other control characters")
    return text


def check_fact(value: object) -> str:
    """A short status word a later lane adds to a project list line."""
    text = check_line(value, "fact", FACT_MAX)
    if any(mark in text for mark in ("[", "]", "|", "<", ">", ";")):
        raise DesignError(INPUT_INVALID_CODE, f"fact {text!r}: use plain words without [ ] | < > ;")
    return text


def new_uid() -> str:
    return secrets.token_hex(16)


def is_branch_ref(value: str) -> bool:
    return (value.startswith("refs/heads/") and not _REF_FORBIDDEN.search(value)
            and not any(mark in value for mark in ("..", "@{")) and not value.endswith(".")
            and all(part and not part.startswith(".") and not part.endswith(".lock") for part in value.split("/")))


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


# Text ---------------------------------------------------------------------------------------------


def _normal_form(text: str) -> str:
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def normalize_text(value: object, what: str) -> str:
    """Text a caller gives for a part: LF line ends and one final newline, or OAH_DESIGN_INPUT_INVALID.

    No line may start with `<!-- oah` (any case, after leading spaces): that keeps every oah marker unambiguous
    wherever the text is shown.
    """
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DesignError(INPUT_INVALID_CODE, f"the {what} text is not UTF-8 ({exc}); save it as UTF-8") from exc
    if not isinstance(value, str):
        raise DesignError(INPUT_INVALID_CODE, f"the {what} text must be text")
    text = _normal_form(value)
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DesignError(INPUT_INVALID_CODE, f"the {what} text holds characters UTF-8 cannot store ({exc})") from exc
    if "\x00" in text:
        raise DesignError(INPUT_INVALID_CODE, f"the {what} text holds a NUL character; remove it")
    if not text.strip():
        raise DesignError(INPUT_INVALID_CODE, f"the {what} text is empty; give the text, or drop the part")
    if len(encoded) >= MAX_BYTES:
        raise DesignError(INPUT_INVALID_CODE, f"the {what} text is {len(encoded)} bytes; a part must be under "
                          f"{MAX_BYTES} bytes (1 MiB)")
    for number, line in enumerate(text.split("\n"), start=1):
        if line.lstrip().casefold().startswith("<!-- oah"):
            raise DesignError(INPUT_INVALID_CODE, f"line {number} of the {what} text starts with '<!-- oah', which "
                              "oah keeps for its own markers; indent it inside a code example or reword it")
    return text


def stored_text(data: bytes) -> str | None:
    """A stored part in normal form, or None when the bytes are not UTF-8."""
    try:
        return _normal_form(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest_of(data: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(data)).hexdigest()


def file_bytes(data: Mapping) -> bytes:
    """How oah writes a record: indented JSON with sorted keys, like `atomic.write_json_atomic`."""
    return (json.dumps(dict(data), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def content_digest(uid: str, project: str, design: str, title: str, parts: Mapping[str, str]) -> str:
    return digest_of({"uid": uid, "project": project, "design": design, "title": title, "parts": dict(parts)})


def event_key(kind: str, uid: str, project: str, design: str, revision: int, digest: str) -> str:
    """The once-only key of one event's log entry."""
    return hashlib.sha256(canonical({"kind": kind, "uid": uid, "project": project, "design": design,
                                     "revision": revision, "digest": digest})).hexdigest()


def title_digest(title: str) -> str:
    return digest_of({"title": title})


# Records ------------------------------------------------------------------------------------------


def _get(data: Mapping, key: str, kind: type | tuple, where: str):
    value = data.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise corrupt(where, f"{key} is missing or malformed")
    return value


def _version(data: object, kind: str, where: str) -> Mapping:
    if not isinstance(data, Mapping) or data.get("version") != SCHEMA_VERSION or data.get("kind") != kind:
        raise corrupt(where, f"not a version-{SCHEMA_VERSION} {kind} record")
    return data


def _id(data: Mapping, key: str, where: str, limit: int = ID_MAX) -> str:
    value = data.get(key)
    if not isinstance(value, str) or len(value) > limit or not _ID.fullmatch(value) or value in _DEVICE_NAMES:
        raise corrupt(where, f"{key} is not a valid ID")
    return value


def _digest(data: Mapping, key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise corrupt(where, f"{key} is not a sha256 digest")
    return value


def _number(data: Mapping, key: str, where: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise corrupt(where, f"{key} is not a whole number from 1")
    return value


def _text(data: Mapping, key: str, where: str, limit: int = LINE_MAX) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value) or len(value) > limit:
        raise corrupt(where, f"{key} is not a one-line text")
    return value


def _when(data: Mapping, key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        raise corrupt(where, f"{key} is not a UTC time")
    return value


@dataclasses.dataclass(frozen=True)
class ProjectRecord:
    id: str
    uid: str
    title: str
    title_revision: int
    former_titles: tuple[str, ...]
    authority: str
    created_at: str
    event: str
    record_bytes: bytes | None = dataclasses.field(default=None, compare=False, repr=False)

    def to_json(self) -> dict:
        return {"version": SCHEMA_VERSION, "kind": "oah-design-project", "id": self.id, "uid": self.uid,
                "title": self.title, "title_revision": self.title_revision,
                "former_titles": list(self.former_titles), "authority": self.authority,
                "created_at": self.created_at, "event": self.event}

    def public_json(self) -> dict:
        return {"id": self.id, "uid": self.uid, "title": self.title, "authority": self.authority}

    @classmethod
    def from_json(cls, data: object, where: str, project: str) -> "ProjectRecord":
        data = _version(data, "oah-design-project", where)
        record = cls(id=_id(data, "id", where), uid=_get(data, "uid", str, where), title=_text(data, "title", where),
                     title_revision=_number(data, "title_revision", where),
                     former_titles=tuple(_get(data, "former_titles", list, where)),
                     authority=_get(data, "authority", str, where), created_at=_when(data, "created_at", where),
                     event=_get(data, "event", str, where))
        if record.id != project:
            raise corrupt(where, f"id is {record.id!r}, but the folder is {project!r}")
        if not _UID.fullmatch(record.uid):
            raise corrupt(where, "uid is not 32 hex characters")
        if not is_branch_ref(record.authority):
            raise corrupt(where, "authority is not a refs/heads/<branch> ref")
        if not TASK_RE.fullmatch(record.event):
            raise corrupt(where, "event is not an event ID")
        if (len(record.former_titles) != record.title_revision - 1
                or not all(isinstance(item, str) and item.strip() and not _CONTROL.search(item)
                           for item in record.former_titles)):
            raise corrupt(where, "former_titles does not match title_revision")
        return record


@dataclasses.dataclass(frozen=True)
class RevisionInfo:
    project: str
    uid: str
    design: str
    revision: int
    parent: tuple[int, str] | None  # (revision, manifest digest)
    digest: str
    title: str
    parts: Mapping[str, str]  # part name -> part digest
    reason: str
    origin: str
    event: str
    at: str
    manifest: str = ""  # digest of to_json(), filled by build() and from_json()

    def to_json(self) -> dict:
        return {"version": SCHEMA_VERSION, "kind": "oah-design-revision", "project": self.project, "uid": self.uid,
                "design": self.design, "revision": self.revision,
                "parent": None if self.parent is None else {"revision": self.parent[0], "manifest": self.parent[1]},
                "digest": self.digest, "title": self.title, "parts": dict(sorted(self.parts.items())),
                "reason": self.reason, "origin": self.origin, "event": self.event, "at": self.at}

    def public_json(self) -> dict:
        data = self.to_json()
        for key in ("version", "kind"):
            data.pop(key)
        data["manifest"] = self.manifest
        return data

    @classmethod
    def build(cls, **fields) -> "RevisionInfo":
        info = cls(**fields)
        return dataclasses.replace(info, manifest=digest_of(info.to_json()))

    @classmethod
    def from_json(cls, data: object, where: str) -> "RevisionInfo":
        data = _version(data, "oah-design-revision", where)
        parent = data.get("parent")
        if parent is not None:
            if not isinstance(parent, Mapping):
                raise corrupt(where, "parent is malformed")
            parent = (_number(parent, "revision", where), _digest(parent, "manifest", where))
        parts = _get(data, "parts", Mapping, where)
        if not parts or len(parts) > MAX_PARTS or DESIGN_PART not in parts:
            raise corrupt(where, "parts must name the design part and at most 16 parts")
        for name, value in parts.items():
            if (not isinstance(name, str) or len(name) > PART_MAX or not _ID.fullmatch(name) or name in _DEVICE_NAMES
                    or not isinstance(value, str) or not DIGEST_RE.fullmatch(value)):
                raise corrupt(where, f"part {name!r} is malformed")
        origin = _get(data, "origin", str, where)
        if len(origin) > PART_MAX or not _ID.fullmatch(origin):
            raise corrupt(where, "origin is malformed")
        info = cls(project=_id(data, "project", where), uid=_get(data, "uid", str, where),
                   design=_id(data, "design", where), revision=_number(data, "revision", where), parent=parent,
                   digest=_digest(data, "digest", where), title=_text(data, "title", where),
                   parts=dict(sorted(parts.items())), reason=_text(data, "reason", where), origin=origin,
                   event=_get(data, "event", str, where), at=_when(data, "at", where))
        if not _UID.fullmatch(info.uid) or not TASK_RE.fullmatch(info.event):
            raise corrupt(where, "uid or event is malformed")
        if parent is not None and parent[0] >= info.revision:
            raise corrupt(where, "parent revision is not lower than the revision")
        if info.digest != content_digest(info.uid, info.project, info.design, info.title, info.parts):
            raise corrupt(where, "digest does not match its uid, IDs, title and part digests")
        return dataclasses.replace(info, manifest=digest_of(info.to_json()))


@dataclasses.dataclass(frozen=True)
class AcceptanceInfo:
    project: str
    uid: str
    design: str
    sequence: int
    previous: tuple[int, str] | None  # (sequence, record digest)
    revision: int
    digest: str
    manifest: str
    reason: str
    event: str
    at: str
    record: str = ""  # digest of to_json()

    def to_json(self) -> dict:
        return {"version": SCHEMA_VERSION, "kind": "oah-design-acceptance", "project": self.project,
                "uid": self.uid, "design": self.design, "sequence": self.sequence,
                "previous": None if self.previous is None else {"sequence": self.previous[0],
                                                                "record": self.previous[1]},
                "revision": self.revision, "digest": self.digest, "manifest": self.manifest,
                "reason": self.reason, "event": self.event, "at": self.at}

    def public_json(self) -> dict:
        data = self.to_json()
        for key in ("version", "kind"):
            data.pop(key)
        data["record"] = self.record
        return data

    @classmethod
    def build(cls, **fields) -> "AcceptanceInfo":
        info = cls(**fields)
        return dataclasses.replace(info, record=digest_of(info.to_json()))

    @classmethod
    def from_json(cls, data: object, where: str) -> "AcceptanceInfo":
        data = _version(data, "oah-design-acceptance", where)
        previous = data.get("previous")
        if previous is not None:
            if not isinstance(previous, Mapping):
                raise corrupt(where, "previous is malformed")
            previous = (_number(previous, "sequence", where), _digest(previous, "record", where))
        info = cls(project=_id(data, "project", where), uid=_get(data, "uid", str, where),
                   design=_id(data, "design", where), sequence=_number(data, "sequence", where), previous=previous,
                   revision=_number(data, "revision", where), digest=_digest(data, "digest", where),
                   manifest=_digest(data, "manifest", where), reason=_text(data, "reason", where),
                   event=_get(data, "event", str, where), at=_when(data, "at", where))
        if not _UID.fullmatch(info.uid) or not TASK_RE.fullmatch(info.event):
            raise corrupt(where, "uid or event is malformed")
        if previous is not None and previous[0] >= info.sequence:
            raise corrupt(where, "previous sequence is not lower than the sequence")
        return dataclasses.replace(info, record=digest_of(info.to_json()))


@dataclasses.dataclass(frozen=True)
class Pointer:
    project: str
    uid: str
    design: str
    created_at: str
    current: tuple[int, str, str]  # revision, content digest, manifest digest
    accepted: tuple[int, int, str, str] | None  # sequence, revision, content digest, acceptance record digest

    def to_json(self) -> dict:
        accepted = None
        if self.accepted is not None:
            sequence, revision, digest, record = self.accepted
            accepted = {"sequence": sequence, "revision": revision, "digest": digest, "record": record}
        revision, digest, manifest = self.current
        return {"version": SCHEMA_VERSION, "kind": "oah-design", "project": self.project, "uid": self.uid,
                "id": self.design, "created_at": self.created_at,
                "current": {"revision": revision, "digest": digest, "manifest": manifest}, "accepted": accepted}

    @classmethod
    def from_json(cls, data: object, where: str) -> "Pointer":
        data = _version(data, "oah-design", where)
        current = _get(data, "current", Mapping, where)
        accepted = data.get("accepted")
        if accepted is not None:
            if not isinstance(accepted, Mapping):
                raise corrupt(where, "accepted is malformed")
            accepted = (_number(accepted, "sequence", where), _number(accepted, "revision", where),
                        _digest(accepted, "digest", where), _digest(accepted, "record", where))
        pointer = cls(project=_id(data, "project", where), uid=_get(data, "uid", str, where),
                      design=_id(data, "id", where), created_at=_when(data, "created_at", where),
                      current=(_number(current, "revision", where), _digest(current, "digest", where),
                               _digest(current, "manifest", where)),
                      accepted=accepted)
        if not _UID.fullmatch(pointer.uid):
            raise corrupt(where, "uid is malformed")
        return pointer


@dataclasses.dataclass(frozen=True)
class DesignBinding:
    """What a lease, packet or brief records about the design it was given (lane B stores this)."""

    project: str
    uid: str
    design: str
    revision: int
    digest: str

    def to_json(self) -> dict:
        return {"project": self.project, "uid": self.uid, "design": self.design, "revision": self.revision,
                "digest": self.digest}

    @classmethod
    def from_json(cls, data: object) -> "DesignBinding":
        """Rebuild a binding from `to_json()` output; raises ValueError on anything else."""
        if not isinstance(data, Mapping):
            raise ValueError(f"a design binding must be an object, not {data!r}")
        try:
            return cls(project=check_id(data.get("project")), uid=_uid(data.get("uid")),
                       design=check_id(data.get("design"), "design"), revision=check_revision(data.get("revision")),
                       digest=check_digest(data.get("digest")))
        except DesignError as exc:
            raise ValueError(f"malformed design binding: {exc}") from None

    def line(self) -> str:
        return f"DESIGN {self.project}/{self.design}@r{self.revision} {self.digest}"


def _uid(value: object) -> str:
    if not isinstance(value, str) or not _UID.fullmatch(value):
        raise DesignError(ID_INVALID_CODE, f"uid {value!r} is not 32 hex characters")
    return value


def is_hex_key(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX.fullmatch(value))


@dataclasses.dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str | None = None
    level: str = "warning"

    def to_json(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path, "level": self.level}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
