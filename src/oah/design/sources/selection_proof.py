# 선택 증거 재생 — 저장한 광고·REST 원본만으로 선택기를 다시 실행해 폐쇄된 후보·예산·승자 증거를 대조한다.
from __future__ import annotations

import dataclasses
import hashlib
import math
import re
from collections.abc import Mapping
from typing import Any, Callable

from oah.design.sources import gitref, release, web
from oah.design.sources.model import build_spec, parse_iso
from oah.design.sources.state import is_hex

_KEYS = {"version", "kind", "source_registration", "spec_digest", "policy", "universe", "ref_observations",
         "candidates", "selected", "originals", "budget", "window", "outcome", "reason", "residual_windows"}


def verify(record: Mapping[str, Any], spec: Mapping[str, Any], observed: Mapping[str, Any],
           read: Callable[[str], bytes | None], release_record: Mapping[str, Any]) -> None:
    if set(record) != _KEYS or record["version"] != 2 or record["kind"] != "oah-release-selection":
        raise ValueError("the release selection has an unknown shape")
    token = record["source_registration"]
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("the selection registration is invalid")
    original = record["originals"]
    if not isinstance(original, dict) or set(original) != {"advertisements", "metadata"}:
        raise ValueError("the selection originals are absent")
    advertisements, metadata = original["advertisements"], original["metadata"]
    if not isinstance(advertisements, list) or len(advertisements) != 2 or not isinstance(metadata, list) \
            or not 2 <= len(metadata) <= release.REQUEST_LIMIT:
        raise ValueError("the selection originals exceed their counts")
    rounds = record["ref_observations"]
    if not isinstance(rounds, list) or len(rounds) != 2:
        raise ValueError("the selection needs both advertisements")
    for item in rounds:
        if not isinstance(item, dict) or set(item) != {"round", "started_at", "ended_at", "bytes", "rows",
                                                      "raw_sha256", "relevant_map_sha256"}:
            raise ValueError("the advertisement observation is invalid")
        if parse_iso(item["started_at"]) is None or parse_iso(item["ended_at"]) is None:
            raise ValueError("the advertisement observation time is invalid")

    def chunks(refs: object, limit: int) -> bytes:
        if not isinstance(refs, list) or len(refs) > (limit + release.RESPONSE_LIMIT - 1) // release.RESPONSE_LIMIT:
            raise ValueError("the original chunk count exceeds its quota")
        parts = []
        total = 0
        for sha in refs:
            if not is_hex(sha):
                raise ValueError("the original chunk identity is invalid")
            data = read(sha)
            if data is None or not data or len(data) > release.RESPONSE_LIMIT or hashlib.sha256(data).hexdigest() != sha:
                raise ValueError("an original chunk is missing or altered")
            total += len(data)
            if total > limit:
                raise ValueError("the original exceeds its byte quota")
            parts.append(data)
        return b"".join(parts)

    advertisement_index = metadata_index = 0

    def advertise(_url: str, *, tags: bool, timeout: float) -> gitref.Advertisement:
        nonlocal advertisement_index
        index = advertisement_index
        advertisement_index += 1
        if index >= 2:
            raise ValueError("an extra advertisement was requested")
        item, observation = advertisements[index], rounds[index]
        if not isinstance(item, dict) or set(item) != {"round", "chunks"} or item["round"] != ("R0", "R1")[index]:
            raise ValueError("the advertisement originals have an unknown shape or order")
        raw = chunks(item["chunks"], gitref.BYTE_LIMIT)
        refs = gitref.parse(raw, tags_only=True)
        return gitref.Advertisement(raw, len(refs), observation["started_at"], observation["ended_at"], refs)

    def get(url: str, *, policy: web.WebPolicy, headers: Mapping[str, str]) -> web.WebResult:
        nonlocal metadata_index
        if metadata_index >= len(metadata):
            raise ValueError("a required metadata original is absent")
        item = metadata[metadata_index]
        metadata_index += 1
        if not isinstance(item, dict) or set(item) != {"endpoint", "status", "bytes", "sha256", "chunks"}:
            raise ValueError("a metadata original has an unknown shape")
        if not isinstance(item["endpoint"], str) or url != selector.repo_api + item["endpoint"]:
            raise ValueError("the metadata original belongs to a different request")
        if type(item["bytes"]) is not int or not 0 <= item["bytes"] <= policy.max_bytes:
            raise ValueError("the metadata original exceeds its byte quota")
        raw = chunks(item["chunks"], policy.max_bytes)
        if item["status"] == 404 and item["bytes"] == 0 and item["sha256"] is None and not raw:
            raise web.WebFailure("not-found", 404)
        if item["status"] != 200 or len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("the metadata original has an invalid status, length or digest")
        return web.WebResult(200, url, {}, raw)

    def put(data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        if read(sha) != data:
            raise ValueError("an original was not preserved exactly")
        return sha

    parsed = build_spec(url=spec["locator"], track="release", tag_prefix=spec["tag_prefix"],
                        prerelease=spec["channel"] == "prerelease", paths=spec["paths"],
                        manifest_path=spec["manifest_path"], release_order=spec["release_order"])
    if parsed.verification() != dict(spec):
        raise ValueError("the selection spec is not canonical")
    parsed = dataclasses.replace(parsed, registration=token)
    selector = release.Selector(parsed, api_base="https://api.github.com", git_url=parsed.locator, getter=get,
        policy=lambda size, timeout: web.WebPolicy(max_bytes=size, timeout=timeout), deadline=600, timeout=30,
        put_blob=put, clock=lambda: 0.0, wall=lambda: record["window"]["started_at"], advertiser=advertise)
    selector.observe(None)
    selector.confirm()
    if advertisement_index != len(advertisements) or metadata_index != len(metadata) or selector.selected is None:
        raise ValueError("the selection originals have missing or extra membership")
    selector.content = {"commit": observed["commit"], "tree": observed["tree"]}
    selector.release_observation = observed["release_observation"]
    if record["outcome"] not in ("latest-observed", "unverified") or record["reason"] not in (None, "tag-disappeared"):
        raise ValueError("the selection did not establish a selected release")
    selector.outcome, selector.reason = record["outcome"], record["reason"] or ""
    notes = selector.selected.notes
    if selector.release_observation_record(None if notes is None else hashlib.sha256(notes).hexdigest()) != release_record:
        raise ValueError("the selected metadata originals do not reconstruct the release observation")
    expected = selector.record()
    window = record["window"]
    if not isinstance(window, dict) or set(window) != {"started_at", "ended_at", "elapsed_monotonic"} \
            or parse_iso(window["started_at"]) is None or parse_iso(window["ended_at"]) is None:
        raise ValueError("the selection window is invalid")
    for number in (window["elapsed_monotonic"], record["budget"]["deadline_remaining"]):
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 3600:
            raise ValueError("the selection monotonic budget is invalid")
    expected["window"] = window
    expected["budget"]["deadline_remaining"] = record["budget"]["deadline_remaining"]
    if expected != record or (selector.selected.fields or {}).get("id") != observed["id"] \
            or selector.selected.tag != observed["tag"]:
        raise ValueError("the selection, universe, ordering or budgets cannot be reconstructed from its originals")
