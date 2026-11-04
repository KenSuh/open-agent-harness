# 설계 위키 페이지 — 설계 페이지·프로젝트 페이지·index.md 블록·로그 항목을 그리고, 페이지의 소유 표지와 표지 줄을 읽어 상태를 가린다.
"""The wiki side of designs: what oah writes, and how it reads its own pages back.

A design page is a pure function of one revision manifest, its part texts and the project title: frontmatter with
the mark `oah: design-page`, a heading, two generated lines, the whole-line revision marker, then each part behind a
whole-line part marker. Markers are matched as whole lines regardless of code fences, which is safe because a part
can never hold a line that starts with `<!-- oah`. A project page (mark `oah: design-project`) is oah's from the top
down to its list end marker; anything after that marker is the user's. `index.md` gets one block between markers.
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Iterable, Mapping, Sequence

from oah.design import fs
from oah.design.model import DESIGN_PART, AcceptanceInfo, ProjectRecord, RevisionInfo
from oah.wiki.base import EXIT_REFUSED, WikiError
from oah.wiki.markdown import FenceTracker, eol_of, split_lines, strip_eol
from oah.wiki.notes import parse_frontmatter

FORMAT = 1
DESIGN_MARK_NAME = "design-page"
PROJECT_MARK_NAME = "design-project"
LIST_BEGIN = "<!-- OAH:DESIGN_LIST:BEGIN -->"
LIST_END = "<!-- OAH:DESIGN_LIST:END -->"
INDEX_BEGIN = "<!-- OAH:DESIGN_INDEX:BEGIN -->"
INDEX_END = "<!-- OAH:DESIGN_INDEX:END -->"
INDEX_LABEL = "Designs:"
_REVISION_LINE = re.compile(r"<!-- OAH:DESIGN revision=([1-9][0-9]{0,8}) digest=(sha256:[0-9a-f]{64}) "
                            r"format=([1-9][0-9]{0,3}) -->")
_PART_LINE = re.compile(r"<!-- OAH:DESIGN_PART ([a-z0-9]+(?:-[a-z0-9]+)*) -->")
_STRIP = re.compile(r"[\[\]|]")
_SPACES = re.compile(r"\s+")


def clean(text: str) -> str:
    """Text safe inside a heading, a link alias or a list line: no [ ] |, single spaces."""
    return _SPACES.sub(" ", _STRIP.sub("", text)).strip()


def fit(text: str, room: int) -> str:
    if room < 1:
        return ""
    if len(text) <= room:
        return text
    return (text[:room - 1].rstrip() + "…") if room > 1 else ""


def short_ref(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def part_order(names: Iterable[str]) -> list[str]:
    names = sorted(names)
    return ([DESIGN_PART] if DESIGN_PART in names else []) + [name for name in names if name != DESIGN_PART]


def normal(text: str) -> str:
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


# Design pages -------------------------------------------------------------------------------------


def revision_marker(revision: int, digest: str) -> str:
    return f"<!-- OAH:DESIGN revision={revision} digest={digest} format={FORMAT} -->"


def part_marker(name: str) -> str:
    return f"<!-- OAH:DESIGN_PART {name} -->"


def render_design_page(project: str, project_title: str, info: RevisionInfo, texts: Mapping[str, str]) -> str:
    title = clean(info.title) or info.design
    date = info.at[:10]
    alias = clean(project_title) or project
    lines = ["---",
             "title: " + _quoted(f"{title} (design {project}/{info.design})"),
             f"date: {date}",
             "sources: [" + _quoted(fs.manifest_rel(project, info.design, info.revision)) + "]",
             "tags: [design]",
             f"oah: {DESIGN_MARK_NAME}",
             "---",
             f"# {title}",
             "",
             f"Design `{project}/{info.design}` of [[designs/{project}|{alias}]], revision {info.revision}, saved "
             f"{date}: {clean(info.reason) or 'saved'}",
             "oah writes this page from the design record. Edit the text below the markers, then ask the AI to "
             "save it as a new revision. The project page shows whether it is accepted.",
             "",
             revision_marker(info.revision, info.digest)]
    text = "\n".join(lines) + "\n"
    for position, name in enumerate(part_order(info.parts)):
        # A blank line after each marker lets lint and the commit hook check the part's links as plain prose.
        text += ("\n" if position else "") + part_marker(name) + "\n\n" + texts[name]
    return text


@dataclasses.dataclass(frozen=True)
class PageView:
    """A design page as read: missing, foreign (no oah mark), unreadable (marked, markers broken) or marked."""

    state: str
    text: str | None = None
    revision: int | None = None
    digest: str | None = None
    parts: dict[str, str] | None = None


def read_design_page(data: bytes | None) -> PageView:
    if data is None:
        return PageView("missing")
    try:
        text = normal(data.decode("utf-8"))
    except UnicodeDecodeError:
        return PageView("foreign")
    meta = parse_frontmatter(text)
    if not isinstance(meta, dict) or meta.get("oah") != DESIGN_MARK_NAME:
        return PageView("foreign", text)
    lines = text.split("\n")
    revisions = [(row, match) for row, line in enumerate(lines) if (match := _REVISION_LINE.fullmatch(line))]
    if len(revisions) != 1 or int(revisions[0][1].group(3)) != FORMAT:
        return PageView("unreadable", text)
    row, match = revisions[0]
    markers = [(number, found.group(1)) for number, line in enumerate(lines)
               if (found := _PART_LINE.fullmatch(line))]
    names = [name for _, name in markers]
    if (not markers or markers[0][0] != row + 1 or any(number < row for number, _ in markers)
            or len(set(names)) != len(names)):
        return PageView("unreadable", text, int(match.group(1)), match.group(2))
    parts = {}
    for position, (number, name) in enumerate(markers):
        end = markers[position + 1][0] if position + 1 < len(markers) else len(lines)
        segment = lines[number + 1:end]
        if segment and segment[0] == "":
            segment = segment[1:]  # the blank line oah writes after each marker
        parts[name] = "\n".join(segment)
    return PageView("marked", text, int(match.group(1)), match.group(2), parts)


# Project pages ------------------------------------------------------------------------------------


def project_head(project: ProjectRecord) -> list[str]:
    title = clean(project.title) or project.id
    return ["---",
            "title: " + _quoted(f"{title} (project {project.id})"),
            f"date: {project.created_at[:10]}",
            "sources: [" + _quoted(fs.project_record_rel(project.id)) + "]",
            "tags: [design-project]",
            f"oah: {PROJECT_MARK_NAME}",
            "---",
            f"# {title}",
            "",
            f"Designs of project `{project.id}`, changed on branch `{short_ref(project.authority)}`. oah writes this "
            "page down to the end marker; add your own notes below it.",
            "",
            LIST_BEGIN]


def list_line(project: str, design: str, title: str, revision: int, status: str, accepted: int | None,
              updated: str, facts: Sequence[str] = ()) -> str:
    alias = clean(title) or design
    if status == "accepted":
        state = "accepted"
    elif accepted is not None:
        state = f"draft; accepted revision {accepted}"
    else:
        state = "draft; nothing accepted yet"
    line = f"- [[{fs.stem(project, design)}|{alias}]]: revision {revision}, {state} ({updated})"
    return line + "".join(f"; {fact}" for fact in facts)


@dataclasses.dataclass(frozen=True)
class ProjectPageView:
    state: str  # missing | foreign | unreadable | marked
    rows: dict[str, str] = dataclasses.field(default_factory=dict)  # design -> its list line, as written
    tail: bytes = b""
    eol: bytes = b"\n"


def _block_rows(lines: list[bytes], begin: int, end: int) -> list[str]:
    return [strip_eol(line.decode("utf-8")) for line in lines[begin + 1:end]]


def _markers(lines: list[bytes], begin: str, end: str) -> tuple[list[int], list[int], bool]:
    fences = FenceTracker()
    begins, ends = [], []
    for number, line in enumerate(lines):
        text = line.decode("utf-8")
        if fences.feed(text):
            continue
        body = strip_eol(text).rstrip()
        if body == begin:
            begins.append(number)
        elif body == end:
            ends.append(number)
    return begins, ends, fences.open


def read_project_page(data: bytes | None, project: str) -> ProjectPageView:
    if data is None:
        return ProjectPageView("missing")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ProjectPageView("foreign")
    meta = parse_frontmatter(normal(text))
    if not isinstance(meta, dict) or meta.get("oah") != PROJECT_MARK_NAME:
        return ProjectPageView("foreign")
    lines = split_lines(data)
    begins, ends, _ = _markers(lines, LIST_BEGIN, LIST_END)
    if len(begins) != 1 or len(ends) != 1 or not begins[0] < ends[0]:
        return ProjectPageView("unreadable", eol=eol_of(data))
    rows = {}
    prefix = f"- [[{project}--"
    for row in _block_rows(lines, begins[0], ends[0]):
        if row.startswith(prefix):
            design = row[len(prefix):].split("|", 1)[0].split("]", 1)[0]
            rows.setdefault(design, row)
    return ProjectPageView("marked", rows, b"".join(lines[ends[0] + 1:]), eol_of(data))


def build_project_page(view: ProjectPageView, project: ProjectRecord, rows: Sequence[str]) -> bytes:
    """The page with a regenerated head and list; the tail after the end marker keeps every byte."""
    eol = view.eol.decode("ascii")
    lines = [*project_head(project), *rows, LIST_END]
    return (eol.join(lines) + eol).encode("utf-8") + view.tail


# index.md block -----------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class IndexView:
    lines: list[bytes]
    span: tuple[int, int] | None
    eol: bytes
    projects: dict[str, list[str]]  # project -> the block lines of its group, as written


def read_index(data: bytes) -> IndexView:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WikiError("OAH_WIKI_INDEX_INVALID", f"index.md is not valid UTF-8 ({exc}); fix its encoding and retry",
                        EXIT_REFUSED) from exc
    lines = split_lines(data)
    begins, ends, open_fence = _markers(lines, INDEX_BEGIN, INDEX_END)
    if not begins and not ends:
        if open_fence:
            raise WikiError("OAH_WIKI_INDEX_INVALID", "index.md ends inside a code fence; close it before oah "
                            "appends the design block. Nothing was written.", EXIT_REFUSED)
        return IndexView(lines, None, eol_of(data), {})
    if len(begins) != 1 or len(ends) != 1 or not begins[0] < ends[0]:
        raise WikiError("OAH_WIKI_INDEX_INVALID", f"index.md needs exactly one {INDEX_BEGIN} line followed by one "
                        f"{INDEX_END} line, or neither; it has {len(begins)} and {len(ends)}. Nothing was written. "
                        "Fix the markers by hand, then retry", EXIT_REFUSED)
    groups: dict[str, list[str]] = {}
    current = None
    for row in _block_rows(lines, begins[0], ends[0]):
        if row.startswith("- [[designs/"):
            current = row[len("- [[designs/"):].split("|", 1)[0].split("]", 1)[0]
            groups.setdefault(current, []).append(row)
        elif current is not None and row.startswith("  - "):
            groups[current].append(row)
        else:
            current = None
    return IndexView(lines, (begins[0], ends[0]), eol_of(data), groups)


def _link_line(prefix: str, target: str, title: str, limit: int) -> str:
    bare = f"{prefix}[[{target}]]"
    if len(bare) > limit:
        raise WikiError("OAH_CONFIG_INVALID", f"[wiki] index_line_limit={limit} cannot hold the index line {bare!r}. "
                        f"Set index_line_limit to at least {len(bare)} in .oah/config.toml and retry.")
    alias = fit(clean(title), limit - len(bare) - 1)
    return f"{prefix}[[{target}|{alias}]]" if alias else bare


def index_project_line(project: str, title: str, limit: int) -> str:
    return _link_line("- ", f"designs/{project}", title, limit)


def index_design_line(project: str, design: str, title: str, limit: int) -> str:
    return _link_line("  - ", fs.stem(project, design), title, limit)


def build_index(data: bytes, view: IndexView, block: Sequence[str]) -> bytes:
    """index.md with the design block replaced, or appended when it has none; other bytes stay as they are."""
    eol = view.eol
    rendered = b"".join(line.encode("utf-8") + eol for line in [INDEX_BEGIN, INDEX_LABEL, *block, INDEX_END])
    if view.span is None:
        out = data
        if out and not out.endswith(b"\n"):
            out += eol
        if out.strip() and not out.endswith(eol + eol):
            out += eol
        return out + rendered
    begin, end = view.span
    lines = list(view.lines)
    if not lines[end].endswith(b"\n"):
        rendered = rendered[:-len(eol)]
    return b"".join(lines[:begin]) + rendered + b"".join(lines[end + 1:])


# Log entries --------------------------------------------------------------------------------------


def log_marker(key: str) -> str:
    return f"<!-- oah-design: {key} -->"


def log_entry(kind: str, project: ProjectRecord, *, design: str | None = None, info: RevisionInfo | None = None,
              acceptance: AcceptanceInfo | None = None, old_title: str | None = None) -> tuple[str, str]:
    """The (title, body) of one event's log entry; the marker line is added by `Wiki.add_marked_entry`."""
    project_link = f"[[designs/{project.id}|{clean(project.title) or project.id}]]"
    if kind == "registered":
        return (f"Design project {project.id} registered",
                f"Project {project_link} is registered; its designs change on branch "
                f"`{short_ref(project.authority)}`.")
    if kind == "renamed":
        return (f"Design project {project.id} renamed",
                f"Project {project_link} was called {clean(old_title or '') or project.id}.")
    assert info is not None and design is not None
    link = f"[[{fs.stem(project.id, design)}|{clean(info.title) or design}]]"
    if kind == "accepted":
        assert acceptance is not None
        return (f"Design {project.id}/{design} r{info.revision} accepted",
                f"Revision {info.revision} of {link}, {info.digest}, is accepted: {clean(acceptance.reason)}")
    return (f"Design {project.id}/{design} r{info.revision} {kind}",
            f"Revision {info.revision} of {link}, {info.digest}: {clean(info.reason)}")
