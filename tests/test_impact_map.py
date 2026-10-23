# 영향 지도 저장 검사 — 빌드·신원·낡음 판정·증분 갱신·거부·훅을 임시 저장소에서 실제 프로세스로 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

from oah.impact import ImpactError, ensure_fresh, mapstore
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def run_impact(cwd: pathlib.Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run `python -m oah impact …` in a real process."""
    env = dict(os.environ, PYTHONPATH=SRC)
    env.pop("OAH_STATE_DIR", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "oah", "impact", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=120)



def canonical_digest(data: object) -> str:
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def head(repo: pathlib.Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


BASE = {
    "src/app.py": "from lib import util\nprint('app')\n",
    "src/lib/util.py": "def helper():\n    return 1\n",
    "tools/check.py": "print('check')\n",
    ".github/workflows/ci.yml": "steps:\n  - run: python tools/check.py\n  - run: sha256sum src/lib/util.py\n",
    "docs/guide.md": "See src/app.py and util.py.\n",
    "docs/wiki/app.md": "The [[app]] entry point is src/app.py.\n",
}


class MapStoreTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, BASE)

    def full_build_elsewhere(self) -> mapstore.MapInfo:
        return mapstore.build(self.repo, env={"OAH_STATE_DIR": str(make_tmp_dir(self) / "state")})

    def test_build_records_identity(self):
        info = mapstore.build(self.repo, env={})
        self.assertEqual(info.commit, head(self.repo))
        self.assertEqual(info.action, "built")
        self.assertRegex(info.tool_digest, r"\A[0-9a-f]{64}\Z")
        stored = json.loads(info.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["commit"], info.commit)
        self.assertEqual(stored["tool"], info.tool_digest)
        self.assertEqual(stored["result"], canonical_digest(stored["data"]))
        self.assertEqual(info.result_digest, stored["result"])
        self.assertTrue(info.path.is_relative_to(self.repo / ".git" / "oah" / "impact"))
        manifest = stored["manifest"]
        self.assertEqual(set(manifest), {"worktree", "head", "index", "dirty", "untracked", "ledgers", "config",
                                         "tool"})
        self.assertEqual((manifest["head"], manifest["tool"], manifest["config"]), (info.commit, info.tool_digest, None))
        self.assertEqual(stored["manifest_digest"], canonical_digest(manifest))
        self.assertEqual(info.manifest_digest, stored["manifest_digest"])
        self.assertEqual(ensure_fresh(self.repo).action, "fresh")

    def test_commit_makes_map_stale(self):
        mapstore.build(self.repo, env={})
        git_commit(self.repo, {"src/new.py": "x = 1\n"})
        info, reason = mapstore.status(self.repo, env={})
        self.assertEqual(info.action, "stale")
        self.assertIn("HEAD moved", reason)
        refreshed = ensure_fresh(self.repo)
        self.assertEqual((refreshed.action, refreshed.changed, refreshed.commit), ("refreshed", 1, head(self.repo)))
        self.assertEqual(mapstore.status(self.repo, env={})[0].action, "fresh")

    def test_refresh_equals_full_build(self):
        mapstore.build(self.repo, env={})
        git_commit(self.repo, {
            "src/app.py": "from lib import util\nimport helpers\nprint('tools/check.py')\n",
            "src/helpers.py": "VALUE = 2\n",
            "docs/guide.md": "Now see src/helpers.py too.\n",
            "docs/wiki/app.md": None,
            "db/001.sql": "CREATE TABLE widgets (id int);\n",
            "src/store.py": "query('select * from widgets')\n",
        })
        refreshed = ensure_fresh(self.repo)
        self.assertEqual(refreshed.action, "refreshed")
        self.assertEqual(refreshed.result_digest, self.full_build_elsewhere().result_digest)
        git_commit(self.repo, {"db/001.sql": "CREATE TABLE gadgets (id int);\n", "src/store.py": None})
        again = ensure_fresh(self.repo)
        self.assertEqual(again.result_digest, self.full_build_elsewhere().result_digest)
        (self.repo / "db" / "002.sql").write_text("CREATE TABLE sprockets (id int);\n", encoding="utf-8")
        (self.repo / "src" / "app.py").write_text("query('sprockets'); print('src/helpers.py')\n", encoding="utf-8")
        (self.repo / "docs" / "guide.md").unlink()
        dirty = ensure_fresh(self.repo)
        self.assertEqual(dirty.action, "refreshed")
        self.assertEqual(dirty.result_digest, self.full_build_elsewhere().result_digest)

    def test_working_tree_inputs_make_map_stale(self):
        mapstore.build(self.repo, env={})
        (self.repo / "docs" / "guide.md").write_text("See tools/check.py now.\n", encoding="utf-8")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "working tree changed")
        info = ensure_fresh(self.repo)
        self.assertEqual((info.action, info.changed), ("refreshed", 1))
        data = mapstore.load(info.path)["data"]
        self.assertIn(["docs/guide.md", 1, ""], data["hits"]["tools/check.py"])
        stored = mapstore.load(info.path)
        self.assertEqual(list(stored["manifest"]["dirty"]), ["docs/guide.md"])
        (self.repo / "notes.txt").write_text("util.py\n", encoding="utf-8")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "working tree changed")
        self.assertIn("notes.txt", mapstore.load(ensure_fresh(self.repo).path)["manifest"]["untracked"])
        (self.repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        ensure_fresh(self.repo)
        (self.repo / "debug.log").write_text("util.py\n", encoding="utf-8")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "fresh", "ignored files are not inputs")

    def test_index_change_makes_map_stale(self):
        (self.repo / "docs" / "guide.md").write_text("changed\n", encoding="utf-8")
        ensure_fresh(self.repo)
        subprocess.run(["git", "-C", str(self.repo), "add", "docs/guide.md"], check=True)
        self.assertEqual(mapstore.status(self.repo, env={})[1], "index changed")
        self.assertEqual(ensure_fresh(self.repo).action, "refreshed")

    def test_ledgers_and_config_make_map_stale(self):
        (self.repo / ".gitignore").write_text(".oah/tasks/\n", encoding="utf-8")
        git_commit(self.repo, {})
        ensure_fresh(self.repo)
        ledger = self.repo / ".oah" / "tasks" / "T" / "GATES.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("# Gates\n", encoding="utf-8")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "ledgers changed", "ignored ledgers still count")
        info = ensure_fresh(self.repo)
        self.assertEqual(list(mapstore.load(info.path)["manifest"]["ledgers"]), [".oah/tasks/T/GATES.md"])
        ledger.write_text("# Gates\n- [x] G1: done\n", encoding="utf-8")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "ledgers changed")
        ensure_fresh(self.repo)
        (self.repo / ".oah" / "config.toml").write_text("[impact]\nlimit = 3\n", encoding="utf-8")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "config changed")
        info = ensure_fresh(self.repo)
        self.assertEqual((info.action, info.changed), ("refreshed", 1), "the untracked config file is an input too")

    def test_state_folder_inside_worktree_is_not_an_input(self):
        env = {"OAH_STATE_DIR": str(self.repo / "local-state")}
        info = mapstore.build(self.repo, env=env)
        self.assertTrue(info.path.is_relative_to(self.repo / "local-state"))
        self.assertEqual(mapstore.status(self.repo, env=env)[1], "fresh", "storing the map must not stale it")

    def test_inputs_changing_during_build_retry_then_refuse(self):
        real = mapstore.build_data
        calls = []

        def editing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                (self.repo / "docs" / "guide.md").write_text(f"edit {len(calls)}\n", encoding="utf-8")
            return real(*args, **kwargs)

        with patch.object(mapstore, "build_data", editing):
            info = mapstore.build(self.repo, env={})
        self.assertEqual(len(calls), 2, "the build is retried once the inputs settle")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "fresh")
        self.assertEqual(info.manifest_digest, mapstore.status(self.repo, env={})[0].manifest_digest)

        def always_editing(*args, **kwargs):
            calls.append(1)
            (self.repo / "docs" / "guide.md").write_text(f"edit {len(calls)}\n", encoding="utf-8")
            return real(*args, **kwargs)

        before = info.path.read_bytes()
        with patch.object(mapstore, "build_data", always_editing):
            with self.assertRaises(ImpactError) as caught:
                mapstore.build(self.repo, env={})
        self.assertEqual(caught.exception.code, "OAH_IMPACT_STALE")
        self.assertIn("kept changing", str(caught.exception))
        self.assertEqual(info.path.read_bytes(), before, "a map built from moving inputs is never stored")

    def test_refresh_handles_delete_rename(self):
        mapstore.build(self.repo, env={})
        subprocess.run(["git", "-C", str(self.repo), "mv", "src/lib/util.py", "src/lib/tools_util.py"], check=True)
        git_commit(self.repo, {"tools/check.py": None}, message="rename and delete")
        refreshed = ensure_fresh(self.repo)
        self.assertEqual(refreshed.changed, 3)
        self.assertEqual(refreshed.result_digest, self.full_build_elsewhere().result_digest)
        data = mapstore.load(refreshed.path)["data"]
        self.assertNotIn("src/lib/util.py", data["files"])
        self.assertIn("src/lib/tools_util.py", data["files"])

    def test_no_refresh_refuses_stale(self):
        mapstore.build(self.repo, env={})
        git_commit(self.repo, {"src/new.py": "x = 1\n"})
        with self.assertRaises(ImpactError) as caught:
            ensure_fresh(self.repo, refresh=False)
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("OAH_IMPACT_STALE", 69))
        result = run_impact(self.repo, "src/app.py", "--no-refresh")
        self.assertEqual(result.returncode, 69)
        self.assertIn("OAH_IMPACT_STALE", result.stderr)
        self.assertIn("oah impact refresh", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_map_refused_without_refresh(self):
        with self.assertRaises(ImpactError) as caught:
            ensure_fresh(self.repo, refresh=False)
        self.assertEqual(caught.exception.code, "OAH_IMPACT_STALE")
        self.assertIn("oah impact build", str(caught.exception))

    def test_tampered_map_is_stale(self):
        info = mapstore.build(self.repo, env={})
        stored = json.loads(info.path.read_text(encoding="utf-8"))
        stored["data"]["files"]["src/ghost.py"] = "code"
        info.path.write_text(json.dumps(stored), encoding="utf-8")
        status, reason = mapstore.status(self.repo, env={})
        self.assertEqual((status.action, reason), ("stale", "result digest mismatch"))
        rebuilt = ensure_fresh(self.repo)
        self.assertEqual(rebuilt.action, "built")
        self.assertNotIn("src/ghost.py", mapstore.load(rebuilt.path)["data"]["files"])
        info.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "unreadable")
        self.assertEqual(ensure_fresh(self.repo).action, "built")

    def test_tool_change_rebuilds(self):
        first = mapstore.build(self.repo, env={})
        changed = ensure_fresh(self.repo, settings={"min_needle": 5})
        self.assertEqual(changed.action, "built")
        self.assertNotEqual(changed.tool_digest, first.tool_digest)
        same_map = ensure_fresh(self.repo, settings={"limit": 7})
        self.assertEqual(same_map.action, "built", "display-only settings keep the map's tool digest")
        self.assertEqual(same_map.tool_digest, first.tool_digest)
        self.assertEqual(ensure_fresh(self.repo, settings={"limit": 7}).action, "fresh")

    def test_status_creates_nothing(self):
        info, reason = mapstore.status(self.repo, env={})
        self.assertIsNone(info)
        self.assertEqual(reason, "missing")
        self.assertFalse((self.repo / ".git" / "oah").exists())
        result = run_impact(self.repo, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("map: missing", result.stdout)
        self.assertFalse((self.repo / ".git" / "oah").exists())

    def test_no_commit_refused(self):
        empty = make_git_repo(self)
        with self.assertRaises(ImpactError) as caught:
            ensure_fresh(empty)
        self.assertEqual(caught.exception.code, "OAH_IMPACT_NO_COMMIT")
        result = run_impact(empty, "build")
        self.assertEqual(result.returncode, 69)
        self.assertIn("OAH_IMPACT_NO_COMMIT", result.stderr)

    def test_ensure_fresh_reads_config(self):
        (self.repo / ".oah").mkdir()
        (self.repo / ".oah" / "config.toml").write_text("[impact]\nmin_needle = 5\n", encoding="utf-8")
        configured = ensure_fresh(self.repo)
        explicit = mapstore.build(self.repo, env={"OAH_STATE_DIR": str(make_tmp_dir(self) / "s")},
                                  settings={"min_needle": 5})
        self.assertEqual(configured.tool_digest, explicit.tool_digest)
        self.assertEqual(ensure_fresh(self.repo).action, "fresh", "callers without settings agree on the map")

    def test_sql_symlinks_are_excluded(self):
        outside = make_tmp_dir(self) / "outside.sql"
        outside.write_text("CREATE TABLE ghost (id int);\n", encoding="utf-8")
        sql = self.repo / "linked.sql"
        try:
            sql.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        git_commit(self.repo, {})
        info = mapstore.build(self.repo, env={})
        self.assertIsNone(mapstore.load(info.path)["data"]["db"])
        self.assertIsNone(mapstore.snapshot(self.repo, "HEAD")[1]["db"])
        outside.write_text("CREATE TABLE other_ghost (id int);\n", encoding="utf-8")
        self.assertEqual(mapstore.status(self.repo, env={})[1], "fresh")
        self.assertEqual(ensure_fresh(self.repo).result_digest, self.full_build_elsewhere().result_digest)
        sql.unlink()
        sql.write_text("CREATE TABLE real_table (id int);\n", encoding="utf-8")
        updated = ensure_fresh(self.repo)
        self.assertIn("db_table:real_table", mapstore.load(updated.path)["data"]["db"]["entities"])
        self.assertEqual(updated.result_digest, self.full_build_elsewhere().result_digest)
        sql.unlink()
        sql.symlink_to(outside)
        updated = ensure_fresh(self.repo)
        self.assertIsNone(mapstore.load(updated.path)["data"]["db"])
        self.assertEqual(updated.result_digest, self.full_build_elsewhere().result_digest)

    def test_sql_symlink_directory_is_not_read(self):
        outside = make_tmp_dir(self)
        (outside / "001.sql").write_text("CREATE TABLE ghost (id int);\n", encoding="utf-8")
        link = self.repo / "external"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(mapstore.read_worktree(self.repo, ["external/001.sql"]), {})

    @unittest.skipUnless(os.name == "nt", "Windows symlink handling")
    def test_windows_sql_symlink_is_not_read(self):
        self.test_sql_symlinks_are_excluded()

    def test_config_change_during_build_reloads_settings(self):
        config = self.repo / ".oah" / "config.toml"
        config.parent.mkdir()
        config.write_text("[impact]\nmin_needle = 3\n", encoding="utf-8")
        real = mapstore.build_data
        for operation in (mapstore.build, ensure_fresh):
            with self.subTest(operation=operation.__name__):
                config.write_text("[impact]\nmin_needle = 3\n", encoding="utf-8")
                seen = []
                env = {"OAH_STATE_DIR": str(make_tmp_dir(self) / "state")}

                def editing(root, rev, settings, **kwargs):
                    seen.append(settings["min_needle"])
                    data = real(root, rev, settings, **kwargs)
                    if len(seen) == 1:
                        config.write_text("[impact]\nmin_needle = 9\n", encoding="utf-8")
                    return data

                with patch.object(mapstore, "build_data", editing):
                    info = operation(self.repo, env=env)
                self.assertEqual(seen, [3, 9])
                self.assertEqual(info.result_digest, self.full_build_elsewhere().result_digest)
                self.assertNotIn("util", mapstore.load(info.path)["data"]["hits"])
                self.assertEqual(mapstore.status(self.repo, env=env)[1], "fresh")

    def test_config_change_with_explicit_settings_refuses(self):
        config = self.repo / ".oah" / "config.toml"
        config.parent.mkdir()
        config.write_text("[impact]\nmin_needle = 3\n", encoding="utf-8")
        previous = mapstore.build(self.repo, env={})
        before = previous.path.read_bytes()
        real = mapstore.build_data

        def editing(*args, **kwargs):
            data = real(*args, **kwargs)
            config.write_text("[impact]\nmin_needle = 9\n", encoding="utf-8")
            return data

        with patch.object(mapstore, "build_data", editing):
            with self.assertRaises(ImpactError) as caught:
                mapstore.build(self.repo, env={}, settings={"min_needle": 3})
        self.assertEqual(caught.exception.code, "OAH_IMPACT_STALE")
        self.assertIn("oah impact refresh", str(caught.exception))
        self.assertEqual(previous.path.read_bytes(), before)

    def test_worktrees_keep_separate_maps(self):
        other = make_tmp_dir(self) / "other"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "side", str(other)],
                       check=True, env=git_env())
        git_commit(other, {"src/side.py": "y = 2\n"})
        here, there = ensure_fresh(self.repo), ensure_fresh(other)
        self.assertNotEqual(here.path, there.path)
        self.assertEqual(ensure_fresh(self.repo).action, "fresh")
        self.assertEqual(ensure_fresh(other).action, "fresh")

    def test_hooks_refresh_map(self):
        mapstore.build(self.repo, env={})
        for hook in ("hook_post_commit", "hook_post_merge"):
            git_commit(self.repo, {f"src/{hook}.py": "z = 3\n"})
            code = f"import sys, argparse\nfrom oah import {hook}\nsys.exit({hook}.run(argparse.Namespace()))\n"
            result = subprocess.run([sys.executable, "-c", code], cwd=self.repo, capture_output=True, text=True,
                                    encoding="utf-8", env=dict(os.environ, PYTHONPATH=SRC), timeout=120)
            with self.subTest(hook=hook):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("map refreshed", result.stdout)
                self.assertEqual(mapstore.status(self.repo, env={})[0].action, "fresh")

    def test_hook_reports_error_without_traceback(self):
        empty = make_git_repo(self)
        code = "import sys, argparse\nfrom oah import hook_post_commit\nsys.exit(hook_post_commit.run(argparse.Namespace()))\n"
        result = subprocess.run([sys.executable, "-c", code], cwd=empty, capture_output=True, text=True,
                                encoding="utf-8", env=dict(os.environ, PYTHONPATH=SRC), timeout=120)
        self.assertEqual(result.returncode, 69)
        self.assertIn("OAH_IMPACT_NO_COMMIT", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
