# 위키 명령 — oah wiki new/log/lint/rollover/recover/hygiene 의 인자, 저장소·상태 경로 찾기, 출력 형식, 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys

from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError, load_config
from oah.journal import PendingTransaction
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, repo_info, state_dir
from oah.wiki.base import DEFAULTS, EXIT_ENVIRONMENT, WikiError, WikiSettings
from oah.wiki.store import Wiki

EXIT_OK, EXIT_PROBLEMS = 0, 1
COMMANDS = ("wiki",)


def register(commands: argparse._SubParsersAction) -> None:
    wiki = commands.add_parser("wiki", help="keep the project wiki (docs/wiki) linked, sourced and tidy")
    sub = wiki.add_subparsers(dest="wiki_command", metavar="<wiki command>", required=True)

    new = sub.add_parser("new", help="create a note from the template and list it in index.md")
    new.add_argument("title")
    new.add_argument("--kind", choices=("note", "decision"), default="note")
    new.add_argument("--source", action="append", default=[],
                     help="a file path with a commit, a URL or a packet; repeat for more")
    new.add_argument("--tag", action="append", default=[])

    log = sub.add_parser("log", help="add a dated entry at the top of log.md")
    log.add_argument("title")
    log.add_argument("--body", default="", help="the entry text below the heading")
    log.add_argument("--date", type=_date, default=None, help="YYYY-MM-DD (default: today)")

    lint = sub.add_parser("lint", help="list wiki problem candidates (reads only; exit 1 when any exist)",
                          description="Links are ignored only in certainly closed top-level fences at column 0 "
                          "or unambiguous code spans that open and close on the same line. Indented code, "
                          "multiline spans and fences in a list or blockquote may leave candidates, as may "
                          "uncertain HTML/front matter or escaped backticks. Inspect each candidate before "
                          "editing notes.")
    lint.add_argument("--index", action="store_true",
                      help="check index.md for banners, over-long lines and unindexed notes")
    lint.add_argument("--json", action="store_true")

    rollover = sub.add_parser("rollover", help="plan (default) or --apply moving the oldest whole log entries "
                              "into the next numbered archive")
    rollover.add_argument("--apply", action="store_true",
                          help="write the plan; needs a lease whose scope covers log.md and the new archive")
    recover = sub.add_parser("recover", help="finish or check an interrupted rollover")
    recover.add_argument("--resolved", action="store_true",
                         help="after fixing a recovery conflict by hand: check that the log and archives parse "
                              "and no entry appears twice, then drop the journal")
    sub.add_parser("hygiene", help="print one line of problem counts and their change since the last run")

    maintain = sub.add_parser("maintain", help="task upkeep before the final commit: log entry, index line and "
                              "size-based rollover, once per task id")
    maintain.add_argument("--task", required=True, help="the task id used with oah start")
    maintain.add_argument("--title", default=None, help='log and index title (default: "Task <task>")')
    maintain.add_argument("--body", default="", help="the log entry text below the heading")


def _date(value: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def _settings(root: pathlib.Path) -> WikiSettings:
    """The [wiki] section of .oah/config.toml over DEFAULTS."""
    return WikiSettings.from_mapping(load_config(root).section("wiki", DEFAULTS))


def run(args: argparse.Namespace) -> int:
    try:
        info = repo_info(pathlib.Path.cwd())
        wiki = Wiki(info.root, _settings(info.root), state_dir(info, os.environ))
        return HANDLERS[args.wiki_command](args, wiki)
    except WikiError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except (ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, StorageError, PendingTransaction) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT


def _new(args: argparse.Namespace, wiki: Wiki) -> int:
    path = wiki.new(args.title, kind=args.kind, sources=args.source, tags=args.tag)
    print(f"OAH_WIKI_NOTE_CREATED {path.relative_to(wiki.root).as_posix()}")
    if not args.source:
        print("Next: add a source under sources: in its frontmatter (a file path with a commit, a URL or a "
              "packet); oah wiki lint reports notes without one.")
    return EXIT_OK


def _log(args: argparse.Namespace, wiki: Wiki) -> int:
    entry = wiki.log(args.title, body=args.body, today=args.date)
    print(f"OAH_WIKI_LOGGED {entry.date} {entry.title}")
    return EXIT_OK


def _lint(args: argparse.Namespace, wiki: Wiki) -> int:
    report = wiki.lint(index=args.index)
    if args.json:
        print(json.dumps(report.to_json(), indent=2, ensure_ascii=False))
    else:
        from oah.wiki.lint import HINTS

        prefix = pathlib.PurePosixPath(wiki.settings.dir)
        for problem in report.problems:
            print(f"{prefix / problem.path}:{problem.line}: {problem.kind}: {problem.detail}; "
                  f"{HINTS[problem.kind]}")
        print(report.summary())
        if args.index:
            print(f"candidates: {wiki.state / 'index-candidates.json'}")
    return EXIT_PROBLEMS if report.problems else EXIT_OK


def _rollover(args: argparse.Namespace, wiki: Wiki) -> int:
    print(wiki.rollover(apply=args.apply, before_write=lambda plan: _require_lease(wiki, plan)).line)
    return EXIT_OK


def _require_lease(wiki: Wiki, plan) -> None:
    """--apply writes log.md and a new archive: an active lease of this worktree must cover both."""
    from oah.lease import Store
    from oah.scope import outside

    paths = [(wiki.dir / name).relative_to(wiki.root).as_posix() for name in ("log.md", plan.archive_name)]
    root = wiki.root.resolve()
    for lease, stale in Store(wiki.state.parent).snapshot():
        if not stale and pathlib.Path(lease.worktree).resolve() == root and not outside(lease.scope, paths):
            return
    raise WikiError("OAH_WIKI_LEASE_REQUIRED", f"`oah wiki rollover --apply` writes {paths[0]} and {paths[1]}, "
                    "and no active lease in this worktree covers both. Nothing was written. Take one first, for "
                    f"example `oah start --task <task> --agent <you> --scope {wiki.settings.dir}`, or let "
                    "`oah wiki maintain --task <task>` roll the log over under the wiki lock.")


def _recover(args: argparse.Namespace, wiki: Wiki) -> int:
    print(wiki.recover(resolved=args.resolved))
    return EXIT_OK


def _hygiene(args: argparse.Namespace, wiki: Wiki) -> int:
    print(wiki.hygiene())
    return EXIT_OK


def _maintain(args: argparse.Namespace, wiki: Wiki) -> int:
    for line in wiki.maintain_task(args.task, title=args.title, body=args.body):
        print(line)
    return EXIT_OK


HANDLERS = {"maintain": _maintain, "new": _new, "log": _log, "lint": _lint, "rollover": _rollover, "recover": _recover,
            "hygiene": _hygiene}
