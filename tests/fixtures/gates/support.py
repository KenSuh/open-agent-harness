# 게이트 테스트 공용 도우미 — 가짜 node 준비, 가짜 unlazy 폴더, v1 자동 증거가 채워진 원장, 명령 모듈 실행을 맡는다.
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Sequence

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
                   OAH_NODE=json.dumps(list(self.node)), PYTHONPATH=SRC)
        env.update(extra)
        return env

    def calls(self, script: str | None = None) -> list[dict]:
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]
        return [entry for entry in entries if script is None or entry["script"] == script]


def make_unlazy_dir(folder: pathlib.Path) -> pathlib.Path:
    """An unlazy install with the three scripts oah calls (contents unused under the stub)."""
    (folder / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("gate-check.mjs", "gate-lint.mjs", "stop-hook.mjs"):
        (folder / "scripts" / name).write_text("", encoding="utf-8")
    (folder / "package.json").write_text(json.dumps({"name": "unlazy-skill", "version": "2.1.0"}),
                                         encoding="utf-8")
    return folder


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


def run_cli(module: str, argv: Sequence[str], *, cwd: pathlib.Path, env: dict | None = None,
            stdin: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    """Run `oah <argv>` through one command module's register/run, as cli.py discovery will."""
    code = ("import argparse, sys\n"
            "from oah.console import configure_stdio\n"
            f"from oah import {module} as m\n"
            "configure_stdio()\n"
            "p = argparse.ArgumentParser(prog='oah')\n"
            "s = p.add_subparsers(dest='command')\n"
            "m.register(s)\n"
            "sys.exit(m.run(p.parse_args(sys.argv[1:])))\n")
    full_env = dict(env if env is not None else os.environ)
    full_env["PYTHONPATH"] = SRC
    return subprocess.run([sys.executable, "-c", code, *argv], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", env=full_env, input=stdin, timeout=timeout)


def build_args(module, argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="oah")
    module.register(parser.add_subparsers(dest="command"))
    return parser.parse_args(list(argv))
