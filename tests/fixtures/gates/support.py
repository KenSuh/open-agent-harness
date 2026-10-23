# 게이트 테스트 공용 도우미 — 가짜 node 준비, 가짜 unlazy 폴더, v1 자동 증거가 채워진 원장, oah 명령 실행을 맡는다.
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
from typing import Sequence
from unittest.mock import patch

from oah.gates.ledger import definition_digest, parse_text

HERE = pathlib.Path(__file__).resolve().parent
STUB = HERE / "stub_node.py"
SRC = str(HERE.parent.parent.parent / "src")


class StubNode:
    """A fake `node` that records calls and replays canned unlazy output."""

    def __init__(self, folder: pathlib.Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.log = folder / "stub-calls.jsonl"
        self.reply_file = folder / "stub-replies.json"
        self.replies: dict[str, list[dict]] = {}
        self.reply_file.write_text("{}", encoding="utf-8")
        self.node = (sys.executable, str(STUB))

    def reply(self, script: str, *replies: dict) -> None:
        self.replies[script] = list(replies)
        self.reply_file.write_text(json.dumps(self.replies), encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ, OAH_STUB_LOG=str(self.log), OAH_STUB_REPLY=str(self.reply_file),
                   OAH_NODE=json.dumps(list(self.node)), PYTHONPATH=SRC,
                   OAH_STATE_DIR=str(self.log.parent / "state"))
        env.update(extra)
        return env

    def calls(self, script: str | None = None) -> list[dict]:
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]
        return [entry for entry in entries if script is None or entry["script"] == script]


def make_unlazy_dir(folder: pathlib.Path) -> pathlib.Path:
    """A stand-in unlazy: a git checkout holding every pinned script path (contents unused under the stub)."""
    from oah.unlazy import PIN
    from tests.helpers import git_commit

    files = {relative: f"// stand-in for {relative}\n" for relative in PIN.files}
    files["package.json"] = json.dumps({"name": "unlazy-skill", "version": "2.1.0"})
    subprocess.run(["git", "init", "-q", "-b", "main", str(folder)], check=True)
    git_commit(folder, files, "stand-in unlazy")
    return folder


def stub_pin(folder: pathlib.Path):
    """A pin that the stand-in unlazy at `folder` satisfies (its HEAD and its script digests)."""
    from oah.unlazy import PIN, Pin, file_digest

    head = subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    return Pin(commit=head, files={relative: file_digest(folder / relative) for relative in PIN.files})


def automatic_evidence(check: str, expect: str, cwd: str | None = None) -> str:
    digest = definition_digest(check, expect, cwd)
    return (f"automatic-evidence=v1; definition-sha256={digest}; exit=0; EXPECT=matched; "
            f"output-sha256={'a' * 64}; output-bytes=12; shell=/bin/sh; cwd=.; path=x")


def met_ledger(text: str, *, skip: Sequence[str] = ()) -> str:
    """The same ledger with every runnable gate (except `skip`) checked and carrying v1 evidence."""
    ledger = parse_text(text)
    lines = text.splitlines(keepends=True)
    for gate in ledger.gates:
        if gate.check is None or gate.id in skip:
            continue
        lines[gate.line] = lines[gate.line].replace("- [ ]", "- [x]", 1)
        evidence = lines[gate.evidence_line]
        indent = evidence[: len(evidence) - len(evidence.lstrip())]
        ending = "\r\n" if evidence.endswith("\r\n") else "\n"
        lines[gate.evidence_line] = f"{indent}EVIDENCE: {automatic_evidence(gate.check, gate.expect, gate.cwd)}{ending}"
    return "".join(lines)


def run_cli(argv: Sequence[str], *, cwd: pathlib.Path, env: dict | None = None, stdin: str | None = None,
            pin=None) -> subprocess.CompletedProcess:
    """Run `oah <argv>` in this process through the real command discovery.

    In-process so that a test can stand in for the unlazy pin (`pin`); environment, working directory,
    stdin, stdout and stderr are isolated for the call.
    """
    from oah import cli, unlazy

    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, dict(env if env is not None else os.environ), clear=True))
        stack.enter_context(patch("sys.stdin", io.StringIO(stdin or "")))
        if pin is not None:
            stack.enter_context(patch.object(unlazy, "PIN", pin))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        os.chdir(cwd)
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
        finally:
            os.chdir(previous)
    return subprocess.CompletedProcess(list(argv), code, out.getvalue(), err.getvalue())


def build_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse `oah <argv>` with the real command discovery."""
    from oah.cli import build_parser

    return build_parser().parse_args(list(argv))
