# 가짜 node — unlazy 스크립트 대신 실행되어 받은 인자·표준입력을 기록하고, 미리 정한 출력·종료 코드·파일 쓰기를 순서대로 재생한다.
"""A stand-in for `node <unlazy script> ...` in contract tests.

Environment:
- OAH_STUB_LOG: a JSONL file; each call appends {script, args, stdin, cwd}.
- OAH_STUB_REPLY: a JSON file {"<script name>": [reply, ...]}. Replies are used in order and the last
  one repeats. A reply is {"stdout": str, "stderr": str, "code": int, "write": {path: text}}; `write`
  paths are relative to the working directory and are written before the output is printed.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys


def main() -> int:
    script = pathlib.Path(sys.argv[1]).name if len(sys.argv) > 1 else ""
    args = sys.argv[2:]
    stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    log = os.environ.get("OAH_STUB_LOG")
    calls = 0
    if log:
        path = pathlib.Path(log)
        if path.exists():
            calls = sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                        if json.loads(line)["script"] == script)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"script": script, "args": args, "stdin": stdin, "cwd": os.getcwd()}) + "\n")
    replies = {}
    reply_file = os.environ.get("OAH_STUB_REPLY")
    if reply_file:
        replies = json.loads(pathlib.Path(reply_file).read_text(encoding="utf-8"))
    queue = replies.get(script) or [{"stdout": "", "code": 0}]
    reply = queue[min(calls, len(queue) - 1)]
    for name, text in (reply.get("write") or {}).items():
        target = pathlib.Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
    sys.stdout.write(reply.get("stdout", ""))
    sys.stderr.write(reply.get("stderr", ""))
    return int(reply.get("code", 0))


if __name__ == "__main__":
    sys.exit(main())
