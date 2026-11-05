# 설계 명령 — oah design project/new/save/accept/status/show/context/check/reconcile/recover/task/carry 의 인자, 입력 읽기, 출력 형식, 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError
from oah.design.model import (BINDING_INVALID_CODE, CONTEXT_STALE_CODE, FRESHNESS_UNKNOWN_CODE, INPUT_INVALID_CODE,
                              NOT_FOUND_CODE, UNREGISTERED_EDIT_CODE, DesignBinding, DesignError, check_part)
from oah.design.store import explain_binding, open_store
from oah.intent import MAX_BYTES, SourceError, read_source
from oah.journal import PendingTransaction
from oah.lease import LeaseError
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo
from oah.wiki.base import EXIT_ENVIRONMENT, WikiError

EXIT_OK, EXIT_REFUSED = 0, 1
COMMANDS = ("design",)
CHECK_CODES = {"stale": CONTEXT_STALE_CODE, "invalid": BINDING_INVALID_CODE, "edited": UNREGISTERED_EDIT_CODE,
               "missing": NOT_FOUND_CODE, "unknown": FRESHNESS_UNKNOWN_CODE}
# Design writes and carries a dispatched worker (OAH_WORKER=1) may not run: a cooperative guard on oah's own
# commands, so a worker cannot change or approve the design it implements. It is not a security boundary against
# other programs of the same user, which can still write the files.
WORKER_REFUSED = ("project", "new", "save", "accept", "reconcile", "recover", "carry")


def register(commands: argparse._SubParsersAction) -> None:
    design = commands.add_parser("design", help="keep each product's design, its revisions and its wiki pages",
                                 description="Designs live in .oah/designs (the record) and in <wiki>/designs (the "
                                 "pages people read and edit). Designs change only on the branch their project was "
                                 "registered on; every branch can read them.")
    sub = design.add_subparsers(dest="design_command", metavar="<design command>", required=True)

    project = sub.add_parser("project", help="register a design project, or change its title")
    actions = project.add_subparsers(dest="project_command", metavar="<project command>", required=True)
    add = actions.add_parser("add", help="register a project; its designs then change only on this branch")
    add.add_argument("project", help="the project ID, such as academy-booking")
    add.add_argument("--title", required=True, help="the name people see, in any language")
    add.add_argument("--event", help="an ID for this change, so a retry never repeats it")
    rename = actions.add_parser("rename", help="change a project's title; its ID and files stay")
    rename.add_argument("project")
    rename.add_argument("--title", required=True)
    rename.add_argument("--event")

    new = sub.add_parser("new", help="create a design at its first revision")
    new.add_argument("project")
    new.add_argument("design", help="the design ID, such as booking")
    new.add_argument("--title", required=True)
    new.add_argument("--file", required=True, help="the design text, or - for standard input")
    new.add_argument("--part", action="append", default=[], metavar="NAME=FILE",
                     help="another part, such as request=FILE (the user's own words); repeat for more")
    new.add_argument("--reason", default="Created")
    new.add_argument("--event")

    save = sub.add_parser("save", help="save a change as the next revision of the revision you read")
    save.add_argument("project")
    save.add_argument("design")
    save.add_argument("--expect-revision", type=int, required=True, help="the revision you read")
    save.add_argument("--expect-digest", required=True, help="that revision's digest")
    save.add_argument("--reason", required=True, help="why the design changes, in one line")
    save.add_argument("--from-wiki", action="store_true", help="take every part from the edited wiki page")
    save.add_argument("--file", help="new design text, or - for standard input")
    save.add_argument("--part", action="append", default=[], metavar="NAME=FILE")
    save.add_argument("--drop-part", action="append", default=[], metavar="NAME")
    save.add_argument("--title")
    save.add_argument("--event")

    accept = sub.add_parser("accept", help="record that a revision is approved for implementation")
    accept.add_argument("project")
    accept.add_argument("design")
    accept.add_argument("--revision", type=int, required=True)
    accept.add_argument("--digest", required=True)
    accept.add_argument("--reason", required=True, help="who approved it and why, in one line")
    accept.add_argument("--event")

    status = sub.add_parser("status", help="list projects and designs, or show one design's history (reads only)")
    status.add_argument("project", nargs="?")
    status.add_argument("design", nargs="?")
    status.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="print one part of a design (reads only)")
    show.add_argument("project")
    show.add_argument("design")
    show.add_argument("--revision", type=int)
    show.add_argument("--part", default="design")

    context = sub.add_parser("context", help="print everything about one design as JSON (reads only)")
    context.add_argument("project")
    context.add_argument("design")

    check = sub.add_parser("check", help="fail unless a revision and digest are still the design's current ones")
    check.add_argument("project")
    check.add_argument("design")
    check.add_argument("--revision", type=int, required=True)
    check.add_argument("--digest", required=True)
    check.add_argument("--accepted", action="store_true", help="compare with the accepted revision instead")

    reconcile = sub.add_parser("reconcile", help="rewrite the design pages and lists from the records")
    reconcile.add_argument("project", nargs="?")
    reconcile.add_argument("design", nargs="?")

    recover = sub.add_parser("recover", help="finish or check an interrupted design write")
    recover.add_argument("--resolved", action="store_true",
                         help="after fixing a recovery conflict by hand: keep the journal aside and go on")

    task = sub.add_parser("task", help="show the design a task follows, its state and its text (reads only)")
    task.add_argument("task")
    task.add_argument("--json", action="store_true", help="print the state and the design text as JSON")
    task.add_argument("--check", action="store_true", help="exit 1 unless the task's design permits work")

    carry = sub.add_parser("carry", help="record that a design change does not change a task, so its evidence "
                           "keeps counting")
    carry.add_argument("task")
    carry.add_argument("--agent", required=True, help="who decides; the lease holder while the lease is open")
    carry.add_argument("--revision", type=int, required=True, help="the newest revision you read (oah design task)")
    carry.add_argument("--digest", required=True, help="that revision's digest")
    carry.add_argument("--reason", required=True, help="why the change does not affect the task, in one line")


def run(args: argparse.Namespace) -> int:
    try:
        if os.environ.get("OAH_WORKER") == "1" and args.design_command in WORKER_REFUSED:
            from oah.design.binding import WORKER_REFUSED_CODE

            raise DesignError(WORKER_REFUSED_CODE, f"dispatched workers do not run oah design {args.design_command}; "
                              "nothing was changed. Return your work to the dispatcher, who changes, accepts and "
                              "carries designs")
        store = open_store(pathlib.Path.cwd())
        return HANDLERS[args.design_command](args, store)
    except WikiError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except LeaseError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except (ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, StorageError, PendingTransaction) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT


class _Inputs:
    """Part texts from files, or once from standard input."""

    def __init__(self) -> None:
        self.stdin_used = False

    def read(self, value: str, what: str) -> str:
        if value == "-":
            if self.stdin_used:
                raise DesignError(INPUT_INVALID_CODE, "standard input (-) can give only one part")
            self.stdin_used = True
            data = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(data) >= MAX_BYTES:
                raise DesignError(INPUT_INVALID_CODE, f"the {what} text on standard input must be under "
                                  f"{MAX_BYTES} bytes (1 MiB)")
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DesignError(INPUT_INVALID_CODE, f"the {what} text on standard input is not UTF-8") from exc
        try:
            return read_source(pathlib.Path(value))
        except SourceError as exc:
            raise DesignError(INPUT_INVALID_CODE, f"cannot use {value} for the {what}: {exc}") from exc

    def parts(self, args: argparse.Namespace) -> dict[str, str]:
        parts = {}
        if args.file is not None:
            parts["design"] = self.read(args.file, "design part")
        for item in args.part:
            name, separator, value = item.partition("=")
            if not separator or not value:
                raise DesignError(INPUT_INVALID_CODE, f"--part {item!r}: write NAME=FILE, such as request=request.md")
            name = check_part(name)
            if name in parts:
                raise DesignError(INPUT_INVALID_CODE, f"the {name} part is given twice")
            parts[name] = self.read(value, f"{name} part")
        return parts


def _print(lines) -> None:
    for line in lines:
        print(line)


def _project(args: argparse.Namespace, store) -> int:
    if args.project_command == "add":
        result = store.add_project(args.project, title=args.title, event=args.event)
    else:
        result = store.rename_project(args.project, title=args.title, event=args.event)
    _print(result.lines())
    return EXIT_OK


def _new(args: argparse.Namespace, store) -> int:
    parts = _Inputs().parts(args)
    result = store.new(args.project, args.design, title=args.title, parts=parts, reason=args.reason,
                       event=args.event)
    _print(result.lines())
    return EXIT_OK


def _save(args: argparse.Namespace, store) -> int:
    parts = _Inputs().parts(args)
    result = store.save(args.project, args.design, expect_revision=args.expect_revision,
                        expect_digest=args.expect_digest, reason=args.reason, parts=parts, drop=args.drop_part,
                        title=args.title, from_wiki=args.from_wiki, event=args.event)
    _print(result.lines())
    return EXIT_OK


def _accept(args: argparse.Namespace, store) -> int:
    result = store.accept(args.project, args.design, revision=args.revision, digest=args.digest, reason=args.reason,
                          event=args.event)
    _print(result.lines())
    return EXIT_OK


def _status(args: argparse.Namespace, store) -> int:
    if args.design is not None:
        context = store.context(args.project, args.design)
        if args.json:
            print(json.dumps(context.to_json(), indent=2, ensure_ascii=False))
            return EXIT_OK
        project = context.project
        print(f"design {project.id}/{context.design} {json.dumps(context.title, ensure_ascii=False)}")
        print(f"project {project.id} {json.dumps(project.title, ensure_ascii=False)} uid={project.uid} "
              f"authority={project.authority}")
        print(f"current r{context.current.revision} {context.current.digest} manifest {context.current.info.manifest}")
        if context.accepted is not None:
            print(f"accepted r{context.accepted.revision} {context.accepted.digest}")
        else:
            print("accepted none")
        print(f"status={context.status} page={context.page} page_state={context.page_state} source={context.source} "
              f"freshness={context.freshness}")
        print("revisions:")
        for info in context.revisions:
            parent = f"parent r{info.parent[0]}" if info.parent else "first"
            print(f"  r{info.revision} {info.at} {info.origin} {parent} {info.digest}: {info.reason}")
        if context.acceptances:
            print("acceptances:")
            for item in context.acceptances:
                print(f"  a{item.sequence} r{item.revision} {item.at}: {item.reason}")
        for item in context.diagnostics:
            print(f"{item.level} {item}")
        return EXIT_OK
    summaries = store.designs(args.project)
    projects = {record.id: record for record in store.projects()}
    if args.json:
        names = [args.project] if args.project else sorted({*projects, *(item.project for item in summaries)})
        data = {"version": 1, "projects": [
            {**(projects[name].public_json() if name in projects else {"id": name, "status": "corrupt"}),
             "designs": [item.to_json() for item in summaries if item.project == name]} for name in names],
            "diagnostics": [item.to_json() for item in store.diagnostics(args.project)]}
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return EXIT_OK
    names = [args.project] if args.project else sorted({*projects, *(item.project for item in summaries)})
    if not names:
        print("OAH_DESIGN_NONE no design projects yet; register one with "
              "`oah design project add <project> --title <title>`")
    for name in names:
        record = projects.get(name)
        designs = [item for item in summaries if item.project == name]
        if record is None:
            print(f"project {name}: corrupt record ({len(designs)} designs)")
        else:
            print(f"project {name} {json.dumps(record.title, ensure_ascii=False)} (authority "
                  f"{record.authority.removeprefix('refs/heads/')}): {len(designs)} design"
                  f"{'' if len(designs) == 1 else 's'}")
        for item in designs:
            if item.status == "corrupt":
                print(f"  {item.project}/{item.design}: corrupt; `oah design status {item.project} {item.design}` "
                      "names the file")
                continue
            accepted = f"accepted r{item.accepted[0]}" if item.accepted else "nothing accepted"
            print(f"  {item.project}/{item.design} {json.dumps(item.title, ensure_ascii=False)}: r{item.current[0]} "
                  f"{item.current[1]} {item.status}, {accepted}; page {item.page_state}; freshness {item.freshness}")
    for item in store.diagnostics(args.project):
        print(f"{item.level} {item}")
    return EXIT_OK


def _show(args: argparse.Namespace, store) -> int:
    if args.revision is None:
        context = store.context(args.project, args.design)
        snapshot = context.current
        for item in context.diagnostics:
            if item.level != "info" or item.code.endswith("NOT_AUTHORITY"):
                print(f"{item.level} {item}", file=sys.stderr)
    else:
        snapshot = store.snapshot(args.project, args.design, args.revision)
    part = check_part(args.part)
    if part not in snapshot.texts:
        raise DesignError(NOT_FOUND_CODE, f"revision {snapshot.revision} of {args.project}/{args.design} has no "
                          f"{part} part; it has {', '.join(sorted(snapshot.texts))}")
    sys.stdout.write(snapshot.texts[part])
    return EXIT_OK


def _context(args: argparse.Namespace, store) -> int:
    print(json.dumps(store.context(args.project, args.design).to_json(), indent=2, ensure_ascii=False))
    return EXIT_OK


def _check(args: argparse.Namespace, store) -> int:
    from oah.design.model import check_digest, check_id, check_revision

    project = store.context(check_id(args.project), check_id(args.design, "design")).project
    binding = DesignBinding(project.id, project.uid, check_id(args.design, "design"), check_revision(args.revision),
                            check_digest(args.digest))
    state, reason = explain_binding(binding, store, against="accepted" if args.accepted else "current")
    if state == "current":
        print(f"OAH_DESIGN_CURRENT {binding.project}/{binding.design} r{binding.revision} {binding.digest}")
        return EXIT_OK
    error = DesignError(CHECK_CODES[state], f"{binding.line()} is {state}: {reason}")
    print(error, file=sys.stderr)
    return error.exit_code


def _reconcile(args: argparse.Namespace, store) -> int:
    result = store.reconcile(args.project, args.design)
    _print(result.lines())
    return EXIT_REFUSED if result.skipped else EXIT_OK


def _recover(args: argparse.Namespace, store) -> int:
    print(store.recover(resolved=args.resolved))
    return EXIT_OK


def _task_view(store, task: str):
    """(task view, judgement, basis inputs) of a task; reads only, writes nothing, never reconciles."""
    from oah.design import binding as design
    from oah.lease import TASK_RE

    if not TASK_RE.fullmatch(task or ""):
        raise LeaseError("OAH_TASK_INVALID", f"{task!r}: use letters, digits, '.', '_' or '-' (max 64)")
    view = design.task_view(store.state, task)
    lease = view.lease
    epoch = view.closing or view.epoch or {}
    worktree = lease.worktree if lease is not None else epoch.get("worktree")
    root = pathlib.Path(worktree) if worktree and pathlib.Path(worktree).is_dir() else store.root
    if lease is not None:
        from oah.lease_cli import lease_design

        source = lease.binding().source if lease.intent is not None else f".oah/tasks/{task}/PACKET.md"
        try:
            text = read_source(root / source)
        except SourceError:
            text = None
        return view, lease_design(lease, store.state, packet_text=text, source=source, root=root)
    if view.problem is not None and view.record is None:
        return view, design.judge(None, root=root, found=design.Origin("invalid", view.problem))
    if view.record is None:
        source = f".oah/tasks/{task}/PACKET.md"
        try:
            text = read_source(root / source)
        except SourceError:
            text = None
        found = design.origin(store.state, task, packet_text=text, root=root, source=source, view=view)
        if found is None and view.epoch is None:
            raise LeaseError("OAH_LEASE_NOT_FOUND", f"task {task} has never been leased and follows no design; link "
                             f"a design with oah packet new {task} --design <project>/<design>")
        return view, design.judge(None, root=root, found=found)
    basis = design.basis_of(view.record, intent=epoch.get("intent"), scope=epoch.get("scope") or (),
                            gates=epoch.get("gates") or ())
    return view, design.judge(view.record, root=root, basis=basis)


def _task(args: argparse.Namespace, store) -> int:
    from oah.design import binding as design
    from oah.design import task_context

    view, judged = _task_view(store, args.task)
    record = judged.task
    block = task_context.render(judged)
    lease = view.lease
    agent = lease.agent if lease is not None else "<you>"
    held = "open" if lease is not None else (f"closed ({view.closed})" if view.closed else "none")
    if args.json:
        print(json.dumps({"task": args.task, "lease": held, "state": judged.state, "code": judged.code,
                          "reason": judged.reason, "next": judged.next_step(args.task, agent),
                          "instance": record.identity.instance if record is not None else None,
                          "record": block.record, "delivered": block.delivered, "block": block.text,
                          "plan": block.plan}, indent=2, ensure_ascii=False))
    else:
        bound = record.binding.line() if record is not None else "no design binding"
        print(f"OAH_DESIGN_TASK task={args.task} state={judged.state} {bound} lease={held}")
        context = judged.context
        if context is not None:
            accepted = (f"accepted r{context.accepted.revision} {context.accepted.digest}"
                        if context.accepted is not None else "nothing accepted")
            print(f"{accepted}; working r{context.current.revision} {context.current.digest}")
            if record is not None:
                for info in reversed([item for item in context.revisions if item.revision > record.binding.revision]):
                    print(f"  since r{record.binding.revision}: r{info.revision} {info.digest}: {info.reason}")
        if record is not None:
            for item in record.carried:
                print(f"carried r{item.source['revision']} -> r{item.to['revision']} by {item.actor} at {item.at}: "
                      f"{item.reason}")
            if record.draft_ack is not None:
                ack = record.draft_ack
                print(f"draft acknowledged r{ack.viewed_working['revision']} {ack.viewed_working['digest']} by "
                      f"{ack.actor} at {ack.at}: {ack.reason}")
        print(f"reason: {judged.reason}")
        if judged.state not in ("current", "unbound"):
            print(f"next: {judged.next_step(args.task, agent)}")
        if record is not None:
            sys.stdout.write(block.readout())
    if args.check and judged.state not in design.WORK:
        return judged.exit_code
    return EXIT_OK


class _NothingToCarry(Exception):
    pass


def _carry(args: argparse.Namespace, store) -> int:
    """`oah design carry`: an explicit cooperative decision that a design change leaves the task unchanged.

    Open lease: only its holder, against the exact generation and record (CAS). Closed task: only a finished epoch,
    against its closing transaction, generation and record; it grants no new work. Never a changed basis/intent."""
    from oah import intent as oah_intent
    from oah.design import binding as design
    from oah.design.model import check_digest, check_line, check_revision
    from oah.lease import Store, _iso, utc_now

    revision, digest = check_revision(args.revision), check_digest(args.digest)
    reason = check_line(args.reason, "reason")
    outcome: dict = {}

    def update(locked):
        view = design.task_view(store.state, args.task, lease=locked)
        if view.unreadable is not None or (view.problem is not None and view.record is None):
            found = design.origin(store.state, args.task, view=view)
            raise design.judge(None, root=store.root, found=found).error("carry", args.task, args.agent)
        if locked is not None:
            try:
                record = locked.design_record()
            except design.RecordInvalid as exc:
                raise design.judge(None, root=store.root, invalid=exc.identity).error("carry", args.task, args.agent)
            epoch = {"intent": locked.intent, "scope": locked.scope, "gates": locked.gates,
                     "worktree": locked.worktree}
            target = {"lease_id": locked.id, "closing_transaction_id": None, "expected_generation": locked.generation}
            authority = design.authority_of(locked)
        else:
            if view.closed != "finished" or view.closing is None:
                raise DesignError(design.BINDING_REQUIRED_CODE, f"task {args.task} has no open lease and its last "
                                  f"lease was {view.closed or 'never'} finished; only an open lease's holder or a "
                                  "finished task can record a carry")
            record, epoch = view.record, view.closing
            target = {"lease_id": epoch["lease_id"], "closing_transaction_id": epoch["transaction_id"],
                      "expected_generation": epoch["generation"]}
            authority = None
        if record is None:
            raise DesignError(design.BINDING_REQUIRED_CODE, f"task {args.task} has no design binding to carry; bind "
                              f"its packet with oah start --task {args.task} --agent {args.agent} --packet <packet>")
        worktree = epoch.get("worktree")
        root = pathlib.Path(worktree) if worktree and pathlib.Path(worktree).is_dir() else store.root
        binding = oah_intent.Binding.from_json(epoch["intent"]) if epoch.get("intent") else None
        if binding is not None:
            state, why = oah_intent.explain_binding(binding, root)
            if state != "current":
                raise DesignError("OAH_INTENT_NOT_CURRENT", f"the task's intent is {state} ({why}); a carry never "
                                  "stands in for a changed goal. Bind the new revision with oah start --packet instead")
            try:
                section = design.parse_section(oah_intent.read_source(oah_intent.source_path(binding, root)))
            except (oah_intent.SourceError, design.SectionInvalid):
                section = None
            committed = design.committed_text(root, binding.source) if locked is None else None
            if locked is None:
                try:
                    section = design.parse_section(committed) if committed is not None else None
                except design.SectionInvalid:
                    section = None
            if section is None or not record.same_design(section) or not record.holds(section.binding()):
                raise DesignError(design.SOURCE_MISMATCH_CODE, f"the bound packet {binding.source} does not name a "
                                  f"revision task {args.task} follows ({record.binding.line()}); restore it, or rebind "
                                  "with oah packet design and oah start --packet")
        basis = design.basis_of(record, intent=epoch.get("intent"), scope=epoch.get("scope") or (),
                                gates=epoch.get("gates") or ())
        context = design.read_context(record.binding, root=root, task=record)
        if isinstance(context, design.TaskDesign):
            raise context.error("carry", args.task, args.agent)
        new, action = design.carry(record, context, basis=basis, revision=revision, digest=digest, reason=reason,
                                   agent=args.agent, at=_iso(utc_now()), authority=authority, task=args.task)
        if new is None:
            outcome["why"] = action
            raise _NothingToCarry()
        outcome.update(old=record, new=new, action=action)
        target.update(expected_design_record_digest=record.digest, task_basis_digest=basis.digest)
        return new.to_json(), action, {"target": target, "old_design": record.to_json(), "reason": reason,
                                       "actor": args.agent,
                                       "viewed": {"accepted": design._ref(context.accepted),
                                                  "working": design._ref(context.current)}}

    try:
        Store(store.state).carry_design(task=args.task, agent=args.agent, update=update)
    except _NothingToCarry:
        print(f"{design.ALREADY_CURRENT_CODE} task={args.task}: {outcome['why']}")
        return EXIT_OK
    old, new = outcome["old"], outcome["new"]
    print(f"OAH_DESIGN_CARRIED task={args.task} {old.binding.line()} -> {new.binding.line()} ({outcome['action']})")
    if outcome["action"] == "carried":
        print(f"evidence for r{old.binding.revision} keeps counting for {args.task} with the same task inputs; the "
              "packet may keep naming it")
    else:
        print(f"the newer draft r{new.draft_ack.viewed_working['revision']} is acknowledged for these task inputs; it "
              "is still not accepted")
    return EXIT_OK


HANDLERS = {"project": _project, "new": _new, "save": _save, "accept": _accept, "status": _status, "show": _show,
            "context": _context, "check": _check, "reconcile": _reconcile, "recover": _recover, "task": _task,
            "carry": _carry}
