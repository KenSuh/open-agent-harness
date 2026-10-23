# 원래 의도(OAH_INTENT) — 의도 구획 파싱·다이제스트, 안전한 원본 읽기, 네 가지 결속 상태, 반환 줄 검사, 공용 꼬리말을 맡는 공유 모듈이다.
"""The original intent: one section that carries a task's goal through every stage.

This module is a shared API. Packets, leases (lane P4b), briefs (P3b), review and fix (P6) and the
tier agent files (P7b) all use it, so the names below stay stable.

Section
    `parse(text) -> Intent` finds exactly one `BEGIN`/`END` marker pair, each a whole line at line
    start, and checks the layout between them: `## Intent`; the fields `Intent-ID`, `Revision` and
    `Status` once each (a trailing `# comment` is ignored); then the headings `HEADINGS`, once each and
    in order. Success conditions are top-level bullets `- AC<n>: <text>` with unique ids; indented
    lines continue a bullet and other text is allowed, but every other top-level bullet is invalid.
    Every problem found is raised together in `IntentInvalid.problems`.

Digest
    `digest_of(section)` is `sha256:` plus the hex sha256 of the UTF-8 bytes from the BEGIN line to
    the END marker, after CRLF (and lone CR) became LF, plus exactly one LF. `Intent.section` is that
    exact text, so CRLF and LF copies of one packet share a digest.

Source files
    `read_source(path)` reads a regular file that is not a symlink, is under `MAX_BYTES`, and does
    not change between `lstat` and the read. It raises `SourceError` with `.code` one of
    `OAH_INTENT_SOURCE_MISSING`, `OAH_INTENT_SOURCE_INVALID` or `OAH_INTENT_SOURCE_UNREADABLE`.

Binding
    `Binding(source, id, revision, digest, acs)` is what a lease records (`Intent.bind(source)`).
    `binding_state(binding, root)` returns the first match of `STATES`: `unbound` (no binding),
    `missing` (the source path does not exist, not even as a dangling link), `current` (readable,
    well-formed, `Status: accepted`, and the same ID, revision, digest and AC ids), else `stale`.
    Invalid content is never `current`. `explain_binding` returns the state with a reason.

Returns
    `check_return(text, binding)` requires exactly one `INTENT <id>@r<n> <digest>` line equal to the
    binding and exactly one `AC<n>: met|unmet|deferred — <evidence>` line per bound AC (` -- ` may
    stand for ` — `). Lines are matched after stripping surrounding whitespace; a line that starts
    with `INTENT` or `AC<n>:` but does not match is malformed. Missing, malformed, duplicate,
    mismatched and invented lines are returned as `Problem`s with code `OAH_INTENT_RETURN_INVALID`;
    callers refuse with that code.

Footer
    `FOOTER` is the one canonical intent footer, starting with the line `FOOTER_MARKER`.
    `has_footer(text)` counts it only when that marker is a whole line at line start;
    `with_footer(text)` appends it unless it is already there.
"""
from __future__ import annotations

import dataclasses
import errno
import hashlib
import os
import pathlib
import re
import stat

BEGIN = "<!-- OAH_INTENT:BEGIN -->"
END = "<!-- OAH_INTENT:END -->"
MAX_BYTES = 1024 * 1024
STATES = ("unbound", "missing", "current", "stale")
STATUSES = ("draft", "accepted")
FIELDS = ("Intent-ID", "Revision", "Status")
HEADINGS = ("Problem", "Desired outcome", "Success conditions", "Constraints", "Non-goals", "Open questions")
RETURN_INVALID = "OAH_INTENT_RETURN_INVALID"

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
REVISION_RE = re.compile(r"[1-9][0-9]*")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
AC_ID_RE = re.compile(r"AC[1-9][0-9]*")
_FIELD_LINE = re.compile(r"([A-Za-z-]+):(.*)")
_AC_BULLET = re.compile(r"- (AC[1-9][0-9]*):(.*)")
_BULLET = re.compile(r"[-*+](?:\s|$)")
_INTENT_LINE = re.compile(r"INTENT (\S+)@r([1-9][0-9]*) (sha256:[0-9a-f]{64})")
_AC_LINE = re.compile(r"(AC[1-9][0-9]*): (met|unmet|deferred) (?:—|--) (\S.*)")
_AC_CANDIDATE = re.compile(r"(AC[0-9]+)\s*:")


@dataclasses.dataclass(frozen=True)
class Problem:
    code: str
    message: str
    level: str = "error"  # "error" or "warning"

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class IntentInvalid(Exception):
    def __init__(self, problems: list[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(str(problem) for problem in self.problems))


class SourceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass(frozen=True)
class Binding:
    source: str
    id: str
    revision: int
    digest: str
    acs: tuple[str, ...]

    def to_json(self) -> dict:
        return {"source": self.source, "id": self.id, "revision": self.revision, "digest": self.digest,
                "acs": list(self.acs)}

    @classmethod
    def from_json(cls, data: object) -> "Binding":
        """Rebuild a binding from `to_json()` output; raises ValueError on anything else."""
        if not isinstance(data, dict):
            raise ValueError(f"an intent binding must be an object, not {data!r}")
        source, ident, revision, digest, acs = (data.get(key) for key in ("source", "id", "revision", "digest", "acs"))
        valid = (isinstance(source, str) and source
                 and isinstance(ident, str) and ID_RE.fullmatch(ident)
                 and isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1
                 and isinstance(digest, str) and DIGEST_RE.fullmatch(digest)
                 and isinstance(acs, list) and acs and len(set(acs)) == len(acs)
                 and all(isinstance(ac, str) and AC_ID_RE.fullmatch(ac) for ac in acs))
        if not valid:
            raise ValueError(f"malformed intent binding: {data!r}")
        return cls(source=source, id=ident, revision=revision, digest=digest, acs=tuple(acs))

    def line(self) -> str:
        return f"INTENT {self.id}@r{self.revision} {self.digest}"


@dataclasses.dataclass(frozen=True)
class Intent:
    id: str
    revision: int
    status: str
    section: str
    digest: str
    acs: tuple[str, ...]
    conditions: tuple[tuple[str, str], ...]

    def line(self) -> str:
        """The `INTENT <id>@r<n> <digest>` line every return must carry."""
        return f"INTENT {self.id}@r{self.revision} {self.digest}"

    def bind(self, source: str) -> Binding:
        return Binding(source=source, id=self.id, revision=self.revision, digest=self.digest, acs=self.acs)


# Parsing ----------------------------------------------------------------------------------------


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def digest_of(section: str) -> str:
    """Digest of a section from BEGIN to END; newlines are normalized and one LF is ensured."""
    text = normalize_newlines(section).rstrip("\n") + "\n"
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_marker_like(line: str) -> bool:
    return line.lstrip().startswith("<!--") and ("OAH_INTENT:BEGIN" in line or "OAH_INTENT:END" in line)


def _marker_span(lines: list[str]) -> tuple[int, int]:
    begins = [n for n, line in enumerate(lines) if line == BEGIN]
    ends = [n for n, line in enumerate(lines) if line == END]
    malformed = [n + 1 for n, line in enumerate(lines) if _is_marker_like(line) and line not in (BEGIN, END)]
    if not begins and not ends and not malformed:
        raise IntentInvalid([Problem("OAH_INTENT_MISSING",
                                     f"no {BEGIN} … {END} section found; add the intent section "
                                     "(oah packet new scaffolds one)")])
    problems = []
    if malformed:
        problems.append(Problem("OAH_INTENT_MARKERS_INVALID",
                                f"malformed intent marker on line {', '.join(map(str, malformed))}; each marker "
                                f"must be exactly {BEGIN} or {END} on its own line, at the start of the line"))
    if len(begins) != 1 or len(ends) != 1:
        problems.append(Problem("OAH_INTENT_MARKERS_INVALID",
                                f"found {len(begins)} BEGIN and {len(ends)} END markers; a packet holds exactly "
                                "one intent section"))
    elif ends[0] < begins[0]:
        problems.append(Problem("OAH_INTENT_MARKERS_INVALID", "the END marker comes before the BEGIN marker"))
    if problems:
        raise IntentInvalid(problems)
    return begins[0], ends[0]


def _field_value(raw: str) -> str:
    return re.sub(r"\s+#.*$", "", raw).strip()


def _parse_fields(header: list[str], problems: list[Problem]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in header:
        if not line.strip():
            continue
        match = _FIELD_LINE.fullmatch(line.rstrip())
        name = match.group(1) if match else None
        if name not in FIELDS:
            problems.append(Problem("OAH_INTENT_FIELD_INVALID",
                                    f"unexpected line {line.strip()!r} before the first ### heading; only "
                                    f"{', '.join(FIELDS)} belong there"))
        elif name in fields:
            problems.append(Problem("OAH_INTENT_FIELD_INVALID", f"{name} appears more than once"))
        else:
            fields[name] = _field_value(match.group(2))
    for name in FIELDS:
        if name not in fields:
            problems.append(Problem("OAH_INTENT_FIELD_INVALID", f"the {name} line is missing"))
    if "Intent-ID" in fields and not ID_RE.fullmatch(fields["Intent-ID"]):
        problems.append(Problem("OAH_INTENT_FIELD_INVALID",
                                f"Intent-ID {fields['Intent-ID']!r}: use letters, digits, '.', '_' or '-' (max 64)"))
    if "Revision" in fields and not REVISION_RE.fullmatch(fields["Revision"]):
        problems.append(Problem("OAH_INTENT_FIELD_INVALID",
                                f"Revision {fields['Revision']!r}: use a whole number from 1, without leading zeros"))
    if "Status" in fields and fields["Status"] not in STATUSES:
        problems.append(Problem("OAH_INTENT_FIELD_INVALID",
                                f"Status {fields['Status']!r}: use draft, or accepted once you approve the intent"))
    return fields


def _parse_conditions(lines: list[str], problems: list[Problem]) -> list[tuple[str, str]]:
    conditions: list[tuple[str, str]] = []
    for line in lines:
        if not _BULLET.match(line):
            continue  # blank lines, indented continuations and prose
        match = _AC_BULLET.fullmatch(line.rstrip())
        if match is None:
            problems.append(Problem("OAH_INTENT_AC_INVALID",
                                    f"{line.strip()!r}: each success condition is a bullet '- AC<n>: <outcome>'"))
            continue
        ac_id, text = match.group(1), match.group(2).strip()
        if not text:
            problems.append(Problem("OAH_INTENT_AC_INVALID", f"{ac_id} has no text; describe the outcome"))
        elif ac_id in (known for known, _ in conditions):
            problems.append(Problem("OAH_INTENT_AC_DUPLICATE",
                                    f"{ac_id} is used more than once; give each success condition its own id"))
        else:
            conditions.append((ac_id, text))
    if not conditions and not problems:
        problems.append(Problem("OAH_INTENT_AC_INVALID",
                                "### Success conditions lists no '- AC<n>: <outcome>' bullet; add at least one"))
    return conditions


def parse(text: str) -> Intent:
    """Parse the single intent section in `text`; raises IntentInvalid with every problem found."""
    lines = normalize_newlines(text).split("\n")
    begin, end = _marker_span(lines)
    body = lines[begin + 1:end]
    problems: list[Problem] = []
    if not body or body[0].rstrip() != "## Intent":
        problems.append(Problem("OAH_INTENT_SECTION_INVALID", "the line after BEGIN must be '## Intent'"))
        body = body[1:] if body and body[0].startswith("## ") else body
    else:
        body = body[1:]
    heading_rows = [n for n, line in enumerate(body) if line.startswith("### ")]
    first = heading_rows[0] if heading_rows else len(body)
    fields = _parse_fields(body[:first], problems)
    headings = [body[n][4:].strip() for n in heading_rows]
    if tuple(headings) != HEADINGS:
        problems.append(Problem("OAH_INTENT_SECTION_INVALID",
                                f"the headings are {headings}; use exactly these ### headings in this order: "
                                f"{', '.join(HEADINGS)}"))
    stray = [line.strip() for line in body[first:] if line.startswith(("# ", "## "))]
    if stray:
        problems.append(Problem("OAH_INTENT_SECTION_INVALID",
                                f"heading {stray[0]!r} inside the intent section; use ### headings only"))
    conditions: list[tuple[str, str]] = []
    if "Success conditions" in headings:
        start = heading_rows[headings.index("Success conditions")]
        stop = next((n for n in heading_rows if n > start), len(body))
        conditions = _parse_conditions(body[start + 1:stop], problems)
    else:
        problems.append(Problem("OAH_INTENT_AC_INVALID", "there is no ### Success conditions heading"))
    if problems:
        raise IntentInvalid(problems)
    try:
        revision = int(fields["Revision"])
    except ValueError as exc:
        raise IntentInvalid([Problem("OAH_INTENT_FIELD_INVALID",
                                     "Revision is too large to read; use a smaller positive whole number")]) from exc
    section = "\n".join(lines[begin:end + 1]) + "\n"
    return Intent(id=fields["Intent-ID"], revision=revision, status=fields["Status"],
                  section=section, digest=digest_of(section), acs=tuple(ac for ac, _ in conditions),
                  conditions=tuple(conditions))


# Reading a source file --------------------------------------------------------------------------


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    if (a.st_size, a.st_mtime_ns, stat.S_IFMT(a.st_mode)) != (b.st_size, b.st_mtime_ns, stat.S_IFMT(b.st_mode)):
        return False
    if a.st_ino and b.st_ino and (a.st_ino, a.st_dev) != (b.st_ino, b.st_dev):
        return False
    return True


def read_source(path: pathlib.Path) -> str:
    """Read a packet safely: a regular file, not a symlink, under MAX_BYTES, unchanged while read."""
    path = pathlib.Path(path)
    changed = SourceError("OAH_INTENT_SOURCE_INVALID", f"{path} changed while it was read; run the command again")
    try:
        before = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise SourceError("OAH_INTENT_SOURCE_MISSING", f"{path} does not exist") from exc
    except OSError as exc:
        raise SourceError("OAH_INTENT_SOURCE_UNREADABLE", f"cannot read {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise SourceError("OAH_INTENT_SOURCE_INVALID", f"{path} is a symbolic link; point oah at the real file")
    if not stat.S_ISREG(before.st_mode):
        raise SourceError("OAH_INTENT_SOURCE_INVALID", f"{path} is not a regular file")
    if before.st_size >= MAX_BYTES:
        raise SourceError("OAH_INTENT_SOURCE_INVALID",
                          f"{path} is {before.st_size} bytes; a packet must be under {MAX_BYTES} bytes (1 MiB)")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SourceError("OAH_INTENT_SOURCE_INVALID", f"{path} became a symbolic link") from exc
        if isinstance(exc, FileNotFoundError):
            raise changed from exc
        raise SourceError("OAH_INTENT_SOURCE_UNREADABLE", f"cannot read {path}: {exc}") from exc
    try:
        if not _same_file(before, os.fstat(fd)):
            raise changed
        chunks = []
        remaining = MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    except OSError as exc:
        raise SourceError("OAH_INTENT_SOURCE_UNREADABLE", f"cannot read {path}: {exc}") from exc
    finally:
        os.close(fd)
    try:
        path_after = os.lstat(path)
    except OSError as exc:
        raise changed from exc
    if (len(data) != before.st_size or not _same_file(before, after)
            or not stat.S_ISREG(path_after.st_mode) or not _same_file(before, path_after)
            or not _same_file(after, path_after)):
        raise changed
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceError("OAH_INTENT_SOURCE_INVALID", f"{path} is not UTF-8 text; save it as UTF-8") from exc
    return text[1:] if text.startswith("﻿") else text


# Binding states ---------------------------------------------------------------------------------


def source_path(binding: Binding, root: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(binding.source)
    return path if path.is_absolute() else pathlib.Path(root) / path


def explain_binding(binding: Binding | None, root: pathlib.Path) -> tuple[str, str]:
    """The binding state (first match of STATES) and a one-line reason."""
    if binding is None:
        return "unbound", "no intent is bound to this lease"
    path = source_path(binding, root)
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing", f"the bound source {binding.source} does not exist; restore it or bind the correct source"
    except OSError as exc:
        return "stale", (f"OAH_INTENT_SOURCE_UNREADABLE: cannot inspect {binding.source}: {exc}; "
                         "check the path and permissions, then run the command again")
    try:
        parsed = parse(read_source(path))
    except (SourceError, IntentInvalid) as exc:
        return "stale", str(exc)
    differences = [name for name, ok in (
        ("status is not accepted", parsed.status == "accepted"),
        (f"Intent-ID is {parsed.id}, bound {binding.id}", parsed.id == binding.id),
        (f"revision is {parsed.revision}, bound {binding.revision}", parsed.revision == binding.revision),
        ("digest differs from the bound digest", parsed.digest == binding.digest),
        ("success condition ids differ", parsed.acs == binding.acs),
    ) if not ok]
    if differences:
        return "stale", f"{binding.source}: " + "; ".join(differences)
    return "current", f"{binding.source} matches {binding.line()}"


def binding_state(binding: Binding | None, root: pathlib.Path) -> str:
    """`unbound`, `missing`, `current` or `stale`, applying the ordered predicates of PLAN 5.6."""
    return explain_binding(binding, root)[0]


# Returns ----------------------------------------------------------------------------------------


def _return_problem(message: str) -> Problem:
    return Problem(RETURN_INVALID, message)


def check_return(text: str, binding: Binding) -> list[Problem]:
    """Structural problems of a return's INTENT and AC lines against the binding; empty when valid."""
    intent_problems: list[Problem] = []
    ac_problems: list[Problem] = []
    intents: list[tuple[str, int, str]] = []
    ac_counts: dict[str, int] = {}
    invented: list[str] = []
    ac_format = "'AC<n>: met|unmet|deferred — <evidence>'"
    for number, raw in enumerate(normalize_newlines(text).split("\n"), start=1):
        line = raw.strip()
        if line.startswith("INTENT"):
            match = _INTENT_LINE.fullmatch(line)
            if match is None:
                intent_problems.append(_return_problem(f"line {number}: malformed INTENT line {line!r}; write "
                                                       f"exactly {binding.line()!r}"))
            else:
                try:
                    revision = int(match.group(2))
                except ValueError:
                    intent_problems.append(_return_problem(f"line {number}: INTENT revision is too large; "
                                                           f"write exactly {binding.line()!r}"))
                else:
                    intents.append((match.group(1), revision, match.group(3)))
            continue
        if _AC_CANDIDATE.match(line) is None:
            continue
        match = _AC_LINE.fullmatch(line)
        if match is None:
            ac_problems.append(_return_problem(f"line {number}: malformed AC line {line!r}; write {ac_format}"))
            continue
        ac_id = match.group(1)
        ac_counts[ac_id] = ac_counts.get(ac_id, 0) + 1
        if ac_id not in binding.acs and ac_id not in invented:
            invented.append(ac_id)
    if not intents:
        intent_problems.append(_return_problem(f"the INTENT line is missing; end the return with "
                                               f"{binding.line()!r}"))
    elif len(intents) > 1:
        intent_problems.append(_return_problem("there is more than one INTENT line; keep exactly one"))
    elif intents[0] != (binding.id, binding.revision, binding.digest):
        ident, revision, digest = intents[0]
        intent_problems.append(_return_problem(f"INTENT {ident}@r{revision} {digest} does not match the bound "
                                               f"intent; write {binding.line()!r}"))
    for ac_id in binding.acs:
        count = ac_counts.get(ac_id, 0)
        if count == 0:
            ac_problems.append(_return_problem(f"the {ac_id} line is missing; add one {ac_format} line for it"))
        elif count > 1:
            ac_problems.append(_return_problem(f"there is more than one {ac_id} line; keep exactly one"))
    for ac_id in invented:
        ac_problems.append(_return_problem(f"{ac_id} is not a success condition of "
                                           f"{binding.id}@r{binding.revision}; remove that line"))
    return intent_problems + ac_problems


# Footer -----------------------------------------------------------------------------------------

FOOTER_MARKER = "<!-- OAH_INTENT_FOOTER -->"
FOOTER = f"""{FOOTER_MARKER}
## Intent rules
- The intent section (between the OAH_INTENT markers) is the only statement of the goal, for every
  stage and every worker. Keep it exactly as it is: do not edit, shorten or restate it. Review and
  correction briefs carry the same section; round instructions sit outside it.
- Success conditions are outcomes. Leases, commits, review rounds and the return format are process,
  and they live outside the section.
- If the goal has to change, stop and tell the lead. A changed goal keeps its Intent-ID, raises
  Revision by one and must be accepted again. Never invent a new Intent-ID. A draft cannot be bound.
- End your return with plain lines (no bullets, no code formatting):
  - exactly one line `INTENT <id>@r<revision> <digest>` for the intent you were given
    (`oah intent show` prints it);
  - exactly one line `AC<n>: met|unmet|deferred — <evidence>` for each success condition, and no
    line for a condition that is not in the section.
- A return with a missing, malformed, duplicated, changed or invented line is refused with
  OAH_INTENT_RETURN_INVALID.
"""


def has_footer(text: str) -> bool:
    """True when FOOTER_MARKER is a whole line at line start; a mere mention does not count."""
    return any(line == FOOTER_MARKER for line in normalize_newlines(text).split("\n"))


def with_footer(text: str) -> str:
    """`text` followed by the canonical footer, unless it already has one."""
    if has_footer(text):
        return text
    return text.rstrip("\n") + "\n\n" + FOOTER
