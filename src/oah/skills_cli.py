# 스킬 명령 — oah skills list/install/update/schedule 의 인자, 동의 확인, 출력 형식, 종료 코드를 맡는다.
"""Toolkit: bundled, linked and installed skills."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
from typing import Mapping

from oah.atomic import StateCorrupt, StorageError
from oah.journal import PendingTransaction
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, repo_info, state_dir
from oah.toolkit import catalog as catalog_module
from oah.toolkit.catalog import CatalogError, kit_skills_dir, load_catalog
from oah.toolkit.fetch import FetchError, StageError
from oah.toolkit.install import InstallError, apply_install, list_rows, plan_install, run_linked
from oah.toolkit.schedule import ScheduleError, entry_installed, install_entry, plan_entry, remove_entry
from oah.toolkit.skillmeta import SkillError
from oah.toolkit.update import (DEFAULTS, active_leases, installed_destinations, record_run, run_update,
                                scheduled_update, vendored_destinations, find_gitleaks)

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("skills",)


class Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def register(commands: argparse._SubParsersAction) -> None:
    skills = commands.add_parser("skills", help="list, install and update bundled and linked skills")
    sub = skills.add_subparsers(dest="skills_command", metavar="<subcommand>", required=True)

    listing = sub.add_parser("list", help="show bundled, linked and installed skills (reads only)")
    listing.add_argument("--json", action="store_true", help="print machine-readable JSON")

    install = sub.add_parser("install", help="copy a bundled skill into skill folders, or run a linked installer")
    install.add_argument("names", nargs="*", metavar="name")
    install.add_argument("--all", action="store_true", help="every bundled item (linked items must be named)")
    install.add_argument("--target", choices=("claude", "codex", "both"), default="both")
    level = install.add_mutually_exclusive_group()
    level.add_argument("--user", dest="level", action="store_const", const="user",
                       help="your user-level skill folders (global files; asks first)")
    level.add_argument("--project", dest="level", action="store_const", const="project",
                       help="this project's skill folders (the default)")
    install.set_defaults(level="project")
    install.add_argument("--dry-run", action="store_true", help="show the plan and write nothing")
    install.add_argument("--yes", action="store_true", help="agree to writes outside the project")

    update = sub.add_parser("update", help="check upstreams and advance pins that pass every check")
    update.add_argument("--check", action="store_true", help="report available updates and write nothing")
    update.add_argument("--yes", action="store_true", help="agree to updating user-level skill folders")
    update.add_argument("--user", action="store_true", help="also update skills installed at user level")
    update.add_argument("--scheduled", action="store_true",
                        help="the automatic check: only when due, and never while a lease is active")
    update.add_argument("--vendored", action="store_true",
                        help="update the kit's own skills/ folder (kit maintainers, inside the kit checkout)")

    schedule = sub.add_parser("schedule", help="add or remove a daily OS entry for the automatic check")
    schedule.add_argument("action", choices=("install", "remove", "status"))
    schedule.add_argument("--yes", action="store_true", help="agree to writing the OS scheduler entry")
    schedule.add_argument("--user", action="store_true", help="let the entry update user-level skills too")


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
    """The [skills] settings; defaults until the config loader is wired in."""
    return dict(DEFAULTS)


def run(args: argparse.Namespace) -> int:
    try:
        return HANDLERS[args.skills_command](args)
    except (Refused, InstallError, StageError, ScheduleError) as exc:
        code = getattr(exc, "code", "")
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT if code in ("OAH_SKILLS_TOOL_MISSING", "OAH_SCHEDULE_TOOL_MISSING",
                                            "OAH_SCHEDULE_UNSUPPORTED") else EXIT_REFUSED
    except (CatalogError, FetchError, SkillError, NotARepo, GitMissing, LockTimeout, StateCorrupt,
            PendingTransaction, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(f"OAH_SKILLS_IO: {exc}. Check permissions and free disk space, then retry.", file=sys.stderr)
        return EXIT_ENVIRONMENT


def _context() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    info = repo_info(pathlib.Path.cwd())
    return info.root, state_dir(info, os.environ), pathlib.Path.home()


def _list(args: argparse.Namespace) -> int:
    root, _, home = _context()
    rows = list_rows(catalog=load_catalog(), bundle_dir=kit_skills_dir(), project_root=root, home=home)
    if args.json:
        print(json.dumps({"skills": [row.to_json() for row in rows]}, indent=2, ensure_ascii=False))
        return EXIT_OK
    for row in rows:
        print(f"{row.name:28} {row.status:24} {row.version:14} {row.license:20} {row.source}\n{'':28} {row.location}")
    return EXIT_OK


def _report_scheduled(state: str, results: list) -> None:
    if state == "ran":
        changed = [r for r in results if r.status not in ("current",)]
        print(f"OAH_SKILLS_AUTO_UPDATE checked {len(results)} skills, {len(changed)} changed or held back")
        for result in changed:
            print(result.describe())
    elif state == "deferred":
        print(f"OAH_SKILLS_AUTO_UPDATE deferred: {results[0].reasons[0]}; it runs once no lease is active")


def _after_mutation(root: pathlib.Path, state: pathlib.Path, home: pathlib.Path) -> None:
    """The automatic check rides on mutating commands: at most once per interval, never under a lease."""
    try:
        outcome, results = scheduled_update(catalog=load_catalog(), state_dir=state, project_root=root, home=home,
                                            settings=settings_for(root), now=dt.datetime.now(dt.timezone.utc),
                                            gitleaks=find_gitleaks())
    except (CatalogError, FetchError, StateCorrupt, PendingTransaction, StorageError, OSError) as exc:
        print(f"OAH_SKILLS_AUTO_UPDATE skipped: {exc}", file=sys.stderr)
        return
    _report_scheduled(outcome, results)


def _install(args: argparse.Namespace) -> int:
    root, state, home = _context()
    catalog = load_catalog()
    if args.all and args.names:
        raise Refused("OAH_SKILLS_USAGE", "give names or --all, not both")
    names = [entry.name for entry in catalog.bundled] if args.all else list(args.names)
    if not names:
        raise Refused("OAH_SKILLS_USAGE", "name a skill to install, or use --all; oah skills list shows them")
    bundle = kit_skills_dir()
    plan = plan_install(names, catalog=catalog, bundle_dir=bundle, target=args.target, level=args.level,
                        project_root=root, home=home)
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
    done = apply_install(plan, catalog=catalog, bundle_dir=bundle, now=dt.datetime.now(dt.timezone.utc))
    for path in done:
        print(f"OAH_SKILL_INSTALLED {path}")
    for entry in plan.linked:
        for text in run_linked(entry):
            print(f"OAH_SKILL_MANUAL_STEP {entry.name}: type this in the tool yourself: {text}")
        print(f"OAH_SKILL_LINKED {entry.name}")
    _after_mutation(root, state, home)
    return EXIT_OK


def _update(args: argparse.Namespace) -> int:
    root, state, home = _context()
    catalog = load_catalog()
    settings = settings_for(root)
    now = dt.datetime.now(dt.timezone.utc)
    gitleaks = find_gitleaks()
    if args.scheduled:
        if args.user:
            _confirm("This updates your user-level skill folders.", args)
        outcome, results = scheduled_update(catalog=catalog, state_dir=state, project_root=root, home=home,
                                            settings=settings, now=now, include_user=args.user, gitleaks=gitleaks)
        _report_scheduled(outcome, results)
        if outcome in ("not-due", "disabled", "busy"):
            print(f"OAH_SKILLS_AUTO_UPDATE {outcome}")
        return _exit_for(results)
    if args.vendored:
        kit = kit_skills_dir()
        if kit is None:
            raise Refused("OAH_SKILLS_NOT_KIT", "--vendored works only inside an open-agent-harness checkout")
        destinations = vendored_destinations(catalog, kit)
    else:
        destinations = installed_destinations(catalog, project_root=root, home=home, include_user=args.user)
    if not destinations:
        print("OAH_SKILLS_NOTHING_TO_UPDATE no oah-installed skills here; oah skills install adds some")
        return EXIT_OK
    if not args.check and not args.vendored:
        holders = active_leases(state)
        if holders:
            raise Refused("OAH_SKILLS_LEASES_ACTIVE", f"tasks {', '.join(holders)} hold leases; skills change "
                                                      "only when no session is working. Run this again after "
                                                      "oah finish, or use --check to look without changing anything")
        if any(d.level == "user" for d in destinations):
            _confirm("This updates your user-level skill folders.", args)
    results = run_update(catalog=catalog, destinations=destinations, check_only=args.check, settings=settings,
                         now=now, gitleaks=gitleaks,
                         catalog_path=catalog_module.CATALOG_PATH if args.vendored else None)
    for result in results:
        print(result.describe())
    if not args.check and not args.vendored:
        record_run(state, now, results)
    return _exit_for(results)


def _exit_for(results: list) -> int:
    statuses = {result.status for result in results}
    if "failed" in statuses:
        return EXIT_ENVIRONMENT
    if statuses & {"rolled-back", "blocked"}:
        return EXIT_REFUSED
    return EXIT_OK


def _schedule(args: argparse.Namespace) -> int:
    root, state, home = _context()
    entry = plan_entry(project_root=root, home=home, state_dir=state, include_user=args.user)
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
