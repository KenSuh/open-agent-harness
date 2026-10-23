# git pre-push 훅 — 이 워크트리에 끝나지 않은 리스가 있으면 푸시를 막고, gitleaks 가 있으면 푸시할 커밋만 비밀 검사한다.
"""`oah hook pre-push <remote> <url>`, with git's ref lines on stdin.

1. Refuse while any lease, active or stale, is recorded for this worktree: finish or release it first.
2. When gitleaks is installed, scan exactly the commits being pushed (`<remote sha>..<local sha>`, or
   `<local sha> --not --remotes=<remote>` for a new branch) with redacted output. A finding or a
   gitleaks failure refuses the push. Without gitleaks the scan is reported as `unused(no gitleaks)`.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Mapping, Sequence

from oah.engines.proc import UnsupportedBatchShim, resolve_command, run_command
from oah.lease import Store
from oah.paths import repo_info, state_dir

EXIT_OK, EXIT_REFUSED = 0, 1
ZERO = "0" * 40
GITLEAKS_TIMEOUT = 300.0
GITLEAKS_HINT = "https://github.com/gitleaks/gitleaks#installing"


def find_gitleaks(env: Mapping[str, str]) -> Sequence[str] | None:
    found = resolve_command("gitleaks", env.get("PATH"))
    return found.argv if found is not None else None


def same_worktree(left: str | pathlib.Path, right: str | pathlib.Path) -> bool:
    a, b = os.path.normcase(str(pathlib.Path(left).resolve())), os.path.normcase(str(pathlib.Path(right).resolve()))
    return a == b


def _ranges(stdin_text: str, remote: str) -> list[str]:
    ranges = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if set(local_sha) == {"0"}:
            continue  # a branch deletion pushes no commits
        ranges.append(f"{local_sha} --not --remotes={remote}" if set(remote_sha) == {"0"}
                      else f"{remote_sha}..{local_sha}")
    return ranges


def run(args: argparse.Namespace) -> int:
    hook_args = list(getattr(args, "hook_args", []) or [])
    remote = hook_args[0] if hook_args else "origin"
    stdin_text = sys.stdin.read() if sys.stdin is not None else ""
    info = repo_info(pathlib.Path.cwd())
    leases = [(lease, stale) for lease, stale in Store(state_dir(info, os.environ)).snapshot()
              if same_worktree(lease.worktree, info.root)]
    if leases:
        lease, stale = leases[0]
        label = " (stale)" if stale else ""
        print(f"OAH_PUSH_REFUSED: task {lease.task} held by {lease.agent} is still open on this worktree{label}. "
              f'Finish it with oah finish --task {lease.task} --agent {lease.agent} --summary "<what you did>", '
              f'or drop it with oah release --task {lease.task} --agent {lease.agent} --reason "<why>", '
              "then push again.", file=sys.stderr)
        return EXIT_REFUSED
    try:
        gitleaks = find_gitleaks(os.environ)
    except UnsupportedBatchShim as exc:
        print(f"OAH_GITLEAKS_FAILED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if gitleaks is None:
        print("oah pre-push: gitleaks is not installed, so the pushed commits were not scanned for secrets "
              f"(unused(no gitleaks)). Install it from {GITLEAKS_HINT}", file=sys.stderr)
        return EXIT_OK
    for log_range in _ranges(stdin_text, remote):
        result = run_command([*gitleaks, "detect", "--source", str(info.root), "--log-opts", log_range,
                              "--redact", "--no-banner", "--exit-code", "1"], timeout=GITLEAKS_TIMEOUT)
        if result.returncode == 0:
            continue
        sys.stderr.write(result.stdout + result.stderr)
        if result.returncode == 1:
            print("OAH_PUSH_SECRETS: gitleaks found possible secrets in the commits being pushed (redacted above). "
                  "Remove them from those commits, rotate any real key, then push again.", file=sys.stderr)
        else:
            state = "timed out" if result.timed_out else f"exited {result.returncode}"
            print(f"OAH_GITLEAKS_FAILED: gitleaks {state}; fix the error above, then push again.", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK
