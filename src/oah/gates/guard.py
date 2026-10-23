# 종료 가드 — 작업의 선언된 원장이 unlazy 기준으로 충족되고 바인딩이 최종 입력과 맞는지 판정하고, 포기는 인계로 보고하며 요약을 만든다.
"""The gate check behind `oah finish` (and the lease-aware Stop hook).

A task's ledgers are the lease's declared `gates` paths, or `.oah/tasks/<task>/GATES.md` when none are
declared and that file exists. With unlazy, every ledger must be met by unlazy's `--status`, and every
non-abandoned gate must have a binding that matches the final inputs. Without unlazy, the structural
parser decides, and finish passes only with `--allow-structural` (recorded as unverified). An
abandoned gate never counts as success: the outcome is `handoff` and the ids are reported.
"""
from __future__ import annotations

import dataclasses
import hashlib
import pathlib
from typing import Mapping, Sequence

from oah.gates import check as check_mod
from oah.gates import summary as summary_mod
from oah.gates.bindings import BindingStore
from oah.gates.check import GateRefused
from oah.gates.context import GateTools, default_ledger
from oah.gates.inputs import collect
from oah.gates.ledger import parse_ledger, sha256_text
from oah.gates.status import LedgerStatus, ledger_status, structural_status


@dataclasses.dataclass
class Assessment:
    ledgers: list[pathlib.Path]
    verified: bool
    statuses: dict[str, LedgerStatus]
    fresh: dict[str, dict[str, str]]
    invalid: list[str]
    unmet: list[str]
    stale: list[str]
    handoff: list[str]
    reason: str  # why the result is structural only, or ""


@dataclasses.dataclass
class GuardReport:
    outcome: str  # met | handoff | structural | none
    verified: bool
    handoff: list[str]
    summary_text: str | None

    def event(self, ledger_count: int) -> dict:
        return {"outcome": self.outcome, "verified": self.verified, "handoff": self.handoff,
                "ledgers": ledger_count}


def resolve_ledgers(root: pathlib.Path, task: str, declared: Sequence[str]) -> list[pathlib.Path]:
    """The ledgers that govern a task. A declared ledger that does not exist is refused."""
    root = pathlib.Path(root)
    if declared:
        paths = [root / pathlib.PurePosixPath(entry) for entry in declared]
        missing = [entry for entry, path in zip(declared, paths) if not path.is_file()]
        if missing:
            raise GateRefused("OAH_GATES_MISSING", f"declared ledger {', '.join(missing)} does not exist; create it "
                                                   f"(oah gates new {task}) or renew the lease with the right --gates")
        return paths
    fallback = default_ledger(root, task)
    return [fallback] if fallback.is_file() else []


def _label(gate_id: str, key: str, many: bool) -> str:
    return f"{key}:{gate_id}" if many else gate_id


def assess(root: pathlib.Path, task: str, declared: Sequence[str], state_dir: pathlib.Path, *, tools: GateTools,
           env: Mapping[str, str] | None = None, env_names: Sequence[str] = ()) -> Assessment:
    """Gather every ledger's status and binding freshness without writing anything."""
    root = pathlib.Path(root)
    ledgers = resolve_ledgers(root, task, declared)
    result = Assessment(ledgers, tools.available, {}, {}, [], [], [], [], tools.reason)
    many = len(ledgers) > 1
    manifest = None
    store = BindingStore(state_dir, task)
    for path in ledgers:
        key = check_mod.ledger_key(root, path)
        if tools.available:
            status = ledger_status(path, unlazy=tools.unlazy_dir, node=tools.node, env=env, root=root)
        else:
            status = structural_status(parse_ledger(path))
        result.statuses[key] = status
        if status.verdict == "invalid":
            result.invalid.append(f"{key}: {status.detail or '; '.join(status.errors)}")
            continue
        result.unmet += [_label(gate_id, key, many) for gate_id in status.unmet]
        result.handoff += [_label(gate_id, key, many) for gate_id in status.abandoned]
        if tools.available:
            if manifest is None:
                manifest = collect(root, env_names=env_names, env=env,
                                   tools=check_mod.tool_versions(tools.unlazy_dir))
            fresh = check_mod.freshness(root, path, store, unlazy_dir=tools.unlazy_dir, env_names=env_names,
                                        env=env, manifest=manifest)
            result.fresh[key] = fresh
            result.stale += [f"{_label(gate_id, key, many)} ({state})" for gate_id, state in fresh.items()
                             if state in ("stale", "missing") and gate_id not in status.unmet]
    return result


def _summary(root: pathlib.Path, task: str, state_dir: pathlib.Path, found: Assessment) -> str:
    bound = BindingStore(state_dir, task).load() if found.verified else {}
    sections = []
    for path in found.ledgers:
        key = check_mod.ledger_key(root, path)
        ledger = parse_ledger(path)
        bindings = bound.get(key, {})
        entries = []
        for gate in ledger.gates:
            binding = bindings.get(gate.id) if found.verified and gate.id not in ledger.abandoned else None
            entries.append(summary_mod.Entry(
                gate_id=gate.id, status="abandoned" if gate.id in ledger.abandoned else "met",
                binding=binding["kind"] if isinstance(binding, dict) else "none",
                title=sha256_text(gate.title),
                check=sha256_text(gate.check) if gate.check else None,
                expect=sha256_text(gate.expect) if gate.expect else None,
                evidence=sha256_text(gate.evidence or ""),
                inputs=binding.get("inputs") if isinstance(binding, dict) else None))
        sections.append((hashlib.sha256(path.read_bytes()).hexdigest(), entries))
    return summary_mod.render(sections, verified="unlazy" if found.verified else "structural")


def evaluate(root: pathlib.Path, task: str, declared: Sequence[str], state_dir: pathlib.Path, *, tools: GateTools,
             allow_structural: bool, env: Mapping[str, str] | None = None, env_names: Sequence[str] = (),
             commit_ledgers: bool = False) -> GuardReport:
    """Decide whether `oah finish` may close the task. Raises GateRefused with the reason otherwise."""
    root = pathlib.Path(root)
    found = assess(root, task, declared, state_dir, tools=tools, env=env, env_names=env_names)
    if not found.ledgers:
        return GuardReport("none", tools.available, [], None)
    if found.invalid:
        raise GateRefused("OAH_GATES_INVALID", "; ".join(found.invalid) + f". Fix the ledger and run oah gates lint "
                                                                          f"{task}")
    if found.unmet:
        how = (f"run oah gates check {task} (inspect the CHECK commands; add --approve to run them)"
               if found.verified else "meet them and record their evidence")
        raise GateRefused("OAH_GATES_UNMET", f"gates not met: {', '.join(found.unmet)}. To finish, {how}, then run "
                                             "oah finish again. If a gate is genuinely impossible, write "
                                             "ABANDON: <id> <reason> in the ledger")
    if found.verified and found.stale:
        raise GateRefused("OAH_GATES_STALE", f"evidence does not match the current files for {', '.join(found.stale)}. "
                                             f"Run oah gates check {task}, then oah finish again")
    if not found.verified and not allow_structural:
        raise GateRefused("OAH_GATES_UNVERIFIED", f"{found.reason}. Install unlazy and run oah gates check {task}, or "
                                                  "pass --allow-structural to finish on the boxes alone (recorded "
                                                  "as unverified)")
    outcome = "structural" if not found.verified else "handoff" if found.handoff else "met"
    text = None if commit_ledgers else _summary(root, task, state_dir, found)
    return GuardReport(outcome, found.verified, found.handoff, text)
