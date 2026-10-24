# 리뷰 명령 모듈 — oah review 의 인자, 결과 줄과 JSON 출력, 거부 코드별 종료 코드(0 수렴, 1 그 밖, 2 사용법, 69 환경)를 맡는다.
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys

from oah import briefs, call_cli, routing
from oah.atomic import StateCorrupt, StorageError
from oah.engines.effort import EngineError
from oah.journal import PendingTransaction
from oah.lease import LeaseError
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo
from oah.review import ReviewError, runner
from oah.runs import RunError

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69
COMMANDS = ("review",)
ENVIRONMENT_CODES = frozenset({"OAH_REVIEW_SNAPSHOT_FAILED", "OAH_REVIEW_GIT_FAILED", "OAH_REVIEW_RECORD_EXISTS",
                               "OAH_RUN_PATH_UNSAFE", *call_cli.ENVIRONMENT_CODES})


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} must be a number of seconds above 0")
    return value


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("review", help="run one Codex review round of a task's committed work and record "
                                                "its receipt")
    parser.add_argument("--task", required=True, help="the task whose lease, packet and branch are reviewed")
    parser.add_argument("--base", help="review against this commit (default: the merge-base with the default branch)")
    parser.add_argument("--extra-round", action="store_true",
                        help="run one round beyond [review] max_rounds (recorded in the receipt and the events)")
    parser.add_argument("--extra-attempt", action="store_true",
                        help="permit one additional reviewer call after round_attempts is spent (recorded)")
    parser.add_argument("--decision", metavar="TEXT",
                        help="continue a series that stopped for a decision; say what you decided (recorded)")
    parser.add_argument("--tier", choices=routing.TIERS,
                        help="the reviewer's tier (default: the packet's plan, else scored from the task)")
    parser.add_argument("--timeout-seconds", type=_seconds,
                        help="stop the reviewer after this many seconds (default: the tier's timeout_min)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def _refused(exc: BaseException, as_json: bool) -> int:
    code = getattr(exc, "code", "OAH_REVIEW_FAILED")
    exit_code = EXIT_ENVIRONMENT if code in ENVIRONMENT_CODES else EXIT_REFUSED
    print(exc, file=sys.stderr)
    if as_json:
        print(json.dumps({"code": code, "exit_code": exit_code, "message": str(exc)}, indent=2, ensure_ascii=False))
    return exit_code


def run(args: argparse.Namespace) -> int:
    from oah.config import ConfigError

    if args.decision is not None and not args.decision.strip():
        print("OAH_USAGE: --decision needs the decision in words; say what you decided", file=sys.stderr)
        return EXIT_USAGE
    request = runner.Request(task=args.task, base=args.base, extra_round=args.extra_round, decision=args.decision,
                             tier=args.tier, timeout_s=args.timeout_seconds, extra_attempt=args.extra_attempt)
    try:
        result = runner.review(request, cwd=pathlib.Path.cwd(), env=os.environ)
    except (ReviewError, EngineError, LeaseError, RunError, briefs.BriefError, routing.RoutingError) as exc:
        return _refused(exc, args.json)
    except (ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, PendingTransaction, StorageError) as exc:
        print(exc, file=sys.stderr)
        if args.json:
            print(json.dumps({"code": getattr(exc, "code", type(exc).__name__), "exit_code": EXIT_ENVIRONMENT,
                              "message": str(exc)}, indent=2, ensure_ascii=False))
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(StorageError(pathlib.Path(exc.filename or "."), exc), file=sys.stderr)
        return EXIT_ENVIRONMENT
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    if args.json:
        print(json.dumps(result.to_json(), indent=2, ensure_ascii=False))
    else:
        print(result.message)
        for line in result.lines:
            print(line)
    return result.exit_code
