# 프로젝트 도구 실행기 검사 — 실제 자식 프로세스로 인자 문법·JSON 한 줄·고정 종료 코드·오프라인 구조를 보고, 빌드된 독립 실행 파일이 주어지면 그 버전과 지도 신원이 파이썬 소스와 같은지 본다.
"""Run: python -m unittest tests.test_project_tools_runner

BinaryTest needs a really built sidecar: OAH_PROJECT_TOOLS_BIN=<path to oah-project-tools[.exe]>. Without it those
tests are reported as skipped, never as passed.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from oah import __version__
from oah import project_tools_impact as impact
from oah import project_tools_runner as runner
from oah.impact import mapstore
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
BINARY = os.path.abspath(os.environ["OAH_PROJECT_TOOLS_BIN"]) if os.environ.get("OAH_PROJECT_TOOLS_BIN") else None
NO_BINARY = "OAH_PROJECT_TOOLS_BIN is not set to a built oah-project-tools binary"
BASE = {"src/app.py": "from lib import util\n", "src/lib/util.py": "VALUE = 1\n", "docs/guide.md": "See src/app.py.\n"}
DOCUMENT = ["schema", "tool", "action", "ok", "state", "reason", "performed", "changed", "root", "branch", "head",
            "store", "map", "graph", "runner", "git", "error", "warnings"]
NETWORK = ("socket", "_socket", "ssl", "_ssl", "http.client", "urllib.request", "ftplib", "smtplib", "asyncio")


def child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ, PYTHONPATH=SRC, **extra)
    env.pop("OAH_STATE_DIR", None)
    return env


def run_source(*args: str) -> subprocess.CompletedProcess:
    """The runner as a real child process, from this checkout's sources."""
    return subprocess.run([sys.executable, "-m", "oah.project_tools_runner", *args], capture_output=True,
                          env=child_env(), timeout=180)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stopped_by_term(test: unittest.TestCase, argv: list[str], env: dict[str, str]) -> tuple[int, bytes, float]:
    """Start a refresh whose git never answers, stop it the way the desktop app does (TERM to the runner's own
    process group) and return (exit status, standard output, seconds until the runner was gone).

    The stand-in git answers --version, then starts a child of its own and waits; both must be gone afterwards.
    """
    folder = make_tmp_dir(test)
    pids, tool = folder / "pids", folder / "git"
    tool.write_text("#!/bin/sh\n"
                    "case \"$1\" in --version) echo 'git version 2.50.0'; exit 0;; esac\n"
                    "sleep 600 &\n"
                    f"echo \"$$ $!\" > '{pids}.tmp' && mv '{pids}.tmp' '{pids}'\n"
                    "wait\n", encoding="utf-8")
    tool.chmod(0o755)
    repo = make_git_repo(test)
    git_commit(repo, BASE)
    runner = subprocess.Popen([*argv, "impact", "refresh", "--root", str(repo), "--git", str(tool)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)

    def sweep() -> None:
        for pid in [runner.pid, *(int(text) for text in (pids.read_text().split() if pids.exists() else []))]:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        runner.communicate()

    test.addCleanup(sweep)
    deadline = time.monotonic() + 60
    while not pids.exists() and runner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    test.assertTrue(pids.exists(), "the runner never reached its first git call")
    children = [int(text) for text in pids.read_text().split()]
    test.assertEqual([pid_alive(pid) for pid in children], [True, True])
    started = time.monotonic()
    os.killpg(runner.pid, signal.SIGTERM)
    out, _err = runner.communicate(timeout=60)
    seconds = time.monotonic() - started
    deadline = time.monotonic() + 10
    while any(pid_alive(pid) for pid in children) and time.monotonic() < deadline:
        time.sleep(0.05)
    test.assertEqual([pid_alive(pid) for pid in children], [False, False], "git outlived the stopped runner")
    test.assertFalse((repo / ".git" / "oah").exists(), "a stopped run wrote to the store")
    return runner.returncode, out, seconds


def document(done: subprocess.CompletedProcess) -> dict:
    """The one JSON line a run printed; anything else on standard output fails the test."""
    assert done.stdout.isascii() and done.stdout.endswith(b"\n") and done.stdout.count(b"\n") == 1, done.stdout
    return json.loads(done.stdout)


class ContractTest(unittest.TestCase):
    def test_version_is_one_exact_line(self):
        done = run_source("--version")
        self.assertEqual((done.returncode, done.stdout.replace(b"\r\n", b"\n"), done.stderr),
                         (0, f"oah-project-tools {__version__}\n".encode(), b""))

    def test_an_impact_call_answers_one_ascii_json_line(self):
        repo = make_tmp_dir(self) / "프로젝트 저장소"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        head = git_commit(repo, BASE)
        seen = run_source("impact", "status", "--root", str(repo))
        answer = document(seen)
        self.assertEqual((seen.returncode, seen.stderr, list(answer)), (0, b"", DOCUMENT))
        self.assertEqual((answer["schema"], answer["tool"], answer["action"], answer["ok"], answer["state"]),
                         (1, "impact", "status", True, "missing"))
        self.assertEqual((answer["root"], answer["head"], answer["runner"]),
                         (str(repo), head, {"name": "oah-project-tools", "version": __version__, "frozen": False}))
        built = run_source("impact", "refresh", "--root", str(repo))
        self.assertEqual((built.returncode, document(built)["performed"]), (0, "built"))
        again = document(run_source("impact", "refresh", "--root", str(repo)))
        self.assertEqual((again["performed"], again["state"], again["map"]["commit"]), ("none", "fresh", head))

    def test_wrong_arguments_exit_1_with_a_usage_document(self):
        root = str(make_tmp_dir(self))
        for args in ([], ["impact"], ["impact", "build", "--root", root], ["status", "--root", root],
                     ["jev", "status", "--root", root], ["wiki-index", "refresh", "--root", root],
                     ["impact", "status"], ["impact", "status", "--root"], ["impact", "status", "--root", root, "x"],
                     ["impact", "status", "--root", root, "--root", root], ["impact", "status", "--root", root, "--json"],
                     ["--version", "extra"]):
            done = run_source(*args)
            answer = document(done)
            self.assertEqual((done.returncode, answer["ok"], answer["tool"], answer["error"]["code"]),
                             (1, False, None, "OAH_PROJECT_TOOLS_USAGE"), args)
            self.assertIn("usage: oah-project-tools", answer["error"]["message"])

    def test_refusals_exit_69_and_prerequisites_are_states(self):
        plain = make_tmp_dir(self)
        seen = run_source("impact", "status", "--root", str(plain))
        self.assertEqual((seen.returncode, document(seen)["state"]), (0, "not_a_repository"))
        tried = run_source("impact", "refresh", "--root", str(plain))
        self.assertEqual((tried.returncode, document(tried)["state"], document(tried)["error"]["code"]),
                         (69, "not_a_repository", "OAH_NOT_A_REPO"))
        relative = run_source("impact", "status", "--root", "some/folder")
        self.assertEqual((relative.returncode, document(relative)["error"]["code"]),
                         (69, "OAH_PROJECT_TOOLS_ROOT_INVALID"))
        untrusted = run_source("impact", "status", "--root", str(make_git_repo(self)), "--git", "git")
        self.assertEqual((untrusted.returncode, document(untrusted)["error"]["code"]),
                         (69, "OAH_PROJECT_TOOLS_GIT_UNTRUSTED"))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes in a folder are a POSIX feature")
    def test_a_pipe_in_place_of_a_tracked_file_does_not_hold_the_run(self):
        repo = make_git_repo(self)
        git_commit(repo, BASE)
        (repo / "docs" / "guide.md").unlink()
        os.mkfifo(repo / "docs" / "guide.md")  # reading it would wait for a writer forever
        done = subprocess.run([sys.executable, "-m", "oah.project_tools_runner", "impact", "refresh", "--root",
                               str(repo)], capture_output=True, env=child_env(), timeout=120)
        answer = document(done)
        self.assertEqual((done.returncode, answer["performed"], answer["map"]["dirty"]), (0, "built", 1), done.stderr)
        self.assertEqual(document(run_source("impact", "status", "--root", str(repo)))["state"], "fresh")

    @unittest.skipIf(os.name == "nt", "TERM is the POSIX stop; on Windows the caller ends its Job Object")
    def test_term_stops_the_run_and_its_git_without_an_answer(self):
        code, out, seconds = stopped_by_term(self, [sys.executable, "-m", "oah.project_tools_runner"], child_env())
        self.assertEqual((code, out), (128 + signal.SIGTERM, b""))
        self.assertLess(seconds, 10)

    def test_a_run_loads_no_network_module(self):
        repo = make_git_repo(self)
        git_commit(repo, BASE)
        code = ("import contextlib, io, json, sys\n"
                "from oah import project_tools_runner as runner\n"
                "with contextlib.redirect_stdout(io.StringIO()):\n"
                "    codes = [runner.main(['impact', action, '--root', sys.argv[1]]) for action in ('refresh', 'status')]\n"
                "print(json.dumps({'codes': codes, 'modules': sorted(sys.modules)}))\n")
        done = subprocess.run([sys.executable, "-c", code, str(repo)], capture_output=True, text=True,
                              env=child_env(), timeout=180)
        seen = json.loads(done.stdout)
        self.assertEqual(seen["codes"], [0, 0], done.stderr)
        loaded = [name for name in seen["modules"] if name in NETWORK
                  or name.startswith(("oah.rerank", "oah.secrets", "oah.benchmarks", "oah.impact.defaultbranch"))]
        self.assertEqual(loaded, [])


class MainTest(unittest.TestCase):
    def call(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.main(list(args))
        return code, json.loads(out.getvalue())

    def test_every_call_is_an_app_selected_call(self):
        answer = impact.failure("refresh", "/x", "OAH_PROJECT_TOOLS_IO", "stand-in")
        with mock.patch.object(impact, "refresh", return_value=answer) as refresh:
            code, shown = self.call("impact", "refresh", "--root", "/x", "--git", "/usr/bin/git")
        refresh.assert_called_once_with("/x", selected=True, git="/usr/bin/git")
        self.assertEqual((code, shown["error"]["code"]), (69, "OAH_PROJECT_TOOLS_IO"))

    def test_an_unexpected_failure_exits_70_in_the_same_shape(self):
        with mock.patch.object(impact, "status", side_effect=RuntimeError("boom")):
            code, shown = self.call("impact", "status", "--root", "/x")
        self.assertEqual((code, shown["ok"], shown["state"], shown["error"]["code"]),
                         (70, False, "unavailable", "OAH_PROJECT_TOOLS_INTERNAL"))
        self.assertEqual((shown["error"]["message"], list(shown)), ("RuntimeError: boom", DOCUMENT))

    def test_an_answer_over_the_output_limit_is_replaced(self):
        huge = impact.failure("status", "/x", "OAH_PROJECT_TOOLS_IO", "m")
        object.__setattr__(huge, "store", {"path": "p" * 5000, "source": "git-common-dir"})
        with mock.patch.object(impact, "status", return_value=huge), mock.patch.object(runner, "OUTPUT_LIMIT", 4096):
            code, shown = self.call("impact", "status", "--root", "/x")
        self.assertEqual((code, shown["error"]["code"]), (69, "OAH_PROJECT_TOOLS_INTERNAL"))

    def test_the_stop_handlers_are_put_back(self):
        before = [signal.getsignal(getattr(signal, name)) for name in runner.STOP_SIGNALS if hasattr(signal, name)]
        seen = []
        with mock.patch.object(impact, "status", side_effect=lambda *a, **k: seen.append(
                signal.getsignal(signal.SIGTERM)) or impact.failure("status", "/x", "OAH_PROJECT_TOOLS_IO", "x")):
            self.call("impact", "status", "--root", "/x")
        self.assertNotIn(seen[0], (signal.SIG_DFL, signal.SIG_IGN, before[0]), "no stop handler during the run")
        self.assertEqual([signal.getsignal(getattr(signal, name)) for name in runner.STOP_SIGNALS
                          if hasattr(signal, name)], before)

    def test_an_incomplete_frozen_bundle_is_refused(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(mapstore, "__file__", str(make_tmp_dir(self) / "mapstore.pyc")):
            code, shown = self.call("impact", "status", "--root", "/x")
        self.assertEqual((code, shown["error"]["code"]), (70, "OAH_PROJECT_TOOLS_INTERNAL"))
        self.assertIn("incomplete", shown["error"]["message"])


@unittest.skipUnless(BINARY, NO_BINARY)
class BinaryTest(unittest.TestCase):
    """The standalone sidecar itself. It must not need this checkout, PYTHONPATH or an installed Python."""

    def run_binary(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = {name: value for name, value in os.environ.items() if not name.upper().startswith("PYTHON")}
        env.pop("OAH_STATE_DIR", None)
        env.update(extra)
        return subprocess.run([BINARY, *args], capture_output=True, env=env, cwd=make_tmp_dir(self), timeout=300)

    def test_version_is_the_package_version(self):
        done = self.run_binary("--version")
        self.assertEqual((done.returncode, done.stdout.replace(b"\r\n", b"\n")),
                         (0, f"oah-project-tools {__version__}\n".encode()))

    def test_the_map_identity_equals_the_python_source(self):
        repo = make_tmp_dir(self) / "프로젝트"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        head = git_commit(repo, BASE)
        (repo / "untracked.md").write_text("src/lib/util.py\n", encoding="utf-8")
        done = self.run_binary("impact", "refresh", "--root", str(repo))
        built = document(done)
        self.assertEqual((done.returncode, built["performed"], built["runner"]["frozen"], built["map"]["commit"]),
                         (0, "built", True, head), done.stderr)
        source = mapstore.build(repo, env={"OAH_STATE_DIR": str(make_tmp_dir(self) / "state")})
        self.assertEqual((built["map"]["tool"], built["map"]["manifest"], built["map"]["result"]),
                         (source.tool_digest, source.manifest_digest, source.result_digest))
        self.assertEqual(mapstore.status(repo, env={})[0].action, "fresh", "the source CLI accepts the sidecar's map")
        seen = document(self.run_binary("impact", "status", "--root", str(repo)))
        self.assertEqual((seen["state"], seen["performed"]), ("fresh", "none"))
        (repo / "src" / "app.py").write_text("from lib import util\nprint(util.VALUE)\n", encoding="utf-8")
        self.assertEqual(document(self.run_binary("impact", "status", "--root", str(repo)))["state"], "stale")
        self.assertEqual(impact.refresh(repo).performed, "refreshed")
        self.assertEqual(document(self.run_binary("impact", "status", "--root", str(repo)))["state"], "fresh",
                         "the sidecar accepts the source CLI's map")

    @unittest.skipIf(os.name == "nt", "TERM is the POSIX stop; on Windows the caller ends its Job Object")
    def test_term_stops_the_binary_its_git_and_leaves_no_unpacked_folder(self):
        unpack = make_tmp_dir(self)
        env = {name: value for name, value in os.environ.items() if not name.upper().startswith("PYTHON")}
        env.pop("OAH_STATE_DIR", None)
        code, out, seconds = stopped_by_term(self, [BINARY], dict(env, TMPDIR=str(unpack)))
        self.assertEqual((code, out), (128 + signal.SIGTERM, b""))
        self.assertLess(seconds, 10)
        self.assertEqual(sorted(unpack.iterdir()), [], "the one-file runner left its unpacked folder behind")

    def test_python_variables_and_repository_programs_do_not_run(self):
        marker = make_tmp_dir(self) / "ran"
        inject = make_tmp_dir(self)
        hostile = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        for name in ("sitecustomize.py", "usercustomize.py", "startup.py"):
            (inject / name).write_text(hostile, encoding="utf-8")
        (inject / "oah").mkdir()
        (inject / "oah" / "__init__.py").write_text(hostile + "__version__ = '0.0.0'\n", encoding="utf-8")
        repo = make_git_repo(self)
        git_commit(repo, BASE)
        tool = make_tmp_dir(self) / "tool.sh"
        tool.write_text(f"#!/bin/sh\n: > '{marker.as_posix()}'\n", encoding="utf-8")
        tool.chmod(0o755)
        subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", tool.as_posix()], check=True, env=git_env())
        variables = {"PYTHONPATH": str(inject), "PYTHONSTARTUP": str(inject / "startup.py"), "PYTHONINSPECT": "",
                     "PYTHONHOME": str(inject)}
        version = self.run_binary("--version", **variables)
        self.assertEqual(version.stdout.replace(b"\r\n", b"\n"), f"oah-project-tools {__version__}\n".encode())
        done = self.run_binary("impact", "refresh", "--root", str(repo), **variables)
        self.assertEqual((done.returncode, document(done)["performed"]), (0, "built"), done.stderr)
        self.assertFalse(marker.exists(), "an injected module or the repository's program ran")


if __name__ == "__main__":
    unittest.main()
