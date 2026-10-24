# oah fix 검사 — 가짜 리뷰어·수정자 프로세스와 실제 모듈로 수정 시도 기록, 작업자 실행, 끝마다 하나뿐인 결과, 커밋 묶기, 수동 수정, 리뷰 왕복, 비밀 차단을 본다.
from __future__ import annotations

import _thread
import json
import os
import pathlib
import shlex
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
from oah.review import ReviewError, fix, receipts, runner
from oah.secrets import NoKeychain
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir
from tests.test_review_rounds import CLAUDE_TRAILER, LEVELS, PACKET, RULES, SECTION, bug, git, toml_list

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_FIX = FAKES / "fake_fix_codex.py"
CODEX_TRAILER = "\n\nAgent-Engine: codex"
HUMAN_TRAILER = "\n\nAgent-Engine: human"


class FixProject:
    """A task branch with a bound lease, reviewed and corrected by the fake engine processes."""

    def __init__(self, test: unittest.TestCase, *, config: str = "", files: dict | None = None,
                 packet: str = PACKET, newline: str = "\n", attributes: str | None = "* -text\n") -> None:
        self.test = test
        self.repo = make_git_repo(test)
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


def interrupt_when(path: pathlib.Path) -> threading.Thread:
    """Deliver Ctrl-C to this (main) thread, as Python sees it, once the hanging worker wrote its pid file."""
    def run():
        if wait_for(path):
            _thread.interrupt_main()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class CallSeamTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
