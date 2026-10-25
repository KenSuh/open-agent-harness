# oah fix 검사 — 가짜 리뷰어·수정자 프로세스와 실제 모듈로 수정 시도 기록, 작업자 실행, 끝마다 하나뿐인 결과, 커밋 묶기, 수동 수정, 리뷰 왕복, 비밀 차단을 본다.
from __future__ import annotations

import _thread
import contextlib
import json
import os
import pathlib
import secrets as token_source
import shlex
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from oah import briefs, call, call_cli, intent
from oah.lease import Store
from oah.lockfile import StateLock
from oah.paths import repo_info
from oah.review import ReviewError, authors, fix, receipts, runner
from oah.secrets import NoKeychain
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir
from tests.test_review_rounds import CLAUDE_TRAILER, LEVELS, PACKET, RULES, bug, git, toml_list

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_FIX = FAKES / "fake_fix_codex.py"
CODEX_TRAILER = "\n\nAgent-Engine: codex"
HUMAN_TRAILER = "\n\nAgent-Engine: human"


class FixProject:
    """A task branch with a bound lease, reviewed and corrected by the fake engine processes."""

    def __init__(self, test: unittest.TestCase, *, config: str = "", files: dict | None = None,
                 packet: str = PACKET, newline: str = "\n", attributes: str | None = "* -text\n",
                 repo_name: str = "repo") -> None:
        self.test = test
        self.repo = make_git_repo(test)
        if repo_name != self.repo.name:
            self.repo = self.repo.rename(self.repo.with_name(repo_name))
        self.work = make_tmp_dir(test)
        self.state, self.home = self.work / "state", self.work / "codex-home"
        self.calls_dir, self.fix_dir, self.temp_root = self.work / "calls", self.work / "fix-calls", self.work / "temp"
        self.script, self.fix_script = self.work / "script.json", self.work / "fix-script.json"
        for folder in (self.home, self.calls_dir, self.fix_dir, self.temp_root):
            folder.mkdir()
        (self.home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-test-a", "is_default": True,
             "supported_reasoning_levels": [{"effort": level} for level in LEVELS]}]}), encoding="utf-8")
        command = [sys.executable, str(FAKE_FIX), "--fix-script", str(self.fix_script), "--script", str(self.script)]
        files = {"README.md": "hello\n", ".oah/config.toml": f"[engines.codex]\ncommand = {toml_list(command)}\n" + config,
                 ".oah/tasks/t1/PACKET.md": packet.replace("\n", newline).encode("utf-8"), **(files or {})}
        if attributes is not None:
            files[".gitattributes"] = attributes
        self.base = git_commit(self.repo, files, "start")
        origin = self.work / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(origin)],
                       check=True, capture_output=True)
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", "-b", "feat")
        self.head = self.commit({"src/app.py": "print('v1')\n"})
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("FAKE_", "OAH_")) and key != "CLAUDE_CONFIG_DIR"}
        self.env.update(CODEX_HOME=str(self.home), OAH_STATE_DIR=str(self.state), HOME=str(self.work / "home"),
                        USERPROFILE=str(self.work / "home"))
        self.steps: list[dict] = []
        self.fix_steps: list[dict] = []
        self.queue()
        self.queue_fix()
        source = self.repo / ".oah/tasks/t1/PACKET.md"
        self.binding = intent.parse(intent.read_source(source)).bind(".oah/tasks/t1/PACKET.md")
        self.lease = Store(self.state).start(task="t1", agent="claude", scope=("src", ".oah/tasks/t1"),
                                             note="fix fixture", ttl_hours=1, info=repo_info(self.repo),
                                             intent=self.binding)

    def commit(self, files: dict, message: str = "work", trailer: str = CLAUDE_TRAILER) -> str:
        self.head = git_commit(self.repo, files, message + trailer)
        return self.head

    def queue(self, *steps: dict) -> None:
        """Steps for the fake reviewer (read-only calls)."""
        self.steps.extend(steps)
        self.script.write_text(json.dumps({"record_dir": str(self.calls_dir), "steps": self.steps}), encoding="utf-8")

    def queue_fix(self, *steps: dict) -> None:
        """Steps for the fake corrector (workspace-write calls)."""
        self.fix_steps.extend(steps)
        self.fix_script.write_text(json.dumps({"record_dir": str(self.fix_dir), "state_dir": str(self.state),
                                               "steps": self.fix_steps}), encoding="utf-8")

    def review(self, **fields) -> runner.Result:
        fields.setdefault("tier", "high")
        return runner.review(runner.Request(task="t1", **fields), cwd=self.repo, env=self.env, keychain=NoKeychain(),
                             temp_root=self.temp_root, rules=RULES)

    def fix(self, *, cwd: pathlib.Path | None = None, env: dict | None = None, rules: list | None = None,
            **fields) -> fix.Result:
        fields.setdefault("tier", "high")
        return fix.correct(fix.Request(task="t1", **fields), cwd=cwd or self.repo, env=self.env if env is None else env,
                           keychain=NoKeychain(), rules=RULES if rules is None else rules, temp_root=self.temp_root)

    def bind(self, **fields) -> fix.Result:
        return fix.bind("t1", cwd=self.repo, env=self.env, **fields)

    def manual(self, engine: str, **fields) -> fix.Result:
        return fix.record_manual("t1", engine=engine, cwd=self.repo, env=self.env, **fields)

    def abort(self) -> fix.Result:
        return fix.abort("t1", cwd=self.repo, env=self.env)

    def refused(self, code: str, action, *args, **kwargs) -> ReviewError:
        """Expect a refusal that calls no engine and publishes no record."""
        calls, fixes, names = len(self.calls()), len(self.fix_calls()), self.names()
        with self.test.assertRaises(ReviewError) as caught:
            action(*args, **kwargs)
        self.test.assertEqual(caught.exception.code, code, str(caught.exception))
        self.test.assertEqual((len(self.calls()), len(self.fix_calls())), (calls, fixes), "a refusal must launch nothing")
        self.test.assertEqual(self.names(), names, "a refusal must publish nothing")
        return caught.exception

    def calls(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.calls_dir.glob("call-*.json"))]

    def fix_calls(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.fix_dir.glob("call-*.json"))]

    def folder(self) -> pathlib.Path:
        return self.state / "reviews" / "t1"

    def names(self) -> list[str]:
        return [entry.name for entry in receipts.load(self.state, "t1").entries]

    def record(self, name: str) -> dict:
        return json.loads((self.folder() / name).read_text(encoding="utf-8"))

    def receipt(self, number: int) -> dict:
        return self.record(f"R{number}.json")

    def receipt_digest(self, number: int) -> str:
        return receipts.digest((self.folder() / f"R{number}.json").read_bytes())

    def changes_requested(self, *findings: dict) -> runner.Result:
        """Review the head once with these findings (default: one P1 bug)."""
        self.queue({"findings": list(findings) or [bug()]})
        result = self.review()
        self.test.assertEqual(result.code, "OAH_REVIEW_CHANGES_REQUESTED", result.message)
        return result

    def run_commands(self, commands: list[str]) -> str:
        """Run the printed commit commands as a user would (POSIX shell words), and return the new HEAD."""
        for command in commands:
            subprocess.run(shlex.split(command), cwd=self.repo, check=True, capture_output=True, env=git_env())
        self.head = git(self.repo, "rev-parse", "HEAD")
        return self.head


def wait_for(path: pathlib.Path, seconds: float = 60) -> bool:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


@contextlib.contextmanager
def default_sigint():
    """A background test runner may inherit an ignored SIGINT; Ctrl-C tests need Python's own handler."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def interrupt_when(path: pathlib.Path) -> threading.Thread:
    """Deliver Ctrl-C to this (main) thread, as Python sees it, once the hanging worker wrote its pid file."""
    def run():
        if wait_for(path):
            _thread.interrupt_main()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class CallSeamTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX calls retain their landed descendant behavior")
    def test_plain_call_retains_its_descendant_behavior(self):
        project = FixProject(self)
        project.queue_fix({"spawn_descendant": True})
        prepared = self.prepare(project)
        pid_file = project.fix_dir / "descendant.txt"
        try:
            run = call.execute(prepared, base_env=runner.reviewer_env(project.env), rules=RULES)
            self.assertEqual(run["status"], "finished")
            heartbeat = pid_file.with_suffix(".heartbeat")
            before = heartbeat.read_bytes()
            time.sleep(0.2)
            self.assertNotEqual(heartbeat.read_bytes(), before)
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def prepare(self, project: FixProject, **fields) -> call.Prepared:
        tools = call_cli.intent_tools()
        return call.prepare(call.CallRequest(engine="codex", stage="correction", task="t1", tier="high", **fields),
                            call_cli.load_settings(project.repo), root=project.repo, state_dir=project.state,
                            env=runner.reviewer_env(project.env), footer=tools.footer, has_footer=tools.has_footer,
                            parse_intent=tools.parse, read_source=tools.read_source, keychain=NoKeychain())

    def test_interrupt_stops_the_tree(self):
        project = FixProject(self)
        project.queue_fix({"hang": True})
        prepared = self.prepare(project, timeout_s=120)
        pid_file = project.fix_dir / "pid-000.txt"
        with default_sigint():
            thread = interrupt_when(pid_file)
            with self.assertRaises(KeyboardInterrupt):
                call.execute(prepared, base_env=runner.reviewer_env(project.env), rules=RULES)
            thread.join(5)
        pid = int(pid_file.read_text(encoding="utf-8"))
        self.assertFalse(call._alive(pid), "an interrupted call must stop its worker")
        run = prepared.run.read()
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["error"].startswith("OAH_RUN_INTERRUPTED:"), run["error"])
        call.check_prepared(prepared)  # the lease lock was released on the way out

    def exited_launcher_interruption(self):
        project = FixProject(self)
        project.queue_fix({"exit_with_descendant": True})
        prepared = self.prepare(project, timeout_s=120)
        pid_file = project.fix_dir / "descendant.txt"
        children, exits = [], []
        popen = subprocess.Popen

        def capture(*args, **kwargs):
            child = popen(*args, **kwargs)
            if str(FAKE_FIX) in args[0] and "exec" in args[0]:
                children.append(child)
            return child

        def interrupt():
            if wait_for(pid_file):
                exits.append(children[0].wait(timeout=10))
                _thread.interrupt_main()

        from oah.engines.proc import kill_tree

        with default_sigint(), mock.patch.object(subprocess, "Popen", side_effect=capture):
            thread = threading.Thread(target=interrupt, daemon=True)
            thread.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    call.execute(prepared, base_env=runner.reviewer_env(project.env), rules=RULES)
                thread.join(5)
                self.assertEqual(exits, [0], "the launcher exited before interruption")
                heartbeat = pid_file.with_suffix(".heartbeat")
                stopped = heartbeat.read_bytes()
                time.sleep(0.3)
                self.assertEqual(heartbeat.read_bytes(), stopped,
                                 "the exited launcher's descendant survived interruption")
                self.assertEqual(prepared.run.read()["status"], "failed")
                call.check_prepared(prepared)
            finally:
                if children:
                    job = getattr(children[0], "_oah_job", None)
                    if job is None or job.handle:
                        kill_tree(children[0])

    @unittest.skipIf(os.name == "nt", "POSIX process group cleanup")
    def test_interrupt_stops_descendant_after_launcher_exits(self):
        self.exited_launcher_interruption()

    @unittest.skipUnless(os.name == "nt", "Windows job cleanup")
    def test_windows_interrupt_stops_descendant_after_launcher_exits(self):
        self.exited_launcher_interruption()

    def test_polled_wait_keeps_output_and_timeout(self):
        project = FixProject(self)
        output = "".join(f"line {number:06d} " + "x" * 60 + "\n" for number in range(4000))
        project.queue_fix({"sleep": 0.6, "stdout": output}, {"hang": True})
        with mock.patch.object(call, "POLL_SECONDS", 0.05):
            prepared = self.prepare(project, round_instructions="y" * 300_000)
            run = call.execute(prepared, base_env=runner.reviewer_env(project.env), rules=RULES)
            self.assertEqual((run["status"], run["exit_code"]), ("finished", 0), run.get("error"))
            self.assertEqual(project.fix_calls()[0]["prompt"].encode("utf-8"), prepared.run.prompt_path.read_bytes())
            self.assertEqual(prepared.run.stdout_path.read_text(encoding="utf-8"), output + "fake correction done\n")
            hanging = self.prepare(project, timeout_s=1)
            run = call.execute(hanging, base_env=runner.reviewer_env(project.env), rules=RULES)
        self.assertEqual((run["status"], run["timed_out"]), ("timed_out", True))
        pid = int((project.fix_dir / "pid-001.txt").read_text(encoding="utf-8"))
        self.assertFalse(call._alive(pid), "a timed-out call must stop its worker")


def publish_start(project: FixProject, number: int = 1, *, finding_ids=("R1-F1",), class_ids=("R1-C1",)) -> None:
    """A start record without its outcome: what a hard-killed oah fix leaves behind."""
    receipts.publish(project.state, "t1", f"A{number}-start.json", {
        "schema": 1, "kind": "attempt-start", "task": "t1", "attempt_id": f"A{number}", "source_receipt": "R1.json",
        "source_receipt_digest": project.receipt_digest(1), "finding_ids": list(finding_ids),
        "class_ids": list(class_ids), "before_head": project.receipt(1)["head"], "engine": "codex", "manual": False,
        "started_at": "2026-10-24T00:00:00Z"})


FIXED = {"src/app.py": "print('fixed')\n"}


class RefusalTest(unittest.TestCase):
    def test_fix_and_manual_refuse_intent_different_from_source_review(self):
        source = ".oah/tasks/t1/PACKET.md"
        for change in ("revision", "source", "committed bytes"):
            for mode in ("automatic", "manual"):
                with self.subTest(change=change, mode=mode):
                    project = FixProject(self)
                    project.changes_requested()
                    packet = PACKET
                    bound_source = source
                    if change == "revision":
                        packet = packet.replace("Revision: 1", "Revision: 2").replace(
                            "Reports miss files.", "Reports skip files.")
                    elif change == "source":
                        bound_source = ".oah/tasks/t1/UPDATED.md"
                    else:
                        packet = packet.replace("\n", "\r\n")
                    project.commit({bound_source: packet.encode("utf-8"), **FIXED}, trailer=HUMAN_TRAILER)
                    binding = intent.parse(packet).bind(bound_source)
                    store = Store(project.state)
                    store.release(task="t1", agent="claude", reason="rebind", force=False)
                    store.start(task="t1", agent="claude", scope=("src", ".oah/tasks/t1"), note="new intent",
                                ttl_hours=1, info=repo_info(project.repo), intent=binding)
                    self.assertEqual(intent.explain_binding(binding, project.repo)[0], "current")
                    project.queue_fix({"write": {"src/app.py": "print('another correction')\n"}})
                    runs_before = sorted((project.state / "runs" / "t1").iterdir())
                    action = project.fix if mode == "automatic" else lambda: project.manual("human")
                    error = project.refused("OAH_INTENT_SOURCE_MISMATCH", action)
                    self.assertIn("oah review --task t1", str(error))
                    self.assertEqual(sorted((project.state / "runs" / "t1").iterdir()), runs_before)

    def test_fix_refuses_every_non_current_intent_state(self):
        def no_lease(project):
            Store(project.state).release(task="t1", agent="claude", reason="test", force=False)

        def unbound(project):
            no_lease(project)
            Store(project.state).start(task="t1", agent="claude", scope=("src", ".oah/tasks/t1"), note="unbound",
                                       ttl_hours=1, info=repo_info(project.repo))

        def packet(project) -> pathlib.Path:
            return project.repo / ".oah/tasks/t1/PACKET.md"

        def uncommitted(project):
            project.commit({".oah/tasks/t1/PACKET.md": PACKET.replace("Reports miss files.", "Reports skip files.")})
            packet(project).write_text(PACKET, encoding="utf-8")

        cases = (
            ("no lease", no_lease, "no active lease"),
            ("unbound", unbound, "unbound"),
            ("missing", lambda p: packet(p).unlink(), "missing"),
            ("draft", lambda p: packet(p).write_text(PACKET.replace("Status: accepted", "Status: draft"),
                                                    encoding="utf-8"), "stale"),
            ("broken markers", lambda p: packet(p).write_text(PACKET.replace("<!-- OAH_INTENT:END -->", "<!-- END"),
                                                             encoding="utf-8"), "stale"),
            ("not UTF-8", lambda p: packet(p).write_bytes(b"\xff\xfe" + PACKET.encode("utf-8")), "stale"),
            ("edited", lambda p: packet(p).write_text(PACKET.replace("Reports miss files.", "Reports skip files."),
                                                     encoding="utf-8"), "stale"),
            ("not committed as bound", uncommitted, "not committed at HEAD"),
        )
        for label, damage, word in cases:
            with self.subTest(label=label):
                project = FixProject(self)
                project.changes_requested()
                damage(project)
                error = project.refused("OAH_INTENT_NOT_CURRENT", project.fix)
                self.assertIn(word, str(error))
                self.assertIn("Nothing was launched", str(error))
                project.refused("OAH_INTENT_NOT_CURRENT", project.manual, "human")
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        self.assertEqual(project.fix().code, "OAH_FIX_APPLIED", "a current intent dispatches")

    def test_fix_refuses_without_a_changes_requested_receipt(self):
        project = FixProject(self)
        error = project.refused("OAH_FIX_NOTHING_TO_FIX", project.fix)
        self.assertIn("oah review --task t1", str(error))
        project.queue({})
        self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")
        error = project.refused("OAH_FIX_NOT_REQUESTED", project.fix)
        self.assertIn("converged", str(error))
        project = FixProject(self)
        project.queue({"findings": [bug(fix_kind="design")]})
        self.assertEqual(project.review().code, "OAH_REVIEW_NEEDS_DECISION")
        error = project.refused("OAH_FIX_NOT_REQUESTED", project.fix)
        self.assertIn("--decision", str(error))
        project.refused("OAH_FIX_NOT_REQUESTED", project.manual, "human")

    def test_fix_refuses_unfinished_or_unbound_attempts(self):
        project = FixProject(self)
        project.changes_requested()
        publish_start(project)
        for action, args in ((project.fix, ()), (project.manual, ("human",)), (project.bind, ())):
            error = project.refused("OAH_FIX_ATTEMPT_UNFINISHED", action, *args)
            self.assertIn("A1", str(error))
            self.assertIn("oah fix --abort --task t1", str(error))
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        self.assertEqual(project.fix().code, "OAH_FIX_APPLIED")
        git(project.repo, "stash", "push", "-q", "-m", "set aside")  # a clean tree, still waiting for its commit
        for action, args in ((project.fix, ()), (project.manual, ("human",)), (project.abort, ())):
            code = "OAH_FIX_NOTHING_TO_ABORT" if action == project.abort else "OAH_FIX_BIND_REQUIRED"
            error = project.refused(code, action, *args)
            self.assertIn("oah fix --bind --task t1", str(error))

    def test_fix_refuses_changed_history(self):
        project = FixProject(self)
        project.changes_requested()
        path = project.folder() / "R1.json"
        path.write_bytes(path.read_bytes().replace(b'"P1"', b'"P3"'))
        error = project.refused("OAH_FIX_HISTORY_INVALID", project.fix)
        self.assertIn("digest", str(error))

    def test_fix_refuses_a_dirty_tree_or_an_unrelated_head(self):
        project = FixProject(self)
        project.changes_requested()
        (project.repo / "src/app.py").write_text("print('uncommitted')\n", encoding="utf-8")
        error = project.refused("OAH_FIX_DIRTY_TREE", project.fix)
        self.assertIn("src/app.py", str(error))
        git(project.repo, "checkout", "-q", "--", "src/app.py")
        git(project.repo, "checkout", "-q", "-b", "elsewhere", project.base)
        project.commit({"src/other.py": "print('other')\n"})
        error = project.refused("OAH_FIX_HEAD_UNRELATED", project.fix)
        self.assertIn(project.receipt(1)["head"], str(error))

    def test_fix_refuses_unknown_or_already_corrected_findings(self):
        project = FixProject(self)
        project.changes_requested(bug(1), bug(2), bug(3, severity="P2"))
        for chosen in (["R1-F9"], ["R1-F3"]):
            error = project.refused("OAH_FIX_FINDING_UNKNOWN", project.fix, findings=tuple(chosen))
            self.assertIn(chosen[0], str(error))
        project.queue_fix({"write": FIXED}, {"write": {"src/app.py": "print('fixed twice')\n"}})
        first = project.fix(findings=("R1-F1",))
        self.assertEqual(first.code, "OAH_FIX_APPLIED", first.message)
        project.run_commands(first.commands)
        self.assertEqual(project.bind().code, "OAH_FIX_BOUND")
        second = project.fix()
        self.assertEqual(second.code, "OAH_FIX_APPLIED", second.message)
        brief = project.fix_calls()[1]["prompt"]
        self.assertIn("R1-F2", brief)
        self.assertNotIn("- R1-F1 ·", brief, "an applied finding is not corrected again by default")
        project.run_commands(second.commands)
        self.assertEqual(project.bind().code, "OAH_FIX_BOUND")
        error = project.refused("OAH_FIX_NOTHING_TO_FIX", project.fix)
        self.assertIn("A1", str(error))
        self.assertIn("A2", str(error))
        self.assertIn("oah review --task t1", str(error))

    def test_fix_refuses_a_non_codex_correction_stage(self):
        project = FixProject(self, config='[stages]\ncorrection = "claude"\n')
        project.changes_requested()
        error = project.refused("OAH_FIX_ENGINE_UNSUPPORTED", project.fix)
        self.assertIn("--manual --engine claude", str(error))

    def test_fix_refuses_dispatched_workers(self):
        project = FixProject(self)
        project.changes_requested()
        env = dict(project.env, OAH_WORKER="1")
        for action in (lambda: project.fix(env=env), lambda: fix.bind("t1", cwd=project.repo, env=env),
                       lambda: fix.record_manual("t1", engine="human", cwd=project.repo, env=env),
                       lambda: fix.abort("t1", cwd=project.repo, env=env)):
            project.refused("OAH_FIX_WORKER_REFUSED", action)

    def test_another_fix_or_review_of_the_task_is_running(self):
        project = FixProject(self)
        project.changes_requested()
        with StateLock(receipts.task_lock_path(project.state, "t1"), timeout=0):
            for action, args in ((project.fix, ()), (project.manual, ("human",)), (project.bind, ()),
                                 (project.abort, ())):
                error = project.refused("OAH_FIX_BUSY", action, *args)
                self.assertIn("t1", str(error))


class WorkerTest(unittest.TestCase):
    def test_corrector_staging_a_user_file_withholds_commit_guidance(self):
        project = FixProject(self)
        project.changes_requested()
        name = "src/notes.txt"
        (project.repo / name).write_bytes(b"user's original\n")
        project.queue_fix({"write": {name: "overwritten\n", **FIXED}})
        execute = call.execute

        def staged(*args, **kwargs):
            result = execute(*args, **kwargs)
            git(project.repo, "add", "--", name)
            return result

        with mock.patch.object(call, "execute", side_effect=staged):
            result = project.fix()
        self.assertEqual((result.outcome, result.commands), ("failed", []))
        self.assertIn("preexisting untracked", "\n".join(result.lines))
        self.assertIn("-user's original", "\n".join(result.lines))

    def test_preexisting_untracked_bytes_are_compared_even_with_unchanged_metadata(self):
        project = FixProject(self)
        path = project.repo / "src/notes.txt"
        path.write_bytes(b"old\x00\r\n")
        snapshot = {"src/notes.txt": fix._file_snapshot(project.repo, "src/notes.txt")}
        info = path.stat()
        path.write_bytes(b"new\x00\r\n")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        changes = fix._changes(project.repo, project.head, snapshot)
        self.assertEqual(changes.user, ("src/notes.txt",))
        self.assertEqual(changes.new, ())
        self.assertIn(b"Binary files differ", fix._patch(project.repo, project.head, changes, snapshot))

    def test_preexisting_untracked_modifications_and_deletions_are_reported(self):
        for name in ("src/notes.txt", "outside/notes.txt"):
            for content in ("overwritten\n", None):
                with self.subTest(name=name, content=content):
                    project = FixProject(self)
                    project.changes_requested()
                    path = project.repo / name
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(b"user's original\r\n")
                    project.queue_fix({"write": {name: content}})
                    result = project.fix()
                    self.assertNotEqual(result.outcome, "no-change")
                    self.assertIn(name, "\n".join(result.lines))
                    self.assertIn(name, project.record("A1-outcome.json")["evidence"])
                    patch = (pathlib.Path(result.run_dir) / "correction.patch").read_bytes()
                    self.assertIn(b"-user's original\r\n", patch)
                    if content is not None:
                        self.assertIn(b"+overwritten\n", patch)
                    self.assertFalse(any(name in command for command in result.commands))
                    self.assertEqual(any("OAH_FIX_OUT_OF_SCOPE" in warning for warning in result.warnings),
                                     name.startswith("outside/"))
                    saved = json.loads((pathlib.Path(result.run_dir) / "correction.json").read_text(encoding="utf-8"))
                    self.assertIn(name, saved["user_files"])
                    self.assertNotIn(name, saved["new_files"])

    def test_correction_runs_as_a_worker_in_the_leased_worktree(self):
        project = FixProject(self)
        project.changes_requested()
        other = make_tmp_dir(self) / "other"
        git(project.repo, "worktree", "add", "-q", "--detach", str(other), project.base)
        project.queue_fix({"write": FIXED, "probe_lease": True})
        before = [(lease.to_json(), stale) for lease, stale in Store(project.state).snapshot()]
        result = project.fix(cwd=other)
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        [record] = project.fix_calls()
        self.assertTrue(os.path.samefile(record["cwd"], project.repo), "the corrector runs in the leased worktree")
        argv = record["argv"][4:]
        self.assertEqual(argv[:1], ["exec"])
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertTrue(os.path.samefile(argv[argv.index("-C") + 1], project.repo))
        self.assertFalse(pathlib.Path(argv[argv.index("-o") + 1]).is_relative_to(project.state),
                         "the corrector never writes its raw final message into retained state")
        env = record["env"]
        self.assertEqual((env["OAH_WORKER"], env["OAH_TASK"], env["OAH_RUN_ID"]),
                         ("1", "t1", pathlib.Path(result.run_dir).name))
        renew, finish, bind = record["probes"]
        for probe, code in ((renew, "OAH_WORKER_LEASE_REFUSED"), (finish, "OAH_WORKER_LEASE_REFUSED"),
                            (bind, "OAH_FIX_WORKER_REFUSED")):
            self.assertEqual(probe["returncode"], 1, probe)
            self.assertTrue(probe["stderr"].startswith(code), probe)
        after = [(lease.to_json(), stale) for lease, stale in Store(project.state).snapshot()]
        self.assertEqual(after, before, "neither the worker nor oah fix touches the lease")
        self.assertEqual(git(project.repo, "rev-parse", "HEAD"), project.head, "oah fix never commits")
        self.assertEqual(git(project.repo, "status", "--porcelain"), "M src/app.py")
        self.assertEqual(git(other, "status", "--porcelain"), "", "the caller's worktree is untouched")

    def test_brief_carries_the_findings_and_the_review_section(self):
        for label, options, autocrlf in (("lf", {}, False), ("crlf", {"newline": "\r\n"}, False),
                                         ("autocrlf", {"attributes": None}, True)):
            with self.subTest(label=label):
                project = FixProject(self, **options)
                if autocrlf:
                    git(project.repo, "config", "core.autocrlf", "true")
                    packet = project.repo / ".oah/tasks/t1/PACKET.md"
                    packet.unlink()
                    git(project.repo, "checkout", "--", ".oah/tasks/t1/PACKET.md")
                    self.assertIn(b"\r\n", packet.read_bytes())
                    self.assertEqual(git(project.repo, "status", "--porcelain"), "")
                project.changes_requested(bug(1), bug(2, severity="P0"), bug(3, severity="P2"))
                project.queue_fix({"write": FIXED})
                result = project.fix()
                self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
                review_brief, brief = project.calls()[0]["prompt"], project.fix_calls()[0]["prompt"]
                section = briefs.raw_section(review_brief)
                self.assertIsNotNone(section)
                self.assertEqual(briefs.raw_section(brief), section, "the intent section is byte for byte the same")
                self.assertTrue(brief.startswith("You are a dispatched oah worker (OAH_WORKER=1, run "))
                self.assertIn("- Task: t1 · Stage: correction · Engine: codex", brief)
                self.assertIn("- Scope: src, .oah/tasks/t1", brief)
                self.assertIn("## Round instructions\n## Correction of R1 (A1)\n", brief)
                for ident in ("R1-F1", "R1-C1", "R1-F2", "R1-C2", project.receipt_digest(1)):
                    self.assertIn(ident, brief)
                self.assertNotIn("R1-F3", brief, "P2 findings are follow-ups, not corrections")
                self.assertTrue(brief.endswith(intent.FOOTER))
                self.assertEqual(brief.splitlines().count(intent.FOOTER_MARKER), 1)
                self.assertEqual(brief.splitlines().count(briefs.VERIFY_MARKER), 1)
                self.assertNotRegex(brief, r"\boah (start|renew|finish|release)\b", "a worker brief carries no lease "
                                    "command (C1)")

    def test_out_of_scope_and_intent_changes_warn(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": {"src/app.py": "print('fixed')\n", "docs/notes.md": "notes\n",
                                     ".oah/tasks/t1/PACKET.md": PACKET.replace("Reports miss files.", "Changed.")}})
        result = project.fix()
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        scope = next(warning for warning in result.warnings if warning.startswith("OAH_FIX_OUT_OF_SCOPE"))
        self.assertIn("docs/notes.md", scope)
        self.assertNotIn("src/app.py", scope)
        changed = next(warning for warning in result.warnings if warning.startswith("OAH_FIX_INTENT_CHANGED"))
        self.assertIn(".oah/tasks/t1/PACKET.md", changed)

    def test_a_corrector_that_commits_fails_the_attempt(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED, "commit": True})
        result = project.fix()
        self.assertEqual((result.code, result.outcome, result.exit_code), ("OAH_FIX_FAILED", "failed", 1))
        self.assertIn("HEAD moved", result.message)
        self.assertIn("git log", result.message)
        self.assertNotIn("git restore", result.message, "committed changes cannot be restored away")
        self.assertIn("HEAD moved", project.record("A1-outcome.json")["evidence"])
        self.assertEqual(result.commands, [])


class OutcomeTest(unittest.TestCase):
    def stop_before_popen_returns(self, stop):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"hang": True})
        popen = subprocess.Popen
        children = []

        def interrupted(argv, *args, **kwargs):
            child = popen(argv, *args, **kwargs)
            if str(FAKE_FIX) in argv and "exec" in argv:
                children.append(child)
                signal.raise_signal(stop)  # native creation succeeded, but the caller has no reference yet
            return child

        from oah.engines.proc import kill_tree

        try:
            with default_sigint(), mock.patch.object(subprocess, "Popen", side_effect=interrupted):
                with self.assertRaises(fix.FixInterrupted):
                    project.fix(timeout_s=120)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll(), "the worker survived an aborted attempt")
            self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
            self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")
        finally:
            for child in children:
                if child.poll() is None:
                    kill_tree(child)
                child.communicate(timeout=10)

    def test_sigint_before_popen_returns_stops_the_worker(self):
        self.stop_before_popen_returns(signal.SIGINT)

    @unittest.skipIf(os.name == "nt", "POSIX SIGTERM handler")
    def test_sigterm_before_popen_returns_stops_the_worker(self):
        self.stop_before_popen_returns(signal.SIGTERM)

    @unittest.skipUnless(os.name == "nt", "native Windows suspended worker and job ownership")
    def test_windows_sigbreak_before_popen_returns_stops_the_worker(self):
        self.stop_before_popen_returns(signal.SIGBREAK)

    def test_repeated_git_inspection_errors_still_publish_a_terminal_outcome(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                project = FixProject(self)
                project.changes_requested()
                project.queue_fix({"write": FIXED})
                run_git = fix.run_git
                inspecting = False

                def fail_inspection(root, *args, **kwargs):
                    if inspecting:
                        raise PermissionError("git inspection unavailable")
                    return run_git(root, *args, **kwargs)

                execute = call.execute

                def dispatched(*args, **kwargs):
                    nonlocal inspecting
                    result = execute(*args, **kwargs)
                    inspecting = True
                    if interrupt:
                        raise KeyboardInterrupt("stop after dispatch")
                    return result

                with mock.patch.object(call, "execute", side_effect=dispatched), \
                        mock.patch.object(fix, "run_git", side_effect=fail_inspection):
                    with self.assertRaises((fix.FixInterrupted, ReviewError)):
                        project.fix()
                self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
                outcome = project.record("A1-outcome.json")
                self.assertEqual(outcome["outcome"], "aborted" if interrupt else "failed")
                self.assertIn("could not list", outcome["evidence"])

    def test_result_diagnostic_failure_is_settled_as_failed(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        with mock.patch.object(fix._Attempt, "_result", side_effect=PermissionError("diagnostic unavailable")):
            with self.assertRaises(ReviewError):
                project.fix()
        self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")

    def completed_tree_stops_before_inspection(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"spawn_descendant": True})
        heartbeat = project.fix_dir / "descendant.heartbeat"

        def inspect(*_args):
            self.assertTrue(heartbeat.exists())
            time.sleep(0.1)  # allow Windows job termination to finish
            before = heartbeat.read_bytes()
            time.sleep(0.2)
            self.assertEqual(heartbeat.read_bytes(), before, "the corrector's descendant is still writing")
            raise KeyboardInterrupt("after the corrector exited")

        with mock.patch.object(fix._Attempt, "_judge", side_effect=inspect):
            try:
                with self.assertRaises(fix.FixInterrupted):
                    project.fix()
            finally:
                pid_file = project.fix_dir / "descendant.txt"
                if os.name != "nt" and pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")

    @unittest.skipIf(os.name == "nt", "POSIX process group cleanup")
    def test_completed_corrector_tree_stops_before_inspection(self):
        self.completed_tree_stops_before_inspection()

    @unittest.skipUnless(os.name == "nt", "native Windows job cleanup")
    def test_windows_completed_corrector_tree_stops_before_inspection(self):
        self.completed_tree_stops_before_inspection()

    def test_exception_during_post_run_inspection_settles_the_attempt(self):
        for exception in (KeyboardInterrupt, SystemExit, RuntimeError):
            with self.subTest(exception=exception):
                project = FixProject(self)
                project.changes_requested()
                project.queue_fix({"write": FIXED})
                with mock.patch.object(fix._Attempt, "_judge", side_effect=exception("inspection stopped")):
                    with self.assertRaises(BaseException) as caught:
                        project.fix()
                self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
                self.assertIsInstance(caught.exception, (fix.FixInterrupted, ReviewError))
                self.assertEqual(project.record("A1-outcome.json")["outcome"],
                                 "aborted" if exception is KeyboardInterrupt else "failed")

    def outcome_names(self, project: FixProject) -> list[str]:
        return [name for name in project.names() if name.startswith("A")]

    def test_start_is_published_before_launch(self):
        project = FixProject(self)
        project.changes_requested(bug(1), bug(2))
        project.queue_fix({"write": FIXED})
        result = project.fix()
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        self.assertEqual(project.fix_calls()[0]["indexed"], ["R1.json", "A1-start.json"])
        self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
        start = project.record("A1-start.json")
        self.assertEqual({key: start[key] for key in ("source_receipt", "source_receipt_digest", "finding_ids",
                                                      "class_ids", "before_head", "engine", "manual", "run_id")},
                         {"source_receipt": "R1.json", "source_receipt_digest": project.receipt_digest(1),
                          "finding_ids": ["R1-F1", "R1-F2"], "class_ids": ["R1-C1", "R1-C2"],
                          "before_head": project.head, "engine": "codex", "manual": False,
                          "run_id": pathlib.Path(result.run_dir).name})
        outcome = project.record("A1-outcome.json")
        self.assertEqual((outcome["outcome"], outcome["after_head"]), ("applied", None))
        self.assertIn(result.attempt_id, ("A1",))
        self.assertEqual([item["name"] for item in result.records], ["A1-start.json", "A1-outcome.json"])

    def test_every_ending_leaves_one_terminal_outcome(self):
        def launch_failure(project):
            popen = subprocess.Popen

            def refuse(argv, *args, **kwargs):
                if "exec" in argv and str(FAKE_FIX) in argv:
                    raise OSError("simulated launch failure")
                return popen(argv, *args, **kwargs)

            return mock.patch.object(subprocess, "Popen", side_effect=refuse)

        cases = (
            ("applied", {"write": FIXED}, {}, None, "OAH_FIX_APPLIED", "applied", "changed"),
            ("no change", {}, {}, None, "OAH_FIX_NO_CHANGE", "no-change", "without changing"),
            ("non-zero exit", {"write": FIXED, "exit": 3}, {}, None, "OAH_FIX_FAILED", "failed", "OAH_ENGINE_FAILED"),
            ("launch failure", {}, {}, launch_failure, "OAH_FIX_FAILED", "failed", "OAH_ENGINE_LAUNCH_FAILED"),
            ("effort downgrade", {"header_effort": "low"}, {}, None, "OAH_FIX_FAILED", "failed", "OAH_EFFORT_DOWNGRADED"),
            ("effort mismatch", {"header_effort": "ultra"}, {}, None, "OAH_FIX_FAILED", "failed", "OAH_EFFORT_MISMATCH"),
            ("timeout", {"hang": True}, {"timeout_s": 1}, None, "OAH_FIX_FAILED", "failed", "OAH_RUN_TIMEOUT"),
        )
        for label, step, fields, patcher, code, outcome, word in cases:
            with self.subTest(label=label):
                project = FixProject(self)
                project.changes_requested()
                project.queue_fix(step)
                if patcher is None:
                    result = project.fix(**fields)
                else:
                    with patcher(project):
                        result = project.fix(**fields)
                self.assertEqual((result.code, result.outcome), (code, outcome), result.message)
                self.assertEqual(result.exit_code, 0 if outcome == "applied" else 1)
                self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
                record = project.record("A1-outcome.json")
                self.assertEqual((record["outcome"], record["after_head"]), (outcome, None))
                self.assertIn(word, record["evidence"])
                [attempt] = receipts.attempts(receipts.load(project.state, "t1"))
                self.assertTrue(attempt.terminal)
                if label == "non-zero exit":
                    self.assertIn("src/app.py", result.message, "changes a failed run left behind are named")
                    self.assertEqual(result.commands, [])

    def test_interrupted_correction_stops_the_corrector_and_records_aborted(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"hang": True})
        pid_file = project.fix_dir / "pid-000.txt"
        with default_sigint():
            thread = interrupt_when(pid_file)
            with self.assertRaises(fix.FixInterrupted) as caught:
                project.fix(timeout_s=120)
            thread.join(5)
        self.assertIsInstance(caught.exception, KeyboardInterrupt)
        self.assertEqual(caught.exception.code, "OAH_FIX_INTERRUPTED")
        self.assertIn("A1", caught.exception.message)
        self.assertFalse(call._alive(int(pid_file.read_text(encoding="utf-8"))), "Ctrl-C stops the corrector")
        self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")
        run = json.loads((project.state / "runs/t1" / project.record("A1-start.json")["run_id"] / "result.json")
                         .read_text(encoding="utf-8"))
        self.assertTrue(run["error"].startswith("OAH_RUN_INTERRUPTED:"))
        project.queue_fix({"write": FIXED})
        self.assertEqual(project.fix().code, "OAH_FIX_APPLIED", "the lock was released and A1 is settled")

    def test_signals_are_held_while_a_record_is_written(self):
        project = FixProject(self)
        project.changes_requested()
        publish = receipts.publish

        def interrupted(state, task, name, record, **kwargs):
            digest = publish(state, task, name, record, **kwargs)
            if name.endswith("-start.json"):
                _thread.interrupt_main()  # Ctrl-C while the start record is written
                time.sleep(0.2)
            return digest

        with default_sigint(), mock.patch.object(receipts, "publish", side_effect=interrupted):
            with self.assertRaises(fix.FixInterrupted):
                project.fix()
        self.assertEqual(project.fix_calls(), [], "an interruption before launch launches nothing")
        self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")


def applied(test: unittest.TestCase, project: FixProject, step: dict | None = None, *findings: dict) -> fix.Result:
    """R1 requests changes, and one Codex correction applies `step`."""
    project.changes_requested(*findings)
    project.queue_fix(step or {"write": FIXED})
    result = project.fix()
    test.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
    return result


class BindTest(unittest.TestCase):
    def test_commit_guidance_runs_in_the_leased_worktree_from_any_caller(self):
        self.assert_guidance_from_any_caller()

    @unittest.skipUnless(os.name == "nt", "native Windows PowerShell 5.1 command quoting")
    def test_powershell_commit_guidance_runs_in_the_leased_worktree_from_any_caller(self):
        self.assert_guidance_from_any_caller(powershell=True)

    def assert_guidance_from_any_caller(self, *, powershell=False):
        for location in ("subdirectory", "other-worktree"):
            with self.subTest(location=location):
                project = FixProject(self, repo_name="leased 'space $cash `tick !")
                project.changes_requested()
                other = project.work / "other"
                git(project.repo, "worktree", "add", "-q", "--detach", str(other), project.base)
                (other / "README.md").write_bytes(b"unrelated caller changes\n")
                git(other, "add", "-u")
                other_head, other_status = git(other, "rev-parse", "HEAD"), git(other, "status", "--porcelain")
                caller = project.repo / ".oah/tasks/t1" if location == "subdirectory" else other
                project.queue_fix({"write": {**FIXED, "README.md": "corrected root file\n", "src/new.py": "NEW = 1\n"}})
                result = project.fix(cwd=caller)
                self.assertEqual(result.outcome, "applied", result.message)
                for command in result.commands:
                    argv = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             command + "; exit $LASTEXITCODE"] if powershell else
                            ["/bin/sh", "-c", command] if os.name != "nt" else shlex.split(command))
                    done = subprocess.run(argv, cwd=caller, env=git_env(), capture_output=True)
                    self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", errors="replace"))
                self.assertEqual(git(project.repo, "status", "--porcelain"), "")
                self.assertEqual(git(project.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines(),
                                 ["README.md", "src/app.py", "src/new.py"])
                self.assertEqual(git(other, "rev-parse", "HEAD"), other_head)
                self.assertEqual(git(other, "status", "--porcelain"), other_status)
                self.assertEqual(project.bind().code, "OAH_FIX_BOUND")

    def test_applied_correction_shows_the_diff_and_the_commit_command(self):
        project = FixProject(self)
        (project.repo / "src/i.py").write_text("THE USER'S OWN UNTRACKED FILE\n", encoding="utf-8")
        result = applied(self, project, {"write": {"src/app.py": "print('fixed')\n", "src/new.py": "NEW = 1\n",
                                                   "src/odd name.py": "ODD = 1\n", "src/[id].py": "ID = 1\n"}})
        text = "\n".join(result.lines)
        for line in ("-print('v1')", "+print('fixed')", "+NEW = 1", "+ODD = 1", "+ID = 1"):
            self.assertIn(line, text)
        self.assertNotIn("THE USER'S OWN", text, "a file that was untracked before the run is not Codex's change")
        command = f"git -C '{project.repo.as_posix()}'"
        self.assertEqual(result.commands, [
            command + " add -u", command + " add -- ':(literal)src/[id].py' src/new.py ':(literal)src/odd name.py'",
            command + ' commit -m "Correct R1-F1 of R1 in task t1 (oah fix A1)" -m "Agent-Engine: codex"'])
        self.assertEqual(result.next_command, "oah fix --bind --task t1")
        for command in (*result.commands, result.next_command):
            self.assertIn(command, result.lines)
        saved = json.loads((pathlib.Path(result.run_dir) / "correction.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["commands"], saved["changed"], saved["new_files"]),
                         (result.commands, ["src/app.py"], ["src/[id].py", "src/new.py", "src/odd name.py"]))
        patch = (pathlib.Path(result.run_dir) / "correction.patch").read_text(encoding="utf-8")
        self.assertEqual((patch.count("+NEW = 1"), patch.count("+print('fixed')")), (1, 1))
        self.assertIn(saved["changes_sha256"], project.record("A1-outcome.json")["evidence"])
        head = project.run_commands(result.commands)
        self.assertTrue(git(project.repo, "log", "-1", "--format=%B").rstrip().endswith("Agent-Engine: codex"))
        self.assertEqual(authors.commit_engines(project.repo, project.receipt(1)["head"], head), [(head, "codex")])
        self.assertEqual(git(project.repo, "status", "--porcelain"), "?? src/i.py",
                         "the literal pathspec added src/[id].py, not the glob match src/i.py")

    def test_bind_records_the_head_binding(self):
        project = FixProject(self)
        head = project.run_commands(applied(self, project).commands)
        bound = project.bind()
        self.assertEqual((bound.code, bound.exit_code, bound.attempt_id), ("OAH_FIX_BOUND", 0, "A1"), bound.message)
        self.assertEqual(bound.next_command, "oah review --task t1")
        record = project.record("A1-head.json")
        self.assertEqual(record["after_head"], head)
        self.assertIn(f"{head[:12]} (codex)", record["evidence"])
        [attempt] = receipts.attempts(receipts.load(project.state, "t1"))
        self.assertEqual((attempt.outcome, attempt.after_head, attempt.problems), ("applied", head, ()))
        project.refused("OAH_FIX_NOTHING_TO_BIND", project.bind)

    def test_bind_refuses_until_committed_cleanly_with_trailers(self):
        project = FixProject(self)
        applied(self, project, {"write": {"src/app.py": "print('fixed')\n", "src/new.py": "NEW = 1\n"}})
        error = project.refused("OAH_FIX_NOT_COMMITTED", project.bind)
        self.assertIn('-m "Agent-Engine: codex"', str(error), "the refusal repeats the commit commands")
        git(project.repo, "add", "-u")
        git(project.repo, "commit", "-q", "-m", "the tracked part" + CLAUDE_TRAILER)
        error = project.refused("OAH_FIX_DIRTY_TREE", project.bind)
        self.assertIn("src/new.py", str(error))
        git(project.repo, "add", "--", "src/new.py")
        git(project.repo, "commit", "-q", "-m", "the new file")
        project.refused("OAH_FIX_TRAILER_MISSING", project.bind)
        git(project.repo, "commit", "-q", "--amend", "-m", "the new file" + CLAUDE_TRAILER)
        error = project.refused("OAH_FIX_ENGINE_MISMATCH", project.bind)
        self.assertIn("Agent-Engine: codex", str(error))
        self.assertIn("--engine", str(error))
        project.refused("OAH_FIX_ENGINE_MISMATCH", project.bind, engine="human")
        bound = project.bind(engine="claude")
        self.assertEqual(bound.code, "OAH_FIX_BOUND", bound.message)
        self.assertIn("--engine claude", project.record("A1-head.json")["evidence"])
        events = [json.loads(line) for line in (project.state / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("--engine claude" in event.get("message", "") for event in events), "the override is logged")
        project = FixProject(self)
        applied(self, project)
        git(project.repo, "stash", "push", "-q", "-u", "-m", "set aside")
        git(project.repo, "checkout", "-q", "-b", "elsewhere", project.base)
        project.commit({"src/other.py": "OTHER = 1\n"}, trailer=CODEX_TRAILER)
        project.refused("OAH_FIX_HEAD_UNRELATED", project.bind)


    def test_bind_and_abort_refuse_attempts_without_a_start(self):
        project = FixProject(self)
        project.changes_requested()
        receipts.publish(project.state, "t1", "A1-outcome.json", {
            "schema": 1, "kind": "attempt-outcome", "task": "t1", "attempt_id": "A1", "outcome": "applied",
            "after_head": None, "evidence": "written by hand", "finished_at": "2026-10-24T00:01:00Z"})
        error = project.refused("OAH_FIX_HISTORY_INVALID", project.bind)
        self.assertIn("no start record", str(error))
        project = FixProject(self)
        project.changes_requested()
        receipts.publish(project.state, "t1", "A1-head.json", {
            "schema": 1, "kind": "attempt-head", "task": "t1", "attempt_id": "A1", "after_head": project.head,
            "evidence": "written by hand", "bound_at": "2026-10-24T00:02:00Z"})
        error = project.refused("OAH_FIX_HISTORY_INVALID", project.abort)
        self.assertIn("A1", str(error))


class ManualTest(unittest.TestCase):
    def test_manual_correction_counts_like_a_codex_attempt(self):
        project = FixProject(self)
        project.changes_requested(bug(1), bug(2))
        project.refused("OAH_FIX_NOT_COMMITTED", project.manual, "human")
        head = project.commit({"src/app.py": "print('fixed by hand')\n"}, "fix by hand", HUMAN_TRAILER)
        recorded = project.manual("human")
        self.assertEqual((recorded.code, recorded.exit_code, recorded.attempt_id), ("OAH_FIX_RECORDED", 0, "A1"),
                         recorded.message)
        start, outcome, bound = (project.record(f"A1-{kind}.json") for kind in ("start", "outcome", "head"))
        self.assertEqual((start["engine"], start["manual"], start["before_head"], start["finding_ids"],
                          start["class_ids"]), ("human", True, project.receipt(1)["head"], ["R1-F1", "R1-F2"],
                                                ["R1-C1", "R1-C2"]))
        self.assertEqual((outcome["outcome"], outcome["after_head"], bound["after_head"]), ("applied", None, head))
        project.queue({"findings": [bug(1, class_id="R1-C1", title="the same defect, worded differently")]})
        result = project.review()
        self.assertEqual(result.code, "OAH_REVIEW_NEEDS_DECISION", result.message)
        receipt = project.receipt(2)
        self.assertEqual(receipt["series_round"], 2, "the next review counted a round")
        self.assertEqual([item["name"] for item in receipt["history"]],
                         ["R1.json", "A1-start.json", "A1-outcome.json", "A1-head.json"])
        self.assertIn("A1 (applied)", receipt["decision_stop_reason"])

    def test_manual_correction_starts_from_the_last_bound_result(self):
        project = FixProject(self)
        project.changes_requested(bug(1), bug(2))
        project.queue_fix({"write": FIXED})
        first = project.fix(findings=("R1-F1",))
        self.assertEqual(first.code, "OAH_FIX_APPLIED", first.message)
        codex_head = project.run_commands(first.commands)
        self.assertEqual(project.bind().code, "OAH_FIX_BOUND")
        head = project.commit({"src/app.py": "print('both fixed')\n"}, "claude fix", CLAUDE_TRAILER)
        self.assertEqual(project.manual("claude").code, "OAH_FIX_RECORDED")
        start = project.record("A2-start.json")
        self.assertEqual((start["before_head"], start["finding_ids"], start["engine"]), (codex_head, ["R1-F2"], "claude"))
        bound = project.record("A2-head.json")
        self.assertEqual(bound["after_head"], head)
        self.assertIn("(claude)", bound["evidence"])
        self.assertNotIn("(codex)", bound["evidence"], "only the commits after the bound result are attributed")

    def test_manual_refuses_without_new_attributed_commits(self):
        project = FixProject(self)
        project.changes_requested()
        project.refused("OAH_FIX_NOT_COMMITTED", project.manual, "human")
        (project.repo / "src/app.py").write_text("print('by hand')\n", encoding="utf-8")
        project.refused("OAH_FIX_DIRTY_TREE", project.manual, "human")
        git(project.repo, "commit", "-q", "-a", "-m", "no trailer")
        project.refused("OAH_FIX_TRAILER_MISSING", project.manual, "human")
        git(project.repo, "commit", "-q", "--amend", "-m", "by claude" + CLAUDE_TRAILER)
        error = project.refused("OAH_FIX_ENGINE_MISMATCH", project.manual, "human")
        self.assertIn("--engine", str(error))
        project.refused("OAH_USAGE", project.manual, "codex")
        self.assertEqual(project.manual("claude").code, "OAH_FIX_RECORDED")

    def test_abort_records_unfinished_attempts_only(self):
        project = FixProject(self)
        project.changes_requested()
        project.refused("OAH_FIX_NOTHING_TO_ABORT", project.abort)
        publish_start(project)
        result = project.abort()
        self.assertEqual((result.code, result.exit_code, result.attempt_id), ("OAH_FIX_ABORTED", 0, "A1"), result.message)
        outcome = project.record("A1-outcome.json")
        self.assertEqual((outcome["outcome"], outcome["after_head"]), ("aborted", None))
        self.assertIn("oah fix --abort", outcome["evidence"])
        project.refused("OAH_FIX_NOTHING_TO_ABORT", project.abort)
        project.queue_fix({"write": FIXED})
        self.assertEqual(project.fix().code, "OAH_FIX_APPLIED", "an aborted attempt is retried")
        self.assertEqual(project.record("A2-start.json")["finding_ids"], ["R1-F1"])

    def test_records_are_never_rewritten(self):
        project = FixProject(self)
        result = applied(self, project)
        written = {path.name: path.read_bytes() for path in project.folder().glob("A*.json")}
        self.assertEqual(sorted(written), ["A1-outcome.json", "A1-start.json"])
        project.run_commands(result.commands)
        bound = project.bind()
        for name, data in written.items():
            self.assertEqual((project.folder() / name).read_bytes(), data, name)
        index = {entry.name: entry.digest for entry in receipts.load(project.state, "t1").entries}
        for item in result.records + bound.records:
            self.assertEqual(index[item["name"]], item["digest"])
        with self.assertRaises(ReviewError) as caught:
            receipts.publish(project.state, "t1", "A1-outcome.json", project.record("A1-outcome.json"))
        self.assertEqual(caught.exception.code, "OAH_REVIEW_RECORD_EXISTS")


class EndToEndTest(unittest.TestCase):
    def test_fix_commit_bind_review_round_trip(self):
        project = FixProject(self)
        result = applied(self, project)
        project.run_commands(result.commands)
        self.assertEqual(project.bind().code, "OAH_FIX_BOUND")
        project.queue({})
        review = project.review()
        self.assertEqual(review.code, "OAH_REVIEW_CONVERGED", review.message)
        receipt = project.receipt(2)
        self.assertEqual((receipt["round"], receipt["series_round"], receipt["outcome"]), (2, 2, "converged"))
        index = {entry.name: (entry.kind, entry.digest) for entry in receipts.load(project.state, "t1").entries}
        expected = [{"kind": index[name][0], "name": name, "digest": index[name][1]}
                    for name in ("R1.json", "A1-start.json", "A1-outcome.json", "A1-head.json")]
        self.assertEqual(receipt["history"], expected)
        self.assertEqual(receipt["history_digest"], receipts.history_digest(expected))
        brief = project.calls()[1]["prompt"]
        for item in expected[1:]:
            self.assertIn(f"#### {item['name']} ({item['kind']}, {item['digest']})", brief)
        self.assertIs(receipt["independent"], False, "codex corrected the work it reviews: an honest label")
        self.assertEqual(receipt["author_engines"], ["claude", "codex"])

    def test_reappearing_attempted_class_stops_for_a_decision(self):
        project = FixProject(self)
        project.run_commands(applied(self, project).commands)
        project.bind()
        project.queue({"findings": [bug(1, class_id="R1-C1", title="the defect came back")]})
        result = project.review()
        self.assertEqual(result.code, "OAH_REVIEW_NEEDS_DECISION", result.message)
        self.assertIn("A1 (applied)", project.receipt(2)["decision_stop_reason"])
        project.refused("OAH_FIX_NOT_REQUESTED", project.fix)

    def test_unfinished_attempt_stops_the_next_review(self):
        project = FixProject(self)
        project.changes_requested()
        publish_start(project)
        for decision in (None, "go on anyway"):
            with self.assertRaises(ReviewError) as caught:
                project.review(decision=decision)
            self.assertEqual(caught.exception.code, "OAH_REVIEW_NEEDS_DECISION")
            self.assertIn("unfinished", str(caught.exception))
            self.assertIn("oah fix --abort --task t1", str(caught.exception))
        self.assertEqual(len(project.calls()), 1, "no review was launched")
        self.assertEqual(project.abort().code, "OAH_FIX_ABORTED")
        project.queue({})
        self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")


class SecretTest(unittest.TestCase):
    def test_file_diffs_disable_submodule_expansion_and_user_diff_drivers(self):
        project = FixProject(self, attributes="* -text\n*.py diff=custom\n")
        project.changes_requested()
        driver, marker = project.work / "diff-driver.py", project.work / "driver-ran"
        driver.write_bytes(b"import pathlib, sys\npathlib.Path(sys.argv[1]).write_bytes(b'ran')\n"
                           b"sys.stdout.buffer.write(b'unsafe driver output\\n')\n")
        command = f'"{pathlib.Path(sys.executable).as_posix()}" "{driver.as_posix()}" "{marker.as_posix()}"'
        for setting in ("diff.external", "diff.custom.command", "diff.custom.textconv"):
            git(project.repo, "config", setting, command)
        git(project.repo, "config", "diff.submodule", "diff")
        project.queue_fix({"write": {**FIXED, "src/new.py": "NEW = 1\n"}})
        built, git_bytes = [], fix._git_bytes

        def capture(root, *args, **kwargs):
            result = git_bytes(root, *args, **kwargs)
            if args[0] == "diff":
                built.append(args)
            return result

        with mock.patch.object(fix, "_git_bytes", side_effect=capture):
            result = project.fix()
        self.assertEqual(result.outcome, "applied", result.message)
        self.assertFalse(marker.exists(), "neither external diff nor textconv drivers may run")
        self.assertNotIn("unsafe driver output", json.dumps(result.to_json()))
        patch = (pathlib.Path(result.run_dir) / "correction.patch").read_bytes()
        self.assertIn(b"+print('fixed')", patch)
        self.assertIn(b"+NEW = 1", patch)
        self.assertEqual(len(built), 2, "both tracked and new files use the same diff protections")
        for args in built:
            for option in ("--submodule=short", "--no-ext-diff", "--no-textconv"):
                self.assertIn(option, args)

    def test_submodule_changes_fail_closed_before_building_a_diff(self):
        for change in ("dirty", "ignored", "deleted", "added", "untracked", "index-only"):
            with self.subTest(change=change):
                project = FixProject(self)
                source = make_git_repo(self)
                git_commit(source, {"private.txt": b"before\n"})
                remote = project.work / "nested.git"
                git(source, "clone", "--bare", "-q", str(source), str(remote))
                name = "src/nested"

                def add_submodule():
                    git(project.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(remote), name)

                if change not in ("added", "untracked", "index-only"):
                    add_submodule()
                    project.commit({}, "add submodule")
                project.changes_requested()
                git(project.repo, "config", "diff.submodule", "diff")
                if change == "ignored":
                    git(project.repo, "config", "diff.ignoreSubmodules", "all")
                    git(project.repo, "config", f"submodule.{name}.ignore", "all")
                key = "runtime-" + token_source.token_hex(16) + "\ncontinuation-" + token_source.token_hex(16)
                project.env["OPENAI_API_KEY"] = key
                writes = dict(FIXED)
                if change in ("dirty", "ignored"):
                    writes[f"{name}/private.txt"] = key + "\n"
                project.queue_fix({"write": writes})
                execute, git_bytes = call.execute, fix._git_bytes
                built = []

                def dispatched(*args, **kwargs):
                    result = execute(*args, **kwargs)
                    if change == "deleted":
                        git(project.repo, "rm", "-q", "-f", "--", name)
                    elif change in ("added", "untracked"):
                        add_submodule()
                        git_commit(project.repo / name, {"private.txt": (key + "\n").encode("utf-8")})
                        git(project.repo, "add", "--", name)
                        if change == "untracked":
                            git(project.repo, "reset", "-q", "--", name)
                    elif change == "index-only":
                        oid = git(source, "rev-parse", "HEAD")
                        git(project.repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},{name}")
                    return result

                def capture(root, *args, **kwargs):
                    result = git_bytes(root, *args, **kwargs)
                    if args[0] == "diff":
                        built.append((args, result))
                    return result

                with mock.patch.object(call, "execute", side_effect=dispatched), \
                        mock.patch.object(fix, "_git_bytes", side_effect=capture):
                    result = project.fix()
                self.assertEqual((result.outcome, result.commands), ("failed", []))
                self.assertIsNone(result.next_command)
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                self.assert_private(project, [key, *key.splitlines()], json.dumps(result.to_json()))
                self.assertNotIn(name, json.dumps(result.to_json()), "unsupported changes are named by position only")
                patch = (pathlib.Path(result.run_dir) / "correction.patch").read_text(encoding="utf-8")
                self.assertIn("OAH_FIX_UNSUPPORTED_SUBMODULE", patch)
                self.assertIn("[file ", patch)
                self.assertNotIn("diff --git", patch, "a submodule change withholds the whole diff")
                self.assertEqual(built, [], "no content diff is built for an unsupported attempt")

    def test_raw_multiline_credentials_withhold_every_affected_file_diff(self):
        for channel in ("tracked", "new", "staged", "deleted", "replaced", "user"):
            with self.subTest(channel=channel):
                project = FixProject(self)
                key = "runtime-" + token_source.token_hex(16) + "\ncontinuation-" + token_source.token_hex(16)
                name = "src/private.txt"
                if channel in ("tracked", "deleted", "replaced"):
                    project.commit({name: key + "\n" if channel != "tracked" else "before\n"})
                project.changes_requested()
                project.env["OPENAI_API_KEY"] = key
                if channel == "user":
                    (project.repo / name).write_bytes((key + "\n").encode("utf-8"))
                content = None if channel == "deleted" else "replacement\n" if channel in ("replaced", "user") else key + "\n"
                project.queue_fix({"write": {name: content, **FIXED}})
                execute = call.execute

                def dispatched(*args, **kwargs):
                    result = execute(*args, **kwargs)
                    if channel == "staged":
                        git(project.repo, "add", "--", name)
                    return result

                built = []
                git_bytes = fix._git_bytes

                def capture(root, *args, **kwargs):
                    result = git_bytes(root, *args, **kwargs)
                    if args[0] == "diff":
                        built.append(result)
                    return result

                with mock.patch.object(call, "execute", side_effect=dispatched), \
                        mock.patch.object(fix, "_git_bytes", side_effect=capture):
                    result = project.fix()
                self.assertEqual((result.outcome, result.commands), ("failed", []))
                self.assertIsNone(result.next_command)
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                self.assert_private(project, [key, *key.splitlines()], json.dumps(result.to_json()))
                self.assertNotIn(name, json.dumps(result.to_json()), "identify a withheld file by position only")
                patch = (pathlib.Path(result.run_dir) / "correction.patch").read_text(encoding="utf-8")
                self.assertIn("withheld", patch)
                self.assertIn("src/app.py", patch, "safe changed files remain reviewable")
                for raw in built:
                    for line in key.splitlines():
                        self.assertFalse(line.encode("utf-8") in raw, "Git built a diff containing credential content")

    def test_untracked_snapshots_never_retain_credentials(self):
        for sensitive_name in (False, True):
            with self.subTest(sensitive_name=sensitive_name):
                project = FixProject(self)
                project.changes_requested()
                key = "runtime-" + token_source.token_hex(16)
                if not sensitive_name:
                    key += "\ncontinuation-" + token_source.token_hex(16)
                project.env["OPENAI_API_KEY"] = key
                name = "src/" + (key if sensitive_name else "notes.txt")
                (project.repo / name).write_bytes((key + "\r\n").encode("utf-8"))
                project.queue_fix({"write": {name: "overwritten\n"}})
                result = project.fix()
                self.assertEqual((result.outcome, result.commands), ("failed", []))
                self.assert_private(project, [key, *key.splitlines()], json.dumps(result.to_json()))
                patch = (pathlib.Path(result.run_dir) / "correction.patch").read_text(encoding="utf-8")
                self.assertIn("withheld", patch)

    def launches(self) -> tuple[list, object]:
        found: list = []
        popen = subprocess.Popen

        def capture(argv, *args, **kwargs):
            if str(FAKE_FIX) in [str(part) for part in argv]:
                found.append((list(argv), dict(kwargs.get("env") or {})))
            return popen(argv, *args, **kwargs)

        return found, mock.patch.object(subprocess, "Popen", side_effect=capture)

    def assert_private(self, project: FixProject, keys: list[str], *texts: str) -> None:
        artifacts = [text.encode("utf-8") for text in texts]
        artifacts += [path.read_bytes() for path in project.state.rglob("*") if path.is_file()]
        for data in artifacts:
            for key in keys:
                for form in (key, json.dumps(key)[1:-1], json.dumps(key, ensure_ascii=False)[1:-1]):
                    self.assertFalse(form.encode("utf-8") in data, "a runtime credential reached a retained artifact")

    def test_corrector_environment_excludes_credentials(self):
        project = FixProject(self)
        project.changes_requested()
        excluded = ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY", "openai_api_key",
                    "Codex_Api_Key", "OAH_PROBE_PRIVATE", "PYTHONPATH", "NODE_OPTIONS")
        project.env.update({name: "runtime-" + token_source.token_hex(16) for name in excluded})
        project.env["HTTPS_PROXY"] = "http://proxy.invalid:8080"
        project.queue_fix({"write": FIXED})
        found, patch = self.launches()
        with patch:
            result = project.fix()
        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        self.assertEqual([("--version" in argv, "exec" in argv) for argv, _env in found], [(True, False), (False, True)])
        expected = runner.reviewer_env(project.env)
        self.assertEqual(expected["HTTPS_PROXY"], "http://proxy.invalid:8080")
        for argv, env in found:
            wanted = dict(expected)
            if "exec" in argv:
                wanted.update(OAH_WORKER="1", OAH_TASK="t1", OAH_RUN_ID=pathlib.Path(result.run_dir).name)
            self.assertEqual(env, wanted)
            self.assertFalse({name.upper() for name in excluded} & {name.upper() for name in env})
        self.assertFalse(any(name.upper() in ("CODEX_API_KEY", "OPENAI_API_KEY") for name in project.fix_calls()[0]["env"]))
        self.assert_private(project, [project.env[name] for name in excluded], json.dumps(result.to_json()))

    def test_proxy_userinfo_refuses_before_any_launch(self):
        for name in ("HTTPS_PROXY", "http_proxy", "ALL_PROXY", "NO_PROXY"):
            with self.subTest(name=name):
                project = FixProject(self)
                project.changes_requested()
                password = token_source.token_hex(24)
                project.env[name] = f"http://user:{password}@proxy.invalid:8080"
                runs_before = sorted((project.state / "runs" / "t1").iterdir())
                found, patch = self.launches()
                with patch:
                    error = project.refused("OAH_FIX_PROXY_CREDENTIALS", project.fix)
                self.assertEqual(found, [], "not even a version probe may start")
                self.assertEqual(sorted((project.state / "runs" / "t1").iterdir()), runs_before)
                self.assert_private(project, [password], str(error))

    def test_runtime_credentials_never_reach_records_or_output(self):
        for channel in ("stdout", "stderr", "last_message", "file", "header_effort"):
            with self.subTest(channel=channel):
                project = FixProject(self)
                project.changes_requested()
                keys = ["runtime-" + token_source.token_hex(8), "runtime-" + token_source.token_hex(8) + '"\\\n한글']
                project.env.update(OPENAI_API_KEY=keys[0], CODEX_API_KEY=keys[1])
                echoed = " ".join(keys)
                step = {"write": dict(FIXED)}
                if channel == "file":
                    step["write"]["src/app.py"] = f"print({json.dumps(echoed)})\n"
                elif channel == "header_effort":
                    step["header_effort"] = keys[0]
                else:
                    step[channel] = echoed
                project.queue_fix(step)
                result = project.fix()
                expected = "OAH_FIX_FAILED" if channel in ("header_effort", "file") else "OAH_FIX_APPLIED"
                self.assertEqual(result.code, expected, result.message)
                self.assert_private(project, keys, json.dumps(result.to_json()), result.message, *result.lines,
                                    *result.warnings)
                if channel == "file":
                    self.assertIn("withheld", "\n".join(result.lines))
                    self.assertEqual(result.commands, [])

    def test_credential_filenames_never_reach_metadata_or_executable_guidance(self):
        for channel in ("new", "tracked", "out of scope", "unquotable", "encoded", "failed"):
            with self.subTest(channel=channel):
                project = FixProject(self)
                project.changes_requested()
                key = "runtime-" + token_source.token_hex(16)
                if channel == "encoded":
                    key += "-한글"
                name = ("outside/" if channel == "out of scope" else "src/") + key
                if channel == "unquotable":
                    name += "'file.txt"
                if channel == "tracked":
                    project.commit({name: "before\n"})
                project.env["OPENAI_API_KEY"] = key
                project.queue_fix({"write": {name: "after\n", **FIXED}, "exit": 1 if channel == "failed" else 0})
                result = project.fix()
                self.assert_private(project, [key], json.dumps(result.to_json()), result.message, *result.lines,
                                    *result.warnings)
                self.assertEqual((result.code, result.outcome), ("OAH_FIX_FAILED", "failed"))
                self.assertEqual(result.commands, [], "sensitive filenames must withhold executable guidance")
                saved = json.loads((pathlib.Path(result.run_dir) / "correction.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["commands"], [])
                if channel == "encoded":
                    patch = (pathlib.Path(result.run_dir) / "correction.patch").read_text(encoding="utf-8")
                    self.assertFalse("diff --git" in patch, "withhold Git-quoted sensitive filenames too")
                project.refused("OAH_FIX_NOTHING_TO_BIND", project.bind)

    def test_bind_withholds_saved_commands_that_now_contain_a_credential(self):
        project = FixProject(self)
        project.changes_requested()
        key = "runtime-" + token_source.token_hex(16)
        project.queue_fix({"write": {"src/" + key: "correction\n"}})
        self.assertEqual(project.fix().code, "OAH_FIX_APPLIED")
        project.env["OPENAI_API_KEY"] = key
        error = project.refused("OAH_FIX_NOT_COMMITTED", project.bind)
        self.assertFalse(key in str(error), "a runtime credential reached a refusal")
        self.assertNotIn("git add", str(error), "do not print a redacted executable command")
        self.assertNotIn("git commit", str(error))

    def test_fix_exception_contains_no_credential(self):
        project = FixProject(self)
        project.changes_requested()
        key = "runtime-" + token_source.token_hex(24)
        project.env["OPENAI_API_KEY"] = key
        error = project.refused("OAH_FIX_FINDING_UNKNOWN", project.fix, findings=(key,))
        self.assertIn("[REDACTED]", str(error))
        self.assertNotIn(key, str(error))
        self.assertIsNone(error.__context__)


class TemplateTest(unittest.TestCase):
    def test_round_instructions_render_every_placeholder_once(self):
        finding = {"finding_id": "R1-F1", "class_id": "R1-C1", "severity": "P1", "axis": "quality",
                   "title": "keeps {{TASK}} literally\nacross lines", "detail": "a fence ``` and {{HEAD}} inside",
                   "location": "src/app.py:1", "in_scope": True, "fix_kind": "local", "prior_refs": []}
        classes = [{"class_id": "R1-C1", "mechanism": "the {{ATTEMPT}} mechanism", "rule": "the rule"}]
        text = fix.round_instructions(task="t1", attempt_id="A1", receipt="R1", receipt_digest="sha256:" + "a" * 64,
                                      base="b" * 40, reviewed="c" * 40, head="d" * 40, findings=[finding],
                                      classes=classes)
        self.assertTrue(text.startswith("## Correction of R1 (A1)\n"))
        for value in ("task t1", "sha256:" + "a" * 64, "b" * 40 + ".." + "c" * 40, "Worktree HEAD: " + "d" * 40):
            self.assertIn(value, text)
        self.assertIn("- R1-F1 · P1 · class R1-C1 · quality · src/app.py:1 — keeps {{TASK}} literally across lines",
                      text)
        self.assertIn("- R1-C1: the {{ATTEMPT}} mechanism; rule: the rule", text)
        payload = json.loads(text.split("````json\n", 1)[1].split("\n````", 1)[0])
        self.assertEqual(payload, {"findings": [finding], "classes": classes})
        self.assertEqual(text.count("{{TASK}}"), 2, "finding text is never substituted")
        template = (pathlib.Path(fix.__file__).resolve().parent.parent / "templates/review/correction.md").read_text(
            encoding="utf-8")
        placeholders = set(__import__("re").findall(r"\{\{([A-Z_]+)\}\}", template))
        self.assertEqual(placeholders, set(fix.PLACEHOLDERS))
        self.assertIn("Agent-Engine: codex", text)
        self.assertIn("Do not stage or commit", text)

    def test_commit_commands_quote_paths_for_every_shell(self):
        new = ("-dash.py", "src/a b.py", "src/a,b.py", "src/new.py", "src/[id].py", "src/한글.py", "src/it's.py",
               "src/tab\there.py")
        commands, unquoted = fix.commit_commands(task="t1", attempt_id="A2", receipt="R3", finding_ids=["R3-F1", "R3-F2"],
                                                 changes=fix._Changes(("src/app.py",), new), root=pathlib.Path("/worktree"))
        literal = [":(literal)-dash.py", ":(literal)src/a b.py", ":(literal)src/a,b.py", "src/new.py",
                   ":(literal)src/[id].py", ":(literal)src/한글.py"]
        self.assertEqual(commands[0], "git -C '/worktree' add -u")
        self.assertEqual(shlex.split(commands[1]), ["git", "-C", "/worktree", "add", "--", *literal])
        self.assertEqual(commands[2], 'git -C \'/worktree\' commit -m "Correct R3-F1, R3-F2 of R3 in task t1 (oah fix A2)" '
                                      '-m "Agent-Engine: codex"')
        self.assertEqual(unquoted, ["src/it's.py", "src/tab\there.py"])
        for command in commands:
            self.assertFalse(set(command) & set("$`!"), "nothing either shell would expand")
        many, _ = fix.commit_commands(task="t1", attempt_id="A1", receipt="R1",
                                      finding_ids=[f"R1-F{k}" for k in range(1, 6)], changes=fix._Changes((), ("x.py",)),
                                      root=pathlib.Path("/worktree"))
        self.assertEqual(many, ["git -C '/worktree' add -- x.py",
                                'git -C \'/worktree\' commit -m "Correct 5 findings of R1 in task t1 (oah fix A1)" '
                                '-m "Agent-Engine: codex"'])


if __name__ == "__main__":
    unittest.main()
