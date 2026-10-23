# 리스 범위 규칙 — 경로를 저장소 기준 POSIX 형태로 정규화하고, 대소문자와 유니코드 정규화를 접어 겹침과 범위 밖 파일을 판정한다.
"""Lease scopes.

A scope is `repo` (the whole repository) or repository-relative paths in POSIX form. Comparison folds
case and Unicode normalization (macOS can report decomposed Hangul), so it errs towards reporting a
conflict rather than missing one. `src` covers `src/a.py`; it does not cover `srcx`.
"""
from __future__ import annotations

import os
import pathlib
import posixpath
import unicodedata
from typing import Iterable, Sequence

REPO = "repo"


class ScopeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _relative_to_root(path: pathlib.Path, root: pathlib.Path, raw: str) -> str:
    for candidate, base in ((path, root), (path.resolve(strict=False), root.resolve(strict=False))):
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            pass
        text, prefix = os.path.normcase(str(candidate)), os.path.normcase(str(base))
        if text == prefix:
            return "."
        if text.startswith(prefix.rstrip("\\/") + os.sep):
            return pathlib.PurePath(str(candidate)[len(str(base).rstrip("\\/")) + 1:]).as_posix()
    raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r} is outside the repository")


def _normalize_one(raw: str, root: pathlib.Path) -> str:
    text = raw.strip()
    if not text:
        raise ScopeError("OAH_SCOPE_INVALID", "an empty path is not a scope")
    if text == REPO:
        return REPO
    path = pathlib.Path(text)
    if path.is_absolute():
        text = _relative_to_root(path, root, raw)
    text = text.replace("\\", "/")
    if ":" in text:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r}: ':' is not allowed in a scope path")
    normalized = posixpath.normpath(text)
    if normalized == ".":
        return REPO
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r} is outside the repository")
    return unicodedata.normalize("NFC", normalized)


def normalize_scope(raw: Sequence[str], root: pathlib.Path) -> tuple[str, ...]:
    """Normalize the --scope values; `repo` anywhere means the whole repository."""
    if not raw:
        raise ScopeError("OAH_SCOPE_EMPTY",
                         "a lease needs at least one --scope: a path, or 'repo' for the whole repository")
    items: list[str] = []
    seen: set[str] = set()
    for value in raw:
        normalized = _normalize_one(value, root)
        if normalized == REPO:
            return (REPO,)
        if _key(normalized) not in seen:
            seen.add(_key(normalized))
            items.append(normalized)
    return tuple(items)


def _covers(outer: str, inner: str) -> bool:
    outer_key, inner_key = _key(outer), _key(inner)
    return inner_key == outer_key or inner_key.startswith(outer_key + "/")


def overlaps(a: Sequence[str], b: Sequence[str]) -> bool:
    if REPO in a or REPO in b:
        return True
    return any(_covers(x, y) or _covers(y, x) for x in a for y in b)


def outside(scope: Sequence[str], changed: Iterable[str]) -> list[str]:
    """The changed repository-relative paths that no scope entry covers, in order."""
    if REPO in scope:
        return []
    return [path for path in changed if not any(_covers(entry, path) for entry in scope)]
