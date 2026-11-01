# oah call·wait 검사 — 가짜 엔진으로 실행 폴더 전체, 작업자 환경, 단계 강제·우회 기록, 읽기 전용 리뷰, 단 출처, 헤더 낮춤, 시간 초과 시 프로세스 트리 종료, 백그라운드와 대기를 본다.
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import secrets as token_source
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

from oah import call, call_cli, intent, rerank
from oah.briefs import VERIFY_FOOTER, VERIFY_MARKER, BriefError, raw_section
from oah.engines.effort import EngineError
from oah.lease import Store
from oah.paths import repo_info
from oah.routing import RoutingError
from oah.runs import RESULT_FIELDS, UNVERIFIED, find_run, new_run
from oah.secrets import NoKeychain
from tests.helpers import directory_alias, git_commit, make_git_repo, make_tmp_dir

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_CODEX = [sys.executable, str(FAKES / "fake_codex.py")]
FAKE_CLAUDE = [sys.executable, str(FAKES / "fake_claude.py")]
MARKER = "<!-- OAH_INTENT_FOOTER -->"
FOOTER = f"{MARKER}\n## Intent rules\n- End with INTENT and AC lines.\n"
SECTION = ("<!-- OAH_INTENT:BEGIN -->\n## Intent\nIntent-ID: t1\nRevision: 2\nStatus: accepted\n"
           "### Problem\nReports miss files.\n### Desired outcome\nEvery file is listed.\n"
           "### Success conditions\n- AC1: the report lists every file\n### Constraints\n"
           "### Non-goals\n### Open questions\n<!-- OAH_INTENT:END -->\n")


def has_footer(text: str) -> bool:
    return any(line == MARKER for line in text.replace("\r\n", "\n").split("\n"))


def parse_intent(text: str) -> intent.Intent:
    """Use the landed parser for launch fixtures as well as binding checks."""
    return intent.parse(text)


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
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("FAKE_", "OAH_")) and key != "CLAUDE_CONFIG_DIR"}
        self.env.update(CODEX_HOME=str(self.home), FAKE_RECORD=str(self.record), OAH_STATE_DIR=str(self.state),
                        HOME=str(work / "home"), USERPROFILE=str(work / "home"))

    def settings(self) -> call.Settings:
        return call_cli.load_settings(self.repo)

    def bind(self, task: str = "t1", *, root: pathlib.Path | None = None, path: pathlib.Path | None = None):
        """Establish a real accepted lease for positive launch tests; never refresh an existing binding."""
        root = root or self.repo
        store = Store(self.state)
        holder = next((lease for lease, _ in store.snapshot() if lease.task == task), None)
        if holder is not None and holder.binding() is not None:
            return holder
        path = path or root / f".oah/tasks/{task}/PACKET.md"
        path = path if path.is_absolute() else root / path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SECTION.replace("Intent-ID: t1", f"Intent-ID: {task}"), encoding="utf-8", newline="")
        bound = intent.parse(intent.read_source(path)).bind(path.relative_to(root).as_posix())
        if holder is not None:
            return store.rebind(task=task, agent=holder.agent, binding=bound, ttl_hours=1)[0]
        return store.start(task=task, agent="codex", scope=(f".oah/tasks/{task}",), note="launch fixture",
                           ttl_hours=1, info=repo_info(root), intent=bound)

    def prepare(self, engine: str = "codex", stage: str = "review", env: dict | None = None, **fields):
        fields.setdefault("task", "t1")
        self.bind(fields["task"], path=fields.get("packet"))
        request = call.CallRequest(engine=engine, stage=stage, **fields)
        return call.prepare(request, self.settings(), root=self.repo, state_dir=self.state,
                            env=dict(self.env, **(env or {})), footer=FOOTER, has_footer=has_footer,
                            parse_intent=parse_intent, read_source=intent.read_source, keychain=NoKeychain())

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
    def test_packet_fixture_helpers_keep_exact_line_endings(self):
        project = Project(self)
        project.bind()
        self.assertEqual((project.repo / ".oah/tasks/t1/PACKET.md").read_bytes(), SECTION.encode("utf-8"))
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                path = project.packet(newline=newline)
                self.assertEqual(path.read_bytes(), SECTION.replace("\n", newline).encode("utf-8"))

    def test_legacy_spec_lease_commands_warn_in_every_stage(self):
        project = Project(self)
        spec = project.repo / "legacy-spec.md"
        for stage, engine in (("initial", "claude"), ("correction", "codex"), ("review", "codex")):
            for command in ("start", "renew", "finish"):
                with self.subTest(stage=stage, command=command):
                    spec.write_bytes((f"Before proceeding, run `oah {command} --task t1`.\n" + SECTION).encode("utf-8"))
                    code, _, err = run_cli(project, "call", engine, "--stage", stage, "--task", "t1",
                                           "--tier", "low", "--brief-file", str(spec))
                    self.assertEqual(code, 0, err)
                    self.assertIn("OAH_WORKER_LEASE_COMMAND", err)
                    self.assertIn("remove", err)
                    self.assertIn(f"oah {command}", project.recorded()["stdin"])

    def test_windows_spec_lease_commands_warn_in_every_stage(self):
        project = Project(self)
        spec = project.repo / "windows-spec.md"
        for stage, engine in (("initial", "claude"), ("correction", "codex"), ("review", "codex")):
            for executable in ("oah.exe", "OAH.EXE", "OaH.ExE"):
                for command in ("start", "renew", "finish"):
                    with self.subTest(stage=stage, executable=executable, command=command):
                        instruction = f"Run `{executable} {command} --task t1`.\n"
                        spec.write_bytes((instruction + SECTION).encode("utf-8"))
                        code, _, err = run_cli(project, "call", engine, "--stage", stage, "--task", "t1",
                                               "--tier", "low", "--brief-file", str(spec))
                        self.assertEqual(code, 0, err)
                        self.assertIn("OAH_WORKER_LEASE_COMMAND", err)
                        self.assertIn(instruction, project.recorded()["stdin"])

    def test_stored_spec_gets_the_current_worker_envelope(self):
        project = Project(self)
        data = project.run(tier="low", stored_brief="Review this reusable spec.\n" + SECTION)
        prompt = project.recorded()["stdin"]
        self.assertTrue(prompt.startswith("You are a dispatched oah worker (OAH_WORKER=1, run " + data["run_id"]))
        self.assertIn("Stage: review · Engine: codex", prompt)
        self.assertIn("## Round instructions\nReview this reusable spec.", prompt)
        self.assertEqual(prompt.count(VERIFY_MARKER), 1)
        self.assertTrue(prompt.endswith(VERIFY_FOOTER + "\n" + FOOTER))
        self.assertNotIn("oah start", prompt)

    def test_reused_implementation_brief_refreshes_review_envelope(self):
        project = Project(self)
        project.packet("## Scope\n- src/a.py\n")
        initial = project.prepare(engine="claude", stage="initial", tier="low", round_instructions="Inspect AC1.")
        stored = initial.run.prompt_path.read_bytes().decode("utf-8")
        data = project.run(tier="low", stored_brief=stored, round_instructions="Review the current diff.")
        prompt = project.recorded()["stdin"]
        self.assertTrue(prompt.startswith("You are a dispatched oah worker (OAH_WORKER=1, run " + data["run_id"]))
        self.assertNotIn(initial.run_id, prompt)
        self.assertNotIn("Stage: initial", prompt)
        self.assertNotIn("Agent-Engine", prompt)
        self.assertNotIn("oah start", prompt)
        self.assertEqual(prompt.count(SECTION), 1)
        self.assertIn("Inspect AC1.", prompt)
        self.assertIn("Review the current diff.", prompt)
        self.assertEqual(prompt.count(VERIFY_MARKER), 1)

    def test_stored_review_spec_with_lease_instruction_warns(self):
        project = Project(self)
        prepared = project.prepare(tier="low", stored_brief="Run oah start --task old --agent claude before reviewing.\n"
                                   + SECTION)
        self.assertTrue(any(warning.startswith("OAH_WORKER_LEASE_COMMAND") for warning in prepared.warnings))
        self.assertFalse(project.record.exists())

    def test_refresh_failure_keeps_launch_version_separate_from_catalog(self):
        project = Project(self)
        project.prepare(tier="low")
        (project.home / "models_cache.json").unlink()
        measured_old_rule = [{"id": "old-version-only", "engine": "codex", "cli_versions": ["0.163."],
                              "models": ["gpt-test-a"], "map": {"low": "low"}}]
        for version, expected in (("0.170.0", "0.170.0"), ("unknown", None)):
            with self.subTest(version=version):
                prepared = project.prepare(tier="low", env={"FAKE_CODEX_VERSION": version})
                self.assertEqual(prepared.cli_version, expected)
                data = call.execute(prepared, base_env=project.env, rules=measured_old_rule)
                self.assertEqual(data["cli_version"], expected or UNVERIFIED)
                self.assertEqual(data["resolved_model"], "gpt-test-a")
                self.assertFalse(data["effort_verified"])
                self.assertEqual(data["resolved_inference_effort"], UNVERIFIED)
        self.assertEqual(call.models.Catalog(project.state).load("codex").cli_version, "0.163.0")

    def test_call_writes_complete_run_folder(self):
        project = Project(self)
        data = project.run(tier="high", round_instructions="Review the change.")
        run = find_run(project.state, data["run_id"])
        self.assertEqual(sorted(p.name for p in run.path.iterdir()),
                         ["last-message.md", "lease.json", "prompt.md", "result.json", "skills.json", "stderr.log",
                          "stdout.log"])
        self.assertEqual(set(data), set(RESULT_FIELDS))
        expected = {"status": "finished", "exit_code": 0, "timed_out": False, "engine": "codex", "stage": "review",
                    "cli_version": "0.163.0", "resolved_model": "gpt-test-a", "model_source": "cli_default",
                    "requested_effort": "high", "launch_effort": "high", "header_effort": "high",
                    "header_check": "match", "resolved_inference_effort": UNVERIFIED,
                    "normalization_basis": "unverified: no normalization rule for codex 0.163.0 gpt-test-a effort high",
                    "effort_verified": False, "tier": "high",
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
        data = project.run(tier="low", stored_brief="An old spec that mentions " + MARKER + " inline.\n" + SECTION)
        prompt = find_run(project.state, data["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertIn("## Round instructions\nAn old spec", prompt)
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
        self.assertIn("Scope: .oah/tasks/t1", project.recorded()["stdin"])
        self.assertEqual(rules["advisory"], "unused(not-configured)")
        flagged = project.run(stage="review", axes={"risk": "critical"})
        self.assertEqual((flagged["tier_source"], flagged["tier"]), ("flag", "max"))
        project.packet("## Scope\n- src/a.py\n## Plan\n- review → codex → high\n")
        packet = project.run(stage="review")
        self.assertEqual((packet["tier_source"], packet["tier"], packet["axes"]), ("packet", "high", None))
        prompt = find_run(project.state, packet["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertIn(SECTION, prompt)
        self.assertIn("Scope: .oah/tasks/t1", prompt)  # the lease still limits this worker's writes
        self.assertNotIn("oah start", prompt)
        correction = project.run(stage="correction")
        prompt = find_run(project.state, correction["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertEqual(correction["tier_source"], "rules")
        self.assertNotIn("oah start", prompt)
        self.assertIn("lease belongs to the dispatcher", prompt)
        self.assertIn("Binding: INTENT t1@r2 sha256:", prompt)

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
            advice = types.SimpleNamespace(status=status, advisory_tier=tier)
            module = types.SimpleNamespace(advise_difficulty=lambda axes, task, *, root, advice=advice, **kw: advice)
            with mock.patch.dict(sys.modules, {"oah.rerank": module}):
                data = project.run(stage="review")
            self.assertEqual((data["tier"], data["advisory"]), ("medium", f"used tier={tier}"))

        def broken(axes, task, *, root, **kw):
            raise RuntimeError("provider down")

        with mock.patch.dict(sys.modules, {"oah.rerank": types.SimpleNamespace(advise_difficulty=broken)}):
            data = project.run(stage="review")
        self.assertEqual((data["tier"], data["advisory"]), ("medium", "unused(error: RuntimeError)"))
        no_key = types.SimpleNamespace(
            advise_difficulty=lambda axes, task, *, root, **kw: types.SimpleNamespace(status="unused(no key)"))
        with mock.patch.dict(sys.modules, {"oah.rerank": no_key}):
            self.assertEqual(project.run(stage="review")["advisory"], "unused(no key)")

    def test_real_advice_records_provider_tier_without_changing_launch_tier(self):
        project = Project(self, '[rerank]\nenabled = true\nprovider = "fake"\n')
        provider = types.SimpleNamespace(name="fake", key_name=lambda config: "FAKE_RERANK_KEY",
                                         advise=mock.Mock())
        project.env["FAKE_RERANK_KEY"] = "fake-" + token_source.token_hex(16)
        with contextlib.chdir(project.repo), \
                mock.patch.dict(rerank.PROVIDERS, {"fake": provider}):
            for tier in ("max", "low"):
                with self.subTest(advisory_tier=tier):
                    provider.advise.reset_mock()
                    provider.advise.return_value = (tier, 0.8)
                    data = project.run(stage="review")
                    self.assertEqual((data["tier"], data["tier_source"], data["advisory"]),
                                     ("medium", "rules", f"used tier={tier}"))
                    self.assertEqual(data["launch_effort"], "medium")
                    provider.advise.assert_called_once()

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


NON_ASCII = SECTION.replace("Reports miss files.", "한글 목표 — 이모지 \U0001f600 그대로.")


class IntentLineageTest(unittest.TestCase):
    def test_unsafe_packet_can_be_renamed_and_retried_for_the_same_task(self):
        project = Project(self)
        source = project.packet(name="my packets/PACKET.md")
        with self.assertRaises(BriefError) as ctx:
            project.prepare(stage="correction", tier="low", packet=source)
        self.assertEqual(ctx.exception.code, "OAH_UNSAFE_ARGUMENT")
        self.assertIsNone(call.dispatch_binding(project.state, "t1"))
        self.assertFalse(project.record.exists())

        renamed = source.parent.with_name("my-packets")
        source.parent.rename(renamed)
        path = renamed / source.name
        path.write_text(path.read_text(encoding="utf-8").replace("Revision: 2", "Revision: 3"), encoding="utf-8", newline="")
        Store(project.state).rebind(task="t1", agent="codex", ttl_hours=1,
                                    binding=intent.parse(intent.read_source(path)).bind("my-packets/PACKET.md"))
        data = project.run(stage="correction", tier="low", packet=renamed / source.name)
        self.assertEqual(data["status"], "finished")
        self.assertEqual(call.dispatch_binding(project.state, "t1")["source"], "my-packets/PACKET.md")
        self.assertIn("Packet: my-packets/PACKET.md", project.recorded()["stdin"])

    def test_stored_brief_delivers_source_intent_bytes_on_stdin(self):
        project = Project(self)
        source = project.packet("## Scope\n- src/a.py\n", section=NON_ASCII, newline="\r\n")
        section = raw_section(source.read_bytes().decode("utf-8"))
        stored = "Reusable spec\r\n" + section + FOOTER.replace("\n", "\r\n")
        data = project.run(tier="low", stored_brief=stored)
        delivered = project.recorded()["stdin"].encode("utf-8")
        self.assertEqual(raw_section(delivered.decode("utf-8")).encode("utf-8"), section.encode("utf-8"))
        self.assertEqual(find_run(project.state, data["run_id"]).prompt_path.read_bytes(), delivered)

    def test_stored_brief_with_changed_intent_line_endings_refuses(self):
        project = Project(self)
        project.packet(newline="\r\n")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(tier="low", stored_brief=SECTION)
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")

    def test_linked_task_directory_cannot_redirect_intent_binding(self):
        project = Project(self)
        project.packet()
        outside = make_tmp_dir(self)
        (project.state / "runs").mkdir(parents=True)
        directory_alias(outside, project.state / "runs" / "t1")
        with self.assertRaises(call.runs.RunError):
            project.prepare(tier="low")
        self.assertEqual(list(outside.iterdir()), [])

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
        self.assertIn("new revision of the bound packet", str(ctx.exception))
        self.assertIn("--packet .oah/tasks/t1/PACKET.md", str(ctx.exception))
        self.assertEqual(project.runs(), [first["run_id"]])

    def test_in_place_edit_refused_and_new_revision_advances(self):
        project = Project(self)
        path = project.packet()
        project.run(stage="review", tier="low")
        path.write_text(SECTION.replace("Reports miss files.", "edited"), encoding="utf-8", newline="")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="low")
        self.assertEqual(ctx.exception.code, "OAH_INTENT_NOT_CURRENT")
        self.assertIn("digest differs", str(ctx.exception))
        path.write_text(SECTION.replace("Revision: 2", "Revision: 3"), encoding="utf-8", newline="")
        Store(project.state).rebind(task="t1", agent="codex", ttl_hours=1,
                                    binding=intent.parse(intent.read_source(path)).bind(".oah/tasks/t1/PACKET.md"))
        data = project.run(stage="correction", tier="low")
        self.assertEqual(data["status"], "finished")
        binding = json.loads((project.state / "runs" / "t1" / "intent-binding.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["revision"] for entry in binding["history"]], [2, 3])
        path.write_text(SECTION, encoding="utf-8", newline="")
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="low")
        self.assertEqual(ctx.exception.code, "OAH_INTENT_NOT_CURRENT")
        self.assertIn("revision is 2, bound 3", str(ctx.exception))

    def test_later_round_reuses_bound_source(self):
        project = Project(self)
        custom = project.packet("## Scope\n- docs/x.md\n", name="plans/T1.md")
        project.run(stage="review", tier="low", packet=custom)
        data = project.run(stage="correction", tier="low")
        prompt = find_run(project.state, data["run_id"]).prompt_path.read_text(encoding="utf-8")
        self.assertIn("Packet: plans/T1.md", prompt)
        self.assertIn("Scope: .oah/tasks/t1", prompt)
        custom.unlink()
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="correction", tier="low")
        self.assertEqual(ctx.exception.code, "OAH_INTENT_NOT_CURRENT")

    def test_stored_brief_with_other_intent_refused(self):
        project = Project(self)
        project.packet()
        with self.assertRaises(EngineError) as ctx:
            project.prepare(stage="review", tier="low", stored_brief=SECTION.replace("Revision: 2", "Revision: 1"))
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        with self.assertRaises(EngineError):
            project.prepare(stage="review", tier="low", stored_brief="no intent here\n")
        data = project.run(stage="review", tier="low", stored_brief="Reused.\n\n" + SECTION)
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
        for task in ("t1", "adhoc"):
            self.project.bind(task, root=self.first)

    def test_mutating_selector_refuses_a_sibling_of_the_leased_tree(self):
        source = ".oah/tasks/t1/PACKET.md"
        packet = self.second / source
        packet.parent.mkdir(parents=True, exist_ok=True)
        packet.write_bytes((self.first / source).read_bytes())
        before = (self.project.state / "leases.json").read_bytes()
        for stage, engine in (("initial", "claude"), ("correction", "codex")):
            with self.subTest(stage=stage):
                code, _, err = run_cli(self.project, "call", engine, "--stage", stage, "--task", "t1",
                                       "--tier", "low", "--worktree", str(self.second))
                self.assertEqual(code, 1, err)
                self.assertIn("OAH_WORKTREE_LEASE_MISMATCH", err)
                self.assertIn("--worktree", err)
                self.assertFalse(self.project.record.exists())
                self.assertEqual((self.project.state / "leases.json").read_bytes(), before)

    def test_advice_uses_target_worktree_rerank_settings(self):
        config = (self.project.repo / ".oah" / "config.toml").read_text(encoding="utf-8")
        self.project.env["FAKE_RERANK_KEY"] = "fake-" + token_source.token_hex(16)
        provider = types.SimpleNamespace(name="fake", key_name=lambda config: "FAKE_RERANK_KEY",
                                         advise=mock.Mock(return_value=("medium", 0.8)))
        for invoking_enabled, target_enabled in ((True, False), (False, True)):
            with self.subTest(invoking_enabled=invoking_enabled, target_enabled=target_enabled):
                for root, enabled in ((self.project.repo, invoking_enabled), (self.first, target_enabled)):
                    (root / ".oah" / "config.toml").write_text(
                        config + f'[rerank]\nenabled = {str(enabled).lower()}\nprovider = "fake"\n',
                        encoding="utf-8")
                provider.advise.reset_mock()
                with mock.patch.dict(rerank.PROVIDERS, {"fake": provider}):
                    code, out, err = run_cli(self.project, "call", "codex", "--stage", "review", "--task", "t1",
                                             "--worktree", str(self.first), "--json")
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                self.assertEqual((data["tier"], data["advisory"]),
                                 ("medium", "used tier=medium" if target_enabled else "unused(not-configured)"))
                self.assertEqual(pathlib.Path(self.project.recorded()["cwd"]).resolve(), self.first.resolve())
                if target_enabled:
                    provider.advise.assert_called_once()
                else:
                    provider.advise.assert_not_called()

    def test_ambiguous_selector_refused_before_launch(self):
        code, _, err = run_cli(self.project, "call", "codex", "--stage", "review", "--task", "t1", "--tier", "low",
                               "--worktree", "wt")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_WORKTREE_UNRESOLVED"))
        self.assertIn("nothing was launched", err)
        stored = [json.loads(path.read_text(encoding="utf-8")) for path in (self.project.state / "requests").glob("*.json")]
        self.assertEqual({item["worktree"] for item in stored}, {str(self.first.resolve()), str(self.second.resolve())})
        self.assertFalse(self.project.record.exists())
        self.assertEqual(self.project.runs(), [])
        retry = next(line.strip() for line in err.splitlines() if line.strip().startswith("oah call "))
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

    def test_retry_reproduces_stored_request_and_free_text_exactly(self):
        text = "quotes ' \" ’ $ ` % ! spaces 한글\r\nsecond line\n"
        for source in ("message", "file", "stdin"):
            with self.subTest(source=source):
                prompt = self.project.repo / "round instructions.txt"
                spec = self.project.repo / "reused brief.txt"
                prompt.write_bytes(text.encode("utf-8"))
                stored = "Reusable spec.\r\n" + SECTION
                spec.write_bytes(stored.encode("utf-8"))
                extra = {"message": ["--message", text], "file": ["--prompt-file", str(prompt)],
                         "stdin": ["--prompt-file", "-"]}[source]
                stdin = types.SimpleNamespace(buffer=io.BytesIO(text.encode("utf-8")))
                with mock.patch.object(call_cli.sys, "stdin", stdin):
                    code, _, err = run_cli(self.project, "call", "codex", "--stage", "review", "--task", "t1",
                                           "--tier", "low", "--timeout-seconds", "19", "--brief-file", str(spec),
                                           "--worktree", "wt", *extra)
                self.assertEqual(code, 1, err)
                retry = next(line.strip() for line in err.splitlines() if line.strip().startswith("oah call "))
                words = retry.split()
                self.assertEqual(words[:6], ["oah", "call", "codex", "--stage", "review", "--request-id"])
                self.assertRegex(words[6], r"^q[0-9a-f]{24}$")
                saved = json.loads((self.project.state / "requests" / (words[6] + ".json")).read_text(encoding="utf-8"))
                self.assertEqual(saved["message"], text)
                prompt.unlink()
                spec.unlink()
                with mock.patch.object(call, "prepare", wraps=call.prepare) as prepare:
                    code, _, err = run_cli(self.project, *words[1:])
                self.assertEqual(code, 0, err)
                request = prepare.call_args.args[0]
                self.assertEqual(request.round_instructions, text)
                self.assertEqual(request.stored_brief, stored)
                self.assertEqual((request.task, request.tier, request.timeout_s), ("t1", "low", 19))
                self.assertIn(text, self.project.recorded()["stdin"])

    def test_saved_request_preserves_saved_or_explicit_require_detach(self):
        for saved_required, retry_required in ((False, True), (True, False), (False, False)):
            with self.subTest(saved_required=saved_required, retry_required=retry_required):
                code, _, err = run_cli(self.project, "call", "codex", "--stage", "review", "--tier", "low",
                                       "--background", "--worktree", "wt",
                                       *(["--require-detach"] if saved_required else []))
                self.assertEqual(code, 1, err)
                retry = next(line.strip() for line in err.splitlines() if line.strip().startswith("oah call "))
                with mock.patch.object(call, "start_background", return_value="test-run") as start:
                    code, _, err = run_cli(self.project, *retry.split()[1:],
                                           *(["--require-detach"] if retry_required else []))
                self.assertEqual(code, 0, err)
                self.assertEqual(start.call_args.kwargs["require_detach"], saved_required or retry_required)

    def test_retry_refuses_unsafe_embedded_value(self):
        args = argparse.Namespace(engine="codex’;Write-Output-INJECTED", stage="review")
        with self.assertRaises(BriefError) as ctx:
            call_cli.retry_command(args, "safe/path")
        self.assertEqual(ctx.exception.code, "OAH_UNSAFE_ARGUMENT")


def real_packet(task: str = "t1", *, revision: int = 1, goal: str = "the report lists every file",
                scope: str = "src/app.py", constraints: str = "") -> str:
    """A packet the real oah.intent parser accepts."""
    lines = [intent.BEGIN, "## Intent", f"Intent-ID: {task}", f"Revision: {revision}", "Status: accepted",
             "### Problem", "Reports miss files.", "### Desired outcome", "Every file is listed.",
             "### Success conditions", f"- AC1: {goal}", "### Constraints", *([constraints] if constraints else []),
             "### Non-goals", "### Open questions", intent.END, "## Scope", f"- {scope}", "## Plan",
             "## Acceptance", "## Return", ""]
    return "\n".join(lines)


class LeaseBindingTest(unittest.TestCase):
    """`oah call` and the lease's intent binding (lane P4b): refusal, bound source, skills in the brief."""

    def setUp(self):
        self.project = Project(self)
        skills = self.project.home.parent / "home" / ".claude" / "skills"
        for name, description in (("test-driven-development", "write the test first"),
                                  ("verification-before-completion", "evidence before claims"),
                                  ("receiving-code-review", "check each finding")):
            folder = skills / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nBody.\n",
                                             encoding="utf-8")

    def prepare(self, **fields):
        fields.setdefault("task", "t1")
        fields.setdefault("tier", "low")
        stage = fields.pop("stage", "correction")
        request = call.CallRequest(engine="codex", stage=stage, **fields)
        return call.prepare(request, self.project.settings(), root=self.project.repo, state_dir=self.project.state,
                            env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer,
                            parse_intent=intent.parse, read_source=intent.read_source, keychain=NoKeychain())

    def bind(self, name: str = ".oah/tasks/t1/PACKET.md", **packet) -> pathlib.Path:
        path = self.project.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(real_packet(**packet), encoding="utf-8", newline="")
        binding = intent.parse(path.read_text(encoding="utf-8")).bind(name)
        Store(self.project.state).start(task="t1", agent="claude", scope=("src",), note="", ttl_hours=1,
                                        info=repo_info(self.project.repo), intent=binding)
        return path

    def prompt(self, prepared) -> str:
        return pathlib.Path(prepared.run_dir, "prompt.md").read_bytes().decode("utf-8")

    def test_packet_fixture_keeps_the_source_bytes_used_by_briefs(self):
        path = self.bind()
        self.assertEqual(path.read_bytes(), real_packet().encode("utf-8"))
        prepared = self.prepare()
        self.assertIn(raw_section(real_packet()).encode("utf-8"), prepared.run.prompt_path.read_bytes())

    def test_dispatched_worker_finish_is_refused_and_dispatcher_lease_survives(self):
        self.bind()
        prepared = self.prepare()
        returned = Store(self.project.state).inspect("t1").binding().line() + "\nAC1: met — checked\n"
        command = [sys.executable, "-m", "oah", "finish", "--task", "t1", "--agent", "claude",
                   "--summary", "worker tried finish", "--return", "-", "--allow-out-of-scope"]
        worker = ("import json, subprocess, sys; sys.stdin.read(); "
                  f"result = subprocess.run({command!r}, input={returned!r}, capture_output=True, "
                  "text=True, encoding='utf-8'); "
                  "print(json.dumps({'code': result.returncode, 'out': result.stdout, 'err': result.stderr}))")
        prepared = call.dataclasses.replace(prepared, argv=(sys.executable, "-c", worker))
        paths = [self.project.state / name for name in ("leases.json", "events.jsonl")]
        before = [path.read_bytes() for path in paths]
        env = dict(self.project.env, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent / "src"))
        result = call.execute(prepared, base_env=env)
        self.assertEqual(result["status"], "finished", result)
        child = json.loads(prepared.run.stdout_path.read_text(encoding="utf-8"))
        self.assertEqual(child["code"], 1, child)
        self.assertTrue(child["err"].startswith("OAH_WORKER_LEASE_REFUSED"), child)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(Store(self.project.state).inspect("t1").agent, "claude")

    def test_mutating_dispatch_accepts_case_alias_of_leased_directory(self):
        self.bind()
        alias = self.project.repo.with_name(self.project.repo.name.upper())
        if not alias.is_dir() or not os.path.samefile(alias, self.project.repo):
            self.skipTest("this filesystem does not provide a case alias")
        for stage, engine in (("initial", "claude"), ("correction", "codex")):
            with self.subTest(stage=stage):
                request = call.CallRequest(engine=engine, stage=stage, task="t1", tier="low")
                prepared = call.prepare(request, self.project.settings(), root=alias, state_dir=self.project.state,
                                        env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer,
                                        keychain=NoKeychain())
                self.assertEqual(call.execute(prepared, base_env=self.project.env)["status"], "finished")

    def test_mutating_dispatch_refuses_distinct_case_named_directories(self):
        self.bind()
        other = self.project.repo.with_name(self.project.repo.name.upper())
        if other.exists():
            self.skipTest("this filesystem does not support distinct case names")
        add_worktree(self.project.repo, other, "case-distinct")
        target = other / ".oah/tasks/t1/PACKET.md"
        target.parent.mkdir(parents=True)
        target.write_bytes((self.project.repo / ".oah/tasks/t1/PACKET.md").read_bytes())
        self.assertFalse(os.path.samefile(other, self.project.repo))
        request = call.CallRequest(engine="codex", stage="correction", task="t1", tier="low")
        with self.assertRaises(EngineError) as caught:
            call.prepare(request, self.project.settings(), root=other, state_dir=self.project.state,
                         env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer)
        self.assertEqual(caught.exception.code, "OAH_WORKTREE_LEASE_MISMATCH")
        self.assertEqual(self.project.runs(), [])

    def test_mutating_prepare_refuses_a_sibling_with_the_same_intent(self):
        path = self.bind()
        sibling = make_tmp_dir(self) / "sibling"
        add_worktree(self.project.repo, sibling, "sibling")
        target = sibling / path.relative_to(self.project.repo)
        target.parent.mkdir(parents=True)
        target.write_bytes(path.read_bytes())
        request = call.CallRequest(engine="codex", stage="correction", task="t1", tier="low")
        with self.assertRaises(EngineError) as caught:
            call.prepare(request, self.project.settings(), root=sibling, state_dir=self.project.state,
                         env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer)
        self.assertEqual(caught.exception.code, "OAH_WORKTREE_LEASE_MISMATCH")
        self.assertEqual(self.project.runs(), [])

    def test_mutating_launch_refuses_an_out_of_scope_edit_in_a_sibling(self):
        path = self.bind()
        sibling = make_tmp_dir(self) / "sibling"
        add_worktree(self.project.repo, sibling, "sibling")
        target = sibling / path.relative_to(self.project.repo)
        target.parent.mkdir(parents=True)
        target.write_bytes(path.read_bytes())
        prepared = self.prepare()
        worker = (sys.executable, "-c", "import pathlib, sys; sys.stdin.read(); "
                  "pathlib.Path('outside.py').write_bytes(b'escaped scope\\n')")
        prepared = call.dataclasses.replace(prepared, cwd=str(sibling), argv=worker)
        with self.assertRaises(EngineError) as caught:
            call.execute(prepared, base_env=self.project.env)
        self.assertEqual(caught.exception.code, "OAH_WORKTREE_LEASE_MISMATCH")
        self.assertEqual(prepared.run.read()["status"], "refused")
        self.assertFalse((sibling / "outside.py").exists())
        self.assertFalse((self.project.repo / "outside.py").exists())

    def test_prepare_refuses_lease_changes_during_engine_resolution(self):
        for change in ("release", "replace", "rebind", "renew", "gates", "scope", "source"):
            with self.subTest(change=change):
                self.project = Project(self)
                path = self.bind()
                store = Store(self.project.state)
                checked = store.inspect("t1")
                resolve = call.resolve_engine

                def changed(*args, **kwargs):
                    resolution = resolve(*args, **kwargs)
                    if change in ("release", "replace"):
                        store.release(task="t1", agent="claude", reason="resolution race", force=False)
                        if change == "replace":
                            store.start(task="t1", agent="claude", scope=checked.scope, note="replacement",
                                        ttl_hours=1, info=repo_info(self.project.repo), intent=checked.binding())
                    elif change == "rebind":
                        store.rebind(task="t1", agent="claude", binding=checked.binding(), ttl_hours=1)
                    elif change == "source":
                        path.write_text(real_packet(goal="changed during resolution"), encoding="utf-8", newline="")
                    else:
                        extra = {"gates": ["checks/late.md"]} if change == "gates" else (
                            {"scope": ["src", "tests"]} if change == "scope" else {})
                        store.renew(task="t1", agent="claude", ttl_hours=1, **extra)
                    return resolution

                with mock.patch.object(call, "resolve_engine", side_effect=changed):
                    with self.assertRaises(EngineError) as caught:
                        self.prepare()
                self.assertEqual(caught.exception.code,
                                 "OAH_INTENT_NOT_CURRENT" if change == "source" else "OAH_LEASE_CHANGED")
                self.assertIn("again", str(caught.exception))
                self.assertIsNone(call.dispatch_binding(self.project.state, "t1"))
                self.assertFalse(self.project.record.exists())

    def test_execute_and_background_dispatch_refuse_changed_leases(self):
        for launch in ("foreground", "background", "runner"):
            for change in ("renew", "release", "source"):
                with self.subTest(launch=launch, change=change):
                    self.project = Project(self)
                    path = self.bind()
                    prepared = self.prepare()
                    store = Store(self.project.state)
                    if change == "renew":
                        store.renew(task="t1", agent="claude", ttl_hours=1)
                    elif change == "release":
                        store.release(task="t1", agent="claude", reason="queued race", force=False)
                    else:
                        path.write_text(real_packet(goal="changed while queued"), encoding="utf-8", newline="")
                    expected = "OAH_INTENT_NOT_CURRENT" if change == "source" else "OAH_LEASE_CHANGED"
                    if launch == "runner":
                        prepared.run.launch_path.write_text(json.dumps(prepared.to_json()), encoding="utf-8")
                        result = subprocess.run(call._boot_argv("background_main", prepared.run_dir),
                                                cwd=self.project.repo, env=self.project.env, capture_output=True,
                                                text=True, encoding="utf-8", timeout=30)
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn(expected, result.stderr)
                    else:
                        function = call.execute if launch == "foreground" else call.start_background
                        with self.assertRaises(EngineError) as caught:
                            function(prepared, base_env=self.project.env)
                        self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(prepared.run.read()["status"], "refused")
                    self.assertFalse(self.project.record.exists())

    def test_background_launcher_preserves_a_refusal_after_queuing(self):
        self.bind()
        prepared = self.prepare()
        launch = subprocess.run

        def changed(*args, **kwargs):
            Store(self.project.state).renew(task="t1", agent="claude", ttl_hours=1)
            return launch(*args, **kwargs)

        with mock.patch.object(call.subprocess, "run", side_effect=changed):
            with self.assertRaises(EngineError) as caught:
                call.start_background(prepared, base_env=self.project.env)
        self.assertEqual(caught.exception.code, "OAH_LEASE_CHANGED")
        self.assertEqual(prepared.run.read()["status"], "refused")
        self.assertFalse(self.project.record.exists())

    def test_spawn_holds_the_state_lock_but_engine_execution_releases_it(self):
        self.bind()
        prepared = self.prepare()
        script = (
            "import pathlib, sys\n"
            "from oah.lease import Store\n"
            "from oah.lockfile import LockTimeout, StateLock\n"
            "try:\n"
            "    store = Store(pathlib.Path(sys.argv[1]))\n"
            "    with StateLock(store.lock_path, timeout=0.15):\n"
            "        assert store.snapshot()[0][0].task == 't1'\n"
            "except LockTimeout:\n"
            "    print('locked')\n"
            "else:\n"
            "    print('unlocked')\n")
        argv = (sys.executable, "-c", script, str(self.project.state))
        prepared = call.dataclasses.replace(prepared, argv=argv)
        popen = subprocess.Popen
        environment = dict(self.project.env, PYTHONPATH=str(FAKES.parent.parent / "src"))

        def spawning(*args, **kwargs):
            contender = popen(argv, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = contender.communicate(timeout=10)
            self.assertEqual(contender.returncode, 0, err.decode("utf-8"))
            self.assertEqual(out.strip(), b"locked", "the actual spawn must hold the lease's state lock")
            return popen(*args, **kwargs)

        with mock.patch.object(call.subprocess, "Popen", side_effect=spawning):
            result = call.execute(prepared, base_env=environment)
        self.assertEqual(result["status"], "finished", result.get("error"))
        self.assertIn("unlocked", prepared.run.stdout_path.read_text(encoding="utf-8"))

    def test_call_refuses_stale_and_missing_binding(self):
        path = self.bind()
        self.assertTrue(self.prompt(self.prepare()))
        path.write_text(real_packet(goal="a different goal"), encoding="utf-8", newline="")
        for case in ("stale", "missing"):
            with self.subTest(case=case):
                if case == "missing":
                    path.unlink()
                before = self.project.runs()
                with self.assertRaises(EngineError) as ctx:
                    self.prepare(stage="review")
                self.assertEqual(ctx.exception.code, "OAH_INTENT_NOT_CURRENT")
                self.assertIn(f"is {case}", str(ctx.exception))
                self.assertIn("oah start --task t1 --agent claude --packet .oah/tasks/t1/PACKET.md", str(ctx.exception))
                self.assertEqual(self.project.runs(), before)

    def test_call_requires_a_current_lease_binding(self):
        for leased in (False, True):
            for packet_status in (None, "accepted", "draft"):
                with self.subTest(leased=leased, packet_status=packet_status):
                    project = Project(self)
                    if packet_status is not None:
                        project.packet(section=real_packet().replace("Status: accepted",
                                                                    f"Status: {packet_status}"))
                    if leased:
                        Store(project.state).start(task="t1", agent="claude", scope=("src",), note="",
                                                    ttl_hours=1, info=repo_info(project.repo))
                    request = call.CallRequest(engine="codex", stage="review", task="t1", tier="low")
                    with self.assertRaises(EngineError) as caught:
                        call.prepare(request, project.settings(), root=project.repo, state_dir=project.state,
                                     env=project.env, footer=intent.FOOTER, has_footer=intent.has_footer,
                                     parse_intent=intent.parse, read_source=intent.read_source,
                                     keychain=NoKeychain())
                    self.assertEqual(caught.exception.code, "OAH_INTENT_NOT_CURRENT")
                    self.assertIn("oah start --task t1", str(caught.exception))
                    self.assertEqual(project.runs(), [])
                    self.assertFalse(project.record.exists())

    def test_call_refuses_a_bound_source_changed_to_draft(self):
        path = self.bind()
        path.write_text(path.read_text(encoding="utf-8").replace("Status: accepted", "Status: draft"),
                        encoding="utf-8", newline="")
        with self.assertRaises(EngineError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "OAH_INTENT_NOT_CURRENT")
        self.assertEqual(self.project.runs(), [])

    def test_dispatch_history_does_not_replace_a_live_lease(self):
        self.bind()
        self.prepare()
        before = self.project.runs()
        Store(self.project.state).release(task="t1", agent="claude", reason="task stopped", force=False)
        self.assertIsNotNone(call.dispatch_binding(self.project.state, "t1"))
        with self.assertRaises(EngineError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "OAH_INTENT_NOT_CURRENT")
        self.assertEqual(self.project.runs(), before)

    def test_explicit_draft_cannot_bypass_checks_when_parser_is_omitted(self):
        self.bind()
        draft = self.project.packet(name="plans/draft.md", section=real_packet().replace("Status: accepted",
                                                                                       "Status: draft"))
        request = call.CallRequest(engine="codex", stage="review", task="t1", tier="low", packet=draft)
        with self.assertRaises(EngineError) as caught:
            call.prepare(request, self.project.settings(), root=self.project.repo, state_dir=self.project.state,
                         env=self.project.env, footer=intent.FOOTER, has_footer=intent.has_footer,
                         keychain=NoKeychain())
        self.assertEqual(caught.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        self.assertEqual(self.project.runs(), [])

    def test_call_uses_lease_binding_source(self):
        self.bind("plans/T1.md")
        prompt = self.prompt(self.prepare())
        self.assertIn("- Packet: plans/T1.md", prompt)
        self.assertEqual(prompt.count(intent.BEGIN), 1)
        other = self.project.repo / ".oah" / "tasks" / "t1" / "PACKET.md"
        other.parent.mkdir(parents=True)
        other.write_text(real_packet(), encoding="utf-8", newline="")
        with self.assertRaises(EngineError) as ctx:
            self.prepare(packet=other)
        self.assertEqual(ctx.exception.code, "OAH_INTENT_SOURCE_MISMATCH")
        self.assertIn("plans/T1.md", str(ctx.exception))

    def test_brief_lists_skills_with_reasons_and_rerank(self):
        self.bind()
        prepared = self.prepare()
        prompt = self.prompt(prepared)
        self.assertIn("- Skills: test-driven-development, receiving-code-review, verification-before-completion "
                      "(reasons in ## Skills below)", prompt)
        skills = prompt.split("## Skills\n", 1)[1].split(intent.BEGIN, 1)[0]
        self.assertIn("Skill order: rerank=unused(not-enabled)", skills)
        self.assertIn("- `test-driven-development` (user): `~/.claude/skills/test-driven-development/SKILL.md`", skills)
        self.assertIn("  - why: code changes are written test-first (kind implementation, stage correction)", skills)
        self.assertIn("  - why: corrections check each review finding before acting on it (stage correction)", skills)
        record = json.loads(pathlib.Path(prepared.run_dir, "skills.json").read_text(encoding="utf-8"))
        self.assertEqual(record["rerank"], "unused(not-enabled)")
        self.assertEqual([skill["id"] for skill in record["skills"]],
                         ["test-driven-development", "receiving-code-review", "verification-before-completion"])

    def test_brief_keeps_intent_section_and_footer_byte_intact_with_skills(self):
        path = self.bind()
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        Store(self.project.state).release(task="t1", agent="claude", reason="rebind CRLF copy", force=False)
        binding = intent.parse(path.read_bytes().decode("utf-8")).bind(".oah/tasks/t1/PACKET.md")
        Store(self.project.state).start(task="t1", agent="claude", scope=("src",), note="", ttl_hours=1,
                                        info=repo_info(self.project.repo), intent=binding)
        prompt = self.prompt(self.prepare())
        section = raw_section(path.read_bytes().decode("utf-8"))
        self.assertEqual(prompt.count(section), 1)
        self.assertIn("\r\n", section)
        self.assertTrue(prompt.endswith(intent.FOOTER))
        self.assertEqual(sum(line == intent.FOOTER_MARKER for line in prompt.replace("\r\n", "\n").split("\n")), 1)
        self.assertLess(prompt.index("## Skills"), prompt.index(section))

    def test_no_rerank_flag_and_packet_constraint(self):
        project = self.project
        (project.repo / ".oah" / "config.toml").write_text(
            (project.repo / ".oah" / "config.toml").read_text(encoding="utf-8")
            + '[rerank]\nenabled = true\nprovider = "fake"\n', encoding="utf-8")
        project.env["FAKE_RERANK_KEY"] = "fake-" + token_source.token_hex(16)
        provider = types.SimpleNamespace(name="fake", key_name=lambda config: "FAKE_RERANK_KEY",
                                         score=mock.Mock(side_effect=lambda kind, query, items, *, key, config:
                                                         list(range(len(items)))),
                                         advise=mock.Mock(return_value=("max", 0.8)))
        path = self.bind()
        with mock.patch.dict(rerank.PROVIDERS, {"fake": provider}):
            used = self.prompt(self.prepare(tier=None))
            self.assertIn("Skill order: rerank=used", used)
            order = used.split("- Skills: ", 1)[1].split(" (reasons", 1)[0]
            self.assertEqual(order, "verification-before-completion, receiving-code-review, test-driven-development")
            provider.score.assert_called_once()
            provider.advise.assert_called_once()
            provider.score.reset_mock()
            provider.advise.reset_mock()
            disabled = self.prepare(tier=None, no_rerank=True)
            self.assertIn("rerank=unused(disabled-by-brief)", self.prompt(disabled))
            self.assertEqual(disabled.run.read()["advisory"], "unused(disabled-by-brief)")
            provider.score.assert_not_called()
            provider.advise.assert_not_called()
            path.write_text(real_packet(revision=2, constraints="No re-ranker for this task."), encoding="utf-8", newline="")
            Store(project.state).rebind(task="t1", agent="claude", ttl_hours=1,
                                        binding=intent.parse(path.read_text(encoding="utf-8")).bind(
                                            ".oah/tasks/t1/PACKET.md"))
            disabled = self.prepare(tier=None)
            self.assertIn("rerank=unused(disabled-by-brief)", self.prompt(disabled))
            self.assertEqual(disabled.run.read()["advisory"], "unused(disabled-by-brief)")
            provider.score.assert_not_called()
            provider.advise.assert_not_called()
        secret = project.env["FAKE_RERANK_KEY"]
        for path in project.state.rglob("*"):
            if path.is_file():
                self.assertFalse(secret.encode() in path.read_bytes(), f"runtime key leaked into {path.name}")


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
    def test_denied_breakaway_fallback_completes_a_real_background_run(self):
        project = Project(self)
        prepared = project.prepare(tier="low")
        popen = subprocess.Popen

        def spawn(*args, **kwargs):
            flags = kwargs.pop("creationflags", 0)
            if flags & 0x01000000:
                raise PermissionError("breakaway denied")
            if os.name == "nt":
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            return popen(*args, **kwargs)

        def launch(argv, **kwargs):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(call, "_windows", return_value=True), \
                    mock.patch.object(call.subprocess, "Popen", side_effect=spawn), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = call.launch_main(prepared.run_dir)
            return subprocess.CompletedProcess(argv, code, out.getvalue().encode(), err.getvalue().encode())

        with mock.patch.object(call.subprocess, "run", side_effect=launch), \
                mock.patch.dict(os.environ, project.env, clear=True), contextlib.redirect_stderr(io.StringIO()) as err:
            run_id = call.start_background(prepared, base_env=project.env)
        data = call.wait(project.state, run_id, timeout=30)
        self.assertEqual((data["status"], data["detached"]), ("finished", "unverified"))
        self.assertIn("closing this terminal may stop the run", err.getvalue())
        self.assertTrue(any("--require-detach" in warning for warning in data["warnings"]))

    def test_runner_can_finish_before_launcher_returns(self):
        project = Project(self)
        prepared = project.prepare(tier="low")
        launch = subprocess.run

        def complete_before_return(*args, **kwargs):
            result = launch(*args, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(call.wait(project.state, prepared.run_id, timeout=20)["status"], "finished")
            return result

        with mock.patch.object(call.subprocess, "run", side_effect=complete_before_return):
            call.start_background(prepared, base_env=project.env)
        data = call.wait(project.state, prepared.run_id, timeout=0)
        self.assertEqual((data["status"], data["exit_code"]), ("finished", 0))
        self.assertIsNotNone(data["finished_at"])

    def test_windows_breakaway_denial_falls_back_and_records_unverified(self):
        project = Project(self)
        prepared = project.prepare(tier="low")
        from oah.atomic import write_json_atomic
        write_json_atomic(prepared.run.launch_path, dict(prepared.to_json(), require_detach=False))
        child = types.SimpleNamespace(pid=12345, returncode=None)
        real_popen = subprocess.Popen
        launcher = call._boot_argv("background_main", prepared.run_dir)

        def spawn(argv, **kwargs):
            if list(argv) != launcher:
                return real_popen(argv, **kwargs)
            if kwargs["creationflags"] & 0x01000000:
                raise PermissionError("breakaway denied")
            return child

        with mock.patch.object(call, "_windows", return_value=True), \
                mock.patch.object(call.subprocess, "Popen", side_effect=spawn) as popen, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = call.launch_main(prepared.run_dir)
        self.assertEqual(code, 0)
        launches = [item for item in popen.call_args_list if list(item.args[0]) == launcher]
        self.assertEqual(len(launches), 2)
        self.assertTrue(launches[0].kwargs["creationflags"] & 0x01000000)
        self.assertFalse(launches[1].kwargs["creationflags"] & 0x01000000)
        data = prepared.run.read()
        self.assertEqual(data["detached"], "unverified")
        self.assertIn("closing this terminal may stop the run", data["warnings"][-1])
        self.assertIn("--require-detach", err.getvalue())

    def test_require_detach_refuses_when_windows_denies_breakaway(self):
        project = Project(self)
        prepared = project.prepare(tier="low")
        from oah.atomic import write_json_atomic
        write_json_atomic(prepared.run.launch_path, dict(prepared.to_json(), require_detach=True))
        real_popen = subprocess.Popen
        launcher = call._boot_argv("background_main", prepared.run_dir)

        def spawn(argv, **kwargs):
            if list(argv) == launcher:
                raise PermissionError("breakaway denied")
            return real_popen(argv, **kwargs)

        with mock.patch.object(call, "_windows", return_value=True), \
                mock.patch.object(call.subprocess, "Popen", side_effect=spawn) as popen, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = call.launch_main(prepared.run_dir)
        self.assertEqual(code, call_cli.EXIT_ENVIRONMENT)
        self.assertEqual(sum(list(item.args[0]) == launcher for item in popen.call_args_list), 1)
        self.assertEqual(prepared.run.read()["detached"], "unverified")
        self.assertIn("OAH_RUN_DETACH_FAILED", err.getvalue())
        self.assertIn("without --require-detach", err.getvalue())

    def test_verified_detachment_requires_successful_launch(self):
        project = Project(self)
        prepared = project.prepare(tier="low")
        from oah.atomic import write_json_atomic
        write_json_atomic(prepared.run.launch_path, dict(prepared.to_json(), require_detach=True))
        real_popen = subprocess.Popen
        launcher = call._boot_argv("background_main", prepared.run_dir)

        def spawn(argv, **kwargs):
            if list(argv) == launcher:
                return types.SimpleNamespace(pid=12345)
            return real_popen(argv, **kwargs)

        with mock.patch.object(call, "_windows", return_value=True), \
                mock.patch.object(call.subprocess, "Popen", side_effect=spawn), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(call.launch_main(prepared.run_dir), 0)
        self.assertEqual(prepared.run.read()["detached"], "verified")

    @unittest.skipUnless(os.name == "nt", "native Windows denied-breakaway job")
    def test_background_finishes_inside_job_that_denies_breakaway(self):
        from oah.engines.proc import run_command

        project = Project(self)
        for required in (False, True):
            with self.subTest(require_detach=required):
                prepared = project.prepare(tier="low")
                script = ("import sys; sys.path.insert(0,sys.argv[1]); from oah import call; "
                          "from oah.atomic import read_json; from pathlib import Path; "
                          "p=call.Prepared.from_json(read_json(Path(sys.argv[2]),None)); "
                          f"call.start_background(p,require_detach={required!r}); "
                          "data=call.wait(Path(p.run_dir).parents[2],p.run_id,timeout=30); "
                          "raise SystemExit(0 if data['status']=='finished' else 1)")
                from oah.atomic import write_json_atomic
                write_json_atomic(prepared.run.launch_path, prepared.to_json())
                result = run_command([sys.executable, "-c", script, str(pathlib.Path(call.__file__).parent.parent),
                                      str(prepared.run.launch_path)], timeout=45, env=project.env)
                self.assertEqual(prepared.run.read()["detached"], "unverified")
                if required:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("OAH_RUN_DETACH_FAILED", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(prepared.run.read()["status"], "finished")
                    self.assertIn("closing this terminal may stop the run", result.stderr)

    @unittest.skipUnless(os.name == "nt", "native Windows kill-on-close jobs")
    def test_background_survives_originating_job_close(self):
        import ctypes
        from ctypes import wintypes
        from oah.atomic import write_json_atomic
        from oah.engines.proc import _WindowsJob

        project = Project(self)
        prepared = project.prepare(tier="low", timeout_s=30)
        write_json_atomic(prepared.run.launch_path, prepared.to_json())
        ready = make_tmp_dir(self) / "origin-ready"
        # Give the originating process a kill-on-close job that explicitly permits breakaway.
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        job = _WindowsJob()
        self.addCleanup(job.close)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        self.assertTrue(job.api.SetInformationJobObject(job.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        script = ("import pathlib,sys,time; sys.path.insert(0,sys.argv[3]); "
                  "from oah import call; from oah.atomic import read_json; "
                  "p=call.Prepared.from_json(read_json(pathlib.Path(sys.argv[1]),None)); "
                  "call.start_background(p); pathlib.Path(sys.argv[2]).touch(); time.sleep(60)")
        origin = subprocess.Popen([sys.executable, "-c", script, str(prepared.run.launch_path), str(ready),
                                   str(pathlib.Path(call.__file__).resolve().parent.parent)],
                                  env=dict(project.env, FAKE_SLEEP="3"), creationflags=0x4,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: origin.kill() if origin.poll() is None else None)
        self.addCleanup(origin.stderr.close)
        try:
            job.start(origin)
            deadline = time.monotonic() + 20
            while not ready.exists() and origin.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not ready.exists():
                origin.kill()
                self.fail(origin.communicate(timeout=10)[1].decode("utf-8", errors="replace"))
            data = prepared.run.read()
            if data["detached"] == "unverified":
                self.assertEqual(call.wait(project.state, prepared.run_id, timeout=30)["status"], "finished")
                self.skipTest("The host job denies breakaway; fallback completed but survival is unverified")
            self.assertEqual(data["detached"], "verified")
            self.assertNotIn(data["status"], ("finished", "failed"))
        finally:
            job.close()  # kills the origin; the detached runner must outlive it
        origin.wait(timeout=10)
        self.assertEqual(call.wait(project.state, prepared.run_id, timeout=30)["status"], "finished")

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
    args = parser.parse_args(list(argv))
    if args.command == "call" and not args.worktree and not args.request_id:
        project.bind(args.task, path=pathlib.Path(args.packet) if args.packet else None)
    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    os.chdir(project.repo)
    try:
        with mock.patch.dict(os.environ, project.env, clear=True), \
                mock.patch.object(call_cli, "intent_tools",
                                  return_value=call_cli.IntentTools(FOOTER, has_footer, parse_intent, call._read_text)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = call_cli.run(args)
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class CallCliTest(unittest.TestCase):
    def test_require_detach_flag_is_forwarded_and_requires_background(self):
        project = Project(self)
        with mock.patch.object(call, "start_background", return_value="test-run") as start:
            code, _, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "low",
                                   "--background", "--require-detach")
        self.assertEqual(code, 0, err)
        self.assertTrue(start.call_args.kwargs["require_detach"])
        code, _, err = run_cli(project, "call", "codex", "--stage", "review", "--require-detach")
        self.assertEqual(code, 2)
        self.assertIn("--background", err)

    def test_detachment_refusal_is_an_environment_error(self):
        project = Project(self)
        with mock.patch.object(call, "start_background", side_effect=EngineError(
                "OAH_RUN_DETACH_FAILED", "job breakaway denied; retry without --background")):
            code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "low",
                                     "--background")
        self.assertEqual(code, call_cli.EXIT_ENVIRONMENT)
        self.assertIn("without --background", err)
        self.assertNotIn("OAH_RUN_STARTED", out)

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
