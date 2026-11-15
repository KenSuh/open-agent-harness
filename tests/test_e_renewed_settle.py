from __future__ import annotations

import datetime as dt
import unittest

from tests import test_e_spent_evidence as spent
from tests.test_skill_delivery import DeliveryTask


class RenewedSettleTest(DeliveryTask, unittest.TestCase):
    offered_parent_relay = spent.SpentEvidenceTest.offered_parent_relay
    settle_at = spent.SpentEvidenceTest.settle_at

    def test_first_native_settle_after_renewal_keeps_offer_identity(self):
        now, run, command = self.offered_parent_relay()
        before = {name: (run / name).read_bytes() for name in ("skills.json", "capabilities.json", "prompt.md")}
        renewed = self.oah("renew", "--task", "t1", "--agent", "claude", "--ttl-hours", "1")
        self.assertEqual(renewed.returncode, 0, renewed.stdout + renewed.stderr)

        self.settle_at(command, now + dt.timedelta(minutes=7))

        self.assertEqual({name: (run / name).read_bytes() for name in before}, before)
        self.assertFalse(self.record.exists())

    def test_idempotent_native_settle_after_renewal_keeps_first_receipt(self):
        now, run, command = self.offered_parent_relay()
        self.settle_at(command, now + dt.timedelta(minutes=2))
        before = (run / "native-settlement.json").read_bytes()
        renewed = self.oah("renew", "--task", "t1", "--agent", "claude", "--ttl-hours", "1")
        self.assertEqual(renewed.returncode, 0, renewed.stdout + renewed.stderr)

        self.settle_at(command, now + dt.timedelta(minutes=7))

        self.assertEqual((run / "native-settlement.json").read_bytes(), before)
        self.assertFalse(self.record.exists())
