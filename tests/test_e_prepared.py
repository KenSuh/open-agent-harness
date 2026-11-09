# E 준비 회귀 시험 — 시험 기록의 한 필드만 바꾼 뒤 전송 거절과 기존 기록 보존을 확인한다.
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import os
import pathlib
import unittest
from unittest import mock

from tests import test_design_judgement as judgement_fixture
from tests import test_skill_delivery as delivery_fixture
from tests.test_design_context import DesignTask
from tests.test_skill_delivery import DeliveryTask, PLAIN, reseal


class EntryAdmissionTest(judgement_fixture.JudgementCase):
    def test_replacing_the_prepared_value_refuses_before_its_recheck_or_transport(self):
        from oah import skillpack

        live = skillpack.explore(1)
        self.assertIsNotNone(live.prepared)
        assert live.prepared is not None
        checked = []
        changed = dataclasses.replace(live, prepared=dataclasses.replace(live.prepared,
                                                    recheck=lambda: checked.append(True)))
        with self.assertRaises(skillpack.PackRefused) as refused:
            skillpack.require(changed)
        self.assertEqual(refused.exception.reason, "changed")
        self.assertEqual(checked, [])
        self.assertEqual(self.provider.calls, [])

    def request(self):
        from oah.rerank import judge

        return judge.Request("skill-fit", "skill-fit/v1", (
            ("o0:skill_excerpt", self.notes()), ("o1:skill_excerpt", self.notes()),
            ("design_stage", judge.label("design-stage", "data")),
            ("check", judge.label("design-check", "data.entities"))), ("o0", "o1"))

    def test_direct_skill_fit_without_live_admission_sends_nothing_even_when_disabled(self):
        from oah.rerank import judge

        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                result = judge.judge(self.request(), config=judgement_fixture.config(enabled=enabled),
                    policy=judgement_fixture.POLICY, sources=self.store, env={"FAKE_JUDGE_KEY": "k"},
                    providers={"fake": self.provider})
                self.assertEqual(result.status, "unused(admission)")
                self.assertEqual(self.provider.calls, [])

    def test_constructed_skill_fit_admission_sends_nothing_even_when_disabled(self):
        from oah import skillpack
        from oah.rerank import judge

        admitted = skillpack.Admitted(skillpack.Operation("public-explore", max_queries=1))
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                result = judge.judge(self.request(), config=judgement_fixture.config(enabled=enabled),
                    policy=judgement_fixture.POLICY, sources=self.store, env={"FAKE_JUDGE_KEY": "k"},
                    providers={"fake": self.provider}, admitted=admitted)
                self.assertEqual(result.status, "unused(admission)")
                self.assertEqual(self.provider.calls, [])

    def test_live_admission_still_allows_the_verified_skill_fit(self):
        from oah import skillpack
        from oah.rerank import judge

        result = judge.judge(self.request(), config=judgement_fixture.config(), policy=judgement_fixture.POLICY,
            sources=self.store, env={"FAKE_JUDGE_KEY": "k"}, providers={"fake": self.provider},
            admitted=skillpack.explore(1))
        self.assertEqual(result.status, "used")
        self.assertEqual(len(self.provider.calls), 1)

    def test_search_cache_disabled_review_and_refresh_need_the_same_live_admission(self):
        from oah import discovery, skillpack
        from oah.discovery import providers, review
        from tests.helpers import make_tmp_dir

        state = make_tmp_dir(self) / "state"
        now = dt.datetime.now(dt.timezone.utc)
        for enabled in (False, True):
            settings = dict(providers.DEFAULTS, enabled=enabled)
            actions = (
                lambda: providers.cached("mysql", state_dir=state, settings=settings, now=now),
                lambda: providers.search("mysql", state_dir=state, settings=settings, now=now),
                lambda: discovery.find([], state_dir=state, settings=settings, now=now,
                    provider=providers.PROVIDER, view=None, admitted=None),
                lambda: review.review(state, repo="acme/skill", path="skills/test", candidate={}, allow=["MIT"]),
                lambda: review.review_hit(state, hit={}, candidate={}, allow=["MIT"]),
                lambda: discovery.refresh_links(None, {"stages": []}, state_dir=state, allow=["MIT"]))
            with mock.patch.object(providers, "GETTER", side_effect=AssertionError("no transport")) as transport:
                for step, action in enumerate(actions):
                    with self.subTest(enabled=enabled, step=step), self.assertRaises(skillpack.PackRefused) as refused:
                        action()
                    self.assertEqual(refused.exception.reason, "admission")
                self.assertEqual(transport.call_count, 0)
            self.assertFalse(state.exists())


class PreparedIdentityTest(DeliveryTask, unittest.TestCase):
    def test_persisted_host_and_scope_restrictions_refuse_before_a_run(self):
        for field, value in (("host", ["codex"]), ("scope", ["src/only.py"])):
            def restricted(record):
                edit = delivery_fixture.link_op(record)
                edit["need"][field] = value
                edit["need"]["id"] = delivery_fixture.obligation_id(edit["need"])
                edit["link"]["need_id"] = edit["need"]["id"]
                return [edit]

            self.build_skills((f"acme/plain-{field}", PLAIN[1], PLAIN[2], restricted))
            result = self.call()
            with self.subTest(field=field):
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"OAH_SKILL_NOT_DELIVERABLE({field})", result.stderr)
                self.assertEqual(self.runs(), [])
                self.assertFalse(self.record.exists())

    def test_optional_exclusion_records_its_exact_reason_without_delivering_the_body(self):
        self.build_skills((*PLAIN[:3], {"required": False}))
        config = self.repo / ".oah" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + '\n[skills]\nexclude = ["plain"]\n', encoding="utf-8")

        result = self.call()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = self.runs()[-1]
        bundle = self.bundle(run)
        self.assertIsNone(bundle)
        facts = json.loads((run / "skills.json").read_text(encoding="utf-8"))["prepared_e"]["facts"]
        self.assertEqual([row["reason"] for row in facts["not_delivered"]], ["excluded"])
        self.assertEqual(facts["payloads"], [])
        body = (self.upstreams["acme/plain"] / "skills/plain/SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn(body, (run / "prompt.md").read_text(encoding="utf-8"))

    def test_expired_preparation_refuses_without_altering_its_record(self):
        from oah import call

        self.build_skills(PLAIN)
        prepared = self.prepare()
        path = prepared.run.path / "skills.json"
        before = path.read_bytes()
        after_expiry = dt.datetime.fromisoformat(json.loads(before)["prepared_e"]["expires_at"]) \
            + dt.timedelta(seconds=1)
        class Later(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return after_expiry.astimezone(tz)

        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(call.dt, "datetime", Later), \
                self.assertRaisesRegex(call.EngineError, "OAH_SKILL_NOT_DELIVERABLE\\(changed\\)"):
            call.check_prepared(prepared)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.record.exists())

    def test_excluded_mandatory_skill_refuses_before_a_run_or_engine(self):
        self.build_skills(PLAIN)
        config = self.repo / ".oah" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + '\n[skills]\nexclude = ["plain"]\n',
                          encoding="utf-8")

        result = self.call()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("OAH_SKILL_NOT_DELIVERABLE(excluded)", result.stderr)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())

    def test_whole_known_render_refuses_before_either_engine_judge(self):
        from oah import call, call_cli, skillpack
        from oah.secrets import NoKeychain

        self.build_skills(PLAIN)
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(call, "judge_engine", wraps=call.judge_engine) as engine, \
                mock.patch.object(call, "judge_difficulty", wraps=call.judge_difficulty) as difficulty:
            with self.assertRaisesRegex(call.EngineError, "OAH_SKILLS_BUDGET_EXCEEDED\\(brief\\)"):
                call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                    round_instructions="x" * (skillpack.BRIEF_BUDGET + 1)), call_cli.load_settings(self.repo),
                    root=self.repo, state_dir=self.state_dir, env=self.env, footer=tools.footer,
                    has_footer=tools.has_footer, keychain=NoKeychain())
        self.assertEqual(engine.call_count, 0)
        self.assertEqual(difficulty.call_count, 0)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())

    def test_shipped_unknown_route_refuses_before_advice_or_engine(self):
        self.build_skills(PLAIN)

        result = DesignTask.oah(self, "call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown)", result.stderr)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())

    def test_unchanged_prepared_record_launches_and_complete_bundle_binding_is_compared(self):
        from oah import call

        self.build_skills(PLAIN)
        prepared = self.prepare()
        path = prepared.run.path / "skills.json"
        original = path.read_bytes()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
            call.check_prepared(prepared)
            for field, value in (("task", "other-task"), ("lease", ["other-lease", 7]),
                                 ("engine", "codex"), ("stage", "correction")):
                saved = json.loads(original)
                if field in ("task", "lease"):
                    saved["bundle"]["operation"][field] = value
                else:
                    saved["bundle"][field] = value
                saved["bundle"] = reseal(saved["bundle"])
                changed = json.dumps(saved).encode("utf-8")
                path.write_bytes(changed)
                with self.subTest(field=field):
                    with self.assertRaisesRegex(call.EngineError, "OAH_SKILL_NOT_DELIVERABLE\\(changed\\)"):
                        call.check_prepared(prepared)
                    self.assertEqual(path.read_bytes(), changed)
                path.write_bytes(original)
        self.assertFalse(self.record.exists())

    def test_prepared_local_policy_change_refuses_and_preserves_the_earlier_record(self):
        from oah import call

        self.build_skills(PLAIN)
        prepared = self.prepare()
        before = (prepared.run.path / "skills.json").read_bytes()
        config = self.repo / ".oah" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + '\n[skills]\nexclude = ["plain"]\n',
                          encoding="utf-8")
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
            with self.assertRaisesRegex(call.EngineError, "OAH_SKILL_NOT_DELIVERABLE\\(changed\\)"):
                call.check_prepared(prepared)
        self.assertEqual((prepared.run.path / "skills.json").read_bytes(), before)
        self.assertFalse(self.record.exists())

    def test_reference_rechecks_the_same_cli_qualification_as_design_reads(self):
        build = getattr(delivery_fixture, "build_reference_guide", delivery_fixture.ReferenceDeliveryTest.build_guide)
        build(self)

        result = self.call(env={"FAKE_CLAUDE_VERSION": "2.1.317"})

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        if os.name == "nt":
            self.assertIn("OAH_SKILL_REFERENCE_UNAVAILABLE", result.stderr)
        else:
            self.assertIn("OAH_SKILL_REFERENCE_UNAVAILABLE(profile)", result.stderr)
            self.assertIn("2.1.318", result.stderr)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())

    def test_inline_fits_but_full_reference_render_refuses_before_advice(self):
        from oah import call, call_cli, skillpack
        from oah.secrets import NoKeychain

        build = getattr(delivery_fixture, "build_reference_guide", delivery_fixture.ReferenceDeliveryTest.build_guide)
        build(self)
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(call, "judge_engine", wraps=call.judge_engine) as engine, \
                mock.patch.object(call, "_skill_fit_advisor", wraps=call._skill_fit_advisor) as advisor:
            with self.assertRaises(call.EngineError) as caught:
                call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                    round_instructions="q" * 300000), call_cli.load_settings(self.repo), root=self.repo,
                    state_dir=self.state_dir, env=self.env, footer=tools.footer, has_footer=tools.has_footer,
                    keychain=NoKeychain())
        if os.name == "nt":
            self.assertIn("OAH_SKILL_REFERENCE_UNAVAILABLE", str(caught.exception))
        else:
            self.assertIn("OAH_SKILLS_BUDGET_EXCEEDED(brief)", str(caught.exception))
            totals = json.JSONDecoder().raw_decode(str(caught.exception).split("measured totals ", 1)[1])[0]
            self.assertLess(totals["prompt_bytes"], skillpack.BRIEF_BUDGET)
            self.assertGreater(totals["prompt_bytes"] + totals["deferred_render_bytes"], skillpack.BRIEF_BUDGET)
        self.assertEqual(engine.call_count, 0)
        self.assertEqual(advisor.call_count, 0)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())


class OrcaIdentityTest(DeliveryTask, unittest.TestCase):
    orca_queue = delivery_fixture.InstalledCopyTest.orca_queue
    terminals_opened = delivery_fixture.InstalledCopyTest.terminals_opened

    @property
    def orca_state(self) -> pathlib.Path:
        return self.work / "orca"

    def test_orca_legacy_and_unchanged_e_launch_records_still_start(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                if managed:
                    self.build_skills(PLAIN)
                else:
                    self.build_skills()
                prepared = self.prepare()
                self.orca_queue(prepared)
                launch = json.loads((prepared.run.path / "launch.json").read_text(encoding="utf-8"))
                self.assertEqual("e_digest" in launch, managed)
                if managed:
                    saved = json.loads((prepared.run.path / "skills.json").read_text(encoding="utf-8"))
                    self.assertEqual(launch["e_digest"], saved["prepared_e"]["digest"])
                worker = self.oah("worker-run", prepared.run_id, "--stage", "initial")
                self.assertEqual(worker.returncode, 0, worker.stdout + worker.stderr)
                self.assertTrue(self.record.exists())
                self.record.unlink()

    def test_orca_worker_refuses_when_only_the_saved_e_digest_changes(self):
        self.build_skills(PLAIN)
        prepared = self.prepare()
        self.orca_queue(prepared)
        record = prepared.run.path / "skills.json"
        before = record.read_bytes()
        launch = prepared.run.path / "launch.json"
        changed = json.loads(launch.read_text(encoding="utf-8"))
        changed["e_digest"] = "sha256:" + "f" * 64
        raw = json.dumps(changed).encode("utf-8")
        launch.write_bytes(raw)

        worker = self.oah("worker-run", prepared.run_id, "--stage", "initial")

        self.assertEqual(worker.returncode, 1, worker.stdout + worker.stderr)
        self.assertIn("OAH_RUN_LAUNCH_INVALID", worker.stderr)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(launch.read_bytes(), raw)
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
