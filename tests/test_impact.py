# 영향 지도 검사 — 파일 종류 분류, 어휘 바늘 매칭, 명령 결속 판정, git grep, 질의 순위·상한·게이트·범위 경고를 본다.
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

from oah.impact import gatebind, kinds, lexical, query
from oah.impact.defaults import effective
from tests.helpers import git_commit, git_env, make_git_repo
from tests.test_impact_map import run_impact

FIXTURE_REPO = pathlib.Path(__file__).resolve().parent / "fixtures" / "impact" / "repo"


def fixture_repo(test: unittest.TestCase) -> pathlib.Path:
    """A temporary git repository holding a committed copy of tests/fixtures/impact/repo."""
    repo = make_git_repo(test)
    shutil.copytree(FIXTURE_REPO, repo, dirs_exist_ok=True)
    git_commit(repo, {})
    return repo


def row(binding="link", layer="lexical", source="a.md", line=1, target="t.py", score=1, detail="path"):
    return query.Row(binding, layer, "doc", source, line, target, detail, score)


class KindsTest(unittest.TestCase):
    def test_classify_kinds(self):
        cases = {
            ".github/workflows/ci.yml": "workflow",
            "Makefile": "workflow",
            "db/migrations/001_init.sql": "migration",
            "schema/extra.sql": "migration",
            "tests/test_a.py": "test",
            "pkg/a_test.go": "test",
            "web/app.spec.ts": "test",
            "pyproject.toml": "manifest",
            "web/package.json": "manifest",
            "requirements-dev.txt": "manifest",
            "conf/app.yaml": "config",
            "settings.toml": "config",
            "README.md": "doc",
            "docs/guide/intro.html": "doc",
            "src/oah/cli.py": "code",
        }
        for path, kind in cases.items():
            with self.subTest(path=path):
                self.assertEqual(kinds.classify(path), kind)

    def test_glob_double_star(self):
        self.assertTrue(kinds.glob_match("**/*.sql", "a.sql"))
        self.assertTrue(kinds.glob_match("**/*.sql", "a/b/c.sql"))
        self.assertTrue(kinds.glob_match("docs/**", "docs/a/b.md"))
        self.assertFalse(kinds.glob_match("docs/**", "docsx/a.md"))
        self.assertFalse(kinds.glob_match("*.md", "a/b.md"))
        self.assertTrue(kinds.glob_match("test_?.py", "test_a.py"))
        self.assertFalse(kinds.glob_match("**/*.SQL", "a.sql"), "matching is case-sensitive")


class LexicalTest(unittest.TestCase):
    def test_needles_skip_short_parts(self):
        self.assertEqual(lexical.needles_for("src/oah/impact.py"),
                         {"src/oah/impact.py": "path", "impact.py": "basename", "impact": "stem"})
        self.assertEqual(lexical.needles_for("a.c"), {"a.c": "path"})
        self.assertEqual(lexical.needles_for("x/ab.py"), {"x/ab.py": "path", "ab.py": "basename"})
        self.assertEqual(lexical.needles_for("Makefile"), {"Makefile": "path"})
        self.assertEqual(lexical.needles_for("bad\nname.py"), {})

    def test_find_needles_boundaries(self):
        needles = {"src/oah/impact.py", "impact.py", "impact", "PLAN.md"}
        self.assertEqual(lexical.find_needles("see src/oah/impact.py:12 now", needles),
                         {"src/oah/impact.py", "impact.py", "impact"})
        self.assertEqual(lexical.find_needles("run ./x/src/oah/impact.py.", needles),
                         {"src/oah/impact.py", "impact.py", "impact"})
        self.assertEqual(lexical.find_needles(r"open src\oah\impact.py", needles),
                         {"src/oah/impact.py", "impact.py", "impact"})
        self.assertEqual(lexical.find_needles("from oah import impact_cli", needles), set())
        self.assertEqual(lexical.find_needles("the impactful thing", needles), set())
        self.assertEqual(lexical.find_needles("see [[impact]] and `docs/PLAN.md`", needles),
                         {"impact", "PLAN.md"})

    def test_find_identifiers_case(self):
        names = {"users", "audit_log"}
        self.assertEqual(lexical.find_identifiers('db.query("SELECT * FROM Users")', names), {"users"})
        self.assertEqual(lexical.find_identifiers("public.audit_log.id", names), {"audit_log"})
        self.assertEqual(lexical.find_identifiers("superusers and audit_logs", names), set())

    def test_classify_command_pin_exec(self):
        cases = [
            ("run: python3 tools/x.py --flag", "tools/x.py", "exec"),
            ("python -X utf8 tools/x.py", "x.py", "exec"),
            ("./tools/x.py arg", "tools/x.py", "exec"),
            ("- tools/x.py", "tools/x.py", "exec"),
            ('"test": "node scripts/check.js"', "scripts/check.js", "exec"),
            ("powershell -File tools\\x.ps1", "tools/x.ps1", "exec"),
            ("sha256sum tools/x.py", "tools/x.py", "pin"),
            ("git hash-object tools/x.py", "tools/x.py", "pin"),
            ("grep -c needle tools/x.py && echo ok", "tools/x.py", "pin"),
            ("echo ok && grep -q x tools/x.py", "tools/x.py", "pin"),
            ("python tools/y.py | grep tools/x.py", "tools/x.py", "pin"),
            ("python tools/x.py | grep ok", "tools/x.py", "exec"),
            ("see tools/x.py for details", "tools/x.py", None),
            ("python tools/other.py", "tools/x.py", None),
        ]
        for text, needle, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(lexical.classify_command(text, needle), expected)

    def test_grep_reads_ref_not_worktree(self):
        repo = make_git_repo(self)
        first = git_commit(repo, {"a b/doc.md": "see x.py here\r\nnothing\n", "x.py": "x\n"})
        git_commit(repo, {"a b/doc.md": "nothing\n"})
        (repo / "late.md").write_text("x.py uncommitted\n", encoding="utf-8")
        self.assertEqual(lexical.grep(repo, first, ["x.py"]), [("a b/doc.md", 1, "see x.py here")])
        self.assertEqual(lexical.grep(repo, "HEAD", ["x.py"]), [])
        self.assertEqual(lexical.grep(repo, first, ["X.PY"], ignore_case=True),
                         [("a b/doc.md", 1, "see x.py here")])
        self.assertEqual(lexical.grep(repo, first, ["x.py"], paths=["x.py"]), [])
        self.assertEqual(lexical.grep(repo, first, []), [])



def data_with(files: dict, hits: dict | None = None, history: list | None = None) -> dict:
    return {"files": files, "hits": hits or {}, "db": None, "db_hits": {}, "history": history or []}


class QueryTest(unittest.TestCase):
    def test_rank_order(self):
        rows = [row("mention", "cochange", "c.py", score=9), row("link", "wiki", "w.md"),
                row("exec", "lexical", "ci.yml"), row("link", "lexical", "b.md", score=1),
                row("link", "lexical", "a.md", score=3), row("pin", "gates", "G#G1", line=None),
                row("exec", "gates", "G#G2", line=None)]
        ranked = [(r.binding, r.layer, r.source) for r in query.rank(rows)]
        self.assertEqual(ranked, [("pin", "gates", "G#G1"), ("exec", "gates", "G#G2"), ("exec", "lexical", "ci.yml"),
                                  ("link", "lexical", "a.md"), ("link", "lexical", "b.md"), ("link", "wiki", "w.md"),
                                  ("mention", "cochange", "c.py")])

    def test_weak_rows_capped_strong_kept(self):
        rows = [row("exec", source=f"s{i}") for i in range(3)] + [row("mention", source=f"m{i:02}") for i in range(10)]
        shown, total, overflow = query.cap(query.rank(rows), limit=4, strong_only=False)
        self.assertEqual((len(shown), total, overflow), (7, 13, 6))
        self.assertEqual([r.binding for r in shown].count("exec"), 3)
        shown, total, overflow = query.cap(query.rank(rows), limit=0, strong_only=True)
        self.assertEqual((len(shown), total, overflow), (3, 3, 0))
        self.assertEqual(query.count_line(total, len(shown), overflow), "total=3 shown=3 overflow=0")

    def test_lexical_bindings_and_wiki_layer(self):
        files = {"tools/check.py": "code", ".github/workflows/ci.yml": "workflow", "docs/guide.md": "doc",
                 "docs/wiki/check.md": "doc"}
        hits = {"tools/check.py": [[".github/workflows/ci.yml", 2, "exec"], [".github/workflows/ci.yml", 4, ""],
                                   ["docs/guide.md", 1, ""]],
                "check": [["docs/wiki/check.md", 3, "wikilink"], ["docs/guide.md", 5, ""]],
                "check.py": [["tools/check.py", 1, ""]]}
        rows = query.query(data_with(files, hits), ["tools/check.py"], settings=effective(None))
        found = {(r.binding, r.layer, r.source, r.line) for r in rows}
        self.assertEqual(found, {("exec", "lexical", ".github/workflows/ci.yml", 2),
                                 ("link", "lexical", "docs/guide.md", 1),
                                 ("link", "wiki", "docs/wiki/check.md", 3)})

    def test_ambiguous_names_skipped(self):
        files = {f"pkg{i}/util.py": "code" for i in range(4)}
        files["docs/a.md"] = "doc"
        hits = {"util.py": [["docs/a.md", 1, ""]], "pkg0/util.py": [["docs/a.md", 2, ""]]}
        rows = query.query(data_with(files, hits), ["pkg0/util.py"], settings=effective(None))
        self.assertEqual([(r.binding, r.line) for r in rows], [("link", 2)])
        rows = query.query(data_with(files, hits), ["pkg0/util.py"], settings=effective({"max_ambiguity": 4}))
        self.assertEqual(sorted((r.binding, r.line) for r in rows), [("link", 2)],
                         "one row per source, keeping the strongest line")

    def test_cochange_layer(self):
        files = {"a.py": "code", "b.py": "code", "c.py": "code", "gone.py": "code"}
        history = [["s1", ["a.py", "b.py"]], ["s2", ["a.py", "b.py", "c.py"]], ["s3", ["c.py"]],
                   ["s4", ["a.py", "gone2.py"]]]
        rows = query.query(data_with(files, history=history), ["a.py"], settings=effective(None))
        self.assertEqual([(r.layer, r.source, r.score, r.detail) for r in rows],
                         [("cochange", "b.py", 2, "changed together in 2 of 3 commits"),
                          ("cochange", "c.py", 1, "changed together in 1 of 3 commits")])
        rows = query.query(data_with(files, history=history), ["a.py"],
                           settings=effective({"cochange_max_files": 2}))
        self.assertEqual([r.source for r in rows], ["b.py"], "commits with more files are not evidence")

    def test_graph_neighbours_optional(self):
        repo = make_git_repo(self)
        settings = effective(None)
        self.assertIsNone(query.load_graph(repo, settings))
        (repo / "graphify-out").mkdir()
        (repo / "graphify-out" / "graph.json").write_text("{broken", encoding="utf-8")
        self.assertIsNone(query.load_graph(repo, settings))
        graph = {"nodes": [{"id": "n1", "source_file": "a.py"}, {"id": "n2", "source_file": "b.py"},
                           {"id": "n3", "file": "c.py"}, {"id": "n4"}],
                 "links": [{"source": "n1", "target": "n2", "relation": "calls"}, {"source": "n3", "target": "n1"},
                           {"source": "n1", "target": "n4"}]}
        (repo / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
        loaded = query.load_graph(repo, settings)
        files = {"a.py": "code", "b.py": "code", "c.py": "code"}
        rows = query.query(data_with(files), ["a.py"], graph=loaded, settings=settings)
        self.assertEqual(sorted((r.layer, r.source, r.binding) for r in rows),
                         [("graph", "b.py", "mention"), ("graph", "c.py", "mention")])

    def test_gate_binding_pin_exec(self):
        gates = [gatebind.GateRef(".oah/tasks/T/GATES.md", "G1", "python3 tools/check.py", "OK"),
                 gatebind.GateRef(".oah/tasks/T/GATES.md", "G2", "sha256sum tools/check.py", "abc"),
                 gatebind.GateRef(".oah/tasks/T/GATES.md", "G3", "cat build.log", "tools/check.py"),
                 gatebind.GateRef(".oah/tasks/T/GATES.md", "G4", "see tools/check.py notes", "x"),
                 gatebind.GateRef(".oah/tasks/T/GATES.md", "G5", "python3 other.py", "check")]
        rows = gatebind.rows(gates, "tools/check.py", min_needle=3)
        self.assertEqual([(r.source, r.binding, r.detail) for r in rows],
                         [(".oah/tasks/T/GATES.md#G1", "exec", "binding=exec"),
                          (".oah/tasks/T/GATES.md#G2", "pin", "binding=pin"),
                          (".oah/tasks/T/GATES.md#G3", "pin", "binding=pin"),
                          (".oah/tasks/T/GATES.md#G4", "link", "binding=link")])
        found, status = gatebind.discover(pathlib.Path("."))
        self.assertEqual(found, [])
        self.assertTrue(status.startswith("unavailable"))

    def test_rerun_plan_strong_only(self):
        rows = [row("exec", "gates", ".oah/tasks/T/GATES.md#G1", line=None, target="a.py"),
                row("exec", "gates", ".oah/tasks/T/GATES.md#G1", line=None, target="b.py"),
                row("pin", "lexical", "ci.yml", line=3, target="a.py"),
                row("link", "gates", ".oah/tasks/T/GATES.md#G2", line=None, target="a.py"),
                row("mention", "lexical", "x.md", target="a.py")]
        self.assertEqual(query.rerun_plan(rows), [
            {"source": "ci.yml", "line": 3, "binding": "pin", "targets": ["a.py"]},
            {"source": ".oah/tasks/T/GATES.md#G1", "line": None, "binding": "exec", "targets": ["a.py", "b.py"]}])

    def test_empty_scope_warns(self):
        files = {"src/a.py": "code", "db/1.sql": "migration", "docs/x.md": "doc"}
        selected, warnings = query.select_scope([], files)
        self.assertEqual(selected, sorted(files))
        self.assertTrue(warnings and warnings[0].startswith("OAH_SCOPE_EMPTY"))
        selected, warnings = query.select_scope(["src"], files, kinds=["migration"])
        self.assertEqual(selected, [])
        self.assertTrue(warnings and warnings[0].startswith("OAH_SCOPE_NO_MATCH"))
        selected, warnings = query.select_scope(["repo"], files, kinds=["migration"])
        self.assertEqual((selected, warnings), (["db/1.sql"], []))
        selected, warnings = query.select_scope(["SRC/"], files)
        self.assertEqual((selected, warnings), (["src/a.py"], []))

    def test_rerank_status(self):
        rows = [row("mention", source="b"), row("link", source="a")]
        self.assertEqual(query.apply_rerank(rows)[1], "unused(not-enabled)")
        self.assertEqual(query.apply_rerank(rows)[0], rows, "an unused re-ranker never changes the rows")
        self.assertEqual(query.apply_rerank(rows, enabled=True)[1], "unused(not-configured)",
                         "enabled, but no provider is installed")
        self.assertEqual(query.apply_rerank(rows, disabled=True, enabled=True)[1], "unused(disabled-by-brief)")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.repo = fixture_repo(self)

    def test_cli_query_text(self):
        result = run_impact(self.repo, "tools/check.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "impact: tools/check.py")
        self.assertRegex(lines[1], r"\Amap: commit=[0-9a-f]{40} manifest=[0-9a-f]{12} tool=[0-9a-f]{12} "
                                   r"result=[0-9a-f]{12} \(built\)\Z")
        self.assertIn("rerank=unused(not-enabled)", lines)
        self.assertTrue(any(line.startswith("gates: unavailable") for line in lines))
        self.assertTrue(any(line.startswith("db-impact: commit=") for line in lines))
        self.assertRegex(result.stdout, r"exec\s+lexical\s+workflow\s+\.github/workflows/ci\.yml:2\s+tools/check\.py")
        self.assertRegex(result.stdout, r"link\s+lexical\s+doc\s+docs/guide\.md:1\s+tools/check\.py")
        self.assertRegex(lines[-1], r"\Atotal=\d+ shown=\d+ overflow=0\Z")
        again = run_impact(self.repo, "tools/check.py")
        self.assertIn("(fresh)", again.stdout.splitlines()[1])

    def test_cli_json(self):
        result = run_impact(self.repo, "src/app.py", "--json", "--no-rerank")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["targets"], ["src/app.py"])
        self.assertEqual(report["rerank"], "unused(disabled-by-brief)")
        self.assertEqual(report["map"]["action"], "built")
        self.assertEqual((report["total"], report["shown"], report["overflow"]), (len(report["rows"]),) * 2 + (0,))
        wiki = [r for r in report["rows"] if r["layer"] == "wiki"]
        self.assertEqual([(r["source"], r["binding"]) for r in wiki], [("docs/wiki/app.md", "link")])

    def test_cli_strong_only_fixture(self):
        result = run_impact(self.repo, "tools/check.py", "src/lib/util.py", "--strong-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line.split() for line in result.stdout.splitlines() if line.startswith(("pin ", "exec "))]
        self.assertEqual(sorted((r[0], r[3], r[4]) for r in rows), [
            ("exec", ".github/workflows/ci.yml:2", "tools/check.py"),
            ("exec", "Makefile:2", "tools/check.py"),
            ("pin", ".github/workflows/ci.yml:3", "src/lib/util.py"),
            ("pin", "Makefile:2", "src/lib/util.py"),
        ])
        self.assertEqual(result.stdout.splitlines()[-1], "total=4 shown=4 overflow=0")
        self.assertNotIn("docs/guide.md", result.stdout)

    def test_cli_changed_since(self):
        base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()
        git_commit(self.repo, {"tools/check.py": "print('check v2')\n"})
        result = run_impact(self.repo, "--changed-since", base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("changed since " + base[:12] + ": 1 file", result.stdout)
        self.assertIn("re-run: .github/workflows/ci.yml:2 (exec tools/check.py)", result.stdout)
        self.assertIn("re-run: Makefile:2 (exec tools/check.py)", result.stdout)
        report = json.loads(run_impact(self.repo, "--changed-since", base, "--json").stdout)
        self.assertEqual(report["changed_since"]["files"], ["tools/check.py"])
        self.assertEqual([item["source"] for item in report["changed_since"]["rerun"]],
                         [".github/workflows/ci.yml", "Makefile"])

    def test_cli_preserves_each_strong_command_location(self):
        workflow = ".github/workflows/ci.yml"
        base = git_commit(self.repo, {workflow: "steps:\n  - run: python tools/check.py\n\n"
                                               "  - run: sha256sum tools/check.py\n\n"
                                               "  - run: python tools/check.py --again\n"})
        (self.repo / "tools" / "check.py").write_text("print('changed')\n", encoding="utf-8")
        result = run_impact(self.repo, "--changed-since", base, "--strong-only", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        rows = [(r["line"], r["binding"]) for r in report["rows"] if r["source"] == workflow]
        self.assertEqual(sorted(rows), [(2, "exec"), (4, "pin"), (6, "exec")])
        plan = [(r["line"], r["binding"]) for r in report["changed_since"]["rerun"]
                if r["source"] == workflow]
        self.assertEqual(sorted(plan), sorted(rows), "alternate needles must not duplicate command locations")

    def test_cli_ref(self):
        first = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                               text=True).stdout.strip()
        git_commit(self.repo, {".github/workflows/ci.yml": "steps: []\n"})
        result = run_impact(self.repo, "tools/check.py", "--ref", first, "--strong-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"map: commit={first}", result.stdout)
        self.assertIn("(ephemeral)", result.stdout)
        self.assertIn(".github/workflows/ci.yml:2", result.stdout)
        now = run_impact(self.repo, "tools/check.py", "--strong-only")
        self.assertNotIn(".github/workflows/ci.yml", now.stdout)

    def test_cli_usage_exit_1(self):
        for args, code in [((), "OAH_IMPACT_USAGE"), (("../outside.py",), "OAH_IMPACT_PATH"),
                           (("x.py", "--ref", "no-such-ref"), "OAH_IMPACT_BAD_REF"),
                           (("--changed-since", "no-such-ref"), "OAH_IMPACT_BAD_REF"),
                           (("db_table:nope",), "OAH_IMPACT_UNKNOWN_ENTITY"),
                           (("--task", "missing"), "OAH_IMPACT_NO_TASK")]:
            with self.subTest(args=args):
                result = run_impact(self.repo, *args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(code, result.stderr)

    def test_cli_argparse_usage_exit_2(self):
        for args in (("--bogus",), ("--ref",)):
            with self.subTest(args=args):
                result = run_impact(self.repo, *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage:", result.stderr)

    def test_cli_outside_repo_exit_69(self):
        outside = make_git_repo(self).parent / "plain"
        outside.mkdir()
        result = run_impact(outside, "x.py", env_extra={"GIT_CEILING_DIRECTORIES": str(outside.parent)})
        self.assertEqual(result.returncode, 69)
        self.assertIn("OAH_NOT_A_REPO", result.stderr)

    def test_cli_task_scope(self):
        env = dict(git_env())
        start = subprocess.run([*_oah_module(), "start", "--task", "t1", "--agent", "claude", "--scope", "tools"],
                               cwd=self.repo, capture_output=True, text=True, encoding="utf-8",
                               env=dict(env, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent / "src")))
        self.assertEqual(start.returncode, 0, start.stderr)
        result = run_impact(self.repo, "--task", "t1", "--strong-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("impact: tools/check.py", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "total=2 shown=2 overflow=0")
        none = run_impact(self.repo, "--task", "t1", "--kind", "migration")
        self.assertEqual(none.returncode, 0, none.stderr)
        self.assertIn("OAH_SCOPE_NO_MATCH", none.stderr)
        self.assertEqual(none.stdout.splitlines()[-1], "total=0 shown=0 overflow=0")

    def test_cli_reads_impact_config(self):
        (self.repo / ".oah").mkdir()
        config = self.repo / ".oah" / "config.toml"
        config.write_text("[impact]\nlimit = 1\n", encoding="utf-8")
        result = run_impact(self.repo, "tools/check.py", "src/lib/util.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        last = result.stdout.splitlines()[-1]
        self.assertRegex(last, r"\Atotal=\d+ shown=5 overflow=[1-9]\d*\Z", "4 exec/pin rows plus 1 weaker row")
        for text, code in [("[impact]\nlimitt = 1\n", "OAH_CONFIG_UNKNOWN_KEY"),
                           ('[impact]\nlimit = "many"\n', "OAH_CONFIG_INVALID"),
                           ("[impact]\nkinds = [[\"doc\"]]\n", "OAH_CONFIG_INVALID"),
                           ("[impact]\nmin_needle = 0\n", "OAH_CONFIG_INVALID"),
                           ('[impact]\nsql_dialect = "auto"\n', "OAH_CONFIG_INVALID"),
                           ('[impact]\nsql_dialect = []\n', "OAH_CONFIG_INVALID")]:
            config.write_text(text, encoding="utf-8")
            with self.subTest(config=text):
                bad = run_impact(self.repo, "tools/check.py")
                self.assertEqual(bad.returncode, 69, bad.stderr)
                self.assertIn(code, bad.stderr)

    def test_cli_rerank_needs_explicit_enable(self):
        (self.repo / ".oah").mkdir()
        (self.repo / ".oah" / "config.toml").write_text("[impact]\nrerank = true\n", encoding="utf-8")
        result = run_impact(self.repo, "tools/check.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rerank=unused(not-configured)", result.stdout.splitlines())
        brief = run_impact(self.repo, "tools/check.py", "--no-rerank")
        self.assertIn("rerank=unused(disabled-by-brief)", brief.stdout.splitlines())

    def test_cli_answers_from_working_tree(self):
        (self.repo / "docs" / "notes.md").write_text("Run tools/check.py first.\n", encoding="utf-8")
        result = run_impact(self.repo, "tools/check.py", "--json")
        sources = {row["source"] for row in json.loads(result.stdout)["rows"] if row["layer"] == "lexical"}
        self.assertIn("docs/notes.md", sources, "untracked files are inputs")
        (self.repo / "docs" / "guide.md").write_text("Nothing here.\n", encoding="utf-8")
        again = json.loads(run_impact(self.repo, "tools/check.py", "--json").stdout)
        self.assertEqual(again["map"]["action"], "refreshed")
        self.assertNotIn("docs/guide.md", {row["source"] for row in again["rows"] if row["layer"] == "lexical"},
                         "dirty edits are inputs")

    def test_cli_build_refresh_status(self):
        built = run_impact(self.repo, "build")
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertRegex(built.stdout, r"\AOAH_IMPACT_BUILT commit=[0-9a-f]{40} ")
        git_commit(self.repo, {"src/new.py": "n = 1\n"})
        status = run_impact(self.repo, "status")
        self.assertIn("map: stale (HEAD moved", status.stdout)
        self.assertIn("oah impact refresh", status.stdout)
        refreshed = run_impact(self.repo, "refresh", "--json")
        self.assertEqual(json.loads(refreshed.stdout)["action"], "refreshed")
        self.assertIn("map: fresh", run_impact(self.repo, "status").stdout)


def _oah_module() -> list[str]:
    import sys

    return [sys.executable, "-m", "oah"]


if __name__ == "__main__":
    unittest.main()
