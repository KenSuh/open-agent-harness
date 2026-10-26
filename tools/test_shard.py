#!/usr/bin/env python3
# CI 테스트 분할 실행기 — unittest 탐색 결과를 나눠 N개 조각 중 하나만 돌린다.
"""Run one shard of the unit tests: `python tools/test_shard.py INDEX COUNT [unittest options]`.

Discovery runs once, so nested packages and load_tests hooks shape the suite as usual. The suite is then cut into
units, each a custom suite or the innermost plain suite holding only test cases (normally one TestCase class).
One large module can spread over several shards while class fixtures stay together. Custom suites stay intact.
Units are assigned greedily by test count, largest first, to the smallest shard so far; ties follow discovery order,
and each shard runs its units in discovery order,
so every runner computes the same split and a module's classes stay adjacent.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def shards(count: int, tests_dir: pathlib.Path = ROOT / "tests",
           loader: unittest.TestLoader | None = None) -> list[list[unittest.TestSuite]]:
    loader = loader if loader is not None else unittest.TestLoader()
    discovered = loader.discover(str(tests_dir), top_level_dir=str(tests_dir.parent))
    units = list(_units(discovered))
    order = sorted(range(len(units)), key=lambda i: (-units[i].countTestCases(), i))
    placed: list[list[int]] = [[] for _ in range(count)]
    sizes = [0] * count
    for unit in order:
        index = min(range(count), key=lambda i: (sizes[i], i))
        placed[index].append(unit)
        sizes[index] += units[unit].countTestCases()
    return [[units[i] for i in sorted(group)] for group in placed]


def _units(suite: unittest.TestSuite):
    """Keep custom suites intact; split plain suites down to case-only suites or lone test cases."""
    if type(suite) is not unittest.TestSuite:
        yield suite
        return
    children = list(suite)
    if children and all(isinstance(child, unittest.TestCase) for child in children):
        yield suite
        return
    for child in children:
        if isinstance(child, unittest.TestCase):
            yield unittest.TestSuite([child])
        else:
            yield from _units(child)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[0].isdigit() or not argv[1].isdigit() or not 1 <= int(argv[0]) <= int(argv[1]):
        print("usage: python tools/test_shard.py INDEX COUNT [unittest options], with 1 <= INDEX <= COUNT",
              file=sys.stderr)
        return 2
    index, count = int(argv[0]), int(argv[1])
    sys.path.insert(0, str(ROOT))

    class ShardProgram(unittest.TestProgram):
        def createTests(self, from_discovery=False, Loader=None):
            if self.testNamePatterns:
                self.testLoader.testNamePatterns = self.testNamePatterns
            self.test = unittest.TestSuite(shards(count, loader=self.testLoader)[index - 1])
            print(f"shard {index}/{count}: {self.test.countTestCases()} tests", flush=True)

    program = ShardProgram(module=None, argv=["test_shard", *argv[2:]],
                           testLoader=unittest.TestLoader(), exit=False)
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
