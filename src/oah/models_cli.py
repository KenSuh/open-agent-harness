# models 명령 모듈 — 엔진별 모델 카탈로그를 확인·새로 읽고, 단마다 고른 모델·effort 와 그 출처를 출력한다.
"""models"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from oah import call, routing
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
    report = {"cli_version": entry.cli_version, "checked_at": entry.checked_at, "source": entry.source,
              "refreshed": checked[1].refreshed, "warnings": list(checked[1].warnings),
              "models": [model.to_json() for model in entry.models], "tiers": {}}
    for tier in routing.TIERS:
        try:
            resolution = call.resolve_engine(engine, tier, settings, root=root, state_dir=state, env=os.environ,
                                             checked=checked)
        except EngineError as exc:
            report["tiers"][tier] = {"error": str(exc)}
            continue
        report["tiers"][tier] = {"model": resolution.model, "model_source": resolution.model_source,
                                 "requested_effort": resolution.requested_effort, "effort": resolution.launch_effort}
    return report


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
        for tier, row in report["tiers"].items():
            if "error" in row:
                print(f"  {tier:6} {row['error']}")
            else:
                print(f"  {tier:6} {row['model']} ({row['model_source']}) effort {row['effort']}"
                      f"{'' if row['effort'] == row['requested_effort'] else ' from ' + row['requested_effort']}")
    return EXIT_OK
