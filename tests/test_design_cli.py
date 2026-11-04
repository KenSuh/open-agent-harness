# 설계 명령 검사 — oah design 의 등록·흐름·JSON 모양·표준 입력·입력 파일 거부·종료 코드(0/1/2/69)·하위 폴더 실행을 본다.
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from oah.cli import command_modules, main
from oah.intent import MAX_BYTES
from tests.helpers import git_commit, make_tmp_dir
from tests.test_design_store import PAGE, SRC, DesignRepo, git


class CliTest(DesignRepo, unittest.TestCase):
    def test_c1_oversize_stdin_is_refused_before_normalization_or_writes(self):
        self.created()
        for name, raw in (("crlf", b"x\r\n" * (MAX_BYTES // 3 + 2) + b"Meaningful tail.\n"),
                          ("newline", b"Start.\n" + b"\n" * MAX_BYTES + b"Meaningful tail.\n")):
            code, out, err = self.oah("new", "acme", name, "--title", "Input", "--file",
                                       self.file(name + ".md", "Original.\n"))
            self.assertEqual(code, 0, err)
            digest = out.split()[3]
            for command in (("new", "acme", name + "-new", "--title", "Other", "--file", "-"),
                            ("save", "acme", name, "--expect-revision", "1", "--expect-digest", digest,
                             "--reason", "Two", "--file", "-")):
                with self.subTest(command=command[0], prefix=raw[:10]):
                    before = self.tree(self.repo, self.state(self.repo))
                    code, _, err = self.oah(*command, stdin=raw)
                    self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_INPUT_INVALID"))
                    self.assertEqual(self.tree(self.repo, self.state(self.repo)), before)

    def test_c1_reads_and_check_work_before_local_project_registration(self):
        early = self.repo.parent / "early"
        git(self.repo, "worktree", "add", "-q", "-b", "early", str(early))
        digest = self.created()
        git_commit(self.repo, {}, "register on main")
        before = self.tree(early, self.state(early))
        code, out, err = self.oah("show", "acme", "booking", cwd=early)
        self.assertEqual((code, out), (0, "People book a lesson.\n"), err)
        code, out, err = self.oah("context", "acme", "booking", cwd=early)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["freshness"], "behind")
        code, out, err = self.oah("check", "acme", "booking", "--revision", "1", "--digest", digest, cwd=early)
        self.assertEqual(code, 0, err)
        self.assertIn("OAH_DESIGN_CURRENT acme/booking", out)
        code, out, err = self.oah("status", "acme", "--json", cwd=early)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["projects"][0]["designs"][0]["current"]["digest"], digest)
        self.assertEqual(self.tree(early, self.state(early)), before)

    def setUp(self):
        self.repo = self.make_repo()
        self.enterContext(mock.patch.dict(os.environ, {"OAH_STATE_DIR": str(self.state(self.repo))}))
        self.files = make_tmp_dir(self)

    def oah(self, *args: str, stdin: bytes | None = None, cwd: pathlib.Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        streams = [contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), contextlib.chdir(cwd or self.repo)]
        if stdin is not None:
            streams.append(mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")))
        with contextlib.ExitStack() as stack:
            for item in streams:
                stack.enter_context(item)
            try:
                code = main(["design", *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def file(self, name: str, text: str | bytes) -> str:
        path = self.files / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return str(path)

    def created(self) -> str:
        self.assertEqual(self.oah("project", "add", "acme", "--title", "Acme")[0], 0)
        code, out, err = self.oah("new", "acme", "booking", "--title", "Booking",
                                  "--file", self.file("design.md", "People book a lesson.\n"),
                                  "--part", "request=" + self.file("request.md", "I want lessons booked.\n"))
        self.assertEqual(code, 0, err)
        self.assertIn("OAH_DESIGN_CREATED acme/booking r1 sha256:", out)
        self.assertIn(f"OAH_DESIGN_PAGE {PAGE}", out)
        return out.split()[3]

    def test_registered_by_discovery(self):
        self.assertIn("oah.design_cli", [module.__name__ for module in command_modules()])
        self.assertEqual(self.oah("--help")[0], 0)

    def test_whole_flow_with_stable_output(self):
        digest = self.created()
        code, out, _ = self.oah("show", "acme", "booking")
        self.assertEqual((code, out), (0, "People book a lesson.\n"))
        self.assertEqual(self.oah("show", "acme", "booking", "--part", "request")[1], "I want lessons booked.\n")
        code, out, _ = self.oah("status")
        self.assertEqual(code, 0)
        self.assertIn('project acme "Acme" (authority main): 1 design', out)
        self.assertIn(f'  acme/booking "Booking": r1 {digest} draft, nothing accepted; page clean; freshness current',
                      out)
        data = json.loads(self.oah("status", "--json")[1])
        self.assertEqual(set(data), {"version", "projects", "diagnostics"})
        self.assertEqual(set(data["projects"][0]["designs"][0]),
                         {"project", "design", "title", "current", "accepted", "status", "page", "page_state",
                          "freshness"})
        context = json.loads(self.oah("context", "acme", "booking")[1])
        self.assertEqual(set(context), {"version", "project", "design", "source", "freshness", "recovery_pending",
                                        "current", "accepted", "acceptances", "revisions", "diagnostics"})
        self.assertEqual(context["design"]["page_state"], "clean")
        self.assertEqual(context["recovery_pending"], {"pending": False, "this_design": False, "kind": None,
                                                       "project": None, "design": None})
        self.assertEqual(context["current"]["parts"]["design"]["text"], "People book a lesson.\n")
        code, out, err = self.oah("save", "acme", "booking", "--expect-revision", "1", "--expect-digest", digest,
                                  "--reason", "Add cancelling", "--file", "-",
                                  stdin=b"People book and cancel a lesson.\n")
        self.assertEqual(code, 0, err)
        second = out.split()[3]
        self.assertEqual(self.oah("show", "acme", "booking")[1], "People book and cancel a lesson.\n")
        self.assertEqual(self.oah("show", "acme", "booking", "--revision", "1")[1], "People book a lesson.\n")
        code, out, _ = self.oah("accept", "acme", "booking", "--revision", "2", "--digest", second, "--reason", "Yes")
        self.assertEqual((code, out.splitlines()[0]), (0, f"OAH_DESIGN_ACCEPTED acme/booking r2 {second}"))
        self.assertIn("(already accepted)", self.oah("accept", "acme", "booking", "--revision", "2", "--digest",
                                                     second, "--reason", "Yes")[1])
        code, out, _ = self.oah("status", "acme", "booking")
        self.assertIn("revisions:\n  r2 ", out)
        self.assertIn("acceptances:\n  a1 r2 ", out)
        self.assertEqual(self.oah("reconcile")[1].splitlines()[0], "OAH_DESIGN_RECONCILED written=0 unchanged=3 "
                         "skipped=0")

    def test_check_codes_and_exits(self):
        first = self.created()
        self.assertEqual(self.oah("check", "acme", "booking", "--revision", "1", "--digest", first)[:2],
                         (0, f"OAH_DESIGN_CURRENT acme/booking r1 {first}\n"))
        code, _, err = self.oah("check", "acme", "booking", "--revision", "1", "--digest", "sha256:" + "2" * 64)
        self.assertEqual(code, 1)
        self.assertIn("OAH_DESIGN_BINDING_INVALID", err)
        code, _, err = self.oah("check", "acme", "booking", "--revision", "1", "--digest", first, "--accepted")
        self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_CONTEXT_STALE"))
        page = self.repo / PAGE
        page.write_text(page.read_text(encoding="utf-8").replace("a lesson.", "a lesson today."), encoding="utf-8")
        code, _, err = self.oah("check", "acme", "booking", "--revision", "1", "--digest", first)
        self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_UNREGISTERED_EDIT"))
        code, _, err = self.oah("check", "acme", "nothing", "--revision", "1", "--digest", first)
        self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_NOT_FOUND"))

    def test_refusals_carry_their_codes_and_exits(self):
        first = self.created()
        self.assertEqual(self.oah("save", "acme", "booking", "--expect-revision", "1", "--reason", "x",
                                  "--file", "-")[0], 2)  # --expect-digest is required
        code, _, err = self.oah("save", "acme", "booking", "--expect-revision", "2", "--expect-digest", first,
                                "--reason", "x", "--file", self.file("x.md", "x\n"))
        self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_REVISION_CONFLICT"))
        code, _, err = self.oah("save", "acme", "booking", "--expect-revision", "1", "--expect-digest",
                                "sha256:" + "3" * 64, "--reason", "x", "--file", self.file("x.md", "x\n"))
        self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_CONTEXT_STALE"))
        code, _, err = self.oah("show", "acme", "nothing")
        self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_NOT_FOUND"))
        code, _, err = self.oah("save", "acme", "booking", "--expect-revision", "1", "--expect-digest", first,
                                "--reason", "x", "--part", "no-equals-sign")
        self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_INPUT_INVALID"))
        journal = self.state(self.repo) / "wiki" / "design-journal.json"
        journal.write_text(json.dumps({"kind": "saved", "project": "acme", "design": "booking"}), encoding="utf-8")
        code, _, err = self.oah("save", "acme", "booking", "--expect-revision", "1", "--expect-digest", first,
                                "--reason", "x", "--file", self.file("x.md", "x\n"))
        self.assertEqual((code, err.split(":")[0]), (69, "OAH_DESIGN_RECOVERY_PENDING"))
        code, _, err = self.oah("recover")
        self.assertEqual((code, err.split(":")[0]), (69, "OAH_DESIGN_JOURNAL_CORRUPT"))
        journal.unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "designs")
        git(self.repo, "checkout", "-q", "--detach")
        code, _, err = self.oah("save", "acme", "booking", "--expect-revision", "1", "--expect-digest", first,
                                "--reason", "x", "--file", self.file("x.md", "x\n"))
        self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_NOT_AUTHORITY"))
        self.assertEqual(self.oah("show", "acme", "booking")[1], "People book a lesson.\n")  # reads still work

    def test_input_files_are_read_safely(self):
        self.oah("project", "add", "acme", "--title", "Acme")
        cases = {"not UTF-8": self.file("latin.md", b"caf\xe9\n"),
                 "too large": self.file("big.md", b"x" * (1024 * 1024 + 1)),
                 "missing": str(self.files / "absent.md")}
        link = self.files / "link.md"
        try:
            link.symlink_to(self.file("real.md", "Real.\n"))
            cases["a link"] = str(link)
        except OSError:
            pass
        for label, path in cases.items():
            with self.subTest(label=label):
                code, _, err = self.oah("new", "acme", "booking", "--title", "Booking", "--file", path)
                self.assertEqual((code, err.split(":")[0]), (1, "OAH_DESIGN_INPUT_INVALID"))
        self.assertFalse((self.repo / ".oah/designs/acme/booking").exists())

    def test_runs_from_a_subfolder_and_refuses_outside_a_repository(self):
        self.created()
        env = dict(os.environ, PYTHONPATH=SRC)
        result = subprocess.run([sys.executable, "-m", "oah", "design", "status"], cwd=self.repo / "docs",
                                env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acme/booking", result.stdout)
        outside = make_tmp_dir(self)
        env["GIT_CEILING_DIRECTORIES"] = str(outside.parent)
        result = subprocess.run([sys.executable, "-m", "oah", "design", "status"], cwd=outside, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 69, result.stderr)
        self.assertIn("OAH_NOT_A_REPO", result.stderr)


if __name__ == "__main__":
    unittest.main()
