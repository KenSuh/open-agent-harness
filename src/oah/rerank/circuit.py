# Jev 차단기 — 시간 초과·네트워크·429·529 실패가 이어지면 한동안 요청을 멈추고, UTC 하루 요청 수 상한을 지킨다.
"""The Jev circuit breaker and daily request cap: one small file per project, in the re-ranker's state folder.

Every Jev use that sends a request (skill ranking, difficulty advice and the model judge) asks `Breaker.refusal`
right after the provider gate, then `Breaker.reserve` immediately before dispatch. A refusal reports
`unused(circuit-open)`, `unused(daily-limit)` or `unused(circuit-busy)` without a request.
After the request, `Breaker.record` updates the file:

- a success clears the count and closes the breaker;
- a timeout, a network failure, HTTP 429 or HTTP 529 (`COUNTED`) adds one to the count of such failures in a row.
  When the count reaches `[rerank] circuit_failures` (default 5), the breaker opens for `[rerank]
  circuit_open_seconds` (default 600). After that window the next use sends one request: a success closes the
  breaker, a counted failure opens it again for a whole window;
- any other outcome (HTTP 401, a bad response, an answer below the confidence threshold) leaves the count alone.

`[rerank] max_requests_per_day` (default 0, meaning off) caps the requests sent in one UTC day. Every request
is reserved before dispatch, on that UTC day, and stays counted even if interrupted; the next UTC day starts
again from zero. Recording the outcome never changes the daily count.

The file holds counts, UTC times and failure-kind names only: never request text, key names, URLs or answers.
It is replaced atomically (a temporary file, then `os.replace`). A missing, unreadable or malformed file reads as
closed, and the next update replaces it; an open window longer than the current setting (a clock that went back,
or a shorter setting) counts as over. A short cross-process lock serializes each read, update and replacement;
the provider request never holds it. Lock contention refuses the request after a bounded wait. Without a state
folder nothing is read or written. With `write=False` (a correction's preparation), outcomes are not recorded,
but an enabled daily cap still reserves the request. A failed write is ignored unless a daily reservation is
needed, in which case the request is skipped: the breaker never blocks work.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
from typing import Mapping

from oah.atomic import write_text_atomic
from oah.config import ConfigError
from oah.lockfile import LockTimeout, StateLock
from oah.rerank.base import NETWORK, OVERLOADED, RATE_LIMITED, TIMEOUT

FILE_NAME = "rerank-circuit.json"
LOCK_NAME = "rerank-circuit.lock"
LOCK_WAIT_SECONDS = 1.0
BUSY_CODE = "circuit-busy"
COUNTED = (TIMEOUT, NETWORK, RATE_LIMITED, OVERLOADED)
OPEN, DAILY_LIMIT, SUCCESS = "circuit-open", "daily-limit", "success"
DEFAULTS = {"circuit_failures": 5, "circuit_open_seconds": 600, "max_requests_per_day": 0}
LONGEST_WINDOW = 365 * 24 * 3600  # seconds; a longer window could not be stored as a time
FILE_LIMIT = 4096  # bytes; a larger file is malformed
_FIELDS = ("failures", "last_failure", "open_until", "day", "requests")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclasses.dataclass(frozen=True)
class Limits:
    failures: int = DEFAULTS["circuit_failures"]
    open_seconds: int = DEFAULTS["circuit_open_seconds"]
    per_day: int = DEFAULTS["max_requests_per_day"]


@dataclasses.dataclass(frozen=True)
class State:
    failures: int = 0  # counted failures in a row
    last_failure: str | None = None  # the kind of the last counted failure
    open_until: dt.datetime | None = None
    day: str | None = None  # the UTC day `requests` counts, as YYYY-MM-DD
    requests: int = 0

    def to_json(self) -> dict:
        return {"failures": self.failures, "last_failure": self.last_failure,
                "open_until": None if self.open_until is None else stamp(self.open_until), "day": self.day,
                "requests": self.requests}


def limits(config: Mapping) -> Limits:
    """The breaker keys of a `[rerank]` table; a bad value refuses with `OAH_CONFIG_INVALID`, naming the key."""
    values = []
    for key, least, most in (("circuit_failures", 1, None), ("circuit_open_seconds", 1, LONGEST_WINDOW),
                             ("max_requests_per_day", 0, None)):
        value = config.get(key, DEFAULTS[key])
        if type(value) is not int or value < least or (most is not None and value > most):
            span = f"from {least} to {most}" if most is not None else f"of at least {least}"
            raise ConfigError("OAH_CONFIG_INVALID", f".oah/config.toml: rerank.{key} must be a whole number {span}; "
                              f"change it, or remove it to use the default {DEFAULTS[key]}")
        values.append(value)
    return Limits(*values)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parsed(data: object) -> State | None:
    """The state a file holds, or None when it is not exactly the breaker's shape."""
    if type(data) is not dict or set(data) != set(_FIELDS):
        return None
    failures, kind, until, day, requests = (data[name] for name in _FIELDS)
    if type(failures) is not int or failures < 0 or type(requests) is not int or requests < 0:
        return None
    if kind is not None and kind not in COUNTED:
        return None
    if until is not None and (type(until) is not str or not _TIME.fullmatch(until)):
        return None
    if day is not None and (type(day) is not str or not _DAY.fullmatch(day)):
        return None
    try:
        if until is not None:
            until = dt.datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if day is not None:
            dt.date.fromisoformat(day)
    except ValueError:  # a date or time that does not exist
        return None
    return State(failures, kind, until, day, requests)


def read(path: pathlib.Path) -> tuple[State, bool]:
    """(state, sound): a missing file is sound and closed; an unreadable or malformed one is closed, not sound."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(FILE_LIMIT + 1)
    except FileNotFoundError:
        return State(), True
    except OSError:
        return State(), False
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= FILE_LIMIT else None
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        data = None
    state = _parsed(data)
    return (State(), False) if state is None else (state, True)


def refusal(state: State, limits: Limits, now: dt.datetime) -> str | None:
    """`OPEN` or `DAILY_LIMIT` when no request may be sent at `now`, else None."""
    # One second of slack: the stored time is rounded up to a whole second.
    window = dt.timedelta(seconds=limits.open_seconds + 1)
    if state.open_until is not None and now < state.open_until <= now + window:
        return OPEN
    if limits.per_day and state.day == now.date().isoformat() and state.requests >= limits.per_day:
        return DAILY_LIMIT
    return None


def advance(state: State, limits: Limits, outcome: str | None, now: dt.datetime) -> State:
    """The state after one request whose outcome is `SUCCESS`, a `COUNTED` kind, or None (count unchanged)."""
    failures, kind, until = state.failures, state.last_failure, state.open_until
    if outcome == SUCCESS:
        failures, kind, until = 0, None, None
    elif outcome in COUNTED:
        failures, kind = failures + 1, outcome
        if failures >= limits.failures:
            start = now.replace(microsecond=0) + dt.timedelta(seconds=1 if now.microsecond else 0)
            until = start + dt.timedelta(seconds=limits.open_seconds)  # a whole window, rounded up
    return State(failures, kind, until, state.day, state.requests)


class Breaker:
    """One Jev use's view of the breaker file. Without a state folder it reads and writes nothing."""

    def __init__(self, state_dir: os.PathLike | str | None, limits: Limits, *, write: bool = True) -> None:
        self.path = None if state_dir is None else pathlib.Path(state_dir) / FILE_NAME
        self.limits = limits
        self.write = write

    def refusal(self) -> str | None:
        """`circuit-open` or `daily-limit` when this use must not send a request now, else None."""
        if self.path is None:
            return None
        return refusal(read(self.path)[0], self.limits, utc_now())

    def reserve(self) -> str | None:
        """Recheck under the lock and count a capped request before dispatch; never hold the lock over a request."""
        if self.path is None or (not self.write and not self.limits.per_day):
            return self.refusal()
        try:
            with StateLock(self.path.with_name(LOCK_NAME), timeout=LOCK_WAIT_SECONDS):
                now = utc_now()
                state, _sound = read(self.path)
                blocked = refusal(state, self.limits, now)
                if blocked is not None:
                    return blocked
                if self.limits.per_day:
                    today = now.date().isoformat()
                    updated = dataclasses.replace(state, day=today,
                                                  requests=(state.requests if state.day == today else 0) + 1)
                    write_text_atomic(self.path, json.dumps(updated.to_json(), sort_keys=True) + "\n", retry_seconds=0)
        except LockTimeout:
            return BUSY_CODE
        except OSError:
            return BUSY_CODE if self.limits.per_day else None
        return None

    def record(self, outcome: str | None) -> None:
        """After one request: `SUCCESS`, a `COUNTED` kind, or None for an outcome that leaves the count alone."""
        if self.path is None or not self.write:
            return
        try:
            with StateLock(self.path.with_name(LOCK_NAME), timeout=LOCK_WAIT_SECONDS):
                state, sound = read(self.path)
                updated = advance(state, self.limits, outcome, utc_now())
                if updated == state and sound:
                    return  # nothing changed, so nothing is written
                write_text_atomic(self.path, json.dumps(updated.to_json(), sort_keys=True) + "\n", retry_seconds=0)
        except (OSError, LockTimeout):
            pass  # an unwritable state folder never blocks a call


def outcome(status: str, *, below_threshold: bool = False) -> str | None:
    """The breaker outcome of one request's status: `used` is a success unless its answer is below the threshold;
    `unused(error:<kind>)` counts for the four `COUNTED` kinds; anything else leaves the count alone."""
    if status == "used":
        return None if below_threshold else SUCCESS
    kind = status[len("unused(error:"):-1] if status.startswith("unused(error:") and status.endswith(")") else None
    return kind if kind in COUNTED else None
