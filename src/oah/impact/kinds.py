# 파일 종류 분류 — 저장소 기준 POSIX 경로를 glob 규칙으로 workflow·migration·test·manifest·config·doc·code 로 나눈다.
from __future__ import annotations

import functools
import re
from typing import Sequence

# First match wins, so the order matters: a workflow YAML is not "config", a SQL migration is not "code".
DEFAULT_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workflow", (".github/workflows/**", ".gitlab-ci.yml", ".circleci/**", "azure-pipelines.yml",
                  "Jenkinsfile", "Makefile", "justfile", "noxfile.py", "tox.ini")),
    ("migration", ("**/migrations/**", "**/migrate/**", "**/*.sql")),
    ("test", ("tests/**", "test/**", "**/tests/**", "**/test_*.py", "**/*_test.*", "**/*.test.*",
              "**/*.spec.*")),
    ("manifest", ("pyproject.toml", "setup.py", "setup.cfg", "uv.lock", "**/package.json",
                  "**/package-lock.json", "**/Cargo.toml", "**/go.mod", "**/requirements*.txt",
                  "**/Gemfile", "**/pom.xml", "**/build.gradle*", "**/*.lock")),
    ("config", ("**/*.toml", "**/*.ini", "**/*.cfg", "**/*.yaml", "**/*.yml", "**/*.json", "**/.*rc",
                "**/*.conf")),
    ("doc", ("**/*.md", "**/*.rst", "**/*.txt", "**/*.adoc", "docs/**")),
)
FALLBACK = "code"


@functools.lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pattern: str, path: str) -> bool:
    """Case-sensitive match of a repository-relative POSIX path; `**` spans folders, `*` does not."""
    return _compile(pattern).match(path) is not None


def classify(path: str, kinds: Sequence[Sequence] = DEFAULT_KINDS) -> str:
    for kind, patterns in kinds:
        if any(glob_match(pattern, path) for pattern in patterns):
            return kind
    return FALLBACK
