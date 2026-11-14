from __future__ import annotations

import builtins
import contextlib
import errno
import hashlib
import io
import os
import pathlib
import re
import traceback
import unittest
from unittest import mock

from tests import e_c6_fixture, test_capabilities, test_e_relay_times, test_e_spent_evidence
from tests.e_c12_matrix import FAULTS, INPUT_MATRIX
from tests.test_e_c10_boundaries import cli_result
from tests.test_skill_delivery import DeliveryTask, PLAIN
from tests import test_design_cli as design_fixture
from tests.test_design_store import DesignRepo
from tests.helpers import make_tmp_dir


class InputContractTest(DeliveryTask, unittest.TestCase):
    request = test_e_relay_times.RelayTimesTest.request
    offered_parent_relay = test_e_spent_evidence.SpentEvidenceTest.offered_parent_relay

    def test_new_run_output_metadata_keeps_the_storage_io_contract(self):
        self.build_skills(PLAIN)
        original, reached = os.stat, []

        def denied(raw, *args, **options):
            target = isinstance(raw, (str, bytes, os.PathLike)) and pathlib.Path(os.fsdecode(raw)).name == 'prompt.md'
            writing = target and any(frame.f_code.co_name == 'write_prompt' and
                frame.f_code.co_filename.endswith('/oah/runs.py') for frame, _line in traceback.walk_stack(None))
            if writing:
                reached.append(str(raw))
                raise PermissionError(errno.EACCES, 'fixture new prompt output metadata denied', str(raw))
            return original(raw, *args, **options)

        with mock.patch.object(os, 'stat', denied):
            result = cli_result(self.repo, self.env, 'call', 'claude', '--stage', 'initial', '--task', 't1',
                                '--tier', 'low', '--json')
        self.assertTrue(reached)
        self.assertEqual(result[0], 69, str(result))
        self.assertTrue(result[2].startswith('OAH_STATE_IO:'), str(result))
        self.assertFalse(self.record.exists())

    def test_packaged_qualification_parse_keeps_its_admission_refusal(self):
        from oah.discovery import budget

        self.build_skills(PLAIN)
        raw = b'{'
        source = budget.PinnedSource(raw, hashlib.sha256(raw).hexdigest())
        with mock.patch.object(budget, '_profile_source', return_value=source):
            result = cli_result(self.repo, self.env, 'call', 'claude', '--stage', 'initial', '--task', 't1',
                                '--tier', 'low', '--json')
        self.assertEqual(result[0], 1, str(result))
        self.assertIn('OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown):', result[2])
        self.assertNotIn('Traceback', result[2])
        self.assertFalse(self.record.exists())

    def test_packet_read_after_binding_keeps_its_input_failure_category(self):
        self.build_skills(PLAIN)
        target = self.repo / '.oah/tasks/t1/PACKET.md'
        original = os.open
        earlier = target.read_bytes()
        for number, expected in ((errno.ENOENT, ('OAH_INTENT_SOURCE_INVALID', 1)),
                (errno.EACCES, ('OAH_INTENT_SOURCE_UNREADABLE', 69)), (errno.EIO, ('OAH_STATE_IO', 69))):
            reached = []

            def opened(raw, *args, **options):
                at_packet = any(frame.name == 'read_packet' and frame.filename.endswith('/oah/call.py')
                                for frame in traceback.extract_stack())
                if at_packet and isinstance(raw, (str, bytes, os.PathLike)) and pathlib.Path(os.fsdecode(raw)) == target:
                    reached.append(str(raw))
                    raise OSError(number, 'fixture packet input read failed after binding', str(raw))
                return original(raw, *args, **options)

            with self.subTest(errno=number), mock.patch.object(os, 'open', opened):
                result = cli_result(self.repo, self.env, 'call', 'claude', '--stage', 'initial', '--task', 't1',
                                    '--tier', 'low', '--packet', str(target), '--json')
            codes = re.findall(r'OAH_[A-Z_]+(?:\([^\n:]+\))?:', result[2])
            self.assertTrue(reached, 'the packet read after accepted-intent binding must be reached')
            self.assertEqual((codes[-1][:-1] if codes else '', result[0]), expected, str(result))
            self.assertEqual(target.read_bytes(), earlier)
            self.assertFalse(self.record.exists())

    def test_packet_recheck_for_needs_keeps_its_input_failure_category(self):
        project, _run, _need, _basis = e_c6_fixture.selected_ordinary(self)
        target = project.repo / '.oah/tasks/t1/PACKET.md'
        earlier, opened = target.read_bytes(), os.open
        calls = project.fix_calls()
        for number in (errno.ENOENT, errno.EACCES, errno.EIO):
            reached = []

            def changed(raw, *args, **options):
                recheck = any(frame.name == 'task_basis' and frame.filename.endswith('/oah/call.py')
                              for frame in traceback.extract_stack())
                if recheck and isinstance(raw, (str, bytes, os.PathLike)) and pathlib.Path(os.fsdecode(raw)) == target:
                    reached.append(str(raw))
                    raise OSError(number, 'fixture packet input recheck failed for needs', str(raw))
                return opened(raw, *args, **options)

            with self.subTest(errno=number), mock.patch.object(os, 'open', changed):
                result = cli_result(project.repo, project.env, 'call', 'codex', '--stage', 'initial', '--task', 't1',
                    '--tier', 'high', '--override-stage', '--no-rerank', '--packet', str(target),
                    '--skill-needs', str(project.work / 'needs.json'), '--json')
            self.assertTrue(reached)
            codes = re.findall(r'OAH_[A-Z_]+(?:\([^\n:]+\))?:', result[2])
            self.assertEqual((codes[0][:-1], result[0]), ('OAH_STATE_IO', 69) if number == errno.EIO
                             else ('OAH_SKILL_NEEDS_INVALID(stale)', 1), str(result))
            self.assertNotIn('Traceback', result[2])
            self.assertEqual(target.read_bytes(), earlier)
            self.assertEqual(project.fix_calls(), calls)

    def check_file(self, root, env, target, command, role):
        opened, io_opened = builtins.open, io.open
        earlier = target.read_bytes()
        observed = set()
        for fault, expected in zip(FAULTS, INPUT_MATRIX[role], strict=True):
            reached = []

            def changed(operation, raw, *args, **options):
                if isinstance(raw, (str, bytes, os.PathLike)) and pathlib.Path(os.fsdecode(raw)) == target:
                    reached.append(str(raw))
                    if fault == 'invalid-content':
                        return io.BytesIO(b'\xff')
                    number = {'missing': errno.ENOENT, 'not-regular': errno.EISDIR,
                              'denied': errno.EACCES, 'eio': errno.EIO}[fault]
                    kind = PermissionError if fault == 'denied' else OSError
                    raise kind(number, 'fixture input file failed', str(raw))
                return operation(raw, *args, **options)

            with self.subTest(role=role, fault=fault), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(builtins, 'open', lambda *a, **k: changed(opened, *a, **k)))
                stack.enter_context(mock.patch.object(io, 'open', lambda *a, **k: changed(io_opened, *a, **k)))
                stack.enter_context(mock.patch('oah.discovery.inputs.RETRY_SECONDS', 0.01))
                result = cli_result(root, env, *command)
                self.assertTrue(reached, 'the command must read its named input')
                observed.add(fault)
                codes = re.findall(r'OAH_[A-Z_]+(?:\([^\n:]+\))?:', result[1] + result[2])
                code = codes[-1][:-1] if codes else ''
                self.assertEqual((code, result[0]), expected, str(result))
                self.assertNotIn('Traceback', result[2])
                if fault != 'eio':
                    self.assertNotIn('the recorded ', result[2])
            self.assertEqual(target.read_bytes(), earlier)
        self.assertEqual(observed, set(FAULTS))

    def test_parent_inventory_input_preserves_the_base_usage_contract(self):
        _request, parent, _now = self.request()
        self.check_file(self.repo, self.env, parent, ['call', 'claude', '--stage', 'initial', '--task', 't1',
            '--tier', 'low', '--parent-inventory', str(parent), '--json'], 'parent-inventory')

    def test_needs_input_preserves_the_base_usage_contract(self):
        project, _run, _need, _basis = e_c6_fixture.selected_ordinary(self)
        target = project.work / 'needs.json'
        self.check_file(project.repo, project.env, target, ['call', 'codex', '--stage', 'initial', '--task', 't1',
            '--tier', 'high', '--override-stage', '--no-rerank', '--skill-needs', str(target), '--json'], 'skill-needs')

    def test_relay_inputs_preserve_the_base_usage_contract(self):
        request, _parent, _now = self.request()
        source = test_capabilities.write(self.work / 'request.json', request)
        invocation = test_capabilities.write(self.work / 'invocation.json', {
            'outcome': 'ok', 'at': request['created_at'], 'arguments_digest': 'sha256:' + '3' * 64})
        payload = test_capabilities.write(self.work / 'payload.txt', test_capabilities.RESULT)
        command = ['capabilities', 'relay', '--request', str(source), '--invocation', str(invocation),
                   '--result-file', str(payload), '--capability', request['capability_id'], '--json']
        for target, role in ((source, 'relay-request'), (invocation, 'relay-invocation'), (payload, 'relay-result')):
            self.check_file(self.repo, self.env, target, command, role)

    def test_native_result_input_preserves_the_base_contract(self):
        _now, _run, command = self.offered_parent_relay()
        target = pathlib.Path(command[command.index('--result-file') + 1])
        self.check_file(self.repo, self.env, target, command, 'native-result')

    def test_select_needs_input_preserves_the_base_usage_contract(self):
        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        import json

        target = project.work / 'needs.json'
        review = json.loads((original.run.path / 'skills.json').read_bytes())['bundle']['items'][0]['review']
        self.check_file(project.repo, project.env, target, ['skills', 'select', '--needs', str(target),
                                                         '--review', review, '--json'], 'skill-needs')

    def test_prompt_and_brief_inputs_preserve_the_base_file_contract(self):
        self.build_skills(PLAIN)
        real_open, real_io = builtins.open, io.open
        for option, name in (('--prompt-file', 'prompt-input.txt'), ('--brief-file', 'brief-input.txt')):
            target = self.work / name
            target.write_text('Fixture request details.\n', encoding='utf-8')
            earlier = target.read_bytes()
            command = ['call', 'claude', '--stage', 'initial', '--task', 't1', '--tier', 'low', option, str(target), '--json']
            for number in (errno.ENOENT, errno.EISDIR, errno.EACCES, errno.EIO):
                reached = []

                def fail(operation, raw, *args, **options):
                    if isinstance(raw, (str, bytes, os.PathLike)) and pathlib.Path(os.fsdecode(raw)) == target:
                        reached.append(str(raw))
                        raise OSError(number, 'fixture text input failed', str(raw))
                    return operation(raw, *args, **options)

                with self.subTest(option=option, errno=number), \
                        mock.patch.object(builtins, 'open', lambda *a, **k: fail(real_open, *a, **k)), \
                        mock.patch.object(io, 'open', lambda *a, **k: fail(real_io, *a, **k)):
                    result = cli_result(self.repo, self.env, *command)
                self.assertTrue(reached)
                self.assertEqual(result[0], 69, str(result))
                self.assertTrue(result[2].startswith('OAH_STATE_IO:'), str(result))
                self.assertEqual(target.read_bytes(), earlier)
                self.assertFalse(self.record.exists())


class DesignInputContractTest(DesignRepo, unittest.TestCase):
    oah = design_fixture.CliTest.oah
    file = design_fixture.CliTest.file
    created = design_fixture.CliTest.created

    def __init__(self, methodName='runTest'):
        super().__init__(methodName)
        self.repo = pathlib.Path()
        self.files = pathlib.Path()

    def setUp(self):
        self.repo = self.make_repo()
        self.enterContext(mock.patch.dict(os.environ, {'OAH_STATE_DIR': str(self.state(self.repo))}))
        self.files = make_tmp_dir(self)

    def test_design_file_part_and_coverage_input_io_keep_the_final_category(self):
        self.created()
        target = pathlib.Path(self.file('input.txt', 'Fixture design text.\n'))
        design = self.file('other.md', 'Fixture design body.\n')
        commands = (
            (('new', 'acme', 'other', '--title', 'Other', '--file', str(target)), 'OAH_DESIGN_INPUT_INVALID'),
            (('new', 'acme', 'other', '--title', 'Other', '--file', design, '--part', 'request=' + str(target)),
             'OAH_DESIGN_INPUT_INVALID'),
            (('coverage', 'edit', 'acme', 'booking', '--from', str(target)), 'OAH_COVERAGE_INVALID'),
        )
        opened, earlier = os.open, target.read_bytes()
        before = self.tree(self.repo, self.state(self.repo))
        for command, stale in commands:
            for number in (errno.ENOENT, errno.EISDIR, errno.EACCES, errno.EIO):
                reached = []

                def changed(raw, *args, **options):
                    if isinstance(raw, (str, bytes, os.PathLike)) and pathlib.Path(os.fsdecode(raw)) == target:
                        reached.append(str(raw))
                        raise OSError(number, 'fixture named design input read failed', str(raw))
                    return opened(raw, *args, **options)

                with self.subTest(command=command[0], errno=number), mock.patch.object(os, 'open', changed):
                    result = self.oah(*command)
                    self.assertTrue(reached)
                    code = re.findall(r'OAH_[A-Z_]+:', result[2])[0][:-1]
                    expected = ('OAH_STATE_IO', 69) if number == errno.EIO else (stale, 1)
                    self.assertEqual((code, result[0]), expected, str(result))
                    self.assertNotIn('Traceback', result[2])
                self.assertEqual(target.read_bytes(), earlier)
                self.assertEqual(self.tree(self.repo, self.state(self.repo)), before)
