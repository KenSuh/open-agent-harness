# 명령 경계 회귀 시험 — 선언한 오류 표의 모든 행과 실제 거절 경로를 확인한다.
from __future__ import annotations

import ast
import contextlib
import datetime as dt
import inspect
import importlib
import json
import pathlib
import textwrap
import typing
import unittest
from unittest import mock

import tests.e_c11_table_fixture as table_fixture
from tests import e_c9_fixture
from tests import test_capabilities as capability_fixture
from tests import test_e_relay_times as relay_fixture
from tests import test_e_spent_evidence as spent_fixture
from tests import test_launcher_orca as orca_fixture
from tests import test_e_prepared as prepared_fixture
from tests.test_e_c10_boundaries import cli_result, files
from tests.test_skill_delivery import DeliveryTask


class AdmissionTableTest(DeliveryTask, unittest.TestCase):
    request = relay_fixture.RelayTimesTest.request

    def test_windows_scratch_refusal_keeps_its_code_and_remedy_in_both_cli_modes(self):
        from oah import call
        from oah.scratch import ScratchError

        self.request()
        error = ScratchError(self.work / "LocalAppData", "fixture local folder is not absolute", "win32")
        for flags in ((), ("--json",)):
            with self.subTest(flags=flags):
                with mock.patch.object(call, "_prepare", side_effect=error):
                    code, out, err = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial",
                        "--task", "t1", "--tier", "low", *flags)

                self.assertEqual(code, 69, out + err)
                self.assertEqual(err, str(error) + "\n")
                self.assertEqual(out, "")
                self.assertEqual(self.runs(), [])
                self.assertFalse(self.record.exists())

    def test_stale_need_refusal_keeps_one_prefix_and_the_no_launch_message(self):
        from oah import call
        from oah.discovery.tasks import NeedsInvalid

        self.request()
        error = NeedsInvalid("stale", "the fixture packet changed after the needs were written")
        with mock.patch.object(call, "_prepare", side_effect=error):
            code, out, err = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial",
                "--task", "t1", "--tier", "low", "--json")

        self.assertEqual(code, 1, out + err)
        self.assertEqual(err, str(error) + "; nothing was launched\n")
        self.assertEqual(out, "")
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())

    def test_every_declared_admission_row_reaches_process_and_native_cli(self):
        from oah import call
        import oah.discovery.boundaries as boundaries

        self.request()
        for surface in boundaries.TABLE["admission"].surfaces:
            if surface == "orca":
                continue
            for row in boundaries.TABLE["admission"].rules:
                with self.subTest(boundary="admission", surface=surface, incoming=row.incoming):
                    sample = table_fixture.samples(self.work)[row.incoming]
                    handler = "_prepare_native" if surface == "native" else "_prepare"
                    with table_fixture.mapped_by(self, "admission", sample, surface), \
                            mock.patch.object(call, handler, side_effect=sample.error):
                        result = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial", "--task",
                                            "t1", "--tier", "low", "--launcher", surface, "--json")

                    table_fixture.assert_cli(self, result, row, sample)
                    self.assertEqual(self.runs(), [])
                    self.assertFalse(self.record.exists())


class OrcaAdmissionTableTest(unittest.TestCase):
    def test_every_declared_admission_row_reaches_orca_preparation_cli(self):
        from oah import call
        import oah.discovery.boundaries as boundaries

        project = orca_fixture.OrcaProject(self)
        for row in boundaries.TABLE["admission"].rules:
            with self.subTest(boundary="admission", surface="orca", incoming=row.incoming):
                sample = table_fixture.samples(project.repo)[row.incoming]
                with table_fixture.mapped_by(self, "admission", sample, "orca"), \
                        mock.patch.object(call, "_prepare", side_effect=sample.error):
                    result = cli_result(project.repo, project.env, "call", "codex", "--stage", "initial", "--task",
                        "t1", "--tier", "low", "--override-stage", "--launcher", "orca", "--json")

                table_fixture.assert_cli(self, result, row, sample)
                self.assertFalse(project.record.exists())
                self.assertFalse((project.state / "runs").exists())
                self.assertFalse((project.orca_state / "terminals").exists())


class LaunchTableTest(DeliveryTask, unittest.TestCase):
    request = relay_fixture.RelayTimesTest.request

    def test_already_changed_pack_refusal_is_kept_once_before_launch(self):
        from oah import call
        from oah.capabilities import plan
        from oah.skillpack import PackRefused

        request, parent, now = self.request()
        relay, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        error = PackRefused("OAH_SKILL_NOT_DELIVERABLE", "changed", "the preparation evidence expired; prepare again")
        with mock.patch.object(call, "_streamed_skill_bundle", side_effect=error):
            code, out, err = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial", "--task",
                "t1", "--tier", "low", "--parent-inventory", str(parent), "--relay-result", relay["id"], "--json")

        self.assertEqual(code, 1, out + err)
        self.assertEqual(err.rstrip().splitlines()[-1], str(error))
        self.assertFalse(self.record.exists())
        self.assertTrue(all(json.loads((run / "result.json").read_bytes())["status"] == "refused"
                            for run in self.runs()))

    def test_every_declared_launch_row_refuses_before_engine(self):
        from oah import call
        from oah.capabilities import plan
        import oah.discovery.boundaries as boundaries

        request, parent, now = self.request()
        relay, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        for row in boundaries.TABLE["launch"].rules:
            with self.subTest(boundary="launch", incoming=row.incoming):
                sample = table_fixture.samples(self.work)[row.incoming]
                with table_fixture.mapped_by(self, "launch", sample), \
                        mock.patch.object(call, "_streamed_skill_bundle", side_effect=sample.error):
                    result = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial", "--task",
                        "t1", "--tier", "low", "--parent-inventory", str(parent), "--relay-result", relay["id"], "--json")

                table_fixture.assert_cli(self, result, row, sample)
                self.assertFalse(self.record.exists())
                self.assertTrue(all(json.loads((run / "result.json").read_bytes())["status"] != "running"
                                    for run in self.runs()))


class FreezeTableTest(e_c9_fixture.CorrectionFixture):
    def test_every_first_correction_e_diagnostic_keeps_its_code_before_a_child(self):
        from oah import call
        from oah.engines.effort import EngineError
        from oah.review import fix

        project, original = self.correction_only()
        before, calls, admit = files(original.run.path), project.fix_calls(), call._admit_skills
        codes = [code for code in fix._ERROR_DIAGNOSTICS if code.startswith(
            ("OAH_CAPABILITY_", "OAH_SKILL_", "OAH_SKILLS_"))]
        self.assertTrue(codes)
        for expected in codes:
            with self.subTest(admission=expected):
                reached = []

                def refuse(request, *args, **options):
                    if request.stage == "correction":
                        reached.append(expected)
                        raise EngineError(expected, "fixture correction admission")
                    return admit(request, *args, **options)

                with mock.patch.object(call, "_admit_skills", refuse):
                    code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

                self.assertEqual(reached, [expected])
                self.assertEqual((code, result["code"]), (69 if expected == "OAH_SKILL_STATE_CORRUPT" else 1, expected),
                                 str(result) + err)
                self.assertIn(expected + ":", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(project.fix_calls(), calls)
                self.assertEqual(files(original.run.path), before)

    def test_every_declared_freeze_row_refuses_before_correction_child(self):
        from oah import call
        import oah.discovery.boundaries as boundaries

        project, original, _now, _parent, _relays = self.selected_parent(relay=True)
        before, calls = files(original.run.path), project.fix_calls()
        for row in boundaries.TABLE["freeze"].rules:
            with self.subTest(boundary="freeze", incoming=row.incoming):
                sample = table_fixture.samples(project.repo)[row.incoming]
                with table_fixture.mapped_by(self, "freeze", sample), \
                        mock.patch.object(call, "_freeze_e_checked", side_effect=sample.error):
                    code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

                expected, public = table_fixture.expected(row, sample)
                self.assertEqual((code, result["code"]), (expected, public), str(result) + err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(project.fix_calls(), calls)
                self.assertEqual(files(original.run.path), before)


class SettleTableTest(DeliveryTask, unittest.TestCase):
    offered_parent_relay = spent_fixture.SpentEvidenceTest.offered_parent_relay

    def test_every_declared_settle_row_refuses_without_settlement(self):
        from oah import call
        import oah.discovery.boundaries as boundaries

        _now, run, command = self.offered_parent_relay()
        before = files(self.state_dir)
        for row in boundaries.TABLE["settle"].rules:
            with self.subTest(boundary="settle", incoming=row.incoming):
                sample = table_fixture.samples(self.work)[row.incoming]
                with table_fixture.mapped_by(self, "settle", sample), \
                        mock.patch.object(call, "_native_basis", side_effect=sample.error):
                    result = cli_result(self.repo, self.env, *command)

                table_fixture.assert_cli(self, result, row, sample)
                self.assertFalse(self.record.exists())
                self.assertEqual(files(self.state_dir), before)


class RelayTableTest(DeliveryTask, unittest.TestCase):
    request = relay_fixture.RelayTimesTest.request

    def test_every_declared_relay_row_refuses_without_store_writes(self):
        from oah.capabilities import plan
        import oah.discovery.boundaries as boundaries

        request, _parent, _now = self.request()
        source = capability_fixture.write(self.work / "request.json", request)
        payload = capability_fixture.write(self.work / "result.txt", capability_fixture.RESULT)
        invocation = capability_fixture.write(self.work / "invocation.json", {
            "outcome": "ok", "at": request["created_at"], "arguments_digest": "sha256:" + "3" * 64})
        before = files(self.state_dir)
        for row in boundaries.TABLE["relay"].rules:
            with self.subTest(boundary="relay", incoming=row.incoming):
                sample = table_fixture.samples(self.work)[row.incoming]
                with table_fixture.mapped_by(self, "relay", sample), \
                        mock.patch.object(plan, "ingest", side_effect=sample.error):
                    result = cli_result(self.repo, self.env, "capabilities", "relay", "--request", str(source),
                        "--capability", request["capability_id"], "--result-file", str(payload),
                        "--invocation", str(invocation), "--json")

                table_fixture.assert_cli(self, result, row, sample)
                self.assertEqual(files(self.state_dir), before)
                self.assertFalse(self.record.exists())

    def test_relay_utc_range_check_refuses_before_either_write(self):
        request, _parent, _now = self.request()
        source = capability_fixture.write(self.work / "request.json", request)
        payload = capability_fixture.write(self.work / "result.txt", capability_fixture.RESULT)
        invocation = capability_fixture.write(self.work / "invocation.json", {
            "outcome": "ok", "at": "0001-01-01T00:00:00+01:00", "arguments_digest": "sha256:" + "3" * 64})
        before = files(self.state_dir)

        result = self.oah("capabilities", "relay", "--request", str(source), "--capability", request["capability_id"],
                          "--result-file", str(payload), "--invocation", str(invocation))

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_CAPABILITY_RELAY_REQUIRED(invalid):"), result.stderr)
        self.assertIn("cannot be represented in UTC", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(files(self.state_dir), before)


class ReadTableTest(DeliveryTask, unittest.TestCase):
    offered_parent_relay = spent_fixture.SpentEvidenceTest.offered_parent_relay

    def test_every_declared_skills_row_reaches_the_read_command(self):
        from oah import skillpack
        from oah.discovery import boundaries

        _now, run, _command = self.offered_parent_relay()
        for row in boundaries.TABLE["skills"].rules:
            sample = table_fixture.samples(self.work)[row.incoming]
            with table_fixture.mapped_by(self, "skills", sample), \
                    mock.patch.object(skillpack, "oah_read", side_effect=sample.error):
                result = cli_result(self.repo, self.env, "skills", "read", "--run", run.name, "--item", "1")
            table_fixture.assert_cli(self, result, row, sample)
            self.assertFalse(self.record.exists())


class WorkerTableTest(DeliveryTask, unittest.TestCase):
    terminals_opened = prepared_fixture.OrcaIdentityTest.terminals_opened
    orca_state = prepared_fixture.OrcaIdentityTest.orca_state
    orca_queue = prepared_fixture.OrcaIdentityTest.orca_queue

    def test_every_declared_worker_row_reaches_the_worker_command(self):
        from oah.discovery import boundaries
        from oah.launchers import orca
        from tests import test_e_prepared

        self.build_skills(test_e_prepared.PLAIN)
        prepared = self.prepare()
        self.orca_queue(prepared)
        for row in boundaries.TABLE["worker"].rules:
            sample = table_fixture.samples(self.work)[row.incoming]
            with table_fixture.mapped_by(self, "worker", sample), \
                    mock.patch.object(orca, "worker_prepared", side_effect=sample.error):
                result = cli_result(self.repo, self.env, "worker-run", prepared.run_id, "--stage", "initial")
            table_fixture.assert_cli(self, result, row, sample)
            self.assertEqual(json.loads((prepared.run.path / "result.json").read_bytes())["status"], "refused")
            self.assertFalse(self.record.exists())


class OtherTableTest(DeliveryTask, unittest.TestCase):
    def test_design_row_reaches_coverage_command(self):
        from oah.design import coverage
        from oah.discovery import boundaries
        from tests.test_skill_delivery import PLAIN

        self.build_skills(PLAIN)
        for row in boundaries.TABLE['design'].rules:
            sample = table_fixture.samples(self.work)[row.incoming]
            with table_fixture.mapped_by(self, 'design', sample), \
                    mock.patch.object(coverage, 'evaluate', side_effect=sample.error):
                result = cli_result(self.repo, self.env, 'design', 'coverage', 'acme', 'booking', '--json')
            table_fixture.assert_cli(self, result, row, sample)
            self.assertFalse(self.record.exists())

    def test_wait_row_reaches_saved_run_command(self):
        from oah.discovery import boundaries, records
        from tests.test_skill_delivery import PLAIN

        self.build_skills(PLAIN)
        prepared = self.prepare()
        before = (prepared.run.path / 'result.json').read_bytes()
        for row in boundaries.TABLE['wait'].rules:
            sample = table_fixture.samples(self.work)[row.incoming]
            with table_fixture.mapped_by(self, 'wait', sample), \
                    mock.patch.object(records, 'run_json', side_effect=sample.error):
                result = cli_result(self.repo, self.env, 'wait', prepared.run_id, '--timeout-seconds', '30')
            table_fixture.assert_cli(self, result, row, sample)
            self.assertEqual((prepared.run.path / 'result.json').read_bytes(), before)
            self.assertFalse(self.record.exists())


CLI_CASES = {"admission": {"process": AdmissionTableTest, "native": AdmissionTableTest, "orca": OrcaAdmissionTableTest},
             "launch": {"process": LaunchTableTest}, "freeze": {"fix": FreezeTableTest},
             "settle": {"native": SettleTableTest}, "relay": {"capabilities": RelayTableTest},
             "skills": {"skills": ReadTableTest}, "worker": {"orca": WorkerTableTest},
             "design": {"coverage": OtherTableTest}, "wait": {"wait": OtherTableTest}}


class BoundaryCompletenessTest(unittest.TestCase):
    def test_category_matrix_matches_every_command_row(self):
        from oah.discovery import boundaries, records
        from tests.e_c12_matrix import FAULTS, OWNER_MATRIX, OWNER_RUN_MATRIX, OWNER_KEPT_MATRIX

        issues = (records.Issue.MISSING, records.Issue.NOT_REGULAR, records.Issue.DENIED,
                  records.Issue.CONTENT, records.Issue.IO)
        self.assertEqual(set(OWNER_MATRIX), set(boundaries.TABLE))
        self.assertEqual(set(OWNER_MATRIX), set(OWNER_RUN_MATRIX))
        self.assertEqual(set(OWNER_MATRIX), set(OWNER_KEPT_MATRIX))
        for name in boundaries.TABLE:
            expected = OWNER_MATRIX[name]
            self.assertEqual(len(expected), len(FAULTS))
            self.assertEqual(len(OWNER_RUN_MATRIX[name]), len(FAULTS))
            self.assertEqual(len(OWNER_KEPT_MATRIX[name]), len(FAULTS))
            for index, issue in enumerate(issues):
                with self.subTest(command=name, fault=FAULTS[index]):
                    refusal = boundaries.mapped(name, records.RecordRefusal(pathlib.Path('fixture.json'), issue,
                        'fixture recorded file failed'))
                    assert isinstance(refusal, boundaries.CommandRefusal)
                    self.assertEqual((refusal.code, refusal.exit_code), expected[index])
                    self.assertTrue(OWNER_RUN_MATRIX[name][index])
                    self.assertTrue(OWNER_KEPT_MATRIX[name][index])

    def test_every_direct_typed_mapping_catch_is_declared(self):
        from oah.discovery import boundaries
        from tests.e_c12_structure import catches

        self.assertEqual(catches(), boundaries.CATCHES)

    def test_record_failures_and_every_declared_row_have_cli_cases(self):
        import io
        from oah.discovery import boundaries
        from tests.e_c12_matrix import OWNER_RULES, OWNER_SURFACES, ROW_REACH

        self.assertEqual(set(boundaries.TABLE), set(OWNER_RULES))
        for name in boundaries.TABLE:
            expected = OWNER_RULES[name]
            self.assertEqual(tuple((row.incoming, row.outward) for row in boundaries.TABLE[name].rules), expected)
            self.assertEqual(tuple(boundaries.TABLE[name].surfaces), OWNER_SURFACES[name])
        ROW_REACH.clear()
        cases = (
            AdmissionTableTest('test_every_declared_admission_row_reaches_process_and_native_cli'),
            OrcaAdmissionTableTest('test_every_declared_admission_row_reaches_orca_preparation_cli'),
            LaunchTableTest('test_every_declared_launch_row_refuses_before_engine'),
            FreezeTableTest('test_every_declared_freeze_row_refuses_before_correction_child'),
            SettleTableTest('test_every_declared_settle_row_refuses_without_settlement'),
            RelayTableTest('test_every_declared_relay_row_refuses_without_store_writes'),
            ReadTableTest('test_every_declared_skills_row_reaches_the_read_command'),
            WorkerTableTest('test_every_declared_worker_row_reaches_the_worker_command'),
            OtherTableTest('test_design_row_reaches_coverage_command'),
            OtherTableTest('test_wait_row_reaches_saved_run_command'),
        )
        out = io.StringIO()
        result = unittest.TextTestRunner(stream=out).run(unittest.TestSuite(cases))
        self.assertTrue(result.wasSuccessful(), out.getvalue())
        expected = {(boundary, surface, incoming) for boundary, rules in OWNER_RULES.items()
                    for surface in OWNER_SURFACES[boundary] for incoming, _outward in rules}
        self.assertEqual(ROW_REACH, expected, 'every literal owner row must reach its real CLI mapper')

    def test_e_boundaries_use_the_table_instead_of_separate_exception_lists(self):
        from oah.discovery import records
        from tests.e_c12_structure import architectural_problems

        self.assertFalse(issubclass(records.RecordRefusal, (OSError, ValueError)))
        self.assertEqual({item.value for item in records.Category}, {'stale', 'io'})
        self.assertFalse(hasattr(records, 'content'))
        self.assertEqual(architectural_problems(), [])
