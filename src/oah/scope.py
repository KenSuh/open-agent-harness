# 리스 범위 규칙 — 경로를 저장소 기준 POSIX 형태로 정규화하고, 대소문자와 유니코드 정규화를 접어 겹침과 범위 밖 파일을 판정한다.
"""Lease scopes.

A scope is `repo` (the whole repository) or repository-relative paths in POSIX form. Comparison folds
case and Unicode normalization (macOS can report decomposed Hangul), so it errs towards reporting a
conflict rather than missing one. `src` covers `src/a.py`; it does not cover `srcx`.

User scopes resolve as the host OS opens them: Windows collapses `link/..` lexically before following
an alias; POSIX follows the link first. Acquired canonical scopes stay fixed until explicitly renewed.
Git's changed entries are compared literally, with only case and Unicode normalization folded.
"""
from __future__ import annotations

import pathlib
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
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError as exc:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r} is outside the repository") from exc
    except RuntimeError as exc:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r} contains a symlink loop") from exc


def _check_components(text: str, raw: str) -> None:
    # Apply this on every OS so scopes remain unambiguous when shared with Windows.
    if any(part not in (".", "..") and part.endswith((".", " ")) for part in text.split("/")):
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r}: path components must not end in a dot or space")


def _normalize_one(raw: str, root: pathlib.Path) -> str:
    text = raw.replace("\\", "/")
    if not text.strip():
        raise ScopeError("OAH_SCOPE_INVALID", "an empty path is not a scope")
    if text == REPO:
        return REPO
    windows = pathlib.PureWindowsPath(text)
    if windows.drive and not windows.root:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r}: drive-relative paths are not allowed in a scope")
    if ":" in text[len(windows.drive):]:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r}: ':' is not allowed in a scope path")
    _check_components(text, raw)
    path = pathlib.Path(text)
    text = _relative_to_root(path if path.is_absolute() else root / path, root, raw)
    _check_components(text, raw)
    if ":" in text:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r}: ':' is not allowed in a scope path")
    if text == ".":
        return REPO
    return unicodedata.normalize("NFC", text)


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
    """Compare fixed canonical scopes with literal Git entries, preserving their spelling and order."""
    if REPO in scope:
        return []
    return [path for path in changed if not any(_covers(entry, path) for entry in scope)]
