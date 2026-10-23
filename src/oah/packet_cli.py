# 패킷 명령 — oah packet new/check 의 인자, 저장소 루트 찾기, 결과 출력과 종료 코드를 맡는다.
# `oah packet new` refreshes the impact map first (A11), then scores each stage's difficulty axes by rules
# (A10), selects and optionally re-ranks its skills (A9, A15) and records all of it in `## Plan`. No module
# docstring: `oah --help` names command groups by it.
from __future__ import annotations

import argparse
import os
import pathlib
import sys

from oah import packet
from oah.config import ConfigError
from oah.paths import GitMissing, NotARepo, repo_info, state_dir
from oah.preflight import ImpactRefused, ImpactUnavailable, refresh_impact
from oah.recommend_cli import add_axis_arguments

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("packet",)


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("packet", help="create or check a task packet (.oah/tasks/<task>/PACKET.md)")
    actions = parser.add_subparsers(dest="packet_command", metavar="<action>", required=True)
    new = actions.add_parser("new", help="scaffold a draft packet for a task, with its plan scored by rules")
    new.add_argument("task")
    new.add_argument("--scope", action="append", default=[], metavar="PATH",
                     help="a path this task may write, or 'repo'; repeat for more (fills ## Scope)")
    add_axis_arguments(new, required=False)
    new.add_argument("--no-rerank", action="store_true", help="keep the rule order of the skills; the re-ranker is "
                     "not asked (recorded as rerank=unused(disabled-by-brief))")
    check = actions.add_parser("check", help="check a packet's intent, sections, scope and gate ledger")
    target = check.add_mutually_exclusive_group(required=True)
    target.add_argument("task", nargs="?", help="the task whose packet to check")
    target.add_argument("--file", help="check this packet file instead")
    check.add_argument("--allow-draft", action="store_true", help="check the structure of a draft packet")


def run(args: argparse.Namespace) -> int:
    try:
        if args.packet_command == "new":
            return _new(args)
        return _check(args)
    except packet.PacketError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except ImpactRefused as exc:
        print(f"{exc}. Nothing was written.", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except (NotARepo, GitMissing, ConfigError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(f"OAH_PACKET_IO: {exc}. Check the folder's permissions and free disk space, then retry.",
              file=sys.stderr)
        return EXIT_ENVIRONMENT


def display(path: pathlib.Path, root: pathlib.Path | None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return str(path)


def _stage_plans(args: argparse.Namespace, root: pathlib.Path, state: pathlib.Path,
                 scope: tuple[str, ...]) -> list[packet.StagePlan]:
    """Each stage scored by rules: its engine, axes, tier, advice and skills (A9, A10)."""
    from oah import call, routing, skillsel
    from oah.config import load_config

    stages = load_config(root).section("stages", call.STAGE_DEFAULTS)
    given = {axis: getattr(args, axis) for axis in ("intensity", "reasoning", "risk") if getattr(args, axis)}
    plans = []
    for stage in routing.STAGES:
        # The task's kind belongs to the implementing stages; the review stage keeps the review kind.
        kind = args.kind if args.kind and stage != "review" else None
        scored = routing.score_axes(stage=stage, kind=kind, scope=scope, overrides=given)
        recommendation = routing.recommend(scored.axes)
        selection, status = skillsel.choose(root, kind=scored.axes.kind, stage=stage, risk=scored.axes.risk,
                                            scope=scope, state_dir=state, env=os.environ,
                                            brief_disabled=args.no_rerank)
        plans.append(packet.StagePlan(stage, stages.get(stage, call.STAGE_DEFAULTS[stage]), scored.axes,
                                      recommendation.tier, tuple(scored.reasons) + recommendation.reasons,
                                      call.advise(scored.axes, args.task, root=root, brief_disabled=args.no_rerank,
                                                  env=os.environ), selection, status))
    return plans


def _new(args: argparse.Namespace) -> int:
    from oah.scope import normalize_scope

    info = repo_info(pathlib.Path.cwd())
    root = info.root
    path = packet.packet_path(root, args.task)
    if os.path.lexists(path):
        raise packet.PacketError("OAH_PACKET_EXISTS", f"{display(path, root)} already exists; edit it, and raise "
                                                      "its Revision when the goal changes")
    scope = normalize_scope(args.scope, root) if args.scope else ()
    try:
        impact = refresh_impact(root)
        impact_line = f"impact=map {impact.action} at {impact.commit[:12]}"
    except ImpactUnavailable as exc:
        impact_line = f"impact=unavailable({exc})"
    plans = _stage_plans(args, root, state_dir(info, os.environ), scope)
    path = packet.create(root, args.task, plan=packet.plan_text(plans), scope=scope)
    for warning in dict.fromkeys(w for plan in plans for w in plan.selection.warnings if "OAH_SCOPE_EMPTY" in w):
        print(f"warning {warning}", file=sys.stderr)
    print(impact_line)
    print(f"OAH_PACKET_CREATED {display(path, root)}")
    print("plan: " + "; ".join(f"{plan.stage} → {plan.engine} → {plan.tier}" for plan in plans))
    print("skills: " + " ".join(f"{plan.stage}={','.join(s.id for s in plan.selection.skills) or 'none'}"
                                for plan in plans) + f" rerank={plans[0].rerank}")
    print("Next: describe the problem, the outcome and checkable success conditions, check the Scope paths and "
          f"the plan, then change 'Status: draft' to 'Status: accepted' and run: oah packet check {args.task}")
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    if args.file:
        path = pathlib.Path(args.file).absolute()
        task = packet.task_of(path)
        try:
            root = repo_info(path.parent if path.parent.is_dir() else pathlib.Path.cwd()).root
        except (NotARepo, GitMissing):
            root = None
    else:
        root = repo_info(pathlib.Path.cwd()).root
        task = args.task
        path = packet.packet_path(root, task)
    result = packet.check(path, task=task, require_accepted=not args.allow_draft)
    for problem in result.problems:
        print(f"{problem.level} {problem}")
    shown = display(path, root)
    if any(p.code == "OAH_INTENT_SOURCE_MISSING" for p in result.errors) and task is not None:
        print(f"Create it with: oah packet new {task}")
    if result.errors:
        print(f"OAH_PACKET_INVALID {shown} errors={len(result.errors)} warnings={len(result.warnings)}")
        return EXIT_REFUSED
    parsed = result.intent
    print(f"OAH_PACKET_OK {shown} intent={parsed.id}@r{parsed.revision} status={parsed.status} "
          f"acs={len(parsed.acs)} scope={len(result.scope)} warnings={len(result.warnings)}")
    return EXIT_OK
