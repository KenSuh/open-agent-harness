# 네이티브 실행기 기록 — 제안·정산·수신 기록을 닫힌 형식으로 만들고, 제안과 정산은 한 번만 쓰며, 결과 파일은 실행하지 않고 짧은 UTF-8 데이터로만 읽는다.
"""Records of the native route (DESIGN §5.5, §5.8).

`oah call --launcher native` starts no oah process. It prints the exact brief for the lead's own host to pass to a
subagent (Claude Code's Agent tool, or a Codex native subagent) and keeps a NativeOfferV1 beside the run. After the
host returns, `oah call settle` keeps a NativeSettlementV1 and a ReceivingRecordV1. The host gives oah no signed
delivery receipt, so delivery is `parent-attested` and the attestation is `cooperative`. Nothing here counts a round,
finishes a task, releases a lease or infers that a skill was applied; an offer never authorizes a process or Orca run.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import secrets
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from oah.atomic import StorageError, write_json_atomic
from oah.engines.effort import EngineError

if TYPE_CHECKING:
    from oah.discovery.records import Json

OFFER_NAME = "native-offer.json"
SETTLEMENT_NAME = "native-settlement.json"
RECEIVING_NAME = "skills-receiving.json"
STALE_CODE = "OAH_NATIVE_OFFER_STALE"
CONFLICT_CODE = "OAH_NATIVE_SETTLE_CONFLICT"
RESULT_CODE = "OAH_NATIVE_RESULT_INVALID"
HOST_UNSUPPORTED_CODE = "OAH_SKILL_HOST_UNSUPPORTED"
UNMANAGED_RUN_CODE = "OAH_SKILL_UNMANAGED_RUN"
OFFER_HOURS = 24  # an older offer cannot be settled; offer again
RESULT_LIMIT = 65_536  # bytes of the host child's returned result
OUTCOMES = ("succeeded", "failed")
STATES = ("offered", "settled-succeeded", "settled-failed", "stale")
OFFER_KEYS = ("version", "id", "run", "bundle_id", "pack_id", "prep_id", "task", "design", "target", "budget_profile",
              "message", "canary_sha256", "created_at", "expires_at", "state", "settlement", "authority", "counted")
SETTLEMENT_KEYS = ("version", "id", "offer_id", "offer_digest", "host_child_id_digest", "task_fingerprint",
                   "bundle_id", "result", "outcome", "delivery", "canary", "receiving_ref", "attestation", "at",
                   "counted")
# A retry is compared on what the lead claims; the receiving reference and times are derived, not claimed.
CLAIM_KEYS = tuple(key for key in SETTLEMENT_KEYS if key not in ("id", "receiving_ref", "at"))
RECEIVING_KEYS = ("version", "id", "run", "bundle_id", "target_digest", "session_id_digest", "delivery", "items",
                  "reference_delivery", "capability_receiving_ref", "at")


class ProtocolRefused(EngineError):
    pass


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest(value: object) -> str:
    """The SHA-256 of a record's canonical JSON form."""
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canary(run_id: str, bundle_id: str | None) -> str:
    """The token the brief asks the subagent to echo. A match proves only the echo, not delivery or understanding."""
    return "OAH-NATIVE-" + hashlib.sha256(f"{run_id}\n{bundle_id}".encode("utf-8")).hexdigest()[:16]


def host_params(engine: str, *, profile_id: str, description: str, message: str) -> dict:
    """Only the parameters each host documents: Claude's Agent tool takes these three; Codex takes the message."""
    if engine == "claude":
        return {"subagent_type": profile_id, "description": description, "prompt": message}
    if engine == "codex":
        return {"message": message}
    raise ProtocolRefused(HOST_UNSUPPORTED_CODE, f"no native host route for {engine!r}; nothing was offered")


def offer_id(record: Mapping) -> str:
    """Hashes everything the offer states except its own id and the fields settlement changes."""
    return digest({key: value for key, value in record.items() if key not in ("id", "state", "settlement")})


def offer_record(*, run_id: str, bundle_id: str | None, pack_id: str, prep_id: str, task: Mapping,
                 design: Mapping | None, target: Mapping, budget_profile: str, message: str, token: str,
                 now: dt.datetime) -> dict:
    data = message.encode("utf-8")
    record = {"version": 1, "run": run_id, "bundle_id": bundle_id, "pack_id": pack_id, "prep_id": prep_id,
              "task": dict(task), "design": None if design is None else dict(design), "target": dict(target),
              "budget_profile": budget_profile, "message": {"bytes": len(data), "sha256": sha(data)},
              "canary_sha256": sha(token.encode("utf-8")), "created_at": iso(now),
              "expires_at": iso(now + dt.timedelta(hours=OFFER_HOURS)), "state": "offered", "settlement": None,
              "authority": "none", "counted": False}
    record["id"] = offer_id(record)
    return {key: record[key] for key in OFFER_KEYS}


def _create_only(path: pathlib.Path, record: Mapping) -> bool:
    """Write `record` to `path` atomically unless the path exists. False when it already existed."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        return True
    except OSError as exc:
        raise StorageError(path, exc) from exc
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _read(path: pathlib.Path, keys: tuple[str, ...], *, optional: tuple[str, ...] = (),
          required: bool = False,
          parse: Callable[[dict[str, Json]], dict[str, Json]] = dict) -> dict[str, Json] | None:
    from oah.discovery import records

    def checked(data: dict[str, Json]) -> dict[str, Json]:
        if not set(keys) <= set(data) <= {*keys, *optional} or type(data.get("version")) is not int \
                or data["version"] != 1:
            raise ValueError("not a version 1 record with exactly its documented keys")
        return parse(data)

    return records.read_json(path, required=required, parse=checked)


def read_offer(run_dir: pathlib.Path, *, required: bool = False) -> dict | None:
    path = pathlib.Path(run_dir) / OFFER_NAME

    def checked(offer: dict[str, Json]) -> dict[str, Json]:
        if (offer["id"] != offer_id(offer) or offer["state"] not in STATES
                or offer["run"] != pathlib.Path(run_dir).name):
            raise ValueError("the offer does not match its own id, state or run")
        task, target, message = offer["task"], offer["target"], offer["message"]
        if not isinstance(task, dict) or not isinstance(target, dict) or not isinstance(message, dict):
            raise ValueError("the offer's task, target and message must be objects")
        lease = task.get("lease")
        if not isinstance(lease, dict):
            raise ValueError("the offer needs its lease snapshot")
        generation, size = lease.get("generation"), message.get("bytes")
        if (not all(isinstance(task.get(key), str) and task[key]
                           for key in ("dispatch_stage", "kind", "risk"))
                or not all(isinstance(target.get(key), str) and target[key]
                           for key in ("engine", "profile_id", "model", "effort"))
                or type(generation) is not int or generation < 0 or not isinstance(message.get("sha256"), str)
                or type(size) is not int or size < 0):
            raise ValueError("the offer's task, target and message fields are incomplete")
        for name in ("created_at", "expires_at"):
            value = offer[name]
            if not isinstance(value, str):
                raise ValueError("the offer timestamps must be ISO strings")
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                raise ValueError("the offer timestamps need UTC offsets")
            stamp.astimezone(dt.timezone.utc)
        return offer

    return _read(path, OFFER_KEYS, required=required, parse=checked)


def publish_offer(run_dir: pathlib.Path, record: Mapping) -> dict:
    """Create-only: a matching repeat returns the stored offer; a different one refuses and changes nothing."""
    path = pathlib.Path(run_dir) / OFFER_NAME
    if _create_only(path, record):
        return dict(record)
    stored = read_offer(run_dir)
    if stored is not None and stored["id"] == record["id"]:
        return stored
    raise ProtocolRefused(CONFLICT_CODE, f"run {record['run']} already holds a different native offer; nothing was "
                      "offered")


def set_state(run_dir: pathlib.Path, offer: Mapping, state: str, settlement: str | None) -> dict:
    """The offer with a new state; its id, which leaves state and settlement out, does not change."""
    updated = {**offer, "state": state, "settlement": settlement}
    write_json_atomic(pathlib.Path(run_dir) / OFFER_NAME, {key: updated[key] for key in OFFER_KEYS})
    return updated


def read_result(path: pathlib.Path) -> bytes:
    """The host child's result as data: never run, at most RESULT_LIMIT bytes, strict UTF-8, no oah marker line."""
    from oah.discovery import inputs

    try:
        data = inputs.read_bytes(path, limit=RESULT_LIMIT)
    except OSError as exc:
        raise StorageError(pathlib.Path(path), exc) from exc
    if len(data) > RESULT_LIMIT:
        raise ProtocolRefused(f"{RESULT_CODE}(too-large)", f"the result file is over {RESULT_LIMIT} bytes; keep the "
                          "subagent's reply short, or save its long output elsewhere and settle with a summary")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolRefused(f"{RESULT_CODE}(not-utf8)", "the result file is not valid UTF-8 text") from None
    if any(line.lstrip().casefold().startswith("<!-- oah") for line in text.split("\n")):
        raise ProtocolRefused(f"{RESULT_CODE}(marker)", "the result file has a line starting with <!-- oah, which only "
                          "oah writes; remove that line and settle again")
    return data


def claim(offer: Mapping, *, host_child_id: str, task_fingerprint: str, result: bytes, outcome: str,
          token: str | None) -> dict:
    """The settlement fields the lead's command states, without the derived receiving reference, time or id."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    echoed = ("absent" if token is None
              else "match" if sha(token.encode("utf-8")) == offer["canary_sha256"] else "mismatch")
    return {"version": 1, "offer_id": offer["id"], "offer_digest": offer["message"]["sha256"],
            "host_child_id_digest": sha(host_child_id.encode("utf-8")), "task_fingerprint": task_fingerprint,
            "bundle_id": offer["bundle_id"], "result": {"bytes": len(result), "sha256": sha(result)},
            "outcome": outcome, "delivery": "parent-attested", "canary": echoed, "attestation": "cooperative",
            "counted": False}


def read_settlement(run_dir: pathlib.Path, *, required: bool = False) -> dict | None:
    path = pathlib.Path(run_dir) / SETTLEMENT_NAME

    def checked(settlement: dict[str, Json]) -> dict[str, Json]:
        if settlement["id"] != digest({key: value for key, value in settlement.items() if key != "id"}):
            raise ValueError("the settlement does not match its own id")
        return settlement

    return _read(path, SETTLEMENT_KEYS, required=required, parse=checked)


def read_receiving(run_dir: pathlib.Path, *, required: bool = False) -> dict | None:
    path = pathlib.Path(run_dir) / RECEIVING_NAME

    def checked(record: dict[str, Json]) -> dict[str, Json]:
        try:
            valid_receiving(record, run_id=pathlib.Path(run_dir).name)
        except ProtocolRefused as exc:
            raise ValueError(str(exc)) from None
        return record

    return _read(path, RECEIVING_KEYS, optional=("available_usage",), required=required, parse=checked)


def valid_receiving(record: Mapping, *, run_id: str, receiving_ref: str | None = None) -> None:
    from oah.capabilities.usage import valid_summary

    if (record.get("run") != run_id or record.get("id") != digest({key: value for key, value in record.items()
                                                                 if key != "id"})
            or receiving_ref is not None and record.get("id") != receiving_ref):
        raise ProtocolRefused(STALE_CODE, "the receiving record no longer matches its run and settlement; "
                              "the earlier records are kept; offer again with oah call --launcher native")
    if "available_usage" in record and not valid_summary(record["available_usage"]):
        raise ProtocolRefused(STALE_CODE, "the receiving record has no closed available-usage summary; "
                              "the earlier records are kept; offer again with oah call --launcher native")


def receiving_for_read(run_dir: pathlib.Path) -> dict | None:
    offer = read_offer(run_dir)
    settlement = read_settlement(run_dir, required=offer is not None and offer["settlement"] is not None)
    receiving = read_receiving(run_dir, required=settlement is not None)
    if receiving is not None:
        valid_receiving(receiving, run_id=pathlib.Path(run_dir).name,
                        receiving_ref=settlement["receiving_ref"] if settlement is not None else None)
    return receiving


def receiving_record(run_id: str, offer: Mapping, bundle: Mapping | None, reads: Iterable[Mapping], *,
                     now: dt.datetime) -> dict:
    """ReceivingRecordV1 for a settled offer: inline texts were parent-attested; reads are only oah-mediated ones."""
    rows = [row for row in reads if isinstance(row, Mapping) and row.get("route") == "oah-mediated"
            and row.get("run") == run_id]
    items = []
    for entry in (bundle or {}).get("items", []):
        observations = [{"kind": "oah-read", "event_id_digest": digest(row), "bytes": row.get("bytes"),
                         "sha256": row.get("sha256"), "exact": row.get("sha256") == entry["sha256"],
                         "model_directed": False}  # oah cannot tell whether the subagent or the lead ran the read
                        for row in rows if row.get("item") == entry["n"]]
        items.append({"ordinal": entry["n"], "selected": True, "delivery": "parent-attested", "access": "not-needed",
                      "read": "oah-mediated" if observations else "unverified", "observations": observations,
                      "application": {"state": "not-observed", "evidence_refs": []}})
    record = {"version": 1, "run": run_id, "bundle_id": offer["bundle_id"], "target_digest": digest(offer["target"]),
              "session_id_digest": None, "delivery": "parent-attested", "items": items,
              "reference_delivery": "not-needed", "capability_receiving_ref": None, "at": iso(now)}
    record["id"] = digest(record)
    return {key: record[key] for key in RECEIVING_KEYS}


def record_settlement(run_dir: pathlib.Path, offer: Mapping, stated: Mapping, receiving: Mapping,
                      now: dt.datetime) -> tuple[dict, dict, bool]:
    """(the settlement, receiving record, whether they already existed). A first settlement writes the receiving record, then the
    settlement create-only, then the offer's state. An identical retry returns the first receipt; a contradictory
    one refuses and changes nothing."""
    run_dir = pathlib.Path(run_dir)
    existing = read_settlement(run_dir, required=offer["settlement"] is not None)
    if existing is None:
        valid_receiving(receiving, run_id=offer["run"])
        write_json_atomic(run_dir / RECEIVING_NAME, dict(receiving))
        settlement = {**stated, "receiving_ref": receiving["id"], "at": iso(now)}
        settlement["id"] = digest(settlement)
        settlement = {key: settlement[key] for key in SETTLEMENT_KEYS}
        if _create_only(run_dir / SETTLEMENT_NAME, settlement):
            set_state(run_dir, offer, f"settled-{stated['outcome']}", settlement["id"])
            return settlement, dict(receiving), False
        existing = read_settlement(run_dir, required=True)  # another settle won the race; compare with it
    stored_receiving = read_receiving(run_dir, required=True)
    assert existing is not None and stored_receiving is not None
    valid_receiving(stored_receiving, run_id=offer["run"], receiving_ref=existing["receiving_ref"])
    differs = [key for key in CLAIM_KEYS if existing[key] != stated[key]]
    if differs:
        raise ProtocolRefused(CONFLICT_CODE, f"run {offer['run']} was already settled with another "
                          f"{', '.join(key.replace('_', ' ') for key in differs)}; the first receipt stands and "
                          "nothing was changed")
    if offer["state"] != f"settled-{existing['outcome']}" or offer["settlement"] != existing["id"]:
        set_state(run_dir, offer, f"settled-{existing['outcome']}", existing["id"])  # finish an interrupted settle
    return existing, stored_receiving, True
