# 출처·재료 판정 시험 — 비밀 패턴이 없는 사적 요구 문장도 허락 없이는 보내지 않고, 검증한 공개 조각과 허락한 설계 원문만 보내며, 게이트 순서·전체 문자열 검사·신원 재확인과 바뀐 릴리스 노트의 캐시 거부를 본다.
from __future__ import annotations

import copy
import unittest

from oah import rerank
from oah.design.sources.view import DesignRef, DesignView, SnapshotView
from oah.rerank import judge
from tests.test_design_sources import LICENSE, SourceCase

POLICY = "sha256:" + "a" * 64


class FakeJudge:
    name = "fake"

    def __init__(self, answer=("review", 0.8)) -> None:
        self.answer = answer
        self.calls: list[tuple] = []

    def key_name(self, config) -> str:
        return "FAKE_JUDGE_KEY"

    def judge(self, state, instructions, options, *, key, config):
        self.calls.append((copy.deepcopy(state), instructions, dict(options)))
        return self.answer


def config(*, enabled: bool = True, design_text: bool = False, rerank_on: bool = True) -> dict:
    data = copy.deepcopy(rerank.DEFAULTS)
    data.update(enabled=rerank_on, provider="fake")
    data["judgement"] = {"enabled": enabled, "send_design_text": design_text}
    return data


def view(texts: dict, revision: int = 2) -> DesignView:
    ref = DesignRef("acme", "0" * 32, "booking", revision, "sha256:" + "b" * 64)
    snapshot = SnapshotView(ref, "Booking", {name: "sha256:" + "c" * 64 for name in texts}, texts)
    return DesignView("acme", "0" * 32, "authority", "booking", snapshot, None, None, "current", False, "current",
                      "worktree", (ref,))


class JudgementCase(SourceCase):
    def setUp(self) -> None:
        super().setUp()
        self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})
        self.tag("v1.0.0")
        self.github.add("v1.0.0", body="Fixes a crash when the calendar is empty.")
        self.store = self.sources()
        self.spec = self.register(self.store)
        self.first = self.store.check_one(self.spec).snapshot
        self.provider = FakeJudge()

    def run_judge(self, request: judge.Request, cfg: dict | None = None, **options) -> judge.Judgement:
        return judge.judge(request, config=cfg or config(), policy=POLICY, env={"FAKE_JUDGE_KEY": "k"},
                           providers={"fake": self.provider}, **options)

    def notes(self, snapshot: str | None = None) -> judge.PublicSnapshotField:
        return judge.public_field(self.store, snapshot or self.first, "release-notes", 0, 20)


class ProvenanceTest(JudgementCase):
    """Per-purpose public provenance, not caller labels."""

    PRIVATE = ("Our clinic lets each nurse see only her own patients, and the board meets on Thursdays to approve "
               "new vendors.\n")

    def test_private_requirement_prose_without_a_secret_pattern_sends_nothing(self) -> None:
        private = judge.design_field(view({"request": self.PRIVATE}), "part:request", 0, 60)
        request = judge.Request("source-change-impact", "source-change-impact/v1",
                                (("release_note_excerpt", self.notes()), ("relevance", private)))
        result = self.run_judge(request)
        self.assertEqual("unused(design-text-off)", result.status)
        self.assertEqual([], self.provider.calls)
        for raw in ("Our clinic lets each nurse", {"text": "Our clinic"}):
            with self.subTest(raw=type(raw).__name__):
                forged = judge.Request("source-change-impact", "source-change-impact/v1", (("relevance", raw),))
                self.assertEqual("unused(scope)", self.run_judge(forged).status)
        self.assertEqual([], self.provider.calls)

    def test_a_verified_public_excerpt_is_sent_with_its_identity(self) -> None:
        excerpt = self.notes()
        request = judge.Request("source-change-impact", "source-change-impact/v1",
                                (("release_note_excerpt", excerpt), ("role", judge.label("role", "chosen")),
                                 ("version_delta", judge.label("version-delta", "patch"))))
        result = self.run_judge(request, sources=self.store)
        self.assertEqual(("used", "review"), (result.status, result.choice))
        state, instructions, options = self.provider.calls[0]
        self.assertEqual({"release_note_excerpt": "Fixes a crash when t", "role": "chosen", "version_delta": "patch"},
                         state["fields"])
        self.assertEqual(judge.RUBRICS["source-change-impact/v1"][1], instructions)
        self.assertEqual(["none", "review", "reselect"], list(options))
        field = result.identity["fields"][0]
        self.assertEqual((self.first, "release-notes", 0, 20), (field["snapshot_ref"], field["path_or_field"],
                                                                field["offset"], field["length"]))
        self.assertEqual(judge.CONSTRUCTOR_VERSION, result.identity["constructor"])

    def test_opted_in_authored_text_is_sent(self) -> None:
        design = view({"request": "Book a lesson in under a minute.\n"})
        authored = judge.design_field(design, "part:request", 0, 20)
        request = judge.Request("source-change-impact", "source-change-impact/v1",
                                (("release_note_excerpt", self.notes()), ("relevance", authored)))
        result = self.run_judge(request, config(design_text=True), sources=self.store, view=design)
        self.assertEqual("used", result.status)
        self.assertEqual("Book a lesson in und", self.provider.calls[0][0]["fields"]["relevance"])

    def test_gate_order_needs_judgement_enabled_independently(self) -> None:
        request = judge.Request("source-change-impact", "source-change-impact/v1",
                                (("release_note_excerpt", self.notes()),))
        self.assertEqual("unused(judgement-off)", self.run_judge(request, config(enabled=False)).status)
        self.assertEqual("unused(not-enabled)", self.run_judge(request, config(rerank_on=False)).status)
        no_key = judge.judge(request, config=config(), policy=POLICY, env={}, providers={"fake": self.provider})
        self.assertEqual("unused(no key)", no_key.status)
        self.assertEqual([], self.provider.calls)

    def test_every_outgoing_string_is_screened(self) -> None:
        self.clock.advance(60)
        self.commit({"src/lib.rs": "pub fn a() {}\n// contact maintainer@example.org\n"})
        self.tag("v1.0.1")
        self.github.add("v1.0.1", body="Contact maintainer@example.org for access.")
        second = self.store.check_one(self.spec).snapshot
        for field in (judge.public_field(self.store, second, "release-notes", 0, 40),
                      judge.public_field(self.store, second, "src/lib.rs", 14, 33)):
            with self.subTest(field=field.path_or_field):
                request = judge.Request("source-change-impact", "source-change-impact/v1",
                                        (("release_note_excerpt" if field.path_or_field == "release-notes"
                                          else "file_excerpt", field),))
                self.assertEqual("unused(secret)", self.run_judge(request).status)
        self.assertEqual([], self.provider.calls)

    def test_closed_allowlists_tags_and_required_inputs(self) -> None:
        notes = self.notes()
        cases = [judge.Request("source-change-impact", "source-change-impact/v1", (("relevance", notes),)),
                 judge.Request("source-change-impact", "source-change-impact/v1", (("unknown", notes),)),
                 judge.Request("source-change-impact", "material-fit/v1", (("release_note_excerpt", notes),)),
                 judge.Request("material-fit", "material-fit/v1", (("o0:role", judge.label("role", "chosen")),),
                               ("o0",)),
                 judge.Request("source-change-impact", "source-change-impact/v1",
                               (("role", judge.PublicLabel("role", "owner")),))]
        for request in cases:
            with self.subTest(request=request.fields[0][0]):
                self.assertEqual("unused(scope)", self.run_judge(request).status)
        stage = judge.Request("next-stage", "next-stage/v1", (("stage", judge.label("stage", "start")),))
        self.assertEqual("unused(insufficient-input)", self.run_judge(stage, config(design_text=True)).status)
        self.assertEqual([], self.provider.calls)

    def test_public_fields_come_only_from_verified_public_bytes(self) -> None:
        with self.assertRaises(judge.JudgeError):
            judge.public_field(self.store, "sha256:" + "0" * 64, "release-notes", 0, 10)
        with self.assertRaises(judge.JudgeError):
            judge.public_field(self.store, self.first, "src/missing.rs", 0, 10)
        with self.assertRaises(judge.JudgeError):
            judge.label("role", "owner")
        forged = judge.PublicSnapshotField(self.first, "release-notes", 0, 20, self.notes().bytes_digest,
                                           "Our clinic lets each")
        request = judge.Request("source-change-impact", "source-change-impact/v1",
                                (("release_note_excerpt", forged),))
        self.assertEqual("unused(scope)", self.run_judge(request, sources=self.store).status)
        self.assertEqual([], self.provider.calls)


class RecheckTest(JudgementCase):
    """A recorded result applies only while every identity still holds."""

    def test_changed_release_notes_never_reuse_an_old_assessment(self) -> None:
        old_request = judge.Request("source-change-impact", "source-change-impact/v1",
                                    (("release_note_excerpt", self.notes()),))
        old = self.run_judge(old_request, sources=self.store)
        stored = self.store.store_assessment(old.to_record())
        self.github.releases["v1.0.0"]["body"] = "Fixes a crash. SECURITY: upgrade now."
        self.clock.advance(3600)
        changed = self.store.check_one(self.spec)
        self.assertEqual("changed", changed.result)
        new_request = judge.Request("source-change-impact", "source-change-impact/v1",
                                    (("release_note_excerpt", self.notes(changed.snapshot)),))
        new_identity = judge.identity_of(new_request, policy=POLICY, provider="fake", model=old.identity[
            "requested_model"])
        self.assertNotEqual(judge.cache_key(old.identity), judge.cache_key(new_identity))
        self.assertNotEqual(old.identity["fields"][0]["snapshot_ref"], new_identity["fields"][0]["snapshot_ref"])
        record = self.store.assessment(stored)
        self.assertEqual([], judge.recheck(record, sources=self.store, policy=POLICY, provider="fake",
                                           model=old.identity["requested_model"]))
        self.assertIn("policy-changed", judge.recheck(record, sources=self.store, policy="sha256:" + "9" * 64,
                                                      provider="fake", model=old.identity["requested_model"]))
        self.assertIn("provider-changed", judge.recheck(record, sources=self.store, policy=POLICY,
                                                        provider="other", model=old.identity["requested_model"]))

    def test_design_text_results_are_stale_after_a_new_revision(self) -> None:
        design = view({"request": "Book a lesson in under a minute.\n"})
        authored = judge.design_field(design, "part:request", 0, 20)
        request = judge.Request("material-fit", "material-fit/v1",
                                (("o0:need", authored), ("o1:need", authored)), ("o0", "o1"))
        self.provider.answer = ("o1", None)
        result = self.run_judge(request, config(design_text=True), view=design)
        self.assertEqual(("used", "o1"), (result.status, result.choice))
        model = result.identity["requested_model"]
        self.assertEqual([], judge.recheck(result, sources=self.store, view=design, policy=POLICY, provider="fake",
                                           model=model))
        later = view({"request": "Book a lesson in under a minute.\n"}, revision=3)
        self.assertTrue(any(reason.startswith("design-changed") for reason in
                            judge.recheck(result, sources=self.store, view=later, policy=POLICY, provider="fake",
                                          model=model)))


if __name__ == "__main__":
    unittest.main()
