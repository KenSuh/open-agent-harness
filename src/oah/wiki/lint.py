# 위키 점검 — 실제 문제만 센다(깨진 링크·미색인·출처·머리말·고아·중복 제목·오래됨), index.md 점검과 hygiene 증감 줄을 만든다. 위키 파일은 절대 쓰지 않는다.
"""Read-only wiki scans. Results are candidates; nothing here deletes, merges or moves a note."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
from dataclasses import asdict, dataclass, field

from oah.atomic import read_json, write_json_atomic
from oah.wiki import log as log_module
from oah.wiki.base import EXIT_ENVIRONMENT, WikiError, WikiSettings
from oah.wiki.markdown import FenceTracker, Link, strip_eol, wikilinks
from oah.wiki.notes import frontmatter_span, parse_frontmatter

KINDS = ("broken_link", "unindexed", "missing_sources", "missing_frontmatter", "orphan", "duplicate_title",
         "stale", "banner", "overlong_line")
COUNTERS = {
    "broken_links": "broken_link", "unindexed": "unindexed", "missing_sources": "missing_sources",
    "missing_frontmatter": "missing_frontmatter", "orphans": "orphan", "duplicate_titles": "duplicate_title",
    "stale": "stale", "banners": "banner", "overlong_lines": "overlong_line",
}
_INDEX_ARCHIVE = re.compile(r"^index_archive_[^/]*\.md$")
_BANNERS = (re.compile(r"^#{1,6} +.*$"), re.compile(r"^(?:[-=*_] *){3,}$"))
_NOT_NOTES = ("templates", "raw")

HINTS = {
    "broken_link": "create the note, fix the link, or add the target's folder to [wiki] link_roots",
    "unindexed": 'add a line for it to index.md (oah wiki new does this for new notes)',
    "missing_sources": "list a file path with a commit, a URL or a packet under sources:",
    "missing_frontmatter": "start the note with --- title/date/sources/tags --- frontmatter",
    "orphan": "link to it from a related note or from index.md",
    "duplicate_title": "give each note a distinct title",
    "stale": "review the note and commit an update, or record that it is still accurate",
    "banner": "replace the heading or rule with a plain category label such as 'Notes:'",
    "overlong_line": "shorten the line",
}


@dataclass(frozen=True)
class Problem:
    kind: str
    path: str
    line: int
    detail: str


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    notes: int = 0

    def counts(self) -> dict[str, int]:
        return {counter: sum(1 for p in self.problems if p.kind == kind) for counter, kind in COUNTERS.items()}

    def summary(self) -> str:
        if not self.problems:
            return f"OAH_WIKI_LINT_CLEAN notes={self.notes}"
        counts = " ".join(f"{name}={count}" for name, count in self.counts().items() if count)
        return f"OAH_WIKI_LINT problems={len(self.problems)} {counts}"

    def to_json(self) -> dict:
        return {"notes": self.notes, "counts": self.counts(), "problems": [asdict(p) for p in self.problems]}


def markdown_files(folder: pathlib.Path) -> list[pathlib.Path]:
    return [path for path in _files(folder) if path.suffix.lower() == ".md"]


def _files(folder: pathlib.Path) -> list[pathlib.Path]:
    found = []
    for current, dirs, files in os.walk(folder):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        found.extend(pathlib.Path(current) / name for name in sorted(files) if not name.startswith("."))
    return found


class Names:
    """Obsidian-style link resolution: by name anywhere, or by a trailing path, ignoring case and `.md`.

    The first root is the wiki (tags are wiki-relative paths); later roots are [wiki] link_roots.
    """

    def __init__(self, roots: list[pathlib.Path]) -> None:
        self.by_name: dict[str, list[tuple[str, str]]] = {}
        for index, root in enumerate(roots):
            self.add_folder(root, "" if index == 0 else f"{root}:")

    def add_folder(self, root: pathlib.Path, tag_prefix: str) -> None:
        if root.is_dir():
            self.add([path.relative_to(root).as_posix() for path in _files(root)], tag_prefix)

    def add(self, rels: list[str], tag_prefix: str = "") -> None:
        for rel in rels:
            key = rel[:-3] if rel.lower().endswith(".md") else rel
            self.by_name.setdefault(key.rsplit("/", 1)[-1].casefold(), []).append((key.casefold(), tag_prefix + rel))

    def resolve(self, target: str) -> list[str]:
        wanted = target.replace("\\", "/").lstrip("/").removeprefix("./")
        found = []
        for candidate in (wanted, wanted[:-3] if wanted.lower().endswith(".md") else None):
            if not candidate:
                continue
            key = candidate.casefold()
            for full, tag in self.by_name.get(key.rsplit("/", 1)[-1], []):
                if full == key or full.endswith("/" + key):
                    found.append(tag)
        return found


@dataclass
class _Page:
    rel: str
    text: str
    links: list[Link]
    note: bool


def links_checked(rel: str, naming: log_module.ArchiveNaming = log_module.DEFAULT_NAMING) -> bool:
    """Whether lint counts broken links in this wiki page: not raw/, templates/ or frozen archives."""
    return (rel.lower().endswith(".md") and rel.split("/", 1)[0] not in _NOT_NOTES
            and naming.number(rel) is None)


def _is_system(rel: str, naming: log_module.ArchiveNaming) -> bool:
    return (rel in ("index.md", "log.md") or bool(_INDEX_ARCHIVE.match(rel))
            or naming.number(rel) is not None)


def _pages(wiki: pathlib.Path, naming: log_module.ArchiveNaming) -> list[_Page]:
    pages = []
    for path in markdown_files(wiki):
        rel = path.relative_to(wiki).as_posix()
        if rel.split("/", 1)[0] in _NOT_NOTES:
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        pages.append(_Page(rel, text, wikilinks(text), not _is_system(rel, naming)))
    return pages


def link_root_paths(root: pathlib.Path, settings: WikiSettings) -> list[pathlib.Path]:
    out = []
    for item in settings.link_roots:
        path = pathlib.Path(os.path.expanduser(item))
        out.append(path if path.is_absolute() else root / path)
    return out


def _archive_index_links(text: str) -> list[Link]:
    """Links inside the generated archive-index table of log.md."""
    lines = text.split("\n")
    fences = FenceTracker()
    inside, kept = False, []
    for line in lines:
        fenced = fences.feed(line)
        body = strip_eol(line).rstrip()
        if not fenced and body == log_module.INDEX_BEGIN:
            inside = True
        elif not fenced and body == log_module.INDEX_END:
            inside = False
        kept.append(line if inside else "")
    return wikilinks("\n".join(kept))


def commit_times(root: pathlib.Path, wiki: pathlib.Path) -> dict[str, int]:
    """Wiki-relative path → committer time of the last commit that touched it."""
    from oah.paths import run_git

    if run_git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        return {}
    try:
        prefix = wiki.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return {}
    result = run_git(root, "log", "-z", "--no-renames", "--name-only", "--format=%x01%ct", "--",
                     prefix or ".")
    if result.returncode != 0:
        raise WikiError("OAH_GIT_FAILED", f"git log failed in {root}: {result.stderr.strip()}. "
                        "Check the repository, then retry.", EXIT_ENVIRONMENT)
    times: dict[str, int] = {}
    current = 0
    for token in result.stdout.split("\0"):
        token = token.lstrip("\n")
        if token.startswith("\x01"):
            current = int(token[1:].strip() or 0)
        elif token:
            rel = token[len(prefix) + 1:] if prefix and prefix != "." else token
            times.setdefault(rel, current)
    return times


def scan(root: pathlib.Path, wiki: pathlib.Path, settings: WikiSettings, *, now: float) -> Report:
    """The normal lint: real problems only, sorted by path and line."""
    root, wiki = pathlib.Path(root), pathlib.Path(wiki)
    pages = _pages(wiki, settings.naming)
    names = Names([wiki, *link_root_paths(root, settings)])
    problems: list[Problem] = []
    inbound: dict[str, set[str]] = {}
    for page in pages:
        if settings.naming.number(page.rel) is not None:
            continue  # archives are frozen history; their links cannot be fixed
        for link in page.links:
            resolved = names.resolve(link.target)
            if not resolved:
                problems.append(Problem("broken_link", page.rel, link.line, f"{link.raw} does not resolve"))
            for tag in resolved:
                if tag != page.rel:
                    inbound.setdefault(tag, set()).add(page.rel)
    indexed = _indexed(pages, names)
    times = commit_times(root, wiki)
    titles: dict[str, list[str]] = {}
    notes = [page for page in pages if page.note]
    for page in notes:
        meta = parse_frontmatter(page.text)
        if meta is None:
            problems.append(Problem("missing_frontmatter", page.rel, 1, "no closed, readable --- frontmatter block"))
        else:
            absent = [key for key in ("title", "date") if not str(meta.get(key) or "").strip()]
            if absent:
                problems.append(Problem("missing_frontmatter", page.rel, 1, f"frontmatter lacks {', '.join(absent)}"))
            sources = meta.get("sources")
            items = sources if isinstance(sources, list) else [sources or ""]
            if not any(str(item).strip() for item in items):
                problems.append(Problem("missing_sources", page.rel, 1, "no entry under sources:"))
        if page.rel not in indexed:
            problems.append(Problem("unindexed", page.rel, 1, "index.md does not link to it"))
        if not inbound.get(page.rel):
            problems.append(Problem("orphan", page.rel, 1, "no other page links to it"))
        title = str((meta or {}).get("title") or pathlib.PurePosixPath(page.rel).stem).strip().casefold()
        titles.setdefault(title, []).append(page.rel)
        committed = times.get(page.rel)
        if committed and now - committed > settings.stale_days * 86400:
            day = datetime.datetime.fromtimestamp(committed, datetime.timezone.utc).date().isoformat()
            problems.append(Problem("stale", page.rel, 1, f"last commit {day}, over {settings.stale_days} days ago"))
    for title, paths in titles.items():
        if len(paths) > 1:
            for rel in paths:
                others = ", ".join(other for other in paths if other != rel)
                problems.append(Problem("duplicate_title", rel, 1, f"same title as {others}"))
    problems.sort(key=lambda p: (p.path, p.line, KINDS.index(p.kind), p.detail))
    return Report(problems, len(notes))


def _indexed(pages: list[_Page], names: Names) -> set[str]:
    indexed: set[str] = set()
    for page in pages:
        if page.rel == "index.md" or _INDEX_ARCHIVE.match(page.rel):
            links = page.links
        elif page.rel == "log.md":
            links = _archive_index_links(page.text)
        else:
            continue
        for link in links:
            indexed.update(names.resolve(link.target))
    return indexed


def scan_index(root: pathlib.Path, wiki: pathlib.Path, settings: WikiSettings, *, now: float) -> Report:
    """`--index`: banners and over-long lines in index.md, plus truly unindexed notes."""
    path = pathlib.Path(wiki) / "index.md"
    problems: list[Problem] = []
    if path.is_file():
        text = path.read_bytes().decode("utf-8", errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        span = frontmatter_span(lines)
        skip = span[1] + 1 if span else 0
        fences = FenceTracker()
        for number, line in enumerate(lines, start=1):
            body = strip_eol(line)
            if len(body) > settings.index_line_limit:
                problems.append(Problem("overlong_line", "index.md", number,
                                        f"{len(body)} characters, over the {settings.index_line_limit} limit"))
            if number <= skip or fences.feed(line):
                continue
            trimmed = body.strip()
            if any(pattern.match(trimmed) for pattern in _BANNERS):
                problems.append(Problem("banner", "index.md", number, f"banner line {trimmed[:60]!r}"))
    normal = scan(root, wiki, settings, now=now)
    problems.extend(p for p in normal.problems if p.kind == "unindexed")
    problems.sort(key=lambda p: (p.path, p.line, KINDS.index(p.kind), p.detail))
    return Report(problems, normal.notes)


def write_candidates(path: pathlib.Path, wiki: pathlib.Path, report: Report) -> None:
    """Store the --index candidates in the state folder, never inside the wiki."""
    write_json_atomic(path, {"version": 1, "wiki": str(pathlib.Path(wiki).resolve()),
                             "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                             "candidates": [asdict(p) for p in report.problems]})


def hygiene(root: pathlib.Path, wiki: pathlib.Path, settings: WikiSettings, baseline: pathlib.Path, *,
            now: float, store: bool = True) -> str:
    """One `count(delta)` line; the baseline is stored only after the whole scan succeeds (and when store)."""
    normal = scan(root, wiki, settings, now=now)
    index = scan_index(root, wiki, settings, now=now)
    counts = normal.counts()
    for counter in ("banners", "overlong_lines"):
        counts[counter] = index.counts()[counter]
    config = settings.lint_config()
    try:
        previous = read_json(baseline, None)
    except Exception:  # an unreadable baseline only means there is nothing to compare with
        previous = None
    same = isinstance(previous, dict) and previous.get("config") == config and isinstance(previous.get("counts"), dict)
    parts = []
    for counter, count in counts.items():
        before = previous["counts"].get(counter) if same else None
        delta = f"{count - before:+d}" if isinstance(before, int) else "n/a"
        parts.append(f"{counter}={count}({delta})")
    if store:
        write_json_atomic(baseline, {"version": 1, "config": config, "counts": counts,
                                     "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
    return "OAH_WIKI_HYGIENE " + " ".join(parts)
