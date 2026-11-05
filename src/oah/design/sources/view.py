# 설계 보기 — 설계 저장소의 Context 에서 현재·수락 리비전의 참조·제목·부분 다이제스트·본문과 복구·신선도 관측을 꺼내, 실행 쪽 모듈을 들여오지 않는 공용 DesignViewV1 모양으로 만든다.
"""DesignViewV1: what source checks read about one design, built from the design store's read-only `Context`.

Identities are the store's own: project uid, design ID, revision and content digest for the current (working) and
accepted snapshots, the acceptance sequence and record digest, and each part's digest and text. A slug, a task
worktree HEAD or an unreadable record never stands in for them. Reading the store is the caller's job (lock-free
`store.context`); `read_view` wraps the store's failures as `ViewUnavailable` with a fixed code so callers keep
`unknown`, never current.
"""
from __future__ import annotations

import dataclasses
from typing import Mapping

from oah.design.sources.state import digest_of

MATERIALS_PART = "materials"


class ViewUnavailable(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True)
class DesignRef:
    project: str
    uid: str
    design: str
    revision: int
    digest: str

    def to_json(self) -> dict:
        return {"project": self.project, "uid": self.uid, "design": self.design, "revision": self.revision,
                "digest": self.digest}


@dataclasses.dataclass(frozen=True)
class SnapshotView:
    ref: DesignRef
    title: str
    parts: Mapping[str, str]   # part name -> the store's part digest
    texts: Mapping[str, str]

    def to_json(self) -> dict:
        return {"ref": self.ref.to_json(), "title": self.title, "parts": dict(sorted(self.parts.items()))}


@dataclasses.dataclass(frozen=True)
class DesignView:
    project: str
    uid: str
    authority: str
    design: str
    current: SnapshotView
    accepted: SnapshotView | None
    acceptance: Mapping | None       # {"sequence", "record"} of the latest acceptance
    page_state: str
    recovery_pending: bool
    freshness: str
    source: str
    history: tuple[DesignRef, ...] = ()

    def identity(self) -> dict:
        """The exact pair a consumer must recheck: current and accepted refs plus the acceptance identity."""
        return {"uid": self.uid, "design": self.design, "current": self.current.ref.to_json(),
                "accepted": None if self.accepted is None else self.accepted.ref.to_json(),
                "acceptance": None if self.acceptance is None else dict(self.acceptance)}

    def materials(self, which: str = "current") -> str | None:
        snapshot = self.current if which == "current" else self.accepted
        return None if snapshot is None else snapshot.parts.get(MATERIALS_PART)

    def snapshot(self, revision: int) -> SnapshotView | None:
        for item in (self.current, self.accepted):
            if item is not None and item.ref.revision == revision:
                return item
        return None

    def to_json(self) -> dict:
        return {"version": 1, "project": self.project, "uid": self.uid, "authority": self.authority,
                "design": self.design, "current": self.current.to_json(),
                "accepted": None if self.accepted is None else self.accepted.to_json(),
                "acceptance": None if self.acceptance is None else dict(self.acceptance),
                "page_state": self.page_state, "recovery_pending": self.recovery_pending,
                "freshness": self.freshness, "source": self.source,
                "history": [item.to_json() for item in self.history]}


def design_basis(view: DesignView, which: str = "current") -> str | None:
    """The design part of MaterialInputBasisV1: identity, title and every non-materials part digest."""
    snapshot = view.current if which == "current" else view.accepted
    if snapshot is None:
        return None
    parts = {name: value for name, value in sorted(snapshot.parts.items()) if name != MATERIALS_PART}
    return digest_of({"version": 1, "project": view.project, "uid": view.uid, "design": view.design,
                      "title": snapshot.title, "parts": parts})


def _snapshot(project: str, uid: str, design: str, snapshot) -> SnapshotView:
    info = snapshot.info
    return SnapshotView(DesignRef(project, uid, design, info.revision, info.digest), info.title,
                        dict(info.parts), dict(snapshot.texts))


def view_of(context) -> DesignView:
    """A DesignView from the store's Context (duck-typed, so no private store module is imported)."""
    project = context.project
    accepted = None if context.accepted is None else _snapshot(project.id, project.uid, context.design,
                                                                context.accepted)
    acceptance = None
    if context.acceptances:
        latest = context.acceptances[-1]
        acceptance = {"sequence": latest.sequence, "record": latest.record, "revision": latest.revision,
                      "digest": latest.digest}
    history = tuple(DesignRef(project.id, project.uid, context.design, info.revision, info.digest)
                    for info in context.revisions)
    return DesignView(project.id, project.uid, project.authority, context.design,
                      _snapshot(project.id, project.uid, context.design, context.current), accepted, acceptance,
                      context.page_state, bool(context.recovery.pending), context.freshness, context.source, history)


def read_view(store, project: str, design: str) -> DesignView:
    """`view_of(store.context(...))`, or ViewUnavailable(code) for the store's failures (kept unknown, never
    current)."""
    from oah.design import fs as design_fs
    from oah.design.model import DesignError

    try:
        return view_of(store.context(project, design))
    except DesignError as exc:
        raise ViewUnavailable(str(getattr(exc, "code", "OAH_DESIGN_ERROR"))) from None
    except design_fs.RefUnavailable:
        raise ViewUnavailable("ref-unavailable") from None
    except (OSError, KeyError, ValueError):
        raise ViewUnavailable("io") from None
