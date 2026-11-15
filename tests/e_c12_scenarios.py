from __future__ import annotations

import contextlib
import contextvars
import argparse
import hashlib
import io
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import unittest
import types
import traceback
from unittest import mock

from tests import e_c6_fixture, e_c9_fixture
from tests import test_e_relay_times as relay_fixture
from tests import test_e_prepared as prepared_fixture
from tests import test_e_spent_evidence as spent
from tests import test_capabilities as capabilities
from tests import test_fix
from tests.e_c12_observer import ObservedFiles
from tests.test_e_c10_boundaries import cli_result
from tests.helpers import make_tmp_dir
from tests.test_skill_delivery import PLAIN


def recorded_path(path):
    return pathlib.PurePosixPath(os.fspath(path).replace('\\', '/'))


class Scenario(e_c9_fixture.CorrectionFixture):
    request = relay_fixture.RelayTimesTest.request
    offered_parent_relay = spent.SpentEvidenceTest.offered_parent_relay
    orca_queue = prepared_fixture.OrcaIdentityTest.orca_queue
    terminals_opened = prepared_fixture.OrcaIdentityTest.terminals_opened
    orca_state = prepared_fixture.OrcaIdentityTest.orca_state

    def __init__(self, methodName='scenario'):
        super().__init__(methodName)
        self.name = ''
        self.extra = []
        self.root = pathlib.Path()
        self.storage = pathlib.Path()
        self.command = []
        self.run_dir = pathlib.Path()
        self.project = None
        self.environment = {}
        self.backup = pathlib.Path()
        self.saved_roots = []
        self.before = {}
        self.child_calls = []
        self.sent = False
        self.engine_spawns = 0
        self.runner_creations = 0

    def valid(self):
        from oah.capabilities import plan

        request, parent, now = self.request()
        record, _ = plan.ingest(self.state_dir, request, capability=request['capability_id'],
            payload=capabilities.RESULT.encode('utf-8'), invocation={'outcome': 'ok', 'at': request['created_at'],
            'arguments_digest': 'sha256:' + '3' * 64}, now=now)
        return request, parent, record

    def scenario(self, name):
        from oah import call

        self.name = name
        self.extra = []
        if name in ('settle', 'settle-retry', 'skills', 'skills-receiving', 'skills-repeat'):
            _now, run, command = self.offered_parent_relay()
            if name in ('settle-retry', 'skills-receiving', 'skills-repeat'):
                settled = cli_result(self.repo, self.env, *command)
                self.assertEqual(settled[0], 0, str(settled))
            self.command = command if name in ('settle', 'settle-retry') else ['skills', 'read', '--run', run.name, '--item', '1']
            if name == 'skills-repeat':
                read = cli_result(self.repo, self.env, *self.command)
                self.assertEqual(read[0], 0, str(read))
            self.run_dir = run
            if name in ('settle', 'settle-retry'):
                self.extra = [pathlib.Path(command[command.index('--result-file') + 1])]
        elif name == 'freeze':
            project, original, _now, _parent, _relays = self.selected_parent(relay=True)
            project.queue_fix({'write': {'src/app.py': "print('corrected')\n"}})
            self.project = project
            self.root, self.storage, self.environment = project.repo, project.state, project.env
            self.saved_roots = [project.work, project.repo]
            self.child_calls = project.fix_calls()
            self.command = ['fix', '--task', 't1', '--tier', 'high', '--json']
            from oah.review import fix

            real = fix.correct
            self.enterContext(mock.patch('oah.review.fix.correct',
                lambda request, **options: real(request, **options, rules=test_fix.RULES, temp_root=project.temp_root)))
        elif name in ('admission-needs', 'skills-select', 'skills-find'):
            def configure(project, _reviewed):
                path = project.repo / '.oah/config.toml'
                command = test_fix.toml_list([sys.executable, str(test_fix.FAKES / 'fake_claude.py')])
                project.commit({'.oah/config.toml': path.read_text(encoding='utf-8') +
                    '\n[engines.claude]\ncommand = ' + command + '\n'})

            project, _original, _need, _basis = e_c6_fixture.selected_ordinary(self, configure=configure)
            self.project = project
            self.root, self.storage, self.environment = project.repo, project.state, project.env
            self.saved_roots = [project.work, project.repo]
            self.extra = [project.work / 'needs.json']
            self.child_calls = project.fix_calls()
            self.command = ['call', 'codex', '--stage', 'initial', '--task', 't1', '--tier', 'high',
                            '--override-stage', '--no-rerank', '--skill-needs', str(self.extra[0]), '--json']
            if name == 'skills-select':
                review = json.loads((_original.run.path / 'skills.json').read_bytes())['bundle']['items'][0]['review']
                self.command = ['skills', 'select', '--needs', str(self.extra[0]), '--review', review, '--json']
            elif name == 'skills-find':
                self.command = ['skills', 'find', '--task', 't1', '--needs', str(self.extra[0]),
                                '--engine', 'claude', '--json']
        elif name in ('skills-plan', 'skills-read-review', 'skills-candidates', 'skills-review'):
            self.build_skills(PLAIN)
            review = self.reviews['plain']['id']
            self.command = {
                'skills-plan': ['skills', 'plan', '--task', 't1', '--stage', 'initial', '--json'],
                'skills-read-review': ['skills', 'read', '--review', review, '--json'],
                'skills-candidates': ['skills', 'candidates', '--design', 'acme/booking', '--json'],
                'skills-review': ['skills', 'review', '--update', review, '--json'],
            }[name]
        elif name == 'relay':
            request, _parent, _now = self.request()
            source = capabilities.write(self.work / 'request.json', request)
            payload = capabilities.write(self.work / 'result.txt', capabilities.RESULT)
            invocation = capabilities.write(self.work / 'invocation.json', {
                'outcome': 'ok', 'at': request['created_at'], 'arguments_digest': 'sha256:' + '3' * 64})
            self.command = ['capabilities', 'relay', '--request', str(source), '--result-file', str(payload),
                            '--invocation', str(invocation), '--capability', request['capability_id'], '--json']
            self.extra = [source, payload, invocation]
            result = cli_result(self.repo, self.env, *self.command)
            self.assertEqual(result[0], 0, str(result))
        elif name in ('worker', 'wait'):
            self.build_skills(PLAIN)
            prepared = self.prepare()
            self.run_dir = prepared.run.path
            if name == 'worker':
                self.orca_queue(prepared)
                self.command = ['worker-run', prepared.run_id, '--stage', 'initial']
            else:
                with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
                    call.execute(prepared, base_env=self.env)
                self.command = ['wait', prepared.run_id, '--timeout-seconds', '30', '--json']
        elif name == 'design':
            self.build_skills(PLAIN)
            self.command = ['design', 'coverage', 'acme', 'booking', '--json']
        else:
            _request, parent, relay = self.valid()
            if name == 'admission-orca':
                local_state = self.repo / '.git' / 'oah'
                shutil.copytree(self.state_dir, local_state, dirs_exist_ok=True)
                self.state_dir = local_state
                self.env.pop('OAH_STATE_DIR', None)
            self.extra = [parent]
            self.command = ['call', 'claude', '--stage', 'initial', '--task', 't1', '--tier', 'low',
                            '--parent-inventory', str(parent), '--relay-result', relay['id'], '--json',
                            '--launcher', 'native' if name == 'admission-native' else
                            'orca' if name == 'admission-orca' else 'process']
            if name == 'capabilities-plan':
                self.command = ['capabilities', 'plan', '--task', 't1', '--stage', 'initial', '--engine', 'claude',
                                '--parent-inventory', str(parent), '--relay-result', relay['id'], '--json']
            if name == 'background':
                self.command += ['--background']
            if name == 'admission-orca':
                from oah import call_cli
                from tests.test_launcher_orca import FAKE_ORCA, _stub_oah

                self.orca_state.mkdir(exist_ok=True)
                (self.orca_state / 'controls.json').write_text(json.dumps({'FAKE_ORCA_HOLD': '1'}), encoding='utf-8')
                binary = self.work / 'bin'
                _stub_oah(binary)
                self.env['PATH'] = str(binary) + os.pathsep + self.env['PATH']
                self.env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
                section = call_cli.load_call_section

                def settings(root):
                    found = section(root)
                    found['orca_command'] = [sys.executable, '-B', FAKE_ORCA[1], '--state', str(self.orca_state)]
                    return found

                self.enterContext(mock.patch.object(call_cli, 'load_call_section', settings))
                self.command += ['--background']
            if name == 'admission-saved':
                from oah import call_cli

                parser = argparse.ArgumentParser()
                call_cli.register(parser.add_subparsers(dest='command'))
                saved = call_cli._snapshot(parser.parse_args(self.command))
                request_id = 'q' + 'a' * 24
                path = self.state_dir / 'requests' / (request_id + '.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(saved), encoding='utf-8')
                self.command = ['call', 'claude', '--stage', 'initial', '--request-id', request_id, '--json']
        if name not in ('freeze', 'admission-needs', 'skills-select', 'skills-find'):
            self.root, self.storage, self.environment = self.repo, self.state_dir, self.env
            self.saved_roots = [self.work, self.repo]
        if name in ('admission', 'admission-native', 'admission-orca', 'admission-saved', 'launch', 'background'):
            from oah import call_cli
            from oah.secrets import NoKeychain

            with mock.patch.dict(os.environ, self.environment, clear=True), contextlib.chdir(self.root):
                call.check_catalog('claude', call_cli.load_settings(self.root), state_dir=self.storage,
                                   env=self.environment, keychain=NoKeychain())
        roots = set((*self.saved_roots, self.storage))
        self.saved_roots = sorted(path for path in roots if not any(
            path != other and path.is_relative_to(other) for other in roots))
        self.remember()

    def remember(self):
        self.backup = make_tmp_dir(self) / 'snapshot'
        for index, path in enumerate(self.saved_roots):
            shutil.copytree(path, self.backup / str(index))
        self.before = {path: path.read_bytes() for path in self.storage.rglob('*') if path.is_file()}

    def restore(self):
        for index, path in enumerate(self.saved_roots):
            source = self.backup / str(index)
            saved = {entry.relative_to(source): entry for entry in source.rglob('*')}
            for entry in sorted(path.rglob('*'), key=lambda value: len(value.parts), reverse=True):
                if entry.relative_to(path) not in saved:
                    if entry.is_dir() and not entry.is_symlink():
                        entry.rmdir()
                    else:
                        entry.unlink()
            for relative, entry in saved.items():
                target = path / relative
                if entry.is_dir():
                    target.mkdir(exist_ok=True)
                elif not target.is_file() or target.read_bytes() != entry.read_bytes():
                    shutil.copy2(entry, target)

    def invoke(self, observer):
        from oah import call

        real_popen, real_run = subprocess.Popen, subprocess.run
        self.sent = False
        self.engine_spawns = self.runner_creations = 0
        nonce = 0

        def token_hex(size=32):
            nonlocal nonce
            nonce += 1
            raw = hashlib.sha256(f'{self.name}:{nonce}'.encode('utf-8')).hexdigest()
            return (raw * ((size * 2 + len(raw) - 1) // len(raw)))[:size * 2]

        observer.active = True
        observer.phase = lambda: ('after-engine' if self.engine_spawns else
            'after-runner' if self.runner_creations else 'before', self.engine_spawns, self.runner_creations)

        def popen(argv, *args, **options):
            if self.name == 'background' and len(argv) >= 2 and argv[-2] == 'background_main':
                self.runner_creations += 1
                self.sent = True
                return types.SimpleNamespace(pid=123456, returncode=None)
            engine = options.get('stdin') == subprocess.PIPE and any(
                    'call.py' in frame.filename and frame.name == '_run_prepared'
                    for frame in traceback.extract_stack())
            child = real_popen(argv, *args, **options)
            if engine:
                self.engine_spawns += 1
                self.sent = True
            return child

        def launch(argv, *args, **options):
            if len(argv) >= 2 and argv[-2] == 'launch_main':
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = contextvars.Context().run(call.launch_main, argv[-1])
                return subprocess.CompletedProcess(argv, code, out.getvalue().encode(), err.getvalue().encode())
            return real_run(argv, *args, **options)

        from oah.launchers import orca

        real_orca_json = orca.Orca.json

        def orca_json(client, *args, **options):
            result = real_orca_json(client, *args, **options)
            if args[:2] == ('terminal', 'create'):
                self.runner_creations += 1
                self.sent = True
            return result

        with observer, mock.patch.object(secrets, 'token_hex', token_hex), \
                mock.patch.object(orca.Orca, 'json', orca_json), \
                mock.patch.object(subprocess, 'Popen', popen), \
                mock.patch.object(subprocess, 'run', launch):
            return cli_result(self.root, self.environment, *self.command)

    def observe(self, check_success=None):
        observer = ObservedFiles(self.storage, extra=self.extra)
        result = self.invoke(observer)
        self.assertEqual(result[0], 0, str(result))
        self.assertTrue(observer.accesses)
        if check_success is not None:
            check_success(observer)
        self.restore()
        return observer.accesses
