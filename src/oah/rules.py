# 규칙 블록 — AGENTS.md 의 관리 블록 하나와 CLAUDE.md 의 @AGENTS.md 한 줄만 만들고 바꾸며, 줄끝·예산·표시줄을 지키고 전역 파일은 쓰지 않는다.
"""`oah rules sync|check` (PLAN 5.11).

Only `<root>/AGENTS.md` and `<root>/CLAUDE.md` are ever written. Everything is computed and
validated first; a refusal writes nothing. Markers count only as whole lines at line start, so a
document that merely mentions a marker is left alone.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

from oah.atomic import write_text_atomic
from oah.wiki.base import read_bytes
from oah.wiki.markdown import eol_of, split_lines, strip_eol
from oah.wiki.notes import packaged

VERSION = 1
BEGIN = f"<!-- OAH:RULES:BEGIN v{VERSION} -->"
END = "<!-- OAH:RULES:END -->"
IMPORT = "@AGENTS.md"
LANGS = ("en", "ko")
AGENTS_BUDGET = 32 * 1024  # Codex reads at most 32 KiB of project docs by default
BLOCK_LIMIT = 6 * 1024
LOCAL_RULES = pathlib.PurePosixPath(".oah/rules.local.md")

_BEGIN = re.compile(r"^<!-- OAH:RULES:BEGIN v([0-9]+) -->$")
_LANG = re.compile(r"^<!-- oah rules lang=([a-z]+);")
EXIT_REFUSED, EXIT_ENVIRONMENT = 1, 69


class RulesError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Plan:
    lang: str
    agents_before: bytes | None
    agents_after: bytes
    claude_before: bytes | None
    claude_after: bytes
    block_bytes: int


def render_block(lang: str, local: str, eol: str) -> str:
    """The managed block, markers included, without a trailing line ending."""
    if lang not in LANGS:
        raise RulesError("OAH_RULES_LANG", f"unknown language {lang!r}; use one of {', '.join(LANGS)}")
    lines = [BEGIN, f"<!-- oah rules lang={lang}; managed by `oah rules sync`. Put project rules in "
             f"{LOCAL_RULES}, not here. -->", *packaged("rules", f"block.{lang}.md").rstrip("\n").split("\n")]
    local = local.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if any(line.rstrip().startswith("<!-- OAH:RULES:") for line in local.split("\n")):
        raise RulesError("OAH_RULES_MARKER_BROKEN", f"{LOCAL_RULES} contains a reserved rules marker line. "
                         "Remove that marker, then run `oah rules sync` again. Nothing was written.")
    if local.strip():
        lines += ["", f"### Project rules ({LOCAL_RULES})", "", *local.split("\n")]
    lines.append(END)
    return eol.join(lines)


def _decode(data: bytes, name: str) -> list[str]:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesError("OAH_RULES_ENCODING", f"{name} is not valid UTF-8 ({exc}); save it as UTF-8 and retry") from exc
    return [line.decode("utf-8") for line in split_lines(data)]


def find_block(lines: list[str]) -> tuple[int, int] | None:
    """0-based (begin, end) lines of the managed block, None when absent; refuses broken markers."""
    begins, ends, broken = [], [], []
    for number, line in enumerate(lines):
        body = strip_eol(line).rstrip()
        if not body.startswith("<!-- OAH:RULES:"):
            continue
        match = _BEGIN.match(body)
        if match and int(match.group(1)) >= 1:
            begins.append((number, int(match.group(1))))
        elif body == END:
            ends.append(number)
        else:
            broken.append(number)
    hint = (f"Fix the markers by hand so AGENTS.md has at most one {BEGIN} line followed by one {END} line, "
            "then run `oah rules sync` again. Nothing was written.")
    if broken:
        raise RulesError("OAH_RULES_MARKER_BROKEN", f"AGENTS.md line {broken[0] + 1} is a malformed rules marker. {hint}")
    if len(begins) > 1 or len(ends) > 1:
        raise RulesError("OAH_RULES_MARKER_DUPLICATE", f"AGENTS.md has {len(begins)} begin and {len(ends)} end "
                         f"markers. {hint}")
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0][0]:
        raise RulesError("OAH_RULES_MARKER_BROKEN", f"AGENTS.md has an unpaired or reversed rules marker. {hint}")
    if begins[0][1] > VERSION:
        raise RulesError("OAH_RULES_NEWER", f"AGENTS.md has a v{begins[0][1]} rules block from a newer oah; "
                         "upgrade oah instead of syncing with this one")
    return begins[0][0], ends[0]


def _refuse_link(path: pathlib.Path) -> None:
    if path.is_symlink():
        raise RulesError("OAH_RULES_LINK", f"{path.name} is a link to {path.resolve()}; oah never writes through "
                         "links, which could reach global files. Replace it with a regular file, or add the "
                         "block by hand.")


def plan(root: pathlib.Path, *, lang: str | None = None) -> Plan:
    """Compute both files' new bytes and validate every limit; writes nothing."""
    root = pathlib.Path(root)
    agents_path, claude_path = root / "AGENTS.md", root / "CLAUDE.md"
    _refuse_link(agents_path)
    _refuse_link(claude_path)
    local_bytes = read_bytes(root / LOCAL_RULES)
    local = "".join(_decode(local_bytes, str(LOCAL_RULES))) if local_bytes is not None else ""
    agents = read_bytes(agents_path)
    if agents is None:
        lang = lang or "en"
        block = render_block(lang, local, "\n")
        after = (block + "\n").encode("utf-8")
    else:
        lines = _decode(agents, "AGENTS.md")
        span = find_block(lines)
        if span is None:
            lang = lang or "en"
            eol = eol_of(agents).decode("ascii")
            block = render_block(lang, local, eol)
            head = agents if not agents or agents.endswith(b"\n") else agents + eol.encode("ascii")
            gap = eol.encode("ascii") if head.strip() and not head.endswith(eol.encode("ascii") * 2) else b""
            after = head + gap + (block + eol).encode("utf-8")
        else:
            begin, end = span
            found = _LANG.match(strip_eol(lines[begin + 1])) if begin + 1 < end else None
            lang = lang or (found.group(1) if found and found.group(1) in LANGS else "en")
            eol = "\r\n" if lines[begin].endswith("\r\n") else "\n" if lines[begin].endswith("\n") \
                else eol_of(agents).decode("ascii")
            block = render_block(lang, local, eol)
            tail = lines[end][len(strip_eol(lines[end])):]
            after = ("".join(lines[:begin]) + block + tail + "".join(lines[end + 1:])).encode("utf-8")
    find_block(_decode(after, "AGENTS.md"))
    size = len(block.encode("utf-8"))
    if size >= BLOCK_LIMIT:
        raise RulesError("OAH_RULES_BLOCK_TOO_LARGE", f"the managed block would be {size} bytes, over the "
                         f"{BLOCK_LIMIT}-byte limit. Shorten {LOCAL_RULES} (link a wiki note for long project "
                         "rules) and retry. Nothing was written.")
    if len(after) > AGENTS_BUDGET:
        raise RulesError("OAH_RULES_BUDGET", f"AGENTS.md would be {len(after)} bytes, over Codex's {AGENTS_BUDGET}-"
                         "byte project-doc limit. Move long sections into docs/wiki/ and link them, then retry. "
                         "Nothing was written.")
    claude = read_bytes(claude_path)
    if claude is None:
        claude_after = (IMPORT + "\n").encode("ascii")
    else:
        lines = _decode(claude, "CLAUDE.md")
        count = sum(1 for line in lines if strip_eol(line).strip() == IMPORT)
        if count > 1:
            raise RulesError("OAH_RULES_MARKER_DUPLICATE", f"CLAUDE.md has {count} {IMPORT} lines; keep exactly one "
                             "and run `oah rules sync` again. Nothing was written.")
        eol = eol_of(claude)
        claude_after = claude if count else (claude + (b"" if not claude or claude.endswith(b"\n") else eol)
                                             + IMPORT.encode("ascii") + eol)
    return Plan(lang, agents, after, claude, claude_after, size)


@dataclass(frozen=True)
class SyncResult:
    lang: str
    changed: tuple[str, ...]
    agents_bytes: int
    block_bytes: int


def sync(root: pathlib.Path, *, lang: str | None = None) -> SyncResult:
    root = pathlib.Path(root)
    result = plan(root, lang=lang)
    changed = []
    for name, before, after in (("AGENTS.md", result.agents_before, result.agents_after),
                                ("CLAUDE.md", result.claude_before, result.claude_after)):
        if before != after:
            write_text_atomic(root / name, after.decode("utf-8"))
            if read_bytes(root / name) != after:
                raise RulesError("OAH_RULES_VERIFY", f"{name} did not read back as written; check the disk and "
                                 "retry", EXIT_ENVIRONMENT)
            changed.append(name)
    return SyncResult(result.lang, tuple(changed), len(result.agents_after), result.block_bytes)


def check(root: pathlib.Path, *, lang: str | None = None) -> list[str]:
    """Drift lines; empty when both files match what sync would write. Reads only."""
    try:
        result = plan(pathlib.Path(root), lang=lang)
    except RulesError as exc:
        return [str(exc)]
    drift = []
    if result.agents_before is None:
        drift.append("OAH_RULES_DRIFT: AGENTS.md is missing")
    elif result.agents_before != result.agents_after:
        drift.append(f"OAH_RULES_DRIFT: the managed block in AGENTS.md is missing or differs from the {result.lang} "
                     "template plus .oah/rules.local.md")
    if result.claude_before is None:
        drift.append("OAH_RULES_DRIFT: CLAUDE.md is missing")
    elif result.claude_before != result.claude_after:
        drift.append(f"OAH_RULES_DRIFT: CLAUDE.md has no {IMPORT} line")
    return drift
