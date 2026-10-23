# 위키 커밋 검사(oah hook pre-commit) — 스테이징된 트리를 HEAD 와 비교해 로그 축소·아카이브·raw 수정·비 UTF-8 을 막고 새 깨진 링크를 경고하거나 막는다.
"""The opt-in git pre-commit check for the wiki (PLAN 5.10, A8c).

Lane P5 owns `oah hook` and the installer; it calls run(args). Everything is read from git objects
(HEAD and the index), so the check sees exactly what the commit will contain.
"""
from __future__ import annotations

import argparse
import collections
import os
import pathlib
import sys
from dataclasses import dataclass, field

from oah.atomic import StateCorrupt, StorageError
from oah.config import ConfigError, load_config
from oah.engines.proc import run_command
from oah.paths import GitMissing, NotARepo, git_argv, repo_info, run_git, state_dir
from oah.wiki import log as log_module
from oah.wiki.base import DEFAULTS, EXIT_ENVIRONMENT, WikiError, WikiSettings
from oah.wiki.lint import Names, link_root_paths, links_checked
from oah.wiki.markdown import wikilinks
from oah.wiki.store import JOURNAL

EXIT_OK, EXIT_BLOCKED = 0, 1
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".canvas")
GIT_TIMEOUT = 120.0


@dataclass
class Verdict:
    blocked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _git(root: pathlib.Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Run git and return its raw stdout bytes (latin-1 maps every byte to one character and back)."""
    result = run_command(git_argv() + ("-C", str(root), *args), timeout=GIT_TIMEOUT, encoding="latin-1",
                         stdin_text=None if stdin is None else stdin.decode("latin-1"))
    if result.returncode != 0:
        raise WikiError("OAH_GIT_FAILED", f"git {args[0]} failed in {root}: {result.stderr.strip()}. "
                        "Check the repository, then commit again.", EXIT_ENVIRONMENT)
    return result.stdout.encode("latin-1")


def _paths(raw: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]


def _blobs(root: pathlib.Path, rev: str, paths: list[str]) -> dict[str, bytes]:
    """Contents of paths at rev ("HEAD", or "" for the index) through one `git cat-file --batch`."""
    paths = [path for path in paths if "\n" not in path]
    if not paths:
        return {}
    request = b"".join(f"{rev}:{path}\n".encode("utf-8", errors="surrogateescape") for path in paths)
    out, position, blobs = _git(root, "cat-file", "--batch", stdin=request), 0, {}
    for path in paths:
        end = out.index(b"\n", position)
        header = out[position:end].split(b" ")
        position = end + 1
        if len(header) != 3:
            continue  # "<name> missing"
        size = int(header[2])
        blobs[path] = out[position:position + size]
        position += size + 1
    return blobs


def _valid_utf8(data: bytes | None) -> bool:
    try:
        (data or b"").decode("utf-8")  # the strict codec also refuses encoded surrogates
    except UnicodeDecodeError:
        return False
    return True


def _broken(blobs: dict[str, bytes], prefix: str, names: Names, naming) -> collections.Counter:
    found: collections.Counter = collections.Counter()
    for path, data in blobs.items():
        rel = path[len(prefix) + 1:]
        if not links_checked(rel, naming):
            continue
        for link in wikilinks(data.decode("utf-8", errors="replace")):
            if not names.resolve(link.target):
                found[(rel, link.raw)] += 1
    return found


def _names(rels: list[str], root: pathlib.Path, settings: WikiSettings) -> Names:
    names = Names([])
    names.add(rels)
    for folder in link_root_paths(root, settings):
        names.add_folder(folder, f"{folder}:")
    return names


def check(root: pathlib.Path, settings: WikiSettings, state: pathlib.Path) -> Verdict:
    verdict = Verdict()
    if (pathlib.Path(state) / "wiki" / JOURNAL).exists():
        verdict.blocked.append("OAH_WIKI_RECOVERY_PENDING: an interrupted wiki rollover is pending; run "
                               "`oah wiki recover`, stage the result, then commit again")
        return verdict
    prefix = settings.dir.replace("\\", "/").strip("/")
    head = run_git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
    raw = _git(root, "diff", "--cached", "--name-status", "-z", "--no-renames", "HEAD" if head else EMPTY_TREE,
               "--", prefix).split(b"\0")
    changes = [(raw[i].decode("ascii")[:1], raw[i + 1].decode("utf-8", errors="surrogateescape"))
               for i in range(0, len(raw) - 1, 2) if raw[i]]
    if not changes:
        return verdict
    head_files = _paths(_git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", prefix)) if head else []
    staged_files = _paths(_git(root, "ls-files", "-z", "--cached", "--", prefix))
    text = lambda files: [path for path in files if path.lower().endswith(TEXT_SUFFIXES)]  # noqa: E731
    head_blobs = _blobs(root, "HEAD", text(head_files)) if head else {}
    staged_blobs = _blobs(root, "", text(staged_files))

    added_archives = []
    for status, path in changes:
        rel = path[len(prefix) + 1:]
        archive = settings.naming.number(rel) is not None
        if archive and status == "A":
            added_archives.append(rel)
        elif archive:
            verdict.blocked.append(f"OAH_WIKI_COMMIT_BLOCKED: {path} is an archived log and must never change; "
                                   f"restore it with `git restore --staged --worktree -- {path}`")
        if rel.startswith("raw/") and status != "A":
            verdict.blocked.append(f"OAH_WIKI_COMMIT_BLOCKED: {path} is an imported source under raw/, which is "
                                   f"append-only; restore it with `git restore --staged --worktree -- {path}` "
                                   "and add a new file instead")
        if status in "AMT" and path.lower().endswith(TEXT_SUFFIXES) and not _valid_utf8(staged_blobs.get(path)):
            if status == "A" or _valid_utf8(head_blobs.get(path)):
                verdict.blocked.append(f"OAH_WIKI_COMMIT_BLOCKED: {path} has bytes that are not valid UTF-8 "
                                       "(or encode surrogates); save it as UTF-8 and stage it again")

    log_path = f"{prefix}/log.md"
    head_log, staged_log = head_blobs.get(log_path), staged_blobs.get(log_path)
    if head_log is not None and (staged_log is None or len(staged_log) < len(head_log)):
        if staged_log is None or not added_archives:
            verdict.blocked.append(f"OAH_WIKI_COMMIT_BLOCKED: {log_path} got shorter but no new log archive is "
                                   "staged; use `oah wiki rollover` to move old entries, or restore the removed "
                                   "entries")
        else:
            problem = _lost_entries(prefix, head_blobs, staged_blobs, settings.naming)
            if problem:
                verdict.blocked.append(f"OAH_WIKI_COMMIT_BLOCKED: {problem}")

    names_head = _names([path[len(prefix) + 1:] for path in head_files], root, settings)
    names_staged = _names([path[len(prefix) + 1:] for path in staged_files], root, settings)
    new = (_broken(staged_blobs, prefix, names_staged, settings.naming)
           - _broken(head_blobs, prefix, names_head, settings.naming))
    if new:
        lines = [f"{prefix}/{rel}: {link} does not resolve" for (rel, link) in sorted(new)]
        if settings.new_broken_links == "block":
            verdict.blocked.extend(f"OAH_WIKI_COMMIT_BLOCKED: new broken link {line}; create the note or fix "
                                   "the link" for line in lines)
        else:
            verdict.warnings.extend(f"OAH_WIKI_NEW_BROKEN_LINKS: {line}; create the note or fix the link "
                                    '(set [wiki] new_broken_links = "block" to refuse such commits)'
                                    for line in lines)
    return verdict


def _lost_entries(prefix: str, head_blobs: dict[str, bytes], staged_blobs: dict[str, bytes],
                  naming: log_module.ArchiveNaming) -> str | None:
    """None when every HEAD log entry survives in the staged log or archives, byte for byte."""
    def stream(blobs: dict[str, bytes]) -> bytes:
        archives = {}
        for path, data in blobs.items():
            number = naming.number(path[len(prefix) + 1:])
            if number is not None:
                archives[number] = data
        return log_module.preservation_stream(blobs.get(f"{prefix}/log.md", b""), archives, naming)

    try:
        before, after = stream(head_blobs), stream(staged_blobs)
    except WikiError as exc:
        return f"the staged log or an archive does not parse ({exc})"
    if not after.endswith(before):
        return (f"entries removed from {prefix}/log.md are not in the new archive byte for byte; undo the edit "
                "and run `oah wiki rollover` instead")
    return None


def _settings(root: pathlib.Path) -> WikiSettings:
    """The [wiki] section of .oah/config.toml over DEFAULTS."""
    return WikiSettings.from_mapping(load_config(root).section("wiki", DEFAULTS))


def run(args: argparse.Namespace) -> int:
    try:
        info = repo_info(pathlib.Path.cwd())
        verdict = check(info.root, _settings(info.root), state_dir(info, os.environ))
    except WikiError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except (ConfigError, NotARepo, GitMissing, StateCorrupt, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    for line in verdict.warnings + verdict.blocked:
        print(line, file=sys.stderr)
    if verdict.blocked:
        print("The wiki commit check refused this commit. Fix the lines above, stage the fixes and commit again.",
              file=sys.stderr)
        return EXIT_BLOCKED
    return EXIT_OK
