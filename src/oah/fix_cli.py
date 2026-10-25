# 수정 명령 모듈 — oah fix 의 인자(수정 실행·--bind·--manual·--abort), 결과 줄과 JSON 출력, 거부 코드별 종료 코드(0 성공, 1 그 밖, 2 사용법, 69 환경, 130 중단)를 맡는다.
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
from oah.land_cli import _LandParser
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo
from oah.review import ReviewError, fix
from oah.runs import RunError

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT, EXIT_INTERRUPTED = 0, 1, 2, 69, 130
COMMANDS = ("fix",)
ENVIRONMENT_CODES = frozenset({"OAH_FIX_GIT_FAILED", "OAH_REVIEW_GIT_FAILED", "OAH_REVIEW_RECORD_EXISTS",
                               "OAH_RUN_PATH_UNSAFE", "OAH_STATE_IO", "OAH_STATE_CORRUPT", "OAH_STATE_PENDING",
                               "OAH_STATE_LOCK_TIMEOUT", "OAH_NOT_A_REPO", "OAH_GIT_MISSING",
                               *call_cli.ENVIRONMENT_CODES})


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds above 0") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds above 0")
    return value


def _usage_error(as_json: bool) -> int:
    code, message = fix._error_diagnostic(ReviewError("OAH_USAGE", ""))
    return _report(code, EXIT_USAGE, message, as_json)


class _FixParser(_LandParser):
    """Reuse land's token-consuming boundary with the correction diagnostic catalog."""

    def error(self, message):
        _usage_error(self._as_json)
        self.exit(EXIT_USAGE)


def register(commands: argparse._SubParsersAction) -> None:
    parser_class = commands._parser_class
    try:
        commands._parser_class = _FixParser
        parser = commands.add_parser("fix", help="run one Codex correction of the latest review's findings, or record "
                                                 "and bind a correction so the next review counts it")
    finally:
        commands._parser_class = parser_class
    parser.add_argument("--task", required=True, help="the task whose latest review is corrected")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--bind", action="store_true",
                      help="bind the committed correction to the attempt that waits for it")
    mode.add_argument("--manual", action="store_true",
                      help="record a correction you or Claude made and committed (needs --engine)")
    mode.add_argument("--abort", action="store_true",
                      help="record as aborted an attempt whose oah fix stopped without an outcome")
    parser.add_argument("--engine", choices=("claude", "codex", "human"),
                        help="who wrote the commits: required with --manual (claude or human); with --bind, who "
                             "replaced Codex's changes (recorded)")
    parser.add_argument("--finding", dest="findings", nargs="+", action="extend", metavar="ID",
                        help="correct only these P0/P1 findings of the latest receipt (default: those without an "
                             "applied correction)")
    parser.add_argument("--tier", choices=routing.TIERS,
                        help="the corrector's tier (default: the packet's plan, else scored from the task)")
    parser.add_argument("--timeout-seconds", type=_seconds,
                        help="stop the corrector after this many seconds (default: the tier's timeout_min)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def _usage(args: argparse.Namespace) -> str | None:
    if args.manual and args.engine not in fix.MANUAL_ENGINES:
        return "--manual needs --engine claude or --engine human: say who made the correction"
    if args.engine and not (args.manual or args.bind):
        return "--engine goes with --manual or --bind; a correction run is always Codex"
    if args.findings and (args.bind or args.abort):
        return "--finding goes with a correction run or --manual"
    if (args.tier or args.timeout_seconds) and (args.bind or args.manual or args.abort):
        return "--tier and --timeout-seconds go with a correction run only"
    return None


def _report(code: str, exit_code: int, message: str, as_json: bool) -> int:
    print(message, file=sys.stderr)
    if as_json:
        print(json.dumps({"code": code, "exit_code": exit_code, "message": message}, indent=2, ensure_ascii=False))
    return exit_code


def _refused(exc: BaseException, as_json: bool) -> int:
    code, message = fix._error_diagnostic(exc)
    exit_code = EXIT_USAGE if code == "OAH_USAGE" else EXIT_ENVIRONMENT if code in ENVIRONMENT_CODES else EXIT_REFUSED
    return _report(code, exit_code, message, as_json)


def run(args: argparse.Namespace) -> int:
    from oah.config import ConfigError

    problem = _usage(args)
    if problem is not None:
        return _usage_error(args.json)
    cwd, env, findings = pathlib.Path.cwd(), os.environ, tuple(args.findings or ())
    try:
        if args.bind:
            result = fix.bind(args.task, cwd=cwd, env=env, engine=args.engine)
        elif args.manual:
            result = fix.record_manual(args.task, engine=args.engine, cwd=cwd, env=env, findings=findings)
        elif args.abort:
            result = fix.abort(args.task, cwd=cwd, env=env)
        else:
            result = fix.correct(fix.Request(task=args.task, findings=findings, tier=args.tier,
                                             timeout_s=args.timeout_seconds), cwd=cwd, env=env)
    except fix.FixInterrupted as exc:
        code, message = fix._error_diagnostic(exc)
        return _report(code, EXIT_INTERRUPTED, message, args.json)
    except KeyboardInterrupt:
        code, message = fix._error_diagnostic(KeyboardInterrupt())
        return _report(code, EXIT_INTERRUPTED, message, args.json)
    except (ReviewError, EngineError, LeaseError, RunError, briefs.BriefError, routing.RoutingError) as exc:
        return _refused(exc, args.json)
    except (ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, PendingTransaction, StorageError) as exc:
        code, message = fix._error_diagnostic(exc)
        return _report(code, EXIT_ENVIRONMENT, message, args.json)
    except OSError as exc:
        code, message = fix._error_diagnostic(exc)
        return _report(code, EXIT_ENVIRONMENT, message, args.json)
    except Exception as exc:
        return _refused(exc, args.json)
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    if args.json:
        print(json.dumps(result.to_json(), indent=2, ensure_ascii=False))
    else:
        print(result.message)
        for line in result.lines:
            print(line)
    return result.exit_code
