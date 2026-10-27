# 평가 실행기 검사 — 사례 형식, 새 작업 공간과 격리 검사, 사전 등록, 변형 실행, 채점, 보고서를 합성 픽스처로 본다.
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import unittest
import zipfile

from oah.eval import case as case_mod
from oah.eval import grade, plan, report, runner, workspace
from oah.eval.errors import EvalError
from tests.helpers import make_tmp_dir

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "eval"
SNAPSHOT = "11ac4e9c014277808ff6cba43db7ee4adfa650aa"
SUPERIORITY = re.compile(r"\b(better|best|superior|wins?|winner|outperform\w*|beats?)\b", re.IGNORECASE)


def fixture_copy(test: unittest.TestCase) -> pathlib.Path:
    """A private, editable copy of tests/fixtures/eval."""
    target = make_tmp_dir(test) / "eval"
    shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def case_dir(root: pathlib.Path) -> pathlib.Path:
    return root / "cases" / "derived-index"


def oracle_path(root: pathlib.Path) -> pathlib.Path:
    return root / "oracles" / "derived-index.toml"


def set_case(root: pathlib.Path, old: str, new: str) -> None:
    path = case_dir(root) / "case.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def set_plan(root: pathlib.Path, old: str, new: str) -> None:
    path = root / "plan.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout.strip()


def objects(repo: pathlib.Path) -> set[str]:
    out = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    return set(out.split())


def snapshot_tree() -> str:
    return "11ac4e9c014277808ff6cba43db7ee4adfa650aa^{tree}"


class Env:
    """Run settings for a test: a run root and the variant environment."""

    def __init__(self, test: unittest.TestCase) -> None:
        base = make_tmp_dir(test)
        self.runs = base / "runs"
        self.marker = base / "marker.txt"
        self.records = base / "records"
        self.records.mkdir()
        self.env = dict(os.environ, FAKE_EVAL_MARKER=str(self.marker), FAKE_EVAL_RECORD_DIR=str(self.records))
        self.env.pop("FAKE_EVAL_EXIT", None)

    def launched(self) -> list[str]:
        return self.marker.read_text(encoding="utf-8").split() if self.marker.exists() else []


def run_fixture(test: unittest.TestCase, root: pathlib.Path, env: Env, *, frozen: bool = False):
    source = root / "plan.toml"
    if frozen:
        source = root / "prereg.json"
        plan.preregister(plan.load_plan(root / "plan.toml"), [oracle_path(root)], source)
    return runner.run_eval(plan.load_run_source(source), env.runs, env=env.env, timeout_seconds=120)


def read_result(run_dir: pathlib.Path, variant: str) -> dict:
    return json.loads((run_dir / "cases" / "derived-index" / variant / "result.json").read_text(encoding="utf-8"))


class CaseTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)

    def test_loads_fixture_case(self):
        case = case_mod.load_case(case_dir(self.root))
        self.assertEqual((case.id, case.split, case.snapshot_format, case.snapshot_commit),
                         ("derived-index", "control", "bundle", SNAPSHOT))
        self.assertEqual(case.requirement.name, "requirement.md")
        self.assertEqual(case.inputs, case_dir(self.root) / "inputs")
        self.assertEqual(case.forbidden, ("*.orig", "fix.patch"))
        self.assertIn("Synthetic", case.provenance)
        self.assertRegex(case_mod.case_digest(case_dir(self.root)), r"^[0-9a-f]{64}$")

    def test_refuses_missing_field(self):
        set_case(self.root, 'prompt = "task.md"\n', "")
        with self.assertRaises(EvalError) as caught:
            case_mod.load_case(case_dir(self.root))
        self.assertEqual(caught.exception.code, "OAH_EVAL_CASE_INVALID")
        self.assertIn("prompt", str(caught.exception))

    def test_refuses_unknown_split(self):
        set_case(self.root, 'split = "control"', 'split = "test"')
        with self.assertRaises(EvalError) as caught:
            case_mod.load_case(case_dir(self.root))
        self.assertEqual(caught.exception.code, "OAH_EVAL_CASE_INVALID")
        self.assertIn("dev, control or holdout", str(caught.exception))

    def test_refuses_path_outside_case(self):
        for old, new in (('requirement = "requirement.md"', 'requirement = "../requirement.md"'),
                         ('file = "snapshot.bundle"', 'file = "/abs/snapshot.bundle"')):
            with self.subTest(new=new):
                root = fixture_copy(self)
                set_case(root, old, new)
                with self.assertRaises(EvalError) as caught:
                    case_mod.load_case(case_dir(root))
                self.assertEqual(caught.exception.code, "OAH_EVAL_CASE_INVALID")

    def test_refuses_missing_inputs_folder(self):
        shutil.rmtree(case_dir(self.root) / "inputs")
        with self.assertRaises(EvalError) as caught:
            case_mod.load_case(case_dir(self.root))
        self.assertIn("inputs", str(caught.exception))

    def test_accepts_crlf(self):
        path = case_dir(self.root) / "case.toml"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(case_mod.load_case(case_dir(self.root)).id, "derived-index")


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.scratch = make_tmp_dir(self)

    def build(self, name: str = "ws"):
        snapshot = workspace.open_snapshot(case_mod.load_case(case_dir(self.root)), self.scratch / "extract")
        return snapshot, workspace.build_workspace(snapshot, self.scratch / name)

    def test_one_parentless_commit_with_snapshot_tree(self):
        snapshot, ws = self.build()
        self.assertEqual(git(ws.repo, "rev-list", "--all", "--count"), "1")
        self.assertEqual(git(ws.repo, "rev-list", "--parents", "-n", "1", "HEAD"), ws.commit)
        self.assertEqual(git(ws.repo, "rev-parse", "HEAD^{tree}"), snapshot.tree)
        self.assertNotEqual(ws.commit, SNAPSHOT)
        self.assertEqual(git(ws.repo, "status", "--porcelain"), "")
        self.assertIn("kiwi", (ws.repo / "words.txt").read_text(encoding="utf-8"))
        self.assertNotIn("kiwi", (ws.repo / "index.txt").read_text(encoding="utf-8"))
        self.assertEqual(list(ws.output.iterdir()), [])
        self.assertEqual((ws.inputs / "notes.md").read_bytes(), (case_dir(self.root) / "inputs" / "notes.md").read_bytes())
        self.assertEqual(ws.requirement.read_bytes(), (case_dir(self.root) / "requirement.md").read_bytes())

    def test_no_history_or_later_objects(self):
        snapshot, ws = self.build()
        found = objects(ws.repo)
        self.assertEqual(found & snapshot.history_objects, set())
        self.assertIn(SNAPSHOT, snapshot.history_objects)
        self.assertIn("7ffdbf7207603fcb7a806aa45147c55a9dc9315e", snapshot.history_objects)  # the later fix
        self.assertEqual(found - {ws.commit}, set(snapshot.tree_objects))
        self.assertFalse(any(ws.root.rglob("*.bundle")))
        workspace.scan_workspace(ws, snapshot, oracle_hashes=workspace.content_hashes(
            oracle_path(self.root).read_bytes()))

    def test_each_workspace_is_fresh_with_the_same_start(self):
        _, first = self.build("a")
        _, second = self.build("b")
        self.assertNotEqual(first.repo, second.repo)
        self.assertEqual(first.commit, second.commit)

    def test_tar_and_zip_snapshots(self):
        snapshot, _ = self.build()
        extract = make_tmp_dir(self) / "src"
        subprocess.run(["git", "clone", "-q", str(case_dir(self.root) / "snapshot.bundle"), str(extract)],
                       check=True)
        for fmt in ("tar", "zip"):
            with self.subTest(fmt=fmt):
                root = fixture_copy(self)
                archive = case_dir(root) / f"snapshot.{fmt}"
                git(extract, "archive", f"--format={fmt}", "-o", str(archive), SNAPSHOT)
                (case_dir(root) / "snapshot.bundle").unlink()
                set_case(root, 'file = "snapshot.bundle"', f'file = "snapshot.{fmt}"')
                set_case(root, 'format = "bundle"', f'format = "{fmt}"')
                source = workspace.open_snapshot(case_mod.load_case(case_dir(root)), make_tmp_dir(self) / "x")
                ws = workspace.build_workspace(source, make_tmp_dir(self) / "ws")
                self.assertEqual(git(ws.repo, "rev-parse", "HEAD^{tree}"), snapshot.tree)
                self.assertEqual(git(ws.repo, "rev-list", "--all", "--count"), "1")
                workspace.scan_workspace(ws, source)

    def test_tar_with_another_commit_id_refused(self):
        extract = make_tmp_dir(self) / "src"
        subprocess.run(["git", "clone", "-q", str(case_dir(self.root) / "snapshot.bundle"), str(extract)],
                       check=True)
        git(extract, "archive", "--format=tar", "-o", str(case_dir(self.root) / "snapshot.tar"), "main")
        set_case(self.root, 'file = "snapshot.bundle"', 'file = "snapshot.tar"')
        set_case(self.root, 'format = "bundle"', 'format = "tar"')
        with self.assertRaises(EvalError) as caught:
            workspace.open_snapshot(case_mod.load_case(case_dir(self.root)), self.scratch / "x")
        self.assertEqual(caught.exception.code, "OAH_EVAL_SNAPSHOT")

    def test_archive_member_outside_refused(self):
        archive = case_dir(self.root) / "snapshot.tar"
        payload = self.scratch / "evil.txt"
        payload.write_text("x", encoding="utf-8")
        with tarfile.open(archive, "w") as tar:
            tar.add(payload, arcname="../evil.txt")
        set_case(self.root, 'file = "snapshot.bundle"', 'file = "snapshot.tar"')
        set_case(self.root, 'format = "bundle"', 'format = "tar"')
        source = workspace.open_snapshot(case_mod.load_case(case_dir(self.root)), self.scratch / "x")
        with self.assertRaises(EvalError) as caught:
            workspace.build_workspace(source, self.scratch / "ws")
        self.assertEqual(caught.exception.code, "OAH_EVAL_SNAPSHOT")
        self.assertFalse((self.scratch / "ws" / "evil.txt").exists())

    def test_zip_member_outside_refused(self):
        archive = case_dir(self.root) / "snapshot.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../evil.txt", "x")
        set_case(self.root, 'file = "snapshot.bundle"', 'file = "snapshot.zip"')
        set_case(self.root, 'format = "bundle"', 'format = "zip"')
        source = workspace.open_snapshot(case_mod.load_case(case_dir(self.root)), self.scratch / "x")
        with self.assertRaises(EvalError) as caught:
            workspace.build_workspace(source, self.scratch / "ws")
        self.assertEqual(caught.exception.code, "OAH_EVAL_SNAPSHOT")

    def test_missing_commit_refused(self):
        set_case(self.root, SNAPSHOT, "0" * 40)
        with self.assertRaises(EvalError) as caught:
            workspace.open_snapshot(case_mod.load_case(case_dir(self.root)), self.scratch / "x")
        self.assertEqual(caught.exception.code, "OAH_EVAL_SNAPSHOT")
        self.assertIn("0" * 40, str(caught.exception))


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.scratch = make_tmp_dir(self)
        self.oracle = oracle_path(self.root).read_bytes()

    def build(self):
        snapshot = workspace.open_snapshot(case_mod.load_case(case_dir(self.root)), self.scratch / "extract")
        return snapshot, workspace.build_workspace(snapshot, self.scratch / "ws")

    def refused(self, snapshot, ws) -> str:
        with self.assertRaises(EvalError) as caught:
            workspace.scan_workspace(ws, snapshot, oracle_hashes=workspace.content_hashes(self.oracle))
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        return str(caught.exception)

    def test_oracle_copy_refused(self):
        (case_dir(self.root) / "inputs" / "hint.toml").write_bytes(self.oracle)
        message = self.refused(*self.build())
        self.assertIn("oracle", message)
        self.assertIn("hint.toml", message)

    def test_oracle_copy_with_crlf_refused(self):
        snapshot, ws = self.build()
        (ws.repo / "notes" ).mkdir()
        (ws.repo / "notes" / "answers.toml").write_bytes(self.oracle.replace(b"\n", b"\r\n"))
        self.assertIn("notes/answers.toml", self.refused(snapshot, ws))

    def test_history_object_refused(self):
        snapshot, ws = self.build()
        git(ws.repo, "fetch", "-q", str(case_dir(self.root) / "snapshot.bundle"), "main")
        message = self.refused(snapshot, ws)
        self.assertIn("original history", message)

    def test_alternates_refused(self):
        snapshot, ws = self.build()
        alternates = ws.repo / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(str(self.scratch / "elsewhere") + "\n", encoding="utf-8")
        self.assertIn("alternates", self.refused(snapshot, ws))

    def test_snapshot_file_copy_refused(self):
        shutil.copy(case_dir(self.root) / "snapshot.bundle", case_dir(self.root) / "inputs" / "copy.bin")
        self.assertIn("copy.bin", self.refused(*self.build()))

    def test_forbidden_file_refused(self):
        snapshot, ws = self.build()
        (ws.repo / "fix.patch").write_text("diff", encoding="utf-8")
        (ws.inputs / "words.txt.orig").write_text("old", encoding="utf-8")
        message = self.refused(snapshot, ws)
        self.assertIn("fix.patch", message)
        self.assertIn("words.txt.orig", message)


class PreregTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.out = self.root / "prereg.json"

    def test_writes_frozen_json_and_sha256(self):
        digest = plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], self.out)
        raw = self.out.read_bytes()
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual((self.root / "prereg.json.sha256").read_text(encoding="utf-8").split()[0], digest)
        data = json.loads(raw)
        self.assertEqual(data["format"], "oah-eval-prereg/1")
        [entry] = data["cases"]
        self.assertEqual((entry["id"], entry["split"], entry["used_for_development"], entry["snapshot_commit"]),
                         ("derived-index", "control", False, SNAPSHOT))
        self.assertEqual(entry["case_sha256"], case_mod.case_digest(case_dir(self.root)))
        self.assertEqual(entry["oracle_sha256"], hashlib.sha256(oracle_path(self.root).read_bytes()).hexdigest())
        self.assertNotIn(str(oracle_path(self.root)), raw.decode("utf-8"))
        self.assertEqual([v["name"] for v in data["variants"]], ["baseline", "improved"])
        self.assertEqual(data["variants"][0]["command"], ["{python}", "{plan_dir}/variants/fake_variant.py", "miss"])
        self.assertTrue(data["variants"][0]["fake"])
        self.assertIn("variants/fake_variant.py", json.dumps(data["variants"][0]["files"]))
        self.assertIn("matched", data["metrics"])
        self.assertEqual(data["grader"]["unmatched_findings"], "human-judgement")
        frozen = plan.load_frozen(self.out)
        self.assertEqual(frozen.sha256, digest)

    def test_embeds_variant_config(self):
        (self.root / "variant.toml").write_text("[review]\nmax_rounds = 3\n", encoding="utf-8")
        set_plan(self.root, 'name = "improved"', 'name = "improved"\nconfig = "variant.toml"')
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], self.out)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertIsNone(data["variants"][0]["config_text"])
        self.assertEqual(data["variants"][1]["config_text"], "[review]\nmax_rounds = 3\n")

    def test_oracle_inside_case_refused(self):
        inside = case_dir(self.root) / "oracle.toml"
        shutil.copy(oracle_path(self.root), inside)
        with self.assertRaises(EvalError) as caught:
            plan.preregister(plan.load_plan(self.root / "plan.toml"), [inside], self.out)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_IN_CASE")
        self.assertFalse(self.out.exists())

    def test_missing_oracle_refused(self):
        with self.assertRaises(EvalError) as caught:
            plan.preregister(plan.load_plan(self.root / "plan.toml"), [], self.out)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_MISSING")

    def test_holdout_used_for_development_refused(self):
        set_case(self.root, 'split = "control"', 'split = "holdout"')
        set_plan(self.root, "used_for_development = false", "used_for_development = true")
        with self.assertRaises(EvalError) as caught:
            plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], self.out)
        self.assertEqual(caught.exception.code, "OAH_EVAL_HOLDOUT_USED")
        self.assertIn('split = "dev"', str(caught.exception))

    def test_tampered_prereg_refused(self):
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], self.out)
        self.out.write_text(self.out.read_text(encoding="utf-8").replace('"miss"', '"catch"'), encoding="utf-8")
        with self.assertRaises(EvalError) as caught:
            plan.load_frozen(self.out)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_HASH")
        with self.assertRaises(EvalError):
            plan.load_frozen(self.out, expect_sha256="0" * 64)

    def test_missing_sidecar_needs_expected_hash(self):
        digest = plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], self.out)
        (self.root / "prereg.json.sha256").unlink()
        with self.assertRaises(EvalError) as caught:
            plan.load_frozen(self.out)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_HASH")
        self.assertEqual(plan.load_frozen(self.out, expect_sha256=digest).sha256, digest)

    def test_plan_errors(self):
        for old, new in (('name = "improved"', 'name = "baseline"'),
                         ('command = ["{python}", "{plan_dir}/variants/fake_variant.py", "miss"]', "command = []"),
                         ("fake = true\ntimeout_seconds = 120\n\n[[variant]]", "fake = 1\n\n[[variant]]")):
            with self.subTest(new=new):
                root = fixture_copy(self)
                set_plan(root, old, new)
                with self.assertRaises(EvalError) as caught:
                    plan.load_plan(root / "plan.toml")
                self.assertEqual(caught.exception.code, "OAH_EVAL_PLAN_INVALID")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.env = Env(self)

    def test_holdout_without_prereg_refused(self):
        set_case(self.root, 'split = "control"', 'split = "holdout"')
        with self.assertRaises(EvalError) as caught:
            run_fixture(self, self.root, self.env)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_REQUIRED")
        self.assertIn("oah eval prereg", str(caught.exception))
        self.assertEqual(self.env.launched(), [])

    def test_holdout_case_changed_refused(self):
        set_case(self.root, 'split = "control"', 'split = "holdout"')
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], self.root / "p.json")
        (case_dir(self.root) / "requirement.md").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_run_source(self.root / "p.json"), self.env.runs, env=self.env.env,
                            timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_MISMATCH")
        self.assertEqual(self.env.launched(), [])

    def test_prereg_dev_used_holdout_refused(self):
        out = self.root / "p.json"
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        data = json.loads(out.read_text(encoding="utf-8"))
        data["cases"][0].update(split="holdout", used_for_development=True)
        out.write_text(json.dumps(data), encoding="utf-8")
        (self.root / "p.json.sha256").write_text(hashlib.sha256(out.read_bytes()).hexdigest() + "  p.json\n",
                                                 encoding="utf-8")
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_run_source(out), self.env.runs, env=self.env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_HOLDOUT_USED")
        self.assertEqual(self.env.launched(), [])

    def test_changed_variant_script_refused(self):
        out = self.root / "p.json"
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        script = self.root / "variants" / "fake_variant.py"
        script.write_text(script.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_run_source(out), self.env.runs, env=self.env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_MISMATCH")
        self.assertIn("fake_variant.py", str(caught.exception))

    def test_isolation_refused_before_any_variant(self):
        (case_dir(self.root) / "inputs" / "hint.toml").write_bytes(oracle_path(self.root).read_bytes())
        with self.assertRaises(EvalError) as caught:
            run_fixture(self, self.root, self.env, frozen=True)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertEqual(self.env.launched(), [])

    def test_missing_command_refused_before_any_variant(self):
        set_plan(self.root, '["{python}", "{plan_dir}/variants/fake_variant.py", "catch"]',
                 '["oah-eval-no-such-tool-xyz"]')
        with self.assertRaises(EvalError) as caught:
            run_fixture(self, self.root, self.env)
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("OAH_EVAL_COMMAND_MISSING", 69))
        self.assertEqual(self.env.launched(), [])

    def test_variants_get_identical_inputs(self):
        run_dir = run_fixture(self, self.root, self.env)
        self.assertEqual(self.env.launched(), ["miss", "catch"])
        seen = {mode: json.loads((self.env.records / f"{mode}.json").read_text(encoding="utf-8"))
                for mode in ("miss", "catch")}
        self.assertNotEqual(seen["miss"].pop("cwd"), seen["catch"].pop("cwd"))
        self.assertEqual(seen["miss"], seen["catch"])
        self.assertEqual(seen["miss"]["commits"], "1")
        self.assertTrue(seen["miss"]["output_empty"])
        self.assertEqual(seen["miss"]["prompt"], (case_dir(self.root) / "task.md").read_bytes().decode("utf-8"))
        self.assertEqual(read_result(run_dir, "baseline")["workspace_commit"],
                         read_result(run_dir, "improved")["workspace_commit"])

    def test_records_exit_time_and_unknowns(self):
        self.env.env["FAKE_EVAL_EXIT"] = "3"
        run_dir = run_fixture(self, self.root, self.env)
        base, improved = read_result(run_dir, "baseline"), read_result(run_dir, "improved")
        self.assertEqual((base["exit_code"], base["timed_out"]), (3, False))
        self.assertIsInstance(base["wall_seconds"], float)
        self.assertEqual((base["usage"], base["rounds"], base["corrections"]), ("unknown", "unknown", "unknown"))
        self.assertEqual(improved["usage"], {"input_tokens": 120, "output_tokens": 30})
        self.assertEqual((improved["rounds"], improved["corrections"]), (1, 1))
        self.assertEqual(base["label"], "control-flow-only")
        self.assertEqual(base["findings"][0]["id"], "F1")
        self.assertTrue((run_dir / "cases" / "derived-index" / "improved" / "workspace" / "index.txt").is_file())
        self.assertIn("fig", (run_dir / "cases" / "derived-index" / "improved" / "workspace" / "index.txt")
                      .read_text(encoding="utf-8"))
        self.assertTrue((run_dir / "cases" / "derived-index" / "baseline" / "stdout.txt").is_file())

    def test_real_engine_label_and_missing_output(self):
        script = self.root / "variants" / "silent.py"
        script.write_text("# 출력 없는 변형\nimport sys\nsys.stdin.read()\n", encoding="utf-8")
        set_plan(self.root, '["{python}", "{plan_dir}/variants/fake_variant.py", "catch"]\nfake = true',
                 '["{python}", "{plan_dir}/variants/silent.py"]\nfake = false')
        run_dir = run_fixture(self, self.root, self.env)
        silent = read_result(run_dir, "improved")
        self.assertEqual(silent["label"], "engine-run")
        self.assertEqual(silent["findings"], [])
        self.assertEqual(silent["output"], "missing")
        self.assertEqual(silent["usage"], "unknown")

    def test_prereg_hash_in_every_result(self):
        run_dir = run_fixture(self, self.root, self.env, frozen=True)
        digest = (self.root / "prereg.json.sha256").read_text(encoding="utf-8").split()[0]
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["prereg_sha256"], digest)
        for variant in ("baseline", "improved"):
            self.assertEqual(read_result(run_dir, variant)["prereg_sha256"], digest)

    def test_unfrozen_run_records_no_prereg_and_unchecked_oracle(self):
        run_dir = run_fixture(self, self.root, self.env)
        result = read_result(run_dir, "baseline")
        self.assertIsNone(result["prereg_sha256"])
        self.assertEqual(result["isolation"]["oracle"], "not-checked")


class GradeTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.env = Env(self)

    def finding(self, file: str, summary: str, fid: str = "F1") -> dict:
        return {"id": fid, "file": file, "summary": summary}

    def test_defect_needs_file_and_keywords(self):
        defects = [grade.Defect(id="D1", files=("index.txt",), keywords=("stale",), summary="")]
        cases = {
            "both": ([self.finding("index.txt", "The index is STALE")], ["D1"]),
            "./ path": ([self.finding("./index.txt", "stale index")], ["D1"]),
            "no keyword": ([self.finding("index.txt", "index looks odd")], []),
            "wrong file": ([self.finding("words.txt", "stale words")], []),
        }
        for label, (findings, matched) in cases.items():
            with self.subTest(label=label):
                result = grade.match_findings(defects, findings)
                self.assertEqual([m["defect"] for m in result["matched"]], matched)
                self.assertEqual(result["missed"], [] if matched else ["D1"])
                self.assertEqual(len(result["unmatched"]), 0 if matched else 1)

    def test_unmatched_findings_need_human(self):
        run_dir = run_fixture(self, self.root, self.env, frozen=True)
        graded = grade.grade_run(run_dir, [oracle_path(self.root)], env=self.env.env)
        base = graded["cases"]["derived-index"]["variants"]["baseline"]
        self.assertEqual(base["missed"], ["D1"])
        self.assertEqual(base["unmatched"], [{"id": "F1", "file": "words.txt",
                                              "text": "words.txt is not sorted alphabetically",
                                              "verdict": "needs-human-judgement"}])
        self.assertNotIn("false-positive", json.dumps(graded))
        improved = graded["cases"]["derived-index"]["variants"]["improved"]
        self.assertEqual(improved["matched"], [{"defect": "D1", "findings": ["F1"]}])
        self.assertEqual((improved["missed"], improved["unmatched"]), ([], []))
        self.assertTrue((run_dir / "grade.json").is_file())

    def test_human_verdict_marks_false_positive(self):
        run_dir = run_fixture(self, self.root, self.env)
        verdicts = self.root / "verdicts.toml"
        verdicts.write_text('[[verdict]]\ncase = "derived-index"\nvariant = "baseline"\nfinding = "F1"\n'
                            'verdict = "false-positive"\nnote = "sorting is not required"\n', encoding="utf-8")
        graded = grade.grade_run(run_dir, [oracle_path(self.root)], verdicts_path=verdicts, env=self.env.env)
        [item] = graded["cases"]["derived-index"]["variants"]["baseline"]["unmatched"]
        self.assertEqual(item["verdict"], "false-positive")
        verdicts.write_text('[[verdict]]\ncase = "derived-index"\nvariant = "baseline"\nfinding = "F1"\n'
                            'verdict = "probably-fine"\n', encoding="utf-8")
        with self.assertRaises(EvalError) as caught:
            grade.grade_run(run_dir, [oracle_path(self.root)], verdicts_path=verdicts, env=self.env.env)
        self.assertEqual(caught.exception.code, "OAH_EVAL_VERDICT_INVALID")

    def test_test_rule_snapshot_fails_variant_fixed(self):
        run_dir = run_fixture(self, self.root, self.env)
        graded = grade.grade_run(run_dir, [oracle_path(self.root)], env=self.env.env)
        variants = graded["cases"]["derived-index"]["variants"]
        self.assertEqual(graded["cases"]["derived-index"]["tests"], [{"id": "T1", "snapshot": "fails"}])
        self.assertEqual(variants["baseline"]["tests"], [{"id": "T1", "result": "not-fixed"}])
        self.assertEqual(variants["improved"]["tests"], [{"id": "T1", "result": "fixed"}])
        # The saved workspace is graded on a copy and stays unchanged.
        self.assertEqual(git(run_dir / "cases" / "derived-index" / "improved" / "workspace", "status",
                             "--porcelain"), "M index.txt")

    def test_test_rule_passing_on_snapshot_is_reported(self):
        run_dir = run_fixture(self, self.root, self.env)
        oracle = oracle_path(self.root)
        oracle.write_text(oracle.read_text(encoding="utf-8").replace('"check_index.py"', '"build_index.py"'),
                          encoding="utf-8")
        graded = grade.grade_run(run_dir, [oracle], env=self.env.env)
        self.assertEqual(graded["cases"]["derived-index"]["tests"],
                         [{"id": "T1", "snapshot": "passes (oracle-invalid)"}])
        self.assertEqual(graded["cases"]["derived-index"]["variants"]["improved"]["tests"],
                         [{"id": "T1", "result": "oracle-invalid"}])

    def test_oracle_changed_since_prereg_refused(self):
        run_dir = run_fixture(self, self.root, self.env, frozen=True)
        oracle = oracle_path(self.root)
        oracle.write_text(oracle.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(EvalError) as caught:
            grade.grade_run(run_dir, [oracle], env=self.env.env)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_CHANGED")

    def test_oracle_inside_case_refused(self):
        run_dir = run_fixture(self, self.root, self.env)
        inside = case_dir(self.root) / "oracle.toml"
        shutil.copy(oracle_path(self.root), inside)
        with self.assertRaises(EvalError) as caught:
            grade.grade_run(run_dir, [inside], env=self.env.env)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_IN_CASE")

    def test_oracle_errors(self):
        oracle = self.root / "bad.toml"
        for body in ('case = "derived-index"\n[[defect]]\nid = "D1"\n',
                     '[[defect]]\nid = "D1"\nfiles = ["a"]\n',
                     'case = "derived-index"\n[[test]]\nid = "T1"\ncommand = "check"\n'):
            with self.subTest(body=body):
                oracle.write_text(body, encoding="utf-8")
                with self.assertRaises(EvalError) as caught:
                    grade.load_oracle(oracle)
                self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_INVALID")


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.env = Env(self)
        self.run_dir = run_fixture(self, self.root, self.env, frozen=True)

    def test_not_graded_refused(self):
        with self.assertRaises(EvalError) as caught:
            report.build_report(self.run_dir)
        self.assertEqual(caught.exception.code, "OAH_EVAL_NOT_GRADED")
        self.assertIn("oah eval grade", str(caught.exception))

    def test_aggregates_by_split_and_variant(self):
        grade.grade_run(self.run_dir, [oracle_path(self.root)], env=self.env.env)
        data = report.build_report(self.run_dir)
        rows = {(row["split"], row["variant"]): row for row in data["rows"]}
        self.assertEqual(set(rows), {("control", "baseline"), ("control", "improved")})
        base, improved = rows["control", "baseline"], rows["control", "improved"]
        self.assertEqual((base["cases"], base["expected"], base["matched"], base["missed"]), (1, 1, 0, 1))
        self.assertEqual((base["unmatched"], base["awaiting_human"], base["human_false_positive"]), (1, 1, 0))
        self.assertEqual((base["tests_fixed"], base["tests_total"]), (0, 1))
        self.assertEqual((improved["matched"], improved["tests_fixed"]), (1, 1))
        self.assertEqual((base["rounds"], base["corrections"], base["usage"]), ("unknown", "unknown", "unknown"))
        self.assertEqual((improved["rounds"], improved["corrections"]), (1, 1))
        self.assertEqual(improved["usage"], {"input_tokens": 120, "output_tokens": 30})
        self.assertTrue(base["control_flow_only"])
        self.assertIsInstance(base["wall_seconds"], float)

    def test_limits_and_no_superiority_claim(self):
        grade.grade_run(self.run_dir, [oracle_path(self.root)], env=self.env.env)
        text = report.render(report.build_report(self.run_dir))
        self.assertIn("split=control variant=baseline [control-flow-only]", text)
        self.assertIn("Limits", text)
        self.assertIn("cases run: 1", text)
        self.assertIn("control-flow-only variants: baseline, improved", text)
        self.assertIn("unknown metrics: baseline: usage, rounds, corrections", text)
        self.assertIn("awaiting human judgement", text)
        self.assertIsNone(SUPERIORITY.search(text), text)


class FixtureTest(unittest.TestCase):
    def test_fixture_bundle_is_reproducible(self):
        spec = importlib.util.spec_from_file_location("make_snapshot", FIXTURE / "make_snapshot.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        commits = module.build(make_tmp_dir(self) / "rebuilt.bundle")
        self.assertEqual(commits["snapshot"], SNAPSHOT)
        heads = subprocess.run(["git", "bundle", "list-heads", str(case_dir(FIXTURE) / "snapshot.bundle")],
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(heads[0], commits["fix"])
        self.assertEqual(case_mod.load_case(case_dir(FIXTURE)).snapshot_commit, SNAPSHOT)


if __name__ == "__main__":
    unittest.main()
