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


class _LinkFenceTracker(FenceTracker):
    """Lint masks only column-zero fences; ambiguous indented examples remain candidates."""

    _max_indent = 0


def _mask_spans(line: str) -> str:
    """Blank out code spans that open and close on this line, keeping its length."""
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
            position = opening.end()
            continue
        for index in range(opening.start(), candidate.end()):
            if out[index] != "\n":
                out[index] = " "
        position = candidate.end()
    return "".join(out)


def mask_code(text: str) -> str:
    """Lint-only masking of column-zero fences and same-line spans; line numbers survive."""
    fences = _LinkFenceTracker()
    return "\n".join("" if fences.feed(line) else _mask_spans(line) for line in text.split("\n"))


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
