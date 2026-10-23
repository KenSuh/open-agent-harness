# 게이트 실행 환경 — 기본 설정값, 기본 원장 경로, unlazy·node 찾기를 한곳에서 정한다.
from __future__ import annotations

import dataclasses
import pathlib
from typing import Mapping

from oah import unlazy as unlazy_mod

# The [gates] config section's defaults. Core functions take these as parameters.
DEFAULTS = {
    "commit_ledgers": False,  # true: commit the full ledger instead of writing GATES.summary.md
    "stop_max_blocks": 6,  # the Stop hook releases after this many blocks without progress
    "input_env": [],  # environment variable names whose values are gate inputs (only hashes are kept)
    "check_timeout": 120,  # seconds per CHECK, passed to unlazy as --timeout
}
LEDGER_NAME = "GATES.md"
SUMMARY_NAME = "GATES.summary.md"


def task_dir(root: pathlib.Path, task: str) -> pathlib.Path:
    return pathlib.Path(root) / ".oah" / "tasks" / task


def default_ledger(root: pathlib.Path, task: str) -> pathlib.Path:
    return task_dir(root, task) / LEDGER_NAME


@dataclasses.dataclass(frozen=True)
class GateTools:
    unlazy_dir: pathlib.Path | None
    node: tuple[str, ...] | None
    reason: str  # why the tools are unavailable, or "" when both were found

    @property
    def available(self) -> bool:
        return self.unlazy_dir is not None and self.node is not None


def find_tools(root: pathlib.Path | None, env: Mapping[str, str], home: pathlib.Path) -> GateTools:
    """Find unlazy (OAH_UNLAZY_DIR, the project's skill dirs, the user's), verify its pin, and find node.

    Raises UnlazyError (OAH_UNLAZY_UNPINNED) for a discovered copy that is not the pinned one.
    """
    unlazy_dir, reason = unlazy_mod.find_unlazy_dir(root, env, home)
    if unlazy_dir is None:
        target = unlazy_mod.default_install_dir(home)
        reason = reason.split(" (install:")[0]
        return GateTools(None, None, f"{reason}; install the pinned copy: {unlazy_mod.install_hint(target)}")
    unlazy_mod.require_pinned(unlazy_dir)  # a copy that is not the pinned one is refused, never used
    node = unlazy_mod.resolve_node(env)
    if node is None:
        return GateTools(unlazy_dir, None, f"node was not found on PATH; {unlazy_mod.NODE_HINT}")
    return GateTools(unlazy_dir, node, "")
