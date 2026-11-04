# 설계 저장소 검사 — revision·digest·상태·사유·포인터 일치, 기대 revision/digest 규칙, 재시도·고아 폴더, 중단·복구, 동시 저장, 위변조, 정본 브랜치·신선도를 실제 저장소에서 본다.
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from oah.design import DesignError, explain_binding, open_store
from oah.design import fs as design_fs
from oah.design import store as design_store
from oah.design.model import DesignBinding
from oah.wiki import notes
from oah.wiki.base import WikiSettings
from oah.wiki.store import Wiki
from tests.helpers import git_commit, git_env, make_git_repo

TODAY = datetime.date(2026, 11, 4)
UID = "0123456789abcdef0123456789abcdef"
SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
PAGE = "docs/wiki/designs/acme/acme--booking.md"


class Crash(Exception):
    """A simulated crash at a named step."""


def crash_at(step: str):
    def fault(name: str) -> None:
        if name == step:
            raise Crash(name)
    return fault


class Clock:
    """A clock that moves one second per call, so two runs of the same steps write the same bytes."""

    def __init__(self) -> None:
        self.moment = datetime.datetime(2026, 11, 4, 9, 0, tzinfo=datetime.timezone.utc)

    def __call__(self) -> datetime.datetime:
        self.moment += datetime.timedelta(seconds=1)
        return self.moment


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=git_env()).stdout


class DesignRepo:
    """Fixtures for real repositories with the packaged wiki scaffold, committed on main."""

    def make_repo(self, *, wiki_dir: str = "docs/wiki") -> pathlib.Path:
        repo = make_git_repo(self)
        files: dict = {f"{wiki_dir}/{rel}": data for rel, data in notes.scaffold_files(TODAY).items()}
        if wiki_dir != "docs/wiki":
            files[".oah/config.toml"] = f"[wiki]\ndir = {json.dumps(wiki_dir)}\n"
        git_commit(repo, files, "scaffold")
        return repo

    @staticmethod
    def state(repo: pathlib.Path) -> pathlib.Path:
        return repo.parent / "state"

    def store(self, repo: pathlib.Path, **options):
        options.setdefault("now", Clock())
        return open_store(repo, env={"OAH_STATE_DIR": str(self.state(repo))}, **options)

    def wiki(self, repo: pathlib.Path, **settings) -> Wiki:
        return Wiki(repo, WikiSettings(**settings), self.state(repo))

    @staticmethod
    def tree(*roots: pathlib.Path) -> dict[str, bytes]:
        out = {}
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root)
                if rel.parts and rel.parts[0] == ".git" or not path.is_file() or path.name == ".lock":
                    continue  # a lock file is created by any wiki command and holds nothing
                out[f"{root.name}/{rel.as_posix()}"] = path.read_bytes()
        return out

    def booking(self, store, *, text: str = "People book a lesson.\n", request: str | None = "I want lessons booked.\n"):
        store.add_project("acme", title="Acme", event="ev-project", today=TODAY)
        parts = {"design": text, **({"request": request} if request else {})}
        return store.new("acme", "booking", title="Booking", parts=parts, event="ev-r1", today=TODAY)

    def log_entries(self, repo: pathlib.Path, marker_prefix: str = "<!-- oah-design: ") -> list[str]:
        text = (repo / "docs/wiki/log.md").read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line.startswith(marker_prefix)]


class RevisionTest(DesignRepo, unittest.TestCase):
    def test_c1_retries_require_the_expected_digest(self):
        for explicit, expected in ((False, 1), (True, 1), (True, 2)):
            with self.subTest(explicit=explicit, expected=expected):
                repo = self.make_repo()
                store = self.store(repo)
                r1 = self.booking(store)
                assert r1.digest is not None
                r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest,
                                event="ev-two", reason="Two", parts={"design": "Two.\n"}, today=TODAY)
                assert r2.digest is not None
                before = self.tree(repo, self.state(repo))
                with self.assertRaises(DesignError) as stale:
                    store.save("acme", "booking", expect_revision=expected,
                               expect_digest=r1.digest if expected == 2 else r2.digest,
                               event="ev-two" if explicit else None, reason="Two", parts={"design": "Two.\n"},
                               today=TODAY)
                self.assertEqual(stale.exception.code, "OAH_DESIGN_CONTEXT_STALE")
                self.assertEqual(self.tree(repo, self.state(repo)), before)

    def test_c1_drop_and_ancestor_retries_use_the_original_base(self):
        repo = self.make_repo()
        store = self.store(repo)
        store.add_project("acme", title="Acme", today=TODAY)
        r1 = store.new("acme", "booking", title="Booking", today=TODAY,
                       parts={"design": "One.\n", "materials": "A.\n", "coverage": "Original.\n"})
        assert r1.digest is not None
        def replay(event):
            return store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest,
                              reason="Drop materials", drop=["materials"], event=event, today=TODAY)
        r2 = replay("ev-drop")
        before = self.tree(repo, self.state(repo))
        self.assertEqual(replay("ev-drop").action, "applied")
        self.assertEqual(self.tree(repo, self.state(repo)), before)
        assert r2.digest is not None
        r3 = store.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="Independent edit",
                        title="Lesson booking", parts={"coverage": "Expanded.\n"}, today=TODAY)
        before = self.tree(repo, self.state(repo))
        for event in ("ev-drop", None):
            result = replay(event)
            self.assertEqual((result.action, result.revision, result.current), ("applied", 2, 3))
        self.assertEqual(self.tree(repo, self.state(repo)), before)
        self.assertEqual(store.context("acme", "booking").current.digest, r3.digest)

    def test_c1_ancestor_retry_survives_later_title_and_part_changes(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        assert r1.digest is not None
        original_digest = r1.digest
        r2 = store.save("acme", "booking", expect_revision=1, expect_digest=original_digest, reason="Two",
                        parts={"design": "Two.\n"}, event="ev-two", today=TODAY)
        assert r2.digest is not None
        store.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="Independent change",
                   title="Lessons", parts={"request": "I want lessons booked.\nCorrection: groups too.\n"}, today=TODAY)
        before = self.tree(repo, self.state(repo))
        for event in ("ev-two", None):
            result = store.save("acme", "booking", expect_revision=1, expect_digest=original_digest, reason="Two",
                                parts={"design": "Two.\n"}, event=event, today=TODAY)
            self.assertEqual((result.action, result.revision, result.current), ("applied", 2, 3))
        self.assertEqual(self.tree(repo, self.state(repo)), before)

    def test_c1_accept_verifies_target_parts_before_any_success(self):
        for target, damage, already in ((1, "changed", False), (1, "missing", False),
                                        (2, "missing-page", False), (1, "changed", True)):
            with self.subTest(target=target, damage=damage, already=already):
                repo = self.make_repo()
                store = self.store(repo)
                r1 = self.booking(store)
                if already:
                    store.accept("acme", "booking", revision=1, digest=r1.digest, reason="Yes", today=TODAY)
                r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                                parts={"design": "Two.\n"}, today=TODAY)
                part = repo / design_fs.part_rel("acme", "booking", target, "design")
                if damage == "missing":
                    part.unlink()
                else:
                    part.write_text("Changed.\n", encoding="utf-8")
                if damage == "missing-page":
                    (repo / PAGE).unlink()
                before = self.tree(repo, self.state(repo))
                with self.assertRaises(DesignError) as corrupt:
                    store.accept("acme", "booking", revision=target, digest=r1.digest if target == 1 else r2.digest,
                                 reason="Yes", today=TODAY)
                self.assertEqual(corrupt.exception.code, "OAH_DESIGN_CORRUPT")
                self.assertEqual(self.tree(repo, self.state(repo)), before)

    def test_create_save_resume_keeps_one_consistent_history(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Add cancelling",
                        parts={"design": "People book and cancel a lesson.\n"}, today=TODAY)
        r3 = store.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="Rename",
                        title="Lesson booking", today=TODAY)
        self.assertEqual((r1.revision, r2.revision, r3.revision), (1, 2, 3))
        resumed = self.store(repo).context("acme", "booking")  # a fresh store, as a new session would open it
        self.assertEqual([info.revision for info in resumed.revisions], [3, 2, 1])
        self.assertEqual((resumed.current.revision, resumed.current.digest, resumed.status), (3, r3.digest, "draft"))
        self.assertEqual([info.reason for info in resumed.revisions], ["Rename", "Add cancelling", "Created"])
        self.assertEqual(resumed.revisions[0].parent, (2, resumed.revisions[1].manifest))
        self.assertEqual(resumed.title, "Lesson booking")
        first = self.store(repo).snapshot("acme", "booking", revision=1)
        self.assertEqual(first.texts, {"design": "People book a lesson.\n", "request": "I want lessons booked.\n"})
        pointer = json.loads((repo / ".oah/designs/acme/booking/design.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["current"], {"revision": 3, "digest": r3.digest,
                                              "manifest": resumed.revisions[0].manifest})
        page = (repo / PAGE).read_text(encoding="utf-8")
        self.assertIn(f"<!-- OAH:DESIGN revision=3 digest={r3.digest} format=1 -->", page)
        listing = (repo / "docs/wiki/designs/acme.md").read_text(encoding="utf-8")
        self.assertIn("- [[acme--booking|Lesson booking]]: revision 3, draft; nothing accepted yet", listing)
        self.assertEqual(len(self.log_entries(repo)), 4)  # registered, created, saved twice

    def test_wrong_revision_conflicts_and_wrong_digest_is_stale(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Second",
                        parts={"design": "Two.\n"}, today=TODAY)
        before = self.tree(repo, self.state(repo))
        with self.assertRaises(DesignError) as conflict:
            store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Third",
                       parts={"design": "Three.\n"}, today=TODAY)
        self.assertEqual((conflict.exception.code, conflict.exception.exit_code), ("OAH_DESIGN_REVISION_CONFLICT", 1))
        with self.assertRaises(DesignError) as stale:
            store.save("acme", "booking", expect_revision=2, expect_digest=r1.digest, reason="Third",
                       parts={"design": "Three.\n"}, today=TODAY)
        self.assertEqual((stale.exception.code, stale.exception.exit_code), ("OAH_DESIGN_CONTEXT_STALE", 1))
        self.assertEqual(self.tree(repo, self.state(repo)), before)
        self.assertEqual(store.context("acme", "booking").current.digest, r2.digest)

    def test_retried_save_is_applied_once(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        args = dict(expect_revision=1, expect_digest=r1.digest, reason="Two", parts={"design": "Two.\n"},
                    today=TODAY)
        saved = store.save("acme", "booking", event="ev-two", **args)
        again = store.save("acme", "booking", event="ev-two", **args)
        self.assertEqual((saved.action, again.action), ("saved", "applied"))
        self.assertEqual(again.revision, saved.revision)
        without_event = store.save("acme", "booking", **args)  # a retry that lost its event ID too
        self.assertEqual((without_event.action, without_event.revision), ("applied", 2))
        with self.assertRaises(DesignError) as reused:
            store.save("acme", "booking", expect_revision=2, expect_digest=saved.digest, reason="Other",
                       parts={"design": "Other.\n"}, event="ev-two", today=TODAY)
        self.assertEqual(reused.exception.code, "OAH_DESIGN_REVISION_CONFLICT")
        self.assertEqual([info.revision for info in store.context("acme", "booking").revisions], [2, 1])
        self.assertEqual(len(self.log_entries(repo)), 3)

    def test_identical_save_is_unchanged_and_writes_nothing(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        before = self.tree(repo, self.state(repo))
        result = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Same",
                            parts={"design": "People book a lesson.\r\n\r\n"}, today=TODAY)  # CRLF, extra newline
        self.assertEqual((result.action, result.revision), ("unchanged", 1))
        self.assertEqual(self.tree(repo, self.state(repo)), before)

    def test_orphan_revision_folder_never_counts_as_applied(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        change = dict(expect_revision=1, expect_digest=r1.digest, reason="Two", parts={"design": "Two.\n"},
                      today=TODAY)
        with self.assertRaises(Crash):
            store.save("acme", "booking", fault=crash_at("revision-published"), **change)
        # The journal is lost (as on another machine); r2 stays an orphan that no pointer names.
        (self.state(repo) / "wiki" / "design-journal.json").unlink()
        self.assertTrue((repo / ".oah/designs/acme/booking/revisions/r2/revision.json").is_file())
        context = store.context("acme", "booking")
        self.assertEqual(context.current.revision, 1)
        self.assertIn("OAH_DESIGN_ORPHAN_RECORD", [item.code for item in context.diagnostics])
        result = store.save("acme", "booking", **change)
        self.assertEqual((result.action, result.revision), ("saved", 3))
        self.assertEqual([info.revision for info in store.context("acme", "booking").revisions], [3, 1])

    def test_accept_moves_forward_and_keeps_evidence(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                        parts={"design": "Two.\n"}, today=TODAY)
        with self.assertRaises(DesignError) as wrong:
            store.accept("acme", "booking", revision=2, digest=r1.digest, reason="Owner approved", today=TODAY)
        self.assertEqual(wrong.exception.code, "OAH_DESIGN_CONTEXT_STALE")
        accepted = store.accept("acme", "booking", revision=2, digest=r2.digest, reason="Owner approved",
                                today=TODAY)
        self.assertEqual(accepted.action, "accepted")
        self.assertEqual(store.accept("acme", "booking", revision=2, digest=r2.digest, reason="Again",
                                      today=TODAY).action, "already-accepted")
        with self.assertRaises(DesignError) as backwards:
            store.accept("acme", "booking", revision=1, digest=r1.digest, reason="Back", today=TODAY)
        self.assertEqual(backwards.exception.code, "OAH_DESIGN_REVISION_CONFLICT")
        r3 = store.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="Three",
                        parts={"design": "Three.\n"}, today=TODAY)
        store.accept("acme", "booking", revision=3, digest=r3.digest, reason="Second approval", today=TODAY)
        context = self.store(repo).context("acme", "booking")
        self.assertEqual([(item.sequence, item.revision, item.reason) for item in context.acceptances],
                         [(1, 2, "Owner approved"), (2, 3, "Second approval")])
        self.assertEqual(context.acceptances[1].previous, (1, context.acceptances[0].record))
        self.assertEqual(context.status, "accepted")
        self.assertEqual(self.store(repo).snapshot("acme", "booking", revision=2).texts["design"], "Two.\n")
        self.assertTrue((repo / ".oah/designs/acme/booking/acceptances/a1.json").is_file())


class CrashTest(DesignRepo, unittest.TestCase):
    STEPS = ("journal-written", "revision-published", "pointer-written", f"wrote {PAGE}",
             "wrote docs/wiki/designs/acme.md", "wrote docs/wiki/index.md", "log-written")

    def test_c1_resolved_requires_current_and_accepted_part_bytes(self):
        for damaged_revision in (1, 2):
            with self.subTest(damaged_revision=damaged_revision):
                repo = self.make_repo()
                store = self.store(repo)
                r1 = self.booking(store)
                store.accept("acme", "booking", revision=1, digest=r1.digest, reason="Yes", today=TODAY)
                with self.assertRaises(Crash):
                    store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                               parts={"design": "Two.\n"}, fault=crash_at("pointer-written"), today=TODAY)
                part = repo / design_fs.part_rel("acme", "booking", damaged_revision, "design")
                original = part.read_bytes()
                part.write_bytes(b"Corrupt.\n")
                before = self.tree(repo, self.state(repo))
                with self.assertRaises(DesignError) as conflict:
                    store.recover(resolved=True)
                self.assertEqual(conflict.exception.code, "OAH_DESIGN_RECOVERY_CONFLICT")
                self.assertEqual(self.tree(repo, self.state(repo)), before)
                part.write_bytes(original)
                self.assertIn("OAH_DESIGN_RECOVERY_RESOLVED", store.recover(resolved=True))
                self.assertEqual(store.context("acme", "booking").current.text(), "Two.\n")


    def run_save(self, fault=None):
        repo = self.make_repo()
        with mock.patch("oah.design.store.new_uid", return_value=UID):
            store = self.store(repo)
            r1 = self.booking(store)
        change = dict(expect_revision=1, expect_digest=r1.digest, reason="Two", parts={"design": "Two.\n"},
                      title="Lesson booking", event="ev-two", today=TODAY)  # a new title also changes index.md
        if fault is None:
            store.save("acme", "booking", **change)
        else:
            with self.assertRaises(Crash):
                store.save("acme", "booking", fault=fault, **change)
        return repo, store

    def test_crash_at_every_step_recovers_to_the_uninterrupted_result(self):
        reference, _ = self.run_save()
        expected = {rel.split("/", 1)[1]: data for rel, data in self.tree(reference).items()}
        for step in self.STEPS:
            with self.subTest(step=step):
                repo, store = self.run_save(crash_at(step))
                committed = self.STEPS.index(step) >= self.STEPS.index("pointer-written")
                reader = self.store(repo).context("acme", "booking")
                self.assertEqual(reader.current.revision, 2 if committed else 1)
                self.assertTrue(reader.recovery.pending and reader.recovery.this_design)
                self.assertIn(reader.page_state, ("clean", "stale"))
                with self.assertRaises(DesignError) as pending:
                    store.save("acme", "booking", expect_revision=2, expect_digest="sha256:" + "0" * 64,
                               reason="More", parts={"design": "More.\n"}, today=TODAY)
                self.assertEqual((pending.exception.code, pending.exception.exit_code),
                                 ("OAH_DESIGN_RECOVERY_PENDING", 69))
                with mock.patch("oah.design.store.new_uid", return_value=UID):
                    self.assertTrue(store.recover().startswith("OAH_DESIGN_RECOVERED saved acme/booking"))
                actual = {rel.split("/", 1)[1]: data for rel, data in self.tree(repo).items()}
                self.assertEqual(actual, expected)
                self.assertEqual(len(self.log_entries(repo)), 3)
                self.assertEqual(store.recover(), "OAH_DESIGN_RECOVER_NOTHING nothing to recover")

    def test_crash_during_recover_then_recover_again(self):
        reference, _ = self.run_save()
        repo, store = self.run_save(crash_at("journal-written"))
        with self.assertRaises(Crash):
            store.recover(fault=crash_at("pointer-written"))
        store.recover()
        self.assertEqual({rel.split("/", 1)[1]: data for rel, data in self.tree(repo).items()},
                         {rel.split("/", 1)[1]: data for rel, data in self.tree(reference).items()})

    def test_crash_during_project_add_and_accept_recovers(self):
        repo = self.make_repo()
        store = self.store(repo)
        with self.assertRaises(Crash):
            store.add_project("acme", title="Acme", fault=crash_at("pointer-written"), today=TODAY)
        self.assertIn("registered acme", store.recover())
        self.assertTrue((repo / "docs/wiki/designs/acme.md").is_file())
        self.assertTrue((repo / ".oah/designs/README.md").is_file())
        r1 = store.new("acme", "booking", title="Booking", parts={"design": "One.\n"}, today=TODAY)
        with self.assertRaises(Crash):
            store.accept("acme", "booking", revision=1, digest=r1.digest, reason="Yes",
                         fault=crash_at("revision-published"), today=TODAY)
        self.assertIsNone(store.context("acme", "booking").accepted)
        store.recover()
        self.assertEqual(store.context("acme", "booking").status, "accepted")
        self.assertEqual(len(self.log_entries(repo)), 3)

    def test_changed_record_after_crash_is_a_recovery_conflict(self):
        repo, store = self.run_save(crash_at("revision-published"))
        part = repo / ".oah/designs/acme/booking/revisions/r2/design.md"
        part.write_text("Someone else.\n", encoding="utf-8")
        before = self.tree(repo, self.state(repo))
        with self.assertRaises(DesignError) as conflict:
            store.recover()
        self.assertEqual((conflict.exception.code, conflict.exception.exit_code), ("OAH_DESIGN_RECOVERY_CONFLICT", 69))
        self.assertEqual(self.tree(repo, self.state(repo)), before)
        lines = store.recover(resolved=True)
        self.assertIn("OAH_DESIGN_RECOVERY_RESOLVED", lines)
        kept = list((self.state(repo) / "wiki" / "resolved-records").glob("design-journal-*.json"))
        self.assertEqual(len(kept), 1)
        record = json.loads((self.state(repo) / "wiki" / "design-resolutions.jsonl").read_text().splitlines()[0])
        self.assertEqual((record["kind"], record["worktree"]), ("saved", "present"))
        self.assertEqual(store.context("acme", "booking").current.revision, 1)

    def test_journal_of_a_removed_worktree_is_refused_then_resolved(self):
        repo = self.make_repo()
        other = repo.parent / "other"
        git(repo, "worktree", "add", "-q", "-b", "design", str(other))
        store = self.store(other)
        store.add_project("acme", title="Acme", today=TODAY)
        r1 = store.new("acme", "booking", title="Booking", parts={"design": "One.\n"}, today=TODAY)
        with self.assertRaises(Crash):
            store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                       parts={"design": "Two.\n"}, fault=crash_at("journal-written"), today=TODAY)
        git(repo, "worktree", "remove", "--force", str(other))
        main = self.store(repo)
        with self.assertRaises(DesignError) as gone:
            main.recover()
        self.assertEqual(gone.exception.code, "OAH_DESIGN_RECOVERY_CONFLICT")
        self.assertIn("no longer exists", str(gone.exception))
        resolved = main.recover(resolved=True)
        self.assertIn("its worktree is gone", resolved)
        self.assertEqual(main.recover(), "OAH_DESIGN_RECOVER_NOTHING nothing to recover")

    def test_recover_refuses_a_worktree_now_on_another_branch(self):
        repo, store = self.run_save(crash_at("journal-written"))
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "wip")
        git(repo, "checkout", "-q", "-b", "elsewhere")
        with self.assertRaises(DesignError) as moved:
            store.recover()
        self.assertEqual(moved.exception.code, "OAH_DESIGN_RECOVERY_CONFLICT")
        self.assertIn("now on refs/heads/elsewhere", str(moved.exception))
        git(repo, "checkout", "-q", "main")
        self.assertIn("OAH_DESIGN_RECOVERED", store.recover())

    def test_wiki_moved_after_crash_gets_the_pages_and_entry(self):
        repo, store = self.run_save(crash_at("pointer-written"))
        (repo / "docs" / "wiki").rename(repo / "notes")
        (repo / ".oah" / "config.toml").write_text('[wiki]\ndir = "notes"\n', encoding="utf-8")
        lines = self.store(repo).recover()
        self.assertIn("the wiki moved from docs/wiki to notes", lines)
        self.assertIn("revision=2", (repo / "notes/designs/acme/acme--booking.md").read_text(encoding="utf-8"))
        self.assertEqual(sum(1 for line in (repo / "notes/log.md").read_text(encoding="utf-8").splitlines()
                             if line.startswith("<!-- oah-design: ")), 3)
        self.assertFalse((repo / "docs" / "wiki").exists())


class ConcurrencyTest(DesignRepo, unittest.TestCase):
    def test_c1_writes_keep_the_initial_record_baseline(self):
        for operation in ("save", "accept", "rename"):
            with self.subTest(operation=operation):
                repo = self.make_repo()
                store = self.store(repo)
                r1 = self.booking(store)
                pointer = repo / design_fs.pointer_rel("acme", "booking")
                project = repo / design_fs.project_record_rel("acme")
                target = project if operation == "rename" else pointer
                initial = target.read_bytes()
                if operation == "rename":
                    store.rename_project("acme", title="Intervening title", today=TODAY)
                else:
                    r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                                    parts={"design": "Two.\n"}, today=TODAY)
                    store.accept("acme", "booking", revision=2, digest=r2.digest, reason="Yes", today=TODAY)
                intervening = target.read_bytes()
                target.write_bytes(initial)
                if operation != "rename":
                    (repo / PAGE).unlink()
                loader = "load_project" if operation == "rename" else "load_design"
                original = getattr(design_store, loader)
                injected = []

                def advance(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if not injected:
                        injected.append(True)
                        target.write_bytes(intervening)
                    return result

                with mock.patch.object(design_store, loader, side_effect=advance):
                    with self.assertRaises(DesignError) as conflict:
                        if operation == "rename":
                            store.rename_project("acme", title="My title", today=TODAY)
                        elif operation == "accept":
                            store.accept("acme", "booking", revision=1, digest=r1.digest, reason="Mine", today=TODAY)
                        else:
                            store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Mine",
                                       parts={"design": "Mine.\n"}, today=TODAY)
                self.assertEqual(conflict.exception.code, "OAH_DESIGN_REVISION_CONFLICT")
                self.assertEqual(target.read_bytes(), intervening)
                self.assertFalse(store.journal_path.exists())

    def test_c1_reader_retries_when_a_generated_page_advances(self):
        for surface in ("context", "binding"):
            with self.subTest(surface=surface):
                repo = self.make_repo()
                writer = self.store(repo)
                r1 = self.booking(writer)
                reader = self.store(repo)
                read_page = reader._read_page
                advanced = []

                def advance(rel):
                    if not advanced:
                        advanced.append(writer.save("acme", "booking", expect_revision=1, expect_digest=r1.digest,
                                                    reason="Two", parts={"design": "Two.\n"}, today=TODAY))
                    return read_page(rel)

                with mock.patch.object(reader, "_read_page", side_effect=advance):
                    if surface == "context":
                        context = reader.context("acme", "booking")
                        self.assertIn((context.current.revision, context.current.text()),
                                      ((1, "People book a lesson.\n"), (2, "Two.\n")))
                        self.assertIn(context.page_state, ("clean", "stale"))
                        self.assertNotIn("OAH_DESIGN_UNREGISTERED_EDIT", [d.code for d in context.diagnostics])
                    else:
                        uid = writer.context("acme", "booking").project.uid
                        from oah.design.model import content_digest, text_digest

                        digest = content_digest(uid, "acme", "booking", "Booking",
                                                {"design": text_digest("Two.\n"),
                                                 "request": text_digest("I want lessons booked.\n")})
                        self.assertEqual(explain_binding(DesignBinding("acme", uid, "booking", 2, digest), reader)[0],
                                         "current")

    def cli(self, repo: pathlib.Path, *args: str) -> subprocess.Popen:
        env = dict(os.environ, PYTHONPATH=SRC, OAH_STATE_DIR=str(self.state(repo)))
        return subprocess.Popen([sys.executable, "-m", "oah", "design", *args], cwd=repo, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def race(self, other_text: str):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        (repo.parent / "other.md").write_text(other_text, encoding="utf-8")
        started = []

        def start_other(step: str) -> None:
            if step == "journal-written" and not started:  # the other writer now waits for the wiki lock
                started.append(self.cli(repo, "save", "acme", "booking", "--expect-revision", "1",
                                        "--expect-digest", r1.digest, "--reason", "Two",
                                        "--file", str(repo.parent / "other.md")))

        mine = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                          parts={"design": "Two.\n"}, fault=start_other, today=TODAY)
        out, err = started[0].communicate(timeout=60)
        return repo, store, mine, started[0].returncode, out, err

    def test_second_writer_with_the_same_expectation_conflicts(self):
        repo, store, mine, code, out, err = self.race("Something else.\n")
        self.assertEqual(mine.action, "saved")
        self.assertEqual(code, 1, out + err)
        self.assertIn("OAH_DESIGN_REVISION_CONFLICT", err)
        self.assertEqual([info.revision for info in store.context("acme", "booking").revisions], [2, 1])
        self.assertEqual(len(self.log_entries(repo)), 3)

    def test_second_writer_with_the_same_change_is_applied(self):
        repo, store, mine, code, out, err = self.race("Two.\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("OAH_DESIGN_APPLIED acme/booking r2", out)
        self.assertEqual(len(self.log_entries(repo)), 3)

    def test_reader_sees_a_whole_old_or_new_revision_at_every_step(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        reader = self.store(repo)
        seen = []

        def look(step: str) -> None:
            context = reader.context("acme", "booking")
            seen.append((step, context.current.revision, context.current.text(), context.page_state))

        store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                   parts={"design": "Two.\n"}, fault=look, today=TODAY)
        for step, revision, text, page_state in seen:
            committed = step not in ("journal-written", "revision-published")
            self.assertEqual((revision, text), (2, "Two.\n") if committed else (1, "People book a lesson.\n"), step)
            self.assertIn(page_state, ("clean", "stale"), step)


class TamperTest(DesignRepo, unittest.TestCase):
    def setUp(self):
        self.repo = self.make_repo()
        self.store_ = self.store(self.repo)
        r1 = self.booking(self.store_)
        self.r2 = self.store_.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                                   parts={"design": "Two.\n"}, today=TODAY)
        self.store_.accept("acme", "booking", revision=2, digest=self.r2.digest, reason="Yes", today=TODAY)
        self.folder = self.repo / ".oah/designs/acme/booking"

    def corrupted(self) -> DesignError:
        with self.assertRaises(DesignError) as caught:
            self.store(self.repo).context("acme", "booking")
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("OAH_DESIGN_CORRUPT", 69))
        return caught.exception

    def test_changed_part_byte_of_an_old_revision(self):
        (self.folder / "revisions/r1/design.md").write_text("People book a lesson!\n", encoding="utf-8")
        self.store(self.repo).context("acme", "booking")  # the current revision still reads
        with self.assertRaises(DesignError) as caught:
            self.store(self.repo).snapshot("acme", "booking", revision=1)
        self.assertEqual(caught.exception.code, "OAH_DESIGN_CORRUPT")

    def test_edited_reason_breaks_the_chain(self):
        path = self.folder / "revisions/r1/revision.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"Created"', '"Imported"'), encoding="utf-8")
        self.assertIn("revisions/r1/revision.json", str(self.corrupted()))
        uid = json.loads((self.repo / ".oah/designs/acme/project.json").read_text(encoding="utf-8"))["uid"]
        binding = DesignBinding("acme", uid, "booking", 2, self.r2.digest)
        self.assertEqual(explain_binding(binding, self.store(self.repo))[0], "invalid")

    def test_removed_acceptance_is_detected(self):
        (self.folder / "acceptances/a1.json").unlink()
        self.assertIn("acceptances/a1.json", str(self.corrupted()))

    def test_crlf_checkout_still_verifies(self):
        for path in self.folder.rglob("*"):
            if path.is_file():
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(self.store(self.repo).context("acme", "booking").current.text(), "Two.\n")

    def test_one_corrupt_design_never_blocks_another(self):
        store = self.store_
        first = store.new("acme", "payments", title="Payments", parts={"design": "Pay.\n"}, today=TODAY)
        index_before = (self.repo / "docs/wiki/index.md").read_text(encoding="utf-8")
        (self.folder / "design.json").write_text("{ not json", encoding="utf-8")
        summaries = {item.design: item.status for item in store.designs()}
        self.assertEqual(summaries, {"booking": "corrupt", "payments": "draft"})
        store.save("acme", "payments", expect_revision=1, expect_digest=first.digest, reason="Two",
                   parts={"design": "Pay twice.\n"}, today=TODAY)
        index = (self.repo / "docs/wiki/index.md").read_text(encoding="utf-8")
        self.assertIn("  - [[acme--booking|Booking]]", index)
        self.assertEqual(index, index_before)
        listing = (self.repo / "docs/wiki/designs/acme.md").read_text(encoding="utf-8")
        self.assertIn("- [[acme--booking|Booking]]: revision 2, accepted", listing)
        self.assertIn("- [[acme--payments|Payments]]: revision 2, draft", listing)
        result = store.reconcile()
        self.assertIn("OAH_DESIGN_CORRUPT", [item.code for item in result.diagnostics])


class BindingTest(DesignRepo, unittest.TestCase):
    def test_binding_states(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        context = store.context("acme", "booking")
        bound = context.binding()
        self.assertEqual(explain_binding(bound, store)[0], "current")
        self.assertEqual(explain_binding(None, store)[0], "unbound")
        self.assertEqual(explain_binding(DesignBinding("acme", bound.uid, "nothing", 1, r1.digest), store)[0],
                         "missing")
        forged = DesignBinding("acme", bound.uid, "booking", 1, "sha256:" + "1" * 64)
        self.assertEqual(explain_binding(forged, store)[0], "invalid")
        self.assertEqual(explain_binding(bound, store, against="accepted")[0], "stale")
        page = repo / PAGE
        original = page.read_bytes()
        page.write_bytes(original.replace(b"People book a lesson.", b"People book a lesson quickly."))
        self.assertEqual(explain_binding(bound, store)[0], "edited")
        self.assertEqual(store.context("acme", "booking").page_state, "edited")
        page.write_bytes(original)
        r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                        parts={"design": "Two.\n"}, today=TODAY)
        self.assertEqual(explain_binding(bound, store)[0], "stale")
        self.assertEqual(DesignBinding.from_json(bound.to_json()), bound)
        with self.assertRaises(ValueError):
            DesignBinding.from_json({**bound.to_json(), "uid": "x"})
        self.assertEqual(bound.line(), f"DESIGN acme/booking@r1 {r1.digest}")
        self.assertNotEqual(r2.digest, r1.digest)

    def test_same_ids_and_text_in_another_clone_never_match(self):
        first, second = self.make_repo(), self.make_repo()
        a, b = self.store(first), self.store(second)
        ra, rb = self.booking(a), self.booking(b)
        self.assertNotEqual(ra.digest, rb.digest)  # the project uid enters every digest
        borrowed = a.context("acme", "booking").binding()
        state, reason = explain_binding(borrowed, b)
        self.assertEqual(state, "invalid")
        self.assertIn("another project with the same ID", reason)


class OutputPathsTest(DesignRepo, unittest.TestCase):
    def test_output_paths_cover_every_file_a_write_changes(self):
        settings = WikiSettings(dir="notes/wiki")
        self.assertEqual(design_fs.design_output_paths(settings), (".oah/designs/", "notes/wiki/designs/",
                                                                   "notes/wiki/index.md", "notes/wiki/log.md"))
        for rel, expected in ((".oah/designs/acme/project.json", True), ("notes/wiki/designs/acme.md", True),
                              ("notes/wiki/index.md", True), ("notes/wiki/log.md", True),
                              ("notes/wiki/other.md", False), (".oah/designsx/a.json", False),
                              (".oah/tasks/t1/PACKET.md", False), ("notes/wiki/log_archive_01.md", False)):
            with self.subTest(rel=rel):
                self.assertEqual(design_fs.is_design_output(settings, rel), expected)
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        written = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                             title="Lesson booking", parts={"design": "Two.\n"}, today=TODAY).paths
        self.assertIn(".oah/designs/acme/booking/design.json", written)
        self.assertTrue(all(design_fs.is_design_output(WikiSettings(), rel) for rel in written), written)
        changed = git(repo, "status", "--porcelain", "-uall").splitlines()
        self.assertTrue(all(design_fs.is_design_output(WikiSettings(), line[3:]) for line in changed), changed)


class AuthorityTest(DesignRepo, unittest.TestCase):
    def test_c1_worktree_predating_registration_reads_without_local_writes(self):
        repo = self.make_repo()
        task = repo.parent / "early-task"
        git(repo, "worktree", "add", "-q", "-b", "early-task", str(task))
        main = self.store(repo)
        r1 = self.booking(main)
        git_commit(repo, {}, "register after task checkout")
        before = self.tree(task, self.state(task))
        store = self.store(task)
        context = store.context("acme", "booking")
        self.assertEqual((context.source, context.freshness, context.current.digest),
                         ("authority", "behind", r1.digest))
        self.assertEqual(store.snapshot("acme", "booking").text(), "People book a lesson.\n")
        self.assertEqual(explain_binding(context.binding(), store)[0], "current")
        self.assertEqual([p.id for p in store.projects()], ["acme"])
        self.assertEqual([(d.design, d.current, d.freshness) for d in store.designs()],
                         [("booking", (1, r1.digest), "behind")])
        self.assertEqual(self.tree(task, self.state(task)), before)
        self.assertFalse((task / ".oah/designs/acme/project.json").exists())

    def test_c1_lists_use_committed_authority_metadata(self):
        r2 = self.main.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest, reason="Two",
                            title="Lessons", parts={"design": "Two.\n"}, today=TODAY)
        self.main.accept("acme", "booking", revision=2, digest=r2.digest, reason="Yes", today=TODAY)
        second = self.main.new("acme", "payments", title="Payments", parts={"design": "Pay.\n"}, today=TODAY)
        self.main.rename_project("acme", title="Acme Web", today=TODAY)
        git_commit(self.repo, {}, "new authoritative metadata")
        before = self.tree(self.task, self.state(self.task))
        store = self.store(self.task)
        self.assertEqual([(p.id, p.title) for p in store.projects()], [("acme", "Acme Web")])
        self.assertEqual([(d.design, d.title, d.current, d.accepted, d.status, d.freshness)
                          for d in store.designs("acme")],
                         [("booking", "Lessons", (2, r2.digest), (2, r2.digest), "accepted", "behind"),
                          ("payments", "Payments", (1, second.digest), None, "draft", "behind")])
        self.assertEqual(self.tree(self.task, self.state(self.task)), before)

    def test_c1_authority_expressions_and_copied_locators_cannot_be_current(self):
        self.main.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest, reason="Two",
                       parts={"design": "Two.\n"}, today=TODAY)
        git_commit(self.repo, {}, "r2")
        git(self.repo, "branch", "copied", "main~1")
        path = self.task / ".oah/designs/acme/project.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for ref in ("refs/heads/main~1", "refs/heads/copied"):
            with self.subTest(ref=ref):
                path.write_text(json.dumps(dict(data, authority=ref)), encoding="utf-8")
                before = self.tree(self.task, self.state(self.task))
                store = self.store(self.task)
                binding = DesignBinding("acme", data["uid"], "booking", 1, self.r1.digest)
                self.assertIn(explain_binding(binding, store)[0], ("invalid", "unknown"))
                self.assertEqual(self.tree(self.task, self.state(self.task)), before)

    def test_c1_discovery_reports_absent_and_ambiguous_authority(self):
        repo = self.make_repo()
        early = repo.parent / "early"
        other = repo.parent / "other"
        git(repo, "worktree", "add", "-q", "-b", "early", str(early))
        git(repo, "worktree", "add", "-q", "-b", "other", str(other))
        store = self.store(early)
        with self.assertRaises(DesignError) as missing:
            store.context("acme", "booking")
        self.assertEqual(missing.exception.code, "OAH_DESIGN_NOT_FOUND")
        self.booking(self.store(repo))
        git_commit(repo, {}, "main project")
        self.booking(self.store(other))
        git_commit(other, {}, "other project")
        before = self.tree(early, self.state(early))
        with self.assertRaises(DesignError) as ambiguous:
            store.context("acme", "booking")
        self.assertEqual(ambiguous.exception.code, "OAH_DESIGN_FRESHNESS_UNKNOWN")
        self.assertIn("ambiguous", str(ambiguous.exception).lower())
        self.assertIn("OAH_DESIGN_FRESHNESS_UNKNOWN", [d.code for d in store.diagnostics()])
        self.assertEqual(self.tree(early, self.state(early)), before)

    def setUp(self):
        self.repo = self.make_repo()
        self.main = self.store(self.repo)
        self.r1 = self.booking(self.main)
        git_commit(self.repo, {}, "design r1")
        self.task = self.repo.parent / "task"
        git(self.repo, "worktree", "add", "-q", "-b", "task", str(self.task))

    def test_writes_refuse_outside_the_authority_branch_and_on_detached_head(self):
        task = self.store(self.task)
        for call in (lambda: task.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest,
                                       reason="x", parts={"design": "x\n"}, today=TODAY),
                     lambda: task.new("acme", "other", title="Other", parts={"design": "x\n"}, today=TODAY),
                     lambda: task.accept("acme", "booking", revision=1, digest=self.r1.digest, reason="x",
                                         today=TODAY),
                     lambda: task.rename_project("acme", title="Elsewhere", today=TODAY)):
            with self.assertRaises(DesignError) as refused:
                call()
            self.assertEqual((refused.exception.code, refused.exception.exit_code), ("OAH_DESIGN_NOT_AUTHORITY", 1))
            self.assertIn("change only on main", str(refused.exception))
        git(self.task, "checkout", "-q", "--detach")
        with self.assertRaises(DesignError) as detached:
            self.store(self.task).add_project("other", title="Other", today=TODAY)
        self.assertEqual(detached.exception.code, "OAH_DESIGN_NOT_AUTHORITY")
        self.assertIn("detached", str(detached.exception))
        self.assertEqual(json.loads((self.repo / ".oah/designs/acme/project.json").read_text())["authority"],
                         "refs/heads/main")

    def test_other_branch_reads_the_committed_authority_copy_with_freshness(self):
        r2 = self.main.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest, reason="Two",
                            parts={"design": "Two.\n"}, today=TODAY)
        git_commit(self.repo, {}, "design r2")
        context = self.store(self.task).context("acme", "booking")
        self.assertEqual((context.source, context.freshness, context.current.revision), ("authority", "behind", 2))
        self.assertEqual(context.current.text(), "Two.\n")
        self.assertEqual(context.page_state, "clean")  # the task worktree's own page shows its own r1
        codes = [item.code for item in context.diagnostics]
        self.assertIn("OAH_DESIGN_BEHIND", codes)
        self.assertIn("OAH_DESIGN_NOT_AUTHORITY", codes)
        self.assertEqual({item.design: item.freshness for item in self.store(self.task).designs()},
                         {"booking": "behind"})
        git(self.task, "merge", "-q", "--ff-only", "main")
        merged = self.store(self.task).context("acme", "booking")
        self.assertEqual((merged.freshness, merged.current.digest), ("current", r2.digest))
        self.main.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="Three",
                       parts={"design": "Three.\n"}, today=TODAY)  # saved on main, not committed yet
        self.assertEqual(self.store(self.task).context("acme", "booking").current.revision, 2)
        self.assertEqual(self.main.context("acme", "booking").current.revision, 3)

    def test_unreadable_authority_gives_unknown_freshness(self):
        project = self.task / ".oah/designs/acme/project.json"
        data = json.loads(project.read_text(encoding="utf-8"))
        data["authority"] = "refs/heads/gone"
        project.write_text(json.dumps(data), encoding="utf-8")
        store = self.store(self.task)
        context = store.context("acme", "booking")
        self.assertEqual((context.source, context.freshness, context.current.revision), ("worktree", "unknown", 1))
        self.assertIn("OAH_DESIGN_FRESHNESS_UNKNOWN", [item.code for item in context.diagnostics])
        self.assertEqual(explain_binding(context.binding(), store)[0], "unknown")


if __name__ == "__main__":
    unittest.main()
