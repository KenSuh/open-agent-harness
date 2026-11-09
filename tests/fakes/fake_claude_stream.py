# 스킬 참조 스트림 가짜 Claude — 브리프가 가리키는 스킬 참조 파일을 Read 도구 이벤트로 읽은 것처럼 stream-json 을 낸다.
"""A stand-in for `claude -p --output-format stream-json --verbose` that reads the brief's skill references.

Every other invocation (version, help, the json result mode) goes to fake_claude. For a stream run it records argv and
the prompt as fake_claude does, prints an init event, then for each `Read `<path>` in full with your Read tool` line of
the brief a Read tool_use and its tool_result in 2,000-line slices, and finally the result event.

FAKE_SKILL_READS   full (default): read every referenced file; none: read nothing; no-result: read nothing and leave
                   out the result event
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_claude  # noqa: E402
import fakelib  # noqa: E402


def _reads(prompt: str) -> list[dict]:
    out, n = [], 0
    for path in re.findall(r"^Read `([^`]+)` in full with your Read tool", prompt, re.M):
        path = os.path.abspath(path)
        with open(path, "rb") as handle:
            lines = handle.read().decode("utf-8").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for start in range(1, len(lines) + 1, 2000):
            chunk = lines[start - 1:start + 1999]
            n += 1
            out.append({"type": "assistant", "session_id": "s1", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_use", "id": f"t{n}", "name": "Read", "input": {"file_path": path}}]}})
            out.append({"type": "user", "session_id": "s1", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{n}",
                 "content": "\n".join(f"{i:6d}→{line}" for i, line in enumerate(chunk, start))}]},
                "tool_use_result": {"type": "text", "file": {"filePath": path, "content": "\n".join(chunk),
                                                             "numLines": len(chunk), "startLine": start,
                                                             "totalLines": len(lines)}}})
    return out


def main(argv: list[str]) -> int:
    if "-p" not in argv or "stream-json" not in argv:
        return fake_claude.main(argv)
    prompt = fakelib.read_stdin()
    fakelib.record(argv, prompt)
    mode = os.environ.get("FAKE_SKILL_READS", "full")
    out = [{"type": "system", "subtype": "init", "session_id": "s1", "cwd": os.getcwd(), "tools": ["Read", "Grep"],
            "model": "fake"}]
    if mode == "full":
        out.extend(_reads(prompt))
    if mode != "no-result":
        out.append({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s1",
                    "modelUsage": {"claude-opus-fake": {"inputTokens": 1, "outputTokens": 1}}})
    for event in out:
        print(json.dumps(event))
    return fakelib.exit_code()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
