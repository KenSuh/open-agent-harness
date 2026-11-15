from __future__ import annotations

import dataclasses
import os
import pathlib
import sys
import unittest
from unittest import mock

from oah import paths
from oah.engines import proc
from tests import test_e_c12_observed as checks
from tests import test_e_c12_regressions as regressions
from tests.e_c12_scenarios import Scenario, recorded_path
from tests.test_e_c10_boundaries import cli_result
from tests.helpers import guard_subprocesses


class HermeticScenariosTest(unittest.TestCase):
    def test_e_c14_scenarios_supply_engines_and_match_both_recorded_path_forms(self):
        real_find = proc._find_command
        git = paths.git_argv()[0]
        programs = {os.path.normcase(git), os.path.normcase(sys.executable)}

        def find(command, *args, **options):
            self.assertNotIn(pathlib.PureWindowsPath(command).name.lower(),
                ('claude', 'claude.exe', 'claude.cmd', 'codex', 'codex.exe', 'codex.cmd'),
                'E-C14: configure a fake engine before discovery or admission')
            return real_find(command, *args, **options)

        guard_subprocesses(self, lambda argv: argv[0] == 'git' or os.path.normcase(argv[0]) in programs
                           or argv[:2] == ['/bin/ls', '-lde'])
        with mock.patch.object(proc, '_find_command', find):
            select = regressions.capture_selection_access
            captured = []

            def portable_selection(accesses):
                selected = select(accesses)
                captured.append(selected)
                for separator in ('/', '\\'):
                    converted = [dataclasses.replace(row,
                        path=row.path.replace('\\', '/').replace('/', separator)) for row in accesses]
                    self.assertEqual(select(converted), dataclasses.replace(selected,
                        path=selected.path.replace('\\', '/').replace('/', separator)))
                return selected

            result = unittest.TestResult()
            with mock.patch.object(regressions, 'capture_selection_access', portable_selection):
                regressions.RecordRegression(
                    'test_capture_selection_change_keeps_the_original_and_valid_run_advances_it').run(result)
            self.assertEqual((result.testsRun, result.errors, result.failures, result.skipped), (1, [], [], []))
            self.assertTrue(captured, 'E-C14: the capture regression must reach its portable selector')
            for name in sorted(checks.BOUNDARY, key=lambda name: (name != 'skills-find', name != 'background')):
                with self.subTest(command=name):
                    case = Scenario('scenario')
                    try:
                        case.setUp()
                        case.scenario(name)
                        accesses = case.observe()
                        if name == 'admission-needs':
                            found = cli_result(case.root, case.environment, 'skills', 'find', '--task', 't1',
                                '--needs', str(case.extra[0]), '--engine', 'claude', '--json')
                            self.assertEqual(found[0], 0, str(found))
                        windows = [dataclasses.replace(row, path=row.path.replace('/', '\\')) for row in accesses]
                        windows_state = str(case.storage).replace('/', '\\')
                        for native, converted in zip(accesses, windows):
                            self.assertEqual(recorded_path(converted.path).parts[-3:],
                                             pathlib.Path(native.path).parts[-3:])
                            self.assertEqual(checks.label(converted, windows_state), checks.label(native, case.storage))
                            self.assertEqual(checks.excluded_access(name, converted, windows_state),
                                             checks.excluded_access(name, native, case.storage))
                        if name == 'background':
                            targets = checks.post_runner_output_accesses(accesses)
                            self.assertEqual(checks.post_runner_output_accesses(windows), tuple(
                                dataclasses.replace(row, path=row.path.replace('/', '\\')) for row in targets))
                            windows_trace = [row for row in windows if row.operation != 'lstat']
                            self.assertEqual(checks.post_runner_output_accesses(windows_trace, platform='nt'),
                                             (dataclasses.replace(targets[0], path=targets[0].path.replace('/', '\\')),))
                            stat = targets[0]
                            self.assertEqual((stat.operation, stat.site, stat.phase),
                                             ('stat', 'runs.py:_contained_path:538', 'after-runner'))
                            for wrong in (dataclasses.replace(stat, operation='read'),
                                          dataclasses.replace(stat, phase='before'),
                                          dataclasses.replace(stat, site='runs.py:_contained_path:525'),
                                          dataclasses.replace(stat, chain=tuple(part for part in stat.chain
                                              if part != 'runs.py:_write_result')),
                                          dataclasses.replace(stat, chain=tuple(part for part in stat.chain
                                              if part != 'runs.py:_contained_path'))):
                                self.assertEqual(checks.post_runner_output_accesses([*accesses, wrong]), targets)
                    finally:
                        case.doCleanups()


if __name__ == '__main__':
    unittest.main()
