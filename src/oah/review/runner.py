# 리뷰 한 라운드 실행 — 리스·의도·범위·잠금·영향 갱신·이력 관문을 거쳐 정화된 스냅샷에서 Codex 를 읽기 전용 작업자로 한 번 띄우고, 출력을 검증해 영수증을 쓰거나 세지 않은 시도로 남긴다.
"""One `oah review` invocation (PLAN 5.9): at most one paid reviewer call.

Order of refusals, all before any call: dispatched workers; a task without a bound lease or whose intent is
not current, or whose bound packet is not committed at HEAD (`OAH_INTENT_NOT_CURRENT`); the range
(`context.resolve`); a dirty tracked tree; a change that edits the base-choosing `[preflight]` settings without
an explicit `--base`; a review stage other than Codex; a busy series or task; dispatch-source lineage;
an impact map that cannot be refreshed (`OAH_IMPACT_STALE`); the series gate (`rounds.gate`);
a reviewer CLI that does not match
`[review] codex_sha256` (the resolved CLI entry file: for an npm install that is the JavaScript launcher, not the
native program it starts); a reviewer CLI whose `--version` first line differs from `[review] codex_version`
(`OAH_REVIEW_VERSION_MISMATCH`: one `--version` run in the reviewer environment, without a shell, for at most
10 seconds, and only when that setting is not empty).

Reviewer policy (`[stages]`, `[engines]`, `[models]` and `[review]`) comes from `.oah/config.toml` at the base
(`config_at`). The task's max_rounds budget is the strictest of the worktree config's limit and the limits in
the task log; no other branch's or base's config is read for it. A config change is
a critical path and a warning. `[impact]` stays the worktree's, because the persisted impact map binds it.

Then the snapshot is built in a temporary folder, the canary is planted, and `oah.call` prepares and runs
the reviewer in the snapshot: stage `review` (read-only), `--output-schema`, `--ephemeral`, the user's Codex
config and rules ignored, and an allowlisted environment plus the worker variables. API-key environment
variables never pass through (in any letter case); sign in through `codex login`, including
`codex login --with-api-key`. Structured output is redacted once, after JSON decoding and before retention
or validation. The snapshot is removed as soon as the reviewer exits. A validated round publishes
`R<n>.json` and its index line; every other call has no validated receipt in `rounds.jsonl`.
A round starts once `oah.call` has admitted the rule's model and effort, before the paid judge or
the reviewer. A refusal before that start logs nothing, so it spends no round and no attempt; from the start line on,
failed and interrupted calls retain that spend.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
from typing import Callable, Mapping, Sequence

from oah import call, call_cli, models, preflight, runs
from oah import intent as oah_intent
from oah.config import Config, load_config, parse_config
from oah.engines.effort import DOWNGRADED, MISMATCH
from oah.engines.proc import UnsupportedBatchShim, run_command
from oah.lease import Store
from oah.lockfile import LockTimeout, StateLock
from oah.paths import repo_info, run_git, state_dir
from oah.review import ReviewError, authors, canary, context, prompt, receipts, rounds, schema, snapshot
from oah.review import settings as review_settings

ENGINE = "codex"
CONFIG_PATH = ".oah/config.toml"
INTERPRETERS = ("python", "pythonw", "py", "node", "nodejs")
KEEP_ENV = ("PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "CODEX_HOME", "TMPDIR", "TEMP", "TMP",
            "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
            "LANG", "LC_ALL", "LC_CTYPE", "TERM", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "all_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "NODE_EXTRA_CA_CERTS")
OUTCOME_CODES = {"converged": "OAH_REVIEW_CONVERGED", "changes-requested": "OAH_REVIEW_CHANGES_REQUESTED",
                 "needs-decision": rounds.NEEDS_DECISION}
VERSION_MISMATCH_CODE = "OAH_REVIEW_VERSION_MISMATCH"
VERSION_TIMEOUT = 10.0
# The only `--version` lines a message repeats; anything else is reported as unreadable.
VERSION_LINE = re.compile(r"codex-cli \d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.-]+)?")


@dataclasses.dataclass(frozen=True)
class Request:
    task: str
    base: str | None = None
    extra_round: bool = False
    decision: str | None = None
    tier: str | None = None
    timeout_s: float | None = None
    extra_attempt: bool = False


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
        kept = {key: value for key, value in env.items() if key.upper() in keep}
    else:
        kept = {key: value for key, value in env.items() if key in KEEP_ENV}
    # The other entries are OS/CLI/certificate paths and locale/terminal settings, not auth values.
    # Refuse '@' conservatively, including scheme-less proxies and entries in NO_PROXY lists.
    if any("@" in value for key, value in kept.items()
           if key.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")):
        raise ReviewError("OAH_REVIEW_PROXY_CREDENTIALS", "proxy userinfo is not allowed in the reviewer "
                          "environment; use a credential-free proxy or Codex's own configuration, then retry "
                          "oah review")
    return kept


def credential_redactor(env: Mapping[str, str]) -> Callable[[str], str]:
    """Keep runtime credentials and their JSON/repr encodings out of review artifacts."""
    values = {value for name, value in env.items() if value and name.upper().endswith(
        ("_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))}
    encoded = set(values)
    for value in values:
        encoded.update(json.dumps(value, ensure_ascii=ascii_only)[1:-1] for ascii_only in (False, True))
        encoded.add(repr(value)[1:-1])
    needles = sorted(encoded, key=len, reverse=True)

    def redact(text: str) -> str:
        for value in needles:
            text = text.replace(value, "[REDACTED]")
        return text

    return redact


def _safe_output(raw: bytes, redact: Callable[[str], str]) -> bytes:
    """The structured-output boundary: decode, redact every string, then allow retention and validation."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise schema.OutputInvalid(["the final message is not UTF-8 text"]) from None
    except (ValueError, RecursionError):
        # Never quote malformed output: it has not crossed the decoded-string boundary.
        raise schema.OutputInvalid(["the final message is not one JSON object"]) from None

    def clean(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {redact(key): clean(item) for key, item in value.items()}
        return value

    try:
        return (json.dumps(clean(data), ensure_ascii=True) + "\n").encode("utf-8")
    except RecursionError:
        raise schema.OutputInvalid(["the final message is nested too deeply"]) from None


def _reviewer_entry(base: Sequence[str]) -> int:
    name = pathlib.Path(base[0]).name.casefold().removesuffix(".exe")
    return 1 if len(base) > 1 and (name in INTERPRETERS or name.startswith("python3")) else 0


def reviewer_command(command: Sequence[str], search_path: str | None = None) -> list[str]:
    """The argv prefix `oah review` runs for `[engines.codex] command`: found on PATH, with its program and its
    entry file (the script after an interpreter) made absolute."""
    base = models.resolve_base(ENGINE, command, search_path)
    for index in {0, _reviewer_entry(base)}:
        base[index] = os.path.abspath(base[index])
    return base


def reviewer_binary(base: Sequence[str]) -> tuple[str, str]:
    """(resolved path, sha256) of the reviewer CLI file: the script after an interpreter, else the program."""
    entry = base[_reviewer_entry(base)]
    path = os.path.realpath(entry)
    if not os.path.isfile(path):
        raise ReviewError("OAH_ENGINE_MISSING", f"the reviewer CLI {entry} is not a file; install Codex (oah doctor "
                          "prints the command) or fix [engines.codex] command in .oah/config.toml")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return path, digest.hexdigest()


def _version_line(base: Sequence[str], env: Mapping[str, str]) -> str | None:
    """The first non-empty line of `<reviewer> --version`, stripped; None when it cannot be read."""
    try:
        result = run_command([*base, "--version"], timeout=VERSION_TIMEOUT, env=env)
    except (OSError, UnsupportedBatchShim):
        return None
    if result.timed_out or result.returncode != 0:
        return None
    return next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)


def check_version(base: Sequence[str], pinned: str, env: Mapping[str, str]) -> None:
    """Refuse unless the reviewer CLI's `--version` first line equals `[review] codex_version`."""
    observed = _version_line(base, env)
    if observed == pinned:
        return
    pin = f"pins {pinned}" if VERSION_LINE.fullmatch(pinned) else "pins another value"
    if observed is not None and VERSION_LINE.fullmatch(observed):
        found, update = f"reports {observed}", f'set codex_version = "{observed}"'
    else:
        found = "gave --version output that could not be read"
        update = "set codex_version to the first line of codex --version"
    raise ReviewError(VERSION_MISMATCH_CODE, f"the reviewer CLI {found}, but [review] codex_version {pin}; nothing "
                      f"was launched. If you updated Codex on purpose, {update}; otherwise reinstall Codex from its "
                      "official source")


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
    committed_conditions(root, "HEAD", binding)
    return binding


def _check_design(root: pathlib.Path, state: pathlib.Path, lease, binding: oah_intent.Binding):
    """A review is a read-only inspection: a current design, or one with an unresolved newer draft (shown and never
    counted as converged). The packet committed at HEAD must still name a revision the task follows."""
    from oah.design import binding as design
    from oah.design.model import DesignError
    from oah.lease_cli import lease_design

    judged = lease_design(lease, state, source=binding.source, root=root)
    try:
        judged.require("inspect", task=lease.task, agent=lease.agent)
        if judged.task is not None:
            committed = design.committed_text(root, binding.source)
            try:
                section = design.parse_section(committed) if committed is not None else None
            except design.SectionInvalid:
                section = None
            if section is None or not judged.task.same_design(section) or not judged.task.holds(section.binding()):
                raise DesignError(design.SOURCE_MISMATCH_CODE, f"{binding.source} committed at HEAD does not name a "
                                  f"revision task {lease.task} follows ({judged.task.binding.line()}); the reviewer "
                                  "reads the committed packet, so commit its Design section as bound, then run oah "
                                  "review again")
    except DesignError as exc:
        text = str(exc)
        raise ReviewError(exc.code, text.split(": ", 1)[1] if text.startswith(exc.code + ": ") else text) from None
    return judged


class _SnapshotDisposal:
    def __init__(self, state: pathlib.Path, run_id: str, disposition: list):
        self.state, self.run_id, self.disposition = state, run_id, disposition
        self.plan: snapshot.RetirementPlan | None = None

    def prepare(self, parent: pathlib.Path, design, *, metadata: dict | None = None) -> None:
        allocation = {"version": 1, "run": self.run_id, "task_identity": design.task.identity.to_json(),
                      "task_basis_digest": design.task.basis, "container_object": snapshot._container_object(parent),
                      "review": dict(metadata or {})}
        if os.path.lexists(parent / "last-message.md"):
            raise snapshot.PlanUnavailable("the designated output slot was not absent before launch")
        self.plan = snapshot.capture_retirement_plan(allocation, parent, self.state,
                    output_slot={"path": ["last-message.md"], "producer": {"run": self.run_id, "engine": ENGINE}})

    def adopt(self, record: dict) -> dict:
        if (self.plan is None or record.get("run") != self.run_id or
                record.get("task_identity") != self.plan.descriptor["allocation"]["task_identity"] or
                record.get("task_basis_digest") != self.plan.descriptor["allocation"]["task_basis_digest"]):
            raise snapshot.PlanUnavailable("the origin does not match the prelaunch allocation")
        return dict(record, retirement={"descriptor": self.plan.descriptor, "sha256": self.plan.descriptor_sha256,
                    "pages_relative": list(self.plan.pages_dir.relative_to(self.state).parts)})

    def capture_final(self, parent: pathlib.Path, source: bytes, copied: bytes) -> None:
        from oah.design import task_context
        from oah.lease import persist_create_only

        absent, _reason = task_context.transport_absence(self.state, run=self.run_id)
        if absent:
            return
        views = list(task_context.transport_views(self.state, run=self.run_id).values())
        if self.plan is None or len(views) != 1 or views[0].problem is not None:
            raise snapshot.Retained("the review has no single verified prelaunch origin allocation")
        view = views[0]
        terminal = view.last(*task_context.QUIESCENT)
        cleaned = view.last("verified-cleaned")
        if terminal is None or cleaned is None or not view.quiescent or view.exposure is not None:
            raise snapshot.Retained("the original producer and DATA cleanup have no durable clean checkpoint")
        slot = snapshot.capture_output_slot(parent, ["last-message.md"],
                    producer={"run": self.run_id, "checkpoint": terminal["record_digest"], "origin": view.origin})
        if (slot["presence"] == "present" and (slot["sha256"] != "sha256:" + hashlib.sha256(source).hexdigest()
                or slot["size"] != len(source))) or (slot["presence"] == "absent" and source):
            raise snapshot.Retained("the copied review output is not the final owned output bytes")
        final = snapshot.finalize_retirement_plan(self.plan, slot)
        snapshot.validate_retirement_plan(final, parent, self.plan.pages_dir)
        raw = snapshot._canonical(final)
        digest = hashlib.sha256(raw).hexdigest()
        target = self.plan.pages_dir.parent / (digest + ".json")
        persist_create_only(target, raw)
        writer = Store(self.state)
        authority = writer.transport_action_basis(task=view.task, origin=view.origin, expected=view.latest,
                                                   receiving_root=parent / "tree")
        checked = task_context.verify_transport_origin(view.record, parent / "tree", expect="cleaned")
        if checked["state"] != "clean":
            raise snapshot.Retained(checked["why"])
        writer.observe_transport(task=view.task, origin=view.origin, expected=view.latest,
                    kind="retirement-checkpoint", authority=authority,
                    fact={"base_plan_sha256": self.plan.descriptor_sha256, "final_sha256": digest,
                          "final_bytes": len(raw), "final_relative": list(target.relative_to(self.state).parts),
                          "data_checkpoint": {"sequence": cleaned["sequence"], "record_digest": cleaned["record_digest"]},
                          "head": checked["head"], "output_preserved": True,
                          "copied_sha256": hashlib.sha256(copied).hexdigest(), "copied_bytes": len(copied)})

    def __call__(self, parent: pathlib.Path, created: dict) -> tuple[str, str | None]:
        from oah.design import task_context

        try:
            absent, reason = task_context.transport_absence(self.state, run=self.run_id)
            if absent:
                if snapshot._container_object(parent) != created:
                    outcome = ("retained", "the snapshot folder is not the one this review created")
                elif snapshot.remove(parent):
                    outcome = ("legacy-disposed", None)
                else:
                    outcome = ("retained", "the snapshot folder could not be removed")
            else:
                views = list(task_context.transport_views(self.state, run=self.run_id).values())
                if len(views) != 1 or views[0].problem is not None:
                    outcome = ("retained", reason)
                else:
                    outcome = retire_snapshot(self.state, views[0], parent)
        except (OSError, ValueError, snapshot.Retained, snapshot.PlanUnavailable) as exc:
            outcome = ("retained", f"the checked retirement stopped ({type(exc).__name__})")
        self.disposition.append(outcome)
        return outcome


def _snapshot_disposal(state: pathlib.Path, run_id: str, disposition: list):
    return _SnapshotDisposal(state, run_id, disposition)


@contextlib.contextmanager
def _snapshot_failure(record_failure):
    try:
        yield
    except BaseException as exc:
        problem = exc
        if isinstance(exc, (snapshot.PlanUnavailable, snapshot.Retained)):
            code = ("OAH_REVIEW_SNAPSHOT_RETAINED" if isinstance(exc, snapshot.Retained)
                    else "OAH_REVIEW_SNAPSHOT_FAILED")
            problem = ReviewError(code, str(exc))
        try:
            record_failure(problem)
        except Exception as logging_error:
            problem.add_note(f"the failed attempt could not be recorded ({type(logging_error).__name__})")
        if problem is exc:
            raise
        raise problem from exc


def _retirement_path(state: pathlib.Path, components, *, file: bool) -> pathlib.Path:
    if (not isinstance(components, list) or not components or components[0] != snapshot.RETIREMENT_DIR or
            len(components) > 8 or any(not isinstance(p, str) or p in ("", ".", "..") or "/" in p or "\\" in p
                                      for p in components)):
        raise snapshot.Retained("the retirement evidence path is outside its allocation")
    path = state
    for part in components:
        path = path / part
        if path.is_symlink():
            raise snapshot.Retained("a retirement evidence path is linked")
    if file and not path.is_file():
        raise snapshot.Retained("the final retirement evidence is missing")
    return path


def retire_snapshot(state: pathlib.Path, view, parent: pathlib.Path) -> tuple[str, str | None]:
    from oah.design import task_context as tc
    from oah.lease import LeaseError

    registration = view.record.get("retirement") or {}
    checkpoint = view.last("retirement-checkpoint")
    if checkpoint is None or checkpoint["fact"].get("output_preserved") is not True:
        return "retained", "there is no durable final output and retirement checkpoint"
    fact = checkpoint["fact"]
    descriptor = registration.get("descriptor")
    if (not isinstance(descriptor, dict) or hashlib.sha256(snapshot._canonical(descriptor)).hexdigest() !=
            registration.get("sha256") or fact.get("base_plan_sha256") != registration["sha256"] or
            descriptor.get("allocation", {}).get("run") != view.record.get("run") or
            descriptor["allocation"].get("task_identity") != view.record.get("task_identity") or
            descriptor["allocation"].get("task_basis_digest") != view.record.get("task_basis_digest")):
        return "retained", "the immutable origin does not own the exact complete plan"
    pages = _retirement_path(state, registration.get("pages_relative"), file=False)
    target = _retirement_path(state, fact.get("final_relative"), file=True)
    raw = snapshot.read_evidence(target, 256 * 1024)
    if (len(raw) != fact.get("final_bytes") or hashlib.sha256(raw).hexdigest() != fact.get("final_sha256")
            or len(raw) > 256 * 1024):
        return "retained", "the final plan bytes changed or exceed their bound"
    final = tc.strict_json(raw.decode("utf-8"))
    if final.get("base_plan") != descriptor or final.get("base_plan_sha256") != registration["sha256"]:
        return "retained", "the final plan belongs to another allocation"
    slot = final.get("output_slot_final") or {}
    if slot.get("presence") == "present":
        producer = slot.get("producer") or {}
        if (producer.get("run") != view.record["run"] or producer.get("origin") != view.origin or
                not any(item["kind"] in tc.QUIESCENT and item["record_digest"] == producer.get("checkpoint")
                        and item["fact"].get("quiescent") is True for item in view.observations)):
            return "retained", "the final output is not bound to its original durable producer"
    data_e = fact.get("data_checkpoint")
    if not any(item["kind"] == "verified-cleaned" and data_e ==
               {"sequence": item["sequence"], "record_digest": item["record_digest"]} for item in view.observations):
        return "retained", "the checkpoint lacks the original verified DATA cleanup"
    pending, pending_proof, recorded, finalized, interrupted = None, None, [], False, False
    predecessor = {"sequence": checkpoint["sequence"], "record_digest": checkpoint["record_digest"]}
    for event in view.observations[checkpoint["sequence"]:]:
        event_predecessor = predecessor
        predecessor = {"sequence": event["sequence"], "record_digest": event["record_digest"]}
        if event["kind"] == "retired":
            if (pending is not None or finalized or event != view.observations[-1]
                    or event["fact"].get("plan_sha256") != registration["sha256"]
                    or event["fact"].get("final_sha256") != fact["final_sha256"]
                    or event["fact"].get("removed_entries") != len(recorded)
                    or (interrupted and event["fact"].get("counted") is not False)):
                return "retained", "the durable retirement completion is incompatible with its exact progress"
            finalized = True
            continue
        if event["kind"] not in ("retirement-intent", "retirement-progress", "retirement-cancel", "retirement-absence-ack"):
            return "retained", "the retirement checkpoint has an incompatible successor"
        progress_fact = event["fact"]
        if (progress_fact.get("plan_sha256") != registration["sha256"] or
                progress_fact.get("final_sha256") != fact["final_sha256"] or
                type(progress_fact.get("cursor")) is not int or progress_fact["cursor"] != len(recorded)):
            return "retained", "the retirement cursor or allocation changed"
        if event["kind"] == "retirement-intent":
            if pending is not None or progress_fact.get("observed") != "intent":
                return "retained", "the retirement has a conflicting pending delete"
            pending = progress_fact
            pending_proof = {"origin": view.origin, "sequence": event["sequence"], "predecessor": event_predecessor,
                             "record_digest": event["record_digest"], "fact": progress_fact}
        elif event["kind"] == "retirement-cancel":
            if (pending is None or progress_fact.get("observed") != "present-unchanged"
                    or any(progress_fact.get(key) != pending.get(key) for key in ("path", "entry_sha256"))):
                return "retained", "the pending delete was not cancelled against the exact unchanged entry"
            pending = None
            pending_proof = None
        else:
            acknowledgment = event["kind"] == "retirement-absence-ack"
            if (pending is None or pending_proof is None or progress_fact.get("observed") != ("absence-after-intent" if acknowledgment else "absent") or
                    any(progress_fact.get(key) != pending.get(key) for key in ("path", "entry_sha256"))):
                return "retained", "a deletion has no exact preceding durable intent"
            if acknowledgment and (progress_fact.get("causality") != "unknown" or progress_fact.get("counted") is not False
                    or progress_fact.get("intent_record") != {"sequence": pending_proof["sequence"],
                                                             "record_digest": pending_proof["record_digest"]}
                    or progress_fact.get("predecessor_record") != pending_proof["predecessor"]):
                return "retained", "the interrupted absence is not bound to its exact intent and predecessor"
            interrupted = interrupted or acknowledgment
            recorded.append(progress_fact)
            pending = None
            pending_proof = None
    removed = frozenset(tuple(item["path"]) for item in recorded)
    if len(removed) != len(recorded):
        return "retained", "the retirement cursor repeats a path"
    root_removed = () in removed
    planned = snapshot.validate_retirement_plan(final, parent, pages, removed=removed - {()},
                                               container_removed=root_removed, pending=pending_proof)
    entries_recorded = recorded[:-1] if root_removed else recorded
    for item, progress_fact in zip(planned.all_entries, entries_recorded):
        if (item["path"] != progress_fact["path"] or
                hashlib.sha256(snapshot._canonical(item)).hexdigest() != progress_fact["entry_sha256"]):
            return "retained", "recorded progress is not the exact prefix of the immutable complete plan"
    if len(entries_recorded) > len(planned.all_entries):
        return "retained", "the recorded retirement exceeds its complete plan"
    if root_removed and (len(entries_recorded) != len(planned.all_entries) or recorded[-1]["path"] != [] or
            recorded[-1]["entry_sha256"] != hashlib.sha256(snapshot._canonical({"path": [], "type": "root"})).hexdigest()):
        return "retained", "the container removal is not the last exact complete progress entry"
    if finalized:
        return (("disposed-noncounting" if interrupted else "disposed", None) if root_removed else
                ("retained", "the durable retirement has no root progress"))
    writer = Store(state)
    expected = view.latest
    removed_paths = set(removed)

    def retired_roles(additional=()):
        roles = []
        for name, path in (("receiving-git-common", ("tree", ".git")), ("receiving-root", ("tree",)),
                           ("snapshot-parent", ())):
            if path in removed_paths or path in additional:
                roles.append(name)
        return roles

    pending_removed = {tuple(pending["path"])} if pending is not None and planned.pending_absent else set()
    authority = writer.transport_action_basis(task=view.task, origin=view.origin, expected=expected,
                    receiving_root=parent / "tree", retired_roles=retired_roles(pending_removed))
    if pending is not None:
        if pending_proof is None:
            return "retained", "the pending entry has no exact durable intent proof"
        with writer.transport_action(task=view.task, origin=view.origin, expected=expected, authority=authority,
                    receiving_root=parent / "tree", retired_roles=retired_roles(pending_removed)):
            fresh = snapshot.validate_retirement_plan(final, parent, pages, removed=removed - {()}, pending=pending_proof)
            if fresh.pending_absent != planned.pending_absent:
                return "retained", "the pending entry changed during its full-plan recovery check"
            planned = fresh
            if planned.pending_absent:
                expected = writer.observe_transport(task=view.task, origin=view.origin, expected=expected,
                    kind="retirement-absence-ack", authority=authority, held=True,
                    fact={**pending, "observed": "absence-after-intent", "causality": "unknown", "counted": False,
                          "intent_record": {"sequence": pending_proof["sequence"], "record_digest": pending_proof["record_digest"]},
                          "predecessor_record": pending_proof["predecessor"]})
                removed_paths.add(tuple(pending["path"]))
                removed = frozenset(removed_paths)
                root_removed = () in removed_paths
                interrupted = True
            else:
                expected = writer.observe_transport(task=view.task, origin=view.origin, expected=expected,
                        kind="retirement-cancel", authority=authority, held=True,
                        fact={**pending, "observed": "present-unchanged"})
    if not removed:
        checked = tc.verify_transport_origin(view.record, parent / "tree", expect="cleaned")
        if checked["state"] != "clean" or checked["head"] != fact.get("head"):
            return "retained", "the pre-delete Git and DATA checkpoint cannot be verified"
    cursor = len(removed)

    def guard(item):
        return writer.transport_action(task=view.task, origin=view.origin, expected=expected, authority=authority,
                    receiving_root=parent / "tree", retired_roles=retired_roles())

    def progress(item, observed):
        nonlocal expected, cursor
        kind = "retirement-intent" if observed == "intent" else "retirement-progress"
        expected = writer.observe_transport(task=view.task, origin=view.origin, expected=expected, kind=kind,
                    authority=authority, held=True,
                    fact={"plan_sha256": registration["sha256"], "final_sha256": fact["final_sha256"],
                          "cursor": cursor, "path": item["path"], "observed": observed,
                          "entry_sha256": hashlib.sha256(snapshot._canonical(item)).hexdigest(),
                          **({"surviving_parent": snapshot.retirement_parent_proof(parent, registration["sha256"])}
                             if observed == "intent" and not item["path"] else {})})
        if observed == "absent":
            cursor += 1
            removed_paths.add(tuple(item["path"]))
        return True

    try:
        disposed = root_removed or snapshot.remove(parent, expected_plan=planned, progress=progress, guard=guard)
    except (LeaseError, OSError) as exc:
        return "retained", f"retirement stopped ({getattr(exc, 'code', type(exc).__name__)})"
    if not disposed:
        return "retained", "guarded deletion stopped; the exact progress and remainder were kept"
    writer.observe_transport(task=view.task, origin=view.origin, expected=expected, kind="retired", authority=authority,
                              fact={"plan_sha256": registration["sha256"], "final_sha256": fact["final_sha256"],
                                    "removed_entries": cursor, **({"counted": False} if interrupted else {})})
    return "disposed-noncounting" if interrupted else "disposed", None


def committed_conditions(root: pathlib.Path, rev: str, binding: oah_intent.Binding) -> tuple[tuple[str, str], ...]:
    """The success conditions of the bound packet as committed at `rev`; it must match the binding."""
    shown = run_git(root, "show", f"{rev}:{binding.source}")
    try:
        parsed = oah_intent.parse(shown.stdout) if shown.returncode == 0 else None
    except oah_intent.IntentInvalid:
        parsed = None
    if parsed is None or parsed.digest != binding.digest:
        where = rev if rev == "HEAD" else rev[:12]
        raise ReviewError("OAH_INTENT_NOT_CURRENT", f"{binding.source} is not committed at {where} as bound "
                          f"({binding.line()}); the reviewer reads the committed packet, so commit it as bound, then "
                          "run oah review again")
    return parsed.conditions


def config_at(root: pathlib.Path, rev: str) -> Config:
    """`.oah/config.toml` as committed at `rev`; a commit without the file reads as empty."""
    label = f"{CONFIG_PATH} at the review base {rev[:12]}"
    status, _out = context._review_git(root, "ls-tree", "-z", rev, "--", CONFIG_PATH)
    if status == context.ABSENT:
        return Config(path=None, raw={}, label=label)
    _status, raw = context._review_git(root, "cat-file", "blob", f"{rev}:{CONFIG_PATH}")
    return parse_config(raw.encode("latin-1"), label=label)


def _call_settings(config: Config) -> call.Settings:
    return call.Settings.validated(config.section("stages", call.STAGE_DEFAULTS),
                                   config.section("engines", call.ENGINE_DEFAULTS),
                                   config.section("models", models.DEFAULTS))


def _is_ancestor(root: pathlib.Path, commit: str, head: str) -> bool:
    return run_git(root, "merge-base", "--is-ancestor", commit, head).returncode == 0


def review(request: Request, *, cwd: pathlib.Path, env: Mapping[str, str], keychain=None,
           temp_root: pathlib.Path | None = None, rules: Sequence[Mapping] | None = None,
           ensure_impact: Callable | None = None) -> Result:
    """Run one round; pre-dispatch refusals raise ReviewError (or the shared config, git and state errors)."""
    redact = credential_redactor(env)
    if request.decision is not None:
        request = dataclasses.replace(request, decision=redact(request.decision))
    try:
        return _review(request, cwd=cwd, env=env, keychain=keychain, temp_root=temp_root, rules=rules,
                       ensure_impact=ensure_impact)
    except BaseException as exc:
        message = str(exc)
        if redact(message) == message:
            raise
        code = getattr(exc, "code", "OAH_REVIEW_ENGINE_FAILED")
        message = redact(message)
        if message.startswith(code + ": "):
            message = message[len(code) + 2:]
        safe_error = ReviewError(code, message + "; fix the cause, then run oah review again")
    raise safe_error from None


def _review(request: Request, *, cwd: pathlib.Path, env: Mapping[str, str], keychain,
            temp_root: pathlib.Path | None, rules: Sequence[Mapping] | None, ensure_impact: Callable | None) -> Result:
    if env.get("OAH_WORKER") == "1":
        raise ReviewError("OAH_REVIEW_WORKER_REFUSED", "dispatched workers do not start reviews; return your work "
                          "to the dispatcher, who runs oah review")
    if request.decision is not None and not request.decision.strip():
        raise ReviewError("OAH_REVIEW_DECISION_EMPTY", "--decision needs the decision in words; say what you decided")
    info = repo_info(pathlib.Path(cwd))
    state = state_dir(info, env)
    lease = _lease(state, request.task)
    root = pathlib.Path(lease.worktree)
    if not root.is_dir():
        raise ReviewError("OAH_WORKTREE_UNRESOLVED", f"the lease of task {request.task} names the worktree {root}, "
                          "which no longer exists; nothing was launched. Release it with oah release --task "
                          f'{request.task} --agent {lease.agent} --reason "worktree removed", then start the lease '
                          "again in the task's worktree")
    binding = _check_intent(root, lease)
    design = _check_design(root, state, lease, binding)
    chosen = _base_settings(load_config(root))
    span = context.resolve(root, request.base, remote=chosen[0], branch=chosen[1])
    dirty = context.tracked_dirty(root)
    if dirty:
        listed = ", ".join(dirty[:10]) + (f" and {len(dirty) - 10} more" if len(dirty) > 10 else "")
        raise ReviewError("OAH_REVIEW_DIRTY_TREE", f"tracked files have uncommitted changes ({listed}); a review "
                          "reads a commit, so commit or stash them, then run oah review again")
    based = config_at(root, span.base)
    if request.base is None and _base_settings(based) != chosen:
        raise ReviewError("OAH_REVIEW_BASE_INVALID", "this change edits [preflight] remote or default_branch in "
                          f"{CONFIG_PATH}, which choose the review base; nothing was launched. Pass --base with the "
                          "commit this work started from")
    conf = review_settings.from_config(based)
    call_settings = _call_settings(based)
    if call_settings.stages.get("review") != ENGINE:
        raise ReviewError("OAH_REVIEW_ENGINE_UNSUPPORTED", f"[stages] review is {call_settings.stages.get('review')!r} "
                          f"in {based.where}; oah review runs Codex (structured output, ephemeral, ignored user "
                          'config). Set [stages] review = "codex" in .oah/config.toml and install Codex')
    series_lock = StateLock(receipts.lock_path(state, request.task, span.base), timeout=0)
    task_lock = StateLock(receipts.task_lock_path(state, request.task), timeout=0)
    try:
        series_lock.__enter__()
    except LockTimeout:
        raise ReviewError("OAH_REVIEW_SERIES_BUSY", f"another oah review of task {request.task} against base "
                          f"{span.base[:12]} is running; wait for it to finish, then run oah review again") from None
    try:
        try:
            task_lock.__enter__()
        except LockTimeout:
            raise ReviewError("OAH_REVIEW_SERIES_BUSY", f"another oah review of task {request.task} is running "
                              "against another base; wait for it to finish, then run oah review again") from None
        try:
            with rounds._managed_dispatch(request.task, root=root, base=span.base):
                return _round(request, root=root, state=state, env=env, lease=lease, binding=binding, conf=conf,
                              call_settings=call_settings, span=span, keychain=keychain, temp_root=temp_root,
                              rules=rules, ensure_impact=ensure_impact, design=design)
        finally:
            task_lock.__exit__(None, None, None)
    finally:
        series_lock.__exit__(None, None, None)


def _base_settings(config: Config) -> tuple[str, str]:
    found = config.section("preflight", preflight.DEFAULTS)
    return str(found["remote"] or ""), str(found["default_branch"] or "")


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
    if info.commit != head or _info.commit != head:
        raise ReviewError("OAH_IMPACT_STALE", f"HEAD moved to {info.commit} while the impact map was refreshed; "
                          "run oah review again")
    return _info, data


def _round(request: Request, *, root: pathlib.Path, state: pathlib.Path, env: Mapping[str, str], lease,
           binding: oah_intent.Binding, conf: review_settings.ReviewSettings, call_settings: call.Settings,
           span: context.Range, keychain, temp_root: pathlib.Path | None, rules: Sequence[Mapping] | None,
           ensure_impact: Callable | None, design=None) -> Result:
    task = request.task
    launch_env = reviewer_env(env)  # refuse proxy credentials before impact, probes or retained runs
    redact = credential_redactor(env)
    packet = call.read_packet(root, root / binding.source, parse_intent=oah_intent.parse,
                              read_source=oah_intent.read_source)
    call.check_dispatch_binding(call.dispatch_binding(state, task), packet, task)
    impact_info, data = _impact(root, env, ensure_impact, span.head)
    with StateLock(state / ".lock"):
        records = receipts.load(state, task)
        s = rounds.series(records, receipts.read_round_log(state, task), span.base, head=span.head,
                          is_ancestor=lambda commit, head: _is_ancestor(root, commit, head))
        budget = rounds.resolve_task_budget(s, conf, root=root, state=state)
    conf = dataclasses.replace(conf, max_rounds=budget.limit)
    from oah.design.binding import receipt_design, review_evidence

    evidence = review_evidence(design) if design is not None else None
    if rounds.gate(s, head=span.head, decision=request.decision, extra_round=request.extra_round,
                   settings=conf, extra_attempt=request.extra_attempt, binding=binding,
                   budget=budget, design=evidence) == rounds.CONVERGED:
        latest = s.latest
        return Result("OAH_REVIEW_CONVERGED", 0, f"OAH_REVIEW_CONVERGED: {latest.name} already approved head "
                      f"{span.head}; nothing was launched",
                      lines=_lines(latest.record, latest.digest, latest.name, conf, task), receipt=latest.record,
                      receipt_path=receipts.review_dir(state, task) / latest.name)
    extra = bool(request.extra_round and conf.max_rounds is not None and s.counted_rounds >= conf.max_rounds)
    extra_attempt = request.extra_attempt
    warnings: list[str] = []
    stopped = rounds.waiting(s, settled=False)
    if request.decision is not None and stopped is None:
        warnings.append("OAH_REVIEW_DECISION_UNUSED: no decision was pending for this round; your note is recorded "
                        "with it anyway")
    if request.extra_round and not extra:
        warnings.append("OAH_REVIEW_EXTRA_ROUND_UNUSED: this task is below [review] max_rounds, so --extra-round "
                        "changed nothing")
    decision = request.decision or rounds.round_decision(s)
    command = reviewer_command(call_settings.engines[ENGINE]["command"])
    cli_path, cli_sha = reviewer_binary(command)
    call_settings = dataclasses.replace(call_settings, engines={
        **call_settings.engines, ENGINE: {**call_settings.engines[ENGINE], "command": command}})
    if conf.codex_sha256 and cli_sha != conf.codex_sha256:
        raise ReviewError("OAH_REVIEW_BINARY_MISMATCH", f"the reviewer CLI {cli_path} has sha256 {cli_sha}, but "
                          f"[review] codex_sha256 pins {conf.codex_sha256}; nothing was launched. If you updated "
                          "Codex on purpose, set codex_sha256 to the new value; otherwise reinstall Codex from its "
                          "official source")
    if conf.codex_version:
        check_version(command, conf.codex_version, launch_env)
    commits = authors.commit_engines(root, span.base, span.head)
    engines = [engine for _sha, engine in commits]
    changed = context.changed_files(root, span.base, span.head)
    critical = context.critical(changed, conf.critical_paths)
    if CONFIG_PATH in changed:
        critical = sorted({*critical, CONFIG_PATH})
        warnings.append(f"OAH_REVIEW_CONFIG_CHANGED: this change edits {CONFIG_PATH}; the review uses the "
                        "configuration committed at the base, and the reviewer is asked to check the change")
    db, impact, impact_rows, impact_warnings = context.assess_impact(
        root, changed, span.base, span.head, impact_info, data, env=env)
    warnings.extend(impact_warnings)
    conditions = committed_conditions(root, span.head, binding)
    number, attempt = s.next_number, sum(1 for line in s.round_lines() if line.get("event") == "start") + 1
    tools = call_cli.intent_tools()
    started = dt.datetime.now(dt.timezone.utc)
    # SC4-P4-R: the run is allocated before the snapshot, and a design-bound review arms the run-bound exclusive
    # disposal closure; a review without a design keeps the baseline removal.
    planned_run = runs._plan_run(state, task, ENGINE, "review")
    disposition: list[tuple[str, str | None]] = []
    dispose = (_snapshot_disposal(state, planned_run[0].id, disposition)
               if design is not None and design.task is not None else None)
    where = {"base": span.base, "series_round": s.series_round, "round": number, "attempt": attempt,
             "run_id": planned_run[0].id}
    started_round = []

    def record_failure(exc):
        if started_round:
            outcome = disposition[-1] if disposition else ("retained", "the disposal outcome is unavailable")
            receipts.log_round(state, task, {"event": "finish", **where, "counted": False,
                               "code": getattr(exc, "code", type(exc).__name__),
                               "message": redact(str(exc))[:600],
                               "snapshot_disposition": outcome[0],
                               "snapshot_reason": redact(outcome[1])[:600] if outcome[1] else None})

    with _snapshot_failure(record_failure), snapshot.make(
            root, span.head, base=span.base, temp_root=temp_root, warnings=warnings, dispose=dispose) as snap:
        value = canary.plant(snap.tree)
        schema_path = snap.tree.parent / "output-schema.json"
        schema_path.write_bytes(schema.text().encode("utf-8"))  # the bytes the receipt's digest names
        final_path = snap.tree.parent / "last-message.md"
        if dispose is not None:
            dispose.prepare(snap.tree.parent, design, metadata={"base": span.base, "head": span.head,
                            "stripped": list(snap.stripped), "canary_sha256": hashlib.sha256(value.encode()).hexdigest(),
                            "schema_sha256": hashlib.sha256(schema.text().encode("utf-8")).hexdigest()})
        context_ = prompt.PromptContext(
            task=task, number=number, series_round=s.series_round, base=span.base, head=span.head,
            scope=tuple(lease.scope), conditions=conditions, diff_stat=context.diff_stat(root, span.base, span.head),
            critical=tuple(critical), stripped=snap.stripped, db=db,
            history=tuple(prompt.HistoryItem(entry.kind, entry.name, entry.digest,
                                             receipts.canonical(entry.record).decode("utf-8")) for entry in s.history),
            history_digest=s.history_digest(),
            attempted=tuple(prompt.AttemptedClass(class_id, tuple(tried),
                                                  s.classes().get(class_id, {}).get("mechanism", "(not recorded)"),
                                                  s.classes().get(class_id, {}).get("rule", "(not recorded)"))
                            for class_id, tried in s.attempted().items()),
            decision=decision, stopped=stopped if decision else None, extra_round=extra,
            impact=impact, impact_rows=impact_rows,
            design_line=(f"{design.task.binding.line()}, state {design.state}"
                         if design is not None and design.task is not None else None))
        def start_round() -> None:
            receipts.log_round(state, task, {"event": "start", **where, "head": span.head, "extra_round": extra,
                                             "extra_attempt": extra_attempt, "decision": request.decision,
                                             "max_rounds": conf.max_rounds,
                                             "decision_for": s.task_latest.digest if s.task_latest else None})
            started_round.append(True)
            for note in ([f"review R{number}: the user's decision before this round: {request.decision}"]
                         if request.decision else []) + ([f"review R{number} runs beyond [review] max_rounds = "
                                                          f"{conf.max_rounds} (--extra-round)"] if extra else []):
                Store(state).log(task=task, agent=lease.agent, message=note)
            if extra_attempt:
                Store(state).log(task=task, agent=lease.agent,
                                 message=f"review R{number} attempt {attempt}: one additional call (--extra-attempt)")

        from oah.design import task_context as design_context

        token = call._PREPARATION_RUN.set(planned_run)
        start_token = call._PREPARATION_START.set(start_round)
        retirement_token = design_context.RETIREMENT_CONTEXT.set(dispose)
        try:
            prepared = call.prepare(
                call.CallRequest(engine=ENGINE, stage="review", task=task, tier=request.tier,
                                 round_instructions=redact(prompt.round_instructions(context_)),
                                 output_schema=str(schema_path), ephemeral=True, isolated=True,
                                 timeout_s=request.timeout_s, output_last_message=str(final_path)),
                call_settings, root=snap.tree, state_dir=state, env=launch_env,
                footer=tools.footer, has_footer=tools.has_footer,
                parse_intent=tools.parse, read_source=tools.read_source, keychain=keychain)
        finally:
            call._PREPARATION_START.reset(start_token)
            call._PREPARATION_RUN.reset(token)
            design_context.RETIREMENT_CONTEXT.reset(retirement_token)
        warnings.extend(prepared.warnings)
        retirement_token = design_context.RETIREMENT_CONTEXT.set(dispose)
        try:
            run = call.execute(prepared, base_env=launch_env, rules=rules, redact=redact)
        finally:
            design_context.RETIREMENT_CONTEXT.reset(retirement_token)
        last = runs._contained_path(snap.tree.parent, final_path.name)
        raw = last.read_bytes() if last.is_file() else b""
        original_output = raw
        output_error = None
        try:
            raw = _safe_output(raw, redact)
        except ReviewError as exc:
            output_error, raw = exc, b""
        prepared.run._write_text("last-message.md", raw.decode("utf-8"))
        if dispose is not None:
            dispose.capture_final(snap.tree.parent, original_output, raw)
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
            message = (f"{code}: the reviewer did not finish ({run.get('error')}), so this round does not count. "
                       "If sign-in is missing, use codex login (including codex login --with-api-key); the reviewer "
                       "never receives CODEX_API_KEY or OPENAI_API_KEY environment variables. Then retry oah review.")
    else:
        try:
            if output_error is not None:
                raise output_error
            output = schema.parse(raw, number=number)
            rounds.check(output, s, number=number, binding=binding, base=span.base, head=span.head,
                         canary_value=value, db_line=db.line)
        except ReviewError as exc:
            code, message = exc.code, str(exc)
    if (dispose is not None and code is None
            and (not disposition or disposition[-1][0] not in ("disposed", "legacy-disposed"))):
        why = disposition[-1][1] if disposition else "the disposal check did not run"
        if disposition and disposition[-1][0] == "disposed-noncounting":
            code, message = "OAH_REVIEW_SNAPSHOT_NONCOUNTING", ("OAH_REVIEW_SNAPSHOT_NONCOUNTING: snapshot retirement "
                                "completed after an interrupted delete; its absence acknowledgment does not count "
                                "the original round")
        else:
            code, message = "OAH_REVIEW_SNAPSHOT_RETAINED", ("OAH_REVIEW_SNAPSHOT_RETAINED: the review snapshot has "
                                                           f"no counting disposal proof ({why}), so this round does not count")
    if code is not None:
        receipts.log_round(state, task, {"event": "finish", **where, "counted": False, "code": code,
                                         "message": message[:600]})
        left = conf.round_attempts - attempt
        retry = f"oah review --task {task} --base {span.base}"
        if conf.max_rounds is not None and s.counted_rounds + 1 >= conf.max_rounds:
            retry += " --extra-round"
        if left <= 0:
            retry += " --extra-attempt"
        budget = (f" Its reserved round remains spent. This was attempt {attempt} of {conf.round_attempts} for "
                  f"round {s.series_round} of this series; fix the cause, then run {retry} to retry (recorded).")
        return Result(code, 1, message + budget, lines=[f"run folder: {prepared.run_dir}"], warnings=warnings,
                      run_dir=prepared.run_dir)
    judged = rounds.decide(output, s)
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
                     "tier_source": run.get("tier_source"), "run_id": prepared.run_id,
                     "model_source": run.get("model_source"), **(run.get("admission") or {}),
                     **{key: run.get(key) for key in ("rule_tier", "jev_tier", "jev_confidence", "jev_status")}},
        "author_engines": sorted(set(engines)), "authorship": [{"commit": sha, "engine": engine} for sha, engine in commits],
        "independent": independent, "verdict": output.verdict, "summary": output.summary,
        "findings": [finding.to_json() for finding in output.findings],
        "classes": [item.to_json() for item in output.classes], "outcome": judged.outcome,
        "follow_ups": list(judged.follow_ups), "decision_stop_reason": judged.stop_reason,
        "new_instances": list(judged.new_instances),
        "decision": {"text": decision, "after": s.task_latest.name if s.task_latest else None} if decision else None,
        "extra_round": extra, "extra_attempt": extra_attempt,
        "history": s.history_items(), "history_digest": s.history_digest(),
        "canary_verified": True, "canary_scope": canary.LIMIT, "db_impact": db.line, "db_query": db.query,
        "impact": impact,
        "critical_paths": list(critical), "stripped": list(context_.stripped),
        "intent_return": output.intent_return, "evidence": output.evidence,
        "prompt_sha256": receipts.digest(prepared.run.prompt_path.read_bytes()), "output_sha256": receipts.digest(raw),
        "output_schema_sha256": receipts.digest(schema.text().encode("utf-8")),
        "started_at": _iso(started), "finished_at": _iso(finished)}
    if design is not None and design.task is not None:
        receipt["design"] = receipt_design(prepared.run._read_json(call.DESIGN_NAME))
    name = f"R{number}.json"
    digest = receipts.publish(state, task, name, receipt, summary={
        "round": number, "series_round": s.series_round, "base": span.base, "head": span.head,
        "verdict": output.verdict, "outcome": judged.outcome})
    code = OUTCOME_CODES[judged.outcome]
    receipts.log_round(state, task, {"event": "finish", **where, "counted": True, "code": code, "receipt": name,
                                     "outcome": judged.outcome})
    if judged.outcome == "converged" and design is not None and design.state == "needs-review":
        unresolved = ("OAH_DESIGN_NEEDS_REVIEW: the reviewer approved the change against the accepted design, but the "
                      f"newer draft r{design.context.current.revision} is unresolved, so this approval cannot complete "
                      f"task {task}. Carry it (oah design carry) or rebind, then the receipt can count")
        return Result("OAH_DESIGN_NEEDS_REVIEW", 1, unresolved, lines=_lines(receipt, digest, name, conf, task),
                      warnings=warnings, receipt_path=receipts.review_dir(state, task) / name, receipt=receipt,
                      run_dir=prepared.run_dir)
    return Result(code, 0 if judged.outcome == "converged" else 1, _headline(code, receipt, task),
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
    marks = "; " + "; ".join(receipt["new_instances"]) if receipt.get("new_instances") else ""
    return f"{code}: {summary} {rest}{marks}"


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
             f"OAH_REVIEW_EFFORT: normalization basis: {reviewer['normalization_basis']}; inspect the receipt "
             "for the recorded effort checks",
             label, f"canary: verified — {canary.LIMIT}", receipt["db_impact"]]
    if receipt["critical_paths"]:
        lines.append(f"critical paths changed: {', '.join(receipt['critical_paths'])}")
    lines += [f"{item['finding_id']} {item['severity']} {item['axis']} {item['location']} — {item['title']}"
              for item in receipt["findings"]]
    if (receipt["outcome"] == "changes-requested" and conf.max_rounds is not None
            and receipt["round"] >= conf.max_rounds):
        lines.append(f"OAH_REVIEW_ROUNDS_EXHAUSTED: this task reached [review] max_rounds = {conf.max_rounds}; the "
                     f"next round needs oah review --task {task} --extra-round")
    return lines
