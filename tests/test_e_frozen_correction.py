from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import unittest
from unittest import mock

from tests import e_c6_fixture, test_capabilities as capabilities
from tests.helpers import git_commit
from tests.test_e_spent_evidence import clock
from tests.test_skill_delivery import DeliveryTask, DESIGN, MIT_LICENSE, coverage_text, link_op, skill_md


class FrozenCorrectionTest(DeliveryTask, unittest.TestCase):
    def correct(self, project, at):
        from oah import call, cli
        from oah.review import fix
        from tests.test_fix import RULES

        real = fix.correct
        out, err = io.StringIO(), io.StringIO()

        def correct(request, **options):
            return real(request, **options, rules=RULES, temp_root=project.temp_root)

        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                mock.patch.object(call.dt, "datetime", clock(at)), mock.patch.object(fix, "correct", correct), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["fix", "--task", "t1", "--tier", "high", "--json"])
        return code, json.loads(out.getvalue()), err.getvalue()

    def selected_parent(self, *, relay=False):
        from oah import call, cli
        from oah.capabilities import inventory, plan
        from tests.helpers import make_tmp_dir

        now = dt.datetime.now(dt.timezone.utc)
        entries = [inventory.parent_entry("mcp:docs/lookup", schema=capabilities.SCHEMA, observed=now,
                                         effects=["read-network"])] if relay else []
        parent = capabilities.write(make_tmp_dir(self) / "lead.json", inventory.parent_inventory(entries, observed=now))
        original = call.prepare
        relayed = []

        def prepare(request, *args, **options):
            if request.stage == "initial":
                refs = ()
                if relay:
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = cli.main(["capabilities", "plan", "--task", "t1", "--stage", "initial",
                            "--engine", "codex", "--skill-needs", request.skill_needs,
                            "--parent-inventory", str(parent), "--json"])
                    self.assertEqual(code, 1, err.getvalue())
                    emitted, = json.loads(out.getvalue())["requests"]
                    record, _ = plan.ingest(options["state_dir"], emitted, capability=emitted["capability_id"],
                        payload=capabilities.RESULT.encode("utf-8"), invocation={"outcome": "ok",
                            "at": emitted["created_at"], "arguments_digest": "sha256:" + "3" * 64}, now=now)
                    relayed.append(record)
                    refs = (record["id"],)
                request = dataclasses.replace(request, parent_inventory=str(parent), relay_results=refs)
            return original(request, *args, **options)

        with mock.patch.object(call, "prepare", prepare):
            project, ordinary, _need, _basis = e_c6_fixture.selected_ordinary(
                self, skill_tools="allowed-tools: mcp__docs__lookup\n" if relay else "")
        return project, ordinary, now, parent, relayed

    def test_relay_correction_at_seven_minutes_delivers_original_bytes_and_time_without_refresh(self):
        from oah.capabilities import plan

        project, ordinary, now, _parent, relayed = self.selected_parent(relay=True)
        earlier = ordinary.run.file_path("skills.json").read_bytes()
        real_load = plan.load
        refs = []

        def load(state, ref, **options):
            refs.append(ref)
            return real_load(state, ref, **options)

        with mock.patch.object(plan, "load", load):
            code, result, err = self.correct(project, now + dt.timedelta(minutes=7))
        self.assertEqual(code, 0, str(result) + err)
        self.assertEqual(result["code"], "OAH_FIX_APPLIED")
        prompt = project.fix_calls()[-1]["prompt"]
        self.assertIn(capabilities.RESULT.rstrip("\n"), prompt)
        self.assertIn(relayed[0]["invocation"]["at"], prompt)
        self.assertEqual(set(refs), {relayed[0]["id"]})
        self.assertEqual(ordinary.run.file_path("skills.json").read_bytes(), earlier)

    def test_changed_recorded_relay_permission_with_pointer_unchanged_refuses_before_child(self):
        from oah.capabilities import inventory
        from oah.discovery.store import digest

        project, ordinary, now, _parent, _relayed = self.selected_parent(relay=True)
        path = ordinary.run.file_path("skills.json")
        saved = json.loads(path.read_bytes())
        changed = saved["prepared_e"]["facts"]["inventory"]["parent"]
        changed["entries"][0]["permission"]["effects"] = ["read-local"]
        changed["entries"][0]["entry_digest"] = inventory.entry_digest(changed["entries"][0])
        changed["digest"] = inventory.inventory_digest(changed)
        record = saved["prepared_e"]
        record["prep_id"] = digest({key: value for key, value in record["facts"].items()
                                   if key not in ("relay_results", "capability_plan")})
        record["digest"] = digest({key: value for key, value in record.items() if key != "digest"})
        capabilities.write(path, saved)
        before, calls = ordinary.run.file_path("skills.json").read_bytes(), project.fix_calls()
        code, result, err = self.correct(project, now + dt.timedelta(minutes=7))
        self.assertEqual(code, 1, str(result) + err)
        self.assertEqual(result["code"], "OAH_FIX_STATE_CHANGED")
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(ordinary.run.file_path("skills.json").read_bytes(), before)

    def test_parent_empty_list_with_pointer_unchanged_refuses_with_state_changed(self):
        from oah.discovery.store import digest

        project, ordinary, now, _parent, _relayed = self.selected_parent()
        path = ordinary.run.file_path("skills.json")
        saved = json.loads(path.read_text(encoding="utf-8"))
        record = saved["prepared_e"]
        record["facts"]["inventory"]["parent"] = []
        record["prep_id"] = digest({k: v for k, v in record["facts"].items() if k != "relay_results"})
        record["digest"] = digest({k: v for k, v in record.items() if k != "digest"})
        capabilities.write(path, saved)
        before, calls = path.read_bytes(), project.fix_calls()
        code, result, err = self.correct(project, now)
        self.assertEqual(code, 1, str(result) + err)
        self.assertEqual(result["code"], "OAH_FIX_STATE_CHANGED")
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_frozen_relay_payload_refuses_before_child_and_preserves_original(self):
        project, ordinary, now, _parent, relayed = self.selected_parent(relay=True)
        blob = relayed[0]["payload"]["blob_ref"].removeprefix("sha256:")
        (project.state / "skills/relay-payloads" / (blob + ".json")).unlink()
        before, calls = ordinary.run.file_path("skills.json").read_bytes(), project.fix_calls()
        code, result, err = self.correct(project, now + dt.timedelta(minutes=7))
        self.assertEqual(code, 1, str(result) + err)
        self.assertEqual(result["code"], "OAH_FIX_STATE_CHANGED")
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(ordinary.run.file_path("skills.json").read_bytes(), before)

    def test_parent_correction_at_seven_minutes_needs_no_inventory_refresh(self):
        project, ordinary, now, parent, _relayed = self.selected_parent()
        before = parent.read_bytes()
        code, result, err = self.correct(project, now + dt.timedelta(minutes=7))
        self.assertEqual(code, 0, str(result) + err)
        self.assertEqual(result["code"], "OAH_FIX_APPLIED")
        self.assertIn("Prepare the selected ledger rows.", project.fix_calls()[-1]["prompt"])
        self.assertEqual(parent.read_bytes(), before)

    def test_missing_or_replaced_managed_records_refuse_before_child(self):
        from oah.discovery import preparation

        for change in ("delete-skills", "projection-without-bundle", "result-status"):
            with self.subTest(change=change):
                self.upstreams.pop("acme/ledger", None)
                project, ordinary, _need, _basis = e_c6_fixture.selected_ordinary(self)
                path = ordinary.run.file_path("skills.json")
                saved = json.loads(path.read_text(encoding="utf-8"))
                if change == "delete-skills":
                    path.unlink()
                elif change == "projection-without-bundle":
                    saved["prepared_e"] = preparation.projection(saved["prepared_e"])
                    saved.pop("bundle")
                    capabilities.write(path, saved)
                else:
                    result_path = ordinary.run.file_path("result.json")
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    result["status"] = "refused"
                    capabilities.write(result_path, result)
                earlier = {p.name: p.read_bytes() for p in ordinary.run.path.iterdir() if p.is_file()}
                calls = project.fix_calls()
                code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))
                self.assertEqual(code, 1, str(result) + err)
                self.assertEqual(result["code"], "OAH_FIX_STATE_CHANGED")
                self.assertEqual(project.fix_calls(), calls)
                self.assertEqual({p.name: p.read_bytes() for p in ordinary.run.path.iterdir() if p.is_file()}, earlier)

    def test_stage_specific_coverage_delivers_only_the_correction_stage_content(self):
        from oah import cli
        from oah.wiki import notes
        from oah.design import open_store
        from oah.design.binding import PacketDesign, replace_section
        from tests.test_design_store import TODAY

        def configure(project, primary):
            self.upstream("acme/extra", {"LICENSE": MIT_LICENSE,
                "skills/extra/SKILL.md": skill_md("extra", "Use the correction-only procedure.\n")})
            extra = self.reviewed(project.state, "acme/extra", "skills/extra")
            git_commit(project.repo, {f"docs/wiki/{p}": b for p, b in notes.scaffold_files(TODAY).items()})
            store = open_store(project.repo, env=project.env)
            store.add_project("acme", title="Acme", event="project", today=TODAY)
            coverage = coverage_text(project.state, [link_op(primary, dispatch=("initial",)),
                link_op(extra, check="lifetime", required=False, dispatch=("correction",))])
            first = store.new("acme", "booking", title="Booking", parts={"design": DESIGN, "coverage": coverage},
                              event="first", today=TODAY)
            assert first.digest is not None
            store.accept("acme", "booking", revision=1, digest=first.digest, reason="accepted", today=TODAY)
            packet = project.repo / ".oah/tasks/t1/PACKET.md"
            section = PacketDesign.of(store.context("acme", "booking").binding("accepted"))
            source = packet.read_text(encoding="utf-8").replace("- .oah/tasks/t1\n", "") + \
                "\n## Plan\n- initial → codex → high\n## Acceptance\n## Return\n"
            git_commit(project.repo, {".oah/tasks/t1/PACKET.md": replace_section(source, section)})
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["start", "--task", "t1", "--agent", "claude", "--packet",
                                 ".oah/tasks/t1/PACKET.md"])
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            from oah import call
            project.lease = call.bound_lease(project.state, "t1")

        project, ordinary, _need, _basis = e_c6_fixture.selected_ordinary(self, configure=configure)
        before = ordinary.run.file_path("skills.json").read_bytes()
        code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(code, 0, str(result) + err)
        self.assertEqual(result["code"], "OAH_FIX_APPLIED")
        prompt = project.fix_calls()[-1]["prompt"]
        self.assertIn("Use the correction-only procedure.", prompt)
        self.assertNotIn("Prepare the selected ledger rows.", prompt)
        self.assertEqual(ordinary.run.file_path("skills.json").read_bytes(), before)
