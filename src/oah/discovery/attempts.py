# 요청 증거 — 시작한 준비 요청의 횟수·해시·상태만 남겨 뒤늦은 거절에도 이미 한 요청을 보존한다.
from __future__ import annotations

import contextlib
import datetime as dt
import pathlib
import secrets
from contextvars import ContextVar
from typing import Iterator, Literal, TypedDict

from oah.discovery.store import digest

Kind = Literal["skill-search", "skill-review", "skill-judge", "difficulty-judge", "engine-judge", "skill-order"]
Status = Literal["started", "returned", "failed", "unknown"]


class RequestFact(TypedDict):
    kind: Kind
    input_digest: str
    state: Status
    requests: int | None
    outcome_digest: str | None


class Summary(TypedDict):
    version: int
    attempt_id: str
    preparation_digest: str | None
    requests: list[RequestFact]
    omitted: int
    usage: Literal["unknown"]
    at: str
    counted: Literal[False]


class Attempt:
    def __init__(self, state: pathlib.Path) -> None:
        self.state = state
        self.id = digest({"nonce": secrets.token_hex(16)})
        self.preparation: str | None = None
        self.rows: list[RequestFact] = []
        self.omitted = 0

    def save(self) -> None:
        from oah.atomic import write_json_atomic

        if not self.rows and not self.omitted:
            return
        summary: Summary = {"version": 1, "attempt_id": self.id, "preparation_digest": self.preparation,
                            "requests": list(self.rows), "omitted": self.omitted, "usage": "unknown",
                            "at": dt.datetime.now(dt.timezone.utc).isoformat(), "counted": False}
        write_json_atomic(self.state / "skills" / "attempts" / f"{self.id[7:]}.json", summary)

    @contextlib.contextmanager
    def request(self, kind: Kind, input_digest: str, *, known: bool = True) -> Iterator[RequestFact]:
        row: RequestFact = {"kind": kind, "input_digest": input_digest, "state": "started",
                            "requests": 1 if known else None, "outcome_digest": None}
        if len(self.rows) < 128:
            self.rows.append(row)
        else:
            self.omitted += 1
        self.save()
        ended = False
        try:
            yield row
            ended = True
        finally:
            row["state"] = "returned" if ended else "failed"
            self.save()


CURRENT: ContextVar[Attempt | None] = ContextVar("oah_e_attempt", default=None)


@contextlib.contextmanager
def scope(state: pathlib.Path) -> Iterator[Attempt]:
    existing = CURRENT.get()
    if existing is not None:
        yield existing
        return
    attempt = Attempt(state)
    token = CURRENT.set(attempt)
    try:
        yield attempt
    finally:
        try:
            attempt.save()
        finally:
            CURRENT.reset(token)


@contextlib.contextmanager
def request(kind: Kind, input_digest: str, *, state: pathlib.Path | None = None,
            known: bool = True, preparation: str | None = None) -> Iterator[RequestFact | None]:
    attempt = CURRENT.get()
    if attempt is None and state is not None:
        attempt = Attempt(state)
    if attempt is None:
        yield
        return
    if attempt.preparation is None:
        attempt.preparation = preparation
    with attempt.request(kind, input_digest, known=known) as row:
        yield row
