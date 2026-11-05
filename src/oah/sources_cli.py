# 설계 출처 명령 — oah sources register/remove/list/show/check/diff/proposals/dismiss/materials/schedule 의 인자, 출력 형식, 종료 코드를 맡는다.
"""Design sources: follow public upstreams, compare snapshots and keep material evidence."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Mapping

from oah.atomic import StorageError
from oah.config import ConfigError
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("sources",)
ENVIRONMENT_CODES = ("OAH_SOURCES_STATE_CORRUPT", "OAH_SCHEDULE_TOOL_MISSING", "OAH_SCHEDULE_UNSUPPORTED")


class Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def register(commands: argparse._SubParsersAction) -> None:
    sources = commands.add_parser("sources", help="follow public upstreams a design's materials come from",
                                  description="Sources live in this clone's oah state folder; nothing here is "
                                  "committed. A design changes only through oah design save.")
    sub = sources.add_subparsers(dest="sources_command", metavar="<sources command>", required=True)

    add = sub.add_parser("register", help="follow a GitHub repository or an https documentation page")
    add.add_argument("url")
    add.add_argument("--id", dest="source_id")
    add.add_argument("--track", choices=("release", "commit", "doc"))
    add.add_argument("--tag-prefix", default="")
    add.add_argument("--prerelease", action="store_true", help="prereleases count as candidates too")
    add.add_argument("--release-order", choices=("auto", "semver", "published"))
    add.add_argument("--ref", help="the branch or tag a commit-tracked source follows")
    add.add_argument("--paths", nargs="*", default=[], help="the folders or files that matter")
    add.add_argument("--manifest", dest="manifest_path")
    add.add_argument("--for-design", dest="for_designs", action="append", default=[], metavar="PROJECT/DESIGN")
    add.add_argument("--json", action="store_true")

    remove = sub.add_parser("remove", help="stop following a source; its stored evidence stays")
    remove.add_argument("source_id")

    listing = sub.add_parser("list", help="every registered source with its freshness (reads only)")
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="one source: registration, head and observation times (reads only)")
    show.add_argument("source_id")
    show.add_argument("--json", action="store_true")

    check = sub.add_parser("check", help="check sources now, or one bounded fair pass with --scheduled")
    check.add_argument("source_ids", nargs="*", metavar="id")
    check.add_argument("--scheduled", action="store_true", help="the daily OS entry's bounded pass")
    check.add_argument("--json", action="store_true")

    diff = sub.add_parser("diff", help="compare two stored snapshots of one source, offline")
    diff.add_argument("source_id")
    diff.add_argument("--from", dest="from_snapshot", required=True, metavar="SNAPSHOT")
    diff.add_argument("--to", dest="to_snapshot", required=True, metavar="SNAPSHOT")
    diff.add_argument("--json", action="store_true")

    proposals = sub.add_parser("proposals", help="source changes a design could review (reads only)")
    proposals.add_argument("action", choices=("list", "assess"), nargs="?", default="list")
    proposals.add_argument("--design", metavar="PROJECT/DESIGN")
    proposals.add_argument("--json", action="store_true")

    dismiss = sub.add_parser("dismiss", help="hide one proposal, with a reason")
    dismiss.add_argument("key")
    dismiss.add_argument("--reason", required=True)

    materials = sub.add_parser("materials", help="render or check a design's materials part")
    actions = materials.add_subparsers(dest="materials_command", metavar="<materials command>", required=True)
    render = actions.add_parser("render", help="build the materials part from a selection JSON file")
    render.add_argument("--design", required=True, metavar="PROJECT/DESIGN")
    render.add_argument("--input", required=True, help="the selection JSON file, or - for standard input")
    render.add_argument("--out", help="a new file inside this repository (default: standard output)")
    render.add_argument("--allow-unverified", action="store_true")
    render.add_argument("--json", action="store_true", help="print the projection manifest as JSON")
    mcheck = actions.add_parser("check", help="whether a design's material decisions still match their inputs")
    mcheck.add_argument("--design", required=True, metavar="PROJECT/DESIGN")
    mcheck.add_argument("--revision", type=int, help="check the materials part of this revision")
    mcheck.add_argument("--json", action="store_true")

    schedule = sub.add_parser("schedule", help="add, remove or inspect the daily OS entry for source checks")
    schedule.add_argument("action", choices=("install", "remove", "status"))
    schedule.add_argument("--yes", action="store_true", help="agree to changing the OS scheduler")
    schedule.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    from oah.design.fs import RefUnavailable
    from oah.design.model import DesignError
    from oah.design.materials import MaterialsError
    from oah.design.sources import SourceError, StateProblem
    from oah.design.sources.output import OutputError
    from oah.design.sources.view import ViewUnavailable
    from oah.toolkit.schedule import ScheduleError

    try:
        return HANDLERS[args.sources_command](args)
    except (ViewUnavailable, DesignError, RefUnavailable) as exc:
        code = getattr(exc, "code", "ref-unavailable")
        print(f"OAH_SOURCES_DESIGN_UNAVAILABLE: {code}; design authority is unknown. Retry the read before "
              "using design-dependent results.", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except (Refused, SourceError, MaterialsError, OutputError, ScheduleError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT if getattr(exc, "code", "") in ENVIRONMENT_CODES else EXIT_REFUSED
    except (StateProblem, ConfigError, NotARepo, GitMissing, LockTimeout, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(f"OAH_SOURCES_IO: {exc}. Check permissions and free disk space, then retry.", file=sys.stderr)
        return EXIT_ENVIRONMENT


def _sources():
    from oah.design.sources import open_sources

    return open_sources(pathlib.Path.cwd())


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False))


def _status_json(status) -> dict:
    head = status.head
    return {"source": status.spec.to_json(), "freshness": status.freshness, "problem": status.problem,
            "head": None if head is None else head.to_json()}


def _register(args: argparse.Namespace) -> int:
    from oah.design.sources import build_spec

    spec = build_spec(url=args.url, source_id=args.source_id, track=args.track, tag_prefix=args.tag_prefix,
                      prerelease=args.prerelease, ref=args.ref, paths=args.paths, manifest_path=args.manifest_path,
                      for_designs=args.for_designs, release_order=args.release_order)
    outcome, stored = _sources().register(spec)
    if args.json:
        _print({"outcome": outcome, "source": stored.to_json()})
    else:
        print(f"OAH_SOURCES_{outcome.upper()} {stored.id} {stored.locator} ({stored.track})")
    return EXIT_OK


def _remove(args: argparse.Namespace) -> int:
    removed = _sources().remove(args.source_id)
    print(f"OAH_SOURCES_REMOVED {args.source_id}" if removed else f"OAH_SOURCES_ABSENT {args.source_id}")
    return EXIT_OK


def _list(args: argparse.Namespace) -> int:
    sources = _sources()
    registry, raw = sources.registry()
    rows = [_status_json(sources.status(registry.sources[key])) for key in sorted(registry.sources)]
    if args.json:
        _print({"sources": rows, "unreadable": sorted(raw)})
        return EXIT_OK
    for row in rows:
        head = row["head"] or {}
        print(f"{row['source']['id']:24} {row['freshness']:12} {row['source']['track']:8} {row['source']['locator']}"
              f"  confirmed {head.get('confirmed_at') or '-'}")
    for key in sorted(raw):
        print(f"{key:24} unreadable   the registry entry cannot be read")
    return EXIT_OK


def _show(args: argparse.Namespace) -> int:
    sources = _sources()
    spec = sources.spec(args.source_id)
    status = sources.status(spec)
    data = _status_json(status) | {"observations": sources.observations(spec, limit=20)}
    if args.json:
        _print(data)
        return EXIT_OK
    head = data["head"] or {}
    print(f"{spec.id}: {spec.locator} ({spec.track}) {status.freshness}")
    print(f"  spec {spec.spec_digest}, registration {spec.registration}")
    for key in ("attempted_at", "confirmed_at", "outcome"):
        print(f"  {key.replace('_', ' ')}: {head.get(key) or '-'}")
    verified = head.get("verified") or {}
    print(f"  verified snapshot: {verified.get('snapshot') or '-'} at {verified.get('at') or '-'}")
    for record in data["observations"]:
        print(f"  observation {record['digest']}: {record['outcome']} confirmed {record['confirmed_at']}")
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    sources = _sources()
    if args.scheduled:
        if args.source_ids:
            raise Refused("OAH_SOURCES_USAGE", "--scheduled checks the persistent queue; give no source IDs")
        report = sources.scheduled(scheduled=True)
        if args.json:
            _print(report.to_json())
        else:
            print(f"OAH_SOURCES_SCAN {report.outcome} checked={len(report.results)} "
                  f"outstanding={len(report.outstanding)}")
        return EXIT_REFUSED if report.outcome.startswith("partial") else EXIT_OK
    started = sources.now()
    results = sources.check(args.source_ids or None)
    counts: dict[str, int] = {}
    for item in results:
        counts[item.result] = counts.get(item.result, 0) + 1
    sources.record_command("complete", started, counts, False)
    if args.json:
        _print({"results": [item.to_json() for item in results]})
    else:
        for item in results:
            extra = f" {', '.join(item.problems)}" if item.problems else ""
            print(f"{item.source:24} {item.result:16} {item.freshness}{extra}")
    failed = {"failed", "blocked", "unverified", "state-corrupt", "superseded"}
    return EXIT_REFUSED if any(item.result in failed for item in results) else EXIT_OK


def _diff(args: argparse.Namespace) -> int:
    report = _sources().diff(args.source_id, args.from_snapshot, args.to_snapshot)
    if args.json:
        _print(report)
    else:
        print(f"{args.source_id}: {report['completeness']} {args.from_snapshot} -> {args.to_snapshot}")
        counts = (report.get("paths") or {}).get("counts")
        if counts:
            print("  paths: " + ", ".join(f"{key} {value}" for key, value in counts.items()))
        shown = 0
        for entry in (report.get("paths") or {}).get("entries") or []:
            if shown >= 200:
                print(f"  ... {len(report['paths']['entries']) - shown} more (display limit 200; --json lists all)")
                break
            print(f"  {entry['change']:8} {entry['path']}")
            shown += 1
        for item in report.get("text_diffs") or []:
            print(f"  --- {item['item']} ({item['status']}; external data)")
            for line in item.get("lines") or []:
                print(f"  | {line}")
        for problem in report.get("problems") or []:
            print(f"  problem: {problem}")
    return {"complete": EXIT_OK, "partial": EXIT_REFUSED}.get(report["completeness"], EXIT_REFUSED)


def _split_design(value: str) -> tuple[str, str]:
    project, slash, design = value.partition("/")
    if not slash or not project or not design:
        raise Refused("OAH_SOURCES_USAGE", "--design is PROJECT/DESIGN")
    return project, design


def _proposals(args: argparse.Namespace) -> int:
    from oah.config import load_config
    from oah.design import open_store
    from oah.design.sources import assessment, read_view
    from oah.rerank import DEFAULTS

    sources = _sources()
    store = open_store(pathlib.Path.cwd())
    config = load_config(pathlib.Path.cwd()).section("rerank", DEFAULTS)
    reader = lambda project, name: read_view(store, project, name)
    design = None
    if args.design:
        project, name = _split_design(args.design)
        view = reader(project, name)
        design = (view.uid, view.design)
    proposals, findings = sources.proposals(design=design)
    proposals = (assessment.assess(sources, proposals, reader=reader, config=config) if args.action == "assess"
                 else assessment.recorded(sources, proposals, reader=reader, config=config))
    if args.json:
        _print({"proposals": [item.to_json() for item in proposals], "findings": [item.to_json() for item in findings]})
        return EXIT_OK
    for item in proposals:
        print(f"{item.status:9} {item.key}  {item.project}/{item.design} {item.source}: {item.kind} "
              f"{', '.join(item.labels)} jev={item.assessment['jev']} applied={item.assessment['applied']}")
        for label in item.assessment["labels"]:
            print(f"  jev: {label['label']} ({label['probability']:.2f})")
    for item in findings:
        print(f"finding   {item.kind} {item.project}/{item.design} {item.source}: {item.detail}")
    return EXIT_OK


def _dismiss(args: argparse.Namespace) -> int:
    _sources().dismiss(args.key, args.reason)
    print(f"OAH_SOURCES_DISMISSED {args.key}")
    return EXIT_OK


def _materials(args: argparse.Namespace) -> int:
    from oah.design import materials, open_store
    from oah.design.fs import RefUnavailable
    from oah.design.model import DesignError
    from oah.design.sources import ViewUnavailable, read_view

    project, name = _split_design(args.design)
    sources = _sources()
    store = open_store(pathlib.Path.cwd())
    if args.materials_command == "check":
        try:
            view = read_view(store, project, name)
            text = view.current.texts.get("materials") if args.revision is None else store.snapshot(
                project, name, args.revision).texts.get("materials")
        except (ViewUnavailable, DesignError, RefUnavailable) as exc:
            view, unavailable, text = None, getattr(exc, "code", "ref-unavailable"), None
        else:
            unavailable = None
        if text is None and view is not None:
            raise Refused("OAH_MATERIALS_MISSING", f"{project}/{name} has no materials part at that revision")
        report = materials.check(text, sources, None, view, unavailable=unavailable)
        if args.json:
            _print(report)
        else:
            print(f"{project}/{name}: {report['overall']}")
            for item in report["decisions"]:
                problems = f" ({', '.join(item['problems'])})" if item["problems"] else ""
                print(f"  decision {item['id']}: {item['input_status']}{problems}")
            for read in report["source_reads"]:
                print(f"  material {read['material']}: evidence {read['historical_evidence']}, eligibility "
                      f"{read['eligibility'] or '-'}, upstream {read['upstream_freshness']}, confirmed "
                      f"{read['historical_times'].get('confirmed_at') or '-'}")
            for problem in report.get("problems") or []:
                print(f"  problem: {problem}")
        return EXIT_OK if report["overall"] == "current" else EXIT_REFUSED
    view = read_view(store, project, name)
    raw = sys.stdin.read() if args.input == "-" else pathlib.Path(args.input).read_text(encoding="utf-8")
    try:
        selection = json.loads(raw)
    except ValueError as exc:
        raise Refused("OAH_MATERIALS_INVALID", f"the selection is not JSON ({exc})") from None
    try:
        built = materials.build(selection, sources, view=view, allow_unverified=args.allow_unverified)
    except materials.MaterialsError as exc:
        if exc.details and args.json:
            _print({"code": exc.code, "details": exc.details})
        raise
    for warning in built.warnings:
        print(warning.line(), file=sys.stderr)
    if args.out is None:
        if args.json:
            _print({"projection": built.projection, "text": built.text})
        else:
            sys.stdout.write(built.text)
        return EXIT_OK
    from oah.design.sources.output import write_new
    from oah.paths import repo_info

    path = write_new(repo_info(pathlib.Path.cwd()).root, args.out, built.text.encode("utf-8"))
    if args.json:
        _print({"projection": built.projection, "out": str(path)})
    else:
        print(f"OAH_MATERIALS_WRITTEN {path} ({built.projection['part_bytes']} bytes)")
    return EXIT_OK


def schedule_status(entry, sources, *, auto_check: bool, interval_hours: float, runner=None,
                    search_path: str | None = None, now=None) -> dict:
    """ScheduleStatusV1: the OS definition comparison and three separate evidence channels."""
    from oah.toolkit import schedule as schedule_module

    options: dict = {} if runner is None else {"runner": runner}
    evidence = sources.schedule_state()
    status = {"version": 1, "kind": "oah-sources-schedule-status", "auto_check": auto_check,
              "auto_check_note": None if auto_check else "auto-check-off",
              "os_wakeup_seconds": schedule_module.OS_WAKEUP_SECONDS, "check_interval_hours": interval_hours,
              "entry": None if entry is None else {"name": entry.name, "scheduler": entry.kind}}
    if entry is None:
        inspection = None
        state = "unsupported"
    else:
        try:
            inspection = schedule_module.inspect_entry(entry, search_path=search_path, now=now, **options)
            state = inspection.state
        except schedule_module.ScheduleError as exc:
            if exc.code != "OAH_SCHEDULE_TOOL_MISSING":
                raise
            inspection, state = None, "tool-missing"
    details = {} if inspection is None else inspection.to_json()
    os_run = {"status": "unknown", "reason": state}
    if inspection is not None:
        os_run = schedule_module.os_run_observation(entry, inspection, search_path=search_path, now=now, **options)
    return status | {"registration_state": state, "desired_digest": details.get("desired_digest"),
                     "effective_digest": details.get("effective_digest"), "reasons": details.get("reasons", []),
                     "missing_facts": details.get("missing_facts", []), "desired": details.get("desired"),
                     "effective": details.get("effective"), "observations": details.get("observations", []),
                     "evidence": {"source_command_run": evidence.get("source_command_run"),
                                  "cooperative_scheduled_run": evidence.get("cooperative_scheduled_run"),
                                  "os_run_observation": os_run}}


def _schedule(args: argparse.Namespace) -> int:
    from oah.paths import repo_info, state_dir
    from oah.toolkit import schedule as schedule_module

    info = repo_info(pathlib.Path.cwd())
    state = state_dir(info, os.environ)
    sources = _sources()
    try:
        entry = schedule_module.plan_entry(project_root=info.root, home=pathlib.Path.home(), state_dir=state,
                                           purpose="sources")
    except schedule_module.ScheduleError as exc:
        if args.action != "status":
            raise
        entry = None
        print(exc, file=sys.stderr)
    if args.action == "status":
        report = schedule_status(entry, sources, auto_check=sources.settings["auto_check"],
                                 interval_hours=sources.settings["check_interval_hours"])
        if args.json:
            _print(report)
        else:
            print(f"OAH_SCHEDULE {report['registration_state']} {'-' if entry is None else entry.name}")
            print(f"  os_wakeup_seconds={report['os_wakeup_seconds']} check_interval_hours="
                  f"{report['check_interval_hours']} auto_check={'on' if report['auto_check'] else 'off'}")
            for reason in report["reasons"]:
                print(f"  {reason}")
            channels = report["evidence"]
            command = channels["source_command_run"] or {}
            print(f"  last command run: {command.get('ended_at') or '-'} ({command.get('outcome') or '-'}); "
                  "it does not show who started it")
            cooperative = channels["cooperative_scheduled_run"] or {}
            print(f"  last --scheduled run: {cooperative.get('at') or '-'}; a manual command can produce it")
            os_run = channels["os_run_observation"]
            print(f"  OS run record: {os_run.get('status')}"
                  + (f" ({os_run.get('reason')})" if os_run.get("reason") else ""))
        return EXIT_OK
    for line in entry.describe():
        print(line)
    _confirm(("This adds an entry to your OS scheduler." if args.action == "install"
              else "This removes the OS scheduler entry."), args)
    if args.action == "install":
        schedule_module.install_entry(entry)
        print(f"OAH_SCHEDULE_INSTALLED {entry.name}")
        return EXIT_OK
    removed = schedule_module.remove_entry(entry)
    print(f"OAH_SCHEDULE_REMOVED {entry.name}" if removed else f"OAH_SCHEDULE_ABSENT {entry.name}")
    return EXIT_OK


def _confirm(what: str, args: argparse.Namespace) -> None:
    if getattr(args, "yes", False):
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        answer = input(f"{what} Continue? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return
        raise Refused("OAH_SOURCES_DECLINED", "nothing was changed")
    raise Refused("OAH_SOURCES_CONSENT_REQUIRED", f"{what} Run the same command with --yes to agree.")


HANDLERS: Mapping[str, object] = {"register": _register, "remove": _remove, "list": _list, "show": _show,
                                  "check": _check, "diff": _diff, "proposals": _proposals, "dismiss": _dismiss,
                                  "materials": _materials, "schedule": _schedule}
