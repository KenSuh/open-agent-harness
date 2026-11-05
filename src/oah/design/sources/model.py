# 출처 기록 모델 — 등록 명세(위치·추적 방식·경로)의 검증, 등록 토큰과 명세 다이제스트, 출처 머리(head)의 엄격한 읽기, 관측을 머리에 반영하는 순수 전이와 신선도 계산을 둔다.
"""What a registered source is and what its head says, independent of the network.

A registration names exactly what is checked: the provider, the canonical locator, the track and every input that
decides what a snapshot is (`verification()`); `spec_digest` covers those inputs, and a random `registration` token
changes on every new registration, so a removed and re-added source never inherits observations made for another.

The head is the only mutable record per source and its atomic replace is the commit point. It keeps three times
apart: `attempted_at` (any attempt), `confirmed_at` (the last attempt that verified the latest upstream state) and
`verified.at` (when the verified snapshot was established). `freshness()` derives the state on read; an unverified
or failed attempt never moves `confirmed_at`, so an old proof is never shown as fresh.

Every establishment or confirmation also leaves an immutable `ObservationV1` record (`observation_record()`), linked
to the one before it; the head points at the latest. Nothing evicts them, so the observation a saved material used
stays readable after later checks.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import ipaddress
import re
import secrets
import urllib.parse
from typing import Mapping, Sequence

from oah.design.model import DesignError, check_id
from oah.design.sources.state import digest_of, hex_of, is_hex
from oah.toolkit.fetch import StageError, check_portable_paths

MAX_SOURCES = 200
MAX_PATHS = 16
MAX_FOR = 16
PATH_MAX = 300
PROVIDERS = ("github", "http")
TRACKS = {"github": ("release", "commit"), "http": ("doc",)}
CHANNELS = ("stable", "prerelease")
RELEASE_ORDERS = ("auto", "semver", "published")
SELECTION_POLICY = "release-selection-v2"
VERIFICATION_POLICY = "source-verification-v1"
MANIFEST_NAMES = ("Cargo.toml", "package.json", "pyproject.toml")
OUTCOMES = ("unchanged", "changed", "unverified", "blocked", "failed")
# Closed problem and failure codes; nothing else is ever stored or printed for an attempt.
PROBLEMS = ("tag-moved", "ref-mismatch", "path-missing", "unsafe-entry", "oversize", "selection-incomplete",
            "no-matching-release", "tag-missing", "ref-missing", "manifest-missing", "release-notes-too-large",
            "changed-during-check", "tag-disappeared", "release-metadata-invalid")
FAILURES = ("timeout", "network", "dns", "rate-limited", "not-found", "gone", "moved", "proxy-configured",
            "private-destination", "insecure-url", "redirect-off-origin", "too-many-redirects", "oversize",
            "content-type", "content-encoding", "bad-response", "git-failed", "http-error")
FRESHNESS = ("unknown", "fresh", "stale", "unverified", "unavailable")

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")
_URL_PATH = re.compile(r"/[\x21-\x7e]{0,1999}")
_TAG_PREFIX = re.compile(r"(?:[A-Za-z0-9._@][A-Za-z0-9._/@-]{0,63})?")
_BRANCH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}")
_TOKEN = re.compile(r"[0-9a-f]{32}")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class SourceError(Exception):
    """A refusal; the message starts with an OAH_SOURCE(S)_* code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


# Locators -------------------------------------------------------------------------------------------------------


def _invalid(message: str) -> SourceError:
    return SourceError("OAH_SOURCES_LOCATOR_INVALID", message)


def _refuse_hidden(url: str) -> None:
    """urlsplit silently drops tabs, line breaks and edge spaces, so such a URL would register another address
    than the one shown; it is refused instead."""
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise _invalid("the URL holds a space, tab, line break or control character; paste it again without "
                       "them")


def github_locator(url: str) -> str:
    """The canonical `https://github.com/<owner>/<repo>` of a public GitHub repository URL."""
    _refuse_hidden(url)
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise _invalid(f"{url!r} is not a URL") from None
    if (parts.scheme != "https" or (parts.hostname or "").lower() != "github.com" or "@" in parts.netloc
            or port is not None or parts.query or parts.fragment):
        raise _invalid("a GitHub source is https://github.com/<owner>/<repo>, with no user name, port, query or "
                       "fragment")
    segments = parts.path.strip("/").split("/")
    if len(segments) != 2:
        raise _invalid("a GitHub source names one repository: https://github.com/<owner>/<repo>")
    owner, repo = segments
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not _OWNER.fullmatch(owner) or not _REPO.fullmatch(repo) or repo in (".", ".."):
        raise _invalid("the owner or repository name is not a GitHub name")
    return f"https://github.com/{owner.lower()}/{repo.lower()}"


def doc_locator(url: str, *, allow_loopback: bool = False) -> str:
    """A public https document URL without user name, port, query or fragment (loopback http only in tests)."""
    _refuse_hidden(url)
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise _invalid(f"{url!r} is not a URL") from None
    host = (parts.hostname or "").lower()
    loopback = allow_loopback and host in _LOOPBACK
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise _invalid("a document source has no user name, query or fragment")
    if loopback:
        if parts.scheme not in ("http", "https"):
            raise _invalid("a document source uses https")
    elif parts.scheme != "https" or port is not None:
        raise _invalid("a document source is a plain https URL on the default port")
    if not loopback:
        try:
            ipaddress.ip_address(host.strip("[]"))
            raise _invalid("a document source names a host, not an IP address")
        except ValueError:
            pass
        if not _HOST.fullmatch(host):
            raise _invalid("the host is not a public DNS name")
    path = parts.path or "/"
    if not _URL_PATH.fullmatch(path):
        raise _invalid("the path must be printable ASCII (percent-encode other characters), up to 2,000 characters")
    netloc = host if port is None else f"{host}:{port}"
    if ":" in host and port is None:
        netloc = f"[{host}]"
    elif ":" in host:
        netloc = f"[{host}]:{port}"
    return f"{parts.scheme}://{netloc}{path}"


def _check_paths(values: Sequence[str], what: str) -> tuple[str, ...]:
    paths = []
    for value in values:
        if (not isinstance(value, str) or not value or len(value) > PATH_MAX or value.startswith(("/", "-"))
                or value.endswith("/") or any(c in value for c in "\\[]:*?\"<>|") or "//" in value
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise SourceError("OAH_SOURCES_USAGE", f"{what} {value!r}: give a repository path such as crates/foo or "
                              "src/lib.rs, with forward slashes and no wildcards")
        paths.append(value)
    try:
        check_portable_paths(paths)
    except StageError as exc:
        raise SourceError("OAH_SOURCES_USAGE", f"{what}: {str(exc).partition(': ')[2]}") from None
    if len(set(paths)) != len(paths):
        raise SourceError("OAH_SOURCES_USAGE", f"{what}: a path is given twice")
    return tuple(sorted(paths))


def _check_for(values: Sequence[str]) -> tuple[str, ...]:
    out = []
    for value in values:
        project, slash, design = str(value).partition("/")
        try:
            out.append(f"{check_id(project)}/{check_id(design, 'design')}")
        except DesignError:
            raise SourceError("OAH_SOURCES_USAGE", f"--for {value!r}: use PROJECT/DESIGN IDs, such as acme/booking"
                              ) from None
        if not slash:
            raise SourceError("OAH_SOURCES_USAGE", f"--for {value!r}: use PROJECT/DESIGN")
    if len(out) > MAX_FOR:
        raise SourceError("OAH_SOURCES_LIMIT", f"a source names at most {MAX_FOR} designs")
    return tuple(sorted(set(out)))


def derived_id(locator: str) -> str | None:
    """A default ID from the repository name or the document host and last path segment."""
    parts = urllib.parse.urlsplit(locator)
    words = parts.path.rstrip("/").rsplit("/", 1)[-1] if parts.hostname == "github.com" else (
        f"{parts.hostname} {parts.path.rstrip('/').rsplit('/', 1)[-1]}")
    text = re.sub(r"[^a-z0-9]+", "-", words.lower()).strip("-")[:40].strip("-")
    try:
        return check_id(text)
    except DesignError:
        return None


# Registrations -------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    id: str
    provider: str
    track: str
    locator: str
    tag_prefix: str = ""
    channel: str = "stable"
    ref: str = ""
    paths: tuple[str, ...] = ()
    manifest_path: str | None = None
    for_designs: tuple[str, ...] = ()
    registration: str = ""
    registered_at: str = ""
    release_order: str = ""          # auto | semver | published for release tracking, "" otherwise

    def verification(self) -> dict:
        """Every input that decides what a snapshot of this source is, including the policy versions."""
        return {"provider": self.provider, "locator": self.locator, "track": self.track,
                "tag_prefix": self.tag_prefix, "channel": self.channel, "ref": self.ref, "paths": list(self.paths),
                "manifest_path": self.manifest_path, "release_order": self.release_order,
                "selection_policy": SELECTION_POLICY if self.track == "release" else None,
                "verification_policy": VERIFICATION_POLICY}

    @property
    def spec_digest(self) -> str:
        return digest_of(self.verification())

    def to_json(self) -> dict:
        return {**self.verification(), "id": self.id, "for": list(self.for_designs),
                "registration": self.registration, "registered_at": self.registered_at,
                "spec_digest": self.spec_digest}

    @classmethod
    def from_json(cls, data: object, where: str) -> "SourceSpec":
        if not isinstance(data, Mapping):
            raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"{where} is not an object")
        try:
            spec = build_spec(
                url=data.get("locator"), source_id=data.get("id"), track=data.get("track"),
                tag_prefix=data.get("tag_prefix") or "", prerelease=data.get("channel") == "prerelease",
                ref=_short_ref(data.get("ref")), paths=data.get("paths") or [],
                manifest_path=data.get("manifest_path"), for_designs=data.get("for") or [],
                allow_loopback=True, provider=data.get("provider"),
                release_order=data.get("release_order") or None)
        except (SourceError, TypeError, AttributeError) as exc:
            raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"{where} is not a valid registration ({exc})") from None
        token, when = data.get("registration"), data.get("registered_at")
        if (not isinstance(token, str) or not _TOKEN.fullmatch(token) or parse_iso(when) is None
                or data.get("channel") not in CHANNELS or data.get("spec_digest") != spec.spec_digest
                or spec.locator != data.get("locator") or data.get("selection_policy") != (
                    SELECTION_POLICY if spec.track == "release" else None)
                or data.get("verification_policy") != VERIFICATION_POLICY):
            raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"{where} has a malformed token, time or digest")
        return dataclasses.replace(spec, registration=token, registered_at=when)


def _short_ref(value: object) -> str | None:
    if value in (None, ""):
        return None
    if value == "HEAD":
        return "HEAD"
    if isinstance(value, str) and value.startswith("refs/heads/"):
        return value[len("refs/heads/"):]
    raise SourceError("OAH_SOURCES_USAGE", f"ref {value!r} is not HEAD or a branch")


def build_spec(*, url: object, source_id: object = None, track: object = None, tag_prefix: object = "",
               prerelease: bool = False, ref: object = None, paths: Sequence[str] = (), manifest_path: object = None,
               for_designs: Sequence[str] = (), allow_loopback: bool = False, provider: object = None,
               release_order: object = None) -> SourceSpec:
    """Validate a registration offline. `allow_loopback` is a test seam for document fixtures; never from config."""
    if not isinstance(url, str) or not url:
        raise _invalid("give a https://github.com/<owner>/<repo> or https:// document URL")
    is_github = provider == "github" or (provider is None and urllib.parse.urlsplit(url).hostname == "github.com")
    if provider not in (None, *PROVIDERS):
        raise _invalid(f"unknown provider {provider!r}")
    if is_github:
        locator, provider = github_locator(url), "github"
    else:
        locator, provider = doc_locator(url, allow_loopback=allow_loopback), "http"
    track = track or TRACKS[provider][0]
    if track not in TRACKS[provider]:
        raise SourceError("OAH_SOURCES_USAGE", f"a {provider} source tracks {' or '.join(TRACKS[provider])}, "
                          f"not {track!r}")
    if (not isinstance(tag_prefix, str) or not _TAG_PREFIX.fullmatch(tag_prefix) or ".." in tag_prefix
            or "@{" in tag_prefix):
        raise SourceError("OAH_SOURCES_USAGE", "--tag-prefix uses letters, digits, '.', '_', '@', '/' or '-', up to "
                          "64 (as in pkg-a@ or crates/foo-v)")
    if tag_prefix and track != "release":
        raise SourceError("OAH_SOURCES_USAGE", "--tag-prefix applies to release tracking only")
    if prerelease and track != "release":
        raise SourceError("OAH_SOURCES_USAGE", "--prerelease applies to release tracking only")
    if ref is not None and track != "commit":
        raise SourceError("OAH_SOURCES_USAGE", "--ref applies to commit tracking only")
    if release_order is not None and track != "release":
        raise SourceError("OAH_SOURCES_USAGE", "--release-order applies to release tracking only")
    if track == "release":
        release_order = release_order or "auto"
        if release_order not in RELEASE_ORDERS:
            raise SourceError("OAH_SOURCES_USAGE", f"--release-order is {', '.join(RELEASE_ORDERS)}")
    else:
        release_order = ""
    full_ref = ""
    if track == "commit":
        name = ref or "HEAD"
        if name != "HEAD" and (not isinstance(name, str) or not _BRANCH.fullmatch(name) or ".." in name
                               or name.endswith(("/", ".lock")) or "//" in name):
            raise SourceError("OAH_SOURCES_USAGE", f"--ref {name!r}: give HEAD or a branch name")
        full_ref = "HEAD" if name == "HEAD" else f"refs/heads/{name}"
    paths = tuple(paths or ())
    if len(paths) > MAX_PATHS:
        raise SourceError("OAH_SOURCES_LIMIT", f"a source selects at most {MAX_PATHS} paths")
    if provider == "http" and (paths or manifest_path):
        raise SourceError("OAH_SOURCES_USAGE", "--path and --manifest apply to GitHub sources only")
    checked_paths = _check_paths(paths, "--path")
    if manifest_path is not None:
        manifest_path = _check_paths([manifest_path], "--manifest")[0]
        if manifest_path.rsplit("/", 1)[-1] not in MANIFEST_NAMES:
            raise SourceError("OAH_SOURCES_USAGE", f"--manifest reads {', '.join(MANIFEST_NAMES)} only")
    if source_id is None:
        source_id = derived_id(locator)
        if source_id is None:
            raise SourceError("OAH_SOURCES_USAGE", "no ID can be made from this URL; give --id")
    try:
        source_id = check_id(source_id, "source")
    except DesignError as exc:
        raise SourceError("OAH_SOURCES_USAGE", str(exc).partition(": ")[2]) from None
    return SourceSpec(id=source_id, provider=provider, track=track, locator=locator, tag_prefix=tag_prefix,
                      channel="prerelease" if prerelease else "stable", ref=full_ref, paths=checked_paths,
                      manifest_path=manifest_path, for_designs=_check_for(for_designs), release_order=release_order)


def new_registration() -> str:
    return secrets.token_hex(16)


@dataclasses.dataclass(frozen=True)
class Registry:
    sources: Mapping[str, SourceSpec]
    problems: Mapping[str, str]   # source ID -> why its entry cannot be used (kept as is)

    def to_json(self, raw_problems: Mapping[str, object]) -> dict:
        entries = {key: spec.to_json() for key, spec in self.sources.items()}
        entries.update(raw_problems)  # unusable entries are written back exactly as they were read
        return {"version": 1, "kind": "oah-sources-registry", "sources": dict(sorted(entries.items()))}


def parse_registry(data: object, rel: str) -> tuple[Registry, dict]:
    """(registry, raw entries that are unusable). A file that is not a registry at all raises SourceError."""
    if data is None:
        return Registry({}, {}), {}
    if (not isinstance(data, Mapping) or data.get("version") != 1 or data.get("kind") != "oah-sources-registry"
            or not isinstance(data.get("sources"), Mapping)):
        raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"{rel} is not a version-1 sources registry; oah never "
                          "resets it. Move it aside to register sources again")
    sources, problems, raw = {}, {}, {}
    for key, value in data["sources"].items():
        try:
            spec = SourceSpec.from_json(value, f"{rel} entry {key!r}")
            if spec.id != key:
                raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"{rel} entry {key!r} names id {spec.id!r}")
            sources[key] = spec
        except SourceError as exc:
            problems[str(key)[:80]] = str(exc).partition(": ")[2]
            raw[key] = value
    return Registry(sources, problems), raw


# Heads ----------------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Pointer:
    snapshot: str
    at: str

    def to_json(self) -> dict:
        return {"snapshot": self.snapshot, "at": self.at}


@dataclasses.dataclass(frozen=True)
class Failure:
    code: str
    http: int | None
    count: int
    since: str

    def to_json(self) -> dict:
        return {"code": self.code, "http": self.http, "count": self.count, "since": self.since}


@dataclasses.dataclass(frozen=True)
class Head:
    id: str
    registration: str
    spec_digest: str
    generation: int = 0
    attempted_at: str | None = None
    confirmed_at: str | None = None
    outcome: str | None = None
    problems: tuple[str, ...] = ()
    failure: Failure | None = None
    retry_after: str | None = None
    verified: Pointer | None = None
    discovered: Pointer | None = None
    seen: str | None = None           # the last upstream commit or text digest an attempt saw
    observation: str | None = None    # the latest ObservationV1 record (establishment or confirmation)
    selection: str | None = None      # the latest attempt's release-selection record, when it made one

    def to_json(self) -> dict:
        return {"version": 1, "kind": "oah-source-head", "id": self.id, "registration": self.registration,
                "spec_digest": self.spec_digest, "generation": self.generation, "attempted_at": self.attempted_at,
                "confirmed_at": self.confirmed_at, "outcome": self.outcome, "problems": list(self.problems),
                "failure": None if self.failure is None else self.failure.to_json(),
                "retry_after": self.retry_after,
                "verified": None if self.verified is None else self.verified.to_json(),
                "discovered": None if self.discovered is None else self.discovered.to_json(),
                "seen": self.seen, "observation": self.observation, "selection": self.selection}


_HEAD_KEYS = {"version", "kind", "id", "registration", "spec_digest", "generation", "attempted_at", "confirmed_at",
              "outcome", "problems", "failure", "retry_after", "verified", "discovered", "seen", "observation",
              "selection"}


def _pointer(value: object, where: str) -> Pointer | None:
    if value is None:
        return None
    if (not isinstance(value, Mapping) or set(value) != {"snapshot", "at"} or hex_of(value["snapshot"]) is None
            or parse_iso(value["at"]) is None):
        raise SourceError("OAH_SOURCES_STATE_CORRUPT", f"{where} has a malformed pointer")
    return Pointer(value["snapshot"], value["at"])


def parse_head(data: object, where: str, source_id: str) -> Head:
    def bad(what: str) -> SourceError:
        return SourceError("OAH_SOURCES_STATE_CORRUPT", f"{where}: {what}")

    if not isinstance(data, Mapping) or data.get("version") != 1 or data.get("kind") != "oah-source-head":
        raise bad("not a version-1 source head")
    if set(data) != _HEAD_KEYS:
        raise bad("has missing or unknown fields")
    if data.get("id") != source_id or not isinstance(data.get("registration"), str) or not _TOKEN.fullmatch(
            data["registration"]) or hex_of(data.get("spec_digest")) is None:
        raise bad("id, registration or spec digest is malformed")
    generation = data.get("generation")
    if type(generation) is not int or not 0 <= generation < 10**12:
        raise bad("generation is not a whole number")
    for key in ("attempted_at", "confirmed_at", "retry_after"):
        if data.get(key) is not None and parse_iso(data[key]) is None:
            raise bad(f"{key} is not a UTC time")
    outcome = data.get("outcome")
    if outcome is not None and outcome not in OUTCOMES:
        raise bad("outcome is not a known word")
    problems = data.get("problems")
    if not isinstance(problems, list) or any(item not in PROBLEMS + FAILURES for item in problems):
        raise bad("problems hold an unknown code")
    failure = data.get("failure")
    if failure is not None:
        if (not isinstance(failure, Mapping) or set(failure) != {"code", "http", "count", "since"}
                or failure["code"] not in FAILURES or not (failure["http"] is None or type(failure["http"]) is int)
                or type(failure["count"]) is not int or failure["count"] < 1 or parse_iso(failure["since"]) is None):
            raise bad("failure is malformed")
        failure = Failure(failure["code"], failure["http"], failure["count"], failure["since"])
    seen = data.get("seen")
    if seen is not None and not (isinstance(seen, str) and (is_hex(seen) or re.fullmatch(r"[0-9a-f]{40}", seen))):
        raise bad("seen is malformed")
    for key in ("observation", "selection"):
        if data.get(key) is not None and hex_of(data[key]) is None:
            raise bad(f"{key} is not a record digest")
    return Head(id=source_id, registration=data["registration"], spec_digest=data["spec_digest"],
                generation=generation, attempted_at=data.get("attempted_at"), confirmed_at=data.get("confirmed_at"),
                outcome=outcome, problems=tuple(problems), failure=failure, retry_after=data.get("retry_after"),
                verified=_pointer(data.get("verified"), where), discovered=_pointer(data.get("discovered"), where),
                seen=seen, observation=data.get("observation"), selection=data.get("selection"))


# Observations and the transition ----------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Observation:
    """What one attempt found. kind: unchanged | snapshot | blocked | failed."""

    kind: str
    snapshot: str | None = None        # manifest digest (kind snapshot)
    verified: bool = False             # kind snapshot: identity checks passed
    problems: tuple[str, ...] = ()
    code: str | None = None            # kind failed
    http: int | None = None
    retry_after: dt.datetime | None = None
    seen: str | None = None
    selection: str | None = None       # the release-selection record this attempt stored, if any


def observation_record(head: Head, *, outcome: str, started: str, now: str, selection: str | None) -> dict:
    """ObservationV1 for an establishment or confirmation recorded in `head` (already transitioned)."""
    if head.verified is None or head.confirmed_at is None:
        raise ValueError("an observation record needs a verified, confirmed head")
    return {"version": 1, "kind": "oah-source-observation",
            "source": {"id": head.id, "registration": head.registration, "spec_digest": head.spec_digest},
            "attempted_at": started, "observed_at": now, "outcome": outcome, "confirmed_at": head.confirmed_at,
            "verified_at": head.verified.at, "snapshot": head.verified.snapshot, "head_generation": head.generation,
            "selection_observation": selection, "verification_policy": VERIFICATION_POLICY,
            "previous": head.observation}


_OBSERVATION_KEYS = {"version", "kind", "source", "attempted_at", "observed_at", "outcome", "confirmed_at",
                     "verified_at", "snapshot", "head_generation", "selection_observation", "verification_policy",
                     "previous"}


def parse_observation(data: object, where: str) -> dict:
    """An ObservationV1 record validated exactly; SourceError(OAH_SOURCES_STATE_CORRUPT) otherwise."""
    def bad(what: str) -> SourceError:
        return SourceError("OAH_SOURCES_STATE_CORRUPT", f"{where}: {what}")

    if not isinstance(data, Mapping) or set(data) != _OBSERVATION_KEYS or data.get("version") != 1 \
            or data.get("kind") != "oah-source-observation":
        raise bad("is not a version-1 source observation")
    source = data["source"]
    if (not isinstance(source, Mapping) or set(source) != {"id", "registration", "spec_digest"}
            or not isinstance(source["registration"], str) or not _TOKEN.fullmatch(source["registration"])
            or hex_of(source["spec_digest"]) is None or not isinstance(source["id"], str)):
        raise bad("source is malformed")
    if data["outcome"] not in ("established", "confirmed", "recovered"):
        raise bad("outcome is not a known word")
    for key in ("attempted_at", "observed_at", "confirmed_at", "verified_at"):
        if parse_iso(data[key]) is None:
            raise bad(f"{key} is not a UTC time")
    if hex_of(data["snapshot"]) is None or type(data["head_generation"]) is not int or data["head_generation"] < 1:
        raise bad("snapshot or generation is malformed")
    for key in ("selection_observation", "previous"):
        if data[key] is not None and hex_of(data[key]) is None:
            raise bad(f"{key} is not a record digest")
    if data["verification_policy"] != VERIFICATION_POLICY:
        raise bad("names an unknown verification policy")
    return dict(data)


def transition(head: Head, observation: Observation, now: dt.datetime,
               started: dt.datetime | None = None) -> tuple[Head, str]:
    """The head after one observation and the result word (unchanged | recovered | changed | unverified | blocked |
    failed). `started` is when the attempt began (its `attempted_at`); confirmation times are `now`. Pure: the caller
    stores the ObservationV1 record (`observation_record`, `record_word`) and commits the returned head."""
    at = iso(now)
    troubled = head.outcome in ("failed", "unverified", "blocked")
    base = dataclasses.replace(head, generation=head.generation + 1, attempted_at=iso(started or now),
                               selection=observation.selection)
    if observation.kind == "failed":
        code = observation.code if observation.code in FAILURES else "network"
        streak = head.failure is not None and head.outcome == "failed"
        failure = Failure(code, observation.http, head.failure.count + 1 if streak else 1,
                          head.failure.since if streak else at)
        retry = iso(observation.retry_after) if observation.retry_after is not None else None
        return dataclasses.replace(base, outcome="failed", problems=(code,), failure=failure,
                                   retry_after=retry), "failed"
    if observation.kind == "blocked":
        problems = tuple(p for p in observation.problems if p in PROBLEMS) or ("selection-incomplete",)
        return dataclasses.replace(base, outcome="blocked", problems=problems, failure=None,
                                   retry_after=None), "blocked"
    if observation.kind == "unchanged":
        if head.verified is None:
            raise ValueError("an unchanged observation needs a verified snapshot")
        return dataclasses.replace(base, outcome="unchanged", problems=(), failure=None, retry_after=None,
                                   confirmed_at=at, discovered=head.verified, seen=observation.seen or head.seen
                                   ), "recovered" if troubled else "unchanged"
    if observation.kind != "snapshot" or observation.snapshot is None:
        raise ValueError(f"unknown observation {observation.kind!r}")
    pointer = Pointer(observation.snapshot, at)
    previous_verified = head.verified.snapshot if head.verified else None
    if observation.verified:
        if observation.snapshot == previous_verified:
            return dataclasses.replace(base, outcome="unchanged", problems=(), failure=None, retry_after=None,
                                       confirmed_at=at, discovered=head.verified, seen=observation.seen or head.seen
                                       ), "recovered" if troubled else "unchanged"
        return dataclasses.replace(base, outcome="changed", problems=(), failure=None, retry_after=None,
                                   confirmed_at=at, verified=pointer, discovered=pointer, seen=observation.seen
                                   ), "changed"
    problems = tuple(p for p in observation.problems if p in PROBLEMS) or ("ref-mismatch",)
    same = head.discovered is not None and head.discovered.snapshot == observation.snapshot
    return dataclasses.replace(base, outcome="unverified", problems=problems, failure=None, retry_after=None,
                               discovered=head.discovered if same else pointer, seen=observation.seen
                               ), "unverified"


def record_word(word: str) -> str | None:
    """The ObservationV1 outcome a result word leaves, or None when it confirms nothing."""
    return {"changed": "established", "unchanged": "confirmed", "recovered": "recovered"}.get(word)


def freshness(head: Head | None, now: dt.datetime, fresh_hours: float) -> str:
    """unknown | fresh | stale | unverified | unavailable, from the head alone."""
    if head is None or head.attempted_at is None:
        return "unknown"
    if head.outcome == "failed":
        return "unavailable"
    if head.outcome in ("unverified", "blocked"):
        return "unverified"
    confirmed = parse_iso(head.confirmed_at)
    if head.verified is None or confirmed is None:
        return "unknown"
    return "fresh" if dt.timedelta(0) <= now - confirmed <= dt.timedelta(hours=fresh_hours) else "stale"
