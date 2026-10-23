# 작업 지시서 검사 — 고정 배치(작업자 줄·서문·의도 구획·라운드 지시·검증 꼬리말·의도 꼬리말), 구현·수정에만 리스 명령, 꼬리말 표시 줄 규칙·중복·누락을 본다.
from __future__ import annotations

import importlib.util
import unittest
from collections import namedtuple

from oah.briefs import (VERIFY_FOOTER, VERIFY_MARKER, BriefError, build, deliver, lease_command,
                        strip_intent_footer)

MARKER = "<!-- OAH_INTENT_FOOTER -->"
FOOTER = f"{MARKER}\n## Intent rules\n- Keep the intent section exactly as it is.\n- End with INTENT and AC lines.\n"
Binding = namedtuple("Binding", "id revision digest")
BINDING = Binding("p3b", 1, "sha256:" + "ab" * 32)
SECTION = ("<!-- OAH_INTENT:BEGIN -->\n## Intent\nIntent-ID: p3b\nRevision: 1\nStatus: accepted\n### Problem\n"
           "Mentions <!-- OAH_INTENT_FOOTER --> inline only.\n### Success conditions\n- AC1: works\n"
           "<!-- OAH_INTENT:END -->\n")


def has_footer(text: str) -> bool:
    return any(line == MARKER for line in text.replace("\r\n", "\n").split("\n"))


def brief(stage: str = "initial", **overrides) -> str:
    args = dict(run_id="r20261023T120000Z-0123abcd", task="p3b", stage=stage, engine="claude",
                scope=["src/oah/call.py", "tests/test_call.py"], intent_section=SECTION, binding=BINDING,
                packet_source=".oah/tasks/p3b/PACKET.md", round_instructions="Implement AC1.\n",
                skills=[("tdd", "implementation stage")], footer=FOOTER, has_footer=has_footer)
    args.update(overrides)
    return build(**args)


class LayoutTest(unittest.TestCase):
    def test_worker_line_first(self):
        first = brief().split("\n", 1)[0]
        self.assertTrue(first.startswith("You are a dispatched oah worker (OAH_WORKER=1"))
        self.assertIn("r20261023T120000Z-0123abcd", first)
        self.assertIn("Skip any session-entry procedure", first)

    def test_layout_order(self):
        text = brief()
        positions = [text.index(part) for part in (
            "You are a dispatched oah worker", "## Brief", SECTION, "## Round instructions\nImplement AC1.",
            VERIFY_MARKER, MARKER + "\n## Intent rules")]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(text.endswith(FOOTER))
        self.assertEqual(text.count(VERIFY_MARKER), 1)
        self.assertIn(VERIFY_FOOTER, text)

    def test_preamble_names_task_stage_scope_rules_packet_and_return(self):
        text = brief()
        for part in ("Task: p3b", "Stage: initial", "Engine: claude", "src/oah/call.py, tests/test_call.py",
                     "never push", "ledger", ".oah/tasks/p3b/PACKET.md", "tdd (implementation stage)",
                     "INTENT p3b@r1 sha256:"):
            self.assertIn(part, text)

    def test_intent_section_verbatim(self):
        self.assertEqual(brief().count(SECTION), 1)
        no_packet = brief(intent_section=None, binding=None, packet_source=None)
        self.assertIn("No intent section", no_packet)

    def test_empty_scope_warns_in_brief(self):
        self.assertIn("OAH_SCOPE_EMPTY", brief(scope=[]))


class LeaseCommandTest(unittest.TestCase):
    def test_lease_command_only_for_implementation_and_correction(self):
        command = lease_command("p3b", "claude", ".oah/tasks/p3b/PACKET.md")
        self.assertEqual(command, "oah start --task p3b --agent claude --packet .oah/tasks/p3b/PACKET.md")
        for stage in ("initial", "correction"):
            text = brief(stage)
            self.assertIn(command, text)
            self.assertIn("binds INTENT p3b@r1 " + BINDING.digest, text)
            self.assertIn("`Agent-Engine: claude`", text)
        self.assertIn("`Agent-Engine: codex`", brief("correction", engine="codex"))
        review = brief("review", engine="codex")
        self.assertNotIn("Agent-Engine", review)
        self.assertNotIn("oah start", review)
        self.assertNotIn("--packet", review)

    def test_lease_command_quotes_spaces(self):
        self.assertEqual(lease_command("t", "codex", "my tasks/PACKET.md"),
                         'oah start --task t --agent codex --packet "my tasks/PACKET.md"')

    def test_without_packet_implementation_still_gets_a_scope_lease(self):
        text = brief(intent_section=None, binding=None, packet_source=None)
        self.assertIn("oah start --task p3b --agent claude --scope src/oah/call.py --scope tests/test_call.py", text)


class FooterTest(unittest.TestCase):
    def test_mention_of_marker_still_gets_footer(self):
        # The intent section mentions the marker inline; the footer is still appended exactly once.
        text = brief()
        self.assertEqual(sum(1 for line in text.split("\n") if line == MARKER), 1)

    def test_missing_footer_refuses(self):
        for footer in ("", "no marker here\n"):
            with self.subTest(footer=footer), self.assertRaises(BriefError) as ctx:
                brief(footer=footer)
            self.assertEqual(ctx.exception.code, "OAH_FOOTER_NOT_APPENDED")

    def test_stored_brief_reuse_gets_canonical_footer(self):
        stored = "Reused spec\nmentions " + MARKER + " in a sentence\n"
        delivered = deliver(stored, footer=FOOTER, has_footer=has_footer)
        self.assertTrue(delivered.startswith(stored.rstrip("\n")))
        self.assertTrue(delivered.endswith(FOOTER))
        self.assertEqual(deliver(delivered, footer=FOOTER, has_footer=has_footer), delivered)
        crlf = delivered.replace("\n", "\r\n")
        self.assertTrue(deliver(crlf, footer=FOOTER, has_footer=has_footer).endswith(FOOTER))

    def test_stored_brief_with_two_footers_refuses(self):
        stored = "Spec\n\n" + FOOTER + "\n" + FOOTER
        with self.assertRaises(BriefError) as ctx:
            deliver(stored, footer=FOOTER, has_footer=has_footer)
        self.assertEqual(ctx.exception.code, "OAH_FOOTER_NOT_APPENDED")

    def test_stored_brief_with_outdated_footer_refuses(self):
        stored = "Spec\n\n" + MARKER + "\n## Old intent rules\n"
        with self.assertRaises(BriefError) as ctx:
            deliver(stored, footer=FOOTER, has_footer=has_footer)
        self.assertEqual(ctx.exception.code, "OAH_FOOTER_NOT_APPENDED")
        self.assertIn("rebuild", str(ctx.exception))

    def test_strip_footer_baseline(self):
        text = brief()
        baseline = strip_intent_footer(text, footer=FOOTER, has_footer=has_footer)
        self.assertNotIn(MARKER + "\n", baseline)
        self.assertTrue(baseline.endswith(VERIFY_FOOTER.rstrip("\n")))
        with self.assertRaises(BriefError) as ctx:
            strip_intent_footer(baseline, footer=FOOTER, has_footer=has_footer)
        self.assertEqual(ctx.exception.code, "OAH_FOOTER_MISSING")
        with self.assertRaises(BriefError) as ctx:
            strip_intent_footer(text + "\n" + FOOTER, footer=FOOTER, has_footer=has_footer)
        self.assertEqual(ctx.exception.code, "OAH_FOOTER_DUPLICATED")
        with self.assertRaises(BriefError) as ctx:
            strip_intent_footer(text + "trailing round text\n", footer=FOOTER, has_footer=has_footer)
        self.assertEqual(ctx.exception.code, "OAH_FOOTER_MISPLACED")

    @unittest.skipUnless(importlib.util.find_spec("oah.intent"), "lane P4a (oah.intent) has not landed")
    def test_real_intent_footer_when_available(self):
        from oah import intent

        text = brief(footer=intent.FOOTER, has_footer=intent.has_footer)
        self.assertTrue(intent.has_footer(text))
        self.assertTrue(text.endswith(intent.FOOTER))


if __name__ == "__main__":
    unittest.main()
