# 엔진 호출 핵심 — 단계·난이도로 엔진·단·모델·effort 를 정하고, 브리프와 실행 폴더를 만든 뒤 proc 로 띄우거나 백그라운드로 넘기며, 실제로 돈 값을 기록한다.
"""`oah call` and `oah wait` without the command line (PLAN 5.0, 5.5, A1, A6, A10, A14).

`prepare` decides everything before launch and writes the run folder:
- the stage's engine (`OAH_STAGE_ENGINE_MISMATCH` unless overridden, which is logged as an event);
- the tier: `--tier` (source `flag`), else the packet's `## Plan` line for this stage (`packet`), else the
  rule-scored axes through `routing.recommend` (`rules`, or `flag` when axes were given). The re-ranker's
  advisory judgement is recorded next to it and never changes the tier;
- the model through the catalog (`models.map_tier`) and the launch effort through the adapter;
- the brief (`briefs.build`) and the argv; a review is always read-only.
`execute` holds the lease lock through process creation and uses `oah.engines.proc`'s job/tree handling
(prompt on stdin, the process tree killed on timeout), recording the exit code, logs, header check and
inference normalization. `start_background`
hands the same prepared run to a detached Python runner and returns at once; `wait` polls the record
for at most `WAIT_LIMIT_S` seconds and can be called again.

A task must have a lease bound to an intent (`oah start --packet`) and dispatches only while that binding is
`current` (`OAH_INTENT_NOT_CURRENT` otherwise), and only from the bound source. Every brief carries the
skills `oah.skillsel.choose` selected for it, with a reason each and the `rerank=` status; the selection is
also kept as `skills.json` in the run folder. `lease.json` carries the checked lease identity, generation
and binding, so queued runners refuse any intervening lease change before launching the engine.

The first dispatch of a task that has a packet records its intent source in
`runs/<task>/intent-binding.json`. Later rounds reuse that source; a different source, another Intent-ID,
an in-place edit or an older revision refuses with `OAH_INTENT_SOURCE_MISMATCH`, and only a raised Revision
of the same Intent-ID in the same source moves the binding forward. `resolve_worktree` turns a path,
branch or worktree name into exactly one worktree before any intent is read.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Callable, Mapping, Sequence

import oah.intent as oah_intent
import oah.packet as oah_packet
from oah import briefs, launchers, models, routing, runs, skillsel
from oah.engines import claude, codex
from oah.engines.effort import EngineError, HeaderCheck, compare_labels, load_rules, verify_inference
from oah.engines.proc import run_command

WORKER_ENV = ("OAH_WORKER", "OAH_RUN_ID", "OAH_TASK")
ISOLATED_CODEX_FLAGS = ("--ignore-user-config", "--ignore-rules")  # review rounds (PLAN 5.9 step 6)
WAIT_LIMIT_S = 540.0
DEFAULT_TASK = "adhoc"
STAGE_DEFAULTS = {"initial": "claude", "review": "codex", "correction": "codex"}
TIMEOUTS = {"low": 15, "medium": 30, "high": 60, "max": 90}
ENGINE_DEFAULTS = {
    "claude": {
        "command": ["claude"],
        "quality_order": ["opus", "sonnet", "haiku"],
        "permission_mode": claude.DEFAULT_PERMISSION_MODE,
        "tools": list(claude.DEFAULT_TOOLS),
        "tiers": {tier: {"model": "", "effort": tier, "timeout_min": TIMEOUTS[tier]} for tier in routing.TIERS},
    },
    "codex": {
        "command": ["codex"],
        "quality_order": [],
        "tiers": {tier: {"model": "", "effort": "highest" if tier == "max" else tier,
                         "timeout_min": TIMEOUTS[tier]} for tier in routing.TIERS},
    },
}
_PLAN_LINE = re.compile(r"^\s*(?:[-*]\s*)?`?(initial|review|correction)`?\s*(?:→|->|:)\s*`?(claude|codex)`?\s*"
                        r"(?:→|->|:)\s*`?(low|medium|high|max)`?\b")
_PLAN_AXES = re.compile(r"^\s*[-*]\s*(initial|review|correction):\s+kind=(\S+)\s+intensity=(\S+)\s+"
                        r"reasoning=(\S+)\s+risk=(\S+)(?:\s|$)")


@dataclasses.dataclass(frozen=True)
class Settings:
    stages: dict
    engines: dict
    models: dict

    @classmethod
    def defaults(cls) -> "Settings":
        return cls.validated(dict(STAGE_DEFAULTS), json.loads(json.dumps(ENGINE_DEFAULTS)), dict(models.DEFAULTS))

    @classmethod
    def validated(cls, stages: dict, engines: dict, models_section: dict) -> "Settings":
        def invalid(message: str) -> EngineError:
            return EngineError("OAH_CONFIG_INVALID", f".oah/config.toml: {message}")

        for stage, engine in stages.items():
            if engine not in routing.ENGINES:
                raise invalid(f"stages.{stage} = {engine!r}; use one of: {', '.join(routing.ENGINES)}")
        for engine, section in engines.items():
            if not all(isinstance(item, str) for item in section["quality_order"]):
                raise invalid(f"engines.{engine}.quality_order must list model names as strings")
            for tier, values in section["tiers"].items():
                if values["timeout_min"] <= 0:
                    raise invalid(f"engines.{engine}.tiers.{tier}.timeout_min must be a positive number of minutes")
        if models_section["ttl_hours"] <= 0:
            raise invalid("models.ttl_hours must be a positive number of hours")
        return cls(stages=stages, engines=engines, models=models_section)


@dataclasses.dataclass(frozen=True)
class Resolution:
    engine: str
    tier: str
    base: tuple[str, ...]
    cli_version: str | None
    requested_model: str
    model: str
    model_source: str
    requested_effort: str
    launch_effort: str
    timeout_s: float
    warnings: tuple[str, ...]
    changes: tuple[dict, ...]


def check_catalog(engine: str, settings: Settings, *, state_dir: pathlib.Path, env: Mapping[str, str],
                  refresh: bool = False, now: dt.datetime | None = None) -> tuple[list[str], models.Freshness]:
    """The engine's argv prefix and its catalog entry, after the freshness check that runs before every launch."""
    base = models.resolve_base(engine, settings.engines[engine]["command"])
    freshness = models.Catalog(state_dir).ensure_fresh(engine, base=base, env=env,
                                                       ttl_hours=settings.models["ttl_hours"], refresh=refresh, now=now)
    return base, freshness


def resolve_engine(engine: str, tier: str, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
                   env: Mapping[str, str], refresh: bool = False, now: dt.datetime | None = None,
                   checked: tuple[list[str], models.Freshness] | None = None) -> Resolution:
    """Model and effort for one engine and tier; the change log records anything that differs from last time."""
    section = settings.engines[engine]
    tier_settings = section["tiers"][tier]
    base, freshness = checked or check_catalog(engine, settings, state_dir=state_dir, env=env, refresh=refresh, now=now)
    catalog = models.Catalog(state_dir)
    entry = freshness.entry
    effective = codex.effective_model(root, codex.codex_home(env)) if engine == "codex" else None
    model, source = models.map_tier(engine, tier, pin=tier_settings["model"], quality_order=section["quality_order"],
                                    entry=entry, effective=effective)
    info = entry.model(model) if entry else None
    levels = info.efforts if info and info.efforts else None
    if engine == "codex":
        cache = {model: codex.CachedModel(model, levels, info.default)} if levels else None
        effort = codex.resolve_effort(model, tier_settings["effort"], cache=cache)
    else:
        effort = claude.resolve_effort(tier_settings["effort"], efforts=levels, model=model)
    why = f"{source} mapping for tier {tier} with {engine} CLI {freshness.launch_version or 'unknown'}"
    if freshness.refreshed:
        why += f"; catalog rebuilt ({freshness.refreshed})"
    changes = catalog.record_resolution(engine, tier, model=model, effort=effort.launch, source=source, why=why,
                                        now=now)
    return Resolution(engine=engine, tier=tier, base=tuple(base), cli_version=freshness.launch_version,
                      requested_model=tier_settings["model"], model=model, model_source=source,
                      requested_effort=effort.requested, launch_effort=effort.launch,
                      timeout_s=float(tier_settings["timeout_min"]) * 60, warnings=freshness.warnings,
                      changes=tuple(changes))


@dataclasses.dataclass(frozen=True)
class Packet:
    source: str  # repository-relative, with forward slashes
    intent: object | None  # has .section, .id, .revision, .digest
    section: str | None  # the intent section exactly as the source holds it
    scope: tuple[str, ...]
    plan: dict  # stage -> (engine, tier)
    rerank_disabled: bool = False  # the packet's Constraints turn the re-ranker off
    axes: dict[str, routing.Axes] = dataclasses.field(default_factory=dict)


def _section_lines(text: str, heading: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            inside = re.match(rf"## {re.escape(heading)}\b", line) is not None
            continue
        if inside:
            out.append(line)
    return out


def _read_text(path: pathlib.Path) -> str:
    text = path.read_bytes().decode("utf-8")  # bytes, so CRLF stays byte-intact
    return text[1:] if text.startswith("\ufeff") else text


def read_packet(root: pathlib.Path, path: pathlib.Path, *, parse_intent: Callable[[str], object] | None,
                read_source: Callable[[pathlib.Path], str] | None = None) -> Packet:
    """The parts of a packet a call needs. oah.intent (lane P4a) reads the source safely and parses it."""
    try:
        text = (read_source or _read_text)(path)
    except Exception as exc:
        code = getattr(exc, "code", "OAH_PACKET_UNREADABLE")
        message = str(exc).split(": ", 1)[1] if str(exc).startswith(f"{code}: ") else str(exc)
        raise EngineError(code, f"{message}; check the path given with --packet") from exc
    try:
        intent = parse_intent(text) if parse_intent is not None else None
    except Exception as exc:
        raise EngineError("OAH_PACKET_INTENT_INVALID", f"{path}: {exc}; fix the packet's OAH_INTENT section "
                          "(`oah packet check` lists the problems)") from exc
    scope = []
    for line in _section_lines(text, "Scope"):
        match = re.match(r"\s*[-*]\s+`?([^`\s]+)`?", line)
        if match:
            scope.append(match.group(1))
    plan: dict = {}
    axes: dict[str, routing.Axes] = {}
    for line in _section_lines(text, "Plan"):
        match = _PLAN_LINE.match(line)
        if match:
            plan[match.group(1)] = (match.group(2), match.group(3))
        recorded = _PLAN_AXES.match(line)
        if recorded:
            values = routing.Axes(*recorded.groups()[1:])
            routing.recommend(values)  # validate the recorded values before they reach selection
            axes[recorded.group(1)] = values
    try:
        source = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        source = str(path)
    section = briefs.raw_section(text) if intent is not None else None
    return Packet(source=source, intent=intent, section=section, scope=tuple(scope), plan=plan,
                  rerank_disabled=oah_packet.rerank_disabled(text), axes=axes)


BINDING_NAME = "intent-binding.json"
SOURCE_MISMATCH = "OAH_INTENT_SOURCE_MISMATCH"


def _binding_path(state_dir: pathlib.Path, task: str) -> pathlib.Path:
    runs.task_directory(state_dir, task)
    return runs._contained_path(state_dir, "runs", task, BINDING_NAME)


def dispatch_binding(state_dir: pathlib.Path, task: str) -> dict | None:
    """The intent source bound at the task's first dispatch, or None."""
    from oah.atomic import read_json

    data = read_json(_binding_path(state_dir, task), None)
    return data if isinstance(data, dict) else None


def check_dispatch_binding(bound: dict | None, packet: Packet, task: str) -> bool:
    """Refuse a swapped intent; True when this dispatch moves the binding (first dispatch or a new revision)."""
    intent = packet.intent
    if intent is None:
        return False
    if bound is None:
        return True
    fix = (f"To change the goal, raise Revision of Intent-ID {bound['id']} in {bound['source']} and accept it again; "
           "never point a task at a different intent source")
    line = f"INTENT {bound['id']}@r{bound['revision']} {bound['digest']}"
    if packet.source != bound["source"]:
        raise EngineError(SOURCE_MISMATCH, f"task {task} was first dispatched with intent source {bound['source']} "
                          f"({line}); this dispatch reads {packet.source}. {fix}. To continue, run the command again "
                          f"with --packet {briefs.shell_word(bound['source'])} or without --packet")
    if intent.id != bound["id"]:
        raise EngineError(SOURCE_MISMATCH, f"{packet.source} now holds Intent-ID {intent.id}, but task {task} is bound "
                          f"to {line}. {fix}")
    if intent.revision < bound["revision"] or (intent.revision == bound["revision"] and intent.digest != bound["digest"]):
        what = "an older revision" if intent.revision < bound["revision"] else "the same revision with edited content"
        raise EngineError(SOURCE_MISMATCH, f"{packet.source} holds {what} (INTENT {intent.id}@r{intent.revision} "
                          f"{intent.digest}) than the binding {line}. {fix}")
    return intent.revision > bound["revision"]


def record_dispatch_binding(state_dir: pathlib.Path, packet: Packet, task: str, run_id: str, *,
                            checked_lease=None, root: pathlib.Path | None = None) -> None:
    """Bind (or advance) the task's intent source under the state lock, re-checking against a racing dispatch."""
    from oah.atomic import write_json_atomic
    from oah.lease import LeaseError, Store

    checked_lease = checked_lease or bound_lease(state_dir, task)
    if checked_lease is None:
        raise EngineError("OAH_LEASE_CHANGED", "the checked lease was released; inspect oah status and "
                          "run oah call again")
    try:
        with Store(state_dir).checked(task=task, lease_id=checked_lease.id,
                                      generation=checked_lease.generation) as live:
            run = runs.Run(id=run_id, task=task, path=runs._contained_path(state_dir, "runs", task, run_id))
            _check_dispatch_lease(live, root or pathlib.Path(live.worktree), stage=run.read()["stage"])
            bound = dispatch_binding(state_dir, task)
            if check_dispatch_binding(bound, packet, task):
                intent = packet.intent
                history = list(bound.get("history", [])) if bound else []
                history.append({"revision": intent.revision, "digest": intent.digest, "run_id": run_id})
                write_json_atomic(_binding_path(state_dir, task),
                                  {"source": packet.source, "id": intent.id, "revision": intent.revision,
                                   "digest": intent.digest, "history": history})
            write_json_atomic(runs._contained_path(state_dir, "runs", task, run_id, "lease.json"),
                              {"id": live.id, "generation": live.generation, "intent": live.intent})
    except LeaseError as exc:
        raise EngineError(exc.code, str(exc).split(": ", 1)[1]) from None


def bound_lease(state_dir: pathlib.Path, task: str):
    """The task's active lease when it is bound to an intent (`oah start --packet`), else None."""
    from oah.lease import Store

    for lease, stale in Store(state_dir).snapshot():
        if lease.task == task and lease.intent is not None and not stale:
            return lease
    return None


def check_lease_binding(lease) -> None:
    """Refuse to dispatch for a bound task whose intent is not current (PLAN 5.6, A7)."""
    binding = lease.binding()
    state, reason = oah_intent.explain_binding(binding, pathlib.Path(lease.worktree))
    if state != "current":
        raise EngineError("OAH_INTENT_NOT_CURRENT",
                          f"task {lease.task} is bound to {binding.line()} and that intent is {state}: {reason}. "
                          f"Nothing was launched. Restore {binding.source} as it was bound, or raise its Revision, "
                          f"accept it and rebind with oah start --task {lease.task} --agent {lease.agent} --packet "
                          f"{binding.source}; then call again")


def _check_dispatch_lease(lease, root: pathlib.Path, *, stage: str) -> None:
    from oah.lease import utc_now

    if lease.expired(utc_now()):
        raise EngineError("OAH_LEASE_CHANGED", "the checked lease expired; start a new lease with "
                          "oah start --packet and run oah call again")
    if stage != "review":
        try:
            same_worktree = os.path.samefile(root, lease.worktree)
        except OSError:
            same_worktree = False
        if not same_worktree:
            raise EngineError("OAH_WORKTREE_LEASE_MISMATCH", "a mutating worker must run in the dispatcher's leased "
                              f"worktree ({lease.worktree}). Nothing was launched. Run oah call again with "
                              "--worktree set to that exact path")
    check_lease_binding(lease)
    # A read-only review may use another tree; its intent must still match the lease.
    check_lease_binding(dataclasses.replace(lease, worktree=str(root)))


@contextmanager
def dispatch_guard(prepared: Prepared):
    """Revalidate the prepared lease and source under the state lock, through a short launch only."""
    from oah.lease import LeaseError, Store

    run = prepared.run
    try:
        saved = run._read_json("lease.json")
        if not (isinstance(saved, dict) and isinstance(saved.get("id"), str) and saved["id"]
                and type(saved.get("generation")) is int and saved["generation"] >= 0):
            raise EngineError("OAH_RUN_LAUNCH_INVALID", "the run has no valid checked lease snapshot; "
                              "run oah call again to prepare it from the current lease")
        try:
            oah_intent.Binding.from_json(saved.get("intent"))
        except ValueError:
            raise EngineError("OAH_RUN_LAUNCH_INVALID", "the run has no valid checked intent binding; "
                              "run oah call again to prepare it from the current lease") from None
        with Store(run.path.parents[2]).checked(task=prepared.task, lease_id=saved["id"],
                                               generation=saved["generation"]) as live:
            _check_dispatch_lease(live, pathlib.Path(prepared.cwd), stage=prepared.stage)
            yield
    except (LeaseError, EngineError) as exc:
        error = EngineError(exc.code, str(exc).split(": ", 1)[1])
        run.result(status="refused", error=str(error))  # the state lock has been released
        raise error from None


def check_prepared(prepared: Prepared) -> None:
    """Check a queue/launcher handoff; execute checks again at the engine's actual spawn."""
    with dispatch_guard(prepared):
        pass


def _check_lease_source(binding, packet: Packet | None, task: str) -> None:
    if (packet is None or packet.intent is None or packet.source != binding.source
            or getattr(packet.intent, "digest", None) != binding.digest):
        source = packet.source if packet is not None else "no packet"
        raise EngineError(SOURCE_MISMATCH, f"task {task}'s lease is bound to {binding.line()} from {binding.source}, "
                          f"but this dispatch reads {source}. Run the command again without --packet, or "
                          f"with --packet {binding.source}; a changed goal is a new revision of the bound packet")


def _stored_brief_matches(stored: str, packet: Packet | None) -> None:
    if packet is None or packet.intent is None:
        return
    section = briefs.raw_section(stored)
    if section != (packet.section or packet.intent.section):
        found = "no intent section" if section is None else "a different intent section"
        raise EngineError(SOURCE_MISMATCH, f"the stored brief carries {found}, not INTENT {packet.intent.id}@r"
                          f"{packet.intent.revision} {packet.intent.digest} from {packet.source}; rebuild the brief "
                          "from the bound packet instead of reusing it. A changed goal needs a new revision of the "
                          "same Intent-ID, not a different source")


def resolve_worktree(selector: str, root: pathlib.Path, *, retry: Callable[[str], str]) -> pathlib.Path:
    """The one worktree of this repository named by a path, a branch or a worktree folder name."""
    from oah.paths import run_git

    listed = run_git(root, "worktree", "list", "--porcelain", "-z")
    if listed.returncode != 0:
        raise EngineError("OAH_WORKTREE_UNRESOLVED", f"git could not list the worktrees of {root}: "
                          f"{listed.stderr.strip()}")
    worktrees: list[tuple[pathlib.Path, str | None]] = []
    for record in listed.stdout.split("\0\0"):
        fields = [field for field in record.split("\0") if field]
        path = next((f[len("worktree "):] for f in fields if f.startswith("worktree ")), None)
        if path is None:
            continue
        branch = next((f[len("branch "):] for f in fields if f.startswith("branch ")), None)
        worktrees.append((pathlib.Path(path), branch))
    target = pathlib.Path(selector).expanduser()
    target = target if target.is_absolute() else pathlib.Path.cwd() / target
    matches: list[pathlib.Path] = []
    for path, branch in worktrees:
        by_path = target.exists() and path.exists() and os.path.samefile(path, target)
        by_branch = branch is not None and selector in (branch, branch.removeprefix("refs/heads/"))
        if (by_path or by_branch or selector == path.name) and path not in matches:
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    candidates = matches or [path for path, _ in worktrees]
    commands = "\n".join(f"  {path}\n    {retry(str(path))}" for path in candidates)
    if matches:
        raise EngineError("OAH_WORKTREE_UNRESOLVED", f"--worktree {selector!r} matches {len(matches)} worktrees; "
                          f"nothing was launched. Retry with the exact path:\n{commands}")
    raise EngineError("OAH_WORKTREE_UNRESOLVED", f"--worktree {selector!r} names no worktree of this repository; "
                      f"nothing was launched. Retry with one of:\n{commands}")


@dataclasses.dataclass(frozen=True)
class CallRequest:
    engine: str
    stage: str
    task: str = DEFAULT_TASK
    tier: str | None = None
    axes: Mapping[str, str | None] = dataclasses.field(default_factory=dict)
    override_stage: bool = False
    round_instructions: str = ""
    stored_brief: str | None = None
    packet: pathlib.Path | None = None
    timeout_s: float | None = None
    output_schema: str | None = None
    ephemeral: bool = False
    refresh_models: bool = False
    skills: tuple[tuple[str, str], ...] = ()
    no_rerank: bool = False  # the brief turns the re-ranker off (--no-rerank)
    isolated: bool = False  # Codex ignores the user's config.toml and execpolicy rules (oah review)


@dataclasses.dataclass(frozen=True)
class Prepared:
    run_dir: str
    run_id: str
    task: str
    engine: str
    stage: str
    argv: tuple[str, ...]
    cwd: str
    timeout_s: float
    env_additions: dict
    model: str
    launch_effort: str
    cli_version: str | None
    warnings: tuple[str, ...]

    @property
    def run(self) -> runs.Run:
        return runs.Run(id=self.run_id, task=self.task, path=pathlib.Path(self.run_dir))

    def to_json(self) -> dict:
        data = dataclasses.asdict(self)
        data["argv"] = list(self.argv)
        data["warnings"] = list(self.warnings)
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Prepared":
        fields = dict(data, argv=tuple(data["argv"]), warnings=tuple(data["warnings"]))
        fields.pop("require_detach", None)  # launch.json also stores this launcher-only option
        return cls(**fields)


def advise(axes: routing.Axes, task: str, *, root: pathlib.Path, brief_disabled: bool = False,
           env: Mapping[str, str] | None = None, keychain=None) -> str:
    """The re-ranker's advisory difficulty status (lane P3s); it never changes the tier."""
    try:
        rerank = importlib.import_module("oah.rerank")
    except ImportError:
        return "unused(not-configured)"
    try:
        advice = rerank.advise_difficulty(axes, task, root=root, brief_disabled=brief_disabled, env=env,
                                          keychain=keychain)
    except Exception as exc:  # a plugin failure must never block a launch
        return f"unused(error: {type(exc).__name__})"
    status = str(getattr(advice, "status", "unused(no status)"))
    tier = getattr(advice, "advisory_tier", None)
    return f"{status} tier={tier}" if status == "used" and tier else status


def _lease_scope(state_dir: pathlib.Path, task: str) -> tuple[str, ...]:
    from oah.lease import Store

    for lease, stale in Store(state_dir).snapshot():
        if lease.task == task and not stale:
            return tuple(lease.scope)
    return ()


def prepare(request: CallRequest, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
            env: Mapping[str, str], footer: str, has_footer: Callable[[str], bool],
            parse_intent: Callable[[str], object] | None = None,
            read_source: Callable[[pathlib.Path], str] | None = None, now: dt.datetime | None = None,
            keychain=None) -> Prepared:
    """Decide engine, tier, model and effort; write the run folder and brief. Raises before any launch.

    `keychain` is passed to the re-ranker's key lookup (`oah.secrets`); None means the OS keychain."""
    root = pathlib.Path(root)
    briefs.check_footer(footer, has_footer)
    warnings = list(routing.check_stage(request.engine, request.stage, settings.stages,
                                        override=request.override_stage))
    same = routing.same_engine_warning(settings.stages)
    if same:
        warnings.append(same)
    holder = bound_lease(state_dir, request.task)
    if holder is None:
        raise EngineError("OAH_INTENT_NOT_CURRENT", f"task {request.task} has no active lease with an intent binding. "
                          "Nothing was launched. Accept the packet, then bind it with "
                          f"oah start --task {request.task} --agent {request.engine} --packet "
                          f".oah/tasks/{request.task}/PACKET.md (use your packet's path); then call again")
    _check_dispatch_lease(holder, root, stage=request.stage)
    leased = holder.binding()
    bound = dispatch_binding(state_dir, request.task)
    default_source = bound["source"] if bound else leased.source
    packet_path = request.packet or root / default_source
    packet = read_packet(root, packet_path, parse_intent=parse_intent or oah_intent.parse,
                         read_source=read_source or oah_intent.read_source)
    _check_lease_source(leased, packet, request.task)
    check_dispatch_binding(bound, packet, request.task)
    stored = None
    if request.stored_brief is not None:
        stored = briefs.deliver(request.stored_brief, footer=footer, has_footer=has_footer)
        _stored_brief_matches(stored, packet)
    scope = packet.scope or tuple(holder.scope)
    disabled = request.no_rerank or packet.rerank_disabled
    recorded = packet.axes.get(request.stage)
    given = recorded.to_json() if recorded is not None else {}
    given.update({axis: value for axis, value in request.axes.items() if value is not None})
    scored = routing.score_axes(stage=request.stage, kind=given.pop("kind", None), scope=scope, overrides=given)
    axes = scored.axes if recorded is not None else None
    reasons = None
    advisory = "unused(tier-given)"
    if request.tier:
        tier, tier_source = request.tier, "flag"
    elif packet and request.stage in packet.plan:
        planned_engine, tier = packet.plan[request.stage]
        tier_source = "packet"
        if planned_engine != request.engine:
            warnings.append(f"OAH_PACKET_PLAN_ENGINE: the packet plans {planned_engine} for {request.stage}; "
                            f"its tier {tier} is used for {request.engine}")
    else:
        explicit = any(value is not None for value in request.axes.values())
        recommendation = routing.recommend(scored.axes)
        tier, axes = recommendation.tier, scored.axes
        tier_source = "flag" if explicit else "rules"
        reasons = list(scored.reasons) + list(recommendation.reasons)
        warnings.extend(scored.warnings)
        advisory = advise(axes, request.task, root=root, brief_disabled=disabled, env=env, keychain=keychain)
    skill_axes = scored.axes
    selection = skillsel.choose(root, kind=skill_axes.kind, stage=request.stage, risk=skill_axes.risk, scope=scope,
                                state_dir=state_dir, env=env, keychain=keychain,
                                brief_disabled=disabled, now=now)
    resolution = resolve_engine(request.engine, tier, settings, root=root, state_dir=state_dir, env=env,
                                refresh=request.refresh_models, now=now)
    warnings.extend(resolution.warnings)
    run = runs.new_run(state_dir, request.task, request.engine, request.stage, now=now)
    try:
        return _finish_prepare(request, settings, run, root=root, state_dir=state_dir, packet=packet, scope=scope,
                               stored=stored, footer=footer, has_footer=has_footer, resolution=resolution, tier=tier,
                               tier_source=tier_source, axes=axes, reasons=reasons, advisory=advisory,
                               warnings=warnings, selection=selection, checked_lease=holder)
    except (EngineError, briefs.BriefError) as exc:
        run.result(status="refused", error=str(exc), warnings=warnings)
        raise


def _finish_prepare(request: CallRequest, settings: Settings, run: runs.Run, *, root: pathlib.Path,
                    state_dir: pathlib.Path, packet: Packet | None, scope: Sequence[str], stored: str | None,
                    footer: str, has_footer: Callable[[str], bool], resolution: Resolution, tier: str,
                    tier_source: str, axes: routing.Axes | None, reasons: list | None, advisory: str,
                    warnings: list[str], selection: tuple | None = None, checked_lease=None) -> Prepared:
    if any(warning.startswith("OAH_STAGE_OVERRIDDEN") for warning in warnings):
        from oah.lease import Store

        Store(state_dir).log(task=request.task, agent=request.engine,
                             message=f"stage override: run {run.id} runs {request.engine} for stage {request.stage} "
                                     f"(configured: {settings.stages[request.stage]})")
    intent = packet.intent if packet else None
    section = (packet.section or getattr(intent, "section", None)) if packet else None
    instructions = request.round_instructions
    if stored is not None:
        section = section if section is not None else briefs.raw_section(stored)
        content = briefs.reusable_content(stored, stage=request.stage, footer=footer, has_footer=has_footer)
        warnings.extend(briefs.reusable_warnings(content))
        instructions = "\n\n".join(part for part in (content, instructions) if part)
    chosen, skills_rerank = selection if selection is not None else (None, None)
    text = briefs.build(run_id=run.id, task=request.task, stage=request.stage, engine=request.engine,
                        scope=list(checked_lease.scope) if checked_lease is not None else list(scope),
                        intent_section=section, binding=intent,
                        packet_source=packet.source if packet else None,
                        round_instructions=instructions, skills=request.skills, footer=footer, has_footer=has_footer,
                        skills_block=chosen.brief_text(rerank=skills_rerank) if chosen is not None else None,
                        skill_ids=[skill.id for skill in chosen.skills] if chosen is not None else ())
    read_only = request.stage == "review"
    if request.engine == "codex":
        argv = codex.build_command(resolution.base, workdir=str(root), read_only=read_only, model=resolution.model,
                                   effort=resolution.launch_effort,
                                   output_last_message=str(run.file_path("last-message.md")),
                                   output_schema=request.output_schema, ephemeral=request.ephemeral)
        if request.isolated:
            argv[-1:-1] = ISOLATED_CODEX_FLAGS  # before the stdin marker
    else:
        section = settings.engines["claude"]
        argv = claude.build_command(resolution.base, model=resolution.model, effort=resolution.launch_effort,
                                    permission_mode=section["permission_mode"], tools=section["tools"],
                                    read_only=read_only)
    run.write_prompt(text)
    if chosen is not None:
        from oah.atomic import write_json_atomic

        write_json_atomic(run.file_path("skills.json"), {"rerank": skills_rerank, **chosen.to_record()})
    additions = {"OAH_WORKER": "1", "OAH_RUN_ID": run.id, "OAH_TASK": request.task}
    timeout_s = float(request.timeout_s) if request.timeout_s else resolution.timeout_s
    run.result(status="prepared", command=list(argv), cwd=str(root), read_only=read_only,
               cli_path=resolution.base[0], cli_version=resolution.cli_version,
               requested_model=resolution.requested_model or None, resolved_model=resolution.model,
               model_source=resolution.model_source, requested_effort=resolution.requested_effort,
               launch_effort=resolution.launch_effort,
               axes=axes.to_json() if axes else None, tier=tier, tier_source=tier_source, tier_reasons=reasons,
               advisory=advisory, override_stage=request.override_stage, warnings=warnings)
    if packet is not None:
        record_dispatch_binding(state_dir, packet, request.task, run.id, checked_lease=checked_lease, root=root)
    return Prepared(run_dir=str(run.path), run_id=run.id, task=request.task, engine=request.engine,
                    stage=request.stage, argv=tuple(argv), cwd=str(root), timeout_s=timeout_s,
                    env_additions=additions, model=resolution.model, launch_effort=resolution.launch_effort,
                    cli_version=resolution.cli_version, warnings=tuple(warnings))


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _effort_fields(prepared: Prepared, stdout: str, stderr: str, rules: Sequence[Mapping]) -> dict:
    if prepared.engine == "codex":
        header = codex.check_header(stderr, prepared.launch_effort)
        inference = verify_inference("codex", prepared.cli_version, prepared.model, prepared.launch_effort, header,
                                     rules)
        code = inference.code or header.code
        return {"header_effort": header.header_effort, "header_check": header.status, "header_code": code,
                "resolved_inference_effort": inference.resolved_inference_effort,
                "normalization_basis": inference.normalization_basis, "effort_verified": inference.effort_verified,
                "effort_observable": header.status != "unverified"}
    observed = claude.observe(stdout)
    fields = {"observed_models": list(observed.models), "effort_observable": observed.effort_observable}
    if not observed.effort_observable:
        return dict(fields, header_effort=runs.UNVERIFIED, header_check=runs.UNVERIFIED, header_code=None,
                    resolved_inference_effort=runs.UNVERIFIED,
                    normalization_basis="unverified: Claude Code's result JSON does not report the inference effort",
                    effort_verified=False)
    status, code = compare_labels(observed.effort, prepared.launch_effort)
    check = HeaderCheck(observed.effort, status, code, None)
    return dict(fields, header_effort=check.header_effort, header_check=check.status, header_code=code,
                resolved_inference_effort=observed.effort,
                normalization_basis="observed: Claude Code's result JSON reports the effort",
                effort_verified=status == "match")


def execute(prepared: Prepared, *, base_env: Mapping[str, str] | None = None,
            rules: Sequence[Mapping] | None = None) -> dict:
    """Run a prepared call in the foreground and record what happened."""
    run = prepared.run
    env = dict(os.environ if base_env is None else base_env)
    env.update(prepared.env_additions)
    started = dt.datetime.now(dt.timezone.utc)
    run.result(status="running", started_at=_iso(started), pid=os.getpid())
    prompt = run.prompt_path.read_bytes().decode("utf-8")  # the brief goes to stdin byte for byte
    run.file_path("last-message.md")
    try:
        result = _run_prepared(prepared, env, prompt)
    except (OSError, ValueError) as exc:
        run.write_logs("", "")
        return run.result(status="failed", finished_at=_iso(dt.datetime.now(dt.timezone.utc)),
                          error=f"OAH_ENGINE_LAUNCH_FAILED: {exc}")
    finished = dt.datetime.now(dt.timezone.utc)
    run.write_logs(result.stdout, result.stderr)
    fields = _effort_fields(prepared, result.stdout, result.stderr, load_rules() if rules is None else rules)
    if result.timed_out:
        status, error = "timed_out", (f"OAH_RUN_TIMEOUT: the engine ran longer than {prepared.timeout_s:g} s and its "
                                      "process tree was stopped; raise the tier's timeout_min or split the task")
    elif result.returncode != 0:
        status, error = "failed", (f"OAH_ENGINE_FAILED: {prepared.engine} exited with {result.returncode}; read "
                                   f"{run.stderr_path}")
    elif fields.get("header_code"):
        status, error = "failed", (f"{fields['header_code']}: the engine reported effort {fields['header_effort']} "
                                   f"(normalized {fields['resolved_inference_effort']}) for launch effort "
                                   f"{prepared.launch_effort}; this run does not count")
    else:
        status, error = "finished", None
    return run.result(status=status, finished_at=_iso(finished), duration_s=round((finished - started).total_seconds(), 3),
                      exit_code=result.returncode, timed_out=result.timed_out, error=error, **fields)


def _run_prepared(prepared: Prepared, env: Mapping[str, str], prompt: str):
    """Use proc's job/tree handling, holding the lease lock only through process creation and resume."""
    from oah.engines.proc import (REAP_SECONDS, RunResult, UnsupportedBatchShim, _WindowsJob, decode_output,
                                  is_batch, kill_tree)

    if not prepared.argv:
        raise ValueError("empty command")
    if is_batch(prepared.argv[0]):
        raise UnsupportedBatchShim(prepared.argv[0], "batch files are never launched directly")
    job = _WindowsJob() if os.name == "nt" else None
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x4} if job is not None else
               {"start_new_session": True})
    try:
        with dispatch_guard(prepared):
            child = subprocess.Popen(list(prepared.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=prepared.cwd, env=dict(env), **options)
            if job is not None:
                try:
                    job.start(child)
                except BaseException:
                    child.kill()
                    child.communicate(timeout=REAP_SECONDS)
                    raise
        try:
            out, err = child.communicate(input=prompt.encode("utf-8"), timeout=prepared.timeout_s)
        except subprocess.TimeoutExpired:
            kill_tree(child)
            try:
                out, err = child.communicate(timeout=REAP_SECONDS)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
            return RunResult(None, decode_output(out), decode_output(err), True)
        return RunResult(child.returncode, decode_output(out), decode_output(err), False)
    finally:
        if job is not None:
            job.close()


_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); import oah.call as c; "
         "raise SystemExit(getattr(c, sys.argv[2])(sys.argv[3]))")
LAUNCH_TIMEOUT = 30.0


def _boot_argv(entry: str, run_dir: str) -> list[str]:
    package_root = str(pathlib.Path(__file__).resolve().parent.parent)
    return [sys.executable, "-c", _BOOT, package_root, entry, run_dir]


def start_background(prepared: Prepared, *, base_env: Mapping[str, str] | None = None,
                     require_detach: bool = False) -> str:
    """Start a detached runner for a prepared call and return its run id at once.

    A short-lived launcher starts the runner in its own session (POSIX) or as a detached process
    (Windows, breaking away from the caller's job when allowed) and exits, so the caller keeps no child.
    """
    run = prepared.run
    from oah.atomic import write_json_atomic

    check_prepared(prepared)
    write_json_atomic(run.launch_path, dict(prepared.to_json(), require_detach=require_detach))
    run.result(status="queued")
    env = dict(os.environ if base_env is None else base_env)
    launched = subprocess.run(_boot_argv("launch_main", prepared.run_dir), stdin=subprocess.DEVNULL,
                              capture_output=True, cwd=prepared.cwd, env=env, timeout=LAUNCH_TIMEOUT)
    text = launched.stdout.decode("utf-8", errors="replace").strip()
    detail = launched.stderr.decode("utf-8", errors="replace").strip()
    if launched.returncode != 0 or not text.isdigit():
        if detail.startswith(("OAH_LEASE_CHANGED: ", "OAH_INTENT_NOT_CURRENT: ", "OAH_RUN_LAUNCH_INVALID: ")):
            code, message = detail.split(": ", 1)
            raise EngineError(code, message)
        if detail.startswith("OAH_RUN_DETACH_FAILED:"):
            run.result(status="failed", error=detail)
            raise EngineError("OAH_RUN_DETACH_FAILED", detail.split(": ", 1)[1])
        run.result(status="failed", error=f"OAH_RUN_LAUNCH_FAILED: {detail or 'no runner pid'}")
        raise EngineError("OAH_RUN_LAUNCH_FAILED", f"the background runner did not start ({detail or 'no pid'}); "
                          "run the same command without --background")
    if detail:
        print(detail, file=sys.stderr)
    run.result(status="queued", pid=int(text))
    return prepared.run_id


def _windows() -> bool:
    return os.name == "nt"


def _background_run(run_dir: str) -> runs.Run:
    path = pathlib.Path(run_dir)
    return runs.Run(id=path.name, task=path.parent.name, path=path)


def launch_main(run_dir: str) -> int:
    try:
        return _launch_main(run_dir)
    except EngineError as exc:
        print(exc, file=sys.stderr)
        return 1


def _launch_main(run_dir: str) -> int:
    """Entry point of the launcher: start the runner detached, print its pid and exit."""
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                     "close_fds": True}
    from oah.atomic import read_json

    run = _background_run(run_dir)
    saved = read_json(run.launch_path, {})
    require_detach = saved.get("require_detach", False)
    prepared = Prepared.from_json(saved)
    argv = _boot_argv("background_main", run_dir)
    if _windows():
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            with dispatch_guard(prepared):
                child = subprocess.Popen(argv, creationflags=flags | 0x01000000, **options)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError as exc:
            if require_detach:
                print(f"OAH_RUN_DETACH_FAILED: Windows refused job breakaway ({exc}); retry without "
                      "--require-detach, or use a terminal whose job permits breakaway", file=sys.stderr)
                return 69
            warning = (f"OAH_RUN_DETACH_UNVERIFIED: Windows refused job breakaway ({exc}); closing this terminal "
                       "may stop the run. Keep it open and use oah wait, or retry with --require-detach "
                       "in a terminal whose job permits breakaway")
            run.result(detached=runs.UNVERIFIED, warnings=[*run.read().get("warnings", []), warning])
            print(warning, file=sys.stderr, flush=True)
            with dispatch_guard(prepared):
                child = subprocess.Popen(argv, creationflags=flags, **options)
        else:
            run.result(detached="verified")
    else:
        with dispatch_guard(prepared):
            child = subprocess.Popen(argv, start_new_session=True, **options)
        run.result(detached="verified")
    print(child.pid, flush=True)
    child.returncode = 0  # the runner outlives this launcher on purpose; nothing waits for it here
    return 0


def background_main(run_dir: str) -> int:
    """Entry point of the detached runner."""
    from oah.atomic import read_json

    prepared = Prepared.from_json(read_json(_background_run(run_dir).launch_path, None))
    try:
        execute(prepared)
    except EngineError as exc:
        print(exc, file=sys.stderr)
        return 1
    except BaseException as exc:
        prepared.run.result(status="failed", error=f"OAH_RUN_RUNNER_FAILED: {type(exc).__name__}: {exc}")
        raise
    return 0


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = (wintypes.HANDLE,), wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait(state_dir: pathlib.Path, run_id: str, *, timeout: float = WAIT_LIMIT_S, poll: float = 0.2,
         deadline: float | None = None) -> dict:
    """The final or current record after `timeout` (capped at 9 minutes).

    An optional entry deadline also bounds storage retries and lost-result publication, even when
    timeout is zero. Without it, storage keeps its existing retry budgets.
    """
    run = runs.find_run(state_dir, run_id, deadline=deadline)
    stop = time.monotonic() + min(max(timeout, 0.0), WAIT_LIMIT_S)
    if deadline is not None:
        stop = min(stop, deadline)
    data = None
    try:
        while True:
            data = run.read()
            if data.get("status") in runs.TERMINAL:
                return data
            pid = data.get("pid")
            if data.get("status") in ("queued", "running") and isinstance(pid, int) and not _alive(pid):
                data = run.read()
                if data.get("status") not in runs.TERMINAL:
                    return run.result(status="lost", error=f"OAH_RUN_LOST: the runner (pid {pid}) exited without "
                                      "recording a result; read the run's logs and call the engine again")
                return data
            if time.monotonic() >= stop:
                return data
            time.sleep(poll if deadline is None else min(poll, max(stop - time.monotonic(), 0.0)))
    except launchers.DeadlineExpired:
        if data is None:
            raise
        return data  # Leave lost-result publication for the next wait when the budget expires.
