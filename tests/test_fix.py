# oah fix 검사 — 가짜 리뷰어·수정자 프로세스와 실제 모듈로 수정 시도 기록, 작업자 실행, 끝마다 하나뿐인 결과, 커밋 묶기, 수동 수정, 리뷰 왕복, 비밀 차단을 본다.
from __future__ import annotations

import _thread
import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import secrets as token_source
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from oah import briefs, call, call_cli, intent, runs
from oah.lease import Store
from oah.lockfile import StateLock
from oah.paths import repo_info
from oah.review import ReviewError, authors, fix, receipts, runner
from oah.secrets import NoKeychain
from tests.fixtures.gates.support import remove_tree
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir
from tests.test_review_rounds import CLAUDE_TRAILER, LEVELS, PACKET, RULES, bug, git, toml_list

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_FIX = FAKES / "fake_fix_codex.py"
CODEX_TRAILER = "\n\nAgent-Engine: codex"
HUMAN_TRAILER = "\n\nAgent-Engine: human"


def state_snapshot(root: pathlib.Path) -> dict:
    """Keep exact state bytes without reading Windows byte-range-locked files."""
    return {path.relative_to(root).as_posix():
            (("lock", path.stat().st_size) if path.name.endswith(".lock") else path.read_bytes())
            if path.is_file() else None for path in root.rglob("*")}


class StateSnapshotTest(unittest.TestCase):
    def test_locked_files_use_metadata_and_other_files_keep_exact_bytes(self):
        root = make_tmp_dir(self)
        self.addCleanup(remove_tree, root)
        nested = root / "reviews/t1/.locks"
        nested.mkdir(parents=True)
        shared, task = root / ".lock", nested / "task.lock"
        shared.write_bytes(b"")
        task.write_bytes(b"locked")
        record = root / "reviews/t1/record.json"
        record.write_bytes(b"{\r\n}\x00\xff")
        expected = {".lock": ("lock", 0), "reviews": None, "reviews/t1": None,
                    "reviews/t1/.locks": None, "reviews/t1/.locks/task.lock": ("lock", 6),
                    "reviews/t1/record.json": b"{\r\n}\x00\xff"}
        read_bytes = pathlib.Path.read_bytes

        def read_unlocked(path):
            if path.name.endswith(".lock"):
                raise PermissionError("locked bytes must not be read")
            return read_bytes(path)

        with StateLock(shared), StateLock(task):
            with mock.patch.object(pathlib.Path, "read_bytes", read_unlocked):
                self.assertEqual(state_snapshot(root), expected)
        self.assertEqual(state_snapshot(root), expected)
        record.write_bytes(b"changed")
        expected["reviews/t1/record.json"] = b"changed"
        self.assertEqual(state_snapshot(root), expected)
        task.write_bytes(b"size changed")
        expected["reviews/t1/.locks/task.lock"] = ("lock", 12)
        self.assertEqual(state_snapshot(root), expected)
        shared.unlink()
        del expected[".lock"]
        self.assertEqual(state_snapshot(root), expected)


class FixProject:
    """A task branch with a bound lease, reviewed and corrected by the fake engine processes."""

    def __init__(self, test: unittest.TestCase, *, config: str = "", files: dict | None = None,
                 packet: str = PACKET, newline: str = "\n", attributes: str | None = "* -text\n",
                 repo_name: str = "repo", agent: str = "claude") -> None:
        self.test = test
        self.repo = make_git_repo(test)
        test.addCleanup(lambda: remove_tree(self.repo) if self.repo.exists() else None)
        if repo_name != self.repo.name:
            self.repo = self.repo.rename(self.repo.with_name(repo_name))
        self.work = make_tmp_dir(test)
        test.addCleanup(remove_tree, self.work)
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
        self.lease = Store(self.state).start(task="t1", agent=agent, scope=("src", ".oah/tasks/t1"),
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
            terminal: io.StringIO | None = None, **fields) -> fix.Result:
        fields.setdefault("tier", "high")
        self.patches = []
        patch = fix._patch
        def captured(*args, **kwargs):
            data = patch(*args, **kwargs)
            self.patches.append(data)
            return data
        with contextlib.redirect_stderr(terminal if terminal is not None else io.StringIO()), \
                mock.patch.object(fix, "_patch", side_effect=captured):
            return fix.correct(fix.Request(task="t1", **fields), cwd=cwd or self.repo,
                               env=self.env if env is None else env, keychain=NoKeychain(),
                               rules=RULES if rules is None else rules, temp_root=self.temp_root)

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

    def run_folder(self, result: fix.Result) -> pathlib.Path:
        return runs.find_run(self.state, result.run_id).path

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
        """Run the printed commands in their platform shell, and return the new HEAD."""
        manifests = list((self.state / "runs" / "t1").glob("*/correction.json"))
        if manifests:
            saved = json.loads(max(manifests, key=lambda p: p.stat().st_mtime_ns).read_bytes())
            for name in fix._untracked(self.repo):
                if hashlib.sha256(os.fsencode(name)).hexdigest() in saved.get("new_file_digests", []):
                    git(self.repo, "add", "--", ":(literal)" + name)
        for command in commands:
            argv = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command + "; exit $LASTEXITCODE"]
                    if os.name == "nt" else shlex.split(command))
            subprocess.run(argv, cwd=self.repo, check=True, capture_output=True, env=git_env())
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
    def test_every_owned_signal_rechecks_birth_identity(self):
        for number in (19, 9):
            for observed in ((1, "R", "original"), (1, "R", "reused"), None, OSError()):
                with self.subTest(signal=number, observed=observed):
                    tree = object.__new__(call._PosixTree)
                    tree.owned, tree.error = {123: "original"}, False
                    with mock.patch.object(tree, "_process_info", create=True,
                                           side_effect=observed if isinstance(observed, Exception) else None,
                                           return_value=observed) as read, mock.patch.object(os, "kill") as send:
                        tree._signal({123}, number)
                    self.assertEqual(send.call_args_list,
                                     [mock.call(123, number)] if observed == (1, "R", "original") else [])
                    read.assert_called_once_with(123)
                    self.assertEqual(tree.error, isinstance(observed, Exception) or observed == (1, "R", "reused"))

    def test_missing_ownership_never_falls_back_to_a_pid_or_group(self):
        child = mock.Mock(spec=("pid", "kill"))
        child.pid = 123
        with mock.patch("oah.engines.proc.kill_tree") as unchecked:
            with self.assertRaises(call._CleanupFailed):
                call._kill_owned_tree(child)
        unchecked.assert_not_called()
        child.kill.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX ownership initialization")
    def test_failed_initial_enumeration_keeps_cleanup_on_the_identity_checked_path(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            with mock.patch.object(call._PosixTree, "_processes", side_effect=OSError):
                with self.assertRaises(OSError):
                    call._PosixTree(child)
                with self.assertRaises(call._CleanupFailed):
                    call._kill_owned_tree(child)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    @unittest.skipIf(os.name == "nt", "POSIX PID birth identities")
    def test_cleanup_enumeration_failure_never_signals_a_reused_pid(self):
        for failure in ("enumeration", "watcher"):
            with self.subTest(failure=failure):
                children = []
                try:
                    for _ in range(2):
                        children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                                         start_new_session=True))
                    child, unrelated = children
                    tree = call._PosixTree(child)
                    tree.close()
                    # The live unrelated process has reused an exited descendant's remembered PID.
                    tree.owned[unrelated.pid] = ("exited descendant",)
                    with contextlib.ExitStack() as stack:
                        if failure == "enumeration":
                            stack.enter_context(mock.patch.object(tree, "_processes", side_effect=OSError))
                        else:
                            tree.error = True
                            self.assertNotIn(unrelated.pid, tree._scan(tree._processes()))
                        with self.assertRaises(call._CleanupFailed):
                            tree.kill()
                    with self.assertRaises(subprocess.TimeoutExpired,
                                           msg="fallback killed a PID whose birth identity no longer matches"):
                        unrelated.wait(timeout=0.2)
                    self.assertIsNotNone(child.wait(timeout=5))
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=5)

    def test_landed_orca_launcher_imports_with_prepared_subclass(self):
        result = subprocess.run([sys.executable, "-c", "import tests; from oah.launchers import orca"],
                                capture_output=True, cwd=FAKES.parent.parent)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))

    def test_prepared_keeps_the_landed_default_launch_schema(self):
        for retain in (True, False):
            with self.subTest(retain_worker_text=retain):
                prepared = self.prepare(FixProject(self), retain_worker_text=retain)
                saved = prepared.to_json()
                self.assertEqual("retain_worker_text" in saved, not retain)
                self.assertEqual(call.Prepared.from_json(saved), prepared)

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

    def test_polled_wait_delivers_large_prompt_to_delayed_reader(self):
        for retain in (True, False):
            with self.subTest(retain_worker_text=retain):
                project = FixProject(self)
                project.queue_fix({"stdin_delay": 0.8})
                prepared = self.prepare(project, timeout_s=5, round_instructions="y" * 300_000 + "한글\r\n",
                                        retain_worker_text=retain)
                with contextlib.redirect_stderr(io.StringIO()):
                    run = call.execute(prepared, base_env=runner.reviewer_env(project.env), rules=RULES)
                self.assertEqual((run["status"], run["exit_code"]), ("finished", 0), run.get("error"))
                self.assertEqual(project.fix_calls()[0]["prompt"].encode("utf-8"),
                                 prepared.run.prompt_path.read_bytes())

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
                    self.assertIn("oah review --task <task>", str(error))
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
                self.assertEqual(str(error), fix._error_diagnostic(error)[1])
                project.refused("OAH_INTENT_NOT_CURRENT", project.manual, "human")
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        self.assertEqual(project.fix().code, "OAH_FIX_APPLIED", "a current intent dispatches")

    def test_fix_refuses_without_a_changes_requested_receipt(self):
        project = FixProject(self)
        error = project.refused("OAH_FIX_NOTHING_TO_FIX", project.fix)
        self.assertIn("oah review --task <task>", str(error))
        project.queue({})
        self.assertEqual(project.review().code, "OAH_REVIEW_CONVERGED")
        error = project.refused("OAH_FIX_NOT_REQUESTED", project.fix)
        self.assertIn("converged", str(error))
        project = FixProject(self)
        project.queue({"findings": [bug(fix_kind="design")]})
        self.assertEqual(project.review().code, "OAH_REVIEW_NEEDS_DECISION")
        error = project.refused("OAH_REVIEW_NEEDS_DECISION", project.fix)
        self.assertIn("--decision", str(error))
        project.refused("OAH_FIX_NOT_REQUESTED", project.manual, "human")

    def test_fix_refuses_unfinished_or_unbound_attempts(self):
        project = FixProject(self)
        project.changes_requested()
        publish_start(project)
        for action, args in ((project.fix, ()), (project.manual, ("human",)), (project.bind, ())):
            error = project.refused("OAH_FIX_ATTEMPT_UNFINISHED", action, *args)
            self.assertNotIn("A1", str(error))
            self.assertIn("oah fix --abort --task <task>", str(error))
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        self.assertEqual(project.fix().code, "OAH_FIX_APPLIED")
        git(project.repo, "stash", "push", "-q", "-m", "set aside")  # a clean tree, still waiting for its commit
        for action, args in ((project.fix, ()), (project.manual, ("human",)), (project.abort, ())):
            code = "OAH_FIX_NOTHING_TO_ABORT" if action == project.abort else "OAH_FIX_BIND_REQUIRED"
            error = project.refused(code, action, *args)
            self.assertIn("oah fix --bind --task <task>", str(error))

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
        self.assertNotIn("src/app.py", str(error))
        git(project.repo, "checkout", "-q", "--", "src/app.py")
        git(project.repo, "checkout", "-q", "-b", "elsewhere", project.base)
        project.commit({"src/other.py": "print('other')\n"})
        error = project.refused("OAH_FIX_HEAD_UNRELATED", project.fix)
        self.assertNotIn(project.receipt(1)["head"], str(error))

    def test_fix_refuses_unknown_or_already_corrected_findings(self):
        project = FixProject(self)
        project.changes_requested(bug(1), bug(2), bug(3, severity="P2"))
        for chosen in (["R1-F9"], ["R1-F3"]):
            error = project.refused("OAH_FIX_FINDING_UNKNOWN", project.fix, findings=tuple(chosen))
            self.assertNotIn(chosen[0], str(error))
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
        self.assertNotIn("A1", str(error))
        self.assertNotIn("A2", str(error))
        self.assertIn("oah review --task <task>", str(error))

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
                self.assertIn("wait", str(error))


class WorkerTest(unittest.TestCase):
    @unittest.skipUnless(os.name != "nt", "POSIX directory symlink; Windows junction has its own test")
    def test_nested_metadata_snapshot_does_not_follow_external_directory_links(self):
        self.assert_nested_snapshot_does_not_follow_external_link(junction=False)

    @unittest.skipUnless(os.name == "nt", "native Windows directory junction")
    def test_nested_metadata_snapshot_does_not_follow_external_junctions(self):
        self.assert_nested_snapshot_does_not_follow_external_link(junction=True)

    def assert_nested_snapshot_does_not_follow_external_link(self, *, junction):
        project = FixProject(self)
        nested = project.repo / "src/nested"
        metadata = nested / ".git"
        metadata.mkdir(parents=True)
        external = project.work / "external"
        external.mkdir()
        (external / "user.txt").write_bytes(b"before\r\n")
        link = metadata / "external"
        if junction:
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(external)],
                           check=True, capture_output=True)
            self.addCleanup(link.rmdir)  # Remove the junction before temporary directory cleanup.
        else:
            link.symlink_to(external, target_is_directory=True)
        before = fix._file_snapshot(project.repo, "src/nested")
        (external / "user.txt").write_bytes(b"edited\r\n")
        self.assertEqual(fix._file_snapshot(project.repo, "src/nested"), before,
                         "directory inventories must not read external link targets")

    def test_untracked_nested_repository_metadata_is_user_owned_too(self):
        for alongside in (False, True):
            with self.subTest(alongside=alongside):
                project = FixProject(self)
                project.changes_requested()
                nested = project.repo / "src/nested"
                nested.mkdir()
                subprocess.run(["git", "init", "-q", "--initial-branch=main", str(nested)],
                               check=True, capture_output=True)
                git_commit(nested, {"README.md": b"nested\r\n"}, "nested")
                name = "src/nested/.git/config"
                data = (project.repo / name).read_bytes() + b"\n# changed by corrector\r\n"
                project.queue_fix({"write": {**(FIXED if alongside else {}), name: data.decode("utf-8")}})
                result = project.fix()
                self.assertEqual((result.outcome, result.commands, result.next_command), ("failed", [], None))
                self.assertNotIn(name, json.dumps(result.to_json()))
                self.assertTrue(any("OAH_FIX_USER_FILES_CHANGED" in warning for warning in result.warnings))

    def test_ignored_nested_repository_metadata_is_user_owned_too(self):
        project = FixProject(self, files={".gitignore": "private/\n"})
        project.changes_requested()
        nested = project.repo / "private/nested"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--initial-branch=main", str(nested)],
                       check=True, capture_output=True)
        git_commit(nested, {"README.md": b"nested\n"}, "nested")
        name = "private/nested/.git/user-data"
        (project.repo / name).write_bytes(b"user bytes\r\n")
        project.queue_fix({"write": {name: "other bytes\r\n"}})
        result = project.fix()
        self.assertEqual((result.outcome, result.commands), ("failed", []))
        self.assertNotIn(name, json.dumps(result.to_json()))
        self.assertTrue(any("OAH_FIX_USER_FILES_CHANGED" in warning for warning in result.warnings))

    def test_ignored_fingerprints_detect_same_size_edits_and_mode_changes(self):
        for change in ("bytes", "mode"):
            with self.subTest(change=change):
                if change == "mode" and os.name == "nt":
                    continue  # Windows chmod exposes only the read-only bit, not POSIX permissions.
                project = FixProject(self, files={".gitignore": "src/private/\n"})
                project.changes_requested()
                name = "src/private/user.txt"
                path = project.repo / name
                path.parent.mkdir()
                path.write_bytes(b"before\r\n")
                if os.name != "nt":
                    path.chmod(0o644)
                info = path.stat()
                project.queue_fix({"write": {**FIXED, **({name: "edited\r\n"} if change == "bytes" else {})},
                                   "chmod": {name: 0o600} if change == "mode" else {}})
                execute = call.execute

                def keep_timestamp(*args, **kwargs):
                    result = execute(*args, **kwargs)
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                    return result

                with mock.patch.object(call, "execute", side_effect=keep_timestamp):
                    result = project.fix()
                self.assertEqual(path.stat().st_size, info.st_size)
                self.assertEqual((result.outcome, result.commands), ("failed", []))
                self.assertIn("OAH_FIX_USER_FILES_CHANGED", result.message)
                self.assertFalse(any("OAH_FIX_OUT_OF_SCOPE" in warning for warning in result.warnings))
                patch = (project.run_folder(result) / "correction.patch").read_bytes()
                self.assertNotIn(b"+print('fixed')", patch)
                self.assertIn(b"+print('fixed')", b"".join(project.patches))
                self.assertNotIn(b"before\r\n", patch)
                self.assertNotIn(name.encode("ascii"), patch)

    def test_ignored_user_file_changes_fail_without_retaining_their_names_or_bytes(self):
        for change in ("overwrite", "delete", "create", "unchanged"):
            for secret_side in ("old", "new"):
                with self.subTest(change=change, secret_side=secret_side):
                    project = FixProject(self, files={".gitignore": "private/\n"})
                    project.changes_requested()
                    name = "private/account-" + token_source.token_hex(12) + ".env"
                    path = project.repo / name
                    path.parent.mkdir()
                    key = "s" + "k-" + token_source.token_hex(20)
                    old = (key if secret_side == "old" else "user bytes") + "\r\n"
                    new = (key if secret_side == "new" else "other bytes") + "\r\n"
                    if change != "create":
                        path.write_bytes(old.encode("utf-8"))
                    writes = ({name: None if change == "delete" else new} if change != "unchanged" else {})
                    project.queue_fix({"write": writes})
                    result = project.fix()
                    if change == "unchanged":
                        self.assertEqual(result.outcome, "no-change")
                        continue
                    self.assertEqual((result.outcome, result.commands, result.next_command), ("failed", [], None))
                    self.assertTrue(any("OAH_FIX_OUT_OF_SCOPE" in warning for warning in result.warnings))
                    self.assertTrue(any("OAH_FIX_USER_FILES_CHANGED" in warning for warning in result.warnings))
                    self.assertRegex(project.record("A1-outcome.json")["evidence"], r"^sha256:[0-9a-f]{64}$")
                    patch = (project.run_folder(result) / "correction.patch").read_bytes()
                    self.assertEqual(patch, b"OAH_FIX_FAILED\n")
                    self.assertIn("withheld", result.message)
                    self.assertNotIn(b"user bytes", patch)
                    self.assertNotIn(b"other bytes", patch)
                    SecretTest.assert_private(self, project, [key, name], json.dumps(result.to_json()))

    def test_outcome_and_file_diagnostics_use_positions_for_unrecognized_account_names(self):
        for exit_code in (0, 3):
            with self.subTest(exit_code=exit_code):
                project = FixProject(self)
                project.changes_requested()
                account = "account-" + token_source.token_hex(12)
                name = f"src/{account}.txt"
                project.queue_fix({"write": {name: "correction\n"}, "exit": exit_code})
                result = project.fix()
                self.assertEqual(result.outcome, "applied" if exit_code == 0 else "failed")
                outcome = project.record("A1-outcome.json")
                self.assertNotIn(account, json.dumps(outcome))
                self.assertRegex(outcome["evidence"], r"^sha256:[0-9a-f]{64}$")
                self.assertNotIn(account, result.message)
                for line in result.lines:
                    if line.startswith("changed files:"):
                        self.assertNotIn(account, line)
                        self.assertIn("[file 1]", line)

    def test_all_index_mutations_fail_even_when_worktree_matches_baseline(self):
        for change in ("blob", "addition", "removal", "mode", "conflict", "assume-unchanged", "skip-worktree"):
            with self.subTest(change=change):
                project = FixProject(self)
                project.changes_requested()
                key = "runtime-" + token_source.token_hex(16) + "\ncontinuation-" + token_source.token_hex(16)
                project.env["OPENAI_API_KEY"] = key
                name = "src/app.py" if change != "addition" else "src/added.txt"
                step = {}
                if change in ("blob", "addition"):
                    step = {"write": {name: key}, "stage": [name],
                            "restore": {name: "print('v1')\n" if change == "blob" else None}}
                elif change == "removal":
                    step = {"index_commands": [["--force-remove", name]]}
                elif change == "mode":
                    step = {"index_commands": [["--chmod=+x", name]]}
                elif change in ("assume-unchanged", "skip-worktree"):
                    step = {"index_commands": [["--" + change, name]], "write": FIXED}
                else:
                    oid = git(project.repo, "rev-parse", "HEAD:src/app.py")
                    # A real unmerged index with the same stage-0 blob still has extra entries.
                    execute = call.execute
                    def conflict(*args, **kwargs):
                        result = execute(*args, **kwargs)
                        subprocess.run(["git", "-C", str(project.repo), "update-index", "--index-info"],
                                       input=(f"0 {'0' * 40}\t{name}\n100644 {oid} 1\t{name}\n"
                                              f"100644 {oid} 2\t{name}\n").encode("ascii"),
                                       check=True, capture_output=True)
                        return result
                project.queue_fix(step)
                with (mock.patch.object(call, "execute", side_effect=conflict) if change == "conflict"
                      else contextlib.nullcontext()):
                    result = project.fix()
                self.assertEqual((result.outcome, result.commands), ("failed", []), result.message)
                self.assertIn("OAH_FIX_INDEX_CHANGED", result.message)
                self.assertIn("[file ", json.dumps(result.to_json()))
                self.assertNotIn(name, json.dumps(result.to_json()), "index paths are position-only")
                self.assertIsNone(result.next_command)
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                SecretTest.assert_private(self, project, [key, *key.splitlines()], json.dumps(result.to_json()))

    def test_checkout_cleanup_retries_permission_error_after_enabling_writes(self):
        folder = make_tmp_dir(self) / "checkout"
        folder.mkdir()
        path = folder / "loose-object"
        path.write_bytes(b"git object fixture\n")
        import stat
        path.chmod(stat.S_IREAD)
        def remove(_folder, **kwargs):
            def retry(target):
                self.assertTrue(path.stat().st_mode & stat.S_IWRITE)
                os.unlink(target)
            error = PermissionError("read-only Git object")
            if "onexc" in kwargs:
                kwargs["onexc"](retry, path, error)
            else:
                kwargs["onerror"](retry, path, (PermissionError, error, None))
        with mock.patch.object(shutil, "rmtree", side_effect=remove):
            remove_tree(folder)
        self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == "nt", "Git for Windows read-only loose objects")
    def test_checkout_removal_retries_read_only_git_objects(self):
        project = FixProject(self)
        import stat
        objects = [p for p in (project.repo / ".git" / "objects").glob("??/*") if p.is_file()]
        self.assertTrue(objects)
        for path in objects:
            path.chmod(stat.S_IREAD)
        remove_tree(project.repo)
        self.assertFalse(project.repo.exists())

    def test_fresh_autocrlf_checkout_is_clean_without_running_filters(self):
        project = FixProject(self, attributes=None)
        git(project.repo, "push", "-q", "origin", "feat")
        remove_tree(project.repo)
        subprocess.run(["git", "clone", "-c", "core.autocrlf=true", "-q", "-b", "feat",
                        str(project.work / "remote.git"), str(project.repo)], check=True, capture_output=True)
        project.changes_requested()
        packet = project.repo / ".oah/tasks/t1/PACKET.md"
        self.assertIn(b"\r\n", packet.read_bytes())
        self.assertEqual(fix._tracked_dirty(project.repo), [])
        self.assertEqual(project.fix().outcome, "no-change")
        path = project.repo / "src/app.py"
        info = path.stat()
        path.write_bytes(path.read_bytes().replace(b"v1", b"v2"))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        project.refused("OAH_FIX_DIRTY_TREE", project.fix)

    def test_cached_checkout_stats_accept_conversions_but_refuse_racy_filter_output(self):
        name = "src/converted.txt"
        project = FixProject(self, files={name: b"before\n"})
        project.changes_requested()
        driver, marker = project.work / "filter.py", project.work / "filter-ran"
        driver.write_bytes(b"import pathlib, sys\npathlib.Path(sys.argv[1]).write_bytes(b'ran')\n"
                           b"data = sys.stdin.buffer.read()\n"
                           b"sys.stdout.buffer.write(data.upper() if sys.argv[2] == 'smudge' else data.lower())\n")
        command = " ".join('"' + p.as_posix() + '"' for p in (pathlib.Path(sys.executable), driver, marker))
        git(project.repo, "config", "filter.test.smudge", command + " smudge")
        git(project.repo, "config", "filter.test.clean", command + " clean")
        (project.repo / ".git/info/attributes").write_bytes((name + " filter=test\n").encode("ascii"))
        path = project.repo / name
        path.unlink()
        git(project.repo, "checkout", "--", name)
        self.assertEqual(path.read_bytes(), b"BEFORE\n")
        marker.unlink()
        index_path = project.repo / git(project.repo, "rev-parse", "--git-path", "index")
        timestamp = path.stat().st_mtime_ns
        os.utime(index_path, ns=(timestamp + 2_000_000_000,) * 2)
        self.assertEqual(fix._tracked_dirty(project.repo), [])
        os.utime(index_path, ns=(timestamp - 2_000_000_000,) * 2)
        self.assertEqual(fix._tracked_dirty(project.repo), [name])
        self.assertFalse(marker.exists(), "stat and racy checks must never start a content filter")

    def test_cleanliness_respects_core_filemode_without_ignoring_index_modes(self):
        project = FixProject(self)
        project.changes_requested()
        git(project.repo, "config", "core.filemode", "false")
        path = project.repo / "src/app.py"
        path.chmod(path.stat().st_mode | 0o111)
        self.assertEqual(project.fix().outcome, "no-change")
        git(project.repo, "update-index", "--chmod=+x", "src/app.py")
        project.refused("OAH_FIX_DIRTY_TREE", project.fix)

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
        self.assertIn("OAH_FIX_INDEX_CHANGED", result.message)
        self.assertNotIn(name, json.dumps(result.to_json()))

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
                    self.assertNotIn(name, "\n".join(result.lines))
                    self.assertNotIn(name, project.record("A1-outcome.json")["evidence"])
                    self.assertRegex(project.record("A1-outcome.json")["evidence"], r"^sha256:[0-9a-f]{64}$")
                    patch = b"".join(project.patches)
                    self.assertIn(b"-user's original\r\n", patch)
                    if content is not None:
                        self.assertIn(b"+overwritten\n", patch)
                    self.assertFalse(any(name in command for command in result.commands))
                    self.assertEqual(any("OAH_FIX_OUT_OF_SCOPE" in warning for warning in result.warnings),
                                     name.startswith("outside/"))
                    saved = json.loads((project.run_folder(result) / "correction.json").read_text(encoding="utf-8"))
                    self.assertIn("[file 1]", saved["user_files"])
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
        self.assertNotIn("-o", argv, "the corrector streams its final message without creating a file")
        self.assertIn("--ephemeral", argv, "Codex must not retain its own correction session text")
        env = record["env"]
        self.assertEqual((env["OAH_WORKER"], env["OAH_TASK"], env["OAH_RUN_ID"]),
                         ("1", "t1", result.run_id))
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
        self.assertIn("1 file(s)", scope)
        self.assertIn("[file 3]", scope)
        for value in (*project.lease.scope, "t1", "docs/notes.md", "src/app.py"):
            self.assertNotIn(value, scope)
        changed = next(warning for warning in result.warnings if warning.startswith("OAH_FIX_INTENT_CHANGED"))
        self.assertIn("stale", changed)
        self.assertIn("Restore the bound packet", changed)

    def test_a_corrector_that_commits_fails_the_attempt(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED, "commit": True})
        result = project.fix()
        self.assertEqual((result.code, result.outcome, result.exit_code), ("OAH_FIX_FAILED", "failed", 1))
        self.assertIn("HEAD moved", result.message)
        self.assertIn("git log", result.message)
        self.assertNotIn("git restore", result.message, "committed changes cannot be restored away")
        self.assertRegex(project.record("A1-outcome.json")["evidence"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(result.commands, [])


class OutcomeTest(unittest.TestCase):
    def detached_descendant_cleanup(self, *, interrupt=False):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"spawn_descendant": True, "detached_descendant": True, "hang": interrupt})
        pid_file = project.fix_dir / "descendant.txt"
        heartbeat = pid_file.with_suffix(".heartbeat")
        judge, publish = fix._Attempt._judge, receipts.publish
        checked = []

        def stopped():
            self.assertTrue(heartbeat.exists())
            before = heartbeat.read_bytes()
            time.sleep(0.2)
            self.assertEqual(heartbeat.read_bytes(), before, "detached descendant survived cleanup")

        def inspect(*args, **kwargs):
            stopped()
            checked.append("inspection")
            return judge(*args, **kwargs)

        def record(state, task, name, *args, **kwargs):
            if name.endswith("-outcome.json"):
                stopped()
                checked.append("publication")
            return publish(state, task, name, *args, **kwargs)

        thread = None
        try:
            with default_sigint(), mock.patch.object(fix._Attempt, "_judge", autospec=True, side_effect=inspect), \
                    mock.patch.object(receipts, "publish", side_effect=record):
                if interrupt:
                    thread = interrupt_when(pid_file)
                    with self.assertRaises(fix.FixInterrupted):
                        project.fix(timeout_s=10)
                else:
                    self.assertEqual(project.fix().outcome, "no-change")
            self.assertEqual(checked, ["publication"] if interrupt else ["inspection", "publication"])
            self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted" if interrupt else "no-change")
        finally:
            if thread is not None:
                thread.join(5)
            if os.name != "nt" and pid_file.exists():
                try:
                    os.kill(int(pid_file.read_bytes()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipIf(os.name == "nt", "POSIX detached session cleanup")
    def test_detached_descendant_stops_before_accounting_and_publication(self):
        self.detached_descendant_cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX detached session interruption")
    def test_interruption_stops_detached_descendant_before_publication(self):
        self.detached_descendant_cleanup(interrupt=True)

    @unittest.skipUnless(os.name == "nt", "native Windows process group inside the owned job")
    def test_windows_new_process_group_stops_before_publication(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                self.detached_descendant_cleanup(interrupt=interrupt)

    def test_worktree_change_after_accounting_fails_before_terminal_publication(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        result = fix._Attempt._result

        def change_after_accounting(*args, **kwargs):
            rendered = result(*args, **kwargs)
            (project.repo / "src/app.py").write_bytes(b"late mutation\n")
            return rendered

        with mock.patch.object(fix._Attempt, "_result", autospec=True, side_effect=change_after_accounting):
            with self.assertRaises(ReviewError) as caught:
                project.fix()
        self.assertEqual(caught.exception.code, "OAH_FIX_WORKTREE_CHANGED")
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
        run_id = project.record("A1-start.json")["run_id"]
        saved = json.loads(runs.find_run(project.state, run_id).file_path("correction.json").read_bytes())
        self.assertEqual((saved["outcome"], saved["commands"]), ("failed", []))

    def test_closed_terminal_refuses_launch_and_records_failed(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"hang": True})
        terminal = io.StringIO()
        terminal.close()
        with self.assertRaises(ReviewError) as caught:
            project.fix(terminal=terminal, timeout_s=3)
        self.assertEqual(caught.exception.code, "OAH_FIX_STREAM_FAILED")
        folder = runs.find_run(project.state, project.record("A1-start.json")["run_id"]).path
        run = json.loads((folder / "result.json").read_bytes())
        self.assertEqual(run["status"], "failed", "a closed terminal stops the worker before its timeout")
        self.assertIn("OAH_FIX_STREAM_FAILED", run["error"])
        self.assertFalse((project.fix_dir / "pid-000.txt").exists())
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
        for name in ("stdout.log", "stderr.log", "last-message.md"):
            self.assertFalse((folder / name).exists())

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
                self.assertEqual(outcome["evidence"], receipts.digest(fix._error_diagnostic(
                    KeyboardInterrupt() if interrupt else PermissionError())[1].encode("utf-8")))

    def test_result_diagnostic_failure_is_settled_as_failed(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        with mock.patch.object(fix._Attempt, "_result", side_effect=PermissionError("diagnostic unavailable")):
            with self.assertRaises(ReviewError):
                project.fix()
        self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")

    def test_manifest_write_failure_settles_as_failed_without_commit_guidance(self):
        for transient in (True, False):
            with self.subTest(transient=transient):
                project = FixProject(self)
                project.changes_requested()
                project.queue_fix({"write": FIXED})
                write = fix.write_text_atomic
                failures = []
                terminal = io.StringIO()

                def persist(path, text, **kwargs):
                    if path.name == "correction.json" and (not transient or not failures):
                        failures.append(path)
                        raise PermissionError("manifest persistence unavailable")
                    return write(path, text, **kwargs)

                with mock.patch.object(fix, "write_text_atomic", side_effect=persist):
                    with self.assertRaises(ReviewError) as caught:
                        project.fix(terminal=terminal)
                self.assertEqual(caught.exception.code, "OAH_STATE_IO")
                self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                for text in (str(caught.exception), terminal.getvalue()):
                    self.assertNotIn("git commit", text)
                    self.assertNotIn("oah fix --bind", text)
                    self.assertNotIn("manifest persistence unavailable", text)
                self.assertEqual((project.repo / "src/app.py").read_text(encoding="utf-8"), FIXED["src/app.py"])
                project.refused("OAH_FIX_NOTHING_TO_BIND", project.bind)

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
                          "run_id": result.run_id})
        outcome = project.record("A1-outcome.json")
        self.assertEqual((outcome["outcome"], outcome["after_head"]), ("applied", None))
        self.assertIn(result.attempt_id, ("A1",))
        self.assertEqual([item["kind"] for item in result.records], ["attempt-start", "attempt-outcome"])

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
                evidence_digests = []
                end = fix._Attempt._end
                def ended(attempt, outcome, evidence):
                    evidence_digests.append(receipts.digest(evidence.encode("utf-8")))
                    return end(attempt, outcome, evidence)
                with mock.patch.object(fix._Attempt, "_end", ended):
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
                self.assertEqual([record["evidence"]], evidence_digests)
                [attempt] = receipts.attempts(receipts.load(project.state, "t1"))
                self.assertTrue(attempt.terminal)
                if label == "non-zero exit":
                    self.assertNotIn("src/app.py", result.message)
                    self.assertIn("[file 1]", result.message, "failed-run diagnostics use positions only")
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
        self.assertIn("task records", caught.exception.message)
        self.assertFalse(call._alive(int(pid_file.read_text(encoding="utf-8"))), "Ctrl-C stops the corrector")
        self.assertEqual(self.outcome_names(project), ["A1-start.json", "A1-outcome.json"])
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")
        run = json.loads((project.state / "runs/t1" / project.record("A1-start.json")["run_id"] / "result.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(run["error"], "OAH_RUN_INTERRUPTED")
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
    def test_commit_guidance_names_no_directory_and_runs_from_the_leased_root(self):
        self.assert_guidance_from_any_caller()

    @unittest.skipUnless(os.name == "nt", "native Windows PowerShell 5.1 command quoting")
    def test_powershell_commit_guidance_names_no_directory_and_runs_from_the_leased_root(self):
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
                git(project.repo, "add", "--", "src/new.py")
                for command in result.commands:
                    argv = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             command + "; exit $LASTEXITCODE"] if powershell or os.name == "nt" else
                            ["/bin/sh", "-c", command] if os.name != "nt" else shlex.split(command))
                    done = subprocess.run(argv, cwd=project.repo, env=git_env(), capture_output=True)
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
        text = json.dumps(result.to_json())
        for line in ("-print('v1')", "+print('fixed')", "NEW = 1", "ODD = 1", "ID = 1", "src/app.py"):
            self.assertNotIn(line, text)
        self.assertIn("git diff", text)
        self.assertEqual(result.commands, [
            "git add -u", 'git commit -m "Correct review findings" -m "Agent-Engine: codex"'])
        self.assertEqual(result.next_command, "oah fix --bind --task <task>")
        saved = json.loads((project.run_folder(result) / "correction.json").read_bytes())
        self.assertEqual((saved["changed"], len(saved["new_files"])), (["[file 1]"], 3))
        patch = (project.run_folder(result) / "correction.patch").read_text(encoding="utf-8")
        self.assertNotIn("print(", patch)
        self.assertEqual(patch, "OAH_FIX_APPLIED\n")
        self.assertRegex(project.record("A1-outcome.json")["evidence"], r"^sha256:[0-9a-f]{64}$")
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
        self.assertEqual(bound.next_command, "oah review --task <task>")
        record = project.record("A1-head.json")
        self.assertEqual(record["after_head"], head)
        self.assertEqual(record["engine"], "codex")
        [attempt] = receipts.attempts(receipts.load(project.state, "t1"))
        self.assertEqual((attempt.outcome, attempt.after_head, attempt.problems), ("applied", head, ()))
        project.refused("OAH_FIX_NOTHING_TO_BIND", project.bind)

    def test_bind_refuses_missing_or_invalid_automatic_manifest(self):
        for damage in ("run-folder", "missing", "json", "encoding", "list", "empty", "inventory",
                       "attempt", "before", "outcome", "commands", "digest"):
            with self.subTest(damage=damage):
                project = FixProject(self)
                result = applied(self, project, {"write": {**FIXED, "src/new.py": "NEW = 1\n"}})
                git(project.repo, "add", "-u")
                git(project.repo, "commit", "-q", "-m", "tracked correction" + CODEX_TRAILER)
                folder = project.run_folder(result)
                path = folder / "correction.json"
                saved = json.loads(path.read_bytes())
                if damage == "run-folder":
                    shutil.rmtree(folder)
                elif damage == "missing":
                    path.unlink()
                elif damage in ("json", "encoding"):
                    path.write_bytes(b"{" if damage == "json" else b"\xff")
                else:
                    if damage == "list":
                        saved = []
                    elif damage == "empty":
                        saved = {}
                    else:
                        field, value = {"inventory": ("new_files", "src/new.py"),
                                        "attempt": ("attempt_id", "A2"), "before": ("before_head", "0" * 40),
                                        "outcome": ("outcome", "failed"), "commands": ("commands", [None]),
                                        "digest": ("changes_sha256", "invalid")}[damage]
                        saved[field] = value
                    path.write_bytes(json.dumps(saved).encode("utf-8"))
                error = project.refused("OAH_STATE_CORRUPT" if damage in ("json", "encoding") else
                                        "OAH_FIX_MANIFEST_INVALID", project.bind)
                if damage not in ("json", "encoding"):
                    self.assertIn("rerun oah fix --bind --task <task>", str(error))
                self.assertTrue((project.repo / "src/new.py").is_file())
                [attempt] = receipts.attempts(receipts.load(project.state, "t1"))
                self.assertIsNone(attempt.after_head, "metadata loss must never complete the attempt")

    def test_bind_refuses_until_committed_cleanly_with_trailers(self):
        project = FixProject(self)
        applied(self, project, {"write": {"src/app.py": "print('fixed')\n", "src/new.py": "NEW = 1\n"}})
        error = project.refused("OAH_FIX_NOT_COMMITTED", project.bind)
        self.assertIn("Agent-Engine: codex", str(error))
        self.assertNotIn("git commit", str(error), "exceptions carry fixed guidance only")
        git(project.repo, "add", "-u")
        git(project.repo, "commit", "-q", "-m", "the tracked part" + CLAUDE_TRAILER)
        error = project.refused("OAH_FIX_DIRTY_TREE", project.bind)
        self.assertNotIn("src/new.py", str(error))
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
        self.assertEqual(project.record("A1-head.json")["engine"], "claude")
        events = [json.loads(line) for line in (project.state / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(any("--engine claude" in event.get("message", "") for event in events),
                         "the immutable binding records the override without another state transaction")
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
        self.assertIn("history", str(error))
        project = FixProject(self)
        project.changes_requested()
        receipts.publish(project.state, "t1", "A1-head.json", {
            "schema": 1, "kind": "attempt-head", "task": "t1", "attempt_id": "A1", "after_head": project.head,
            "evidence": "written by hand", "bound_at": "2026-10-24T00:02:00Z"})
        error = project.refused("OAH_FIX_HISTORY_INVALID", project.abort)
        self.assertNotIn("A1", str(error))


class ManualTest(unittest.TestCase):
    def test_rewritten_bound_correction_blocks_fix_and_manual_until_decision(self):
        for operation in ("fix", "manual"):
            with self.subTest(operation=operation):
                project = FixProject(self)
                project.changes_requested(bug(1), bug(2))
                project.queue_fix({"write": FIXED})
                first = project.fix(findings=("R1-F1",))
                self.assertEqual(first.code, "OAH_FIX_APPLIED", first.message)
                original = project.run_commands(first.commands)
                self.assertEqual(project.bind().code, "OAH_FIX_BOUND")
                git(project.repo, "commit", "-q", "--amend", "-m", "rewritten correction" + HUMAN_TRAILER)
                project.head = git(project.repo, "rev-parse", "HEAD")
                self.assertNotEqual(project.head, original)
                action = project.fix if operation == "fix" else lambda: project.manual("human")
                # Check correction before review logs a stop: ancestry alone must refuse it.
                code = "OAH_REVIEW_NEEDS_DECISION" if operation == "fix" else "OAH_FIX_NOT_REQUESTED"
                project.refused(code, action)
                project.refused("OAH_REVIEW_NEEDS_DECISION", project.review)
                project.refused(code, action)
                # A decision attached to this receipt settles the same gate, even if the
                # subsequent reviewer fails without publishing another receipt.
                project.queue({"exit": 3})
                self.assertEqual(project.review(decision="the rewritten commit keeps the correction").code,
                                 "OAH_REVIEW_ENGINE_FAILED")
                result = action()
                self.assertEqual(result.code, "OAH_FIX_NO_CHANGE" if operation == "fix" else "OAH_FIX_RECORDED",
                                 result.message)

    def test_process_filter_installed_before_preflight_is_never_started(self):
        project = FixProject(self)
        project.changes_requested()
        marker = project.work / "process-filter-ran"
        driver = project.work / "process-filter.py"
        driver.write_bytes(b"import pathlib, sys\npathlib.Path(sys.argv[1]).write_bytes(b'ran')\n")
        command = " ".join('"' + p.as_posix() + '"' for p in (pathlib.Path(sys.executable), driver, marker))
        git(project.repo, "config", "filter.trap.process", command)
        git(project.repo, "config", "filter.trap.required", "true")
        (project.repo / ".git/info/attributes").write_bytes(b"src/app.py filter=trap\n")
        path = project.repo / "src/app.py"
        path.write_bytes(b"print('dirty')\n")
        project.refused("OAH_FIX_DIRTY_TREE", project.fix)
        self.assertFalse(marker.exists(), "even dirty-tree refusal must be conversion-free")

    def test_uncounted_task_decision_stop_blocks_fix_and_manual_until_settled(self):
        project = FixProject(self)
        project.changes_requested()
        other_base = project.head
        project.commit({"src/other.py": "pass\n"})
        project.queue({"findings": [bug(prior_refs=[{"round": 1, "receipt_digest": "sha256:" + "0" * 64,
                                                    "finding_id": "R1-F1", "class_id": "R1-C1"}])]})
        stopped = project.review(decision="inspect another review", base=other_base)
        self.assertEqual(stopped.code, "OAH_REVIEW_NEEDS_DECISION", stopped.message)
        self.assertEqual(project.names(), ["R1.json"], "the stop did not count a receipt")
        project.refused("OAH_REVIEW_NEEDS_DECISION", project.fix)
        project.commit(FIXED, trailer=HUMAN_TRAILER)
        project.refused("OAH_FIX_NOT_REQUESTED", project.manual, "human")
        project.queue({"exit": 3})
        self.assertEqual(project.review(decision="correct R1 then retry").code, "OAH_REVIEW_ENGINE_FAILED")
        self.assertEqual(project.manual("human").code, "OAH_FIX_RECORDED")

    def test_filters_never_run_in_fix_preflight_bind_or_manual(self):
        for operation in ("fix", "bind", "manual"):
            with self.subTest(operation=operation):
                project = FixProject(self)
                project.changes_requested()
                if operation == "bind":
                    project.queue_fix({"write": FIXED})
                    project.run_commands(project.fix().commands)
                elif operation == "manual":
                    project.commit(FIXED, trailer=HUMAN_TRAILER)
                marker = project.work / "filter-ran"
                driver = project.work / "filter.py"
                driver.write_bytes(b"import pathlib, sys\npathlib.Path(sys.argv[1]).write_bytes(b'ran')\n"
                                   b"sys.stdout.buffer.write(sys.stdin.buffer.read())\n")
                command = " ".join('"' + p.as_posix() + '"' for p in (pathlib.Path(sys.executable), driver, marker))
                git(project.repo, "config", "filter.trap.clean", command)
                (project.repo / ".git/info/attributes").write_bytes(b"src/app.py filter=trap\n")
                path = project.repo / "src/app.py"
                path.write_bytes(path.read_bytes())
                os.utime(path, ns=(1_000_000_000, 1_000_000_000))
                if operation == "fix":
                    self.assertEqual(project.fix().outcome, "no-change")
                elif operation == "bind":
                    self.assertEqual(project.bind().code, "OAH_FIX_BOUND")
                else:
                    self.assertEqual(project.manual("human").code, "OAH_FIX_RECORDED")
                self.assertFalse(marker.exists(), "correction cleanliness must never run Git filters")

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
        project.queue({"findings": [bug(1, class_id="R1-C1", title="the same defect, worded differently", prior_refs=[{
            "round": 1, "receipt_digest": project.receipt_digest(1), "finding_id": "R1-F1", "class_id": "R1-C1"}])]})
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
        self.assertEqual(bound["engine"], "claude", "only the commits after the bound result are attributed")

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
        self.assertEqual(outcome["evidence"], receipts.digest(
            b"recorded by oah fix --abort; interrupted attempt; its run was last unknown"))
        project.refused("OAH_FIX_NOTHING_TO_ABORT", project.abort)
        project.queue_fix({"write": FIXED})
        self.assertEqual(project.fix().code, "OAH_FIX_APPLIED", "an aborted attempt is retried")
        self.assertEqual(project.record("A2-start.json")["finding_ids"], ["R1-F1"])

    def test_abort_refuses_inconsistent_run_references_before_publication(self):
        for mismatch in ("directory", "engine", "stage", "run_id", "task", "task_digest", "start_engine"):
            with self.subTest(mismatch=mismatch):
                project = FixProject(self)
                project.changes_requested()
                run = runs.new_run(project.state, "t2" if mismatch == "directory" else "t1", "codex", "correction")
                run.result(status="running")
                data = run.read()
                if mismatch == "engine":
                    data["engine"] = "claude"
                elif mismatch == "stage":
                    data["stage"] = "review"
                elif mismatch == "run_id":
                    data["run_id"] = run.id[:-8] + ("00000000" if run.id[-8:] != "00000000" else "11111111")
                elif mismatch == "task":
                    data["task"] = "t2"
                elif mismatch == "task_digest":
                    data["task"] = "sha256:" + hashlib.sha256(b"t2").hexdigest()
                run.result_path.write_bytes(receipts.canonical(data))
                # A preceding valid attempt must not be partially settled on refusal either.
                publish_start(project)
                start = dict(project.record("A1-start.json"), attempt_id="A2", run_id=run.id)
                if mismatch == "start_engine":
                    start["engine"] = "claude"
                receipts.publish(project.state, "t1", "A2-start.json", start)
                before = {p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                          for p in project.state.rglob("*")}
                project.refused("OAH_FIX_HISTORY_INVALID", project.abort)
                self.assertEqual({p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                                  for p in project.state.rglob("*")}, before)
                self.assertEqual(run.read()["status"], "running")
                self.assertEqual(run.result(status="finished")["status"], "finished")

    def test_abort_refuses_all_history_problems_without_changing_state(self):
        for problem in ("index", "source_digest", "findings", "classes", "completed_attempt"):
            with self.subTest(problem=problem):
                project = FixProject(self)
                project.changes_requested()
                run = runs.new_run(project.state, "t1", "codex", "correction")
                run.result(status="running")
                publish_start(project)
                start = dict(project.record("A1-start.json"), attempt_id="A2", run_id=run.id)
                if problem == "source_digest":
                    start["source_receipt_digest"] = receipts.digest(b"unmatched source receipt")
                elif problem == "findings":
                    start["finding_ids"] = ["R1-F2"]
                elif problem == "classes":
                    start["class_ids"] = ["R1-C2"]
                receipts.publish(project.state, "t1", "A2-start.json", start)
                if problem == "index":
                    with (project.folder() / "index.jsonl").open("ab") as handle:
                        handle.write(b"{}\n")
                elif problem == "completed_attempt":
                    receipts.publish(project.state, "t1", "A3-outcome.json", {
                        "schema": 1, "kind": "attempt-outcome", "task": "t1", "attempt_id": "A3",
                        "outcome": "failed", "after_head": None, "evidence": "missing start",
                        "finished_at": "2026-10-24T00:01:00Z"})
                records = receipts.load(project.state, "t1")
                self.assertTrue(records.problems or any(
                    any(not p.startswith(f"{a.attempt_id} is unfinished:") for p in a.problems)
                    for a in receipts.attempts(records)))
                before = {p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                          for p in project.state.rglob("*")}
                project.refused("OAH_FIX_HISTORY_INVALID", project.abort)
                self.assertEqual({p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                                  for p in project.state.rglob("*")}, before)
                self.assertEqual(run.read()["status"], "running")

    def test_abort_accepts_owned_correction_run_identities(self):
        for hashed in (False, True):
            with self.subTest(hashed=hashed):
                project = FixProject(self)
                project.changes_requested()
                run = runs.new_run(project.state, "t1", "codex", "correction")
                run.result(status="running")
                if hashed:
                    data = run.read()
                    data["task"] = "sha256:" + hashlib.sha256(b"t1").hexdigest()
                    run.result_path.write_bytes(receipts.canonical(data))
                start = {"schema": 1, "kind": "attempt-start", "task": "t1", "attempt_id": "A1",
                         "source_receipt": "R1.json", "source_receipt_digest": project.receipt_digest(1),
                         "finding_ids": ["R1-F1"], "class_ids": ["R1-C1"], "before_head": project.head,
                         "engine": "codex", "manual": False, "started_at": "2026-10-25T00:00:00Z",
                         "run_id": run.id}
                receipts.publish(project.state, "t1", "A1-start.json", start)
                self.assertEqual(project.abort().code, "OAH_FIX_ABORTED")
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "aborted")
                self.assertEqual(run.read()["status"], "lost")

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
            self.assertIn(item["digest"], index.values())
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
        project.queue({"findings": [bug(1, class_id="R1-C1", title="the defect came back", prior_refs=[{
            "round": 1, "receipt_digest": project.receipt_digest(1), "finding_id": "R1-F1", "class_id": "R1-C1"}])]})
        result = project.review()
        self.assertEqual(result.code, "OAH_REVIEW_NEEDS_DECISION", result.message)
        self.assertIn("A1 (applied)", project.receipt(2)["decision_stop_reason"])
        project.refused("OAH_REVIEW_NEEDS_DECISION", project.fix)

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
    def test_no_pool_status_survives_correction_record_writes(self):
        status = "unused(no-pool-for-tier)"
        closed = runs._correction_record({"model_judge": status}, runs.RESULT_FIELDS)
        run = runs.new_run(make_tmp_dir(self), "t1", "codex", "correction")
        run.result(**closed)
        run.result(**closed)
        self.assertEqual(run.read()["model_judge"], status)
        self.assertEqual(runs._correction_record(closed, runs.RESULT_FIELDS), closed)

    def test_model_choice_schema_hashes_individual_untrusted_values(self):
        key = "fake-" + token_source.token_hex(20)
        digest = "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        pair = {"model": key, "effort": "high"}
        choice = {"deterministic": dict(pair, source="cli_default"),
                  "floor": dict(pair, tier="high"), "mode": "pools", "note": key,
                  "candidates": [[key, "high"]], "excluded": {key: "above-cap"},
                  "proposal": pair, "provider": key, "requested": pair}
        fields = {"model_choice": choice, "model_judge": "unused(error:timeout)",
                  "header_code": "OAH_MODEL_MISMATCH", "error": "OAH_MODEL_MISMATCH: " + key}
        closed = runs._correction_record(fields, runs.RESULT_FIELDS)
        run = runs.new_run(make_tmp_dir(self), "t1", "codex", "correction")
        run.result(**closed)
        saved = json.loads(run.result_path.read_bytes())
        self.assertEqual(saved["model_choice"], {
            "deterministic": {"model": digest, "effort": "high", "source": "cli_default"},
            "floor": {"model": digest, "effort": "high", "tier": "high"}, "mode": "pools", "note": digest,
            "candidates": [[digest, "high"]], "excluded": {digest: "above-cap"},
            "proposal": {"model": digest, "effort": "high"}, "provider": digest,
            "requested": {"model": digest, "effort": "high"}})
        self.assertEqual(saved["model_judge"], "unused(error:timeout)")
        self.assertEqual((saved["header_code"], saved["error"]), ("OAH_MODEL_MISMATCH", "OAH_MODEL_MISMATCH"))
        self.assertNotIn(key.encode("utf-8"), run.result_path.read_bytes())
        self.assertEqual(runs._correction_record(closed, runs.RESULT_FIELDS), closed)
        for status in ("unused(error:" + key + ")", "rejected(" + key + ")"):
            safe = runs._correction_record({"model_judge": status}, runs.RESULT_FIELDS)
            self.assertNotIn(key, json.dumps(safe))
        # Unknown structural keys remain opaque; a provider cannot extend the retained schema.
        unsafe = runs._correction_record({"model_choice": {key: pair}}, runs.RESULT_FIELDS)
        self.assertIsInstance(unsafe["model_choice"], str)
        self.assertNotIn(key, json.dumps(unsafe))

    def test_start_publication_failure_preserves_index_and_settles_attempt(self):
        for point in ("before-run", "prompt"):
            with self.subTest(point=point):
                project = FixProject(self)
                project.changes_requested()
                target, name = ((call._CorrectionInputs, "publish_run") if point == "before-run" else
                                (call._CorrectionRun, "write_prompt"))
                with mock.patch.object(target, name, side_effect=OSError("untrusted-publication-text")):
                    with self.assertRaises(ReviewError) as caught:
                        project.fix(tier="medium")  # Force planned model-resolution writes after the high-tier review.
                self.assertNotIn("untrusted-publication-text", str(caught.exception))
                self.assertEqual(project.names(), ["R1.json", "A1-start.json", "A1-outcome.json"])
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                records = receipts.load(project.state, "t1")
                self.assertEqual(records.problems, ())
                self.assertEqual(receipts.attempts(records)[0].problems, ())

    def test_git_fingerprint_inspection_does_not_hold_shared_state_lock(self):
        project = FixProject(self)
        project.changes_requested()
        fingerprint = fix._worktree_fingerprint
        observed = []

        def inspected(*args, **kwargs):
            code = ("import sys; from oah.lockfile import StateLock\n"
                    "with StateLock(sys.argv[1], timeout=0): pass\n")
            done = subprocess.run([sys.executable, "-c", code, str(project.state / ".lock")],
                                  capture_output=True, timeout=20,
                                  env=dict(project.env, PYTHONPATH=str(FAKES.parent.parent / "src")))
            observed.append(done.returncode)
            return fingerprint(*args, **kwargs)

        with mock.patch.object(fix, "_worktree_fingerprint", side_effect=inspected):
            self.assertEqual(project.fix().outcome, "no-change")
        self.assertGreaterEqual(len(observed), 2)
        self.assertEqual(observed, [0] * len(observed))

    def test_dispatch_dependency_error_codes_still_publish_failed_outcome(self):
        for code in ("OAH_LEASE_CHANGED", "OAH_FIX_STATE_CHANGED"):
            with self.subTest(code=code):
                project = FixProject(self)
                project.changes_requested()
                with mock.patch.object(call, "execute", side_effect=call.EngineError(code, "untrusted-dispatch-text")):
                    with self.assertRaises(ReviewError) as caught:
                        project.fix()
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("untrusted-dispatch-text", str(caught.exception))
                self.assertEqual(project.names(), ["R1.json", "A1-start.json", "A1-outcome.json"])
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                run_id = project.record("A1-start.json")["run_id"]
                self.assertEqual(runs.find_run(project.state, run_id).read()["status"], "failed")

    def test_terminal_lease_conflicts_leave_all_state_bytes_unchanged(self):
        for change in ("renew", "release", "same-generation", "expired"):
            with self.subTest(change=change), contextlib.ExitStack() as stack:
                project = FixProject(self)
                project.changes_requested()
                judge = fix._Attempt._judge
                expected = []

                def changed(attempt, *args, **kwargs):
                    result = judge(attempt, *args, **kwargs)
                    store = Store(project.state, lock_timeout=0)
                    if change == "renew":
                        store.renew(task="t1", agent="claude", ttl_hours=2)
                    elif change == "release":
                        store.release(task="t1", agent="claude", reason="test", force=False)
                    elif change == "same-generation":
                        with StateLock(project.state / ".lock", timeout=0):
                            path = project.state / "leases.json"
                            data = json.loads(path.read_bytes())
                            data["leases"][0]["note"] = "changed between publications"
                            path.write_bytes((json.dumps(data) + "\n").encode("utf-8"))
                    else:
                        later = call.dt.datetime.now(call.dt.timezone.utc) + call.dt.timedelta(hours=2)
                        stack.enter_context(mock.patch("oah.lease.utc_now", return_value=later))
                    expected.append(state_snapshot(project.state))
                    return result

                with mock.patch.object(fix._Attempt, "_judge", changed):
                    with self.assertRaises(ReviewError) as caught:
                        project.fix()
                self.assertEqual(caught.exception.code, "OAH_LEASE_CHANGED")
                self.assertEqual(state_snapshot(project.state), expected[0])
                self.assertEqual(project.names(), ["R1.json", "A1-start.json"])

    def test_landed_lease_changes_between_steps_keep_the_named_refusal(self):
        for change in ("renew", "release"):
            with self.subTest(change=change):
                project = FixProject(self)
                project.changes_requested()
                publication = fix._publication
                expected = []
                @contextlib.contextmanager
                def changed(t, h):
                    store = Store(project.state, lock_timeout=0)
                    if change == "renew":
                        store.renew(task="t1", agent="claude", ttl_hours=2)
                    else:
                        store.release(task="t1", agent="claude", reason="test", force=False)
                    expected.append(state_snapshot(project.state))
                    with publication(t, h):
                        yield
                with mock.patch.object(fix, "_publication", changed):
                    project.refused("OAH_LEASE_CHANGED", project.fix)
                self.assertEqual(state_snapshot(project.state), expected[0])

    def test_publication_waits_for_brief_shared_lock_contention(self):
        project = FixProject(self)
        project.changes_requested()
        ready = project.work / "lock-ready"
        code = ("import pathlib, sys, time; from oah.lockfile import StateLock\n"
                "with StateLock(pathlib.Path(sys.argv[1])):\n"
                " pathlib.Path(sys.argv[2]).write_bytes(b'ready')\n time.sleep(0.3)\n")
        process = subprocess.Popen([sys.executable, "-c", code, str(project.state / ".lock"), str(ready)],
                                   env=dict(project.env, PYTHONPATH=str(FAKES.parent.parent / "src")))
        try:
            self.assertTrue(wait_for(ready))
            with fix._ExistingLock(project.state / ".lock"):
                self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def test_each_mode_refuses_changed_round_inputs_before_any_publication(self):
        for mode in ("fix", "manual", "bind", "abort"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                project.changes_requested()
                if mode == "manual":
                    project.commit(FIXED, trailer=HUMAN_TRAILER)
                elif mode == "bind":
                    project.queue_fix({"write": FIXED})
                    project.run_commands(project.fix().commands)
                elif mode == "abort":
                    publish_start(project)
                original = fix._publication
                expected = []
                @contextlib.contextmanager
                def changed(t, h):
                    path = project.folder() / "index.jsonl"
                    with StateLock(project.state / ".lock", timeout=0):
                        path.write_bytes(path.read_bytes() + b'{"concurrent": true}\n')
                    expected.append(state_snapshot(project.state))
                    with original(t, h):
                        yield
                action = (lambda: project.manual("human")) if mode == "manual" else getattr(project, mode)
                with mock.patch.object(fix, "_publication", changed):
                    project.refused("OAH_FIX_STATE_CHANGED", action)
                self.assertEqual(state_snapshot(project.state), expected[0])

    def test_terminal_publication_refuses_changed_inputs_without_any_write(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        judge = fix._Attempt._judge
        expected = []
        def changed(attempt, *args, **kwargs):
            result = judge(attempt, *args, **kwargs)
            path = project.folder() / "rounds.jsonl"
            with StateLock(project.state / ".lock", timeout=0):
                path.write_bytes(path.read_bytes() + b'{"concurrent": true}\n')
            expected.append(state_snapshot(project.state))
            return result
        with mock.patch.object(fix._Attempt, "_judge", changed):
            with self.assertRaises(ReviewError) as caught:
                project.fix()
        self.assertEqual(caught.exception.code, "OAH_FIX_STATE_CHANGED")
        self.assertEqual(state_snapshot(project.state), expected[0])

    def test_model_change_after_capture_refuses_without_overwriting_state(self):
        project = FixProject(self)
        project.changes_requested()
        resolve = call.resolve_engine
        expected = []

        def changed(*args, **kwargs):
            result = resolve(*args, **kwargs)
            with StateLock(project.state / ".lock", timeout=0):
                path = project.state / "models/resolved.json"
                path.write_bytes(b'{"other": {}}\n')
            expected.append(state_snapshot(project.state))
            return result

        with mock.patch.object(call, "resolve_engine", side_effect=changed):
            project.refused("OAH_FIX_STATE_CHANGED", project.fix)
        self.assertEqual(state_snapshot(project.state), expected[0])

    def test_live_output_shows_only_scanned_dispatcher_diff(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED, "stdout": "WORKER-ONLY-TEXT\n", "stderr": "WORKER-ERROR\n",
                           "last_message": "WORKER-FINAL\n"})
        terminal = io.StringIO()
        result = project.fix(terminal=terminal)
        self.assertEqual(result.outcome, "applied")
        for text in ("t1", "src/app.py", "-print('v1')", "+print('fixed')"):
            self.assertIn(text, terminal.getvalue())
            self.assertNotIn(text, json.dumps(result.to_json()))
        for text in ("WORKER-ONLY-TEXT", "WORKER-ERROR", "WORKER-FINAL"):
            self.assertNotIn(text, terminal.getvalue())

    def test_automatic_tier_reasons_never_retain_scope_paths(self):
        path = "src/auth/account-unrecognized-file.py"
        project = FixProject(self, packet=PACKET.replace("- src\n", f"- {path}\n"))
        project.changes_requested()
        result = project.fix(tier=None)
        data = json.loads((project.run_folder(result) / "result.json").read_bytes())
        self.assertEqual((data["tier_source"], data["axes"]["risk"]), ("rules", "high"))
        self.assertTrue(data["tier_reasons"])
        self.assertNotIn(path, json.dumps(data))
        self.assertEqual(set(data["tier_reasons"]), {"OAH_FIX_ROUTING_RULE"})

    def test_captured_model_changes_require_complete_object_lines(self):
        for raw in (b'{}', b'[]\n', b'\n', b'{}\n\n', b'{}\r', b'\xff\n'):
            with self.subTest(raw=raw):
                project = FixProject(self)
                project.changes_requested()
                (project.state / "models/changes.jsonl").write_bytes(raw)
                # A different tier forces resolution changes and hence a planned append.
                before = {p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                          for p in project.state.rglob("*")}
                project.refused("OAH_STATE_CORRUPT", project.fix, tier="max")
                self.assertEqual({p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                                  for p in project.state.rglob("*")}, before)

    def test_abort_validates_closed_run_fields_before_publishing(self):
        project = FixProject(self)
        project.changes_requested()
        run = runs.new_run(project.state, "t1", "codex", "correction")
        publish_start(project)
        path = project.folder() / "A1-start.json"
        start = project.record(path.name)
        start["run_id"] = run.id
        path.write_bytes(receipts.canonical(start))
        index = project.folder() / "index.jsonl"
        lines = [json.loads(line) for line in index.read_bytes().splitlines()]
        lines[-1]["digest"] = receipts.digest(path.read_bytes())
        index.write_bytes(b"".join((json.dumps(line) + "\n").encode() for line in lines))
        data = run.read()
        data["unrecognized-field"] = "account-unrecognized-value"
        run.result_path.write_bytes(json.dumps(data).encode())
        before = {p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                  for p in project.state.rglob("*")}
        project.refused("OAH_STATE_CORRUPT", project.abort)
        self.assertEqual({p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                          for p in project.state.rglob("*")}, before)

    def test_abort_scans_every_captured_index_object_before_publication(self):
        project = FixProject(self)
        project.changes_requested()
        publish_start(project)
        marker = "s" + "k-" + token_source.token_hex(20)
        encoded = json.dumps({"kind": "unrecognized", "note": marker}).replace(
            marker, "".join(f"\\u{ord(c):04x}" for c in marker))
        with (project.folder() / "index.jsonl").open("ab") as stream:
            stream.write((encoded + "\n").encode())
        before = {p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                  for p in project.state.rglob("*")}
        error = project.refused("OAH_FIX_SENSITIVE_INPUT", project.abort)
        self.assertNotIn(marker, str(error))
        self.assertEqual({p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                          for p in project.state.rglob("*")}, before)

    def test_captured_state_uses_landed_validation_before_any_publication(self):
        for mode in ("fix", "manual", "bind", "abort"):
            for target in ("index", "record", "catalog", "resolved"):
                if mode != "fix" and target in ("catalog", "resolved"):
                    continue  # Those modes do not consume model inputs.
                with self.subTest(mode=mode, target=target):
                    project = FixProject(self)
                    project.changes_requested()
                    if mode == "manual":
                        project.commit(FIXED, trailer=HUMAN_TRAILER)
                    elif mode == "bind":
                        project.queue_fix({"write": FIXED})
                        project.run_commands(project.fix().commands)
                    elif mode == "abort":
                        publish_start(project)
                    if target == "index":
                        path = project.folder() / "index.jsonl"
                        path.write_bytes(path.read_bytes().rstrip(b"\n"))
                    elif target == "record":
                        path = project.folder() / "R1.json"
                        path.write_bytes(b'{')
                        index = project.folder() / "index.jsonl"
                        lines = [json.loads(line) for line in index.read_bytes().splitlines()]
                        lines[0]["digest"] = receipts.digest(path.read_bytes())
                        index.write_bytes(b"".join((json.dumps(line) + "\n").encode() for line in lines))
                    else:
                        (project.state / f"models/{target}.json").write_bytes(b"")
                    before = {p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                              for p in project.state.rglob("*")}
                    action = (lambda: project.manual("human")) if mode == "manual" else getattr(project, mode)
                    project.refused("OAH_STATE_CORRUPT", action)
                    self.assertEqual({p.relative_to(project.state): p.read_bytes() if p.is_file() else None
                                      for p in project.state.rglob("*")}, before)

    def test_every_correction_record_has_only_closed_schema_strings(self):
        enums = {"codex", "claude", "human", "correction", "attempt-start", "attempt-outcome", "attempt-head",
                 "created", "prepared", "queued", "running", "finished", "failed", "timed_out", "refused", "lost",
                 "applied", "no-change", "aborted", "unverified", "match", "mismatch", "downgraded",
                 "low", "medium", "high", "xhigh", "max", "highest", "normal", "critical", "flag", "packet", "rules",
                 "implementation", "pin", "quality_order", "quality_order(unconfirmed)", "cli_default", "effective_config",
                 "judge", "used", "unused(pinned)", "unused(floor-unresolved)", "unused(disabled-by-brief)",
                 "unused(not-enabled)", "unused(not-configured)", "unused(unknown-provider)", "unused(no key)",
                 "unused(no-candidates)", "unused(no-pool-for-tier)", "unused(one-candidate)", "unused(too-many-candidates)",
                 "ranked", "pools", "deterministic", "unused(error:timeout)",
                 "outside-candidates", "not-allowed", "unsupported-effort", "unranked-model",
                 "not-in-tier-pool", "below-floor", "above-cap",
                 *(f"rejected({rule})" for rule in ("outside-candidates", "not-allowed", "unsupported-effort",
                                                     "unranked-model", "not-in-tier-pool", "below-floor",
                                                     "above-cap"))}
        codes = {"OAH_FIX_ROUTING_RULE", "OAH_FIX_ADVISORY_UNUSED", "OAH_FIX_ADVISORY_USED",
                 "OAH_FIX_WARNING", "OAH_FIX_NORMALIZATION", "OAH_FIX_COMMIT_COMMAND",
                 "OAH_MODEL_MISMATCH", "OAH_EFFORT_CAPPED",
                 *fix._ERROR_DIAGNOSTICS}
        forms = re.compile(r"(?:sha256:)?[0-9a-f]{64}|[0-9a-f]{40}|r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}|"
                           r"A[1-9][0-9]*|R[1-9][0-9]*(?:-[FC][1-9][0-9]*)?|R[1-9][0-9]*\.json|"
                           r"A[1-9][0-9]*-(?:start|outcome|head)\.json|\[file [1-9][0-9]*\]|"
                           r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z")

        def check(value):
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
            elif isinstance(value, str):
                self.assertTrue(value in enums or value in codes or forms.fullmatch(value), repr(value))
            else:
                self.assertIsInstance(value, (int, float, bool, type(None)))

        for scenario in ("applied", "no-change", "failed", "timeout", "interrupted", "bind", "manual", "abort"):
            with self.subTest(scenario=scenario):
                project = FixProject(self, packet=PACKET.replace("- src\n", "- src/auth/account-unrecognized-file.py\n"),
                                     files={".claude/skills/systematic-debugging/SKILL.md":
                                            "---\nname: systematic-debugging\ndescription: trace bugs\n---\nTrace the defect.\n"})
                project.changes_requested()
                before = {p: p.read_bytes() for p in project.state.rglob("*") if p.is_file()}
                if scenario == "manual":
                    project.commit(FIXED, trailer=HUMAN_TRAILER)
                    project.manual("human")
                elif scenario == "abort":
                    publish_start(project)
                    before[project.folder() / "A1-start.json"] = (project.folder() / "A1-start.json").read_bytes()
                    project.abort()
                elif scenario == "interrupted":
                    project.queue_fix({"hang": True})
                    with default_sigint():
                        thread = interrupt_when(project.fix_dir / "pid-000.txt")
                        with self.assertRaises(fix.FixInterrupted):
                            project.fix(tier=None, timeout_s=120)
                        thread.join(5)
                else:
                    project.queue_fix({"write": FIXED} if scenario in ("applied", "bind") else
                                      {"exit": 3} if scenario == "failed" else
                                      {"hang": True} if scenario == "timeout" else {})
                    result = project.fix(tier=None, **({"timeout_s": 1} if scenario == "timeout" else {}))
                    if scenario == "bind":
                        project.run_commands(result.commands)
                        project.bind()
                artifacts = list(project.folder().glob("A*.json"))
                for name in ("result.json", "correction.json", "skills.json", "lease.json"):
                    artifacts.extend((project.state / "runs/t1").glob("*/" + name))
                for path in artifacts:
                    if path.read_bytes() != before.get(path):
                        check(json.loads(path.read_bytes()))
                for path in (project.state / "runs/t1").glob("*/correction.patch"):
                    self.assertIn(path.read_text().strip(), codes | {"OAH_FIX_APPLIED", "OAH_FIX_NO_CHANGE"})
                for line in (project.folder() / "index.jsonl").read_bytes().splitlines():
                    data = json.loads(line)
                    if data["kind"].startswith("attempt-"):
                        check(data)

    def test_shared_lock_covers_only_every_modes_publication(self):
        for mode in ("fix", "manual", "bind", "abort"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                project.changes_requested()
                if mode == "manual":
                    project.commit(FIXED, trailer=HUMAN_TRAILER)
                elif mode == "bind":
                    project.queue_fix({"write": FIXED})
                    project.run_commands(project.fix().commands)
                elif mode == "abort":
                    publish_start(project)
                observed = []

                def probe(expected):
                    code = ("import sys; from oah.lockfile import StateLock, LockTimeout\n"
                            "try:\n with StateLock(sys.argv[1], timeout=0): pass\n"
                            "except LockTimeout: raise SystemExit(0)\nraise SystemExit(1)\n")
                    done = subprocess.run([sys.executable, "-c", code, str(project.state / ".lock")],
                                          capture_output=True, timeout=20,
                                          env=dict(project.env, PYTHONPATH=str(FAKES.parent.parent / "src")))
                    observed.append((done.returncode, expected))

                history, publish = fix._history, receipts.publish
                catalog_init = call._CorrectionCatalog.__init__
                def captured_models(catalog, *args, **kwargs):
                    probe(1)
                    return catalog_init(catalog, *args, **kwargs)
                def captured(*args, **kwargs):
                    probe(1)
                    return history(*args, **kwargs)
                def published(*args, **kwargs):
                    probe(0)
                    return publish(*args, **kwargs)
                action = (lambda: project.manual("human")) if mode == "manual" else getattr(project, mode)
                with mock.patch.object(fix, "_history", side_effect=captured), \
                        mock.patch.object(call._CorrectionCatalog, "__init__", captured_models), \
                        mock.patch.object(receipts, "publish", side_effect=published):
                    action()
                self.assertGreaterEqual(len(observed), 2)
                self.assertTrue(all(actual == expected for actual, expected in observed), observed)

    def test_manual_lease_change_after_preparation_refuses_all_three_records(self):
        project = FixProject(self)
        project.changes_requested()
        project.commit(FIXED, trailer=HUMAN_TRAILER)
        attribution = fix._attribution
        expected = []
        def changed(*args, **kwargs):
            result = attribution(*args, **kwargs)
            path = project.state / "leases.json"
            data = json.loads(path.read_bytes())
            data["leases"][0]["note"] = "preparation changed"
            path.write_bytes(json.dumps(data).encode("utf-8"))
            expected.append(state_snapshot(project.state))
            return result
        with mock.patch.object(fix, "_attribution", side_effect=changed):
            project.refused("OAH_LEASE_CHANGED", project.manual, "human")
        self.assertEqual(state_snapshot(project.state), expected[0])

    def test_every_mode_refuses_without_creating_missing_locks(self):
        for mode in ("fix", "manual", "bind", "abort"):
            for missing in ("task", "state"):
                with self.subTest(mode=mode, missing=missing):
                    project = FixProject(self)
                    if missing == "state":
                        (project.state / ".lock").unlink()
                    before = state_snapshot(project.state)
                    action = (lambda: project.manual("human")) if mode == "manual" else getattr(project, mode)
                    with self.assertRaises(ReviewError):
                        action()
                    self.assertEqual(state_snapshot(project.state), before)

    def test_every_mode_consumes_captured_state_after_publication_starts(self):
        for mode in ("fix", "manual", "bind", "abort"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                project.changes_requested()
                if mode == "manual":
                    project.commit(FIXED, trailer=HUMAN_TRAILER)
                elif mode == "bind":
                    project.queue_fix({"write": FIXED})
                    project.run_commands(project.fix().commands)
                elif mode == "abort":
                    # Include a real interrupted run, so abort must use its captured lifecycle record.
                    run = runs.new_run(project.state, "t1", "codex", "correction")
                    run.result(status="running")
                    publish_start(project)
                    path = project.folder() / "A1-start.json"
                    record = project.record(path.name)
                    record["run_id"] = run.id
                    path.write_bytes(receipts.canonical(record))
                    index = project.folder() / "index.jsonl"
                    lines = [json.loads(line) for line in index.read_bytes().splitlines()]
                    lines[-1]["digest"] = receipts.digest(path.read_bytes())
                    index.write_bytes(b"".join((json.dumps(line) + "\n").encode("utf-8") for line in lines))
                read, publish = pathlib.Path.read_bytes, receipts.publish
                publication = fix._publication
                started = False
                @contextlib.contextmanager
                def step(*args, **kwargs):
                    nonlocal started
                    started = False
                    with publication(*args, **kwargs):
                        yield
                    started = False
                def checked(path):
                    if started and path.is_relative_to(project.state):
                        self.fail("publication reopened a state input")
                    return read(path)
                def published(*args, **kwargs):
                    nonlocal started
                    result = publish(*args, **kwargs)
                    started = True
                    return result
                action = (lambda: project.manual("human")) if mode == "manual" else getattr(project, mode)
                with mock.patch.object(pathlib.Path, "read_bytes", checked), \
                        mock.patch.object(fix, "_publication", step), \
                        mock.patch.object(receipts, "publish", side_effect=published):
                    result = action()
                self.assertIn(result.code, ("OAH_FIX_NO_CHANGE", "OAH_FIX_RECORDED", "OAH_FIX_BOUND", "OAH_FIX_ABORTED"))

    def test_bind_and_abort_recheck_the_complete_captured_lease_before_writing(self):
        for mode in ("bind", "abort"):
            for change in ("renew", "release"):
                with self.subTest(mode=mode, change=change):
                    project = FixProject(self)
                    if mode == "bind":
                        project.run_commands(applied(self, project).commands)
                    else:
                        project.changes_requested()
                        publish_start(project)
                    publication = fix._publication
                    expected = []
                    @contextlib.contextmanager
                    def changed(t, h):
                        path = project.state / "leases.json"
                        data = json.loads(path.read_bytes())
                        if change == "renew":
                            data["leases"][0]["generation"] += 1
                        else:
                            data["leases"] = []
                        path.write_bytes(json.dumps(data).encode("utf-8"))
                        expected.append(state_snapshot(project.state))
                        with publication(t, h):
                            yield
                    with mock.patch.object(fix, "_publication", changed):
                        project.refused("OAH_LEASE_CHANGED", getattr(project, mode))
                    self.assertEqual(state_snapshot(project.state), expected[0])

    def test_noncanonical_lease_identifiers_leave_the_complete_state_tree_unchanged(self):
        for position, identifier in enumerate(("", "not-a-lease", "account-" + token_source.token_hex(8))):
            with self.subTest(position=position):
                project = FixProject(self)
                project.changes_requested()
                path = project.state / "leases.json"
                data = json.loads(path.read_bytes())
                data["leases"][0]["id"] = identifier
                path.write_bytes(json.dumps(data).encode("utf-8"))

                before = state_snapshot(project.state)
                error = project.refused("OAH_STATE_CORRUPT", project.fix)
                if identifier:
                    self.assertNotIn(identifier, str(error))
                self.assertEqual(state_snapshot(project.state), before)

    def test_expired_lease_is_rechecked_at_terminal_publication(self):
        project = FixProject(self)
        project.changes_requested()
        publish = fix._Attempt._publish
        with contextlib.ExitStack() as stack:
            def published(attempt, kind, *args, **kwargs):
                result = publish(attempt, kind, *args, **kwargs)
                if kind == "start":
                    # Start publication finishes, then the lease expires before terminal publication.
                    later = call.dt.datetime.now(call.dt.timezone.utc) + call.dt.timedelta(hours=2)
                    stack.enter_context(mock.patch("oah.lease.utc_now", return_value=later))
                return result

            stack.enter_context(mock.patch.object(fix._Attempt, "_publish", published))
            with self.assertRaises(ReviewError) as caught:
                project.fix()
        self.assertEqual(caught.exception.code, "OAH_LEASE_CHANGED")
        self.assertEqual(project.names(), ["R1.json", "A1-start.json"])

    def test_missing_task_lock_is_not_created_by_a_refusal(self):
        for reviewed in (False, True):
            with self.subTest(reviewed=reviewed):
                project = FixProject(self)
                if reviewed:
                    project.changes_requested()
                    shutil.rmtree(project.folder() / ".locks")

                before = state_snapshot(project.state)
                project.refused("OAH_FIX_HISTORY_INVALID" if reviewed else "OAH_FIX_NOTHING_TO_FIX", project.fix)
                self.assertEqual(state_snapshot(project.state), before)

    def test_publication_releases_the_shared_lock_for_execution(self):
        project = FixProject(self)
        project.changes_requested()
        execute, end = call.execute, fix._Attempt._end
        probed = []

        def probe():
            code = ("import sys; from pathlib import Path; from oah.lockfile import StateLock, LockTimeout\n"
                    "try:\n with StateLock(Path(sys.argv[1]) / '.lock', timeout=0): pass\n"
                    "except LockTimeout: raise SystemExit(0)\n"
                    "raise SystemExit(1)\n")
            done = subprocess.run([sys.executable, "-c", code, str(project.state)], capture_output=True,
                                  env=dict(project.env, PYTHONPATH=str(FAKES.parent.parent / "src")), timeout=20)
            probed.append(done.returncode)

        def dispatched(*args, **kwargs):
            probe()
            return execute(*args, **kwargs)

        def ended(attempt, *args, **kwargs):
            probe()
            return end(attempt, *args, **kwargs)

        with mock.patch.object(call, "execute", side_effect=dispatched), \
                mock.patch.object(fix._Attempt, "_end", ended):
            result = project.fix()
        self.assertEqual(probed, [1, 0])
        self.assertEqual(result.outcome, "no-change")
        Store(project.state, lock_timeout=0).renew(task="t1", agent="claude", ttl_hours=2)

    def test_preparation_lease_changes_leave_the_complete_state_tree_unchanged(self):
        for change in ("renew", "release", "same-generation"):
            with self.subTest(change=change):
                project = FixProject(self)
                project.changes_requested()
                # Force deferred catalog creation as well as resolution writes.
                shutil.rmtree(project.state / "models")

                expected = []
                resolve = call.resolve_engine

                def changed(*args, **kwargs):
                    result = resolve(*args, **kwargs)
                    path = project.state / "leases.json"
                    data = json.loads(path.read_bytes())
                    if change == "renew":
                        data["leases"][0]["generation"] += 1
                    elif change == "release":
                        data["leases"] = []
                    else:
                        data["leases"][0]["note"] = "changed without advancing generation"
                    path.write_bytes(json.dumps(data).encode("utf-8"))
                    expected.append(state_snapshot(project.state))
                    return result

                with mock.patch.object(call, "resolve_engine", side_effect=changed):
                    project.refused("OAH_LEASE_CHANGED", project.fix)
                self.assertEqual(state_snapshot(project.state), expected[0], "the refused fix must add no writes to the racing state")

    def test_the_entire_lease_record_is_scanned_before_any_state_write(self):
        for field in ("id", "note", "gates"):
            with self.subTest(field=field):
                project = FixProject(self)
                project.changes_requested()
                marker = "s" + "k-" + token_source.token_hex(20)
                path = project.state / "leases.json"
                data = json.loads(path.read_bytes())
                data["leases"][0][field] = [marker] if field == "gates" else marker
                path.write_bytes(json.dumps(data).encode("utf-8"))

                before = state_snapshot(project.state)
                error = project.refused("OAH_FIX_SENSITIVE_METADATA", project.fix)
                self.assertNotIn(marker, str(error))
                self.assertEqual(state_snapshot(project.state), before)

    def test_preparation_reads_each_state_input_once(self):
        project = FixProject(self)
        project.changes_requested()
        watched = {project.state / name for name in (
            "leases.json", "transaction.json", "runs/t1/intent-binding.json", "models/catalog.json",
            "models/resolved.json", "models/changes.jsonl", "reviews/t1/R1.json",
            "reviews/t1/index.jsonl", "reviews/t1/rounds.jsonl")}
        reads = {path: 0 for path in watched}
        at_plan = []
        read_bytes, plan = pathlib.Path.read_bytes, call._plan_prepare

        def read(path):
            if path in reads:
                reads[path] += 1
            return read_bytes(path)

        def planned(*args, **kwargs):
            result = plan(*args, **kwargs)
            at_plan.append(dict(reads))
            return result

        with mock.patch.object(pathlib.Path, "read_bytes", read), \
                mock.patch.object(call, "_plan_prepare", side_effect=planned):
            self.assertEqual(project.fix().outcome, "no-change")
        planned_reads = dict.fromkeys(watched, 1)
        log_path = project.state / "reviews/t1/rounds.jsonl"
        planned_reads[log_path] = 3  # Capture plus checks before skill ranking and model judgment.
        self.assertEqual(at_plan, [planned_reads], "capture inputs once; revalidate the live task budget before paid steps")
        expected = dict.fromkeys(watched, 3)  # Capture, start comparison and terminal comparison.
        expected[project.state / "transaction.json"] = 1  # Pending journal checks use existence after capture.
        for name in ("models/catalog.json", "models/resolved.json", "models/changes.jsonl"):
            expected[project.state / name] = 2  # Only the start publication depends on model inputs.
        expected[log_path] = 6  # Also revalidate the live task budget immediately before the engine starts.
        self.assertEqual(reads, expected)

    def test_invalid_model_pins_leave_the_complete_state_tree_unchanged(self):
        for pin in ("-invalid", "s" + "k-" + token_source.token_hex(20)):
            for cached in (True, False):
                with self.subTest(credential=not pin.startswith("-"), cached=cached):
                    project = FixProject(self)
                    project.changes_requested()
                    config = (project.repo / ".oah/config.toml").read_text(encoding="utf-8")
                    config += '\n[engines.codex.tiers.high]\nmodel = ' + json.dumps(pin) + '\n'
                    project.commit({".oah/config.toml": config})
                    if not cached:
                        shutil.rmtree(project.state / "models")

                    before = state_snapshot(project.state)
                    code = "OAH_MODEL_UNRESOLVED" if pin.startswith("-") else "OAH_FIX_SENSITIVE_INPUT"
                    error = project.refused(code, project.fix)
                    self.assertNotIn(pin, str(error))
                    self.assertEqual(state_snapshot(project.state), before,
                                     "invalid launch arguments must leave all state bytes and directories unchanged")

    def test_rejected_model_metadata_leaves_the_complete_state_tree_unchanged(self):
        for field in ("pin", "quality-order", "cache", "stored-catalog", "escaped-catalog", "escaped-changes",
                      "stored-resolution", "effective", "resolution", "late-brief", "no-model-state"):
            with self.subTest(field=field):
                project = FixProject(self)
                project.changes_requested()
                marker = "s" + "k-" + token_source.token_hex(20)
                config = (project.repo / ".oah/config.toml").read_text(encoding="utf-8")
                if field == "pin":
                    config += '\n[engines.codex.tiers.high]\nmodel = ' + json.dumps(marker) + '\n'
                elif field == "quality-order":
                    config += '\nquality_order = [' + json.dumps(marker) + ']\n'
                elif field in ("cache", "effective", "late-brief"):
                    # Force a real catalog refresh even though the preceding review populated it.
                    catalog = project.state / "models/catalog.json"
                    catalog.unlink()
                    if field == "cache":
                        cache = project.home / "models_cache.json"
                        data = json.loads(cache.read_bytes())
                        data["models"].append({"slug": marker, "supported_reasoning_levels": []})
                        cache.write_bytes(json.dumps(data).encode("utf-8"))
                    elif field == "effective":
                        (project.home / "config.toml").write_bytes(('model = ' + json.dumps(marker) + '\n').encode())
                else:
                    if field in ("stored-catalog", "stored-resolution", "escaped-catalog"):
                        path = project.state / ("models/resolved.json" if field == "stored-resolution" else "models/catalog.json")
                        data = json.loads(path.read_bytes())
                        data["unrelated"] = marker
                        encoded = json.dumps(data)
                        if field == "escaped-catalog":
                            encoded = encoded.replace(marker, "".join(f"\\u{ord(c):04x}" for c in marker))
                        path.write_bytes(encoded.encode("utf-8"))
                    elif field == "escaped-changes":
                        path = project.state / "models/changes.jsonl"
                        with path.open("ab") as stream:
                            encoded = json.dumps({"old": marker}).replace(
                                marker, "".join(f"\\u{ord(c):04x}" for c in marker))
                            stream.write((encoded + "\n").encode("utf-8"))
                    elif field == "no-model-state":
                        shutil.rmtree(project.state / "models")
                if config != (project.repo / ".oah/config.toml").read_text(encoding="utf-8"):
                    project.commit({".oah/config.toml": config})

                before = state_snapshot(project.state)
                with contextlib.ExitStack() as stack:
                    if field in ("late-brief", "no-model-state"):
                        render = call._brief_text
                        stack.enter_context(mock.patch.object(call, "_brief_text", side_effect=
                            lambda *args, **kwargs: render(*args, **kwargs) + marker))
                    elif field == "resolution":
                        resolve = call.resolve_engine
                        stack.enter_context(mock.patch.object(call, "resolve_engine", side_effect=
                            lambda *args, **kwargs: call.dataclasses.replace(resolve(*args, **kwargs), model=marker)))
                    error = project.refused("OAH_FIX_SENSITIVE_INPUT", project.fix)
                self.assertNotIn(marker, str(error))
                self.assertEqual(state_snapshot(project.state), before, "refusal must leave every state file and directory unchanged")

    def test_recovery_and_binding_notes_use_fixed_actor_metadata(self):
        for operation in ("abort", "bind-override"):
            with self.subTest(operation=operation):
                account = "account-" + token_source.token_hex(10)
                project = FixProject(self, agent=account)
                project.changes_requested()
                if operation == "abort":
                    publish_start(project)
                else:
                    project.queue_fix({"write": FIXED})
                    self.assertEqual(project.fix().outcome, "applied")
                    project.commit({"src/app.py": "print('replacement')\n"}, trailer=CLAUDE_TRAILER)
                events = project.state / "events.jsonl"
                before = events.read_bytes()
                result = project.abort() if operation == "abort" else project.bind(engine="claude")
                self.assertNotIn(account, json.dumps(result.to_json()))
                self.assertNotIn(account.encode("ascii"), events.read_bytes()[len(before):])

    def test_abort_does_not_copy_saved_status_or_invalid_run_id_into_new_records(self):
        for field in ("status", "run_id"):
            with self.subTest(field=field):
                project = FixProject(self)
                project.changes_requested()
                marker = "account-" + token_source.token_hex(12)
                run = runs.new_run(project.state, "t1", "codex", "correction")
                if field == "status":
                    record = run.read()
                    record["status"] = str(project.repo / marker)
                    run.result_path.write_bytes(json.dumps(record).encode("utf-8"))
                start = {"schema": 1, "kind": "attempt-start", "task": "t1", "attempt_id": "A1",
                         "source_receipt": "R1.json", "source_receipt_digest": project.receipt_digest(1),
                         "finding_ids": ["R1-F1"], "class_ids": ["R1-C1"], "before_head": project.head,
                         "engine": "codex", "manual": False, "started_at": "2026-10-25T00:00:00Z",
                         "run_id": str(project.repo / marker) if field == "run_id" else run.id}
                receipts.publish(project.state, "t1", "A1-start.json", start)
                if field == "run_id":
                    project.refused("OAH_FIX_HISTORY_INVALID", project.abort)
                else:
                    result = project.abort()
                    self.assertNotIn(marker, json.dumps(result.to_json()))
                    self.assertNotIn(marker, json.dumps(project.record("A1-outcome.json")))
                    self.assertEqual(project.record("A1-outcome.json")["evidence"], receipts.digest(
                        b"recorded by oah fix --abort; interrupted attempt; its run was last unknown"))

    def test_all_retained_brief_inputs_are_scanned_before_creating_a_correction_run(self):
        for field in ("intent", "finding", "class", "footer", "verify-footer", "skill"):
            for exported in (False, True):
                with self.subTest(field=field, exported=exported):
                    marker = "s" + "k-" + token_source.token_hex(20) if not exported else token_source.token_hex(20)
                    packet = PACKET.replace("### Problem", "### Problem\n" + marker) if field == "intent" else PACKET
                    skill = {".claude/skills/systematic-debugging/SKILL.md":
                             "---\nname: systematic-debugging\ndescription: trace bugs\n---\n" + marker + "\n"}
                    project = FixProject(self, packet=packet, files=skill if field == "skill" else None,
                                         config='[skills]\nmode = "attach"\n' if field == "skill" else "")
                    step = {"findings": [bug(title=marker) if field == "finding" else bug()]}
                    if field == "class":
                        step["classes"] = [{"class_id": "R{n}-C1", "mechanism": marker, "rule": "safe rule"}]
                    project.queue(step)
                    self.assertEqual(project.review().code, "OAH_REVIEW_CHANGES_REQUESTED")
                    if exported:
                        project.env["OPENAI_API_KEY"] = marker
                    before = {path: path.read_bytes() for path in project.state.rglob("*") if path.is_file()}
                    tools = call_cli.intent_tools()
                    with contextlib.ExitStack() as stack:
                        if field == "footer":
                            stack.enter_context(mock.patch.object(call_cli, "intent_tools", return_value=
                                tools._replace(footer=tools.footer + "\n" + marker + "\n")))
                        elif field == "verify-footer":
                            stack.enter_context(mock.patch.object(briefs, "VERIFY_FOOTER",
                                                                  briefs.VERIFY_FOOTER + "\n" + marker + "\n"))
                        error = project.refused("OAH_FIX_SENSITIVE_INPUT", project.fix)
                    self.assertNotIn(marker, str(error))
                    self.assertEqual({path: path.read_bytes() for path in project.state.rglob("*") if path.is_file()},
                                     before, "input refusal must not create or change run/state artifacts")

    def test_sensitive_task_and_path_metadata_refuses_before_retention(self):
        for operation in ("correct", "bind", "manual", "abort"):
            for field in ("task", "worktree", "state"):
                with self.subTest(operation=operation, field=field):
                    marker = "s" + "k-" + token_source.token_hex(20)
                    project = FixProject(self, repo_name=marker if field == "worktree" else "repo")
                    env = dict(project.env)
                    if field == "state":
                        env["OAH_STATE_DIR"] = str(project.work / marker)
                    task = marker if field == "task" else "t1"
                    actions = {"correct": lambda: fix.correct(fix.Request(task), cwd=project.repo, env=env),
                               "bind": lambda: fix.bind(task, cwd=project.repo, env=env),
                               "manual": lambda: fix.record_manual(task, engine="human", cwd=project.repo, env=env),
                               "abort": lambda: fix.abort(task, cwd=project.repo, env=env)}
                    before = {path: path.read_bytes() for path in project.state.rglob("*") if path.is_file()}
                    error = project.refused("OAH_TASK_INVALID" if field == "task" else "OAH_FIX_SENSITIVE_METADATA",
                                            actions[operation])
                    self.assertNotIn(marker, str(error))
                    self.assertEqual({path: path.read_bytes() for path in project.state.rglob("*") if path.is_file()},
                                     before)
                    if field == "state":
                        self.assertFalse((project.work / marker).exists())

    def test_packet_scope_is_checked_against_credentials_removed_from_worker_env(self):
        project = FixProject(self)
        project.changes_requested()
        marker = token_source.token_hex(20)
        project.env["OPENAI_API_KEY"] = marker
        project.commit({".oah/tasks/t1/PACKET.md": (PACKET + f"- {marker}\n").encode("utf-8")})
        error = project.refused("OAH_FIX_SENSITIVE_SCOPE", project.fix)
        self.assertNotIn(marker, str(error))

    def test_exception_text_never_reaches_correction_records_or_diagnostics(self):
        for point in ("inspection", "launch", "interrupt", "prepare", "dispatch", "unknown-code"):
            with self.subTest(point=point):
                project = FixProject(self)
                project.changes_requested()
                project.queue_fix({"write": FIXED})
                key = "s" + "k-" + token_source.token_hex(20)
                self.assertFalse(any(key in value for value in project.env.values()))
                if point == "prepare":
                    target, name = call.briefs, "build"
                    error = briefs.BriefError("OAH_UNSAFE_ARGUMENT", key)
                elif point == "dispatch":
                    target, name = call, "dispatch_guard"
                    error = call.EngineError("OAH_LEASE_CHANGED", key)
                else:
                    target, name = (call, "_run_prepared") if point == "launch" else (fix._Attempt, "_judge")
                    error = (KeyboardInterrupt(key) if point == "interrupt" else ReviewError(key, key)
                             if point == "unknown-code" else PermissionError(key))
                with mock.patch.object(target, name, side_effect=error):
                    try:
                        result = project.fix()
                    except (fix.FixInterrupted, ReviewError) as exc:
                        diagnostic = str(exc)
                    else:
                        diagnostic = json.dumps(result.to_json())
                self.assert_private(project, [key], diagnostic)
                if point != "prepare":
                    self.assertEqual(project.record("A1-outcome.json")["outcome"],
                                     "aborted" if point == "interrupt" else "failed")

    def test_intent_preflight_exceptions_omit_packet_values(self):
        for sensitive in (False, True):
            for mode in ("automatic", "manual"):
                with self.subTest(sensitive=sensitive, mode=mode):
                    project = FixProject(self)
                    value = ("s" + "k-" if sensitive else "untrusted-") + token_source.token_hex(20)
                    self.assertFalse(any(value in item for item in project.env.values()))
                    (project.repo / ".oah/tasks/t1/PACKET.md").write_bytes(
                        PACKET.replace("Status: accepted", f"Status: {value}").encode("utf-8"))
                    action = project.fix if mode == "automatic" else lambda: project.manual("human")
                    error = project.refused("OAH_INTENT_NOT_CURRENT", action)
                    self.assertIn("stale", str(error))
                    self.assert_private(project, [value], str(error))

    def test_regression_source_contains_no_env_secret_assignments(self):
        from tests.test_privacy_scan import load_scanner

        hits = load_scanner().scan_text(pathlib.Path(__file__).read_text(encoding="utf-8"), [])
        self.assertEqual([number for rule, number, _ in hits if rule == "env-secret"], [])

    def test_crlf_multiline_credentials_are_scanned_on_both_raw_sides(self):
        for channel in ("deleted", "replaced", "new", "user"):
            for newline in ("\n", "\r\n"):
                with self.subTest(channel=channel, environment_newline=repr(newline)):
                    project = FixProject(self)
                    key = "runtime-" + token_source.token_hex(16) + "\ncontinuation-" + token_source.token_hex(16)
                    name = "src/private.txt"
                    raw = (key.replace("\n", "\r\n") + "\r\n").encode("utf-8")
                    if channel in ("deleted", "replaced"):
                        project.commit({name: raw})
                        self.assertEqual(fix._git_bytes(project.repo, "cat-file", "blob", "HEAD:" + name), raw)
                    project.changes_requested()
                    project.env["OPENAI_API_KEY"] = key.replace("\n", newline)
                    if channel == "user":
                        (project.repo / name).write_bytes(raw)
                    content = None if channel == "deleted" else raw.decode("utf-8") if channel == "new" else "safe\n"
                    project.queue_fix({"write": {name: content, **FIXED}})
                    result = project.fix()
                    self.assertEqual((result.outcome, result.commands), ("failed", []))
                    self.assertIsNone(result.next_command)
                    self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                    self.assert_private(project, [key, *key.splitlines()], json.dumps(result.to_json()))
                    patch = (project.run_folder(result) / "correction.patch").read_bytes()
                    self.assertIn("[file ", result.message)
                    self.assertNotIn(name.encode("utf-8"), patch)
                    self.assertNotIn(b"src/app.py", patch)
                    self.assertIn(b"src/app.py", b"".join(project.patches))

    def test_unexported_credential_filenames_are_withheld_by_position(self):
        for channel in ("new", "tracked", "deleted", "renamed-from", "renamed-to", "user", "user-deleted",
                        "out-of-scope", "encoded", "failed", "env-secret"):
            with self.subTest(channel=channel):
                project = FixProject(self)
                key = ("SERVICE_" + "TOK" + "EN=" if channel == "env-secret" else "s" + "k-") + token_source.token_hex(20)
                name = ("outside/" if channel == "out-of-scope" else "src/") + key + ".txt"
                if channel == "encoded":
                    name += "-한글"
                project.changes_requested()
                if channel in ("tracked", "deleted", "renamed-from"):
                    project.commit({name: b"harmless\n"})
                elif channel == "renamed-to":
                    project.commit({"src/original.txt": b"harmless\n"})
                if channel in ("user", "user-deleted"):
                    (project.repo / name).write_bytes(b"harmless\n")
                writes = {**FIXED, name: None if channel in ("deleted", "renamed-from", "user-deleted") else "after\n"}
                if channel == "renamed-from":
                    writes["src/renamed.txt"] = "harmless\n"
                elif channel == "renamed-to":
                    writes.update({"src/original.txt": None, name: "harmless\n"})
                self.assertFalse(any(key in value for value in project.env.values()))
                project.queue_fix({"write": writes, "exit": 1 if channel == "failed" else 0})
                terminal = io.StringIO()
                result = project.fix(terminal=terminal)
                self.assert_private(project, [key], json.dumps(result.to_json()), terminal.getvalue())
                self.assertEqual((result.code, result.outcome, result.commands), ("OAH_FIX_FAILED", "failed", []))
                self.assertIsNone(result.next_command)
                self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                patch = (project.run_folder(result) / "correction.patch").read_text(encoding="utf-8")
                saved = json.loads((project.run_folder(result) / "correction.json").read_text(encoding="utf-8"))
                self.assertEqual(patch, "OAH_FIX_FAILED\n")
                self.assertTrue(any(item.startswith("[file ") for field in ("changed", "new_files", "user_files")
                                    for item in saved[field]))
                project.refused("OAH_FIX_NOTHING_TO_BIND", project.bind)

    def test_unexported_credential_filenames_are_withheld_in_stop_diagnostics(self):
        for exception in (KeyboardInterrupt, PermissionError):
            with self.subTest(exception=exception.__name__):
                project = FixProject(self)
                project.changes_requested()
                key = "s" + "k-" + token_source.token_hex(20)
                project.queue_fix({"write": {"src/" + key + ".txt": "harmless\n"}})
                with mock.patch.object(fix._Attempt, "_judge", side_effect=exception("inspection stopped")):
                    try:
                        project.fix()
                    except (fix.FixInterrupted, ReviewError) as error:
                        self.assert_private(project, [key], str(error))
                    else:
                        self.fail("inspection must stop")
                outcome = project.record("A1-outcome.json")
                self.assertEqual(outcome["outcome"], "aborted" if exception is KeyboardInterrupt else "failed")
                self.assertEqual(outcome["evidence"], receipts.digest(fix._error_diagnostic(
                    KeyboardInterrupt() if exception is KeyboardInterrupt else PermissionError())[1].encode("utf-8")))

    def test_unexported_credential_filenames_are_withheld_in_dirty_tree_refusals(self):
        for mode in ("correct", "manual", "bind"):
            with self.subTest(mode=mode):
                project = FixProject(self)
                project.changes_requested()
                if mode == "bind":
                    project.queue_fix({"write": FIXED})
                    self.assertEqual(project.fix().outcome, "applied")
                key = "s" + "k-" + token_source.token_hex(20)
                name = "src/" + key + ".txt"
                project.commit({name: b"harmless\n"})
                (project.repo / name).write_bytes(b"changed\n")
                action = project.fix if mode == "correct" else project.bind if mode == "bind" else \
                    lambda: project.manual("human")
                error = project.refused("OAH_FIX_DIRTY_TREE", action)
                self.assert_private(project, [key], str(error))
                self.assertNotIn(name, str(error))

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "POSIX file read permissions")
    def test_unexported_credential_filenames_are_withheld_in_file_read_errors(self):
        project = FixProject(self)
        project.changes_requested()
        key = "s" + "k-" + token_source.token_hex(20)
        name = "src/" + key + ".txt"
        project.commit({name: b"before\n"})
        project.queue_fix({"write": {name: "harmless\n"}})
        judge = fix._Attempt._judge

        def unreadable(attempt, *args, **kwargs):
            target = project.repo / name
            mode = target.stat().st_mode
            target.chmod(0)
            try:
                return judge(attempt, *args, **kwargs)
            finally:
                target.chmod(mode)

        with mock.patch.object(fix._Attempt, "_judge", unreadable), self.assertRaises(ReviewError) as caught:
            project.fix()
        self.assert_private(project, [key], str(caught.exception))
        self.assertEqual(caught.exception.code, "OAH_FIX_GIT_FAILED")
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")

    def test_unexported_credential_patterns_withhold_raw_file_inputs(self):
        for pattern in ("key", "env-secret"):
            for channel in ("tracked", "new", "deleted", "replaced", "user", "utf16-le", "utf16-be", "binary"):
                with self.subTest(pattern=pattern, channel=channel):
                    project = FixProject(self)
                    key = ("s" + "k-" if pattern == "key" else "") + token_source.token_hex(20)
                    text = (("SERVICE_TOKEN" + "=") if pattern == "env-secret" else "") + key + "\n"
                    raw = text.encode("utf-8")
                    if channel.startswith("utf16"):
                        bom = b"\xff\xfe" if channel == "utf16-le" else b"\xfe\xff"
                        raw = bom + text.encode(channel.replace("utf16", "utf-16"))
                    elif channel == "binary":
                        raw = b"\x00\xff" + raw
                    name = "src/private.txt"
                    old_side = channel in ("deleted", "replaced", "utf16-le", "utf16-be", "binary")
                    if old_side or channel == "tracked":
                        project.commit({name: raw if old_side else b"before\n"})
                    project.changes_requested()
                    if channel == "user":
                        (project.repo / name).write_bytes(raw)
                    self.assertFalse(any(key in value for value in project.env.values()))
                    content = None if channel == "deleted" else "replacement\n" if old_side or channel == "user" else text
                    project.queue_fix({"write": {**FIXED, name: content}})
                    terminal = io.StringIO()
                    result = project.fix(terminal=terminal)
                    self.assertEqual((result.outcome, result.commands), ("failed", []))
                    self.assertIsNone(result.next_command)
                    self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
                    self.assert_private(project, [key, text.rstrip()], json.dumps(result.to_json()), terminal.getvalue())
                    self.assertNotIn(name, json.dumps(result.to_json()), "withheld files are position-only")
                    patch = (project.run_folder(result) / "correction.patch").read_text(encoding="utf-8")
                    self.assertIn("OAH_FIX_SENSITIVE_CONTENT", result.message)
                    self.assertIn("[file ", result.message)
                    self.assertNotIn(name, patch)
                    self.assertNotIn("src/app.py", patch)
                    self.assertIn(b"src/app.py", b"".join(project.patches), "safe raw diffs are built only in memory")

    def test_diff_prefixed_worker_streams_are_neither_displayed_nor_retained(self):
        for channel in ("stdout", "stderr", "last_message"):
            with self.subTest(channel=channel):
                project = FixProject(self)
                project.changes_requested()
                key = "runtime-" + token_source.token_hex(16) + "\ncontinuation-" + token_source.token_hex(16)
                project.env["OPENAI_API_KEY"] = key
                text = "+" + key.replace("\n", "\n+") + "\n"
                project.queue_fix({"write": FIXED, "last_message": "", channel: text})
                terminal = io.StringIO()
                result = project.fix(terminal=terminal)
                self.assertEqual(result.outcome, "applied", result.message)
                self.assertNotIn(text, terminal.getvalue(), "only dispatcher-authored scanned diffs may be displayed")
                self.assert_private(project, [key, *key.splitlines()], json.dumps(result.to_json()))
                folder = project.run_folder(result)
                for name in ("stdout.log", "stderr.log", "last-message.md"):
                    self.assertFalse((folder / name).exists(), "corrector text must never be written")
                run = json.loads((folder / "result.json").read_bytes())
                stream = run["worker_output"]["stdout" if channel == "last_message" else channel]
                self.assertGreaterEqual(stream["bytes"], len(text.encode("utf-8")))
                self.assertRegex(stream["sha256"], r"^[0-9a-f]{64}$")
                if channel == "stdout":
                    expected = (text + "fake correction done\n").encode("utf-8")
                    self.assertEqual(stream, {"bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest()})

    def test_patch_uses_raw_bytes_without_git_content_conversion(self):
        for conversion in ("clean", "process", "textconv", "encoding", "eol", "ident"):
            with self.subTest(conversion=conversion):
                name = "src/converted.txt"
                old = b"old $Id$\n"
                project = FixProject(self, files={name: old})
                project.changes_requested()
                key = "runtime-" + token_source.token_hex(16) + "\ncontinuation-" + token_source.token_hex(16)
                project.env["OPENAI_API_KEY"] = key
                marker = project.work / "conversion-ran"
                payload = project.work / "payload"
                payload.write_bytes((key + "\n").encode("utf-8"))
                driver = project.work / "conversion.py"
                # A process filter speaks Git's pkt-line protocol; other drivers read ordinary stdin.
                driver.write_bytes(
                    b"import pathlib, sys\n"
                    b"pathlib.Path(sys.argv[1]).write_bytes(b'ran')\n"
                    b"data = pathlib.Path(sys.argv[2]).read_bytes()\n"
                    b"if sys.argv[3] != 'process':\n"
                    b"    sys.stdin.buffer.read()\n"
                    b"    sys.stdout.buffer.write(data)\n"
                    b"    sys.exit(0)\n"
                    b"def read():\n"
                    b"    result = []\n"
                    b"    while True:\n"
                    b"        size = sys.stdin.buffer.read(4)\n"
                    b"        if not size: raise EOFError\n"
                    b"        size = int(size, 16)\n"
                    b"        if not size: return result\n"
                    b"        result.append(sys.stdin.buffer.read(size - 4))\n"
                    b"def write(parts):\n"
                    b"    for part in parts:\n"
                    b"        sys.stdout.buffer.write(('%04x' % (len(part) + 4)).encode('ascii') + part)\n"
                    b"    sys.stdout.buffer.write(b'0000')\n"
                    b"    sys.stdout.buffer.flush()\n"
                    b"read()\nwrite([b'git-filter-server\\n', b'version=2\\n'])\n"
                    b"read()\nwrite([b'capability=clean\\n', b'capability=smudge\\n'])\n"
                    b"try:\n"
                    b"    while True:\n"
                    b"        read()\n        read()\n"
                    b"        write([b'status=success\\n'])\n        write([data])\n        write([])\n"
                    b"except EOFError:\n    pass\n")
                command = " ".join('"' + path.as_posix() + '"' for path in
                                   (pathlib.Path(sys.executable), driver, marker, payload)) + " " + conversion
                new = (b"old $Id$\r\n" if conversion == "eol" else
                       b"old $Id: expanded-id $\n" if conversion == "ident" else
                       "new $Id$\n".encode("utf-16-le") if conversion == "encoding" else b"new $Id$\n")
                project.queue_fix({"write": {name: "temporary\n", **FIXED}})
                execute = call.execute

                def dispatched(*args, **kwargs):
                    result = execute(*args, **kwargs)
                    (project.repo / name).write_bytes(new)
                    attribute = {"clean": "filter=custom", "process": "filter=custom", "textconv": "diff=custom",
                                 "encoding": "working-tree-encoding=UTF-16LE", "eol": "text eol=lf",
                                 "ident": "ident"}[conversion]
                    (project.repo / ".git/info/attributes").write_bytes((name + " " + attribute + "\n").encode("ascii"))
                    if conversion in ("clean", "process"):
                        git(project.repo, "config", "filter.custom." + conversion, command)
                        git(project.repo, "config", "filter.custom.required", "true")
                    elif conversion == "textconv":
                        git(project.repo, "config", "diff.custom.textconv", command)
                    return result

                with mock.patch.object(call, "execute", side_effect=dispatched):
                    result = project.fix()
                self.assertEqual(result.outcome, "applied", result.message)
                self.assertFalse(marker.exists(), "change discovery and patch generation must not run conversions")
                self.assert_private(project, [key, *key.splitlines()], json.dumps(result.to_json()))
                patch = b"".join(project.patches)
                if conversion == "encoding":
                    self.assertIn(f"binary, {len(new)} bytes".encode("ascii"), patch)
                else:
                    self.assertIn(b"-" + old, patch)
                    self.assertIn(b"+" + new, patch)

    def test_patch_uses_the_same_bytes_that_passed_the_scan(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        key = "runtime-" + token_source.token_hex(16) + "\ncontinuation-" + token_source.token_hex(16)
        project.env["OPENAI_API_KEY"] = key
        scan = fix._scan_files
        patch, patches = fix._patch, []

        def scanned(*args, **kwargs):
            result = scan(*args, **kwargs)
            (project.repo / "src/app.py").write_bytes((key + "\n").encode("utf-8"))
            return result

        def captured(*args, **kwargs):
            data = patch(*args, **kwargs)
            patches.append(data)
            return data

        with mock.patch.object(fix, "_scan_files", side_effect=scanned), \
                mock.patch.object(fix, "_patch", side_effect=captured):
            with self.assertRaises(ReviewError) as caught:
                project.fix()
        self.assertEqual(caught.exception.code, "OAH_FIX_WORKTREE_CHANGED")
        self.assert_private(project, [key, *key.splitlines()], str(caught.exception))
        self.assertIn(b"+print('fixed')", patches[0])
        self.assertNotIn(key.encode("utf-8"), patches[0])
        self.assertEqual(project.record("A1-outcome.json")["outcome"], "failed")
        run = runs.find_run(project.state, project.record("A1-start.json")["run_id"])
        self.assertEqual(json.loads(run.file_path("correction.json").read_bytes())["commands"], [])

    def test_binary_and_undecodable_sides_are_size_markers_only(self):
        for old, new in ((b"old\x00secret\n", b"new\x00secret\n"), (b"old\xff\n", b"new\xfe\n")):
            with self.subTest(old=old):
                project = FixProject(self, files={"src/data": old})
                project.changes_requested()
                project.queue_fix({"write": {"src/data": "temporary\n"}})
                execute = call.execute

                def dispatched(*args, **kwargs):
                    result = execute(*args, **kwargs)
                    (project.repo / "src/data").write_bytes(new)
                    return result

                with mock.patch.object(call, "execute", side_effect=dispatched):
                    result = project.fix()
                self.assertEqual(result.outcome, "applied", result.message)
                patch = b"".join(project.patches)
                for data in (old, new):
                    self.assertIn(f"binary, {len(data)} bytes".encode("ascii"), patch)
                    self.assertNotIn(data, patch)

    @unittest.skipUnless(os.name != "nt", "POSIX symlinks; Windows requires symlink privileges")
    def test_external_symlinks_are_withheld_by_position(self):
        for side in ("old", "new", "parent"):
            with self.subTest(side=side):
                project = FixProject(self)
                name = "src/link" if side != "parent" else "src/app.py"
                target = project.work / "outside"
                target.mkdir()
                (target / "app.py").write_bytes(b"outside contents\n")
                project.changes_requested()
                if side == "old":
                    (project.repo / name).symlink_to(target / "app.py")
                    project.commit({})
                project.queue_fix({"write": FIXED})
                execute = call.execute

                def dispatched(*args, **kwargs):
                    result = execute(*args, **kwargs)
                    path = project.repo / name
                    if side == "old":
                        path.unlink()
                    elif side == "new":
                        path.symlink_to(target / "app.py")
                    else:
                        path.unlink()
                        path.parent.rmdir()
                        path.parent.symlink_to(target, target_is_directory=True)
                    return result

                with mock.patch.object(call, "execute", side_effect=dispatched):
                    result = project.fix()
                self.assertEqual((result.outcome, result.commands), ("failed", []))
                self.assertIsNone(result.next_command)
                self.assertNotIn(name, json.dumps(result.to_json()))
                patch = (project.run_folder(result) / "correction.patch").read_bytes()
                self.assertNotIn(b"outside contents", patch)
                self.assertIn("[file ", result.message)

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
        patch = b"".join(project.patches)
        self.assertIn(b"+print('fixed')", patch)
        self.assertIn(b"+NEW = 1", patch)
        self.assertEqual(built, [], "Git must never build correction diff content")

    def test_submodule_changes_fail_closed_before_building_a_diff(self):
        for change in ("dirty", "ignored", "deleted", "added", "untracked", "index-only", "head-only"):
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
                    nested = project.repo / name
                    git(nested, "config", "core.autocrlf", "true")
                    (nested / "private.txt").unlink()
                    git(nested, "checkout", "--", "private.txt")
                    self.assertEqual((nested / "private.txt").read_bytes(), b"before\r\n")
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
                    elif change == "head-only":
                        subprocess.run(["git", "-C", str(project.repo / name), "-c", "commit.gpgsign=false", "commit",
                                        "-q", "--allow-empty", "-m", "move nested HEAD without changing its files"],
                                       env=git_env(), check=True, capture_output=True)
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
                patch = (project.run_folder(result) / "correction.patch").read_text(encoding="utf-8")
                expected = ("OAH_FIX_INDEX_CHANGED" if change in ("deleted", "added", "untracked", "index-only")
                            else "OAH_FIX_UNSUPPORTED_SUBMODULE")
                self.assertIn(expected, result.message)
                self.assertIn("[file ", result.message)
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
                patch = (project.run_folder(result) / "correction.patch").read_text(encoding="utf-8")
                self.assertEqual(patch, "OAH_FIX_FAILED\n")
                if channel == "staged":
                    self.assertIn("OAH_FIX_INDEX_CHANGED", result.message)
                    self.assertNotIn("diff --git", patch)
                else:
                    self.assertNotIn("src/app.py", patch)
                    self.assertIn(b"src/app.py", b"".join(project.patches), "safe raw diffs are built only in memory")
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
                patch = (project.run_folder(result) / "correction.patch").read_text(encoding="utf-8")
                self.assertEqual(patch, "OAH_FIX_FAILED\n")

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
                wanted.update(OAH_WORKER="1", OAH_TASK="t1", OAH_RUN_ID=result.run_id)
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
                saved = json.loads((project.run_folder(result) / "correction.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["commands"], [])
                if channel == "encoded":
                    patch = (project.run_folder(result) / "correction.patch").read_text(encoding="utf-8")
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

    def test_bind_withholds_credential_shaped_filenames_in_older_saved_commands(self):
        project = FixProject(self)
        result = applied(self, project)
        key = "s" + "k-" + token_source.token_hex(20)
        manifest = project.run_folder(result) / "correction.json"
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        saved["commands"] = ["git add -- src/" + key + ".txt"]
        manifest.write_text(json.dumps(saved), encoding="utf-8")
        error = project.refused("OAH_FIX_SENSITIVE_INPUT", project.bind)
        self.assertFalse(key in str(error), "a credential-shaped filename reached saved command guidance")
        self.assertNotIn("git add", str(error))

    def test_fix_exception_contains_no_credential(self):
        project = FixProject(self)
        project.changes_requested()
        key = "runtime-" + token_source.token_hex(24)
        project.env["OPENAI_API_KEY"] = key
        error = project.refused("OAH_FIX_FINDING_UNKNOWN", project.fix, findings=(key,))
        self.assertEqual(str(error), "OAH_FIX_FINDING_UNKNOWN: the selection is not a P0/P1 finding of the "
                         "latest receipt; inspect that receipt and retry with its finding IDs.")
        self.assertNotIn(key, str(error))
        self.assertIsNone(error.__context__)


class TemplateTest(unittest.TestCase):
    def test_powershell_guidance_omits_the_worktree_argument(self):
        root = pathlib.Path("/leased 'space $cash `tick !")
        with mock.patch.object(fix.os, "name", "nt"):
            commands, unquoted = fix.commit_commands(
                task="t1", attempt_id="A1", receipt="R1", finding_ids=["R1-F1"],
                changes=fix._Changes(("src/app.py",), ()), root=root)
        self.assertEqual(unquoted, [])
        self.assertEqual(commands[0], "git add -u")
        for command in commands:
            self.assertTrue(command.startswith("git "))

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
        self.assertEqual(commands, ["git add -u",
                                    'git commit -m "Correct review findings" -m "Agent-Engine: codex"'])
        self.assertEqual(unquoted, [])
        for command in commands:
            self.assertFalse(set(command) & set("$`!"), "nothing either shell would expand")
            for name in (*new, "t1", "R3-F1", "R3-F2"):
                self.assertNotIn(name, command)



if __name__ == "__main__":
    unittest.main()
