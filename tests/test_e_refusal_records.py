# 거절 기록 회귀 — 전송 전 거절과 저장 실패가 실제 요청 수나 진행 중 결과를 바꾸지 않는지 확인한다.
from __future__ import annotations

import contextlib
import dataclasses
import json
import unittest
from unittest import mock

from tests.helpers import make_tmp_dir
from tests.test_e_release_gateway import ReleaseFixture, AFTER


class RefusalAttemptTest(unittest.TestCase):
    def test_B2_final_admission_refusal_counts_no_started_request(self):
        from oah import skillpack
        from oah.discovery import gateway
        from oah.discovery.store import digest

        root = make_tmp_dir(self)
        sent = []
        with contextlib.chdir(root):
            admitted = skillpack.explore(1)
            real = skillpack.Admitted.require
            count = 0

            def require(value):
                nonlocal count
                count += 1
                if count == 2:
                    raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "changed", "fixture policy changed")
                return real(value)

            with mock.patch.object(skillpack.Admitted, "require", require), \
                    self.assertRaises(skillpack.PackRefused):
                gateway.send(admitted, "skill-search", digest("query"), lambda: sent.append(1), state=root)
        self.assertEqual(sent, [])
        rows = [row for path in (root / "skills/attempts").glob("*.json")
                for row in json.loads(path.read_bytes())["requests"]]
        self.assertTrue(all(row["requests"] == 0 for row in rows), rows)

    def test_B3_first_save_failure_leaves_no_phantom_started_row(self):
        from oah import atomic
        from oah.discovery import attempts
        from oah.discovery.store import digest

        root = make_tmp_dir(self)
        sent = []
        real = atomic.write_json_atomic
        first = True

        def save(path, data, **options):
            nonlocal first
            if first:
                first = False
                raise OSError("fixture first save failed")
            return real(path, data, **options)

        with mock.patch.object(atomic, "write_json_atomic", save), attempts.scope(root):
            with self.assertRaises(OSError):
                with attempts.request("skill-search", digest("query")):
                    sent.append(1)
        self.assertEqual(sent, [])
        rows = [row for path in (root / "skills/attempts").glob("*.json")
                for row in json.loads(path.read_bytes())["requests"]]
        self.assertEqual(rows, [])

    def test_B3_later_save_failure_keeps_the_transport_refusal_or_return_value(self):
        from oah import atomic, skillpack
        from oah.discovery import attempts, gateway
        from oah.discovery.store import digest

        for refuse in (False, True):
            with self.subTest(refuse=refuse):
                root = make_tmp_dir(self)
                real = atomic.write_json_atomic
                calls = 0
                sent = []
                refusal = skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "changed", "fixture refusal")

                def save(path, data, **options):
                    nonlocal calls
                    calls += 1
                    if calls > 1:
                        raise OSError("fixture later save failed")
                    return real(path, data, **options)

                def transport():
                    sent.append(1)
                    if refuse:
                        raise refusal
                    return "returned"

                with contextlib.chdir(root), mock.patch.object(atomic, "write_json_atomic", save):
                    admitted = skillpack.explore(1)
                    if refuse:
                        with self.assertRaises(skillpack.PackRefused) as caught, attempts.scope(root):
                            gateway.send(admitted, "skill-search", digest("query"), transport, state=root)
                        self.assertIs(caught.exception, refusal)
                    else:
                        with attempts.scope(root):
                            result = gateway.send(admitted, "skill-search", digest("query"), transport, state=root)
                        self.assertEqual(result, "returned")
                self.assertEqual(sent, [1])
                saved = [json.loads(path.read_bytes()) for path in (root / "skills/attempts").glob("*.json")]
                self.assertEqual(len(saved), 1)
                self.assertIsNone(saved[0]["requests"][0]["requests"])
                self.assertEqual(saved[0]["requests"][0]["state"], "unknown")


class CollectorRefusalTest(ReleaseFixture):
    def test_B1_collector_refusal_preserves_head_generation_and_failure_count(self):
        from oah import skillpack
        from oah.discovery import gateway

        fixture = self.release_fixture()
        head_path = fixture.collector.state.root / "heads" / f"{fixture.spec.id}.json"
        before = head_path.read_bytes()
        config = fixture.root / ".oah/config.toml"
        original = fixture.collector.transport

        def remote(*args, **options):
            result = original.ls_remote(*args, **options)
            config.write_text(AFTER, encoding="utf-8")
            return result

        with contextlib.chdir(fixture.root):
            guarded = gateway.collector_transport(dataclasses.replace(original, ls_remote=remote),
                                                  skillpack.explore(1), state=fixture.root)
            fixture.collector.transport = fixture.collector.observer.transport = guarded
            with self.assertRaises(skillpack.PackRefused) as caught:
                fixture.collector.check_one(fixture.spec)
        self.assertEqual(caught.exception.reason, "changed")
        self.assertEqual(head_path.read_bytes(), before)
        self.assertEqual({path: path.read_bytes() for path in fixture.earlier}, fixture.earlier)
