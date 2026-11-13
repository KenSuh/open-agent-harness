# 대상 예산 — 패키지에 고정한 검증 프로필만 받아 전체 전달량과 지연 읽기의 보수적 상한을 확인한다.
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
from typing import Final, Literal, Mapping, Sequence, TypedDict

from oah.discovery.store import digest

Target = Mapping[str, str | None]
SOURCE_PIN: Final = "b500a0772502d930b120e3b4640705074df9e3ababe7205743252c26a4e880b2"
TARGET_KEYS: Final = frozenset({"engine", "route", "cli_version", "profile_id", "model", "effort", "platform",
                              "architecture", "root_digest", "config_digest"})
PROFILE_KEYS: Final = frozenset({"version", "id", "target", "source", "observed_at", "expires_at",
                               "input_capacity_tokens", "output_reserve_tokens", "host_reserve_tokens",
                               "existing_context_upper_tokens", "estimator", "transport_budget_bytes",
                               "skill_budget_bytes", "evidence_ref"})
ESTIMATOR_KEYS: Final = frozenset({"id", "fixed_render_tokens", "tool_schema_upper_tokens"})
READ_ENVELOPE_BYTES: Final = 2048


@dataclasses.dataclass(frozen=True, slots=True)
class PinnedSource:
    raw: bytes
    sha256: str


class Estimator(TypedDict):
    id: Literal["utf8-upper/v1"]
    fixed_render_tokens: int
    tool_schema_upper_tokens: int


class TargetBudgetProfileV1(TypedDict):
    version: Literal[1]
    id: str
    target: Target
    source: Literal["qualified-profile", "live-host"]
    observed_at: str
    expires_at: str
    input_capacity_tokens: int
    output_reserve_tokens: int
    host_reserve_tokens: int
    existing_context_upper_tokens: int
    estimator: Estimator
    transport_budget_bytes: int
    skill_budget_bytes: int
    evidence_ref: str


class Measurement(TypedDict):
    prompt_bytes: int
    deferred_required_bytes: int
    deferred_render_bytes: int
    optional_reserve_bytes: int
    upper_tokens: int
    available_tokens: int
    sha256: str


class BudgetRefused(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        self.reason, self.detail = reason, detail
        super().__init__(reason, detail)

    def __str__(self) -> str:
        return f"OAH_SKILLS_BUDGET_EXCEEDED({self.reason}): {self.detail}"


def _profile_source(_target: Target) -> PinnedSource:
    from oah.discovery.target_profiles import DATA

    return PinnedSource(DATA, SOURCE_PIN)


def moment(value: str) -> dt.datetime:
    if not isinstance(value, str):
        raise BudgetRefused("target-capacity-unknown", "qualification times must be ISO timestamps")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise BudgetRefused("target-capacity-unknown", "qualification times must include a UTC offset")
    try:
        return parsed.astimezone(dt.timezone.utc)
    except OverflowError:
        raise BudgetRefused("target-capacity-unknown", "qualification times must be representable in UTC") from None


def _hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 71 and value.startswith("sha256:") and all(
        char in "0123456789abcdef" for char in value[7:])


def profiles(target: Target, *, now: dt.datetime) -> list[TargetBudgetProfileV1]:
    from oah.discovery import records

    try:
        source = _profile_source(target)
    except OSError as exc:
        raise records.io_failure(pathlib.Path("target-profiles.json"), exc) from exc
    if len(source.raw) > 65536 or hashlib.sha256(source.raw).hexdigest() != source.sha256:
        raise BudgetRefused("target-capacity-unknown", "the packaged qualification data does not match its pin")
    try:
        data = records.json_bytes(source.raw, pathlib.Path("target-profiles.json"))
        if not isinstance(data, dict) or set(data) != {"version", "profiles"} or type(data["version"]) is not int \
                or data["version"] != 1 \
                or not isinstance(data["profiles"], list) or len(data["profiles"]) > 32:
            raise BudgetRefused("target-capacity-unknown", "the qualification source has an unknown format")
        selected: list[TargetBudgetProfileV1] = []
        for profile in data["profiles"]:
            if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS or type(profile["version"]) is not int \
                    or profile["version"] != 1:
                raise BudgetRefused("target-capacity-unknown", "a qualification has unknown or missing fields")
            identity, estimator = profile["target"], profile["estimator"]
            numbers = ("input_capacity_tokens", "output_reserve_tokens", "host_reserve_tokens",
                       "existing_context_upper_tokens", "transport_budget_bytes", "skill_budget_bytes")
            valid = (isinstance(identity, dict) and set(identity) == TARGET_KEYS
                     and all(isinstance(value, str) or key == "cli_version" and value is None
                             for key, value in identity.items())
                     and identity["engine"] in ("claude", "codex")
                     and identity["route"] in ("process", "orca", "native")
                     and _hash(identity["root_digest"]) and _hash(identity["config_digest"])
                     and profile["source"] in ("qualified-profile", "live-host")
                     and all(type(profile[key]) is int and profile[key] >= 0 for key in numbers)
                     and all(profile[key] > 0 for key in ("input_capacity_tokens", "transport_budget_bytes",
                                                         "skill_budget_bytes"))
                     and isinstance(estimator, dict) and set(estimator) == ESTIMATOR_KEYS
                     and estimator["id"] == "utf8-upper/v1"
                     and all(type(estimator[key]) is int and estimator[key] >= 0 for key in
                             ("fixed_render_tokens", "tool_schema_upper_tokens"))
                     and _hash(profile["evidence_ref"]) and _hash(profile["id"])
                     and profile["id"] == digest({key: value for key, value in profile.items() if key != "id"}))
            if not valid:
                raise BudgetRefused("target-capacity-unknown", "a qualification does not satisfy TargetBudgetProfileV1")
            observed, expires = moment(profile["observed_at"]), moment(profile["expires_at"])
            if observed >= expires:
                raise BudgetRefused("target-capacity-unknown", "a qualification has no finite evidence window")
            if sum(profile[key] for key in ("output_reserve_tokens", "host_reserve_tokens",
                                            "existing_context_upper_tokens")) >= profile["input_capacity_tokens"]:
                raise BudgetRefused("target-capacity-unknown", "qualification reserves leave no input capacity")
            if dict(identity) == dict(target) and observed <= now < expires:
                selected.append(TargetBudgetProfileV1(
                    version=1, id=profile["id"], target=identity, source=profile["source"],
                    observed_at=profile["observed_at"], expires_at=profile["expires_at"],
                    input_capacity_tokens=profile["input_capacity_tokens"],
                    output_reserve_tokens=profile["output_reserve_tokens"],
                    host_reserve_tokens=profile["host_reserve_tokens"],
                    existing_context_upper_tokens=profile["existing_context_upper_tokens"],
                    estimator=Estimator(id="utf8-upper/v1", fixed_render_tokens=estimator["fixed_render_tokens"],
                                        tool_schema_upper_tokens=estimator["tool_schema_upper_tokens"]),
                    transport_budget_bytes=profile["transport_budget_bytes"],
                    skill_budget_bytes=profile["skill_budget_bytes"], evidence_ref=profile["evidence_ref"]))
        return selected
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise BudgetRefused("target-capacity-unknown", "the packaged qualification data cannot be verified") from exc


def qualify(target: Target, *, now: dt.datetime) -> TargetBudgetProfileV1:
    found = profiles(target, now=now)
    if len(found) != 1:
        raise BudgetRefused("target-capacity-unknown", "this exact host/CLI/model has no current qualified capacity "
                            "profile; no advice was requested. Use inline or smaller material outside managed "
                            "delivery, split the task, or record a qualified route with evidence and expiry")
    return found[0]


def read_render(data: bytes, *, envelope_bytes: int = READ_ENVELOPE_BYTES) -> int:
    from oah.design.task_context import CHUNK_LIMIT, READ_LINE_LIMIT, file_lines

    rendered, reads, slice_bytes, slice_lines = 0, 0, 0, 0
    for number, line in enumerate(file_lines(data), 1):
        size = len(f"{number:6d}→{line}\n".encode("utf-8"))
        rendered += size
        if slice_lines and (slice_lines == READ_LINE_LIMIT or slice_bytes + size > CHUNK_LIMIT):
            reads += 1
            slice_bytes, slice_lines = 0, 0
        if size > CHUNK_LIMIT:
            reads += (size + CHUNK_LIMIT - 1) // CHUNK_LIMIT
        else:
            slice_bytes += size
            slice_lines += 1
    reads += int(bool(slice_lines) or reads == 0)
    return rendered + reads * envelope_bytes


def measure(profile: TargetBudgetProfileV1, text: str, *, deferred: Sequence[bytes] = (), skill_bytes: int = 0,
            design_bytes: int = 0, optional_bytes: int = 0, read_paths: Sequence[str] = ()) -> Measurement:
    prompt = len(text.encode("utf-8"))
    payloads = sum(len(data) for data in deferred)
    path_bound = max((2 * len(json.dumps(path, ensure_ascii=True).encode("ascii")) + 1024 for path in read_paths),
                     default=0)
    rendered = sum(read_render(data, envelope_bytes=max(READ_ENVELOPE_BYTES, path_bound)) for data in deferred)
    estimator = profile["estimator"]
    upper = prompt + rendered + optional_bytes + estimator["fixed_render_tokens"] + estimator["tool_schema_upper_tokens"]
    available = (profile["input_capacity_tokens"] - profile["output_reserve_tokens"] - profile["host_reserve_tokens"]
                 - profile["existing_context_upper_tokens"])
    totals: Measurement = {"prompt_bytes": prompt, "deferred_required_bytes": payloads, "deferred_render_bytes": rendered,
              "optional_reserve_bytes": optional_bytes, "upper_tokens": upper, "available_tokens": available,
              "sha256": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()}
    reason = ("brief" if prompt + rendered + optional_bytes > min(524288, profile["transport_budget_bytes"])
              else "skills" if skill_bytes > min(196608, profile["skill_budget_bytes"])
              else "design" if design_bytes > 262144 else "target-capacity" if upper > available else None)
    if reason is not None:
        raise BudgetRefused(reason, f"measured totals {json.dumps(totals, sort_keys=True)} exceed the qualified "
                            "limits; split the task or remove optional material; no required payload is truncated")
    return totals
