# 리스 저장소 검사 — 시작·겹침 거절·만료 정리·갱신·해제·종료 범위 검사·이벤트, 그리고 동시 시작에서 한 건만 이기는지 본다.
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from oah.atomic import StateCorrupt
from oah.intent import Binding
from oah.lease import Lease, LeaseError, Store
from oah.paths import RepoInfo
from oah.scope import normalize_scope
from tests.helpers import directory_alias, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
T0 = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.timezone.utc)

RACER = (
    "import json, pathlib, sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from oah.lease import LeaseError, Store\n"
    "from oah.paths import RepoInfo\n"
    "state, task, scope, ready, go = pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]\n"
    "info = RepoInfo(state.parent, state.parent, 'main', None)\n"
    "pathlib.Path(ready).write_text('ready', encoding='utf-8')\n"
    "while not pathlib.Path(go).exists():\n"
    "    time.sleep(0.005)\n"
    "try:\n"
    "    Store(state).start(task=task, agent='racer', scope=(scope,), note='', ttl_hours=1, info=info)\n"
    "    print('OK')\n"
    "except LeaseError as exc:\n"
    "    print(exc.code)\n"
)


class Clock:
    def __init__(self, start: dt.datetime) -> None:
        self.value = start

    def __call__(self) -> dt.datetime:
        return self.value

    def advance(self, **delta: float) -> None:
        self.value += dt.timedelta(**delta)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.root = make_tmp_dir(self)
        self.state = self.root / "state"
        self.clock = Clock(T0)
        self.store = Store(self.state, now=self.clock)
        self.info = RepoInfo(self.root, self.root / ".git", "main", "a" * 40)

    def start(self, task="t1", agent="claude", scope=("src",), ttl_hours=4.0, note=""):
        return self.store.start(task=task, agent=agent, scope=scope, note=note, ttl_hours=ttl_hours, info=self.info)

    def events(self) -> list[dict]:
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assert_code(self, code: str, call, *args, **kwargs):
        with self.assertRaises(LeaseError) as ctx:
            call(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_start_and_snapshot(self):
        lease = self.start(note="fix login")
        self.assertEqual((lease.task, lease.agent, lease.scope), ("t1", "claude", ["src"]))
        self.assertEqual((lease.created_at, lease.expires_at), ("2026-10-23T09:00:00Z", "2026-10-23T13:00:00Z"))
        self.assertEqual((lease.branch, lease.head_at_start, lease.worktree), ("main", "a" * 40, str(self.root)))
        [(found, stale)] = self.store.snapshot()
        self.assertEqual((found.id, stale), (lease.id, False))

    def test_overlapping_start_is_refused_with_holder(self):
        self.start(task="t1", agent="claude", scope=("src",))
        error = self.assert_code("OAH_LEASE_CONFLICT", self.start, task="t2", agent="codex", scope=("src/a.py",))
        self.assertIn("t1", str(error))
        self.assertIn("claude", str(error))
        self.start(task="t3", agent="codex", scope=("docs",))
        self.assertEqual(len(self.store.snapshot()), 2)

    def test_recorded_alias_conflicts_with_later_scope_on_its_spelling(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "link")
        started = self.start(scope=normalize_scope(["link/a.py"], self.root))
        self.assertEqual(started.scope, ["src/a.py", "link/a.py"])
        error = self.assert_code("OAH_LEASE_CONFLICT", self.start, task="t2", scope=("link/a.py",))
        self.assertIn("link/a.py", str(error))
        self.assertEqual(self.store.snapshot()[0][0].scope, started.scope)

    def test_same_task_twice_is_refused(self):
        self.start(task="t1", agent="claude", scope=("src",))
        self.assert_code("OAH_LEASE_EXISTS", self.start, task="t1", agent="claude", scope=("docs",))
        self.assert_code("OAH_LEASE_CONFLICT", self.start, task="t1", agent="codex", scope=("docs",))

    def test_invalid_names_and_ttl_are_refused(self):
        self.assert_code("OAH_TASK_INVALID", self.start, task="bad task")
        self.assert_code("OAH_AGENT_INVALID", self.start, agent="")
        self.assert_code("OAH_TTL_INVALID", self.start, ttl_hours=0)
        self.assertFalse((self.state / "leases.json").exists())

    def test_expired_lease_is_reported_stale_then_pruned_on_start(self):
        self.start(task="t1", scope=("src",), ttl_hours=1)
        self.clock.advance(hours=2)
        [(_, stale)] = self.store.snapshot()
        self.assertTrue(stale)
        self.start(task="t2", scope=("src",))
        self.assertEqual([lease.task for lease, _ in self.store.snapshot()], ["t2"])
        self.assertEqual([e["event"] for e in self.events()], ["lease_started", "lease_expired", "lease_started"])

    def test_pruning_is_saved_even_when_start_is_refused(self):
        self.start(task="t1", scope=("src",), ttl_hours=1)
        self.start(task="t2", scope=("docs",), ttl_hours=8)
        self.clock.advance(hours=2)
        self.assert_code("OAH_LEASE_CONFLICT", self.start, task="t3", scope=("docs/a.md",))
        self.assertEqual([lease.task for lease, _ in self.store.snapshot()], ["t2"])
        self.assertIn("lease_expired", [e["event"] for e in self.events()])

    def test_renew_extends_and_can_widen_scope(self):
        self.start(task="t1", scope=("src",), ttl_hours=1)
        self.clock.advance(minutes=30)
        renewed = self.store.renew(task="t1", agent="claude", ttl_hours=2, scope=("src", "docs"))
        self.assertEqual(renewed.expires_at, "2026-10-23T11:30:00Z")
        self.assertEqual(renewed.scope, ["src", "docs"])
        self.assertEqual(self.events()[-1]["event"], "lease_renewed")

    def test_renew_scope_conflict_and_ownership(self):
        self.start(task="t1", scope=("src",))
        self.start(task="t2", agent="codex", scope=("docs",))
        self.assert_code("OAH_LEASE_CONFLICT", self.store.renew, task="t1", agent="claude", ttl_hours=1,
                         scope=("src", "docs/x.md"))
        self.assert_code("OAH_LEASE_NOT_YOURS", self.store.renew, task="t1", agent="codex", ttl_hours=1)
        self.assert_code("OAH_LEASE_NOT_FOUND", self.store.renew, task="t9", agent="claude", ttl_hours=1)

    def test_renew_expired_is_refused(self):
        self.start(task="t1", ttl_hours=1)
        self.clock.advance(hours=1)
        self.assert_code("OAH_LEASE_EXPIRED", self.store.renew, task="t1", agent="claude", ttl_hours=1)

    def test_release_needs_force_for_other_agent(self):
        self.start(task="t1", agent="claude")
        self.assert_code("OAH_LEASE_NOT_YOURS", self.store.release, task="t1", agent="codex", reason="stuck",
                         force=False)
        self.assert_code("OAH_REASON_REQUIRED", self.store.release, task="t1", agent="claude", reason=" ",
                         force=False)
        self.store.release(task="t1", agent="codex", reason="session crashed", force=True)
        self.assertEqual(self.store.snapshot(), [])
        released = self.events()[-1]
        self.assertEqual((released["event"], released["forced"], released["reason"]),
                         ("lease_released", True, "session crashed"))

    def test_finish_refuses_out_of_scope_changes(self):
        lease = self.start(task="t1", scope=("src",))
        error = self.assert_code("OAH_SCOPE_EXCEEDED", self.store.finish, task="t1", agent="claude",
                                 lease_id=lease.id,
                                 summary="done", changed=["src/a.py", "docs/b.md"], allow_out_of_scope=False)
        self.assertIn("docs/b.md", str(error))
        self.assertEqual(len(self.store.snapshot()), 1)

    def test_finish_refuses_unrelated_changes_with_repo_alias(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "repo")
        started = self.start(scope=normalize_scope(["./repo"], self.root))
        error = self.assert_code("OAH_SCOPE_EXCEEDED", self.store.finish, task="t1", agent="claude",
                                 lease_id=started.id, summary="done", changed=["docs/other.md"],
                                 allow_out_of_scope=False)
        self.assertIn("docs/other.md", str(error))
        self.assertEqual(self.store.snapshot()[0][0].scope, ["src"])
        self.assertEqual([event["event"] for event in self.events()], ["lease_started"])

    def test_finish_requires_alias_to_be_declared_by_scope_renewal(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "link")
        started = self.start(scope=("src/a.py",))
        for changed in (["link/a.py"], ["link/a.py", "src/a.py"]):
            with self.subTest(changed=changed):
                error = self.assert_code("OAH_SCOPE_EXCEEDED", self.store.finish, task="t1", agent="claude",
                                         lease_id=started.id, summary="done", changed=changed,
                                         allow_out_of_scope=False)
                self.assertIn("link/a.py", str(error))
        renewed = self.store.renew(task="t1", agent="claude", ttl_hours=4,
                                   scope=normalize_scope(["src/a.py", "link/a.py"], self.root))
        self.assertEqual(renewed.scope, ["src/a.py", "link/a.py"])
        lease, beyond = self.store.finish(task="t1", agent="claude", lease_id=started.id, summary="done",
                                          changed=["link/a.py", "src/a.py"], allow_out_of_scope=False)
        self.assertEqual(lease.scope, ["src/a.py", "link/a.py"])
        self.assertEqual(beyond, [])
        self.assertEqual(self.store.snapshot(), [])

    def test_finish_accepts_alias_recorded_at_start(self):
        (self.root / "src").mkdir()
        directory_alias(self.root / "src", self.root / "link")
        started = self.start(scope=normalize_scope(["link/a.py"], self.root))
        lease, beyond = self.store.finish(task="t1", agent="claude", lease_id=started.id, summary="done",
                                          changed=["src/a.py", "link/a.py"], allow_out_of_scope=False)
        self.assertEqual(lease.scope, ["src/a.py", "link/a.py"])
        self.assertEqual(beyond, [])

    def test_finish_refuses_both_deletions_after_other_leases_directory_becomes_alias(self):
        for name in ("src", "docs"):
            (self.root / name).mkdir()
            (self.root / name / "readme.md").write_text("original", encoding="utf-8")
        started = self.start(scope=("src",))
        other = self.start(task="t2", agent="codex", scope=("docs",))
        for name in ("src", "docs"):
            (self.root / name / "readme.md").unlink()
        (self.root / "docs").rmdir()
        directory_alias(self.root / "src", self.root / "docs")
        error = self.assert_code("OAH_SCOPE_EXCEEDED", self.store.finish, task="t1", agent="claude",
                                 lease_id=started.id, summary="deleted", changed=["src/readme.md", "docs/readme.md"],
                                 allow_out_of_scope=False)
        self.assertIn("docs/readme.md", str(error))
        self.assertEqual([lease.id for lease, _ in self.store.snapshot()], [started.id, other.id])

    def test_finish_refuses_changes_under_directory_replaced_by_alias(self):
        for variant in ("deleted", "modified"):
            with self.subTest(variant=variant):
                root = make_tmp_dir(self)
                (root / "src").mkdir()
                (root / "src" / "a.py").write_text("unchanged\n", encoding="utf-8")
                if variant == "modified":
                    (root / "src" / "readme.md").write_text("unchanged target\n", encoding="utf-8")
                (root / "docs").mkdir()
                (root / "docs" / "readme.md").write_text("original\n", encoding="utf-8")
                info = RepoInfo(root, root / ".git", "main", "a" * 40)
                started = self.store.start(task="t1", agent="claude", scope=("src",), note="",
                                            ttl_hours=4, info=info)
                (root / "docs" / "readme.md").unlink()
                (root / "docs").rmdir()
                directory_alias(root / "src", root / "docs")
                changed = ["docs/readme.md", "docs/a.py"]
                error = self.assert_code("OAH_SCOPE_EXCEEDED", self.store.finish, task="t1", agent="claude",
                                         lease_id=started.id, summary="replaced", changed=changed,
                                         allow_out_of_scope=False)
                for path in changed:
                    self.assertIn(path, str(error))
                self.assertEqual([lease.id for lease, _ in self.store.snapshot()], [started.id])
                _, beyond = self.store.finish(task="t1", agent="claude", lease_id=started.id,
                                              summary="override", changed=changed, allow_out_of_scope=True)
                self.assertEqual(beyond, changed)
                self.assertEqual(self.events()[-1]["out_of_scope"], changed)
                self.assertTrue(self.events()[-1]["allowed_out_of_scope"])

    def test_finish_allows_out_of_scope_when_asked_and_records_it(self):
        started = self.start(task="t1", scope=("src",))
        lease, beyond = self.store.finish(task="t1", agent="claude", lease_id=started.id, summary="done",
                                          changed=["src/a.py", "docs/b.md"], allow_out_of_scope=True)
        self.assertEqual(beyond, ["docs/b.md"])
        self.assertEqual(self.store.snapshot(), [])
        finished = self.events()[-1]
        self.assertEqual((finished["event"], finished["out_of_scope"], finished["summary"]),
                         ("lease_finished", ["docs/b.md"], "done"))

    def test_finish_by_other_agent_is_refused(self):
        lease = self.start(task="t1", agent="claude")
        self.assert_code("OAH_LEASE_NOT_YOURS", self.store.finish, task="t1", agent="codex", summary="x",
                         lease_id=lease.id,
                         changed=[], allow_out_of_scope=False)

    def test_log_appends_a_note(self):
        self.store.log(task="t1", agent="claude", message="waiting for review")
        self.assertEqual(self.events()[-1]["event"], "note")

    def test_snapshot_reads_nothing_into_existence(self):
        self.assertEqual(self.store.snapshot(), [])
        self.assertFalse(self.state.exists())

    def test_corrupt_leases_file_is_reported(self):
        self.state.mkdir()
        (self.state / "leases.json").write_text('{"version": 1, "leases": [{"task": 3}]}', encoding="utf-8")
        from oah.atomic import StateCorrupt

        with self.assertRaises(StateCorrupt):
            self.store.snapshot()
        with self.assertRaises(StateCorrupt):
            self.start()

    def test_naive_timestamps_are_corrupt_on_read_and_mutation(self):
        original = self.start().to_json()
        for field in ("created_at", "expires_at"):
            with self.subTest(field=field):
                record = dict(original, **{field: "2026-10-23T10:00:00"})
                self.store.leases_path.write_text(json.dumps({"version": 1, "leases": [record]}), encoding="utf-8")
                for call in (self.store.snapshot, lambda: self.start(task="t2", scope=("docs",))):
                    with self.assertRaises(StateCorrupt) as caught:
                        call()
                    self.assertIn(str(self.store.leases_path), str(caught.exception))
                with self.assertRaises(StateCorrupt):
                    Lease.from_json(record, self.store.leases_path)

    def test_lease_without_gates_field_loads(self):
        record = self.start().to_json()
        self.assertEqual(record["gates"], [])
        del record["gates"]
        self.store.leases_path.write_text(json.dumps({"version": 1, "leases": [record]}), encoding="utf-8")
        (lease, _), = self.store.snapshot()
        self.assertEqual(lease.gates, [])
        bad = dict(record, gates="GATES.md")
        with self.assertRaises(StateCorrupt):
            Lease.from_json(bad, self.store.leases_path)

    def test_start_and_renew_record_gates_in_schema_1(self):
        lease = self.store.start(task="t1", agent="claude", scope=("src",), note="", ttl_hours=1, info=self.info,
                                 gates=(".oah/tasks/t1/GATES.md",))
        self.assertEqual(lease.gates, [".oah/tasks/t1/GATES.md"])
        data = json.loads(self.store.leases_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["leases"][0]["gates"], [".oah/tasks/t1/GATES.md"])
        renewed = self.store.renew(task="t1", agent="claude", ttl_hours=1, gates=("gates/a.md", "gates/b.md"))
        self.assertEqual(renewed.gates, ["gates/a.md", "gates/b.md"])
        self.assertEqual(self.events()[-1]["gates"], ["gates/a.md", "gates/b.md"])
        kept = self.store.renew(task="t1", agent="claude", ttl_hours=1)
        self.assertEqual(kept.gates, ["gates/a.md", "gates/b.md"])

    def test_finish_records_the_gate_report(self):
        lease = self.start()
        self.store.finish(task="t1", agent="claude", lease_id=lease.id, summary="done", changed=[],
                          allow_out_of_scope=False, gate_report={"outcome": "met", "verified": True})
        self.assertEqual(self.events()[-1]["gates"], {"outcome": "met", "verified": True})

    def test_fractional_ttl_survives_start_and_renew(self):
        self.clock.advance(microseconds=500000)
        lease = self.start(ttl_hours=0.000001)
        self.assertFalse(lease.expired(self.clock()))
        self.assertEqual(dt.datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00")) - self.clock(),
                         dt.timedelta(microseconds=3600))
        self.clock.advance(microseconds=1000)
        renewed = self.store.renew(task="t1", agent="claude", ttl_hours=0.000001)
        self.assertFalse(renewed.expired(self.clock()))
        self.clock.advance(microseconds=3600)
        self.assertTrue(self.store.snapshot()[0][1])

    def test_positive_ttl_below_clock_resolution_still_has_future_expiry(self):
        self.clock.advance(microseconds=500000)
        lease = self.start(ttl_hours=1e-12)
        self.assertFalse(lease.expired(self.clock()))
        renewed = self.store.renew(task="t1", agent="claude", ttl_hours=1e-12)
        self.assertFalse(renewed.expired(self.clock()))

    def test_failed_event_append_is_replayed_by_next_mutation(self):
        real_open = open

        def fail_append(path, mode="r", *args, **kwargs):
            if pathlib.Path(path) == self.store.events_path and "a" in mode:
                raise OSError("disk full at event append")
            return real_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=fail_append):
            with self.assertRaises(OSError):
                self.start()
        recovered = Store(self.state, now=self.clock)
        recovered.log(task="t1", agent="claude", message="after crash")
        self.assertEqual([event["event"] for event in self.events()], ["lease_started", "note"])
        self.assertEqual(len(recovered.snapshot()), 1)


class ConcurrentStartTest(unittest.TestCase):
    def race(self, scopes: list[str]) -> list[str]:
        work = make_tmp_dir(self)
        state, go = work / "state", work / "go"
        racers = []
        for n, scope in enumerate(scopes):
            ready = work / f"ready-{n}"
            racers.append((ready, subprocess.Popen(
                [sys.executable, "-c", RACER, SRC, str(state), f"task{n}", scope, str(ready), str(go)],
                stdout=subprocess.PIPE, text=True)))
        deadline = time.monotonic() + 30
        while not all(ready.exists() for ready, _ in racers):
            self.assertLess(time.monotonic(), deadline, "racers did not start")
            time.sleep(0.01)
        go.write_text("go", encoding="utf-8")
        outputs = [proc.communicate(timeout=60)[0].strip() for _, proc in racers]
        self.leases = json.loads((state / "leases.json").read_text(encoding="utf-8"))["leases"]
        return outputs

    def test_concurrent_overlapping_starts_admit_exactly_one(self):
        # Without the lock a single race shows several winners only part of the time (measured 3 of 5),
        # so run three independent races.
        # Every pair of these scopes overlaps (case folding included), so exactly one start can win.
        for round_no in range(3):
            with self.subTest(round=round_no):
                outputs = self.race(["src", "src/a.py", "src", "SRC", "src/a.py", "repo"])
                self.assertEqual(sorted(outputs), ["OAH_LEASE_CONFLICT"] * 5 + ["OK"])
                self.assertEqual(len(self.leases), 1)

    def test_concurrent_disjoint_starts_all_land(self):
        outputs = self.race(["a", "b", "c", "d", "e", "f"])
        self.assertEqual(outputs, ["OK"] * 6)
        self.assertEqual(sorted(lease["task"] for lease in self.leases), [f"task{n}" for n in range(6)])


def binding(revision: int = 1, *, ident: str = "t1", digest: str = "1", acs=("AC1",),
            source: str = ".oah/tasks/t1/PACKET.md") -> Binding:
    return Binding(source=source, id=ident, revision=revision, digest="sha256:" + digest * 64, acs=tuple(acs))


class IntentBindingTest(unittest.TestCase):
    """The optional `intent` field (schema 1) and the forward-only rebind rules (A7)."""

    def setUp(self):
        self.root = make_tmp_dir(self)
        self.state = self.root / "state"
        self.clock = Clock(T0)
        self.store = Store(self.state, now=self.clock)
        self.info = RepoInfo(self.root, self.root / ".git", "main", "a" * 40)

    def start(self, intent=None, scope=("src",)):
        return self.store.start(task="t1", agent="claude", scope=scope, note="", ttl_hours=4.0, info=self.info,
                                intent=intent)

    def records(self) -> list[dict]:
        return json.loads((self.state / "leases.json").read_text(encoding="utf-8"))["leases"]

    def events(self) -> list[dict]:
        return [json.loads(line) for line in (self.state / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_lease_without_intent_keeps_old_record_shape(self):
        lease = self.start()
        self.assertIsNone(lease.intent)
        self.assertIsNone(lease.binding())
        self.assertNotIn("intent", self.records()[0])
        old = dict(self.records()[0])
        loaded = Lease.from_json(old, self.state / "leases.json")
        self.assertIsNone(loaded.intent)

    def test_start_records_intent_binding(self):
        lease = self.start(binding())
        self.assertEqual(lease.binding(), binding())
        self.assertEqual(self.records()[0]["intent"], binding().to_json())
        self.assertEqual(json.loads((self.state / "leases.json").read_text(encoding="utf-8"))["version"], 1)
        self.assertEqual(self.store.snapshot()[0][0].binding(), binding())
        self.assertEqual(self.events()[-1]["intent"], binding().to_json())

    def test_malformed_intent_is_state_corrupt(self):
        self.start(binding())
        data = json.loads((self.state / "leases.json").read_text(encoding="utf-8"))
        for bad in ({"source": "x"}, "text", {**binding().to_json(), "revision": 0}):
            with self.subTest(bad=bad):
                data["leases"][0]["intent"] = bad
                (self.state / "leases.json").write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(StateCorrupt):
                    self.store.snapshot()

    def rebind(self, new: Binding, **extra):
        return self.store.rebind(task="t1", agent="claude", binding=new, ttl_hours=4.0, **extra)

    def test_rebind_rules(self):
        self.start()
        lease, action = self.rebind(binding())
        self.assertEqual((action, lease.binding()), ("bound", binding()))
        lease, action = self.rebind(binding())
        self.assertEqual(action, "unchanged")
        self.clock.advance(hours=1)
        lease, action = self.rebind(binding(2, digest="2"))
        self.assertEqual((action, lease.binding().revision), ("rebound", 2))
        self.assertEqual(lease.expires_at, "2026-10-23T14:00:00Z")
        refusals = {
            "OAH_INTENT_ID_CHANGED": binding(3, ident="other", digest="3"),
            "OAH_INTENT_REVISION_REQUIRED": binding(2, digest="4"),
            "OAH_INTENT_REVISION_INVALID": binding(1, digest="1"),
        }
        for code, new in refusals.items():
            with self.subTest(code=code), self.assertRaises(LeaseError) as caught:
                self.rebind(new)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.records()[0]["intent"]["revision"], 2)
        names = [e["event"] for e in self.events()]
        self.assertEqual(names.count("intent_bound"), 1)
        self.assertEqual(names.count("intent_rebound"), 1)

    def test_rebind_refuses_other_agent_expired_and_overlap(self):
        self.start(binding())
        with self.assertRaises(LeaseError) as caught:
            self.store.rebind(task="t1", agent="codex", binding=binding(2, digest="2"), ttl_hours=1)
        self.assertEqual(caught.exception.code, "OAH_LEASE_NOT_YOURS")
        self.store.start(task="t2", agent="codex", scope=("docs",), note="", ttl_hours=4.0, info=self.info)
        with self.assertRaises(LeaseError) as caught:
            self.rebind(binding(2, digest="2"), scope=["docs/a.md"])
        self.assertEqual(caught.exception.code, "OAH_LEASE_CONFLICT")
        lease, _ = self.rebind(binding(2, digest="2"), scope=["src", "tests"])
        self.assertEqual(lease.scope, ["src", "tests"])
        self.clock.advance(hours=5)
        with self.assertRaises(LeaseError) as caught:
            self.rebind(binding(3, digest="3"))
        self.assertEqual(caught.exception.code, "OAH_LEASE_EXPIRED")

    def test_rebind_changes_gates_atomically_and_omission_preserves_them(self):
        lease = self.start(binding())
        self.store.renew(task="t1", agent="claude", ttl_hours=4, gates=["checks/old.md"])
        rebound, _ = self.rebind(binding(2, digest="2"), gates=["checks/new.md"])
        self.assertEqual((rebound.id, rebound.gates), (lease.id, ["checks/new.md"]))
        self.assertEqual(self.events()[-1]["gates"], ["checks/new.md"])
        unchanged, _ = self.rebind(binding(2, digest="2"))
        self.assertEqual(unchanged.gates, ["checks/new.md"])
        self.store.start(task="t2", agent="codex", scope=("docs",), note="", ttl_hours=4, info=self.info)
        before = self.records()
        with self.assertRaises(LeaseError) as caught:
            self.rebind(binding(3, digest="3"), scope=["docs"], gates=["checks/unmet.md"])
        self.assertEqual(caught.exception.code, "OAH_LEASE_CONFLICT")
        self.assertEqual(self.records(), before)

    def test_finish_records_intent_report(self):
        from oah import intent, packet

        path = packet.create(self.root, "t1", scope=("src",))
        path.write_text(path.read_text(encoding="utf-8").replace('Status: draft', 'Status: accepted'),
                        encoding="utf-8")
        bound = intent.parse(intent.read_source(path)).bind(".oah/tasks/t1/PACKET.md")
        lease = self.start(bound)
        report = {"line": bound.line(), "acs": {"AC1": "met"}}
        self.store.finish(task="t1", agent="claude", lease_id=lease.id, summary="done", changed=[],
                          allow_out_of_scope=False, intent_report=report, checked_binding=bound)
        self.assertEqual(self.events()[-1]["intent"], report)

    def test_finish_compares_the_checked_binding_under_the_lock(self):
        old = binding()
        lease = self.start(old)
        self.rebind(binding(2, digest="2"))
        with self.assertRaises(LeaseError) as caught:
            self.store.finish(task="t1", agent="claude", lease_id=lease.id, summary="done", changed=[],
                              allow_out_of_scope=False, checked_binding=old)
        self.assertEqual(caught.exception.code, "OAH_LEASE_CHANGED")
        self.assertEqual(self.store.snapshot()[0][0].binding(), binding(2, digest="2"))
        self.assertNotEqual(self.events()[-1]["event"], "lease_finished")

    def test_finish_refuses_when_an_unbound_lease_was_bound_during_checks(self):
        lease = self.start()
        self.rebind(binding())
        with self.assertRaises(LeaseError) as caught:
            self.store.finish(task="t1", agent="claude", lease_id=lease.id, summary="done", changed=[],
                              allow_out_of_scope=False)
        self.assertEqual(caught.exception.code, "OAH_LEASE_CHANGED")
        self.assertEqual(self.store.snapshot()[0][0].binding(), binding())


if __name__ == "__main__":
    unittest.main()
