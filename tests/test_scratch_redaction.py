from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import subprocess
import sys
import types
import unittest
from unittest import mock

from oah import eval_cli, land_cli, review_cli, scratch, skills_cli, sources_cli, unlazy
from oah.cli import build_parser
from oah.paths import repo_info
from oah.review import runner
from oah.secrets import NoKeychain
from oah.toolkit import fetch, install, schedule, update
from oah.toolkit.catalog import Catalog
from tests import test_design_sources as source_tests
from tests import test_doctor as doctor_tests
from tests import test_eval as eval_tests
from tests import test_review_rounds as review_tests
from tests.test_call import Project
from tests.test_models import project_with_fakes
from tests.helpers import hostile_temp, make_git_repo, make_tmp_dir, scratch_env


@contextlib.contextmanager
def credential_base(test, value):
    parent = make_tmp_dir(test) / "credential-parent"
    parent.mkdir(mode=0o700)
    selected = parent / value
    if os.name == "nt":
        selection = mock.patch.object(scratch, "base_path", return_value=pathlib.Path("credential-parent") / value)
    else:
        parent.chmod(0o777)
        selection = contextlib.nullcontext()
    recorded_mode = parent.stat().st_mode
    with hostile_temp(test) as (shared, created), selection, \
            mock.patch.dict(os.environ, scratch_env(selected) | {"GH_TOKEN": value}):
        yield selected
    test.assertEqual(created, [], "the refused command creates no scratch folder elsewhere")
    test.assertEqual(list(shared.iterdir()), [])
    test.assertFalse(os.path.lexists(selected))
    test.assertTrue(parent.is_dir())
    test.assertEqual(parent.stat().st_mode, recorded_mode)


class OutputCase(unittest.TestCase):
    def assert_outputs(self, argv, action, exit_code):
        formats = (False, True) if argv[0] in ("sources", "land", "doctor", "review") else (False,)
        for value in ("fixture-exported-value-c3", "fixture-exported\r\nvalue-c3"):
            for as_json in formats:
                with self.subTest(command=argv[0], as_json=as_json, newline="\r\n" in value):
                    out, err = io.StringIO(), io.StringIO()
                    with credential_base(self, value) as selected, contextlib.redirect_stdout(out), \
                            contextlib.redirect_stderr(err):
                        args = build_parser().parse_args([*argv, *(["--json"] if as_json else [])])
                        code = action(args, selected)
                    self.assertEqual(code, exit_code)
                    rendered = out.getvalue() + err.getvalue()
                    self.assertIn("OAH_SCRATCH_UNSAFE", rendered)
                    self.assertIn("credential-parent", rendered)
                    self.assertIn("[REDACTED]", rendered)
                    for secret in (value, value.replace("\r\n", "\n")):
                        for form in (secret, json.dumps(secret)[1:-1], repr(secret)[1:-1]):
                            self.assertNotIn(form, rendered)
                    if as_json and argv[:2] != ["sources", "schedule"]:
                        payload = json.loads(out.getvalue())
                        if argv[0] == "doctor":
                            self.assertEqual(payload["scratch"]["error"], "OAH_SCRATCH_UNSAFE")
                            self.assertEqual(payload["scratch"]["unreadable"], 1)
                        else:
                            self.assertEqual(payload["code"], "OAH_SCRATCH_UNSAFE")
                            if argv[0] == "land":
                                self.assertIsNone(payload["assessment"])
                                self.assertIsNone(payload["pushed"])
                    elif argv[0] != "doctor":
                        self.assertEqual(out.getvalue(), "")


class SourceCredentialTest(source_tests.SourceCase, OutputCase):
    def test_exported_value_in_source_refusal_is_redacted(self):
        self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": source_tests.LICENSE})
        branch = source_tests.git(self.upstream, "branch", "--show-current")
        sources = self.sources()
        spec = self.register(sources, track="commit", ref=branch)

        def action(args, _selected):
            with mock.patch.object(sources_cli, "_sources", return_value=sources):
                return sources_cli.run(args)

        self.assert_outputs(["sources", "check", spec.id], action, 1)
        self.assertIsNone(sources.head(spec))


class CredentialOutputTest(OutputCase):
    def test_exported_newline_value_is_redacted_after_line_normalization(self):
        redact = runner.credential_redactor({"GH_TOKEN": "fixture-exported\r\nvalue-c3"})
        self.assertEqual(redact("base fixture-exported\r\nvalue-c3; reason fixture-exported\nvalue-c3"),
                         "base [REDACTED]; reason [REDACTED]")

    def test_exported_value_in_land_refusal_is_redacted(self):
        repo = make_git_repo(self)

        def action(args, _selected):
            with contextlib.chdir(repo):
                return land_cli.run(args)

        self.assert_outputs(["land", "--check", "--task", "t1"], action, 1)

    def test_exported_value_in_eval_refusal_is_redacted(self):
        root = eval_tests.fixture_copy(self)
        destination = root / "prereg.json"
        self.assert_outputs(["eval", "prereg", str(root / "plan.toml"), "--oracle",
                             str(eval_tests.oracle_path(root)), "--out", str(destination)],
                            lambda args, _selected: eval_cli.run(args), 69)
        self.assertFalse(destination.exists())

    def test_exported_value_in_install_and_schedule_refusals_is_redacted(self):
        now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        root = make_tmp_dir(self)
        catalog = Catalog((), ())
        entry = schedule.Entry(kind="schtasks", name="oah-test-task", path=None, argv=("oah", "skills", "update"),
                               project_root=root, log_path=root / "log.txt")
        attempts = {"install": lambda: install.apply_install(install.InstallPlan((), (), {}), catalog=catalog, now=now),
                    "update": lambda: update.run_update(catalog=catalog, destinations=[], check_only=True, settings={},
                                                         now=now, gitleaks=None),
                    "schedule": lambda: schedule.install_entry(entry, search_path=str(root))}
        for name, attempt in attempts.items():
            def action(args, _selected):
                with mock.patch.dict(skills_cli.HANDLERS, {name: lambda _args: attempt()}):
                    return skills_cli.run(args)

            self.assert_outputs(["skills", name, *(["install", "--yes"] if name == "schedule" else [])], action, 1)

        def source_action(args, _selected):
            with mock.patch.dict(sources_cli.HANDLERS, {"schedule": lambda _args: attempts["schedule"]()}):
                return sources_cli.run(args)

        self.assert_outputs(["sources", "schedule", "install", "--yes"], source_action, 1)

    def test_exported_value_in_direct_toolkit_refusal_is_redacted(self):
        def operation(_args):
            with fetch._git_env():
                self.fail("scratch refusal must precede git execution")

        def action(args, _selected):
            with mock.patch.dict(skills_cli.HANDLERS, {"update": operation}):
                return skills_cli.run(args)

        self.assert_outputs(["skills", "update"], action, 69)

    def test_exported_value_in_automatic_toolkit_refusal_is_redacted(self):
        repo = make_git_repo(self)

        def action(_args, _selected):
            with mock.patch.object(skills_cli, "load_catalog", return_value=None), \
                    mock.patch.object(skills_cli, "settings_for", return_value={}), \
                    mock.patch.object(skills_cli, "find_gitleaks", return_value=None), \
                    mock.patch.object(skills_cli, "scheduled_update", side_effect=lambda **kw: scratch.make("oah-update-")):
                return skills_cli.before_mutation(repo_info(repo), repo / ".state")

        self.assert_outputs(["skills", "update"], action, None)

    def test_exported_value_in_doctor_refusal_is_redacted(self):
        fixture = doctor_tests.ScratchDoctorTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        base_path = scratch.base_path

        def action(args, _selected):
            fixture.env = {"HOME": str(pathlib.Path("credential-parent") / os.environ["GH_TOKEN"]), "XDG_CACHE_HOME": ""}
            with mock.patch.object(scratch, "base_path", side_effect=lambda env=None, platform=None: base_path(env, "darwin")):
                code, rendered = fixture.doctor(json_output=args.json)
            sys.stdout.write(rendered)
            return code

        self.assert_outputs(["doctor"], action, 0)

    def test_exported_value_in_review_refusal_is_redacted(self):
        project = review_tests.Project(self)
        review = runner.review

        def action(args, selected):
            project.env["GH_TOKEN"] = os.environ["GH_TOKEN"]
            def execute(request, **kw):
                root = pathlib.Path("credential-parent") / os.environ["GH_TOKEN"] if os.name == "nt" else selected
                return review(request, cwd=project.repo, env=project.env, keychain=NoKeychain(),
                              temp_root=root, rules=review_tests.RULES)

            with mock.patch.object(runner, "review", side_effect=execute):
                return review_cli.run(args)

        self.assert_outputs(["review", "--task", "t1"], action, 69)
        self.assertEqual(project.calls(), [])


class ScratchConstructionTest(unittest.TestCase):
    def assert_safe(self, error, values):
        self.assertEqual(error.code, "OAH_SCRATCH_UNSAFE")
        self.assertIn("[REDACTED]", error.base)
        self.assertIn("[REDACTED]", error.reason)
        for field in (str(error), error.base, error.reason, error.detail, *error.args):
            for value in values:
                for normalized in (value, value.replace("\r\n", "\n")):
                    for form in (normalized, json.dumps(normalized)[1:-1], repr(normalized)[1:-1]):
                        self.assertNotIn(form, field)
        self.assertIn("then retry", str(error))

    def test_each_raise_site_redacts_base_reason_message_and_args(self):
        root = make_tmp_dir(self)
        value = "fixture-construction-value-c4"
        folder = root / value
        deep = folder.joinpath(*["part"] * 70)
        cases = [
            ("home lookup", "~", "cannot be read", lambda: scratch._home({}, "darwin"),
             [mock.patch.dict(sys.modules, {"pwd": types.SimpleNamespace(
                 getpwuid=mock.Mock(side_effect=OSError("lookup ~ failed")))}),
              mock.patch.object(os, "getuid", return_value=123, create=True)]),
            ("localappdata", "LOCALAPPDATA", "absolute folder",
             lambda: scratch.base_path({"LOCALAPPDATA": "relative"}, "win32"), []),
            ("relative home", value, "home folder",
             lambda: scratch.base_path({"HOME": value}, "darwin"), []),
            ("relative root", value, "absolute path", lambda: scratch.make("oah-test-", root=value), []),
            ("bounded input", value, "not normalized", lambda: scratch.make("oah-test-", root=deep), []),
            ("resolve error", value, value, lambda: scratch.make("oah-test-", root=folder),
             [mock.patch.object(scratch.os.path, "realpath", side_effect=OSError("resolve " + value))]),
            ("bounded resolved path", value, "not normalized", lambda: scratch.make("oah-test-", root=folder),
             [mock.patch.object(scratch.os.path, "realpath", return_value=str(deep))]),
            ("ancestry refusal", value, value, lambda: scratch.make("oah-test-", root=folder),
             [mock.patch.object(unlazy, "_verify_state_ancestry",
                                side_effect=unlazy.UnlazyError("OAH_UNLAZY_UNSAFE_STATE", "proof " + value))]),
            ("filesystem refusal", value, value, lambda: scratch.make("oah-test-", root=folder),
             [mock.patch.object(unlazy, "_verify_state_ancestry", side_effect=OSError("proof " + value))]),
        ]
        for step, base_value, reason_value, action, patches in cases:
            with self.subTest(step=step), hostile_temp(self) as (shared, created), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"GH_TOKEN": base_value,
                                                                  "GH_PASSWORD": reason_value}))
                for patch in patches:
                    stack.enter_context(patch)
                with self.assertRaises(scratch.ScratchError) as caught:
                    action()
                self.assert_safe(caught.exception, (base_value, reason_value))
                self.assertEqual((created, list(shared.iterdir())), ([], []))
            self.assertFalse(folder.exists())
            self.assertTrue(root.is_dir())

    def test_construction_reads_current_exports_and_normalizes_newlines(self):
        for value in ("fixture-first-export-c4", "fixture-exported\r\nvalue-c4"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"GH_TOKEN": value}):
                error = scratch.ScratchError(pathlib.Path("readable-base") / value,
                                             "readable reason " + value.replace("\r\n", "\n"))
                self.assert_safe(error, (value,))
                self.assertIn("readable-base", error.base)
                self.assertIn("readable reason", error.reason)


class AliasCredentialTest(unittest.TestCase):
    def test_real_catalog_alias_refusals_are_redacted_without_tracebacks_in_both_modes(self):
        project = Project(self, config="[models]\nfetch_scores = false\n")
        project.bind()
        models_repo = project_with_fakes(self, "[models]\nfetch_scores = false\n")
        commands = [(models_repo, ["models", "--refresh"], 69),
                    (models_repo, ["recommend", "--engine", "claude", "--stage", "initial", "--kind", "implementation",
                                   "--intensity", "high", "--reasoning", "high", "--risk", "normal"], 1),
                    (project.repo, ["call", "claude", "--stage", "initial", "--task", "t1", "--tier", "high",
                                    "--refresh-models", "--launcher", "process", "--no-rerank"], 69)]
        value = "LOCALAPPDATA" if os.name == "nt" else "fixture-exported-alias-c4"
        for repo, argv, expected_exit in commands:
            for as_json in (False, True):
                with self.subTest(command=argv[0], as_json=as_json), credential_base(self, value):
                    env = dict(os.environ, OAH_STATE_DIR=str(project.state), CODEX_HOME=str(project.home),
                               FAKE_RECORD=str(project.record), FAKE_CLAUDE_ALIASES="sonnet", FAKE_CLAUDE_BARE="real",
                               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
                    if os.name == "nt":
                        env["LOCALAPPDATA"] = str(pathlib.Path("credential-parent") / value)
                    done = subprocess.run([sys.executable, "-B", "-m", "oah", *argv,
                                           *(["--json"] if as_json else [])], cwd=repo, env=env,
                                          capture_output=True, text=True, encoding="utf-8", timeout=30)
                    rendered = done.stdout + done.stderr
                    self.assertNotIn(value, rendered)
                    self.assertNotIn("Traceback", rendered)
                    self.assertTrue(done.stderr.startswith("OAH_SCRATCH_UNSAFE: "), done.stderr)
                    self.assertIn("[REDACTED]", done.stderr)
                    self.assertIn("then retry", done.stderr)
                    self.assertEqual(done.returncode, expected_exit, done.stderr)
                    self.assertEqual(done.stdout, "")
                    if os.name != "nt":
                        self.assertIn("credential-parent", done.stderr)
                        self.assertIn("the directory permits group or other accounts", done.stderr)
                        self.assertIn("chmod 700", done.stderr)
                    self.assertEqual(project.runs(), [])
                    self.assertFalse(project.record.exists())
