# 작업 전 사전 점검 — 기준 최신성, 작업 트리, 리스 겹침, 최근 커밋, 영향, 패킷, 프로젝트 검사를 돌려 PROCEED/경고/ABORT 를 판정한다.
"""Preflight: is it safe to start this task on this base? (PLAN 5.7)

`run_checks()` runs seven checks in order and returns a `Report`:

1. `base`: fetch the default branch (never prompting for a password) and count how far HEAD is
   behind it. Behind, or a remote that cannot be reached without `--offline`, is ABORT. No remote
   at all is a WARN.
2. `dirty`: uncommitted changes are a WARN.
3. `leases`: active leases of other tasks that overlap the compared scope are ABORT; stale ones are
   a WARN. The compared scope is `--touch`, the packet's Scope and the task's own lease scope; when
   it is empty the check warns (`OAH_SCOPE_EMPTY`) instead of passing silently.
4. `recent`: commits on the default branch since the packet's base that touch the compared scope
   are a WARN. The base is the task lease's starting commit, else the last commit that changed the
   packet, else the packet's modification time.
5. `impact`: the `impact` hook, given the normalized `--touch` paths, returns information lines
   (the bound gates). Without a hook the report says `impact=unavailable`.
6. `packet`: with a task, its packet must check clean, be accepted and have success conditions.
7. `project`: each configured argv must exit 0.

Settings are parameters (`DEFAULTS`, the `[preflight]` section of `.oah/config.toml`). The CLI passes
`[preflight].checks` as `project_checks`; lane P4b injects `oah.impact.ensure_fresh` as `impact`. The verdict is PROCEED (0),
PROCEED_WITH_WARNINGS (0) or ABORT (2), and the rendered report ends with what it did not prove.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import pathlib
from typing import Callable, Mapping, Sequence

from oah import packet
from oah.atomic import StateCorrupt, StorageError
from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command
from oah.journal import PendingTransaction
from oah.lease import Lease, Store
from oah.paths import RepoInfo, git_argv, run_git
from oah.scope import REPO, ScopeError, normalize_scope, overlaps

DEFAULTS: dict[str, object] = {
    "remote": "",            # empty: origin, or the only remote
    "default_branch": "",    # empty: the remote's HEAD branch
    "fetch_timeout": 60.0,
    "check_timeout": 300.0,
    "checks": [],            # project checks: argv lists that must exit 0
}
LEVELS = ("ok", "info", "warn", "abort")
MAX_LISTED = 20
NETWORK_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

ImpactHook = Callable[[pathlib.Path, tuple[str, ...]], Sequence[str]]


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    level: str
    message: str
    details: tuple[str, ...] = ()


@dataclasses.dataclass
class Report:
    checks: list[Check]
    impact: str
    not_proven: list[str]

    @property
    def verdict(self) -> str:
        levels = {check.level for check in self.checks}
        if "abort" in levels:
            return "ABORT"
        return "PROCEED_WITH_WARNINGS" if "warn" in levels else "PROCEED"

    @property
    def exit_code(self) -> int:
        return 2 if self.verdict == "ABORT" else 0

    def render(self) -> str:
        lines = []
        for check in self.checks:
            lines.append(f"[{check.level}]".ljust(8) + f"{check.name}: {check.message}")
            lines.extend(f"          {detail}" for detail in check.details)
        aborts = sum(check.level == "abort" for check in self.checks)
        warnings = sum(check.level == "warn" for check in self.checks)
        lines.append(f"impact={self.impact}")
        lines.append(f"OAH_PREFLIGHT verdict={self.verdict} aborts={aborts} warnings={warnings}")
        if self.verdict == "ABORT":
            lines.append("Fix the [abort] lines above, then run oah preflight again.")
        elif warnings:
            lines.append("Read the [warn] lines above before you start.")
        lines.append("Not proven by this preflight:")
        lines.extend(f"- {item}" for item in self.not_proven)
        return "\n".join(lines) + "\n"


@dataclasses.dataclass
class _Context:
    info: RepoInfo
    settings: dict
    offline: bool
    not_proven: list[str]
    remote: str | None = None
    ref: str | None = None          # the default branch as a ref, such as refs/remotes/origin/main
    ref_name: str | None = None     # its short name, such as origin/main
    task_lease: Lease | None = None
    packet_result: packet.CheckResult | None = None


def _git(ctx: _Context, *args: str) -> tuple[int | None, str, str]:
    result = run_git(ctx.info.root, *args)
    return result.returncode, result.stdout, result.stderr


def _network_git(ctx: _Context, *args: str) -> tuple[int | None, str, str]:
    env = dict(os.environ, **NETWORK_ENV)
    result = run_command(git_argv() + ("-C", str(ctx.info.root), *args),
                         timeout=float(ctx.settings["fetch_timeout"]), env=env)
    if result.timed_out:
        return None, result.stdout, f"timed out after {ctx.settings['fetch_timeout']} s"
    return result.returncode, result.stdout, result.stderr


def _ref_exists(ctx: _Context, ref: str) -> bool:
    return _git(ctx, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] == 0


def _pick_remote(ctx: _Context) -> tuple[str | None, str | None]:
    """The remote to compare with, or (None, reason)."""
    code, out, err = _git(ctx, "remote")
    if code != 0:
        return None, f"git remote failed: {err.strip()}; fix the repository, then run oah preflight again"
    remotes = [line.strip() for line in out.splitlines() if line.strip()]
    if not remotes:
        return None, "none"
    wanted = str(ctx.settings["remote"] or "")
    if wanted:
        return ((wanted, None) if wanted in remotes else
                (None, f"the configured remote {wanted!r} does not exist; set [preflight] remote in "
                       ".oah/config.toml to an existing remote, then run oah preflight again"))
    if "origin" in remotes:
        return "origin", None
    if len(remotes) == 1:
        return remotes[0], None
    return None, (f"there are several remotes ({', '.join(remotes)}) and none is called origin; set "
                  "[preflight] remote in .oah/config.toml")


def _default_branch(ctx: _Context, remote: str) -> tuple[str | None, str | None]:
    """The default branch name, or (None, problem) where problem 'unreachable: ...' means ABORT."""
    configured = str(ctx.settings["default_branch"] or "")
    if configured:
        return configured, None
    code, out, _ = _git(ctx, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    if code == 0 and out.strip().startswith(remote + "/"):
        return out.strip()[len(remote) + 1:], None
    if not ctx.offline:
        code, out, err = _network_git(ctx, "ls-remote", "--symref", remote, "HEAD")
        if code != 0:
            return None, f"unreachable: {err.strip() or 'no answer'}"
        for line in out.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                return line[len("ref: refs/heads/"):-len("\tHEAD")], None
    for guess in ("main", "master"):
        if _ref_exists(ctx, f"refs/remotes/{remote}/{guess}"):
            return guess, None
    return None, f"{remote} has no default branch yet"


def _check_base(ctx: _Context) -> Check:
    remote, why = _pick_remote(ctx)
    if remote is None:
        if why == "none":
            ctx.not_proven.append("that nobody else changed the default branch: this repository has no remote")
            return Check("base", "warn", "there is no remote, so base freshness does not apply; that is fine "
                                         "for a local-only repository")
        ctx.not_proven.append("that the base is fresh: no remote was compared")
        return Check("base", "warn" if ctx.offline else "abort", why or "no remote to compare with; configure "
                     "[preflight] remote in .oah/config.toml and run oah preflight again")
    ctx.remote = remote
    branch, problem = _default_branch(ctx, remote)
    if branch is None:
        if problem and problem.startswith("unreachable: "):
            return Check("base", "abort", f"cannot reach {remote} ({problem[len('unreachable: '):]}). Check the "
                                          "network or your sign-in, or pass --offline to compare with the last "
                                          "fetched state")
        ctx.not_proven.append("that the base is fresh: the remote has no default branch to compare with")
        return Check("base", "warn", f"{problem}; push a first commit or set [preflight] default_branch")
    ctx.ref, ctx.ref_name = f"refs/remotes/{remote}/{branch}", f"{remote}/{branch}"
    if ctx.offline:
        ctx.not_proven.append(f"that the base is fresh: {remote} was not fetched (--offline)")
    else:
        code, _, err = _network_git(ctx, "fetch", "--quiet", "--no-tags", remote,
                                    f"+refs/heads/{branch}:{ctx.ref}")
        if code != 0:
            return Check("base", "abort", f"cannot fetch {ctx.ref_name} ({err.strip() or 'no answer'}). Check the "
                                          "network or your sign-in, or pass --offline to compare with the last "
                                          "fetched state")
    if not _ref_exists(ctx, ctx.ref):
        ctx.ref = ctx.ref_name = None
        ctx.not_proven.append("that the base is fresh: the default branch was never fetched")
        return Check("base", "warn", f"{remote}/{branch} is not known locally; run git fetch {remote}")
    if ctx.info.head is None:
        return Check("base", "warn", "this branch has no commits yet, so there is nothing to compare")
    code, out, err = _git(ctx, "rev-list", "--count", f"HEAD..{ctx.ref}")
    if code != 0:
        return Check("base", "abort", f"git could not compare HEAD with {ctx.ref_name}: {err.strip()}")
    behind = int(out.strip() or "0")
    source = "last fetched " if ctx.offline else ""
    if behind:
        plural = "commit" if behind == 1 else "commits"
        return Check("base", "abort", f"HEAD is {behind} {plural} behind {source}{ctx.ref_name}. Update first: "
                                      f"git rebase {ctx.ref_name}")
    if ctx.offline:
        return Check("base", "warn", f"offline: HEAD is up to date with the last fetched {ctx.ref_name}, which may "
                                     "be old")
    return Check("base", "ok", f"HEAD is up to date with {ctx.ref_name}")


def _check_dirty(ctx: _Context) -> Check:
    code, out, err = _git(ctx, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if code != 0:
        return Check("dirty", "warn", f"git status failed: {err.strip()}")
    entries, fields, skip = [], out.split("\0"), False
    for field in fields:
        if skip:
            skip = False
            continue
        if len(field) > 3:
            entries.append(field[3:])
            skip = field[0] in "RC"  # a rename or copy carries its old path in the next field
    if not entries:
        return Check("dirty", "ok", "the working tree is clean")
    shown = entries[:MAX_LISTED] + ([f"and {len(entries) - MAX_LISTED} more"] if len(entries) > MAX_LISTED else [])
    plural = "change" if len(entries) == 1 else "changes"
    return Check("dirty", "warn", f"{len(entries)} uncommitted {plural}; commit or stash them, or continue "
                                  "knowing they are there", tuple(shown))


def _compare_scope(ctx: _Context, touch: tuple[str, ...]) -> tuple[tuple[str, ...], list[str]]:
    scope: list[str] = []
    problems: list[str] = []
    sources = [("--touch", list(touch))]
    if ctx.packet_result is not None:
        sources.append(("the packet Scope", list(ctx.packet_result.scope)))
    for label, raw in sources:
        if not raw:
            continue
        try:
            scope.extend(normalize_scope(raw, ctx.info.root))
        except ScopeError as exc:
            problems.append(f"{label}: {exc}")
    if ctx.task_lease is not None:
        scope.extend(ctx.task_lease.scope)
    unique: list[str] = []
    for entry in scope:
        if entry not in unique:
            unique.append(entry)
    return (REPO,) if REPO in unique else tuple(unique), problems


def _check_leases(ctx: _Context, scope: tuple[str, ...], scope_problems: list[str],
                  entries: list[tuple[Lease, bool]] | None, store_error: str | None, task: str | None) -> Check:
    if scope_problems:
        return Check("leases", "abort", "; ".join(scope_problems))
    if store_error is not None:
        return Check("leases", "abort", f"cannot read the leases: {store_error}")
    others = [(lease, stale) for lease, stale in entries or [] if lease.task != task]
    if not scope:
        ctx.not_proven.append("that your files are free of other leases: there was no scope to compare")
        return Check("leases", "warn", "OAH_SCOPE_EMPTY: nothing to compare with other leases. Pass --touch PATH… "
                                       "or --task with a packet that lists its Scope",
                     tuple(f"held: {lease.describe()}" for lease, stale in others if not stale))
    active = [lease for lease, stale in others if not stale and overlaps(lease.scope, scope)]
    stale = [lease for lease, stale in others if stale and overlaps(lease.scope, scope)]
    if active:
        return Check("leases", "abort", f"{', '.join(lease.task for lease in active)} already holds overlapping "
                                        "paths. Wait for it to finish, pick other paths, or ask its holder to "
                                        "release it", tuple(lease.describe() for lease in active + stale))
    if stale:
        return Check("leases", "warn", f"STALE leases overlap your paths: {', '.join(l.task for l in stale)}. They "
                                       "expired; oah start prunes them, or ask their holders",
                     tuple(lease.describe() for lease in stale))
    return Check("leases", "ok", f"no other lease overlaps {', '.join(scope)}")


def _packet_base(ctx: _Context, path: pathlib.Path | None) -> tuple[list[str], str] | None:
    """`git log` arguments that select commits since the packet's base, with a description."""
    if ctx.task_lease is not None and ctx.task_lease.head_at_start:
        start = ctx.task_lease.head_at_start
        return [f"{start}..{ctx.ref}"], f"since the lease started at {start[:12]}"
    if path is None:
        return None
    try:
        rel = path.resolve().relative_to(ctx.info.root.resolve()).as_posix()
    except ValueError:
        rel = None
    if rel is not None:
        code, out, _ = _git(ctx, "log", "-1", "--format=%H", "--", rel)
        if code == 0 and out.strip():
            return [f"{out.strip()}..{ctx.ref}"], f"since the packet was last committed ({out.strip()[:12]})"
    try:
        moment = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    except OSError:
        return None
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [f"--since={stamp}", ctx.ref], f"since the packet was last edited ({stamp})"


def _check_recent(ctx: _Context, scope: tuple[str, ...], path: pathlib.Path | None) -> Check:
    if ctx.ref is None:
        return Check("recent", "info", "skipped: no default branch to read")
    if not scope:
        return Check("recent", "info", "skipped: no scope to compare")
    base = _packet_base(ctx, path)
    if base is None:
        return Check("recent", "info", "skipped: no packet base (pass --task with a packet)")
    selection, described = base
    specs = [] if REPO in scope else ["--", *(f":(literal,icase){entry}" for entry in scope)]
    code, out, err = _git(ctx, "log", f"--max-count={MAX_LISTED}", "--format=%h %s", *selection, *specs)
    if code != 0:
        return Check("recent", "warn", f"git log failed: {err.strip()}")
    commits = [line for line in out.splitlines() if line.strip()]
    if not commits:
        return Check("recent", "ok", f"no commits on {ctx.ref_name} {described} touch your scope")
    plural = "commit" if len(commits) == 1 else "commits"
    more = "+" if len(commits) == MAX_LISTED else ""
    return Check("recent", "warn", f"{len(commits)}{more} {plural} on {ctx.ref_name} {described} touch your "
                                   "scope; read them before you start", tuple(commits))


def _check_impact(ctx: _Context, impact: ImpactHook | None, touch: tuple[str, ...]) -> tuple[Check, str]:
    if impact is None:
        ctx.not_proven.append("what else the touched paths affect (impact=unavailable)")
        return Check("impact", "info", "impact=unavailable: the impact map is not set up yet"), "unavailable"
    if not touch:
        ctx.not_proven.append("what else your paths affect: no --touch paths were given")
        return Check("impact", "info", "skipped: pass --touch PATH… to see bound gates"), "skipped(no --touch paths)"
    try:
        lines = tuple(str(line) for line in impact(ctx.info.root, touch))
    except Exception as exc:  # a broken plugin must not hide the other checks
        reason = f"{type(exc).__name__}: {exc}"
        ctx.not_proven.append("what else the touched paths affect (impact=failed)")
        return Check("impact", "warn", f"the impact step failed ({reason})"), f"failed({reason})"
    return Check("impact", "info", f"{len(lines)} item(s) for {', '.join(touch)}", lines), "used"


def _check_packet(ctx: _Context, task: str | None, path: pathlib.Path | None) -> Check:
    if task is None or path is None:
        ctx.not_proven.append("that the task has an accepted packet (no --task)")
        return Check("packet", "info", "skipped: no --task")
    result = ctx.packet_result
    errors = [str(p) for p in result.errors]
    warnings = [str(p) for p in result.warnings]
    if any(p.code == "OAH_INTENT_SOURCE_MISSING" for p in result.errors):
        return Check("packet", "abort", f"there is no packet for {task}; create it with oah packet new {task}")
    if errors:
        return Check("packet", "abort", f"the packet for {task} is not ready; fix it and run oah packet check "
                                        f"{task}", tuple(errors + warnings))
    if warnings:
        return Check("packet", "warn", f"{task}@r{result.intent.revision} is accepted, with warnings",
                     tuple(warnings))
    return Check("packet", "ok", f"{task}@r{result.intent.revision} is accepted with {len(result.intent.acs)} "
                                 "success condition(s)")


def _check_project(ctx: _Context, project_checks: Sequence[Sequence[str]]) -> Check:
    if not project_checks:
        ctx.not_proven.append("project-specific rules: none are configured in [preflight].checks")
        return Check("project", "info", "no project checks configured")
    timeout = float(ctx.settings["check_timeout"])
    for argv in project_checks:
        argv = [str(part) for part in argv]
        shown = " ".join(argv)
        if not argv:
            return Check("project", "abort", "an empty project check is configured; remove it")
        command = argv
        if not any(sep in argv[0] for sep in ("/", "\\")):
            try:
                found = resolve_command(argv[0])
            except UnsupportedBatchShim as exc:
                return Check("project", "abort", str(exc))
            if found is None:
                return Check("project", "abort", f"{argv[0]} was not found on PATH (check: {shown}); install it "
                                                 "or fix [preflight].checks")
            command = [*found.argv, *argv[1:]]
        try:
            result = run_command(command, timeout=timeout, cwd=str(ctx.info.root))
        except (OSError, UnsupportedBatchShim) as exc:
            return Check("project", "abort", f"could not run {shown}: {exc}")
        if result.timed_out:
            return Check("project", "abort", f"{shown} did not finish within {timeout:g} s")
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip().splitlines()[-5:]
            return Check("project", "abort", f"{shown} failed (exit {result.returncode}); fix what it reports",
                         tuple(tail))
    return Check("project", "ok", f"{len(project_checks)} project check(s) passed")


def run_checks(info: RepoInfo, *, state: pathlib.Path, task: str | None = None, touch: Sequence[str] = (),
               offline: bool = False, settings: Mapping[str, object] | None = None,
               impact: ImpactHook | None = None, project_checks: Sequence[Sequence[str]] = ()) -> Report:
    """Run the preflight checks for the repository `info` with lease state in `state`."""
    ctx = _Context(info=info, settings={**DEFAULTS, **(settings or {})}, offline=offline, not_proven=[])
    path = packet.packet_path(info.root, task) if task is not None else None
    if path is not None:
        ctx.packet_result = packet.check(path, task=task)
    entries, store_error = None, None
    try:
        entries = Store(state).snapshot()
    except (StateCorrupt, PendingTransaction, StorageError, OSError) as exc:
        store_error = str(exc)
    if task is not None and entries:
        ctx.task_lease = next((lease for lease, _ in entries if lease.task == task), None)

    touch_scope: tuple[str, ...] = ()
    if touch:
        try:
            touch_scope = normalize_scope(list(touch), info.root)
        except ScopeError:
            pass  # reported by the leases check
    checks = [_check_base(ctx), _check_dirty(ctx)]
    scope, scope_problems = _compare_scope(ctx, tuple(touch))
    checks.append(_check_leases(ctx, scope, scope_problems, entries, store_error, task))
    checks.append(_check_recent(ctx, scope, path))
    impact_check, impact_state = _check_impact(ctx, impact, touch_scope)
    checks.append(impact_check)
    checks.append(_check_packet(ctx, task, path))
    checks.append(_check_project(ctx, project_checks))
    ctx.not_proven[:0] = [
        "that the code builds, the tests pass or the success conditions are met",
        "that nobody takes an overlapping lease or pushes after this check; take your lease now with oah start",
    ]
    return Report(checks=checks, impact=impact_state, not_proven=ctx.not_proven)
