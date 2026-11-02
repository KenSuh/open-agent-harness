# 가짜 Claude Code CLI — --version·--help·-p 를 흉내 내며, 도움말의 모델 별칭·effort 목록과 JSON 결과, --bare 별칭 확인의 답을 FAKE_* 환경 변수대로 낸다.
"""A stand-in for `claude` that tests point the config at.

FAKE_CLAUDE_VERSION   version printed by --version (default 2.1.309)
FAKE_CLAUDE_ALIASES   comma-separated aliases quoted in --help (default fable,opus,sonnet); "-" breaks --help
FAKE_CLAUDE_EFFORTS   comma-separated efforts listed in --help (default low,medium,high,xhigh,max)
FAKE_CLAUDE_REPORT_EFFORT  when set, the JSON result carries this "effort"
FAKE_CLAUDE_BARE      how a `--bare` run (the alias check) answers; it never records or misbehaves:
                      unset: prints nothing and exits 0, so no alias is confirmed
                      real: as Claude Code 2.1.316 with no credential: "Not logged in" and exit 1, and an alias
                            outside FAKE_CLAUDE_KNOWN (default haiku,sonnet,opus,fable) also prints the tag
                      untagged: "Not logged in" and exit 1 for every alias, with no tag
                      all-tagged: "Not logged in", exit 1 and the tag for every alias
                      sleep: as real, but a known alias sleeps 60 s first
                      logged-in: an answer and exit 0, as if a credential were found
                      zero-exit: "Not logged in" but exit 0
FAKE_BARE_RECORD      path: append one JSON line per `--bare` run with its argv, cwd, the cwd's entries, the stdin it
                      read and the environment's names
"""
from __future__ import annotations

import json
import os
import sys
import time

import fakelib

HELP = """Usage: claude [options] [command] [prompt]

Options:
  --effort <level>                      Effort level for the current session
                                        ({efforts})
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        {aliases}) or a
                                        model's full name.
  --permission-mode <mode>              Permission mode to use for the session
"""
TAG = "[claude-code:unrecognized_model]"


def bare(argv: list[str]) -> int:
    """The alias check's run: answer as FAKE_CLAUDE_BARE says, and nothing else."""
    model = argv[argv.index("--model") + 1] if "--model" in argv else ""
    stdin = sys.stdin.buffer.read() if sys.stdin is not None else b""
    path = os.environ.get("FAKE_BARE_RECORD")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "entries": sorted(os.listdir(".")),
                                     "stdin": stdin.decode("latin-1"), "env": sorted(os.environ)}) + "\n")
    mode = os.environ.get("FAKE_CLAUDE_BARE", "")
    known = model in os.environ.get("FAKE_CLAUDE_KNOWN", "haiku,sonnet,opus,fable").split(",")
    if mode == "":
        return 0
    if mode == "logged-in":
        print(json.dumps({"type": "result", "result": "x"}))
        return 0
    if mode == "sleep" and known:
        time.sleep(60)
    if mode == "all-tagged" or (mode in ("real", "sleep", "zero-exit") and not known):
        print(TAG, file=sys.stderr)
    print("Not logged in · Please run /login")
    return 0 if mode == "zero-exit" and known else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print(f"{os.environ.get('FAKE_CLAUDE_VERSION', '2.1.309')} (Claude Code)")
        return 0
    if argv[:1] == ["--help"]:
        aliases = os.environ.get("FAKE_CLAUDE_ALIASES", "fable,opus,sonnet")
        if aliases == "-":
            print("Usage: claude (help text changed)")
            return 0
        quoted = [f"'{alias}'" for alias in aliases.split(",") if alias]
        efforts = os.environ.get("FAKE_CLAUDE_EFFORTS", "low,medium,high,xhigh,max").replace(",", ", ")
        print(HELP.format(aliases=", ".join(quoted), efforts=efforts))
        return 0
    if "--bare" in argv:
        return bare(argv)
    if "-p" not in argv:
        print(f"fake claude: unsupported arguments {argv}", file=sys.stderr)
        return 2
    prompt = fakelib.read_stdin()
    fakelib.record(argv, prompt)
    fakelib.misbehave()
    model = argv[argv.index("--model") + 1] if "--model" in argv else "default"
    result = {"type": "result", "is_error": False, "result": "fake claude done",
              "modelUsage": {f"claude-{model}-fake": {"inputTokens": 1, "outputTokens": 1}}}
    if os.environ.get("FAKE_CLAUDE_REPORT_EFFORT"):
        result["effort"] = os.environ["FAKE_CLAUDE_REPORT_EFFORT"]
    print(json.dumps(result))
    return fakelib.exit_code()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
