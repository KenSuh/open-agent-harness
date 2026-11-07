# 재료 부분 시험 — 실제 설계 저장소와 로컬 출처로 입력 기준(현재·검토 필요·낡음·무효·모름), 명시적 이어받기·재선택, 빈 클론의 휴대 증거 재검증, 표식·제어·양방향 원본, 크기 초과 거부와 다른 부분이 따라 만드는 공유 JSON 모양을 본다.
from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import io
import datetime as dt
import json
import os
import pathlib
import re
import sys
import textwrap
import unittest
from unittest import mock

from oah.cli import main
from oah.design import fs as design_fs
from oah.design import materials, open_store
from oah.design.sources import association, license_texts, output, portable
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


def populated_holder_cases() -> list[tuple[str, bytes, str]]:
    notice = "Copyright (c) 2026 Acme\n"
    bsd = notice + license_texts.BSD_OPENING + " " + license_texts.BSD_THIRD + " " + license_texts.BSD_DISCLAIMER
    apache = (pathlib.Path(__file__).resolve().parents[1] / "skills/skill-creator/LICENSE.txt").read_text()
    populated = "Copyright 2026 Anthropic, PBC."
    restrictions = ("Acme No Commercial Use", "Acme NONCOMMERCIAL ONLY", "Not For Commercial Use",
                    "Acme no COMMERCIAL use", "Acme (commercial use is forbidden)",
                    "Acme\nCommercial use is forbidden", "Acme\nCopyright 2026 No Commercial Use")
    rows = []
    for template, make in (("bsd3", lambda field: bsd.format(HOLDER=field, ITS="its")),
                            ("apache", lambda field: apache.replace(populated, field))):
        for index, restriction in enumerate(restrictions):
            for extra in (False, True):
                field = ("Acme\n" if extra else "") + restriction
                valid = "Acme-Labs, LLC. <contact@example.org>"
                if template == "apache":
                    field = ("Copyright 2026 Acme\n" if extra else "") + "Copyright 2026 " + restriction
                    valid = ("Copyright 2026 Acme\n" if extra else "") + "Copyright 2026 " + valid
                elif extra:
                    valid = "\n" + valid
                name = f"{template}-{index}-{'additional' if extra else 'first'}"
                rows.extend(((name, make(field).encode(), "unknown"),
                             (name + "-positive", make(valid).encode(), "eligible")))
        for index, holder in enumerate(("Jane Doe", "The Apache Software Foundation", "Acme And Contributors")):
            field = "Copyright 2026 " + holder if template == "apache" else holder
            rows.append((f"{template}-multiword-{index}", make(field).encode(), "unknown"))
        for index, holder in enumerate(("Acme", "Jean-Luc", "O'Neil, Inc.", "NONCOMMERCIAL", "No-Commercial-Use")):
            field = "Copyright 2026 " + holder if template == "apache" else holder
            rows.append((f"{template}-identifier-{index}", make(field).encode(), "eligible"))
    rows.append(("apache-fixed-example", apache.replace(populated, "Copyright [yyyy] [name of copyright owner]").encode(), "eligible"))
    for index, example in enumerate(("Copyright [yyyy] [name of copyright owner] No Commercial Use",
                                     "Copyright [2026] [Acme]", "Copyright [yyyy] [name of copyright owner]\nCopyright 2026 Acme")):
        rows.append((f"apache-modified-example-{index}", apache.replace(populated, example).encode(), "unknown"))
    for holder in ("the copyright holder", "the copyright owner"):
        for pronoun in ("its", "their"):
            plain = bsd.format(HOLDER=holder, ITS=pronoun)
            numbered = plain.replace("redistributions of source", "\n1. redistributions of source")
            numbered = numbered.replace("redistributions in binary", "\n2. redistributions in binary")
            numbered = numbered.replace("neither the name", "\n3. neither the name")
            bulleted = re.sub(r"\n[123]\.", "\n*", numbered)
            wrapped = notice + textwrap.fill(plain[len(notice):], width=72)
            for style, original in (("plain", plain), ("numbered", numbered), ("bulleted", bulleted),
                                    ("wrapped", wrapped)):
                name = f"bsd3-fixed-{holder.rsplit(' ', 1)[1]}-{pronoun}-{style}"
                rows.append((name, original.encode(), "eligible"))
                if (holder, pronoun, style) in (("the copyright holder", "its", "numbered"),
                                               ("the copyright owner", "their", "bulleted")):
                    titled = original.replace(notice, "BSD 3-Clause License\n\n" + notice + "All rights reserved.\n", 1)
                    rows.append((name + "-title-rights", titled.encode(), "eligible"))
    for index, holder in enumerate(("THE COPYRIGHT HOLDER", "the\ncopyright\towner")):
        rows.append((f"bsd3-fixed-raw-{index}", bsd.format(HOLDER=holder, ITS="its").encode(), "eligible"))
    for index, holder in enumerate(("the copyright holder, no commercial use", "the copyright owner No Commercial Use")):
        rows.append((f"bsd3-fixed-extra-prose-{index}", bsd.format(HOLDER=holder, ITS="its").encode(), "unknown"))
    return rows


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

    def test_rebuild_cannot_clear_policy_review_without_resolution(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        strict = self.make_sources(self.sources.state_dir, license_allow=["MIT", "Apache-2.0"])
        with self.assertRaises(materials.MaterialsError) as policy:
            materials.build(self.selection(), strict, view=self.view(), now=self.clock.now())
        self.assertEqual("OAH_MATERIALS_REVIEW_REQUIRED", policy.exception.code)

    def test_rebuild_cannot_clear_source_review_without_resolution(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.release("v1.1.0", {"src/lib.rs": "pub fn changed() {}\n"})
        self.assertEqual("changed", self.sources.check_one(self.spec).result)
        with self.assertRaises(materials.MaterialsError) as source:
            materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.assertEqual("OAH_MATERIALS_REVIEW_REQUIRED", source.exception.code)

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


class MaterialShapeTest(MaterialsCase):
    def two_decisions(self):
        guide = self.sources.register(build_spec(url=LOCATOR, source_id="guide", track="commit", ref="HEAD",
                                                paths=["src"]))[1]
        self.assertEqual("changed", self.sources.check_one(guide).result)
        selection = self.selection()
        selection["decision_inputs"]["docs"] = dict(selection["decision_inputs"]["storage"], need="Write docs")
        selection["decisions"].append({"id": "docs", "status": "selected", "chosen": "guide",
                                       "alternatives": [], "reason": "Public guide"})
        selection["materials"].append({"id": "guide", "source": "guide", "choice": "selected", "decision": "docs"})
        return materials.build(selection, self.sources, view=self.view())

    def test_invalid_sibling_never_hides_another_decisions_prose_edit(self) -> None:
        built = self.two_decisions()
        for corrupt in ("material", "envelope"):
            with self.subTest(corrupt=corrupt):
                record = json.loads(materials.canonical_json(built.record))
                bad = next(item for item in record["materials"] if item["id"] == "guide")
                if corrupt == "material":
                    bad["license"] = "MIT"
                else:
                    envelope = next(item for item in record["portable_evidence"]
                                    if portable.envelope_id(item) == bad["portable_evidence_id"])
                    envelope["blobs"][0]["data"] = base64.b64encode(b"tampered").decode("ascii")
                    bad["portable_evidence_id"] = portable.envelope_id(envelope)
                    record["portable_evidence"].sort(key=portable.envelope_id)
                text = built.text[:built.text.rindex(materials.FENCE)] + materials.FENCE + "\n" \
                    + materials.canonical_json(record) + "\n```\n"
                text = text.replace("Small and offline", "Changed without updating the record", 1)
                report = materials.check(text, self.sources, None, self.view())
                self.assertTrue(all(item["input_status"] != "current" for item in report["decisions"]))
                self.assertIn("OAH_MATERIALS_EDIT_UNKNOWN" if corrupt == "material" else "OAH_MATERIALS_EDITED",
                              report["problems"])
                reads = {item["material"]: item for item in report["source_reads"]}
                self.assertEqual(portable.HISTORICAL, reads["lib"]["historical_evidence"])

    def test_malformed_manifest_notices_are_typed_and_keep_a_healthy_sibling(self) -> None:
        from oah.design.sources.model import SourceError
        from oah.design.sources.state import record_digest

        built = self.two_decisions()
        for notices in (7, ["notice", 7]):
            with self.subTest(notices=notices):
                record = json.loads(materials.canonical_json(built.record))
                bad = next(item for item in record["materials"] if item["id"] == "lib")
                envelope = next(item for item in record["portable_evidence"]
                                if portable.envelope_id(item) == bad["portable_evidence_id"])
                manifest = envelope["snapshot"]["manifest"]
                manifest["notices"] = notices
                snapshot = record_digest(manifest)
                envelope["snapshot"]["digest"] = snapshot
                observation = envelope["provenance"]["observation"]
                observation["snapshot"] = snapshot
                envelope["provenance"]["observation_digest"] = record_digest(observation)
                bad["portable_evidence_id"], bad["snapshot"] = portable.envelope_id(envelope), snapshot
                record["portable_evidence"].sort(key=portable.envelope_id)
                with self.subTest(path="verify"):
                    with self.assertRaises(portable.PortableError) as caught:
                        portable.verify(envelope)
                    self.assertEqual("OAH_SOURCES_EVIDENCE_INVALID", caught.exception.code)
                with self.subTest(path="diff"):
                    with self.assertRaises(SourceError) as caught:
                        self.sources.diff("lib", snapshot, snapshot, evidence=[envelope])
                    self.assertEqual("OAH_SOURCES_EVIDENCE_INVALID", caught.exception.code)
                with self.subTest(path="materials"):
                    report = materials.check(materials.render(record), self.sources, None, self.view())
                    reads = {item["material"]: item for item in report["source_reads"]}
                    self.assertEqual("invalid", report["overall"])
                    self.assertEqual(portable.HISTORICAL, reads["guide"]["historical_evidence"])
                with self.subTest(path="cli"):
                    self.save({"materials": materials.render(record)})
                    out, err = io.StringIO(), io.StringIO()
                    with mock.patch("oah.sources_cli._sources", return_value=self.sources), \
                            mock.patch("oah.design.open_store", return_value=self.store), \
                            contextlib.chdir(self.repo), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = main(["sources", "materials", "check", "--design", "acme/booking", "--json"])
                    self.assertEqual((1, ""), (code, err.getvalue()))
                    report = json.loads(out.getvalue())
                    reads = {item["material"]: item for item in report["source_reads"]}
                    self.assertEqual("invalid", report["overall"])
                    self.assertEqual(portable.HISTORICAL, reads["guide"]["historical_evidence"])
    def test_same_policy_license_claims_must_match_the_portable_originals(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view())
        record = json.loads(materials.canonical_json(built.record))
        record["materials"][0]["license"]["eligibility"] = "ineligible"
        record["materials"][0]["license"]["reason"] = "forged"
        report = materials.check(materials.render(record), self.sources, None, self.view())
        self.assertEqual("invalid", report["overall"])
        self.assertIn("claim-unsupported", report["source_reads"][0]["problems"])

    def test_malformed_material_facts_are_scoped_and_keep_healthy_evidence(self) -> None:
        selection = self.selection(materials=[
            {"id": "lib", "source": "lib", "choice": "selected", "decision": "storage"},
            {"id": "healthy", "source": "lib", "choice": "candidate", "decision": "storage"}])
        built = materials.build(selection, self.sources, view=self.view())
        cases = (("license", "MIT"), ("checked", []), ("reviewed", "v1"), ("release", 5),
                 ("source.paths", 5), ("source.paths", ["src", {}]), ("license.eligibility", []),
                 ("checked.confirmed_at", 5), ("reviewed.commit", []), ("release.tag", {}))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                record = json.loads(materials.canonical_json(built.record))
                material = next(item for item in record["materials"] if item["id"] == "lib")
                parent, dot, child = field.partition(".")
                if dot:
                    material[parent][child] = value
                else:
                    material[field] = value
                text = built.text[:built.text.rindex(materials.FENCE)] + materials.FENCE + "\n" \
                    + materials.canonical_json(record) + "\n```\n"
                with self.assertRaises(materials.MaterialsError):
                    materials.parse(text)
                report = materials.check(text, self.sources, None, self.view())
                self.assertEqual("invalid", report["overall"])
                reads = {item["material"]: item for item in report["source_reads"]}
                self.assertEqual({"lib", "healthy"}, set(reads))
                self.assertEqual(portable.HISTORICAL, reads["healthy"]["historical_evidence"])
                self.assertEqual("invalid", reads["lib"]["historical_evidence"])
                self.assertTrue(reads["lib"]["problems"])

    def test_non_mapping_manifest_entries_are_typed_in_verify_check_and_diff(self) -> None:
        from oah.design.sources.model import SourceError
        from oah.design.sources.state import record_digest

        built = materials.build(self.selection(), self.sources, view=self.view())
        for field in ("files", "license.files", "license.notices"):
            with self.subTest(field=field):
                envelope = json.loads(materials.canonical_json(built.record["portable_evidence"][0]))
                manifest = envelope["snapshot"]["manifest"]
                parent, dot, child = field.partition(".")
                if dot:
                    manifest[parent][child] = ["not-a-mapping"]
                else:
                    manifest[field] = ["not-a-mapping"]
                snapshot = record_digest(manifest)
                envelope["snapshot"]["digest"] = snapshot
                observation = envelope["provenance"]["observation"]
                observation["snapshot"] = snapshot
                envelope["provenance"]["observation_digest"] = record_digest(observation)
                with self.assertRaises(portable.PortableError) as caught:
                    portable.verify(envelope)
                self.assertEqual("OAH_SOURCES_EVIDENCE_INVALID", caught.exception.code)
                with self.assertRaises(SourceError) as caught:
                    self.sources.diff("lib", snapshot, snapshot, evidence=[envelope])
                self.assertEqual("OAH_SOURCES_EVIDENCE_INVALID", caught.exception.code)
                record = json.loads(materials.canonical_json(built.record))
                record["portable_evidence"] = [envelope]
                record["materials"][0]["portable_evidence_id"] = portable.envelope_id(envelope)
                record["materials"][0]["snapshot"] = snapshot
                report = materials.check(materials.render(record), self.sources, None, self.view())
                self.assertEqual("invalid", report["overall"])
                self.assertTrue(report["source_reads"][0]["problems"])


class PortableEvidenceTest(MaterialsCase):
    def test_four_source_accepted_materials_and_a_distinct_working_draft_stay_reproducible(self) -> None:
        from tests.test_design_sources import Pages

        pages = Pages()
        getter = lambda url, **kwargs: self.github(url, **kwargs) if url.startswith("https://api.github.com") \
            else pages(url, **kwargs)
        sources = Sources(make_tmp_dir(self) / "four", transport=Transport(
            git_url=lambda locator: str(self.upstream), getter=getter), now=self.clock.now, clock=self.clock.monotonic)
        sources.register(build_spec(url=LOCATOR, paths=["src"]))
        for name in "abc":
            sources.register(build_spec(url=f"https://docs.example.org/{name}", source_id=f"doc-{name}"))
        self.assertEqual({"changed"}, {item.result for item in sources.check()})
        selected = [{"id": "lib", "source": "lib", "choice": "selected", "decision": "storage"}] \
            + [{"id": f"m{name}", "source": f"doc-{name}", "choice": "candidate", "decision": "storage"}
               for name in "abc"]
        built = materials.build(self.selection(materials=selected), sources, view=self.view())
        self.assertEqual(4, len(built.record["portable_evidence"]))
        self.save({"materials": built.text})
        self.accept_current()
        accepted_view = self.view().accepted
        assert accepted_view is not None
        accepted = accepted_view.ref
        draft = materials.build(self.selection(materials=selected, narrative="A distinct working comparison."),
                                sources, view=self.view())
        self.save({"request": MULTI, "materials": draft.text})
        current = self.view()
        assert current.accepted is not None
        self.assertNotEqual(accepted.digest, current.current.ref.digest)
        self.assertEqual(accepted, current.accepted.ref)
        self.assertNotEqual(current.accepted.texts["materials"], current.current.texts["materials"])
        self.assertEqual(4, len(materials.parse(current.accepted.texts["materials"])["portable_evidence"]))
        report = materials.check(self.saved_text(), None, None, current)
        self.assertEqual("needs-review", report["overall"])
        self.assertEqual({"lib", "ma", "mb", "mc"}, {item["material"] for item in report["source_reads"]})
        self.assertEqual({portable.HISTORICAL}, {item["historical_evidence"] for item in report["source_reads"]})

    def test_a_300kb_document_is_portable_and_tamper_or_missing_bytes_are_scoped(self) -> None:
        from tests.test_design_sources import Pages

        pages = Pages()
        pages.type, pages.body = "text/plain", b"public documentation\n" * 15000
        sources = Sources(make_tmp_dir(self) / "docs", transport=Transport(getter=pages), now=self.clock.now)
        spec = sources.register(build_spec(url="https://docs.example.org/guide", source_id="guide"))[1]
        self.assertEqual("changed", sources.check_one(spec).result)
        selected = [{"id": "lib", "source": "guide", "choice": "selected", "decision": "storage"}]
        built = materials.build(self.selection(materials=selected), sources, view=self.view())
        envelope = built.record["portable_evidence"][0]
        evidence = portable.verify(json.loads(materials.canonical_json(envelope)))
        self.assertEqual(315000, len(pages.body))
        self.assertEqual(pages.body, evidence.read(evidence.manifest["document"]["sha256"]))
        self.assertEqual("current", materials.check(built.text, None, None, self.view())["overall"])
        for mutation in ("tamper", "missing"):
            with self.subTest(mutation=mutation):
                bad = json.loads(materials.canonical_json(envelope))
                if mutation == "tamper":
                    bad["blobs"][0]["data"] = base64.b64encode(b"other public bytes").decode("ascii")
                else:
                    bad["blobs"] = []
                with self.assertRaises(portable.PortableError):
                    portable.verify(bad)

    def test_private_narrative_stays_local_and_offline_checks_send_nothing(self) -> None:
        private = "Private design: a confidential school booking plan."
        before = {str(p): p.read_bytes() for p in self.sources.state.root.rglob("*") if p.is_file()}
        with mock.patch.object(self.sources.observer, "observe", side_effect=AssertionError("unexpected fetch")), \
                mock.patch("oah.rerank.judge.judge", side_effect=AssertionError("unexpected judge request")):
            built = materials.build(self.selection(narrative=private), self.sources, view=self.view())
            self.assertIn(private, built.text)
            envelopes = built.record["portable_evidence"]
            self.assertNotIn(private, materials.canonical_json(envelopes))
            for envelope in envelopes:
                portable.verify(envelope)
            self.assertEqual("current", materials.check(built.text, None, None, self.view())["overall"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.sources.state.root.rglob("*") if p.is_file()})

    def test_copyright_notices_keep_license_eligibility_and_originals_offline(self) -> None:
        notice = "Copyright (c) 2026 Acme"
        cases = ((notice, "eligible"),
                 ("Copyright (c) 2015-2026 Acme, Inc.\nAll rights reserved.", "eligible"),
                 ("Copyright © 2026 Jane Doe <jane@example.org>", "unknown"),
                 ("(c) 2026 Acme", "eligible"),
                 ("Copyright (c) 2026 Jean-Luc O'Neil", "unknown"),
                 ("Copyright (c) 2026 The Apache Software Foundation", "unknown"),
                 (notice + ". Commercial use is forbidden.", "unknown"),
                 (notice + "\nCopyright notice: commercial use is forbidden.", "unknown"),
                 (notice + "\nAll rights reserved; commercial use is forbidden.", "unknown"),
                 (notice + " commercial use is forbidden.", "unknown"),
                 (notice + " and you may not use this software commercially.", "unknown"),
                 (notice + " distribution requires written approval", "unknown"),
                 (notice + " and Contributors", "unknown"),
                 ("Copyright (c) 2026 the Acme project contributors", "unknown"),
                 ("Copyright (c) 2026 One Two Three Four Five", "unknown"),
                 ("Copyright (c) 2026 Acme, Inc.", "eligible"),
                 (notice + " No Commercial Use", "unknown"),
                 ("Copyright (c) 2026 Acme-Labs, LLC", "eligible"),
                 (notice + " NONCOMMERCIAL ONLY", "unknown"),
                 ("Copyright (c) 2026 Acme, LLC", "eligible"),
                 ("Copyright (c) 2026 Not For Commercial Use", "unknown"),
                 (notice + "\nCopyright (c) 2026 Acme-Labs, Inc.", "eligible"),
                 (notice + "\nCopyright (c) 2026 No Commercial Use", "unknown"),
                 (notice + "\nCopyright (c) 2026 Acme, LLC", "eligible"),
                 (notice + "\nCopyright (c) 2026 NONCOMMERCIAL ONLY", "unknown"),
                 (notice + " And Contributors", "unknown"),
                 (notice + "\nCopyright (c) 2026 Acme And Contributors", "unknown"),
                 ("Copyright © 2026 Acme <contact@example.org>", "eligible"),
                 ("Copyright (c) 2026 Jean-Luc", "eligible"),
                 ("Copyright (c) 2026 O'Neil, LLC.", "eligible"))
        originals = [(f"mit-preamble-{index}", LICENSE.replace(notice, prefix).encode(), expected)
                     for index, (prefix, expected) in enumerate(cases)] + populated_holder_cases()
        self.sources = self.make_sources(make_tmp_dir(self) / "all-license-formats",
                                         license_allow=["MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"])
        self.spec = self.sources.register(self.spec)[1]
        for index, (name, original, expected) in enumerate(originals):
            with self.subTest(name=name):
                self.release(f"v1.0.{index + 1}", {"LICENSE": original.decode(),
                                                  "src/lib.rs": f"pub fn notice_{index}() {{}}\n"})
                checked = self.sources.check_one(self.spec)
                self.assertEqual("changed", checked.result)
                assert checked.snapshot is not None
                manifest = self.sources.snapshot(checked.snapshot)
                assert manifest is not None
                saved = manifest["license"]["files"][0]
                self.assertEqual(original, self.sources.blob(saved["sha256"]))
                built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
                empty = self.make_sources(make_tmp_dir(self) / "empty",
                                          license_allow=["MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"])
                report = materials.check(built.text, empty, None, self.view())
                embedded = portable.verify(built.record["portable_evidence"][0])
                self.assertEqual(original, embedded.read(saved["sha256"]))
                self.assertEqual((expected,) * 3, (self.sources.eligibility(manifest).status,
                                 built.record["materials"][0]["license"]["eligibility"],
                                 report["source_reads"][0]["eligibility"]))
                self.assertEqual("verified-from-portable-bytes", report["source_reads"][0]["historical_evidence"])
                self.assertEqual("unknown(offline-or-unregistered)", report["source_reads"][0]["upstream_freshness"])

    def test_registered_license_manifest_is_preserved_and_rechecked_offline(self) -> None:
        data = b'[package]\nname = "example"\nversion = "1.0.1"\nlicense = "MIT"\n'
        self.release("v1.0.1", {"Cargo.toml": data})
        spec = self.sources.register(build_spec(url=LOCATOR, source_id="with-manifest", paths=["src"],
                                                manifest_path="Cargo.toml"))[1]
        self.assertEqual("changed", self.sources.check_one(spec).result)
        selection = self.selection(materials=[{"id": "lib", "source": spec.id, "choice": "selected",
                                               "decision": "storage"}])
        built = materials.build(selection, self.sources, view=self.view(), now=self.clock.now())
        envelope = built.record["portable_evidence"][0]
        evidence = portable.verify(envelope)
        self.assertEqual(data, evidence.read(hashlib.sha256(data).hexdigest()))
        self.assertEqual("eligible", materials.check(built.text, None, None, self.view())["source_reads"][0]
                         ["eligibility"])
        from oah.design.sources.state import record_digest

        altered = json.loads(json.dumps(envelope))
        altered["snapshot"]["manifest"]["license"]["declared"]["value"] = "ISC"
        altered["snapshot"]["digest"] = record_digest(altered["snapshot"]["manifest"])
        altered["provenance"]["observation"]["snapshot"] = altered["snapshot"]["digest"]
        altered["provenance"]["observation_digest"] = record_digest(altered["provenance"]["observation"])
        with self.assertRaises(portable.PortableError):
            portable.verify(altered)

    def test_rehashed_selected_original_cannot_exceed_the_source_file_quota(self) -> None:
        from oah.design.sources.state import record_digest

        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        envelope = json.loads(json.dumps(built.record["portable_evidence"][0]))
        file = envelope["snapshot"]["manifest"]["files"][0]
        blob = next(item for item in envelope["blobs"] if item["sha256"] == file["sha256"])
        data = b"x" * ((1 << 20) + 1)
        sha = hashlib.sha256(data).hexdigest()
        file.update(size=len(data), sha256=sha)
        blob.update(id="sha256:" + sha, sha256=sha, bytes=len(data), data=base64.b64encode(data).decode("ascii"))
        envelope["snapshot"]["digest"] = record_digest(envelope["snapshot"]["manifest"])
        envelope["provenance"]["observation"]["snapshot"] = envelope["snapshot"]["digest"]
        envelope["provenance"]["observation_digest"] = record_digest(envelope["provenance"]["observation"])
        with self.assertRaises(portable.PortableError):
            portable.verify(envelope)

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

    def test_portable_release_reconstructs_the_selection_and_its_originals(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        envelope = built.record["portable_evidence"][0]
        evidence = portable.verify(envelope)
        selection_id = evidence.manifest["observed"]["release"]["selection"]
        selection = self.sources.record("selections", selection_id)
        self.assertEqual(selection, envelope["provenance"].get("selection"))
        self.assertTrue(selection["universe"]["advertisement_blob_refs"])
        for original in selection["originals"]["advertisements"] + selection["originals"]["metadata"]:
            for digest in original["chunks"]:
                self.assertEqual(self.sources.blob(digest), evidence.read(digest))
        for change in ("selection", "original"):
            altered = json.loads(json.dumps(envelope))
            if change == "selection":
                altered["provenance"]["selection"]["universe"]["matching_count"] += 1
            else:
                digest = selection["universe"]["advertisement_blob_refs"][0]
                altered["blobs"] = [blob for blob in altered["blobs"] if blob["sha256"] != digest]
            with self.subTest(change=change), self.assertRaises(portable.PortableError):
                portable.verify(altered)

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

    def test_rehashed_selection_claims_still_need_the_original_universe_and_quotas(self) -> None:
        from oah.design.sources.state import record_digest

        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        for change in ("universe", "extra-response", "kind", "quota"):
            envelope = json.loads(json.dumps(built.record["portable_evidence"][0]))
            selection = envelope["provenance"]["selection"]
            if change == "universe":
                selection["universe"]["matching_count"] += 1
            elif change == "extra-response":
                selection["originals"]["metadata"].append(dict(selection["originals"]["metadata"][-1]))
            elif change == "kind":
                envelope["blobs"][-1]["kind"] = "license"
            else:
                envelope["blobs"][-1]["bytes"] = (8 << 20) + 1
            selection_digest = record_digest(selection)
            envelope["provenance"]["selection_digest"] = selection_digest
            envelope["snapshot"]["manifest"]["observed"]["release"]["selection"] = selection_digest
            envelope["snapshot"]["digest"] = record_digest(envelope["snapshot"]["manifest"])
            observation = envelope["provenance"]["observation"]
            observation["snapshot"] = envelope["snapshot"]["digest"]
            envelope["provenance"]["observation_digest"] = record_digest(observation)
            with self.subTest(change=change), self.assertRaises(portable.PortableError):
                portable.verify(envelope)

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

    def test_authored_and_local_materials_keep_inline_provenance_and_unknown_facts(self) -> None:
        for kind in ("authored", "local-unverified"):
            source = {"kind": kind, "origin": "Local architecture note", "revision": None, "author": "main-ai",
                      "at": self.clock.now().isoformat(), "limitations": ["No public source or license verified"],
                      "evidence": ["Text supplied by the author"], "text": "Keep the booking log in one local file."}
            selection = self.selection(materials=[{"id": "lib", "source": source, "choice": "selected",
                                                  "decision": "storage"}])
            with self.subTest(kind=kind):
                built = materials.build(selection, self.sources, view=self.view(), allow_unverified=True,
                                        now=self.clock.now())
                material = built.record["materials"][0]
                self.assertEqual(kind, material["source"]["kind"])
                self.assertEqual("unverified", material["observation"])
                self.assertIsNone(material["license"])
                self.assertIsNone(material["release"])
                self.assertIsNone(material["portable_evidence_id"])
                self.assertIsNone(material["checked"]["verified_at"])
                self.assertEqual([], built.record["portable_evidence"])
                self.assertEqual([], materials.refs_of(built.text))
                self.assertIn(source["text"], built.text)
                self.assertEqual(0, built.projection["original_payload_bytes"])
                self.assertEqual(built.record, materials.parse(built.text))
                check = materials.check(built.text, None, None, self.view())
                self.assertEqual("current", check["overall"], check)
                self.assertEqual("unknown", check["source_reads"][0]["eligibility"])
                self.assertEqual("unverified(authored)", check["source_reads"][0]["historical_evidence"])
                with self.assertRaises(materials.MaterialsError):
                    materials.build(selection, self.sources, view=self.view(), now=self.clock.now())
                selection["materials"][0]["source"]["verified"] = True
                with self.assertRaises(materials.MaterialsError):
                    materials.build(selection, self.sources, view=self.view(), allow_unverified=True,
                                    now=self.clock.now())



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

    def test_refresh_returns_source_failure_when_a_saved_material_head_is_unreadable(self) -> None:
        self.sources.refresh_for(self.store, "acme", "booking", reason="start")
        original = self.entry()
        good = self.sources.register(build_spec(url=LOCATOR, paths=["LICENSE"], source_id="z-good",
                                                for_designs=["acme/booking"]))[1]
        self.assertNotEqual(self.spec.id, good.id)
        self.clock.advance(2 * 86400)
        read = self.sources.state.read_json

        def read_head(rel, limit):
            if rel == f"heads/{self.spec.id}.json":
                raise OSError("private failure value")
            return read(rel, limit)

        with mock.patch.object(self.sources.state, "read_json", side_effect=read_head):
            report = self.sources.refresh_for(self.store, "acme", "booking", reason="resume")
        results = {item.source: item for item in report.results}
        self.assertEqual({self.spec.id, good.id}, set(results))
        self.assertEqual(("failed", "unavailable"), (results[self.spec.id].result, results[self.spec.id].freshness))
        self.assertEqual("unknown", report.association["status"])
        self.assertEqual(original, self.entry())
        self.assertTrue(report.sources)
        self.assertEqual({None}, {item["verified"] for item in report.sources})
        good_head = self.sources.head(good)
        assert good_head is not None
        self.assertEqual(self.clock.now().isoformat(), good_head.attempted_at)
        self.assertNotIn("private failure value", json.dumps(report.to_json()))

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

    def test_a_real_managed_page_edit_and_an_invalid_final_read_preserve_the_good_association(self) -> None:
        report = self.sources.refresh_for(self.store, "acme", "booking", reason="start").to_json()
        good = self.entry()
        old_view = self.view()
        page = self.repo / self.store.context("acme", "booking").page
        page.write_bytes(page.read_bytes().replace(b"People book a lesson.", b"People share all calendars."))
        edited = self.view()
        self.assertEqual("edited", edited.page_state)
        self.assertFalse(self.sources.matches_view(report, edited)["matches"])
        checked = materials.check(self.saved_text(), self.sources, None, edited)
        self.assertEqual("unknown", checked["overall"])
        self.assertEqual([], self.provider()("acme", "booking"))
        reads = iter([old_view, edited])
        final = self.sources.refresh_for(self.store, "acme", "booking", reason="resume", reader=lambda: next(reads))
        self.assertEqual("unknown", final.association["status"])
        self.assertTrue(final.design_state.startswith("unknown"))
        self.assertFalse(self.sources.matches_view(final.to_json(), old_view)["matches"])
        self.assertEqual(good, self.entry())

    def test_refresh_keeps_sources_referenced_only_by_accepted_materials(self) -> None:
        empty = materials.build({"materials": [], "decisions": []}, self.sources, view=self.view(),
                                now=self.clock.now())
        self.save({"materials": empty.text}, "Working materials no longer use lib")
        self.clock.advance(25 * 3600)
        report = self.sources.refresh_for(self.store, "acme", "booking", reason="resume").to_json()
        self.assertEqual(["lib"], [item["source"] for item in report["results"]])
        self.assertEqual(["lib"], [item["id"] for item in report["sources"]])
        self.assertEqual(["accepted"], report["sources"][0]["roles"])

    def test_acceptance_identity_is_part_of_the_consumer_recheck(self) -> None:
        report = self.sources.refresh_for(self.store, "acme", "booking", reason="start").to_json()
        current = self.view()
        self.assertIsNotNone(current.acceptance)
        changed = dataclasses.replace(current, acceptance=dict(current.acceptance or {}, record="sha256:" + "f" * 64))
        result = self.sources.matches_view(report, changed)
        self.assertFalse(result["matches"])
        self.assertIn("acceptance-changed", result["reasons"])

    def test_invalid_authority_observations_discard_facts_and_keep_last_good_association(self) -> None:
        report = self.sources.refresh_for(self.store, "acme", "booking", reason="start").to_json()
        good = self.entry()
        for changed in (dataclasses.replace(self.view(), freshness="unknown"),
                        dataclasses.replace(self.view(), page_state="unregistered-edit"),
                        dataclasses.replace(self.view(), recovery_pending=True)):
            with self.subTest(freshness=changed.freshness, page=changed.page_state,
                              recovery=changed.recovery_pending):
                self.assertFalse(self.sources.matches_view(report, changed)["matches"])
                again = self.sources.refresh_for(self.store, "acme", "booking", reason="resume", reader=lambda: changed)
                self.assertEqual("unknown", again.association["status"])
                self.assertEqual(good, self.entry())

    def test_material_check_keeps_invalid_a_observations_unknown(self) -> None:
        for changed in (dataclasses.replace(self.view(), freshness="unknown"),
                        dataclasses.replace(self.view(), page_state="unregistered-edit"),
                        dataclasses.replace(self.view(), recovery_pending=True)):
            with self.subTest(freshness=changed.freshness, page=changed.page_state,
                              recovery=changed.recovery_pending):
                report = materials.check(self.saved_text(), self.sources, None, changed)
                self.assertEqual("unknown", report["overall"])

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

    @unittest.skipUnless(sys.platform == "darwin", "native macOS descriptor staging")
    def test_staging_is_an_owned_unlinked_descriptor_on_the_pinned_filesystem(self) -> None:
        observed: list[int] = []
        original = output._publish
        missing = str(self.root / "missing-staging")

        def publish(handle: int, parent: int, final: str) -> None:
            info = os.fstat(handle)
            self.assertEqual(os.fstat(parent).st_dev, info.st_dev)
            self.assertEqual(0, info.st_nlink)
            self.assertFalse(os.get_inheritable(handle))
            self.assertEqual(missing, os.environ["TMPDIR"])
            observed.append(handle)
            original(handle, parent, final)

        with mock.patch.dict(os.environ, {"TMPDIR": missing}), mock.patch.object(output, "_publish", side_effect=publish):
            path = output.write_new(self.root, "out/materials.md", b"owned bytes")
        self.assertEqual(1, len(observed))
        with self.assertRaises(OSError):
            os.fstat(observed[0])
        self.assertEqual(b"owned bytes", path.read_bytes())
        self.assertEqual(["out", "out/materials.md"], self.listing())

    @unittest.skipUnless(sys.platform == "darwin", "native macOS descriptor staging")
    def test_unavailable_staging_interpreter_refuses_without_public_files(self) -> None:
        with mock.patch.object(output.sys, "executable", str(self.root / "missing-python")):
            self.refuses("out/materials.md", output.UNVERIFIABLE_CODE, "anonymous-file-unavailable")

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

    def test_replaced_temporary_never_publishes_or_unlinks_the_replacement(self) -> None:
        substituted = []

        def replace(step: str) -> None:
            if step == "temporary-written":
                names = list((self.root / "out").glob("*.tmp"))
                temp = names[0] if names else self.root / "out" / ".materials.md.oah-decoy.tmp"
                if names:
                    temp.rename(self.root / "out" / "moved-owned")
                temp.write_bytes(b"theirs")
                substituted.append(temp)

        try:
            final = output.write_new(self.root, "out/materials.md", b"ours", fault=replace)
        except output.OutputError:
            self.assertFalse((self.root / "out" / "materials.md").exists())
        else:
            self.assertEqual(b"ours", final.read_bytes())
        moved = self.root / "out" / "moved-owned"
        if moved.exists():
            self.assertEqual(b"ours", moved.read_bytes())
        self.assertEqual(b"theirs", substituted[0].read_bytes())


class WindowsOutputTest(unittest.TestCase):
    def test_native_primitives_share_destination_writes_but_deny_directory_replacement(self) -> None:
        import ctypes
        from types import SimpleNamespace
        from oah.design.sources.output_windows import Native

        api = SimpleNamespace(CreateFileW=mock.Mock(return_value=7), SetFileInformationByHandle=mock.Mock(return_value=1))
        native = object.__new__(Native)
        native.api = api
        self.assertEqual(7, native.open_directory("C:/repo/out"))
        directory = api.CreateFileW.call_args.args
        self.assertEqual((0x81, 3, None, 3, 0x02200000, None), directory[1:])
        def information(_handle, kind, _buffer, _size):
            if kind == 3:
                self.assertEqual(3, directory[2] & 3)
                self.assertEqual(0, directory[2] & 4)
            return 1
        api.SetFileInformationByHandle.side_effect = information
        self.assertEqual(7, native.create_file("C:/repo/out/temp"))
        self.assertEqual((0xC0010000, 1, None, 1, 0x00200080, None), api.CreateFileW.call_args.args[1:])
        native.rename(7, "C:/repo/out/materials.md")
        handle, kind, buffer, size = api.SetFileInformationByHandle.call_args.args
        self.assertEqual((7, 3), (handle, kind))
        self.assertEqual(0, ctypes.string_at(buffer, 1)[0])
        self.assertTrue(ctypes.string_at(buffer, size).endswith("C:/repo/out/materials.md".encode("utf-16-le")))
        native.discard(7)
        self.assertEqual((7, 4), api.SetFileInformationByHandle.call_args.args[:2])
        self.assertEqual(7, native.open_file("C:/repo/out/materials.md"))
        self.assertEqual((0x80, 7, None, 3, 0x00200080, None), api.CreateFileW.call_args.args[1:])

    def test_native_identity_reads_the_full_volume_serial_and_128_bit_file_id(self) -> None:
        from types import SimpleNamespace
        from oah.design.sources.output_windows import Info, Native

        serial, identifier = 0xFEDCBA9876543210, bytes(range(16))

        def information(_handle, kind, pointer, size):
            match kind:
                case 9:
                    pointer._obj.attributes = 0x80
                case 18:
                    self.assertEqual(24, size)
                    pointer._obj.volume = serial
                    pointer._obj.identifier[:] = identifier
                case _:
                    self.fail(f"unexpected file information class {kind}")
            return 1

        def final_path(_handle, buffer, _size, flags):
            self.assertEqual(0, flags)
            buffer.value = "\\\\?\\C:\\repo\\out\\materials.md"
            return len(buffer.value)

        api = SimpleNamespace(GetFileType=mock.Mock(return_value=1),
            GetFileInformationByHandleEx=mock.Mock(side_effect=information),
            GetFinalPathNameByHandleW=mock.Mock(side_effect=final_path))
        native = object.__new__(Native)
        with mock.patch.object(native, "api", api, create=True):
            self.assertEqual(Info("C:\\repo\\out\\materials.md", serial, identifier, 0x80), native.info(7))
        self.assertEqual([9, 18], [call.args[1] for call in api.GetFileInformationByHandleEx.call_args_list])

    def api(self):
        from oah.design.sources.output_windows import Info

        class FakeNative:
            def __init__(self):
                self.handles = {}
                self.identities = {}
                self.closed = []
                self.reparse = False
                self.unsupported = False
                self.fail_write = False

            def open_directory(self, name):
                return self.pin(name)

            def pin(self, name):
                handle = len(self.handles) + 1
                self.handles[handle] = name
                info = pathlib.Path(name).stat()
                self.identities[handle] = Info(name, info.st_dev, info.st_ino.to_bytes(16, "little"),
                                               0x10 if pathlib.Path(name).is_dir() else 0x80)
                return handle

            def create_file(self, name):
                with pathlib.Path(name).open("xb"):
                    pass
                return self.pin(name)

            def open_file(self, name):
                return self.pin(name)

            def info(self, handle):
                info = self.identities[handle]
                return dataclasses.replace(info, attributes=info.attributes | (0x400 if self.reparse else 0))

            def volume(self, handle):
                if self.unsupported:
                    raise output.OutputError(output.UNVERIFIABLE_CODE, "filesystem-unsupported", "unsupported filesystem")

            def names(self, handle):
                return os.listdir(self.handles[handle])

            def write(self, handle, data):
                if self.fail_write:
                    raise OSError("injected write failure")
                pathlib.Path(self.handles[handle]).write_bytes(data)

            def flush(self, handle):
                pass

            def rename(self, handle, destination):
                if os.path.lexists(destination):
                    error = OSError("already exists")
                    error.winerror = 183
                    raise error
                os.rename(self.handles[handle], destination)
                self.handles[handle] = destination

            def discard(self, handle):
                os.unlink(self.handles[handle])

            def close(self, handle):
                self.closed.append(handle)

        return FakeNative()

    def test_supported_windows_handle_path_creates_an_ordinary_file(self) -> None:
        from oah.design.sources import output_windows

        root = make_tmp_dir(self)
        (root / "out").mkdir()
        api = self.api()
        with mock.patch.object(output_windows, "Native", return_value=api):
            path = output.write_new(root, "out/materials.md", b"ours", windows=True)
        self.assertEqual(b"ours", path.read_bytes())
        self.assertEqual(["materials.md"], os.listdir(root / "out"))
        self.assertEqual(set(api.handles), set(api.closed))

    def test_missing_native_apis_refuse_before_writing(self) -> None:
        from oah.design.sources import output_windows

        root = make_tmp_dir(self)
        with mock.patch.object(output_windows.ctypes, "WinDLL", create=True, side_effect=OSError("unavailable")), \
                self.assertRaises(output.OutputError) as caught:
            output.write_new(root, "materials.md", b"x", windows=True)
        self.assertEqual((output.UNVERIFIABLE_CODE, "windows-api-unavailable"),
                         (caught.exception.code, caught.exception.reason))
        self.assertEqual([], list(root.iterdir()))

    def test_a_published_file_with_a_different_id_or_volume_is_refused(self) -> None:
        from oah.design.sources import output_windows

        for field, replacement in (("file_id", b"x" * 16), ("volume", 2**63 + 1),
                                   ("path", "C:/elsewhere/materials.md")):
            with self.subTest(field=field):
                root = make_tmp_dir(self)
                api = self.api()
                original = api.open_file

                def swapped(name):
                    handle = original(name)
                    api.identities[handle] = dataclasses.replace(api.identities[handle], **{field: replacement})
                    return handle

                api.open_file = swapped
                with mock.patch.object(output_windows, "Native", return_value=api), \
                        self.assertRaises(output.OutputError) as caught:
                    output.write_new(root, "materials.md", b"ours", windows=True)
                self.assertEqual("identity-changed", caught.exception.reason)
                self.assertEqual(set(api.handles), set(api.closed))

    def test_a_changed_pinned_directory_is_refused_before_publication(self) -> None:
        from oah.design.sources import output_windows

        for stage in ("parent-pinned", "temporary-written"):
            with self.subTest(stage=stage):
                root = make_tmp_dir(self)
                api = self.api()

                def swap(step):
                    if step == stage:
                        api.identities[1] = dataclasses.replace(api.identities[1], file_id=b"x" * 16)

                with mock.patch.object(output_windows, "Native", return_value=api), \
                        self.assertRaises(output.OutputError) as caught:
                    output.write_new(root, "materials.md", b"ours", windows=True, fault=swap)
                self.assertEqual("identity-changed", caught.exception.reason)
                self.assertEqual([], list(root.iterdir()))
                self.assertEqual(set(api.handles), set(api.closed))

    def test_windows_reparse_filesystem_and_write_failures_preserve_other_files(self) -> None:
        from oah.design.sources import output_windows

        for field, reason in (("reparse", "link"), ("unsupported", "filesystem-unsupported"),
                              ("fail_write", "windows-handle-refused")):
            root = make_tmp_dir(self)
            api = self.api()
            setattr(api, field, True)
            with self.subTest(field=field), mock.patch.object(output_windows, "Native", return_value=api), \
                    self.assertRaises(output.OutputError) as caught:
                output.write_new(root, "materials.md", b"ours", windows=True)
            self.assertEqual(reason, caught.exception.reason)
            self.assertEqual([], list(root.iterdir()))
            self.assertEqual(set(api.handles), set(api.closed))



class ProposalAssessmentTest(MaterialsCase):
    def prepare_proposal(self: MaterialsCase, selection=None):
        from oah.design.sources import assessment
        from tests.test_design_judgement import FakeJudge, config

        built = materials.build(selection or self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.sources.refresh_for(self.store, "acme", "booking", reason="start")
        self.release("v1.1.0", {"src/lib.rs": "pub fn changed() {}\n"})
        self.assertEqual("changed", self.sources.check_one(self.spec).result)
        proposals, findings = self.sources.proposals()
        self.assertEqual([], findings)
        self.assertEqual(1, len(proposals))
        provider = FakeJudge()
        provider.labels = {"may_break_users": 0.82, "security_fix": 0.93, "drops_platform": 0.1}
        return assessment, proposals, provider, config()

    def test_roles_follow_decision_membership_in_current_and_accepted_materials(self) -> None:
        def selection(role: str, snapshot: str | None = None):
            return self.selection(
                decisions=[{"id": "storage", "status": "selected" if role == "chosen" else "open",
                            "chosen": "storage-option" if role == "chosen" else None,
                            "alternatives": ["storage-option"] if role == "alternative" else [], "reason": "Compare"}],
                materials=[{"id": "storage-option", "source": "lib", "decision": "storage",
                            "choice": "selected" if role == "chosen" else "candidate",
                            **({"snapshot": snapshot} if snapshot else {})}])

        assessment, proposals, provider, cfg = self.prepare_proposal(selection("alternative"))
        snapshot = proposals[0].from_snapshot
        reader = lambda project, design: read_view(self.store, project, design)
        cases = ((None, "alternative"), ("dependency", "alternative"), ("chosen", "chosen"),
                 ("alternative", "chosen"), ("dependency", "chosen"), (None, "dependency"))
        for index, (current, expected) in enumerate(cases):
            with self.subTest(current=current, expected=expected):
                if index in (1, 3, 5):
                    self.accept_current()
                if current:
                    changed = selection(current, snapshot)
                    changed["resolutions"] = [self.resolve("reselected", selection=changed)]
                    built = materials.build(changed, self.sources, view=self.view(), now=self.clock.now())
                    self.save({"materials": built.text}, "Explicit material selection")
                self.sources.refresh_for(self.store, "acme", "booking", reason="resume")
                proposals, findings = self.sources.proposals()
                self.assertEqual([], findings)
                self.assertEqual(1, len(proposals))
                before = len(provider.calls)
                rows = assessment.assess(self.sources, proposals, reader=reader, config=cfg,
                                         env={"FAKE_JUDGE_KEY": "k"}, providers={"fake": provider})
                self.assertEqual(before + 1, len(provider.calls))
                self.assertEqual(expected, provider.calls[-1][0]["fields"]["role"])
                self.assertEqual("yes", rows[0].assessment["applied"])
                self.assertEqual("open", rows[0].status)

    def test_public_assessment_is_saved_displayed_and_has_no_authority(self) -> None:
        assessment, proposals, provider, config = self.prepare_proposal()
        before_files = {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*")
                        if path.is_file() and ".git" not in path.relative_to(self.repo).parts}
        before_head = self.sources.head(self.spec).to_json()
        before_material = materials.parse(self.saved_text())
        reader = lambda project, design: read_view(self.store, project, design)
        with mock.patch.object(self.store, "save", side_effect=AssertionError("assessment may not save")), \
                mock.patch.object(self.store, "accept", side_effect=AssertionError("assessment may not accept")), \
                mock.patch.object(self.sources, "dismiss", side_effect=AssertionError("assessment may not dismiss")):
            rows = assessment.assess(self.sources, proposals, reader=reader, config=config,
                                     env={"FAKE_JUDGE_KEY": "k"}, providers={"fake": provider})
        self.assertEqual(1, len(provider.calls))
        state, questions = provider.calls[0]
        self.assertEqual({"may_break_users", "security_fix", "drops_platform", "urgency"}, set(questions))
        self.assertEqual("score", questions["urgency"]["type"])
        self.assertEqual(("v1.0.0", "v1.1.0", "minor", "chosen"), tuple(state["fields"][key] for key in
                         ("from_tag", "to_tag", "version_delta", "role")))
        self.assertNotIn("One teacher", json.dumps(state))
        row = rows[0]
        self.assertEqual(("used", "yes", "2"), (row.assessment["jev"], row.assessment["applied"],
                                                 row.assessment["urgency"]))
        self.assertEqual({"may_break_users", "security_fix"}, {item["label"] for item in row.assessment["labels"]})
        self.assertEqual(proposals[0].labels, row.labels)
        self.assertEqual("open", row.status)
        digest = row.assessment["record"]
        stored = self.sources.assessment(digest)
        self.assertEqual("oah-proposal-assessment", stored["kind"])
        self.assertEqual("source-change-assessment/v1", stored["judgement"]["identity"]["rubric"])
        displayed = assessment.recorded(self.sources, proposals, reader=reader, config=config)[0]
        self.assertEqual(row.assessment, displayed.assessment)
        self.assertEqual(before_head, self.sources.head(self.spec).to_json())
        self.assertEqual(before_material, materials.parse(self.saved_text()))
        self.assertEqual(before_files, {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*")
                                      if path.is_file() and ".git" not in path.relative_to(self.repo).parts})
        changed = dict(config, judgement=dict(config["judgement"], yes=0.95))
        stale = assessment.recorded(self.sources, proposals, reader=reader, config=changed)[0]
        self.assertEqual("no(thresholds-changed)", stale.assessment["applied"])
        self.assertEqual(stored, self.sources.assessment(digest))

    def test_a_stale_proposal_does_not_prevent_a_healthy_assessment(self) -> None:
        assessment, proposals, provider, config = self.prepare_proposal()
        stale = dataclasses.replace(proposals[0], key="0" * 64, design="removed-design")
        rows = assessment.assess(self.sources, [stale, *proposals],
                                 reader=lambda project, design: read_view(self.store, project, design),
                                 config=config, env={"FAKE_JUDGE_KEY": "k"}, providers={"fake": provider})
        self.assertEqual(["no(stale-or-unknown)", "yes"], [p.assessment["applied"] for p in rows])
        self.assertEqual(1, len(provider.calls))
        pointers = self.sources.state.read_json("assessment-index.json", 1 << 20)
        assert isinstance(pointers, dict)
        self.assertNotIn(stale.key, pointers["assessments"])

    def test_a_design_change_during_assessment_preserves_prior_good_evidence(self) -> None:
        assessment, proposals, provider, config = self.prepare_proposal()
        reader = lambda project, design: read_view(self.store, project, design)
        first = assessment.assess(self.sources, proposals, reader=reader, config=config,
                                  env={"FAKE_JUDGE_KEY": "k"}, providers={"fake": provider})[0]
        before_index = self.sources.state.read_json("assessment-index.json", 1 << 20)
        good = self.sources.assessment(first.assessment["record"])
        original = provider.judge

        def concurrent_change(*args, **kwargs):
            reply = original(*args, **kwargs)
            self.save({"request": MULTI}, "Concurrent correction")
            return reply

        with mock.patch.object(provider, "judge", side_effect=concurrent_change):
            rows = assessment.assess(self.sources, proposals, reader=reader, config=config,
                                     env={"FAKE_JUDGE_KEY": "k"}, providers={"fake": provider})
        self.assertEqual("no(stale-or-unknown)", rows[0].assessment["applied"])
        self.assertEqual(before_index, self.sources.state.read_json("assessment-index.json", 1 << 20))
        self.assertEqual(good, self.sources.assessment(first.assessment["record"]))
        self.assertEqual("open", self.sources.proposals()[0][0].status)


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

    def test_malformed_materials_cli_returns_invalid_and_healthy_source_reads(self) -> None:
        selection = self.selection(materials=[
            {"id": "lib", "source": "lib", "choice": "selected", "decision": "storage"},
            {"id": "healthy", "source": "lib", "choice": "candidate", "decision": "storage"}])
        built = materials.build(selection, self.sources, view=self.view())
        record = json.loads(materials.canonical_json(built.record))
        next(item for item in record["materials"] if item["id"] == "lib")["license"] = "MIT"
        text = built.text[:built.text.rindex(materials.FENCE)] + materials.FENCE + "\n" \
            + materials.canonical_json(record) + "\n```\n"
        self.save({"materials": text})
        code, out, err = self.cli("sources", "materials", "check", "--design", "acme/booking", "--json")
        self.assertEqual((1, ""), (code, err))
        report = json.loads(out)
        self.assertEqual("invalid", report["overall"])
        reads = {item["material"]: item for item in report["source_reads"]}
        self.assertEqual({"healthy", "lib"}, set(reads))
        self.assertEqual(portable.HISTORICAL, reads["healthy"]["historical_evidence"])

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

    @unittest.skipUnless(sys.platform == "darwin", "native macOS anonymous staging")
    def test_render_out_uses_the_destination_with_a_missing_private_tmpdir(self) -> None:
        selection = str(self.selection_file())
        (self.repo / "out").mkdir()
        missing = self.repo.parent / "missing" / "tmp"
        self.assertFalse(missing.exists())
        with mock.patch.dict(os.environ, {"TMPDIR": str(missing)}):
            code, text, err = self.cli("sources", "materials", "render", "--design", "acme/booking", "--input", selection)
            self.assertEqual(0, code, err)
            self.assertIsNotNone(materials.parse(text))
            code, _out, err = self.cli("sources", "materials", "render", "--design", "acme/booking", "--input", selection,
                                      "--out", "out/materials.md")
        self.assertEqual(0, code, err)
        self.assertEqual(text.encode(), (self.repo / "out/materials.md").read_bytes())
        self.assertEqual(["materials.md"], sorted(p.name for p in (self.repo / "out").iterdir()))
        self.assertFalse(missing.exists())

    def test_stale_advice_keeps_other_proposals_and_never_reassesses(self) -> None:
        from oah.config import load_config
        from oah.rerank import DEFAULTS, judge
        from oah.design.sources.state import digest_of

        assessment, proposals, provider, _cfg = ProposalAssessmentTest.prepare_proposal(self)
        (self.repo / ".oah/config.toml").write_text(
            '[rerank]\nenabled = true\nprovider = "fake"\n[rerank.judgement]\nenabled = true\n', encoding="utf-8")
        cfg = load_config(self.repo).section("rerank", DEFAULTS)
        other = self.store.new("acme", "reporting", title="Reporting", parts={"design": "Reports.\n"}, today=TODAY)
        assert other.revision is not None and other.digest is not None
        other_view = read_view(self.store, "acme", "reporting")
        selected = self.selection()
        selected["materials"][0]["snapshot"] = proposals[0].from_snapshot
        built = materials.build(selected, self.sources, view=other_view, now=self.clock.now())
        self.store.save("acme", "reporting", expect_revision=other.revision, expect_digest=other.digest,
                        reason="Materials", parts={"materials": built.text}, today=TODAY)
        self.sources.refresh_for(self.store, "acme", "reporting", reason="start")
        reader = lambda project, design: read_view(self.store, project, design)
        proposals, findings = self.sources.proposals()
        self.assertEqual([], findings)
        assessed = assessment.assess(self.sources, proposals, reader=reader, config=cfg,
                                     env={"FAKE_JUDGE_KEY": "k"}, providers={"fake": provider})
        self.assertEqual(["yes", "yes"], [p.assessment["applied"] for p in assessed])
        stale = next(p for p in assessed if p.design == "booking")
        stored = self.sources.assessment(stale.assessment["record"])
        assert stored is not None
        self.save({"request": MULTI}, "New requirements")
        before = {str(p.relative_to(self.sources.state.root)): p.read_bytes()
                  for p in self.sources.state.root.rglob("*") if p.is_file()}
        calls = len(provider.calls)
        with mock.patch.object(judge, "judge", side_effect=AssertionError("listing cannot infer")), \
                mock.patch.object(Sources, "refresh_for", side_effect=AssertionError("listing cannot refresh")):
            rows = assessment.recorded(self.sources, proposals, reader=reader, config=cfg)
            by_design = {p.design: p for p in rows}
            self.assertEqual({"booking", "reporting"}, set(by_design))
            self.assertEqual("no(stale-or-unknown)", by_design["booking"].assessment["applied"])
            self.assertEqual([], by_design["booking"].assessment["labels"])
            self.assertIsNone(by_design["booking"].assessment["urgency"])
            self.assertEqual("yes", by_design["reporting"].assessment["applied"])
            code, out, err = self.cli("sources", "proposals", "--json")
            self.assertEqual(0, code, err)
            self.assertEqual({"booking", "reporting"}, {p["design"] for p in json.loads(out)["proposals"]})
            rows = assessment.assess(self.sources, [stale], reader=reader, config=cfg,
                                     env={"FAKE_JUDGE_KEY": "k"}, providers={"fake": provider})
            self.assertEqual("no(stale-or-unknown)", rows[0].assessment["applied"])
        current = dict(stored["basis"], binding=reader("acme", "booking").identity())
        request = assessment.request_for(self.sources, stale, current, reader("acme", "booking"))
        applied = judge.apply(stored["judgement"], current_input_ref=digest_of(current), config=cfg,
                              sources=self.sources, request=request, policy=self.sources.policy_digest())
        self.assertEqual((False, "stale-input"), (applied.applied, applied.reason))
        self.assertEqual(calls, len(provider.calls))
        self.assertEqual(before, {str(p.relative_to(self.sources.state.root)): p.read_bytes()
                                 for p in self.sources.state.root.rglob("*") if p.is_file()})

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

    def test_diff_terminal_output_escapes_external_path_names(self) -> None:
        path = "src/\x1b[31mred\u202eevil.py"
        report = {"completeness": "complete", "paths": {"entries": [{"change": "added", "path": path}]},
                  "text_diffs": [{"item": path, "status": "shown", "lines": []}]}
        digest = "sha256:" + "0" * 64
        with mock.patch.object(Sources, "diff", return_value=report):
            code, out, err = self.cli("sources", "diff", "lib", "--from", digest, "--to", digest)
        self.assertEqual(0, code, err)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertEqual(2, out.count("src/\\u001b[31mred\\u202eevil.py"))

    def test_unavailable_a_check_is_unknown_without_fabricated_materials(self) -> None:
        with mock.patch("oah.design.sources.read_view", side_effect=ViewUnavailable("ref-unavailable")):
            code, out, err = self.cli("sources", "materials", "check", "--design", "acme/booking", "--json")
        self.assertEqual(1, code, err)
        report = json.loads(out)
        self.assertEqual("unknown", report["overall"])
        self.assertIsNone(report["materials_digest"])
        self.assertEqual([], report["decisions"])
        self.assertEqual("ref-unavailable", report["checked_view"]["authority_observation"]["code"])

    def test_unavailable_a_render_is_a_closed_refusal(self) -> None:
        with mock.patch("oah.design.sources.read_view", side_effect=ViewUnavailable("ref-unavailable")):
            code, out, err = self.cli("sources", "materials", "render", "--design", "acme/booking", "--input",
                                      str(self.selection_file()))
        self.assertEqual(69, code)
        self.assertEqual("", out)
        self.assertIn("OAH_SOURCES_DESIGN_UNAVAILABLE", err)
        self.assertIn("ref-unavailable", err)

    def test_historical_a_read_failure_keeps_the_original_closed_residual(self) -> None:
        with mock.patch("oah.design.store.DesignStore.snapshot", side_effect=design_fs.RefUnavailable("not available")):
            code, out, err = self.cli("sources", "materials", "check", "--design", "acme/booking", "--revision", "1", "--json")
        self.assertEqual(1, code, err)
        report = json.loads(out)
        self.assertEqual("unknown", report["overall"])
        self.assertIsNone(report["materials_digest"])
        self.assertEqual("ref-unavailable", report["checked_view"]["authority_observation"]["code"])

    def test_explicit_proposal_assessment_has_an_honest_disabled_fallback(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        self.sources.refresh_for(self.store, "acme", "booking", reason="start")
        self.release("v1.1.0", {"src/lib.rs": "pub fn changed() {}\n"})
        self.assertEqual("changed", self.sources.check_one(self.spec).result)
        before = self.view().identity()
        code, out, err = self.cli("sources", "proposals", "assess", "--design", "acme/booking", "--json")
        self.assertEqual(0, code, err)
        proposals = json.loads(out)["proposals"]
        self.assertTrue(proposals)
        self.assertEqual("unused(judgement-off)", proposals[0]["assessment"]["jev"])
        self.assertEqual("no(judgement-off)", proposals[0]["assessment"]["applied"])
        self.assertEqual("open", proposals[0]["status"])
        self.assertEqual(before, self.view().identity())



# ---- shared shapes ---------------------------------------------------------------------------------------------
# Neutral JSON that other parts of oah build against without importing this code: the real output of the calls in
# SharedShapesTest for its fixed inputs, with run-specific identities replaced by markers. A change here is a change
# to a shared record shape, so make it together with the code that reads it.
SHARED_SHAPES = json.loads(r"""
{
 "material-input-basis-v1": {
  "version": 1,
  "project": "acme",
  "uid": "<id>",
  "design": "booking",
  "title": "Booking",
  "parts": {
   "design": "sha256:<digest>",
   "request": "sha256:<digest>"
  },
  "decision_inputs_digest": "sha256:<digest>"
 },
 "material-check-v1": {
  "version": 1,
  "checked_view": {
   "accepted": {
    "project": "acme",
    "uid": "<id>",
    "design": "booking",
    "revision": 1,
    "digest": "sha256:<digest>"
   },
   "working": {
    "project": "acme",
    "uid": "<id>",
    "design": "booking",
    "revision": 2,
    "digest": "sha256:<digest>"
   },
   "authority_observation": {
    "status": "read",
    "source": "worktree",
    "freshness": "current",
    "page_state": "clean",
    "recovery_pending": false,
    "acceptance": {
     "sequence": 1,
     "record": "sha256:<digest>",
     "revision": 1,
     "digest": "sha256:<digest>"
    }
   }
  },
  "materials_digest": "sha256:<digest>",
  "policy_digest": "sha256:<digest>",
  "source_reads": [
   {
    "material": "lib",
    "source": "lib",
    "spec_digest": "sha256:<digest>",
    "snapshot": "sha256:<digest>",
    "envelope": "sha256:<digest>",
    "historical_evidence": "verified-from-portable-bytes",
    "historical_times": {
     "verified_at": "<time>",
     "confirmed_at": "<time>"
    },
    "recorded_eligibility": "eligible",
    "eligibility": "eligible",
    "integration": "untested",
    "registration": "same-spec",
    "registered_head": "sha256:<digest>",
    "upstream_freshness": "fresh",
    "problems": []
   }
  ],
  "decisions": [
   {
    "id": "storage",
    "selection_status": "selected",
    "input_status": "current",
    "current_basis": "sha256:<digest>",
    "recorded_basis": "sha256:<digest>",
    "execution_relevance": "selected",
    "problems": [],
    "resolution_ref": null
   }
  ],
  "overall": "current",
  "problems": []
 },
 "materials-projection-v1": {
  "version": 1,
  "kind": "oah-materials-projection",
  "part_bytes": 20994,
  "part_digest": "sha256:<digest>",
  "semantic_bytes": 19034,
  "semantic_sha256": "<sha256>",
  "original_payload_bytes": 1960,
  "envelopes": [
   {
    "id": "sha256:<digest>",
    "source": "lib",
    "snapshot": "sha256:<digest>",
    "blobs": [
     {
      "id": "sha256:<digest>",
      "kind": "selected-file",
      "path_or_field": "src/lib.rs",
      "bytes": 14,
      "sha256": "<sha256>",
      "encoded_bytes": 20
     },
     {
      "id": "sha256:<digest>",
      "kind": "license",
      "path_or_field": "LICENSE",
      "bytes": 1061,
      "sha256": "<sha256>",
      "encoded_bytes": 1416
     },
     {
      "id": "sha256:<digest>",
      "kind": "release-notes",
      "path_or_field": "observed.release.notes",
      "bytes": 16,
      "sha256": "<sha256>",
      "encoded_bytes": 24
     },
     {
      "id": "sha256:<digest>",
      "kind": "release-advertisement",
      "path_or_field": "selection.release-advertisement.0.0",
      "bytes": 119,
      "sha256": "<sha256>",
      "encoded_bytes": 160
     },
     {
      "id": "sha256:<digest>",
      "kind": "release-metadata",
      "path_or_field": "selection.release-metadata.0.0",
      "bytes": 253,
      "sha256": "<sha256>",
      "encoded_bytes": 340
     }
    ]
   }
  ],
  "inline_budget": 262144,
  "semantic_fits_inline": true,
  "limit": 1048576
 },
 "sources-refresh-v1": {
  "version": 1,
  "kind": "oah-sources-refresh",
  "reason": "start",
  "project": "acme",
  "design": "booking",
  "binding": {
   "uid": "<id>",
   "design": "booking",
   "current": {
    "project": "acme",
    "uid": "<id>",
    "design": "booking",
    "revision": 2,
    "digest": "sha256:<digest>"
   },
   "accepted": {
    "project": "acme",
    "uid": "<id>",
    "design": "booking",
    "revision": 2,
    "digest": "sha256:<digest>"
   },
   "acceptance": {
    "sequence": 2,
    "record": "sha256:<digest>",
    "revision": 2,
    "digest": "sha256:<digest>"
   },
   "authority_observation": {
    "authority": "refs/heads/main",
    "source": "worktree",
    "freshness": "current",
    "page_state": "clean",
    "recovery_pending": false
   }
  },
  "design_state": "current",
  "materials": {
   "current": "sha256:<digest>",
   "accepted": "sha256:<digest>"
  },
  "sources": [
   {
    "id": "lib",
    "locator": "https://github.com/acme/lib",
    "spec_digest": "sha256:<digest>",
    "snapshot": "sha256:<digest>",
    "registration": "<id>",
    "registered_spec": "sha256:<digest>",
    "verified": "sha256:<digest>",
    "roles": ["current", "accepted"]
   }
  ],
  "association": {
   "status": "published",
   "key": "<id>/booking",
   "generation": 1,
   "digest": "sha256:<digest>"
  },
  "results": [
   {
    "source": "lib",
    "result": "skipped-fresh",
    "snapshot": null,
    "previous": null,
    "problems": [],
    "http": null,
    "notable": false,
    "freshness": "fresh",
    "retry_after": null,
    "observation": "sha256:<digest>",
    "selection": null
   }
  ],
  "unvisited": [],
  "unregistered": [],
  "proposals": [],
  "findings": [],
  "problems": [],
  "policy": "sha256:<digest>",
  "observation_window": {
   "started_at": "<time>",
   "ended_at": "<time>"
  }
 },
 "view-match-v1": {
  "matches": true,
  "reasons": []
 },
 "source-facts-v1": [
  "sources 1 fresh"
 ],
 "material-resolution-v1": {
  "kind": "carry",
  "decision_id": "storage",
  "from_basis": "sha256:<digest>",
  "to_basis": "sha256:<digest>",
  "old_and_new_design_refs": {
   "old": {
    "project": "acme",
    "uid": "<id>",
    "design": "booking",
    "revision": 2,
    "digest": "sha256:<digest>"
   },
   "new": {
    "project": "acme",
    "uid": "<id>",
    "design": "booking",
    "revision": 3,
    "digest": "sha256:<digest>"
   }
  },
  "source_dependencies": [
   {
    "material": "lib",
    "source": "lib",
    "spec_digest": "sha256:<digest>",
    "snapshot": "sha256:<digest>",
    "registered_spec": "sha256:<digest>",
    "head": "sha256:<digest>"
   }
  ],
  "policy_digest": "sha256:<digest>",
  "reason": "Reviewed old and new bodies",
  "actor": "main-ai",
  "at": "<time>"
 }
}
""")
_MARKERS = ((re.compile(r"sha256:[0-9a-f]{64}"), "sha256:<digest>"), (re.compile(r"\b[0-9a-f]{64}\b"), "<sha256>"),
            (re.compile(r"\b[0-9a-f]{40}\b"), "<commit>"), (re.compile(r"\b[0-9a-f]{32}\b"), "<id>"),
            (re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(?:Z|[+-][0-9]{2}:[0-9]{2})"), "<time>"))


def neutral(value: object) -> object:
    """Digests, commits, ids, registration tokens and times replaced by fixed markers; everything else kept."""
    if isinstance(value, dict):
        return {key: neutral(item) for key, item in value.items()}
    if isinstance(value, list):
        return [neutral(item) for item in value]
    if isinstance(value, str):
        for pattern, marker in _MARKERS:
            value = pattern.sub(marker, value)
    return value


class SharedShapesTest(MaterialsCase):
    """The input basis, check, resolution, projection, refresh report, view match and facts lines as the API emits
    them, so another part can be built against the JSON alone."""

    def test_the_api_emits_the_shared_shapes(self) -> None:
        built = materials.build(self.selection(), self.sources, view=self.view(), now=self.clock.now())
        self.save({"materials": built.text}, "Materials")
        inputs = self.selection()["decision_inputs"]["storage"]
        actual = {"material-input-basis-v1": materials.input_basis(self.view(), inputs),
                  "material-check-v1": self.check(), "materials-projection-v1": built.projection}
        canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.assertEqual("sha256:" + hashlib.sha256(canonical).hexdigest(),
                         actual["material-input-basis-v1"]["decision_inputs_digest"])
        self.accept_current()
        report = self.sources.refresh_for(self.store, "acme", "booking", reason="start").to_json()
        actual["sources-refresh-v1"] = report
        actual["view-match-v1"] = self.sources.matches_view(report, self.view())
        actual["source-facts-v1"] = self.sources.list_facts(
            lambda project, design: read_view(self.store, project, design))("acme", "booking")
        self.save({"request": MULTI}, "Corrected")
        actual["material-resolution-v1"] = self.resolve("carry")
        self.assertEqual(set(SHARED_SHAPES), set(actual))
        for name, shape in SHARED_SHAPES.items():
            with self.subTest(shape=name):
                self.assertEqual(shape, neutral(actual[name]))


if __name__ == "__main__":
    unittest.main()
