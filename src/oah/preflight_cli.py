# 사전 점검 명령 — oah preflight 의 인자, 저장소·상태 경로 찾기, 보고서 출력과 판정별 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import os
import pathlib
import sys

from oah import preflight
from oah.packet import PacketError
from oah.paths import GitMissing, NotARepo, repo_info, state_dir

EXIT_USAGE, EXIT_ENVIRONMENT = 2, 69
COMMANDS = ("preflight",)


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("preflight", help="check that the base, the tree and the leases are safe to start on")
    parser.add_argument("--task", help="also check this task's packet and use its Scope")
    parser.add_argument("--touch", nargs="+", action="extend", default=[], metavar="PATH",
                        help="paths you plan to change")
    parser.add_argument("--offline", action="store_true",
                        help="do not fetch; compare with the last fetched state")


def run(args: argparse.Namespace) -> int:
    try:
        info = repo_info(pathlib.Path.cwd())
        report = preflight.run_checks(info, state=state_dir(info, os.environ), task=args.task,
                                      touch=tuple(args.touch), offline=args.offline,
                                      settings=dict(preflight.DEFAULTS))
    except PacketError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    except (NotARepo, GitMissing) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    print(report.render(), end="")
    return report.exit_code
