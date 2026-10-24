# 착지 — 작업 HEAD 에 리뷰 수렴·게이트 요약·비밀 검사·CI 가 묶였는지, 기본 브랜치를 새로 가져와 영향을 다시 판정해 새로 걸린 검사가 HEAD 에서 통과했는지 보고, 확인 뒤 원격이 가져온 SHA 를 요구하는 조건으로 정확한 HEAD 를 빨리감기 푸시한다.
"""`oah land --check|--merge` (PLAN 5.9 Landing, section 4 row 29, A11, A17, A23).

`assess(root, task, env=...)` runs the landing checks in one fixed order, immediately before landing.
Preconditions are a caller that is not a dispatched worker, a commit at HEAD, clean tracked files, and no untracked
file that gate evidence hashes as an input (it would not land). Every check then reports `passed`, `failed`,
`unverified` (CI only) or `skipped` (a check it needs failed). The run is ready only when every check passed or is
unverified, and the first failure is the headline.

1. lease: no lease, active or stale, is recorded for this worktree (`OAH_LAND_LEASE_OPEN`).
2. intent: the task's latest lease binding is `current`, and its packet is committed at HEAD as bound
   (`OAH_LAND_INTENT_NOT_CURRENT`).
3. review: `rounds.gate` finds the task's latest event approving exactly HEAD for that binding
   (`OAH_LAND_REVIEW_REQUIRED`).
4. gates: a ledger governs the task, `oah.gates.guard.evaluate` passes with unlazy, and its summary equals
   `GATES.summary.md` committed at HEAD. With `[gates] commit_ledgers`, every ledger is committed at HEAD as it is
   on disk. Codes: `OAH_LAND_GATES_REQUIRED`, `OAH_LAND_GATES_UNMET`, `OAH_LAND_GATES_SUMMARY_MISSING`,
   `OAH_LAND_GATES_SUMMARY_STALE`.
5. impact: the worktree map is fresh at HEAD. The remote's default branch was fetched in this run into oah's
   private mirror; no user ref, object or configuration is written. The changed paths are assessed against both
   maps, and the result digest is recorded. Offline mode, a failed fetch or unavailable impact is
   `OAH_LAND_IMPACT_STALE`.
6. required-checks: these are the newly affected checks. Each is a gate in the ledgers committed at the fetched
   SHA F that pins or execs a changed path. Its presence at HEAD is not execution evidence. It must pass against
   HEAD: one of the task's ledgers must hold a gate with the same definition (CHECK, EXPECT, CWD) whose binding is
   fresh and met. Otherwise it is `pending` (no such gate) or `failed`, and the check refuses with
   `OAH_LAND_CHECKS_REQUIRED`. CI workflow bindings are left to the CI check runs of the exact head.
7. fast-forward: HEAD contains F and differs from it. A default branch that moved past the task's base is
   `OAH_LAND_IMPACT_STALE`; one already at HEAD is `OAH_LAND_NOTHING_TO_LAND`.
8. scan: gitleaks reports nothing in `F..HEAD`. Without gitleaks the check refuses with `OAH_LAND_SCAN_REQUIRED`;
   findings or a scanner error refuse with `OAH_LAND_SCAN_FAILED`. Its output, which names authors, is never shown.
9. ci: through `gh`, every check run for HEAD completed as success, neutral or skipped (`OAH_LAND_CI_FAILED`,
   `OAH_LAND_CI_PENDING`). No `gh`, no check runs, or a failed `gh` call is `unverified`, never `passed`.

The changed paths are `F..HEAD` when HEAD contains F (exactly what lands). Otherwise they are the review range,
from the merge-base with the local default ref to HEAD.

`push(root, assessment, env=...)` revalidates local authorization under the state lock, then runs
`git push --porcelain --force-with-lease=refs/heads/<branch>:<F> <verified-destination>
<HEAD>:refs/heads/<branch>`. The remote updates the branch only while it still equals F, as one atomic step.
After a failed push, a fresh fetch checks whether the remote history contains HEAD; failed verification leaves
the outcome explicitly uncertain.

A17: oah never reads a credential. `gh` and git use their own helpers. Remote URLs, `gh` and `git push` output
and gitleaks findings are never echoed or stored. `gh` uses an explicit destination selector with its
`{owner}/{repo}` placeholders. `record` appends SHAs, digests, check names, states and codes to
`<state>/land/<task>/records.jsonl`, and never messages or paths.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import re
import urllib.parse
from typing import Mapping, Sequence

from oah import intent as oah_intent
from oah.engines.proc import resolve_command, run_command
from oah.paths import git_argv, repo_info, run_git, state_dir
from oah.review import summary as evidence

CHECKS = ("lease", "intent", "review", "gates", "impact", "required-checks", "fast-forward", "scan", "ci")
PASSED, FAILED, UNVERIFIED, SKIPPED = "passed", "failed", "unverified", "skipped"
READY = "OAH_LAND_READY"
CI_PASSING = ("success", "neutral", "skipped")
GH_TIMEOUT = 60.0
GH_PER_PAGE = 100
GH_PAGES = 10
GITLEAKS_TIMEOUT = 300.0
GITLEAKS_HINT = "https://github.com/gitleaks/gitleaks#installing"
# No prompt may wait for an answer: gh and git run unattended, with their own credential helpers.
GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_PAGER": "", "NO_COLOR": "1", "CLICOLOR": "0"}
PUSH_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "SSH_ASKPASS_REQUIRE": "never"}
RECORD_SCHEMA = 1


class LandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    state: str  # passed, failed, unverified or skipped
    code: str | None
    message: str
    detail: dict = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict:
        data = {"name": self.name, "state": self.state, "code": self.code, "message": self.message}
        if self.detail:
            data["detail"] = dict(self.detail)
        return data


def _passed(name: str, message: str, detail: dict | None = None) -> Check:
    return Check(name, PASSED, None, message, detail or {})


def _failed(name: str, code: str, message: str, detail: dict | None = None) -> Check:
    return Check(name, FAILED, code, message, detail or {})


def _skipped(name: str, why: str) -> Check:
    return Check(name, SKIPPED, None, f"not checked: {why}")


@dataclasses.dataclass
class Assessment:
    task: str
    head: str
    remote: str | None = None
    branch: str | None = None
    fetched: str | None = None  # the default branch's full SHA, fetched in this run
    base: str | None = None  # where the changed paths start
    changed: list[str] = dataclasses.field(default_factory=list)
    commits: int = 0
    impact: dict | None = None
    required: list[dict] = dataclasses.field(default_factory=list)
    checks: list[Check] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    _destination: tuple[str, str] | None = dataclasses.field(default=None, init=False, repr=False)
    _authorization: str | None = dataclasses.field(default=None, init=False, repr=False)

    @property
    def ready(self) -> bool:
        return (tuple(check.name for check in self.checks) == CHECKS
                and all(check.state in (PASSED, UNVERIFIED) for check in self.checks))

    @property
    def failure(self) -> Check | None:
        return next((check for check in self.checks if check.state == FAILED), None)

    @property
    def code(self) -> str:
        failure = self.failure
        return failure.code if failure else READY

    def check(self, name: str) -> Check:
        return next(check for check in self.checks if check.name == name)

    def to_json(self) -> dict:
        return {"task": self.task, "head": self.head, "remote": self.remote, "branch": self.branch,
                "default_branch": self.fetched, "base": self.base, "changed": list(self.changed),
                "commits": self.commits, "impact": dict(self.impact) if self.impact else None,
                "required_checks": [dict(item) for item in self.required],
                "checks": [check.to_json() for check in self.checks], "ready": self.ready, "code": self.code,
                "warnings": list(self.warnings)}


def find_gh(env: Mapping[str, str]) -> tuple[str, ...] | None:
    found = resolve_command("gh", env.get("PATH"))
    return found.argv if found is not None else None


def find_gitleaks(env: Mapping[str, str]) -> tuple[str, ...] | None:
    found = resolve_command("gitleaks", env.get("PATH"))
    return found.argv if found is not None else None


def _one_line(value: object, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _listed(paths: Sequence[str]) -> str:
    return ", ".join(paths[:10]) + (f" and {len(paths) - 10} more" if len(paths) > 10 else "")


def _untracked_inputs(root: pathlib.Path, declared: Sequence[str]) -> list[str]:
    """Untracked, non-ignored files that gate input manifests hash (generated gate outputs excepted)."""
    import fnmatch
    import glob

    from oah.gates.inputs import GENERATED
    from oah.review import ReviewError

    result = run_git(root, "ls-files", "-z", "--others", "--exclude-standard")
    if result.returncode != 0:
        raise ReviewError("OAH_REVIEW_GIT_FAILED", f"git could not list untracked files in {root} "
                          f"({result.stderr.strip() or 'no output'}); check the repository and retry")
    skip = (*GENERATED, *(glob.escape(path) for path in declared))
    return sorted(name for name in result.stdout.split("\0")
                  if name and not any(fnmatch.fnmatchcase(name, pattern) for pattern in skip))


# Checks 1-4: the exact head's own evidence ---------------------------------------------------------------


def _lease_check(state: pathlib.Path, root: pathlib.Path) -> Check:
    from oah.hook_pre_push import same_worktree
    from oah.lease import Store

    held = [(lease, stale) for lease, stale in Store(state).snapshot() if same_worktree(lease.worktree, root)]
    if not held:
        return _passed("lease", "no lease is open on this worktree")
    lease, stale = held[0]
    more = f" (and {len(held) - 1} more)" if len(held) > 1 else ""
    return _failed("lease", "OAH_LAND_LEASE_OPEN",
                   f"task {lease.task} held by {lease.agent} is still open on this worktree"
                   f"{' (stale)' if stale else ''}{more}, so a push would be refused. Finish it with oah finish "
                   f'--task {lease.task} --agent {lease.agent} --summary "<what you did>", or drop it with oah release '
                   f'--task {lease.task} --agent {lease.agent} --reason "<why>", then run oah land again')


def _intent_check(root: pathlib.Path, task: str, head: str,
                  lease: evidence.TaskLease | None) -> tuple[oah_intent.Binding | None, Check]:
    def refuse(why: str) -> tuple[None, Check]:
        return None, _failed("intent", "OAH_LAND_INTENT_NOT_CURRENT", why)

    bind = (f"bind the accepted packet with oah start --task {task} --agent <you> --packet "
            f".oah/tasks/{task}/PACKET.md, get the work reviewed and finished, then run oah land again")
    if lease is None:
        return refuse(f"task {task} has never been leased in this repository, so no intent is bound to it; {bind}")
    binding = lease.binding
    if binding is None:
        return refuse(f"the latest lease of task {task} has no intent binding (unbound); {bind}")
    state, reason = oah_intent.explain_binding(binding, root)
    if state != "current":
        return refuse(f"{binding.line()} is {state}: {reason}. Restore {binding.source} as it was bound, or raise its "
                      "Revision, accept it, rebind it with oah start --packet and get it reviewed again")
    data = evidence.committed_bytes(root, head, binding.source)
    try:
        parsed = oah_intent.parse(data.decode("utf-8")) if data is not None else None
    except (UnicodeDecodeError, oah_intent.IntentInvalid):
        parsed = None
    if parsed is None or parsed.digest != binding.digest:
        return refuse(f"{binding.source} is not committed at HEAD as bound ({binding.line()}); commit it as bound, "
                      "get the head reviewed, then run oah land --check again")
    return binding, _passed("intent", f"{binding.line()} is current and committed at HEAD")


def _review_check(root: pathlib.Path, state: pathlib.Path, task: str, head: str,
                  binding: oah_intent.Binding) -> Check:
    found = evidence.convergence(root, state, task, head, binding)
    if not found.converged:
        return _failed("review", "OAH_LAND_REVIEW_REQUIRED",
                       f"no converged review approves HEAD {head[:12]}: {found.reason}")
    record = found.receipt.record
    reviewer = record.get("reviewer") if isinstance(record.get("reviewer"), dict) else {}
    effort = "effort verified" if reviewer.get("effort_verified") is True else "effort unverified"
    return _passed("review", f"{found.receipt.name} {found.receipt.digest} approves HEAD for {binding.line()} "
                             f"({evidence.independence_label(record.get('independent'))}, {effort})",
                   {"receipt": found.receipt.name, "digest": found.receipt.digest})


def _gate_tools(root: pathlib.Path, env: Mapping[str, str], home: pathlib.Path | None):
    from oah.gates.context import find_tools
    from oah.unlazy import UnlazyError

    try:
        return find_tools(root, env, home or pathlib.Path.home()), ""
    except UnlazyError as exc:
        return None, str(exc)


def _gates_check(root: pathlib.Path, state: pathlib.Path, task: str, head: str, lease: evidence.TaskLease | None,
                 settings: Mapping, tools, tools_error: str, env: Mapping[str, str]) -> Check:
    from oah.gates import check as check_mod
    from oah.gates import guard
    from oah.gates.check import GateRefused
    from oah.unlazy import UnlazyError

    declared = lease.gates if lease else ()
    summary = evidence.summary_path(task)
    retry = "then run oah land --check again"
    try:
        ledgers = guard.resolve_ledgers(root, task, declared)
    except GateRefused as exc:
        return _failed("gates", "OAH_LAND_GATES_UNMET", f"{exc}; {retry}")
    if not ledgers:
        return _failed("gates", "OAH_LAND_GATES_REQUIRED",
                       f"no gate ledger governs task {task}, so nothing proves it done at HEAD. Write one with oah "
                       f"gates new {task}, meet it with oah gates check {task}, commit {summary}, get the head "
                       f"reviewed, {retry}")
    if tools is None:
        return _failed("gates", "OAH_LAND_GATES_UNMET", f"{tools_error}; {retry}")
    try:
        report = guard.evaluate(root, task, declared, state, tools=tools, allow_structural=False, env=env,
                                env_names=settings["input_env"], commit_ledgers=settings["commit_ledgers"])
    except (GateRefused, UnlazyError) as exc:
        return _failed("gates", "OAH_LAND_GATES_UNMET", f"the gates of task {task} do not pass at HEAD ({exc}); "
                                                        f"once they pass, {retry}")
    detail = {"outcome": report.outcome, "handoff": list(report.handoff)}
    if settings["commit_ledgers"]:
        for path in ledgers:
            key = check_mod.ledger_key(root, path)
            committed = evidence.committed_bytes(root, head, key)
            if committed is None:
                return _failed("gates", "OAH_LAND_GATES_SUMMARY_MISSING",
                               f"{key} is not committed at HEAD ([gates] commit_ledgers = true), so the gate results "
                               f"are not bound to the commit that lands; commit it, get the head reviewed, {retry}")
            if committed.replace(b"\r\n", b"\n") != path.read_bytes().replace(b"\r\n", b"\n"):
                return _failed("gates", "OAH_LAND_GATES_SUMMARY_STALE",
                               f"{key} at HEAD differs from the ledger on disk; commit the checked ledger, get the "
                               f"head reviewed, {retry}")
        bound = "the ledgers committed at HEAD match the gate records"
    else:
        committed = evidence.committed_bytes(root, head, summary)
        if committed is None:
            return _failed("gates", "OAH_LAND_GATES_SUMMARY_MISSING",
                           f"{summary} is not committed at HEAD, so the gate results are not bound to the commit that "
                           f"lands; run oah gates check {task}, commit {summary}, get the head reviewed, {retry}")
        if committed.decode("utf-8", errors="replace").replace("\r\n", "\n") != report.summary_text:
            return _failed("gates", "OAH_LAND_GATES_SUMMARY_STALE",
                           f"{summary} at HEAD does not match the current gate records; run oah gates check {task}, "
                           f"commit {summary}, get the head reviewed, {retry}")
        bound = f"{summary} at HEAD matches the gate records"
    handoff = f"; abandoned (a handoff, not met): {', '.join(report.handoff)}" if report.handoff else ""
    count = f"{len(ledgers)} ledgers pass" if len(ledgers) != 1 else "1 ledger passes"
    return _passed("gates", f"{count} through unlazy and {bound}{handoff}", detail)


# Checks 5-7: the fetched default branch -------------------------------------------------------------------


@dataclasses.dataclass
class _Reassessment:
    ok: bool
    message: str = ""
    remote: str | None = None
    branch: str | None = None
    fetched: str | None = None
    contains: bool = False
    base: str | None = None
    changed: list[str] = dataclasses.field(default_factory=list)
    result: str | None = None
    newly: list[dict] = dataclasses.field(default_factory=list)
    commits: int = 0
    warnings: list[str] = dataclasses.field(default_factory=list)
    destination: tuple[str, str] | None = dataclasses.field(default=None, repr=False)


_STALE_FIXES = {"offline": "drop --offline when the network is back",
                "no-refresh": "let oah fetch the default branch",
                "refresh_default_branch=false": "set [impact] refresh_default_branch = true in .oah/config.toml",
                "no remote": "landing pushes to a remote; add one with git remote add origin <url>",
                "cache cleanup pending": "wait for the earlier impact query to stop, check the impact cache "
                                         "permissions",
                "default branch lookup failed": "restore the remote's symbolic HEAD (git ls-remote --symref "
                                                "<remote> HEAD shows it)"}


def _stale_message(branch) -> str:
    fix = _STALE_FIXES.get(branch.reason, f"run git fetch {branch.remote or 'origin'} yourself to see why")
    if branch.reason.startswith("map failed"):
        fix = "run oah impact refresh"
    return (f"the default branch was not fetched in this run ({branch.label()}), and landing reassesses impact "
            f"against the remote's current SHA, so it refuses. To fix it, {fix}, then run oah land --check again")


def _contains(root: pathlib.Path, commit: str, head: str) -> bool:
    """Whether HEAD contains commit; a commit absent from this repository is not in HEAD's history."""
    return run_git(root, "merge-base", "--is-ancestor", commit, head).returncode == 0


def _count(root: pathlib.Path, base: str, head: str) -> int:
    result = run_git(root, "rev-list", "--count", f"{base}..{head}")
    return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip().isdigit() else 0


def _task_base(root: pathlib.Path, head: str) -> str:
    """The review range's base: the merge-base of HEAD with the local default ref (`oah review`'s default)."""
    from oah import preflight
    from oah.config import load_config
    from oah.review import ReviewError, context

    chosen = load_config(root).section("preflight", preflight.DEFAULTS)
    try:
        return context.resolve(root, None, remote=str(chosen["remote"] or ""),
                               branch=str(chosen["default_branch"] or "")).base
    except ReviewError as exc:
        if exc.code == "OAH_REVIEW_NOTHING_TO_REVIEW":
            return head
        raise


def _ledger_gates(source: pathlib.Path, rev: str, settings: Mapping) -> list[tuple]:
    """(GateRef, definition digest) of every runnable gate in the ledgers committed at rev in source."""
    from oah.gates import parse_text
    from oah.impact import gatebind, mapstore
    from oah.impact.errors import ImpactError

    files, excluded = mapstore._tree_files(source, rev, settings)
    chosen = lambda path: any(gatebind._matches(path, pattern) for pattern in settings["ledger_globs"])  # noqa: E731
    if any(chosen(path) for path in excluded):
        raise ImpactError("OAH_GATES_INVALID", f"commit {rev[:12]} holds a linked gate ledger; replace it with a "
                          "regular ledger and land again")
    paths = sorted(path for path in files if chosen(path))
    contents = mapstore.read_blobs(source, rev, paths, settings["git_timeout"])
    found = []
    for path in paths:
        if path not in contents:
            raise ImpactError("OAH_GATES_INVALID", f"{path} could not be read at {rev[:12]}; check that commit's "
                              "ledgers and land again")
        ledger = parse_text(contents[path], pathlib.Path(path))
        if ledger.errors:
            raise ImpactError("OAH_GATES_INVALID", f"{path} at {rev[:12]} cannot be parsed ({len(ledger.errors)} "
                              "problems); fix it with oah gates lint where it was committed, then land again")
        found += [(gatebind.GateRef(path, gate.id, gate.check, gate.expect), gate.definition)
                  for gate in ledger.gates if gate.runnable]
    return found


def _newly_affected(their_rows: Sequence, here: Sequence[tuple], there: Sequence[tuple]) -> list[dict]:
    """Affected fetched definitions require evidence even when HEAD already contains the definition."""
    definitions = {gate.label: definition for gate, definition in there}
    found: dict[str, dict] = {}
    for row in their_rows:
        if row.layer != "gates" or row.binding not in ("pin", "exec"):
            continue
        definition = definitions.get(row.source)
        item = found.setdefault(row.source, {"gate": row.source, "binding": row.binding, "targets": [],
                                             "definition": definition})
        if row.binding == "pin":
            item["binding"] = "pin"
        if row.target not in item["targets"]:
            item["targets"].append(row.target)
    for item in found.values():
        item["targets"].sort()
    return [found[key] for key in sorted(found)]


def _destination(root: pathlib.Path, remote: str, env: Mapping[str, str]) -> tuple[str, str]:
    """One literal destination shared by the isolated fetch and push; never export its URL.

    URL rewrites are deliberately unsupported. In particular the mirror does not load local
    rewrites, whereas a push normally does. Refusing them avoids two different identities.
    """
    from oah.impact import mapstore

    def refuse():
        raise LandError("OAH_LAND_DESTINATION_UNSUPPORTED",
                        "landing needs one identical fetch and push destination without URL rewrites; configure "
                        "one literal remote URL and at most one identical push URL, then run oah land again")

    def config(args, config_env):
        result = run_command([*git_argv(), "-C", str(root), "config", "--null", *args],
                             timeout=GH_TIMEOUT, env=dict(config_env))
        if result.timed_out or result.returncode not in (0, 1):
            refuse()
        return result.stdout.split("\0")[:-1] if result.returncode == 0 else []

    isolated = mapstore._isolated_git_env()
    destinations = []
    for config_env in (isolated, env):
        if config(["--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"], config_env):
            refuse()
        urls = config(["--get-all", f"remote.{remote}.url"], config_env)
        pushes = config(["--get-all", f"remote.{remote}.pushurl"], config_env)
        if len(urls) != 1 or not urls[0] or (pushes and pushes != urls):
            refuse()
        stored = urls[0]
        url = stored
        if not pathlib.Path(url).is_absolute() and not re.match(r"[^/\\]+:", url):
            url = str((root / pathlib.Path(url).expanduser()).resolve())
        destinations.append((stored, url))
    if destinations[0] != destinations[1]:
        refuse()
    return destinations[0]


def _gh_repository(destination: str) -> tuple[str, str]:
    """gh's explicit [HOST/]OWNER/REPO and API host; neither is returned to the caller."""
    # An absolute local path such as /a/b must not become GitHub repository a/b.
    # This invalid single-component selector makes real gh refuse before a request; fake gh
    # can use its digest to model CI for the local bare repositories in the offline suite.
    unsupported = "oah-local-destination:" + hashlib.sha256(destination.encode("utf-8")).hexdigest()
    parsed = urllib.parse.urlsplit(destination)
    if "://" in destination:
        ports = {"https": 443, "ssh": 22, "git": 9418}
        if parsed.scheme not in ports or not parsed.hostname:
            return unsupported, "github.com"
        if parsed.port not in (None, ports[parsed.scheme]) or parsed.query or parsed.fragment:
            raise ValueError("unsupported CI destination")
        host, path = parsed.hostname, parsed.path.strip("/")
    else:
        scp = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):([^:]+)", destination)
        if not scp or pathlib.Path(destination).is_absolute() or len(scp[1]) <= 1:
            return unsupported, "github.com"
        host, path = scp[1], scp[2].strip("/")
    path = path.removesuffix(".git")
    if len(path.split("/")) != 2 or any(not part for part in path.split("/")):
        return unsupported, "github.com"
    return f"{host}/{path}", host


def _reassess(root: pathlib.Path, head: str, env: Mapping[str, str], offline: bool) -> _Reassessment:
    from oah.impact import defaultbranch, mapstore, query
    from oah.impact.defaults import configured
    from oah.impact.errors import ImpactError
    from oah.impact_cli import _rows, _with_live_hits
    from oah.review import ReviewError, context

    retry = "then run oah land --check again"
    try:
        settings = configured(root)
        info, data = mapstore.fresh_map(root, refresh=True, env=env)
    except ImpactError as exc:
        return _Reassessment(False, f"the impact map of this worktree is unavailable ({exc}); run oah impact refresh, "
                                    f"{retry}")
    if info.commit != head:
        return _Reassessment(False, f"HEAD moved to {info.commit[:12]} while the impact map was refreshed; {retry}")
    warnings = list(info.warnings())
    try:
        remote = defaultbranch.remote_for(root, float(settings["git_timeout"]))
        destination = _destination(root, remote, env) if remote else None
        with defaultbranch._query_context(root, settings, offline=offline, env=env) as branch:
            if branch.state != "fresh":
                return _Reassessment(False, _stale_message(branch), remote=branch.remote, branch=branch.branch,
                                     warnings=warnings)
            fetched = branch.commit
            contains = _contains(root, fetched, head)
            base = fetched if contains else _task_base(root, head)
            changed = context.changed_files(root, base, head) if base != head else []
            here = _ledger_gates(root, head, settings)
            source = defaultbranch._snapshot_source(root, fetched, env)
            if (branch.remote != remote or destination is None
                    or _destination(root, remote, env) != destination
                    or defaultbranch._source(source).get("url") != mapstore.digest(list(destination))):
                raise LandError("OAH_LAND_DESTINATION_CHANGED",
                                "the destination changed during assessment; run oah land again")
            there = _ledger_gates(source, fetched, settings)
            mine = _with_live_hits(root, dict(data), head, changed, settings)
            mine_rows = _rows(mine, changed, gates=[gate for gate, _ in here], graph=query.load_graph(root, settings),
                              settings=settings)
            theirs = _with_live_hits(source, branch.data, fetched, changed, settings)
            their_rows = _rows(theirs, changed, gates=[gate for gate, _ in there], settings=settings)
            rows, found = query.merge_maps(mine_rows, their_rows)
            digest = mapstore.digest([query.row_json(row, found) for row in rows])
            if branch.info:
                warnings += branch.info.warnings()
            return _Reassessment(True, remote=branch.remote, branch=branch.branch, fetched=fetched, contains=contains,
                                 base=base, changed=list(changed), result=digest,
                                 newly=_newly_affected(their_rows, here, there),
                                 commits=_count(root, fetched, head) if contains else 0, warnings=warnings,
                                 destination=destination)
    except (ImpactError, ReviewError, LandError) as exc:
        return _Reassessment(False, f"impact could not be reassessed against the fetched default branch ({exc}); fix "
                                    f"that, {retry}", warnings=warnings)


def _evidence_by_definition(root: pathlib.Path, state: pathlib.Path, task: str, lease: evidence.TaskLease | None,
                            settings: Mapping, tools, tools_error: str,
                            env: Mapping[str, str]) -> tuple[dict[str, list[bool]], str]:
    """(definition digest -> whether each gate of the task's ledgers with it is fresh and met, why none was read)."""
    from oah.gates import check as check_mod
    from oah.gates import guard, parse_ledger
    from oah.gates.check import GateRefused
    from oah.unlazy import UnlazyError

    if tools is None or not tools.available:
        return {}, tools_error or (tools.reason if tools is not None else "unlazy is not available")
    declared = lease.gates if lease else ()
    try:
        found = guard.assess(root, task, declared, state, tools=tools, env=env, env_names=settings["input_env"])
    except (GateRefused, UnlazyError) as exc:
        return {}, str(exc)
    seen: dict[str, list[bool]] = {}
    for path in found.ledgers:
        key = check_mod.ledger_key(root, path)
        for gate in parse_ledger(path).gates:
            if gate.definition:
                seen.setdefault(gate.definition, []).append(found.fresh.get(key, {}).get(gate.id) == "fresh")
    return seen, ""


def _required_check(result: Assessment, reassessed: _Reassessment, seen: dict[str, list[bool]],
                    unread: str) -> Check:
    if not reassessed.newly:
        return _passed("required-checks", "the fetched default branch binds no gate to the changed paths")
    for item in reassessed.newly:
        matches = seen.get(item["definition"])
        state = PASSED if matches and any(matches) else "failed" if matches is not None else "pending"
        result.required.append({**item, "state": state})
    open_items = [item for item in result.required if item["state"] != PASSED]
    if not open_items:
        return _passed("required-checks", f"{len(result.required)} newly affected check(s) pass against HEAD through "
                                          f"gates of task {result.task} with the same definition")
    listed = "; ".join(f"{item['gate']} ({item['binding']} on {', '.join(item['targets'])}): {item['state']}"
                       for item in open_items)
    unverified = f" (no gate evidence of the task could be read: {unread})" if unread else ""
    return _failed("required-checks", "OAH_LAND_CHECKS_REQUIRED",
                   f"the fetched default branch binds checks to files this task changed, and they have no passing "
                   f"evidence against HEAD{unverified}: {listed}. Add a gate with the same CHECK, EXPECT and CWD to "
                   f"the task's ledger and run oah gates "
                   f"check {result.task}; then commit, get the head reviewed and run oah land --check again")


def _fast_forward_check(head: str, reassessed: _Reassessment) -> Check:
    where = f"{reassessed.remote}/{reassessed.branch}"
    fetched = reassessed.fetched
    if fetched == head:
        return _failed("fast-forward", "OAH_LAND_NOTHING_TO_LAND", f"{where} is already at HEAD {head[:12]}; there is "
                                                                   "nothing to land")
    if not reassessed.contains:
        return _failed("fast-forward", "OAH_LAND_IMPACT_STALE",
                       f"{where} is at {fetched[:12]}, which HEAD does not contain: the default branch moved since "
                       f"this task was based on it, so HEAD cannot fast-forward it and nothing can land. Rebase onto "
                       f"it (git fetch {reassessed.remote}, then git rebase {where}), get the new head reviewed, then "
                       "run oah land --check again; it fetches the new SHA, reassesses impact and requires the "
                       "affected checks again")
    return _passed("fast-forward", f"{where} moves from {fetched[:12]} to HEAD {head[:12]}, a fast-forward of "
                                   f"{reassessed.commits} commit{'s' if reassessed.commits != 1 else ''}")


# Checks 8-9: secret scan and CI ---------------------------------------------------------------------------


def _scan_check(root: pathlib.Path, head: str, reassessed: _Reassessment, env: Mapping[str, str]) -> Check:
    from oah.engines.proc import UnsupportedBatchShim

    span = f"{reassessed.fetched}..{head}"
    shown = f"{reassessed.fetched[:12]}..{head[:12]}"
    retry = "then run oah land --check again"
    try:
        gitleaks = find_gitleaks(env)
    except UnsupportedBatchShim as exc:
        return _failed("scan", "OAH_LAND_SCAN_REQUIRED", f"{exc}; {retry}")
    if gitleaks is None:
        return _failed("scan", "OAH_LAND_SCAN_REQUIRED",
                       f"gitleaks is not installed, so the commits that would land ({shown}) were not scanned for "
                       f"secrets; install it from {GITLEAKS_HINT} (oah doctor prints the command), {retry}")
    try:
        result = run_command([*gitleaks, "detect", "--source", str(root), "--log-opts", span, "--redact",
                              "--no-banner", "--exit-code", "1"], timeout=GITLEAKS_TIMEOUT, env=dict(env))
    except OSError as exc:
        return _failed("scan", "OAH_LAND_SCAN_FAILED", f"gitleaks could not start ({exc.strerror or exc}); {retry}")
    look = f"run gitleaks detect --source . --log-opts {span} --redact yourself"
    if result.returncode == 0 and not result.timed_out:
        return _passed("scan", f"gitleaks found nothing in the commits that would land ({shown})", {"range": span})
    if result.returncode == 1:
        return _failed("scan", "OAH_LAND_SCAN_FAILED",
                       f"gitleaks found possible secrets in the commits that would land ({shown}). Its findings name "
                       f"authors, so they are not shown here: {look} to see them, remove them from those commits, "
                       f"rotate any real key, {retry}")
    how = "timed out" if result.timed_out else f"exited {result.returncode}"
    return _failed("scan", "OAH_LAND_SCAN_FAILED", f"gitleaks {how} while scanning {shown}; {look} to see why, fix it, "
                                                   f"{retry}")


def _check_runs(gh: Sequence[str], root: pathlib.Path, head: str,
                env: Mapping[str, str], destination: str) -> tuple[list[dict], str | None]:
    """The check runs GitHub reports for exactly head, or a reason they could not be read (never gh's output)."""
    runs: list[dict] = []
    seen = 0
    try:
        repository, host = _gh_repository(destination)
    except ValueError:
        return [], "destination could not be verified"
    for page in range(1, GH_PAGES + 1):
        endpoint = f"repos/{{owner}}/{{repo}}/commits/{head}/check-runs?per_page={GH_PER_PAGE}&page={page}"
        try:
            result = run_command([*gh, "api", "--hostname", host, "--method", "GET", "-H",
                                  "Accept: application/vnd.github+json", endpoint], timeout=GH_TIMEOUT, cwd=str(root),
                                 env={**env, **GH_ENV, "GH_REPO": repository, "GH_HOST": host})
        except OSError:
            return [], "gh could not start"
        if result.timed_out:
            return [], f"gh did not answer within {GH_TIMEOUT:g}s"
        if result.returncode != 0:
            return [], f"gh exited {result.returncode}"
        try:
            data = json.loads(result.stdout)
            total, items = int(data["total_count"]), data["check_runs"]
            if not isinstance(items, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return [], "gh did not return check-run JSON"
        seen += len(items)
        for item in items:
            if isinstance(item, dict) and item.get("head_sha") == head:
                conclusion = item.get("conclusion")
                runs.append({"name": _one_line(item.get("name")), "status": str(item.get("status")),
                             "conclusion": None if conclusion is None else str(conclusion)})
        if seen >= total:
            return runs, None
        if not items:
            return [], "check-run pagination was incomplete"
    return [], "check-run pagination exceeded the supported limit; inspect all runs before landing"


def _ci_check(root: pathlib.Path, head: str, env: Mapping[str, str], *, destination: str | None = None) -> Check:
    from oah.engines.proc import UnsupportedBatchShim

    retry = "then run oah land --check again"
    try:
        gh = find_gh(env)
    except UnsupportedBatchShim as exc:
        return Check("ci", UNVERIFIED, None, f"{exc}; CI was not checked (unverified, not passed)")
    if gh is None:
        return Check("ci", UNVERIFIED, None, "gh is not installed, so CI was not checked (unverified, not passed); "
                                             "install it (oah doctor prints the command), sign in with gh auth login, "
                                             f"push the task branch, {retry}")
    if destination is None:
        return Check("ci", UNVERIFIED, None, f"the destination was not assessed, so CI is unverified; {retry}")
    runs, problem = _check_runs(gh, root, head, env, destination)
    if problem is not None and problem.startswith("check-run pagination"):
        return _failed("ci", "OAH_LAND_CI_PENDING", "CI pagination is incomplete, so passing CI cannot be "
                       f"established; inspect all check runs, {retry}")
    if problem is not None:
        return Check("ci", UNVERIFIED, None, f"gh could not read the check runs for HEAD ({problem}), so CI is "
                                             f"unverified, not passed; check gh auth status and that the task branch "
                                             f"is pushed, {retry}")
    if not runs:
        return Check("ci", UNVERIFIED, None, f"no CI check runs are reported for HEAD {head[:12]}, so CI is "
                                             f"unverified, not passed; push the task branch and let CI run, {retry}")
    failed = [run for run in runs if run["status"] == "completed" and run["conclusion"] not in CI_PASSING]
    if failed:
        listed = ", ".join(f"{run['name']} ({run['conclusion']})" for run in failed)
        return _failed("ci", "OAH_LAND_CI_FAILED", f"CI check runs failed for HEAD {head[:12]}: {listed}; fix them, "
                                                   f"push, wait for CI, {retry}")
    pending = [run for run in runs if run["status"] != "completed"]
    if pending:
        listed = ", ".join(f"{run['name']} ({run['status']})" for run in pending)
        return _failed("ci", "OAH_LAND_CI_PENDING", f"CI is still running for HEAD {head[:12]}: {listed}; wait for it "
                                                    f"to finish, {retry}")
    return _passed("ci", f"{len(runs)} CI check run{'s' if len(runs) != 1 else ''} passed for HEAD {head[:12]}",
                   {"runs": len(runs)})


# The assessment -------------------------------------------------------------------------------------------


def assess(root: pathlib.Path, task: str, *, env: Mapping[str, str], offline: bool = False,
           home: pathlib.Path | None = None) -> Assessment:
    """Every landing check for the exact HEAD of root's worktree; preconditions raise LandError."""
    from oah.config import load_config
    from oah.gates.context import DEFAULTS as GATE_DEFAULTS
    from oah.review import context

    if env.get("OAH_WORKER") == "1":
        raise LandError("OAH_LAND_WORKER_REFUSED", "dispatched workers never land; return your work to the dispatcher, "
                                                   "who runs oah land")
    evidence.check_task(task)
    info = repo_info(pathlib.Path(root))
    root, state, head = info.root, state_dir(info, env), info.head
    if head is None:
        raise LandError("OAH_LAND_NOTHING_TO_LAND", "this repository has no commits yet; commit the work, get it "
                                                    "reviewed, then run oah land")
    dirty = context.tracked_dirty(root)
    if dirty:
        raise LandError("OAH_LAND_DIRTY_TREE", f"tracked files have uncommitted changes ({_listed(dirty)}); landing "
                                               "checks and pushes a commit, so commit or stash them, then run oah land "
                                               "again")
    authorization = _authorization(state, task)
    lease = evidence.task_lease(state, task)
    untracked = _untracked_inputs(root, lease.gates if lease else ())
    if untracked:
        raise LandError("OAH_LAND_DIRTY_TREE", f"untracked files are present ({_listed(untracked)}); gate evidence "
                                               "covers them as inputs, but they would not land, so commit, ignore or "
                                               f"remove them, run oah gates check {task} if a gate read them, then run "
                                               "oah land again")
    result = Assessment(task=task, head=head)
    result._authorization = authorization
    result.checks.append(_lease_check(state, root))
    binding, intent_check = _intent_check(root, task, head, lease)
    result.checks.append(intent_check)
    result.checks.append(_review_check(root, state, task, head, binding) if binding is not None
                         else _skipped("review", "it needs a current intent binding"))
    gate_settings = load_config(root).section("gates", GATE_DEFAULTS)
    tools, tools_error = _gate_tools(root, env, home)
    result.checks.append(_gates_check(root, state, task, head, lease, gate_settings, tools, tools_error, env))
    reassessed = _reassess(root, head, env, offline)
    result.remote, result.branch, result.warnings = reassessed.remote, reassessed.branch, reassessed.warnings
    result._destination = reassessed.destination
    if reassessed.ok:
        result.fetched, result.base, result.changed = reassessed.fetched, reassessed.base, reassessed.changed
        result.commits = reassessed.commits
        result.impact = {"worktree": head, "default_branch": reassessed.fetched, "result": reassessed.result}
        result.checks.append(_passed("impact", f"default branch {reassessed.remote}/{reassessed.branch} fetched at "
                                               f"{reassessed.fetched[:12]}; {len(reassessed.changed)} changed path(s) "
                                               f"reassessed against it and HEAD {head[:12]}; result "
                                               f"{reassessed.result[:12]}", dict(result.impact)))
        seen, unread = (_evidence_by_definition(root, state, task, lease, gate_settings, tools, tools_error, env)
                        if reassessed.newly else ({}, ""))
        result.checks.append(_required_check(result, reassessed, seen, unread))
        forward = _fast_forward_check(head, reassessed)
        result.checks.append(forward)
        result.checks.append(_scan_check(root, head, reassessed, env) if forward.state == PASSED
                             else _skipped("scan", "the commits that would land are known only when HEAD fast-forwards "
                                                   "the fetched default branch"))
    else:
        result.checks.append(_failed("impact", "OAH_LAND_IMPACT_STALE", reassessed.message))
        why = "impact was not reassessed against a freshly fetched default branch"
        result.checks += [_skipped("required-checks", why), _skipped("fast-forward", why), _skipped("scan", why)]
    result.checks.append(_ci_check(root, head, env, destination=(result._destination[1]
                                                              if result._destination else None)))
    return result


# Landing --------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Pushed:
    landed: bool | None  # None means the transport failed and acceptance cannot be determined
    code: str
    message: str
    remote_sha: str | None = None

    def to_json(self) -> dict:
        return {"landed": self.landed, "code": self.code, "message": self.message, "remote_sha": self.remote_sha}


def _push_reason(stdout: str, stderr: str, ref: str, timed_out: bool, code: int | None) -> str:
    """Only fixed diagnostics: neither porcelain reasons nor hook stderr are trusted."""
    return "timed out" if timed_out else f"git exited {code}"


def _state_path(state: pathlib.Path, *parts: str) -> pathlib.Path:
    from oah.runs import RunError, _contained_path

    try:
        return _contained_path(state, *parts)
    except RunError:
        raise LandError("OAH_RUN_PATH_UNSAFE", "landing authorization state contains a linked or unsafe path; "
                        "remove linked state paths and run oah land again") from None


def _authorization(state: pathlib.Path, task: str) -> str:
    """Digest the cooperating authorization writers' state, including starts without receipts."""
    from oah.impact.mapstore import digest
    from oah.review import receipts

    paths = (("leases.json",), ("events.jsonl",), ("reviews", task, receipts.INDEX_NAME),
             ("reviews", task, receipts.ROUNDS_NAME), ("gates", task, "bindings.json"))
    values = []
    for parts in paths:
        path = _state_path(state, *parts)
        try:
            values.append(hashlib.sha256(path.read_bytes()).hexdigest())
        except FileNotFoundError:
            values.append(None)
    return digest(values)


def _revalidate_local(root: pathlib.Path, assessment: Assessment, env: Mapping[str, str]) -> Check | None:
    from oah.config import load_config
    from oah.gates.context import DEFAULTS as GATE_DEFAULTS
    from oah.review import context

    info = repo_info(root)
    state = state_dir(info, env)
    task, head = assessment.task, assessment.head
    lease = evidence.task_lease(state, task)
    if (info.head != head or context.tracked_dirty(root)
            or _untracked_inputs(root, lease.gates if lease else ())):
        return _failed("review", "OAH_LAND_AUTHORIZATION_CHANGED",
                       "the reviewed tree changed while awaiting confirmation; run oah land --merge again")
    checked = _lease_check(state, root)
    if checked.state != PASSED:
        return checked
    binding, checked = _intent_check(root, task, head, lease)
    if checked.state != PASSED:
        return checked
    checked = _review_check(root, state, task, head, binding)
    if checked.state != PASSED:
        return checked
    settings = load_config(root).section("gates", GATE_DEFAULTS)
    tools, error = _gate_tools(root, env, None)
    checked = _gates_check(root, state, task, head, lease, settings, tools, error, env)
    if checked.state != PASSED:
        return checked
    if assessment.required:
        seen, unread = _evidence_by_definition(root, state, task, lease, settings, tools, error, env)
        current = Assessment(task, head)
        checked = _required_check(current, _Reassessment(True, remote=assessment.remote, branch=assessment.branch,
                                                        newly=assessment.required), seen, unread)
        if checked.state != PASSED:
            return checked
    if _authorization(state, task) != assessment._authorization:
        return _failed("review", "OAH_LAND_AUTHORIZATION_CHANGED",
                       "local authorization changed while awaiting confirmation; read fresh checks and confirm "
                       "again with oah land --merge")
    return None


def push(root: pathlib.Path, assessment: Assessment, *, env: Mapping[str, str]) -> Pushed:
    """Revalidate after confirmation and serialize local authorization through the conditional push."""
    if not (assessment.ready and assessment.remote and assessment.branch and assessment.fetched):
        raise LandError("OAH_LAND_NOT_READY", "a push needs an assessment where every check passed; run oah land "
                                              "--merge")
    from oah.lockfile import StateLock

    state = state_dir(repo_info(root), env)
    with StateLock(_state_path(state, ".lock")):
        refusal = _revalidate_local(root, assessment, env)
        if refusal is not None:
            return Pushed(False, refusal.code, refusal.message)
        if assessment._destination is None or _destination(root, assessment.remote, env) != assessment._destination:
            return Pushed(False, "OAH_LAND_DESTINATION_CHANGED",
                          "the destination changed after assessment; run oah land --merge and confirm again")
        return _push_verified(root, assessment, env=env)


def _push_verified(root: pathlib.Path, assessment: Assessment, *, env: Mapping[str, str]) -> Pushed:
    from oah.impact import defaultbranch, mapstore
    from oah.impact.defaults import configured

    settings = configured(root)
    ref = f"refs/heads/{assessment.branch}"
    where = f"{assessment.remote}/{assessment.branch}"
    # Literal destinations can contain credentials. Disable URL/curl tracing, including
    # configured trace2 targets, before handing the destination to git or its children.
    push_env = {key: value for key, value in env.items()
                if not key.upper().startswith("GIT_TRACE") and key.upper() != "GIT_CURL_VERBOSE"}
    push_env.update(PUSH_ENV, GIT_TRACE="0", GIT_TRACE2="0", GIT_TRACE2_EVENT="0", GIT_TRACE2_PERF="0")
    result = run_command([*git_argv(), "-C", str(root), "push", "--porcelain",
                          f"--force-with-lease={ref}:{assessment.fetched}", "--", assessment._destination[1],
                          f"{assessment.head}:{ref}"], timeout=float(settings["git_timeout"]), env=push_env)
    if result.returncode == 0 and not result.timed_out:
        return Pushed(True, "OAH_LAND_LANDED", f"{where} now points at {assessment.head} (a fast-forward from "
                                               f"{assessment.fetched[:12]})", assessment.head)
    from oah.atomic import StateCorrupt
    from oah.impact.errors import ImpactError
    from oah.lockfile import LockTimeout

    try:
        # Keep this fetched generation leased until its history and identity have been checked.
        with defaultbranch._query_context(root, settings, env=env) as after:
            now = None
            contains = None
            if (after.state == "fresh" and after.remote == assessment.remote and after.branch == assessment.branch
                    and _destination(root, assessment.remote, env) == assessment._destination):
                source = defaultbranch._snapshot_source(root, after.commit, env)
                if defaultbranch._source(source).get("url") == mapstore.digest(list(assessment._destination)):
                    now = after.commit
                    history = run_command([*git_argv(), "-C", str(source), "rev-list", now],
                                          timeout=float(settings["git_timeout"]),
                                          env=mapstore._isolated_git_env())
                    if history.returncode == 0 and not history.timed_out:
                        contains = assessment.head in history.stdout.splitlines()
                        if not contains:
                            shallow = run_git(source, "rev-parse", "--is-shallow-repository")
                            if shallow.returncode != 0 or shallow.stdout.strip() != "false":
                                contains = None
            if contains is True:
                return Pushed(True, "OAH_LAND_LANDED", f"{where} contains {assessment.head}, although git reported a "
                              "problem after the update; inspect the remote history and hooks", now)
            if contains is False and now != assessment.fetched:
                return Pushed(False, "OAH_LAND_IMPACT_STALE",
                              f"{where} moved to {now[:12]} while landing, and its history does not contain HEAD; "
                              "nothing landed. Run oah land --check again: it fetches the new SHA, reassesses "
                              "impact and requires the affected checks again", now)
            if contains is False:
                reason = _push_reason(result.stdout, result.stderr, ref, result.timed_out, result.returncode)
                return Pushed(False, "OAH_LAND_PUSH_FAILED",
                              f"git push did not update {where} ({reason}); nothing landed. Inspect git's hooks "
                              "and remote permissions yourself, fix the refusal, then run oah land --merge again", now)
    except (ImpactError, LandError, LockTimeout, StateCorrupt, OSError):
        pass  # A failed verification must never turn a possible acceptance into 'nothing landed'.
    return Pushed(None, "OAH_LAND_OUTCOME_UNCERTAIN",
                  "git push reported a problem and acceptance could not be established; the outcome is uncertain. "
                  "Inspect the destination's history for the assessed HEAD before retrying oah land --check")


def record(state: pathlib.Path, assessment: Assessment, mode: str, pushed: Pushed | None,
           *, now: dt.datetime | None = None) -> None:
    """Append one landing record: SHAs, digests, check names, states and codes; never messages or paths."""
    from oah.atomic import append_jsonl
    from oah.lockfile import StateLock
    from oah.runs import RunError, _contained_path

    moment = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    line = {"schema": RECORD_SCHEMA, "at": moment.isoformat().replace("+00:00", "Z"), "mode": mode,
            "task": assessment.task, "head": assessment.head, "remote": assessment.remote, "branch": assessment.branch,
            "default_branch": assessment.fetched, "base": assessment.base,
            "impact_result": assessment.impact["result"] if assessment.impact else None,
            "required_checks": [dict(item) for item in assessment.required],
            "checks": [{"name": check.name, "state": check.state, "code": check.code} for check in assessment.checks],
            "ready": assessment.ready, "code": pushed.code if pushed else assessment.code,
            "landed": pushed.landed if pushed else False, "remote_sha": pushed.remote_sha if pushed else None}
    try:
        path = _contained_path(state, "land", assessment.task, "records.jsonl")
        with StateLock(_contained_path(state, ".lock")):
            append_jsonl(path, line)
    except RunError as exc:
        raise LandError(exc.code, str(exc).split(": ", 1)[-1]) from None
