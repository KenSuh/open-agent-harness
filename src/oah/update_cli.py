# 도구 업데이트 명령 — oah update, oah update status, 그리고 oah finish 가 분리 실행하는 숨은 --after-finish 진입점을 맡는다.
from __future__ import annotations

import argparse
import os
import pathlib
import sys

from oah.atomic import StorageError
from oah.paths import GitMissing, NotARepo, repo_info, state_dir

COMMANDS = ("update",)
EXIT_OK, EXIT_ATTENTION, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 1, 69
EXIT_CODES = {"complete": EXIT_OK, "needs-attention": EXIT_ATTENTION, "deferred": EXIT_REFUSED,
              "busy": EXIT_REFUSED, "skipped": EXIT_REFUSED}


def register(commands: argparse._SubParsersAction) -> None:
    update = commands.add_parser(
        "update", help="update Claude Code, Codex, Claude Code plugins and oah's skills now; `status` shows the last pass",
        description="Run the tool-update pass now: each tool's own update command, in a fixed order, only when "
                    "[updates] after_finish is on and no task or run is in flight, every result checked by reading "
                    "what is installed. Codex plugins are updated manually. MCP servers are never changed. "
                    "`oah update status` prints the last pass and "
                    "runs nothing.")
    update.add_argument("action", nargs="?", choices=("status",), help="status: print the last pass; runs nothing")
    update.add_argument("--after-finish", action="store_true", help=argparse.SUPPRESS)
    update.add_argument("--repo", help=argparse.SUPPRESS)


def run(args: argparse.Namespace) -> int:
    from oah import toolupdate

    if args.action == "status":
        for line in toolupdate.status_lines():
            print(line)
        return EXIT_OK
    if args.after_finish:
        return _after_finish(args)
    try:
        info = repo_info(pathlib.Path.cwd())
        outcome = toolupdate.run_pass(info.root, state_dir(info, os.environ), env=os.environ, trigger="manual",
                                      out=print)
    except (NotARepo, GitMissing) as exc:
        print(f"{exc}. Run oah update inside your project's git repository", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except StorageError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        from oah.toolupdate import records

        print(StorageError(records.updates_dir(), exc), file=sys.stderr)
        return EXIT_ENVIRONMENT
    return EXIT_CODES[outcome]


def _after_finish(args: argparse.Namespace) -> int:
    """The detached pass `oah finish` starts: nobody reads its exit code, so it is always 0."""
    from oah import toolupdate
    from oah.toolupdate import records

    try:
        root = pathlib.Path(args.repo or ".")
        info = repo_info(root)
        toolupdate.run_pass(info.root, state_dir(info, os.environ), env=os.environ, trigger="after-finish",
                            out=print, log=records.log_path())
    except Exception as exc:  # nothing waits for this process; the log and the records say what happened
        print(f"OAH_UPDATE_SKIPPED: the background pass stopped ({type(exc).__name__}); run oah update to see why")
    return EXIT_OK
