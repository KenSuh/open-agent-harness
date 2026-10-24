#!/usr/bin/env python3
# CI 테스트 분할 실행기 — unittest 탐색 결과를 나눠 N개 조각 중 하나만 돌린다.
"""Run one shard of the unit tests: `python tools/test_shard.py INDEX COUNT [unittest options]`.

Discovery suites go to exactly one shard, preserving nested packages and load_tests hooks.
Suites are assigned greedily by test count, largest first, to the smallest shard so far.
Ties follow discovery order, so every runner computes the same split.
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
    suites = sorted(discovered, key=lambda suite: -suite.countTestCases())
    groups: list[list[unittest.TestSuite]] = [[] for _ in range(count)]
    sizes = [0] * count
    for suite in suites:
        index = min(range(count), key=lambda i: (sizes[i], i))
        groups[index].append(suite)
        sizes[index] += suite.countTestCases()
    return groups


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
