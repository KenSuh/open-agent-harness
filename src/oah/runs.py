# 실행 기록 — 호출마다 runs/<task>/<run-id>/ 폴더를 원자적으로 만들고 prompt·stdout·stderr·result.json 을 남기며, 없는 값은 unverified 로 둔다.
"""One folder per engine run: `<state>/runs/<task>/<run-id>/`.

`new_run` builds the folder under a temporary name with a first `result.json` (status `created`) and
renames it into place, so a run folder is either complete or absent. `Run.result(**fields)` merges
fields into `result.json` under the state lock and never rolls status backward; lane P6 records review
runs through it. Effort and header
fields that nothing could observe stay `unverified`, never empty.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
import secrets
from dataclasses import dataclass

from oah.atomic import StorageError, read_json, write_json_atomic, write_text_atomic
from oah.lease import TASK_RE
from oah.lockfile import StateLock

UNVERIFIED = "unverified"
RUN_ID_RE = re.compile(r"r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
TERMINAL = ("finished", "failed", "timed_out", "refused", "lost")
_PROGRESS = {status: n for n, status in enumerate(("created", "prepared", "queued", "running"))}
UNVERIFIED_FIELDS = ("cli_version", "resolved_model", "requested_effort", "launch_effort", "header_effort",
                     "header_check", "resolved_inference_effort", "normalization_basis")
RESULT_FIELDS = (
    "run_id", "task", "engine", "stage", "status", "created_at", "started_at", "finished_at", "duration_s",
    "exit_code", "timed_out", "pid", "command", "cwd", "read_only", "cli_path", "cli_version",
    "requested_model", "resolved_model", "model_source", "observed_models",
    "requested_effort", "launch_effort", "header_effort", "header_check", "header_code",
    "resolved_inference_effort", "normalization_basis", "effort_verified", "effort_observable",
    "axes", "tier", "tier_source", "tier_reasons", "advisory", "override_stage", "warnings", "error",
)


class RunError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Run:
    id: str
    task: str
    path: pathlib.Path

    @property
    def prompt_path(self) -> pathlib.Path:
        return self.path / "prompt.md"

    @property
    def stdout_path(self) -> pathlib.Path:
        return self.path / "stdout.log"

    @property
    def stderr_path(self) -> pathlib.Path:
        return self.path / "stderr.log"

    @property
    def result_path(self) -> pathlib.Path:
        return self.path / "result.json"

    @property
    def launch_path(self) -> pathlib.Path:
        return self.path / "launch.json"

    def write_prompt(self, text: str) -> None:
        write_text_atomic(self.prompt_path, text)

    def write_logs(self, stdout: str, stderr: str) -> None:
        write_text_atomic(self.stdout_path, stdout)
        write_text_atomic(self.stderr_path, stderr)

    def read(self) -> dict:
        data = read_json(self.result_path, None)
        if not isinstance(data, dict):
            raise RunError("OAH_RUN_CORRUPT", f"{self.result_path} is missing or not a JSON object; the run folder "
                           "was changed by hand")
        return data

    def result(self, **fields: object) -> dict:
        """Merge fields into result.json; unknown names are refused so records keep one schema."""
        unknown = sorted(set(fields) - set(RESULT_FIELDS))
        if unknown:
            raise RunError("OAH_RUN_FIELD_UNKNOWN", f"unknown run fields: {', '.join(unknown)}")
        if "status" in fields and fields["status"] not in (*_PROGRESS, *TERMINAL):
            raise RunError("OAH_RUN_STATUS_INVALID", f"unknown run status {fields['status']!r}; use a lifecycle "
                           "status such as queued, running or finished")
        with StateLock(self.path.parents[2] / ".lock"):
            data = self.read()
            current, incoming = data.get("status"), fields.get("status")
            if incoming is not None and ((current in TERMINAL and incoming != current) or
                    _PROGRESS.get(incoming, len(_PROGRESS)) < _PROGRESS.get(current, -1)):
                return data  # ignore the entire stale update, including its pid and error
            data.update(fields)
            for name in UNVERIFIED_FIELDS:
                if data.get(name) in (None, ""):
                    data[name] = UNVERIFIED
            write_json_atomic(self.result_path, data)
            return data


def _contained_path(state_dir: pathlib.Path, *parts: str) -> pathlib.Path:
    """Refuse symlinks and Windows junctions that resolve outside the trusted state root."""
    root = pathlib.Path(state_dir).resolve()
    path = pathlib.Path(state_dir).joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise RunError("OAH_RUN_PATH_UNSAFE", f"{path} resolves outside {root}; remove the linked directory "
                       "and retry with run storage inside the state folder")
    return path


def task_directory(state_dir: pathlib.Path, task: str) -> pathlib.Path:
    """The contained task directory, also used for its intent binding."""
    if not TASK_RE.fullmatch(task or ""):
        raise RunError("OAH_TASK_INVALID", f"task {task!r} must match {TASK_RE.pattern}; choose a valid task name")
    _contained_path(state_dir, "runs")
    return _contained_path(state_dir, "runs", task)


def new_run(state_dir: pathlib.Path, task: str, engine: str, stage: str, *, now: dt.datetime | None = None) -> Run:
    """Create runs/<task>/<run-id>/ atomically, with a first result.json."""
    parent = task_directory(state_dir, task)
    now = now or dt.datetime.now(dt.timezone.utc)
    run_id = f"r{now.astimezone(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    final = parent / run_id
    temp = parent / f".{run_id}.tmp"
    record = {name: None for name in RESULT_FIELDS}
    record.update({name: UNVERIFIED for name in UNVERIFIED_FIELDS})
    record.update(run_id=run_id, task=task, engine=engine, stage=stage, status="created", created_at=_iso(now),
                  warnings=[])
    try:
        temp.mkdir(parents=True)
        write_json_atomic(temp / "result.json", record)
        os.rename(temp, final)
    except OSError as exc:
        raise StorageError(final, exc) from exc
    return Run(id=run_id, task=task, path=final)


def find_run(state_dir: pathlib.Path, run_id: str) -> Run:
    if not RUN_ID_RE.fullmatch(run_id or ""):
        raise RunError("OAH_RUN_ID_INVALID", f"{run_id!r} is not a run id; `oah call` prints ids like "
                       "r20261023T120000Z-1a2b3c4d")
    runs = _contained_path(state_dir, "runs")
    matches = []
    for parent in sorted(runs.iterdir()) if runs.is_dir() else []:
        if not parent.is_dir():
            continue
        task_directory(state_dir, parent.name)
        candidate = _contained_path(state_dir, "runs", parent.name, run_id)
        if candidate.is_dir():
            matches.append(candidate)
    if len(matches) != 1:
        raise RunError("OAH_RUN_NOT_FOUND", f"no run {run_id} in {runs}; check the id `oah call` printed")
    return Run(id=run_id, task=matches[0].parent.name, path=matches[0])
