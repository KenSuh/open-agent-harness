# 저장소·상태 경로 검사 — 워크트리끼리 같은 git common dir 을 보는지, 상태 폴더 위치와 재정의를 확인한다.
from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from oah.paths import NotARepo, repo_info, state_dir
from tests.helpers import git_commit, make_git_repo, make_tmp_dir


class RepoInfoTest(unittest.TestCase):
    def test_repo_info_in_main_checkout_and_worktree(self):
        repo = make_git_repo(self)
        git_commit(repo, {"a.txt": "x\n"})
        worktree = repo.parent / "second"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "side", str(worktree)], check=True)
        main_info, other_info = repo_info(repo), repo_info(worktree)
        self.assertEqual(main_info.common_dir.resolve(), other_info.common_dir.resolve())
        self.assertEqual(main_info.root.resolve(), repo.resolve())
        self.assertEqual(other_info.root.resolve(), worktree.resolve())
        self.assertEqual((main_info.branch, other_info.branch), ("main", "side"))
        self.assertEqual(len(main_info.head), 40)

    def test_repo_info_from_subdirectory(self):
        repo = make_git_repo(self)
        git_commit(repo, {"sub/a.txt": "x\n"})
        self.assertEqual(repo_info(repo / "sub").root.resolve(), repo.resolve())

    def test_repository_without_commits_has_no_head(self):
        info = repo_info(make_git_repo(self))
        self.assertIsNone(info.head)
        self.assertEqual(info.branch, "main")

    def test_state_dir_default_and_override(self):
        info = repo_info(make_git_repo(self))
        self.assertEqual(state_dir(info, {}), info.common_dir / "oah")
        custom = make_tmp_dir(self) / "state"
        self.assertEqual(state_dir(info, {"OAH_STATE_DIR": str(custom)}), custom)

    def test_not_a_repo(self):
        plain = make_tmp_dir(self)
        # The temporary folder may itself live inside a repository; stop git's upward search there.
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(plain.parent)}):
            with self.assertRaises(NotARepo) as ctx:
                repo_info(plain)
        self.assertEqual(ctx.exception.code, "OAH_NOT_A_REPO")


if __name__ == "__main__":
    unittest.main()
