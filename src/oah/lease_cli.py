# 리스 명령 — oah start/renew/status/finish/release/log 의 인자, 저장소·상태 경로 찾기, 출력 형식, 종료 코드를 맡는다.
# `start --packet` binds the packet's accepted intent in the same command, or moves an existing binding to a
# raised Revision; `status` shows each lease's binding state; `finish` checks a bound lease's return
# (`--return`) against the binding (PLAN 5.6, A7). No module docstring: `oah --help` names groups by it.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys

from oah import intent, packet
from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError, load_config
from oah.gates import guard
from oah.gates.check import GateRefused
from oah.gates.context import DEFAULTS as GATE_DEFAULTS, LEDGER_NAME, SUMMARY_NAME, GateTools, find_tools
from oah.gates.summary import SummaryRejected
from oah.journal import PendingTransaction
from oah.lease import DEFAULT_TTL_HOURS, Lease, LeaseError, Store, _refuse_worker_lease
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, RepoInfo, repo_info, run_git, state_dir
from oah.scope import ScopeError, normalize_scope
from oah.unlazy import UnlazyError
from oah.wiki.base import DEFAULTS as WIKI_DEFAULTS, WikiError, WikiSettings
from oah.wiki.store import maintenance_output_paths

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69
GATE_ENVIRONMENT_CODES = {"OAH_UNLAZY_FAILED"}
_AC_STATUS = re.compile(r"(AC[1-9][0-9]*): (met|unmet|deferred) (?:—|--) \S")
COMMANDS = ("start", "renew", "status", "finish", "release", "log")


def register(commands: argparse._SubParsersAction) -> None:
    start = commands.add_parser("start", help="take a lease on paths before editing them")
    _identity(start)
    start.add_argument("--scope", action="append", help="a path, or 'repo'; repeat for more (required without "
                                                        "--packet; with --packet it adds to the packet's Scope)")
    start.add_argument("--packet", help="bind this accepted packet's intent to the lease; the packet's Scope "
                                        "becomes the lease scope. On your existing lease it binds a raised Revision")
    start.add_argument("--note", default="")
    start.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    start.add_argument("--gates", action="append", help="a gate ledger this task must meet; repeat for more "
                                                        "(default: .oah/tasks/<task>/GATES.md when it exists)")
    start.add_argument("--draft-revision", type=int, metavar="N", help="with --packet: the newer draft of the design "
                       "you read and found not to change this task (with --draft-digest and --draft-reason)")
    start.add_argument("--draft-digest", metavar="DIGEST", help="that draft's digest, as oah design task prints it")
    start.add_argument("--draft-reason", metavar="WHY", help="why the draft does not change this task (recorded)")
    start.add_argument("--json", action="store_true", help="print the lease as JSON")

    renew = commands.add_parser("renew", help="extend your lease, optionally with a new scope")
    _identity(renew)
    renew.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    renew.add_argument("--scope", action="append", help="replace the scope; repeat for more")
    renew.add_argument("--gates", action="append", help="replace the declared gate ledgers; repeat for more")

    status = commands.add_parser("status", help="show active and stale leases (reads only)")
    status.add_argument("--json", action="store_true")

    finish = commands.add_parser("finish", help="check the changes against the scope and close your lease")
    _identity(finish)
    finish.add_argument("--summary", required=True)
    finish.add_argument("--allow-out-of-scope", action="store_true",
                        help="finish even though files outside the scope changed (recorded)")
    finish.add_argument("--allow-structural", action="store_true",
                        help="without unlazy, finish on the ledger's boxes alone (recorded as unverified)")
    finish.add_argument("--allow-missing-wiki-maintenance", action="store_true",
                        help="finish although oah wiki maintain was not run for this task (recorded)")
    finish.add_argument("--return", dest="return_file", metavar="FILE",
                        help="your return text ('-' reads stdin); required when the lease is bound to an intent")

    release = commands.add_parser("release", help="drop a lease without finishing it")
    _identity(release)
    release.add_argument("--reason", required=True)
    release.add_argument("--force", action="store_true", help="release another agent's lease (recorded)")

    log = commands.add_parser("log", help="append a note to the event log")
    _identity(log)
    log.add_argument("--message", required=True)


def _identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", required=True)
    parser.add_argument("--agent", required=True, help="who holds the lease, for example claude, codex or human")


def run(args: argparse.Namespace) -> int:
    store = None
    try:
        info = repo_info(pathlib.Path.cwd())
        store = Store(state_dir(info, os.environ))
        return HANDLERS[args.command](args, info, store)
    except (LeaseError, ScopeError, SummaryRejected) as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except GateRefused as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT if exc.code in GATE_ENVIRONMENT_CODES else EXIT_REFUSED
    except UnlazyError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except WikiError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except (NotARepo, GitMissing, LockTimeout, StateCorrupt, PendingTransaction, StorageError, ConfigError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        path = pathlib.Path(exc.filename) if exc.filename else store.dir if store else pathlib.Path(".")
        print(StorageError(path, exc), file=sys.stderr)
        return EXIT_ENVIRONMENT


def _summary(lease: Lease) -> str:
    return f"task={lease.task} agent={lease.agent} scope={','.join(lease.scope)} expires={lease.expires_at}"


def _ledger_paths(raw: list[str] | None, info: RepoInfo) -> list[str]:
    """Declared gate ledgers as repository-relative POSIX paths; a path outside the repository is refused."""
    paths = []
    root = info.root.resolve()
    for entry in raw or []:
        candidate = pathlib.Path(entry)
        resolved = (candidate if candidate.is_absolute() else pathlib.Path.cwd() / candidate).resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            raise LeaseError("OAH_GATES_PATH_INVALID", f"{entry}: a gate ledger must be inside the repository") from None
        if relative in (".", "") or relative in paths:
            continue
        paths.append(relative)
    return paths


def _packet_source(raw: str, info: RepoInfo) -> tuple[pathlib.Path, str]:
    """The packet path and its repository-relative POSIX source; a packet outside the repository is refused."""
    candidate = pathlib.Path(raw)
    path = candidate if candidate.is_absolute() else pathlib.Path.cwd() / candidate
    for base in (info.root, info.root.resolve()):
        try:
            return path, pathlib.Path(os.path.normpath(path)).relative_to(base).as_posix()
        except ValueError:
            continue
    try:
        return path, path.resolve().relative_to(info.root.resolve()).as_posix()
    except (ValueError, OSError):
        raise LeaseError("OAH_INTENT_SOURCE_INVALID", f"{raw} is outside this repository; keep the packet in the "
                         "repository, such as .oah/tasks/<task>/PACKET.md (oah packet new <task>)") from None


def _bindable(args: argparse.Namespace, info: RepoInfo) -> tuple[intent.Binding, tuple[str, ...], list[str],
                                                                  packet.CheckResult]:
    """The binding, lease scope and packet warnings from an accepted packet, or a refusal naming the first problem."""
    path, source = _packet_source(args.packet, info)
    result = packet.check(path, task=args.task)
    if result.errors:
        first, rest = result.errors[0], result.errors[1:]
        more = f" Also: {'; '.join(str(problem) for problem in rest)}." if rest else ""
        nxt = (f" Create it with: oah packet new {args.task}" if first.code == "OAH_INTENT_SOURCE_MISSING" else
               f" Fix the packet, run oah packet check {args.task}, then run oah start again.")
        raise LeaseError(first.code, f"{first.message}.{more}{nxt}")
    explicit = list(args.scope or [])
    if not result.scope and "repo" not in explicit:
        raise LeaseError("OAH_SCOPE_EMPTY", f"the packet's ## Scope lists no paths. List the paths this task may "
                         f"write in {source}, or pass --scope repo to lease the whole repository")
    warnings = [f"warning {warning}" for warning in result.warnings]
    return result.intent.bind(source), normalize_scope([*result.scope, *explicit], info.root), warnings, result


def _default_packet_text(root: pathlib.Path, task: str) -> tuple[str, str | None]:
    """The task's conventional packet source and its text when it can be read."""
    source = f".oah/tasks/{task}/PACKET.md"
    try:
        return source, intent.read_source(root / source)
    except intent.SourceError:
        return source, None


def _no_section(task: str, agent: str, found, source: str):
    from oah.design.binding import BINDING_REQUIRED_CODE, ORIGIN_INVALID_CODE, ORIGIN_UNKNOWN_CODE
    from oah.design.model import DesignError

    if found.kind == "unknown":
        return DesignError(ORIGIN_UNKNOWN_CODE, f"whether task {task} follows a design is unknown ({found.detail}); "
                           "nothing was changed. Repair the state folder, then retry", EXIT_ENVIRONMENT)
    code = ORIGIN_INVALID_CODE if found.kind == "invalid" else BINDING_REQUIRED_CODE
    return DesignError(code, f"task {task} follows a design ({found.detail}), and nothing names its design here, so "
                       f"the lease would not be bound to it; nothing was changed. Restore the packet's ## Design section "
                       f"(oah packet design {task}), commit it and run oah start --task {task} --agent {agent} --packet "
                       f"{source}")


def _draft_given(args: argparse.Namespace):
    """(revision, digest, reason) from --draft-revision/--draft-digest/--draft-reason, or None; all or none."""
    from oah.design.model import INPUT_INVALID_CODE, DesignError, check_digest, check_line, check_revision

    given = [args.draft_revision is not None, args.draft_digest is not None, args.draft_reason is not None]
    if not any(given):
        return None
    if not all(given) or not args.packet:
        raise DesignError(INPUT_INVALID_CODE, "--draft-revision, --draft-digest and --draft-reason go together, with "
                          "--packet; give all three")
    return (check_revision(args.draft_revision, "draft revision"), check_digest(args.draft_digest, "draft digest"),
            check_line(args.draft_reason, "draft reason"))


def _instance(view, locked, repo: str, task: str):
    """(task identity, previous record or None, resume link or None) for a start or rebind (B1).

    An open lease keeps its instance. A new lease resumes the last instance only when its last epoch ended by
    expiry; after finish or release the reused label is a new undertaking. A task linked by oah packet new
    --design but never started keeps that instance."""
    from oah.design import binding as design

    if locked is not None:
        record = locked.design_record()
        if record is not None:
            return record.identity, record, None
        if view.record is not None and view.epoch is not None and view.epoch.get("lease_id") == locked.id:
            return view.record.identity, view.record, None
        return design.TaskIdentity(repo, task, design.mint_instance()), None, None
    epoch = view.epoch
    if epoch is not None and epoch.get("ended") == "expired" and view.record is not None:
        link = {"expired_lease_id": epoch.get("lease_id"), "expired_event_transaction": epoch.get("end_transaction"),
                "prior_record_digest": view.record.digest}
        return view.record.identity, view.record, link
    if epoch is not None and epoch.get("ended") in ("finished", "released"):
        return design.TaskIdentity(repo, task, design.mint_instance()), None, None
    if view.identity is not None and view.identity.repo == repo:
        return view.identity, None, None
    return design.TaskIdentity(repo, task, design.mint_instance()), None, None


def _design_update(args: argparse.Namespace, info: RepoInfo, store: Store, result: packet.CheckResult | None,
                   source: str, notes: dict, *, intent_binding: intent.Binding | None = None,
                   scope: tuple[str, ...] = (), gates: list[str] | None = None, explicit_gates: bool = False):
    """The locked design step of oah start: refuse a design-origin task without a section, or bind onto the
    accepted revision for the exact proposed task inputs (B1), resuming an expired instance when it may."""
    from oah.design import binding as design
    from oah.lease import _iso, utc_now

    section = result.design if result is not None else None
    drafted = _draft_given(args)

    def update(locked):
        view = design.task_view(store.dir, args.task, lease=locked)
        if section is None:
            text = result.text if result is not None else _default_packet_text(info.root, args.task)[1]
            found = design.origin(store.dir, args.task, packet_text=text, root=info.root, source=source, view=view)
            if found is None and locked is not None and (locked.design is not None or locked.design_invalid):
                found = design.Origin("lease", "its lease records a design")
            if found is not None:
                raise _no_section(args.task, args.agent, found, source)
            return None, None, {}
        if view.unreadable is not None or view.problem is not None:
            raise _no_section(args.task, args.agent, design.origin(store.dir, args.task, view=view), source)
        try:
            identity, old, resume = _instance(view, locked, design.repo_identity(info.root), args.task)
        except design.RecordInvalid as exc:
            raise design.judge(None, root=info.root, invalid=exc.identity).error("start", args.task, args.agent)
        root = pathlib.Path(locked.worktree) if locked is not None else info.root
        context = design.read_context(section.binding(), root=root)
        if isinstance(context, design.TaskDesign):
            raise context.error("start", args.task, args.agent)
        declared = list(gates or []) if locked is None or explicit_gates else list(locked.gates)
        basis = design.TaskBasis.of(identity, project_uid=section.uid, design=section.design,
                                    intent=intent_binding.to_json() if intent_binding is not None else None,
                                    scope=scope, gates=declared)
        draft = None
        if drafted is not None:
            authority = design.authority_of(locked) if locked is not None else None
            draft = design.draft_decision(context, basis=basis.digest, revision=drafted[0], digest=drafted[1],
                                          reason=drafted[2], actor=args.agent, at=_iso(utc_now()),
                                          authority=authority, task=args.task)
            if draft is None:
                notes.setdefault("warnings", []).append(
                    "OAH_DESIGN_DRAFT_UNUSED: there is no newer draft than the accepted revision, so the draft "
                    "acknowledgment was not recorded")
        record, action, warnings = design.next_record(old, section, context, identity=identity, basis=basis,
                                                      draft=draft)
        design.require_fits(record, args.task)
        design.judge_with_context(record, context).require("start", task=args.task, agent=args.agent)
        notes.setdefault("warnings", []).extend(warnings)
        notes.update(action=action, record=record)
        extra = {"design_packet": section.to_json(), "task_basis_digest": basis.digest}
        if locked is None or locked.design is None:  # the epoch's first design binding: capture its outputs
            extra["design_output_baseline"] = design.output_baseline(
                root, _wiki_settings(root), record.binding, head=locked.head_at_start if locked is not None
                else info.head)
        if resume is not None:
            extra["resume_from"] = resume
        if old is not None and old.basis != basis.digest:
            extra["old_task_basis_digest"] = old.basis
        return record.to_json(), action, extra

    return update


def _design_readout(lease: Lease, notes: dict, as_json: bool) -> tuple[list[str], dict | None]:
    """After a bound start: the state line and the design text (or an explicit non-delivery), read lock-free."""
    from oah.design import binding as design
    from oah.design import task_context

    record = notes.get("record")
    if record is None:
        return [], None
    basis = design.basis_of(record, intent=lease.intent, scope=lease.scope, gates=lease.gates)
    judged = design.judge(record, root=pathlib.Path(lease.worktree), basis=basis)
    block = task_context.readout_block(judged, task=lease.task, root=pathlib.Path(lease.worktree))
    word = {"bound": "OAH_DESIGN_BOUND", "rebound": "OAH_DESIGN_REBOUND", "kept": "OAH_DESIGN_BINDING_KEPT",
            "rebased": "OAH_DESIGN_INPUTS_REBOUND"}
    lines = [f"{word[notes['action']]} task={lease.task} {record.binding.line()} state={judged.state} "
             f"instance={record.identity.instance}"]
    if notes["action"] in ("rebound", "rebased"):
        lines.append("note: the task's requirements or inputs moved; review receipts and gate evidence made for the "
                     "earlier binding or inputs no longer count")
    if not as_json:
        lines.append(block.readout().rstrip("\n"))
    # Start offers the text; delivery is a terminal fact of a run that received it.
    data = {"state": judged.state, "delivered": False, "offered": block.delivered, "block": block.text,
            "record": block.record, "plan": block.plan}
    return lines, data


def _start(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    _refuse_worker_lease()
    if not args.scope and not args.packet:
        print("OAH_USAGE: oah start needs --scope PATH (repeat for more, or 'repo') or --packet PATH",
              file=sys.stderr)
        return EXIT_USAGE
    gates = _ledger_paths(args.gates, info)
    if args.packet:
        return _start_packet(args, info, store, gates)
    if any(value is not None for value in (args.draft_revision, args.draft_digest, args.draft_reason)):
        print("OAH_USAGE: --draft-revision, --draft-digest and --draft-reason need --packet", file=sys.stderr)
        return EXIT_USAGE
    scope = normalize_scope(args.scope, info.root)
    source, _text = _default_packet_text(info.root, args.task)
    lease = store.start(task=args.task, agent=args.agent, scope=scope, note=args.note, ttl_hours=args.ttl_hours,
                        info=info, gates=gates, design_update=_design_update(args, info, store, None, source, {}))
    print(json.dumps(lease.public_json(), indent=2, ensure_ascii=True) if args.json
          else f"OAH_LEASE_STARTED {_summary(lease)}")
    return EXIT_OK


def _start_packet(args: argparse.Namespace, info: RepoInfo, store: Store, gates: list[str]) -> int:
    binding, scope, warnings, result = _bindable(args, info)
    named = f"intent={binding.id}@r{binding.revision} {binding.digest}"
    notes: dict = {}
    update = _design_update(args, info, store, result, binding.source, notes, intent_binding=binding, scope=scope,
                            gates=gates, explicit_gates=bool(args.gates))
    try:
        lease = store.start(task=args.task, agent=args.agent, scope=scope, note=args.note, ttl_hours=args.ttl_hours,
                            info=info, gates=gates, intent=binding, design_update=update)
        line = f"OAH_LEASE_STARTED {_summary(lease)} {named}"
    except LeaseError as exc:
        if exc.code != "OAH_LEASE_EXISTS":
            raise
        lease, action = store.rebind(task=args.task, agent=args.agent, binding=binding, ttl_hours=args.ttl_hours,
                                     scope=scope, gates=gates if args.gates else None, design_update=update)
        word = {"bound": "OAH_INTENT_BOUND", "rebound": "OAH_INTENT_REBOUND", "unchanged": "OAH_INTENT_UNCHANGED"}
        line = f"{word[action]} task={lease.task} {named} expires={lease.expires_at}"
    for warning in warnings:
        print(warning, file=sys.stderr)
    for warning in notes.get("warnings", ()):
        print(f"warning {warning}", file=sys.stderr)
    design_lines, design_data = _design_readout(lease, notes, args.json)
    if args.json:
        data = lease.public_json()
        if design_data is not None:
            data["design_context"] = design_data
        print(json.dumps(data, indent=2, ensure_ascii=True))
    else:
        print(line)
        for item in design_lines:
            print(item)
    return EXIT_OK


def _intent_state(lease: Lease) -> tuple[str, str]:
    """The lease's binding state and reason, judged against the lease's own worktree."""
    try:
        binding = lease.binding()
    except ValueError as exc:
        return "stale", str(exc)
    return intent.explain_binding(binding, pathlib.Path(lease.worktree))


def _renew(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    _refuse_worker_lease()
    scope = normalize_scope(args.scope, info.root) if args.scope else None
    gates = _ledger_paths(args.gates, info) or None
    lease = store.renew(task=args.task, agent=args.agent, ttl_hours=args.ttl_hours, scope=scope, gates=gates)
    print(f"OAH_LEASE_RENEWED {_summary(lease)}")
    return EXIT_OK


def lease_design(lease: Lease, state: pathlib.Path, *, packet_text: str | None = None, source: str | None = None,
                 root: pathlib.Path | None = None):
    """The judged design of an open lease: its record against the basis rederived from the lease's own intent,
    scope and gates; for a lease without a record, the task's origin evidence decides (legacy or required)."""
    from oah.design import binding as design

    root = root or pathlib.Path(lease.worktree)
    view = design.task_view(state, lease.task, lease=lease)
    if view.unreadable is not None or view.problem is not None:
        return design.judge_task(state, lease.task, root=root, view=view, packet_text=packet_text, source=source)
    try:
        record = lease.design_record()
    except design.RecordInvalid as exc:
        return design.judge(None, root=root, invalid=exc.identity)
    if record is None:
        # The caller's lease is the record: never re-read leases.json here (fix captures it exactly once).
        found = design.origin(state, lease.task, packet_text=packet_text, root=root if source else None,
                              source=source, view=design.task_view(state, lease.task, lease=lease))
        if found is None and lease.design_invalid:
            found = design.Origin("lease", "its lease records a design")
        return design.judge(None, root=root, found=found)
    return design.judge_task(state, lease.task, root=root, view=view, packet_text=packet_text, source=source)


def _design_state(lease: Lease, state: pathlib.Path) -> tuple[str, str]:
    """The lease's design state and reason, read-only."""
    judged = lease_design(lease, state)
    return judged.state, judged.reason


def _status(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    entries = store.snapshot()
    states = [_intent_state(lease) for lease, _stale in entries]
    designs = [_design_state(lease, store.dir) for lease, _stale in entries]
    if args.json:
        print(json.dumps({"leases": [dict(lease.public_json(), stale=stale, intent_state=state, intent_reason=reason,
                                          design_state=design_state, design_reason=design_reason)
                                     for (lease, stale), (state, reason), (design_state, design_reason)
                                     in zip(entries, states, designs)]},
                         indent=2, ensure_ascii=True))
        return EXIT_OK
    if not entries:
        print("no leases")
        return EXIT_OK
    for (lease, stale), (intent_state, _reason), (design_state, _why) in zip(entries, states, designs):
        state = "STALE" if stale else "active"
        shown = f"  design={design_state}" if design_state != "unbound" else ""
        print(f"{state:6}  {_summary(lease)}  intent={intent_state}{shown}  worktree={lease.worktree}")
    return EXIT_OK


def _changed_files(lease: Lease, info: RepoInfo) -> list[str]:
    """Files changed in the lease's worktree since the lease started: committed, staged, unstaged, untracked."""
    worktree = pathlib.Path(lease.worktree)
    message = (f"recorded worktree {worktree} is missing or no longer belongs to this repository; "
               "restore that worktree before finishing, or explicitly release the lease")
    if not worktree.is_dir():
        raise LeaseError("OAH_LEASE_WORKTREE_INVALID", message)
    try:
        current = repo_info(worktree)
    except NotARepo as exc:
        raise LeaseError("OAH_LEASE_WORKTREE_INVALID", message) from exc
    if current.root.resolve() != worktree.resolve() or current.common_dir.resolve() != info.common_dir.resolve():
        raise LeaseError("OAH_LEASE_WORKTREE_INVALID", message)
    registered = run_git(worktree, "worktree", "list", "--porcelain", "-z")
    roots = [pathlib.Path(field[len("worktree "):]).absolute() for field in registered.stdout.split("\0")
             if field.startswith("worktree ")]
    if registered.returncode != 0 or worktree.absolute() not in roots:
        raise LeaseError("OAH_LEASE_WORKTREE_INVALID", message)
    commands = []
    diff = ("diff", "--no-renames", "--name-only", "-z")
    if current.head is not None:
        if lease.head_at_start is not None:
            commands.append((*diff, lease.head_at_start, current.head, "--"))
        else:
            commands.append(("ls-tree", "-r", "--name-only", "-z", current.head))
    elif lease.head_at_start is not None:
        raise LeaseError("OAH_LEASE_WORKTREE_INVALID", f"{worktree}: HEAD disappeared since lease start; "
                                                    "restore the recorded worktree history before finishing")
    commands.extend([(*diff, "--cached", "--"), (*diff, "--"),
                     ("ls-files", "--others", "--exclude-standard", "-z")])
    changed: list[str] = []
    for command in commands:
        result = run_git(worktree, *command)
        if result.returncode != 0:
            raise GitMissing(f"{GitMissing.code}: git failed while listing changes in {worktree}: "
                             f"{result.stderr.strip()}")
        for name in result.stdout.split("\0"):
            if name and name not in changed:
                changed.append(name)
    return changed


def _existed_at_start(root: pathlib.Path, lease: Lease, path: str) -> bool:
    if lease.head_at_start is None:
        return False
    return run_git(root, "cat-file", "-e", f"{lease.head_at_start}:{path}").returncode == 0


def _wiki_maintenance(root: pathlib.Path, lease: Lease, allow_missing: bool) -> tuple[set[str], dict | None]:
    """Wiki maintenance outputs exempt from the scope check, and the record for the finish event."""
    settings = WikiSettings.from_mapping(load_config(root).section("wiki", WIKI_DEFAULTS)).for_root(root)
    if not (root / settings.dir).is_dir():
        return set(), None
    try:
        from oah.wiki import verify_maintenance
    except ImportError:
        problem = "the wiki maintenance verifier is unavailable"
        if not allow_missing:
            raise LeaseError("OAH_WIKI_MAINTENANCE_MISSING",
                             f"{problem}. Install oah with wiki support and run oah wiki maintain --task "
                             f"{lease.task}, or pass --allow-missing-wiki-maintenance (recorded)") from None
        return set(), {"task": lease.task, "override": True, "problems": [problem]}
    check = verify_maintenance(root, lease.task, settings=settings)
    if not check.ok:
        if not allow_missing:
            problems = "; ".join(check.problems) or "wiki maintenance was not run for this task"
            raise LeaseError("OAH_WIKI_MAINTENANCE_MISSING",
                             f"{problems}. Run oah wiki maintain --task {lease.task} before you commit, run the gates "
                             "and ask for review; or pass --allow-missing-wiki-maintenance (recorded)")
        return set(), {"task": lease.task, "override": True, "problems": list(check.problems)}
    outputs = check.outputs
    if outputs is None:
        problem = f"the wiki maintenance record for {lease.task} has no readable outputs"
        if not allow_missing:
            raise LeaseError("OAH_WIKI_MAINTENANCE_MISSING",
                             f"{problem}. Run oah wiki maintain --task {lease.task} again before you commit, or pass "
                             "--allow-missing-wiki-maintenance (recorded)")
        return set(), {"task": lease.task, "override": True, "problems": [problem]}
    exempt = []
    fixed = maintenance_output_paths(settings)
    for path, digest in outputs:
        if path not in maintenance_output_paths(settings, archives=(path.rsplit("/", 1)[-1],)):
            continue
        try:
            actual = hashlib.sha256((root / pathlib.PurePosixPath(path)).read_bytes()).hexdigest()
        except OSError:
            continue
        if actual != digest or (path not in fixed and _existed_at_start(root, lease, path)):
            continue
        exempt.append([path, digest])
    return {path for path, _ in exempt}, {"task": lease.task, "exempt": exempt}


def _read_return(raw: str) -> str:
    try:
        data = sys.stdin.buffer.read() if raw == "-" else pathlib.Path(raw).read_bytes()
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise LeaseError(intent.RETURN_INVALID, f"cannot read the return {raw}: {exc}; save it as UTF-8 text and "
                         "pass it with --return FILE, or --return - to read stdin") from None


def _current_binding(lease: Lease) -> intent.Binding | None:
    """The lease's checked binding, or a refusal if its source is no longer current."""
    try:
        binding = lease.binding()
    except ValueError as exc:
        raise StateCorrupt(f"{StateCorrupt.code}: the lease for {lease.task} holds {exc}") from None
    if binding is None:
        return None
    state, reason = intent.explain_binding(binding, pathlib.Path(lease.worktree))
    if state != "current":
        raise LeaseError("OAH_INTENT_NOT_CURRENT",
                         f"the bound intent is {state}: {reason}. Restore {binding.source} as it was bound, or raise "
                         f"its Revision, accept it and rebind with oah start --task {lease.task} --agent {lease.agent} "
                         f"--packet {binding.source}; then run oah finish again")
    return binding


def _check_intent(args: argparse.Namespace, lease: Lease) -> dict | None:
    """For a bound lease: refuse a binding that is not current or an invalid return; else the event record."""
    binding = _current_binding(lease)
    if binding is None:
        return None
    shape = "one 'AC<n>: met|unmet|deferred — <evidence>' line per success condition"
    if args.return_file is None:
        raise LeaseError(intent.RETURN_INVALID,
                         f"this lease is bound to {binding.line()}; pass your return with --return FILE (or --return - "
                         f"for stdin). It needs that INTENT line and {shape}")
    text = _read_return(args.return_file)
    _current_binding(lease)  # stdin may have blocked while the source changed
    problems = intent.check_return(text, binding)
    if problems:
        raise LeaseError(intent.RETURN_INVALID, "; ".join(problem.message for problem in problems) +
                         ". Fix the return and run oah finish again")
    statuses = {}
    for line in intent.normalize_newlines(text).split("\n"):
        match = _AC_STATUS.match(line.strip())
        if match:
            statuses[match.group(1)] = match.group(2)
    return {"line": binding.line(), "acs": {ac: statuses[ac] for ac in binding.acs}}


def _packet_section(lease: Lease, record) -> None:
    """The bound packet must still name the task's design, at a revision the task follows or carried to."""
    from oah.design.binding import SOURCE_MISMATCH_CODE, SectionInvalid, parse_section
    from oah.design.model import DesignError

    binding = lease.binding()
    if binding is None:
        return
    try:
        section = parse_section(intent.read_source(intent.source_path(binding, pathlib.Path(lease.worktree))))
    except (intent.SourceError, SectionInvalid):
        section = None
    if section is None or not record.same_design(section) or not record.holds(section.binding()):
        named = section.line() if section is not None else "no valid design"
        raise DesignError(SOURCE_MISMATCH_CODE, f"the bound packet {binding.source} names {named}, but task "
                          f"{lease.task} follows {record.binding.line()}; restore the packet's Design section, or "
                          f"rewrite it with oah packet design {lease.task} and rebind with oah start --packet")


def _wiki_settings(root: pathlib.Path) -> WikiSettings:
    return WikiSettings.from_mapping(load_config(root).section("wiki", WIKI_DEFAULTS)).for_root(root)


def _design_outputs(lease: Lease, store: Store, record, changed: list[str]) -> dict:
    """B2's generated-content equivalence report for this lease's changed design outputs."""
    from oah.design import binding as design

    view = design.task_view(store.dir, lease.task, lease=lease)
    epoch = view.epoch or {}
    baseline = epoch.get("output_baseline") if epoch.get("lease_id") == lease.id else None
    root = pathlib.Path(lease.worktree)
    return design.generated_outputs(root, _wiki_settings(root), record, changed, baseline)


def _finish_design(lease: Lease, store: Store):
    """Judge the design before the gates (refusing early), and return the check `Store.finish` repeats under its
    lock, at the control point itself; None for an origin-free (legacy) task."""
    root = pathlib.Path(lease.worktree)
    source = lease.binding().source if lease.intent is not None else f".oah/tasks/{lease.task}/PACKET.md"
    try:
        text = intent.read_source(root / source)
    except intent.SourceError:
        text = None
    judged = lease_design(lease, store.dir, packet_text=text, source=source)
    judged.require("finish", task=lease.task, agent=lease.agent)
    if judged.state == "unbound":
        return None
    _packet_section(lease, judged.task)

    def locked(live: Lease, recompute=None, preliminary=None) -> dict:
        from oah.design.binding import CHANGED_CODE
        from oah.design.model import DesignError

        again = lease_design(live, store.dir)
        if again.state == "unbound":  # a design-bound lease cannot turn legacy between the two checks
            from oah.design import binding as design

            again = design.judge(None, root=pathlib.Path(live.worktree),
                                 found=design.Origin("lease", "its lease recorded a design"))
        again.require("finish", task=live.task, agent=live.agent)
        if again.task != judged.task:
            raise DesignError(CHANGED_CODE, f"the design record of task {live.task} changed during finish; the lease "
                              "stays open. Run oah finish again")
        outputs = recompute(live) if recompute is not None else None
        if outputs is not None and preliminary is not None and outputs != preliminary:
            raise DesignError(CHANGED_CODE, f"the design outputs of task {live.task} changed during finish (a file was "
                              "added, changed or became unreadable); the lease stays open. Run oah finish again")
        report = {"version": 1, "state": again.state, "fingerprint": again.fingerprint(),
                  "evidence_key": again.task.key}
        if outputs is not None:
            report["outputs"] = outputs
        return {"design_report": report}

    return locked


def _transport_guard(lease: Lease, store: Store):
    """SC4-P4's scope-independent transport guard: every retained origin that can intersect this worktree is
    replayed and re-checked before gates, generated outputs and ordinary scope. A task with none passes as before."""
    from oah.design import task_context
    from oah.design.binding import repo_identity
    from oah.design.model import DesignError

    root = pathlib.Path(lease.worktree)
    record = lease.design if isinstance(lease.design, dict) else None
    current = None
    if record is not None and isinstance(record.get("task_identity"), dict):
        current = {"task_instance_id": record["task_identity"].get("task_instance_id"),
                   "task_basis_digest": record.get("basis")}
    try:
        repo = repo_identity(root)
    except DesignError:
        repo = None
    result = task_context.completion_guard(store.dir, lease.task, root=root, repo=repo, current=current)
    if not result.ok:
        raise DesignError(result.code, f"task {lease.task}: {result.reason}; the lease stays open")
    return result


def _finish(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    _refuse_worker_lease()
    lease = store.inspect(args.task)
    intent_report = _check_intent(args, lease)
    design_check = _finish_design(lease, store)
    transport = _transport_guard(lease, store)
    # The task's ledgers and summary are gate artifacts written by oah and unlazy, not changes to the scope.
    artifacts = {f".oah/tasks/{lease.task}/{LEDGER_NAME}", f".oah/tasks/{lease.task}/{SUMMARY_NAME}", *lease.gates}
    if intent_report is not None:
        artifacts.add(lease.binding().source)  # the bound packet itself, checked current just above
    root = pathlib.Path(lease.worktree)
    wiki_exempt, wiki_report = _wiki_maintenance(root, lease, args.allow_missing_wiki_maintenance)
    changed = [name for name in _changed_files(lease, info) if name not in artifacts and name not in wiki_exempt]
    if transport.exempt:
        # Only exact retained owned DATA leaves (ACK_RETAINED present-unchanged) leave ordinary change accounting.
        retained = set(transport.exempt)
        changed = [name for name in changed if name not in retained]
    if design_check is not None:
        # Only exact generated outputs of the bound design leave ordinary scope; the design state check above is
        # independent of scope and of --allow-out-of-scope. Store.finish recomputes all of it under its lock.
        record = lease.design_record()
        outputs = _design_outputs(lease, store, record, changed)
        exempt = {item["path"] for item in outputs["verified"]}
        changed = [name for name in changed if name not in exempt]
        first_check = design_check

        def recompute(live: Lease) -> dict:
            names = [name for name in _changed_files(live, info) if name not in artifacts and name not in wiki_exempt]
            return _design_outputs(live, store, live.design_record(), names)

        def design_check(live: Lease, _first=first_check, _outputs=outputs) -> dict:
            return _first(live, recompute, _outputs)
    if transport.origins:
        inner = design_check

        def design_check(live: Lease, _inner=inner, _first=transport) -> dict:
            from oah.design.binding import CHANGED_CODE
            from oah.design.model import DesignError

            again = _transport_guard(live, store)  # repeated at the locked control point
            if again.exempt != _first.exempt:
                raise DesignError(CHANGED_CODE, f"the design transport accounting of task {live.task} changed during "
                                  "finish; the lease stays open. Run oah finish again")
            fields = dict((_inner(live) if _inner is not None else None) or {})
            if fields:  # design-bound only: an origin-free lease keeps its exact legacy finish event
                fields["transport_report"] = {"version": 1, "origins": [item["origin"] for item in again.origins
                                                                          if item["task"] == live.task],
                                              "exempt": list(again.exempt)}
            return fields
    settings = load_config(root).section("gates", GATE_DEFAULTS)
    # unlazy is looked up (and its pin verified) only when a ledger governs this task.
    ledgers = guard.resolve_ledgers(root, lease.task, lease.gates)
    tools = find_tools(root, os.environ, pathlib.Path.home()) if ledgers else GateTools(None, None, "no ledger")
    report = guard.evaluate(root, lease.task, lease.gates, store.dir, tools=tools,
                            allow_structural=args.allow_structural, env_names=settings["input_env"],
                            commit_ledgers=settings["commit_ledgers"])
    if report.summary_text is not None:
        guard.verify_summary(root, lease.task, report.summary_text)  # finish writes nothing tracked
    lease, beyond = store.finish(task=args.task, agent=args.agent, lease_id=lease.id,
                                 summary=args.summary, changed=changed,
                                 allow_out_of_scope=args.allow_out_of_scope, gate_report=report.event(len(ledgers)),
                                 wiki_report=wiki_report, intent_report=intent_report, checked_binding=lease.binding(),
                                 checked_generation=lease.generation, design_check=design_check)
    extra = f" out_of_scope={','.join(beyond)}" if beyond else ""
    if intent_report is not None:
        extra += f" intent={intent_report['line'].split(' ')[1]}"
    print(f"OAH_LEASE_FINISHED task={lease.task} agent={lease.agent} changed={len(changed)} "
          f"gates={report.outcome}{extra}")
    if report.handoff:
        print(f"OAH_GATES_HANDOFF_REQUIRED {','.join(report.handoff)}: these gates were abandoned, not met; name "
              "them in your report and hand them to the owner")
    if report.outcome == "structural":
        print(f"OAH_GATES_UNVERIFIED: finished on the ledger's boxes alone ({tools.reason}); recorded as unverified")
    if wiki_report and wiki_report.get("override"):
        print("OAH_WIKI_MAINTENANCE_SKIPPED: finished without oah wiki maintain (recorded)")
    open_acs = [f"{ac}={state}" for ac, state in (intent_report or {}).get("acs", {}).items() if state != "met"]
    if open_acs:
        print(f"OAH_INTENT_ACS_OPEN {','.join(open_acs)}: name these conditions in your report; they are not met")
    from oah import toolupdate  # loaded only here; it never raises, waits or changes this exit code

    toolupdate.start_after_finish(root, store.dir, os.environ)
    return EXIT_OK


def _release(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    lease = store.release(task=args.task, agent=args.agent, reason=args.reason, force=args.force)
    print(f"OAH_LEASE_RELEASED task={lease.task} holder={lease.agent} by={args.agent}")
    return EXIT_OK


def _log(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    store.log(task=args.task, agent=args.agent, message=args.message)
    print(f"OAH_NOTE_LOGGED task={args.task}")
    return EXIT_OK


HANDLERS = {"start": _start, "renew": _renew, "status": _status, "finish": _finish, "release": _release,
            "log": _log}
