# 스킬 명령 — oah skills list/install/update/schedule 의 인자, 동의 확인, 자체 리스, 출력 형식, 종료 코드를 맡는다.
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import sys
from typing import Iterator, Mapping

from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError, load_config
from oah.journal import PendingTransaction
from oah.lease import Lease, LeaseError, Store
from oah.lockfile import LockTimeout, StateLock
from oah.paths import GitMissing, NotARepo, RepoInfo, repo_info, state_dir
from oah.scope import ScopeError, normalize_scope
from oah.skillsel import config_defaults
from oah.toolkit import catalog as catalog_module
from oah.toolkit.catalog import CatalogError, kit_skills_dir, load_catalog
from oah.toolkit.fetch import FetchError, StageError
from oah.toolkit.install import (SKILL_FOLDERS, InstallError, apply_install, list_rows, plan_install, run_linked)
from oah.toolkit.schedule import ScheduleError, entry_installed, install_entry, plan_entry, remove_entry
from oah.toolkit.skillmeta import SkillError
from oah.toolkit.update import (DEFAULTS, LeasesActive, cache_dir, find_gitleaks, idle_state, installed_destinations,
                                log_results, refresh_cache, run_update, scheduled_update, vendored_destinations)

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("skills",)
OWN_TASK, OWN_AGENT = "oah-skills", "oah"
OWN_LEASE_HOURS = 1.0
ENVIRONMENT_CODES = ("OAH_SKILLS_TOOL_MISSING", "OAH_SCHEDULE_TOOL_MISSING", "OAH_SCHEDULE_UNSUPPORTED")


class Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def register(commands: argparse._SubParsersAction) -> None:
    skills = commands.add_parser("skills", help="list, install and update bundled and linked skills")
    sub = skills.add_subparsers(dest="skills_command", metavar="<subcommand>", required=True)

    listing = sub.add_parser("list", help="show bundled, cached, linked and installed skills (reads only)")
    listing.add_argument("--json", action="store_true", help="print machine-readable JSON")

    install = sub.add_parser("install", help="copy a bundled skill into skill folders, or run a linked installer")
    install.add_argument("names", nargs="*", metavar="name")
    install.add_argument("--all", action="store_true",
                         help="every default bundled item (opt-in and linked items must be named)")
    install.add_argument("--target", choices=("claude", "codex", "both"), default="both")
    level = install.add_mutually_exclusive_group()
    level.add_argument("--global", "--user", dest="level", action="store_const", const="user",
                       help="your user-level skill folders (global files; asks first)")
    level.add_argument("--project", dest="level", action="store_const", const="project",
                       help="this project's tracked skill folders (the default; takes its own lease)")
    install.set_defaults(level="project")
    install.add_argument("--dry-run", action="store_true", help="show the plan and write nothing")
    install.add_argument("--yes", action="store_true", help="agree to writes outside the project")

    update = sub.add_parser("update", help="refresh the kit cache, or update installed copies, after every check")
    update.add_argument("--check", action="store_true", help="report available updates and write nothing")
    update.add_argument("--yes", action="store_true", help="agree to updating user-level skill folders")
    where = update.add_mutually_exclusive_group()
    where.add_argument("--project", action="store_true",
                       help="update this project's tracked skill folders, as its own leased task")
    where.add_argument("--global", dest="global_", action="store_true",
                       help="update your user-level skill folders (asks first)")
    where.add_argument("--scheduled", action="store_true",
                       help="the automatic check: the kit cache only, when due, never while a lease is active")
    where.add_argument("--vendored", action="store_true",
                       help="update the kit's own skills/ folder (kit maintainers, inside the kit checkout)")

    schedule = sub.add_parser("schedule", help="add or remove a daily OS entry for the automatic check")
    schedule.add_argument("action", choices=("install", "remove", "status"))
    schedule.add_argument("--yes", action="store_true", help="agree to changing the OS scheduler")


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _confirm(what: str, args: argparse.Namespace) -> None:
    if getattr(args, "yes", False):
        return
    if _interactive():
        answer = input(f"{what} Continue? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return
        raise Refused("OAH_SKILLS_DECLINED", "nothing was written")
    raise Refused("OAH_SKILLS_CONSENT_REQUIRED", f"{what} Run the same command with --yes to agree.")


def settings_for(root: pathlib.Path) -> dict:
    """The shared [skills] defaults, with the updater's validation preserved."""
    settings = load_config(root).section("skills", config_defaults())
    pins = settings["pins"]
    if not all(isinstance(name, str) for name in pins):
        raise ConfigError("OAH_CONFIG_INVALID", "skills.pins must be an array of item names, such as "
                                                '["ponytail"]; oah skills list shows the names')
    if settings["update_interval_days"] <= 0:
        raise ConfigError("OAH_CONFIG_INVALID", "skills.update_interval_days must be above 0; "
                                                "set auto_update = false to turn the check off")
    return settings


def run(args: argparse.Namespace) -> int:
    try:
        return HANDLERS[args.skills_command](args)
    except (Refused, InstallError, StageError, ScheduleError, LeaseError, ScopeError, LeasesActive) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT if getattr(exc, "code", "") in ENVIRONMENT_CODES else EXIT_REFUSED
    except (CatalogError, ConfigError, FetchError, SkillError, NotARepo, GitMissing, LockTimeout, StateCorrupt,
            PendingTransaction, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(f"OAH_SKILLS_IO: {exc}. Check permissions and free disk space, then retry.", file=sys.stderr)
        return EXIT_ENVIRONMENT


def _context() -> tuple[RepoInfo, pathlib.Path, pathlib.Path]:
    info = repo_info(pathlib.Path.cwd())
    return info, state_dir(info, os.environ), pathlib.Path.home()


@contextlib.contextmanager
def own_lease(info: RepoInfo, state: pathlib.Path, engines: tuple[str, ...], note: str,
              *, paths: tuple[str, ...] | None = None) -> Iterator[Lease]:
    """Tracked project skill folders change only as their own leased task, released when done."""
    scope = normalize_scope(paths if paths is not None else ["/".join(SKILL_FOLDERS[engine]) for engine in engines], info.root)
    store = Store(state)
    lease = store.start(task=OWN_TASK, agent=OWN_AGENT, scope=scope, note=note, ttl_hours=OWN_LEASE_HOURS, info=info)
    try:
        yield lease
    finally:
        store.release(task=OWN_TASK, agent=OWN_AGENT, reason=f"{note} finished", force=False)
    print(f"OAH_SKILLS_LEASE task={OWN_TASK} scope={','.join(scope)} released")


def _list(args: argparse.Namespace) -> int:
    info, state, home = _context()
    rows = list_rows(catalog=load_catalog(), bundle_dir=kit_skills_dir(), cache_dir=cache_dir(state),
                     project_root=info.root, home=home)
    if args.json:
        print(json.dumps({"skills": [row.to_json() for row in rows]}, indent=2, ensure_ascii=False))
        return EXIT_OK
    for row in rows:
        print(f"{row.name:28} {row.status:28} {row.version:14} {row.license:20} {row.source}\n{'':28} {row.location}")
    return EXIT_OK


def _report_scheduled(state: str, results: list) -> None:
    if state == "ran":
        changed = [r for r in results if r.status != "current"]
        items = sum(1 for r in results if r.kind == "skill")
        pins = len(results) - items
        print(f"OAH_SKILLS_AUTO_UPDATE kit cache checked {items} items" + (f" and {pins} MCP pins" if pins else "")
              + f", {len(changed)} changed, held back or noted")
        for result in changed:
            print(result.describe())
    elif state == "deferred":
        print(f"OAH_SKILLS_AUTO_UPDATE deferred: {results[0].reasons[0]}; it runs once no lease is active")


def before_mutation(info: RepoInfo, state: pathlib.Path) -> None:
    """Best-effort daily cache maintenance for any mutating command, before acquiring its task lease.

    Active leases defer the check. Maintenance errors are reported without failing the caller's work.
    Command owners call this only for writes, never for --check, --dry-run or read-only commands.
    """
    try:
        outcome, results = scheduled_update(catalog=load_catalog(), state_dir=state, bundle_dir=kit_skills_dir(),
                                            settings=settings_for(info.root), now=dt.datetime.now(dt.timezone.utc),
                                            gitleaks=find_gitleaks())
    except (CatalogError, ConfigError, FetchError, StageError, SkillError, LeaseError, GitMissing, NotARepo,
            LockTimeout, StateCorrupt, PendingTransaction, StorageError, OSError, ValueError) as exc:
        print(f"OAH_SKILLS_AUTO_UPDATE skipped: {exc}; run oah skills update to retry", file=sys.stderr)
        return
    _report_scheduled(outcome, results)


def _after_mutation(info: RepoInfo, state: pathlib.Path) -> None:
    before_mutation(info, state)


@contextlib.contextmanager
def _leased_publication(state: pathlib.Path, lease: Lease) -> Iterator[None]:
    with StateLock(Store(state).lock_path):
        if not any(current.id == lease.id and current.scope == lease.scope and current.worktree == lease.worktree
                   for current, stale in Store(state).snapshot() if not stale):
            raise Refused("OAH_SKILLS_LEASE_REQUIRED", "the skill publication lease expired or changed; "
                                                     "rerun the skills command")
        yield


def _engines(target: str) -> tuple[str, ...]:
    return ("claude", "codex") if target == "both" else (target,)


def _install(args: argparse.Namespace) -> int:
    info, state, home = _context()
    catalog = load_catalog()
    if args.all and args.names:
        raise Refused("OAH_SKILLS_USAGE", "give names or --all, not both")
    names = [entry.name for entry in catalog.bundled if entry.default] if args.all else list(args.names)
    if not names:
        raise Refused("OAH_SKILLS_USAGE", "name a skill to install, or use --all; oah skills list shows them")
    plan = plan_install(names, catalog=catalog, source_dirs=[cache_dir(state), kit_skills_dir()], target=args.target,
                        level=args.level, project_root=info.root, home=home)
    print("Plan:")
    for line in plan.describe():
        print(f"  {line}")
    if plan.conflicts:
        raise InstallError("OAH_SKILLS_INSTALL_CONFLICT", "nothing was written; resolve the conflicts above")
    if args.dry_run:
        print("OAH_SKILLS_DRY_RUN nothing was written")
        return EXIT_OK
    if plan.needs_consent:
        _confirm("This writes outside the project (user-level skill folders or a linked installer).", args)
    now = dt.datetime.now(dt.timezone.utc)
    if plan.copies and args.level == "project":
        with own_lease(info, state, _engines(args.target), "oah skills install") as lease:
            done = apply_install(plan, catalog=catalog, now=now,
                                 guard=lambda: _leased_publication(state, lease))
    else:
        done = apply_install(plan, catalog=catalog, now=now,
                             guard=lambda: StateLock(Store(state).lock_path))
    for path in done:
        print(f"OAH_SKILL_INSTALLED {path}")
    for entry in plan.linked:
        for text in run_linked(entry):
            print(f"OAH_SKILL_MANUAL_STEP {entry.name}: type this in the tool yourself: {text}")
        print(f"OAH_SKILL_LINKED {entry.name}")
    _after_mutation(info, state)
    return EXIT_OK


def _print(results: list) -> int:
    for result in results:
        print(result.describe())
    return _exit_code(results)


def _exit_code(results: list) -> int:
    statuses = {result.status for result in results}
    if "failed" in statuses:
        return EXIT_ENVIRONMENT
    if statuses & {"rolled-back", "blocked", "deferred"}:
        return EXIT_REFUSED
    return EXIT_OK


def _update(args: argparse.Namespace) -> int:
    if args.scheduled and args.check:
        raise Refused("OAH_SKILLS_USAGE", "--scheduled cannot be combined with --check; use oah skills update --check")
    info, state, home = _context()
    catalog = load_catalog()
    settings = settings_for(info.root)
    now = dt.datetime.now(dt.timezone.utc)
    gitleaks = find_gitleaks()
    if args.scheduled:
        outcome, results = scheduled_update(catalog=catalog, state_dir=state, bundle_dir=kit_skills_dir(),
                                            settings=settings, now=now, gitleaks=gitleaks)
        _report_scheduled(outcome, results)
        if outcome in ("not-due", "disabled", "busy"):
            print(f"OAH_SKILLS_AUTO_UPDATE {outcome}")
        return _exit_code(results) if outcome == "ran" else EXIT_OK
    common = dict(catalog=catalog, check_only=args.check, settings=settings, now=now, gitleaks=gitleaks, home=home)
    if args.vendored:
        kit = kit_skills_dir()
        catalog_path = info.root / "src" / "oah" / "templates" / "toolkit" / "upstreams.json"
        if (kit is None or kit.resolve() != (info.root / "skills").resolve()
                or not kit.resolve().is_relative_to(info.root.resolve())
                or catalog_module.CATALOG_PATH.resolve() != catalog_path.resolve()
                or not catalog_path.resolve().is_relative_to(info.root.resolve())):
            raise Refused("OAH_SKILLS_NOT_KIT", "--vendored requires this repository to be the actual kit checkout; "
                                                "run it from that checkout with its local oah installation")
        destinations = vendored_destinations(catalog, kit)
        if any(not d.path.resolve().is_relative_to(info.root.resolve()) for d in destinations):
            raise Refused("OAH_SKILLS_NOT_KIT", "a vendored destination points outside the checkout; remove the alias and retry")
        if args.check:
            return _print(run_update(destinations=destinations, mcp=catalog.mcp, **common))
        _confirm("This updates the kit's skills and upstream catalog.", args)
        with own_lease(info, state, (), "oah skills update --vendored",
                       paths=("skills", "src/oah/templates/toolkit/upstreams.json")) as lease:
            results = run_update(destinations=destinations, catalog_path=catalog_path, mcp=catalog.mcp,
                                 guard=lambda: _leased_publication(state, lease), **common)
        if not args.check:
            log_results(state, now, results, "update --vendored")
        return _print(results)
    if args.project or args.global_:
        level = "project" if args.project else "global"
        destinations = installed_destinations(catalog, project_root=info.root, home=home, level=level)
        if not destinations:
            print(f"OAH_SKILLS_NOTHING_TO_UPDATE no oah-installed skills at {level} level; "
                  "oah skills install adds some")
            return EXIT_OK
        if args.check:
            return _print(run_update(destinations=destinations, **common))
        if args.project:
            with own_lease(info, state, ("claude", "codex"), "oah skills update --project") as lease:
                results = run_update(destinations=destinations,
                                     guard=lambda: _leased_publication(state, lease), **common)
        else:
            _confirm("This updates your user-level skill folders.", args)
            results = run_update(destinations=destinations, guard=lambda: idle_state(state), **common)
        log_results(state, now, results, f"update --{level}")
        code = _print(results)
        _after_mutation(info, state)
        return code
    results = refresh_cache(catalog=catalog, state_dir=state, bundle_dir=kit_skills_dir(), settings=settings,
                            now=now, check_only=args.check, gitleaks=gitleaks, why="update")
    return _print(results)


def _schedule(args: argparse.Namespace) -> int:
    info, state, home = _context()
    entry = plan_entry(project_root=info.root, home=home, state_dir=state)
    if args.action == "status":
        print(f"OAH_SCHEDULE {'installed' if entry_installed(entry) else 'absent'} {entry.name}")
        return EXIT_OK
    for line in entry.describe():
        print(line)
    if args.action == "install":
        _confirm("This adds an entry to your OS scheduler.", args)
        install_entry(entry)
        print(f"OAH_SCHEDULE_INSTALLED {entry.name}")
        return EXIT_OK
    _confirm("This removes the OS scheduler entry.", args)
    print(f"OAH_SCHEDULE_REMOVED {entry.name}" if remove_entry(entry) else f"OAH_SCHEDULE_ABSENT {entry.name}")
    return EXIT_OK


HANDLERS: Mapping[str, object] = {"list": _list, "install": _install, "update": _update, "schedule": _schedule}
