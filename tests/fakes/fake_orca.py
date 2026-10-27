# 가짜 Orca CLI·앱 — 받은 인자를 기록하고 worktree·terminal 명령의 JSON 응답을 흉내 내며, 터미널은 별도 호스트 프로세스가 소유한다.
"""A stand-in for the `orca` CLI and the Orca app behind it, with the reply shapes of Orca 1.4.237.

CLI:   python fake_orca.py --state <folder> <group> <command> [--flag value ...] --json
Host:  python fake_orca.py __host   (the test starts it; like the real app it owns the terminals, so a
       terminal outlives the CLI call and runs with the host's environment, not the caller's)

FAKE_ORCA_STATE     folder for argv.jsonl, worktrees, terminals and host requests (required)
FAKE_ORCA_VERSION   printed by --version (default 1.4.237)
FAKE_ORCA_VERSION_SLEEP  seconds to delay --version (default 0)
FAKE_ORCA_DISABLED  "1": --version fails, as when Orca's command-line tool is not enabled
FAKE_ORCA_FAIL      one command such as "terminal create" that answers ok:false
FAKE_ORCA_OAH       (host) JSON argv run in place of the word `oah` in a terminal command
FAKE_ORCA_HOLD      "1": leave a queued terminal for direct worker-run tests
FAKE_ORCA_RELEASE   (host) wait for this file before starting the terminal command

CLI controls come from <folder>/controls.json, so production need not forward any FAKE_* variables.
Optional command/reply/exit_code controls inject malformed replies for contract tests.

A terminal command must be safe words followed by `; exit`; anything else is refused, so the tests
see any change to the generated command.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

COMMAND = re.compile(r"(?:[A-Za-z0-9._/-]+ )*[A-Za-z0-9._/-]+; exit")
PID_RECORD = re.compile(r"(\d+)\r?\n")


def state() -> pathlib.Path:
    return pathlib.Path(os.environ["FAKE_ORCA_STATE"])


def reply(result: dict | None = None, *, code: str | None = None, message: str = "") -> int:
    if code is not None:
        print(json.dumps({"id": "fake", "ok": False, "error": {"code": code, "message": message or code}}))
        return 1
    print(json.dumps({"id": "fake", "ok": True, "result": result or {}}))
    return 0


def load(name: str) -> dict:
    path = state() / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save(name: str, data: dict) -> None:
    path = state() / name
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)


def publish_pid(path: pathlib.Path, pid: int) -> None:
    """Write `<pid>\\n` to a temporary file beside path, then replace path, so no reader sees a partial file."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(f"{pid}\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)


def read_pid(path: pathlib.Path) -> int | None:
    """The published PID, or None while the file is missing, empty or not yet a complete record."""
    try:
        match = PID_RECORD.fullmatch(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError):
        return None
    return int(match.group(1)) if match else None


def flags(argv: list[str]) -> dict:
    out: dict = {}
    index = 0
    while index < len(argv):
        name = argv[index]
        if name.startswith("--") and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            out[name[2:]] = argv[index + 1]
            index += 2
        else:
            out[name[2:]] = True
            index += 1
    return out


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")


def worktree_show(selector: str) -> int:
    worktrees = load("worktrees.json")
    if selector.startswith("id:"):
        found = worktrees.get(selector[3:])
        return reply({"worktree": found}) if found else reply(code="selector_not_found")
    if not selector.startswith("path:"):
        return reply(code="invalid_argument", message="unsupported selector")
    top = git("-C", selector[5:], "rev-parse", "--show-toplevel")
    common = git("-C", selector[5:], "rev-parse", "--path-format=absolute", "--git-common-dir")
    if top.returncode != 0 or common.returncode != 0:
        return reply(code="selector_not_found", message="no Orca worktree at that path")
    path = str(pathlib.Path(top.stdout.strip()).resolve())
    repo_id = "repo-" + hashlib.sha1(common.stdout.strip().encode("utf-8")).hexdigest()[:8]
    repos = load("repos.json")
    repos.setdefault(repo_id, path)
    save("repos.json", repos)
    record = {"id": f"{repo_id}::{path}", "repoId": repo_id, "path": path, "hostId": os.environ.get(
        "FAKE_ORCA_HOST", "local")}
    worktrees[record["id"]] = record
    save("worktrees.json", worktrees)
    return reply({"worktree": record})


def worktree_create(options: dict) -> int:
    repo = load("repos.json").get(str(options.get("repo", ""))[3:])
    if repo is None or "no-parent" not in options:
        return reply(code="invalid_argument", message="unknown repo or missing --no-parent")
    name = options["name"]
    path = (state() / "worktrees" / name).resolve()
    added = git("-C", repo, "worktree", "add", "-q", "-b", name, str(path))
    if added.returncode != 0:
        return reply(code="worktree_create_failed", message=added.stderr.strip())
    repo_id = options["repo"][3:]
    record = {"id": f"{repo_id}::{path}", "repoId": repo_id, "path": str(path), "hostId": "local"}
    worktrees = load("worktrees.json")
    worktrees[record["id"]] = record
    save("worktrees.json", worktrees)
    return reply({"worktree": {"id": record["id"]}})


def terminals() -> pathlib.Path:
    folder = state() / "terminals"
    folder.mkdir(exist_ok=True)
    return folder


def terminal_create(options: dict) -> int:
    worktree = load("worktrees.json").get(str(options.get("worktree", ""))[3:])
    command = options.get("command")
    if worktree is None:
        return reply(code="selector_not_found")
    if not isinstance(command, str) or not COMMAND.fullmatch(command):
        return reply(code="fake_unsupported_command", message=f"refusing command {command!r}")
    handle = "term_" + hashlib.sha1(os.urandom(16)).hexdigest()[:12]
    request = terminals() / f"{handle}.json"
    request.with_suffix(".tmp").write_text(json.dumps({"command": command, "cwd": worktree["path"],
                                                      "shell": options.get("shell")}), encoding="utf-8")
    os.replace(request.with_suffix(".tmp"), request)
    pidfile = terminals() / f"{handle}.pid"
    if os.environ.get("FAKE_ORCA_HOLD") == "1":
        publish_pid(pidfile, 0)
    deadline = time.monotonic() + 15
    while (pid := read_pid(pidfile)) is None:
        if time.monotonic() > deadline:
            return reply(code="host_unreachable", message="the fake Orca host did not start the terminal")
        time.sleep(0.02)
    return reply({"terminal": {"handle": handle, "worktreeId": worktree["id"], "title": options.get("title"),
                               "executionHostId": "local", "hostPlatform": sys.platform,
                               "processId": pid, "surface": "visible"}})


def _lines(handle: str) -> list[str]:
    log = terminals() / f"{handle}.log"
    return log.read_bytes().decode("utf-8", errors="replace").splitlines() if log.exists() else []


def terminal_wait(options: dict) -> int:
    handle = options.get("terminal", "")
    if not (terminals() / f"{handle}.json").exists():
        return reply(code="terminal_handle_stale")
    deadline = time.monotonic() + int(options.get("timeout-ms", "0")) / 1000
    exitfile = terminals() / f"{handle}.exit"
    while not exitfile.exists():
        if time.monotonic() >= deadline:
            return reply(code="timeout", message="timeout")
        time.sleep(0.02)
    return reply({"wait": {"handle": handle, "condition": "exit", "satisfied": True, "status": "exited",
                           "exitCode": -1, "exitCause": {"kind": "unknown", "reason": "stop_unverified"}}})


def terminal_read(options: dict) -> int:
    handle = options.get("terminal", "")
    if not (terminals() / f"{handle}.json").exists():
        return reply(code="terminal_handle_stale")
    exited = (terminals() / f"{handle}.exit").exists()
    lines = [] if exited else _lines(handle)  # the real Orca returns no lines once the terminal exited
    cursor = int(options.get("cursor", "0"))
    limit = int(options.get("limit", "120"))
    tail = lines[cursor:cursor + limit]
    return reply({"terminal": {"handle": handle, "status": "exited" if exited else "running", "tail": tail,
                               "truncated": False, "limited": cursor + len(tail) < len(lines),
                               "oldestCursor": "0", "nextCursor": str(cursor + len(tail)),
                               "latestCursor": str(len(lines)), "returnedLineCount": len(tail),
                               "source": "stream"}})


def terminal_show(options: dict) -> int:
    handle = options.get("terminal", "")
    if not (terminals() / f"{handle}.json").exists():
        return reply(code="terminal_handle_stale")
    return reply({"terminal": {"handle": handle, "preview": "\n".join(_lines(handle)[-10:])}})


def cli(argv: list[str]) -> int:
    folder = state()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "argv.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv) + "\n")
    if argv == ["--version"]:
        time.sleep(float(os.environ.get("FAKE_ORCA_VERSION_SLEEP", "0")))
        if os.environ.get("FAKE_ORCA_DISABLED") == "1":
            print("orca: the command-line tool is not enabled", file=sys.stderr)
            return 1
        print(os.environ.get("FAKE_ORCA_VERSION", "1.4.237"))
        return 0
    words = " ".join(argv[:2])
    malformed = load("controls.json").get("reply")
    if malformed is not None and words == load("controls.json").get("command"):
        print(json.dumps(malformed))
        return int(load("controls.json").get("exit_code", 0))
    if os.environ.get("FAKE_ORCA_FAIL") == words:
        return reply(code="fake_failure", message=f"{words} failed on purpose")
    if "--json" not in argv:
        print("fake orca: every call from oah must pass --json", file=sys.stderr)
        return 2
    options = flags(argv[2:])
    handlers = {"worktree show": lambda: worktree_show(str(options.get("worktree", ""))),
                "worktree create": lambda: worktree_create(options),
                "terminal create": lambda: terminal_create(options),
                "terminal wait": lambda: terminal_wait(options),
                "terminal read": lambda: terminal_read(options),
                "terminal show": lambda: terminal_show(options)}
    if words not in handlers:
        print(f"Unknown command: {words}", file=sys.stderr)
        return 1
    return handlers[words]()


def run_terminal(handle: str) -> None:
    folder = terminals()
    request = json.loads((folder / f"{handle}.json").read_text(encoding="utf-8"))
    words = request["command"][:-len("; exit")].split(" ")
    if words[0] == "oah" and os.environ.get("FAKE_ORCA_OAH"):
        words = json.loads(os.environ["FAKE_ORCA_OAH"]) + words[1:]
    if release := os.environ.get("FAKE_ORCA_RELEASE"):
        # Publish the waiting process's PID normally, but keep the worker blocked until the test releases it.
        gate = ("import pathlib, subprocess, sys, time\n"
                "while not pathlib.Path(sys.argv[1]).exists():\n"
                "    time.sleep(0.02)\n"
                "raise SystemExit(subprocess.call(sys.argv[2:]))\n")
        words = [sys.executable, "-c", gate, release, *words]
    env = {key: value for key, value in os.environ.items() if not key.startswith("OAH_")}
    with open(folder / f"{handle}.log", "wb") as log:
        try:
            child = subprocess.Popen(words, cwd=request["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT)
        except OSError as exc:
            log.write(f"fake shell: {exc}\n".encode("utf-8"))
            publish_pid(folder / f"{handle}.pid", 0)
            (folder / f"{handle}.exit").write_text("127", encoding="utf-8")
            return
        publish_pid(folder / f"{handle}.pid", child.pid)
        code = child.wait()
    (folder / f"{handle}.exit").write_text(str(code), encoding="utf-8")


def host() -> int:
    folder = terminals()
    started: set[str] = set()
    threads: list[threading.Thread] = []
    deadline = time.monotonic() + 300
    while not (state() / "stop").exists() and time.monotonic() < deadline:
        for request in folder.glob("*.json"):
            if request.stem not in started:
                started.add(request.stem)
                thread = threading.Thread(target=run_terminal, args=(request.stem,), daemon=True)
                thread.start()
                threads.append(thread)
        time.sleep(0.02)
    for thread in threads:
        thread.join(timeout=30)
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[:1] == ["--state"]:
        os.environ["FAKE_ORCA_STATE"] = argv[1]
        argv = argv[2:]
        for key, value in load("controls.json").items():
            if key.startswith("FAKE_ORCA_"):
                os.environ[key] = value
    sys.exit(host() if argv == ["__host"] else cli(argv))
