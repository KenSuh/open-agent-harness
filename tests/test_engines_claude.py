# Claude 어댑터 검사 — --help 에서 별칭·effort 목록 읽기, effort 해석, 명령줄(리뷰는 읽기 전용), 결과 JSON 에서 관측 가능한 값만 기록하는지 본다.
from __future__ import annotations

import os
import pathlib
import sys
import unittest

from oah.engines import claude
from oah.engines.effort import EngineError
from oah.engines.proc import run_command

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_CLAUDE = [sys.executable, str(FAKES / "fake_claude.py")]


def fake(*args: str, **env: str):
    full = dict(os.environ, **env)
    return run_command([*FAKE_CLAUDE, *args], timeout=30, env=full, stdin_text="" if "-p" in args else None)


class HelpTest(unittest.TestCase):
    def test_parse_help_from_fake_claude(self):
        result = fake("--help")
        info = claude.parse_help(result.stdout)
        self.assertEqual(info.aliases, ("fable", "opus", "sonnet"))
        self.assertEqual(info.efforts, ("low", "medium", "high", "xhigh", "max"))

    def test_parse_help_crlf_and_other_lists(self):
        result = fake("--help", FAKE_CLAUDE_ALIASES="opus,haiku", FAKE_CLAUDE_EFFORTS="low,high")
        info = claude.parse_help(result.stdout.replace("\n", "\r\n"))
        self.assertEqual((info.aliases, info.efforts), (("opus", "haiku"), ("low", "high")))

    def test_changed_help_gives_nothing(self):
        info = claude.parse_help(fake("--help", FAKE_CLAUDE_ALIASES="-").stdout)
        self.assertEqual((info.aliases, info.efforts), ((), ()))

    def test_parse_version(self):
        self.assertEqual(claude.parse_version(fake("--version").stdout), "2.1.309")
        self.assertIsNone(claude.parse_version("no version"))


class EffortTest(unittest.TestCase):
    def test_resolve_effort(self):
        efforts = ("low", "medium", "high", "xhigh", "max")
        self.assertEqual(claude.resolve_effort("max", efforts=efforts, model="opus").launch, "max")
        self.assertEqual(claude.resolve_effort("highest", efforts=efforts, model="opus").launch, "max")
        with self.assertRaises(EngineError) as ctx:
            claude.resolve_effort("ultra", efforts=efforts, model="opus")
        self.assertEqual(ctx.exception.code, "OAH_EFFORT_UNSUPPORTED")
        self.assertEqual(claude.resolve_effort("max", efforts=(), model="opus").metadata, "missing")


class CommandTest(unittest.TestCase):
    def test_build_command_work(self):
        argv = claude.build_command(["claude"], model="opus", effort="high", permission_mode="acceptEdits",
                                    tools=["Read", "Edit", "Bash"], read_only=False)
        self.assertEqual(argv, ["claude", "-p", "--output-format", "json", "--model", "opus", "--effort", "high",
                                "--permission-mode", "acceptEdits", "--tools", "Read,Edit,Bash"])

    def test_review_is_read_only_claude(self):
        argv = claude.build_command(["claude"], model="opus", effort="max", permission_mode="acceptEdits",
                                    tools=["Read", "Edit", "Bash"], read_only=True)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--tools") + 1], ",".join(claude.READ_ONLY_TOOLS))
        self.assertNotIn("Edit", argv[argv.index("--tools") + 1])

    def test_bad_values_refuse(self):
        for kwargs in ({"permission_mode": "sudo"}, {"tools": ["Read,Edit"]}, {"model": "-x"}, {"effort": "a b"}):
            args = dict(model="opus", effort="high", permission_mode="acceptEdits", tools=["Read"], read_only=False)
            args.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(EngineError):
                claude.build_command(["claude"], **args)

    def test_never_defaults_to_bypass(self):
        self.assertEqual(claude.DEFAULT_PERMISSION_MODE, "acceptEdits")


class ObserveTest(unittest.TestCase):
    def test_effort_is_unobservable_in_json_result(self):
        result = fake("-p", "--output-format", "json", "--model", "opus")
        observed = claude.observe(result.stdout)
        self.assertEqual(observed.models, ("claude-opus-fake",))
        self.assertIsNone(observed.effort)
        self.assertFalse(observed.effort_observable)

    def test_reported_effort_is_observed(self):
        result = fake("-p", "--model", "opus", FAKE_CLAUDE_REPORT_EFFORT="high")
        observed = claude.observe(result.stdout)
        self.assertEqual((observed.effort, observed.effort_observable), ("high", True))

    def test_non_json_output(self):
        observed = claude.observe("not json")
        self.assertEqual((observed.models, observed.effort, observed.effort_observable), ((), None, False))


if __name__ == "__main__":
    unittest.main()
