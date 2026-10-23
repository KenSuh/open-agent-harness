# 게이트 결속 층 — 설정된 원장을 실제 게이트 파서로 읽고 CHECK·EXPECT 가 경로를 고정(pin)하거나 실행(exec)하는지 판정한다.
from __future__ import annotations

import fnmatch
import pathlib
from dataclasses import dataclass
from typing import Iterable

from oah.impact import lexical
from oah.impact.query import Row

UNAVAILABLE = ("unavailable (no gate ledgers found; create .oah/tasks/<task>/GATES.md or configure "
               "impact.ledger_globs)")


@dataclass(frozen=True)
class GateRef:
    """One gate as the ledger parser reports it: `oah.gates.parse_ledger(path)` gates carry check and expect."""

    ledger: str  # repository-relative POSIX path of the ledger
    gate_id: str  # G1, G2, ...
    check: str
    expect: str

    @property
    def label(self) -> str:
        return f"{self.ledger}#{self.gate_id}"


def binding(gate: GateRef, needles: dict[str, str]) -> str | None:
    """pin when CHECK hashes or greps the path or EXPECT names it, exec when CHECK runs it, link when CHECK
    only names it, else None. Stems are too loose to bind a gate."""
    found = []
    for needle, via in needles.items():
        if via == "stem":
            continue
        if lexical.find_needles(gate.expect, {needle}):
            found.append("pin")
        if lexical.find_needles(gate.check, {needle}):
            found.append(lexical.classify_command(gate.check, needle) or "link")
    order = ("pin", "exec", "link")
    return min(found, key=order.index) if found else None


def rows(gates: Iterable[GateRef], target: str, *, min_needle: int) -> list[Row]:
    needles = lexical.needles_for(target, min_needle)
    out = []
    for gate in gates:
        bound = binding(gate, needles)
        if bound:
            out.append(Row(bound, "gates", "gate", gate.label, None, target, f"binding={bound}", 0))
    return out


def _matches(path: str, pattern: str) -> bool:
    """Match a root-relative pathlib glob against a revision's paths without reading the worktree."""
    def match(parts: tuple[str, ...], globs: tuple[str, ...]) -> bool:
        if not globs:
            return not parts
        if globs[0] == "**":
            return match(parts, globs[1:]) or bool(parts and match(parts[1:], globs))
        return bool(parts and fnmatch.fnmatch(parts[0], globs[0]) and match(parts[1:], globs[1:]))

    return match(pathlib.PurePosixPath(path).parts, pathlib.PurePath(pattern).parts)


def discover(root: pathlib.Path, *, rev: str | None = None) -> tuple[list[GateRef], str]:
    """Read configured ledgers from the worktree (including ignored files), or only from a pinned revision."""
    from oah.gates import parse_ledger, parse_text
    from oah.impact.defaults import configured
    from oah.impact.errors import ImpactError
    from oah.impact.mapstore import _ledgers, _safe_worktree_path, _tree_files, read_blobs, resolve_rev

    root = pathlib.Path(root)
    settings = configured(root)
    if rev is None:
        paths = _ledgers(root, settings)
        contents = None
    else:
        revision = resolve_rev(root, rev, timeout=settings["git_timeout"])
        files, excluded = _tree_files(root, revision, settings)
        selected = lambda path: any(_matches(path, pattern) for pattern in settings["ledger_globs"])
        if any(selected(path) for path in excluded):
            raise ImpactError("OAH_GATES_INVALID", "the requested revision contains a linked gate ledger; "
                              "replace it with a regular ledger and query the corrected revision")
        paths = sorted(path for path in files if selected(path))
        contents = read_blobs(root, revision, paths, settings["git_timeout"])
    gates = []
    for relative in paths:
        if contents is None and not _safe_worktree_path(root, relative):
            raise ImpactError("OAH_GATES_INVALID", f"{relative} is linked or outside the repository; replace it "
                              "with a regular ledger inside the repository and retry")
        path = root / pathlib.PurePosixPath(relative)
        if contents is not None and relative not in contents:
            raise ImpactError("OAH_GATES_INVALID", "a gate ledger blob could not be read at the requested revision; "
                              "check that revision's ledger paths and query again")
        ledger = parse_ledger(path) if contents is None else parse_text(contents[relative], path)
        if ledger.errors:
            raise ImpactError("OAH_GATES_INVALID", f"{relative} cannot be parsed ({len(ledger.errors)} problems); "
                              "fix it with oah gates lint before querying impact")
        gates.extend(GateRef(relative, gate.id, gate.check or "", gate.expect or "") for gate in ledger.gates)
    return gates, (f"{len(gates)} gates from {len(paths)} ledgers; re-run bound checks for touched paths"
                   if paths else UNAVAILABLE)
