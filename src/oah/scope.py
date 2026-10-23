# 리스 범위 규칙 — 경로를 저장소 기준 POSIX 형태로 정규화하고, 대소문자와 유니코드 정규화를 접어 겹침과 범위 밖 파일을 판정한다.
"""Lease scopes.

A scope is the exact raw input `repo` (the whole repository) or repository-relative paths in POSIX form.
The root itself (`.` or its absolute path) also means the whole repository. A canonical top-level path
named `repo` is refused: scope paths below it, such as `repo/src`, or use `repo` for the whole repository.
Case variants such as `Repo` stay ordinary paths. Comparison folds case and Unicode normalization
(macOS can report decomposed Hangul), so it errs towards reporting a conflict rather than missing one.
`src` covers `src/a.py`; it does not cover `srcx`.

User scopes resolve as the host OS opens them: Windows collapses `link/..` lexically before following
an alias; POSIX follows the link first. Acquisition records the canonical path followed by its lexical
repository-relative spelling only if that spelling currently resolves to the same path. An alias spelling
equal to `repo` is omitted, preserving the canonical path. Both stay fixed until explicitly renewed and
participate in overlap checks. Only the exact stored entry `repo` is the keyword for overlaps and outside.
Git's changed entries match these recorded spellings literally, folding only case and NFC; undeclared
aliases fail closed, even if their canonical twins also changed. Git paths are never resolved, and their
backslashes and colons stay literal.
"""
from __future__ import annotations

import os
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
    if raw == REPO:
        return REPO
    windows = pathlib.PureWindowsPath(text)
    if windows.drive and not windows.root:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r}: drive-relative paths are not allowed in a scope")
    path = pathlib.Path(text)
    if not path.is_absolute() and ":" in text[len(windows.drive):]:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r}: ':' is not allowed in a scope path")
    _check_components(text, raw)
    text = _relative_to_root(path if path.is_absolute() else root / path, root, raw)
    _check_components(text, raw)
    if ":" in text:
        raise ScopeError("OAH_SCOPE_INVALID", f"{raw!r}: ':' is not allowed in a scope path")
    if text == ".":
        return REPO
    if text == REPO:
        raise ScopeError("OAH_SCOPE_INVALID",
                         f"{raw!r}: top-level path named 'repo' collides with the whole-repository keyword; "
                         "scope paths below it, such as 'repo/src', or use 'repo' for the whole repository")
    return unicodedata.normalize("NFC", text)


def _alias_spelling(raw: str, root: pathlib.Path, canonical: str) -> str | None:
    path = pathlib.Path(os.path.normpath(raw.replace("\\", "/")))
    bases = ((pathlib.Path(os.path.abspath(root)), root.resolve(strict=False))
             if path.is_absolute() else (None,))
    for base in bases:
        try:
            lexical = path.relative_to(base) if base is not None else path
        except ValueError:
            continue
        text = lexical.as_posix()
        if lexical.anchor or ".." in lexical.parts or ":" in text or _key(text) == _key(canonical):
            continue
        try:
            _check_components(text, raw)
            resolved = _relative_to_root(root / lexical, root, raw)
        except (ScopeError, OSError):
            continue
        if unicodedata.normalize("NFC", resolved) == canonical:
            return unicodedata.normalize("NFC", text)
    return None


def normalize_scope(raw: Sequence[str], root: pathlib.Path) -> tuple[str, ...]:
    """Record paths and verified aliases; only raw `repo` or the root grants repository scope."""
    if not raw:
        raise ScopeError("OAH_SCOPE_EMPTY",
                         "a lease needs at least one --scope: a path, or 'repo' for the whole repository")
    items: list[str] = []
    seen: set[str] = set()
    for value in raw:
        normalized = _normalize_one(value, root)
        if normalized == REPO:
            return (REPO,)
        for entry in (normalized, _alias_spelling(value, root, normalized)):
            if entry == REPO:
                continue
            if entry is not None and _key(entry) not in seen:
                seen.add(_key(entry))
                items.append(entry)
    return tuple(items)


def _covers(outer: str, inner: str) -> bool:
    outer_key, inner_key = _key(outer), _key(inner)
    return inner_key == outer_key or inner_key.startswith(outer_key + "/")


def overlaps(a: Sequence[str], b: Sequence[str]) -> bool:
    if REPO in a or REPO in b:
        return True
    return any(_covers(x, y) or _covers(y, x) for x in a for y in b)


def outside(scope: Sequence[str], changed: Iterable[str]) -> list[str]:
    """Compare Git entries literally against the acquired scope, folding only case and NFC."""
    if REPO in scope:
        return []
    return [path for path in changed if not any(_covers(entry, path) for entry in scope)]
