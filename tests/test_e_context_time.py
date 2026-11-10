# 준비 시간 회귀 — 짧은 실행 전 창과 네이티브 정산 창 및 새 수정 준비를 구분하는지 확인한다.
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import unittest
from unittest import mock

from . import e_c6_fixture
from tests.test_launcher_native import NativeTask
from tests.test_skill_delivery import DeliveryTask


def clock_after(record):
    at = dt.datetime.fromisoformat(record["expires_at"]) + dt.timedelta(minutes=1)

    class Later(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz)

    return Later


class NativeWindowTest(NativeTask, unittest.TestCase):
    def test_A3_idempotent_settle_keeps_the_first_receipt_after_five_minutes(self):
        from oah import call, cli

        offered, run = self.offered()
        first = self.settle(run)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = (run / "native-settlement.json").read_bytes()
        saved = json.loads((run / "skills.json").read_bytes())["prepared_e"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(call.dt, "datetime", clock_after(saved)), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["call", "settle", "--run", run.name, "--offer-digest", offered["message"]["sha256"],
                "--host-child-id", "agent-1", "--outcome", "succeeded", "--result-file", str(self.work / "reply.txt")])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertEqual((run / "native-settlement.json").read_bytes(), before)

    def test_A3_first_and_idempotent_settle_use_the_offer_window_after_five_minutes(self):
        from oah import call, cli

        offered, run = self.offered()
        before = (run / "skills.json").read_bytes()
        saved = json.loads(before)["prepared_e"]
        reply = self.work / "reply.txt"
        reply.write_text("Received the selected skill.\n", encoding="utf-8")
        command = ["call", "settle", "--run", offered["run"], "--offer-digest", offered["message"]["sha256"],
                   "--host-child-id", "child-one", "--outcome", "succeeded", "--result-file", str(reply), "--json"]
        receipts = []
        for ordinal in (1, 2):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                    mock.patch.object(call.dt, "datetime", clock_after(saved)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(command)
            self.assertEqual(code, 0, f"settle {ordinal}: {out.getvalue()}{err.getvalue()}")
            receipts.append((run / "native-settlement.json").read_bytes())
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual((run / "skills.json").read_bytes(), before)
        self.assertFalse(self.record.exists())


class CorrectionWindowTest(DeliveryTask, unittest.TestCase):
    def test_A3_correction_after_five_minutes_takes_fresh_admission(self):
        from oah import call

        project, ordinary, _need, _basis = e_c6_fixture.selected_ordinary(self)
        before = ordinary.run.file_path("skills.json").read_bytes()
        saved = json.loads(before)["prepared_e"]
        admissions = []
        real = call._admit_skills

        def capture(*args, **options):
            pack = real(*args, **options)
            if pack and pack.admitted and pack.admitted.prepared:
                admissions.append(pack.admitted.prepared.record())
            return pack

        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                mock.patch.object(call.dt, "datetime", clock_after(saved)), \
                mock.patch.object(call, "_admit_skills", capture):
            result = project.fix()

        self.assertEqual(result.code, "OAH_FIX_APPLIED", result.message)
        self.assertTrue(admissions)
        self.assertTrue(all(dt.datetime.fromisoformat(row["expires_at"]) >
                            dt.datetime.fromisoformat(saved["expires_at"]) for row in admissions))
        self.assertTrue(any(row["facts"]["selections"] == saved["facts"]["selections"] for row in admissions))
        self.assertEqual(ordinary.run.file_path("skills.json").read_bytes(), before)
        self.assertIn("Prepare the selected ledger rows.", project.fix_calls()[-1]["prompt"])

    def test_A3_changed_selection_refuses_a_late_correction_and_keeps_the_original(self):
        from oah import call
        from oah.discovery import tasks
        from tests.test_fix import ReviewError

        project, ordinary, need, basis = e_c6_fixture.selected_ordinary(self)
        before = ordinary.run.file_path("skills.json").read_bytes()
        saved = json.loads(before)["prepared_e"]
        prior_calls = project.fix_calls()
        from tests.test_skill_delivery import MIT_LICENSE, skill_md

        self.upstream("acme/revised", {"LICENSE": MIT_LICENSE,
            "skills/revised/SKILL.md": skill_md("revised", "Use the revised ledger procedure.\n")})
        revised = self.reviewed(project.state, "acme/revised", "skills/revised")
        tasks.select(project.state, need, [revised["id"]], basis=basis)
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                mock.patch.object(call.dt, "datetime", clock_after(saved)), self.assertRaises(ReviewError) as caught:
            project.fix()
        self.assertEqual(caught.exception.code, "OAH_FIX_STATE_CHANGED")
        self.assertNotIn("expired", str(caught.exception))
        self.assertEqual(project.fix_calls(), prior_calls)
        self.assertEqual(ordinary.run.file_path("skills.json").read_bytes(), before)
