# CI 영향 테스트 선택기 검사 — 작은 임시 저장소에서 규칙마다 고르는 테스트와 전체 실행 대체를 확인한다.
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("affected_tests_tool", ROOT / "tools" / "affected_tests.py")
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool  # dataclasses look the module up by name
SPEC.loader.exec_module(tool)

LANE_BASE = "62613ef388a09930e89f8f89bb28850bbd1b74ed"
GIT_COMMIT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def case(body: str = "pass") -> str:
    return f"import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        {body}\n"


# A small repository with one module per rule. Paths are repo-relative; sources are dedented.
BASE_FILES = {
    "pyproject.toml": "[project]\nname = 'oah'\n",
    "LICENSE": "license text\n",
    "install/setup.sh": "echo setup\n",
    "src/oah/__init__.py": "",
    "src/oah/a.py": "from oah import b\n",
    "src/oah/b.py": "from . import c\n",
    "src/oah/c.py": "VALUE = 1\n",
    "src/oah/d.py": "NAME = 'data.txt'\n",
    "src/oah/data.txt": "package data\n",
    "src/oah/lazy.py": "def run():\n    from oah import c\n    return c\n",
    "tests/__init__.py": "from tests.helpers import setup\nsetup()\n",
    "tests/helpers.py": """
        import os
        def setup():
            os.environ.setdefault('X', '1')
        def make():
            from oah import d
            return d
    """,
    "tests/test_a.py": "import oah.a\n" + case(),
    "tests/test_d.py": "from tests.helpers import make\n" + case("make()"),
    "tests/test_lazy.py": "from oah import lazy\n" + case(),
    "tests/test_child.py": "import subprocess, sys\n" + case("subprocess.run([sys.executable, '-m', 'oah'])"),
    "tests/test_fake.py": "import pathlib\nFAKE = pathlib.Path(__file__).parent / 'fakes' / 'fake_tool.py'\n" + case(),
    "tests/fakes/fake_tool.py": "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'oah', 'x'])\n",
    "tests/test_text.py": "SCRIPT = 'install/setup.sh'\n" + case(),
    "tests/test_plain.py": case(),
    "tests/fixtures/data.json": "{}\n",
    "tools/test_shard_times.json": json.dumps({"default_ms": 1000, "source": "s",
                                               "times_ms": {"tests.test_a.T.test_x": 5}}),
}
CHILD = ["tests.test_child", "tests.test_fake"]


class Repo:
    """A throwaway git repository whose base commit holds BASE_FILES; `head` commits a change on top."""

    def __init__(self, test: unittest.TestCase, files: dict[str, str] | None = None, reverse: bool = False):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="oah-affected-")).resolve()
        test.addCleanup(shutil.rmtree, tmp, True)
        self.path = tmp / "repo"
        self.path.mkdir()
        empty = tmp / "empty-config"
        empty.write_text("", encoding="utf-8")
        test.enterContext(mock.patch.dict(os.environ, {
            "GIT_CEILING_DIRECTORIES": str(tmp), "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}))
        self.git("init", "-q")
        items = sorted((files if files is not None else BASE_FILES).items(), reverse=reverse)
        for name, text in items:
            self.write(name, text)
        self.base = self.commit("base")

    def git(self, *args: str) -> str:
        done = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def write(self, name: str, text: str) -> None:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8", newline="\n")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git(*GIT_COMMIT, "commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def head(self, change: dict[str, str | None]) -> str:
        for name, text in change.items():
            if text is None:
                (self.path / name).unlink()
            else:
                self.write(name, text)
        return self.commit("change")

    def select(self, head: str, event: str = "pull_request", base: str | None = None):
        return tool.select(self.path, self.base if base is None else base, head, event)


class SelectionRulesTest(unittest.TestCase):
    def assertSelected(self, selection, modules):
        self.assertFalse(selection.whole, selection.reason)
        self.assertEqual(list(selection.modules), sorted(modules))

    def assertWhole(self, selection, reason):
        self.assertTrue(selection.whole, selection.modules)
        self.assertIn(reason, selection.reason)
        self.assertEqual(selection.count, 6)

    def test_rule1_shared_infrastructure_runs_the_whole_suite(self):
        repo = Repo(self)
        for name in ("tests/helpers.py", "tests/__init__.py", ".github/workflows/ci.yml", "tools/test_shard.py",
                     "tools/test_shard_times.json", "tools/affected_tests.py", "pyproject.toml",
                     "src/oah/__init__.py", ".gitattributes"):
            with self.subTest(name=name):
                repo.git("checkout", "-q", "--detach", repo.base)
                text = "{}" if name.endswith(".json") else "# changed\n"
                self.assertWhole(repo.select(repo.head({name: text})), f"{name} is shared test infrastructure")

    def test_rule2_a_changed_test_module_runs_itself(self):
        repo = Repo(self)
        self.assertSelected(repo.select(repo.head({"tests/test_plain.py": case("self.assertTrue(1)")})),
                            ["tests.test_plain"])

    def test_rule2_a_deleted_test_module_is_dropped_without_error(self):
        repo = Repo(self)
        selection = repo.select(repo.head({"tests/test_plain.py": None}))
        self.assertSelected(selection, [])
        self.assertEqual(selection.count, 1)

    def test_rule2_a_renamed_test_module_runs_under_its_new_name(self):
        repo = Repo(self)
        repo.git("mv", "tests/test_plain.py", "tests/test_renamed.py")
        self.assertSelected(repo.select(repo.commit("rename")), ["tests.test_renamed"])

    def test_rule3_two_level_chain_and_relative_import(self):
        repo = Repo(self)
        # test_a imports oah.a, which imports oah.b, which imports oah.c relatively; test_lazy calls lazy.run().
        self.assertSelected(repo.select(repo.head({"src/oah/c.py": "VALUE = 2\n"})),
                            ["tests.test_a", "tests.test_lazy", *CHILD])

    def test_rule3_a_rename_counts_old_and_new_paths(self):
        repo = Repo(self)
        repo.git("mv", "src/oah/c.py", "src/oah/c2.py")
        repo.write("src/oah/b.py", "from . import c2\n")
        selection = repo.select(repo.commit("rename"))
        # test_lazy still imports the old name, so it is selected too: at this head it would fail.
        self.assertSelected(selection, ["tests.test_a", "tests.test_lazy", *CHILD])
        changed = tool.changed_files(repo.path, repo.base, "HEAD")
        self.assertIn("src/oah/c.py", changed)
        self.assertIn("src/oah/c2.py", changed)

    def test_rule3_a_helper_function_reaches_only_the_tests_that_name_it(self):
        repo = Repo(self)
        self.assertSelected(repo.select(repo.head({"src/oah/d.py": "NAME = 'data.txt'\nOTHER = 1\n"})),
                            ["tests.test_d", *CHILD])

    def test_rule3_tools_imports_count_for_changed_tools(self):
        files = dict(BASE_FILES, **{"tools/check.py": "X = 1\n",
                                    "tests/test_tool.py": "from tools import check\n" + case()})
        repo = Repo(self, files)
        self.assertSelected(repo.select(repo.head({"tools/check.py": "X = 2\n"})), ["tests.test_tool"])

    def test_imported_test_helpers_follow_transitive_function_imports(self):
        for folder in ("tests/fixtures/gates", "tests/fakes/nested", "tests/support/nested"):
            with self.subTest(folder=folder):
                files = dict(BASE_FILES, **{
                    "tests/test_helper.py": f"from {folder.replace('/', '.')}.support import run_cli\n" + case(),
                    f"{folder}/support.py": "def run_cli():\n    from . import bridge\n    return bridge.run()\n",
                    f"{folder}/bridge.py": "def run():\n    import oah.c\n    return oah.c.VALUE\n",
                    "tests/fixtures/data/repo/broken.py": "def (:\n",
                })
                repo = Repo(self, files)
                self.assertSelected(repo.select(repo.head({"src/oah/c.py": "VALUE = 2\n"})),
                                    ["tests.test_a", "tests.test_lazy", "tests.test_helper", *CHILD])

    def test_rule4_child_process_tests_and_fakes_run_for_any_src_change(self):
        repo = Repo(self)
        self.assertSelected(repo.select(repo.head({"src/oah/new.py": "X = 1\n"})), CHILD)

    def test_rule4_a_fake_that_starts_oah_makes_its_users_child_tests(self):
        files = dict(BASE_FILES, **{"tests/fakes/fake_tool.py": "print('no child')\n"})
        repo = Repo(self, files)
        self.assertSelected(repo.select(repo.head({"src/oah/new.py": "X = 1\n"})), ["tests.test_child"])

    def test_rule5_a_text_reader_runs_when_the_file_it_names_changes(self):
        repo = Repo(self)
        self.assertSelected(repo.select(repo.head({"install/setup.sh": "echo changed\n"})), ["tests.test_text"])

    def test_rule5_a_changed_fake_runs_the_tests_that_name_it(self):
        repo = Repo(self)
        self.assertSelected(repo.select(repo.head({"tests/fakes/fake_tool.py": "print(1)\n"})),
                            ["tests.test_fake"])

    def test_non_python_src_changes_always_run_the_whole_suite(self):
        cases: dict[str, dict[str, str | None]] = {
            "changed named resource": {"src/oah/data.txt": "new data\n"},
            "added directory resource": {"src/oah/templates/obsidian/extra.json": "not JSON\n"},
            "deleted resource": {"src/oah/data.txt": None},
            "renamed template": {"src/oah/templates/rules/block.en.md": None,
                                 "src/oah/templates/rules/block.english.md": "template\n"},
            "other src package": {"src/another/extra.json": "{}\n"},
            "extensionless file": {"src/oah/NOTICE": "notice\n"},
        }
        for label, change in cases.items():
            with self.subTest(change=label):
                files = dict(BASE_FILES, **{"src/oah/templates/rules/block.en.md": "template\n"})
                repo = Repo(self, files)
                self.assertWhole(repo.select(repo.head(change)), "is a non-Python file under src/")

    def test_rule6_an_unreached_file_under_tests_runs_the_whole_suite(self):
        repo = Repo(self)
        self.assertWhole(repo.select(repo.head({"tests/fixtures/data.json": "[]\n"})),
                         "tests/fixtures/data.json reaches no test module")

    def test_rule6_files_outside_src_and_tests_may_select_nothing(self):
        repo = Repo(self)
        selection = repo.select(repo.head({"LICENSE": "new license\n"}))
        self.assertSelected(selection, [])
        self.assertEqual(selection.count, 1)

    def test_rule6_src_without_child_tests_and_importers_runs_the_whole_suite(self):
        files = {name: text for name, text in BASE_FILES.items()
                 if name not in ("tests/test_child.py", "tests/test_fake.py", "tests/fakes/fake_tool.py")}
        repo = Repo(self, files)
        self.assertWhole(repo.select(repo.head({"src/oah/new.py": "X = 1\n"})), "src/oah/new.py reaches no test module")


class FallbackTest(unittest.TestCase):
    def test_a_manual_run_runs_the_whole_suite(self):
        repo = Repo(self)
        head = repo.head({"tests/test_plain.py": case("pass  # changed")})
        selection = repo.select(head, event="workflow_dispatch")
        self.assertTrue(selection.whole)
        self.assertIn("workflow_dispatch", selection.reason)

    def test_a_missing_or_bad_base_runs_the_whole_suite(self):
        repo = Repo(self)
        head = repo.head({"tests/test_plain.py": case("pass  # changed")})
        for base in ("", "0" * 40, "no-such-ref", "--output=x"):
            with self.subTest(base=base):
                selection = repo.select(head, base=base)
                self.assertTrue(selection.whole, selection)
                self.assertEqual(selection.count, 6)

    def test_missing_git_runs_the_whole_suite(self):
        repo = Repo(self)
        head = repo.head({"tests/test_plain.py": case("pass  # changed")})
        empty = repo.path.parent / "no-tools"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            selection = repo.select(head)
        self.assertTrue(selection.whole)
        self.assertIn("selector error", selection.reason)

    def test_a_syntax_error_in_a_scanned_file_runs_the_whole_suite(self):
        repo = Repo(self)
        selection = repo.select(repo.head({"src/oah/broken.py": "def (:\n"}))
        self.assertTrue(selection.whole)
        self.assertIn("SyntaxError", selection.reason)

    def test_a_syntax_error_in_an_imported_fixture_runs_the_whole_suite(self):
        files = dict(BASE_FILES, **{
            "tests/test_helper.py": "from tests.fixtures.support import run\n" + case(),
            "tests/fixtures/support.py": "def (:\n",
        })
        repo = Repo(self, files)
        selection = repo.select(repo.head({"tests/test_plain.py": case("pass  # changed")}))
        self.assertTrue(selection.whole)
        self.assertIn("SyntaxError", selection.reason)

    def test_unreadable_timing_data_runs_the_whole_suite(self):
        files = {name: text for name, text in BASE_FILES.items() if name != "tools/test_shard_times.json"}
        repo = Repo(self, files)
        selection = repo.select(repo.head({"tests/test_plain.py": case("pass  # changed")}))
        self.assertTrue(selection.whole)
        self.assertIn("test_shard_times.json", selection.reason)

    def test_the_cli_reports_any_error_as_the_whole_suite(self):
        with mock.patch.object(tool, "_select", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(tool.main(["--base", "x", "--head", "y"]), 0)
        self.assertEqual(out.getvalue(), "ALL\n")


class OutputTest(unittest.TestCase):
    def run_main(self, *args: str) -> str:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(tool.main(list(args)), 0)
        return out.getvalue()

    def test_same_inputs_give_byte_identical_output_in_any_file_order(self):
        outputs = []
        for reverse in (False, True):
            repo = Repo(self, reverse=reverse)
            head = repo.head({"src/oah/c.py": "VALUE = 3\n", "install/setup.sh": "echo 3\n"})
            for _ in range(2):
                outputs.append(self.run_main("--repo", str(repo.path), "--base", repo.base, "--head", head,
                                             "--summary"))
        self.assertEqual(len(set(outputs)), 1, outputs)
        self.assertEqual(outputs[0], "selected 5 of 7 test modules (1 shard)\n"
                                     "  tests.test_a\n  tests.test_child\n  tests.test_fake\n  tests.test_lazy\n"
                                     "  tests.test_text\n")

    def test_plain_output_and_github_output_for_a_selection(self):
        repo = Repo(self)
        head = repo.head({"tests/test_plain.py": case("pass  # changed")})
        github = repo.path.parent / "github-output"
        out = self.run_main("--repo", str(repo.path), "--base", repo.base, "--head", head,
                            "--github-output", str(github))
        self.assertEqual(out, "tests.test_plain\n")
        self.assertEqual(github.read_text(encoding="utf-8"),
                         "count=1\nshards=[1]\nmodules=tests.test_plain\n")

    def test_an_empty_selection_outputs_no_modules_and_one_shard(self):
        repo = Repo(self)
        head = repo.head({"LICENSE": "changed\n"})
        github = repo.path.parent / "github-output"
        out = self.run_main("--repo", str(repo.path), "--base", repo.base, "--head", head, "--summary",
                            "--github-output", str(github))
        self.assertEqual(out, "selected 0 of 7 test modules (1 shard): no affected tests\n")
        self.assertEqual(github.read_text(encoding="utf-8"), "count=1\nshards=[1]\nmodules=NONE\n")

    def test_the_whole_suite_outputs_all_modules_and_six_shards(self):
        repo = Repo(self)
        head = repo.head({"tests/helpers.py": "# changed\n"})
        github = repo.path.parent / "github-output"
        out = self.run_main("--repo", str(repo.path), "--base", repo.base, "--head", head,
                            "--github-output", str(github))
        self.assertEqual(out, "ALL\n")
        self.assertEqual(github.read_text(encoding="utf-8"),
                         "count=6\nshards=[1, 2, 3, 4, 5, 6]\nmodules=ALL\n")


class ShardCountTest(unittest.TestCase):
    def test_shard_count_from_weights(self):
        shard = tool.SHARD_MS
        self.assertEqual(tool.shard_count([]), 1)
        self.assertEqual(tool.shard_count([1]), 1)
        self.assertEqual(tool.shard_count([shard]), 1)
        self.assertEqual(tool.shard_count([shard, 1]), 2)
        self.assertEqual(tool.shard_count([shard, shard, 1]), 3)
        self.assertEqual(tool.shard_count([shard * 4, shard]), 5)
        self.assertEqual(tool.shard_count([shard * 5, 1]), 6)
        self.assertEqual(tool.shard_count([shard * 40]), 6)

    def test_module_weight_counts_listed_and_unlisted_tests(self):
        times = {"tests.test_m.T.test_a": 7, "tests.test_m.T.test_gone": 3, "tests.test_mx.T.test_a": 100}
        ids = ["tests.test_m.T.test_a", "tests.test_m.T.test_b"]
        self.assertEqual(tool.module_weight("tests.test_m", ids, 50, times), 7 + 50 + 3)

    def test_a_heavy_selection_needs_more_shards(self):
        files = dict(BASE_FILES)
        files["tools/test_shard_times.json"] = json.dumps({"default_ms": 1000, "source": "s", "times_ms": {
            "tests.test_a.T.test_x": tool.SHARD_MS * 2, "tests.test_child.T.test_x": tool.SHARD_MS // 2}})
        repo = Repo(self, files)
        selection = repo.select(repo.head({"src/oah/c.py": "VALUE = 2\n"}))
        self.assertEqual(selection.count, 3)


class WorkflowTest(unittest.TestCase):
    def test_ci_runs_the_selection_and_falls_back_to_the_whole_suite(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('    paths-ignore:\n      - "docs/**"\n      - "*.md"\n  workflow_dispatch:\n', text)
        self.assertIn("  select:\n    runs-on: ubuntu-latest\n", text)
        self.assertIn("    needs: select\n", text)
        self.assertEqual(text.count("        continue-on-error: true\n"), 1)
        self.assertEqual(text.count("python tools/affected_tests.py --event \"$OAH_EVENT\" --base \"$OAH_BASE\" "
                                    "--head \"$OAH_HEAD\" --summary"), 1)
        select, test = text.split("  test:\n", 1)
        ready = "steps.affected.outcome == 'success' && steps.affected.outputs.modules"
        self.assertIn("count: ${{ " + ready + " && steps.affected.outputs.count || 6 }}", select)
        self.assertIn("shards: ${{ " + ready + " && steps.affected.outputs.shards || '[1, 2, 3, 4, 5, 6]' }}", select)
        self.assertIn("modules: ${{ " + ready + " || 'ALL' }}", select)
        self.assertNotIn("affected_tests.py", test)
        self.assertNotIn("continue-on-error", test)
        self.assertIn("OAH_TEST_MODULES: ${{ needs.select.outputs.modules || 'ALL' }}", test)
        self.assertIn("--modules-env OAH_TEST_MODULES -v", test)
        self.assertIn("name: test (${{ matrix.os }}, ${{ matrix.shard }}/${{ needs.select.outputs.count || 6 }})", text)
        self.assertEqual(text.count("if: matrix.shard == 1"), 4)
        self.assertEqual(text.count("if: runner.os == 'macOS' && matrix.shard == 1"), 1)
        self.assertEqual(text.count("if: runner.os == 'Windows' && matrix.shard == 1"), 2)


class RealTreeTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("needs the repository's git history")

    def test_this_lanes_own_diff_runs_the_whole_suite(self):
        present = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{LANE_BASE}^{{commit}}"],
                                 capture_output=True).returncode == 0
        selection = tool.select(ROOT, LANE_BASE, "HEAD")
        self.assertTrue(selection.whole, selection.modules)
        if present:
            self.assertIn("is shared test infrastructure", selection.reason)

    def test_the_real_tree_scans_and_a_journal_change_selects_fewer_than_all(self):
        tree = tool.Tree.read(ROOT, "HEAD")
        graph = tool.Graph(tree, ["src/oah/journal.py"])
        self.assertEqual(set(graph.depends), set(tree.test_modules))
        found = graph.tests_for("src/oah/journal.py")
        self.assertIn("tests/test_journal.py", found)
        self.assertLess(len(found), len(tree.test_modules))
        self.assertNotIn("tests/test_paths.py", found)


if __name__ == "__main__":
    unittest.main()
