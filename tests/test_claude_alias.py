# Claude 별칭 확인 검사 — 가짜 claude 로 --bare 확인의 인자·환경·작업 폴더를 보고, 확인됨·대조 실패·별칭 태그·시간 초과·도움말에 이미 있음을 가른다.
from __future__ import annotations

import json
import os
import pathlib
import re
import unittest
from unittest import mock

from oah import call
from oah.engines import claude_alias
from oah.models import Catalog
from tests.helpers import make_tmp_dir
from tests.test_call import Project

CANARY = "sk-ant-canary-" + "0123456789abcdef"
# Every credential switch the check must drop, and an ordinary variable it must keep.
CREDENTIALS = {"ANTHROPIC_API_KEY": CANARY, "ANTHROPIC_AUTH_TOKEN": CANARY, "ANTHROPIC_BASE_URL": "https://x.example",
               "CLAUDE_CODE_OAUTH_TOKEN": CANARY, "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
               "CLAUDE_CODE_USE_FOUNDRY": "1", "CLAUDE_CODE_USE_SOMETHING_NEW": "1", "anthropic_api_key": CANARY}
KEPT = {"OAH_ALIAS_TEST_KEPT": "yes"}


class AliasCheckTest(unittest.TestCase):
    def check(self, mode: str | None, *, aliases: str | None = None, timeout: float | None = None):
        """A Claude catalog rebuild through the real path, with a fake claude answering --bare as `mode` says."""
        project = Project(self)
        record = make_tmp_dir(self) / "bare.jsonl"
        env = dict(project.env, **CREDENTIALS, **KEPT, FAKE_BARE_RECORD=str(record))
        if mode is not None:
            env["FAKE_CLAUDE_BARE"] = mode
        if aliases is not None:
            env["FAKE_CLAUDE_ALIASES"] = aliases
        with mock.patch.object(claude_alias, "TIMEOUT", timeout or claude_alias.TIMEOUT):
            _, freshness = call.check_catalog("claude", project.settings(), state_dir=project.state, env=env,
                                              refresh=True)
        runs = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()] if record.exists() else []
        return project, freshness.entry, runs

    def test_a_confirmed_alias_is_listed_and_stored(self):
        project, entry, runs = self.check("real")
        self.assertEqual([model.name for model in entry.models], ["fable", "opus", "sonnet", "haiku"])
        self.assertTrue(entry.model("haiku").listed)
        self.assertEqual(entry.model("haiku").efforts, entry.model("opus").efforts)
        self.assertEqual(entry.source, "claude --help + alias check")
        self.assertEqual(Catalog(project.state).load("claude"), entry, "launches read the stored result")
        self.assertEqual(len(runs), 2, "one control, then the one alias the help omits")
        control, alias = runs
        self.assertRegex(control["argv"][4], r"\Aoah-control-[0-9a-f]{8}\Z")
        for run, model in ((control, control["argv"][4]), (alias, "haiku")):
            self.assertEqual(run["argv"], ["--bare", "-p", "x", "--model", model, "--no-session-persistence",
                                           "--tools", ""])
            self.assertNotIn("--settings", run["argv"])
            self.assertEqual(run["entries"], [], "an empty folder")
            self.assertNotEqual(pathlib.Path(run["cwd"]).resolve(), project.repo.resolve())
            self.assertEqual(run["stdin"], "", "stdin is closed")
            names = {name.upper() for name in run["env"]}
            self.assertFalse({name.upper() for name in CREDENTIALS} & names, "no credential reaches the check")
            self.assertIn("OAH_ALIAS_TEST_KEPT", names, "the rest of the environment is kept")
        self.assertNotEqual(control["cwd"], alias["cwd"], "each run gets its own empty folder")
        self.assertFalse(pathlib.Path(alias["cwd"]).exists(), "the folder is removed afterwards")

    def test_unconfirmed_answers_leave_the_alias_out(self):
        for mode, runs_expected in (("untagged", 1), ("all-tagged", 2), ("logged-in", 1), ("zero-exit", 2),
                                    (None, 1)):
            with self.subTest(mode=mode):
                _project, entry, runs = self.check(mode)
                self.assertEqual([model.name for model in entry.models], ["fable", "opus", "sonnet"])
                self.assertEqual(entry.source, "claude --help")
                self.assertEqual(len(runs), runs_expected, "no alias runs once the control is not tagged")

    def test_a_timeout_leaves_the_alias_out_and_the_rebuild_stands(self):
        _project, entry, runs = self.check("sleep", timeout=1.0)
        self.assertEqual([model.name for model in entry.models], ["fable", "opus", "sonnet"])
        self.assertEqual([run["argv"][4] for run in runs][1:], ["haiku"])

    def test_an_alias_the_help_lists_is_not_checked(self):
        _project, entry, runs = self.check("real", aliases="haiku,sonnet,opus,fable")
        self.assertEqual(runs, [])
        self.assertEqual(entry.source, "claude --help")
        self.assertEqual({model.name for model in entry.models}, {"haiku", "sonnet", "opus", "fable"})

    def test_a_help_text_that_stops_parsing_runs_no_check(self):
        _project, entry, runs = self.check("real", aliases="-")
        self.assertEqual(runs, [])
        self.assertEqual(entry.models, ())

    def test_a_launch_that_rebuilds_nothing_runs_no_check(self):
        project, _entry, runs = self.check("real")
        record = make_tmp_dir(self) / "again.jsonl"
        env = dict(project.env, FAKE_CLAUDE_BARE="real", FAKE_BARE_RECORD=str(record))
        call.check_catalog("claude", project.settings(), state_dir=project.state, env=env)
        self.assertFalse(record.exists())

    def test_the_control_alias_is_new_each_time(self):
        names = {self.check("real")[2][0]["argv"][4] for _ in range(2)}
        self.assertEqual(len(names), 2)
        self.assertTrue(all(re.fullmatch(r"oah-control-[0-9a-f]{8}", name) for name in names))


class EnvironmentTest(unittest.TestCase):
    def test_the_child_environment_drops_every_credential_switch(self):
        env = dict(os.environ, **CREDENTIALS, **KEPT)
        kept = claude_alias.child_env(env)
        self.assertFalse({name.upper() for name in CREDENTIALS} & {name.upper() for name in kept})
        self.assertEqual(kept["OAH_ALIAS_TEST_KEPT"], "yes")
        self.assertEqual(env["ANTHROPIC_API_KEY"], CANARY, "the caller's environment is not changed")


if __name__ == "__main__":
    unittest.main()
