# oah call·wait 검사 — 가짜 엔진으로 실행 폴더 전체, 작업자 환경, 단계 강제·우회 기록, 읽기 전용 리뷰, 단 출처, 헤더 낮춤, 시간 초과 시 프로세스 트리 종료, 백그라운드와 대기를 본다.
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import time
import types
import unittest
from collections import namedtuple
from unittest import mock

from oah import call, call_cli
from oah.briefs import BriefError
from oah.engines.effort import EngineError
from oah.lease import Store
from oah.paths import repo_info
from oah.routing import RoutingError
from oah.runs import RESULT_FIELDS, UNVERIFIED, find_run, new_run
from tests.helpers import git_commit, make_git_repo, make_tmp_dir

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_CODEX = [sys.executable, str(FAKES / "fake_codex.py")]
FAKE_CLAUDE = [sys.executable, str(FAKES / "fake_claude.py")]
MARKER = "<!-- OAH_INTENT_FOOTER -->"
FOOTER = f"{MARKER}\n## Intent rules\n- End with INTENT and AC lines.\n"
Intent = namedtuple("Intent", "section id revision digest")
SECTION = "<!-- OAH_INTENT:BEGIN -->\n## Intent\nIntent-ID: t1\nRevision: 2\nStatus: accepted\n<!-- OAH_INTENT:END -->\n"


def has_footer(text: str) -> bool:
    return any(line == MARKER for line in text.replace("\r\n", "\n").split("\n"))


def parse_intent(text: str) -> Intent:
    """A small stand-in for oah.intent.parse: LF-normalized section, its fields and its digest."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    begin, end = lines.index("<!-- OAH_INTENT:BEGIN -->"), lines.index("<!-- OAH_INTENT:END -->")
    section = "\n".join(lines[begin:end + 1]) + "\n"
    fields = dict(line.split(": ", 1) for line in lines[begin:end] if line.startswith(("Intent-ID: ", "Revision: ")))
    digest = "sha256:" + hashlib.sha256(section.encode("utf-8")).hexdigest()
    return Intent(section, fields["Intent-ID"], int(fields["Revision"]), digest)


def toml_list(argv: list[str]) -> str:
    return "[" + ", ".join(json.dumps(part) for part in argv) + "]"


class Project:
    """A temporary repository whose config points at the fake engines."""

    def __init__(self, test: unittest.TestCase, config: str = "") -> None:
        self.test = test
        self.repo = make_git_repo(test)
        git_commit(self.repo, {"README.md": "x\n"})
        work = make_tmp_dir(test)
        self.state, self.home, self.record = work / "state", work / "codex-home", work / "record.json"
        self.home.mkdir()
        (self.home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-test-a", "is_default": True,
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        ]}), encoding="utf-8")
        (self.repo / ".oah").mkdir()
        (self.repo / ".oah" / "config.toml").write_text(
            f"[engines.claude]\ncommand = {toml_list(FAKE_CLAUDE)}\n"
            f"[engines.codex]\ncommand = {toml_list(FAKE_CODEX)}\n" + config, encoding="utf-8")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("FAKE_", "OAH_"))}
        self.env.update(CODEX_HOME=str(self.home), FAKE_RECORD=str(self.record), OAH_STATE_DIR=str(self.state))

    def settings(self) -> call.Settings:
        return call_cli.load_settings(self.repo)

    def prepare(self, engine: str = "codex", stage: str = "review", env: dict | None = None, **fields):
        fields.setdefault("task", "t1")
        request = call.CallRequest(engine=engine, stage=stage, **fields)
        return call.prepare(request, self.settings(), root=self.repo, state_dir=self.state,
                            env=dict(self.env, **(env or {})), footer=FOOTER, has_footer=has_footer,
                            parse_intent=parse_intent)

    def run(self, engine: str = "codex", stage: str = "review", env: dict | None = None, **fields) -> dict:
        prepared = self.prepare(engine, stage, env=env, **fields)
        return call.execute(prepared, base_env=dict(self.env, **(env or {})))

    def recorded(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def packet(self, body: str = "", *, section: str = SECTION, name: str = ".oah/tasks/t1/PACKET.md",
               newline: str = "\n") -> pathlib.Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((section + body).replace("\n", newline).encode("utf-8"))
        return path

    def runs(self) -> list[str]:
        folder = self.state / "runs" / "t1"
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []


class RunFolderTest(unittest.TestCase):
    def test_call_writes_complete_run_folder(self):
        project = Project(self)
        data = project.run(tier="high", round_instructions="Review the change.")
        run = find_run(project.state, data["run_id"])
        self.assertEqual(sorted(p.name for p in run.path.iterdir()),
                         ["last-message.md", "prompt.md", "result.json", "stderr.log", "stdout.log"])
        self.assertEqual(set(data), set(RESULT_FIELDS))
        expected = {"status": "finished", "exit_code": 0, "timed_out": False, "engine": "codex", "stage": "review",
                    "cli_version": "0.163.0", "resolved_model": "gpt-test-a", "model_source": "cli_default",
                    "requested_effort": "high", "launch_effort": "high", "header_effort": "high",
                    "header_check": "match", "resolved_inference_effort": "high",
                    "normalization_basis": "codex-0.163-ordinary", "effort_verified": True, "tier": "high",
                    "tier_source": "flag", "read_only": True, "error": None, "advisory": "unused(tier-given)"}
        self.assertEqual({key: data[key] for key in expected}, expected)
        self.assertIsInstance(data["duration_s"], float)
        self.assertIn("read-only", data["command"])
        self.assertNotIn("workspace-write", data["command"])
        self.assertIn("fake codex done", run.stdout_path.read_text(encoding="utf-8"))
        self.assertIn("reasoning effort: high", run.stderr_path.read_text(encoding="utf-8"))

    def test_worker_environment(self):
        project = Project(self)
        data = project.run(tier="low", round_instructions="Do it.")
        record = project.recorded()
        self.assertEqual({key: record["env"].get(key) for key in call.WORKER_ENV},
                         {"OAH_WORKER": "1", "OAH_RUN_ID": data["run_id"], "OAH_TASK": "t1"})
        prompt = find_run(project.state, data["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertEqual(record["stdin"], prompt)
        self.assertTrue(prompt.startswith("You are a dispatched oah worker (OAH_WORKER=1, run " + data["run_id"]))
        self.assertTrue(prompt.endswith(FOOTER))
        self.assertEqual(pathlib.Path(record["cwd"]).resolve(), project.repo.resolve())

    def test_highest_resolves_from_supported_levels(self):
        data = Project(self).run(tier="max")
        self.assertEqual((data["requested_effort"], data["launch_effort"], data["header_check"]),
                         ("highest", "max", "match"))

    def test_correction_is_writable(self):
        data = Project(self).run(stage="correction", tier="low")
        self.assertIn("workspace-write", data["command"])
        self.assertFalse(data["read_only"])

    def test_stored_brief_delivery(self):
        project = Project(self)
        data = project.run(tier="low", stored_brief="An old spec that mentions " + MARKER + " inline.\n")
        prompt = find_run(project.state, data["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith("An old spec"))
        self.assertTrue(prompt.endswith(FOOTER))


class RoutingTest(unittest.TestCase):
    def test_settings_defaults(self):
        repo = make_git_repo(self)
        settings = call_cli.load_settings(repo)
        self.assertEqual(settings.stages, {"initial": "claude", "review": "codex", "correction": "codex"})
        claude, codex = settings.engines["claude"], settings.engines["codex"]
        self.assertEqual((claude["command"], claude["quality_order"], claude["permission_mode"]),
                         (["claude"], ["opus", "sonnet", "haiku"], "acceptEdits"))
        self.assertEqual((codex["command"], codex["quality_order"]), (["codex"], []))
        self.assertEqual({tier: (row["model"], row["effort"], row["timeout_min"]) for tier, row in claude["tiers"].items()},
                         {"low": ("", "low", 15), "medium": ("", "medium", 30), "high": ("", "high", 60),
                          "max": ("", "max", 90)})
        self.assertEqual({tier: row["effort"] for tier, row in codex["tiers"].items()},
                         {"low": "low", "medium": "medium", "high": "high", "max": "highest"})
        self.assertEqual(settings.models, {"ttl_hours": 24.0})
        self.assertEqual(settings, call.Settings.defaults())

    def test_settings_from_config_and_bad_values(self):
        project = Project(self, '[stages]\ninitial = "codex"\n[engines.codex.tiers.high]\nmodel = "gpt-pin"\n')
        settings = project.settings()
        self.assertEqual(settings.stages["initial"], "codex")
        self.assertEqual(settings.engines["codex"]["tiers"]["high"], {"model": "gpt-pin", "effort": "high",
                                                                      "timeout_min": 60})
        bad = Project(self, '[stages]\nreview = "gemini"\n')
        with self.assertRaises(EngineError) as ctx:
            bad.settings()
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")

    def test_single_engine_setup_warns(self):
        project = Project(self, '[stages]\ninitial = "codex"\nreview = "codex"\ncorrection = "codex"\n')
        data = project.run(stage="review", tier="low")
        self.assertTrue(any(w.startswith("OAH_REVIEW_NOT_INDEPENDENT") for w in data["warnings"]))
        default = Project(self).run(stage="review", tier="low")
        self.assertFalse(any(w.startswith("OAH_REVIEW_NOT_INDEPENDENT") for w in default["warnings"]))

    def test_stage_mismatch_refuses(self):
        project = Project(self)
        with self.assertRaises(RoutingError) as ctx:
            project.prepare(engine="claude", stage="review", tier="low")
        self.assertEqual(ctx.exception.code, "OAH_STAGE_ENGINE_MISMATCH")
        self.assertFalse((project.state / "runs").exists())

    def test_override_stage_is_recorded(self):
        project = Project(self)
        data = project.run(engine="claude", stage="review", tier="low", override_stage=True)
        self.assertTrue(data["override_stage"])
        self.assertTrue(any(w.startswith("OAH_STAGE_OVERRIDDEN") for w in data["warnings"]))
        events = [json.loads(line) for line in (project.state / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["event"], "note")
        self.assertIn(f"stage override: run {data['run_id']} runs claude for stage review", events[-1]["message"])

    def test_review_is_read_only_claude(self):
        project = Project(self)
        data = project.run(engine="claude", stage="review", tier="low", override_stage=True)
        argv = project.recorded()["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertTrue(data["read_only"])

    def test_claude_initial_records_unobservable_effort(self):
        project = Project(self)
        data = project.run(engine="claude", stage="initial", tier="high")
        argv = project.recorded()["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual((data["resolved_model"], data["model_source"], data["launch_effort"]),
                         ("opus", "quality_order", "high"))
        self.assertEqual((data["effort_observable"], data["header_check"], data["resolved_inference_effort"],
                          data["effort_verified"]), (False, UNVERIFIED, UNVERIFIED, False))
        self.assertEqual(data["observed_models"], ["claude-opus-fake"])
        self.assertIn("does not report", data["normalization_basis"])

    def test_claude_reported_effort_is_observed(self):
        project = Project(self)
        env = {"FAKE_CLAUDE_REPORT_EFFORT": "high"}
        data = project.run(engine="claude", stage="initial", tier="high", env=env)
        self.assertEqual((data["effort_observable"], data["header_check"], data["resolved_inference_effort"]),
                         (True, "match", "high"))

    def test_tier_sources_rules_packet_flag(self):
        project = Project(self)
        rules = project.run(stage="review")
        self.assertEqual((rules["tier_source"], rules["tier"]), ("rules", "medium"))
        self.assertEqual(rules["axes"], {"kind": "review", "intensity": "low", "reasoning": "medium", "risk": "normal"})
        self.assertTrue(any(w.startswith("OAH_SCOPE_EMPTY") for w in rules["warnings"]))
        self.assertEqual(rules["advisory"], "unused(not-configured)")
        flagged = project.run(stage="review", axes={"risk": "critical"})
        self.assertEqual((flagged["tier_source"], flagged["tier"]), ("flag", "max"))
        project.packet("## Scope\n- src/a.py\n## Plan\n- review → codex → high\n")
        packet = project.run(stage="review")
        self.assertEqual((packet["tier_source"], packet["tier"], packet["axes"]), ("packet", "high", None))
        prompt = find_run(project.state, packet["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertIn(SECTION, prompt)
        self.assertIn("Scope: src/a.py", prompt)
        self.assertNotIn("oah start", prompt)
        correction = project.run(stage="correction")
        prompt = find_run(project.state, correction["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertEqual(correction["tier_source"], "rules")
        self.assertIn("oah start --task t1 --agent codex --packet .oah/tasks/t1/PACKET.md", prompt)
        self.assertIn("binds INTENT t1@r2 sha256:", prompt)

    def test_rules_use_lease_scope_and_risk_paths(self):
        project = Project(self)
        Store(project.state).start(task="t1", agent="codex", scope=[".github/workflows/ci.yml"], note="",
                                   ttl_hours=1, info=repo_info(project.repo))
        data = project.run(stage="correction")
        self.assertEqual(data["axes"]["risk"], "high")
        self.assertEqual(data["tier"], "high")
        self.assertTrue(any("workflow" in reason for reason in data["tier_reasons"]))

    def test_advice_never_changes_tier(self):
        project = Project(self)
        for status, tier in (("used", "max"), ("used", "low")):
            advice = types.SimpleNamespace(status=status, tier=tier)
            module = types.SimpleNamespace(advise_difficulty=lambda axes, task, advice=advice: advice)
            with mock.patch.dict(sys.modules, {"oah.rerank": module}):
                data = project.run(stage="review")
            self.assertEqual((data["tier"], data["advisory"]), ("medium", f"used tier={tier}"))

        def broken(axes, task):
            raise RuntimeError("provider down")

        with mock.patch.dict(sys.modules, {"oah.rerank": types.SimpleNamespace(advise_difficulty=broken)}):
            data = project.run(stage="review")
        self.assertEqual((data["tier"], data["advisory"]), ("medium", "unused(error: RuntimeError)"))
        no_key = types.SimpleNamespace(advise_difficulty=lambda axes, task: types.SimpleNamespace(status="unused(no key)"))
        with mock.patch.dict(sys.modules, {"oah.rerank": no_key}):
            self.assertEqual(project.run(stage="review")["advisory"], "unused(no key)")

    def test_fresh_install_refuses_before_launch(self):
        project = Project(self)
        (project.home / "models_cache.json").unlink()
        with self.assertRaises(EngineError) as ctx:
            project.prepare(tier="high")
        self.assertEqual(ctx.exception.code, "OAH_MODEL_UNRESOLVED")
        self.assertFalse((project.state / "runs").exists())
        self.assertFalse(project.record.exists())

    def test_missing_footer_refuses_dispatch(self):
        project = Project(self)
        request = call.CallRequest(engine="codex", stage="review", task="t1", tier="low")
        with self.assertRaises(BriefError) as ctx:
            call.prepare(request, project.settings(), root=project.repo, state_dir=project.state, env=project.env,
                         footer="", has_footer=has_footer)
        self.assertEqual(ctx.exception.code, "OAH_FOOTER_NOT_APPENDED")
        self.assertFalse((project.state / "runs").exists())


NON_ASCII = SECTION.replace("Status: accepted\n", "Status: accepted\n### Problem\n한글 목표 — 이모지 \U0001f600 그대로.\n")


class IntentLineageTest(unittest.TestCase):
    def test_section_stays_byte_intact_with_non_ascii_and_crlf(self):
        project = Project(self)
        project.packet("## Scope\n- src/a.py\n", section=NON_ASCII, newline="\r\n")
        data = project.run(stage="review", tier="low")
        raw = NON_ASCII.replace("\n", "\r\n")
        prompt_bytes = find_run(project.state, data["run_id"]).prompt_path.read_bytes()
        self.assertEqual(prompt_bytes.count(raw.encode("utf-8")), 1)
        self.assertEqual(project.recorded()["stdin"].count(raw), 1)
        prompt = prompt_bytes.decode("utf-8")
        self.assertEqual(sum(1 for line in prompt.replace("\r\n", "\n").split("\n") if line == MARKER), 1)
        self.assertTrue(prompt.endswith(FOOTER))

    def test_swapped_source_refused_on_later_round(self):
        project = Project(self)
        project.packet()
        first = project.run(stage="review", tier="low")
        binding = json.loads((project.state / "runs" / "t1" / "intent-binding.json").read_text(encoding="utf-8"))
        self.assertEqual((binding["source"], binding["id"], binding["revision"]), (".oah/tasks/t1/PACKET.md", "t1", 2))
        other = project.packet(name="elsewhere/PACKET.md")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="low", packet=other)
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        self.assertIn("raise Revision of Intent-ID t1", str(ctx.exception))
        self.assertIn("--packet .oah/tasks/t1/PACKET.md", str(ctx.exception))
        self.assertEqual(project.runs(), [first["run_id"]])

    def test_in_place_edit_refused_and_new_revision_advances(self):
        project = Project(self)
        path = project.packet()
        project.run(stage="review", tier="low")
        path.write_text(SECTION.replace("Status: accepted", "Status: accepted\n### Problem\nedited"), encoding="utf-8")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="low")
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        self.assertIn("same revision with edited content", str(ctx.exception))
        path.write_text(SECTION.replace("Revision: 2", "Revision: 3"), encoding="utf-8")
        data = project.run(stage="correction", tier="low")
        self.assertEqual(data["status"], "finished")
        binding = json.loads((project.state / "runs" / "t1" / "intent-binding.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["revision"] for entry in binding["history"]], [2, 3])
        path.write_text(SECTION, encoding="utf-8")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="low")
        self.assertIn("an older revision", str(ctx.exception))

    def test_later_round_reuses_bound_source(self):
        project = Project(self)
        custom = project.packet("## Scope\n- docs/x.md\n", name="plans/T1.md")
        project.run(stage="review", tier="low", packet=custom)
        data = project.run(stage="correction", tier="low")
        prompt = find_run(project.state, data["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertIn("--packet plans/T1.md", prompt)
        self.assertIn("Scope: docs/x.md", prompt)
        custom.unlink()
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="low")
        self.assertEqual(ctx.exception.code, "OAH_PACKET_UNREADABLE")

    def test_stored_brief_with_other_intent_refused(self):
        project = Project(self)
        project.packet()
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="review", tier="low", stored_brief=SECTION.replace("Revision: 2", "Revision: 1"))
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        with self.assertRaises(EngineError):
            project.prepare(stage="review", tier="low", stored_brief="no intent here\n")
        data = project.run(stage="review", tier="low", stored_brief="Reused.\n\n" + SECTION.replace("\n", "\r\n"))
        self.assertEqual(data["status"], "finished")
        self.assertEqual(project.runs(), [data["run_id"]])


def add_worktree(repo: pathlib.Path, path: pathlib.Path, branch: str) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(path)], check=True,
                   capture_output=True)


class WorktreeSelectorTest(unittest.TestCase):
    def setUp(self):
        self.project = Project(self)
        git_commit(self.project.repo, {}, "config")
        base = make_tmp_dir(self)
        self.first, self.second = base / "a" / "wt", base / "b" / "wt"
        add_worktree(self.project.repo, self.first, "lane-x")
        add_worktree(self.project.repo, self.second, "lane-y")

    def test_ambiguous_selector_refused_before_launch(self):
        code, _, err = run_cli(self.project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low",
                               "--worktree", "wt")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_WORKTREE_UNRESOLVED"))
        self.assertIn("nothing was launched", err)
        for path in (self.first, self.second):
            self.assertIn(f"oah call codex --stage review --task t1 --tier low --worktree {path.resolve()}", err)
        self.assertFalse(self.project.record.exists())
        self.assertEqual(self.project.runs(), [])
        retry = next(line.strip() for line in err.splitlines() if str(self.first.resolve()) in line)
        code, out, err = run_cli(self.project, *retry.split()[1:])
        self.assertEqual(code, 0, err)
        self.assertEqual(pathlib.Path(self.project.recorded()["cwd"]).resolve(), self.first.resolve())

    def test_branch_and_unresolvable_selectors(self):
        root = call.resolve_worktree("lane-y", self.project.repo, retry=lambda path: f"retry {path}")
        self.assertEqual(root.resolve(), self.second.resolve())
        with self.assertRaises(EngineError) as ctx:
            call.resolve_worktree("nope", self.project.repo, retry=lambda path: f"retry {path}")
        self.assertEqual(ctx.exception.code, "OAH_WORKTREE_UNRESOLVED")
        self.assertIn(f"retry {self.first.resolve()}", str(ctx.exception))
        self.assertEqual(call.resolve_worktree(str(self.first), self.project.repo, retry=str).resolve(),
                         self.first.resolve())


class HeaderTest(unittest.TestCase):
    def test_header_downgrade_fails_run(self):
        data = Project(self).run(tier="high", env={"FAKE_CODEX_HEADER_EFFORT": "medium"})
        self.assertEqual((data["status"], data["header_check"], data["header_code"]),
                         ("failed", "downgraded", "OAH_EFFORT_DOWNGRADED"))
        self.assertTrue(data["error"].startswith("OAH_EFFORT_DOWNGRADED"))
        self.assertFalse(data["effort_verified"])

    def test_header_mismatch_fails_run(self):
        data = Project(self).run(tier="high", env={"FAKE_CODEX_HEADER_EFFORT": "max"})
        self.assertEqual((data["status"], data["header_code"]), ("failed", "OAH_EFFORT_MISMATCH"))

    def test_unreadable_header_is_unverified_not_a_match(self):
        data = Project(self).run(tier="high", env={"FAKE_CODEX_HEADER_EFFORT": "-"})
        self.assertEqual((data["status"], data["header_effort"], data["header_check"],
                          data["resolved_inference_effort"], data["effort_verified"]),
                         ("finished", UNVERIFIED, UNVERIFIED, UNVERIFIED, False))

    def test_unknown_cli_version_is_unverified(self):
        data = Project(self).run(tier="high", env={"FAKE_CODEX_VERSION": "0.170.0"})
        self.assertEqual((data["header_check"], data["resolved_inference_effort"], data["effort_verified"]),
                         ("match", UNVERIFIED, False))

    def test_engine_failure(self):
        data = Project(self).run(tier="low", env={"FAKE_EXIT": "3"})
        self.assertEqual((data["status"], data["exit_code"]), ("failed", 3))
        self.assertTrue(data["error"].startswith("OAH_ENGINE_FAILED"))


def wait_until_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not call._alive(pid):
            return True
        time.sleep(0.1)
    return False


class TimeoutAndBackgroundTest(unittest.TestCase):
    def test_timeout_kills_process_tree(self):
        project = Project(self)
        pidfile = make_tmp_dir(self) / "child.pid"
        env = {"FAKE_CHILD_PIDFILE": str(pidfile), "FAKE_SLEEP": "60"}
        started = time.monotonic()
        data = project.run(tier="low", env=env, timeout_s=2)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((data["status"], data["timed_out"], data["exit_code"]), ("timed_out", True, None))
        self.assertTrue(data["error"].startswith("OAH_RUN_TIMEOUT"))
        grandchild = int(pidfile.read_text(encoding="utf-8"))
        self.assertTrue(wait_until_gone(grandchild), f"grandchild {grandchild} survived the timeout")

    def test_background_and_wait(self):
        project = Project(self)
        prepared = project.prepare(tier="low")
        run_id = call.start_background(prepared, base_env=project.env)
        self.assertIn(find_run(project.state, run_id).read()["status"], ("queued", "running", "finished"))
        data = call.wait(project.state, run_id, timeout=60)
        self.assertEqual(data["status"], "finished", data.get("error"))
        self.assertEqual(call.wait(project.state, run_id, timeout=0), data)
        record = project.recorded()
        self.assertEqual(record["env"]["OAH_RUN_ID"], run_id)

    def test_wait_is_bounded_and_can_be_called_again(self):
        project = Project(self)
        prepared = project.prepare(tier="low", env={"FAKE_SLEEP": "4"}, timeout_s=30)
        run_id = call.start_background(prepared, base_env=dict(project.env, FAKE_SLEEP="4"))
        started = time.monotonic()
        first = call.wait(project.state, run_id, timeout=0.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn(first["status"], ("queued", "running"))
        self.assertEqual(call.WAIT_LIMIT_S, 540)
        final = call.wait(project.state, run_id, timeout=60)
        self.assertEqual(final["status"], "finished", final.get("error"))

    @unittest.skipUnless(os.name == "nt", "Windows process query")
    def test_alive_reports_windows_processes(self):
        self.assertTrue(call._alive(os.getpid()))
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.assertFalse(call._alive(gone.pid))

    def test_lost_runner(self):
        state = make_tmp_dir(self)
        run = new_run(state, "t1", "codex", "review")
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        run.result(status="running", pid=gone.pid)
        data = call.wait(state, run.id, timeout=5)
        self.assertEqual(data["status"], "lost")
        self.assertTrue(data["error"].startswith("OAH_RUN_LOST"))


def run_cli(project: Project, *argv: str) -> tuple[int, str, str]:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    call_cli.register(commands)
    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    os.chdir(project.repo)
    try:
        with mock.patch.dict(os.environ, project.env, clear=True), \
                mock.patch.object(call_cli, "intent_tools",
                                  return_value=call_cli.IntentTools(FOOTER, has_footer, parse_intent, call._read_text)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = call_cli.run(parser.parse_args(list(argv)))
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class CallCliTest(unittest.TestCase):
    def test_cli_call_json(self):
        project = Project(self)
        code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low",
                                 "--message", "hello", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["status"], data["tier"], data["tier_source"]), ("finished", "low", "flag"))
        self.assertIn("hello", project.recorded()["stdin"])

    def test_cli_background_then_wait(self):
        project = Project(self)
        code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low",
                                 "--background")
        self.assertEqual(code, 0, err)
        run_id = out.split("run=", 1)[1].split()[0]
        self.assertIn(f"oah wait {run_id}", out)
        code, out, err = run_cli(project, "wait", run_id, "--timeout-seconds", "60")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"OAH_RUN_FINISHED run={run_id}"))

    def test_cli_refusals(self):
        project = Project(self)
        code, _, err = run_cli(project, "call", "claude", "--stage", "review", "--tier", "low")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_STAGE_ENGINE_MISMATCH"))
        code, _, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "low", "--risk", "high")
        self.assertEqual(code, 2)
        code, _, err = run_cli(project, "wait", "nonsense")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_RUN_ID_INVALID"))

    def test_cli_config_error_is_environment(self):
        project = Project(self, "[engines.gemini]\ncommand = [\"x\"]\n")
        code, _, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "low")
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_CONFIG_UNKNOWN_KEY"))

    @unittest.skipIf(importlib.util.find_spec("oah.intent"), "oah.intent exists, so the footer is available")
    def test_cli_without_intent_module_refuses(self):
        project = Project(self)
        parser = argparse.ArgumentParser()
        call_cli.register(parser.add_subparsers(dest="command"))
        err = io.StringIO()
        previous = os.getcwd()
        os.chdir(project.repo)
        try:
            with mock.patch.dict(os.environ, project.env, clear=True), contextlib.redirect_stderr(err):
                code = call_cli.run(parser.parse_args(["call", "codex", "--stage", "review", "--tier", "low"]))
        finally:
            os.chdir(previous)
        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue().startswith("OAH_FOOTER_NOT_APPENDED"))


if __name__ == "__main__":
    unittest.main()
