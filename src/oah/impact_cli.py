# 영향 명령 — oah impact build/refresh/status 와 경로·DB 개체 질의의 인자, 출력 형식(글·JSON), 종료 코드(0·1·69)를 맡는다.
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from oah.atomic import StateCorrupt, StorageError
from oah.impact import gatebind, lexical, mapstore, query
from oah.config import ConfigError
from oah.impact.defaults import configured
from oah.impact.errors import EXIT_ENVIRONMENT, EXIT_USAGE, ImpactError
from oah.journal import PendingTransaction
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, repo_info, state_dir

EXIT_OK = 0
COMMANDS = ("impact",)
ACTIONS = ("build", "refresh", "status")


def register(commands: argparse._SubParsersAction) -> None:
    impact = commands.add_parser("impact", help="list what to re-check when files or DB objects change")
    impact.add_argument("targets", nargs="*", metavar="TARGET",
                        help="files, folders or DB entities (db_table:NAME, db_routine:NAME, db_grant:ROLE); "
                             "or one of build, refresh, status")
    impact.add_argument("--changed-since", metavar="REV", help="add the files changed since REV and list what to re-run")
    impact.add_argument("--strong-only", action="store_true", help="list every pin and exec result, nothing weaker")
    impact.add_argument("--json", action="store_true", help="print machine-readable JSON")
    impact.add_argument("--ref", metavar="REV", help="answer for another commit (built in memory, not stored)")
    impact.add_argument("--no-refresh", action="store_true", help="refuse a stale map instead of refreshing it")
    impact.add_argument("--no-rerank", action="store_true", help="do not re-rank (the brief says so)")
    impact.add_argument("--task", help="use this task's lease Scope as the targets")
    impact.add_argument("--kind", action="append", help="with --task: only files of this kind; repeat for more")


def run(args: argparse.Namespace) -> int:
    try:
        root = repo_info(pathlib.Path.cwd()).root
        settings = configured(root)
        if len(args.targets) == 1 and args.targets[0] in ACTIONS:
            return _action(args, root, settings)
        return _query(args, root, settings)
    except ImpactError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except (ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, StorageError, PendingTransaction) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(StorageError(pathlib.Path(exc.filename or "."), exc), file=sys.stderr)
        return EXIT_ENVIRONMENT


def _identity(info: mapstore.MapInfo) -> str:
    return f"commit={info.commit} tool={info.tool_digest[:12]} result={info.result_digest[:12]}"


def _action(args: argparse.Namespace, root: pathlib.Path, settings: dict) -> int:
    action = args.targets[0]
    if action == "status":
        info, reason = mapstore.status(root, settings=settings)
        if args.json:
            print(json.dumps({"state": "missing" if info is None and reason == "missing" else
                              (reason if info is None else info.action), "reason": reason,
                              "map": info.to_json() if info else None}, indent=2))
        elif info is None:
            print(f"map: {reason}; run oah impact build")
        elif info.action == "stale":
            print(f"map: stale ({reason}); run oah impact refresh")
        else:
            print(f"map: fresh {_identity(info)}")
        return EXIT_OK
    info = mapstore.build(root, settings=settings) if action == "build" else mapstore.ensure_fresh(root, settings=settings)
    if args.json:
        print(json.dumps(info.to_json(), indent=2))
    elif info.action == "built":
        print(f"OAH_IMPACT_BUILT {_identity(info)} files={info.changed}")
    elif info.action == "refreshed":
        print(f"OAH_IMPACT_REFRESHED {_identity(info)} changed={info.changed}")
    else:
        print(f"OAH_IMPACT_FRESH {_identity(info)}")
    return EXIT_OK


def _relative(raw: str, root: pathlib.Path) -> str:
    path = pathlib.Path(raw)
    absolute = pathlib.Path(os.path.normpath(path if path.is_absolute() else pathlib.Path.cwd() / path))
    for candidate, base in ((absolute, root), (absolute.resolve(), root.resolve())):
        try:
            relative = candidate.relative_to(base).as_posix()
            return "" if relative == "." else relative
        except ValueError:
            continue
    raise ImpactError("OAH_IMPACT_PATH", f"{raw!r} is outside this repository; pass paths inside it", EXIT_USAGE)


def _expand(relative: str, files: dict) -> list[str]:
    if relative in files:
        return [relative]
    prefix = relative + "/" if relative else ""
    inside = [path for path in sorted(files) if path.startswith(prefix)]
    return inside or [relative]


def _git_names(root: pathlib.Path, *args: str) -> list[str]:
    output = mapstore._git(root, *args, timeout=60.0)
    return [name for name in output.split("\0") if name]


def _changed(root: pathlib.Path, since: str, ref: str | None) -> list[str]:
    if ref:
        return sorted(set(_git_names(root, "diff", "--no-renames", "--name-only", "-z", since, ref, "--")))
    names = _git_names(root, "diff", "--no-renames", "--name-only", "-z", since, "--")
    names += _git_names(root, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(names))


def _task_scope(root: pathlib.Path, task: str) -> list[str]:
    from oah.lease import Store

    for lease, _stale in Store(state_dir(repo_info(root), os.environ)).snapshot():
        if lease.task == task:
            return list(lease.scope)
    raise ImpactError("OAH_IMPACT_NO_TASK", f"no lease for task {task!r}; start one with oah start, "
                      "or pass paths instead", EXIT_USAGE)


def _with_live_hits(root: pathlib.Path, data: dict, commit: str, targets: list[str], settings: dict) -> dict:
    """Search by name for targets the map does not track (new or deleted files); nothing is stored."""
    known = mapstore.needle_set(data["files"], settings["min_needle"])
    missing = set()
    for target in targets:
        if query.normalize_entity(target) is None and target not in data["files"]:
            missing.update(set(lexical.needles_for(target, settings["min_needle"])) - known)
    if not missing:
        return data
    extra = mapstore.scan_lexical(root, commit, missing, data["files"], None, settings["git_timeout"])
    return dict(data, hits={**data["hits"], **extra})


def _query(args: argparse.Namespace, root: pathlib.Path, settings: dict) -> int:
    if args.kind and not args.task:
        raise ImpactError("OAH_IMPACT_USAGE", "--kind only filters a task's Scope; add --task <task>", EXIT_USAGE)
    if not args.targets and not args.task and not args.changed_since:
        raise ImpactError("OAH_IMPACT_USAGE", "name at least one file, folder or db_table:NAME entity, or use "
                          "--changed-since REV or --task TASK (try: oah impact src/app.py)", EXIT_USAGE)
    since = mapstore.resolve_rev(root, args.changed_since) if args.changed_since else None
    if args.ref:
        info, data = mapstore.snapshot(root, args.ref, settings=settings)
    else:
        info, data = mapstore.fresh_map(root, refresh=not args.no_refresh, settings=settings)
    targets: list[str] = []
    warnings: list[str] = []

    def add(items: list[str]) -> None:
        targets.extend(item for item in items if item not in targets)

    for raw in args.targets:
        entity = query.normalize_entity(raw)
        add([entity] if entity else _expand(_relative(raw, root), data["files"]))
    if args.task:
        selected, notes = query.select_scope(_task_scope(root, args.task), data["files"], args.kind)
        warnings += notes
        add(selected)
    changed = _changed(root, since, info.commit if args.ref else None) if since else []
    add(changed)
    data = _with_live_hits(root, data, info.commit, targets, settings)
    gates, gate_status = gatebind.discover(root)
    rows = query.query(data, targets, gates=gates, graph=query.load_graph(root, settings), settings=settings)
    rows, rerank = query.apply_rerank(rows, disabled=args.no_rerank)
    shown, total, overflow = query.cap(rows, limit=settings["limit"], strong_only=args.strong_only)
    identity = query.db_identity(data, info.commit, shown)
    plan = query.rerun_plan([row for row in rows if row.target in changed]) if since else []
    for warning in warnings:
        print(warning, file=sys.stderr)
    if args.json:
        report = {"targets": targets, "map": info.to_json(), "rerank": rerank, "gates": gate_status,
                  "db_impact": identity, "warnings": warnings, "rows": [row.to_json() for row in shown],
                  "total": total, "shown": len(shown), "overflow": overflow,
                  "changed_since": {"rev": since, "files": changed, "rerun": plan} if since else None}
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return EXIT_OK
    listed = ", ".join(targets[:5]) + (f" and {len(targets) - 5} more" if len(targets) > 5 else "")
    print(f"impact: {listed or '(nothing selected)'}")
    print(f"map: {_identity(info)} ({info.action})")
    print(f"rerank={rerank}")
    print(f"gates: {gate_status}")
    print(query.db_line(identity))
    if since:
        print(f"changed since {since[:12]}: {len(changed)} file{'' if len(changed) == 1 else 's'}")
        for item in plan:
            where = item["source"] if item["line"] is None else f"{item['source']}:{item['line']}"
            print(f"re-run: {where} ({item['binding']} {', '.join(item['targets'])})")
        if not plan:
            print("re-run: nothing is bound to these files by pin or exec")
    for row in shown:
        print(f"{row.binding:<8}{row.layer:<10}{row.kind:<10}{row.where}  {row.target}  {row.detail}")
    if not shown:
        print("no references found")
    print(query.count_line(total, len(shown), overflow))
    return EXIT_OK
