# 작업 스킬 필요 — 수락된 패킷·하위 작업 범위·공개 기술 표지로 TaskSkillNeedV1 을 만들고, 탐색 결과(TaskDiscoveryV1)와 리드의 선택(TaskSkillSelectionV1)을 남긴다.
"""Task skill needs (DESIGN §4.1, §4.8): the wire between a dispatch's admission and discovery.

TaskSkillNeedV1 names one task's (or subtask's) skill need: its packet and intent, dispatch stage, kind, risk and
scope, the byte range of the packet that states the requirement (never the text itself), the design stage and check
when the task is bound to a design, the public technology labels, the platform and a digest of local compatibility
constraints. Its id is the digest of every other field, so a changed requirement, scope, version, platform or policy
is another need. `TaskBasis` holds the current facts; `need` builds a need from them; `parse_needs` re-resolves a
lead's needs file and refuses anything that no longer matches. `prepare_task` runs the bounded search for admitted
needs and stores TaskDiscoveryV1; `select` stores a lead's TaskSkillSelectionV1 for a task without a design.
Nothing here installs, executes or delivers a skill; delivery stays with skillpack's admission.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
from typing import Callable, Mapping, Sequence

from oah.design import stages
from oah.discovery import providers, store

NEED_VERSION = 1
NEEDS_LIMIT = 64
NEEDS_BYTES = 65536
REVIEWS_LIMIT = 16
INVALID_CODE = "OAH_SKILL_NEEDS_INVALID"
SOURCES = ("packet-intent", "packet-instructions")
PLATFORMS = {"darwin": "macos", "win32": "windows", "linux": "linux"}
DISCOVERY, SELECTIONS = "task-discovery", "task-selections"
NEED_KEYS = frozenset({"version", "id", "task", "subtask", "dispatch_stage", "kind", "risk", "packet", "scope",
                       "requirement", "design", "tech", "platform", "compatibility", "required", "policy_digest"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PLACEHOLDER = re.compile(r"\{([a-z-]+)\}")


class NeedsInvalid(ValueError):
    """OAH_SKILL_NEEDS_INVALID(<reason>): a needs file or selection that does not match the task as it is now."""

    code = INVALID_CODE

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{INVALID_CODE}({reason}): {detail}")


@dataclasses.dataclass(frozen=True)
class TaskBasis:
    """The current, verified facts a task's needs are built and re-resolved from (never read from a needs file)."""

    task: str
    text: str  # the packet source as oah read it (line endings as stored)
    intent: Mapping  # {id, revision, digest} of the lease's intent binding
    scope: tuple[str, ...]
    axes: Mapping  # dispatch stage -> (kind, risk)
    lease: tuple[str, int]
    design: Mapping | None = None  # {ref, coverage_basis, record} of the accepted coverage of a design-bound task
    platform: str = PLATFORMS.get(sys.platform, "any")
    bound: bool = False  # the task is bound to a design (with or without an accepted coverage part)

    @property
    def source_digest(self) -> str:
        return _sha(self.text.encode("utf-8"))

    def section(self, source: str) -> tuple[int, int]:
        """(offset, length) in UTF-8 bytes of the intent section or of the packet text after it."""
        lines = self.text.splitlines(keepends=True)
        marks = [n for n, line in enumerate(lines) if line.rstrip("\r\n") in ("<!-- OAH_INTENT:BEGIN -->",
                                                                             "<!-- OAH_INTENT:END -->")]
        if len(marks) != 2:
            raise NeedsInvalid("stale", "the packet has no single intent section")
        begin, end = marks
        size = lambda part: len("".join(part).encode("utf-8"))  # noqa: E731
        if source == "packet-intent":
            return size(lines[:begin]), size(lines[begin:end + 1])
        return size(lines[:end + 1]), size(lines[end + 1:])

    def requirement(self, source: str, offset: int | None = None, length: int | None = None) -> dict:
        start, total = self.section(source)
        offset = start if offset is None else offset
        length = total if length is None else length
        data = self.text.encode("utf-8")
        if not (start <= offset and length >= 1 and offset + length <= start + total):
            raise NeedsInvalid("stale", f"the requirement range {offset}+{length} is outside the packet's {source}")
        return {"source": source, "offset": offset, "length": length, "sha256": _sha(data[offset:offset + length])}


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def design_of(task_design) -> dict | None:
    """{ref, coverage_basis, record} for a task bound to a design with an accepted coverage part, else None."""
    from oah import skillpack

    record, accepted, ref = skillpack._accepted_coverage(task_design)
    if record is None:
        return None
    return {"ref": dict(ref), "coverage_basis": _sha(accepted.texts["coverage"].encode("utf-8")), "record": record}


def tech_entries(pairs: Sequence[tuple[str, str]]) -> list[dict]:
    """Closed technology labels; a public spelling becomes a public token, anything else stays local."""
    out, seen = [], set()
    for label, value in pairs:
        if label not in stages.TECH_LABELS or label in seen:
            raise NeedsInvalid("malformed", f"technology label {label!r} is unknown or named twice")
        if value not in stages.TECH_LABELS[label] and not stages.LOCAL_VALUE.fullmatch(str(value)):
            raise NeedsInvalid("malformed", f"{label} is one of {', '.join(stages.TECH_LABELS[label])} or "
                               "other:<id>")
        seen.add(label)
        public = value if value in stages.PUBLIC_QUERY_TERMS and value not in ("none", "other") else None
        out.append({"label": label, "value": value, "public_token": public, "version_token": None})
    return sorted(out, key=lambda entry: entry["label"])


def _within(path: str, scope: Sequence[str]) -> bool:
    if path in scope or "repo" in scope:
        return True
    pure = pathlib.PurePosixPath(path)
    return any(pure == pathlib.PurePosixPath(entry) or pathlib.PurePosixPath(entry) in pure.parents
               for entry in scope)


def need(basis: TaskBasis, *, dispatch_stage: str, policy: str, subtask: str | None = None,
         requirement: Mapping | None = None, tech: Sequence[tuple[str, str]] = (), constraints: Sequence[str] = (),
         scope: Sequence[str] | None = None, required: bool = False, stage: str | None = None,
         check: str | None = None) -> dict:
    """A TaskSkillNeedV1 built from the basis; the requirement is a byte range of the current packet."""
    if dispatch_stage not in basis.axes:
        raise NeedsInvalid("malformed", f"dispatch stage {dispatch_stage!r} is one of {', '.join(basis.axes)}")
    if subtask is not None and not _ID.fullmatch(subtask):
        raise NeedsInvalid("malformed", "a subtask id is 1-64 letters, digits, dots, underscores or hyphens")
    entries = tech_entries(tech)
    runtime = next((entry["public_token"] for entry in entries if entry["label"] == "runtime"), None)
    kind, risk = basis.axes[dispatch_stage]
    design = None
    if basis.design is not None:
        if stage not in stages.BY_ID or (check is not None and check not in stages.all_checks(stage)):
            raise NeedsInvalid("malformed", "a design-bound task's need names its design stage and one of that "
                               "stage's checks")
        design = {"ref": dict(basis.design["ref"]), "coverage_basis": basis.design["coverage_basis"],
                  "stage": stage, "check": check}
    elif stage is not None or check is not None:
        raise NeedsInvalid("malformed", "a task without a design names no design stage or check")
    paths = sorted(set(scope if scope is not None else basis.scope))
    if not paths or not all(isinstance(path, str) and _within(path, basis.scope) for path in paths):
        raise NeedsInvalid("scope", f"a need's scope is inside the packet's scope ({', '.join(basis.scope)})")
    req = dict(requirement) if requirement is not None else basis.requirement("packet-intent")
    record = {"version": NEED_VERSION, "task": basis.task, "subtask": subtask, "dispatch_stage": dispatch_stage,
              "kind": kind, "risk": risk,
              "packet": {"source_digest": basis.source_digest, "intent": dict(basis.intent)},
              "scope": paths, "requirement": req, "design": design, "tech": entries, "platform": basis.platform,
              "compatibility": {"runtime": runtime, "constraints_digest": store.digest(sorted(set(constraints)))},
              "required": bool(required), "policy_digest": policy}
    return store.with_id(record)


def derive(basis: TaskBasis, *, dispatch_stage: str, policy: str) -> list[dict]:
    """The needs an ordinary call derives by itself: one per (stage, check) the accepted coverage asks a skill for at
    this dispatch stage, with the design's technology values; for a task without a design, one need for its intent
    and no technology label, so nothing is searched outside the machine."""
    if basis.design is None:
        return [] if basis.bound else [need(basis, dispatch_stage=dispatch_stage, policy=policy)]
    record = basis.design["record"]
    tech = {label: entry.get("value") for label, entry in (record.get("tech") or {}).items()
            if isinstance(entry, Mapping) and isinstance(entry.get("value"), str)}
    wanted: dict[tuple[str, str | None], bool] = {}
    for row in record.get("stages", ()):
        for item in row.get("needs", ()):
            if dispatch_stage not in item.get("dispatch_stages", ()) or item.get("invocation") == "user-only":
                continue
            if stages.condition_holds(item.get("condition", "always"), record) is False:
                continue
            key = (row["id"], item.get("check"))
            wanted[key] = wanted.get(key, False) or bool(item.get("required"))
    out = []
    for (stage, check), required in wanted.items():
        labels = sorted({label for template in stages.QUERY_TEMPLATES.get((stage, check), ())
                         for label in _PLACEHOLDER.findall(template)})
        pairs = [(label, tech[label]) for label in labels if label in tech]
        out.append(need(basis, dispatch_stage=dispatch_stage, policy=policy, tech=pairs, required=required,
                        stage=stage, check=check))
    return out[:NEEDS_LIMIT]


def verify(item: object, basis: TaskBasis, *, policy: str) -> dict:
    """Re-resolve one lead-supplied TaskSkillNeedV1: every field must be what the current task and packet give."""
    if not isinstance(item, dict) or set(item) != NEED_KEYS:
        raise NeedsInvalid("malformed", "a need has exactly the TaskSkillNeedV1 fields")
    if item["version"] != NEED_VERSION:
        raise NeedsInvalid("malformed", f"need version {item['version']!r} is not {NEED_VERSION}")
    if item["task"] != basis.task:
        raise NeedsInvalid("foreign", f"the need is for task {item['task']!r}, not {basis.task}")
    packet = item["packet"]
    if packet != {"source_digest": basis.source_digest, "intent": dict(basis.intent)}:
        raise NeedsInvalid("stale", "the packet or its accepted intent changed since the need was written; derive "
                           "the needs again with oah skills find --task")
    req = item["requirement"]
    if not (isinstance(req, dict) and set(req) == {"source", "offset", "length", "sha256"} and req["source"] in SOURCES
            and type(req["offset"]) is int and type(req["length"]) is int):
        raise NeedsInvalid("malformed", "requirement is {source, offset, length, sha256} over packet-intent or "
                           "packet-instructions")
    current = basis.requirement(req["source"], req["offset"], req["length"])
    if current["sha256"] != req["sha256"]:
        raise NeedsInvalid("stale", "the packet bytes at the requirement range changed; oah never takes replacement "
                           "text from a needs file")
    tech = item["tech"]
    if not isinstance(tech, list) or not all(isinstance(entry, dict) and set(entry) == {"label", "value",
                                                                                         "public_token",
                                                                                         "version_token"}
                                             for entry in tech):
        raise NeedsInvalid("malformed", "tech is a list of {label, value, public_token, version_token}")
    if any(entry["version_token"] is not None for entry in tech):
        raise NeedsInvalid("malformed", "this release catalogues no public version terms; keep versions in local "
                           "constraints")
    design = item["design"]
    stage = check = None
    if design is not None:
        if not (isinstance(design, dict) and set(design) == {"ref", "coverage_basis", "stage", "check"}):
            raise NeedsInvalid("malformed", "design is {ref, coverage_basis, stage, check} or null")
        stage, check = design["stage"], design["check"]
        if basis.design is None or design["ref"] != basis.design["ref"] \
                or design["coverage_basis"] != basis.design["coverage_basis"]:
            raise NeedsInvalid("stale", "the task's accepted design or its coverage changed since the need was "
                               "written")
    elif basis.design is not None:
        raise NeedsInvalid("stale", "the task is now bound to an accepted design; derive its needs again")
    compatibility = item["compatibility"]
    if not (isinstance(compatibility, dict) and set(compatibility) == {"runtime", "constraints_digest"}
            and isinstance(compatibility["constraints_digest"], str)
            and _DIGEST.fullmatch(compatibility["constraints_digest"])):
        raise NeedsInvalid("malformed", "compatibility is {runtime, constraints_digest}")
    if item["platform"] != basis.platform:
        raise NeedsInvalid("stale", f"the need is for platform {item['platform']!r}, this machine is {basis.platform}")
    if item["policy_digest"] != policy:
        raise NeedsInvalid("stale", "the discovery settings changed since the need was written")
    if type(item["required"]) is not bool or not isinstance(item["scope"], list):
        raise NeedsInvalid("malformed", "required is true or false and scope is a list of paths")
    rebuilt = need(basis, dispatch_stage=item["dispatch_stage"], policy=policy, subtask=item["subtask"],
                   requirement=current, tech=[(entry["label"], entry["value"]) for entry in tech],
                   scope=item["scope"], required=item["required"], stage=stage, check=check)
    rebuilt["compatibility"]["constraints_digest"] = compatibility["constraints_digest"]  # local, kept as given
    rebuilt = store.with_id({key: value for key, value in rebuilt.items() if key != "id"})
    if rebuilt != item:
        raise NeedsInvalid("stale", "the need does not match the task as it is now (kind, risk, scope, technology "
                           "or its id); derive the needs again with oah skills find --task")
    return rebuilt


def parse_needs(raw: bytes, basis: TaskBasis, *, policy: str) -> list[dict]:
    """A lead's needs file {version, task, needs} re-resolved against the current task (at most 64 needs, 64 KiB)."""
    from oah.design.sources.state import StateProblem, loads

    if len(raw) > NEEDS_BYTES:
        raise NeedsInvalid("malformed", f"a needs file is at most {NEEDS_BYTES} bytes")
    try:
        data = loads(raw, "needs file")
    except StateProblem as exc:
        raise NeedsInvalid("malformed", exc.detail) from None
    return resolve_needs(data, basis, policy=policy)


def resolve_needs(data: object, basis: TaskBasis, *, policy: str) -> list[dict]:
    if not (isinstance(data, dict) and set(data) == {"version", "task", "needs"} and data["version"] == 1
            and isinstance(data["needs"], list) and 0 < len(data["needs"]) <= NEEDS_LIMIT):
        raise NeedsInvalid("malformed", f"a needs file is {{version: 1, task, needs: [1 to {NEEDS_LIMIT} needs]}}")
    if data["task"] != basis.task:
        raise NeedsInvalid("foreign", f"the needs file is for task {data['task']!r}, not {basis.task}")
    out = [verify(item, basis, policy=policy) for item in data["needs"]]
    if len({item["id"] for item in out}) != len(out):
        raise NeedsInvalid("malformed", "the needs file names one need twice")
    return out


def needs_file(task: str, needs: Sequence[Mapping]) -> dict:
    return {"version": 1, "task": task, "needs": list(needs)}


def queries(item: Mapping) -> tuple[list[dict], list[dict]]:
    """(public queries, local-only templates) for one need. A design-bound need uses its stage and check's templates;
    a need without a design uses every template whose placeholders its public technology tokens fill."""
    tokens = {entry["label"]: entry["public_token"] for entry in item["tech"]}
    design = item["design"]
    if design is not None:
        templates = list(stages.QUERY_TEMPLATES.get((design["stage"], design["check"]), ()))
    else:
        templates = [template for group in stages.QUERY_TEMPLATES.values() for template in group
                     if _PLACEHOLDER.findall(template)
                     and any(label in tokens for label in _PLACEHOLDER.findall(template))]
    out, local, seen = [], [], set()
    for template in templates:
        labels = _PLACEHOLDER.findall(template)
        missing = [label for label in labels if not tokens.get(label)]
        if missing:
            local.append({"template": template, "reason": "public-term-required", "labels": missing})
            continue
        text = template
        for label in labels:
            text = text.replace("{" + label + "}", tokens[label])
        if text in seen:
            continue
        seen.add(text)
        if providers.is_public(text):
            out.append({"text": text, "used": {label: tokens[label] for label in labels}})
        else:
            local.append({"template": template, "reason": "public-term-required", "labels": labels})
    return out, local


def answerable(item: Mapping) -> bool:
    """Whether discovery has anything to do for a need: a public query, a template that waits for a public term, or
    the design stage and check of a design-bound need. A need with neither stays out of the store."""
    public, local = queries(item)
    return bool(public or local or item["design"] is not None)


def installed_terms(needs: Sequence[Mapping]) -> list[str]:
    """Words for the local installed-skill match: the public query words and every technology value, local ones too
    (this match never leaves the machine)."""
    words = {word for item in needs for query in queries(item)[0] for word in query["text"].split()}
    words |= {entry["value"].removeprefix("other:") for item in needs for entry in item["tech"]
              if entry["value"] not in ("none", "other")}
    return sorted(words)


def _outcome(searches: list[dict], deferred: list[dict], public: list[dict]) -> str:
    failures = [item["outcome"] for item in searches if item["outcome"] not in providers.SUCCESS]
    if failures:
        return failures[0]
    if any(item["outcome"] == "ok" for item in searches):
        return "ok"
    if searches:
        return "zero-results"
    if deferred:
        return "deferred"
    return "local-only" if not public else "not-searched"


def _linked_reviews(design: Mapping, record: Mapping, state_dir) -> list[str]:
    from oah.design import coverage

    wanted = {item["id"] for row in record.get("stages", ()) if row.get("id") == design["stage"]
              for item in row.get("needs", ()) if item.get("check") == design["check"]}
    return [link["review"] for link in coverage.links(record, evidence=store.ReviewEvidence(state_dir))
            if link.get("need_id") in wanted and link.get("review")]


def _discovery_index_path(state_dir) -> pathlib.Path:
    return store.root(state_dir) / DISCOVERY / "index.json"


def _discovery_index(state_dir) -> dict:
    data = store._read(_discovery_index_path(state_dir), parse=_need_index)
    if data is None:
        return {}
    return data["needs"]


def _need_index(data):
    if (set(data) != {"version", "needs"} or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["needs"], dict)):
        raise ValueError("is not a version 1 task need index")
    return data


def _fresh_discovery(state_dir, item: Mapping, selection: Mapping | None, now: dt.datetime) -> dict | None:
    """The need's last stored discovery while it is unexpired, under the same policy and selection."""
    record_id = _discovery_index(state_dir).get(item["id"])

    def checked(record):
        expires = dt.datetime.fromisoformat(record["expires_at"])
        if expires.utcoffset() is None:
            raise ValueError("a stored discovery expiry needs a timezone")
        return record, expires

    loaded = store.get_record(state_dir, DISCOVERY, record_id, parse=checked) if record_id else None
    if loaded is None:
        return None
    record, expires = loaded
    if record.get("policy_digest") != item["policy_digest"] \
            or record.get("selection_ref") != (selection["id"] if selection is not None else None):
        return None
    return record if now.astimezone(dt.timezone.utc) < expires else None


def prepare_task(admitted, needs: Sequence[Mapping], *, state_dir, settings: Mapping, now: dt.datetime,
                 basis: TaskBasis, notify: Callable[[str], None] | None = None, reuse: bool = False) -> list[dict]:
    """Search for each admitted need within the admission's request budget and store one TaskDiscoveryV1 per need.

    Only skillpack's Admitted value of this task's dispatch may run it (DESIGN §4.1): never under a launch lock, and
    its results are proposals; nothing found here is delivered by this attempt. With `reuse` (an ordinary call), an
    unexpired discovery of the same need, policy and selection is returned as it is, without searching again."""
    from oah import skillpack
    from oah.atomic import write_text_atomic

    skillpack.require(admitted)
    if not isinstance(admitted, skillpack.Admitted) or admitted.operation.kind != "dispatch" \
            or admitted.operation.task != basis.task:
        raise ValueError("prepare_task runs only under this task's dispatch admission")
    budget, requests = admitted.allows_requests(), 0
    checked = now.astimezone(dt.timezone.utc)
    expires = checked + dt.timedelta(hours=float(settings["cache_hours"]))
    out = []
    for item in needs:
        skillpack.require(admitted)
        public, local = queries(item)
        if reuse:
            current = selection_for(state_dir, item, lease_id=basis.lease[0]) if item["design"] is None else None
            kept = _fresh_discovery(state_dir, item, current, now)
            if kept is not None:
                out.append({**kept, "searches": [], "deferred": [], "local": local, "public": public, "reused": True})
                continue
        searches, deferred = [], []
        for query in public:
            if not settings["enabled"] or settings["provider"] != providers.PROVIDER:
                break
            cached = providers.cached(query["text"], state_dir=state_dir, settings=settings, now=now,
                                      admitted=admitted)
            if cached is None and requests >= budget:
                deferred.append({"query": query["text"], "outcome": "deferred", "reason": "budget"})
                continue
            found = cached or providers.search(query["text"], state_dir=state_dir, settings=settings, now=now,
                                               admitted=admitted, notify=notify,
                                               purpose={"task": True, "need_id": item["id"]})
            requests += int(found.requested)
            searches.append(found.to_json())
        query_ids = sorted({search["query_id"] for search in searches})
        reviews = sorted({review["id"] for review in store.reviews(state_dir)
                          if review.get("status") == "reviewed"
                          and (review.get("candidate") or {}).get("query_id") in query_ids})
        selection = None
        gaps = []
        if item["design"] is None:
            selection = selection_for(state_dir, item, lease_id=basis.lease[0])
            chosen = set(selection["review_ids"]) if selection is not None else set()
            linked = []
        else:
            chosen = set()
            linked = _linked_reviews(item["design"], basis.design["record"], state_dir) if basis.design else []
            texts = {query["text"] for query in public}
            for review_id in linked:
                candidate = (store.get_review(state_dir, review_id) or {}).get("candidate") or {}
                old = store.get_query(state_dir, candidate["query_id"]) if candidate.get("query_id") else None
                if old is not None and old.get("query") not in texts:
                    gaps.append({"code": "reselect", "need_id": item["id"]})
                    break
        outcome = _outcome(searches, deferred, public)
        if outcome == "local-only" and local:
            gaps.append({"code": "public-term-required", "need_id": item["id"]})
        elif outcome not in ("ok", "zero-results", "deferred", "local-only", "not-searched"):
            gaps.append({"code": "provider-failed", "need_id": item["id"]})
        record = {"version": 1, "need_id": item["id"], "checked_at": checked.isoformat(timespec="seconds"),
                  "expires_at": expires.isoformat(timespec="seconds"), "query_ids": query_ids, "outcome": outcome,
                  "reviews": reviews, "selection_ref": selection["id"] if selection is not None else None,
                  "proposed_reviews": [review for review in reviews if review not in chosen and review not in linked],
                  "gaps": gaps, "policy_digest": item["policy_digest"]}
        _word, stored = store.put_record(state_dir, DISCOVERY, record)
        index = _discovery_index(state_dir)
        if index.get(item["id"]) != stored["id"]:
            index[item["id"]] = stored["id"]
            write_text_atomic(_discovery_index_path(state_dir), json.dumps({"version": 1, "needs": index}, indent=1,
                                                                           sort_keys=True) + "\n")
        out.append({**stored, "searches": searches, "deferred": deferred, "local": local, "public": public,
                    "reused": False})
    return out


# Selections ------------------------------------------------------------------------------------------------------

def _index_path(state_dir) -> pathlib.Path:
    return store.root(state_dir) / SELECTIONS / "index.json"


def _index(state_dir) -> dict:
    data = store._read(_index_path(state_dir), parse=_need_index)
    if data is None:
        return {}
    return data["needs"]


def design_bound(basis: TaskBasis, item: Mapping) -> bool:
    """True when only an accepted coverage link can deliver a skill for this need: the task follows a design (accepted
    or not yet), or the need names a design check. A task selection is never the route there (DESIGN §4.8)."""
    return basis.bound or basis.design is not None or item["design"] is not None


def select(state_dir, item: Mapping, review_ids: Sequence[str], *, basis: TaskBasis) -> tuple[str, dict]:
    """Store a lead's TaskSkillSelectionV1 binding reviewed skills to one need of a task without a design."""
    from oah import skillpack
    from oah.atomic import write_text_atomic

    if design_bound(basis, item):
        raise NeedsInvalid("design-bound", "this task follows its accepted design: link the skill in the design's "
                           "coverage and accept it; a task selection never stands in for design acceptance")
    ids = list(review_ids)
    if not 0 < len(ids) <= REVIEWS_LIMIT or len(set(ids)) != len(ids):
        raise NeedsInvalid("malformed", f"select 1 to {REVIEWS_LIMIT} different reviews")
    for review_id in ids:
        review = store.get_review(state_dir, review_id) if isinstance(review_id, str) else None
        if review is None or review.get("status") != "reviewed":
            raise NeedsInvalid("malformed", f"{review_id} is not a stored, reviewed skill")
        if (review.get("license") or {}).get("eligibility") != "eligible":
            raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "license", f"{review_id}: its license is not "
                                        "eligible under the policy; it cannot be selected")
        if (review.get("frontmatter") or {}).get("model_invocable") is False:
            raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "user-only", f"{review_id}: the skill is for "
                                        "explicit use only and is never delivered")
        if (review.get("screening") or {}).get("marker_collision"):
            raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "marker", f"{review_id}: its text has a line "
                                        "starting with <!-- oah")
    record = {"version": 1, "need_id": item["id"], "packet_digest": item["packet"]["source_digest"],
              "intent_digest": item["packet"]["intent"]["digest"],
              "lease": {"id": basis.lease[0], "generation": basis.lease[1]}, "review_ids": ids, "selected_by": "lead",
              "policy_digest": item["policy_digest"]}
    word, stored = store.put_record(state_dir, SELECTIONS, record)
    index = _index(state_dir)
    if index.get(item["id"]) != stored["id"]:
        index[item["id"]] = stored["id"]
        write_text_atomic(_index_path(state_dir), json.dumps({"version": 1, "needs": index}, indent=1,
                                                             sort_keys=True) + "\n")
    return word, stored


def selection_for(state_dir, item: Mapping, *, lease_id: str) -> dict | None:
    """The need's current selection, rechecked: same need, packet, intent and policy, made under this lease."""
    record_id = _index(state_dir).get(item["id"])
    record = store.get_record(state_dir, SELECTIONS, record_id) if record_id else None
    if record is None:
        return None
    if (record.get("need_id"), record.get("packet_digest"), record.get("intent_digest"),
            record.get("policy_digest"), (record.get("lease") or {}).get("id")) != (
            item["id"], item["packet"]["source_digest"], item["packet"]["intent"]["digest"], item["policy_digest"],
            lease_id):
        return None
    return record


def still_selected(state_dir, need_id: str, review_id: str, *, lease_id: str) -> bool:
    """Local only, for the launch guard: the need's current selection still names this review under this lease."""
    record_id = _index(state_dir).get(need_id)
    record = store.get_record(state_dir, SELECTIONS, record_id) if record_id else None
    return (record is not None and record.get("need_id") == need_id and review_id in (record.get("review_ids") or ())
            and (record.get("lease") or {}).get("id") == lease_id)
