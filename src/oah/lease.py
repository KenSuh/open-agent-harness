# 리스 저장소 — 상태 잠금 아래에서 리스를 시작·갱신·종료·해제하고 모든 변경을 이벤트로 남기며, 읽기는 잠금도 생성도 없이 한다.
"""Leases: who is working on which paths of this repository, until when.

Every change happens under the single state lock and is written atomically; every change is also
appended to events.jsonl. `snapshot()` only reads: it takes no lock and creates nothing.

A lease may carry the intent it is bound to (`intent`, an `oah.intent.Binding` as JSON). The field is
optional in schema 1: a record without it is unbound, and an unbound lease writes no `intent` key.
`rebind` only moves a binding forward: the same Intent-ID at a higher Revision.

A lease may also carry the design its task follows (`design`, an `oah.design.binding.TaskBinding` as JSON), with
the same optional-field rule. `start`, `rebind` and `finish` take a design callback that runs under the state lock,
after the lease checks and before the commit, so the design is judged at the control point itself;
`carry_design` and `mark_design_origin` record design decisions, and every design change is in the events.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import os
import pathlib
import re
import secrets
from contextlib import contextmanager
from typing import Callable, Iterator, Sequence

import hashlib
import json
from typing import Mapping

from oah.atomic import StateCorrupt, StorageError
from oah.intent import Binding, explain_binding
from oah.lockfile import StateLock
from oah.paths import RepoInfo
from oah.scope import outside, overlaps


def read_bytes_bounded(path: pathlib.Path, limit: int) -> bytes | None:
    """A file's exact bytes, None when it does not exist; over `limit` bytes is StateCorrupt, never truncated.

    It reads like `oah.atomic.read_json`: Path.read_bytes with the same Windows permission retry."""
    from oah import atomic

    path = pathlib.Path(path)
    try:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = None  # still read below, exactly as read_json does, so a denied read is never "missing"
        if size is not None and size > limit:
            raise StateCorrupt(f"{StateCorrupt.code}: {path} is over the {limit}-byte limit; oah never truncates it")
        raw = atomic._retry_permission(path.read_bytes, atomic.RETRY_SECONDS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageError(path, exc) from exc
    if len(raw) > limit:
        raise StateCorrupt(f"{StateCorrupt.code}: {path} is over the {limit}-byte limit; oah never truncates it")
    return raw


SCHEMA_VERSION = 1
DEFAULT_TTL_HOURS = 4.0
MAX_TTL_HOURS = 168.0
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
AGENT_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")


class LeaseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _refuse_worker_lease() -> None:
    if os.environ.get("OAH_WORKER") == "1":
        raise LeaseError("OAH_WORKER_LEASE_REFUSED", "dispatched workers use the dispatcher's lease; "
                         "return your work to the dispatcher so it can manage the lease")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(text: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.utcoffset() is None:
        raise ValueError("lease timestamps must include a timezone")
    return moment


def _expiration(now: dt.datetime, ttl_hours: float) -> str:
    duration = max(dt.timedelta(microseconds=1), dt.timedelta(hours=ttl_hours))
    return _iso(now + duration)


@dataclasses.dataclass
class Lease:
    id: str
    task: str
    agent: str
    scope: list[str]
    note: str
    worktree: str
    branch: str | None
    head_at_start: str | None
    created_at: str
    expires_at: str
    gates: list[str] = dataclasses.field(default_factory=list)  # declared ledger paths (optional, schema 1)
    intent: dict | None = None  # the bound intent, Binding.to_json() (optional, schema 1)
    design: dict | None = None  # the followed design, TaskBinding.to_json() (optional, schema 1)
    generation: int = 0  # optional in schema 1; every successful mutation advances it
    # How the design value was read (absent, valid with its exact bytes, or task-local invalid); never serialized.
    design_field: "DesignField | None" = dataclasses.field(default=None, compare=False, repr=False)

    def expired(self, now: dt.datetime) -> bool:
        return _parse(self.expires_at) <= now

    def binding(self) -> Binding | None:
        return Binding.from_json(self.intent) if self.intent is not None else None

    @property
    def design_invalid(self) -> bool:
        return self.design_field is not None and self.design_field.kind == "invalid"

    def design_record(self):
        """The design record as an `oah.design.binding.TaskBinding`, or None for a lease without one.

        A task-local invalid design value raises `oah.design.binding.RecordInvalid` (never None, never legacy)."""
        from oah.design.binding import RecordInvalid, TaskBinding

        if self.design_invalid:
            raise RecordInvalid(self.task, self.design_field.identity())
        if self.design is None:
            return None
        return TaskBinding.from_json(self.design)

    def to_json(self) -> dict:
        """Persistence record, including the internal generation used to detect lease changes."""
        data = {field.name: getattr(self, field.name) for field in dataclasses.fields(self)
                if field.name != "design_field"}
        data = json.loads(json.dumps(data))  # independent copies of the bounded, validated values
        if data["intent"] is None:
            del data["intent"]  # an unbound lease keeps the record shape it always had
        if data["design"] is None:
            del data["design"]  # so does a lease whose task follows no design
        if data["generation"] == 0:
            del data["generation"]  # records written before generations remain compatible
        return data

    def public_json(self) -> dict:
        """Public lease output; generation is only a persistence and concurrency detail."""
        data = self.to_json()
        data.pop("generation", None)
        return data

    @classmethod
    def from_json(cls, data: object, source: pathlib.Path, *, design: "DesignField | None" = None) -> "Lease":
        """The core record. `design` is the task-local field the document reader isolated; when None, a `design`
        member in `data` is checked here (callers outside the document reader)."""
        if design is not None and isinstance(data, dict):
            data = {key: value for key, value in data.items() if key != "design"}
            if design.kind == "valid":
                data["design"] = design.value
        strings = ("id", "task", "agent", "note", "worktree", "created_at", "expires_at")
        optional = ("branch", "head_at_start")
        valid = (isinstance(data, dict)
                 and all(isinstance(data.get(key), str) for key in strings)
                 and all(data.get(key) is None or isinstance(data.get(key), str) for key in optional)
                 and isinstance(data.get("scope"), list) and data["scope"]
                 and all(isinstance(entry, str) for entry in data["scope"])
                 and type(data.get("generation", 0)) is int and data.get("generation", 0) >= 0
                 and (data.get("gates") is None or (isinstance(data["gates"], list)
                                                    and all(isinstance(entry, str) for entry in data["gates"]))))
        if valid:
            try:
                _parse(data["created_at"])
                _parse(data["expires_at"])
            except ValueError:
                valid = False
        if valid and data.get("intent") is not None:
            try:
                Binding.from_json(data["intent"])
            except ValueError:
                valid = False
        field = design
        if valid and design is None and data.get("design") is not None:
            from oah.design.binding import TaskBinding

            try:
                TaskBinding.from_json(data["design"])
                field = DesignField("valid", raw=json.dumps(data["design"], sort_keys=True), value=data["design"])
            except (ValueError, RecursionError):
                field = DesignField("invalid", code="not-a-task-binding")
        if not valid:
            raise StateCorrupt(f"{StateCorrupt.code}: {source} holds a malformed lease record; "
                               "repair the record before running the command again")
        values = {item.name: data.get(item.name) for item in dataclasses.fields(cls) if item.name != "design_field"}
        values["gates"] = list(values["gates"] or [])
        values["generation"] = data.get("generation", 0)
        if field is not None and field.kind != "valid":
            values["design"] = None
        return cls(**values, design_field=field or ABSENT)

    def describe(self) -> str:
        return (f"task {self.task} held by {self.agent} (scope {', '.join(self.scope)}; expires {self.expires_at}; "
                f"worktree {self.worktree})")


# The lease document: one bounded reader, a task-local optional design field and a lossless writer ------------
#
# leases.json is read lexically before any ordinary JSON decoding. Each top-level lease's optional `design` value
# is isolated as an exact span and checked on its own (depth, number length, size), so one deep, huge or malformed
# design value makes only its task Invalid; every other lease stays usable. The writer copies unchanged records
# and healthy design values byte for byte and splices only the fields a mutation really changed.

DOCUMENT_LIMIT = 8 * 1024 * 1024
DESIGN_SPAN_LIMIT = 64 * 1024
DESIGN_DEPTH_LIMIT = 32
DESIGN_DIGITS_LIMIT = 20
INVALID_DIR_NAME = "design-invalid"
DESIGN_RECORD_INVALID = "OAH_DESIGN_RECORD_INVALID"
_WS = " \t\n\r"


class _Unreadable(Exception):
    """The outer document cannot be read; it is a store-wide StateCorrupt, never a per-task problem."""


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def _strict_loads(text: str):
    return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)


def _skip(text: str, i: int) -> int:
    while i < len(text) and text[i] in _WS:
        i += 1
    return i


def _string_end(text: str, i: int) -> int:
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c < " ":
            raise _Unreadable("a control character inside a string")
        else:
            i += 1
    raise _Unreadable("an unterminated string")


def _value(text: str, i: int) -> tuple[int, int, int, int]:
    """(start, end, deepest nesting, longest number) of the JSON value at i, found lexically and iteratively."""
    i = _skip(text, i)
    start, stack, deepest, longest = i, [], 0, 0
    while True:
        if i >= len(text):
            raise _Unreadable("the document ends inside a value")
        c = text[i]
        if c == '"':
            i = _string_end(text, i)
        elif c in "{[":
            stack.append(c)
            deepest = max(deepest, len(stack))
            i += 1
        elif c in "}]":
            if not stack or (stack[-1] == "{") != (c == "}"):
                raise _Unreadable("unbalanced brackets")
            stack.pop()
            i += 1
        elif stack and (c in ",:" or c in _WS):
            i += 1
            continue
        elif c == "-" or c.isdigit():
            j = i + 1
            while j < len(text) and (text[j].isdigit() or text[j] in "+-.eE"):
                j += 1
            longest = max(longest, sum(ch.isdigit() for ch in text[i:j]))
            i = j
        elif text.startswith("true", i) or text.startswith("null", i):
            i += 4
        elif text.startswith("false", i):
            i += 5
        else:
            raise _Unreadable(f"an unexpected character at offset {i}")
        if not stack:
            return start, i, deepest, longest


def _members(text: str, i: int) -> tuple[list[tuple[str, int, int, int, int, int]], int]:
    """The members of the object at i: (key, key start, value start, value end, depth, digits), and its end."""
    i = _skip(text, i)
    if i >= len(text) or text[i] != "{":
        raise _Unreadable("an object was expected")
    i, out = _skip(text, i + 1), []
    if i < len(text) and text[i] == "}":
        return out, i + 1
    while True:
        if i >= len(text) or text[i] != '"':
            raise _Unreadable("an object key was expected")
        key_end = _string_end(text, i)
        try:
            key = json.loads(text[i:key_end])
        except ValueError as exc:
            raise _Unreadable(f"an unreadable key ({exc})") from None
        colon = _skip(text, key_end)
        if colon >= len(text) or text[colon] != ":":
            raise _Unreadable("a ':' was expected")
        start, end, depth, digits = _value(text, colon + 1)
        out.append((key, i, start, end, depth, digits))
        i = _skip(text, end)
        if i < len(text) and text[i] == ",":
            i = _skip(text, i + 1)
            continue
        if i < len(text) and text[i] == "}":
            return out, i + 1
        raise _Unreadable("',' or '}' was expected")


def _elements(text: str, i: int) -> tuple[list[tuple[int, int]], int]:
    i = _skip(text, i)
    if i >= len(text) or text[i] != "[":
        raise _Unreadable("an array was expected")
    i, out = _skip(text, i + 1), []
    if i < len(text) and text[i] == "]":
        return out, i + 1
    while True:
        start, end, _depth, _digits = _value(text, i)
        out.append((start, end))
        i = _skip(text, end)
        if i < len(text) and text[i] == ",":
            i = _skip(text, i + 1)
            continue
        if i < len(text) and text[i] == "]":
            return out, i + 1
        raise _Unreadable("',' or ']' was expected")


@dataclasses.dataclass(frozen=True)
class DesignField:
    """A lease's optional design value: absent, valid (with its exact bytes) or invalid (code and identity only)."""

    kind: str  # absent | valid | invalid
    raw: str | None = None  # the exact own value text (valid, or an already preserved invalid envelope)
    value: dict | None = None  # the validated TaskBinding JSON (valid only)
    code: str | None = None
    spans: tuple[tuple[int, int], ...] = ()  # byte offsets in the original document (invalid only)
    document_sha256: str | None = None
    preserved: bool = False  # the raw value is already a bounded invalid-evidence envelope

    def identity(self) -> str:
        return f"{self.code} document {self.document_sha256} spans {list(self.spans)}"


ABSENT = DesignField("absent")


@dataclasses.dataclass(frozen=True)
class LeaseRecordV1:
    task: str
    core: dict  # every member except design, decoded, in document order (unknown fields kept)
    raw: str  # the exact record object text
    design: DesignField
    lease: "Lease"  # as read; callers get copies, so this stays the comparison baseline
    snapshot: dict = dataclasses.field(default_factory=dict)  # lease.to_json() when read


@dataclasses.dataclass(frozen=True)
class LeaseDocumentV1:
    raw: bytes | None  # None when leases.json does not exist
    records: tuple[LeaseRecordV1, ...]

    @property
    def sha256(self) -> str | None:
        return None if self.raw is None else hashlib.sha256(self.raw).hexdigest()

    def leases(self) -> list["Lease"]:
        """Independent copies: a mutation never changes the captured baseline the writer compares against."""
        return [copy.deepcopy(record.lease) for record in self.records]

    def record(self, task: str) -> LeaseRecordV1 | None:
        return next((record for record in self.records if record.task == task), None)


def _byte_offset(text: str, index: int) -> int:
    return len(text[:index].encode("utf-8"))


def _design_field(text: str, spans: list[tuple[int, int, int, int]], document_sha256: str) -> DesignField:
    """Check one lease's design value(s) without handing an unchecked subtree to the recursive decoder."""
    byte_spans = tuple((_byte_offset(text, start), len(text[start:end].encode("utf-8")))
                       for start, end, _depth, _digits in spans)

    def invalid(code: str) -> DesignField:
        return DesignField("invalid", code=code, spans=byte_spans, document_sha256=document_sha256)

    if len(spans) > 1:
        return invalid("duplicate-design-keys")
    start, end, depth, digits = spans[0]
    raw = text[start:end]
    if len(raw.encode("utf-8")) > DESIGN_SPAN_LIMIT:
        return invalid("too-large")
    if depth > DESIGN_DEPTH_LIMIT:
        return invalid("too-deep")
    if digits > DESIGN_DIGITS_LIMIT:
        return invalid("number-too-long")
    try:
        value = _strict_loads(raw)
    except (ValueError, RecursionError):
        return invalid("malformed")
    if isinstance(value, dict) and value.get("invalid") is True:
        envelope_ok = (set(value) <= {"version", "invalid", "code", "document_sha256", "span", "spans"}
                       and value.get("version") == 1 and isinstance(value.get("code"), str)
                       and isinstance(value.get("document_sha256"), str))
        return DesignField("invalid", raw=raw, code=value.get("code") if envelope_ok else "malformed-envelope",
                           spans=byte_spans, document_sha256=value.get("document_sha256") if envelope_ok else
                           document_sha256, preserved=envelope_ok)
    if value is None:
        return invalid("null")
    from oah.design.binding import TaskBinding

    try:
        TaskBinding.from_json(value)
    except (ValueError, RecursionError):
        return invalid("not-a-task-binding")
    return DesignField("valid", raw=raw, value=value)


def read_lease_document(raw: bytes | None, source: pathlib.Path) -> LeaseDocumentV1:
    """The only leases.json parser: bounded, lexical span isolation first, then strict core validation."""
    if raw is None:
        return LeaseDocumentV1(None, ())

    def corrupt(why: str) -> StateCorrupt:
        return StateCorrupt(f"{StateCorrupt.code}: {source} is not a readable version-{SCHEMA_VERSION} lease file "
                            f"({why}); oah never resets it. Repair it by hand, then run the command again")

    if len(raw) > DOCUMENT_LIMIT:
        raise corrupt(f"{len(raw)} bytes, over the {DOCUMENT_LIMIT}-byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise corrupt("not UTF-8") from None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        outer, end = _members(text, 0)
        if _skip(text, end) != len(text):
            raise _Unreadable("text after the document")
        found = [item for item in outer if item[0] == "leases"]
        if len(found) != 1:
            raise _Unreadable("there must be exactly one leases member")
        elements, _ = _elements(text, found[0][2])
        lease_spans = [(start, end, _members(text, start)[0]) for start, end in elements]
    except _Unreadable as exc:
        raise corrupt(str(exc)) from None
    # Mask every lease's design values (renamed keys, null values) so the core decodes without them.
    edits: list[tuple[int, int, str]] = []
    designs: list[list[tuple[int, int, int, int]]] = []
    for _start, _end, members in lease_spans:
        mine = [(start, end, depth, digits) for key, key_start, start, end, depth, digits in members
                if key == "design"]
        designs.append(mine)
        for number, (key, key_start, start, end, _depth, _digits) in enumerate(
                [item for item in members if item[0] == "design"]):
            edits.append((key_start, end, f'"design#{number}": null'))
    masked, cursor = [], 0
    for start, end, replacement in sorted(edits):
        masked.append(text[cursor:start])
        masked.append(replacement)
        cursor = end
    masked.append(text[cursor:])
    try:
        data = _strict_loads("".join(masked))
    except (ValueError, RecursionError) as exc:
        raise corrupt(f"its core does not decode: {str(exc)[:120]}") from None
    if not (isinstance(data, dict) and data.get("version") == SCHEMA_VERSION and isinstance(data.get("leases"), list)
            and len(data["leases"]) == len(lease_spans)):
        raise corrupt("not a version-1 lease list")
    records = []
    for (start, end, _members_), item, spans in zip(lease_spans, data["leases"], designs):
        if not isinstance(item, dict):
            raise corrupt("a lease entry is not an object")
        core = {key: value for key, value in item.items() if not key.startswith("design#")}
        field = _design_field(text, spans, digest) if spans else ABSENT
        lease = Lease.from_json(core, source, design=field)
        records.append(LeaseRecordV1(lease.task, core, text[start:end], field, lease, lease.to_json()))
    tasks = [record.task for record in records]
    if len(set(tasks)) != len(tasks):
        raise corrupt("two leases name the same task")
    return LeaseDocumentV1(raw, tuple(records))


def _envelope(field: DesignField) -> str:
    spans = [{"offset": offset, "bytes": size} for offset, size in field.spans]
    body = {"version": 1, "invalid": True, "code": field.code, "document_sha256": field.document_sha256}
    if len(spans) == 1:
        body["span"] = spans[0]
    else:
        body["spans"] = spans
    return json.dumps(body, sort_keys=True)


def _member(key: str, value_text: str) -> str:
    return f"{json.dumps(key)}: {value_text}"


def _record_text(lease: "Lease", record: LeaseRecordV1 | None) -> str:
    """A changed or new record: captured members kept, only really changed fields replaced, design spliced."""
    fresh = lease.to_json()
    fresh.pop("design", None)
    if record is not None and record.lease.id == lease.id:
        before = dict(record.snapshot)
        before.pop("design", None)
        members = dict(record.core)
        for name in set(fresh) | set(before):
            if fresh.get(name, _MISSING) != before.get(name, _MISSING):
                if name in fresh:
                    members[name] = fresh[name]
                else:
                    members.pop(name, None)
    else:
        members = fresh
    parts = [_member(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in members.items()]
    field = lease.design_field
    if lease.design is not None:
        unchanged = field is not None and field.kind == "valid" and field.value == lease.design
        parts.append(_member("design", field.raw if unchanged else json.dumps(lease.design, sort_keys=True)))
    elif field is not None and field.kind == "invalid":
        parts.append(_member("design", field.raw if field.preserved else _envelope(field)))
    return "{" + ", ".join(parts) + "}"


_MISSING = object()


def serialize_lease_document(document: LeaseDocumentV1 | None, leases: Sequence["Lease"]) -> str:
    """leases.json text for `leases`: unchanged records copied verbatim, changed ones spliced field by field."""
    captured = {record.task: record for record in (document.records if document is not None else ())}
    texts = []
    for lease in leases:
        record = captured.get(lease.task)
        field = lease.design_field
        unpreserved = field is not None and field.kind == "invalid" and not field.preserved
        if (record is not None and record.lease.id == lease.id and record.snapshot == lease.to_json()
                and record.design == field and not unpreserved):
            texts.append(record.raw)
        else:
            texts.append(_record_text(lease, record))
    body = ",\n    ".join(texts)
    return '{\n  "leases": [' + (f"\n    {body}\n  " if texts else "") + '],\n  "version": 1\n}\n'


def preserve_invalid(state: pathlib.Path, document: LeaseDocumentV1 | None, leases: Sequence["Lease"]) -> list[dict]:
    """Before an unrelated mutation rewrites a document holding Invalid design values, keep the original bytes."""
    from oah.atomic import _fsync_folder

    pending = [lease for lease in leases if lease.design_field is not None and lease.design_field.kind == "invalid"
               and not lease.design_field.preserved]
    if not pending or document is None or document.raw is None:
        return []
    folder = pathlib.Path(state) / INVALID_DIR_NAME
    target = folder / f"{document.sha256}.raw"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            if target.read_bytes() != document.raw:
                raise StateCorrupt(f"{StateCorrupt.code}: {target} exists with other bytes; nothing was changed")
        else:
            try:
                os.write(fd, document.raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_folder(folder)
    except OSError as exc:
        raise StorageError(target, exc) from exc
    return [{"task": lease.task, "code": lease.design_field.code, "document_sha256": document.sha256}
            for lease in pending]


class LeaseJournal:
    """lease.py's own transaction adapter: the version-1 transaction/event schema of oah.journal, but the state
    member is spliced in and published as exact text, so healthy design bytes are never normalized."""

    def __init__(self, state_dir: pathlib.Path) -> None:
        self.dir = pathlib.Path(state_dir)
        self.path = self.dir / "transaction.json"
        self.state_path = self.dir / "leases.json"
        self.events_path = self.dir / "events.jsonl"

    def _read(self) -> tuple[bytes, str, dict] | None:
        """(transaction bytes, exact state text, envelope) or None; malformed is StateCorrupt."""
        raw = read_bytes_bounded(self.path, DOCUMENT_LIMIT + 1024 * 1024)
        if raw is None:
            return None
        bad = StateCorrupt(f"{StateCorrupt.code}: {self.path} is a malformed lease transaction")
        try:
            text = raw.decode("utf-8")
            members, end = _members(text, 0)
            if _skip(text, end) != len(text):
                raise _Unreadable("text after the transaction")
            states = [item for item in members if item[0] == "state"]
            if len(states) != 1:
                raise _Unreadable("one state member is required")
            _key, key_start, start, value_end, _depth, _digits = states[0]
            envelope = _strict_loads(text[:key_start] + '"state": null' + text[value_end:])
        except (UnicodeDecodeError, _Unreadable, ValueError, RecursionError):
            raise bad from None
        if not (isinstance(envelope, dict) and envelope.get("version") == 1
                and isinstance(envelope.get("transaction_id"), str) and envelope["transaction_id"]
                and isinstance(envelope.get("event"), dict)
                and envelope["event"].get("transaction_id") == envelope["transaction_id"]):
            raise bad
        return raw, text[start:value_end], envelope

    def check_pending(self) -> None:
        from oah.journal import PendingTransaction

        if self._read() is not None:
            raise PendingTransaction(f"{PendingTransaction.code}: {self.path} needs recovery. "
                                     "The next mutating oah command replays it under the state lock.")

    def recover(self) -> None:
        """The caller holds the lock. Validate the live and the pending state with the same reader, then finish."""
        from oah.atomic import read_jsonl, write_text_atomic

        found = self._read()
        if found is None:
            read_jsonl(self.events_path)
            return
        raw, state_text, envelope = found
        read_lease_document(read_bytes_bounded(self.state_path, DOCUMENT_LIMIT), self.state_path)
        read_lease_document(state_text.encode("utf-8"), self.path)
        write_text_atomic(self.path, raw.decode("utf-8"))  # re-sync the original bytes before publishing
        self._publish(state_text, envelope)

    def commit(self, state_text: str, event: Mapping) -> None:
        from oah.atomic import read_jsonl, write_text_atomic

        read_jsonl(self.events_path)
        transaction_id = secrets.token_hex(16)
        record = dict(event, transaction_id=transaction_id)
        text = ('{"event": ' + json.dumps(record, ensure_ascii=False, sort_keys=True) + ', "state": '
                + state_text.rstrip("\n") + ', "transaction_id": ' + json.dumps(transaction_id) + ', "version": 1}\n')
        write_text_atomic(self.path, text)
        self._publish(state_text.rstrip("\n"), {"version": 1, "transaction_id": transaction_id, "event": record})

    def _publish(self, state_text: str, envelope: Mapping) -> None:
        from oah.atomic import append_jsonl, read_jsonl, unlink_durable, write_text_atomic

        event = envelope["event"]
        matches = [item for item in read_jsonl(self.events_path)
                   if item.get("transaction_id") == envelope["transaction_id"]]
        if matches and (len(matches) != 1 or matches[0] != event):
            raise StateCorrupt(f"{StateCorrupt.code}: {self.events_path} conflicts with {self.path}")
        write_text_atomic(self.state_path, state_text.rstrip("\n") + "\n")
        append_jsonl(self.events_path, event)
        unlink_durable(self.path)


def _check_names(task: str, agent: str) -> None:
    if not TASK_RE.fullmatch(task or ""):
        raise LeaseError("OAH_TASK_INVALID", f"{task!r}: use letters, digits, '.', '_' or '-' (max 64)")
    if not AGENT_RE.fullmatch(agent or ""):
        raise LeaseError("OAH_AGENT_INVALID", f"{agent!r}: use letters, digits, '.', '_' or '-' (max 32)")


def _check_ttl(ttl_hours: float) -> None:
    if not (0 < ttl_hours <= MAX_TTL_HOURS):
        raise LeaseError("OAH_TTL_INVALID", f"{ttl_hours!r}: the TTL must be above 0 and at most "
                                            f"{MAX_TTL_HOURS:g} hours")


def _forward(old: Binding, new: Binding) -> str:
    """`rebound` for a raised Revision of the same Intent-ID, `unchanged` for the same binding; else refuse."""
    if new.id != old.id:
        raise LeaseError("OAH_INTENT_ID_CHANGED",
                         f"this lease is bound to Intent-ID {old.id}, and the packet says {new.id}. Keep the ID: a "
                         f"changed goal raises Revision of {old.id}")
    if new.revision < old.revision:
        raise LeaseError("OAH_INTENT_REVISION_INVALID",
                         f"the packet is revision {new.revision}, lower than the bound revision {old.revision}; "
                         "revisions only go up")
    if new.revision == old.revision:
        if new == old:
            return "unchanged"
        raise LeaseError("OAH_INTENT_REVISION_REQUIRED",
                         f"the intent changed but is still revision {old.revision}. Set Revision: "
                         f"{old.revision + 1}, then Status: accepted again once you approve it")
    return "rebound"


# Called under the state lock with the locked lease (None for a new lease): (record JSON or None, action, event fields)
DesignUpdate = Callable[[Lease | None], tuple[dict | None, str | None, dict]]
DESIGN_SCHEMA = 2


def _design_snapshot(lease: Lease) -> dict:
    """The typed design payload every event of a design-bound lease carries (none for an origin-free lease)."""
    if lease.design_invalid:
        return {"design_schema": DESIGN_SCHEMA, "design_problem": lease.design_field.code,
                "design_problem_document": lease.design_field.document_sha256}
    if lease.design is not None:
        return {"design_schema": DESIGN_SCHEMA, "design": lease.design}
    return {}


class Store:
    def __init__(self, state_dir: pathlib.Path, *, now: Callable[[], dt.datetime] = utc_now,
                 lock_timeout: float = 10.0) -> None:
        self.dir = pathlib.Path(state_dir)
        self.now = now
        self.lock_timeout = lock_timeout
        self.leases_path = self.dir / "leases.json"
        self.events_path = self.dir / "events.jsonl"
        self.lock_path = self.dir / ".lock"
        self.journal = LeaseJournal(self.dir)
        self._document: LeaseDocumentV1 | None = None  # captured by the last _load, for the lossless writer

    # Reading -------------------------------------------------------------------------------------

    def capture_document(self) -> LeaseDocumentV1:
        """One bounded exact read of leases.json through the only lease parser; a missing file has raw None."""
        return read_lease_document(read_bytes_bounded(self.leases_path, DOCUMENT_LIMIT), self.leases_path)

    def _load(self) -> list[Lease]:
        self._document = self.capture_document()
        return self._document.leases()

    def _decode(self, data: object) -> list[Lease]:
        """Leases from an already decoded document (compatibility); the same reader validates it."""
        try:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise StateCorrupt(f"{StateCorrupt.code}: {self.leases_path} is not a version-{SCHEMA_VERSION} "
                               "lease file") from None
        return read_lease_document(raw, self.leases_path).leases()

    def snapshot(self) -> list[tuple[Lease, bool]]:
        """Every lease with an expired flag. Takes no lock and creates no file."""
        self.journal.check_pending()
        now = self.now()
        entries = [(lease, lease.expired(now)) for lease in self._load()]
        self.journal.check_pending()
        return entries

    # Writing (always under the lock) -------------------------------------------------------------

    def _commit(self, leases: list[Lease], event: str, task: str, agent: str, lease_id: str | None,
                **extra: object) -> None:
        """Publish `leases` losslessly: unchanged records and healthy design values keep their exact bytes."""
        _refuse_worker_lease()
        preserved = preserve_invalid(self.dir, self._document, leases)
        if preserved:
            extra = dict(extra, preserved_design_problems=preserved)
        text = serialize_lease_document(self._document, leases)
        record = {"ts": _iso(self.now()), "event": event, "task": task, "agent": agent, "lease_id": lease_id, **extra}
        self.journal.commit(text, record)
        self._document = read_lease_document(text.encode("utf-8"), self.leases_path)
        for lease in leases:  # later commits in the same locked call splice against the new bytes
            published = self._document.record(lease.task)
            if published is not None and published.lease.id == lease.id:
                lease.design_field = published.design

    @contextmanager
    def _lock(self) -> Iterator[None]:
        try:
            with StateLock(self.lock_path, timeout=self.lock_timeout):
                if os.environ.get("OAH_WORKER") == "1":
                    self.journal.check_pending()  # recovery writes lease state; leave it to the dispatcher
                else:
                    self.journal.recover()
                yield
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(self.lock_path, exc) from exc

    def inspect(self, task: str) -> Lease:
        """Inspect under the lock; only the dispatcher may recover pending writes."""
        with self._lock():
            return self._find(self._load(), task)

    def _checked(self, leases: list[Lease], task: str, lease_id: str, generation: int) -> Lease:
        lease = next((entry for entry in leases if entry.task == task), None)
        if lease is None or lease.id != lease_id or lease.generation != generation:
            raise LeaseError("OAH_LEASE_CHANGED", f"the lease for {task} changed during the command's checks; "
                             "inspect it with oah status and run the command again")
        return lease

    @contextmanager
    def checked(self, *, task: str, lease_id: str, generation: int) -> Iterator[Lease]:
        """Keep a checked lease current until a short commit or process launch completes."""
        with self._lock():
            yield self._checked(self._load(), task, lease_id, generation)

    def _find(self, leases: list[Lease], task: str) -> Lease:
        for lease in leases:
            if lease.task == task:
                return lease
        raise LeaseError("OAH_LEASE_NOT_FOUND", f"no lease for task {task}")

    def _prune(self, leases: list[Lease]) -> list[Lease]:
        now = self.now()
        for lease in list(leases):
            if lease.expired(now):
                leases.remove(lease)
                self._commit(leases, "lease_expired", lease.task, lease.agent, lease.id, scope=lease.scope,
                             expires_at=lease.expires_at, **_design_snapshot(lease))
        return leases

    def start(self, *, task: str, agent: str, scope: Sequence[str], note: str, ttl_hours: float,
              info: RepoInfo, gates: Sequence[str] = (), intent: Binding | None = None,
              design_update: DesignUpdate | None = None) -> Lease:
        """`design_update(None)` runs under the lock after the lease checks; it returns the design record to
        bind (or None), its action and extra event fields, or raises to refuse with nothing written."""
        _refuse_worker_lease()
        _check_names(task, agent)
        _check_ttl(ttl_hours)
        if not scope:
            raise LeaseError("OAH_SCOPE_EMPTY", "a lease needs at least one scope entry")
        with self._lock():
            leases = self._prune(self._load())
            for other in leases:
                if other.task == task:
                    if other.agent == agent:
                        raise LeaseError("OAH_LEASE_EXISTS", f"task {task} already has your lease; use oah renew")
                    raise LeaseError("OAH_LEASE_CONFLICT", f"{other.describe()}")
                if overlaps(other.scope, scope):
                    raise LeaseError("OAH_LEASE_CONFLICT",
                                     f"scope {', '.join(scope)} overlaps {other.describe()}")
            design, design_fields = None, {}
            if design_update is not None:
                design, action, extra = design_update(None)
                if design is not None:
                    design_fields = {"design_schema": DESIGN_SCHEMA, "design": design, "design_action": action,
                                     **extra}
            now = self.now()
            lease = Lease(id=secrets.token_hex(8), task=task, agent=agent, scope=list(scope), note=note,
                          worktree=str(info.root), branch=info.branch, head_at_start=info.head,
                          created_at=_iso(now), expires_at=_expiration(now, ttl_hours), gates=list(gates),
                          intent=intent.to_json() if intent is not None else None, design=design, generation=1)
            leases.append(lease)
            bound = {"intent": lease.intent} if lease.intent is not None else {}
            self._commit(leases, "lease_started", task, agent, lease.id, scope=lease.scope, note=note,
                         expires_at=lease.expires_at, worktree=lease.worktree, branch=lease.branch,
                         gates=lease.gates, generation=lease.generation, **bound, **design_fields)
            return lease

    def renew(self, *, task: str, agent: str, ttl_hours: float, scope: Sequence[str] | None = None,
              gates: Sequence[str] | None = None) -> Lease:
        _refuse_worker_lease()
        _check_names(task, agent)
        _check_ttl(ttl_hours)
        with self._lock():
            leases = self._load()
            lease = self._find(leases, task)
            if lease.agent != agent:
                raise LeaseError("OAH_LEASE_NOT_YOURS", f"{lease.describe()}")
            if lease.expired(self.now()):
                raise LeaseError("OAH_LEASE_EXPIRED", f"the lease for {task} expired at {lease.expires_at}; "
                                                      "start a new one")
            old_scope = list(lease.scope)
            if scope:
                for other in leases:
                    if other is not lease and not other.expired(self.now()) and overlaps(other.scope, scope):
                        raise LeaseError("OAH_LEASE_CONFLICT",
                                         f"scope {', '.join(scope)} overlaps {other.describe()}")
                lease.scope = list(scope)
            if gates:
                lease.gates = list(gates)
            lease.expires_at = _expiration(self.now(), ttl_hours)
            lease.generation += 1
            self._commit(leases, "lease_renewed", task, agent, lease.id, scope=lease.scope, old_scope=old_scope,
                         expires_at=lease.expires_at, gates=lease.gates, generation=lease.generation,
                         **_design_snapshot(lease))
            return lease

    def rebind(self, *, task: str, agent: str, binding: Binding, ttl_hours: float,
               scope: Sequence[str] | None = None, gates: Sequence[str] | None = None,
               design_update: DesignUpdate | None = None) -> tuple[Lease, str]:
        """Bind your lease to `binding`, or move it to a raised Revision of the same Intent-ID.

        Returns the lease and `bound`, `rebound` or `unchanged`. The TTL is renewed; `scope`, when given,
        replaces the lease scope after the same overlap check as renew. Supplied `gates` replace the
        ledger paths in the same transaction; omitting them preserves the existing paths.
        `design_update(locked_lease)` runs under the lock, as for `start`.
        """
        _refuse_worker_lease()
        _check_names(task, agent)
        _check_ttl(ttl_hours)
        with self._lock():
            leases = self._load()
            lease = self._find(leases, task)
            if lease.agent != agent:
                raise LeaseError("OAH_LEASE_NOT_YOURS", f"{lease.describe()}")
            if lease.expired(self.now()):
                raise LeaseError("OAH_LEASE_EXPIRED", f"the lease for {task} expired at {lease.expires_at}; "
                                                      "start a new one")
            old = lease.binding()
            action = "bound" if old is None else _forward(old, binding)
            if scope:
                for other in leases:
                    if other is not lease and not other.expired(self.now()) and overlaps(other.scope, scope):
                        raise LeaseError("OAH_LEASE_CONFLICT",
                                         f"scope {', '.join(scope)} overlaps {other.describe()}")
            design_fields = {}
            if design_update is not None:
                old_design = lease.design
                design, design_action, extra = design_update(lease)
                if design is not None:
                    design_fields = {"design_action": design_action, **extra}
                    if old_design is not None and old_design != design:
                        design_fields["old_design"] = old_design
                    lease.design = design
            if scope:
                lease.scope = list(scope)
            lease.intent = binding.to_json()
            if gates is not None:
                lease.gates = list(gates)
            lease.expires_at = _expiration(self.now(), ttl_hours)
            lease.generation += 1
            event = {"bound": "intent_bound", "rebound": "intent_rebound", "unchanged": "lease_renewed"}[action]
            self._commit(leases, event, task, agent, lease.id, scope=lease.scope, expires_at=lease.expires_at,
                         gates=lease.gates, intent=lease.intent, generation=lease.generation,
                         **({"old_intent": old.to_json()} if old and action == "rebound" else {}), **design_fields,
                         **_design_snapshot(lease))
            return lease, action

    def finish(self, *, task: str, agent: str, lease_id: str, summary: str, changed: Sequence[str],
               allow_out_of_scope: bool, gate_report: dict | None = None,
               wiki_report: dict | None = None, intent_report: dict | None = None,
               checked_binding: Binding | None = None,
               checked_generation: int | None = None,
               design_check: Callable[[Lease], dict | None] | None = None) -> tuple[Lease, list[str]]:
        """`design_check(locked_lease)` judges the design again under the lock, after the generation check and
        before the commit; it returns event fields (or None) or raises to keep the lease open."""
        _refuse_worker_lease()
        _check_names(task, agent)
        # Direct callers without a prior scan start their decision here. A scanned bound return must
        # carry the generation captured by inspect(), alongside its checked binding.
        if checked_generation is None:
            if checked_binding is not None:
                raise LeaseError("OAH_LEASE_CHANGED", "the return has no checked lease generation; "
                                 "run oah finish again to check the current lease and return together")
            checked_generation = self.inspect(task).generation
        with self._lock():
            leases = self._load()
            lease = self._checked(leases, task, lease_id, checked_generation)
            if lease.agent != agent:
                raise LeaseError("OAH_LEASE_NOT_YOURS", f"{lease.describe()}")
            binding = lease.binding()
            if binding is not None and checked_binding is None:
                raise LeaseError("OAH_LEASE_CHANGED", "the lease's intent was not checked with this return; "
                                 "run oah finish --return FILE again against the current lease")
            if binding is not None:
                state, reason = explain_binding(binding, pathlib.Path(lease.worktree))
                if state != "current":
                    raise LeaseError("OAH_INTENT_NOT_CURRENT", f"the checked intent is {state}: {reason}. Restore "
                                     "the bound source or accept and bind its new revision with oah start --packet, "
                                     "then update the return and retry oah finish")
            design_fields = (design_check(lease) or {}) if design_check is not None else {}
            if lease.design is not None or lease.design_invalid:
                # `gates` already names this event's gate report, so the declared ledgers are `declared_gates`.
                design_fields = {**_design_snapshot(lease), "bound_intent": lease.intent, "scope": lease.scope,
                                 "declared_gates": lease.gates, "worktree": lease.worktree,
                                 "generation": lease.generation, **design_fields}
            beyond = outside(lease.scope, changed)
            if beyond and not allow_out_of_scope:
                shown = ", ".join(beyond[:20]) + (f" and {len(beyond) - 20} more" if len(beyond) > 20 else "")
                raise LeaseError("OAH_SCOPE_EXCEEDED",
                                 f"changes outside scope {', '.join(lease.scope)}: {shown}. Widen the scope with "
                                 "oah renew --scope, or pass --allow-out-of-scope (recorded)")
            leases.remove(lease)
            gate_fields = {"gates": gate_report} if gate_report is not None else {}
            if wiki_report is not None:
                gate_fields["wiki_maintenance"] = wiki_report
            if intent_report is not None:
                gate_fields["intent"] = intent_report
            self._commit(leases, "lease_finished", task, agent, lease.id, summary=summary, out_of_scope=beyond,
                         allowed_out_of_scope=bool(beyond), **gate_fields, **design_fields)
            return lease, beyond

    def carry_design(self, *, task: str, agent: str, update: DesignUpdate) -> Lease | None:
        """Record a design carry under the lock. `update(locked_lease or None)` returns the new record, the action
        and the event fields (its exact `target`, the old record, the viewed pair and the reason), or raises. An open
        lease changes only for its holder and gets a new generation; a closed task gets the event only."""
        _refuse_worker_lease()
        _check_names(task, agent)
        with self._lock():
            leases = self._load()
            lease = next((entry for entry in leases if entry.task == task), None)
            if lease is not None and lease.agent != agent:
                raise LeaseError("OAH_LEASE_NOT_YOURS", f"{lease.describe()}; the lease holder records design carries")
            design, action, extra = update(lease)
            if lease is not None:
                lease.design = design
                lease.generation += 1
            self._commit(leases, "design_carried", task, agent, lease.id if lease is not None else None,
                         design_schema=DESIGN_SCHEMA, design=design, design_action=action, **extra)
            return lease

    def mark_design_origin(self, *, task: str, task_identity: dict, origin: dict) -> None:
        """Append that a task's packet is (being) linked to a design; leases do not change and nothing is bound."""
        _refuse_worker_lease()
        _check_names(task, "oah")
        with self._lock():
            self._commit(self._load(), "design_origin", task, "oah", None, design_schema=DESIGN_SCHEMA,
                         task_identity=task_identity, origin=origin)

    def release(self, *, task: str, agent: str, reason: str, force: bool) -> Lease:
        _refuse_worker_lease()
        _check_names(task, agent)
        if not reason or not reason.strip():
            raise LeaseError("OAH_REASON_REQUIRED", "say why the lease is released (--reason)")
        with self._lock():
            leases = self._load()
            lease = self._find(leases, task)
            forced = lease.agent != agent
            if forced and not force:
                raise LeaseError("OAH_LEASE_NOT_YOURS", f"{lease.describe()}; pass --force to release it anyway")
            leases.remove(lease)
            self._commit(leases, "lease_released", task, agent, lease.id, reason=reason.strip(), forced=forced,
                         holder=lease.agent, **_design_snapshot(lease))
            return lease

    def log(self, *, task: str, agent: str, message: str) -> None:
        _refuse_worker_lease()
        _check_names(task, agent)
        with self._lock():
            self._commit(self._load(), "note", task, agent, None, message=message)
