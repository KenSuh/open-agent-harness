# 리뷰 설정 — [review] 의 기본값(라운드 상한 없음, 라운드별 시도 2회, 리뷰어 해시 고정 없음, 중요 경로)과 값 모양 검사를 맡는다.
"""`[review]` in `.oah/config.toml` (PLAN 5.9, A22). Lanes P6b and P6c add their own keys to `DEFAULTS`.

- `max_rounds`: unset by default, so a series has no ceiling. When set (a whole number of at least 1), a
  series that reached it stops with `OAH_REVIEW_ROUNDS_EXHAUSTED` until `--extra-round`.
- `round_attempts`: how many dispatched attempts one round may use when they do not count (invalid output,
  missing canary, effort mismatch, engine failure). Default 2.
- `codex_sha256`: pin the reviewer CLI file's sha256 (`sha256:` prefix optional). Empty means not pinned. The
  pinned file is the resolved CLI entry: for an npm install that is the JavaScript launcher, not the native
  program it starts.
- `critical_paths`: globs whose changes the prompt (and `oah review summary`) call out. The defaults include
  `.oah/config.toml`, because it configures the review itself.

Reviewer policy comes from the configuration committed at the review base (`from_config`). The task's
max_rounds budget also observes the current worktree and saved limits; `budget_limit` reads that field
independently of reviewer policy. `load(root)` reads a worktree's reviewer settings.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
from typing import Mapping

from oah.config import Config, ConfigError, load_config

DEFAULTS: dict[str, object] = {
    "max_rounds": None,
    "round_attempts": 2,
    "codex_sha256": "",
    "critical_paths": [".github/workflows/**", "**/AGENTS.md", "**/AGENTS.override.md", "**/CLAUDE.md",
                       "**/CLAUDE.local.md", "**/.claude/**", "**/.codex/**", "**/.agents/**", "**/migrations/**",
                       ".oah/config.toml"],
}
_HEX = re.compile(r"[0-9a-f]{64}")


@dataclasses.dataclass(frozen=True)
class ReviewSettings:
    max_rounds: int | None
    round_attempts: int
    codex_sha256: str | None
    critical_paths: tuple[str, ...]


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def budget_limit(config: Config) -> int | None:
    """Read only the task budget; reviewer policy remains pinned to the review base."""
    section = config.raw.get("review", {})
    if not isinstance(section, Mapping):
        raise ConfigError("OAH_CONFIG_INVALID", "review must be a table; use [review] in .oah/config.toml "
                          "and retry")
    value = section.get("max_rounds")
    if value is not None and not _count(value):
        raise ConfigError("OAH_CONFIG_INVALID", "review.max_rounds must be a whole number of at least 1; "
                          "fix it in .oah/config.toml or remove it for no ceiling")
    return value


def from_section(section: Mapping[str, object], where: str = ".oah/config.toml") -> ReviewSettings:
    """Check the shapes the config loader's type check cannot see."""
    def invalid(key: str, what: str) -> ConfigError:
        return ConfigError("OAH_CONFIG_INVALID", f"{where}: review.{key} must be {what}; fix it, or remove it "
                           "to use the default")

    rounds = section["max_rounds"]
    if rounds is not None and not _count(rounds):
        raise invalid("max_rounds", "a whole number of at least 1 (leave it out for no ceiling)")
    if not _count(section["round_attempts"]):
        raise invalid("round_attempts", "a whole number of at least 1")
    pin = section["codex_sha256"]
    if not isinstance(pin, str):
        raise invalid("codex_sha256", "a string")
    pin = pin.strip().lower().removeprefix("sha256:")
    if pin and not _HEX.fullmatch(pin):
        raise invalid("codex_sha256", "the 64 hex digits of the reviewer CLI's sha256, or empty")
    paths = section["critical_paths"]
    if not isinstance(paths, (list, tuple)) or not all(isinstance(item, str) and item for item in paths):
        raise invalid("critical_paths", "an array of glob strings")
    return ReviewSettings(max_rounds=rounds, round_attempts=section["round_attempts"], codex_sha256=pin or None,
                          critical_paths=tuple(paths))


def from_config(config: Config) -> ReviewSettings:
    return from_section(config.section("review", DEFAULTS), config.where)


def load(root: pathlib.Path) -> ReviewSettings:
    return from_config(load_config(pathlib.Path(root)))
