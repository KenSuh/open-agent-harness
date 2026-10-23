# python -m oah.setup 진입점 — 설치 스크립트가 부르는 동의 기반 도구 설치 안내를 실행한다.
"""python -m oah.setup [--os macos|windows] [--yes] [--with orca,aside] [--dry-run]"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        sys.stderr.write("oah needs Python 3.11 or newer; this is Python %d.%d\n" % sys.version_info[:2])
        return 2
    import argparse

    from oah.console import configure_stdio
    from oah.setup.guide import guided_setup
    from oah.setup.tools import catalog, os_name_for

    configure_stdio()
    parser = argparse.ArgumentParser(prog="python -m oah.setup",
                                     description="Check the tools oah uses and install the missing ones, "
                                                 "asking before each one.")
    parser.add_argument("--os", choices=("macos", "windows"), default=os_name_for(sys.platform),
                        help="which OS's install commands to use (default: this one)")
    parser.add_argument("--yes", action="store_true",
                        help="install every missing tool without asking, except the optional apps")
    parser.add_argument("--with", dest="with_optional", default=None, metavar="APPS",
                        help="optional apps to install too, comma-separated (for example orca,aside)")
    parser.add_argument("--dry-run", action="store_true", help="only check and print the plan; install nothing")
    args = parser.parse_args(argv)
    optional = [tool.name for tool in catalog() if tool.optional]
    named: list[str] = []
    if args.with_optional is not None:
        named = [name.strip() for name in args.with_optional.split(",")]
        unknown = [name for name in named if name not in optional]
        if unknown or not named:
            shown = ", ".join(repr(name) for name in unknown) or "nothing"
            sys.stderr.write(f"OAH_SETUP_USAGE: --with got {shown}. Name optional apps from this list, separated "
                             f"by commas: {', '.join(optional)}\n")
            return 2
    return guided_setup(os_name=args.os, yes=args.yes, dry_run=args.dry_run, with_optional=named)


if __name__ == "__main__":
    raise SystemExit(main())
