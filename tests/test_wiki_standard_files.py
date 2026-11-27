# 위키 표준 파일 검사 — 소스 CLI 와 데스크톱 코어가 같은 다섯 파일을 같은 바이트로 만드는지 공용 기준 파일로 본다.
from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import unittest

from oah.wiki import notes

FIXTURE = (pathlib.Path(__file__).resolve().parents[1]
           / "crates" / "oah-core" / "tests" / "project_tools_wiki" / "standard_files.json")


class StandardFilesTest(unittest.TestCase):
    def test_standard_files_match_the_fixture_the_desktop_core_checks(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        files = notes.standard_files(datetime.date.fromisoformat(fixture["today"]))
        self.assertEqual({name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                         fixture["files"])
        self.assertEqual(list(files), list(fixture["files"]))

    def test_scaffold_is_the_standard_files_followed_by_the_obsidian_settings(self) -> None:
        today = datetime.date(2026, 1, 2)
        standard, scaffold = notes.standard_files(today), notes.scaffold_files(today)
        self.assertEqual(list(scaffold)[:len(standard)], list(standard))
        self.assertEqual({name: scaffold[name] for name in standard}, standard)
        extra = [name for name in scaffold if name not in standard]
        self.assertTrue(extra)
        self.assertTrue(all(name.startswith(".obsidian/") and name.endswith(".json") for name in extra))


if __name__ == "__main__":
    unittest.main()
