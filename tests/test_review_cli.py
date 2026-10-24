# oah review 명령 검사 — 실제 명령 탐색과 python -m oah 로 사용법, 종료 코드, 더러운 트리, 시리즈 잠금, base 검사, 의도 상태, 영향 갱신 실패, 작업자 거부, JSON 출력을 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import sys
import unittest

from oah.cli import build_parser
from oah.lease import Store
from oah.lockfile import StateLock
from oah.paths import repo_info
from oah.review import receipts
from tests.helpers import git_commit
from tests.test_review_rounds import FAKE_REVIEWER, Project, bug, git, toml_list

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def oah(project: Project, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(project.env, PYTHONPATH=SRC)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "oah", *args], cwd=project.repo, capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=600)


class CliTest(unittest.TestCase):
    def test_binary_pin_executes_the_caller_script_from_another_worktree(self):
        for relative_executable in (False, True):
            with self.subTest(relative_executable=relative_executable):
                project = Project(self, remote=True)
                caller = project.temp_root.parent / "caller"
                executable = pathlib.Path(sys.executable).name
                if relative_executable:
                    try:
                        executable = os.path.relpath(sys.executable, caller)
                    except ValueError:
                        self.skipTest("a relative executable needs the same Windows drive")
                trusted = FAKE_REVIEWER.read_bytes()
                pinned = hashlib.sha256(trusted).hexdigest()
                command = [executable, "tools/reviewer.py", "--script", str(project.script)]
                config = (f"[engines.codex]\ncommand = {toml_list(command)}\n"
                          f'[review]\ncodex_sha256 = "{pinned}"\n')
                project.base = project.commit({".oah/config.toml": config, "tools/reviewer.py": trusted})
                git(project.repo, "worktree", "add", "-q", "--detach", str(caller), project.base)
                task_copy = trusted.replace(b'"fake review"', b'"task copy"').replace(b"0.163.0", b"9.999.0")
                project.commit({"tools/reviewer.py": task_copy})
                env = dict(project.env, PYTHONPATH=SRC)
                env["PATH"] = str(pathlib.Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
                done = subprocess.run([sys.executable, "-m", "oah", "review", "--task", "t1", "--base",
                                       project.base, "--json"], cwd=caller, capture_output=True, text=True,
                                      encoding="utf-8", env=env, timeout=600)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                result = json.loads(done.stdout)
                receipt = result["receipt"]
                self.assertEqual(receipt["summary"], "fake review")
                self.assertEqual(receipt["head"], project.head)
                self.assertEqual(receipt["reviewer"]["cli_path"], os.path.realpath(caller / "tools/reviewer.py"))
                self.assertEqual(receipt["reviewer"]["cli_sha256"], pinned)
                self.assertEqual(receipt["reviewer"]["cli_version"], "0.163.0")
                run = json.loads((pathlib.Path(result["run_dir"]) / "result.json").read_text(encoding="utf-8"))
                self.assertTrue(os.path.isabs(run["command"][0]))
                self.assertEqual(run["command"][1], receipt["reviewer"]["cli_path"])
                self.assertEqual(len(project.calls()), 1)
                self.assertNotEqual(pathlib.Path(project.calls()[0]["cwd"]), project.repo)

    def test_cli_json_redacts_noncanonical_escaped_credentials(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                project = Project(self, remote=True)
                key = "synthetic-" + secrets.token_hex(24)
                project.env["OPENAI_API_KEY"] = key
                step = {"escape_strings": [key], "summary": key}
                if invalid:
                    step["mutate"] = {"verdict": key}
                project.queue(step)
                done = oah(project, "review", "--task", "t1", "--json")
                self.assertEqual(done.returncode, 1 if invalid else 0, done.stderr)
                data = json.loads(done.stdout)
                self.assertEqual(data["code"], "OAH_REVIEW_OUTPUT_INVALID" if invalid else "OAH_REVIEW_CONVERGED")
                self.assertIn("[REDACTED]", done.stdout)
                artifacts = [done.stdout.encode(), done.stderr.encode(),
                             *(path.read_bytes() for path in project.state.rglob("*") if path.is_file())]
                self.assertTrue(all(key.encode() not in artifact for artifact in artifacts))
                self.assertNotIn("OPENAI_API_KEY", project.calls()[0]["env"])

    def test_cli_extra_attempt_recovers_exhausted_range(self):
        project = Project(self)
        project.queue({"raw": "invalid"}, {"raw": "invalid"}, {})
        for _ in range(2):
            self.assertEqual(oah(project, "review", "--task", "t1").returncode, 1)
        self.refused(project, "OAH_REVIEW_ATTEMPTS_EXHAUSTED")
        done = oah(project, "review", "--task", "t1", "--extra-attempt", "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIs(json.loads(done.stdout)["receipt"]["extra_attempt"], True)
        self.assertEqual(project.receipt(1)["head"], project.head)

    def refused(self, project: Project, code: str, *args: str, exit_code: int = 1, **options) -> str:
        calls = len(project.calls())
        done = oah(project, "review", "--task", "t1", *args, **options)
        self.assertEqual(done.returncode, exit_code, done.stdout + done.stderr)
        self.assertTrue(done.stderr.startswith(code + ":"), done.stderr)
        self.assertEqual(len(project.calls()), calls, "a refusal must not call the reviewer")
        return done.stderr

    def test_review_is_discovered_with_usage(self):
        parser = build_parser()
        self.assertIn("review", parser._subparsers._group_actions[0].choices)
        project = Project(self)
        done = oah(project, "review")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--task", done.stderr)
        done = oah(project, "review", "--task", "t1", "--decision", "  ")
        self.assertEqual(done.returncode, 2)
        self.assertIn("OAH_USAGE", done.stderr)
        self.assertIn("--extra-round", oah(project, "review", "--help").stdout)

    def test_cli_round_and_json_output(self):
        project = Project(self)
        project.queue({"findings": [bug()]}, {})
        done = oah(project, "review", "--task", "t1", "--tier", "high")
        self.assertEqual(done.returncode, 1, done.stderr)
        first, *rest = done.stdout.splitlines()
        self.assertTrue(first.startswith("OAH_REVIEW_CHANGES_REQUESTED: R1 of task t1"), first)
        self.assertTrue(any(line.startswith("receipt: R1.json sha256:") for line in rest), rest)
        self.assertTrue(any(line.startswith("R1-F1 P1 ") for line in rest), rest)
        project.correct(1)
        done = oah(project, "review", "--task", "t1", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual((result["code"], result["exit_code"], result["receipt"]["round"]), ("OAH_REVIEW_CONVERGED", 0, 2))
        self.assertTrue(result["receipt_path"].endswith("R2.json"))
        done = oah(project, "review", "--task", "t1")
        self.assertEqual(done.returncode, 0)
        self.assertTrue(done.stdout.startswith("OAH_REVIEW_CONVERGED: R2.json already approved head"), done.stdout)
        self.assertEqual(len(project.calls()), 2)

    def test_cli_refuses_dirty_tree(self):
        project = Project(self)
        (project.repo / "src" / "app.py").write_text("print('edited')\n", encoding="utf-8")
        message = self.refused(project, "OAH_REVIEW_DIRTY_TREE")
        self.assertIn("src/app.py", message)
        (project.repo / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (project.repo / "notes.txt").write_text("untracked files do not block a review\n", encoding="utf-8")
        self.assertEqual(oah(project, "review", "--task", "t1").returncode, 0)

    def test_cli_series_lock_busy(self):
        project = Project(self)
        with StateLock(receipts.lock_path(project.state, "t1", project.base)):
            message = self.refused(project, "OAH_REVIEW_SERIES_BUSY")
        self.assertIn(project.base[:12], message)
        self.assertEqual(oah(project, "review", "--task", "t1").returncode, 0)

    def test_cli_base_must_be_ancestor(self):
        project = Project(self)
        git(project.repo, "checkout", "-q", "main")
        git_commit(project.repo, {"NOTES.md": "main moved\n"}, "main")
        git(project.repo, "checkout", "-q", "feat")
        self.refused(project, "OAH_REVIEW_BASE_INVALID", "--base", "main")
        self.refused(project, "OAH_REVIEW_BASE_INVALID", "--base", "no-such-branch")
        done = oah(project, "review", "--task", "t1", "--base", project.base)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(project.receipt(1)["base"], project.base)

    def test_cli_intent_not_current_states(self):
        packet = ".oah/tasks/t1/PACKET.md"

        def no_lease(project):
            Store(project.state).release(task="t1", agent="claude", reason="test", force=False)

        def unbound(project):
            no_lease(project)
            Store(project.state).start(task="t1", agent="claude", scope=("src",), note="unbound", ttl_hours=1,
                                       info=repo_info(project.repo))

        def missing(project):
            git(project.repo, "rm", "-q", packet)
            project.commit({}, "drop the packet")

        def draft(project):
            path = project.repo / packet
            path.write_text(path.read_text(encoding="utf-8").replace("Status: accepted", "Status: draft"),
                            encoding="utf-8")

        def invalid(project):
            (project.repo / packet).write_text("not a packet at all\n", encoding="utf-8")

        def uncommitted(project):
            git(project.repo, "rm", "-q", "--cached", packet)
            git(project.repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "untrack the packet")

        cases = (("no lease", no_lease, "no active lease"), ("unbound", unbound, "unbound"),
                 ("missing", missing, "missing"), ("draft", draft, "stale"), ("invalid content", invalid, "stale"),
                 ("not committed", uncommitted, "not committed at HEAD"))
        for label, damage, word in cases:
            with self.subTest(label=label):
                project = Project(self)
                damage(project)
                message = self.refused(project, "OAH_INTENT_NOT_CURRENT")
                self.assertIn(word, message)
        project = Project(self)
        self.assertEqual(oah(project, "review", "--task", "t1").returncode, 0)

    def test_cli_impact_stale_refuses(self):
        project = Project(self)
        config = project.repo / ".oah" / "config.toml"
        project.commit({".oah/config.toml": config.read_text(encoding="utf-8") + '[impact]\nsql_dialect = "bogus"\n'})
        message = self.refused(project, "OAH_IMPACT_STALE")
        self.assertIn("sql_dialect", message)

    def test_cli_worker_refused(self):
        project = Project(self)
        self.refused(project, "OAH_REVIEW_WORKER_REFUSED", env_extra={"OAH_WORKER": "1"})
        self.assertFalse((project.state / "reviews").exists())

    def test_cli_config_problems_exit_69(self):
        project = Project(self, config="[review]\nmax_rounds = 0\n")
        message = self.refused(project, "OAH_CONFIG_INVALID", exit_code=69)
        self.assertIn("max_rounds", message)
        self.assertIn("review base", message)

    def test_cli_engine_must_be_codex(self):
        project = Project(self, config='[stages]\nreview = "claude"\n')
        self.refused(project, "OAH_REVIEW_ENGINE_UNSUPPORTED")

    def test_cli_timeout_must_be_a_positive_number(self):
        project = Project(self)
        for value in ("0", "-1", "inf", "nan", "soon"):
            with self.subTest(value=value):
                done = oah(project, "review", "--task", "t1", f"--timeout-seconds={value}")
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("--timeout-seconds", done.stderr)
        self.assertEqual(project.calls(), [])


if __name__ == "__main__":
    unittest.main()
