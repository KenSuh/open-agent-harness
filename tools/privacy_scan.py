#!/usr/bin/env python3
# 이 저장소의 푸시 전 개인정보 검사 — HEAD 파일, 전체 이력의 추가 줄, 커밋 메시지·신원에서 홈 경로·이메일·키 모양·비공개 용어를 찾는다.
"""Privacy scan for this repository. Run it before every push.

  python tools/privacy_scan.py [--repo .] [--terms FILE] [--allowlist FILE] [--gitleaks auto|require|off]

Last line: `PRIVACY_SCAN_CLEAN files=<n> commits=<m> gitleaks=<ran|skipped>` (exit 0) or
`PRIVACY_SCAN_HITS n=<k>` (exit 1). Usage or environment errors exit 2.

Private terms (names, products) are never stored in the repository. They live in a local file
(default `.oah-dev/privacy-terms.txt`), one `word <term>` or `substr <term>` per line.
"""
from __future__ import annotations

import argparse
import fnmatch
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Iterable

# Pattern sources are assembled from fragments so this file never matches its own rules,
# nor a plain text search for the same words.
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
# Group 1 counts consecutive backslashes; only an odd count introduces an escape.
# Group 2 keeps the complete local part for allowlist checks before any escape letter is removed.
EMAIL = re.compile(r"(\\*)([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
ALLOWED_EMAILS = {"noreply@github.com", "noreply@anthropic.com", "git@github.com"}
ALLOWED_EMAIL_DOMAINS = ("users.noreply.github.com", "example.com", "example.org", "example.net")
ALLOWED_EMAIL_TLDS = (".example", ".test", ".invalid", ".localhost")
IDENTITY = re.compile(r"^(?:\d+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com$")
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


class ScanError(Exception):
    """A usage or environment problem (exit code 2)."""


@dataclass(frozen=True)
class Term:
    kind: str  # "word" or "substr"
    text: str

    def pattern(self) -> re.Pattern[str]:
        escaped = re.escape(self.text)
        if self.kind == "word":
            return re.compile(r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])", re.IGNORECASE)
        return re.compile(escaped, re.IGNORECASE)


@dataclass(frozen=True)
class Allow:
    rule: str
    path_glob: str
    value: re.Pattern[str]


@dataclass(frozen=True)
class Hit:
    rule: str
    where: str
    path: str
    value: str


def load_terms(path: pathlib.Path) -> list[Term]:
    terms: list[Term] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, text = line.partition(" ")
        if kind not in ("word", "substr") or not text.strip():
            raise ScanError(f"{path}:{number}: expected 'word <term>' or 'substr <term>'")
        terms.append(Term(kind, text.strip()))
    return terms


def load_allowlist(path: pathlib.Path) -> list[Allow]:
    entries: list[Allow] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, hash_sign, reason = line.partition("#")
        fields = body.split()
        if not hash_sign or not reason.strip():
            raise ScanError(f"{path}:{number}: every allowlist entry needs a '# reason'")
        if len(fields) != 3:
            raise ScanError(f"{path}:{number}: expected '<rule> <path-glob> <value-regex> # reason'")
        entries.append(Allow(fields[0], fields[1], re.compile(fields[2])))
    return entries


def _email_allowed(address: str) -> bool:
    lowered = address.lower()
    domain = lowered.rsplit("@", 1)[-1]
    return (lowered in ALLOWED_EMAILS
            or any(domain == allowed or domain.endswith("." + allowed) for allowed in ALLOWED_EMAIL_DOMAINS)
            or domain.endswith(ALLOWED_EMAIL_TLDS))


def scan_text(text: str, terms: Iterable[Term]) -> list[tuple[str, int, str]]:
    """Return (rule, line number, matched value) for every finding in text."""
    found: list[tuple[str, int, str]] = []
    term_patterns = [term.pattern() for term in terms]
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in CONTENT_RULES:
            found.extend((rule, number, m.group(0)) for m in pattern.finditer(line))
        for match in EMAIL.finditer(line):
            address = match.group(2)
            if len(match.group(1)) % 2 and address[0] in "abfnrtv":
                trimmed = address[1:]
                if trimmed.startswith("@"):
                    continue  # An escape followed directly by @ has no local part.
                # Ambiguous escapes must not turn a private mailbox into an allowed one.
                if not _email_allowed(trimmed):
                    address = trimmed
            if not _email_allowed(address):
                found.append(("email", number, address))
        for pattern in term_patterns:
            found.extend(("private-term", number, m.group(0)) for m in pattern.finditer(line))
    return found


def _git(repo: pathlib.Path, git: str, *args: str) -> bytes:
    result = subprocess.run([git, "-C", str(repo), "-c", "core.quotepath=false", *args], capture_output=True)
    if result.returncode != 0:
        raise ScanError(f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def _decode(data: bytes) -> str | None:
    if data.startswith(UTF16_BOMS):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data[:8000]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def _redact(value: str) -> str:
    return value[:2] + "*" * min(max(len(value) - 2, 1), 8)


def scan_repo(repo: pathlib.Path, git: str, terms: list[Term]) -> tuple[list[Hit], int, int]:
    hits: list[Hit] = []
    files = [p for p in _git(repo, git, "ls-tree", "-r", "-z", "--name-only", "HEAD").decode("utf-8").split("\0") if p]
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        text = _decode(_git(repo, git, "cat-file", "blob", f"HEAD:{path}"))
        if text is None:
            continue
        for rule, number, value in scan_text(text, terms):
            hits.append(Hit(rule, f"{path}:{number}", path, value))
            seen.add((path, rule, value))

    log = _git(repo, git, "log", "-p", "-m", "--no-color", "--no-ext-diff", "--no-textconv", "--unified=0",
               "--format=%x00commit %H", "HEAD").decode("utf-8", errors="replace")
    commit, path = "", ""
    for line in log.splitlines():
        if line.startswith("\0commit "):
            commit, path = line[len("\0commit "):][:12], ""
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and path:
            for rule, _, value in scan_text(line[1:], terms):
                if (path, rule, value) not in seen:
                    seen.add((path, rule, value))
                    hits.append(Hit(rule, f"commit {commit} {path}", path, value))

    # Git omits UTF-16 contents from patches as binary. Read changed blobs as well,
    # including root and merge commits, so deleted or replaced versions remain visible.
    changes = _git(repo, git, "log", "--raw", "-z", "-m", "--no-abbrev", "--no-renames",
                   "--format=%x00commit %H", "HEAD").decode("utf-8", errors="replace")
    records = iter(changes.split("\0"))
    checked: set[tuple[str, str]] = set()
    for record in records:
        record = record.lstrip("\n")
        if record.startswith("commit "):
            commit = record[len("commit "):][:12]
        elif record.startswith(":"):
            _, mode, _, blob, status = record.split()
            path = next(records)
            if status == "D" or mode == "160000" or (path, blob) in checked:
                continue
            checked.add((path, blob))
            data = _git(repo, git, "cat-file", "blob", blob)
            if not data.startswith(UTF16_BOMS):
                continue
            for rule, _, value in scan_text(_decode(data), terms):
                if (path, rule, value) not in seen:
                    seen.add((path, rule, value))
                    hits.append(Hit(rule, f"commit {commit} {path}", path, value))

    commits = 0
    for record in _git(repo, git, "log", "--format=%H%x00%ae%x00%ce%x00%B%x1e", "HEAD").decode(
            "utf-8", errors="replace").split("\x1e"):
        record = record.lstrip("\n")
        if not record:
            continue
        sha, author, committer, message = (record.split("\0", 3) + ["", "", ""])[:4]
        commits += 1
        for address in sorted({author, committer}):
            if not (IDENTITY.match(address) or address.lower() == "noreply@github.com"):
                hits.append(Hit("commit-identity", f"commit {sha[:12]} identity", "", address))
        for rule, _, value in scan_text(message, terms):
            hits.append(Hit(rule, f"commit {sha[:12]} message", "", value))
    return hits, len(files), commits


def _allowed(hit: Hit, allowlist: list[Allow]) -> bool:
    return any(entry.rule == hit.rule and fnmatch.fnmatch(hit.path, entry.path_glob) and entry.value.search(hit.value)
               for entry in allowlist)


def run_gitleaks(repo: pathlib.Path, gitleaks: str) -> bool:
    """True when gitleaks finds nothing in the history."""
    result = subprocess.run([gitleaks, "detect", "--source", str(repo), "--redact", "--no-banner",
                             "--exit-code", "1", "--log-opts=--all --full-history -m"], capture_output=True)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        sys.stdout.write(result.stdout.decode("utf-8", "replace"))
        return False
    raise ScanError(f"gitleaks failed: {result.stderr.decode('utf-8', 'replace').strip()}")


def main(argv: list[str] | None = None, *, which: Callable[..., str | None] = shutil.which) -> int:
    parser = argparse.ArgumentParser(prog="privacy_scan.py", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--terms", help="local private-terms file (default: .oah-dev/privacy-terms.txt)")
    parser.add_argument("--allowlist", help="allowlist file (default: tools/privacy-allowlist.txt if present)")
    parser.add_argument("--gitleaks", choices=("auto", "require", "off"), default="auto")
    args = parser.parse_args(argv)
    try:
        git = which("git")
        if git is None:
            raise ScanError("git was not found")
        repo = pathlib.Path(_git(pathlib.Path(args.repo), git, "rev-parse", "--show-toplevel").decode("utf-8").strip())
        gitleaks = which("gitleaks") if args.gitleaks != "off" else None
        if args.gitleaks == "require" and gitleaks is None:
            raise ScanError("gitleaks is required (--gitleaks require) but was not found")

        terms_path = pathlib.Path(args.terms) if args.terms else repo / ".oah-dev" / "privacy-terms.txt"
        terms = load_terms(terms_path) if terms_path.is_file() else []
        if not terms_path.is_file():
            sys.stderr.write(f"PRIVACY_SCAN_WARN no local terms file at {terms_path}; private-term checks skipped\n")
        allow_path = pathlib.Path(args.allowlist) if args.allowlist else repo / "tools" / "privacy-allowlist.txt"
        allowlist = load_allowlist(allow_path) if allow_path.is_file() else []

        hits, files, commits = scan_repo(repo, git, terms)
        hits = [hit for hit in hits if not _allowed(hit, allowlist)]
        gitleaks_state = "skipped"
        if gitleaks is not None:
            gitleaks_state = "ran"
            if not run_gitleaks(repo, gitleaks):
                hits.append(Hit("gitleaks", "history", "", "see the gitleaks report above"))
    except (ScanError, OSError) as exc:
        sys.stderr.write(f"PRIVACY_SCAN_ERROR {exc}\n")
        return 2

    for hit in hits:
        shown = hit.value if hit.rule == "gitleaks" else _redact(hit.value)
        print(f"PRIVACY_HIT {hit.rule} {hit.where}: {shown}")
    if hits:
        print(f"PRIVACY_SCAN_HITS n={len(hits)}")
        return 1
    print(f"PRIVACY_SCAN_CLEAN files={files} commits={commits} gitleaks={gitleaks_state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
