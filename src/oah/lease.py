# 리스 저장소 — 상태 잠금 아래에서 리스를 시작·갱신·종료·해제하고 모든 변경을 이벤트로 남기며, 읽기는 잠금도 생성도 없이 한다.
"""Leases: who is working on which paths of this repository, until when.

Every change happens under the single state lock and is written atomically; every change is also
appended to events.jsonl. `snapshot()` only reads: it takes no lock and creates nothing.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import re
import secrets
from contextlib import contextmanager
from typing import Callable, Iterator, Sequence

from oah.atomic import StateCorrupt, StorageError, read_json
from oah.journal import Journal
from oah.lockfile import StateLock
from oah.paths import RepoInfo
from oah.scope import outside, overlaps

SCHEMA_VERSION = 1
DEFAULT_TTL_HOURS = 4.0
MAX_TTL_HOURS = 168.0
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
AGENT_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")


class LeaseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


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

    def expired(self, now: dt.datetime) -> bool:
        return _parse(self.expires_at) <= now

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: object, source: pathlib.Path) -> "Lease":
        strings = ("id", "task", "agent", "note", "worktree", "created_at", "expires_at")
        optional = ("branch", "head_at_start")
        valid = (isinstance(data, dict)
                 and all(isinstance(data.get(key), str) for key in strings)
                 and all(data.get(key) is None or isinstance(data.get(key), str) for key in optional)
                 and isinstance(data.get("scope"), list) and data["scope"]
                 and all(isinstance(entry, str) for entry in data["scope"])
                 and (data.get("gates") is None or (isinstance(data["gates"], list)
                                                    and all(isinstance(entry, str) for entry in data["gates"]))))
        if valid:
            try:
                _parse(data["created_at"])
                _parse(data["expires_at"])
            except ValueError:
                valid = False
        if not valid:
            raise StateCorrupt(f"{StateCorrupt.code}: {source} holds a malformed lease record: {data!r}")
        values = {field.name: data.get(field.name) for field in dataclasses.fields(cls)}
        values["gates"] = list(values["gates"] or [])
        return cls(**values)

    def describe(self) -> str:
        return (f"task {self.task} held by {self.agent} (scope {', '.join(self.scope)}; expires {self.expires_at}; "
                f"worktree {self.worktree})")


def _check_names(task: str, agent: str) -> None:
    if not TASK_RE.fullmatch(task or ""):
        raise LeaseError("OAH_TASK_INVALID", f"{task!r}: use letters, digits, '.', '_' or '-' (max 64)")
    if not AGENT_RE.fullmatch(agent or ""):
        raise LeaseError("OAH_AGENT_INVALID", f"{agent!r}: use letters, digits, '.', '_' or '-' (max 32)")


def _check_ttl(ttl_hours: float) -> None:
    if not (0 < ttl_hours <= MAX_TTL_HOURS):
        raise LeaseError("OAH_TTL_INVALID", f"{ttl_hours!r}: the TTL must be above 0 and at most "
                                            f"{MAX_TTL_HOURS:g} hours")


class Store:
    def __init__(self, state_dir: pathlib.Path, *, now: Callable[[], dt.datetime] = utc_now,
                 lock_timeout: float = 10.0) -> None:
        self.dir = pathlib.Path(state_dir)
        self.now = now
        self.lock_timeout = lock_timeout
        self.leases_path = self.dir / "leases.json"
        self.events_path = self.dir / "events.jsonl"
        self.lock_path = self.dir / ".lock"
        self.journal = Journal(self.dir)

    # Reading -------------------------------------------------------------------------------------

    def _load(self) -> list[Lease]:
        data = read_json(self.leases_path, {"version": SCHEMA_VERSION, "leases": []})
        return self._decode(data)

    def _decode(self, data: object) -> list[Lease]:
        if not (isinstance(data, dict) and data.get("version") == SCHEMA_VERSION
                and isinstance(data.get("leases"), list)):
            raise StateCorrupt(f"{StateCorrupt.code}: {self.leases_path} is not a version-{SCHEMA_VERSION} lease file")
        return [Lease.from_json(item, self.leases_path) for item in data["leases"]]

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
        self.journal.commit({"version": SCHEMA_VERSION, "leases": [l.to_json() for l in leases]},
                            {"ts": _iso(self.now()), "event": event, "task": task, "agent": agent,
                             "lease_id": lease_id, **extra})

    @contextmanager
    def _lock(self) -> Iterator[None]:
        try:
            with StateLock(self.lock_path, timeout=self.lock_timeout):
                self.journal.recover(self._decode)
                yield
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(self.lock_path, exc) from exc

    def inspect(self, task: str) -> Lease:
        """Prepare a finish scan: recover pending writes and inspect the current lease under the lock."""
        with self._lock():
            return self._find(self._load(), task)

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
                             expires_at=lease.expires_at)
        return leases

    def start(self, *, task: str, agent: str, scope: Sequence[str], note: str, ttl_hours: float,
              info: RepoInfo, gates: Sequence[str] = ()) -> Lease:
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
            now = self.now()
            lease = Lease(id=secrets.token_hex(8), task=task, agent=agent, scope=list(scope), note=note,
                          worktree=str(info.root), branch=info.branch, head_at_start=info.head,
                          created_at=_iso(now), expires_at=_expiration(now, ttl_hours), gates=list(gates))
            leases.append(lease)
            self._commit(leases, "lease_started", task, agent, lease.id, scope=lease.scope, note=note,
                         expires_at=lease.expires_at, worktree=lease.worktree, branch=lease.branch,
                         gates=lease.gates)
            return lease

    def renew(self, *, task: str, agent: str, ttl_hours: float, scope: Sequence[str] | None = None,
              gates: Sequence[str] | None = None) -> Lease:
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
            self._commit(leases, "lease_renewed", task, agent, lease.id, scope=lease.scope, old_scope=old_scope,
                         expires_at=lease.expires_at, gates=lease.gates)
            return lease

    def finish(self, *, task: str, agent: str, lease_id: str, summary: str, changed: Sequence[str],
               allow_out_of_scope: bool, gate_report: dict | None = None) -> tuple[Lease, list[str]]:
        _check_names(task, agent)
        with self._lock():
            leases = self._load()
            lease = self._find(leases, task)
            if lease.agent != agent:
                raise LeaseError("OAH_LEASE_NOT_YOURS", f"{lease.describe()}")
            if lease.id != lease_id:
                raise LeaseError("OAH_LEASE_CHANGED", f"the lease for {task} was replaced during the change scan; "
                                                    "inspect the current lease and retry oah finish")
            beyond = outside(lease.scope, changed)
            if beyond and not allow_out_of_scope:
                shown = ", ".join(beyond[:20]) + (f" and {len(beyond) - 20} more" if len(beyond) > 20 else "")
                raise LeaseError("OAH_SCOPE_EXCEEDED",
                                 f"changes outside scope {', '.join(lease.scope)}: {shown}. Widen the scope with "
                                 "oah renew --scope, or pass --allow-out-of-scope (recorded)")
            leases.remove(lease)
            gate_fields = {"gates": gate_report} if gate_report is not None else {}
            self._commit(leases, "lease_finished", task, agent, lease.id, summary=summary, out_of_scope=beyond,
                         allowed_out_of_scope=bool(beyond), **gate_fields)
            return lease, beyond

    def release(self, *, task: str, agent: str, reason: str, force: bool) -> Lease:
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
                         holder=lease.agent)
            return lease

    def log(self, *, task: str, agent: str, message: str) -> None:
        _check_names(task, agent)
        with self._lock():
            self._commit(self._load(), "note", task, agent, None, message=message)
