# 따라 하기 예제 검사 — examples/hello-oah 의 README 에 적힌 oah 명령을 가짜 Claude·Codex 와 가짜 node 로 처음부터 끝까지 실제로 실행한다.
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.fixtures.gates.support import StubNode, make_unlazy_dir, met_ledger, run_cli, stub_pin
from tests.helpers import git_env, make_tmp_dir
from oah.gates.inputs import collect
from oah.gates.ledger import parse_text

REPO = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "hello-oah"
FAKES = REPO / "tests" / "fakes"
TASK = "hello-name"
SH_BLOCK = re.compile(r"^```sh\n(.*?)^```", re.M | re.S)
# `oah init` asks questions on a terminal; without one, every question needs its flag.
INIT_ANSWERS = ("--without-agents", "--without-hooks", "--without-unlazy", "--without-graph", "--without-ci",
                "--without-orca", "--without-aside", "--without-jev", "--without-skill-updates")
# The packet text the README tells the reader to write.
INTENT = {
    "<what is wrong or missing today>":
        "`python hello.py` always prints `Hello, world!`; it cannot greet a person by name.",
    "<what is true when this task is done>":
        "`python hello.py Ada` prints `Hello, Ada!`, and without a name it still prints `Hello, world!`.",
    "- AC1: <checkable condition>":
        "- AC1: `python hello.py Ada` prints `Hello, Ada!`.\n- AC2: the unit tests pass, including a test for a name.",
}
GATES = f"""# Gates: {TASK}

- [ ] G1: `python hello.py Ada` prints `Hello, Ada!`
  CHECK: python hello.py Ada
  EXPECT: Hello, Ada!
  EVIDENCE: pending

- [ ] G2: the unit tests pass
  CHECK: python -m unittest discover -s tests
  EXPECT: /^OK/m
  EVIDENCE: pending
"""
# What Claude writes in the walkthrough, and what the reviewer then finds.
CLAUDE_HELLO = '''# 예제 프로그램 — 인사말을 출력한다. 따라 하기 작업에서 이름을 받도록 고친다.
"""Print a greeting. The worked example's task teaches it to greet a name."""
import sys


def greet(name: str = "world") -> str:
    return f"Hello, {name}!"


def main(argv: list[str]) -> int:
    print(greet(*argv[:1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''
CODEX_HELLO = CLAUDE_HELLO.replace("{name}!", "{name or 'world'}!")
NAME_TEST = '''
    def test_name(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")
'''
FINDING = {"title": "an empty name prints 'Hello, !'", "location": "hello.py:7"}
UNAPPROVED = {"stdout": "APPROVAL REQUIRED GATES:G1\n    NOT RUN: inspect\nAPPROVAL REQUIRED GATES:G2\n"
                        "    NOT RUN: inspect\nGATES.md: 2 gates\nUNMET: 2 (met: 0)\n  GATES:G1\n  GATES:G2\n", "code": 1}


def readme_commands(path: pathlib.Path) -> list[str]:
    """The `oah ...` lines of the README's sh blocks, in order."""
    text = path.read_text(encoding="utf-8")
    return [line.strip() for block in SH_BLOCK.findall(text) for line in block.splitlines()
            if line.strip().startswith("oah ")]


def has_walkthrough_commands() -> bool:
    """`oah fix` (lane P6b) and `oah init` (lane P7b), and the fake corrector, are on this branch."""
    return (importlib.util.find_spec("oah.fix_cli") is not None and importlib.util.find_spec("oah.init_cli") is not None
            and (FAKES / "fake_fix_codex.py").is_file())


def git(cwd: pathlib.Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=git_env())
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout


class ReadmeTest(unittest.TestCase):
    def test_readmes_list_the_same_commands(self):
        english, korean = readme_commands(EXAMPLE / "README.md"), readme_commands(EXAMPLE / "README.ko.md")
        self.assertGreaterEqual(len(english), 15)
        self.assertEqual(english, korean)

    def test_local_remote_ci_refuses_with_gh_present(self):
        from oah import land

        tmp = make_tmp_dir(self)
        remote = tmp / "hello-remote.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        marker = tmp / "gh-called"
        gh = tmp / "gh.py"
        gh.write_bytes((
            "# gh 대역 — GitHub 저장소 선택자가 아닌 로컬 원격은 네트워크 없이 거절한다.\n"
            "import os, pathlib, sys\n"
            "selector = os.environ['GH_REPO']\n"
            "if selector.startswith('oah-local-destination:') and '/' not in selector:\n"
            f"    pathlib.Path({str(marker)!r}).write_text('refused local selector', encoding='utf-8')\n"
            "    sys.exit(1)\n"
            "sys.exit(99)\n").encode("utf-8"))
        # Discovery alone is replaced; the gh stand-in is a real process that refuses a local selector.
        with patch.object(land, "find_gh", return_value=(sys.executable, str(gh))):
            check = land._ci_check(tmp, "a" * 40, dict(os.environ), destination=str(remote))
        self.assertEqual(marker.read_text(encoding="utf-8"), "refused local selector")
        self.assertEqual(check.state, "failed")
        self.assertEqual(check.code, "OAH_LAND_CI_PENDING")

    def test_real_checks_preserve_gate_inputs(self):
        work = make_tmp_dir(self) / "hello-oah"
        shutil.copytree(EXAMPLE, work, ignore=shutil.ignore_patterns("README*.md", "__pycache__"))
        git(work, "init", "-q", "-b", "main")
        ignores = []
        for name in ("README.md", "README.ko.md"):
            text = (EXAMPLE / name).read_text(encoding="utf-8")
            block = re.search(r"^```gitignore\n(.*?)^```", text, re.M | re.S)
            self.assertIsNotNone(block, "the walkthrough must ignore Python bytecode before the gates")
            ignores.append(block.group(1))
            self.assertIn(GATES, text)
        self.assertEqual(ignores[0], ignores[1])
        (work / ".gitignore").write_bytes(ignores[0].encode("utf-8"))
        (work / "hello.py").write_text(CLAUDE_HELLO, encoding="utf-8")
        test_file = work / "tests/test_hello.py"
        test_file.write_text(test_file.read_text(encoding="utf-8").replace(
            "\n\n\nif __name__", NAME_TEST + "\n\nif __name__"), encoding="utf-8")
        # Force ordinary bytecode creation even when the parent test runner disables it.
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}}
        before = collect(work, env=env).digest_for(None)
        for gate in parse_text(GATES).gates:
            argv = shlex.split(gate.check)
            self.assertEqual(argv[0], "python")
            done = subprocess.run([sys.executable, *argv[1:]], cwd=work, env=env,
                                  capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            if gate.id == "G1":
                self.assertEqual(done.stdout.strip(), "Hello, Ada!")
            else:
                self.assertRegex(done.stderr, r"(?m)^OK$")
        self.assertTrue(list(work.rglob("*.pyc")), "the real CHECK must create bytecode")
        self.assertEqual(collect(work, env=env).digest_for(None), before)


@unittest.skipUnless(has_walkthrough_commands(), "oah fix (P6b) and oah init (P7b) are not on this branch yet")
class WalkthroughTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_dir(self)
        self.work = self.tmp / "hello-oah"
        shutil.copytree(EXAMPLE, self.work, ignore=shutil.ignore_patterns("README*.md", "__pycache__"))
        self.remote = self.tmp / "hello-remote.git"
        self.state = self.tmp / "state"
        self.state.mkdir(mode=0o700)
        home, codex_home, bin_dir = self.tmp / "home", self.tmp / "codex-home", self.tmp / "bin"
        for folder in (home, codex_home, bin_dir):
            folder.mkdir()
        (codex_home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-test-a", "is_default": True,
             "supported_reasoning_levels": [{"effort": level} for level in ("low", "medium", "high", "xhigh")]}]}),
            encoding="utf-8")
        self.stub = StubNode(self.tmp / "stub")
        self.stub.reply("gate-lint.mjs", {"stdout": "LINT OK\n", "code": 0})
        unlazy_dir = make_unlazy_dir(self.tmp / "unlazy")
        self.pin = stub_pin(unlazy_dir)
        self.calls, self.fix_calls = self.tmp / "calls", self.tmp / "fix-calls"
        self.calls.mkdir()
        self.fix_calls.mkdir()
        self.review_script, self.fix_script = self.tmp / "review.json", self.tmp / "fix.json"
        self.review_script.write_text(json.dumps({"record_dir": str(self.calls), "steps": [{"findings": [FINDING]}]}),
                                      encoding="utf-8")
        self.fix_script.write_text(json.dumps({"record_dir": str(self.fix_calls), "state_dir": str(self.state),
                                               "steps": [{"write": {"hello.py": CODEX_HELLO}}]}), encoding="utf-8")
        # Only git and the stand-ins for claude and codex are on PATH: no real engine, gh or gitleaks runs.
        git_dir = pathlib.Path(shutil.which("git")).parent
        if os.name != "nt":
            os.symlink(shutil.which("git"), bin_dir / "git")
            git_dir = bin_dir
            for name in ("claude", "codex"):  # signed in, as their status commands report it
                tool = bin_dir / name
                tool.write_text(f"#!{sys.executable}\nprint('{{}}')\n", encoding="utf-8")
                tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("FAKE_", "OAH_", "GIT_")) and key != "CLAUDE_CONFIG_DIR"}
        self.env.update(self.stub.env(OAH_UNLAZY_DIR=str(unlazy_dir), OAH_STATE_DIR=str(self.state)))
        self.env.update({key: value for key, value in git_env().items() if key.startswith("GIT_")})
        self.env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex_home),
                        PATH=os.pathsep.join(dict.fromkeys([str(bin_dir), str(git_dir)])))
        self.ran: list[str] = []

    def oah(self, command: str, *extra: str, code: int = 0) -> str:
        """Run one README command (plus test-only arguments) and return its output."""
        self.ran.append(command)
        argv = shlex.split(command)[1:] + list(extra)
        done = run_cli(argv, cwd=self.work, env=self.env, pin=self.pin)
        output = done.stdout + done.stderr
        self.assertEqual(done.returncode, code, f"$ {command}\n{output}")
        return output

    def commit(self, message: str, engine: str) -> None:
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message, "-m", f"Agent-Engine: {engine}")

    def test_walkthrough_runs_end_to_end(self):
        work = self.work
        # 1. The documented local practice alternative; GitHub itself is not exercised offline.
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(work, "init", "-q", "-b", "main")
        self.commit("start hello-oah", "human")
        git(work, "remote", "add", "origin", str(self.remote))  # landing needs an absolute path or URL
        git(work, "push", "-q", "-u", "origin", "main")

        # 2. Guided setup.
        output = self.oah("oah init", *INIT_ANSWERS) if os.name != "nt" else self.init_without_engines()
        self.assertIn("Impact map: built", output)
        config = work / ".oah/config.toml"
        settings = config.read_text(encoding="utf-8")
        settings = settings.replace('command = ["claude"]', "command = " + json.dumps(
            [sys.executable, str(FAKES / "fake_claude.py")]))
        settings = settings.replace('command = ["codex"]', "command = " + json.dumps(
            [sys.executable, str(FAKES / "fake_fix_codex.py"), "--fix-script", str(self.fix_script),
             "--script", str(self.review_script)]))
        config.write_text(settings, encoding="utf-8")
        ignores = re.search(r"^```gitignore\n(.*?)^```", (EXAMPLE / "README.md").read_text(encoding="utf-8"),
                            re.M | re.S).group(1)
        with (work / ".gitignore").open("a", encoding="utf-8") as stream:
            stream.write("\n" + ignores)
        self.commit("set up oah", "human")
        git(work, "push", "-q")

        # 3. The task: a branch, a packet with its intent, and the gates.
        git(work, "switch", "-q", "-c", TASK)
        self.assertIn("OAH_PACKET_CREATED", self.oah(f"oah packet new {TASK} --scope hello.py --scope tests"))
        packet = work / f".oah/tasks/{TASK}/PACKET.md"
        text = re.sub(r"^Status: draft.*$", "Status: accepted", packet.read_text(encoding="utf-8"), flags=re.M)
        for placeholder, written in INTENT.items():
            text = text.replace(placeholder, written)
        packet.write_text(text, encoding="utf-8")
        self.assertIn("OAH_GATES_CREATED", self.oah(f"oah gates new {TASK}"))
        ledger = work / f".oah/tasks/{TASK}/GATES.md"
        ledger.write_text(GATES, encoding="utf-8")
        self.oah(f"oah gates lint {TASK}")
        self.assertIn("warnings=0", self.oah(f"oah packet check {TASK}"))
        self.commit(f"packet for {TASK}", "human")

        # 4. Preflight and the lease.
        self.assertIn("verdict=PROCEED ", self.oah(f"oah preflight --task {TASK}"))
        self.assertIn(f"intent={TASK}@r1", self.oah(f"oah start --task {TASK} --agent claude "
                                                    f"--packet .oah/tasks/{TASK}/PACKET.md"))

        # 5. Claude implements (the fake stands in for its edits).
        self.assertIn("engine=claude stage=initial", self.oah(f"oah call claude --stage initial --task {TASK}"))
        (work / "hello.py").write_text(CLAUDE_HELLO, encoding="utf-8")
        test_file = work / "tests/test_hello.py"
        test_file.write_text(test_file.read_text(encoding="utf-8").replace(
            "\n\n\nif __name__", NAME_TEST + "\n\nif __name__"), encoding="utf-8")

        # 6. Wiki, gates and the commit: maintenance must not stale passing evidence.
        passing = {"write": {str(ledger): met_ledger(GATES)},
                   "stdout": "  PASS GATES:G1: G1\n  PASS GATES:G2: G2\nGATES.md: 2 gates\nALL MET (2 met, reran: 2)\n", "code": 0}
        self.stub.reply("gate-check.mjs", UNAPPROVED, passing)
        self.assertIn(f"oah gates check {TASK} --approve", self.oah(f"oah gates check {TASK}", code=1))
        self.assertIn("OAH_WIKI_MAINTAINED", self.oah(f"oah wiki maintain --task {TASK}"))
        checked_inputs = collect(work, env=self.env).digest_for(None)
        self.assertIn("OAH_GATES_CHECKED", self.oah(f"oah gates check {TASK} --approve"))
        self.commit("greet a name", "claude")
        self.assertEqual(collect(work, env=self.env).digest_for(None), checked_inputs)

        # 7. Codex reviews, corrects, and reviews again.
        self.assertIn("OAH_REVIEW_CHANGES_REQUESTED", self.oah(f"oah review --task {TASK}", code=1))
        self.assertIn("OAH_FIX_APPLIED", self.oah(f"oah fix --task {TASK}"))
        self.assertEqual((work / "hello.py").read_text(encoding="utf-8"), CODEX_HELLO)
        self.assertIn("OAH_WIKI_MAINTAINED", self.oah(f"oah wiki maintain --task {TASK}"))
        self.assertIn("OAH_GATES_CHECKED", self.oah(f"oah gates check {TASK} --approve"))
        self.commit("fall back to world for an empty name", "codex")
        self.assertIn("OAH_FIX_BOUND", self.oah(f"oah fix --task {TASK} --bind"))
        self.assertIn("OAH_REVIEW_CONVERGED", self.oah(f"oah review --task {TASK}"))

        # 8. Finish with the return.
        shown = self.oah(f"oah intent show {TASK}")
        intent_line = next(line for line in shown.splitlines() if line.startswith("INTENT "))
        (self.tmp / f"{TASK}-return.txt").write_text(
            f"{intent_line}\nAC1: met — gate G1\nAC2: met — gate G2\n", encoding="utf-8")
        finish = f'oah finish --task {TASK} --agent claude --summary "greet a name" --return ../{TASK}-return.txt'
        self.assertIn("gates=met", self.oah(finish))

        # 9. Local refusal checks only. The real GitHub landing route needs signed-in gh and CI.
        git(work, "push", "-q", "-u", "origin", TASK)
        self.assertIn("## Review summary", self.oah(f"oah review summary --task {TASK}"))
        landed_before = git(self.remote, "rev-parse", "main")
        check = self.oah(f"oah land --check --task {TASK}", code=1)
        for name in ("lease", "intent", "review", "gates", "impact", "fast-forward"):
            self.assertIn(f"\n{name}: passed", "\n" + check)
        self.assertIn("OAH_LAND_SCAN_REQUIRED", check)
        self.assertIn("ci: unverified", check)
        self.assertIn("OAH_LAND_CI_UNVERIFIED", check)
        merge = self.oah(f"oah land --merge --task {TASK}", "--confirm", git(work, "rev-parse", "HEAD").strip(), code=1)
        self.assertIn("Nothing was pushed.", merge)
        self.assertEqual(git(self.remote, "rev-parse", "main"), landed_before)
        self.assertEqual(self.ran, readme_commands(EXAMPLE / "README.md"))

    def init_without_engines(self) -> str:
        """On Windows the stand-ins cannot be on PATH, so init reports the two sign-ins as still to do."""
        self.ran.append("oah init")
        done = run_cli(["init", *INIT_ANSWERS], cwd=self.work, env=self.env, pin=self.pin)
        output = done.stdout + done.stderr
        last = output.strip().splitlines()[-1]
        self.assertIn(done.returncode, (0, 1), output)
        self.assertRegex(last, r"^OAH_INIT_(DONE|INCOMPLETE) .*impact=built", output)
        if last.startswith("OAH_INIT_INCOMPLETE"):
            self.assertIn("pending=claude,codex", last)
        return output


if __name__ == "__main__":
    unittest.main()
