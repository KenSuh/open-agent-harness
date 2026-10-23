# oah 명령 진입점 — 파이썬 버전을 먼저 확인한 뒤 출력 인코딩을 안전하게 하고 하위 명령으로 넘긴다.
from __future__ import annotations

import argparse
import sys

MIN_PYTHON = (3, 11)


def build_parser() -> argparse.ArgumentParser:
    from oah import __version__

    parser = argparse.ArgumentParser(
        prog="oah",
        description="open-agent-harness: run Claude Code and Codex CLI as a disciplined team on your repository.",
    )
    parser.add_argument("--version", action="version", version=f"oah {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    doctor = commands.add_parser("doctor", help="check that the tools oah needs are installed")
    doctor.add_argument("--json", action="store_true", help="print machine-readable JSON")
    from oah import lease_cli

    lease_cli.register(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write("oah needs Python 3.11 or newer; this is Python %d.%d\n" % sys.version_info[:2])
        return 2
    from oah.console import configure_stdio

    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        from oah.doctor import doctor_main

        return doctor_main(json_output=args.json)
    from oah import lease_cli

    if args.command in lease_cli.COMMANDS:
        return lease_cli.run(args)
    parser.error(f"unknown command: {args.command}")
    return 2
