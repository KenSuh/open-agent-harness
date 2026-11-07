# 설계 출처 공개 API — 등록한 GitHub 저장소와 공식 문서를 상태 폴더에서 따라가는 Sources 와 그 열기 함수, 기본값을 내보낸다.
"""Design sources: public upstreams a design's materials come from, followed in the clone's state folder.

    sources = open_sources(root)
    sources.register(build_spec(url="https://github.com/<owner>/<repo>", paths=["crates/foo"]))
    sources.check()                                   # every source; unchanged is quiet
    sources.scheduled()                               # one bounded fair pass of the persistent queue
    report = sources.refresh_for(store, "acme", "booking", reason="start")   # the start/resume/recommend seam
    sources.matches_view(report.to_json(), view)      # recheck before consuming the report
    sources.proposals()                               # derived; never saves, accepts or rebinds a design
    sources.diff("foo", from_snapshot, to_snapshot)   # offline comparison of two stored snapshots
    open_store(root, facts=sources.list_facts(reader))   # list-line words (the host wires this)

Settings come from `[sources]` in `.oah/config.toml` (`DEFAULTS`). Nothing here is committed or written to the
working tree; `oah design save --part materials=FILE` is the only way materials enter a design.
"""
from __future__ import annotations

import os
import pathlib
from typing import Callable, Mapping

from oah.design.sources.adapters import Transport
from oah.design.sources.check import (DEFAULTS, REASONS, CheckResult, Finding, Proposal, RefreshReport,
                                      ScanReport, Sources, SourceStatus, check_settings)
from oah.design.sources.model import SourceError, SourceSpec, build_spec, freshness
from oah.design.sources.state import StateProblem
from oah.design.sources.view import DesignRef, DesignView, ViewUnavailable, read_view, view_of

DEFAULT_TRANSPORT = Transport()

__all__ = ["DEFAULTS", "DEFAULT_TRANSPORT", "REASONS", "CheckResult", "DesignRef", "DesignView", "Finding",
           "Proposal", "RefreshReport", "ScanReport", "SourceError", "SourceSpec", "SourceStatus", "Sources",
           "StateProblem", "Transport", "ViewUnavailable", "build_spec", "check_settings", "freshness",
           "open_sources", "read_view", "view_of"]


def open_sources(root: os.PathLike | str = ".", *, env: Mapping[str, str] | None = None,
                 transport: Transport | None = None, now: Callable | None = None) -> Sources:
    """The sources of the repository that contains `root`, with its `[sources]` settings."""
    from oah.config import load_config
    from oah.paths import repo_info, state_dir

    info = repo_info(pathlib.Path(root))
    settings = check_settings(load_config(info.root).section("sources", DEFAULTS))
    return Sources(state_dir(info, os.environ if env is None else env), repo_root=info.root, settings=settings,
                   transport=transport or DEFAULT_TRANSPORT, now=now)
