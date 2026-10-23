# call·wait 명령 모듈 — .oah/config.toml 의 [stages]·[engines.*]·[models] 를 읽어 엔진 호출을 준비·실행하고, 백그라운드 실행을 최대 9분씩 기다린다.
"""engines"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Callable, NamedTuple

from oah import briefs, call, models, routing
from oah.atomic import StateCorrupt, StorageError
from oah.engines.effort import EngineError
from oah.journal import PendingTransaction
from oah.lease import LeaseError
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, repo_info, state_dir
from oah.recommend_cli import add_axis_arguments
from oah.runs import TERMINAL, RunError

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT, EXIT_AGAIN = 0, 1, 2, 69, 75
COMMANDS = ("call", "wait")
ENVIRONMENT_CODES = ("OAH_ENGINE_MISSING", "OAH_CONFIG_INVALID", "OAH_CODEX_CONFIG_INVALID",
                     "OAH_UNSUPPORTED_BATCH_SHIM", "OAH_PACKET_UNREADABLE", "OAH_INTENT_SOURCE_UNREADABLE",
                     "OAH_RUN_DETACH_FAILED")


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("call", help="launch Claude Code or Codex for one stage and record the run")
    parser.add_argument("engine", choices=routing.ENGINES)
    parser.add_argument("--stage", required=True, choices=routing.STAGES)
    parser.add_argument("--task", default=call.DEFAULT_TASK, help=f"the task this run belongs to (default {call.DEFAULT_TASK})")
    parser.add_argument("--tier", choices=routing.TIERS, help="use this tier instead of scoring the difficulty (recorded)")
    add_axis_arguments(parser, required=False)
    parser.add_argument("--message", default="", help="round instructions for the worker")
    parser.add_argument("--prompt-file", help="read the round instructions from this file ('-' reads stdin)")
    parser.add_argument("--brief-file", help="reuse this brief or spec inside a fresh dispatch envelope "
                        "with the current stage, run and canonical footers")
    parser.add_argument("--packet", help="the task packet (default: the source bound at the task's first dispatch, "
                        "else .oah/tasks/<task>/PACKET.md when it exists)")
    parser.add_argument("--worktree", help="run in this worktree of the repository: a path, a branch or a worktree "
                        "folder name that names exactly one worktree")
    parser.add_argument("--override-stage", action="store_true",
                        help="run an engine that is not configured for this stage (recorded in the events)")
    parser.add_argument("--background", action="store_true", help="return the run id at once; then use oah wait")
    parser.add_argument("--timeout-seconds", type=float, help="stop the run after this many seconds (default: the "
                        "tier's timeout_min)")
    parser.add_argument("--refresh-models", action="store_true", help="rebuild the model catalog before launching")
    parser.add_argument("--json", action="store_true", help="print the run record as JSON")

    wait = commands.add_parser("wait", help="wait up to 9 minutes for a background run; call it again if needed")
    wait.add_argument("run_id")
    wait.add_argument("--timeout-seconds", type=float, default=call.WAIT_LIMIT_S,
                      help=f"wait at most this long (at most {call.WAIT_LIMIT_S:g} s)")
    wait.add_argument("--json", action="store_true", help="print the run record as JSON")


def load_settings(root: pathlib.Path) -> call.Settings:
    from oah.config import load_config

    config = load_config(root)
    return call.Settings.validated(config.section("stages", call.STAGE_DEFAULTS),
                                   config.section("engines", call.ENGINE_DEFAULTS),
                                   config.section("models", models.DEFAULTS))


class IntentTools(NamedTuple):
    footer: str
    has_footer: Callable[[str], bool]
    parse: Callable[[str], object]
    read_source: Callable[[pathlib.Path], str]


def intent_tools() -> IntentTools:
    """The shared intent footer, its presence test, the parser and the safe source reader (oah.intent, lane P4a)."""
    footer = briefs.intent_footer()
    from oah import intent

    return IntentTools(footer, briefs.has_intent_footer, intent.parse, intent.read_source)


def retry_command(args: argparse.Namespace, worktree: str) -> str:
    """This `oah call` command line again, with an exact --worktree."""
    words = ["oah", "call", args.engine, "--stage", args.stage, "--task", args.task]
    for name in ("tier", "kind", "intensity", "reasoning", "risk", "message", "prompt_file", "brief_file", "packet",
                 "timeout_seconds"):
        value = getattr(args, name)
        if value not in (None, ""):
            words += ["--" + name.replace("_", "-"), f"{value:g}" if isinstance(value, float) else str(value)]
    for name in ("override_stage", "background", "refresh_models", "json"):
        if getattr(args, name):
            words.append("--" + name.replace("_", "-"))
    words += ["--worktree", worktree]
    return " ".join(briefs.shell_word(word) for word in words)


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.buffer.read().decode("utf-8-sig")
    return pathlib.Path(path).read_bytes().decode("utf-8-sig")  # bytes keep a stored brief's line endings


def _summary(data: dict) -> str:
    fields = ("status", "engine", "stage", "tier", "tier_source", "resolved_model", "launch_effort", "header_check")
    return f"run={data.get('run_id')} " + " ".join(f"{name}={data.get(name)}" for name in fields)


def _report(data: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=True))
    else:
        word = "OAH_RUN_FINISHED" if data.get("status") == "finished" else "OAH_RUN_" + str(data.get("status")).upper()
        print(f"{word} {_summary(data)}")
        if data.get("error"):
            print(data["error"], file=sys.stderr)
    return EXIT_OK if data.get("status") == "finished" else EXIT_REFUSED


def _call(args: argparse.Namespace, root: pathlib.Path, state: pathlib.Path) -> int:
    axes = {name: getattr(args, name) for name in ("kind", "intensity", "reasoning", "risk")}
    if args.tier and any(axes.values()):
        print("OAH_USAGE: give either --tier or the difficulty axes, not both", file=sys.stderr)
        return EXIT_USAGE
    if args.message and args.prompt_file:
        print("OAH_USAGE: give either --message or --prompt-file, not both", file=sys.stderr)
        return EXIT_USAGE
    if args.worktree:
        root = call.resolve_worktree(args.worktree, root, retry=lambda path: retry_command(args, path))
        state = state_dir(repo_info(root), os.environ)
    settings = load_settings(root)
    tools = intent_tools()
    request = call.CallRequest(
        engine=args.engine, stage=args.stage, task=args.task, tier=args.tier, axes=axes,
        override_stage=args.override_stage,
        round_instructions=_read(args.prompt_file) if args.prompt_file else args.message,
        stored_brief=_read(args.brief_file) if args.brief_file else None,
        packet=(root / args.packet) if args.packet else None, timeout_s=args.timeout_seconds,
        refresh_models=args.refresh_models)
    prepared = call.prepare(request, settings, root=root, state_dir=state, env=os.environ, footer=tools.footer,
                            has_footer=tools.has_footer, parse_intent=tools.parse, read_source=tools.read_source)
    for warning in prepared.warnings:
        print(warning, file=sys.stderr)
    if args.background:
        run_id = call.start_background(prepared)
        print(f"OAH_RUN_STARTED run={run_id} folder={prepared.run_dir}")
        print(f"Wait for it with: oah wait {run_id}")
        return EXIT_OK
    return _report(call.execute(prepared), args.json)


def _wait(args: argparse.Namespace, root: pathlib.Path, state: pathlib.Path) -> int:
    data = call.wait(state, args.run_id, timeout=args.timeout_seconds)
    if data.get("status") in TERMINAL:
        return _report(data, args.json)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=True))
    else:
        print(f"OAH_RUN_RUNNING {_summary(data)}")
        print(f"Still running; call `oah wait {args.run_id}` again.", file=sys.stderr)
    return EXIT_AGAIN


def run(args: argparse.Namespace) -> int:
    from oah.config import ConfigError

    try:
        info = repo_info(pathlib.Path.cwd())
        state = state_dir(info, os.environ)
        return (_call if args.command == "call" else _wait)(args, info.root, state)
    except EngineError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT if exc.code in ENVIRONMENT_CODES else EXIT_REFUSED
    except (routing.RoutingError, briefs.BriefError, RunError, LeaseError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except (ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, PendingTransaction, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(StorageError(pathlib.Path(exc.filename or "."), exc), file=sys.stderr)
        return EXIT_ENVIRONMENT
