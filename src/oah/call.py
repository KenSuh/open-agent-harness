# 엔진 호출 핵심 — 단계·난이도로 엔진·단·모델·effort 를 정하고, 브리프와 실행 폴더를 만든 뒤 proc 로 띄우거나 백그라운드로 넘기며, 실제로 돈 값을 기록한다.
"""`oah call` and `oah wait` without the command line (PLAN 5.0, 5.5, A1, A6, A10, A14).

`prepare` decides everything before launch and writes the run folder:
- the stage's engine (`OAH_STAGE_ENGINE_MISMATCH` unless overridden, which is logged as an event);
- the tier: `--tier` (source `flag`), else the packet's `## Plan` line for this stage (`packet`), else the
  rule-scored axes through `routing.recommend` (`rules`, or `flag` when axes were given). The re-ranker's
  advisory judgement is recorded next to it and never changes the tier;
- the model through the catalog (`models.map_tier`) and the launch effort through the adapter;
- the brief (`briefs.build`) and the argv; a review is always read-only.
`execute` launches through `oah.engines.proc.run_command` (prompt on stdin, the process tree killed on
timeout) and records the exit code, logs, header check and inference normalization. `start_background`
hands the same prepared run to a detached Python runner and returns at once; `wait` polls the record
for at most `WAIT_LIMIT_S` seconds and can be called again.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Callable, Mapping, Sequence

from oah import briefs, models, routing, runs
from oah.engines import claude, codex
from oah.engines.effort import EngineError, HeaderCheck, compare_labels, load_rules, verify_inference
from oah.engines.proc import run_command

WORKER_ENV = ("OAH_WORKER", "OAH_RUN_ID", "OAH_TASK")
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
    why = f"{source} mapping for tier {tier} with {engine} CLI {entry.cli_version if entry else 'unknown'}"
    if freshness.refreshed:
        why += f"; catalog rebuilt ({freshness.refreshed})"
    changes = catalog.record_resolution(engine, tier, model=model, effort=effort.launch, source=source, why=why,
                                        now=now)
    return Resolution(engine=engine, tier=tier, base=tuple(base), cli_version=freshness.entry.cli_version,
                      requested_model=tier_settings["model"], model=model, model_source=source,
                      requested_effort=effort.requested, launch_effort=effort.launch,
                      timeout_s=float(tier_settings["timeout_min"]) * 60, warnings=freshness.warnings,
                      changes=tuple(changes))


@dataclasses.dataclass(frozen=True)
class Packet:
    source: str  # repository-relative, with forward slashes
    intent: object | None  # has .section, .id, .revision, .digest
    scope: tuple[str, ...]
    plan: dict  # stage -> (engine, tier)


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


def read_packet(root: pathlib.Path, path: pathlib.Path, *, parse_intent: Callable[[str], object] | None) -> Packet:
    """The parts of a packet a call needs. The intent is parsed by oah.intent (lane P4a)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise EngineError("OAH_PACKET_UNREADABLE", f"{path} cannot be read as UTF-8 ({exc}); check the path given "
                          "with --packet") from exc
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
    for line in _section_lines(text, "Plan"):
        match = _PLAN_LINE.match(line)
        if match:
            plan[match.group(1)] = (match.group(2), match.group(3))
    try:
        source = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        source = str(path)
    return Packet(source=source, intent=intent, scope=tuple(scope), plan=plan)


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
        return cls(**dict(data, argv=tuple(data["argv"]), warnings=tuple(data["warnings"])))


def _advise(axes: routing.Axes, task: str) -> str:
    """The re-ranker's advisory difficulty status (lane P3s); it never changes the tier."""
    try:
        from oah import rerank  # type: ignore[attr-defined]
    except ImportError:
        return "unused(not-configured)"
    try:
        advice = rerank.advise_difficulty(axes, task)
    except Exception as exc:  # a plugin failure must never block a launch
        return f"unused(error: {type(exc).__name__})"
    status = str(getattr(advice, "status", "unused(no status)"))
    tier = getattr(advice, "tier", None)
    return f"{status} tier={tier}" if status == "used" and tier else status


def _lease_scope(state_dir: pathlib.Path, task: str) -> tuple[str, ...]:
    from oah.lease import Store

    for lease, stale in Store(state_dir).snapshot():
        if lease.task == task and not stale:
            return tuple(lease.scope)
    return ()


def prepare(request: CallRequest, settings: Settings, *, root: pathlib.Path, state_dir: pathlib.Path,
            env: Mapping[str, str], footer: str, has_footer: Callable[[str], bool],
            parse_intent: Callable[[str], object] | None = None, now: dt.datetime | None = None) -> Prepared:
    """Decide engine, tier, model and effort; write the run folder and brief. Raises before any launch."""
    root = pathlib.Path(root)
    briefs.check_footer(footer, has_footer)
    stored = (briefs.deliver(request.stored_brief, footer=footer, has_footer=has_footer)
              if request.stored_brief is not None else None)
    warnings = list(routing.check_stage(request.engine, request.stage, settings.stages,
                                        override=request.override_stage))
    same = routing.same_engine_warning(settings.stages)
    if same:
        warnings.append(same)
    packet_path = request.packet or root / ".oah" / "tasks" / request.task / "PACKET.md"
    packet = None
    if request.packet is not None or packet_path.is_file():
        packet = read_packet(root, packet_path, parse_intent=parse_intent)
    scope = packet.scope if packet and packet.scope else _lease_scope(state_dir, request.task)
    axes = reasons = None
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
        given = {axis: value for axis, value in request.axes.items() if value is not None}
        explicit = bool(given)
        scored = routing.score_axes(stage=request.stage, kind=given.pop("kind", None), scope=scope, overrides=given)
        recommendation = routing.recommend(scored.axes)
        tier, axes = recommendation.tier, scored.axes
        tier_source = "flag" if explicit else "rules"
        reasons = list(scored.reasons) + list(recommendation.reasons)
        warnings.extend(scored.warnings)
        advisory = _advise(axes, request.task)
    resolution = resolve_engine(request.engine, tier, settings, root=root, state_dir=state_dir, env=env,
                                refresh=request.refresh_models, now=now)
    warnings.extend(resolution.warnings)
    run = runs.new_run(state_dir, request.task, request.engine, request.stage, now=now)
    try:
        return _finish_prepare(request, settings, run, root=root, state_dir=state_dir, packet=packet, scope=scope,
                               stored=stored, footer=footer, has_footer=has_footer, resolution=resolution, tier=tier,
                               tier_source=tier_source, axes=axes, reasons=reasons, advisory=advisory,
                               warnings=warnings)
    except (EngineError, briefs.BriefError) as exc:
        run.result(status="refused", error=str(exc), warnings=warnings)
        raise


def _finish_prepare(request: CallRequest, settings: Settings, run: runs.Run, *, root: pathlib.Path,
                    state_dir: pathlib.Path, packet: Packet | None, scope: Sequence[str], stored: str | None,
                    footer: str, has_footer: Callable[[str], bool], resolution: Resolution, tier: str,
                    tier_source: str, axes: routing.Axes | None, reasons: list | None, advisory: str,
                    warnings: list[str]) -> Prepared:
    if any(warning.startswith("OAH_STAGE_OVERRIDDEN") for warning in warnings):
        from oah.lease import Store

        Store(state_dir).log(task=request.task, agent=request.engine,
                             message=f"stage override: run {run.id} runs {request.engine} for stage {request.stage} "
                                     f"(configured: {settings.stages[request.stage]})")
    intent = packet.intent if packet else None
    if stored is not None:
        text = stored
    else:
        text = briefs.build(run_id=run.id, task=request.task, stage=request.stage, engine=request.engine,
                            scope=list(scope), intent_section=getattr(intent, "section", None), binding=intent,
                            packet_source=packet.source if packet else None,
                            round_instructions=request.round_instructions, skills=request.skills, footer=footer,
                            has_footer=has_footer)
    run.write_prompt(text)
    read_only = request.stage == "review"
    if request.engine == "codex":
        argv = codex.build_command(resolution.base, workdir=str(root), read_only=read_only, model=resolution.model,
                                   effort=resolution.launch_effort,
                                   output_last_message=str(run.path / "last-message.md"),
                                   output_schema=request.output_schema, ephemeral=request.ephemeral)
    else:
        section = settings.engines["claude"]
        argv = claude.build_command(resolution.base, model=resolution.model, effort=resolution.launch_effort,
                                    permission_mode=section["permission_mode"], tools=section["tools"],
                                    read_only=read_only)
    additions = {"OAH_WORKER": "1", "OAH_RUN_ID": run.id, "OAH_TASK": request.task}
    timeout_s = float(request.timeout_s) if request.timeout_s else resolution.timeout_s
    run.result(status="prepared", command=list(argv), cwd=str(root), read_only=read_only,
               cli_path=resolution.base[0], cli_version=resolution.cli_version,
               requested_model=resolution.requested_model or None, resolved_model=resolution.model,
               model_source=resolution.model_source, requested_effort=resolution.requested_effort,
               launch_effort=resolution.launch_effort,
               axes=axes.to_json() if axes else None, tier=tier, tier_source=tier_source, tier_reasons=reasons,
               advisory=advisory, override_stage=request.override_stage, warnings=warnings)
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
    prompt = run.prompt_path.read_text(encoding="utf-8")
    try:
        result = run_command(list(prepared.argv), timeout=prepared.timeout_s, cwd=prepared.cwd, env=env,
                             stdin_text=prompt)
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


_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); import oah.call as c; "
         "raise SystemExit(getattr(c, sys.argv[2])(sys.argv[3]))")
LAUNCH_TIMEOUT = 30.0


def _boot_argv(entry: str, run_dir: str) -> list[str]:
    package_root = str(pathlib.Path(__file__).resolve().parent.parent)
    return [sys.executable, "-c", _BOOT, package_root, entry, run_dir]


def start_background(prepared: Prepared, *, base_env: Mapping[str, str] | None = None) -> str:
    """Start a detached runner for a prepared call and return its run id at once.

    A short-lived launcher starts the runner in its own session (POSIX) or as a detached process
    (Windows, breaking away from the caller's job when allowed) and exits, so the caller keeps no child.
    """
    run = prepared.run
    from oah.atomic import write_json_atomic

    write_json_atomic(run.launch_path, prepared.to_json())
    env = dict(os.environ if base_env is None else base_env)
    launched = subprocess.run(_boot_argv("launch_main", prepared.run_dir), stdin=subprocess.DEVNULL,
                              capture_output=True, cwd=prepared.cwd, env=env, timeout=LAUNCH_TIMEOUT)
    text = launched.stdout.decode("utf-8", errors="replace").strip()
    if launched.returncode != 0 or not text.isdigit():
        detail = launched.stderr.decode("utf-8", errors="replace").strip()
        run.result(status="failed", error=f"OAH_RUN_LAUNCH_FAILED: {detail or 'no runner pid'}")
        raise EngineError("OAH_RUN_LAUNCH_FAILED", f"the background runner did not start ({detail or 'no pid'}); "
                          "run the same command without --background")
    run.result(status="queued", pid=int(text))
    return prepared.run_id


def launch_main(run_dir: str) -> int:
    """Entry point of the launcher: start the runner detached, print its pid and exit."""
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                     "close_fds": True}
    argv = _boot_argv("background_main", run_dir)
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            child = subprocess.Popen(argv, creationflags=flags | 0x01000000, **options)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            child = subprocess.Popen(argv, creationflags=flags, **options)
    else:
        child = subprocess.Popen(argv, start_new_session=True, **options)
    print(child.pid, flush=True)
    child.returncode = 0  # the runner outlives this launcher on purpose; nothing waits for it here
    return 0


def background_main(run_dir: str) -> int:
    """Entry point of the detached runner."""
    from oah.atomic import read_json

    prepared = Prepared.from_json(read_json(pathlib.Path(run_dir) / "launch.json", None))
    try:
        execute(prepared)
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


def wait(state_dir: pathlib.Path, run_id: str, *, timeout: float = WAIT_LIMIT_S, poll: float = 0.2) -> dict:
    """The run's record once it ends, or the current record after at most `timeout` (capped at 9 minutes)."""
    run = runs.find_run(state_dir, run_id)
    deadline = time.monotonic() + min(max(timeout, 0.0), WAIT_LIMIT_S)
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
        if time.monotonic() >= deadline:
            return data
        time.sleep(poll)
