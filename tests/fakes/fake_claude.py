# 가짜 Claude Code CLI — --version·--help·-p 를 흉내 내며, 도움말의 모델 별칭·effort 목록과 JSON 결과를 FAKE_* 환경 변수대로 낸다.
"""A stand-in for `claude` that tests point the config at.

FAKE_CLAUDE_VERSION   version printed by --version (default 2.1.309)
FAKE_CLAUDE_ALIASES   comma-separated aliases quoted in --help (default fable,opus,sonnet); "-" breaks --help
FAKE_CLAUDE_EFFORTS   comma-separated efforts listed in --help (default low,medium,high,xhigh,max)
FAKE_CLAUDE_REPORT_EFFORT  when set, the JSON result carries this "effort"
"""
from __future__ import annotations

import json
import os
import sys

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
