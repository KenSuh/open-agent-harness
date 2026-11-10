# 릴리스 요청 회귀 — 수집 단계 사이의 정책 변경을 거절하고 실제 Git 및 HTTP 요청 기록을 보존한다.
from __future__ import annotations

import contextlib
import dataclasses
import json
import pathlib
from unittest import mock

from oah.design.sources.check import Sources
from oah.design.sources.model import SourceSpec
from tests.helpers import make_tmp_dir
from tests.test_design_sources import LICENSE, SourceCase

EVIDENCE = {}
BEFORE = '[skills.discovery]\nlicense_allow=["MIT"]\n'
AFTER = '[skills.discovery]\nlicense_allow=["Apache-2.0"]\n'


@dataclasses.dataclass(frozen=True, slots=True)
class ReleaseData:
    collector: Sources
    spec: SourceSpec
    snapshot: str
    observation: str
    earlier: dict[pathlib.Path, bytes]
    root: pathlib.Path


class ReleaseFixture(SourceCase):
    def release_fixture(self) -> ReleaseData:
        self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})
        self.tag("v1.0.0")
        self.github.add("v1.0.0")
        collector = self.sources(license_allow=["MIT"])
        spec = self.register(collector, track="release")
        previous = collector.check_one(spec)
        self.assertEqual(previous.result, "changed")
        assert previous.snapshot is not None and previous.observation is not None
        earlier = {p: p.read_bytes() for kind in
                       ("snapshots", "observations", "selections", "release-observations", "blobs")
                       for p in (self.state / "sources" / kind).rglob("*") if p.is_file()}
        self.assertTrue(earlier)
        self.commit({"src/lib.rs": "pub fn b() {}\n"})
        self.tag("v1.1.0")
        self.github.add("v1.1.0")
        root = make_tmp_dir(self)
        config = root / ".oah/config.toml"
        config.parent.mkdir()
        config.write_text(BEFORE, encoding="utf-8")
        return ReleaseData(collector, spec, previous.snapshot, previous.observation, earlier, root)


class ReleaseGatewayTest(ReleaseFixture):
    def collect(self, change_at: str | None):
        from oah import skillpack
        from oah.design.sources import gitref
        from oah.discovery import gateway

        fixture = self.release_fixture()
        config = fixture.root / ".oah/config.toml"
        original = fixture.collector.transport
        real_send = gateway.send
        depth, trace, refusals = 0, [], []

        def resolve(locator):
            if change_at == "url":
                config.write_text(AFTER, encoding="utf-8")
            return original.git_url(locator)

        def send(*args, **kwargs):
            nonlocal depth
            depth += 1
            try:
                return real_send(*args, **kwargs)
            except skillpack.PackRefused as exc:
                refusals.append(f"{exc.code}({exc.reason})")
                raise
            finally:
                depth -= 1

        def observed(name, operation):
            def invoke(*args, **kwargs):
                rows = [json.loads(p.read_text(encoding="utf-8"))
                        for p in (fixture.root / "skills/attempts").glob("*.json")]
                trace.append({"name": name, "gateway_depth": depth, "attempts": rows})
                result = operation(*args, **kwargs)
                if name == "ls-remote" and change_at == "remote":
                    config.write_text(AFTER, encoding="utf-8")
                return result

            return invoke

        remote = observed("ls-remote", original.ls_remote)
        with contextlib.chdir(fixture.root), mock.patch.object(gateway, "send", send), \
                mock.patch.object(gitref, "ls_remote", remote):
            guarded = gateway.collector_transport(dataclasses.replace(original, git_url=resolve,
                getter=observed("http", original.getter), ls_remote=remote,
                fetch=observed("fetch", original.fetch)), skillpack.explore(1), state=fixture.root)
            fixture.collector.transport = fixture.collector.observer.transport = guarded
            try:
                result = fixture.collector.check_one(fixture.spec)
            except skillpack.PackRefused as exc:
                result = exc
        self.assertEqual({p: p.read_bytes() for p in fixture.earlier}, fixture.earlier)
        self.assertIsNotNone(fixture.collector.snapshot(fixture.snapshot))
        self.assertEqual(fixture.collector.observation(fixture.observation)["snapshot"], fixture.snapshot)
        summaries = [json.loads(p.read_text(encoding="utf-8"))
                     for p in (fixture.root / "skills/attempts").glob("*.json")]
        EVIDENCE[change_at or "control"] = {"result": str(result) if isinstance(result, skillpack.PackRefused)
            else result.result, "problems": () if isinstance(result, skillpack.PackRefused) else result.problems,
            "trace": trace, "summaries": summaries, "refusals": refusals, "earlier_preserved": True}
        return result, trace, summaries, refusals

    def test_policy_change_at_url_refuses_before_advertisement(self):
        from oah.skillpack import PackRefused

        result, trace, summaries, refusals = self.collect("url")
        self.assertIsInstance(result, PackRefused)
        self.assertEqual(refusals, ["OAH_SKILL_NOT_DELIVERABLE(changed)"])
        self.assertEqual(trace, [])
        self.assertEqual(summaries, [])

    def test_policy_change_after_advertisement_refuses_before_next_operation(self):
        from oah.skillpack import PackRefused

        result, trace, summaries, refusals = self.collect("remote")
        self.assertIsInstance(result, PackRefused)
        self.assertEqual(refusals, ["OAH_SKILL_NOT_DELIVERABLE(changed)"])
        self.assertEqual([r["name"] for r in trace], ["ls-remote"])
        self.assertEqual(len(summaries), 1)
        self.assertEqual([(r["kind"], r["state"], r["requests"]) for r in summaries[0]["requests"]],
                         [("skill-review", "returned", 1)])
        self.assertIsNotNone(summaries[0]["requests"][0]["outcome_digest"])

    def test_unchanged_release_records_each_operation_while_open(self):
        from oah.skillpack import PackRefused

        result, trace, summaries, refusals = self.collect(None)
        assert not isinstance(result, PackRefused)
        self.assertEqual(result.result, "changed")
        self.assertEqual(refusals, [])
        self.assertEqual([r["name"] for r in trace], ["ls-remote", "http", "http", "fetch",
                                                       "http", "http", "ls-remote"])
        started_ids = []
        for index, step in enumerate(trace):
            with self.subTest(operation=index):
                self.assertEqual(step["gateway_depth"], 1)
                self.assertEqual(len(step["attempts"]), index + 1)
                active = [s for s in step["attempts"] if any(r["state"] == "started" for r in s["requests"])]
                self.assertEqual(len(active), 1)
                self.assertEqual(len(active[0]["requests"]), 1)
                started_ids.append(active[0]["attempt_id"])
        self.assertEqual(len(set(started_ids)), len(trace))
        self.assertEqual({s["attempt_id"] for s in summaries}, set(started_ids))
        self.assertTrue(all(len(s["requests"]) == 1 for s in summaries))
        facts = [r for s in summaries for r in s["requests"]]
        self.assertEqual([(r["kind"], r["state"], r["requests"]) for r in facts],
                         [("skill-review", "returned", 1)] * len(trace))
        self.assertTrue(all(r["outcome_digest"] is not None for r in facts))
