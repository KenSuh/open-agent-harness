# 원본 계보 — 작업별 원자적 포인터가 지정한 완전한 E 준비 기록만 수정의 원본으로 읽는다.
from __future__ import annotations

import pathlib
from enum import StrEnum
from typing import Mapping

from oah.atomic import write_json_atomic
from oah.discovery import preparation, records, store, tasks
from oah.engines.effort import EngineError

FOLDER = "originals"


class MissingReason(StrEnum):
    NEVER_WRITTEN = "never-written"
    RETIRED = "retired-by-ordinary-original"


class OriginalUnavailable(EngineError):
    def __init__(self, reason: MissingReason) -> None:
        self.pointer_reason = reason
        super().__init__("OAH_FIX_STATE_CHANGED", "the original pointer is unavailable; start a new initial run")


def _index(state_dir: pathlib.Path, task: str) -> pathlib.Path:
    return store.root(state_dir) / FOLDER / "tasks" / (store.digest(task)[7:] + ".json")


def publish(run, skills: Mapping[str, preparation.Json] | None, *, stage: str) -> None:
    record = skills.get("prepared_e") if skills is not None else None
    is_original = stage not in ("correction", "review")
    state_dir = run.path.parents[2]
    pointer = None
    if is_original and record is not None:
        try:
            checked = preparation.parse(record)
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise EngineError("OAH_RUN_LAUNCH_INVALID", f"{exc}; the original pointer is kept") from None
        _word, pointer = store.put_record(state_dir, FOLDER, {"run_id": run.id, "digest": checked["digest"]})
    if skills is not None:
        write_json_atomic(run.file_path("skills.json"), skills)
    if is_original:
        index = _index(state_dir, run.task)
        if pointer is None and records.present(index):
            write_json_atomic(index, {"original": None})
        elif pointer is not None:
            write_json_atomic(index, {"original": pointer["id"]})


def lookup(state_dir: pathlib.Path, task: str) -> preparation.Record | MissingReason:
    path = _index(state_dir, task)

    def checked_index(index):
        if index != {"original": None} and (set(index) != {"original"} or not preparation._hash(index["original"])):
            raise ValueError("the task's original pointer index is incomplete")
        return index

    index = store._read(path, parse=checked_index)
    if index is None:
        return MissingReason.NEVER_WRITTEN
    if index == {"original": None}:
        return MissingReason.RETIRED
    from oah.runs import RUN_ID_RE

    def checked_pointer(pointer):
        if (set(pointer) != {"id", "run_id", "digest"} or not isinstance(pointer["run_id"], str)
                or not RUN_ID_RE.fullmatch(pointer["run_id"]) or not preparation._hash(pointer["digest"])):
            raise ValueError("the original pointer record is incomplete")
        return pointer

    pointer = store.get_record(state_dir, FOLDER, index["original"], parse=checked_pointer)
    if pointer is None:
        raise store.StoreProblem(path, "the task's original pointer is missing or incomplete")
    return pointer


def resolve(state_dir: pathlib.Path, task: str) -> preparation.Record | None:
    found = lookup(state_dir, task)
    return found if isinstance(found, dict) else None


def selections(state_dir: pathlib.Path, basis: tasks.TaskBasis, policy: str) -> list[preparation.Record]:
    found: list[preparation.Record] = []
    for ref in tasks._index(state_dir).values():
        row = store.get_record(state_dir, tasks.SELECTIONS, ref)
        if row is not None and row.get("packet_digest") == basis.source_digest \
                and row.get("intent_digest") == basis.intent["digest"] and row.get("policy_digest") == policy \
                and (row.get("lease") or {}).get("id") == basis.lease[0] and row.get("review_ids"):
            found.append(row)
    return sorted(found, key=lambda row: str(row["id"]))


def managed(basis: tasks.TaskBasis, selected: list[preparation.Record]) -> bool:
    return bool(selected) or basis.design is not None and any(
        need["resolution"] == "skill" for stage in basis.design["record"]["stages"] for need in stage["needs"])


def needs(basis: tasks.TaskBasis, supplied: list[preparation.Record], policy: str) -> list[preparation.Record]:
    derived = ([item for stage in ("initial", "correction", "review")
                for item in tasks.derive(basis, dispatch_stage=stage, policy=policy)] if basis.design is not None else [])
    return sorted({str(item["id"]): item for item in (*supplied, *derived)}.values(), key=lambda row: str(row["id"]))
