# 훅 설치 검사 — Claude Code 설정·git 훅의 설치·제거·상태, 변경 미리보기, 백업, 멱등성, 공유 설치의 절대 경로 없음, oah hook 분기를 본다.
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

from oah import hook_cli, hook_pre_push, hooks, hooks_cli
from oah.lease import Store
from oah.paths import repo_info
from tests.fixtures.gates.support import build_args, run_cli
from tests.helpers import git_commit, make_git_repo, make_tmp_dir


def always(_kind):
    return True


class ClaudeHooksTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"a.txt": "a\n"})
        self.local = self.repo / ".claude" / "settings.local.json"

    def test_install_shows_diff_and_writes(self):
        preview = hooks.install("stop", self.repo, dry_run=True)
        self.assertEqual(preview.action, "install")
        self.assertIn('"Stop"', preview.diff)
        self.assertIn("oah hook stop", preview.diff)
        self.assertFalse(self.local.exists())
        change = hooks.install("stop", self.repo, dry_run=False)
        self.assertEqual(change.diff, preview.diff)
        data = json.loads(self.local.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": "oah hook stop"}]}])

    def test_install_is_idempotent(self):
        hooks.install("stop", self.repo, dry_run=False)
        first = self.local.read_bytes()
        again = hooks.install("stop", self.repo, dry_run=False)
        self.assertEqual(again.action, "unchanged")
        self.assertEqual(again.diff, "")
        self.assertEqual(self.local.read_bytes(), first)

    def test_backup_kept_before_change(self):
        self.local.parent.mkdir()
        original = '{\r\n  "permissions": {"allow": ["Bash(ls)"]},\r\n  "hooks": {"Stop": [{"hooks": [{"type": "command", ' \
                   '"command": "echo bye"}]}]}\r\n}\r\n'
        self.local.write_bytes(original.encode("utf-8"))
        change = hooks.install("stop", self.repo, dry_run=False)
        self.assertEqual(change.backup, self.local.with_name("settings.local.json.oah.bak"))
        self.assertEqual(change.backup.read_bytes(), original.encode("utf-8"))
        text = self.local.read_bytes().decode("utf-8")
        self.assertIn("\r\n", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        data = json.loads(text)
        commands = [hook["command"] for group in data["hooks"]["Stop"] for hook in group["hooks"]]
        self.assertEqual(commands, ["echo bye", "oah hook stop"])
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})

    def test_uninstall_restores_cleanly(self):
        self.local.parent.mkdir()
        self.local.write_text('{"permissions": {"allow": []}}\n', encoding="utf-8")
        hooks.install("stop", self.repo, dry_run=False)
        hooks.install("impact-hint", self.repo, dry_run=False, available=always)
        removed = hooks.uninstall("stop", self.repo, dry_run=False)
        self.assertEqual(removed.action, "uninstall")
        self.assertIn("-", removed.diff)
        hooks.uninstall("impact-hint", self.repo, dry_run=False)
        self.assertEqual(json.loads(self.local.read_text(encoding="utf-8")), {"permissions": {"allow": []}})
        self.assertFalse(self.local.with_name("settings.local.json.oah.bak").exists())
        self.assertEqual(hooks.uninstall("stop", self.repo, dry_run=False).action, "unchanged")

    def test_uninstall_of_created_file_removes_it(self):
        hooks.install("stop", self.repo, dry_run=False)
        hooks.uninstall("stop", self.repo, dry_run=False)
        self.assertFalse(self.local.exists())

    def test_shared_install_has_no_absolute_paths(self):
        change = hooks.install("stop", self.repo, dry_run=False, shared=True)
        shared = self.repo / ".claude" / "settings.json"
        self.assertEqual(change.path, shared)
        text = shared.read_text(encoding="utf-8")
        self.assertNotIn(str(self.repo), text)
        self.assertNotIn(str(pathlib.Path.home()), text)
        for hook in (h for group in json.loads(text)["hooks"]["Stop"] for h in group["hooks"]):
            self.assertFalse(os.path.isabs(hook["command"].split()[0]))

    def test_impact_hint_uses_pre_tool_use_matcher(self):
        hooks.install("impact-hint", self.repo, dry_run=False, available=always)
        group = json.loads(self.local.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]
        self.assertIn("Edit", group["matcher"])
        self.assertEqual(group["hooks"][0]["command"], "oah hook impact-hint")

    def test_invalid_settings_refused_untouched(self):
        self.local.parent.mkdir()
        for text in ("{not json", "[]", '{"hooks": []}', '{"hooks": {"Stop": {}}}'):
            with self.subTest(text=text):
                self.local.write_text(text, encoding="utf-8")
                with self.assertRaises(hooks.HookError) as caught:
                    hooks.install("stop", self.repo, dry_run=False)
                self.assertEqual(caught.exception.code, "OAH_HOOKS_SETTINGS_INVALID")
                self.assertEqual(self.local.read_text(encoding="utf-8"), text)

    def test_unavailable_hook_command_is_refused(self):
        with self.assertRaises(hooks.HookError) as caught:
            hooks.install("session-start", self.repo, dry_run=False, available=lambda kind: False)
        self.assertEqual(caught.exception.code, "OAH_HOOK_UNAVAILABLE")

    def test_status(self):
        hooks.install("stop", self.repo, dry_run=False)
        rows = {row.kind: row.state for row in hooks.status(self.repo)}
        self.assertEqual(rows["stop"], "installed")
        self.assertEqual(rows["pre-push"], "absent")
        self.assertEqual(set(rows), set(hooks.KINDS))


class GitHooksTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"a.txt": "a\n"})
        self.hook = self.repo / ".git" / "hooks" / "pre-push"

    def test_git_hook_install_uninstall_status(self):
        change = hooks.install("pre-push", self.repo, dry_run=False)
        self.assertEqual(change.path.resolve(), self.hook.resolve())
        text = self.hook.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn('exec oah hook pre-push "$@"', text)
        if os.name != "nt":
            self.assertTrue(os.access(self.hook, os.X_OK))
        self.assertEqual(hooks.install("pre-push", self.repo, dry_run=False).action, "unchanged")
        self.assertEqual({row.kind: row.state for row in hooks.status(self.repo)}["pre-push"], "installed")
        self.assertEqual(hooks.uninstall("pre-push", self.repo, dry_run=False).action, "uninstall")
        self.assertFalse(self.hook.exists())
        self.assertEqual(hooks.uninstall("pre-push", self.repo, dry_run=False).action, "unchanged")

    def test_every_git_kind_calls_its_hook(self):
        for kind in ("pre-commit", "post-commit", "post-merge", "pre-push"):
            with self.subTest(kind=kind):
                hooks.install(kind, self.repo, dry_run=False, available=always)
                text = (self.repo / ".git" / "hooks" / kind).read_text(encoding="utf-8")
                self.assertIn(f'exec oah hook {kind} "$@"', text)

    def test_foreign_git_hook_needs_force_and_is_restored(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        with self.assertRaises(hooks.HookError) as caught:
            hooks.install("pre-push", self.repo, dry_run=False)
        self.assertEqual(caught.exception.code, "OAH_HOOK_FOREIGN")
        self.assertEqual({row.kind: row.state for row in hooks.status(self.repo)}["pre-push"], "foreign")
        change = hooks.install("pre-push", self.repo, dry_run=False, force=True)
        self.assertEqual(change.backup.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        restored = hooks.uninstall("pre-push", self.repo, dry_run=False)
        self.assertEqual(restored.action, "restore")
        self.assertEqual(self.hook.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        self.assertFalse(change.backup.exists())

    def test_edited_managed_hook_requires_force_and_is_restored(self):
        hooks.install("pre-push", self.repo, dry_run=False)
        edited = hooks.git_hook_text("pre-push") + "# user addition\necho keep-me\n"
        self.hook.write_bytes(edited.encode("utf-8"))
        with self.assertRaises(hooks.HookError):
            hooks.install("pre-push", self.repo, dry_run=False)
        self.assertEqual(self.hook.read_bytes(), edited.encode("utf-8"))
        self.assertEqual(hooks.uninstall("pre-push", self.repo, dry_run=False).action, "unchanged")
        self.assertEqual(self.hook.read_bytes(), edited.encode("utf-8"))
        self.assertEqual({row.kind: row.state for row in hooks.status(self.repo)}["pre-push"], "foreign")
        preview = hooks.install("pre-push", self.repo, dry_run=True, force=True)
        self.assertEqual(self.hook.read_bytes(), edited.encode("utf-8"))
        self.assertFalse(preview.backup.exists())
        change = hooks.install("pre-push", self.repo, dry_run=False, force=True)
        self.assertEqual(change.backup.read_bytes(), edited.encode("utf-8"))
        hooks.uninstall("pre-push", self.repo, dry_run=False)
        self.assertEqual(self.hook.read_bytes(), edited.encode("utf-8"))

    def test_uninstall_preserves_edited_managed_hook_and_existing_backup(self):
        self.hook.write_text("#!/bin/sh\necho original\n", encoding="utf-8")
        change = hooks.install("pre-push", self.repo, dry_run=False, force=True)
        edited = hooks.git_hook_text("pre-push") + "# user addition\n"
        self.hook.write_bytes(edited.encode("utf-8"))
        original_backup = change.backup.read_bytes()
        removed = hooks.uninstall("pre-push", self.repo, dry_run=False)
        self.assertEqual(removed.action, "unchanged")
        self.assertEqual(self.hook.read_bytes(), edited.encode("utf-8"))
        self.assertEqual(change.backup.read_bytes(), original_backup)
        with self.assertRaises(hooks.HookError) as caught:
            hooks.install("pre-push", self.repo, dry_run=False, force=True)
        self.assertEqual(caught.exception.code, "OAH_HOOK_BACKUP_EXISTS")

    def test_hooks_path_config_is_respected(self):
        subprocess.run(["git", "-C", str(self.repo), "config", "core.hooksPath", "githooks"], check=True)
        change = hooks.install("pre-push", self.repo, dry_run=False)
        self.assertEqual(change.path.resolve(), (self.repo / "githooks" / "pre-push").resolve())


class HooksCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"a.txt": "a\n"})

    def test_cli_dry_run_install_status_uninstall(self):
        dry = run_cli(["hooks", "install", "stop", "--dry-run"], cwd=self.repo)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("oah hook stop", dry.stdout)
        self.assertIn("OAH_HOOK_DRY_RUN kind=stop", dry.stdout)
        self.assertFalse((self.repo / ".claude" / "settings.local.json").exists())
        done = run_cli(["hooks", "install", "stop", "pre-push"], cwd=self.repo)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("OAH_HOOK_INSTALLED kind=stop", done.stdout)
        self.assertIn("OAH_HOOK_INSTALLED kind=pre-push", done.stdout)
        status = run_cli(["hooks", "status"], cwd=self.repo)
        self.assertRegex(status.stdout, r"stop\s+installed")
        removed = run_cli(["hooks", "uninstall", "stop", "pre-push"], cwd=self.repo)
        self.assertIn("OAH_HOOK_REMOVED kind=stop", removed.stdout)
        self.assertNotIn(str(self.repo), removed.stdout.replace(str(self.repo.resolve()), ""))

    def test_cli_refusals(self):
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        refused = run_cli(["hooks", "install", "pre-push"], cwd=self.repo)
        self.assertEqual(refused.returncode, 1)
        self.assertTrue(refused.stderr.startswith("OAH_HOOK_FOREIGN"), refused.stderr)
        self.assertIn("--force", refused.stderr)
        unknown = run_cli(["hooks", "install", "nope"], cwd=self.repo)
        self.assertEqual(unknown.returncode, 2)


class HookDispatchTest(unittest.TestCase):
    def test_unknown_hook_exits_2_and_lists(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = hook_cli.run(build_args(["hook", "no-such-hook"]))
        self.assertEqual(code, 2)
        self.assertIn("OAH_HOOK_UNKNOWN", err.getvalue())
        self.assertIn("stop", err.getvalue())
        self.assertIn("pre-push", err.getvalue())
        self.assertNotIn("cli", hook_cli.known_hooks())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hook_cli.run(build_args(["hook", "Bad_Name"])), 2)
            self.assertEqual(hook_cli.run(build_args(["hook", "cli"])), 2)

    def test_dispatch_dashes_to_underscores(self):
        seen = {}

        class Fake:
            @staticmethod
            def run(args):
                seen["args"] = args.hook_args
                return 7

        args = build_args(["hook", "pre-push", "origin", "url"])
        with patch.object(hook_cli.importlib, "import_module", return_value=Fake) as importer, \
                patch.object(hook_cli, "known_hooks", return_value=("pre-push", "stop")):
            code = hook_cli.run(args)
        importer.assert_called_with("oah.hook_pre_push")
        self.assertEqual((code, seen["args"]), (7, ["origin", "url"]))


FAKE_GITLEAKS = """import json, os, sys
with open(os.environ["FAKE_GITLEAKS_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
print("Finding: REDACTED" if os.environ.get("FAKE_GITLEAKS_CODE") == "1" else "no leaks found")
sys.exit(int(os.environ.get("FAKE_GITLEAKS_CODE", "0")))
"""


class PrePushTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        self.head = git_commit(self.repo, {"a.txt": "a\n"})
        self.tmp = make_tmp_dir(self)
        self.state = self.tmp / "state"
        self.fake = self.tmp / "fake_gitleaks.py"
        self.fake.write_text(FAKE_GITLEAKS, encoding="utf-8")
        self.log = self.tmp / "gitleaks.jsonl"

    def push(self, stdin, *, gitleaks=True, code="0"):
        env = dict(os.environ, OAH_STATE_DIR=str(self.state), FAKE_GITLEAKS_LOG=str(self.log),
                   FAKE_GITLEAKS_CODE=code)
        finder = (lambda _env: (sys.executable, str(self.fake))) if gitleaks else (lambda _env: None)
        out, err = io.StringIO(), io.StringIO()
        previous = os.getcwd()
        os.chdir(self.repo)
        try:
            with patch.dict(os.environ, env, clear=True), patch.object(hook_pre_push, "find_gitleaks", finder), \
                    patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code_out = hook_pre_push.run(build_args(["hook", "pre-push", "origin", "git@example.com:x"]))
        finally:
            os.chdir(previous)
        return code_out, out.getvalue(), err.getvalue()

    def line(self, remote_sha="0" * 40):
        return f"refs/heads/main {self.head} refs/heads/main {remote_sha}\n"

    def test_pre_push_refuses_with_unfinished_lease(self):
        info = repo_info(self.repo)
        Store(self.state).start(task="t1", agent="claude", scope=("a.txt",), note="", ttl_hours=1, info=info)
        code, _, err = self.push(self.line())
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_PUSH_REFUSED"), err)
        self.assertIn("oah finish --task t1 --agent claude", err)
        self.assertFalse(self.log.exists())

    def test_lease_on_other_worktree_does_not_block(self):
        other = make_tmp_dir(self) / "wt"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "side", str(other)], check=True)
        Store(self.state).start(task="t1", agent="claude", scope=("a.txt",), note="", ttl_hours=1,
                                info=repo_info(other))
        code, _, err = self.push(self.line())
        self.assertEqual(code, 0, err)

    def test_pre_push_runs_gitleaks_over_pushed_range(self):
        code, _, err = self.push(self.line(remote_sha="1" * 40))
        self.assertEqual(code, 0, err)
        argv = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertIn("--log-opts", argv)
        self.assertEqual(argv[argv.index("--log-opts") + 1], f"{'1' * 40}..{self.head}")
        self.assertIn("--redact", argv)
        self.push(self.line())
        argv = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(argv[argv.index("--log-opts") + 1], f"{self.head} --not --remotes=origin")
        deletion = f"(delete) {'0' * 40} refs/heads/old {self.head}\n"
        before = len(self.log.read_text(encoding="utf-8").splitlines())
        self.assertEqual(self.push(deletion)[0], 0)
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), before)

    def test_pre_push_refuses_when_gitleaks_finds_secrets(self):
        code, _, err = self.push(self.line(), code="1")
        self.assertEqual(code, 1)
        self.assertIn("OAH_PUSH_SECRETS", err)
        self.assertIn("REDACTED", err)
        failed, _, err = self.push(self.line(), code="3")
        self.assertEqual(failed, 1)
        self.assertIn("OAH_GITLEAKS_FAILED", err)

    def test_pre_push_reports_gitleaks_absent(self):
        code, _, err = self.push(self.line(), gitleaks=False)
        self.assertEqual(code, 0)
        self.assertIn("unused(no gitleaks)", err)


if __name__ == "__main__":
    unittest.main()
