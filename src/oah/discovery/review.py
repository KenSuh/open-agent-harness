# 스킬 검토 — GitHub 스킬 폴더를 C 의 출처 수집기로 별도 상태 폴더에 고정하고, 앞부분·라이선스·호환·참조·지시문 표시를 데이터로 기록한다.
"""`review`: pin one GitHub skill folder through C's collector and record ReviewedSkillV1.

The folder is registered in `Sources(<state>/skills)` (a root apart from the user's followed design sources) with
E's source id: `sk-` and the first 37 hex digits of the SHA-256 of the normalized spec fields plus the review policy
version. The full 256-bit identity is remembered beside it, so a truncated collision refuses instead of overwriting.
C's bounded HTTP, address vetting, size caps, path checks, immutable snapshots and strict license classifier apply.
The body is read as data: instruction-like lines are flagged, never followed; a `<!-- oah` line makes the skill
undeliverable inline. Nothing is installed, executed or delivered here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Mapping

from oah.design.sources.adapters import Transport
from oah.design.sources.check import Sources
from oah.design.sources.model import SourceError, build_spec
from oah.discovery import store

POLICY_VERSION = "oah-skill-review/v1"
SKILL_LIMIT = 196608
_REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_LITERAL = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
TRANSPORT: Transport | None = None  # tests point the collector at local fixtures; production uses Transport()
CLAUDE_ONLY_TOOLS = {"Task", "TodoWrite", "TodoRead", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Skill"}
_FLAGS = (("ignore-instructions", re.compile(r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|earlier)\b"
                                             r".{0,20}\b(instructions?|prompts?|rules?)\b", re.IGNORECASE)),
          ("role-override", re.compile(r"\byou are now\b|\bnew system prompt\b", re.IGNORECASE)),
          ("shell-pipe", re.compile(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b", re.IGNORECASE)),
          ("destructive-shell", re.compile(r"^\s*(\$\s*)?(sudo\s+)?rm\s+-rf\b", re.IGNORECASE)))
_REFERENCE = re.compile(r"\]\(([^)\s#]+)\)|`((?:references|scripts|assets|templates|examples)/[^`\s]+)`"
                        r"|\b((?:references|scripts|assets|templates|examples)/[A-Za-z0-9._/-]+\.[A-Za-z0-9]+)")


class SourceUnsupported(Exception):
    code = "OAH_SKILL_SOURCE_UNSUPPORTED"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class ReviewFailed(Exception):
    """OAH_SKILL_REVIEW_FAILED(<reason>): exit 1 for the skill's own problems, 69 when the collector environment fails."""

    code = "OAH_SKILL_REVIEW_FAILED"

    def __init__(self, reason: str, detail: str, *, environment: bool = False, record: Mapping | None = None) -> None:
        self.reason, self.environment, self.record = reason, environment, record
        super().__init__(f"{self.code}({reason}): {detail}")


def license_allow(skills_settings: Mapping, sources_settings: Mapping | None = None) -> list[str]:
    allow = list((skills_settings.get("discovery") or {}).get("license_allow") or [])
    return allow or list((sources_settings or {}).get("license_allow") or ["MIT"])


def skill_sources(state_dir, allow: list[str]) -> Sources:
    return Sources(store.root(state_dir), settings={"license_allow": allow, "auto_check": False},
                   transport=TRANSPORT or Transport())


def identity(repo: str, path: str) -> tuple[str, str, dict]:
    """(E source id, full identity, normalized fields) for a GitHub repository folder tracked by commit."""
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        raise SourceUnsupported(f"{repo!r} is not an owner/repo GitHub name; only GitHub skill folders are reviewed")
    if not isinstance(path, str) or not _LITERAL.fullmatch(path) or ".." in path.split("/"):
        raise SourceUnsupported(f"{path!r} is not a literal folder path inside the repository")
    try:
        provisional = build_spec(url=f"https://github.com/{repo}", source_id="sk-normalize", track="commit",
                                 paths=[path])
    except SourceError as exc:
        raise SourceUnsupported(str(exc)) from None
    fields = {"provider": provisional.provider, "locator": provisional.locator, "track": provisional.track,
              "ref": provisional.ref, "paths": list(provisional.paths), "manifest_path": provisional.manifest_path,
              "verification_policy_version": POLICY_VERSION}
    full = hashlib.sha256(store.canonical(fields)).hexdigest()
    return "sk-" + full[:37], "sha256:" + full, fields


def _frontmatter(text: str) -> dict:
    from oah.skillsel import parse_frontmatter

    data = parse_frontmatter(text)
    tools = data.get("allowed-tools")
    tool_list = None if not tools else [item for item in re.split(r"[,\s]+", tools) if item][:32]

    def flag(key: str) -> bool | None:
        value = data.get(key)
        if value is None or value == "":
            return None
        return {"true": True, "false": False}.get(value.strip().lower())

    disabled = flag("disable-model-invocation")
    user = flag("user-invocable")
    return {"name": data.get("name") or None, "description": (data.get("description") or None),
            "model_invocable": None if disabled is None and "disable-model-invocation" in data else (not disabled
                                                                                                  if disabled is not None
                                                                                                  else True),
            "user_invocable": True if user is None else user, "allowed_tools": tool_list}


def screening(text: str) -> dict:
    flags = []
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        for kind, pattern in _FLAGS:
            if pattern.search(line):
                flags.append({"line": number, "kind": kind})
    collision = any(line.lstrip().casefold().startswith("<!-- oah") for line in text.split("\n"))
    return {"flags": flags[:64], "marker_collision": collision}


def references(text: str, folder: str, present: set[str]) -> list[dict]:
    found: dict[str, bool] = {}
    for match in _REFERENCE.finditer(text):
        ref = next(group for group in match.groups() if group)
        if re.match(r"[a-z][a-z0-9+.-]*:", ref) or ref.startswith(("/", "#")) or ".." in ref.split("/"):
            continue
        ref = ref.lstrip("./")
        if not ref or ref == "SKILL.md":
            continue
        found.setdefault(ref, f"{folder}/{ref}" in present)
    return [{"path": path, "present": ok} for path, ok in sorted(found.items())][:64]


def review(state_dir, *, repo: str, path: str, candidate: Mapping, allow: list[str],
           now: dt.datetime | None = None, sources: Sources | None = None) -> tuple[dict, str]:
    """Pin, read and classify one skill folder; (ReviewedSkillV1, 'stored' | 'unchanged').

    A review whose snapshot, candidate, classification and policy equal a stored one returns that record unchanged,
    so repeating a review (or `--update` with no upstream change) adds nothing."""
    short, full, fields = identity(repo, path)
    try:
        store.remember_identity(state_dir, short, full, fields)
    except store.StoreProblem as exc:
        raise ReviewFailed("identity-collision", str(exc)) from None
    sources = sources or skill_sources(state_dir, allow)
    try:
        _word, spec = sources.register(build_spec(url=f"https://github.com/{repo}", source_id=short, track="commit",
                                                  paths=[path]))
        result = sources.check_one(spec)
    except SourceError as exc:
        raise ReviewFailed("collector", str(exc), environment=True) from None
    if "path-missing" in result.problems:
        sources.remove(short)
        raise ReviewFailed("path-unresolved", f"{repo} has no folder {path} at its current commit")
    head = sources.head(spec)
    if head is None or head.verified is None:
        raise ReviewFailed("collector", f"the collector could not verify {repo} ({result.result}: "
                           f"{', '.join(result.problems) or 'no snapshot'})", environment=True)
    snapshot_id = head.verified.snapshot
    manifest = sources.snapshot(snapshot_id) or {}
    files = [item for item in manifest.get("files", []) if str(item.get("path", "")).startswith(path + "/")]
    skill_file = next((item for item in files if item["path"] == f"{path}/SKILL.md"), None)
    if skill_file is None:
        raise ReviewFailed("path-unresolved", f"{repo}/{path} has no SKILL.md")
    raw = sources.blob(skill_file["sha256"]) or b""
    text = raw.decode("utf-8", "replace")
    lf = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    front = _frontmatter(text)
    verdict, eligibility = sources.verdict(manifest), sources.eligibility(manifest)
    present = {item["path"] for item in files}
    refs = references(text, path, present)
    screen = screening(text)
    tools = front["allowed_tools"] or []
    hosts = ["claude"] if any(tool.split("(", 1)[0] in CLAUDE_ONLY_TOOLS for tool in tools) else ["claude", "codex"]
    problems = [f"missing-reference: {item['path']}" for item in refs if not item["present"]]
    hit = candidate.get("hit") if isinstance(candidate, Mapping) else None
    status, reason = "reviewed", None
    if not front["name"] or not front["description"]:
        status, reason = "rejected", "malformed-frontmatter"
    elif len(raw) > SKILL_LIMIT:
        status, reason = "rejected", "too-large"
    elif hit and hit.get("name") and hit["name"] != front["name"]:
        status, reason = "rejected", "name-mismatch"
    if hit and hit.get("name") and hit["name"] != front["name"]:
        problems.append("name-mismatch")
    if screen["marker_collision"]:
        problems.append("marker-collision: not deliverable inline")
    observed = manifest.get("observed") or {}
    record = {
        "version": 1,
        "candidate": {"provider": candidate.get("provider", "github"), "query_id": candidate.get("query_id"),
                      "hit": hit, "seed": candidate.get("seed")},
        "source": {"provider": "github", "source_id": short, "spec_digest": spec.spec_digest, "locator": spec.locator,
                   "commit": observed.get("commit"), "tree": observed.get("tree"), "path": path,
                   "snapshot": snapshot_id, "skill_sha256": "sha256:" + skill_file["sha256"],
                   "skill_lf_sha256": "sha256:" + hashlib.sha256(lf).hexdigest(),
                   "files": [{"path": item["path"], "bytes": item.get("size"), "sha256": "sha256:" + item["sha256"]}
                             for item in files][:256],
                   "references": refs},
        "frontmatter": front,
        "license": {"verdict": "recognized" if verdict.expression else "unknown", "expression": verdict.expression,
                    "form": verdict.form, "eligibility": eligibility.status,
                    "governing": [{"path": item["path"], "sha256": "sha256:" + item["sha256"]}
                                  for item in (manifest.get("license") or {}).get("files", [])][:16]},
        "compatibility": {"hosts": hosts, "requires_tools": tools, "scripts_present":
                          any(item["path"].startswith(f"{path}/scripts/") for item in files), "problems": problems},
        "screening": screen,
        "status": status, "reason": reason,
        "checked_at": (now or sources.now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "policy_digest": store.digest({"policy": POLICY_VERSION, "license_allow": sorted(allow)}),
    }
    for existing in store.reviews_of(state_dir, spec.locator):
        if {key: value for key, value in existing.items() if key not in ("id", "checked_at")} == \
                {key: value for key, value in record.items() if key != "checked_at"}:
            return existing, "unchanged"
    _word, stored = store.put_review(state_dir, record)
    return stored, "stored"


def hit_paths(name: str) -> list[str]:
    """The literal folders a search hit may live in (`skills/<name>`, then `<name>`); a non-literal name has none."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in (".", ".."):
        return []
    return [f"skills/{name}", name]


def review_hit(state_dir, *, hit: Mapping, candidate: Mapping, allow: list[str]) -> tuple[dict, str]:
    """Review a search hit: an owner/repo source and at most the literal guesses of `hit_paths`."""
    source = hit.get("source")
    if not isinstance(source, str) or not _REPO.fullmatch(source):
        raise SourceUnsupported(f"the hit's source {source!r} is not an owner/repo GitHub name")
    for path in hit_paths(hit.get("name")):
        try:
            return review(state_dir, repo=source, path=path, candidate=candidate, allow=allow)
        except ReviewFailed as exc:
            if exc.reason != "path-unresolved":
                raise
    raise ReviewFailed("path-unresolved", f"{source} has no skill folder for {hit.get('name')!r} at skills/<name> or "
                       "<name>; review it with --github OWNER/REPO --path PATH")


def repo_of(locator: str) -> str:
    prefix = "https://github.com/"
    if not isinstance(locator, str) or not locator.startswith(prefix):
        raise SourceUnsupported(f"{locator!r} is not a GitHub locator")
    return locator[len(prefix):].removesuffix(".git").strip("/")


def body(state_dir, record: Mapping, allow: list[str]) -> bytes | None:
    """The reviewed SKILL.md bytes from E's immutable snapshot store."""
    digest = str((record.get("source") or {}).get("skill_sha256", ""))
    if not digest.startswith("sha256:"):
        return None
    return skill_sources(state_dir, allow).blob(digest.split(":", 1)[1])
