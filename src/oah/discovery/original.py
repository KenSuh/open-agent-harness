# 원본 계보 — 작업별 원자적 포인터가 지정한 완전한 E 준비 기록만 수정의 원본으로 읽는다.
from __future__ import annotations

import pathlib
from typing import Mapping

from oah.atomic import write_json_atomic
from oah.discovery import preparation, store, tasks

FOLDER = "originals"


def _index(state_dir: pathlib.Path, task: str) -> pathlib.Path:
    return store.root(state_dir) / FOLDER / "tasks" / (store.digest(task)[7:] + ".json")


def publish(run, skills: Mapping[str, preparation.Json], *, stage: str) -> None:
    record = skills.get("prepared_e")
    if stage in ("correction", "review") or record is None:
        write_json_atomic(run.file_path("skills.json"), skills)
        return
    checked = preparation.parse(record)
    state_dir = run.path.parents[2]
    _word, pointer = store.put_record(state_dir, FOLDER, {"run_id": run.id, "digest": checked["digest"]})
    write_json_atomic(run.file_path("skills.json"), skills)
    write_json_atomic(_index(state_dir, run.task), {"original": pointer["id"]})


def resolve(state_dir: pathlib.Path, task: str) -> preparation.Record | None:
    path = _index(state_dir, task)
    index = store._read(path)
    if index is None:
        return None
    if not isinstance(index, dict) or set(index) != {"original"} or not preparation._hash(index["original"]):
        raise store.StoreProblem(path, "the task's original pointer index is incomplete")
    pointer = store.get_record(state_dir, FOLDER, index["original"])
    from oah.runs import RUN_ID_RE

    if pointer is None or set(pointer) != {"id", "run_id", "digest"} \
            or not isinstance(pointer["run_id"], str) or not RUN_ID_RE.fullmatch(pointer["run_id"]) \
            or not preparation._hash(pointer["digest"]):
        raise store.StoreProblem(path, "the task's original pointer is missing or incomplete")
    return pointer


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
