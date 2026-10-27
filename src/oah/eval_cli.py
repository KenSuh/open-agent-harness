# eval 명령 — oah eval prereg/run/grade/report 의 인자, 설정([eval])·실행 폴더 위치, 출력과 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import os
import pathlib
import sys

from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError, load_config
from oah.eval import grade, plan, report, runner
from oah.eval.errors import EXIT_ENVIRONMENT, EvalError
from oah.paths import STATE_ENV, GitMissing, NotARepo, repo_info, state_dir

EXIT_OK = 0
COMMANDS = ("eval",)


def register(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser("eval", help="replay real cases with hidden oracles and compare variants")
    sub = command.add_subparsers(dest="eval_command", metavar="<eval command>", required=True)

    prereg = sub.add_parser("prereg", help="freeze cases, variants, metrics and grader rules before running")
    prereg.add_argument("plan", help="the evaluation plan (.toml)")
    prereg.add_argument("--oracle", action="append", required=True,
                        help="an oracle file, outside every case folder; repeat for each case")
    prereg.add_argument("--out", required=True, help="the preregistration .json to write (and .json.sha256)")

    run = sub.add_parser("run", help="run every variant on every case in fresh, isolated workspaces")
    run.add_argument("source", help="a preregistration .json, or a plan .toml (holdout cases need a .json)")
    run.add_argument("--expect-sha256", help="the preregistration hash you recorded; default: the .sha256 file")
    run.add_argument("--runs-dir", help="where run folders go (default: <oah state>/eval/runs)")
    run.add_argument("--timeout-seconds", type=float, help="per-variant time limit when the plan sets none")

    grade_cmd = sub.add_parser("grade", help="compare a run's outputs with the oracles by explicit match rules")
    grade_cmd.add_argument("run_dir", help="the run folder that oah eval run printed")
    grade_cmd.add_argument("--oracle", action="append", required=True, help="an oracle file; repeat for each case")
    grade_cmd.add_argument("--verdicts", help="a human verdict file for unmatched findings (see docs/eval.md)")

    report_cmd = sub.add_parser("report", help="aggregate a graded run by split and variant, with its limits")
    report_cmd.add_argument("run_dir", help="the graded run folder")
    report_cmd.add_argument("--json", action="store_true", help="print the report as JSON")


def _settings() -> dict:
    try:
        root = repo_info(pathlib.Path.cwd()).root
    except NotARepo:
        return dict(runner.DEFAULTS)
    return load_config(root).section("eval", runner.DEFAULTS)


def _runs_root(args: argparse.Namespace) -> pathlib.Path:
    if args.runs_dir:
        return pathlib.Path(args.runs_dir)
    if os.environ.get(STATE_ENV):
        return pathlib.Path(os.environ[STATE_ENV]) / "eval" / "runs"
    try:
        return state_dir(repo_info(pathlib.Path.cwd()), os.environ) / "eval" / "runs"
    except NotARepo as exc:
        raise EvalError("OAH_EVAL_NO_STATE", f"{exc}; run inside a git repository, set {STATE_ENV}, or pass "
                                             "--runs-dir", EXIT_ENVIRONMENT) from exc


def _prereg(args: argparse.Namespace) -> int:
    digest = plan.preregister(plan.load_plan(pathlib.Path(args.plan)), [pathlib.Path(p) for p in args.oracle],
                              pathlib.Path(args.out))
    print(f"OAH_EVAL_PREREG_WRITTEN sha256={digest} file={pathlib.Path(args.out).resolve()}")
    print("Record this hash somewhere the agents cannot change. Next: oah eval run "
          f"{args.out}")
    return EXIT_OK


def _run(args: argparse.Namespace) -> int:
    settings = _settings()
    source = plan.load_run_source(pathlib.Path(args.source), args.expect_sha256)
    timeout = args.timeout_seconds or settings["timeout_seconds"]
    run_dir = runner.run_eval(source, _runs_root(args), env=dict(os.environ), timeout_seconds=timeout)
    run = grade.load_run(run_dir)
    for entry in run["results"]:
        result = grade.load_result(run_dir, entry)
        tag = f" [{result['label']}]" if result["label"] == runner.CONTROL_FLOW_ONLY else ""
        status = "timed out" if result["timed_out"] else f"exit {result['exit_code']}"
        print(f"case {result['case']} ({result['split']}) variant {result['variant']}: {status} in "
              f"{result['wall_seconds']} s · output {result['output']} · isolation oracle "
              f"{result['isolation']['oracle']}{tag}")
    print(f"OAH_EVAL_RUN_DONE run={run['run_id']} folder={run_dir.resolve()}")
    print(f"Next: oah eval grade {run_dir.resolve()} --oracle <oracle file>")
    return EXIT_OK


def _grade(args: argparse.Namespace) -> int:
    settings = _settings()
    graded = grade.grade_run(pathlib.Path(args.run_dir), [pathlib.Path(p) for p in args.oracle],
                             verdicts_path=pathlib.Path(args.verdicts) if args.verdicts else None,
                             timeout_seconds=settings["test_timeout_seconds"])
    sys.stdout.write(grade.render(graded))
    print(f"OAH_EVAL_GRADED file={(pathlib.Path(args.run_dir) / grade.GRADE_FILE).resolve()}")
    print(f"Next: oah eval report {args.run_dir}")
    return EXIT_OK


def _report(args: argparse.Namespace) -> int:
    data = report.build_report(pathlib.Path(args.run_dir))
    sys.stdout.write(report.dumps(data) if args.json else report.render(data))
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    handlers = {"prereg": _prereg, "run": _run, "grade": _grade, "report": _report}
    try:
        return handlers[args.eval_command](args)
    except EvalError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except (GitMissing, StorageError, StateCorrupt) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
