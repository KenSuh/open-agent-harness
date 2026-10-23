# 패킷 검사 — 템플릿 배치, 초안 생성·덮어쓰기 거부, 필수 구획·AC 중복·수락 상태, 빈 Scope·과정 단계 경고, GATES.md 구조, CLI 종료 코드를 본다.
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import re
import secrets
import types
import unittest
from unittest.mock import Mock, patch

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


NO_NETWORK_NOTE = """<!-- "No network" forbids installs, downloads, fetches and outside web access, except two things oah
needs: `oah impact`'s automatic refresh of the default branch, and a re-ranker you enabled (it also
needs its key). Write it as
"no network except the re-ranker and oah impact's automatic default-branch refresh".
Write "no re-ranker" here to turn the re-ranker off; the default-branch refresh always stays on. -->"""

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

    def test_template_no_network_note_is_pinned(self):
        note = packet.render("demo").split("### Constraints\n", 1)[1].split("\n### Non-goals", 1)[0]
        self.assertEqual(note, NO_NETWORK_NOTE)
        self.assertFalse(packet.rerank_disabled(packet.render("demo")))

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


def run_cli(cwd: pathlib.Path, *argv: str, home: pathlib.Path | None = None) -> tuple[int, str, str]:
    """`oah <argv>` in-process, with a temporary home folder so no skill of this machine is read."""
    out, err = io.StringIO(), io.StringIO()
    previous = pathlib.Path.cwd()
    os.chdir(cwd)
    home = home or pathlib.Path(cwd).parent / "home"
    try:
        with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            for name in [key for key in os.environ if key.startswith("OAH_")] + ["CLAUDE_CONFIG_DIR", "CODEX_HOME"]:
                os.environ.pop(name, None)
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

    def test_rerank_disabled_by_constraints(self):
        def constraints(text: str) -> str:
            return accepted().replace("### Constraints\n", f"### Constraints\n{text}\n")

        for text in ("No re-ranker for this task.", "- without the reranker", "Re-ranker: off",
                     "disable the re-ranker", "Turn off the re-ranker."):
            with self.subTest(text=text):
                self.assertTrue(packet.rerank_disabled(constraints(text)))
        for text in ("no network except the re-ranker and oah impact's automatic default-branch refresh",
                     "<!-- no re-ranker -->", "Use the re-ranker."):
            with self.subTest(text=text):
                self.assertFalse(packet.rerank_disabled(constraints(text)))
        self.assertFalse(packet.rerank_disabled(accepted(extra="No re-ranker in the return section.")))

    def test_cli_outside_repository(self):
        outside = make_tmp_dir(self)
        with patch.dict(os.environ, GIT_CEILING_DIRECTORIES=str(outside.parent)):
            code, _, err = run_cli(outside, "packet", "new", "demo")
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_NOT_A_REPO"), err)


class PacketPlanTest(unittest.TestCase):
    """`oah packet new` scores the axes, records tier and skills in ## Plan, and refreshes impact first."""

    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/app.py": "x = 1\n", "db/migrations/001.sql": "create table t (id int);\n"})
        self.home = self.repo.parent / "home"
        for name in ("test-driven-development", "requesting-code-review", "receiving-code-review"):
            folder = self.home / ".claude" / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")
        self.path = self.repo / ".oah" / "tasks" / "demo" / "PACKET.md"

    def new(self, *extra: str) -> tuple[int, str, str]:
        return run_cli(self.repo, "packet", "new", "demo", *extra, home=self.home)

    def plan(self) -> str:
        return self.path.read_text(encoding="utf-8").split("## Plan", 1)[1].split("\n## Acceptance", 1)[0]

    def test_new_records_axes_tier_and_skills_in_plan(self):
        code, out, err = self.new("--scope", "src/app.py")
        self.assertEqual(code, 0, out + err)
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(intent.parse(text).status, "draft")
        self.assertIn("## Scope        (paths this task may write)\n", text)
        self.assertEqual(packet.check(self.path, require_accepted=False).scope, ("src/app.py",))
        plan = self.plan()
        for line in ("- initial → claude → medium", "- review → codex → medium", "- correction → codex → medium",
                     "- initial: kind=implementation intensity=low reasoning=medium risk=normal → medium "
                     "(advice=unused(not-configured))",
                     "  - skills (rerank=unused(not-enabled)):",
                     "    - `test-driven-development` (user): code changes are written test-first "
                     "(kind implementation, stage initial)",
                     "    - `requesting-code-review` (user): review work follows a review checklist (kind review); "
                     "review rounds follow a review checklist (stage review)"):
            self.assertIn(line, plan.splitlines())
        self.assertIn("why: kind implementation from stage initial", plan)
        self.assertNotIn(str(self.home), text)
        self.assertNotIn("\r", text)
        self.assertIn("plan: initial → claude → medium; review → codex → medium; correction → codex → medium", out)
        self.assertIn("rerank=unused(not-enabled)", out)

    def test_risky_scope_and_explicit_axes_raise_the_tier(self):
        code, out, err = self.new("--scope", "db/migrations/001.sql", "--kind", "migration")
        self.assertEqual(code, 0, out + err)
        plan = self.plan()
        self.assertIn("- initial → claude → high", plan.splitlines())
        self.assertIn("- review → codex → high", plan.splitlines())
        self.assertIn("kind=migration", plan)
        self.assertIn("risk path db/migrations/001.sql (migration)", plan)

    def test_empty_scope_warns_in_plan(self):
        code, out, err = self.new()
        self.assertEqual(code, 0, out + err)
        self.assertIn("OAH_SCOPE_EMPTY", self.plan())
        self.assertIn("OAH_SCOPE_EMPTY", err)

    def test_plan_lines_are_read_by_call(self):
        self.assertEqual(self.new("--scope", "src/app.py")[0], 0)
        from oah import call
        read = call.read_packet(self.repo, self.path, parse_intent=None)
        self.assertEqual(read.plan, {"initial": ("claude", "medium"), "review": ("codex", "medium"),
                                     "correction": ("codex", "medium")})
        self.assertEqual(read.scope, ("src/app.py",))
        self.assertEqual({stage: axes.to_json() for stage, axes in read.axes.items()},
                         {stage: {"kind": "review" if stage == "review" else "implementation",
                                  "intensity": "low", "reasoning": "medium", "risk": "normal"}
                          for stage in ("initial", "review", "correction")})

    def test_new_refreshes_impact_first(self):
        import oah.impact

        seen = []
        real = oah.impact.ensure_fresh

        def spy(root, **kwargs):
            seen.append((pathlib.Path(root).resolve(), self.path.exists()))
            return real(root, **kwargs)

        with patch.object(oah.impact, "ensure_fresh", spy):
            code, out, err = self.new("--scope", "src/app.py")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, [(self.repo.resolve(), False)])
        self.assertTrue(any(line.startswith("impact=map ") for line in out.splitlines()), out)

    def test_new_refuses_when_impact_cannot_refresh(self):
        import oah.impact

        def stale(root, **kwargs):
            raise oah.impact.ImpactError("OAH_IMPACT_STALE", "the inputs kept changing")

        with patch.object(oah.impact, "ensure_fresh", stale):
            code, out, err = self.new("--scope", "src/app.py")
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_IMPACT_STALE"), err)
        self.assertFalse(self.path.exists())

    def test_new_without_commits_reports_impact_unavailable(self):
        empty = make_git_repo(self)
        code, out, err = run_cli(empty, "packet", "new", "demo", home=self.home)
        self.assertEqual(code, 0, out + err)
        self.assertIn("impact=unavailable(no commits yet)", out.splitlines())
        self.assertTrue((empty / ".oah" / "tasks" / "demo" / "PACKET.md").is_file())

    def test_no_rerank_flag_is_recorded(self):
        self.assertEqual(self.new("--scope", "src/app.py", "--no-rerank")[0], 0)
        self.assertIn("  - skills (rerank=unused(disabled-by-brief)):", self.plan().splitlines())

    def test_no_rerank_skips_both_provider_methods(self):
        from oah import rerank

        config = self.repo / ".oah" / "config.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text('[rerank]\nenabled = true\nprovider = "fake"\n', encoding="utf-8")
        secret = "fake-" + secrets.token_hex(16)
        provider = types.SimpleNamespace(name="fake", key_name=lambda config: "FAKE_RERANK_KEY",
                                         score=Mock(return_value=[]), advise=Mock(return_value=("max", 0.8)))
        with patch.dict(os.environ, {"FAKE_RERANK_KEY": secret}), \
                patch.dict(rerank.PROVIDERS, {"fake": provider}):
            code, out, err = self.new("--scope", "src/app.py", "--no-rerank")
        self.assertEqual(code, 0, out + err)
        provider.score.assert_not_called()
        provider.advise.assert_not_called()
        self.assertEqual(self.plan().count("(advice=unused(disabled-by-brief))"), 3)
        self.assertFalse(secret in out + err + self.path.read_text(encoding="utf-8"),
                         "runtime key leaked into packet output")


if __name__ == "__main__":
    unittest.main()
