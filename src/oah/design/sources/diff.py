# 출처 스냅숏 비교 — 저장된 두 불변 스냅숏의 경로·라이선스·릴리스·문서·관측 시각 차이와 상한 있는 텍스트 diff 를 네트워크·설계 저장 없이 만든다.
"""DiffReportV1: an offline comparison of two immutable snapshots of one source.

Both sides are named by snapshot digest and read from the re-hashed content-addressed store. Paths are compared by
identity (mode, blob, size, sha256); text differences use difflib on strict UTF-8 only, with terminal controls and
bidi characters escaped and every external line labelled as data. Bounds: 200 displayed paths, 50 text diffs, 200
lines each, 64 KiB of rendered diff; every omitted diff is counted and named, and the JSON keeps every path identity
(the display limit only shortens human output). Missing bytes make that item `unavailable` and the report `partial`;
binary or non-UTF-8 bytes give `binary-no-text-diff`. Nothing here touches the network, moves a confirmation time or
saves a design.
"""
from __future__ import annotations

import difflib
import unicodedata
from typing import Callable, Mapping

PATH_LIMIT = 200
DIFF_LIMIT = 50
LINE_LIMIT = 200
TOTAL_LIMIT = 64 * 1024


def escape(text: str) -> str:
    """Terminal controls and bidi/format characters as visible escapes (external text stays data)."""
    out = []
    for char in text:
        if char in "\t":
            out.append(char)
        elif ord(char) < 32 or ord(char) == 127 or unicodedata.category(char) in ("Cf", "Cc", "Zl", "Zp"):
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def _text(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class _Budget:
    def __init__(self) -> None:
        self.count = 0
        self.bytes = 0
        self.omitted: list[dict] = []


def _text_diff(name: str, old: bytes | None, new: bytes | None, budget: _Budget) -> dict:
    """One bounded text diff item: complete | truncated | binary-no-text-diff | unavailable | omitted."""
    if old is None or new is None:
        return {"item": name, "status": "unavailable"}
    old_text, new_text = _text(old), _text(new)
    if old_text is None or new_text is None or "\x00" in old_text or "\x00" in new_text:
        return {"item": name, "status": "binary-no-text-diff"}
    if budget.count >= DIFF_LIMIT or budget.bytes >= TOTAL_LIMIT:
        budget.omitted.append({"item": name, "reason": "diff-limit"})
        return {"item": name, "status": "omitted"}
    budget.count += 1
    lines = list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=2))
    shown, size, truncated = [], 0, False
    for line in lines:
        rendered = escape(line)
        if len(shown) >= LINE_LIMIT or budget.bytes + size + len(rendered.encode("utf-8")) + 1 > TOTAL_LIMIT:
            truncated = True
            break
        shown.append(rendered)
        size += len(rendered.encode("utf-8")) + 1
    budget.bytes += size
    return {"item": name, "status": "truncated" if truncated else "complete", "label": "external-data",
            "lines": shown, "total_lines": len(lines)}


def _files(manifest: Mapping) -> dict[str, dict]:
    return {item["path"]: item for item in manifest.get("files") or []}


def _identity(item: Mapping | None) -> dict | None:
    return None if item is None else {key: item.get(key) for key in ("mode", "blob", "size", "sha256")}


def _license_ids(evidence: Mapping | None) -> list:
    if not evidence:
        return []
    files = [{"path": item.get("path"), "sha256": item.get("sha256")} for item in evidence.get("files") or []]
    notices = [{"path": item.get("path"), "sha256": item.get("sha256")} for item in evidence.get("notices") or []]
    return [files, notices, evidence.get("declared")]


def times_of(chain: list[Mapping], snapshots: tuple[str, str]) -> dict[str, dict | None]:
    """Original verification/confirmation times per snapshot from a newest-first observation chain."""
    times: dict[str, dict | None] = {digest: None for digest in snapshots}
    for record in reversed(chain):  # oldest first
        digest = record.get("snapshot")
        if digest not in times:
            continue
        entry = times[digest]
        if entry is None:
            times[digest] = {"verified_at": record["verified_at"], "first_observed_at": record["observed_at"],
                             "confirmed_at": record["confirmed_at"], "observations": 1,
                             "first_observation": record["digest"], "last_observation": record["digest"]}
        else:
            entry.update(confirmed_at=record["confirmed_at"], observations=entry["observations"] + 1,
                         last_observation=record["digest"])
    return times


def compare(source: Mapping, before: Mapping, after: Mapping, *, read: Callable[[str], bytes | None],
            times: Mapping[str, Mapping | None], eligibility: Callable[[Mapping], str], policy: str) -> dict:
    """The DiffReportV1 for two manifests of the same provider/locator (the caller checks that).

    `read` returns a stored blob's bytes or None; `eligibility` derives a snapshot's license eligibility under the
    current `policy`, so a policy-only change is never reported as changed source bytes."""
    budget = _Budget()
    problems: list[str] = []
    old_files, new_files = _files(before), _files(after)
    names = sorted(set(old_files) | set(new_files))
    added = [name for name in names if name not in old_files]
    removed = [name for name in names if name not in new_files]
    changed = [name for name in names if name in old_files and name in new_files
               and _identity(old_files[name]) != _identity(new_files[name])]
    unchanged = [name for name in names if name in old_files and name in new_files and name not in changed]
    entries = [{"path": name, "change": kind, "from": _identity(old_files.get(name)),
                "to": _identity(new_files.get(name))}
               for kind, group in (("added", added), ("removed", removed), ("changed", changed))
               for name in group]
    text_diffs = []
    for name in changed:
        old, new = old_files[name], new_files[name]
        text_diffs.append(_text_diff(f"path:{name}", read(old["sha256"]), read(new["sha256"]), budget))
    old_license, new_license = before.get("license"), after.get("license")
    license_bytes_changed = _license_ids(old_license) != _license_ids(new_license)
    if license_bytes_changed:
        old_ids = {item["path"]: item for item in (old_license or {}).get("files") or []}
        for item in (new_license or {}).get("files") or []:
            previous = old_ids.get(item["path"])
            if previous is not None and previous.get("sha256") != item.get("sha256"):
                text_diffs.append(_text_diff(f"license:{item['path']}", read(previous["sha256"]),
                                             read(item["sha256"]), budget))
    old_release = (before.get("observed") or {}).get("release") or {}
    new_release = (after.get("observed") or {}).get("release") or {}
    release = None
    if old_release or new_release:
        keys = ("id", "tag", "tag_object", "commit", "tree", "prerelease", "published_at", "metadata_digest")
        release = {"from": {key: old_release.get(key) for key in keys} | {"notes": old_release.get("notes")},
                   "to": {key: new_release.get(key) for key in keys} | {"notes": new_release.get("notes")},
                   "changed": [key for key in keys if old_release.get(key) != new_release.get(key)]}
        old_notes, new_notes = old_release.get("notes") or {}, new_release.get("notes") or {}
        if old_notes.get("sha256") != new_notes.get("sha256"):
            release["changed"].append("notes")
            if old_notes.get("sha256") and new_notes.get("sha256"):
                text_diffs.append(_text_diff("release-notes", read(old_notes["sha256"]), read(new_notes["sha256"]),
                                             budget))
    document = None
    old_doc, new_doc = before.get("document") or {}, after.get("document") or {}
    if old_doc or new_doc:
        keys = ("final_url", "content_type", "bytes", "sha256", "text_sha256")
        document = {"from": {key: old_doc.get(key) for key in keys}, "to": {key: new_doc.get(key) for key in keys},
                    "changed": [key for key in keys if old_doc.get(key) != new_doc.get(key)]}
        if old_doc.get("text_sha256") != new_doc.get("text_sha256"):
            text_diffs.append(_text_diff("document-text", read(old_doc.get("text_sha256") or ""),
                                         read(new_doc.get("text_sha256") or ""), budget))
    statuses = {item["status"] for item in text_diffs}
    if "unavailable" in statuses:
        problems.append("bytes-unavailable")
    if statuses & {"truncated", "omitted"}:
        problems.append("text-diff-limited")
    old_spec, new_spec = before.get("spec") or {}, after.get("spec") or {}
    eligible_from, eligible_to = eligibility(before), eligibility(after)
    return {"version": 1, "kind": "oah-source-diff", "source_identity": dict(source),
            "from": {"snapshot": source["from"], "spec_digest": before.get("spec_digest"),
                     "content_key": before.get("content_key")},
            "to": {"snapshot": source["to"], "spec_digest": after.get("spec_digest"),
                   "content_key": after.get("content_key")},
            "completeness": "partial" if problems else "complete",
            "scope": {"compared": "spec" if before.get("spec_digest") != after.get("spec_digest") else "same-spec",
                      "from": {key: old_spec.get(key) for key in ("track", "paths", "ref", "tag_prefix")},
                      "to": {key: new_spec.get(key) for key in ("track", "paths", "ref", "tag_prefix")}},
            "paths": {"counts": {"added": len(added), "removed": len(removed), "changed": len(changed),
                                 "unchanged": len(unchanged)},
                      "unchanged_compared_by": "identity",
                      "entries": entries, "display_limit": PATH_LIMIT,
                      "beyond_display_limit": [item["path"] for item in entries[PATH_LIMIT:]]},
            "license": {"source_bytes_changed": license_bytes_changed,
                        "from": _license_ids(old_license), "to": _license_ids(new_license),
                        "eligibility": {"policy": policy, "from": eligible_from, "to": eligible_to,
                                        "changed": eligible_from != eligible_to}},
            "release": release, "document": document,
            "notices": {"from": list(before.get("notices") or []), "to": list(after.get("notices") or [])},
            "observation_times": {"from": times.get(source["from"]), "to": times.get(source["to"])},
            "text_diffs": text_diffs, "omitted_diffs": budget.omitted,
            "limits": {"paths": PATH_LIMIT, "diffs": DIFF_LIMIT, "lines": LINE_LIMIT, "bytes": TOTAL_LIMIT},
            "problems": problems}


def unavailable(source: Mapping, missing: str) -> dict:
    return {"version": 1, "kind": "oah-source-diff", "source_identity": dict(source),
            "from": {"snapshot": source["from"]}, "to": {"snapshot": source["to"]}, "completeness": "unavailable",
            "problems": [f"snapshot-unavailable:{missing}"]}
