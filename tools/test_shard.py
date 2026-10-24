#!/usr/bin/env python3
# CI 테스트 분할 실행기 — tests/test_*.py 모듈을 크기로 나눠 N개 조각 중 하나만 unittest 로 돌린다.
"""Run one shard of the unit tests: `python tools/test_shard.py INDEX COUNT [unittest options]`.

Every `tests/test_*.py` module goes to exactly one shard. Modules are assigned greedily by file size,
largest first, to the shard with the smallest total so far, so shards stay balanced as the suite grows.
The assignment depends only on the module names and sizes, so every runner computes the same split.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def shards(count: int, tests_dir: pathlib.Path = ROOT / "tests") -> list[list[str]]:
    modules = sorted(tests_dir.glob("test_*.py"), key=lambda p: (-p.stat().st_size, p.name))
    groups: list[list[str]] = [[] for _ in range(count)]
    sizes = [0] * count
    for path in modules:
        index = min(range(count), key=lambda i: (sizes[i], i))
        groups[index].append(f"tests.{path.stem}")
        sizes[index] += path.stat().st_size
    return [sorted(group) for group in groups]


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[0].isdigit() or not argv[1].isdigit() or not 1 <= int(argv[0]) <= int(argv[1]):
        print("usage: python tools/test_shard.py INDEX COUNT [unittest options], with 1 <= INDEX <= COUNT",
              file=sys.stderr)
        return 2
    index, count = int(argv[0]), int(argv[1])
    names = shards(count)[index - 1]
    print(f"shard {index}/{count}: {len(names)} modules", flush=True)
    if not names:
        return 0
    sys.path.insert(0, str(ROOT))
    program = unittest.main(module=None, argv=["test_shard", *argv[2:], *names], exit=False)
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
