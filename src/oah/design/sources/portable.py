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
from typing import Any, Callable, Mapping, cast

from oah.design.sources.model import SourceError, parse_observation
from oah.design.sources import selection_proof, release as release_module, gitref, web
import oah.design.sources.shapes as shapes
from oah.design.sources import license as license_module
from oah.design.sources.state import digest_of, hex_of, is_hex, record_digest

VERSION = 1
KIND = "oah-portable-evidence"
BLOB_KINDS = ("selected-file", "license", "notice", "license-manifest", "document-body", "document-text", "release-notes",
              "release-advertisement", "release-metadata")
MAX_BLOBS = 2048
MAX_TOTAL_BYTES = 128 << 20
SPEC_KEYS = ("provider", "locator", "track", "tag_prefix", "channel", "ref", "paths", "manifest_path",
             "release_order", "selection_policy", "verification_policy")
_ENVELOPE_KEYS = {"version", "kind", "source", "spec_digest", "snapshot", "provenance", "blobs"}
_PROVENANCE_KEYS = {"claim", "registration", "observation", "observation_digest", "release_observation",
                    "release_observation_digest", "selection", "selection_digest"}
_BLOB_KEYS = {"id", "kind", "path_or_field", "encoding", "bytes", "sha256", "data"}
HISTORICAL = "verified-from-portable-bytes"
OFFLINE_FRESHNESS = "unknown(offline-or-unregistered)"


class PortableError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _bad(message: str) -> PortableError:
    return PortableError("OAH_SOURCES_EVIDENCE_INVALID", message)


def _object(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _bad(f"{where} is not an object")
    return cast(Mapping[str, Any], value)


def _objects(value: object, where: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise _bad(f"{where} is not a list")
    return [_object(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _manifest_shape(manifest: Mapping[str, Any]) -> None:
    try:
        for key in ("notices", "problems"):
            shapes.validate(manifest.get(key), [str], "manifest." + key)
        shapes.validate(manifest.get("verified"), bool, "manifest.verified")
        shapes.validate(manifest.get("content_key"), str, "manifest.content_key")
        shapes.validate(manifest.get("files"), [shapes.FILE], "manifest.files")
        spec = _object(manifest.get("spec"), "manifest.spec")
        shapes.validate({"id": "manifest", **dict(spec)}, shapes.SOURCE, "manifest.spec")
        license = manifest.get("license")
        if license is not None:
            license = _object(license, "manifest.license")
            shapes.validate({key: license.get(key) for key in ("scopes", "files", "notices", "declared", "problems")},
                            {"scopes": [shapes.SCOPE], "files": [shapes.ORIGINAL], "notices": [shapes.ORIGINAL],
                             "declared": shapes.DECLARED, "problems": [str]}, "manifest.license")
            if license.get("manifest") is not None:
                shapes.validate(license["manifest"], shapes.ORIGINAL, "manifest.license.manifest")
        document = manifest.get("document")
        if document is not None:
            shapes.validate(document, {"url": str, "final_url": str, "status": int, "content_type": str,
                                      "charset": str, "bytes": int, "sha256": str, "text_sha256": str,
                                      "text_bytes": int}, "manifest.document")
        observed = _object(manifest.get("observed"), "manifest.observed")
        for key in ("commit", "ref", "tree"):
            shapes.validate(observed.get(key), shapes.TEXT, "manifest.observed." + key)
        release = observed.get("release")
        if release is not None:
            schema = {"id": int, "tag": str, "tag_object": shapes.TEXT, "commit": str, "metadata_digest": str,
                      "tree": str, "prerelease": bool, "published_at": shapes.TEXT, "release_observation": str,
                      "notes": (type(None), {"bytes": int, "sha256": str}), "selection": str}
            shapes.validate(release, schema, "manifest.observed.release")
    except shapes.ShapeError as exc:
        raise _bad(str(exc)) from None


def uses(manifest: object, selection: object = None) -> list[tuple[str, str, str]]:
    """Every original a manifest names: (kind, path or field, sha256 hex), in a fixed order."""
    out: list[tuple[str, str, str]] = []
    manifest = _object(manifest, "manifest")
    _manifest_shape(manifest)
    for item in _objects(manifest.get("files", []), "manifest.files"):
        out.append(("selected-file", item["path"], item["sha256"]))
    evidence = _object({} if manifest.get("license") is None else manifest["license"], "manifest.license")
    for item in _objects(evidence.get("files", []), "manifest.license.files"):
        out.append(("license", item["path"], item["sha256"]))
    for item in _objects(evidence.get("notices", []), "manifest.license.notices"):
        out.append(("notice", item["path"], item["sha256"]))
    if evidence.get("manifest") is not None:
        package = _object(evidence["manifest"], "manifest.license.manifest")
        out.append(("license-manifest", package["path"], package["sha256"]))
    document = _object({} if manifest.get("document") is None else manifest["document"], "manifest.document")
    if document:
        out.append(("document-body", "document.sha256", document["sha256"]))
        out.append(("document-text", "document.text_sha256", document["text_sha256"]))
    observed = _object(manifest.get("observed", {}), "manifest.observed")
    release = _object({} if observed.get("release") is None else observed["release"], "manifest.observed.release")
    notes = _object({} if release.get("notes") is None else release["notes"], "manifest.observed.release.notes")
    if notes.get("sha256"):
        out.append(("release-notes", "observed.release.notes", notes["sha256"]))
    if selection is not None:
        selection = _object(selection, "selection")
        originals = _object(selection.get("originals", {}), "selection.originals")
        for kind, entries in (("release-advertisement", originals.get("advertisements") or []),
                              ("release-metadata", originals.get("metadata") or [])):
            if not isinstance(entries, list) or len(entries) > release_module.REQUEST_LIMIT:
                raise _bad("selection originals exceed their count quota")
            for index, item in enumerate(entries):
                chunks = item.get("chunks") if isinstance(item, Mapping) else None
                if not isinstance(chunks, list) or len(chunks) > 2:
                    raise _bad("selection originals have invalid chunk membership")
                for chunk, sha in enumerate(chunks):
                    out.append((kind, f"selection.{kind}.{index}.{chunk}", sha))
    if any(not isinstance(where, str) for _kind, where, _sha in out):
        raise _bad("an original has no path or field name")
    return out


def envelope_id(envelope: Mapping) -> str:
    return digest_of(dict(envelope))


def build(*, source_id: str, spec: Mapping, snapshot: str, manifest: Mapping, observation: Mapping | None,
          release_observation: Mapping | None, registration: str | None,
          selection: Mapping | None = None,
          read: Callable[[str], bytes | None]) -> dict:
    """The envelope for one stored snapshot; OAH_SOURCES_EVIDENCE_MISSING when any original is not stored here."""
    if record_digest(manifest) != snapshot:
        raise _bad(f"snapshot {snapshot} does not match its manifest bytes")
    blobs, seen = [], set()
    for kind, where, sha in uses(manifest, selection):
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
                                                          else record_digest(release_observation)),
                           "selection": None if selection is None else dict(selection),
                           "selection_digest": None if selection is None else record_digest(selection)},
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
    selection: Mapping | None
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


def _originals(manifest: Mapping, spec: Mapping, blobs: Mapping[str, bytes]) -> None:
    from oah.design.sources.adapters import FILE_COUNT_LIMIT, FILE_LIMIT, SNAPSHOT_LIMIT

    files = manifest.get("files") or []
    if not isinstance(files, list) or len(files) > FILE_COUNT_LIMIT:
        raise _bad("selected originals exceed their file-count quota")
    evidence = manifest.get("license") or {}
    groups = [(files, FILE_LIMIT), (evidence.get("files") or [], license_module.FILE_LIMIT),
              (evidence.get("notices") or [], license_module.FILE_LIMIT)]
    package = evidence.get("manifest")
    if package is not None:
        if not isinstance(package, Mapping) or package.get("path") != spec.get("manifest_path"):
            raise _bad("the package manifest is not the registered manifest")
        groups.append(([package], license_module.MANIFEST_LIMIT))
    elif evidence.get("declared") is not None:
        raise _bad("a declared license has no original package manifest")
    for entries, limit in groups:
        if not isinstance(entries, list) or len(entries) > license_module.TREE_LIMIT:
            raise _bad("license originals exceed their count quota")
        paths = set()
        for item in entries:
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str) or item["path"] in paths:
                raise _bad("original file membership is malformed or duplicated")
            paths.add(item["path"])
            data = blobs.get(item.get("sha256"))
            if data is None or type(item.get("size")) is not int or item["size"] != len(data) or len(data) > limit:
                raise _bad("an original file differs from its size or exceeds its source quota")
    if sum(item["size"] for item in files) > SNAPSHOT_LIMIT:
        raise _bad("selected originals exceed their aggregate source quota")
    if package is not None:
        declared, problem = license_module.declared_from_manifest(package["path"], blobs[package["sha256"]])
        if declared != evidence.get("declared") or problem and problem not in (evidence.get("problems") or []):
            raise _bad("the license declaration differs from the original package manifest")
    notes = ((manifest.get("observed") or {}).get("release") or {}).get("notes") or {}
    if notes.get("sha256") and len(blobs[notes["sha256"]]) > release_module.NOTES_LIMIT:
        raise _bad("release notes exceed their source quota")


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
    originals = uses(manifest, provenance["selection"])
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
    selection = provenance["selection"]
    if release:
        if (not isinstance(release_record, Mapping) or release_record.get("kind") != "oah-release-observation"
                or record_digest(release_record) != provenance["release_observation_digest"]
                or provenance["release_observation_digest"] != release.get("release_observation")
                or release_record.get("metadata_digest") != release.get("metadata_digest")
                or release_record.get("release_id") != release.get("id") or release_record.get("tag") != release.get("tag")
                or release_record.get("notes") != release.get("notes")):
            raise _bad("the release observation does not match the snapshot's release")
        if not isinstance(selection, Mapping) or record_digest(selection) != release.get("selection") \
                or provenance["selection_digest"] != release.get("selection"):
            raise _bad("the release selection does not match the snapshot")
    elif release_record is not None or provenance["release_observation_digest"] is not None:
        raise _bad("a release observation for a snapshot without a release")
    elif selection is not None or provenance["selection_digest"] is not None:
        raise _bad("a selection for a snapshot without a release")
    raw = envelope["blobs"]
    if not isinstance(raw, list) or len(raw) > MAX_BLOBS:
        raise _bad("blobs is not a list")
    memberships = {}
    for kind, where, sha in originals:
        if not is_hex(sha):
            raise _bad("a required original has no valid digest")
        memberships.setdefault(sha, (kind, where))
    blobs: dict[str, bytes] = {}
    total = 0
    for index, item in enumerate(raw):
        where = f"blob {index}"
        if not isinstance(item, Mapping) or set(item) != _BLOB_KEYS or item["encoding"] != "base64" \
                or item["kind"] not in BLOB_KINDS or not isinstance(item["path_or_field"], str) \
                or not is_hex(item["sha256"]) or item["id"] != "sha256:" + item["sha256"] \
                or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= release_module.RESPONSE_LIMIT \
                or not isinstance(item["data"], str) or len(item["data"]) > ((item["bytes"] + 2) // 3) * 4 \
                or memberships.get(item["sha256"]) != (item["kind"], item["path_or_field"]):
            raise _bad(f"{where} is malformed")
        total += item["bytes"]
        if total > MAX_TOTAL_BYTES:
            raise _bad("portable originals exceed their aggregate byte quota")
        data = _strict_base64(item["data"], where)
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise _bad(f"{where} ({item['path_or_field']}) does not match its size or sha256")
        if item["sha256"] in blobs:
            raise _bad(f"{where} repeats an original")
        blobs[item["sha256"]] = data
    needed = set(memberships)
    missing = sorted(needed - set(blobs))
    if missing:
        raise _bad(f"the originals {', '.join(sha[:12] for sha in missing)} are missing")
    extra = sorted(set(blobs) - needed)
    if extra:
        raise _bad(f"the blobs {', '.join(sha[:12] for sha in extra)} are not in the manifest")
    _originals(manifest, spec, blobs)
    if release:
        try:
            selection_proof.verify(selection, spec, release, blobs.get, release_record)
        except (ValueError, KeyError, TypeError, AttributeError, SourceError, gitref.RefError,
                release_module.Incomplete, release_module.Changed, web.WebFailure) as exc:
            raise _bad(f"the release selection cannot be verified: {type(exc).__name__}") from None
    return Evidence(envelope_id(envelope), source["id"], spec, envelope["spec_digest"], snapshot["digest"],
                    dict(manifest), None if observation is None else dict(observation),
                    None if release_record is None else dict(release_record),
                    None if selection is None else dict(selection), blobs)


def payload_bytes(envelope: Mapping) -> int:
    """The base64 original payload size of one envelope (what the materials projection replaces with read
    references)."""
    return sum(len(item.get("data") or "") for item in envelope.get("blobs") or [])
