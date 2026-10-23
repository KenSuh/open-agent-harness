# Orca 실행기 검사 — 가짜 orca 로 실행기 선택, 조용한 대체 금지, 브리프 바이트 전달, 실행 기록의 Orca 식별자, 9분 상한 대기, 기본 경로 불변을 본다.
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

from oah import briefs, call, call_cli, launchers
from oah.engines.effort import EngineError
from oah.engines.proc import resolve_command
from oah.launchers import orca
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
        orca_line = "" if orca_command == [] else f"orca_command = {toml_list(orca_command or FAKE_ORCA)}\n"
        git_commit(self.repo, {"README.md": "x\n", ".oah/config.toml":
                               f"[engines.claude]\ncommand = {toml_list(FAKE_CLAUDE)}\n"
                               f"[engines.codex]\ncommand = {toml_list(FAKE_CODEX)}\n"
                               f"[call]\n{orca_line}{config}"})
        self.state = self.repo / ".git" / "oah"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("FAKE_", "OAH_"))}
        self.env.update(CODEX_HOME=str(self.home), FAKE_RECORD=str(self.record), FAKE_ORCA_STATE=str(self.orca_state),
                        PATH=_minimal_path(self.bin))
        self.host: subprocess.Popen | None = None

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
    def test_new_worktree_run_delivers_the_exact_brief_and_records_orca_ids(self):
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
        self.assertNotEqual(worktree.resolve(), project.repo.resolve())
        self.assertTrue(worktree.name.startswith("oah-t1-"))
        self.assertEqual((info["name"], info["orca_version"], info["worktree_created"], info["worker_env"]),
                         ("orca", "1.4.237", True, "env"))
        self.assertTrue(info["terminal"].startswith("term_"))
        self.assertIsInstance(info["terminal_process_id"], int)
        self.assertEqual(info["worktree_id"], f"{info['worktree_id'].split('::')[0]}::{worktree}")
        self.assertEqual((info["reported_model"], info["reported_effort"]), (UNVERIFIED, UNVERIFIED))
        self.assertEqual((data["requested_effort"], data["launch_effort"]), ("low", "low"))
        self.assertEqual(info["command"], f"oah worker-run {run_id}; exit")

        run = find_run(project.state, run_id)
        prompt = run.prompt_path.read_bytes()
        expected = briefs.build(run_id=run_id, task="t1", stage="initial", engine="claude", scope=[],
                                intent_section=None, binding=None, packet_source=None, round_instructions=message,
                                footer=FOOTER, has_footer=has_footer)
        self.assertEqual(prompt, expected.encode("utf-8"))
        worker = project.recorded()
        self.assertEqual(worker["stdin"].encode("utf-8"), prompt)
        self.assertEqual(worker["env"], {"OAH_WORKER": "1", "OAH_RUN_ID": run_id, "OAH_TASK": "t1"})
        self.assertEqual(pathlib.Path(worker["cwd"]).resolve(), worktree.resolve())
        self.assertIn("--model", worker["argv"])

        calls = project.orca_calls()
        self.assertTrue(all("--json" in argv for argv in calls if argv != ["--version"]))
        self.assertEqual([argv[:2] for argv in calls[:5]], [["--version"], ["worktree", "show"],
                                                            ["worktree", "create"], ["worktree", "show"],
                                                            ["terminal", "create"]])
        create = calls[2]
        self.assertEqual(create[create.index("--name") + 1], worktree.name)
        self.assertIn("--no-parent", create)
        terminal = calls[4]
        self.assertEqual(terminal[terminal.index("--command") + 1], f"oah worker-run {run_id}; exit")
        self.assertEqual(terminal[terminal.index("--worktree") + 1], f"id:{info['worktree_id']}")
        flat = "\n".join(" ".join(argv) for argv in calls)
        self.assertNotIn("Build AC1", flat)
        self.assertNotIn("dispatched oah worker", flat)

    def test_existing_worktree_keeps_crlf_intent_bytes(self):
        project = OrcaProject(self)
        project.start_host()
        lane = make_tmp_dir(self) / "lane"
        add_worktree(project.repo, lane, "lane-a")
        section = SECTION.replace("Status: accepted\n", "Status: accepted\n### Problem\n한글 — café\n")
        packet = lane / ".oah" / "tasks" / "t1" / "PACKET.md"
        packet.parent.mkdir(parents=True)
        packet.write_bytes((section + "## Scope\n- src/a.py\n").replace("\n", "\r\n").encode("utf-8"))
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
        self.assertEqual(orca.runner_command("r20261023T120000Z-1a2b3c4d"),
                         "oah worker-run r20261023T120000Z-1a2b3c4d; exit")
        with self.assertRaises(briefs.BriefError) as ctx:
            orca.runner_command("r1; rm -rf x")
        self.assertEqual(ctx.exception.code, "OAH_UNSAFE_ARGUMENT")
        place = orca.Placement(pathlib.Path("wt"), "repo-1::/x y/wt", False)
        posix = orca.terminal_args(place, "oah t1 initial", "oah worker-run r1; exit", windows=False)
        windows = orca.terminal_args(place, "oah t1 initial", "oah worker-run r1; exit", windows=True)
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
        code, _, err = project.cli("worker-run", run.id)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("OAH_RUN_NOT_QUEUED"), err)
        run.result(status="finished", launcher={"name": "orca"})
        code, _, err = project.cli("worker-run", run.id)
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
