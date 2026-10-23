# 패키징 검사 — 만든 wheel 에 src/oah/templates/ 아래 파일(하위 폴더 포함)이 실리는지 본다.
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import unittest
import zipfile

from tests.helpers import make_tmp_dir

REPO = pathlib.Path(__file__).resolve().parent.parent


def _local_setuptools_is_new_enough() -> bool:
    if importlib.util.find_spec("setuptools") is None:
        return False
    import setuptools

    return int(setuptools.__version__.split(".")[0]) >= 77


class PackagingTest(unittest.TestCase):
    def test_wheel_ships_templates(self):
        work = make_tmp_dir(self)
        source = work / "source"
        shutil.copytree(REPO / "src", source / "src",
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copyfile(REPO / name, source / name)
        sample = source / "src" / "oah" / "templates" / "sample" / "nested" / "x.txt"
        sample.parent.mkdir(parents=True)
        sample.write_text("sample\n", encoding="utf-8")

        command = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", str(work / "dist")]
        if _local_setuptools_is_new_enough():
            command.append("--no-build-isolation")
        result = subprocess.run([*command, str(source)], capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

        wheels = list((work / "dist").glob("*.whl"))
        self.assertEqual(len(wheels), 1, wheels)
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = set(wheel.namelist())
            self.assertIn("oah/templates/README.md", names)
            self.assertIn("oah/templates/sample/nested/x.txt", names)
            self.assertEqual(wheel.read("oah/templates/README.md"),
                             (REPO / "src" / "oah" / "templates" / "README.md").read_bytes())


if __name__ == "__main__":
    unittest.main()
