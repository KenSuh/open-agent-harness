# 영향 범위 보고 검사 — 층별 분석 상태·분석하지 못한 목록·빈 결과 문구·대체 점검을 단위 표와 실제 CLI 로 확인한다.
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

from oah.impact import gatebind, query, scope
from oah.impact.defaults import effective
from tests.helpers import git_commit, git_env, make_git_repo
from tests.test_impact_default_branch import RemoteFixture, git
from tests.test_impact_map import SRC, head, run_impact

LAYER_NAMES = ["gates", "lexical", "db", "graph", "cochange"]


def data_with(files: dict, hits: dict | None = None) -> dict:
    return {"files": files, "hits": hits or {}, "db": None, "db_hits": {}, "history": []}


def write_graph(repo: pathlib.Path, body) -> None:
    (repo / "graphify-out").mkdir(exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body)
    (repo / "graphify-out" / "graph.json").write_text(text, encoding="utf-8")


class GraphStatusTest(unittest.TestCase):
    def test_graph_status_table(self):
        repo = make_git_repo(self)
        settings = effective(None)
        commit = "a" * 40
        nodes = {"nodes": [{"id": "n1", "source_file": "a.py"}], "links": []}
        self.assertEqual(query.read_graph(repo, settings, commit)[1:3][0], "unavailable")
        self.assertIn("graphify-out/graph.json", query.read_graph(repo, settings, commit)[2])
        cases = [
            ("{broken", "unavailable", "cannot be read"),
            ({"commit": commit}, "unavailable", "cannot be read"),
            ({"nodes": "", "links": "", "commit": commit}, "unavailable", "cannot be read"),
            (nodes, "stale-unknown", "records no commit"),
            (dict(nodes, commit="main"), "stale-unknown", "not a full commit id"),
            (dict(nodes, commit="b" * 40), "stale", "bbbbbbbbbbbb"),
            (dict(nodes, commit=commit), "analysed", commit[:12]),
            (dict(nodes, graph={"commit": commit.upper()}), "analysed", commit[:12]),
        ]
        for body, status, reason in cases:
            with self.subTest(status=status, reason=reason):
                write_graph(repo, body)
                graph, got, why = query.read_graph(repo, settings, commit)
                self.assertEqual(got, status)
                self.assertIn(reason, why)
                self.assertEqual(graph is None, status == "unavailable")
                self.assertEqual(query.load_graph(repo, settings) is None, status == "unavailable")

    def test_graph_collections_must_be_lists_of_records(self):
        repo = make_git_repo(self)
        settings = effective(None)
        commit = "a" * 40
        for edge_key in ("links", "edges"):
            for collections in ({}, {"nodes": []}, {edge_key: []},
                                {"nodes": "", edge_key: ""},
                                {"nodes": {}, edge_key: []}, {"nodes": [], edge_key: {}},
                                {"nodes": None, edge_key: []}, {"nodes": [], edge_key: None},
                                {"nodes": [[]], edge_key: []}, {"nodes": [], edge_key: [[]]},
                                {"nodes": [{}], edge_key: []}, {"nodes": [], edge_key: [{}]}):
                with self.subTest(edge_key=edge_key, collections=collections):
                    write_graph(repo, dict(collections, commit=commit))
                    graph, status, reason = query.read_graph(repo, settings, commit)
                    self.assertIsNone(graph)
                    self.assertEqual(status, "unavailable")
                    self.assertTrue(reason.startswith("OAH_"), reason)
                    self.assertIn("oah graph update", reason)
                    self.assertIsNone(query.load_graph(repo, settings))
            with self.subTest(valid_empty=edge_key):
                write_graph(repo, {"nodes": [], edge_key: [], "commit": commit})
                self.assertEqual(query.read_graph(repo, settings, commit)[:2],
                                 ({"files": {}, "edges": []}, "analysed"))

    def test_ambiguous_report_matches_lexical_skip(self):
        files = {f"pkg{i}/util.py": "code" for i in range(4)}
        files["docs/a.md"] = "doc"
        hits = {"util.py": [["docs/a.md", 1, ""]], "util": [["docs/a.md", 3, ""]],
                "pkg0/util.py": [["docs/a.md", 2, ""]]}
        data = data_with(files, hits)
        settings = effective(None)
        skipped = query.ambiguous(data, ["pkg0/util.py"], settings)
        self.assertEqual(skipped, [
            {"target": "pkg0/util.py", "name": "util.py", "via": "basename", "files": 4, "limit": 3},
            {"target": "pkg0/util.py", "name": "util", "via": "stem", "files": 4, "limit": 3}])
        rows = query.query(data, ["pkg0/util.py"], settings=settings)
        self.assertEqual([r.line for r in rows], [2], "the skipped names produced no rows")
        self.assertEqual(query.ambiguous(data, ["pkg0/util.py"], effective({"max_ambiguity": 4})), [])
        self.assertEqual(query.ambiguous(data, ["db_table:users"], settings), [], "entities have no names")


class LayersTest(unittest.TestCase):
    def layers(self, **overrides):
        options = dict(targets=["src/a.py"], gate_status="2 gates from 1 ledgers; re-run bound checks",
                       db_available=True, graph_status=("analysed", "graph built at commit abc"),
                       branch_state="fresh", branch_reason="")
        options.update(overrides)
        return scope.layers(**options)

    def test_layers_table(self):
        fresh = self.layers()
        self.assertEqual(list(fresh), LAYER_NAMES)
        self.assertEqual({name: layer["status"] for name, layer in fresh.items()},
                         dict.fromkeys(LAYER_NAMES, "analysed"))
        cases = [
            ({"gate_status": gatebind.UNAVAILABLE}, {"gates": "unavailable"}),
            ({"db_available": False}, {"db": "unavailable"}),
            ({"graph_status": ("stale-unknown", "no commit")}, {"graph": "stale-unknown"}),
            ({"branch_state": "stale", "branch_reason": "offline"},
             {"lexical": "stale", "db": "stale", "cochange": "stale"}),
            ({"branch_state": "unavailable", "branch_reason": "no remote"}, {}),
            ({"targets": ["db_table:users"]},
             {"gates": "skipped", "lexical": "skipped", "graph": "skipped", "cochange": "skipped"}),
            ({"targets": []}, dict.fromkeys(LAYER_NAMES, "skipped")),
        ]
        for overrides, changed in cases:
            with self.subTest(overrides=overrides):
                got = self.layers(**overrides)
                expected = dict(dict.fromkeys(LAYER_NAMES, "analysed"), **changed)
                self.assertEqual({name: layer["status"] for name, layer in got.items()}, expected)
                for layer in got.values():
                    self.assertTrue(layer["reason"], layer)
        stale = self.layers(branch_state="stale", branch_reason="offline", db_available=False)
        self.assertEqual(stale["db"]["status"], "unavailable", "unavailable outranks stale")

    def test_complete_only_when_everything_fresh(self):
        fresh = self.layers()
        done = scope.report(fresh, [], [], rows_found=3)
        self.assertEqual((done["depth"], done["complete"]), (1, True))
        self.assertEqual(done["unanalysed"], [{"kind": "dynamic", "detail": scope.DYNAMIC_NOTE}])
        ambiguous = [{"target": "a/util.py", "name": "util.py", "via": "basename", "files": 4, "limit": 3}]
        rename = [{"from": "a.py", "to": "b.py", "similarity": 90}]
        for layers, skipped, renames in ((self.layers(graph_status=("stale", "old")), [], []),
                                         (fresh, ambiguous, []), (fresh, [], rename)):
            with self.subTest(skipped=skipped, renames=renames):
                partial = scope.report(layers, skipped, renames, rows_found=3)
                self.assertFalse(partial["complete"])
                self.assertEqual(partial["unanalysed"][-1]["kind"], "dynamic")
        partial = scope.report(self.layers(graph_status=("unavailable", "missing")), ambiguous, rename,
                               rows_found=0)
        self.assertEqual([item["kind"] for item in partial["unanalysed"]],
                         ["ambiguous-name", "layer", "rename", "dynamic"])
        self.assertEqual(partial["unanalysed"][0]["files"], 4)
        self.assertEqual((partial["unanalysed"][1]["layer"], partial["unanalysed"][1]["status"]),
                         ("graph", "unavailable"))
        self.assertEqual(partial["summary"], scope.EMPTY)
        self.assertIn("not proof of no impact", scope.EMPTY)

    def test_text_lines(self):
        report = scope.report(self.layers(graph_status=("unavailable", "graphify-out/graph.json is missing")),
                              [{"target": "a/util.py", "name": "util.py", "via": "basename", "files": 4,
                                "limit": 3}],
                              [{"from": "old.py", "to": "new.py", "similarity": None}], rows_found=0)
        text = "\n".join(scope.lines(report))
        self.assertIn("scope: depth=1 complete=false", text)
        self.assertRegex(text, r"unavailable\s+graph\s+graphify-out/graph\.json is missing")
        self.assertIn("util.py", text)
        self.assertIn("shared by 4 files", text)
        self.assertIn("old.py -> new.py", text)
        self.assertIn(scope.DYNAMIC_NOTE, text)


class RenameTest(unittest.TestCase):
    def test_repetitive_rename_similarity_finishes_within_budget(self):
        repo = make_git_repo(self)
        body = b"VALUE = 1\n" * 12000
        base = git_commit(repo, {".gitattributes": b"* -text\n", "old.py": body})
        (repo / "old.py").unlink()
        (repo / "new.py").write_bytes(body + b"EXTRA = 2\n")
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import json, pathlib, sys; from oah.impact.scope import likely_renames; "
             "print(json.dumps(likely_renames(pathlib.Path.cwd(), sys.argv[1], None, timeout=2)))", base],
            cwd=repo, capture_output=True, text=True, encoding="utf-8", timeout=5,
            env=dict(os.environ, PYTHONPATH=SRC))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [{"from": "old.py", "to": "new.py", "similarity": 50}])

    def test_large_rename_candidates_are_unchecked_unless_hashes_match(self):
        for large_old, large_new, identical in ((True, False, False), (False, True, False),
                                               (True, True, True)):
            with self.subTest(large_old=large_old, large_new=large_new, identical=identical):
                repo = make_git_repo(self)
                small = b"VALUE = 1\n"
                large = b"".join(f"VALUE_{i} = {i}\n".encode("ascii") for i in range(20000))
                base = git_commit(repo, {".gitattributes": b"* -text\n",
                                         "old.py": large if large_old else small})
                (repo / "old.py").unlink()
                # Exact normalized hashes still detect large LF / CRLF renames.
                body = large if large_new else small
                (repo / "new.py").write_bytes(body.replace(b"\n", b"\r\n") if identical else body)
                items = scope.likely_renames(repo, base, None, timeout=60.0)
                self.assertEqual(len(items), 1, items)
                item = items[0]
                self.assertEqual((item["from"], item["to"]), ("old.py", "new.py"))
                if identical:
                    self.assertEqual(item["similarity"], 100)
                else:
                    self.assertEqual(item["kind"], "possible-rename-unchecked")
                    self.assertIn("262144", item["detail"])
                    self.assertIn("review", item["detail"])
                    fresh = {name: {"status": "analysed", "reason": "fresh"} for name in LAYER_NAMES}
                    report = scope.report(fresh, [], items, rows_found=0)
                    self.assertFalse(report["complete"])
                    self.assertEqual(report["unanalysed"], [item, {"kind": "dynamic", "detail": scope.DYNAMIC_NOTE}])

    def test_untracked_rename_matches_crlf_checkout_to_lf_blob(self):
        repo = make_git_repo(self)
        body = b"FIRST = 1\nSECOND = 2\n"
        base = git_commit(repo, {".gitattributes": b"* -text\n", "src/old.py": body})
        (repo / "src/old.py").unlink()
        # Reproduce Git's LF blob / Windows CRLF checkout without depending on the host OS.
        (repo / "src/new.py").write_bytes(body.replace(b"\n", b"\r\n"))
        self.assertEqual(scope.likely_renames(repo, base, None, timeout=60.0),
                         [{"from": "src/old.py", "to": "src/new.py", "similarity": 100}])

    def test_untracked_rename_with_different_filename(self):
        for edited in (False, True):
            with self.subTest(edited=edited):
                repo = make_git_repo(self)
                body = b"".join(f"VALUE_{i} = {i}\r\n".encode("ascii") for i in range(40))
                base = git_commit(repo, {".gitattributes": "* -text\n", "src/old.py": body})
                (repo / "src/old.py").rename(repo / "src/new.py")
                if edited:
                    (repo / "src/new.py").write_bytes(body + b"EXTRA = 1\r\n")
                (repo / "src/unrelated.py").write_bytes(b"unrelated content\r\n")
                pairs = scope.likely_renames(repo, base, None, timeout=60.0)
                self.assertEqual(len(pairs), 1, pairs)
                self.assertEqual((pairs[0]["from"], pairs[0]["to"]), ("src/old.py", "src/new.py"))
                self.assertGreaterEqual(pairs[0]["similarity"], 50)
                self.assertEqual(pairs[0]["similarity"] == 100, not edited)
                self.assertEqual(scope.likely_renames(repo, base, head(repo), timeout=60.0), [])

    def test_rename_pairs(self):
        repo = make_git_repo(self)
        body = "".join(f"line {i}\n" for i in range(40))
        base = git_commit(repo, {"src/old_name.py": body, "lib/helper.py": "def h():\n    return 1\n",
                                 "keep.py": "x = 1\n"})
        git_commit(repo, {"src/old_name.py": None, "src/new_name.py": body + "extra\n"})
        subprocess.run(["git", "-C", str(repo), "rm", "-q", "lib/helper.py"], check=True, env=git_env())
        subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "rm"],
                       check=True, env=git_env())
        (repo / "pkg").mkdir()
        (repo / "pkg" / "helper.py").write_text("totally different\n", encoding="utf-8")
        renames = scope.likely_renames(repo, base, None, timeout=60.0)
        self.assertEqual(len(renames), 2, renames)
        by_name = {item["to"]: item for item in renames}
        self.assertEqual(by_name["src/new_name.py"]["from"], "src/old_name.py")
        self.assertIsInstance(by_name["src/new_name.py"]["similarity"], int)
        self.assertEqual(by_name["pkg/helper.py"], {"from": "lib/helper.py", "to": "pkg/helper.py",
                                                    "similarity": None})
        self.assertEqual(scope.likely_renames(repo, base, head(repo), timeout=60.0),
                         [r for r in renames if r["to"] != "pkg/helper.py"], "--ref ignores untracked files")
        self.assertEqual(scope.likely_renames(repo, head(repo), None, timeout=60.0),
                         [], "an untracked file with no deleted partner is not a rename")


class FallbackTest(unittest.TestCase):
    def gate(self, gate_id, check, expect="OK", ledger=".oah/tasks/t/GATES.md"):
        return gatebind.GateRef(ledger, gate_id, check, expect)

    def test_fallback_needles(self):
        gates = [self.gate("G1", "python src/plugins/loader.py"), self.gate("G2", "echo unrelated"),
                 self.gate("G1", "python -m src.plugins.loader", ledger="other/GATES.md"),
                 self.gate("G1", "pytest src/pluginsx", ledger="miss/GATES.md"),
                 self.gate("G1", "python -m unittest", "plugins.alpha ok", ledger="mod/GATES.md"),
                 self.gate("G1", "python tools/x.py", ledger="top/GATES.md")]
        found = scope.fallback(gates, ["src/plugins/alpha.py"], strong=[], checks=[])
        self.assertEqual([(item["source"], item["binding"]) for item in found],
                         [(".oah/tasks/t/GATES.md#G1", "fallback"), (".oah/tasks/t/GATES.md#G2", "fallback"),
                          ("mod/GATES.md#G1", "fallback"), ("other/GATES.md#G1", "fallback")])
        first = found[0]
        self.assertEqual(first["targets"], ["src/plugins/alpha.py"])
        self.assertEqual(first["check"], "python src/plugins/loader.py")
        self.assertIn("src/plugins", first["why"])
        self.assertIsNone(first["line"])
        strong = [{"source": ".oah/tasks/t/GATES.md#G1", "line": None, "binding": "exec", "targets": ["x"]}]
        self.assertNotIn(".oah/tasks/t/GATES.md#G1",
                         [item["source"] for item in scope.fallback(gates, ["src/plugins/alpha.py"], strong=strong,
                                                                    checks=[])])
        self.assertEqual(scope.fallback(gates, ["setup.py"], strong=[], checks=[]), [],
                         "a top-level file has no folder needle, and its module name has one segment")
        configured = scope.fallback([], ["src/a.py", "db_table:users"], strong=[], checks=["make test"])
        self.assertEqual(configured, [{"source": "[impact] fallback_checks", "line": None, "binding": "fallback",
                                       "check": "make test", "why": "configured in [impact] fallback_checks",
                                       "targets": ["db_table:users", "src/a.py"]}])
        self.assertEqual(scope.fallback(gates, ["db_table:users"], strong=[], checks=["make test"]),
                         [{"source": "[impact] fallback_checks", "line": None, "binding": "fallback",
                           "check": "make test", "why": "configured in [impact] fallback_checks",
                           "targets": ["db_table:users"]}])
        self.assertEqual(scope.fallback(gates, [], strong=[], checks=["make test"]), [])


LEDGER = (".oah/tasks/t1/GATES.md", "- [ ] G1: the check runs\n  CHECK: python tools/check.py\n  EXPECT: OK\n"
          "  EVIDENCE: pending\n")
BASE = {
    ".gitignore": "graphify-out/\n",
    "src/app.py": "from lib import util\nprint('app')\n",
    "src/lib/util.py": "def helper():\n    return 1\n",
    "tools/check.py": "print('check')\n",
    ".github/workflows/ci.yml": "steps:\n  - run: python tools/check.py\n",
    "docs/guide.md": "See src/app.py and tools/check.py.\n",
    "db/migrations/001_init.sql": "CREATE TABLE users (id int);\n",
    LEDGER[0]: LEDGER[1],
}
SAFE = re.compile(r"\bsafe(ly|ty)?\b", re.I)


def json_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in [key, *json_strings(item)]]
    if isinstance(value, list):
        return [text for item in value for text in json_strings(item)]
    return []


class ScopeCliTest(unittest.TestCase):
    """Each case builds a repository and runs the real `python -m oah impact`."""

    def repo(self, extra: dict | None = None) -> pathlib.Path:
        repo = make_git_repo(self)
        git_commit(repo, dict(BASE, **(extra or {})))
        return repo

    def impact(self, repo: pathlib.Path, *args: str) -> tuple[dict, str]:
        report = run_impact(repo, *args, "--json")
        self.assertEqual(report.returncode, 0, report.stderr)
        text = run_impact(repo, *args)
        self.assertEqual(text.returncode, 0, text.stderr)
        return json.loads(report.stdout), text.stdout

    def assert_shape(self, report: dict, text: str) -> dict:
        scope_block = report["scope"]
        self.assertEqual(scope_block["depth"], 1)
        self.assertIsInstance(scope_block["complete"], bool)
        self.assertEqual(list(scope_block["layers"]), LAYER_NAMES)
        for name, layer in scope_block["layers"].items():
            self.assertIn(layer["status"], ("analysed", "unavailable", "stale", "stale-unknown", "skipped"), name)
            self.assertTrue(layer["reason"], name)
            self.assertRegex(text, rf"\n  {re.escape(layer['status'])}\s+{name}\s+\S")
        self.assertEqual(scope_block["unanalysed"][-1], {"kind": "dynamic", "detail": scope.DYNAMIC_NOTE})
        self.assertIn(f"scope: depth=1 complete={'true' if scope_block['complete'] else 'false'}", text)
        self.assertIn("not analysed:", text)
        self.assertIn(scope.DYNAMIC_NOTE, text)
        self.assertNotIn("no references found", text)
        self.assertIsInstance(report["fallback"], list)
        if scope_block["complete"]:
            self.assertEqual(report["fallback"], [])
        for item in report["fallback"]:
            self.assertEqual(item["binding"], "fallback")
        for line in [*text.splitlines(), *json_strings(report)]:
            self.assertIsNone(SAFE.search(line), f"a result is never called safe: {line}")
        self.assertRegex(text.splitlines()[-1], r"\Atotal=\d+ shown=\d+ overflow=\d+\Z")
        return scope_block

    def test_scope_table_json_shape(self):
        def fresh_graph(repo):
            # The SQL dependency scanner reports every statement as partially parsed.
            git_commit(repo, {"db/migrations/001_init.sql": b"-- no SQL statements\n"})
            write_graph(repo, {"nodes": [{"id": "n1", "source_file": "tools/check.py"}], "links": [],
                               "graph": {"commit": head(repo)}})

        cases = {
            "direct references, everything fresh": (fresh_graph, ["tools/check.py"], True, {}),
            "missing graph": (None, ["tools/check.py"], False, {"graph": "unavailable"}),
            "stale graph": (lambda repo: write_graph(repo, {"nodes": [], "links": [], "commit": "b" * 40}),
                            ["tools/check.py"], False, {"graph": "stale"}),
            "unverifiable graph": (lambda repo: write_graph(repo, {"nodes": [], "links": []}),
                                   ["tools/check.py"], False, {"graph": "stale-unknown"}),
        }
        for name, (prepare, args, complete, statuses) in cases.items():
            with self.subTest(case=name):
                repo = self.repo()
                if prepare:
                    prepare(repo)
                report, text = self.impact(repo, *args)
                block = self.assert_shape(report, text)
                self.assertEqual(block["complete"], complete, block)
                for layer, status in dict(dict.fromkeys(LAYER_NAMES, "analysed"), **statuses).items():
                    self.assertEqual(block["layers"][layer]["status"], status, layer)
                self.assertIn((".github/workflows/ci.yml", "exec"),
                              {(row["source"], row["binding"]) for row in report["rows"]}, "direct rows are kept")
                kinds = [(item["kind"], item.get("layer")) for item in block["unanalysed"]]
                self.assertEqual(("layer", "graph") in kinds, not complete)
                if not complete:
                    self.assertIn(block["layers"]["graph"]["reason"], text)

    def test_malformed_graph_keeps_scope_incomplete_and_configured_fallback(self):
        repo = self.repo({"db/migrations/001_init.sql": b"-- no SQL statements\n",
                          ".oah/config.toml": b'[impact]\nfallback_checks = ["python -m unittest"]\n'})
        commit = head(repo)
        for collections in ({"nodes": [], "links": []}, {}, {"nodes": "", "links": ""}):
            with self.subTest(collections=collections):
                write_graph(repo, dict(collections, commit=commit))
                report, text = self.impact(repo, "tools/check.py")
                block = self.assert_shape(report, text)
                valid = collections == {"nodes": [], "links": []}
                self.assertEqual(block["complete"], valid)
                self.assertTrue(all(layer["status"] == "analysed" for name, layer in block["layers"].items()
                                    if name != "graph"))
                self.assertEqual(block["layers"]["graph"]["status"], "analysed" if valid else "unavailable")
                gaps = [item for item in block["unanalysed"] if item.get("layer") == "graph"]
                if valid:
                    self.assertEqual(gaps, [])
                    self.assertEqual(report["fallback"], [])
                else:
                    self.assertEqual(len(gaps), 1)
                    self.assertEqual(gaps[0]["status"], "unavailable")
                    self.assertIn("oah graph update", gaps[0]["detail"])
                    self.assertEqual([item["check"] for item in report["fallback"]], ["python -m unittest"])
                    self.assertIn("re-run (fallback): python -m unittest", text)

    def test_ambiguous_skip_with_count(self):
        repo = self.repo({f"pkg{i}/shared.py": f"N = {i}\n" for i in range(4)})
        report, text = self.impact(repo, "pkg0/shared.py")
        block = self.assert_shape(report, text)
        skipped = [item for item in block["unanalysed"] if item["kind"] == "ambiguous-name"]
        self.assertEqual([(item["name"], item["files"], item["limit"]) for item in skipped],
                         [("shared.py", 4, 3), ("shared", 4, 3)])
        self.assertFalse(block["complete"])
        self.assertIn("shared.py (basename of pkg0/shared.py) is shared by 4 files", text)

    def test_map_coverage_gaps_make_scope_incomplete(self):
        for source in ("worktree", "default-branch"):
            for kind in ("excluded-path", "sql-unparsed"):
                with self.subTest(source=source, kind=kind):
                    if source == "worktree":
                        repo = gap_repo = self.repo({"db/migrations/001_init.sql": b"-- no SQL statements\n"})
                    else:
                        fx = RemoteFixture(self)
                        repo, gap_repo = fx.lane, fx.pusher
                        git_commit(repo, dict(BASE, **{"db/migrations/001_init.sql": b"-- no SQL statements\n"}))
                    git_commit(repo, {".oah/config.toml":
                                     '[impact]\nfallback_checks = ["python -m unittest"]\n'})
                    path = "tools/excluded.py" if kind == "excluded-path" else "db/unparsed.sql"
                    git_commit(gap_repo, {path: b"check.py" if kind == "excluded-path" else
                                         b"\nCREATE UNRECOGNISED widget;\n"})
                    if kind == "excluded-path":
                        # A real Git symlink entry also works where OS symlinks need privileges.
                        blob = git(gap_repo, "rev-parse", f"HEAD:{path}")
                        git(gap_repo, "update-index", "--cacheinfo", f"120000,{blob},{path}")
                        git(gap_repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "symlink")
                    if source == "default-branch":
                        git(gap_repo, "push", "-q", "origin", "HEAD:main")
                    write_graph(repo, {"nodes": [], "links": [], "commit": head(repo)})
                    report, text = self.impact(repo, "tools/check.py", "--ref", "HEAD")
                    block = self.assert_shape(report, text)
                    self.assertTrue(all(layer["status"] == "analysed" for layer in block["layers"].values()))
                    self.assertFalse(block["complete"])
                    gaps = [item for item in block["unanalysed"] if item["kind"] == kind]
                    self.assertEqual(len(gaps), 1, block)
                    self.assertEqual((gaps[0]["map"], gaps[0]["file"]), (source, path))
                    if kind == "sql-unparsed":
                        self.assertEqual(gaps[0]["line"], 2)
                    self.assertIn(gaps[0]["detail"], text)
                    self.assertTrue(any(item["check"] == "python -m unittest" for item in report["fallback"]))
                    self.assertIn("re-run (fallback): python -m unittest", text)

    def test_rename_pair(self):
        repo = self.repo()
        base = head(repo)
        subprocess.run(["git", "-C", str(repo), "mv", "src/lib/util.py", "src/lib/helpers.py"], check=True,
                       env=git_env())
        git_commit(repo, {})
        report, text = self.impact(repo, "--changed-since", base)
        block = self.assert_shape(report, text)
        renames = [item for item in block["unanalysed"] if item["kind"] == "rename"]
        self.assertEqual([(item["from"], item["to"], item["similarity"]) for item in renames],
                         [("src/lib/util.py", "src/lib/helpers.py", 100)])
        self.assertFalse(block["complete"])
        self.assertIn("src/lib/util.py -> src/lib/helpers.py looks like a rename", text)
        self.assertEqual(report["changed_since"]["files"], ["src/lib/helpers.py", "src/lib/util.py"],
                         "changes are still read without rename folding")

    def test_unstaged_rename_is_unanalysed(self):
        repo = self.repo({"db/migrations/001_init.sql": b"-- no SQL statements\n"})
        base = head(repo)
        (repo / "src/lib/util.py").rename(repo / "src/lib/helpers.py")
        write_graph(repo, {"nodes": [], "links": [], "commit": base})
        report, text = self.impact(repo, "--changed-since", base)
        block = self.assert_shape(report, text)
        renames = [item for item in block["unanalysed"] if item["kind"] == "rename"]
        self.assertEqual([(item["from"], item["to"], item["similarity"]) for item in renames],
                         [("src/lib/util.py", "src/lib/helpers.py", 100)])
        self.assertFalse(block["complete"])
        self.assertEqual(report["changed_since"]["files"], ["src/lib/helpers.py", "src/lib/util.py"])

    def test_unchecked_rename_makes_fresh_layers_incomplete(self):
        repo = self.repo({"db/migrations/001_init.sql": b"-- no SQL statements\n",
                          ".gitattributes": b"* -text\n",
                          ".oah/config.toml": b'[impact]\nfallback_checks = ["python -m unittest"]\n',
                          "old.py": b"VALUE = 1\n" * 30000})
        base = head(repo)
        (repo / "old.py").unlink()
        (repo / "new.py").write_bytes(b"VALUE = 1\n")
        write_graph(repo, {"nodes": [], "links": [], "commit": base})
        report, text = self.impact(repo, "--changed-since", base)
        block = self.assert_shape(report, text)
        self.assertTrue(all(layer["status"] == "analysed" for layer in block["layers"].values()))
        self.assertFalse(block["complete"])
        unchecked = [item for item in block["unanalysed"] if item["kind"] == "possible-rename-unchecked"]
        self.assertEqual(len(unchecked), 1, block)
        self.assertEqual((unchecked[0]["from"], unchecked[0]["to"]), ("old.py", "new.py"))
        self.assertIn(unchecked[0]["detail"], text)
        for plan in (report["fallback"], report["changed_since"]["fallback"]):
            self.assertEqual([item["check"] for item in plan], ["python -m unittest"])

    def test_entity_only_query_includes_configured_fallback(self):
        repo = self.repo({".oah/config.toml": '[impact]\nfallback_checks = ["python -m unittest"]\n'})
        report, text = self.impact(repo, "db_table:users")
        block = self.assert_shape(report, text)
        self.assertFalse(block["complete"])
        self.assertEqual(report["fallback"],
                         [{"source": "[impact] fallback_checks", "line": None, "binding": "fallback",
                           "check": "python -m unittest", "why": "configured in [impact] fallback_checks",
                           "targets": ["db_table:users"]}])
        self.assertIn("re-run (fallback): python -m unittest", text)

    def test_empty_result_is_not_proof(self):
        repo = self.repo()
        git_commit(repo, {"src/lonely_module.py": "VALUE = 7\n"})
        report, text = self.impact(repo, "src/lonely_module.py")
        block = self.assert_shape(report, text)
        self.assertEqual(report["rows"], [])
        self.assertEqual(block["summary"], scope.EMPTY)
        lines = text.splitlines()
        self.assertIn(scope.EMPTY, lines)
        after = lines[lines.index(scope.EMPTY) + 1:]
        self.assertIn("not analysed:", after, "the unanalysed list follows the empty-result sentence")
        self.assertTrue(any(scope.DYNAMIC_NOTE in line for line in after))
        self.assertFalse(block["complete"])

    def test_entity_only_query_skips_file_layers(self):
        repo = self.repo()
        report, text = self.impact(repo, "db_table:users")
        block = self.assert_shape(report, text)
        self.assertEqual({name: layer["status"] for name, layer in block["layers"].items()},
                         {"gates": "skipped", "lexical": "skipped", "db": "analysed", "graph": "skipped",
                          "cochange": "skipped"})
        self.assertFalse(block["complete"], "skipped layers did not run")
        self.assertEqual(report["fallback"], [], "an entity has no folder or module to fall back on")

    def test_fallback_checks_config(self):
        repo = self.repo()
        config = repo / ".oah" / "config.toml"
        config.write_text('[impact]\nfallback_checks = ["make test"]\n', encoding="utf-8")
        report, text = self.impact(repo, "src/app.py")
        self.assert_shape(report, text)
        self.assertIn({"source": "[impact] fallback_checks", "line": None, "binding": "fallback",
                       "check": "make test", "why": "configured in [impact] fallback_checks",
                       "targets": ["src/app.py"]}, report["fallback"])
        self.assertIn("re-run (fallback): make test (configured in [impact] fallback_checks)", text)
        for bad in ('fallback_checks = "make test"', "fallback_checks = [1]"):
            with self.subTest(config=bad):
                config.write_text(f"[impact]\n{bad}\n", encoding="utf-8")
                result = run_impact(repo, "src/app.py")
                self.assertEqual(result.returncode, 69, result.stderr)
                self.assertIn("OAH_CONFIG_INVALID", result.stderr)


PLUGIN_LOADER = """import importlib
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(here))
names = sorted(f[:-3] for f in os.listdir(here) if f.endswith(".py") and f not in ("__init__.py", "loader.py"))
for name in names:
    importlib.import_module("plugins." + name)
print("loaded", len(names), "plugins")
"""
PLUGIN_LEDGER = (".oah/tasks/plugins/GATES.md", "- [ ] G1: every plugin loads\n  CHECK: python src/plugins/loader.py\n"
                 "  EXPECT: loaded 2 plugins\n  EVIDENCE: pending\n")


class MissedCaseTest(unittest.TestCase):
    def test_root_module_invoked_with_python_m_gets_fallback(self):
        repo = make_git_repo(self)
        ledger = ".oah/tasks/root/GATES.md"
        base = git_commit(repo, {"alpha.py": b"VALUE = 1\n", ledger:
                                 b"- [ ] G1: root module runs\n  CHECK: python -m alpha\n  EXPECT: OK\n"
                                 b"- [ ] G2: companion check\n  CHECK: python -m unittest\n  EXPECT: OK\n"})
        (repo / "alpha.py").write_bytes(b"VALUE = 2\n")
        report_result = run_impact(repo, "--changed-since", base, "--json")
        self.assertEqual(report_result.returncode, 0, report_result.stderr)
        report = json.loads(report_result.stdout)
        self.assertEqual(report["changed_since"]["rerun"], [])
        self.assertFalse(report["scope"]["complete"])
        for plan in (report["fallback"], report["changed_since"]["fallback"]):
            self.assertEqual([(item["source"], item["binding"], item["targets"]) for item in plan],
                             [(ledger + "#G1", "fallback", ["alpha.py"]),
                              (ledger + "#G2", "fallback", ["alpha.py"])])
        self.assertEqual(scope.fallback_needles("alpha.py"), {"alpha"})
        self.assertEqual(scope.fallback([gatebind.GateRef("miss/GATES.md", "G1", "python -m alphabet", "OK")],
                                        ["alpha.py"], strong=[], checks=[]), [])

    def test_string_built_dependency_is_caught_by_fallback(self):
        repo = make_git_repo(self)
        git_commit(repo, {"src/plugins/__init__.py": "", "src/plugins/loader.py": PLUGIN_LOADER,
                          "src/plugins/beta.py": "VALUE = 2\n", PLUGIN_LEDGER[0]: PLUGIN_LEDGER[1]})
        git_commit(repo, {"src/plugins/alpha.py": "VALUE = 1\n"})
        base = head(repo)
        git_commit(repo, {"src/plugins/alpha.py": "VALUE = (\n"})
        broken = subprocess.run([sys.executable, "-B", "src/plugins/loader.py"], cwd=repo, capture_output=True,
                                text=True)
        self.assertNotEqual(broken.returncode, 0, "the gate's check really depends on alpha.py")

        result = run_impact(repo, "--changed-since", base, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["changed_since"]["files"], ["src/plugins/alpha.py"])
        sources = {row["source"] for row in report["rows"]}
        self.assertNotIn("src/plugins/loader.py", sources, "impact cannot see the string-built import")
        self.assertNotIn(PLUGIN_LEDGER[0] + "#G1", sources)
        self.assertEqual(report["changed_since"]["rerun"], [])
        self.assertFalse(report["scope"]["complete"])
        self.assertIn({"kind": "dynamic", "detail": scope.DYNAMIC_NOTE}, report["scope"]["unanalysed"])
        gate = PLUGIN_LEDGER[0] + "#G1"
        for plan in (report["fallback"], report["changed_since"]["fallback"]):
            caught = [item for item in plan if item["source"] == gate]
            self.assertEqual(len(caught), 1, plan)
            self.assertEqual((caught[0]["binding"], caught[0]["check"], caught[0]["targets"]),
                             ("fallback", "python src/plugins/loader.py", ["src/plugins/alpha.py"]))
        text = run_impact(repo, "--changed-since", base).stdout
        self.assertIn(f"re-run (fallback): {gate} python src/plugins/loader.py", text)
        self.assertIn("re-run: nothing is bound to these files by pin or exec", text)


if __name__ == "__main__":
    unittest.main()
