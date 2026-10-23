# 규칙 명령 — oah rules sync/check 의 인자, 저장소 루트 찾기, 출력 형식, 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import pathlib
import sys

from oah.atomic import StorageError
from oah.paths import GitMissing, NotARepo, repo_info
from oah.rules import LANGS, RulesError, check, sync

EXIT_OK, EXIT_DRIFT, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("rules",)


def register(commands: argparse._SubParsersAction) -> None:
    rules = commands.add_parser("rules", help="keep the shared rules block in AGENTS.md (CLAUDE.md imports it)")
    sub = rules.add_subparsers(dest="rules_command", metavar="<rules command>", required=True)
    for name, text in (("sync", "write or update the managed block and the @AGENTS.md import"),
                       ("check", "report drift from the template (reads only; exit 1 on drift)")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--lang", choices=LANGS, default=None,
                             help="block language (default: the block's current language, else en)")


def run(args: argparse.Namespace) -> int:
    try:
        root = repo_info(pathlib.Path.cwd()).root
        if args.rules_command == "sync":
            result = sync(root, lang=args.lang)
            changed = ",".join(result.changed) or "nothing"
            print(f"OAH_RULES_SYNCED lang={result.lang} changed={changed} agents_bytes={result.agents_bytes} "
                  f"block_bytes={result.block_bytes}")
            return EXIT_OK
        drift = check(root, lang=args.lang)
        if not drift:
            print("OAH_RULES_IN_SYNC")
            return EXIT_OK
        for line in drift:
            print(line)
        print("Run `oah rules sync` to update AGENTS.md and CLAUDE.md.")
        return EXIT_DRIFT
    except RulesError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except (NotARepo, GitMissing, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
