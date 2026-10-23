# 의도 명령 — oah intent show/check 로 패킷 의도의 신원·다이제스트·결속 상태를 보이고, ID 변경과 개정 없는 목표 변경을 잡는다.
"""`oah intent show|check (<task> | --file PATH)`.

Both print the binding state and the intent's identity. The binding comes from the task's lease when
the lease record carries an `intent` field (lane P4b adds it); otherwise the state is `unbound`.

`check` also compares the packet with earlier statements of the same goal: the lease binding, and
the packet's version at `HEAD` when git tracks it. A different `Intent-ID` is `OAH_INTENT_ID_CHANGED`.
A changed section of an accepted intent with the same `Revision` is `OAH_INTENT_REVISION_REQUIRED`,
and a lower `Revision` is `OAH_INTENT_REVISION_INVALID`. A draft, or a `missing` or `stale` binding,
also fails the check.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import NamedTuple

from oah import intent, packet
from oah.atomic import StateCorrupt, StorageError
from oah.intent import Binding, IntentInvalid, Problem, SourceError
from oah.journal import PendingTransaction
from oah.lease import Store
from oah.lockfile import LockTimeout
from oah.packet_cli import display
from oah.paths import GitMissing, NotARepo, RepoInfo, repo_info, run_git, state_dir

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("intent",)


class Earlier(NamedTuple):
    label: str
    id: str
    revision: int
    digest: str
    accepted: bool


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("intent", help="show or check a task's intent and its binding state")
    actions = parser.add_subparsers(dest="intent_command", metavar="<action>", required=True)
    for name, text in (("show", "print the intent's identity and binding state"),
                       ("check", "fail unless the intent is accepted, unchanged in identity and current")):
        action = actions.add_parser(name, help=text)
        target = action.add_mutually_exclusive_group(required=True)
        target.add_argument("task", nargs="?", help="the task whose packet to read")
        target.add_argument("--file", help="read this packet file instead")


def run(args: argparse.Namespace) -> int:
    try:
        return _run(args)
    except packet.PacketError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except (NotARepo, GitMissing, LockTimeout, StateCorrupt, PendingTransaction, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT


def lease_binding(info: RepoInfo, task: str | None) -> tuple[Binding | None, str]:
    """The intent bound to the task's lease, if any, with a reason when there is none."""
    if task is None:
        return None, "no task is known for this file, so no lease binding applies"
    for lease, _stale in Store(state_dir(info, os.environ)).snapshot():
        if lease.task != task:
            continue
        data = getattr(lease, "intent", None)
        if data is None:
            return None, f"the lease for {task} has no intent binding (oah start --packet binds one)"
        try:
            return Binding.from_json(data), ""
        except ValueError as exc:
            raise StateCorrupt(f"{StateCorrupt.code}: the lease for {task} holds {exc}") from exc
    return None, f"no lease for task {task}"


def _head_version(root: pathlib.Path, path: pathlib.Path) -> intent.Intent | None:
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    shown = run_git(root, "show", f"HEAD:{rel}")
    if shown.returncode != 0:
        return None
    try:
        return intent.parse(shown.stdout)
    except IntentInvalid:
        return None


def _compare(current: intent.Intent, earlier: Earlier) -> list[Problem]:
    if current.id != earlier.id:
        return [Problem("OAH_INTENT_ID_CHANGED",
                        f"Intent-ID is {current.id}, but {earlier.label} says {earlier.id}. Keep the ID: a changed "
                        f"goal raises Revision of {earlier.id}")]
    if current.revision < earlier.revision:
        return [Problem("OAH_INTENT_REVISION_INVALID",
                        f"Revision {current.revision} is lower than {earlier.revision} in {earlier.label}; "
                        f"revisions only go up")]
    if current.revision == earlier.revision and current.digest != earlier.digest and earlier.accepted:
        return [Problem("OAH_INTENT_REVISION_REQUIRED",
                        f"the intent changed since {earlier.label}, but it is still revision {earlier.revision}. "
                        f"Set Revision: {earlier.revision + 1}, then Status: accepted again once you approve it")]
    return []


def _run(args: argparse.Namespace) -> int:
    info: RepoInfo | None
    if args.file:
        path = pathlib.Path(args.file).absolute()
        task = packet.task_of(path)
        try:
            info = repo_info(path.parent if path.parent.is_dir() else pathlib.Path.cwd())
        except NotARepo:
            info = None
    else:
        info = repo_info(pathlib.Path.cwd())
        task = args.task
        path = packet.packet_path(info.root, task)
    root = info.root if info is not None else None
    binding, unbound_reason = lease_binding(info, task) if info is not None else (None, "not in a repository")
    if binding is None:
        state, reason = "unbound", unbound_reason
    else:
        state, reason = intent.explain_binding(binding, root)

    problems: list[Problem] = []
    current: intent.Intent | None = None
    try:
        current = intent.parse(intent.read_source(path))
    except SourceError as exc:
        problems.append(Problem(exc.code, str(exc).split(": ", 1)[1]))
    except IntentInvalid as exc:
        problems.extend(exc.problems)

    print(f"state={state} ({reason})")
    if current is not None:
        print(f"intent={current.id} revision={current.revision} status={current.status}")
        print(f"digest={current.digest}")
        print(f"source={display(path, root)}")
        print(f"acs={','.join(current.acs)}")
        print(current.line())
    if binding is not None:
        print(f"bound={binding.line()} source={binding.source}")

    if args.intent_command == "check" and current is not None:
        if current.status != "accepted":
            problems.append(Problem("OAH_INTENT_NOT_ACCEPTED",
                                    f"Status is {current.status}; change it to accepted once the intent says "
                                    "what you want"))
        if task is not None and current.id != task:
            problems.append(Problem("OAH_INTENT_ID_CHANGED",
                                    f"Intent-ID is {current.id} but the task is {task}. Keep Intent-ID: {task}; a "
                                    "changed goal raises Revision instead"))
        earlier = []
        if binding is not None:
            earlier.append(Earlier("the lease binding", binding.id, binding.revision, binding.digest, True))
        head = _head_version(root, path) if root is not None else None
        if head is not None:
            earlier.append(Earlier("the version at HEAD", head.id, head.revision, head.digest,
                                   head.status == "accepted"))
        for version in earlier:
            for problem in _compare(current, version):
                if problem not in problems:
                    problems.append(problem)
        if state in ("missing", "stale"):
            problems.append(Problem("OAH_INTENT_NOT_CURRENT",
                                    f"the bound intent is {state}: {reason}. Restore the bound packet, or bind the "
                                    "new revision with oah start --packet"))

    for problem in problems:
        print(f"error {problem}")
    if problems:
        return EXIT_REFUSED
    if args.intent_command == "check":
        print(f"OAH_INTENT_OK {current.id}@r{current.revision} state={state}")
    return EXIT_OK
