# 스킬 보조 파일 회귀 시험 — 기록한 모든 필수 payload가 실제 brief에 한 번씩 전달되는지 확인한다.
from __future__ import annotations

import unittest

from tests.test_skill_delivery import DeliveryTask, MIT_LICENSE, sha, skill_md


class PayloadConservationTest(DeliveryTask, unittest.TestCase):
    def build_files(self, count: int) -> list[str]:
        files = {"LICENSE": MIT_LICENSE, "skills/guide/SKILL.md": skill_md("guide")}
        auxiliary = []
        contents = []
        for number in range(1, count + 1):
            content = f"Required file {number:03d}: payload-{number:03d}-contents.\n"
            relative = f"references/file-{number:03d}.md"
            files[f"skills/guide/{relative}"] = content
            auxiliary.append({"path": relative, "required": True, "sha256": sha(content.encode("utf-8"))})
            contents.append(content)
        self.build_skills(("acme/guide", "skills/guide", files, {"aux": tuple(auxiliary)}))
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        return contents

    def test_all_27_required_auxiliary_files_appear_once_in_the_serialized_brief(self):
        contents = self.build_files(27)

        result = self.call()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = self.runs()[-1]
        bundle = self.bundle(run)
        assert bundle is not None
        self.assertEqual(len(bundle["items"][0]["aux"]), len(contents))
        prompt = (run / "prompt.md").read_text(encoding="utf-8")
        for content in contents:
            with self.subTest(content=content):
                self.assertEqual(prompt.count(content), 1)

    def test_129_total_required_payloads_refuse_before_run_publication(self):
        self.build_files(128)

        result = self.call()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("OAH_SKILLS_BUDGET_EXCEEDED(items)", result.stderr)
        self.assertIn("129", result.stderr)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
