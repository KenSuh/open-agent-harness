# 의도 모듈 검사 — 마커·필드 파싱, CRLF/LF 다이제스트, 안전한 원본 읽기, 네 가지 결속 상태, 반환 줄 검사, 공용 꼬리말을 본다.
from __future__ import annotations

import hashlib
import os
import pathlib
import unittest
from unittest.mock import patch

from oah import intent
from oah.intent import Binding, IntentInvalid, SourceError
from tests.helpers import make_tmp_dir

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


if __name__ == "__main__":
    unittest.main()
