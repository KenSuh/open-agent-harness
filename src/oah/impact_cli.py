# 영향 명령 — oah impact build/refresh/status 와 경로·DB 개체 질의의 인자, 출력 형식(글·JSON), 종료 코드(0·1·69)를 맡는다. 질의는 작업 트리 지도와 기본 브랜치 지도를 함께 읽는다.
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from oah.atomic import StateCorrupt, StorageError
from oah.impact import defaultbranch, gatebind, lexical, mapstore, query
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
    impact.add_argument("--offline", action="store_true",
                        help="do not fetch the default branch; use the last fetched commit, labelled stale")
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
    return (f"commit={info.commit} manifest={info.manifest_digest[:12]} tool={info.tool_digest[:12]} "
            f"result={info.result_digest[:12]}")


def _action(args: argparse.Namespace, root: pathlib.Path, settings: dict) -> int:
    action = args.targets[0]
    if action == "status":
        info, reason = mapstore.status(root, settings=settings)
        warnings = info.warnings() if info else []
        for warning in warnings:
            print(warning, file=sys.stderr)
        if args.json:
            print(json.dumps({"state": "missing" if info is None and reason == "missing" else
                              (reason if info is None else info.action), "reason": reason,
                              "map": info.to_json() if info else None, "warnings": warnings}, indent=2))
        elif info is None:
            print(f"map: {reason}; run oah impact build")
        elif info.action == "stale":
            print(f"map: stale ({reason}); run oah impact refresh")
        else:
            print(f"map: fresh {_identity(info)}")
        return EXIT_OK
    info = mapstore.build(root, settings=settings) if action == "build" else mapstore.ensure_fresh(root, settings=settings)
    for warning in info.warnings():
        print(warning, file=sys.stderr)
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
    return inside


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


def _with_live_hits(root: pathlib.Path, data: dict, commit: str | None, targets: list[str], settings: dict) -> dict:
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


def _rows(data: dict, targets: list[str], **options) -> list[query.Row]:
    """query.query, but each DB entity the map does not know is left out instead of refusing."""
    graph = data["db"] or {"entities": {}}
    known = [t for t in targets if query.normalize_entity(t) is None or query.normalize_entity(t) in graph["entities"]]
    return query.query(data, known, **options)


def _both_maps(root: pathlib.Path, data: dict, branch: defaultbranch.DefaultBranch, targets: list[str],
               gates: list, settings: dict, *, warnings: list[str] | None = None) -> tuple[list[query.Row], dict]:
    """Rows from the worktree map and the default-branch map, merged (A23). An entity is refused only when
    neither map knows it. Gates belong only to the explicitly queried worktree or revision; the
    default-branch mirror contributes its existing map layers without discovering gate ledgers."""
    graph = query.load_graph(root, settings)
    if branch.data is None:
        rows = query.query(data, targets, gates=gates, graph=graph, settings=settings)
        return rows, {row: "worktree" for row in rows}
    source = defaultbranch._snapshot_source(root, branch.commit)
    if branch.state == "stale":
        try:
            mapstore.resolve_rev(source, branch.commit, timeout=settings["git_timeout"])
        except ImpactError:
            source = root  # Maps recorded before mirror isolation still have their objects in the user's repo.
    try:
        theirs = _with_live_hits(source, branch.data, branch.commit, targets, settings)
    except ImpactError:
        if branch.state != "stale":
            raise
        # A missing legacy cache may no longer contain this snapshot.
        # Its persisted rows remain usable, even when additional missing-path searches cannot run.
        theirs = branch.data
        if warnings is not None:
            warnings.append(f"OAH_IMPACT_INCOMPLETE: additional path searches at default-branch commit={branch.commit} "
                            "could not run; impact used its recorded rows. Restore the remote and retry without "
                            "--offline or --no-refresh to fetch a complete map")
    for target in targets:
        entity = query.normalize_entity(target)
        if entity and all(entity not in (m["db"] or {"entities": {}})["entities"] for m in (data, theirs)):
            query.query(data, [target], settings=settings)  # raises the worktree map's refusal
    mine = _rows(data, targets, gates=gates, graph=graph, settings=settings)
    return query.merge_maps(mine, _rows(theirs, targets, settings=settings))


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
    warnings: list[str] = info.warnings()
    for warning in warnings:
        print(warning, file=sys.stderr)

    def add(items: list[str]) -> None:
        targets.extend(item for item in items if item not in targets)

    requested = [query.normalize_entity(raw) or _relative(raw, root) for raw in args.targets]
    scope = _task_scope(root, args.task) if args.task else None
    # Keep the mirror alive through missing-target searches as well as fetch and map construction.
    with defaultbranch._query_context(root, settings, offline=args.offline, no_refresh=args.no_refresh) as branch:
        maps = [data["files"]] + ([branch.data["files"]] if branch.data else [])
        files = {path: kind for mapped in reversed(maps) for path, kind in mapped.items()}
        for target in requested:
            if query.normalize_entity(target):
                add([target])
            else:
                selected = [path for mapped in maps for path in _expand(target, mapped)]
                add(selected or [target])
        if scope is not None:
            selected, notes = query.select_scope(scope, files, args.kind)
            warnings += notes
            add(selected)
        changed = _changed(root, since, info.commit if args.ref else None) if since else []
        add(changed)
        data = _with_live_hits(root, data, info.commit if args.ref else None, targets, settings)
        gates, gate_status = gatebind.discover(root, rev=info.commit if args.ref else None)
        if branch.warning():
            warnings.append(branch.warning())
        if branch.info:
            warnings += [f"{warning} [default-branch map commit={branch.commit}]" for warning in branch.info.warnings()]
        rows, found = _both_maps(root, data, branch, targets, gates, settings, warnings=warnings)
    rows, rerank = query.apply_rerank(rows, disabled=args.no_rerank, enabled=settings["rerank"])
    shown, total, overflow = query.cap(rows, limit=settings["limit"], strong_only=args.strong_only)
    identity = query.db_identity(data, info.commit, shown, found,
                                 default_branch=(branch.data, branch.commit) if branch.data is not None else None)
    plan = query.rerun_plan([row for row in rows if row.target in changed]) if since else []
    for warning in warnings[len(info.warnings()):]:
        print(warning, file=sys.stderr)
    first = f"{'ref' if args.ref else 'worktree'}={info.commit}"
    if args.json:
        report = {"targets": targets, "map": info.to_json(), "label": f"{first} {branch.label()}",
                  "maps": {"worktree": {"commit": info.commit, "ref": args.ref}, "default_branch": branch.to_json()},
                  "rerank": rerank, "gates": gate_status, "db_impact": identity, "warnings": warnings,
                  "rows": [query.row_json(row, found) for row in shown],
                  "total": total, "shown": len(shown), "overflow": overflow,
                  "changed_since": {"rev": since, "files": changed, "rerun": plan} if since else None}
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return EXIT_OK
    listed = ", ".join(targets[:5]) + (f" and {len(targets) - 5} more" if len(targets) > 5 else "")
    print(f"impact: {listed or '(nothing selected)'}")
    print(f"map: {_identity(info)} ({info.action})")
    print(f"maps: {first} {branch.details()}")
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
        only = "  [default-branch only]" if found.get(row) == "default-branch" else ""
        print(f"{row.binding:<8}{row.layer:<10}{row.kind:<10}{row.where}  {row.target}  {row.detail}{only}")
    if not shown:
        print("no references found")
    print(query.count_line(total, len(shown), overflow))
    return EXIT_OK
