# 가짜 Codex 수정자 — workspace-write 호출은 스크립트 단계대로 파일을 고치고 본 것을 기록하며, 읽기 전용 호출은 가짜 리뷰어에게 넘긴다.
"""A stand-in for `codex` in review and correction runs.

    fake_fix_codex.py --fix-script FILE --script REVIEW_FILE <codex arguments>

`--version` prints the CLI version. A read-only `exec` (a review) goes to fake_review_codex.py with REVIEW_FILE.
A `workspace-write` `exec` (a correction) follows the fix script, JSON {"record_dir": DIR, "state_dir": DIR,
"steps": [step, ...]}. Correction call k (counted by start markers in record_dir) uses steps[k], or {} past
the end. A step may set:
  write          {path: text, or null to delete}, relative to the -C directory
  chmod          {path: integer mode}, for ignored-file permission accounting
  stage          paths to stage before applying restore (an intentional sandbox violation for accounting tests)
  index_commands argument lists for git update-index, also intentional violations
  restore        {path: text, or null to delete}, after staging, to exercise hidden index-only changes
  commit         true: commit everything with git itself, which Codex's read-only .git forbids
  hang           true: write pid-<k>.txt in record_dir before reading the brief, then sleep until killed
  exit_with_descendant true: exit while a descendant keeps stdout/stderr open
  spawn_descendant true: leave a descendant with closed output pipes after a normal return
  detached_descendant true: that descendant starts a new POSIX session (a Windows process group)
  probe_lease    true: run `oah renew`, `oah finish` and `oah fix --bind` as this worker and record the results
  header_effort  the header's reasoning effort; "-" omits the line; default echoes -c model_reasoning_effort
  header_workdir the header's workdir label; default echoes -C (fixed labels isolate dispatcher output tests)
  last_message   the final message; default: a line, then the INTENT and AC lines read from the brief
  sleep          seconds to wait after the header, before any change or output
  stdin_delay    seconds to wait before reading the brief
  stdout, stderr extra output; {env:NAME} resolves in this process only
  exit           the exit code (default 0)
Every correction call writes call-<k>.json: argv, cwd, env, prompt, the record names indexed in
state_dir/reviews/<OAH_TASK>/index.jsonl when it started, and the probe results.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time

import fake_review_codex

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
IDENTITY = ("-c", "user.name=Someone", "-c", "user.email=" + "1234+someone" + "@" + "users.noreply.github.com",
            "-c", "commit.gpgsign=false")


def options_of(args: list[str]) -> tuple[dict[str, str], list[str]]:
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
    return options, configs


def claim(record_dir: pathlib.Path) -> int:
    """The number of this correction call: the first start marker this process creates."""
    number = 0
    while True:
        try:
            os.close(os.open(record_dir / f"start-{number:03d}", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return number
        except FileExistsError:
            number += 1


def indexed(state_dir: str | None, task: str | None) -> list[str] | None:
    if not state_dir or not task:
        return None
    path = pathlib.Path(state_dir) / "reviews" / task / "index.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line)["name"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def probe(cwd: pathlib.Path, *args: str) -> dict:
    env = dict(os.environ, PYTHONPATH=str(SRC))
    done = subprocess.run([sys.executable, "-m", "oah", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    return {"argv": list(args), "returncode": done.returncode, "stderr": done.stderr}


def default_message(prompt: str) -> str:
    line = re.search(r"INTENT \S+@r\d+ sha256:[0-9a-f]{64}", prompt)
    acs = list(dict.fromkeys(re.findall(r"^- (AC\d+):", prompt, re.M)))
    lines = ["Corrected the findings of the brief."] + ([line.group(0)] if line else [])
    return "\n".join(lines + [f"{ac}: met — corrected by the fake corrector" for ac in acs]) + "\n"


def main(argv: list[str]) -> int:
    if argv[:1] == ["--descendant"]:
        pid_file = pathlib.Path(argv[1])
        heartbeat = pid_file.with_suffix(".heartbeat")
        heartbeat.write_bytes(b"0")
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        count = 0
        while True:
            time.sleep(0.05)
            count += 1
            heartbeat.write_bytes(str(count).encode("ascii"))
    if argv[:1] != ["--fix-script"] or argv[2:3] != ["--script"]:
        print("fake corrector: start with --fix-script FILE --script REVIEW_FILE", file=sys.stderr)
        return 2
    fix_script, review_script, args = argv[1], argv[3], argv[4:]
    if args[:1] == ["--version"]:
        print("codex-cli 0.163.0")
        return 0
    if args[:1] != ["exec"]:
        print(f"fake corrector: unsupported arguments {args}", file=sys.stderr)
        return 2
    options, configs = options_of(args)
    if options.get("-s") != "workspace-write":
        return fake_review_codex.main(["--script", review_script, *args])
    script = json.loads(pathlib.Path(fix_script).read_text(encoding="utf-8"))
    record_dir = pathlib.Path(script["record_dir"])
    number = claim(record_dir)
    steps = script.get("steps", [])
    step = fake_review_codex.substitute_env(steps[number] if number < len(steps) else {})
    task = os.environ.get("OAH_TASK")
    seen = indexed(script.get("state_dir"), task)
    if step.get("hang"):
        (record_dir / f"pid-{number:03d}.txt").write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(float(step.get("stdin_delay", 0)))
    prompt = sys.stdin.buffer.read().decode("utf-8")
    cwd = pathlib.Path(options.get("-C") or os.getcwd())
    probes = []
    if step.get("probe_lease"):
        probes = [probe(cwd, "renew", "--task", task or "", "--agent", "claude"),
                  probe(cwd, "finish", "--task", task or "", "--agent", "claude", "--summary", "worker"),
                  probe(cwd, "fix", "--bind", "--task", task or "")]
    record = {"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ), "prompt": prompt, "indexed": seen,
              "probes": probes}
    (record_dir / f"call-{number:03d}.json").write_text(json.dumps(record), encoding="utf-8")
    effort = next((m.group(1) for m in (re.fullmatch(r'model_reasoning_effort="([a-z]+)"', c) for c in configs) if m),
                  "")
    header = step.get("header_effort", effort)
    lines = ["OpenAI Codex v0.163.0 (fake)", "--------", f"workdir: {step.get('header_workdir', options.get('-C', ''))}",
             f"model: {options.get('-m', '')}", f"sandbox: {options.get('-s', '')}"]
    if header != "-":
        lines.append(f"reasoning effort: {header}")
    sys.stderr.write("\n".join(lines + ["--------"]) + "\n")
    sys.stderr.flush()
    if step.get("exit_with_descendant"):
        subprocess.Popen([sys.executable, __file__, "--descendant", str(record_dir / "descendant.txt")],
                         stdin=subprocess.DEVNULL)
        # Keep the parent-PID edge observable before testing cleanup after the launcher exits.
        time.sleep(0.3)
        os._exit(0)
    if step.get("spawn_descendant"):
        pid_file = record_dir / "descendant.txt"
        detached = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else
                    {"start_new_session": True}) if step.get("detached_descendant") else {}
        subprocess.Popen([sys.executable, __file__, "--descendant", str(pid_file)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached)
        deadline = time.monotonic() + 10
        while not pid_file.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("fake descendant did not start")
            time.sleep(0.01)
        if step.get("detached_descendant"):
            time.sleep(0.3)  # Keep the parent alive while the detached descendant is reachable by PPID.
    if step.get("hang"):
        while True:
            time.sleep(1)
    time.sleep(float(step.get("sleep", 0)))
    for name, content in step.get("write", {}).items():
        target = cwd / name
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
    for name, mode in step.get("chmod", {}).items():
        (cwd / name).chmod(mode)
    if step.get("commit"):
        subprocess.run(["git", "-C", str(cwd), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(cwd), *IDENTITY, "commit", "-q", "-m", "fake corrector commit"], check=True,
                       capture_output=True)
    if step.get("stage"):
        subprocess.run(["git", "-C", str(cwd), "add", "--", *step["stage"]], check=True, capture_output=True)
    for args in step.get("index_commands", []):
        subprocess.run(["git", "-C", str(cwd), "update-index", *args], check=True, capture_output=True)
    for name, content in step.get("restore", {}).items():
        target = cwd / name
        if content is None:
            target.unlink()
        else:
            target.write_bytes(content.encode("utf-8"))
    if "-o" in options:
        pathlib.Path(options["-o"]).write_text(step.get("last_message", default_message(prompt)), encoding="utf-8")
    else:
        sys.stdout.buffer.write(step.get("last_message", default_message(prompt)).encode("utf-8"))
    sys.stdout.buffer.write(step.get("stdout", "").encode("utf-8"))
    sys.stderr.buffer.write(step.get("stderr", "").encode("utf-8"))
    sys.stdout.buffer.write(b"fake correction done\n")
    return int(step.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
