# 설계 패키지 공개 API — 다른 단계가 부르는 이름(저장소 열기·읽기·쓰기·결속 판정·출력 경로)을 한곳에서 내보낸다.
"""Designs: one product's design, its revisions and its wiki pages. Lanes B to E use these names.

Records live in `.oah/designs/<project>/` (committed); pages in `<[wiki] dir>/designs/`; one journal in the state
folder. `open_store(root)` gives a `DesignStore`. Reads (`projects`, `designs`, `snapshot`, `context`,
`diagnostics`) take no lock and never write; on a branch other than the project's authority branch they return the
authority branch's committed copy and report `freshness`. Writes (`add_project`, `rename_project`, `new`, `save`,
`accept`, `reconcile`, `recover`) run under the wiki lock with the design journal, and the first four refuse
outside the authority branch with `OAH_DESIGN_NOT_AUTHORITY`.

`DesignBinding` is what a lease, packet or brief stores; `explain_binding(binding, store, against=...)` returns one
of `BINDING_STATES` with a reason. `design_output_paths(settings)` and `is_design_output(settings, rel)` say which
repository paths a design write may change, without touching the file system.
"""
from __future__ import annotations

from oah.design.fs import GitRefReader, Reader, WorktreeReader, design_output_paths, is_design_output
from oah.design.model import (RESERVED_PARTS, AcceptanceInfo, DesignBinding, DesignError, Diagnostic, ProjectRecord,
                              RevisionInfo)
from oah.design.store import (BINDING_STATES, PAGE_STATES, Context, DesignRecord, DesignStore, DesignSummary,
                              ReconcileResult, RecoveryState, Snapshot, WriteResult, explain_binding, open_store)

__all__ = ["BINDING_STATES", "PAGE_STATES", "RESERVED_PARTS", "AcceptanceInfo", "Context", "DesignBinding",
           "DesignError", "DesignRecord", "DesignStore", "DesignSummary", "Diagnostic", "GitRefReader",
           "ProjectRecord", "Reader", "ReconcileResult", "RecoveryState", "RevisionInfo", "Snapshot",
           "WorktreeReader", "WriteResult", "design_output_paths", "explain_binding", "is_design_output",
           "open_store"]
