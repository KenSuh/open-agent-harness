# 마크다운 도우미 — 구조용 펜스 추적과 보수적인 린트 링크 검색을 분리하고 줄끝을 보존한다.
from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]*)\]\]")
_TICKS = re.compile(r"`+")


def split_lines(data: bytes) -> list[bytes]:
    """Split on LF only; every line keeps its ending, so joining the lines gives the input back."""
    lines = data.split(b"\n")
    out = [line + b"\n" for line in lines[:-1]]
    if lines[-1]:
        out.append(lines[-1])
    return out


def strip_eol(line: str) -> str:
    """The line without its LF and one CR before it."""
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def eol_of(data: bytes) -> bytes:
    """The file's line ending: that of its first line, else LF."""
    first = data.find(b"\n")
    return b"\r\n" if first > 0 and data[first - 1:first] == b"\r" else b"\n"


class FenceTracker:
    """Structural fences: up to three leading spaces, with no container interpretation."""

    _max_indent = 3

    def __init__(self) -> None:
        self._char = ""
        self._length = 0

    @property
    def open(self) -> bool:
        return bool(self._char)

    def feed(self, line: str) -> bool:
        line = strip_eol(line).expandtabs(4)
        if self._char:
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            run = len(stripped) - len(stripped.lstrip(self._char))
            if indent <= self._max_indent and run >= self._length and not stripped[run:].strip(" \t"):
                self._char, self._length = "", 0
            return True
        match = _FENCE_OPEN.match(line)
        if match is None or len(match.group(1)) > self._max_indent:
            return False
        fence, info = match.group(2), match.group(3)
        if fence[0] == "`" and "`" in info:
            return False
        self._char, self._length = fence[0], len(fence)
        return True


_HTML_RAW = re.compile(r"^ {0,3}<(?:script|pre|style|textarea)(?:[ \t>]|$)", re.IGNORECASE)
_HTML_BLOCK = re.compile(
    r"^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|"
    r"dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|"
    r"head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|"
    r"p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?:[ \t]|/?>|$)",
    re.IGNORECASE)
_HTML_TAG = re.compile(
    r"^ {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:[ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:[ \t]*=[ \t]*(?:[^ \t\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*[ \t]*/?>|"
    r"</[A-Za-z][A-Za-z0-9-]*[ \t]*>)[ \t]*$")
_HTML_ENDS = {
    "raw": re.compile(r"</(?:script|pre|style|textarea)>", re.IGNORECASE),
    "comment": re.compile(r"-->"), "instruction": re.compile(r"\?>"),
    "declaration": re.compile(r">"), "cdata": re.compile(r"\]\]>"),
    "blank": re.compile(r"^[ \t]*$"),
}
_CONTAINER = re.compile(r"^ {0,3}(?:> ?|(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]+|$))")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_RULE = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|[=-]+[ \t]*)$")


@dataclass(frozen=True)
class _LintFence:
    char: str
    length: int
    indent: int
    start: int


@dataclass(frozen=True)
class _LintState:
    fence: _LintFence | None = None
    html: str = ""
    frontmatter: bool = False
    paragraph: bool | None = False  # None keeps paragraph status uncertain until a block boundary.


def _html_start(line: str, paragraph: bool | None) -> str:
    """CommonMark's seven HTML starts; a complete generic tag cannot interrupt a paragraph."""
    if _HTML_RAW.match(line):
        return "raw"
    for opening, kind in (("<!--", "comment"), ("<?", "instruction"), ("<![CDATA[", "cdata")):
        if line.lstrip(" ").upper().startswith(opening) and len(line) - len(line.lstrip(" ")) <= 3:
            return kind
    if re.match(r"^ {0,3}<![A-Za-z]", line):
        return "declaration"
    if _HTML_BLOCK.match(line) or (not paragraph and _HTML_TAG.match(line)):
        return "blank"
    return ""


def _lint_steps(state: _LintState, line: str, number: int) -> list[tuple[_LintState, int | None, bool]]:
    """Possible next states, a just-closed fence's start, and whether inline parsing is certain."""
    if state.frontmatter:
        return [(_LintState() if line.strip(" \t") in ("---", "...") else state, None, False)]
    if state.html:
        return [(_LintState() if _HTML_ENDS[state.html].search(line) else state, None, False)]
    indent = len(line) - len(line.lstrip(" "))
    if state.fence:
        fence = state.fence
        steps = []
        # A containing list or quote may end before a dedented nonblank line. Keep both readings.
        if line.strip(" \t") and indent < fence.indent:
            steps.extend(_lint_steps(_LintState(), line, number))
        body = line.lstrip(" ")
        run = len(body) - len(body.lstrip(fence.char))
        if indent <= 3 and run >= fence.length and not body[run:].strip(" \t"):
            steps.append((_LintState(), fence.start, False))
        else:
            steps.append((state, None, False))
        return steps

    # Container prefixes are ambiguous to lint. Retain the plain reading as well as their delimiters.
    views = [line]
    while match := _CONTAINER.match(views[-1]):
        views.append(views[-1][match.end():])
    steps = []
    if number == 0 and line.lstrip("\ufeff").strip(" \t") == "---":
        steps.append((_LintState(frontmatter=True), None, False))
    for index, view in enumerate(views):
        html = _html_start(view, state.paragraph if index == 0 else False)
        if html:
            steps.append((_LintState() if _HTML_ENDS[html].search(view) else _LintState(html=html),
                          None, False))
        match = _FENCE_OPEN.match(view)
        if match and not (match.group(2)[0] == "`" and "`" in match.group(3)):
            fence = match.group(2)
            opening_indent = len(match.group(1)) if index == 0 else max(1, indent)
            steps.append((_LintState(fence=_LintFence(fence[0], len(fence), opening_indent, number)),
                          None, False))
            if index == 0 and not opening_indent:
                return steps

    if not line.strip(" \t") or _HEADING.match(line) or _RULE.match(line):
        paragraphs = (False,)
    elif len(views) > 1 or indent >= 4 or line.lstrip(" ").startswith("[") or state.paragraph is None:
        # Containers and possible multiline reference definitions need not leave a paragraph open.
        paragraphs = (None,)
    else:
        paragraphs = (True,)
    plain_inline = len(views) == 1 and indent < 4 and not steps
    steps.extend((_LintState(paragraph=paragraph), None, plain_inline) for paragraph in paragraphs)
    return steps


def _unparsed_block(line: str, paragraph: bool | None) -> bool:
    """Reference definitions and other HTML may span lines; their block boundaries are unknown."""
    while True:
        body = line.lstrip(" ")
        if ((body.startswith("[") and not body.startswith("[["))
                or ("<" in line and not _html_start(line, paragraph) and not _HTML_TAG.match(line))):
            return True
        match = _CONTAINER.match(line)
        if match is None:
            return False
        line, paragraph = line[match.end():], False


def _fenced_lines(lines: list[str]) -> tuple[int, list[bool]]:
    """Intersect closed-fence masks over all readings, merging equal states after every line.

    A state's integer mask contains only fences already closed on every path reaching that state.
    Pending fences add no bits, so an unclosed or implicitly ended fence masks nothing, even at EOF.
    """
    states = {_LintState(): 0}
    inline = []
    uncertain_from = len(lines)
    for number, line in enumerate(lines):
        body = strip_eol(line).expandtabs(4)
        following: dict[_LintState, int] = {}
        certain_inline = True
        for state, masked in states.items():
            if not (state.fence or state.html or state.frontmatter) and _unparsed_block(body, state.paragraph):
                # Do not guess later fence pairings after syntax this parser cannot fully resolve.
                uncertain_from = min(uncertain_from, number)
            for after, start, allow_inline in _lint_steps(state, body, number):
                closed = 0 if start is None else (1 << (number + 1)) - (1 << start)
                mask = masked | closed
                following[after] = following[after] & mask if after in following else mask
                certain_inline = certain_inline and allow_inline
        states = following
        inline.append(certain_inline)
    certain = (1 << len(lines)) - 1
    for masked in states.values():
        certain &= masked
    return certain & ((1 << uncertain_from) - 1), inline


def _blank_code(text: str) -> str:
    return "".join(char if char in "\r\n" else " " for char in text)


def _mask_spans(line: str) -> tuple[str, bool]:
    """Mask same-line spans unless HTML or escaped/unpaired ticks make their ownership uncertain."""
    if "<" in line or any(match.start() and line[match.start() - 1] == "\\" for match in _TICKS.finditer(line)):
        return line, True
    out = list(line)
    position = 0
    while True:
        opening = _TICKS.search(line, position)
        if opening is None:
            break
        size = len(opening.group())
        closing = opening.end()
        while True:
            candidate = _TICKS.search(line, closing)
            if candidate is None or len(candidate.group()) == size:
                break
            closing = candidate.end()
        if candidate is None:
            return line, True
        for index in range(opening.start(), candidate.end()):
            if out[index] != "\n":
                out[index] = " "
        position = candidate.end()
    return "".join(out), False


def mask_code(text: str) -> str:
    """Hide a lint candidate only when every possible reading puts it inside code."""
    lines = text.split("\n")
    fenced, inline = _fenced_lines(lines)
    out = []
    uncertain = False
    for number, line in enumerate(lines):
        blank = not strip_eol(line).strip(" \t")
        if blank:
            uncertain = False
        if fenced & (1 << number):
            out.append(_blank_code(line))
        elif blank:
            out.append(line)
        else:
            masked, ambiguous = _mask_spans(line)
            out.append(masked if inline[number] and not uncertain else line)
            uncertain = uncertain or ambiguous
    return "\n".join(out)


@dataclass(frozen=True)
class Link:
    line: int
    target: str
    raw: str
    embed: bool


def wikilinks(text: str) -> list[Link]:
    """Lint link candidates with 1-based lines; same-note links ([[#x]]) are skipped."""
    masked = mask_code(text)
    links = []
    for number, line in enumerate(masked.split("\n"), start=1):
        for match in _WIKILINK.finditer(line):
            inner = match.group(2)
            target = inner.split("|", 1)[0]
            if target.endswith("\\"):
                target = target[:-1]
            target = target.split("#", 1)[0].strip()
            if target:
                links.append(Link(number, target, match.group(0), bool(match.group(1))))
    return links
