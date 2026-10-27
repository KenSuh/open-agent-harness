# Orca 실행기 검사 — 가짜 orca 로 실행기 선택, 조용한 대체 금지, 브리프 바이트 전달, 실행 기록의 Orca 식별자, 9분 상한 대기, 기본 경로 불변을 본다.
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from oah import briefs, call, call_cli, intent, launchers, skillsel
from oah.engines.effort import EngineError
from oah.engines.proc import resolve_command, run_command
from oah.launchers import orca
from oah.lease import Store
from oah.paths import repo_info
from oah.runs import UNVERIFIED, find_run, new_run
from tests.helpers import git_commit, make_git_repo, make_tmp_dir
from tests.test_call import (FAKE_CLAUDE, FAKE_CODEX, FOOTER, SECTION, Project, add_worktree, has_footer,
                             parse_intent, run_cli, toml_list)

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_ORCA = [sys.executable, str(FAKES / "fake_orca.py")]
SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def _stub_oah(folder: pathlib.Path) -> None:
    """An `oah` that the PATH check finds; the fake Orca host runs FAKE_ORCA_OAH instead of it."""
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (folder / "oah.exe").write_bytes(b"")
        return
    stub = folder / "oah"
    stub.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)


def _minimal_path(*extra: pathlib.Path) -> str:
    """PATH with the extra folders, git and this Python, and nothing else (so no real orca is found)."""
    folders = [str(folder) for folder in extra]
    for tool in (shutil.which("git"), sys.executable):
        if tool and str(pathlib.Path(tool).parent) not in folders:
            folders.append(str(pathlib.Path(tool).parent))
    return os.pathsep.join(folders)


def _load_fake_orca():
    spec = importlib.util.spec_from_file_location("fake_orca", FAKES / "fake_orca.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOrcaPidFileTest(unittest.TestCase):
    """The fake Orca publishes PID files atomically, so a reader never acts on an empty or partial one."""

    def setUp(self):
        self.fake = _load_fake_orca()
        self.folder = make_tmp_dir(self)

    def test_publish_never_opens_the_final_path_for_writing(self):
        final = self.folder / "term_x.pid"
        code = (
            "import importlib.util, json, os, pathlib, sys\n"
            "final = pathlib.Path(sys.argv[2])\n"
            "spec = importlib.util.spec_from_file_location('fake_orca', sys.argv[1])\n"
            "fake = importlib.util.module_from_spec(spec); spec.loader.exec_module(fake)\n"
            "opened = []\n"
            "def hook(event, args):\n"
            "    if event == 'open' and args[0] is not None and not isinstance(args[0], int):\n"
            "        mode, flags = args[1], args[2]\n"
            "        writes = any(c in mode for c in 'wax+') if isinstance(mode, str) else bool(\n"
            "            flags & (os.O_WRONLY | os.O_RDWR))\n"
            "        if writes:\n"
            "            opened.append(os.path.abspath(os.fsdecode(args[0])))\n"
            "sys.addaudithook(hook)\n"
            "fake.publish_pid(final, 4242)\n"
            "print(json.dumps(opened))\n")
        result = subprocess.run([sys.executable, "-c", code, str(FAKES / "fake_orca.py"), str(final)],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        opened = [pathlib.Path(path) for path in json.loads(result.stdout)]
        self.assertTrue(opened, "publishing a PID writes a temporary file first")
        self.assertNotIn(final.resolve(), [path.resolve() for path in opened])
        self.assertTrue(all(path.parent.resolve() == final.parent.resolve() for path in opened),
                        "the temporary file sits in the same folder so os.replace stays atomic")
        self.assertEqual(final.read_text(encoding="utf-8"), "4242\n")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["term_x.pid"])

    def test_concurrent_reader_never_sees_a_partial_pid(self):
        count = 300
        pids = [7 ** (index % 12) + index for index in range(count)]
        seen: list[str] = []
        failures: list[str] = []

        def reader():
            for index in range(count):
                path = self.folder / f"term_{index}.pid"
                deadline = time.monotonic() + 15
                while not path.exists():
                    if time.monotonic() > deadline:
                        failures.append(f"{path.name} never appeared")
                        return
                seen.append(path.read_bytes().decode("utf-8"))

        thread = threading.Thread(target=reader)
        thread.start()
        for index, pid in enumerate(pids):
            self.fake.publish_pid(self.folder / f"term_{index}.pid", pid)
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(seen, [f"{pid}\n" for pid in pids])

    def test_reader_waits_for_a_complete_integer(self):
        path = self.folder / "term_y.pid"
        self.assertIsNone(self.fake.read_pid(path))
        path.write_bytes(b"")
        self.assertIsNone(self.fake.read_pid(path), "an empty file is not a PID yet")
        path.write_bytes(b"12")
        self.assertIsNone(self.fake.read_pid(path), "digits without the newline may be a partial write")
        path.write_bytes(b"x\n")
        self.assertIsNone(self.fake.read_pid(path))
        self.fake.publish_pid(path, 1234)
        self.assertEqual(self.fake.read_pid(path), 1234)
        path.write_bytes(b"0\r\n")
        self.assertEqual(self.fake.read_pid(path), 0, "a CRLF line ending is still a complete record")


class OrcaProject:
    """A committed temporary repository whose config points at the fake engines and the fake orca."""

    def __init__(self, test: unittest.TestCase, config: str = "", *, orca_command: list[str] | None = None) -> None:
        self.test = test
        self.repo = make_git_repo(test)
        work = make_tmp_dir(test)
        self.orca_state, self.home, self.record = work / "orca", work / "codex-home", work / "record.json"
        self.bin = work / "bin"
        _stub_oah(self.bin)
        self.home.mkdir()
        (self.home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-test-a", "is_default": True,
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]},
        ]}), encoding="utf-8")
        self.orca_command = [*FAKE_ORCA, "--state", str(self.orca_state)]
        orca_line = "" if orca_command == [] else f"orca_command = {toml_list(orca_command or self.orca_command)}\n"
        files = {"README.md": "x\n", ".oah/config.toml":
                 f"[engines.claude]\ncommand = {toml_list(FAKE_CLAUDE)}\n"
                 f"[engines.codex]\ncommand = {toml_list(FAKE_CODEX)}\n"
                 f"[call]\n{orca_line}{config}"}
        for task in ("t1", "adhoc"):
            files[f".oah/tasks/{task}/PACKET.md"] = SECTION.replace("Intent-ID: t1", f"Intent-ID: {task}").encode("utf-8")
        git_commit(self.repo, files)
        self.state = self.repo / ".git" / "oah"
        for task in ("t1", "adhoc"):
            self.bind(task)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("FAKE_", "OAH_")) and key != "CLAUDE_CONFIG_DIR"}
        self.env.update(CODEX_HOME=str(self.home), FAKE_RECORD=str(self.record), FAKE_ORCA_STATE=str(self.orca_state),
                        PATH=_minimal_path(self.bin), HOME=str(work / "home"), USERPROFILE=str(work / "home"))
        self.host: subprocess.Popen | None = None

    def bind(self, task="t1", root=None):
        """Bind the launch fixture to a real accepted source in its execution worktree."""
        root = root or self.repo
        store = Store(self.state)
        if any(lease.task == task for lease, _ in store.snapshot()):
            store.release(task=task, agent="codex", reason="bind launch fixture worktree", force=False)
        source = f".oah/tasks/{task}/PACKET.md"
        bound = intent.parse(intent.read_source(root / source)).bind(source)
        store.start(task=task, agent="codex", scope=(f".oah/tasks/{task}",), note="launch fixture", ttl_hours=1,
                     info=repo_info(root), intent=bound)

    def start_host(self, **extra: str) -> None:
        """The fake Orca app; its environment is what terminals get (no OAH_* variables, like the real app)."""
        env = dict(self.env, FAKE_ORCA_OAH=json.dumps([sys.executable, "-m", "oah"]), PYTHONPATH=SRC, **extra)
        self.orca_state.mkdir(parents=True, exist_ok=True)
        self.host = subprocess.Popen([*FAKE_ORCA, "__host"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.test.addCleanup(self.stop_host)

    def stop_host(self) -> None:
        (self.orca_state / "stop").write_text("", encoding="utf-8")
        try:
            self.host.wait(timeout=40)
        except subprocess.TimeoutExpired:
            self.host.kill()
            self.host.wait()

    def cli(self, *argv: str, env: dict | None = None, cwd: pathlib.Path | None = None) -> tuple[int, str, str]:
        self.orca_state.mkdir(parents=True, exist_ok=True)
        (self.orca_state / "controls.json").write_text(json.dumps(
            {key: value for key, value in (env or {}).items() if key.startswith("FAKE_ORCA_")}), encoding="utf-8")
        parser = argparse.ArgumentParser()
        call_cli.register(parser.add_subparsers(dest="command"))
        out, err = io.StringIO(), io.StringIO()
        previous = os.getcwd()
        os.chdir(cwd or self.repo)
        try:
            with mock.patch.dict(os.environ, dict(self.env, **(env or {})), clear=True), \
                    mock.patch.object(call_cli, "intent_tools", return_value=call_cli.IntentTools(
                        FOOTER, has_footer, parse_intent, call._read_text)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = call_cli.run(parser.parse_args(list(argv)))
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    def orca_calls(self) -> list[list[str]]:
        path = self.orca_state / "argv.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def runs(self) -> list[pathlib.Path]:
        root = self.state / "runs"
        return [run for task in root.iterdir() for run in task.iterdir() if run.is_dir()] if root.is_dir() else []

    def recorded(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def queued(self, engine: str = "codex", stage: str = "review", *, override: bool = False) -> call.Prepared:
        self.orca_state.mkdir(parents=True, exist_ok=True)
        (self.orca_state / "controls.json").write_text(json.dumps({"FAKE_ORCA_HOLD": "1"}), encoding="utf-8")
        prepared = call.prepare(call.CallRequest(engine=engine, stage=stage, task="t1", tier="low",
                                                 override_stage=override), call_cli.load_settings(self.repo),
                                root=self.repo, state_dir=self.state, env=self.env, footer=FOOTER,
                                has_footer=has_footer, parse_intent=parse_intent, read_source=call._read_text)
        client = orca.find(self.orca_command, self.env)
        placement = orca.existing_worktree(client, self.repo)
        orca.start(client, prepared, placement)
        return prepared


def started_run(out: str) -> str:
    return out.split("run=", 1)[1].split()[0]


class SelectionTest(unittest.TestCase):
    def test_default_launcher_is_process(self):
        self.assertEqual(launchers.DEFAULTS["launcher"], "process")
        self.assertEqual(launchers.choose(None, launchers.DEFAULTS), "process")

    def test_config_selects_orca_and_flag_overrides(self):
        section = dict(launchers.DEFAULTS, launcher="orca")
        self.assertEqual(launchers.choose(None, section), "orca")
        self.assertEqual(launchers.choose("process", section), "process")
        self.assertEqual(launchers.choose("orca", launchers.DEFAULTS), "orca")

    def test_unknown_configured_launcher_is_config_invalid(self):
        with self.assertRaises(EngineError) as ctx:
            launchers.choose(None, dict(launchers.DEFAULTS, launcher="tmux"))
        self.assertEqual(ctx.exception.code, "OAH_CONFIG_INVALID")
        project = OrcaProject(self, 'launcher = "tmux"\n')
        code, _, err = project.cli("call", "codex", "--stage", "review", "--tier", "low")
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_CONFIG_INVALID"), err)
        self.assertEqual(project.runs(), [])

    def test_process_run_records_no_launcher(self):
        project = Project(self)
        code, out, err = run_cli(project, "call", "codex", "--stage", "review", "--tier", "low", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("launcher", data)
        self.assertIsNone(data["launcher"])

    def test_require_detach_is_refused_with_orca(self):
        project = OrcaProject(self)
        code, _, err = project.cli("call", "codex", "--stage", "review", "--tier", "low", "--launcher", "orca",
                                   "--background", "--require-detach")
        self.assertEqual(code, 2)
        self.assertIn("process launcher", err)
        self.assertEqual(project.orca_calls(), [])


class UnavailableTest(unittest.TestCase):
    def test_explicit_batch_shims_refuse_with_native_guidance(self):
        folder = make_tmp_dir(self)
        for suffix in (".cmd", ".bat", ".CMD"):
            with self.subTest(suffix=suffix):
                shim = folder / ("orca & wrapper" + suffix)
                shim.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
                with self.assertRaises(EngineError) as ctx:
                    orca.find([str(shim)], os.environ)
                self.assertEqual(ctx.exception.code, orca.UNAVAILABLE)
                self.assertIn("native executable", str(ctx.exception))
                self.assertIn("interpreter", str(ctx.exception))

    def test_connect_rejects_explicit_batch_shims_without_a_probe(self):
        folder = make_tmp_dir(self)
        for suffix in (".cmd", ".bat", ".CMD"):
            with self.subTest(suffix=suffix):
                shim = folder / ("orca & wrapper" + suffix)
                shim.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
                with mock.patch.object(orca, "run_command") as command:
                    with self.assertRaises(EngineError) as ctx:
                        orca.connect([str(shim)], os.environ)
                self.assertEqual(ctx.exception.code, orca.UNAVAILABLE)
                self.assertIn("native executable", str(ctx.exception))
                self.assertIn("interpreter", str(ctx.exception))
                command.assert_not_called()

    def test_wait_rejects_an_explicit_batch_shim_with_native_guidance(self):
        project = OrcaProject(self)
        prepared = project.queued()
        shim = project.bin / "orca & wrapper.cmd"
        marker = project.bin / "injected"
        shim.write_text(f'@echo injected > "{marker}"\n', encoding="utf-8")
        config = project.repo / ".oah" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace(
            toml_list(project.orca_command), toml_list([str(shim)])), encoding="utf-8")
        before = project.orca_calls()
        code, _, err = project.cli("wait", prepared.run_id, "--timeout-seconds", "1")
        self.assertEqual(code, 69, err)
        self.assertTrue(err.startswith(orca.UNAVAILABLE), err)
        self.assertIn("native executable", err)
        self.assertIn("interpreter", err)
        self.assertEqual(project.orca_calls(), before)
        self.assertEqual(prepared.run.read()["status"], "queued")
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "nt", "native Windows batch refusal")
    def test_windows_explicit_batch_never_executes(self):
        project = OrcaProject(self)
        marker = project.bin / "injected"
        shim = project.bin / "orca & wrapper.cmd"
        shim.write_text(f'@echo injected > "{marker}"\n', encoding="utf-8")
        with self.assertRaises(EngineError) as ctx:
            orca.find([str(shim)], project.env)
        self.assertEqual(ctx.exception.code, orca.UNAVAILABLE)
        self.assertFalse(marker.exists())

    def test_every_orca_invocation_gets_only_the_allowlisted_environment(self):
        project = OrcaProject(self)
        project.start_host(FAKE_SLEEP="0.5")
        secret = secrets.token_hex(32)
        additions = {name: secret for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ORCA_PAIRING_TOKEN",
                                                "ORCA_ACCOUNT_ID", "UNRELATED", "NODE_OPTIONS", "PYTHONPATH")}
        seen = []

        def invoke(*args, **kwargs):
            seen.append(dict(kwargs["env"]))
            return run_command(*args, **kwargs)

        with mock.patch.object(orca, "run_command", side_effect=invoke):
            code, out, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low",
                                         "--launcher", "orca", "--background", env=additions)
            self.assertEqual(code, 0, err)
            code, out, err = project.cli("wait", started_run(out), "--timeout-seconds", "60", env=additions)
            self.assertEqual(code, 0, err)
        self.assertGreaterEqual(len(seen), 6)
        for env in seen:
            self.assertTrue(set(env).isdisjoint(additions), set(env) & set(additions))
            self.assertNotIn(secret, env.values())
            self.assertEqual(env["PATH"], project.env["PATH"])
        for folder in project.runs():
            for path in folder.iterdir():
                self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    def assert_nothing_launched(self, project: OrcaProject) -> None:
        self.assertEqual(project.runs(), [])
        self.assertFalse(project.record.exists())
        self.assertFalse(any(call[:2] in (["worktree", "create"], ["terminal", "create"])
                             for call in project.orca_calls()))

    def test_missing_orca_refuses_and_launches_nothing(self):
        project = OrcaProject(self, orca_command=[])
        if resolve_command("orca", project.env["PATH"]) is not None:
            self.skipTest("a real orca sits next to git or Python on this machine")
        for argv in (["--launcher", "orca"], []):
            env = {} if argv else None
            if not argv:  # selected through the config instead of the flag
                (project.repo / ".oah" / "config.toml").write_text(
                    (project.repo / ".oah" / "config.toml").read_text(encoding="utf-8") + 'launcher = "orca"\n',
                    encoding="utf-8")
            code, out, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", *argv, env=env)
            self.assertEqual(code, 69, err)
            self.assertTrue(err.startswith("OAH_LAUNCHER_UNAVAILABLE"), err)
            self.assertIn(orca.INSTALL_URL, err)
            self.assertIn("Nothing was launched", err)
            self.assertNotIn("OAH_RUN_", out)
            self.assert_nothing_launched(project)

    def test_disabled_orca_cli_refuses_and_launches_nothing(self):
        project = OrcaProject(self)
        code, _, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", "--launcher", "orca",
                                   env={"FAKE_ORCA_DISABLED": "1"})
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_LAUNCHER_UNAVAILABLE"), err)
        self.assertIn("--version", err)
        self.assertEqual(project.orca_calls(), [["--version"]])
        self.assert_nothing_launched(project)

    def test_missing_oah_runner_refuses(self):
        project = OrcaProject(self)
        code, _, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", "--launcher", "orca",
                                   env={"PATH": _minimal_path()})
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_LAUNCHER_UNAVAILABLE"), err)
        self.assertIn("oah worker-run", err)
        self.assert_nothing_launched(project)

    def test_state_dir_override_refuses(self):
        project = OrcaProject(self)
        code, _, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", "--launcher", "orca",
                                   env={"OAH_STATE_DIR": str(make_tmp_dir(self))})
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_LAUNCHER_UNAVAILABLE"), err)
        self.assertIn("OAH_STATE_DIR", err)
        self.assert_nothing_launched(project)


class DispatchTest(unittest.TestCase):
    def test_orca_handoff_refuses_a_mutating_placement_outside_the_lease(self):
        project = OrcaProject(self)
        sibling = make_tmp_dir(self) / "sibling"
        add_worktree(project.repo, sibling, "sibling")
        prepared = call.prepare(call.CallRequest(engine="codex", stage="correction", task="t1", tier="low"),
                                call_cli.load_settings(project.repo), root=project.repo, state_dir=project.state,
                                env=project.env, footer=FOOTER, has_footer=has_footer)
        client = orca.find(project.orca_command, project.env)
        placement = orca.existing_worktree(client, sibling)
        with self.assertRaises(EngineError) as caught:
            orca.start(client, prepared, placement)
        self.assertEqual(caught.exception.code, "OAH_WORKTREE_LEASE_MISMATCH")
        self.assertFalse(any(argv[:2] == ["terminal", "create"] for argv in project.orca_calls()))

    def test_committed_packet_fixtures_keep_lf_bytes(self):
        project = OrcaProject(self)
        for task in ("t1", "adhoc"):
            with self.subTest(task=task):
                self.assertEqual((project.repo / f".oah/tasks/{task}/PACKET.md").read_bytes(),
                                 SECTION.replace("Intent-ID: t1", f"Intent-ID: {task}").encode("utf-8"))

    def test_correction_reuses_the_dispatchers_leased_worktree(self):
        project = OrcaProject(self)
        project.start_host()
        before = (project.state / "leases.json").read_bytes()
        code, out, err = project.cli("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "low",
                                     "--launcher", "orca", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["status"], "finished", data.get("error"))
        self.assertEqual(pathlib.Path(data["cwd"]).resolve(), project.repo.resolve())
        self.assertFalse(data["launcher"]["worktree_created"])
        self.assertEqual((project.state / "leases.json").read_bytes(), before)
        self.assertFalse(any(argv[:2] == ["worktree", "create"] for argv in project.orca_calls()))

    def test_mutating_sibling_selector_refuses_before_opening_an_orca_terminal(self):
        project = OrcaProject(self)
        sibling = make_tmp_dir(self) / "sibling"
        add_worktree(project.repo, sibling, "sibling")
        for stage, engine in (("initial", "claude"), ("correction", "codex")):
            with self.subTest(stage=stage):
                code, _, err = project.cli("call", engine, "--stage", stage, "--task", "t1", "--tier", "low",
                                           "--launcher", "orca", "--worktree", str(sibling), "--background")
                self.assertEqual(code, 1, err)
                self.assertIn("OAH_WORKTREE_LEASE_MISMATCH", err)
                self.assertFalse(any(argv[:2] in (["worktree", "create"], ["terminal", "create"])
                                     for argv in project.orca_calls()))
                self.assertFalse(project.record.exists())

    def test_review_can_still_run_in_a_new_orca_worktree(self):
        project = OrcaProject(self)
        project.start_host()
        code, out, err = project.cli("call", "codex", "--stage", "review", "--task", "t1", "--tier", "low",
                                     "--launcher", "orca", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["status"], "finished", data.get("error"))
        self.assertNotEqual(pathlib.Path(data["cwd"]).resolve(), project.repo.resolve())
        self.assertTrue(data["launcher"]["worktree_created"])
        self.assertIn("read-only", project.recorded()["argv"])

    def test_orca_dispatch_refuses_a_lease_changed_after_prepare(self):
        project = OrcaProject(self)
        prepare = call.prepare

        def changed(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            Store(project.state).renew(task="t1", agent="codex", ttl_hours=1)
            return prepared

        with mock.patch.object(call, "prepare", side_effect=changed):
            code, out, err = project.cli("call", "codex", "--stage", "review", "--task", "t1", "--tier", "low",
                                         "--launcher", "orca", "--worktree", str(project.repo), "--background")
        self.assertEqual(code, 1, out + err)
        self.assertIn("OAH_LEASE_CHANGED", err)
        self.assertFalse(any(argv[:2] == ["terminal", "create"] for argv in project.orca_calls()))
        self.assertFalse(project.record.exists())

    def test_queued_orca_worker_refuses_lease_or_source_changes(self):
        for change in ("renew", "release", "source"):
            with self.subTest(change=change):
                project = OrcaProject(self)
                prepared = project.queued()
                store = Store(project.state)
                if change == "renew":
                    store.renew(task="t1", agent="codex", ttl_hours=1)
                elif change == "release":
                    store.release(task="t1", agent="codex", reason="queued race", force=False)
                else:
                    path = project.repo / ".oah/tasks/t1/PACKET.md"
                    path.write_text(SECTION.replace("Reports miss files.", "The goal changed."), encoding="utf-8", newline="")
                code, out, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
                self.assertEqual(code, 1, out + err)
                expected = "OAH_INTENT_NOT_CURRENT" if change == "source" else "OAH_LEASE_CHANGED"
                self.assertIn(expected, err)
                self.assertEqual(prepared.run.read()["status"], "refused")
                self.assertFalse(project.record.exists())

    def test_coordinated_stage_edits_cannot_give_a_review_write_permissions(self):
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                project = OrcaProject(self)
                prepared = project.queued(engine, "review", override=engine == "claude")
                prompt = prepared.run.prompt_path.read_bytes()
                terminal = next(argv for argv in project.orca_calls() if argv[:2] == ["terminal", "create"])
                command = terminal[terminal.index("--command") + 1]
                saved = json.loads(prepared.run.launch_path.read_text(encoding="utf-8"))
                saved["stage"] = "correction"
                prepared.run.launch_path.write_text(json.dumps(saved), encoding="utf-8")
                data = prepared.run.read()
                data["stage"] = "correction"
                prepared.run.result_path.write_text(json.dumps(data), encoding="utf-8")
                code, _, err = project.cli(*command.removesuffix("; exit").split()[1:])
                self.assertEqual(code, 1, err)
                self.assertTrue(err.startswith("OAH_RUN_LAUNCH_INVALID"), err)
                self.assertEqual(prepared.run.read()["status"], "queued")
                self.assertEqual(prepared.run.prompt_path.read_bytes(), prompt)
                self.assertFalse((prepared.run.path / "worker.claim").exists())
                self.assertFalse(project.record.exists())

    def test_coordinated_review_command_and_digest_edits_are_refused(self):
        for engine in ("codex", "claude"):
            with self.subTest(engine=engine):
                project = OrcaProject(self)
                prepared = project.queued(engine, "review", override=engine == "claude")
                saved = json.loads(prepared.run.launch_path.read_text(encoding="utf-8"))
                argv = list(prepared.argv)
                if engine == "codex":
                    argv[argv.index("-s") + 1] = "workspace-write"
                else:
                    argv[argv.index("--permission-mode") + 1] = "bypassPermissions"
                    argv[argv.index("--tools") + 1] = "Read,Grep,Glob,Edit,Write,Bash"
                saved["argv"] = argv
                prepared.run.launch_path.write_text(json.dumps(saved), encoding="utf-8")
                data = prepared.run.read()
                data["command"] = argv
                data["launcher"]["launch_digest"] = hashlib.sha256(
                    json.dumps(saved, sort_keys=True).encode("utf-8")).hexdigest()
                prepared.run.result_path.write_text(json.dumps(data), encoding="utf-8")
                code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
                self.assertEqual(code, 1, err)
                self.assertTrue(err.startswith("OAH_RUN_LAUNCH_INVALID"), err)
                self.assertEqual(prepared.run.read()["status"], "queued")
                self.assertFalse(project.record.exists())

    def test_orca_saves_no_execution_argv_permissions_or_digest(self):
        project = OrcaProject(self)
        prepared = project.queued()
        data = prepared.run.read()
        saved = json.loads(prepared.run.launch_path.read_text(encoding="utf-8"))
        for field in ("command", "read_only"):
            self.assertNotIn(field, data)
        for field in ("argv", "env_additions"):
            self.assertNotIn(field, saved)
        self.assertNotIn("launch_digest", data["launcher"])

    @unittest.skipUnless(os.name != "nt", "Windows pins the open directory against replacement")
    def test_worker_refuses_run_directory_replaced_between_validation_and_claim(self):
        for alter_record in (False, True):
            with self.subTest(alter_record=alter_record):
                project = OrcaProject(self)
                prepared = project.queued("claude", "initial")
                original = orca.worker_prepared
                replacement = {}

                def replace_after_validation(*args):
                    validated = original(*args)
                    folder = prepared.run.path
                    moved = folder.with_name("original")
                    folder.rename(moved)
                    shutil.copytree(moved, folder)
                    if alter_record:
                        data = json.loads((folder / "result.json").read_text(encoding="utf-8"))
                        data.update(run_id="r20260101T000000Z-00000000", task="other")
                        (folder / "result.json").write_text(json.dumps(data), encoding="utf-8")
                    (folder / "prompt.md").write_text("replacement prompt", encoding="utf-8", newline="")
                    replacement.update({p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()})
                    return validated

                with mock.patch.object(orca, "worker_prepared", side_effect=replace_after_validation):
                    code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
                self.assertEqual(code, 1, err)
                self.assertTrue(err.startswith("OAH_RUN_LAUNCH_INVALID"), err)
                self.assertFalse(project.record.exists())
                self.assertEqual(replacement, {p.name: p.read_bytes()
                                               for p in prepared.run.path.iterdir() if p.is_file()})

    def test_worker_refuses_record_replaced_between_validation_and_claim(self):
        project = OrcaProject(self)
        prepared = project.queued("claude", "initial")
        original = orca.worker_prepared

        def replace_after_validation(*args):
            validated = original(*args)
            data = prepared.run.read()
            data.update(run_id="r20260101T000000Z-00000000", task="other")
            prepared.run.result_path.write_text(json.dumps(data), encoding="utf-8")
            return validated

        with mock.patch.object(orca, "worker_prepared", side_effect=replace_after_validation):
            code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
        self.assertEqual(code, 1, err)
        self.assertTrue(err.startswith("OAH_RUN_LAUNCH_INVALID"), err)
        self.assertFalse(project.record.exists())

    def test_worker_executes_the_validated_prompt_snapshot(self):
        project = OrcaProject(self)
        prepared = project.queued("claude", "initial")
        prompt = prepared.run.prompt_path.read_bytes()
        original = call.execute

        def replace_before_execute(validated):
            prepared.run.prompt_path.write_text("replacement prompt", encoding="utf-8", newline="")
            return original(validated)

        with mock.patch.object(call, "execute", side_effect=replace_before_execute):
            code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
        self.assertEqual(code, 0, err)
        self.assertEqual(project.recorded()["stdin"].encode("utf-8"), prompt)

    def test_worker_refuses_record_identity_changed_after_claim(self):
        project = OrcaProject(self)
        prepared = project.queued("claude", "initial")
        original = call.execute

        def replace_before_execute(validated):
            data = prepared.run.read()
            data.update(run_id="r20260101T000000Z-00000000", task="other")
            prepared.run.result_path.write_text(json.dumps(data), encoding="utf-8")
            return original(validated)

        with mock.patch.object(call, "execute", side_effect=replace_before_execute):
            code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
        self.assertEqual(code, 1, err)
        self.assertTrue(err.startswith("OAH_RUN_LAUNCH_INVALID"), err)
        self.assertFalse(project.record.exists())

    def test_worker_claim_cannot_be_replayed_by_resetting_the_record(self):
        project = OrcaProject(self)
        prepared = project.queued("claude", "initial")
        queued = prepared.run.result_path.read_bytes()
        code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
        self.assertEqual(code, 0, err)
        self.assertTrue((prepared.run.path / "worker.claim").is_file())
        project.record.unlink()
        prepared.run.result_path.write_bytes(queued)
        code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
        self.assertEqual(code, 1, err)
        self.assertTrue(err.startswith("OAH_RUN_NOT_QUEUED"), err)
        self.assertFalse(project.record.exists())

    @unittest.skipUnless(os.name == "nt", "native Windows directory sharing semantics")
    def test_windows_worker_pins_run_directory_until_execution_finishes(self):
        project = OrcaProject(self)
        prepared = project.queued("claude", "initial")
        original = orca.worker_prepared

        def try_replace_after_validation(*args):
            validated = original(*args)
            with self.assertRaises(PermissionError):
                prepared.run.path.rename(prepared.run.path.with_name("moved"))
            return validated

        with mock.patch.object(orca, "worker_prepared", side_effect=try_replace_after_validation):
            code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
        self.assertEqual(code, 0, err)
        prepared.run.path.rename(prepared.run.path.with_name("closed"))

    def test_worker_rejects_altered_launch_metadata_before_execution(self):
        project = OrcaProject(self)
        prepared = project.queued()
        original = json.loads(prepared.run.launch_path.read_text(encoding="utf-8"))
        record = prepared.run.result_path.read_bytes()
        outside = new_run(make_tmp_dir(self), "other", "codex", "review")
        changes = {"run_dir": str(outside.path), "run_id": "r20260101T000000Z-00000000",
                   "task": "other", "engine": "claude", "stage": "initial", "cwd": str(project.bin),
                   "argv": [sys.executable, "-c", "raise SystemExit(0)"], "env_additions": {},
                   "model": "other", "launch_effort": "max", "timeout_s": 0.001,
                   "cli_version": "other", "warnings": ["altered"]}
        for key, value in changes.items():
            with self.subTest(field=key):
                prepared.run.result_path.write_bytes(record)
                project.record.unlink(missing_ok=True)
                prepared.run.launch_path.write_text(json.dumps(dict(original, **{key: value})), encoding="utf-8")
                code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
                self.assertEqual(code, 1, err)
                self.assertTrue(err.startswith("OAH_RUN_LAUNCH_INVALID"), err)
                self.assertEqual(prepared.run.read()["status"], "queued")
                self.assertFalse(project.record.exists())

    def test_worker_rechecks_stage_and_review_permissions(self):
        project = OrcaProject(self)
        prepared = project.queued()
        config = project.repo / ".oah" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + '\n[stages]\nreview = "claude"\n', encoding="utf-8")
        code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_STAGE_ENGINE_MISMATCH"), err)
        self.assertFalse(project.record.exists())

        project = OrcaProject(self)
        prepared = project.queued()
        prepared.run.result(read_only=False)
        code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_RUN_LAUNCH_INVALID"), err)
        self.assertFalse(project.record.exists())

    def test_worker_preserves_explicit_stage_override_and_rebuilds_worker_variables(self):
        project = OrcaProject(self)
        prepared = project.queued("claude", "review", override=True)
        code, _, err = project.cli("worker-run", prepared.run_id, "--stage", prepared.stage, env={"OAH_WORKER": "0", "OAH_TASK": "wrong",
                                                                      "OAH_RUN_ID": "wrong"})
        self.assertEqual(code, 0, err)
        record = project.recorded()
        self.assertEqual({name: record["env"][name] for name in call.WORKER_ENV},
                         {"OAH_WORKER": "1", "OAH_TASK": "t1", "OAH_RUN_ID": prepared.run_id})
        self.assertEqual(record["argv"][record["argv"].index("--permission-mode") + 1], "plan")

    def test_concurrent_worker_starts_execute_exactly_once(self):
        project = OrcaProject(self)
        prepared = project.queued("claude", "initial")
        rendezvous = make_tmp_dir(self)
        script = '''import argparse, os, pathlib, sys, time
from oah import call_cli, runs
original = runs.Run.read
first = True
def read(self):
    global first
    data = original(self)
    if first:
        first = False
        pathlib.Path(sys.argv[2], sys.argv[3] + ".ready").touch()
        deadline = time.monotonic() + 10
        while len(list(pathlib.Path(sys.argv[2]).glob("*.ready"))) < 2:
            if time.monotonic() > deadline: raise RuntimeError("rendezvous timed out")
            time.sleep(0.01)
    return data
runs.Run.read = read
parser = argparse.ArgumentParser()
call_cli.register(parser.add_subparsers(dest="command"))
raise SystemExit(call_cli.run(parser.parse_args(["worker-run", sys.argv[1], "--stage", "initial"])))
'''
        children = []
        for index in range(2):
            env = dict(project.env, PYTHONPATH=SRC, FAKE_SLEEP="0.5",
                       FAKE_RECORD=str(rendezvous / f"engine-{index}.json"))
            child = subprocess.Popen([sys.executable, "-c", script, prepared.run_id, str(rendezvous), str(index)],
                                     cwd=project.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            children.append(child)
            self.addCleanup(lambda child=child: child.kill() if child.poll() is None else None)
        results = [child.communicate(timeout=30) for child in children]
        self.assertEqual(sorted(child.returncode for child in children), [0, 1], results)
        self.assertEqual(len(list(rendezvous.glob("engine-*.json"))), 1)
        self.assertEqual(prepared.run.read()["status"], "finished")

    def test_mutating_run_reuses_the_leased_tree_and_delivers_the_exact_brief(self):
        project = OrcaProject(self)
        project.start_host()
        message = "Build AC1 · 한글 \"quoted\" $HOME `tick`\r\nsecond line"
        code, out, err = project.cli("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low",
                                     "--launcher", "orca", "--message", message, "--background")
        self.assertEqual(code, 0, err)
        run_id = started_run(out)
        self.assertIn(f"oah wait {run_id}", out)
        code, out, err = project.cli("wait", run_id, "--timeout-seconds", "60", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["status"], "finished", data.get("error"))
        info = data["launcher"]
        worktree = pathlib.Path(info["worktree"])
        self.assertEqual(worktree.resolve(), project.repo.resolve())
        self.assertEqual((info["name"], info["orca_version"], info["worktree_created"], info["worker_env"]),
                         ("orca", "1.4.237", False, "env"))
        self.assertTrue(info["terminal"].startswith("term_"))
        self.assertIsInstance(info["terminal_process_id"], int)
        self.assertEqual(info["worktree_id"], f"{info['worktree_id'].split('::')[0]}::{worktree}")
        self.assertEqual((info["reported_model"], info["reported_effort"]), (UNVERIFIED, UNVERIFIED))
        self.assertEqual((data["requested_effort"], data["launch_effort"]), ("low", "low"))
        self.assertEqual(info["command"], f"oah worker-run {run_id} --stage initial; exit")

        run = find_run(project.state, run_id)
        prompt = run.prompt_path.read_bytes()
        scope = [".oah/tasks/t1"]
        selection, rerank = skillsel.choose(worktree, kind="implementation", stage="initial", risk="normal", scope=scope,
                                            state_dir=None, env=project.env)
        expected = briefs.build(run_id=run_id, task="t1", stage="initial", engine="claude", scope=scope,
                                intent_section=SECTION, binding=intent.parse(SECTION),
                                packet_source=".oah/tasks/t1/PACKET.md", round_instructions=message,
                                footer=FOOTER, has_footer=has_footer, skills_block=selection.brief_text(rerank=rerank),
                                skill_ids=[skill.id for skill in selection.skills])
        self.assertEqual(prompt, expected.encode("utf-8"))
        worker = project.recorded()
        self.assertEqual(worker["stdin"].encode("utf-8"), prompt)
        self.assertEqual(worker["env"], {"OAH_WORKER": "1", "OAH_RUN_ID": run_id, "OAH_TASK": "t1"})
        self.assertEqual(pathlib.Path(worker["cwd"]).resolve(), worktree.resolve())
        self.assertIn("--model", worker["argv"])

        calls = project.orca_calls()
        self.assertTrue(all("--json" in argv for argv in calls if argv != ["--version"]))
        self.assertEqual([argv[:2] for argv in calls[:3]], [["--version"], ["worktree", "show"],
                                                            ["terminal", "create"]])
        self.assertFalse(any(argv[:2] == ["worktree", "create"] for argv in calls))
        terminal = calls[2]
        self.assertEqual(terminal[terminal.index("--command") + 1], f"oah worker-run {run_id} --stage initial; exit")
        self.assertEqual(terminal[terminal.index("--worktree") + 1], f"id:{info['worktree_id']}")
        flat = "\n".join(" ".join(argv) for argv in calls)
        self.assertNotIn("Build AC1", flat)
        self.assertNotIn("dispatched oah worker", flat)

    def test_existing_worktree_keeps_crlf_intent_bytes(self):
        project = OrcaProject(self)
        project.start_host()
        lane = make_tmp_dir(self) / "lane"
        add_worktree(project.repo, lane, "lane-a")
        section = SECTION.replace("Reports miss files.", "한글 — café")
        packet = lane / ".oah" / "tasks" / "t1" / "PACKET.md"
        packet.parent.mkdir(parents=True, exist_ok=True)
        packet.write_bytes((section + "## Scope\n- src/a.py\n").replace("\n", "\r\n").encode("utf-8"))
        project.bind(root=lane)
        code, out, err = project.cli("call", "codex", "--stage", "review", "--task", "t1", "--tier", "low",
                                     "--launcher", "orca", "--worktree", "lane-a", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["status"], "finished", data.get("error"))
        self.assertFalse(data["launcher"]["worktree_created"])
        self.assertEqual(pathlib.Path(data["launcher"]["worktree"]).resolve(), lane.resolve())
        stdin = project.recorded()["stdin"].encode("utf-8")
        self.assertIn(section.replace("\n", "\r\n").encode("utf-8"), stdin)
        self.assertEqual(stdin, find_run(project.state, data["run_id"]).prompt_path.read_bytes())
        self.assertIn("read-only", project.recorded()["argv"])
        self.assertFalse(any(argv[:2] == ["worktree", "create"] for argv in project.orca_calls()))

    def test_runner_command_is_safe_and_portable(self):
        self.assertEqual(orca.runner_command("r20261023T120000Z-1a2b3c4d", "review"),
                         "oah worker-run r20261023T120000Z-1a2b3c4d --stage review; exit")
        with self.assertRaises(briefs.BriefError) as ctx:
            orca.runner_command("r1; rm -rf x", "review")
        self.assertEqual(ctx.exception.code, "OAH_UNSAFE_ARGUMENT")
        place = orca.Placement(pathlib.Path("wt"), "repo-1::/x y/wt", False)
        posix = orca.terminal_args(place, "oah t1 initial", "oah worker-run r1 --stage initial; exit", windows=False)
        windows = orca.terminal_args(place, "oah t1 initial", "oah worker-run r1 --stage initial; exit", windows=True)
        self.assertNotIn("--shell", posix)
        self.assertEqual(windows[-2:], ["--shell", "powershell.exe"])
        self.assertEqual(posix[:4], ["terminal", "create", "--worktree", "id:repo-1::/x y/wt"])

    @unittest.skipUnless(os.name == "nt", "Windows terminals run Windows PowerShell")
    def test_windows_terminal_runs_powershell(self):
        project = OrcaProject(self)
        project.start_host()
        code, out, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", "--launcher", "orca",
                                     "--background")
        self.assertEqual(code, 0, err)
        terminal = next(argv for argv in project.orca_calls() if argv[:2] == ["terminal", "create"])
        self.assertEqual(terminal[terminal.index("--shell") + 1], "powershell.exe")
        code, _, err = project.cli("wait", started_run(out), "--timeout-seconds", "60")
        self.assertEqual(code, 0, err)

    def test_terminal_create_failure_marks_run_failed(self):
        project = OrcaProject(self)
        code, out, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", "--launcher", "orca",
                                     "--background", env={"FAKE_ORCA_FAIL": "terminal create"})
        self.assertEqual(code, 69)
        self.assertTrue(err.startswith("OAH_LAUNCHER_FAILED"), err)
        self.assertIn("fake_failure", err)
        self.assertNotIn("OAH_RUN_STARTED", out)
        [folder] = project.runs()
        data = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "failed")
        self.assertTrue(data["error"].startswith("OAH_LAUNCHER_FAILED"))
        self.assertFalse(project.record.exists())

    def test_worker_run_refuses_a_run_that_is_not_queued(self):
        project = OrcaProject(self)
        run = new_run(project.state, "t1", "codex", "review")
        code, _, err = project.cli("worker-run", run.id, "--stage", "review")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_RUN_NOT_QUEUED"), err)
        run.result(status="finished", launcher={"name": "orca"})
        code, _, err = project.cli("worker-run", run.id, "--stage", "review")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_RUN_NOT_QUEUED"), err)
        self.assertFalse(project.record.exists())


class StubOrca:
    """An Orca whose terminal never exits; a fake clock advances by every requested wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[int] = []
        self.version = "1.4.237"

    def json(self, *args: str, timeout: float = 0) -> dict:
        if args[:2] == ("terminal", "wait"):
            ms = int(args[args.index("--timeout-ms") + 1])
            self.waits.append(ms)
            self.now += ms / 1000
            raise orca.OrcaReplyError("timeout", "timeout")
        if args[:2] == ("terminal", "read"):
            return {"terminal": {"tail": ["working"], "nextCursor": "1", "latestCursor": "1"}}
        raise AssertionError(args)


class WaitTest(unittest.TestCase):
    def test_process_cli_wait_includes_repository_lookup_and_bounds_polling(self):
        for timeout, lookup in ((1.0, 0.75), (1.0, 1.25), (0.1, 0.0), (100_000.0, 0.75)):
            with self.subTest(timeout=timeout, lookup=lookup):
                project = OrcaProject(self)
                run = new_run(project.state, "t1", "codex", "review")
                run.result(status="running", pid=os.getpid())
                info = call_cli.repo_info(project.repo)
                clock = StubOrca()
                sleeps = []

                def locate(cwd, deadline):
                    clock.now += lookup
                    return info

                def sleep(seconds):
                    sleeps.append(seconds)
                    clock.now += seconds

                with mock.patch.object(call_cli, "_wait_repo_info", side_effect=locate), \
                        mock.patch.object(call_cli.time, "monotonic", lambda: clock.now), \
                        mock.patch.object(call_cli.time, "sleep", side_effect=sleep):
                    code, out, err = project.cli("wait", run.id, "--timeout-seconds", str(timeout), "--json")
                self.assertEqual(code, 75, err)
                if lookup < timeout:
                    self.assertEqual(json.loads(out)["status"], "running")
                else:
                    self.assertIn("OAH_WAIT_TIMEOUT", err)
                    self.assertIn(f"oah wait {run.id}", err)
                self.assertLessEqual(clock.now - 1000, max(lookup, min(timeout, call.WAIT_LIMIT_S)) + 0.000001)
                if lookup >= timeout:
                    self.assertEqual(sleeps, [])
                self.assertIsNone(run.read()["launcher"])

    def test_process_cli_wait_lost_result_lock_contention_stays_inside_the_deadline(self):
        project = OrcaProject(self)
        run = new_run(project.state, "t1", "codex", "review")
        with subprocess.Popen([sys.executable, "-c", "pass"]) as gone:
            gone.wait(timeout=10)
        run.result(status="running", pid=gone.pid)
        info = call_cli.repo_info(project.repo)
        clock = StubOrca()

        def locate(cwd, deadline):
            clock.now += 0.75
            return info

        def sleep(seconds):
            clock.now += seconds

        script = '''import pathlib, sys
from oah.lockfile import StateLock
with StateLock(pathlib.Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read(1)
'''
        child = subprocess.Popen([sys.executable, "-c", script, str(project.state / ".lock")],
                                 env=dict(project.env, PYTHONPATH=SRC), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with mock.patch.object(call_cli, "_wait_repo_info", side_effect=locate), \
                    mock.patch.object(call.time, "monotonic", lambda: clock.now), \
                    mock.patch.object(call.time, "sleep", side_effect=sleep):
                code, out, err = project.cli("wait", run.id, "--timeout-seconds", "1.01", "--json")
            self.assertEqual(code, 75, err)
            self.assertEqual(json.loads(out)["status"], "running")
            self.assertLessEqual(clock.now - 1000, 1.010001)
            self.assertEqual(run.read()["status"], "running")
        finally:
            child.communicate("x", timeout=10)
        code, out, err = project.cli("wait", run.id, "--timeout-seconds", "10", "--json")
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["status"], "lost")
        self.assertTrue(run.read()["error"].startswith("OAH_RUN_LOST"))

    def test_process_cli_wait_storage_retries_stay_inside_the_deadline(self):
        for target in ("poll-read", "lost-read", "result.json"):
            with self.subTest(target=target):
                project = OrcaProject(self)
                run = new_run(project.state, "t1", "codex", "review")
                with subprocess.Popen([sys.executable, "-c", "pass"]) as gone:
                    gone.wait(timeout=10)
                run.result(status="running", pid=os.getpid() if target == "poll-read" else gone.pid)
                result_path = run.result_path
                before = result_path.read_bytes()
                info = call_cli.repo_info(project.repo)
                clock = StubOrca()
                original_read, original_replace = pathlib.Path.read_bytes, os.replace
                reads = attempts = 0

                def locate(cwd, deadline):
                    clock.now += 0.75
                    return info

                def read_bytes(path):
                    nonlocal reads, attempts
                    if path == result_path:
                        reads += 1
                        if (target == "poll-read" and reads > 1) or (target == "lost-read" and reads > 2):
                            attempts += 1
                            raise PermissionError("busy")
                    return original_read(path)

                def replace(source, destination, *args, **kwargs):
                    nonlocal attempts
                    if target == "result.json" and pathlib.Path(destination) == result_path:
                        attempts += 1
                        raise PermissionError("busy")
                    return original_replace(source, destination, *args, **kwargs)

                def sleep(seconds):
                    clock.now += seconds

                with mock.patch.object(call_cli, "_wait_repo_info", side_effect=locate), \
                        mock.patch.object(call.time, "monotonic", lambda: clock.now), \
                        mock.patch.object(call.time, "sleep", side_effect=sleep), \
                        mock.patch.object(pathlib.Path, "read_bytes", read_bytes), \
                        mock.patch.object(os, "replace", side_effect=replace):
                    code, _, err = project.cli("wait", run.id, "--timeout-seconds", "1.01", "--json")
                self.assertEqual(code, 75, err)
                self.assertGreater(attempts, 1)
                self.assertLessEqual(clock.now - 1000, 1.010001)
                self.assertEqual(result_path.read_bytes(), before)

    def test_process_wait_bounds_polling_by_the_shared_deadline(self):
        for timeout, budget in ((1.0, 0.1), (0.1, 1.0)):
            with self.subTest(timeout=timeout, budget=budget):
                state = make_tmp_dir(self)
                run = new_run(state, "t1", "codex", "review")
                before = run.result(status="running", pid=os.getpid())
                clock = StubOrca()
                sleeps = []

                def sleep(seconds):
                    sleeps.append(seconds)
                    clock.now += seconds

                with mock.patch.object(call.time, "monotonic", lambda: clock.now), \
                        mock.patch.object(call.time, "sleep", side_effect=sleep):
                    data = call.wait(state, run.id, timeout=timeout, poll=10, deadline=clock.now + budget)
                self.assertEqual(data, before)
                self.assertEqual(len(sleeps), 1)
                self.assertAlmostEqual(sleeps[0], min(timeout, budget))
                self.assertLessEqual(clock.now - 1000, min(timeout, budget) + 0.000001)

    def test_process_wait_without_a_deadline_keeps_its_storage_retry_budget(self):
        state = make_tmp_dir(self)
        run = new_run(state, "t1", "codex", "review")
        before = run.result(status="running", pid=os.getpid())
        clock = StubOrca()
        original_read = pathlib.Path.read_bytes
        reads = 0

        def read_bytes(path):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise PermissionError("busy")
            return original_read(path)

        def sleep(seconds):
            clock.now += seconds

        with mock.patch.object(call.time, "monotonic", lambda: clock.now), \
                mock.patch.object(call.time, "sleep", side_effect=sleep), \
                mock.patch.object(pathlib.Path, "read_bytes", read_bytes):
            data = call.wait(state, run.id, timeout=0)
        self.assertEqual(data, before)
        self.assertEqual(reads, 2)
        self.assertGreater(clock.now, 1000)

    def test_orca_wait_state_lock_contention_stays_inside_the_deadline(self):
        for mode in ("cursor", "exited", "missing"):
            with self.subTest(mode=mode):
                project = OrcaProject(self)
                prepared = project.queued()
                if mode == "missing":
                    data = prepared.run.read()
                    data["launcher"]["terminal"] = None
                    prepared.run.result(launcher=data["launcher"])
                script = '''import pathlib, sys
from oah.lockfile import StateLock
with StateLock(pathlib.Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read(1)
'''
                child = subprocess.Popen([sys.executable, "-c", script, str(project.state / ".lock")],
                                         env=dict(project.env, PYTHONPATH=SRC), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
                try:
                    self.assertEqual(child.stdout.readline().strip(), "locked")
                    clock = StubOrca()

                    def terminal(*args, timeout=orca.CLI_TIMEOUT):
                        if args[:2] == ("terminal", "wait"):
                            clock.now += 0.1
                            if mode == "exited":
                                return {"wait": {"status": "exited", "satisfied": True, "condition": "exit"}}
                            raise orca.OrcaReplyError("timeout", "timeout")
                        if args[:2] == ("terminal", "read"):
                            if mode == "exited":
                                return {"terminal": {"tail": [], "nextCursor": "0", "latestCursor": "0"}}
                            return {"terminal": {"tail": ["working"], "nextCursor": "1", "latestCursor": "1"}}
                        if args[:2] == ("terminal", "show"):
                            return {"terminal": {"preview": "stopped"}}
                        raise AssertionError(args)

                    def sleep(seconds):
                        clock.now += seconds

                    clock.json = terminal
                    with mock.patch.object(orca.time, "monotonic", lambda: clock.now), \
                            mock.patch.object(orca.time, "sleep", side_effect=sleep):
                        data = orca.wait(clock, project.state, prepared.run_id, timeout=1.01)
                    self.assertEqual(data["status"], "queued")
                    self.assertLessEqual(clock.now - 1000, 1.010001)
                    self.assertEqual(prepared.run.read()["status"], "queued")
                finally:
                    child.communicate("x", timeout=10)

    def test_wait_storage_permission_retries_stay_inside_the_deadline(self):
        from oah import atomic

        for target in ("read", "result.json", orca.TERMINAL_LOG):
            with self.subTest(target=target):
                project = OrcaProject(self)
                prepared = project.queued()
                clock = StubOrca()
                original_read = pathlib.Path.read_bytes
                original_replace = os.replace

                def read_bytes(path):
                    if target == "read" and path == prepared.run.result_path:
                        raise PermissionError("busy")
                    return original_read(path)

                def replace(source, destination, *args, **kwargs):
                    if target != "read" and pathlib.Path(destination) == prepared.run.path / target:
                        raise PermissionError("busy")
                    return original_replace(source, destination, *args, **kwargs)

                def terminal(*args, timeout=orca.CLI_TIMEOUT):
                    if args[:2] == ("terminal", "wait"):
                        clock.now += 0.1
                        raise orca.OrcaReplyError("timeout", "timeout")
                    return {"terminal": {"tail": ["working"], "nextCursor": "1", "latestCursor": "1"}}

                def sleep(seconds):
                    clock.now += seconds

                with mock.patch.object(orca.time, "monotonic", lambda: clock.now), \
                        mock.patch.object(orca.time, "sleep", side_effect=sleep), \
                        mock.patch.object(pathlib.Path, "read_bytes", read_bytes), \
                        mock.patch.object(atomic.os, "replace", side_effect=replace), \
                        mock.patch.object(orca.Orca, "json", side_effect=terminal):
                    code, _, err = project.cli("wait", prepared.run_id, "--timeout-seconds", "1.01")
                self.assertEqual(code, 75, err)
                self.assertLessEqual(clock.now - 1000, 1.010001)
                self.assertEqual(prepared.run.read()["status"], "queued")

    def test_probe_honors_an_existing_deadline(self):
        project = OrcaProject(self)
        project.orca_state.mkdir()
        (project.orca_state / "controls.json").write_text(json.dumps(
            {"FAKE_ORCA_VERSION_SLEEP": "3"}), encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(EngineError) as ctx:
            orca.find(project.orca_command, project.env, deadline=started + 0.2)
        self.assertEqual(ctx.exception.code, orca.UNAVAILABLE)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_expired_client_starts_no_subprocess(self):
        client = orca.Orca(tuple(FAKE_ORCA), "1.4.237", {}, deadline=time.monotonic() - 1)
        with mock.patch.object(orca, "run_command") as command:
            with self.assertRaises(orca.OrcaReplyError) as ctx:
                client.json("terminal", "read")
        self.assertEqual(ctx.exception.code, "cli-timeout")
        command.assert_not_called()

    def test_cli_wait_does_not_probe_again_or_extend_the_deadline(self):
        project = OrcaProject(self)
        prepared = project.queued()
        before = len(project.orca_calls())
        started = time.monotonic()
        code, _, err = project.cli("wait", prepared.run_id, "--timeout-seconds", "1",
                                   env={"FAKE_ORCA_VERSION_SLEEP": "3"})
        elapsed = time.monotonic() - started
        self.assertEqual(code, 75, err)
        self.assertLess(elapsed, 2.5)
        self.assertNotIn(["--version"], project.orca_calls()[before:])

    def test_cli_wait_budget_starts_before_launcher_initialization(self):
        for initialization in (0.75, 1.25):
            with self.subTest(initialization=initialization):
                project = OrcaProject(self)
                prepared = project.queued()
                clock = StubOrca()
                original = call_cli.load_call_section
                budgets = []

                def slow_settings(root):
                    clock.now += initialization
                    return original(root)

                def terminal(*args, timeout=orca.CLI_TIMEOUT):
                    budgets.append(timeout)
                    clock.now += timeout
                    raise orca.OrcaReplyError("cli-timeout", "timeout")

                with mock.patch.object(orca.time, "monotonic", lambda: clock.now), \
                        mock.patch.object(call_cli, "load_call_section", side_effect=slow_settings), \
                        mock.patch.object(orca.Orca, "json", side_effect=terminal):
                    code, _, err = project.cli("wait", prepared.run_id, "--timeout-seconds", "1")
                self.assertEqual(code, 75, err)
                if initialization < 1:
                    self.assertLessEqual(clock.now - 1000, 1)
                    self.assertTrue(budgets)
                    self.assertLessEqual(max(budgets), 0.25)
                else:
                    self.assertEqual(budgets, [])

    def test_wait_budget_includes_slow_waits_reads_and_preview(self):
        for mode in ("wait", "read", "preview"):
            with self.subTest(mode=mode):
                state = make_tmp_dir(self)
                run = new_run(state, "t1", "codex", "review")
                run.result(status="queued", launcher={"name": "orca", "terminal": "term_x"})
                stub = StubOrca()
                calls = []
                budget = 305.25 if mode == "preview" else 540

                def slow(*args, timeout=orca.CLI_TIMEOUT):
                    remaining = budget - (stub.now - 1000)
                    calls.append((args[:2], timeout, remaining))
                    if args[:2] == ("terminal", "wait"):
                        stub.now += timeout if mode == "wait" else 0.1
                        if mode == "preview":
                            return {"wait": {"status": "exited", "satisfied": True, "condition": "exit"}}
                        raise orca.OrcaReplyError("timeout", "timeout")
                    stub.now += timeout
                    return {"terminal": {"tail": ["working"], "nextCursor": str(len(calls)),
                                         "latestCursor": "999", "preview": "stopped"}}

                stub.json = slow
                with mock.patch.object(orca.time, "monotonic", lambda: stub.now):
                    orca.wait(stub, state, run.id, timeout=budget)
                self.assertLessEqual(stub.now - 1000, budget + 0.000001)
                self.assertTrue(all(0 < budget <= remaining for _, budget, remaining in calls), calls)

    def test_malformed_orca_replies_fail_with_controlled_launcher_error(self):
        project = OrcaProject(self)
        prepared = project.queued()
        client = orca.find(project.orca_command, project.env)
        record = prepared.run.result_path.read_bytes()
        malformed = [[], {"ok": True, "result": []}, {"ok": True},
                     {"ok": True, "result": {}}, {"ok": True, "result": {"wait": []}},
                     {"ok": True, "result": {"wait": {"status": "running"}}},
                     {"ok": True, "result": {"wait": {"status": "exited", "satisfied": False}}},
                     {"ok": False, "error": []},
                     {"ok": False, "error": {"code": "timeout", "message": "timeout"}}]
        for payload in malformed:
            with self.subTest(payload=payload):
                prepared.run.result_path.write_bytes(record)
                (project.orca_state / "controls.json").write_text(json.dumps(
                    {"command": "terminal wait", "reply": payload}), encoding="utf-8")
                with self.assertRaises(EngineError) as ctx:
                    orca.wait(client, project.state, prepared.run_id, timeout=0.2)
                self.assertEqual(ctx.exception.code, orca.FAILED)
        for command, result in (("terminal read", {"terminal": []}),
                                ("terminal read", {"terminal": {"tail": [42]}}),
                                ("terminal show", {"terminal": {"preview": []}})):
            with self.subTest(command=command, result=result):
                prepared.run.result_path.write_bytes(record)
                handle = prepared.run.read()["launcher"]["terminal"]
                (project.orca_state / "terminals" / f"{handle}.exit").write_text("0", encoding="utf-8")
                (project.orca_state / "controls.json").write_text(json.dumps(
                    {"command": command, "reply": {"ok": True, "result": result}}), encoding="utf-8")
                with self.assertRaises(EngineError) as ctx:
                    orca.wait(client, project.state, prepared.run_id, timeout=2)
                self.assertEqual(ctx.exception.code, orca.FAILED)

    def test_success_envelope_with_failed_process_exit_is_rejected(self):
        project = OrcaProject(self)
        project.orca_state.mkdir()
        (project.orca_state / "controls.json").write_text(json.dumps(
            {"command": "worktree show", "reply": {"ok": True, "result": {"worktree": {
                "id": "fake", "path": str(project.repo)}}}, "exit_code": 3}), encoding="utf-8")
        client = orca.find(project.orca_command, project.env)
        with self.assertRaises(EngineError) as ctx:
            orca.existing_worktree(client, project.repo)
        self.assertEqual(ctx.exception.code, orca.FAILED)

    def test_wait_returns_75_while_running_then_the_final_record(self):
        project = OrcaProject(self)
        project.start_host(FAKE_SLEEP="4")
        code, out, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", "--launcher", "orca",
                                     "--background")
        self.assertEqual(code, 0, err)
        run_id = started_run(out)
        started = time.monotonic()
        code, out, err = project.cli("wait", run_id, "--timeout-seconds", "1")
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(code, 75, err)
        self.assertTrue(out.startswith(f"OAH_RUN_RUNNING run={run_id}"), out)
        self.assertIn(f"oah wait {run_id}", err)
        code, out, err = project.cli("wait", run_id, "--timeout-seconds", "60")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"OAH_RUN_FINISHED run={run_id}"), out)
        log = find_run(project.state, run_id).file_path(orca.TERMINAL_LOG)
        self.assertIn(f"oah worker {run_id}", log.read_text(encoding="utf-8"))
        waits = [argv for argv in project.orca_calls() if argv[:2] == ["terminal", "wait"]]
        self.assertTrue(waits)
        self.assertTrue(all(argv[argv.index("--for") + 1] == "exit" for argv in waits))
        reads = [argv for argv in project.orca_calls() if argv[:2] == ["terminal", "read"]]
        self.assertTrue(reads)
        self.assertTrue(all(int(argv[argv.index("--limit") + 1]) <= orca.READ_LIMIT for argv in reads))

    def test_wait_never_asks_orca_for_more_than_the_bound(self):
        state = make_tmp_dir(self)
        run = new_run(state, "t1", "codex", "review")
        run.result(status="queued", launcher={"name": "orca", "terminal": "term_x", "terminal_cursor": "0"})
        stub = StubOrca()
        with mock.patch.object(orca.time, "monotonic", lambda: stub.now), mock.patch.object(orca.time, "sleep"):
            data = orca.wait(stub, state, run.id, timeout=100_000)
        self.assertEqual(data["status"], "queued")
        self.assertEqual(sum(stub.waits), int(call.WAIT_LIMIT_S * 1000))
        self.assertTrue(all(0 < ms <= orca.WAIT_SLICE_S * 1000 for ms in stub.waits))
        self.assertEqual(run.read()["launcher"]["terminal_cursor"], "1")
        self.assertIn("working", run.file_path(orca.TERMINAL_LOG).read_text(encoding="utf-8"))

    def test_terminal_exit_without_result_is_lost(self):
        project = OrcaProject(self)
        project.orca_state.mkdir(parents=True)
        env = dict(project.env, FAKE_ORCA_OAH=json.dumps([sys.executable, "-c", "print('runner crashed'); raise SystemExit(1)"]))
        project.host = subprocess.Popen([*FAKE_ORCA, "__host"], env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(project.stop_host)
        code, out, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", "--launcher", "orca",
                                     "--background")
        self.assertEqual(code, 0, err)
        run_id = started_run(out)
        code, out, err = project.cli("wait", run_id, "--timeout-seconds", "60")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith(f"OAH_RUN_LOST run={run_id}"), out)
        self.assertTrue(err.startswith("OAH_RUN_LOST"), err)
        log = find_run(project.state, run_id).file_path(orca.TERMINAL_LOG)
        self.assertIn("runner crashed", log.read_text(encoding="utf-8"))
        self.assertFalse(project.record.exists())

    def test_foreground_orca_call_reports_the_final_record(self):
        project = OrcaProject(self, 'launcher = "orca"\n')
        project.start_host()
        code, out, err = project.cli("call", "claude", "--stage", "initial", "--tier", "low", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["status"], data["launcher"]["name"]), ("finished", "orca"))
        self.assertIn(f"OAH_RUN_STARTED run={data['run_id']}", err)


if __name__ == "__main__":
    unittest.main()
