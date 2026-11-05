# 휴대 증거 — 한 스냅숏의 출처 명세·매니페스트·관측 기록·원본 바이트 전부를 base64 로 담아, 등록도 저장소도 없는 빈 클론에서 다시 검증하는 PortableEvidenceV1 를 만들고 확인한다.
"""PortableEvidenceV1: the complete evidence of one source snapshot, verifiable offline.

    envelope = build(source_id=..., spec=spec.verification(), snapshot=digest, manifest=manifest,
                     observation=record, release_observation=record, read=sources.blob)
    ident = envelope_id(envelope)          # the digest that references name; it is outside the payload
    evidence = verify(envelope)            # strict: shapes, record digests, base64, sizes, hashes, membership

The envelope carries the full verification SourceSpec, the immutable snapshot manifest (its stored-record digest is
the snapshot digest), the observation record used at decision time and, for a release, its ReleaseObservationV1, as
separate local claims. Every original the manifest names (selected files, license and notice files, the document body
and text, the complete release notes) is embedded once per distinct sha256 as `{id,kind,path_or_field,encoding,bytes,
sha256,data}`; no clone-only blob reference counts as preserved bytes. Decoded bytes are data, never instructions:
NUL, controls, bidi, non-UTF-8 and marker lines all survive inside base64. Verification needs no registry, store or
network, and its success is historical evidence, not a current upstream confirmation.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
from typing import Callable, Mapping

from oah.design.sources.model import SourceError, parse_observation
from oah.design.sources.state import digest_of, hex_of, is_hex, record_digest

VERSION = 1
KIND = "oah-portable-evidence"
BLOB_KINDS = ("selected-file", "license", "notice", "document-body", "document-text", "release-notes")
SPEC_KEYS = ("provider", "locator", "track", "tag_prefix", "channel", "ref", "paths", "manifest_path",
             "release_order", "selection_policy", "verification_policy")
_ENVELOPE_KEYS = {"version", "kind", "source", "spec_digest", "snapshot", "provenance", "blobs"}
_PROVENANCE_KEYS = {"claim", "registration", "observation", "observation_digest", "release_observation",
                    "release_observation_digest"}
_BLOB_KEYS = {"id", "kind", "path_or_field", "encoding", "bytes", "sha256", "data"}
HISTORICAL = "verified-from-portable-bytes"
OFFLINE_FRESHNESS = "unknown(offline-or-unregistered)"


class PortableError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _bad(message: str) -> PortableError:
    return PortableError("OAH_SOURCES_EVIDENCE_INVALID", message)


def uses(manifest: Mapping) -> list[tuple[str, str, str]]:
    """Every original a manifest names: (kind, path or field, sha256 hex), in a fixed order."""
    out: list[tuple[str, str, str]] = []
    for item in manifest.get("files") or []:
        out.append(("selected-file", item.get("path"), item.get("sha256")))
    evidence = manifest.get("license") or {}
    for item in evidence.get("files") or []:
        out.append(("license", item.get("path"), item.get("sha256")))
    for item in evidence.get("notices") or []:
        out.append(("notice", item.get("path"), item.get("sha256")))
    document = manifest.get("document") or {}
    if document:
        out.append(("document-body", "document.sha256", document.get("sha256")))
        out.append(("document-text", "document.text_sha256", document.get("text_sha256")))
    release = (manifest.get("observed") or {}).get("release") or {}
    notes = release.get("notes") or {}
    if notes.get("sha256"):
        out.append(("release-notes", "observed.release.notes", notes.get("sha256")))
    return out


def envelope_id(envelope: Mapping) -> str:
    return digest_of(dict(envelope))


def build(*, source_id: str, spec: Mapping, snapshot: str, manifest: Mapping, observation: Mapping | None,
          release_observation: Mapping | None, registration: str | None,
          read: Callable[[str], bytes | None]) -> dict:
    """The envelope for one stored snapshot; OAH_SOURCES_EVIDENCE_MISSING when any original is not stored here."""
    if record_digest(manifest) != snapshot:
        raise _bad(f"snapshot {snapshot} does not match its manifest bytes")
    blobs, seen = [], set()
    for kind, where, sha in uses(manifest):
        if not is_hex(sha):
            raise _bad(f"{where} names no sha256")
        if sha in seen:
            continue
        seen.add(sha)
        data = read(sha)
        if data is None or hashlib.sha256(data).hexdigest() != sha:
            raise PortableError("OAH_SOURCES_EVIDENCE_MISSING", f"the original {where} ({sha[:12]}) of snapshot "
                                f"{snapshot} is not stored in this clone; check the source again before building "
                                "portable evidence")
        blobs.append({"id": "sha256:" + sha, "kind": kind, "path_or_field": where, "encoding": "base64",
                      "bytes": len(data), "sha256": sha, "data": base64.b64encode(data).decode("ascii")})
    return {"version": VERSION, "kind": KIND, "source": {"id": source_id, **{key: spec.get(key) for key in SPEC_KEYS}},
            "spec_digest": digest_of({key: spec.get(key) for key in SPEC_KEYS}),
            "snapshot": {"digest": snapshot, "manifest": dict(manifest)},
            "provenance": {"claim": "local-observation", "registration": registration,
                           "observation": None if observation is None else dict(observation),
                           "observation_digest": None if observation is None else record_digest(observation),
                           "release_observation": None if release_observation is None else dict(release_observation),
                           "release_observation_digest": (None if release_observation is None
                                                          else record_digest(release_observation))},
            "blobs": blobs}


@dataclasses.dataclass(frozen=True)
class Evidence:
    """A verified envelope: its identity, spec, manifest, provenance records and decoded originals."""

    ident: str
    source_id: str
    spec: Mapping
    spec_digest: str
    snapshot: str
    manifest: Mapping
    observation: Mapping | None
    release_observation: Mapping | None
    blobs: Mapping[str, bytes]

    def read(self, sha: str) -> bytes | None:
        return self.blobs.get(sha)

    def times(self) -> dict | None:
        record = self.observation
        if record is None:
            return None
        return {key: record[key] for key in ("attempted_at", "observed_at", "outcome", "confirmed_at", "verified_at")}


def _strict_base64(text: object, where: str) -> bytes:
    if not isinstance(text, str):
        raise _bad(f"{where} data is not base64 text")
    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise _bad(f"{where} data is not strict base64") from None
    if base64.b64encode(data).decode("ascii") != text:
        raise _bad(f"{where} data is not canonical base64")
    return data


def verify(envelope: object) -> Evidence:
    """Every identity and byte rechecked from the envelope alone; OAH_SOURCES_EVIDENCE_INVALID otherwise."""
    if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_KEYS or envelope.get("version") != VERSION \
            or envelope.get("kind") != KIND:
        raise _bad("not a version-1 portable evidence envelope")
    source = envelope["source"]
    if not isinstance(source, Mapping) or set(source) != {"id", *SPEC_KEYS} or not isinstance(source["id"], str):
        raise _bad("the source spec is not complete")
    spec = {key: source[key] for key in SPEC_KEYS}
    if digest_of(spec) != envelope["spec_digest"]:
        raise _bad("the spec digest does not match the spec")
    snapshot = envelope["snapshot"]
    if not isinstance(snapshot, Mapping) or set(snapshot) != {"digest", "manifest"} \
            or hex_of(snapshot["digest"]) is None or not isinstance(snapshot["manifest"], Mapping):
        raise _bad("the snapshot is malformed")
    manifest = snapshot["manifest"]
    if record_digest(manifest) != snapshot["digest"]:
        raise _bad("the snapshot digest does not match the manifest")
    if manifest.get("kind") != "oah-source-snapshot" or manifest.get("version") != 1 \
            or manifest.get("spec") != spec or manifest.get("spec_digest") != envelope["spec_digest"]:
        raise _bad("the manifest belongs to another spec")
    provenance = envelope["provenance"]
    if not isinstance(provenance, Mapping) or set(provenance) != _PROVENANCE_KEYS \
            or provenance["claim"] != "local-observation":
        raise _bad("the provenance is malformed")
    observation = provenance["observation"]
    if observation is not None:
        try:
            parse_observation(observation, "portable observation")
        except SourceError:
            raise _bad("the observation record is malformed") from None
        if (record_digest(observation) != provenance["observation_digest"]
                or observation["snapshot"] != snapshot["digest"]
                or observation["source"]["spec_digest"] != envelope["spec_digest"]):
            raise _bad("the observation does not belong to this snapshot")
    elif provenance["observation_digest"] is not None:
        raise _bad("an observation digest without its record")
    release = (manifest.get("observed") or {}).get("release")
    release_record = provenance["release_observation"]
    if release:
        if (not isinstance(release_record, Mapping) or release_record.get("kind") != "oah-release-observation"
                or record_digest(release_record) != provenance["release_observation_digest"]
                or provenance["release_observation_digest"] != release.get("release_observation")
                or release_record.get("metadata_digest") != release.get("metadata_digest")
                or release_record.get("release_id") != release.get("id") or release_record.get("tag") != release.get("tag")
                or release_record.get("notes") != release.get("notes")):
            raise _bad("the release observation does not match the snapshot's release")
    elif release_record is not None or provenance["release_observation_digest"] is not None:
        raise _bad("a release observation for a snapshot without a release")
    raw = envelope["blobs"]
    if not isinstance(raw, list):
        raise _bad("blobs is not a list")
    blobs: dict[str, bytes] = {}
    for index, item in enumerate(raw):
        where = f"blob {index}"
        if not isinstance(item, Mapping) or set(item) != _BLOB_KEYS or item["encoding"] != "base64" \
                or item["kind"] not in BLOB_KINDS or not isinstance(item["path_or_field"], str) \
                or not is_hex(item["sha256"]) or item["id"] != "sha256:" + item["sha256"] \
                or type(item["bytes"]) is not int:
            raise _bad(f"{where} is malformed")
        data = _strict_base64(item["data"], where)
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise _bad(f"{where} ({item['path_or_field']}) does not match its size or sha256")
        if item["sha256"] in blobs:
            raise _bad(f"{where} repeats an original")
        blobs[item["sha256"]] = data
    needed = {sha for _kind, _where, sha in uses(manifest)}
    missing = sorted(needed - set(blobs))
    if missing:
        raise _bad(f"the originals {', '.join(sha[:12] for sha in missing)} are missing")
    extra = sorted(set(blobs) - needed)
    if extra:
        raise _bad(f"the blobs {', '.join(sha[:12] for sha in extra)} are not in the manifest")
    return Evidence(envelope_id(envelope), source["id"], spec, envelope["spec_digest"], snapshot["digest"],
                    dict(manifest), None if observation is None else dict(observation),
                    None if release_record is None else dict(release_record), blobs)


def payload_bytes(envelope: Mapping) -> int:
    """The base64 original payload size of one envelope (what the materials projection replaces with read
    references)."""
    return sum(len(item.get("data") or "") for item in envelope.get("blobs") or [])
