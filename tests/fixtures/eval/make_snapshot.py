# 평가 픽스처 스냅샷 생성기 — 고정 신원·고정 날짜로 세 커밋짜리 합성 저장소를 만들고 git bundle 로 쓴다.
"""Rebuild the synthetic `derived-index` case bundle.

The tiny repository keeps `index.txt` generated from `words.txt` by `build_index.py`. Its three commits:
  initial   words and index agree
  snapshot  two words are added, but index.txt is not regenerated (the defect; the case starts here)
  fix       index.txt is regenerated (a later object that must never reach an agent)

Usage: python make_snapshot.py OUT.bundle   (prints the three commit ids as JSON)
Fixed identity and dates make the commit ids the same on every machine.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

IDENTITY = {"GIT_AUTHOR_NAME": "Fixture Author", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture Author", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}

BUILD = '''import pathlib

words = sorted({line.strip() for line in pathlib.Path("words.txt").read_text(encoding="utf-8").splitlines()
                if line.strip()})
pathlib.Path("index.txt").write_text(f"count={len(words)}\\n" + "".join(word + "\\n" for word in words),
                                     encoding="utf-8", newline="\\n")
'''

CHECK = '''import pathlib
import sys

words = sorted({line.strip() for line in pathlib.Path("words.txt").read_text(encoding="utf-8").splitlines()
                if line.strip()})
expected = f"count={len(words)}\\n" + "".join(word + "\\n" for word in words)
actual = pathlib.Path("index.txt").read_text(encoding="utf-8").replace("\\r\\n", "\\n")
if actual != expected:
    print("index.txt is stale; run build_index.py")
    sys.exit(1)
print("index.txt is up to date")
'''

README = """# Word index

`index.txt` is generated from `words.txt` by `python build_index.py`. Regenerate it after every change
to `words.txt`; `python check_index.py` fails when it is stale.
"""

INITIAL = {"README.md": README, "build_index.py": BUILD, "check_index.py": CHECK,
           "words.txt": "banana\napple\ncherry\n", "index.txt": "count=3\napple\nbanana\ncherry\n"}
SNAPSHOT = {"words.txt": "banana\napple\ncherry\nkiwi\nfig\n"}
FIX = {"index.txt": "count=5\napple\nbanana\ncherry\nfig\nkiwi\n"}


def _git(repo: pathlib.Path, *args: str, env: dict, stdin: str | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True,
                            encoding="utf-8", env=env, check=True)
    return result.stdout.strip()


def build(out: pathlib.Path) -> dict:
    """Write the bundle to `out` and return {"initial", "snapshot", "fix"} commit ids."""
    work = pathlib.Path(tempfile.mkdtemp(prefix="oah-eval-fixture-"))
    try:
        repo = work / "repo"
        env = dict(os.environ, **IDENTITY)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
        _git(repo, "config", "core.autocrlf", "false", env=env)
        commits: dict[str, str] = {}
        parent = None
        files: dict[str, str] = {}
        for day, (name, change) in enumerate((("initial", INITIAL), ("snapshot", SNAPSHOT), ("fix", FIX)), 1):
            files.update(change)
            for path, text in files.items():
                (repo / path).write_bytes(text.encode("utf-8"))
            _git(repo, "add", "-A", env=env)
            tree = _git(repo, "write-tree", env=env)
            stamp = f"2026-01-0{day}T00:00:00+00:00"
            dated = dict(env, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
            args = ["commit-tree", tree, "-m", name] + (["-p", parent] if parent else [])
            parent = _git(repo, *args, env=dated)
            commits[name] = parent
        _git(repo, "update-ref", "refs/heads/main", parent, env=env)
        out = pathlib.Path(out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "bundle", "create", str(out), "main", env=env)
        return commits
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python make_snapshot.py OUT.bundle")
    print(json.dumps(build(pathlib.Path(sys.argv[1])), indent=2))
