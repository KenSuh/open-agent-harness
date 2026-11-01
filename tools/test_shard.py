#!/usr/bin/env python3
# CI 테스트 분할 실행기 — unittest 탐색 결과를 나눠 N개 조각 중 하나만 돌린다.
"""Run one shard of the unit tests: `python tools/test_shard.py INDEX COUNT [unittest options]`.

Discovery runs once, so nested packages and load_tests hooks shape the suite as usual. The suite is then cut into
units, each a single test method or a custom suite, which stays intact. A unit's weight is the sum of its tests'
measured times in integer milliseconds, read only from tools/test_shard_times.json, so no runner's clock, earlier
run or network changes the split and every runner computes the same one. A test the file does not list, a
unittest.loader._FailedTest or a test inside a custom suite included, weighs `default_ms`, the mean of the listed
values rounded down, and still runs exactly once. A listed id that is no longer discovered is ignored. A missing,
unreadable or malformed file stops the run with a message; it never falls back to counting tests.

Units are assigned greedily, heaviest first, to the lightest shard so far; ties follow discovery order and go to the
lowest shard. The methods of one class land apart, so no single class can outgrow a shard. Each shard runs its units
in discovery order, so a class's methods stay adjacent and unittest runs its module and class fixtures once in each
shard that holds their tests.

Refresh the file with `python tools/shard_times.py tools/test_shard_times.json LOG [LOG ...]` from saved GitHub job
logs. A slow shard is answered by this split and fresh data, not by a higher limit or more shards.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest
from collections.abc import Mapping

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIMES = ROOT / "tools" / "test_shard_times.json"


def shards(count: int, tests_dir: pathlib.Path = ROOT / "tests", loader: unittest.TestLoader | None = None,
           weights: tuple[int, Mapping[str, int]] | None = None) -> list[list[unittest.TestSuite]]:
    """Split discovery into `count` shards. `weights` is (default_ms, times_ms); without it the data file is read."""
    default_ms, times_ms = weights if weights is not None else load_times()
    loader = loader if loader is not None else unittest.TestLoader()
    discovered = loader.discover(str(tests_dir), top_level_dir=str(tests_dir.parent))
    units = list(_units(discovered))
    costs = [weight(unit, default_ms, times_ms) for unit in units]
    order = sorted(range(len(units)), key=lambda i: (-costs[i], i))
    placed: list[list[int]] = [[] for _ in range(count)]
    totals = [0] * count
    for unit in order:
        index = min(range(count), key=lambda i: (totals[i], i))
        placed[index].append(unit)
        totals[index] += costs[unit]
    return [[units[i] for i in sorted(group)] for group in placed]


def weight(test: unittest.TestCase | unittest.TestSuite, default_ms: int, times_ms: Mapping[str, int]) -> int:
    """A test's listed time, or `default_ms`; a suite weighs the sum of its tests."""
    if isinstance(test, unittest.TestCase):
        return times_ms.get(test.id(), default_ms)
    return sum(weight(child, default_ms, times_ms) for child in test)


def load_times(path: pathlib.Path | None = None) -> tuple[int, dict[str, int]]:
    """Return (default_ms, times_ms) from the data file; anything wrong with it ends the process."""
    path = path if path is not None else TIMES
    try:
        data = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=_unique_keys)
    except (OSError, ValueError) as error:
        raise SystemExit(f"test_shard: cannot read {path}: {error}") from None
    problem = _shape_problem(data)
    if problem is not None:
        raise SystemExit(f"test_shard: bad timing data in {path}: {problem}")
    return data["default_ms"], data["times_ms"]


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Refuse a repeated key: which value would win depends on the key order."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError(f"repeated key among {sorted(key for key in set(keys) if keys.count(key) > 1)}")
    return dict(pairs)


def _shape_problem(data: object) -> str | None:
    if not isinstance(data, dict) or set(data) != {"default_ms", "source", "times_ms"}:
        return "expected an object with exactly default_ms, source and times_ms"
    if not isinstance(data["source"], str) or not data["source"]:
        return "source must be a non-empty string"
    if not _positive_int(data["default_ms"]):
        return "default_ms must be a positive integer"
    if not isinstance(data["times_ms"], dict) or not data["times_ms"]:
        return "times_ms must be a non-empty object"
    for name, value in data["times_ms"].items():
        if not _positive_int(value):
            return f"times_ms[{name!r}] must be a positive integer"
    return None


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _units(suite: unittest.TestSuite):
    """Keep custom suites intact; split plain suites down to lone test cases."""
    if type(suite) is not unittest.TestSuite:
        yield suite
        return
    for child in suite:
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
