# 가짜 엔진 공용 부분 — 받은 인자·표준입력·작업자 환경을 기록하고, 잠자기·손자 프로세스·종료 코드를 환경 변수로 흉내 낸다.
"""Shared behaviour of the fake engines, steered by FAKE_* environment variables.

FAKE_RECORD   path: write {argv, stdin, cwd, env} as JSON (env holds only the OAH_* worker variables)
FAKE_SLEEP    seconds to sleep before answering
FAKE_CHILD_PIDFILE  path: start a grandchild that sleeps 120 s and write its pid there first
FAKE_EXIT     exit code (default 0)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def record(argv: list[str], stdin_text: str) -> None:
    path = os.environ.get("FAKE_RECORD")
    if not path:
        return
    env = {key: value for key, value in os.environ.items() if key.startswith("OAH_")}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"argv": argv, "stdin": stdin_text, "cwd": os.getcwd(), "env": env}, handle)


def read_stdin() -> str:
    data = sys.stdin.buffer.read() if sys.stdin is not None else b""
    return data.decode("utf-8")


def misbehave() -> None:
    pidfile = os.environ.get("FAKE_CHILD_PIDFILE")
    if pidfile:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(pidfile, "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))
    seconds = float(os.environ.get("FAKE_SLEEP", "0") or 0)
    if seconds:
        time.sleep(seconds)


def exit_code() -> int:
    return int(os.environ.get("FAKE_EXIT", "0") or 0)
