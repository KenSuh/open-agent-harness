# 평가 실행기 검사 — 사례 형식, 새 작업 공간과 격리 검사, 사전 등록, 변형 실행, 채점, 보고서를 합성 픽스처로 본다.
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import unittest
import zipfile
import zlib
from unittest import mock

from oah.eval import case as case_mod
from oah.engines import proc
from oah.eval import commands, grade, plan, report, runner, workspace
from oah.eval.errors import EvalError
from tests.helpers import directory_alias, make_tmp_dir, npm_layout

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "eval"
SNAPSHOT = "e485fa382909fe7dedb3d0a437d7e8b6b14ca10c"
SUPERIORITY = re.compile(r"\b(better|best|superior|wins?|winner|outperform\w*|beats?)\b", re.IGNORECASE)


def fixture_copy(test: unittest.TestCase) -> pathlib.Path:
    """A private, editable copy of tests/fixtures/eval."""
    target = make_tmp_dir(test) / "eval"
    shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def variant_script(root: pathlib.Path) -> pathlib.Path:
    """Stage an editable fake script in case inputs, inside every workspace boundary."""
    script = case_dir(root) / "inputs" / "fake_variant.py"
    shutil.copyfile(root / "variants" / "fake_variant.py", script)
    set_plan(root, '"fake_variant.py"', '"../inputs/fake_variant.py"')
    return script


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
    return "e485fa382909fe7dedb3d0a437d7e8b6b14ca10c^{tree}"


class Env:
    """Run settings for a test: a run root and the variant environment."""

    def __init__(self, test: unittest.TestCase) -> None:
        base = make_tmp_dir(test)
        self.runs = base / "runs"
        self.marker = base / "marker.txt"
        self.records = base / "records"
        self.env = dict(os.environ, FAKE_EVAL_MARKER=str(self.marker), FAKE_EVAL_RECORD_DIR=str(self.records))
        self.env.pop("FAKE_EVAL_EXIT", None)

    def launched(self) -> list[str]:
        return self.marker.read_text(encoding="utf-8").split() if self.marker.exists() else []


def run_fixture(test: unittest.TestCase, root: pathlib.Path, env: Env, *, frozen: bool = False):
    configure_fixture(root, env.env)
    source = root / "plan.toml"
    if frozen:
        source = root / "prereg.json"
        plan.preregister(plan.load_plan(root / "plan.toml"), [oracle_path(root)], source)
    return runner.run_eval(plan.load_run_source(source), env.runs, env=env.env, timeout_seconds=120)


def configure_fixture(root: pathlib.Path, env: dict) -> None:
    """Test instrumentation is explicit command input, never an environment exemption."""
    args = ["--exit-code", env.get("FAKE_EVAL_EXIT", "0")]
    for key, flag in (("FAKE_EVAL_MARKER", "--marker"), ("FAKE_EVAL_RECORD_DIR", "--record-dir")):
        if env.get(key):
            args.extend((flag, env[key]))
    path = root / "plan.toml"
    text = path.read_text(encoding="utf-8")
    for mode in ("miss", "catch"):
        text = text.replace(f'"{mode}"]', f'"{mode}", ' + ", ".join(json.dumps(arg) for arg in args) + "]")
    path.write_text(text, encoding="utf-8")


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
        self.assertIn("2bb04a8c3c3255a3338a36e652687afceeb94d1b", snapshot.history_objects)  # the later fix
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

    def test_archive_git_metadata_aliases_refused_before_extraction(self):
        aliases = (".GIT/config", ".git./config", ".git /config", "GIT~1/config",
                   ".git::$INDEX_ALLOCATION/config", ".git:stream", ".g\u200cit/config",
                   "nested/.GiT/config", "nested\\.GIT\\config")
        for fmt in ("tar", "zip"):
            for name in aliases:
                with self.subTest(fmt=fmt, name=name):
                    root = fixture_copy(self)
                    archive = case_dir(root) / f"snapshot.{fmt}"
                    members = (("first.txt", b"must not be extracted"), (name, b"[core]\n"))
                    if fmt == "tar":
                        with tarfile.open(archive, "w") as handle:
                            for member_name, data in members:
                                info = tarfile.TarInfo(member_name)
                                info.size = len(data)
                                handle.addfile(info, io.BytesIO(data))
                    else:
                        with zipfile.ZipFile(archive, "w") as handle:
                            for member_name, data in members:
                                handle.writestr(member_name, data)
                    set_case(root, 'file = "snapshot.bundle"', f'file = "snapshot.{fmt}"')
                    set_case(root, 'format = "bundle"', f'format = "{fmt}"')
                    scratch = make_tmp_dir(self)
                    source = workspace.open_snapshot(case_mod.load_case(case_dir(root)), scratch / "src")
                    with self.assertRaises(EvalError) as caught:
                        workspace.build_workspace(source, scratch / "ws")
                    self.assertEqual(caught.exception.code, "OAH_EVAL_SNAPSHOT")
                    self.assertFalse((scratch / "ws" / "repo" / "first.txt").exists())

    @unittest.skipUnless(os.name == "nt", "native Windows path aliases")
    def test_windows_git_metadata_aliases_refused(self):
        self.test_archive_git_metadata_aliases_refused_before_extraction()

    def test_caller_git_context_is_removed(self):
        _, caller = self.build("caller")
        before = {p.relative_to(caller.repo).as_posix(): p.read_bytes()
                  for p in caller.repo.rglob("*") if p.is_file()}
        hostile = {"GIT_DIR": str(caller.repo / ".git"), "GIT_WORK_TREE": str(caller.repo),
                   "GIT_COMMON_DIR": str(caller.repo / ".git"),
                   "GIT_INDEX_FILE": str(caller.repo / ".git" / "index"),
                   "GIT_OBJECT_DIRECTORY": str(caller.repo / ".git" / "objects"),
                   "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(caller.repo / ".git" / "objects"),
                   "GIT_NAMESPACE": "caller", "GIT_PREFIX": "caller/"}
        for key, value in hostile.items():
            with self.subTest(key=key):
                with mock.patch.dict(os.environ, {key: value}):
                    source = workspace.open_snapshot(case_mod.load_case(case_dir(self.root)),
                                                     self.scratch / key / "src")
                    ws = workspace.build_workspace(source, self.scratch / key / "ws")
                    workspace.scan_workspace(ws, source)
                self.assertEqual(git(ws.repo, "rev-list", "--all", "--count"), "1")
                self.assertEqual({p.relative_to(caller.repo).as_posix(): p.read_bytes()
                                  for p in caller.repo.rglob("*") if p.is_file()}, before)

    def test_git_config_environment_cannot_redirect_workspace(self):
        _, caller = self.build("caller")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree",
                                          "GIT_CONFIG_VALUE_0": str(caller.repo)}):
            source, ws = self.build("fresh")
            workspace.scan_workspace(ws, source)
        self.assertEqual(pathlib.Path(git(ws.repo, "rev-parse", "--show-toplevel")).resolve(), ws.repo.resolve())
        self.assertTrue((ws.repo / "words.txt").is_file())

    def test_input_directory_link_refused_before_copy(self):
        target = self.scratch / "external"
        target.mkdir()
        (target / "note.txt").write_bytes(b"must not be copied\n")
        try:
            directory_alias(target, case_dir(self.root) / "inputs" / "reference")
        except OSError:
            self.skipTest("directory links are unavailable")
        with self.assertRaises(EvalError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertFalse((self.scratch / "ws" / "inputs").exists())


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
        self.assertIn("working-file", message)

    def test_oracle_in_primary_git_metadata_still_refused(self):
        snapshot, ws = self.build()
        (ws.repo / ".git" / "answers.toml").write_bytes(self.oracle)
        self.assertIn("oracle", self.refused(snapshot, ws))

    def test_oracle_copy_with_crlf_refused(self):
        snapshot, ws = self.build()
        (ws.repo / "notes" ).mkdir()
        (ws.repo / "notes" / "answers.toml").write_bytes(self.oracle.replace(b"\n", b"\r\n"))
        self.assertIn("working-file", self.refused(snapshot, ws))

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

    def test_forbidden_symlink_refused(self):
        snapshot, ws = self.build()
        try:
            (ws.repo / "fix.patch").symlink_to("words.txt")
        except OSError:
            self.skipTest("file symlinks are unavailable")
        self.assertIn("forbidden", self.refused(snapshot, ws))

    def test_external_input_symlink_refused_without_disclosing_target(self):
        link = case_dir(self.root) / "inputs" / "hint"
        try:
            link.symlink_to(oracle_path(self.root))
        except OSError:
            self.skipTest("file symlinks are unavailable")
        message = self.refused(*self.build())
        self.assertIn("link", message)
        self.assertNotIn(str(oracle_path(self.root)), message)

    def test_external_directory_symlink_refused(self):
        snapshot, ws = self.build()
        try:
            (ws.inputs / "reference").symlink_to(self.root / "oracles", target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable")
        self.assertIn("link", self.refused(snapshot, ws))

    def test_internal_file_symlink_remains_allowed(self):
        snapshot, ws = self.build()
        try:
            (ws.repo / "words-link").symlink_to("words.txt")
        except OSError:
            self.skipTest("file symlinks are unavailable")
        workspace.scan_workspace(ws, snapshot)

    @unittest.skipUnless(os.name == "nt", "native Windows junction")
    def test_external_windows_junction_refused(self):
        snapshot, ws = self.build()
        directory_alias(self.root / "oracles", ws.inputs / "reference")
        self.assertIn("link", self.refused(snapshot, ws))

    def test_committed_symlink_mode_refused_even_as_plain_file(self):
        snapshot, ws = self.build()
        # update-index writes a real mode-120000 entry without requiring symlink privileges.
        for name, target in (("hint", str(oracle_path(self.root))), ("fix.patch", "words.txt")):
            with self.subTest(name=name):
                (ws.repo / name).write_bytes(target.encode("utf-8"))
                blob = git(ws.repo, "hash-object", "-w", str(ws.repo / name))
                git(ws.repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},{name}")
        git(ws.repo, "-c", "user.name=fixture", "-c", "user.email=fixture@oah.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "link modes")
        message = self.refused(snapshot, ws)
        self.assertIn("link", message)
        self.assertIn("forbidden", message)
        self.assertNotIn(str(oracle_path(self.root)), message)

    @unittest.skipUnless(os.name == "nt", "Windows core.symlinks=false checkout")
    def test_windows_committed_symlink_plain_checkout_refused(self):
        snapshot, ws = self.build()
        path = ws.repo / "hint"
        path.write_bytes(str(oracle_path(self.root)).encode("utf-8"))
        blob = git(ws.repo, "hash-object", "-w", str(path))
        git(ws.repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},hint")
        git(ws.repo, "-c", "user.name=fixture", "-c", "user.email=fixture@oah.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "link mode")
        git(ws.repo, "config", "core.symlinks", "false")
        path.unlink()
        git(ws.repo, "checkout-index", "-f", "hint")
        self.assertFalse(path.is_symlink())
        self.assertIn("link", self.refused(snapshot, ws))

    def test_nested_clone_refused(self):
        snapshot, ws = self.build()
        git(ws.inputs, "clone", "-q", str(case_dir(self.root) / "snapshot.bundle"), "reference")
        self.assertIn("embedded", self.refused(snapshot, ws))

    def test_embedded_bare_repository_refused(self):
        snapshot, ws = self.build()
        git(ws.inputs, "clone", "-q", "--bare", str(case_dir(self.root) / "snapshot.bundle"), "reference")
        self.assertIn("embedded", self.refused(snapshot, ws))

    def test_renamed_history_containers_refused(self):
        snapshot, ws = self.build()
        git(ws.inputs, "clone", "-q", "--bare", str(case_dir(self.root) / "snapshot.bundle"), "reference")
        git(ws.inputs / "reference", "repack", "-ad")
        pack = next((ws.inputs / "reference" / "objects" / "pack").glob("*.pack")).read_bytes()
        bundle = (case_dir(self.root) / "snapshot.bundle").read_bytes() + b"\n"
        workspace.remove_tree(ws.inputs / "reference")
        for data in (pack, bundle):
            with self.subTest(header=data[:12]):
                (ws.inputs / "reference.bin").write_bytes(data)
                self.assertIn("history container", self.refused(snapshot, ws))

    def test_loose_git_object_store_refused(self):
        snapshot, ws = self.build()
        bare = self.scratch / "loose.git"
        git(self.scratch, "init", "-q", "--bare", str(bare))
        commit = subprocess.run(["git", "-C", str(snapshot.extract), "cat-file", "commit", SNAPSHOT],
                                capture_output=True, check=True).stdout
        subprocess.run(["git", "-C", str(bare), "hash-object", "-t", "commit", "-w", "--stdin"],
                       input=commit, capture_output=True, check=True)
        shutil.copytree(bare / "objects", ws.inputs / "reference")
        self.assertIn("history container", self.refused(snapshot, ws))

    def test_package_text_is_not_a_git_pack(self):
        snapshot, ws = self.build()
        (ws.inputs / "package.txt").write_bytes(b"PACKAGE documentation\n")
        workspace.scan_workspace(ws, snapshot)


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
        self.assertEqual(data["variants"][0]["command"], ["{python}", "fake_variant.py", "miss"])
        self.assertTrue(data["variants"][0]["fake"])
        self.assertIn("{workspace}/repo/fake_variant.py",
                      data["variants"][0]["channel_files"]["derived-index"])
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
                         ('command = ["{python}", "fake_variant.py", "miss"]', "command = []"),
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
        script = variant_script(self.root)
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        script.write_text(script.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_run_source(out), self.env.runs, env=self.env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_MISMATCH")
        self.assertIn("case derived-index", str(caught.exception))

    def test_isolation_refused_before_any_variant(self):
        (case_dir(self.root) / "inputs" / "hint.toml").write_bytes(oracle_path(self.root).read_bytes())
        with self.assertRaises(EvalError) as caught:
            run_fixture(self, self.root, self.env, frozen=True)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertEqual(self.env.launched(), [])

    def test_oracle_in_prompt_refused_before_any_variant(self):
        for prefix in (b"", b"\xef\xbb\xbf"):
            with self.subTest(prefix=prefix):
                root = fixture_copy(self)
                (case_dir(root) / "task.md").write_bytes(prefix + oracle_path(root).read_bytes().replace(
                    b"\n", b"\r\n"))
                with self.assertRaises(EvalError) as caught:
                    run_fixture(self, root, self.env, frozen=True)
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                self.assertIn("prompt", str(caught.exception))
                self.assertEqual(self.env.launched(), [])
                self.assertFalse(self.env.runs.exists())

    def test_forbidden_prompt_name_refused(self):
        (case_dir(self.root) / "task.md").rename(case_dir(self.root) / "fix.patch")
        set_case(self.root, 'prompt = "task.md"', 'prompt = "fix.patch"')
        with self.assertRaises(EvalError) as caught:
            run_fixture(self, self.root, self.env)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertEqual(self.env.launched(), [])

    def test_bom_oracle_in_prompt_refused(self):
        oracle = oracle_path(self.root)
        oracle.write_bytes(b"\xef\xbb\xbf" + oracle.read_bytes())
        (case_dir(self.root) / "task.md").write_bytes(oracle.read_bytes())
        with self.assertRaises(EvalError) as caught:
            run_fixture(self, self.root, self.env, frozen=True)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertEqual(self.env.launched(), [])

    def test_launch_uses_the_prompt_bytes_that_were_scanned(self):
        prompt = case_dir(self.root) / "task.md"
        prompt.write_bytes(b"\xef\xbb\xbfThe same prompt\r\n")
        script = variant_script(self.root)
        script.write_text('# 프롬프트 입력 고정 확인\nimport pathlib, sys\n'
                          'print(sys.stdin.buffer.read().hex())\n'
                          'sys.stdin.close()\n', encoding="utf-8")
        launch = runner._launch

        def change_prompt_then_launch(*args, **kwargs):
            prompt.write_bytes(b"changed after scan")
            return launch(*args, **kwargs)

        with mock.patch.object(runner, "_launch", side_effect=change_prompt_then_launch):
            run_dir = run_fixture(self, self.root, self.env)
        for name in ("baseline", "improved"):
            out = run_dir / "cases" / "derived-index" / name / "stdout.txt"
            self.assertEqual(out.read_text(encoding="utf-8").strip(), b"The same prompt\r\n".hex())

    def test_other_case_oracle_refused_in_every_workspace(self):
        second = self.root / "cases" / "holdout"
        shutil.copytree(case_dir(self.root), second)
        manifest = second / "case.toml"
        manifest.write_bytes(manifest.read_bytes().replace(b'id = "derived-index"', b'id = "holdout"').replace(
            b'split = "control"', b'split = "holdout"'))
        other_oracle = self.root / "oracles" / "holdout.toml"
        other_oracle.write_bytes(oracle_path(self.root).read_bytes().replace(
            b'case = "derived-index"', b'case = "holdout"'))
        (case_dir(self.root) / "inputs" / "hint.toml").write_bytes(other_oracle.read_bytes())
        with (self.root / "plan.toml").open("a", encoding="utf-8") as handle:
            handle.write('\n[[case]]\npath = "cases/holdout"\n')
        prereg = self.root / "p.json"
        with self.assertRaises(EvalError) as caught:
            plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root), other_oracle], prereg)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertFalse(prereg.exists())
        self.assertEqual(self.env.launched(), [])
        self.assertFalse(self.env.runs.exists())

    def test_variant_environment_excludes_caller_metadata(self):
        script = variant_script(self.root)
        script.write_text('# 환경 경계 확인\nimport json, os, sys\n'
                          'print(json.dumps({"leaked": any(k.upper() in '
                          '{"ORACLE_PATH", "CALLER_METADATA", "GIT_DIR", "OAH_STATE_DIR", "PYTHONPATH"} '
                          'for k in os.environ), "workspace": all(os.environ.get(k) for k in '
                          '["OAH_EVAL_INPUTS", "OAH_EVAL_REQUIREMENT", "OAH_EVAL_OUTPUT"]), '
                          '"path": bool(os.environ.get("PATH"))}))\nsys.stdin.read()\n', encoding="utf-8")
        self.env.env.update(ORACLE_PATH=str(oracle_path(self.root)), CALLER_METADATA="private",
                            GIT_DIR="caller", OAH_STATE_DIR="caller", PYTHONPATH="caller")
        run_dir = run_fixture(self, self.root, self.env)
        for name in ("baseline", "improved"):
            seen = json.loads((run_dir / "cases" / "derived-index" / name / "stdout.txt").read_text(
                encoding="utf-8"))
            self.assertEqual(seen, {"leaked": False, "workspace": True, "path": True})

    def test_missing_command_refused_before_any_variant(self):
        set_plan(self.root, '["{python}", "fake_variant.py", "catch"]',
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
        script = case_dir(self.root) / "inputs" / "silent.py"
        script.write_text("# 출력 없는 변형\nimport sys\nsys.stdin.read()\n", encoding="utf-8")
        set_plan(self.root, '["{python}", "fake_variant.py", "catch"]\nfake = true',
                 '["{python}", "../inputs/silent.py"]\nfake = false')
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

    def test_duplicate_finding_ids_refused(self):
        defects = [grade.Defect("D1", "", ("index.txt",), ("stale",))]
        for first, second in (("F1", "F1"), (1, "1"), ("F2", None)):
            with self.subTest(ids=(first, second)):
                findings = [self.finding("index.txt", "stale", first),
                            self.finding("words.txt", "unrelated", second)]
                with self.assertRaises(EvalError) as caught:
                    grade.match_findings(defects, findings)
                self.assertEqual(caught.exception.code, "OAH_EVAL_FINDING_DUPLICATE")
                self.assertIn("unique", str(caught.exception))

    def test_duplicate_ids_refused_before_human_verdicts(self):
        run_dir = run_fixture(self, self.root, self.env)
        path = run_dir / "cases" / "derived-index" / "improved" / "result.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        result["findings"].append(self.finding("words.txt", "unrelated", "F1"))
        path.write_text(json.dumps(result), encoding="utf-8")
        verdicts = self.root / "verdicts.toml"
        verdicts.write_bytes(b'[[verdict]]\ncase = "derived-index"\nvariant = "improved"\n'
                             b'finding = "F1"\nverdict = "false-positive"\n')
        with self.assertRaises(EvalError) as caught:
            grade.grade_run(run_dir, [oracle_path(self.root)], verdicts_path=verdicts, env=self.env.env)
        self.assertEqual(caught.exception.code, "OAH_EVAL_FINDING_DUPLICATE")
        self.assertFalse((run_dir / "grade.json").exists())

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

    def test_usage_totals_track_each_field_completeness(self):
        grade.grade_run(self.run_dir, [oracle_path(self.root)], env=self.env.env)
        run_path = self.run_dir / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        for i, usage in enumerate(({"input_tokens": 10, "cost_usd": 0.01}, {"input_tokens": 20})):
            path = self.run_dir / run["results"][i]["path"]
            result = json.loads(path.read_text(encoding="utf-8"))
            result.update(variant="baseline", usage=usage)
            path.write_text(json.dumps(result), encoding="utf-8")
            run["results"][i]["variant"] = "baseline"
        run_path.write_text(json.dumps(run), encoding="utf-8")
        data = report.build_report(self.run_dir)
        [row] = data["rows"]
        self.assertEqual(row["usage"]["input_tokens"], 30)
        self.assertEqual(row["usage"]["cost_usd"], "unknown (1 of 2 recorded)")
        self.assertIn("usage.cost_usd", "\n".join(data["limits"]))
        self.assertIn("cost_usd=unknown (1 of 2 recorded)", report.render(data))

    def test_usage_missing_whole_record_still_tracks_fields(self):
        total = report._sum_usage([{"input_tokens": 10, "cost_usd": 0}, "unknown"])
        self.assertIsInstance(total, dict)
        self.assertEqual(total["input_tokens"], "unknown (1 of 2 recorded)")
        self.assertEqual(total["cost_usd"], "unknown (1 of 2 recorded)")
        self.assertEqual(report._sum_usage(["unknown", "unknown"]), "unknown")

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
        self.assertEqual(improved["usage"], {"input_tokens": 120, "output_tokens": 30, "cost_usd": "unknown"})
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


class CorrectionC2Test(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.env = Env(self)
        self.out = self.root / "prereg.json"

    def prereg(self):
        return plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], self.out)

    def refused(self, action):
        with self.assertRaises(EvalError) as caught:
            action()
        self.assertIn(caught.exception.code, ("OAH_EVAL_ISOLATION", "OAH_EVAL_CHANNEL_UNCHECKED",
                                              "OAH_EVAL_CASE_INVALID"))
        self.assertEqual(self.env.launched(), [])
        self.assertFalse(self.env.runs.exists())
        return str(caught.exception)

    def argument(self, value):
        set_plan(self.root, '"miss"]', '"miss", ' + json.dumps(value) + ']')

    def oracle_snapshot(self, name, *, encoding=False):
        repo = make_tmp_dir(self) / "repo"
        git(repo.parent, "init", "-q", "-b", "main", str(repo))
        data = oracle_path(self.root).read_bytes()
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if encoding:
            (repo / ".gitattributes").write_bytes(b"answers.txt working-tree-encoding=UTF-16\n")
            target.write_bytes(data.decode("utf-8").encode("utf-16"))
        else:
            target.write_bytes(data)
        git(repo, "add", "-A")
        tree = git(repo, "write-tree")
        commit = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-m", "snapshot"],
                                env=dict(os.environ, **workspace.IDENTITY), capture_output=True,
                                text=True, check=True).stdout.strip()
        git(repo, "update-ref", "refs/heads/main", commit)
        bundle = case_dir(self.root) / "snapshot.bundle"
        bundle.unlink()
        git(repo, "bundle", "create", str(bundle), "main")
        set_case(self.root, SNAPSHOT, commit)

    def test_raw_utf16_checkout_blob_refused(self):
        self.oracle_snapshot("answers.txt", encoding=True)
        scratch = make_tmp_dir(self)
        source = workspace.open_snapshot(case_mod.load_case(case_dir(self.root)), scratch / "source")
        ws = workspace.build_workspace(source, scratch / "ws")
        self.assertNotEqual((ws.repo / "answers.txt").read_bytes(), oracle_path(self.root).read_bytes())
        self.assertEqual(subprocess.run(["git", "-C", str(ws.repo), "show", "HEAD:answers.txt"],
                                       capture_output=True, check=True).stdout, oracle_path(self.root).read_bytes())
        self.refused(lambda: workspace.scan_workspace(ws, source, oracle_hashes=workspace.content_hashes(
            oracle_path(self.root).read_bytes())))
        self.refused(self.prereg)
        self.assertFalse(self.out.exists())

    def test_overwritten_committed_config_blob_refused(self):
        self.oracle_snapshot(".oah/config.toml")
        scratch = make_tmp_dir(self)
        source = workspace.open_snapshot(case_mod.load_case(case_dir(self.root)), scratch / "source")
        ws = workspace.build_workspace(source, scratch / "ws", config_text="[review]\nmax_rounds = 3\n")
        self.refused(lambda: workspace.scan_workspace(ws, source, oracle_hashes=workspace.content_hashes(
            oracle_path(self.root).read_bytes())))

    def test_oracle_argument_refused_before_freeze(self):
        self.argument(str(oracle_path(self.root)))
        message = self.refused(self.prereg)
        self.assertNotIn(str(oracle_path(self.root)), message)
        self.assertFalse(self.out.exists())

    def test_oracle_argument_contents_refused(self):
        self.argument(oracle_path(self.root).read_bytes().decode("utf-8"))
        self.refused(self.prereg)

    def test_oracle_option_equals_and_resolved_alias_refused(self):
        alias = self.root / "alias.toml"
        try:
            alias.symlink_to(oracle_path(self.root))
        except OSError:
            self.skipTest("file symlinks are unavailable")
        self.argument("--prompt-file=" + str(alias))
        self.refused(self.prereg)

    def test_referenced_oracle_copy_refused(self):
        copy = self.root / "copy.txt"
        copy.write_bytes(oracle_path(self.root).read_bytes())
        self.argument("--prompt-file=" + str(copy))
        self.refused(self.prereg)

    def test_config_oracle_path_and_nested_reference_refused(self):
        nested = self.root / "nested.toml"
        nested.write_bytes(('prompt_file = ' + json.dumps(str(oracle_path(self.root))) + '\n').encode("utf-8"))
        config = self.root / "variant.toml"
        config.write_bytes(('include = ' + json.dumps(str(nested)) + '\n').encode("utf-8"))
        set_plan(self.root, 'name = "baseline"', 'name = "baseline"\nconfig = "variant.toml"')
        self.refused(self.prereg)

    def test_uncheckable_reference_refused(self):
        self.argument("--prompt-file={plan_dir}/missing.txt")
        self.assertIn("OAH_EVAL_CHANNEL_UNCHECKED", self.refused(self.prereg))

    def test_attached_short_file_option_refused(self):
        copy = self.root / "copy.txt"
        copy.write_bytes(oracle_path(self.root).read_bytes())
        self.argument("-F" + str(copy))
        self.refused(self.prereg)

    def test_missing_config_string_reference_refused(self):
        (self.root / "variant.toml").write_bytes(b'prompt_file = "missing.txt"\n')
        set_plan(self.root, 'name = "baseline"', 'name = "baseline"\nconfig = "variant.toml"')
        self.refused(self.prereg)

    def test_unlaunchable_environment_refused_before_run_folder(self):
        self.prereg()
        self.refused(lambda: runner.run_eval(plan.load_frozen(self.out), self.env.runs,
                                             env=dict(self.env.env, LANG="bad\x00value"), timeout_seconds=60))

    def test_referenced_file_digest_is_frozen(self):
        config = case_dir(self.root) / "inputs" / "extra.json"
        config.write_bytes(b'{"review": {"rounds": 1}}\n')
        self.argument("--config=../inputs/extra.json")
        self.prereg()
        config.write_bytes(b'{"review": {"rounds": 2}}\n')
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_frozen(self.out), self.env.runs, env=self.env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_MISMATCH")
        self.assertFalse(self.env.runs.exists())

    def test_prompt_and_working_file_paths_refused_before_freeze(self):
        for name in ("task.md", "inputs/notes.md"):
            with self.subTest(channel=name):
                path = case_dir(self.root) / name
                original = path.read_bytes()
                path.write_bytes(('read ' + str(oracle_path(self.root)) + '\n').encode("utf-8"))
                self.refused(self.prereg)
                path.write_bytes(original)

    def test_allowed_environment_oracle_path_and_contents_refused(self):
        self.prereg()
        for key, value in (("LANG", str(oracle_path(self.root))),
                           ("LC_ALL", oracle_path(self.root).read_bytes().decode("utf-8"))):
            with self.subTest(channel=key):
                env = dict(self.env.env, **{key: value})
                self.refused(lambda: runner.run_eval(plan.load_frozen(self.out), self.env.runs,
                                                     env=env, timeout_seconds=60))

    def test_environment_checked_before_freeze(self):
        with mock.patch.dict(os.environ, {"LC_CTYPE": str(oracle_path(self.root))}):
            self.refused(self.prereg)
        self.assertFalse(self.out.exists())

    def test_runtime_checks_new_reference_contents_before_launch(self):
        copy = self.root / "new-destination.txt"
        self.argument("--marker=" + str(copy))
        self.prereg()
        copy.write_bytes(oracle_path(self.root).read_bytes())
        self.refused(lambda: runner.run_eval(plan.load_frozen(self.out), self.env.runs,
                                             env=self.env.env, timeout_seconds=60))

    def test_non_utf8_prompt_is_a_named_refusal(self):
        (case_dir(self.root) / "task.md").write_bytes(b"\xff\xfe\x00")
        self.assertIn("OAH_EVAL_CHANNEL_UNCHECKED", self.refused(self.prereg))

    def test_internal_named_ancestor_link_also_refused(self):
        original = case_dir(self.root) / "original"
        original.mkdir()
        shutil.copyfile(case_dir(self.root) / "requirement.md", original / "requirement.md")
        try:
            directory_alias(original, case_dir(self.root) / "linked")
        except OSError:
            self.skipTest("directory links are unavailable")
        set_case(self.root, 'requirement = "requirement.md"', 'requirement = "linked/requirement.md"')
        self.refused(self.prereg)

    def test_nested_named_input_is_in_frozen_digest(self):
        nested = case_dir(self.root) / "regular"
        nested.mkdir()
        shutil.copyfile(case_dir(self.root) / "requirement.md", nested / "requirement.md")
        set_case(self.root, 'requirement = "requirement.md"', 'requirement = "regular/requirement.md"')
        self.prereg()
        (nested / "requirement.md").write_bytes(b"changed consumed input\n")
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_frozen(self.out), self.env.runs, env=self.env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_MISMATCH")
        self.assertFalse(self.env.runs.exists())

    @unittest.skipUnless(os.name == "nt", "native Windows path case and separators")
    def test_windows_oracle_path_alias_refused(self):
        self.argument(str(oracle_path(self.root)).upper().replace("\\", "/"))
        self.refused(self.prereg)

    def test_linked_named_case_ancestors_refused(self):
        outside = make_tmp_dir(self)
        for name in ("requirement.md", "task.md", "snapshot.bundle"):
            shutil.copyfile(case_dir(self.root) / name, outside / name)
        try:
            directory_alias(outside, case_dir(self.root) / "linked")
        except OSError:
            self.skipTest("directory links are unavailable")
        for key, name in (("requirement", "requirement.md"), ("prompt", "task.md"), ("file", "snapshot.bundle")):
            with self.subTest(field=key):
                set_case(self.root, f'{key} = "{name}"', f'{key} = "linked/{name}"')
                self.refused(lambda: case_mod.load_case(case_dir(self.root)))
                set_case(self.root, f'{key} = "linked/{name}"', f'{key} = "{name}"')

    @unittest.skipUnless(os.name == "nt", "native Windows junction ancestors")
    def test_windows_named_case_junction_refused(self):
        self.test_linked_named_case_ancestors_refused()


class CorrectionC3Test(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.scratch = make_tmp_dir(self)

    def snapshot(self, files):
        repo = self.scratch / "original"
        git(repo.parent, "init", "-q", "-b", "main", str(repo))
        for name, (mode, raw) in files.items():
            oid = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                 input=raw, capture_output=True, check=True).stdout.decode("utf-8").strip()
            git(repo, "update-index", "--add", "--cacheinfo", mode, oid, name)
        tree = git(repo, "write-tree")
        commit = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-m", "snapshot"],
                                env=dict(os.environ, **workspace.IDENTITY), capture_output=True,
                                text=True, check=True).stdout.strip()
        git(repo, "update-ref", "refs/heads/main", commit)
        bundle = case_dir(self.root) / "snapshot.bundle"
        bundle.unlink()
        git(repo, "bundle", "create", str(bundle), "main")
        set_case(self.root, SNAPSHOT, commit)
        return self.source()

    def source(self):
        return workspace.open_snapshot(case_mod.load_case(case_dir(self.root)),
                                       make_tmp_dir(self) / "source")

    def build(self, source=None, *, config_text=None):
        return workspace.build_workspace(source or self.source(), make_tmp_dir(self) / "ws",
                                         config_text=config_text)

    def refused(self, action):
        with self.assertRaises(EvalError) as caught:
            action()
        self.assertIn(caught.exception.code, ("OAH_EVAL_ISOLATION", "OAH_EVAL_CHANNEL_UNCHECKED"))
        self.assertNotIn(str(oracle_path(self.root)), str(caught.exception))
        return caught.exception

    def test_config_file_link_never_overwrites_external_file(self):
        external = self.scratch / "private.toml"
        external.write_bytes(b"keep these bytes exactly\r\n")
        source = self.snapshot({".oah/config.toml": ("120000", str(external).encode("utf-8"))})
        try:
            ws = self.build(source, config_text="[review]\nmax_rounds = 3\n")
        except EvalError:
            pass
        else:
            self.refused(lambda: workspace.scan_workspace(ws, source))
        self.assertEqual(external.read_bytes(), b"keep these bytes exactly\r\n")

    def test_config_parent_link_never_overwrites_external_file(self):
        external = self.scratch / "private"
        external.mkdir()
        target = external / "config.toml"
        target.write_bytes(b"keep parent target\n")
        source = self.snapshot({".oah": ("120000", str(external).encode("utf-8"))})
        try:
            ws = self.build(source, config_text="[review]\nmax_rounds = 3\n")
        except EvalError:
            pass
        else:
            self.refused(lambda: workspace.scan_workspace(ws, source))
        self.assertEqual(target.read_bytes(), b"keep parent target\n")

    def test_oracle_dotdot_alias_in_text_channels_refused(self):
        oracle = oracle_path(self.root)
        alias = str(oracle.parent / ".." / oracle.parent.name / oracle.name)
        ws = self.build()
        checks = workspace.oracle_path_checks([oracle])
        for channel in ("prompt", "working-file", "config"):
            with self.subTest(channel=channel):
                kwargs = {"oracle_paths": checks}
                if channel == "working-file":
                    target = ws.inputs / "alias.txt"
                    target.write_bytes(('read "' + alias + '"\n').encode("utf-8"))
                elif channel == "config":
                    kwargs["config_text"] = "description = " + json.dumps("read " + alias)
                else:
                    kwargs["prompt"] = ('read "' + alias + '"\n').encode("utf-8")
                try:
                    self.refused(lambda: workspace.check_variant_channels(ws, **kwargs))
                finally:
                    if channel == "working-file":
                        target.unlink()

    def test_positional_filename_with_equals_checked_whole(self):
        ws = self.build()
        target = ws.repo / "answers=copy"
        target.write_bytes(oracle_path(self.root).read_bytes())
        # The working-file scan already finds this; use an external file to isolate argv parsing.
        external = self.scratch / "answers=copy"
        target.rename(external)
        self.refused(lambda: workspace.check_variant_channels(
            ws, argv=[sys.executable, str(external)], oracle_hashes=workspace.content_hashes(external.read_bytes())))

    def test_retained_config_history_containers_refused(self):
        bundle = (case_dir(self.root) / "snapshot.bundle").read_bytes()
        payloads = (bundle, b"PACK\x00\x00\x00\x02\x00\x00\x00\x00",
                    zlib.compress(b"commit 4\x00test"))
        for raw in payloads:
            with self.subTest(header=raw[:8]):
                # Each subcase needs its own source tree and bundle.
                if (self.scratch / "original").exists():
                    workspace.remove_tree(self.scratch / "original")
                    manifest = case_dir(self.root) / "case.toml"
                    text = manifest.read_text(encoding="utf-8")
                    text = re.sub(r'commit = "[0-9a-f]+"', 'commit = "' + SNAPSHOT + '"', text)
                    manifest.write_text(text, encoding="utf-8")
                source = self.snapshot({".oah/config.toml": ("100644", raw)})
                ws = self.build(source, config_text="[review]\nmax_rounds = 3\n")
                retained = subprocess.run(["git", "-C", str(ws.repo), "show", "HEAD:.oah/config.toml"],
                                          capture_output=True, check=True).stdout
                self.assertEqual(retained, raw)
                self.refused(lambda: workspace.scan_workspace(ws, source))

    def test_inline_code_option_names_refused_before_values(self):
        ws = self.build()
        for option in ("--eval", "--command", "-c", "-e"):
            for tail in ("", '=require("fs")["readFileSync"](process["env"]["HOME"])'):
                with self.subTest(option=option + tail):
                    self.refused(lambda: workspace.check_variant_channels(ws, argv=[sys.executable, option + tail]))

    def test_existing_external_file_arguments_and_config_values_refused(self):
        ws = self.build()
        external = self.scratch / "public.txt"
        external.write_bytes(b"ordinary public material\n")
        alias = str(external.parent / ".." / external.parent.name / external.name)
        for channel in ("argument", "config"):
            for value in (alias, "NAME=" + alias):
                with self.subTest(channel=channel, assignment=value.startswith("NAME=")):
                    kwargs = {}
                    if channel == "argument":
                        kwargs["argv"] = [sys.executable, value]
                    else:
                        kwargs["config_text"] = "input_file = " + json.dumps(value)
                    self.refused(lambda: workspace.check_variant_channels(ws, **kwargs))

    def test_external_executable_is_the_only_exception(self):
        ws = self.build()
        workspace.check_variant_channels(ws, argv=[sys.executable])
        self.refused(lambda: workspace.check_variant_channels(ws, argv=[sys.executable, str(self.scratch)]))

    def test_text_oracle_alias_with_prose_punctuation_refused(self):
        ws = self.build()
        oracle = oracle_path(self.root)
        alias = str(oracle.parent / ".." / oracle.parent.name / oracle.name)
        for suffix in (".", ":", "!"):
            with self.subTest(punctuation=suffix):
                self.refused(lambda: workspace.check_variant_channels(
                    ws, prompt=("Read " + alias + suffix).encode("utf-8"),
                    oracle_paths=workspace.oracle_path_checks([oracle])))

    def test_root_directory_in_prompt_is_ordinary_prose(self):
        ws = self.build()
        workspace.check_variant_channels(ws, prompt=b"Read /")

    def test_aliased_oracle_environment_refused_at_run(self):
        out = self.root / "prereg.json"
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        oracle = oracle_path(self.root)
        alias = str(oracle.parent / ".." / oracle.parent.name / oracle.name)
        runs = self.scratch / "runs"
        self.refused(lambda: runner.run_eval(plan.load_frozen(out), runs,
                                             env=dict(os.environ, LC_CTYPE=alias), timeout_seconds=60))
        self.assertFalse(runs.exists())

    def test_positional_equals_file_is_read_and_frozen(self):
        ws = self.build()
        target = ws.repo / "notes=copy"
        target.write_bytes(b"ordinary input\n")
        files = workspace.check_variant_channels(ws, argv=[sys.executable, "notes=copy"])
        self.assertEqual(files["{workspace}/repo/notes=copy"], hashlib.sha256(target.read_bytes()).hexdigest())

    def test_injection_refuses_linked_repository_ancestor(self):
        target = self.scratch / "target"
        repo = target / "repo"
        config = repo / ".oah" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"preserve ancestor target\n")
        alias = self.scratch / "ancestor"
        try:
            directory_alias(target, alias)
        except OSError:
            self.skipTest("directory links are unavailable")
        self.refused(lambda: workspace._inject_config(alias / "repo", "[review]\nmax_rounds = 3\n"))
        self.assertEqual(config.read_bytes(), b"preserve ancestor target\n")

    def test_prereg_config_link_cannot_overwrite_oracle(self):
        oracle = oracle_path(self.root)
        original = oracle.read_bytes()
        self.snapshot({".oah/config.toml": ("120000", str(oracle).encode("utf-8"))})
        (self.root / "injected.toml").write_bytes(b"[review]\nmax_rounds = 3\n")
        set_plan(self.root, 'name = "baseline"', 'name = "baseline"\nconfig = "injected.toml"')
        out = self.root / "prereg.json"
        self.refused(lambda: plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle], out))
        self.assertEqual(oracle.read_bytes(), original)
        self.assertFalse(out.exists())
        self.assertFalse(out.with_suffix(".json.sha256").exists())

    def test_injection_replaces_internal_file_link_without_writing_through(self):
        source = self.snapshot({".oah/config.toml": ("120000", b"../original.toml"),
                                "original.toml": ("100644", b"preserve original\n")})
        ws = self.build(source, config_text="[review]\nmax_rounds = 3\n")
        self.assertFalse(workspace._is_link(ws.repo / ".oah" / "config.toml"))
        self.assertFalse(workspace._is_link(ws.repo / ".oah"))
        self.assertEqual((ws.repo / "original.toml").read_bytes(), b"preserve original\n")
        self.assertEqual((ws.repo / ".oah" / "config.toml").read_bytes(), b"[review]\nmax_rounds = 3\n")
        workspace.scan_workspace(ws, source)

    def test_unreferenced_history_blob_refused(self):
        ws = self.build()
        pack = subprocess.run(["git", "-C", str(ws.repo), "pack-objects", "--stdout"],
                              input=b"", capture_output=True, check=True).stdout
        subprocess.run(["git", "-C", str(ws.repo), "hash-object", "-w", "--stdin"],
                       input=pack, capture_output=True, check=True)
        self.refused(lambda: workspace.check_variant_channels(ws))

    def test_unresolvable_file_argument_is_a_named_refusal(self):
        ws = self.build()
        loop = self.scratch / "loop"
        try:
            loop.symlink_to(loop)
        except OSError:
            self.skipTest("file symlinks are unavailable")
        error = self.refused(lambda: workspace.check_variant_channels(ws, argv=[sys.executable, str(loop)]))
        self.assertEqual(error.code, "OAH_EVAL_CHANNEL_UNCHECKED")
        self.assertTrue(error.__suppress_context__)
        self.assertNotIn(str(loop), str(error))

    @unittest.skipUnless(os.name == "nt", "native Windows junction config parent")
    def test_windows_config_junction_cannot_redirect_injection(self):
        repo = self.scratch / "junction-repo"
        repo.mkdir()
        external = self.scratch / "junction-target"
        external.mkdir()
        target = external / "config.toml"
        target.write_bytes(b"preserve junction target\r\n")
        directory_alias(external, repo / ".oah")
        workspace._inject_config(repo, "[review]\nmax_rounds = 3\n")
        self.assertEqual(target.read_bytes(), b"preserve junction target\r\n")
        self.assertFalse(workspace._is_link(repo / ".oah"))

    @unittest.skipUnless(os.name == "nt", "native Windows path aliases in text")
    def test_windows_oracle_alias_in_prompt_refused(self):
        ws = self.build()
        oracle = oracle_path(self.root)
        alias = str(oracle.parent / ".." / oracle.parent.name / oracle.name).upper().replace("\\", "/")
        self.refused(lambda: workspace.check_variant_channels(
            ws, prompt=("Read " + alias).encode("utf-8"), oracle_paths=workspace.oracle_path_checks([oracle])))


class CorrectionC4Test(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.scratch = make_tmp_dir(self)
        self.env = Env(self)

    snapshot = CorrectionC3Test.snapshot
    source = CorrectionC3Test.source
    build = CorrectionC3Test.build

    def prereg(self, oracle=None):
        out = self.root / "prereg.json"
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle or oracle_path(self.root)], out)
        return out

    def test_grader_folder_prefixes_refused_in_all_content_channels(self):
        ws = self.build()
        oracle = oracle_path(self.root)
        # The filename is different: only the grader-folder literal identifies this leak.
        alias = oracle.parent / "answers-link"
        try:
            alias.symlink_to(oracle)
        except OSError:
            # Literal folder checks also cover missing aliases on hosts without symlink privileges.
            pass
        checks = workspace.oracle_path_checks([oracle])
        for prefix in ("@", "file://"):
            value = "Read " + prefix + str(oracle.parent / "answers-link")
            for channel in ("prompt", "working-file", "git-blob", "argument"):
                with self.subTest(prefix=prefix, channel=channel):
                    kwargs = {"oracle_paths": checks}
                    if channel == "prompt":
                        kwargs["prompt"] = value.encode("utf-8")
                    elif channel == "working-file":
                        (ws.inputs / "reference.md").write_bytes(value.encode("utf-8"))
                    elif channel == "git-blob":
                        subprocess.run(["git", "-C", str(ws.repo), "hash-object", "-w", "--stdin"],
                                       input=value.encode("utf-8"), capture_output=True, check=True)
                    else:
                        kwargs["argv"] = [sys.executable, "--description=" + value]
                    with self.assertRaises(EvalError) as caught:
                        workspace.check_variant_channels(ws, **kwargs)
                    self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                    self.assertNotIn(str(oracle.parent), str(caught.exception))
                    # A fresh workspace keeps each subcase independent, including unreachable blobs.
                    ws = self.build()

    def test_ordinary_prose_is_not_interpreted_as_filesystem_paths(self):
        ws = self.build()
        external = self.scratch / "public.txt"
        external.write_bytes(b"public material\n")
        loop = self.scratch / "loop"
        try:
            loop.symlink_to(loop)
        except OSError:
            loop = external
        prose = f"Read @{external} or file://{external}; ../notes.md is prose. Read / and {loop}."
        (ws.inputs / "notes.md").write_bytes(prose.encode("utf-8"))
        workspace.check_variant_channels(ws, prompt=prose.encode("utf-8"))

    def test_variant_grading_reconstructs_inputs_requirement_and_environment(self):
        check = case_dir(self.root) / "inputs" / "check.py"
        check.write_bytes(b"import os, pathlib, sys\n"
                          b"root = pathlib.Path.cwd().parent\n"
                          b"assert pathlib.Path(os.environ['OAH_EVAL_INPUTS']) == root.joinpath('inputs')\n"
                          b"assert pathlib.Path(os.environ['OAH_EVAL_REQUIREMENT']).read_bytes() == "
                          b"root.joinpath('requirement.md').read_bytes()\n"
                          b"assert pathlib.Path(os.environ['OAH_EVAL_OUTPUT']) == root.joinpath('output')\n"
                          b"assert root.joinpath('output').is_dir()\n"
                          b"sys.exit(0 if 'kiwi' in pathlib.Path('index.txt').read_text() else 1)\n")
        oracle = oracle_path(self.root)
        oracle.write_bytes(oracle.read_bytes().replace(b'"check_index.py"', b'"../inputs/check.py"'))
        run_dir = run_fixture(self, self.root, self.env)
        graded = grade.grade_run(run_dir, [oracle], env=self.env.env)
        case = graded["cases"]["derived-index"]
        self.assertEqual(case["tests"], [{"id": "T1", "snapshot": "fails"}])
        self.assertEqual(case["variants"]["baseline"]["tests"], [{"id": "T1", "result": "not-fixed"}])
        self.assertEqual(case["variants"]["improved"]["tests"], [{"id": "T1", "result": "fixed"}])

    @unittest.skipUnless(os.name != "nt", "POSIX executable scripts; native Windows uses exe files")
    def test_relative_snapshot_executables_resolve_per_workspace(self):
        self.snapshot({"run_variant": ("100755", b"#!/bin/sh\necho launched > ../output/launched.txt\n"),
                       "oracle_test": ("100755", b"#!/bin/sh\ntest -f fixed\n")})
        set_plan(self.root, '["{python}", "fake_variant.py", "miss"]', '["./run_variant"]')
        set_plan(self.root, '["{python}", "fake_variant.py", "catch"]', '["./run_variant"]')
        oracle = oracle_path(self.root)
        oracle.write_bytes(b'case = "derived-index"\n[[test]]\nid = "T1"\ncommand = ["./oracle_test"]\n')
        out = self.prereg()
        run_dir = runner.run_eval(plan.load_frozen(out), self.env.runs, env=self.env.env, timeout_seconds=60)
        for name in ("baseline", "improved"):
            folder = run_dir / "cases" / "derived-index" / name
            self.assertEqual((folder / "output" / "launched.txt").read_bytes(), b"launched\n")
        improved = run_dir / "cases" / "derived-index" / "improved" / "workspace"
        (improved / "fixed").write_bytes(b"fixed\n")
        graded = grade.grade_run(run_dir, [oracle], env=self.env.env)
        self.assertEqual(graded["cases"]["derived-index"]["variants"]["improved"]["tests"],
                         [{"id": "T1", "result": "fixed"}])

    @unittest.skipUnless(os.name == "nt", "native Windows relative executable")
    def test_windows_relative_executable_resolves_against_repo(self):
        from oah.eval.commands import resolve_argv
        repo = self.scratch / "repo"
        repo.mkdir()
        shutil.copyfile(sys.executable, repo / "variant.exe")
        # Resolution must not depend on the caller cwd; no batch shim is involved.
        argv = resolve_argv([r".\variant.exe"], os.environ, "variant", cwd=repo)
        self.assertEqual(pathlib.Path(argv[0]), repo / "variant.exe")

    def test_retained_regular_git_names_checked_after_config_replacement(self):
        source = self.snapshot({".oah/config.toml/fix.patch": ("100644", b"ordinary bytes\n")})
        ws = self.build(source, config_text="[review]\nmax_rounds = 3\n")
        self.assertTrue((ws.repo / ".oah" / "config.toml").is_file())
        self.assertEqual(git(ws.repo, "show", "HEAD:.oah/config.toml/fix.patch"), "ordinary bytes")
        with self.assertRaises(EvalError) as caught:
            workspace.scan_workspace(ws, source)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertIn("forbidden", str(caught.exception))

    def test_unreferenced_retained_tree_names_are_also_checked(self):
        source = self.source()
        ws = self.build(source)
        oid = subprocess.run(["git", "-C", str(ws.repo), "hash-object", "-w", "--stdin"],
                             input=b"ordinary bytes\n", capture_output=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(ws.repo), "mktree"],
                       input=b"100644 blob " + oid + b"\tfix.patch\n", capture_output=True, check=True)
        with self.assertRaises(EvalError) as caught:
            workspace.scan_workspace(ws, source)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_grader_folder_cannot_contain_a_case(self):
        oracle = self.root / "answers.toml"
        shutil.copyfile(oracle_path(self.root), oracle)
        with self.assertRaises(EvalError) as caught:
            self.prereg(oracle)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_FOLDER")
        self.assertFalse((self.root / "prereg.json").exists())

    def test_grader_folder_cannot_equal_or_be_inside_a_case(self):
        for folder in (case_dir(self.root), case_dir(self.root) / "grader"):
            with self.subTest(folder=folder.name):
                folder.mkdir(exist_ok=True)
                oracle = folder / "answers.toml"
                shutil.copyfile(oracle_path(self.root), oracle)
                with self.assertRaises(EvalError) as caught:
                    self.prereg(oracle)
                self.assertIn(caught.exception.code, ("OAH_EVAL_ORACLE_IN_CASE", "OAH_EVAL_ORACLE_FOLDER"))

    def test_run_rechecks_case_and_saved_workspace_separation(self):
        out = self.prereg()
        source = plan.load_frozen(out)
        relocated = oracle_path(self.root).parent / "case"
        shutil.copytree(case_dir(self.root), relocated)
        source.data["cases"][0]["path"] = str(relocated)
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(source, self.env.runs, env=self.env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_FOLDER")
        self.assertFalse(self.env.runs.exists())
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_frozen(out), oracle_path(self.root).parent / "runs",
                            env=self.env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_FOLDER")

    def test_grader_folder_and_workspace_are_disjoint_in_both_directions(self):
        ws = self.build()
        for folder in (ws.root, ws.root / "grader", ws.root.parent):
            with self.subTest(folder=folder.name):
                folder.mkdir(exist_ok=True)
                oracle = folder / "answers.toml"
                oracle.write_bytes(oracle_path(self.root).read_bytes())
                with self.assertRaises(EvalError) as caught:
                    workspace.check_variant_channels(ws, oracle_paths=workspace.oracle_path_checks([oracle]))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ORACLE_FOLDER")
                oracle.unlink()

    def test_report_names_honest_author_and_sandbox_limits(self):
        run_dir = run_fixture(self, self.root, self.env)
        grade.grade_run(run_dir, [oracle_path(self.root)], env=self.env.env)
        limits = " ".join(report.build_report(run_dir)["limits"])
        self.assertIn("honest", limits)
        self.assertIn("malicious case author", limits)
        self.assertIn("sandbox", limits)


class CorrectionC5Test(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.scratch = make_tmp_dir(self)
        self.env = Env(self)

    source = CorrectionC3Test.source
    build = CorrectionC3Test.build

    def test_utf16_oracle_input_copy_refused_before_freeze(self):
        oracle = oracle_path(self.root)
        text = oracle.read_bytes().decode("utf-8").replace("\r\n", "\n")
        for bom, encoding in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
            with self.subTest(encoding=encoding):
                out = self.root / (encoding + ".json")
                (case_dir(self.root) / "inputs" / "notes.md").write_bytes(
                    bom + text.replace("\n", "\r\n").encode(encoding))
                with self.assertRaises(EvalError) as caught:
                    plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle], out)
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                self.assertNotIn(str(oracle.parent), str(caught.exception))
                self.assertFalse(out.exists())
                self.assertFalse(out.with_suffix(".json.sha256").exists())

    def test_utf16_oracle_input_copy_refused_by_run_scanner(self):
        source = self.source()
        ws = self.build(source)
        raw = oracle_path(self.root).read_bytes()
        for bom, encoding in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
            with self.subTest(encoding=encoding):
                (ws.inputs / "notes.md").write_bytes(bom + raw.decode("utf-8").encode(encoding))
                with self.assertRaises(EvalError) as caught:
                    workspace.scan_workspace(ws, source, oracle_hashes=workspace.content_hashes(
                        b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_utf16_input_requires_valid_text_and_allows_non_oracle_text(self):
        ws = self.build()
        target = ws.inputs / "notes.md"
        hashes = workspace.content_hashes(oracle_path(self.root).read_bytes())
        target.write_bytes(b"\xff\xfe" + "ordinary notes\r\n".encode("utf-16-le"))
        workspace.check_variant_channels(ws, oracle_hashes=hashes)
        target.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, oracle_hashes=hashes)
        self.assertEqual(caught.exception.code, "OAH_EVAL_CHANNEL_UNCHECKED")

    def npm(self):
        prefix = self.scratch / "npm"
        shim = npm_layout(prefix, "@test/cli", {"claude": "cli.js"}, command="claude",
                          script_rel="cli.js", local_node=True)
        script = prefix / "node_modules" / "@test" / "cli" / "cli.js"
        script.write_bytes((self.root / "variants" / "fake_variant.py").read_bytes())
        if os.name != "nt":
            # A real, tiny fake runtime avoids requiring Node or an npm installer.
            node = prefix / "node.exe"
            node.write_bytes(("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n').encode("utf-8"))
            node.chmod(0o755)
        return shim, script

    def test_verified_npm_entry_is_scanned_and_frozen(self):
        shim, script = self.npm()
        ws = self.build()
        resolved = proc.classify(shim, "claude", platform="win32", search_path=str(shim.parent))
        with mock.patch.object(commands, "resolve_command", return_value=resolved):
            argv = commands.resolve_argv(["claude"], self.env.env, "variant", cwd=ws.repo)
        files = workspace.check_variant_channels(ws, argv=argv)
        self.assertEqual(files[str(script)], hashlib.sha256(script.read_bytes()).hexdigest())
        self.assertIn(str(shim.parent / "node.exe"), files)
        # Spelling the same argv directly conveys no resolver provenance.
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, argv=list(argv))
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        external = self.scratch / "public.txt"
        external.write_bytes(b"ordinary external input\n")
        argv.append(str(external))
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, argv=argv)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        argv.pop()
        entry = argv[1]
        argv[1] = str(external)
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, argv=argv)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        argv[1] = entry
        script.write_bytes(oracle_path(self.root).read_bytes())
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, argv=argv, oracle_hashes=workspace.content_hashes(
                script.read_bytes()))
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_exploratory_source_is_an_identifier(self):
        source = plan.load_run_source(self.root / "plan.toml")
        run_dir = runner.run_eval(source, self.env.runs, env=self.env.env, timeout_seconds=60)
        run = json.loads((run_dir / "run.json").read_bytes())
        self.assertIsNone(run["prereg_sha256"])
        expected = hashlib.sha256(json.dumps(source.data, ensure_ascii=False, sort_keys=True).encode(
            "utf-8")).hexdigest()
        self.assertEqual(run["source"], "sha256:" + expected)

    @unittest.skipUnless(os.name != "nt", "POSIX fake runtime; Windows resolution is checked separately")
    def test_npm_prereg_run_and_changed_entry_refusal(self):
        shim, script = self.npm()
        set_plan(self.root, '"{python}", "fake_variant.py"', '"claude"')
        configure_fixture(self.root, self.env.env)
        out = self.root / "prereg.json"

        def resolve(name, search_path):
            return proc.classify(shim, name, platform="win32", search_path=search_path)

        with mock.patch.object(commands, "resolve_command", side_effect=resolve):
            plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
            original = script.read_bytes()
            script.write_bytes(original + b"\n# changed installed entry\n")
            with self.assertRaises(EvalError) as caught:
                runner.run_eval(plan.load_frozen(out), self.env.runs, env=self.env.env,
                                timeout_seconds=60, run_id="changed")
            self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_MISMATCH")
            self.assertEqual(self.env.launched(), [])
            self.assertFalse(self.env.runs.exists())
            script.write_bytes(original)
            run_dir = runner.run_eval(plan.load_frozen(out), self.env.runs, env=self.env.env,
                                      timeout_seconds=60)
            self.assertEqual(self.env.launched(), ["miss", "catch"])
            self.assertEqual(read_result(run_dir, "baseline")["exit_code"], 0)
            self.assertEqual(read_result(run_dir, "improved")["exit_code"], 0)

    @unittest.skipUnless(os.name == "nt", "native Windows PATH/npm resolution")
    def test_windows_npm_resolver_entry_passes_scan(self):
        shim, script = self.npm()
        ws = self.build()
        argv = commands.resolve_argv(["claude"], dict(self.env.env, PATH=str(shim.parent)),
                                     "variant", cwd=ws.repo)
        files = workspace.check_variant_channels(ws, argv=argv)
        self.assertEqual(files[str(script)], hashlib.sha256(script.read_bytes()).hexdigest())


class CorrectionC6Test(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)

    source = CorrectionC3Test.source
    build = CorrectionC3Test.build

    def test_bomless_utf16_oracle_copy_refused_before_freeze(self):
        oracle = oracle_path(self.root)
        text = oracle.read_bytes().decode("utf-8").replace("\r\n", "\n")
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                out = self.root / (encoding + ".json")
                (case_dir(self.root) / "inputs" / "notes.md").write_bytes(
                    text.replace("\n", "\r\n").encode(encoding))
                with self.assertRaises(EvalError) as caught:
                    plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle], out)
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                self.assertNotIn(str(oracle.parent), str(caught.exception))
                self.assertFalse(out.exists())
                self.assertFalse(out.with_suffix(".json.sha256").exists())

    def test_bomless_utf16_oracle_copy_refused_by_run_scanner(self):
        source = self.source()
        ws = self.build(source)
        raw = oracle_path(self.root).read_bytes()
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                (ws.inputs / "notes.md").write_bytes(raw.decode("utf-8").encode(encoding))
                with self.assertRaises(EvalError) as caught:
                    workspace.scan_workspace(ws, source, oracle_hashes=workspace.content_hashes(
                        b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_binary_without_utf16_nul_pattern_keeps_valid_decodings_and_raw_checks(self):
        source = self.source()
        ws = self.build(source)
        # Even length, invalid UTF-16 LE, but fewer than one quarter NULs in either byte lane.
        raw = b"\x00\xd8\x01\x02\x03\x04\x05\x06\x07\x08"
        (ws.inputs / "binary.dat").write_bytes(raw)
        workspace.scan_workspace(ws, source, oracle_hashes=workspace.content_hashes(
            oracle_path(self.root).read_bytes()))
        with self.assertRaises(UnicodeError):
            raw.decode("utf-16-le")
        self.assertEqual(workspace.content_hashes(raw), frozenset({hashlib.sha256(raw).hexdigest(),
                         hashlib.sha256(raw.decode("utf-16-be").encode("utf-8")).hexdigest()}))

    def test_bomless_utf16_accepts_one_valid_byte_order(self):
        ws = self.build()
        hashes = workspace.content_hashes(oracle_path(self.root).read_bytes())
        text = "Ø ordinary notes\r\n"
        normalized = hashlib.sha256(text.replace("\r\n", "\n").rstrip().encode("utf-8")).hexdigest()
        for encoding, other in (("utf-16-le", "utf-16-be"), ("utf-16-be", "utf-16-le")):
            with self.subTest(encoding=encoding):
                raw = text.encode(encoding)
                with self.assertRaises(UnicodeError):
                    raw.decode(other)
                (ws.inputs / "notes.md").write_bytes(raw)
                workspace.check_variant_channels(ws, oracle_hashes=hashes)
                self.assertIn(normalized, workspace.content_hashes(raw))

    def test_binary_matching_nul_pattern_keeps_raw_checks(self):
        ws = self.build()
        raw = b"\x00\x00\x00\xd8\xd8\x00"
        (ws.inputs / "notes.md").write_bytes(raw)
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.assertRaises(UnicodeError):
                raw.decode(encoding)
        workspace.check_variant_channels(ws)
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, oracle_hashes=(hashlib.sha256(raw).hexdigest(),))
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertIsNone(caught.exception.__cause__)


class CorrectionC7Test(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)

    source = CorrectionC3Test.source
    build = CorrectionC3Test.build

    def test_oracle_blocks_refused_before_freeze(self):
        oracle = oracle_path(self.root)
        text = oracle.read_bytes().decode("utf-8")
        oracle.write_bytes("\n".join(line + " \t" for line in text.split("\n")).encode("utf-8"))
        shapes = (text + "\n", "# Notes\n\n" + text + "\nEnd of notes\n",
                  "# Notes\n" + "\n".join(line + " \t" for line in text.split("\n")) + "\n")
        encodings = (("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b""),
                     ("utf-16-be", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"))
        for index, content in enumerate(shapes):
            for encoding, bom in encodings:
                with self.subTest(shape=index, encoding=encoding, bom=bool(bom)):
                    out = self.root / f"blocks-{index}-{encoding}-{bool(bom)}.json"
                    (case_dir(self.root) / "inputs" / "notes.md").write_bytes(
                        bom + content.replace("\r\n", "\n").replace("\n", "\r\n").encode(encoding))
                    with self.assertRaises(EvalError) as caught:
                        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle], out)
                    self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                    self.assertNotIn(str(oracle.parent), str(caught.exception))
                    self.assertFalse(out.exists())
                    self.assertFalse(out.with_suffix(".json.sha256").exists())

    def test_oracle_blocks_refused_before_any_run_launch(self):
        text = oracle_path(self.root).read_bytes().decode("utf-8")
        for value in (text + "\n", "# Notes\n\n" + text + "\nEnd of notes\n"):
            with self.subTest(heading=value.startswith("#")):
                root, env = fixture_copy(self), Env(self)
                configure_fixture(root, env.env)
                out = root / "prereg.json"
                oracle = oracle_path(root)
                plan.preregister(plan.load_plan(root / "plan.toml"), [oracle], out)
                with self.assertRaises(EvalError) as caught:
                    runner.run_eval(plan.load_frozen(out), env.runs, env=dict(env.env, LANG=value),
                                    timeout_seconds=60)
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                self.assertNotIn(str(oracle.parent), str(caught.exception))
                self.assertEqual(env.launched(), [])
                self.assertFalse(env.runs.exists())

    def test_short_oracle_embedded_in_notes_is_allowed_but_whole_copy_refuses(self):
        oracle = oracle_path(self.root)
        raw = b'case="derived-index"\n[[defect]]\nid="D"\nkeywords=["x"]\n'
        self.assertLess(len(raw), 64)
        oracle.write_bytes(raw)
        (case_dir(self.root) / "inputs" / "notes.md").write_bytes(b"# Notes\n" + raw + b"Other text\n")
        env = Env(self)
        run_fixture(self, self.root, env, frozen=True)
        self.assertEqual(env.launched(), ["miss", "catch"])
        ws = self.build()
        (ws.inputs / "notes.md").write_bytes(raw)
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw))
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_oracle_hash_normalization_strips_trailing_whitespace_per_line(self):
        normalized = b"first line\nsecond line"
        digest = hashlib.sha256(normalized).hexdigest()
        for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                raw = "first line \t\r\nsecond line\t \r\n".encode(encoding)
                self.assertIn(digest, workspace.content_hashes(raw))

    def test_embedded_oracle_blocks_refused_in_retained_blobs_and_working_files(self):
        source = self.source()
        raw = oracle_path(self.root).read_bytes()
        hashes, blocks = workspace.content_hashes(raw), workspace.oracle_block_checks(raw)
        text = "Introduction: " + raw.decode("utf-8") + "\nOther text\n"
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            for channel in ("working-file", "git-blob"):
                with self.subTest(encoding=encoding, channel=channel):
                    ws = self.build(source)
                    data = text.encode(encoding)
                    if channel == "working-file":
                        (ws.inputs / "notes.md").write_bytes(data)
                    else:
                        subprocess.run(["git", "-C", str(ws.repo), "hash-object", "-w", "--stdin"],
                                       input=data, capture_output=True, check=True)
                    with self.assertRaises(EvalError) as caught:
                        workspace.scan_workspace(ws, source, oracle_hashes=hashes, oracle_blocks=blocks)
                    self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                    self.assertIn(channel, str(caught.exception))

    def test_preregistration_keeps_only_block_fingerprints_and_legacy_run_refuses(self):
        env = Env(self)
        out = self.root / "prereg.json"
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        data = plan.load_frozen(out).data
        checks = data["cases"][0].pop("oracle_block_checks")
        self.assertTrue(checks)
        for check in checks:
            self.assertEqual(set(check), {"length", "rolling", "sha256"})
            self.assertIsInstance(check["length"], int)
            self.assertIsInstance(check["rolling"], int)
            self.assertRegex(check["sha256"], r"^[0-9a-f]{64}$")
        # Recreate the previous format with a valid sidecar, so run must request a fresh preregistration.
        raw = json.dumps(data).encode("utf-8")
        out.write_bytes(raw)
        out.with_suffix(".json.sha256").write_text(hashlib.sha256(raw).hexdigest(), encoding="utf-8")
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_frozen(out), env.runs, env=env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_CHANNEL_UNCHECKED")
        self.assertIn("preregister again", str(caught.exception))
        self.assertEqual(env.launched(), [])
        self.assertFalse(env.runs.exists())

    def test_repeated_rolling_collisions_refuse_with_bounded_verification_work(self):
        ws = self.build()
        # These base-257 digits encode 49 * 2**64: a real rolling collision with 64 NULs.
        raw = b"\0" * 55 + bytes((47, 127, 76, 96, 78, 88, 85, 122, 49))
        # Supply the synthetic long fingerprint directly: interpreting these
        # non-TOML bytes in UTF-16 also produces short forms under the new rule.
        blocks = [{"length": len(raw), "rolling": workspace._rolling_hash(raw),
                   "sha256": hashlib.sha256(raw).hexdigest()}]
        self.assertEqual(blocks[0]["rolling"], 0)
        (ws.inputs / "notes.md").write_bytes(b"\0" * 4096)
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                             oracle_blocks=blocks)
        self.assertEqual(caught.exception.code, "OAH_EVAL_CHANNEL_UNCHECKED")
        self.assertIn("work limit", str(caught.exception))

    def test_bomless_utf16_grader_path_refused_before_freeze(self):
        oracle = oracle_path(self.root)
        text = "Read file://" + str(oracle.parent / "other-name") + "\n"
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                out = self.root / (encoding + ".json")
                (case_dir(self.root) / "inputs" / "notes.md").write_bytes(text.encode(encoding))
                with self.assertRaises(EvalError) as caught:
                    plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle], out)
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                self.assertNotIn(str(oracle.parent), str(caught.exception))
                self.assertFalse(out.exists())
                self.assertFalse(out.with_suffix(".json.sha256").exists())

    def test_bomless_utf16_grader_path_refused_in_files_and_retained_blobs(self):
        source = self.source()
        oracle = oracle_path(self.root)
        text = "Read @" + str(oracle.parent / "other-name") + "\n"
        for encoding in ("utf-16-le", "utf-16-be"):
            for channel in ("working-file", "git-blob"):
                with self.subTest(encoding=encoding, channel=channel):
                    ws = self.build(source)
                    raw = text.encode(encoding)
                    if channel == "working-file":
                        (ws.inputs / "notes.md").write_bytes(raw)
                    else:
                        subprocess.run(["git", "-C", str(ws.repo), "hash-object", "-w", "--stdin"],
                                       input=raw, capture_output=True, check=True)
                    with self.assertRaises(EvalError) as caught:
                        workspace.scan_workspace(ws, source, oracle_paths=workspace.oracle_path_checks([oracle]))
                    self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                    self.assertIn(channel, str(caught.exception))
                    self.assertNotIn(str(oracle.parent), str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "native Windows UTF-16 grader path case/separator folding")
    def test_windows_bomless_utf16_grader_path_refused(self):
        ws = self.build()
        oracle = oracle_path(self.root)
        text = "Read file://" + str(oracle.parent / "other-name").upper().replace("\\", "/")
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                (ws.inputs / "notes.md").write_bytes(text.encode(encoding))
                with self.assertRaises(EvalError) as caught:
                    workspace.check_variant_channels(ws, oracle_paths=workspace.oracle_path_checks([oracle]))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")


class CorrectionC8Test(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)

    source = CorrectionC3Test.source
    build = CorrectionC3Test.build

    def test_multilingual_utf16_oracle_copies_refused_before_freeze(self):
        oracle = oracle_path(self.root)
        public = oracle.read_bytes().decode("utf-8")
        for index, language in enumerate(("가나다라마바사아자차카타파하", "日本語中文漢字資料説明")):
            localized = public.replace('summary = "', 'summary = "' + language * 500, 1)
            for shape, expected, notes in (("localized", localized, localized),
                                           ("embedded", public, language * 500 + "\n" + public + language * 500)):
                oracle.write_bytes(expected.encode("utf-8"))
                for encoding in ("utf-16-le", "utf-16-be"):
                    with self.subTest(language=language, shape=shape, encoding=encoding):
                        raw = notes.replace("\n", "\r\n").encode(encoding)
                        self.assertLess(max(raw[::2].count(0), raw[1::2].count(0)) * 4, len(raw) // 2)
                        (case_dir(self.root) / "inputs" / "notes.md").write_bytes(raw)
                        out = self.root / f"prereg-{index}-{shape}-{encoding}.json"
                        with self.assertRaises(EvalError) as caught:
                            plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle], out)
                        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                        self.assertNotIn(str(oracle.parent), str(caught.exception))
                        self.assertFalse(out.exists())
                        self.assertFalse(out.with_suffix(".json.sha256").exists())

    def test_multilingual_utf16_oracle_blocks_refused_in_files_and_blobs(self):
        source = self.source()
        raw = oracle_path(self.root).read_bytes()
        hashes, blocks = workspace.content_hashes(raw), workspace.oracle_block_checks(raw)
        notes = "한국어日本語漢字" * 500 + "\n" + raw.decode("utf-8") + "\n" + "日本語한국어" * 500
        for encoding in ("utf-16-le", "utf-16-be"):
            for channel in ("working-file", "git-blob"):
                with self.subTest(encoding=encoding, channel=channel):
                    ws = self.build(source)
                    data = notes.encode(encoding)
                    if channel == "working-file":
                        (ws.inputs / "notes.md").write_bytes(data)
                    else:
                        subprocess.run(["git", "-C", str(ws.repo), "hash-object", "-w", "--stdin"],
                                       input=data, capture_output=True, check=True)
                    with self.assertRaises(EvalError) as caught:
                        workspace.check_variant_channels(ws, oracle_hashes=hashes, oracle_blocks=blocks)
                    self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                    self.assertIn(channel, str(caught.exception))

    def test_dotted_model_settings_preregister_and_run(self):
        config = ('[engines.codex.tiers]\n'
                  'high = { model = "gpt-6.1-sol", effort = "high", timeout_min = 60 }\n'
                  'low = { model = "gpt-6-luna", effort = "low", timeout_min = 10 }\n'
                  '[metadata]\nversion = "1.2.3"\nlabel = "ordinary.words"\nchannel = ".preview"\n')
        (self.root / "variant.toml").write_bytes(config.encode("utf-8"))
        set_plan(self.root, 'name = "baseline"', 'name = "baseline"\nconfig = "variant.toml"')
        env = Env(self)
        run_dir = run_fixture(self, self.root, env, frozen=True)
        self.assertEqual(env.launched(), ["miss", "catch"])
        injected = run_dir / "cases" / "derived-index" / "baseline" / "workspace" / ".oah" / "config.toml"
        self.assertEqual(injected.read_bytes(), config.encode("utf-8"))

    def test_dotted_argument_values_are_plain_text(self):
        ws = self.build()
        workspace.check_variant_channels(ws, argv=[sys.executable, "--model=gpt-6.1-sol", "1.2.3", ".preview"])

    def test_scalar_settings_still_check_actual_file_references(self):
        ws = self.build()
        target = ws.inputs / "gpt-6.1-sol"
        target.write_bytes(b"ordinary model notes\n")
        dependencies = workspace.check_variant_channels(ws, config_text="model = " + json.dumps(str(target)))
        self.assertEqual(dependencies["{workspace}/inputs/gpt-6.1-sol"], hashlib.sha256(target.read_bytes()).hexdigest())
        for value in ("missing/file", "./missing", "../missing", "~missing", "C:missing", target.name):
            with self.subTest(value=value):
                with self.assertRaises(EvalError) as caught:
                    workspace.check_variant_channels(ws, config_text="model = " + json.dumps(value),
                                                     plan_dir=str(ws.inputs))
                self.assertEqual(caught.exception.code, "OAH_EVAL_CHANNEL_UNCHECKED")

    def test_scalar_settings_keep_oracle_content_and_literal_path_checks(self):
        ws = self.build()
        oracle = oracle_path(self.root)
        raw = oracle.read_bytes()
        for value in (raw.decode("utf-8"), "Read file://" + str(oracle.parent / "other-name")):
            with self.subTest(content=value.startswith("#")):
                with self.assertRaises(EvalError) as caught:
                    workspace.check_variant_channels(
                        ws, config_text="model = " + json.dumps(value),
                        oracle_hashes=workspace.content_hashes(raw), oracle_blocks=workspace.oracle_block_checks(raw),
                        oracle_paths=workspace.oracle_path_checks([oracle]))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                self.assertNotIn(str(oracle.parent), str(caught.exception))


class CorrectionC9Test(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)

    source = CorrectionC3Test.source
    build = CorrectionC3Test.build

    def test_rstripped_oracle_copies_refused_before_freeze(self):
        oracle = oracle_path(self.root)
        text = oracle.read_text(encoding="utf-8").rstrip()
        for heading in ("", "# Notes\n\n"):
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
                with self.subTest(heading=bool(heading), encoding=encoding):
                    (case_dir(self.root) / "inputs" / "notes.md").write_bytes((heading + text).encode(encoding))
                    out = self.root / f"refused-{bool(heading)}-{encoding}.json"
                    with self.assertRaises(EvalError) as caught:
                        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle], out)
                    self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                    self.assertFalse(out.exists())
                    self.assertFalse(out.with_suffix(".json.sha256").exists())
        (case_dir(self.root) / "inputs" / "notes.md").write_bytes(b"ordinary notes")
        (case_dir(self.root) / "task.md").write_bytes(text.encode("utf-8"))
        with self.assertRaises(EvalError) as caught:
            plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle], self.root / "prompt.json")
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_rstripped_oracle_copies_refused_by_frozen_run_scanner(self):
        out = self.root / "prereg.json"
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        entry = plan.load_frozen(out).data["cases"][0]
        text = oracle_path(self.root).read_text(encoding="utf-8").rstrip()
        source = self.source()
        for heading in ("", "# Notes\n\n"):
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
                with self.subTest(heading=bool(heading), encoding=encoding):
                    ws = self.build(source)
                    (ws.inputs / "notes.md").write_bytes((heading + text).encode(encoding))
                    with self.assertRaises(EvalError) as caught:
                        workspace.scan_workspace(ws, source, oracle_hashes=entry["oracle_match_sha256"],
                                                 oracle_blocks=entry["oracle_block_checks"])
                    self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        env = Env(self)
        for heading in ("", "# Notes\n\n"):
            with self.subTest(launch=True, heading=bool(heading)):
                with self.assertRaises(EvalError) as caught:
                    runner.run_eval(plan.load_frozen(out), env.runs, env=dict(env.env, LANG=heading + text),
                                    timeout_seconds=60)
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                self.assertEqual(env.launched(), [])
                self.assertFalse(env.runs.exists())

    def test_one_canonical_form_for_hashes_blocks_short_oracles_and_paths(self):
        canonical = 'case="derived-index"\n[[defect]]\nid="D"\nkeywords=["x"]'
        decorated = "\r\n \t\r" + canonical.replace("\n", " \t\r") + "\t \r\n\r"
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertLess(len(canonical.encode("utf-8")), workspace.ORACLE_BLOCK_MIN)
        ws = self.build()
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                raw = decorated.encode(encoding)
                self.assertIn(digest, workspace.content_hashes(raw))
                self.assertEqual(workspace.oracle_block_checks(raw), [])
                (ws.inputs / "notes.md").write_bytes(raw)
                with self.assertRaises(EvalError) as caught:
                    workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(
                        canonical.encode("utf-8")))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        path = oracle_path(self.root)
        path_text = "\r\n" + str(path.parent) + " \t\r\n"
        (ws.inputs / "notes.md").write_bytes(path_text.encode("utf-16-be"))
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, oracle_paths=workspace.oracle_path_checks([path]))
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def large_executable(self):
        # A real local interpreter with inert appended bytes; no installer or network.
        executable = self.root / ("engine.exe" if os.name == "nt" else "engine")
        shutil.copyfile(sys.executable, executable)
        executable.chmod(0o755)
        with executable.open("ab") as handle:
            handle.truncate((64 << 20) + 4096)
        return executable

    def test_large_executable_scanned_frozen_and_changed_digest_refused(self):
        executable = self.large_executable()
        ws = self.build()
        files = workspace.check_variant_channels(ws, argv=[str(executable)])
        digest = case_mod.file_sha256(executable)
        self.assertEqual(files[str(executable)], digest)
        # PATH resolution avoids the plan's absolute-argument digest check: this
        # specifically exercises the scanner's frozen executable dependency.
        (self.root / "plan.toml").write_text(
            '[[case]]\npath="cases/derived-index"\n[[variant]]\nname="engine"\ncommand=["'
            + executable.name + '", "--version"]\n', encoding="utf-8")
        env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ.get("PATH", ""))
        out = self.root / "prereg.json"
        with mock.patch.dict(os.environ, env):
            plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        frozen = plan.load_frozen(out)
        self.assertEqual(frozen.data["variants"][0]["channel_files"]["derived-index"][str(executable)], digest)
        runs = make_tmp_dir(self) / "runs"
        if os.name != "nt":
            # Relocated Windows python.exe may need sibling DLLs; native tests
            # below cover scanning/freezing without assuming that runtime layout.
            run = runner.run_eval(frozen, runs, env=env, timeout_seconds=60)
            self.assertEqual(read_result(run, "engine")["exit_code"], 0)
        with executable.open("r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            handle.write(b"X")
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(frozen, runs, env=env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_PREREG_MISMATCH")

    def test_large_verified_npm_runtime_is_scanned_and_frozen(self):
        prefix = make_tmp_dir(self)
        shim = npm_layout(prefix, "@test/cli", {"claude": "cli.js"}, command="claude",
                          script_rel="cli.js", local_node=True)
        node = prefix / "node.exe"
        node.unlink()
        self.large_executable().rename(node)
        resolved = proc.classify(shim, "claude", platform="win32", search_path=str(prefix))
        ws = self.build()
        with mock.patch.object(commands, "resolve_command", return_value=resolved):
            argv = commands.resolve_argv(["claude"], dict(os.environ), "variant", cwd=ws.repo)
        raw = oracle_path(self.root).read_bytes()
        files = workspace.check_variant_channels(ws, argv=argv, oracle_hashes=workspace.content_hashes(raw),
                                                 oracle_blocks=workspace.oracle_block_checks(raw))
        self.assertEqual(files[str(node)], case_mod.file_sha256(node))
        script = prefix / "node_modules" / "@test" / "cli" / "cli.js"
        self.assertEqual(files[str(script)], case_mod.file_sha256(script))

    @unittest.skipUnless(os.name == "nt", "native Windows large executable scanning")
    def test_windows_large_executable_scan_freeze_and_leak_refusal(self):
        self.test_large_executable_scanned_frozen_and_changed_digest_refused()
        self.test_large_executable_embedded_oracle_refused()

    def test_large_executable_embedded_oracle_refused(self):
        executable = self.large_executable()
        raw = oracle_path(self.root).read_bytes()
        text = raw.decode("utf-8").rstrip()
        ws = self.build()
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                # Cross a stream boundary well beyond the former refusal threshold.
                offset = (64 << 20) - 128
                with executable.open("r+b") as handle:
                    handle.seek(offset)
                    handle.write(("# Notes\n" + text).encode(encoding))
                with self.assertRaises(EvalError) as caught:
                    workspace.check_variant_channels(ws, argv=[str(executable)],
                        oracle_hashes=workspace.content_hashes(raw), oracle_blocks=workspace.oracle_block_checks(raw))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                self.assertNotIn(text, str(caught.exception))
                with executable.open("r+b") as handle:
                    handle.seek(offset)
                    handle.write(b"\0" * (len(text) * 2 + 100))

    def test_binary_regions_keep_oracle_checks_despite_invalid_surrounding_bytes(self):
        ws = self.build()
        raw = oracle_path(self.root).read_bytes()
        text = raw.decode("utf-8").rstrip()
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                invalid = b"\xff\xff\x00\xd8\xd8\x00"
                content = "# Notes\r\n" + text.replace("\n", " " * 600 + "\r\n")
                (ws.inputs / "binary.dat").write_bytes(invalid + content.encode(encoding) + invalid)
                with mock.patch.object(workspace, "NORMALIZE_LIMIT", 256):
                    with self.assertRaises(EvalError) as caught:
                        workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                                         oracle_blocks=workspace.oracle_block_checks(raw))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_utf16_regions_in_odd_length_binary_channels_are_checked(self):
        ws = self.build()
        raw = oracle_path(self.root).read_bytes()
        text = raw.decode("utf-8").rstrip()
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                data = b"\xff\xff\x00\xd8\xd8\x00" + text.encode(encoding) + b"X"
                self.assertEqual(len(data) % 2, 1)
                (ws.inputs / "binary.dat").write_bytes(data)
                with mock.patch.object(workspace, "NORMALIZE_LIMIT", 256):
                    with self.assertRaises(EvalError) as caught:
                        workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                                         oracle_blocks=workspace.oracle_block_checks(raw))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_reused_content_pass_checks_current_bytes_and_oracle_context(self):
        ws = self.build()
        raw = oracle_path(self.root).read_bytes()
        path = ws.inputs / "notes.md"
        payload = b"# Notes\n" + raw.rstrip()
        path.write_bytes(payload.ljust(2048, b" "))
        # A pass without oracle checks cannot authorize the same bytes with an oracle.
        workspace.check_variant_channels(ws)
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                             oracle_blocks=workspace.oracle_block_checks(raw))
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        path.write_bytes(b"ordinary notes".ljust(2048, b" "))
        workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                         oracle_blocks=workspace.oracle_block_checks(raw))
        before = path.stat()
        path.write_bytes(payload.ljust(2048, b" "))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        # Same name, size and timestamps still require the current content digest.
        with self.assertRaises(EvalError) as caught:
            workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                             oracle_blocks=workspace.oracle_block_checks(raw))
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")

    def test_legacy_normalization_preregistration_refused(self):
        out = self.root / "prereg.json"
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        data = plan.load_frozen(out).data
        data.pop("oracle_normalization", None)
        raw = json.dumps(data).encode("utf-8")
        out.write_bytes(raw)
        out.with_suffix(".json.sha256").write_text(hashlib.sha256(raw).hexdigest(), encoding="utf-8")
        env = Env(self)
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_frozen(out), env.runs, env=env.env, timeout_seconds=60)
        self.assertEqual(caught.exception.code, "OAH_EVAL_CHANNEL_UNCHECKED")
        self.assertIn("preregister again", str(caught.exception))
        self.assertFalse(env.runs.exists())

    def test_large_executable_leak_refused_at_preregistration_and_run(self):
        executable = self.large_executable()
        (self.root / "plan.toml").write_text(
            '[[case]]\npath="cases/derived-index"\n[[variant]]\nname="engine"\ncommand=['
            + json.dumps(str(executable)) + ', "--version"]\n', encoding="utf-8")
        out = self.root / "prereg.json"
        plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], out)
        text = oracle_path(self.root).read_text(encoding="utf-8").rstrip()
        with executable.open("r+b") as handle:
            handle.seek((64 << 20) - 128)
            handle.write(("# Notes\n" + text).encode("utf-16-be"))
        env = Env(self)
        with self.assertRaises(EvalError) as caught:
            runner.run_eval(plan.load_frozen(out), env.runs, env=env.env, timeout_seconds=60)
        # Absolute command files are checked for changed digests even before scanning.
        self.assertIn(caught.exception.code, ("OAH_EVAL_ISOLATION", "OAH_EVAL_PREREG_MISMATCH"))
        self.assertFalse(env.runs.exists())
        rejected = self.root / "rejected.json"
        with self.assertRaises(EvalError) as caught:
            plan.preregister(plan.load_plan(self.root / "plan.toml"), [oracle_path(self.root)], rejected)
        self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
        self.assertFalse(rejected.exists())
        self.assertFalse(rejected.with_suffix(".json.sha256").exists())

    def test_streamed_working_files_and_retained_blobs_share_canonical_checks(self):
        raw = oracle_path(self.root).read_bytes()
        text = raw.decode("utf-8").rstrip()
        source = self.source()
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            for channel in ("working-file", "git-blob"):
                with self.subTest(encoding=encoding, channel=channel):
                    ws = self.build(source)
                    data = ("\r\n# Notes\r\n" + text.replace("\n", " \t\r\n")).encode(encoding)
                    if channel == "working-file":
                        (ws.inputs / "notes.md").write_bytes(data)
                    else:
                        subprocess.run(["git", "-C", str(ws.repo), "hash-object", "-w", "--stdin"],
                                       input=data, capture_output=True, check=True)
                    with mock.patch.object(workspace, "NORMALIZE_LIMIT", 256):
                        with self.assertRaises(EvalError) as caught:
                            workspace.scan_workspace(ws, source, oracle_hashes=workspace.content_hashes(raw),
                                                     oracle_blocks=workspace.oracle_block_checks(raw))
                    self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                    self.assertIn(channel, str(caught.exception))

    def test_streamed_short_oracle_requires_whole_channel_equality(self):
        raw = b'case="derived-index"\n[[defect]]\nid="D"\nkeywords=["x"]'
        ws = self.build()
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                path = ws.inputs / "notes.md"
                path.write_bytes(("\r\n" * 200 + raw.decode("utf-8") + " \t\r\n" * 200).encode(encoding))
                with mock.patch.object(workspace, "NORMALIZE_LIMIT", 256):
                    with self.assertRaises(EvalError) as caught:
                        workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                                         oracle_blocks=workspace.oracle_block_checks(raw))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")
                path.write_bytes(("Heading\n" + raw.decode("utf-8") + "\nOther text").encode(encoding))
                with mock.patch.object(workspace, "NORMALIZE_LIMIT", 16):
                    workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                                     oracle_blocks=workspace.oracle_block_checks(raw))

    def test_streamed_malformed_marked_utf16_is_still_unchecked(self):
        ws = self.build()
        # Valid UTF-8 bytes are not a substitute for validating marked UTF-16.
        data = b"\xff\xfe" + "ordinary notes\n".encode("utf-16-le") * 100 + b"\x00"
        (ws.inputs / "notes.md").write_bytes(data)
        with mock.patch.object(workspace, "NORMALIZE_LIMIT", 256):
            with self.assertRaises(EvalError) as caught:
                workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(
                    oracle_path(self.root).read_bytes()))
        self.assertEqual(caught.exception.code, "OAH_EVAL_CHANNEL_UNCHECKED")

    def test_stream_normalization_across_long_whitespace_and_decoder_boundaries(self):
        raw = oracle_path(self.root).read_bytes()
        text = raw.decode("utf-8").rstrip()
        ws = self.build()
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                content = "\r\n\t\r# Notes\r" + text.replace("\n", " " * 300 + "\t\r\n")
                (ws.inputs / "notes.md").write_bytes(content.encode(encoding))
                with mock.patch.object(workspace, "NORMALIZE_LIMIT", 256):
                    with self.assertRaises(EvalError) as caught:
                        workspace.check_variant_channels(ws, oracle_hashes=workspace.content_hashes(raw),
                                                         oracle_blocks=workspace.oracle_block_checks(raw))
                self.assertEqual(caught.exception.code, "OAH_EVAL_ISOLATION")


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
