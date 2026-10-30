# 위키 정리 뒤 종료 종단 검사 — 실제 oah start·wiki maintain·커밋·finish 를 임시 저장소에서 차례로 돌린다.
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

from oah.wiki import WikiSettings, notes, verify_maintenance
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


class FinishAfterWikiMaintainTest(unittest.TestCase):
    """The real `oah wiki maintain` record must satisfy the real `oah finish` (no fakes)."""

    def setUp(self):
        self.repo = make_git_repo(self)
        files = {f"docs/wiki/{rel}": data for rel, data in notes.scaffold_files(datetime.date(2026, 10, 27)).items()}
        git_commit(self.repo, {"src/a.py": "a = 1\n", **files})
        self.state = make_tmp_dir(self) / "state"
        self.env = dict(os.environ, PYTHONPATH=SRC, OAH_STATE_DIR=str(self.state),
                        OAH_UNLAZY_DIR=str(self.state / "absent"))

    def oah(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "oah", *args], cwd=self.repo, capture_output=True, text=True,
                              encoding="utf-8", env=self.env, timeout=120)

    def ok(self, *args: str) -> subprocess.CompletedProcess:
        result = self.oah(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def finish(self, *extra: str) -> subprocess.CompletedProcess:
        return self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", *extra)

    def commit(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=git_env())
        subprocess.run(["git", "-C", str(self.repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "work"],
                       check=True, env=git_env())

    def last_finish(self) -> dict:
        lines = (self.state / "events.jsonl").read_text(encoding="utf-8").splitlines()
        return [event for event in map(json.loads, lines) if event.get("event") == "lease_finished"][-1]

    def maintain_after_edit(self) -> None:
        self.ok("start", "--task", "t1", "--agent", "claude", "--scope", "src")
        (self.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
        self.ok("wiki", "maintain", "--task", "t1", "--title", "Bump a")
        self.commit()

    def test_finish_after_real_wiki_maintain(self):
        self.maintain_after_edit()
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("OAH_LEASE_FINISHED task=t1 agent=claude changed=1", finished.stdout)
        self.assertNotIn("Traceback", finished.stderr)
        expected = [[rel, hashlib.sha256((self.repo / rel).read_bytes()).hexdigest()]
                    for rel in ("docs/wiki/index.md", "docs/wiki/log.md")]
        self.assertEqual(self.last_finish()["wiki_maintenance"], {"task": "t1", "exempt": expected})

    def test_finish_uses_configured_wiki_directory(self):
        (self.repo / "docs" / "wiki").rename(self.repo / "notes")
        git_commit(self.repo, {".oah/config.toml": '[wiki]\ndir = "notes"\n'})
        self.maintain_after_edit()
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        expected = [[rel, hashlib.sha256((self.repo / rel).read_bytes()).hexdigest()]
                    for rel in ("notes/index.md", "notes/log.md")]
        self.assertEqual(self.last_finish()["wiki_maintenance"], {"task": "t1", "exempt": expected})

    @unittest.skipUnless(os.name != "nt", "POSIX absolute paths")
    def test_finish_uses_absolute_posix_wiki_directory(self):
        self.check_absolute_wiki_directory()

    @unittest.skipUnless(os.name == "nt", "native Windows drive-letter paths")
    def test_finish_uses_absolute_windows_wiki_directory(self):
        self.assertTrue(self.repo.drive.endswith(":"), self.repo)
        self.check_absolute_wiki_directory()

    def check_absolute_wiki_directory(self):
        folder = self.repo / "notes"
        (self.repo / "docs" / "wiki").rename(folder)
        # JSON string escaping is also valid TOML, including native Windows backslashes.
        config = '[wiki]\ndir = ' + json.dumps(str(folder)) + '\n'
        git_commit(self.repo, {".oah/config.toml": config.encode("utf-8")})
        self.maintain_after_edit()
        check = verify_maintenance(self.repo, "t1", settings=WikiSettings(dir=str(folder)), env=self.env)
        self.assertTrue(check.ok, check.problems)
        expected = [[rel, hashlib.sha256((self.repo / rel).read_bytes()).hexdigest()]
                    for rel in ("notes/index.md", "notes/log.md")]
        self.assertEqual(check.outputs, [tuple(pair) for pair in expected])
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("OAH_LEASE_FINISHED task=t1 agent=claude changed=1", finished.stdout)
        self.assertEqual(self.last_finish()["wiki_maintenance"], {"task": "t1", "exempt": expected})

    def test_outside_wiki_directory_refuses_even_with_override(self):
        folder = make_tmp_dir(self) / "notes"
        (self.repo / "docs" / "wiki").rename(folder)
        before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        config = '[wiki]\ndir = ' + json.dumps(str(folder)) + '\n'
        git_commit(self.repo, {".oah/config.toml": config.encode("utf-8")})
        self.ok("start", "--task", "t1", "--agent", "claude", "--scope", "src")
        for extra in ((), ("--allow-missing-wiki-maintenance",)):
            with self.subTest(extra=extra):
                refused = self.finish(*extra)
                self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
                self.assertTrue(refused.stderr.startswith("OAH_WIKI_MISSING"), refused.stderr)
                self.assertNotIn("Traceback", refused.stderr)
        refused = self.oah("wiki", "maintain", "--task", "t1")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertTrue(refused.stderr.startswith("OAH_WIKI_MISSING"), refused.stderr)
        self.assertFalse((self.state / "wiki" / "maintenance.jsonl").exists())
        self.assertEqual({p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}, before)

    def test_finish_records_task_entry_page_but_keeps_exemptions_restricted(self):
        archive = "docs/wiki/log_archive_001.md"
        page = (b"# Log archive 1\n\n<!-- OAH:LOG_INSERT_BELOW -->\n"
                b"## 2026-10-26 Previous task entry\n\n<!-- oah-task: t1 -->\n")
        git_commit(self.repo, {archive: page})
        self.maintain_after_edit()
        check = verify_maintenance(self.repo, "t1", env=self.env)
        self.assertTrue(check.ok, check.problems)
        self.assertIn((archive, hashlib.sha256(page).hexdigest()), check.outputs)
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        expected = [[rel, hashlib.sha256((self.repo / rel).read_bytes()).hexdigest()]
                    for rel in ("docs/wiki/index.md", "docs/wiki/log.md")]
        self.assertEqual(self.last_finish()["wiki_maintenance"], {"task": "t1", "exempt": expected})

    def test_configured_existing_task_archive_still_needs_scope(self):
        (self.repo / "docs" / "wiki").rename(self.repo / "notes")
        archive = "notes/log_archive_01.md"
        page = (b"# Log archive 1\n\n<!-- OAH:LOG_INSERT_BELOW -->\n"
                b"## 2026-10-26 Previous task entry\n\n<!-- oah-task: t1 -->\n")
        git_commit(self.repo, {".oah/config.toml": '[wiki]\ndir = "notes"\n', archive: page})
        self.ok("start", "--task", "t1", "--agent", "claude", "--scope", "src")
        (self.repo / archive).write_bytes(page.replace(b"Previous task entry", b"Edited task entry"))
        self.ok("wiki", "maintain", "--task", "t1")
        self.commit()
        check = verify_maintenance(self.repo, "t1", settings=WikiSettings(dir="notes"), env=self.env)
        self.assertTrue(check.ok, check.problems)
        refused = self.finish()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertTrue(refused.stderr.startswith("OAH_SCOPE_EXCEEDED"), refused.stderr)
        self.assertIn(archive, refused.stderr)
        self.assertNotIn("notes/log.md", refused.stderr)
        self.assertNotIn("notes/index.md", refused.stderr)

    def test_maintain_and_finish_across_archive_99_to_100(self):
        self.check_archive_boundary("docs/wiki")

    def test_configured_maintain_and_finish_across_archive_99_to_100(self):
        (self.repo / "docs" / "wiki").rename(self.repo / "notes")
        self.check_archive_boundary("notes")

    def check_archive_boundary(self, directory):
        git_commit(self.repo, {
            ".oah/config.toml": f'[wiki]\ndir = "{directory}"\nrollover_trigger_bytes = 1500\n'
                                "rollover_target_bytes = 700\n",
            f"{directory}/log_archive_98.md": b"# Log archive 98\n\n<!-- OAH:LOG_INSERT_BELOW -->\n",
        })
        for number in (99, 100):
            task = f"t{number}"
            rel = f"{directory}/log_archive_{number}.md"
            self.ok("start", "--task", task, "--agent", "claude", "--scope", "src")
            (self.repo / "src" / "a.py").write_bytes(f"a = {number}\n".encode("ascii"))
            maintained = self.ok("wiki", "maintain", "--task", task, "--title", f"Work {number}",
                                 "--body", "x" * 1800)
            self.assertIn(f"rollover=log_archive_{number}.md", maintained.stdout)
            self.commit()
            expected = [[path, hashlib.sha256((self.repo / path).read_bytes()).hexdigest()]
                        for path in (f"{directory}/index.md", f"{directory}/log.md", rel)]
            with self.subTest(number=number, step="verify"):
                check = verify_maintenance(self.repo, task, settings=WikiSettings(dir=directory), env=self.env)
                self.assertTrue(check.ok, check.problems)
                self.assertEqual(check.outputs, [tuple(pair) for pair in expected])
            with self.subTest(number=number, step="finish"):
                finished = self.oah("finish", "--task", task, "--agent", "claude", "--summary", "done")
                self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
                self.assertIn(f"OAH_LEASE_FINISHED task={task} agent=claude changed=1", finished.stdout)
                self.assertEqual(self.last_finish()["wiki_maintenance"], {"task": task, "exempt": expected})

    def test_tampered_record_outputs_refuse_by_name(self):
        self.maintain_after_edit()
        records = self.state / "wiki" / "maintenance.jsonl"
        lines = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
        lines[-1]["outputs"] = [[rel, digest] for rel, digest in lines[-1]["outputs"].items()]
        records.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        refused = self.finish()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertTrue(refused.stderr.startswith("OAH_WIKI_MAINTENANCE_MISSING"), refused.stderr)
        self.assertIn("oah wiki maintain --task t1", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        # The override skips the check but exempts nothing, so the wiki edits still need scope.
        overridden = self.finish("--allow-missing-wiki-maintenance")
        self.assertEqual(overridden.returncode, 1, overridden.stdout + overridden.stderr)
        self.assertTrue(overridden.stderr.startswith("OAH_SCOPE_EXCEEDED"), overridden.stderr)
        self.assertIn("docs/wiki/log.md", overridden.stderr)

    def test_json_encoded_nul_path_refuses_and_allows_recorded_override(self):
        self.maintain_after_edit()
        records = self.state / "wiki" / "maintenance.jsonl"
        lines = [json.loads(line) for line in records.read_bytes().splitlines()]
        lines[-1]["outputs"]["docs/wiki/log\x00.md"] = "0" * 64
        data = b"".join((json.dumps(line) + "\n").encode("utf-8") for line in lines)
        self.assertIn(b"\\u0000", data)
        records.write_bytes(data)

        check = verify_maintenance(self.repo, "t1", env=self.env)
        self.assertFalse(check.ok)
        self.assertIsNone(check.outputs)
        self.assertTrue(any(problem.startswith("OAH_WIKI_MAINTENANCE_STALE") for problem in check.problems),
                        check.problems)
        refused = self.finish()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertTrue(refused.stderr.startswith("OAH_WIKI_MAINTENANCE_MISSING"), refused.stderr)
        self.assertIn("OAH_WIKI_MAINTENANCE_STALE", refused.stderr)
        self.assertIn("oah wiki maintain --task t1", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        overridden = self.finish("--allow-missing-wiki-maintenance")
        self.assertEqual(overridden.returncode, 1, overridden.stdout + overridden.stderr)
        self.assertTrue(overridden.stderr.startswith("OAH_SCOPE_EXCEEDED"), overridden.stderr)
        self.assertIn("docs/wiki/log.md", overridden.stderr)

        self.ok("renew", "--task", "t1", "--agent", "claude", "--scope", "src", "--scope", "docs/wiki")
        finished = self.finish("--allow-missing-wiki-maintenance")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("OAH_WIKI_MAINTENANCE_SKIPPED", finished.stdout)
        self.assertNotIn("Traceback", finished.stderr)
        self.assertEqual(self.last_finish()["wiki_maintenance"],
                         {"task": "t1", "override": True, "problems": check.problems})
        self.assertEqual(records.read_bytes(), data)

    def test_json_encoded_surrogate_path_refuses_with_and_without_override(self):
        self.maintain_after_edit()
        records = self.state / "wiki" / "maintenance.jsonl"
        lines = [json.loads(line) for line in records.read_bytes().splitlines()]
        lines[-1]["outputs"]["docs/wiki/\ud800.md"] = "0" * 64
        data = b"".join((json.dumps(line, ensure_ascii=True) + "\n").encode("ascii") for line in lines)
        self.assertIn(b"docs/wiki/\\ud800.md", data)
        records.write_bytes(data)

        for extra, code in (((), "OAH_WIKI_MAINTENANCE_MISSING"),
                            (("--allow-missing-wiki-maintenance",), "OAH_SCOPE_EXCEEDED")):
            with self.subTest(extra=extra):
                result = self.finish(*extra)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertTrue(result.stderr.startswith(code), result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if not extra:
                    self.assertIn("OAH_WIKI_MAINTENANCE_STALE", result.stderr)
                    self.assertIn("oah wiki maintain --task t1", result.stderr)

        check = verify_maintenance(self.repo, "t1", env=self.env)
        self.assertFalse(check.ok)
        self.assertIsNone(check.outputs)
        self.assertTrue(any(problem.startswith("OAH_WIKI_MAINTENANCE_STALE") for problem in check.problems),
                        check.problems)
        self.assertEqual(records.read_bytes(), data)

    def test_disallowed_output_paths_refuse_with_and_without_override(self):
        self.maintain_after_edit()
        records = self.state / "wiki" / "maintenance.jsonl"
        lines = [json.loads(line) for line in records.read_bytes().splitlines()]
        valid = dict(lines[-1]["outputs"])
        paths = ("docs/wiki/log\x00.md", "docs/wiki/\ud800.md", "docs/wiki/\udfff.md",
                 str(self.repo / "src" / "a.py"), "../docs/wiki/log.md", "docs/wiki/../log.md",
                 "docs/wiki/./log.md", "docs//wiki/log.md", "docs\\wiki\\log.md",
                 "C:/docs/wiki/log.md", "C:docs/wiki/log.md", "\\\\server\\wiki\\log.md",
                 "docs/wiki/page.md", "docs/wiki/log.md\n", "docs/wiki/log.md/extra",
                 "docs/wiki/log_archive_1.md", "docs/wiki/log_archive_001.md",
                 "docs/wiki/log_archive_00.md", "docs/wiki/log_archive_000.md",
                 "docs/wiki/log_archive_010.md", "docs/wiki/log_archive_100.md\n", None, 1)
        for path in paths:
            lines[-1]["outputs"] = {**valid, path: "0" * 64}
            # JSON object keys become strings; the property test covers non-str keys before serialization.
            data = b"".join((json.dumps(line, ensure_ascii=True) + "\n").encode("ascii") for line in lines)
            records.write_bytes(data)
            with self.subTest(path=path, verifier=True):
                check = verify_maintenance(self.repo, "t1", env=self.env)
                self.assertFalse(check.ok)
                self.assertIsNone(check.outputs)
                self.assertTrue(any(problem.startswith("OAH_WIKI_MAINTENANCE_STALE")
                                    and "malformed outputs" in problem for problem in check.problems), check.problems)
            for extra, code in (((), "OAH_WIKI_MAINTENANCE_MISSING"),
                                (("--allow-missing-wiki-maintenance",), "OAH_SCOPE_EXCEEDED")):
                with self.subTest(path=path, extra=extra):
                    result = self.finish(*extra)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertTrue(result.stderr.startswith(code), result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    if not extra:
                        self.assertIn("OAH_WIKI_MAINTENANCE_STALE", result.stderr)
                        self.assertIn("oah wiki maintain --task t1", result.stderr)
            self.assertEqual(records.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
