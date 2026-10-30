# 가짜 Codex CLI — --version 과 exec 만 흉내 내며, 헤더의 reasoning effort 줄과 -o 파일을 FAKE_* 환경 변수대로 쓴다.
"""A stand-in for `codex` that tests point the config at.

FAKE_CODEX_VERSION        version printed by --version (default 0.163.0)
FAKE_CODEX_HEADER_EFFORT  effort printed in the header; "-" omits the line; unset echoes -c
FAKE_CODEX_HEADER_MODEL   model printed in the header; "-" omits the line; unset echoes -m
"""
from __future__ import annotations

import os
import re
import sys

import fakelib


def main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print(f"codex-cli {os.environ.get('FAKE_CODEX_VERSION', '0.163.0')}")
        return 0
    if argv[:1] != ["exec"]:
        print(f"fake codex: unsupported arguments {argv}", file=sys.stderr)
        return 2
    options: dict[str, str] = {}
    configs: list[str] = []
    index = 1
    while index < len(argv):
        item = argv[index]
        if item in ("-C", "-s", "-m", "-o", "--output-schema"):
            options[item] = argv[index + 1]
            index += 2
        elif item == "-c":
            configs.append(argv[index + 1])
            index += 2
        else:
            index += 1
    prompt = fakelib.read_stdin() if argv[-1] == "-" else ""
    fakelib.record(argv, prompt)
    effort = ""
    for config in configs:
        match = re.fullmatch(r'model_reasoning_effort="([a-z]+)"', config)
        if match:
            effort = match.group(1)
    header = os.environ.get("FAKE_CODEX_HEADER_EFFORT", effort)
    model = os.environ.get("FAKE_CODEX_HEADER_MODEL", options.get("-m", ""))
    lines = ["OpenAI Codex v0.163.0 (fake)", "--------", f"workdir: {options.get('-C', '')}"]
    if model != "-":
        lines.append(f"model: {model}")
    lines.append(f"sandbox: {options.get('-s', '')}")
    if header != "-":
        lines.append(f"reasoning effort: {header}")
    lines.append("--------")
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()
    fakelib.misbehave()
    if "-o" in options:
        with open(options["-o"], "w", encoding="utf-8") as handle:
            handle.write("fake last message\n")
    print("fake codex done")
    return fakelib.exit_code()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
