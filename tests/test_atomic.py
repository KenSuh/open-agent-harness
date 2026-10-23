# 원자적 상태 쓰기 검사 — 손상 파일 보고, 임시 파일 정리, PermissionError 재시도, 동시 읽기 중 깨진 JSON 없음, 한 줄 한 이벤트.
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from oah.atomic import StateCorrupt, append_jsonl, read_json, write_json_atomic
from tests.helpers import make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
WRITER = (
    "import pathlib, sys\n"
    "sys.path.insert(0, sys.argv[3])\n"
    "from oah.atomic import write_json_atomic\n"
    "target = pathlib.Path(sys.argv[1])\n"
    "for n in range(int(sys.argv[2])):\n"
    "    write_json_atomic(target, {'n': n, 'pad': 'x' * 4000})\n"
)


class AtomicTest(unittest.TestCase):
    def test_round_trip_and_trailing_newline(self):
        path = make_tmp_dir(self) / "state" / "leases.json"
        write_json_atomic(path, {"b": 1, "a": "한글"})
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(read_json(path, None), {"a": "한글", "b": 1})

    def test_missing_file_gives_default(self):
        self.assertEqual(read_json(make_tmp_dir(self) / "absent.json", {"leases": []}), {"leases": []})

    def test_corrupt_file_raises_state_corrupt(self):
        path = make_tmp_dir(self) / "leases.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StateCorrupt) as ctx:
            read_json(path, {})
        self.assertEqual(ctx.exception.code, "OAH_STATE_CORRUPT")
        self.assertIn(str(path), str(ctx.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_invalid_utf8_is_reported_as_corrupt(self):
        path = make_tmp_dir(self) / "leases.json"
        path.write_bytes(b'{"a": "\xff"}')
        with self.assertRaises(StateCorrupt):
            read_json(path, {})

    def test_temp_files_are_cleaned_after_failure(self):
        folder = make_tmp_dir(self)
        with patch("oah.atomic.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                write_json_atomic(folder / "leases.json", {"a": 1})
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [])

    def test_permission_error_is_retried(self):
        path = make_tmp_dir(self) / "leases.json"
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError("held by another process")
            return real_replace(src, dst)

        with patch("oah.atomic.os.replace", side_effect=flaky):
            write_json_atomic(path, {"a": 1})
        self.assertEqual(len(calls), 3)
        self.assertEqual(read_json(path, None), {"a": 1})

    def test_permission_error_gives_up_after_deadline(self):
        folder = make_tmp_dir(self)
        started = time.monotonic()
        with patch("oah.atomic.os.replace", side_effect=PermissionError("held")):
            with self.assertRaises(PermissionError):
                write_json_atomic(folder / "leases.json", {"a": 1}, retry_seconds=0.3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(list(folder.iterdir()), [])

    def test_concurrent_reader_never_sees_partial_json(self):
        path = make_tmp_dir(self) / "leases.json"
        writes = 300
        writer = subprocess.Popen([sys.executable, "-c", WRITER, str(path), str(writes), SRC])
        self.addCleanup(lambda: (writer.kill(), writer.wait()))
        last, reads = -1, 0
        while writer.poll() is None or reads == 0:
            value = read_json(path, None)
            reads += 1
            if value is not None:
                self.assertGreaterEqual(value["n"], last)
                last = value["n"]
        self.assertEqual(writer.wait(), 0)
        self.assertEqual(read_json(path, None)["n"], writes - 1)
        self.assertGreater(reads, 0)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_replace_retries_while_reader_holds_file(self):
        path = make_tmp_dir(self) / "leases.json"
        write_json_atomic(path, {"n": 0})
        handle = open(path, "rb")
        timer = threading.Timer(0.5, handle.close)
        timer.start()
        self.addCleanup(timer.cancel)
        write_json_atomic(path, {"n": 1})
        self.assertEqual(read_json(path, None), {"n": 1})

    def test_append_jsonl_one_object_per_line(self):
        path = make_tmp_dir(self) / "events.jsonl"
        append_jsonl(path, {"event": "lease_started", "task": "t1"})
        append_jsonl(path, {"event": "note", "message": "줄\n바꿈"})
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["message"], "줄\n바꿈")


if __name__ == "__main__":
    unittest.main()
