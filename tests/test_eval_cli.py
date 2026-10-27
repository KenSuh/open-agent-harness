# oah eval 명령 검사 — 명령 발견, 합성 픽스처의 사전 등록·실행·채점·보고 전 과정, 격리·홀드아웃 거부를 실제 진입점으로 본다.
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import re
import unittest

from oah import eval_cli
from oah.cli import _command_table, command_modules, main
from tests.helpers import make_git_repo, make_tmp_dir
from tests.test_eval import SUPERIORITY, case_dir, configure_fixture, fixture_copy, oracle_path, set_case, set_plan

ENV_KEYS = ("OAH_STATE_DIR", "FAKE_EVAL_MARKER", "FAKE_EVAL_RECORD_DIR", "FAKE_EVAL_EXIT")


def run_cli(cwd: pathlib.Path, *argv: str, env: dict | None = None):
    out, err = io.StringIO(), io.StringIO()
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    old = os.getcwd()
    os.chdir(cwd)
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(env or {})
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
    finally:
        os.chdir(old)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, out.getvalue(), err.getvalue()


class EvalCliTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_copy(self)
        self.repo = make_git_repo(self)
        base = make_tmp_dir(self)
        self.marker = base / "marker.txt"
        self.state = base / "state"
        self.env = {"OAH_STATE_DIR": str(self.state), "FAKE_EVAL_MARKER": str(self.marker)}
        configure_fixture(self.root, self.env)

    def cli(self, *argv: str):
        return run_cli(self.repo, *argv, env=self.env)

    def launched(self) -> list[str]:
        return self.marker.read_text(encoding="utf-8").split() if self.marker.exists() else []

    def test_eval_command_is_discovered(self):
        self.assertEqual(eval_cli.COMMANDS, ("eval",))
        self.assertIs(_command_table(command_modules())["eval"], eval_cli)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            main(["eval", "--help"])
        self.assertEqual(caught.exception.code, 0)
        for name in ("prereg", "run", "grade", "report"):
            self.assertIn(name, out.getvalue())

    def test_end_to_end_fixture(self):
        prereg = self.root / "prereg.json"
        code, out, err = self.cli("eval", "prereg", str(self.root / "plan.toml"), "--oracle",
                                  str(oracle_path(self.root)), "--out", str(prereg))
        self.assertEqual(code, 0, err)
        digest = (self.root / "prereg.json.sha256").read_text(encoding="utf-8").split()[0]
        self.assertIn(digest, out)

        code, out, err = self.cli("eval", "run", str(prereg))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.launched(), ["miss", "catch"])
        match = re.search(r"^OAH_EVAL_RUN_DONE run=(\S+) folder=(.+)$", out, re.MULTILINE)
        self.assertIsNotNone(match, out)
        run_dir = pathlib.Path(match.group(2))
        self.assertEqual(run_dir.parent, self.state / "eval" / "runs")
        self.assertIn("control-flow-only", out)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["prereg_sha256"], digest)

        code, out, err = self.cli("eval", "grade", str(run_dir), "--oracle", str(oracle_path(self.root)))
        self.assertEqual(code, 0, err)
        self.assertIn("baseline: matched 0 · missed 1 (D1) · unmatched 1", out)
        self.assertIn("F1 words.txt: needs-human-judgement", out)
        self.assertIn("improved: matched 1 · missed 0 · unmatched 0", out)
        self.assertNotIn("false-positive", out)

        code, out, err = self.cli("eval", "report", str(run_dir))
        self.assertEqual(code, 0, err)
        self.assertIn("split=control variant=baseline [control-flow-only]", out)
        self.assertIn("split=control variant=improved [control-flow-only]", out)
        self.assertIn("Limits", out)
        self.assertIn(digest[:12], out)
        self.assertIsNone(SUPERIORITY.search(out), out)

        code, out, err = self.cli("eval", "report", str(run_dir), "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["rows"]), 2)

    def test_isolation_refusal_via_cli(self):
        (case_dir(self.root) / "inputs" / "answers.toml").write_bytes(oracle_path(self.root).read_bytes())
        prereg = self.root / "prereg.json"
        self.assertEqual(self.cli("eval", "prereg", str(self.root / "plan.toml"), "--oracle",
                                  str(oracle_path(self.root)), "--out", str(prereg))[0], 0)
        code, out, err = self.cli("eval", "run", str(prereg))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_EVAL_ISOLATION"), err)
        self.assertIn("answers.toml", err)
        self.assertEqual(self.launched(), [])
        self.assertFalse((self.state / "eval" / "runs").exists() and any((self.state / "eval" / "runs").iterdir()))

    def test_holdout_refusals_via_cli(self):
        set_case(self.root, 'split = "control"', 'split = "holdout"')
        code, _, err = self.cli("eval", "run", str(self.root / "plan.toml"))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_EVAL_PREREG_REQUIRED"), err)
        set_plan(self.root, "used_for_development = false", "used_for_development = true")
        code, _, err = self.cli("eval", "prereg", str(self.root / "plan.toml"), "--oracle",
                                str(oracle_path(self.root)), "--out", str(self.root / "p.json"))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_EVAL_HOLDOUT_USED"), err)
        self.assertEqual(self.launched(), [])

    def test_missing_files_are_reported(self):
        code, _, err = self.cli("eval", "run", str(self.root / "nope.json"))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_EVAL_PLAN_INVALID"), err)
        code, _, err = self.cli("eval", "report", str(self.root / "no-run"))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_EVAL_RUN_INVALID"), err)


if __name__ == "__main__":
    unittest.main()
