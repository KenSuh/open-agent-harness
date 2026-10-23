# 실행기 선택 — oah call 이 준비된 실행을 어디서 돌릴지(기본 process, 선택 orca)를 플래그·설정으로 고르며 조용히 바꾸지 않는다.
"""Where `oah call` runs a prepared call (PLAN section 4 row 8, 5.5).

- `process` (the default): P3b's foreground run or detached background runner, unchanged.
- `orca`: the same prepared run inside an Orca worktree terminal (`oah.launchers.orca`).

`[call] launcher` in `.oah/config.toml` picks one, and `oah call --launcher` overrides it for one call.
A selected launcher that cannot run refuses; it never falls back to another one.
"""
from __future__ import annotations

import time
from typing import Mapping

from oah.engines.effort import EngineError

NAMES = ("process", "orca")
DEFAULTS = {"launcher": "process", "orca_command": ["orca"]}


class DeadlineExpired(Exception):
    """No subprocess may start after the caller's monotonic deadline."""


def remaining(deadline: float | None, cap: float) -> float:
    timeout = cap if deadline is None else min(cap, deadline - time.monotonic())
    if timeout <= 0:
        raise DeadlineExpired()
    return timeout


def choose(flag: str | None, section: Mapping) -> str:
    """The launcher for this call: the flag when given, else the configured one."""
    name = flag or section["launcher"]
    if name not in NAMES:
        raise EngineError("OAH_CONFIG_INVALID", f".oah/config.toml: call.launcher = {name!r}; use one of: "
                          f"{', '.join(NAMES)}")
    return name
