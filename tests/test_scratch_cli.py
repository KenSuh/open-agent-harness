from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import pathlib
import unittest
from unittest import mock

from oah import fix_cli, land_cli, scratch, sources_cli
from oah.cli import build_parser
from oah.review import fix, receipts, runner
from oah.secrets import NoKeychain
from tests.helpers import hostile_temp, make_git_repo, scratch_base, unsafe_scratch
from tests.test_design_sources import LICENSE, SourceCase, git, gitref
from tests.test_fix import FIXED, FixProject
from tests.test_review_rounds import RULES


@contextlib.contextmanager
def recorded_refusals():
    errors = []

    def observed(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except scratch.ScratchError as exc:
            errors.append(exc)
            raise

    with mock.patch.object(scratch, "directory", functools.partial(observed, scratch.directory)), \
            mock.patch.object(scratch, "make", functools.partial(observed, scratch.make)):
        yield errors


class SourceScratchCliTest(SourceCase):
    def refusal(self, site):
        self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})
        self.tag("v1.0.0")
        self.github.add("v1.0.0")
        branch = git(self.upstream, "branch", "--show-current")
        track = "release" if site == "release-fetch" else "commit"
        sources = self.sources()
        spec = self.register(sources, track=track, **({"ref": branch} if track == "commit" else {}))
        advertisement = gitref.ls_remote(str(self.upstream), tags=track == "release")
        for as_json in (False, True):
            with self.subTest(site=site, as_json=as_json):
                args = build_parser().parse_args(["sources", "check", spec.id, *(["--json"] if as_json else [])])
                out, err = io.StringIO(), io.StringIO()
                remote = (contextlib.nullcontext() if site == "advertisement" else
                          mock.patch.object(gitref, "ls_remote", return_value=advertisement))
                with unsafe_scratch(self), remote, recorded_refusals() as errors, \
                        mock.patch.object(sources_cli, "_sources", return_value=sources), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    base = r"%LOCALAPPDATA%\oah\scratch" if os.name == "nt" else str(scratch.base_path().resolve())
                    code = sources_cli.run(args)
                self.assertEqual(code, 1)
                self.assertEqual(len(errors), 1, "the command stops on the first scratch refusal")
                self.assertEqual(errors[0].base, base)
                self.assertEqual(err.getvalue().strip(), str(errors[0]))
                if as_json:
                    payload = json.loads(out.getvalue())
                    self.assertEqual(payload["code"], "OAH_SCRATCH_UNSAFE")
                    self.assertEqual(payload["message"], str(errors[0]))
                else:
                    self.assertEqual(out.getvalue(), "")
                self.assertIsNone(sources.head(spec), "a local scratch refusal is never a failed source attempt")

    def test_advertisement_refusal_reaches_the_command(self):
        self.refusal("advertisement")

    def test_commit_fetch_refusal_reaches_the_command(self):
        self.refusal("commit-fetch")

    def test_release_fetch_refusal_reaches_the_command(self):
        self.refusal("release-fetch")


class LandScratchCliTest(unittest.TestCase):
    def test_refusal_keeps_the_code_base_reason_and_fix(self):
        repo = make_git_repo(self)
        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                args = build_parser().parse_args(["land", "--check", "--task", "t1",
                                                 *(["--json"] if as_json else [])])
                out, err = io.StringIO(), io.StringIO()
                with contextlib.chdir(repo), unsafe_scratch(self), recorded_refusals() as errors, \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    base = r"%LOCALAPPDATA%\oah\scratch" if os.name == "nt" else str(scratch.base_path().resolve())
                    code = land_cli.run(args)
                self.assertEqual(code, 1)
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0].base, base)
                self.assertEqual(err.getvalue().strip(), str(errors[0]))
                if as_json:
                    payload = json.loads(out.getvalue())
                    self.assertEqual((payload["code"], payload["exit_code"]), ("OAH_SCRATCH_UNSAFE", 1))
                    self.assertEqual(payload["message"], errors[0].detail)
                    self.assertIsNone(payload["assessment"])
                    self.assertIsNone(payload["pushed"])
                else:
                    self.assertEqual(out.getvalue(), "")


class FixScratchCliTest(unittest.TestCase):
    def refusal(self, *, passed_root=False, credential=False):
        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                project = FixProject(self)
                project.changes_requested()
                project.queue_fix({"write": FIXED})
                marker = "fixture-scratch-value-c2" if credential else "readable-root"
                folder = project.repo / marker
                recorded_mode = None
                if passed_root:
                    folder.mkdir(mode=0o755)
                    if os.name != "nt":
                        folder.chmod(0o755)
                    root = pathlib.Path(marker) if os.name == "nt" else folder
                    recorded_mode = folder.stat().st_mode
                else:
                    root = None
                if credential:
                    project.env["EXAMPLE_API_KEY"] = marker
                correct = fix.correct

                def invoke(request, *, cwd, env):
                    return correct(request, cwd=cwd, env=env, keychain=NoKeychain(), rules=RULES, temp_root=root)

                args = build_parser().parse_args(["fix", "--task", "t1", "--tier", "high",
                                                 *(["--json"] if as_json else [])])
                out, err = io.StringIO(), io.StringIO()
                selected = hostile_temp(self) if passed_root else unsafe_scratch(self)
                with contextlib.chdir(project.repo), mock.patch.dict(os.environ, project.env, clear=True), selected as placement, \
                        recorded_refusals() as errors, mock.patch.object(fix, "correct", side_effect=invoke), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    default_base = scratch_base() if passed_root else None
                    code = fix_cli.run(args)
                self.assertEqual(code, 1)
                self.assertEqual(len(errors), 1)
                expected = runner.credential_redactor(project.env)(str(errors[0]))
                self.assertEqual(err.getvalue().strip(), expected)
                self.assertIn("OAH_SCRATCH_UNSAFE", expected)
                if passed_root:
                    assert root is not None
                    self.assertEqual(errors[0].base, str(root.parent / "[REDACTED]" if credential else root))
                    self.assertIn("not an absolute path" if os.name == "nt" else "0755", errors[0].reason)
                    self.assertEqual(list(folder.iterdir()), [])
                    self.assertEqual(folder.stat().st_mode, recorded_mode)
                    assert placement is not None
                    shared, created = placement
                    self.assertEqual(list(shared.iterdir()), [])
                    self.assertTrue(all(path.parent == default_base for path in created))
                if credential:
                    self.assertNotIn(marker, errors[0].base)
                    self.assertNotIn(marker, out.getvalue() + err.getvalue())
                    self.assertIn("[REDACTED]", expected)
                if as_json:
                    payload = json.loads(out.getvalue())
                    self.assertEqual((payload["code"], payload["exit_code"]), ("OAH_SCRATCH_UNSAFE", 1))
                    self.assertEqual(payload["message"], expected)
                else:
                    self.assertEqual(out.getvalue(), "")
                self.assertEqual(list(project.fix_dir.glob("pid-*.txt")), [])

    def test_state_capture_refusal_reaches_the_command(self):
        self.refusal()

    def test_passed_root_refusal_reaches_the_command(self):
        self.refusal(passed_root=True)

    def test_passed_root_diagnostic_keeps_credential_redaction(self):
        self.refusal(passed_root=True, credential=True)

    def test_started_attempt_refusal_reaches_the_command_and_outcome(self):
        project = FixProject(self)
        project.changes_requested()
        project.queue_fix({"write": FIXED})
        folder = project.repo / "readable-after-start"
        folder.mkdir(mode=0o700)
        root = pathlib.Path(folder.name) if os.name == "nt" else folder

        def inspect(*args, **kwargs):
            if os.name != "nt":
                folder.chmod(0o755)
            scratch.make("oah-fix-test-", root=root)

        out, err = io.StringIO(), io.StringIO()
        args = build_parser().parse_args(["fix", "--task", "t1", "--tier", "high", "--json"])
        with contextlib.chdir(project.repo), mock.patch.dict(os.environ, project.env, clear=True), \
                recorded_refusals() as errors, mock.patch.object(fix._Attempt, "_judge", side_effect=inspect), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fix_cli.run(args)
        self.assertEqual(code, 1)
        self.assertEqual(len(errors), 1)
        expected = str(errors[0])
        payload = json.loads(out.getvalue())
        self.assertEqual((payload["code"], payload["message"]), ("OAH_SCRATCH_UNSAFE", expected))
        self.assertTrue(err.getvalue().rstrip().endswith(expected))
        outcome = project.record("A1-outcome.json")
        self.assertEqual((outcome["outcome"], outcome["evidence"]),
                         ("failed", receipts.digest(expected.encode("utf-8"))))
        self.assertEqual(list(folder.iterdir()), [])
