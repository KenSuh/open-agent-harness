# CI 스타터 검사 — 테스트 명령 감지, 워크플로 템플릿의 고정 핀, 계획·쓰기의 동의 규칙, oah ci init 명령을 본다.
from __future__ import annotations

import importlib.resources
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import unittest

from oah import ci
from oah.cli import build_parser, command_modules
from tests.helpers import git_commit, make_git_repo, make_tmp_dir

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = str(REPO / "src")
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
        found = self.detect_one({"pyproject.toml": "[dependency-groups]\ndev = [\"pytestish\"]\n",
                                 "tests/test_math.py": "import unittest\n"})
        self.assertEqual(runs(found), ["python -m unittest discover -s tests"])

    def test_python_unittest_from_tests_folder(self):
        found = self.detect_one({"tests/test_math.py": "import unittest\n"})
        self.assertEqual(found.ecosystem, "python")
        self.assertEqual(runs(found), ["python -m unittest discover -s tests"])
        self.assertIn("tests", found.reason)

    def test_unittest_command_runs_failing_test_without_package(self):
        for filename, extra in (("tests/test_math.py", {}),
                                ("tests/test_math.py", {"tests/__init__.py": ""}),
                                ("test_math.py", {"pyproject.toml": ""})):
            with self.subTest(filename=filename, extra=sorted(extra)):
                root = make_project(self, {**extra, filename:
                                          "import unittest\n"
                                          "class MathTest(unittest.TestCase):\n"
                                          "    def test_math(self):\n"
                                          "        self.fail('discovery reached this test')\n"})
                self.assertEqual((root / "tests" / "__init__.py").exists(), "tests/__init__.py" in extra)
                command = runs(ci.detect(root)[0])[-1]
                result = subprocess.run([sys.executable, *shlex.split(command)[1:]], cwd=root,
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("Ran 1 test", result.stderr)
                self.assertIn("discovery reached this test", result.stderr)

    def test_python_without_discoverable_test_layout_uses_placeholder(self):
        for files in ({"pyproject.toml": "[project]\nname = 'x'\n"},
                      {"setup.cfg": "[metadata]\nname = x\n"},
                      {"tests/helpers.py": "import unittest\n"},
                      {"tests/__init__.py": ""},
                      {"tests/test-math.py": "import unittest\n"},
                      {"pyproject.toml": "", "test math.py": "import unittest\n"},
                      {"tests/sub/test_math.py": "import unittest\n"}):
            with self.subTest(files=sorted(files)):
                root = make_project(self, files)
                self.assertEqual(ci.detect(root), ())
                result = ci.plan(root, ["linux"])
                self.assertTrue(result.placeholder)
                self.assertIn("          exit 1\n", result.text)

    def test_tests_folder_without_python_files_is_not_python(self):
        self.assertEqual(ci.detect(make_project(self, {"tests/a.test.js": "", "tests/sub/b.txt": ""})), ())

    def test_python_installs_requirements_and_project(self):
        found = self.detect_one({"pyproject.toml": "[project]\nname = \"x\"\n", "requirements.txt": "requests\n",
                                 "tests/test_math.py": "import unittest\n"})
        self.assertEqual(runs(found), ["python -m pip install -r requirements.txt",
                                       "python -m pip install -e .", "python -m unittest discover -s tests"])

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
                                   "tests/test_math.py": "import unittest\n",
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
        self.assertEqual(ci.detect(make_project(self, {"pyproject.toml": b"[tool.pytest.ini_options\n\xff\xfe"})), ())
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

    def test_gitleaks_never_prints_context_or_writes_a_report(self):
        for name, text in self.renderings().items():
            with self.subTest(name=name):
                command = re.search(r"^\s*run: (gitleaks .+)$", text, re.MULTILINE).group(1)
                self.assertEqual(shlex.split(command),
                                 ["gitleaks", "detect", "--source", ".", "--redact", "--no-banner"])

    def test_gitleaks_adjacent_credentials_are_absent_from_output(self):
        gitleaks = shutil.which("gitleaks")
        if gitleaks is None:
            self.skipTest("install gitleaks 8.30.1 to run the redaction regression")
        version = subprocess.run([gitleaks, "version"], check=True, capture_output=True,
                                 text=True, encoding="utf-8", timeout=30).stdout.strip()
        if version != "8.30.1":
            self.skipTest("install gitleaks 8.30.1 to test the workflow's pinned version")
        repo = make_git_repo(self)
        text = ci.plan(repo, ["linux"]).text
        command = re.search(r"^\s*run: (gitleaks .+)$", text, re.MULTILINE).group(1)
        # These uppercase characters do not occur in gitleaks' summary logs. Checking
        # each character catches even a one-character leak of an adjacent value.
        alphabet = "CDEGHJLOQSTUVXYZ"
        values = []
        lines = []
        for length in (6, 12, 64):
            key = "".join(secrets.SystemRandom().sample(alphabet * 2, 32))
            key += "".join(secrets.choice(alphabet) for _ in range(4))
            password = "".join(secrets.choice(alphabet) for _ in range(length))
            values.extend((key, password))
            lines.append(f"api_key = 'ghp_{key}'; password = '{password}'\n")
        git_commit(repo, {"credentials.txt": "".join(lines)})
        before = tree(repo)
        env = {key: value for key, value in os.environ.items() if not key.startswith("GITLEAKS_")}
        result = subprocess.run([gitleaks, *shlex.split(command)[1:]], cwd=repo, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 1, "the scan must fail on the generated keys")
        output = result.stdout + result.stderr
        count = re.search(r"leaks found: (\d+)", output)
        self.assertIsNotNone(count, "the scan must report findings")
        self.assertGreaterEqual(int(count.group(1)), 3, "every generated key must be detected")
        for value in values:
            self.assertFalse(any(char in output for char in value), "credential characters reached the log")
        self.assertTrue(tree(repo) == before, "the scan must not create a report")

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

    def test_explicit_placeholder_keeps_detected_setup(self):
        result = ci.plan(make_project(self, FIXTURES["go"]), ["linux"], ci.PLACEHOLDER)
        self.assertTrue(result.placeholder)
        self.assertIsNone(result.detection)
        self.assertIn("actions/setup-go@", result.text)
        self.assertNotIn("go test", result.text)
        self.assertIn("could not find a test command", result.text)

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


def run_ci(cwd: pathlib.Path, *args: str, answers: str = "", env_extra: dict | None = None):
    """`python -m oah ci init ARGS` in cwd, with `answers` on stdin; returns (rc, stdout, stderr)."""
    env = dict(os.environ, PYTHONPATH=SRC)
    env.update(env_extra or {})
    result = subprocess.run([sys.executable, "-m", "oah", "ci", "init", *args], cwd=cwd,
                            input=answers.encode("utf-8"), capture_output=True, env=env, timeout=60)
    return result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8")


def tree(root: pathlib.Path) -> dict[str, bytes]:
    """Every file under root except .git, with its bytes."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.relative_to(root).parts}


class CliTest(unittest.TestCase):
    def repo(self, files: dict) -> pathlib.Path:
        repo = make_git_repo(self)
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return repo

    def target(self, repo: pathlib.Path) -> pathlib.Path:
        return repo / ".github" / "workflows" / "oah-ci.yml"

    def expected(self, repo: pathlib.Path, oses=("linux",), test_command=None) -> str:
        return ci.plan(repo, list(oses), test_command).text

    def test_ci_is_discovered(self):
        self.assertIn("oah.ci_cli", [module.__name__ for module in command_modules()])
        args = build_parser().parse_args(["ci", "init", "--os", "macos,linux", "--test-command", "make t",
                                          "--dry-run", "--yes", "--force"])
        self.assertEqual((args.command, args.ci_command, args.os, args.test_command, args.dry_run, args.yes,
                          args.force), ("ci", "init", "macos,linux", "make t", True, True, True))

    def test_dry_run_writes_nothing(self):
        repo = self.repo(FIXTURES["go"])
        before = tree(repo)
        for flags in (["--dry-run"], ["--dry-run", "--yes", "--force"]):
            with self.subTest(flags=flags):
                rc, out, err = run_ci(repo, "--os", "linux", *flags)
                self.assertEqual(rc, 0, err)
                self.assertIn(self.expected(repo), out)
                self.assertIn("dry run", out)
                self.assertEqual(tree(repo), before)
                self.assertFalse((repo / ".github").exists())
        self.target(repo).parent.mkdir(parents=True)
        self.target(repo).write_text("name: mine\n", encoding="utf-8")
        before = tree(repo)
        rc, out, err = run_ci(repo, "--os", "linux", "--dry-run", "--yes", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("-name: mine\n", out)
        self.assertEqual(tree(repo), before)

    def test_declined_writes_nothing(self):
        repo = self.repo(FIXTURES["go"])
        for answers in ("\nn\n", "\n\n", "\nmaybe\n", "n\n\nn\n"):
            with self.subTest(answers=answers):
                rc, out, err = run_ci(repo, "--os", "linux", answers=answers)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("OAH_CI_NOT_WRITTEN", err)
                self.assertFalse((repo / ".github").exists())

    def test_end_of_input_writes_nothing(self):
        repo = self.repo(FIXTURES["go"])
        for answers in ("", "\n", "n\n"):
            with self.subTest(answers=answers):
                rc, out, err = run_ci(repo, "--os", "linux", answers=answers)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("OAH_CI_NOT_WRITTEN", err)
                self.assertFalse((repo / ".github").exists())

    def test_confirmed_writes_file(self):
        repo = self.repo(FIXTURES["go"])
        rc, out, err = run_ci(repo, "--os", "linux", answers="\ny\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("go test ./...", out)
        self.assertIn(self.expected(repo), out)
        self.assertIn("OAH_CI_WRITTEN", out)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), self.expected(repo))

    def test_yes_writes_file(self):
        repo = self.repo(FIXTURES["go"])
        rc, out, err = run_ci(repo, "--os", "linux", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertIn(self.expected(repo), out)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), self.expected(repo))

    def test_runs_from_a_subfolder(self):
        repo = self.repo({**FIXTURES["go"], "pkg/a.go": "package pkg\n"})
        rc, out, err = run_ci(repo / "pkg", "--os", "linux", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.target(repo).is_file())
        self.assertFalse((repo / "pkg" / ".github").exists())

    def test_existing_file_shows_diff_and_asks(self):
        repo = self.repo({**FIXTURES["go"], ".github/workflows/oah-ci.yml": "name: mine\n"})
        rc, out, err = run_ci(repo, "--os", "linux", answers="\nn\n")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("-name: mine\n", out)
        self.assertIn("+name: oah-ci\n", out)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), "name: mine\n")
        rc, out, err = run_ci(repo, "--os", "linux", "--force", answers="\n")
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), "name: mine\n")
        rc, out, err = run_ci(repo, "--os", "linux", answers="\ny\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), self.expected(repo))

    def test_yes_without_force_refuses(self):
        repo = self.repo({**FIXTURES["go"], ".github/workflows/oah-ci.yml": "name: mine\n"})
        rc, out, err = run_ci(repo, "--os", "linux", "--yes")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("-name: mine\n", out)
        self.assertIn("OAH_CI_EXISTS", err)
        self.assertIn("--force --yes", err)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), "name: mine\n")

    def test_force_yes_overwrites(self):
        repo = self.repo({**FIXTURES["go"], ".github/workflows/oah-ci.yml": "name: mine\n"})
        rc, out, err = run_ci(repo, "--os", "linux", "--force", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertIn("-name: mine\n", out)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), self.expected(repo))

    def test_up_to_date_says_so(self):
        repo = self.repo(FIXTURES["go"])
        self.assertEqual(run_ci(repo, "--os", "linux", "--yes")[0], 0)
        before = self.target(repo).stat().st_mtime_ns
        rc, out, err = run_ci(repo, "--os", "linux", "--yes", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("already up to date", out)
        self.assertEqual(self.target(repo).stat().st_mtime_ns, before)

    def test_several_matches_user_picks(self):
        repo = self.repo({**FIXTURES["node"], **FIXTURES["go"]})
        rc, out, err = run_ci(repo, "--os", "linux", answers="2\ny\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("1. Node.js", out)
        self.assertIn("2. Go", out)
        text = self.target(repo).read_text(encoding="utf-8")
        self.assertIn("go test ./...", text)
        self.assertNotIn("npm test", text)
        for answers in ("9\n", "x\n", ""):
            with self.subTest(answers=answers):
                self.target(repo).unlink()
                rc, out, err = run_ci(repo, "--os", "linux", answers=answers)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("OAH_CI_NOT_WRITTEN", err)
                self.assertFalse(self.target(repo).exists())
                self.target(repo).write_text("", encoding="utf-8")

    def test_several_matches_with_yes_refuses(self):
        repo = self.repo({**FIXTURES["node"], **FIXTURES["go"]})
        rc, out, err = run_ci(repo, "--os", "linux", "--yes")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("OAH_CI_AMBIGUOUS", err)
        self.assertIn("--test-command", err)
        self.assertFalse((repo / ".github").exists())
        rc, out, err = run_ci(repo, "--os", "linux", "--yes", "--test-command", "npm test")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"),
                         self.expected(repo, test_command="npm test"))

    def test_several_matches_dry_run_shows_the_first(self):
        repo = self.repo({**FIXTURES["node"], **FIXTURES["go"]})
        rc, out, err = run_ci(repo, "--os", "linux", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn(self.expected(repo, test_command=ci.detect(repo)[0]), out)
        self.assertIn("asks you to pick", out)

    def test_detected_command_declined_then_typed(self):
        repo = self.repo(FIXTURES["go"])
        rc, out, err = run_ci(repo, "--os", "linux", answers="n\nmake check\ny\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"),
                         self.expected(repo, test_command="make check"))
        self.target(repo).unlink()
        rc, out, err = run_ci(repo, "--os", "linux", answers="n\n\ny\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"),
                         self.expected(repo, test_command=ci.PLACEHOLDER))
        self.assertNotIn("go test", self.target(repo).read_text(encoding="utf-8"))

    def test_no_detection_offers_typing_or_placeholder(self):
        repo = self.repo({"README.md": "hi\n"})
        rc, out, err = run_ci(repo, "--os", "linux", answers="\ny\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("placeholder", out)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), self.expected(repo))
        self.target(repo).unlink()
        rc, out, err = run_ci(repo, "--os", "linux", answers="make t\ny\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), self.expected(repo, test_command="make t"))
        self.target(repo).unlink()
        rc, out, err = run_ci(repo, "--os", "linux", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertIn("edit", out.lower())
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"), self.expected(repo))

    def test_test_command_flag(self):
        repo = self.repo(FIXTURES["go"])
        rc, out, err = run_ci(repo, "--os", "linux", "--test-command", "make check", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.target(repo).read_text(encoding="utf-8"),
                         self.expected(repo, test_command="make check"))
        for bad in ("", "echo ${{ github.ref }}"):
            with self.subTest(bad=bad):
                rc, out, err = run_ci(repo, "--os", "linux", "--test-command", bad, "--yes", "--force")
                self.assertEqual(rc, 2, out + err)
                self.assertIn("OAH_CI_TEST_COMMAND_INVALID", err)

    def test_config_section_is_used(self):
        repo = self.repo({**FIXTURES["go"], ".oah/config.toml":
                          "[ci]\nos = [\"windows\", \"linux\"]\ntest_command = \"make check\"\n"})
        rc, out, err = run_ci(repo, "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn(self.expected(repo, ("windows", "linux"), "make check"), out)
        rc, out, err = run_ci(repo, "--dry-run", "--os", "macos", "--test-command", "go vet ./...")
        self.assertEqual(rc, 0, err)
        self.assertIn(self.expected(repo, ("macos",), "go vet ./..."), out)

    def test_config_errors_exit_69(self):
        for body, code in (("[ci]\nfoo = 1\n", "OAH_CONFIG_UNKNOWN_KEY"),
                           ("[ci]\nos = \"linux\"\n", "OAH_CONFIG_INVALID"),
                           ("[ci]\nos = [\"beos\"]\n", "OAH_CONFIG_INVALID"),
                           ("[ci]\nos = [1]\n", "OAH_CONFIG_INVALID"),
                           ("[ci]\ntest_command = \"a\\nb\"\n", "OAH_CONFIG_INVALID")):
            with self.subTest(body=body):
                repo = self.repo({**FIXTURES["go"], ".oah/config.toml": body})
                rc, out, err = run_ci(repo, "--yes")
                self.assertEqual(rc, 69, out + err)
                self.assertIn(code, err)
                self.assertFalse((repo / ".github").exists())

    def test_not_a_repository_exits_69(self):
        folder = make_tmp_dir(self) / "plain"
        folder.mkdir()
        rc, out, err = run_ci(folder, "--yes", env_extra={"GIT_CEILING_DIRECTORIES": str(folder.parent)})
        self.assertEqual(rc, 69, out + err)
        self.assertIn("OAH_NOT_A_REPO", err)
        self.assertEqual(list(folder.iterdir()), [])

    def test_bad_os_exits_2(self):
        repo = self.repo(FIXTURES["go"])
        for value in ("macos,beos", "", ","):
            with self.subTest(value=value):
                rc, out, err = run_ci(repo, "--os", value, "--yes")
                self.assertEqual(rc, 2, out + err)
                self.assertIn("OAH_CI_OS_INVALID", err)
                self.assertFalse((repo / ".github").exists())

    def test_target_problems_exit_69(self):
        repo = self.repo({**FIXTURES["go"], ".github/workflows/oah-ci.yml/inside": ""})
        rc, out, err = run_ci(repo, "--yes", "--force")
        self.assertEqual(rc, 69, out + err)
        self.assertIn("OAH_CI_TARGET_INVALID", err)

    def test_default_runner_is_current_os(self):
        repo = self.repo(FIXTURES["go"])
        rc, out, err = run_ci(repo, "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"os: [{ci.RUNNERS[ci.current_os()]}]", out)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_default_runner(self):
        self.assertEqual(ci.current_os(), "windows")
        repo = self.repo(FIXTURES["go"])
        rc, out, err = run_ci(repo, "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("os: [windows-latest]", out)


if __name__ == "__main__":
    unittest.main()
