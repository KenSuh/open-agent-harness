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
import contextlib
import json
import os
import pathlib
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from oah import launchers
from oah.atomic import RETRY_SECONDS, StateCorrupt, StorageError, read_json, write_json_atomic, write_text_atomic
from oah.lease import TASK_RE
from oah.lockfile import LockTimeout, StateLock

T = TypeVar("T")

UNVERIFIED = "unverified"
RUN_ID_RE = re.compile(r"r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
TERMINAL = ("finished", "failed", "timed_out", "refused", "lost")
_PROGRESS = {status: n for n, status in enumerate(("created", "prepared", "queued", "running"))}
UNVERIFIED_FIELDS = ("cli_version", "resolved_model", "requested_effort", "launch_effort", "header_effort",
                     "header_check", "resolved_inference_effort", "normalization_basis", "detached")
RESULT_FIELDS = (
    "run_id", "task", "engine", "stage", "status", "created_at", "started_at", "finished_at", "duration_s",
    "exit_code", "timed_out", "pid", "command", "cwd", "read_only", "cli_path", "cli_version",
    "requested_model", "resolved_model", "model_source", "observed_models",
    "requested_effort", "launch_effort", "header_effort", "header_check", "header_code",
    "resolved_inference_effort", "normalization_basis", "effort_verified", "effort_observable",
    "axes", "tier", "tier_source", "tier_reasons", "advisory", "override_stage", "warnings", "error", "detached",
    "launcher", "worker_output",
)


class RunError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _retry_until(deadline: float, timeout: float, operation: Callable[[], T]) -> T:
    """Retry transient storage contention without adding a fresh budget to a caller's wait."""
    stop = min(deadline, time.monotonic() + timeout)
    while True:
        launchers.remaining(deadline, timeout)
        try:
            return operation()
        except (PermissionError, StorageError, LockTimeout) as exc:
            if isinstance(exc, StorageError) and not isinstance(exc.__cause__, PermissionError):
                raise
            delay = stop - time.monotonic()
            if delay <= 0:
                launchers.remaining(deadline, timeout)
                raise
            time.sleep(min(0.05, delay))


@dataclass(frozen=True)
class Run:
    id: str
    task: str
    path: pathlib.Path
    deadline: float | None = field(default=None, kw_only=True)

    def file_path(self, name: str) -> pathlib.Path:
        """Re-check the run and file immediately before use, including unlink by a caller."""
        state = self.path.parents[2]
        return _contained_path(state, *self.path.relative_to(state).parts, name)

    @property
    def prompt_path(self) -> pathlib.Path:
        return self.file_path("prompt.md")

    @property
    def stdout_path(self) -> pathlib.Path:
        return self.file_path("stdout.log")

    @property
    def stderr_path(self) -> pathlib.Path:
        return self.file_path("stderr.log")

    @property
    def result_path(self) -> pathlib.Path:
        return self.file_path("result.json")

    @property
    def launch_path(self) -> pathlib.Path:
        return self.file_path("launch.json")

    def write_prompt(self, text: str) -> None:
        write_text_atomic(self.prompt_path, text)

    def write_logs(self, stdout: str, stderr: str) -> None:
        self._write_text("stdout.log", stdout)
        self._write_text("stderr.log", stderr)

    def _write_text(self, name: str, text: str) -> None:
        path = self.file_path(name)
        if self.deadline is None:
            write_text_atomic(path, text)
        else:
            _retry_until(self.deadline, RETRY_SECONDS, lambda: write_text_atomic(path, text, retry_seconds=0))

    def _read_json(self, name: str) -> object:
        path = self.file_path(name)
        if self.deadline is None:
            return read_json(path, None)
        try:
            raw = _retry_until(self.deadline, RETRY_SECONDS, path.read_bytes)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(path, exc) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateCorrupt(f"{StateCorrupt.code}: {path} is not valid UTF-8 JSON ({exc}). "
                               "oah never resets it; repair or remove the file by hand.") from exc

    def _write_result(self, data: dict) -> None:
        path = self.result_path
        if self.deadline is None:
            write_json_atomic(path, data)
        else:
            _retry_until(self.deadline, RETRY_SECONDS, lambda: write_json_atomic(path, data, retry_seconds=0))

    @contextlib.contextmanager
    def _result_lock(self):
        lock = StateLock(_contained_path(self.path.parents[2], ".lock"))
        if self.deadline is None:
            with lock:
                yield
        else:
            timeout, lock.timeout = lock.timeout, 0
            _retry_until(self.deadline, timeout, lock.__enter__)
            try:
                yield
            finally:
                lock.__exit__(None, None, None)

    def read(self) -> dict:
        data = self._read_json("result.json")
        if not isinstance(data, dict):
            raise RunError("OAH_RUN_CORRUPT", f"{self.result_path} is missing or not a JSON object; the run folder "
                           "was changed by hand")
        return data

    def claim_queued_orca(self) -> None:
        """Claim only the directory and record already validated by worker-run."""
        if not isinstance(self, WorkerRun) or self._validated is None:
            raise _launch_invalid()
        with self._result_lock():
            data = self.read()
            launcher = data.get("launcher")
            if (data.get("status") != "queued" or not isinstance(launcher, dict) or
                    launcher.get("name") != "orca"):
                raise RunError("OAH_RUN_NOT_QUEUED", "this run is not a queued Orca run, so nothing was started; "
                               "start a new run with oah call")
            if self._record_identity(data) != self._record_identity(self._validated):
                raise _launch_invalid()
            try:
                fd = self._open_file("worker.claim", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                raise RunError("OAH_RUN_NOT_QUEUED", "this Orca run was already claimed, so nothing was started; "
                               "start a new run with oah call") from None
            os.close(fd)
            self._check_directory()
            data.update(status="running", pid=os.getpid())
            self._write_result(data)

    def result(self, **fields: object) -> dict:
        """Merge fields into result.json; unknown names are refused so records keep one schema."""
        unknown = sorted(set(fields) - set(RESULT_FIELDS))
        if unknown:
            raise RunError("OAH_RUN_FIELD_UNKNOWN", f"unknown run fields: {', '.join(unknown)}")
        if "status" in fields and fields["status"] not in (*_PROGRESS, *TERMINAL):
            raise RunError("OAH_RUN_STATUS_INVALID", f"unknown run status {fields['status']!r}; use a lifecycle "
                           "status such as queued, running or finished")
        self.file_path("result.json")
        with self._result_lock():
            data = self.read()
            current, incoming = data.get("status"), fields.get("status")
            if incoming is not None and ((current in TERMINAL and incoming != current) or
                    _PROGRESS.get(incoming, len(_PROGRESS)) < _PROGRESS.get(current, -1)):
                return data  # ignore the entire stale update, including its pid and error
            data.update(fields)
            if isinstance(data.get("launcher"), dict) and data["launcher"].get("name") == "orca":
                for name in ("command", "read_only"):
                    if name in fields and fields[name] is None:
                        data.pop(name, None)
            for name in UNVERIFIED_FIELDS:
                if data.get(name) in (None, ""):
                    data[name] = UNVERIFIED
            self._write_result(data)
            return data


def _launch_invalid() -> RunError:
    return RunError("OAH_RUN_LAUNCH_INVALID", "the run folder or saved launch changed; "
                    "start a new run with oah call")


def _open_worker_directory(path: pathlib.Path) -> int:
    if os.name != "nt":
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    # BACKUP_SEMANTICS opens a directory; omit FILE_SHARE_DELETE to prevent rename/replacement.
    handle = api.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        api.CloseHandle(handle)
        raise


@dataclass(frozen=True)
class _WorkerPrompt:
    run: WorkerRun

    def read_bytes(self) -> bytes:
        self.run._check_directory()
        if self.run._prompt is None:
            raise _launch_invalid()
        return self.run._prompt


class WorkerRun(Run):
    """Pin directory identity for oah's races; the user's state folder remains trusted.

    On Windows the open directory prevents rename/replacement, but does not pin its children
    against a local writer's reparse-point races. That adversary is outside the trust boundary.
    """

    def __init__(self, run: Run) -> None:
        super().__init__(run.id, run.task, run.path)
        self._fd: int | None = None
        self._validated: dict | None = None
        self._prompt: bytes | None = None

    def __enter__(self) -> WorkerRun:
        self.file_path("result.json")
        self._fd = _open_worker_directory(self.path)
        try:
            self._identity = os.fstat(self._fd)
            self._check_directory()
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _check_directory(self) -> None:
        if self._fd is None:
            raise _launch_invalid()
        try:
            info = self.path.stat(follow_symlinks=False)
        except OSError:
            raise _launch_invalid() from None
        if (not stat.S_ISDIR(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or
                (info.st_dev, info.st_ino) != (
                    self._identity.st_dev, self._identity.st_ino)):
            raise _launch_invalid()

    def file_path(self, name: str) -> pathlib.Path:
        if self._fd is not None:
            self._check_directory()
        return super().file_path(name)

    def _open_file(self, name: str, flags: int) -> int:
        self._check_directory()
        path = self.file_path(name)
        if os.name == "nt":
            fd = os.open(path, flags | os.O_BINARY, 0o600)  # only the directory is pinned; children are trusted
        else:
            fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _launch_invalid()
            self._check_directory()
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _read_bytes(self, name: str) -> bytes:
        with os.fdopen(self._open_file(name, os.O_RDONLY), "rb") as handle:
            raw = handle.read()
        self._check_directory()
        return raw

    def _read_json(self, name: str) -> object:
        try:
            data = json.loads(self._read_bytes(name).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, FileNotFoundError):
            raise _launch_invalid() from None
        if name == "result.json":
            if not isinstance(data, dict) or data.get("run_id") != self.id or data.get("task") != self.task:
                raise _launch_invalid()
            if self._validated is not None and any(data.get(key) != self._validated.get(key) for key in (
                    "engine", "stage", "created_at", "cwd", "resolved_model", "launch_effort", "cli_version",
                    "override_stage")):
                raise _launch_invalid()
        return data

    def _write_text(self, name: str, text: str) -> None:
        if os.name == "nt":
            return super()._write_text(name, text)
        temp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            with os.fdopen(self._open_file(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                           "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            self.file_path(name)
            os.replace(temp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            os.fsync(self._fd)
            self._check_directory()
        finally:
            try:
                os.unlink(temp, dir_fd=self._fd)
            except FileNotFoundError:
                pass

    def _write_result(self, data: dict) -> None:
        self._write_text("result.json", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _record_identity(data: dict) -> dict:
        record = dict(data)
        launcher = dict(record.get("launcher") or {})
        for name in ("terminal", "terminal_process_id", "host_platform", "terminal_cursor"):
            launcher.pop(name, None)  # Orca may finish creating the terminal while the worker validates.
        return dict(record, launcher=launcher)

    def bind(self, data: dict) -> None:
        if data.get("run_id") != self.id or data.get("task") != self.task:
            raise _launch_invalid()
        self._validated = data
        self._prompt = self._read_bytes("prompt.md")
        try:
            self._prompt.decode("utf-8")
        except UnicodeDecodeError:
            raise _launch_invalid() from None

    @property
    def prompt_path(self) -> _WorkerPrompt:
        return _WorkerPrompt(self)


def _contained_path(state_dir: pathlib.Path, *parts: str) -> pathlib.Path:
    """Refuse every symlink or junction beneath the trusted state root."""
    # Re-check at each operation, not just lookup. This does not defend against a concurrent local
    # attacker who can already write to the state folder and swap a component after this check.
    root = pathlib.Path(state_dir).resolve()
    path = root.joinpath(*parts)

    def unsafe() -> RunError:
        return RunError("OAH_RUN_PATH_UNSAFE", f"{path} is linked or outside {root}; remove the linked path "
                        "and retry with run storage inside the state folder")

    if not path.is_relative_to(root) or ".." in path.relative_to(root).parts:
        raise unsafe()
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        # Python 3.11 has no Path.is_junction(); Windows marks junctions as reparse points.
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise unsafe()
    if not path.resolve().is_relative_to(root):
        raise unsafe()
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
        _contained_path(state_dir, "runs", task, temp.name)
        temp.mkdir(parents=True)
        write_json_atomic(_contained_path(state_dir, "runs", task, temp.name, "result.json"), record)
        _contained_path(state_dir, "runs", task, temp.name)
        _contained_path(state_dir, "runs", task, run_id)
        os.rename(temp, final)
    except OSError as exc:
        raise StorageError(final, exc) from exc
    return Run(id=run_id, task=task, path=final)


def find_run(state_dir: pathlib.Path, run_id: str, *, deadline: float | None = None) -> Run:
    if not RUN_ID_RE.fullmatch(run_id or ""):
        raise RunError("OAH_RUN_ID_INVALID", f"{run_id!r} is not a run id; `oah call` prints ids like "
                       "r20261023T120000Z-1a2b3c4d")
    runs = _contained_path(state_dir, "runs")
    matches = []
    for parent in sorted(runs.iterdir()) if runs.is_dir() else []:
        _contained_path(state_dir, "runs", parent.name)
        if not parent.is_dir():
            continue
        task_directory(state_dir, parent.name)
        candidate = _contained_path(state_dir, "runs", parent.name, run_id)
        if candidate.is_dir():
            matches.append(candidate)
    if len(matches) != 1:
        raise RunError("OAH_RUN_NOT_FOUND", f"no run {run_id} in {runs}; check the id `oah call` printed")
    return Run(id=run_id, task=matches[0].parent.name, path=matches[0], deadline=deadline)
