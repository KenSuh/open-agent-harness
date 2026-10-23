# 패킷 명령 — oah packet new/check 의 인자, 저장소 루트 찾기, 결과 출력과 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import pathlib
import sys

from oah import packet
from oah.paths import GitMissing, NotARepo, repo_info

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("packet",)


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("packet", help="create or check a task packet (.oah/tasks/<task>/PACKET.md)")
    actions = parser.add_subparsers(dest="packet_command", metavar="<action>", required=True)
    new = actions.add_parser("new", help="scaffold a draft packet for a task")
    new.add_argument("task")
    check = actions.add_parser("check", help="check a packet's intent, sections, scope and gate ledger")
    target = check.add_mutually_exclusive_group(required=True)
    target.add_argument("task", nargs="?", help="the task whose packet to check")
    target.add_argument("--file", help="check this packet file instead")
    check.add_argument("--allow-draft", action="store_true", help="check the structure of a draft packet")


def run(args: argparse.Namespace) -> int:
    try:
        if args.packet_command == "new":
            return _new(args)
        return _check(args)
    except packet.PacketError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except (NotARepo, GitMissing) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(f"OAH_PACKET_IO: {exc}. Check the folder's permissions and free disk space, then retry.",
              file=sys.stderr)
        return EXIT_ENVIRONMENT


def display(path: pathlib.Path, root: pathlib.Path | None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return str(path)


def _new(args: argparse.Namespace) -> int:
    root = repo_info(pathlib.Path.cwd()).root
    path = packet.create(root, args.task)
    print(f"OAH_PACKET_CREATED {display(path, root)}")
    print("Next: describe the problem, the outcome and checkable success conditions, list the Scope paths, "
          f"then change 'Status: draft' to 'Status: accepted' and run: oah packet check {args.task}")
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    if args.file:
        path = pathlib.Path(args.file).absolute()
        task = packet.task_of(path)
        try:
            root = repo_info(path.parent if path.parent.is_dir() else pathlib.Path.cwd()).root
        except (NotARepo, GitMissing):
            root = None
    else:
        root = repo_info(pathlib.Path.cwd()).root
        task = args.task
        path = packet.packet_path(root, task)
    result = packet.check(path, task=task, require_accepted=not args.allow_draft)
    for problem in result.problems:
        print(f"{problem.level} {problem}")
    shown = display(path, root)
    if any(p.code == "OAH_INTENT_SOURCE_MISSING" for p in result.errors) and task is not None:
        print(f"Create it with: oah packet new {task}")
    if result.errors:
        print(f"OAH_PACKET_INVALID {shown} errors={len(result.errors)} warnings={len(result.warnings)}")
        return EXIT_REFUSED
    parsed = result.intent
    print(f"OAH_PACKET_OK {shown} intent={parsed.id}@r{parsed.revision} status={parsed.status} "
          f"acs={len(parsed.acs)} scope={len(result.scope)} warnings={len(result.warnings)}")
    return EXIT_OK
