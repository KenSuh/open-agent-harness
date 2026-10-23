# 저장소 ignore 규칙 검사 — 로컬 상태·비공개 파일이 실수로 커밋되지 않는지 git 에게 직접 묻는다.
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = [
    ".tmp/x",
    ".oah-dev/x",
    ".unlazy/x",
    ".unlazy-hook-state.json",
    ".claude/settings.local.json",
    "src/oah/__pycache__/x.pyc",
    "x.egg-info/PKG-INFO",
    "dist/x.whl",
    "build/x",
    ".venv/x",
]


class GitignoreTest(unittest.TestCase):
    def test_local_state_paths_are_ignored(self):
        # Only the tracked .gitignore counts: copy it into a scratch repo and neutralize global excludes.
        with tempfile.TemporaryDirectory() as tmp:
            scratch = pathlib.Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(scratch)], check=True)
            shutil.copyfile(REPO / ".gitignore", scratch / ".gitignore")
            no_global = pathlib.Path(tmp) / "empty-excludes"
            no_global.write_text("", encoding="utf-8")

            def ignored(path: str) -> bool:
                result = subprocess.run(
                    ["git", "-C", str(scratch), "-c", f"core.excludesFile={no_global}",
                     "check-ignore", "--no-index", "-q", path],
                )
                return result.returncode == 0

            for path in MUST_BE_IGNORED:
                with self.subTest(path=path):
                    self.assertTrue(ignored(path), f"{path} is not ignored by .gitignore")
            self.assertFalse(ignored("src/oah/cli.py"), "source files must not be ignored")


if __name__ == "__main__":
    unittest.main()
