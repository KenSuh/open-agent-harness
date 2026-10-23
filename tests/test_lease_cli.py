# 리스 명령 검사 — 임시 저장소에서 python -m oah 로 start/status/renew/finish/release/log, 종료 코드, JSON, 워크트리 공유를 본다.
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

from oah import lease_cli
from oah.cli import build_parser
from tests.helpers import directory_alias, git_commit, make_git_repo, make_tmp_dir

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

    def test_finish_cannot_close_replacement_created_during_scan(self):
        original = json.loads(self.start(extra=("--json",)).stdout)
        real_scan = lease_cli._changed_files
        replacement = {}

        def replace_during_scan(lease, info):
            changed = real_scan(lease, info)
            released = oah(self.repo, "release", "--task", "t1", "--agent", "claude", "--reason", "replaced")
            self.assertEqual(released.returncode, 0, released.stderr)
            started = self.start(extra=("--json",))
            self.assertEqual(started.returncode, 0, started.stderr)
            replacement.update(json.loads(started.stdout))
            self.assertNotEqual(replacement["id"], original["id"])
            return changed

        args = build_parser().parse_args(["finish", "--task", "t1", "--agent", "claude", "--summary", "old work"])
        stderr = io.StringIO()
        with patch("oah.lease_cli.pathlib.Path.cwd", return_value=self.repo), \
                patch("oah.lease_cli._changed_files", side_effect=replace_during_scan), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            result = lease_cli.run(args)
        self.assertEqual(result, 1, stderr.getvalue())
        self.assertIn("OAH_LEASE_CHANGED", stderr.getvalue())
        remaining = json.loads(oah(self.repo, "status", "--json").stdout)["leases"]
        self.assertEqual([lease["id"] for lease in remaining], [replacement["id"]])

    def test_alias_scopes_cannot_acquire_the_same_file_twice(self):
        directory_alias(self.repo / "src", self.repo / "link")
        self.assertEqual(self.start("t1", "claude", "src/a.py").returncode, 0)
        for scope in ("link/a.py", str(self.repo / "link" / "a.py")):
            with self.subTest(scope=scope):
                result = self.start("t2", "codex", scope)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("OAH_LEASE_CONFLICT", result.stderr)

    @unittest.skipUnless(os.name == "nt", "native Windows path aliases")
    def test_windows_alias_scope_is_refused(self):
        self.assertEqual(self.start("t1", "claude", "src/a.py").returncode, 0)
        for scope in ("src/a.py.", "src/a.py ", "src./a.py"):
            with self.subTest(scope=scope):
                result = self.start("t2", "codex", scope)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("OAH_SCOPE_INVALID", result.stderr)

    def test_finish_counts_both_sides_of_rename_into_scope(self):
        self.assertEqual(self.start("t1", "claude", "docs").returncode, 0)
        subprocess.run(["git", "-C", str(self.repo), "mv", "src/a.py", "docs/a.py"], check=True)
        result = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "renamed")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("src/a.py", result.stderr)

    def test_finish_counts_staged_change_restored_in_worktree(self):
        self.assertEqual(self.start().returncode, 0)
        target = self.repo / "docs" / "readme.md"
        target.write_text("staged violation\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "docs/readme.md"], check=True)
        target.write_text("hi\n", encoding="utf-8")
        result = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("docs/readme.md", result.stderr)

    def test_finish_counts_committed_change_restored_in_worktree(self):
        self.assertEqual(self.start().returncode, 0)
        git_commit(self.repo, {"docs/readme.md": "committed violation\n"})
        (self.repo / "docs" / "readme.md").write_text("hi\n", encoding="utf-8")
        result = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("docs/readme.md", result.stderr)

    def test_unborn_start_counts_committed_paths_restored_and_removed_from_index(self):
        repo = make_git_repo(self)
        started = oah(repo, "start", "--task", "t1", "--agent", "claude", "--scope", "src")
        self.assertEqual(started.returncode, 0, started.stderr)
        git_commit(repo, {"docs/a.md": "first commit\n"})
        subprocess.run(["git", "-C", str(repo), "rm", "docs/a.md"], check=True, capture_output=True)
        result = oah(repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("docs/a.md", result.stderr)

    def test_finish_with_staged_changes_on_still_unborn_branch(self):
        repo = make_git_repo(self)
        started = oah(repo, "start", "--task", "t1", "--agent", "claude", "--scope", "src")
        self.assertEqual(started.returncode, 0, started.stderr)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "src/a.py"], check=True)
        result = oah(repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_finish_refuses_missing_recorded_worktree(self):
        other = self.repo.parent / "other"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "side", str(other)], check=True)
        started = oah(other, "start", "--task", "t1", "--agent", "claude", "--scope", "src")
        self.assertEqual(started.returncode, 0, started.stderr)
        git_commit(other, {"docs/readme.md": "out of scope\n"})
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", str(other)], check=True)
        result = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("OAH_LEASE_WORKTREE_INVALID", result.stderr)
        self.assertIn(str(other), result.stderr)
        self.assertEqual(len(json.loads(oah(self.repo, "status", "--json").stdout)["leases"]), 1)

    def test_finish_refuses_recorded_path_replaced_by_another_repository(self):
        other = self.repo.parent / "other"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "side", str(other)], check=True)
        started = oah(other, "start", "--task", "t1", "--agent", "claude", "--scope", "src")
        self.assertEqual(started.returncode, 0, started.stderr)
        git_commit(other, {"docs/readme.md": "out of scope\n"})
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", str(other)], check=True)
        replacement = make_git_repo(self)
        git_commit(replacement, {"src/a.py": "a = 1\n", "docs/readme.md": "hi\n"})
        replacement.rename(other)
        result = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("OAH_LEASE_WORKTREE_INVALID", result.stderr)

    def test_finish_refuses_removed_worktree_aliased_to_clean_checkout(self):
        other = self.repo.parent / "other"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "side", str(other)], check=True)
        started = oah(other, "start", "--task", "t1", "--agent", "claude", "--scope", "src")
        self.assertEqual(started.returncode, 0, started.stderr)
        git_commit(other, {"docs/readme.md": "out of scope\n"})
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", str(other)], check=True)
        directory_alias(self.repo, other)
        result = oah(self.repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "done")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("OAH_LEASE_WORKTREE_INVALID", result.stderr)

    def test_naive_timestamp_exits_69(self):
        self.assertEqual(self.start().returncode, 0)
        path = self.repo / ".git" / "oah" / "leases.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["leases"][0]["expires_at"] = "2026-10-23T10:00:00"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = oah(self.repo, "status")
        self.assertEqual(result.returncode, 69, result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_STATE_CORRUPT"), result.stderr)

    def test_storage_failure_exits_69_and_names_path(self):
        # A real, portable storage error: the override's parent is a regular file.
        blocker = self.repo.parent / "not-a-folder"
        blocker.write_text("blocked", encoding="utf-8")
        state = blocker / "state"
        result = oah(self.repo, "start", "--task", "t1", "--agent", "claude", "--scope", "src",
                     env_extra={"OAH_STATE_DIR": str(state)})
        self.assertEqual(result.returncode, 69, result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_STATE_IO"), result.stderr)
        self.assertIn(str(state), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_injected_storage_failures_exit_69(self):
        state = self.repo.parent / "state"
        state.mkdir()
        real_open = open
        real_replace = os.replace
        commands = {
            "read": ["status"],
            "replace": ["start", "--task", "t1", "--agent", "claude", "--scope", "src"],
            "append": ["log", "--task", "t1", "--agent", "claude", "--message", "note"],
        }
        for boundary, command in commands.items():
            with self.subTest(boundary=boundary):
                target = state / ("leases.json" if boundary == "read" else
                                  "events.jsonl" if boundary == "append" else "transaction.json")
                error = PermissionError(errno.EACCES, "denied", str(target)) if boundary != "append" else \
                    OSError(errno.ENOSPC, "disk full", str(target))

                def replace(src, dst):
                    if boundary == "replace":
                        raise error
                    return real_replace(src, dst)

                def open_file(path, mode="r", *args, **kwargs):
                    if boundary == "append" and pathlib.Path(path) == target and "a" in mode:
                        raise error
                    return real_open(path, mode, *args, **kwargs)

                args = build_parser().parse_args(command)
                stderr = io.StringIO()
                with patch("oah.lease_cli.pathlib.Path.cwd", return_value=self.repo), \
                        patch.dict(os.environ, {"OAH_STATE_DIR": str(state)}), \
                        patch("oah.atomic.RETRY_SECONDS", 0), \
                        patch("oah.atomic.os.replace", side_effect=replace), \
                        patch("builtins.open", side_effect=open_file), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    if boundary == "read":
                        with patch("pathlib.Path.read_bytes", side_effect=error):
                            result = lease_cli.run(args)
                    else:
                        result = lease_cli.run(args)
                self.assertEqual(result, 69, stderr.getvalue())
                self.assertTrue(stderr.getvalue().startswith("OAH_STATE_IO"), stderr.getvalue())
                self.assertIn(str(target), stderr.getvalue())

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
