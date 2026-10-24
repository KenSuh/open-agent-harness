# 작업 지시서 검사 — 고정 배치·발주자 리스 사용·꼬리말 표시 줄 규칙·중복·누락을 본다.
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest
from collections import namedtuple
from unittest import mock

from oah.briefs import (VERIFY_FOOTER, VERIFY_MARKER, BriefError, build, deliver, has_intent_footer, intent_footer,
                        lease_command, raw_section, reusable_content, strip_intent_footer)
from oah import briefs
from oah.briefs import shell_word
from tests.helpers import make_tmp_dir

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
    def test_reusable_plain_and_generated_specs_warn_about_lease_commands(self):
        for stage in ("initial", "correction", "review"):
            for executable in ("oah", "oah.exe", "OAH.EXE", "OaH.ExE"):
                for command in ("start", "renew", "finish"):
                    instruction = f"Run `{executable} {command} --task p3b` before returning.\n"
                    for stored in (instruction + SECTION, brief(round_instructions=instruction)):
                        with self.subTest(stage=stage, executable=executable, command=command,
                                          generated=stored.startswith("You are")):
                            content = reusable_content(stored, stage=stage, footer=FOOTER, has_footer=has_footer)
                            self.assertIn(instruction.strip(), content)
                            warnings = briefs.reusable_warnings(content)
                            self.assertEqual(len(warnings), 1)
                            self.assertTrue(warnings[0].startswith("OAH_WORKER_LEASE_COMMAND"))
                            self.assertIn("remove", warnings[0])

    def test_unsafe_command_values_are_refused_before_building(self):
        words = ["a'b", 'a"b', "x’;Write-Output-INJECTED", "a‘b", "a“b", "a”b", "$x", "`x`",
                 "%PATH%", "!x!", "a b", "a\nb", "한글", "café", r"a\b", "C:/packet.md", "@args"]
        for word in words:
            with self.subTest(word=word):
                for operation in (lambda: shell_word(word), lambda: lease_command("t", "codex", word),
                                  lambda: brief(packet_source=word),
                                  lambda: brief(packet_source=None, binding=None, scope=[word]),
                                  lambda: brief(task=word), lambda: brief(engine=word)):
                    with self.assertRaises(BriefError) as ctx:
                        operation()
                    self.assertEqual(ctx.exception.code, "OAH_UNSAFE_ARGUMENT")
                    self.assertIn("rename", str(ctx.exception).lower())

    def _native_round_trip(self, shell, command):
        folder = make_tmp_dir(self)
        record = folder / "record.json"
        word = "packets/safe_Name-1.2.md"
        env = dict(os.environ, OAH_TEST_PYTHON=sys.executable,
                   OAH_TEST_SCRIPT=str(pathlib.Path(__file__).parent / "fakes" / "fake_codex.py"),
                   FAKE_RECORD=str(record))
        result = subprocess.run([*shell, command + shell_word(word)], env=env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["argv"], ["exec", word])
        self.assertEqual(result.returncode, 0)

    @unittest.skipIf(os.name == "nt", "POSIX native argv")
    def test_safe_value_round_trips_through_posix_native_process(self):
        self._native_round_trip(["/bin/sh", "-c"], '"$OAH_TEST_PYTHON" "$OAH_TEST_SCRIPT" exec ')

    @unittest.skipUnless(os.name == "nt", "native PowerShell argv")
    def test_safe_value_round_trips_through_powershell_native_process(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(shell, "Install PowerShell to check native Windows arguments")
        self._native_round_trip([shell, "-NoProfile", "-NonInteractive", "-Command"],
                                "& $env:OAH_TEST_PYTHON $env:OAH_TEST_SCRIPT exec ")

    def test_workers_use_the_dispatchers_lease_in_every_stage(self):
        command = lease_command("p3b", "claude", ".oah/tasks/p3b/PACKET.md")
        self.assertEqual(command, "oah start --task p3b --agent claude --packet .oah/tasks/p3b/PACKET.md")
        for stage in ("initial", "correction", "review"):
            text = brief(stage)
            self.assertNotIn("oah start", text)
            self.assertNotIn("oah renew", text)
            self.assertNotIn("oah finish", text)
            self.assertIn("lease belongs to the dispatcher", text)
            self.assertIn("never start, renew or finish a lease", text)
            self.assertIn("INTENT p3b@r1 " + BINDING.digest, text)
            if stage != "review":
                self.assertIn("`Agent-Engine: claude`", text)
        self.assertIn("`Agent-Engine: codex`", brief("correction", engine="codex"))
        review = brief("review", engine="codex")
        self.assertNotIn("Agent-Engine", review)
        self.assertNotIn("oah start", review)
        self.assertNotIn("--packet", review)

    def test_without_packet_worker_still_uses_dispatchers_scope(self):
        text = brief(intent_section=None, binding=None, packet_source=None)
        self.assertNotIn("oah start", text)
        self.assertIn("lease belongs to the dispatcher", text)
        self.assertIn("Scope: src/oah/call.py, tests/test_call.py", text)


class FooterTest(unittest.TestCase):
    def test_stored_intent_bytes_survive_footer_delivery(self):
        section = SECTION.replace("\n", "\r\n")
        for stored in (section, "Spec\r\n" + section + FOOTER.replace("\n", "\r\n")):
            with self.subTest(stored=stored):
                delivered = deliver(stored, footer=FOOTER, has_footer=has_footer)
                self.assertEqual(raw_section(delivered).encode("utf-8"), section.encode("utf-8"))

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

    def test_raw_section_is_byte_intact(self):
        text = ("# Packet\r\n" + SECTION.replace("works", "한글 \U0001f600 그대로") + "## Scope\n").replace(
            "Revision: 1\n", "Revision: 1\r\n")
        raw = raw_section(text)
        self.assertTrue(raw.startswith("<!-- OAH_INTENT:BEGIN -->\n"))
        self.assertIn("Revision: 1\r\n", raw)
        self.assertIn("한글 \U0001f600 그대로", raw)
        self.assertEqual(text.count(raw), 1)
        built = brief(intent_section=raw)
        self.assertEqual(built.encode("utf-8").count(raw.encode("utf-8")), 1)
        self.assertIsNone(raw_section("no markers"))
        self.assertIsNone(raw_section(SECTION + SECTION))

    @unittest.skipIf(importlib.util.find_spec("oah.intent"), "oah.intent exists")
    def test_intent_footer_without_intent_module_refuses(self):
        with self.assertRaises(BriefError) as ctx:
            intent_footer()
        self.assertEqual(ctx.exception.code, "OAH_FOOTER_NOT_APPENDED")

    @unittest.skipUnless(importlib.util.find_spec("oah.intent"), "lane P4a (oah.intent) has not landed")
    def test_intent_footer_is_the_shared_footer(self):
        from oah import intent

        self.assertEqual(intent_footer(), intent.FOOTER)
        self.assertTrue(has_intent_footer(brief(footer=intent_footer(), has_footer=has_intent_footer)))

    @unittest.skipUnless(importlib.util.find_spec("oah.intent"), "lane P4a (oah.intent) has not landed")
    def test_real_intent_footer_when_available(self):
        from oah import intent

        text = brief(footer=intent.FOOTER, has_footer=intent.has_footer)
        self.assertTrue(intent.has_footer(text))
        self.assertTrue(text.endswith(intent.FOOTER))


if __name__ == "__main__":
    unittest.main()
