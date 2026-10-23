# 훅 설치 명령 — oah hooks install/uninstall/status 의 인자, 변경 미리보기 출력, 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import pathlib
import sys

from oah import hooks
from oah.atomic import StorageError
from oah.paths import GitMissing, NotARepo, repo_info

COMMANDS = ("hooks",)
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69


def register(commands: argparse._SubParsersAction) -> None:
    group = commands.add_parser("hooks", help="install, remove or list oah's opt-in Claude Code and git hooks")
    actions = group.add_subparsers(dest="hooks_command", metavar="<action>", required=True)
    for name, text in (("install", "add hooks (shows the change first)"), ("uninstall", "remove oah's hooks only")):
        action = actions.add_parser(name, help=text)
        action.add_argument("kinds", nargs="+", metavar="kind", help=f"one or more of: {', '.join(hooks.KINDS)}")
        action.add_argument("--dry-run", action="store_true", help="show the change without writing it")
        action.add_argument("--shared", action="store_true",
                            help="use .claude/settings.json (committed) instead of .claude/settings.local.json")
        if name == "install":
            action.add_argument("--force", action="store_true",
                                help="replace a git hook oah did not write (it is backed up and restored on uninstall)")
    status = actions.add_parser("status", help="show which hooks are installed")
    status.add_argument("--shared", action="store_true", help="read .claude/settings.json instead")


def _relative(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def run(args: argparse.Namespace) -> int:
    try:
        root = repo_info(pathlib.Path.cwd()).root
        if args.hooks_command == "status":
            for row in hooks.status(root, shared=args.shared):
                print(f"{row.kind:14} {row.state:9} {_relative(row.path, root)}")
            return EXIT_OK
        unknown = [kind for kind in args.kinds if kind not in hooks.KINDS]
        if unknown:
            print(f"OAH_HOOK_KIND_UNKNOWN: {', '.join(unknown)}; choose from {', '.join(hooks.KINDS)}", file=sys.stderr)
            return EXIT_USAGE
        for kind in args.kinds:
            if args.hooks_command == "install":
                change = hooks.install(kind, root, dry_run=args.dry_run, shared=args.shared, force=args.force)
            else:
                change = hooks.uninstall(kind, root, dry_run=args.dry_run, shared=args.shared)
            if change.diff:
                sys.stdout.write(change.diff)
            where = _relative(change.path, root)
            if change.action == "unchanged":
                state = "already installed" if args.hooks_command == "install" else "not installed"
                print(f"OAH_HOOK_UNCHANGED kind={kind} path={where} ({state})")
            elif args.dry_run:
                print(f"OAH_HOOK_DRY_RUN kind={kind} path={where} action={change.action} (nothing written)")
            else:
                verb = "INSTALLED" if args.hooks_command == "install" else "REMOVED"
                backup = f" backup={_relative(change.backup, root)}" if change.backup else ""
                print(f"OAH_HOOK_{verb} kind={kind} path={where}{backup}")
        return EXIT_OK
    except hooks.HookError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except (NotARepo, GitMissing, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
