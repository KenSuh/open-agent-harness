# 리스 명령 — oah start/renew/status/finish/release/log 의 인자, 저장소·상태 경로 찾기, 출력 형식, 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from oah.atomic import StateCorrupt, StorageError, write_text_atomic
from oah.config import ConfigError, load_config
from oah.gates import guard
from oah.gates.check import GateRefused
from oah.gates.context import DEFAULTS as GATE_DEFAULTS, LEDGER_NAME, SUMMARY_NAME, find_tools, task_dir
from oah.gates.summary import SummaryRejected
from oah.journal import PendingTransaction
from oah.lease import DEFAULT_TTL_HOURS, Lease, LeaseError, Store
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, RepoInfo, repo_info, run_git, state_dir
from oah.scope import ScopeError, normalize_scope
from oah.unlazy import UnlazyError

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
GATE_ENVIRONMENT_CODES = {"OAH_UNLAZY_FAILED"}
COMMANDS = ("start", "renew", "status", "finish", "release", "log")


def register(commands: argparse._SubParsersAction) -> None:
    start = commands.add_parser("start", help="take a lease on paths before editing them")
    _identity(start)
    start.add_argument("--scope", action="append", required=True, help="a path, or 'repo'; repeat for more")
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


def _start(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    scope = normalize_scope(args.scope, info.root)
    gates = _ledger_paths(args.gates, info)
    lease = store.start(task=args.task, agent=args.agent, scope=scope, note=args.note, ttl_hours=args.ttl_hours,
                        info=info, gates=gates)
    print(json.dumps(lease.to_json(), indent=2, ensure_ascii=True) if args.json
          else f"OAH_LEASE_STARTED {_summary(lease)}")
    return EXIT_OK


def _renew(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    scope = normalize_scope(args.scope, info.root) if args.scope else None
    gates = _ledger_paths(args.gates, info) or None
    lease = store.renew(task=args.task, agent=args.agent, ttl_hours=args.ttl_hours, scope=scope, gates=gates)
    print(f"OAH_LEASE_RENEWED {_summary(lease)}")
    return EXIT_OK


def _status(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    entries = store.snapshot()
    if args.json:
        print(json.dumps({"leases": [dict(lease.to_json(), stale=stale) for lease, stale in entries]},
                         indent=2, ensure_ascii=True))
        return EXIT_OK
    if not entries:
        print("no leases")
        return EXIT_OK
    for lease, stale in entries:
        state = "STALE" if stale else "active"
        print(f"{state:6}  {_summary(lease)}  worktree={lease.worktree}")
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


def _finish(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    lease = store.inspect(args.task)
    # The task's ledgers and summary are gate artifacts written by oah and unlazy, not changes to the scope.
    artifacts = {f".oah/tasks/{lease.task}/{LEDGER_NAME}", f".oah/tasks/{lease.task}/{SUMMARY_NAME}", *lease.gates}
    changed = [name for name in _changed_files(lease, info) if name not in artifacts]
    root = pathlib.Path(lease.worktree)
    settings = load_config(root).section("gates", GATE_DEFAULTS)
    tools = find_tools(root, os.environ, pathlib.Path.home())
    report = guard.evaluate(root, lease.task, lease.gates, store.dir, tools=tools,
                            allow_structural=args.allow_structural, env_names=settings["input_env"],
                            commit_ledgers=settings["commit_ledgers"])
    if report.summary_text is not None:
        write_text_atomic(task_dir(root, lease.task) / SUMMARY_NAME, report.summary_text)
    ledgers = len(guard.resolve_ledgers(root, lease.task, lease.gates)) if report.outcome != "none" else 0
    lease, beyond = store.finish(task=args.task, agent=args.agent, lease_id=lease.id,
                                 summary=args.summary, changed=changed,
                                 allow_out_of_scope=args.allow_out_of_scope, gate_report=report.event(ledgers))
    extra = f" out_of_scope={','.join(beyond)}" if beyond else ""
    print(f"OAH_LEASE_FINISHED task={lease.task} agent={lease.agent} changed={len(changed)} "
          f"gates={report.outcome}{extra}")
    if report.handoff:
        print(f"OAH_GATES_HANDOFF_REQUIRED {','.join(report.handoff)}: these gates were abandoned, not met; name "
              "them in your report and hand them to the owner")
    if report.outcome == "structural":
        print(f"OAH_GATES_UNVERIFIED: finished on the ledger's boxes alone ({tools.reason}); recorded as unverified")
    if report.summary_text is not None:
        print(f"Commit .oah/tasks/{lease.task}/{SUMMARY_NAME} with your work.")
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
