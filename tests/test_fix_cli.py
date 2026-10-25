# oah fix 명령 검사 — 실제 명령 탐색과 python -m oah 로 사용법, 의도 상태, 작업자 거부, JSON 왕복, Ctrl-C·SIGTERM·강제 종료 뒤 기록과 --abort 회복을 본다.
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import secrets as token_source
import signal
import subprocess
import sys
import threading
import time
import unittest

from oah import call
from oah.cli import build_parser, main
from oah.lease import Store
from oah.paths import repo_info
from oah.review import receipts
from tests.test_fix import FIXED, FixProject, wait_for
from tests.test_review_rounds import PACKET, git

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
# Python keeps a SIG_IGN that a background test runner passes through exec, so the helper resets it.
SIGINT_MAIN = ("import signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); "
               "from oah.cli import main; raise SystemExit(main(sys.argv[1:]))")
TRANSITION_MAIN = '''
import contextlib, os, pathlib, signal, sys
from oah.cli import main
from oah.review import fix
signal.signal(signal.SIGINT, signal.default_int_handler)
point = os.environ["OAH_FIX_TEST_POINT"]
number = getattr(signal, os.environ["OAH_FIX_TEST_SIGNAL"])
held_signals = fix._held_signals
judge = fix._Attempt._judge
fired = False
def send_stop():
    if os.name == "nt":
        signal.raise_signal(number)
    else:
        os.kill(os.getpid(), number)
@contextlib.contextmanager
def held():
    global fired
    with held_signals() as pending:
        yield pending
    if point == "after-start" and not fired:
        fired = True
        send_stop()
def inspect(self, *args, **kwargs):
    if point == "before-inspection":
        send_stop()
    return judge(self, *args, **kwargs)
fix._held_signals = held
fix._Attempt._judge = inspect
raise SystemExit(main(sys.argv[1:]))
'''


def oah(project: FixProject, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(project.env, PYTHONPATH=SRC)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "oah", *args], cwd=project.repo, capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=600)


def start_oah(project: FixProject, *args: str, code: str | None = None,
              env_extra: dict | None = None) -> subprocess.Popen:
    env = dict(project.env, PYTHONPATH=SRC, **(env_extra or {}))
    argv = [sys.executable, "-c", code, *args] if code else [sys.executable, "-m", "oah", *args]
    return subprocess.Popen(argv, cwd=project.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def names(project: FixProject) -> list[str]:
    return [entry.name for entry in receipts.load(project.state, "t1").entries]


def reap(pid: int) -> None:
    """Stop a corrector the test itself orphaned (POSIX keeps it alive in its own session)."""
    if os.name != "nt" and call._alive(pid):
        os.kill(pid, signal.SIGKILL)


class CliTest(unittest.TestCase):
    def test_cli_streams_before_exit_and_retains_only_digests_on_timeout(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"hang": True})
        prior = {path: path.read_bytes() for path in project.state.rglob("*") if path.is_file()}
        process = start_oah(project, "fix", "--task", "t1", "--tier", "high", "--json",
                            "--timeout-seconds", "3")
        seen = []
        reader = threading.Thread(target=lambda: seen.append(process.stderr.readline()), daemon=True)
        try:
            reader.start()
            reader.join(timeout=10)
            self.assertFalse(reader.is_alive(), "the first worker line must stream while it is still running")
            self.assertIn(b"OpenAI Codex", seen[0])
            self.assertIsNone(process.poll(), "the worker output must be visible before timeout")
            run_id = project.record("A1-start.json")["run_id"]
            folder = project.state / "runs" / "t1" / run_id
            for name in ("stdout.log", "stderr.log", "last-message.md"):
                self.assertFalse((folder / name).exists())
            out, err = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=30)
            reader.join(timeout=5)
        result = json.loads(out)
        self.assertEqual((result["outcome"], result["commands"]), ("failed", []))
        run = json.loads((folder / "result.json").read_bytes())
        self.assertEqual(run["status"], "timed_out")
        self.assertGreater(run["worker_output"]["stderr"]["bytes"], 0)
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
        for path in project.state.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                if data != prior.get(path):
                    self.assertNotIn(b"OpenAI Codex v0.163.0 (fake)", data)

    def test_cli_withholds_raw_multiline_credentials_in_text_and_json(self):
        for as_json in (False, True):
            for name in ("src/app.py", "src/new.txt"):
                with self.subTest(as_json=as_json, name=name):
                    project = FixProject(self)
                    project.changes_requested()
                    key = "runtime-" + token_source.token_hex(16) + "\ncontinuation-" + token_source.token_hex(16)
                    project.queue_fix({"write": {name: key + "\n"}})
                    args = ("--json",) if as_json else ()
                    done = oah(project, "fix", "--task", "t1", "--tier", "high", *args,
                               env_extra={"OPENAI_API_KEY": key})
                    artifacts = [done.stdout.encode("utf-8"), done.stderr.encode("utf-8")]
                    artifacts += [path.read_bytes() for path in project.state.rglob("*") if path.is_file()]
                    for data in artifacts:
                        for line in key.splitlines():
                            self.assertFalse(line.encode("utf-8") in data, "a credential line reached CLI output or state")
                    self.assertEqual(done.returncode, 1)
                    self.assertIn("withheld", done.stdout)
                    self.assertNotIn("git add", done.stdout + done.stderr)
                    self.assertNotIn("git commit", done.stdout + done.stderr)
                    self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                    if as_json:
                        self.assertEqual(json.loads(done.stdout)["commands"], [])

    @unittest.skipUnless(os.name == "nt", "native Windows console signal handlers")
    def test_cli_windows_sigbreak_at_attempt_context_transitions(self):
        for point in ("after-start", "before-inspection"):
            with self.subTest(point=point):
                project = FixProject(self)
                project.changes_requested()
                project.queue_fix({"write": FIXED})
                process = start_oah(project, "fix", "--task", "t1", "--tier", "high", code=TRANSITION_MAIN,
                                    env_extra={"OAH_FIX_TEST_POINT": point, "OAH_FIX_TEST_SIGNAL": "SIGBREAK"})
                out, err = process.communicate(timeout=60)
                self.assertEqual(names(project), ["R1.json", "A1-start.json", "A1-outcome.json"],
                                 (process.returncode, out, err))
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")
                self.assertEqual(process.returncode, 130, err)

    @unittest.skipIf(os.name == "nt", "POSIX stop signals to a subprocess")
    def test_cli_stop_signals_at_attempt_context_transitions(self):
        for point in ("after-start", "before-inspection"):
            for stop in ("SIGINT", "SIGTERM", "SIGHUP"):
                with self.subTest(point=point, stop=stop):
                    project = FixProject(self)
                    project.changes_requested()
                    project.queue_fix({"write": FIXED})
                    process = start_oah(project, "fix", "--task", "t1", "--tier", "high", code=TRANSITION_MAIN,
                                        env_extra={"OAH_FIX_TEST_POINT": point, "OAH_FIX_TEST_SIGNAL": stop})
                    out, err = process.communicate(timeout=60)
                    self.assertEqual(names(project), ["R1.json", "A1-start.json", "A1-outcome.json"],
                                     (process.returncode, out, err))
                    self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")
                    self.assertEqual(process.returncode, 130, err)
                    if point == "after-start":
                        self.assertEqual(project.fix_calls(), [])

    def test_cli_withholds_sensitive_filename_commands_in_text_and_json(self):
        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                project = FixProject(self)
                project.changes_requested()
                key = "runtime-" + token_source.token_hex(16)
                project.queue_fix({"write": {"outside/" + key: "correction\n"}})
                args = ("--json",) if as_json else ()
                done = oah(project, "fix", "--task", "t1", "--tier", "high", *args,
                           env_extra={"OPENAI_API_KEY": key})
                self.assertFalse(key in done.stdout + done.stderr, "a runtime credential reached CLI output")
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertNotIn("git add", done.stdout + done.stderr)
                self.assertNotIn("git commit", done.stdout + done.stderr)
                if as_json:
                    self.assertEqual(json.loads(done.stdout)["commands"], [])

    def test_fix_command_is_discovered(self):
        parser = build_parser()
        commands = next(action for action in parser._actions if action.dest == "command")
        self.assertIn("fix", commands.choices)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([]), 0)
        self.assertIn("fix: fix\n", out.getvalue())

    def test_cli_usage_errors(self):
        project = FixProject(self)
        for args in (("--bind", "--manual"), ("--manual",), ("--manual", "--engine", "codex"), ("--engine", "claude"),
                     ("--bind", "--finding", "R1-F1"), ("--abort", "--tier", "high"), ("--bind", "--timeout-seconds", "5"),
                     ("--timeout-seconds", "0")):
            with self.subTest(args=args):
                done = oah(project, "fix", "--task", "t1", *args)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        done = oah(project, "fix")
        self.assertEqual(done.returncode, 2, "--task is required")
        self.assertEqual(names(project), [])

    def test_cli_intent_not_current_states(self):
        def unbound(project):
            store = Store(project.state)
            store.release(task="t1", agent="claude", reason="test", force=False)
            store.start(task="t1", agent="claude", scope=("src",), note="unbound", ttl_hours=1,
                        info=repo_info(project.repo))

        packet = ".oah/tasks/t1/PACKET.md"
        cases = (("unbound", unbound),
                 ("missing", lambda project: (project.repo / packet).unlink()),
                 ("stale", lambda project: (project.repo / packet).write_text(
                     PACKET.replace("Status: accepted", "Status: draft"), encoding="utf-8")))
        for label, damage in cases:
            with self.subTest(state=label):
                project = FixProject(self)
                project.changes_requested()
                damage(project)
                done = oah(project, "fix", "--task", "t1", "--json")
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertTrue(done.stderr.startswith("OAH_INTENT_NOT_CURRENT:"), done.stderr)
                self.assertIn(label, done.stderr)
                self.assertEqual(json.loads(done.stdout)["code"], "OAH_INTENT_NOT_CURRENT")
                self.assertEqual(project.fix_calls(), [])
                self.assertEqual(names(project), ["R1.json"])

    def test_cli_worker_refused(self):
        project = FixProject(self)
        project.changes_requested()
        for args in ((), ("--bind",), ("--manual", "--engine", "human"), ("--abort",)):
            with self.subTest(args=args):
                done = oah(project, "fix", "--task", "t1", *args, env_extra={"OAH_WORKER": "1"})
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertTrue(done.stderr.startswith("OAH_FIX_WORKER_REFUSED:"), done.stderr)
        self.assertEqual(names(project), ["R1.json"])

    def test_cli_round_trip_json(self):
        project = FixProject(self)
        project.queue({"findings": [{"finding_id": "R{n}-F1", "class_id": "R{n}-C1", "severity": "P1"}]}, {})
        done = oah(project, "review", "--task", "t1", "--tier", "high", "--json")
        self.assertEqual((done.returncode, json.loads(done.stdout)["code"]), (1, "OAH_REVIEW_CHANGES_REQUESTED"),
                         done.stderr)
        project.queue_fix({"write": FIXED})
        done = oah(project, "fix", "--task", "t1", "--tier", "high", "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual((result["code"], result["attempt_id"], result["outcome"]), ("OAH_FIX_APPLIED", "A1", "applied"))
        self.assertEqual(result["next_command"], "oah fix --bind --task t1")
        done = oah(project, "fix", "--task", "t1")
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("OAH_FIX_BIND_REQUIRED:"), done.stderr)
        project.run_commands(result["commands"])
        done = oah(project, "fix", "--bind", "--task", "t1", "--json")
        self.assertEqual((done.returncode, json.loads(done.stdout)["code"]), (0, "OAH_FIX_BOUND"), done.stderr)
        done = oah(project, "review", "--task", "t1", "--tier", "high", "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        receipt = json.loads(done.stdout)["receipt"]
        self.assertEqual((receipt["round"], receipt["outcome"]), (2, "converged"))
        self.assertEqual([item["name"] for item in receipt["history"]],
                         ["R1.json", "A1-start.json", "A1-outcome.json", "A1-head.json"])
        done = oah(project, "fix", "--task", "t1")
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("OAH_FIX_NOT_REQUESTED:"), done.stderr)

    def interrupted(self, stop: signal.Signals, *, code: str | None = None) -> None:
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"hang": True})
        process = start_oah(project, "fix", "--task", "t1", "--tier", "high", code=code)
        pid_file = project.fix_dir / "pid-000.txt"
        try:
            self.assertTrue(wait_for(pid_file), "the corrector must start")
            time.sleep(0.5)  # let the corrector read its brief while oah waits for it
            process.send_signal(stop)
            out, err = process.communicate(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=30)
        pid = int(pid_file.read_text(encoding="utf-8"))
        self.addCleanup(reap, pid)
        self.assertEqual(process.returncode, 130, err.decode("utf-8", "replace"))
        self.assertIn(b"OAH_FIX_INTERRUPTED: A1 was interrupted and recorded as aborted", err)
        self.assertFalse(call._alive(pid), "stopping oah fix stops the corrector")
        self.assertEqual(names(project), ["R1.json", "A1-start.json", "A1-outcome.json"])
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery to a child process")
    def test_cli_sigint_records_aborted(self):
        self.interrupted(signal.SIGINT, code=SIGINT_MAIN)

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery to a child process")
    def test_cli_sigterm_records_aborted(self):
        self.interrupted(signal.SIGTERM)

    def test_cli_killed_fix_needs_abort(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"hang": True}, {"write": FIXED})
        temp = project.work / "killed-temp"  # a killed process cannot remove its scratch folder; keep it here
        temp.mkdir()
        process = start_oah(project, "fix", "--task", "t1", "--tier", "high",
                            env_extra={"TMPDIR": str(temp), "TEMP": str(temp), "TMP": str(temp)})
        pid_file = project.fix_dir / "pid-000.txt"
        try:
            self.assertTrue(wait_for(pid_file), "the corrector must start")
        finally:
            process.kill()  # a hard kill: no handler runs, so no outcome is recorded
            process.communicate(timeout=30)
        pid = int(pid_file.read_text(encoding="utf-8"))
        reap(pid)
        deadline = time.monotonic() + 10
        while call._alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)  # Windows closes the corrector's job with its killed parent
        self.assertFalse(call._alive(pid))
        self.assertEqual(len(list(temp.glob("oah-fix-*"))), 1, "the scratch folder stayed in the given temp folder")
        self.assertEqual(names(project), ["R1.json", "A1-start.json"])
        done = oah(project, "review", "--task", "t1", "--tier", "high")
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("OAH_REVIEW_NEEDS_DECISION:"), done.stderr)
        self.assertIn("oah fix --abort --task t1", done.stderr)
        done = oah(project, "fix", "--task", "t1")
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("OAH_FIX_ATTEMPT_UNFINISHED:"), done.stderr)
        done = oah(project, "fix", "--abort", "--task", "t1", "--json")
        self.assertEqual((done.returncode, json.loads(done.stdout)["code"]), (0, "OAH_FIX_ABORTED"), done.stderr)
        self.assertIn("its run was last running", project.record("A1-outcome.json")["evidence"])
        run_id = project.record("A1-start.json")["run_id"]
        run = json.loads((project.state / "runs" / "t1" / run_id / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "lost")
        self.assertTrue(run["error"].startswith("OAH_RUN_LOST:"), run["error"])
        done = oah(project, "fix", "--abort", "--task", "t1")
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("OAH_FIX_NOTHING_TO_ABORT:"), done.stderr)
        done = oah(project, "fix", "--task", "t1", "--tier", "high")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.startswith("OAH_FIX_APPLIED: A2 changed the worktree to correct R1-F1"), done.stdout)
        self.assertIn('-m "Agent-Engine: codex"', done.stdout)
        self.assertEqual(git(project.repo, "status", "--porcelain"), "M src/app.py")


if __name__ == "__main__":
    unittest.main()
