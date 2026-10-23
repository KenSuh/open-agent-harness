# doctor 명령 모듈 — oah doctor 의 인자를 등록하고 doctor.doctor_main 으로 넘긴다.
from __future__ import annotations

import argparse

COMMANDS = ("doctor",)


def register(commands: argparse._SubParsersAction) -> None:
    doctor = commands.add_parser("doctor", help="check that the tools oah needs are installed")
    doctor.add_argument("--json", action="store_true", help="print machine-readable JSON")


def run(args: argparse.Namespace) -> int:
    from oah.doctor import doctor_main

    return doctor_main(json_output=args.json)
