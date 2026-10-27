# oah land 명령 검사 — 실제 명령 탐색으로 검사 출력·종료 코드, 확인 없는 병합 거부, 확인 중 원격 이동, 바뀐 기본 브랜치, 비밀·계정 정보 누출 없음, 사용법 오류를 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import secrets
import unittest
from unittest.mock import patch

from oah import land, land_cli
from oah.cli import build_parser
from tests.test_land import OTHER_LEDGER, LandProject, git, git_env


def land_command(project: LandProject, *args: str, stdin: str | None = None, interactive: bool = False,
                 ask=None):
    """`oah land ...` through the real command discovery, with the project's fake gh and gitleaks."""
    with patch.object(land, "find_gh", lambda env: project.gh_argv), \
            patch.object(land, "find_gitleaks", lambda env: project.gitleaks_argv), \
            patch.object(land_cli, "_interactive", lambda: interactive):
        if ask is not None:
            with patch.object(land_cli, "_ask", ask):
                return project.oah("land", *args, stdin=stdin)
        return project.oah("land", *args, stdin=stdin)


def records(project: LandProject) -> list[dict]:
    path = project.state / "land" / "t1" / "records.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class LandCliTest(unittest.TestCase):
    def test_ci_disappears_after_confirmation_updates_json_and_record(self):
        project = LandProject(self)

        def confirm(_prompt):
            project.gh_argv = None
            return "y\n"

        done = land_command(project, "--merge", "--task", "t1", "--json", interactive=True, ask=confirm)
        body = json.loads(done.stdout)
        self.assertEqual((done.returncode, body["code"]), (1, "OAH_LAND_CI_UNVERIFIED"))
        final = body["assessment"]
        self.assertEqual((final["ready"], final["code"]), (False, "OAH_LAND_CI_UNVERIFIED"))
        for saved in (final, records(project)[-1]):
            self.assertFalse(saved["ready"])
            self.assertEqual(next(c for c in saved["checks"] if c["name"] == "ci")["state"], "unverified")
        self.assertEqual(project.remote_main(), project.base)

    def test_excepted_ci_transitions_after_confirmation_update_json_and_record(self):
        for change, state, code in (("pending", "failed", "OAH_LAND_CI_PENDING"),
                                     ("failure", "failed", "OAH_LAND_CI_FAILED"),
                                     ("unverified", "unverified", "OAH_LAND_AUTHORIZATION_CHANGED"),
                                     ("success", "passed", "OAH_LAND_AUTHORIZATION_CHANGED")):
            with self.subTest(change=change):
                project = LandProject(self)
                project.gh_argv = None

                def confirm(_prompt):
                    project.gh_argv = (sys.executable, str(pathlib.Path(__file__).parent / "fakes/fake_gh.py"))
                    project.ci([], statuses=[] if change == "unverified" else [{"context": "ci", "state": change}])
                    return "y\n"

                done = land_command(project, "--merge", "--task", "t1", "--json", "--accept-unverified-ci",
                                    "no CI yet", interactive=True, ask=confirm)
                body = json.loads(done.stdout)
                self.assertEqual((done.returncode, body["code"]), (1, code), done.stdout + done.stderr)
                for saved in (body["assessment"], records(project)[-1]):
                    self.assertEqual((saved["ready"], saved["code"]), (False, code))
                    self.assertEqual(next(c for c in saved["checks"] if c["name"] == "ci")["state"], state)
                self.assertEqual(project.remote_main(), project.base)

    def test_exception_record_failure_prevents_push(self):
        for failure in ("corrupt", "directory"):
            with self.subTest(failure=failure):
                project = LandProject(self)
                project.gh_argv = None
                path = project.state / "land/t1/ci-exceptions.jsonl"
                path.parent.mkdir(parents=True)
                if failure == "corrupt":
                    path.write_bytes(b"{broken\n")
                else:
                    path.mkdir()  # portable write failure, also on Windows and privileged users
                done = land_command(project, "--merge", "--task", "t1", "--json", "--confirm", project.head,
                                    "--accept-unverified-ci", "no CI yet")
                body = json.loads(done.stdout)
                self.assertEqual((done.returncode, body["code"]), (1, "OAH_LAND_CI_EXCEPTION_RECORD_FAILED"))
                self.assertFalse(body["assessment"]["ready"])
                self.assertEqual(project.remote_main(), project.base)
                if failure == "corrupt":
                    self.assertEqual(path.read_bytes(), b"{broken\n")

    def test_branch_cannot_loosen_its_own_landing_policy(self):
        for target, proposed in (("", '[land]\nci_unverified = "exception"\n'),
                                  ('[land]\nrequired_checks = ["tests", "build"]\n',
                                   '[land]\nrequired_checks = ["tests"]\n')):
            with self.subTest(proposed=proposed):
                project = LandProject(self, config=target, work_files={".oah/config.toml": proposed})
                project.ci([])
                done = land_command(project, "--merge", "--task", "t1", "--json", "--confirm", project.head,
                                    "--accept-unverified-ci", "bootstrap")
                body = json.loads(done.stdout)
                self.assertEqual((done.returncode, body["code"]), (1, "OAH_LAND_POLICY_LOOSENED"))
                expected = {"target": project.base, "changed": True, "loosened": True}
                self.assertEqual(body["assessment"]["land_policy"], expected)
                self.assertEqual(records(project)[-1]["land_policy"], expected)
                ci = next(c for c in body["assessment"]["checks"] if c["name"] == "ci")
                self.assertEqual(ci["detail"]["required"], ["tests", "build"] if target else [])
                self.assertEqual(project.remote_main(), project.base)
                text = land_command(project, "--check", "--task", "t1")
                self.assertIn("OAH_LAND_POLICY_CHANGED", text.stdout)

    @unittest.skipIf(os.name == "nt", "the git proxy executable fixture uses a POSIX shebang")
    def test_git_proxy_rerouting_during_confirmation_updates_neither_repository(self):
        project = LandProject(self)
        other = project.work / "other.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(project.remote), str(other)],
                       check=True, env=git_env())
        driver = pathlib.Path(str(project.remote) + ".proxy.py")
        other_driver = pathlib.Path(str(other) + ".proxy.py")
        script = (f"#!{sys.executable}\n"
            "# 로컬 Git 프록시 시험 — 네트워크 없이 요청을 실제 bare 저장소 서비스에 연결한다.\n"
            "import os, subprocess, sys\n"
            "def read_exact(size):\n"
            "    data = b''\n"
            "    while len(data) < size:\n"
            "        part = os.read(0, size - len(data))\n"
            "        if not part:\n"
            "            sys.exit(1)\n"
            "        data += part\n"
            "    return data\n"
            "request = read_exact(int(read_exact(4), 16) - 4)\n"
            "service = request.split(b' ', 1)[0].decode('ascii').removeprefix('git-')\n"
            "env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}\n"
            "sys.exit(subprocess.run(['git', service, sys.argv[0].removesuffix('.proxy.py')], env=env).returncode)\n"
        ).encode("utf-8")
        for proxy in (driver, other_driver):
            proxy.write_bytes(script)
            proxy.chmod(0o755)
        git(project.repo, "remote", "set-url", "origin", "git://oah-proxy.invalid/fixture")
        git(project.repo, "config", "core.gitProxy", str(driver))
        confirmed = []

        def reroute(_prompt):
            confirmed.append(True)
            git(project.repo, "config", "core.gitProxy", str(other_driver))
            return "y\n"

        done = land_command(project, "--merge", "--task", "t1", "--json", interactive=True, ask=reroute)
        self.assertTrue(confirmed, done.stdout + done.stderr)
        self.assertEqual(project.remote_main(), project.base)
        self.assertEqual(git(other, "rev-parse", "main"), project.base)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        body = json.loads(done.stdout)
        self.assertEqual(body["code"], "OAH_LAND_DESTINATION_CHANGED")
        self.assertRegex(body["assessment"]["git_context"], r"^[0-9a-f]{64}$")
        self.assertEqual(records(project)[-1]["git_context"], body["assessment"]["git_context"])
        emitted = done.stdout + done.stderr + json.dumps(records(project))
        for private in (str(project.remote), str(other), str(driver), "oah-proxy.invalid"):
            self.assertNotIn(private, emitted)

    def test_non_ssh_config_changes_during_confirmation_refuse_privately(self):
        for setting in ("core.gitProxy", "http.proxy", "http.https://example.invalid/.proxy", "http.noProxy"):
            with self.subTest(setting=setting):
                project = LandProject(self)
                other = project.work / "other.git"
                subprocess.run(["git", "clone", "--bare", "-q", str(project.remote), str(other)],
                               check=True, env=git_env())
                private = secrets.token_hex(24)

                def reroute(_prompt):
                    git(project.repo, "config", setting, private)
                    return "y\n"

                done = land_command(project, "--merge", "--task", "t1", "--json", interactive=True, ask=reroute)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(json.loads(done.stdout)["code"], "OAH_LAND_DESTINATION_CHANGED")
                self.assertEqual(project.remote_main(), project.base)
                self.assertEqual(git(other, "rev-parse", "main"), project.base)
                self.assertNotIn(private, done.stdout + done.stderr + json.dumps(records(project)))

    def test_proxy_environment_changes_during_confirmation_refuse_privately(self):
        project = LandProject(self)
        for setting in ("GIT_PROXY_COMMAND", "http_proxy", "HTTPS_PROXY", "All_Proxy", "nO_pRoXy"):
            with self.subTest(setting=setting):
                private = secrets.token_hex(24)

                def reroute(_prompt):
                    os.environ[setting] = private
                    return "y\n"

                done = land_command(project, "--merge", "--task", "t1", "--json", interactive=True, ask=reroute)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(json.loads(done.stdout)["code"], "OAH_LAND_DESTINATION_CHANGED")
                self.assertEqual(project.remote_main(), project.base)
                self.assertNotIn(private, done.stdout + done.stderr + json.dumps(records(project)))

    def test_ssh_transport_changes_during_confirmation_refuse(self):
        for setting in ("GIT_SSH", "GIT_SSH_COMMAND", "core.sshCommand"):
            with self.subTest(setting=setting):
                project = LandProject(self)
                private = secrets.token_hex(24)

                def change_transport(_prompt):
                    if setting.startswith("GIT_"):
                        os.environ[setting] = private
                    else:
                        git(project.repo, "config", setting, private)
                    return "y\n"

                done = land_command(project, "--merge", "--task", "t1", "--json", interactive=True,
                                    ask=change_transport)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(json.loads(done.stdout)["code"], "OAH_LAND_DESTINATION_CHANGED")
                self.assertNotIn(private, done.stdout + done.stderr + json.dumps(records(project)))
                self.assertEqual(project.remote_main(), project.base)

    @unittest.skipIf(os.name == "nt", "the redirecting receive-pack command uses POSIX shell quoting")
    def test_receivepack_cannot_redirect_the_confirmed_push(self):
        for after_confirmation in (False, True):
            with self.subTest(after_confirmation=after_confirmation):
                project = LandProject(self)
                other = project.work / ("private-" + secrets.token_hex(12) + ".git")
                subprocess.run(["git", "clone", "--bare", "-q", str(project.remote), str(other)],
                               check=True, env=git_env())

                def configure(_prompt=None):
                    git(project.repo, "config", "remote.origin.receivepack",
                        f"git-receive-pack {shlex.quote(str(other))} #")
                    return "y\n"

                if not after_confirmation:
                    configure()
                done = land_command(project, "--merge", "--task", "t1", "--json", interactive=True,
                                    ask=configure if after_confirmation else lambda _: "y\n")
                self.assertEqual(done.returncode, 1)
                self.assertEqual(json.loads(done.stdout)["code"], "OAH_LAND_DESTINATION_UNSUPPORTED"
                                 if after_confirmation else "OAH_LAND_IMPACT_STALE")
                self.assertIn("OAH_LAND_DESTINATION_UNSUPPORTED", done.stderr)
                self.assertEqual(project.remote_main(), project.base)
                self.assertEqual(git(other, "rev-parse", "main"), project.base)
                emitted = done.stdout + done.stderr + json.dumps(records(project))
                self.assertFalse(str(other) in emitted, "receiver path entered landing output or records")

    def test_remote_transport_settings_added_during_confirmation_refuse(self):
        project = LandProject(self)
        for key, value in (("receivepack", "git-receive-pack"), ("vcs", "git")):
            with self.subTest(key=key):
                def confirm(_prompt):
                    git(project.repo, "config", f"remote.origin.{key}", value)
                    return "y\n"

                done = land_command(project, "--merge", "--task", "t1", interactive=True, ask=confirm)
                self.assertEqual(done.returncode, 1)
                self.assertIn("OAH_LAND_DESTINATION_UNSUPPORTED", done.stderr)
                self.assertEqual(project.remote_main(), project.base)
                git(project.repo, "config", "--unset", f"remote.origin.{key}")

    def test_url_named_remote_added_during_confirmation_refuses_without_disclosure(self):
        project = LandProject(self)

        def confirm(_prompt):
            git(project.repo, "config", f"remote.{project.remote}.pushurl", str(project.work / "other.git"))
            return "y\n"

        done = land_command(project, "--merge", "--task", "t1", interactive=True, ask=confirm)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("OAH_LAND_DESTINATION_UNSUPPORTED", done.stderr)
        self.assertIn("remote names without", done.stderr)
        self.assertNotIn(str(project.remote), done.stdout + done.stderr)
        self.assertEqual(project.remote_main(), project.base)

    def test_required_gate_labels_and_paths_are_private_in_all_exports(self):
        private = "account-" + secrets.token_hex(16)
        target = f"src/{private}/app.py"
        ledger = f".oah/tasks/{private}/GATES.md"
        project = LandProject(self, main_files={ledger: OTHER_LEDGER.replace("src/app.py", target), target: "v0\n"},
                              work_files={target: "v1\n"})
        result = project.assess()
        self.assertEqual(result.check("required-checks").code, "OAH_LAND_CHECKS_REQUIRED")
        digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for flags in ((), ("--json",)):
            done = land_command(project, "--check", "--task", "t1", *flags)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            emitted = done.stdout + done.stderr + json.dumps(result.to_json()) + json.dumps(records(project))
            self.assertNotIn(private, emitted)
            self.assertNotIn(ledger, emitted)
            self.assertNotIn(target, emitted)
            self.assertIn(digest(ledger + "#G1"), emitted)
            self.assertIn(digest(target), emitted)
        item = result.to_json()["required_checks"][0]
        self.assertEqual(item["position"], 1)
        self.assertEqual(item["gate"], {"position": 1, "digest": digest(ledger + "#G1")})
        self.assertIn({"position": 1, "digest": digest(target)}, item["targets"])
        self.assertEqual(records(project)[-1]["required_checks"], result.to_json()["required_checks"])

    def test_destination_and_confirmation_values_stay_private(self):
        project = LandProject(self)
        remote, branch = ("account-" + secrets.token_hex(16), "branch-" + secrets.token_hex(16))
        git(project.repo, "remote", "rename", "origin", remote)
        git(project.repo, "config", "branch.feat.remote", remote)
        git(project.repo, "push", "-q", remote, f"{project.base}:refs/heads/{branch}")
        git(project.remote, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        private = "github" + "_pat_" + secrets.token_hex(16)
        outputs = []
        for flags in ((), ("--json",)):
            outputs.append(land_command(project, "--check", "--task", "t1", *flags))
            outputs.append(land_command(project, "--merge", "--task", "t1", "--confirm", private, *flags))
            self.assertEqual(outputs[-2].returncode, 0, outputs[-2].stdout + outputs[-2].stderr)
            self.assertEqual(outputs[-1].returncode, 1)

        def answer(prompt):
            outputs.append(prompt)
            return "y\n"

        landed = land_command(project, "--merge", "--task", "t1", interactive=True, ask=answer)
        self.assertEqual(landed.returncode, 0, landed.stdout + landed.stderr)
        emitted = "".join(item if isinstance(item, str) else item.stdout + item.stderr for item in outputs)
        emitted += landed.stdout + landed.stderr + json.dumps(records(project))
        for value in (remote, branch, private, str(project.remote)):
            self.assertNotIn(value, emitted)
        for value in (remote, branch):
            self.assertIn(hashlib.sha256(value.encode("utf-8")).hexdigest(), emitted)
        self.assertEqual(git(project.remote, "rev-parse", branch), project.head)

    def test_gate_errors_and_tool_installation_details_stay_private(self):
        project = LandProject(self)
        private = "account-" + secrets.token_hex(16)
        token = "gh" + "p_" + secrets.token_hex(18)
        for problem in ("runtime", "unavailable", "unpinned"):
            with self.subTest(problem=problem):
                if problem == "runtime":
                    project.stub.reply("gate-check.mjs", {"stdout": private, "stderr": token, "code": 2})
                else:
                    folder = project.work / private
                    folder.mkdir(exist_ok=True)
                    project.env["OAH_UNLAZY_DIR"] = str(folder)
                    if problem == "unpinned":
                        (folder / "scripts").mkdir(exist_ok=True)
                        (folder / "scripts/gate-check.mjs").write_bytes(b"// unpinned\n")
                for flags in ((), ("--json",)):
                    done = land_command(project, "--check", "--task", "t1", *flags)
                    self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                    self.assertIn("OAH_LAND_GATES_UNMET", done.stdout + done.stderr)
                    emitted = done.stdout + done.stderr + json.dumps(records(project))
                    for value in (private, token, str(project.work), str(project.home)):
                        self.assertNotIn(value, emitted)
        self.assertEqual(project.remote_main(), project.base)

    def test_environment_exceptions_and_record_warnings_stay_private(self):
        project = LandProject(self)
        private = "account-" + secrets.token_hex(16)
        project.commit({".oah/config.toml": "[" + private + "\n"})
        for flags in ((), ("--json",)):
            done = land_command(project, "--check", "--task", "t1", *flags)
            self.assertEqual(done.returncode, 69, done.stdout + done.stderr)
            for value in (private, str(project.repo)):
                self.assertNotIn(value, done.stdout + done.stderr)
        project = LandProject(self)
        # Real record I/O failure: the parent that should be a directory is a private file.
        (project.state / "land").write_bytes(private.encode("utf-8"))
        for flags in ((), ("--json",)):
            done = land_command(project, "--check", "--task", "t1", *flags)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("OAH_LAND_RECORD_FAILED", done.stderr)
            for value in (private, str(project.state)):
                self.assertNotIn(value, done.stdout + done.stderr)

    def test_private_ci_names_never_reach_text_json_or_records(self):
        project = LandProject(self)
        private = ["gh" + "p_" + secrets.token_hex(18), "account-" + secrets.token_hex(18)]
        for status, conclusion, code in (("completed", "failure", "OAH_LAND_CI_FAILED"),
                                         ("in_progress", None, "OAH_LAND_CI_PENDING")):
            project.ci([{"name": value, "status": status, "conclusion": conclusion} for value in private])
            for flags in ((), ("--json",)):
                with self.subTest(status=status, flags=flags):
                    done = land_command(project, "--merge", "--task", "t1", "--confirm", project.head, *flags)
                    self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                    self.assertIn(code, done.stdout + done.stderr)
                    self.assertIn("positions 1, 2", done.stdout + done.stderr)
                    emitted = done.stdout + done.stderr + json.dumps(records(project))
                    for value in private:
                        self.assertNotIn(value, emitted)
        self.assertEqual(project.remote_main(), project.base)

    def test_confirmation_revalidates_local_authorization(self):
        from oah.review import receipts

        for change in ("lease", "review", "intent", "gates", "new-approval"):
            with self.subTest(change=change):
                project = LandProject(self)

                def answer(_prompt):
                    if change == "lease":
                        done = project.oah("start", "--task", "t1", "--agent", "claude", "--packet",
                                           ".oah/tasks/t1/PACKET.md")
                        self.assertEqual(done.returncode, 0, done.stderr)
                    elif change == "review":
                        receipts.log_round(project.state, "t1", {"event": "start", "round": 2, "series_round": 2,
                                           "attempt": 1, "run_id": "later", "base": project.base, "head": project.head})
                    elif change == "intent":
                        path = project.repo / ".oah/tasks/t1/PACKET.md"
                        path.write_bytes(path.read_bytes().replace(b"Revision: 1", b"Revision: 2"))
                    elif change == "gates":
                        project.ledger.write_bytes(project.ledger.read_bytes().replace(b"[x]", b"[ ]"))
                    else:
                        from tests.test_review_summary import publish_receipt
                        publish_receipt(project, 2)
                    return "y\n"

                done = land_command(project, "--merge", "--task", "t1", interactive=True, ask=answer)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(project.remote_main(), project.base)
                if change == "review":
                    self.assertIn("OAH_LAND_REVIEW_REQUIRED", done.stderr)

    def test_confirmation_rejects_new_index_flags_without_changing_index(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            for output in ((), ("--json",)):
                with self.subTest(flag=flag, output=output):
                    project = LandProject(self)
                    index = project.repo / ".git/index"
                    before = []

                    def answer(_prompt):
                        git(project.repo, "update-index", flag, "--", "src/app.py")
                        before.append(index.read_bytes())
                        return "y\n"

                    done = land_command(project, "--merge", "--task", "t1", *output,
                                        interactive=True, ask=answer)
                    self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                    self.assertIn("OAH_LAND_AUTHORIZATION_CHANGED", done.stderr)
                    self.assertEqual(project.remote_main(), project.base)
                    self.assertEqual(index.read_bytes(), before[0])

    @unittest.skipIf(os.name == "nt", "the pre-push hook fixture uses a POSIX shell")
    def test_push_hook_cannot_leak_oah_prefixed_stderr(self):
        import shlex

        project = LandProject(self)
        marker = secrets.token_hex(24)
        hook = project.repo / ".git/hooks/pre-push"
        hook.write_bytes(("#!/bin/sh\nprintf '%s\\n' " + shlex.quote("OAH_PUSH_REFUSED: " + marker)
                          + " >&2\nexit 1\n").encode("utf-8"))
        hook.chmod(0o755)
        for output in ((), ("--json",)):
            done = land_command(project, "--merge", "--task", "t1", "--confirm", project.head, *output)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertNotIn(marker, done.stdout + done.stderr + json.dumps(records(project)))
            self.assertIn("git exited 1", done.stdout + done.stderr)
        self.assertEqual(project.remote_main(), project.base)

    @unittest.skipIf(os.name == "nt", "the pre-push hook fixture uses a POSIX shell")
    def test_authorization_lock_is_held_through_push(self):
        import shlex

        project = LandProject(self)
        probe = project.work / "probe.py"
        observed = project.work / "observed"
        probe.write_text("# 상태 잠금 관찰용 시험 프로세스.\nfrom pathlib import Path\nfrom oah.lockfile import StateLock, LockTimeout\n"
                         + "try:\n    with StateLock(Path(" + repr(str(project.state / ".lock"))
                         + "), timeout=0.1):\n        result = 'unlocked'\n"
                         + "except LockTimeout:\n    result = 'locked'\n"
                         + "Path(" + repr(str(observed)) + ").write_text(result, encoding='utf-8')\n",
                         encoding="utf-8")
        hook = project.repo / ".git/hooks/pre-push"
        hook.write_bytes(("#!/bin/sh\n" + shlex.join([sys.executable, str(probe)]) + "\n").encode("utf-8"))
        hook.chmod(0o755)
        done = land_command(project, "--merge", "--task", "t1", "--confirm", project.head)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(observed.read_text(encoding="utf-8"), "locked")

    def test_linked_authorization_state_refuses_without_exposing_paths(self):
        from tests.helpers import directory_alias

        project = LandProject(self)
        folder = project.state / "reviews/t1"
        saved = project.work / ("private-" + secrets.token_hex(12))
        folder.rename(saved)
        directory_alias(saved, folder)
        done = land_command(project, "--check", "--task", "t1", "--json")
        self.assertEqual(done.returncode, 69, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout)["code"], "OAH_RUN_PATH_UNSAFE")
        self.assertNotIn(str(saved), done.stdout + done.stderr)
        self.assertNotIn(str(project.state), done.stdout + done.stderr)

    def test_land_is_discovered(self):
        self.assertIn("land", build_parser()._subparsers._group_actions[0].choices)

    def test_check_prints_every_check_and_exits_zero(self):
        project = LandProject(self)
        done = land_command(project, "--check", "--task", "t1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual([line.split(":")[0] for line in lines[:len(land.CHECKS)]], list(land.CHECKS))
        self.assertTrue(all(": passed — " in line for line in lines[:len(land.CHECKS)]), done.stdout)
        self.assertTrue(lines[-1].startswith(f"OAH_LAND_READY: the task can land: HEAD {project.head} fast-forwards destination"), lines[-1])
        self.assertIn("oah land --merge --task <task>", lines[-1])
        self.assertEqual(done.stderr, "")
        self.assertEqual(project.remote_main(), project.base)  # a check never pushes
        shown = land_command(project, "--check", "--task", "t1", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        data = json.loads(shown.stdout)
        self.assertEqual((data["code"], data["exit_code"]), ("OAH_LAND_READY", 0))
        self.assertEqual(data["assessment"]["head"], project.head)
        self.assertEqual(data["assessment"]["default_branch"], project.base)
        self.assertRegex(data["assessment"]["impact"]["result"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual([check["state"] for check in data["assessment"]["checks"]], ["passed"] * len(land.CHECKS))
        self.assertIsNone(data["pushed"])
        self.assertEqual([item["mode"] for item in records(project)], ["check", "check"])

    def test_check_refusal_names_code_and_next_step(self):
        project = LandProject(self)
        project.ci([{"name": "tests", "status": "completed", "conclusion": "failure"}])
        done = land_command(project, "--check", "--task", "t1")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(done.stderr.startswith("OAH_LAND_CI_FAILED: 1 CI check run(s) failed for HEAD"), done.stderr)
        self.assertIn("then run oah land --check again", done.stderr)
        self.assertIn("ci: failed OAH_LAND_CI_FAILED — ", done.stdout)
        offline = land_command(project, "--check", "--task", "t1", "--offline", "--json")
        self.assertEqual(offline.returncode, 1)
        self.assertEqual(json.loads(offline.stdout)["code"], "OAH_LAND_IMPACT_STALE")
        self.assertTrue(offline.stderr.startswith("OAH_LAND_IMPACT_STALE:"), offline.stderr)

    def test_unverified_ci_refuses_check_and_merge_with_named_code(self):
        project = LandProject(self)
        for setup in (lambda: project.ci([]), lambda: setattr(project, "gh_argv", None)):
            setup()
            done = land_command(project, "--check", "--task", "t1")
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertTrue(done.stderr.startswith("OAH_LAND_CI_UNVERIFIED: "), done.stderr)
            self.assertIn('--accept-unverified-ci "<reason>"', done.stderr)
            self.assertIn("ci: unverified OAH_LAND_CI_UNVERIFIED — ", done.stdout)
            self.assertNotIn("OAH_LAND_READY", done.stdout + done.stderr)
            shown = json.loads(land_command(project, "--check", "--task", "t1", "--json").stdout)
            self.assertEqual((shown["code"], shown["exit_code"], shown["assessment"]["ready"]),
                             ("OAH_LAND_CI_UNVERIFIED", 1, False))
            merged = land_command(project, "--merge", "--task", "t1", "--confirm", project.head)
            self.assertEqual(merged.returncode, 1, merged.stdout + merged.stderr)
            self.assertTrue(merged.stderr.startswith("OAH_LAND_CI_UNVERIFIED: "), merged.stderr)
            self.assertIn("Nothing was pushed.", merged.stderr)
            self.assertEqual(project.remote_main(), project.base)
        self.assertIn("gh auth login", done.stderr)
        self.assertEqual({(item["code"], item["ready"]) for item in records(project)},
                         {("OAH_LAND_CI_UNVERIFIED", False)})

    def test_accept_unverified_ci_lands_with_distinct_code_and_record(self):
        project = LandProject(self)
        git(project.repo, "config", "user.name", "Fake Lander")
        project.gh_argv = None
        reason = ("--accept-unverified-ci", "CI is not set up yet")
        done = land_command(project, "--check", "--task", "t1", *reason)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("ci: unverified OAH_LAND_CI_UNVERIFIED — ", done.stdout)
        last = done.stdout.splitlines()[-1]
        self.assertTrue(last.startswith("OAH_LAND_READY_WITH_CI_EXCEPTION: "), last)
        self.assertIn("unverified, not passed", last)
        self.assertNotIn("OAH_LAND_READY:", done.stdout)
        shown = json.loads(land_command(project, "--check", "--task", "t1", "--json", *reason).stdout)
        self.assertEqual(shown["code"], "OAH_LAND_READY_WITH_CI_EXCEPTION")
        self.assertEqual([c["state"] for c in shown["assessment"]["checks"] if c["name"] == "ci"], ["unverified"])
        prompts = []

        def decline(prompt):
            prompts.append(prompt)
            return "n\n"

        land_command(project, "--merge", "--task", "t1", *reason, interactive=True, ask=decline)
        self.assertIn("recorded CI exception", prompts[0])
        landed = land_command(project, "--merge", "--task", "t1", "--confirm", project.head, "--json", *reason)
        self.assertEqual(landed.returncode, 0, landed.stdout + landed.stderr)
        self.assertEqual(json.loads(landed.stdout)["code"], "OAH_LAND_LANDED_WITH_CI_EXCEPTION")
        self.assertEqual(project.remote_main(), project.head)
        saved = records(project)
        self.assertEqual([item["code"] for item in saved],
                         ["OAH_LAND_READY_WITH_CI_EXCEPTION"] * 2 + ["OAH_LAND_CANCELLED",
                                                                    "OAH_LAND_LANDED_WITH_CI_EXCEPTION"])
        self.assertTrue(all([c["state"] for c in item["checks"] if c["name"] == "ci"] == ["unverified"]
                            for item in saved))
        path = project.state / "land" / "t1" / "ci-exceptions.jsonl"
        noted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(item["mode"], item["code"]) for item in noted],
                         [("check", "OAH_LAND_READY_WITH_CI_EXCEPTION")] * 2
                         + [("merge", "OAH_LAND_CANCELLED"), ("merge", "OAH_LAND_READY_WITH_CI_EXCEPTION"),
                            ("merge", "OAH_LAND_LANDED_WITH_CI_EXCEPTION")])
        self.assertEqual({(item["reason"], item["head"], item["user"]) for item in noted},
                         {("CI is not set up yet", project.head, "Fake Lander")})

    def test_accept_unverified_ci_needs_a_reason(self):
        project = LandProject(self)
        project.gh_argv = None
        for args in (("--accept-unverified-ci", ""), ("--accept-unverified-ci", "  \t"), ("--accept-unverified-ci",)):
            with self.subTest(args=args):
                done = land_command(project, "--check", "--task", "t1", *args)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertTrue(done.stderr.startswith("OAH_USAGE: "), done.stderr)
        self.assertIn("non-empty reason", land_command(project, "--check", "--task", "t1",
                                                       "--accept-unverified-ci", " ").stderr)
        self.assertEqual(records(project), [])

    def test_accept_unverified_ci_does_not_override_failed_or_pending(self):
        project = LandProject(self)
        for runs, code in (([{"name": "tests", "status": "completed", "conclusion": "failure"}], "OAH_LAND_CI_FAILED"),
                           ([{"name": "tests", "status": "in_progress", "conclusion": None}], "OAH_LAND_CI_PENDING")):
            project.ci(runs)
            done = land_command(project, "--merge", "--task", "t1", "--confirm", project.head,
                                "--accept-unverified-ci", "CI is flaky")
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertTrue(done.stderr.startswith(code + ": "), done.stderr)
            self.assertEqual(project.remote_main(), project.base)
        self.assertFalse((project.state / "land" / "t1" / "ci-exceptions.jsonl").exists())

    def test_invalid_land_config_exits_69(self):
        project = LandProject(self, config='[land]\nci_unverified = "warn"\n')
        done = land_command(project, "--check", "--task", "t1")
        self.assertEqual(done.returncode, 69, done.stdout + done.stderr)
        self.assertTrue(done.stderr.startswith("OAH_CONFIG_INVALID: "), done.stderr)

    def test_merge_moved_branch_lands_nothing(self):
        project = LandProject(self)
        checked = land_command(project, "--check", "--task", "t1")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        moved = project.advance_main({"docs/other.md": "landed elsewhere\n"})
        done = land_command(project, "--merge", "--task", "t1", "--confirm", project.head)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(done.stderr.startswith("OAH_LAND_IMPACT_STALE:"), done.stderr)
        self.assertIn(f"is at {moved[:12]}, which HEAD does not contain", done.stderr)
        self.assertIn("Nothing was pushed.", done.stderr)
        self.assertEqual(project.remote_main(), moved)
        merge = records(project)[-1]
        self.assertEqual((merge["mode"], merge["landed"], merge["default_branch"], merge["code"]),
                         ("merge", False, moved, "OAH_LAND_IMPACT_STALE"))

    def test_merge_race_during_confirmation_lands_nothing_and_retry_reassesses(self):
        project = LandProject(self)
        landed: list[str] = []

        def racing_answer(prompt: str) -> str:
            self.assertIn(f"push HEAD {project.head} to destination", prompt)
            landed.append(project.advance_main({".oah/tasks/x/GATES.md": OTHER_LEDGER}))
            return "y\n"

        done = land_command(project, "--merge", "--task", "t1", interactive=True, ask=racing_answer)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(done.stderr.startswith("OAH_LAND_IMPACT_STALE: destination"), done.stderr)
        self.assertIn(f"moved to {landed[0][:12]} while landing", done.stderr)
        self.assertIn("nothing landed", done.stderr)
        self.assertEqual(project.remote_main(), landed[0])
        self.assertEqual(records(project)[-1]["landed"], False)
        retry = land_command(project, "--merge", "--task", "t1", "--confirm", project.head)
        self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
        self.assertTrue(retry.stderr.startswith("OAH_LAND_CHECKS_REQUIRED:"), retry.stderr)
        self.assertIn(hashlib.sha256(b".oah/tasks/x/GATES.md#G1").hexdigest(), retry.stdout)
        self.assertIn("): pending", retry.stdout)
        self.assertEqual(records(project)[-1]["default_branch"], landed[0])
        self.assertEqual(project.remote_main(), landed[0])

    def test_merge_pushes_parent_with_recurse_submodules_only(self):
        project = LandProject(self)
        git(project.repo, "config", "push.recurseSubmodules", "only")
        done = land_command(project, "--merge", "--task", "t1", "--confirm", project.head, "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(project.remote_main(), project.head)
        body = json.loads(done.stdout)
        self.assertEqual((body["code"], body["pushed"]["landed"], body["pushed"]["remote_sha"]),
                         ("OAH_LAND_LANDED", True, project.head))
        self.assertEqual((records(project)[-1]["landed"], records(project)[-1]["remote_sha"]),
                         (True, project.head))

    @unittest.skipIf(os.name == "nt", "the post-receive hook fixture uses a POSIX shell")
    def test_zero_exit_push_requires_remote_at_exact_head(self):
        for outcome in ("rewound", "descendant"):
            with self.subTest(outcome=outcome):
                project = LandProject(self)
                expected = project.base
                if outcome == "descendant":
                    expected = git(project.repo, "commit-tree", "-p", project.head, "-m", "later landing",
                                   f"{project.head}^{{tree}}")
                    git(project.repo, "push", "-q", "origin", f"{expected}:refs/heads/fixture")
                hook = project.remote / "hooks/post-receive"
                hook.write_bytes((f"#!/bin/sh\ngit update-ref refs/heads/main {expected}\n").encode("utf-8"))
                hook.chmod(0o755)
                done = land_command(project, "--merge", "--task", "t1", "--confirm", project.head, "--json")
                self.assertEqual(project.remote_main(), expected)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                body = json.loads(done.stdout)
                self.assertEqual((body["code"], body["pushed"]["landed"], body["pushed"]["remote_sha"]),
                                 ("OAH_LAND_PUSH_FAILED", False, expected))
                saved = records(project)[-1]
                self.assertEqual((saved["code"], saved["landed"], saved["remote_sha"]),
                                 ("OAH_LAND_PUSH_FAILED", False, expected))

    def test_merge_requires_confirmation(self):
        project = LandProject(self)
        cases = [(("--merge", "--task", "t1"), {}, "OAH_LAND_CONFIRM_REQUIRED"),
                 (("--merge", "--task", "t1", "--confirm", "f" * 40), {}, "OAH_LAND_CONFIRM_MISMATCH"),
                 (("--merge", "--task", "t1", "--confirm", project.head[:12]), {}, "OAH_LAND_CONFIRM_MISMATCH"),
                 (("--merge", "--task", "t1"), {"interactive": True, "stdin": "n\n"}, "OAH_LAND_CANCELLED"),
                 (("--merge", "--task", "t1"), {"interactive": True, "stdin": ""}, "OAH_LAND_CANCELLED")]
        for args, options, code in cases:
            with self.subTest(args=args, options=options):
                done = land_command(project, *args, **options)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertTrue(done.stderr.splitlines()[-1].startswith(code + ":"), done.stderr)
                self.assertIn("nothing was pushed", done.stderr)
                self.assertEqual(project.remote_main(), project.base)
        self.assertIn(f"--confirm {project.head}", land_command(project, "--merge", "--task", "t1").stderr)
        self.assertEqual([item["landed"] for item in records(project)], [False] * (len(cases) + 1))
        done = land_command(project, "--merge", "--task", "t1", interactive=True, stdin="y\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Land it? [y/N]", done.stderr)
        self.assertTrue(done.stdout.splitlines()[-1].startswith("OAH_LAND_LANDED: destination"), done.stdout)
        self.assertEqual(project.remote_main(), project.head)
        self.assertEqual((records(project)[-1]["landed"], records(project)[-1]["remote_sha"]), (True, project.head))
        again = land_command(project, "--merge", "--task", "t1", "--confirm", project.head)
        self.assertEqual(again.returncode, 1)
        self.assertTrue(again.stderr.startswith("OAH_LAND_NOTHING_TO_LAND:"), again.stderr)

    def test_land_output_and_records_hold_no_credentials(self):
        project = LandProject(self)
        tokens = ["gh" + "p_" + secrets.token_hex(18), "github" + "_pat_" + secrets.token_hex(16)]
        project.env.update(GH_TOKEN=tokens[0], GITHUB_TOKEN=tokens[1])
        outputs = []
        project.ci(exit=1, stderr=f"HTTP 401: Bad credentials {tokens[0]} for decoy-owner-login\n")
        outputs.append(land_command(project, "--check", "--task", "t1"))
        project.ci([{"name": "tests", "status": "completed", "conclusion": "success"}])
        outputs.append(land_command(project, "--check", "--task", "t1", "--json"))
        project.env["FAKE_GITLEAKS_CODE"] = "1"
        outputs.append(land_command(project, "--check", "--task", "t1"))
        project.env["FAKE_GITLEAKS_CODE"] = "0"
        outputs.append(land_command(project, "--merge", "--task", "t1", "--confirm", project.head, "--json"))
        self.assertEqual(outputs[-1].returncode, 0, outputs[-1].stdout + outputs[-1].stderr)
        self.assertEqual(project.remote_main(), project.head)
        text = "".join(done.stdout + done.stderr for done in outputs)
        text += (project.state / "land" / "t1" / "records.jsonl").read_text(encoding="utf-8")
        for private in (*tokens, "decoy-owner-login", "example.invalid", "someone@example.com", "Someone Private",
                        str(project.remote), project.remote.as_posix()):
            self.assertNotIn(private, text)
        self.assertIn("OAH_LAND_SCAN_FAILED", text)  # the leak scenario really ran
        self.assertIn("gh exited 1", text)

    def test_record_failure_does_not_hide_a_landing(self):
        from oah.atomic import StorageError

        project = LandProject(self)

        def broken(*_args, **_kwargs):
            raise StorageError(project.state / "land", PermissionError(13, "denied"))

        with patch.object(land, "record", broken):
            done = land_command(project, "--merge", "--task", "t1", "--confirm", project.head)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.splitlines()[-1].startswith("OAH_LAND_LANDED:"), done.stdout)
        self.assertIn("OAH_LAND_RECORD_FAILED", done.stderr)
        self.assertEqual(project.remote_main(), project.head)

    def test_usage_errors_exit_two(self):
        project = LandProject(self, ledger=None, receipt=False, finish=False)
        for args in (("--task", "t1"), ("--check", "--merge", "--task", "t1"), ("--check",),
                     ("--check", "--task", "t1", "--confirm", project.head)):
            with self.subTest(args=args):
                done = land_command(project, *args)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(records(project), [])
        bad = land_command(project, "--check", "--task", "../x")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("OAH_TASK_INVALID", bad.stderr)

    def test_argument_errors_use_fixed_text_and_json_without_credentials(self):
        project = LandProject(self, ledger=None, receipt=False, finish=False)
        private = "gh" + "p_" + secrets.token_hex(24)
        errors = ((private,), ("--" + private,), ("--unknown=" + private,),
                  ("--confirm",), ("--task",), ("--check=" + private,), ("--merge",),
                  ("--confirm", private), ("--", private))
        for flags in ((), ("--json",)):
            expected = None
            for error in errors:
                with self.subTest(json=bool(flags), case=errors.index(error)):
                    done = land_command(project, "--check", "--task", "t1", *flags, *error)
                    emitted = done.stdout + done.stderr
                    self.assertFalse(private in emitted, "credential entered argument-error output")
                    self.assertEqual(done.returncode, 2)
                    self.assertTrue(done.stderr.startswith("OAH_USAGE:"))
                    self.assertIn("oah land --help", done.stderr)
                    if expected is None:
                        expected = (done.stdout, done.stderr)
                    self.assertEqual((done.stdout, done.stderr), expected)
                    if flags:
                        body = json.loads(done.stdout)
                        self.assertEqual(body["code"], "OAH_USAGE")
                        self.assertEqual(body["exit_code"], 2)
                        self.assertIsNone(body["assessment"])
                    else:
                        self.assertEqual(done.stdout, "")
        self.assertEqual(records(project), [])

    def test_root_argument_errors_use_fixed_landing_boundary(self):
        project = LandProject(self, ledger=None, receipt=False, finish=False)
        private = "gh" + "p_" + secrets.token_hex(24)
        for flags in ((), ("--json",)):
            for prefix in (("--private=" + private,), ("--private", private), (private,)):
                with self.subTest(json=bool(flags), position=len(prefix)):
                    done = project.oah(*prefix, "land", "--check", "--task", "t1", *flags)
                    self.assertNotIn(private, done.stdout + done.stderr)
                    self.assertEqual(done.returncode, 2)
                    self.assertEqual(done.stderr, f"OAH_USAGE: {land_cli.USAGE_MESSAGE}\n")
                    if flags:
                        self.assertEqual(json.loads(done.stdout)["code"], "OAH_USAGE")
                    else:
                        self.assertEqual(done.stdout, "")
        done = project.oah("--json", "--private=" + private, "land", "--check", "--task", "t1")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(json.loads(done.stdout)["code"], "OAH_USAGE")
        self.assertNotIn(private, done.stdout + done.stderr)
        self.assertEqual(records(project), [])


if __name__ == "__main__":
    unittest.main()
