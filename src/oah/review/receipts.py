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
Record format (`schema: 1`; every record also has `kind`, `task` and, for attempts, `attempt_id` = `A<k>`)
    - receipt (`R<n>.json`, `round` = n): full SHAs `base` and `head`, `series_round`, `verdict`, `outcome`
      (`RECEIPT_OUTCOMES`), `findings[]` (each with `finding_id`, `class_id` and a P0-P3 `severity`) and
      `classes[]` (each with `class_id`, `mechanism` and `rule`); `oah.review.runner` writes the other fields.
    - attempt start: exactly `source_receipt` (a receipt name), `source_receipt_digest`, `finding_ids[]` and
      `class_ids[]` (non-empty, each id once; the class ids are exactly the classes of those findings in the
      source receipt), `before_head`, `engine` (`claude`, `codex` or `human`), `manual` (true or false) and
      `started_at`.
    - attempt outcome: exactly `outcome` (`OUTCOMES`), `after_head` (present; a full SHA only for `applied`,
      otherwise null), `evidence` (non-empty) and `finished_at`.
    - attempt head binding, only after an `applied` outcome whose `after_head` is null: exactly `after_head`,
      `evidence` and `bound_at`.
    - attempt records may also carry `run_id`; no other field is accepted.
    `publish` refuses records and index summaries that break this format. `load` reports such records as
    problems, and `attempts` merges each attempt into the 5.9 view `{attempt_id, source_receipt_digest,
    finding_ids, class_ids, before_head, after_head, outcome, evidence}`. An attempt without an outcome is
    unfinished, and an applied attempt without a new bound commit is incomplete: both are problems.
Round log
    `rounds.jsonl` records the start and finish of every dispatched round attempt. It is not history; the
    attempt budget, sticky decision stops and recorded task limit are read from it. Ordinary calls append
    a `dispatch` reservation immediately before spawn; review-stage reservations also spend a task round.
"""
from __future__ import annotations

import dataclasses
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
from typing import Iterable, Mapping
from contextvars import ContextVar

from oah.atomic import StateCorrupt, StorageError, _fsync_folder, append_jsonl, read_jsonl, write_text_atomic
from oah.lease import TASK_RE
from oah.lockfile import StateLock
from oah.review import ReviewError
from oah.runs import RunError, _captured_state, _contained_path

SCHEMA = 1
INDEX_NAME = "index.jsonl"
ROUNDS_NAME = "rounds.jsonl"
KINDS = ("receipt", "attempt-start", "attempt-outcome", "attempt-head")
OUTCOMES = ("applied", "no-change", "failed", "aborted")
SUMMARY_FIELDS = ("round", "series_round", "base", "head", "verdict", "outcome")
RECEIPT_OUTCOMES = ("converged", "changes-requested", "needs-decision")
ENGINES = ("claude", "codex", "human")
_ATTEMPT_KEYS = {
    "attempt-start": frozenset({"schema", "kind", "task", "attempt_id", "source_receipt", "source_receipt_digest",
                                "finding_ids", "class_ids", "before_head", "engine", "manual", "started_at"}),
    "attempt-outcome": frozenset({"schema", "kind", "task", "attempt_id", "outcome", "after_head", "evidence",
                                  "finished_at"}),
    "attempt-head": frozenset({"schema", "kind", "task", "attempt_id", "after_head", "evidence", "bound_at"}),
}
_OPTIONAL_KEYS = frozenset({"run_id", "engine"})
SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RECEIPT_NAME = re.compile(r"R([1-9][0-9]{0,8})\.json")
_ATTEMPT_NAME = re.compile(r"(A[1-9][0-9]{0,8})-(start|outcome|head)\.json")
_CORRECTION_PUBLICATION = ContextVar("oah_correction_receipts_publication", default=None)


@contextlib.contextmanager
def _correction_publication(state, task, index, lines):
    """The caller holds the lease lock; index bytes were captured with the correction history."""
    publication = {"state": state, "task": task, "index": index, "lines": list(lines)}
    token = _CORRECTION_PUBLICATION.set(publication)
    try:
        yield
    finally:
        _CORRECTION_PUBLICATION.reset(token)


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


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha(value: object) -> bool:
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def _ids(field: str, value: object) -> str | None:
    if not isinstance(value, list) or not value or not all(_text(item) for item in value):
        return f"{field} must be a non-empty list of ids"
    twice = sorted({item for item in value if value.count(item) > 1})
    return f"{field} lists {', '.join(twice)} twice" if twice else None


def _receipt_shape(record: dict) -> str | None:
    from oah.review.schema import SEVERITIES, VERDICTS

    if not (_sha(record.get("base")) and _sha(record.get("head"))):
        return "base and head must be full commit SHAs"
    rounds = record.get("series_round")
    if not (isinstance(rounds, int) and not isinstance(rounds, bool) and rounds >= 1):
        return "series_round must be a round number"
    if record.get("verdict") not in VERDICTS:
        return f"verdict {record.get('verdict')!r} is not one of {', '.join(VERDICTS)}"
    if record.get("outcome") not in RECEIPT_OUTCOMES:
        return f"outcome {record.get('outcome')!r} is not one of {', '.join(RECEIPT_OUTCOMES)}"
    findings, classes = record.get("findings"), record.get("classes")
    if not isinstance(findings, list) or not isinstance(classes, list):
        return "findings and classes must be lists"
    for item in findings:
        if not (isinstance(item, dict) and _text(item.get("finding_id")) and _text(item.get("class_id"))
                and item.get("severity") in SEVERITIES):
            return f"finding {item!r} needs a finding_id, a class_id and a P0-P3 severity"
    for item in classes:
        if not (isinstance(item, dict) and all(_text(item.get(key)) for key in ("class_id", "mechanism", "rule"))):
            return f"class {item!r} needs a class_id, a mechanism and a rule"
    return None


def _attempt_shape(kind: str, record: dict) -> str | None:
    keys = _ATTEMPT_KEYS[kind]
    optional = _OPTIONAL_KEYS if kind == "attempt-head" else {"run_id"}
    missing, extra = sorted(keys - set(record)), sorted(set(record) - keys - optional)
    if missing:
        return f"it misses {', '.join(missing)}"
    if extra:
        return f"it has unknown fields {', '.join(extra)}"
    if "run_id" in record and not _text(record["run_id"]):
        return "run_id must be text"
    if kind == "attempt-head" and "engine" in record and record["engine"] not in ENGINES:
        return "engine must be a known author engine"
    if kind == "attempt-start":
        if not (isinstance(record["source_receipt"], str) and _RECEIPT_NAME.fullmatch(record["source_receipt"])):
            return "source_receipt must name a receipt such as R1.json"
        if not (isinstance(record["source_receipt_digest"], str) and DIGEST_RE.fullmatch(record["source_receipt_digest"])):
            return "source_receipt_digest must be a sha256: digest"
        problem = _ids("finding_ids", record["finding_ids"]) or _ids("class_ids", record["class_ids"])
        if problem:
            return problem
        if not _sha(record["before_head"]):
            return "before_head must be a full commit SHA"
        if record["engine"] not in ENGINES:
            return f"engine {record['engine']!r} is not one of {', '.join(ENGINES)}"
        if not isinstance(record["manual"], bool):
            return "manual must be true or false"
        return None if _text(record["started_at"]) else "started_at must be a time"
    if kind == "attempt-outcome":
        if record["outcome"] not in OUTCOMES:
            return f"outcome {record['outcome']!r} is not one of {', '.join(OUTCOMES)}"
        after = record["after_head"]
        if after is not None and not _sha(after):
            return "after_head must be a full commit SHA or null"
        if after is not None and record["outcome"] != "applied":
            return f"after_head must be null for outcome {record['outcome']}; only applied ends at a commit"
        if not _text(record["evidence"]):
            return "evidence must be non-empty text"
        return None if _text(record["finished_at"]) else "finished_at must be a time"
    if not _sha(record["after_head"]):
        return "after_head must be a full commit SHA"
    if not _text(record["evidence"]):
        return "evidence must be non-empty text"
    return None if _text(record["bound_at"]) else "bound_at must be a time"


def _record_problem(name: str, kind: str, ident: int | str, record: object, task: str) -> str | None:
    if not isinstance(record, dict):
        return f"{name} is not a JSON object"
    if record.get("kind") != kind:
        return f"{name} has kind {record.get('kind')!r}, not {kind!r}"
    if record.get("task") != task and not (kind != "receipt" and record.get("task") == digest(task.encode("utf-8"))):
        return f"{name} belongs to task {record.get('task')!r}, not {task!r}"
    if kind == "receipt" and (type(record.get("round")) is not int or record.get("round") != ident):
        return f"{name} holds round {record.get('round')!r}, not {ident}"
    if kind != "receipt" and record.get("attempt_id") != ident:
        return f"{name} holds attempt {record.get('attempt_id')!r}, not {ident}"
    if type(record.get("schema")) is not int or record.get("schema") != SCHEMA:
        return f"{name} has schema {record.get('schema')!r}, not {SCHEMA}"
    why = _receipt_shape(record) if kind == "receipt" else _attempt_shape(kind, record)
    return f"{name} is not a valid {kind} record: {why}" if why else None


def _summary_problem(name: str, line: Mapping, record: Mapping) -> str | None:
    for field in SUMMARY_FIELDS:
        if field in line and line[field] != record.get(field):
            return (f"{INDEX_NAME} says {field} {line[field]!r} for {name}, but the record says "
                    f"{record.get(field)!r}")
    return None


def publish(state: pathlib.Path, task: str, name: str, record: Mapping, *, summary: Mapping | None = None,
            now: dt.datetime | None = None) -> str:
    """Write one immutable record and index it; returns its digest."""
    try:
        kind, ident = parse_name(name)
    except ValueError as exc:
        raise ReviewError("OAH_REVIEW_RECORD_INVALID", str(exc)) from None
    folder = review_dir(state, task)
    problem = _record_problem(name, kind, ident, dict(record) if isinstance(record, Mapping) else record, task)
    extra = dict(summary or {})
    unknown = sorted(set(extra) - set(SUMMARY_FIELDS))
    if problem is None and unknown:
        problem = f"unknown index fields {', '.join(unknown)}"
    if problem is None:
        problem = _summary_problem(name, extra, record)
    if problem is not None:
        raise ReviewError("OAH_REVIEW_RECORD_INVALID", f"{problem}; nothing was written")
    data = canonical(record)
    value = digest(data)
    line = {"kind": kind, "name": name, "digest": value, "recorded_at": _iso(now), **extra}
    publication = _CORRECTION_PUBLICATION.get()
    captured = (publication is not None and publication["state"] == state and publication["task"] == task)
    try:
        with contextlib.nullcontext() if captured else StateLock(_contained_path(state, ".lock")):
            folder.mkdir(parents=True, exist_ok=True)
            index = _path(state, task, INDEX_NAME)
            lines = publication["lines"] if captured else read_jsonl(index)
            if any(item.get("name") == name for item in lines):
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
            if captured:
                contents = publication["index"] + (json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                write_text_atomic(index, contents.decode("utf-8"))
                publication["index"] = contents
                publication["lines"].append(line)
                from oah.runs import _CORRECTION_CAPTURE
                if (capture := _CORRECTION_CAPTURE.get()) is not None:
                    capture[index] = contents
                    capture[path] = data
            else:
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
    return _load(state, task, read_jsonl)


def _capture_records(state: pathlib.Path, task: str) -> tuple[Records, bytes, tuple[dict, ...]]:
    """Capture the index once, retaining its exact bytes for correction publication."""
    raw, lines = _captured_state(_path(state, task, INDEX_NAME), jsonl=True)
    return _load(state, task, lambda _path: lines, capture=True), raw, tuple(lines)


def _load(state, task, read_index, *, capture=False):
    folder = review_dir(state, task)
    if not folder.is_dir():
        return Records(task)
    problems: list[str] = []
    try:
        lines = read_index(_path(state, task, INDEX_NAME))
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
            if capture:
                data, record = _captured_state(path)
                if not data and record is None:
                    raise FileNotFoundError
            else:
                data = path.read_bytes()
        except FileNotFoundError:
            problems.append(f"{name} is listed in {INDEX_NAME} but the file is missing")
            continue
        except OSError as exc:
            raise StorageError(path, exc) from exc
        if digest(data) != value:
            problems.append(f"{name} no longer matches its indexed digest {value}; it was changed after publication")
            continue
        if not capture:
            try:
                record = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                problems.append(f"{name} is not UTF-8 JSON")
                continue
        problem = _record_problem(name, kind, ident, record, task) or _summary_problem(name, line, record)
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


def _merge(attempt_id: str, items: list[Entry], receipts: Mapping[str, Entry], task: str) -> Attempt:
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
                        "evidence is incomplete. If no oah fix is still running, record it with oah fix --abort "
                        f"--task {task}")
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
                        f"its head binding with oah fix --bind --task {task}, then review again")
    elif fields.get("outcome") == "applied" and after == fields.get("before_head"):
        problems.append(f"{attempt_id} is applied, but its after_head is its before_head {after}; an applied "
                        "correction ends at a new commit")
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
    return [_merge(attempt_id, items, receipts, records.task) for attempt_id, items in groups.items()]


def log_round(state: pathlib.Path, task: str, entry: Mapping, *, now: dt.datetime | None = None) -> None:
    """Append one start or finish line to the round-attempt log under the state lock."""
    try:
        with StateLock(_contained_path(state, ".lock")):
            _log_round_unlocked(state, task, entry, now=now)
    except RunError as exc:
        raise ReviewError(exc.code, str(exc).split(": ", 1)[1]) from None


def _log_round_unlocked(state, task, entry, *, now=None):
    """The caller already holds the state lock (including ordinary call process creation)."""
    read_round_log(state, task)
    review_dir(state, task).mkdir(parents=True, exist_ok=True)
    append_jsonl(_path(state, task, ROUNDS_NAME), {"at": _iso(now), **entry, "task": task})


def read_round_log(state: pathlib.Path, task: str) -> list[dict]:
    """A torn, foreign or invalid stop/budget log refuses with OAH_REVIEW_STATE_INVALID; never repaired."""
    from oah.runs import _CORRECTION_CAPTURE
    path = _path(state, task, ROUNDS_NAME)
    try:
        lines = (_captured_state(path, jsonl=True)[1] if _CORRECTION_CAPTURE.get() is not None else read_jsonl(path))
        if not lines and path.exists():
            raise ValueError  # an existing empty log can be a truncation, never a fresh task
        for line in lines:
            if not _round_shape(line, task):
                raise ValueError
    except (StateCorrupt, OSError, ValueError, TypeError):
        raise ReviewError("OAH_REVIEW_STATE_INVALID", "saved task stop, limit or count state cannot be validated; "
                          "nothing was launched. Restore the original rounds.jsonl from a trusted backup or "
                          "repair it against the task's receipts, preserving spent rounds and pending stops; "
                          "then retry (oah preserves the file)") from None
    return lines


def _round_shape(line, task):
    """Accept landed logs without a task field; validate all fields used to authorize spending."""
    def count(value):
        return type(value) is int and value >= 1

    if not isinstance(line, dict) or line.get("task", task) != task:
        return False
    event = line.get("event")
    if event not in ("start", "finish", "dispatch"):
        return False
    if "max_rounds" in line and line["max_rounds"] is not None and not count(line["max_rounds"]):
        return False
    if event == "dispatch":
        from oah.runs import RUN_ID_RE
        return (line.get("stage") in ("initial", "review", "correction")
                and isinstance(line.get("run_id"), str) and RUN_ID_RE.fullmatch(line["run_id"]) is not None
                and "max_rounds" in line and line.get("task") == task)
    if not _sha(line.get("base")) or not all(count(line.get(key)) for key in ("round", "series_round")):
        return False
    if "attempt" in line and not count(line["attempt"]):
        return False
    if event == "start" and (not count(line.get("attempt"))
                             or line.get("decision") is not None and not _text(line["decision"])):
        return False
    if any(key in line and type(line[key]) is not bool for key in ("extra_round", "extra_attempt", "counted")):
        return False
    if "decision_for" in line and line["decision_for"] is not None and not (
            isinstance(line["decision_for"], str) and DIGEST_RE.fullmatch(line["decision_for"])):
        return False
    if event == "finish" and (type(line.get("counted")) is not bool or not _text(line.get("code"))):
        return False
    return True


def task_lock_path(state: pathlib.Path, task: str) -> pathlib.Path:
    """The task-wide lock: receipt numbers are per task, so one round of a task runs at a time."""
    review_dir(state, task)
    try:
        return _contained_path(state, "reviews", task, ".locks", "task.lock")
    except RunError as exc:
        raise ReviewError(exc.code, str(exc).split(": ", 1)[1]) from None


def lock_path(state: pathlib.Path, task: str, base: str) -> pathlib.Path:
    """The per-series lock file, `reviews/<task>/.locks/<base>.lock`."""
    if not SHA_RE.fullmatch(base or ""):
        raise ReviewError("OAH_REVIEW_BASE_INVALID", f"the series base {base!r} is not a full commit SHA")
    review_dir(state, task)
    try:
        return _contained_path(state, "reviews", task, ".locks", f"{base}.lock")
    except RunError as exc:
        raise ReviewError(exc.code, str(exc).split(": ", 1)[1]) from None
