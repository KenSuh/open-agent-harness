# 안내형 oah init 의 핵심 — 프로젝트 환경(설정·템플릿·위키·티어 에이전트·.graphifyignore·.gitignore 등)을 데이터로 계획하고, 보여 준 계획 그대로만 쓴다.
"""Guided `oah init` (PLAN 6.5, 7): plan the project environment as data, then write exactly that plan.

Every planned file is a `FileChange`: `create` (missing), `replace` (differs, and `--force` was given),
`update` (managed content added in place: missing `.gitignore` entries, the guide's index line), `keep`
(exists and stays as it is), `unchanged` (already what init would write) or `blocked` (a link is on its
path: nothing is written through a link, which could reach a file outside the project). The wiki's
`index.md` and `log.md` are content: they are never replaced, not even with `--force`. A file that changed
after the plan was shown is skipped with `OAH_INIT_CHANGED`. A blocked file, a refused hook or a skipped
CI starter leaves the run incomplete. Settings are parameters, so this module never reads the config.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import sys
import time
from typing import Callable, Mapping, NamedTuple

from oah import call, ci, hooks, intent, models, rerank, routing, rules, skillsel, unlazy
from oah.atomic import StateCorrupt, StorageError, write_text_atomic
from oah.config import Config, ConfigError, parse_config
from oah.engines.effort import EngineError
from oah.engines.proc import run_command
from oah.impact import mapstore
from oah.impact.errors import ImpactError
from oah.journal import PendingTransaction
from oah.lockfile import LockTimeout
from oah.paths import GitMissing, NotARepo, git_argv, repo_info, run_git, state_dir
from oah.setup import companions, guide
from oah.setup.tools import Tool, catalog, display, os_name_for
from oah.wiki import notes
from oah.wiki.base import DEFAULTS as WIKI_DEFAULTS, WikiError, WikiSettings, publish_new, read_bytes
from oah.wiki.log import parse_log
from oah.wiki.markdown import eol_of
from oah.wiki.store import Wiki

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
KEEP_NOTES = {CONFIG_PATH: "your settings stay as they are; --force rewrites them with oah's defaults"}
TIER_WORK = {
    "low": "quick lookups, docs and short summaries",
    "medium": "routine implementation, refactors, research and reviews of one area",
    "high": "broad audits, migrations, security work, high-risk reviews and reasoning across many files",
    "max": "critical-risk work and the hardest reasoning",
}
NETWORK_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # a clone never stops to ask
CLONE_TIMEOUT = 600.0
RENAME_RETRY_SECONDS = 2.0
CLAUDE_SETTINGS = ".claude/settings.local.json"
LINK_REPARSE_TAGS = (getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
                     getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003))  # Windows symlinks and junctions
MAP_FAILURES = (ImpactError, ConfigError, LockTimeout, StateCorrupt, StorageError, GitMissing, NotARepo,
                PendingTransaction, OSError)
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
    action: str  # create | replace | update | keep | unchanged | blocked
    group: str  # config | templates | wiki | agents | graph | gitignore
    data: bytes | None = None  # the bytes to write, for create, replace and update
    before: bytes | None = None  # the bytes found while planning
    note: str = ""


# Questions -------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Question:
    key: str
    title: str
    what: str
    prompt: str
    default: bool | None  # what Enter means; None: the user must type y or n
    recorded: bool = False  # the answer lives in .oah/config.toml, so it is asked only when init writes that file

    @property
    def flag_yes(self) -> str:
        return f"--with-{self.key.replace('_', '-')}"

    @property
    def flag_no(self) -> str:
        return f"--without-{self.key.replace('_', '-')}"


@dataclasses.dataclass(frozen=True)
class Answer:
    value: bool
    source: str  # flag | asked | config | default


QUESTIONS = (
    Question("agents", "Tier agents", "Writes .claude/agents/oah-low, -medium, -high and -max.md: Claude Code "
             "subagents whose model and effort follow the difficulty tier in .oah/config.toml.",
             "Add the tier agents?", True),
    Question("hooks", "Hooks", "Adds oah's hooks: the Claude Code Stop and SessionStart hooks in "
             ".claude/settings.local.json, and the git pre-commit, post-commit, post-merge and pre-push hooks. "
             "The plan shows each change first.", "Install the hooks?", True),
    Question("unlazy", "unlazy, the gate checker", "Downloads unlazy from GitHub into .agents/skills/unlazy at the "
             "commit oah is tested with, and checks its commit and file digests. Gates need it; it runs on "
             "Node.js.", "Install unlazy?", True),
    Question("graph", "Knowledge graph", "Writes .graphifyignore for graphify (install graphify with "
             "`uv tool install graphifyy`); build the graph later with `oah graph update`.",
             "Write .graphifyignore?", False),
    Question("ci", "CI starter", "Writes .github/workflows/oah-ci.yml: your tests on GitHub Actions plus a secret "
             "scan, shown in full first. Actions minutes on a private repository can cost money.",
             "Write the CI starter?", False),
    Question("orca", "Orca (optional app)", "Orca runs coding agents side by side in isolated git worktrees. "
             "Installs the app from its official source and asks before each setup step.", "Set up Orca?", False),
    Question("aside", "Aside (optional app)", "Aside is a browser with an AI agent that Claude Code and Codex use "
             "for web tasks through MCP. Installs it from its official source and asks before each setup step.",
             "Set up Aside?", False),
    Question("jev", "TypeSafe Jev re-ranker (recommended)", "Jev re-orders the skills and impact results that oah's "
             "fixed rules chose; it never adds or drops one. Cost: TypeSafe may charge for each request. Data sent: "
             "the task kind, the query, and each candidate's name and description (up to 200 characters); for "
             "difficulty advice, the four difficulty axes and the task summary (up to 500 characters). Your key is "
             "read at run time from ${TYPESAFE_API_KEY} or the OS keychain; oah never asks for it or stores it, and "
             "without it Jev stays off. Your answer is recorded in .oah/config.toml.",
             "Turn on Jev? Yes is recommended. Type y or n:", None, recorded=True),
    Question("skill_updates", "Skill updates", "Keeps oah's own skill cache current automatically, checked at most "
             "once a day. Your project's skill folders never change on their own.",
             "Turn on automatic skill updates?", True, recorded=True),
)
ASK_ATTEMPTS = 5


def _ask(question: Question, ask, out) -> bool:
    out("")
    out(f"{question.title} ({question.flag_yes} or {question.flag_no})")
    out(f"  {question.what}")
    choices = "" if question.default is None else " [Y/n]" if question.default else " [y/N]"
    for _attempt in range(ASK_ATTEMPTS):
        reply = ask(f"  {question.prompt}{choices} ")
        if reply is None:
            raise InitError("OAH_INIT_NO_ANSWER", "the input ended before every question had an answer, so nothing "
                            f"was changed. Run oah init again, or answer this one with {question.flag_yes} or "
                            f"{question.flag_no}")
        text = reply.strip().lower()
        if text in ("y", "yes"):
            return True
        if text in ("n", "no"):
            return False
        if not text and question.default is not None:
            return question.default
        out("  Please type y or n.")
    raise InitError("OAH_INIT_NO_ANSWER", f"no y or n for {question.title}, so nothing was changed. Run oah init "
                    f"again, or pass {question.flag_yes} or {question.flag_no}")


def resolve_answers(given: Mapping[str, bool | None], *, recorded: Mapping[str, bool] | None, interactive: bool,
                    dry_run: bool, ask, out) -> dict[str, Answer]:
    """One answer per question, in QUESTIONS order: the flag; for a recorded question, the existing config
    (`recorded`, None when init writes the config); else the user's answer, or in a dry run the default
    (Jev: off). A real run without a terminal refuses with OAH_INIT_ANSWERS_REQUIRED instead of guessing."""
    answers: dict[str, Answer] = {}
    open_questions = []
    for question in QUESTIONS:
        if question.recorded and recorded is not None:
            answers[question.key] = Answer(bool(recorded[question.key]), "config")
        elif given.get(question.key) is not None:
            answers[question.key] = Answer(bool(given[question.key]), "flag")
        else:
            open_questions.append(question)
    if open_questions and not interactive and not dry_run:
        flags = "; ".join(f"{question.flag_yes} or {question.flag_no}" for question in open_questions)
        raise InitError("OAH_INIT_ANSWERS_REQUIRED", "oah init has no terminal to ask in, and these questions have no "
                        f"answer: {', '.join(question.title for question in open_questions)}. Nothing was changed. "
                        f"Run it again with one flag for each: {flags}. `oah init --dry-run` shows the plan first.",
                        EXIT_USAGE)
    for question in open_questions:
        answers[question.key] = (Answer(bool(question.default), "default") if dry_run
                                 else Answer(_ask(question, ask, out), "asked"))
    return {question.key: answers[question.key] for question in QUESTIONS}


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

def _is_link(path: pathlib.Path) -> bool:
    """A symlink, or on Windows a junction or symlink reparse point (os.path.isjunction needs Python 3.12)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in LINK_REPARSE_TAGS


def _linked(root: pathlib.Path, relative: str) -> str | None:
    """The first existing link (symlink or junction) on the way from root to the file, if any."""
    current = root
    for part in pathlib.PurePosixPath(relative).parts:
        current = current / part
        if _is_link(current):
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
            change = FileChange(path, "blocked", group, None, before,
                                f"{link} is a link; oah never writes through links, which could reach files outside "
                                "the project")
        elif before is None:
            change = FileChange(path, "create", group, data)
        elif before == data:
            change = FileChange(path, "unchanged", group, None, before)
        elif force:
            change = FileChange(path, "replace", group, data, before)
        else:
            change = FileChange(path, "keep", group, None, before,
                                KEEP_NOTES.get(path, "exists; pass --force to replace it"))
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
        return FileChange(path, "blocked", "wiki", None, before, f"{link} is a link; oah never writes through links")
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
    if _is_link(root / GITIGNORE):
        return FileChange(GITIGNORE, "blocked", "gitignore", None, before,
                          "it is a link; oah never writes through links")
    try:
        data = add_gitignore_entries(before, unlazy_copy=unlazy_copy)
    except UnicodeDecodeError:
        missing = [entry for _comment, entries in _gitignore_groups(unlazy_copy) for entry in entries]
        return FileChange(GITIGNORE, "keep", "gitignore", None, before,
                          f"it is not UTF-8 text, so add these entries by hand: {', '.join(missing)}")
    if data is None:
        return FileChange(GITIGNORE, "unchanged", "gitignore", None, before)
    what = "gate ledgers, Obsidian's UI state and local secret files stay out of git"
    if before is None:
        return FileChange(GITIGNORE, "create", "gitignore", data, None, what)
    added = len(data.decode("utf-8").splitlines()) - len(before.decode("utf-8").splitlines())
    return FileChange(GITIGNORE, "update", "gitignore", data, before, f"append {added} lines so {what}")


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


# Managed blocks ----------------------------------------------------------------------------------------

class RulesStep(NamedTuple):
    lang: str
    agents: str  # create | update | unchanged
    claude: str  # create | update | unchanged
    block_bytes: int


def plan_rules(root: pathlib.Path, *, lang: str | None) -> RulesStep:
    """What `rules.sync` would do to AGENTS.md and CLAUDE.md; raises rules.RulesError for broken markers."""
    found = rules.plan(pathlib.Path(root), lang=lang)

    def action(before: bytes | None, after: bytes) -> str:
        return "create" if before is None else "unchanged" if before == after else "update"

    return RulesStep(found.lang, action(found.agents_before, found.agents_after),
                     action(found.claude_before, found.claude_after), found.block_bytes)


def add_aside_rules(root: pathlib.Path, *, lang: str) -> str:
    """Put the Aside web rules (A20) into .oah/rules.local.md once, so the rules block carries them.

    Returns "added", "present" or "skipped: <why>". The file keeps its bytes and line ending."""
    root = pathlib.Path(root)
    relative = rules.LOCAL_RULES.as_posix()
    link = _linked(root, relative)
    if link is not None:
        return f"skipped: {link} is a link; oah never writes through links"
    path = root / relative
    before = read_bytes(path)
    text, eol = "", "\n"
    if before is not None:
        try:
            text = before.decode("utf-8")
        except UnicodeDecodeError:
            return f"skipped: {relative} is not UTF-8 text"
        if any(line.rstrip() == companions.RULES_BEGIN for line in text.split("\n")):
            return "present"
        eol = eol_of(before).decode("ascii")
    if text and not text.endswith("\n"):
        text += eol
    if text.strip() and not text.endswith(eol * 2):
        text += eol
    write_text_atomic(path, text + companions.rules_section(True, lang=lang).replace("\n", eol))
    return "added"


# Hooks -------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class HookStep:
    kind: str
    action: str  # install | installed | keep | refused | unavailable
    diff: str = ""
    path: str = ""
    note: str = ""


def _shown_path(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def plan_hooks(root: pathlib.Path, *, force: bool) -> list[HookStep]:
    """Every hook oah offers (PLAN 5.12) with the diff its install would make. Writes nothing."""
    root = pathlib.Path(root)
    steps = []
    for kind in hooks.KINDS:
        if not hooks.hook_available(kind):
            steps.append(HookStep(kind, "unavailable", note=f"this oah version does not provide `oah hook {kind}` yet"))
            continue
        link = _linked(root, CLAUDE_SETTINGS) if kind in hooks.CLAUDE_KINDS else None
        if link is not None:
            steps.append(HookStep(kind, "refused", note=f"{link} is a link; oah never writes through links, which "
                                                        "could reach your global Claude Code settings"))
            continue
        try:
            change = hooks.install(kind, root, dry_run=True, force=force)
        except hooks.HookError as exc:
            if exc.code == "OAH_HOOK_FOREIGN":
                steps.append(HookStep(kind, "keep", note="a hook oah did not write is there; pass --force to replace "
                                                         "it (oah keeps it as a backup and puts it back on "
                                                         f"`oah hooks uninstall {kind}`)"))
            else:
                steps.append(HookStep(kind, "refused", note=str(exc)))
            continue
        action = "installed" if change.action == "unchanged" else "install"
        steps.append(HookStep(kind, action, change.diff, _shown_path(root, change.path)))
    return steps


def install_hooks(root: pathlib.Path, steps: list[HookStep], *, force: bool) -> tuple[list[str], list[str]]:
    """Install the planned hooks; (installed kinds, problems)."""
    root = pathlib.Path(root)
    installed, problems = [], []
    for step in steps:
        if step.action != "install":
            continue
        if step.kind in hooks.CLAUDE_KINDS and _linked(root, CLAUDE_SETTINGS) is not None:
            problems.append(f"OAH_INIT_CHANGED: {CLAUDE_SETTINGS} became a link after the plan was shown; "
                            f"the {step.kind} hook was not installed")
            continue
        try:
            change = hooks.install(step.kind, root, dry_run=False, force=force)
        except hooks.HookError as exc:
            problems.append(f"{exc} (the {step.kind} hook was not installed)")
            continue
        if change.action == "install":
            installed.append(step.kind)
    return installed, problems


# unlazy -------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class UnlazyStep:
    action: str  # present | install | replace | blocked
    folder: str = ""  # the copy discovery found, when there is one
    note: str = ""
    aside: str = ""  # replace: the folder moved aside to <name>.oah.bak first


def plan_unlazy(root: pathlib.Path, *, env: Mapping[str, str], home: pathlib.Path, pin: unlazy.Pin,
                force: bool) -> UnlazyStep:
    """Whether the pinned unlazy (5.8) is in place, and what installing the project copy would do."""
    root = pathlib.Path(root)
    target = root / UNLAZY_PROJECT
    folder, reason = unlazy.find_unlazy_dir(root, env, home)
    override = unlazy.ENV_VAR if env.get(unlazy.ENV_VAR) else None
    short = pin.commit[:12]
    if folder is None:
        if override:
            return UnlazyStep("blocked", note=f"{override} is set but names no unlazy ({reason}). Clear {override}, "
                                              "then run oah init --with-unlazy again")
        if target.exists() or target.is_symlink():
            if force:
                return UnlazyStep("replace", str(target), f"{UNLAZY_PROJECT} is not unlazy; init moves it aside to "
                                                          f"{UNLAZY_PROJECT}.oah.bak first", aside=str(target))
            return UnlazyStep("blocked", str(target), f"{UNLAZY_PROJECT} exists but is not unlazy. Move it away, or "
                                                      "pass --force to move it aside")
        return UnlazyStep("install", note=f"downloads unlazy into {UNLAZY_PROJECT} at commit {short}")
    problems = unlazy.verify_pin(folder, pin)
    if not problems:
        return UnlazyStep("present", str(folder), f"{folder} is the pinned commit {short}, with matching file digests")
    why = "; ".join(problems)
    if override:
        return UnlazyStep("blocked", str(folder), f"{override} names {folder}, which is not the pinned unlazy ({why}). "
                                                  f"Clear {override}, then run oah init --with-unlazy again")
    project = {(root / ".agents" / "skills" / "unlazy").resolve(), (root / ".claude" / "skills" / "unlazy").resolve()}
    if folder.resolve() in project:
        where = _shown_path(root, folder)
        if force:
            return UnlazyStep("replace", str(folder), f"the project copy {where} is not the pinned one ({why}); init "
                                                      f"moves it aside to {where}.oah.bak first", aside=str(folder))
        return UnlazyStep("blocked", str(folder), f"the project copy {where} is not the pinned one ({why}). Pass "
                                                  "--force to move it aside and install the pinned copy")
    return UnlazyStep("install", str(folder), f"your copy at {folder} is not the pinned one ({why}); init installs "
                                              f"the pinned project copy into {UNLAZY_PROJECT}, which oah uses first")


def unlazy_commands(target: pathlib.Path, *, source: str, pin: unlazy.Pin) -> list[list[str]]:
    """unlazy.install_commands with git resolved through oah.paths and the clone source swappable."""
    commands = unlazy.install_commands(target, pin)
    if commands[0][3] == unlazy.UPSTREAM_GIT:
        commands[0][3] = source
    return [[*git_argv(), *argv[1:]] for argv in commands]


def _rename(source: pathlib.Path, target: pathlib.Path) -> None:
    deadline = time.monotonic() + RENAME_RETRY_SECONDS
    while True:
        try:
            os.rename(source, target)
            return
        except PermissionError:  # Windows: an indexer or antivirus holds a file for a moment
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _remove_tree(path: pathlib.Path) -> None:
    def writable(function, name, _error) -> None:
        os.chmod(name, stat.S_IWRITE)  # git marks its object files read-only, which Windows refuses to delete
        function(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def install_unlazy(root: pathlib.Path, step: UnlazyStep, *, source: str = unlazy.UPSTREAM_GIT,
                   pin: unlazy.Pin = unlazy.PIN, timeout: float = CLONE_TIMEOUT) -> str:
    """Clone into a staging folder, check out the pinned commit, verify it, then move it into place.

    Returns "" on success, else the problem; a failure leaves no project copy behind and puts back a
    copy that was moved aside."""
    root = pathlib.Path(root)
    target = root / UNLAZY_PROJECT
    moved: tuple[pathlib.Path, pathlib.Path] | None = None
    staging = target.parent / f".unlazy-{secrets.token_hex(4)}.tmp"
    try:
        if step.action == "replace":
            aside = pathlib.Path(step.aside)
            backup = aside.with_name(aside.name + ".oah.bak")
            if backup.exists() or backup.is_symlink():
                return (f"OAH_INIT_UNLAZY_FAILED: {_shown_path(root, backup)} already exists; move it away so nothing "
                        "is lost, then run oah init --with-unlazy --force again")
            _rename(aside, backup)
            moved = (backup, aside)
        if target.exists() or target.is_symlink():
            return (f"OAH_INIT_UNLAZY_FAILED: {UNLAZY_PROJECT} appeared after the plan was shown; run oah init "
                    "again to see the new plan")
        target.parent.mkdir(parents=True, exist_ok=True)
        for argv in unlazy_commands(staging, source=source, pin=pin):
            result = run_command(argv, timeout=timeout, env={**os.environ, **NETWORK_ENV})
            if result.timed_out or result.returncode != 0:
                lines = result.stderr.strip().splitlines()
                detail = lines[-1] if lines else ("timed out" if result.timed_out else f"exit code {result.returncode}")
                return (f"OAH_INIT_UNLAZY_FAILED: git {argv[len(git_argv())]} failed ({detail}). Check your network "
                        "and git, then run oah init --with-unlazy again")
        problems = unlazy.verify_pin(staging, pin)
        if problems:
            return (f"OAH_INIT_UNLAZY_FAILED: the downloaded copy is not the pinned unlazy ({'; '.join(problems)}); "
                    "nothing was installed")
        _rename(staging, target)
        moved = None
        return ""
    finally:
        if staging.exists():
            _remove_tree(staging)
        if moved is not None and not moved[1].exists():
            _rename(*moved)


# The CI starter ------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class CiStep:
    action: str  # create | replace | keep | unchanged | skipped
    text: str = ""
    diff: str = ""
    note: str = ""
    plan: ci.CiPlan | None = None


def plan_ci(root: pathlib.Path, *, force: bool, oses: list[str], test_command) -> CiStep:
    """The starter workflow (A21) through oah.ci: shown in full, written only on the user's answer."""
    try:
        found = ci.plan(pathlib.Path(root), oses or None, test_command)
    except ci.CiError as exc:
        if exc.code == "OAH_CI_AMBIGUOUS":
            return CiStep("skipped", note="this project matches several kinds of tests; run `oah ci init` to pick one")
        return CiStep("skipped", note=str(exc))
    if found.up_to_date:
        return CiStep("unchanged", found.text, plan=found)
    if found.existing is not None:
        if force:
            return CiStep("replace", found.text, found.diff, plan=found)
        return CiStep("keep", found.text, found.diff, "it exists and differs; run `oah ci init` to review the "
                                                      "difference, or pass --force", found)
    note = "its 'Run tests (edit me)' step fails until you put your test command in it" if found.placeholder else ""
    return CiStep("create", found.text, note=note, plan=found)


def write_ci(step: CiStep) -> str:
    """Write the planned workflow; "" on success, else the problem (the file changed since it was shown)."""
    try:
        ci.write(step.plan, confirm=True, overwrite=step.action == "replace")
    except ci.CiError as exc:
        return str(exc)
    return ""


# The impact map ------------------------------------------------------------------------------------------

def map_state(root: pathlib.Path, env: Mapping[str, str]) -> tuple[str, str]:
    """(missing | stale | fresh | unavailable, reason). Reads only."""
    try:
        info, reason = mapstore.status(pathlib.Path(root), env=env)
    except MAP_FAILURES as exc:
        return "unavailable", str(exc)
    if info is None:
        return "missing", reason
    return info.action, reason


def refresh_map(root: pathlib.Path, env: Mapping[str, str]) -> tuple[str, str]:
    """Build or refresh the persisted worktree map (A11): (built | refreshed | fresh | unavailable, detail)."""
    try:
        info = mapstore.ensure_fresh(pathlib.Path(root), env=env)
    except MAP_FAILURES as exc:
        return "unavailable", str(exc)
    return info.action, f"commit={info.commit[:12]} result={info.result_digest[:12]}"


def origin_url(root: pathlib.Path) -> str | None:
    """The origin remote's URL, used only to name the project for Orca. Never printed or stored."""
    result = run_git(pathlib.Path(root), "remote", "get-url", "origin")
    return (result.stdout.strip() or None) if result.returncode == 0 else None


# The machine --------------------------------------------------------------------------------------------

def _input(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        return None


@dataclasses.dataclass
class Machine:
    """Everything init touches outside the project: tests swap each one for a fake."""
    which: Callable[..., str | None]
    install_runner: Callable[[tuple[str, ...]], int]
    status_runner: Callable[[tuple[str, ...]], tuple[int | None, str]]
    refresh: Callable[[], None]
    companion_runner: Callable[[tuple[str, ...], pathlib.Path], int]
    companion_probe: Callable[[tuple[str, ...], pathlib.Path], int | None]
    ask: Callable[[str], str | None]
    interactive: bool
    env: Mapping[str, str]
    home: pathlib.Path
    os_name: str  # macos | windows: which official install commands the tool catalog offers
    companion_os: str  # macos | windows | linux
    today: dt.date
    unlazy_source: str = unlazy.UPSTREAM_GIT
    unlazy_pin: unlazy.Pin = unlazy.PIN

    @classmethod
    def real(cls) -> "Machine":
        return cls(which=shutil.which, install_runner=guide.run_interactive, status_runner=guide.default_status_runner,
                   refresh=guide.refresh_path, companion_runner=companions.default_runner,
                   companion_probe=companions.default_probe, ask=_input,
                   interactive=sys.stdin is not None and sys.stdin.isatty(), env=os.environ, home=pathlib.Path.home(),
                   os_name=os_name_for(sys.platform), companion_os=companions.os_for(sys.platform),
                   today=dt.date.today())


def _no_answer(prompt: str) -> None:
    """The `ask` of a run without a terminal: every question stays unanswered; stdin is never read."""
    return None


# The plan -----------------------------------------------------------------------------------------------

def recorded_answers(config: Config) -> dict[str, bool]:
    """The Jev and skill-update answers an existing .oah/config.toml records."""
    chosen = config.section("rerank", rerank.DEFAULTS)
    skills = config.section("skills", skillsel.config_defaults())
    return {"jev": chosen["enabled"] is True and chosen["provider"] == "typesafe",
            "skill_updates": skills["auto_update"] is True}


@dataclasses.dataclass(frozen=True)
class Plan:
    root: pathlib.Path
    state: pathlib.Path
    answers: dict[str, Answer]
    notes: tuple[str, ...]
    branch: str
    how: str
    wiki: WikiSettings
    tools: tuple[Tool, ...]
    tool_steps: tuple[guide.Step, ...]
    files: tuple[FileChange, ...]
    rules: RulesStep
    hooks: tuple[HookStep, ...]
    unlazy: UnlazyStep | None
    ci: CiStep | None
    apps: tuple[str, ...]
    companion_steps: tuple[companions.CompanionStep, ...]
    map: tuple[str, str]  # map_state before anything is written
    unlazy_runs: tuple[str, ...] = ()  # the commands an unlazy install runs, as shown

    @property
    def changes(self) -> int:
        """Planned writes and installs; companion steps are checks that act only when something is missing."""
        count = sum(1 for change in self.files if change.action in WRITES)
        count += (self.rules.agents != "unchanged") + (self.rules.claude != "unchanged")
        count += sum(1 for step in self.hooks if step.action == "install")
        count += int(self.unlazy is not None and self.unlazy.action in ("install", "replace"))
        count += int(self.ci is not None and self.ci.action in ("create", "replace"))
        return count + sum(1 for step in self.tool_steps if not step.manual)

    @property
    def map_action(self) -> str:
        state, _reason = self.map
        if state == "unavailable":
            return "unavailable"
        if state == "missing":
            return "build"
        return "refresh" if state == "stale" or self.changes else "fresh"


def build_plan(root: pathlib.Path, answers: dict[str, Answer], *, given: Mapping[str, bool | None], machine: Machine,
               lang: str | None, force: bool, existing: Config | None) -> Plan:
    """Everything init would do, from read-only lookups. `existing` is the project's config, or None when
    --force replaces one that does not parse."""
    root = pathlib.Path(root)
    values = {key: answer.value for key, answer in answers.items()}
    branch, how = default_branch(root)
    config = render_config(branch=branch, how=how, jev=values["jev"], skill_updates=values["skill_updates"],
                           today=machine.today).encode("utf-8")
    has_config = existing is not None and existing.path is not None
    effective = existing if has_config and not force else parse_config(config)
    engines = effective.section("engines", call.ENGINE_DEFAULTS)
    wiki = WikiSettings.from_mapping(effective.section("wiki", WIKI_DEFAULTS))
    ci_section = effective.section("ci", ci.DEFAULTS)
    apps = tuple(app for app in companions.APPS if values[app])
    tools = tuple(tool for tool in catalog() if tool.name != "unlazy" and (not tool.optional or tool.name in apps))
    statuses = guide.check_tools(tools, which=machine.which, env=machine.env, home=machine.home, repo_root=root)
    unlazy_step = (plan_unlazy(root, env=machine.env, home=machine.home, pin=machine.unlazy_pin, force=force)
                   if values["unlazy"] else None)
    unlazy_copy = unlazy_step is not None and (unlazy_step.action in ("install", "replace") or (
        unlazy_step.action == "present"
        and pathlib.Path(unlazy_step.folder).resolve() == (root / UNLAZY_PROJECT).resolve()))
    files = plan_files(root, config=config, wiki_dir=wiki.dir, agents=agent_files(engines) if values["agents"] else {},
                       graph=values["graph"], unlazy_copy=unlazy_copy, force=force, today=machine.today)
    notes_found = []
    for question in QUESTIONS:
        answer, flag = answers[question.key], given.get(question.key)
        if answer.source == "config" and flag is not None and flag != answer.value:
            used = question.flag_yes if flag else question.flag_no
            notes_found.append(f"{used} was not applied: .oah/config.toml already records this answer, and init "
                               "does not edit an existing config. Edit the file, or pass --force to rewrite it.")
    return Plan(
        root=root, state=state_dir(repo_info(root), machine.env), answers=answers, notes=tuple(notes_found),
        branch=branch, how=how, wiki=wiki, tools=tools,
        tool_steps=tuple(guide.plan_installs(statuses, os_name=machine.os_name, which=machine.which)),
        files=tuple(files), rules=plan_rules(root, lang=lang),
        hooks=tuple(plan_hooks(root, force=force)) if values["hooks"] else (), unlazy=unlazy_step,
        ci=plan_ci(root, force=force, oses=list(ci_section["os"]), test_command=ci_section["test_command"] or None)
        if values["ci"] else None,
        apps=apps, companion_steps=tuple(companions.plan_steps(apps, os_name=machine.companion_os, project_root=root,
                                                               remote_url=origin_url(root))) if apps else (),
        map=map_state(root, machine.env),
        unlazy_runs=tuple(" ".join(["git", *argv[len(git_argv()):]]) for argv in unlazy_commands(
            pathlib.Path(UNLAZY_PROJECT), source=machine.unlazy_source, pin=machine.unlazy_pin))
        if unlazy_step is not None and unlazy_step.action in ("install", "replace") else ())


def _indented(text: str, prefix: str) -> list[str]:
    return [f"{prefix}{line}" for line in text.rstrip("\n").split("\n")] if text else []


def render_plan(plan: Plan) -> list[str]:
    """The plan as printed, the same for --dry-run and a real run."""
    lines = [f"Plan for {plan.root}:"]
    names = {question.key: question.key.replace("_", "-") for question in QUESTIONS}
    lines += _wrap("  Answers: ", ", ".join(f"{names[key]} {'yes' if answer.value else 'no'}"
                                            for key, answer in plan.answers.items()))
    sources = []
    for source, label in (("flag", "your flags"), ("asked", "your answers"), ("config", ".oah/config.toml"),
                          ("default", "defaults, which a real run asks about")):
        keys = [names[key] for key, answer in plan.answers.items() if answer.source == source]
        if keys:
            sources.append(f"{label} ({', '.join(keys)})")
    lines += _wrap("  From: ", "; ".join(sources))
    lines += [f"  Note: {note}" for note in plan.notes]
    lines.append("Tools (each installs only after you confirm it, or with --yes; an app you chose above needs no "
                 "second answer):")
    if not plan.tool_steps:
        lines.append("  - every tool oah uses is installed")
    for step in plan.tool_steps:
        how = (f"install it yourself from {step.url}" if step.manual else step.display)
        lines.append(f"  - {step.tool.name} ({step.tool.requirement}: {step.tool.purpose}): {how}")
        if step.note:
            lines.append(f"      {step.note}")
    lines.append("Sign-ins (each tool's own status command; oah never sees a password, key or token):")
    for tool in plan.tools:
        if tool.sign_in is not None:
            status = display((tool.check[0],) + tool.sign_in.status_args)
            lines.append(f"  - {tool.name}: checked with `{status}`; when you are signed out, init shows "
                         f"`{tool.sign_in.login}` to run in your own terminal")
    lines.append("Files:")
    for change in plan.files:
        if change.group != "gitignore":
            lines.append(_file_line(change))
    lines.append("Managed content (changed in place; everything else in these files stays as it is):")
    for name, action in (("AGENTS.md", plan.rules.agents), ("CLAUDE.md", plan.rules.claude)):
        what = (f"the oah rules block ({plan.rules.lang}, {plan.rules.block_bytes} bytes)" if name == "AGENTS.md"
                else "the @AGENTS.md import line")
        lines.append(f"  {action:<10} {name}: {what}")
    lines += [_file_line(change) for change in plan.files if change.group == "gitignore"]
    if "aside" in plan.apps:
        lines.append(f"  if Aside gets registered, its web rules go into {rules.LOCAL_RULES} and the AGENTS.md block")
    lines.append("Hooks:")
    if not plan.answers["hooks"].value:
        lines.append("  none chosen; `oah hooks install <kind>` adds one later")
    for step in plan.hooks:
        where = f" ({step.path})" if step.path else ""
        lines.append(f"  {step.action:<10} {step.kind}{where}{': ' + step.note if step.note else ''}")
        lines += _indented(step.diff, "             | ")
    lines.append("unlazy:")
    if plan.unlazy is None:
        lines.append("  not chosen; gate commands need it (`oah init --with-unlazy` installs it later)")
    else:
        lines.append(f"  {plan.unlazy.action:<10} {plan.unlazy.note}")
        for command in plan.unlazy_runs:
            lines.append(f"             runs: {command}")
        if plan.unlazy_runs:
            lines.append("             (into a staging folder first; it moves into place only after the commit and "
                         "file digests check out)")
    lines.append("CI starter:")
    if plan.ci is None:
        lines.append("  not chosen; `oah ci init` writes it later")
    else:
        lines.append(f"  {plan.ci.action:<10} {ci.WORKFLOW_PATH}{': ' + plan.ci.note if plan.ci.note else ''}")
        if plan.ci.action in ("create", "replace"):
            lines += _indented(plan.ci.diff if plan.ci.action == "replace" else plan.ci.text, "             | ")
    lines.append("Companion apps:")
    if not plan.apps:
        lines.append("  none chosen")
    for number, step in enumerate(plan.companion_steps, 1):
        label = " [global: changes files in your user folder]" if step.global_write else ""
        action = f"runs `{step.display.splitlines()[0]}`" if step.argv is not None else f"you do it: {step.instruction}"
        lines.append(f"  {number}. {step.app}: {step.title}{label}; asks first; {action}")
    jev = plan.answers["jev"].value
    lines.append("Settings:")
    lines.append(f"  default branch: {plan.branch} ({plan.how})")
    lines.append(f"  Jev re-ranker: {'on, with your consent' if jev else 'off'}")
    updates = plan.answers["skill_updates"].value
    lines.append(f"  skill updates: {'on, checked at most once a day' if updates else 'off'}")
    state, reason = plan.map
    action = plan.map_action
    lines.append("Impact map:")
    lines.append({"build": "  build the first map after everything above is written",
                  "refresh": f"  refresh it after everything above is written ({reason})",
                  "fresh": "  fresh; nothing to do",
                  "unavailable": f"  unavailable: {reason} Init still finishes the rest."}[action])
    return lines


def _file_line(change: FileChange) -> str:
    return f"  {change.action:<10} {change.path}{': ' + change.note if change.note else ''}"


def _wrap(prefix: str, text: str, width: int = 110) -> list[str]:
    words, lines, current = text.split(" "), [], prefix
    for word in words:
        if len(current) + len(word) > width and current.strip():
            lines.append(current.rstrip())
            current = " " * len(prefix)
        current += word + " "
    lines.append(current.rstrip())
    return lines


# Applying the plan --------------------------------------------------------------------------------------

@dataclasses.dataclass
class Outcome:
    changed: int = 0
    pending: list[str] = dataclasses.field(default_factory=list)
    impact: str = ""
    impact_detail: str = ""


def _confirm_tool(step: guide.Step, machine: Machine, yes: bool, out) -> bool:
    if step.tool.optional:
        return True  # the user chose this app with its own question
    if yes:
        return True
    if not machine.interactive:
        return False
    out(f"Install {step.tool.name} ({step.tool.requirement}: {step.tool.purpose})?")
    out(f"  This runs: {step.display}")
    out(f"  Official page: {step.url}")
    reply = machine.ask("  Install now? [y/N] ")
    return reply is not None and reply.strip().lower() in ("y", "yes")


def apply_plan(plan: Plan, machine: Machine, *, yes: bool, force: bool, out) -> Outcome:
    """Do the plan in order: tools, sign-ins, files, managed content, hooks, CI, unlazy, companion apps, map."""
    root = plan.root
    outcome = Outcome()
    ask = machine.ask if machine.interactive else _no_answer
    if plan.tool_steps:
        out("")
        out("Tools:")
        results = guide.run_installs(plan.tool_steps, confirm=lambda step: _confirm_tool(step, machine, yes, out),
                                     runner=machine.install_runner, refresh=machine.refresh, out=out)
        outcome.changed += sum(1 for result in results if result.status == "installed")
    statuses = guide.check_tools(plan.tools, which=machine.which, env=machine.env, home=machine.home, repo_root=root)
    out("")
    out("Sign-in:")
    signed_out = guide.walk_sign_ins(statuses, status_runner=machine.status_runner,
                                     interactive=machine.interactive and not yes, ask=ask, out=out)
    outcome.pending += [status.tool.name for status in statuses if status.tool.required and not status.present]
    outcome.pending += [f"{status.tool.name}:sign-in" for status in statuses
                        if status.tool.required and status.tool.name in signed_out]
    written, problems = write_files(root, list(plan.files), wiki=Wiki(root, plan.wiki, plan.state))
    synced = rules.sync(root, lang=plan.rules.lang)
    if written or problems or synced.changed:
        out("")
        out("Files:")
    for change in written:
        out(f"  {VERBS[change.action]} {change.path}")
    for name in synced.changed:
        out(f"  updated {name} (the oah rules block)" if name == "AGENTS.md" else f"  updated {name} (@AGENTS.md)")
    blocked = [change for change in plan.files if change.action == "blocked"]
    if blocked and not (written or problems or synced.changed):
        out("")
        out("Files:")
    for change in blocked:
        out(f"  not written: {change.path}: {change.note}")
    for problem in problems:
        out(f"  {problem}")
    outcome.changed += len(written) + len(synced.changed)
    if problems or blocked:
        outcome.pending.append("files")
    installed, problems = install_hooks(root, list(plan.hooks), force=force)
    problems += [f"hook {step.kind} was not installed: {step.note}" for step in plan.hooks if step.action == "refused"]
    if installed or problems:
        out("")
        out("Hooks:")
    for kind in installed:
        out(f"  installed hook {kind}")
    for problem in problems:
        out(f"  {problem}")
    outcome.changed += len(installed)
    if problems:
        outcome.pending.append("hooks")
    if plan.ci is not None and plan.ci.action in ("create", "replace", "skipped"):
        out("")
        out("CI starter:")
        problem = write_ci(plan.ci) if plan.ci.action != "skipped" else f"not written: {plan.ci.note}"
        out(f"  {problem}" if problem else f"  wrote {ci.WORKFLOW_PATH}")
        if problem:
            outcome.pending.append("ci")
        else:
            outcome.changed += 1
    _apply_unlazy(plan, machine, outcome, out)
    _apply_companions(plan, machine, outcome, yes=yes, ask=ask, out=out)
    outcome.impact, outcome.impact_detail = refresh_map(root, machine.env)
    out("")
    if outcome.impact == "unavailable":
        out(f"OAH_INIT_IMPACT_UNAVAILABLE: {outcome.impact_detail} The rest of init is done; once that is fixed, "
            "build the map with `oah impact build`.")
    else:
        out(f"Impact map: {outcome.impact} ({outcome.impact_detail})")
    return outcome


VERBS = {"create": "created", "replace": "replaced", "update": "updated"}


def _apply_unlazy(plan: Plan, machine: Machine, outcome: Outcome, out) -> None:
    step = plan.unlazy
    if step is None or step.action == "present":
        return
    out("")
    out("unlazy:")
    if step.action == "blocked":
        out(f"  unlazy: not installed: {step.note}")
        outcome.pending.append("unlazy")
        return
    out(f"  installing unlazy into {UNLAZY_PROJECT} from {machine.unlazy_source} ...")
    problem = install_unlazy(plan.root, step, source=machine.unlazy_source, pin=machine.unlazy_pin)
    if problem:
        out(f"  {problem}")
        outcome.pending.append("unlazy")
        return
    out(f"  installed unlazy into {UNLAZY_PROJECT} at commit {machine.unlazy_pin.commit[:12]}, verified")
    outcome.changed += 1


def _apply_companions(plan: Plan, machine: Machine, outcome: Outcome, *, yes: bool, ask, out) -> None:
    if not plan.companion_steps:
        return
    out("")
    out("Companion apps:")
    found = []
    for app in plan.apps:
        outcomes = companions.run_steps([step for step in plan.companion_steps if step.app == app],
                                        project_root=plan.root, yes=yes, which=machine.which, ask=ask, out=out,
                                        runner=machine.companion_runner, probe=machine.companion_probe,
                                        refresh=machine.refresh)
        found += outcomes
        outcome.changed += sum(1 for item in outcomes if item.status == "done")
        if any(item.status not in companions.OK_STATUSES for item in outcomes):
            outcome.pending.append(app)
    if companions.CompanionResult(found).aside_registered:
        status = add_aside_rules(plan.root, lang=plan.rules.lang)
        if status == "added":
            synced = rules.sync(plan.root, lang=plan.rules.lang)
            out(f"  added the Aside web rules to {rules.LOCAL_RULES} and the AGENTS.md block")
            outcome.changed += 1 + len(synced.changed)
        elif status != "present":
            out(f"  the Aside web rules were {status}")
