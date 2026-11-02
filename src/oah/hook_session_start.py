# Claude Code SessionStart 훅 — 세션 시작 때 규칙 위치·활성 리스·최근 위키 기록·작업 시작법을 짧게 알리고, oah 가 만든 하위 에이전트 파일을 현재 모델 선택에 맞춰 고치며, 발주된 작업자(OAH_WORKER=1)에게는 아무것도 하지 않는다.
"""`oah hook session-start`: the opt-in Claude Code SessionStart hook (PLAN 5.12, row 11, A6).

Claude Code adds what a SessionStart hook prints to the new session's context, so this prints a short
session-entry guide: where the rules are, the repository's active leases, the newest wiki log entries
and how to start a task. A dispatched worker (`OAH_WORKER=1`) skips any session-entry procedure, so it
gets nothing at all and nothing is written. Outside a repository it prints nothing.

Its one write: each existing `.claude/agents/oah-<tier>.md` whose model or effort is no longer oah's choice is
rewritten in place, and one line says so, but only when the file is exactly what oah generates for the values it
names (`init.agent_check`, LF or CRLF), so an edited file is left alone and a missing one is never created. The
choice comes from the stored catalog and scores (`init.tier_agents`): this step runs no subprocess and reaches no
network. Any failure is one plain line, and the hook always exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import stat
import sys
from typing import Mapping

from oah.atomic import write_text_atomic
from oah.config import load_config
from oah.init import _preview_text
from oah.lease import Store
from oah.paths import GitMissing, NotARepo, repo_info, state_dir
from oah.wiki.base import DEFAULTS as WIKI_DEFAULTS
from oah.wiki.log import parse_log

MAX_LEASES = 5
MAX_SCOPE = 3
LOG_ENTRIES = 2


def lines(cwd: pathlib.Path, env: Mapping[str, str]) -> list[str]:
    """The guide for the repository that contains cwd; empty outside a repository."""
    try:
        info = repo_info(pathlib.Path(cwd))
    except (NotARepo, GitMissing, OSError):
        return []
    out = ["oah: this repository uses open-agent-harness. Its rules are the oah block in AGENTS.md, which "
           "CLAUDE.md imports; follow them."]
    out += _leases(info, env)
    out += _log(info.root)
    out.append("Before you edit: run `oah status` and `oah preflight`, then take a lease with "
               "`oah start --task <task> --agent <you> --scope <paths>`.")
    guide = [line if _preview_text(line.encode("utf-8")) is not None else
             "OAH_SESSION_START_UNSAFE: omitted unsafe session guidance; remove private or control data "
             "from the source entry before starting another session." for line in out]
    return guide + agent_lines(info, env)  # built from fixed paths, plain model names and checked score labels


def _leases(info, env: Mapping[str, str]) -> list[str]:
    try:
        entries = Store(state_dir(info, env)).snapshot()
    except Exception as exc:  # a broken state folder must not break the session
        code = getattr(exc, "code", None) or type(exc).__name__
        return [f"Active leases: unreadable ({code}); run `oah status` to see why."]
    if not entries:
        return ["Active leases: none."]
    out = [f"Active leases ({len(entries)}):"]
    for lease, stale in entries[:MAX_LEASES]:
        when = f"STALE, expired {lease.expires_at}" if stale else f"expires {lease.expires_at}"
        more = f",+{len(lease.scope) - MAX_SCOPE} more" if len(lease.scope) > MAX_SCOPE else ""
        out.append(f"- task={lease.task} agent={lease.agent} scope={','.join(lease.scope[:MAX_SCOPE])}{more} ({when})")
    if len(entries) > MAX_LEASES:
        out.append(f"- and {len(entries) - MAX_LEASES} more; `oah status` lists them all.")
    return out


def _log(root: pathlib.Path) -> list[str]:
    try:
        folder = str(load_config(root).section("wiki", WIKI_DEFAULTS)["dir"])
        entries = parse_log((root / folder / "log.md").read_bytes(), "log.md").entries[:LOG_ENTRIES]
    except Exception:  # no wiki, an unreadable log or a broken config: say nothing about the log
        return []
    if not entries:
        return []
    where = pathlib.PurePosixPath(folder.replace("\\", "/")) / "log.md"
    return [f"Newest wiki log entries ({where}):"] + [f"- {entry.date} {entry.title}" for entry in entries]


def _regular(path: pathlib.Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)  # never through a link
    except OSError:
        return False


def _shown(name: str) -> str:
    from oah.models import PLAIN_MODEL

    return name if PLAIN_MODEL.fullmatch(name) else "a named model"


def _label(scores: str) -> str:
    return scores if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", scores) else "unknown"


def agent_lines(info, env: Mapping[str, str]) -> list[str]:
    """Bring each generated `.claude/agents/oah-<tier>.md` to the current model and effort; one line per change."""
    from oah import call, init, models, routing

    folder = pathlib.Path(info.root) / init.AGENTS_DIR
    out: list[str] = []
    try:
        present = [tier for tier in routing.TIERS if _regular(folder / f"oah-{tier}.md")]
        if not present:
            return out
        config = load_config(info.root)
        section = config.section("models", models.DEFAULTS)
        expected = init.tier_agents(config.section("engines", call.ENGINE_DEFAULTS), profile=section["profile"],
                                    models_section=section, state_dir=state_dir(info, env))
        for tier in present:
            relative = f"{init.AGENTS_DIR}/oah-{tier}.md"
            path = folder / f"oah-{tier}.md"
            data = path.read_bytes()
            found, update = init.agent_check(tier, data, expected.files[relative])
            if found != "stale":
                continue  # current, or edited by the user: `oah doctor` names an edited file
            (old_model, old_effort), (model, effort) = init.agent_values(data), init.agent_values(update)
            write_text_atomic(path, update.decode("utf-8"), retry_seconds=0)
            what = f"model {_shown(old_model)} -> {_shown(model)}"
            if effort != old_effort:
                what += f", effort {_shown(old_effort)} -> {_shown(effort)}"
            out.append(f"oah: updated {relative}: {what} (scores {_label(expected.scores)})")
    except Exception as exc:  # the hook must never block or break a session
        out.append(f"oah: could not update the oah subagent files in {init.AGENTS_DIR} ({type(exc).__name__}); "
                   "run `oah doctor` to see the manual step")
    return out


def _payload_cwd() -> pathlib.Path | None:
    """The session's folder from Claude Code's JSON payload on stdin; a terminal is never read."""
    stdin = sys.stdin
    if stdin is None or stdin.isatty():
        return None
    try:
        payload = json.loads(stdin.read() or "{}")
    except (OSError, ValueError):
        return None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return pathlib.Path(cwd) if isinstance(cwd, str) and cwd else None


def run(args: argparse.Namespace) -> int:
    if os.environ.get("OAH_WORKER") == "1":
        return 0  # dispatched workers skip any session-entry procedure (A6)
    try:
        for line in lines(_payload_cwd() or pathlib.Path.cwd(), os.environ):
            print(line)
    except Exception:  # the hook must never block or break a session
        pass
    return 0
