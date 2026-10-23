# 의도 모듈 검사 — 마커·필드 파싱, CRLF/LF 다이제스트, 안전한 원본 읽기, 네 가지 결속 상태, 반환 줄 검사, 공용 꼬리말을 본다.
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import pathlib
import unittest
from unittest.mock import patch

from oah import cli, intent, intent_cli
from oah.intent import Binding, IntentInvalid, SourceError
from oah.lease import Lease, Store
from oah.paths import repo_info, state_dir
from tests.helpers import git_commit, make_git_repo, make_tmp_dir

HEADINGS = ("Problem", "Desired outcome", "Success conditions", "Constraints", "Non-goals", "Open questions")


def section(*, ident: str = "demo", revision: str = "1", status: str = "accepted",
            acs: tuple[str, ...] = ("- AC1: the report lists every file", "- AC2: the digest is stable"),
            headings: tuple[str, ...] = HEADINGS, begin: str = intent.BEGIN, end: str = intent.END,
            header_extra: tuple[str, ...] = ()) -> str:
    lines = [begin, "## Intent", f"Intent-ID: {ident}", f"Revision: {revision}", f"Status: {status}",
             *header_extra]
    for heading in headings:
        lines.append(f"### {heading}")
        if heading == "Success conditions":
            lines.extend(acs)
        elif heading == "Problem":
            lines.append("Agents forget the goal.")
    lines.append(end)
    return "\n".join(lines) + "\n"


def packet(body: str = "") -> str:
    return "Intro text.\n\n" + (body or section()) + "## Scope\n- src\n## Return\n"


def codes(problems) -> list[str]:
    return [problem.code for problem in problems]


class ParseTest(unittest.TestCase):
    def test_parse_well_formed_section(self):
        parsed = intent.parse(packet())
        self.assertEqual((parsed.id, parsed.revision, parsed.status), ("demo", 1, "accepted"))
        self.assertEqual(parsed.acs, ("AC1", "AC2"))
        self.assertEqual(parsed.conditions[0], ("AC1", "the report lists every file"))
        self.assertTrue(parsed.section.startswith(intent.BEGIN + "\n"))
        self.assertTrue(parsed.section.endswith(intent.END + "\n"))
        self.assertEqual(parsed.line(), f"INTENT demo@r1 {parsed.digest}")

    def test_status_comment_is_ignored(self):
        text = section(status='draft            # becomes "accepted" when you approve it')
        self.assertEqual(intent.parse(text).status, "draft")

    def test_digest_equal_for_crlf_and_lf(self):
        lf = packet()
        crlf = lf.replace("\n", "\r\n")
        self.assertNotEqual(lf, crlf)
        self.assertEqual(intent.parse(lf).digest, intent.parse(crlf).digest)
        self.assertEqual(intent.parse(lf).section, intent.parse(crlf).section)

    def test_digest_known_value(self):
        text = section()
        expected = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(intent.parse(text).digest, expected)
        self.assertEqual(intent.digest_of(text), expected)
        # Text outside the markers and extra trailing newlines do not change the digest.
        self.assertEqual(intent.parse("x\n" + text.rstrip("\n") + "\n\n\nmore\n").digest, expected)

    def test_digest_changes_with_section_text(self):
        self.assertNotEqual(intent.parse(section()).digest,
                            intent.parse(section(acs=("- AC1: something else",))).digest)

    def test_marker_table(self):
        good = section()
        cases = {
            "missing": ("no markers here\n", "OAH_INTENT_MISSING"),
            "only begin": (good.replace(intent.END, "end"), "OAH_INTENT_MARKERS_INVALID"),
            "only end": (good.replace(intent.BEGIN, "begin"), "OAH_INTENT_MARKERS_INVALID"),
            "duplicate begin": (intent.BEGIN + "\n" + good, "OAH_INTENT_MARKERS_INVALID"),
            "duplicate end": (good + intent.END + "\n", "OAH_INTENT_MARKERS_INVALID"),
            "duplicate section": (good + good, "OAH_INTENT_MARKERS_INVALID"),
            "end before begin": (intent.END + "\n" + good.replace(intent.END, "x"), "OAH_INTENT_MARKERS_INVALID"),
            "indented marker": (good.replace(intent.BEGIN, "  " + intent.BEGIN), "OAH_INTENT_MARKERS_INVALID"),
            "malformed marker": (good.replace(intent.BEGIN, "<!-- OAH_INTENT:BEGIN-->"),
                                 "OAH_INTENT_MARKERS_INVALID"),
            "trailing text": (good.replace(intent.END, intent.END + " x"), "OAH_INTENT_MARKERS_INVALID"),
        }
        for name, (text, code) in cases.items():
            with self.subTest(name), self.assertRaises(IntentInvalid) as caught:
                intent.parse(text)
            self.assertIn(code, codes(caught.exception.problems), name)
            self.assertTrue(str(caught.exception).startswith("OAH_INTENT_"), name)

    def test_prose_mention_of_marker_is_not_a_marker(self):
        text = "See the `<!-- OAH_INTENT:BEGIN -->` line below.\n" + section()
        self.assertEqual(intent.parse(text).id, "demo")

    def test_field_table(self):
        cases = {
            "bad id": section(ident="bad id"),
            "id with slash": section(ident="a/b"),
            "revision zero": section(revision="0"),
            "revision leading zero": section(revision="01"),
            "revision word": section(revision="two"),
            "status unknown": section(status="done"),
            "duplicate field": section(header_extra=("Status: accepted",)),
            "stray header text": section(header_extra=("free text",)),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(IntentInvalid) as caught:
                intent.parse(text)
            self.assertIn("OAH_INTENT_FIELD_INVALID", codes(caught.exception.problems), name)
        missing = section().replace("Revision: 1\n", "")
        with self.assertRaises(IntentInvalid) as caught:
            intent.parse(missing)
        self.assertIn("OAH_INTENT_FIELD_INVALID", codes(caught.exception.problems))

    def test_heading_table(self):
        cases = {
            "missing heading": HEADINGS[:-1],
            "duplicate heading": HEADINGS + ("Constraints",),
            "wrong order": (HEADINGS[1], HEADINGS[0], *HEADINGS[2:]),
            "unknown heading": HEADINGS + ("Notes",),
        }
        for name, headings in cases.items():
            with self.subTest(name), self.assertRaises(IntentInvalid) as caught:
                intent.parse(section(headings=headings))
            self.assertIn("OAH_INTENT_SECTION_INVALID", codes(caught.exception.problems), name)
        no_title = section().replace("## Intent\n", "")
        with self.assertRaises(IntentInvalid) as caught:
            intent.parse(no_title)
        self.assertIn("OAH_INTENT_SECTION_INVALID", codes(caught.exception.problems))

    def test_success_conditions_table(self):
        cases = {
            "none": ((), "OAH_INTENT_AC_INVALID"),
            "duplicate id": (("- AC1: a", "- AC1: b"), "OAH_INTENT_AC_DUPLICATE"),
            "empty text": (("- AC1:",), "OAH_INTENT_AC_INVALID"),
            "no id": (("- the build works",), "OAH_INTENT_AC_INVALID"),
            "zero id": (("- AC0: a",), "OAH_INTENT_AC_INVALID"),
            "padded id": (("- AC01: a",), "OAH_INTENT_AC_INVALID"),
        }
        for name, (acs, code) in cases.items():
            with self.subTest(name), self.assertRaises(IntentInvalid) as caught:
                intent.parse(section(acs=acs))
            self.assertIn(code, codes(caught.exception.problems), name)

    def test_continuation_lines_and_prose_are_allowed(self):
        parsed = intent.parse(section(acs=("<!-- outcomes only -->", "- AC1: one", "  - detail", "",
                                           "- AC3: three")))
        self.assertEqual(parsed.acs, ("AC1", "AC3"))

    def test_all_problems_are_reported_together(self):
        with self.assertRaises(IntentInvalid) as caught:
            intent.parse(section(ident="bad id", acs=("- AC1: a", "- AC1: b")))
        self.assertIn("OAH_INTENT_FIELD_INVALID", codes(caught.exception.problems))
        self.assertIn("OAH_INTENT_AC_DUPLICATE", codes(caught.exception.problems))


class ReadSourceTest(unittest.TestCase):
    def setUp(self):
        self.dir = make_tmp_dir(self)
        self.path = self.dir / "PACKET.md"
        self.path.write_text(packet(), encoding="utf-8")

    def assert_source_error(self, code: str, path: pathlib.Path | None = None):
        with self.assertRaises(SourceError) as caught:
            intent.read_source(path or self.path)
        self.assertEqual(caught.exception.code, code)
        self.assertTrue(str(caught.exception).startswith(code))

    def test_reads_utf8_text(self):
        self.assertEqual(intent.read_source(self.path), packet())

    def test_read_source_bom(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + packet().encode("utf-8"))
        self.assertEqual(intent.read_source(self.path), packet())

    def test_read_source_limits(self):
        self.assert_source_error("OAH_INTENT_SOURCE_MISSING", self.dir / "absent.md")
        self.assert_source_error("OAH_INTENT_SOURCE_INVALID", self.dir)
        self.path.write_bytes(b"a" * (intent.MAX_BYTES - 1))
        self.assertEqual(len(intent.read_source(self.path)), intent.MAX_BYTES - 1)
        self.path.write_bytes(b"a" * intent.MAX_BYTES)
        self.assert_source_error("OAH_INTENT_SOURCE_INVALID")
        self.path.write_bytes(b"\xff\xfe not utf-8")
        self.assert_source_error("OAH_INTENT_SOURCE_INVALID")

    def test_symlink_is_refused(self):
        link = self.dir / "link.md"
        try:
            link.symlink_to(self.path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        self.assert_source_error("OAH_INTENT_SOURCE_INVALID", link)

    def test_read_source_refuses_change_between_stat_and_read(self):
        real_open = os.open

        def replace_then_open(path, flags, *args):
            replacement = self.dir / "replacement.md"
            replacement.write_text(packet() + "changed\n", encoding="utf-8")
            os.replace(replacement, self.path)
            return real_open(path, flags, *args)

        with patch("oah.intent.os.open", side_effect=replace_then_open):
            self.assert_source_error("OAH_INTENT_SOURCE_INVALID")

        def grow_then_open(path, flags, *args):
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write("more\n")
            return real_open(path, flags, *args)

        self.path.write_text(packet(), encoding="utf-8")
        with patch("oah.intent.os.open", side_effect=grow_then_open):
            self.assert_source_error("OAH_INTENT_SOURCE_INVALID")


class BindingStateTest(unittest.TestCase):
    def setUp(self):
        self.root = make_tmp_dir(self)
        self.rel = ".oah/tasks/demo/PACKET.md"
        self.path = self.root / self.rel
        self.path.parent.mkdir(parents=True)
        self.path.write_text(packet(), encoding="utf-8")
        self.binding = intent.parse(packet()).bind(self.rel)

    def state(self, text: str | bytes | None = None, binding: Binding | None = None) -> str:
        if isinstance(text, bytes):
            self.path.write_bytes(text)
        elif text is not None:
            self.path.write_text(text, encoding="utf-8")
        return intent.binding_state(binding or self.binding, self.root)

    def test_binding_state_table(self):
        good = section()
        cases = {
            "accepted and matching": (good, "current"),
            "matching with CRLF": (good.replace("\n", "\r\n").encode("utf-8"), "current"),
            "draft": (section(status="draft"), "stale"),
            "changed id": (section(ident="other"), "stale"),
            "changed revision": (section(revision="2"), "stale"),
            "changed digest": (section(acs=("- AC1: the report lists every file", "- AC2: changed")), "stale"),
            "changed ac list": (section(acs=("- AC1: the report lists every file",)), "stale"),
            "missing markers": ("nothing\n", "stale"),
            "malformed marker": (good.replace(intent.END, "<!-- OAH_INTENT:END"), "stale"),
            "duplicate markers": (good + good, "stale"),
            "invalid field": (section(revision="x"), "stale"),
            "not utf-8": (b"\xff\xfe", "stale"),
            "too large": (b"a" * intent.MAX_BYTES, "stale"),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.state(text), expected)

    def test_unbound_and_missing(self):
        self.assertEqual(intent.binding_state(None, self.root), "unbound")
        self.path.unlink()
        self.assertEqual(intent.binding_state(self.binding, self.root), "missing")
        state, reason = intent.explain_binding(self.binding, self.root)
        self.assertEqual(state, "missing")
        self.assertIn(self.rel, reason)

    def test_dangling_link_is_stale_not_missing(self):
        self.path.unlink()
        try:
            self.path.symlink_to(self.root / "nowhere.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        self.assertEqual(intent.binding_state(self.binding, self.root), "stale")

    def test_symlink_to_valid_packet_is_stale(self):
        real = self.root / "real.md"
        real.write_text(packet(), encoding="utf-8")
        self.path.unlink()
        try:
            self.path.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        self.assertEqual(intent.binding_state(self.binding, self.root), "stale")

    def test_unreadable_source_is_stale(self):
        with patch("oah.intent.os.open", side_effect=PermissionError(13, "denied")):
            state, reason = intent.explain_binding(self.binding, self.root)
        self.assertEqual(state, "stale")
        self.assertIn("OAH_INTENT_SOURCE_UNREADABLE", reason)

    def test_absolute_source(self):
        binding = intent.parse(packet()).bind(str(self.path))
        self.assertEqual(intent.binding_state(binding, self.root / "elsewhere"), "current")

    def test_stale_reason_names_the_difference(self):
        self.path.write_text(section(revision="2"), encoding="utf-8")
        state, reason = intent.explain_binding(self.binding, self.root)
        self.assertEqual(state, "stale")
        self.assertIn("revision", reason)

    def test_binding_json_round_trip(self):
        data = self.binding.to_json()
        self.assertEqual(set(data), {"source", "id", "revision", "digest", "acs"})
        self.assertEqual(Binding.from_json(data), self.binding)
        for broken in ({}, dict(data, revision="1"), dict(data, acs=[]), dict(data, digest="abc"),
                       dict(data, acs=["AC1", "AC1"]), dict(data, id=""), "text"):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                Binding.from_json(broken)


class CheckReturnTest(unittest.TestCase):
    def setUp(self):
        self.intent = intent.parse(section())
        self.binding = self.intent.bind("PACKET.md")
        self.line = self.intent.line()

    def check(self, *lines: str) -> list[str]:
        problems = intent.check_return("\n".join(lines) + "\n", self.binding)
        self.assertTrue(all(problem.code == "OAH_INTENT_RETURN_INVALID" for problem in problems))
        return [problem.message for problem in problems]

    def test_check_return_table(self):
        other_digest = "sha256:" + "0" * 64
        ac1, ac2 = "AC1: met — tests pass", "AC2: deferred — waits for P4b"
        cases = {
            "complete": ((self.line, ac1, ac2), []),
            "complete with prose": (("Summary first.", self.line, "notes", ac1, ac2), []),
            "ascii dash": ((self.line, "AC1: unmet -- not yet", ac2), []),
            "missing intent": ((ac1, ac2), ["INTENT line is missing"]),
            "malformed intent": (("INTENT demo r1", ac1, ac2), ["malformed INTENT line", "INTENT line is missing"]),
            "duplicate intent": ((self.line, self.line, ac1, ac2), ["more than one INTENT line"]),
            "mismatched id": ((self.line.replace("demo@", "other@"), ac1, ac2), ["does not match"]),
            "mismatched revision": ((self.line.replace("@r1", "@r2"), ac1, ac2), ["does not match"]),
            "mismatched digest": ((f"INTENT demo@r1 {other_digest}", ac1, ac2), ["does not match"]),
            "missing ac": ((self.line, ac1), ["AC2 line is missing"]),
            "malformed ac": ((self.line, ac1, "AC2: met"), ["malformed AC line", "AC2 line is missing"]),
            "bad status": ((self.line, ac1, "AC2: done — yes"), ["malformed AC line", "AC2 line is missing"]),
            "empty evidence": ((self.line, ac1, "AC2: met — "), ["malformed AC line", "AC2 line is missing"]),
            "duplicate ac": ((self.line, ac1, ac2, "AC2: met — again"), ["more than one AC2 line"]),
            "invented ac": ((self.line, ac1, ac2, "AC3: met — extra"), ["AC3 is not a success condition"]),
        }
        for name, (lines, expected) in cases.items():
            with self.subTest(name):
                messages = self.check(*lines)
                self.assertEqual(len(messages), len(expected), messages)
                for fragment, message in zip(expected, messages):
                    self.assertIn(fragment, message)

    def test_crlf_return(self):
        text = "\r\n".join([self.line, "AC1: met — a", "AC2: met — b"]) + "\r\n"
        self.assertEqual(intent.check_return(text, self.binding), [])

    def test_bullets_and_backticks_do_not_count(self):
        messages = self.check(f"- {self.line}", "`AC1: met — a`", "AC2: met — b")
        self.assertEqual(len(messages), 2)
        self.assertIn("INTENT line is missing", messages[0])
        self.assertIn("AC1 line is missing", messages[1])


class FooterTest(unittest.TestCase):
    def test_footer_starts_with_marker(self):
        self.assertTrue(intent.FOOTER.startswith(intent.FOOTER_MARKER + "\n"))
        self.assertTrue(intent.FOOTER.endswith("\n"))
        for phrase in ("Revision", "Intent-ID", "INTENT <id>@r<revision> <digest>",
                       "AC<n>: met|unmet|deferred — <evidence>", "OAH_INTENT_RETURN_INVALID"):
            self.assertIn(phrase, intent.FOOTER)
        self.assertTrue(intent.has_footer(intent.FOOTER))

    def test_footer_does_not_disturb_the_intent_section(self):
        brief = section() + "\nRound 2 instructions.\n\n" + intent.FOOTER
        self.assertEqual(intent.parse(brief).digest, intent.parse(section()).digest)

    def test_has_footer_requires_marker_line(self):
        marker = intent.FOOTER_MARKER
        cases = {
            "marker line": (f"brief\n{marker}\nrules\n", True),
            "marker with CRLF": (f"brief\r\n{marker}\r\nrules\r\n", True),
            "first line": (f"{marker}\n", True),
            "mentioned inline": (f"the {marker} marker ends a brief\n", False),
            "indented": (f"  {marker}\n", False),
            "quoted": (f"> {marker}\n", False),
            "trailing text": (f"{marker} extra\n", False),
            "absent": ("brief\n", False),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                self.assertIs(intent.has_footer(text), expected)

    def test_with_footer_appends_once(self):
        once = intent.with_footer("brief")
        self.assertEqual(once, "brief\n\n" + intent.FOOTER)
        self.assertEqual(intent.with_footer(once), once)
        mentioned = f"the {intent.FOOTER_MARKER} marker\n"
        self.assertEqual(intent.with_footer(mentioned).count(intent.FOOTER_MARKER), 2)


def full_packet(*, ident: str = "demo", revision: str = "1", status: str = "accepted",
                acs: tuple[str, ...] = ("- AC1: the report lists every file", "- AC2: the digest is stable")) -> str:
    return section(ident=ident, revision=revision, status=status, acs=acs) + \
        "## Scope\n- src\n## Plan\n## Acceptance\n## Return\n"


def run_intent_cli(cwd: pathlib.Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    previous = pathlib.Path.cwd()
    os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class IntentCliTest(unittest.TestCase):
    rel = ".oah/tasks/demo/PACKET.md"

    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/app.py": "x = 1\n"})
        self.path = self.repo / self.rel
        self.path.parent.mkdir(parents=True)
        self.write(full_packet())

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_commands_and_registration(self):
        self.assertEqual(intent_cli.COMMANDS, ("intent",))

    def test_show_prints_identity_and_unbound(self):
        code, out, err = run_intent_cli(self.repo, "intent", "show", "demo")
        self.assertEqual(code, 0, err)
        parsed = intent.parse(full_packet())
        self.assertIn("state=unbound", out)
        self.assertIn("intent=demo revision=1 status=accepted", out)
        self.assertIn(f"digest={parsed.digest}", out)
        self.assertIn(f"source={self.rel}", out)
        self.assertIn("acs=AC1,AC2", out)
        self.assertIn(parsed.line(), out.splitlines())

    def test_show_invalid_packet_exits_1(self):
        self.write("no intent here\n")
        code, out, _ = run_intent_cli(self.repo, "intent", "show", "demo")
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_MISSING", out)
        code, out, _ = run_intent_cli(self.repo, "intent", "show", "absent")
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_SOURCE_MISSING", out)

    def test_check_accepts_valid_packet(self):
        code, out, err = run_intent_cli(self.repo, "intent", "check", "demo")
        self.assertEqual(code, 0, out + err)
        self.assertIn("OAH_INTENT_OK", out)

    def test_check_refuses_draft(self):
        self.write(full_packet(status="draft"))
        code, out, _ = run_intent_cli(self.repo, "intent", "check", "demo")
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_NOT_ACCEPTED", out)

    def test_check_reports_id_different_from_task(self):
        self.write(full_packet(ident="other"))
        code, out, _ = run_intent_cli(self.repo, "intent", "check", "demo")
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_ID_CHANGED", out)

    def test_check_reports_changed_id_against_head(self):
        git_commit(self.repo, {}, "add packet")
        self.write(full_packet(ident="other"))
        code, out, _ = run_intent_cli(self.repo, "intent", "check", "--file", str(self.path))
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_ID_CHANGED", out)
        self.assertIn("HEAD", out)

    def test_check_requires_new_revision_for_changed_goal(self):
        git_commit(self.repo, {}, "add packet")
        self.write(full_packet(acs=("- AC1: a different goal",)))
        code, out, _ = run_intent_cli(self.repo, "intent", "check", "demo")
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_REVISION_REQUIRED", out)
        self.assertIn("Revision: 2", out)

    def test_check_accepts_raised_revision(self):
        git_commit(self.repo, {}, "add packet")
        self.write(full_packet(revision="2", acs=("- AC1: a different goal",)))
        code, out, _ = run_intent_cli(self.repo, "intent", "check", "demo")
        self.assertEqual(code, 0, out)

    def test_check_refuses_lower_revision(self):
        git_commit(self.repo, {self.rel: full_packet(revision="3")}, "r3")
        self.write(full_packet(revision="2"))
        code, out, _ = run_intent_cli(self.repo, "intent", "check", "demo")
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_REVISION_INVALID", out)

    def test_editing_a_committed_draft_needs_no_new_revision(self):
        git_commit(self.repo, {self.rel: full_packet(status="draft")}, "draft")
        self.write(full_packet(acs=("- AC1: refined while still a draft",)))
        code, out, _ = run_intent_cli(self.repo, "intent", "check", "demo")
        self.assertEqual(code, 0, out)

    def bind(self, binding: Binding):
        info = repo_info(self.repo)
        Store(state_dir(info, os.environ)).start(task="demo", agent="claude", scope=("src",), note="",
                                                 ttl_hours=1, info=info)
        return patch.object(Lease, "intent", binding.to_json(), create=True)

    def test_show_uses_lease_binding_when_present(self):
        binding = intent.parse(full_packet()).bind(self.rel)
        with self.bind(binding):
            code, out, _ = run_intent_cli(self.repo, "intent", "show", "demo")
            self.assertEqual(code, 0, out)
            self.assertIn("state=current", out)
            self.write(full_packet(revision="2", acs=("- AC1: new goal",)))
            code, out, _ = run_intent_cli(self.repo, "intent", "check", "demo")
            self.assertEqual(code, 1)
            self.assertIn("state=stale", out)
            self.assertIn("OAH_INTENT_NOT_CURRENT", out)
            self.write(full_packet(ident="other"))
            code, out, _ = run_intent_cli(self.repo, "intent", "check", "--file", str(self.path))
            self.assertIn("OAH_INTENT_ID_CHANGED", out)
            self.assertIn("lease binding", out)

    def test_lease_without_binding_is_unbound(self):
        info = repo_info(self.repo)
        Store(state_dir(info, os.environ)).start(task="demo", agent="claude", scope=("src",), note="",
                                                 ttl_hours=1, info=info)
        code, out, _ = run_intent_cli(self.repo, "intent", "show", "demo")
        self.assertEqual(code, 0, out)
        self.assertIn("state=unbound", out)
        self.assertIn("oah start --packet", out)

    def test_bound_source_missing(self):
        binding = intent.parse(full_packet()).bind(".oah/tasks/demo/OLD.md")
        with self.bind(binding):
            code, out, _ = run_intent_cli(self.repo, "intent", "check", "demo")
        self.assertEqual(code, 1)
        self.assertIn("state=missing", out)


if __name__ == "__main__":
    unittest.main()
