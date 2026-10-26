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
from tests.test_fix import FIXED, FixProject, publish_start, wait_for
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
# Noninteractive launchers may ignore SIGHUP; this test must deliver the chosen stop signal.
signal.signal(number, signal.default_int_handler if number == signal.SIGINT else signal.SIG_DFL)
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
    def test_unrecognized_task_file_and_patch_markers_never_enter_complete_results(self):
        for scenario in ("applied", "no-change", "failed", "bind", "manual", "abort"):
            for as_json in (False, True):
                with self.subTest(scenario=scenario, as_json=as_json):
                    marker = "account-unrecognized-owner"
                    project = FixProject(self)
                    project.changes_requested()
                    # Rename the valid task in its real state, keeping the reviewed Git binding intact.
                    for path in project.state.rglob("*"):
                        if path.is_file() and path.suffix in (".json", ".jsonl"):
                            path.write_bytes(path.read_bytes().replace(b'"task": "t1"', b'"task": ' + json.dumps(marker).encode("ascii")))
                    for parent in (project.state / "reviews", project.state / "runs"):
                        (parent / "t1").rename(parent / marker)
                    from oah.review import receipts
                    folder = project.state / "reviews" / marker
                    lines = [json.loads(line) for line in (folder / "index.jsonl").read_bytes().splitlines()]
                    for line in lines:
                        line["digest"] = receipts.digest((folder / line["name"]).read_bytes())
                    (folder / "index.jsonl").write_bytes(
                        b"".join((json.dumps(line) + "\n").encode("utf-8") for line in lines))
                    args = []
                    if scenario == "manual":
                        project.commit(FIXED, trailer="\n\nAgent-Engine: human")
                        args = ["--manual", "--engine", "human"]
                    elif scenario == "abort":
                        from oah.review import receipts
                        receipt = json.loads((project.state / "reviews" / marker / "R1.json").read_bytes())
                        receipts.publish(project.state, marker, "A1-start.json", {
                            "schema": 1, "kind": "attempt-start", "task": marker, "attempt_id": "A1",
                            "source_receipt": "R1.json", "source_receipt_digest": receipts.digest(
                                (project.state / "reviews" / marker / "R1.json").read_bytes()),
                            "finding_ids": ["R1-F1"], "class_ids": ["R1-C1"], "before_head": receipt["head"],
                            "engine": "codex", "manual": False, "started_at": "2026-10-24T00:00:00Z"})
                        args = ["--abort"]
                    else:
                        project.queue_fix({"header_workdir": "leased worktree",
                                           **({"write": {"src/account-unrecognized-file.py": "unmatched-patch-marker\n"}}
                                              if scenario in ("applied", "bind") else
                                              {"exit": 3} if scenario == "failed" else {})})
                        if scenario == "bind":
                            first = oah(project, "fix", "--task", marker, "--json")
                            self.assertEqual(json.loads(first.stdout)["outcome"], "applied")
                            project.commit({"src/account-unrecognized-file.py": "unmatched-patch-marker\n"},
                                           trailer="\n\nAgent-Engine: codex")
                            args = ["--bind"]
                    before_artifacts = {path: path.read_bytes() for path in project.state.rglob("*") if path.is_file()}
                    done = oah(project, "fix", "--task", marker, *args, *(('--json',) if as_json else ()))
                    for value in (marker, "account-unrecognized-file.py", "unmatched-patch-marker"):
                        self.assertNotIn(value, done.stdout + done.stderr)
                    expected = {"applied": "OAH_FIX_APPLIED", "no-change": "OAH_FIX_NO_CHANGE",
                                "failed": "OAH_FIX_FAILED", "bind": "OAH_FIX_BOUND",
                                "manual": "OAH_FIX_RECORDED", "abort": "OAH_FIX_ABORTED"}[scenario]
                    self.assertIn(expected, done.stdout + done.stderr)
                    artifacts = list(folder.glob("A*.json"))
                    for name in ("result.json", "correction.json", "correction.patch"):
                        artifacts.extend((project.state / "runs" / marker).glob("*/" + name))
                    for path in artifacts:
                        if before_artifacts.get(path) == path.read_bytes():
                            continue  # Landed review/legacy records remain immutable.
                        for value in (marker, "account-unrecognized-file.py", "unmatched-patch-marker"):
                            self.assertNotIn(value.encode("ascii"), path.read_bytes())
                    if scenario == "applied":
                        self.assertIn("git diff", done.stdout)

    def test_task_identifiers_refuse_sensitive_or_non_identifier_values_in_all_modes(self):
        marker = "s" + "k-" + token_source.token_hex(20)
        account = "account-" + token_source.token_hex(12) + "@example.invalid"
        project = FixProject(self)
        for task in (marker, account, "Task", "-task", "a" * 65):
            for mode in ((), ("--bind",), ("--manual", "--engine", "human"), ("--abort",)):
                for as_json in (False, True):
                    with self.subTest(task=task, mode=mode, as_json=as_json):
                        done = oah(project, "fix", "--task=" + task, *mode, *(('--json',) if as_json else ()))
                        self.assertEqual(done.returncode, 1)
                        self.assertIn("OAH_TASK_INVALID", done.stderr)
                        self.assertNotIn(marker, done.stdout + done.stderr)
                        self.assertNotIn(account, done.stdout + done.stderr)
        self.assertEqual(project.names(), [])
        self.assertEqual(project.fix_calls(), [])

    def test_results_and_new_records_use_run_ids_and_omit_directory_metadata(self):
        for scenario in ("applied", "no-change", "failed", "bind", "manual", "abort", "refused"):
            for as_json in (False, True):
                with self.subTest(scenario=scenario, as_json=as_json):
                    account = "account-" + token_source.token_hex(12)
                    project = FixProject(self, repo_name=account)
                    project.changes_requested()
                    args = []
                    if scenario == "bind":
                        project.queue_fix({"write": FIXED})
                        project.run_commands(project.fix().commands)
                        args = ["--bind"]
                    elif scenario == "manual":
                        project.commit(FIXED, trailer="\n\nAgent-Engine: human")
                        args = ["--manual", "--engine", "human"]
                    elif scenario == "abort":
                        publish_start(project)
                        args = ["--abort"]
                    elif scenario == "refused":
                        (project.repo / "src/app.py").write_bytes(b"dirty\r\n")
                    else:
                        # Worker streams remain live and unretained (owner I). Keep their header fixed here
                        # so the complete CLI capture checks the dispatcher's text/JSON output boundary.
                        project.queue_fix({"header_workdir": "leased worktree",
                                           **({"write": FIXED} if scenario == "applied" else
                                              {"exit": 3} if scenario == "failed" else {})})
                    before = {path: path.read_bytes() for path in project.state.rglob("*") if path.is_file()}
                    done = oah(project, "fix", "--task", "t1", *args, *(('--json',) if as_json else ()))
                    expected = {"applied": "OAH_FIX_APPLIED", "no-change": "OAH_FIX_NO_CHANGE",
                                "failed": "OAH_FIX_FAILED", "bind": "OAH_FIX_BOUND", "manual": "OAH_FIX_RECORDED",
                                "abort": "OAH_FIX_ABORTED", "refused": "OAH_FIX_DIRTY_TREE"}[scenario]
                    self.assertIn(expected, done.stdout + done.stderr)
                    artifacts = [done.stdout.encode("utf-8"), done.stderr.encode("utf-8")]
                    for path in project.state.rglob("*"):
                        if path.is_file() and before.get(path) != path.read_bytes():
                            data = path.read_bytes()
                            previous = before.get(path, b"")
                            # Append-only logs retain their preexisting entries, including the landed lease.
                            artifacts.append(data[len(previous):] if data.startswith(previous) else data)
                    for value in (account, str(project.repo), str(project.state), str(project.work)):
                        self.assertFalse(any(value.encode("utf-8") in data for data in artifacts),
                                         "directory metadata reached a result or newly retained record")
                    if as_json and scenario in ("applied", "no-change", "failed"):
                        result = json.loads(done.stdout)
                        self.assertIsNone(result["run_dir"])
                        self.assertRegex(result["run_id"], r"^r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")

    def test_argument_errors_use_fixed_diagnostics_in_text_and_json(self):
        project = FixProject(self)
        marker = "account-" + token_source.token_hex(12)
        key = "s" + "k-" + token_source.token_hex(20)
        cases = (("--timeout-seconds", marker), ("--timeout-seconds", key),
                 ("--timeout-seconds", "nan"), ("--timeout-seconds", "inf"),
                 ("--timeout-seconds", "-1"), ("--engine", marker), ("--tier", key),
                 ("--" + key,), (marker,), ("--timeout-seconds",), ("--bind", "--manual"),
                 ("--manual",), ("--engine", "claude"))
        for as_json in (False, True):
            for args in cases:
                for prefix in ((), ("--" + marker,)):
                    with self.subTest(as_json=as_json, args=args, prefix=prefix):
                        done = oah(project, *prefix, "fix", "--task", "t1", *args,
                                   *(("--json",) if as_json else ()))
                        self.assertEqual(done.returncode, 2)
                        self.assertNotIn(marker, done.stdout + done.stderr)
                        self.assertNotIn(key, done.stdout + done.stderr)
                        expected = ("OAH_USAGE: the correction arguments are invalid; run oah fix --help "
                                    "and retry with the required options.")
                        self.assertEqual(done.stderr.strip(), expected)
                        if as_json:
                            saved = json.loads(done.stdout)
                            self.assertEqual((saved["code"], saved["exit_code"], saved["message"]),
                                             ("OAH_USAGE", 2, expected))
                        else:
                            self.assertEqual(done.stdout, "")
        self.assertEqual(project.fix_calls(), [])
        self.assertEqual(project.names(), [])

    def test_all_correction_scenario_outputs_omit_runtime_scope_marker(self):
        scenarios = ("applied", "no-change", "failed", "bind", "manual", "abort", "refused",
                     "sensitive-lease", "sensitive-packet")
        for as_json in (False, True):
            for scenario in scenarios:
                with self.subTest(as_json=as_json, scenario=scenario):
                    project = FixProject(self)
                    project.changes_requested()
                    marker = ("s" + "k-" if scenario.startswith("sensitive-") else "scope-") + token_source.token_hex(20)
                    self.assertFalse(any(marker in value for value in project.env.values()))
                    args = []
                    if scenario == "bind":
                        project.queue_fix({"write": FIXED})
                        project.run_commands(project.fix().commands)
                        args = ["--bind"]
                    elif scenario == "manual":
                        project.commit(FIXED, trailer="\n\nAgent-Engine: human")
                        args = ["--manual", "--engine", "human"]
                    elif scenario == "abort":
                        record = {"schema": 1, "kind": "attempt-start", "task": "t1", "attempt_id": "A1",
                                  "source_receipt": "R1.json", "source_receipt_digest": project.receipt_digest(1),
                                  "finding_ids": ["R1-F1"], "class_ids": ["R1-C1"], "before_head": project.head,
                                  "engine": "codex", "manual": False, "started_at": "2026-10-25T00:00:00Z"}
                        receipts.publish(project.state, "t1", "A1-start.json", record)
                        args = ["--abort"]
                    elif scenario == "refused":
                        (project.repo / "src/app.py").write_bytes(b"dirty\n")
                    else:
                        project.queue_fix({"write": {"outside/notes.txt": "notes\n"},
                                           "exit": 3 if scenario == "failed" else 0}
                                          if scenario != "no-change" else {})
                    if scenario == "sensitive-packet":
                        project.commit({".oah/tasks/t1/PACKET.md": (PACKET + f"- {marker}\n").encode("utf-8")})
                    else:
                        Store(project.state).renew(task="t1", agent="claude", ttl_hours=1,
                                                   scope=(*project.lease.scope, marker))
                    before = {path: path.read_bytes() for path in project.state.rglob("*") if path.is_file()}
                    done = oah(project, "fix", "--task", "t1", *args, *(("--json",) if as_json else ()))
                    outputs = [done.stdout, done.stderr]
                    if as_json:
                        outputs.append(json.dumps(json.loads(done.stdout), ensure_ascii=False))
                    self.assertFalse(any(marker in output for output in outputs),
                                     "scope metadata reached a text or JSON output")
                    if scenario.startswith("sensitive-"):
                        self.assertEqual(done.returncode, 1)
                        self.assertIn("OAH_FIX_SENSITIVE_SCOPE", done.stderr)
                        self.assertEqual(project.fix_calls(), [])
                        self.assertEqual(project.names(), ["R1.json"])
                        retained = [path.read_bytes() for path in project.state.rglob("*")
                                    if path.is_file() and before.get(path) != path.read_bytes()]
                        self.assertFalse(any(marker.encode("utf-8") in data for data in retained),
                                         "credential-bearing scope reached new correction state")
                    else:
                        expected = {"applied": "OAH_FIX_APPLIED", "no-change": "OAH_FIX_NO_CHANGE",
                                    "failed": "OAH_FIX_FAILED", "bind": "OAH_FIX_BOUND",
                                    "manual": "OAH_FIX_RECORDED", "abort": "OAH_FIX_ABORTED",
                                    "refused": "OAH_FIX_DIRTY_TREE"}[scenario]
                        self.assertIn(expected, done.stdout + done.stderr)

    def test_cli_plan_diagnostics_omit_every_packet_field_in_text_and_json(self):
        fields = ("stage", "engine", "tier", "axes-stage", "kind", "intensity", "reasoning", "risk", "Status")
        for as_json in (False, True):
            project = FixProject(self)
            project.changes_requested()
            for field in fields:
                with self.subTest(as_json=as_json, field=field):
                    value = "s" + "k-" + token_source.token_hex(20)
                    self.assertFalse(any(value in item for item in project.env.values()))
                    plan = {"stage": "correction", "engine": "codex", "tier": "high",
                            "axes-stage": "correction", "kind": "implementation", "intensity": "low",
                            "reasoning": "medium", "risk": "low"}
                    plan[field] = value
                    packet = PACKET + ("## Plan\n- {stage} -> {engine} -> {tier}\n"
                                       "- {axes-stage}: kind={kind} intensity={intensity} "
                                       "reasoning={reasoning} risk={risk}\n").format(**plan)
                    if field == "Status":
                        packet = packet.replace("Status: accepted", f"Status: {value}")
                    source = ".oah/tasks/t1/PACKET.md"
                    project.commit({source: packet.encode("utf-8")})
                    done = oah(project, "fix", "--task", "t1", "--tier", "high",
                               *(("--json",) if as_json else ()))
                    artifacts = [done.stdout.encode("utf-8"), done.stderr.encode("utf-8")]
                    artifacts += [path.read_bytes() for path in project.state.rglob("*") if path.is_file()]
                    self.assertFalse(any(value.encode("utf-8") in data for data in artifacts),
                                     "a packet field reached output or state")
                    if field in ("kind", "intensity", "reasoning", "risk", "Status"):
                        code = "OAH_INTENT_NOT_CURRENT" if field == "Status" else "OAH_RECOMMEND_INVALID"
                        self.assertEqual(done.returncode, 1)
                        expected = ("OAH_INTENT_NOT_CURRENT: the intent is unbound, missing, stale or not committed "
                                    "as bound; accept and bind the committed packet with oah start, then retry.\n"
                                    if field == "Status" else
                                    "OAH_RECOMMEND_INVALID: the packet Plan has invalid difficulty axes; "
                                    "correct its kind, intensity, reasoning and risk, then retry.\n")
                        self.assertEqual(done.stderr, expected)
                        if as_json:
                            result = json.loads(done.stdout)
                            self.assertEqual((result["code"], result["message"]), (code, expected.rstrip()))

    def test_cli_intent_diagnostics_omit_packet_values_in_text_and_json(self):
        for phase in ("preflight", "post-run"):
            for as_json in (False, True):
                for sensitive in (False, True):
                    with self.subTest(phase=phase, as_json=as_json, sensitive=sensitive):
                        project = FixProject(self)
                        project.changes_requested()
                        value = ("s" + "k-" if sensitive else "untrusted-") + token_source.token_hex(20)
                        self.assertFalse(any(value in item for item in project.env.values()))
                        source = ".oah/tasks/t1/PACKET.md"
                        packet = PACKET.replace("Status: accepted", f"Status: {value}")
                        if phase == "preflight":
                            (project.repo / source).write_bytes(packet.encode("utf-8"))
                        else:
                            project.queue_fix({"write": {source: packet}})
                        args = ("--json",) if as_json else ()
                        done = oah(project, "fix", "--task", "t1", "--tier", "high", *args)
                        diagnostic = "OAH_INTENT_NOT_CURRENT" if phase == "preflight" else "OAH_FIX_INTENT_CHANGED"
                        self.assertIn(diagnostic, done.stderr)
                        self.assertIn("stale", done.stderr)
                        self.assertFalse(value in done.stderr, "a packet value reached a CLI diagnostic")
                        result = json.loads(done.stdout) if as_json else None
                        if phase == "preflight":
                            self.assertEqual(done.returncode, 1)
                            self.assertEqual(project.fix_calls(), [])
                            self.assertEqual(names(project), ["R1.json"])
                        else:
                            self.assertEqual(done.returncode, 1 if sensitive else 0)
                            self.assertEqual(project.record("A1-outcome.json")["outcome"],
                                             "failed" if sensitive else "applied")
                            if result is not None:
                                self.assertFalse(any(value in warning for warning in result["warnings"]),
                                                 "a packet value reached a JSON diagnostic")
                            if sensitive:
                                self.assertIn("withheld", done.stdout)
                                self.assertNotIn("git add", done.stdout + done.stderr)
                                self.assertNotIn("git commit", done.stdout + done.stderr)
                                if result is not None:
                                    self.assertEqual(result["commands"], [])
                        if sensitive or phase == "preflight":
                            artifacts = [done.stdout.encode("utf-8"), done.stderr.encode("utf-8")]
                            artifacts += [path.read_bytes() for path in project.state.rglob("*") if path.is_file()]
                            self.assertFalse(any(value.encode("utf-8") in data for data in artifacts),
                                             "a packet value reached output or state")

    def test_cli_withholds_unexported_credential_filenames_in_text_and_json(self):
        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                project = FixProject(self)
                project.changes_requested()
                key = "s" + "k-" + token_source.token_hex(20)
                self.assertFalse(any(key in value for value in project.env.values()))
                project.queue_fix({"write": {"src/" + key + ".txt": "harmless\n"}})
                args = ("--json",) if as_json else ()
                done = oah(project, "fix", "--task", "t1", "--tier", "high", *args)
                artifacts = [done.stdout.encode("utf-8"), done.stderr.encode("utf-8")]
                artifacts += [path.read_bytes() for path in project.state.rglob("*") if path.is_file()]
                self.assertFalse(any(key.encode("utf-8") in data for data in artifacts),
                                 "a credential-shaped filename reached CLI output or state")
                self.assertEqual(done.returncode, 1)
                self.assertIn("withheld", done.stdout)
                self.assertIn("[file ", done.stdout)
                self.assertNotIn("git add", done.stdout + done.stderr)
                self.assertNotIn("git commit", done.stdout + done.stderr)
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                if as_json:
                    self.assertEqual(json.loads(done.stdout)["commands"], [])

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
        self.assertEqual(result["next_command"], "oah fix --bind --task <task>")
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
        self.assertIn(b"OAH_FIX_INTERRUPTED: the correction was interrupted", err)
        self.assertFalse(call._alive(pid), "stopping oah fix stops the corrector")
        self.assertEqual(names(project), ["R1.json", "A1-start.json", "A1-outcome.json"])
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery to a child process")
    def test_cli_sigint_records_aborted(self):
        self.interrupted(signal.SIGINT, code=SIGINT_MAIN)

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery to a child process")
    def test_cli_sigterm_records_aborted(self):
        self.interrupted(signal.SIGTERM)

    @unittest.skipIf(os.name == "nt", "POSIX detached session and SIGTERM")
    def test_cli_sigterm_stops_a_descendant_in_another_session(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"spawn_descendant": True, "detached_descendant": True, "hang": True})
        pid_file = project.fix_dir / "descendant.txt"
        process = start_oah(project, "fix", "--task", "t1", "--tier", "high", "--json")
        try:
            self.assertTrue(wait_for(pid_file))
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 130, err)
            self.assertEqual(json.loads(out)["code"], "OAH_FIX_INTERRUPTED")
            self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")
            heartbeat = pid_file.with_suffix(".heartbeat")
            stopped = heartbeat.read_bytes()
            time.sleep(0.2)
            self.assertEqual(heartbeat.read_bytes(), stopped, "the detached descendant survived SIGTERM cleanup")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=30)
            if pid_file.exists():
                reap(int(pid_file.read_bytes()))

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
        self.assertEqual(project.record("A1-outcome.json")["evidence"], receipts.digest(
            b"recorded by oah fix --abort; interrupted attempt; its run was last running"))
        run_id = project.record("A1-start.json")["run_id"]
        run = json.loads((project.state / "runs" / "t1" / run_id / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "lost")
        self.assertEqual(run["error"], "OAH_RUN_LOST")
        done = oah(project, "fix", "--abort", "--task", "t1")
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("OAH_FIX_NOTHING_TO_ABORT:"), done.stderr)
        done = oah(project, "fix", "--task", "t1", "--tier", "high")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.startswith("OAH_FIX_APPLIED:"), done.stdout)
        self.assertIn("git diff", done.stdout)
        self.assertNotIn("t1", done.stdout)
        self.assertIn('-m "Agent-Engine: codex"', done.stdout)
        self.assertEqual(git(project.repo, "status", "--porcelain"), "M src/app.py")


if __name__ == "__main__":
    unittest.main()
