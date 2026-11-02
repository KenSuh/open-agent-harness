# recommend 명령 모듈 — oah recommend 의 네 축 인자를 받아 점수표로 고른 단과 그 근거를 출력하고, 엔진·단계를 주면 쓰게 될 모델도 기록 없이 보여 준다.
"""routing"""
from __future__ import annotations

import argparse
import json
import sys

from oah.routing import (ENGINES, INTENSITY_SCORES, KIND_SCORES, REASONING_SCORES, RISK_SCORES, STAGES, Axes,
                         RoutingError, recommend)

EXIT_OK, EXIT_REFUSED = 0, 1
COMMANDS = ("recommend",)


def add_axis_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    """The four difficulty axes, shared with `oah call`."""
    parser.add_argument("--kind", choices=tuple(KIND_SCORES), required=required, help="what kind of work")
    parser.add_argument("--intensity", choices=tuple(INTENSITY_SCORES), required=required, help="how much work")
    parser.add_argument("--reasoning", choices=tuple(REASONING_SCORES), required=required,
                        help="how much thinking it needs")
    parser.add_argument("--risk", choices=tuple(RISK_SCORES), required=required, help="how bad a mistake would be")


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("recommend", help="pick a tier (low, medium, high, max) from four difficulty axes")
    add_axis_arguments(parser, required=True)
    parser.add_argument("--engine", choices=ENGINES, help="also show the model this engine would use (launches nothing)")
    parser.add_argument("--stage", choices=STAGES, help="the stage to apply, with its floor; alone, it names its engine")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")


def _model(args: argparse.Namespace, tier: str) -> tuple[dict, tuple[str, ...]]:
    """The model a call would use. This reads the repository, so the plain tier answer never needs one."""
    import os
    import pathlib

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

    axes = Axes(args.kind, args.intensity, args.reasoning, args.risk)
    model, warnings = None, ()
    try:
        result = recommend(axes)
        if args.engine or args.stage:
            model, warnings = _model(args, result.tier)
    except (RoutingError, EngineError, ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt,
            StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    for warning in warnings:
        print(warning, file=sys.stderr)
    if args.json:
        extra = {"model": model} if model is not None else {}
        print(json.dumps(dict(result.to_json(), axes=axes.to_json(), **extra), indent=2, ensure_ascii=True))
    else:
        print(f"OAH_RECOMMEND tier={result.tier} score={result.score} base={result.base}")
        for reason in result.reasons:
            print(f"  {reason}")
        if model is not None:
            _print_model(model)
    return EXIT_OK
