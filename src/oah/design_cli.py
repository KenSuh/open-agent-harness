# 설계 명령 — oah design project/new/save/accept/status/show/context/check/reconcile/recover 의 인자, 입력 읽기, 출력 형식, 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError
from oah.design.model import (BINDING_INVALID_CODE, CONTEXT_STALE_CODE, FRESHNESS_UNKNOWN_CODE, INPUT_INVALID_CODE,
                              NOT_FOUND_CODE, UNREGISTERED_EDIT_CODE, DesignBinding, DesignError, check_part)
from oah.design.store import explain_binding, open_store
from oah.intent import MAX_BYTES, SourceError, read_source
from oah.journal import PendingTransaction
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo
from oah.wiki.base import EXIT_ENVIRONMENT, WikiError

EXIT_OK, EXIT_REFUSED = 0, 1
COMMANDS = ("design",)
CHECK_CODES = {"stale": CONTEXT_STALE_CODE, "invalid": BINDING_INVALID_CODE, "edited": UNREGISTERED_EDIT_CODE,
               "missing": NOT_FOUND_CODE, "unknown": FRESHNESS_UNKNOWN_CODE}


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


def run(args: argparse.Namespace) -> int:
    try:
        store = open_store(pathlib.Path.cwd())
        return HANDLERS[args.design_command](args, store)
    except WikiError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
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


HANDLERS = {"project": _project, "new": _new, "save": _save, "accept": _accept, "status": _status, "show": _show,
            "context": _context, "check": _check, "reconcile": _reconcile, "recover": _recover}
