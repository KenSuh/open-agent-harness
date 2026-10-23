# 브리프용 스킬 선택 — 설치된 스킬 폴더를 읽기만 하고, 작업 종류·단계·위험·Scope 파일 종류에 맞는 스킬을 이유와 함께 고른다.
"""Pick the installed skills a brief needs (A4, A9, A15, C6).

    roots = skillsel.default_roots(repo_root, home=Path.home(), env=os.environ)
    settings = load_config(repo_root).section("skills", skillsel.DEFAULTS)
    selection = skillsel.select("implementation", "initial", "normal", scope, roots=roots,
                                settings=settings, state_dir=state_dir)
    brief += selection.brief_text()

- **Read-only, every call.** Each `select` reads the `SKILL.md` files again, so the next brief uses
  whatever version is installed now. Nothing inside a skill root is ever written.
- **Precedence (A15).** For one skill id, the project copy wins, then the kit cache, then user copies,
  then plugin copies. Within one origin, the earlier root wins.
- **Rules are data.** `DEFAULTS["rules"]` lists which skill fits which task kind, stage, risk and file
  kind; `extra_rules` adds to them, `exclude` removes skills. An empty list in a rule means "any".
- **Never silently empty (A4).** An empty Scope warns `OAH_SCOPE_EMPTY`, and selection by kind, stage
  and risk still runs. A selection with no skills says so, and names the skills that were wanted.
- **Local telemetry (A9).** With `state_dir`, each selection is appended to
  `<state_dir>/skills-telemetry.jsonl`. Nothing is sent anywhere.

For a task, `choose(root, kind=…, stage=…, risk=…, scope=…, state_dir=…, env=…)` does all of it:
- it reads `[skills]` from `root` (`settings_for`), whose keys the toolkit updater shares;
- the roots are the project copies, the published kit-cache copies (`kit_roots`, leaving out items held
  by `[skills] pins`), then the user and plugin copies under the home folder named by `env`;
- `rerank_skills` runs the optional re-ranker. It can only re-order the selection, and it reports
  `used` or `unused(<reason>)`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping, Sequence

from oah.atomic import StateCorrupt, StorageError, append_jsonl
from oah.lockfile import LockTimeout, StateLock

ORIGINS = ("project", "kit", "user", "plugin")
MAX_SKILL_BYTES = 1024 * 1024
WALK_LIMIT = 10000
TELEMETRY_FILE = "skills-telemetry.jsonl"
SKIP_FOLDERS = {"node_modules", "__pycache__"}

_IMPLEMENTING = ["implementation", "refactor", "fix", "migration", "security"]
_REVIEWING = ["review", "adversarial-review", "high-risk-review"]

DEFAULTS = {
    "mode": "reference",  # "reference" names each skill and its path; "attach" adds its text
    "telemetry": True,
    "attach_max_bytes": 65536,
    "exclude": [],
    "extra_rules": [],
    "file_kinds": {
        ".py": "python", ".pyi": "python",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
        ".tsx": "react", ".jsx": "react",
        ".md": "markdown", ".mdx": "markdown",
        ".sql": "sql",
        ".sh": "shell", ".bash": "shell", ".zsh": "shell",
        ".ps1": "powershell", ".psm1": "powershell",
        ".toml": "config", ".json": "config", ".yml": "config", ".yaml": "config",
        ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".swift": "swift",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
        ".rb": "ruby", ".php": "php",
        ".html": "web", ".css": "web", ".scss": "web", ".vue": "web", ".svelte": "web",
    },
    "rules": [
        {"skill": ["test-driven-development", "tdd"], "kinds": _IMPLEMENTING,
         "stages": ["initial", "correction"], "reason": "code changes are written test-first"},
        {"skill": ["systematic-debugging", "diagnosing-bugs"], "kinds": ["fix"],
         "reason": "a fix starts from the root cause"},
        {"skill": ["systematic-debugging", "diagnosing-bugs"], "stages": ["correction"],
         "reason": "each review finding is traced to its root cause before it is fixed"},
        {"skill": ["receiving-code-review"], "stages": ["correction"],
         "reason": "corrections check each review finding before acting on it"},
        {"skill": ["requesting-code-review"], "kinds": _REVIEWING,
         "reason": "review work follows a review checklist"},
        {"skill": ["requesting-code-review"], "stages": ["review"],
         "reason": "review rounds follow a review checklist"},
        {"skill": ["verification-before-completion"], "stages": ["initial", "correction"],
         "reason": "a claim of done needs fresh evidence"},
        {"skill": ["writing-plans"], "kinds": ["implementation", "refactor", "migration", "security"],
         "stages": ["initial"], "risks": ["high", "critical"],
         "reason": "risky work is planned before it is built"},
        {"skill": ["unlazy"], "stages": ["initial", "correction"], "risks": ["high", "critical"],
         "reason": "risky work proves done with gates"},
        {"skill": ["unlazy"], "kinds": ["broad-audit"], "reason": "a broad audit tracks every item with gates"},
        {"skill": ["vercel-react-best-practices", "react-best-practices"], "file_kinds": ["react"],
         "reason": "the scope has React files"},
    ],
}


# Reading skills ----------------------------------------------------------------------------------

def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        inner = text[1:-1]
        out, index = [], 0
        while index < len(inner):
            char = inner[index]
            if char == "\\" and index + 1 < len(inner):
                nxt = inner[index + 1]
                out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)
    return text


def parse_frontmatter(text: str) -> dict[str, str]:
    """The top-level string fields of a `---` YAML frontmatter block; {} when there is none.

    A small subset: plain, quoted and block (`|`, `>`) scalars. Nested tables read as "".
    """
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() in ("---", "..."))
    except StopIteration:
        return {}
    body = lines[1:end]
    result: dict[str, str] = {}
    index = 0
    while index < len(body):
        line = body[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        continuation: list[str] = []
        while index < len(body) and (not body[index].strip() or body[index][0] in " \t"):
            continuation.append(body[index])
            index += 1
        while continuation and not continuation[-1].strip():
            continuation.pop()
        if value[:1] in ("|", ">"):
            stripped = [item.strip() for item in continuation]
            result[key] = "\n".join(stripped) if value[0] == "|" else " ".join(item for item in stripped if item)
        elif not value:
            result[key] = ""  # a nested table or an empty value
        else:
            parts = [value] + [item.strip() for item in continuation if item.strip()]
            result[key] = _scalar(" ".join(parts))
    return result


@dataclass(frozen=True)
class SkillRoot:
    """A folder of `<skill>/SKILL.md` folders. `label` is how briefs show it; plugin skills get a prefix."""

    path: pathlib.Path
    origin: str
    label: str
    plugin: str = ""
    only: tuple[str, ...] = ()  # when set, only these skill folders are read


@dataclass(frozen=True)
class InstalledSkill:
    id: str
    name: str
    description: str
    path: pathlib.Path
    label: str
    origin: str
    digest: str
    text: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class InstalledSet:
    skills: Mapping[str, InstalledSkill]
    shadowed: Mapping[str, tuple[InstalledSkill, ...]]
    issues: tuple[str, ...]


def _read_skill(root: SkillRoot, folder: pathlib.Path) -> InstalledSkill | str | None:
    path = folder / "SKILL.md"
    label = f"{root.label}/{folder.name}/SKILL.md"
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_SKILL_BYTES:
            return f"OAH_SKILL_UNREADABLE: {label} is larger than 1 MiB; it was skipped"
        data = path.read_bytes()
    except OSError as exc:
        return f"OAH_SKILL_UNREADABLE: {label} cannot be read ({exc.strerror or exc}); it was skipped"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"OAH_SKILL_UNREADABLE: {label} is not UTF-8; save it as UTF-8 to use it"
    meta = parse_frontmatter(text)
    name = (meta.get("name") or folder.name).strip()
    skill_id = f"{root.plugin}:{name}" if root.plugin else name
    return InstalledSkill(id=skill_id, name=name, description=meta.get("description", ""), path=path,
                          label=label, origin=root.origin, digest=hashlib.sha256(data).hexdigest()[:16],
                          text=text)


def installed(roots: Sequence[SkillRoot]) -> InstalledSet:
    """Every readable skill under `roots`; for each id the highest-precedence copy wins."""
    found: list[tuple[tuple[int, int], InstalledSkill]] = []
    issues: list[str] = []
    for position, root in enumerate(roots):
        rank = ORIGINS.index(root.origin) if root.origin in ORIGINS else len(ORIGINS)
        try:
            folders = sorted((item for item in pathlib.Path(root.path).iterdir()
                              if not item.name.startswith(".") and item.is_dir()
                              and (not root.only or item.name in root.only)),
                             key=lambda item: item.name.casefold())
        except OSError:
            continue  # a root that does not exist is normal
        for folder in folders:
            skill = _read_skill(root, folder)
            if isinstance(skill, str):
                issues.append(skill)
            elif skill is not None:
                found.append(((rank, position), skill))
    found.sort(key=lambda item: item[0])
    winners: dict[str, InstalledSkill] = {}
    shadowed: dict[str, list[InstalledSkill]] = {}
    keys: dict[str, str] = {}
    for _, skill in found:
        folded = skill.id.casefold()
        if folded in keys:
            shadowed.setdefault(keys[folded], []).append(skill)
        else:
            keys[folded] = skill.id
            winners[skill.id] = skill
    return InstalledSet(winners, {key: tuple(value) for key, value in shadowed.items()}, tuple(issues))


def _label(path: pathlib.Path, home: pathlib.Path, repo_root: pathlib.Path) -> str:
    for base, prefix in ((repo_root, ""), (home, "~/")):
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        return prefix + relative.as_posix()
    return str(path)


def _plugin_roots(claude_home: pathlib.Path, home: pathlib.Path,
                  repo_root: pathlib.Path) -> list[SkillRoot]:
    manifest = claude_home / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return []
    roots = []
    for key, entries in plugins.items():
        if not isinstance(key, str) or not isinstance(entries, list):
            continue
        plugin = key.partition("@")[0]
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("installPath"), str):
                continue
            scope = entry.get("scope")
            if scope != "user":
                project = entry.get("projectPath")
                if not isinstance(project, str) or os.path.normcase(os.path.abspath(project)) != \
                        os.path.normcase(os.path.abspath(repo_root)):
                    continue
            folder = pathlib.Path(entry["installPath"]) / "skills"
            roots.append(SkillRoot(folder, "plugin", _label(folder, home, repo_root), plugin=plugin))
    return roots


def kit_roots(state_dir: pathlib.Path, *, pins: Iterable[str] = (), catalog=None) -> list[SkillRoot]:
    """The kit-cache skills A15 may select: published items of the shipped catalog, minus pinned items.

    An item counts as a validated update only when its upstream record exists; the automatic check writes
    that record last, after license, load and privacy checks. Items named in `[skills] pins` are left out,
    so the user's own copy stays in use.
    """
    from oah.toolkit.catalog import CatalogError, item_skill_dirs, load_catalog
    from oah.toolkit.skillmeta import UPSTREAM_FILE
    from oah.toolkit.update import cache_dir

    try:
        catalog = catalog if catalog is not None else load_catalog()
    except CatalogError:
        return []
    held = {str(name).casefold() for name in pins}
    roots: list[SkillRoot] = []
    for entry in catalog.bundled:
        item = cache_dir(state_dir) / entry.name
        if entry.name.casefold() in held or not (item / UPSTREAM_FILE).is_file():
            continue
        groups: dict[pathlib.Path, list[str]] = {}
        for folder in item_skill_dirs(item, entry):
            groups.setdefault(folder.parent, []).append(folder.name)
        for parent, names in groups.items():
            folder = parent.absolute()
            roots.append(SkillRoot(folder, "kit", folder.as_posix(), only=tuple(names)))
    return roots


def default_roots(repo_root: pathlib.Path, *, home: pathlib.Path, env: Mapping[str, str],
                  kit_cache: pathlib.Path | Sequence[SkillRoot] | None = None) -> list[SkillRoot]:
    """The usual skill folders for this repository and user, in precedence order.

    `kit_cache` is one folder of skill folders, or the roots `kit_roots` returns."""
    repo_root, home = pathlib.Path(repo_root), pathlib.Path(home)
    claude_home = pathlib.Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else home / ".claude"
    codex_home = pathlib.Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"
    roots = [
        SkillRoot(repo_root / ".claude" / "skills", "project", ".claude/skills"),
        SkillRoot(repo_root / ".agents" / "skills", "project", ".agents/skills"),
    ]
    if isinstance(kit_cache, (list, tuple)):
        roots.extend(kit_cache)
    elif kit_cache is not None:
        folder = pathlib.Path(kit_cache).absolute()
        roots.append(SkillRoot(folder, "kit", folder.as_posix()))
    for folder in (claude_home / "skills", codex_home / "skills", home / ".agents" / "skills"):
        roots.append(SkillRoot(folder, "user", _label(folder, home, repo_root)))
    return roots + _plugin_roots(claude_home, home, repo_root)


# Selecting ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectedSkill:
    id: str
    name: str
    path: pathlib.Path
    label: str
    origin: str
    digest: str
    reasons: tuple[str, ...]
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Unavailable:
    skill: str
    reason: str


@dataclass(frozen=True)
class Selection:
    kind: str
    stage: str
    risk: str
    scope_count: int
    file_kinds: tuple[str, ...]
    mode: str
    skills: tuple[SelectedSkill, ...]
    unavailable: tuple[Unavailable, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]

    def brief_text(self, rerank: str | None = None) -> str:
        lines = ["## Skills", "",
                 f"Selected by rules for kind `{self.kind}`, stage `{self.stage}` and risk `{self.risk}`"
                 + (f", file kinds {', '.join(self.file_kinds)}" if self.file_kinds else "")
                 + ", from the skills installed on this machine. Other installed skills stay available "
                   "when the work calls for them.", ""]
        if rerank is not None:
            lines += [f"Skill order: rerank={rerank.removeprefix('rerank=')}", ""]
        if self.skills:
            for skill in self.skills:
                lines.append(f"- `{skill.id}` ({skill.origin}): `{skill.label}`")
                for reason in skill.reasons:
                    lines.append(f"  - why: {reason}")
        else:
            lines.append("No installed skill was selected for this task.")
        if self.unavailable:
            lines.append("")
            lines.append("Wanted but not used:")
            for item in self.unavailable:
                lines.append(f"- `{item.skill}`: {item.reason}")
        for title, items in (("Warnings", self.warnings), ("Notes", self.notes)):
            if items:
                lines.append("")
                lines.append(f"{title}:")
                lines.extend(f"- {item}" for item in items)
        for skill in self.skills:
            if skill.text is not None:
                lines += ["", f"### Skill `{skill.id}`", "", skill.text.rstrip("\n")]
        return "\n".join(lines) + "\n"

    def to_record(self) -> dict:
        return {
            "kind": self.kind, "stage": self.stage, "risk": self.risk, "scope_count": self.scope_count,
            "file_kinds": list(self.file_kinds), "mode": self.mode,
            "skills": [{"id": skill.id, "origin": skill.origin, "digest": skill.digest,
                        "reasons": list(skill.reasons)} for skill in self.skills],
            "unavailable": [item.skill for item in self.unavailable],
            "warnings": [warning.partition(":")[0] for warning in self.warnings],
        }


def _norm_path(entry: str) -> str:
    return entry.replace("\\", "/").rstrip("/")


def file_kinds(scope_paths: Iterable[str], table: Mapping[str, str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(the file kinds named by Scope's extensions, the entries that are folders or `repo`)."""
    kinds: set[str] = set()
    folders: list[str] = []
    lowered = {str(key).lower(): value for key, value in table.items()}
    for entry in scope_paths:
        path = _norm_path(entry)
        suffix = pathlib.PurePosixPath(path).suffix.lower()
        if not suffix or path == "repo":
            folders.append(entry)
        elif suffix in lowered:
            kinds.add(lowered[suffix])
    return tuple(sorted(kinds)), tuple(folders)


def _walk_kinds(repo_root: pathlib.Path, folders: Sequence[str], table: Mapping[str, str]) -> tuple[set[str], bool]:
    """File kinds found under the folder entries, read-only and bounded; (kinds, stopped early)."""
    lowered = {str(key).lower(): value for key, value in table.items()}
    kinds: set[str] = set()
    seen = 0
    for entry in folders:
        path = _norm_path(entry)
        start = repo_root if path in ("repo", ".", "") else repo_root / pathlib.PurePosixPath(path)
        for current, dirs, files in os.walk(start):
            dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name not in SKIP_FOLDERS)
            for name in files:
                seen += 1
                if seen > WALK_LIMIT:
                    return kinds, True
                kind = lowered.get(pathlib.PurePosixPath(name).suffix.lower())
                if kind:
                    kinds.add(kind)
    return kinds, False


@dataclass(frozen=True)
class _Rule:
    skills: tuple[str, ...]
    kinds: tuple[str, ...]
    stages: tuple[str, ...]
    risks: tuple[str, ...]
    file_kinds: tuple[str, ...]
    reason: str


def _rule(raw: object) -> _Rule | None:
    if not isinstance(raw, Mapping):
        return None
    skill = raw.get("skill")
    skills = (skill,) if isinstance(skill, str) else skill
    if not isinstance(skills, (list, tuple)) or not skills or not all(isinstance(s, str) and s for s in skills):
        return None
    lists = {}
    for name in ("kinds", "stages", "risks", "file_kinds"):
        value = raw.get(name, [])
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
            return None
        lists[name] = tuple(item.lower() for item in value)
    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        return None
    unknown = set(raw) - {"skill", "kinds", "stages", "risks", "file_kinds", "reason"}
    if unknown:
        return None
    return _Rule(tuple(skills), lists["kinds"], lists["stages"], lists["risks"], lists["file_kinds"],
                 reason.strip() or "selected by a rule")


def _matches(rule: _Rule, kind: str, stage: str, risk: str, kinds: set[str]) -> str | None:
    """The matched criteria as text, or None when the rule does not apply."""
    matched = []
    for values, actual, word in ((rule.kinds, kind, "kind"), (rule.stages, stage, "stage"),
                                 (rule.risks, risk, "risk")):
        if values:
            if actual not in values:
                return None
            matched.append(f"{word} {actual}")
    if rule.file_kinds:
        hit = sorted(kinds.intersection(rule.file_kinds))
        if not hit:
            return None
        matched.append(f"file kinds {', '.join(hit)}")
    return ", ".join(matched) or "always"


def _find(found: InstalledSet, wanted: str) -> InstalledSkill | None:
    target = wanted.casefold()
    for skill in found.skills.values():  # already in precedence order
        if skill.id.casefold() == target or skill.id.casefold().endswith(":" + target):
            return skill
    return None


def _now_iso(now: dt.datetime | None) -> str:
    moment = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def select(kind: str, stage: str, risk: str, scope_paths: Sequence[str], *, roots: Sequence[SkillRoot],
           settings: Mapping | None = None, state_dir: pathlib.Path | None = None,
           repo_root: pathlib.Path | None = None, now: dt.datetime | None = None) -> Selection:
    """The skills that fit this task, with a reason each; see the module docstring."""
    settings = DEFAULTS if settings is None else settings
    option = lambda name: settings.get(name, DEFAULTS[name])  # noqa: E731 - a short local lookup
    kind, stage, risk = kind.lower(), stage.lower(), risk.lower()
    scope = list(scope_paths)
    warnings: list[str] = []
    notes: list[str] = []
    if not scope:
        warnings.append("OAH_SCOPE_EMPTY: the task's Scope lists no paths, so no skill was chosen by file "
                        "kind; add the paths the task may change to its Scope")
    table = option("file_kinds")
    kinds, folders = file_kinds(scope, table)
    kind_set = set(kinds)
    if folders and repo_root is not None:
        walked, stopped = _walk_kinds(pathlib.Path(repo_root), folders, table)
        kind_set |= walked
        if stopped:
            notes.append(f"Scope folders hold more than {WALK_LIMIT} files; file kinds were read from the "
                         "first ones only")
    elif folders:
        notes.append(f"{len(folders)} Scope entr{'y is a folder' if len(folders) == 1 else 'ies are folders'}; "
                     "skills chosen by file kind looked only at the listed files")
    found = installed(roots)
    warnings.extend(found.issues)

    rules: list[_Rule] = []
    for group in ("rules", "extra_rules"):
        raw_rules = option(group)
        for index, raw in enumerate(raw_rules if isinstance(raw_rules, (list, tuple)) else []):
            rule = _rule(raw)
            if rule is None:
                warnings.append(f"OAH_SKILLS_RULE_INVALID: [skills] {group}[{index}] needs skill (a name or "
                                "list of names), optional kinds/stages/risks/file_kinds lists and a reason; "
                                "it was skipped")
            else:
                rules.append(rule)
    excluded = {str(item).casefold() for item in option("exclude")}

    chosen: dict[str, tuple[InstalledSkill, list[str]]] = {}
    unavailable: dict[str, str] = {}
    for rule in rules:
        criteria = _matches(rule, kind, stage, risk, kind_set)
        if criteria is None:
            continue
        reason = f"{rule.reason} ({criteria})"
        skill = next((hit for hit in (_find(found, name) for name in rule.skills) if hit), None)
        if skill is None:
            unavailable.setdefault(rule.skills[0], f"not installed ({reason})")
            continue
        if skill.id.casefold() in excluded or skill.name.casefold() in excluded:
            unavailable.setdefault(skill.id, "excluded by [skills] exclude")
            continue
        entry = chosen.setdefault(skill.id, (skill, []))
        if reason not in entry[1]:
            entry[1].append(reason)
    for name in [name for name in unavailable if name in chosen]:
        del unavailable[name]

    mode = option("mode") if option("mode") in ("reference", "attach") else "reference"
    cap = option("attach_max_bytes")
    selected = []
    for skill, reasons in chosen.values():
        text = None
        if mode == "attach":
            size = len(skill.text.encode("utf-8"))
            if size <= cap:
                text = skill.text
            else:
                notes.append(f"`{skill.id}` is too long to attach ({size} bytes, limit {cap}); read it at "
                             f"`{skill.label}`")
        selected.append(SelectedSkill(skill.id, skill.name, skill.path, skill.label, skill.origin, skill.digest,
                                      tuple(reasons), text))
    if not selected:
        warnings.append(f"OAH_SKILLS_NONE_SELECTED: no installed skill matched kind {kind}, stage {stage}, "
                        f"risk {risk}; install the skills listed as not installed, or add rules under [skills]")
    selection = Selection(kind, stage, risk, len(scope), tuple(sorted(kind_set)), mode, tuple(selected),
                          tuple(Unavailable(name, why) for name, why in unavailable.items()),
                          tuple(warnings), tuple(notes))
    if state_dir is not None and option("telemetry") is True:
        selection = _record(selection, pathlib.Path(state_dir), now)
    return selection


def _record(selection: Selection, state_dir: pathlib.Path, now: dt.datetime | None) -> Selection:
    """Append the selection to the local telemetry file; a failure becomes a warning, never an error."""
    record = {"at": _now_iso(now), **selection.to_record()}
    try:
        with StateLock(state_dir / ".lock"):
            append_jsonl(state_dir / TELEMETRY_FILE, record)
    except (StateCorrupt, StorageError, LockTimeout, OSError) as exc:
        code = getattr(exc, "code", "OAH_STATE_IO")
        warning = (f"OAH_SKILLS_TELEMETRY_FAILED: the local selection log was not updated ({code}); "
                   f"selection still worked. Repair or remove {state_dir / TELEMETRY_FILE}")
        return replace(selection, warnings=selection.warnings + (warning,))
    return selection


# One task ----------------------------------------------------------------------------------------

def config_defaults() -> dict:
    """The `[skills]` defaults of the selector and of the toolkit updater, which share that section."""
    from oah.toolkit.update import DEFAULTS as UPDATE_DEFAULTS

    return {**DEFAULTS, **UPDATE_DEFAULTS}


def settings_for(root: pathlib.Path) -> dict:
    """`[skills]` of `root`'s .oah/config.toml over both default sets, so neither side's keys are unknown."""
    from oah.config import load_config

    return load_config(pathlib.Path(root)).section("skills", config_defaults())


def home_of(env: Mapping[str, str]) -> pathlib.Path:
    """The home folder named by `env` (USERPROFILE on Windows, HOME elsewhere), else the process's."""
    value = env.get("USERPROFILE" if os.name == "nt" else "HOME")
    return pathlib.Path(value) if value else pathlib.Path.home()


def rerank_skills(selection: Selection, *, config: Mapping, query: str, brief_disabled: bool = False,
                  env: Mapping[str, str] | None = None, keychain=None, providers=None) -> tuple[Selection, str]:
    """The selection in the re-ranker's order, and `used` or `unused(<reason>)`.

    The re-ranker may only re-order: a result that is not the same set of skills keeps the rule order and
    reports `unused(error:dropped)`. Only skill ids and their reasons are sent to an enabled provider."""
    from oah import rerank

    candidates = [rerank.Candidate(skill.id, "; ".join(skill.reasons)) for skill in selection.skills]
    try:
        result = rerank.rerank("skills", candidates, config=config, query=query, brief_disabled=brief_disabled,
                               env=env, keychain=keychain, providers=providers)
    except rerank.ProviderError as exc:
        return selection, f"unused(error:{exc.code})"
    names = [candidate.name for candidate in result.order]
    if sorted(names) != sorted(skill.id for skill in selection.skills):
        return selection, "unused(error:dropped)"
    if not result.used:
        return selection, result.status
    by_id = {skill.id: skill for skill in selection.skills}
    return replace(selection, skills=tuple(by_id[name] for name in names)), result.status


def choose(root: pathlib.Path, *, kind: str, stage: str, risk: str, scope: Sequence[str],
           state_dir: pathlib.Path | None, env: Mapping[str, str], brief_disabled: bool = False,
           keychain=None, providers=None, now: dt.datetime | None = None) -> tuple[Selection, str]:
    """Select and re-rank the skills for one task in the repository `root` (see the module docstring)."""
    from oah import rerank
    from oah.config import load_config

    root = pathlib.Path(root)
    settings = settings_for(root)
    kit = kit_roots(state_dir, pins=settings["pins"]) if state_dir is not None else None
    roots = default_roots(root, home=home_of(env), env=env, kit_cache=kit)
    selection = select(kind, stage, risk, scope, roots=roots, settings=settings, state_dir=state_dir,
                       repo_root=root, now=now)
    config = load_config(root).section("rerank", rerank.DEFAULTS)
    return rerank_skills(selection, config=config, query=f"kind={kind} stage={stage} risk={risk}",
                         brief_disabled=brief_disabled, env=env, keychain=keychain, providers=providers)
