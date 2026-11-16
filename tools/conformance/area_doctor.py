# doctor 영역: 로그인 판정·버전 줄 읽기·도구 상태 결정표와 JSON 출력의 도구 행을 가짜 해석기·실행기로 사례로 만든다.
"""Area `doctor`: signed_in(), version lines, the tool status decision table and the JSON tool rows."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

from fixture import Area, Case, fn, ok

VERSIONS = {"git": "git version 2.50.1", "uv": "uv 0.11.7", "node": "v24.14.0", "claude": "2.1.318 (Claude Code)",
            "codex": "codex-cli 0.163.0", "graphify": "graphify 0.9.93", "gh": "gh version 2.91.0 (2026-04-22)",
            "gitleaks": "gitleaks version 8.30.1", "jq": "jq-1.8.1", "rg": "ripgrep 14.1.1", "orca": "1.4.237",
            "aside": "1.26.1010.1739"}
LOGINS = {"claude": ["auth", "status"], "codex": ["login", "status"]}


def _run_tool(spec: dict):
    from oah import doctor
    from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim
    from oah.setup.tools import catalog

    tool = next(item for item in catalog() if item.name == spec["tool"])

    def resolve(name, search_path=None):
        value = spec["resolve"]
        if value is None:
            return None
        if "unsupported_shim" in value:
            raise UnsupportedBatchShim(value["unsupported_shim"]["path"], value["unsupported_shim"]["reason"])
        return Resolved(tuple(value["argv"]), value["source"], value["kind"])

    def run(argv, *, timeout, **kwargs):
        key = " ".join(argv[len(spec["resolve"]["argv"]):])
        answer = spec["runs"].get(key)
        if answer is None:
            raise SystemExit(f"doctor: unexpected command line {argv}")
        if "oserror" in answer:
            raise OSError(answer["oserror"])
        return RunResult(answer["returncode"], answer["stdout"], answer["stderr"], answer["timed_out"])

    status = doctor._check_tool(tool, search_path=None, timeout=spec["timeout"], resolver=resolve, runner=run,
                                gates=spec["gates"])
    return dataclasses.asdict(status)


def _answer(stdout: str = "", stderr: str = "", code: int | None = 0, timed_out: bool = False) -> dict:
    return {"returncode": code, "stdout": stdout, "stderr": stderr, "timed_out": timed_out}


def build(base: pathlib.Path) -> Area:
    from oah import doctor
    from oah.setup import guide
    from oah.setup.tools import catalog

    cases: list[Case] = []
    signed = {key: {"returncode": code, "stdout": stdout} for key, code, stdout in (
        ("claude-true", 0, '{"loggedIn": true}\n'), ("claude-false", 0, '{"loggedIn": false}\n'),
        ("claude-not-bool", 0, '{"loggedIn": "yes"}'), ("exit-nonzero", 1, '{"loggedIn": true}'),
        ("no-exit-code", None, ""), ("plain-text", 0, "Logged in using ChatGPT\n"), ("json-list", 0, "[1]"),
        ("json-without-flag", 0, '{"account": "x"}'), ("empty-output", 0, ""))}
    cases.append(Case("signed-in", ok({key: guide.signed_in(row["returncode"], row["stdout"])
                                       for key, row in signed.items()}), {"calls": signed},
                      function="oah.setup.guide.signed_in", note="a table of signed_in calls"))
    lines = {"plain": "git version 2.50.1\n", "ansi": "\x1b[32mgh version 2.91.0\x1b[0m\n",
             "leading-blank-lines": "\n  \n  v24.14.0  \nmore\n", "empty": "", "long": "x" * 130,
             "crlf": "uv 0.11.7\r\nextra\r\n"}
    cases.append(Case("first-line", ok({key: doctor.first_line(text) for key, text in lines.items()}),
                      {"calls": {key: {"text": text} for key, text in lines.items()}}, function="oah.doctor.first_line",
                      note="a table of first_line calls: the version line doctor keeps"))

    status_rows: dict = {}
    status_results: dict = {}

    def tool_case(name: str, tool: str, *, resolve="ok", runs: dict | None = None, gates: bool = False,
                  note: str = "") -> None:
        argv = [f"fake/bin/{tool}"]
        spec = {"tool": tool, "gates": gates, "timeout": 10.0,
                "resolve": {"argv": argv, "source": argv[0], "kind": "native"} if resolve == "ok" else resolve,
                "runs": runs if runs is not None else {"--version": _answer(VERSIONS[tool] + "\n")}}
        if tool in LOGINS and runs is None:
            spec["runs"][" ".join(LOGINS[tool])] = _answer('{"loggedIn": true}\n' if tool == "claude"
                                                           else "Logged in using ChatGPT\n")
        status_rows[name] = spec
        status_results[name] = _run_tool(spec)

    for tool in ("git", "claude", "codex", "aside"):
        tool_case(f"ok-{tool}", tool)
    tool_case("missing", "codex", resolve=None)
    tool_case("unsupported-shim", "codex",
              resolve={"unsupported_shim": {"path": "fake/bin/codex.cmd", "reason": "no package"}})
    tool_case("version-launch-error", "git", runs={"--version": {"oserror": "exec format error"}})
    tool_case("version-timeout", "claude", runs={"--version": _answer(code=None, timed_out=True)})
    tool_case("version-exit-code", "rg", runs={"--version": _answer("ripgrep 14.1.1\n", code=2)})
    tool_case("version-from-stderr", "gh", runs={"--version": _answer("", "\x1b[32mgh version 2.91.0\x1b[0m\n")})
    tool_case("too-old", "node", runs={"--version": _answer("v14.21.3\n")})
    tool_case("too-old-unparsed", "node", runs={"--version": _answer("node (unknown build)\n")})
    tool_case("node-required-with-gates", "node", gates=True)
    tool_case("too-old-required-with-gates", "node", gates=True, runs={"--version": _answer("v14.21.3\n")})
    tool_case("claude-not-signed-in", "claude", runs={"--version": _answer("2.1.318 (Claude Code)\n"),
                                                             "auth status": _answer('{"loggedIn": false}\n')})
    tool_case("codex-not-signed-in", "codex", runs={"--version": _answer("codex-cli 0.163.0\n"),
                                                           "login status": _answer("Not logged in\n", code=1)})
    tool_case("login-launch-error", "codex", runs={"--version": _answer("codex-cli 0.163.0\n"),
                                                          "login status": {"oserror": "permission denied"}})
    tool_case("login-timeout", "claude", runs={"--version": _answer("2.1.318 (Claude Code)\n"),
                                                      "auth status": _answer(code=None, timed_out=True)})

    cases.append(Case("status-table", ok(status_results), {"calls": status_rows}, function="oah.doctor._check_tool",
                      note="the status decision table: one _check_tool call per row"))
    pythons = {}
    for key, version in (("supported", [3, 11, 4]), ("too-old", [3, 10, 12])):
        status = dataclasses.asdict(doctor.python_status(version))
        if status["source"] != sys.executable:
            raise SystemExit("doctor: python_status no longer reports sys.executable")
        status["source"] = "<python-executable>"
        pythons[key] = status
    cases.append(Case("python-status", ok(pythons), {"calls": {"supported": {"version_info": [3, 11, 4]},
                                                               "too-old": {"version_info": [3, 10, 12]}}},
                      function="oah.doctor.python_status",
                      note="`source` is sys.executable, written as <python-executable>"))

    names = [tool.name for tool in catalog()]
    rows = []
    for name in names:
        status = {"name": name, "requirement": "required" if name in ("git", "python", "claude", "codex") else
                  "recommended", "status": "ok", "version": VERSIONS.get(name, "1.0.0"), "source": f"fake/bin/{name}",
                  "note": "fixture"}
        rows.append(status)
    rows[names.index("jq")].update(status="missing", version=None, source=None)
    rows[names.index("node")].update(status="too-old", version="v14.21.3")
    failing = [dict(row) for row in rows]
    failing[names.index("codex")].update(status="missing", version=None, source=None)
    for key, statuses, platform in (("darwin", rows, "darwin"), ("win32", rows, "win32"),
                                    ("linux", rows, "linux"), ("win32-required-missing", failing, "win32")):
        objects = [doctor.ToolStatus(**row) for row in statuses]
        cases.append(Case(f"json-rows-{key}", ok(json.loads(doctor.render_json(objects, platform))),
                          {"statuses": statuses, "platform": platform}, function="oah.doctor.render_json"))

    return Area(
        "doctor",
        functions=[fn("oah.setup.guide", "signed_in"), fn("oah.doctor", "first_line"),
                   fn("oah.doctor", "_check_tool"), fn("oah.doctor", "python_status"), fn("oah.doctor", "render_json"),
                   fn("oah.doctor", "exit_code")],
        mined_from=["tests/test_doctor.py"],
        cases=cases,
        omitted=[
            {"behaviour": "check_tools over the whole catalog, with the python and unlazy rows",
             "reason": "it reads the running interpreter and installed skill folders; each row's decision is pinned "
                       "through _check_tool and python_status instead"},
            {"behaviour": "the text output (render_text) and the MCP, model, agent, update and scratch notices",
             "reason": "not in the area's listed behaviour; they read per-machine state"},
            {"behaviour": "resolving real executables (resolve_command, npm shims, Windows batch files)",
             "reason": "depends on the host's PATH and file system; the resolver is a fake here"},
        ],
        notes=["Table cases give `calls` (row name -> arguments) and expect a map of row name -> result.",
               "status-table rows give the catalog tool, how the fake resolver answers (`resolve`: argv, "
               "source and kind; null for not found; or `unsupported_shim`), and how the fake runner answers each "
               "command line after the resolved argv (`runs`, keyed by the arguments joined with spaces; an "
               "`oserror` answer raises OSError with that text). `gates` says whether the project uses gates.",
               "json-rows-* cases pass the given ToolStatus rows and platform to render_json; the expectation is the "
               "parsed JSON."],
    )
