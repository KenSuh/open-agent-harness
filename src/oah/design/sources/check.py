# 출처 확인 흐름 — 등록·제거, 잠금 없이 관측한 뒤 짧은 잠금 안에서 등록 토큰·명세·세대를 다시 확인해 머리를 바꾸는 확인, 공정한 예약 실행, 설계 연결의 단조 공개와 갱신 보고, 파생 제안과 보기 대조를 거친 목록 줄 사실을 맡는다.
"""The source service of one clone: `Sources`.

A check runs the network part with no lock held, storing blobs and records create-only. It then takes the one short
sources lock, re-reads the registry entry and the head, and commits only when the registration token, the spec digest
and the head generation are what it started from; otherwise the observation is `superseded` and changes nothing. The
head replace is the only commit point; an establishment or confirmation also leaves an immutable observation record.

Scheduled runs use the persistent fair queue (`queue`), refreshes publish a design association under a generation CAS
(`association`), and every consumer-facing read (proposals, list-line facts) is derived on read and rechecked
against the caller's current design view. Nothing here writes `.oah/designs`, the wiki, a lease, a packet or a
binding, and no external change saves, accepts or rebinds a design.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import math
import os
import pathlib
import sys
import time
from typing import Callable, Iterable, Mapping, Sequence

from oah.config import ConfigError
from oah.design.model import check_fact, check_id
from oah.design.sources import association, portable, queue
from oah.design.sources import license as license_module
from oah.design.sources.adapters import SOURCE_DEADLINE, Observer, Settings, Transport
from oah.design.sources.model import (MAX_SOURCES, VERIFICATION_POLICY, Head, Observation, Registry, SourceError, SourceSpec,
                                      freshness, iso, new_registration, observation_record, parse_head, parse_iso,
                                      parse_observation, parse_registry, record_word, transition)
from oah.design.sources.state import (ASSESSMENT_LIMIT, DESIGNS_LIMIT, DISMISSALS_LIMIT, HEAD_LIMIT, REGISTRY_LIMIT,
                                      SCHEDULE_LIMIT, State, StateProblem, canonical, digest_of, hex_of)
from oah.design.sources.view import DesignView, ViewUnavailable, read_view
from oah.lockfile import LockTimeout

DEFAULTS = {"auto_check": True, "check_interval_hours": 24.0, "fresh_hours": 24.0, "license_allow": ["MIT"],
            "max_document_bytes": 1 << 20, "timeout_seconds": 30.0}
REASONS = ("start", "resume", "recommend")
POLICY_VERSION = "license-policy-v1"
MAX_DESIGNS = 2000
MAX_DISMISSALS = 10_000
OBSERVATION_CHAIN = 10_000
Fault = Callable[[str], None]


def _no_fault(step: str) -> None:
    return None


def policy_digest_of(license_allow: Sequence[str]) -> str:
    """The identity of a license/verification policy; equal policies give equal digests in any clone."""
    return digest_of({"version": POLICY_VERSION, "license_allow": list(license_allow),
                      "verification_policy": VERIFICATION_POLICY})


def check_settings(section: Mapping) -> dict:
    """The `[sources]` table validated; OAH_CONFIG_INVALID names the key."""
    def bad(key: str, what: str) -> ConfigError:
        return ConfigError("OAH_CONFIG_INVALID", f".oah/config.toml: sources.{key} must be {what}; change it, or "
                           "remove it to use the default")

    settings = dict(section)
    if type(settings["auto_check"]) is not bool:
        raise bad("auto_check", "true or false")
    for key, low, high in (("check_interval_hours", 1, 720), ("fresh_hours", 1, 720), ("timeout_seconds", 1, 600)):
        value = settings[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise bad(key, f"a number from {low} to {high}")
        settings[key] = float(value)
    size = settings["max_document_bytes"]
    if type(size) is not int or not 1024 <= size <= 8 << 20:
        raise bad("max_document_bytes", "a whole number from 1024 to 8388608")
    allow = settings["license_allow"]
    if not isinstance(allow, (list, tuple)) or any(item not in license_module.SUPPORTED for item in allow):
        raise bad("license_allow", f"a list drawn from {', '.join(license_module.SUPPORTED)}")
    settings["license_allow"] = sorted(set(allow))
    return settings


@dataclasses.dataclass(frozen=True)
class CheckResult:
    source: str
    result: str          # unchanged | recovered | changed | unverified | blocked | failed | superseded |
                         # skipped-fresh | skipped-rate-limited | over-budget | disabled | state-corrupt
    snapshot: str | None = None
    previous: str | None = None
    problems: tuple[str, ...] = ()
    http: int | None = None
    notable: bool = False
    freshness: str = "unknown"
    retry_after: str | None = None
    observation: str | None = None
    selection: str | None = None

    def to_json(self) -> dict:
        return dataclasses.asdict(self) | {"problems": list(self.problems)}


@dataclasses.dataclass(frozen=True)
class SourceStatus:
    spec: SourceSpec
    head: Head | None
    freshness: str
    verified: dict | None
    discovered: dict | None
    problem: str | None = None


@dataclasses.dataclass(frozen=True)
class Proposal:
    key: str
    project: str
    uid: str
    design: str
    source: str
    locator: str
    from_snapshot: str
    to_snapshot: str
    kind: str            # new-release | content-changed | release-metadata-changed
    labels: tuple[str, ...]
    status: str          # open | dismissed
    reason: str | None = None
    association: str | None = None
    assessment: Mapping | None = None

    def to_json(self) -> dict:
        from oah.design.sources.assessment import unassessed

        return dataclasses.asdict(self) | {"labels": list(self.labels),
                                           "assessment": dict(self.assessment or unassessed())}


@dataclasses.dataclass(frozen=True)
class Finding:
    kind: str            # unregistered | locator-mismatch | attention | snapshot-missing | state-corrupt
    project: str
    design: str
    source: str
    detail: str

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class RefreshReport:
    reason: str
    project: str
    design: str
    binding: dict | None             # the actual read pair {"current", "accepted"}
    design_state: str
    materials: dict
    sources: tuple[dict, ...]
    association: dict
    results: tuple[CheckResult, ...]
    unvisited: tuple[str, ...]
    unregistered: tuple[str, ...]
    proposals: tuple[Proposal, ...]
    findings: tuple[Finding, ...]
    problems: tuple[str, ...]
    policy: str
    window: dict

    def to_json(self) -> dict:
        return {"version": 1, "kind": "oah-sources-refresh", "reason": self.reason, "project": self.project,
                "design": self.design, "binding": self.binding, "design_state": self.design_state,
                "materials": self.materials, "sources": [dict(item) for item in self.sources],
                "association": self.association, "results": [item.to_json() for item in self.results],
                "unvisited": list(self.unvisited), "unregistered": list(self.unregistered),
                "proposals": [item.to_json() for item in self.proposals],
                "findings": [item.to_json() for item in self.findings], "problems": list(self.problems),
                "policy": self.policy, "observation_window": self.window}


@dataclasses.dataclass(frozen=True)
class ScanReport:
    outcome: str         # disabled | not-due | busy | complete | partial(over-budget|interrupted)
    results: tuple[CheckResult, ...] = ()
    outstanding: tuple[str, ...] = ()
    retry_after: str | None = None

    def to_json(self) -> dict:
        return {"outcome": self.outcome, "results": [item.to_json() for item in self.results],
                "outstanding": list(self.outstanding), "retry_after": self.retry_after}


def _proposal_key(data: Mapping) -> str:
    return hashlib.sha256(canonical(dict(data))).hexdigest()


class Sources:
    def __init__(self, state_dir: os.PathLike | str, *, repo_root: os.PathLike | str | None = None,
                 settings: Mapping | None = None, transport: Transport | None = None,
                 now: Callable[[], dt.datetime] | None = None, clock: Callable[[], float] | None = None) -> None:
        self.state = State(state_dir)
        self.state_dir = pathlib.Path(state_dir)
        self.repo_root = None if repo_root is None else pathlib.Path(repo_root)
        self.settings = check_settings(dict(DEFAULTS, **(settings or {})))
        self.transport = transport or Transport()
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.clock = clock or time.monotonic
        self.observer = Observer(self.state, self.transport, Settings(self.settings["timeout_seconds"],
                                                                      self.settings["max_document_bytes"]),
                                 self.clock)

    def now(self) -> dt.datetime:
        return self._now()

    def policy_digest(self) -> str:
        """The license/verification policy a decision or cached judgement depends on."""
        return policy_digest_of(self.settings["license_allow"])

    # ---- registry ---------------------------------------------------------------------------------------------

    def registry(self) -> tuple[Registry, dict]:
        try:
            data = self.state.read_json("registry.json", REGISTRY_LIMIT)
        except StateProblem as exc:
            raise SourceError("OAH_SOURCES_STATE_CORRUPT", str(exc).partition(": ")[2]) from None
        return parse_registry(data, "sources/registry.json", allow_loopback=self.transport.allow_loopback)

    def register(self, spec: SourceSpec) -> tuple[str, SourceSpec]:
        """('registered' | 'present' | 'updated', the stored spec). Never touches the network."""
        with self.state.lock():
            registry, raw = self.registry()
            current = registry.sources.get(spec.id)
            if current is None and spec.id in raw:
                raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"the registry entry {spec.id!r} cannot be read; move "
                                  "the registry aside or remove that entry by hand")
            if current is not None:
                if current.spec_digest != spec.spec_digest:
                    raise SourceError("OAH_SOURCES_EXISTS", f"{spec.id} already tracks {current.locator} with other "
                                      f"settings; run oah sources remove {spec.id} first, or choose another --id")
                merged = tuple(sorted(set(current.for_designs) | set(spec.for_designs)))
                if merged == current.for_designs:
                    return "present", current
                updated = dataclasses.replace(current, for_designs=merged)
                self._write_registry(registry, raw, {spec.id: updated})
                return "updated", updated
            same = next((item for item in registry.sources.values() if item.spec_digest == spec.spec_digest), None)
            if same is not None:
                return "present", same
            if len(registry.sources) + len(raw) >= MAX_SOURCES:
                raise SourceError("OAH_SOURCES_LIMIT", f"at most {MAX_SOURCES} sources can be registered; remove one "
                                  "you no longer follow")
            stored = dataclasses.replace(spec, registration=new_registration(), registered_at=iso(self.now()))
            self._write_registry(registry, raw, {spec.id: stored})
            return "registered", stored

    def _write_registry(self, registry: Registry, raw: dict, changes: Mapping[str, SourceSpec | None]) -> None:
        sources = dict(registry.sources)
        for key, value in changes.items():
            if value is None:
                sources.pop(key, None)
            else:
                sources[key] = value
        self.state.write_json("registry.json", Registry(sources, registry.problems).to_json(raw))

    def remove(self, source_id: str) -> bool:
        """Forget a registration; its head moves to heads/removed and every snapshot, blob and record stays."""
        with self.state.lock():
            registry, raw = self.registry()
            spec = registry.sources.get(source_id)
            if spec is None:
                if source_id in raw:
                    raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"the registry entry {source_id!r} cannot be read")
                return False
            self._retire_head(source_id)
            self._write_registry(registry, raw, {source_id: None})
            return True

    def _retire_head(self, source_id: str) -> None:
        rel = f"heads/{source_id}.json"
        if not self.state.exists(rel):
            return
        data = self.state.read(rel, HEAD_LIMIT) or b""
        token = hashlib.sha256(data).hexdigest()[:16]
        target = self.state.path(f"heads/removed/{source_id}.{token}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.state.path(rel), target)

    def spec(self, source_id: str) -> SourceSpec:
        registry, raw = self.registry()
        if source_id in registry.sources:
            return registry.sources[source_id]
        if source_id in raw:
            raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"the registry entry {source_id!r} cannot be read")
        raise SourceError("OAH_SOURCES_NOT_FOUND", f"no registered source {source_id!r}; oah sources list shows them")

    # ---- heads, snapshots and records ---------------------------------------------------------------------------

    def head(self, spec: SourceSpec) -> Head | None:
        """This registration's head, None when it has none; a head of an earlier registration counts as none."""
        data = self.state.read_json(f"heads/{spec.id}.json", HEAD_LIMIT)
        if data is None:
            return None
        try:
            head = parse_head(data, f"sources/heads/{spec.id}.json", spec.id)
        except SourceError as exc:
            raise StateProblem("bad-head", f"heads/{spec.id}.json", str(exc).partition(": ")[2]) from None
        if head.registration != spec.registration:
            return None
        if head.spec_digest != spec.spec_digest:
            raise StateProblem("bad-head", f"heads/{spec.id}.json", "names another spec digest than its registration")
        return head

    def status(self, spec: SourceSpec) -> SourceStatus:
        now = self.now()
        try:
            head = self.head(spec)
            verified = None if head is None or head.verified is None else self.snapshot(head.verified.snapshot)
            discovered = (None if head is None or head.discovered is None else self.snapshot(head.discovered.snapshot))
        except StateProblem as exc:
            return SourceStatus(spec, None, "unknown", None, None, str(exc).partition(": ")[2])
        return SourceStatus(spec, head, freshness(head, now, self.settings["fresh_hours"]), verified, discovered)

    def snapshot(self, digest: str) -> dict | None:
        from oah.design.sources import portable

        manifest = self.state.get_record("snapshots", digest)
        if manifest is not None and (manifest.get("kind") != "oah-source-snapshot" or manifest.get("version") != 1
                                     or not isinstance(manifest.get("spec"), dict)
                                     or hex_of(manifest.get("content_key")) is None):
            raise StateProblem("bad-record", f"snapshots/{digest[7:9]}/{digest[7:]}.json",
                               "is not a source snapshot")
        if manifest is not None:
            try:
                portable.uses(manifest)
            except portable.PortableError as exc:
                raise StateProblem("bad-record", f"snapshots/{digest[7:9]}/{digest[7:]}.json",
                                   str(exc).partition(": ")[2]) from None
        return manifest

    def record(self, kind: str, digest: str) -> dict | None:
        """An immutable record (observations, selections, release-observations), re-hashed on read."""
        if kind not in ("observations", "selections", "release-observations"):
            raise ValueError(kind)
        return self.state.get_record(kind, digest)

    def observation(self, digest: str) -> dict:
        data = self.record("observations", digest)
        if data is None:
            raise StateProblem("missing-record", f"observations/{digest[7:]}", "is not stored in this clone")
        try:
            return parse_observation(data, f"observations/{digest[7:]}")
        except SourceError as exc:
            raise StateProblem("bad-record", f"observations/{digest[7:]}", str(exc).partition(": ")[2]) from None

    def observations(self, spec: SourceSpec, limit: int = 50) -> list[dict]:
        """The newest-first observation chain of this registration (bounded; every record stays stored)."""
        head = self.head(spec)
        chain, digest = [], None if head is None else head.observation
        while digest is not None and len(chain) < min(limit, OBSERVATION_CHAIN):
            record = self.observation(digest)
            chain.append(dict(record, digest=digest))
            digest = record["previous"]
        return chain

    def blob(self, hex_digest: str) -> bytes | None:
        try:
            return self.state.get_blob(hex_digest)
        except StateProblem:
            return None

    def verdict(self, manifest: Mapping | None) -> license_module.Verdict:
        return license_module.verdict((manifest or {}).get("license"), self.blob)

    def eligibility(self, manifest: Mapping | None) -> license_module.Eligibility:
        return license_module.eligibility(self.verdict(manifest), self.settings["license_allow"])

    # ---- checks -----------------------------------------------------------------------------------------------

    def _base(self, spec: SourceSpec) -> tuple[Head | None, int | None]:
        head = self.head(spec)
        return head, None if head is None else head.generation

    def check_one(self, spec: SourceSpec, *, fault: Fault = _no_fault, skip_fresh: bool = False,
                  deadline: float | None = None) -> CheckResult:
        started = self.now()
        deadline = self.clock() + SOURCE_DEADLINE if deadline is None else deadline
        try:
            head, generation = self._base(spec)
        except StateProblem:
            return CheckResult(spec.id, "state-corrupt", notable=True)
        current = freshness(head, started, self.settings["fresh_hours"])
        retry = parse_iso(head.retry_after) if head is not None else None
        if retry is not None and started < retry:
            return CheckResult(spec.id, "skipped-rate-limited", problems=("rate-limited",),
                               freshness=current, retry_after=head.retry_after)
        if skip_fresh and current == "fresh":
            return CheckResult(spec.id, "skipped-fresh", freshness="fresh",
                               observation=None if head is None else head.observation)
        observation = self.observer.observe(spec, head, deadline=deadline)
        fault("after-store")
        with self.state.lock():
            registry, _raw = self.registry()
            entry = registry.sources.get(spec.id)
            if entry is None or entry.registration != spec.registration or entry.spec_digest != spec.spec_digest:
                return CheckResult(spec.id, "superseded", notable=True, freshness=current)
            try:
                latest, latest_generation = self._base(spec)
            except StateProblem:
                return CheckResult(spec.id, "state-corrupt", notable=True)
            if latest_generation != generation:
                return CheckResult(spec.id, "superseded", notable=True,
                                   freshness=freshness(latest, self.now(), self.settings["fresh_hours"]))
            start = latest or Head(spec.id, spec.registration, spec.spec_digest)
            if latest is None:
                self._retire_head(spec.id)  # a head of an earlier registration is kept aside, never reused
            now = self.now()
            if deadline is not None and self.clock() >= deadline:
                observation = Observation("failed", code="timeout", selection=observation.selection)
            updated, word = transition(start, observation, now, started)
            outcome = record_word(word)
            if outcome is not None:
                record = observation_record(updated, outcome=outcome, started=iso(started), now=iso(now),
                                            selection=observation.selection)
                updated = dataclasses.replace(updated, observation=self.state.put_record("observations", record))
            if self.clock() >= deadline and observation.kind != "failed":
                observation = Observation("failed", code="timeout", selection=observation.selection)
                updated, word = transition(start, observation, self.now(), started)
            self.state.write_json(f"heads/{spec.id}.json", updated.to_json())
        fault("after-head")
        first_trouble = word in ("failed", "blocked") and (start.outcome != updated.outcome
                                                           or start.problems != updated.problems)
        return CheckResult(spec.id, word, snapshot=observation.snapshot,
                           previous=start.verified.snapshot if start.verified else None,
                           problems=updated.problems, http=observation.http,
                           notable=word in ("changed", "unverified", "recovered") or first_trouble,
                           freshness=freshness(updated, self.now(), self.settings["fresh_hours"]),
                           retry_after=updated.retry_after, observation=updated.observation,
                           selection=observation.selection)

    def check(self, ids: Iterable[str] | None = None, *, deadline_seconds: float | None = None,
              max_sources: int | None = None, skip_fresh: bool = False,
              fault: Fault = _no_fault) -> list[CheckResult]:
        """Check the given sources (all when None), oldest attempt first; the rest of a budget is over-budget."""
        registry, raw = self.registry()
        if ids is None:
            specs = [registry.sources[key] for key in sorted(registry.sources)]
            results = [CheckResult(key, "state-corrupt", notable=True) for key in sorted(raw)]
        else:
            specs, results = [], []
            for key in ids:
                if key in registry.sources:
                    specs.append(registry.sources[key])
                elif key in raw:
                    results.append(CheckResult(key, "state-corrupt", notable=True))
                else:
                    raise SourceError("OAH_SOURCES_NOT_FOUND", f"no registered source {key!r}; oah sources list "
                                      "shows them")
        specs = self._fair_order(specs)
        deadline = None if deadline_seconds is None else self.clock() + deadline_seconds
        for index, spec in enumerate(specs):
            if ((deadline is not None and self.clock() >= deadline)
                    or (max_sources is not None and index >= max_sources)):
                results.extend(CheckResult(item.id, "over-budget", notable=True) for item in specs[index:])
                break
            results.append(self.check_one(spec, fault=fault, skip_fresh=skip_fresh, deadline=deadline))
        return results

    def _fair_order(self, specs: list[SourceSpec]) -> list[SourceSpec]:
        """Never-attempted first, then the oldest attempt; ties by ID."""
        def key(spec: SourceSpec) -> tuple:
            try:
                head = self.head(spec)
            except StateProblem:
                head = None
            return (head is not None and head.attempted_at is not None,
                    "" if head is None or head.attempted_at is None else head.attempted_at, spec.id)
        return sorted(specs, key=key)

    # ---- scheduled run (fair queue) --------------------------------------------------------------------------

    def _schedule(self) -> dict:
        data = self.state.read_json("schedule.json", SCHEDULE_LIMIT * 16)
        if data is None:
            return queue.empty()
        if not queue.well_formed(data):
            raise StateProblem("bad-record", "schedule.json", "is not a version-1 schedule state; oah never resets it")
        return data

    def _write_schedule(self, data: Mapping) -> None:
        self.state.write_json("schedule.json", data)

    def schedule_state(self) -> dict:
        return self._schedule()

    def scheduled(self, *, budget: float = queue.RUN_BUDGET, scheduled: bool = True,
                  fault: Fault = _no_fault) -> ScanReport:
        """One bounded fair pass; never writes the repository, the kit cache or installed skills."""
        started = self.now()
        if not self.settings["auto_check"]:
            self.record_command("disabled", started, {}, scheduled)
            return ScanReport("disabled")
        deadline = self.clock() + budget
        with self.state.lock():
            data = self._schedule()
            registry, _raw = self.registry()
            run, word = queue.reserve_run(data, started, budget)
            if run is None:
                return ScanReport("busy")
            cycle = data["scan"]["cycle"]
            if cycle is None or not cycle["pending"]:
                if not queue.due(data, started, self.settings["check_interval_hours"]):
                    data["scan"]["reservation"] = None
                    self._record_command(data, "not-due", started, {}, scheduled)
                    return ScanReport("not-due")
                queue.start_cycle(data, registry.sources, started)
            queue.reconcile(data, registry.sources)
            self._write_schedule(data)
        results: list[CheckResult] = []
        lost = False
        while self.clock() < deadline:
            with self.state.lock():
                data = self._schedule()
                if not queue.owns(data, run):
                    lost = True
                    break
                registry, _raw = self.registry()
                queue.reconcile(data, registry.sources)
                flight = queue.next_source(data, self.now())
                self._write_schedule(data)
            if flight is None:
                break
            spec = registry.sources.get(flight["id"])
            result = (CheckResult(flight["id"], "superseded") if spec is None or spec.registration
                      != flight["registration"] else self.check_one(spec, fault=fault, skip_fresh=True,
                                                                    deadline=deadline))
            results.append(result)
            outcome = {"skipped-fresh": "reused"}.get(result.result, result.result)
            if result.result == "failed" and "rate-limited" in result.problems:
                outcome = "rate-limited"
            with self.state.lock():
                data = self._schedule()
                if not queue.owns(data, run):
                    lost = True
                    break
                queue.finish_source(data, flight, outcome, self.now(), result.retry_after)
                self._write_schedule(data)
            fault("after-source")
        with self.state.lock():
            data = self._schedule()
            final = queue.close_run(data, run, self.now()) if not lost else "partial(interrupted)"
            if lost:
                data["scan"]["last_command_at"] = iso(self.now())
            self._write_schedule(data)
            pending = queue.outstanding(data)
        retry = min((entry["not_before"] for entry in pending if entry.get("not_before")), default=None)
        counts: dict[str, int] = {}
        for item in results:
            counts[item.result] = counts.get(item.result, 0) + 1
        self.record_command(final, started, counts, scheduled)
        return ScanReport(final, tuple(results), tuple(entry["id"] for entry in pending), retry)

    def record_command(self, outcome: str, started: dt.datetime, counts: Mapping, scheduled: bool) -> None:
        """source_command_run and, for --scheduled, the cooperative record; neither proves an OS timer fired."""
        try:
            with self.state.lock():
                self._record_command(self._schedule(), outcome, started, counts, scheduled)
        except (StateProblem, LockTimeout):
            pass

    def _record_command(self, data: dict[str, object], outcome: str, started: dt.datetime, counts: Mapping[str, int],
                        scheduled: bool) -> None:
        from oah import __version__

        data["source_command_run"] = {"command": "sources check" + (" --scheduled" if scheduled else ""),
                                      "started_at": iso(started), "ended_at": iso(self.now()),
                                      "outcome": outcome, "counts": dict(sorted(counts.items())),
                                      "executable": sys.executable,
                                      "root": None if self.repo_root is None else str(self.repo_root),
                                      "version": __version__}
        if scheduled:
            data["cooperative_scheduled_run"] = {"at": iso(started), "flag": "--scheduled", "job": None,
                                                 "definition_digest": None}
        self._write_schedule(data)

    # ---- design associations ---------------------------------------------------------------------------------

    def _designs(self) -> dict:
        data = self.state.read_json("designs.json", DESIGNS_LIMIT)
        if data is None:
            return {}
        if (not isinstance(data, dict) or data.get("version") != 1 or data.get("kind") != "oah-sources-designs"
                or not isinstance(data.get("designs"), dict)):
            raise StateProblem("bad-record", "designs.json", "is not a version-1 design association cache")
        return data["designs"]

    def association_entry(self, uid: str, design: str) -> dict | None:
        entry = self._designs().get(association.key_of(uid, design))
        return dict(entry) if association.well_formed(entry) else None

    def _source_reads(self, refs: Sequence[Mapping]) -> list[dict]:
        registry, _raw = self.registry()
        reads = []
        for ref in refs:
            spec = registry.sources.get(ref["source"])
            verified = None
            if spec is not None:
                try:
                    head = self.head(spec)
                except StateProblem:
                    head = None
                verified = None if head is None or head.verified is None else head.verified.snapshot
            reads.append({"id": ref["source"], "locator": ref.get("locator"), "spec_digest": ref.get("spec_digest"),
                          "snapshot": ref.get("snapshot"),
                          "registration": None if spec is None else spec.registration,
                          "registered_spec": None if spec is None else spec.spec_digest, "verified": verified,
                          "roles": list(ref.get("roles") or [])})
        return reads

    def publish_association(self, view: DesignView, refs: Sequence[Mapping], *, captured_generation: int | None,
                            reader: Callable[[], DesignView] | None = None) -> dict:
        """CAS publication under the sources lock: {"status", "key", "generation", "digest"}."""
        from oah.design.sources.view import design_basis

        key = association.key_of(view.uid, view.design)
        if view.problems():
            return {"status": "unknown", "key": key, "generation": None, "digest": None,
                    "problems": view.problems()}
        basis = {"current": design_basis(view, "current"), "accepted": design_basis(view, "accepted")}
        with self.state.lock():
            try:
                designs = self._designs()
            except StateProblem:
                return {"status": "unknown", "key": key, "generation": None, "digest": None}
            existing = designs.get(key)
            existing = existing if association.well_formed(existing) else None
            if reader is not None:
                try:
                    latest = reader()
                except ViewUnavailable:
                    return {"status": "unknown", "key": key, "generation": None, "digest": None}
                if latest.problems():
                    return {"status": "unknown", "key": key, "generation": None, "digest": None,
                            "problems": latest.problems()}
                if latest.identity() != view.identity() or latest.observation() != view.observation() or any(
                        latest.materials(role) != view.materials(role) for role in ("current", "accepted")):
                    return {"status": "superseded", "key": key, "generation": None, "digest": None}
            generation = 0 if existing is None else existing["generation"]
            candidate = association.build(view, self._source_reads(refs), input_basis=basis,
                                          generation=generation + 1, observed_at=iso(self.now()),
                                          policy=self.policy_digest())
            decision = association.decide(existing, candidate)
            if decision in ("superseded", "same"):
                current = existing if existing is not None else candidate
                return {"status": "unchanged" if decision == "same" else "superseded", "key": key,
                        "generation": current["generation"], "digest": association.digest(current),
                        "moved": captured_generation is not None and generation != captured_generation}
            entry = association.with_conflict(existing, candidate, iso(self.now())) if decision == "invalid" \
                else candidate
            designs[key] = entry
            if len(designs) > MAX_DESIGNS:
                return {"status": "unknown", "key": key, "generation": None, "digest": None, "reason": "limit"}
            self.state.write_json("designs.json", {"version": 1, "kind": "oah-sources-designs", "designs": designs})
            return {"status": "invalid" if decision == "invalid" else "published", "key": key,
                    "generation": entry["generation"], "digest": association.digest(entry)}

    def refresh_for(self, store, project: str, design: str, *, reason: str, deadline_seconds: float = 120.0,
                    max_sources: int = 20, fault: Fault = _no_fault,
                    reader: Callable[[], DesignView] | None = None) -> RefreshReport:
        """The start/resume/recommend seam: refresh the sources a design uses, quietly when nothing changed.

        Reads A lock-free, checks sources with no lock, rereads A, then publishes the association under the CAS.
        Never writes the design or takes the wiki lock; network failures are results, not exceptions. A design that
        cannot be read is `unknown(<code>)` and never publishes an empty association over a good one."""
        from oah.design.materials import MaterialsError, refs_of

        if reason not in REASONS:
            raise ValueError(f"reason must be one of {', '.join(REASONS)}")
        project, design = check_id(project), check_id(design, "design")
        reader = reader or (lambda: read_view(store, project, design))
        window_start = iso(self.now())
        problems: list[str] = []
        try:
            view = reader()
        except ViewUnavailable as exc:
            return RefreshReport(reason, project, design, None, f"unknown({exc.code})", {}, (), {"status": "unknown"},
                                 (), (), (), (), (), (), self.policy_digest(),
                                 {"started_at": window_start, "ended_at": iso(self.now())})
        captured = self.association_entry(view.uid, view.design)
        captured_generation = None if captured is None else captured["generation"]
        refs: list[dict] = []
        for role, snapshot in (("current", view.current), ("accepted", view.accepted)):
            text = None if snapshot is None else snapshot.texts.get("materials")
            if text is not None:
                try:
                    for ref in refs_of(text):
                        existing = next((item for item in refs if all(item.get(key) == value
                                                                      for key, value in ref.items())), None)
                        if existing is None:
                            refs.append(dict(ref, roles=[role]))
                        else:
                            existing["roles"].append(role)
                except MaterialsError:
                    problems.append(f"materials-invalid:{role}")
        registry, _raw = self.registry()
        wanted = {ref["source"] for ref in refs}
        wanted |= {key for key, spec in registry.sources.items() if f"{project}/{design}" in spec.for_designs}
        unregistered = tuple(sorted(key for key in wanted if key not in registry.sources))
        selected = sorted(key for key in wanted if key in registry.sources)
        if not self.settings["auto_check"]:
            results = [CheckResult(key, "disabled") for key in selected]
        else:
            results = self.check(selected, deadline_seconds=deadline_seconds, max_sources=max_sources,
                                 skip_fresh=True, fault=fault)
        unvisited = tuple(item.source for item in results if item.result == "over-budget")
        try:
            published = ({"status": "unknown", "reason": "materials-invalid"} if problems else
                         self.publish_association(view, refs, captured_generation=captured_generation, reader=reader))
        except (StateProblem, LockTimeout):
            published = {"status": "unknown", "key": association.key_of(view.uid, view.design)}
            problems.append("designs-cache-unavailable")
        proposals, findings = ([], [])
        if published["status"] in ("published", "unchanged", "invalid"):
            proposals, findings = self.proposals(design=(view.uid, view.design))
        binding = {**view.identity(), "authority_observation": view.observation()}
        design_state = "unknown(authority-or-association)" if view.problems() or published["status"] == "unknown" else view.freshness
        return RefreshReport(reason, project, design, binding, design_state,
                             {"current": view.materials("current"), "accepted": view.materials("accepted")},
                             tuple(self._source_reads(refs)), published, tuple(results), unvisited, unregistered,
                             tuple(proposals), tuple(findings), tuple(problems), self.policy_digest(),
                             {"started_at": window_start, "ended_at": iso(self.now())})

    def matches_view(self, report: Mapping, view: DesignView) -> dict:
        """The consumer's recheck: the report or association against the current view, source reads and policy."""
        reads = {item["id"]: item for item in self._source_reads(
            [{"source": item.get("id"), "locator": item.get("locator"), "spec_digest": item.get("spec_digest"),
              "snapshot": item.get("snapshot")} for item in report.get("sources") or []])}
        return association.matches_view(report, view, reads, self.policy_digest())

    # ---- proposals --------------------------------------------------------------------------------------------

    def _dismissals(self) -> dict:
        data = self.state.read_json("dismissals.json", DISMISSALS_LIMIT)
        if data is None:
            return {}
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("dismissed"), dict):
            raise StateProblem("bad-record", "dismissals.json", "is not a version-1 dismissal list")
        return data["dismissed"]

    def dismiss(self, key: str, reason: str) -> None:
        reason = " ".join(str(reason).split())
        if not reason or len(reason) > 200:
            raise SourceError("OAH_SOURCES_USAGE", "give --reason as one line of up to 200 characters")
        with self.state.lock():
            dismissed = self._dismissals()
            if len(dismissed) >= MAX_DISMISSALS and key not in dismissed:
                raise SourceError("OAH_SOURCES_LIMIT", f"at most {MAX_DISMISSALS} dismissals are kept")
            dismissed[key] = {"reason": reason, "at": iso(self.now())}
            self.state.write_json("dismissals.json", {"version": 1, "dismissed": dismissed})

    def _kind(self, track: str, before: dict | None, after: dict) -> str:
        if track != "release":
            return "content-changed"
        old = ((before or {}).get("observed") or {}).get("release") or {}
        new = (after.get("observed") or {}).get("release") or {}
        same_content = (old.get("tag") == new.get("tag") and old.get("commit") == new.get("commit")
                        and (before or {}).get("files") == after.get("files")
                        and (before or {}).get("license") == after.get("license"))
        if same_content and old.get("metadata_digest") != new.get("metadata_digest"):
            return "release-metadata-changed"
        return "new-release"

    def _labels(self, track: str, before: dict | None, after: dict) -> list[str]:
        from oah.design.sources.release import version_of

        labels = []
        old = ((before or {}).get("observed") or {}).get("release") or {}
        new = (after.get("observed") or {}).get("release") or {}
        if new.get("tag"):
            prefix = (after.get("spec") or {}).get("tag_prefix") or ""
            a, b = version_of(old.get("tag") or "", prefix), version_of(new["tag"], prefix)
            if a is not None and b is not None:
                labels.append("semver-major" if b[0] != a[0] else "semver-minor" if b[1] != a[1]
                              else "semver-patch" if b[2] != a[2] else "semver-pre")
            if new.get("prerelease"):
                labels.append("prerelease")
        if before is not None:
            was, now_is = self.eligibility(before).status, self.eligibility(after).status
            if was != now_is:
                labels.append(f"license {was} -> {now_is}")
        return labels

    def proposals(self, *, design: tuple[str, str] | None = None) -> tuple[list[Proposal], list[Finding]]:
        """(proposals, findings) derived now from associations x registry x heads x dismissals."""
        proposals: list[Proposal] = []
        findings: list[Finding] = []
        try:
            designs = self._designs()
            dismissed = self._dismissals()
        except StateProblem as exc:
            return [], [Finding("state-corrupt", "", "", "", str(exc).partition(": ")[2])]
        registry, _raw = self.registry()
        for key in sorted(designs):
            entry = designs[key]
            if not association.well_formed(entry):
                findings.append(Finding("state-corrupt", "", "", "", f"designs.json entry {key[:80]!r} is malformed"))
                continue
            project, uid, name = str(entry.get("project")), str(entry.get("project_uid")), str(entry.get("design"))
            if design is not None and (uid, name) != design:
                continue
            assoc = association.digest(entry)
            for ref in entry["sources"]:
                source = str(ref.get("id"))
                spec = registry.sources.get(source)
                if spec is None:
                    findings.append(Finding("unregistered", project, name, source, "the design names a source this "
                                            "clone has not registered; oah sources register adds it"))
                    continue
                if spec.locator != ref.get("locator") or spec.spec_digest != ref.get("spec_digest"):
                    findings.append(Finding("locator-mismatch", project, name, source, f"{source} here tracks "
                                            f"{spec.locator} with other settings than the design recorded"))
                    continue
                status = self.status(spec)
                if status.problem is not None:
                    findings.append(Finding("state-corrupt", project, name, source, status.problem))
                    continue
                head = status.head
                if head is not None and head.problems:
                    findings.append(Finding("attention", project, name, source, ", ".join(head.problems)))
                if head is None or head.verified is None or status.verified is None:
                    continue
                target = head.verified.snapshot
                if target == ref.get("snapshot"):
                    continue
                try:
                    before = self.snapshot(str(ref.get("snapshot")))
                except StateProblem:
                    before = None
                if before is None:
                    findings.append(Finding("snapshot-missing", project, name, source, "the snapshot the design "
                                            "references is not stored in this clone"))
                elif before.get("content_key") == status.verified.get("content_key"):
                    continue
                kind = self._kind(spec.track, before, status.verified)
                fields = {"uid": uid, "design": name, "source": source, "locator": spec.locator,
                          "from": ref.get("snapshot"), "to": target, "kind": kind}
                proposal_key = _proposal_key(fields)
                note = dismissed.get(proposal_key)
                proposals.append(Proposal(proposal_key, project, uid, name, source, spec.locator,
                                          str(ref.get("snapshot")), target, kind,
                                          tuple(self._labels(spec.track, before, status.verified)),
                                          "dismissed" if note else "open",
                                          note.get("reason") if isinstance(note, dict) else None, assoc))
        return proposals, findings

    # ---- list-line facts (the design store's facts= seam; the host wires it) ------------------------------------

    def list_facts(self, reader: Callable[[str, str], DesignView]) -> Callable[[str, str], list[str]]:
        """A facts provider for `open_store(..., facts=...)`. `reader(project, design)` must be a local, read-only
        current-view reader supplied by the host; nothing here refreshes, calls the network or takes the sources
        lock. A cached association that no longer matches the view gives no source facts."""
        def provider(project: str, design: str) -> list[str]:
            try:
                view = reader(project, design)
                entry = self._designs().get(association.key_of(view.uid, view.design))
                if not association.well_formed(entry):
                    return []
                report = {"binding": entry.get("binding"),
                    "materials": entry["materials"], "sources": entry["sources"], "policy": entry.get("policy"),
                    "conflict": entry.get("conflict")}
                if not self.matches_view(report, view)["matches"]:
                    return []
                registry, _raw = self.registry()
                counts: dict[str, int] = {}
                for source_id in sorted({str(ref.get("id")) for ref in entry["sources"]}):
                    spec = registry.sources.get(source_id)
                    word = "unregistered" if spec is None else self.status(spec).freshness
                    counts[word] = counts.get(word, 0) + 1
                open_count = sum(1 for item in self.proposals(design=(view.uid, view.design))[0]
                                 if item.status == "open")
            except Exception:
                return []
            facts = []
            total = sum(counts.values())
            if total and counts.get("fresh") == total:
                facts.append(f"sources {total} fresh")
            for word, label in (("unavailable", "check failed"), ("unverified", "unverified"), ("stale", "stale"),
                                ("unregistered", "not registered here"), ("unknown", "not checked yet")):
                if counts.get(word):
                    facts.append(f"{counts[word]} source{'s' if counts[word] > 1 else ''} {label}")
            if open_count:
                facts.append(f"{open_count} source change{'s' if open_count > 1 else ''} to review")
            return [check_fact(item) for item in facts]
        return provider

    @staticmethod
    def _accepted_ref(entry: Mapping, view: DesignView) -> dict | None:
        accepted = entry.get("accepted")
        if accepted is None or view.accepted is None:
            return None if accepted is None and view.accepted is None else {}
        if accepted["revision"] != view.accepted.ref.revision or accepted["digest"] != view.accepted.ref.digest:
            return {}
        return view.accepted.ref.to_json()

    # ---- offline diff -----------------------------------------------------------------------------------------

    def diff(self, source_id: str, from_snapshot: str, to_snapshot: str, *,
             evidence: Sequence[Mapping] = ()) -> dict:
        """DiffReportV1 between two immutable snapshots of one source; never the network, never a save.

        Each side comes from this clone's rehashed store or, when it is not stored here, from a verified portable
        envelope in `evidence` (for example the ones a saved materials part carries)."""
        from oah.design.sources import diff as diff_module

        if hex_of(from_snapshot) is None or hex_of(to_snapshot) is None:
            raise SourceError("OAH_SOURCES_USAGE", "--from and --to are snapshot digests (sha256:...)")
        envelopes: dict[str, portable.Evidence] = {}
        for item in evidence:
            try:
                verified = portable.verify(item)
            except portable.PortableError as exc:
                raise SourceError(exc.code, str(exc).partition(": ")[2]) from None
            envelopes[verified.snapshot] = verified
        sides = []
        for digest in (from_snapshot, to_snapshot):
            manifest = self.snapshot(digest)
            if manifest is None and digest in envelopes:
                manifest = envelopes[digest].manifest
            sides.append(manifest)
        before, after = sides
        identity = {"id": source_id, "from": from_snapshot, "to": to_snapshot}
        if before is None or after is None:
            return diff_module.unavailable(identity, from_snapshot if before is None else to_snapshot)
        old_spec, new_spec = before.get("spec") or {}, after.get("spec") or {}
        pair = (new_spec.get("provider"), new_spec.get("locator"))
        if (old_spec.get("provider"), old_spec.get("locator")) != pair:
            raise SourceError("OAH_SOURCES_SOURCE_MISMATCH", "the two snapshots come from different sources; diff "
                              "compares snapshots of one provider and locator")
        chain: list[dict] = []
        try:
            spec = self.spec(source_id)
        except SourceError as exc:
            if exc.code != "OAH_SOURCES_NOT_FOUND":
                raise
            spec = None
        if spec is not None:
            if (spec.provider, spec.locator) != pair:
                raise SourceError("OAH_SOURCES_SOURCE_MISMATCH", f"source {source_id} is registered for another "
                                  "provider or locator than these snapshots; an ID that was reused is not a comparison")
            chain = self.observations(spec, limit=OBSERVATION_CHAIN)
        times = diff_module.times_of(chain, (from_snapshot, to_snapshot))
        for digest, verified in envelopes.items():
            if digest in times and times[digest] is None and verified.observation is not None:
                times[digest] = dict(verified.times() or {}, origin="portable-evidence")

        def read(sha: str) -> bytes | None:
            data = self.blob(sha)
            if data is None:
                for verified in envelopes.values():
                    data = verified.read(sha)
                    if data is not None:
                        break
            return data

        identity.update(provider=pair[0], locator=pair[1])
        return diff_module.compare(identity, before, after, read=read, times=times,
                                   eligibility=lambda manifest: license_module.eligibility(
                                       license_module.verdict(manifest.get("license"), read),
                                       self.settings["license_allow"]).status,
                                   policy=self.policy_digest())

    def portable(self, spec: SourceSpec, snapshot: str) -> dict:
        """PortableEvidenceV1 for a stored snapshot of this registration, with the latest observation of it."""
        manifest = self.snapshot(snapshot)
        if manifest is None:
            raise SourceError("OAH_SOURCES_EVIDENCE_MISSING", f"snapshot {snapshot} is not stored in this clone")
        if manifest.get("spec_digest") != spec.spec_digest:
            raise SourceError("OAH_SOURCES_SOURCE_MISMATCH", f"snapshot {snapshot} belongs to another registration "
                              f"of {spec.id}")
        observation = next(({key: value for key, value in record.items() if key != "digest"}
                            for record in self.observations(spec, limit=OBSERVATION_CHAIN)
                            if record["snapshot"] == snapshot), None)
        release = (manifest.get("observed") or {}).get("release") or {}
        release_record = None
        selection_record = None
        if release:
            release_record = self.record("release-observations", release.get("release_observation") or "")
            selection_record = self.record("selections", release.get("selection") or "")
            if release_record is None or selection_record is None:
                raise SourceError("OAH_SOURCES_EVIDENCE_MISSING", f"the release observation of {snapshot} is not "
                                  "stored in this clone")
        try:
            return portable.build(source_id=spec.id, spec=spec.verification(), snapshot=snapshot, manifest=manifest,
                                  observation=observation, release_observation=release_record,
                                  selection=selection_record,
                                  registration=spec.registration, read=self.blob)
        except portable.PortableError as exc:
            raise SourceError(exc.code, str(exc).partition(": ")[2]) from None

    # ---- assessments ------------------------------------------------------------------------------------------

    def store_assessment(self, record: Mapping) -> str:
        return self.state.put_record("assessments", record)

    def assessment(self, digest: str) -> dict | None:
        return self.state.get_record("assessments", digest, ASSESSMENT_LIMIT)
