from __future__ import annotations

import datetime as dt
import dataclasses
import fnmatch
import json
import pathlib
import unittest
from unittest import mock

from tests.e_c12_observer import ObservedFiles
from tests.e_c12_scenarios import Scenario
from tests.test_e_c12_observed import label
from tests.test_e_c10_boundaries import cli_result


CONTENT_CASES = (
    ('skills-read-review', 'skills/reviews/*/*.json', 'status', [], 'OAH_SKILLS_IO', 69),
    ('skills-candidates', 'skills/queries/*/*.json', 'query_id', 'sha256:' + '0' * 64, 'OAH_SKILLS_IO', 69),
    ('skills-candidates', 'skills/queries/*/*.json', 'purpose', [], 'OAH_SKILLS_IO', 69),
    ('skills-candidates', 'skills/queries/*/*.json', 'hits', [0], 'OAH_SKILLS_IO', 69),
    ('skills-candidates', 'skills/queries/*/*.json', 'query', [], 'OAH_SKILLS_IO', 69),
    ('skills-candidates', 'skills/queries/*/*.json', 'checked_at', 'changed', 'OAH_SKILLS_IO', 69),
    ('skills-review', 'skills/reviews/index.json', 'by_locator', [], 'OAH_SKILLS_IO', 69),
    ('skills-review', 'skills/identities.json', 'sources', [], 'OAH_SKILLS_IO', 69),
    ('skills-review', 'skills/identities.json', 'sources', {'changed': []}, 'OAH_SKILLS_IO', 69),
    ('skills-select', 'skills/task-selections/index.json', 'needs', [], 'OAH_SKILLS_IO', 69),
    ('skills-select', 'skills/task-selections/index.json', 'needs', {'changed': []}, 'OAH_SKILLS_IO', 69),
    ('skills-select', 'skills/task-selections/*.json', 'review_ids', [], 'OAH_SKILLS_IO', 69),
    ('admission-needs', 'skills/task-discovery/index.json', 'needs', [], 'OAH_SKILL_STATE_CORRUPT', 69),
    ('admission-needs', 'skills/task-discovery/*.json', 'outcome', [], 'OAH_SKILL_STATE_CORRUPT', 69),
    ('freeze', 'skills/originals/tasks/*.json', 'original', [], 'OAH_FIX_STATE_CHANGED', 1),
    ('freeze', 'skills/originals/*.json', 'digest', 'sha256:' + '0' * 64, 'OAH_FIX_STATE_CHANGED', 1),
    ('skills-receiving', 'runs/*/{original-run}/skills-receiving.json', 'delivery', 'unverified',
     'OAH_SKILLS_IO', 69),
    ('skills', 'runs/*/{original-run}/native-offer.json', 'id', 'sha256:' + '0' * 64, 'OAH_SKILLS_IO', 69),
    ('skills-receiving', 'runs/*/{original-run}/native-settlement.json', 'id', 'sha256:' + '0' * 64,
     'OAH_SKILLS_IO', 69),
    ('admission-needs', 'runs/*/intent-binding.json', 'revision', [], 'OAH_SKILL_STATE_CORRUPT', 69),
    ('worker', 'runs/*/{original-run}/lease.json', 'generation', [], 'OAH_RUN_LAUNCH_INVALID', 1),
    ('worker', 'runs/*/{original-run}/design.json', 'version', [], 'OAH_RUN_LAUNCH_INVALID', 1),
    ('worker', 'runs/*/{original-run}/launch.json', 'warnings', 'changed', 'OAH_RUN_LAUNCH_INVALID', 1),
    ('worker', 'runs/*/{original-run}/result.json', 'stage', [], 'OAH_RUN_LAUNCH_INVALID', 1),
)


class ContentOwnershipTest(unittest.TestCase):
    def test_offer_fields_use_the_read_command_category_with_a_matching_offer_id(self):
        from oah.launchers import native

        for field, value in (('expires_at', 'changed'), ('target', []), ('task', [])):
            with self.subTest(field=field):
                case = Scenario('scenario')
                case.setUp()
                try:
                    case.scenario('skills')
                    target = next(access for access in case.observe() if access.owned and
                                  pathlib.Path(access.path).name == 'native-offer.json')
                    path = pathlib.Path(target.path)
                    offer = json.loads(path.read_bytes())
                    offer[field] = value
                    offer['id'] = native.offer_id(offer)
                    path.write_text(json.dumps(offer), encoding='utf-8')
                    earlier = {item: item.read_bytes() for item in case.storage.rglob('*') if item.is_file()}
                    observer = ObservedFiles(case.storage, target=target)

                    try:
                        result = case.invoke(observer)
                    except (ValueError, TypeError, AttributeError, KeyError) as exc:
                        self.fail(f'the CLI must return the offer field refusal: {exc}')

                    self.assertTrue(observer.reached, 'the CLI must read this changed offer field')
                    self.assertEqual(result[0], 69, str(result))
                    self.assertIn('OAH_SKILLS_IO:', result[2])
                    self.assertNotIn('Traceback', result[2])
                    self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))
                    for item, raw in earlier.items():
                        self.assertEqual(item.read_bytes(), raw, 'keep the earlier fixture record')
                    print('CONTENT_OFFER', json.dumps(dict(field=field, code='OAH_SKILLS_IO',
                        exit=69, reached=True)), flush=True)
                finally:
                    case.doCleanups()

    def test_relay_and_payload_schema_fields_use_the_reader_category_with_matching_content_ids(self):
        from oah.discovery import store

        for field in ('invocation.arguments_digest', 'payload.text'):
            with self.subTest(field=field):
                case = Scenario('scenario')
                case.setUp()
                try:
                    case.scenario('admission')
                    accesses = case.observe()
                    relay_id = case.command[case.command.index('--relay-result') + 1]
                    relay_path = case.storage / 'skills/relays' / (relay_id.removeprefix('sha256:') + '.json')
                    relay = json.loads(relay_path.read_bytes())
                    target_path = relay_path
                    payload_id = None
                    if field == 'invocation.arguments_digest':
                        relay['invocation']['arguments_digest'] = 'sha256:abc'
                    else:
                        target_path = case.storage / 'skills/relay-payloads' / (
                            relay['payload']['blob_ref'].removeprefix('sha256:') + '.json')
                        blob = json.loads(target_path.read_bytes())
                        blob['text'] = 0
                        _word, blob = store.put_record(case.storage, 'relay-payloads', blob)
                        payload_id = blob['id']
                        relay['payload']['blob_ref'] = payload_id
                    target = next(access for access in accesses if access.owned and
                                  access.path == str(target_path))
                    _word, relay = store.put_record(case.storage, 'relays', relay)
                    case.command[case.command.index('--relay-result') + 1] = relay['id']
                    changed_id = payload_id or relay['id']
                    target_path = target_path.with_name(changed_id.removeprefix('sha256:') + '.json')
                    case.remember()
                    earlier = {path: path.read_bytes() for path in case.storage.rglob('*') if path.is_file()}
                    observer = ObservedFiles(case.storage, extra=case.extra,
                        target=dataclasses.replace(target, path=str(target_path)))

                    try:
                        result = case.invoke(observer)
                    except (ValueError, TypeError, AttributeError, KeyError) as exc:
                        self.fail(f'the CLI must return the relay field refusal: {exc}')

                    self.assertTrue(observer.reached, 'the CLI must read the changed field with its matching id')
                    self.assertEqual(result[0], 69, str(result))
                    self.assertIn('OAH_SKILL_STATE_CORRUPT:', result[2])
                    self.assertNotIn('Traceback', result[2])
                    self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))
                    for path, raw in earlier.items():
                        self.assertEqual(path.read_bytes(), raw, 'keep the earlier fixture record')
                    print('CONTENT_RELAY', json.dumps(dict(field=field, code='OAH_SKILL_STATE_CORRUPT',
                        exit=69, reached=True)), flush=True)
                finally:
                    case.doCleanups()

    def test_own_record_fields_refuse_inside_the_reader_before_the_cli_can_continue(self):
        from oah.discovery import records

        reached = set()
        for ordinal, (command, pattern, field, value, code, exit_code) in enumerate(CONTENT_CASES):
            with self.subTest(command=command, record=pattern, field=field):
                now = dt.datetime.now(dt.timezone.utc)

                class FixtureClock(dt.datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return cls.fromtimestamp(now.timestamp(), tz)

                with mock.patch.object(dt, 'datetime', FixtureClock):
                    case = Scenario('scenario')
                    case.setUp()
                    try:
                        case.scenario(command)
                        if '/task-discovery/' in pattern:
                            review = next(data['bundle']['items'][0]['review']
                                for path in case.storage.glob('runs/*/r*/skills.json')
                                if (data := json.loads(path.read_bytes())).get('bundle'))
                            need = cli_result(case.root, case.environment, 'skills', 'find', '--task', 't1',
                                '--tech', 'database=postgresql', '--required', '--engine', 'claude',
                                '--out', str(case.extra[0]), '--json')
                            self.assertEqual(need[0], 0, str(need))
                            selected = cli_result(case.root, case.environment, 'skills', 'select',
                                '--needs', str(case.extra[0]), '--review', review, '--json')
                            self.assertEqual(selected[0], 0, str(selected))
                            control = cli_result(case.root, case.environment, 'skills', 'find', '--task', 't1',
                                '--needs', str(case.extra[0]), '--engine', 'claude', '--json')
                            self.assertEqual(control[0], 0, str(control))
                            case.remember()
                        if '/queries/' in pattern:
                            from oah.discovery import store

                            paths = [access.path for access in case.observe() if access.owned and
                                     fnmatch.fnmatchcase(label(access, case.storage), pattern)]
                            self.assertTrue(paths, 'the control CLI must query the named cache')
                            ident = 'sha256:' + pathlib.Path(paths[0]).stem
                            store.put_query(case.storage, dict(version=1, query_id=ident, provider='skills-sh',
                                query='postgresql schema design', parser='skills-sh-search/v1',
                                checked_at=now.isoformat(), outcome='zero-results', detail='no results',
                                count=0, hits=[], origin='template', choice=None, purpose={}))
                            case.remember()
                        targets = [access for access in case.observe() if access.owned and access.present and
                                   access.existing and fnmatch.fnmatchcase(label(access, case.storage), pattern)]
                        targets = [access for access in targets
                                   if field in json.loads(pathlib.Path(access.path).read_bytes())]
                        self.assertTrue(targets, 'the control CLI must read the named fixture record')
                        target = targets[0]
                        path = pathlib.Path(target.path)
                        data = json.loads(path.read_bytes())
                        self.assertNotEqual(data.get(field), value)
                        data[field] = value
                        path.write_text(json.dumps(data), encoding='utf-8')
                        earlier = {item: item.read_bytes() for item in case.storage.rglob('*') if item.is_file()
                                   and item.suffix in ('.json', '.jsonl', '.md')
                                   and '/skills/sources/' not in item.as_posix()}
                        refusals = []
                        constructor = records.InvalidRecord.__init__

                        def refused(instance, *args, **options):
                            constructor(instance, *args, **options)
                            refusals.append(instance)

                        observer = ObservedFiles(case.storage, extra=case.extra, target=target)
                        with mock.patch.object(records.InvalidRecord, '__init__', refused):
                            try:
                                result = case.invoke(observer)
                            except (AttributeError, TypeError, KeyError, ValueError, records.InvalidRecord) as exc:
                                self.fail(f'the CLI must return the content refusal for {path.name}: {exc}')
                        self.assertTrue(observer.reached, 'the changed field must reach its recorded OS access')
                        self.assertEqual(result[0], exit_code, str(result))
                        self.assertIn(code + ':', result[2])
                        self.assertNotIn('Traceback', result[2])
                        matching = [refusal for refusal in refusals if getattr(refusal, 'path', None) == path]
                        self.assertTrue(matching, 'the stored-record reader must classify this field')
                        self.assertTrue(all(not isinstance(refusal, (OSError, ValueError)) for refusal in matching))
                        self.assertEqual((case.engine_spawns, case.runner_creations), (0, 0))
                        if case.project is not None:
                            self.assertEqual(case.project.fix_calls(), case.child_calls)
                        for item, raw in earlier.items():
                            if command == 'worker' and item == case.run_dir / 'result.json' and item != path:
                                self.assertEqual(json.loads(item.read_bytes())['status'], 'refused')
                                continue
                            self.assertEqual(item.read_bytes(), raw, f'keep the earlier {item.name}')
                        if command == 'worker' and path == case.run_dir / 'result.json':
                            self.assertTrue((case.run_dir / 'worker.claim').exists())
                        reached.add(ordinal)
                        print('CONTENT_CASE', json.dumps(dict(command=command, path=pattern, field=field,
                            code=code, exit=exit_code, reached=True)), flush=True)
                    finally:
                        case.doCleanups()
        self.assertEqual(reached, set(range(len(CONTENT_CASES))))


if __name__ == '__main__':
    unittest.main()
