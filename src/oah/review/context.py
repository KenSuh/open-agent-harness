# 리뷰 맥락 — base·head 를 전체 SHA 로 정하고(기본은 기본 브랜치와의 merge-base), 추적 파일이 깨끗한지, diff stat·바뀐 파일·중요 경로·DB 영향 줄을 고정된 head 에서 만든다.
"""What one round reviews and what its prompt states (PLAN 5.9 steps 1 and 5, A3, A23).

`resolve(root, base_arg)` returns full SHAs: the head is HEAD, and the base is `--base` (which must be an
ancestor of HEAD) or the merge-base of HEAD with the default branch. The default branch is found locally,
without a fetch: `[preflight] default_branch` (on the configured or `origin` remote, then locally), else the
remote's HEAD branch, `main` or `master` on that remote, then local `main` or `master`.

Everything else reads the fixed head, never the refreshed default branch (A23): the diff stat and changed
files of base..head, the configured critical paths among them, and the DB impact. A change touches DB
entities when a changed file defines or alters one (an event or index in the DB graph) or refers to a table
or routine by name; the query then runs on the worktree map that `oah review` has just refreshed, and its
identity line (`db-impact: commit=… graph=… output=…`) is what the reviewer must copy (A3).
"""
from __future__ import annotations

import dataclasses
import pathlib
from typing import Iterable, Mapping, Sequence

from oah.impact.kinds import glob_match
from oah.paths import run_git
from oah.review import ReviewError

NOT_TOUCHED = "db-impact: not touched (no DB entity changes in base..head)"
STAT_LINES = 300
ROW_LINES = 80


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


def _git(root: pathlib.Path, *args: str, ok: tuple[int, ...] = (0,)) -> tuple[int | None, str]:
    result = run_git(root, *args)
    if result.returncode not in ok:
        raise ReviewError("OAH_REVIEW_GIT_FAILED", f"git {args[0]} failed in {root} "
                          f"({result.stderr.strip() or 'no output'}); check the repository and retry")
    return result.returncode, result.stdout


def _commit(root: pathlib.Path, rev: str) -> str | None:
    code, out = _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}", ok=(0, 1))
    return out.strip() if code == 0 and out.strip() else None


def _default_ref(root: pathlib.Path, remote: str, branch: str) -> str | None:
    remotes = _git(root, "remote")[1].split()
    chosen = remote if remote in remotes else "origin" if "origin" in remotes else (
        remotes[0] if len(remotes) == 1 else None)
    candidates = []
    if branch:
        if chosen:
            candidates.append(f"refs/remotes/{chosen}/{branch}")
        candidates.append(f"refs/heads/{branch}")
    else:
        if chosen:
            code, out = _git(root, "symbolic-ref", "--quiet", f"refs/remotes/{chosen}/HEAD", ok=(0, 1, 128))
            if code == 0 and out.strip():
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


def db_impact(changed: Sequence[str], data: Mapping, commit: str, settings: Mapping) -> DbImpact:
    """The DB entities base..head touches, the query oah ran for them and its identity line."""
    from oah.impact import query

    graph = data.get("db")
    if not graph:
        return DbImpact(query.db_line({"status": "unavailable"}), None, (), ())
    changed_set = set(changed)
    touched = {entity for entity, info in graph["entities"].items()
               if any(event[0] in changed_set for event in info["events"])
               or any(index[1] in changed_set for index in info["indexes"])}
    names = {name for name, hits in (data.get("db_hits") or {}).items() if any(hit[0] in changed_set for hit in hits)}
    touched |= {entity for entity, info in graph["entities"].items()
                if entity.startswith(("db_table:", "db_routine:")) and info["name"] in names}
    if not touched:
        return DbImpact(NOT_TOUCHED, None, (), ())
    targets = sorted(touched)
    rows = query.query(data, targets, settings=settings)
    lines = [f"{row.binding} {row.layer} {row.where} {row.target} {row.detail}".rstrip() for row in rows]
    if len(lines) > ROW_LINES:
        lines = lines[:ROW_LINES] + [f"… {len(lines) - ROW_LINES} more rows (run the query yourself)"]
    identity = query.db_identity(data, commit, rows)
    return DbImpact(query.db_line(identity), "oah impact " + " ".join(targets), tuple(targets), tuple(lines))
