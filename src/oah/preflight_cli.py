# 사전 점검 명령 — oah preflight 의 인자, 저장소·상태 경로와 [preflight] 설정 읽기, 보고서 출력과 판정별 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import os
import pathlib
import sys

from oah import preflight
from oah.config import ConfigError, load_config
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


def project_checks(settings: dict, where: str) -> list[list[str]]:
    """[preflight].checks as argv lists; anything else is OAH_CONFIG_INVALID."""
    checks = settings["checks"]
    for number, argv in enumerate(checks, start=1):
        if not (isinstance(argv, list) and argv and all(isinstance(part, str) and part for part in argv)):
            raise ConfigError("OAH_CONFIG_INVALID",
                              f"{where}: preflight.checks entry {number} must be a non-empty array of strings, "
                              'such as ["python", "tools/check_migrations.py"]')
    return [list(argv) for argv in checks]


def run(args: argparse.Namespace) -> int:
    try:
        info = repo_info(pathlib.Path.cwd())
        config = load_config(info.root)
        settings = config.section("preflight", preflight.DEFAULTS)
        checks = project_checks(settings, str(config.path or ".oah/config.toml"))
        report = preflight.run_checks(info, state=state_dir(info, os.environ), task=args.task,
                                      touch=tuple(args.touch), offline=args.offline, settings=settings,
                                      impact=preflight.impact_hook, project_checks=checks)
    except PacketError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    except (NotARepo, GitMissing, ConfigError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    print(report.render(), end="")
    return report.exit_code
