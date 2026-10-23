# 트랜잭션 복구 검사 — 리스·이벤트 게시의 각 실패 경계와 실제 프로세스 중단, 읽기 전용 보류 보고를 확인한다.
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

from oah.atomic import StateCorrupt, append_jsonl
from oah.lease import Store
from oah.paths import RepoInfo
from tests.helpers import git_commit, make_git_repo, make_tmp_dir
from tests.test_lease_cli import oah

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
T0 = dt.datetime(2026, 10, 23, 9, tzinfo=dt.timezone.utc)


class JournalTest(unittest.TestCase):
    def test_every_mutation_recovers_at_each_publication_boundary(self):
        from oah.journal import PendingTransaction

        for operation in ("start", "renew", "finish", "release", "prune", "log"):
            for boundary in ("journal", "state", "append", "cleanup"):
                for after in (False, True):
                    with self.subTest(operation=operation, boundary=boundary, after=after):
                        root = make_tmp_dir(self)
                        state = root / "state"
                        now = T0
                        store = Store(state, now=lambda: now)
                        info = RepoInfo(root, root / ".git", "main", None)

                        def start(task="t1"):
                            return store.start(task=task, agent="claude", scope=("src",), note="", ttl_hours=1,
                                               info=info)

                        lease = start() if operation in ("renew", "finish", "release", "prune") else None
                        before_events = ["lease_started"] if lease else []
                        if operation == "prune":
                            now += dt.timedelta(hours=2)
                        calls = {
                            "start": start,
                            "renew": lambda: store.renew(task="t1", agent="claude", ttl_hours=3),
                            "finish": lambda: store.finish(task="t1", agent="claude", lease_id=lease.id,
                                                            summary="done", changed=[], allow_out_of_scope=False),
                            "release": lambda: store.release(task="t1", agent="claude", reason="aborted", force=False),
                            "prune": lambda: start("t2"),
                            "log": lambda: store.log(task="t1", agent="claude", message="original"),
                        }
                        real_replace, real_unlink = os.replace, pathlib.Path.unlink
                        failed = False

                        def fail_once(real, *args, **kwargs):
                            nonlocal failed
                            if failed:
                                return real(*args, **kwargs)
                            failed = True
                            if after:
                                real(*args, **kwargs)
                            raise OSError("injected publication failure")

                        def replace(src, dst):
                            target = "transaction.json" if boundary == "journal" else "leases.json"
                            if boundary in ("journal", "state") and pathlib.Path(dst).name == target:
                                return fail_once(real_replace, src, dst)
                            return real_replace(src, dst)

                        def append(path, record):
                            if boundary == "append":
                                return fail_once(append_jsonl, path, record)
                            return append_jsonl(path, record)

                        def unlink(path, *args, **kwargs):
                            if boundary == "cleanup" and path == state / "transaction.json":
                                return fail_once(real_unlink, path, *args, **kwargs)
                            return real_unlink(path, *args, **kwargs)

                        with patch("oah.atomic.os.replace", side_effect=replace), \
                                patch("oah.journal.append_jsonl", side_effect=append), \
                                patch("pathlib.Path.unlink", autospec=True, side_effect=unlink):
                            with self.assertRaises(OSError):
                                calls[operation]()
                        self.assertTrue(failed)
                        pending = not (boundary == "journal" and not after or boundary == "cleanup" and after)
                        before_read = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in state.iterdir()}
                        if pending:
                            with self.assertRaises(PendingTransaction):
                                Store(state, now=lambda: now).snapshot()
                        else:
                            Store(state, now=lambda: now).snapshot()
                        self.assertEqual(before_read,
                                         {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in state.iterdir()})

                        recovered = Store(state, now=lambda: now)
                        recovered.log(task="marker", agent="claude", message="recover")
                        events = [json.loads(line) for line in (state / "events.jsonl").read_text(
                            encoding="utf-8").splitlines()]
                        landed = boundary != "journal" or after
                        event_name = {"start": "lease_started", "renew": "lease_renewed", "finish": "lease_finished",
                                      "release": "lease_released", "prune": "lease_expired", "log": "note"}[operation]
                        self.assertEqual([e["event"] for e in events],
                                         before_events + ([event_name] if landed else []) + ["note"])
                        ids = [e["transaction_id"] for e in events]
                        self.assertEqual(len(ids), len(set(ids)))
                        expected_tasks = ["t1"] if (operation in ("start", "renew") and landed or
                                                   operation in ("renew", "finish", "release", "prune") and not landed) else []
                        self.assertEqual([l.task for l, _ in recovered.snapshot()], expected_tasks)
                        if operation == "renew" and landed:
                            self.assertEqual(recovered.snapshot()[0][0].expires_at, "2026-10-23T12:00:00Z")
                        self.assertFalse((state / "transaction.json").exists())

    def test_crashed_process_leaves_replayable_transaction(self):
        from oah.journal import PendingTransaction

        state = make_tmp_dir(self) / "state"
        script = (
            "import os, pathlib, sys\n"
            "from unittest.mock import patch\n"
            "from oah.lease import Store\n"
            "from oah.paths import RepoInfo\n"
            "state = pathlib.Path(sys.argv[1])\n"
            "info = RepoInfo(state.parent, state.parent / '.git', 'main', None)\n"
            "with patch('oah.journal.append_jsonl', side_effect=lambda *a: os._exit(23)):\n"
            "    Store(state).start(task='t1', agent='claude', scope=('src',), note='', ttl_hours=1, info=info)\n"
        )
        result = subprocess.run([sys.executable, "-c", script, str(state)], env=dict(os.environ, PYTHONPATH=SRC),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 23, result.stderr)
        with self.assertRaises(PendingTransaction):
            Store(state).snapshot()
        Store(state).log(task="t1", agent="claude", message="recovered after process exit")
        events = [json.loads(line) for line in (state / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events], ["lease_started", "note"])
        self.assertEqual(len(Store(state).snapshot()), 1)

    def test_torn_append_blocks_replay_without_discarding_evidence(self):
        state = make_tmp_dir(self) / "state"
        store = Store(state)
        info = RepoInfo(state.parent, state.parent / ".git", "main", None)
        real_open = open

        class TornAppend:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def write(self, data):
                self.handle.write(data[:len(data) // 2])
                self.handle.flush()
                os.fsync(self.handle.fileno())
                raise OSError("interrupted event write")

        def open_file(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            return TornAppend(handle) if pathlib.Path(path) == store.events_path and "a" in mode else handle

        with patch("builtins.open", side_effect=open_file):
            with self.assertRaises(OSError):
                store.start(task="t1", agent="claude", scope=("src",), note="", ttl_hours=1, info=info)
        before = {p.name: p.read_bytes() for p in state.iterdir()}
        self.assertFalse(before["events.jsonl"].endswith(b"\n"))
        with self.assertRaises(StateCorrupt) as caught:
            Store(state).log(task="t1", agent="claude", message="must not concatenate")
        self.assertIn(str(store.events_path), str(caught.exception))
        self.assertEqual(before, {p.name: p.read_bytes() for p in state.iterdir()})

    def test_status_reports_pending_without_writing_and_finish_recovers(self):
        repo = make_git_repo(self)
        git_commit(repo, {"src/a.py": "a = 1\n"})
        state = repo / ".git" / "oah"
        started = oah(repo, "start", "--task", "t1", "--agent", "claude", "--scope", "src")
        self.assertEqual(started.returncode, 0, started.stderr)
        with patch("oah.journal.append_jsonl", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                Store(state).renew(task="t1", agent="claude", ttl_hours=1)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in state.iterdir()}
        for args in (("status",), ("status", "--json")):
            result = oah(repo, *args)
            self.assertEqual(result.returncode, 69, result.stderr)
            self.assertTrue(result.stderr.startswith("OAH_STATE_PENDING"), result.stderr)
            self.assertIn(str(state / "transaction.json"), result.stderr)
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in state.iterdir()})
        result = oah(repo, "finish", "--task", "t1", "--agent", "claude", "--summary", "recovered")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in (state / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events], ["lease_started", "lease_renewed", "lease_finished"])


if __name__ == "__main__":
    unittest.main()
