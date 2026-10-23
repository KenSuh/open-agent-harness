# 위키 로그 롤오버 검사 — 항목 경계, 임계값, 큰 항목 규칙, 아카이브 번호, 보존 스트림 바이트 비교, 저널 복구·장애 주입·동시 편집을 본다.
from __future__ import annotations

import base64
import datetime
import json
import pathlib
import unittest
from unittest import mock

from oah import wiki as wiki_pkg
from oah.wiki import log
from oah.wiki.base import WikiError, WikiSettings
from oah.wiki.store import Wiki
from tests.helpers import make_tmp_dir

TODAY = datetime.date(2026, 10, 23)
HEADER = (b"# Log\n\nNewest first.\n\n<!-- OAH:ARCHIVE_INDEX:BEGIN -->\n<!-- OAH:ARCHIVE_INDEX:END -->\n\n"
          b"<!-- OAH:LOG_INSERT_BELOW -->\n\n")
STEPS = ("journal-written", "validated", "archive-published", "phase-recorded", "log-replaced")


class Crash(Exception):
    """Simulates the process dying at a publication step."""


def crash_at(step: str):
    def fault(name: str) -> None:
        if name == step:
            raise Crash(name)
    return fault


def entry(number: int, body: int = 10, eol: bytes = b"\n") -> bytes:
    day = datetime.date(2026, 1, 1) + datetime.timedelta(days=number)
    return (f"## {day.isoformat()} entry {number}".encode() + eol + eol + b"x" * body + eol + eol)


def make_log(entries: list[bytes], header: bytes = HEADER, eol: bytes = b"\n") -> bytes:
    return header.replace(b"\n", eol) + b"".join(entries)


def newest_first(count: int, body: int = 10, eol: bytes = b"\n") -> list[bytes]:
    return [entry(number, body, eol) for number in range(count, 0, -1)]


def wiki_stream(folder: pathlib.Path) -> bytes:
    archives = {log.archive_number(p.name): p.read_bytes() for p in folder.iterdir()
                if log.archive_number(p.name) is not None}
    return log.preservation_stream((folder / "log.md").read_bytes(), archives)


def snapshot(*folders: pathlib.Path) -> dict[str, bytes]:
    out = {}
    for folder in folders:
        if folder.exists():
            for path in sorted(folder.rglob("*")):
                if path.is_file() and path.name != ".lock":
                    out[str(path)] = path.read_bytes()
    return out


class ParseTest(unittest.TestCase):
    def test_entry_parsing_includes_blank_lines(self):
        data = make_log([b"## 2026-01-03 c\n\nbody\n\n\n", b"## 2026-01-02 b\nno blank\n", b"## 2026-01-01 a"])
        parsed = log.parse_log(data, "log.md")
        self.assertEqual([e.data for e in parsed.entries],
                         [b"## 2026-01-03 c\n\nbody\n\n\n", b"## 2026-01-02 b\nno blank\n", b"## 2026-01-01 a"])
        self.assertEqual([(e.date, e.title) for e in parsed.entries],
                         [("2026-01-03", "c"), ("2026-01-02", "b"), ("2026-01-01", "a")])
        self.assertEqual(parsed.header + parsed.prefix + b"".join(e.data for e in parsed.entries), data)

    def test_fenced_heading_is_not_entry(self):
        body = b"## 2026-01-02 real\n\n```md\n## 2026-01-09 fenced\n```\n~~~\n## 2026-13-99 bad but fenced\n~~~\n"
        parsed = log.parse_log(make_log([body, b"## 2026-01-01 old\n"]), "log.md")
        self.assertEqual([e.title for e in parsed.entries], ["real", "old"])
        self.assertEqual(parsed.entries[0].data, body)

    def test_heading_without_digit_is_content(self):
        parsed = log.parse_log(make_log([b"## 2026-01-02 a\n\n## Notes\n### 2026-01-05 sub\n"]), "log.md")
        self.assertEqual(len(parsed.entries), 1)

    def test_invalid_boundaries_refuse(self):
        cases = {
            "no marker": (b"# Log\n\n## 2026-01-01 a\n", "no marker"),
            "marker only in a fence": (b"```\n<!-- OAH:LOG_INSERT_BELOW -->\n```\n", "no marker"),
            "two markers": (HEADER + b"<!-- OAH:LOG_INSERT_BELOW -->\n## 2026-01-01 a\n", "marker lines"),
            "marker inside entry": (HEADER + b"## 2026-01-01 a\n<!-- OAH:LOG_INSERT_BELOW -->\n", "OAH_WIKI_LOG_INVALID"),
            "first heading undated": (HEADER + b"## Fixed the bug\nx\n## 2026-01-01 a\n", "undated first entry heading"),
            "first heading bad date": (HEADER + b"## 2026-13-01 a\n", "undated first entry heading"),
            "first heading no title": (HEADER + b"## 2026-01-01\n", "undated first entry heading"),
            "first heading short date": (HEADER + b"## 2026-1-1 a\n", "undated first entry heading"),
            "later bad date": (HEADER + b"## 2026-01-02 a\n## 2026-13-01 b\n", "OAH_WIKI_LOG_INVALID"),
            "indented": (HEADER + b"## 2026-01-02 a\n  ## 2026-01-01 b\n", "OAH_WIKI_LOG_INVALID"),
            "unclosed fence": (HEADER + b"## 2026-01-01 a\n```\n## 2026-01-01 b\n", "unclosed code fence"),
            "unclosed fence above marker": (b"```\n" + HEADER, "unclosed code fence"),
            "nonempty prefix": (HEADER + b"stray text\n## 2026-01-01 a\n", "OAH_WIKI_LOG_INVALID"),
            "fence in prefix": (HEADER + b"```\n```\n## 2026-01-01 a\n", "OAH_WIKI_LOG_INVALID"),
            "index below marker": (b"<!-- OAH:LOG_INSERT_BELOW -->\n<!-- OAH:ARCHIVE_INDEX:BEGIN -->\n",
                                   "OAH_WIKI_LOG_INVALID"),
            "index end only": (b"<!-- OAH:ARCHIVE_INDEX:END -->\n<!-- OAH:LOG_INSERT_BELOW -->\n",
                               "OAH_WIKI_LOG_INVALID"),
            "not utf-8": (HEADER + b"## 2026-01-01 a\n\xff\n", "OAH_WIKI_LOG_INVALID"),
            "surrogate": (HEADER + b"## 2026-01-01 a\n\xed\xa0\x80\n", "OAH_WIKI_LOG_INVALID"),
        }
        for name, (data, case) in cases.items():
            with self.subTest(name), self.assertRaises(WikiError) as ctx:
                log.parse_log(data, "log.md")
            self.assertEqual(ctx.exception.code, "OAH_WIKI_LOG_INVALID")
            if not case.startswith("OAH_"):
                self.assertIn(case, str(ctx.exception))

    def test_blank_prefix_stays_with_file(self):
        prefix_log = make_log(newest_first(30, body=100), header=HEADER + b"\n  \n")
        parsed = log.parse_log(prefix_log, "log.md")
        self.assertEqual(parsed.prefix, b"\n\n  \n")
        plan = log.plan_rollover(prefix_log, {}, trigger=1000, target=500, today=TODAY)
        new = log.parse_log(plan.log, "log.md")
        self.assertEqual(new.prefix, b"\n\n  \n")
        self.assertTrue(plan.archive.split(b"<!-- OAH:LOG_INSERT_BELOW -->\n")[1].startswith(b"## "))


class AddEntryTest(unittest.TestCase):
    def test_wiki_log_inserts_parseable_entry(self):
        data = make_log(newest_first(2))
        out = log.add_entry(data, title="Shipped", body="Line one\nLine two", date=TODAY)
        parsed = log.parse_log(out, "log.md")
        self.assertEqual(parsed.entries[0].data, b"## 2026-10-23 Shipped\n\nLine one\nLine two\n\n")
        self.assertEqual([e.data for e in parsed.entries[1:]], newest_first(2))
        self.assertTrue(out.startswith(HEADER))

    def test_wiki_log_into_empty_log_without_trailing_newline(self):
        out = log.add_entry(b"<!-- OAH:LOG_INSERT_BELOW -->", title="First", body="", date=TODAY)
        self.assertEqual(out, b"<!-- OAH:LOG_INSERT_BELOW -->\n## 2026-10-23 First\n\n")

    def test_wiki_log_keeps_crlf(self):
        data = make_log(newest_first(1, eol=b"\r\n"), eol=b"\r\n")
        out = log.add_entry(data, title="Crlf", body="a\nb", date=TODAY)
        self.assertIn(b"## 2026-10-23 Crlf\r\n\r\na\r\nb\r\n\r\n## 2026-01-02", out)
        self.assertNotIn(b"\n", out.replace(b"\r\n", b""))

    def test_wiki_log_refuses_breaking_body(self):
        data = make_log(newest_first(1))
        for body in ["x\n## 2026-01-05 sneaky", "<!-- OAH:LOG_INSERT_BELOW -->", "```\nopen fence"]:
            with self.subTest(body=body), self.assertRaises(WikiError) as ctx:
                log.add_entry(data, title="t", body=body, date=TODAY)
            self.assertEqual(ctx.exception.code, "OAH_WIKI_LOG_INVALID")


def sized_log(total: int, count: int = 10) -> bytes:
    """A log of exactly `total` bytes with `count` equal-ish entries."""
    base = make_log(newest_first(count, body=0))
    pad = total - len(base)
    per, extra = divmod(pad, count)
    entries = [entry(n, per + (extra if n == 1 else 0)) for n in range(count, 0, -1)]
    data = make_log(entries)
    assert len(data) == total, len(data)
    return data


class PlanTest(unittest.TestCase):
    def test_threshold_boundaries(self):
        at = sized_log(512_000)
        self.assertIsInstance(log.plan_rollover(at, {}, trigger=512_000, target=409_600, today=TODAY),
                              log.RolloverNoOp)
        over = sized_log(512_001)
        plan = log.plan_rollover(over, {}, trigger=512_000, target=409_600, today=TODAY)
        self.assertIsInstance(plan, log.RolloverPlan)
        self.assertLessEqual(plan.retained_bytes, 409_600)
        self.assertEqual(wiki_pkg.DEFAULTS["rollover_trigger_bytes"], 512_000)
        self.assertEqual(wiki_pkg.DEFAULTS["rollover_target_bytes"], 409_600)

    def test_metadata_only_excess_is_noop(self):
        entries = newest_first(5, body=100)
        target = sum(len(e) for e in entries)
        header = HEADER.replace(b"Newest first.", b"Newest first. " + b"m" * 2000)
        data = make_log(entries, header=header)
        result = log.plan_rollover(data, {}, trigger=1000, target=target, today=TODAY)
        self.assertIsInstance(result, log.RolloverNoOp)
        self.assertIn("headers", result.reason)
        plan = log.plan_rollover(data, {}, trigger=1000, target=target - 1, today=TODAY)
        self.assertIsInstance(plan, log.RolloverPlan)

    def test_smallest_oldest_suffix_moves(self):
        sizes = [50, 300, 20, 400, 10, 90, 250, 60]
        entries = [entry(len(sizes) - i, body) for i, body in enumerate(sizes)]
        data = make_log(entries)
        lengths = [len(e) for e in entries]
        for target in [100, 400, 450, 900, 1300, sum(lengths) - 1]:
            with self.subTest(target=target):
                plan = log.plan_rollover(data, {}, trigger=10, target=target, today=TODAY)
                expected = max([k for k in range(1, len(entries)) if sum(lengths[:k]) <= target], default=1)
                self.assertEqual(plan.kept, expected)
                self.assertEqual(plan.moved, len(entries) - expected)
                self.assertEqual(log.parse_log(plan.log, "log.md").entries[-1].data, entries[expected - 1])

    def test_retained_exactly_target_is_allowed(self):
        entries = newest_first(4, body=50)
        target = len(entries[0]) + len(entries[1])
        plan = log.plan_rollover(make_log(entries), {}, trigger=10, target=target, today=TODAY)
        self.assertEqual((plan.kept, plan.retained_bytes), (2, target))

    def test_oversized_newest_stays(self):
        entries = [entry(3, 5000), entry(2, 10), entry(1, 10)]
        plan = log.plan_rollover(make_log(entries), {}, trigger=100, target=1000, today=TODAY)
        self.assertEqual((plan.kept, plan.moved), (1, 2))
        self.assertEqual(len(plan.oversized), 1)
        self.assertIn("entry 3", plan.oversized[0])

    def test_single_oversized_entry_creates_no_archive(self):
        result = log.plan_rollover(make_log([entry(1, 5000)]), {}, trigger=100, target=1000, today=TODAY)
        self.assertIsInstance(result, log.RolloverNoOp)
        self.assertEqual(len(result.oversized), 1)

    def test_oversized_older_entry_moves_whole(self):
        entries = [entry(3, 10), entry(2, 5000), entry(1, 10)]
        plan = log.plan_rollover(make_log(entries), {}, trigger=100, target=1000, today=TODAY)
        self.assertEqual(plan.kept, 1)
        moved = log.parse_log(plan.archive, plan.archive_name).entries
        self.assertEqual([e.data for e in moved], entries[1:])

    def test_one_run_one_archive_even_over_target(self):
        entries = [entry(3, 10), entry(2, 3000), entry(1, 3000)]
        plan = log.plan_rollover(make_log(entries), {}, trigger=100, target=1000, today=TODAY)
        self.assertEqual(plan.moved, 2)
        self.assertGreater(len(plan.archive), 1000)

    def test_archive_numbering(self):
        data = make_log(newest_first(10, body=100))
        archive = log.plan_rollover(data, {}, trigger=10, target=200, today=TODAY).archive
        for existing, expected in [({}, "log_archive_01.md"), ({1: archive, 2: archive}, "log_archive_03.md"),
                                   ({7: archive}, "log_archive_08.md"), ({99: archive}, "log_archive_100.md")]:
            with self.subTest(existing=sorted(existing)):
                plan = log.plan_rollover(data, existing, trigger=10, target=200, today=TODAY)
                self.assertEqual(plan.archive_name, expected)

    def test_archive_index_regenerated(self):
        data = make_log(newest_first(10, body=100))
        plan = log.plan_rollover(data, {}, trigger=10, target=300, today=TODAY)
        header = log.parse_log(plan.log, "log.md").header.decode()
        self.assertIn("| [[log_archive_01]] | ", header)
        self.assertLess(header.index("OAH:ARCHIVE_INDEX:BEGIN"), header.index("[[log_archive_01]]"))
        self.assertLess(header.index("[[log_archive_01]]"), header.index("OAH:ARCHIVE_INDEX:END"))
        bare = b"# Log\n<!-- OAH:LOG_INSERT_BELOW -->\n" + b"".join(newest_first(10, body=100))
        plan = log.plan_rollover(bare, {}, trigger=10, target=300, today=TODAY)
        self.assertIn(b"<!-- OAH:ARCHIVE_INDEX:BEGIN -->", plan.log)
        self.assertIsNotNone(log.parse_log(plan.log, "log.md").index_span)


def make_store(test: unittest.TestCase, data: bytes, **settings) -> tuple[Wiki, pathlib.Path]:
    root = make_tmp_dir(test)
    folder = root / "docs" / "wiki"
    folder.mkdir(parents=True)
    (folder / "log.md").write_bytes(data)
    options = {"rollover_trigger_bytes": 600, "rollover_target_bytes": 300, **settings}
    return Wiki(root, WikiSettings(**options), root / "state"), folder


class StoreRolloverTest(unittest.TestCase):
    def test_stream_preserved_crlf_and_multiple_archives(self):
        for eol in (b"\n", b"\r\n"):
            with self.subTest(eol=eol):
                w, folder = make_store(self, make_log(newest_first(8, body=40, eol=eol), eol=eol))
                original = wiki_stream(folder)
                new_entries = []
                for round_number in range(1, 4):
                    result = w.rollover(today=TODAY)
                    self.assertTrue(result.line.startswith("OAH_WIKI_ROLLED_OVER"), result.line)
                    self.assertEqual(result.archive.name, f"log_archive_{round_number:02d}.md")
                    self.assertEqual(wiki_stream(folder), b"".join(new_entries[::-1]) + original)
                    for _ in range(6):
                        w.log(f"round {round_number}", body="y" * 40, today=TODAY)
                        new_entries.append(log.parse_log((folder / "log.md").read_bytes(), "log.md").entries[0].data)
                    stream = wiki_stream(folder)
                    self.assertEqual(stream, b"".join(new_entries[::-1]) + original)
                for path in folder.iterdir():
                    if eol == b"\r\n":
                        self.assertNotIn(b"\n", path.read_bytes().replace(b"\r\n", b""), path.name)
                header = log.parse_log((folder / "log.md").read_bytes(), "log.md").header.decode()
                self.assertLess(header.index("log_archive_03"), header.index("log_archive_02"))

    def test_noop_writes_nothing(self):
        w, folder = make_store(self, make_log(newest_first(2)))
        before = snapshot(folder, w.state)
        self.assertTrue(w.rollover(today=TODAY).line.startswith("OAH_WIKI_ROLLOVER_NOOP"))
        self.assertEqual(snapshot(folder, w.state), before)

    def test_bad_archive_names_refuse(self):
        for name in ("log_archive_1.md", "log_archive_00.md"):
            with self.subTest(name=name):
                w, folder = make_store(self, make_log(newest_first(20, body=40)))
                (folder / name).write_bytes(b"<!-- OAH:LOG_INSERT_BELOW -->\n")
                with self.assertRaises(WikiError) as ctx:
                    w.rollover(today=TODAY)
                self.assertEqual(ctx.exception.code, "OAH_WIKI_LOG_INVALID")
        w, folder = make_store(self, make_log(newest_first(20, body=40)))
        for name in ("log_archive_01.md", "log_archive_001.md"):
            (folder / name).write_bytes(b"<!-- OAH:LOG_INSERT_BELOW -->\n")
        with self.assertRaises(WikiError):
            w.rollover(today=TODAY)

    def test_rollover_publishes_archive_then_log(self):
        w, folder = make_store(self, make_log(newest_first(20, body=40)))
        original = (folder / "log.md").read_bytes()
        seen = {}

        def fault(step: str) -> None:
            seen[step] = ((folder / "log_archive_01.md").exists(), (folder / "log.md").read_bytes() == original,
                          w.journal_path.exists())

        w.rollover(today=TODAY, fault=fault)
        self.assertEqual(seen["journal-written"], (False, True, True))
        self.assertEqual(seen["archive-published"], (True, True, True))
        self.assertEqual(seen["log-replaced"], (True, False, True))
        self.assertFalse(w.journal_path.exists())
        journal_phases = []

        w2, folder2 = make_store(self, make_log(newest_first(20, body=40)))
        w2.rollover(today=TODAY, fault=lambda step: journal_phases.append(
            json.loads(w2.journal_path.read_text(encoding="utf-8"))["phase"]) if w2.journal_path.exists() else None)
        self.assertEqual(journal_phases[0], "staged")
        self.assertEqual(journal_phases[-1], "archive-published")

    def test_journal_records_paths_bytes_digests_and_phase(self):
        w, folder = make_store(self, make_log(newest_first(20, body=40)))
        with self.assertRaises(Crash):
            w.rollover(today=TODAY, fault=crash_at("journal-written"))
        journal = json.loads(w.journal_path.read_text(encoding="utf-8"))
        for key in ("wiki_dir", "log", "archive", "phase", "original_log", "original_log_sha256",
                    "replacement_log", "replacement_log_sha256", "archive_bytes", "archive_sha256", "archive_set"):
            self.assertIn(key, journal)
        self.assertEqual(journal["phase"], "staged")
        self.assertEqual(journal["archive"], "log_archive_01.md")


class RecoveryTest(unittest.TestCase):
    def crashed(self, step: str, archives: int = 0) -> tuple[Wiki, pathlib.Path, bytes]:
        w, folder = make_store(self, make_log(newest_first(20, body=40)))
        for _ in range(archives):
            w.rollover(today=TODAY)
            for i in range(12):
                w.log(f"more {_}-{i}", body="z" * 40, today=TODAY)
        stream = wiki_stream(folder)
        with self.assertRaises(Crash):
            w.rollover(today=TODAY, fault=crash_at(step))
        self.assertTrue(w.journal_path.exists())
        return w, folder, stream

    def assert_complete(self, w: Wiki, folder: pathlib.Path, stream: bytes) -> None:
        self.assertFalse(w.journal_path.exists())
        self.assertEqual(wiki_stream(folder), stream)
        entries = log.parse_log((folder / "log.md").read_bytes(), "log.md").entries
        archived = [e for p in sorted(folder.glob("log_archive_*.md"))
                    for e in log.parse_log(p.read_bytes(), p.name).entries]
        all_data = [e.data for e in entries + archived]
        self.assertEqual(len(all_data), len(set(all_data)), "an entry is duplicated")

    def test_fault_at_every_step_then_recover(self):
        for archives in (0, 2):
            for step in STEPS:
                with self.subTest(step=step, archives=archives):
                    w, folder, stream = self.crashed(step, archives)
                    line = w.recover()
                    self.assertTrue(line.startswith("OAH_WIKI_RECOVERED"), line)
                    if step == "log-replaced":
                        self.assertIn("already complete", line)
                    self.assert_complete(w, folder, stream)
                    self.assertEqual(w.recover(), "OAH_WIKI_RECOVER_NOTHING nothing to recover")

    def test_fault_during_recovery_then_recover_again(self):
        for step in ("archive-published", "log-replaced"):
            with self.subTest(step=step):
                w, folder, stream = self.crashed("validated")
                with self.assertRaises(Crash):
                    w.recover(fault=crash_at(step))
                self.assertTrue(w.journal_path.exists())
                self.assertTrue(w.recover().startswith("OAH_WIKI_RECOVERED"))
                self.assert_complete(w, folder, stream)

    def test_recover_twice_is_idempotent(self):
        w, folder, stream = self.crashed("archive-published")
        w.recover()
        after = snapshot(folder)
        for _ in range(2):
            self.assertEqual(w.recover(), "OAH_WIKI_RECOVER_NOTHING nothing to recover")
            self.assertEqual(snapshot(folder), after)

    def test_recovery_revalidates_before_archive_publication(self):
        w, folder, _ = self.crashed("validated")
        path = folder / "log.md"
        late = path.read_bytes() + HardeningTest.APPEND
        digests = w._digests

        def editor(*args, **kwargs):
            result = digests(*args, **kwargs)
            path.write_bytes(late)  # after recovery's first log snapshot
            return result

        with mock.patch.object(w, "_digests", side_effect=editor):
            with self.assertRaises(WikiError) as ctx:
                w.recover()
        self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_CONFLICT")
        self.assertEqual(path.read_bytes(), late)
        self.assertFalse((folder / "log_archive_01.md").exists())
        journal = json.loads(w.journal_path.read_text(encoding="utf-8"))
        self.assertEqual(base64.b64decode(journal["current_log"]), late)

    def test_recovery_concurrent_edits_preserve_live_bytes_and_journal(self):
        cases = [("archive-published", "log.md", "append"),
                 ("archive-published", "log_archive_01.md", "append"),
                 ("archive-published", "log_archive_01.md", "delete"),
                 ("log-replaced", "log.md", "append"),
                 ("log-replaced", "log_archive_01.md", "append"),
                 ("log-replaced", "log_archive_01.md", "delete")]
        for step, name, action in cases:
            with self.subTest(step=step, name=name, action=action):
                w, folder, _ = self.crashed("validated")
                live = {}

                def editor(current):
                    if current == step:
                        path = folder / name
                        if action == "delete":
                            path.unlink()
                        else:
                            path.write_bytes(path.read_bytes() + HardeningTest.APPEND)
                        live.update(snapshot(folder))

                with self.assertRaises(WikiError) as ctx:
                    w.recover(fault=editor)
                self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_CONFLICT")
                self.assertIn(name, str(ctx.exception))
                self.assertIn(str(w.journal_path), str(ctx.exception))
                self.assertEqual(snapshot(folder), live)
                journal = json.loads(w.journal_path.read_text(encoding="utf-8"))
                self.assertEqual(base64.b64decode(journal["current_log"]), (folder / "log.md").read_bytes())
                if (folder / "log_archive_01.md").exists():
                    self.assertEqual(base64.b64decode(journal["current_archive"]),
                                     (folder / "log_archive_01.md").read_bytes())
                before = snapshot(folder, w.state)
                with self.assertRaises(WikiError):
                    w.recover()
                self.assertEqual(snapshot(folder, w.state), before)

    def test_already_complete_recovery_rechecks_before_clearing_journal(self):
        w, folder, _ = self.crashed("log-replaced")
        path = folder / "log_archive_01.md"
        digests = w._digests
        calls = 0

        def editor(*args, **kwargs):
            nonlocal calls
            result = digests(*args, **kwargs)
            calls += 1
            if calls == 1:
                path.unlink()
            return result

        with mock.patch.object(w, "_digests", side_effect=editor):
            with self.assertRaises(WikiError) as ctx:
                w.recover()
        self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_CONFLICT")
        self.assertFalse(path.exists())
        self.assertTrue(w.journal_path.exists())

    def test_recover_conflict_preserves_everything(self):
        def edit_log(folder):
            (folder / "log.md").write_bytes((folder / "log.md").read_bytes() + b"edited\n")

        def edit_archive(folder):
            (folder / "log_archive_01.md").write_bytes(b"other bytes")

        def new_sibling(folder):
            (folder / "log_archive_09.md").write_bytes(b"<!-- OAH:LOG_INSERT_BELOW -->\n")

        def drop_archive(folder):
            (folder / "log_archive_01.md").unlink()

        cases = [("archive-published", edit_log), ("archive-published", edit_archive),
                 ("validated", edit_archive), ("archive-published", new_sibling),
                 ("log-replaced", drop_archive)]
        for step, change in cases:
            with self.subTest(step=step, change=change.__name__):
                w, folder, _ = self.crashed(step)
                change(folder)
                before = snapshot(folder, w.state)
                with self.assertRaises(WikiError) as ctx:
                    w.recover()
                self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_CONFLICT")
                self.assertEqual(snapshot(folder, w.state), before)

    def test_resolved_reconciles_the_older_conflict(self):
        w, folder, stream = self.crashed("archive-published")
        (folder / "log.md").write_bytes((folder / "log.md").read_bytes() + b"edited\n")
        with self.assertRaises(WikiError) as ctx:
            w.recover()
        self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_CONFLICT")
        self.assertIn("oah wiki recover --resolved", str(ctx.exception))
        with self.assertRaises(WikiError):  # the published archive duplicates entries still in log.md
            w.recover(resolved=True)
        (folder / "log_archive_01.md").unlink()
        self.assertTrue(w.recover(resolved=True).startswith("OAH_WIKI_RECOVERY_RESOLVED"))
        self.assertFalse(w.journal_path.exists())

    def test_corrupt_journal_is_never_reset(self):
        w, folder, _ = self.crashed("validated")
        w.journal_path.write_text("{not json", encoding="utf-8")
        before = snapshot(folder, w.state)
        with self.assertRaises(Exception):
            w.recover()
        data = json.loads(json.dumps({"version": 1}))
        w.journal_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(WikiError) as ctx:
            w.recover()
        self.assertEqual(ctx.exception.code, "OAH_WIKI_JOURNAL_CORRUPT")
        self.assertEqual(snapshot(folder), {k: v for k, v in before.items() if str(folder) in k})

    def test_concurrent_edit_refused_before_publication(self):
        w, folder = make_store(self, make_log(newest_first(20, body=40)))

        def editor(step: str) -> None:
            if step == "journal-written":
                (folder / "log.md").write_bytes((folder / "log.md").read_bytes() + b"late edit\n")

        with self.assertRaises(WikiError) as ctx:
            w.rollover(today=TODAY, fault=editor)
        self.assertEqual(ctx.exception.code, "OAH_WIKI_CONCURRENT_EDIT")
        self.assertFalse((folder / "log_archive_01.md").exists())
        self.assertFalse(w.journal_path.exists())
        self.assertTrue((folder / "log.md").read_bytes().endswith(b"late edit\n"))

    def test_concurrent_archive_change_refused(self):
        w, folder = make_store(self, make_log(newest_first(20, body=40)))
        w.rollover(today=TODAY)
        for i in range(12):
            w.log(f"n{i}", body="q" * 40, today=TODAY)

        def editor(step: str) -> None:
            if step == "journal-written":
                (folder / "log_archive_01.md").write_bytes(b"tampered")

        with self.assertRaises(WikiError) as ctx:
            w.rollover(today=TODAY, fault=editor)
        self.assertEqual(ctx.exception.code, "OAH_WIKI_CONCURRENT_EDIT")
        self.assertFalse((folder / "log_archive_02.md").exists())

    def test_existing_archive_path_is_never_replaced(self):
        w, folder = make_store(self, make_log(newest_first(20, body=40)))
        original = (folder / "log.md").read_bytes()

        def racer(step: str) -> None:
            if step == "validated":
                (folder / "log_archive_01.md").write_bytes(b"someone else's file")

        with self.assertRaises(WikiError) as ctx:
            w.rollover(today=TODAY, fault=racer)
        self.assertEqual(ctx.exception.code, "OAH_WIKI_CONCURRENT_EDIT")
        self.assertEqual((folder / "log_archive_01.md").read_bytes(), b"someone else's file")
        self.assertEqual((folder / "log.md").read_bytes(), original)
        self.assertFalse(w.journal_path.exists())

    def test_pending_journal_blocks_every_command(self):
        w, folder, _ = self.crashed("archive-published")
        (folder / "index.md").write_text("Notes:\n", encoding="utf-8")
        before = snapshot(folder, w.state)
        calls = {
            "lint": lambda: w.lint(),
            "lint --index": lambda: w.lint(index=True),
            "hygiene": lambda: w.hygiene(),
            "new": lambda: w.new("Fresh", today=TODAY),
            "log": lambda: w.log("Fresh", today=TODAY),
            "rollover": lambda: w.rollover(today=TODAY),
        }
        for name, call in calls.items():
            with self.subTest(name), self.assertRaises(WikiError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_PENDING")
            self.assertIn("oah wiki recover", str(ctx.exception))
            self.assertEqual(snapshot(folder, w.state), before)


if __name__ == "__main__":
    unittest.main()


class HardeningTest(unittest.TestCase):
    """Amendment 4 (A16 port): hash checks, revalidation, and recording (never touching) late changes."""

    def setUp(self):
        self.w, self.folder = make_store(self, make_log(newest_first(20, body=40)))
        self.log_path = self.folder / "log.md"
        self.original = self.log_path.read_bytes()
        self.stream = wiki_stream(self.folder)

    def wiki_files(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir())

    def test_byte_exact_move_with_hash_checks(self):
        plan = log.plan_rollover(self.original, {}, trigger=600, target=300, today=TODAY)
        entries = log.parse_log(self.original, "log.md").entries
        kept = b"".join(e.data for e in entries[:plan.kept])
        moved = b"".join(e.data for e in entries[plan.kept:])
        self.assertEqual(plan.entries_sha256, log.sha256(b"".join(e.data for e in entries)))
        self.assertEqual(log.sha256(kept + moved), plan.entries_sha256)
        result = self.w.rollover(today=TODAY)
        self.assertIn(f"entries_sha256={plan.entries_sha256}", result.line)
        on_disk = (log.entry_stream(self.log_path.read_bytes(), "log.md")
                   + log.entry_stream(result.archive.read_bytes(), result.archive.name))
        self.assertEqual(log.sha256(on_disk), plan.entries_sha256)
        self.assertEqual(wiki_stream(self.folder), self.stream)
        self.assertEqual(self.wiki_files(), ["log.md", "log_archive_01.md"])
        self.assertFalse(self.w.journal_path.exists())

    def test_oversized_newest_entry_is_kept_on_disk(self):
        w, folder = make_store(self, make_log([entry(30, 5000)] + newest_first(20, body=40)))
        stream = wiki_stream(folder)
        result = w.rollover(today=TODAY)
        self.assertIn("kept=1", result.line)
        self.assertIn("oversized=", result.line)
        entries = log.parse_log((folder / "log.md").read_bytes(), "log.md").entries
        self.assertEqual([e.data for e in entries], [entry(30, 5000)])
        self.assertEqual(wiki_stream(folder), stream)

    def test_refusals_happen_before_any_write(self):
        cases = {
            "no marker": b"# Log\n\n" + b"".join(newest_first(20, body=40)),
            "undated first entry heading": HEADER + b"## Undated heading\n" + b"".join(newest_first(20, body=40)),
            "unclosed code fence": make_log(newest_first(20, body=40)) + b"```\nopen\n",
        }
        for case, data in cases.items():
            for apply in (False, True):
                with self.subTest(case, apply=apply):
                    w, folder = make_store(self, data)
                    before = snapshot(folder, w.state)
                    with self.assertRaises(WikiError) as ctx:
                        w.rollover(today=TODAY, apply=apply)
                    self.assertEqual(ctx.exception.code, "OAH_WIKI_LOG_INVALID")
                    self.assertIn(case, str(ctx.exception))
                    self.assertEqual(snapshot(folder, w.state), before)

    def test_identity_change_refused_before_publication(self):
        def swap(step: str) -> None:
            if step == "journal-written":  # same bytes, but another file now has the name
                temp = self.folder / "other"
                temp.write_bytes(self.original)
                temp.replace(self.log_path)

        with self.assertRaises(WikiError) as ctx:
            self.w.rollover(today=TODAY, fault=swap)
        self.assertEqual(ctx.exception.code, "OAH_WIKI_CONCURRENT_EDIT")
        self.assertFalse((self.folder / "log_archive_01.md").exists())
        self.assertFalse(self.w.journal_path.exists())

    APPEND = b"## 2025-01-01 late append\n\nlate\n"

    def append_after_publication(self, step: str) -> None:
        if step == "log-replaced":  # a writer appends right after rollover replaced log.md
            with open(self.log_path, "ab") as handle:
                handle.write(self.APPEND)

    def assert_recorded_not_touched(self, fault, differ: str) -> dict:
        """A change right after publication: no file is touched, the journal records it, all refuse."""
        published = {}

        def watch(step: str) -> None:
            if step == "log-replaced":
                published["log"] = self.log_path.read_bytes()
                published["archive"] = (self.folder / "log_archive_01.md").read_bytes()
            fault(step)

        with self.assertRaises(WikiError) as ctx:
            self.w.rollover(today=TODAY, fault=watch)
        self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_PENDING")
        self.assertIn(differ, str(ctx.exception))
        live = snapshot(self.folder)
        journal = json.loads(self.w.journal_path.read_text(encoding="utf-8"))
        self.assertEqual((journal["phase"], journal["reason"]), ("changed-after-publication",) * 2)
        self.assertEqual(base64.b64decode(journal["current_log"]), self.log_path.read_bytes())
        self.assertEqual(base64.b64decode(journal["replacement_log"]), published["log"])
        for call in (lambda: self.w.lint(), lambda: self.w.log("x", today=TODAY), lambda: self.w.rollover(today=TODAY),
                     lambda: self.w.maintain_task("t1", today=TODAY)):
            with self.assertRaises(WikiError) as pending:
                call()
            self.assertEqual(pending.exception.code, "OAH_WIKI_RECOVERY_PENDING")
            self.assertIn("changed-after-publication", str(pending.exception))
        before = snapshot(self.folder, self.w.state)
        with self.assertRaises(WikiError) as conflict:
            self.w.recover()
        self.assertEqual(conflict.exception.code, "OAH_WIKI_RECOVERY_CONFLICT")
        self.assertIn(differ, str(conflict.exception))
        self.assertIn(str(self.w.journal_path), str(conflict.exception))
        self.assertIn("oah wiki recover --resolved", str(conflict.exception))
        self.assertEqual(snapshot(self.folder, self.w.state), before)
        self.assertEqual(snapshot(self.folder), live)
        return published

    def test_late_write_after_publication_is_recorded_not_restored(self):
        published = self.assert_recorded_not_touched(self.append_after_publication, "log.md")
        self.assertEqual(self.log_path.read_bytes(), published["log"] + self.APPEND)  # untouched
        self.assertTrue((self.folder / "log_archive_01.md").exists())

    def test_identity_change_after_publication_is_recorded(self):
        def swap(step: str) -> None:
            if step == "log-replaced":  # an editor rewrites log.md with the same bytes as a new file
                temp = self.folder / "other"
                temp.write_bytes(self.log_path.read_bytes())
                temp.replace(self.log_path)

        self.assert_recorded_not_touched(swap, "log.md")

    def test_archive_change_after_publication_is_recorded(self):
        def edit(step: str) -> None:
            if step == "log-replaced":
                with open(self.folder / "log_archive_01.md", "ab") as handle:
                    handle.write(b"edited archive\n")

        self.assert_recorded_not_touched(edit, "log_archive_01.md")

    def test_resolved_after_a_manual_fix(self):
        self.assert_recorded_not_touched(self.append_after_publication, "log.md")
        # The user decides the late text belongs to the newest kept entry and leaves both files as they are.
        line = self.w.recover(resolved=True)
        self.assertTrue(line.startswith("OAH_WIKI_RECOVERY_RESOLVED 2 files parse"), line)
        self.assertFalse(self.w.journal_path.exists())
        [record] = [json.loads(x) for x in (self.w.state / "recovery-resolutions.jsonl").read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual(record["reason"], "changed-after-publication")
        self.assertEqual(sorted(record["files"]), ["log.md", "log_archive_01.md"])
        self.assertTrue(self.w.log("works again", today=TODAY).title == "works again")
        self.assertEqual(self.w.recover(resolved=True), "OAH_WIKI_RECOVER_NOTHING nothing to recover")
        self.assertEqual(len(list((self.w.state / "resolved-records").glob("*.json"))), 1)

    def test_resolved_lists_a_dropped_entry_and_keeps_it_recoverable(self):
        self.assert_recorded_not_touched(self.append_after_publication, "log.md")
        journal_bytes = self.w.journal_path.read_bytes()
        moved = log.parse_log((self.folder / "log_archive_01.md").read_bytes(), "log_archive_01.md").entries
        (self.folder / "log_archive_01.md").unlink()  # a careless "fix": the moved entries are gone
        line = self.w.recover(resolved=True)
        self.assertTrue(line.startswith("OAH_WIKI_RECOVERY_RESOLVED 1 files parse"), line)
        self.assertIn(f"OAH_WIKI_RECOVERY_DROPPED {len(moved)} entries", line)
        for entry in moved:
            self.assertIn(f"'## {entry.date} {entry.title}'", line)
        self.assertFalse(self.w.journal_path.exists())
        [kept] = list((self.w.state / "resolved-records").glob("rollover-journal-*.json"))
        self.assertRegex(kept.name, r"^rollover-journal-\d{8}T\d{6}\.\d{6}Z\.json$")
        self.assertEqual(kept.read_bytes(), journal_bytes)  # moved, never rewritten or deleted
        original = base64.b64decode(json.loads(kept.read_text(encoding="utf-8"))["original_log"])
        for entry in moved:
            self.assertIn(entry.data, original)  # recoverable byte for byte
        [record] = [json.loads(x) for x in (self.w.state / "recovery-resolutions.jsonl").read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual(record["resolved_journal"], str(kept))
        self.assertEqual(len(record["dropped_entries"]), len(moved))

    def test_resolved_reports_exact_byte_changes_as_dropped(self):
        for change in ("crlf", "trailing whitespace"):
            with self.subTest(change=change):
                original_entry = entry(1, eol=b"\r\n")
                original = make_log([original_entry], eol=b"\r\n")
                w, folder = make_store(self, original, rollover_trigger_bytes=1, rollover_target_bytes=1)
                # Reconcile a real pending journal with an entry that differs only in its exact bytes.
                (folder / "log.md").write_bytes(make_log([entry(2, 700), original_entry]))
                with self.assertRaises(Crash):
                    w.rollover(today=TODAY, fault=crash_at("archive-published"))
                journal = json.loads(w.journal_path.read_text(encoding="utf-8"))
                replacement = base64.b64decode(journal["replacement_log"])
                archive_path = folder / journal["archive"]
                if change == "crlf":
                    altered = original_entry.replace(b"\r\n", b"\n")
                else:
                    altered = original_entry + b" \t\r\n"
                archive_path.write_bytes(archive_path.read_bytes().replace(original_entry, altered))
                (folder / "log.md").write_bytes(replacement)
                journal_bytes = w.journal_path.read_bytes()
                result = w.recover(resolved=True)
                self.assertIn("OAH_WIKI_RECOVERY_DROPPED 1 entries", result)
                [kept] = (w.state / "resolved-records").glob("*.json")
                self.assertEqual(kept.read_bytes(), journal_bytes)

    def test_dropped_report_deduplicates_exact_bytes_only(self):
        first = entry(1)
        second = first.replace(b"\n", b"\r\n")
        journal = {"archive": "log_archive_01.md", "original_log": make_log([first]),
                   "archive_bytes": make_log([first]), "current_log": make_log([second])}
        missing = self.w._dropped_entries(journal, set())
        self.assertEqual(len(missing), 2)
        self.assertIn("original_log", missing[0])
        self.assertIn("current_log", missing[1])

    def test_resolved_refuses_while_an_entry_is_duplicated(self):
        self.assert_recorded_not_touched(self.append_after_publication, "log.md")
        self.log_path.write_bytes(self.original)  # a "fix" that brings back entries the archive also holds
        before = snapshot(self.folder, self.w.state)
        with self.assertRaises(WikiError) as ctx:
            self.w.recover(resolved=True)
        self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_CONFLICT")
        self.assertIn("is in both log.md", str(ctx.exception))
        self.assertIn("log_archive_01.md", str(ctx.exception))
        self.assertEqual(snapshot(self.folder, self.w.state), before)
        (self.folder / "log_archive_01.md").unlink()  # the real fix: the archive goes, log.md keeps every entry
        self.assertTrue(self.w.recover(resolved=True).startswith("OAH_WIKI_RECOVERY_RESOLVED 1 files"))
        self.assertEqual(wiki_stream(self.folder), self.stream)

    def test_resolved_refuses_an_unparsable_log(self):
        self.assert_recorded_not_touched(self.append_after_publication, "log.md")
        self.log_path.write_bytes(b"no marker any more\n")
        before = snapshot(self.folder, self.w.state)
        with self.assertRaises(WikiError) as ctx:
            self.w.recover(resolved=True)
        self.assertEqual(ctx.exception.code, "OAH_WIKI_RECOVERY_CONFLICT")
        self.assertIn("no marker", str(ctx.exception))
        self.assertEqual(snapshot(self.folder, self.w.state), before)

    def test_dry_run_writes_nothing(self):
        before = snapshot(self.folder, self.w.state)
        result = self.w.rollover(today=TODAY, apply=False)
        self.assertTrue(result.line.startswith("OAH_WIKI_ROLLOVER_PLAN archive=log_archive_01.md"), result.line)
        self.assertIn("dry run", result.line)
        self.assertIsNone(result.archive)
        self.assertEqual(snapshot(self.folder, self.w.state), before)

