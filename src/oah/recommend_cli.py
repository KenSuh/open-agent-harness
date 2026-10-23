# recommend 명령 모듈 — oah recommend 의 네 축 인자를 받아 점수표로 고른 단과 그 근거를 출력한다.
"""Difficulty routing: recommend."""
from __future__ import annotations

import argparse
import json
import sys

from oah.routing import INTENSITY_SCORES, KIND_SCORES, REASONING_SCORES, RISK_SCORES, Axes, RoutingError, recommend

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
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")


def run(args: argparse.Namespace) -> int:
    axes = Axes(args.kind, args.intensity, args.reasoning, args.risk)
    try:
        result = recommend(axes)
    except RoutingError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    if args.json:
        print(json.dumps(dict(result.to_json(), axes=axes.to_json()), indent=2, ensure_ascii=True))
    else:
        print(f"OAH_RECOMMEND tier={result.tier} score={result.score} base={result.base}")
        for reason in result.reasons:
            print(f"  {reason}")
    return EXIT_OK
