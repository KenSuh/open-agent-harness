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

from oah import hook_session_start, hooks
from oah.lease import Store, utc_now
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


if __name__ == "__main__":
    unittest.main()
