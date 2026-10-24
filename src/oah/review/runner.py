# 리뷰 한 라운드 실행 — 리스·의도·범위·잠금·영향 갱신·이력 관문을 거쳐 정화된 스냅샷에서 Codex 를 읽기 전용 작업자로 한 번 띄우고, 출력을 검증해 영수증을 쓰거나 세지 않은 시도로 남긴다.
"""One `oah review` invocation (PLAN 5.9): at most one paid reviewer call.

Order of refusals, all before any call: dispatched workers; a task without a bound lease or whose intent is
not current, or whose bound packet is not committed at HEAD (`OAH_INTENT_NOT_CURRENT`); a review stage other
than Codex; the range (`context.resolve`); a dirty tracked tree; a busy series; an impact map that cannot be
refreshed (`OAH_IMPACT_STALE`); the series gate (`rounds.gate`); a reviewer CLI that does not match
`[review] codex_sha256`.

Then the snapshot is built in a temporary folder, the canary is planted, and `oah.call` prepares and runs
the reviewer in the snapshot: stage `review` (read-only), `--output-schema`, `--ephemeral`, the user's Codex
config and rules ignored, and an allowlisted environment plus the worker variables. The snapshot is removed
as soon as the reviewer exits. A validated round publishes `R<n>.json` and its index line; every other call
is an uncounted attempt in `rounds.jsonl`.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import os
import pathlib
from typing import Callable, Mapping, Sequence

from oah import call, call_cli, models, preflight
from oah import intent as oah_intent
from oah.config import load_config
from oah.engines.effort import DOWNGRADED, MISMATCH
from oah.lease import Store
from oah.lockfile import LockTimeout, StateLock
from oah.paths import repo_info, run_git, state_dir
from oah.review import ReviewError, authors, canary, context, prompt, receipts, rounds, schema, snapshot
from oah.review import settings as review_settings

ENGINE = "codex"
INTERPRETERS = ("python", "pythonw", "py", "node", "nodejs")
KEEP_ENV = ("PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "CODEX_HOME", "TMPDIR", "TEMP", "TMP",
            "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
            "LANG", "LC_ALL", "LC_CTYPE", "TERM", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "all_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "NODE_EXTRA_CA_CERTS")
OUTCOME_CODES = {"converged": "OAH_REVIEW_CONVERGED", "changes-requested": "OAH_REVIEW_CHANGES_REQUESTED",
                 "needs-decision": rounds.NEEDS_DECISION}


@dataclasses.dataclass(frozen=True)
class Request:
    task: str
    base: str | None = None
    extra_round: bool = False
    decision: str | None = None
    tier: str | None = None
    timeout_s: float | None = None


@dataclasses.dataclass
class Result:
    code: str
    exit_code: int
    message: str
    lines: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    receipt_path: pathlib.Path | None = None
    receipt: dict | None = None
    run_dir: str | None = None

    def to_json(self) -> dict:
        return {"code": self.code, "exit_code": self.exit_code, "message": self.message, "lines": self.lines,
                "warnings": self.warnings, "receipt_path": str(self.receipt_path) if self.receipt_path else None,
                "receipt": self.receipt, "run_dir": self.run_dir}


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def reviewer_env(env: Mapping[str, str]) -> dict[str, str]:
    """The allowlisted environment a reviewer starts from; `oah.call` adds the worker variables."""
    if os.name == "nt":
        keep = {name.upper() for name in KEEP_ENV}
        return {key: value for key, value in env.items() if key.upper() in keep}
    return {key: value for key, value in env.items() if key in KEEP_ENV}


def reviewer_binary(base: Sequence[str]) -> tuple[str, str]:
    """(resolved path, sha256) of the reviewer CLI file: the script after an interpreter, else the program."""
    name = pathlib.Path(base[0]).name.casefold().removesuffix(".exe")
    entry = base[1] if len(base) > 1 and (name in INTERPRETERS or name.startswith("python3")) else base[0]
    path = os.path.realpath(entry)
    if not os.path.isfile(path):
        raise ReviewError("OAH_ENGINE_MISSING", f"the reviewer CLI {entry} is not a file; install Codex (oah doctor "
                          "prints the command) or fix [engines.codex] command in .oah/config.toml")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return path, digest.hexdigest()


def _lease(state: pathlib.Path, task: str):
    holder = next((lease for lease, stale in Store(state).snapshot() if lease.task == task and not stale), None)
    if holder is None or holder.binding() is None:
        why = "has no active lease" if holder is None else "has a lease without an intent binding (unbound)"
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"task {task} {why}, so there is no current intent to review "
                          f"against; nothing was launched. Accept the packet, then bind it with oah start --task {task} "
                          f"--agent <you> --packet .oah/tasks/{task}/PACKET.md (use your packet's path), and run "
                          "oah review again")
    return holder


def _check_intent(root: pathlib.Path, lease) -> oah_intent.Binding:
    binding = lease.binding()
    state, reason = oah_intent.explain_binding(binding, root)
    if state != "current":
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"task {lease.task} is bound to {binding.line()} and that intent "
                          f"is {state}: {reason}. Nothing was launched. Restore {binding.source} as it was bound, or "
                          f"raise its Revision, accept it and rebind with oah start --task {lease.task} --agent "
                          f"{lease.agent} --packet {binding.source}; then run oah review again")
    shown = run_git(root, "show", f"HEAD:{binding.source}")
    try:
        committed = oah_intent.parse(shown.stdout).digest if shown.returncode == 0 else None
    except oah_intent.IntentInvalid:
        committed = None
    if committed != binding.digest:
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"{binding.source} is not committed at HEAD as bound "
                          f"({binding.line()}); the reviewer reads the committed packet, so commit it, then run oah "
                          "review again")
    return binding


def _is_ancestor(root: pathlib.Path, commit: str, head: str) -> bool:
    return run_git(root, "merge-base", "--is-ancestor", commit, head).returncode == 0


def review(request: Request, *, cwd: pathlib.Path, env: Mapping[str, str], keychain=None,
           temp_root: pathlib.Path | None = None, rules: Sequence[Mapping] | None = None,
           ensure_impact: Callable | None = None) -> Result:
    """Run one round; pre-dispatch refusals raise ReviewError (or the shared config, git and state errors)."""
    if env.get("OAH_WORKER") == "1":
        raise ReviewError("OAH_REVIEW_WORKER_REFUSED", "dispatched workers do not start reviews; return your work "
                          "to the dispatcher, who runs oah review")
    if request.decision is not None and not request.decision.strip():
        raise ReviewError("OAH_REVIEW_DECISION_EMPTY", "--decision needs the decision in words; say what you decided")
    info = repo_info(pathlib.Path(cwd))
    state = state_dir(info, env)
    lease = _lease(state, request.task)
    root = pathlib.Path(lease.worktree)
    binding = _check_intent(root, lease)
    conf = review_settings.load(root)
    call_settings = call_cli.load_settings(root)
    if call_settings.stages.get("review") != ENGINE:
        raise ReviewError("OAH_REVIEW_ENGINE_UNSUPPORTED", f"[stages] review is {call_settings.stages.get('review')!r}; "
                          "oah review runs Codex (structured output, ephemeral, ignored user config). Set [stages] "
                          'review = "codex" in .oah/config.toml and install Codex')
    found = load_config(root).section("preflight", preflight.DEFAULTS)
    span = context.resolve(root, request.base, remote=str(found["remote"] or ""),
                           branch=str(found["default_branch"] or ""))
    dirty = context.tracked_dirty(root)
    if dirty:
        listed = ", ".join(dirty[:10]) + (f" and {len(dirty) - 10} more" if len(dirty) > 10 else "")
        raise ReviewError("OAH_REVIEW_DIRTY_TREE", f"tracked files have uncommitted changes ({listed}); a review "
                          "reads a commit, so commit or stash them, then run oah review again")
    lock = StateLock(receipts.lock_path(state, request.task, span.base), timeout=0)
    try:
        lock.__enter__()
    except LockTimeout:
        raise ReviewError("OAH_REVIEW_SERIES_BUSY", f"another oah review of task {request.task} against base "
                          f"{span.base[:12]} is running; wait for it to finish, then run oah review again") from None
    try:
        return _round(request, root=root, state=state, env=env, lease=lease, binding=binding, conf=conf,
                      call_settings=call_settings, span=span, keychain=keychain, temp_root=temp_root, rules=rules,
                      ensure_impact=ensure_impact)
    finally:
        lock.__exit__(None, None, None)


def _impact(root: pathlib.Path, env: Mapping[str, str], ensure_impact: Callable | None, head: str):
    from oah.impact import ensure_fresh, mapstore

    ensure = ensure_impact or ensure_fresh
    try:
        info = preflight.refresh_impact(root, ensure=lambda where: ensure(where, env=env))
    except preflight.ImpactRefused as exc:
        raise ReviewError(exc.code, str(exc).split(": ", 1)[1]) from None
    except preflight.ImpactUnavailable:
        raise ReviewError("OAH_IMPACT_STALE", "the impact map has no commit to describe; run oah impact build, "
                          "then oah review again") from None
    try:
        _info, data = mapstore.fresh_map(root, refresh=False, env=env)
    except Exception as exc:  # the map moved, or its storage failed: never review with a stale map
        raise ReviewError("OAH_IMPACT_STALE", f"the impact map changed while it was read ({exc}); run oah review "
                          "again") from None
    if info.commit != head:
        raise ReviewError("OAH_IMPACT_STALE", f"HEAD moved to {info.commit} while the impact map was refreshed; "
                          "run oah review again")
    return data


def _round(request: Request, *, root: pathlib.Path, state: pathlib.Path, env: Mapping[str, str], lease,
           binding: oah_intent.Binding, conf: review_settings.ReviewSettings, call_settings: call.Settings,
           span: context.Range, keychain, temp_root: pathlib.Path | None, rules: Sequence[Mapping] | None,
           ensure_impact: Callable | None) -> Result:
    from oah.impact.defaults import configured

    task = request.task
    data = _impact(root, env, ensure_impact, span.head)
    records = receipts.load(state, task)
    s = rounds.series(records, receipts.read_round_log(state, task), span.base, head=span.head,
                      is_ancestor=lambda commit, head: _is_ancestor(root, commit, head))
    if rounds.gate(s, head=span.head, decision=request.decision, extra_round=request.extra_round,
                   settings=conf) == rounds.CONVERGED:
        latest = s.latest
        return Result("OAH_REVIEW_CONVERGED", 0, f"OAH_REVIEW_CONVERGED: {latest.name} already approved head "
                      f"{span.head}; nothing was launched", receipt=latest.record,
                      receipt_path=receipts.review_dir(state, task) / latest.name)
    extra = bool(request.extra_round and conf.max_rounds is not None and len(s.receipts) >= conf.max_rounds)
    cli_path, cli_sha = reviewer_binary(models.resolve_base(ENGINE, call_settings.engines[ENGINE]["command"]))
    if conf.codex_sha256 and cli_sha != conf.codex_sha256:
        raise ReviewError("OAH_REVIEW_BINARY_MISMATCH", f"the reviewer CLI {cli_path} has sha256 {cli_sha}, but "
                          f"[review] codex_sha256 pins {conf.codex_sha256}; nothing was launched. If you updated "
                          "Codex on purpose, set codex_sha256 to the new value; otherwise reinstall Codex from its "
                          "official source")
    commits = authors.commit_engines(root, span.base, span.head)
    engines = [engine for _sha, engine in commits]
    changed = context.changed_files(root, span.base, span.head)
    db = context.db_impact(changed, data, span.head, configured(root))
    committed = run_git(root, "show", f"HEAD:{binding.source}").stdout
    conditions = oah_intent.parse(committed).conditions
    number, attempt = s.next_number, sum(1 for line in s.round_lines() if line.get("event") == "start") + 1
    tools = call_cli.intent_tools()
    warnings: list[str] = []
    started = dt.datetime.now(dt.timezone.utc)
    with snapshot.make(root, span.head, base=span.base, temp_root=temp_root, warnings=warnings) as snap:
        value = canary.plant(snap.tree)
        schema_path = snap.tree.parent / "output-schema.json"
        schema_path.write_text(schema.text(), encoding="utf-8")
        context_ = prompt.PromptContext(
            task=task, number=number, series_round=s.series_round, base=span.base, head=span.head,
            scope=tuple(lease.scope), conditions=conditions, diff_stat=context.diff_stat(root, span.base, span.head),
            critical=tuple(context.critical(changed, conf.critical_paths)), stripped=snap.stripped, db=db,
            history=tuple(prompt.HistoryItem(entry.kind, entry.name, entry.digest,
                                             receipts.canonical(entry.record).decode("utf-8")) for entry in s.history),
            history_digest=s.history_digest(),
            attempted=tuple((class_id, attempt_id, outcome) for class_id, tried in s.attempted().items()
                            for attempt_id, outcome in tried),
            decision=request.decision, extra_round=extra)
        prepared = call.prepare(
            call.CallRequest(engine=ENGINE, stage="review", task=task, tier=request.tier,
                             round_instructions=prompt.round_instructions(context_), output_schema=str(schema_path),
                             ephemeral=True, isolated=True, timeout_s=request.timeout_s),
            call_settings, root=snap.tree, state_dir=state, env=env, footer=tools.footer, has_footer=tools.has_footer,
            parse_intent=tools.parse, read_source=tools.read_source, keychain=keychain)
        warnings.extend(prepared.warnings)
        where = {"base": span.base, "series_round": s.series_round, "round": number, "attempt": attempt,
                 "run_id": prepared.run_id}
        receipts.log_round(state, task, {"event": "start", **where, "head": span.head, "extra_round": extra,
                                         "decision": request.decision})
        for note in ([f"review R{number}: the user's decision before this round: {request.decision}"]
                     if request.decision else []) + ([f"review R{number} runs beyond [review] max_rounds = "
                                                      f"{conf.max_rounds} (--extra-round)"] if extra else []):
            Store(state).log(task=task, agent=lease.agent, message=note)
        try:
            run = call.execute(prepared, base_env=reviewer_env(env), rules=rules)
        except BaseException as exc:
            receipts.log_round(state, task, {"event": "finish", **where, "counted": False,
                                             "code": getattr(exc, "code", type(exc).__name__),
                                             "message": str(exc)[:600]})
            raise
        last = prepared.run.file_path("last-message.md")
        raw = last.read_bytes() if last.is_file() else b""
    finished = dt.datetime.now(dt.timezone.utc)
    code = message = None
    output = None
    if run.get("status") != "finished":
        header_code = run.get("header_code")
        if header_code in (DOWNGRADED, MISMATCH):
            code, message = header_code, (f"{header_code}: the reviewer's header reported effort {run.get('header_effort')} "
                                          f"(normalized {run.get('resolved_inference_effort')}) for launch effort "
                                          f"{run.get('launch_effort')}, so this round does not count")
        else:
            code = "OAH_REVIEW_ENGINE_FAILED"
            message = f"{code}: the reviewer did not finish ({run.get('error')}), so this round does not count"
    else:
        try:
            output = schema.parse(raw, number=number)
            rounds.check(output, s, number=number, binding=binding, base=span.base, head=span.head,
                         canary_value=value, db_line=db.line)
        except ReviewError as exc:
            code, message = exc.code, str(exc)
    if code is not None:
        receipts.log_round(state, task, {"event": "finish", **where, "counted": False, "code": code,
                                         "message": message[:600]})
        left = conf.round_attempts - attempt
        budget = (f" This was attempt {attempt} of {conf.round_attempts} for round {s.series_round} of this series; "
                  + (f"run oah review --task {task} again to retry." if left > 0 else
                     "the budget is spent, so the next oah review refuses until round_attempts is raised."))
        return Result(code, 1, message + budget, lines=[f"run folder: {prepared.run_dir}"], warnings=warnings,
                      run_dir=prepared.run_dir)
    decision = rounds.decide(output, s)
    independent = authors.independence(engines, ENGINE)
    receipt = {
        "schema": receipts.SCHEMA, "kind": "receipt", "task": task, "round": number, "series_round": s.series_round,
        "base": span.base, "head": span.head, "base_label": span.base_label,
        "intent": binding.to_json(), "intent_digest": binding.digest,
        "reviewer": {"engine": ENGINE, "cli_path": cli_path, "cli_version": run.get("cli_version"),
                     "cli_sha256": cli_sha, "model": run.get("resolved_model"), "effort": run.get("launch_effort"),
                     "requested_effort": run.get("requested_effort"), "header_effort": run.get("header_effort"),
                     "header_check": run.get("header_check"),
                     "resolved_inference_effort": run.get("resolved_inference_effort"),
                     "normalization_basis": run.get("normalization_basis"),
                     "effort_verified": run.get("effort_verified") is True, "tier": run.get("tier"),
                     "tier_source": run.get("tier_source"), "run_id": prepared.run_id},
        "author_engines": sorted(set(engines)), "authorship": [{"commit": sha, "engine": engine} for sha, engine in commits],
        "independent": independent, "verdict": output.verdict, "summary": output.summary,
        "findings": [finding.to_json() for finding in output.findings],
        "classes": [item.to_json() for item in output.classes], "outcome": decision.outcome,
        "follow_ups": list(decision.follow_ups), "decision_stop_reason": decision.stop_reason,
        "decision": {"text": request.decision, "after": s.latest.name if s.latest else None} if request.decision else None,
        "extra_round": extra, "history": s.history_items(), "history_digest": s.history_digest(),
        "canary_verified": True, "canary_scope": canary.LIMIT, "db_impact": db.line, "db_query": db.query,
        "critical_paths": context.critical(changed, conf.critical_paths), "stripped": list(context_.stripped),
        "intent_return": output.intent_return, "evidence": output.evidence,
        "prompt_sha256": receipts.digest(prepared.run.prompt_path.read_bytes()), "output_sha256": receipts.digest(raw),
        "output_schema_sha256": receipts.digest(schema.text().encode("utf-8")),
        "started_at": _iso(started), "finished_at": _iso(finished)}
    name = f"R{number}.json"
    digest = receipts.publish(state, task, name, receipt, summary={
        "round": number, "series_round": s.series_round, "base": span.base, "head": span.head,
        "verdict": output.verdict, "outcome": decision.outcome})
    code = OUTCOME_CODES[decision.outcome]
    receipts.log_round(state, task, {"event": "finish", **where, "counted": True, "code": code, "receipt": name,
                                     "outcome": decision.outcome})
    return Result(code, 0 if decision.outcome == "converged" else 1, _headline(code, receipt, task),
                  lines=_lines(receipt, digest, name, conf, task), warnings=warnings,
                  receipt_path=receipts.review_dir(state, task) / name, receipt=receipt, run_dir=prepared.run_dir)


def _headline(code: str, receipt: dict, task: str) -> str:
    findings = receipt["findings"]
    counts = ", ".join(f"{severity}×{n}" for severity in ("P0", "P1", "P2", "P3")
                       if (n := sum(1 for item in findings if item["severity"] == severity)))
    summary = f"R{receipt['round']} of task {task}"
    if receipt["outcome"] == "converged":
        rest = (f"the series converged at head {receipt['head'][:12]}"
                + (f"; follow-ups: {', '.join(receipt['follow_ups'])}" if receipt["follow_ups"] else ""))
    elif receipt["outcome"] == "needs-decision":
        rest = (f"stopped for a decision: {receipt['decision_stop_reason']}. Decide how to continue, then run oah "
                f'review --task {task} --decision "<what you decided>"')
    else:
        rest = (f"requested changes ({counts}). Correct them (oah fix --task {task} runs Codex for that), commit, "
                f"then run oah review --task {task} again")
    return f"{code}: {summary} {rest}"


def _lines(receipt: dict, digest: str, name: str, conf: review_settings.ReviewSettings, task: str) -> list[str]:
    reviewer = receipt["reviewer"]
    verified = "effort verified" if reviewer["effort_verified"] else "effort unverified"
    independent = receipt["independent"]
    label = {True: "independent: yes — no commit in base..head was written by codex",
             False: "independent: no — codex wrote commits in base..head, so this is not an independent review "
                    "(an honest label for this engine order, not an error)"}.get(
        independent, "independent: unknown — some commits in base..head carry no Agent-Engine trailer")
    lines = [f"receipt: {name} {digest}",
             f"reviewer: codex {reviewer['cli_version']} sha256 {reviewer['cli_sha256'][:12]} · model "
             f"{reviewer['model']} · effort {reviewer['effort']} (header {reviewer['header_check']}, {verified})",
             label, f"canary: verified — {canary.LIMIT}", receipt["db_impact"]]
    lines += [f"{item['finding_id']} {item['severity']} {item['axis']} {item['location']} — {item['title']}"
              for item in receipt["findings"]]
    if (receipt["outcome"] == "changes-requested" and conf.max_rounds is not None
            and receipt["series_round"] >= conf.max_rounds):
        lines.append(f"OAH_REVIEW_ROUNDS_EXHAUSTED: this series reached [review] max_rounds = {conf.max_rounds}; the "
                     f"next round needs oah review --task {task} --extra-round")
    return lines
