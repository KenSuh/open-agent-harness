# 착지 명령 — oah land --check|--merge 의 인자, 검사 줄과 JSON 출력, 병합 전 확인(대화형 y 또는 --confirm 전체 SHA), 기록, 종료 코드(0 준비·착지, 1 거부, 2 사용법, 69 환경)를 맡는다.
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from oah import land
from oah.atomic import StateCorrupt, StorageError
from oah.journal import PendingTransaction
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, repo_info, state_dir
from oah.review import ReviewError

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69
COMMANDS = ("land",)
ENVIRONMENT_CODES = frozenset({"OAH_REVIEW_GIT_FAILED", "OAH_RUN_PATH_UNSAFE"})


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("land", help="check that a task's exact reviewed head may land, or land it as a "
                                              "fast-forward push after you confirm")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="run every landing check and push nothing")
    mode.add_argument("--merge", action="store_true",
                      help="run every landing check again, then, after you confirm, fast-forward the remote default "
                           "branch to exactly HEAD (only while it still equals the SHA just checked)")
    parser.add_argument("--task", required=True, help="the finished, reviewed task whose head lands")
    parser.add_argument("--confirm", metavar="SHA",
                        help="with --merge: confirm without a prompt by giving HEAD's full SHA (needed when not "
                             "interactive)")
    parser.add_argument("--offline", action="store_true",
                        help="do not fetch the default branch; landing then refuses with OAH_LAND_IMPACT_STALE, but "
                             "every other check still runs")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _ask(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    answer = sys.stdin.readline() if sys.stdin is not None else ""
    if not answer.endswith("\n") or sys.stdin is None or not sys.stdin.isatty():
        sys.stderr.write("\n")  # no echoed Enter: keep the next message off the prompt line
    return answer


def _check_lines(assessment: land.Assessment) -> list[str]:
    lines = []
    for check in assessment.checks:
        code = f" {check.code}" if check.code else ""
        lines.append(f"{check.name}: {check.state}{code} — {check.message}")
        if check.name == "required-checks":
            lines += [f"  - {item['gate']} ({item['binding']} on {', '.join(item['targets'])}): {item['state']}"
                      for item in assessment.required]
    return lines


def _where(assessment: land.Assessment) -> str:
    return f"{assessment.remote}/{assessment.branch}"


def _ci_note(assessment: land.Assessment) -> str:
    ci = assessment.check("ci")
    return " CI is unverified, not passed: read the ci line above." if ci.state == land.UNVERIFIED else ""


def _ready_message(assessment: land.Assessment) -> str:
    plural = "s" if assessment.commits != 1 else ""
    return (f"task {assessment.task} can land: HEAD {assessment.head} fast-forwards {_where(assessment)} from "
            f"{assessment.fetched[:12]} ({assessment.commits} commit{plural}).{_ci_note(assessment)} Run oah land "
            f"--merge --task {assessment.task} to land it; it checks everything again and asks you to confirm")


def _confirmation(args: argparse.Namespace, assessment: land.Assessment) -> tuple[str, str] | None:
    """None when the merge is confirmed, else (code, message); nothing is pushed in that case."""
    head, task, where = assessment.head, assessment.task, _where(assessment)
    if args.confirm is not None:
        given = args.confirm.strip().lower()
        if given == head:
            return None
        return ("OAH_LAND_CONFIRM_MISMATCH", f"--confirm {args.confirm.strip()!r} is not HEAD's full SHA {head}; "
                                             f"nothing was pushed. Read the checks above, then run oah land --merge "
                                             f"--task {task} --confirm {head}")
    if not _interactive():
        return ("OAH_LAND_CONFIRM_REQUIRED", f"landing pushes HEAD to {where} and needs your confirmation; nothing "
                                             "was pushed. Run it in a terminal to answer the prompt, or read the "
                                             f"checks above and run oah land --merge --task {task} --confirm {head}")
    plural = "s" if assessment.commits != 1 else ""
    answer = _ask(f"About to land task {task}: push HEAD {head} to {where}, a fast-forward from "
                  f"{assessment.fetched[:12]} ({assessment.commits} commit{plural}).{_ci_note(assessment)}\n"
                  "This changes the remote's default branch, and oah cannot undo it. Land it? [y/N] ")
    if answer.strip().lower() in ("y", "yes"):
        return None
    return ("OAH_LAND_CANCELLED", f"you did not confirm, so nothing was pushed to {where}; run oah land --merge "
                                  f"--task {task} again when you are ready")


def _record(state: pathlib.Path, assessment: land.Assessment, mode: str, pushed: land.Pushed | None) -> None:
    """Write the landing record; a failure never changes what happened, so it is only reported."""
    try:
        land.record(state, assessment, mode, pushed)
    except (land.LandError, LockTimeout, StateCorrupt, OSError) as exc:
        print(f"OAH_LAND_RECORD_FAILED: the landing record could not be written ({exc}); the result below stands. "
              "Check the state folder's permissions and free space", file=sys.stderr)


def _done(code: str, message: str, exit_code: int, as_json: bool, assessment: land.Assessment | None = None,
          pushed: land.Pushed | None = None) -> int:
    line = f"{code}: {message}"
    if exit_code != EXIT_OK:
        print(line, file=sys.stderr)
    elif not as_json:
        print(line)
    if as_json:
        print(json.dumps({"code": code, "exit_code": exit_code, "message": message,
                          "assessment": assessment.to_json() if assessment else None,
                          "pushed": pushed.to_json() if pushed else None}, indent=2, ensure_ascii=False))
    return exit_code


def run(args: argparse.Namespace) -> int:
    from oah.config import ConfigError
    from oah.impact.errors import ImpactError

    if args.confirm is not None and not args.merge:
        print("OAH_USAGE: --confirm only applies to oah land --merge", file=sys.stderr)
        return EXIT_USAGE
    env = os.environ
    mode = "merge" if args.merge else "check"
    try:
        info = repo_info(pathlib.Path.cwd())
        state = state_dir(info, env)
        assessment = land.assess(info.root, args.task, env=env, offline=args.offline)
        if not args.json:
            print("\n".join(_check_lines(assessment)))
        if not assessment.ready:
            _record(state, assessment, mode, None)
            failure = assessment.failure
            message = failure.message.rstrip(". ") + ". Nothing was pushed." if args.merge else failure.message
            return _done(failure.code, message, EXIT_REFUSED, args.json, assessment)
        if args.check:
            _record(state, assessment, mode, None)
            return _done(land.READY, _ready_message(assessment), EXIT_OK, args.json, assessment)
        refusal = _confirmation(args, assessment)
        if refusal is not None:
            pushed = land.Pushed(False, *refusal)
            _record(state, assessment, mode, pushed)
            return _done(refusal[0], refusal[1], EXIT_REFUSED, args.json, assessment, pushed)
        pushed = land.push(info.root, assessment, env=env)
        _record(state, assessment, mode, pushed)
        return _done(pushed.code, pushed.message, EXIT_OK if pushed.landed else EXIT_REFUSED, args.json, assessment,
                     pushed)
    except (land.LandError, ReviewError) as exc:
        code = getattr(exc, "code", "OAH_LAND_FAILED")
        return _done(code, str(exc).split(": ", 1)[-1], EXIT_ENVIRONMENT if code in ENVIRONMENT_CODES
                     else EXIT_REFUSED, args.json)
    except ImpactError as exc:
        return _done(exc.code, str(exc).split(": ", 1)[-1], exc.exit_code, args.json)
    except (ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, PendingTransaction, StorageError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        return _done(code, str(exc).removeprefix(f"{code}: "), EXIT_ENVIRONMENT, args.json)
    except OSError as exc:
        error = StorageError(pathlib.Path(exc.filename or "."), exc)
        return _done(error.code, str(error).removeprefix(f"{error.code}: "), EXIT_ENVIRONMENT, args.json)
