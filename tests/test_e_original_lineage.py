from __future__ import annotations

import datetime as dt
import json

from tests import e_c6_fixture
from tests.e_c9_fixture import CorrectionFixture, reseal


class OriginalLineageTest(CorrectionFixture):
    def test_latest_projection_with_empty_correction_marker_refuses_without_using_earlier_relay(self):
        from oah.discovery import preparation

        project, earlier, latest = self.two_originals()
        prior = {path: path.read_bytes() for path in earlier.run.path.iterdir() if path.is_file()}
        path = latest.run.file_path("skills.json")
        saved = json.loads(path.read_bytes())
        saved["prepared_e"] = preparation.projection(saved["prepared_e"])
        saved.pop("bundle")
        path.write_text(json.dumps(saved), encoding="utf-8")
        latest.run.file_path("correction.json").write_text("{}", encoding="utf-8")

        self.assert_refusal(project, latest)

        self.assertEqual({path: path.read_bytes() for path in prior}, prior)

    def test_latest_deleted_skills_and_correction_stage_refuse_without_using_earlier_relay(self):
        project, earlier, latest = self.two_originals()
        prior = {path: path.read_bytes() for path in earlier.run.path.iterdir() if path.is_file()}
        latest.run.file_path("skills.json").unlink()
        result_path = latest.run.file_path("result.json")
        result = json.loads(result_path.read_bytes())
        result["stage"] = "correction"
        result_path.write_text(json.dumps(result), encoding="utf-8")

        self.assert_refusal(project, latest)

        self.assertEqual({path: path.read_bytes() for path in prior}, prior)

    def test_two_intact_originals_deliver_the_latest_relay(self):
        project, earlier, latest = self.two_originals()
        prior = {path: path.read_bytes() for path in earlier.run.path.iterdir() if path.is_file()}
        before = latest.run.file_path("skills.json").read_bytes()

        code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

        self.assertEqual((code, result["code"]), (0, "OAH_FIX_APPLIED"), str(result) + err)
        self.assertIn("Second original relay data.", project.fix_calls()[-1]["prompt"])
        self.assertEqual(latest.run.file_path("skills.json").read_bytes(), before)
        self.assertEqual({path: path.read_bytes() for path in prior}, prior)

    def test_resealed_one_second_expiry_change_refuses_against_original_pointer(self):
        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        path = original.run.file_path("skills.json")
        saved = json.loads(path.read_bytes())
        record = saved["prepared_e"]
        record["expires_at"] = (dt.datetime.fromisoformat(record["expires_at"]) - dt.timedelta(seconds=1)).isoformat()
        reseal(record)
        path.write_text(json.dumps(saved), encoding="utf-8")

        self.assert_refusal(project, original)

    def test_empty_initial_stage_records_the_identity_and_delivers_correction_only_skill_later(self):
        project, original = self.correction_only()
        before = original.run.file_path("skills.json").read_bytes()
        saved = json.loads(before)
        self.assertIn("prepared_e", saved)
        self.assertNotIn("bundle", saved)
        self.assertEqual(saved["prepared_e"]["profile"], {})

        code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

        self.assertEqual((code, result["code"]), (0, "OAH_FIX_APPLIED"), str(result) + err)
        self.assertIn("Use the correction-only procedure.", project.fix_calls()[-1]["prompt"])
        self.assertEqual(original.run.file_path("skills.json").read_bytes(), before)
