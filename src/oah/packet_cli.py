# 패킷 명령 — oah packet new/check 의 인자, 저장소 루트 찾기, 결과 출력과 종료 코드를 맡는다.
# `oah packet new` refreshes the impact map first (A11), then scores each stage's difficulty axes by rules
# (A10), selects and optionally re-ranks its skills (A9, A15) and records all of it in `## Plan`. It warns
# OAH_DESIGN_UNJUDGED, never blocking, when `oah recommend --request-file` recorded no design judgement for the
# task. `--design <project>/<design>` links the packet to a design: a `design_origin` mark is recorded before the
# file is written (pending, then linked), so the task stays design-origin even if the section is removed later.
# `oah packet design <task>` rewrites only that section. No module docstring: `oah --help` names command groups by it.
from __future__ import annotations

import argparse
import os
import pathlib
import sys

from oah import packet
from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError
from oah.journal import PendingTransaction
from oah.lease import LeaseError
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, repo_info, state_dir
from oah.preflight import ImpactRefused, ImpactUnavailable, refresh_impact
from oah.recommend_cli import add_axis_arguments, judgement_path
from oah.wiki.base import WikiError

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
    new.add_argument("--design", metavar="PROJECT/DESIGN", help="link the packet to this design: a ## Design section "
                     "names the revision the requirements are written from")
    new.add_argument("--design-revision", type=int, metavar="N", help="the design revision to name (default: the "
                     "accepted revision, else the newest)")
    check = actions.add_parser("check", help="check a packet's intent, sections, scope and gate ledger")
    target = check.add_mutually_exclusive_group(required=True)
    target.add_argument("task", nargs="?", help="the task whose packet to check")
    target.add_argument("--file", help="check this packet file instead")
    check.add_argument("--allow-draft", action="store_true", help="check the structure of a draft packet")
    design = actions.add_parser("design", help="rewrite a packet's ## Design section to a newer revision of its "
                                "design (the accepted one by default)")
    design.add_argument("task")
    design.add_argument("--design", metavar="PROJECT/DESIGN", help="link a packet that has no ## Design section yet")
    design.add_argument("--revision", type=int, metavar="N", help="the revision to name (default: the accepted one)")


def run(args: argparse.Namespace) -> int:
    try:
        if args.packet_command == "new":
            return _new(args)
        if args.packet_command == "design":
            return _design(args)
        return _check(args)
    except (packet.PacketError, LeaseError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except WikiError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except ImpactRefused as exc:
        print(f"{exc}. Nothing was written.", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except (NotARepo, GitMissing, ConfigError, StateCorrupt, PendingTransaction, LockTimeout, StorageError) as exc:
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
                                      call.advise(scored.axes, scope, root=root, state_dir=state,
                                                  brief_disabled=args.no_rerank, env=os.environ), selection, status))
    return plans


def _packet_design(root: pathlib.Path, spec: str, revision: int | None):
    """The PacketDesign for PROJECT/DESIGN at `revision` (default: accepted, else newest) and whether it is accepted."""
    from oah.design.binding import PacketDesign
    from oah.design.model import (FRESHNESS_UNKNOWN_CODE, INPUT_INVALID_CODE, NOT_FOUND_CODE, UNREGISTERED_EDIT_CODE,
                                  DesignError, check_id, check_revision)
    from oah.design.store import open_store

    project, separator, design = (spec or "").partition("/")
    if not separator:
        raise DesignError(INPUT_INVALID_CODE, f"--design {spec!r}: write PROJECT/DESIGN, such as acme/booking")
    check_id(project)
    check_id(design, "design")
    context = open_store(root).context(project, design)
    if context.freshness == "unknown" and context.source != "reader":
        raise DesignError(FRESHNESS_UNKNOWN_CODE, f"the authority copy of {project}/{design} cannot be read, so the "
                          "packet would name a revision that may not be current; make the authority branch readable")
    if context.page_state in ("edited", "foreign"):
        raise DesignError(UNREGISTERED_EDIT_CODE, f"{context.page} was edited after oah wrote it; register the edit "
                          f"with oah design save {project} {design} --from-wiki --expect-revision <N> --expect-digest "
                          "<digest> --reason <why>, or delete the page and run oah design reconcile, then link again")
    if revision is None:
        chosen = context.accepted or context.current
        number, digest = chosen.revision, chosen.digest
    else:
        check_revision(revision)
        info = next((item for item in context.revisions if item.revision == revision), None)
        if info is None:
            raise DesignError(NOT_FOUND_CODE, f"revision {revision} is not in the history of {project}/{design}; "
                              f"oah design status {project} {design} lists it")
        number, digest = info.revision, info.digest
    accepted = context.accepted is not None and context.accepted.revision == number
    return PacketDesign(project, context.project.uid, design, number, digest), accepted


def _linked(found, accepted: bool) -> str:
    note = "accepted" if accepted else f"draft: oah start waits until revision {found.revision} is accepted"
    return f"OAH_DESIGN_LINKED {found.line()} ({note})"


def _write_linked(state: pathlib.Path, root: pathlib.Path, task: str, source: str, found, write) -> None:
    """Mark the design origin before the write (pending), then linked, or error after a handled failure.

    The marks share one operation ID and the task's instance: the current one on a relink, a new one only for a new
    undertaking (B1). They establish design origin only; they never bind a lease or authorize work."""
    import secrets

    from oah.design import binding as design
    from oah.lease import Store
    from oah.lease_cli import _instance

    store = Store(state)
    view = design.task_view(state, task)
    identity = view.lease.design_record().identity if (view.lease is not None and view.lease.design is not None) \
        else _instance(view, None, design.repo_identity(root), task)[0]
    operation = secrets.token_hex(8)

    def mark(status: str, problem: str | None = None) -> None:
        store.mark_design_origin(task=task, task_identity=identity.to_json(), origin={
            "status": status, "packet_source": source, "packet_design": found.to_json(), "operation_id": operation,
            "problem_code": problem})

    mark("pending")
    try:
        write()
    except (packet.PacketError, OSError) as exc:
        mark("error", getattr(exc, "code", None) or "OAH_PACKET_IO")
        raise
    mark("linked")


def _new(args: argparse.Namespace) -> int:
    from oah.scope import normalize_scope

    info = repo_info(pathlib.Path.cwd())
    root = info.root
    path = packet.packet_path(root, args.task)
    if os.path.lexists(path):
        raise packet.PacketError("OAH_PACKET_EXISTS", f"{display(path, root)} already exists; edit it, and raise "
                                                      "its Revision when the goal changes")
    if args.design_revision is not None and not args.design:
        raise packet.PacketError("OAH_USAGE", "--design-revision needs --design PROJECT/DESIGN")
    linked = _packet_design(root, args.design, args.design_revision) if args.design else None
    scope = normalize_scope(args.scope, root) if args.scope else ()
    try:
        impact = refresh_impact(root)
        impact_line = f"impact=map {impact.action} at {impact.commit[:12]}"
    except ImpactUnavailable as exc:
        impact_line = f"impact=unavailable({exc})"
    state = state_dir(info, os.environ)
    plans = _stage_plans(args, root, state, scope)
    if linked is None:
        path = packet.create(root, args.task, plan=packet.plan_text(plans), scope=scope)
    else:
        source = display(path, root)
        _write_linked(state, root, args.task, source, linked[0], lambda: packet.create(
            root, args.task, plan=packet.plan_text(plans), scope=scope, design=linked[0]))
    for warning in dict.fromkeys(w for plan in plans for w in plan.selection.warnings if "OAH_SCOPE_EMPTY" in w):
        print(f"warning {warning}", file=sys.stderr)
    if not _design_judged(state, args.task):
        print(f"OAH_DESIGN_UNJUDGED: no design judgement is recorded for task {args.task}; before writing the design, "
              f"run oah recommend --kind design --task {args.task} --request-file <your request file>", file=sys.stderr)
    print(impact_line)
    print(f"OAH_PACKET_CREATED {display(path, root)}")
    if linked is not None:
        print(_linked(*linked))
        print(f"Write the intent from the design: oah design show {linked[0].project} {linked[0].design} "
              f"--revision {linked[0].revision} prints it.")
    print("plan: " + "; ".join(f"{plan.stage} → {plan.engine} → {plan.tier}" for plan in plans))
    print("skills: " + " ".join(f"{plan.stage}={','.join(s.id for s in plan.selection.skills) or 'none'}"
                                for plan in plans) + f" rerank={plans[0].rerank}")
    print("Next: describe the problem, the outcome and checkable success conditions, check the Scope paths and "
          f"the plan, then change 'Status: draft' to 'Status: accepted' and run: oah packet check {args.task}")
    return EXIT_OK


def _design_judged(state: pathlib.Path, task: str) -> bool:
    """Whether `oah recommend --request-file` recorded a design judgement for the task; unreadable reads as none."""
    try:
        return judgement_path(state, task, "design").is_file()
    except Exception:  # a warning never blocks the packet
        return False


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
    if result.design is not None and root is not None:
        for line in _design_lines(root, result.design):
            print(line)
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


def _design_lines(root: pathlib.Path, found) -> list[str]:
    """A read-only line on the design the packet names; a revision behind the accepted one is a warning."""
    from oah.design import binding as design

    context = design.read_context(found.binding(), root=root)
    if isinstance(context, design.TaskDesign):
        return [f"design: {found.line()} (packet) — {context.state}: {context.reason}"]
    accepted = f"accepted r{context.accepted.revision}" if context.accepted is not None else "nothing accepted"
    known = any(info.revision == found.revision and info.digest == found.digest for info in context.revisions)
    state = ("invalid" if context.project.uid != found.uid or not known else
             "not accepted" if context.accepted is None or found.revision > context.accepted.revision else
             "behind" if found.revision < context.accepted.revision else "accepted")
    lines = [f"design: {found.line()} (packet) — {accepted}, working r{context.current.revision}: {state}"]
    if state == "behind":
        lines.append(f"warning OAH_DESIGN_PACKET_STALE: the packet was written from revision {found.revision}, and "
                     f"revision {context.accepted.revision} is accepted; if the requirements changed, rewrite the "
                     "Design section with oah packet design <task>, raise the intent Revision and start again")
    elif state == "not accepted":
        lines.append(f"warning OAH_DESIGN_NOT_ACCEPTED: revision {found.revision} is not accepted; oah start --packet "
                     "binds only an accepted revision")
    elif state == "invalid":
        lines.append("warning OAH_DESIGN_BINDING_INVALID: this repository's design history does not hold that "
                     "project uid, revision and digest")
    return lines


def _design(args: argparse.Namespace) -> int:
    """`oah packet design <task>`: rewrite only the Design section; revisions only move forward."""
    from oah.atomic import write_text_atomic
    from oah.design.binding import PACKET_STALE_CODE, SOURCE_MISMATCH_CODE, parse_section, replace_section
    from oah.design.model import DesignError
    from oah.intent import SourceError, read_source

    info = repo_info(pathlib.Path.cwd())
    root = info.root
    path = packet.packet_path(root, args.task)
    try:
        text = read_source(path)
    except SourceError as exc:
        raise packet.PacketError(exc.code, f"{str(exc).split(': ', 1)[1]}; create the packet with oah packet new "
                                 f"{args.task} --design PROJECT/DESIGN") from None
    current = parse_section(text)
    if current is None and not args.design:
        raise packet.PacketError("OAH_USAGE", f"{display(path, root)} has no ## Design section; link it with oah "
                                 f"packet design {args.task} --design PROJECT/DESIGN")
    spec = f"{current.project}/{current.design}" if current is not None else args.design
    if current is not None and args.design and args.design != spec:
        raise DesignError(SOURCE_MISMATCH_CODE, f"the packet follows {spec}, not {args.design}; a task follows one "
                          "design, so start another task for the other design")
    found, accepted = _packet_design(root, spec, args.revision)
    if current is not None and found.uid != current.uid:
        raise DesignError(SOURCE_MISMATCH_CODE, f"the packet names project uid {current.uid}, but {current.project} "
                          f"here has uid {found.uid}: another project with the same ID; nothing was changed")
    if current is not None and found.revision < current.revision:
        raise DesignError(PACKET_STALE_CODE, f"the packet already names revision {current.revision}; revisions only "
                          f"move forward, and revision {found.revision} is older")
    if current == found:
        print(f"OAH_DESIGN_UNCHANGED {found.line()}: the packet already names it")
        return EXIT_OK
    source = display(path, root)
    _write_linked(state_dir(info, os.environ), root, args.task, source, found,
                  lambda: write_text_atomic(path, replace_section(text, found)))
    before = current.line() if current is not None else "no design"
    print(f"OAH_DESIGN_PACKET_REWRITTEN {source}: {before} -> {found.line()}")
    print(_linked(found, accepted))
    print("Next: if the requirements changed, update the intent, raise its Revision and accept it; commit; then run "
          f"oah start --task {args.task} --agent <you> --packet {source} (a rebind: earlier reviews and gate "
          f"evidence stop counting). If nothing in the task changes, a carry keeps that evidence instead: restore "
          f"the packet with git checkout -- {source} and run oah design carry {args.task}.")
    return EXIT_OK
