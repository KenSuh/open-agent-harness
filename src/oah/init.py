# 안내형 oah init 의 핵심 — 프로젝트 환경(설정·템플릿·위키·티어 에이전트·.graphifyignore·.gitignore 등)을 데이터로 계획하고, 보여 준 계획 그대로만 쓴다.
"""Guided `oah init` (PLAN 6.5, 7): plan the project environment as data, then write exactly that plan.

Every planned file is a `FileChange`: `create` (missing), `replace` (differs, and `--force` was given),
`update` (managed content added in place: missing `.gitignore` entries, the guide's index line), `keep`
(exists and stays as it is) or `unchanged` (already what init would write). The wiki's `index.md` and
`log.md` are content: they are never replaced, not even with `--force`. Nothing is written through a
link, which could reach a file outside the project. A file that changed after the plan was shown is
skipped with `OAH_INIT_CHANGED`. Settings are parameters, so this module never reads the config itself.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
from typing import Mapping

from oah import intent, models, routing
from oah.atomic import write_text_atomic
from oah.engines.effort import EngineError
from oah.paths import run_git
from oah.wiki import notes
from oah.wiki.base import WikiError, publish_new, read_bytes
from oah.wiki.log import parse_log
from oah.wiki.markdown import eol_of

CONFIG_PATH = ".oah/config.toml"
TEMPLATES_DIR = ".oah/templates"
AGENTS_DIR = ".claude/agents"
GRAPHIFYIGNORE = ".graphifyignore"
GITIGNORE = ".gitignore"
UNLAZY_PROJECT = ".agents/skills/unlazy"
WIKI_CONTENT = ("index.md", "log.md")  # the wiki's own content: kept, never replaced
GUIDE_SLUG, GUIDE_TITLE = "wiki-guide", "How this wiki works"
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69
WRITES = ("create", "replace", "update")
TIER_WORK = {
    "low": "quick lookups, docs and short summaries",
    "medium": "routine implementation, refactors, research and reviews of one area",
    "high": "broad audits, migrations, security work, high-risk reviews and reasoning across many files",
    "max": "critical-risk work and the hardest reasoning",
}
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_PLAIN = re.compile(r"[A-Za-z0-9._-]+")
_GUIDE_LINK = re.compile(r"\[\[" + GUIDE_SLUG + r"(?:[|#\]])")


class InitError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass(frozen=True)
class FileChange:
    path: str  # repository-relative, POSIX
    action: str  # create | replace | update | keep | unchanged
    group: str  # config | templates | wiki | agents | graph | gitignore
    data: bytes | None = None  # the bytes to write, for create, replace and update
    before: bytes | None = None  # the bytes found while planning
    note: str = ""


# Templates -------------------------------------------------------------------------------------------

def _template(name: str, **values: str) -> str:
    """templates/init/<name> with every {{NAME}} filled in one pass (a value is never re-read)."""
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], notes.packaged("init", name))


def render_config(*, branch: str, how: str, jev: bool, skill_updates: bool, today: dt.date) -> str:
    """`.oah/config.toml` with the owner's defaults (PLAN 5.5), the default branch and the two recorded answers."""
    rerank_text = _template("rerank-on.toml" if jev else "rerank-off.toml", DATE=today.isoformat())
    return _template("config.toml", DEFAULT_BRANCH=json.dumps(branch, ensure_ascii=False), BRANCH_HOW=how,
                     AUTO_UPDATE="true" if skill_updates else "false", RERANK=rerank_text)


def _yaml(value: str) -> str:
    return value if _PLAIN.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def agent_files(engines: Mapping) -> dict[str, bytes]:
    """`.claude/agents/oah-<tier>.md` per tier: model by A14's mapping (a tier pin wins), effort from the tier."""
    claude = engines["claude"]
    files = {}
    for tier in routing.TIERS:
        values = claude["tiers"][tier]
        try:
            model, _source = models.map_tier("claude", tier, pin=values["model"],
                                             quality_order=claude["quality_order"], entry=None, effective=None)
        except EngineError:
            model = "inherit"  # nothing resolves: the subagent uses the session's model
        description = (f"oah {tier} tier: {TIER_WORK[tier]}. Model {model}, effort {values['effort']}, "
                       "from .oah/config.toml.")
        text = _template("agent.md", NAME=f"oah-{tier}", DESCRIPTION=json.dumps(description, ensure_ascii=False),
                         MODEL=_yaml(model), EFFORT=_yaml(values["effort"]), TIER=tier, FOOTER=intent.FOOTER)
        files[f"{AGENTS_DIR}/oah-{tier}.md"] = text.encode("utf-8")
    return files


def _gitignore_groups(unlazy_copy: bool) -> list[tuple[str, list[str]]]:
    """(comment line, entries) groups: ledgers and secrets (A17), Obsidian UI state (A8d), unlazy's copy."""
    groups: list[tuple[str, list[str]]] = []
    for text in (notes.packaged("init", "gitignore.txt"), notes.gitignore_snippet()):
        comment, entries = "", []
        for line in text.split("\n"):
            if line.startswith("#"):
                if entries:
                    groups.append((comment, entries))
                comment, entries = line, []
            elif line.strip():
                entries.append(line.strip())
        if entries:
            groups.append((comment, entries))
    if unlazy_copy:
        groups.append(("# The project copy of unlazy that oah init installed; it is a git checkout of its own.",
                       [UNLAZY_PROJECT + "/"]))
    return groups


def add_gitignore_entries(data: bytes | None, *, unlazy_copy: bool) -> bytes | None:
    """data with the missing entries appended in its line ending; None when nothing is missing."""
    present = {line.strip() for line in (data or b"").decode("utf-8").splitlines()}
    block: list[str] = []
    for comment, entries in _gitignore_groups(unlazy_copy):
        missing = [entry for entry in entries if entry not in present]
        if missing:
            block += [comment, *missing]
    if not block:
        return None
    eol = eol_of(data).decode("ascii") if data else "\n"
    out = data or b""
    if out and not out.endswith(b"\n"):
        out += eol.encode("ascii")
    if out.strip() and not out.endswith((eol * 2).encode("ascii")):
        out += eol.encode("ascii")
    return out + (eol.join(block) + eol).encode("utf-8")


# The default branch ----------------------------------------------------------------------------------

def default_branch(root: pathlib.Path) -> tuple[str, str]:
    """(branch, how it was found), without the network: the remote's HEAD, its main or master, a local
    main or master, the current branch, else main."""
    def git(*args: str) -> str:
        result = run_git(pathlib.Path(root), *args)
        return result.stdout.strip() if result.returncode == 0 else ""

    remotes = git("remote").split()
    remote = "origin" if "origin" in remotes else remotes[0] if len(remotes) == 1 else None
    if remote is not None:
        head = git("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        if head.startswith(remote + "/") and len(head) > len(remote) + 1:
            return head[len(remote) + 1:], f"from {remote}/HEAD"
        for name in ("main", "master"):
            if git("rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{name}"):
                return name, f"from {remote}/{name}"
    for name in ("main", "master"):
        if git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name, f"the local {name} branch"
    current = git("symbolic-ref", "--quiet", "--short", "HEAD")
    if current:
        return current, "the current branch"
    return "main", "a default, because HEAD names no branch"


# Planning and writing files --------------------------------------------------------------------------

def _linked(root: pathlib.Path, relative: str) -> str | None:
    """The first existing link (symlink or junction) on the way from root to the file, if any."""
    current = root
    for part in pathlib.PurePosixPath(relative).parts:
        current = current / part
        isjunction = getattr(os.path, "isjunction", None)
        if current.is_symlink() or (isjunction is not None and isjunction(current)):
            return current.relative_to(root).as_posix()
    return None


def _wiki_path(wiki_dir: str, name: str) -> str:
    return (pathlib.PurePosixPath(wiki_dir.replace("\\", "/")) / name).as_posix()


def plan_files(root: pathlib.Path, *, config: bytes, wiki_dir: str, agents: Mapping[str, bytes], graph: bool,
               unlazy_copy: bool, force: bool, today: dt.date) -> list[FileChange]:
    """Every file init manages, with what would happen to it. Reads only."""
    root = pathlib.Path(root)
    changes: list[FileChange] = []

    def owned(path: str, data: bytes, group: str) -> FileChange:
        before = read_bytes(root / path)
        link = _linked(root, path)
        if link is not None:
            change = FileChange(path, "keep", group, None, before,
                                f"{link} is a link; oah never writes through links, which could reach files outside "
                                "the project")
        elif before is None:
            change = FileChange(path, "create", group, data)
        elif before == data:
            change = FileChange(path, "unchanged", group, None, before)
        elif force:
            change = FileChange(path, "replace", group, data, before)
        else:
            change = FileChange(path, "keep", group, None, before, "exists; pass --force to replace it")
        changes.append(change)
        return change

    owned(CONFIG_PATH, config, "config")
    readme = _template("templates-readme.md", WIKI_DIR=wiki_dir.replace("\\", "/")).encode("utf-8")
    for name, data in (("PACKET.md", notes.packaged("packet", "PACKET.md").encode("utf-8")),
                       ("GATES.md", notes.packaged("gates", "GATES.md").encode("utf-8")), ("README.md", readme)):
        owned(f"{TEMPLATES_DIR}/{name}", data, "templates")
    scaffold = notes.scaffold_files(today)
    guide_missing = read_bytes(root / _wiki_path(wiki_dir, f"{GUIDE_SLUG}.md")) is None
    for name in WIKI_CONTENT:
        changes.append(_plan_wiki_content(root, _wiki_path(wiki_dir, name), scaffold[name], guide_missing))
    for name, data in scaffold.items():
        if name not in WIKI_CONTENT:
            owned(_wiki_path(wiki_dir, name), data, "wiki")
    for path, data in agents.items():
        owned(path, data, "agents")
    if graph:
        owned(GRAPHIFYIGNORE, _template("graphifyignore.txt", WIKI_DIR=wiki_dir.replace("\\", "/")).encode("utf-8"),
              "graph")
    changes.append(_plan_gitignore(root, unlazy_copy))
    return changes


def _plan_wiki_content(root: pathlib.Path, path: str, fresh: bytes, guide_missing: bool) -> FileChange:
    before = read_bytes(root / path)
    link = _linked(root, path)
    if link is not None:
        return FileChange(path, "keep", "wiki", None, before, f"{link} is a link; oah never writes through links")
    if before is None:
        return FileChange(path, "create", "wiki", fresh)
    if before == fresh:
        return FileChange(path, "unchanged", "wiki", None, before)
    keep = "wiki content; init never replaces it"
    if path.endswith("/index.md") and guide_missing:
        try:
            if not _GUIDE_LINK.search(before.decode("utf-8")):
                updated = notes.add_to_index(before, "Notes", notes.index_item(GUIDE_SLUG, GUIDE_TITLE))
                return FileChange(path, "update", "wiki", updated, before, f"add the line for {GUIDE_SLUG}.md")
        except (UnicodeDecodeError, WikiError):
            return FileChange(path, "keep", "wiki", None, before,
                              f"{keep}; it is not UTF-8 text, so add [[{GUIDE_SLUG}]] to it by hand")
    if path.endswith("/log.md"):
        try:
            parse_log(before, "log.md")
        except WikiError as exc:
            return FileChange(path, "keep", "wiki", None, before, f"{keep}; oah wiki commands refuse it: {exc}")
    return FileChange(path, "keep", "wiki", None, before, keep)


def _plan_gitignore(root: pathlib.Path, unlazy_copy: bool) -> FileChange:
    before = read_bytes(root / GITIGNORE)
    if (root / GITIGNORE).is_symlink():
        return FileChange(GITIGNORE, "keep", "gitignore", None, before, "it is a link; oah never writes through links")
    try:
        data = add_gitignore_entries(before, unlazy_copy=unlazy_copy)
    except UnicodeDecodeError:
        missing = [entry for _comment, entries in _gitignore_groups(unlazy_copy) for entry in entries]
        return FileChange(GITIGNORE, "keep", "gitignore", None, before,
                          f"it is not UTF-8 text, so add these entries by hand: {', '.join(missing)}")
    if data is None:
        return FileChange(GITIGNORE, "unchanged", "gitignore", None, before)
    if before is None:
        return FileChange(GITIGNORE, "create", "gitignore", data)
    added = len(data.decode("utf-8").splitlines()) - len(before.decode("utf-8").splitlines())
    return FileChange(GITIGNORE, "update", "gitignore", data, before, f"append {added} lines of missing entries")


def write_files(root: pathlib.Path, changes: list[FileChange], *, wiki) -> tuple[list[FileChange], list[str]]:
    """Write the planned creates, replacements and updates; (written, problems). Wiki files are written
    under the wiki lock. A path that changed since the plan is skipped with OAH_INIT_CHANGED."""
    root = pathlib.Path(root)
    written: list[FileChange] = []
    problems: list[str] = []

    def write(change: FileChange) -> None:
        path = root / change.path
        if change.action == "create":
            if publish_new(path, change.data):
                written.append(change)
                return
            problems.append(f"OAH_INIT_CHANGED: {change.path} appeared after the plan was shown, so init left it "
                            "as it is. Run oah init again to see the new plan.")
            return
        if read_bytes(path) != change.before or _linked(root, change.path) is not None:
            problems.append(f"OAH_INIT_CHANGED: {change.path} changed after the plan was shown, so init left it "
                            "as it is. Run oah init again to see the new plan.")
            return
        write_text_atomic(path, change.data.decode("utf-8"))
        written.append(change)

    pending = [change for change in changes if change.action in WRITES]
    for change in pending:
        if change.group != "wiki":
            write(change)
    wiki_changes = [change for change in pending if change.group == "wiki"]
    if wiki_changes:
        with wiki.session():
            for change in wiki_changes:
                write(change)
    return written, problems
