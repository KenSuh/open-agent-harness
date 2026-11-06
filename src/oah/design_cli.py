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
    task.add_argument("--run", help="one run of the task: its design transport origin, history and the actions "
                      "it is eligible for (reads only, unless --transport-cleanup)")
    task.add_argument("--evidence-plan", help="with --run: the run's read plan (its manifest sha256 hex)")
    task.add_argument("--blob", help="with --run and --evidence-plan: print one exact chunk of this DATA file as "
                      "ReadChunkV1 JSON (reads only)")
    task.add_argument("--offset", type=int, default=0, help="the chunk's byte offset (default 0)")
    task.add_argument("--length", type=int, default=32768, help="the chunk's byte length (at most 32768)")
    task.add_argument("--transport-cleanup", action="store_true",
                      help="with --run: remove the run's exact unchanged design data, or with --retain record it "
                      "as retained; needs the exact --expect-* values a --run readout prints")
    task.add_argument("--expect-origin", help="the origin sha256 the readout printed")
    task.add_argument("--expect-record", help="the latest record SEQUENCE:DIGEST the readout printed")
    task.add_argument("--expect-task-basis", help="the task basis digest the readout printed")
    task.add_argument("--settle", choices=("failed", "aborted"),
                      help="how a run without a recorded exit is settled (needs a qualified changed-boot profile)")
    task.add_argument("--retain", action="store_true", help="acknowledge the data as retained instead of removing it")
    task.add_argument("--gate-history", action="store_true",
                      help="the task's gate evidence history: the live recent page, or one archive page (reads only)")
    task.add_argument("--page", help="with --gate-history: an archive page's sha256 hex")
    task.add_argument("--restore-gate-history", metavar="PAGE",
                      help="rebuild a lost or damaged live gate binding file from this exact archive page")
    task.add_argument("--expect-current", help="with --restore-gate-history: the live file's sha256 hex now, or "
                      "'absent'")

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


def _gate_history(args: argparse.Namespace, store) -> int:
    """B4's read and recovery path: one verified gate history page per call, or a live-file rebuild from an exact
    archive page under the dispatcher's expected-current-byte check. It never searches for a green record."""
    import hashlib

    from oah.gates.bindings import BindingStore
    from oah.gates.check import GateRefused, history_page
    from oah.lease import TASK_RE

    if not TASK_RE.fullmatch(args.task or ""):
        raise LeaseError("OAH_TASK_INVALID", f"{args.task!r}: use letters, digits, '.', '_' or '-' (max 64)")
    bindings = BindingStore(store.state, args.task)
    try:
        if args.restore_gate_history is None:
            page = history_page(bindings, args.page)
            if args.json:
                print(json.dumps(page, indent=2, ensure_ascii=False))
            else:
                head = page.get("archive_head")
                print(f"OAH_GATE_HISTORY task={args.task} page={page['page']} entries={len(page['history'])} "
                      f"previous={head['sha256'] if head else 'none'}")
                for item in page["history"]:
                    print(f"  {item.get('kind')} {item.get('title') or item.get('id') or ''} "
                          f"recorded={item.get('recorded_at')}")
            return EXIT_OK
        if os.environ.get("OAH_WORKER") == "1":
            from oah.design.binding import WORKER_REFUSED_CODE

            raise DesignError(WORKER_REFUSED_CODE, "dispatched workers do not restore gate history; nothing was "
                              "changed")
        if args.expect_current is None:
            raise GateRefused("OAH_GATES_HISTORY_INVALID", "--restore-gate-history needs --expect-current (the live "
                              "file's sha256 hex now, or 'absent')")
        history_page(bindings, args.restore_gate_history)  # the exact page must verify first
        page_bytes = (bindings.path.parent / "design-history" / f"{args.restore_gate_history}.json").read_bytes()
        from oah.atomic import write_text_atomic
        from oah.lockfile import StateLock

        with StateLock(store.state / ".lock", timeout=bindings.lock_timeout):
            try:
                current = hashlib.sha256(bindings.path.read_bytes()).hexdigest()
            except FileNotFoundError:
                current = "absent"
            if current != args.expect_current:
                raise GateRefused("OAH_GATES_HISTORY_INVALID", f"the live gate file is {current}, not "
                                  f"{args.expect_current}; nothing was changed")
            if hashlib.sha256(page_bytes).hexdigest() != args.restore_gate_history:
                raise GateRefused("OAH_GATES_HISTORY_INVALID", "the archive page changed while it was restored")
            bindings.path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(bindings.path, page_bytes.decode("utf-8"))
        print(f"OAH_GATE_HISTORY_RESTORED task={args.task} from={args.restore_gate_history} replaced={current}: the "
              "live file now holds that page's exact bytes; run oah gates check to refresh current evidence")
        return EXIT_OK
    except GateRefused as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, ValueError) as exc:
        print(f"OAH_GATES_HISTORY_INVALID: the gate history cannot be read ({type(exc).__name__})", file=sys.stderr)
        return EXIT_REFUSED


def _transport_eligibility(store, task: str, view) -> dict:
    """Which current authority-bearing actions a transport origin is eligible for, separately from its OLD facts."""
    from oah.lease import Store

    lease = next((entry for entry, stale in Store(store.state).snapshot() if entry.task == task and not stale), None)
    design = lease.design if lease is not None and isinstance(lease.design, dict) else {}
    current = (lease is not None and (design.get("task_identity") or {}).get("task_instance_id")
               == (view.record.get("task_identity") or {}).get("task_instance_id"))
    if current:
        from oah.lease_cli import lease_design

        allowed = ("current", "needs-review") if (view.record.get("profile") or {}).get("operation") == "inspect" \
            else ("current",)
        current = lease_design(lease, store.state).state in allowed
    blocked = []
    if not view.quiescent:
        blocked.append("no recorded exit or settlement")
    if view.exposure is not None:
        blocked.append(f"sticky exposure ({view.exposure.get('state')})")
    if view.resolution is not None:
        blocked.append(f"already {view.state}")
    if not current:
        blocked.append("no checked current unexpired authority on the origin's task instance")
    return {"current_authority": bool(current), "cleanup": "eligible" if not blocked else "blocked",
            "blocked_by": blocked}


def _task_run(args: argparse.Namespace, store) -> int:
    from oah.design import task_context
    from oah.lease import TASK_RE

    if not TASK_RE.fullmatch(args.task or ""):
        raise LeaseError("OAH_TASK_INVALID", f"{args.task!r}: use letters, digits, '.', '_' or '-' (max 64)")
    if args.blob is not None:
        if args.evidence_plan is None:
            raise DesignError(task_context.REFERENCE_UNAVAILABLE_CODE, "--blob needs --evidence-plan")
        chunk = task_context.read_chunk(store.root, task=args.task, run=args.run, manifest=args.evidence_plan,
                                        blob=args.blob, offset=args.offset, length=args.length)
        print(json.dumps(chunk, ensure_ascii=False))
        return EXIT_OK
    if args.transport_cleanup:
        if os.environ.get("OAH_WORKER") == "1":
            from oah.design.binding import WORKER_REFUSED_CODE

            raise DesignError(WORKER_REFUSED_CODE, "dispatched workers do not clean design transport data; nothing "
                              "was changed. Return your work to the dispatcher")
        return _transport_cleanup(args, store)
    views = task_context.transport_views(store.state, task=args.task, run=args.run)
    rows = []
    for view in views.values():
        row = view.summary()
        row["eligibility"] = _transport_eligibility(store, args.task, view)
        row["history"] = [{"sequence": item["sequence"], "kind": item["kind"], "record_digest": item["record_digest"]}
                          for item in view.observations]
        rows.append(row)
    if args.json:
        print(json.dumps({"task": args.task, "run": args.run, "origins": rows}, indent=2, ensure_ascii=False))
        return EXIT_OK
    if not rows:
        print(f"OAH_DESIGN_TASK_RUN task={args.task} run={args.run} origins=0: the run delivered its design inline "
              "(or had none), so it has no design transport origin")
        return EXIT_OK
    for row in rows:
        latest = row["latest"]
        print(f"OAH_DESIGN_TASK_RUN task={args.task} run={args.run} origin={row['origin']} state={row['state']} "
              f"quiescent={str(row['quiescent']).lower()} delivered={str(row['delivered']).lower()}")
        print(f"  expect: --evidence-plan {row['manifest']} --expect-origin {row['origin']} --expect-record "
              f"{latest['sequence']}:{latest['record_digest']} --expect-task-basis {row['task_basis_digest']}")
        if row["exposure"] is not None:
            print(f"  exposure: {row['exposure'].get('state')} — {row['exposure'].get('why')} (sticky)")
        eligibility = row["eligibility"]
        print(f"  cleanup: {eligibility['cleanup']}" + (f" ({'; '.join(eligibility['blocked_by'])})"
                                                         if eligibility["blocked_by"] else ""))
        for item in row["history"]:
            print(f"  {item['sequence']:>3} {item['kind']}")
    return EXIT_OK


def _transport_cleanup(args: argparse.Namespace, store) -> int:
    """SC4-P5: exact cleanup or ACK_RETAINED of one quiescent origin under current authority and a fresh P4."""
    import secrets

    from oah.design import task_context as tc
    from oah.lease import Store

    for name in ("evidence_plan", "expect_origin", "expect_record", "expect_task_basis", "settle"):
        if getattr(args, name) is None:
            raise DesignError(tc.REFERENCE_UNAVAILABLE_CODE, f"--transport-cleanup needs --{name.replace('_', '-')}; "
                              "read the exact values with oah design task <task> --run <run>")
    sequence, _sep, digest = args.expect_record.partition(":")
    if not sequence.isdigit():
        raise DesignError(tc.REFERENCE_UNAVAILABLE_CODE, "--expect-record is SEQUENCE:DIGEST")
    expected = {"sequence": int(sequence), "record_digest": digest}
    view = tc.transport_views(store.state, task=args.task, run=args.run).get(args.expect_origin)
    if view is None or view.record.get("manifest") != args.evidence_plan:
        raise DesignError(tc.TRANSPORT_HISTORY_UNKNOWN, f"run {args.run} of task {args.task} has no transport origin "
                          f"{args.expect_origin} with plan {args.evidence_plan}")
    if view.record.get("task_basis_digest") != args.expect_task_basis or view.latest != expected:
        raise DesignError(tc.TRANSPORT_CONFLICT, f"origin {view.origin} is now at "
                          f"{view.latest['sequence']}:{view.latest['record_digest']} for basis "
                          f"{view.record.get('task_basis_digest')}; nothing was changed. Read it again with --run")
    receiving = store.root
    if view.record.get("retirement"):
        raw = tc._read_regular(store.state / "runs" / args.task / args.run / "result.json", tc.POLICY_FILE_LIMIT)
        result = tc.strict_json(raw.decode("utf-8")) if raw is not None else {}
        cwd = result.get("cwd") if isinstance(result, dict) else None
        if not isinstance(cwd, str) or not pathlib.Path(cwd).is_absolute() or tc._label_digest(
                "oah-receiving-root", view.record["nonce"], os.fsencode(os.path.realpath(cwd))) != view.record["root_digest"]:
            raise DesignError(tc.TRANSPORT_HISTORY_UNKNOWN, "the original review allocation cannot be located")
        receiving = pathlib.Path(cwd)
    if not view.quiescent:
        writer = Store(store.state)
        expected = writer.settle_transport(task=args.task, origin=view.origin, expected=expected,
                                          root=receiving, settle=args.settle)
        view = tc.transport_views(store.state, task=args.task, run=args.run)[view.origin]
    if view.exposure is not None:
        raise DesignError(tc._P4_CODES[view.exposure["state"]], f"run {args.run}'s design data was "
                          f"{view.exposure['state']}; remove it from the index and every new commit first. Nothing "
                          "was changed (the exposure stays recorded)")
    if view.record.get("retirement") and view.last("retired") is None:
        from oah.review.runner import retire_snapshot

        run_path = store.state / "runs" / args.task / args.run / "result.json"
        data = tc._read_regular(run_path, tc.POLICY_FILE_LIMIT)
        result = tc.strict_json(data.decode("utf-8")) if data is not None else {}
        cwd = result.get("cwd") if isinstance(result, dict) else None
        if not isinstance(cwd, str) or not pathlib.Path(cwd).is_absolute() or tc._label_digest(
                "oah-receiving-root", view.record["nonce"], os.fsencode(os.path.realpath(cwd))) != \
                view.record["root_digest"]:
            raise DesignError(tc.TRANSPORT_HISTORY_UNKNOWN, "the original review allocation cannot be located")
        if args.retain:
            writer = Store(store.state)
            authority = writer.transport_action_basis(task=args.task, origin=view.origin, expected=expected,
                                                       receiving_root=pathlib.Path(cwd))
            writer.observe_transport(task=args.task, origin=view.origin, expected=expected,
                        kind="retirement-ack-retained", authority=authority,
                        fact={"plan_sha256": view.record["retirement"]["sha256"], "counted": False,
                              "reason": "retained by explicit dispatcher request"})
            print(f"OAH_DESIGN_TRANSPORT_ACK_RETAINED run={args.run} snapshot=true counted=false")
            return EXIT_OK
        outcome, reason = retire_snapshot(store.state, view, pathlib.Path(cwd).parent)
        if outcome != "disposed":
            raise DesignError(tc.TRANSPORT_RETAINED, reason or "the review snapshot was retained")
        print(f"OAH_DESIGN_TRANSPORT_RETIRED run={args.run}")
        return EXIT_OK
    if view.resolution is not None:
        print(f"OAH_DESIGN_TRANSPORT_RESOLVED run={args.run} state={view.state}: nothing to do")
        return EXIT_OK
    root = store.root
    checked = tc.intersects(view.record, root)
    if checked["state"] != "clean":
        raise DesignError(checked["code"] or tc.TRANSPORT_HISTORY_UNKNOWN, f"{checked['why']}; nothing was changed")
    settled = view.last("boot-settled")
    installed = settled["fact"]["installed_current"] if settled else \
        next((item["fact"] for item in view.observations if item["kind"] == "installed"), {})
    done, states = tc.reopen(root, view.record["manifest"], installed, progress=view.observations)
    try:
        intents = {name for item in view.observations if item["kind"] == "cleanup-intent"
                   for name in [entry["name"] for entry in item["fact"].get("planned", [])]}
        unknown = sorted(name for name, state in states.items() if state in ("changed", "unexpected", "unknown"))
        if unknown:
            raise DesignError(tc.TRANSPORT_CHANGED, f"the design data of run {args.run} changed ({unknown[:3]}); "
                              "nothing is removed or adopted")
        writer = Store(store.state)
        authority = writer.transport_action_basis(task=args.task, origin=view.origin, expected=expected)
        operation = secrets.token_hex(8)

        def before(name: str):
            return writer.transport_cleanup_guard(name, task=args.task, origin=view.origin,
                        expected=lambda: expected, authority=authority, installation=done)

        leaves = {}
        for name, state in sorted(states.items()):
            item = next(entry for entry in installed.get("leaves", []) if entry["name"] == name)
            if state == "present-unchanged":
                leaves[name] = {"state": "present-unchanged", "sha256": item["sha256"]}
            else:
                leaves[name] = {"state": "absent-after-intent" if name in intents else "absent-unattributed"}
        present = sorted(name for name, item in leaves.items() if item["state"] == "present-unchanged")
        if args.retain or len(present) != len(leaves):
            removed = []
            if not args.retain and present and done is not None:
                expected = writer.observe_transport(task=args.task, origin=view.origin, expected=expected,
                                                    kind="cleanup-intent", authority=authority,
                                                    fact={"operation": operation,
                                                    "planned": [{"name": name, "identity_digest": next(
                                                        entry["identity_digest"] for entry in installed["leaves"]
                                                        if entry["name"] == name)} for name in present], "dirs": []})

                def after(name: str, observed: str, entry: str) -> None:
                    nonlocal expected
                    expected = writer.observe_transport(task=args.task, origin=view.origin, expected=expected,
                                                        kind="cleanup-progress", fact={"operation": operation,
                                                        "name": name, "entry": entry, "observed": observed},
                                                        authority=authority, held=True)

                tc.remove_leaves(done, present, before=before, after=after)
                removed = present
                for name in removed:
                    leaves[name] = {"state": "removed-under-intent"}
            writer.observe_transport(task=args.task, origin=view.origin, expected=expected, kind="ack-retained",
                                     authority=authority, fact={"operation": operation, "settle": args.settle,
                                           "reason": "retained by request" if args.retain else "partial or missing",
                                           "leaves": leaves, "missing": sorted(name for name, item in leaves.items()
                                                                               if item["state"].startswith("absent"))})
            print(f"OAH_DESIGN_TRANSPORT_ACK_RETAINED run={args.run} present={sum(1 for item in leaves.values() if item['state'] == 'present-unchanged')} "
                  f"missing={sum(1 for item in leaves.values() if item['state'].startswith('absent'))} "
                  f"removed={len(removed)}")
            return EXIT_OK
        expected = writer.observe_transport(task=args.task, origin=view.origin, expected=expected,
                                            kind="cleanup-intent", authority=authority,
                                            fact={"operation": operation, "planned": [
                                                {"name": name, "identity_digest": next(
                                                    entry["identity_digest"] for entry in installed["leaves"]
                                                    if entry["name"] == name)} for name in present],
                                                "dirs": [view.record["manifest"]] + (
                                                    [tc.DATA_DIR] if installed.get("data_dir_created") else [])})

        def progress(name: str, observed: str, entry: str) -> None:
            nonlocal expected
            expected = writer.observe_transport(task=args.task, origin=view.origin, expected=expected,
                                                kind="cleanup-progress", fact={"operation": operation, "name": name,
                                                                               "entry": entry, "observed": observed},
                                                authority=authority, held=True)

        tc.remove_leaves(done, present, before=before, after=progress)
        tc.remove_directories(done, before=before, after=progress)
        final = tc.verify_transport_origin(view.record, root, expect="cleaned")
        if final["state"] != "clean":
            raise DesignError(final["code"] or tc.TRANSPORT_HISTORY_UNKNOWN, final["why"])
        writer.observe_transport(task=args.task, origin=view.origin, expected=expected, kind="verified-cleaned",
                                 authority=authority, fact={"operation": operation, "head": final["head"]})
        print(f"OAH_DESIGN_TRANSPORT_CLEANED run={args.run} removed={len(present)}")
        return EXIT_OK
    except tc.InstallRefused as exc:
        raise DesignError(tc.TRANSPORT_CHANGED, f"{exc}; the remaining data stays") from None
    finally:
        if done is not None:
            done.close()


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

    if args.run is not None:
        return _task_run(args, store)
    if args.gate_history or args.restore_gate_history is not None:
        return _gate_history(args, store)
    view, judged = _task_view(store, args.task)
    record = judged.task
    block = task_context.readout_block(judged, task=args.task, root=store.root)
    lease = view.lease
    agent = lease.agent if lease is not None else "<you>"
    held = "open" if lease is not None else (f"closed ({view.closed})" if view.closed else "none")
    if args.json:
        print(json.dumps({"task": args.task, "lease": held, "state": judged.state, "code": judged.code,
                          "reason": judged.reason, "next": judged.next_step(args.task, agent),
                          "instance": record.identity.instance if record is not None else None,
                          "record": block.record, "delivered": False, "offered": block.delivered,
                          "block": block.text, "plan": block.plan}, indent=2, ensure_ascii=False))
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
