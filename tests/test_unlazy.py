# unlazy 설치 위치 찾기 검사 — 환경변수 우선, 프로젝트 우선 순서, 잘못된 폴더 건너뛰기, 버전 읽기를 본다.
from __future__ import annotations

import json
import pathlib
import unittest

from oah.unlazy import find_unlazy_dir, unlazy_version
from tests.helpers import make_tmp_dir


def make_unlazy(folder: pathlib.Path, version: str | None = "2.1.0") -> pathlib.Path:
    (folder / "scripts").mkdir(parents=True)
    (folder / "scripts" / "gate-check.mjs").write_text("", encoding="utf-8")
    if version is not None:
        (folder / "package.json").write_text(json.dumps({"name": "unlazy-skill", "version": version}),
                                             encoding="utf-8")
    return folder


class FindUnlazyTest(unittest.TestCase):
    def setUp(self):
        self.root = make_tmp_dir(self)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.home.mkdir()
        self.repo.mkdir()

    def test_env_override_is_authoritative(self):
        custom = make_unlazy(self.root / "custom")
        make_unlazy(self.repo / ".agents" / "skills" / "unlazy")
        found, _ = find_unlazy_dir(self.repo, {"OAH_UNLAZY_DIR": str(custom)}, self.home)
        self.assertEqual(found, custom)

    def test_invalid_env_override_does_not_fall_back(self):
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        found, reason = find_unlazy_dir(self.repo, {"OAH_UNLAZY_DIR": str(self.root / "missing")}, self.home)
        self.assertIsNone(found)
        self.assertIn("OAH_UNLAZY_DIR", reason)

    def test_project_dir_beats_home_dir(self):
        project_copy = make_unlazy(self.repo / ".claude" / "skills" / "unlazy")
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        found, _ = find_unlazy_dir(self.repo, {}, self.home)
        self.assertEqual(found, project_copy)

    def test_agents_dir_beats_claude_dir(self):
        agents_copy = make_unlazy(self.home / ".agents" / "skills" / "unlazy")
        make_unlazy(self.home / ".claude" / "skills" / "unlazy")
        found, _ = find_unlazy_dir(None, {}, self.home)
        self.assertEqual(found, agents_copy)

    def test_folder_without_gate_check_is_skipped(self):
        (self.repo / ".agents" / "skills" / "unlazy").mkdir(parents=True)
        home_copy = make_unlazy(self.home / ".claude" / "skills" / "unlazy")
        found, _ = find_unlazy_dir(self.repo, {}, self.home)
        self.assertEqual(found, home_copy)

    def test_not_installed(self):
        found, reason = find_unlazy_dir(self.repo, {}, self.home)
        self.assertIsNone(found)
        self.assertIn("not found", reason)


class UnlazyVersionTest(unittest.TestCase):
    def test_version_read_from_package_json(self):
        folder = make_unlazy(make_tmp_dir(self) / "unlazy", version="2.1.0")
        self.assertEqual(unlazy_version(folder), "2.1.0")

    def test_missing_package_json_gives_none(self):
        folder = make_unlazy(make_tmp_dir(self) / "unlazy", version=None)
        self.assertIsNone(unlazy_version(folder))


if __name__ == "__main__":
    unittest.main()
