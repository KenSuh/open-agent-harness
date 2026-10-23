# unlazy 연동 — 설치 위치 찾기(OAH_UNLAZY_DIR, 프로젝트, 사용자 스킬 폴더), 고정 커밋·스크립트 다이제스트 검증, gate-check·gate-lint·stop-hook 호출 계약을 맡는다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import shutil
import types
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Mapping, Sequence

from oah.engines.proc import RunResult, UnsupportedBatchShim, resolve_command, run_command

ENV_VAR = "OAH_UNLAZY_DIR"
PINNED_COMMIT = "16671491f6679ad9378f52604d3bc2415b4120c7"
UPSTREAM = "https://github.com/Leonxlnx/unlazy"
UPSTREAM_GIT = UPSTREAM + ".git"
# The only install instruction oah prints: a git checkout of the pinned commit (never an unversioned install).
INSTALL_HINT = (f"git clone {UPSTREAM_GIT} <skills folder>/unlazy, then "
                f"git -C <skills folder>/unlazy checkout --detach {PINNED_COMMIT}")


def _is_unlazy(folder: pathlib.Path) -> bool:
    return (folder / "scripts" / "gate-check.mjs").is_file()


def find_unlazy_dir(repo_root: pathlib.Path | None, env: Mapping[str, str],
                    home: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """Return (folder, reason). An OAH_UNLAZY_DIR value is the only folder checked."""
    override = env.get(ENV_VAR)
    if override:
        folder = pathlib.Path(override).resolve()
        if _is_unlazy(folder):
            return folder, f"from {ENV_VAR}"
        return None, f"{ENV_VAR} has no scripts/gate-check.mjs"
    candidates: list[pathlib.Path] = []
    if repo_root is not None:
        candidates += [repo_root / ".agents" / "skills" / "unlazy", repo_root / ".claude" / "skills" / "unlazy"]
    candidates += [home / ".agents" / "skills" / "unlazy", home / ".claude" / "skills" / "unlazy"]
    for folder in candidates:
        if _is_unlazy(folder):
            return folder.resolve(), "found"
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

PINNED_VERSION = "2.1.0"
NODE_ENV = "OAH_NODE"
NODE_HINT = "install Node.js from https://nodejs.org (the LTS installer), then open a new terminal"
STATUS_TIMEOUT = 120.0
RUN_TIMEOUT = 3600.0
MODES = {"status": "--status", "run": None, "reverify": "--reverify"}


class UnlazyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


# The pin --------------------------------------------------------------------------------------------
#
# A discovered unlazy is used only when it is a git checkout of PINNED_COMMIT and every script oah calls,
# with everything those scripts load, has the pinned sha256 (of LF-normalized bytes, so a Windows
# checkout with core.autocrlf still matches). A new unlazy commit is adopted only by updating PIN here
# and passing the contract tests (tests/test_gates.py, ContractTest and PinnedUnlazyProbeTest).

@dataclass(frozen=True)
class Pin:
    commit: str
    files: Mapping[str, str]  # path inside the unlazy folder -> sha256 of its LF-normalized bytes


PIN = Pin(commit=PINNED_COMMIT, files=types.MappingProxyType({
    "scripts/gate-check.mjs": "29dee3acfdf566c24eedace85163a4e7a66eda01b712c5b4b76a99eae1eda8a1",
    "scripts/gate-lint.mjs": "12fedaa4ad7bcfacf6da8d66f4ead0d186115e03fe941d3d20efd361dec7ee7e",
    "scripts/stop-hook.mjs": "f18f2f189a3be2ff1bcdd01b09eea0ac53edcf9a3f9d9b1ba64817661ce4b683",
    "scripts/lib/gates.mjs": "12758e27c415e619a93148fb71cd217d0c1b81de39fe932b2dda344fac778809",
    "scripts/lib/dispatch.mjs": "101cc06361f2eae77946d5fa1d9dd5dd9effcfc039f376967565951909ad1baf",
    "scripts/lib/process-tree.mjs": "2b6c28613b2686e3afaa32a0196eaa3c67fafa283fbbba11df872f4deb993682",
    "scripts/lib/check-supervisor.mjs": "2bf15a383c0e64f77024820fedf9da8045c02cca9051ec0e6444e85814eb8c3b",
    "scripts/lib/regex-worker.mjs": "76a9448f3c1b15b4904239608bd50381ee609e1edf0c6ac281d3f30473b36e0b",
}))


def default_install_dir(home: pathlib.Path) -> pathlib.Path:
    """Where `oah init` installs unlazy for a user: the user's agent skills folder."""
    return pathlib.Path(home) / ".agents" / "skills" / "unlazy"


def install_commands(target: pathlib.Path, pin: Pin | None = None) -> list[list[str]]:
    """The argv lists that install the pinned unlazy into `target` (run them in order, without a shell)."""
    pin = pin or PIN
    return [["git", "clone", "--quiet", UPSTREAM_GIT, str(target)],
            ["git", "-C", str(target), "checkout", "--quiet", "--detach", pin.commit]]


def install_hint(target: pathlib.Path, pin: Pin | None = None) -> str:
    """The same commands as one line a person can copy."""
    pin = pin or PIN
    return f'git clone {UPSTREAM_GIT} "{target}", then git -C "{target}" checkout --detach {pin.commit}'


def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify_pin(folder: pathlib.Path, pin: Pin | None = None) -> list[str]:
    """Why `folder` is not the pinned unlazy; an empty list means it matches by commit and digests."""
    from oah.paths import GitMissing, run_git

    pin = pin or PIN
    folder = pathlib.Path(folder)
    problems = []
    for relative, digest in pin.files.items():
        path = folder / pathlib.PurePosixPath(relative)
        try:
            actual = file_digest(path)
        except OSError:
            problems.append(f"{relative} is missing")
            continue
        if actual != digest:
            problems.append(f"{relative} differs from the pinned copy")
    try:
        top = run_git(folder, "rev-parse", "--show-toplevel")
        head = run_git(folder, "rev-parse", "--verify", "--quiet", "HEAD")
    except GitMissing:
        return problems + ["git is missing, so its commit cannot be confirmed"]
    top_path = pathlib.Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else None
    if top_path is None or top_path.resolve() != folder.resolve():
        problems.append("it is not a git checkout of its own, so its commit cannot be confirmed")
    elif head.stdout.strip() != pin.commit:
        problems.append(f"it is at commit {head.stdout.strip()[:12] or 'none'}, not the pinned {pin.commit[:12]}")
    return problems


def require_pinned(folder: pathlib.Path, pin: Pin | None = None) -> None:
    """Refuse a discovered unlazy that is not the pinned one, with the command that installs it."""
    pin = pin or PIN
    problems = verify_pin(folder, pin)
    if problems:
        raise UnlazyError("OAH_UNLAZY_UNPINNED", f"{folder} is not the unlazy oah is pinned to "
                                                 f"({'; '.join(problems)}). Move that folder aside, then install "
                                                 f"the pinned copy: {install_hint(folder, pin)}")


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
        found = resolve_command("node", search_path=env.get("PATH", ""))
    except UnsupportedBatchShim as exc:
        raise UnlazyError("OAH_NODE_INVALID", str(exc)) from exc
    return found.argv if found is not None else None


def _script(unlazy_dir: pathlib.Path, name: str) -> pathlib.Path:
    script = pathlib.Path(unlazy_dir).resolve() / "scripts" / name
    if not script.is_file():
        raise UnlazyError("OAH_UNLAZY_INCOMPLETE", f"{unlazy_dir} has no scripts/{name}; install the pinned copy: "
                                                   f"{install_hint(unlazy_dir)}")
    return script


def _run(argv: list[str], *, env: Mapping[str, str] | None, timeout: float, cwd: pathlib.Path | None = None,
         stdin_text: str | None = None) -> RunResult:
    try:
        return run_command(argv, timeout=timeout, env=env, cwd=str(cwd) if cwd else None, stdin_text=stdin_text)
    except UnsupportedBatchShim as exc:
        raise UnlazyError("OAH_NODE_INVALID", str(exc)) from exc
    except OSError as exc:
        raise UnlazyError("OAH_NODE_MISSING", f"cannot start {argv[0]}: {exc}; {NODE_HINT}") from exc


def _private_directory(folder: pathlib.Path) -> None:
    """Create the snapshot with owner-only access, atomically on both supported platforms."""
    if os.name != "nt":
        folder.mkdir(mode=0o700)
        return
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.DWORD))
    convert.restype = wintypes.BOOL
    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    create = kernel.CreateDirectoryW
    create.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes))
    create.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # Protected DACL: only the object's owner has full access; files and subdirectories inherit it.
    if not convert("D:P(A;OICI;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not create(str(folder), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


@contextmanager
def _snapshot(folder: pathlib.Path, env: Mapping[str, str], anchor: pathlib.Path):
    """Copy the entire pinned closure into private state, verify the copy, keep it through execution.

    No copy is reused: each invocation checks its own bytes and removes them after the child exits.
    The discovered paths may change during copying, but only a copy matching every pin can execute.
    """
    from oah.paths import NotARepo, repo_info, run_git, state_dir

    pin = PIN
    require_pinned(folder, pin)
    commit = run_git(folder, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip()
    if env.get("OAH_STATE_DIR"):
        state = pathlib.Path(env["OAH_STATE_DIR"])
    else:
        try:
            info = repo_info(anchor)
        except NotARepo:
            info = repo_info(folder)
        state = state_dir(info, env)
    try:
        state = state.resolve()
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        copy = state / f"unlazy-{secrets.token_hex(16)}"
        _private_directory(copy)
        try:
            for relative in pin.files:
                target = copy / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                # Copy bytes, never symlinks, hard links, git metadata, or unpinned neighboring code.
                with target.open("xb") as handle:
                    target.chmod(0o600)
                    handle.write((folder / relative).read_bytes())
            record = copy / "pin.json"
            record.write_text(json.dumps({"commit": commit}), encoding="utf-8")
            record.chmod(0o600)
            if (json.loads(record.read_text(encoding="utf-8")) != {"commit": pin.commit}
                    or any(file_digest(copy / relative) != digest for relative, digest in pin.files.items())):
                raise UnlazyError("OAH_UNLAZY_UNPINNED", "unlazy changed while being copied; install the pinned "
                                  f"copy and retry: {install_hint(folder, pin)}")
            yield copy
        finally:
            shutil.rmtree(copy)
    except OSError as exc:
        raise UnlazyError("OAH_UNLAZY_SNAPSHOT_FAILED", f"cannot prepare or protect the private unlazy copy: {exc}; "
                          "set OAH_STATE_DIR to a private writable state folder and retry") from exc


def _execute(folder: pathlib.Path, name: str, args: Sequence[str], *, node: Sequence[str],
             env: Mapping[str, str] | None, timeout: float, anchor: pathlib.Path,
             cwd: pathlib.Path | None = None, stdin_text: str | None = None) -> RunResult:
    env = dict(os.environ if env is None else env)
    options = [value for key, value in env.items()
               if (key.upper() if os.name == "nt" else key) == "NODE_OPTIONS"]
    if any(options):
        raise UnlazyError("OAH_NODE_OPTIONS_REFUSED", "Clear NODE_OPTIONS before running gates or the Stop hook; "
                          "unverified Node preloads and runtime flags are not permitted")
    _script(folder, name)  # preserve the missing-entry-point diagnostic
    with _snapshot(pathlib.Path(folder), env, anchor) as copy:
        return _run([*node, str(_script(copy, name)), *args], env=env, timeout=timeout, cwd=cwd,
                    stdin_text=stdin_text)


def gate_check(unlazy_dir: pathlib.Path, ledger: pathlib.Path, *, node: Sequence[str], mode: str,
               approve: bool = False, cwd: pathlib.Path | None = None, root: pathlib.Path | None = None,
               timeout: int | None = None, env: Mapping[str, str] | None = None,
               process_timeout: float | None = None) -> UnlazyResult:
    """Run gate-check.mjs on one ledger. `mode` is status, run or reverify."""
    if mode not in MODES:
        raise ValueError(f"unknown gate-check mode {mode!r}")
    if mode == "status" and (approve or cwd is not None or timeout is not None):
        raise ValueError("--status takes no --approve, --cwd or --timeout")
    argv = []
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
    result = _execute(unlazy_dir, "gate-check.mjs", argv, node=node, env=env, timeout=limit,
                      anchor=root or cwd or ledger.parent)
    verdict = "error" if result.timed_out else parse_verdict(result.returncode, result.stdout)
    return UnlazyResult(result.returncode, result.stdout, result.stderr, verdict)


def gate_lint(unlazy_dir: pathlib.Path, ledger: pathlib.Path, *, node: Sequence[str], strict: bool = False,
              env: Mapping[str, str] | None = None) -> UnlazyResult:
    """Run gate-lint.mjs on one ledger; it never executes a CHECK."""
    argv = (["--strict"] if strict else []) + ["--", str(ledger)]
    result = _execute(unlazy_dir, "gate-lint.mjs", argv, node=node, env=env, timeout=STATUS_TIMEOUT,
                      anchor=ledger.parent)
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
    result = _execute(unlazy_dir, "stop-hook.mjs", (), node=node, env=env, timeout=STATUS_TIMEOUT,
                      anchor=cwd or pathlib.Path(payload.get("cwd") or pathlib.Path.cwd()),
                      cwd=cwd, stdin_text=json.dumps(dict(payload)))
    verdict = "met" if result.returncode == 0 and not result.timed_out else "error"
    return UnlazyResult(result.returncode, result.stdout, result.stderr, verdict)
