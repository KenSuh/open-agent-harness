# 요청 실행 검사 — 실제 E 진입 흐름의 최하위 연결에서 열린 gateway 시도와 저장된 시작 기록을 확인한다.
from __future__ import annotations

import collections
import contextlib
import dataclasses
import importlib
import io
import json
import os
import unittest
import urllib.request
from unittest import mock

from tests.helpers import git_commit, make_tmp_dir
from tests.test_skill_delivery import DeliveryTask, PLAIN, link_op, skill_md

EVIDENCE = {}


class RequestTripwire:
    def __init__(self, test: unittest.TestCase):
        self.test = test
        self.depth = 0
        self.open_rows = []
        self.calls = collections.Counter()
        self.violations = []

    def checked(self, name, operation):
        def invoke(*args, **kwargs):
            self.calls[name] += 1
            if not self.depth or not self.open_rows:
                self.violations.append(name)
                raise AssertionError(f"{name}: no open gateway attempt")
            attempt, row = self.open_rows[-1]
            saved = json.loads((attempt.state / "skills/attempts" / f"{attempt.id[7:]}.json").read_text(
                encoding="utf-8"))
            if row["state"] != "started" or not any(r["state"] == "started" and
                    r["input_digest"] == row["input_digest"] for r in saved["requests"]):
                self.violations.append(name)
                raise AssertionError(f"{name}: the started request is not retained")
            return operation(*args, **kwargs)

        return invoke

    def install(self):
        from oah.design.sources import adapters, gitref, web
        from oah.discovery import attempts, gateway, providers, review
        from oah.rerank import typesafe
        from tests.test_design_judgement import FakeJudge

        real_send, real_request = gateway.send, attempts.Attempt.request

        def send(*args, **kwargs):
            self.depth += 1
            try:
                return real_send(*args, **kwargs)
            finally:
                self.depth -= 1

        @contextlib.contextmanager
        def request(attempt, *args, **kwargs):
            with real_request(attempt, *args, **kwargs) as row:
                self.open_rows.append((attempt, row))
                try:
                    yield row
                finally:
                    self.open_rows.pop()

        patch = self.test.enterContext
        patch(mock.patch.object(gateway, "send", send))
        patch(mock.patch.object(attempts.Attempt, "request", request))
        getter = self.checked("web.get", web.get_in_process)
        remote = self.checked("git.ls_remote", gitref.ls_remote)
        fetch = self.checked("sources.fetch", adapters.fetch)
        patch(mock.patch.object(web, "get", getter))
        patch(mock.patch.object(providers, "GETTER", getter))
        patch(mock.patch.object(web, "_fetch", self.checked("web.opener", web._fetch)))
        for cls in (web._PinnedHTTP, web._PinnedHTTPS):
            patch(mock.patch.object(cls, "request", self.checked("http.request", cls.request)))
        patch(mock.patch.object(urllib.request.OpenerDirector, "open",
                                self.checked("provider.opener", urllib.request.OpenerDirector.open)))
        patch(mock.patch.object(gitref, "ls_remote", remote))
        patch(mock.patch.object(adapters, "fetch", fetch))
        for name in ("judge", "score"):
            patch(mock.patch.object(typesafe.TypeSafeProvider, name,
                                    self.checked("provider." + name, getattr(typesafe.TypeSafeProvider, name))))
        patch(mock.patch.object(typesafe, "_post_json", self.checked("provider.send", typesafe._post_json)))
        patch(mock.patch.object(FakeJudge, "judge", self.checked("fixture.judge", FakeJudge.judge)))
        original = review.TRANSPORT or adapters.Transport()
        fields = {field.name for field in dataclasses.fields(original)}
        io = {name: function for name, function in (("getter", getter), ("ls_remote", remote), ("fetch", fetch))
              if name in fields}
        patch(mock.patch.object(review, "TRANSPORT", dataclasses.replace(original, **io)))


class EntryFlowTripwireTest(unittest.TestCase):
    def test_each_collector_exploration_and_advice_entry_keeps_a_request_attempt_open(self):
        cases = (
            ("review", "ReviewTest", "test_mit_skill_is_reviewed_and_pinned_in_its_own_state_root", "sources.fetch"),
            ("update", "ReviewTest", "test_update_returns_a_new_review_and_keeps_the_old_one_readable", "sources.fetch"),
            ("refresh", "CoverageLinkTest", "test_refresh_links_proposes_a_moved_upstream_and_never_moves_the_link",
             "git.ls_remote"),
            ("taskless-find", "PublicTermBoundaryTest", "test_public_value_is_sent_after_the_notice", "http.request"),
            ("design-inspection", "TasklessAdmissionTest",
             "test_draft_design_and_bare_search_carry_their_own_operation", "web.opener"),
            ("judge-advice", "SearchPathTest", "test_a_rechecked_choice_runs_its_query_first_and_sends_only_labels",
             "provider.send"),
            ("advice-options", "test_design_judgement.NewRubricTest",
             "test_each_rubric_is_one_choice_and_a_fits_question", "fixture.judge"),
            ("advice-capability-class", "test_design_judgement.SkillFitAndTaskTextTest",
             "test_task_text_needs_its_own_permission_and_the_verified_task_view", "fixture.judge"),
            ("advice-skill-fit", "test_skill_delivery.SkillFitAdviceTest",
             "test_loopback_jev_reorders_only_the_optional_items_of_one_check", "provider.send"),
        )
        for flow, cls, method, required in cases:
            with self.subTest(flow=flow):
                module_name, _, class_name = cls.rpartition(".")
                module = importlib.import_module("tests." + (module_name or "test_skill_discovery"))
                case = getattr(module, class_name)(method)
                setup = case.setUp
                guards = []

                def guarded_setup():
                    setup()
                    guard = RequestTripwire(case)
                    guard.install()
                    guards.append(guard)

                result = unittest.TestResult()
                with mock.patch.object(case, "setUp", side_effect=guarded_setup):
                    case.run(result)
                self.assertEqual(len(guards), 1)
                guard = guards[0]
                EVIDENCE[flow] = {"calls": dict(guard.calls), "violations": list(guard.violations),
                                  "cases": result.testsRun, "failures": len(result.failures),
                                  "errors": len(result.errors), "skips": len(result.skipped)}
                self.assertEqual(guard.violations, [])
                self.assertEqual(result.errors, [])
                self.assertEqual(result.failures, [])
                self.assertEqual(result.skipped, [])
                self.assertGreater(guard.calls[required], 0)

    def test_tripwire_refuses_a_request_without_a_gateway_even_if_a_caller_catches_it(self):
        guard = RequestTripwire(self)
        operation = mock.Mock()
        with self.assertRaisesRegex(AssertionError, "no open gateway attempt"):
            guard.checked("fixture.request", operation)()
        self.assertEqual(guard.violations, ["fixture.request"])
        operation.assert_not_called()


class TaskFlowTripwireTest(DeliveryTask, unittest.TestCase):
    def test_task_find_reaches_http_only_with_an_open_attempt(self):
        from oah import cli
        from oah.discovery import providers
        from tests.test_skill_discovery import SearchServer

        def linked(record):
            return [{"op": "set-tech", "label": "database", "value": "mysql", "decision": None, "note": None},
                    link_op(record)]

        guard = RequestTripwire(self)
        guard.install()
        self.build_skills((*PLAIN[:3], linked))
        server = SearchServer()
        self.addCleanup(server.close)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(providers, "ENDPOINT", server.endpoint), \
                mock.patch.object(providers, "POLICY", dataclasses.replace(providers.POLICY, allow_loopback=True)), \
                mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["skills", "find", "--task", "t1", "--json"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["requests"], 1)
        self.assertEqual(server.requests, ["mysql schema design"])
        self.assertEqual(guard.violations, [])
        self.assertEqual(guard.calls["web.get"], 1)
        self.assertEqual(guard.calls["http.request"], 1)
        EVIDENCE["task-find"] = dict(guard.calls)

    def test_e_active_legacy_order_reaches_the_provider_only_with_an_open_attempt(self):
        from tests.test_rerank import FakeServer, fake_key

        def respond(_path, body):
            answers = {key: {"type": "noul", "noul": 0.8} for key in body["questions"]}
            return 200, {"answers": answers, "model": "fixture"}, 0, {}

        guard = RequestTripwire(self)
        guard.install()
        self.build_skills(PLAIN)
        server = FakeServer(self, respond)
        config = self.repo / ".oah/config.toml"
        additions = {f".claude/skills/{name}/SKILL.md": skill_md(name, "Use the local checklist.\n")
                     for name in ("test-driven-development", "verification-before-completion")}
        additions[".oah/config.toml"] = config.read_text(encoding="utf-8") + \
            '\n[rerank]\nenabled=true\nprovider="typesafe"\n[rerank.typesafe]\nurl="' + server.url() + '"\n'
        git_commit(self.repo, additions, "local ordering fixture")
        self.env["TYPESAFE_API_KEY"] = fake_key()
        self.prepare(no_rerank=False)
        self.assertEqual(guard.violations, [])
        self.assertEqual(guard.calls["provider.score"], 1)
        self.assertEqual(guard.calls["provider.send"], 1)
        self.assertEqual(guard.calls["provider.opener"], 1)
        self.assertEqual(len(server.requests), 1)
        EVIDENCE["legacy-order"] = dict(guard.calls)


class CollectorFieldTest(unittest.TestCase):
    def test_every_callable_field_is_classified_and_every_io_field_is_wrapped(self):
        from oah import skillpack
        from oah.design.sources.adapters import IO_FIELDS, Transport
        from oah.discovery import gateway

        pure = frozenset(("git_url",))

        def classified(transport):
            callables = {field.name for field in dataclasses.fields(transport)
                         if callable(getattr(transport, field.name))}
            self.assertEqual(callables, IO_FIELDS | pure)

        original = Transport()
        classified(original)
        extended = dataclasses.make_dataclass("ExtraTransport", [("unclassified_request", type(lambda: None),
            dataclasses.field(default=staticmethod(lambda: None)))], bases=(Transport,), frozen=True)()
        with self.assertRaises(AssertionError):
            classified(extended)
        root = make_tmp_dir(self)
        operations = {name: mock.Mock(return_value=name) for name in IO_FIELDS}
        original = dataclasses.replace(original, **operations)
        with contextlib.chdir(root):
            guarded = gateway.collector_transport(original, skillpack.explore(1), state=root)
            for field in dataclasses.fields(original):
                if field.name in IO_FIELDS:
                    with self.subTest(field=field.name):
                        self.assertIsNot(getattr(guarded, field.name), getattr(original, field.name))
                        self.assertEqual(getattr(guarded, field.name)("fixture"), field.name)
                        operations[field.name].assert_called_once_with("fixture")
                else:
                    self.assertIs(getattr(guarded, field.name), getattr(original, field.name))
        summaries = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "skills/attempts").glob("*.json")]
        self.assertEqual(len(summaries), len(IO_FIELDS))
        self.assertTrue(all(summary["requests"][0]["state"] == "returned" for summary in summaries))
