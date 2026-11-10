# 수집 요청 회귀 — 로컬 Git 단계 사이의 정책 변경을 거절하고 기존 검토 및 요청 기록을 보존한다.
from __future__ import annotations

import dataclasses
import json
import unittest
from unittest import mock

from tests.test_skill_discovery import DiscoveryRepo, mit_repo, skill_md

EVIDENCE = {}


class CollectorGatewayTest(DiscoveryRepo, unittest.TestCase):
    def collect(self, change_at: str | None):
        from oah.design.sources import adapters, gitref
        from oah.discovery import gateway, review, store

        self.upstream("acme/skills", mit_repo({"skills/guide": skill_md("guide")}))
        before = '[skills.discovery]\nlicense_allow=["MIT"]\n'
        after = '[skills.discovery]\nlicense_allow=["Apache-2.0"]\n'
        self.config(before)
        old, _ = self.review_json("--github", "acme/skills", "--path", "skills/guide")
        assert old is not None
        earlier = {p: p.read_bytes() for kind in ("reviews", "attempts")
                   for p in (self.skills_state / kind).rglob("*.json") if p.stem != "index"}
        self.assertTrue(earlier)
        self.upstream("acme/skills", {"skills/guide/SKILL.md": skill_md("guide", "Check the updated local guide.\n")})
        config = self.repo / ".oah/config.toml"
        original = review.TRANSPORT
        assert original is not None
        depth, trace = 0, []
        real_send, real_remote, real_fetch = gateway.send, gitref.ls_remote, adapters.fetch

        def resolve(locator):
            if change_at == "url":
                config.write_text(after, encoding="utf-8")
            return original.git_url(locator)

        def send(*args, **kwargs):
            nonlocal depth
            depth += 1
            try:
                return real_send(*args, **kwargs)
            finally:
                depth -= 1

        def record_step(name):
            rows = [json.loads(p.read_text(encoding="utf-8")) for p in
                    (self.skills_state / "attempts").glob("*.json") if p not in earlier]
            trace.append({"name": name, "gateway_depth": depth,
                          "policy_changed": config.read_text(encoding="utf-8") != before, "attempts": rows})

        def remote(*args, **kwargs):
            record_step("ls-remote")
            result = real_remote(*args, **kwargs)
            if change_at == "remote":
                config.write_text(after, encoding="utf-8")
            return result

        def fetch(*args, **kwargs):
            record_step("fetch")
            return real_fetch(*args, **kwargs)

        fields = {field.name for field in dataclasses.fields(original)}
        io = {name: function for name, function in (("ls_remote", remote), ("fetch", fetch)) if name in fields}
        with mock.patch.object(review, "TRANSPORT", dataclasses.replace(original, git_url=resolve, **io)), \
                mock.patch.object(gateway, "send", send), mock.patch.object(gitref, "ls_remote", remote), \
                mock.patch.object(adapters, "fetch", fetch):
            code, out, err = self.skills("review", "--github", "acme/skills", "--path", "skills/guide", "--json",
                                         expect=None)
        self.assertEqual({p: p.read_bytes() for p in earlier}, earlier)
        self.assertEqual(store.get_review(self.state(self.repo), old["id"]), old)
        summaries = [json.loads(p.read_text(encoding="utf-8")) for p in
                     (self.skills_state / "attempts").glob("*.json") if p not in earlier]
        EVIDENCE[change_at or "control"] = {"code": code, "out": out, "err": err, "trace": trace,
                                           "summaries": summaries, "earlier_preserved": True}
        return code, out, err, trace, summaries

    def test_policy_change_at_url_resolution_refuses_before_actual_git_io(self):
        code, out, err, trace, summaries = self.collect("url")
        self.assertEqual(code, 1, out + err)
        self.assertIn("OAH_SKILL_NOT_DELIVERABLE(changed)", err)
        self.assertEqual(trace, [])
        self.assertEqual(summaries, [])

    def test_policy_change_after_ls_remote_refuses_before_fetch(self):
        code, out, err, trace, summaries = self.collect("remote")
        self.assertEqual(code, 1, out + err)
        self.assertIn("OAH_SKILL_NOT_DELIVERABLE(changed)", err)
        self.assertEqual([row["name"] for row in trace], ["ls-remote"])
        self.assertEqual(len(summaries), 1)
        self.assertEqual([(r["kind"], r["state"], r["requests"]) for r in summaries[0]["requests"]],
                         [("skill-review", "returned", 1)])

    def test_unchanged_collector_records_each_git_operation_while_it_is_open(self):
        code, out, err, trace, summaries = self.collect(None)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([row["name"] for row in trace], ["ls-remote", "fetch"])
        self.assertTrue(all(row["gateway_depth"] == 1 for row in trace))
        self.assertTrue(all(any(r["state"] == "unknown" and r["requests"] == 0 for summary in row["attempts"]
                               for r in summary["requests"]) for row in trace))
        self.assertEqual(len(summaries), 2)
        rows = [row for summary in summaries for row in summary["requests"]]
        self.assertEqual([(r["kind"], r["state"], r["requests"]) for r in rows],
                         [("skill-review", "returned", 1), ("skill-review", "returned", 1)])
        self.assertNotEqual(rows[0]["input_digest"], rows[1]["input_digest"])
        self.assertTrue(all(r["outcome_digest"] is not None for r in rows))
