# python -m oah.setup 진입점 — 설치 스크립트가 부르는 동의 기반 도구 설치 안내를 실행한다.
"""python -m oah.setup [--os macos|windows] [--yes] [--dry-run]"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        sys.stderr.write("oah needs Python 3.11 or newer; this is Python %d.%d\n" % sys.version_info[:2])
        return 2
    import argparse

    from oah.console import configure_stdio
    from oah.setup.guide import guided_setup
    from oah.setup.tools import os_name_for

    configure_stdio()
    parser = argparse.ArgumentParser(prog="python -m oah.setup",
                                     description="Check the tools oah uses and install the missing ones, "
                                                 "asking before each one.")
    parser.add_argument("--os", choices=("macos", "windows"), default=os_name_for(sys.platform),
                        help="which OS's install commands to use (default: this one)")
    parser.add_argument("--yes", action="store_true", help="install every missing tool without asking")
    parser.add_argument("--dry-run", action="store_true", help="only check and print the plan; install nothing")
    args = parser.parse_args(argv)
    return guided_setup(os_name=args.os, yes=args.yes, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
