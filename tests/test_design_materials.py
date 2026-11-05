# 재료 부분 시험 — 실제 설계 저장소와 로컬 출처로 입력 기준(현재·검토 필요·낡음·무효·모름), 명시적 이어받기·재선택, 빈 클론의 휴대 증거 재검증, 표식·제어·양방향 원본, 크기 초과 거부를 본다.
from __future__ import annotations

import base64
import contextlib
import dataclasses
import io
import datetime as dt
import json
import os
import pathlib
import unittest
from unittest import mock

from oah.cli import main
from oah.design import fs as design_fs
from oah.design import materials, open_store
from oah.design.sources import association, output, portable
from oah.design.sources.adapters import Transport
from oah.design.sources.check import Sources
from oah.design.sources.model import build_spec
from oah.design.sources.view import ViewUnavailable, read_view
from oah.wiki import notes
from tests.helpers import git_commit, make_git_repo, make_tmp_dir
from tests.test_design_sources import LICENSE, Clock, FakeGitHub, git

TODAY = dt.date(2026, 11, 1)
MULTI = "One teacher books alone.\nCorrection: many organizations share one booking service.\n"
LOCATOR = "https://github.com/acme/lib"


class StoreClock:
    """A design-store clock that moves one second per call."""

    def __init__(self) -> None:
        self.moment = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> dt.datetime:
        self.moment += dt.timedelta(seconds=1)
        return self.moment


class MaterialsCase(unittest.TestCase):
    """A real design store and a registered, checked release source."""

    shared_state = False  # True: the sources live in the repository's state folder, as the CLI finds them

    def setUp(self) -> None:
        self.repo = make_git_repo(self)
        git_commit(self.repo, {f"docs/wiki/{rel}": data for rel, data in notes.scaffold_files(TODAY).items()},
                   "scaffold")
        self.store = open_store(self.repo, env={"OAH_STATE_DIR": str(self.repo.parent / "state")}, now=StoreClock())
        self.store.add_project("acme", title="Acme", event="ev-project", today=TODAY)
        first = self.store.new("acme", "booking", title="Booking", event="ev-r1", today=TODAY,
                               parts={"design": "People book a lesson.\n", "request": "One teacher books alone.\n"})
        self.store.accept("acme", "booking", revision=1, digest=first.digest, reason="Accepted", event="ev-a1",
                          today=TODAY)
        self.upstream = make_git_repo(self)
        self.clock = Clock()
        self.github = FakeGitHub()
        self.sources = self.make_sources(self.repo.parent / "state" if self.shared_state
                                         else make_tmp_dir(self) / "sources")
        self.release("v1.0.0", {"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})
        self.spec = self.sources.register(build_spec(url=LOCATOR, paths=["src"]))[1]
        self.assertEqual("changed", self.sources.check_one(self.spec).result)

    def make_sources(self, state: pathlib.Path, **settings) -> Sources:
        transport = Transport(api_base="https://api.github.com", git_url=lambda locator: str(self.upstream),
                              getter=self.github)
        return Sources(state, settings=settings, transport=transport, now=self.clock.now, clock=self.clock.monotonic)

    def release(self, tag: str, files: dict) -> None:
        git_commit(self.upstream, files, tag)
        git(self.upstream, "-c", "tag.gpgSign=false", "tag", "-a", "-m", tag, tag)
        self.github.add(tag, body=f"notes for {tag}")

    def view(self):
        return read_view(self.store, "acme", "booking")

    def selection(self, **changes) -> dict:
        base = {"narrative": "We keep bookings in a small embedded library.\n\n- one process\n- one file",
                "decision_inputs": {"storage": {"need": "Store bookings", "constraints": ["single user"],
                                                "platform": ["macOS", "Windows"], "kind": "library",
                                                "compatibility": ["rust 1.80"]}},
                "decisions": [{"id": "storage", "status": "selected", "chosen": "lib", "alternatives": [],
                               "reason": "Small and offline"}],
                "materials": [{"id": "lib", "source": "lib", "choice": "selected", "decision": "storage"}]}
        base.update(changes)
        return base

    def save(self, parts: dict, reason: str = "Update") -> object:
        context = self.store.context("acme", "booking")
        return self.store.save("acme", "booking", expect_revision=context.current.info.revision,
                               expect_digest=context.current.info.digest, reason=reason, parts=parts, today=TODAY)

    def accept_current(self) -> None:
        info = self.store.context("acme", "booking").current.info
        self.store.accept("acme", "booking", revision=info.revision, digest=info.digest, reason="Accepted",
                          today=TODAY)

    def saved_text(self) -> str:
        return self.store.context("acme", "booking").current.texts["materials"]

    def check(self, text: str | None = None, sources: Sources | None = None) -> dict:
        return materials.check(self.saved_text() if text is None else text, sources or self.sources, None,
                               self.view())

    def statuses(self, report: dict) -> dict:
        return {item["id"]: (item["input_status"], tuple(item["problems"])) for item in report["decisions"]}

    def record_of(self, text: str) -> dict:
        return materials.parse(text)

    def resolve(self, kind: str, *, selection: dict | None = None, reason: str = "Reviewed old and new bodies") -> dict:
        template = materials.resolution_template(self.saved_text(), self.sources, self.view(), "storage", kind=kind,
                                                 selection=selection)
        return dict(template, reason=reason, actor="main-ai", at=self.clock.now().isoformat())


class InputBasisTest(MaterialsCase):
    """Input bases, priority order and explicit carry/reselection."""

    def test_materials_only_save_stays_input_current_without_self_reference(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        report = self.check()
        self.assertEqual("current", report["overall"], report)
        self.assertEqual({"storage": ("current", ())}, self.statuses(report))
        self.accept_current()
        self.assertEqual("current", self.check()["overall"])
        recorded = self.record_of(self.saved_text())["decisions"][0]
        self.assertNotIn("materials", recorded["input_basis"]["parts"])
        self.assertEqual(report["decisions"][0]["current_basis"], report["decisions"][0]["recorded_basis"])

    def test_changed_requirements_are_needs_review_then_stale_after_acceptance(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.accept_current()
        self.save({"request": MULTI}, "Multi-organization")
        self.assertEqual(built.text, self.saved_text())
        working = self.check()
        self.assertEqual({"storage": ("needs-review", ("working-input-changed",))}, self.statuses(working))
        self.accept_current()
        accepted = self.check()
        self.assertEqual("stale", accepted["overall"])
        self.assertEqual({"storage": ("stale", ("accepted-input-changed",))}, self.statuses(accepted))
        self.assertEqual("selected", accepted["decisions"][0]["execution_relevance"])
        self.assertEqual("eligible", accepted["source_reads"][0]["eligibility"])
        self.assertEqual("fresh", accepted["source_reads"][0]["upstream_freshness"])

    def test_title_only_edit_conservatively_changes_the_basis(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        context = self.store.context("acme", "booking")
        self.store.save("acme", "booking", expect_revision=context.current.info.revision,
                        expect_digest=context.current.info.digest, reason="Rename", title="Lesson booking",
                        today=TODAY)
        self.assertEqual("needs-review", self.check()["overall"])

    def test_rebuild_after_changed_inputs_needs_an_explicit_resolution(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.save({"request": MULTI}, "Multi-organization")
        with self.assertRaises(materials.MaterialsError) as caught:
            materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.assertEqual("OAH_MATERIALS_REVIEW_REQUIRED", caught.exception.code)

    def test_carry_adds_the_new_basis_and_keeps_the_pinned_bytes(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.accept_current()
        self.save({"request": MULTI}, "Multi-organization")
        carry = self.resolve("carry")
        rebuilt = materials.build(self.selection(resolutions=[carry]), self.sources, view=self.view(),
                                  now=self.clock.now())
        self.save({"materials": rebuilt.text}, "Carry storage")
        report = self.check()
        self.assertEqual("current", report["overall"], report)
        decision = self.record_of(rebuilt.text)["decisions"][0]
        self.assertEqual(2, len(decision["supported_bases"]))
        self.assertEqual(["carry"], [item["kind"] for item in decision["resolutions"]])
        before = self.record_of(built.text)["materials"][0]
        after = self.record_of(rebuilt.text)["materials"][0]
        self.assertEqual(before["snapshot"], after["snapshot"])
        self.accept_current()
        self.assertEqual("current", self.check()["overall"])

    def test_carry_cannot_change_the_chosen_source_bytes_or_claims(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.save({"request": MULTI}, "Multi-organization")
        carry = self.resolve("carry")
        tested = self.selection(resolutions=[carry])
        tested["materials"][0]["integration"] = {"method": "dependency", "status": "tested", "evidence": [
            {"environment": "macOS 15", "command": "cargo test", "exit": 0, "at": "2026-11-01T11:00:00Z",
             "output_sha256": "a" * 64}]}
        with self.assertRaises(materials.MaterialsError) as caught:
            materials.build(tested, self.sources, view=self.view(), now=self.clock.now())
        self.assertIn("record a reselected resolution", str(caught.exception))

    def test_new_upstream_release_needs_review_until_reselected(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.clock.advance(3600)
        self.release("v1.1.0", {"src/lib.rs": "pub fn a() {}\npub fn b() {}\n"})
        self.assertEqual("changed", self.sources.check_one(self.spec).result)
        report = self.check()
        self.assertEqual({"storage": ("needs-review", ("source-snapshot-changed",))}, self.statuses(report))
        reselect = self.resolve("reselected", selection=self.selection())
        rebuilt = materials.build(self.selection(resolutions=[reselect]), self.sources, view=self.view(),
                                  now=self.clock.now())
        self.save({"materials": rebuilt.text}, "Reselect storage")
        self.assertEqual("current", self.check()["overall"])
        self.assertNotEqual(self.record_of(built.text)["materials"][0]["snapshot"],
                            self.record_of(rebuilt.text)["materials"][0]["snapshot"])

    def test_policy_change_needs_review_and_a_carry_covers_it(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        strict = self.make_sources(self.sources.state_dir, license_allow=["MIT", "Apache-2.0"])
        report = materials.check(self.saved_text(), strict, None, self.view())
        self.assertEqual({"storage": ("needs-review", ("policy-changed",))}, self.statuses(report))
        template = materials.resolution_template(self.saved_text(), strict, self.view(), "storage", kind="carry")
        carry = dict(template, reason="The added license does not change this choice", actor="main-ai",
                     at=self.clock.now().isoformat())
        rebuilt = materials.build(self.selection(resolutions=[carry]), strict, view=self.view(), now=self.clock.now())
        self.save({"materials": rebuilt.text}, "Carry policy")
        self.assertEqual("current", materials.check(self.saved_text(), strict, None, self.view())["overall"])

    def test_unknown_authority_and_legacy_records_are_never_current(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        report = materials.check(built.text, self.sources, None, None, unavailable="OAH_DESIGN_NOT_FOUND")
        self.assertEqual("unknown", report["overall"])
        self.assertIn("authority-unavailable:OAH_DESIGN_NOT_FOUND", report["decisions"][0]["problems"])
        legacy = "# Materials\n\n## Data\n\n```oah-materials\n" + json.dumps(
            {"version": 1, "decisions": [{"id": "storage", "status": "selected"}], "materials": [],
             "stacks": []}) + "\n```\n"
        old = materials.check(legacy, self.sources, None, self.view())
        self.assertEqual("unknown", old["overall"])
        self.assertEqual(["missing-input-basis"], old["decisions"][0]["problems"])

    def test_edits_and_forged_identities_are_invalid(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        text = self.saved_text()
        edited = text.replace("Small and offline", "Small, offline and tested", 1)
        self.assertEqual("invalid", self.check(edited)["overall"])
        self.assertIn("OAH_MATERIALS_EDITED", self.check(edited)["problems"])

        def forged(change) -> dict:
            record = json.loads(json.dumps(self.record_of(text)))
            change(record)
            return self.check(materials.render(record))

        def inputs(record):
            record["decision_inputs"]["storage"]["constraints"] = ["many organizations"]

        def same_revision(record):
            record["decisions"][0]["based_on"]["digest"] = "sha256:" + "1" * 64

        def wrong_uid(record):
            record["decisions"][0]["input_basis"]["uid"] = "f" * 32

        def extra_basis(record):
            record["decisions"][0]["supported_bases"].append("sha256:" + "2" * 64)

        def claims(record):
            record["materials"][0]["reviewed"]["commit"] = "0" * 40

        def blob(record):
            envelope = record["portable_evidence"][0]
            envelope["blobs"][0]["data"] = base64.b64encode(b"other").decode("ascii")
            record["materials"][0]["portable_evidence_id"] = portable.envelope_id(envelope)

        for change, problem in ((inputs, "decision-inputs-edited"), (same_revision, "same-revision-different-digest"),
                                (wrong_uid, "wrong-design-identity"), (extra_basis, "resolution-chain-broken"),
                                (claims, "material-invalid:lib"), (blob, "material-invalid:lib")):
            with self.subTest(change=change.__name__):
                report = forged(change)
                self.assertEqual("invalid", report["overall"])
                self.assertIn(problem, report["decisions"][0]["problems"])


class PortableEvidenceTest(MaterialsCase):
    """Complete originals survive an empty clone, with S1 times kept apart from S2 freshness."""

    def test_empty_clone_round_trip_keeps_s1_times_and_reports_s2_freshness(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        saved = self.record_of(self.saved_text())["materials"][0]["checked"]
        self.assertEqual("2026-11-01T12:00:00+00:00", saved["confirmed_at"])
        self.clock.advance(7200)
        self.assertEqual("unchanged", self.sources.check_one(self.spec).result)
        here = self.check()
        self.assertEqual("2026-11-01T12:00:00+00:00", here["source_reads"][0]["historical_times"]["confirmed_at"])
        self.assertEqual("fresh", here["source_reads"][0]["upstream_freshness"])
        self.assertEqual("2026-11-01T14:00:00+00:00", self.sources.head(self.spec).confirmed_at)
        empty = self.make_sources(make_tmp_dir(self) / "empty")
        for sources in (empty, None):
            report = materials.check(self.saved_text(), sources, None, self.view())
            self.assertEqual("current", report["overall"], report)
            read = report["source_reads"][0]
            self.assertEqual(portable.HISTORICAL, read["historical_evidence"])
            self.assertEqual(portable.OFFLINE_FRESHNESS, read["upstream_freshness"])
            self.assertEqual("eligible", read["eligibility"])

    def test_marker_control_bidi_binary_and_long_originals_are_lossless(self) -> None:
        originals = {"src/marker.md": "<!-- oah-design: not a marker -->\n```oah-materials\n{}\n```\n",
                     "src/control.bin": b"\x00\x1b[31m\xff\xfe\x07", "src/bidi.txt": "left ‮right x\n",
                     "src/long.txt": "y" * 70000 + "\n"}
        self.clock.advance(60)
        self.release("v1.0.1", originals)
        self.assertEqual("changed", self.sources.check_one(self.spec).result)
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        result = self.save({"materials": built.text}, "Materials")
        self.assertEqual("saved", result.action)
        record = self.record_of(self.saved_text())
        evidence = portable.verify(record["portable_evidence"][0])
        by_path = {item["path"]: item["sha256"] for item in evidence.manifest["files"]}
        for name, content in originals.items():
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            self.assertEqual(data, evidence.read(by_path[name]), name)
        lines = self.saved_text().split("\n")
        self.assertFalse(any(line.lstrip().startswith("<!-- oah") for line in lines))
        self.assertNotIn("‮", self.saved_text())
        self.assertNotIn("\x1b", self.saved_text())
        self.assertEqual("current", materials.check(self.saved_text(), None, None, self.view())["overall"])

    def test_oversized_evidence_refuses_before_any_save(self) -> None:
        self.clock.advance(60)
        self.release("v1.0.2", {"src/huge.bin": bytes(range(256)) * 3200})
        self.assertEqual("changed", self.sources.check_one(self.spec).result)
        before = self.store.context("acme", "booking").current.info
        with self.assertRaises(materials.MaterialsError) as caught:
            materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.assertEqual("OAH_MATERIALS_EVIDENCE_TOO_LARGE", caught.exception.code)
        details = caught.exception.details
        self.assertFalse(details["complete"])
        self.assertGreaterEqual(details["measured_bytes"], details["limit"])
        self.assertIn("narrow the selected paths of source lib", details["plan"][0])
        self.assertEqual(before, self.store.context("acme", "booking").current.info)

    def test_tampered_or_incomplete_envelopes_refuse(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        envelope = built.record["portable_evidence"][0]
        missing = dict(envelope, blobs=envelope["blobs"][1:])
        extra = dict(envelope, blobs=envelope["blobs"] + [{"id": "sha256:" + "3" * 64, "kind": "selected-file",
                                                           "path_or_field": "x", "encoding": "base64", "bytes": 0,
                                                           "sha256": "3" * 64, "data": ""}])
        loose = json.loads(json.dumps(envelope))
        loose["blobs"][0]["data"] = loose["blobs"][0]["data"].rstrip("=") + "\n"
        moved = json.loads(json.dumps(envelope))
        moved["snapshot"]["manifest"]["files"][0]["path"] = "src/elsewhere.rs"
        altered = json.loads(json.dumps(envelope))
        original = base64.b64decode(altered["blobs"][0]["data"])
        flipped = original[:-1] + bytes([original[-1] ^ 1]) if original else b"x"
        altered["blobs"][0]["data"] = base64.b64encode(flipped).decode("ascii")
        for name, item in (("missing", missing), ("extra", extra), ("loose", loose), ("moved", moved),
                           ("altered", altered)):
            with self.subTest(name=name), self.assertRaises(portable.PortableError):
                portable.verify(item)

    def test_projection_manifest_counts_semantic_and_original_bytes(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        projection = built.projection
        self.assertEqual(len(built.text.encode("utf-8")), projection["part_bytes"])
        self.assertEqual(projection["part_bytes"] - projection["original_payload_bytes"],
                         projection["semantic_bytes"])
        self.assertTrue(projection["semantic_fits_inline"])
        self.assertEqual([portable.envelope_id(built.record["portable_evidence"][0])],
                         [item["id"] for item in projection["envelopes"]])



class AssociationTest(MaterialsCase):
    """Monotonic association publication and the consumer's recheck before it uses facts."""

    def setUp(self) -> None:
        super().setUp()
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.accept_current()
        self.key = association.key_of(self.view().uid, "booking")

    def entry(self) -> dict | None:
        return self.sources.association_entry(self.view().uid, "booking")

    def page(self) -> str:
        return (self.repo / design_fs.project_page_rel(self.store.wiki_dir, "acme")).read_text(encoding="utf-8")

    def provider(self):
        return self.sources.list_facts(lambda project, design: read_view(self.store, project, design))

    def test_refresh_publishes_the_exact_read_pair_and_d_rechecks_it(self) -> None:
        report = self.sources.refresh_for(self.store, "acme", "booking", reason="start").to_json()
        self.assertEqual("published", report["association"]["status"])
        view = self.view()
        self.assertEqual(view.current.ref.to_json(), report["binding"]["current"])
        self.assertEqual(view.accepted.ref.to_json(), report["binding"]["accepted"])
        self.assertTrue(self.sources.matches_view(report, view)["matches"])
        self.save({"request": MULTI}, "Multi-organization")
        recheck = self.sources.matches_view(report, self.view())
        self.assertFalse(recheck["matches"])
        self.assertIn("current-changed", recheck["reasons"])
        again = self.sources.refresh_for(self.store, "acme", "booking", reason="resume").to_json()
        self.assertEqual("published", again["association"]["status"])
        self.assertEqual(2, again["association"]["generation"])

    def test_an_older_read_never_overwrites_a_newer_entry(self) -> None:
        old_view = self.view()
        self.save({"request": MULTI}, "Multi-organization")
        self.assertEqual("published", self.sources.refresh_for(self.store, "acme", "booking",
                                                               reason="start").association["status"])
        stored = self.entry()
        refs = materials.refs_of(self.saved_text())
        result = self.sources.publish_association(old_view, refs, captured_generation=stored["generation"])
        self.assertEqual("superseded", result["status"])
        self.assertEqual(stored, self.entry())
        reads = iter([old_view, self.view()])
        late = self.sources.refresh_for(self.store, "acme", "booking", reason="start", reader=lambda: next(reads))
        self.assertEqual("superseded", late.association["status"])
        self.assertEqual(stored["current"], self.entry()["current"])

    def test_same_revision_with_another_digest_is_invalid_and_keeps_the_last_good_entry(self) -> None:
        self.sources.refresh_for(self.store, "acme", "booking", reason="start")
        good = self.entry()
        view = self.view()
        forged_ref = dataclasses.replace(view.current.ref, digest="sha256:" + "4" * 64)
        forged = dataclasses.replace(view, current=dataclasses.replace(view.current, ref=forged_ref))
        result = self.sources.publish_association(forged, materials.refs_of(self.saved_text()),
                                                  captured_generation=good["generation"])
        self.assertEqual("invalid", result["status"])
        entry = self.entry()
        self.assertEqual(good["current"], entry["current"])
        self.assertEqual(forged_ref.digest, entry["conflict"]["current"]["digest"])
        self.store.reconcile("acme", facts=self.provider())
        self.assertNotIn("sources 1 fresh", self.page())
        clean = self.sources.refresh_for(self.store, "acme", "booking", reason="resume")
        self.assertEqual("published", clean.association["status"])
        self.assertIsNone(self.entry()["conflict"])

    def test_an_unreadable_design_publishes_nothing(self) -> None:
        self.sources.refresh_for(self.store, "acme", "booking", reason="start")
        before = self.entry()

        def unreadable():
            raise ViewUnavailable("io")

        report = self.sources.refresh_for(self.store, "acme", "booking", reason="start", reader=unreadable)
        self.assertEqual("unknown(io)", report.design_state)
        self.assertEqual("unknown", report.association["status"])
        self.assertEqual(before, self.entry())

    def test_source_or_policy_identity_change_discards_cached_facts(self) -> None:
        report = self.sources.refresh_for(self.store, "acme", "booking", reason="start").to_json()
        self.sources.remove("lib")
        self.sources.register(build_spec(url=LOCATOR, paths=["src", "docs"]))
        moved = self.sources.matches_view(report, self.view())
        self.assertIn("source-changed:lib", moved["reasons"])
        strict = self.make_sources(self.sources.state_dir, license_allow=["Apache-2.0"])
        self.assertIn("policy-changed", strict.matches_view(report, self.view())["reasons"])

    def test_list_facts_reach_the_project_page_only_for_a_matching_view(self) -> None:
        self.sources.refresh_for(self.store, "acme", "booking", reason="start")
        self.store.reconcile("acme", facts=self.provider())
        self.assertIn("; sources 1 fresh", self.page())
        self.save({"request": MULTI}, "Multi-organization")
        self.store.reconcile("acme", facts=self.provider())
        self.assertNotIn("sources 1 fresh", self.page())
        with_facts = open_store(self.repo, env={"OAH_STATE_DIR": str(self.repo.parent / "state")},
                                now=StoreClock(), facts=self.provider())
        self.sources.refresh_for(with_facts, "acme", "booking", reason="resume")
        context = with_facts.context("acme", "booking")
        with_facts.accept("acme", "booking", revision=context.current.info.revision,
                          digest=context.current.info.digest, reason="Accepted", today=TODAY)
        self.assertNotIn("sources 1 fresh", self.page())
        self.sources.refresh_for(with_facts, "acme", "booking", reason="resume")
        with_facts.reconcile("acme")
        self.assertIn("; sources 1 fresh", self.page())



@unittest.skipIf(os.name == "nt", "the anchored POSIX path; native Windows refuses (tested below)")
class GuardedOutputTest(unittest.TestCase):
    """--out creates a new regular file through pinned directories, or refuses before writing."""

    def setUp(self) -> None:
        self.root = make_tmp_dir(self) / "repo"
        (self.root / "out").mkdir(parents=True)
        self.outside = make_tmp_dir(self)

    def listing(self) -> list[str]:
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))

    def refuses(self, name: str, code: str, reason: str) -> None:
        before = self.listing()
        with self.assertRaises(output.OutputError) as caught:
            output.write_new(self.root, name, b"data")
        self.assertEqual((code, reason), (caught.exception.code, caught.exception.reason), name)
        self.assertEqual(before, self.listing())

    def test_a_new_file_under_a_plain_folder_is_written_without_a_leftover(self) -> None:
        path = output.write_new(self.root, "out/materials.md", b"# Materials\n")
        self.assertEqual(b"# Materials\n", path.read_bytes())
        self.assertEqual(["out", "out/materials.md"], self.listing())
        absolute = output.write_new(self.root, f"{self.root.as_posix()}/out/second.md", b"x")
        self.assertEqual(b"x", absolute.read_bytes())

    def test_lexical_refusals_happen_before_anything_is_opened(self) -> None:
        cases = {"../x.md": "traversal", "out/../x.md": "traversal", "out//x.md": "empty-component",
                 "./x.md": "empty-component", "C:x.md": "drive", "\\\\server\\share\\x.md": "unc-or-device",
                 "out\\x.md": "backslash", "out/CON.md": "reserved-name", "out/nul": "reserved-name",
                 "out/a.md:stream": "alternate-stream", "out/a.": "trailing-dot-or-space",
                 "out/a ": "trailing-dot-or-space", "out/a\u202eb.md": "control-character",
                 f"{self.outside.as_posix()}/x.md": "outside-root", "out/" + "n" * 250: "too-long"}
        for name, reason in cases.items():
            with self.subTest(name=name):
                self.refuses(name, output.PATH_INVALID_CODE, reason)

    def test_links_missing_or_non_folder_parents_and_existing_finals_refuse(self) -> None:
        (self.root / "linked").symlink_to(self.outside, target_is_directory=True)
        (self.root / "file").write_bytes(b"")
        (self.root / "out" / "taken.md").write_bytes(b"old")
        (self.root / "out" / "dir").mkdir()
        (self.root / "out" / "link.md").symlink_to(self.outside / "target.md")
        (self.root / "out" / "Notes.md").write_bytes(b"")
        self.refuses("linked/x.md", output.PATH_INVALID_CODE, "link")
        self.refuses("missing/x.md", output.PATH_INVALID_CODE, "missing-parent")
        self.refuses("file/x.md", output.PATH_INVALID_CODE, "not-directory")
        self.refuses("out/notes.md", output.PATH_INVALID_CODE, "case-collision")
        for name in ("out/taken.md", "out/dir", "out/link.md"):
            self.refuses(name, output.EXISTS_CODE, "final-exists")
        self.assertEqual(b"old", (self.root / "out" / "taken.md").read_bytes())
        self.assertEqual([], list(self.outside.iterdir()))
        alias = make_tmp_dir(self) / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(output.OutputError) as caught:
            output.write_new(alias, "out/x.md", b"x")
        self.assertEqual("link", caught.exception.reason)

    def test_a_parent_swapped_for_a_link_after_pinning_cannot_redirect_the_write(self) -> None:
        def swap(step: str) -> None:
            if step == "parent-pinned":
                (self.root / "out").rename(self.root / "moved")
                (self.root / "out").symlink_to(self.outside, target_is_directory=True)

        output.write_new(self.root, "out/materials.md", b"pinned", fault=swap)
        self.assertEqual(b"pinned", (self.root / "moved" / "materials.md").read_bytes())
        self.assertEqual([], list(self.outside.iterdir()))

    def test_a_final_name_that_appears_during_the_write_is_never_replaced(self) -> None:
        def race(step: str) -> None:
            if step == "temporary-written":
                (self.root / "out" / "materials.md").write_bytes(b"theirs")

        with self.assertRaises(output.OutputError) as caught:
            output.write_new(self.root, "out/materials.md", b"ours", fault=race)
        self.assertEqual(output.EXISTS_CODE, caught.exception.code)
        self.assertEqual(b"theirs", (self.root / "out" / "materials.md").read_bytes())
        self.assertEqual(["out", "out/materials.md"], self.listing())


class WindowsOutputTest(unittest.TestCase):
    def test_native_windows_refuses_as_unverifiable_before_writing(self) -> None:
        root = make_tmp_dir(self)
        with self.assertRaises(output.OutputError) as caught:
            output.write_new(root, "materials.md", b"x", windows=True)
        self.assertEqual((output.UNVERIFIABLE_CODE, "windows-handles-unqualified"),
                         (caught.exception.code, caught.exception.reason))
        self.assertEqual([], list(root.iterdir()))



class SourcesCliTest(MaterialsCase):
    """The oah sources materials and diff commands over the same store and state folder."""

    shared_state = True

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(self.repo.parent / "state")}), \
                contextlib.chdir(self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def selection_file(self) -> pathlib.Path:
        path = self.repo.parent / "selection.json"
        path.write_text(json.dumps(self.selection()), encoding="utf-8")
        return path

    def test_render_check_and_guarded_out(self) -> None:
        selection = str(self.selection_file())
        code, text, err = self.cli("sources", "materials", "render", "--design", "acme/booking", "--input",
                                   selection)
        self.assertEqual(0, code, err)
        self.assertIn("```oah-materials", text)
        (self.repo / "out").mkdir()
        code, out, err = self.cli("sources", "materials", "render", "--design", "acme/booking", "--input", selection,
                                  "--out", "out/materials.md")
        self.assertEqual(0, code, err)
        self.assertEqual(text, (self.repo / "out" / "materials.md").read_text(encoding="utf-8"))
        code, _out, err = self.cli("sources", "materials", "render", "--design", "acme/booking", "--input",
                                   selection, "--out", "out/materials.md")
        self.assertEqual((1, True), (code, err.startswith("OAH_MATERIALS_OUTPUT_EXISTS")))
        self.save({"materials": text}, "Materials")
        code, out, err = self.cli("sources", "materials", "check", "--design", "acme/booking", "--json")
        self.assertEqual(0, code, err)
        self.assertEqual("current", json.loads(out)["overall"])
        self.save({"request": MULTI}, "Multi-organization")
        code, out, _err = self.cli("sources", "materials", "check", "--design", "acme/booking")
        self.assertEqual(1, code)
        self.assertIn("decision storage: needs-review (working-input-changed)", out)
        code, out, _err = self.cli("sources", "materials", "check", "--design", "acme/booking", "--revision", "2",
                                   "--json")
        self.assertEqual("needs-review", json.loads(out)["overall"])

    def test_diff_exit_codes(self) -> None:
        head = self.sources.head(self.spec).verified.snapshot
        code, out, err = self.cli("sources", "diff", "lib", "--from", head, "--to", head, "--json")
        self.assertEqual(0, code, err)
        self.assertEqual("complete", json.loads(out)["completeness"])
        code, out, _err = self.cli("sources", "diff", "lib", "--from", head, "--to", "sha256:" + "0" * 64)
        self.assertEqual(1, code)
        self.assertIn("unavailable", out)
        code, _out, err = self.cli("sources", "diff", "lib", "--from", "HEAD", "--to", head)
        self.assertEqual((1, True), (code, err.startswith("OAH_SOURCES_USAGE")))


if __name__ == "__main__":
    unittest.main()
