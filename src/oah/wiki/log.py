# 위키 로그 형식 — log.md 와 아카이브를 바이트 그대로 항목으로 나누고, 새 항목 삽입과 롤오버 계획(보존 스트림 검증 포함)을 만든다.
"""The log format shared by `oah wiki log`, rollover, recovery and the commit check.

A log or archive is: header lines, one `<!-- OAH:LOG_INSERT_BELOW -->` marker line, optional blank
lines, then entries. An entry starts at an unindented `## YYYY-MM-DD <title>` line outside fenced
code and owns every byte up to the next entry or the end of the file. All work is on bytes, so
moving entries never changes a single byte of them, CR/LF included.
"""
from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass

from oah.wiki.base import EXIT_REFUSED, WikiError
from oah.wiki.markdown import FenceTracker, eol_of, split_lines, strip_eol

MARKER = "<!-- OAH:LOG_INSERT_BELOW -->"
INDEX_BEGIN = "<!-- OAH:ARCHIVE_INDEX:BEGIN -->"
INDEX_END = "<!-- OAH:ARCHIVE_INDEX:END -->"
ARCHIVE_PREFIX = "log_archive_"  # fixed: the 5.4 scope exemption, lint and recovery rely on this name

_ENTRY = re.compile(r"^## ([0-9]{4}-[0-9]{2}-[0-9]{2}) +(\S.*)$")
_CANDIDATE = re.compile(r"^ {0,3}##[ \t]+[0-9]")
_HEADING = re.compile(r"^ {0,3}##(?:[ \t]|$)")


class ArchiveNaming:
    """Archives are named log_archive_NN.md, numbered from 01 with at least two digits."""

    digits = 2
    prefix = ARCHIVE_PREFIX

    def __init__(self) -> None:
        self._regex = re.compile("^" + re.escape(self.prefix) + "([0-9]+)\\.md$")

    def number(self, name: str) -> int | None:
        """The archive number of a file name, or None when the name is not an archive."""
        match = self._regex.match(name)
        return int(match.group(1)) if match else None

    def well_formed(self, name: str) -> bool:
        match = self._regex.match(name)
        return bool(match) and len(match.group(1)) >= self.digits and int(match.group(1)) > 0

    def name(self, number: int) -> str:
        return f"{self.prefix}{number:0{self.digits}d}.md"


DEFAULT_NAMING = ArchiveNaming()


@dataclass(frozen=True)
class Entry:
    date: str
    title: str
    line: int  # 1-based line of the heading in its file
    data: bytes


@dataclass(frozen=True)
class ParsedLog:
    lines: list[bytes]
    marker: int  # 0-based index of the marker line
    index_span: tuple[int, int] | None  # 0-based begin and end marker lines of the archive index
    prefix: bytes
    entries: list[Entry]

    @property
    def header(self) -> bytes:
        return b"".join(self.lines[:self.marker + 1])

    @property
    def entry_bytes(self) -> int:
        return sum(len(entry.data) for entry in self.entries)


def _invalid(name: str, problem: str) -> WikiError:
    return WikiError("OAH_WIKI_LOG_INVALID", f"{name}: {problem}. Nothing was written. Fix the file by hand so every entry starts "
                     f"with an unindented '## YYYY-MM-DD <title>' line below the single {MARKER} line, "
                     "then retry.", EXIT_REFUSED)


def parse_log(data: bytes, name: str) -> ParsedLog:
    """Split a log or archive into header, blank prefix and whole entries, or refuse OAH_WIKI_LOG_INVALID."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(name, f"is not valid UTF-8 ({exc})") from exc
    lines = split_lines(data)
    texts = [line.decode("utf-8") for line in lines]
    fences = FenceTracker()
    fenced: list[bool] = []
    markers, begins, ends = [], [], []
    opened_at = 0
    for number, text in enumerate(texts):
        was_open = fences.open
        fenced.append(fences.feed(text))
        if fences.open and not was_open:
            opened_at = number
        if fenced[-1]:
            continue
        body = strip_eol(text).rstrip()
        if body == MARKER:
            markers.append(number)
        elif body == INDEX_BEGIN:
            begins.append(number)
        elif body == INDEX_END:
            ends.append(number)
    if fences.open:
        raise _invalid(name, f"unclosed code fence: the fence opened at line {opened_at + 1} is never closed, so "
                       "entry boundaries are ambiguous")
    if not markers:
        raise _invalid(name, f"no marker: there is no {MARKER} line outside code")
    if len(markers) != 1:
        raise _invalid(name, f"has {len(markers)} {MARKER} marker lines outside code; it needs exactly one")
    marker = markers[0]
    index_span = None
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or not begins[0] < ends[0] < marker:
            raise _invalid(name, f"needs exactly one {INDEX_BEGIN} line followed by one {INDEX_END} line, "
                           "both above the marker")
        index_span = (begins[0], ends[0])
    starts: list[tuple[int, str, str]] = []
    for number in range(marker + 1, len(lines)):
        body = strip_eol(texts[number])
        if not starts and not fenced[number] and _HEADING.match(body):
            match = _ENTRY.match(body)
            if match is None or _bad_date(match.group(1)):
                raise _invalid(name, f"undated first entry heading: line {number + 1} has no 'YYYY-MM-DD' date: "
                               f"{body.strip()!r}")
        if not fenced[number] and _CANDIDATE.match(body):
            match = _ENTRY.match(body)
            if match is None or _bad_date(match.group(1)):
                raise _invalid(name, f"line {number + 1} looks like an entry heading but is not "
                               f"'## YYYY-MM-DD <title>' with a real date: {body.strip()!r}")
            starts.append((number, match.group(1), match.group(2).rstrip()))
        elif not starts and (fenced[number] or body.strip()):
            raise _invalid(name, f"line {number + 1} has text between the marker and the first entry")
    first = starts[0][0] if starts else len(lines)
    entries = []
    for position, (number, date, title) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        entries.append(Entry(date, title, number + 1, b"".join(lines[number:end])))
    return ParsedLog(lines, marker, index_span, b"".join(lines[marker + 1:first]), entries)


def _bad_date(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return True
    return False


def add_entry(data: bytes, *, title: str, body: str, date: datetime.date) -> bytes:
    """The log with a new entry at the top, in the file's own line ending, or OAH_WIKI_LOG_INVALID."""
    parsed = parse_log(data, "log.md")
    eol = eol_of(data).decode("ascii")
    if "\n" in title or "\r" in title:
        raise WikiError("OAH_WIKI_TITLE_INVALID", "a log title is one line; put details in --body", EXIT_REFUSED)
    text = f"## {date.isoformat()} {title.strip()}{eol}{eol}"
    body_lines = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if body_lines.strip():
        text += eol.join(body_lines.split("\n")) + eol + eol
    entry = text.encode("utf-8")
    head = parsed.header + parsed.prefix
    if not head.endswith(b"\n"):
        head += eol.encode("ascii")
    result = head + entry + b"".join(item.data for item in parsed.entries)
    try:
        check = parse_log(result, "log.md")
    except WikiError as exc:
        raise WikiError("OAH_WIKI_LOG_INVALID", f"the new entry would break the log format ({exc}); remove "
                        "heading-like '## <date>' lines, the marker and unclosed code fences from the body",
                        EXIT_REFUSED) from exc
    if [item.data for item in check.entries] != [entry, *(item.data for item in parsed.entries)]:
        raise WikiError("OAH_WIKI_LOG_INVALID", "the new entry's body contains a line that starts another "
                        "entry; remove '## YYYY-MM-DD' headings from the body", EXIT_REFUSED)
    return result


def archive_number(name: str, naming: ArchiveNaming = DEFAULT_NAMING) -> int | None:
    return naming.number(name)


def archive_name(number: int, naming: ArchiveNaming = DEFAULT_NAMING) -> str:
    return naming.name(number)


def entry_stream(data: bytes, name: str) -> bytes:
    return b"".join(entry.data for entry in parse_log(data, name).entries)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preservation_stream(log_data: bytes, archives: dict[int, bytes], naming: ArchiveNaming = DEFAULT_NAMING) -> bytes:
    """Entry bytes of log.md, then of each archive from the highest number down; metadata excluded."""
    parts = [entry_stream(log_data, "log.md")]
    for number in sorted(archives, reverse=True):
        parts.append(entry_stream(archives[number], naming.name(number)))
    return b"".join(parts)


@dataclass(frozen=True)
class RolloverPlan:
    log: bytes
    number: int
    archive_name: str
    archive: bytes
    moved: int
    kept: int
    retained_bytes: int
    oversized: tuple[str, ...]
    entries_sha256: str  # sha256 of log.md's entry bytes before; kept + moved must hash the same

    def summary(self) -> str:
        extra = f" oversized={'; '.join(self.oversized)}" if self.oversized else ""
        return (f"archive={self.archive_name} moved={self.moved} kept={self.kept} "
                f"retained_bytes={self.retained_bytes} entries_sha256={self.entries_sha256}{extra}")


@dataclass(frozen=True)
class RolloverNoOp:
    reason: str
    oversized: tuple[str, ...] = ()


def _archive_header(number: int, today: datetime.date, eol: str, naming: ArchiveNaming) -> bytes:
    return (f"# Log archive {number:02d}{eol}{eol}Whole entries moved from [[log]] by `oah wiki rollover` on "
            f"{today.isoformat()}. Never edit this file; the commit check refuses changes to it.{eol}{eol}"
            f"{MARKER}{eol}").encode("utf-8")


def _index_table(archives: dict[int, ParsedLog], eol: str, naming: ArchiveNaming) -> list[bytes]:
    rows = ["| Archive | Entries | Newest | Oldest |", "|---|---|---|---|"]
    for number in sorted(archives, reverse=True):
        entries = archives[number].entries
        newest = entries[0].date if entries else "-"
        oldest = entries[-1].date if entries else "-"
        rows.append(f"| [[{naming.name(number)[:-3]}]] | {len(entries)} | {newest} | {oldest} |")
    return [(row + eol).encode("utf-8") for row in rows]


def _with_index(parsed: ParsedLog, archives: dict[int, ParsedLog], eol: str, naming: ArchiveNaming) -> bytes:
    """The header with a regenerated archive-index table (inserted above the marker when absent)."""
    lines = list(parsed.lines[:parsed.marker + 1])
    table = _index_table(archives, eol, naming)
    if parsed.index_span is None:
        block = [(INDEX_BEGIN + eol).encode("utf-8"), *table, (INDEX_END + eol).encode("utf-8"), eol.encode("utf-8")]
        lines[parsed.marker:parsed.marker] = block
    else:
        begin, end = parsed.index_span
        lines[begin + 1:end] = table
    return b"".join(lines)


def _entry_label(entry: Entry, name: str) -> str:
    return f"{name}:{entry.line} '## {entry.date} {entry.title}' ({len(entry.data)} bytes)"


def plan_rollover(log_data: bytes, archives: dict[int, bytes], *, trigger: int, target: int,
                  today: datetime.date, naming: ArchiveNaming = DEFAULT_NAMING) -> RolloverPlan | RolloverNoOp:
    """Which oldest whole entries move to the next archive, following PLAN 5.10 (A8b) exactly.

    Entries move as byte slices. The kept bytes plus the moved bytes must hash the same as the
    original entry bytes, and the whole preservation stream must stay identical; otherwise nothing
    is planned.
    """
    parsed = parse_log(log_data, "log.md")
    parsed_archives = {number: parse_log(data, naming.name(number)) for number, data in archives.items()}
    if len(log_data) <= trigger:
        return RolloverNoOp(f"log.md is {len(log_data)} bytes, not over the {trigger}-byte trigger")
    sizes = [len(entry.data) for entry in parsed.entries]
    if sum(sizes) <= target:
        return RolloverNoOp(f"log.md entries total {sum(sizes)} bytes, within the {target}-byte target; "
                            "the excess is headers and the archive index")
    keep = next((count for count in range(len(sizes) - 1, 0, -1) if sum(sizes[:count]) <= target), 1)
    oversized = [_entry_label(entry, "log.md") for entry in parsed.entries if len(entry.data) > target]
    if len(sizes) == 1:
        return RolloverNoOp("the only entry is larger than the target, and the newest entry always stays",
                            tuple(oversized))
    number = max(archives, default=0) + 1
    eol = eol_of(log_data).decode("ascii")
    moved = parsed.entries[keep:]
    name = naming.name(number)
    moved_bytes = b"".join(entry.data for entry in moved)
    kept_bytes = b"".join(entry.data for entry in parsed.entries[:keep])
    archive = _archive_header(number, today, eol, naming) + moved_bytes
    new_archive = parse_log(archive, name)
    new_log = _with_index(parsed, {**parsed_archives, number: new_archive}, eol, naming) + parsed.prefix + kept_bytes
    original = entry_stream(log_data, "log.md")
    before = preservation_stream(log_data, archives, naming)
    after = preservation_stream(new_log, {**archives, number: archive}, naming)
    if (sha256(kept_bytes + moved_bytes) != sha256(original) or before != after
            or [entry.data for entry in new_archive.entries] != [entry.data for entry in moved]):
        raise WikiError("OAH_WIKI_LOG_INVALID", "rollover could not keep every entry byte-identical; nothing "
                        "was written. Report this with a copy of log.md.", EXIT_REFUSED)
    return RolloverPlan(new_log, number, name, archive, len(moved), keep, sum(sizes[:keep]), tuple(oversized),
                        sha256(original))
