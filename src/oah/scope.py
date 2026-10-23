# 리스 범위 규칙 — 경로를 저장소 기준 POSIX 형태로 정규화하고, 대소문자와 유니코드 정규화를 접어 겹침과 범위 밖 파일을 판정한다.
"""Lease scopes.

A scope is `repo` (the whole repository) or repository-relative paths in POSIX form. Comparison folds
case and Unicode normalization (macOS can report decomposed Hangul), so it errs towards reporting a
conflict rather than missing one. `src` covers `src/a.py`; it does not cover `srcx`.
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


def outside(scope: Sequence[str], changed: Iterable[str], *, root: pathlib.Path | None = None) -> list[str]:
    """The changed repository-relative paths that no scope entry covers, in order."""
    if root is not None:
        scope = normalize_scope(scope, root)
        paths = [(path, _normalize_one(path, root)) for path in changed]
    else:
        paths = [(path, path) for path in changed]
    if REPO in scope:
        return []
    return [path for path, normalized in paths if not any(_covers(entry, normalized) for entry in scope)]
