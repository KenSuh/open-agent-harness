# 의도 결속 배선 검사 — 실제 oah 명령으로 start --packet 결속·재결속, status 의 네 상태, finish 의 반환 줄 검사를 끝에서 끝까지 본다.
from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
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

    def test_rebind_applies_gates_and_finish_enforces_them(self):
        self.repo.write_packet(packet_text())
        self.repo.ok("start", "--task", "t1", "--agent", "claude", "--scope", "src")
        ledger = self.repo.root / "checks" / "GATES.md"
        ledger.parent.mkdir()
        ledger.write_text("- [ ] G1: the owner accepts the result\n  EVIDENCE: pending\n", encoding="utf-8")
        rebound = self.repo.start_packet("t1", "--gates", "checks/GATES.md")
        self.assertEqual(rebound.returncode, 0, rebound.stderr)
        self.assertEqual(self.repo.status()["t1"]["gates"], ["checks/GATES.md"])
        self.assertEqual(self.repo.events()[-1]["gates"], ["checks/GATES.md"])
        # A later start without --gates preserves the declaration.
        self.assertEqual(self.repo.start_packet().returncode, 0)
        self.assertEqual(self.repo.status()["t1"]["gates"], ["checks/GATES.md"])
        finished = self.repo.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done",
                                 "--return", "-", "--allow-structural", stdin=self.repo.returned())
        self.assertEqual(finished.returncode, 1, finished.stdout + finished.stderr)
        self.assertIn("OAH_GATES_UNMET", finished.stderr)
        self.assertIn("t1", self.repo.status())

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
    def test_task_commands_read_the_custom_bound_source(self):
        repo = Repo(self)
        custom = repo.root / "plans" / "T1.md"
        custom.parent.mkdir()
        custom.write_text(packet_text(), encoding="utf-8")
        repo.ok("start", "--task", "t1", "--agent", "claude", "--packet", "plans/T1.md")
        for decoy in (False, True):
            if decoy:
                repo.write_packet(packet_text(ident="decoy"))
            for command in ("show", "check"):
                with self.subTest(decoy=decoy, command=command):
                    result = repo.ok("intent", command, "t1")
                    self.assertIn("state=current", result.stdout)
                    self.assertIn("intent=t1 revision=1 status=accepted", result.stdout)
                    self.assertIn("source=plans/T1.md", result.stdout)
                    self.assertNotIn("intent=decoy", result.stdout)
        explicit = repo.ok("intent", "show", "--file", PACKET.format(task="t1"))
        self.assertIn("intent=decoy", explicit.stdout)
        self.assertIn("source=.oah/tasks/t1/PACKET.md", explicit.stdout)
        explicit = repo.oah("intent", "check", "--file", PACKET.format(task="t1"))
        self.assertEqual(explicit.returncode, 1)
        self.assertIn("intent=decoy", explicit.stdout)
        self.assertIn("OAH_INTENT_ID_CHANGED", explicit.stdout)

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
        for command in ("show", "check"):
            result = repo.ok("intent", command, "t1")
            self.assertIn("state=current", result.stdout)
            self.assertIn("intent=t1 revision=1 status=accepted", result.stdout)


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

    def test_finish_rechecks_binding_after_reading_stdin(self):
        for rebind in (False, True):
            with self.subTest(rebind=rebind):
                self.repo = Repo(self)
                self.bound()
                old_return = self.repo.returned()
                old_id = self.repo.status()["t1"]["id"]
                ready = make_tmp_dir(self) / "reading-return"
                script = (
                    "import pathlib, sys\n"
                    "from oah import cli, lease_cli\n"
                    "read_return = lease_cli._read_return\n"
                    "def reading(raw):\n"
                    "    pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
                    "    return read_return(raw)\n"
                    "lease_cli._read_return = reading\n"
                    "raise SystemExit(cli.main(sys.argv[2:]))\n")
                child = subprocess.Popen([sys.executable, "-c", script, str(ready), "finish", "--task", "t1",
                                          "--agent", "claude", "--summary", "done", "--return", "-"],
                                         cwd=self.repo.root, env=self.repo.env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 15
                    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), "finish did not reach stdin reading")
                    self.repo.write_packet(packet_text(revision=2))
                    if rebind:
                        self.assertEqual(self.repo.start_packet().returncode, 0)
                    out, err = child.communicate(old_return.encode("utf-8"), timeout=30)
                    self.assertEqual(child.returncode, 1, (out + err).decode("utf-8"))
                    self.assertIn(b"OAH_INTENT_NOT_CURRENT", err)
                    self.assertEqual(self.repo.status()["t1"]["id"], old_id)
                    self.assertFalse(any(e["event"] == "lease_finished" for e in self.repo.events()))
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)

    def test_finish_rechecks_binding_after_gate_evaluation(self):
        for rebind in (False, True):
            with self.subTest(rebind=rebind):
                self.repo = Repo(self)
                self.bound()
                old_return = self.repo.returned()
                script = (
                    "import os, pathlib, sys\n"
                    "from oah import cli, intent\n"
                    "from oah.gates import guard\n"
                    "from oah.lease import Store\n"
                    "from oah.paths import repo_info, state_dir\n"
                    "evaluate = guard.evaluate\n"
                    "def changed(*args, **kwargs):\n"
                    "    report = evaluate(*args, **kwargs)\n"
                    "    info = repo_info(pathlib.Path.cwd())\n"
                    "    source = '.oah/tasks/t1/PACKET.md'\n"
                    "    path = info.root / source\n"
                    "    text = path.read_text(encoding='utf-8').replace('Revision: 1', 'Revision: 2')\n"
                    "    path.write_text(text, encoding='utf-8')\n"
                    "    if sys.argv[1] == 'rebind':\n"
                    "        Store(state_dir(info, os.environ)).rebind(task='t1', agent='claude', ttl_hours=1,\n"
                    "                                                       binding=intent.parse(text).bind(source))\n"
                    "    return report\n"
                    "guard.evaluate = changed\n"
                    "raise SystemExit(cli.main(sys.argv[2:]))\n")
                result = subprocess.run([sys.executable, "-c", script, "rebind" if rebind else "edit", "finish",
                                         "--task", "t1", "--agent", "claude", "--summary", "done", "--return", "-"],
                                        cwd=self.repo.root, env=self.repo.env, input=old_return, capture_output=True,
                                        text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                code = "OAH_LEASE_CHANGED" if rebind else "OAH_INTENT_NOT_CURRENT"
                self.assertIn(code, result.stderr)
                self.assertIn("t1", self.repo.status())
                self.assertFalse(any(e["event"] == "lease_finished" for e in self.repo.events()))


def accept(text: str, *, revision: int = 1, goals: tuple[str, ...] = ("the report lists every file",
                                                                       "the digest is stable")) -> str:
    """A scaffolded packet with its intent filled in and accepted."""
    conditions = "\n".join(f"- AC{n}: {goal}" for n, goal in enumerate(goals, start=1))
    text = (text.replace("<what is wrong or missing today>", "Reports miss files.")
            .replace("<what is true when this task is done>", "Every changed file is listed.")
            .replace("- AC1: <checkable condition>", conditions)
            .replace('Status: draft            # becomes "accepted" when you approve it', "Status: accepted"))
    return text.replace("Revision: 1\n", f"Revision: {revision}\n", 1)


def toml_list(argv: list[str]) -> str:
    return "[" + ", ".join(json.dumps(part) for part in argv) + "]"


class EndToEndTest(unittest.TestCase):
    """Every success condition through the real CLI and the real modules, with no fake in between."""

    def test_packet_critical_axes_reach_delivered_skills(self):
        repo = Repo(self)
        for name in ("unlazy", "writing-plans"):
            folder = repo.home / ".claude" / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")
        config = repo.root / ".oah" / "config.toml"
        config.parent.mkdir()
        fake_claude = [sys.executable, str(FAKES / "fake_claude.py")]
        config.write_text(f"[engines.claude]\ncommand = {toml_list(fake_claude)}\n", encoding="utf-8")
        record = make_tmp_dir(self) / "record.json"
        repo.env["FAKE_RECORD"] = str(record)
        repo.ok("packet", "new", "t1", "--scope", "src", "--kind", "security", "--intensity", "max",
                "--reasoning", "xhigh", "--risk", "critical")
        path = repo.path()
        path.write_text(accept(path.read_text(encoding="utf-8")), encoding="utf-8")
        self.assertEqual(repo.start_packet().returncode, 0)
        for overrides, expected_risk in (((), "critical"), (("--risk", "normal"), "normal"),
                                         (("--tier", "low"), "critical")):
            with self.subTest(overrides=overrides):
                called = repo.ok("call", "claude", "--stage", "initial", "--task", "t1", "--json", *overrides)
                data = json.loads(called.stdout)
                self.assertEqual((data["tier"], data["tier_source"]),
                                 ("low", "flag") if "--tier" in overrides else ("max", "packet"))
                run = repo.root / ".git" / "oah" / "runs" / "t1" / data["run_id"]
                selection = json.loads((run / "skills.json").read_text(encoding="utf-8"))
                self.assertEqual((selection["kind"], selection["risk"]), ("security", expected_risk))
                self.assertEqual(data["axes"], {"kind": "security", "intensity": "max", "reasoning": "xhigh",
                                                "risk": expected_risk})
                prompt = json.loads(record.read_text(encoding="utf-8"))["stdin"]
                chosen = {skill["id"] for skill in selection["skills"]}
                for name in ("unlazy", "writing-plans"):
                    self.assertEqual(name in chosen, expected_risk == "critical")
                    if expected_risk == "critical":
                        self.assertIn(f"- `{name}` (user):", prompt)

    def test_end_to_end_bind_stale_rebind_finish(self):
        repo = Repo(self)
        created = repo.ok("packet", "new", "t1", "--scope", "src")
        self.assertIn("OAH_PACKET_CREATED .oah/tasks/t1/PACKET.md", created.stdout)
        draft = repo.start_packet()
        self.assertEqual(draft.returncode, 1)
        self.assertTrue(draft.stderr.startswith("OAH_INTENT_NOT_ACCEPTED"), draft.stderr)

        path = repo.path()
        path.write_text(accept(path.read_text(encoding="utf-8")), encoding="utf-8")
        self.assertIn("intent=t1@r1", repo.ok(*shlex.split(briefs.lease_command("t1", "claude", PACKET.format(
            task="t1")))[1:]).stdout)
        self.assertEqual(repo.status()["t1"]["intent_state"], "current")
        self.assertIn("state=current", repo.ok("intent", "show", "t1").stdout)

        accepted = path.read_text(encoding="utf-8")
        path.write_text(accepted.replace("the digest is stable", "the digest never changes"), encoding="utf-8")
        self.assertEqual(repo.status()["t1"]["intent_state"], "stale")
        refused = repo.oah("call", "codex", "--stage", "review", "--task", "t1", "--tier", "low")
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_INTENT_NOT_CURRENT"), refused.stderr)
        self.assertIn("is stale", refused.stderr)

        path.write_text(accept(accepted.replace("Status: accepted", 'Status: draft            # becomes "accepted" '
                                                "when you approve it"), revision=2,
                               goals=("the report lists every file", "the digest never changes")), encoding="utf-8")
        rebound = repo.start_packet()
        self.assertEqual(rebound.returncode, 0, rebound.stderr)
        self.assertIn("OAH_INTENT_REBOUND task=t1 intent=t1@r2", rebound.stdout)
        self.assertEqual(repo.status()["t1"]["intent_state"], "current")

        (repo.root / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
        finish = ("finish", "--task", "t1", "--agent", "claude", "--summary", "done", "--return", "-")
        invalid = repo.oah(*finish, stdin=repo.returned(acs={"AC1": "met"}))
        self.assertEqual(invalid.returncode, 1)
        self.assertTrue(invalid.stderr.startswith("OAH_INTENT_RETURN_INVALID"), invalid.stderr)
        self.assertIn("the AC2 line is missing", invalid.stderr)
        self.assertIn("t1", repo.status())
        finished = repo.oah(*finish, stdin=repo.returned())
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("OAH_LEASE_FINISHED task=t1 agent=claude changed=1", finished.stdout)
        self.assertEqual(repo.events()[-1]["intent"]["acs"], {"AC1": "met", "AC2": "met"})

    def test_call_brief_carries_skills_intent_and_footer(self):
        repo = Repo(self)
        for name in ("test-driven-development", "receiving-code-review"):
            folder = repo.home / ".claude" / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")
        codex_home = make_tmp_dir(self)
        (codex_home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-test-a", "is_default": True,
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "max")]}]}),
            encoding="utf-8")
        fake_codex = [sys.executable, str(FAKES / "fake_codex.py")]
        (repo.root / ".oah").mkdir()
        (repo.root / ".oah" / "config.toml").write_text(f"[engines.codex]\ncommand = {toml_list(fake_codex)}\n",
                                                        encoding="utf-8")
        record = make_tmp_dir(self) / "record.json"
        repo.env.update(CODEX_HOME=str(codex_home), FAKE_RECORD=str(record))
        path = repo.write_packet(packet_text().replace("\n", "\r\n"))
        repo.ok("start", "--task", "t1", "--agent", "claude", "--packet", PACKET.format(task="t1"))
        called = repo.oah("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low", "--json")
        self.assertEqual(called.returncode, 0, called.stderr)
        self.assertIn("OAH_SKILLS selected=test-driven-development,receiving-code-review "
                      "rerank=unused(not-enabled)", called.stderr)
        data = json.loads(called.stdout)
        common = subprocess.run(["git", "-C", str(repo.root), "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], capture_output=True, text=True, check=True).stdout.strip()
        prompt = (pathlib.Path(common) / "oah" / "runs" / "t1" / data["run_id"] / "prompt.md").read_bytes()
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["stdin"].encode("utf-8"), prompt)
        text = prompt.decode("utf-8")
        section = briefs.raw_section(path.read_bytes().decode("utf-8"))
        self.assertIn("\r\n", section)
        self.assertEqual(text.count(section), 1)
        self.assertTrue(text.endswith(intent.FOOTER))
        self.assertEqual(sum(line == intent.FOOTER_MARKER for line in text.replace("\r\n", "\n").split("\n")), 1)
        skills = text.split("## Skills\n", 1)[1].split(intent.BEGIN, 1)[0]
        self.assertIn("Skill order: rerank=unused(not-enabled)", skills)
        self.assertIn("- `test-driven-development` (user): `~/.claude/skills/test-driven-development/SKILL.md`",
                      skills)
        self.assertIn("  - why: code changes are written test-first (kind implementation, stage correction)", skills)
        self.assertIn("  - why: corrections check each review finding before acting on it (stage correction)", skills)
        self.assertIn("- Lease: before editing, run exactly `oah start --task t1 --agent codex --packet "
                      ".oah/tasks/t1/PACKET.md`", text)

    def test_packet_new_and_preflight_never_fetch_for_impact(self):
        repo = Repo(self)
        remote = make_tmp_dir(self) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        subprocess.run(["git", "-C", str(repo.root), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(repo.root), "push", "-q", "origin", "main"], check=True, capture_output=True)
        other = make_tmp_dir(self) / "other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
        git_commit(other, {"src/later.py": "y = 1\n"}, "moves the default branch")
        subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True, capture_output=True)

        def tracking() -> tuple[str, bool]:
            ref = subprocess.run(["git", "-C", str(repo.root), "rev-parse", "refs/remotes/origin/main"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            return ref, (repo.root / ".git" / "FETCH_HEAD").exists()

        before = tracking()
        created = repo.ok("packet", "new", "t2", "--scope", "src")
        self.assertTrue(any(line.startswith("impact=map ") for line in created.stdout.splitlines()), created.stdout)
        checked = repo.oah("preflight", "--offline", "--touch", "src/app.py")
        self.assertIn("impact: ", checked.stdout)
        self.assertRegex(checked.stdout, r"\n {10}map (fresh|refreshed) at [0-9a-f]{12}\n")
        self.assertEqual(tracking(), before)
        self.assertIn("map: fresh", repo.ok("impact", "status").stdout)


if __name__ == "__main__":
    unittest.main()
