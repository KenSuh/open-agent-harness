# 훅 설치기 — Claude Code 설정과 git 훅에 oah hook <이름> 을 넣고 빼며, 바뀔 내용을 먼저 보여 주고 원자적으로 쓰고 백업을 남긴다.
"""The one installer for oah's opt-in hooks.

Claude Code hooks go into `.claude/settings.local.json` (untracked) by default, or
`.claude/settings.json` with `shared=True`. Each entry is exactly
`{"type": "command", "command": "oah hook <kind>"}`, so a shared install holds no absolute path. Git
hooks are small `#!/bin/sh` scripts (Git for Windows runs them with its own sh) in the folder git
reports for `hooks`, which honours `core.hooksPath`.

Every change is computed first and returned as a unified diff; `dry_run=True` writes nothing. Before
oah changes an existing file it keeps a copy as `<file>.oah.bak`. A foreign git hook is replaced only
with `force=True`, and uninstall puts it back. Installing a hook whose `oah hook <kind>` command this
oah version does not provide is refused, so a git hook can never block on a missing command.
"""
from __future__ import annotations

import dataclasses
import difflib
import importlib.util
import json
import os
import pathlib
import stat
from typing import Callable

from oah.atomic import unlink_durable, write_text_atomic
from oah.paths import run_git

CLAUDE_KINDS = {
    "stop": ("Stop", None),
    "session-start": ("SessionStart", None),
    "impact-hint": ("PreToolUse", "Edit|MultiEdit|Write|NotebookEdit"),
}
GIT_KINDS = ("pre-commit", "post-commit", "post-merge", "pre-push")
KINDS = (*CLAUDE_KINDS, *GIT_KINDS)
BACKUP_SUFFIX = ".oah.bak"
MARKER = "# oah-managed hook"


class HookError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclasses.dataclass(frozen=True)
class Change:
    kind: str
    path: pathlib.Path
    action: str  # install | uninstall | restore | unchanged
    diff: str
    backup: pathlib.Path | None = None


@dataclasses.dataclass(frozen=True)
class Row:
    kind: str
    path: pathlib.Path
    state: str  # installed | absent | foreign


def command_for(kind: str) -> str:
    return f"oah hook {kind}"


def hook_available(kind: str) -> bool:
    """True when this oah provides the module behind `oah hook <kind>`."""
    return importlib.util.find_spec(f"oah.hook_{kind.replace('-', '_')}") is not None


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise HookError("OAH_HOOK_KIND_UNKNOWN", f"{kind!r} is not a hook oah installs; choose from {', '.join(KINDS)}")


def _diff(path: pathlib.Path, before: str | None, after: str | None, root: pathlib.Path) -> str:
    try:
        label = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        label = path.name
    lines = difflib.unified_diff((before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
                                 fromfile=f"a/{label}" if before is not None else "/dev/null",
                                 tofile=f"b/{label}" if after is not None else "/dev/null")
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _read_text(path: pathlib.Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise HookError("OAH_HOOKS_SETTINGS_INVALID", f"{path} is not UTF-8 ({exc.reason}); fix it by hand") from exc


# Claude Code settings ------------------------------------------------------------------------------

def settings_path(root: pathlib.Path, shared: bool) -> pathlib.Path:
    return pathlib.Path(root) / ".claude" / ("settings.json" if shared else "settings.local.json")


def _load_settings(path: pathlib.Path) -> tuple[str | None, dict]:
    text = _read_text(path)
    if text is None:
        return None, {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise HookError("OAH_HOOKS_SETTINGS_INVALID", f"{path} is not valid JSON ({exc}); fix it, then retry. "
                                                      "oah left it untouched") from exc
    hooks = data.get("hooks", {}) if isinstance(data, dict) else None
    valid = (isinstance(data, dict) and isinstance(hooks, dict)
             and all(isinstance(groups, list) and all(isinstance(group, dict) for group in groups)
                     for groups in hooks.values()))
    if not valid:
        raise HookError("OAH_HOOKS_SETTINGS_INVALID", f"{path} does not have the settings shape Claude Code uses "
                                                      "(an object whose hooks map events to lists); oah left it "
                                                      "untouched")
    return text, data


def _render(data: dict, like: str | None) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return text.replace("\n", "\r\n") if like is not None and "\r\n" in like else text


def _is_ours(hook: object, kind: str) -> bool:
    return isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == command_for(kind)


def _claude_installed(data: dict, kind: str) -> bool:
    event, _ = CLAUDE_KINDS[kind]
    return any(_is_ours(hook, kind) for group in data.get("hooks", {}).get(event, [])
               for hook in group.get("hooks", []) if isinstance(group.get("hooks"), list))


def _write_settings(path: pathlib.Path, before: str | None, after: str | None) -> pathlib.Path | None:
    backup = None
    if before is not None:
        # Keep the file as it was before oah first changed it; later installs do not overwrite that copy.
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if not backup.exists():
            write_text_atomic(backup, before)
    if after is None:
        unlink_durable(path)
    else:
        write_text_atomic(path, after)
    return backup


def _claude_install(kind: str, root: pathlib.Path, dry_run: bool, shared: bool) -> Change:
    path = settings_path(root, shared)
    before, data = _load_settings(path)
    if _claude_installed(data, kind):
        return Change(kind, path, "unchanged", "")
    event, matcher = CLAUDE_KINDS[kind]
    group: dict = {"matcher": matcher} if matcher else {}
    group["hooks"] = [{"type": "command", "command": command_for(kind)}]
    data.setdefault("hooks", {}).setdefault(event, []).append(group)
    after = _render(data, before)
    change = Change(kind, path, "install", _diff(path, before, after, root))
    if dry_run:
        return change
    return dataclasses.replace(change, backup=_write_settings(path, before, after))


def _claude_uninstall(kind: str, root: pathlib.Path, dry_run: bool, shared: bool) -> Change:
    path = settings_path(root, shared)
    before, data = _load_settings(path)
    if not _claude_installed(data, kind):
        return Change(kind, path, "unchanged", "")
    event, _ = CLAUDE_KINDS[kind]
    groups = []
    for group in data["hooks"][event]:
        kept = [hook for hook in group.get("hooks", []) if not _is_ours(hook, kind)]
        if kept or not group.get("hooks"):
            groups.append(dict(group, hooks=kept) if "hooks" in group else group)
    if groups:
        data["hooks"][event] = groups
    else:
        del data["hooks"][event]
    if not data["hooks"]:
        del data["hooks"]
    after = _render(data, before) if data else None
    change = Change(kind, path, "uninstall", _diff(path, before, after, root))
    if dry_run:
        return change
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    previous = _read_text(backup) if backup.exists() else None
    _write_settings(path, None, after)
    # The backup is no longer needed once the file is back to what it held before oah touched it.
    try:
        restored = (json.loads(previous) if previous is not None else None) == (data if data else None)
    except ValueError:
        restored = False
    if previous is not None and restored:
        unlink_durable(backup)
    return change


# Git hooks -----------------------------------------------------------------------------------------

def git_hook_path(root: pathlib.Path, kind: str) -> pathlib.Path:
    result = run_git(pathlib.Path(root), "rev-parse", "--git-path", f"hooks/{kind}")
    if result.returncode != 0 or not result.stdout.strip():
        raise HookError("OAH_HOOK_GIT_FAILED", f"git could not name the hooks folder: {result.stderr.strip()}")
    path = pathlib.Path(result.stdout.strip())
    return path if path.is_absolute() else pathlib.Path(root) / path


def git_hook_text(kind: str) -> str:
    return (f"#!/bin/sh\n{MARKER}: runs `{command_for(kind)}`. Remove it with `oah hooks uninstall {kind}`.\n"
            f'exec {command_for(kind)} "$@"\n')


def _git_state(text: str | None) -> str:
    if text is None:
        return "absent"
    return "installed" if MARKER in text else "foreign"


def _git_install(kind: str, root: pathlib.Path, dry_run: bool, force: bool) -> Change:
    path = git_hook_path(root, kind)
    before = _read_text(path)
    after = git_hook_text(kind)
    if before == after:
        return Change(kind, path, "unchanged", "")
    backup = None
    if _git_state(before) == "foreign":
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if not force:
            raise HookError("OAH_HOOK_FOREIGN", f"{path} already holds a {kind} hook that oah did not write. Pass "
                                                "--force to replace it; oah keeps it as a backup and puts it back "
                                                f"on oah hooks uninstall {kind}")
        if backup.exists():
            raise HookError("OAH_HOOK_BACKUP_EXISTS", f"{backup} already exists; move it away first so no hook is lost")
    change = Change(kind, path, "install", _diff(path, before, after, root), backup)
    if dry_run:
        return change
    if backup is not None:
        write_text_atomic(backup, before)
        _make_executable(backup)
    write_text_atomic(path, after)
    _make_executable(path)
    return change


def _make_executable(path: pathlib.Path) -> None:
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git_uninstall(kind: str, root: pathlib.Path, dry_run: bool) -> Change:
    path = git_hook_path(root, kind)
    before = _read_text(path)
    if _git_state(before) != "installed":
        return Change(kind, path, "unchanged", "")
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    restored = _read_text(backup) if backup.exists() else None
    change = Change(kind, path, "restore" if restored is not None else "uninstall",
                    _diff(path, before, restored, root), backup if restored is not None else None)
    if dry_run:
        return change
    if restored is not None:
        write_text_atomic(path, restored)
        _make_executable(path)
        unlink_durable(backup)
    else:
        unlink_durable(path)
    return change


# Public API ----------------------------------------------------------------------------------------

def install(kind: str, root: pathlib.Path, *, dry_run: bool, shared: bool = False, force: bool = False,
            available: Callable[[str], bool] = hook_available) -> Change:
    """Install one hook. Returns the change (with its diff); writes nothing when dry_run is true."""
    _check_kind(kind)
    if not available(kind):
        raise HookError("OAH_HOOK_UNAVAILABLE", f"this oah does not provide `{command_for(kind)}` yet, so the hook "
                                                "would fail; update oah and try again")
    root = pathlib.Path(root)
    if kind in CLAUDE_KINDS:
        return _claude_install(kind, root, dry_run, shared)
    return _git_install(kind, root, dry_run, force)


def uninstall(kind: str, root: pathlib.Path, *, dry_run: bool, shared: bool = False) -> Change:
    """Remove one hook, leaving every other handler in place."""
    _check_kind(kind)
    root = pathlib.Path(root)
    if kind in CLAUDE_KINDS:
        return _claude_uninstall(kind, root, dry_run, shared)
    return _git_uninstall(kind, root, dry_run)


def status(root: pathlib.Path, *, shared: bool = False) -> list[Row]:
    rows = []
    root = pathlib.Path(root)
    path = settings_path(root, shared)
    _, data = _load_settings(path)
    for kind in CLAUDE_KINDS:
        rows.append(Row(kind, path, "installed" if _claude_installed(data, kind) else "absent"))
    for kind in GIT_KINDS:
        hook = git_hook_path(root, kind)
        rows.append(Row(kind, hook, _git_state(_read_text(hook))))
    return rows
