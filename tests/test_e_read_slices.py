from __future__ import annotations

import contextlib
import json
import os
import unittest
from unittest import mock

from tests.helpers import git_commit
from tests.test_skill_delivery import DeliveryTask, MIT_LICENSE, sha, skill_md


class RequiredReadSlicesTest(DeliveryTask, unittest.TestCase):
    def test_real_b_whole_read_plan_counts_fifteen_required_line_slices(self):
        from oah.design import task_context
        from oah.discovery import budget
        from tests.test_design_context import materials_part

        data = b"\n" * 29000
        self.parts = {"design": "People book.\n", "materials": materials_part([("NOTICE", data)])}
        self.build(self.parts)
        _holder, judged = self.judged()
        plan = task_context.prepare_transport(judged, task="t1", run="r1", root=self.repo,
                                             engine="claude", operation="work")
        whole = [item for item in plan.files if item.data == data and item.obligation == "whole"]
        self.assertEqual(len(whole), 1)
        self.assertEqual(len(task_context.file_lines(whole[0].data)), 29000)
        self.assertEqual(budget.read_render(whole[0].data), 290000 + 15 * budget.READ_ENVELOPE_BYTES)

    def test_actual_reference_delivery_refuses_when_fifteen_slice_envelopes_exceed_total(self):
        from oah import call, call_cli
        from oah.secrets import NoKeychain

        data = "\n" * 29000
        files = {"LICENSE": MIT_LICENSE, "skills/slices/SKILL.md": skill_md("slices", "Read every required file.\n")}
        auxiliaries = []
        for number in range(7):
            path = f"references/file-{number}.md"
            files["skills/slices/" + path] = data
            auxiliaries.append({"path": path, "required": True, "sha256": sha(data.encode("utf-8"))})
        self.build_skills(("acme/slices", "skills/slices", files, {"aux": tuple(auxiliaries)}))
        git_commit(self.repo, {".claude/" + name: text for name, text in files.items() if name.startswith("skills/")},
                   "install required slice fixture")
        if os.name == "nt":
            with self.assertRaisesRegex(call.EngineError,
                                        r"OAH_SKILL_REFERENCE_UNAVAILABLE\(unsupported-engine\)") as caught:
                self.prepare()
            self.assertIn("windows-process-profile-pending", str(caught.exception))
            self.assertEqual(self.runs(), [])
            self.assertFalse(self.record.exists())
            return
        control = self.prepare()
        record = json.loads(control.run.file_path("skills.json").read_text(encoding="utf-8"))
        envelope = record["prepared_e"]["envelope"]
        pad = 524000 - envelope["prompt_bytes"] - (290000 + 9 * 2048)
        self.assertGreater(pad, 0)
        earlier = control.run.file_path("skills.json").read_bytes()
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
            with self.assertRaisesRegex(call.EngineError, r"OAH_SKILLS_BUDGET_EXCEEDED\(brief\)"):
                call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                    no_rerank=True, round_instructions="q" * pad), call_cli.load_settings(self.repo), root=self.repo,
                    state_dir=self.state_dir, env=self.env, footer=tools.footer, has_footer=tools.has_footer,
                    keychain=NoKeychain())
        self.assertEqual(control.run.file_path("skills.json").read_bytes(), earlier)
        self.assertEqual(len(self.runs()), 1)
        self.assertFalse(self.record.exists())
