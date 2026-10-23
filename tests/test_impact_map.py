# 영향 지도 저장 검사 — 빌드·신원·낡음 판정·증분 갱신·거부·훅을 임시 저장소에서 실제 프로세스로 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

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
        self.assertEqual(ensure_fresh(self.repo).action, "fresh")

    def test_commit_makes_map_stale(self):
        mapstore.build(self.repo, env={})
        git_commit(self.repo, {"src/new.py": "x = 1\n"})
        info, reason = mapstore.status(self.repo, env={})
        self.assertEqual(info.action, "stale")
        self.assertIn("behind HEAD", reason)
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
