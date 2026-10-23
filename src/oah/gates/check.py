# 게이트 검사 흐름 — 바뀐 게이트만 골라 unlazy --reverify 로 다시 돌리고, 입력이 같은 게이트는 이월 기록을 남기며 바인딩 신선도를 판정한다.
"""`oah gates check` and binding freshness.

A gate is *affected* when it has no binding, or when its definition (CHECK, EXPECT, CWD), title,
evidence or input digest differs from the binding. When any runnable gate is affected, oah runs
unlazy's `gate-check.mjs --reverify` on the ledger (never the default skip-met mode). unlazy 2.1.0 has
no per-gate selection, so `--reverify` reruns every runnable gate it may run; only gates it reports
as `PASS` get a fresh `run` binding. Inputs are collected before and after the run, and a gate whose
inputs changed during the run is not bound. An unaffected gate gets an explicit input-identical
`carry` record. A manual gate gets an `attest` binding when its evidence is new; a manual gate whose
inputs changed under unchanged evidence is stale until someone re-attests it.

`freshness` is what `oah finish` and the Stop hook use: a gate is fresh only when its binding
matches the final inputs, including working-tree changes. unlazy's green status alone is not enough.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import glob
import pathlib
import re
from typing import Mapping, Sequence

from oah import __version__
from oah import unlazy as unlazy_mod
from oah.gates.bindings import BindingStore
from oah.gates.inputs import InputsUncovered, Manifest, collect
from oah.gates.ledger import Gate, Ledger, gate_state, parse_ledger, sha256_text

RESULT_RE = re.compile(r"^  (PASS|FAIL|STALE) (.+?): ")


class GateRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass
class CheckReport:
    actions: dict[str, str]  # gate id -> run|attest|carry|stale|failed|not-run|invalidated|abandoned|unmet
    lines: list[str]
    unlazy: unlazy_mod.UnlazyResult | None = None
    approval_required: bool = False

    @property
    def ok(self) -> bool:
        return all(action in ("run", "attest", "carry") for action in self.actions.values() if action != "abandoned")

    @property
    def handoff(self) -> list[str]:
        return [gate_id for gate_id, action in self.actions.items() if action == "abandoned"]


def ledger_key(root: pathlib.Path, ledger: pathlib.Path) -> str:
    """The ledger's repository-relative POSIX path (absolute when it lies outside the root)."""
    ledger = pathlib.Path(ledger).resolve()
    try:
        return ledger.relative_to(pathlib.Path(root).resolve()).as_posix()
    except ValueError:
        return ledger.as_posix()


def tool_versions(unlazy_dir: pathlib.Path | None) -> dict[str, str | None]:
    return {"oah": __version__, "unlazy": unlazy_mod.unlazy_version(unlazy_dir) if unlazy_dir else None}


def collect_inputs(root: pathlib.Path, ledger_paths: Sequence[pathlib.Path], store: BindingStore, **options) -> Manifest:
    """Exclude the task's governed ledger outputs; their definitions/evidence are checked separately."""
    from oah.lease import Store

    root = pathlib.Path(root)
    paths = list(ledger_paths)
    for lease, _stale in Store(store.state_dir).snapshot():
        if lease.task == store.task and pathlib.Path(lease.worktree).resolve() == pathlib.Path(root).resolve():
            paths += [root / pathlib.PurePosixPath(name) for name in lease.gates]
    excludes = [glob.escape(ledger_key(root, path)) for path in paths]
    try:
        return collect(root, extra_excludes=excludes, **options)
    except InputsUncovered as exc:
        raise GateRefused(exc.code, exc.message) from exc


def _gate_digests(gate: Gate) -> dict:
    return {"definition": gate.definition, "title": sha256_text(gate.title),
            "evidence": sha256_text(gate.evidence or ""),
            "prefixes": list(gate.inputs) if gate.inputs is not None else None}


def _matches(binding: dict | None, gate: Gate, inputs: str) -> bool:
    if not isinstance(binding, dict):
        return False
    digests = _gate_digests(gate)
    return all(binding.get(key) == value for key, value in digests.items()) and binding.get("inputs") == inputs


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _binding(kind: str, gate: Gate, inputs: str, carry: dict | None = None) -> dict:
    entry = {"kind": kind, **_gate_digests(gate), "inputs": inputs, "recorded_at": _now()}
    if carry is not None:
        entry["carry"] = carry
    return entry


def _parse(ledger_path: pathlib.Path) -> Ledger:
    ledger = parse_ledger(ledger_path)
    if ledger.errors:
        raise GateRefused("OAH_GATES_INVALID", f"{ledger_path}: " + "; ".join(ledger.errors[:5]) +
                          ". Fix the ledger, then run oah gates lint")
    return ledger


def freshness(root: pathlib.Path, ledger_path: pathlib.Path, store: BindingStore, *,
              unlazy_dir: pathlib.Path | None = None, env_names: Sequence[str] = (),
              env: Mapping[str, str] | None = None, manifest: Manifest | None = None) -> dict[str, str]:
    """Per gate: fresh, stale, missing or abandoned, judged against the current inputs."""
    ledger = _parse(ledger_path)
    manifest = manifest or collect_inputs(root, [ledger_path], store, env_names=env_names, env=env,
                                          tools=tool_versions(unlazy_dir))
    bound = store.load().get(ledger_key(root, ledger_path), {})
    result = {}
    for gate in ledger.gates:
        state = gate_state(gate, ledger.abandoned)
        if state == "abandoned":
            result[gate.id] = "abandoned"
        elif gate.id not in bound:
            result[gate.id] = "missing"
        elif state == "met" and _matches(bound[gate.id], gate, manifest.digest_for(gate.inputs)):
            result[gate.id] = "fresh"
        else:
            result[gate.id] = "stale"
    return result


def _results(stdout: str, outcome: str) -> set[str]:
    """Gate ids that gate-check reported as PASS, FAIL or discarded STALE results."""
    ids = set()
    for line in stdout.splitlines():
        match = RESULT_RE.match(line)
        if match and match.group(1) == outcome:
            ids.add(match.group(2).rsplit(":", 1)[-1])
    return ids


def run_check(root: pathlib.Path, ledger_path: pathlib.Path, store: BindingStore, *,
              unlazy_dir: pathlib.Path, node: Sequence[str], approve: bool = False, timeout: int | None = None,
              env: Mapping[str, str] | None = None, env_names: Sequence[str] = ()) -> CheckReport:
    """Re-verify affected gates through unlazy and record bindings. Never executes a CHECK itself."""
    root = pathlib.Path(root)
    ledger = _parse(ledger_path)
    original = {gate.id: (gate.definition, gate.title, gate.inputs) for gate in ledger.gates}
    key = ledger_key(root, ledger_path)
    tools = tool_versions(unlazy_dir)
    before = collect_inputs(root, [ledger_path], store, env_names=env_names, env=env, tools=tools, node=node)
    bound = store.load().get(key, {})
    affected = {gate.id for gate in ledger.gates if gate.id not in ledger.abandoned
                and not _matches(bound.get(gate.id), gate, before.digest_for(gate.inputs))}
    report = CheckReport(actions={}, lines=[])
    after, passed, failed, discarded = before, set(), set(), set()
    if any(gate.runnable for gate in ledger.gates if gate.id in affected):
        result = unlazy_mod.gate_check(unlazy_dir, ledger_path, node=node, mode="reverify", approve=approve,
                                       cwd=root, root=root, timeout=timeout, env=env)
        report.unlazy = result
        report.approval_required = "APPROVAL REQUIRED" in result.stdout
        if result.verdict == "error":
            detail = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
            raise GateRefused("OAH_UNLAZY_FAILED", f"unlazy gate-check failed: {detail[-600:]}")
        passed, failed = _results(result.stdout, "PASS"), _results(result.stdout, "FAIL")
        discarded = _results(result.stdout, "STALE")
        after = collect_inputs(root, [ledger_path], store, env_names=env_names, env=env, tools=tool_versions(unlazy_dir),
                               node=node)
        ledger = _parse(ledger_path)
    entries: dict[str, dict] = {}
    for gate in ledger.gates:
        state = gate_state(gate, ledger.abandoned)
        old = bound.get(gate.id)
        inputs_before, inputs_after = before.digest_for(gate.inputs), after.digest_for(gate.inputs)
        if state == "abandoned":
            action = "abandoned"
        elif original.get(gate.id) != (gate.definition, gate.title, gate.inputs) or gate.id in discarded:
            action = "invalidated"
        elif inputs_before != inputs_after:
            action = "invalidated"
        elif gate.runnable and gate.id in failed:
            action = "failed"
        elif gate.runnable and gate.id in passed and state == "met":
            action = "run"
        elif gate.id not in affected and state == "met" and _matches(old, gate, inputs_after):
            action = "carry"
        elif gate.runnable and gate.id in affected:
            action = "not-run"
        elif not gate.runnable and state == "met" and (
                not isinstance(old, dict) or old.get("evidence") != sha256_text(gate.evidence or "")):
            action = "attest"
        elif not gate.runnable and state == "met":
            action = "stale"
        else:
            action = "unmet"
        report.actions[gate.id] = action
        if action == "run":
            entries[gate.id] = _binding("run", gate, inputs_after)
        elif action == "attest":
            entries[gate.id] = _binding("attest", gate, inputs_after)
        elif action == "carry":
            entries[gate.id] = _binding("carry", gate, inputs_after, carry={
                "old_inputs": old["inputs"], "new_inputs": inputs_after, "evidence": old["evidence"]})
        report.lines.append(f"OAH_GATE {gate.id} {action}")
    store.replace(key, entries)
    return report
