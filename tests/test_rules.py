# 규칙 블록 검사 — 생성·추가·블록만 교체, 멱등, CRLF/LF 보존, 깨진·중복 표시줄 거부, 32 KiB·6 KB 한도, 로컬 규칙, CLAUDE.md 가져오기, 전역 파일 무변경을 본다.
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import unittest

from oah import rules, rules_cli
from oah.cli import _command_table, command_modules, main
from oah.rules import BEGIN, END, RulesError
from tests.helpers import make_git_repo, make_tmp_dir


def block_of(data: bytes) -> bytes:
    start = data.index(BEGIN.encode())
    return data[start:data.index(END.encode()) + len(END)]


class RulesTest(unittest.TestCase):
    def test_dispatched_workers_are_exempt_from_lease_lifecycle_commands(self):
        for lang in ("en", "ko"):
            with self.subTest(lang=lang):
                rules.sync(self.root, lang=lang)
                text = self.agents.read_text(encoding="utf-8")
                lease_rule = next(line for line in text.splitlines() if line.startswith("2. "))
                if lang == "en":
                    self.assertIn("dispatcher's lease", lease_rule)
                    self.assertIn("never start, renew or finish", lease_rule)
                    self.assertIn("dispatcher's leased worktree", text)
                else:
                    self.assertIn("발주자의 리스", lease_rule)
                    self.assertIn("시작·갱신·종료하지 않는다", lease_rule)
                    self.assertIn("발주자의 리스 워크트리", text)

    def setUp(self):
        self.root = make_tmp_dir(self)
        self.agents = self.root / "AGENTS.md"
        self.claude = self.root / "CLAUDE.md"

    def refused(self, code: str) -> None:
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with self.assertRaises(RulesError) as ctx:
            rules.sync(self.root)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, before)

    def test_creates_files(self):
        result = rules.sync(self.root)
        self.assertEqual(result.changed, ("AGENTS.md", "CLAUDE.md"))
        data = self.agents.read_bytes()
        self.assertTrue(data.startswith(BEGIN.encode() + b"\n"))
        self.assertTrue(data.endswith(END.encode() + b"\n"))
        self.assertEqual(self.claude.read_bytes(), b"@AGENTS.md\n")

    def test_local_reserved_markers_refuse_before_either_write(self):
        local = self.root / ".oah" / "rules.local.md"
        local.parent.mkdir()
        for existing in (False, True):
            if existing:
                rules.sync(self.root)
            for marker in (BEGIN, END, "<!-- OAH:RULES:BEGIN v2 -->", "<!-- OAH:RULES:END"):
                with self.subTest(existing=existing, marker=marker):
                    local.write_bytes(f"Project rule.\r\n{marker}  \r\n".encode("utf-8"))
                    before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
                    with self.assertRaises(RulesError) as ctx:
                        rules.sync(self.root)
                    self.assertIn("OAH_RULES_MARKER", ctx.exception.code)
                    self.assertIn("rules.local.md", str(ctx.exception))
                    self.assertTrue(rules.check(self.root))
                    self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
            local.write_text("Project rule.\n", encoding="utf-8")

    def test_append_when_absent_keeps_bytes(self):
        original = b"# My agents\r\n\r\nUse tabs.\r\nno final newline"
        self.agents.write_bytes(original)
        rules.sync(self.root)
        data = self.agents.read_bytes()
        self.assertTrue(data.startswith(original + b"\r\n\r\n" + BEGIN.encode() + b"\r\n"))
        self.assertTrue(data.endswith(END.encode() + b"\r\n"))

    def test_replace_only_block(self):
        before, after = b"intro \xe2\x9c\x93\n\n", b"\n## Mine\nkeep\tme  \n"
        self.agents.write_bytes(before + BEGIN.encode() + b"\nold rules\n" + END.encode() + after)
        rules.sync(self.root)
        data = self.agents.read_bytes()
        self.assertTrue(data.startswith(before + BEGIN.encode()))
        self.assertTrue(data.endswith(END.encode() + after))
        self.assertNotIn(b"old rules", data)

    def test_idempotent(self):
        rules.sync(self.root, lang="ko")
        snapshot = (self.agents.read_bytes(), self.claude.read_bytes())
        result = rules.sync(self.root)
        self.assertEqual(result.changed, ())
        self.assertEqual(result.lang, "ko")
        self.assertEqual((self.agents.read_bytes(), self.claude.read_bytes()), snapshot)
        self.assertEqual(rules.check(self.root), [])

    def test_keeps_crlf_and_lf(self):
        for eol in (b"\r\n", b"\n"):
            with self.subTest(eol=eol):
                self.agents.write_bytes(b"top" + eol + BEGIN.encode() + eol + b"x" + eol + END.encode() + eol + b"end")
                self.claude.write_bytes(b"# Claude" + eol)
                rules.sync(self.root)
                block = block_of(self.agents.read_bytes())
                if eol == b"\r\n":
                    self.assertNotIn(b"\n", block.replace(b"\r\n", b""))
                else:
                    self.assertNotIn(b"\r", block)
                self.assertTrue(self.agents.read_bytes().endswith(END.encode() + eol + b"end"))
                self.assertEqual(self.claude.read_bytes(), b"# Claude" + eol + b"@AGENTS.md" + eol)

    def test_mixed_line_endings_outside_block_untouched(self):
        outside = b"a\r\nb\nc\r\n"
        self.agents.write_bytes(outside + BEGIN.encode() + b"\r\nx\r\n" + END.encode() + b"\r\n" + outside)
        rules.sync(self.root)
        data = self.agents.read_bytes()
        self.assertTrue(data.startswith(outside + BEGIN.encode() + b"\r\n"))
        self.assertTrue(data.endswith(END.encode() + b"\r\n" + outside))

    def test_broken_and_duplicate_markers_refuse(self):
        cases = {
            "begin only": (BEGIN + "\nx\n", "OAH_RULES_MARKER_BROKEN"),
            "end only": ("x\n" + END + "\n", "OAH_RULES_MARKER_BROKEN"),
            "reversed": (END + "\n" + BEGIN + "\n", "OAH_RULES_MARKER_BROKEN"),
            "two blocks": (f"{BEGIN}\n{END}\n{BEGIN}\n{END}\n", "OAH_RULES_MARKER_DUPLICATE"),
            "no version": ("<!-- OAH:RULES:BEGIN -->\n" + END + "\n", "OAH_RULES_MARKER_BROKEN"),
            "v0": ("<!-- OAH:RULES:BEGIN v0 -->\n" + END + "\n", "OAH_RULES_MARKER_BROKEN"),
            "unclosed comment": ("<!-- OAH:RULES:BEGIN v1\n" + END + "\n", "OAH_RULES_MARKER_BROKEN"),
            "newer": ("<!-- OAH:RULES:BEGIN v2 -->\n" + END + "\n", "OAH_RULES_NEWER"),
        }
        for name, (text, code) in cases.items():
            with self.subTest(name):
                self.agents.write_text(text, encoding="utf-8")
                self.refused(code)
                self.assertFalse(self.claude.exists())

    def test_mention_is_not_marker(self):
        self.agents.write_text(f"Docs: the block between `{BEGIN}` and `{END}`.\n  {BEGIN}\n", encoding="utf-8")
        rules.sync(self.root)
        self.assertEqual(self.agents.read_text(encoding="utf-8").count(BEGIN), 3)
        self.assertEqual(rules.sync(self.root).changed, ())

    def test_budget_refuses_without_writing(self):
        rules.sync(self.root)
        block_size = len(self.agents.read_bytes())
        filler = rules.AGENTS_BUDGET - block_size - 2  # "x"*n + "\n\n" + block: exactly at the budget
        self.agents.write_bytes(b"x" * filler + b"\n")
        rules.sync(self.root)
        self.assertEqual(len(self.agents.read_bytes()), rules.AGENTS_BUDGET)
        self.agents.write_bytes(b"x" * (filler + 1) + b"\n")
        self.refused("OAH_RULES_BUDGET")

    def test_block_under_6kb_both_langs(self):
        for lang in rules.LANGS:
            for eol in ("\n", "\r\n"):
                with self.subTest(lang=lang, eol=eol):
                    self.assertLess(len(rules.render_block(lang, "", eol).encode("utf-8")), 6 * 1024)

    def test_block_mentions_required_rules(self):
        required = {
            "en": ["OAH_WORKER=1", "session-entry", "Claude Code does the initial implementation",
                   "Codex CLI then reviews, corrects and re-reviews", "oah start", "PACKET.md", "oah preflight",
                   "GATES.md", "Verify what changed", "not independent", "never push", "No keys",
                   "${ENV_VAR}", "docs/wiki/"],
            "ko": ["OAH_WORKER=1", "세션 시작 절차", "1차 구현은 Claude Code", "Codex CLI 가", "oah start",
                   "PACKET.md", "oah preflight", "GATES.md", "바뀐 만큼만", "독립 리뷰가 아니며", "푸시하지 않는다",
                   "키 금지", "${ENV_VAR}", "docs/wiki/"],
        }
        for lang, phrases in required.items():
            text = rules.render_block(lang, "", "\n")
            for phrase in phrases:
                with self.subTest(lang=lang, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_local_rules_appended(self):
        local = self.root / ".oah" / "rules.local.md"
        local.parent.mkdir()
        local.write_bytes(b"- Use pnpm.\r\n- Never touch vendor/.\r\n")
        rules.sync(self.root)
        block = block_of(self.agents.read_bytes()).decode()
        self.assertIn("### Project rules (.oah/rules.local.md)\n\n- Use pnpm.\n- Never touch vendor/.\n" + END, block)
        local.write_text("- Use npm.\n", encoding="utf-8")
        self.assertEqual(len(rules.check(self.root)), 1)
        rules.sync(self.root)
        self.assertIn("- Use npm.", self.agents.read_text(encoding="utf-8"))
        local.write_text("- rule\n" * 1000, encoding="utf-8")
        self.refused("OAH_RULES_BLOCK_TOO_LARGE")

    def test_claude_import_line_once(self):
        self.claude.write_text("# Notes\n  @AGENTS.md  \nmore\n", encoding="utf-8")
        rules.sync(self.root)
        self.assertEqual(self.claude.read_text(encoding="utf-8"), "# Notes\n  @AGENTS.md  \nmore\n")
        self.claude.write_text("# Notes", encoding="utf-8")
        rules.sync(self.root)
        self.assertEqual(self.claude.read_text(encoding="utf-8"), "# Notes\n" + rules.IMPORT + "\n")
        self.claude.write_text((rules.IMPORT + "\n") * 2, encoding="utf-8")
        self.refused("OAH_RULES_MARKER_DUPLICATE")

    def test_links_refused(self):
        target = make_tmp_dir(self) / "global-CLAUDE.md"
        target.write_text("global\n", encoding="utf-8")
        for name in ("AGENTS.md", "CLAUDE.md"):
            with self.subTest(name=name):
                link = self.root / name
                try:
                    link.symlink_to(target)
                except OSError:
                    self.skipTest("symlinks are not available")
                self.refused("OAH_RULES_LINK")
                self.assertEqual(target.read_text(encoding="utf-8"), "global\n")
                link.unlink()

    def test_non_utf8_refused(self):
        self.agents.write_bytes(b"caf\xe9\n")
        self.refused("OAH_RULES_ENCODING")


def run_cli(cwd: pathlib.Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class RulesCliTest(unittest.TestCase):
    def test_commands_tuple(self):
        self.assertEqual(rules_cli.COMMANDS, ("rules",))
        self.assertIs(_command_table(command_modules())["rules"], rules_cli)

    def test_check_reports_drift(self):
        repo = make_git_repo(self)
        code, out, _ = run_cli(repo, "rules", "check")
        self.assertEqual(code, 1)
        self.assertIn("AGENTS.md is missing", out)
        self.assertIn("oah rules sync", out)
        self.assertEqual(run_cli(repo, "rules", "sync", "--lang", "ko")[0], 0)
        self.assertEqual(run_cli(repo, "rules", "check"), (0, "OAH_RULES_IN_SYNC\n", ""))
        self.assertEqual(run_cli(repo, "rules", "check", "--lang", "en")[0], 1)
        (repo / "AGENTS.md").write_text((repo / "AGENTS.md").read_text(encoding="utf-8").replace("1차", "2차"),
                                        encoding="utf-8")
        code, out, _ = run_cli(repo, "rules", "check")
        self.assertEqual(code, 1)
        self.assertIn("differs", out)
        (repo / "CLAUDE.md").write_text("nothing\n", encoding="utf-8")
        self.assertEqual(run_cli(repo, "rules", "sync")[0], 0)
        self.assertEqual(run_cli(repo, "rules", "check")[0], 0)
        (repo / "AGENTS.md").write_text(BEGIN + "\n", encoding="utf-8")
        code, out, _ = run_cli(repo, "rules", "check")
        self.assertEqual(code, 1)
        self.assertIn("OAH_RULES_MARKER_BROKEN", out)
        code, _, err = run_cli(repo, "rules", "sync")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_RULES_MARKER_BROKEN"))

    def test_global_files_untouched(self):
        home = make_tmp_dir(self) / "home"
        for rel in (".claude/CLAUDE.md", ".codex/AGENTS.md"):
            (home / rel).parent.mkdir(parents=True)
            (home / rel).write_text("global\n", encoding="utf-8")
        before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        old_home = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
        try:
            repo = make_git_repo(self)
            self.assertEqual(run_cli(repo, "rules", "sync")[0], 0)
            self.assertEqual(run_cli(repo, "rules", "check")[0], 0)
        finally:
            for key, value in old_home.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual({p: p.read_bytes() for p in home.rglob("*") if p.is_file()}, before)
        self.assertEqual(sorted(p.name for p in repo.iterdir() if p.is_file()), ["AGENTS.md", "CLAUDE.md"])


if __name__ == "__main__":
    unittest.main()
