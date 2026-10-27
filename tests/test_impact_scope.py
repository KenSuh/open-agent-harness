# 영향 범위 보고 검사 — 층별 분석 상태·분석하지 못한 목록·빈 결과 문구·대체 점검을 단위 표와 실제 CLI 로 확인한다.
from __future__ import annotations

import json
import pathlib
import subprocess
import unittest

from oah.impact import gatebind, query, scope
from oah.impact.defaults import effective
from tests.helpers import git_commit, git_env, make_git_repo
from tests.test_impact_map import head

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
                                       "targets": ["src/a.py"]}])
        self.assertEqual(scope.fallback(gates, ["db_table:users"], strong=[], checks=["make test"]), [],
                         "DB entities have no file needles, and configured checks need a file target")


if __name__ == "__main__":
    unittest.main()
