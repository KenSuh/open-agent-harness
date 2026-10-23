# 리스 명령 검사 — 임시 저장소에서 python -m oah 로 start/status/renew/finish/release/log, 종료 코드, JSON, 워크트리 공유를 본다.
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import unittest

from tests.helpers import git_commit, make_git_repo, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def oah(cwd: pathlib.Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=SRC)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "oah", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=60)


class LeaseCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/a.py": "a = 1\n", "docs/readme.md": "hi\n"})

    def start(self, task="t1", agent="claude", *scope: str, extra: tuple = ()) -> subprocess.CompletedProcess:
        args = ["start", "--task", task, "--agent", agent]
        for entry in scope or ("src",):
            args += ["--scope", entry]
        return oah(self.repo, *args, *extra)

    def test_start_status_finish_round_trip(self):
        started = self.start("t1", "claude", "src")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn("OAH_LEASE_STARTED task=t1", started.stdout)
        status = oah(self.repo, "status")
        self.assertIn("t1", status.stdout)
        self.assertIn("claude", status.stdout)
        (self.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
        finished = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("OAH_LEASE_FINISHED task=t1", finished.stdout)
        self.assertIn("no leases", oah(self.repo, "status").stdout)

    def test_conflict_exits_1_with_code(self):
        self.assertEqual(self.start("t1", "claude", "src").returncode, 0)
        refused = self.start("t2", "codex", "src/a.py")
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_LEASE_CONFLICT"), refused.stderr)

    def test_status_creates_nothing(self):
        status = oah(self.repo, "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("no leases", status.stdout)
        self.assertFalse((self.repo / ".git" / "oah").exists())

    def test_second_worktree_sees_leases(self):
        self.assertEqual(self.start("t1", "claude", "src").returncode, 0)
        other = self.repo.parent / "other"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "side", str(other)], check=True)
        status = oah(other, "status", "--json")
        self.assertEqual([lease["task"] for lease in json.loads(status.stdout)["leases"]], ["t1"])
        refused = oah(other, "start", "--task", "t2", "--agent", "codex", "--scope", "src/a.py")
        self.assertEqual(refused.returncode, 1)

    def test_finish_scope_exceeded_exit_1(self):
        self.assertEqual(self.start("t1", "claude", "src").returncode, 0)
        (self.repo / "docs" / "new.md").write_text("new\n", encoding="utf-8")
        refused = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_SCOPE_EXCEEDED"), refused.stderr)
        self.assertIn("docs/new.md", refused.stderr)
        allowed = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done",
                      "--allow-out-of-scope")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("out_of_scope=docs/new.md", allowed.stdout)

    def test_finish_counts_committed_changes(self):
        self.assertEqual(self.start("t1", "claude", "src").returncode, 0)
        git_commit(self.repo, {"docs/readme.md": "changed\n"})
        refused = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("docs/readme.md", refused.stderr)

    def test_renew_release_and_log(self):
        self.assertEqual(self.start("t1", "claude", "src").returncode, 0)
        renewed = oah(self.repo, "renew", "--task", "t1", "--agent", "claude", "--ttl-hours", "1", "--scope", "src",
                      "--scope", "docs")
        self.assertEqual(renewed.returncode, 0, renewed.stderr)
        self.assertEqual(oah(self.repo, "log", "--task", "t1", "--agent", "claude", "--message", "halfway").returncode, 0)
        not_yours = oah(self.repo, "release", "--task", "t1", "--agent", "codex", "--reason", "stuck")
        self.assertEqual(not_yours.returncode, 1)
        self.assertTrue(not_yours.stderr.startswith("OAH_LEASE_NOT_YOURS"))
        forced = oah(self.repo, "release", "--task", "t1", "--agent", "codex", "--reason", "stuck", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        events = (self.repo / ".git" / "oah" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["event"] for line in events],
                         ["lease_started", "lease_renewed", "note", "lease_released"])

    def test_scope_is_normalized(self):
        started = self.start("t1", "claude", "src\\a.py", str(self.repo / "docs"), extra=("--json",))
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(json.loads(started.stdout)["scope"], ["src/a.py", "docs"])
        refused = self.start("t2", "claude", "../outside")
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_SCOPE_INVALID"))

    def test_json_output(self):
        started = self.start("t1", "claude", "src", extra=("--json",))
        lease = json.loads(started.stdout)
        self.assertEqual((lease["task"], lease["agent"], lease["scope"]), ("t1", "claude", ["src"]))
        status = json.loads(oah(self.repo, "status", "--json").stdout)
        self.assertEqual(status["leases"][0]["stale"], False)

    def test_not_a_repo_exits_69(self):
        plain = make_tmp_dir(self)
        result = oah(plain, "status", env_extra={"GIT_CEILING_DIRECTORIES": str(plain.parent)})
        self.assertEqual(result.returncode, 69)
        self.assertTrue(result.stderr.startswith("OAH_NOT_A_REPO"), result.stderr)

    def test_corrupt_state_exits_69(self):
        state = self.repo / ".git" / "oah"
        state.mkdir()
        (state / "leases.json").write_text("{broken", encoding="utf-8")
        result = oah(self.repo, "status")
        self.assertEqual(result.returncode, 69)
        self.assertTrue(result.stderr.startswith("OAH_STATE_CORRUPT"), result.stderr)
        self.assertEqual((state / "leases.json").read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
