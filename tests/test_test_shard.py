# CI 테스트 분할 검사 — 탐색한 테스트가 정확히 한 조각에 들어가고 고르게 나뉘는지 확인한다.
from __future__ import annotations

import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("test_shard_tool", ROOT / "tools" / "test_shard.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class ShardTest(unittest.TestCase):
    def test_nested_packages_and_load_tests_match_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / "shard_fixture"
            files = {
                "__init__.py": "# 분할 테스트용 패키지.\n",
                "nested/__init__.py": "# 재귀 탐색 테스트용 패키지.\n",
                "nested/test_example.py": """
                    # 중첩 테스트 모듈.
                    import unittest
                    class NestedTest(unittest.TestCase):
                        def test_nested(self): pass
                """,
                "testhook.py": """
                    # 기본 탐색 패턴과 모듈 훅 검사.
                    import unittest
                    class HookTest(unittest.TestCase):
                        def test_module_hook(self): pass
                    def load_tests(loader, tests, pattern):
                        assert pattern == 'test*.py', pattern
                        return tests
                """,
                "hooked/__init__.py": """
                    # 패키지 훅은 직접 자식 탐색을 수행한다.
                    import pathlib
                    def load_tests(loader, tests, pattern):
                        assert pattern == 'test*.py', pattern
                        return loader.discover(str(pathlib.Path(__file__).parent), pattern=pattern)
                """,
                "hooked/test_child.py": """
                    # 패키지 훅이 수집하는 테스트.
                    import unittest
                    class ChildTest(unittest.TestCase):
                        def test_child(self): pass
                """,
            }
            for name, source in files.items():
                path = folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
            old_path = sys.path[:]
            try:
                expected = unittest.TestLoader().discover(str(folder), top_level_dir=tmp)
                self.assertEqual(expected.countTestCases(), 3)
                expected_ids = self._test_ids(expected)
                for count in (1, 2, 3, 5):
                    with self.subTest(count=count):
                        groups = tool.shards(count, folder)
                        actual = unittest.TestSuite(unittest.TestSuite(group) for group in groups)
                        self.assertEqual(sorted(self._test_ids(actual)), sorted(expected_ids))
                        result = unittest.TestResult()
                        actual.run(result)
                        self.assertTrue(result.wasSuccessful(), result.errors)
                        self.assertEqual(result.testsRun, len(expected_ids))
            finally:
                sys.path[:] = old_path
                for name in list(sys.modules):
                    if name == "shard_fixture" or name.startswith("shard_fixture."):
                        del sys.modules[name]

    def test_root_package_hook_controls_discovery_and_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / "root_hook_fixture"
            folder.mkdir()
            (folder / "__init__.py").write_text(textwrap.dedent("""
                # 루트 패키지 훅이 기본 탐색을 대체한다.
                import unittest
                calls = []
                class HookTest(unittest.TestCase):
                    def test_hook(self):
                        self.assertEqual(calls, ['test*.py'])
                def load_tests(loader, tests, pattern):
                    calls.append(pattern)
                    return tests
            """).lstrip(), encoding="utf-8")
            (folder / "test_excluded.py").write_text(
                "# 루트 패키지 훅이 제외하는 모듈.\nraise AssertionError('must not be imported')\n",
                encoding="utf-8")
            old_path = sys.path[:]
            try:
                expected = unittest.TestLoader().discover(str(folder), top_level_dir=tmp)
                expected_ids = self._test_ids(expected)
                self.assertEqual(len(expected_ids), 1)
                sys.modules[folder.name].calls.clear()
                groups = tool.shards(3, folder)
                self.assertEqual(self._test_ids(groups), expected_ids)
                sys.modules[folder.name].calls.clear()
                original_shards = tool.shards
                with mock.patch.object(tool, "shards", side_effect=lambda count, **kwargs:
                                       original_shards(count, folder, **kwargs)), \
                        mock.patch("sys.stdout", new_callable=io.StringIO), \
                        mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    self.assertEqual(tool.main(["1", "3", "-v"]), 0, stderr.getvalue())
                    self.assertIn("Ran 1 test", stderr.getvalue())
            finally:
                sys.path[:] = old_path
                for name in list(sys.modules):
                    if name == folder.name or name.startswith(f"{folder.name}."):
                        del sys.modules[name]

    def test_load_tests_custom_suite_preserves_nested_execution(self):
        events = []

        class FirstTest(unittest.TestCase):
            def test_first(self):
                self.assertEqual(events, ["setup"])
                events.append("first")

        class SecondTest(unittest.TestCase):
            def test_second(self):
                self.assertEqual(events, ["setup", "first"])
                events.append("second")

        class Wrapper(unittest.TestSuite):
            def run(self, result, debug=False):
                events.append("setup")
                try:
                    return super().run(result, debug)
                finally:
                    events.append("teardown")

        module = types.ModuleType("custom_suite_fixture")

        def load_tests(loader, tests, pattern):
            return Wrapper([
                unittest.TestSuite([FirstTest("test_first")]),
                unittest.TestSuite([SecondTest("test_second")]),
            ])

        module.load_tests = load_tests
        for count in (1, 2, 5):
            with self.subTest(count=count):
                events.clear()
                loader = unittest.TestLoader()
                with mock.patch.object(loader, "discover", side_effect=lambda *args, **kwargs:
                                       unittest.TestSuite([loader.loadTestsFromModule(module)])):
                    groups = tool.shards(count, loader=loader)
                actual = unittest.TestSuite(unittest.TestSuite(group) for group in groups)
                result = unittest.TestResult()
                actual.run(result)
                self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
                self.assertEqual(result.testsRun, 2)
                self.assertEqual(events, ["setup", "first", "second", "teardown"])

    @staticmethod
    def _test_ids(suite):
        if isinstance(suite, unittest.TestCase):
            return [suite.id()]
        return [name for child in suite for name in ShardTest._test_ids(child)]

    def test_every_discovered_test_runs_in_exactly_one_shard(self):
        expected = sorted(self._test_ids(unittest.TestLoader().discover(
            str(ROOT / "tests"), top_level_dir=str(ROOT))))
        for count in range(1, 7):
            with self.subTest(count=count):
                groups = tool.shards(count)
                self.assertEqual(len(groups), count)
                self.assertTrue(all(groups))
                flat = self._test_ids(unittest.TestSuite(unittest.TestSuite(group) for group in groups))
                self.assertEqual(sorted(flat), expected)
                self.assertEqual(len(flat), len(set(flat)))
                repeated = tool.shards(count)
                self.assertEqual([self._test_ids(group) for group in groups],
                                 [self._test_ids(group) for group in repeated])

    def test_split_is_balanced_by_test_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / "balance_fixture"
            folder.mkdir()
            (folder / "__init__.py").write_text("# 테스트 수 균형 검사 패키지.\n", encoding="utf-8")
            for name, count in (("a", 9), ("b", 5), ("c", 4), ("d", 3), ("e", 2)):
                source = "# 테스트 수 균형 검사 모듈.\nimport unittest\nclass Test(unittest.TestCase):\n"
                source += "".join(f"    def test_{i}(self): pass\n" for i in range(count))
                (folder / f"test_{name}.py").write_text(source, encoding="utf-8")
            old_path = sys.path[:]
            try:
                groups = tool.shards(2, folder)
                totals = [sum(suite.countTestCases() for suite in group) for group in groups]
                self.assertEqual(sorted(totals), [11, 12])
            finally:
                sys.path[:] = old_path
                for name in list(sys.modules):
                    if name == "balance_fixture" or name.startswith("balance_fixture."):
                        del sys.modules[name]

    def test_methods_of_one_class_spread_over_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / "method_fixture"
            folder.mkdir()
            (folder / "__init__.py").write_text("# 메서드 단위 분할 검사 패키지.\n", encoding="utf-8")
            source = "# 큰 클래스 하나를 메서드 단위로 나누는 검사.\nimport unittest\n"
            for name, count in (("Large", 60), ("Small", 3)):
                source += f"class {name}Test(unittest.TestCase):\n"
                source += "".join(f"    def test_{i:02}(self): pass\n" for i in range(count))
            (folder / "test_big.py").write_text(source, encoding="utf-8")
            old_path = sys.path[:]
            try:
                discovered = self._test_ids(unittest.TestLoader().discover(str(folder), top_level_dir=tmp))
                groups = tool.shards(5, folder)
                self.assertEqual([self._test_ids(group) for group in groups],
                                 [self._test_ids(group) for group in tool.shards(5, folder)])
                per_shard = [self._test_ids(group) for group in groups]
                self.assertEqual([sum(".LargeTest." in name for name in ids) for ids in per_shard], [12] * 5)
                self.assertEqual(sorted(len(ids) for ids in per_shard), [12, 12, 13, 13, 13])
                for ids in per_shard:
                    self.assertEqual(ids, sorted(ids, key=discovered.index))
            finally:
                sys.path[:] = old_path
                for name in list(sys.modules):
                    if name == "method_fixture" or name.startswith("method_fixture."):
                        del sys.modules[name]

    def test_fixtures_run_once_per_shard_that_holds_their_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / "fixture_events"
            folder.mkdir()
            (folder / "__init__.py").write_text(
                "# 조각별 픽스처 실행 검사 패키지.\nevents = []\n", encoding="utf-8")
            for module, classes in (("test_one", (("AlphaTest", 7), ("BetaTest", 2))),
                                    ("test_two", (("GammaTest", 4),))):
                source = textwrap.dedent(f"""
                    # 모듈과 클래스 픽스처 호출을 기록하는 모듈.
                    import unittest
                    from fixture_events import events
                    def setUpModule(): events.append("module_setup:{module}")
                    def tearDownModule(): events.append("module_teardown:{module}")
                """).lstrip()
                for name, count in classes:
                    source += textwrap.dedent(f"""
                        class {name}(unittest.TestCase):
                            @classmethod
                            def setUpClass(cls): events.append("class_setup:{name}")
                            @classmethod
                            def tearDownClass(cls): events.append("class_teardown:{name}")
                    """)
                    source += "".join(f"    def test_{i}(self): events.append(self.id())\n"
                                      for i in range(count))
                (folder / f"{module}.py").write_text(source, encoding="utf-8")
            old_path = sys.path[:]
            try:
                for count in (2, 3, 5):
                    with self.subTest(count=count):
                        events = None
                        for group in tool.shards(count, folder):
                            ids = self._test_ids(group)
                            events = sys.modules["fixture_events"].events
                            events.clear()
                            result = unittest.TestResult()
                            unittest.TestSuite(group).run(result)
                            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
                            self.assertEqual(events, self._expected_fixture_events(ids))
            finally:
                sys.path[:] = old_path
                for name in list(sys.modules):
                    if name == "fixture_events" or name.startswith("fixture_events."):
                        del sys.modules[name]

    @staticmethod
    def _expected_fixture_events(ids):
        """Set-up before the first test and tear-down after the last, per run of one module or class."""
        expected, module, cls = [], None, None
        for name in ids:
            next_module, next_class, _ = name.split(".")[-3:]
            if next_class != cls and cls is not None:
                expected.append(f"class_teardown:{cls}")
            if next_module != module:
                if module is not None:
                    expected.append(f"module_teardown:{module}")
                expected.append(f"module_setup:{next_module}")
            if next_class != cls:
                expected.append(f"class_setup:{next_class}")
            expected.append(name)
            module, cls = next_module, next_class
        if cls is not None:
            expected += [f"class_teardown:{cls}", f"module_teardown:{module}"]
        return expected

    def test_usage_errors_exit_2(self):
        for args in ([], ["0", "3"], ["4", "3"], ["x", "3"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(ROOT / "tools" / "test_shard.py"), *args],
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
