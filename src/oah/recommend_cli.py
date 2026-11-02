# recommend 명령 모듈 — oah recommend 의 네 축 인자를 받아 점수표로 고른 단과 그 근거를 출력하고, 엔진·단계를 주면 쓰게 될 모델도 기록 없이 보여 주며, 설계·패킷 작업은 사용자 요청문으로 Jev 판정을 받아 작업 이름으로 기록한다.
"""routing"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from oah.routing import (ENGINES, INTENSITY_SCORES, KIND_SCORES, REASONING_SCORES, RISK_SCORES, STAGES, Axes,
                         RoutingError, recommend)

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
COMMANDS = ("recommend",)
USAGE = "OAH_RECOMMEND_USAGE"
REQUEST_KINDS = ("design", "packet")
REQUEST_LIMIT = 1024 * 1024  # bytes of the request file; Jev reads at most 16,000 characters of it
AXIS_NAMES = ("kind", "intensity", "reasoning", "risk")
JUDGED = "difficulty"  # <state>/difficulty/<task>/<kind>.json: one judgement per task and kind, no request text


class RequestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def add_axis_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    """The four difficulty axes, shared with `oah call`."""
    parser.add_argument("--kind", choices=tuple(KIND_SCORES), required=required, help="what kind of work")
    parser.add_argument("--intensity", choices=tuple(INTENSITY_SCORES), required=required, help="how much work")
    parser.add_argument("--reasoning", choices=tuple(REASONING_SCORES), required=required,
                        help="how much thinking it needs")
    parser.add_argument("--risk", choices=tuple(RISK_SCORES), required=required, help="how bad a mistake would be")


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("recommend", help="pick a tier (low, medium, high, max) from four difficulty axes, "
                                 "or for design and packet work from the user's request")
    add_axis_arguments(parser, required=False)
    parser.add_argument("--request-file", metavar="PATH",
                        help="with --kind design or packet: Jev judges the work from the user's request in this "
                             "UTF-8 file (at most 1 MB); the other axes become optional")
    parser.add_argument("--task", help="the task the judgement is recorded under (with --request-file)")
    parser.add_argument("--scope", action="append", default=[], metavar="PATH",
                        help="a path the work may change, sent after the request; repeat for more (with "
                             "--request-file)")
    parser.add_argument("--engine", choices=ENGINES, help="also show the model this engine would use (launches nothing)")
    parser.add_argument("--stage", choices=STAGES, help="the stage to apply, with its floor; alone, it names its engine")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")


def judgement_path(state: pathlib.Path, task: str, kind: str) -> pathlib.Path:
    """Where `--request-file` records a task's judgement; `oah packet new` looks for the design one."""
    from oah import runs

    return runs._contained_path(state, JUDGED, task, f"{kind}.json")


def _usage(args: argparse.Namespace) -> str | None:
    from oah.lease import TASK_RE

    if args.request_file is None:
        if args.task is not None or args.scope:
            return ("--task and --scope go with --request-file; add --request-file <file> with --kind design or "
                    "packet, or drop them")
        if any(getattr(args, axis) is None for axis in AXIS_NAMES):
            return ("give --kind, --intensity, --reasoning and --risk, or judge design or packet work with --kind "
                    "design --task <task> --request-file <file>")
        return None
    if args.kind not in REQUEST_KINDS:
        return ("--request-file judges design or packet work only; use it with --kind design or --kind packet, or "
                "give the four difficulty axes without --request-file")
    if args.task is None:
        return (f"--request-file needs --task <task>, the task to record the judgement under: oah recommend --kind "
                f"{args.kind} --task <task> --request-file <file>")
    if not TASK_RE.fullmatch(args.task):
        return (f"--task {args.task!r} is not a task name; use letters, digits, '.', '_' and '-' (at most 64), as in "
                f"oah recommend --kind {args.kind} --task <task> --request-file <file>")
    return None


def _read_request(path: pathlib.Path) -> str:
    """The user's request as UTF-8 text, refused over REQUEST_LIMIT bytes before it is decoded."""
    unreadable = "OAH_RECOMMEND_REQUEST_UNREADABLE"
    try:
        if not path.is_file():
            raise RequestError(unreadable, f"{path} is not a file; give the path of a UTF-8 text file that holds the "
                               "user's request")
        with open(path, "rb") as handle:
            data = handle.read(REQUEST_LIMIT + 1)
    except OSError as exc:
        raise RequestError(unreadable, f"{path} cannot be read ({exc.strerror or type(exc).__name__}); give the path "
                           "of a readable UTF-8 text file that holds the user's request") from None
    if len(data) > REQUEST_LIMIT:
        raise RequestError("OAH_RECOMMEND_REQUEST_TOO_LARGE", f"{path} is over 1 MB. Jev reads at most 16,000 "
                           "characters, so write the request itself to a smaller file and run the command again")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestError(unreadable, f"{path} is not UTF-8 (byte {exc.start}); save it as UTF-8 text and run the "
                           "command again") from None
    return text.removeprefix("﻿")


def _judge(args: argparse.Namespace) -> dict:
    """Jev's tier for design or packet work from the request, recorded under the task with no request text."""
    import datetime as dt
    import os

    from oah import __version__, call, routing
    from oah.atomic import write_json_atomic
    from oah.paths import repo_info, state_dir
    from oah.scope import normalize_scope

    text = _read_request(pathlib.Path(args.request_file))
    info = repo_info(pathlib.Path.cwd())
    state = state_dir(info, os.environ)
    scope = normalize_scope(args.scope, info.root) if args.scope else ()
    given = {axis: getattr(args, axis) for axis in AXIS_NAMES[1:] if getattr(args, axis) is not None}
    scored = routing.score_axes(stage="initial", kind=args.kind, scope=scope, overrides=given)
    rule = recommend(scored.axes)
    floor = routing.floor_tier(args.kind)
    difficulty = call.judge_difficulty(scored.axes, section=None, request_text=text, scope=scope, rule_tier=rule.tier,
                                       floor_tier=floor, root=info.root, state_dir=state, env=os.environ)
    warnings = [call.secret_warning(difficulty.note, "the request file")] if difficulty.status == "unused(secret)" else []
    tier, reasons = difficulty.tier, difficulty.reasons(floor)
    if not difficulty.used:
        # A wrong design costs every step after it, so unknown difficulty is not treated as easy.
        tier, reasons = ("max", [f"design unjudged: {difficulty.status}"]) if args.kind == "design" else (
            tier, [f"rule tier: jev {difficulty.status}"])
    record = {"kind": args.kind, "rule_tier": rule.tier, "jev_tier": difficulty.jev_tier,
              "jev_confidence": difficulty.confidence, "jev_status": difficulty.status, "tier": tier,
              "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
              "oah_version": __version__}
    write_json_atomic(judgement_path(state, args.task, args.kind), record)
    return {"tier": tier, "rule": rule, "axes": scored.axes, "rule_tier": rule.tier,
            "tier_source": "jev" if difficulty.used else "rules", "reasons": reasons, "warnings": warnings,
            "jev": {"status": difficulty.status, "tier": difficulty.jev_tier, "confidence": difficulty.confidence,
                    "truncated": difficulty.truncated}}


def _model(args: argparse.Namespace, tier: str) -> tuple[dict, tuple[str, ...]]:
    """The model a call would use. This reads the repository, so the plain tier answer never needs one."""
    import os

    from oah import call, call_cli
    from oah.paths import repo_info, state_dir

    info = repo_info(pathlib.Path.cwd())
    settings = call_cli.load_settings(info.root)
    engine = args.engine or settings.stages[args.stage]
    return call.preview(engine, args.stage, tier, args.kind, settings, state_dir=state_dir(info, os.environ),
                        env=os.environ)


def _print_model(model: dict) -> None:
    scores = f"{model['score_source']} {model['score_date']}" if model["score_source"] else "none"
    print(f"OAH_RECOMMEND_MODEL engine={model['engine']} stage={model['stage'] or '-'} tier={model['tier']} "
          f"model={model['model']} effort={model['effort']} source={model['source']} floor={model['floor_tier']} "
          f"profile={model['profile']} scores={scores}")
    if model["score_credit"]:
        print(f"  credit: {model['score_credit']}")


def run(args: argparse.Namespace) -> int:
    from oah.atomic import StateCorrupt, StorageError
    from oah.config import ConfigError
    from oah.engines.effort import EngineError
    from oah.lockfile import LockTimeout
    from oah.paths import GitMissing, NotARepo
    from oah.runs import RunError
    from oah.scope import ScopeError

    problem = _usage(args)
    if problem is not None:
        print(f"{USAGE}: {problem}", file=sys.stderr)
        return EXIT_USAGE
    model, warnings, judged = None, (), None
    try:
        if args.request_file is not None:
            judged = _judge(args)
            result, axes, tier = judged["rule"], judged["axes"], judged["tier"]
        else:
            axes = Axes(args.kind, args.intensity, args.reasoning, args.risk)
            result = recommend(axes)
            tier = result.tier
        if args.engine or args.stage:
            model, warnings = _model(args, tier)
    except (RoutingError, EngineError, ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt,
            StorageError, RequestError, RunError, ScopeError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    for warning in (*(judged["warnings"] if judged else ()), *warnings):
        print(warning, file=sys.stderr)
    if args.json:
        extra = {"model": model} if model is not None else {}
        if judged is not None:
            extra.update(tier=tier, rule_tier=judged["rule_tier"], tier_source=judged["tier_source"],
                         jev=judged["jev"], reason="; ".join(judged["reasons"]), task=args.task)
        print(json.dumps(dict(result.to_json(), axes=axes.to_json(), **extra), indent=2, ensure_ascii=True))
    else:
        if judged is None:
            print(f"OAH_RECOMMEND tier={result.tier} score={result.score} base={result.base}")
        else:
            print(f"OAH_RECOMMEND tier={tier} rule_tier={judged['rule_tier']} jev={judged['jev']['status']} "
                  f"score={result.score} base={result.base}")
        for reason in (*result.reasons, *(judged["reasons"] if judged else ())):
            print(f"  {reason}")
        if model is not None:
            _print_model(model)
    return EXIT_OK
