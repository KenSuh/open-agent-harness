# 리뷰 맥락 — base·head 를 전체 SHA 로 정하고(기본은 기본 브랜치와의 merge-base), 추적 파일이 깨끗한지, diff stat·바뀐 파일·중요 경로·DB 영향 줄을 고정된 head 에서 만든다.
"""What one round reviews and what its prompt states (PLAN 5.9 steps 1 and 5, A3, A23).

`resolve(root, base_arg)` returns full SHAs: the head is HEAD, and the base is `--base` (which must be an
ancestor of HEAD) or the merge-base of HEAD with the default branch. The default branch is found locally,
without a fetch: `[preflight] default_branch` (a plain branch name, on the configured or `origin` remote, then
locally), else the remote's HEAD branch, `main` or `master` on that remote, then local `main` or `master`.

Diffs and source evidence read the fixed reviewed commits (A23). Impact also queries the refreshed
default-branch map through the landed integration. DB-touch detection reads definitions and references
on both sides of base..head, so deletion cannot erase recurrence evidence. The DB identity line names
the queried graphs and result digest; the reviewer must copy it exactly (A3).
"""
from __future__ import annotations

import dataclasses
import pathlib
import subprocess
from typing import Iterable, Mapping, Sequence

from oah.engines.proc import run_command
from oah.impact.kinds import glob_match
from oah.paths import GIT_TIMEOUT, GitMissing, git_argv, run_git
from oah.review import ReviewError
from oah.review.receipts import SHA_RE

NOT_TOUCHED = "db-impact: not touched (no DB entity changes in base..head)"
STAT_LINES = 300
ROW_LINES = 80
PRESENT = "PRESENT"
ABSENT = "ABSENT"


@dataclasses.dataclass(frozen=True)
class Range:
    base: str
    head: str
    base_label: str


@dataclasses.dataclass(frozen=True)
class DbImpact:
    line: str
    query: str | None
    entities: tuple[str, ...]
    rows: tuple[str, ...]


def _git(root: pathlib.Path, *args: str, ok: tuple[int, ...] = (0,),
         quiet_failure: bool = False) -> tuple[int | None, str]:
    try:
        result = run_git(root, *args)
    except OSError:
        raise ReviewError("OAH_REVIEW_GIT_FAILED", "Git inspection failed; restore the repository and retry") from None
    if (result.timed_out or result.returncode not in ok
            or quiet_failure and result.returncode != 0 and (result.stdout or result.stderr)):
        raise ReviewError("OAH_REVIEW_GIT_FAILED", "Git inspection failed; restore the repository and retry")
    return result.returncode, result.stdout


def _budget_git(root: pathlib.Path, *args: str) -> tuple[str, str]:
    """A budget read is PRESENT, proven ABSENT, or a refusal; Git output is never diagnostic.

    Git documents quiet rev-parse status 1 for an unresolved ref. Quiet symbolic-ref
    status 1 proves only non-symbolic: verify the ref before declaring it absent, and
    retain a direct commit ref as PRESENT. A raw full-ID cat-file existence probe reports a
    missing object with quiet status 1 (probe before peeling, per C7). A successful
    ls-tree with no entry proves a path absent in the inspected tree. No other failure
    is absence, including status 128 with no output.
    """
    try:
        if args[:2] == ("cat-file", "blob"):
            result = run_command([*git_argv(), "-C", str(root), *args], timeout=GIT_TIMEOUT,
                                 encoding="latin-1")  # preserve config bytes
        else:
            result = run_git(root, *args)
    except (OSError, subprocess.SubprocessError, GitMissing):
        raise ReviewError("OAH_REVIEW_GIT_FAILED", "Git inspection failed; restore the repository and retry") from None
    quiet = not result.stdout and not result.stderr
    optional = (len(args) == 5 and args[:4] == ("rev-parse", "--verify", "--quiet", "--end-of-options")
                or len(args) == 3 and args[:2] == ("symbolic-ref", "--quiet")
                or len(args) == 3 and args[:2] == ("cat-file", "-e") and SHA_RE.fullmatch(args[2]))
    if not result.timed_out and result.returncode == 1 and quiet and optional:
        if args[0] == "symbolic-ref":
            status, _out = _budget_git(root, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                      f"{args[2]}^{{commit}}")
            return (PRESENT, args[2]) if status == PRESENT else (ABSENT, "")
        return ABSENT, ""
    if (result.timed_out or result.returncode != 0 or result.stderr
            or args[0] in ("rev-parse", "symbolic-ref", "merge-base") and not result.stdout.strip()
            or args[:2] == ("cat-file", "-e") and result.stdout):
        raise ReviewError("OAH_REVIEW_GIT_FAILED", "Git inspection failed; restore the repository and retry")
    if args[0] == "ls-tree" and not result.stdout:
        return ABSENT, ""
    return PRESENT, result.stdout


def _commit(root: pathlib.Path, rev: str) -> str | None:
    status, out = _budget_git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}")
    return out.strip() if status == PRESENT else None


def _budget_commit_exists(root: pathlib.Path, rev: str) -> bool:
    """Only a quiet missing-object result confirms absence; peeling errors are not absence."""
    if _budget_git(root, "cat-file", "-e", rev)[0] == ABSENT:
        return False
    _budget_git(root, "cat-file", "-e", f"{rev}^{{commit}}")
    return True


def _default_ref(root: pathlib.Path, remote: str, branch: str) -> str | None:
    # Diagnose invalid names without interpreting a failed Git process as validation.
    if branch and (branch.startswith("-") or branch == "@" or "@{" in branch or ".." in branch
                   or branch.endswith(".")
                   or any(not part or part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
                   or any(ord(char) <= 32 or ord(char) == 127 or char in "~^:?*[\\" for char in branch)):
        raise ReviewError("OAH_CONFIG_INVALID", "[preflight] default_branch is not a branch name; set it "
                          "to the name of the default branch (such as main) in .oah/config.toml")
    if branch:
        _budget_git(root, "check-ref-format", "--branch", branch)
    remotes = _budget_git(root, "remote")[1].split()
    chosen = remote if remote in remotes else "origin" if "origin" in remotes else (
        remotes[0] if len(remotes) == 1 else None)
    candidates = []
    if branch:
        if chosen:
            candidates.append(f"refs/remotes/{chosen}/{branch}")
        candidates.append(f"refs/heads/{branch}")
    else:
        if chosen:
            status, out = _budget_git(root, "symbolic-ref", "--quiet", f"refs/remotes/{chosen}/HEAD")
            if status == PRESENT:
                candidates.append(out.strip())
            candidates += [f"refs/remotes/{chosen}/main", f"refs/remotes/{chosen}/master"]
        candidates += ["refs/heads/main", "refs/heads/master"]
    return next((ref for ref in candidates if _commit(root, ref)), None)


def resolve(root: pathlib.Path, base_arg: str | None, *, remote: str = "", branch: str = "") -> Range:
    """The reviewed range as full SHAs."""
    head = _commit(root, "HEAD")
    if head is None:
        raise ReviewError("OAH_REVIEW_NOTHING_TO_REVIEW", "this repository has no commits yet; commit the work, "
                          "then run oah review")
    if base_arg is not None:
        base = _commit(root, base_arg) if base_arg and not base_arg.startswith("-") else None
        if base is None:
            raise ReviewError("OAH_REVIEW_BASE_INVALID", f"--base {base_arg!r} is not a commit of this repository; "
                              "pass a branch, tag or SHA")
        label = f"--base {base_arg}"
    else:
        ref = _default_ref(root, remote, branch)
        if ref is None:
            raise ReviewError("OAH_REVIEW_BASE_INVALID", "no default branch was found to review against (no main or "
                              "master, locally or on the remote); pass --base <branch or SHA>, or set "
                              "[preflight] default_branch in .oah/config.toml")
        code, out = _git(root, "merge-base", "HEAD", ref, ok=(0, 1))
        if code != 0 or not out.strip():
            raise ReviewError("OAH_REVIEW_BASE_INVALID", f"HEAD and {ref} have no common ancestor; pass --base "
                              "<branch or SHA>")
        base, label = out.strip(), f"merge-base with {ref}"
    if base != head:
        code, _ = _git(root, "merge-base", "--is-ancestor", base, head, ok=(0, 1))
        if code != 0:
            raise ReviewError("OAH_REVIEW_BASE_INVALID", f"the base {base} is not an ancestor of HEAD {head}; pass "
                              "--base with a commit HEAD was built on")
    if base == head:
        raise ReviewError("OAH_REVIEW_NOTHING_TO_REVIEW", f"HEAD {head} is the base ({label}), so there is nothing "
                          "to review; commit the work on a task branch, or pass --base")
    return Range(base=base, head=head, base_label=label)


def tracked_dirty(root: pathlib.Path) -> list[str]:
    """Tracked paths with uncommitted changes (staged or not); untracked files are ignored."""
    _code, out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    fields, paths, skip = out.split("\0"), [], False
    for field in fields:
        if skip:
            skip = False
            continue
        if len(field) > 3:
            paths.append(field[3:])
            skip = "R" in field[:2] or "C" in field[:2]
    return sorted(paths)


def changed_files(root: pathlib.Path, base: str, head: str) -> list[str]:
    _code, out = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", base, head,
                      "--")
    return [name for name in out.split("\0") if name]


def diff_stat(root: pathlib.Path, base: str, head: str) -> str:
    _code, out = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--stat=200,160", base, head, "--")
    lines = out.rstrip("\n").split("\n")
    if len(lines) > STAT_LINES:
        lines = lines[:STAT_LINES - 1] + [f"… {len(lines) - STAT_LINES + 1} more lines (run git diff --stat yourself)",
                                          lines[-1]]
    return "\n".join(lines)


def critical(changed: Iterable[str], patterns: Sequence[str]) -> list[str]:
    """Changed paths matching a critical glob; letter case is ignored as well."""
    return [path for path in changed if any(glob_match(pattern, path) or
                                            glob_match(pattern.casefold(), path.casefold()) for pattern in patterns)]


def _touched_db(changed: Sequence[str], data: Mapping) -> set[str]:
    """Entities defined or referenced by changed paths, including paths removed at HEAD."""
    graph = data.get("db")
    if not graph:
        return set()
    changed_set = set(changed)
    touched = {entity for entity, info in graph["entities"].items()
               if any(event[0] in changed_set for event in info["events"])
               or any(index[1] in changed_set for index in info["indexes"])}
    names = {name for name, hits in (data.get("db_hits") or {}).items() if any(hit[0] in changed_set for hit in hits)}
    touched |= {entity for entity, info in graph["entities"].items()
                if entity.startswith(("db_table:", "db_routine:")) and info["name"] in names}
    return touched


def db_impact(changed: Sequence[str], data: Mapping, commit: str, settings: Mapping, *,
              before: tuple[Mapping, str] | None = None,
              default_branch: tuple[Mapping, str] | None = None) -> DbImpact:
    """Query touched entities from both sides of the diff and the fetched default-branch map."""
    from oah.impact import mapstore, query
    from oah.impact_cli import _rows

    sources = {"worktree": (data, commit)}
    if before is not None:
        sources["base"] = before
    if default_branch is not None:
        sources["default_branch"] = default_branch
    touched = _touched_db(changed, data)
    if before is not None:
        touched |= _touched_db(changed, before[0])
    if not touched and not any(mapped.get("db") for mapped, _sha in sources.values()):
        return DbImpact(query.db_line({"status": "unavailable"}), None, (), ())
    if not touched:
        return DbImpact(NOT_TOUCHED, None, (), ())
    targets = sorted(touched)
    # A removed entity may exist only at the base. Query each graph for the entities it knows;
    # every target came from one of the two reviewed commits, so none can disappear silently.
    found: dict = {}
    for label, (mapped, _sha) in sources.items():
        for row in _rows(mapped, targets, settings=settings):
            found.setdefault(row, []).append(label.replace("_", "-"))
    rows = query.rank(found)
    origins = {row: "+".join(labels) for row, labels in found.items()}
    lines = [f"{row.binding} {row.layer} {row.where} {row.target} {row.detail} [{origins[row]}]".rstrip()
             for row in rows]
    if len(lines) > ROW_LINES:
        lines = lines[:ROW_LINES] + [f"… {len(lines) - ROW_LINES} more rows (run the query yourself)"]
    identity = query.db_identity(data, commit, rows, default_branch=default_branch)
    if before is not None:
        identity = {"status": "available", "maps": {
            label: {"commit": sha, "graph": mapstore.digest(mapped.get("db"))}
            for label, (mapped, sha) in sources.items()},
            "merged_output": mapstore.digest([query.row_json(row, origins) for row in rows])}
    return DbImpact(query.db_line(identity), "oah impact " + " ".join(targets), tuple(targets), tuple(lines))


def assess_impact(root: pathlib.Path, changed: Sequence[str], base: str, head: str, info, data: Mapping, *,
                  env: Mapping[str, str]) -> tuple[DbImpact, dict, tuple[str, ...], list[str]]:
    """Use the landed refresh/merge helpers; only impact reads the leased default-branch mirror."""
    from oah.impact import defaultbranch, gatebind, mapstore, query
    from oah.impact.defaults import configured
    from oah.impact.errors import ImpactError
    from oah.impact_cli import _rows, _with_live_hits

    settings = configured(root)
    base_info, base_data = mapstore.snapshot(root, base, settings=settings)
    warnings = info.warnings() + base_info.warnings()
    gates, _status = gatebind.discover(root, rev=head)
    mine = _with_live_hits(root, dict(data), head, list(changed), settings)
    mine_rows = _rows(mine, list(changed), gates=gates, graph=query.load_graph(root, settings), settings=settings)
    with defaultbranch._query_context(root, settings, env=env) as branch:
        if branch.warning():
            warnings.append(branch.warning())
        theirs = branch.data
        if branch.info:
            warnings += branch.info.warnings()
        if theirs is not None:
            source = defaultbranch._snapshot_source(root, branch.commit, env)
            # All missing-path searches use this refresh's private, leased object source.
            try:
                theirs = _with_live_hits(source, theirs, branch.commit, list(changed), settings)
            except ImpactError:
                if branch.state != "stale":
                    raise
                warnings.append("OAH_IMPACT_INCOMPLETE: default-branch path searches could not run; "
                                "restore the remote and run oah review again")
        rows, found = query.merge_maps(mine_rows, _rows(theirs, list(changed), settings=settings) if theirs else [])
        db = db_impact(changed, data, head, settings, before=(base_data, base),
                       default_branch=(theirs, branch.commit) if theirs is not None else None)
        report = {"worktree": info.to_json(), "base": base_info.to_json(), "default_branch": branch.to_json(),
                  "result": mapstore.digest([query.row_json(row, found) for row in rows])}
    lines = tuple(f"{row.binding} {row.layer} {row.where} {row.target} {row.detail} [{found[row]}]" for row in rows)
    return db, report, lines, warnings
