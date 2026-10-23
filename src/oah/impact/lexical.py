# 어휘 참조 — 경로·파일 이름·stem 바늘을 만들고, git grep 결과 줄에서 바늘을 경계 맞춰 찾으며, 명령 줄이 경로를 고정(pin)하는지 실행(exec)하는지 판정한다.
from __future__ import annotations

import pathlib
import re
from typing import Collection, Iterable, Sequence

from oah.engines.proc import run_command
from oah.impact.errors import ImpactError
from oah.paths import git_argv

MIN_NEEDLE = 3
GIT_TIMEOUT = 300.0

_TOKEN = re.compile(r"[\w.\-/\\+@~]+")
_IDENT = re.compile(r"[^\W\d]\w*")
_SEGMENT = re.compile(r"&&|\|\||[;|]")
_STRIP = "\"'`,()[]{}<>"
_RUNNER = re.compile(r"(?:python[0-9.]*|pythonw|py|node|bash|sh|zsh|dash|pwsh|powershell|ruby|perl|deno|bun|tsx|"
                     r"ts-node|php|uv|npx|pipx|source|\.|call|exec|env)\Z")
_PIN = frozenset({"sha256sum", "sha1sum", "sha512sum", "shasum", "md5sum", "b2sum", "cksum", "hash-object",
                  "get-filehash", "certutil", "grep", "egrep", "fgrep", "rg", "ag", "findstr", "select-string",
                  "cmp", "diff"})
_PREFIX = frozenset({"-", "$", ">", "#", "%"})


def needles_for(path: str, min_needle: int = MIN_NEEDLE) -> dict[str, str]:
    """The strings that refer to path: the path itself, its file name and its stem (name before the first dot)."""
    if not path or any(ch in path for ch in "\n\r\0"):
        return {}
    found = {path: "path"}
    name = path.rsplit("/", 1)[-1]
    if name not in found and len(name) >= min_needle:
        found[name] = "basename"
    stem = "" if name.startswith(".") else name.split(".", 1)[0]
    if stem and stem not in found and len(stem) >= min_needle:
        found[stem] = "stem"
    return found


def _candidates(token: str) -> Iterable[str]:
    token = token.replace("\\", "/").rstrip(".-")
    yield token
    for index, char in enumerate(token):
        if char == "/":
            yield token[index + 1:]
    yield from re.split(r"[/.]", token)


def find_needles(line: str, needles: Collection[str]) -> set[str]:
    """Needles that occur in line as a whole token, a folder suffix of a token, or a `/`- or `.`-separated part.

    `impact` matches `src/impact.py` and `[[impact]]`, but neither `impact_cli` nor `impactful`.
    """
    found = set()
    for match in _TOKEN.finditer(line):
        for candidate in _candidates(match.group()):
            if candidate in needles:
                found.add(candidate)
    return found


def find_identifiers(line: str, names: Collection[str]) -> set[str]:
    """Lower-cased names (such as SQL tables) that occur as whole identifiers, ignoring case."""
    return {word for word in (m.group().lower() for m in _IDENT.finditer(line)) if word in names}


def _clean(token: str) -> str:
    return token.strip(_STRIP).replace("\\", "/")


def _names(token: str, needle: str) -> bool:
    token = _clean(token)
    return token == needle or token.endswith("/" + needle)


def _program(token: str) -> str:
    name = _clean(token).rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def classify_command(text: str, needle: str) -> str | None:
    """`pin` when a hash or search command reads needle, `exec` when a command runs it, else None.

    Only one `&&`, `||`, `;` or `|` segment is considered at a time, so `python x.py | grep ok` runs x.py.
    """
    for segment in _SEGMENT.split(text):
        tokens = segment.split()
        while tokens and (tokens[0] in _PREFIX or _clean(tokens[0]).endswith(":")):
            tokens = tokens[1:]
        position = next((i for i, token in enumerate(tokens) if _names(token, needle)), None)
        if position is None:
            continue
        before = [_program(token) for token in tokens[:position]]
        if any(word in _PIN for word in before):
            return "pin"
        if position == 0 or any(_RUNNER.match(word) for word in before):
            return "exec"
    return None


def grep(root: pathlib.Path, rev: str | None, needles: Sequence[str], *, paths: Sequence[str] | None = None,
         ignore_case: bool = False, timeout: float = GIT_TIMEOUT) -> list[tuple[str, int, str]]:
    """Lines that contain any needle as a fixed string: `git grep -n -I -F` with patterns on stdin.

    rev names a commit; None searches the working tree, tracked and untracked files alike (ignored files never).
    """
    patterns = sorted({needle for needle in needles if needle and "\n" not in needle})
    if not patterns or (paths is not None and not paths):
        return []
    argv = [*git_argv(), "--literal-pathspecs", "-c", "core.quotepath=false", "-C", str(root), "grep", "-n", "-I", "-F", "-z",
            "--no-color", "-f", "-"]
    if ignore_case:
        argv.append("-i")
    argv += ["--untracked"] if rev is None else [rev]
    argv += ["--", *(paths or [])]
    result = run_command(argv, timeout=timeout, stdin_text="\n".join(patterns) + "\n")
    if result.timed_out:
        raise ImpactError("OAH_IMPACT_GIT_TIMEOUT", f"git grep took longer than {timeout:g}s in {root}; "
                          "raise [impact] git_timeout or narrow the repository")
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise ImpactError("OAH_IMPACT_GIT_FAILED", f"git grep failed at {rev or 'the working tree'}: "
                          f"{result.stderr.strip()}")
    return _parse_grep(result.stdout, "" if rev is None else rev + ":")


def _parse_grep(output: str, prefix: str) -> list[tuple[str, int, str]]:
    rows, pos = [], 0
    while pos < len(output):
        first = output.index("\0", pos)
        second = output.index("\0", first + 1)
        end = output.find("\n", second + 1)
        end = len(output) if end < 0 else end
        head = output[pos:first]
        path = head[len(prefix):] if head.startswith(prefix) else head
        rows.append((path, int(output[first + 1:second]), output[second + 1:end].rstrip("\r")))
        pos = end + 1
    return rows
