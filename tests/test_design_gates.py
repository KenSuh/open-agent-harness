# 설계 과제 게이트 시험 — 증거 키 각인·권위 우선 신선도·검사 중 변경 차단·반복 검사 무증가·생성 전용 기록 회전·기록 읽기·복원을 본다.
"""B-T07..B-T09, B-T13 and B-T14 (B4) through the real `run_check` with the stub node and a design-bound lease.

Gates of a design task carry the evidence key they were checked under and stay fresh only while the task's design
is current and that key counts. A check refuses before any CHECK runs when the design does not permit work and
publishes nothing when the design or lease moved meanwhile. Unchanged checks keep their entries; meaningful history
rotates into create-only pages that are read one exact digest at a time. Origin-free tasks keep the baseline
behaviour tested in test_gates.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import unittest
from unittest import mock

from oah import unlazy
from oah.gates import check as check_mod
from oah.gates.bindings import BindingStore
from tests.fixtures.gates.support import StubNode, make_unlazy_dir, met_ledger, stub_pin
from tests.helpers import git_commit, make_tmp_dir
from tests.test_design_context import DesignTask
from tests.test_design_store import TODAY
from tests.test_gates import RUNNABLE, SPLIT, pass_reply

PACKET = ".oah/tasks/t1/PACKET.md"


class DesignGateTest(DesignTask, unittest.TestCase):
    def setUp(self):
        self.build({"design": "People book a lesson.\n"})
        git_commit(self.repo, {"src/a.py": "a = 1\n", "docs/readme.md": "hi\n"}, "sources")
        self.stub = StubNode(make_tmp_dir(self) / "stub")
        self.unlazy = make_unlazy_dir(make_tmp_dir(self) / "unlazy")
        self.enterContext(mock.patch.object(unlazy, "PIN", stub_pin(self.unlazy)))
        self.ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.bindings = BindingStore(self.state_dir, "t1")

    def check(self):
        return check_mod.run_check(self.repo, self.ledger, self.bindings, unlazy_dir=self.unlazy,
                                   node=self.stub.node, env=self.stub.env())

    def fresh(self) -> dict:
        return check_mod.freshness(self.repo, self.ledger, self.bindings, unlazy_dir=self.unlazy, env=self.stub.env())

    def entry(self, gate: str = "G1") -> dict:
        return self.bindings.load()[check_mod.ledger_key(self.repo, self.ledger)][gate]

    def runnable(self, text: str = RUNNABLE, ids=("G1",)) -> None:
        self.ledger.write_text(text, encoding="utf-8")
        self.stub.reply("gate-check.mjs", pass_reply(self.ledger, met_ledger(text), ids=ids))

    def accept_r2(self) -> None:
        store = self.store(self.repo)
        context = store.context("acme", "booking")
        saved = store.save("acme", "booking", expect_revision=1, expect_digest=context.current.digest,
                           reason="two", parts={"design": "People book two lessons.\n"}, today=TODAY)
        store.accept("acme", "booking", revision=2, digest=saved.digest, reason="ok", today=TODAY)

    def key(self) -> str:
        lease, _judged = self.judged()
        return lease.design_record().key

    def test_a_current_design_stamps_its_evidence_key(self):
        self.runnable()
        report = self.check()
        self.assertTrue(report.ok, report.lines)
        entry = self.entry()
        self.assertEqual(entry["design"], self.key())
        self.assertEqual(set(entry["observation"]), {"p0", "p1"})
        self.assertEqual(entry["observation"]["p0"], entry["observation"]["p1"])
        self.assertEqual(self.fresh(), {"G1": "fresh"})

    def test_newer_accepted_authority_stales_evidence_without_a_rebind(self):
        self.runnable()
        self.assertTrue(self.check().ok)
        before = self.bindings.path.read_bytes()
        self.accept_r2()  # the task and its narrow inputs still say r1
        self.assertEqual(self.fresh(), {"G1": "stale"})
        calls = len(self.stub.calls("gate-check.mjs"))
        with self.assertRaises(check_mod.GateRefused):
            self.check()
        self.assertEqual(len(self.stub.calls("gate-check.mjs")), calls)  # refused before any CHECK
        self.assertEqual(self.bindings.path.read_bytes(), before)  # the old bytes are kept, never re-tagged

    def test_a_design_or_lease_move_during_the_check_publishes_nothing(self):
        self.runnable()
        real = check_mod._lease_generation
        calls = {"n": 0}

        def moved(store):
            calls["n"] += 1
            value = real(store)
            return value + 1 if calls["n"] > 1 else value  # P1 sees another generation

        with mock.patch.object(check_mod, "_lease_generation", side_effect=moved):
            with self.assertRaises(check_mod.GateRefused) as caught:
                self.check()
        self.assertEqual(caught.exception.code, "OAH_DESIGN_CHANGED")
        self.assertFalse(self.bindings.path.exists())  # no new successful key

    def test_attested_gates_follow_the_same_rule(self):
        self.runnable(SPLIT)
        first = self.check()
        self.assertEqual((first.actions["G1"], first.actions["G2"]), ("run", "attest"))
        self.assertEqual(self.entry("G2")["design"], self.key())
        self.accept_r2()
        self.assertEqual(self.fresh(), {"G1": "stale", "G2": "stale"})

    def test_unchanged_checks_keep_the_entry_and_add_no_history(self):
        self.runnable()
        self.assertTrue(self.check().ok)
        first = self.bindings.path.read_bytes()
        for _ in range(3):
            again = self.check()
            self.assertEqual(again.actions["G1"], "carry")
        self.assertEqual(self.bindings.path.read_bytes(), first)
        self.assertEqual(json.loads(first).get("design_history", []), [])
        self.assertEqual(len(self.stub.calls("gate-check.mjs")), 1)

    def rotate(self) -> str:
        """Make meaningful changes until the live history rotates into an archive page; its digest."""
        self.runnable()
        self.assertTrue(self.check().ok)
        with mock.patch.object(check_mod, "RECENT_LIMIT", 1):
            for value in (2, 3):
                (self.repo / "src" / "a.py").write_text(f"a = {value}\n", encoding="utf-8")
                self.runnable()
                self.assertTrue(self.check().ok)
        head = json.loads(self.bindings.path.read_bytes())["design_archive_head"]
        self.assertIsNotNone(head)
        return head["sha256"]

    def test_meaningful_changes_rotate_into_exact_create_only_pages(self):
        digest = self.rotate()
        page = self.bindings.path.parent / check_mod.HISTORY_DIR_NAME / f"{digest}.json"
        self.assertEqual(hashlib.sha256(page.read_bytes()).hexdigest(), digest)
        read = check_mod.history_page(self.bindings, digest)
        self.assertEqual(read["page"], digest)
        self.assertEqual(self.fresh(), {"G1": "fresh"})  # current freshness never walks the archive
        page.write_bytes(page.read_bytes() + b" ")
        with self.assertRaises(check_mod.GateRefused) as caught:
            check_mod.history_page(self.bindings, digest)
        self.assertEqual(caught.exception.code, "OAH_GATES_HISTORY_INVALID")

    def test_unwritable_history_refuses_with_storage_and_retries(self):
        self.runnable()
        self.assertTrue(self.check().ok)
        blocker = self.bindings.path.parent / check_mod.HISTORY_DIR_NAME
        blocker.write_text("not a folder", encoding="utf-8")
        with mock.patch.object(check_mod, "RECENT_LIMIT", 0):
            (self.repo / "src" / "a.py").write_text("a = 9\n", encoding="utf-8")
            self.runnable()
            with self.assertRaises(check_mod.GateRefused) as caught:
                self.check()
            self.assertEqual(caught.exception.code, "OAH_GATES_HISTORY_STORAGE")
            blocker.unlink()
            self.runnable()
            self.assertTrue(self.check().ok)

    def test_the_history_read_path_and_exact_restore(self):
        digest = self.rotate()
        live = self.oah("design", "task", "t1", "--gate-history")
        self.assertEqual(live.returncode, 0, live.stderr)
        self.assertIn(f"previous={digest}", live.stdout)
        page = self.oah("design", "task", "t1", "--gate-history", "--page", digest, "--json")
        self.assertEqual(json.loads(page.stdout)["page"], digest)
        self.bindings.path.unlink()
        wrong = self.oah("design", "task", "t1", "--restore-gate-history", digest, "--expect-current", "0" * 64)
        self.assertEqual(wrong.returncode, 1)
        restored = self.oah("design", "task", "t1", "--restore-gate-history", digest, "--expect-current", "absent")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        archived = self.bindings.path.parent / check_mod.HISTORY_DIR_NAME / f"{digest}.json"
        self.assertEqual(self.bindings.path.read_bytes(), archived.read_bytes())
        worker = self.oah("design", "task", "t1", "--restore-gate-history", digest, "--expect-current",
                          hashlib.sha256(self.bindings.path.read_bytes()).hexdigest(), env={"OAH_WORKER": "1"})
        self.assertEqual(worker.returncode, 1)

    def test_an_exact_resume_after_expiry_keeps_gate_evidence(self):
        self.runnable()
        self.assertTrue(self.check().ok)
        path = self.state_dir / "leases.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        for lease in data["leases"]:
            lease["expires_at"] = past
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        resumed = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", PACKET)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.fresh(), {"G1": "fresh"})  # same instance, basis and design: same key
        self.assertEqual(self.entry()["design"], self.key())


if __name__ == "__main__":
    unittest.main()
