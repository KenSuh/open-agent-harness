# 의도 결속 배선 검사 — 실제 oah 명령으로 start --packet 결속·재결속, status 의 네 상태, finish 의 반환 줄 검사를 끝에서 끝까지 본다.
from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys
import unittest

from oah import briefs, intent
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
PACKET = ".oah/tasks/{task}/PACKET.md"


def packet_text(task: str = "t1", *, revision: int = 1, status: str = "accepted", ident: str | None = None,
                scope: tuple[str, ...] = ("src",), acs: tuple[str, ...] = ("the report lists every file",
                                                                             "the digest is stable"),
                constraints: str = "") -> str:
    lines = [intent.BEGIN, "## Intent", f"Intent-ID: {ident or task}", f"Revision: {revision}", f"Status: {status}",
             "### Problem", "Reports miss files.", "### Desired outcome", "Every changed file is listed.",
             "### Success conditions", *(f"- AC{n}: {text}" for n, text in enumerate(acs, start=1)),
             "### Constraints", *([constraints] if constraints else []), "### Non-goals", "### Open questions",
             intent.END, "## Scope", *(f"- {entry}" for entry in scope), "## Plan", "- initial → claude → medium",
             "## Acceptance", "## Return", ""]
    return "\n".join(lines)


def clean_env(home: pathlib.Path, **extra: str) -> dict:
    """The parent environment without oah or fake-engine settings, with a temporary home folder."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("OAH_", "FAKE_"))}
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        env.pop(name, None)
    env.update(PYTHONPATH=SRC, HOME=str(home), USERPROFILE=str(home), **extra)
    return env


class Repo:
    """A temporary repository with one commit, driven through the real `python -m oah`."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        self.root = make_git_repo(test)
        git_commit(self.root, {"README.md": "demo\n", "src/app.py": "x = 1\n"})
        self.home = make_tmp_dir(test)
        self.env = clean_env(self.home)

    def oah(self, *args: str, stdin: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "oah", *args], cwd=self.root, input=stdin, capture_output=True,
                              text=True, encoding="utf-8", env=dict(self.env, **(env or {})), timeout=120)

    def ok(self, *args: str, **kw) -> subprocess.CompletedProcess:
        result = self.oah(*args, **kw)
        self.test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def path(self, task: str = "t1") -> pathlib.Path:
        return self.root / PACKET.format(task=task)

    def write_packet(self, text: str, task: str = "t1") -> pathlib.Path:
        path = self.path(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def start_packet(self, task: str = "t1", *extra: str) -> subprocess.CompletedProcess:
        return self.oah("start", "--task", task, "--agent", "claude", "--packet", PACKET.format(task=task), *extra)

    def status(self) -> dict:
        return {lease["task"]: lease for lease in json.loads(self.ok("status", "--json").stdout)["leases"]}

    def events(self) -> list[dict]:
        common = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], capture_output=True, text=True, check=True).stdout.strip()
        lines = (pathlib.Path(common) / "oah" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def returned(self, task: str = "t1", *, acs: dict | None = None) -> str:
        parsed = intent.parse(self.path(task).read_text(encoding="utf-8"))
        statuses = acs or {ac: "met" for ac in parsed.acs}
        lines = ["Summary: done.", parsed.line(), *(f"{ac}: {state} — tests pass" for ac, state in statuses.items())]
        return "\n".join(lines) + "\n"


class StartPacketTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)

    def test_start_packet_binds_in_the_same_command(self):
        text = packet_text()
        self.repo.write_packet(text)
        started = self.repo.start_packet()
        self.assertEqual(started.returncode, 0, started.stderr)
        parsed = intent.parse(text)
        self.assertIn("OAH_LEASE_STARTED task=t1", started.stdout)
        self.assertIn("intent=t1@r1", started.stdout)
        lease = self.repo.status()["t1"]
        self.assertEqual(lease["intent"], parsed.bind(PACKET.format(task="t1")).to_json())
        self.assertEqual(lease["scope"], ["src"])
        self.assertEqual(lease["intent_state"], "current")
        shown = self.repo.ok("intent", "show", "t1").stdout
        self.assertIn("state=current", shown)
        self.assertIn(f"bound={parsed.line()}", shown)

    def test_start_packet_refuses_draft(self):
        self.repo.write_packet(packet_text(status="draft"))
        refused = self.repo.start_packet()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_INTENT_NOT_ACCEPTED"), refused.stderr)
        self.assertEqual(self.repo.status(), {})

    def test_start_packet_refuses_other_intent_id(self):
        self.repo.write_packet(packet_text(ident="other"))
        refused = self.repo.start_packet()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_INTENT_ID_CHANGED"), refused.stderr)
        self.assertEqual(self.repo.status(), {})

    def test_start_packet_refuses_invalid_and_missing_packet(self):
        self.repo.write_packet(packet_text().replace("### Problem\n", ""))
        refused = self.repo.start_packet()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_INTENT_FIELD_INVALID"), refused.stderr)
        self.assertIn("Also: OAH_INTENT_SECTION_INVALID", refused.stderr)
        missing = self.repo.start_packet("absent")
        self.assertEqual(missing.returncode, 1)
        self.assertTrue(missing.stderr.startswith("OAH_INTENT_SOURCE_MISSING"), missing.stderr)
        self.assertIn("oah packet new absent", missing.stderr)

    def test_start_packet_refuses_packet_outside_repository(self):
        outside = make_tmp_dir(self) / "PACKET.md"
        outside.write_text(packet_text(), encoding="utf-8")
        refused = self.repo.oah("start", "--task", "t1", "--agent", "claude", "--packet", str(outside))
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_INTENT_SOURCE_INVALID"), refused.stderr)

    def test_start_packet_empty_scope_needs_scope_repo(self):
        self.repo.write_packet(packet_text(scope=()))
        refused = self.repo.start_packet()
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_SCOPE_EMPTY"), refused.stderr)
        self.assertIn("--scope repo", refused.stderr)
        narrow = self.repo.start_packet("t1", "--scope", "src")
        self.assertEqual(narrow.returncode, 1)
        self.assertTrue(narrow.stderr.startswith("OAH_SCOPE_EMPTY"), narrow.stderr)
        self.repo.ok("start", "--task", "t1", "--agent", "claude", "--packet", PACKET.format(task="t1"),
                     "--scope", "repo")
        self.assertEqual(self.repo.status()["t1"]["scope"], ["repo"])

    def test_scope_adds_to_packet_scope(self):
        self.repo.write_packet(packet_text(scope=("src",)))
        self.repo.ok("start", "--task", "t1", "--agent", "claude", "--packet", PACKET.format(task="t1"),
                     "--scope", "docs")
        self.assertEqual(self.repo.status()["t1"]["scope"], ["src", "docs"])

    def test_start_without_scope_or_packet_is_usage(self):
        refused = self.repo.oah("start", "--task", "t1", "--agent", "claude")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--scope", refused.stderr)

    def test_start_packet_raised_revision_rebinds(self):
        self.repo.write_packet(packet_text())
        self.assertEqual(self.repo.start_packet().returncode, 0)
        again = self.repo.start_packet()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("OAH_INTENT_UNCHANGED", again.stdout)
        self.repo.write_packet(packet_text(acs=("a different goal",)))
        edited = self.repo.start_packet()
        self.assertEqual(edited.returncode, 1)
        self.assertTrue(edited.stderr.startswith("OAH_INTENT_REVISION_REQUIRED"), edited.stderr)
        self.assertEqual(self.repo.status()["t1"]["intent_state"], "stale")
        self.repo.write_packet(packet_text(revision=2, acs=("a different goal",), scope=("src", "tests")))
        rebound = self.repo.start_packet()
        self.assertEqual(rebound.returncode, 0, rebound.stderr)
        self.assertIn("OAH_INTENT_REBOUND task=t1 intent=t1@r2", rebound.stdout)
        lease = self.repo.status()["t1"]
        self.assertEqual((lease["intent"]["revision"], lease["intent_state"]), (2, "current"))
        self.assertEqual(lease["scope"], ["src", "tests"])
        self.assertEqual(sum(event["event"] == "intent_rebound" for event in self.repo.events()), 1)

    def test_other_agent_cannot_rebind(self):
        self.repo.write_packet(packet_text())
        self.assertEqual(self.repo.start_packet().returncode, 0)
        refused = self.repo.oah("start", "--task", "t1", "--agent", "codex", "--packet", PACKET.format(task="t1"))
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_LEASE_CONFLICT"), refused.stderr)

    def test_delivered_lease_command_gives_current(self):
        source = PACKET.format(task="t1")
        self.repo.write_packet(packet_text())
        command = briefs.lease_command("t1", "claude", source)
        argv = shlex.split(command)
        self.assertEqual(argv[:2], ["oah", "start"])
        self.repo.ok(*argv[1:])
        self.assertIn("state=current", self.repo.ok("intent", "show", "t1").stdout)


class StatusTest(unittest.TestCase):
    def test_status_reports_each_binding_state(self):
        repo = Repo(self)
        repo.ok("start", "--task", "plain", "--agent", "claude", "--scope", "README.md")
        cases = {"cur": "src/cur", "edited": "src/edited", "gone": "src/gone", "broken": "src/broken",
                 "draft": "src/draft"}
        for task, scope in cases.items():
            repo.write_packet(packet_text(task, scope=(scope,)), task)
            self.assertEqual(repo.start_packet(task).returncode, 0)
        repo.write_packet(packet_text("edited", scope=("src/edited",), acs=("changed",)), "edited")
        repo.path("gone").unlink()
        broken = repo.path("broken")
        broken.write_text(broken.read_text(encoding="utf-8") + intent.BEGIN + "\n", encoding="utf-8")
        repo.write_packet(packet_text("draft", scope=("src/draft",), status="draft"), "draft")
        states = {task: lease["intent_state"] for task, lease in repo.status().items()}
        self.assertEqual(states, {"plain": "unbound", "cur": "current", "edited": "stale", "gone": "missing",
                                  "broken": "stale", "draft": "stale"})
        reasons = {task: lease["intent_reason"] for task, lease in repo.status().items()}
        self.assertIn("OAH_INTENT_MARKERS_INVALID", reasons["broken"])
        self.assertIn("status is not accepted", reasons["draft"])
        text = repo.ok("status").stdout
        for task, state in states.items():
            line = next(line for line in text.splitlines() if f"task={task} " in line)
            self.assertIn(f"intent={state}", line)


    def test_binding_is_judged_in_the_lease_worktree(self):
        repo = Repo(self)
        other = make_tmp_dir(self) / "lane"
        subprocess.run(["git", "-C", str(repo.root), "worktree", "add", "-q", "-b", "lane", str(other)],
                       check=True, capture_output=True, env=git_env())
        packet = other / PACKET.format(task="t1")
        packet.parent.mkdir(parents=True)
        packet.write_text(packet_text(), encoding="utf-8")
        started = subprocess.run([sys.executable, "-m", "oah", "start", "--task", "t1", "--agent", "claude",
                                  "--packet", PACKET.format(task="t1")], cwd=other, capture_output=True, text=True,
                                 encoding="utf-8", env=repo.env, timeout=120)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(repo.status()["t1"]["intent_state"], "current")
        self.assertIn("state=current", repo.oah("intent", "show", "t1").stdout)


class FinishTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)

    def bound(self, **packet) -> None:
        self.repo.write_packet(packet_text(**packet))
        started = self.repo.start_packet()
        self.assertEqual(started.returncode, 0, started.stderr)
        (self.repo.root / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

    def finish(self, *extra: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return self.repo.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", *extra, stdin=stdin)

    def test_finish_without_binding_is_unchanged(self):
        self.repo.ok("start", "--task", "t1", "--agent", "claude", "--scope", "src")
        (self.repo.root / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertNotIn("intent", self.repo.events()[-1])

    def test_finish_checks_return_lines(self):
        self.bound()
        missing = self.finish()
        self.assertEqual(missing.returncode, 1)
        self.assertTrue(missing.stderr.startswith("OAH_INTENT_RETURN_INVALID"), missing.stderr)
        self.assertIn("--return", missing.stderr)
        parsed = intent.parse(self.repo.path().read_text(encoding="utf-8"))
        bad = self.repo.returned(acs={"AC1": "met", "AC3": "met"}).replace(parsed.digest, "sha256:" + "0" * 64)
        bad_file = make_tmp_dir(self) / "return.md"
        bad_file.write_text(bad, encoding="utf-8")
        refused = self.finish("--return", str(bad_file))
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_INTENT_RETURN_INVALID"), refused.stderr)
        for expected in ("does not match the bound intent", "the AC2 line is missing", "AC3 is not a success"):
            self.assertIn(expected, refused.stderr)
        self.assertIn("t1", self.repo.status())
        valid = self.repo.returned(acs={"AC1": "met", "AC2": "deferred"}).replace("\n", "\r\n")
        finished = self.finish("--return", "-", stdin=valid)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("OAH_LEASE_FINISHED task=t1", finished.stdout)
        self.assertIn("intent=t1@r1", finished.stdout)
        event = self.repo.events()[-1]
        self.assertEqual(event["intent"], {"line": parsed.line(), "acs": {"AC1": "met", "AC2": "deferred"}})

    def test_finish_refuses_stale_binding(self):
        self.bound()
        self.repo.write_packet(packet_text(acs=("changed goal",)))
        refused = self.finish("--return", "-", stdin=self.repo.returned())
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_INTENT_NOT_CURRENT"), refused.stderr)
        self.assertIn("oah start --task t1", refused.stderr)

    def test_finish_exempts_only_the_current_bound_packet(self):
        self.bound()
        notes = self.repo.path().parent / "notes.md"
        notes.write_text("scratch\n", encoding="utf-8")
        refused = self.finish("--return", "-", stdin=self.repo.returned())
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_SCOPE_EXCEEDED"), refused.stderr)
        self.assertIn(".oah/tasks/t1/notes.md", refused.stderr)
        self.assertNotIn("PACKET.md", refused.stderr)
        notes.unlink()
        finished = self.finish("--return", "-", stdin=self.repo.returned())
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("changed=1", finished.stdout)


if __name__ == "__main__":
    unittest.main()
