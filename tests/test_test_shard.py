# CI 테스트 분할 검사 — 모든 테스트 모듈이 정확히 한 조각에 들어가고, 조각 크기가 고르게 나뉘는지 확인한다.
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("test_shard_tool", ROOT / "tools" / "test_shard.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class ShardTest(unittest.TestCase):
    def test_every_module_runs_in_exactly_one_shard(self):
        expected = sorted(f"tests.{p.stem}" for p in (ROOT / "tests").glob("test_*.py"))
        for count in (1, 2, 3, 5):
            with self.subTest(count=count):
                groups = tool.shards(count)
                self.assertEqual(len(groups), count)
                flat = [name for group in groups for name in group]
                self.assertEqual(sorted(flat), expected)
                self.assertEqual(len(flat), len(set(flat)))

    def test_split_is_balanced_by_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            for name, size in (("a", 900), ("b", 500), ("c", 400), ("d", 300), ("e", 200)):
                (folder / f"test_{name}.py").write_bytes(b"#" * size)
            groups = tool.shards(2, folder)
            totals = [sum((folder / f"{n.split('.')[1]}.py").stat().st_size for n in g) for g in groups]
            self.assertEqual(sorted(totals), [1100, 1200])

    def test_usage_errors_exit_2(self):
        for args in ([], ["0", "3"], ["4", "3"], ["x", "3"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(ROOT / "tools" / "test_shard.py"), *args],
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
