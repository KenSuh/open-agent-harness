# 거절 전달 회귀 — 재정렬·판단 및 실제 CLI가 지역 준비 거절을 그대로 보고하는지 확인한다.
from __future__ import annotations

import contextlib
import io
import os
import unittest
from collections.abc import Mapping, Sequence
from unittest import mock

from oah.discovery.preparation import Json
from oah.rerank.base import Candidate
from oah.secrets import Secret
from tests.helpers import make_tmp_dir
from tests.test_design_judgement import JudgementCase
from tests.test_launcher_native import NativeTask
from tests.test_skill_delivery import DeliveryTask, PLAIN, link_op


class RecordingProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def key_name(self, config: Mapping[str, Json]) -> str:
        return "FAKE_RERANK_KEY"

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
              config: Mapping[str, Json]) -> Sequence[float]:
        self.calls.append(kind)
        return [float(index) for index, _ in enumerate(candidates)]


class RerankRefusalTest(unittest.TestCase):
    def test_B4_rerank_propagates_the_gateway_refusal_before_the_provider_starts(self):
        from oah import rerank, skillpack
        from oah.discovery import gateway
        from tests.test_rerank import candidates, enabled

        root = make_tmp_dir(self)
        config = root / ".oah/config.toml"
        config.parent.mkdir()
        config.write_text("[skills]\n", encoding="utf-8")
        provider = RecordingProvider()
        with contextlib.chdir(root):
            admitted = skillpack.explore(1)
            wrapped = gateway.SkillOrderProvider(provider, admitted, root)
            config.write_text("[skills]\n# fixture changed before score\n", encoding="utf-8")
            with mock.patch.dict(rerank.PROVIDERS, {"fake": wrapped}), \
                    self.assertRaises(skillpack.PackRefused) as caught:
                rerank.rerank("skills", candidates("one", "two"), config=enabled(),
                              env={"FAKE_RERANK_KEY": "fixture-key"})
        self.assertEqual(caught.exception.reason, "changed")
        self.assertEqual(provider.calls, [])


class JudgeRefusalTest(JudgementCase):
    def test_B4_judge_propagates_the_gateway_refusal_before_the_provider_starts(self):
        from oah import skillpack
        from oah.discovery import gateway
        from oah.rerank import judge

        root = make_tmp_dir(self)
        config = root / ".oah/config.toml"
        config.parent.mkdir()
        config.write_text("[skills]\n", encoding="utf-8")
        request = judge.Request("skill-fit", "skill-fit/v1", (("o0:skill_excerpt", self.notes()),
            ("o1:skill_excerpt", self.notes()), ("design_stage", judge.label("design-stage", "data")),
            ("check", judge.label("design-check", "data.entities"))), ("o0", "o1"))
        real = gateway.send

        def changed(*args, **options):
            config.write_text("[skills]\n# fixture changed before judge\n", encoding="utf-8")
            return real(*args, **options)

        with contextlib.chdir(root):
            self.admitted = skillpack.explore(1)
            with mock.patch.object(gateway, "send", changed), self.assertRaises(skillpack.PackRefused) as caught:
                self.run_judge(request)
        self.assertEqual(caught.exception.reason, "changed")
        self.assertEqual(self.provider.calls, [])


class CallRefusalTest(DeliveryTask, unittest.TestCase):
    def test_B5_cli_reports_refusals_at_judges_rank_and_task_discovery_without_traceback(self):
        from oah import call, cli, skillpack
        from oah.discovery import providers, tasks

        def linked(record):
            return [{"op": "set-tech", "label": "database", "value": "mysql", "decision": None,
                     "note": None}, link_op(record)]

        self.build_skills((*PLAIN[:3], linked))
        config = self.repo / ".oah/config.toml"
        before = config.read_text(encoding="utf-8") + "\n[skills.discovery]\nenabled=false\n"
        config.write_text(before, encoding="utf-8")
        for owner, name in ((call, "judge_engine"), (call, "judge_difficulty"), (skillpack, "rank"),
                            (tasks, "prepare_task")):
            with self.subTest(step=name):
                real = getattr(owner, name)
                entered = []

                def changed(*args, **options):
                    entered.append(name)
                    config.write_text(before + f"\n# fixture policy changed at {name}\n", encoding="utf-8")
                    return real(*args, **options)

                out, err = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                        mock.patch.object(owner, name, changed), mock.patch.object(providers, "GETTER") as sent, \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(["call", "claude", "--stage", "initial", "--task", "t1", "--no-rerank"])
                self.assertEqual(entered, [name])
                self.assertEqual(code, 1, out.getvalue() + err.getvalue())
                self.assertTrue(err.getvalue().startswith("OAH_SKILL_NOT_DELIVERABLE(changed)"), err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())
                sent.assert_not_called()
                self.assertFalse(self.record.exists())
                config.write_text(before, encoding="utf-8")


class PrepareSaveFailureTest(NativeTask, unittest.TestCase):
    def test_B3_prepare_and_native_keep_the_refusal_when_a_final_save_is_unavailable(self):
        from oah import atomic, call, call_cli, skillpack
        from oah.discovery import gateway
        from oah.discovery.store import digest

        tools = call_cli.intent_tools()
        config = self.repo / ".oah/config.toml"
        original = config.read_text(encoding="utf-8")
        real_admit, real_write = call._admit_skills, atomic.write_json_atomic
        for native in (False, True):
            with self.subTest(native=native):
                blocked = False
                sent, earlier = [], {}

                def save(path, data, **options):
                    if blocked and path.parent.name == "attempts":
                        raise OSError("fixture final save unavailable")
                    return real_write(path, data, **options)

                def changed(*args, **options):
                    nonlocal blocked
                    pack = real_admit(*args, **options)
                    assert pack is not None and pack.admitted is not None
                    gateway.send(pack.admitted, "skill-order", digest("fixture-order"),
                                 lambda: sent.append("started"), state=self.state_dir)
                    earlier.update({path: path.read_bytes() for path in
                                    (self.state_dir / "skills/attempts").glob("*.json")})
                    config.write_text(original + "\n# fixture policy changed at final preparation\n", encoding="utf-8")
                    blocked = True
                    try:
                        skillpack.require(pack.admitted)
                    finally:
                        config.write_text(original, encoding="utf-8")
                    raise AssertionError("the changed fixture must refuse")

                operation = call.prepare_native if native else call.prepare
                with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                        mock.patch.object(call, "_admit_skills", changed), \
                        mock.patch.object(atomic, "write_json_atomic", save), \
                        self.assertRaises(call.EngineError) as caught:
                    operation(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                        no_rerank=True), call_cli.load_settings(self.repo), root=self.repo, state_dir=self.state_dir,
                        env=self.env, footer=tools.footer, has_footer=tools.has_footer)
                self.assertEqual(caught.exception.code, "OAH_SKILL_NOT_DELIVERABLE(changed)")
                self.assertEqual(sent, ["started"])
                self.assertEqual({path: path.read_bytes() for path in earlier}, earlier)
                self.assertFalse(self.record.exists())
                config.write_text(original, encoding="utf-8")
