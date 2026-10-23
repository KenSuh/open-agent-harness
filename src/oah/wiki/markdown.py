# 마크다운 도우미 — 줄 나누기(줄끝 보존), CommonMark 펜스 추적, 코드 영역 가리기, 위키링크 찾기를 맡는다.
from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")
_LIST_PREFIX = re.compile(r"^( {0,3}(?:[-*+]|\d{1,9}[.)]))( +|$)")
_QUOTE_PREFIX = re.compile(r"^ {0,3}> ?")
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
    """Feed lines in order; feed() is True for fence lines and the code between them."""

    def __init__(self) -> None:
        self._char = ""
        self._length = 0
        self._containers: list[tuple[str, int]] = []

    @property
    def open(self) -> bool:
        return bool(self._char)

    def feed(self, line: str) -> bool:
        line = strip_eol(line).expandtabs(4)
        # Consume established container prefixes before interpreting fences. Blank lines may
        # continue a list without indentation; blockquotes still require their `>` prefix.
        matched = 0
        for kind, width in self._containers:
            if kind == "quote":
                quote = _QUOTE_PREFIX.match(line)
                if quote is None:
                    break
                line = line[quote.end():]
            elif not line.strip():
                line = ""
            elif line.startswith(" " * width):
                line = line[width:]
            else:
                break
            matched += 1
        if matched < len(self._containers):
            self._containers = self._containers[:matched]
            self._char, self._length = "", 0  # a fence ends when its container ends
        if not self._char:
            while True:
                quote = _QUOTE_PREFIX.match(line)
                item = _LIST_PREFIX.match(line)
                if quote:
                    self._containers.append(("quote", 0))
                    line = line[quote.end():]
                elif item:
                    # More than four spaces after a marker mean one prefix space plus code indent.
                    padding = len(item.group(2))
                    width = len(item.group(1)) + (padding if 1 <= padding <= 4 else 1)
                    self._containers.append(("list", width))
                    line = line[width:]
                else:
                    break
        if self._char:
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            run = len(stripped) - len(stripped.lstrip(self._char))
            if indent <= 3 and run >= self._length and not stripped[run:].strip(" \t"):
                self._char, self._length = "", 0
            return True
        match = _FENCE_OPEN.match(line)
        if match is None:
            return False
        fence, info = match.group(2), match.group(3)
        if fence[0] == "`" and "`" in info:
            return False
        self._char, self._length = fence[0], len(fence)
        return True


def _indent(line: str) -> int:
    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _mask_spans(paragraph: str) -> str:
    """Blank out inline code spans, keeping newlines and length."""
    out = list(paragraph)
    position = 0
    while True:
        opening = _TICKS.search(paragraph, position)
        if opening is None:
            break
        size = len(opening.group())
        closing = opening.end()
        while True:
            candidate = _TICKS.search(paragraph, closing)
            if candidate is None or len(candidate.group()) == size:
                break
            closing = candidate.end()
        if candidate is None:
            position = opening.end()
            continue
        for index in range(opening.start(), candidate.end()):
            if out[index] != "\n":
                out[index] = " "
        position = candidate.end()
    return "".join(out)


def mask_code(text: str) -> str:
    """Replace fenced code, indented code blocks and code spans with blanks; line numbers survive."""
    lines = text.split("\n")
    masked: list[str | None] = []
    fences = FenceTracker()
    previous_blank, in_code, in_list = True, False, False
    for line in lines:
        if fences.feed(line):
            masked.append("")
            previous_blank, in_code = False, False
            continue
        blank = not line.strip()
        if blank:
            masked.append(None)
            previous_blank = True
            continue
        if _indent(line) >= 4 and (in_code or previous_blank) and not in_list:
            masked.append("")
            in_code, previous_blank = True, False
            continue
        in_code = False
        if _LIST_ITEM.match(line):
            in_list = True
        elif _indent(line) == 0:
            in_list = False
        masked.append(line)
        previous_blank = False
    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.extend(_mask_spans("\n".join(paragraph)).split("\n"))
            paragraph.clear()

    for original, line in zip(lines, masked):
        if line is None or line == "":
            flush()
            out.append(original if line is None else "")
        else:
            paragraph.append(line)
    flush()
    return "\n".join(out)


@dataclass(frozen=True)
class Link:
    line: int
    target: str
    raw: str
    embed: bool


def wikilinks(text: str) -> list[Link]:
    """Every [[wikilink]] outside code, with its 1-based line. Same-note links ([[#x]]) are skipped."""
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
