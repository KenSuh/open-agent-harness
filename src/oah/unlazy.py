# unlazy 연동 — 설치 위치 찾기(OAH_UNLAZY_DIR, 프로젝트, 사용자 스킬 폴더), 고정 커밋, gate-check·gate-lint·stop-hook 호출 계약을 맡는다.
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Mapping, Sequence

from oah.engines.proc import RunResult, UnsupportedBatchShim, resolve_command, run_command

ENV_VAR = "OAH_UNLAZY_DIR"
INSTALL_HINT = "npx skills add Leonxlnx/unlazy"


def _is_unlazy(folder: pathlib.Path) -> bool:
    return (folder / "scripts" / "gate-check.mjs").is_file()


def find_unlazy_dir(repo_root: pathlib.Path | None, env: Mapping[str, str],
                    home: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """Return (folder, reason). An OAH_UNLAZY_DIR value is the only folder checked."""
    override = env.get(ENV_VAR)
    if override:
        folder = pathlib.Path(override)
        if _is_unlazy(folder):
            return folder, f"from {ENV_VAR}"
        return None, f"{ENV_VAR} has no scripts/gate-check.mjs"
    candidates: list[pathlib.Path] = []
    if repo_root is not None:
        candidates += [repo_root / ".agents" / "skills" / "unlazy", repo_root / ".claude" / "skills" / "unlazy"]
    candidates += [home / ".agents" / "skills" / "unlazy", home / ".claude" / "skills" / "unlazy"]
    for folder in candidates:
        if _is_unlazy(folder):
            return folder, "found"
    return None, f"unlazy not found (install: {INSTALL_HINT})"


def unlazy_version(unlazy_dir: pathlib.Path) -> str | None:
    """The version recorded in unlazy's package.json, or None."""
    try:
        manifest = json.loads((unlazy_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) else None


# The pinned CLI contract ------------------------------------------------------------------------
#
# Written against unlazy at PINNED_COMMIT (package version 2.1.0, an unreleased source target):
#
#   node scripts/gate-check.mjs [--status | --reverify] [--approve] [--timeout S] [--cwd DIR]
#                               [--root DIR] -- <ledger>
#     exit 0 and a line "ALL MET (<n> met...)"                      -> met
#     exit 1 and a line "UNMET: <n> (...)"                          -> unmet (wins over a handoff)
#     exit 1 and a line "HANDOFF REQUIRED: <n> abandoned (...)"     -> handoff
#     exit 2 usage, parse or infrastructure; exit 3 lease conflict  -> error
#     --status never executes, approves or writes; it lists "  UNMET <stem>:<id> (<reason>): <title>".
#     Without an approval a run prints "APPROVAL REQUIRED" and "NOT RUN" and executes nothing.
#   node scripts/gate-lint.mjs [--strict] [--json] -- <ledger>
#     exit 0 "LINT OK" -> met; exit 1 "LINT FINDINGS" (strict) -> unmet; exit 2 -> error.
#   node scripts/stop-hook.mjs   (Claude Code Stop payload on stdin)
#     prints nothing, {"systemMessage": ...} or {"decision": "block", "reason": ...}; always exits 0.
#
# oah passes --approve only when the user asked for it, so running a CHECK always goes through
# unlazy's own approval flow. oah never executes CHECK lines itself.

PINNED_COMMIT = "16671491f6679ad9378f52604d3bc2415b4120c7"
PINNED_VERSION = "2.1.0"
UPSTREAM = "https://github.com/Leonxlnx/unlazy"
NODE_ENV = "OAH_NODE"
NODE_HINT = "install Node.js from https://nodejs.org (the LTS installer), then open a new terminal"
STATUS_TIMEOUT = 120.0
RUN_TIMEOUT = 3600.0
MODES = {"status": "--status", "run": None, "reverify": "--reverify"}


class UnlazyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class UnlazyResult:
    returncode: int | None
    stdout: str
    stderr: str
    verdict: str  # met | unmet | handoff | error


def _summary_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line and not line[0].isspace()]


def parse_verdict(returncode: int | None, stdout: str) -> str:
    """Map gate-check's exit code and summary lines to met, unmet, handoff or error."""
    lines = _summary_lines(stdout)
    if returncode == 0:
        return "met" if any(line.startswith("ALL MET (") for line in lines) and not any(
            line.startswith(("UNMET: ", "HANDOFF REQUIRED: ")) for line in lines) else "error"
    if returncode == 1:
        if any(line.startswith("UNMET: ") for line in lines):
            return "unmet"
        if any(line.startswith("HANDOFF REQUIRED: ") for line in lines):
            return "handoff"
    return "error"


def resolve_node(env: Mapping[str, str]) -> tuple[str, ...] | None:
    """The argv that runs node: OAH_NODE (a JSON list) when set, else node from PATH, else None."""
    override = env.get(NODE_ENV)
    if override:
        try:
            value = json.loads(override)
        except ValueError:
            value = None
        if not (isinstance(value, list) and value and all(isinstance(part, str) and part for part in value)):
            raise UnlazyError("OAH_NODE_INVALID", f"{NODE_ENV} must be a JSON list such as "
                                                  '["C:\\\\Program Files\\\\nodejs\\\\node.exe"]')
        return tuple(value)
    try:
        found = resolve_command("node")
    except UnsupportedBatchShim as exc:
        raise UnlazyError("OAH_NODE_INVALID", str(exc)) from exc
    return found.argv if found is not None else None


def _script(unlazy_dir: pathlib.Path, name: str) -> pathlib.Path:
    script = pathlib.Path(unlazy_dir) / "scripts" / name
    if not script.is_file():
        raise UnlazyError("OAH_UNLAZY_INCOMPLETE", f"{unlazy_dir} has no scripts/{name}; update unlazy "
                                                   f"({INSTALL_HINT}) to commit {PINNED_COMMIT[:12]} or newer")
    return script


def _run(argv: list[str], *, env: Mapping[str, str] | None, timeout: float, cwd: pathlib.Path | None = None,
         stdin_text: str | None = None) -> RunResult:
    try:
        return run_command(argv, timeout=timeout, env=env, cwd=str(cwd) if cwd else None, stdin_text=stdin_text)
    except UnsupportedBatchShim as exc:
        raise UnlazyError("OAH_NODE_INVALID", str(exc)) from exc
    except OSError as exc:
        raise UnlazyError("OAH_NODE_MISSING", f"cannot start {argv[0]}: {exc}; {NODE_HINT}") from exc


def gate_check(unlazy_dir: pathlib.Path, ledger: pathlib.Path, *, node: Sequence[str], mode: str,
               approve: bool = False, cwd: pathlib.Path | None = None, root: pathlib.Path | None = None,
               timeout: int | None = None, env: Mapping[str, str] | None = None,
               process_timeout: float | None = None) -> UnlazyResult:
    """Run gate-check.mjs on one ledger. `mode` is status, run or reverify."""
    if mode not in MODES:
        raise ValueError(f"unknown gate-check mode {mode!r}")
    if mode == "status" and (approve or cwd is not None or timeout is not None):
        raise ValueError("--status takes no --approve, --cwd or --timeout")
    argv = [*node, str(_script(unlazy_dir, "gate-check.mjs"))]
    if MODES[mode]:
        argv.append(MODES[mode])
    if approve:
        argv.append("--approve")
    if timeout is not None:
        argv += ["--timeout", str(int(timeout))]
    if cwd is not None:
        argv += ["--cwd", str(cwd)]
    if root is not None:
        argv += ["--root", str(root)]
    argv += ["--", str(ledger)]
    limit = process_timeout or (STATUS_TIMEOUT if mode == "status" else RUN_TIMEOUT)
    result = _run(argv, env=env, timeout=limit)
    verdict = "error" if result.timed_out else parse_verdict(result.returncode, result.stdout)
    return UnlazyResult(result.returncode, result.stdout, result.stderr, verdict)


def gate_lint(unlazy_dir: pathlib.Path, ledger: pathlib.Path, *, node: Sequence[str], strict: bool = False,
              env: Mapping[str, str] | None = None) -> UnlazyResult:
    """Run gate-lint.mjs on one ledger; it never executes a CHECK."""
    argv = [*node, str(_script(unlazy_dir, "gate-lint.mjs"))] + (["--strict"] if strict else []) + ["--", str(ledger)]
    result = _run(argv, env=env, timeout=STATUS_TIMEOUT)
    lines = _summary_lines(result.stdout)
    if result.returncode == 0 and any(line.startswith("LINT OK") for line in lines):
        verdict = "met"
    elif result.returncode == 1 and any(line.startswith("LINT FINDINGS") for line in lines):
        verdict = "unmet"
    else:
        verdict = "error"
    return UnlazyResult(result.returncode, result.stdout, result.stderr, verdict)


def stop_hook(unlazy_dir: pathlib.Path, payload: Mapping, *, node: Sequence[str],
              env: Mapping[str, str] | None = None, cwd: pathlib.Path | None = None) -> UnlazyResult:
    """Run unlazy's own Stop hook with the Claude Code payload on stdin."""
    argv = [*node, str(_script(unlazy_dir, "stop-hook.mjs"))]
    result = _run(argv, env=env, timeout=STATUS_TIMEOUT, cwd=cwd, stdin_text=json.dumps(dict(payload)))
    verdict = "met" if result.returncode == 0 and not result.timed_out else "error"
    return UnlazyResult(result.returncode, result.stdout, result.stderr, verdict)
