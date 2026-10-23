# CI 스타터 검사 — 테스트 명령 감지, 워크플로 템플릿의 고정 핀, 계획·쓰기의 동의 규칙, oah ci init 명령을 본다.
from __future__ import annotations

import importlib.resources
import json
import os
import pathlib
import re
import shutil
import subprocess
import unittest

from oah import ci
from tests.helpers import make_tmp_dir

REPO = pathlib.Path(__file__).resolve().parent.parent
# Read with `git ls-remote` against each action's official repository (see the lane plan).
EXPECTED_PINS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-python": ("5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
    "actions/setup-node": ("949feb2413d6458794dcd2491c4babbbce0c15c1", "v7.1.0"),
    "actions/setup-go": ("b7ad1dad31e06c5925ef5d2fc7ad053ef454303e", "v7.0.0"),
}
FIXTURES = {
    "python": {"pyproject.toml": "[tool.pytest.ini_options]\n"},
    "node": {"package.json": json.dumps({"scripts": {"test": "jest"}})},
    "go": {"go.mod": "module x\n"},
    "rust": {"Cargo.toml": "[package]\nname = \"x\"\n"},
    "none": {},
}


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


def template_files() -> dict[str, str]:
    folder = importlib.resources.files("oah").joinpath("templates/ci")
    return {item.name: item.read_text(encoding="utf-8") for item in folder.iterdir() if item.is_file()}


def assert_ci_error(test: unittest.TestCase, code: str, call, *args, **kwargs) -> ci.CiError:
    with test.assertRaises(ci.CiError) as ctx:
        call(*args, **kwargs)
    test.assertEqual(ctx.exception.code, code, str(ctx.exception))
    test.assertTrue(str(ctx.exception).startswith(code + ": "), str(ctx.exception))
    return ctx.exception


class RenderTest(unittest.TestCase):
    def renderings(self) -> dict[str, str]:
        texts = {name: ci.plan(make_project(self, files), ["macos", "windows", "linux"]).text
                 for name, files in FIXTURES.items()}
        texts["typed"] = ci.plan(make_project(self, {}), ["linux"], "make check").text
        return texts

    def test_every_uses_is_pinned_to_a_full_sha(self):
        pinned = re.compile(r"^\s*(?:- )?uses: ([\w.-]+/[\w.-]+)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)$")
        sources = {f"template {name}": text for name, text in template_files().items()}
        sources.update({f"rendered {name}": text for name, text in self.renderings().items()})
        seen = set()
        for source, text in sources.items():
            for line in text.split("\n"):
                if "uses:" not in line:
                    continue
                with self.subTest(source=source, line=line):
                    match = pinned.match(line)
                    self.assertIsNotNone(match, "every uses: must be owner/repo@<40-hex sha> # vX.Y.Z")
                    action, sha, version = match.groups()
                    self.assertEqual(EXPECTED_PINS.get(action), (sha, version))
                    seen.add(action)
        self.assertEqual(seen, set(EXPECTED_PINS))

    def test_gitleaks_pin_matches_repository_secret_scan(self):
        def pins(text: str) -> tuple[str, str]:
            version = re.search(r'GITLEAKS_VERSION: "([^"]+)"', text)
            digest = re.search(r'GITLEAKS_SHA256: "([^"]+)"', text)
            return version.group(1), digest.group(1)

        reference = pins((REPO / ".github" / "workflows" / "secrets.yml").read_text(encoding="utf-8"))
        self.assertRegex(reference[0], r"^\d+\.\d+\.\d+$")
        self.assertRegex(reference[1], r"^[0-9a-f]{64}$")
        for name, text in self.renderings().items():
            with self.subTest(name=name):
                self.assertEqual(pins(text), reference)
                job = text[text.index("\n  secret-scan:\n"):]
                self.assertIn("runs-on: ubuntu-latest", job)
                self.assertIn("fetch-depth: 0", job)
                check = job.index('sha256sum -c -')
                self.assertLess(check, job.index("tar -xzf"))
                self.assertLess(job.index("curl -fsSL"), check)
                self.assertIn("gitleaks detect --source . --redact", job)

    def test_triggers_permissions_and_jobs(self):
        for name, text in self.renderings().items():
            with self.subTest(name=name):
                self.assertIn("\non:\n  push:\n  pull_request:\n", text)
                self.assertIn("\npermissions:\n  contents: read\n", text)
                self.assertIn("\njobs:\n  test:\n", text)
                self.assertIn("\n  secret-scan:\n", text)
                self.assertIn("        shell: bash\n", text)
                self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))

    def test_runners_follow_chosen_oses(self):
        root = make_project(self, FIXTURES["go"])
        self.assertIn("os: [ubuntu-latest, macos-latest]", ci.plan(root, ["linux", "macos"]).text)
        self.assertIn("os: [windows-latest]", ci.plan(root, ["windows"]).text)
        default = ci.plan(root, None)
        self.assertEqual(default.oses, (ci.current_os(),))
        self.assertIn(f"os: [{ci.RUNNERS[ci.current_os()]}]", default.text)
        self.assertEqual(ci.plan(root, []).oses, (ci.current_os(),))

    def test_current_os_mapping(self):
        self.assertEqual(ci.current_os("darwin"), "macos")
        self.assertEqual(ci.current_os("win32"), "windows")
        self.assertEqual(ci.current_os("linux"), "linux")
        self.assertEqual(ci.RUNNERS, {"macos": "macos-latest", "windows": "windows-latest",
                                      "linux": "ubuntu-latest"})

    def test_parse_oses(self):
        self.assertEqual(ci.parse_oses([" MacOS", "linux", "macos"]), ("macos", "linux"))
        for bad in ([], ["ubuntu"], [""], ["macos", "win"]):
            with self.subTest(bad=bad):
                assert_ci_error(self, "OAH_CI_OS_INVALID", ci.parse_oses, bad)
        assert_ci_error(self, "OAH_CI_OS_INVALID", ci.plan, make_project(self, {}), ["beos"])

    def test_no_secret_or_token_in_workflow(self):
        for name, text in self.renderings().items():
            with self.subTest(name=name):
                lowered = text.lower()
                for word in ("secrets.", "token", "password", "github_token", "api_key"):
                    self.assertNotIn(word, lowered)
                self.assertEqual(text.count("persist-credentials: false"), 2)
                self.assertEqual(lowered.count("permissions:"), 1)

    def test_placeholder_step_fails_with_message(self):
        result = ci.plan(make_project(self, {}), ["linux"])
        self.assertTrue(result.placeholder)
        self.assertEqual(result.steps, ())
        self.assertIsNone(result.detection)
        self.assertIn("could not find a test command", result.text)
        self.assertIn("Edit .github/workflows/oah-ci.yml", result.text)
        self.assertIn("          exit 1\n", result.text)

    def test_typed_command_is_literal(self):
        command = 'pytest -k "a or b" --tb=short # note: {x} & y | z'
        result = ci.plan(make_project(self, {}), ["linux"], f"  {command}  ")
        self.assertFalse(result.placeholder)
        self.assertEqual(result.steps, (ci.Step("Run tests", command),))
        self.assertIn(f"      - name: Run tests\n        run: |\n          {command}\n", result.text)

    def test_typed_command_keeps_detected_setup(self):
        root = make_project(self, {**FIXTURES["python"], **FIXTURES["node"]})
        text = ci.plan(root, ["linux"], "make test").text
        self.assertIn("actions/setup-python@", text)
        self.assertIn("actions/setup-node@", text)
        self.assertEqual(text.count("- name: Run tests"), 1)
        self.assertNotIn("npm ci", text)

    def test_detected_steps_and_setup(self):
        expected = {"python": "actions/setup-python@", "node": "actions/setup-node@", "go": "actions/setup-go@"}
        for name in ("python", "node", "go", "rust"):
            with self.subTest(name=name):
                result = ci.plan(make_project(self, FIXTURES[name]), ["linux"])
                self.assertEqual(result.detection.ecosystem, name)
                for step in result.detection.steps:
                    self.assertIn(f"      - name: {step.name}\n        run: |\n          {step.run}\n", result.text)
                for ecosystem, action in expected.items():
                    self.assertEqual(action in result.text, ecosystem == name)

    def test_test_command_rejects_line_breaks_and_expressions(self):
        root = make_project(self, {})
        for bad in ("", "   ", "a\nb", "a\rb", "a\x00b", "a\tb\x1b", "echo ${{ github.ref }}", "x\u2028y"):
            with self.subTest(bad=bad):
                assert_ci_error(self, "OAH_CI_TEST_COMMAND_INVALID", ci.plan, root, ["linux"], bad)

    def test_actionlint_accepts_every_rendering(self):
        actionlint = shutil.which("actionlint")
        if actionlint is None:
            self.skipTest("actionlint is not installed")
        folder = make_tmp_dir(self) / "lint" / ".github" / "workflows"
        folder.mkdir(parents=True)
        for name, text in self.renderings().items():
            (folder / f"{name}.yml").write_text(text, encoding="utf-8")
        result = subprocess.run([actionlint, *sorted(str(path) for path in folder.iterdir())],
                                capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_marker_left(self):
        for name, text in self.renderings().items():
            with self.subTest(name=name):
                self.assertNotIn("@@", text)


class PlanWriteTest(unittest.TestCase):
    def target(self, root: pathlib.Path) -> pathlib.Path:
        return root / ".github" / "workflows" / "oah-ci.yml"

    def test_plan_ambiguous_raises(self):
        root = make_project(self, {**FIXTURES["node"], **FIXTURES["go"]})
        error = assert_ci_error(self, "OAH_CI_AMBIGUOUS", ci.plan, root, ["linux"])
        self.assertIn("Node.js", str(error))
        self.assertIn("Go", str(error))

    def test_plan_with_picked_detection(self):
        root = make_project(self, {**FIXTURES["node"], **FIXTURES["go"]})
        picked = ci.detect(root)[1]
        result = ci.plan(root, ["linux"], picked)
        self.assertEqual(result.detection, picked)
        self.assertIn("go test ./...", result.text)
        self.assertNotIn("npm test", result.text)

    def test_write_requires_confirm(self):
        root = make_project(self, FIXTURES["go"])
        result = ci.plan(root, ["linux"])
        assert_ci_error(self, "OAH_CI_NOT_CONFIRMED", ci.write, result, confirm=False)
        self.assertFalse((root / ".github").exists())

    def test_write_creates_folders(self):
        root = make_project(self, FIXTURES["go"])
        result = ci.plan(root, ["linux"])
        self.assertEqual(result.existing, None)
        self.assertEqual(result.path, self.target(root))
        self.assertEqual(ci.write(result, confirm=True), self.target(root))
        self.assertEqual(self.target(root).read_bytes(), result.text.encode("utf-8"))

    def test_write_refuses_existing_without_overwrite(self):
        root = make_project(self, {**FIXTURES["go"], ".github/workflows/oah-ci.yml": "name: mine\n"})
        result = ci.plan(root, ["linux"])
        self.assertEqual(result.existing, "name: mine\n")
        assert_ci_error(self, "OAH_CI_EXISTS", ci.write, result, confirm=True)
        self.assertEqual(self.target(root).read_text(encoding="utf-8"), "name: mine\n")
        ci.write(result, confirm=True, overwrite=True)
        self.assertEqual(self.target(root).read_text(encoding="utf-8"), result.text)

    def test_write_refuses_file_changed_since_plan(self):
        root = make_project(self, FIXTURES["go"])
        result = ci.plan(root, ["linux"])
        self.target(root).parent.mkdir(parents=True)
        self.target(root).write_text("name: appeared\n", encoding="utf-8")
        assert_ci_error(self, "OAH_CI_CHANGED", ci.write, result, confirm=True, overwrite=True)
        self.assertEqual(self.target(root).read_text(encoding="utf-8"), "name: appeared\n")

        result = ci.plan(root, ["linux"])
        self.target(root).write_text("name: edited\n", encoding="utf-8")
        assert_ci_error(self, "OAH_CI_CHANGED", ci.write, result, confirm=True, overwrite=True)
        self.assertEqual(self.target(root).read_text(encoding="utf-8"), "name: edited\n")

    def test_write_keeps_crlf(self):
        root = make_project(self, {**FIXTURES["go"], ".github/workflows/oah-ci.yml": b"name: mine\r\n"})
        result = ci.plan(root, ["linux"])
        ci.write(result, confirm=True, overwrite=True)
        self.assertEqual(self.target(root).read_bytes(), result.text.replace("\n", "\r\n").encode("utf-8"))
        again = ci.plan(root, ["linux"])
        self.assertTrue(again.up_to_date)
        self.assertEqual(again.diff, "")

    def test_diff_and_up_to_date(self):
        root = make_project(self, FIXTURES["go"])
        fresh = ci.plan(root, ["linux"])
        self.assertEqual((fresh.diff, fresh.up_to_date), ("", False))
        ci.write(fresh, confirm=True)
        self.assertTrue(ci.plan(root, ["linux"]).up_to_date)
        changed = ci.plan(root, ["linux", "windows"])
        self.assertFalse(changed.up_to_date)
        self.assertIn("-        os: [ubuntu-latest]\n", changed.diff)
        self.assertIn("+        os: [ubuntu-latest, windows-latest]\n", changed.diff)
        self.assertIn(".github/workflows/oah-ci.yml", changed.diff.split("\n")[0])

    def test_invalid_target_refused(self):
        root = make_project(self, {".github/workflows/oah-ci.yml": b"name: \xff\n"})
        assert_ci_error(self, "OAH_CI_TARGET_INVALID", ci.plan, root, ["linux"])
        root = make_project(self, {".github/workflows/oah-ci.yml/inside": ""})
        assert_ci_error(self, "OAH_CI_TARGET_INVALID", ci.plan, root, ["linux"])

    def test_symlink_target_refused(self):
        root = make_project(self, FIXTURES["go"])
        outside = make_tmp_dir(self) / "outside"
        outside.mkdir()
        (root / ".github").mkdir()
        try:
            (root / ".github" / "workflows").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"cannot create a symlink here: {exc}")
        assert_ci_error(self, "OAH_CI_TARGET_INVALID", ci.plan, root, ["linux"])
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
