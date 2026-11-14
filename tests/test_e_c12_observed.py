from __future__ import annotations

import dataclasses
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import pathlib
import re
import unittest
from unittest import mock

from tests.e_c12_matrix import (FAULTS, INPUT_MATRIX, OWNER_MATRIX, OWNER_RUN_MATRIX, OWNER_KEPT_MATRIX,
                                AFTER_MATRIX, SUCCESS_DISPATCH, SUCCESS_RUN_MATRIX, SUCCESS_KEPT_RUN_FILES,
                                OUTPUT_MATRIX)
from tests.e_c12_observer import ObservedFiles
from tests.e_c12_scenarios import Scenario


@dataclasses.dataclass(frozen=True)
class FileRule:
    pattern: str
    required: bool = True
    absent: tuple[str, int] | None = None
    absent_by_context: tuple[tuple[str | tuple[str, ...], tuple[str, int] | tuple[str, int, str]], ...] = ()
    input_id: str | None = None
    input_when: tuple[str, ...] = ()
    recorded_when: tuple[str, ...] = ()

    def is_input(self, access):
        return self.input_id is not None and not any(context in access.chain for context in self.recorded_when) \
            and (not self.input_when or any(context in access.chain for context in self.input_when))

    def absence(self, access):
        return next(((context, outcome) for context, outcome in self.absent_by_context
                     if (context in access.chain if isinstance(context, str) else
                         all(part in access.chain for part in context))), (None, self.absent))


FILE_MATRIX: dict[str, tuple[FileRule, ...]] = {
    'admission': (
        FileRule('parent.json', input_id='parent-inventory', input_when=('plan.py:for_task', 'inputs.py:location'),
                 recorded_when=('call.py:plan_capabilities',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('plan.py:needs_of', ('OAH_CAPABILITY_RELAY_REQUIRED(stale)', 1)),
             ('tasks.py:prepare_task', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)))),
        FileRule('skills/sources/blobs/*/*'),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('call.py:_launch_inputs', ('OAH_RUN_LAUNCH_INVALID', 1)))),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('call.py:_launch_inputs', ('OAH_RUN_LAUNCH_INVALID', 1)))),
        FileRule('runs/*/{new-run}/result.json'), FileRule('runs/*/{new-run}/skills.json', False, ('', 0))),
    'admission-native': (
        FileRule('parent.json', input_id='parent-inventory', input_when=('plan.py:for_task', 'inputs.py:location'),
                 recorded_when=('call.py:plan_capabilities',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('plan.py:needs_of', ('OAH_CAPABILITY_RELAY_REQUIRED(stale)', 1)),
             ('tasks.py:prepare_task', ('OAH_CAPABILITY_RELAY_REQUIRED(stale)', 1)))),
        FileRule('skills/sources/blobs/*/*'),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('runs/*/{new-run}/result.json')),
    'admission-orca': (
        FileRule('parent.json', input_id='parent-inventory',
                 input_when=('plan.py:for_task', 'inputs.py:location'), recorded_when=('call.py:plan_capabilities',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('plan.py:needs_of', ('OAH_CAPABILITY_RELAY_REQUIRED(stale)', 1)),
             ('tasks.py:prepare_task', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)))),
        FileRule('skills/sources/blobs/*/*'),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),)),
        FileRule('runs/*/{new-run}/result.json')),
    'admission-needs': (
        FileRule('needs.json', input_id='skill-needs', recorded_when=('call.py:capture',)),
        FileRule('runs/*/intent-binding.json', False, ('', 0)),
        FileRule('skills/task-selections/index.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             (('call.py:capture', 'call.py:_check_e'), ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('call.py:capture', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1, 'new-refused')))),
        FileRule('skills/task-selections/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             (('call.py:capture', 'call.py:_check_e'), ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('call.py:capture', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1, 'new-refused')))),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('plan.py:needs_of', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)))),
        FileRule('skills/sources/blobs/*/*'),
        FileRule('runs/*/{new-run}/result.json'), FileRule('runs/*/{new-run}/skills.json', False, ('', 0))),
    'admission-saved': (
        FileRule('requests/*.json', False, ('OAH_REQUEST_INVALID', 1)),
        FileRule('parent.json', input_id='parent-inventory',
                 input_when=('plan.py:for_task', 'inputs.py:location'), recorded_when=('call.py:plan_capabilities',)),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1),
            (('skillpack.py:recheck', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('plan.py:needs_of', ('OAH_CAPABILITY_RELAY_REQUIRED(stale)', 1)),
             ('tasks.py:prepare_task', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)))),
        FileRule('skills/sources/blobs/*/*'),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('call.py:_launch_inputs', ('OAH_RUN_LAUNCH_INVALID', 1)))),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1),
            (('skillpack.py:checked', ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
             ('call.py:_launch_inputs', ('OAH_RUN_LAUNCH_INVALID', 1)))),
        FileRule('runs/*/{new-run}/result.json'), FileRule('runs/*/{new-run}/skills.json', False, ('', 0))),
    'launch': (
        FileRule('parent.json'), FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
        FileRule('skills/sources/blobs/*/*'),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{new-run}/result.json'),
        FileRule('runs/*/{new-run}/skills.json'), FileRule('runs/*/{new-run}/prompt.md'),
        FileRule('runs/*/{new-run}/capabilities.json'), FileRule('runs/*/{new-run}/design.json'),
        FileRule('runs/*/{new-run}/lease.json'), FileRule('runs/*/intent-binding.json', False, ('', 0))),
    'background': (
        FileRule('parent.json'), FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_NOT_DELIVERABLE(changed)', 1)),
        FileRule('skills/sources/blobs/*/*'),
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
        FileRule('skills/sources/blobs/*/*'),
        FileRule('skills/relays/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('skills/relay-payloads/*.json', False, ('OAH_CAPABILITY_RELAY_REQUIRED(missing)', 1)),
        FileRule('runs/*/{original-run}/result.json'),
        FileRule('runs/*/{original-run}/skills.json'), FileRule('runs/*/{original-run}/prompt.md'),
        FileRule('runs/*/{original-run}/capabilities.json'), FileRule('runs/*/{original-run}/native-offer.json')),
    'settle-retry': (
        FileRule('reply.txt', input_id='native-result'),
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)),
        FileRule('skills/sources/blobs/*/*'),
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
        FileRule('skills/sources/blobs/*/*')),
    'skills-read-review': (
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILLS_USAGE', 1)),
        FileRule('skills/sources/blobs/*/*')),
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
        FileRule('skills/sources/blobs/*/*'),
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
        FileRule('skills/sources/blobs/*/*'),
        FileRule('runs/*/{original-run}/result.json'),
        FileRule('runs/*/{original-run}/skills.json'), FileRule('runs/*/{original-run}/prompt.md'),
        FileRule('runs/*/{original-run}/design.json'), FileRule('runs/*/{original-run}/lease.json'),
        FileRule('runs/*/intent-binding.json', False, ('', 0)), FileRule('runs/*/{original-run}/launch.json')),
    'freeze': (
        FileRule('skills/reviews/*/*.json', False, ('OAH_SKILL_MANDATORY_MISSING', 1)), FileRule('skills/relays/*.json'),
        FileRule('skills/sources/blobs/*/*'),
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

for command in ('admission', 'admission-saved', 'worker'):
    FILE_MATRIX[command] += (FileRule('runs/*/{new-run}/design-after.json') if command != 'worker' else
                            FileRule('runs/*/{original-run}/design-after.json'),)
for command in ('admission', 'admission-saved'):
    FILE_MATRIX[command] += (FileRule('runs/*/{new-run}/last-message.md'),
                            FileRule('runs/*/{new-run}/native-offer.json', False, ('', 0)))
FILE_MATRIX['launch'] = FILE_MATRIX['admission']
FILE_MATRIX['background'] = tuple(rule for rule in FILE_MATRIX['admission']
    if not rule.pattern.endswith(('/design-after.json', '/last-message.md'))) + (
        FileRule('runs/*/{new-run}/launch.json'),)
FILE_MATRIX['background'] = tuple(dataclasses.replace(rule,
    absent_by_context=tuple(row for row in rule.absent_by_context if row[0] != 'call.py:_launch_inputs'))
    if rule.pattern in ('skills/relays/*.json', 'skills/relay-payloads/*.json') else rule
    for rule in FILE_MATRIX['background'])
FILE_MATRIX['admission-orca'] += (FileRule('runs/*/{new-run}/launch.json'),
                                FileRule('runs/*/{new-run}/native-offer.json', False, ('', 0)))
FILE_MATRIX['admission-needs'] += (FileRule('runs/*/{new-run}/capabilities.json', False, ('', 0)),
    FileRule('runs/*/{new-run}/design.json', False, ('', 0)),
    FileRule('runs/*/{new-run}/native-offer.json', False, ('', 0)))
FILE_MATRIX['freeze'] += (FileRule('runs/*/{new-run}/correction.json'),)

for command, rows in FILE_MATRIX.items():
    FILE_MATRIX[command] = tuple(dataclasses.replace(rule,
        recorded_when=(*rule.recorded_when, 'call.py:capture', 'call.py:_check_e'))
        if rule.input_id == 'parent-inventory' else dataclasses.replace(rule, required=True, absent=None,
        absent_by_context=((*rule.absent_by_context, ('call_cli.py:_print_skills',
            ('', 0) if command == 'admission-orca' else ('OAH_RUN_LAUNCH_INVALID', 1)))))
        if rule.pattern.endswith('/skills.json') and command in (
            'admission', 'admission-needs', 'admission-saved', 'launch', 'background', 'admission-orca') else
        dataclasses.replace(rule, absent_by_context=((*rule.absent_by_context,
            ('call.py:_design_after', ('', 0)), ('call.py:_finish_inline_delivery', ('', 0)))))
        if rule.pattern.endswith('/design.json') and command in (
            'admission', 'admission-saved', 'launch', 'worker') else rule for rule in rows)
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
        if not access.owned and fnmatch.fnmatchcase(path, pattern):
            return owner, reason
    if pathlib.Path(access.path).name == 'skills-reads.jsonl' and 'atomic.py:append_jsonl' in access.chain:
        return 'B atomic append validation', 'the shared append helper keeps its existing read contract'
    metadata = not access.owned and access.operation in ('stat', 'lstat') and 'runs.py:_contained_path' in access.chain
    if metadata and pathlib.Path(access.path).name == 'launch.json' and any(
            context in access.chain for context in ('call.py:_launch_main', 'call.py:background_main')):
        return None
    if metadata and 'runs.py:result' in access.chain and 'runs.py:_write_result' not in access.chain:
        return 'B run result update', 'pre-update metadata check; EACCES classification remains a B follow-up'
    if metadata and name == 'freeze' and any(context in access.chain for context in (
            'call.py:_write_result', 'fix.py:_end')):
        if pathlib.Path(access.path).name == 'correction.json' and 'fix.py:_end' in access.chain:
            return 'B correction manifest', 'required output address check at final settlement'
        return 'B correction optional output', 'optional diagnostic writer keeps the base settlement outcome'
    if metadata and 'call.py:_design_after' in access.chain:
        return 'B design after output', 'write-only design-after address check'
    if metadata and 'call.py:_finish_inline_delivery' in access.chain:
        return 'B design delivery output', 'write-only design receiving address check'
    if metadata and (
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


OUTPUT_OWNERS = {
    'B run publication/output validation', 'B run result update', 'B design after output',
    'B design delivery output', 'B correction manifest', 'B correction optional output',
    'B atomic append validation',
}

PRESENCE_MATRIX = {'native-offer-at-handoff': ('OAH_RUN_LAUNCH_INVALID', 1, 'at-access-kept')}
METADATA_OPERATIONS = frozenset(('stat', 'lstat', 'exists', 'is_file', 'fstat'))
BYTE_OPERATIONS = frozenset(('open', 'read', 'os.open', 'os.read'))


def content_read_ordinal(accesses, ordinal):
    access = accesses[ordinal]
    if access.operation not in METADATA_OPERATIONS:
        return ordinal
    return next((index for index in range(ordinal + 1, len(accesses))
                 if accesses[index].path == access.path and accesses[index].operation in BYTE_OPERATIONS), None)


def presence_admission(access, fault):
    return fault == 'invalid-content' and not access.present and \
        pathlib.Path(access.path).name == 'native-offer.json' and 'call.py:check_prepared' in access.chain


def output_row(name, access, path, owner):
    strict = access.site == 'runs.py:_contained_path:538'
    filename = pathlib.Path(path).name
    address = 'address-only' if strict else 'ignored-address'
    launch = 'launch-address-before-read' if strict else 'launch-read-after-address'
    admission = 'admission-address-before-read' if strict else 'admission-read-after-address'
    if owner == 'B atomic append validation':
        return 'address-only'
    if owner == 'B correction optional output':
        return 'ignored-address' if access.phase == 'after-engine' else 'captured-result'
    if owner in ('B correction manifest', 'B design delivery output'):
        return address
    if owner == 'B design after output':
        return launch
    if '/.' in path and '.tmp/' in path or filename == 'last-message.md' or (
            filename == 'launch.json' and name == 'admission-orca') or (
            name == 'freeze' and filename != 'result.json'):
        return 'address-only' if filename == 'last-message.md' and name == 'admission-needs' else address
    if name == 'admission-native':
        return address
    if owner == 'B run result update':
        if name == 'freeze':
            return 'captured-result'
        return admission if any(context in access.chain for context in (
            'call.py:_finish_prepare', 'call.py:_prepare_native')) else launch
    if filename == 'result.json':
        if access.phase != 'before':
            if name == 'background':
                return 'launch-address-before-read'
            return launch if name == 'worker' else address
        if name == 'freeze':
            return 'captured-result'
        return admission if any(context in access.chain for context in (
            'call.py:_finish_prepare', 'call.py:_prepare_native')) else launch
    if strict:
        return 'launch-address-before-read'
    if filename == 'skills.json' and name in ('admission', 'launch', 'admission-needs', 'admission-saved'):
        return 'optional-summary-before-launch'
    return 'launch-read-after-address'


def boundary_at(name, access):
    if name in ('admission', 'admission-native', 'admission-orca', 'admission-needs', 'admission-saved',
                'launch', 'background', 'worker'):
        if any(context in access.chain for context in (
                'call.py:check_prepared', 'call.py:execute', 'call.py:start_background', 'call.py:_launch_main',
                'orca.py:start')):
            return 'launch'
        return 'worker' if name == 'worker' else 'admission'
    return BOUNDARY[name]


def label(access, state):
    path = pathlib.Path(access.path)
    relative = path.relative_to(state).as_posix() if path.is_relative_to(state) else path.name
    return re.sub(r'r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}',
                  '{original-run}' if access.existing else '{new-run}', relative)


class ObservedAccessTest(unittest.TestCase):
    """Change one fixture file at an observed access and assert its literal owner rule.

    For metadata content changes, choose the first later byte read from the clean
    trace before injection, require that exact read with no intervening byte read,
    and require its own direct content cell. With no later read, assert the
    existing presence rule. The faulted outcome never supplies an expected row.
    """
    @classmethod
    def setUpClass(cls):
        cls.completed = {}
        cls.output_cells = set()
        cls.presence_cells = set()

    def assert_content_read(self, observer, expected, classified):
        self.assertTrue(observer.content_accesses, 'the fault must reach the clean trace byte read')
        actual = observer.content_accesses[0]
        self.assertEqual((actual.path, actual.operation, actual.site, actual.file_occurrence, actual.chain,
                          actual.phase, actual.engine_spawns, actual.runner_creations),
                         (expected.path, expected.operation, expected.site, expected.file_occurrence, expected.chain,
                          expected.phase, expected.engine_spawns, expected.runner_creations),
                         'reach the first clean byte read without another byte read in between')
        if expected.owned and not expected.input_owned:
            self.assertTrue(classified, 'the first invalid byte read must be classified by the record owner')
            self.assertEqual(classified[0], (pathlib.Path(expected.path), 'invalid-content', 1),
                             'the first affected byte read must refuse before a later read can mask it')

    def test_metadata_content_uses_the_first_clean_trace_read(self):
        from oah.discovery import records

        now = dt.datetime.now(dt.timezone.utc)

        class FixtureClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(now.timestamp(), tz)

        self.enterContext(mock.patch.object(dt, 'datetime', FixtureClock))
        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('background')
        accesses = case.observe(lambda observer: self.check_success(case, observer))
        ordinal = next(index for index, row in enumerate(accesses)
            if pathlib.Path(row.path).name == 'intent-binding.json' and row.operation == 'stat'
            and row.site.endswith('_contained_path:538'))
        target = content_read_ordinal(accesses, ordinal)
        self.assertIsNotNone(target)
        assert target is not None
        expected = accesses[target]
        self.assertEqual(boundary_at(case.name, expected), 'launch')
        observer = ObservedFiles(case.storage, extra=case.extra, target=accesses[ordinal],
                                 fault='invalid-content', defer_content=True)
        classified = []
        constructor = records.RecordRefusal.__init__

        def classified_record(instance, path, issue, detail):
            if path == pathlib.Path(expected.path) and issue == records.Issue.CONTENT:
                classified.append((path, str(issue), len(observer.content_accesses)))
            constructor(instance, path, issue, detail)

        with mock.patch.object(records.RecordRefusal, '__init__', classified_record):
            code, out, err = case.invoke(observer)
        self.assertTrue(observer.reached)
        self.assert_content_read(observer, expected, classified)
        self.assertEqual((code, 'OAH_RUN_LAUNCH_INVALID:' in err), (1, True), out + err)
        self.assertNotIn('Traceback', err)
        self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))
        self.check_run_state(case, observer, OWNER_RUN_MATRIX['launch'][FAULTS.index('invalid-content')])

    def test_observed_accesses_have_declared_owners(self):
        self.assertEqual(set(BOUNDARY), set(FILE_MATRIX))
        self.assertEqual(set(OWNER_MATRIX), set(BOUNDARY.values()))
        phases, outputs = set(), set()
        self.assertEqual(set(SUCCESS_DISPATCH), set(BOUNDARY))
        self.assertEqual(set(SUCCESS_RUN_MATRIX), set(BOUNDARY))
        for name in BOUNDARY:
            with self.subTest(command=name):
                case = Scenario('scenario')
                case.setUp()
                try:
                    case.scenario(name)
                    accesses = case.observe(lambda observer: self.check_success(case, observer))
                    paths, contexts = set(), set()
                    for ordinal, access in enumerate(accesses):
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
                        excluded = excluded_access(name, access, case.storage)
                        if excluded:
                            outputs.add((excluded[0], access.phase))
                        elif not rule.is_input(access):
                            phases.add((boundary_at(name, access), access.phase))
                        context, _outcome = rule.absence(access)
                        if context is not None and not excluded:
                            contexts.add((index, context))
                        if access.operation in METADATA_OPERATIONS and not excluded:
                            target = content_read_ordinal(accesses, ordinal)
                            if target is not None:
                                self.assertIsNone(excluded_access(name, accesses[target], case.storage),
                                                  'the selected clean byte read needs its own direct content cell')
                    self.assertEqual(paths, set(range(len(FILE_MATRIX[name]))))
                    self.assertEqual(contexts, {(index, context) for index, rule in enumerate(FILE_MATRIX[name])
                                               for context, _outcome in rule.absent_by_context})
                finally:
                    case.doCleanups()
        self.assertEqual(phases, {(name, 'before') for name in OWNER_MATRIX} | set(AFTER_MATRIX))
        self.assertEqual(outputs, {
            ('B atomic append validation', 'before'), ('B correction manifest', 'after-engine'),
            ('B correction optional output', 'before'), ('B correction optional output', 'after-engine'),
            ('B design after output', 'after-engine'),
            ('B design delivery output', 'after-engine'), ('B run publication/output validation', 'before'),
            ('B run publication/output validation', 'after-engine'),
            ('B run publication/output validation', 'after-runner'),
            ('B run result update', 'before'), ('B run result update', 'after-engine'),
            ('B run result update', 'after-runner'), ('lane C design sources', 'before'),
            ('review/fix publication', 'before'), ('review/fix publication', 'after-engine')})

    def check_run_state(self, case, observer, status, *, at_content=False):
        paths = list(case.storage.glob('runs/*/r*/result.json'))
        snapshot = observer.content_retained if at_content else observer.retained
        retained = {path: raw for path, raw in snapshot.items() if path.name == 'result.json'}
        if status in ('kept', 'at-access-kept'):
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
        elif status == 'new-refused':
            self.assertEqual(len(set(paths) - retained.keys()), 1)
            for path, raw in retained.items():
                self.assertEqual(path.read_bytes(), raw, 'keep every earlier result')
            new = next(iter(set(paths) - retained.keys()))
            self.assertEqual(json.loads(new.read_bytes())['status'], 'refused')
        elif status == 'refused-or-unreadable-result-kept':
            self.assertTrue(paths)
            for path in paths:
                if str(path) != observer.failed:
                    if path in case.before and json.loads(case.before[path]).get('status') == 'finished':
                        self.assertEqual(path.read_bytes(), case.before[path])
                        continue
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

    def check_success(self, case, observer, excluded=None):
        previous = {path: raw for path, raw in case.before.items() if path.name == 'result.json'}
        current = set(case.storage.glob('runs/*/r*/result.json'))
        policy = SUCCESS_RUN_MATRIX[case.name]
        if excluded is not None and excluded[0] == 'B correction optional output' and \
                pathlib.Path(observer.failed).name == 'result.json':
            policy = 'new-prepared'
        if policy == 'kept':
            self.assertEqual(current, set(previous), 'a successful read must not allocate a run')
        elif policy == 'worker-finished':
            self.assertEqual(current, set(previous))
            self.assertEqual(json.loads((case.run_dir / 'result.json').read_bytes())['status'], 'finished')
        else:
            self.assertTrue(policy.startswith('new-'))
            self.assertEqual(len(current - previous.keys()), 1, 'the CLI publishes exactly one new run')
            self.assertTrue(previous.keys() <= current, 'keep every earlier run')
            new = next(iter(current - previous.keys()))
            self.assertEqual(json.loads(new.read_bytes())['status'], policy.removeprefix('new-'))
        for path, raw in previous.items():
            if policy != 'worker-finished' or path != case.run_dir / 'result.json':
                self.assertEqual(path.read_bytes(), raw, 'keep the earlier result on optional success')
        for path, raw in case.before.items():
            if not path.is_relative_to(case.storage / 'runs'):
                continue
            kept_input = path.name in SUCCESS_KEPT_RUN_FILES or \
                path.name in ('design.json', 'launch.json') and not (
                    case.name == 'worker' and path.parent == case.run_dir) or \
                path.name == 'native-offer.json' and case.name not in ('settle', 'settle-retry')
            if kept_input:
                self.assertEqual(path.read_bytes(), raw, 'keep the earlier run input on optional success')
        kept = 0
        for path, raw in {**case.before, **observer.retained}.items():
            immutable = path.stem == hashlib.sha256(raw).hexdigest()
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                data = None
            if isinstance(data, dict):
                for key in ('id', 'digest'):
                    expected = 'sha256:' + hashlib.sha256(json.dumps(
                        {name: value for name, value in data.items() if name != key}, sort_keys=True,
                        separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
                    immutable |= data.get(key) == expected and path.stem == expected[7:]
            if immutable:
                self.assertEqual(path.read_bytes(), raw, 'keep the earlier content-addressed record')
                kept += 1
        for path in case.extra:
            if path in observer.retained:
                self.assertEqual(path.read_bytes(), observer.retained[path], 'keep the named input file')
        print('OS_SUCCESS', json.dumps(dict(command=case.name, policy=policy,
            earlier_results=len(previous), immutable_records=kept)), flush=True)

    def test_optional_summary_missing_file_refuses_at_the_required_launch_read(self):
        now = dt.datetime.now(dt.timezone.utc)

        class FixtureClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(now.timestamp(), tz)

        self.enterContext(mock.patch.object(dt, 'datetime', FixtureClock))
        case = Scenario('scenario')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.scenario('admission')
        accesses = case.observe(lambda observer: self.check_success(case, observer))
        lookups = [row for row in accesses if pathlib.Path(row.path).name == 'skills.json'
                   and 'call_cli.py:_print_skills' in row.chain]
        self.assertTrue(lookups, 'observe the summary lookup on this platform')
        access = next((row for row in lookups if row.operation == 'lstat'), lookups[0])
        observer = ObservedFiles(case.storage, extra=case.extra, target=access, fault='missing')
        code, out, err = case.invoke(observer)
        self.assertTrue(observer.reached, 'the summary path lookup must be reached')
        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.startswith('OAH_RUN_LAUNCH_INVALID:'), err)
        self.assertNotIn('Traceback', err)
        self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))
        self.check_run_state(case, observer, 'refused-or-unreadable-result-kept')
        failed = observer.failed
        assert isinstance(failed, str)
        saved = pathlib.Path(failed)
        self.assertEqual(saved.read_bytes(), observer.retained[saved])

    def exercise(self, name):
        from oah.discovery import records

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
        accesses = case.observe(lambda observer: self.check_success(case, observer))
        self.assertTrue(accesses)
        self.assertEqual((case.engine_spawns, case.runner_creations), SUCCESS_DISPATCH[name])
        reached, specified, contexts, content_links, direct_content = set(), set(), set(), set(), set()
        content_targets = {index: content_read_ordinal(accesses, index) for index, row in enumerate(accesses)
                           if row.operation in METADATA_OPERATIONS}
        for ordinal, access in enumerate(accesses):
            path = label(access, case.storage)
            matched = [index for index, rule in enumerate(FILE_MATRIX[name]) if fnmatch.fnmatchcase(path, rule.pattern)]
            self.assertTrue(matched, f'{name}: observed access has no literal owner path: {path} {access}')
            index = min(matched)
            specified.add(index)
            rule = FILE_MATRIX[name][index]
            excluded = excluded_access(name, access, case.storage)
            initial_input = rule.is_input(access)
            owned = access.input_owned if initial_input else access.owned
            self.assertTrue(owned or excluded, f'{name}: file read outside its owner: {access}')
            faults = ('missing', 'denied', 'eio') if excluded else FAULTS
            for fault in faults:
                with self.subTest(command=name, path=path, access=access.operation, site=access.site,
                                  occurrence=access.occurrence, fault=fault):
                    target = content_targets.get(ordinal) if fault == 'invalid-content' else None
                    routed = fault == 'invalid-content' and access.operation in METADATA_OPERATIONS and target is not None
                    outcome_access = accesses[target] if routed and target is not None else access
                    outcome_input = rule.is_input(outcome_access)
                    route = None
                    if fault == 'invalid-content' and access.operation in METADATA_OPERATIONS:
                        route = dict(kind='first-clean-byte-read' if routed else 'metadata-presence',
                            ordinal=target, operation=outcome_access.operation, site=outcome_access.site,
                            file_occurrence=outcome_access.file_occurrence, phase=outcome_access.phase,
                            boundary=boundary_at(name, outcome_access))
                        if routed:
                            self.assertIsNone(excluded_access(name, outcome_access, case.storage),
                                              'the clean byte read needs its own direct content cell')
                    case.restore()
                    observer = ObservedFiles(case.storage, extra=case.extra, target=access, fault=fault,
                                             defer_content=routed)
                    classified = []
                    constructor = records.RecordRefusal.__init__

                    def classified_record(instance, target_path, issue, detail):
                        if target_path == pathlib.Path(outcome_access.path) and issue == records.Issue.CONTENT:
                            classified.append((target_path, str(issue), len(observer.content_accesses)))
                        constructor(instance, target_path, issue, detail)

                    with contextlib.ExitStack() as retry_window:
                        if routed:
                            retry_window.enter_context(mock.patch.object(records.RecordRefusal, '__init__', classified_record))
                        if fault == 'denied':
                            for module in ('oah.discovery.records', 'oah.discovery.inputs', 'oah.runs', 'oah.atomic'):
                                retry_window.enter_context(mock.patch(module + '.RETRY_SECONDS', 0.01))
                        result = case.invoke(observer)
                    self.assertTrue(observer.reached, 'the named OS access must be reached')
                    self.assertEqual({(row.phase, row.engine_spawns, row.runner_creations)
                                      for row in observer.reached},
                                     {(access.phase, access.engine_spawns, access.runner_creations)})
                    self.assertEqual({row.chain for row in observer.reached}, {access.chain},
                                     'reach the same recorded read purpose as the control run')
                    if routed:
                        self.assert_content_read(observer, outcome_access, classified)
                    reached.add((ordinal, fault))
                    codes = [code for code in re.findall(r'(?m)^(OAH_[A-Z_]+(?:\([^\n:]+\))?):', result[2])
                             if code not in ('OAH_STAGE_OVERRIDDEN', 'OAH_MODEL_UNKNOWN_FAMILY', 'OAH_FIX_STARTED',
                                             'OAH_FIX_APPLIED')]
                    code = codes[0] if result[0] and codes else ''
                    print('OS_CASE', json.dumps(dict(command=name, path=path, operation=access.operation,
                        site=access.site, occurrence=access.occurrence, fault=fault, code=code,
                        exit=result[0], phase=access.phase, engine_spawns=case.engine_spawns,
                        runner_creations=case.runner_creations, boundary=boundary_at(name, outcome_access),
                        fault_boundary=boundary_at(name, access),
                        chain=access.chain, absent_context=rule.absence(access)[0] if fault == 'missing' else None,
                        content_route=route,
                        admission='native-offer-at-handoff' if presence_admission(access, fault) else None,
                        excluded=excluded, input=rule.input_id if outcome_input else None,
                        fault_input=rule.input_id if initial_input else None)), flush=True)
                    if excluded:
                        if excluded[0] in OUTPUT_OWNERS:
                            row = output_row(name, access, path, excluded[0])
                            expected = OUTPUT_MATRIX[row][('missing', 'denied', 'eio').index(fault)]
                            self.assertEqual((code, result[0]), expected, str(result))
                            self.__class__.output_cells.add((row, fault))
                        self.assertEqual((case.engine_spawns, case.runner_creations),
                            SUCCESS_DISPATCH[name] if result[0] == 0 else
                            (access.engine_spawns, access.runner_creations), 'no dispatch after an output fault')
                        if access.phase != 'before' and result[0]:
                            self.check_run_state(case, observer, 'at-access-kept')
                        for earlier, raw in observer.retained.items():
                            if earlier.name == 'result.json' and json.loads(raw).get('status') == 'finished':
                                self.assertEqual(earlier.read_bytes(), raw, 'keep the earlier finished record')
                        if not result[0]:
                            self.check_success(case, observer, excluded)
                        continue
                    boundary = boundary_at(name, outcome_access)
                    if outcome_access.phase == 'before':
                        expected = OWNER_MATRIX[boundary][FAULTS.index(fault)]
                        run_policy = OWNER_RUN_MATRIX[boundary][FAULTS.index(fault)]
                        kept_policy = OWNER_KEPT_MATRIX[boundary][FAULTS.index(fault)]
                        dispatches = (0, 0)
                    else:
                        outward, exit_code, run_policy, kept_policy, engines, runners = AFTER_MATRIX[
                            boundary, outcome_access.phase][FAULTS.index(fault)]
                        expected, dispatches = (outward, exit_code), (engines, runners)
                    context, absent = rule.absence(access) if fault == 'missing' else (None, None)
                    if fault == 'missing':
                        if not rule.required or context is not None:
                            assert absent is not None
                            expected = absent[:2]
                            if len(absent) == 3:
                                run_policy = absent[2]
                    if outcome_input:
                        assert rule.input_id is not None
                        expected = INPUT_MATRIX[rule.input_id][FAULTS.index(fault)]
                    if presence_admission(access, fault):
                        outward, exit_code, run_policy = PRESENCE_MATRIX['native-offer-at-handoff']
                        expected = (outward, exit_code)
                    self.assertEqual((code, result[0]), expected, str(result))
                    if presence_admission(access, fault):
                        self.__class__.presence_cells.add('native-offer-at-handoff')
                    if fault == 'missing' and context is not None:
                        contexts.add((index, context))
                    if fault == 'invalid-content' and access.operation in BYTE_OPERATIONS:
                        direct_content.add(ordinal)
                    if routed:
                        content_links.add((ordinal, target))
                    self.assertNotIn('Traceback', result[2])
                    if outcome_input and fault != 'eio':
                        self.assertNotIn('the recorded ', result[2])
                    if result[0]:
                        self.check_run_state(case, observer, run_policy, at_content=routed)
                        self.assertEqual((case.engine_spawns, case.runner_creations), dispatches,
                                         'the refusal must not cause another dispatch')
                        if outcome_access.phase != 'before':
                            self.assertIn('the run already took place' if outcome_access.phase == 'after-engine' else
                                          'the runner was already created', result[2])
                            self.assertIn('the follow-up read failed', result[2])
                            self.assertNotIn('nothing was sent', result[2])
                            self.assertNotIn('nothing was launched', result[2])
                        if name == 'freeze':
                            assert case.project is not None
                            self.assertEqual(case.project.fix_calls(), case.child_calls)
                    else:
                        self.assertEqual((case.engine_spawns, case.runner_creations), SUCCESS_DISPATCH[name])
                        self.check_success(case, observer)
                    for earlier, raw in observer.retained.items():
                        if not result[0]:
                            continue
                        if earlier.is_relative_to(case.storage) and any(fnmatch.fnmatchcase(
                                earlier.relative_to(case.storage).as_posix(), pattern)
                                for pattern, _owner, _reason in EXCLUDED_PATHS):
                            continue
                        if access.phase == 'before' and earlier.name == 'result.json' and str(earlier) != observer.failed:
                            continue
                        injected_content = fault == 'invalid-content' and observer.failed == str(earlier) and \
                            access.operation in ('stat', 'lstat', 'fstat', 'os.open') and not observer.defer_content
                        self.assertEqual(earlier.read_bytes(), b'\xff' if injected_content else raw,
                                         f'{name}: keep the earlier {earlier.name}')
                    self.assertEqual(kept_policy, 'inputs-at-access')
                    if name == 'worker' and outcome_access.phase == 'before' and result[0] and \
                            pathlib.Path(access.path).name != 'result.json' and not presence_admission(access, fault):
                        self.assertEqual(json.loads((case.run_dir / 'result.json').read_bytes())['status'], 'refused')
                    if name == 'worker' and access.phase == 'before' and pathlib.Path(access.path).name == 'result.json':
                        self.assertEqual((case.run_dir / 'worker.claim').exists(), access.claim_present or fault != 'eio')
                        raw = (case.run_dir / 'result.json').read_bytes()
                        expected_raw = b'\xff' if fault == 'invalid-content' and not observer.defer_content and \
                            access.operation in ('stat', 'lstat', 'fstat', 'os.open') \
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
        self.assertTrue({target for _source, target in content_links} <= direct_content,
                        'each clean byte read selected by metadata content needs its own reached direct content cell')
        self.__class__.completed[name] = dict(accesses=len(accesses), cells=len(reached))

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

    def test_z_all_commands_have_reached_every_observed_fault_cell(self):
        self.assertEqual(set(self.completed), set(BOUNDARY), 'each command must finish its reached-access checks')
        self.assertEqual(self.presence_cells, set(PRESENCE_MATRIX), 'each presence admission row must be reached')
        self.assertEqual(self.output_cells, {(row, fault) for row in OUTPUT_MATRIX
                                           for fault in ('missing', 'denied', 'eio')})
        print('OS_COVERAGE', json.dumps(self.completed, sort_keys=True), flush=True)
