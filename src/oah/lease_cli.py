# 리스 명령 — oah start/renew/status/finish/release/log 의 인자, 저장소·상태 경로 찾기, 출력 형식, 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from oah.atomic import StateCorrupt
from oah.lease import DEFAULT_TTL_HOURS, Lease, LeaseError, Store
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, RepoInfo, repo_info, run_git, state_dir
from oah.scope import ScopeError, normalize_scope

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("start", "renew", "status", "finish", "release", "log")


def register(commands: argparse._SubParsersAction) -> None:
    start = commands.add_parser("start", help="take a lease on paths before editing them")
    _identity(start)
    start.add_argument("--scope", action="append", required=True, help="a path, or 'repo'; repeat for more")
    start.add_argument("--note", default="")
    start.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    start.add_argument("--json", action="store_true", help="print the lease as JSON")

    renew = commands.add_parser("renew", help="extend your lease, optionally with a new scope")
    _identity(renew)
    renew.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    renew.add_argument("--scope", action="append", help="replace the scope; repeat for more")

    status = commands.add_parser("status", help="show active and stale leases (reads only)")
    status.add_argument("--json", action="store_true")

    finish = commands.add_parser("finish", help="check the changes against the scope and close your lease")
    _identity(finish)
    finish.add_argument("--summary", required=True)
    finish.add_argument("--allow-out-of-scope", action="store_true",
                        help="finish even though files outside the scope changed (recorded)")

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
    try:
        info = repo_info(pathlib.Path.cwd())
        store = Store(state_dir(info, os.environ))
        return HANDLERS[args.command](args, info, store)
    except (LeaseError, ScopeError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except (NotARepo, GitMissing, LockTimeout, StateCorrupt) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT


def _summary(lease: Lease) -> str:
    return f"task={lease.task} agent={lease.agent} scope={','.join(lease.scope)} expires={lease.expires_at}"


def _start(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    scope = normalize_scope(args.scope, info.root)
    lease = store.start(task=args.task, agent=args.agent, scope=scope, note=args.note, ttl_hours=args.ttl_hours,
                        info=info)
    print(json.dumps(lease.to_json(), indent=2, ensure_ascii=True) if args.json
          else f"OAH_LEASE_STARTED {_summary(lease)}")
    return EXIT_OK


def _renew(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    scope = normalize_scope(args.scope, info.root) if args.scope else None
    lease = store.renew(task=args.task, agent=args.agent, ttl_hours=args.ttl_hours, scope=scope)
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
    where = worktree if worktree.is_dir() else info.root
    if lease.head_at_start:
        tracked = run_git(where, "diff", "--name-only", "-z", lease.head_at_start)
    else:
        tracked = run_git(where, "ls-files", "-z")
    untracked = run_git(where, "ls-files", "--others", "--exclude-standard", "-z")
    for result in (tracked, untracked):
        if result.returncode != 0:
            raise GitMissing(f"{GitMissing.code}: git failed while listing changes: {result.stderr.strip()}")
    changed: list[str] = []
    for name in (tracked.stdout + untracked.stdout).split("\0"):
        if name and name not in changed:
            changed.append(name)
    return changed


def _finish(args: argparse.Namespace, info: RepoInfo, store: Store) -> int:
    lease = next((lease for lease, _ in store.snapshot() if lease.task == args.task), None)
    if lease is None:
        raise LeaseError("OAH_LEASE_NOT_FOUND", f"no lease for task {args.task}")
    changed = _changed_files(lease, info)
    lease, beyond = store.finish(task=args.task, agent=args.agent, summary=args.summary, changed=changed,
                                 allow_out_of_scope=args.allow_out_of_scope)
    extra = f" out_of_scope={','.join(beyond)}" if beyond else ""
    print(f"OAH_LEASE_FINISHED task={lease.task} agent={lease.agent} changed={len(changed)}{extra}")
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
