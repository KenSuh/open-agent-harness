# Claude Code Stop 훅 — 이 워크트리의 리스가 가진 원장이 미충족·낡은 동안 멈춤을 막고, 진행 없는 차단은 풀며, 리스가 없으면 unlazy 훅에 넘긴다.
"""`oah hook stop`: the lease-aware Claude Code Stop hook.

It reads the Stop payload on stdin and prints nothing (allow), `{"systemMessage": ...}` (allow with a
note) or `{"decision": "block", "reason": ...}`; it always exits 0.

- When an active lease governs the session's worktree, it blocks while that task's ledgers are unmet,
  invalid or stale against the current files (the same checks as `oah finish`). Abandoned gates do
  not block; they are named as a required handoff.
- A progress guard releases the stop after `max_blocks` blocks in a row with no change in gate state,
  so the hook can never wedge a session. Any internal error also allows the stop, with a note.
- When no lease governs, it hands over to unlazy's own Stop hook (if unlazy is installed and its hook
  is not already configured, in which case Claude Code runs it directly).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from typing import Mapping

from oah import unlazy as unlazy_mod
from oah.atomic import read_json, write_json_atomic
from oah.config import load_config
from oah.gates import guard
from oah.gates.check import GateRefused
from oah.gates.context import DEFAULTS, find_tools
from oah.hook_pre_push import same_worktree
from oah.lease import Store
from oah.paths import NotARepo, repo_info, state_dir

DECISION_KEYS = {"decision", "reason", "systemMessage", "continue", "stopReason", "suppressOutput"}


def _unlazy_hook_configured(root: pathlib.Path, home: pathlib.Path) -> bool:
    for path in (root / ".claude" / "settings.local.json", root / ".claude" / "settings.json",
                 home / ".claude" / "settings.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            groups = data.get("hooks", {}).get("Stop", [])
            commands = [hook.get("command", "") for group in groups for hook in group.get("hooks", [])]
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if any("stop-hook.mjs" in str(command) for command in commands):
            return True
    return False


def _hand_over(payload: Mapping, root: pathlib.Path, env: Mapping[str, str], home: pathlib.Path) -> dict | None:
    tools = find_tools(root, env, home)
    if not tools.available or _unlazy_hook_configured(root, home):
        return None
    result = unlazy_mod.stop_hook(tools.unlazy_dir, payload, node=tools.node, env=env, cwd=root)
    try:
        decision = json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        return None
    if isinstance(decision, dict) and decision and set(decision) <= DECISION_KEYS:
        return decision
    return None


def _counter_path(state: pathlib.Path, session: str) -> pathlib.Path:
    key = hashlib.sha256(session.encode("utf-8")).hexdigest()[:24]
    return state / "hooks" / f"stop-{key}.json"


def _blocks(state: pathlib.Path, session: str, progress: str | None) -> int:
    """Count consecutive blocks with the same gate state; None clears the counter."""
    path = _counter_path(state, session)
    if progress is None:
        if path.exists():
            path.unlink()
        return 0
    record = read_json(path, {})
    blocks = record.get("blocks", 0) + 1 if isinstance(record, dict) and record.get("progress") == progress else 1
    write_json_atomic(path, {"progress": progress, "blocks": blocks})
    return blocks


def _shown(items: list[str], limit: int = 6) -> str:
    return ", ".join(items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")


def decide(payload: Mapping, *, env: Mapping[str, str] | None = None, home: pathlib.Path | None = None,
           max_blocks: int | None = None) -> dict | None:
    """The Stop decision for one payload: None (allow), a systemMessage, or a block."""
    env = os.environ if env is None else env
    home = home or pathlib.Path.home()
    session = str(payload.get("session_id") or payload.get("sessionId") or "anonymous")
    try:
        cwd = pathlib.Path(payload.get("cwd") or os.getcwd())
        try:
            info = repo_info(cwd)
        except NotARepo:
            return None
        state = state_dir(info, env)
        settings = load_config(info.root).section("gates", DEFAULTS)
        max_blocks = settings["stop_max_blocks"] if max_blocks is None else max_blocks
        governing = [lease for lease, stale in Store(state).snapshot()
                     if not stale and same_worktree(lease.worktree, info.root)]
        if not governing:
            return _hand_over(payload, info.root, env, home)
        tools = find_tools(info.root, env, home)
        problems, handoffs, notes, progress = [], [], [], []
        for lease in governing:
            try:
                found = guard.assess(info.root, lease.task, lease.gates, state, tools=tools, env=env,
                                     env_names=settings["input_env"])
            except GateRefused as exc:
                problems.append(f"task {lease.task}: {exc}")
                progress.append(f"{lease.task}:refused")
                continue
            if not found.ledgers:
                continue
            open_items = ([f"invalid {item}" for item in found.invalid] + found.unmet
                          + (found.stale if found.verified else []))
            if open_items:
                problems.append(f"task {lease.task}: {_shown(open_items)} need work. Run oah gates check "
                                f"{lease.task} (inspect the CHECK commands; add --approve to run them)")
            handoffs += [f"{lease.task}:{gate_id}" for gate_id in found.handoff]
            if not found.verified and not open_items:
                notes.append(f"task {lease.task} is met on structural only grounds ({found.reason})")
            progress.append(json.dumps([lease.task, sorted((key, status.states) for key, status in found.statuses.items()),
                                        sorted((key, sorted(fresh.items())) for key, fresh in found.fresh.items()),
                                        found.invalid], sort_keys=True))
        handoff = (f" HANDOFF REQUIRED: abandoned gates {_shown(handoffs)}; name them in your report." if handoffs
                   else "")
        if not problems:
            _blocks(state, session, None)
            message = "; ".join(notes)
            text = (f"oah: {message}." if message else "oah:") + handoff
            return {"systemMessage": text} if message or handoff else None
        fingerprint = hashlib.sha256("\n".join(sorted(progress)).encode("utf-8")).hexdigest()
        count = _blocks(state, session, fingerprint)
        if count > max_blocks:
            return {"systemMessage": f"oah: releasing after {max_blocks} blocks without progress; still open: "
                                     f"{' | '.join(problems)}.{handoff}"}
        return {"decision": "block",
                "reason": f"oah: the leased work is not done. {' | '.join(problems)}. If a gate is genuinely "
                          f"impossible, write ABANDON: <id> <reason> in its ledger.{handoff}"}
    except Exception as exc:  # the hook must never trap a session
        return {"systemMessage": f"oah: the Stop hook could not check the gates ({type(exc).__name__}: {exc}); "
                                 "not blocking. Run oah gates status to see why."}


def run(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}") if sys.stdin is not None else {}
    except ValueError:
        payload = {}
    decision = decide(payload if isinstance(payload, dict) else {})
    if decision:
        print(json.dumps(decision, ensure_ascii=False))
    return 0
