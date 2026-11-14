from __future__ import annotations

import dataclasses
import contextlib
import datetime as dt
import fnmatch
import json
import pathlib
import re
import unittest
from unittest import mock

from tests.e_c12_matrix import FAULTS, INPUT_MATRIX, OWNER_MATRIX, OWNER_RUN_MATRIX, OWNER_KEPT_MATRIX
from tests.e_c12_observer import ObservedFiles
from tests.e_c12_scenarios import Scenario


@dataclasses.dataclass(frozen=True)
class FileRule:
    pattern: str
    required: bool = True
    absent: tuple[str, int] | None = None
    absent_by_context: tuple[tuple[str, tuple[str, int]], ...] = ()
    input_id: str | None = None
    input_when: tuple[str, ...] = ()
    recorded_when: tuple[str, ...] = ()

    def is_input(self, access):
        return self.input_id is not None and not any(context in access.chain for context in self.recorded_when) \
            and (not self.input_when or any(context in access.chain for context in self.input_when))


FILE_MATRIX: dict[str, tuple[FileRule, ...]] = {
    'admission': (
        FileRule('parent.json', input_id='parent-inventory', input_when=('plan.py:for_task', 'inputs.py:location'),
                 recorded_when=('call.py:plan_capabilities',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{new-run}/result.json'), FileRule('runs/*/{new-run}/skills.json', False, ('', 0))),
    'admission-native': (
        FileRule('parent.json', input_id='parent-inventory', input_when=('plan.py:for_task', 'inputs.py:location'),
                 recorded_when=('call.py:plan_capabilities',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{new-run}/result.json')),
    'admission-orca': (
        FileRule('parent.json', input_id='parent-inventory',
                 input_when=('plan.py:for_task', 'inputs.py:location'), recorded_when=('call.py:plan_capabilities',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{new-run}/result.json')),
    'admission-needs': (
        FileRule('needs.json', input_id='skill-needs', recorded_when=('call.py:capture',)),
        FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)),
        FileRule('skills/task-selections/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('runs/*/{new-run}/result.json'), FileRule('runs/*/{new-run}/skills.json', False, ('', 0))),
    'admission-saved': (
        FileRule('requests/*.json', False, ('OAH_REQUEST_INVALID', 1)),
        FileRule('parent.json', input_id='parent-inventory',
                 input_when=('plan.py:for_task', 'inputs.py:location'), recorded_when=('call.py:plan_capabilities',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{new-run}/result.json'), FileRule('runs/*/{new-run}/skills.json', False, ('', 0))),
    'launch': (
        FileRule('parent.json'), FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{new-run}/result.json'),
        FileRule('runs/*/{new-run}/skills.json'), FileRule('runs/*/{new-run}/prompt.md'),
        FileRule('runs/*/{new-run}/capabilities.json'), FileRule('runs/*/{new-run}/design.json'),
        FileRule('runs/*/{new-run}/lease.json'), FileRule('runs/*/intent-binding.json', False, ('', 0))),
    'background': (
        FileRule('parent.json'), FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{new-run}/result.json'),
        FileRule('runs/*/{new-run}/skills.json'), FileRule('runs/*/{new-run}/prompt.md'),
        FileRule('runs/*/{new-run}/capabilities.json'), FileRule('runs/*/{new-run}/design.json'),
        FileRule('runs/*/{new-run}/lease.json'), FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('runs/*/{new-run}/launch.json')),
    'settle': (
        FileRule('reply.txt', input_id='native-result'),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1),
            (('call.py:_check_e', ('OAH_NATIVE_OFFER_STALE', 1)),)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{original-run}/result.json'),
        FileRule('runs/*/{original-run}/skills.json'), FileRule('runs/*/{original-run}/prompt.md'),
        FileRule('runs/*/{original-run}/capabilities.json'), FileRule('runs/*/{original-run}/native-offer.json')),
    'settle-retry': (
        FileRule('reply.txt', input_id='native-result'),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{original-run}/result.json'), FileRule('runs/*/{original-run}/skills.json'),
        FileRule('runs/*/{original-run}/prompt.md'), FileRule('runs/*/{original-run}/capabilities.json'),
        FileRule('runs/*/{original-run}/native-offer.json'),
        FileRule('runs/*/{original-run}/native-settlement.json'),
        FileRule('runs/*/{original-run}/skills-receiving.json')),
    'skills': (FileRule('runs/*/{original-run}/skills.json', False, ('OAH_SKILL_UNMANAGED_RUN', 1)),
               FileRule('runs/*/{original-run}/prompt.md'),
               FileRule('runs/*/{original-run}/native-offer.json', False, ('', 0))),
    'skills-receiving': (
        FileRule('runs/*/{original-run}/skills.json', False, ('OAH_SKILL_UNMANAGED_RUN', 1)),
        FileRule('runs/*/{original-run}/prompt.md'),
        FileRule('runs/*/{original-run}/native-offer.json', False, ('', 0)),
        FileRule('runs/*/{original-run}/native-settlement.json'),
        FileRule('runs/*/{original-run}/skills-receiving.json')),
    'skills-plan': (
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1))),
    'skills-read-review': (
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILLS_USAGE', 1)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_STATE_CORRUPT', 69))),
    'skills-candidates': (FileRule('skills/reviews/*/*.json', False, ('', 0)),),
    'skills-review': (
        FileRule('skills/identities.json', False, ('', 0)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILLS_USAGE', 1),
                 (('store.py:reviews_of', ('', 0)),)),
        FileRule('skills/reviews/index.json', False, ('', 0)),
        FileRule('skills/sources/registry.json'), FileRule('skills/sources/heads/*.json'),
        FileRule('skills/sources/snapshots/*/*.json'), FileRule('skills/sources/blobs/*/*')),
    'skills-select': (
        FileRule('needs.json', input_id='skill-needs'),
        FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_NEEDS_INVALID(malformed)', 1)),
        FileRule('skills/task-selections/index.json', False, ('', 0)),
        FileRule('skills/task-selections/*.json', False, ('', 0))),
    'skills-find': (
        FileRule('needs.json', input_id='skill-needs', recorded_when=('call.py:capture',)),
        FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('skills/reviews/*/*.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0)),
        FileRule('skills/task-selections/*.json', False, ('', 0))),
    'capabilities-plan': (
        FileRule('parent.json', input_id='parent-inventory', input_when=('plan.py:for_task',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(stale)', 1)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1)),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1))),
    'relay': (
        FileRule('request.json', input_id='relay-request'),
        FileRule('invocation.json', input_id='relay-invocation'),
        FileRule('result.txt', input_id='relay-result'),
        FileRule('skills/relays/*.json', False, ('', 0)),
        FileRule('skills/relay-payloads/*.json', False, ('', 0))),
    'worker': (
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
        FileRule('runs/*/{original-run}/result.json'),
        FileRule('runs/*/{original-run}/skills.json'), FileRule('runs/*/{original-run}/prompt.md'),
        FileRule('runs/*/{original-run}/design.json'), FileRule('runs/*/{original-run}/lease.json'),
        FileRule('runs/*/intent-binding.json', False, ('', 0)), FileRule('runs/*/{original-run}/launch.json')),
    'freeze': (
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)), FileRule('skills/relays/*.json'),
        FileRule('skills/sources/blobs/*/*', False, ('OAH_SKILL_NOT_DELIVERABLE(pin-mismatch)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('call.py:_check_e', ('OAH_FIX_STATE_CHANGED', 1)))),
        FileRule('skills/relay-payloads/*.json'), FileRule('skills/originals/tasks/*.json'),
        FileRule('skills/originals/*.json'),
        FileRule('skills/task-selections/index.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)),
        FileRule('skills/task-selections/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)),
        FileRule('runs/*/intent-binding.json'),
        FileRule('runs/*/{original-run}/result.json'), FileRule('runs/*/{original-run}/skills.json'),
        FileRule('runs/*/{original-run}/prompt.md'), FileRule('runs/*/{original-run}/capabilities.json'),
        FileRule('runs/*/{new-run}/result.json')),
    'design': (FileRule('skills/reviews/*/*.json', False, ('', 0)),),
    'wait': (FileRule('runs/*/{original-run}/result.json'),),
}

ADDITIONAL_FILES: dict[str, tuple[FileRule, ...]] = {
    'admission': (
        FileRule('runs/*/.{new-run}.tmp/result.json'), FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('runs/*/{new-run}/capabilities.json'), FileRule('runs/*/{new-run}/design.json'),
        FileRule('runs/*/{new-run}/lease.json'), FileRule('runs/*/{new-run}/prompt.md'),
        FileRule('skills/originals/*.json', False, ('', 0)),
        FileRule('skills/task-discovery/index.json', False, ('', 0)),
        FileRule('skills/task-discovery/*.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0))),
    'admission-native': (
        FileRule('runs/*/.{new-run}.tmp/result.json'), FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('runs/*/{new-run}/capabilities.json'), FileRule('runs/*/{new-run}/design.json'),
        FileRule('runs/*/{new-run}/prompt.md'), FileRule('runs/*/{new-run}/skills.json'),
        FileRule('skills/originals/*.json', False, ('', 0)),
        FileRule('skills/task-discovery/index.json', False, ('', 0)),
        FileRule('skills/task-discovery/*.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0))),
    'admission-orca': (
        FileRule('runs/*/.{new-run}.tmp/result.json'), FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('runs/*/{new-run}/capabilities.json'), FileRule('runs/*/{new-run}/design.json'),
        FileRule('runs/*/{new-run}/lease.json'), FileRule('runs/*/{new-run}/prompt.md'),
        FileRule('runs/*/{new-run}/skills.json'), FileRule('skills/originals/*.json', False, ('', 0)),
        FileRule('skills/task-discovery/index.json', False, ('', 0)),
        FileRule('skills/task-discovery/*.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0))),
    'admission-needs': (
        FileRule('runs/*/.{new-run}.tmp/result.json'), FileRule('runs/*/{new-run}/last-message.md'),
        FileRule('runs/*/{new-run}/lease.json'), FileRule('runs/*/{new-run}/prompt.md'),
        FileRule('skills/originals/*.json', False, ('', 0))),
    'admission-saved': (
        FileRule('runs/*/.{new-run}.tmp/result.json'), FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('runs/*/{new-run}/capabilities.json'), FileRule('runs/*/{new-run}/design.json'),
        FileRule('runs/*/{new-run}/lease.json'), FileRule('runs/*/{new-run}/prompt.md'),
        FileRule('skills/originals/*.json', False, ('', 0)),
        FileRule('skills/task-discovery/index.json', False, ('', 0)),
        FileRule('skills/task-discovery/*.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0))),
    'launch': (
        FileRule('runs/*/{new-run}/last-message.md'),
        FileRule('runs/*/{new-run}/native-offer.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0))),
    'background': (FileRule('runs/*/{new-run}/native-offer.json', False, ('', 0)),
                   FileRule('skills/task-selections/index.json', False, ('', 0))),
    'settle': (FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('runs/*/{original-run}/native-settlement.json', False, ('', 0)),
        FileRule('runs/*/{original-run}/skills-reads.jsonl', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0))),
    'settle-retry': (FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('runs/*/{original-run}/skills-reads.jsonl', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0))),
    'skills': (FileRule('runs/*/{original-run}/native-settlement.json', False, ('', 0)),
        FileRule('runs/*/{original-run}/skills-receiving.json', False, ('', 0)),
        FileRule('runs/*/{original-run}/skills-reads.jsonl', False, ('', 0))),
    'skills-receiving': (FileRule('runs/*/{original-run}/skills-reads.jsonl', False, ('', 0)),),
    'skills-plan': (FileRule('runs/*/intent-binding.json', False, ('', 0)),
                   FileRule('skills/task-selections/index.json', False, ('', 0))),
    'skills-read-review': (FileRule('skills/reads/*.json', False, ('', 0)),),
    'skills-candidates': (FileRule('skills/queries/*/*.json', False, ('', 0)),),
    'skills-review': (FileRule('skills/sources/observations/*/*.json'),),
    'skills-find': (FileRule('skills/task-discovery/index.json', False, ('', 0)),
                   FileRule('skills/task-discovery/*.json', False, ('', 0))),
    'capabilities-plan': (FileRule('runs/*/intent-binding.json', False, ('', 0)),
                         FileRule('skills/task-selections/index.json', False, ('', 0))),
    'worker': (FileRule('runs/*/{original-run}/capabilities.json', False, ('', 0)),
        FileRule('runs/*/{original-run}/last-message.md'),
        FileRule('runs/*/{original-run}/native-offer.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('', 0))),
    'freeze': (FileRule('runs/*/.{new-run}.tmp/result.json'), FileRule('runs/*/{new-run}/design.json'),
        FileRule('runs/*/{new-run}/last-message.md'), FileRule('runs/*/{new-run}/lease.json'),
        FileRule('runs/*/{new-run}/prompt.md'), FileRule('runs/*/{new-run}/skills.json')),
}
for command, rows in ADDITIONAL_FILES.items():
    FILE_MATRIX[command] += rows
FILE_MATRIX['skills-repeat'] = (
    FileRule('runs/*/{original-run}/skills.json', False, ('OAH_SKILL_UNMANAGED_RUN', 1)),
    FileRule('runs/*/{original-run}/prompt.md'),
    FileRule('runs/*/{original-run}/native-offer.json', False, ('', 0)),
    FileRule('runs/*/{original-run}/native-settlement.json'),
    FileRule('runs/*/{original-run}/skills-receiving.json'),
    FileRule('runs/*/{original-run}/skills-reads.jsonl', False, ('', 0)))

BOUNDARY = {'admission': 'admission', 'admission-native': 'admission', 'admission-orca': 'admission',
            'admission-needs': 'admission', 'admission-saved': 'admission', 'launch': 'launch',
            'background': 'launch', 'settle': 'settle', 'skills': 'skills', 'relay': 'relay',
            'worker': 'worker', 'freeze': 'freeze', 'design': 'design', 'wait': 'wait'}
BOUNDARY.update({'skills-plan': 'skills', 'skills-read-review': 'skills', 'skills-candidates': 'skills',
                 'skills-review': 'skills', 'skills-select': 'skills', 'skills-find': 'skills',
                 'capabilities-plan': 'relay', 'settle-retry': 'settle', 'skills-receiving': 'skills',
                 'skills-repeat': 'skills'})
EXCLUDED = {'freeze': ('fix.py:_publication:', 'review/fix publication',
                      'comparison of already-captured bytes owned by review/fix publication')}
EXCLUDED_PATHS = (('skills/sources/*', 'lane C design sources',
                  'not an E stored record; C keeps its own failure publication'),)


def excluded_access(name, access, state):
    path = label(access, state)
    for pattern, owner, reason in EXCLUDED_PATHS:
        if fnmatch.fnmatchcase(path, pattern):
            return owner, reason
    if pathlib.Path(access.path).name == 'skills-reads.jsonl' and 'atomic.py:append_jsonl' in access.chain:
        return 'B atomic append validation', 'the shared append helper keeps its existing read contract'
    if not access.owned and access.operation in ('stat', 'lstat') and 'runs.py:result' in access.chain:
        return 'B run result update', 'pre-update metadata check; EACCES classification remains a B follow-up'
    if not access.owned and access.operation in ('stat', 'lstat') and (
            any(context in access.chain for context in ('runs.py:_create_run', 'runs.py:write_prompt',
                'runs.py:_write_text', 'runs.py:_write_result', 'runs.py:launch_path'))
            or pathlib.Path(access.path).name == 'skills.json' and any(context in access.chain for context in (
                'original.py:publish', 'call.py:publish_run'))
            or not access.present and pathlib.Path(access.path).name in ('capabilities.json', 'design.json', 'lease.json')
                and any(context in access.chain for context in ('call.py:_finish_prepare',
                    'call.py:_prepare_native', 'call.py:publish_run'))
            or not access.present and pathlib.Path(access.path).name == 'lease.json' and
                'call.py:record_dispatch_binding' in access.chain
            or pathlib.Path(access.path).name == 'last-message.md'):
        return 'B run publication/output validation', 'write-only path check, not a read of recorded bytes'
    if name in EXCLUDED and access.site.startswith(EXCLUDED[name][0]):
        return EXCLUDED[name][1:]
    return None


def label(access, state):
    path = pathlib.Path(access.path)
    relative = path.relative_to(state).as_posix() if path.is_relative_to(state) else path.name
    return re.sub(r'r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}',
                  '{original-run}' if access.existing else '{new-run}', relative)


class ObservedAccessTest(unittest.TestCase):
    def test_observed_accesses_have_declared_owners(self):
        self.assertEqual(set(BOUNDARY), set(FILE_MATRIX))
        self.assertEqual(set(OWNER_MATRIX), set(BOUNDARY.values()))
        for name in BOUNDARY:
            with self.subTest(command=name):
                case = Scenario('scenario')
                case.setUp()
                try:
                    case.scenario(name)
                    accesses = case.observe()
                    paths, contexts = set(), set()
                    for access in accesses:
                        path = label(access, case.storage)
                        matched = [index for index, rule in enumerate(FILE_MATRIX[name])
                                   if fnmatch.fnmatchcase(path, rule.pattern)]
                        self.assertTrue(matched, f'{name}: access without an owner-decision row: {path} {access}')
                        index = min(matched)
                        paths.add(index)
                        rule = FILE_MATRIX[name][index]
                        owned = access.input_owned if rule.is_input(access) else access.owned
                        self.assertTrue(owned or excluded_access(name, access, case.storage),
                                        f'{name}: stored read outside owner: {access}')
                        for context, _outcome in FILE_MATRIX[name][index].absent_by_context:
                            if context in access.chain:
                                contexts.add((index, context))
                    self.assertEqual(paths, set(range(len(FILE_MATRIX[name]))))
                    self.assertEqual(contexts, {(index, context) for index, rule in enumerate(FILE_MATRIX[name])
                                               for context, _outcome in rule.absent_by_context})
                finally:
                    case.doCleanups()

    def check_run_state(self, case, observer, name, fault):
        status = OWNER_RUN_MATRIX[BOUNDARY[name]][FAULTS.index(fault)]
        paths = list(case.storage.glob('runs/*/r*/result.json'))
        retained = {path: raw for path, raw in observer.retained.items() if path.name == 'result.json'}
        if status == 'kept':
            self.assertEqual(set(paths), set(retained))
            for path, raw in retained.items():
                if str(path) != observer.failed:
                    self.assertEqual(path.read_bytes(), raw)
        elif status == 'preparation':
            self.assertEqual(set(paths), set(retained), 'a refused access cannot create a later run')
            for path, raw in retained.items():
                if str(path) != observer.failed:
                    if path in case.before:
                        self.assertEqual(path.read_bytes(), raw, 'keep the earlier completed run')
                    else:
                        self.assertIn(json.loads(path.read_bytes())['status'], (json.loads(raw)['status'], 'refused'))
        elif status == 'refused-or-unreadable-result-kept':
            self.assertTrue(paths)
            for path in paths:
                if str(path) != observer.failed:
                    saved = json.loads(path.read_bytes())
                    self.assertEqual(saved['status'], 'refused')
        elif status == 'original-kept':
            for path, raw in case.before.items():
                if path.name == 'result.json' and str(path) != observer.failed:
                    self.assertEqual(path.read_bytes(), raw)
            for path in paths:
                if path not in case.before:
                    self.assertIn(json.loads(path.read_bytes())['status'], ('created', 'prepared', 'refused', 'failed'))
        else:
            self.assertIn(status, ('refused-or-stale-result-claimed', 'refused-or-unreadable-result-unclaimed'))

    def exercise(self, name):
        now = dt.datetime.now(dt.timezone.utc)

        class FixtureClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(now.timestamp(), tz)

        self.enterContext(mock.patch.object(dt, 'datetime', FixtureClock))
        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario(name)
        accesses = case.observe()
        self.assertTrue(accesses)
        reached, specified, contexts = set(), set(), set()
        for ordinal, access in enumerate(accesses):
            path = label(access, case.storage)
            matched = [index for index, rule in enumerate(FILE_MATRIX[name]) if fnmatch.fnmatchcase(path, rule.pattern)]
            self.assertTrue(matched, f'{name}: observed access has no literal owner path: {path} {access}')
            index = min(matched)
            specified.add(index)
            rule = FILE_MATRIX[name][index]
            for context, _outcome in rule.absent_by_context:
                if context in access.chain:
                    contexts.add((index, context))
            excluded = excluded_access(name, access, case.storage)
            initial_input = rule.is_input(access)
            owned = access.input_owned if initial_input else access.owned
            self.assertTrue(owned or excluded, f'{name}: file read outside its owner: {access}')
            faults = ('missing', 'denied', 'eio') if excluded else FAULTS
            for fault in faults:
                with self.subTest(command=name, path=path, access=access.operation, site=access.site,
                                  occurrence=access.occurrence, fault=fault):
                    case.restore()
                    observer = ObservedFiles(case.storage, extra=case.extra, target=access, fault=fault)
                    with contextlib.ExitStack() as retry_window:
                        if fault == 'denied':
                            for module in ('oah.discovery.records', 'oah.discovery.inputs', 'oah.runs', 'oah.atomic'):
                                retry_window.enter_context(mock.patch(module + '.RETRY_SECONDS', 0.01))
                        result = case.invoke(observer)
                    self.assertTrue(observer.reached, 'the named OS access must be reached')
                    reached.add((ordinal, fault))
                    codes = [code for code in re.findall(r'OAH_[A-Z_]+(?:\([^\n:]+\))?:', result[1] + result[2])
                             if code not in ('OAH_STAGE_OVERRIDDEN:', 'OAH_MODEL_UNKNOWN_FAMILY:')]
                    code = codes[-1][:-1] if codes else ''
                    print('OS_CASE', json.dumps(dict(command=name, path=path, operation=access.operation,
                        site=access.site, occurrence=access.occurrence, fault=fault, code=code,
                        exit=result[0], excluded=excluded, input=rule.input_id if initial_input else None)), flush=True)
                    if excluded:
                        if excluded[0] == 'B run result update':
                            self.assertEqual((code, result[0]), ('OAH_RUN_LAUNCH_INVALID', 1) if fault == 'missing'
                                             else ('OAH_STATE_IO', 69), str(result))
                            self.assertFalse(case.sent)
                        if excluded[0] in ('B atomic append validation', 'B run publication/output validation'):
                            self.assertEqual(result[0], 0 if fault == 'missing' else 69, str(result))
                            if fault != 'missing':
                                self.assertEqual(code, 'OAH_STATE_IO', str(result))
                                self.assertFalse(case.sent)
                        continue
                    expected = rule.absent if fault == 'missing' and not rule.required else OWNER_MATRIX[
                        BOUNDARY[name]][FAULTS.index(fault)]
                    if fault == 'missing':
                        expected = next((outcome for context, outcome in rule.absent_by_context
                                         if context in access.chain), expected)
                    if initial_input:
                        assert rule.input_id is not None
                        expected = INPUT_MATRIX[rule.input_id][FAULTS.index(fault)]
                    self.assertEqual((code, result[0]), expected, str(result))
                    self.assertNotIn('Traceback', result[2])
                    if initial_input and fault != 'eio':
                        self.assertNotIn('the recorded ', result[2])
                    if result[0]:
                        self.check_run_state(case, observer, name, fault)
                        if name == 'freeze':
                            assert case.project is not None
                            self.assertEqual(case.project.fix_calls(), case.child_calls)
                        else:
                            self.assertFalse(case.sent, 'the command must send nothing')
                    for earlier, raw in observer.retained.items():
                        if earlier.is_relative_to(case.storage) and any(fnmatch.fnmatchcase(
                                earlier.relative_to(case.storage).as_posix(), pattern)
                                for pattern, _owner, _reason in EXCLUDED_PATHS):
                            continue
                        if earlier.name == 'result.json' and str(earlier) != observer.failed:
                            continue
                        injected_content = fault == 'invalid-content' and observer.failed == str(earlier) and \
                            access.operation in ('stat', 'lstat', 'fstat', 'os.open')
                        self.assertEqual(earlier.read_bytes(), b'\xff' if injected_content else raw,
                                         f'{name}: keep the earlier {earlier.name}')
                    self.assertEqual(OWNER_KEPT_MATRIX[BOUNDARY[name]][FAULTS.index(fault)], 'inputs-at-access')
                    if name == 'worker' and result[0] and pathlib.Path(access.path).name != 'result.json':
                        self.assertEqual(json.loads((case.run_dir / 'result.json').read_bytes())['status'], 'refused')
                    if name == 'worker' and pathlib.Path(access.path).name == 'result.json':
                        self.assertEqual((case.run_dir / 'worker.claim').exists(), fault != 'eio')
                        raw = (case.run_dir / 'result.json').read_bytes()
                        expected_raw = b'\xff' if fault == 'invalid-content' and access.operation in ('stat', 'lstat', 'fstat', 'os.open') \
                            else case.before[case.run_dir / 'result.json']
                        self.assertEqual(raw, expected_raw, 'the queued label is historical for this refused claim')
        self.assertEqual(specified, set(range(len(FILE_MATRIX[name]))), 'each literal file row needs an observed case')
        self.assertEqual(contexts, {(index, context) for index, rule in enumerate(FILE_MATRIX[name])
                                   for context, _outcome in rule.absent_by_context},
                         'each literal optional-read phase needs an observed case')
        expected_cells = {(ordinal, fault) for ordinal, access in enumerate(accesses)
                          for fault in (('missing', 'denied', 'eio') if
                              excluded_access(name, access, case.storage) else FAULTS)}
        self.assertEqual(reached, expected_cells, 'every observed access and every fault cell needs a reached case')

    def test_process_admission_observed_files(self):
        self.exercise('admission')

    def test_native_admission_observed_files(self):
        self.exercise('admission-native')

    def test_orca_admission_observed_files(self):
        self.exercise('admission-orca')

    def test_needs_admission_observed_files(self):
        self.exercise('admission-needs')

    def test_saved_request_admission_observed_files(self):
        self.exercise('admission-saved')

    def test_prepared_launch_observed_files(self):
        self.exercise('launch')

    def test_background_child_observed_files(self):
        self.exercise('background')

    def test_native_settle_observed_files(self):
        self.exercise('settle')

    def test_native_settle_retry_observed_files(self):
        self.exercise('settle-retry')

    def test_skills_read_observed_files(self):
        self.exercise('skills')

    def test_skills_receiving_observed_files(self):
        self.exercise('skills-receiving')

    def test_skills_repeat_read_observed_files(self):
        self.exercise('skills-repeat')

    def test_skills_plan_observed_files(self):
        self.exercise('skills-plan')

    def test_skills_review_read_observed_files(self):
        self.exercise('skills-read-review')

    def test_skills_candidates_observed_files(self):
        self.exercise('skills-candidates')

    def test_skills_review_update_observed_files(self):
        self.exercise('skills-review')

    def test_skills_select_observed_files(self):
        self.exercise('skills-select')

    def test_skills_find_observed_files(self):
        self.exercise('skills-find')

    def test_capability_plan_observed_files(self):
        self.exercise('capabilities-plan')

    def test_relay_observed_files(self):
        self.exercise('relay')

    def test_orca_worker_observed_files(self):
        self.exercise('worker')

    def test_freeze_and_correction_observed_files(self):
        self.exercise('freeze')

    def test_design_coverage_observed_files(self):
        self.exercise('design')

    def test_wait_observed_files(self):
        self.exercise('wait')
