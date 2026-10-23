# 갱신 검사 — 라이선스 판별·요약값, 바뀐 줄 요약, 키 모양·홈 경로·gitleaks 검사, 수상한 변경(새 실행 파일·바이너리·크기 급증)을 본다.
"""Checks an update must pass before its pin advances. Each check fails closed."""
from __future__ import annotations

import difflib
import hashlib
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass
from typing import Callable, Iterable

from oah.engines.proc import Resolved, RunResult, run_command

GITLEAKS_TIMEOUT = 300.0
SKIP_NAMES = {".git"}

# Applicable repository rules, mirrored for wheel installs without tools/.
# A parity test binds these to tools/privacy_scan.py; private local terms are never packaged.
_USERS = "Us" + "ers"
CONTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("home-path", re.compile(r"(?<![A-Za-z0-9_])/" + _USERS + r"/(?!<)[\w.-]+")),
    ("home-path", re.compile(r"(?<![A-Za-z0-9_])/" + "ho" + r"me/(?!<)[\w.-]+")),
    ("home-path", re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:\\{1,2}" + _USERS + r"\\{1,2}(?!<)[^\\/\s\"'<>]+")),
    ("key", re.compile(r"(?<![A-Za-z0-9])" + "s" + "k-" + r"[A-Za-z0-9_-]{20,}")),
    ("key", re.compile(r"(?<![A-Za-z0-9])gh" + "[pousr]_" + r"[A-Za-z0-9]{36,}")),
    ("key", re.compile(r"(?<![A-Za-z0-9])github" + "_pat_" + r"[A-Za-z0-9_]{22,}")),
    ("key", re.compile(r"(?<![A-Za-z0-9])xo" + "x[abprs]-" + r"[A-Za-z0-9-]{10,}")),
    ("key", re.compile(r"(?<![A-Za-z0-9])AK" + "IA" + r"[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("key", re.compile(r"-----BEGIN [A-Z ]*PRIV" + "ATE " + r"KEY-----")),
    ("env-secret", re.compile(r"(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9_]*)?(?:K" + "EY|T" + "OKEN|SEC" + "RET|PASS" + "WORD)"
                              + r"(?![A-Za-z0-9_])\s*[:=]\s*"
                              + r"(?!['\"]?(?:\$\{\{|\$\{[A-Za-z_][A-Za-z0-9_]*\}|"
                              + r"\$[A-Za-z_][A-Za-z0-9_]*(?=['\"\s]|$)|<))"
                              + r"(?:'[^'\r\n]{12,}'|\"[^\"\r\n]{12,}\"|(?!['\"])\S{12,})")),
    ("keychain", re.compile(r"secu" + r"rity\s+find-(?:generic|internet)-password")),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAILS = {"noreply@github.com", "noreply@anthropic.com", "git@github.com"}
ALLOWED_EMAIL_DOMAINS = ("users.noreply.github.com", "example.com", "example.org", "example.net")
ALLOWED_EMAIL_TLDS = (".example", ".test", ".invalid", ".localhost")
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

# Distinctive phrases of each license text, lower-cased with whitespace collapsed.
_LICENSES: list[tuple[str, tuple[str, ...]]] = [
    ("Apache-2.0", ("apache license", "version 2.0", "terms and conditions for use, reproduction")),
    ("MIT", ("permission is hereby granted, free of charge", "the above copyright notice and this "
             "permission notice shall be included")),
    ("BSD-3-Clause", ("redistribution and use in source and binary forms", "neither the name")),
    ("BSD-2-Clause", ("redistribution and use in source and binary forms",)),
    ("ISC", ("permission to use, copy, modify, and/or distribute this software for any purpose",)),
]


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def identify_license(text: str) -> str | None:
    """The SPDX id of a license text, or None when it is not one this kit recognizes."""
    body = _normalized(text)
    for spdx, phrases in _LICENSES:
        if all(phrase in body for phrase in phrases):
            if spdx == "BSD-2-Clause" and "neither the name" in body:
                continue
            return spdx
    return None


def text_digest(data: bytes) -> str:
    """SHA-256 of a text file with CRLF folded to LF, so a checkout's line endings do not matter."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def blob_id(data: bytes) -> str:
    """The git blob id of these bytes (what `git hash-object` prints)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def files_under(folder: pathlib.Path) -> dict[str, pathlib.Path]:
    """Every regular file below folder by forward-slash relative path (skipping .git)."""
    folder = pathlib.Path(folder)
    found: dict[str, pathlib.Path] = {}
    if not folder.is_dir():
        return found
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if any(part in SKIP_NAMES for part in relative.parts) or not path.is_file() or path.is_symlink():
            continue
        found[relative.as_posix()] = path
    return found


@dataclass(frozen=True)
class DiffSummary:
    files_changed: int
    added: int
    removed: int

    def describe(self) -> str:
        return f"{self.files_changed} files changed, +{self.added} -{self.removed} lines"


def _lines(path: pathlib.Path | None) -> list[str]:
    if path is None:
        return []
    data = path.read_bytes()
    if is_binary(data):
        return [f"<binary {blob_id(data)}>"]
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").splitlines()


def diff_summary(old_dir: pathlib.Path, new_dir: pathlib.Path, *, ignore: Iterable[str] = ()) -> DiffSummary:
    """Files changed and lines added and removed between two folders."""
    skip = set(ignore)
    old, new = files_under(old_dir), files_under(new_dir)
    changed = added = removed = 0
    for name in sorted((set(old) | set(new)) - skip):
        before, after = _lines(old.get(name)), _lines(new.get(name))
        if before == after:
            continue
        changed += 1
        for line in difflib.ndiff(before, after):
            if line.startswith("+ "):
                added += 1
            elif line.startswith("- "):
                removed += 1
    return DiffSummary(changed, added, removed)


def builtin_privacy_problems(folder: pathlib.Path, *, ignore: Iterable[str] = ()) -> list[str]:
    skip = set(ignore)
    problems: list[str] = []
    for name, path in files_under(folder).items():
        if name in skip:
            continue
        data = path.read_bytes()
        if data.startswith(UTF16_BOMS):
            text = data.decode("utf-16", errors="replace")
        elif is_binary(data):
            continue
        else:
            text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in CONTENT_RULES:
                if pattern.search(line):
                    detail = "looks like an API key or private key" if rule == "key" else rule
                    problems.append(f"{name}:{number}: {detail}; remove the private content upstream before retrying")
            for match in EMAIL.finditer(line):
                address = match.group().lower()
                domain = address.rsplit("@", 1)[-1]
                if (address not in ALLOWED_EMAILS
                        and not any(domain == allowed or domain.endswith("." + allowed) for allowed in ALLOWED_EMAIL_DOMAINS)
                        and not domain.endswith(ALLOWED_EMAIL_TLDS)):
                    problems.append(f"{name}:{number}: email; remove the private address upstream before retrying")
    return problems


Runner = Callable[..., RunResult]


def gitleaks_problems(folder: pathlib.Path, gitleaks: Resolved, *, runner: Runner = run_command) -> list[str]:
    """Findings from `gitleaks detect --no-git` on folder; a gitleaks failure is itself a problem."""
    with tempfile.TemporaryDirectory(prefix="oah-gitleaks-") as scratch:
        report = pathlib.Path(scratch) / "report.json"
        result = runner((*gitleaks.argv, "detect", "--no-git", "--source", str(folder), "--redact",
                         "--no-banner", "--exit-code", "1", "--report-format", "json",
                         "--report-path", str(report)), timeout=GITLEAKS_TIMEOUT)
        if result.timed_out:
            return ["gitleaks did not finish in time"]
        if result.returncode == 0:
            return []
        if result.returncode == 1:
            return ["gitleaks found possible secrets (run gitleaks detect --no-git on the staged copy)"]
        return [f"gitleaks failed (exit {result.returncode}): {result.stderr.strip()[:200]}"]


def privacy_problems(folder: pathlib.Path, *, gitleaks: Resolved | None, runner: Runner = run_command,
                     ignore: Iterable[str] = ()) -> list[str]:
    """Key shapes and home paths in upstream files (oah's own records in `ignore` are skipped)."""
    problems = builtin_privacy_problems(folder, ignore=ignore)
    if gitleaks is not None:
        problems.extend(gitleaks_problems(folder, gitleaks, runner=runner))
    return problems


def suspicious(*, old_spdx: str, new_spdx: str | None, old_license: str, new_license: str,
               old_executables: Iterable[str], new_executables: Iterable[str],
               old_dir: pathlib.Path, new_dir: pathlib.Path,
               size_jump_ratio: float, size_jump_min_bytes: int) -> list[str]:
    """Reasons an update needs a human: license change, new executable, binary blob, size jump."""
    reasons: list[str] = []
    if new_spdx != old_spdx:
        reasons.append(f"license changed from {old_spdx} to {new_spdx or 'an unrecognized license'}")
    elif new_license != old_license:
        reasons.append("license text changed")
    for name in sorted(set(new_executables) - set(old_executables)):
        reasons.append(f"new executable file {name}")
    old, new = files_under(old_dir), files_under(new_dir)
    for name, path in new.items():
        data = path.read_bytes()
        if is_binary(data) and (name not in old or old[name].read_bytes() != data):
            reasons.append(f"new or changed binary file {name}")
    old_size = sum(os.path.getsize(path) for path in old.values())
    new_size = sum(os.path.getsize(path) for path in new.values())
    if new_size - old_size > size_jump_min_bytes and new_size > old_size * size_jump_ratio:
        reasons.append(f"size grew from {old_size} to {new_size} bytes")
    return reasons
