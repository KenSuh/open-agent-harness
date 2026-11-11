from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from unittest import mock

from tests import e_c6_fixture
from tests.e_c9_fixture import CorrectionFixture, reseal


class FrozenInputsTest(CorrectionFixture):
    def test_naive_preparation_expiry_with_pointer_unchanged_refuses_with_state_changed(self):
        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        path = original.run.file_path("skills.json")
        saved = json.loads(path.read_bytes())
        record = saved["prepared_e"]
        record["expires_at"] = dt.datetime.fromisoformat(record["expires_at"]).replace(tzinfo=None).isoformat()
        reseal(record)
        path.write_text(json.dumps(saved), encoding="utf-8")

        self.assert_refusal(project, original)

    def test_naive_parent_observation_with_pointer_unchanged_refuses_with_state_changed(self):
        from oah.capabilities import inventory

        project, original, _now, _parent, _relayed = self.selected_parent()
        path = original.run.file_path("skills.json")
        saved = json.loads(path.read_bytes())
        record = saved["prepared_e"]
        parent = record["facts"]["inventory"]["parent"]
        parent["observed_at"] = dt.datetime.fromisoformat(parent["observed_at"]).replace(tzinfo=None).isoformat()
        parent["digest"] = inventory.inventory_digest(parent)
        reseal(record)
        path.write_text(json.dumps(saved), encoding="utf-8")

        self.assert_refusal(project, original)

    def test_taskless_dispatch_operation_with_pointer_unchanged_refuses_with_state_changed(self):
        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        path = original.run.file_path("skills.json")
        saved = json.loads(path.read_bytes())
        record = saved["prepared_e"]
        record["facts"]["route"] = record["references"]["route"] = "lead"
        record["facts"]["operation"]["kind"] = "public-explore"
        record["facts"]["context"].update(axes=None, floor=None)
        reseal(record)
        path.write_text(json.dumps(saved), encoding="utf-8")

        self.assert_refusal(project, original)

    def test_parent_file_removed_after_freeze_uses_frozen_parent(self):
        from oah import call

        project, original, _now, parent, _relayed = self.selected_parent()
        real = call._freeze_e

        def freeze(inputs, **options):
            real(inputs, **options)
            parent.unlink()

        with mock.patch.object(call, "_freeze_e", freeze):
            self.assert_applied(project, original)

    def test_relay_blob_removed_after_freeze_delivers_in_memory_bytes(self):
        from oah import call

        project, original, _now, _parent, relayed = self.selected_parent(relay=True)
        blob = project.state / "skills/relay-payloads" / (relayed[0]["payload"]["blob_ref"][7:] + ".json")
        real = call._freeze_e

        def freeze(inputs, **options):
            real(inputs, **options)
            blob.unlink()

        with mock.patch.object(call, "_freeze_e", freeze):
            self.assert_applied(project, original, relay=True)

    def test_relay_blob_missing_at_freeze_refuses_before_child(self):
        project, original, _now, _parent, relayed = self.selected_parent(relay=True)
        blob = project.state / "skills/relay-payloads" / (relayed[0]["payload"]["blob_ref"][7:] + ".json")
        blob.unlink()

        self.assert_refusal(project, original)

    def test_parent_file_removed_before_freeze_uses_recorded_parent(self):
        project, original, _now, parent, _relayed = self.selected_parent(relay=True)
        parent.unlink()

        self.assert_applied(project, original, relay=True)

    def test_parent_file_refreshed_before_freeze_uses_recorded_parent(self):
        from oah.capabilities import inventory

        project, original, now, parent, _relayed = self.selected_parent(relay=True)
        refreshed = inventory.parent_inventory([], observed=now + dt.timedelta(seconds=1))
        parent.write_text(json.dumps(refreshed), encoding="utf-8")

        self.assert_applied(project, original, relay=True)

    def test_one_relay_load_at_freeze_supplies_all_later_consumers(self):
        from oah.capabilities import plan

        project, original, _now, _parent, relayed = self.selected_parent(relay=True)
        real, loaded = plan.load, []

        def load(state, ref, **options):
            loaded.append(ref)
            return real(state, ref, **options)

        with mock.patch.object(plan, "load", load):
            self.assert_applied(project, original, relay=True)

        self.assertEqual(loaded, [relayed[0]["id"]])

    def test_renewed_lease_keeps_intact_original_available_to_correction(self):
        from oah.lease import Store

        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo):
            Store(project.state).renew(task="t1", agent="claude", ttl_hours=1)

        self.assert_applied(project, original)

    def test_renewed_lease_does_not_hide_deleted_original_skills(self):
        from oah.lease import Store

        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo):
            Store(project.state).renew(task="t1", agent="claude", ttl_hours=1)
        original.run.file_path("skills.json").unlink()

        self.assert_refusal(project, original)
