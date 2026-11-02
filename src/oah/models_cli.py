# models 명령 모듈 — 엔진별 모델 카탈로그를 확인·새로 읽고, 단마다 고른 모델·effort 와 그 출처, 판정 후보·상한을 출력한다.
"""models"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from oah import call, models, routing, scoring
from oah.atomic import StateCorrupt, StorageError
from oah.engines.effort import EngineError
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, repo_info, state_dir

EXIT_OK, EXIT_ENVIRONMENT = 0, 69
COMMANDS = ("models",)


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("models", help="show (and refresh) the model each tier uses for each engine")
    parser.add_argument("--refresh", action="store_true", help="read the CLIs' model lists again now")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")


def _engine_report(engine: str, settings: call.Settings, root: pathlib.Path, state: pathlib.Path,
                   refresh: bool) -> dict:
    try:
        checked = call.check_catalog(engine, settings, state_dir=state, env=os.environ, refresh=refresh)
    except EngineError as exc:
        return {"error": str(exc)}
    entry = checked[1].entry
    section = settings.engines[engine]
    unpooled = models.unpooled_models(entry, section["tier_pools"])
    report = {"cli_version": entry.cli_version, "checked_at": entry.checked_at, "source": entry.source,
              "refreshed": checked[1].refreshed, "warnings": list(checked[1].warnings),
              "models": [model.to_json() for model in entry.models], "allowed_models": list(section["allowed_models"]),
              "effort_caps": dict(section["effort_caps"]), "tier_pools": dict(section["tier_pools"]), "tiers": {},
              "unpooled": unpooled}
    for tier in routing.TIERS:
        try:
            resolution = call.resolve_engine(engine, tier, settings, state_dir=state, env=os.environ, checked=checked,
                                              floor_tier=tier)
            floor = models.Pair(resolution.model, resolution.launch_effort)
            candidates = None
            if not section["tiers"][tier]["model"]:
                # The candidates a judge may pick from for a call whose floor tier is this tier (amendment 6).
                candidates = models.build_candidates(
                    engine, entry=entry, deterministic=floor, tier=tier, floor_tier=tier,
                    floor=floor, quality_order=section["quality_order"], allowed_models=section["allowed_models"],
                    effort_caps=section["effort_caps"], tier_pools=section["tier_pools"],
                    scores=checked[1].scores, profile=settings.models["profile"],
                    tier_effort=section["tiers"][tier]["effort"], floor_effort=section["tiers"][tier]["effort"])
        except EngineError as exc:
            report["tiers"][tier] = {"error": str(exc)}
            continue
        report["warnings"] += [warning for warning in resolution.warnings if warning not in report["warnings"]]
        report["tiers"][tier] = {"model": resolution.model, "model_source": resolution.model_source,
                                 "requested_effort": resolution.requested_effort, "effort": resolution.launch_effort,
                                 "floor": {"tier": tier, **floor.to_json()},
                                 "mode": candidates.mode if candidates is not None else None,
                                 "note": candidates.note if candidates is not None else "unused(pinned)",
                                 "candidates": [[item.model, item.effort] for item in candidates.pairs]
                                 if candidates is not None else [],
                                 "excluded": dict(candidates.excluded) if candidates is not None else {}}
    # This report always names them: a launch does so only after a rebuild (call.check_catalog).
    notices = [models.unpooled_warning(engine, name) for name in unpooled]
    ignored = models.order_notice(engine, section["quality_order"])
    if ignored is not None:
        notices.append(ignored)
    scores = checked[1].scores
    if not scores.models:
        notices.append(f"{scoring.NO_SCORES}: {scores.failure}")
    report["warnings"] += [notice for notice in notices if notice not in report["warnings"]]
    report.update(models.score_report(scores, settings.models["profile"],
                                      left_out=call._effort_left_out(engine, settings, checked[1])))
    return report


def _score_lines(report: dict) -> list[str]:
    scores = report["scores"]
    if scores is None:
        lines = [f"profile {report['profile']} · no usable scores"]
    else:
        rows = ", ".join(f"{row['model']} {row['score']:.1f}" for row in scores["models"])
        lines = [f"profile {report['profile']} · scores {scores['source']} {scores['date']}: {rows}",
                 f"credit: {scores['credit']}"]
    if report["left_out"]:
        lines.append("left out: " + " · ".join(f"{name} ({why})" for name, why in report["left_out"].items()))
    return lines


def _candidates_text(pairs: list) -> str:
    grouped: dict[str, list[str]] = {}
    for model, effort in pairs:
        grouped.setdefault(model, []).append(effort)
    return " · ".join(f"{model} ({', '.join(efforts)})" for model, efforts in grouped.items()) or "none"


def run(args: argparse.Namespace) -> int:
    from oah.call_cli import load_settings
    from oah.config import ConfigError

    try:
        info = repo_info(pathlib.Path.cwd())
        state = state_dir(info, os.environ)
        settings = load_settings(info.root)
        reports = {engine: _engine_report(engine, settings, info.root, state, args.refresh)
                   for engine in routing.ENGINES}
    except (EngineError, ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    for engine, report in reports.items():
        for warning in report.get("warnings", ()):
            print(warning, file=sys.stderr)
    if args.json:
        print(json.dumps({"engines": reports}, indent=2, ensure_ascii=True))
        return EXIT_OK
    for engine, report in reports.items():
        if "error" in report:
            print(f"{engine}: {report['error']}")
            continue
        names = ", ".join(model["name"] for model in report["models"]) or "none reported"
        print(f"{engine}: CLI {report['cli_version']} · checked {report['checked_at']} · models: {names}")
        caps = ", ".join(f"{model}={effort}" for model, effort in report["effort_caps"].items()) or "none"
        pools = "; ".join(f"{tier}: {', '.join(names)}" for tier, names in report["tier_pools"].items() if names)
        print(f"  judge: allowed {', '.join(report['allowed_models']) or 'any'} · caps {caps} · pools {pools or 'none'}")
        for line in _score_lines(report):
            print(f"  {line}")
        for tier, row in report["tiers"].items():
            if "error" in row:
                print(f"  {tier:6} {row['error']}")
            else:
                print(f"  {tier:6} {row['model']} ({row['model_source']}) effort {row['effort']}"
                      f"{'' if row['effort'] == row['requested_effort'] else ' from ' + row['requested_effort']}")
                print(f"         candidates at floor {tier}: {_candidates_text(row['candidates'])}")
                if row["excluded"]:
                    left = " · ".join(f"{model} ({rule})" for model, rule in row["excluded"].items())
                    print(f"         not candidates at floor {tier}: {left}")
                if row["note"]:
                    print(f"         {row['note']}")
    return EXIT_OK
