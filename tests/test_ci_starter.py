# CI 스타터 검사 — 테스트 명령 감지, 워크플로 템플릿의 고정 핀, 계획·쓰기의 동의 규칙, oah ci init 명령을 본다.
from __future__ import annotations

import json
import pathlib
import unittest

from oah import ci
from tests.helpers import make_tmp_dir


def make_project(test: unittest.TestCase, files: dict) -> pathlib.Path:
    """A temp folder holding `files` (str contents; bytes written as given)."""
    root = make_tmp_dir(test) / "project"
    root.mkdir()
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def runs(detection: ci.Detection) -> list[str]:
    return [step.run for step in detection.steps]


class DetectTest(unittest.TestCase):
    def detect_one(self, files: dict) -> ci.Detection:
        found = ci.detect(make_project(self, files))
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_python_pytest_from_pyproject(self):
        found = self.detect_one({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = \"-q\"\n"})
        self.assertEqual(found.ecosystem, "python")
        self.assertEqual(runs(found), ["python -m pip install pytest", "python -m pytest"])
        self.assertIn("pyproject.toml", found.reason)

    def test_python_pytest_from_setup_cfg(self):
        found = self.detect_one({"setup.cfg": "[metadata]\nname = x\n\n[tool:pytest]\ntestpaths = tests\n"})
        self.assertEqual(runs(found), ["python -m pip install pytest", "python -m pytest"])
        self.assertIn("setup.cfg", found.reason)

    def test_python_pytest_from_dependency(self):
        for files in (
            {"pyproject.toml": "[project.optional-dependencies]\ndev = [\"pytest>=8\", \"ruff\"]\n"},
            {"pyproject.toml": "[dependency-groups]\ntest = [\"pytest\"]\n"},
            {"tests/test_a.py": "", "requirements-dev.txt": "ruff\nPyTest==8.0  # tests\n"},
            {"tests/test_a.py": "", "tests/conftest.py": ""},
            {"tests/test_a.py": "", "pytest.ini": "[pytest]\n"},
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(runs(self.detect_one(files))[-1], "python -m pytest")

    def test_pytest_plugin_counts_but_lookalike_does_not(self):
        found = self.detect_one({"pyproject.toml": "[project.optional-dependencies]\ndev = [\"pytest-cov\"]\n"})
        self.assertEqual(runs(found)[-1], "python -m pytest")
        found = self.detect_one({"pyproject.toml": "[dependency-groups]\ndev = [\"pytestish\"]\n"})
        self.assertEqual(runs(found), ["python -m unittest discover"])

    def test_python_unittest_from_tests_folder(self):
        found = self.detect_one({"tests/test_math.py": "import unittest\n"})
        self.assertEqual(found.ecosystem, "python")
        self.assertEqual(runs(found), ["python -m unittest discover"])
        self.assertIn("tests", found.reason)

    def test_tests_folder_without_python_files_is_not_python(self):
        self.assertEqual(ci.detect(make_project(self, {"tests/a.test.js": "", "tests/sub/b.txt": ""})), ())

    def test_python_installs_requirements_and_project(self):
        found = self.detect_one({"pyproject.toml": "[project]\nname = \"x\"\n", "requirements.txt": "requests\n"})
        self.assertEqual(runs(found), ["python -m pip install -r requirements.txt",
                                       "python -m pip install -e .", "python -m unittest discover"])

    def test_node_test_script(self):
        found = self.detect_one({"package.json": json.dumps({"scripts": {"test": "node --test"}})})
        self.assertEqual(found.ecosystem, "node")
        self.assertEqual(runs(found), ["npm ci", "npm test"])

    def test_node_without_test_script_is_not_detected(self):
        for data in ({}, {"scripts": {"build": "tsc"}}, {"scripts": {"test": "  "}}, {"scripts": ["test"]}, []):
            with self.subTest(data=data):
                self.assertEqual(ci.detect(make_project(self, {"package.json": json.dumps(data)})), ())

    def test_go(self):
        found = self.detect_one({"go.mod": "module example.com/x\n\ngo 1.23\n"})
        self.assertEqual((found.ecosystem, runs(found)), ("go", ["go test ./..."]))

    def test_rust(self):
        found = self.detect_one({"Cargo.toml": "[package]\nname = \"x\"\n"})
        self.assertEqual((found.ecosystem, runs(found)), ("rust", ["cargo test"]))

    def test_several_in_order(self):
        root = make_project(self, {"Cargo.toml": "", "go.mod": "module x\n", "setup.cfg": "",
                                   "package.json": json.dumps({"scripts": {"test": "jest"}})})
        self.assertEqual([found.ecosystem for found in ci.detect(root)], ["python", "node", "go", "rust"])

    def test_nothing_detected(self):
        self.assertEqual(ci.detect(make_project(self, {"README.md": "hello\n", "src/main.c": ""})), ())

    def test_crlf_and_bom_files(self):
        found = self.detect_one({"pyproject.toml": b"\xef\xbb\xbf[tool.pytest.ini_options]\r\naddopts = \"-q\"\r\n"})
        self.assertEqual(runs(found)[-1], "python -m pytest")
        found = self.detect_one({"setup.cfg": b"[tool:pytest]\r\ntestpaths = tests\r\n"})
        self.assertEqual(runs(found)[-1], "python -m pytest")
        found = self.detect_one({"package.json": b"\xef\xbb\xbf{\r\n \"scripts\": {\"test\": \"jest\"}\r\n}\r\n"})
        self.assertEqual(found.ecosystem, "node")

    def test_broken_files_do_not_raise(self):
        found = self.detect_one({"pyproject.toml": b"[tool.pytest.ini_options\n\xff\xfe"})
        self.assertEqual(runs(found), ["python -m unittest discover"])
        self.assertEqual(ci.detect(make_project(self, {"package.json": b"{not json\xff"})), ())


if __name__ == "__main__":
    unittest.main()
