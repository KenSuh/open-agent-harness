# 가짜 Codex 리뷰어 — 브리프에서 라운드·base·head·INTENT·AC·DB 줄을 읽고, 스크립트 단계대로 카나리·헤더·발견을 바꿔 출력 스키마 모양의 JSON 을 -o 파일에 쓰며, 본 것을 기록한다.
"""A stand-in for `codex` in review rounds: `fake_review_codex.py --script FILE <codex arguments>`.

The script is JSON {"record_dir": DIR, "steps": [step, ...]}. Call k (counted from the call files already in
record_dir) uses steps[k]; past the end it approves with no findings. A step may set:
  findings, classes, verdict, summary   strings may hold "{n}", the round number from the brief
  canary          "read" (default), "omit" or "wrong"
  header_effort   the header's reasoning effort; "-" omits the line; default echoes -c model_reasoning_effort
  raw             the final message text instead of JSON
  mutate          top-level fields replaced after the output is built
  intent_return   replaces the INTENT and AC lines
  exit            the exit code (default 0)
  stdout, stderr  extra diagnostics; any string can contain {env:NAME} for a runtime-only echo probe
Every call is recorded as call-<k>.json: argv, cwd, env, prompt, files (relative, without .git), remotes
(`git remote -v`), canary (the value read), probe (the file a write attempt created) and
schema_digest (of the --output-schema file).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

SEVERITIES_BLOCKING = ("P0", "P1")


def substitute(value, n: int):
    if isinstance(value, str):
        return value.replace("{n}", str(n))
    if isinstance(value, list):
        return [substitute(item, n) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, n) for key, item in value.items()}
    return value


def files_under(root: pathlib.Path) -> list[str]:
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in names:
            found.append((pathlib.Path(folder) / name).relative_to(root).as_posix())
        for name in dirs:
            if not os.listdir(pathlib.Path(folder) / name):
                found.append((pathlib.Path(folder) / name).relative_to(root).as_posix() + "/")
    return sorted(found)


def build(prompt: str, step: dict, canary: str) -> str:
    n = int(re.search(r"^- Round: R(\d+)$", prompt, re.M).group(1))
    base = re.search(r"^- Base: ([0-9a-f]+)$", prompt, re.M).group(1)
    head = re.search(r"^- Head: ([0-9a-f]+)$", prompt, re.M).group(1)
    line = re.search(r"INTENT \S+@r\d+ sha256:[0-9a-f]{64}", prompt).group(0)
    acs = list(dict.fromkeys(re.findall(r"^- (AC\d+):", prompt, re.M)))
    db_line = next(text for text in prompt.splitlines() if text.startswith("db-impact:"))
    findings = []
    for item in substitute(step.get("findings", []), n):
        full = {"finding_id": f"R{n}-F{len(findings) + 1}", "class_id": f"R{n}-C{len(findings) + 1}", "severity": "P1",
                "axis": "quality", "title": "a defect", "detail": "the mechanism and its evidence",
                "location": "src/app.py:1", "in_scope": True, "fix_kind": "local", "prior_refs": []}
        full.update(item)
        findings.append(full)
    classes = substitute(step["classes"], n) if "classes" in step else [
        {"class_id": cid, "mechanism": f"mechanism of {cid}", "rule": f"rule of {cid}"}
        for cid in dict.fromkeys(f["class_id"] for f in findings) if cid.startswith(f"R{n}-C")]
    blocking = any(f["severity"] in SEVERITIES_BLOCKING for f in findings)
    evidence = ["ran: cat .oah-review-canary", "ran: git diff"]
    mode = step.get("canary", "read")
    if mode == "read":
        evidence.append(f"REVIEWED-TREE-CANARY: {canary}")
    elif mode == "wrong":
        evidence.append("REVIEWED-TREE-CANARY: " + "0" * 32)
    output = {"verdict": step.get("verdict", "request_changes" if blocking else "approve"), "base": base,
              "head": head, "summary": step.get("summary", "fake review"), "findings": findings, "classes": classes,
              "intent_return": step.get("intent_return",
                                        "\n".join([line] + [f"{ac}: met — checked by the fake reviewer" for ac in acs])),
              "evidence": "\n".join(evidence), "db_impact": db_line}
    output.update(substitute(step.get("mutate", {}), n))
    return json.dumps(output, ensure_ascii=False)


def main(argv: list[str]) -> int:
    if argv[:1] != ["--script"]:
        print("fake reviewer: start with --script FILE", file=sys.stderr)
        return 2
    script = json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8"))
    args = argv[2:]
    if args[:1] == ["--version"]:
        print("codex-cli 0.163.0")
        return 0
    if args[:1] != ["exec"]:
        print(f"fake reviewer: unsupported arguments {args}", file=sys.stderr)
        return 2
    options: dict[str, str] = {}
    configs: list[str] = []
    index = 1
    while index < len(args):
        item = args[index]
        if item in ("-C", "-s", "-m", "-o", "--output-schema"):
            options[item] = args[index + 1]
            index += 2
        elif item == "-c":
            configs.append(args[index + 1])
            index += 2
        else:
            index += 1
    prompt = sys.stdin.buffer.read().decode("utf-8")
    record_dir = pathlib.Path(script["record_dir"])
    number = len(list(record_dir.glob("call-*.json")))
    steps = script.get("steps", [])
    step = steps[number] if number < len(steps) else {}
    step = substitute_env(step)
    effort = next((m.group(1) for m in (re.fullmatch(r'model_reasoning_effort="([a-z]+)"', c) for c in configs) if m), "")
    header = step.get("header_effort", effort)
    lines = ["OpenAI Codex v0.163.0 (fake)", "--------", f"workdir: {options.get('-C', '')}",
             f"model: {options.get('-m', '')}", f"sandbox: {options.get('-s', '')}"]
    if header != "-":
        lines.append(f"reasoning effort: {header}")
    lines.append("--------")
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stdout.buffer.write(step.get("stdout", "").encode("utf-8"))
    sys.stderr.buffer.write(step.get("stderr", "").encode("utf-8"))
    cwd = pathlib.Path.cwd()
    canary_path = cwd / ".oah-review-canary"
    canary = canary_path.read_text(encoding="utf-8").strip() if canary_path.is_file() else ""
    probe = cwd / "probe.txt"
    try:
        probe.write_text("written by the fake reviewer\n", encoding="utf-8")
        probed = str(probe)
    except OSError as exc:
        probed = f"refused: {exc}"
    remotes = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True, encoding="utf-8").stdout
    schema = pathlib.Path(options.get("--output-schema", ""))
    schema_digest = "sha256:" + hashlib.sha256(schema.read_bytes()).hexdigest() if schema.is_file() else None
    record = {"argv": argv, "cwd": str(cwd), "env": dict(os.environ), "prompt": prompt,
              "files": [name for name in files_under(cwd) if name != "probe.txt"], "remotes": remotes,
              "canary": canary, "probe": probed, "schema_digest": schema_digest}
    (record_dir / f"call-{number:03d}.json").write_text(json.dumps(record), encoding="utf-8")
    message = step["raw"] if "raw" in step else build(prompt, step, canary)
    if "-o" in options:
        last = pathlib.Path(options["-o"])
        if "final_link" in step:
            last.symlink_to(step["final_link"])
        else:
            last.write_text(message, encoding="utf-8")
    print("fake review done")
    return int(step.get("exit", 0))


def substitute_env(value):
    """Resolve runtime credentials only inside the fake engine, never in its script fixture."""
    if isinstance(value, str):
        return re.sub(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m[1], ""), value)
    if isinstance(value, list):
        return [substitute_env(item) for item in value]
    if isinstance(value, dict):
        return {key: substitute_env(item) for key, item in value.items()}
    return value


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
