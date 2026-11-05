# 릴리스 선택 — 관측한 태그 광고 전체를 SemVer 또는 게시 순서로 정렬하고, 태그·ID 주소의 메타데이터를 예산 안에서 두 번 읽어 대조해 '관측상 최신' 선택과 변경 가능한 릴리스 메타데이터 관측을 만든다.
"""Release selection over the observed tag universe, and the identity of mutable release metadata.

The candidate universe is the complete tag advertisement (R0), filtered by the literal tag prefix and the channel.
Offset pages never choose a winner. In SemVer order every candidate is ranked locally, then the highest are visited
by exact `/releases/tags/{tag}` reads until the first eligible public release; each higher candidate needs a
determinate exclusion (404, draft, channel). Published order reads every matching candidate, so it fits only up to 20
tags; a small repository may instead use the short-list alternative (two complete short first pages). The selected
release is read again by ID; after content verification every visited read and the by-ID read are repeated (M1) and
the advertisement is read again (R1). Any relevant difference is `changed-during-check`; budget, rate or schema
gaps are `selection-incomplete` or `unavailable`. A success means latest-observed within a disclosed window, never
an atomic snapshot of every live GitHub release.

`ReleaseObservationV1` gives the selected release a metadata digest (times, flags, URL, complete notes up to 64 KiB)
separate from tag, commit and content, so changed notes at an unchanged tag and commit are new evidence.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
import time
import urllib.parse
from typing import Callable, Mapping

from oah.design.sources import gitref, web
from oah.design.sources.model import SELECTION_POLICY, SourceSpec
from oah.design.sources.state import StateProblem, canonical, digest_of, loads

CANDIDATE_LIMIT = 20
SHORT_LIST_LIMIT = 99
REQUEST_LIMIT = 42
RESPONSE_LIMIT = 8 << 20
TOTAL_LIMIT = 32 << 20
NOTES_LIMIT = 64 * 1024
PER_PAGE = 100
TAG_LIMIT = 255
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
OUTCOMES = ("latest-observed", "no-matching-release", "selection-incomplete", "unverified", "unavailable",
            "superseded", "changed-during-check")
RESIDUAL = ("ref advertisements and REST reads are separate observations over an interval; changes that revert "
            "between reads, hidden drafts and changes after the last read are not observable")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_NUM = re.compile(r"0|[1-9][0-9]{0,17}")
_IDENT = re.compile(r"[0-9A-Za-z-]+")


# SemVer -----------------------------------------------------------------------------------------------------------


def parse_semver(text: str) -> tuple | None:
    """(major, minor, patch, prerelease identifiers or None) for strict SemVer 2.0.0; build metadata is ignored."""
    core, plus, build = text.partition("+")
    if plus and (not build or not all(_IDENT.fullmatch(part) for part in build.split("."))):
        return None
    core, dash, pre = core.partition("-")
    numbers = core.split(".")
    if len(numbers) != 3 or not all(_NUM.fullmatch(part) for part in numbers):
        return None
    identifiers = None
    if dash:
        identifiers = []
        for part in pre.split("."):
            if not _IDENT.fullmatch(part):
                return None
            if part.isdigit():
                if not _NUM.fullmatch(part):
                    return None
                identifiers.append((0, int(part), ""))
            else:
                identifiers.append((1, 0, part))
        identifiers = tuple(identifiers)
    return (int(numbers[0]), int(numbers[1]), int(numbers[2]), identifiers)


def version_of(tag: str, prefix: str) -> tuple | None:
    if not tag.startswith(prefix):
        return None
    rest = tag[len(prefix):]
    if rest.startswith("v"):
        rest = rest[1:]
    return parse_semver(rest)


def precedence(version: tuple) -> tuple:
    major, minor, patch, pre = version
    return (major, minor, patch, 1, ()) if pre is None else (major, minor, patch, 0, pre)


def semver_order(tags: list[str], prefix: str) -> list[str]:
    """Highest precedence first; equal precedence by exact tag bytes ascending."""
    by_bytes = sorted(tags, key=lambda tag: tag.encode("utf-8"))
    return sorted(by_bytes, key=lambda tag: precedence(version_of(tag, prefix)), reverse=True)


# Release metadata ---------------------------------------------------------------------------------------------------


def release_fields(data: object) -> tuple[dict, bytes | None] | None:
    """The relevant fields of one REST release object and its complete notes bytes; None when it is not one."""
    if not isinstance(data, dict):
        return None
    ident, tag, url = data.get("id"), data.get("tag_name"), data.get("html_url")
    draft, pre, body = data.get("draft"), data.get("prerelease"), data.get("body")
    created, published, updated = data.get("created_at"), data.get("published_at"), data.get("updated_at")
    if (type(ident) is not int or not 0 < ident < 10**15 or not isinstance(tag, str) or not tag
            or len(tag) > TAG_LIMIT or any(ord(c) < 33 or ord(c) == 127 for c in tag)
            or type(draft) is not bool or type(pre) is not bool
            or not isinstance(created, str) or not _TIME.fullmatch(created)
            or not (published is None or isinstance(published, str) and _TIME.fullmatch(published))
            or not (updated is None or isinstance(updated, str) and _TIME.fullmatch(updated))
            or not isinstance(url, str) or not url.startswith("https://") or len(url) > 2000
            or any(ord(c) < 33 or ord(c) == 127 for c in url) or not (body is None or isinstance(body, str))):
        return None
    notes = None
    if body is not None:
        try:
            notes = body.encode("utf-8")
        except UnicodeEncodeError:
            return None
    fields = {"id": ident, "tag": tag, "draft": draft, "prerelease": pre, "created_at": created,
              "published_at": published, "updated_at": updated, "html_url": url,
              "body": "null" if notes is None else "present", "notes_bytes": len(notes or b""),
              "notes_sha256": None if notes is None else hashlib.sha256(notes).hexdigest()}
    return fields, notes


def metadata_digest(fields: Mapping) -> str:
    return digest_of(dict(fields))


class Incomplete(Exception):
    """The selection cannot be established from these observations (budget, schema, unknown facts)."""

    def __init__(self, reason: str, problem: str = "selection-incomplete") -> None:
        self.reason, self.problem = reason, problem
        super().__init__(reason)


class Changed(Exception):
    """Two observations of the same relevant fact differ."""


class Budget:
    def __init__(self, getter: Callable, policy: Callable[[int, float], web.WebPolicy], deadline: float,
                 timeout: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.getter, self.make_policy, self.deadline, self.timeout, self.clock = (getter, policy, deadline, timeout,
                                                                                    clock)
        self.requests = 0
        self.bytes = 0

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def get(self, url: str) -> web.WebResult:
        left = TOTAL_LIMIT - self.bytes
        if self.requests >= REQUEST_LIMIT or left <= 0 or self.remaining() <= 0:
            raise Incomplete("budget-exhausted")
        self.requests += 1
        try:
            result = self.getter(url, policy=self.make_policy(min(RESPONSE_LIMIT, left),
                                                              min(self.timeout, self.remaining())),
                                 headers=API_HEADERS)
        except web.WebFailure as exc:
            if exc.code == "oversize" and left < RESPONSE_LIMIT:
                raise Incomplete("budget-exhausted") from None
            if exc.code == "timeout" and self.remaining() <= 0:
                raise Incomplete("budget-exhausted") from None
            raise
        self.bytes += len(result.body)
        return result


@dataclasses.dataclass
class Visit:
    tag: str
    sort_key: str
    tag_object: str
    peeled: str | None
    m0: dict | None = None
    m1: dict | None = None
    fields: dict | None = None
    notes: bytes | None = None

    @property
    def eligibility(self) -> str:
        return "unvisited" if self.m0 is None else self.m0["eligibility"]


class Selector:
    """One release selection attempt. `observe()` reads R0, M0 and the selected by-ID read; the caller verifies the
    content; `confirm()` repeats M1 and R1. `record()` is the ReleaseSelectionV2 evidence of what happened."""

    def __init__(self, spec: SourceSpec, *, api_base: str, git_url: str, getter: Callable,
                 policy: Callable[[int, float], web.WebPolicy], deadline: float, timeout: float,
                 put_blob: Callable[[bytes], str], clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], str] = gitref._now) -> None:
        self.spec, self.api_base, self.git_url = spec, api_base.rstrip("/"), git_url
        self.budget = Budget(getter, policy, deadline, timeout, clock)
        self.put_blob, self.clock, self.wall = put_blob, clock, wall
        owner, repo = spec.locator.rstrip("/").split("/")[-2:]
        self.repo_api = f"{self.api_base}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
        self.started_at, self.started = wall(), clock()
        self.ref_rounds: list[dict] = []
        self.relevant: dict[str, dict] = {}
        self.exclusions: list[dict] = []
        self.order = ""
        self.universe_kind = "tags"
        self.visits: list[Visit] = []
        self.listing: list[dict] | None = None
        self.selected: Visit | None = None
        self.by_id: list[dict] = []
        self.outcome = "selection-incomplete"
        self.reason = ""
        self.complete = False
        self.content: dict | None = None
        self.release_observation: str | None = None

    # ---- reads ---------------------------------------------------------------------------------------------

    def _advertise(self, round_name: str) -> dict[str, dict]:
        timeout = min(gitref.TIMEOUT, self.budget.remaining())
        advertisement = gitref.ls_remote(self.git_url, tags=True, timeout=timeout)
        relevant = gitref.tag_map(advertisement, self.spec.tag_prefix)
        relevant_bytes = canonical(relevant)
        blob = self.put_blob(relevant_bytes) if len(relevant_bytes) <= (8 << 20) else None
        if blob is None:
            raise Incomplete("advertisement-too-large")
        self.ref_rounds.append({"round": round_name, "started_at": advertisement.started_at,
                                "ended_at": advertisement.ended_at, "bytes": len(advertisement.raw),
                                "rows": advertisement.rows, "raw_sha256": advertisement.raw_sha256,
                                "relevant_map_sha256": hashlib.sha256(relevant_bytes).hexdigest(), "blob": blob})
        return relevant

    def _metadata(self, url: str, expected_tag: str | None) -> tuple[dict, dict | None, bytes | None]:
        """(observation, fields, notes). A 404 is a determinate observation; other failures raise."""
        try:
            result = self.budget.get(url)
        except web.WebFailure as exc:
            if exc.code == "not-found":
                return {"status": 404, "identity": digest_of({"status": 404}), "response_sha256": None}, None, None
            raise
        try:
            data = loads(result.body, "a GitHub release")
        except StateProblem:
            raise Incomplete("release-metadata-invalid") from None
        parsed = release_fields(data)
        if parsed is None or (expected_tag is not None and parsed[0]["tag"] != expected_tag):
            raise Incomplete("release-metadata-invalid")
        fields, notes = parsed
        return ({"status": 200, "identity": digest_of({"status": 200, "fields": fields}),
                 "response_sha256": hashlib.sha256(result.body).hexdigest()}, fields, notes)

    def _eligibility(self, fields: dict | None) -> tuple[str, str | None]:
        if fields is None:
            return "excluded", "no-public-release"
        if fields["draft"]:
            return "excluded", "draft"
        if self.spec.channel == "stable" and fields["prerelease"]:
            return "excluded", "channel"
        return "eligible", None

    def _visit(self, visit: Visit) -> None:
        url = f"{self.repo_api}/releases/tags/{urllib.parse.quote(visit.tag, safe='')}"
        observation, fields, notes = self._metadata(url, visit.tag)
        eligibility, reason = self._eligibility(fields)
        observation.update(eligibility=eligibility, reason=reason)
        visit.m0, visit.fields, visit.notes = observation, fields, notes

    def _read_by_id(self, visit: Visit) -> None:
        observation, fields, notes = self._metadata(f"{self.repo_api}/releases/{visit.fields['id']}", visit.tag)
        if fields is None or fields != visit.fields or notes != visit.notes:
            raise Changed()
        self.by_id.append(observation)

    # ---- phases ------------------------------------------------------------------------------------------

    def observe(self, prior_tag: str | None) -> None:
        """R0, ordering, candidate visits and the selected release's first by-ID read."""
        self.relevant = self._advertise("R0")
        tags = list(self.relevant)
        prefix = self.spec.tag_prefix
        matching = []
        for tag in tags:
            version = version_of(tag, prefix)
            if self.spec.channel == "stable" and version is not None and version[3] is not None:
                self.exclusions.append({"tag": tag, "reason": "prerelease-version"})
                continue
            matching.append((tag, version))
        semver_all = all(version is not None for _tag, version in matching)
        order = self.spec.release_order or "auto"
        self.order = "semver" if order == "semver" or (order == "auto" and semver_all) else "published"
        if self.order == "semver":
            for tag, version in matching:
                if version is None:
                    self.exclusions.append({"tag": tag, "reason": "not-semver"})
            ranked = semver_order([tag for tag, version in matching if version is not None], prefix)
        else:
            ranked = [tag for tag, _version in matching]
        if prior_tag is not None and prior_tag.startswith(prefix) and prior_tag not in self.relevant:
            self.reason = "tag-disappeared"
        if self.order == "published" and len(ranked) > CANDIDATE_LIMIT:
            self.universe_kind = "short-list"
            self._observe_short_list(ranked)
            return
        self.visits = [Visit(tag, self._sort_key(tag), self.relevant[tag]["object"], self.relevant[tag]["peeled"])
                       for tag in ranked]
        if self.order == "semver":
            for visit in self.visits[:CANDIDATE_LIMIT]:
                self._visit(visit)
                if visit.eligibility == "eligible":
                    self.selected = visit
                    break
            if self.selected is None and len(self.visits) > CANDIDATE_LIMIT:
                raise Incomplete("candidate-limit")
        else:
            for visit in self.visits:
                self._visit(visit)
            eligible = [visit for visit in self.visits if visit.eligibility == "eligible"]
            if any(visit.fields["published_at"] is None for visit in eligible):
                raise Incomplete("published-time-unknown")
            if eligible:
                eligible.sort(key=lambda visit: visit.tag.encode("utf-8"))
                eligible.sort(key=lambda visit: (visit.fields["published_at"], visit.fields["id"]), reverse=True)
                self.selected = eligible[0]
        if self.selected is not None:
            self._read_by_id(self.selected)

    def _sort_key(self, tag: str) -> str:
        return tag[len(self.spec.tag_prefix):]

    def _listing(self) -> list[dict]:
        result = self.budget.get(f"{self.repo_api}/releases?per_page={PER_PAGE}")
        try:
            data = loads(result.body, "the GitHub release listing")
        except StateProblem:
            raise Incomplete("release-metadata-invalid") from None
        if not isinstance(data, list):
            raise Incomplete("release-metadata-invalid")
        if len(data) >= PER_PAGE:
            raise Incomplete("short-list-full-page")
        entries = []
        for item in data:
            parsed = release_fields(item)
            if parsed is None:
                raise Incomplete("release-metadata-invalid")
            entries.append({"fields": parsed[0], "notes": parsed[1]})
        return entries

    def _observe_short_list(self, ranked: list[str]) -> None:
        self.listing = self._listing()
        matching = set(ranked)
        by_tag: dict[str, dict] = {}
        for entry in self.listing:
            tag = entry["fields"]["tag"]
            if tag in by_tag:
                raise Incomplete("release-metadata-invalid")
            by_tag[tag] = entry
        for tag in ranked:
            entry = by_tag.get(tag)
            visit = Visit(tag, self._sort_key(tag), self.relevant[tag]["object"], self.relevant[tag]["peeled"])
            fields = None if entry is None else entry["fields"]
            eligibility, reason = self._eligibility(fields)
            visit.m0 = {"status": 200 if entry else 404, "identity": digest_of({"status": 200 if entry else 404,
                                                                                "fields": fields}),
                        "response_sha256": None, "eligibility": eligibility, "reason": reason}
            visit.fields, visit.notes = fields, None if entry is None else entry["notes"]
            self.visits.append(visit)
        eligible = [visit for visit in self.visits if visit.eligibility == "eligible"]
        if any(visit.fields["published_at"] is None for visit in eligible):
            raise Incomplete("published-time-unknown")
        if eligible:
            eligible.sort(key=lambda visit: visit.tag.encode("utf-8"))
            eligible.sort(key=lambda visit: (visit.fields["published_at"], visit.fields["id"]), reverse=True)
            self.selected = eligible[0]
            self._read_by_id(self.selected)

    def confirm(self) -> None:
        """M1 for every visited candidate and the selected by-ID read, then R1; Changed on any difference."""
        if self.universe_kind == "short-list":
            again = self._listing()
            if [(e["fields"], e["notes"]) for e in again] != [(e["fields"], e["notes"]) for e in self.listing]:
                raise Changed()
            for visit in self.visits:
                visit.m1 = dict(visit.m0)
        else:
            for visit in self.visits:
                if visit.m0 is None:
                    continue
                url = f"{self.repo_api}/releases/tags/{urllib.parse.quote(visit.tag, safe='')}"
                observation, fields, notes = self._metadata(url, visit.tag)
                eligibility, reason = self._eligibility(fields)
                observation.update(eligibility=eligibility, reason=reason)
                visit.m1 = observation
                if observation["identity"] != visit.m0["identity"] or notes != visit.notes:
                    raise Changed()
        if self.selected is not None:
            self._read_by_id(self.selected)
        relevant = self._advertise("R1")
        if relevant != self.relevant:
            raise Changed()
        self.complete = True

    # ---- evidence --------------------------------------------------------------------------------------------

    def release_observation_record(self, notes_blob: str | None) -> dict:
        visit = self.selected
        fields = dict(visit.fields)
        return {"version": 1, "kind": "oah-release-observation", "repository": self.spec.locator,
                "spec_digest": self.spec.spec_digest, "release_id": fields["id"], "tag": fields["tag"],
                "draft": fields["draft"], "prerelease": fields["prerelease"], "created_at": fields["created_at"],
                "published_at": fields["published_at"], "updated_at": fields["updated_at"],
                "html_url": fields["html_url"], "body": fields["body"],
                "notes": None if visit.notes is None else {"bytes": len(visit.notes), "sha256": notes_blob},
                "metadata_digest": metadata_digest(fields),
                "observations": {"by_tag": [item.get("response_sha256") for item in (visit.m0, visit.m1) if item],
                                 "by_id": [item["response_sha256"] for item in self.by_id]}}

    def record(self) -> dict:
        excluded = sorted(self.exclusions, key=lambda item: item["tag"].encode("utf-8"))
        candidates = [{"tag": visit.tag, "sort_key": visit.sort_key, "tag_object": visit.tag_object,
                       "peeled_commit": visit.peeled, "eligibility": visit.eligibility,
                       "exclusion_reason": None if visit.m0 is None else visit.m0.get("reason"),
                       "metadata_M0": None if visit.m0 is None else {key: visit.m0[key] for key in
                                                                     ("status", "identity", "response_sha256")},
                       "metadata_M1": None if visit.m1 is None else {key: visit.m1[key] for key in
                                                                     ("status", "identity", "response_sha256")}}
                      for visit in self.visits if visit.m0 is not None]
        selected = None
        if self.selected is not None and self.outcome in ("latest-observed", "unverified"):
            selected = {"tag": self.selected.tag, "release_id": self.selected.fields["id"],
                        "release_observation_digest": self.release_observation,
                        "tag_object": self.selected.tag_object, "peeled_commit": self.selected.peeled,
                        "fetched_commit": (self.content or {}).get("commit"),
                        "tree_digest": (self.content or {}).get("tree")}
        return {"version": 2, "kind": "oah-release-selection", "source_registration": self.spec.registration,
                "spec_digest": self.spec.spec_digest,
                "policy": {"version": SELECTION_POLICY, "release_order": self.spec.release_order or "auto",
                           "effective_order": self.order or None, "tag_prefix": self.spec.tag_prefix,
                           "channel": self.spec.channel, "universe_kind": self.universe_kind},
                "universe": {"matching_count": len(self.relevant) - len(self.exclusions),
                             "syntactic_exclusions_count": len(excluded),
                             "exclusions_digest": digest_of(excluded),
                             "canonical_refs_digest": digest_of(self.relevant),
                             "advertisement_blob_refs": sorted({item["blob"] for item in self.ref_rounds}),
                             "completeness": "observed-complete" if self.complete else "incomplete"},
                "ref_observations": [{key: item[key] for key in ("round", "started_at", "ended_at", "bytes", "rows",
                                                                 "raw_sha256", "relevant_map_sha256")}
                                     for item in self.ref_rounds],
                "candidates": candidates, "selected": selected,
                "budget": {"candidate_limit": SHORT_LIST_LIMIT if self.universe_kind == "short-list"
                           else CANDIDATE_LIMIT, "request_limit": REQUEST_LIMIT,
                           "response_bytes_limit": RESPONSE_LIMIT, "total_http_bytes_limit": TOTAL_LIMIT,
                           "requests_used": self.budget.requests, "bytes_used": self.budget.bytes,
                           "deadline_remaining": round(self.budget.remaining(), 3)},
                "window": {"started_at": self.started_at, "ended_at": self.wall(),
                           "elapsed_monotonic": round(self.clock() - self.started, 3)},
                "outcome": self.outcome, "reason": self.reason or None, "residual_windows": RESIDUAL}
