# 푸시 전 개인정보 검사 스크립트 검사 — 임시 git 저장소에서 규칙별 적중·허용 목록·이력·커밋 신원·gitleaks 요구를 본다.
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys
import unittest

from tests.helpers import make_tmp_dir

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNER_PATH = ROOT / "tools" / "privacy_scan.py"

# Secret-shaped test values are assembled at runtime so this file never matches the rules itself.
HOME = "/" + "Us" + "ers/alice/project"
WIN_HOME = "C:" + "\\" + "Us" + "ers" + "\\" + "bob" + "\\" + "project"
OPENAI_LIKE = "s" + "k-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
GITHUB_LIKE = "gh" + "p_" + "a" * 36
AWS_LIKE = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
PERSONAL_EMAIL = "alice" + "@" + "corp.dev"
NOREPLY = "1234+someone" + "@" + "users.noreply.github.com"
ENV_ASSIGNMENT = "API_" + "K" + "EY=" + "abcd1234efgh5678"


def load_scanner():
    spec = importlib.util.spec_from_file_location("privacy_scan", SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while the class body runs
    spec.loader.exec_module(module)
    return module


def git(repo: pathlib.Path, *args: str, email: str = NOREPLY) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="Someone", GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME="Someone", GIT_COMMITTER_EMAIL=email)
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args], check=True,
                   capture_output=True, env=env)


def make_repo(test: unittest.TestCase) -> pathlib.Path:
    repo = make_tmp_dir(test) / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    return repo


def commit(repo: pathlib.Path, files: dict[str, str | None], message: str = "change",
           email: str = NOREPLY) -> None:
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, email=email)


class PrivacyScanTest(unittest.TestCase):
    def setUp(self):
        self.scanner = load_scanner()
        self.no_terms = make_tmp_dir(self) / "no-terms.txt"
        self.no_terms.write_text("", encoding="utf-8")

    def run_scan(self, repo: pathlib.Path, *extra: str, terms: pathlib.Path | None = None, which=None):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--repo", str(repo), "--gitleaks", "off", "--terms", str(terms or self.no_terms), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.scanner.main(argv, which=which or shutil.which)
        return code, out.getvalue(), err.getvalue()

    def assert_hit(self, output: str, rule: str):
        self.assertIn(f"PRIVACY_HIT {rule} ", output)

    def test_clean_repo(self):
        repo = make_repo(self)
        commit(repo, {"README.md": "hello\n"})
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 0, out)
        self.assertTrue(out.splitlines()[-1].startswith("PRIVACY_SCAN_CLEAN files=1 commits=1 gitleaks=skipped"))

    def test_home_path_hit_and_placeholder_allowed(self):
        repo = make_repo(self)
        commit(repo, {"a.md": f"see {HOME}\n", "b.md": "use /" + "Us" + "ers/<name>/project\n"})
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 1)
        self.assert_hit(out, "home-path")
        self.assertIn("a.md:1", out)
        self.assertNotIn("b.md", out)

    def test_windows_home_path_hit(self):
        repo = make_repo(self)
        commit(repo, {"a.md": f"path {WIN_HOME}\n"})
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 1)
        self.assert_hit(out, "home-path")

    def test_email_hit_and_noreply_allowed(self):
        repo = make_repo(self)
        commit(repo, {"a.md": f"mail {PERSONAL_EMAIL}\n", "b.md": f"{NOREPLY} dev" + "@example.com\n"})
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 1)
        self.assert_hit(out, "email")
        self.assertNotIn("b.md", out)

    def test_key_shapes_hit_and_identifier_not_hit(self):
        repo = make_repo(self)
        commit(repo, {"keys.txt": f"{OPENAI_LIKE}\n{GITHUB_LIKE}\n{AWS_LIKE}\n", "kinds.md": "high-risk-review\n"})
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 1)
        self.assertEqual(out.count("PRIVACY_HIT key keys.txt:"), 3, out)
        self.assertNotIn("kinds.md", out)

    def test_env_secret_assignment_hit(self):
        repo = make_repo(self)
        commit(repo, {"env.txt": f"{ENV_ASSIGNMENT}\nMAX_TOKEN = 1024\n"})
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 1)
        self.assertEqual(out.count("PRIVACY_HIT env-secret"), 1, out)

    def test_terms_word_and_substr(self):
        repo = make_repo(self)
        commit(repo, {"a.md": "Acme corp\n", "b.md": "acmeville\n", "c.md": "superzorkish\n"})
        terms = make_tmp_dir(self) / "terms.txt"
        terms.write_text("# local terms\nword acme\nsubstr zork\n", encoding="utf-8")
        code, out, _ = self.run_scan(repo, terms=terms)
        self.assertEqual(code, 1)
        self.assertIn("a.md:1", out)
        self.assertNotIn("b.md", out)
        self.assertIn("c.md:1", out)

    def test_commit_identity_must_be_allowlisted(self):
        repo = make_repo(self)
        commit(repo, {"a.md": "one\n"})
        commit(repo, {"b.md": "two\n"}, email=PERSONAL_EMAIL)
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 1)
        self.assert_hit(out, "commit-identity")
        self.assertEqual(out.count("PRIVACY_HIT commit-identity"), 1, out)

    def test_secret_removed_later_still_found_in_history(self):
        repo = make_repo(self)
        commit(repo, {"keys.txt": f"{OPENAI_LIKE}\n"})
        commit(repo, {"keys.txt": None})
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 1)
        self.assertIn("PRIVACY_HIT key commit ", out)

    def test_commit_message_is_scanned(self):
        repo = make_repo(self)
        commit(repo, {"a.md": "one\n"}, message=f"contact {PERSONAL_EMAIL}")
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 1)
        self.assertIn("message", out)

    def test_allowlist_suppresses_hit_with_reason(self):
        repo = make_repo(self)
        commit(repo, {"docs/a.md": f"mail {PERSONAL_EMAIL}\n"})
        allow = make_tmp_dir(self) / "allow.txt"
        allow.write_text("email docs/*.md ^alice@ # a documented public contact\n", encoding="utf-8")
        code, out, _ = self.run_scan(repo, "--allowlist", str(allow))
        self.assertEqual(code, 0, out)

    def test_allowlist_entry_without_reason_is_an_error(self):
        repo = make_repo(self)
        commit(repo, {"a.md": "one\n"})
        allow = make_tmp_dir(self) / "allow.txt"
        allow.write_text("email *.md ^alice@\n", encoding="utf-8")
        code, _, err = self.run_scan(repo, "--allowlist", str(allow))
        self.assertEqual(code, 2)
        self.assertIn("reason", err)

    def test_gitleaks_require_without_binary_exits_2(self):
        repo = make_repo(self)
        commit(repo, {"a.md": "one\n"})
        out, err = io.StringIO(), io.StringIO()
        no_gitleaks = lambda name, *a, **k: None if name == "gitleaks" else shutil.which(name, *a, **k)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.scanner.main(["--repo", str(repo), "--gitleaks", "require", "--terms", str(self.no_terms)],
                                     which=no_gitleaks)
        self.assertEqual(code, 2)
        self.assertIn("gitleaks", err.getvalue())

    def test_scanner_and_its_tests_do_not_flag_themselves(self):
        repo = make_repo(self)
        commit(repo, {"tools/privacy_scan.py": SCANNER_PATH.read_text(encoding="utf-8"),
                      "tests/test_privacy_scan.py": pathlib.Path(__file__).read_text(encoding="utf-8")})
        code, out, _ = self.run_scan(repo)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
