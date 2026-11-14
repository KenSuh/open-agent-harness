from __future__ import annotations

import builtins
import contextlib
import datetime as dt
import errno
import io
import json
import os
import pathlib
from pathlib import Path
import re
import subprocess
import sys
import traceback
import unittest
from unittest import mock

from tests import e_c6_fixture, test_capabilities as capabilities
from tests import test_e_relay_times as relays, test_e_spent_evidence as spent
from tests import test_e_prepared as prepared_fixture, test_e_frozen_correction as correction_fixture
from tests.test_e_c10_boundaries import cli_result as recorded_cli_result
from tests.test_skill_delivery import DeliveryTask, PLAIN


def cli_result(*args, **options):
    from oah.discovery import records

    try:
        return recorded_cli_result(*args, **options)
    except records.InvalidRecord as exc:
        raise AssertionError(f'the CLI must return its typed refusal instead of raising {type(exc).__name__}: {exc}') from exc


@contextlib.contextmanager
def input_fault(target, reached, detail='fixture input read failed at the named recheck'):
    read, text, opened, io_opened = Path.read_bytes, Path.read_text, builtins.open, io.open

    def fault(operation, path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)) == target:
            reached.append(str(path))
            raise OSError(errno.EIO, detail, str(path))
        return operation(path, *args, **kwargs)

    with mock.patch.object(Path, 'read_bytes', lambda path: fault(read, path)), \
            mock.patch.object(Path, 'read_text', lambda *args, **kwargs: fault(text, *args, **kwargs)), \
            mock.patch.object(builtins, 'open', lambda *args, **kwargs: fault(opened, *args, **kwargs)), \
            mock.patch.object(io, 'open', lambda *args, **kwargs: fault(io_opened, *args, **kwargs)):
        yield


class RecordRegression(DeliveryTask, unittest.TestCase):
    request = relays.RelayTimesTest.request
    offered_parent_relay = spent.SpentEvidenceTest.offered_parent_relay
    selected_parent = correction_fixture.FrozenCorrectionTest.selected_parent
    correct = correction_fixture.FrozenCorrectionTest.correct

    def test_frozen_correction_does_not_read_its_new_result_before_launch(self):
        from oah import call
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('freeze')
        execute, changed = call.execute, []
        observer = ObservedFiles(case.storage, extra=case.extra)
        selected_file, executing = observer.selected, False

        def selected(path):
            return executing and selected_file(path)

        def after_freeze(prepared, **options):
            nonlocal executing
            self.assertIsNotNone(call._CORRECTION_PUBLICATION.get())
            path = prepared.run.path / 'result.json'
            path.write_bytes(b'\xff')
            changed.append(path)
            executing = True
            try:
                return execute(prepared, **options)
            finally:
                executing = False

        with mock.patch.object(call, 'execute', after_freeze), mock.patch.object(observer, 'selected', selected):
            result = case.invoke(observer)
        self.assertEqual(len(changed), 1, 'change only the newly published correction result at execute entry')
        self.assertEqual(result[0], 1, str(result))
        self.assertIn('OAH_FIX_STATE_CHANGED:', result[2])
        self.assertNotIn('Traceback', result[2])
        self.assertEqual((case.engine_spawns, case.runner_creations), (1, 0),
                         'launch exactly once from the frozen values before the B publication comparison')
        self.assertEqual(changed[0].read_bytes(), b'\xff', 'keep the changed correction output')
        for path, raw in case.before.items():
            if path.suffix == '.jsonl':
                self.assertTrue(path.read_bytes().startswith(raw), 'keep each earlier history record when appending the start')
            else:
                self.assertEqual(path.read_bytes(), raw, f'keep the earlier {path.name}')
        payloads = [row for row in observer.accesses if pathlib.Path(row.path).name == 'result.json'
                    and row.operation in ('open', 'read', 'os.open', 'os.read')]
        self.assertEqual(payloads, [], 'the frozen launch must not reopen its new result')
        print('FROZEN_RESULT', json.dumps(dict(exit=result[0], code='OAH_FIX_STATE_CHANGED',
            engines=case.engine_spawns, runners=case.runner_creations, result_payload_reads=len(payloads))), flush=True)

    def test_frozen_launch_record_faults_reach_the_first_correction_entry(self):
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario
        from tests.test_e_c12_observed import frozen_publication_transit

        now = dt.datetime.now(dt.timezone.utc)

        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(now.timestamp(), tz)

        self.enterContext(mock.patch.object(dt, 'datetime', Clock))
        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('freeze')
        accesses = case.observe()
        targets = [row for row in accesses if row.operation == 'open'
                   and 'call.py:execute' in row.chain and 'call.py:_run_prepared' in row.chain
                   and row.phase == 'before']
        self.assertEqual(len(targets), 4, 'reach the binding, selection index, selection and review rechecks')
        for target in targets:
            for fault in ('denied', 'invalid-content', 'eio'):
                with self.subTest(path=Path(target.path).name, fault=fault):
                    case.restore()
                    observer = ObservedFiles(case.storage, extra=case.extra, target=target, fault=fault)
                    with mock.patch('oah.discovery.records.RETRY_SECONDS', 0.01, create=True), \
                            mock.patch('oah.runs.RETRY_SECONDS', 0.01), mock.patch('oah.atomic.RETRY_SECONDS', 0.01):
                        result = case.invoke(observer)
                    self.assertTrue(observer.reached, 'reach the named frozen-launch OS access')
                    code, exit_code = ('OAH_STATE_IO', 69) if fault == 'eio' else ('OAH_FIX_STATE_CHANGED', 1)
                    codes = [value for value in re.findall(
                        r'(?m)^(OAH_[A-Z_]+(?:\([^\n:]+\))?):', result[2])
                        if value not in ('OAH_FIX_STARTED', 'OAH_FIX_APPLIED')]
                    actual = (codes[0] if result[0] and codes else '', result[0])
                    self.assertTrue(observer.record_refusals, 'E must raise its final refusal before publication')
                    first = observer.record_refusals[0]
                    self.assertEqual((first['path'], first['category']),
                        (observer.observed_path, 'io' if fault == 'eio' else 'stale'))
                    transit = frozen_publication_transit(observer, target, fault, (code, exit_code), actual)
                    if transit is not None:
                        code, exit_code = 'OAH_STATE_IO', 69
                    self.assertEqual(result[0], exit_code, str(result))
                    self.assertIn(code + ':', result[2])
                    self.assertNotIn('Traceback', result[2])
                    self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))
                    for path, raw in observer.retained.items():
                        actual_raw = path.read_bytes()
                        if transit is not None:
                            self.assertEqual(actual_raw, raw, 'keep every record when B masks the refusal')
                        elif path.suffix == '.jsonl':
                            self.assertTrue(actual_raw.startswith(raw), 'keep every earlier history entry when settling')
                        elif path.name == 'result.json' and path not in case.before:
                            before_result, after_result = json.loads(raw), json.loads(actual_raw)
                            identity = ('run_id', 'task', 'engine', 'stage')
                            self.assertEqual({key: after_result[key] for key in identity},
                                             {key: before_result[key] for key in identity},
                                             'B may settle this new run without replacing its identity')
                        else:
                            self.assertEqual(actual_raw, raw, 'keep every earlier file at the named recheck')
                    print('FROZEN_QUERY', json.dumps(dict(path=Path(target.path).name, fault=fault,
                        code=code, exit=exit_code, engines=case.engine_spawns,
                        runners=case.runner_creations, transit_exclusion=transit)), flush=True)

    def test_skills_find_stage_refusal_keeps_its_code_through_the_cli(self):
        from oah.routing import RoutingError

        project, _original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        earlier = {path: path.read_bytes() for path in project.state.rglob('*') if path.is_file()}
        calls = project.fix_calls()
        try:
            result = cli_result(project.repo, project.env, 'skills', 'find', '--task', 't1', '--needs',
                                str(project.work / 'needs.json'), '--engine', 'codex', '--json')
        except RoutingError as exc:
            self.fail(f'the CLI must return the stage refusal instead of raising it: {exc}')
        self.assertEqual(result[0], 1, str(result))
        self.assertIn('OAH_STAGE_ENGINE_MISMATCH:', result[2])
        self.assertNotIn('Traceback', result[2])
        self.assertEqual(project.fix_calls(), calls)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in earlier.items()))

    def test_expired_preparation_keeps_its_changed_refusal_through_the_cli(self):
        from oah import call

        self.build_skills(PLAIN)
        execute, kept = call.execute, {}

        def after_expiry(prepared, **options):
            path = prepared.run.path / 'skills.json'
            earlier = path.read_bytes()
            later = dt.datetime.fromisoformat(json.loads(earlier)['prepared_e']['expires_at']) + dt.timedelta(seconds=1)
            kept[path] = earlier

            class Later(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return later.astimezone(tz)

            with mock.patch.object(call.dt, 'datetime', Later):
                return execute(prepared, **options)

        with mock.patch.object(call, 'execute', after_expiry):
            result = cli_result(self.repo, self.env, 'call', 'claude', '--stage', 'initial', '--task', 't1',
                                '--tier', 'low', '--json')
        self.assertTrue(kept)
        self.assertEqual(result[0], 1, str(result))
        self.assertIn('OAH_SKILL_NOT_DELIVERABLE(changed):', result[2])
        self.assertIn('original preparation evidence expired', result[2])
        self.assertFalse(self.record.exists())
        self.assertTrue(all(path.read_bytes() == earlier for path, earlier in kept.items()))

    def test_saved_needs_input_keeps_the_declared_input_matrix_category(self):
        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        target = project.work / 'needs.json'
        kept = {path: path.read_bytes() for path in original.run.path.iterdir() if path.is_file()}
        earlier, calls = target.read_bytes(), project.fix_calls()
        real_open, real_io = builtins.open, io.open
        for number, expected, exit_code in ((errno.ENOENT, 'OAH_SKILL_NEEDS_INVALID(malformed)', 1),
                (errno.EACCES, 'OAH_SKILL_NEEDS_INVALID(malformed)', 1), (errno.EIO, 'OAH_STATE_IO', 69)):
            reached = []

            def changed(operation, raw, *args, **options):
                if isinstance(raw, (str, bytes, os.PathLike)) and Path(os.fsdecode(raw)) == target:
                    reached.append(str(raw))
                    kind = PermissionError if number == errno.EACCES else OSError
                    raise kind(number, 'fixture saved needs input read failed', str(raw))
                return operation(raw, *args, **options)

            with self.subTest(errno=number), \
                    mock.patch.object(builtins, 'open', lambda *a, **k: changed(real_open, *a, **k)), \
                    mock.patch.object(io, 'open', lambda *a, **k: changed(real_io, *a, **k)):
                code, out, err = cli_result(project.repo, project.env, 'call', 'codex', '--stage', 'initial',
                    '--task', 't1', '--tier', 'high', '--override-stage', '--no-rerank', '--skill-needs', str(target))
                self.assertTrue(reached)
                self.assertEqual(code, exit_code, out + err)
                self.assertTrue(err.rstrip().splitlines()[-1].startswith(expected + ':'), err)
                self.assertEqual(project.fix_calls(), calls)
            self.assertEqual(target.read_bytes(), earlier)
            self.assertTrue(all(path.read_bytes() == raw for path, raw in kept.items()))

    def test_recorded_needs_recheck_preserves_the_final_category(self):
        from oah import call

        project, _original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        target = project.work / "needs.json"
        read, read_io, task_needs = builtins.open, io.open, call.task_needs
        before, calls = target.read_bytes(), project.fix_calls()
        for number in (errno.ENOENT, errno.EACCES, errno.EIO):
            seen = []

            def fail(operation, path, *args, **options):
                if isinstance(path, (str, os.PathLike)) and Path(os.fsdecode(path)) == target:
                    seen.append(path)
                    kind = PermissionError if number == errno.EACCES else OSError
                    raise kind(number, "fixture recorded needs read failed at capture", str(path))
                return operation(path, *args, **options)

            def at_capture(*args, **options):
                if any(frame.name == 'capture' and Path(frame.filename).name == 'call.py'
                       for frame in traceback.extract_stack()):
                    with mock.patch.object(io, "open", lambda *a, **k: fail(read_io, *a, **k)), \
                            mock.patch.object(builtins, "open", lambda *a, **k: fail(read, *a, **k)):
                        return task_needs(*args, **options)
                return task_needs(*args, **options)

            with self.subTest(errno=number), mock.patch.object(call, 'task_needs', at_capture):
                code, out, err = cli_result(project.repo, project.env, "call", "codex", "--stage", "initial",
                    "--task", "t1", "--tier", "high", "--override-stage", "--no-rerank", "--skill-needs", str(target))
                self.assertTrue(seen)
                self.assertEqual(code, 69, out + err)
                expected = 'OAH_STATE_IO:' if number == errno.EIO else 'OAH_SKILL_STATE_CORRUPT:'
                self.assertTrue(err.rstrip().splitlines()[-1].startswith(expected), err)
                self.assertEqual(project.fix_calls(), calls)
            self.assertEqual(target.read_bytes(), before)

    def test_skills_read_prompt_faults_keep_the_matrix_code(self):
        _now, run, _command = self.offered_parent_relay()
        path = run / "prompt.md"
        earlier, read = path.read_bytes(), Path.read_bytes
        for fault in ("missing", "denied", "io"):
            with self.subTest(fault=fault):
                seen = []

                def changed(current):
                    if current == path:
                        seen.append(current)
                        kind, number = ((FileNotFoundError, errno.ENOENT) if fault == "missing" else
                                        (PermissionError, errno.EACCES) if fault == "denied" else (OSError, errno.EIO))
                        raise kind(number, "fixture prompt read failed", str(current))
                    return read(current)

                with mock.patch.object(Path, "read_bytes", changed):
                    code, out, err = cli_result(self.repo, self.env, "skills", "read", "--run", run.name, "--item", "1")
                self.assertTrue(seen)
                self.assertEqual(code, 69, out + err)
                self.assertTrue(err.startswith("OAH_STATE_IO:" if fault == "io" else "OAH_SKILLS_IO:"), err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(path.read_bytes(), earlier)

    def test_ordinary_preparation_retires_a_damaged_pointer_without_resolving_it(self):
        from oah.discovery import store, tasks

        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        folder = store.root(project.state) / "originals"
        index, = (folder / "tasks").glob("*.json")
        pointed = folder / (json.loads(index.read_bytes())["original"][7:] + ".json")
        pointed.unlink()
        tasks._index_path(project.state).unlink()
        before = set(original.run.path.parent.iterdir())
        code, out, err = cli_result(project.repo, project.env, "call", "codex", "--stage", "initial", "--task", "t1",
                                   "--tier", "high", "--override-stage", "--no-rerank")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(index.read_bytes()), {"original": None})
        newest, = set(original.run.path.parent.iterdir()) - before
        self.assertEqual(json.loads((newest / "result.json").read_bytes())["status"], "finished")
        self.assertFalse(pointed.exists())

    def test_settle_transient_permission_read_retries_then_succeeds(self):
        _now, run, command = self.offered_parent_relay()
        path, read, seen = run / "skills.json", Path.read_bytes, []

        def transient(current):
            if current == path:
                seen.append(current)
                if len(seen) == 1:
                    raise PermissionError(errno.EACCES, "fixture transient sharing denial", str(current))
            return read(current)

        with mock.patch.object(Path, "read_bytes", transient):
            code, out, err = cli_result(self.repo, self.env, *command)
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(code, 0, out + err)

    def test_launch_transient_permission_read_retries_then_succeeds(self):
        from oah import call

        _request, parent, relay = self.valid()
        execute, read, seen = call.execute, Path.read_bytes, []

        def launched(prepared, **options):
            target = prepared.run.path / "skills.json"

            def transient(current):
                if current == target:
                    seen.append(current)
                    if len(seen) == 1:
                        raise PermissionError(errno.EACCES, "fixture transient sharing denial", str(current))
                return read(current)

            with mock.patch.object(Path, "read_bytes", transient):
                return execute(prepared, **options)

        with mock.patch.object(call, "execute", launched):
            code, out, err = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial", "--task", "t1",
                "--tier", "low", "--parent-inventory", str(parent), "--relay-result", relay["id"], "--json")
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(code, 0, out + err)

    def test_freeze_transient_permission_read_retries_then_succeeds(self):
        project, original, _now, _parent, _relay = self.selected_parent(relay=True)
        project.queue_fix({"write": {"src/app.py": "print('corrected')\n"}})
        path, read, seen = original.run.path / "skills.json", Path.read_bytes, []

        def transient(current):
            if current == path:
                seen.append(current)
                if len(seen) == 1:
                    raise PermissionError(errno.EACCES, "fixture transient sharing denial", str(current))
            return read(current)

        with mock.patch.object(Path, "read_bytes", transient):
            code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(code, 0, str(result) + err)

    def test_freeze_transient_permission_at_captured_binding_retries_then_succeeds(self):
        project, _original, _now, _parent, _relay = self.selected_parent(relay=True)
        project.queue_fix({'write': {'src/app.py': "print('corrected')\n"}})
        target = project.state / 'runs/t1/intent-binding.json'
        read, reached = Path.read_bytes, []

        def transient(path):
            capturing = any(frame.name == '_captured_state' and frame.filename.replace('\\', '/').endswith('/oah/runs.py')
                            for frame in traceback.extract_stack())
            if path == target and capturing:
                reached.append(str(path))
                if len(reached) == 1:
                    raise PermissionError(errno.EACCES, 'fixture transient captured binding read denial', str(path))
            return read(path)

        with mock.patch.object(Path, 'read_bytes', transient):
            code, result, error = self.correct(project, dt.datetime.now(dt.timezone.utc))

        self.assertGreaterEqual(len(reached), 2, 'the captured read must retry the same transient denial')
        self.assertEqual(code, 0, str(result) + error)

    def test_prepared_handoff_refusals_are_recorded_before_a_child(self):
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        for name in ('launch', 'background'):
            with self.subTest(command=name):
                case = Scenario('scenario')
                case.setUp()
                try:
                    case.scenario(name)
                    target = next(access for access in case.observe() if pathlib.Path(access.path).name == 'native-offer.json')
                    for fault, expected, exit_code in (('denied', 'OAH_RUN_LAUNCH_INVALID', 1),
                                                       ('eio', 'OAH_STATE_IO', 69)):
                        with self.subTest(fault=fault):
                            case.restore()
                            observer = ObservedFiles(case.storage, extra=case.extra, target=target, fault=fault)
                            with mock.patch('oah.discovery.records.RETRY_SECONDS', 0.01, create=True):
                                result = case.invoke(observer)

                            self.assertTrue(observer.reached)
                            self.assertEqual(result[0], exit_code, str(result))
                            self.assertIn(expected + ':', result[2])
                            self.assertNotIn('Traceback', result[2])
                            saved, = case.storage.glob('runs/t1/r*/result.json')
                            self.assertEqual(json.loads(saved.read_bytes())['status'], 'refused')
                            self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))
                finally:
                    case.doCleanups()

    def test_finished_run_followup_read_refusal_keeps_the_dispatch_and_terminal_record(self):
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('launch')
        target = next(access for access in case.observe() if access.phase == 'after-engine' and
            pathlib.Path(access.path).name == 'design.json' and 'call.py:_finish_inline_delivery' in access.chain)
        for fault in ('denied', 'eio'):
            with self.subTest(fault=fault):
                case.restore()
                observer = ObservedFiles(case.storage, extra=case.extra, target=target, fault=fault)
                with mock.patch('oah.discovery.records.RETRY_SECONDS', 0.01, create=True):
                    result = case.invoke(observer)
                self.assertTrue(observer.reached)
                self.assertEqual((case.engine_spawns, case.runner_creations), (1, 0))
                self.assertEqual(result[0], 69 if fault == 'eio' else 1, str(result))
                self.assertIn(('OAH_STATE_IO' if fault == 'eio' else 'OAH_RUN_LAUNCH_INVALID') + ':', result[2])
                self.assertIn('already took place', result[2])
                self.assertIn('follow-up read failed', result[2])
                self.assertNotIn('nothing was sent', result[2])
                saved, = case.storage.glob('runs/t1/r*/result.json')
                self.assertEqual(json.loads(saved.read_bytes())['status'], 'finished')
                self.assertEqual(saved.read_bytes(), observer.retained[saved])

    def test_background_followup_read_refusal_keeps_the_created_runner_and_published_queue(self):
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('background')
        target = next(access for access in case.observe() if access.phase == 'after-runner' and
            pathlib.Path(access.path).name == 'skills.json' and 'call_cli.py:_print_skills' in access.chain)
        for fault in ('denied', 'eio'):
            with self.subTest(fault=fault):
                case.restore()
                observer = ObservedFiles(case.storage, extra=case.extra, target=target, fault=fault)
                with mock.patch('oah.discovery.records.RETRY_SECONDS', 0.01, create=True):
                    result = case.invoke(observer)
                self.assertTrue(observer.reached)
                self.assertEqual((case.engine_spawns, case.runner_creations), (0, 1))
                self.assertEqual(result[0], 69, str(result))
                self.assertIn(('OAH_STATE_IO' if fault == 'eio' else 'OAH_SKILL_STATE_CORRUPT') + ':', result[2])
                self.assertIn('runner was already created', result[2])
                self.assertIn('follow-up read failed', result[2])
                self.assertNotIn('nothing was sent', result[2])
                saved, = case.storage.glob('runs/t1/r*/result.json')
                self.assertEqual(json.loads(saved.read_bytes())['status'], 'queued')
                self.assertEqual(saved.read_bytes(), observer.retained[saved])

    def test_child_followup_refusal_keeps_the_created_runner_and_its_queued_record(self):
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('background')
        target = next(access for access in case.observe() if access.phase == 'after-runner' and
            pathlib.Path(access.path).name == 'result.json' and access.operation == 'read' and 'call.py:_launch_main' in access.chain)
        observer = ObservedFiles(case.storage, extra=case.extra, target=target, fault='eio')

        result = case.invoke(observer)

        self.assertTrue(observer.reached)
        self.assertEqual((case.engine_spawns, case.runner_creations), (0, 1))
        self.assertEqual(result[0], 69, str(result))
        self.assertIn('OAH_STATE_IO:', result[2])
        self.assertIn('runner was already created', result[2])
        self.assertIn('follow-up read failed', result[2])
        self.assertNotIn('OAH_RUN_LAUNCH_FAILED:', result[2])
        saved, = case.storage.glob('runs/t1/r*/result.json')
        self.assertEqual(json.loads(saved.read_bytes())['status'], 'queued')
        self.assertEqual(saved.read_bytes(), observer.retained[saved])

    def test_worker_followup_refusal_keeps_the_completed_dispatch_and_record(self):
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('worker')
        target = next(access for access in case.observe() if access.phase == 'after-engine' and
            pathlib.Path(access.path).name == 'design.json' and access.operation in ('read', 'os.read') and
            'call.py:_design_after' in access.chain)
        observer = ObservedFiles(case.storage, extra=case.extra, target=target, fault='eio')
        with mock.patch('tests.e_c12_scenarios.cli_result', cli_result):
            result = case.invoke(observer)

        self.assertTrue(observer.reached)
        self.assertEqual((case.engine_spawns, case.runner_creations), (1, 0))
        self.assertEqual(result[0], 69, str(result))
        self.assertIn('OAH_STATE_IO:', result[2])
        self.assertIn('the run already took place', result[2])
        self.assertIn('the follow-up read failed', result[2])
        saved = case.run_dir / 'result.json'
        self.assertEqual(saved.read_bytes(), observer.retained[saved])

    def test_native_offer_and_settlement_identity_parse_uses_the_read_command_category(self):
        _now, run, settle = self.offered_parent_relay()
        offer = run / 'native-offer.json'
        original_offer = offer.read_bytes()
        for name in ('native-offer.json', 'native-settlement.json'):
            if name == 'native-settlement.json':
                offer.write_bytes(original_offer)
                result = cli_result(self.repo, self.env, *settle)
                self.assertEqual(result[0], 0, str(result))
            with self.subTest(record=name):
                path = run / name
                data = json.loads(path.read_bytes())
                data['id'] = 'sha256:' + '0' * 64
                path.write_text(json.dumps(data), encoding='utf-8')
                earlier = {file: file.read_bytes() for file in run.iterdir() if file.is_file()}

                result = cli_result(self.repo, self.env, 'skills', 'read', '--run', run.name, '--item', '1')

                self.assertEqual(result[0], 69, str(result))
                self.assertIn('OAH_SKILLS_IO:', result[2])
                self.assertNotIn('Traceback', result[2])
                self.assertFalse(self.record.exists())
                self.assertEqual({file: file.read_bytes() for file in run.iterdir() if file.is_file()}, earlier)

    def test_relay_input_symlinks_are_followed_with_the_base_contract(self):
        request, _parent, _now = self.request()
        source = capabilities.write(self.work / "request.json", request)
        invocation = capabilities.write(self.work / "invocation.json", {
            "outcome": "ok", "at": request["created_at"], "arguments_digest": "sha256:" + "3" * 64})
        payload = capabilities.write(self.work / "payload.txt", capabilities.RESULT)
        linked = []
        for index, target in enumerate((source, invocation, payload)):
            link = self.work / f"input-{index}"
            link.symlink_to(target)
            linked.append(link)
        code, out, err = cli_result(self.repo, self.env, "capabilities", "relay", "--request", str(linked[0]),
            "--invocation", str(linked[1]), "--result-file", str(linked[2]), "--capability", request["capability_id"])
        self.assertEqual(code, 0, out + err)

    def test_parent_inventory_input_keeps_normal_file_semantics(self):
        _request, parent, relay = self.valid()
        lstat = Path.lstat

        def input_metadata(path, *args, **options):
            if path == parent:
                self.fail('the parent input must not apply a stored-record reparse check')
            return lstat(path, *args, **options)

        with mock.patch.object(Path, 'lstat', input_metadata):
            result = cli_result(self.repo, self.env, 'call', 'claude', '--stage', 'initial', '--task', 't1',
                '--tier', 'low', '--parent-inventory', str(parent), '--relay-result', relay['id'], '--json')
        self.assertEqual(result[0], 0, str(result))

    def valid(self):
        from oah.capabilities import plan
        request, parent, now = self.request()
        record, _ = plan.ingest(self.state_dir, request, capability=request['capability_id'],
            payload=capabilities.RESULT.encode('utf-8'), invocation={'outcome': 'ok', 'at': request['created_at'],
            'arguments_digest': 'sha256:' + '3' * 64}, now=now)
        return request, parent, record

    def prepared(self):
        from oah import call, call_cli
        from oah.secrets import NoKeychain
        _request, parent, record = self.valid()
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
            prepared = call.prepare(call.CallRequest(engine='claude', stage='initial', task='t1', tier='low',
                parent_inventory=str(parent), relay_results=(record['id'],)), call_cli.load_settings(self.repo),
                root=self.repo, state_dir=self.state_dir, env=self.env, footer=tools.footer,
                has_footer=tools.has_footer, keychain=NoKeychain())
        return prepared, parent

    def test_background_parent_preserves_launcher_state_io(self):
        _request, parent, record = self.valid()
        for detail in ('fixture launcher read failed', '\U0001f600' * 8192):
            with self.subTest(diagnostic_bytes=len(detail.encode('utf-8'))):
                self.check_background_refusal(parent, record, detail)

    def check_background_refusal(self, parent, record, detail):
        from oah import call
        run_command, popen = call.subprocess.run, call.subprocess.Popen
        observations = []
        reached = []

        def launch(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and len(argv) >= 2 and argv[-2] == 'launch_main':
                out, err = io.StringIO(), io.StringIO()
                with input_fault(parent, reached, detail), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = call.launch_main(argv[-1])
                observations.append({'launcher_rc': code, 'launcher_stdout': out.getvalue(),
                                     'launcher_stderr': err.getvalue()})
                return subprocess.CompletedProcess(argv, code, out.getvalue().encode(), err.getvalue().encode())
            return run_command(argv, *args, **kwargs)

        def runner(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and len(argv) >= 2 and argv[-2] == 'background_main':
                self.fail('the fixture runner started after the failed record read')
            return popen(argv, *args, **kwargs)

        with mock.patch.object(call.subprocess, 'run', launch), mock.patch.object(call.subprocess, 'Popen', runner):
            result = cli_result(self.repo, self.env, *['call', 'claude', '--stage', 'initial', '--task', 't1',
                '--tier', 'low', '--parent-inventory', str(parent), '--relay-result', record['id'],
                '--background', '--json'])
        states = [json.loads((run / 'result.json').read_bytes()) for run in self.runs()]
        print('BACKGROUND_PARENT', json.dumps({'observed': [{'launcher_rc': item['launcher_rc'],
            'launcher_bytes': len(item['launcher_stdout'].encode('utf-8'))} for item in observations],
            'cli': [result[0], result[1], result[2][:300]],
            'records': [{'status': item['status'], 'error': item.get('error')} for item in states],
            'read_reached': bool(reached), 'engine_ran': self.record.exists()}), file=sys.__stdout__, flush=True)
        self.assertTrue(reached)
        self.assertTrue(observations)
        self.assertFalse(self.record.exists())
        self.assertEqual(result[0], 69, result)
        self.assertTrue(result[2].rstrip().splitlines()[-1].startswith('OAH_STATE_IO:'), result)
        self.assertTrue(all(item['status'] == 'refused' for item in states), states)

    def test_finish_recheck_preserves_parent_state_io(self):
        from oah import skillpack

        _request, parent, record = self.valid()
        finish = skillpack.finish
        reached = []

        def recheck(*args, **kwargs):
            with input_fault(parent, reached):
                return finish(*args, **kwargs)

        for launcher in ('process', 'native'):
            with self.subTest(launcher=launcher), mock.patch.object(skillpack, 'finish', recheck):
                reached.clear()
                result = cli_result(self.repo, self.env, *['call', 'claude', '--stage', 'initial', '--task', 't1',
                    '--tier', 'low', '--launcher', launcher, '--parent-inventory', str(parent),
                    '--relay-result', record['id'], '--json'])
                print('FINISH_PARENT', launcher, result, 'read_reached', bool(reached),
                      file=sys.__stdout__, flush=True)
                self.assertTrue(reached)
                self.assertFalse(self.record.exists())
                self.assertEqual(result[0], 69, result)
                self.assertTrue(result[2].rstrip().splitlines()[-1].startswith('OAH_STATE_IO:'), result)

    def test_recorded_parent_schema_refusal_is_classified_inside_the_reader(self):
        from oah.capabilities import inventory
        from oah.discovery import records

        _request, parent, relay = self.valid()
        read_parent, reached, refusals = inventory.read_parent, [], []
        constructor = records.InvalidRecord.__init__

        def changed(path, **options):
            if not reached and any(frame.name == 'capture' and Path(frame.filename).name == 'call.py'
                                   for frame in traceback.extract_stack()):
                saved = json.loads(parent.read_bytes())
                saved['digest'] = 'sha256:' + '0' * 64
                parent.write_text(json.dumps(saved), encoding='utf-8')
                reached.append(parent.read_bytes())
            return read_parent(path, **options)

        def refused(instance, *args, **options):
            constructor(instance, *args, **options)
            refusals.append(instance)

        with mock.patch.object(inventory, 'read_parent', changed), \
                mock.patch.object(records.InvalidRecord, '__init__', refused):
            result = cli_result(self.repo, self.env, 'call', 'claude', '--stage', 'initial', '--task', 't1',
                '--tier', 'low', '--parent-inventory', str(parent), '--relay-result', relay['id'], '--json')

        self.assertTrue(reached, 'the fixture changes its digest only at the saved-input capture step')
        self.assertEqual(result[0], 69, str(result))
        self.assertIn('OAH_SKILL_STATE_CORRUPT:', result[2])
        self.assertNotIn('Traceback', result[2])
        self.assertTrue(any(getattr(refusal, 'path', None) == parent and
                            not isinstance(refusal, (OSError, ValueError)) for refusal in refusals))
        self.assertEqual(parent.read_bytes(), reached[0])
        self.assertFalse(self.record.exists())

    def test_capability_stat_denial_without_prepared_identity_uses_stale(self):
        from oah import call
        from tests import test_fix

        project = test_fix.FixProject(self, config='\n[models]\nfetch_scores=false\n')
        execute, check, lstat = call.execute, call._check_e, Path.lstat
        reached, targets = [], []
        calls = project.fix_calls()

        def denied(path, *args, **kwargs):
            if targets and path == targets[-1]:
                reached.append(str(path))
                raise PermissionError(errno.EACCES, 'fixture capability stat denied', str(path))
            return lstat(path, *args, **kwargs)

        def checked(*args, **kwargs):
            with mock.patch.object(Path, 'lstat', denied):
                return check(*args, **kwargs)

        def at_launch(prepared, **options):
            self.assertIsNone(prepared.e_digest)
            target = prepared.run.path / 'capabilities.json'
            target.write_text('{}', encoding='utf-8')
            targets.append(target)
            with mock.patch.object(call, '_check_e', checked):
                return execute(prepared, **options)

        with mock.patch.object(call, 'execute', at_launch):
            result = cli_result(project.repo, project.env, *['call', 'codex', '--stage', 'initial', '--task', 't1',
                '--tier', 'high', '--override-stage', '--no-rerank', '--json'])
        print('ABSENT_PREPARATION_CAPS_STAT', result, 'stat_reached', bool(reached),
              file=sys.__stdout__, flush=True)
        self.assertTrue(reached)
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(result[0], 1, result)
        self.assertTrue(result[2].rstrip().splitlines()[-1].startswith('OAH_RUN_LAUNCH_INVALID:'), result)

    def test_changed_receiving_record_refuses_stale_without_new_writes(self):
        _now, run, command = self.offered_parent_relay()
        first = self.oah(*command)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        path = run / 'skills-receiving.json'
        receiving = json.loads(path.read_bytes())
        self.assertNotIn('available_usage', receiving)
        receiving['delivery'] = 'fixture changed delivery'
        path.write_text(json.dumps(receiving), encoding='utf-8')
        before = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}

        result = self.oah(*command)

        print('CHANGED_RECEIVING', json.dumps({'rc': result.returncode, 'out': result.stdout,
            'err': result.stderr, 'record_preserved': path.read_bytes() == before[path.name]}),
            file=sys.__stdout__, flush=True)
        self.assertEqual({p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}, before)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith('OAH_NATIVE_OFFER_STALE:'), result.stderr)

    def test_receiving_from_another_settlement_refuses_retry_without_writes(self):
        _now, first, command = self.offered_parent_relay()
        settled = self.oah(*command)
        self.assertEqual(settled.returncode, 0, settled.stdout + settled.stderr)
        first_result = json.loads(settled.stdout)
        refs = json.loads((first / 'skills.json').read_bytes())['prepared_e']['references']
        args = ['call', 'claude', '--stage', 'initial', '--task', 't1', '--tier', 'low',
                '--launcher', 'native', '--parent-inventory', refs['parent_inventory'], '--json']
        for ref in refs['relay_results']:
            args.extend(('--relay-result', ref))
        offered = self.oah(*args)
        self.assertEqual(offered.returncode, 0, offered.stdout + offered.stderr)
        offer = json.loads(offered.stdout)
        second = self.state_dir / 'runs/t1' / offer['run']
        second_command = list(command)
        for flag, value in (('--run', second.name), ('--offer-digest', offer['message']['sha256']),
                            ('--host-child-id', 'child-two')):
            second_command[second_command.index(flag) + 1] = value
        second_settled = self.oah(*second_command)
        self.assertEqual(second_settled.returncode, 0, second_settled.stdout + second_settled.stderr)
        receiving = json.loads(second_settled.stdout)['receiving']
        target = first / 'skills-receiving.json'
        target.write_bytes((second / target.name).read_bytes())
        before = {path.name: path.read_bytes() for path in first.iterdir() if path.is_file()}

        retried = self.oah(*command)

        self.assertNotEqual(first_result['settlement']['receiving_ref'], receiving['id'])
        self.assertEqual(retried.returncode, 1, retried.stdout + retried.stderr)
        self.assertTrue(retried.stderr.startswith('OAH_NATIVE_OFFER_STALE:'), retried.stderr)
        self.assertEqual({path.name: path.read_bytes() for path in first.iterdir() if path.is_file()}, before)

    def test_worker_prompt_faults_keep_the_command_category_and_retire_the_queue(self):
        from oah.discovery import records
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('worker')
        target = next(access for access in case.observe() if pathlib.Path(access.path).name == 'prompt.md'
                      and access.operation in ('read', 'os.read'))
        for fault in ('missing', 'denied', 'eio'):
            with self.subTest(fault=fault):
                case.restore()
                earlier = (case.run_dir / 'prompt.md').read_bytes()
                observer = ObservedFiles(case.storage, target=target, fault=fault)
                with mock.patch('oah.discovery.records.RETRY_SECONDS', 0.01, create=True), \
                        mock.patch('oah.runs.RETRY_SECONDS', 0.01):
                    try:
                        result = case.invoke(observer)
                    except records.InvalidRecord as exc:
                        self.fail(f'the CLI must return the prompt refusal: {exc}')
                self.assertTrue(observer.reached)
                expected = ('OAH_STATE_IO', 69) if fault == 'eio' else ('OAH_RUN_LAUNCH_INVALID', 1)
                self.assertEqual(result[0], expected[1], str(result))
                self.assertIn(expected[0] + ':', result[2])
                self.assertNotIn('Traceback', result[2])
                self.assertEqual(json.loads((case.run_dir / 'result.json').read_bytes())['status'], 'refused')
                self.assertEqual((case.run_dir / 'prompt.md').read_bytes(), earlier)
                self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))

    def test_worker_unreadable_result_retires_only_stale_queues(self):
        from oah import runs
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('worker')
        target = case.run_dir / 'result.json'
        earlier = target.read_bytes()
        read = runs.WorkerRun._read_bytes
        for fault in ('missing', 'not-regular', 'denied', 'invalid-content', 'eio'):
            with self.subTest(fault=fault):
                case.restore()
                reached = []

                def changed(run, name):
                    if name == 'result.json':
                        reached.append(name)
                        if fault == 'invalid-content':
                            return b'\xff'
                        number = {'missing': errno.ENOENT, 'not-regular': errno.EISDIR,
                                  'denied': errno.EACCES, 'eio': errno.EIO}[fault]
                        kind = PermissionError if fault == 'denied' else OSError
                        raise kind(number, 'fixture saved result cannot be read', str(target))
                    return read(run, name)

                with mock.patch.object(runs.WorkerRun, '_read_bytes', changed):
                    result = cli_result(case.root, case.environment, *case.command)
                self.assertTrue(reached)
                expected = 'OAH_STATE_IO' if fault == 'eio' else 'OAH_RUN_LAUNCH_INVALID'
                self.assertEqual(result[0], 69 if fault == 'eio' else 1, str(result))
                self.assertTrue(result[2].startswith(expected + ':'), str(result))
                self.assertEqual(target.read_bytes(), earlier)
                self.assertEqual((case.run_dir / 'worker.claim').exists(), fault != 'eio')
                self.assertTrue((case.run_dir / 'stderr.log').is_file())
                self.assertIn(expected, (case.run_dir / 'stderr.log').read_text(encoding='utf-8'))
                self.assertFalse(case.record.exists())
                retried = cli_result(case.root, case.environment, *case.command)
                if fault == 'eio':
                    self.assertEqual(retried[0], 0, str(retried))
                else:
                    self.assertEqual(retried[0], 1, str(retried))
                    self.assertIn('OAH_RUN_NOT_QUEUED:', retried[2])
                    self.assertFalse(case.record.exists())

    def test_worker_result_metadata_refusal_retires_only_stale_queues(self):
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('worker')
        target = case.run_dir / 'result.json'
        earlier = target.read_bytes()
        lstat, stat = os.lstat, os.stat
        for number in (errno.ENOENT, errno.EACCES, errno.EIO):
            with self.subTest(errno=number):
                case.restore()
                reached = []

                def changed(operation, raw, *args, **options):
                    if isinstance(raw, (str, bytes, os.PathLike)) and Path(os.fsdecode(raw)) == target:
                        reached.append(str(raw))
                        kind = PermissionError if number == errno.EACCES else OSError
                        raise kind(number, 'fixture result metadata cannot be read', str(target))
                    return operation(raw, *args, **options)

                with mock.patch.object(os, 'lstat', lambda *args, **options: changed(lstat, *args, **options)), \
                        mock.patch.object(os, 'stat', lambda *args, **options: changed(stat, *args, **options)), \
                        mock.patch('oah.discovery.records.RETRY_SECONDS', 0.01, create=True):
                    result = cli_result(case.root, case.environment, *case.command)
                self.assertTrue(reached)
                code = 'OAH_STATE_IO' if number == errno.EIO else 'OAH_RUN_LAUNCH_INVALID'
                self.assertEqual(result[0], 69 if number == errno.EIO else 1, str(result))
                self.assertTrue(result[2].startswith(code + ':'), str(result))
                self.assertEqual((case.run_dir / 'worker.claim').exists(), number != errno.EIO)
                self.assertTrue((case.run_dir / 'stderr.log').is_file())
                self.assertIn(code, (case.run_dir / 'stderr.log').read_text(encoding='utf-8'))
                self.assertEqual(target.read_bytes(), earlier)
                self.assertFalse(case.record.exists())

    def test_worker_result_identity_and_link_refusals_keep_the_record_and_retire_the_queue(self):
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('worker')
        target = case.run_dir / 'result.json'
        for field in ('run_id', 'task', 'linked-file'):
            with self.subTest(field=field):
                case.restore()
                if field == 'linked-file':
                    source = case.work / 'saved-result.json'
                    source.write_bytes(target.read_bytes())
                    target.unlink()
                    target.symlink_to(source)
                else:
                    data = json.loads(target.read_bytes())
                    data[field] = 'different-recorded-value'
                    target.write_text(json.dumps(data), encoding='utf-8')
                earlier = target.read_bytes()

                result = cli_result(case.root, case.environment, *case.command)

                self.assertEqual(result[0], 1, str(result))
                self.assertTrue(result[2].startswith('OAH_RUN_LAUNCH_INVALID:'), str(result))
                self.assertTrue((case.run_dir / 'worker.claim').is_file())
                self.assertEqual(target.read_bytes(), earlier)
                self.assertEqual(target.is_symlink(), field == 'linked-file')
                self.assertFalse(case.record.exists())

    def test_worker_result_directory_is_kept_and_its_queue_is_retired(self):
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('worker')
        target = case.run_dir / 'result.json'
        kept = {path: path.read_bytes() for path in case.run_dir.iterdir() if path.is_file() and path != target}
        target.unlink()
        target.mkdir()

        result = cli_result(case.root, case.environment, *case.command)

        self.assertEqual(result[0], 1, str(result))
        self.assertTrue(result[2].startswith('OAH_RUN_LAUNCH_INVALID:'), str(result))
        self.assertNotIn('Traceback', result[2])
        self.assertTrue(target.is_dir())
        self.assertTrue((case.run_dir / 'worker.claim').is_file())
        self.assertFalse(case.record.exists())
        self.assertTrue(all(path.read_bytes() == raw for path, raw in kept.items()))

    def test_fix_early_binding_faults_keep_the_command_category(self):
        from oah.review import fix
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('freeze')
        accesses = case.observe()
        target = next(access for access in accesses if pathlib.Path(access.path).name == 'intent-binding.json')
        earlier = pathlib.Path(target.path).read_bytes()
        assert case.project is not None
        for fault in ('missing', 'not-regular', 'denied', 'eio'):
            with self.subTest(fault=fault):
                case.restore()
                observer = ObservedFiles(case.storage, target=target, fault=fault)
                with mock.patch('oah.discovery.records.RETRY_SECONDS', 0, create=True):
                    result = case.invoke(observer)
                print('EARLY_FIX_BINDING', fault, result, 'reached', bool(observer.reached),
                      file=sys.__stdout__, flush=True)
                self.assertTrue(observer.reached)
                self.assertEqual(result[0], 69 if fault == 'eio' else 1, str(result))
                self.assertIn(('OAH_STATE_IO' if fault == 'eio' else 'OAH_FIX_STATE_CHANGED') + ':', result[2])
                self.assertEqual(case.project.fix_calls(), case.child_calls)
                self.assertEqual(pathlib.Path(target.path).read_bytes(), earlier)
        case.restore()
        with mock.patch.object(fix, '_head', side_effect=fix.ReviewError('OAH_FIX_GIT_FAILED',
                'fixture Git inspection failed')):
            result = cli_result(case.root, case.environment, *case.command)
        self.assertEqual(result[0], 69, str(result))
        self.assertIn('OAH_FIX_GIT_FAILED:', result[2])
        self.assertEqual(case.project.fix_calls(), case.child_calls)

    def test_background_launch_record_faults_preserve_a_recorded_refusal(self):
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('background')
        target = next(access for access in case.observe() if pathlib.Path(access.path).name == 'launch.json'
                      and access.operation == 'read' and any(context in access.chain for context in (
                          'call.py:_launch_main', 'call.py:launch_main')))
        for fault in ('missing', 'not-regular', 'denied', 'invalid-content', 'eio'):
            with self.subTest(fault=fault):
                case.restore()
                observer = ObservedFiles(case.storage, extra=case.extra, target=target, fault=fault)
                with mock.patch('oah.discovery.records.RETRY_SECONDS', 0.01, create=True):
                    try:
                        result = case.invoke(observer)
                    except KeyError as exc:
                        self.fail(f'the launch record read must return a typed refusal instead of raising KeyError: {exc}')
                self.assertTrue(observer.reached)
                expected = 'OAH_STATE_IO' if fault == 'eio' else 'OAH_RUN_LAUNCH_INVALID'
                self.assertEqual(result[0], 69 if fault == 'eio' else 1, str(result))
                self.assertIn(expected + ':', result[2])
                record, = case.storage.glob('runs/t1/r*/result.json')
                saved = json.loads(record.read_bytes())
                self.assertEqual(saved['status'], 'refused')
                self.assertTrue(saved['error'].startswith(expected + ':'), saved)
                assert observer.failed is not None
                path = pathlib.Path(observer.failed)
                self.assertEqual(path.read_bytes(), observer.retained[path])
                self.assertFalse(case.sent)

    def test_background_launch_record_metadata_is_classified_inside_the_reader(self):
        from tests.e_c12_observer import ObservedFiles
        from tests.e_c12_scenarios import Scenario

        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('background')
        target = next(access for access in case.observe() if pathlib.Path(access.path).name == 'launch.json' and
            access.operation == 'stat' and 'call.py:_launch_main' in access.chain)
        for fault in ('denied', 'eio'):
            with self.subTest(fault=fault):
                case.restore()
                observer = ObservedFiles(case.storage, extra=case.extra, target=target, fault=fault)
                with mock.patch('oah.discovery.records.RETRY_SECONDS', 0.01, create=True):
                    result = case.invoke(observer)
                self.assertTrue(observer.reached)
                self.assertEqual(result[0], 69 if fault == 'eio' else 1, str(result))
                expected = 'OAH_STATE_IO' if fault == 'eio' else 'OAH_RUN_LAUNCH_INVALID'
                self.assertIn(expected + ':', result[2])
                self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))
                saved, = case.storage.glob('runs/t1/r*/result.json')
                self.assertEqual(json.loads(saved.read_bytes())['status'], 'refused')

