# 훅 실행 분기 — oah hook <이름> 을 oah.hook_<이름> 모듈(대시는 밑줄)의 run(args) 로 넘기고, 모르는 이름은 알려진 목록과 함께 거절한다.
from __future__ import annotations

import argparse
import importlib
import pkgutil
import re
import sys

import oah

COMMANDS = ("hook",)
EXIT_USAGE = 2
NAME_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def register(commands: argparse._SubParsersAction) -> None:
    hook = commands.add_parser("hook", help="run one hook (called by the installed Claude Code and git hooks)")
    hook.add_argument("name", help="the hook to run, for example stop or pre-push")
    hook.add_argument("hook_args", nargs=argparse.REMAINDER, help="arguments the caller passes through")


def known_hooks() -> tuple[str, ...]:
    """Every `oah hook <name>` this installation provides (modules oah.hook_<name>)."""
    names = [info.name[len("hook_"):].replace("_", "-") for info in pkgutil.iter_modules(oah.__path__)
             if info.name.startswith("hook_") and info.name != "hook_cli"]
    return tuple(sorted(names))


def run(args: argparse.Namespace) -> int:
    known = known_hooks()
    if not NAME_RE.fullmatch(args.name or "") or args.name not in known:
        print(f"OAH_HOOK_UNKNOWN: {args.name!r} is not a hook this oah provides; known hooks: "
              f"{', '.join(known) or 'none'}. Reinstall it with oah hooks install <kind>", file=sys.stderr)
        return EXIT_USAGE
    module = importlib.import_module(f"oah.hook_{args.name.replace('-', '_')}")
    return module.run(args)
