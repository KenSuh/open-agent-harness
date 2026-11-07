# 작업 설계 결속 시험 — 과제 기준 입력·초안 확인·TTL 갱신·만료 재개·완료 뒤 재구성·생성 출력 면제·잘못된 설계 값 격리를 실제 명령으로 본다.
"""B-T01..B-T06, B-T10/B-T11 and B-T13 (B1/B2/B3/B5).

The task's evidence identity is its semantic basis (identity, project, design, accepted intent, scope, gates), not
the lease incarnation: a TTL renewal or an exact resume after expiry keeps it, a changed input does not. Finished
tasks keep their full binding for land, generated design outputs are exempt only when byte-exact, and a malformed
design value of one task never hides or rewrites another's.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from oah.design import binding as design
from oah.design.binding import TaskBasis, TaskBinding, TaskIdentity
from oah.design.model import DesignError
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir
from tests.test_design_context import DesignTask
from tests.test_design_store import TODAY
from tests.test_intent_wiring import packet_text

PACKET = ".oah/tasks/t1/PACKET.md"


class BasisTest(unittest.TestCase):
    """B-T01 at the unit level: what the basis digest covers."""

    def setUp(self):
        self.identity = TaskIdentity("sha256:" + "0" * 64, "t1", "a" * 32)

    def basis(self, **change) -> TaskBasis:
        fields = {"project_uid": "u1", "design": "booking", "intent": {"id": "t1", "revision": 1},
                  "scope": ["src", "docs"], "gates": [".oah/tasks/t1/GATES.md"], **change}
        return TaskBasis.of(self.identity, **fields)

    def test_order_and_spelling_of_the_same_inputs_keep_the_basis(self):
        self.assertEqual(self.basis().digest, self.basis(scope=["docs", "src", "SRC"]).digest)

    def test_raised_intent_scope_and_gates_change_it(self):
        base = self.basis().digest
        for change in ({"intent": {"id": "t1", "revision": 2}}, {"scope": ["src", "docs", "lib"]},
                       {"gates": [".oah/tasks/t1/GATES.md", "G2.md"]}, {"design": "billing"}):
            with self.subTest(change):
                self.assertNotEqual(self.basis(**change).digest, base)

    def test_a_carry_for_other_inputs_refuses(self):
        record = TaskBinding(self.identity, self.basis().digest, design.DesignBinding("acme", "u1", "booking", 1,
                                                                                         "sha256:" + "1" * 64))
        with self.assertRaises(DesignError) as caught:
            design.carry(record, None, basis=self.basis(scope=["lib"]), revision=2, digest="sha256:" + "2" * 64,
                         reason="r", agent="a", at="t", authority=None, task="t1")
        self.assertEqual(caught.exception.code, design.INPUTS_CHANGED_CODE)

    def test_earlier_record_shapes_are_refused_not_migrated(self):
        current = TaskBinding(self.identity, self.basis().digest,
                              design.DesignBinding("acme", "a" * 32, "booking", 1, "sha256:" + "1" * 64)).to_json()
        self.assertEqual(TaskBinding.from_json(current).to_json(), current)
        for old in ({**current, "version": 1}, {k: v for k, v in current.items() if k != "basis"},
                    {"binding": current["binding"], "carried": [], "draft": None}):
            with self.subTest(old=sorted(old)):
                with self.assertRaises(ValueError):
                    TaskBinding.from_json(old)

    def test_the_evidence_key_names_identity_basis_and_binding_only(self):
        binding = design.DesignBinding("acme", "u1", "booking", 1, "sha256:" + "1" * 64)
        key = design.evidence_key(self.identity, self.basis().digest, binding)
        self.assertEqual(key, design.evidence_key(self.identity, self.basis(scope=["docs", "src"]).digest, binding))
        self.assertNotEqual(key, design.evidence_key(TaskIdentity(self.identity.repo, "t1", "b" * 32),
                                                     self.basis().digest, binding))


class AcknowledgmentTest(DesignTask, unittest.TestCase):
    """B-T01/B-T02: an acknowledgment holds for exact task inputs; renewal keeps it, changed inputs clear it."""

    def setUp(self):
        self.build({"design": "People book a lesson.\n"})
        self.design_store = self.store(self.repo)
        context = self.design_store.context("acme", "booking")
        self.r2 = self.design_store.save("acme", "booking", expect_revision=1, expect_digest=context.current.digest,
                                         reason="draft two", parts={"design": "People book two lessons.\n"},
                                         today=TODAY)

    def task_state(self) -> dict:
        shown = self.oah("design", "task", "t1", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        return json.loads(shown.stdout)

    def acknowledge(self) -> None:
        carried = self.oah("design", "carry", "t1", "--agent", "claude", "--revision", "2", "--digest",
                           self.r2.digest, "--reason", "r2 rewords only")
        self.assertEqual(carried.returncode, 0, carried.stderr)

    def rebind(self, text: str) -> subprocess.CompletedProcess:
        section = design.parse_section((self.repo / PACKET).read_text(encoding="utf-8"))
        git_commit(self.repo, {PACKET: design.replace_section(text, section)}, "packet change")
        return self.oah("start", "--task", "t1", "--agent", "claude", "--packet", PACKET)

    def test_ttl_renewal_keeps_the_acknowledgment(self):
        self.assertEqual(self.task_state()["state"], "needs-review")
        self.acknowledge()
        before = self.task_state()
        self.assertEqual(before["state"], "current")
        renewed = self.oah("renew", "--task", "t1", "--agent", "claude")
        self.assertEqual(renewed.returncode, 0, renewed.stderr)
        after = self.task_state()
        self.assertEqual(after["state"], "current")
        self.assertEqual((after["instance"], after["record"]["evidence_key"]),
                         (before["instance"], before["record"]["evidence_key"]))

    def changed_inputs_need_a_fresh_decision(self, text: str) -> None:
        self.acknowledge()
        before = self.task_state()["record"]
        refused = self.rebind(text)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("OAH_DESIGN_NEEDS_REVIEW", refused.stderr)  # the old acknowledgment does not carry over
        self.assertIn("for these task inputs", refused.stderr)
        self.assertEqual(self.task_state()["record"]["evidence_key"], before["evidence_key"])  # nothing changed
        fresh = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", PACKET, "--draft-revision", "2",
                         "--draft-digest", self.r2.digest, "--draft-reason", "checked against the new inputs")
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        after = self.task_state()
        self.assertEqual(after["state"], "current")
        self.assertNotEqual(after["record"]["task"]["basis"], before["task"]["basis"])
        self.assertEqual(after["record"]["task"]["draft_ack"]["basis"], after["record"]["task"]["basis"])

    def test_a_raised_intent_needs_a_fresh_decision(self):
        self.changed_inputs_need_a_fresh_decision(
            packet_text(revision=2, acs=("every changed file is listed", "a new condition")))

    def test_an_expanded_scope_needs_a_fresh_decision(self):
        self.changed_inputs_need_a_fresh_decision(packet_text(scope=("src", "lib")))


class ExpiryResumeTest(DesignTask, unittest.TestCase):
    """B-T02/B-T13: an exact resume after expiry keeps the task instance and evidence key; a finished or released
    task's reused label is a new instance."""

    def lease_file(self) -> pathlib.Path:
        return self.state_dir / "leases.json"

    def expire(self) -> None:
        path = self.lease_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        for lease in data["leases"]:
            lease["expires_at"] = past
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def instance_and_key(self) -> tuple[str, str]:
        data = json.loads(self.oah("design", "task", "t1", "--json").stdout)
        return data["instance"], data["record"]["evidence_key"]

    def test_expired_open_lease_cannot_publish_a_carry(self):
        self.build({"design": "People book a lesson.\n"})
        store = self.store(self.repo)
        current = store.context("acme", "booking").current
        newer = store.save("acme", "booking", expect_revision=1, expect_digest=current.digest,
                           reason="wording", parts={"request": "Choose a lesson.\n"}, today=TODAY)
        self.expire()
        before = {name: (self.state_dir / name).read_bytes() for name in ("leases.json", "events.jsonl")}
        carried = self.oah("design", "carry", "t1", "--agent", "claude", "--revision", "2", "--digest",
                           newer.digest, "--reason", "wording only")
        self.assertEqual(carried.returncode, 1, carried.stdout + carried.stderr)
        self.assertIn("OAH_LEASE_EXPIRED", carried.stderr)
        self.assertEqual({name: (self.state_dir / name).read_bytes() for name in before}, before)

    def test_exact_resume_after_expiry_keeps_the_evidence_identity(self):
        self.build({"design": "People book a lesson.\n"})
        before = self.instance_and_key()
        self.expire()
        resumed = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", PACKET)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.instance_and_key(), before)
        started = [event for event in self.events("lease_started")]
        self.assertIn("resume_from", started[-1])
        self.assertEqual(set(started[-1]["resume_from"]), {"expired_lease_id", "expired_event_transaction",
                                                           "prior_record_digest"})

    def test_a_released_task_label_starts_a_new_instance(self):
        self.build({"design": "People book a lesson.\n"})
        before = self.instance_and_key()
        released = self.oah("release", "--task", "t1", "--agent", "claude", "--reason", "restart")
        self.assertEqual(released.returncode, 0, released.stderr)
        again = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", PACKET)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotEqual(self.instance_and_key()[0], before[0])


class ClosedTaskTest(DesignTask, unittest.TestCase):
    """B-T05/B-T06: finish keeps the full typed binding for land; origin-only and released epochs never bind."""

    def finish(self) -> subprocess.CompletedProcess:
        return self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "s", "--return",
                        str(self.returned()), "--allow-missing-wiki-maintenance")

    def test_finish_then_offline_land_judgement(self):
        from oah import land
        from oah.lease import Store

        self.build({"design": "People book a lesson.\n"})
        lease = next(item for item, _stale in Store(self.state_dir).snapshot() if item.task == "t1")
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        view = design.task_view(self.state_dir, "t1")
        self.assertIsNone(view.lease)
        self.assertEqual(view.closed, "finished")
        self.assertEqual(view.record.to_json(), lease.design)
        judged = design.judge_task(self.state_dir, "t1", root=self.repo)
        self.assertEqual(judged.state, "current")
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()
        _evidence, check = land._design_check(self.repo, self.state_dir, "t1", head, lease.binding())
        self.assertEqual(check.state, land.PASSED, check)

    def test_a_released_epoch_is_not_landable(self):
        from oah import land
        from oah.lease import Store

        self.build({"design": "People book a lesson.\n"})
        lease = next(item for item, _stale in Store(self.state_dir).snapshot() if item.task == "t1")
        self.assertEqual(self.oah("release", "--task", "t1", "--agent", "claude", "--reason", "stop").returncode, 0)
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()
        _evidence, check = land._design_check(self.repo, self.state_dir, "t1", head, lease.binding())
        self.assertNotEqual(check.state, land.PASSED)

    def test_finished_task_retains_exact_draft_and_unaffected_carry_without_new_work_authority(self):
        self.build({"design": "People book a lesson.\n"})
        self.assertEqual(self.finish().returncode, 0)
        original = design.task_view(self.state_dir, "t1").record
        store = self.store(self.repo)
        context = store.context("acme", "booking")
        draft = store.save("acme", "booking", expect_revision=1, expect_digest=context.current.digest,
                           reason="draft wording", parts={"design": "People book a lesson at a chosen time.\n"},
                           today=TODAY)
        self.assertEqual(design.judge_task(self.state_dir, "t1", root=self.repo).state, "needs-review")
        acknowledged = self.oah("design", "carry", "t1", "--agent", "claude", "--revision", "2", "--digest",
                                draft.digest, "--reason", "wording does not affect completed work")
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stdout + acknowledged.stderr)
        self.assertEqual(design.judge_task(self.state_dir, "t1", root=self.repo).state, "current")
        store.accept("acme", "booking", revision=2, digest=draft.digest, reason="accepted wording", today=TODAY)
        self.assertEqual(design.judge_task(self.state_dir, "t1", root=self.repo).state, "stale")
        carried = self.oah("design", "carry", "t1", "--agent", "claude", "--revision", "2", "--digest",
                          draft.digest, "--reason", "completed behavior is unchanged")
        self.assertEqual(carried.returncode, 0, carried.stdout + carried.stderr)
        closed = design.task_view(self.state_dir, "t1")
        self.assertIsNone(closed.lease)
        self.assertEqual(closed.closed, "finished")
        self.assertEqual(closed.record.identity, original.identity)
        self.assertEqual(closed.record.basis, original.basis)
        self.assertEqual(design.judge_task(self.state_dir, "t1", root=self.repo).state, "current")
        refused = self.oah("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "high")
        self.assertEqual(refused.returncode, 1)
        self.assertFalse(self.record.exists())

    def test_a_tampered_closed_carry_target_is_a_problem_not_a_binding(self):
        self.build({"design": "People book a lesson.\n"})
        self.assertEqual(self.finish().returncode, 0)
        before = design.task_view(self.state_dir, "t1").record
        with (self.state_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "design_carried", "task": "t1", "design_schema": 2,
                                     "design": before.to_json(), "target": {
                                         "lease_id": "x" * 16, "closing_transaction_id": "y" * 32,
                                         "expected_design_record_digest": "sha256:" + "0" * 64}}) + "\n")
        view = design.task_view(self.state_dir, "t1")
        self.assertIsNotNone(view.problem)
        self.assertNotEqual(design.judge_task(self.state_dir, "t1", root=self.repo).state, "current")

    def test_a_linked_packet_alone_never_supplies_a_binding(self):
        self.build({"design": "People book a lesson.\n"})
        linked = self.oah("packet", "new", "t2", "--design", "acme/booking")
        self.assertEqual(linked.returncode, 0, linked.stdout + linked.stderr)
        view = design.task_view(self.state_dir, "t2")
        self.assertIsNone(view.record)
        self.assertNotEqual(design.judge_task(self.state_dir, "t2", root=self.repo).state, "current")


class GeneratedOutputTest(DesignTask, unittest.TestCase):
    """B-T03/B-T04: only byte-exact generated outputs of the bound design leave ordinary scope."""

    def setUp(self):
        self.build({"design": "People book a lesson.\n"})
        from oah.lease_cli import _wiki_settings

        self.settings = _wiki_settings(self.repo)
        lease, _judged = self.judged()
        self.record = lease.design_record()
        view = design.task_view(self.state_dir, "t1", lease=lease)
        self.baseline = view.epoch.get("output_baseline")

    def outputs(self, changed):
        return design.generated_outputs(self.repo, self.settings, self.record, changed, self.baseline)

    def test_project_json_and_other_projects_always_stay_ordinary(self):
        from oah.design import fs

        rel = fs.project_record_rel("acme")
        path = self.repo / rel
        path.write_text(path.read_text(encoding="utf-8").replace("Acme", "Acme Ltd"), encoding="utf-8")
        found = self.outputs([rel])
        self.assertEqual(found["verified"], [])
        self.assertIn("project.json always needs explicit scope", found["ordinary"][0]["reason"])

    def test_a_list_row_injection_with_intact_markers_is_ordinary(self):
        from oah.design import fs

        rel = fs.project_page_rel(self.settings.dir, "acme")
        path = self.repo / rel
        text = path.read_text(encoding="utf-8")
        marker = next(line for line in text.splitlines() if line.startswith("- "))
        path.write_text(text.replace(marker, marker + "\n- [evil](x.md) injected row"), encoding="utf-8")
        found = self.outputs([rel])
        self.assertEqual(found["verified"], [])

    def test_the_exact_generated_design_page_is_verified(self):
        from oah.design import fs

        rel = fs.design_page_rel(self.settings.dir, "acme", "booking")
        found = self.outputs([rel])
        self.assertEqual([item["kind"] for item in found["verified"]], ["design-page"])
        path = self.repo / rel
        path.write_text(path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
        found = self.outputs([rel])
        self.assertEqual(found["verified"], [])


class LeaseDocumentTest(DesignTask, unittest.TestCase):
    """B-T10/B-T11: a malformed design value of t1 is isolated, preserved and never normalizes healthy t2."""

    def setUp(self):
        self.build({"design": "People book a lesson.\n"})
        packet = (self.repo / PACKET).read_text(encoding="utf-8")
        section = design.parse_section(packet)
        git_commit(self.repo, {".oah/tasks/t2/PACKET.md": design.replace_section(packet_text("t2", scope=("lib",)),
                                                                                 section)}, "packet t2")
        started = self.oah("start", "--task", "t2", "--agent", "claude", "--packet", ".oah/tasks/t2/PACKET.md")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.path = self.state_dir / "leases.json"

    def corrupt_t1(self, value: str) -> tuple[str, str]:
        """Replace t1's design value with `value` and respace t2's (noncanonical, still valid); return t2's text."""
        from oah.lease import read_lease_document

        text = self.path.read_text(encoding="utf-8")
        document = read_lease_document(text.encode("utf-8"), self.path)
        first, second = (document.record("t1"), document.record("t2"))
        t1_design = first.design.raw
        t2_design = second.design.raw
        spaced = t2_design.replace('": ', '":   ')
        text = text.replace(t1_design, value, 1).replace(t2_design, spaced, 1)
        self.path.write_text(text, encoding="utf-8")
        return spaced, text

    def test_invalid_values_are_isolated_and_healthy_bytes_survive_a_write(self):
        from oah.lease import Store

        deep = "[" * 40 + "]" * 40
        for value, code in ((deep, "too-deep"), ("1" + "0" * 30, "number-too-long"), ("null", "null"),
                            ('{"version": 2}', "not-a-task-binding")):
            with self.subTest(code):
                self.setUp()
                spaced, original = self.corrupt_t1(value)
                leases = {lease.task: lease for lease, _stale in Store(self.state_dir).snapshot()}
                self.assertTrue(leases["t1"].design_invalid)
                self.assertEqual(leases["t1"].design_field.code, code)
                self.assertFalse(leases["t2"].design_invalid)
                status = self.oah("status", "--json")
                self.assertEqual(status.returncode, 0, status.stderr)
                renewed = self.oah("renew", "--task", "t2", "--agent", "claude")
                self.assertEqual(renewed.returncode, 0, renewed.stderr)
                after = self.path.read_text(encoding="utf-8")
                self.assertIn(spaced, after)  # t2's own design bytes, exactly
                self.assertNotIn(value if value != "null" else '"design": null', after)
                kept = self.state_dir / "design-invalid" / (hashlib.sha256(original.encode()).hexdigest() + ".raw")
                self.assertEqual(kept.read_bytes(), original.encode("utf-8"))
                refused = self.oah("design", "task", "t1", "--check")
                self.assertEqual(refused.returncode, 1)
                self.assertEqual(json.loads(self.oah("design", "task", "t1", "--json").stdout)["state"], "invalid")

    def test_duplicate_design_keys_are_invalid_not_last_wins(self):
        from oah.lease import Store, read_lease_document

        text = self.path.read_text(encoding="utf-8")
        document = read_lease_document(text.encode("utf-8"), self.path)
        raw = document.record("t1").design.raw
        self.path.write_text(text.replace('"design": ' + raw, '"design": ' + raw + ', "design": ' + raw, 1),
                             encoding="utf-8")
        leases = {lease.task: lease for lease, _stale in Store(self.state_dir).snapshot()}
        self.assertEqual(leases["t1"].design_field.code, "duplicate-design-keys")

    def test_failed_invalid_evidence_write_does_not_block_a_retry(self):
        import errno
        import os
        from oah.atomic import StorageError
        from oah.lease import Store

        _spaced, original = self.corrupt_t1("null")
        write = os.write
        calls = []

        def interrupted(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return write(fd, data[:7])
            raise OSError(errno.ENOSPC, "controlled preservation failure")

        if os.name == "nt":
            from oah.design import task_context as tc
            native_write = tc._WindowsIO.write

            def interrupted_native(native, handle, data):
                native_write(native, handle, data[:7])
                raise OSError(errno.ENOSPC, "controlled native preservation failure")

            fault = mock.patch.object(tc._WindowsIO, "write", interrupted_native)
        else:
            fault = mock.patch("oah.lease.os.write", side_effect=interrupted)
        with fault:
            with self.assertRaises(StorageError):
                Store(self.state_dir).renew(task="t2", agent="claude", ttl_hours=1)
        Store(self.state_dir).renew(task="t2", agent="claude", ttl_hours=1)
        kept = self.state_dir / "design-invalid" / (hashlib.sha256(original.encode()).hexdigest() + ".raw")
        self.assertEqual(kept.read_bytes(), original.encode())

    def test_a_pending_transaction_recovers_with_the_exact_bytes(self):
        spaced, _text = self.corrupt_t1("null")
        state_text = self.path.read_text(encoding="utf-8")
        event = {"event": "note", "task": "t2", "transaction_id": "t" * 32}
        (self.state_dir / "transaction.json").write_text(
            '{"event": ' + json.dumps(event, sort_keys=True) + ', "state": ' + state_text.rstrip("\n")
            + ', "transaction_id": "' + "t" * 32 + '", "version": 1}\n', encoding="utf-8")
        renewed = self.oah("renew", "--task", "t2", "--agent", "claude")  # the dispatcher's next write recovers
        self.assertEqual(renewed.returncode, 0, renewed.stderr)
        self.assertFalse((self.state_dir / "transaction.json").exists())
        self.assertIn(spaced, self.path.read_text(encoding="utf-8"))
        notes = [item for item in self.events("note") if item.get("transaction_id") == "t" * 32]
        self.assertEqual(len(notes), 1)  # replayed exactly once


class ReadoutTest(DesignTask, unittest.TestCase):
    """Status, intent show and the session-start hook name the bound design from the records they read."""

    def test_status_intent_and_session_start_name_the_design(self):
        self.build({"design": "People book a lesson.\n"})
        status = self.oah("status", "--json")
        lease = next(item for item in json.loads(status.stdout)["leases"] if item["task"] == "t1")
        self.assertEqual(lease["design_state"], "current")
        self.assertIn(" design=current", self.oah("status").stdout)
        shown = self.oah("intent", "show", "t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("design=current DESIGN acme/booking@r1", shown.stdout)
        hook = subprocess.run([sys.executable, "-m", "oah", "hook", "session-start"], cwd=self.repo, input=b"",
                              capture_output=True, env=self.env, timeout=60)
        text = hook.stdout.decode("utf-8")
        self.assertIn("design=acme/booking@r1", text)
        self.assertIn("oah design task <task>", text)

    def test_hook_keeps_normal_design_labels_and_invalid_records_visible(self):
        self.build({"design": "People book a lesson.\n"})
        path = self.state_dir / "leases.json"
        original = path.read_bytes()
        for project, name in (("shop", "checkout-flow"), ("academy-booking", "lesson-planner"), (None, None)):
            with self.subTest(project=project, name=name):
                document = json.loads(original)
                if project is None:
                    document["leases"][0]["design"] = None
                else:
                    document["leases"][0]["design"]["binding"].update(project=project, design=name)
                path.write_text(json.dumps(document), encoding="utf-8")
                hooked = self.oah("hook", "session-start")
                self.assertIn("- task=t1 agent=claude", hooked.stdout)
                self.assertNotIn("OAH_SESSION_START_UNSAFE", hooked.stdout)
                self.assertIn(name if name is not None else "invalid", hooked.stdout)

    def test_intent_show_explains_unknown_design_history(self):
        self.build({"design": "People book.\n"})
        with (self.state_dir / "events.jsonl").open("a") as handle:
            handle.write("unreadable event\n")
        shown = self.oah("intent", "show", "t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("design=unknown", shown.stdout)
        self.assertIn(design.ORIGIN_UNKNOWN_CODE, shown.stdout)
        self.assertIn("next:", shown.stdout)

    def test_missing_design_error_does_not_repeat_its_code(self):
        import dataclasses

        self.build({"design": "People book.\n"})
        lease, _judged = self.judged()
        record = lease.design_record()
        missing = dataclasses.replace(record, binding=dataclasses.replace(record.binding, design="not-registered"))
        judged = design.read_context(missing.binding, root=self.repo, task=missing)
        self.assertEqual(judged.state, "missing")
        self.assertEqual(str(judged.error("dispatch", "t1", "claude")).count(judged.code), 1)


SRC_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def design_fix_project(test: unittest.TestCase):
    """test_fix's FixProject (read-only reuse) whose task t1 follows an accepted design on its branch."""
    from oah.design import open_store
    from oah.wiki import notes
    from tests.test_design_store import Clock
    from tests.test_fix import FixProject

    scaffold = {f"docs/wiki/{rel}": data for rel, data in notes.scaffold_files(TODAY).items()}
    project = FixProject(test, files=scaffold, packet=packet_text(scope=("src", ".oah/tasks/t1")),
                         config="\n[models]\nfetch_scores = false\n")
    store = open_store(project.repo, env={"OAH_STATE_DIR": str(project.state)}, now=Clock())
    store.add_project("acme", title="Acme", event="ev-project", today=TODAY)
    r1 = store.new("acme", "booking", title="Booking", parts={"design": "People book a lesson.\n"}, event="ev-r1",
                   today=TODAY)
    store.accept("acme", "booking", revision=1, digest=r1.digest, reason="ok", today=TODAY)
    section = design.PacketDesign.of(store.context("acme", "booking").binding("accepted"))
    packet = (project.repo / PACKET).read_text(encoding="utf-8")
    project.commit({PACKET: design.replace_section(packet, section)}, "design bound")
    env = dict(project.env, PYTHONPATH=SRC_DIR)
    bound = subprocess.run([sys.executable, "-m", "oah", "start", "--task", "t1", "--agent", "claude", "--packet",
                            PACKET], cwd=project.repo, env=env, capture_output=True, text=True, timeout=120)
    test.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
    project.design_store = store
    return project


class ReviewDesignTest(unittest.TestCase):
    """B-T13 (test_review) and SC4-P4-R through real review rounds of a design-bound task."""

    def test_simulated_origin_flows_through_all_review_retirement_boundaries(self):
        self.origin_round(interrupt=False)

    def test_interrupted_onstack_retirement_never_publishes_a_counted_review(self):
        self.origin_round(interrupt=True)

    @unittest.skipIf(sys.platform == "win32", "POSIX snapshot replacement")
    def test_origin_snapshot_replacement_retains_both_containers_without_counting(self):
        for phase in ("capture", "retirement"):
            with self.subTest(phase=phase):
                self.origin_round(interrupt=False, replacement=phase)

    def test_identity_refusal_retains_snapshot_even_when_the_next_anchor_succeeds(self):
        from oah.design import task_context as tc
        from oah.review import ReviewError, receipts, snapshot

        kinds = ("native-unavailable", "held-named") if sys.platform != "win32" else ("native-unavailable",)
        for kind in kinds:
            with self.subTest(kind=kind):
                project = design_fix_project(self)
                identity = snapshot._container_object
                allocated, refused, observed = [], [], {}

                def unavailable_once(parent, *, empty=False):
                    if empty:
                        allocated.append(parent)
                    elif not refused:
                        refused.append(parent)
                        if kind == "native-unavailable":
                            raise tc.InstallRefused("temporary native identity read refusal")
                        with self.named_identity_refusal(parent, observed):
                            return identity(parent)
                    return identity(parent, empty=empty)

                with mock.patch.object(snapshot, "_container_object", unavailable_once):
                    with self.assertRaises(ReviewError) as caught:
                        project.review()
                self.assertEqual(caught.exception.code, "OAH_REVIEW_SNAPSHOT_RETAINED")
                self.assertEqual(len(allocated), 1)
                self.assertEqual(len(refused), 1)
                self.assertTrue(allocated[0].is_dir(), "an identity refusal must keep its allocation")
                self.assertEqual((allocated[0] / "tree/src/app.py").read_bytes(),
                                 (project.repo / "src/app.py").read_bytes())
                self.assertEqual(project.calls(), [])
                self.assertFalse(any(line.get("counted") for line in receipts.read_round_log(project.state, "t1")))
                if kind == "held-named":
                    self.assertTrue(observed["replacement"].is_dir())
                    self.assertEqual(observed["marker"].read_bytes(), b"existing fixture contents\n")
        if sys.platform != "win32":
            with self.subTest(origin=True):
                self.origin_round(interrupt=False, identity_refusal=True)

    def test_creation_identity_refusals_preserve_the_original_cause(self):
        import stat

        from oah.design import task_context as tc
        from oah.review import snapshot

        parent = make_tmp_dir(self)
        created: dict[str, int | str] = {"dev": 1, "ino": 2, "type": stat.S_IFDIR, "mode": 0o700, "uid": 3, "gid": 4}
        errors = (snapshot.Retained("held/named mismatch"), tc.InstallRefused("native refusal"),
                  OSError("identity read failed"), ValueError("invalid identity"), RuntimeError("identity failed"),
                  TypeError("identity comparison failed"), MemoryError(), KeyboardInterrupt(), SystemExit())
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(snapshot, "_container_object", side_effect=error):
                with self.assertRaises(BaseException) as caught:
                    snapshot.require_creation_identity(parent, created)
                self.assertIsInstance(caught.exception, snapshot.CreationRetained)
                self.assertIs(caught.exception.__cause__, error)

        error = RuntimeError("creation identity cannot be compared")

        class Unreadable(dict[str, int | str]):
            def __getitem__(self, key):
                raise error

        class Incomparable(dict[str, int | str]):
            def __ne__(self, other):
                raise error

        for phase in ("creation", "recorded", "current"):
            with self.subTest(phase=phase), \
                    mock.patch.object(snapshot, "_container_object", return_value=Incomparable(created)):
                with self.assertRaises(BaseException) as caught:
                    snapshot.require_creation_identity(parent, Unreadable(created) if phase == "creation" else created,
                        *((Incomparable(created),) if phase == "recorded" else ()), current=phase == "current")
                self.assertIsInstance(caught.exception, snapshot.CreationRetained)
                self.assertIs(caught.exception.__cause__, error)

    def named_identity_refusal(self, parent, observed):
        from oah.review import snapshot

        lstat = snapshot.os.lstat

        def alternate(path, *args, **kwargs):
            if pathlib.Path(path) == parent and sys._getframe(1).f_code.co_name == "_container_object" \
                    and not observed.get("replacement"):
                original = parent.with_name(parent.name + "-original")
                replacement = parent.with_name(parent.name + "-replacement")
                parent.rename(original)
                shutil.copytree(original, parent)
                info = lstat(parent)
                marker = parent / "keep.txt"
                marker.write_bytes(b"existing fixture contents\n")
                parent.rename(replacement)
                original.rename(parent)
                observed.update(replacement=replacement, marker=replacement / "keep.txt")
                return info
            return lstat(path, *args, **kwargs)

        return mock.patch.object(snapshot.os, "lstat", alternate)

    @unittest.skipUnless(pathlib.Path("/var/tmp").is_dir(), "unresolved /var/tmp control")
    def test_design_reviews_dispose_and_count_under_unresolved_var_tmp(self):
        from oah.review import receipts

        for origin in (False, True):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory(prefix="oah-birth-", dir="/var/tmp") as folder:
                temporary = pathlib.Path(folder)
                if origin:
                    self.origin_round(interrupt=False, temp_root=temporary)
                else:
                    project = design_fix_project(self)
                    project.temp_root = temporary
                    result = project.review()
                    self.assertEqual(result.code, "OAH_REVIEW_CONVERGED", result.message)
                    self.assertTrue(receipts.read_round_log(project.state, "t1")[-1]["counted"])
                self.assertEqual(list(temporary.iterdir()), [])

    def test_whole_inline_reviews_accept_clone_names_without_retirement_pages(self):
        import os

        for kind in ("plain", "link", "backslash"):
            with self.subTest(kind=kind):
                project = design_fix_project(self)
                if kind == "link":
                    os.symlink("app.py", project.repo / "src" / "link.py")
                if kind == "backslash":
                    if os.name == "nt":
                        continue
                    (project.repo / "src" / "back\\slash.py").write_text("x = 1\n", encoding="utf-8")
                if kind != "plain":
                    project.commit({})
                reviewed = project.review()
                self.assertEqual(reviewed.code, "OAH_REVIEW_CONVERGED", reviewed.message)
                self.assertFalse((project.state / "design-retirement").exists())
                self.assertEqual(list(project.temp_root.iterdir()), [])

    def origin_round(self, *, interrupt: bool, replacement: str | None = None, identity_refusal: bool = False,
                     temp_root: pathlib.Path | None = None) -> None:
        from oah.design import task_context as tc
        from oah.design.binding import repo_identity
        from oah.lease import Store
        from oah.review import runner

        project = design_fix_project(self)
        if temp_root is not None:
            project.temp_root = temp_root
        project.commit({"probe.txt": "written by the fake reviewer\n"})
        prepare, execute = runner.call.prepare, runner.call.execute
        capture_final = runner._SnapshotDisposal.capture_final
        writer = Store(project.state)
        observed = {}

        def replace_container(parent):
            original = parent.with_name(parent.name + "-original")
            parent.rename(original)
            shutil.copytree(original, parent)
            marker = parent / "keep.txt"
            marker.write_bytes(b"existing fixture contents\n")
            observed.update(original=original, replacement=parent, marker=marker)

        def prepared(*args, **kwargs):
            result = prepare(*args, **kwargs)
            disposal = tc.RETIREMENT_CONTEXT.get()
            self.assertIsNotNone(disposal)
            lease = writer.inspect("t1")
            root = pathlib.Path(result.cwd)
            if replacement == "capture":
                replace_container(root.parent)
            live = tc.LiveRoots(tc.current_role_roots(lease, project.state, root, retirement=True))
            self.addCleanup(live.close)
            record = tc.origin_record(
                transport={"read_plan": {"files": [], "manifest_sha256": "sha256:" + "c" * 64}, "view": {}},
                root=root, repo=repo_identity(project.repo), run=result.run_id,
                task_identity=lease.design["task_identity"], basis=lease.design["basis"],
                epoch={"lease_id": lease.id, "generation": lease.generation},
                profile={"operation": "inspect", "fixture": "simulated-producer-proof"},
                h0=tc.git_head(root), policy={}, nonce="01" * 32, continuity={}, boot={},
                root_identity=live.identity("receiving-root"), live_roots=live.facts("01" * 32))
            record = disposal.adopt(record)
            start = writer.register_transport_origin(task="t1", lease_id=lease.id,
                                                     generation=lease.generation, record=record)
            origin = start["record_digest"]
            observed.update(origin=origin, root=root, record=record,
                            expected=writer.observe_transport(task="t1", origin=origin, expected=start,
                                                               kind="spawn-intent", fact={}))
            self.assertEqual(project.calls(), [])
            return result

        def completed(*args, **kwargs):
            result = execute(*args, **kwargs)
            self.assertIsNotNone(tc.RETIREMENT_CONTEXT.get())
            record, origin = observed["record"], observed["origin"]
            original = {"origin": origin, **{key: record[key] for key in
                        ("run", "task_identity", "task_basis_digest", "epoch", "root_digest", "manifest")}}
            latest, _ = writer.observe_transport_exit(original=original, expected=observed["expected"],
                        kind="exit-observed", fact={"quiescent": True, "return_code": 0},
                        live_proof=mock.Mock(holds=lambda value: value == origin))
            latest = writer.observe_transport(task="t1", origin=origin, expected=latest,
                                               kind="delivery-complete", fact={"fixture": True})
            writer.observe_transport(task="t1", origin=origin, expected=latest, kind="verified-cleaned",
                                       fact={"head": tc.git_head(observed["root"])["head"]})
            return result

        def captured(disposal, parent, source, copied):
            if identity_refusal:
                with self.named_identity_refusal(parent, observed):
                    capture_final(disposal, parent, source, copied)
            else:
                capture_final(disposal, parent, source, copied)
            if replacement == "retirement":
                replace_container(parent)
            if interrupt:
                observe, faulted = Store.observe_transport, []

                def fail_progress(store, **options):
                    if options["kind"] == "retirement-progress" and not faulted:
                        faulted.append(options["fact"]["path"])
                        raise OSError("controlled progress append failure after real unlink")
                    return observe(store, **options)

                view = tc.transport_views(project.state)[observed["origin"]]
                with mock.patch.object(Store, "observe_transport", fail_progress):
                    first = runner.retire_snapshot(project.state, view, parent)
                self.assertEqual(first[0], "retained")
                self.assertEqual(len(faulted), 1)
                self.assertFalse(parent.joinpath(*faulted[0]).exists())
            observed["disposition"] = disposal.disposition

        with mock.patch.object(runner.call, "prepare", prepared), mock.patch.object(runner.call, "execute", completed), \
                mock.patch.object(runner._SnapshotDisposal, "capture_final", captured):
            result = None
            if replacement == "capture" or identity_refusal:
                from oah.review import ReviewError

                with self.assertRaises(ReviewError) as caught:
                    project.review()
                self.assertEqual(caught.exception.code, "OAH_REVIEW_SNAPSHOT_RETAINED")
                if replacement == "capture":
                    self.assertFalse((project.state / "design-retirement").exists())
            else:
                result = project.review()
        if identity_refusal:
            self.assertTrue(observed["root"].parent.is_dir())
            self.assertEqual((observed["root"] / "probe.txt").read_bytes(), b"written by the fake reviewer\n")
            self.assertTrue(observed["replacement"].is_dir())
            self.assertEqual(observed["marker"].read_bytes(), b"existing fixture contents\n")
            self.assertEqual(project.names(), [])
            log = runner.receipts.read_round_log(project.state, "t1")
            self.assertEqual([line["event"] for line in log], ["start", "finish"])
            self.assertFalse(log[-1]["counted"])
            self.assertEqual(log[-1]["snapshot_disposition"], "retained")
            self.assertIsNone(tc.transport_views(project.state)[observed["origin"]].last("retired"))
            return
        if replacement is not None:
            self.assertEqual(observed["marker"].read_bytes(), b"existing fixture contents\n")
            self.assertTrue((observed["original"] / "tree/probe.txt").is_file())
            self.assertTrue(observed["replacement"].is_dir())
            self.assertEqual(project.names(), [])
            self.assertFalse(any(line.get("counted") for line in runner.receipts.read_round_log(project.state, "t1")))
            if replacement == "retirement":
                assert result is not None
                self.assertEqual(result.code, "OAH_REVIEW_SNAPSHOT_RETAINED", result.message)
                self.assertEqual(observed["disposition"][-1][0], "retained")
            return
        assert result is not None
        self.assertEqual(len(project.calls()), 1)
        view = tc.transport_views(project.state)[observed["origin"]]
        self.assertIsNone(view.problem)
        self.assertIsNotNone(view.last("retirement-checkpoint"))
        self.assertEqual(view.observations[-1]["kind"], "retired")
        self.assertFalse(observed["root"].parent.exists())
        self.assertEqual(list(project.temp_root.iterdir()), [])
        log = runner.receipts.read_round_log(project.state, "t1")
        if interrupt:
            self.assertEqual(observed["disposition"][-1][0], "disposed-noncounting")
            retired = view.last("retired")
            assert retired is not None
            self.assertFalse(retired["fact"]["counted"])
            self.assertEqual(result.code, "OAH_REVIEW_SNAPSHOT_NONCOUNTING", result.message)
            self.assertEqual(project.names(), [])
            self.assertEqual([line["event"] for line in log], ["start", "finish"])
            self.assertFalse(log[-1]["counted"])
            self.assertEqual(log[-1]["code"], "OAH_REVIEW_SNAPSHOT_NONCOUNTING")
        else:
            self.assertEqual(result.code, "OAH_REVIEW_CONVERGED", result.message)
            self.assertEqual(observed["disposition"][-1][0], "disposed")
            self.assertEqual(project.names(), ["R1.json"])
            self.assertTrue(log[-1]["counted"])

    def test_prelaunch_plan_failure_is_typed_and_spends_no_attempt(self):
        from oah.review import ReviewError, receipts, runner, snapshot

        project = design_fix_project(self)
        with mock.patch.object(runner._SnapshotDisposal, "prepare",
                               side_effect=snapshot.PlanUnavailable("allocation unavailable")):
            with self.assertRaises(ReviewError) as caught:
                project.review()
        self.assertEqual(caught.exception.code, "OAH_REVIEW_SNAPSHOT_FAILED")
        self.assertEqual(project.calls(), [])
        self.assertEqual(receipts.read_round_log(project.state, "t1"), [])
        self.assertEqual(list(project.temp_root.iterdir()), [])

    def test_final_capture_failure_closes_the_started_attempt_once(self):
        from oah.review import ReviewError, receipts, runner, snapshot

        project = design_fix_project(self)
        with mock.patch.object(runner._SnapshotDisposal, "capture_final",
                               side_effect=snapshot.Retained("final object changed")):
            with self.assertRaises(ReviewError) as caught:
                project.review()
        self.assertEqual(caught.exception.code, "OAH_REVIEW_SNAPSHOT_RETAINED")
        self.assertEqual(len(project.calls()), 1)
        lines = receipts.read_round_log(project.state, "t1")
        self.assertEqual([line["event"] for line in lines], ["start", "finish"])
        self.assertFalse(lines[-1]["counted"])
        self.assertEqual(lines[-1]["code"], "OAH_REVIEW_SNAPSHOT_RETAINED")
        self.assertEqual(lines[-1]["snapshot_disposition"], "legacy-disposed")
        self.assertIn("final object changed", lines[-1]["message"])
        self.assertEqual(receipts.load(project.state, "t1").entries, ())

    def test_child_error_keeps_its_reason_and_the_finalizer_outcome(self):
        from oah.review import receipts, runner

        project = design_fix_project(self)
        original = RuntimeError("original simulated child failure")

        def retained(disposal, _parent, _created):
            outcome = ("retained", "producer proof unavailable")
            disposal.disposition.append(outcome)
            return outcome

        with mock.patch.object(runner.call, "execute", side_effect=original), \
                mock.patch.object(runner._SnapshotDisposal, "__call__", retained):
            with self.assertRaises(RuntimeError) as caught:
                project.review()
        self.assertIs(caught.exception, original)
        lines = receipts.read_round_log(project.state, "t1")
        self.assertEqual([line["event"] for line in lines], ["start", "finish"])
        self.assertFalse(lines[-1]["counted"])
        self.assertEqual(lines[-1]["code"], "RuntimeError")
        self.assertIn("original simulated child failure", lines[-1]["message"])
        self.assertEqual(lines[-1]["snapshot_disposition"], "retained")
        self.assertEqual(lines[-1]["snapshot_reason"], "producer proof unavailable")
        self.assertEqual(len(list(project.temp_root.iterdir())), 1)

    def test_a_design_review_keeps_its_key_and_disposes_its_snapshot(self):
        project = design_fix_project(self)
        result = project.review()
        self.assertEqual(result.code, "OAH_REVIEW_CONVERGED", result.message)
        receipt = project.receipt(1)
        from oah.lease import Store

        lease = next(item for item, _stale in Store(project.state).snapshot() if item.task == "t1")
        self.assertEqual(receipt["design"]["evidence_key"], lease.design_record().key)
        self.assertEqual(list(project.temp_root.iterdir()), [])  # the proven no-origin branch disposed it
        prompt = project.calls()[-1]["prompt"]
        self.assertIn("People book a lesson.", prompt)

    def test_a_newer_draft_is_reviewed_but_never_converges(self):
        project = design_fix_project(self)
        context = project.design_store.context("acme", "booking")
        project.design_store.save("acme", "booking", expect_revision=1, expect_digest=context.current.digest,
                                  reason="draft", parts={"design": "People book two lessons.\n"}, today=TODAY)
        project.commit({}, "save the draft")  # a review reads a commit
        result = project.review()
        self.assertEqual(result.code, "OAH_DESIGN_NEEDS_REVIEW", result.message)
        prompt = project.calls()[-1]["prompt"]
        self.assertIn("People book a lesson.", prompt)
        self.assertIn("People book two lessons.", prompt)
        from oah.review import summary

        head = subprocess.run(["git", "-C", str(project.repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
        from oah.lease import Store

        lease = next(item for item, _stale in Store(project.state).snapshot() if item.task == "t1")
        judged = design.judge_task(project.state, "t1", root=project.repo)
        found = summary.convergence(project.repo, project.state, "t1", head, lease.binding(),
                                    design=design.review_evidence(judged))
        self.assertFalse(found.converged)


class LegacyPacketDesignTest(unittest.TestCase):
    def test_main_shaped_lease_with_free_form_design_can_resume_read_and_finish(self):
        for prose in ("A queue feeds the worker.", "- design: a queue in front of the worker",
                      "- project uid: the local project name", "- revision: second pass", "- digest: weekly email"):
            with self.subTest(prose=prose):
                self.legacy_lease(prose)

    def legacy_lease(self, prose: str) -> None:
        from oah import call, intent, packet
        from oah.lease import Store
        from oah.paths import repo_info
        from tests.test_intent_wiring import clean_env

        repo = make_git_repo(self)
        text = packet_text().replace("## Plan", f"## Design\n{prose}\n\n## Plan")
        git_commit(repo, {PACKET: text, "src/app.py": "x = 1\n"})
        state, home = make_tmp_dir(self), make_tmp_dir(self)
        binding = intent.parse(text).bind(PACKET)
        Store(state).start(task="t1", agent="claude", scope=("src",), note="main-started legacy lease",
                           ttl_hours=1, info=repo_info(repo), intent=binding)
        env = clean_env(home, OAH_STATE_DIR=str(state))

        def oah(*args):
            return subprocess.run([sys.executable, "-B", "-m", "oah", *args], cwd=repo, env=env,
                                  capture_output=True, text=True, timeout=60)

        self.assertEqual(packet.check_text(text, task="t1").errors, ())
        self.assertIsNone(call.read_packet(repo, repo / PACKET, parse_intent=intent.parse).design)
        resumed = oah("start", "--task", "t1", "--agent", "claude", "--packet", PACKET)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        returned = home / "return.txt"
        parsed = intent.parse(text)
        returned.write_text("\n".join(["Summary: done.", parsed.line(), *(f"{ac}: met — ok" for ac in parsed.acs)]) + "\n",
                            encoding="utf-8")
        finished = oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", "--return", str(returned),
                       "--allow-missing-wiki-maintenance", "--allow-structural")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertEqual((repo / PACKET).read_text(), text)
        self.assertNotIn("design_origin", (state / "events.jsonl").read_text())

    def test_complete_binding_without_generator_comment_is_still_structured(self):
        section = design.PacketDesign("acme", "a" * 32, "booking", 1, "sha256:" + "b" * 64)
        text = "\n".join([section.lines()[0], *section.lines()[2:]])
        self.assertEqual(design.parse_section(text), section)

    def test_generator_marker_keeps_malformed_binding_a_refusal(self):
        section = design.PacketDesign("acme", "a" * 32, "booking", 1, "sha256:" + "b" * 64)
        with self.assertRaises(design.SectionInvalid):
            design.parse_section(design.render_section(section).replace("- revision: 1", "- revision: second pass"))


class FixTargetOnlyTest(unittest.TestCase):
    """B-T10: an unrelated lease's writes during a design-bound correction never fail its outcome or normalize its
    own design bytes; the unrelated task's malformed design value is preserved, not repaired."""

    def test_unrelated_start_prune_and_roster_reordering_keep_the_correction_authorized(self):
        from oah.lease import Store, read_lease_document
        from oah.paths import repo_info
        from oah.review import fix
        from tests.test_fix import FIXED, applied

        project = design_fix_project(self)
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        Store(project.state, now=lambda: past).start(task="expired-other", agent="claude", scope=("docs",),
            note="old unrelated lease", ttl_hours=1, info=repo_info(project.repo))
        path = project.state / "leases.json"
        original = read_lease_document(path.read_bytes(), path).record("t1").raw
        judge, writes = fix._Attempt._judge, []

        def changed(attempt, *args, **kwargs):
            result = judge(attempt, *args, **kwargs)
            Store(project.state, lock_timeout=0).start(task="new-other", agent="claude", scope=("docs",),
                note="concurrent unrelated start", ttl_hours=1, info=repo_info(project.repo))
            document = read_lease_document(path.read_bytes(), path)
            self.assertIsNone(document.record("expired-other"))
            path.write_text('{"version":1,"leases":[' +
                            ",".join(record.raw for record in reversed(document.records)) + "]}\n",
                            encoding="utf-8")
            writes.append(True)
            return result

        with mock.patch.object(fix._Attempt, "_judge", changed):
            applied(self, project, {"write": FIXED})
        self.assertEqual(writes, [True])
        document = read_lease_document(path.read_bytes(), path)
        self.assertEqual(document.record("t1").raw, original)
        self.assertIsNotNone(document.record("new-other"))

    def test_unrelated_writes_keep_the_outcome_and_the_exact_design_bytes(self):
        from oah.lease import Store, read_lease_document
        from oah.paths import repo_info
        from oah.review import fix
        from tests.test_fix import FIXED, applied

        project = design_fix_project(self)
        Store(project.state).start(task="other", agent="claude", scope=("docs",), note="parallel", ttl_hours=1,
                                   info=repo_info(project.repo))
        path = project.state / "leases.json"
        text = path.read_text(encoding="utf-8")
        own = read_lease_document(text.encode("utf-8"), path).record("t1").design.raw
        spaced = own.replace('": ', '":  ')
        other = read_lease_document(text.encode("utf-8"), path).record("other").raw
        text = text.replace(own, spaced, 1).replace(other, other[:-1] + ', "design": ' + "[" * 40 + "]" * 40 + "}",
                                                     1)
        path.write_text(text, encoding="utf-8")
        judge = fix._Attempt._judge
        writes = []

        def changed(attempt, *args, **kwargs):
            result = judge(attempt, *args, **kwargs)
            Store(project.state, lock_timeout=0).renew(task="other", agent="claude", ttl_hours=2)
            writes.append(True)
            return result

        with mock.patch.object(fix._Attempt, "_judge", changed):
            applied(self, project, {"write": FIXED})
        self.assertEqual(writes, [True])
        after = path.read_text(encoding="utf-8")
        self.assertIn(spaced, after)  # the target's own noncanonical design bytes, exactly
        self.assertNotIn("[" * 40, after)  # the unrelated invalid value is enveloped, its bytes kept aside
        self.assertTrue(any((project.state / "design-invalid").iterdir()))


class CompletedCorrectionDesignChangeTest(unittest.TestCase):
    def test_finished_fix_with_changed_design_reports_completed_run_without_marking_delivery(self):
        from oah import call
        from oah.review import ReviewError
        from tests.test_fix import FIXED

        project = design_fix_project(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        stream = call._stream_correction

        def changed(*args, **kwargs):
            result = stream(*args, **kwargs)
            current = project.design_store.context("acme", "booking").current
            project.design_store.save("acme", "booking", expect_revision=current.revision,
                expect_digest=current.digest, reason="concurrent wording",
                parts={"design": "People book a lesson online.\n"}, today=TODAY)
            return result

        with mock.patch.object(call, "_stream_correction", changed), self.assertRaises(ReviewError) as caught:
            project.fix()
        self.assertEqual(caught.exception.code, "OAH_DESIGN_CHANGED_AFTER_RUN")
        self.assertIn("run completed", str(caught.exception))
        self.assertIn("delivery is not marked", str(caught.exception))
        self.assertEqual(len(project.fix_calls()), 1)
        run = sorted((project.state / "runs/t1").glob("r*/result.json"))[-1]
        result = json.loads(run.read_bytes())
        self.assertEqual((result["status"], result["exit_code"]), ("finished", 0))
        delivered = json.loads((run.parent / "design.json").read_bytes())
        self.assertFalse(delivered.get("delivered"))
        self.assertFalse((delivered.get("receiving") or {}).get("delivered"))


class CorrectionAuthorityTest(DesignTask, unittest.TestCase):
    def test_launch_guard_refuses_pending_transaction_without_recovering_it(self):
        from oah.journal import PendingTransaction
        from oah.lease import Store
        from oah.review import fix

        self.build({"design": "People book.\n"})
        store = Store(self.state_dir)
        stamp, lease = fix._target(store.capture_document(), "t1")
        task = fix._Task("t1", self.state_dir, lease, self.repo, json.dumps(lease.to_json()), stamp)
        pending = b'{"pending": "must be recovered before dispatch"}\n'
        store.journal.path.write_bytes(pending)
        with self.assertRaises(PendingTransaction), fix._design_launch(task):
            self.fail("the pending transaction granted launch authority")
        self.assertEqual(store.journal.path.read_bytes(), pending)
        self.assertFalse(self.record.exists())

    def test_malformed_open_task_replay_refuses_before_launch(self):
        from oah.atomic import append_jsonl
        from oah.lease_cli import lease_design

        self.build({"design": "People book.\n"})
        lease, _ = self.judged()
        append_jsonl(self.state_dir / "events.jsonl", {"event": "design_carried", "task": "t1",
                      "design_schema": 2, "target": None, "design": {}})
        self.assertNotEqual(design.judge_task(self.state_dir, "t1", root=self.repo).state, "current")
        self.assertNotEqual(lease_design(lease, self.state_dir).state, "current")
        refused = self.oah("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "high")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(design.judge_task(self.state_dir, "other", root=self.repo).state, "unbound")

    def test_fix_recaptures_target_before_the_child_can_write(self):
        from oah import call
        from oah.lease import Store
        from oah.review import ReviewError
        from tests.test_fix import FIXED

        project = design_fix_project(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        execute = call.execute

        def changed(prepared, **kwargs):
            Store(project.state).renew(task="t1", agent="claude", ttl_hours=1,
                                       scope=("src", ".oah/tasks/t1", "new-scope"))
            return execute(prepared, **kwargs)

        with mock.patch.object(call, "execute", changed), self.assertRaises(ReviewError) as caught:
            project.fix()
        self.assertEqual(caught.exception.code, "OAH_LEASE_CHANGED")
        self.assertEqual(project.fix_calls(), [])

    def test_unknown_core_fields_survive_renewal_and_enter_the_target_stamp(self):
        from oah.lease import Store, read_lease_document
        from oah.review.fix import _target

        self.build({"design": "People book.\n"})
        path = self.state_dir / "leases.json"
        data = json.loads(path.read_bytes())
        data["leases"][0]["design#future"] = {"keep": "first"}
        data["leases"][0]["design#0"] = "real field"
        path.write_text(json.dumps(data))
        document = read_lease_document(path.read_bytes(), path)
        before, _ = _target(document, "t1")
        self.assertEqual(document.record("t1").core["design#future"], {"keep": "first"})
        Store(self.state_dir).renew(task="t1", agent="claude", ttl_hours=2)
        fresh = json.loads(path.read_bytes())["leases"][0]
        self.assertEqual(fresh["design#future"], {"keep": "first"})
        self.assertEqual(fresh["design#0"], "real field")
        data["leases"][0]["design#future"] = {"keep": "second"}
        changed, _ = _target(read_lease_document(json.dumps(data).encode(), path), "t1")
        self.assertNotEqual(before, changed)

    def test_pruning_preserves_the_invalid_pre_mutation_document(self):
        from oah.lease import Store
        from oah.paths import repo_info

        case = LeaseDocumentTest("test_duplicate_design_keys_are_invalid_not_last_wins")
        case.setUp()
        self.addCleanup(case.doCleanups)
        _, original = case.corrupt_t1("[" * 40 + "]" * 40)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)
        Store(case.state_dir, now=lambda: future).start(task="t3", agent="claude", scope=("new",),
                    note="", ttl_hours=1, info=repo_info(case.repo))
        saved = case.state_dir / "design-invalid" / (hashlib.sha256(original.encode()).hexdigest() + ".raw")
        self.assertEqual(saved.read_bytes(), original.encode())
        self.assertEqual([item.task for item, _ in Store(case.state_dir).snapshot()], ["t3"])

    def test_short_preservation_writes_keep_the_complete_original(self):
        import os
        from oah.lease import Store

        case = LeaseDocumentTest("test_duplicate_design_keys_are_invalid_not_last_wins")
        case.setUp()
        self.addCleanup(case.doCleanups)
        _, original = case.corrupt_t1("[" * 40 + "]" * 40)
        raw = original.encode()
        write = os.write
        with mock.patch("oah.lease.os.write", side_effect=lambda fd, data: write(fd, data[:7])):
            Store(case.state_dir).renew(task="t2", agent="claude", ttl_hours=2)
        saved = case.state_dir / "design-invalid" / (hashlib.sha256(raw).hexdigest() + ".raw")
        self.assertEqual(saved.read_bytes(), raw)


class HistoricalReadFailureTest(unittest.TestCase):
    def test_git_unknown_never_exempts_rewritten_history_at_finish(self):
        from oah import lease_cli, paths
        from oah.design import fs
        from oah.design.model import digest_of
        from oah.engines.proc import RunResult
        from oah.lease import LeaseError, Store
        from tests.fixtures.gates.support import build_args

        for fault in (128, 124):
            with self.subTest(fault=fault):
                case = GeneratedOutputTest("test_the_exact_generated_design_page_is_verified")
                case.setUp()
                self.addCleanup(case.doCleanups)
                rel = fs.acceptance_rel("acme", "booking", 1)
                acceptance = json.loads((case.repo / rel).read_bytes())
                acceptance["reason"] = "rewritten historical acceptance"
                (case.repo / rel).write_text(json.dumps(acceptance))
                pointer_path = case.repo / fs.pointer_rel("acme", "booking")
                pointer = json.loads(pointer_path.read_bytes())
                pointer["accepted"]["record"] = digest_of(acceptance)
                pointer_path.write_text(json.dumps(pointer))
                run_git = paths.run_git

                def failed(root, *args):
                    if args[:2] == ("cat-file", "-e") and args[2].endswith(":" + rel):
                        return RunResult(fault, "", "controlled Git read failure", fault == 124)
                    return run_git(root, *args)

                args = build_args(["finish", "--task", "t1", "--agent", "claude", "--summary", "done",
                      "--return", str(case.returned()), "--allow-missing-wiki-maintenance", "--allow-structural"])
                with mock.patch.object(paths, "run_git", failed):
                    self.assertEqual(case.outputs([rel])["verified"], [])
                    with self.assertRaises(LeaseError) as caught:
                        lease_cli._finish(args, paths.repo_info(case.repo), Store(case.state_dir))
                self.assertEqual(caught.exception.code, "OAH_SCOPE_EXCEEDED")
                self.assertEqual([item.task for item, _ in Store(case.state_dir).snapshot()], ["t1"])


if __name__ == "__main__":
    unittest.main()
