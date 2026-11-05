# 공정한 예약 확인 대기열 — schedule.json 에 주기·대기 항목·실행 예약을 남겨, 앞쪽 출처가 계속 실패해도 뒤쪽 출처가 다음 실행에서 반드시 시도되고 부분 실행이 대기열을 되감지 않게 한다.
"""ScanStateV1: the fair, persistent source-check cycle behind `oah sources check --scheduled`.

A cycle starts from the sorted registry, rotated after the last attempted ID, and keeps its pending queue across
invocations; a partial run (budget, rate limit, interruption) never restarts it. Each run holds a reservation with an
expiry; a crashed run's in-flight source is requeued at the tail, labelled interrupted, once that expires. New
registrations join the tail; a removed or re-registered token loses only its own queued entry. `complete` means every
cycle member had a terminal attempt or a valid fresh reuse, not that every source succeeded. This module is pure
data manipulation; `Sources.scheduled` holds the lock around each step and does the I/O outside it.
"""
from __future__ import annotations

import datetime as dt
import secrets
from typing import Mapping

from oah.design.sources.model import iso, parse_iso
from oah.design.sources.state import digest_of

RUN_BUDGET = 1800.0
GRACE = 120.0
MAX_ENTRIES = 1000


def empty() -> dict:
    return {"version": 1, "kind": "oah-sources-schedule",
            "scan": {"generation": 0, "cycle": None, "reservation": None, "last_command_at": None,
                     "last_complete_scan_at": None, "last_attempted": None, "seq": 0, "counts": {}},
            "source_command_run": None, "cooperative_scheduled_run": None}


def well_formed(data: object) -> bool:
    if not isinstance(data, Mapping) or data.get("version") != 1 or data.get("kind") != "oah-sources-schedule":
        return False
    scan = data.get("scan")
    if not isinstance(scan, Mapping) or type(scan.get("generation")) is not int or type(scan.get("seq")) is not int:
        return False
    cycle = scan.get("cycle")
    if cycle is not None:
        if not isinstance(cycle, Mapping) or not isinstance(cycle.get("pending"), list) \
                or not isinstance(cycle.get("done"), list):
            return False
        if len(cycle["pending"]) + len(cycle["done"]) > MAX_ENTRIES:
            return False
        for entry in cycle["pending"] + cycle["done"]:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str) \
                    or not isinstance(entry.get("registration"), str):
                return False
    reservation = scan.get("reservation")
    return reservation is None or (isinstance(reservation, Mapping) and parse_iso(reservation.get("expires_at"))
                                   is not None and isinstance(reservation.get("run"), str))


def _members(registry: Mapping) -> list[dict]:
    return [{"id": key, "registration": spec.registration, "spec_digest": spec.spec_digest}
            for key, spec in sorted(registry.items())]


def _entry(state: dict, member: Mapping, **extra) -> dict:
    state["scan"]["seq"] += 1
    return {**member, "not_before": None, "seq": state["scan"]["seq"], **extra}


def reconcile(state: dict, registry: Mapping) -> None:
    """Drop queued entries whose token no longer matches; append new registrations at the tail."""
    cycle = state["scan"]["cycle"]
    if cycle is None:
        return
    current = {item["id"]: item for item in _members(registry)}
    cycle["pending"] = [entry for entry in cycle["pending"] if entry["id"] in current
                        and current[entry["id"]]["registration"] == entry["registration"]
                        and current[entry["id"]]["spec_digest"] == entry["spec_digest"]]
    known = {(entry["id"], entry["registration"]) for entry in cycle["pending"] + cycle["done"]}
    for member in current.values():
        if (member["id"], member["registration"]) not in known:
            cycle["pending"].append(_entry(state, member))


def start_cycle(state: dict, registry: Mapping, now: dt.datetime) -> None:
    members = _members(registry)
    last = state["scan"]["last_attempted"]
    if last is not None:
        position = next((index for index, item in enumerate(members) if item["id"] > last), 0)
        members = members[position:] + members[:position]
    state["scan"]["cycle"] = {"id": secrets.token_hex(8), "registry_digest": digest_of(members),
                              "started_at": iso(now), "pending": [], "done": []}
    for member in members:
        state["scan"]["cycle"]["pending"].append(_entry(state, member))


def reserve_run(state: dict, now: dt.datetime, budget: float) -> tuple[str | None, str]:
    """(run ID or None, word). Takes over an expired reservation, requeueing its in-flight source as interrupted."""
    scan = state["scan"]
    held = scan["reservation"]
    if held is not None and parse_iso(held["expires_at"]) > now:
        return None, "busy"
    word = "fresh"
    if held is not None:
        word = "took-over"
        source = held.get("source")
        cycle = scan["cycle"]
        if source is not None and cycle is not None:
            cycle["done"].append({"id": source["id"], "registration": source["registration"],
                                  "outcome": "interrupted", "at": iso(now), "seq": source.get("seq")})
            cycle["pending"].append(_entry(state, {key: source[key] for key in ("id", "registration", "spec_digest")},
                                           interrupted=True))
    run = secrets.token_hex(8)
    scan["reservation"] = {"run": run, "source": None, "started_at": iso(now),
                           "expires_at": iso(now + dt.timedelta(seconds=budget + GRACE))}
    scan["generation"] += 1
    return run, word


def owns(state: dict, run: str) -> bool:
    held = state["scan"]["reservation"]
    return held is not None and held.get("run") == run


def next_source(state: dict, now: dt.datetime) -> dict | None:
    """The first queued entry whose not_before has passed, marked in flight with its attempt start time."""
    cycle = state["scan"]["cycle"]
    if cycle is None:
        return None
    for index, entry in enumerate(cycle["pending"]):
        not_before = parse_iso(entry.get("not_before"))
        if not_before is None or not_before <= now:
            cycle["pending"].pop(index)
            flight = dict(entry, attempted_at=iso(now))
            state["scan"]["reservation"]["source"] = flight
            state["scan"]["last_attempted"] = entry["id"]
            state["scan"]["generation"] += 1
            return flight
    return None


def finish_source(state: dict, flight: Mapping, outcome: str, now: dt.datetime,
                  retry_after: str | None = None) -> None:
    """Record a terminal attempt, or requeue a rate-limited entry with its not_before."""
    cycle = state["scan"]["cycle"]
    state["scan"]["reservation"]["source"] = None
    state["scan"]["generation"] += 1
    if cycle is None:
        return
    if outcome in ("skipped-rate-limited", "rate-limited") and retry_after is not None:
        cycle["pending"].append({key: flight[key] for key in ("id", "registration", "spec_digest", "seq")}
                                | {"not_before": retry_after})
        return
    cycle["done"].append({"id": flight["id"], "registration": flight["registration"], "outcome": outcome,
                          "at": iso(now), "seq": flight.get("seq")})


def outstanding(state: dict) -> list[dict]:
    cycle = state["scan"]["cycle"]
    return [] if cycle is None else [dict(entry) for entry in cycle["pending"]]


def close_run(state: dict, run: str, now: dt.datetime) -> str:
    """Release the reservation; complete | partial(<reason>) and the counts of this cycle."""
    scan = state["scan"]
    if owns(state, run):
        scan["reservation"] = None
    scan["last_command_at"] = iso(now)
    scan["generation"] += 1
    cycle = scan["cycle"]
    counts: dict[str, int] = {}
    for entry in (cycle or {}).get("done", []):
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
    scan["counts"] = dict(sorted(counts.items()))
    if cycle is not None and not cycle["pending"]:
        scan["last_complete_scan_at"] = iso(now)
        return "complete"
    pending = (cycle or {}).get("pending", [])
    if pending and all(parse_iso(entry.get("not_before")) is not None and parse_iso(entry["not_before"]) > now
                       for entry in pending):
        return "partial(rate-limited)"
    return "partial(over-budget)"


def due(state: Mapping, now: dt.datetime, interval_hours: float) -> bool:
    """A new cycle is due when no complete scan happened within the interval."""
    last = parse_iso(state["scan"].get("last_complete_scan_at"))
    return last is None or now - last >= dt.timedelta(hours=interval_hours) or now < last
