# 설계 위키 검사 — 프로젝트별 목록·설계 페이지·로그가 반복·중단·롤오버에도 한 번씩만 남고, 두 프로젝트·사용자 내용·wiki.dir·직접 편집·이름 변경이 올바른지 본다.
from __future__ import annotations

import json
import pathlib
import subprocess
import unittest

from oah import intent, packet
from oah.design import DesignError
from oah.design.model import event_key
from oah.hook_pre_commit import check as commit_check
from oah.wiki import log as log_module
from oah.wiki import notes
from oah.wiki.base import WikiSettings
from oah.wiki.store import Wiki
from tests.helpers import git_commit, git_env
from tests.test_design_store import PAGE, TODAY, Crash, DesignRepo, crash_at


def markers(repo: pathlib.Path, wiki: str = "docs/wiki") -> list[str]:
    """Every design event marker in log.md and its archives."""
    out = []
    for path in sorted((repo / wiki).glob("log*.md")):
        out += [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("<!-- oah-design: ")]
    return out


def block(text: str) -> list[str]:
    lines = text.splitlines()
    return lines[lines.index("<!-- OAH:DESIGN_INDEX:BEGIN -->"):lines.index("<!-- OAH:DESIGN_INDEX:END -->") + 1]


class ProjectsTest(DesignRepo, unittest.TestCase):
    def test_two_projects_with_one_title_and_one_design_id_stay_apart(self):
        repo = self.make_repo()
        store = self.store(repo)
        store.add_project("shop-a", title="Shop", today=TODAY)
        store.add_project("shop-b", title="Shop", today=TODAY)
        a1 = store.new("shop-a", "checkout", title="Checkout", parts={"design": "Card payments.\n"}, today=TODAY)
        store.new("shop-b", "checkout", title="Checkout", parts={"design": "Cash on delivery.\n"}, today=TODAY)
        others = {rel: data for rel, data in self.tree(repo).items() if "shop-b" in rel}
        store.save("shop-a", "checkout", expect_revision=1, expect_digest=a1.digest, reason="Add refunds",
                   parts={"design": "Card payments and refunds.\n"}, today=TODAY)
        self.assertEqual({rel: data for rel, data in self.tree(repo).items() if "shop-b" in rel}, others)
        self.assertEqual(store.snapshot("shop-b", "checkout").text(), "Cash on delivery.\n")
        self.assertEqual(store.snapshot("shop-a", "checkout").text(), "Card payments and refunds.\n")
        self.assertEqual(block((repo / "docs/wiki/index.md").read_text(encoding="utf-8")), [
            "<!-- OAH:DESIGN_INDEX:BEGIN -->", "Designs:", "- [[designs/shop-a|Shop]]",
            "  - [[shop-a--checkout|Checkout]]", "- [[designs/shop-b|Shop]]", "  - [[shop-b--checkout|Checkout]]",
            "<!-- OAH:DESIGN_INDEX:END -->"])
        self.assertEqual(len(markers(repo)), len(set(markers(repo))))
        self.assertEqual(len(markers(repo)), 5)
        self.assertEqual(self.wiki(repo).lint(now=0).summary(), "OAH_WIKI_LINT_CLEAN notes=5")
        self.assertEqual(self.wiki(repo).lint(now=0, index=True).problems, [])

    def test_repeats_and_retries_change_nothing(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store)
        first = store.reconcile()
        before = self.tree(repo, self.state(repo))
        second = store.reconcile()
        self.assertEqual((len(first.written), len(second.written)), (0, 0))
        self.assertEqual(store.add_project("acme", title="Acme", today=TODAY).action, "present")
        self.assertEqual(store.new("acme", "booking", title="Booking", today=TODAY,
                                   parts={"design": "People book a lesson.\n",
                                          "request": "I want lessons booked.\n"}).action, "applied")
        self.assertEqual(self.tree(repo, self.state(repo)), before)
        with self.assertRaises(DesignError) as other:
            store.new("acme", "booking", title="Booking", parts={"design": "Else.\n"}, today=TODAY)
        self.assertEqual(other.exception.code, "OAH_DESIGN_EXISTS")
        self.assertEqual(r1.revision, 1)

    def test_reconcile_without_designs_leaves_index_alone(self):
        repo = self.make_repo()
        before = (repo / "docs/wiki/index.md").read_bytes()
        self.assertEqual(self.store(repo).reconcile().written, ())
        self.assertEqual((repo / "docs/wiki/index.md").read_bytes(), before)

    def test_configured_wiki_folder_is_used(self):
        for wiki_dir in ("notes/wiki", "absolute"):
            with self.subTest(wiki_dir=wiki_dir):
                repo = self.make_repo(wiki_dir="notes/wiki")
                if wiki_dir == "absolute":
                    (repo / ".oah/config.toml").write_text(f"[wiki]\ndir = {json.dumps(str(repo / 'notes/wiki'))}\n",
                                                           encoding="utf-8")
                store = self.store(repo)
                self.booking(store)
                self.assertTrue((repo / "notes/wiki/designs/acme/acme--booking.md").is_file())
                self.assertTrue((repo / ".oah/designs/acme/booking/design.json").is_file())
                self.assertFalse((repo / "docs").exists())
                self.assertEqual(len(markers(repo, "notes/wiki")), 2)

    def test_changed_wiki_folder_gets_its_pages_from_reconcile(self):
        repo = self.make_repo()
        store = self.store(repo)
        self.booking(store)
        old = self.tree(repo / "docs")
        for rel, data in notes.scaffold_files(TODAY).items():
            path = repo / "notes" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (repo / ".oah/config.toml").write_text('[wiki]\ndir = "notes"\n', encoding="utf-8")
        moved = self.store(repo)
        self.assertEqual(moved.context("acme", "booking").page_state, "missing")
        result = moved.reconcile()
        self.assertIn("notes/designs/acme/acme--booking.md", result.written)
        self.assertEqual(moved.context("acme", "booking").page_state, "clean")
        self.assertEqual(self.tree(repo / "docs"), old)


class UserContentTest(DesignRepo, unittest.TestCase):
    def test_bytes_outside_the_generated_parts_are_kept(self):
        repo = self.make_repo()
        wiki = repo / "docs/wiki"
        index = wiki / "index.md"
        index.write_bytes(index.read_bytes().replace(b"\n", b"\r\n") + b"\r\nMy own words.\r\n")
        Wiki(repo, WikiSettings(), self.state(repo)).log("Earlier decision", body="Kept as written.", today=TODAY)
        (wiki / "log_archive_01.md").write_text("# Log archive 01\n\n<!-- OAH:LOG_INSERT_BELOW -->\n\n"
                                                "## 2026-01-01 Old\n\nOld entry.\n", encoding="utf-8")
        (wiki / "raw").mkdir()
        (wiki / "raw/source.txt").write_bytes(b"imported\r\n")
        log_before = (wiki / "log.md").read_bytes()
        archive_before = (wiki / "log_archive_01.md").read_bytes()
        index_before = index.read_bytes()
        store = self.store(repo)
        r1 = self.booking(store)
        project_page = wiki / "designs/acme.md"
        project_page.write_bytes(project_page.read_bytes() + b"\nOur own notes about Acme.\n")
        store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                   parts={"design": "Two.\n"}, today=TODAY)
        after = index.read_bytes()
        begin = after.index(b"<!-- OAH:DESIGN_INDEX:BEGIN -->")
        self.assertEqual(after[:begin], index_before + b"\r\n")
        self.assertIn(b"  - [[acme--booking|Booking]]\r\n", after)
        self.assertTrue(project_page.read_bytes().endswith(b"<!-- OAH:DESIGN_LIST:END -->\n\nOur own notes about "
                                                           b"Acme.\n"))
        old = log_module.parse_log(log_before, "log.md").entries
        new = log_module.parse_log((wiki / "log.md").read_bytes(), "log.md").entries
        self.assertEqual([entry.data for entry in new[-len(old):]], [entry.data for entry in old])
        self.assertEqual((wiki / "log_archive_01.md").read_bytes(), archive_before)
        self.assertEqual((wiki / "raw/source.txt").read_bytes(), b"imported\r\n")

    def test_lint_and_the_commit_hook_see_only_links_written_in_parts(self):
        repo = self.make_repo()
        (repo / ".oah").mkdir(exist_ok=True)
        (repo / ".oah/config.toml").write_text('[wiki]\nnew_broken_links = "block"\n', encoding="utf-8")
        git_commit(repo, {}, "block new broken links")
        store = self.store(repo)
        r1 = self.booking(store)
        r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="See [[it]] | now",
                        title="Booking [draft] | v2", parts={"design": "Two.\n"}, today=TODAY)
        store.accept("acme", "booking", revision=2, digest=r2.digest, reason="Yes", today=TODAY)
        store.rename_project("acme", title="Acme [web]", today=TODAY)
        store.reconcile()
        wiki = self.wiki(repo)
        self.assertEqual(wiki.lint(now=0).problems, [])
        self.assertEqual(wiki.lint(now=0, index=True).problems, [])
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=git_env())
        settings = WikiSettings(new_broken_links="block")
        self.assertEqual(commit_check(repo, settings, self.state(repo)).blocked, [])
        current = store.context("acme", "booking").current
        store.save("acme", "booking", expect_revision=current.revision, expect_digest=current.digest,
                   reason="Link a note", parts={"design": "See [[missing-note]].\n"}, today=TODAY)
        problems = wiki.lint(now=0).problems
        self.assertEqual([(item.kind, item.path) for item in problems],
                         [("broken_link", "designs/acme/acme--booking.md")])
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=git_env())
        blocked = commit_check(repo, settings, self.state(repo)).blocked
        self.assertEqual(len(blocked), 1)
        self.assertIn("acme--booking.md: [[missing-note]] does not resolve", blocked[0])
        heading = (repo / PAGE).read_text(encoding="utf-8").splitlines()[7]
        self.assertEqual(heading, "# Booking draft v2")

    def test_files_without_the_oah_mark_are_never_touched(self):
        repo = self.make_repo()
        own = repo / "docs/wiki/designs/acme.md"
        own.parent.mkdir(parents=True)
        own.write_text("---\ntitle: My acme notes\n---\nMine.\n", encoding="utf-8")
        before = self.tree(repo, self.state(repo))
        store = self.store(repo)
        with self.assertRaises(DesignError) as refused:
            store.add_project("acme", title="Acme", today=TODAY)
        self.assertEqual(refused.exception.code, "OAH_DESIGN_PAGE_FOREIGN")
        self.assertEqual(self.tree(repo, self.state(repo)), before)
        own.rename(repo / "docs/wiki/my-acme.md")
        store.add_project("acme", title="Acme", today=TODAY)
        page = repo / PAGE
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("A note of my own.\n", encoding="utf-8")
        with self.assertRaises(DesignError) as foreign:
            store.new("acme", "booking", title="Booking", parts={"design": "x\n"}, today=TODAY)
        self.assertEqual(foreign.exception.code, "OAH_DESIGN_PAGE_FOREIGN")
        self.assertEqual(page.read_text(encoding="utf-8"), "A note of my own.\n")


class PageEditTest(DesignRepo, unittest.TestCase):
    def setUp(self):
        self.repo = self.make_repo()
        self.store_ = self.store(self.repo)
        self.r1 = self.booking(self.store_)
        self.page = self.repo / PAGE

    def test_direct_edit_is_reported_refused_and_registered(self):
        self.page.write_text(self.page.read_text(encoding="utf-8").replace("People book a lesson.",
                                                                           "People book a lesson online."),
                             encoding="utf-8")
        context = self.store_.context("acme", "booking")
        self.assertEqual(context.page_state, "edited")
        self.assertIn("OAH_DESIGN_UNREGISTERED_EDIT", [item.code for item in context.diagnostics])
        for call in (lambda: self.store_.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest,
                                              reason="x", parts={"design": "x\n"}, today=TODAY),
                     lambda: self.store_.accept("acme", "booking", revision=1, digest=self.r1.digest, reason="x",
                                                today=TODAY)):
            with self.assertRaises(DesignError) as refused:
                call()
            self.assertEqual(refused.exception.code, "OAH_DESIGN_UNREGISTERED_EDIT")
        saved = self.store_.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest,
                                 reason="The owner edited the page", from_wiki=True, today=TODAY)
        after = self.store_.context("acme", "booking")
        self.assertEqual((saved.revision, after.current.info.origin, after.page_state), (2, "wiki-edit", "clean"))
        self.assertEqual(after.current.text(), "People book a lesson online.\n")

    def test_edit_made_on_a_stale_page_is_a_revision_conflict(self):
        old_page = self.page.read_bytes()
        self.store_.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest, reason="Two",
                         parts={"design": "Two.\n"}, today=TODAY)
        self.page.write_bytes(old_page.replace(b"People book a lesson.", b"People book a lesson by phone."))
        current = self.store_.context("acme", "booking").current
        with self.assertRaises(DesignError) as conflict:
            self.store_.save("acme", "booking", expect_revision=current.revision, expect_digest=current.digest,
                             reason="x", from_wiki=True, today=TODAY)
        self.assertEqual(conflict.exception.code, "OAH_DESIGN_REVISION_CONFLICT")
        self.assertIn("edited from revision 1", str(conflict.exception))

    def test_header_edit_and_lost_markers_are_refused_without_a_rewrite(self):
        text = self.page.read_text(encoding="utf-8")
        self.page.write_text(text.replace("# Booking\n", "# Booking\n\nA note above the markers.\n"),
                             encoding="utf-8")
        self.assertEqual(self.store_.context("acme", "booking").page_state, "edited")
        with self.assertRaises(DesignError):
            self.store_.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest, reason="x",
                             parts={"design": "x\n"}, today=TODAY)
        result = self.store_.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest,
                                  reason="Take the page", from_wiki=True, today=TODAY)
        self.assertEqual(result.action, "unchanged")
        self.assertEqual(self.page.read_text(encoding="utf-8"), text)  # --from-wiki rewrote the header on request
        merged = text.replace("<!-- OAH:DESIGN_PART request -->\n", "")  # the request text joins the design part
        self.page.write_text(merged, encoding="utf-8")
        with self.assertRaises(DesignError) as dropped:
            self.store_.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest, reason="x",
                             from_wiki=True, today=TODAY)
        self.assertEqual(dropped.exception.code, "OAH_DESIGN_INPUT_INVALID")  # the request part cannot disappear
        self.assertEqual(self.page.read_text(encoding="utf-8"), merged)
        broken = "\n".join(line for line in text.split("\n") if not line.startswith("<!-- OAH:DESIGN revision="))
        self.page.write_text(broken, encoding="utf-8")
        with self.assertRaises(DesignError) as unreadable:
            self.store_.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest, reason="x",
                             from_wiki=True, today=TODAY)
        self.assertEqual(unreadable.exception.code, "OAH_DESIGN_UNREGISTERED_EDIT")
        self.assertEqual(self.page.read_text(encoding="utf-8"), broken)
        self.assertEqual(self.store_.context("acme", "booking").page_state, "edited")

    def test_unclosed_fence_in_a_part_keeps_every_marker(self):
        r2 = self.store_.save("acme", "booking", expect_revision=1, expect_digest=self.r1.digest, reason="Code",
                              parts={"design": "Example:\n```python\nprint('open')\n"}, today=TODAY)
        self.assertEqual(self.store_.context("acme", "booking").page_state, "clean")
        self.page.write_text(self.page.read_text(encoding="utf-8").replace(
            "I want lessons booked.\n", "I want lessons booked.\nCorrection: only weekdays.\n"), encoding="utf-8")
        saved = self.store_.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="Weekdays",
                                 from_wiki=True, today=TODAY)
        texts = self.store_.snapshot("acme", "booking", saved.revision).texts
        self.assertEqual(texts["design"], "Example:\n```python\nprint('open')\n")
        self.assertEqual(texts["request"], "I want lessons booked.\nCorrection: only weekdays.\n")

    def test_deleted_page_comes_back_with_reconcile(self):
        self.page.unlink()
        self.assertEqual(self.store_.context("acme", "booking").page_state, "missing")
        self.assertIn(PAGE, self.store_.reconcile().written)
        self.assertEqual(self.store_.context("acme", "booking").page_state, "clean")


class LogTest(DesignRepo, unittest.TestCase):
    def test_entry_rolled_into_an_archive_is_not_added_again(self):
        repo = self.make_repo()
        (repo / ".oah").mkdir(exist_ok=True)
        (repo / ".oah/config.toml").write_text("[wiki]\nrollover_trigger_bytes = 900\nrollover_target_bytes = 300\n",
                                               encoding="utf-8")
        store = self.store(repo)
        r1 = self.booking(store)
        with self.assertRaises(Crash):
            store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                       parts={"design": "Two.\n"}, fault=crash_at("log-written"), today=TODAY)
        Wiki(repo, WikiSettings(rollover_trigger_bytes=900, rollover_target_bytes=300),
             self.state(repo)).maintain_task("t1", today=TODAY)
        self.assertTrue((repo / "docs/wiki/log_archive_01.md").is_file())
        rolled = markers(repo)
        store.recover()
        self.assertEqual(markers(repo), rolled)
        self.assertEqual(len(rolled), 3)
        self.assertEqual(len(set(rolled)), 3)

    def test_revert_and_an_already_logged_acceptance_get_one_entry_each(self):
        repo = self.make_repo()
        store = self.store(repo)
        r1 = self.booking(store, request=None)
        r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Two",
                        parts={"design": "Two.\n"}, today=TODAY)
        r3 = store.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="Back to one",
                        parts={"design": "People book a lesson.\n"}, today=TODAY)
        self.assertEqual(r3.digest, r1.digest)
        self.assertEqual(len(set(markers(repo))), 4)
        uid = json.loads((repo / ".oah/designs/acme/project.json").read_text(encoding="utf-8"))["uid"]
        key = event_key("accepted", uid, "acme", "booking", 3, r3.digest)
        wiki = self.wiki(repo)
        with wiki.session():  # the entry arrived first, for example through a merge
            wiki.add_marked_entry(f"<!-- oah-design: {key} -->", title="Design acme/booking r3 accepted",
                                  body="merged", today=TODAY)
        store.accept("acme", "booking", revision=3, digest=r3.digest, reason="Yes", today=TODAY)
        self.assertEqual(markers(repo).count(f"<!-- oah-design: {key} -->"), 1)
        self.assertEqual(len(markers(repo)), 5)


class RequestPartTest(DesignRepo, unittest.TestCase):
    def test_product_request_stays_apart_from_the_task_intent(self):
        repo = self.make_repo()
        packet_path = packet.create(repo, "t1", scope=["src"])
        git_commit(repo, {}, "task packet")
        packet_bytes = packet_path.read_bytes()
        digest = intent.parse(intent.read_source(packet_path)).digest
        store = self.store(repo)
        r1 = self.booking(store)
        r2 = store.save("acme", "booking", expect_revision=1, expect_digest=r1.digest, reason="Correction",
                        parts={"request": "I want lessons booked.\nCorrection: groups too.\n"}, today=TODAY)
        for change in ({"parts": {"request": "Lessons, maybe.\n"}}, {"drop": ["request"]}):
            with self.subTest(change=change):
                with self.assertRaises(DesignError) as refused:
                    store.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="x",
                               today=TODAY, **change)
                self.assertEqual(refused.exception.code, "OAH_DESIGN_INPUT_INVALID")
        self.assertEqual(store.snapshot("acme", "booking").texts["request"],
                         "I want lessons booked.\nCorrection: groups too.\n")
        with self.assertRaises(DesignError) as marker:
            store.save("acme", "booking", expect_revision=2, expect_digest=r2.digest, reason="x", today=TODAY,
                       parts={"design": f"{intent.BEGIN}\n## Intent\n{intent.END}\n"})
        self.assertEqual(marker.exception.code, "OAH_DESIGN_INPUT_INVALID")
        self.assertEqual(packet_path.read_bytes(), packet_bytes)
        self.assertEqual(intent.parse(intent.read_source(packet_path)).digest, digest)
        self.assertNotIn(b"OAH_INTENT", (repo / PAGE).read_bytes())


class RenameTest(DesignRepo, unittest.TestCase):
    def test_rename_rewrites_clean_pages_and_keeps_edited_ones(self):
        repo = self.make_repo()
        store = self.store(repo)
        self.booking(store)
        store.new("acme", "payments", title="Payments", parts={"design": "Pay.\n"}, today=TODAY)
        payments = repo / "docs/wiki/designs/acme/acme--payments.md"
        payments.write_text(payments.read_text(encoding="utf-8").replace("Pay.", "Pay by card."), encoding="utf-8")
        edited = payments.read_bytes()
        result = store.rename_project("acme", title="Acme Web", today=TODAY)
        self.assertEqual(result.action, "renamed")
        self.assertIn("OAH_DESIGN_UNREGISTERED_EDIT", [item.code for item in result.diagnostics])
        self.assertEqual(payments.read_bytes(), edited)
        self.assertIn("[[designs/acme|Acme Web]]", (repo / PAGE).read_text(encoding="utf-8"))
        self.assertIn("- [[designs/acme|Acme Web]]", (repo / "docs/wiki/index.md").read_text(encoding="utf-8"))
        self.assertIn('title: "Acme Web (project acme)"', (repo / "docs/wiki/designs/acme.md").read_text(
            encoding="utf-8"))
        self.assertEqual(store.context("acme", "booking").page_state, "clean")
        self.assertEqual(len(markers(repo)), 4)
        self.assertEqual(json.loads((repo / ".oah/designs/acme/project.json").read_text())["former_titles"], ["Acme"])

    def test_page_left_with_the_former_title_is_stale_not_edited(self):
        repo = self.make_repo()
        store = self.store(repo)
        self.booking(store)
        with self.assertRaises(Crash):
            store.rename_project("acme", title="Acme Web", fault=crash_at("pointer-written"), today=TODAY)
        self.assertEqual(self.store(repo).context("acme", "booking").page_state, "stale")
        store.recover()
        self.assertEqual(store.context("acme", "booking").page_state, "clean")


class FactsTest(DesignRepo, unittest.TestCase):
    def test_facts_appear_in_list_lines_and_never_block(self):
        repo = self.make_repo()
        store = self.store(repo, facts=lambda project, design: ["sources fresh", "review needed"])
        self.booking(store)
        listing = (repo / "docs/wiki/designs/acme.md").read_text(encoding="utf-8")
        self.assertIn("nothing accepted yet (2026-11-04); sources fresh; review needed", listing)
        for provider in (lambda project, design: ["[[link]]"], lambda project, design: 1 / 0):
            with self.subTest(provider=provider):
                result = self.store(repo, facts=provider).reconcile()
                self.assertIn("OAH_DESIGN_LIST_STALE", [item.code for item in result.diagnostics])
        self.assertNotIn("sources fresh", (repo / "docs/wiki/designs/acme.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
