# 패킷 검사 — 템플릿 배치, 초안 생성·덮어쓰기 거부, 필수 구획·AC 중복·수락 상태, 빈 Scope·과정 단계 경고, GATES.md 구조, CLI 종료 코드를 본다.
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import re
import unittest
from unittest.mock import patch

from oah import cli, intent, packet, packet_cli
from tests.helpers import git_commit, make_git_repo, make_tmp_dir

LAYOUT = [
    "<!-- OAH_INTENT:BEGIN -->",
    "## Intent",
    "Intent-ID: {task}",
    "Revision: 1",
    'Status: draft            # becomes "accepted" when you approve it',
    "### Problem",
    "### Desired outcome",
    "### Success conditions",
    "- AC1: <checkable condition>",
    "### Constraints",
    "### Non-goals",
    "### Open questions",
    "<!-- OAH_INTENT:END -->",
    "## Scope        (paths this task may write)",
    "## Plan         (stage → engine → tier)",
    "## Acceptance   (GATES.md in this folder)",
    '## Return       (INTENT <id>@r<n> <digest> + one "AC<n>: met|unmet|deferred — evidence" line each;',
    "                rerank=used|unused(<reason>) when impact was used; the DB-impact line for DB changes)",
]


def accepted(task: str = "demo", *, acs: tuple[str, ...] = ("- AC1: the report lists every changed file",),
             scope: str = "- src/app.py\n", extra: str = "", ident: str | None = None) -> str:
    lines = [intent.BEGIN, "## Intent", f"Intent-ID: {ident or task}", "Revision: 1", "Status: accepted",
             "### Problem", "Reports miss files.", "### Desired outcome", "Every file is listed.",
             "### Success conditions", *acs, "### Constraints", "### Non-goals", "### Open questions",
             intent.END, "## Scope", scope.rstrip("\n"), "## Plan", "- initial → claude", "## Acceptance",
             "## Return", extra]
    return "\n".join(lines) + "\n"


def codes(result: packet.CheckResult, level: str | None = None) -> list[str]:
    return [p.code for p in result.problems if level is None or p.level == level]


GATES_OK = """# Gates: demo

- [ ] G1: the report lists every file
  CHECK: python -m unittest tests.test_report
  EXPECT: /OK/
  EVIDENCE: pending
- [x] G2: privacy scan is clean
  CHECK: python tools/privacy_scan.py
  EXPECT: PRIVACY_SCAN_CLEAN
  EVIDENCE: PRIVACY_SCAN_CLEAN files=3
ABANDON: G1 DEFERRED-TO-LANDING — heavy
"""


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.root = make_tmp_dir(self)

    def test_new_scaffolds_exact_layout(self):
        path = packet.create(self.root, "demo")
        self.assertEqual(path, self.root / ".oah" / "tasks" / "demo" / "PACKET.md")
        text = path.read_text(encoding="utf-8")
        structural = [line for line in text.split("\n")
                      if line.startswith(("<!-- OAH_INTENT", "## ", "### ", "Intent-ID:", "Revision:", "Status:",
                                          "- AC", "                rerank"))]
        self.assertEqual(structural, [line.format(task="demo") for line in LAYOUT])
        self.assertNotIn("\r", text)

    def test_new_is_a_draft_that_parses(self):
        parsed = intent.parse(packet.create(self.root, "demo").read_text(encoding="utf-8"))
        self.assertEqual((parsed.id, parsed.revision, parsed.status, parsed.acs), ("demo", 1, "draft", ("AC1",)))

    def test_new_refuses_existing_packet(self):
        path = packet.create(self.root, "demo")
        path.write_text("mine\n", encoding="utf-8")
        with self.assertRaises(packet.PacketError) as caught:
            packet.create(self.root, "demo")
        self.assertEqual(caught.exception.code, "OAH_PACKET_EXISTS")
        self.assertEqual(path.read_text(encoding="utf-8"), "mine\n")

    def test_new_refuses_bad_task_name(self):
        for task in ("", "../x", "a/b", "-x", "a b"):
            with self.subTest(task=task), self.assertRaises(packet.PacketError) as caught:
                packet.create(self.root, task)
            self.assertEqual(caught.exception.code, "OAH_TASK_INVALID")
        self.assertFalse((self.root / ".oah").exists())

    def test_template_explains_no_network(self):
        constraints = packet.render("demo").split("### Constraints", 1)[1].split("### Non-goals", 1)[0]
        self.assertIn('"No network" forbids installs, downloads, fetches and outside web access, except', constraints)
        self.assertIn("`oah impact`'s automatic refresh of the default branch", constraints)
        self.assertIn("no network except the re-ranker and oah impact's automatic default-branch refresh", constraints)
        self.assertIn("the default-branch refresh always stays on", constraints)

    def test_template_return_lists_rerank_and_db_impact(self):
        returned = packet.render("demo").split("## Return", 1)[1]
        self.assertIn("rerank=used", returned)
        self.assertIn("rerank=unused(<reason>)", returned)
        self.assertIn("db-impact:", returned)
        self.assertIn("oah intent show demo", returned)

    def test_fresh_draft_check_reports_draft_and_placeholders(self):
        result = packet.check(packet.create(self.root, "demo"), task="demo")
        self.assertEqual(codes(result, "error"), ["OAH_INTENT_NOT_ACCEPTED"])
        self.assertIn("OAH_SCOPE_EMPTY", codes(result, "warning"))
        self.assertIn("OAH_PACKET_PLACEHOLDER", codes(result, "warning"))


class CheckTest(unittest.TestCase):
    def test_accepted_packet_is_clean(self):
        result = packet.check_text(accepted(), task="demo")
        self.assertEqual(result.problems, ())
        self.assertEqual(result.scope, ("src/app.py",))
        self.assertEqual(result.intent.id, "demo")

    def test_check_required_sections(self):
        for name in ("Scope", "Plan", "Acceptance", "Return"):
            with self.subTest(missing=name):
                text = re.sub(rf"^## {name}\n", "", accepted(), flags=re.M)
                self.assertIn("OAH_PACKET_SECTION_MISSING", codes(packet.check_text(text)))
            with self.subTest(duplicate=name):
                self.assertIn("OAH_PACKET_SECTION_DUPLICATE", codes(packet.check_text(accepted() + f"## {name}\n")))
        missing_intent = accepted().split(intent.END + "\n", 1)[1]
        self.assertIn("OAH_INTENT_MISSING", codes(packet.check_text(missing_intent)))

    def test_section_headings_inside_the_intent_do_not_count(self):
        text = accepted().replace("## Scope\n- src/app.py\n", "")
        self.assertIn("OAH_PACKET_SECTION_MISSING", codes(packet.check_text(text)))

    def test_check_duplicate_ac_ids(self):
        result = packet.check_text(accepted(acs=("- AC1: one", "- AC1: two")))
        self.assertIn("OAH_INTENT_AC_DUPLICATE", codes(result, "error"))

    def test_check_draft_refused_unless_allowed(self):
        draft = accepted().replace("Status: accepted", "Status: draft")
        self.assertEqual(codes(packet.check_text(draft), "error"), ["OAH_INTENT_NOT_ACCEPTED"])
        self.assertEqual(packet.check_text(draft, require_accepted=False).problems, ())

    def test_check_scope_empty_warns(self):
        for scope in ("", "<!-- one path per line -->\n", "Paths go here.\n"):
            with self.subTest(scope=scope):
                result = packet.check_text(accepted(scope=scope))
                self.assertEqual(codes(result), ["OAH_SCOPE_EMPTY"])
                self.assertEqual(result.errors, ())
                self.assertEqual(result.scope, ())

    def test_scope_entries(self):
        result = packet.check_text(accepted(scope="- src/app.py\n- `docs/guide.md`\n  - nested note\n* tests\n"))
        self.assertEqual(result.scope, ("src/app.py", "docs/guide.md", "tests"))

    def test_check_intent_id_must_match_task(self):
        result = packet.check_text(accepted(ident="other"), task="demo")
        self.assertEqual(codes(result, "error"), ["OAH_INTENT_ID_CHANGED"])
        self.assertIn("raise", result.errors[0].message)
        self.assertEqual(packet.check_text(accepted(ident="other")).problems, ())

    def test_process_step_table(self):
        process = [
            "Take a lease on src before editing",
            "The changes are committed and pushed",
            "Commit the work in small commits",
            "Codex review passes",
            "Two review rounds are completed",
            "Open a pull request for the branch",
            "The return carries the INTENT line",
            "Run oah finish without --allow-out-of-scope",
            "R2 finds no blocking issue",
        ]
        outcomes = [
            "`oah packet check` reports OAH_SCOPE_EMPTY for an empty Scope",
            "The receipt records the commit SHA and the digest",
            "The digest is equal for CRLF and LF files",
            "`oah preflight` aborts when HEAD is behind the remote",
            "`load_config` merges the section over the defaults",
            "`check_return` reports invented AC lines",
            "`binding_state` returns `current` for an accepted packet @r1",
            "`oah start` refuses an overlapping lease scope",
        ]
        for text in process:
            with self.subTest(process=text):
                result = packet.check_text(accepted(acs=(f"- AC1: {text}",)))
                self.assertEqual(codes(result), ["OAH_INTENT_PROCESS_STEP"])
                self.assertIn("AC1", result.warnings[0].message)
        for text in outcomes:
            with self.subTest(outcome=text):
                self.assertEqual(packet.check_text(accepted(acs=(f"- AC1: {text}",))).problems, ())

    def test_gates_ledger_structure_table(self):
        self.assertEqual(packet.check_text(accepted(), gates_text=GATES_OK).problems, ())
        broken = {
            "no gates": "# Gates: demo\n",
            "missing check": GATES_OK.replace("  CHECK: python tools/privacy_scan.py\n", ""),
            "duplicate expect": GATES_OK.replace("  EXPECT: /OK/\n", "  EXPECT: /OK/\n  EXPECT: again\n"),
            "duplicate id": GATES_OK.replace("G2: privacy", "G1: privacy"),
            "bad box": GATES_OK.replace("- [x] G2", "- [v] G2"),
            "field before gate": "  CHECK: python x.py\n" + GATES_OK,
            "unknown abandon": GATES_OK + "ABANDON: G9 gone\n",
            "abandon without reason": GATES_OK + "ABANDON: G2\n",
        }
        for name, text in broken.items():
            with self.subTest(name):
                result = packet.check_text(accepted(), gates_text=text)
                self.assertEqual(codes(result), ["OAH_GATES_INVALID"], [str(p) for p in result.problems])

    def test_check_reads_gates_beside_packet(self):
        folder = make_tmp_dir(self) / "demo"
        folder.mkdir()
        (folder / "PACKET.md").write_text(accepted(), encoding="utf-8")
        self.assertEqual(packet.check(folder / "PACKET.md").problems, ())
        (folder / "GATES.md").write_text("# Gates\n", encoding="utf-8")
        self.assertEqual(codes(packet.check(folder / "PACKET.md")), ["OAH_GATES_INVALID"])

    def test_check_unsafe_source(self):
        folder = make_tmp_dir(self)
        self.assertEqual(codes(packet.check(folder / "absent.md")), ["OAH_INTENT_SOURCE_MISSING"])
        big = folder / "big.md"
        big.write_bytes(b"a" * intent.MAX_BYTES)
        self.assertEqual(codes(packet.check(big)), ["OAH_INTENT_SOURCE_INVALID"])

    def test_crlf_packet_checks_clean(self):
        self.assertEqual(packet.check_text(accepted().replace("\n", "\r\n"), task="demo").problems, ())


def run_cli(cwd: pathlib.Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    previous = pathlib.Path.cwd()
    os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class PacketCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/app.py": "x = 1\n"})

    def test_cli_new_and_check(self):
        self.assertEqual(packet_cli.COMMANDS, ("packet",))
        code, out, err = run_cli(self.repo, "packet", "new", "demo")
        self.assertEqual(code, 0, err)
        self.assertIn("OAH_PACKET_CREATED .oah/tasks/demo/PACKET.md", out)
        self.assertIn("Status: accepted", out)
        code, out, err = run_cli(self.repo, "packet", "new", "demo")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_PACKET_EXISTS"), err)

        code, out, _ = run_cli(self.repo, "packet", "check", "demo")
        self.assertEqual(code, 1)
        self.assertIn("error OAH_INTENT_NOT_ACCEPTED", out)
        self.assertIn("OAH_PACKET_INVALID", out)
        code, out, _ = run_cli(self.repo, "packet", "check", "demo", "--allow-draft")
        self.assertEqual(code, 0, out)
        self.assertIn("warning OAH_SCOPE_EMPTY", out)

        (self.repo / ".oah/tasks/demo/PACKET.md").write_text(accepted(), encoding="utf-8")
        code, out, _ = run_cli(self.repo, "packet", "check", "demo")
        self.assertEqual(code, 0, out)
        self.assertIn("OAH_PACKET_OK .oah/tasks/demo/PACKET.md intent=demo@r1 status=accepted", out)

    def test_cli_check_file_infers_task_from_layout(self):
        path = self.repo / ".oah/tasks/demo/PACKET.md"
        path.parent.mkdir(parents=True)
        path.write_text(accepted(ident="other"), encoding="utf-8")
        code, out, _ = run_cli(self.repo / "src", "packet", "check", "--file", str(path))
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_ID_CHANGED", out)
        loose = self.repo / "loose.md"
        loose.write_text(accepted(ident="other"), encoding="utf-8")
        self.assertEqual(run_cli(self.repo, "packet", "check", "--file", "loose.md")[0], 0)

    def test_cli_check_missing_packet(self):
        code, out, _ = run_cli(self.repo, "packet", "check", "nothing")
        self.assertEqual(code, 1)
        self.assertIn("OAH_INTENT_SOURCE_MISSING", out)
        self.assertIn("oah packet new nothing", out)

    def test_cli_outside_repository(self):
        outside = make_tmp_dir(self)
        with patch.dict(os.environ, GIT_CEILING_DIRECTORIES=str(outside.parent)):
            code, _, err = run_cli(outside, "packet", "new", "demo")
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_NOT_A_REPO"), err)


if __name__ == "__main__":
    unittest.main()
