# oah.skillsel 테스트 — 임시 스킬 폴더로 출처 우선순위·선택 표·빈 Scope 경고·첨부 모드·로컬 원격 측정을 확인한다.
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import pathlib
import tomllib
import unittest
from importlib import resources

from oah import rerank, skillsel
from oah.config import load_config
from tests.helpers import make_tmp_dir

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)


def make_skill(root: pathlib.Path, folder: str, *, name: str | None = None, description: str = "does things",
               body: str = "Body text.\n", newline: str = "\n") -> pathlib.Path:
    skill = root / folder
    skill.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    lines.append(f"description: {description}")
    lines.append("---")
    text = newline.join(lines) + newline + body.replace("\n", newline)
    (skill / "SKILL.md").write_bytes(text.encode("utf-8"))
    return skill / "SKILL.md"


class Fixture:
    """Project, kit, user and plugin skill folders laid out in one temp folder."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.base = make_tmp_dir(test)
        self.repo = self.base / "repo"
        self.home = self.base / "home"
        self.project = self.repo / ".claude" / "skills"
        self.kit = self.base / "state" / "skills-cache"
        self.user = self.home / ".claude" / "skills"
        self.agents = self.home / ".agents" / "skills"
        self.plugin = self.home / ".claude" / "plugins" / "cache" / "market" / "superpowers" / "1.0" / "skills"
        make_skill(self.project, "test-driven-development", name="test-driven-development",
                   description="project copy of tdd")
        make_skill(self.kit, "test-driven-development", description="kit copy")
        make_skill(self.user, "systematic-debugging", description="find root causes")
        make_skill(self.user, "verification-before-completion", description="evidence first")
        make_skill(self.agents, "unlazy", description="gates")
        make_skill(self.agents, "react-best-practices", description="react rules")
        for folder in ("test-driven-development", "requesting-code-review", "receiving-code-review",
                       "writing-plans"):
            make_skill(self.plugin, folder, name=folder, description=f"plugin {folder}")

    def roots(self) -> list[skillsel.SkillRoot]:
        return [
            skillsel.SkillRoot(self.project, "project", ".claude/skills"),
            skillsel.SkillRoot(self.kit, "kit", "kit cache"),
            skillsel.SkillRoot(self.user, "user", "~/.claude/skills"),
            skillsel.SkillRoot(self.agents, "user", "~/.agents/skills"),
            skillsel.SkillRoot(self.plugin, "plugin", "~/.claude/plugins/cache/market/superpowers/1.0/skills",
                               plugin="superpowers"),
        ]


def ids(selection: skillsel.Selection) -> set[str]:
    return {skill.id for skill in selection.skills}


class FrontmatterTest(unittest.TestCase):
    def test_frontmatter_parser_crlf_quotes_and_blocks(self) -> None:
        text = ("﻿---\r\nname: 'it''s'\r\ndescription: >-\r\n  folded\r\n  text\r\n"
                "other: \"a \\\"q\\\" b\"\r\nliteral: |\r\n  one\r\n  two\r\nmetadata:\r\n  type: x\r\n"
                "plain: first\r\n  second\r\n---\r\nbody\r\n")
        meta = skillsel.parse_frontmatter(text)
        self.assertEqual(meta["name"], "it's")
        self.assertEqual(meta["description"], "folded text")
        self.assertEqual(meta["other"], 'a "q" b')
        self.assertEqual(meta["literal"], "one\ntwo")
        self.assertEqual(meta["metadata"], "")
        self.assertEqual(meta["plain"], "first second")

    def test_no_frontmatter(self) -> None:
        self.assertEqual(skillsel.parse_frontmatter("# Title\n"), {})
        self.assertEqual(skillsel.parse_frontmatter("---\nname: x\n"), {})


class InstalledTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)

    def test_installed_reads_all_origins(self) -> None:
        found = skillsel.installed(self.fx.roots())
        self.assertEqual(sorted(found.skills), sorted([
            "test-driven-development", "systematic-debugging", "verification-before-completion", "unlazy",
            "react-best-practices", "superpowers:test-driven-development", "superpowers:requesting-code-review",
            "superpowers:receiving-code-review", "superpowers:writing-plans"]))
        tdd = found.skills["test-driven-development"]
        self.assertEqual(tdd.origin, "project")
        self.assertEqual(tdd.description, "project copy of tdd")
        self.assertEqual(tdd.label, ".claude/skills/test-driven-development/SKILL.md")
        self.assertEqual(len(tdd.digest), 16)

    def test_precedence_project_over_kit_over_user_over_plugin(self) -> None:
        roots = list(reversed(self.fx.roots()))  # precedence comes from origin, not list order
        found = skillsel.installed(roots)
        self.assertEqual(found.skills["test-driven-development"].origin, "project")
        self.assertEqual([copy_.origin for copy_ in found.shadowed["test-driven-development"]], ["kit"])
        (self.fx.project / "test-driven-development" / "SKILL.md").unlink()
        self.assertEqual(skillsel.installed(roots).skills["test-driven-development"].origin, "kit")
        make_skill(self.fx.user, "unlazy", description="user claude copy")
        found = skillsel.installed(self.fx.roots())
        self.assertEqual(found.skills["unlazy"].description, "user claude copy")  # same origin: list order

    def test_plugin_skills_are_namespaced(self) -> None:
        found = skillsel.installed(self.fx.roots())
        skill = found.skills["superpowers:writing-plans"]
        self.assertEqual((skill.name, skill.origin), ("writing-plans", "plugin"))

    def test_unreadable_skills_are_reported(self) -> None:
        bad = self.fx.user / "broken"
        bad.mkdir()
        (bad / "SKILL.md").write_bytes(b"---\nname: \xff\n---\n")
        big = self.fx.user / "big"
        big.mkdir()
        (big / "SKILL.md").write_bytes(b"x" * (skillsel.MAX_SKILL_BYTES + 1))
        found = skillsel.installed(self.fx.roots())
        self.assertNotIn("broken", found.skills)
        self.assertNotIn("big", found.skills)
        self.assertEqual(len(found.issues), 2)
        self.assertTrue(all(issue.startswith("OAH_SKILL_UNREADABLE") for issue in found.issues))

    def test_missing_root_is_skipped(self) -> None:
        roots = [skillsel.SkillRoot(self.fx.base / "nowhere", "user", "~/nowhere")]
        found = skillsel.installed(roots)
        self.assertEqual((found.skills, found.issues), ({}, ()))

    def test_folder_name_when_no_name_field(self) -> None:
        found = skillsel.installed(self.fx.roots())
        self.assertEqual(found.skills["unlazy"].name, "unlazy")


class SelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)

    def select(self, kind: str, stage: str, risk: str, scope, **kwargs) -> skillsel.Selection:
        return skillsel.select(kind, stage, risk, scope, roots=self.fx.roots(), **kwargs)

    def test_selection_table(self) -> None:
        table = [
            ("implementation", "initial", "normal", ["src/app.py"],
             {"test-driven-development", "verification-before-completion"}),
            ("implementation", "initial", "high", ["src/app.py"],
             {"test-driven-development", "verification-before-completion", "superpowers:writing-plans",
              "unlazy"}),
            ("implementation", "correction", "normal", ["src/app.py"],
             {"test-driven-development", "verification-before-completion", "systematic-debugging",
              "superpowers:receiving-code-review"}),
            ("fix", "initial", "normal", ["src/app.py"],
             {"test-driven-development", "verification-before-completion", "systematic-debugging"}),
            ("review", "review", "normal", ["web/page.tsx"],
             {"superpowers:requesting-code-review", "react-best-practices"}),
            ("broad-audit", "review", "normal", ["docs/a.md"],
             {"superpowers:requesting-code-review", "unlazy"}),
            ("docs", "initial", "low", ["README.md"], {"verification-before-completion"}),
            ("quick", "review", "low", ["notes.txt"], {"superpowers:requesting-code-review"}),
        ]
        for kind, stage, risk, scope, expected in table:
            with self.subTest(kind=kind, stage=stage, risk=risk, scope=scope):
                self.assertEqual(ids(self.select(kind, stage, risk, scope)), expected)

    def test_reasons_name_the_criteria(self) -> None:
        selection = self.select("implementation", "initial", "high", ["src/app.py"])
        by_id = {skill.id: skill for skill in selection.skills}
        self.assertTrue(any("kind implementation" in reason for reason in by_id["test-driven-development"].reasons))
        self.assertTrue(any("risk high" in reason for reason in by_id["unlazy"].reasons))
        react = self.select("review", "review", "normal", ["web/page.tsx"])
        reasons = {skill.id: skill.reasons for skill in react.skills}["react-best-practices"]
        self.assertTrue(any("file kinds react" in reason for reason in reasons))

    def test_file_kinds(self) -> None:
        kinds, folders = skillsel.file_kinds(["a.py", "b\\c.TSX", "d.md", "src", "repo", "x.unknown"],
                                             skillsel.DEFAULTS["file_kinds"])
        self.assertEqual(kinds, ("markdown", "python", "react"))
        self.assertEqual(folders, ("src", "repo"))

    def test_folders_expand_with_repo_root(self) -> None:
        (self.fx.repo / "web").mkdir(parents=True)
        (self.fx.repo / "web" / "page.tsx").write_text("x", encoding="utf-8")
        without = self.select("review", "review", "normal", ["web"])
        self.assertNotIn("react-best-practices", ids(without))
        self.assertTrue(any("folder" in note for note in without.notes))
        expanded = self.select("review", "review", "normal", ["web"], repo_root=self.fx.repo)
        self.assertIn("react-best-practices", ids(expanded))
        whole = self.select("review", "review", "normal", ["repo"], repo_root=self.fx.repo)
        self.assertIn("react-best-practices", ids(whole))

    def test_empty_scope_warns_and_still_selects(self) -> None:
        selection = self.select("implementation", "initial", "normal", [])
        self.assertTrue(any(warning.startswith("OAH_SCOPE_EMPTY") for warning in selection.warnings))
        self.assertEqual(ids(selection), {"test-driven-development", "verification-before-completion"})
        self.assertIn("OAH_SCOPE_EMPTY", selection.brief_text())

    def test_nothing_selected_is_stated(self) -> None:
        empty_roots = [skillsel.SkillRoot(self.fx.base / "none", "user", "~/none")]
        selection = skillsel.select("implementation", "initial", "normal", [], roots=empty_roots)
        self.assertEqual(selection.skills, ())
        text = selection.brief_text()
        self.assertIn("No installed skill was selected", text)
        self.assertIn("test-driven-development", text)  # named as not installed
        self.assertTrue(any(warning.startswith("OAH_SKILLS_NONE_SELECTED") for warning in selection.warnings))

    def test_unavailable_rule_is_reported(self) -> None:
        (self.fx.agents / "unlazy" / "SKILL.md").unlink()
        selection = self.select("implementation", "initial", "high", ["a.py"])
        self.assertNotIn("unlazy", ids(selection))
        self.assertIn("unlazy", [item.skill for item in selection.unavailable])
        self.assertIn("unlazy", selection.brief_text())

    def test_exclude(self) -> None:
        settings = copy.deepcopy(skillsel.DEFAULTS)
        settings["exclude"] = ["superpowers:writing-plans", "unlazy"]
        selection = self.select("implementation", "initial", "high", ["a.py"], settings=settings)
        self.assertEqual(ids(selection), {"test-driven-development", "verification-before-completion"})
        self.assertEqual({item.reason for item in selection.unavailable}, {"excluded by [skills] exclude"})

    def test_user_rules_from_config(self) -> None:
        (self.fx.repo / ".oah").mkdir(parents=True)
        (self.fx.repo / ".oah" / "config.toml").write_text(
            '[skills]\nmode = "reference"\n\n[[skills.extra_rules]]\nskill = "systematic-debugging"\n'
            'kinds = ["docs"]\nreason = "we debug docs too"\n', encoding="utf-8")
        settings = load_config(self.fx.repo).section("skills", skillsel.DEFAULTS)
        selection = self.select("docs", "initial", "low", ["a.md"], settings=settings)
        self.assertIn("systematic-debugging", ids(selection))
        reasons = {skill.id: skill.reasons for skill in selection.skills}["systematic-debugging"]
        self.assertIn("we debug docs too (kind docs)", reasons)

    def test_invalid_rule_is_skipped_with_warning(self) -> None:
        settings = copy.deepcopy(skillsel.DEFAULTS)
        settings["extra_rules"] = [{"skill": 3}, "nope", {"skill": "unlazy", "kinds": "docs"}]
        selection = self.select("docs", "initial", "low", ["a.md"], settings=settings)
        invalid = [warning for warning in selection.warnings if warning.startswith("OAH_SKILLS_RULE_INVALID")]
        self.assertEqual(len(invalid), 3)

    def test_attach_mode_text(self) -> None:
        settings = dict(skillsel.DEFAULTS, mode="attach")
        selection = self.select("docs", "initial", "low", ["a.md"], settings=settings)
        (skill,) = selection.skills
        self.assertIn("evidence first", skill.text)
        text = selection.brief_text()
        self.assertIn("Body text.", text)

    def test_reference_mode_has_no_text(self) -> None:
        selection = self.select("docs", "initial", "low", ["a.md"])
        (skill,) = selection.skills
        self.assertIsNone(skill.text)
        self.assertIn("~/.claude/skills/verification-before-completion/SKILL.md", selection.brief_text())
        self.assertNotIn("Body text.", selection.brief_text())

    def test_attach_over_cap_falls_back_to_reference(self) -> None:
        make_skill(self.fx.user, "verification-before-completion", body="y" * 5000)
        settings = dict(skillsel.DEFAULTS, mode="attach", attach_max_bytes=1000)
        selection = self.select("docs", "initial", "low", ["a.md"], settings=settings)
        (skill,) = selection.skills
        self.assertIsNone(skill.text)
        self.assertTrue(any("too long to attach" in note for note in selection.notes))

    def test_reads_installed_version_every_time(self) -> None:
        settings = dict(skillsel.DEFAULTS, mode="attach")
        first = self.select("docs", "initial", "low", ["a.md"], settings=settings).skills[0]
        make_skill(self.fx.user, "verification-before-completion", description="evidence first",
                   body="Updated body.\n")
        second = self.select("docs", "initial", "low", ["a.md"], settings=settings).skills[0]
        self.assertNotEqual(first.digest, second.digest)
        self.assertIn("Updated body.", second.text)

    def test_crlf_skill_file(self) -> None:
        make_skill(self.fx.user, "verification-before-completion", description="crlf ok", newline="\r\n")
        found = skillsel.installed(self.fx.roots())
        self.assertEqual(found.skills["verification-before-completion"].description, "crlf ok")

    def test_selection_is_read_only(self) -> None:
        def snapshot() -> dict:
            return {str(path): (path.stat().st_mtime_ns, path.read_bytes())
                    for path in sorted(self.fx.base.rglob("*")) if path.is_file()}
        before = snapshot()
        self.select("implementation", "correction", "high", ["a.tsx"], settings=dict(skillsel.DEFAULTS, mode="attach"))
        self.assertEqual(snapshot(), before)

    def test_brief_text_c6(self) -> None:
        for kind, stage, risk in (("implementation", "initial", "normal"), ("review", "review", "high")):
            with self.subTest(kind=kind):
                text = self.select(kind, stage, risk, ["a.py"]).brief_text()
                self.assertIn(f"kind `{kind}`", text)
                self.assertIn(f"stage `{stage}`", text)
                self.assertIn(f"risk `{risk}`", text)
                self.assertIn("installed", text)
                lowered = text.lower()
                for promise in ("no other skill", "no optional skill", "zero optional", "only these skills",
                                "do not use other skills", "don't use other skills"):
                    self.assertNotIn(promise, lowered)

    def test_record_shape(self) -> None:
        record = self.select("docs", "initial", "low", ["a.md"]).to_record()
        self.assertEqual(record["kind"], "docs")
        self.assertEqual(record["scope_count"], 1)
        self.assertEqual(record["file_kinds"], ["markdown"])
        self.assertEqual([skill["id"] for skill in record["skills"]], ["verification-before-completion"])
        self.assertEqual(sorted(record["skills"][0]), ["digest", "id", "origin", "reasons"])


class TelemetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)
        self.state = self.fx.base / "state-dir"

    def test_telemetry_appends_locally(self) -> None:
        for _ in range(2):
            skillsel.select("docs", "initial", "low", [], roots=self.fx.roots(), state_dir=self.state, now=NOW)
        lines = (self.state / skillsel.TELEMETRY_FILE).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        record = json.loads(lines[0])
        self.assertEqual(record["at"], "2026-10-23T12:00:00Z")
        self.assertEqual(record["kind"], "docs")
        self.assertEqual(record["warnings"], ["OAH_SCOPE_EMPTY"])
        self.assertEqual([skill["id"] for skill in record["skills"]], ["verification-before-completion"])
        self.assertNotIn(str(self.fx.home), lines[0])

    def test_telemetry_off(self) -> None:
        settings = dict(skillsel.DEFAULTS, telemetry=False)
        skillsel.select("docs", "initial", "low", ["a.md"], roots=self.fx.roots(), settings=settings,
                        state_dir=self.state)
        self.assertFalse((self.state / skillsel.TELEMETRY_FILE).exists())

    def test_no_state_dir_writes_nothing(self) -> None:
        skillsel.select("docs", "initial", "low", ["a.md"], roots=self.fx.roots())
        self.assertFalse(self.state.exists())

    def test_corrupt_telemetry_warns_and_keeps_bytes(self) -> None:
        self.state.mkdir()
        path = self.state / skillsel.TELEMETRY_FILE
        path.write_bytes(b"{torn")
        selection = skillsel.select("docs", "initial", "low", ["a.md"], roots=self.fx.roots(),
                                    state_dir=self.state)
        self.assertEqual(ids(selection), {"verification-before-completion"})
        self.assertTrue(any(w.startswith("OAH_SKILLS_TELEMETRY_FAILED") for w in selection.warnings))
        self.assertEqual(path.read_bytes(), b"{torn")


class DefaultRootsTest(unittest.TestCase):
    def test_cache_references_identify_installed_file(self) -> None:
        base = make_tmp_dir(self)
        kit = base / "state" / "skills-cache"
        path = make_skill(kit, "verification-before-completion", body="x" * 2000)
        roots = skillsel.default_roots(base / "repo", home=base / "home", env={}, kit_cache=kit)
        for mode in ("reference", "attach"):
            with self.subTest(mode=mode):
                selection = skillsel.select("docs", "initial", "low", ["a.md"], roots=roots,
                                            settings=dict(skillsel.DEFAULTS, mode=mode, attach_max_bytes=100))
                (skill,) = selection.skills
                self.assertEqual(pathlib.Path(skill.label), path)
                self.assertTrue(pathlib.Path(skill.label).is_file())
                self.assertIn(skill.label, selection.brief_text())
                self.assertIsNone(skill.text)
                if mode == "attach":
                    self.assertTrue(any(skill.label in note for note in selection.notes))

    def test_default_roots_layout(self) -> None:
        base = make_tmp_dir(self)
        repo, home = base / "repo", base / "home"
        plugin_dir = home / ".claude" / "plugins" / "cache" / "m" / "superpowers" / "2.0"
        other_dir = home / ".claude" / "plugins" / "cache" / "m" / "local-only" / "1.0"
        mine_dir = home / ".claude" / "plugins" / "cache" / "m" / "mine" / "1.0"
        manifest = {"version": 2, "plugins": {
            "superpowers@m": [{"scope": "user", "installPath": str(plugin_dir)}],
            "local-only@m": [{"scope": "project", "installPath": str(other_dir),
                              "projectPath": str(base / "elsewhere")}],
            "mine@m": [{"scope": "local", "installPath": str(mine_dir), "projectPath": str(repo)}],
        }}
        (home / ".claude" / "plugins").mkdir(parents=True)
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(manifest), encoding="utf-8")
        kit = base / "kit"
        roots = skillsel.default_roots(repo, home=home, env={}, kit_cache=kit)
        table = [(root.origin, root.label, root.plugin) for root in roots]
        self.assertEqual(table, [
            ("project", ".claude/skills", ""),
            ("project", ".agents/skills", ""),
            ("kit", kit.as_posix(), ""),
            ("user", "~/.claude/skills", ""),
            ("user", "~/.codex/skills", ""),
            ("user", "~/.agents/skills", ""),
            ("plugin", "~/.claude/plugins/cache/m/superpowers/2.0/skills", "superpowers"),
            ("plugin", "~/.claude/plugins/cache/m/mine/1.0/skills", "mine"),
        ])
        self.assertEqual(roots[0].path, repo / ".claude" / "skills")
        self.assertEqual(roots[6].path, plugin_dir / "skills")

    def test_default_roots_env_overrides(self) -> None:
        base = make_tmp_dir(self)
        roots = skillsel.default_roots(base / "repo", home=base / "home",
                                       env={"CLAUDE_CONFIG_DIR": str(base / "cc"), "CODEX_HOME": str(base / "cx")})
        paths = {root.label: root.path for root in roots}
        self.assertEqual(paths[str(base / "cc" / "skills")], base / "cc" / "skills")
        self.assertEqual(paths[str(base / "cx" / "skills")], base / "cx" / "skills")

    def test_bad_manifest_gives_no_plugins(self) -> None:
        base = make_tmp_dir(self)
        (base / "home" / ".claude" / "plugins").mkdir(parents=True)
        (base / "home" / ".claude" / "plugins" / "installed_plugins.json").write_text("{", encoding="utf-8")
        roots = skillsel.default_roots(base / "repo", home=base / "home", env={})
        self.assertEqual([root for root in roots if root.origin == "plugin"], [])


class TemplateTest(unittest.TestCase):
    def test_enablement_discloses_both_request_types(self) -> None:
        text = resources.files("oah").joinpath("templates/skills/config.toml").read_text(encoding="utf-8")
        disclosure = " ".join(text.split("[rerank]\n", 1)[1].split("enabled =", 1)[0].split())
        for field in ("task kind", "query", "name", "description", "200", "difficulty advice",
                      "kind", "intensity", "reasoning", "risk", "task summary", "500", "charge"):
            self.assertIn(field, disclosure)

    def test_template_parses_and_validates(self) -> None:
        text = resources.files("oah").joinpath("templates/skills/config.toml").read_text(encoding="utf-8")
        tomllib.loads(text)
        root = make_tmp_dir(self)
        (root / ".oah").mkdir()
        (root / ".oah" / "config.toml").write_text(text, encoding="utf-8")
        loaded = load_config(root)
        skills = loaded.section("skills", skillsel.DEFAULTS)
        section = loaded.section("rerank", rerank.DEFAULTS)
        self.assertIs(section["enabled"], False)
        self.assertEqual(skills["mode"], "reference")
        self.assertIn("${TYPESAFE_API_KEY}", text)


if __name__ == "__main__":
    unittest.main()
