from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import unittest
from unittest import mock

from tests.helpers import git_commit
from tests.test_e_frozen_correction import FrozenCorrectionTest
from tests.test_skill_delivery import DeliveryTask, DESIGN, MIT_LICENSE, coverage_text, link_op, skill_md


class CorrectionFixture(DeliveryTask, unittest.TestCase):
    correct = FrozenCorrectionTest.correct
    selected_parent = FrozenCorrectionTest.selected_parent

    def assert_refusal(self, project, original):
        before = {path: path.read_bytes() for path in original.run.path.iterdir() if path.is_file()}
        calls = project.fix_calls()
        code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))
        self.assertEqual((code, result["code"]), (1, "OAH_FIX_STATE_CHANGED"), str(result) + err)
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def assert_applied(self, project, original, *, relay=False):
        before = {path: path.read_bytes() for path in original.run.path.iterdir() if path.is_file()}
        calls = len(project.fix_calls())
        code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))
        self.assertEqual((code, result["code"]), (0, "OAH_FIX_APPLIED"), str(result) + err)
        self.assertEqual(len(project.fix_calls()) - calls, 1)
        self.assertIn("Prepare the selected ledger rows.", project.fix_calls()[-1]["prompt"])
        if relay:
            from tests.test_capabilities import RESULT

            self.assertIn(RESULT.rstrip("\n"), project.fix_calls()[-1]["prompt"])
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def two_originals(self):
        from oah import call, call_cli
        from oah.capabilities import plan
        from oah.secrets import NoKeychain

        real_execute, real_ingest = call.execute, plan.ingest
        later, ingested = [], []

        def execute(prepared, **options):
            result = real_execute(prepared, **options)
            if prepared.stage == "initial":
                saved = json.loads(prepared.run.file_path("skills.json").read_bytes())
                tools = call_cli.intent_tools()
                root = pathlib.Path(prepared.cwd)
                newer = call.prepare(call.CallRequest(engine="codex", stage="initial", task="t1", tier="high",
                    override_stage=True, skill_needs=saved["prepared_e"]["references"]["skill_needs"], no_rerank=True),
                    call_cli.load_settings(root), root=root, state_dir=prepared.run.path.parents[2],
                    env=options["base_env"], footer=tools.footer, has_footer=tools.has_footer, keychain=NoKeychain(),
                    now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
                self.assertEqual(real_execute(newer, **options)["status"], "finished")
                later.append(newer)
            return result

        def ingest(*args, **options):
            if ingested:
                options["payload"] = b"Second original relay data.\n"
            result = real_ingest(*args, **options)
            ingested.append(result[0]["id"])
            return result

        with mock.patch.object(call, "execute", execute), mock.patch.object(plan, "ingest", ingest):
            project, earlier, _now, _parent, _relayed = self.selected_parent(relay=True)
        self.assertEqual(len(later), 1)
        self.assertEqual(len(set(ingested)), 2)
        self.assertNotEqual(later[0].run_id, earlier.run_id)
        project.queue_fix({"write": {"src/app.py": "print('corrected')\n"}})
        return project, earlier, later[0]

    def correction_only(self):
        from oah import call, call_cli, cli
        from oah.design import open_store
        from oah.design.binding import PacketDesign, replace_section
        from oah.secrets import NoKeychain
        from oah.wiki import notes
        from tests.test_design_store import TODAY
        from tests.test_fix import FixProject, RULES

        project = FixProject(self, config="\n[models]\nfetch_scores=false\n")
        self.upstream("acme/correction", {"LICENSE": MIT_LICENSE,
            "skills/correction/SKILL.md": skill_md("correction", "Use the correction-only procedure.\n")})
        reviewed = self.reviewed(project.state, "acme/correction", "skills/correction")
        git_commit(project.repo, {f"docs/wiki/{p}": b for p, b in notes.scaffold_files(TODAY).items()})
        store = open_store(project.repo, env=project.env)
        store.add_project("acme", title="Acme", event="project", today=TODAY)
        coverage = coverage_text(project.state, [link_op(reviewed, required=False, dispatch=("correction",))])
        first = store.new("acme", "booking", title="Booking", parts={"design": DESIGN, "coverage": coverage},
                          event="first", today=TODAY)
        if first.digest is None:
            self.fail("the fixture has no design digest")
        store.accept("acme", "booking", revision=1, digest=first.digest, reason="accepted", today=TODAY)
        packet = project.repo / ".oah/tasks/t1/PACKET.md"
        section = PacketDesign.of(store.context("acme", "booking").binding("accepted"))
        source = packet.read_text(encoding="utf-8").replace("- .oah/tasks/t1\n", "") + \
            "\n## Plan\n- initial \u2192 codex \u2192 high\n## Acceptance\n## Return\n"
        git_commit(project.repo, {".oah/tasks/t1/PACKET.md": replace_section(source, section)})
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["start", "--task", "t1", "--agent", "claude", "--packet",
                                       ".oah/tasks/t1/PACKET.md"]), 0)
            holder = call.bound_lease(project.state, "t1")
            if holder is None:
                self.fail("the fixture has no task lease")
            project.lease = holder
            from oah.discovery import budget

            with mock.patch.object(budget, "qualify", side_effect=AssertionError("an empty stage needs no qualification")):
                original = call.prepare(call.CallRequest(engine="codex", stage="initial", task="t1", tier="high",
                    override_stage=True, no_rerank=True), call_cli.load_settings(project.repo), root=project.repo,
                    state_dir=project.state, env=project.env, footer=tools.footer, has_footer=tools.has_footer,
                    keychain=NoKeychain())
                self.assertEqual(call.execute(original, base_env=project.env, rules=RULES)["status"], "finished")
        project.changes_requested()
        project.queue_fix({}, {"write": {"src/app.py": "print('corrected')\n"}})
        return project, original


def reseal(record):
    from oah.discovery.store import digest

    record["prep_id"] = digest({key: value for key, value in record["facts"].items()
                               if key not in ("relay_results", "capability_plan")})
    record["digest"] = digest({key: value for key, value in record.items() if key != "digest"})
