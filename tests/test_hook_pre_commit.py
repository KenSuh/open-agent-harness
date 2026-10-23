# 위키 커밋 검사 — 실제 임시 저장소에서 로그 축소·아카이브 수정·raw 수정·비 UTF-8·새 깨진 링크(경고→차단)를 본다.
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import os
import pathlib
import subprocess
import unittest

from oah import hook_pre_commit
from oah.wiki import notes
from oah.wiki.base import WikiSettings
from oah.wiki.store import Wiki
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir

TODAY = datetime.date(2026, 10, 23)


def entries(count: int) -> bytes:
    return b"".join(f"## 2026-01-{n:02d} entry {n}\n\nbody {n} {'x' * 40}\n\n".encode()
                    for n in range(count, 0, -1))


class PreCommitTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        self.wiki_dir = self.repo / "docs" / "wiki"
        files = {f"docs/wiki/{rel}": data for rel, data in notes.scaffold_files(TODAY).items()}
        files["docs/wiki/log.md"] = files["docs/wiki/log.md"] + b"\n" + entries(20)
        files["docs/wiki/raw/source.txt"] = b"imported\n"
        git_commit(self.repo, files, "scaffold")
        self.state = make_tmp_dir(self) / "state"

    def stage(self, files: dict) -> None:
        for name, content in files.items():
            path = self.repo / name
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=git_env())

    def check(self, **settings) -> hook_pre_commit.Verdict:
        return hook_pre_commit.check(self.repo, WikiSettings(**settings), self.state)

    def assert_blocked(self, verdict, fragment: str) -> None:
        self.assertTrue(verdict.blocked, verdict)
        self.assertTrue(any(fragment in line for line in verdict.blocked), verdict.blocked)

    def test_clean_change_passes(self):
        self.stage({"docs/wiki/new.md": "---\ntitle: New\n---\n[[wiki-guide]]\n"})
        verdict = self.check()
        self.assertEqual((verdict.blocked, verdict.warnings), ([], []))

    def test_shortened_log_without_archive_blocked(self):
        log_bytes = (self.wiki_dir / "log.md").read_bytes()
        self.stage({"docs/wiki/log.md": log_bytes[:log_bytes.rindex(b"## 2026-01-01")]})
        self.assert_blocked(self.check(), "log.md")
        self.stage({"docs/wiki/log.md": None})
        self.assert_blocked(self.check(), "log.md")

    def test_rollover_commit_passes(self):
        w = Wiki(self.repo, WikiSettings(rollover_trigger_bytes=1000, rollover_target_bytes=500), self.state)
        self.assertTrue(w.rollover(today=TODAY).line.startswith("OAH_WIKI_ROLLED_OVER"))
        w.log("after rollover", today=TODAY)
        self.stage({})
        verdict = self.check()
        self.assertEqual(verdict.blocked, [])

    def test_rollover_that_loses_an_entry_blocked(self):
        w = Wiki(self.repo, WikiSettings(rollover_trigger_bytes=1000, rollover_target_bytes=500), self.state)
        w.rollover(today=TODAY)
        archive = self.wiki_dir / "log_archive_01.md"
        data = archive.read_bytes()
        self.stage({"docs/wiki/log_archive_01.md": data[:data.rindex(b"## 2026-01-01")]})
        self.assert_blocked(self.check(), "not in the new archive")

    def test_archive_edit_and_delete_blocked(self):
        w = Wiki(self.repo, WikiSettings(rollover_trigger_bytes=1000, rollover_target_bytes=500), self.state)
        w.rollover(today=TODAY)
        self.stage({})
        subprocess.run(["git", "-C", str(self.repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "roll"],
                       check=True, env=git_env())
        archive = self.wiki_dir / "log_archive_01.md"
        self.stage({"docs/wiki/log_archive_01.md": archive.read_bytes() + b"typo fix\n"})
        self.assert_blocked(self.check(), "log_archive_01.md")
        self.stage({"docs/wiki/log_archive_01.md": None})
        self.assert_blocked(self.check(), "log_archive_01.md")

    def test_raw_edit_blocked_and_add_allowed(self):
        self.stage({"docs/wiki/raw/second.txt": "new import\n"})
        self.assertEqual(self.check().blocked, [])
        self.stage({"docs/wiki/raw/source.txt": "changed\n"})
        self.assert_blocked(self.check(), "raw/source.txt")
        self.stage({"docs/wiki/raw/source.txt": None})
        self.assert_blocked(self.check(), "raw/source.txt")

    def test_non_utf8_and_surrogates_blocked(self):
        for name, data in [("latin.md", b"caf\xe9\n"), ("surrogate.md", b"x \xed\xa0\x80 y\n"),
                           ("notes.txt", b"\xff\xfe")]:
            with self.subTest(name=name):
                subprocess.run(["git", "-C", str(self.repo), "reset", "-q"], check=True)
                for leftover in self.wiki_dir.glob("*"):
                    if leftover.name in ("latin.md", "surrogate.md", "notes.txt"):
                        leftover.unlink()
                self.stage({f"docs/wiki/{name}": data})
                self.assert_blocked(self.check(), name)
        subprocess.run(["git", "-C", str(self.repo), "reset", "-q", "--hard"], check=True)
        self.stage({"docs/wiki/picture.png": b"\x89PNG\r\n\x1a\n\xff\xfe", "docs/wiki/ok.md": "café ✓\n"})
        self.assertEqual(self.check().blocked, [])

    def test_new_broken_links_warn_then_block(self):
        self.stage({"docs/wiki/a.md": "---\ntitle: A\n---\n[[nowhere]] `[[in code]]`\n"})
        verdict = self.check()
        self.assertEqual(verdict.blocked, [])
        self.assertTrue(any("[[nowhere]]" in line for line in verdict.warnings), verdict.warnings)
        self.assertFalse(any("in code" in line for line in verdict.warnings))
        self.assert_blocked(self.check(new_broken_links="block"), "[[nowhere]]")

    def test_existing_broken_link_is_not_new(self):
        self.stage({"docs/wiki/a.md": "[[nowhere]]\n"})
        subprocess.run(["git", "-C", str(self.repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "a"],
                       check=True, env=git_env())
        self.stage({"docs/wiki/a.md": "[[nowhere]]\nmore text\n"})
        self.assertEqual(self.check(new_broken_links="block").blocked, [])

    def test_deleting_a_target_breaks_links_elsewhere(self):
        self.stage({"docs/wiki/wiki-guide.md": None})
        verdict = self.check()
        self.assertTrue(any("wiki-guide" in line for line in verdict.warnings), verdict.warnings)

    def test_pending_recovery_blocks(self):
        journal = self.state / "wiki" / "rollover-journal.json"
        journal.parent.mkdir(parents=True)
        journal.write_text("{}", encoding="utf-8")
        self.stage({"docs/wiki/new.md": "x\n"})
        self.assert_blocked(self.check(), "oah wiki recover")

    def test_no_wiki_passes(self):
        repo = make_git_repo(self)
        git_commit(repo, {"a.txt": "x"})
        self.assertEqual(hook_pre_commit.check(repo, WikiSettings(), self.state).blocked, [])
        empty = make_git_repo(self)
        self.assertEqual(hook_pre_commit.check(empty, WikiSettings(), self.state).blocked, [])

    def test_first_commit_is_checked(self):
        repo = make_git_repo(self)
        (repo / "docs" / "wiki").mkdir(parents=True)
        (repo / "docs" / "wiki" / "bad.md").write_bytes(b"\xff\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        self.assertTrue(hook_pre_commit.check(repo, WikiSettings(), self.state).blocked)

    def test_run_reads_wiki_config(self):
        old_cwd, old_env = os.getcwd(), os.environ.get("OAH_STATE_DIR")
        os.chdir(self.repo)
        os.environ["OAH_STATE_DIR"] = str(self.state)
        try:
            self.stage({"docs/wiki/a.md": "[[nowhere]]\n"})
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(hook_pre_commit.run(argparse.Namespace()), 0)
            self.assertIn("OAH_WIKI_NEW_BROKEN_LINKS", err.getvalue())
            (self.repo / ".oah").mkdir()
            (self.repo / ".oah" / "config.toml").write_text('[wiki]\nnew_broken_links = "block"\n', encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(hook_pre_commit.run(argparse.Namespace()), 1)
            (self.repo / ".oah" / "config.toml").write_text('[wiki]\nbad = 1\n', encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(hook_pre_commit.run(argparse.Namespace()), 69)
            self.assertTrue(err.getvalue().startswith("OAH_CONFIG_UNKNOWN_KEY"))
        finally:
            os.chdir(old_cwd)
            if old_env is None:
                os.environ.pop("OAH_STATE_DIR", None)
            else:
                os.environ["OAH_STATE_DIR"] = old_env

    def test_run_exit_codes(self):
        old_cwd, old_env = os.getcwd(), os.environ.get("OAH_STATE_DIR")
        os.chdir(self.repo)
        os.environ["OAH_STATE_DIR"] = str(self.state)
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(hook_pre_commit.run(argparse.Namespace()), 0)
                self.stage({"docs/wiki/raw/source.txt": "changed\n"})
                self.assertEqual(hook_pre_commit.run(argparse.Namespace()), 1)
            self.assertIn("OAH_WIKI_COMMIT_BLOCKED", err.getvalue())
            outside = make_tmp_dir(self)
            os.chdir(outside)
            os.environ["GIT_CEILING_DIRECTORIES"] = str(outside.parent)  # the test temp root may sit in a checkout
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(hook_pre_commit.run(argparse.Namespace()), 69)
        finally:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
            os.chdir(old_cwd)
            if old_env is None:
                os.environ.pop("OAH_STATE_DIR", None)
            else:
                os.environ["OAH_STATE_DIR"] = old_env


if __name__ == "__main__":
    unittest.main()
