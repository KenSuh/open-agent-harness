# oah init 명령 — 안내형 설정의 인자, 질문, 계획 출력, 최종 확인, 적용, 요약 줄과 종료 코드를 맡는다.
"""init: guided project setup (tools, sign-ins, rules, wiki, gates, hooks, impact map)."""
from __future__ import annotations

import argparse
import pathlib
import sys

from oah import init
from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError
from oah.journal import PendingTransaction
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, repo_info
from oah.rules import LANGS, RulesError
from oah.wiki.base import WikiError

COMMANDS = ("init",)
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69


def make_machine() -> init.Machine:
    """The real tools, terminal and environment; tests replace this function."""
    return init.Machine.real()


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser(
        "init", help="set up this project for oah: a guided plan, then only the changes you accept",
        description="Check tools and sign-ins, then create the project environment (config, rules block, wiki, "
                    "gate templates, the hooks and options you choose) and build the first impact map. The whole "
                    "plan is printed before anything is written. Each --with-<x>/--without-<x> answers one question "
                    "without asking; without a terminal, every question needs its flag.")
    parser.add_argument("--lang", choices=LANGS, default=None,
                        help="language of the rules block (default: the block's current language, else en)")
    for question in init.QUESTIONS:
        group = parser.add_mutually_exclusive_group()
        group.add_argument(question.flag_yes, dest=question.key, action="store_const", const=True, default=None,
                           help=f"{question.title}: yes, without asking")
        group.add_argument(question.flag_no, dest=question.key, action="store_const", const=False,
                           help=f"{question.title}: no, without asking")
    parser.add_argument("--yes", action="store_true",
                        help="install missing tools without asking (it answers no other question)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write, install and run nothing")
    parser.add_argument("--force", action="store_true",
                        help="replace init's own files that differ (never the wiki's index or log)")


def run(args: argparse.Namespace) -> int:
    try:
        return _init(args, make_machine())
    except init.InitError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except (RulesError, WikiError) as exc:
        print(f"{exc.code}: project rules or wiki cannot be safely processed. Check their format and paths, "
              "then run oah init again", file=sys.stderr)
        return exc.exit_code
    except (NotARepo, GitMissing) as exc:
        print(f"{exc}. Run oah init inside your project's git repository; `git init` starts one.", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except ConfigError as exc:
        print(f"{exc.code}: the project configuration is invalid. Fix .oah/config.toml, then run oah init again",
              file=sys.stderr)
        return EXIT_ENVIRONMENT
    except (LockTimeout, StateCorrupt, StorageError, PendingTransaction, OSError):
        print("OAH_INIT_STORAGE_UNAVAILABLE: project state cannot be safely accessed. Check permissions and "
              "pending wiki recovery, then run oah init again", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except KeyboardInterrupt:
        print("OAH_INIT_STOPPED: stopped. Run oah init again to finish; it changes only what is still missing.",
              file=sys.stderr)
        return EXIT_REFUSED


def _init(args: argparse.Namespace, machine: init.Machine) -> int:
    root = repo_info(pathlib.Path.cwd()).root
    given = {question.key: getattr(args, question.key) for question in init.QUESTIONS}
    try:
        existing = init._load_config(root)
    except ConfigError:
        if not args.force:
            raise
        existing = None  # --force replaces a config that does not parse
    writes_config = existing is None or existing.path is None or args.force
    print(f"oah init: guided setup of {root}"
          + (" (dry run: nothing will be written, installed or run)" if args.dry_run else ""))
    answers = init.resolve_answers(given, recorded=None if writes_config else init.recorded_answers(existing),
                                   interactive=machine.interactive, dry_run=args.dry_run, ask=machine.ask, out=print)
    plan = init.build_plan(root, answers, given=given, machine=machine, lang=args.lang, force=args.force,
                           existing=existing)
    print("")
    for line in init.render_plan(plan):
        print(line)
    print("End of plan.")
    if args.dry_run:
        print("Dry run: nothing was written, installed or run. Run oah init without --dry-run to do it.")
        print(f"OAH_INIT_DRY_RUN changes={plan.changes} impact={plan.map_action}")
        return EXIT_OK
    if machine.interactive and (plan.changes or plan.companion_steps):
        reply = machine.ask("Apply this plan? [Y/n] ")
        if reply is None or reply.strip().lower() not in ("", "y", "yes"):
            raise init.InitError("OAH_INIT_DECLINED", "nothing was changed. Run oah init again when you are ready; "
                                 "oah init --dry-run shows the plan without asking")
    return _summary(init.apply_plan(plan, machine, yes=args.yes, force=args.force, out=print))


def _summary(outcome: init.Outcome) -> int:
    impact = outcome.impact
    print("")
    if outcome.pending:
        print("Some steps are not done yet; each one says above what to do. Then run oah init again: it changes only "
              "what is still missing.")
        print(f"OAH_INIT_INCOMPLETE changed={outcome.changed} impact={impact} pending={','.join(outcome.pending)}")
        return EXIT_REFUSED
    if outcome.changed == 0 and impact in ("fresh", "unavailable"):
        print("Nothing to change: every file is in place" + (" and the impact map is fresh." if impact == "fresh"
                                                             else "; the impact map is still unavailable (see above)."))
        print(f"OAH_INIT_UNCHANGED impact={impact}")
        return EXIT_OK
    if outcome.changed == 0:
        print(f"Every file was already in place; only the impact map was {impact}.")
    else:
        print("Next: review and commit the new files, then start a task with `oah packet new <task>`.")
    print(f"OAH_INIT_DONE changed={outcome.changed} impact={impact}")
    return EXIT_OK
