# 설계 저장소 — 기록 읽기(정본 브랜치 사본과 신선도), 위키 잠금·저널 아래 쓰기(등록·생성·저장·수락·이름 변경), 복구와 위키 투영 다시 쓰기를 맡는다.
"""Design records under `.oah/designs`, their wiki pages, and the one journal that makes a write all-or-nothing.

Reads take no lock and never write. A design's pointer (`design.json`) changes only by an atomic replace, and only
after every immutable file it names is published, so a reader sees the old complete revision or the new one.

Writes run inside `Wiki.session()` (the wiki lock, which refuses while a rollover is pending) and refuse while a
design journal is pending. Each write validates everything and preflights the wiki pages, writes the journal,
publishes the immutable files, replaces the pointer (the commit point), rewrites the derived pages and adds the
event's log entry once, then removes the journal. `recover()` completes an interrupted write forward or refuses.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import datetime
import hashlib
import importlib.resources
import json
import os
import pathlib
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from oah.atomic import StateCorrupt, StorageError, append_jsonl, read_json, unlink_durable, write_json_atomic, \
    write_text_atomic
from oah.design import fs, pages
from oah.design.model import (BEHIND_CODE, CONTEXT_STALE_CODE, CORRUPT_CODE, DESIGN_PART, EXISTS_CODE,
                              FRESHNESS_UNKNOWN_CODE, INPUT_INVALID_CODE, JOURNAL_CORRUPT_CODE, LIST_STALE_CODE,
                              MAX_PARTS, NOT_AUTHORITY_CODE, NOT_FOUND_CODE, ORIGIN_NEW, ORIGIN_SAVE, ORIGIN_WIKI,
                              ORPHAN_CODE, PAGE_FOREIGN_CODE, PAGE_MISSING_CODE, PAGE_STALE_CODE, PART_MAX,
                              PATH_INVALID_CODE, RECOVERY_CONFLICT_CODE, RECOVERY_PENDING_CODE, REQUEST_PART,
                              REVISION_CONFLICT_CODE, UNREGISTERED_EDIT_CODE, AcceptanceInfo, DesignBinding,
                              DesignError, Diagnostic, Pointer, ProjectRecord, RevisionInfo, check_digest, check_event,
                              check_fact, check_id, check_line, check_part, check_revision, content_digest, corrupt,
                              event_key, file_bytes, is_hex_key, iso, new_uid, normalize_text, stored_text,
                              text_digest, title_digest, utc_now)
from oah.wiki.base import EXIT_ENVIRONMENT, WikiError, WikiSettings, fsync_folder, publish_new, read_bytes
from oah.wiki.log import add_entry
from oah.wiki.notes import validate_title
from oah.wiki.store import Wiki

JOURNAL_NAME = "design-journal.json"
RESOLUTIONS_NAME = "design-resolutions.jsonl"
RESOLVED_DIR_NAME = "resolved-records"
PAGE_STATES = ("clean", "stale", "edited", "missing", "foreign")
BINDING_STATES = ("unbound", "missing", "current", "stale", "invalid", "edited", "unknown")
Fault = Callable[[str], None]
Facts = Callable[[str, str], Sequence[str]]


def _no_fault(step: str) -> None:
    return None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("not base64 text")
    return base64.b64decode(value, validate=True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# What reads return --------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DesignRecord:
    """A verified design: the pointer, the revision chain from current back to the first, and the acceptances."""

    project: ProjectRecord
    pointer: Pointer
    chain: tuple[RevisionInfo, ...]  # current first
    acceptances: tuple[AcceptanceInfo, ...]  # oldest first
    orphans: tuple[str, ...]  # revision folders and acceptance files that no chain names
    numbers: frozenset[int] = frozenset()  # every r<N> folder number present
    sequences: frozenset[int] = frozenset()  # every a<K>.json number present

    @property
    def current(self) -> RevisionInfo:
        return self.chain[0]

    @property
    def accepted(self) -> AcceptanceInfo | None:
        return self.acceptances[-1] if self.acceptances else None

    @property
    def status(self) -> str:
        accepted = self.accepted
        return "accepted" if accepted is not None and accepted.revision == self.current.revision else "draft"

    def revision(self, number: int) -> RevisionInfo | None:
        return next((info for info in self.chain if info.revision == number), None)


@dataclasses.dataclass(frozen=True)
class Snapshot:
    info: RevisionInfo
    texts: Mapping[str, str]

    @property
    def revision(self) -> int:
        return self.info.revision

    @property
    def digest(self) -> str:
        return self.info.digest

    def text(self, part: str = DESIGN_PART) -> str:
        return self.texts[part]

    def to_json(self) -> dict:
        data = self.info.public_json()
        data["parts"] = {name: {"digest": self.info.parts[name], "text": self.texts[name]}
                         for name in pages.part_order(self.info.parts)}
        return data


@dataclasses.dataclass(frozen=True)
class RecoveryState:
    pending: bool
    this_design: bool = False
    kind: str | None = None
    project: str | None = None
    design: str | None = None

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class DesignSummary:
    project: str
    design: str
    title: str | None
    current: tuple[int, str] | None
    accepted: tuple[int, str] | None
    status: str  # draft | accepted | corrupt
    page: str | None
    page_state: str | None
    freshness: str  # current | behind | unknown

    def to_json(self) -> dict:
        return {"project": self.project, "design": self.design, "title": self.title,
                "current": None if self.current is None else {"revision": self.current[0], "digest": self.current[1]},
                "accepted": None if self.accepted is None else {"revision": self.accepted[0],
                                                                "digest": self.accepted[1]},
                "status": self.status, "page": self.page, "page_state": self.page_state,
                "freshness": self.freshness}


@dataclasses.dataclass(frozen=True)
class Context:
    """What start, resume, dispatch and review need about one design. Read-only."""

    project: ProjectRecord
    design: str
    title: str
    status: str
    page: str | None
    page_state: str
    source: str  # worktree | authority | reader
    freshness: str  # current | behind | unknown
    current: Snapshot
    accepted: Snapshot | None
    acceptances: tuple[AcceptanceInfo, ...]
    revisions: tuple[RevisionInfo, ...]  # newest first
    recovery: RecoveryState
    diagnostics: tuple[Diagnostic, ...]

    @property
    def recovery_pending(self) -> bool:
        return self.recovery.pending

    def binding(self, which: str = "current") -> DesignBinding:
        snapshot = self.current if which == "current" else self.accepted
        if snapshot is None:
            raise DesignError(NOT_FOUND_CODE, f"{self.project.id}/{self.design} has no accepted revision")
        return DesignBinding(self.project.id, self.project.uid, self.design, snapshot.revision, snapshot.digest)

    def to_json(self) -> dict:
        return {"version": 1, "project": self.project.public_json(),
                "design": {"id": self.design, "title": self.title, "status": self.status, "page": self.page,
                           "page_state": self.page_state},
                "source": self.source, "freshness": self.freshness, "recovery_pending": self.recovery.to_json(),
                "current": self.current.to_json(),
                "accepted": None if self.accepted is None else self.accepted.to_json(),
                "acceptances": [item.public_json() for item in self.acceptances],
                "revisions": [item.public_json() for item in self.revisions],
                "diagnostics": [item.to_json() for item in self.diagnostics]}


@dataclasses.dataclass(frozen=True)
class WriteResult:
    action: str  # registered | present | renamed | unchanged | created | saved | applied | accepted | already-accepted
    project: str
    design: str | None
    revision: int | None
    digest: str | None
    event: str | None
    paths: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    page: str | None = None
    current: int | None = None  # for applied: the design's current revision, when that is another one

    LINE_CODES = {"registered": "OAH_DESIGN_PROJECT_ADDED", "present": "OAH_DESIGN_PROJECT_PRESENT",
                  "renamed": "OAH_DESIGN_PROJECT_RENAMED", "unchanged": "OAH_DESIGN_UNCHANGED",
                  "created": "OAH_DESIGN_CREATED", "saved": "OAH_DESIGN_SAVED", "applied": "OAH_DESIGN_APPLIED",
                  "accepted": "OAH_DESIGN_ACCEPTED", "already-accepted": "OAH_DESIGN_ACCEPTED"}

    def lines(self) -> list[str]:
        name = self.project if self.design is None else f"{self.project}/{self.design}"
        line = f"{self.LINE_CODES[self.action]} {name}"
        if self.revision is not None:
            line += f" r{self.revision} {self.digest}"
        if self.action == "already-accepted":
            line += " (already accepted)"
        if self.current is not None:
            line += f" (the design is now at revision {self.current})"
        out = [line]
        if self.page:
            out.append(f"OAH_DESIGN_PAGE {self.page}")
        out.extend(f"warning {item}" for item in self.diagnostics)
        return out


@dataclasses.dataclass(frozen=True)
class ReconcileResult:
    written: tuple[str, ...]
    unchanged: int
    diagnostics: tuple[Diagnostic, ...]

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.diagnostics if item.code in (UNREGISTERED_EDIT_CODE, PAGE_FOREIGN_CODE,
                                                                   CORRUPT_CODE, LIST_STALE_CODE))

    def lines(self) -> list[str]:
        out = [f"OAH_DESIGN_RECONCILED written={len(self.written)} unchanged={self.unchanged} skipped={self.skipped}"]
        out.extend(f"  wrote {rel}" for rel in self.written)
        out.extend(f"warning {item}" for item in self.diagnostics)
        return out


# Loading ------------------------------------------------------------------------------------------


def _loads(data: bytes, where: str) -> object:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise corrupt(where, f"is not UTF-8 JSON ({exc})") from exc


def load_project(reader: fs.Reader, project: str) -> ProjectRecord:
    rel = fs.project_record_rel(project)
    data = reader.read(rel)
    if data is None:
        raise DesignError(NOT_FOUND_CODE, f"there is no design project {project}; register it with `oah design "
                          f"project add {project} --title <title>`, or list projects with `oah design status`")
    return ProjectRecord.from_json(_loads(data, rel), rel, project)


def load_design(reader: fs.Reader, project: ProjectRecord, design: str) -> DesignRecord:
    """The verified record of one design: both hash chains are walked and checked."""
    pid = project.id
    rel = fs.pointer_rel(pid, design)
    data = reader.read(rel)
    if data is None:
        raise DesignError(NOT_FOUND_CODE, f"project {pid} has no design {design}; create it with `oah design new {pid} "
                          f"{design} --title <title> --file <file>`, or list designs with `oah design status {pid}`")
    pointer = Pointer.from_json(_loads(data, rel), rel)
    if (pointer.project, pointer.design) != (pid, design):
        raise corrupt(rel, f"it names {pointer.project}/{pointer.design}")
    if pointer.uid != project.uid:
        raise corrupt(rel, "its project uid differs from project.json")
    names = reader.list_dir(fs.revisions_rel(pid, design)) or []
    numbers = frozenset(n for n in (fs.revision_number(name) for name in names) if n)
    manifests = reader.read_many([fs.manifest_rel(pid, design, n) for n in sorted(numbers)])
    chain: list[RevisionInfo] = []
    expected: tuple[int, str] | None = (pointer.current[0], pointer.current[2])
    while expected is not None:
        number, manifest = expected
        mrel = fs.manifest_rel(pid, design, number)
        raw = manifests[mrel] if mrel in manifests else reader.read(mrel)
        if raw is None:
            raise corrupt(mrel, "is missing, but the history names it")
        info = RevisionInfo.from_json(_loads(raw, mrel), mrel)
        if (info.project, info.design, info.uid, info.revision) != (pid, design, project.uid, number):
            raise corrupt(mrel, "it names another project, design, uid or revision")
        if info.manifest != manifest:
            raise corrupt(mrel, "its manifest digest differs from the one its child or the pointer recorded")
        chain.append(info)
        expected = info.parent
    if chain[0].digest != pointer.current[1]:
        raise corrupt(rel, "its current digest differs from the current manifest")
    on_chain = {info.revision: info for info in chain}
    orphans = [fs.manifest_rel(pid, design, n).rsplit("/", 1)[0] for n in sorted(numbers - set(on_chain))]
    names = reader.list_dir(fs.acceptances_rel(pid, design)) or []
    sequences = frozenset(n for n in (fs.acceptance_number(name) for name in names) if n)
    acceptances: list[AcceptanceInfo] = []
    if pointer.accepted is not None:
        records = reader.read_many([fs.acceptance_rel(pid, design, n) for n in sorted(sequences)])
        step: tuple[int, str] | None = (pointer.accepted[0], pointer.accepted[3])
        while step is not None:
            sequence, record = step
            arel = fs.acceptance_rel(pid, design, sequence)
            raw = records[arel] if arel in records else reader.read(arel)
            if raw is None:
                raise corrupt(arel, "is missing, but the acceptance history names it")
            item = AcceptanceInfo.from_json(_loads(raw, arel), arel)
            if (item.project, item.design, item.uid, item.sequence) != (pid, design, project.uid, sequence):
                raise corrupt(arel, "it names another project, design, uid or sequence")
            if item.record != record:
                raise corrupt(arel, "its record digest differs from the one recorded after it")
            revision = on_chain.get(item.revision)
            if revision is None or (revision.digest, revision.manifest) != (item.digest, item.manifest):
                raise corrupt(arel, f"it accepts revision {item.revision}, which this history does not hold")
            if acceptances and item.revision >= acceptances[-1].revision:
                raise corrupt(arel, "acceptances must move to higher revisions")
            acceptances.append(item)
            step = item.previous
        acceptances.reverse()
        latest = acceptances[-1]
        if (latest.revision, latest.digest) != (pointer.accepted[1], pointer.accepted[2]):
            raise corrupt(rel, "its accepted revision differs from the latest acceptance")
    named = {item.sequence for item in acceptances}
    orphans += [fs.acceptance_rel(pid, design, n) for n in sorted(sequences - named)]
    return DesignRecord(project, pointer, tuple(chain), tuple(acceptances), tuple(orphans), numbers, sequences)


@dataclasses.dataclass(frozen=True)
class _Head:
    """What a list or index line shows: the current revision, checked against the pointer only."""

    title: str
    revision: int
    status: str
    accepted: int | None
    updated: str


def _head_of(record: DesignRecord) -> _Head:
    accepted = record.accepted
    return _Head(record.current.title, record.current.revision, record.status,
                 None if accepted is None else accepted.revision, record.current.at[:10])


def load_head(reader: fs.Reader, project: ProjectRecord, design: str) -> _Head:
    """The pointer and its current manifest, verified against each other (not the whole chain)."""
    rel = fs.pointer_rel(project.id, design)
    data = reader.read(rel)
    if data is None:
        raise DesignError(NOT_FOUND_CODE, f"project {project.id} has no design {design}")
    pointer = Pointer.from_json(_loads(data, rel), rel)
    if (pointer.project, pointer.design, pointer.uid) != (project.id, design, project.uid):
        raise corrupt(rel, "it names another project, design or uid")
    revision, digest, manifest = pointer.current
    mrel = fs.manifest_rel(project.id, design, revision)
    raw = reader.read(mrel)
    if raw is None:
        raise corrupt(mrel, "is missing, but the pointer names it")
    info = RevisionInfo.from_json(_loads(raw, mrel), mrel)
    if (info.revision, info.digest, info.manifest) != (revision, digest, manifest):
        raise corrupt(mrel, "does not match the pointer")
    accepted = None if pointer.accepted is None else pointer.accepted[1]
    status = "accepted" if accepted == revision else "draft"
    return _Head(info.title, revision, status, accepted, info.at[:10])


def read_texts(reader: fs.Reader, info: RevisionInfo) -> dict[str, str]:
    """The verified part texts of one revision."""
    rels = {name: fs.part_rel(info.project, info.design, info.revision, name) for name in info.parts}
    raw = reader.read_many(rels.values())
    texts = {}
    for name, rel in rels.items():
        data = raw.get(rel)
        if data is None:
            raise corrupt(rel, "is missing, but its revision names it")
        text = stored_text(data)
        if text is None or text_digest(text) != info.parts[name]:
            raise corrupt(rel, "does not match the digest its revision recorded")
        texts[name] = text
    return texts


def _design_names(reader: fs.Reader, project: str) -> list[str]:
    names = reader.list_dir(f"{fs.REGISTRY_DIR}/{project}") or []
    out = []
    for name in names:
        try:
            check_id(name, "design")
        except DesignError:
            continue
        if reader.read(fs.pointer_rel(project, name)) is not None:
            out.append(name)
    return out


def _project_names(reader: fs.Reader) -> list[str]:
    out = []
    for name in reader.list_dir(fs.REGISTRY_DIR) or []:
        try:
            check_id(name)
        except DesignError:
            continue
        if reader.read(fs.project_record_rel(name)) is not None:
            out.append(name)
    return out


# The store ----------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Resolved:
    project: ProjectRecord
    local_project: ProjectRecord
    record: DesignRecord
    local: DesignRecord | None
    reader: fs.Reader
    source: str
    freshness: str
    diagnostics: list[Diagnostic]


@dataclasses.dataclass
class _Plan:
    writes: list[tuple[str, bytes | None, bytes]] = dataclasses.field(default_factory=list)
    diagnostics: list[Diagnostic] = dataclasses.field(default_factory=list)
    same: int = 0  # files that already hold what oah would write


class DesignStore:
    def __init__(self, info, settings: WikiSettings, state: pathlib.Path, *, lock_timeout: float = 10.0,
                 now: Callable[[], datetime.datetime] | None = None, reader: fs.Reader | None = None,
                 facts: Facts | None = None) -> None:
        self.info = info
        self.root = pathlib.Path(info.root)
        self.state = pathlib.Path(state)
        self.lock_timeout = lock_timeout
        self._now = now or utc_now
        self._facts = facts
        self._custom = reader is not None
        self.wiki_error: WikiError | None = None
        try:
            self.wiki: Wiki | None = Wiki(self.root, settings, self.state, lock_timeout=lock_timeout)
        except WikiError as exc:
            self.wiki, self.wiki_error = None, exc
        self.wiki_dir = self.wiki.settings.dir if self.wiki is not None else None
        self.reader: fs.Reader = reader or fs.WorktreeReader(self.root, self.wiki_dir)
        self.journal_path = self.state / "wiki" / JOURNAL_NAME

    # ---- small helpers ---------------------------------------------------------------------------

    def head_ref(self) -> str | None:
        """The full ref HEAD is on (refs/heads/<branch>), or None on a detached HEAD."""
        from oah.paths import run_git

        result = run_git(self.root, "symbolic-ref", "--quiet", "HEAD")
        ref = result.stdout.strip()
        return ref if result.returncode == 0 and ref.startswith("refs/heads/") else None

    def _head_or_none(self) -> str | None:
        from oah.paths import GitMissing

        try:
            return self.head_ref()
        except GitMissing:
            return None

    def page_path(self, project: str, design: str) -> pathlib.Path | None:
        if self.wiki_dir is None:
            return None
        return self.root / fs.design_page_rel(self.wiki_dir, check_id(project), check_id(design, "design"))

    def _page_rel(self, project: str, design: str) -> str | None:
        return None if self.wiki_dir is None else fs.design_page_rel(self.wiki_dir, project, design)

    def _read_page(self, rel: str) -> bytes | None:
        return fs.read_file(fs.guarded_path(self.root, rel, fs.pages_rel(self.wiki_dir)), limit=fs.PAGE_LIMIT)

    def _facts_for(self, project: str, design: str, override: Facts | None, diagnostics: list[Diagnostic]) -> list[str]:
        provider = override or self._facts
        if provider is None:
            return []
        try:
            return [check_fact(item) for item in provider(project, design)]
        except Exception as exc:  # a lane's provider never blocks a write; its words are left out
            diagnostics.append(Diagnostic(LIST_STALE_CODE, f"the facts for {project}/{design} were left out: {exc}"))
            return []

    def recovery_state(self, project: str | None = None, design: str | None = None) -> RecoveryState:
        try:
            data = read_json(self.journal_path, None)
        except (StateCorrupt, StorageError):
            return RecoveryState(True, True, "unreadable")
        if data is None:
            return RecoveryState(False)
        if not isinstance(data, dict):
            return RecoveryState(True, True, "unreadable")
        jp, jd = data.get("project"), data.get("design")
        this = project is not None and jp == project and (jd is None or design is None or jd == design)
        return RecoveryState(True, this, data.get("kind"), jp, jd)

    # ---- reads -----------------------------------------------------------------------------------

    def _resolve(self, project: str, design: str) -> _Resolved:
        local_project = load_project(self.reader, project)
        if self._custom:
            record = load_design(self.reader, local_project, design)
            return _Resolved(local_project, local_project, record, record, self.reader, "reader", "unknown", [])
        head = self._head_or_none()
        authority = local_project.authority
        if head == authority:
            record = load_design(self.reader, local_project, design)
            return _Resolved(local_project, local_project, record, record, self.reader, "worktree", "current", [])
        where = f"on {pages.short_ref(head)}" if head else "at a detached HEAD"
        diagnostics = [Diagnostic(NOT_AUTHORITY_CODE, f"this worktree is {where}; designs of {project} change on "
                                  f"{pages.short_ref(authority)}, so oah reads that branch's committed copy",
                                  level="info")]
        local: DesignRecord | None = None
        local_error: DesignError | None = None
        try:
            local = load_design(self.reader, local_project, design)
        except DesignError as exc:
            local_error = exc
        try:
            remote = fs.GitRefReader(self.root, authority, f"{fs.REGISTRY_DIR}/{project}")
            remote_project = load_project(remote, project)
            if remote_project.uid != local_project.uid:
                raise fs.RefUnavailable(f"project {project} on {pages.short_ref(authority)} has another uid")
            record = load_design(remote, remote_project, design)
        except (fs.RefUnavailable, DesignError) as exc:
            if local is None:
                raise local_error  # type: ignore[misc]
            diagnostics.append(Diagnostic(FRESHNESS_UNKNOWN_CODE, f"cannot read the authority copy on "
                                          f"{pages.short_ref(authority)} ({exc}); showing this worktree's copy, which "
                                          "may be old"))
            return _Resolved(local_project, local_project, local, local, self.reader, "worktree", "unknown",
                             diagnostics)
        if local is not None and local.pointer == record.pointer:
            freshness = "current"
        else:
            freshness = "behind"
            have = f"revision {local.current.revision}" if local is not None else "no copy"
            diagnostics.append(Diagnostic(BEHIND_CODE, f"this worktree has {have} of {project}/{design}; "
                                          f"{pages.short_ref(authority)} has revision {record.current.revision}"))
        return _Resolved(remote_project, local_project, record, local, remote, "authority", freshness, diagnostics)

    def _classify(self, view: pages.PageView, project: ProjectRecord, record: DesignRecord | None,
                  extra: Mapping[int, dict[str, str]] | None = None) -> str:
        if view.state in ("missing", "foreign"):
            return view.state
        if view.state == "unreadable" or record is None:
            return "edited"
        info = record.revision(view.revision)
        if info is None or info.digest != view.digest:
            return "edited"
        texts = (extra or {}).get(info.revision)
        if texts is None:
            texts = read_texts(self.reader, info)
        titles = [project.title, *reversed(project.former_titles)]
        for position, title in enumerate(titles):
            if view.text == pages.render_design_page(project.id, title, info, texts):
                return "clean" if position == 0 and info.revision == record.current.revision else "stale"
        return "edited"

    def _page_state(self, project: ProjectRecord, record: DesignRecord | None,
                    design: str) -> tuple[str | None, Diagnostic | None]:
        rel = self._page_rel(project.id, design)
        if rel is None:
            return None, None
        view = pages.read_design_page(self._read_page(rel))
        state = self._classify(view, project, record)
        return state, self._page_diagnostic(state, rel, project.id, design, view)

    def _page_diagnostic(self, state: str, rel: str, project: str, design: str,
                         view: pages.PageView) -> Diagnostic | None:
        if state == "missing":
            return Diagnostic(PAGE_MISSING_CODE, f"{rel} is missing; `oah design reconcile` writes it again", rel)
        if state == "foreign":
            return Diagnostic(PAGE_FOREIGN_CODE, f"{rel} has no `oah: design-page` mark, so oah leaves it alone; move "
                              "it away, then run `oah design reconcile`", rel)
        if state == "edited":
            based = f" revision {view.revision}" if view.revision else " its revision"
            return Diagnostic(UNREGISTERED_EDIT_CODE, f"{rel} differs from what oah wrote for{based}; register the "
                              f"edit with `oah design save {project} {design} --from-wiki --expect-revision <N> "
                              "--expect-digest <digest> --reason <why>`, or delete the page to discard it", rel)
        if state == "stale":
            return Diagnostic(PAGE_STALE_CODE, f"{rel} shows revision {view.revision} or an older project title; "
                              "`oah design reconcile` rewrites it", rel)
        return None

    def _orphan_diagnostics(self, record: DesignRecord | None) -> list[Diagnostic]:
        if record is None:
            return []
        return [Diagnostic(ORPHAN_CODE, f"{rel} is not part of the history (left by an interrupted write); oah "
                           "ignores it and never reuses its number", rel) for rel in record.orphans]

    def _wiki_diagnostics(self) -> list[Diagnostic]:
        out = []
        if self.wiki is None and self.wiki_error is not None:
            out.append(Diagnostic(self.wiki_error.code, str(self.wiki_error).split(": ", 1)[-1]))
        elif self.wiki is not None and read_bytes(self.wiki.journal_path) is not None:
            out.append(Diagnostic("OAH_WIKI_RECOVERY_PENDING", "an interrupted log rollover is pending; design pages "
                                  "read fine, and design writes wait until `oah wiki recover`"))
        return out

    def context(self, project: str, design: str) -> Context:
        """Everything about one design, read-only: the copy of record, its texts, history and diagnostics."""
        project, design = check_id(project), check_id(design, "design")
        resolved = self._resolve(project, design)
        record = resolved.record
        current = Snapshot(record.current, read_texts(resolved.reader, record.current))
        accepted = None
        if record.accepted is not None:
            info = record.revision(record.accepted.revision)
            accepted = current if info.revision == current.revision else Snapshot(info, read_texts(resolved.reader,
                                                                                                    info))
        page_state, page_diagnostic = self._page_state(resolved.local_project, resolved.local, design)
        diagnostics = list(resolved.diagnostics)
        if page_diagnostic is not None:
            diagnostics.append(page_diagnostic)
        diagnostics += self._orphan_diagnostics(resolved.local)
        recovery = self.recovery_state(project, design)
        if recovery.pending:
            scope = "this design" if recovery.this_design else f"{recovery.project}/{recovery.design or '-'}"
            diagnostics.append(Diagnostic(RECOVERY_PENDING_CODE, f"an interrupted design write ({recovery.kind}, "
                                          f"{scope}) is pending; run `oah design recover`"))
        diagnostics += self._wiki_diagnostics()
        return Context(resolved.project, design, record.current.title, record.status, self._page_rel(project, design),
                       page_state or "missing", resolved.source, resolved.freshness, current, accepted,
                       record.acceptances, record.chain, recovery, tuple(diagnostics))

    def snapshot(self, project: str, design: str, revision: int | None = None) -> Snapshot:
        project, design = check_id(project), check_id(design, "design")
        resolved = self._resolve(project, design)
        info = resolved.record.current if revision is None else resolved.record.revision(check_revision(revision))
        if info is None:
            raise DesignError(NOT_FOUND_CODE, f"revision {revision} is not in the history of {project}/{design}; "
                              f"`oah design status {project} {design}` lists it")
        return Snapshot(info, read_texts(resolved.reader, info))

    def projects(self) -> list[ProjectRecord]:
        out = []
        for name in _project_names(self.reader):
            try:
                out.append(load_project(self.reader, name))
            except DesignError:
                continue
        return out

    def designs(self, project: str | None = None) -> list[DesignSummary]:
        """A summary per design; a record that does not verify is reported as `corrupt`, never raised."""
        names = [check_id(project)] if project is not None else _project_names(self.reader)
        if project is not None:
            load_project(self.reader, names[0])  # NOT_FOUND for an unknown project
        out = []
        for name in names:
            out.extend(self._project_summaries(name))
        return out

    def _project_summaries(self, project: str) -> list[DesignSummary]:
        designs = _design_names(self.reader, project)
        try:
            record = load_project(self.reader, project)
        except DesignError:
            return [DesignSummary(project, name, None, None, None, "corrupt", self._page_rel(project, name), None,
                                  "unknown") for name in designs]
        freshness = self._freshness_map(record, designs)
        out = []
        for name in designs:
            rel = self._page_rel(project, name)
            try:
                design = load_design(self.reader, record, name)
            except DesignError:
                out.append(DesignSummary(project, name, None, None, None, "corrupt", rel, None, "unknown"))
                continue
            try:
                state, _ = self._page_state(record, design, name)
            except DesignError:
                state = "edited"
            accepted = design.accepted
            out.append(DesignSummary(project, name, design.current.title,
                                     (design.current.revision, design.current.digest),
                                     None if accepted is None else (accepted.revision, accepted.digest),
                                     design.status, rel, state, freshness.get(name, "unknown")))
        return out

    def _freshness_map(self, project: ProjectRecord, designs: Sequence[str]) -> dict[str, str]:
        if self._custom:
            return {}
        if self._head_or_none() == project.authority:
            return {name: "current" for name in designs}
        try:
            remote = fs.GitRefReader(self.root, project.authority, f"{fs.REGISTRY_DIR}/{project.id}")
            remote_project = load_project(remote, project.id)
            if remote_project.uid != project.uid:
                return {}
            pointers = remote.read_many([fs.pointer_rel(project.id, name) for name in designs])
        except (fs.RefUnavailable, DesignError):
            return {}
        out = {}
        for name in designs:
            theirs, ours = pointers.get(fs.pointer_rel(project.id, name)), self.reader.read(fs.pointer_rel(project.id,
                                                                                                         name))
            try:
                same = theirs is not None and ours is not None and (
                    Pointer.from_json(_loads(theirs, name), name) == Pointer.from_json(_loads(ours, name), name))
            except DesignError:
                continue
            out[name] = "current" if same else "behind"
        return out

    def diagnostics(self, project: str | None = None, design: str | None = None) -> list[Diagnostic]:
        out = []
        for summary in self.designs(project):
            if design is not None and summary.design != design:
                continue
            if summary.status == "corrupt":
                out.append(Diagnostic(CORRUPT_CODE, f"{summary.project}/{summary.design} does not verify; "
                                      f"`oah design status {summary.project} {summary.design}` names the file"))
            elif summary.page_state not in (None, "clean"):
                out.append(Diagnostic({"stale": PAGE_STALE_CODE, "edited": UNREGISTERED_EDIT_CODE,
                                       "missing": PAGE_MISSING_CODE, "foreign": PAGE_FOREIGN_CODE}
                                      [summary.page_state], f"{summary.page} is {summary.page_state}", summary.page))
        recovery = self.recovery_state(project, design)
        if recovery.pending:
            out.append(Diagnostic(RECOVERY_PENDING_CODE, "an interrupted design write is pending; run "
                                  "`oah design recover`"))
        return out + self._wiki_diagnostics()

    # ---- the write session -----------------------------------------------------------------------

    @contextlib.contextmanager
    def _session(self) -> Iterator[None]:
        if self._custom:
            raise DesignError(NOT_AUTHORITY_CODE, "this store reads another copy of the records; open it on the "
                              "authority worktree to change designs")
        if self.wiki is None:
            raise self.wiki_error  # type: ignore[misc]
        self._check_layout()
        with self.wiki.session():
            if read_bytes(self.journal_path) is not None:
                state = self.recovery_state()
                raise DesignError(RECOVERY_PENDING_CODE, f"an interrupted design write ({state.kind}, "
                                  f"{state.project}/{state.design or '-'}) left {self.journal_path}. Nothing was "
                                  "changed. Run `oah design recover`, then retry.")
            for name in ("index.md", "log.md"):
                if not (self.wiki.dir / name).is_file():
                    raise WikiError("OAH_WIKI_MISSING", f"{self.wiki.dir / name} does not exist. Run `oah init` to "
                                    "scaffold the wiki, or set [wiki] dir in .oah/config.toml.", EXIT_ENVIRONMENT)
            yield

    def _check_layout(self) -> None:
        wiki = pathlib.PurePosixPath(self.wiki_dir.casefold())
        folder = pathlib.PurePosixPath(fs.pages_rel(self.wiki_dir).casefold())
        registry = pathlib.PurePosixPath(fs.REGISTRY_DIR.casefold())
        if (folder == registry or registry in folder.parents or folder in registry.parents
                or wiki == registry or registry in wiki.parents):
            raise WikiError("OAH_CONFIG_INVALID", f"[wiki] dir = {self.wiki_dir!r} puts the design pages "
                            f"({fs.pages_rel(self.wiki_dir)}) on top of the design records ({fs.REGISTRY_DIR}). Set "
                            "[wiki] dir in .oah/config.toml to another folder, then retry.", EXIT_ENVIRONMENT)

    def _require_authority(self, project: ProjectRecord) -> str:
        head = self.head_ref()
        branch = pages.short_ref(project.authority)
        if head is None:
            raise DesignError(NOT_AUTHORITY_CODE, f"HEAD is detached; designs of {project.id} change only on branch "
                              f"{branch}. Check it out (or use its worktree), then retry; reads work everywhere")
        if head != project.authority:
            raise DesignError(NOT_AUTHORITY_CODE, f"this worktree is on {pages.short_ref(head)}, but designs of "
                              f"{project.id} change only on {branch}. Switch to that branch (or its worktree), then "
                              "retry; reads work here")
        return head

    def _registry_path(self, rel: str) -> pathlib.Path:
        return fs.guarded_path(self.root, rel, fs.REGISTRY_DIR, writing=True)

    def _page_path(self, rel: str) -> pathlib.Path:
        return fs.guarded_path(self.root, rel, fs.pages_rel(self.wiki_dir), writing=True)

    # ---- projection ------------------------------------------------------------------------------

    def _design_page_write(self, plan: _Plan, project: ProjectRecord, record: DesignRecord, *, strict: bool,
                           force: str | None = None, extra: Mapping[int, dict[str, str]] | None = None) -> None:
        """Rewrite a design page that is missing, clean or stale; `force` is the sha256 of an edited page that a
        `save --from-wiki` registered, which may then be rewritten while it still holds exactly those bytes."""
        design = record.pointer.design
        rel = self._page_rel(project.id, design)
        path = self._page_path(rel)
        data = fs.read_file(path, limit=fs.PAGE_LIMIT)
        view = pages.read_design_page(data)
        state = self._classify(view, project, record, extra)
        forced = force is not None and data is not None and _sha(data) == force
        if state in ("edited", "foreign") and not (forced and state == "edited"):
            diagnostic = self._page_diagnostic(state, rel, project.id, design, view)
            if strict:
                raise DesignError(diagnostic.code, diagnostic.message)
            plan.diagnostics.append(diagnostic)
            return
        texts = (extra or {}).get(record.current.revision) or read_texts(self.reader, record.current)
        new = pages.render_design_page(project.id, project.title, record.current, texts).encode("utf-8")
        if data != new:
            plan.writes.append((rel, data, new))
        else:
            plan.same += 1

    def _project_page_write(self, plan: _Plan, project: ProjectRecord, records: Mapping[str, DesignRecord], *,
                            strict: bool, facts: Facts | None = None) -> None:
        rel = fs.project_page_rel(self.wiki_dir, project.id)
        path = self._page_path(rel)
        data = fs.read_file(path, limit=fs.PAGE_LIMIT)
        view = pages.read_project_page(data, project.id)
        if view.state in ("foreign", "unreadable"):
            if view.state == "foreign":
                problem = Diagnostic(PAGE_FOREIGN_CODE, f"{rel} has no `oah: design-project` mark, so oah leaves it "
                                     "alone; move it away, then retry", rel)
            else:
                problem = Diagnostic("OAH_WIKI_INDEX_INVALID", f"{rel} needs exactly one {pages.LIST_BEGIN} line "
                                     f"followed by one {pages.LIST_END} line; fix the markers by hand, then retry", rel)
            if strict:
                raise DesignError(problem.code, problem.message) if problem.code.startswith("OAH_DESIGN") else \
                    WikiError(problem.code, problem.message)
            plan.diagnostics.append(problem)
            return
        rows = []
        for name in sorted(set(_design_names(self.reader, project.id)) | set(records)):
            head = self._head(project, name, records)
            if head is None:
                old = view.rows.get(name)
                plan.diagnostics.append(Diagnostic(CORRUPT_CODE, f"{project.id}/{name} does not verify; its list "
                                                   "line is kept as it was"))
                if old is not None:
                    rows.append(old)
                continue
            rows.append(pages.list_line(project.id, name, head.title, head.revision, head.status, head.accepted,
                                        head.updated, self._facts_for(project.id, name, facts, plan.diagnostics)))
        new = pages.build_project_page(view, project, rows)
        if data != new:
            plan.writes.append((rel, data, new))
        else:
            plan.same += 1

    def _index_write(self, plan: _Plan, projects: Mapping[str, ProjectRecord],
                     records: Mapping[tuple[str, str], DesignRecord], *, strict: bool) -> None:
        rel = (pathlib.PurePosixPath(self.wiki_dir) / "index.md").as_posix()
        data = (self.wiki.dir / "index.md").read_bytes()
        try:
            view = pages.read_index(data)
            limit = self.wiki.settings.index_line_limit
            block = []
            names = sorted(set(_project_names(self.reader)) | set(projects))
            if not names and view.span is None:
                return  # no designs and no block yet: index.md stays as it is
            for name in names:
                project = projects.get(name)
                if project is None:
                    try:
                        project = load_project(self.reader, name)
                    except DesignError:
                        block.extend(view.projects.get(name, []))
                        continue
                group = view.projects.get(name, [])
                block.append(pages.index_project_line(name, project.title, limit))
                mine = {design: record for (owner, design), record in records.items() if owner == name}
                for design in sorted(set(_design_names(self.reader, name)) | set(mine)):
                    head = self._head(project, design, mine)
                    if head is None:
                        stem = f"  - [[{fs.stem(name, design)}"
                        block.extend(line for line in group if line.startswith(stem + "|")
                                     or line.startswith(stem + "]]"))
                        continue
                    block.append(pages.index_design_line(name, design, head.title, limit))
            new = pages.build_index(data, view, block)
        except WikiError as exc:
            if strict:
                raise
            plan.diagnostics.append(Diagnostic(exc.code, str(exc).split(": ", 1)[-1], rel))
            return
        if data != new:
            plan.writes.append((rel, data, new))
        else:
            plan.same += 1

    def _head(self, project: ProjectRecord, design: str, records: Mapping[str, DesignRecord]) -> _Head | None:
        """A design's list facts: from the record being written, else read; None when it does not verify."""
        if design in records:
            return _head_of(records[design])
        try:
            return load_head(self.reader, project, design)
        except DesignError:
            return None

    def _plan(self, project: ProjectRecord, records: Mapping[str, DesignRecord], *, strict: bool,
              pages_for: Iterable[str] = (), force: Mapping[str, str] | None = None, facts: Facts | None = None,
              extra: Mapping[tuple[str, int], dict[str, str]] | None = None) -> _Plan:
        plan = _Plan()
        for design in pages_for:
            record = records.get(design)
            if record is None:
                try:
                    record = load_design(self.reader, project, design)
                except DesignError as exc:
                    if strict:
                        raise
                    plan.diagnostics.append(Diagnostic(exc.code, str(exc).split(": ", 1)[-1]))
                    continue
            texts = {revision: value for (name, revision), value in (extra or {}).items() if name == design}
            self._design_page_write(plan, project, record, strict=strict, force=(force or {}).get(design),
                                    extra=texts)
        self._project_page_write(plan, project, records, strict=strict, facts=facts)
        self._index_write(plan, {project.id: project}, {(project.id, name): record for name, record in records.items()},
                          strict=strict)
        return plan

    def _apply(self, plan: _Plan, fault: Fault) -> list[str]:
        written = []
        index_rel = (pathlib.PurePosixPath(self.wiki_dir) / "index.md").as_posix()
        for rel, before, new in plan.writes:
            if rel == index_rel:
                current = read_bytes(self.wiki.dir / "index.md")
                path = self.wiki.dir / "index.md"
            else:
                path = self._page_path(rel)
                current = fs.read_file(path, limit=fs.PAGE_LIMIT)
            if current != before:
                plan.diagnostics.append(Diagnostic(LIST_STALE_CODE, f"{rel} changed while oah was writing it, so it "
                                                   "was left as it is; run `oah design reconcile`", rel))
                continue
            write_text_atomic(path, new.decode("utf-8"))
            written.append(rel)
            fault(f"wrote {rel}")
        return written

    # ---- journal ---------------------------------------------------------------------------------

    def _journal(self, kind: str, *, project: str, design: str | None, event: str, branch: str,
                 files: list[dict], pointer: dict, log: dict) -> dict:
        return {"version": 1, "kind": kind, "created_at": iso(self._now()), "root": str(self.root),
                "common_dir": str(pathlib.Path(self.info.common_dir)), "branch": branch, "wiki_dir": self.wiki_dir,
                "project": project, "design": design, "event": event, "files": files, "pointer": pointer,
                "log": log}

    @staticmethod
    def _rebuild(journal: Mapping) -> tuple[list[tuple[str, bytes]], tuple[str, str | None, bytes]]:
        """The journal's files and pointer as (repository path, bytes); paths come only from validated IDs."""
        project = check_id(journal.get("project"))
        design = journal.get("design")
        design = None if design is None else check_id(design, "design")
        files = []
        for item in journal.get("files") or []:
            if not isinstance(item, Mapping):
                raise ValueError("a journal file entry is not an object")
            role, data = item.get("role"), _unb64(item.get("data"))
            if design is None:
                raise ValueError("a project journal names design files")
            if role == "manifest":
                rel = fs.manifest_rel(project, design, check_revision(item.get("revision")))
                RevisionInfo.from_json(_loads(data, rel), rel)
            elif role == "part":
                rel = fs.part_rel(project, design, check_revision(item.get("revision")), check_part(item.get("part")))
            elif role == "acceptance":
                rel = fs.acceptance_rel(project, design, check_revision(item.get("sequence"), "sequence"))
                AcceptanceInfo.from_json(_loads(data, rel), rel)
            else:
                raise ValueError(f"unknown journal file role {role!r}")
            files.append((rel, data))
        pointer = journal.get("pointer")
        if not isinstance(pointer, Mapping):
            raise ValueError("the journal has no pointer")
        after = _unb64(pointer.get("after"))
        before = pointer.get("before")
        if before is not None and not is_hex_key(before):
            raise ValueError("the pointer's before digest is malformed")
        if pointer.get("role") == "project":
            rel = fs.project_record_rel(project)
            ProjectRecord.from_json(_loads(after, rel), rel, project)
        elif pointer.get("role") == "design" and design is not None:
            rel = fs.pointer_rel(project, design)
            parsed = Pointer.from_json(_loads(after, rel), rel)
            if (parsed.project, parsed.design) != (project, design):
                raise ValueError("the pointer names another design")
        else:
            raise ValueError("the journal pointer role is unknown")
        return files, (rel, before, after)

    def _load_journal(self) -> dict | None:
        try:
            data = read_json(self.journal_path, None)
        except StateCorrupt as exc:
            raise DesignError(JOURNAL_CORRUPT_CODE, f"{self.journal_path} is not valid JSON ({exc}); oah never resets "
                              "it. Compare the design files with your last commit, then move the journal away by "
                              "hand") from exc
        if data is None:
            return None
        problem = None
        if not (isinstance(data, dict) and data.get("version") == 1 and data.get("kind") in
                ("registered", "renamed", "created", "saved", "accepted")
                and all(isinstance(data.get(key), str) for key in ("root", "common_dir", "branch", "event"))
                and isinstance(data.get("log"), dict)):
            problem = "it is not a version-1 design journal"
        else:
            try:
                self._rebuild(data)
                log = data["log"]
                if not (is_hex_key(log.get("key")) and isinstance(log.get("title"), str)
                        and isinstance(log.get("body"), str) and isinstance(log.get("date"), str)):
                    raise ValueError("the log entry is malformed")
                datetime.date.fromisoformat(log["date"])
                page = data.get("page")
                if page is not None and not (isinstance(page, dict) and page.get("design") == data.get("design")
                                             and is_hex_key(page.get("sha256"))):
                    raise ValueError("the page entry is malformed")
            except (ValueError, binascii.Error, DesignError) as exc:
                problem = str(exc)
        if problem:
            raise DesignError(JOURNAL_CORRUPT_CODE, f"{self.journal_path}: {problem}. oah never resets it; compare the "
                              "design files with your last commit, then move the journal away by hand")
        return data

    def _run(self, journal: dict, fault: Fault, result: WriteResult) -> WriteResult:
        files, (pointer_rel, before, after) = self._rebuild(journal)
        for rel, _ in files:
            self._registry_path(rel)
        pointer_path = self._registry_path(pointer_rel)
        write_json_atomic(self.journal_path, journal)
        fault("journal-written")
        for rel, data in files:
            path = self._registry_path(rel)
            if not publish_new(path, data) and fs.read_file(path) != data:
                unlink_durable(self.journal_path)
                raise DesignError(REVISION_CONFLICT_CODE, f"{rel} appeared while oah was saving; nothing was "
                                  "published as current. Read the design again, then retry")
        fault("revision-published")
        now = fs.read_file(pointer_path)
        if (now is None and before is not None) or (now is not None and _sha(now) != before):
            unlink_durable(self.journal_path)
            raise DesignError(REVISION_CONFLICT_CODE, f"{pointer_rel} changed while oah was saving; nothing was "
                              "published as current. Read the design again, then retry")
        self._write_pointer(pointer_path, before, after)
        fault("pointer-written")
        written, diagnostics = self._finish(journal, fault)
        return dataclasses.replace(result, paths=tuple([rel for rel, _ in files] + [pointer_rel] + written),
                                   diagnostics=tuple(diagnostics))

    @staticmethod
    def _write_pointer(path: pathlib.Path, before: str | None, after: bytes) -> None:
        if before is None:
            if not publish_new(path, after) and fs.read_file(path) != after:
                raise DesignError(REVISION_CONFLICT_CODE, f"{path} appeared while oah was saving; read the design "
                                  "again, then retry")
        else:
            write_text_atomic(path, after.decode("utf-8"))

    def _finish(self, journal: Mapping, fault: Fault) -> tuple[list[str], list[Diagnostic]]:
        """After the commit point: rewrite derived pages, add the log entry once, drop the journal."""
        project = load_project(self.reader, journal["project"])
        design = journal.get("design")
        records = {design: load_design(self.reader, project, design)} if design else {}
        kind = journal["kind"]
        if kind == "registered":
            readme = self._registry_path(fs.readme_rel())
            text = (importlib.resources.files("oah") / "templates" / "design" / "registry-readme.md").read_bytes()
            publish_new(readme, text.replace(b"\r\n", b"\n"))
        if kind == "renamed":
            pages_for = _design_names(self.reader, project.id)
        elif kind in ("created", "saved"):
            pages_for = [design]
        else:
            pages_for = []
        page = journal.get("page") or {}
        force = {page["design"]: page["sha256"]} if page else None
        plan = self._plan(project, records, strict=False, pages_for=pages_for, force=force)
        written = self._apply(plan, fault)
        log = journal["log"]
        self.wiki.add_marked_entry(pages.log_marker(log["key"]), title=log["title"], body=log["body"],
                                   today=datetime.date.fromisoformat(log["date"]))
        written.append((pathlib.PurePosixPath(self.wiki_dir) / "log.md").as_posix())
        fault("log-written")
        unlink_durable(self.journal_path)
        return written, plan.diagnostics

    def _preflight_log(self, key: str, title: str, body: str, today: datetime.date) -> dict:
        validate_title(title)
        add_entry((self.wiki.dir / "log.md").read_bytes(), title=title, body=f"{pages.log_marker(key)}\n{body}",
                  date=today)
        return {"key": key, "title": title, "body": body, "date": today.isoformat()}

    # ---- parts -----------------------------------------------------------------------------------

    @staticmethod
    def _parts(given: Mapping[str, object]) -> dict[str, str]:
        out = {}
        for name, value in given.items():
            out[check_part(name)] = normalize_text(value, f"{name} part")
        return out

    @staticmethod
    def _check_parts(parts: Mapping[str, str], parent: Mapping[str, str] | None) -> None:
        if DESIGN_PART not in parts:
            raise DesignError(INPUT_INVALID_CODE, "a design needs its design part (--file)")
        if len(parts) > MAX_PARTS:
            raise DesignError(INPUT_INVALID_CODE, f"a revision holds at most {MAX_PARTS} parts")
        if parent is not None and REQUEST_PART in parent:
            new = parts.get(REQUEST_PART)
            if new is None or not new.startswith(parent[REQUEST_PART]):
                raise DesignError(INPUT_INVALID_CODE, "the request part keeps the user's own words: a new revision's "
                                  "request must start with the previous request text, with corrections added after "
                                  "it, and it cannot be dropped")

    @staticmethod
    def _applied(project: str, design: str, match: RevisionInfo, current: RevisionInfo, page: str | None) -> WriteResult:
        """A retry found its change on the chain: report it, never write it twice."""
        return WriteResult("applied", project, design, match.revision, match.digest, match.event, page=page,
                           current=None if match.revision == current.revision else current.revision)

    def _next_number(self, record: DesignRecord | None, project: str, design: str) -> int:
        names = self.reader.list_dir(fs.revisions_rel(project, design)) or []
        numbers = {n for n in (fs.revision_number(name) for name in names) if n}
        if record is not None:
            numbers |= {info.revision for info in record.chain}
        return max(numbers, default=0) + 1

    def _next_sequence(self, record: DesignRecord, project: str, design: str) -> int:
        names = self.reader.list_dir(fs.acceptances_rel(project, design)) or []
        numbers = {n for n in (fs.acceptance_number(name) for name in names) if n}
        numbers |= {item.sequence for item in record.acceptances}
        return max(numbers, default=0) + 1

    @staticmethod
    def _revision_files(info: RevisionInfo, texts: Mapping[str, str]) -> list[dict]:
        files = [{"role": "manifest", "revision": info.revision, "data": _b64(file_bytes(info.to_json()))}]
        files += [{"role": "part", "revision": info.revision, "part": name, "data": _b64(texts[name].encode("utf-8"))}
                  for name in pages.part_order(info.parts)]
        return files

    # ---- writes ----------------------------------------------------------------------------------

    def add_project(self, project: str, *, title: str, event: str | None = None,
                    today: datetime.date | None = None, fault: Fault = _no_fault) -> WriteResult:
        project = check_id(project)
        title = check_line(title, "title")
        event = check_event(event)
        today = today or datetime.date.today()
        with self._session():
            rel = fs.project_record_rel(project)
            self._registry_path(rel)
            if self.reader.read(rel) is not None:
                record = load_project(self.reader, project)
                if record.title == title:
                    return WriteResult("present", project, None, None, None, record.event,
                                       page=fs.project_page_rel(self.wiki_dir, project))
                raise DesignError(EXISTS_CODE, f"project {project} is already registered as {record.title!r}; rename "
                                  f"it with `oah design project rename {project} --title <title>`")
            head = self.head_ref()
            if head is None:
                raise DesignError(NOT_AUTHORITY_CODE, "HEAD is detached; check out the branch where this project's "
                                  "designs will change, then retry")
            record = ProjectRecord(project, new_uid(), title, 1, (), head, iso(self._now()), event)
            self._plan(record, {}, strict=True)
            log = self._preflight_log(event_key("registered", record.uid, project, "", 1, title_digest(title)),
                                      *pages.log_entry("registered", record), today)
            journal = self._journal("registered", project=project, design=None, event=event, branch=head, files=[],
                                    pointer={"role": "project", "before": None,
                                             "after": _b64(file_bytes(record.to_json()))}, log=log)
            result = WriteResult("registered", project, None, None, None, event,
                                 page=fs.project_page_rel(self.wiki_dir, project))
            return self._run(journal, fault, result)

    def rename_project(self, project: str, *, title: str, event: str | None = None,
                       today: datetime.date | None = None, fault: Fault = _no_fault) -> WriteResult:
        project = check_id(project)
        title = check_line(title, "title")
        event = check_event(event)
        today = today or datetime.date.today()
        with self._session():
            record = load_project(self.reader, project)
            head = self._require_authority(record)
            page = fs.project_page_rel(self.wiki_dir, project)
            if record.title == title:
                return WriteResult("unchanged", project, None, None, None, record.event, page=page)
            renamed = dataclasses.replace(record, title=title, title_revision=record.title_revision + 1,
                                          former_titles=record.former_titles + (record.title,))
            self._plan(renamed, {}, strict=True)
            before = self.reader.read(fs.project_record_rel(project))
            log = self._preflight_log(event_key("renamed", record.uid, project, "", renamed.title_revision,
                                                title_digest(title)),
                                      *pages.log_entry("renamed", renamed, old_title=record.title), today)
            journal = self._journal("renamed", project=project, design=None, event=event, branch=head, files=[],
                                    pointer={"role": "project", "before": _sha(before),
                                             "after": _b64(file_bytes(renamed.to_json()))}, log=log)
            return self._run(journal, fault, WriteResult("renamed", project, None, None, None, event, page=page))

    def new(self, project: str, design: str, *, title: str, parts: Mapping[str, object], reason: str = "Created",
            event: str | None = None, today: datetime.date | None = None, fault: Fault = _no_fault) -> WriteResult:
        project, design = check_id(project), check_id(design, "design")
        title = check_line(title, "title")
        reason = check_line(reason, "reason")
        texts = self._parts(parts)
        self._check_parts(texts, None)
        event = check_event(event)
        today = today or datetime.date.today()
        with self._session():
            record = load_project(self.reader, project)
            head = self._require_authority(record)
            digests = {name: text_digest(text) for name, text in texts.items()}
            digest = content_digest(record.uid, project, design, title, digests)
            page = self._page_rel(project, design)
            if self.reader.read(fs.pointer_rel(project, design)) is not None:
                existing = load_design(self.reader, record, design)
                first = existing.chain[-1]
                if first.digest == digest:
                    return WriteResult("applied", project, design, first.revision, first.digest, first.event,
                                       page=page)
                raise DesignError(EXISTS_CODE, f"design {project}/{design} already exists; change it with `oah design "
                                  "save`, or pick another design ID")
            number = self._next_number(None, project, design)
            info = RevisionInfo.build(project=project, uid=record.uid, design=design, revision=number, parent=None,
                                      digest=digest, title=title, parts=digests, reason=reason, origin=ORIGIN_NEW,
                                      event=event, at=iso(self._now()))
            pointer = Pointer(project, record.uid, design, info.at, (number, digest, info.manifest), None)
            created = DesignRecord(record, pointer, (info,), (), ())
            self._plan(record, {design: created}, strict=True, pages_for=[design], extra={(design, number): texts})
            log = self._preflight_log(event_key("created", record.uid, project, design, number, digest),
                                      *pages.log_entry("created", record, design=design, info=info), today)
            journal = self._journal("created", project=project, design=design, event=event, branch=head,
                                    files=self._revision_files(info, texts),
                                    pointer={"role": "design", "before": None,
                                             "after": _b64(file_bytes(pointer.to_json()))}, log=log)
            return self._run(journal, fault, WriteResult("created", project, design, number, digest, event,
                                                         page=page))

    def save(self, project: str, design: str, *, expect_revision: int, expect_digest: str, reason: str,
             parts: Mapping[str, object] | None = None, drop: Iterable[str] = (), title: str | None = None,
             from_wiki: bool = False, origin: str = ORIGIN_SAVE, event: str | None = None,
             today: datetime.date | None = None, fault: Fault = _no_fault) -> WriteResult:
        project, design = check_id(project), check_id(design, "design")
        expect_revision = check_revision(expect_revision, "expected revision")
        expect_digest = check_digest(expect_digest, "expected digest")
        reason = check_line(reason, "reason")
        title = None if title is None else check_line(title, "title")
        origin = ORIGIN_WIKI if from_wiki else check_id(origin, "origin", PART_MAX)
        given = self._parts(parts or {})
        drop = [check_part(name) for name in drop]
        if from_wiki and (given or drop):
            raise DesignError(INPUT_INVALID_CODE, "--from-wiki takes every part from the wiki page; give no parts")
        if not from_wiki and not given and not drop and title is None:
            raise DesignError(INPUT_INVALID_CODE, "nothing to save: give --file, --part, --drop-part, --title or "
                              "--from-wiki")
        if DESIGN_PART in drop:
            raise DesignError(INPUT_INVALID_CODE, "the design part cannot be dropped")
        explicit_event = event is not None
        event = check_event(event)
        today = today or datetime.date.today()
        with self._session():
            record = load_project(self.reader, project)
            head = self._require_authority(record)
            design_record = load_design(self.reader, record, design)
            current = design_record.current
            page = self._page_rel(project, design)
            current_texts = read_texts(self.reader, current)
            page_bytes = self._read_page(page)
            view = pages.read_design_page(page_bytes)
            state = self._classify(view, record, design_record)
            force = {design: _sha(page_bytes)} if from_wiki and page_bytes is not None else None
            if from_wiki:
                if view.state == "missing":
                    raise DesignError(NOT_FOUND_CODE, f"{page} does not exist, so there is no edit to register")
                if view.state == "foreign":
                    raise DesignError(PAGE_FOREIGN_CODE, f"{page} has no `oah: design-page` mark; oah does not read "
                                      "parts from it")
                if view.state == "unreadable":
                    raise DesignError(UNREGISTERED_EDIT_CODE, f"{page} lost its revision or part markers, so oah "
                                      "cannot tell the parts apart; restore the markers, then retry")
                base = design_record.revision(view.revision)
                if base is None or base.digest != view.digest:
                    raise DesignError(REVISION_CONFLICT_CODE, f"{page} shows revision {view.revision}, which is not "
                                      "in this design's history; merge the edit by hand")
                if view.revision != current.revision:
                    raise DesignError(REVISION_CONFLICT_CODE, f"{page} was edited from revision {view.revision}, but "
                                      f"the design is at revision {current.revision}; copy the edit, delete the page, "
                                      "run `oah design reconcile`, re-apply the edit, then retry")
                texts = self._parts(view.parts)
            else:
                if state in ("edited", "foreign"):
                    diagnostic = self._page_diagnostic(state, page, project, design, view)
                    raise DesignError(diagnostic.code, diagnostic.message)
                texts = dict(current_texts)
                texts.update(given)
                for name in drop:
                    if name not in texts:
                        raise DesignError(NOT_FOUND_CODE, f"revision {current.revision} has no {name} part to drop")
                    del texts[name]
            new_title = title or current.title
            digests = {name: text_digest(text) for name, text in texts.items()}
            digest = content_digest(record.uid, project, design, new_title, digests)
            if explicit_event:
                match = next((info for info in design_record.chain if info.event == event), None)
                if match is not None:
                    if match.digest != digest:
                        raise DesignError(REVISION_CONFLICT_CODE, f"event {event} already recorded revision "
                                          f"{match.revision} with other content; use a new event ID")
                    return self._applied(project, design, match, current, page)
            else:
                match = next((info for info in design_record.chain if info.parent is not None
                              and info.parent[0] == expect_revision and info.digest == digest), None)
                if match is not None:
                    return self._applied(project, design, match, current, page)
            if expect_revision != current.revision:
                raise DesignError(REVISION_CONFLICT_CODE, f"you expected revision {expect_revision}, but {project}/"
                                  f"{design} is at revision {current.revision}; read it again with `oah design show`, "
                                  "merge your change, then retry")
            if expect_digest != current.digest:
                raise DesignError(CONTEXT_STALE_CODE, f"revision {current.revision} of {project}/{design} has digest "
                                  f"{current.digest}, not {expect_digest}: the text you read is not this revision; "
                                  "read it again, then retry")
            self._check_parts(texts, current_texts)
            if digest == current.digest:
                written: list[str] = []
                diagnostics: list[Diagnostic] = []
                if from_wiki and state != "clean":
                    plan = self._plan(record, {design: design_record}, strict=False, pages_for=[design],
                                      force=force)
                    written = self._apply(plan, fault)
                    diagnostics = plan.diagnostics
                return WriteResult("unchanged", project, design, current.revision, current.digest, current.event,
                                   tuple(written), tuple(diagnostics), page)
            number = self._next_number(design_record, project, design)
            info = RevisionInfo.build(project=project, uid=record.uid, design=design, revision=number,
                                      parent=(current.revision, current.manifest), digest=digest, title=new_title,
                                      parts=digests, reason=reason, origin=origin, event=event, at=iso(self._now()))
            pointer = dataclasses.replace(design_record.pointer, current=(number, digest, info.manifest))
            saved = dataclasses.replace(design_record, pointer=pointer, chain=(info, *design_record.chain))
            self._plan(record, {design: saved}, strict=True, pages_for=[design], force=force,
                       extra={(design, number): texts})
            log = self._preflight_log(event_key("saved", record.uid, project, design, number, digest),
                                      *pages.log_entry("saved", record, design=design, info=info), today)
            before = self.reader.read(fs.pointer_rel(project, design))
            journal = self._journal("saved", project=project, design=design, event=event, branch=head,
                                    files=self._revision_files(info, texts),
                                    pointer={"role": "design", "before": _sha(before),
                                             "after": _b64(file_bytes(pointer.to_json()))}, log=log)
            if force:
                journal["page"] = {"design": design, "sha256": force[design]}
            return self._run(journal, fault, WriteResult("saved", project, design, number, digest, event, page=page))

    def accept(self, project: str, design: str, *, revision: int, digest: str, reason: str,
               event: str | None = None, today: datetime.date | None = None, fault: Fault = _no_fault) -> WriteResult:
        project, design = check_id(project), check_id(design, "design")
        revision = check_revision(revision)
        digest = check_digest(digest)
        reason = check_line(reason, "reason")
        event = check_event(event)
        today = today or datetime.date.today()
        with self._session():
            record = load_project(self.reader, project)
            head = self._require_authority(record)
            design_record = load_design(self.reader, record, design)
            page = self._page_rel(project, design)
            info = design_record.revision(revision)
            if info is None:
                raise DesignError(NOT_FOUND_CODE, f"revision {revision} is not in the history of {project}/{design}")
            if info.digest != digest:
                raise DesignError(CONTEXT_STALE_CODE, f"revision {revision} of {project}/{design} has digest "
                                  f"{info.digest}, not {digest}: what was approved is not this revision")
            latest = design_record.accepted
            if latest is not None and latest.revision == revision:
                return WriteResult("already-accepted", project, design, revision, digest, latest.event, page=page)
            if latest is not None and latest.revision > revision:
                raise DesignError(REVISION_CONFLICT_CODE, f"revision {latest.revision} is already accepted, and "
                                  "acceptance only moves forward; to go back, save the older text as a new revision "
                                  "and accept that")
            view = pages.read_design_page(self._read_page(page))
            state = self._classify(view, record, design_record)
            if state in ("edited", "foreign"):
                diagnostic = self._page_diagnostic(state, page, project, design, view)
                raise DesignError(diagnostic.code, diagnostic.message)
            if any(item.event == event for item in design_record.acceptances):
                raise DesignError(REVISION_CONFLICT_CODE, f"event {event} already recorded another acceptance; use "
                                  "a new event ID")
            sequence = self._next_sequence(design_record, project, design)
            acceptance = AcceptanceInfo.build(project=project, uid=record.uid, design=design, sequence=sequence,
                                              previous=None if latest is None else (latest.sequence, latest.record),
                                              revision=revision, digest=digest, manifest=info.manifest,
                                              reason=reason, event=event, at=iso(self._now()))
            pointer = dataclasses.replace(design_record.pointer,
                                          accepted=(sequence, revision, digest, acceptance.record))
            accepted = dataclasses.replace(design_record, pointer=pointer,
                                           acceptances=(*design_record.acceptances, acceptance))
            self._plan(record, {design: accepted}, strict=True)
            log = self._preflight_log(event_key("accepted", record.uid, project, design, revision, digest),
                                      *pages.log_entry("accepted", record, design=design, info=info,
                                                       acceptance=acceptance), today)
            before = self.reader.read(fs.pointer_rel(project, design))
            journal = self._journal("accepted", project=project, design=design, event=event, branch=head,
                                    files=[{"role": "acceptance", "sequence": sequence,
                                            "data": _b64(file_bytes(acceptance.to_json()))}],
                                    pointer={"role": "design", "before": _sha(before),
                                             "after": _b64(file_bytes(pointer.to_json()))}, log=log)
            return self._run(journal, fault, WriteResult("accepted", project, design, revision, digest, event,
                                                         page=page))

    def reconcile(self, project: str | None = None, design: str | None = None, *,
                  facts: Facts | None = None) -> ReconcileResult:
        """Rewrite derived pages and blocks from the records; never registers an edit or adds a log entry."""
        selected = [check_id(project)] if project is not None else None
        design = None if design is None else check_id(design, "design")
        with self._session():
            written: list[str] = []
            diagnostics: list[Diagnostic] = []
            unchanged = 0
            names = selected or _project_names(self.reader)
            if not names:
                plan = _Plan()
                self._index_write(plan, {}, {}, strict=False)
                written += self._apply(plan, _no_fault)
                diagnostics += plan.diagnostics
            for name in names:
                try:
                    record = load_project(self.reader, name)
                except DesignError as exc:
                    diagnostics.append(Diagnostic(exc.code, str(exc).split(": ", 1)[-1]))
                    continue
                designs = [design] if design is not None else _design_names(self.reader, name)
                if design is not None:
                    load_design(self.reader, record, design)
                plan = self._plan(record, {}, strict=False, pages_for=designs, facts=facts)
                written += self._apply(plan, _no_fault)
                unchanged += plan.same
                diagnostics += plan.diagnostics
            return ReconcileResult(tuple(dict.fromkeys(written)), unchanged, tuple(dict.fromkeys(diagnostics)))

    # ---- recovery --------------------------------------------------------------------------------

    def recover(self, *, resolved: bool = False, fault: Fault = _no_fault) -> str:
        """Finish an interrupted design write, or (resolved=True) accept files reconciled by hand."""
        if self.wiki is None:
            raise self.wiki_error  # type: ignore[misc]
        with self.wiki.session():
            journal = self._load_journal()
            if journal is None:
                return "OAH_DESIGN_RECOVER_NOTHING nothing to recover"
            if resolved:
                return self._resolve_journal(journal)
            target = self._journal_store(journal)
            moved = target.wiki_dir != journal.get("wiki_dir")
            files, (pointer_rel, before, after) = self._rebuild(journal)
            conflicts = []
            for rel, data in files:
                current = fs.read_file(target._registry_path(rel))
                if current is not None and current != data:
                    conflicts.append(rel)
            pointer_path = target._registry_path(pointer_rel)
            now = fs.read_file(pointer_path)
            if now != after and not ((now is None and before is None) or (now is not None and _sha(now) == before)):
                conflicts.append(pointer_rel)
            if conflicts:
                raise DesignError(RECOVERY_CONFLICT_CODE, f"{', '.join(conflicts)} no longer match the journal; nothing "
                                  f"was changed. {self.journal_path} keeps the bytes oah meant to write (base64). "
                                  "Reconcile the files by hand, then run `oah design recover --resolved`")
            for rel, data in files:
                path = target._registry_path(rel)
                if fs.read_file(path) is None:
                    publish_new(path, data)
            fault("revision-published")
            if now != after:
                target._write_pointer(pointer_path, before, after)
            fault("pointer-written")
            written, diagnostics = target._finish(journal, fault)
            lines = [f"OAH_DESIGN_RECOVERED {journal['kind']} {journal['project']}/{journal.get('design') or '-'}: "
                     f"{len(files)} record files and the pointer are published, {len(written)} wiki files written"]
            if moved:
                lines.append(f"  the wiki moved from {journal.get('wiki_dir')} to {target.wiki_dir}; pages and the log "
                             "entry went to the new place")
            lines.extend(f"warning {item}" for item in diagnostics)
            return "\n".join(lines)

    def _journal_store(self, journal: Mapping) -> "DesignStore":
        """A store for the journal's worktree, after checking it is this clone's and still on the same branch."""
        from oah.config import ConfigError, load_config
        from oah.paths import GitMissing, NotARepo, repo_info, run_git
        from oah.wiki.base import DEFAULTS

        root = pathlib.Path(journal["root"])
        problem = None
        try:
            info = repo_info(root) if root.is_dir() else None
        except (NotARepo, GitMissing):
            info = None
        if info is None:
            problem = f"the worktree {root} no longer exists"
        elif pathlib.Path(info.root).resolve() != root.resolve():
            problem = f"{root} is no longer a worktree root"
        elif pathlib.Path(info.common_dir).resolve() != pathlib.Path(journal["common_dir"]).resolve() or \
                pathlib.Path(info.common_dir).resolve() != pathlib.Path(self.info.common_dir).resolve():
            problem = f"{root} belongs to another clone"
        else:
            head = run_git(root, "symbolic-ref", "--quiet", "HEAD").stdout.strip()
            if head != journal["branch"]:
                problem = (f"{root} is now on {head or 'a detached HEAD'}, not {journal['branch']}; check that branch "
                           "out there, then run `oah design recover` again")
        if problem:
            raise DesignError(RECOVERY_CONFLICT_CODE, f"{problem}. Nothing was changed; {self.journal_path} keeps the "
                              "bytes. When the write is no longer wanted, run `oah design recover --resolved`")
        try:
            settings = WikiSettings.from_mapping(load_config(info.root).section("wiki", DEFAULTS))
        except ConfigError as exc:
            raise DesignError(RECOVERY_CONFLICT_CODE, f"{root}: {exc}") from exc
        return DesignStore(info, settings, self.state, lock_timeout=self.lock_timeout, now=self._now)

    def _resolve_journal(self, journal: Mapping) -> str:
        root = pathlib.Path(journal["root"])
        where = "missing"
        if root.is_dir():
            try:
                target = self._journal_store(journal)
            except DesignError:
                target = None
            if target is not None:
                where = "present"
                try:
                    record = load_project(target.reader, journal["project"])
                    if journal.get("design"):
                        load_design(target.reader, record, journal["design"])
                except DesignError as exc:
                    if exc.code != NOT_FOUND_CODE:
                        raise DesignError(RECOVERY_CONFLICT_CODE, f"the records still do not verify: {exc}. Fix them "
                                          "by hand, then run `oah design recover --resolved` again") from exc
        moment = datetime.datetime.now(datetime.timezone.utc)
        kept = self.journal_path.parent / RESOLVED_DIR_NAME / f"design-journal-{moment.strftime('%Y%m%dT%H%M%S.%fZ')}.json"
        kept.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.journal_path, kept)
        fsync_folder(kept.parent)
        fsync_folder(self.journal_path.parent)
        append_jsonl(self.journal_path.parent / RESOLUTIONS_NAME,
                     {"version": 1, "at": moment.isoformat(timespec="seconds"), "kind": journal["kind"],
                      "project": journal["project"], "design": journal.get("design"), "event": journal["event"],
                      "worktree": where, "log_key": journal["log"]["key"], "resolved_journal": str(kept)})
        lines = [f"OAH_DESIGN_RECOVERY_RESOLVED the journal is kept as {kept}"]
        if where == "missing":
            lines.append("  its worktree is gone, so nothing could be checked or written")
        lines.append(f"  the log entry {pages.log_marker(journal['log']['key'])} may be missing; nothing adds it later")
        return "\n".join(lines)


# Public helpers -----------------------------------------------------------------------------------


def open_store(root: os.PathLike | str = ".", *, env: Mapping[str, str] | None = None,
               settings: WikiSettings | None = None, lock_timeout: float = 10.0,
               now: Callable[[], datetime.datetime] | None = None, reader: fs.Reader | None = None,
               facts: Facts | None = None) -> DesignStore:
    """The design store of the repository that contains `root`, with the `[wiki]` settings of its config.

    `facts(project, design)` may add short words to project list lines (for example a source check's result). Writes
    call it inside the wiki lock, so it must only read records that are already on disk: no network or model call.
    A provider that fails or returns words that do not validate is left out with a diagnostic; it never blocks a write.
    """
    from oah.config import load_config
    from oah.paths import repo_info, state_dir
    from oah.wiki.base import DEFAULTS

    info = repo_info(pathlib.Path(root))
    if settings is None:
        settings = WikiSettings.from_mapping(load_config(info.root).section("wiki", DEFAULTS))
    return DesignStore(info, settings, state_dir(info, os.environ if env is None else env),
                       lock_timeout=lock_timeout, now=now, reader=reader, facts=facts)


def explain_binding(binding: DesignBinding | None, store: DesignStore, *,
                    against: str = "current") -> tuple[str, str]:
    """The state of a binding (first match of BINDING_STATES' meaning) and a one-line reason."""
    if against not in ("current", "accepted"):
        raise ValueError("against must be current or accepted")
    if binding is None:
        return "unbound", "no design is bound"
    try:
        context = store.context(binding.project, binding.design)
    except DesignError as exc:
        if exc.code == NOT_FOUND_CODE:
            return "missing", str(exc)
        if exc.code in (CORRUPT_CODE, PATH_INVALID_CODE):
            return "invalid", str(exc)
        raise
    if context.project.uid != binding.uid:
        return "invalid", (f"the binding names project uid {binding.uid}, but {binding.project} here has uid "
                           f"{context.project.uid}: another project with the same ID")
    if context.freshness == "unknown" and context.source != "reader":
        return "unknown", "the authority copy cannot be read, so whether this is current is unknown"
    target = context.current if against == "current" else context.accepted
    history = {info.revision: info for info in context.revisions}
    if target is None:
        known = history.get(binding.revision)
        if known is None or known.digest != binding.digest:
            return "invalid", f"revision {binding.revision} with that digest is not in the history"
        return "stale", "no revision is accepted"
    if binding.revision == target.revision:
        if binding.digest != target.digest:
            return "invalid", (f"revision {target.revision} has digest {target.digest} here, not {binding.digest}: "
                               "its bytes changed under the same revision, or the binding comes from another copy")
        if context.page_state in ("edited", "foreign"):
            return "edited", (f"{context.page} was edited after oah wrote it (or cannot be checked); the human copy "
                              "may say something else than the record")
        return "current", f"matches {binding.line()}"
    known = history.get(binding.revision)
    if known is None or known.digest != binding.digest:
        return "invalid", f"revision {binding.revision} with that digest is not in the history"
    return "stale", f"the design moved from revision {binding.revision} to revision {target.revision}"
