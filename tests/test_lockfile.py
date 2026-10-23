# 상태 잠금 검사 — 다른 프로세스가 잡은 잠금은 시간 초과로 거절되고, 잡은 프로세스가 죽으면 잠금이 풀리는지 본다.
from __future__ import annotations

import pathlib
import subprocess
import sys
import time
import unittest

from oah.lockfile import LockTimeout, StateLock
from tests.helpers import make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
HOLDER = (
    "import pathlib, sys, time\n"
    "sys.path.insert(0, sys.argv[3])\n"
    "from oah.lockfile import StateLock\n"
    "lock = StateLock(pathlib.Path(sys.argv[1]))\n"
    "lock.__enter__()\n"
    "pathlib.Path(sys.argv[2]).write_text('locked', encoding='utf-8')\n"
    "time.sleep(60)\n"
)


class StateLockTest(unittest.TestCase):
    def start_holder(self, lock_path: pathlib.Path) -> subprocess.Popen:
        ready = lock_path.parent / "holder-ready"
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_path), str(ready), SRC])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        deadline = time.monotonic() + 20
        while not ready.exists():
            self.assertIsNone(holder.poll(), "the lock holder exited early")
            self.assertLess(time.monotonic(), deadline, "the lock holder never took the lock")
            time.sleep(0.05)
        return holder

    def test_lock_is_reusable_in_one_process(self):
        lock_path = make_tmp_dir(self) / "state" / ".lock"
        with StateLock(lock_path):
            pass
        with StateLock(lock_path):
            pass
        self.assertTrue(lock_path.is_file())

    def test_lock_excludes_second_process(self):
        lock_path = make_tmp_dir(self) / "state" / ".lock"
        self.start_holder(lock_path)
        started = time.monotonic()
        with self.assertRaises(LockTimeout) as ctx:
            with StateLock(lock_path, timeout=0.5):
                pass
        self.assertEqual(ctx.exception.code, "OAH_STATE_LOCK_TIMEOUT")
        self.assertLess(time.monotonic() - started, 5)

    def test_lock_is_released_when_holder_process_dies(self):
        lock_path = make_tmp_dir(self) / "state" / ".lock"
        holder = self.start_holder(lock_path)
        holder.kill()
        holder.wait()
        with StateLock(lock_path, timeout=5):
            pass


if __name__ == "__main__":
    unittest.main()
