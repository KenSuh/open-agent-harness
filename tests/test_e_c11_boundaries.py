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
from tests.test_e_c10_boundaries import cli_result, files
from tests.test_skill_delivery import DeliveryTask


class AdmissionTableTest(DeliveryTask, unittest.TestCase):
    request = relay_fixture.RelayTimesTest.request

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
                    with table_fixture.mapped_by(self, "admission", sample), \
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
                with table_fixture.mapped_by(self, "admission", sample), \
                        mock.patch.object(call, "_prepare", side_effect=sample.error):
                    result = cli_result(project.repo, project.env, "call", "codex", "--stage", "initial", "--task",
                        "t1", "--tier", "low", "--override-stage", "--launcher", "orca", "--json")

                table_fixture.assert_cli(self, result, row, sample)
                self.assertFalse(project.record.exists())
                self.assertFalse((project.state / "runs").exists())
                self.assertFalse((project.orca_state / "terminals").exists())


class LaunchTableTest(DeliveryTask, unittest.TestCase):
    request = relay_fixture.RelayTimesTest.request

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


CLI_CASES = {"admission": {"process": AdmissionTableTest, "native": AdmissionTableTest, "orca": OrcaAdmissionTableTest},
             "launch": {"process": LaunchTableTest}, "freeze": {"fix": FreezeTableTest},
             "settle": {"native": SettleTableTest}, "relay": {"capabilities": RelayTableTest}}


class BoundaryCompletenessTest(unittest.TestCase):
    def test_every_direct_typed_mapping_catch_is_declared(self):
        from oah import call
        import oah.discovery.boundaries as boundaries

        tree = ast.parse(inspect.getsource(call))
        owners = {}
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "boundaries" \
                        and node.func.attr == "mapped" and node.args and isinstance(node.args[0], ast.Constant):
                    owners[function.name] = node.args[0].value
        self.assertTrue(owners)
        kinds, seen = boundaries.error_types(), []
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            calls = [node for statement in handler.body for node in ast.walk(statement)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                     and node.func.id in owners and node.args and isinstance(node.args[0], ast.Name)
                     and node.args[0].id == handler.name]
            for node in calls:
                assert isinstance(node.func, ast.Name)
                boundary = owners[node.func.id]
                caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else (handler.type,)
                for kind in caught:
                    name = kind.attr if isinstance(kind, ast.Attribute) else kind.id if isinstance(kind, ast.Name) else ""
                    with self.subTest(boundary=boundary, caught=name, line=handler.lineno):
                        self.assertIn(name, kinds)
                        self.assertTrue(any(issubclass(kinds[name], incoming)
                                            for incoming in boundaries.incoming(boundary)), name)
                        seen.append((boundary, name))
        self.assertTrue(seen)

    def test_record_failures_and_every_declared_row_have_cli_cases(self):
        import oah.discovery.boundaries as boundaries
        from oah.discovery import records

        self.assertEqual(set(boundaries.TABLE), set(CLI_CASES))
        factories = table_fixture.samples(pathlib.Path("fixture"))
        produced = typing.get_args(typing.get_type_hints(records.io_failure)["return"])
        for boundary, spec in boundaries.TABLE.items():
            with self.subTest(boundary=boundary):
                rows = spec.rules
                self.assertEqual(set(CLI_CASES[boundary]), set(spec.surfaces))
                self.assertEqual(len(rows), len({row.incoming for row in rows}))
                kinds = boundaries.incoming(boundary)
                for error in produced:
                    self.assertTrue(any(issubclass(error, incoming) for incoming in kinds), error.__name__)
                for row in rows:
                    self.assertIn(row.incoming, factories)
                    for case in CLI_CASES[boundary].values():
                        self.assertTrue(unittest.defaultTestLoader.getTestCaseNames(case))

    def test_e_boundaries_use_the_table_instead_of_separate_exception_lists(self):
        import oah.discovery.boundaries as boundaries

        declared = ((boundary, spec, name) for boundary, spec in boundaries.TABLE.items()
                    for name in (*spec.handlers, *spec.readers))
        for boundary, spec, name in declared:
            module, _, attribute = name.rpartition(".")
            function = getattr(importlib.import_module(module), attribute)
            with self.subTest(boundary=boundary, function=name):
                tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
                function_node = tree.body[0]
                self.assertIsInstance(function_node, ast.FunctionDef)
                assert isinstance(function_node, ast.FunctionDef)
                pending: list[ast.AST] = list(function_node.body)
                caught = []
                while pending:
                    node = pending.pop()
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        continue
                    if isinstance(node, ast.ExceptHandler):
                        caught.append(ast.unparse(node.type) if node.type else "bare")
                    pending.extend(ast.iter_child_nodes(node))
                if name in spec.handlers:
                    self.assertEqual(caught, [], caught)
                contexts = [ast.unparse(item.context_expr) for node in ast.walk(function_node)
                            if isinstance(node, ast.With) for item in node.items]
                if name in spec.handlers:
                    self.assertTrue(any(value.startswith("boundaries.refusals(") for value in contexts), contexts)
                direct: list[str] = []
                for node in ast.walk(function_node):
                    if not isinstance(node, ast.Call):
                        continue
                    if isinstance(node.func, ast.Name) and node.func.id == "open":
                        direct.append(ast.unparse(node))
                    if isinstance(node.func, ast.Attribute) and node.func.attr in (
                            "read_bytes", "read_text", "read_json", "_read_json", "open"):
                        if ast.unparse(node.func.value) not in ("records", "record_files"):
                            direct.append(ast.unparse(node))
                self.assertEqual(direct, [], direct)
