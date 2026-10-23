# 게이트 명령 — oah gates new/status/check/lint 의 인자, unlazy 위임, 구조 전용 대체 출력, 종료 코드를 맡는다.
"""gates: write, check and lint a task's gate ledger (unlazy format)."""
from __future__ import annotations

import argparse
import importlib.resources
import os
import pathlib
import sys

from oah.atomic import StateCorrupt, StorageError, write_text_atomic
from oah.config import ConfigError, load_config
from oah.gates import check as check_mod
from oah.gates.bindings import BindingStore
from oah.gates.context import DEFAULTS, GateTools, default_ledger, find_tools
from oah.gates.ledger import parse_ledger
from oah.gates.status import STRUCTURAL_LABEL, ledger_status, structural_status
from oah.lease import TASK_RE
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, RepoInfo, repo_info, state_dir
from oah import unlazy as unlazy_mod

COMMANDS = ("gates",)
EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
ENVIRONMENT_CODES = {"OAH_UNLAZY_FAILED", "OAH_UNLAZY_MISSING"}


class GatesError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def register(commands: argparse._SubParsersAction) -> None:
    gates = commands.add_parser("gates", help="write and check a task's gate ledger (unlazy format)")
    actions = gates.add_subparsers(dest="gates_command", metavar="<action>", required=True)
    new = actions.add_parser("new", help="write .oah/tasks/<task>/GATES.md from the template")
    new.add_argument("task")
    for name, text in (("status", "show which gates are met (never runs a CHECK)"),
                       ("check", "re-verify changed gates through unlazy and bind them to their inputs"),
                       ("lint", "judge whether the gates can fail (never runs a CHECK)")):
        action = actions.add_parser(name, help=text)
        action.add_argument("task")
        action.add_argument("--ledger", help="a ledger path other than .oah/tasks/<task>/GATES.md")
        if name == "check":
            action.add_argument("--approve", action="store_true",
                                help="approve the CHECK commands unlazy prints (inspect them first)")
            action.add_argument("--timeout", type=int,
                                help="seconds per CHECK (default: [gates] check_timeout, else "
                                     f"{DEFAULTS['check_timeout']})")
        if name == "lint":
            action.add_argument("--strict", action="store_true", help="treat lint warnings as failures")


def template_text(task: str) -> str:
    source = importlib.resources.files("oah").joinpath("templates/gates/GATES.md")
    return source.read_text(encoding="utf-8").replace("{task}", task)


def _task(task: str) -> str:
    if not TASK_RE.fullmatch(task or ""):
        raise GatesError("OAH_TASK_INVALID", f"{task!r}: use letters, digits, '.', '_' or '-' (max 64)")
    return task


def _ledger(args: argparse.Namespace, info: RepoInfo) -> pathlib.Path:
    if getattr(args, "ledger", None):
        path = pathlib.Path(args.ledger)
        return path if path.is_absolute() else pathlib.Path.cwd() / path
    return default_ledger(info.root, _task(args.task))


def _require_ledger(path: pathlib.Path, task: str) -> None:
    if not path.is_file():
        raise GatesError("OAH_GATES_MISSING", f"{path} does not exist; create it with oah gates new {task}")


def _echo(text: str, stream=None) -> None:
    if text.strip():
        print(text.rstrip("\n"), file=stream or sys.stdout)


def _new(args: argparse.Namespace, info: RepoInfo, tools: GateTools, settings: dict) -> int:
    task = _task(args.task)
    path = default_ledger(info.root, task)
    if path.exists():
        raise GatesError("OAH_GATES_EXISTS", f"{path} already exists; edit it, or delete it first to start over")
    write_text_atomic(path, template_text(task))
    print(f"OAH_GATES_CREATED {path}")
    print(f"Next: replace the example gates with your outcomes, then run oah gates lint {task}.")
    return EXIT_OK


def _status(args: argparse.Namespace, info: RepoInfo, tools: GateTools, settings: dict) -> int:
    ledger = _ledger(args, info)
    _require_ledger(ledger, args.task)
    if not tools.available:
        status = structural_status(parse_ledger(ledger))
        print(f"STRUCTURAL ONLY: {tools.reason}. Without unlazy oah can only read the boxes.")
        for gate_id, state in status.states:
            print(f"  {gate_id} {state}")
        for error in status.errors:
            print(f"  INVALID {error}")
        print(f"OAH_GATES_STATUS verdict={status.verdict} source=structural ({STRUCTURAL_LABEL})")
        return EXIT_OK if status.verdict == "met" else EXIT_REFUSED
    status = ledger_status(ledger, unlazy=tools.unlazy_dir, node=tools.node, root=info.root)
    _echo(status.output)
    if status.detail:
        print(status.detail)
    store = BindingStore(state_dir(info, os.environ), _task(args.task))
    fresh = check_mod.freshness(info.root, ledger, store, unlazy_dir=tools.unlazy_dir,
                                env_names=settings["input_env"]) if status.verdict != "invalid" else {}
    for gate_id, binding in fresh.items():
        print(f"OAH_GATE {gate_id} binding={binding}")
    stale = [gate_id for gate_id, binding in fresh.items() if binding in ("stale", "missing")]
    bindings = "fresh" if not stale else "stale"
    print(f"OAH_GATES_STATUS verdict={status.verdict} source=unlazy bindings={bindings}")
    if stale and status.verdict != "invalid":
        print(f"Next: run oah gates check {args.task} to re-verify {', '.join(stale)}.")
    return EXIT_OK if status.verdict == "met" and not stale else EXIT_REFUSED


def _check(args: argparse.Namespace, info: RepoInfo, tools: GateTools, settings: dict) -> int:
    ledger = _ledger(args, info)
    _require_ledger(ledger, args.task)
    if not tools.available:
        raise GatesError("OAH_UNLAZY_MISSING", f"{tools.reason}. oah gates status still gives a structural report")
    store = BindingStore(state_dir(info, os.environ), _task(args.task))
    report = check_mod.run_check(info.root, ledger, store, unlazy_dir=tools.unlazy_dir, node=tools.node,
                                 approve=args.approve, timeout=args.timeout or settings["check_timeout"],
                                 env_names=settings["input_env"])
    if report.unlazy is not None:
        _echo(report.unlazy.stdout)
        _echo(report.unlazy.stderr, sys.stderr)
    for line in report.lines:
        print(line)
    counts = {kind: sum(1 for action in report.actions.values() if action == kind) for kind in ("run", "attest", "carry")}
    if report.ok and not report.handoff:
        print(f"OAH_GATES_CHECKED task={args.task} run={counts['run']} attest={counts['attest']} "
              f"carry={counts['carry']}")
        return EXIT_OK
    if report.ok:
        print(f"OAH_GATES_HANDOFF_REQUIRED task={args.task} abandoned={','.join(report.handoff)}: these gates "
              "were abandoned; name them in your report and hand them to the owner")
        return EXIT_REFUSED
    open_gates = ",".join(f"{gate_id}({action})" for gate_id, action in report.actions.items()
                          if action not in ("run", "attest", "carry", "abandoned"))
    print(f"OAH_GATES_UNMET task={args.task} gates={open_gates}")
    if report.approval_required:
        print(f"Next: inspect the CHECK commands printed above, then run oah gates check {args.task} --approve.")
    if "invalidated" in report.actions.values():
        print("Files changed while the gates ran, so those results were discarded; run oah gates check again.")
    if "stale" in report.actions.values():
        print("A manual gate's inputs changed: re-check it by hand and update its EVIDENCE line.")
    return EXIT_REFUSED


def _lint(args: argparse.Namespace, info: RepoInfo, tools: GateTools, settings: dict) -> int:
    ledger = _ledger(args, info)
    _require_ledger(ledger, args.task)
    if not tools.available:
        parsed = parse_ledger(ledger)
        for error in parsed.errors:
            print(f"  ERROR {error}")
        label = f"({STRUCTURAL_LABEL}: {tools.reason})"
        print(f"LINT FINDINGS {label}" if parsed.errors else f"LINT OK {label}")
        return EXIT_REFUSED if parsed.errors else EXIT_OK
    result = unlazy_mod.gate_lint(tools.unlazy_dir, ledger, node=tools.node, strict=args.strict)
    _echo(result.stdout)
    _echo(result.stderr, sys.stderr)
    if result.verdict == "error":
        raise GatesError("OAH_UNLAZY_FAILED", f"unlazy gate-lint exited {result.returncode}")
    return EXIT_OK if result.verdict == "met" else EXIT_REFUSED


HANDLERS = {"new": _new, "status": _status, "check": _check, "lint": _lint}


def run(args: argparse.Namespace) -> int:
    try:
        info = repo_info(pathlib.Path.cwd())
        settings = load_config(info.root).section("gates", DEFAULTS)
        tools = find_tools(info.root, os.environ, pathlib.Path.home())
        return HANDLERS[args.gates_command](args, info, tools, settings)
    except (GatesError, check_mod.GateRefused) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT if exc.code in ENVIRONMENT_CODES else EXIT_REFUSED
    except unlazy_mod.UnlazyError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except (NotARepo, GitMissing, LockTimeout, StateCorrupt, StorageError, ConfigError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
