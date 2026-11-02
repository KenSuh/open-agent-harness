# SessionStart 훅 검사 — 작업자(OAH_WORKER=1)에게는 아무것도 출력하지 않고, 아니면 활성 리스·최근 위키 기록·다음 할 일을 짧게 알리며, 저장소 밖이나 오류에도 늘 0 으로 끝나는지 본다.
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import secrets
import subprocess
import sys
import unittest

from unittest import mock

from oah import hook_session_start, hooks, init, scoring
from oah.atomic import StorageError
from oah.lease import Store, utc_now
from oah.models import Catalog, Entry, ModelInfo
from oah.paths import repo_info, state_dir
from tests.helpers import git_commit, make_git_repo, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
LOG = ("# Log\n\n<!-- OAH:ARCHIVE_INDEX:BEGIN -->\n<!-- OAH:ARCHIVE_INDEX:END -->\n\n<!-- OAH:LOG_INSERT_BELOW -->\n\n"
       "## 2026-10-25 Task two\n\nSecond.\n\n## 2026-10-24 Task one\n\nFirst.\n\n## 2026-10-23 Wiki created\n\nStart.\n")


def run_hook(cwd: pathlib.Path, *, worker: bool, payload: dict | None = None, state: pathlib.Path | None = None,
             env_extra: dict | None = None):
    env = {key: value for key, value in os.environ.items() if key not in ("OAH_WORKER", "OAH_STATE_DIR")}
    env.update(PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1", **(env_extra or {}))
    if worker:
        env["OAH_WORKER"] = "1"
    if state is not None:
        env["OAH_STATE_DIR"] = str(state)
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return subprocess.run([sys.executable, "-m", "oah", "hook", "session-start"], cwd=cwd, input=data,
                          capture_output=True, env=env, timeout=60)


class SessionStartTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"README.md": "hello\n", "docs/wiki/log.md": LOG}, "first")
        self.info = repo_info(self.repo)
        self.state = state_dir(self.info, {})

    def lease(self, task: str, scope: list[str], *, stale: bool = False) -> None:
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
        store = Store(self.state, now=(lambda: past) if stale else utc_now)
        store.start(task=task, agent="claude", scope=scope, note="", ttl_hours=1.0, info=self.info)

    def test_worker_prints_nothing(self):
        self.lease("t1", ["src"])
        result = run_hook(self.repo, worker=True, payload={"cwd": str(self.repo), "source": "startup"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_credentials_in_log_titles_are_omitted_from_session_guidance(self):
        for prefix in ("s" + "k-", "gl" + "pat-"):
            credential = prefix + secrets.token_hex(20)
            (self.repo / "docs/wiki/log.md").write_bytes(
                LOG.replace("Task two", "Private " + credential).encode("utf-8"))
            direct = "\n".join(hook_session_start.lines(self.repo, {}))
            result = run_hook(self.repo, worker=False)
            self.assertFalse(credential in direct, "guidance exposed a credential")
            self.assertFalse(credential.encode("ascii") in result.stdout + result.stderr,
                             "hook output exposed a credential")
            self.assertEqual(result.returncode, 0)
            self.assertIn("OAH_SESSION_START_UNSAFE", direct)
            self.assertIn(b"OAH_SESSION_START_UNSAFE", result.stdout)
            self.assertIn(b"2026-10-24 Task one", result.stdout)
            self.assertEqual(result.stderr, b"")

    def test_credentials_in_lease_fields_are_omitted(self):
        credential = "s" + "k-" + secrets.token_hex(20)
        self.lease("private", ["src/" + credential])
        text = "\n".join(hook_session_start.lines(self.repo, {}))
        self.assertFalse(credential in text, "lease guidance exposed a credential")
        self.assertIn("OAH_SESSION_START_UNSAFE", text)

    def test_provider_and_unknown_tokens_are_omitted_from_session_guidance(self):
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        for prefix, size in (("AI" + "za", 35), ("sk" + "_live_", 24), ("", 20)):
            credential = prefix + "".join(secrets.SystemRandom().sample(alphabet, size))
            (self.repo / "docs/wiki/log.md").write_bytes(
                LOG.replace("Task two", "Integration " + credential).encode("utf-8"))
            direct = "\n".join(hook_session_start.lines(self.repo, {}))
            result = run_hook(self.repo, worker=False)
            self.assertFalse(credential in direct, "guidance exposed a credential")
            self.assertFalse(credential.encode("ascii") in result.stdout + result.stderr,
                             "hook output exposed a credential")
            self.assertIn("OAH_SESSION_START_UNSAFE", direct)
            self.assertIn(b"OAH_SESSION_START_UNSAFE", result.stdout)
            self.assertIn(b"2026-10-24 Task one", result.stdout)
            self.assertEqual((result.returncode, result.stderr), (0, b""))

    def test_prose_and_short_hex_ids_remain_in_session_guidance(self):
        (self.repo / "docs/wiki/log.md").write_bytes(
            LOG.replace("Task two", "Project guidance a1b2c3d4e5f6").encode("utf-8"))
        text = "\n".join(hook_session_start.lines(self.repo, {}))
        self.assertIn("Project guidance a1b2c3d4e5f6", text)
        self.assertNotIn("OAH_SESSION_START_UNSAFE", text)


    def test_worker_prints_nothing_even_when_state_is_broken(self):
        self.state.mkdir(parents=True)
        (self.state / "leases.json").write_text("{broken", encoding="utf-8")
        result = run_hook(self.repo, worker=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_guidance_lists_leases_log_and_next_steps(self):
        self.lease("t1", ["src/app.py"])
        self.lease("t0", ["docs"], stale=True)
        text = "\n".join(hook_session_start.lines(self.repo, {}))
        self.assertIn("AGENTS.md", text)
        t1 = next(line for line in text.splitlines() if "task=t1" in line)
        self.assertIn("agent=claude", t1)
        self.assertIn("scope=src/app.py", t1)
        self.assertNotIn("STALE", t1)
        t0 = next(line for line in text.splitlines() if "task=t0" in line)
        self.assertIn("STALE", t0)
        self.assertIn("2026-10-25 Task two", text)
        self.assertIn("2026-10-24 Task one", text)
        self.assertNotIn("Wiki created", text)  # only the newest entries
        self.assertIn("oah start", text)
        self.assertLessEqual(len(text.splitlines()), 12)

    def test_long_scopes_and_many_leases_stay_short(self):
        for number in range(7):
            self.lease(f"t{number}", [f"area{number}/{name}" for name in "abcdef"])
        found = hook_session_start.lines(self.repo, {})
        rows = [line for line in found if line.startswith("- task=")]
        self.assertEqual(len(rows), 5)
        self.assertIn("scope=area0/a,area0/b,area0/c,+3 more", rows[0])
        self.assertTrue(any(line.startswith("- and 2 more") for line in found))

    def test_subprocess_prints_guidance_for_the_payload_cwd(self):
        self.lease("t1", ["src"])
        elsewhere = make_tmp_dir(self)
        result = run_hook(elsewhere, worker=False, payload={"cwd": str(self.repo / "docs")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("task=t1", result.stdout.decode("utf-8"))
        self.assertEqual(result.stderr, b"")

    def test_no_leases_and_no_wiki(self):
        git_commit(self.repo, {"docs/wiki/log.md": None}, "drop the log")
        text = "\n".join(hook_session_start.lines(self.repo, {}))
        self.assertIn("Active leases: none", text)
        self.assertNotIn("wiki log", text.lower())

    def test_wiki_dir_from_config(self):
        git_commit(self.repo, {".oah/config.toml": '[wiki]\ndir = "notes"\n', "notes/log.md": LOG.replace(
            "Task two", "Elsewhere"), "docs/wiki/log.md": None}, "move the wiki")
        text = "\n".join(hook_session_start.lines(self.repo, {}))
        self.assertIn("2026-10-25 Elsewhere", text)

    def test_outside_a_repository_prints_nothing(self):
        plain = make_tmp_dir(self)  # the test temp folder may sit inside a checkout: stop git's search there
        result = run_hook(plain, worker=False, env_extra={"GIT_CEILING_DIRECTORIES": str(plain.parent)})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_errors_never_fail(self):
        self.state.mkdir(parents=True)
        (self.state / "leases.json").write_text("{broken", encoding="utf-8")
        (self.repo / "docs" / "wiki" / "log.md").write_bytes(b"no marker \xff\n")
        result = run_hook(self.repo, worker=False)
        self.assertEqual(result.returncode, 0)
        out = result.stdout.decode("utf-8")
        self.assertIn("Active leases: unreadable", out)
        self.assertIn("AGENTS.md", out)

    def test_hook_is_installable(self):
        self.assertTrue(hooks.hook_available("session-start"))
        change = hooks.install("session-start", self.repo, dry_run=True)
        self.assertIn("oah hook session-start", change.diff)


class AgentFileHookTest(unittest.TestCase):
    """The hook keeps the subagent files oah generated on the current model choice, and nothing else (AC6)."""

    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"README.md": "hello\n"}, "first")
        self.info = repo_info(self.repo)
        self.state = state_dir(self.info, {})
        self.agents = self.repo / ".claude" / "agents"
        self.agents.mkdir(parents=True)
        Catalog(self.state)._save(Entry("claude", "2.1.316", "2026-11-02T00:00:00Z",
                                        (ModelInfo("sonnet", ("low", "medium", "high", "max")),), "claude --help"))
        data = scoring.bundled()
        self.scores = f"(scores {data['source']} {data['date']})"

    def write(self, tier: str, model: str, *, newline: str = "\n") -> pathlib.Path:
        path = self.agents / f"oah-{tier}.md"
        path.write_bytes(init.agent_text(tier, model, tier).replace(b"\n", newline.encode("ascii")))
        return path

    def refresh(self) -> list[str]:
        return hook_session_start.agent_lines(self.info, {})

    def test_a_generated_stale_file_is_rewritten_with_one_line(self):
        path = self.write("max", "opus")
        self.assertEqual(self.refresh(), [f"oah: updated .claude/agents/oah-max.md: model opus -> sonnet {self.scores}"])
        self.assertEqual(path.read_bytes(), init.agent_text("max", "sonnet", "max"))
        self.assertEqual(self.refresh(), [], "a current file is left alone")

    def test_windows_newlines_are_recognised_and_kept(self):
        path = self.write("high", "opus", newline="\r\n")
        self.assertEqual(len(self.refresh()), 1)
        self.assertEqual(path.read_bytes(), init.agent_text("high", "sonnet", "high").replace(b"\n", b"\r\n"))

    def test_an_edited_file_is_left_alone(self):
        for change in (lambda data: data + b"- My own rule.\n", lambda data: data.replace(b"model: opus", b"model: haiku")):
            with self.subTest():
                path = self.write("max", "opus")
                edited = change(path.read_bytes())
                path.write_bytes(edited)
                self.assertEqual(self.refresh(), [])
                self.assertEqual(path.read_bytes(), edited)

    def test_a_missing_file_is_never_created(self):
        self.write("max", "opus")
        self.refresh()
        self.assertEqual(sorted(path.name for path in self.agents.iterdir()), ["oah-max.md"])
        self.agents.joinpath("oah-max.md").unlink()
        self.agents.rmdir()
        self.assertEqual(self.refresh(), [])
        self.assertFalse(self.agents.exists())

    @unittest.skipIf(os.name == "nt", "creating a symlink needs a privilege on Windows")
    def test_nothing_is_written_through_a_linked_folder(self):
        elsewhere = make_tmp_dir(self) / "shared-agents"
        elsewhere.mkdir()
        path = elsewhere / "oah-max.md"
        path.write_bytes(init.agent_text("max", "opus", "max"))
        self.agents.rmdir()
        self.agents.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.refresh(), [])
        self.assertEqual(path.read_bytes(), init.agent_text("max", "opus", "max"))

    def test_a_write_failure_prints_one_plain_line(self):
        self.write("max", "opus")
        self.write("low", "haiku")
        failure = StorageError(self.agents / "oah-low.md", PermissionError("read-only"))
        with mock.patch.object(hook_session_start, "write_text_atomic", side_effect=failure):
            found = self.refresh()
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("oah: could not update"), found[0])
        self.assertIn("oah doctor", found[0])

    @unittest.skipIf(os.name == "nt", "a read-only folder does not stop writes on Windows")
    def test_the_hook_exits_0_when_the_folder_is_read_only(self):
        path = self.write("max", "opus")
        before = path.read_bytes()
        os.chmod(self.agents, 0o500)
        self.addCleanup(os.chmod, self.agents, 0o755)
        result = run_hook(self.repo, worker=False)
        self.assertEqual(result.returncode, 0)
        failures = [line for line in result.stdout.decode("utf-8").splitlines() if line.startswith("oah: could not")]
        self.assertEqual(len(failures), 1, result.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_no_subprocess_runs(self):
        self.write("max", "opus")
        forbidden = AssertionError("the subagent refresh runs no subprocess")
        with mock.patch("subprocess.Popen", side_effect=forbidden), mock.patch("os.system", side_effect=forbidden):
            self.assertEqual(len(self.refresh()), 1)

    def test_a_dispatched_worker_changes_nothing(self):
        path = self.write("max", "opus")
        before = path.read_bytes()
        result = run_hook(self.repo, worker=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(path.read_bytes(), before)

    def test_a_session_shows_the_update(self):
        path = self.write("max", "opus")
        result = run_hook(self.repo, worker=False)
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertIn(f"oah: updated .claude/agents/oah-max.md: model opus -> sonnet {self.scores}".encode("utf-8"),
                      result.stdout)
        self.assertEqual(path.read_bytes(), init.agent_text("max", "sonnet", "max"))


if __name__ == "__main__":
    unittest.main()
