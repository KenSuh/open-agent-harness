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
from oah.lease import DEFAULT_TTL_HOURS, Lease, LeaseError, Store
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, RepoInfo, repo_info, run_git, state_dir
from oah.scope import ScopeError, normalize_scope
from oah.unlazy import UnlazyError

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69
GATE_ENVIRONMENT_CODES = {"OAH_UNLAZY_FAILED"}
# The only wiki files `oah wiki maintain` may change outside a task's scope (a log archive only when new).
WIKI_MAINTENANCE_OUTPUTS = re.compile(r"docs/wiki/(?:log|index)\.md|docs/wiki/log_archive_[0-9]{2}\.md")
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


def _bindable(args: argparse.Namespace, info: RepoInfo) -> tuple[intent.Binding, tuple[str, ...], list[str]]:
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
    return result.intent.bind(source), normalize_scope([*result.scope, *explicit], info.root), warnings


def _start(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    if not args.scope and not args.packet:
        print("OAH_USAGE: oah start needs --scope PATH (repeat for more, or 'repo') or --packet PATH",
              file=sys.stderr)
        return EXIT_USAGE
    gates = _ledger_paths(args.gates, info)
    if args.packet:
        return _start_packet(args, info, store, gates)
    scope = normalize_scope(args.scope, info.root)
    lease = store.start(task=args.task, agent=args.agent, scope=scope, note=args.note, ttl_hours=args.ttl_hours,
                        info=info, gates=gates)
    print(json.dumps(lease.to_json(), indent=2, ensure_ascii=True) if args.json
          else f"OAH_LEASE_STARTED {_summary(lease)}")
    return EXIT_OK


def _start_packet(args: argparse.Namespace, info: RepoInfo, store: Store, gates: list[str]) -> int:
    binding, scope, warnings = _bindable(args, info)
    named = f"intent={binding.id}@r{binding.revision} {binding.digest}"
    try:
        lease = store.start(task=args.task, agent=args.agent, scope=scope, note=args.note, ttl_hours=args.ttl_hours,
                            info=info, gates=gates, intent=binding)
        line = f"OAH_LEASE_STARTED {_summary(lease)} {named}"
    except LeaseError as exc:
        if exc.code != "OAH_LEASE_EXISTS":
            raise
        lease, action = store.rebind(task=args.task, agent=args.agent, binding=binding, ttl_hours=args.ttl_hours,
                                     scope=scope)
        word = {"bound": "OAH_INTENT_BOUND", "rebound": "OAH_INTENT_REBOUND", "unchanged": "OAH_INTENT_UNCHANGED"}
        line = f"{word[action]} task={lease.task} {named} expires={lease.expires_at}"
    for warning in warnings:
        print(warning, file=sys.stderr)
    print(json.dumps(lease.to_json(), indent=2, ensure_ascii=True) if args.json else line)
    return EXIT_OK


def _intent_state(lease: Lease) -> tuple[str, str]:
    """The lease's binding state and reason, judged against the lease's own worktree."""
    try:
        binding = lease.binding()
    except ValueError as exc:
        return "stale", str(exc)
    return intent.explain_binding(binding, pathlib.Path(lease.worktree))


def _renew(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    scope = normalize_scope(args.scope, info.root) if args.scope else None
    gates = _ledger_paths(args.gates, info) or None
    lease = store.renew(task=args.task, agent=args.agent, ttl_hours=args.ttl_hours, scope=scope, gates=gates)
    print(f"OAH_LEASE_RENEWED {_summary(lease)}")
    return EXIT_OK


def _status(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    entries = store.snapshot()
    states = [_intent_state(lease) for lease, _stale in entries]
    if args.json:
        print(json.dumps({"leases": [dict(lease.to_json(), stale=stale, intent_state=state, intent_reason=reason)
                                     for (lease, stale), (state, reason) in zip(entries, states)]},
                         indent=2, ensure_ascii=True))
        return EXIT_OK
    if not entries:
        print("no leases")
        return EXIT_OK
    for (lease, stale), (intent_state, _reason) in zip(entries, states):
        state = "STALE" if stale else "active"
        print(f"{state:6}  {_summary(lease)}  intent={intent_state}  worktree={lease.worktree}")
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
    if not (root / "docs" / "wiki").is_dir():
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
    check = verify_maintenance(root, lease.task)
    if not check.ok:
        if not allow_missing:
            problems = "; ".join(check.problems) or "wiki maintenance was not run for this task"
            raise LeaseError("OAH_WIKI_MAINTENANCE_MISSING",
                             f"{problems}. Run oah wiki maintain --task {lease.task} before you commit, run the gates "
                             "and ask for review; or pass --allow-missing-wiki-maintenance (recorded)")
        return set(), {"task": lease.task, "override": True, "problems": list(check.problems)}
    exempt = []
    for path, digest in check.outputs:
        if not (isinstance(path, str) and WIKI_MAINTENANCE_OUTPUTS.fullmatch(path)):
            continue
        try:
            actual = hashlib.sha256((root / pathlib.PurePosixPath(path)).read_bytes()).hexdigest()
        except OSError:
            continue
        if actual != digest or (path.startswith("docs/wiki/log_archive_") and _existed_at_start(root, lease, path)):
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


def _check_intent(args: argparse.Namespace, lease: Lease) -> dict | None:
    """For a bound lease: refuse a binding that is not current or an invalid return; else the event record."""
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
    shape = "one 'AC<n>: met|unmet|deferred — <evidence>' line per success condition"
    if args.return_file is None:
        raise LeaseError(intent.RETURN_INVALID,
                         f"this lease is bound to {binding.line()}; pass your return with --return FILE (or --return - "
                         f"for stdin). It needs that INTENT line and {shape}")
    text = _read_return(args.return_file)
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


def _finish(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    lease = store.inspect(args.task)
    intent_report = _check_intent(args, lease)
    # The task's ledgers and summary are gate artifacts written by oah and unlazy, not changes to the scope.
    artifacts = {f".oah/tasks/{lease.task}/{LEDGER_NAME}", f".oah/tasks/{lease.task}/{SUMMARY_NAME}", *lease.gates}
    if intent_report is not None:
        artifacts.add(lease.binding().source)  # the bound packet itself, checked current just above
    root = pathlib.Path(lease.worktree)
    wiki_exempt, wiki_report = _wiki_maintenance(root, lease, args.allow_missing_wiki_maintenance)
    changed = [name for name in _changed_files(lease, info) if name not in artifacts and name not in wiki_exempt]
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
                                 wiki_report=wiki_report, intent_report=intent_report)
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
