# 리뷰 기록 저장소 — reviews/<task>/ 의 영수증과 수정 시도 기록을 배타적으로 한 번만 쓰고 index.jsonl 에 digest 로 묶으며, 읽을 때 모두 다시 대조해 문제를 고치지 않고 알린다.
"""Review records under `<state>/reviews/<task>/` (PLAN 5.9 step 8).

Records
    `R<n>.json` are receipts, numbered per task in publication order. `A<k>-start.json`,
    `A<k>-outcome.json` and `A<k>-head.json` are the records of one correction attempt (lane P6b writes
    them; this module defines and reads them). Every record is a JSON object with `kind` and `task`; a
    receipt also has `round` = n and an attempt record `attempt_id` = `A<k>`.
Publication
    `publish` writes the canonical bytes with exclusive create and fsyncs them, then appends the index line
    `{kind, name, digest, recorded_at, ...summary}` to `index.jsonl` under the state lock. The index line is
    the commit point. Nothing is ever rewritten or repaired.
Reading
    `load` re-reads every indexed file and compares its digest. Unreadable or torn indexes, missing files,
    digest or identity mismatches, receipts out of order and record files missing from the index are
    returned as `problems`; callers stop with `OAH_REVIEW_NEEDS_DECISION`.
Correction attempts
    `attempts` merges each attempt's records into the 5.9 view `{attempt_id, source_receipt_digest,
    finding_ids, class_ids, before_head, after_head, outcome, evidence}`. The start record names the source
    receipt (name and digest), its finding ids and exactly their class ids, and `before_head`. The outcome is
    one of `OUTCOMES`, with `after_head` (a full SHA, or null when nothing was committed). A head-binding
    record may follow only an `applied` outcome with a null `after_head`. An attempt without an outcome is
    unfinished, and an applied attempt without a bound commit is incomplete: both are problems.
Round log
    `rounds.jsonl` records the start and finish of every dispatched round attempt. It is not history; the
    attempt budget and sticky decision stops are read from it.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
from typing import Iterable, Mapping

from oah.atomic import StateCorrupt, StorageError, _fsync_folder, append_jsonl, read_jsonl
from oah.lease import TASK_RE
from oah.lockfile import StateLock
from oah.runs import RunError, _contained_path

SCHEMA = 1
INDEX_NAME = "index.jsonl"
ROUNDS_NAME = "rounds.jsonl"
KINDS = ("receipt", "attempt-start", "attempt-outcome", "attempt-head")
OUTCOMES = ("applied", "no-change", "failed", "aborted")
SUMMARY_FIELDS = ("round", "series_round", "base", "head", "verdict", "outcome")
SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RECEIPT_NAME = re.compile(r"R([1-9][0-9]{0,8})\.json")
_ATTEMPT_NAME = re.compile(r"(A[1-9][0-9]{0,8})-(start|outcome|head)\.json")


class ReviewError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _iso(now: dt.datetime | None = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(record: Mapping) -> bytes:
    """The exact bytes a record is published as: sorted, indented JSON and one LF."""
    return (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def history_digest(items: Iterable[Mapping]) -> str:
    """Digest of the `{kind, name, digest}` list a round was given; `[]` is the explicitly empty history."""
    text = json.dumps(list(items), sort_keys=True, separators=(",", ":"))
    return digest(text.encode("utf-8"))


def review_dir(state: pathlib.Path, task: str) -> pathlib.Path:
    """`<state>/reviews/<task>`, refusing invalid task names and linked folders."""
    if not TASK_RE.fullmatch(task or ""):
        raise ReviewError("OAH_TASK_INVALID", f"task {task!r} must match {TASK_RE.pattern}; choose a valid task name")
    try:
        return _contained_path(state, "reviews", task)
    except RunError as exc:
        raise ReviewError(exc.code, str(exc).split(": ", 1)[1]) from None


def _path(state: pathlib.Path, task: str, name: str) -> pathlib.Path:
    review_dir(state, task)
    try:
        return _contained_path(state, "reviews", task, name)
    except RunError as exc:
        raise ReviewError(exc.code, str(exc).split(": ", 1)[1]) from None


def parse_name(name: str) -> tuple[str, int | str]:
    """(kind, round number or attempt id) for a record file name; ValueError for anything else."""
    match = _RECEIPT_NAME.fullmatch(name or "")
    if match:
        return "receipt", int(match.group(1))
    match = _ATTEMPT_NAME.fullmatch(name or "")
    if match:
        return f"attempt-{match.group(2)}", match.group(1)
    raise ValueError(f"{name!r} is not a review record name (R<n>.json or A<k>-start|outcome|head.json)")


def _identity_problem(name: str, kind: str, ident: int | str, record: object, task: str) -> str | None:
    if not isinstance(record, dict):
        return f"{name} is not a JSON object"
    if record.get("kind") != kind:
        return f"{name} has kind {record.get('kind')!r}, not {kind!r}"
    if record.get("task") != task:
        return f"{name} belongs to task {record.get('task')!r}, not {task!r}"
    if kind == "receipt" and record.get("round") != ident:
        return f"{name} holds round {record.get('round')!r}, not {ident}"
    if kind != "receipt" and record.get("attempt_id") != ident:
        return f"{name} holds attempt {record.get('attempt_id')!r}, not {ident}"
    return None


def publish(state: pathlib.Path, task: str, name: str, record: Mapping, *, summary: Mapping | None = None,
            now: dt.datetime | None = None) -> str:
    """Write one immutable record and index it; returns its digest."""
    try:
        kind, ident = parse_name(name)
    except ValueError as exc:
        raise ReviewError("OAH_REVIEW_RECORD_INVALID", str(exc)) from None
    folder = review_dir(state, task)
    problem = _identity_problem(name, kind, ident, dict(record) if isinstance(record, Mapping) else record, task)
    extra = dict(summary or {})
    unknown = sorted(set(extra) - set(SUMMARY_FIELDS))
    if problem is None and unknown:
        problem = f"unknown index fields {', '.join(unknown)}"
    if problem is not None:
        raise ReviewError("OAH_REVIEW_RECORD_INVALID", f"{problem}; nothing was written")
    data = canonical(record)
    value = digest(data)
    line = {"kind": kind, "name": name, "digest": value, "recorded_at": _iso(now), **extra}
    try:
        with StateLock(_contained_path(state, ".lock")):
            folder.mkdir(parents=True, exist_ok=True)
            index = _path(state, task, INDEX_NAME)
            if any(item.get("name") == name for item in read_jsonl(index)):
                raise ReviewError("OAH_REVIEW_RECORD_EXISTS", f"{name} is already indexed for task {task}; records "
                                  "are never rewritten")
            path = _path(state, task, name)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
            except FileExistsError:
                raise ReviewError("OAH_REVIEW_RECORD_EXISTS", f"{path} already exists but is not indexed, so a "
                                  "write was interrupted; inspect it and move it out of the folder, then run the "
                                  "command again") from None
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_folder(folder)
            append_jsonl(index, line)
    except RunError as exc:
        raise ReviewError(exc.code, str(exc).split(": ", 1)[1]) from None
    except OSError as exc:
        if isinstance(exc, StorageError):
            raise
        raise StorageError(folder / name, exc) from exc
    return value


@dataclasses.dataclass(frozen=True)
class Entry:
    kind: str
    name: str
    digest: str
    record: dict
    line: dict


@dataclasses.dataclass(frozen=True)
class Records:
    task: str
    entries: tuple[Entry, ...] = ()
    problems: tuple[str, ...] = ()

    def receipts(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.kind == "receipt"]


def load(state: pathlib.Path, task: str) -> Records:
    """Every indexed record, re-read and digest-checked, with the problems found (never repaired)."""
    folder = review_dir(state, task)
    if not folder.is_dir():
        return Records(task)
    problems: list[str] = []
    try:
        lines = read_jsonl(_path(state, task, INDEX_NAME))
    except StateCorrupt as exc:
        problems.append(f"{INDEX_NAME} cannot be read ({str(exc).split(': ', 1)[-1]})")
        lines = []
    entries: list[Entry] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, start=1):
        kind, name, value = line.get("kind"), line.get("name"), line.get("digest")
        if kind not in KINDS or not isinstance(name, str) or not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
            problems.append(f"{INDEX_NAME} line {number} is not a review record line")
            continue
        try:
            parsed, ident = parse_name(name)
        except ValueError as exc:
            problems.append(f"{INDEX_NAME} line {number}: {exc}")
            continue
        if parsed != kind:
            problems.append(f"{INDEX_NAME} line {number} calls {name} a {kind} record")
            continue
        if name in seen:
            problems.append(f"{INDEX_NAME} lists {name} more than once")
            continue
        seen.add(name)
        path = _path(state, task, name)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            problems.append(f"{name} is listed in {INDEX_NAME} but the file is missing")
            continue
        except OSError as exc:
            raise StorageError(path, exc) from exc
        if digest(data) != value:
            problems.append(f"{name} no longer matches its indexed digest {value}; it was changed after publication")
            continue
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"{name} is not UTF-8 JSON")
            continue
        problem = _identity_problem(name, kind, ident, record, task)
        if problem is not None:
            problems.append(problem)
            continue
        entries.append(Entry(kind, name, value, record, dict(line)))
    rounds = [entry.record["round"] for entry in entries if entry.kind == "receipt"]
    if rounds != list(range(1, len(rounds) + 1)):
        problems.append(f"the indexed receipt rounds are {rounds}, not R1..R{len(rounds)} in order")
    for child in sorted(folder.iterdir(), key=lambda item: item.name):
        if (_RECEIPT_NAME.fullmatch(child.name) or _ATTEMPT_NAME.fullmatch(child.name)) and child.name not in seen:
            problems.append(f"{child.name} is not listed in {INDEX_NAME}: a write was interrupted or the file was "
                            "added by hand; inspect it and move it out of the folder")
    return Records(task, tuple(entries), tuple(problems))


@dataclasses.dataclass(frozen=True)
class Attempt:
    attempt_id: str
    source_receipt: str | None = None
    source_receipt_digest: str | None = None
    finding_ids: tuple[str, ...] = ()
    class_ids: tuple[str, ...] = ()
    before_head: str | None = None
    after_head: str | None = None
    outcome: str | None = None
    evidence: str | None = None
    manual: bool = False
    names: tuple[str, ...] = ()
    digests: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.outcome in OUTCOMES

    def to_json(self) -> dict:
        return {"attempt_id": self.attempt_id, "source_receipt_digest": self.source_receipt_digest,
                "finding_ids": list(self.finding_ids), "class_ids": list(self.class_ids),
                "before_head": self.before_head, "after_head": self.after_head, "outcome": self.outcome,
                "evidence": self.evidence}


def _strings(value: object) -> tuple[str, ...] | None:
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return tuple(value)
    return None


def _merge(attempt_id: str, items: list[Entry], receipts: Mapping[str, Entry]) -> Attempt:
    problems: list[str] = []
    by_kind = {entry.kind: entry for entry in items}
    start, end, head = (by_kind.get(kind) for kind in KINDS[1:])
    if start is None:
        problems.append(f"{attempt_id} has no start record ({attempt_id}-start.json)")
    elif items[0] is not start:
        problems.append(f"{attempt_id}: {items[0].name} was recorded before its start record")
    if end is not None and head is not None and items.index(head) < items.index(end):
        problems.append(f"{attempt_id}: its head binding was recorded before its outcome")
    fields: dict = {}
    if start is not None:
        record = start.record
        source = receipts.get(record.get("source_receipt_digest"))
        findings, classes = _strings(record.get("finding_ids")), _strings(record.get("class_ids"))
        if source is None or source.name != record.get("source_receipt"):
            problems.append(f"{attempt_id}: its source receipt {record.get('source_receipt')!r} with digest "
                            f"{record.get('source_receipt_digest')!r} matches no indexed receipt")
        elif findings is None or classes is None:
            problems.append(f"{attempt_id}: finding_ids and class_ids must be non-empty lists of ids")
        else:
            known = {f.get("finding_id"): f.get("class_id") for f in source.record.get("findings", [])
                     if isinstance(f, dict)}
            foreign = [ident for ident in findings if ident not in known]
            if foreign:
                problems.append(f"{attempt_id}: findings {', '.join(foreign)} are not in {source.name}")
            elif set(classes) != {known[ident] for ident in findings}:
                problems.append(f"{attempt_id}: class_ids {', '.join(classes)} are not exactly the classes of its "
                                f"findings in {source.name}")
        if not (isinstance(record.get("before_head"), str) and SHA_RE.fullmatch(record["before_head"])):
            problems.append(f"{attempt_id}: before_head must be a full commit SHA")
        fields.update(source_receipt=record.get("source_receipt"),
                      source_receipt_digest=record.get("source_receipt_digest"), finding_ids=findings or (),
                      class_ids=classes or (), before_head=record.get("before_head"),
                      manual=record.get("manual") is True)
    after = None
    evidence: list[str] = []
    if end is None:
        problems.append(f"{attempt_id} is unfinished: it has a start record but no outcome, so recurrence "
                        "evidence is incomplete")
    else:
        outcome, after = end.record.get("outcome"), end.record.get("after_head")
        if outcome not in OUTCOMES:
            problems.append(f"{attempt_id}: outcome {outcome!r} is not one of {', '.join(OUTCOMES)}")
        if after is not None and not (isinstance(after, str) and SHA_RE.fullmatch(after)):
            problems.append(f"{attempt_id}: after_head must be a full commit SHA or null")
        if isinstance(end.record.get("evidence"), str) and end.record["evidence"]:
            evidence.append(end.record["evidence"])
        fields["outcome"] = outcome if outcome in OUTCOMES else None
    if head is not None:
        if end is None or end.record.get("outcome") != "applied" or end.record.get("after_head") is not None:
            problems.append(f"{attempt_id}: a head binding may only follow an applied outcome without after_head")
        bound = head.record.get("after_head")
        if not (isinstance(bound, str) and SHA_RE.fullmatch(bound)):
            problems.append(f"{attempt_id}: the head binding's after_head must be a full commit SHA")
        elif after is None:
            after = bound
        if isinstance(head.record.get("evidence"), str) and head.record["evidence"]:
            evidence.append(head.record["evidence"])
    if fields.get("outcome") == "applied" and after is None:
        problems.append(f"{attempt_id} applied changes but no commit is bound to it; commit the result and record "
                        "its head binding, then review again")
    return Attempt(attempt_id=attempt_id, after_head=after, evidence="; ".join(evidence) or None,
                   names=tuple(entry.name for entry in items), digests=tuple(entry.digest for entry in items),
                   problems=tuple(problems), **fields)


def attempts(records: Records) -> list[Attempt]:
    """Every correction attempt of the task, in the order its first record was indexed."""
    receipts = {entry.digest: entry for entry in records.receipts()}
    groups: dict[str, list[Entry]] = {}
    for entry in records.entries:
        if entry.kind != "receipt":
            groups.setdefault(entry.record["attempt_id"], []).append(entry)
    return [_merge(attempt_id, items, receipts) for attempt_id, items in groups.items()]


def log_round(state: pathlib.Path, task: str, entry: Mapping, *, now: dt.datetime | None = None) -> None:
    """Append one start or finish line to the round-attempt log under the state lock."""
    folder = review_dir(state, task)
    line = {"at": _iso(now), **entry}
    try:
        with StateLock(_contained_path(state, ".lock")):
            folder.mkdir(parents=True, exist_ok=True)
            append_jsonl(_path(state, task, ROUNDS_NAME), line)
    except RunError as exc:
        raise ReviewError(exc.code, str(exc).split(": ", 1)[1]) from None


def read_round_log(state: pathlib.Path, task: str) -> list[dict]:
    """The round-attempt log; a torn or invalid log raises StateCorrupt (never repaired)."""
    return read_jsonl(_path(state, task, ROUNDS_NAME))
