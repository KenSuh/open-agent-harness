# 기술 갱신 예약 바이트 고정 시험 — 출처 예약을 더한 뒤에도 skills 의 launchd plist·작업 XML 바이트, 이름, 인자, 기록 위치가 기준과 같은지 본다.
from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import unittest

from oah.toolkit import schedule

ROOT = pathlib.PurePosixPath("/work/my project")
LOG = pathlib.PurePosixPath("/work/state/toolkit/scheduled-update.log")
ARGV = ("/usr/bin/python3", "-m", "oah", "skills", "update", "--scheduled")
# sha256 of the bytes the baseline (before design sources) generated for these exact inputs.
PLIST_SHA256 = "44a4f0409960c5204d7db070649818b892bfbd286bc9b906c39b04511b91bb96"
TASK_SHA256 = "442ad2b165c60bfb91c4c14095daf428a2674d2e8d1e6aefadbb8c39fb345e3c"


class SkillsScheduleBytesTest(unittest.TestCase):
    def entry(self, kind: str) -> schedule.Entry:
        name = ("io.github.open-agent-harness.skills-update.9559614280" if kind == "launchd"
                else "oah-skills-update-9559614280")
        path = pathlib.PurePosixPath("/work/home/Library/LaunchAgents") / f"{name}.plist" if kind == "launchd" \
            else None
        return schedule.Entry(kind, name, path, ARGV, ROOT, LOG)

    def test_launchd_plist_bytes_are_unchanged(self) -> None:
        data = schedule.launchd_plist(self.entry("launchd"))
        self.assertEqual(PLIST_SHA256, hashlib.sha256(data).hexdigest())

    def test_task_xml_bytes_are_unchanged(self) -> None:
        text = schedule.task_xml(self.entry("schtasks"), start=dt.datetime(2026, 11, 1, 10, 0))
        self.assertEqual(TASK_SHA256, hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertNotIn("<Enabled>", text)

    def test_skills_plan_keeps_its_names_arguments_log_and_hints(self) -> None:
        for platform, kind in (("darwin", "launchd"), ("win32", "schtasks")):
            entry = schedule.plan_entry(project_root=pathlib.Path("/work/my project"), home=pathlib.Path("/work/home"),
                                        state_dir=pathlib.Path("/work/state"), platform=platform,
                                        python="/usr/bin/python3")
            self.assertEqual(kind, entry.kind)
            self.assertEqual(ARGV, entry.argv)
            self.assertEqual(pathlib.Path("/work/state/toolkit/scheduled-update.log"), entry.log_path)
            self.assertEqual("skills", entry.purpose)
            self.assertEqual(schedule.SKILLS_REMOVE_HINT, entry.remove_hint)
            self.assertTrue(entry.name.startswith(schedule.LABEL_PREFIX if kind == "launchd"
                                                  else schedule.TASK_PREFIX))


if __name__ == "__main__":
    unittest.main()
