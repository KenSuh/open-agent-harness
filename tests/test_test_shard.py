# CI 테스트 분할 검사 — 탐색한 테스트가 정확히 한 조각에 들어가고 고르게 나뉘는지 확인한다.
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import re
import shutil
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


def per_shard(groups):
    return [ShardTest._test_ids(group) for group in groups]


def plain_module(count):
    source = "# 가중치 분할 검사 모듈.\nimport unittest\nclass Test(unittest.TestCase):\n"
    return source + "".join(f"    def test_{i}(self): pass\n" for i in range(count))


WRAPPED_MODULE = """
    # 사용자 정의 묶음으로 테스트를 감싸는 모듈.
    import unittest
    class Wrapper(unittest.TestSuite):
        pass
    class Case(unittest.TestCase):
        def test_0(self): pass
        def test_1(self): pass
        def test_2(self): pass
    def load_tests(loader, tests, pattern):
        return Wrapper(tests)
"""


@contextlib.contextmanager
def fixture_package(name, files):
    """A throwaway test package. `name` is its import name, so every test uses its own."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = pathlib.Path(tmp) / name
        for relative, source in {"__init__.py": "# 가중치 분할 검사 패키지.\n", **files}.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        old_path = sys.path[:]
        try:
            yield folder
        finally:
            sys.path[:] = old_path
            for module in list(sys.modules):
                if module == name or module.startswith(f"{name}."):
                    del sys.modules[module]


class WeightedSplitTest(unittest.TestCase):
    def test_listed_and_unlisted_tests_cover_discovery_exactly_once(self):
        with fixture_package("cover_fixture", {
                "test_plain.py": plain_module(7),
                "test_wrapped.py": WRAPPED_MODULE,
                "nested/__init__.py": "# 중첩 패키지.\n",
                "nested/test_deep.py": plain_module(2),
                "testhook.py": """
                    # 기본 패턴의 모듈 훅.
                    import unittest
                    class HookTest(unittest.TestCase):
                        def test_hook(self): pass
                    def load_tests(loader, tests, pattern):
                        return tests
                """}) as folder:
            expected = sorted(ShardTest._test_ids(unittest.TestLoader().discover(
                str(folder), top_level_dir=str(folder.parent))))
            self.assertEqual(len(expected), 13)
            listed = {name: 10 + 7 * i for i, name in enumerate(expected[::2])}
            for count in (1, 2, 3, 5):
                with self.subTest(count=count):
                    flat = [name for ids in per_shard(tool.shards(count, folder, weights=(40, listed)))
                            for name in ids]
                    self.assertEqual(sorted(flat), expected)

    def test_real_tests_folder_at_five_shards_is_covered_by_discovery_only(self):
        expected = sorted(ShardTest._test_ids(unittest.TestLoader().discover(
            str(ROOT / "tests"), top_level_dir=str(ROOT))))
        listed = {name: 1 + i % 97 for i, name in enumerate(expected[::2])}
        groups = per_shard(tool.shards(5, weights=(50, listed)))
        self.assertEqual(sorted(name for ids in groups for name in ids), expected)
        self.assertTrue(all(groups))

    def test_two_computations_match_and_key_order_does_not_matter(self):
        with fixture_package("order_fixture", {"test_a.py": plain_module(9), "test_b.py": plain_module(6)}) as folder:
            names = sorted(ShardTest._test_ids(unittest.TestLoader().discover(
                str(folder), top_level_dir=str(folder.parent))))
            listed = {name: 1 + (i * 37) % 11 for i, name in enumerate(names[:12])}
            backwards = dict(reversed(list(listed.items())))
            self.assertNotEqual(list(listed), list(backwards))
            first = per_shard(tool.shards(3, folder, weights=(6, listed)))
            self.assertEqual(first, per_shard(tool.shards(3, folder, weights=(6, listed))))
            self.assertEqual(first, per_shard(tool.shards(3, folder, weights=(6, backwards))))
            with tempfile.TemporaryDirectory() as tmp:
                data = pathlib.Path(tmp) / "times.json"
                data.write_text(json.dumps({"times_ms": backwards, "source": "fixture", "default_ms": 6}),
                                encoding="utf-8")
                self.assertEqual(first, per_shard(tool.shards(3, folder, weights=tool.load_times(data))))

    def test_measured_time_decides_where_count_balance_would_differ(self):
        with fixture_package("weight_fixture", {"test_m.py": plain_module(4)}) as folder:
            name = "weight_fixture.test_m.Test.test_{}".format
            listed = {name(0): 1000, name(1): 1, name(2): 1, name(3): 1}
            self.assertEqual(per_shard(tool.shards(2, folder, weights=(1, listed))),
                             [[name(0)], [name(1), name(2), name(3)]])

    def test_heaviest_first_ties_by_discovery_then_lowest_shard(self):
        with fixture_package("greedy_fixture", {"test_m.py": plain_module(4)}) as folder:
            name = "greedy_fixture.test_m.Test.test_{}".format
            listed = {name(0): 5, name(1): 9, name(2): 5, name(3): 2}
            self.assertEqual(per_shard(tool.shards(2, folder, weights=(1, listed))),
                             [[name(1), name(3)], [name(0), name(2)]])
            even = {name(i): 3 for i in range(4)}
            self.assertEqual(per_shard(tool.shards(3, folder, weights=(1, even))),
                             [[name(0), name(3)], [name(1)], [name(2)]])

    def test_unlisted_tests_weigh_default_ms_and_stale_ids_change_nothing(self):
        with fixture_package("default_fixture", {"test_m.py": plain_module(4)}) as folder:
            name = "default_fixture.test_m.Test.test_{}".format
            self.assertEqual(per_shard(tool.shards(2, folder, weights=(50, {name(0): 100}))),
                             [[name(0), name(3)], [name(1), name(2)]])
            self.assertEqual(per_shard(tool.shards(2, folder, weights=(1, {name(0): 100}))),
                             [[name(0)], [name(1), name(2), name(3)]])
            stale = {name(0): 100, "default_fixture.test_gone.Test.test_gone": 10 ** 9}
            self.assertEqual(per_shard(tool.shards(2, folder, weights=(50, stale))),
                             per_shard(tool.shards(2, folder, weights=(50, {name(0): 100}))))

    def test_failed_import_is_an_unlisted_test_that_runs_once(self):
        with fixture_package("broken_fixture", {
                "test_broken.py": "# 불러오기가 실패하는 모듈.\nraise ImportError('broken on purpose')\n",
                "test_m.py": plain_module(2)}) as folder:
            failed = "unittest.loader._FailedTest.broken_fixture.test_broken"
            ok = "broken_fixture.test_m.Test.test_{}".format
            groups = per_shard(tool.shards(2, folder, weights=(100, {ok(0): 1, ok(1): 1})))
            self.assertEqual(groups, [[failed], [ok(0), ok(1)]])

    def test_custom_suite_stays_intact_and_weighs_the_sum_of_its_tests(self):
        with fixture_package("suite_fixture", {"test_a_wrapped.py": WRAPPED_MODULE,
                                               "test_m.py": plain_module(3)}) as folder:
            case = "suite_fixture.test_a_wrapped.Case.test_{}".format
            plain = "suite_fixture.test_m.Test.test_{}".format
            wrapped = [case(0), case(1), case(2)]
            # The suite weighs 6, so it goes first. Its size (3), first test (1) or heaviest test (3) lands elsewhere.
            listed = {case(0): 1, case(1): 2, case(2): 3, plain(0): 5, plain(1): 4, plain(2): 3}
            expected = [wrapped + [plain(2)], [plain(0), plain(1)]]
            self.assertEqual(per_shard(tool.shards(2, folder, weights=(1, listed))), expected)
            # An unlisted member of the suite counts as default_ms in that sum.
            del listed[case(2)]
            self.assertEqual(per_shard(tool.shards(2, folder, weights=(3, listed))), expected)
            for count in (1, 2, 3, 5):
                with self.subTest(count=count):
                    groups = per_shard(tool.shards(count, folder, weights=(3, listed)))
                    self.assertEqual(sum(bool(set(wrapped) & set(ids)) for ids in groups), 1)

    def test_a_caller_with_its_own_weights_does_not_touch_the_file(self):
        with fixture_package("own_fixture", {"test_m.py": plain_module(2)}) as folder, \
                tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tool, "TIMES", pathlib.Path(tmp) / "missing.json"):
            self.assertEqual(len(tool.shards(2, folder, weights=(1, {}))), 2)
            with self.assertRaises(SystemExit):
                tool.shards(2, folder)


class BadTimesTest(unittest.TestCase):
    GOOD = {"default_ms": 5, "source": "fixture", "times_ms": {"tests.test_one.Test.test_one": 5}}

    def run_copy(self, content):
        """Run a copy of the tool on a one-test tree with `content` as its data file (None: no file)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "tools").mkdir()
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("# 복사본 패키지.\n", encoding="utf-8")
            (root / "tests" / "test_one.py").write_text(
                "# 복사본 테스트.\nimport unittest\nclass Test(unittest.TestCase):\n    def test_one(self): pass\n",
                encoding="utf-8")
            shutil.copy(ROOT / "tools" / "test_shard.py", root / "tools" / "test_shard.py")
            if content is not None:
                (root / "tools" / "test_shard_times.json").write_bytes(content)
            return subprocess.run([sys.executable, str(root / "tools" / "test_shard.py"), "1", "2"],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_good_file_runs_the_shard(self):
        result = self.run_copy(json.dumps(self.GOOD).encode("utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shard 1/2: 1 tests", result.stdout)

    def test_each_bad_form_stops_with_a_message(self):
        def changed(**fields):
            return json.dumps({**self.GOOD, **fields}).encode("utf-8")

        def without(key):
            return json.dumps({k: v for k, v in self.GOOD.items() if k != key}).encode("utf-8")

        name = next(iter(self.GOOD["times_ms"]))
        cases = {
            "missing": None,
            "not utf-8": b'{"default_ms": 5, "source": "\xff", "times_ms": {}}',
            "not json": b"{not json",
            "empty": b"",
            "top level list": b"[]",
            "no default_ms": without("default_ms"),
            "no source": without("source"),
            "no times_ms": without("times_ms"),
            "extra key": changed(extra=1),
            "source not a string": changed(source=3),
            "empty source": changed(source=""),
            "times_ms a list": changed(times_ms=[name]),
            "times_ms empty, a silent count split": changed(times_ms={}),
            "default zero": changed(default_ms=0),
            "default negative": changed(default_ms=-1),
            "default float": changed(default_ms=1.5),
            "default bool": changed(default_ms=True),
            "default string": changed(default_ms="5"),
            "value zero": changed(times_ms={name: 0}),
            "value negative": changed(times_ms={name: -3}),
            "value float": changed(times_ms={name: 1.5}),
            "value whole float": changed(times_ms={name: 2.0}),
            "value bool": changed(times_ms={name: True}),
            "value string": changed(times_ms={name: "7"}),
            "value null": changed(times_ms={name: None}),
            "value NaN": changed(times_ms={name: float("nan")}),
            "duplicate key": (b'{"default_ms": 5, "source": "fixture", "times_ms": '
                              b'{"a.b": 1, "a.b": 2}}'),
        }
        for label, content in cases.items():
            with self.subTest(label):
                result = self.run_copy(content)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("test_shard_times.json", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("shard 1/2", result.stdout)

    def test_unreadable_path_stops_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as caught:
            tool.load_times(pathlib.Path(tmp))
        self.assertIn(pathlib.Path(tmp).name, str(caught.exception.code))


def text_of(path):
    """Decode a checked-out text file; Windows checkouts turn LF into CRLF (`* text=auto`)."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


class CheckedInTimesTest(unittest.TestCase):
    def test_data_file_is_well_formed(self):
        text = text_of(ROOT / "tools" / "test_shard_times.json")
        data = json.loads(text)
        self.assertEqual(sorted(data), ["default_ms", "source", "times_ms"])
        self.assertEqual(text, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        self.assertEqual(list(data["times_ms"]), sorted(data["times_ms"]))
        self.assertTrue(data["times_ms"])
        for name, value in data["times_ms"].items():
            self.assertRegex(name, r"\Atests(\.\w+)+\Z")
            self.assertIs(type(value), int)
            self.assertGreater(value, 0)
        self.assertEqual(data["default_ms"], sum(data["times_ms"].values()) // len(data["times_ms"]))
        self.assertNotRegex(data["source"], r"[/\\\n]")
        self.assertEqual(tool.load_times(), (data["default_ms"], data["times_ms"]))

    def test_ci_keeps_five_shards_and_the_limit_and_describes_the_basis(self):
        text = text_of(ROOT / ".github" / "workflows" / "ci.yml")
        self.assertRegex(text, r"(?m)^ +shard: \[1, 2, 3, 4, 5\]$")
        self.assertRegex(text, r"(?m)^ +timeout-minutes: 45$")
        self.assertIn("run: python tools/test_shard.py ${{ matrix.shard }} 5 -v", text)
        self.assertNotIn("balanced by test count", text)
        self.assertIn("tools/test_shard_times.json", text)
        self.assertIn("tools/shard_times.py", text)
        self.assertIn("not by a higher limit or more shards", text)


def log_line(clock, text=""):
    return f"test (windows-latest, 1/5)\tUNKNOWN STEP\t2026-11-01T10:{clock}Z {text}"


def result_id(letter):
    return f"tests.test_m.Case.test_{letter}"


def name_of(letter):
    return f"test_{letter} ({result_id(letter)})"


# A cancelled job: a late lone result, a skip, subtests, a docstring line and a test that never ends.
CANCELLED_LOG = "\n".join([
    "test (windows-latest, 1/5)\tUNKNOWN STEP\t﻿2026-11-01T10:00:01.0000000Z Current runner version",
    log_line("00:09.0000000", f"{name_of('early')} ... ok"),
    log_line("00:10.0000000", "shard 1/5: 9 tests"),
    log_line("00:12.5000000", f"{name_of('a')} ... ok"),
    log_line("00:13.0000000", f"{name_of('b')} ... warning: in the working copy of 'a.md', LF will be replaced"),
    log_line("00:14.0000000", "warning: in the working copy of 'b.md', LF will be replaced"),
    log_line("00:15.0000000", "ok"),
    log_line("00:16.0000000", f"{name_of('c')} ... skipped 'node is missing'"),
    log_line("00:17.0000000", f"{name_of('d')} ... "),
    log_line("00:18.0000000", f"  {name_of('d')} [first] ... skipped 'not here'"),
    log_line("00:21.0000000", name_of("e")),
    log_line("00:22.0000000", "A docstring line ... FAIL"),
    log_line("00:22.0000000", f"{name_of('same')} ... ok"),
    log_line("00:23.0000000", "setUpClass (tests.test_m.Other) ... ERROR"),
    log_line("00:30.0000000", f"{name_of('last')} ... Traceback (most recent call last):"),
    log_line("00:30.5000000", "KeyboardInterrupt"),
    log_line("00:31.0000000", "##[error]The operation was canceled."),
    log_line("00:32.0000000", "ok"),
    "",
])

FINISHED_LOG = "\n".join([
    log_line("10:00.0000000", "shard 2/5: 2 tests"),
    log_line("10:04.0000000", f"{name_of('a')} ... ok"),
    log_line("10:05.0000000", f"{name_of('b')} ... warning: a line"),
    log_line("10:05.5000000", "ERROR"),
    log_line("10:05.5000000", ""),
    log_line("10:05.5000000", "Ran 2 tests in 5.500s"),
    log_line("10:06.0000000", "ok"),
    log_line("10:07.0000000", f"{name_of('after')} ... ok"),
    "",
])


class GeneratorTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("shard_times_tool", ROOT / "tools" / "shard_times.py")
        self.generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.generator)

    def test_each_result_shape_ends_its_test(self):
        times, counts = self.generator.parse_log(CANCELLED_LOG)
        self.assertEqual(times, {result_id("a"): 2500, result_id("b"): 2500, result_id("c"): 1000,
                                 result_id("same"): 1})
        self.assertEqual(counts["unfinished"], 2)
        self.assertEqual(counts["unconfirmed start"], 1)
        self.assertEqual(counts["never reached"], 2)
        self.assertEqual(counts["subtest lines"], 1)

    def subtest_log(self, *, following_test=True, cancel=False):
        """Timestamp real verbose unittest output without sleeping for the simulated work."""
        seconds = 0
        lines = [log_line("00:00.0000000", f"shard 1/5: {3 if following_test else 1} tests")]

        class LogStream(io.StringIO):
            pending = ""

            def write(self, text):
                self.pending += text
                while "\n" in self.pending:
                    line, self.pending = self.pending.split("\n", 1)
                    lines.append(log_line(f"{seconds // 60:02}:{seconds % 60:02}.0000000", line))
                return len(text)

        stream = LogStream()

        def cleanup():
            nonlocal seconds
            seconds += 3

        class Case(unittest.TestCase):
            def __str__(self):
                return name_of(self._testMethodName.removeprefix("test_"))

            def test_a(self):
                nonlocal seconds
                seconds += 2
                with self.subTest(x=1):
                    self.skipTest("no")
                with self.subTest(x=2):
                    if cancel:
                        seconds += 30
                        raise KeyboardInterrupt
                    seconds += 60
                self.addCleanup(cleanup)

            def test_b(self):
                nonlocal seconds
                seconds += 4

            def test_c(self):
                nonlocal seconds
                seconds += 7

        suite = unittest.TestSuite([Case("test_a")] + ([Case("test_b"), Case("test_c")] if following_test else []))
        try:
            unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        except KeyboardInterrupt:
            lines.append(log_line(f"{seconds // 60:02}:{seconds % 60:02}.0000000",
                                  "##[error]The operation was canceled."))
        return "\n".join(lines)

    def test_subtest_only_parent_and_quiet_successor_get_no_value(self):
        for following_test in (True, False):
            with self.subTest(following_test=following_test):
                text = self.subtest_log(following_test=following_test)
                self.assertIn(f"  {name_of('a')} (x=1) ... skipped 'no'", text)
                self.assertNotIn(f"{name_of('a')} ... ok", text)
                times, counts = self.generator.parse_log(text)
                self.assertNotIn(result_id("a"), times)
                self.assertNotIn(result_id("b"), times)
                self.assertEqual(counts["unfinished"], 1)
                self.assertEqual(counts["unconfirmed start"], int(following_test))
                self.assertEqual(counts["subtest lines"], 1)

    def test_top_level_result_reanchors_after_an_unconfirmed_start(self):
        text = self.subtest_log()
        self.assertIn(log_line("01:09.0000000", f"{name_of('b')} ... ok"), text)
        times, counts = self.generator.parse_log(text)
        self.assertEqual(times, {result_id("c"): 7000})
        self.assertEqual(counts["unconfirmed start"], 1)

    def test_cancelled_or_truncated_parent_after_skipped_subtest_is_unfinished(self):
        text = self.subtest_log(cancel=True)
        for log in (text, text.rsplit("\n", 1)[0]):
            with self.subTest(cancel_marker="##[error]" in log):
                times, counts = self.generator.parse_log(log)
                self.assertEqual(times, {})
                self.assertEqual(counts["unfinished"], 1)
                self.assertEqual(counts["unconfirmed start"], 0)
                self.assertEqual(counts["never reached"], 2)
                self.assertEqual(counts["subtest lines"], 1)

    def test_the_end_of_the_run_stops_the_log(self):
        times, counts = self.generator.parse_log(FINISHED_LOG)
        self.assertEqual(times, {result_id("a"): 4000, result_id("b"): 1500})
        self.assertEqual(counts["unfinished"], 0)
        self.assertEqual(counts["never reached"], 0)

    def test_a_log_without_a_shard_line_is_refused(self):
        with self.assertRaises(ValueError):
            self.generator.parse_log(log_line("00:00.0000000", f"{name_of('a')} ... ok"))

    def test_median_is_an_integer_of_at_least_one(self):
        self.assertEqual(self.generator.median_ms([7]), 7)
        self.assertEqual(self.generator.median_ms([9, 1, 5]), 5)
        self.assertEqual(self.generator.median_ms([2, 5]), 3)
        self.assertEqual(self.generator.median_ms([0]), 1)

    def write_logs(self, folder):
        short = "\n".join([log_line("00:00.0000000", "shard 3/5: 1 tests"),
                           log_line("00:02.0000000", f"{name_of('a')} ... ok")]) + "\n"
        logs = []
        for name, text in (("run1-w1.log", CANCELLED_LOG), ("run1-w2.log", FINISHED_LOG), ("run2-w1.log", short)):
            logs.append(folder / name)
            logs[-1].write_bytes(text.encode("utf-8"))
        return logs

    def run_generator(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "shard_times.py"), *map(str, args)],
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_cli_writes_the_median_across_logs_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            logs = self.write_logs(folder)
            out = folder / "out.json"
            result = self.run_generator(out, *logs)
            self.assertEqual(result.returncode, 0, result.stderr)
            written = out.read_bytes()
            self.assertNotIn(b"\r", written)
            data = json.loads(written)
            self.assertEqual(data["times_ms"], {
                result_id("a"): 2500, result_id("b"): 2000, result_id("c"): 1000, result_id("same"): 1})
            self.assertEqual(data["default_ms"], 5501 // 4)
            self.assertEqual(written.decode("utf-8"),
                             json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            self.assertIn("run1", data["source"])
            self.assertIn("run2", data["source"])
            self.assertNotIn("-w1", data["source"])
            self.assertNotIn(folder.name, data["source"])
            self.assertIn("4 ids from 3 logs", result.stdout)
            self.assertIn("log entries measured: 7", result.stdout)
            self.assertIn("2 unfinished", result.stdout)
            self.assertIn("1 dropped (unconfirmed start)", result.stdout)
            self.assertIn("2 never reached", result.stdout)
            self.assertIn("1 subtest lines", result.stdout)

            weights = tool.load_times(out)
            totals = [sum(weights[1].get(name, weights[0]) for name in ids)
                      for ids in per_shard(tool.shards(5, weights=weights))]
            minutes = " ".join(f"{total / 60000:.1f}" for total in totals)
            self.assertIn(f"predicted minutes for COUNT=5: {minutes}; ", result.stdout)
            self.assertIn(f"total {sum(totals) / 60000:.1f}", result.stdout)

            again = folder / "again.json"
            self.assertEqual(self.run_generator(again, *reversed(logs)).returncode, 0)
            self.assertEqual(again.read_bytes(), written)

    def test_cli_refuses_bad_input_and_writes_nothing(self):
        self.assertEqual(self.run_generator().returncode, 2)
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            out = folder / "out.json"
            empty = folder / "empty.log"
            empty.write_text("nothing here\n", encoding="utf-8")
            for log in (empty, folder / "missing.log"):
                with self.subTest(log=log.name):
                    result = self.run_generator(out, log)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(log.name, result.stderr)
                    self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
