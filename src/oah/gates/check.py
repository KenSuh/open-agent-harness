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

A task that follows a design (`oah.design.binding.gate_provenance`) adds one requirement to every gate,
whatever its OAH-INPUTS prefixes: the entry carries the task evidence key it was checked under, and that key
must be one the task still counts (its binding, or one an explicit carry moved from) while the design is
current. `run_check` refuses before any CHECK when the design does not permit work, brackets the run with
two design observations (P0 before, P1 after) and publishes nothing new when they differ or the lease
generation moved. Design-origin publication (`publish_design_bindings`) keeps an unchanged gate's stored
entry as it was, appends a complete old entry to a bounded semantic history only on a meaningful change,
and rotates that history into create-only archive pages. A task without a design keeps `BindingStore.replace`
and its exact fields.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import glob
import pathlib
import re
from typing import Mapping, Sequence

import hashlib
import json
import os

from oah import __version__
from oah import unlazy as unlazy_mod
from oah.gates.bindings import BindingStore
from oah.gates.inputs import InputsUncovered, Manifest, collect
from oah.gates.ledger import Gate, Ledger, gate_state, parse_ledger, sha256_text

LIVE_LIMIT = 1024 * 1024
RECENT_LIMIT = 128
HISTORY_DIR_NAME = "design-history"
_DESIGN_STATE_CODES = {"required": "OAH_DESIGN_BINDING_REQUIRED", "needs-review": "OAH_DESIGN_NEEDS_REVIEW",
                       "stale": "OAH_DESIGN_CONTEXT_STALE", "invalid": "OAH_DESIGN_BINDING_INVALID",
                       "edited": "OAH_DESIGN_UNREGISTERED_EDIT", "missing": "OAH_DESIGN_NOT_FOUND",
                       "unknown": "OAH_DESIGN_FRESHNESS_UNKNOWN"}

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


def _matches(binding: dict | None, gate: Gate, inputs: str, provenance=None) -> bool:
    if not isinstance(binding, dict):
        return False
    digests = _gate_digests(gate)
    same = all(binding.get(key) == value for key, value in digests.items()) and binding.get("inputs") == inputs
    if provenance is None:
        return same  # a task without a design: unchanged rule
    return same and binding.get("design") in provenance.accepts


def _provenance(store: BindingStore, root: pathlib.Path):
    """The task's design provenance (None for a task that never followed a design)."""
    from oah.design.binding import gate_provenance

    return gate_provenance(store.state_dir, store.task, root=pathlib.Path(root))


def _observation(provenance, generation: int | None) -> str:
    payload = {"current": provenance.current, "accepts": sorted(provenance.accepts), "state": provenance.state,
               "generation": generation}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _lease_generation(store: BindingStore) -> int | None:
    from oah.lease import Store

    lease = next((item for item, _stale in Store(store.state_dir).snapshot() if item.task == store.task), None)
    return None if lease is None else lease.generation


def require_design_work(store: BindingStore, root: pathlib.Path):
    """P0 for a design-origin task: refuse before any CHECK unless its design permits work."""
    provenance = _provenance(store, root)
    if provenance is not None and provenance.state != "current":
        code = _DESIGN_STATE_CODES.get(provenance.state, "OAH_DESIGN_BINDING_INVALID")
        raise GateRefused(code, f"task {store.task}'s design is {provenance.state} ({provenance.problem}); no gate can "
                          "certify it. Resolve the design (oah design task shows the next step), then run oah gates "
                          "check again")
    return provenance


def semantic_identity(entry: dict) -> str:
    """GateSemanticIdentityV1: what the evidence means, not when it was recorded or how often it was carried."""
    carry = entry.get("carry") if isinstance(entry.get("carry"), dict) else {}
    payload = {"kind": carry.get("source_kind") or entry.get("kind"), "evidence": entry.get("evidence"),
               "definition": entry.get("definition"), "title": entry.get("title"), "inputs": entry.get("inputs"),
               "prefixes": entry.get("prefixes"), "design": entry.get("design"),
               "edge": [carry.get("old_design"), carry.get("new_design"), carry.get("decision")]
               if carry.get("old_design") != carry.get("new_design") else None}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _size(data: dict) -> int:
    return len((json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _history(data: dict) -> tuple[list[dict], dict | None]:
    history = data.get("design_history", [])
    head = data.get("design_archive_head")
    if (not isinstance(history, list) or len(history) > RECENT_LIMIT or not all(isinstance(item, dict) and
                                                                                 "design_history" not in item
                                                                                 for item in history)
            or (head is not None and not (isinstance(head, dict) and set(head) == {"sha256", "bytes", "previous_head"}))):
        raise GateRefused("OAH_GATES_HISTORY_INVALID", "the gate history of this task is malformed; restore "
                          "bindings.json from the archive head it names, or remove it and run oah gates check again")
    return list(history), head


def publish_design_bindings(store: BindingStore, key: str, entries: dict[str, dict], old: list[dict],
                            check=None) -> dict:
    """Publish one ledger's design-origin entries under the state lock, with bounded semantic history.

    `old` are complete previous entries that a meaningful change replaced. `check()` runs under the lock first and
    raises to publish nothing (a moved design or lease generation). Rotation writes the exact previous live bytes
    to a create-only archive page and then replaces the live file: that replace is the single commit point."""
    from oah.atomic import StateCorrupt, write_json_atomic
    from oah.lockfile import StateLock

    with StateLock(store.state_dir / ".lock", timeout=store.lock_timeout):
        if check is not None:
            check()
        try:
            raw = store.path.read_bytes()
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            raise GateRefused("OAH_GATES_HISTORY_STORAGE", f"{store.path} cannot be read ({exc})") from None
        if raw is not None and len(raw) > LIVE_LIMIT:
            raise GateRefused("OAH_GATES_CURRENT_TOO_LARGE", f"{store.path} is {len(raw)} bytes, over {LIVE_LIMIT}")
        ledgers = store.load()
        data = json.loads(raw.decode("utf-8")) if raw is not None else {"version": 1, "ledgers": {}}
        history, head = _history(data)
        known = {semantic_identity(item) for item in history}
        added = []
        for item in old:
            identity = semantic_identity(item)
            if identity not in known:
                known.add(identity)
                added.append({k: v for k, v in item.items() if k != "design_history"})
        current = dict(ledgers, **{key: entries})
        candidate = {"version": 1, "ledgers": current, "design_history": history + added, "design_archive_head": head}
        if len(candidate["design_history"]) > RECENT_LIMIT or _size(candidate) > LIVE_LIMIT:
            if raw is None:
                raise GateRefused("OAH_GATES_CURRENT_TOO_LARGE", "the current gate entries alone are over the live "
                                  f"limit ({_size(candidate)} bytes); split the task's ledgers")
            page = store.path.parent / HISTORY_DIR_NAME / (hashlib.sha256(raw).hexdigest() + ".json")
            try:
                page.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                except FileExistsError:
                    if page.read_bytes() != raw:
                        raise GateRefused("OAH_GATES_HISTORY_INVALID", f"{page} exists with other bytes; nothing was "
                                          "changed") from None
                else:
                    try:
                        os.write(fd, raw)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            except OSError as exc:
                raise GateRefused("OAH_GATES_HISTORY_STORAGE", f"the gate history page {page} ({len(raw)} bytes) cannot "
                                  f"be written ({exc}); free storage or fix permissions, then run the same check "
                                  "again (successful unchanged gates are not rerun)") from None
            candidate = {"version": 1, "ledgers": current, "design_history": added[-RECENT_LIMIT:],
                         "design_archive_head": {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                                                 "previous_head": head}}
            if _size(candidate) > LIVE_LIMIT:
                sizes = {name: _size({"ledgers": {name: value}}) for name, value in current.items()}
                raise GateRefused("OAH_GATES_CURRENT_TOO_LARGE", f"the current gate entries alone are over the live "
                                  f"limit (per ledger bytes: {sizes}); split the task's ledgers")
        try:
            write_json_atomic(store.path, candidate)
        except OSError as exc:
            raise GateRefused("OAH_GATES_HISTORY_STORAGE", f"{store.path} cannot be written ({exc})") from None
        return candidate


def history_page(store: BindingStore, digest: str | None = None) -> dict:
    """One verified page of a task's gate history: the live recent history, or an archive page by exact digest."""
    if digest is None:
        data = json.loads(store.path.read_bytes().decode("utf-8")) if store.path.exists() else {"ledgers": {}}
        history, head = _history(data)
        return {"page": "live", "history": history, "archive_head": head}
    if not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        raise GateRefused("OAH_GATES_HISTORY_INVALID", "an archive page is named by its 64-hex sha256")
    page = store.path.parent / HISTORY_DIR_NAME / f"{digest}.json"
    try:
        raw = page.read_bytes()
    except FileNotFoundError:
        raise GateRefused("OAH_GATES_HISTORY_INVALID", f"archive page {digest} is missing") from None
    if hashlib.sha256(raw).hexdigest() != digest or len(raw) > LIVE_LIMIT:
        raise GateRefused("OAH_GATES_HISTORY_INVALID", f"archive page {digest} does not match its digest; it was "
                          "changed and is not used")
    data = json.loads(raw.decode("utf-8"))
    history, head = _history(data)
    return {"page": digest, "history": history, "archive_head": head, "ledgers": data.get("ledgers")}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _binding(kind: str, gate: Gate, inputs: str, carry: dict | None = None, design: dict | None = None) -> dict:
    entry = {"kind": kind, **_gate_digests(gate), "inputs": inputs, "recorded_at": _now()}
    if carry is not None:
        entry["carry"] = carry
    if design is not None:
        entry.update(design)
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
    provenance = _provenance(store, root)  # authority first: a design that is not current certifies nothing
    result = {}
    for gate in ledger.gates:
        state = gate_state(gate, ledger.abandoned)
        if state == "abandoned":
            result[gate.id] = "abandoned"
        elif gate.id not in bound:
            result[gate.id] = "missing"
        elif state == "met" and _matches(bound[gate.id], gate, manifest.digest_for(gate.inputs), provenance):
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
    provenance = require_design_work(store, root)  # P0: refused before any CHECK runs
    generation = _lease_generation(store) if provenance is not None else None
    p0 = _observation(provenance, generation) if provenance is not None else None
    before = collect_inputs(root, [ledger_path], store, env_names=env_names, env=env, tools=tools, node=node)
    bound = store.load().get(key, {})
    affected = {gate.id for gate in ledger.gates if gate.id not in ledger.abandoned
                and not _matches(bound.get(gate.id), gate, before.digest_for(gate.inputs), provenance)}
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
    replaced: list[dict] = []
    stamp = None
    if provenance is not None:
        stamp = {"design": provenance.current, "observation": {"p0": p0}}
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
        elif gate.id not in affected and state == "met" and _matches(old, gate, inputs_after, provenance):
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
        if provenance is None:
            if action == "run":
                entries[gate.id] = _binding("run", gate, inputs_after)
            elif action == "attest":
                entries[gate.id] = _binding("attest", gate, inputs_after)
            elif action == "carry":
                entries[gate.id] = _binding("carry", gate, inputs_after, carry={
                    "old_inputs": old["inputs"], "new_inputs": inputs_after, "evidence": old["evidence"]})
        elif action in ("run", "attest"):
            entries[gate.id] = _binding(action, gate, inputs_after, design=stamp)
            if isinstance(old, dict) and semantic_identity(old) != semantic_identity(entries[gate.id]):
                replaced.append(old)
        elif action == "carry" and old.get("design") == provenance.current:
            entries[gate.id] = old  # a no-op check keeps the stored entry and its original recorded_at
        elif action == "carry":
            entries[gate.id] = _binding("carry", gate, inputs_after, carry={
                "old_inputs": old["inputs"], "new_inputs": inputs_after, "evidence": old["evidence"],
                "source_kind": (old.get("carry") or {}).get("source_kind") or old.get("kind"),
                "old_design": old.get("design"), "new_design": provenance.current,
                "decision": "explicit-design-carry"}, design=stamp)
            replaced.append(old)
        report.lines.append(f"OAH_GATE {gate.id} {action}")
    if provenance is None:
        store.replace(key, entries)
        return report

    def unchanged():  # P1 under the publication lock: the design and the lease must not have moved
        later = _provenance(store, root)
        if later is None or _observation(later, _lease_generation(store)) != p0:
            raise GateRefused("OAH_DESIGN_CHANGED", f"task {store.task}'s design or lease changed while its gates were "
                              "checked; nothing new was recorded and earlier evidence is unchanged. Run oah gates "
                              "check again")
    for entry in entries.values():
        if entry.get("observation") == {"p0": p0}:
            entry["observation"] = {"p0": p0, "p1": p0}
    for gate in ledger.gates:  # old evidence is kept as it was (stale for the new key), never re-tagged
        if gate.id not in entries and isinstance(bound.get(gate.id), dict):
            entries[gate.id] = bound[gate.id]
    publish_design_bindings(store, key, entries, replaced, check=unchanged)
    return report
