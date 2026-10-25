# oah land 핵심 검사 — 실제 저장소·로컬 bare 원격·가짜 gh·gitleaks·unlazy 로 리뷰·게이트·영향·새로 걸린 검사·비밀 검사·CI 판정과 조건부 푸시, 기록을 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import sys
import unittest
from unittest.mock import patch

from oah import intent, land, unlazy
from oah.gates.ledger import definition_digest
from oah.review import receipts
from tests.fixtures.gates.support import StubNode, make_unlazy_dir, met_ledger, run_cli, stub_pin
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir
from tests.test_privacy_scan import load_scanner
from tests.test_review_summary import publish_receipt

FAKE_GH = pathlib.Path(__file__).resolve().parent / "fakes" / "fake_gh.py"
CLAUDE = "\n\nAgent-Engine: claude\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
PACKET = ("<!-- OAH_INTENT:BEGIN -->\n## Intent\nIntent-ID: t1\nRevision: 1\nStatus: accepted\n"
          "### Problem\nThe app has no version.\n### Desired outcome\nIt prints one.\n"
          "### Success conditions\n- AC1: the app prints v1\n- AC2: errors say what to do next\n"
          "### Constraints\n### Non-goals\n### Open questions\n<!-- OAH_INTENT:END -->\n"
          "## Scope\n- src\n- .oah/tasks/t1\n## Plan\n- initial → claude → medium\n## Acceptance\n## Return\n")
LEDGER = ("# Gates: t1\n\n- [ ] G1: the app exists\n  CHECK: oah check exists src/app.py\n  EXPECT: OAH_CHECK_OK\n"
          "  EVIDENCE: pending\n")
ALL_MET = {"stdout": "GATES.md: 1 gates\nALL MET (1 met)\n", "code": 0}
UNMET = {"stdout": "  UNMET GATES:G1 (unchecked): x\nGATES.md: 1 gates\nUNMET: 1 (met: 0)\n  GATES:G1\n", "code": 1}
OTHER_LEDGER = ("# Gates: x\n\n- [ ] G1: the app runs\n  CHECK: python src/app.py\n  EXPECT: v1\n  EVIDENCE: pending\n")
FAKE_GITLEAKS = """import json, os, sys
with open(os.environ["FAKE_GITLEAKS_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
code = int(os.environ.get("FAKE_GITLEAKS_CODE", "0"))
print("Finding: REDACTED\\nAuthor: Someone Private\\nEmail: someone@example.com" if code == 1 else "no leaks found")
sys.exit(code)
"""


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", env=git_env()).stdout.strip()


def describe(result: land.Assessment) -> str:
    return "\n".join(f"{check.name}: {check.state} {check.code} {check.message}" for check in result.checks)


class LandProject:
    """A finished task t1 on branch feat, reviewed and gated, whose main is on a local bare remote."""

    def __init__(self, test: unittest.TestCase, *, config: str = "", main_files: dict | None = None,
                 work_files: dict | None = None, ledger: str | None = LEDGER, commit_summary: bool = True,
                 receipt: bool = True, finish: bool = True, unmet: tuple[str, ...] = ()) -> None:
        self.test = test
        self.work = make_tmp_dir(test)
        self.repo = make_git_repo(test)
        self.state, self.home = self.work / "state", self.work / "home"
        self.home.mkdir()
        self.stub = StubNode(self.work / "stub")
        self.unlazy = make_unlazy_dir(self.work / "unlazy")
        self.pin = stub_pin(self.unlazy)
        self.remote = self.work / "remote.git"
        self.gitleaks = self.work / "fake_gitleaks.py"
        self.gitleaks.write_text(FAKE_GITLEAKS, encoding="utf-8")
        self.gh_script, self.gh_log, self.gitleaks_log = (self.work / "gh.json", self.work / "gh.jsonl",
                                                          self.work / "gitleaks.jsonl")
        self.gh_argv: tuple | None = (sys.executable, str(FAKE_GH))
        self.gitleaks_argv: tuple | None = (sys.executable, str(self.gitleaks))
        self.base = git_commit(self.repo, {".gitattributes": "* -text\n", ".gitignore": ".oah/tasks/t1/GATES.md\n",
                                           "README.md": "hello\n", ".oah/config.toml": config,
                                           ".oah/tasks/t1/PACKET.md": PACKET, "src/app.py": "print('v0')\n",
                                           **(main_files or {})}, "start" + CLAUDE)
        subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(self.remote)], check=True,
                       env=git_env())
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", "-b", "feat")
        self.head = self.commit({"src/app.py": "print('v1')\n", **(work_files or {})})
        self.env = self.stub.env(OAH_UNLAZY_DIR=str(self.unlazy), OAH_STATE_DIR=str(self.state),
                                 HOME=str(self.home), USERPROFILE=str(self.home),
                                 FAKE_GITLEAKS_LOG=str(self.gitleaks_log), FAKE_GITLEAKS_CODE="0",
                                 FAKE_GH_SCRIPT=str(self.gh_script), FAKE_GH_LOG=str(self.gh_log))
        for name in ("OAH_WORKER", "OAH_RUN_ID", "OAH_TASK", "NODE_OPTIONS"):
            self.env.pop(name, None)
        started = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", ".oah/tasks/t1/PACKET.md")
        test.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        source = self.repo / ".oah/tasks/t1/PACKET.md"
        self.binding = intent.parse(intent.read_source(source)).bind(".oah/tasks/t1/PACKET.md")
        if ledger is not None:
            self.ledger = self.repo / ".oah/tasks/t1/GATES.md"
            self.ledger.write_text(ledger, encoding="utf-8")
            self.gates_check(unmet)
            if commit_summary and (self.repo / ".oah/tasks/t1/GATES.summary.md").exists():
                self.commit({}, "gate summary")
        if receipt:
            publish_receipt(self, 1, head=self.head, base=self.base)
        if finish:
            self.finish()
        self.ci([{"name": "tests", "status": "completed", "conclusion": "success"}])

    def oah(self, *argv: str, stdin: str | None = None):
        return run_cli(list(argv), cwd=self.repo, env=self.env, pin=self.pin, stdin=stdin)

    def commit(self, files: dict, message: str = "work", trailer: str = CLAUDE) -> str:
        self.head = git_commit(self.repo, files, message + trailer)
        return self.head

    def gates_check(self, unmet: tuple[str, ...] = ()) -> None:
        """`oah gates check t1` through the stub unlazy: every runnable gate passes except `unmet`."""
        text = self.ledger.read_text(encoding="utf-8")
        ids = [line.split(":")[0].split()[-1] for line in text.splitlines() if line.startswith("- [")]
        met = len(ids) - len(unmet)
        verdict = (f"UNMET: {len(unmet)} (met: {met})\n" + "".join(f"  GATES:{gate}\n" for gate in unmet)
                   if unmet else f"ALL MET ({met} met")
        run = {"write": {str(self.ledger): met_ledger(text, skip=unmet)}, "code": 1 if unmet else 0,
               "stdout": "".join(f"  {'FAIL' if gate in unmet else 'PASS'} GATES:{gate}: x\n" for gate in ids)
               + f"GATES.md: {len(ids)} gates\n" + (verdict if unmet else f"{verdict}, reran: {met})\n")}
        status = {"stdout": f"GATES.md: {len(ids)} gates\n" + (verdict if unmet else f"{verdict})\n"),
                  "code": 1 if unmet else 0}
        self.stub.reply("gate-check.mjs", run, status)
        self.oah("gates", "check", "t1")

    def finish(self) -> None:
        answer = self.work / "return.md"
        answer.write_text(f"{self.binding.line()}\nAC1: met — prints v1\nAC2: met — explained\n", encoding="utf-8")
        done = self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "done", "--return", str(answer))
        self.test.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def ci(self, runs: list[dict] | None = None, **script) -> None:
        body = {"check_runs": {self.head: runs or []}, **script}
        self.gh_script.write_text(json.dumps(body), encoding="utf-8")

    def advance_main(self, files: dict, message: str = "elsewhere") -> str:
        """Another clone lands a commit on the remote's main."""
        other = make_tmp_dir(self.test) / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True, env=git_env())
        for name, content in files.items():
            path = other / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        git(other, "add", "-f", "-A")
        git(other, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
        git(other, "push", "-q", "origin", "HEAD:main")
        return git(other, "rev-parse", "HEAD")

    def remote_main(self) -> str:
        return subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "main"], check=True,
                              capture_output=True, text=True).stdout.strip()

    def patched(self):
        import contextlib

        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        stack.enter_context(patch.object(unlazy, "PIN", self.pin))
        stack.enter_context(patch.object(land, "find_gh", lambda env: self.gh_argv))
        stack.enter_context(patch.object(land, "find_gitleaks", lambda env: self.gitleaks_argv))
        return stack

    def assess(self, **options) -> land.Assessment:
        with self.patched():
            return land.assess(self.repo, "t1", env=dict(self.env), home=self.home, **options)

    def check(self, result: land.Assessment, name: str) -> land.Check:
        return next(check for check in result.checks if check.name == name)


class AssessTest(unittest.TestCase):
    def test_landing_fixtures_contain_no_private_literals(self):
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        self.assertEqual(load_scanner().scan_text(source, []), [])

    def test_exported_gate_labels_require_the_exact_identifier_pattern(self):
        private = "account-" + secrets.token_hex(16)
        windows_home = "C:\\" + "Users\\" + private
        labels = ("GATES.md#G1", "a" * 80, "a" * 81, f"checks/{private}#G1",
                  f"{windows_home}\\GATES.md#G1", f"G1\n{private}", "게이트#G1", "G1\n")
        result = land.Assessment(task=private, head="a" * 40, remote=private, branch=private,
                                 changed=[f"{windows_home}\\app.py"], warnings=[private])
        result.required = [{"gate": label, "binding": "exec", "targets": result.changed,
                            "definition": "b" * 64, "state": "pending"} for label in labels]
        exported = result.to_json()
        for position, (label, item) in enumerate(zip(labels, exported["required_checks"]), 1):
            with self.subTest(position=position):
                self.assertEqual(item["gate"]["position"], position)
                self.assertEqual(item["gate"]["digest"], hashlib.sha256(label.encode("utf-8")).hexdigest())
                if position <= 2:
                    self.assertEqual(item["gate"]["label"], label)
                else:
                    self.assertNotIn("label", item["gate"])
        folder = make_tmp_dir(self)
        land.record(folder, result, "check", None)
        raw = (folder / "land" / private / "records.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(private, json.dumps(exported) + raw + "\n".join(land._required_text(result.required)))
        self.assertEqual(json.loads(raw)["required_checks"], exported["required_checks"])

    def test_simple_committed_gate_label_remains_visible(self):
        project = LandProject(self, config='[impact]\nledger_globs = ["GATES.md", ".oah/tasks/*/GATES.md"]\n',
                              main_files={"GATES.md": OTHER_LEDGER})
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_CHECKS_REQUIRED", describe(result))
        self.assertIn("GATES.md#G1", result.failure.message)
        self.assertEqual(result.to_json()["required_checks"][0]["gate"]["label"], "GATES.md#G1")

    def test_ci_later_page_errors_refuse_and_preserve_known_failures(self):
        project = LandProject(self)
        errors = ({"exit": 1}, {"raw": "invalid JSON"}, {"raw": "[]"},
                  {"sleep": 2}, {"total_count": 102})
        for failed in (False, True):
            for error in errors:
                with self.subTest(failed=failed, error=error):
                    runs = [{"name": "ok", "status": "completed", "conclusion": "success"}] * 101
                    if failed:
                        runs[0] = {"name": "failed", "status": "completed", "conclusion": "failure"}
                    project.ci(runs, pages={"2": error})
                    with patch.object(land, "GH_TIMEOUT", 0.5):
                        result = project.assess()
                    self.assertFalse(result.ready, describe(result))
                    self.assertEqual(result.check("ci").code,
                                     "OAH_LAND_CI_FAILED" if failed else "OAH_LAND_CI_PENDING", describe(result))

    def test_ci_invalid_pages_and_enums_refuse_without_disclosure(self):
        project = LandProject(self)
        private = secrets.token_hex(24)
        for overrides in ({"status": private}, {"conclusion": private}, {"status": [private]},
                          {"conclusion": {"private": private}}, {"head_sha": "e" * 40}):
            with self.subTest(field=next(iter(overrides))):
                project.ci([{"name": private, "status": "completed", "conclusion": "success", **overrides}])
                result = project.assess()
                self.assertFalse(result.ready, describe(result))
                self.assertEqual(result.check("ci").code, "OAH_LAND_CI_PENDING")
                self.assertNotIn(private, describe(result) + json.dumps(result.to_json()))
        for raw in ('[]', '{"total_count": true, "check_runs": []}',
                    '{"total_count": "0", "check_runs": []}',
                    '{"total_count": 1, "check_runs": []}'):
            with self.subTest(raw=raw):
                project.ci(raw=raw)
                self.assertFalse(project.assess().ready)

    def test_ci_malformed_totals_preserve_same_page_failures(self):
        project = LandProject(self)
        private = "account-" + secrets.token_hex(16)
        for total in (None, True, -1, "1", [], {}):
            for failed in (False, True):
                with self.subTest(total=total, failed=failed):
                    project.ci([{"name": private, "status": "completed",
                                 "conclusion": "failure" if failed else "success"}], total_count=total)
                    result = project.assess()
                    checked = result.check("ci")
                    self.assertFalse(result.ready, describe(result))
                    self.assertEqual(checked.code, "OAH_LAND_CI_FAILED" if failed else "OAH_LAND_CI_PENDING")
                    self.assertIn("incomplete", checked.message)
                    if failed:
                        self.assertIn("1 CI check run(s) failed", checked.message)
                        self.assertIn("positions 1", checked.message)
                    land.record(project.state, result, "check", None)
                    raw = (project.state / "land/t1/records.jsonl").read_text(encoding="utf-8")
                    self.assertNotIn(private, describe(result) + json.dumps(result.to_json()) + raw)

    def test_ci_duplicate_ids_refuse_and_preserve_observed_failures(self):
        project = LandProject(self)
        for count in (2, 101):
            project.ci([{"id": index, "status": "completed", "conclusion": "success"}
                        for index in range(1, count + 1)])
            complete = project.assess()
            self.assertTrue(complete.ready, describe(complete))
            self.assertEqual(complete.check("ci").detail, {"runs": count})
            for failed_position in (None, 1, count):
                with self.subTest(count=count, failed_position=failed_position):
                    runs = [{"id": index, "status": "completed", "conclusion": "success"}
                            for index in range(1, count + 1)]
                    runs[-1]["id"] = count - 1
                    if failed_position is not None:
                        runs[failed_position - 1]["conclusion"] = "failure"
                    project.ci(runs)
                    result = project.assess()
                    self.assertFalse(result.ready, describe(result))
                    checked = result.check("ci")
                    self.assertEqual(checked.code, "OAH_LAND_CI_FAILED" if failed_position is not None
                                     else "OAH_LAND_CI_PENDING", describe(result))
                    self.assertIn("incomplete", checked.message)
                    if failed_position is not None:
                        self.assertIn(f"positions {failed_position}", checked.message)

    def test_ci_invalid_ids_refuse_without_disclosure(self):
        project = LandProject(self)
        private = "account-" + secrets.token_hex(16)
        for identifier in (None, True, 0, -1, 1.5, private, [], {}):
            with self.subTest(identifier=identifier):
                project.ci([{"id": identifier, "name": private, "status": "completed", "conclusion": "success"}])
                result = project.assess()
                self.assertFalse(result.ready, describe(result))
                self.assertEqual(result.check("ci").code, "OAH_LAND_CI_PENDING")
                self.assertNotIn(private, describe(result) + json.dumps(result.to_json()))
        project.ci(raw=json.dumps({"total_count": 1, "check_runs": [
            {"head_sha": project.head, "status": "completed", "conclusion": "failure"}]}))
        result = project.assess()
        self.assertEqual(result.check("ci").code, "OAH_LAND_CI_FAILED")
        self.assertIn("incomplete", result.check("ci").message)

    def test_fetched_ledger_settings_cannot_be_removed_or_relocated_by_head(self):
        for old_globs, new_globs, path in (
                ('[".oah/tasks/*/GATES.md"]', '[]', ".oah/tasks/x/GATES.md"),
                ('["checks/*/GATES.md"]', '["elsewhere/*/GATES.md"]', "checks/x/GATES.md")):
            with self.subTest(globs=new_globs):
                project = LandProject(self, config=f"[impact]\nledger_globs = {old_globs}\n",
                                      main_files={path: OTHER_LEDGER},
                                      work_files={".oah/config.toml": f"[impact]\nledger_globs = {new_globs}\n"})
                result = project.assess()
                self.assertEqual(result.check("gates").state, "passed", describe(result))
                self.assertEqual(result.check("required-checks").code, "OAH_LAND_CHECKS_REQUIRED", describe(result))
                self.assertEqual([(item["gate"], item["state"]) for item in result.required],
                                 [(f"{path}#G1", "pending")])
                self.assertFalse(result.ready, describe(result))

    def test_rebased_imported_pending_gate_still_requires_evidence(self):
        project = LandProject(self)
        moved = project.advance_main({".oah/tasks/x/GATES.md": OTHER_LEDGER})
        git(project.repo, "fetch", "origin", "main")
        git(project.repo, "rebase", "origin/main")
        project.head = git(project.repo, "rev-parse", "HEAD")
        project.gates_check()
        project.commit({}, "refresh gate summary")
        publish_receipt(project, 2, base=moved)
        project.ci([{"name": "tests", "status": "completed", "conclusion": "success"}])
        result = project.assess()
        self.assertEqual(result.check("gates").state, "passed", describe(result))
        self.assertEqual(result.check("fast-forward").state, "passed", describe(result))
        self.assertEqual(result.failure.code if result.failure else None, "OAH_LAND_CHECKS_REQUIRED", describe(result))
        self.assertEqual([item["state"] for item in result.required], ["pending"])

    def test_refuses_ambiguous_push_destinations_without_disclosure(self):
        for kind in ("pushurl", "multiple-push", "multiple-fetch", "push-rewrite", "fetch-rewrite"):
            with self.subTest(kind=kind):
                project = LandProject(self)
                other = project.work / ("private-" + secrets.token_hex(12) + ".git")
                subprocess.run(["git", "clone", "--bare", "-q", str(project.remote), str(other)],
                               check=True, env=git_env())
                if kind == "pushurl":
                    git(project.repo, "config", "remote.origin.pushurl", str(other))
                elif kind == "multiple-push":
                    for url in (str(project.remote), str(other)):
                        git(project.repo, "config", "--add", "remote.origin.pushurl", url)
                elif kind == "multiple-fetch":
                    git(project.repo, "config", "--add", "remote.origin.url", str(other))
                else:
                    key = "pushInsteadOf" if kind == "push-rewrite" else "insteadOf"
                    git(project.repo, "config", f"url.{other}.{key}", str(project.remote))
                result = project.assess()
                self.assertFalse(result.ready, describe(result))
                self.assertNotIn(str(other), json.dumps(result.to_json()))
                self.assertEqual(project.remote_main(), project.base)

    def test_url_named_remote_indirection_refuses_before_assessment_or_push(self):
        for after_assessment in (False, True):
            with self.subTest(after_assessment=after_assessment):
                project = LandProject(self)
                result = project.assess() if after_assessment else None
                other = project.work / ("private-" + secrets.token_hex(12) + ".git")
                subprocess.run(["git", "clone", "--bare", "-q", str(project.remote), str(other)],
                               check=True, env=git_env())
                git(project.repo, "config", f"remote.{project.remote}.url", str(project.remote))
                git(project.repo, "config", f"remote.{project.remote}.pushurl", str(other))
                if after_assessment:
                    self.assertTrue(result.ready, describe(result))
                    with project.patched(), self.assertRaises(land.LandError) as caught:
                        land.push(project.repo, result, env=dict(project.env))
                    self.assertEqual(caught.exception.code, "OAH_LAND_DESTINATION_UNSUPPORTED")
                    shown = str(caught.exception)
                else:
                    result = project.assess()
                    self.assertFalse(result.ready, describe(result))
                    shown = describe(result) + json.dumps(result.to_json())
                self.assertIn("remote names without", shown)
                self.assertNotIn(str(other), shown)
                self.assertNotIn(str(project.remote), shown)
                self.assertEqual(project.remote_main(), project.base)
                self.assertEqual(git(other, "rev-parse", "main"), project.base)

    def test_simple_remote_checks_effective_urls_and_matching_rewrites(self):
        project = LandProject(self)
        for name in ("url:remote", "path/remote"):
            with self.subTest(name=name):
                git(project.repo, "config", f"remote.{name}.url", str(project.remote))
                with project.patched(), self.assertRaises(land.LandError) as caught:
                    land._destination(project.repo, "origin", project.env)
                self.assertEqual(caught.exception.code, "OAH_LAND_DESTINATION_UNSUPPORTED")
                git(project.repo, "config", "--remove-section", f"remote.{name}")
        git(project.repo, "config", "remote.origin.pushurl", str(project.remote))
        for key in ("insteadOf", "pushInsteadOf"):
            with self.subTest(key=key):
                config_key = f"url.{project.work / 'other.git'}.{key}"
                # A rewrite unrelated to this remote must not change its destination.
                git(project.repo, "config", config_key, "unrelated:")
                with project.patched():
                    self.assertEqual(land._destination(project.repo, "origin", project.env),
                                     (str(project.remote), str(project.remote)))
                git(project.repo, "config", config_key, str(project.remote))
                with project.patched(), self.assertRaises(land.LandError):
                    land._destination(project.repo, "origin", project.env)
                git(project.repo, "config", "--unset", config_key)

    def test_ci_uses_assessed_destination_over_gh_repo_and_default(self):
        project = LandProject(self)
        unrelated = "elsewhere/" + secrets.token_hex(10)
        project.env["GH_REPO"] = unrelated
        project.ci([{"name": "destination", "status": "completed", "conclusion": "failure"}],
                   expected_repo="oah-local-destination:" + hashlib.sha256(str(project.remote).encode("utf-8")).hexdigest(),
                   other_runs=[{"name": "fork", "status": "completed", "conclusion": "success"}])
        for inherited in (True, False):
            if not inherited:
                project.env.pop("GH_REPO", None)
            result = project.assess()
            self.assertEqual(result.check("ci").code, "OAH_LAND_CI_FAILED", describe(result))
            self.assertNotIn(unrelated, json.dumps(result.to_json()))
            self.assertNotIn(str(project.remote), json.dumps(result.to_json()))

    def test_ci_selector_does_not_turn_local_paths_into_github_repositories(self):
        for destination in ("/a/b", "C:\\a\\b", "file:///a/b"):
            repository, _host = land._gh_repository(destination)
            self.assertNotIn("/", repository)
            self.assertNotIn(destination, repository)
        for destination in ("https://github.com/owner/project.git", "git@github.com:owner/project.git",
                            "ssh://git@github.com/owner/project.git"):
            self.assertEqual(land._gh_repository(destination), ("github.com/owner/project", "github.com"))

    def test_ci_does_not_certify_truncated_pagination(self):
        project = LandProject(self)
        runs = [{"name": "ok", "status": "completed", "conclusion": "success"}] * (land.GH_PER_PAGE * land.GH_PAGES)
        for status, conclusion in (("completed", "failure"), ("in_progress", None)):
            project.ci(runs + [{"name": "late", "status": status, "conclusion": conclusion}])
            result = project.assess()
            self.assertNotEqual(result.check("ci").state, "passed", describe(result))
            self.assertFalse(result.ready, describe(result))

    def test_ready_when_every_check_binds_to_head(self):
        project = LandProject(self)
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        self.assertEqual([check.name for check in result.checks], list(land.CHECKS))
        self.assertEqual({check.state for check in result.checks}, {"passed"}, describe(result))
        self.assertIsNone(result.failure)
        self.assertEqual((result.head, result.fetched, result.remote, result.branch),
                         (project.head, project.base, "origin", "main"))
        self.assertEqual(result.impact["worktree"], project.head)
        self.assertEqual(result.impact["default_branch"], project.base)
        self.assertRegex(result.impact["result"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(result.required, [])
        self.assertEqual(sorted(result.changed), [".oah/tasks/t1/GATES.summary.md", "src/app.py"])
        scanned = json.loads(project.gitleaks_log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(scanned[scanned.index("--log-opts") + 1], f"{project.base}..{project.head}")
        self.assertIn("--redact", scanned)
        asked = json.loads(project.gh_log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertIn(f"repos/{{owner}}/{{repo}}/commits/{project.head}/check-runs?per_page=100&page=1", asked)
        self.assertEqual(result.check("review").detail["round"], 1)
        data = result.to_json()
        self.assertTrue(data["ready"])
        self.assertEqual(data["default_branch"], project.base)
        self.assertEqual(data["impact"]["result"], result.impact["result"])

    def test_review_check_uses_latest_task_event(self):
        project = LandProject(self, receipt=False)
        older = git(project.repo, "rev-parse", "HEAD~1")
        publish_receipt(project, 1, head=older, base=project.base)
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_REVIEW_REQUIRED", describe(result))
        self.assertIn("no converged latest review approves HEAD", result.failure.message)
        publish_receipt(project, 2, head=project.head, base=project.base)
        self.assertTrue(project.assess().ready)
        receipts.log_round(project.state, "t1", {"event": "start", "base": project.base, "series_round": 3,
                                                 "round": 3, "attempt": 1, "run_id": "later"})
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_REVIEW_REQUIRED", describe(result))
        self.assertIn("inspect the review history", result.failure.message)
        other = intent.Binding(".oah/tasks/t1/PACKET.md", "t1", 2, "sha256:" + "d" * 64, ("AC1", "AC2"))
        publish_receipt(project, 3, head=project.head, base=project.base, binding=other)
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_REVIEW_REQUIRED", describe(result))
        self.assertIn("current intent binding", result.failure.message)

    def test_exact_head_failures_have_named_codes(self):
        older = LandProject(self, receipt=False)
        publish_receipt(older, 1, head=git(older.repo, "rev-parse", "HEAD~1"), base=older.base)
        result = older.assess()
        self.assertEqual((result.ready, result.failure.code), (False, "OAH_LAND_REVIEW_REQUIRED"), describe(result))
        self.assertIn("oah review --task <task>", result.failure.message)

        failing = LandProject(self)
        failing.ci([{"name": "tests", "status": "completed", "conclusion": "failure"},
                    {"name": "lint", "status": "completed", "conclusion": "success"}])
        result = failing.assess()
        self.assertEqual((result.ready, result.failure.code), (False, "OAH_LAND_CI_FAILED"), describe(result))
        self.assertIn("1 CI check run(s) failed", result.failure.message)
        self.assertIn("positions 1", result.failure.message)
        self.assertNotIn("lint", result.failure.message)

        unbound = LandProject(self, commit_summary=False)
        result = unbound.assess()
        self.assertEqual((result.ready, result.failure.code), (False, "OAH_LAND_GATES_SUMMARY_MISSING"),
                         describe(result))
        self.assertIn("oah gates check <task>", result.failure.message)

    def test_gates_need_a_committed_summary_at_head(self):
        bare = LandProject(self, ledger=None)
        result = bare.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_GATES_REQUIRED", describe(result))
        self.assertIn("oah gates new <task>", result.failure.message)

        project = LandProject(self)
        project.stub.reply("gate-check.mjs", UNMET)
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_GATES_UNMET", describe(result))
        project.stub.reply("gate-check.mjs", ALL_MET)
        self.assertTrue(project.assess().ready)

        summary = project.repo / ".oah/tasks/t1/GATES.summary.md"
        text = summary.read_text(encoding="utf-8")
        project.commit({".oah/tasks/t1/GATES.summary.md": text.replace("binding=run", "binding=carry")}, "edit")
        result = project.assess()
        self.assertEqual(project.check(result, "gates").code, "OAH_LAND_GATES_SUMMARY_STALE", describe(result))

    def test_offline_fetch_failure_and_missing_map_are_stale(self):
        project = LandProject(self)
        result = project.assess(offline=True)
        self.assertEqual(result.failure.code, "OAH_LAND_IMPACT_STALE", describe(result))
        self.assertEqual([project.check(result, name).state for name in ("lease", "intent", "review", "gates")],
                         ["passed"] * 4)
        self.assertEqual([project.check(result, name).state for name in ("required-checks", "fast-forward", "scan")],
                         ["skipped"] * 3)
        self.assertIn("offline", result.failure.message)
        self.assertIsNone(result.fetched)

        git(project.repo, "remote", "set-url", "origin", str(project.work / "gone.git"))
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_IMPACT_STALE", describe(result))
        self.assertIn("fetch failed", result.failure.message)
        git(project.repo, "remote", "set-url", "origin", str(project.remote))

        from oah.impact.errors import ImpactError

        def broken(*_args, **_kwargs):
            raise ImpactError("OAH_IMPACT_GIT_FAILED", "git ls-files failed")

        with patch("oah.impact.mapstore.fresh_map", broken):
            result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_IMPACT_STALE", describe(result))
        self.assertIn("impact map of this worktree is unavailable", result.failure.message)

        disabled = LandProject(self, config="[impact]\nrefresh_default_branch = false\n")
        result = disabled.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_IMPACT_STALE", describe(result))
        self.assertIn("refresh_default_branch", result.failure.message)

    def test_new_gate_binding_on_default_branch_requires_the_check(self):
        project = LandProject(self)
        moved = project.advance_main({".oah/tasks/x/GATES.md": OTHER_LEDGER, "docs/note.md": "x\n"})
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_CHECKS_REQUIRED", describe(result))
        self.assertEqual(result.fetched, moved)
        self.assertEqual(project.check(result, "impact").state, "passed")
        self.assertEqual(result.required, [{
            "gate": ".oah/tasks/x/GATES.md#G1", "binding": "exec", "targets": ["src/app.py"],
            "definition": definition_digest("python src/app.py", "v1", None), "state": "pending"}])
        self.assertIn(hashlib.sha256(b".oah/tasks/x/GATES.md#G1").hexdigest(), result.failure.message)
        self.assertEqual(project.check(result, "fast-forward").code, "OAH_LAND_IMPACT_STALE")
        self.assertIn("does not contain", project.check(result, "fast-forward").message)
        self.assertEqual(project.check(result, "scan").state, "skipped")

    def test_changed_gate_definition_must_pass_against_head(self):
        weakened = OTHER_LEDGER.replace("CHECK: python src/app.py", "CHECK: python -c \"print('v1')\"")
        copy = "\n- [ ] G2: the app runs\n  CHECK: python src/app.py\n  EXPECT: v1\n  EVIDENCE: pending\n"
        cases = {"pending": LEDGER, "passed": LEDGER + copy}
        for expected, ledger in cases.items():
            with self.subTest(expected=expected):
                project = LandProject(self, main_files={".oah/tasks/x/GATES.md": OTHER_LEDGER},
                                      work_files={".oah/tasks/x/GATES.md": weakened}, ledger=ledger)
                result = project.assess()
                required = result.required
                self.assertEqual([(item["gate"], item["state"]) for item in required],
                                 [(".oah/tasks/x/GATES.md#G1", expected)], describe(result))
                self.assertEqual(result.ready, expected == "passed", describe(result))
        project = LandProject(self, main_files={".oah/tasks/x/GATES.md": OTHER_LEDGER},
                              work_files={".oah/tasks/x/GATES.md": weakened}, ledger=LEDGER + copy,
                              unmet=("G2",), finish=False)
        result = project.assess()
        self.assertEqual([(item["gate"], item["state"]) for item in result.required],
                         [(".oah/tasks/x/GATES.md#G1", "failed")], describe(result))
        self.assertEqual(project.check(result, "required-checks").code, "OAH_LAND_CHECKS_REQUIRED")

    def test_scan_and_ci_results(self):
        project = LandProject(self)
        project.gitleaks_argv = None
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_SCAN_REQUIRED", describe(result))
        self.assertIn("gitleaks", result.failure.message)
        project.gitleaks_argv = (sys.executable, str(project.gitleaks))
        for code, expected in (("1", "possible secrets"), ("3", "exited 3")):
            project.env["FAKE_GITLEAKS_CODE"] = code
            result = project.assess()
            self.assertEqual(result.failure.code, "OAH_LAND_SCAN_FAILED", describe(result))
            self.assertIn(expected, result.failure.message)
            self.assertNotIn("Someone Private", describe(result))
            self.assertNotIn("someone@example.com", json.dumps(result.to_json()))
        project.env["FAKE_GITLEAKS_CODE"] = "0"

        project.ci([{"name": "tests", "status": "in_progress", "conclusion": None}])
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_CI_PENDING", describe(result))
        project.ci([])
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        self.assertEqual(project.check(result, "ci").state, "unverified")
        self.assertIn("no CI check runs", project.check(result, "ci").message)
        project.ci([{"name": f"job {k}", "status": "completed", "conclusion": "success"} for k in range(101)]
                   + [{"name": "late", "status": "completed", "conclusion": "cancelled"}])
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_CI_FAILED", describe(result))
        self.assertIn("positions 102", result.failure.message)
        project.ci([{"name": "tests", "status": "completed", "conclusion": "success",
                     "head_sha": "e" * 40}])
        self.assertEqual(project.check(project.assess(), "ci").code, "OAH_LAND_CI_PENDING")
        fake_token = "gh" + "o_" + secrets.token_hex(18)
        project.ci(exit=1, stderr=f"HTTP 401: Bad credentials {fake_token} for decoy-owner-login\n")
        result = project.assess()
        self.assertFalse(result.ready, describe(result))
        self.assertEqual(project.check(result, "ci").code, "OAH_LAND_CI_PENDING")
        self.assertNotIn(fake_token, describe(result) + json.dumps(result.to_json()))
        self.assertNotIn("decoy-owner-login", describe(result) + json.dumps(result.to_json()))
        project.gh_argv = None
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        self.assertIn("gh is not installed", project.check(result, "ci").message)

    def test_open_lease_dirty_tree_and_workers_refuse(self):
        project = LandProject(self, finish=False)
        result = project.assess()
        self.assertEqual(result.failure.code, "OAH_LAND_LEASE_OPEN", describe(result))
        self.assertIn("oah finish --task <task> --agent <agent>", result.failure.message)
        (project.repo / "README.md").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(land.LandError) as caught:
            project.assess()
        self.assertEqual(caught.exception.code, "OAH_LAND_DIRTY_TREE")
        (project.repo / "README.md").write_text("hello\n", encoding="utf-8")
        (project.repo / "notes.txt").write_text("a local note\n", encoding="utf-8")
        with self.assertRaises(land.LandError) as caught:
            project.assess()
        self.assertEqual(caught.exception.code, "OAH_LAND_DIRTY_TREE")
        self.assertIn(hashlib.sha256(b"notes.txt").hexdigest(), str(caught.exception))
        self.assertNotIn("notes.txt", str(caught.exception))
        (project.repo / "notes.txt").unlink()
        project.env["OAH_WORKER"] = "1"
        with self.assertRaises(land.LandError) as caught:
            project.assess()
        self.assertEqual(caught.exception.code, "OAH_LAND_WORKER_REFUSED")

    def test_index_flags_refuse_even_when_status_is_clean_without_changing_index(self):
        project = LandProject(self)
        path = project.repo / "src/app.py"
        committed = path.read_bytes()
        for flags in (("--assume-unchanged",), ("--skip-worktree",),
                      ("--assume-unchanged", "--skip-worktree")):
            for changed in (False, True):
                with self.subTest(flags=flags, changed=changed):
                    for flag in flags:
                        git(project.repo, "update-index", flag, "--", "src/app.py")
                    path.write_bytes(b"print('hidden change')\n" if changed else committed)
                    self.assertEqual(git(project.repo, "status", "--porcelain=v1", "--untracked-files=no"), "")
                    index = project.repo / ".git/index"
                    before = index.read_bytes()
                    with self.assertRaises(land.LandError) as caught:
                        project.assess()
                    self.assertEqual(caught.exception.code, "OAH_LAND_DIRTY_TREE")
                    self.assertEqual(index.read_bytes(), before)
                    self.assertEqual(project.remote_main(), project.base)
                    git(project.repo, "update-index", "--no-assume-unchanged", "--", "src/app.py")
                    git(project.repo, "update-index", "--no-skip-worktree", "--", "src/app.py")
                    path.write_bytes(committed)


class PushTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "the remote-name assertion hook is a POSIX shell script")
    def test_push_uses_the_verified_remote_name(self):
        project = LandProject(self)
        hook = project.repo / ".git/hooks/pre-push"
        hook.write_bytes(b'#!/bin/sh\n[ "$1" = origin ]\n')
        hook.chmod(0o755)
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertTrue(pushed.landed, pushed.message)
        self.assertEqual(project.remote_main(), project.head)

    def test_push_follow_tags_never_publishes_unconfirmed_tags(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                project = LandProject(self)
                git(project.repo, "config", "push.followTags", "true")
                git(project.repo, "-c", "tag.gpgsign=false", "tag", "-a", "unconfirmed", "-m", "tag", project.head)
                result = project.assess()
                self.assertTrue(result.ready, describe(result))
                expected = project.base
                if rejected:
                    expected = project.advance_main({"docs/race.md": "another landing\n"})
                with project.patched():
                    pushed = land.push(project.repo, result, env=dict(project.env))
                self.assertEqual(pushed.landed, not rejected, pushed.message)
                self.assertEqual(project.remote_main(), expected if rejected else project.head)
                self.assertEqual(git(project.remote, "for-each-ref", "--format=%(refname)", "refs/tags"), "")

    @unittest.skipIf(os.name == "nt", "the per-ref rejection hook is a POSIX shell script")
    def test_push_follow_tags_cannot_publish_tags_when_branch_update_is_rejected(self):
        project = LandProject(self)
        git(project.repo, "config", "push.followTags", "true")
        git(project.repo, "-c", "tag.gpgsign=false", "tag", "-a", "unconfirmed", "-m", "tag", project.head)
        hook = project.remote / "hooks/update"
        hook.write_bytes(b'#!/bin/sh\n[ "$1" != refs/heads/main ]\n')
        hook.chmod(0o755)
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertFalse(pushed.landed, pushed.message)
        self.assertEqual(project.remote_main(), project.base)
        self.assertEqual(git(project.remote, "for-each-ref", "--format=%(refname)", "refs/tags"), "")

    def test_push_diagnostics_are_fixed_even_with_oah_prefix(self):
        marker = secrets.token_hex(24)
        for stdout, stderr in ((f"!\tHEAD:refs/heads/main\t[remote rejected] {marker}\n", ""),
                               ("", f"OAH_PUSH_REFUSED: {marker}\n")):
            reason = land._push_reason(stdout, stderr, "refs/heads/main", False, 1)
            self.assertNotIn(marker, reason)
            self.assertEqual(reason, "git exited 1")

    def test_changed_destination_after_assessment_refuses_push(self):
        project = LandProject(self)
        result = project.assess()
        other = project.work / "other.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(project.remote), str(other)],
                       check=True, env=git_env())
        git(project.repo, "remote", "set-url", "origin", str(other))
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertFalse(pushed.landed)
        self.assertEqual(project.remote_main(), project.base)
        self.assertEqual(git(other, "rev-parse", "main"), project.base)

    @unittest.skipIf(os.name == "nt", "the transport failure hook is a POSIX shell script")
    def test_failed_push_recognizes_head_in_remote_history(self):
        import shlex

        project = LandProject(self)
        result = project.assess()
        descendant = git(project.repo, "commit-tree", "-p", project.head, "-m", "next landing",
                         f"{project.head}^{{tree}}")
        hook = project.repo / ".git/hooks/pre-push"
        command = ["git", "-C", str(project.repo), "-c", "core.hooksPath=/dev/null", "push", "-q",
                   str(project.remote), f"{descendant}:refs/heads/main"]
        hook.write_bytes(("#!/bin/sh\n" + shlex.join(command) + "\nexit 1\n").encode("utf-8"))
        hook.chmod(0o755)
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
            land.record(project.state, result, "merge", pushed)
        self.assertEqual(project.remote_main(), descendant)
        self.assertTrue(pushed.landed, pushed.message)
        self.assertNotIn("nothing landed", pushed.message)
        self.assertTrue(json.loads((project.state / "land/t1/records.jsonl").read_text(encoding="utf-8"))["landed"])

    @unittest.skipIf(os.name == "nt", "the interrupted transport hook is a POSIX shell script")
    def test_failed_push_without_verifiable_remote_is_uncertain(self):
        import shlex

        project = LandProject(self)
        result = project.assess()
        moved_remote = project.work / "moved.git"
        hook = project.repo / ".git/hooks/pre-push"
        command = ["git", "-C", str(project.repo), "-c", "core.hooksPath=/dev/null", "push", "-q",
                   str(project.remote), f"{project.head}:refs/heads/main"]
        hook.write_bytes(("#!/bin/sh\n" + shlex.join(command) + "\n"
                          + shlex.join(["mv", str(project.remote), str(moved_remote)]) + "\nexit 1\n").encode("utf-8"))
        hook.chmod(0o755)
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
            land.record(project.state, result, "merge", pushed)
        self.assertEqual(git(moved_remote, "rev-parse", "main"), project.head)
        self.assertIsNone(pushed.landed, pushed.message)
        self.assertEqual(pushed.code, "OAH_LAND_OUTCOME_UNCERTAIN")
        self.assertIn("uncertain", pushed.message)
        saved = json.loads((project.state / "land/t1/records.jsonl").read_text(encoding="utf-8"))
        self.assertIsNone(saved["landed"])

    def test_literal_push_destination_does_not_enter_git_trace_logs(self):
        project = LandProject(self)
        trace = project.work / "git-trace.log"
        project.env["GIT_TRACE"] = str(trace)
        result = project.assess()
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertTrue(pushed.landed, pushed.message)
        self.assertNotIn(str(project.remote), trace.read_text(encoding="utf-8") if trace.exists() else "")

    def test_push_fast_forwards_exactly_head(self):
        project = LandProject(self)
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertEqual((pushed.landed, pushed.code), (True, "OAH_LAND_LANDED"), pushed.message)
        self.assertEqual(project.remote_main(), project.head)
        self.assertEqual(git(project.repo, "rev-parse", "feat"), project.head)

    def test_push_refuses_when_remote_moved_after_assessment(self):
        project = LandProject(self)
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        moved = project.advance_main({"docs/other.md": "landed elsewhere\n"})
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertEqual((pushed.landed, pushed.code), (False, "OAH_LAND_IMPACT_STALE"), pushed.message)
        self.assertIn(moved[:12], pushed.message)
        self.assertIn("nothing landed", pushed.message)
        self.assertEqual(project.remote_main(), moved)

    @unittest.skipIf(os.name == "nt", "the racing pre-receive hook is a POSIX shell script")
    def test_remote_enforces_the_assessed_sha_during_the_push(self):
        project = LandProject(self)
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        racer = git(project.repo, "commit-tree", "-p", project.base, "-m", "race", f"{project.base}^{{tree}}")
        git(project.repo, "push", "-q", str(project.remote), f"{racer}:refs/heads/racer")
        hook = project.remote / "hooks" / "pre-receive"
        script = f"#!/bin/sh\nunset GIT_QUARANTINE_PATH\ngit update-ref refs/heads/main {racer}\ncat >/dev/null\n"
        hook.write_text(script, encoding="utf-8")
        hook.chmod(0o755)
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertEqual((pushed.landed, pushed.code), (False, "OAH_LAND_IMPACT_STALE"), pushed.message)
        self.assertEqual(project.remote_main(), racer)

    def test_records_hold_shas_digests_and_codes_only(self):
        project = LandProject(self)
        result = project.assess()
        with project.patched():
            land.record(project.state, result, "check", None)
            pushed = land.push(project.repo, result, env=dict(project.env))
            land.record(project.state, result, "merge", pushed)
        path = project.state / "land" / "t1" / "records.jsonl"
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["mode"] for line in lines], ["check", "merge"])
        self.assertEqual([line["landed"] for line in lines], [False, True])
        first = lines[0]
        self.assertEqual((first["head"], first["default_branch"], first["impact_result"]),
                         (project.head, project.base, result.impact["result"]))
        self.assertEqual(first["required_checks"], [])
        self.assertEqual({item["name"] for item in first["checks"]}, set(land.CHECKS))
        self.assertTrue(all(set(item) == {"name", "state", "code"} for item in first["checks"]))
        raw = path.read_text(encoding="utf-8")
        for private in (str(project.remote), str(project.repo), str(project.work)):
            self.assertNotIn(private, raw)
        self.assertNotIn("message", raw)


if __name__ == "__main__":
    unittest.main()
