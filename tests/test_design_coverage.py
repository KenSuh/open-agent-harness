# 설계 coverage 검사 — 실제 저장소와 oah design 명령으로 아홉 단계 상태·조건부 생략·재확인 그래프·완료 판정·손편집 거부·한국어 출력을 본다.
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

from oah.cli import main
from tests.helpers import make_tmp_dir
from tests.test_design_store import PAGE, SRC, DesignRepo

STAGE_IDS = ("purpose", "behavior", "data", "access", "server", "payment", "quality", "materials", "confirm")
# The required checks of DESIGN §3.2, written out here independently of oah.design.stages.
CHECKS = {
    "purpose": ("users", "outcome", "first-scope", "terms", "open-decisions"),
    "behavior": ("normal-flow", "failure-flow", "cancel-flow", "resume-flow", "user-result"),
    "data": ("entities", "ownership", "lifetime", "integrity"),
    "access": ("subjects", "rules", "cross-owner-counterexample", "enforcement-point"),
    "server": ("responsibilities", "api-contract", "secrets", "external-effects"),
    "payment": ("state-transitions", "failure-refund-cancel", "duplicate-out-of-order-missing", "server-confirmation",
                "reconciliation-owner"),
    "quality": ("platforms", "cost-performance", "observability", "deploy-rollback", "recovery-scenario"),
    "materials": ("stack-options", "sources-licenses", "adoption", "unverified", "first-scope-done",
                  "module-boundaries"),
    "confirm": ("intent-alignment", "first-implementation-scope", "completion-criteria", "packet-ready"),
}
HEADING = re.compile(r"### \[decision:([a-z-]+\.[a-z-]+)\]")
CONFIRMATION = "sha256:" + "c" * 64


def region_digest(text: str, key: str) -> str:
    """The documented region rule, implemented here on its own: the exact bytes after the unique heading line, up to
    the next decision heading line or the end of the part."""
    heading = f"### [decision:{key}]\n"
    assert text.startswith(heading) + text.count("\n" + heading) == 1, key
    start = (0 if text.startswith(heading) else text.index("\n" + heading) + 1) + len(heading)
    following = text.find("\n### [decision:", start - 1)
    end = len(text) if following == -1 else following + 1
    return "sha256:" + hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()


def design_text(answers: dict[str, str], title: str = "Photo renamer") -> str:
    out = [f"# {title}", "", "Plain prose that is not an answer.", ""]
    for key, body in answers.items():
        out += [f"### [decision:{key}]", body, ""]
    return "\n".join(out).rstrip("\n") + "\n"


class CoverageRepo(DesignRepo):
    def setUp(self):
        self.repo = self.make_repo()
        self.enterContext(mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(self.state(self.repo))}))
        self.files = make_tmp_dir(self)

    def oah(self, *args: str, cwd: pathlib.Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), contextlib.chdir(cwd or self.repo):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def file(self, name: str, text: str | bytes) -> str:
        path = self.files / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return str(path)

    def new_design(self, answers: dict[str, str], *, parts: dict[str, str] | None = None) -> tuple[int, str]:
        self.assertEqual(self.oah("design", "project", "add", "acme", "--title", "Acme")[0], 0)
        extra = []
        for name, text in (parts or {}).items():
            extra += ["--part", f"{name}={self.file(name + '.md', text)}"]
        code, out, err = self.oah("design", "new", "acme", "booking", "--title", "Booking",
                                  "--file", self.file("design.md", design_text(answers)), *extra)
        self.assertEqual(code, 0, err)
        return 1, out.split()[3]

    def current(self) -> tuple[int, str, str]:
        data = json.loads(self.oah("design", "context", "acme", "booking")[1])
        current = data["current"]
        return current["revision"], current["digest"], current["parts"]["design"]["text"]

    def readout(self, *extra: str) -> dict:
        code, out, err = self.oah("design", "coverage", "acme", "booking", "--json", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def stages(self, *extra: str) -> dict[str, dict]:
        return {item["id"]: item for item in self.readout(*extra)["stages"]}

    def answer_ops(self, stage: str, checks, text: str, *, status: str = "answered") -> list[dict]:
        return [{"op": "set-check", "stage": stage, "check": check, "status": status, "impact": "outcome",
                 "note": f"{check} answered", "decision": None, "reason": None, "resolve_by": None,
                 "answer_refs": [{"part": "design", "region": f"{stage}.{check}",
                                  "sha256": region_digest(text, f"{stage}.{check}")}]} for check in checks]

    def edit(self, ops: list[dict], *, lang: str | None = None, expect_code: int = 0) -> tuple[str, str, str]:
        revision, digest, _ = self.current()
        envelope = {"version": 1, "expected": {"project": "acme", "design": "booking", "revision": revision,
                                               "digest": digest}, "ops": ops}
        out_file = self.files / f"coverage-r{revision}-{len(list(self.files.iterdir()))}.md"
        args = ["design", "coverage", "edit", "acme", "booking", "--from", self.file("edits.json", json.dumps(envelope)),
                "--out", str(out_file)]
        if lang:
            args += ["--lang", lang]
        code, out, err = self.oah(*args)
        self.assertEqual(code, expect_code, err)
        return out, err, str(out_file)

    def save_printed(self, out: str) -> str:
        """Run the exact save command `coverage edit` printed."""
        line = next(line for line in out.splitlines() if line.startswith("oah design save "))
        argv = self.split(line)[1:]
        code, saved, err = self.oah(*argv)
        self.assertEqual(code, 0, err)
        return saved

    @staticmethod
    def split(line: str) -> list[str]:
        import shlex

        return shlex.split(line, posix=os.name != "nt")


FREE_TOOL = {f"{stage}.{check}": f"The {check} of the photo renamer is settled."
             for stage in ("purpose", "behavior", "data", "quality", "materials", "confirm")
             for check in CHECKS[stage]}


class FreeLocalToolTest(CoverageRepo, unittest.TestCase):
    """E-T01: a free local tool goes from nine unreviewed stages to complete, with payment and server not applicable."""

    def test_free_local_tool_reaches_complete_only_after_every_confirmation(self):
        self.new_design(FREE_TOOL)
        first = self.readout()
        self.assertEqual([item["id"] for item in first["stages"]], list(STAGE_IDS))
        self.assertEqual({item["status"] for item in first["stages"]}, {"unreviewed"})
        self.assertEqual((first["complete"], first["frontier"]), (False, ["purpose"]))

        _, _, text = self.current()
        ops = [{"op": "set-applicability", "stage": stage, "value": "not-applicable",
                "reason": "a single-user local tool with no server or payment"} for stage in ("access", "server", "payment")]
        ops += [{"op": "set-tech", "label": "ui", "value": "cli", "decision": None, "note": None},
                {"op": "set-tech", "label": "database", "value": "none", "decision": None, "note": None},
                {"op": "set-tech", "label": "hosting", "value": "local", "decision": None, "note": None}]
        for stage in ("purpose", "behavior", "data", "quality", "materials", "confirm"):
            checks = [check for check in CHECKS[stage] if check != "module-boundaries"]
            ops += self.answer_ops(stage, checks, text)
        ops.append({"op": "set-check", "stage": "materials", "check": "module-boundaries", "status": "not-applicable",
                    "impact": None, "note": None, "decision": None, "reason": "one small script",
                    "resolve_by": None, "answer_refs": []})
        out, err, part = self.edit(ops)
        self.assertIn("--part coverage=", out)
        self.save_printed(out)

        stages = self.stages()
        for stage in ("access", "server", "payment"):
            self.assertEqual(stages[stage]["status"], "not-applicable")
            self.assertEqual(stages[stage]["reason"], "a single-user local tool with no server or payment")
        self.assertEqual(stages["purpose"]["status"], "in-progress")
        seeds = {link["seed"]: link for stage in stages.values() for link in stage["skills"] if link.get("seed")}
        self.assertEqual(seeds["S1"]["status"], "omitted")
        self.assertIn("database", seeds["S1"]["reason"])
        self.assertEqual(seeds["P1"]["status"], "omitted")
        self.assertIn("payment", seeds["P1"]["reason"])
        self.assertEqual(seeds["M5"]["status"], "omitted")  # user-only skills are never automatic
        code, _, err = self.oah("design", "coverage", "acme", "booking", "--check")
        self.assertEqual(code, 1)
        self.assertIn("OAH_COVERAGE_INCOMPLETE", err)

        for stage in ("purpose", "behavior", "data", "quality", "materials", "confirm"):
            out, _, _ = self.edit([{"op": "confirm-stage", "stage": stage,
                                    "user_confirmation": {"source_ref": CONFIRMATION, "confirmed": True}}])
            self.save_printed(out)
            if stage != "confirm":
                self.assertFalse(self.readout()["complete"])
        final = self.readout()
        self.assertEqual((final["complete"], final["blocking"], final["frontier"]), (True, [], []))
        code, out, err = self.oah("design", "coverage", "acme", "booking", "--check")
        self.assertEqual((code, err), (0, ""))
        page = (self.repo / PAGE).read_text(encoding="utf-8")
        self.assertIn("# Coverage", page)
        self.assertIn("| Payment (optional) | not applicable |", page)

    def test_the_printed_save_is_a_safe_retry(self):
        self.new_design(FREE_TOOL)
        out, _, _ = self.edit([{"op": "set-tech", "label": "ui", "value": "cli", "decision": None, "note": None}])
        self.save_printed(out)
        log = (self.repo / "docs/wiki/log.md").read_text(encoding="utf-8")
        listing = (self.repo / "docs/wiki/index.md").read_text(encoding="utf-8")
        self.save_printed(out)  # the same command again: the event makes it a retry, not a second revision
        self.assertEqual(self.current()[0], 2)
        self.assertEqual((self.repo / "docs/wiki/log.md").read_text(encoding="utf-8"), log)
        self.assertEqual((self.repo / "docs/wiki/index.md").read_text(encoding="utf-8"), listing)

    def test_edit_is_pure_and_refuses_a_stale_expected_revision(self):
        self.new_design(FREE_TOOL)
        before = self.tree(self.repo, self.state(self.repo))
        out, _, part = self.edit([{"op": "set-tech", "label": "ui", "value": "cli", "decision": None, "note": None}])
        after = self.tree(self.repo, self.state(self.repo))
        self.assertEqual(before, after, "coverage edit writes no design, page or state file")
        self.assertTrue(pathlib.Path(part).read_text(encoding="utf-8").rstrip().endswith("```"))
        self.save_printed(out)
        envelope = {"version": 1, "expected": {"project": "acme", "design": "booking", "revision": 1,
                                               "digest": "sha256:" + "0" * 64}, "ops": []}
        code, _, err = self.oah("design", "coverage", "edit", "acme", "booking", "--from",
                                self.file("stale.json", json.dumps(envelope)))
        self.assertEqual(code, 1)
        self.assertIn("OAH_DESIGN_CONTEXT_STALE", err)
        code, _, err = self.oah("design", "coverage", "edit", "acme", "booking", "--from",
                                self.file("again.json", json.dumps({"version": 1, "expected": {}, "ops": []})),
                                "--out", part)
        self.assertEqual(code, 1)
        self.assertIn("OAH_COVERAGE_INVALID", err)


PAID = {f"{stage}.{check}": f"{stage} {check} for the paid booking app."
        for stage in STAGE_IDS for check in (*CHECKS[stage], *(("accessibility",) if stage == "quality" else ()))}


class GrillingRoundTest(CoverageRepo, unittest.TestCase):
    """E-T03: one answered grilling issue never completes the design."""

    def test_one_answered_issue_leaves_every_other_stage_blocking(self):
        self.new_design(PAID)
        _, _, text = self.current()
        out, _, _ = self.edit(self.answer_ops("data", ["entities"], text))
        self.save_printed(out)
        stages = self.stages()
        self.assertEqual(stages["data"]["status"], "in-progress")
        self.assertEqual(stages["purpose"]["status"], "unreviewed")
        code, _, err = self.oah("design", "coverage", "acme", "booking", "--check")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_COVERAGE_INCOMPLETE"))
        for stage in STAGE_IDS:
            self.assertIn(f"{stage}:", err)


def confirm_all_ops(stages) -> list[dict]:
    return [{"op": "confirm-stage", "stage": stage, "user_confirmation": {"source_ref": CONFIRMATION, "confirmed": True}}
            for stage in stages]


class RecheckTest(CoverageRepo, unittest.TestCase):
    """E-T04 and F01: a changed decision rechecks exactly the stages the one influence graph reaches."""

    def paid_and_confirmed(self, database: str = "postgresql") -> None:
        self.new_design(PAID)
        _, _, text = self.current()
        ops = [{"op": "set-applicability", "stage": stage, "value": "applies", "reason": None}
               for stage in ("access", "server", "payment")]
        ops += [{"op": "set-tech", "label": "database", "value": database, "decision": None, "note": None},
                {"op": "set-tech", "label": "payment-provider", "value": "stripe", "decision": None, "note": None},
                {"op": "set-tech", "label": "ui", "value": "web", "decision": None, "note": None}]
        for stage in STAGE_IDS:
            ops += self.answer_ops(stage, CHECKS[stage], text)
        ops += self.answer_ops("quality", ["accessibility"], text)  # required because ui is web
        out, _, _ = self.edit(ops)
        self.save_printed(out)
        for stage in STAGE_IDS:
            out, _, _ = self.edit(confirm_all_ops([stage]))
            self.save_printed(out)
        stages = self.stages()
        self.assertEqual({item["status"] for item in stages.values()}, {"confirmed"}, stages)

    def test_postgres_to_mysql_rechecks_the_graph_without_saved_recheck_entries(self):
        self.paid_and_confirmed()
        before = self.readout()
        self.assertIn("postgresql", json.dumps(before["wanted"]))
        out, _, part = self.edit([{"op": "set-tech", "label": "database", "value": "mysql", "decision": None,
                                   "note": None}])
        text = pathlib.Path(part).read_text(encoding="utf-8")
        record = json.loads(text.split("```oah-coverage\n", 1)[1].split("\n```", 1)[0])
        for stage in record["stages"]:
            stage["recheck"] = []  # an editor that drops every annotation must not hide the recheck
        stripped = text.split("```oah-coverage\n", 1)[0] + "```oah-coverage\n" + json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n```\n"
        line = next(line for line in out.splitlines() if line.startswith("oah design save "))
        argv = self.split(line)[1:]
        argv[argv.index(f"coverage={part}")] = "coverage=" + self.file("stripped.md", stripped)
        event = argv.index("--event")
        del argv[event:event + 2]
        code, _, err = self.oah(*argv)
        # The stripped human section still renders the same: annotations are not shown in the table.
        self.assertEqual(code, 0, err)
        stages = self.stages()
        rechecked = sorted(stage for stage, item in stages.items() if item["recheck"])
        self.assertEqual(rechecked, sorted(["data", "access", "server", "payment", "quality", "materials", "confirm"]))
        for stage in ("purpose", "behavior"):
            self.assertEqual((stages[stage]["status"], stages[stage]["recheck"]), ("confirmed", False))
        self.assertEqual(stages["data"]["status"], "in-progress")
        seeds = {link["seed"]: link for link in stages["data"]["skills"] if link.get("seed")}
        self.assertEqual(seeds["S1"]["status"], "omitted")
        self.assertIn("mysql", seeds["S1"]["reason"])
        wanted = json.dumps(self.readout()["wanted"])
        self.assertIn("mysql", wanted)
        self.assertNotIn("postgresql", wanted)
        self.assertFalse(self.readout()["complete"])

    def test_own_answer_edit_beyond_the_note_rechecks_and_unrelated_stages_stay(self):
        self.paid_and_confirmed()
        revision, digest, text = self.current()
        changed = text.replace("data ownership for the paid booking app.",
                               "data ownership for the paid booking app, now owned by each school.")
        code, _, err = self.oah("design", "save", "acme", "booking", "--expect-revision", str(revision),
                                "--expect-digest", digest, "--reason", "Ownership moved to schools",
                                "--file", self.file("changed.md", changed))
        self.assertEqual(code, 0, err)
        stages = self.stages()
        self.assertTrue(stages["data"]["recheck"], "an own-stage answer change with the same status rechecks")
        self.assertIn("answer-changed", " ".join(stages["data"]["recheck_reasons"]))
        for stage in ("access", "server", "payment", "quality", "materials", "confirm"):
            self.assertTrue(stages[stage]["recheck"], stage)  # transitive and cyclic, with no saved flag
        for stage in ("purpose", "behavior"):
            self.assertEqual((stages[stage]["status"], stages[stage]["recheck"]), ("confirmed", False), stage)
        out, _, _ = self.edit([{"op": "clear-recheck", "stage": "data"}], expect_code=1)
        self.assertEqual(out, "")
        out, _, _ = self.edit(confirm_all_ops(["data"]) + [{"op": "clear-recheck", "stage": "data"}])
        self.save_printed(out)
        stages = self.stages()
        self.assertEqual((stages["data"]["status"], stages["data"]["recheck"]), ("confirmed", False))
        self.assertTrue(stages["access"]["recheck"], "confirming data alone does not repair its dependents")

    def test_applicability_and_deferral_edits_recheck_their_stage(self):
        self.paid_and_confirmed()
        out, _, _ = self.edit([{"op": "defer-stage", "stage": "payment",
                                "deferral": {"reason": "pricing is not decided", "resolve_by": "before-release",
                                             "impact": "minor"}}])
        self.save_printed(out)
        stages = self.stages()
        self.assertEqual(stages["payment"]["status"], "deferred")
        for stage in ("data", "access", "server", "quality", "materials", "confirm"):
            self.assertTrue(stages[stage]["recheck"], stage)  # payment's row of the influence table
        for stage in ("purpose", "behavior"):
            self.assertFalse(stages[stage]["recheck"], stage)
        out, _, _ = self.edit([{"op": "set-applicability", "stage": "server", "value": "not-applicable",
                                "reason": "everything runs in the browser"}])
        self.save_printed(out)
        stages = self.stages()
        self.assertEqual(stages["server"]["status"], "not-applicable")
        self.assertTrue(stages["payment"]["recheck"])
        self.assertTrue(stages["quality"]["recheck"])


class InvalidInputTest(CoverageRepo, unittest.TestCase):
    """E-T05: hand edits, malformed JSON and missing reasons refuse; CRLF and Korean keep working."""

    def saved_coverage(self, lang: str | None = None) -> str:
        self.new_design(FREE_TOOL)
        out, _, part = self.edit([{"op": "set-applicability", "stage": "payment", "value": "not-applicable",
                                   "reason": "free"}], lang=lang)
        self.save_printed(out)
        return pathlib.Path(part).read_text(encoding="utf-8")

    def test_a_hand_edit_of_the_wiki_table_is_refused(self):
        self.saved_coverage()
        page = self.repo / PAGE
        text = page.read_text(encoding="utf-8")
        self.assertIn("| Payment (optional) | not applicable |", text)
        page.write_text(text.replace("| Payment (optional) | not applicable |", "| Payment (optional) | confirmed |"),
                        encoding="utf-8")
        revision, digest, _ = self.current()
        code, _, err = self.oah("design", "save", "acme", "booking", "--expect-revision", str(revision),
                                "--expect-digest", digest, "--reason", "Edited in the wiki", "--from-wiki")
        self.assertEqual(code, 0, err)
        code, out, err = self.oah("design", "coverage", "acme", "booking")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("OAH_COVERAGE_EDITED"), err)

    def test_malformed_records_and_edits_are_invalid(self):
        text = self.saved_coverage()
        head, block = text.split("```oah-coverage\n", 1)
        data = block.split("\n```", 1)[0]
        record = json.loads(data)
        bad_records = {
            "duplicate key": data.replace('{"catalog"', '{"lang":"en","catalog"', 1),
            "nan": data.replace('"version":1', '"version":NaN', 1),
            "unknown stage": json.dumps(dict(record, stages=[dict(record["stages"][0], id="pricing"),
                                                             *record["stages"][1:]]), sort_keys=True),
            "unknown label": json.dumps(dict(record, tech={"color": {"value": "blue", "decision": None,
                                                                     "note": None}}), sort_keys=True),
        }
        from oah.design import coverage

        for name, raw in bad_records.items():
            with self.subTest(record=name):
                with self.assertRaises(coverage.CoverageError) as caught:
                    coverage.parse(head + "```oah-coverage\n" + raw + "\n```\n")
                self.assertEqual(caught.exception.code, "OAH_COVERAGE_INVALID")
        bad_ops = {
            "missing reason": {"op": "set-applicability", "stage": "access", "value": "not-applicable", "reason": None},
            "not-applicable stage that may not be": {"op": "set-applicability", "stage": "data",
                                                     "value": "not-applicable", "reason": "no data"},
            "deferral without resolve_by": {"op": "defer-stage", "stage": "quality",
                                            "deferral": {"reason": "later", "resolve_by": None, "impact": "minor"}},
            "unknown check": {"op": "set-check", "stage": "data", "check": "colour", "status": "open", "impact": None,
                              "note": None, "decision": None, "reason": None, "resolve_by": None, "answer_refs": []},
            "unknown op": {"op": "delete-need", "stage": "data"},
            "extra key": {"op": "set-tech", "label": "ui", "value": "cli", "decision": None, "note": None, "x": 1},
            "unknown tech value": {"op": "set-tech", "label": "database", "value": "oracle-db", "decision": None,
                                   "note": None},
            "answer without a source": {"op": "set-check", "stage": "data", "check": "entities", "status": "answered",
                                        "impact": "outcome", "note": None, "decision": None, "reason": None,
                                        "resolve_by": None, "answer_refs": []},
            "missing region": {"op": "set-check", "stage": "data", "check": "entities", "status": "answered",
                               "impact": "outcome", "note": None, "decision": None, "reason": None, "resolve_by": None,
                               "answer_refs": [{"part": "design", "region": "data.colour", "sha256": CONFIRMATION}]},
            "answer from the coverage part": {"op": "set-check", "stage": "data", "check": "entities",
                                              "status": "answered", "impact": "outcome", "note": None,
                                              "decision": None, "reason": None, "resolve_by": None,
                                              "answer_refs": [{"part": "coverage", "region": "whole",
                                                               "sha256": CONFIRMATION}]},
            "confirm with open outcome checks": {"op": "confirm-stage", "stage": "purpose",
                                                 "user_confirmation": {"source_ref": CONFIRMATION, "confirmed": True}},
        }
        for name, op in bad_ops.items():
            with self.subTest(op=name):
                out, err, _ = self.edit([op], expect_code=1)
                self.assertTrue(err.startswith("OAH_COVERAGE_INVALID"), err)
                self.assertEqual(out, "")

    def test_crlf_keeps_the_digests_and_korean_renders(self):
        text = self.saved_coverage(lang="ko")
        self.assertIn("# 설계 범위 점검", text)
        self.assertIn("| 결제(선택) | 해당 없음 |", text)
        from oah.design import coverage

        record = coverage.parse(text)
        self.assertEqual(coverage.parse(text.replace("\n", "\r\n")), record)
        self.assertEqual(coverage.render(record), text)
        readout = self.readout()
        self.assertEqual(readout["stages"][5]["status"], "not-applicable")


class LegacyAndFailureTest(CoverageRepo, unittest.TestCase):
    """E-T06: no coverage part reads as nine unreviewed stages; unreadable materials force a recheck."""

    def test_a_design_without_coverage_reads_as_nine_unreviewed_stages(self):
        self.new_design({})
        readout = self.readout()
        self.assertEqual([(item["id"], item["status"]) for item in readout["stages"]],
                         [(stage, "unreviewed") for stage in STAGE_IDS])
        self.assertEqual(readout["frontier"], ["purpose"])
        self.assertFalse(readout["complete"])
        code, out, err = self.oah("design", "coverage", "acme", "booking")
        self.assertEqual(code, 0, err)
        self.assertIn("purpose", out)
        code, out, err = self.oah("design", "coverage", "acme", "booking", "--accepted")
        self.assertEqual(code, 1)
        self.assertIn("OAH_DESIGN_NOT_FOUND", err)

    def test_unparseable_materials_rechecks_materials_and_its_dependents(self):
        RecheckTest.paid_and_confirmed(self)
        revision, digest, _ = self.current()
        code, _, err = self.oah("design", "save", "acme", "booking", "--expect-revision", str(revision),
                                "--expect-digest", digest, "--reason", "Broken materials",
                                "--part", "materials=" + self.file("materials.md", "# Materials\n\n```oah-materials\n{\n```\n"))
        self.assertEqual(code, 0, err)
        stages = self.stages()
        self.assertTrue(stages["materials"]["recheck"])
        self.assertIn("unknown-materials", " ".join(stages["materials"]["recheck_reasons"]))
        self.assertTrue(stages["confirm"]["recheck"])
        self.assertFalse(stages["purpose"]["recheck"])

    def test_a_fresh_process_reads_the_accepted_revision(self):
        RecheckTest.paid_and_confirmed(self)
        revision, digest, _ = self.current()
        code, _, err = self.oah("design", "accept", "acme", "booking", "--revision", str(revision), "--digest", digest,
                                "--reason", "Owner approved")
        self.assertEqual(code, 0, err)
        env = dict(os.environ, PYTHONPATH=SRC, PYTHONIOENCODING="utf-8")
        done = subprocess.run([sys.executable, "-m", "oah", "design", "coverage", "acme", "booking", "--accepted",
                               "--json"], cwd=self.repo, env=env, capture_output=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        data = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual((data["revision"], data["digest"], data["source"]), (revision, digest, "accepted"))
        self.assertTrue(data["complete"])


class CatalogTest(unittest.TestCase):
    def test_catalog_matches_the_design_tables(self):
        from oah.design import stages

        self.assertEqual(tuple(stage.id for stage in stages.STAGES), STAGE_IDS)
        for stage in stages.STAGES:
            with self.subTest(stage=stage.id):
                self.assertEqual(stage.checks, CHECKS[stage.id])
                self.assertEqual(stage.may_be_na, stage.id in ("access", "server", "payment"))
        self.assertEqual(dict(stages.STAGES[6].conditional), {"accessibility": "tech:ui=web,mobile,desktop"})
        self.assertEqual(sorted(seed.id for seed in stages.SEED_SKILLS),
                         sorted(["M1", "M2", "M3", "M4", "M5", "M6", "M7", "O1", "O2", "S1", "S2", "W1", "W2", "W3",
                                 "W4", "W5", "W6", "P1"]))
        self.assertEqual(sorted(stages.TECH_LABELS), sorted(["ui", "database", "platform", "auth", "payment-provider",
                                                             "hosting", "durable-workflow", "runtime"]))

    def test_one_influence_graph_with_cycles(self):
        from oah.design import stages

        self.assertEqual(stages.reach_into("purpose"), ("purpose",))
        self.assertEqual(stages.reach_into("behavior"), ("behavior", "purpose"))
        self.assertIn("access", stages.reach_into("data"))  # access -> data and data -> access: a cycle
        self.assertIn("data", stages.reach_into("access"))
        self.assertEqual(set(stages.reach_into("confirm")), set(STAGE_IDS))
        self.assertEqual(set(stages.reach_into("payment")), set(STAGE_IDS) - {"confirm"})
        self.assertNotIn("data", stages.reach_into("behavior"))

    def test_conditions_are_three_valued_and_closed(self):
        from oah.design import stages

        record = {"tech": {"database": {"value": "mysql", "decision": None, "note": None}},
                  "stages": [{"id": stage, "applicability": "undecided" if stage == "payment" else "applies",
                              "checks": []} for stage in STAGE_IDS]}
        self.assertIs(stages.condition_holds("tech:database=postgresql", record), False)
        self.assertIs(stages.condition_holds("tech:database=mysql,postgresql", record), True)
        self.assertIsNone(stages.condition_holds("tech:ui=web", record))
        self.assertIsNone(stages.condition_holds("applies:payment AND tech:database=mysql", record))
        self.assertIs(stages.condition_holds("applies:payment AND tech:database=sqlite", record), False)
        self.assertIs(stages.condition_holds("user-only", record), False)
        for bad in ("tech:color=blue", "applies:pricing", "always OR user-only", "check:data.colour!=not-applicable",
                    "tech:database=oracle"):
            with self.subTest(condition=bad):
                with self.assertRaises(ValueError):
                    stages.parse_condition(bad)

    def test_region_rule_is_the_body_to_the_next_heading(self):
        from oah.design import coverage

        text = "# T\n\n### [decision:data.entities]\nPhotos.\n\n### [decision:data.ownership]\nMine.\n"
        self.assertEqual(coverage.resolve_ref({"design": text}, {"part": "design", "region": "data.entities",
                                                                 "sha256": CONFIRMATION}),
                         "sha256:" + hashlib.sha256(b"Photos.\n\n").hexdigest())
        self.assertEqual(coverage.resolve_ref({"design": text}, {"part": "design", "region": "data.ownership",
                                                                 "sha256": CONFIRMATION}),
                         "sha256:" + hashlib.sha256(b"Mine.\n").hexdigest())
        doubled = text + "### [decision:data.entities]\nAgain.\n"
        self.assertIsNone(coverage.resolve_ref({"design": doubled}, {"part": "design", "region": "data.entities",
                                                                     "sha256": CONFIRMATION}))


if __name__ == "__main__":
    unittest.main()
