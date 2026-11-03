# 도구 업데이트 패스 검사 — 임시 홈과 가짜 claude·codex 만 있는 bin 폴더에서 스위치·판정·기록·잠금·Codex 플러그인 수동 업데이트 안내·실행 중 세션 안내를 본다.
from __future__ import annotations

import ast
import datetime as dt
import itertools
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import unittest
from unittest import mock

from oah.lockfile import StateLock
from oah.toolupdate import probes, records, steps
from oah.toolupdate import run as passes
from tests.helpers import ToolUpdateTestCase, make_tmp_dir, require_node, toolupdate_module_isolation

_module_isolation = None


def setUpModule():
    global _module_isolation
    _module_isolation = toolupdate_module_isolation()
    _module_isolation.__enter__()


def tearDownModule():
    global _module_isolation
    _module_isolation.__exit__(None, None, None)
    _module_isolation = None

FORBIDDEN_WORDS = {"-y", "--yes", "--accept-command", "mcp"}
FORBIDDEN_PROGRAMS = {"npm", "npx", "brew", "ps", "tasklist", "pgrep", "pkill", "kill", "taskkill"}

# Help text as Claude Code 2.1.316 prints it (trimmed to the parts the pass reads).
CLAUDE_HELP = {
    "": """Usage: claude [options] [command] [prompt]

Claude Code - starts an interactive session by default, use -p/--print for
non-interactive output

Arguments:
  prompt                                Your prompt

Options:
  -h, --help                            Display help for command

Commands:
  agents [options]                      Manage background agents
  doctor                                Check the health of your Claude Code
                                        installation. Reads settings files in
                                        the current directory without a trust
                                        prompt.
  install [options] [target]            Install Claude Code native build. Use
                                        [target] to specify version (stable,
                                        latest, or specific version)
  mcp                                   Configure and manage MCP servers
  plugin|plugins                        Manage Claude Code plugins
  update|upgrade                        Check for updates and install if
                                        available; package-manager installs
                                        update through the package manager
""",
    "plugin": """Usage: claude plugin|plugins [options] [command]

Manage Claude Code plugins

Options:
  -h, --help                           Display help for command

Commands:
  install|i [options] <plugin>         Install a plugin from available
                                       marketplaces (use plugin@marketplace for
                                       specific marketplace)
  list [options]                       List installed plugins
  marketplace                          Manage Claude Code marketplaces
  uninstall|remove [options] <plugin>  Uninstall an installed plugin
  update [options] <plugin>            Update a plugin to the latest version
                                       (restart required to apply)
""",
    "plugin list": """Usage: claude plugin list [options]

List installed plugins

Options:
  --available           Include available plugins from marketplaces (requires
                        --json)
  -h, --help            Display help for command
  --json                Output as JSON
""",
    "plugin update": """Usage: claude plugin update [options] <plugin>

Update a plugin to the latest version (restart required to apply)

Options:
  --accept-command <sha256>  Accept the marketplace-declared command (a
                             command-source install, or the headersHelper that
                             fetches the archive) whose sha256 a previous --json
                             run reported as shownCommand.sha256
  -h, --help                 Display help for command
  --json                     Print one machine-readable result line on stdout
                             instead of the human message (same exit codes; a
                             marketplace-declared command is still shown and
                             must be confirmed — pass -y when not interactive)
  -s, --scope <scope>        Installation scope: user, project, local, managed
                             (default: auto-detect)
  -y, --yes                  Accept the displayed marketplace-declared command
                             without the confirmation prompt
""",
    "plugin marketplace": """Usage: claude plugin marketplace [options] [command]

Manage Claude Code marketplaces

Options:
  -h, --help                  Display help for command

Commands:
  add [options] <source>      Add a marketplace from a URL, path, or GitHub repo
  list [options]              List all configured marketplaces
  remove|rm [options] <name>  Remove a configured marketplace
  update [options] [name]     Update marketplace(s) from their source - updates
                              all if no name specified
""",
}
# Help text as codex-cli 0.166.0 prints it (trimmed).
CODEX_HELP = {
    "": """Codex CLI

If no subcommand is specified, options will be forwarded to the interactive CLI.

Usage: codex [OPTIONS] [PROMPT]
       codex [OPTIONS] <COMMAND> [ARGS]

Commands:
  exec              Run Codex non-interactively [aliases: e]
  mcp               Manage external MCP servers for Codex
  plugin            Manage Codex plugins
  update            Update Codex to the latest version
  apply             Apply the latest diff produced by Codex agent as a `git apply` to your local
                    working tree [aliases: a]
  help              Print this message or the help of the given subcommand(s)

Arguments:
  [PROMPT]
          Optional user prompt to start the session

Options:
  -h, --help
          Print help (see a summary with '-h')
""",

}

# The fake CLI: one Python script that plays claude or codex from a JSON world file and logs every argv.
FAKE_TOOL = r'''
import json, os, pathlib, sys, time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    world_path = pathlib.Path(os.environ["FAKE_TOOLS_WORLD"])
    world = json.loads(world_path.read_text(encoding="utf-8"))
    with open(world["log"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": tool, "argv": args, "cwd": os.getcwd()}) + "\n")
    me = world[tool]

    def save():
        temp = world_path.with_name(world_path.name + ".tmp")
        temp.write_text(json.dumps(world), encoding="utf-8")
        os.replace(temp, world_path)

    def act(spec):
        time.sleep(float(spec.get("sleep", 0)))
        if "stdout_hex" in spec:
            sys.stdout.buffer.write(bytes.fromhex(spec["stdout_hex"]))
        sys.stdout.write(spec.get("stdout", ""))
        sys.stdout.flush()
        if "stderr_hex" in spec:
            sys.stderr.buffer.write(bytes.fromhex(spec["stderr_hex"]))
        sys.stderr.write(spec.get("stderr", ""))
        sys.stderr.flush()
        return int(spec.get("exit", 0))

    if args == ["--version"]:
        if "version_hex" in me:
            sys.stdout.buffer.write(bytes.fromhex(me["version_hex"]))
            return 0
        if me.get("version") is None:
            print("unknown build")
            return 0
        print(f"{me['version']} (Claude Code)" if tool == "claude" else f"codex-cli {me['version']}")
        return 0
    if args and args[-1] == "--help":
        text = me["help"].get(" ".join(args[:-1]))
        if isinstance(text, dict):
            return act(text)
        if text is None:
            print(f"error: unknown command {' '.join(args[:-1])}", file=sys.stderr)
            return 2
        sys.stdout.write(text)
        return 0
    if args == ["update"]:
        spec = me.get("update", {})
        code = act(spec)
        if "set_version" in spec:
            me["version"] = spec["set_version"]
            save()
        return code
    if tool == "claude" and args == ["plugin", "list", "--json"]:
        print(json.dumps(me.get("plugins", []), indent=2))
        return 0
    if tool == "claude" and args == ["plugin", "marketplace", "update"]:
        spec = me.get("marketplace_update", {"stdout": "All marketplaces are up to date.\n"})
        code = act(spec)
        if "set_revision" in spec:
            pathlib.Path(world["marketplace_head"]).write_text(spec["set_revision"], encoding="utf-8")
        return code
    if tool == "claude" and args[:2] == ["plugin", "update"] and len(args) >= 3:
        spec = me.get("plugin_update", {}).get(args[2], {})
        code = act(spec)
        if "json" in spec:
            print(json.dumps(spec["json"]))
        if "set_version" in spec:
            for entry in me.get("plugins", []):
                if entry.get("id") == args[2] and entry.get("scope") == "user":
                    entry["version"] = spec["set_version"]
            save()
        return code
    print(f"fake {tool}: unsupported arguments {args}", file=sys.stderr)
    return 2


sys.exit(main())
'''
# Windows runs npm-installed CLIs through node.exe and the package's bin script (oah never runs a .cmd file).
NODE_FORWARDER = """const {spawnSync} = require("child_process");
const result = spawnSync(process.env.FAKE_TOOLS_PYTHON,
                         [process.env.FAKE_TOOLS_SCRIPT, "TOOL", ...process.argv.slice(2)], {stdio: "inherit"});
process.exit(result.status === null ? 1 : result.status);
"""
CLAUDE_CURRENT = "Current version: 2.1.316\nChecking for updates to latest version...\nClaude is up to date!\n"
CODEX_CURRENT = ("Updating Codex via `brew upgrade --cask codex`...\n"
                 "Warning: Not upgrading codex, the latest version is already installed\n")


def toml_config(marketplaces: dict[str, str | None], plugins: tuple[str, ...] = ()) -> str:
    """Codex's config.toml as Codex 0.166.0 writes Git marketplaces and installed plugins."""
    lines = []
    for name, revision in marketplaces.items():
        lines += [f"[marketplaces.{name}]", 'source_type = "git"', f'source = "https://example.invalid/{name}.git"']
        if revision is not None:
            lines += [f'last_revision = "{revision}"', 'last_updated = "2026-11-01T00:00:00Z"']
        lines.append("")
    for ident in plugins:
        lines += [f'[plugins."{ident}"]', "enabled = true", ""]
    return "\n".join(lines)


class FakeTools:
    """A temporary home and a bin folder that holds only fake claude and codex, which log every argv."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.tmp = make_tmp_dir(test)
        self.home = test.home
        self.bin = self.tmp / "bin"
        self.codex_home = self.home / ".codex"
        for folder in (self.home, self.bin, self.codex_home):
            folder.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text("", encoding="utf-8")
        git = self.home / ".claude" / "plugins" / "marketplaces" / "m" / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text("a" * 40, encoding="utf-8")
        (self.home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"m": {"installLocation": str(git.parent)}}), encoding="utf-8")
        self.log = self.tmp / "argv.jsonl"
        self.world_path = self.tmp / "world.json"
        self.script = self.bin / "fake_tool.py"
        self.script.write_text(FAKE_TOOL, encoding="utf-8")
        self.world = {"log": str(self.log), "marketplace_head": str(git / "HEAD"),
                      "claude": {"version": "2.1.316", "help": dict(CLAUDE_HELP), "plugins": [],
                                 "update": {"stdout": CLAUDE_CURRENT}},
                      "codex": {"version": "0.166.0", "help": dict(CODEX_HELP),
                                "update": {"stderr": CODEX_CURRENT}}}
        self.save()
        node = pathlib.Path(require_node(test)) if os.name == "nt" else None
        for name in ("claude", "codex"):
            self._install(name, node)
        self.env = dict(test.isolated_env, **{"PATH": str(self.bin), "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "CODEX_HOME": str(self.codex_home), "FAKE_TOOLS_WORLD": str(self.world_path),
                    "FAKE_TOOLS_PYTHON": sys.executable, "FAKE_TOOLS_SCRIPT": str(self.script), "CLAUDE_CONFIG_DIR": str(self.home / ".claude")})
        for name in ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC"):
            if os.name == "nt" and name in os.environ:
                self.env[name] = os.environ[name]

    def _install(self, name: str, node: pathlib.Path | None) -> None:
        if node is None:
            tool = self.bin / name
            tool.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.script}" {name} "$@"\n', encoding="utf-8")
            tool.chmod(0o755)
            return
        local = self.bin / "node.exe"
        if not local.exists():
            try:
                os.link(node, local)
            except OSError:
                shutil.copy2(node, local)
        package = self.bin / "node_modules" / f"fake-{name}"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": f"fake-{name}", "version": "1.0.0",
                                                          "bin": {name: "cli.js"}}), encoding="utf-8")
        (package / "cli.js").write_text(NODE_FORWARDER.replace("TOOL", name), encoding="utf-8")
        (self.bin / f"{name}.cmd").write_text("@echo off\r\nexit /b 9\r\n", encoding="utf-8", newline="")

    def save(self) -> None:
        self.world_path.write_text(json.dumps(self.world), encoding="utf-8")

    def set(self, tool: str, **values) -> None:
        self.world = json.loads(self.world_path.read_text(encoding="utf-8"))
        self.world[tool].update(values)
        self.save()

    def argv(self) -> list[tuple[str, list[str]]]:
        if not self.log.exists():
            return []
        return [(item["tool"], item["argv"]) for item in map(json.loads, self.log.read_text("utf-8").splitlines())]

    def ran(self, tool: str, *args: str) -> bool:
        return (tool, list(args)) in self.argv()

    def allowed(self, argv: list[str]) -> bool:
        try:
            return bool(argv) and pathlib.Path(argv[0]).resolve().is_relative_to(self.bin.resolve())
        except OSError:
            return False



class FakeToolsCase(ToolUpdateTestCase):
    """Every test here: fake tools only, a temporary home, and a guard that fails on any other program."""

    def setUp(self):
        super().setUp()
        self.fake = FakeTools(self)
        self.enterContext(mock.patch.object(pathlib.Path, "home", side_effect=AssertionError("Path.home() was used")))
        self.addCleanup(self.check_launches)
        self.root = make_tmp_dir(self) / "project"
        self.state = self.root / ".git" / "oah"
        self.configure("after_finish = true\ntimeout_seconds = 30\n")
        self.lines: list[str] = []

    def allowed(self, argv: list[str]) -> bool:
        return hasattr(self, "fake") and self.fake.allowed(argv)

    def check_launches(self):
        for tool, argv in self.fake.argv():
            self.assertFalse(FORBIDDEN_WORDS & set(argv), (tool, argv))
            if tool == "codex":
                self.assertNotIn("plugin", argv, "the pass ran a Codex plugin command")
        for argv in self.launched:
            self.assertNotIn(pathlib.Path(argv[0]).stem.lower(), FORBIDDEN_PROGRAMS, argv)

    def configure(self, updates: str | None) -> None:
        config = self.root / ".oah" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[skills]\nauto_update = false\n" + (f"[updates]\n{updates}" if updates is not None else ""),
                          encoding="utf-8")

    def run_pass(self, *, trigger="manual", env=None, now=None):
        options = {"now": now} if now is not None else {}
        return passes.run_pass(self.root, self.state, env=env or self.fake.env, trigger=trigger,
                               out=self.lines.append, **options)

    def state_record(self) -> dict:
        return records.read_state()

    def components(self) -> dict:
        return self.state_record()["components"]


class SwitchTest(FakeToolsCase):
    def assert_nothing_happened(self, outcome):
        self.assertEqual(outcome, "skipped")
        self.assertEqual(self.launched, [])
        self.assertEqual(self.fake.argv(), [])
        self.assertFalse((self.fake.home / ".oah").exists())

    def test_off_by_default_without_a_config(self):
        shutil.rmtree(self.root / ".oah")
        self.assert_nothing_happened(self.run_pass())
        self.assertIn("OAH_UPDATE_SKIPPED", self.lines[0])

    def test_off_by_absence_of_the_key(self):
        self.configure(None)
        self.assert_nothing_happened(self.run_pass())
        self.assertFalse(records.read_settings(self.root).explicit)

    def test_off_when_said_so(self):
        self.configure("after_finish = false\n")
        self.assert_nothing_happened(self.run_pass())
        self.assertTrue(records.read_settings(self.root).explicit)

    def test_unreadable_switch_is_off(self):
        for text in ('after_finish = "yes"\n', "after_finish = true\ninterval_hours = 0\n",
                     "after_finish = true\ninterval_hours = nan\n", "after_finish = true\ntimeout_seconds = -1\n",
                     "after_finish = true\ntimeout_seconds = 100000\n", "after_finish = true\nunknown = 1\n",
                     "after_finish = true\n[broken\n"):
            with self.subTest(text=text):
                self.configure(text)
                self.lines.clear()
                self.assert_nothing_happened(self.run_pass())
                self.assertEqual(len(self.lines), 1)
                self.assertTrue(self.lines[0].startswith("OAH_UPDATE_SKIPPED: OAH_CONFIG"), self.lines[0])

    def test_worker_is_skipped_before_the_switch(self):
        env = dict(self.fake.env, OAH_WORKER="1")
        self.assert_nothing_happened(self.run_pass(env=env))

    def test_settings_defaults(self):
        self.configure("after_finish = true\n")
        settings = records.read_settings(self.root)
        self.assertEqual((settings.after_finish, settings.interval_hours, settings.timeout_seconds), (True, 24.0, 600.0))


class ParserTest(ToolUpdateTestCase):
    """The parsers, against the help and output the installed CLIs print."""

    def test_commands_listed_in_claude_and_codex_help(self):
        self.assertTrue({"update", "upgrade", "plugin", "plugins", "mcp"} <= probes.listed_commands(CLAUDE_HELP[""]))
        self.assertNotIn("installation.", probes.listed_commands(CLAUDE_HELP[""]))
        self.assertTrue({"list", "update", "marketplace", "i"} <= probes.listed_commands(CLAUDE_HELP["plugin"]))
        self.assertIn("update", probes.listed_commands(CLAUDE_HELP["plugin marketplace"]))
        self.assertTrue({"update", "plugin", "exec", "help"} <= probes.listed_commands(CODEX_HELP[""]))
        self.assertNotIn("working", probes.listed_commands(CODEX_HELP[""]))
        self.assertNotIn("Optional", probes.listed_commands(CODEX_HELP[""]))
        self.assertEqual(probes.listed_commands("Usage: x\n\nOptions:\n  -h, --help\n"), set())

    def test_flags_listed_in_help(self):
        self.assertTrue(probes.lists_flag(CLAUDE_HELP["plugin update"], "--json"))
        self.assertTrue(probes.lists_flag(CLAUDE_HELP["plugin update"], "--scope"))
        self.assertTrue(probes.lists_flag(CLAUDE_HELP["plugin list"], "--json"))
        self.assertFalse(probes.lists_flag(CLAUDE_HELP["plugin list"], "--scope"))
        wrapped_only = CLAUDE_HELP["plugin list"].replace("  --json                Output as JSON\n", "")
        self.assertIn("--json)", wrapped_only)  # still mentioned inside the --available description
        self.assertFalse(probes.lists_flag(wrapped_only, "--json"))
        self.assertTrue(probes.lists_flag("Options:\n      --json\n          Output upgrade result as JSON\n", "--json"))

    def test_every_phrase(self):
        cases = {
            "claude-current": ("Claude is up to date!\n", "Update available: 2.1.320\n"),
            "codex-current-homebrew": ("Warning: Not upgrading codex, the latest version is already installed\n",
                                       "==> Upgrading 1 outdated package\n"),
            "codex-current-npm": ("\nup to date, audited 3 packages in 1s\n", "changed 1 package in 2s\n"),
            "claude-marketplaces-current": ("All marketplaces are up to date.\n",
                                            "Successfully updated 1 marketplace(s)\n"),
            "claude-plugin-confirmation": ("Error: a marketplace-declared command must be confirmed\n",
                                           "Plugin x is already at the latest version\n"),
        }
        self.assertEqual(set(cases), set(probes.PHRASES))
        for name, (yes, no) in cases.items():
            with self.subTest(phrase=name):
                self.assertTrue(probes.said(name, yes))
                self.assertFalse(probes.said(name, no))

    def test_verdict_is_ac5(self):
        from oah.toolupdate import verdict

        rows = [  # before, after, said current, exit, timed out -> status
            ("1", "2", False, 0, False, "updated"),
            ("1", "2", False, 1, False, "updated"),
            ("1", "1", True, 0, False, "current"),
            ("1", "1", False, 0, False, "unverified"),
            ("1", None, True, 0, False, "unverified"),
            (None, "1", True, 0, False, "unverified"),
            (None, None, True, 0, False, "unverified"),
            ("1", "1", True, 1, False, "unverified"),
            ("1", None, False, 1, False, "unverified"),
            ("1", "2", True, None, True, "failed"),
        ]
        for before, after, said, code, timed_out, expected in rows:
            with self.subTest(before=before, after=after, said=said, code=code, timed_out=timed_out):
                old = verdict.confirmed(before) if before else verdict.unknown("unreadable before version")
                new = verdict.confirmed(after) if after else verdict.unknown("unreadable after version")
                ran = probes.Ran(code, "", "", timed_out)
                evidence = verdict.ComponentEvidence(
                    kind="cli", command="claude update", cli=verdict.confirmed("claude"),
                    help=verdict.confirmed(True), before=old, after=new,
                    update=probes.update_evidence(ran, old, new, said_current=said), attempted=True)
                self.assertEqual(verdict.decide(evidence)[0], expected)

    def test_every_evidence_combination_obeys_the_verdict_invariants(self):
        from oah.toolupdate import verdict

        observed = set()
        for kind in ("cli", "marketplace", "plugin", "plugins"):
            fields = {"cli": "/fake/cli", "help": True, "before": "1", "after": "2",
                      "update": verdict.UP_TO_DATE}
            if kind in ("plugin", "plugins"):
                fields["inventory"] = {"good@m": "2"}
            choices = []
            for name, value in fields.items():
                confirmed = [verdict.confirmed(value)]
                if name == "after":
                    confirmed.append(verdict.confirmed("1"))
                if name == "update" and kind in ("plugin", "plugins"):
                    confirmed.append(verdict.confirmed(verdict.CONFIRMATION))
                choices.append((*confirmed, verdict.absent("no Claude Code plugins installed") if name == "inventory" and kind == "plugins" else verdict.absent(), verdict.unknown(name),
                                verdict.unknown(name + " timed out", timed_out=True)))
            for combination in itertools.product(*choices):
                evidence = verdict.ComponentEvidence(kind=kind, command="claude update", attempted=True,
                                                       **dict(zip(fields, combination)))
                status, reason, next_step = verdict.decide(evidence)
                observed.add(status)
                unknowns = [item for item in combination if item.state == "unknown"]
                message = (kind, combination, status)
                if unknowns:
                    self.assertNotIn(status, ("updated", "current", "unsupported"), message)
                if any(item.timed_out for item in unknowns):
                    self.assertEqual(status, "failed", message)
                    self.assertIn("timed out; the tool may be partly updated", reason)
                    self.assertIn(evidence.command, next_step)
                if (kind == "plugins" and evidence.inventory.state == "absent" and
                        all(item.state == "confirmed" for name, item in zip(fields, combination) if name != "inventory")):
                    self.assertEqual(status, "unsupported", message)
                if status == "unsupported":
                    self.assertTrue(evidence.cli.state == "absent" or evidence.help.state == "absent" or
                                    (kind == "plugins" and evidence.inventory.state == "absent"), message)
                if status in ("updated", "current"):
                    self.assertTrue(all(item.state == "confirmed" for item in combination), message)
                self.assertIn(status, records.STATUSES)
        self.assertEqual(observed, set(records.STATUSES))
        self.assertEqual(observed, set(verdict.STATUSES))

    def test_resolver_miss_requires_readable_path_and_no_candidate(self):
        root = make_tmp_dir(self)
        folder = root / "bin"
        folder.mkdir()
        candidate = folder / "claude"
        candidate.write_text("not executable")
        candidate.chmod(0o644)
        def check(platform, path, pathext=".COM;.EXE;.BAT;.CMD"):
            with mock.patch.object(probes, "resolve_command", return_value=None), \
                 mock.patch.object(sys, "platform", platform):
                return probes.Cli("claude", "Claude Code", env={"PATH": str(path), "PATHEXT": pathext},
                                  timeout=1, cwd=root).resolution
        for case in ("posix_candidate", "windows_candidate", "clean_posix", "clean_windows",
                     "unreadable_posix", "unreadable_windows"):
            with self.subTest(case=case):
                candidate.unlink(missing_ok=True)
                windows_candidate = folder / "ClAuDe.cMd"
                windows_candidate.unlink(missing_ok=True)
                if case == "posix_candidate":
                    candidate.write_text("not executable")
                    candidate.chmod(0o644)
                if case == "windows_candidate":
                    windows_candidate.write_text("shim")
                platform = "win32" if "windows" in case else "darwin"
                if case.startswith("unreadable"):
                    with mock.patch.object(pathlib.Path, "iterdir", side_effect=PermissionError):
                        result = check(platform, folder)
                else:
                    result = check(platform, folder)
                self.assertEqual(result.state, "absent" if case.startswith("clean") else "unknown")
                if case.endswith("candidate"):
                    self.assertIn("found but cannot be run", result.reason)
                if case.startswith("unreadable"):
                    self.assertIn("PATH folder could not be read", result.reason)

    def test_confirmation_verdict_only_applies_to_plugin_components(self):
        from oah.toolupdate import verdict

        statuses = set()
        for kind in ("cli", "marketplace", "plugin", "plugins"):
            evidence = verdict.ComponentEvidence(kind=kind, command="claude plugin update a@m",
                cli=verdict.confirmed("claude"), help=verdict.confirmed(True),
                before=verdict.confirmed("1"), after=verdict.confirmed("1"),
                inventory=verdict.confirmed({"a@m": "1"}) if kind in ("plugin", "plugins") else None,
                update=verdict.confirmed(verdict.CONFIRMATION), attempted=True)
            status, _reason, next_step = verdict.decide(evidence)
            statuses.add(status)
            self.assertEqual(status, "needs-confirmation" if kind in ("plugin", "plugins") else "unverified")
            self.assertIn(evidence.command, next_step)
        self.assertEqual(statuses, {"needs-confirmation", "unverified"})

    def test_steps_and_run_cannot_choose_status_literals(self):
        for module in (steps, passes):
            tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
            literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                        and isinstance(node.value, str) and node.value in records.STATUSES]
            self.assertEqual(literals, [], module.__name__)

    def test_plugin_list_keeps_user_scope_plugins(self):
        listing = json.dumps([
            {"id": "superpowers@claude-plugins-official", "version": "5.1.0", "scope": "user", "enabled": True},
            {"id": "team@corp", "version": "1.0.0", "scope": "project", "enabled": True},
            {"id": "local@corp", "version": "1.0.0", "scope": "local", "enabled": True},
            {"id": "mine@skills-dir", "version": "1.0.0", "scope": "user", "enabled": True},
            {"id": "odd@m", "version": "unknown", "scope": "user", "enabled": True},
            {"id": "long@m", "version": "1" * 200, "scope": "user"},
        ])
        self.assertEqual(probes.user_plugins(listing),
                         {"superpowers@claude-plugins-official": "5.1.0", "odd@m": None, "long@m": None})
        self.assertIsNone(probes.user_plugins(json.dumps(json.loads(listing) + [
            {"id": "-x@m", "version": "1.0.0", "scope": "user"}])))
        self.assertEqual(probes.user_plugins(json.dumps({"installed": [], "available": []})), {})
        self.assertIsNone(probes.user_plugins("Installed plugins:\n"))

    def test_mixed_valid_and_malformed_plugin_list_is_unreadable(self):
        good = {"id": "good@m", "version": "1", "scope": "user"}
        for bad in ({"scope": "user", "version": "1"}, {"id": "-x@m", "scope": "user"},
                    {"id": "good@m", "scope": "user", "version": "2"},
                    {"id": "other@m", "scope": "mystery"}, "broken", {}):
            with self.subTest(bad=bad):
                self.assertIsNone(probes.user_plugins(json.dumps([good, bad])))

    def test_plugin_update_result_line(self):
        ok = {"command": "update", "outcome": "ok", "updateOutcome": "up_to_date", "nonce": "n"}
        text = "Checking for updates\n" + json.dumps(ok) + "\n"
        self.assertEqual(probes.plugin_result(text), ok)
        self.assertTrue(probes.plugin_said_current(ok))
        self.assertFalse(probes.plugin_said_current(dict(ok, refreshFailed=True)))
        self.assertFalse(probes.plugin_said_current(dict(ok, updateOutcome="updated")))
        refused = {"command": "update", "outcome": "failed", "failureCode": "command_source_refused"}
        shown = {"command": "update", "outcome": "failed", "failureCode": "op_failed", "shownCommand": {"sha256": "a"}}
        self.assertTrue(probes.plugin_needs_confirmation(refused, ""))
        self.assertTrue(probes.plugin_needs_confirmation(shown, ""))
        self.assertFalse(probes.plugin_needs_confirmation({"command": "update", "outcome": "failed",
                                                           "failureCode": "not_installed"}, ""))
        self.assertIsNone(probes.plugin_result("not json\n"))

    def test_unclean_output_supplies_nothing(self):
        ran = probes.Ran(0, "2.1.3�16 (Claude Code)\nClaude is up to date!\n", "", False)
        self.assertFalse(ran.clean)
        self.assertFalse(ran.ok)
        self.assertEqual(ran.text, "")


class PassTest(FakeToolsCase):
    def test_empty_inventory_is_absent_without_an_update_attempt(self):
        from oah.toolupdate import verdict

        self.fake.set("claude", plugins=[])
        seen = []
        decide = verdict.decide
        def capture(evidence):
            if evidence.kind == "plugins":
                seen.append(evidence)
            return decide(evidence)
        with mock.patch.object(verdict, "decide", side_effect=capture):
            self.run_pass()
        entry = self.components()["claude-plugins"]
        self.assertEqual((entry["status"], entry["settled_version"]), ("unsupported", None))
        self.assertIn("no Claude Code plugins installed", entry["next_step"])
        self.assertEqual(seen[0].inventory.state, "absent")
        self.assertFalse(seen[0].attempted)
        self.assertIsNone(seen[0].update)
        self.assertFalse(any(argv[:2] == ["plugin", "update"] and "--help" not in argv for name, argv in self.fake.argv()
                             if name == "claude"))

    def test_plugin_confirmation_keeps_a_partly_changed_group_unsettled(self):
        self.fake.set("claude", plugins=[{"id": ident, "scope": "user", "version": "1"}
                                         for ident in ("fresh@m", "risky@m")], plugin_update={
            "fresh@m": {"set_version": "2"},
            "risky@m": {"exit": 1, "json": {"command": "update", "outcome": "failed",
                                             "failureCode": "command_source_refused"}}})
        self.assertEqual(self.run_pass(), "needs-attention")
        group = self.components()["claude-plugins"]
        self.assertNotIn(group["status"], records.SETTLED)
        self.assertIsNone(group["settled_version"])
        self.assertIn("claude plugin update risky@m", group["next_step"])

    def test_plugin_update_timeout_fails_the_group_with_the_update_command(self):
        self.fake.set("claude", plugins=[{"id": "slow@m", "scope": "user", "version": "1"}])
        run = probes.Cli.run

        def timeout(cli, *args):
            if args == ("plugin", "update", "slow@m", "--scope", "user", "--json"):
                return probes.Ran(None, "", "", True)
            return run(cli, *args)

        with mock.patch.object(probes.Cli, "run", timeout):
            self.assertEqual(self.run_pass(), "needs-attention")
        for name in ("claude-plugin:slow@m", "claude-plugins"):
            entry = self.components()[name]
            self.assertEqual((entry["status"], entry["settled_version"]), ("failed", None))
            self.assertIn("timed out; the tool may be partly updated", entry["next_step"])
            self.assertIn("claude plugin update slow@m", entry["next_step"])

    def test_post_update_inventory_errors_cannot_settle_members_or_group(self):
        good = {"id": "good@m", "scope": "user", "version": "1"}
        self.fake.set("claude", plugins=[good], plugin_update={"good@m": {"json": {
            "command": "update", "outcome": "ok", "updateOutcome": "up_to_date"}}})
        run = probes.Cli.run
        for problem, expected in ((probes.Ran(0, json.dumps([good, {"scope": "user"}]), "", False), "unverified"),
                                  (probes.Ran(None, "", "", True), "failed")):
            with self.subTest(problem=problem):
                calls = 0

                def unreadable(cli, *args):
                    nonlocal calls
                    if args == ("plugin", "list", "--json"):
                        calls += 1
                        if calls == 2:
                            return problem
                    return run(cli, *args)

                with mock.patch.object(probes.Cli, "run", unreadable):
                    self.assertEqual(self.run_pass(), "needs-attention")
                for name in ("claude-plugin:good@m", "claude-plugins"):
                    entry = self.components()[name]
                    self.assertEqual((entry["status"], entry["settled_version"]), (expected, None))

    def test_malformed_user_inventory_never_settles_the_plugin_group(self):
        good = {"id": "good@m", "scope": "user", "version": "1"}
        self.fake.set("claude", plugins=[good, {"scope": "user", "version": "1"}],
                      plugin_update={"good@m": {"json": {"command": "update", "outcome": "ok",
                                                         "updateOutcome": "up_to_date"}}})
        self.assertEqual(self.run_pass(), "needs-attention")
        entry = self.components()["claude-plugins"]
        self.assertEqual((entry["status"], entry["settled_version"]), ("unverified", None))
        self.assertIn("inventory", entry["next_step"])

    def test_unrunnable_installed_cli_and_dependents_are_unverified(self):
        resolve = probes.resolve_command
        for tool, names in (("claude", ("claude", "claude-marketplaces", "claude-plugins")),
                            ("codex", ("codex",))):
            with self.subTest(tool=tool):
                def shim(name, search_path):
                    if name == tool:
                        raise probes.UnsupportedBatchShim(str(self.fake.bin / (name + ".cmd")), "no native entry")
                    return resolve(name, search_path)

                self.fake.log.unlink(missing_ok=True)
                with mock.patch.object(probes, "resolve_command", side_effect=shim):
                    self.assertEqual(self.run_pass(), "needs-attention")
                for name in names:
                    entry = self.components()[name]
                    self.assertEqual((entry["status"], entry["settled_version"]), ("unverified", None))
                    self.assertIn("batch file", entry["next_step"])
                    self.assertIn("native executable", entry["next_step"])
                self.assertFalse(any(name == tool for name, _argv in self.fake.argv()))

    def test_version_timeouts_before_and_after_are_failed_with_the_command(self):
        run = probes.Cli.run
        for tool in ("claude", "codex"):
            for phase in ("before", "after"):
                with self.subTest(tool=tool, phase=phase):
                    calls = 0

                    def version_timeout(cli, *args):
                        nonlocal calls
                        if cli.name == tool and args == ("--version",):
                            calls += 1
                            if calls == (1 if phase == "before" else 2):
                                return probes.Ran(None, "", "", True)
                        return run(cli, *args)

                    with mock.patch.object(probes.Cli, "run", version_timeout):
                        self.assertEqual(self.run_pass(), "needs-attention")
                    entry = self.components()[tool]
                    self.assertEqual((entry["status"], entry["settled_version"]), ("failed", None))
                    self.assertIn("version check timed out" if phase == "before" else
                                  "timed out; the tool may be partly updated", entry["next_step"])
                    self.assertIn(f"{tool} update", entry["next_step"])

    def test_marketplace_success_without_evidence_is_unverified(self):
        self.fake.set("claude", marketplace_update={"stdout": "arbitrary output\n"})
        self.assertEqual(self.run_pass(), "needs-attention")
        entry = self.components()["claude-marketplaces"]
        self.assertEqual((entry["status"], entry["settled_version"]), ("unverified", None))
        self.assertIn("claude plugin marketplace update", entry["next_step"])

    def test_skipping_any_fixed_component_requires_attention(self):
        fixed = ("claude", "codex", "claude-marketplaces", "claude-plugins")
        stamp = "2026-11-03T00:00:00Z"
        for skipped in fixed:
            with self.subTest(skipped=skipped):
                found = {name: records.component("current", "1", "1", "", stamp)
                         for name in fixed if name != skipped}
                with mock.patch.object(steps, "cli_update", return_value={}), \
                        mock.patch.object(steps, "claude_plugins", return_value=found), \
                        mock.patch.object(steps, "claude_marketplaces", return_value={}):
                    self.assertEqual(self.run_pass(), "needs-attention")
                self.assertEqual(self.components()[skipped]["status"], "unverified")

    def test_codex_plugins_are_only_a_notice_and_never_run_commands(self):
        config = self.fake.codex_home / "config.toml"
        notice = ("Codex plugins are not updated automatically: update them with codex plugin marketplace "
                  "upgrade, then approve their hooks again in /hooks")
        for text in (toml_config({"m": "abc"}, ("hooked@m",)), "[plugins\n", "plugins = []\n"):
            with self.subTest(config=text):
                config.write_text(text, encoding="utf-8")
                before = config.read_bytes()
                self.lines.clear()
                self.assertEqual(self.run_pass(), "complete")
                self.assertIn(notice, "\n".join(self.lines))
                self.assertIn(notice, "\n".join(passes.status_lines()))
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse(any(tool == "codex" and "plugin" in argv for tool, argv in self.fake.argv()))
                self.assertFalse(any(name.startswith("codex-") for name in self.components()))

    def test_codex_plugin_notice_is_read_only_even_without_pass_records(self):
        config = self.fake.codex_home / "config.toml"
        for text, expected in ((toml_config({}, ("a@m",)), True), ("[plugins\n", True), ("[plugins]\n", False)):
            with self.subTest(config=text):
                config.write_text(text, encoding="utf-8")
                lines = "\n".join(passes.status_lines())
                self.assertEqual(steps.CODEX_PLUGINS_NOTE in lines, expected)
                self.assertEqual(config.read_text("utf-8"), text)
                self.assertEqual(self.launched, [])
                self.assertFalse(records.updates_dir().exists())
        config.unlink()
        self.assertIn(steps.CODEX_PLUGINS_NOTE, "\n".join(passes.status_lines()))
        self.assertEqual(self.launched, [])
        self.assertFalse(records.updates_dir().exists())

    def test_marketplace_revision_change_is_updated_and_unreadable_revision_cannot_settle(self):
        self.fake.set("claude", marketplace_update={"stdout": "Successfully updated 1 marketplace(s)\n",
                                                   "set_revision": "b" * 40})
        self.assertEqual(self.run_pass(), "complete")
        entry = self.components()["claude-marketplaces"]
        self.assertEqual(entry["status"], "updated")
        self.assertNotEqual(entry["before"], entry["after"])
        self.assertIn(steps.RESTART_NOTE, entry["next_step"])
        metadata = self.fake.home / ".claude" / "plugins" / "known_marketplaces.json"
        metadata.write_text("{torn", encoding="utf-8")
        self.fake.set("claude", marketplace_update={"stdout": "All marketplaces are up to date.\n"})
        self.assertEqual(self.run_pass(), "needs-attention")
        entry = self.components()["claude-marketplaces"]
        self.assertEqual((entry["status"], entry["settled_version"]), ("unverified", None))

    def test_everything_current(self):
        self.assertEqual(self.run_pass(env=dict(self.fake.env, PYTHONIOENCODING="cp437")), "complete", self.lines)
        components = self.components()
        self.assertEqual({name: entry["status"] for name, entry in components.items()},
                         {"claude": "current", "codex": "current", "claude-marketplaces": "current",
                          "claude-plugins": "unsupported"})
        self.assertEqual(components["claude"]["settled_version"], "2.1.316")
        self.assertTrue(self.fake.ran("claude", "update"))
        self.assertTrue(self.fake.ran("codex", "update"))
        self.assertTrue(self.lines[-1].startswith("OAH_UPDATE_FINISHED: outcome complete"), self.lines)
        self.assertEqual({item["cwd"] for item in map(json.loads, self.fake.log.read_text("utf-8").splitlines())},
                         {str(records.updates_dir())})

    def test_version_up_is_updated_with_the_restart_note(self):
        self.fake.set("claude", update={"stdout": "Successfully updated from 2.1.316 to version 2.1.320\n",
                                        "set_version": "2.1.320"})
        self.assertEqual(self.run_pass(), "complete")
        entry = self.components()["claude"]
        self.assertEqual((entry["status"], entry["before"], entry["after"], entry["settled_version"]),
                         ("updated", "2.1.316", "2.1.320", "2.1.320"))
        self.assertEqual(entry["next_step"], steps.RESTART_NOTE)
        self.assertIn(f"oah: Claude Code 2.1.316 -> 2.1.320 (updated); {steps.RESTART_NOTE}", self.lines)
        self.assertEqual(self.state_record()["models_refresh"], ["claude"])
        self.assertIn("oah: model lists refresh at the next oah call (Claude Code changed)", self.lines)

    def test_exit_zero_same_version_and_no_phrase_is_unverified(self):
        self.fake.set("claude", update={"stdout": "Checking for updates...\n"})
        self.assertEqual(self.run_pass(), "needs-attention")
        entry = self.components()["claude"]
        self.assertEqual((entry["status"], entry["settled_version"]), ("unverified", None))
        self.assertIn("run claude update yourself", entry["next_step"])
        self.assertTrue(self.lines[-1].startswith("OAH_UPDATE_FINISHED: outcome needs-attention"), self.lines)

    def test_unreadable_version_after_is_unverified(self):
        self.fake.set("claude", update={"stdout": CLAUDE_CURRENT, "set_version": None})
        self.run_pass()
        self.assertEqual(self.components()["claude"]["status"], "unverified")

    def test_version_that_is_not_strict_utf8_is_unreadable(self):
        self.fake.set("claude", version_hex=b"2.1.3\xff16 (Claude Code)\n".hex())
        self.run_pass()
        entry = self.components()["claude"]
        self.assertEqual((entry["status"], entry["before"]), ("unverified", None))

    def test_non_zero_exit_with_a_readable_changed_version_is_updated(self):
        # The version read afterwards is what is installed; AC5 decides on it whatever the exit code.
        self.fake.set("codex", update={"stderr": "post-install step failed\n", "exit": 1, "set_version": "0.167.0"})
        self.run_pass()
        self.assertEqual(self.components()["codex"]["status"], "updated")

    def test_non_zero_exit_with_the_same_version_is_unverified(self):
        self.fake.set("codex", update={"stderr": "Failed\n", "exit": 1})
        self.run_pass()
        entry = self.components()["codex"]
        self.assertEqual(entry["status"], "unverified")
        self.assertIn("run codex update yourself", entry["next_step"])

    def test_subcommand_missing_from_help_is_unsupported_and_not_run(self):
        self.fake.set("codex", help=dict(CODEX_HELP, **{"": CODEX_HELP[""].replace(
            "  update            Update Codex to the latest version\n", "")}))
        self.assertEqual(self.run_pass(), "complete")
        self.assertEqual(self.components()["codex"]["status"], "unsupported")
        self.assertFalse(self.fake.ran("codex", "update"))

    def test_missing_cli_is_unsupported_with_the_install_hint(self):
        for name in ("codex", "codex.cmd"):
            (self.fake.bin / name).unlink(missing_ok=True)
        self.assertEqual(self.run_pass(), "complete")
        entry = self.components()["codex"]
        self.assertEqual(entry["status"], "unsupported")
        self.assertIn("oah doctor", entry["next_step"])
        self.assertNotIn("codex-marketplaces", self.components())

    def test_timeout_is_failed_and_the_pass_continues(self):
        self.configure("after_finish = true\ntimeout_seconds = 1\n")
        self.fake.set("claude", update={"sleep": 30})
        self.assertEqual(self.run_pass(), "needs-attention")
        entry = self.components()["claude"]
        self.assertEqual((entry["status"], entry["settled_version"]), ("failed", None))
        self.assertIn("timed out; the tool may be partly updated", entry["next_step"])
        self.assertIn("claude update", entry["next_step"])
        self.assertTrue(any("timed out; the tool may be partly updated" in line for line in self.lines))
        self.assertEqual(self.components()["codex"]["status"], "current")
        self.assertTrue(self.fake.ran("codex", "update"))

    def test_undecodable_update_streams_never_settle_changed_versions(self):
        for stream in ("stdout_hex", "stderr_hex"):
            for tool in ("claude", "codex"):
                with self.subTest(stream=stream, tool=tool):
                    old = "2.1.316" if tool == "claude" else "0.166.0"
                    new = "2.1.320" if tool == "claude" else "0.167.0"
                    self.fake.set(tool, version=old, update={stream: "ff", "set_version": new})
                    self.run_pass()
                    entry = self.components()[tool]
                    self.assertEqual((entry["status"], entry["before"], entry["after"], entry["settled_version"]),
                                     ("unverified", old, new, None))

    def test_undecodable_plugin_update_streams_never_settle(self):
        for stream in ("stdout_hex", "stderr_hex"):
            with self.subTest(stream=stream):
                self.fake.set("claude", plugins=[{"id": "a@m", "version": "1", "scope": "user"}],
                              marketplace_update={stream: "ff", "exit": 1},
                              plugin_update={"a@m": {stream: "ff", "set_version": "2"}})
                self.assertEqual(self.run_pass(), "needs-attention")
                for name in ("claude-marketplaces", "claude-plugin:a@m", "claude-plugins"):
                    with self.subTest(component=name):
                        entry = self.components()[name]
                        self.assertEqual((entry["status"], entry["settled_version"]), ("unverified", None))

    def test_plugins_at_user_scope_are_updated_and_checked(self):
        self.fake.set("claude", plugins=[
            {"id": "fresh@m", "version": "1.0.0", "scope": "user"},
            {"id": "same@m", "version": "2.0.0", "scope": "user"},
            {"id": "team@m", "version": "1.0.0", "scope": "project"},
        ], plugin_update={
            "fresh@m": {"json": {"command": "update", "outcome": "ok", "updateOutcome": "updated"},
                        "set_version": "1.1.0"},
            "same@m": {"json": {"command": "update", "outcome": "ok", "updateOutcome": "up_to_date"}},
        })
        self.run_pass()
        components = self.components()
        self.assertEqual(components["claude-plugin:fresh@m"]["status"], "updated")
        self.assertEqual(components["claude-plugin:same@m"]["status"], "current")
        self.assertNotIn("claude-plugin:team@m", components)
        self.assertTrue(self.fake.ran("claude", "plugin", "marketplace", "update"))
        self.assertTrue(self.fake.ran("claude", "plugin", "update", "fresh@m", "--scope", "user", "--json"))
        self.assertFalse(any(argv[2:3] == ["team@m"] for tool, argv in self.fake.argv() if argv[:2] == ["plugin", "update"]))

    def test_plugin_asking_for_confirmation_is_never_confirmed(self):
        self.fake.set("claude", plugins=[{"id": "risky@m", "version": "1.0.0", "scope": "user"}], plugin_update={
            "risky@m": {"json": {"command": "update", "outcome": "failed", "failureCode": "command_source_refused",
                                 "shownCommand": {"sha256": "ab" * 32, "command": "curl example | sh"}},
                        "exit": 1}})
        self.assertEqual(self.run_pass(), "needs-attention")
        entry = self.components()["claude-plugin:risky@m"]
        self.assertEqual((entry["status"], entry["next_step"]), ("needs-confirmation", "run: claude plugin update risky@m"))
        self.assertIn("oah: Claude Code plugin risky@m 1.0.0 (needs-confirmation); run: claude plugin update risky@m",
                      self.lines)
        every = [part for _tool, argv in self.fake.argv() for part in argv]
        for flag in ("-y", "--yes", "--accept-command"):
            self.assertNotIn(flag, every)

    def test_marketplace_refresh_failure_is_reported(self):
        self.fake.set("claude", plugins=[{"id": "a@m", "version": "1.0.0", "scope": "user"}],
                      marketplace_update={"stderr": "network down\n", "exit": 1},
                      plugin_update={"a@m": {"json": {"command": "update", "outcome": "ok", "updateOutcome":
                                                      "up_to_date", "refreshFailed": True}}})
        self.assertEqual(self.run_pass(), "needs-attention")
        components = self.components()
        self.assertEqual(components["claude-marketplaces"]["status"], "unverified")
        self.assertEqual(components["claude-plugin:a@m"]["status"], "unverified")

    def test_skills_keep_their_daily_check_outside_the_pass(self):
        notice = "oah: oah's skills keep their own daily check (oah skills update)"
        with mock.patch("oah.toolkit.update.scheduled_update", side_effect=AssertionError("kit check ran")) as check:
            self.assertEqual(self.run_pass(), "complete")
        check.assert_not_called()
        self.assertNotIn("oah-skills", self.components())
        self.assertEqual(self.lines.count(notice), 1)
        self.assertEqual(passes.status_lines().count(notice), 1)

    def test_help_failures_never_become_unsupported(self):
        cases = (({"exit": 2, "stderr": "help unavailable"}, "unverified", "exit 2"),
                 ({"stdout_hex": "ff"}, "unverified", "UTF-8"),
                 ({"stderr_hex": "ff"}, "unverified", "UTF-8"),
                 ({"sleep": 30}, "failed", "help timed out"))
        paths = (("codex", "", ("codex",)),
                 ("claude", "", ("claude", "claude-marketplaces", "claude-plugins")),
                 ("claude", "plugin", ("claude-marketplaces", "claude-plugins")),
                 ("claude", "plugin marketplace", ("claude-marketplaces",)),
                 ("claude", "plugin list", ("claude-plugins",)),
                 ("claude", "plugin update", ("claude-plugins",)))
        self.configure("after_finish = true\ntimeout_seconds = 1\n")
        for tool, path, names in paths:
            for spec, status, reason in cases:
                with self.subTest(tool=tool, path=path, spec=spec):
                    self.fake.set("claude", help=dict(CLAUDE_HELP))
                    self.fake.set("codex", help=dict(CODEX_HELP))
                    helps = dict(CLAUDE_HELP if tool == "claude" else CODEX_HELP)
                    helps[path] = spec
                    self.fake.set(tool, help=helps)
                    self.fake.log.unlink(missing_ok=True)
                    self.assertEqual(self.run_pass(), "needs-attention")
                    for name in names:
                        entry = self.components()[name]
                        self.assertEqual((entry["status"], entry["settled_version"]), (status, None))
                        self.assertIn(reason, entry["next_step"])
                        self.assertIn("--help", entry["next_step"])
                        self.assertNotIn("partly updated", entry["next_step"])
                    if tool == "codex":
                        self.assertFalse(self.fake.ran(tool, "update"))
                    else:
                        if "claude-marketplaces" in names:
                            self.assertFalse(self.fake.ran(tool, "plugin", "marketplace", "update"))
                        if "claude-plugins" in names:
                            self.assertFalse(self.fake.ran(tool, "plugin", "list", "--json"))
                            self.assertFalse(any(argv[:2] == ["plugin", "update"] and argv[-1:] != ["--help"]
                                                 for name, argv in self.fake.argv() if name == tool))

    def test_clean_help_without_plugin_capabilities_is_unsupported(self):
        self.fake.set("claude", plugins=[{"id": "a@m", "version": "1", "scope": "user"}],
                      plugin_update={"a@m": {"json": {"command": "update", "outcome": "ok",
                                                      "updateOutcome": "up_to_date"}}})
        for path, line, names in (
                ("plugin", "  marketplace                          Manage Claude Code marketplaces\n",
                 ("claude-marketplaces",)),
                ("plugin marketplace", "  update [options] [name]     Update marketplace(s) from their source - updates\n",
                 ("claude-marketplaces",)),
                ("plugin update", "  --json                     Print one machine-readable result line on stdout\n",
                 ("claude-plugins",))):
            with self.subTest(path=path):
                helps = dict(CLAUDE_HELP)
                helps[path] = helps[path].replace(line, "")
                self.fake.set("claude", help=helps)
                self.fake.log.unlink(missing_ok=True)
                self.assertEqual(self.run_pass(), "complete")
                for name in names:
                    self.assertEqual(self.components()[name]["status"], "unsupported")

    def test_help_launch_error_is_unverified(self):
        run = probes.Cli.run

        def denied(cli, *args):
            if cli.name == "codex" and args == ("--help",):
                raise PermissionError("help denied")
            return run(cli, *args)

        with mock.patch.object(probes.Cli, "run", denied):
            self.assertEqual(self.run_pass(), "needs-attention")
        entry = self.components()["codex"]
        self.assertEqual((entry["status"], entry["settled_version"]), ("unverified", None))
        self.assertIn("PermissionError", entry["next_step"])
        self.assertIn("codex --help", entry["next_step"])
        self.assertFalse(self.fake.ran("codex", "update"))

    def test_full_pass_runs_only_the_listed_commands(self):
        (self.fake.codex_home / "config.toml").write_text(toml_config({"m": "a" * 40}), encoding="utf-8")
        self.fake.set("claude", plugins=[{"id": "a@m", "version": "1.0.0", "scope": "user"}])
        self.run_pass()
        commands = {tuple(argv) for _tool, argv in self.fake.argv()
                    if argv[-1:] not in (["--help"], ["--version"])}
        self.assertEqual(commands, {("update",), ("plugin", "list", "--json"), ("plugin", "marketplace", "update"),
                                    ("plugin", "update", "a@m", "--scope", "user", "--json")})


class SessionsTest(FakeToolsCase):
    """AC10: only the runner's own command may be stopped; updated results and status carry the restart note."""

    def test_full_pass_never_lists_or_signals_unrelated_processes(self):
        from oah.engines import proc

        self.fake.set("claude", update={"stdout": "Successfully updated\n", "set_version": "2.1.320"})
        owned = set()
        kill_tree = proc.kill_tree

        def stop_owned(command):
            self.assertIn(command.args, self.launched)
            self.assertTrue(self.fake.allowed(command.args))
            owned.add(command.pid)
            kill_tree(command)

        def checked_signal(real):
            def send(pid, signum):
                self.assertIn(pid, owned, "a process outside the started command was signalled")
                return real(pid, signum)
            return send

        self.enterContext(mock.patch.object(proc, "kill_tree", stop_owned))
        for name in ("kill", "killpg"):
            if hasattr(os, name):
                self.enterContext(mock.patch.object(os, name, side_effect=checked_signal(getattr(os, name))))
        for name in ("signal", "pthread_kill", "raise_signal"):
            if hasattr(signal, name):
                self.enterContext(mock.patch.object(signal, name, side_effect=AssertionError(name)))
        self.run_pass()
        self.assertTrue(all(self.fake.allowed(argv) for argv in self.launched))
        self.assertIn(f"oah: Claude Code 2.1.316 -> 2.1.320 (updated); {steps.RESTART_NOTE}", self.lines)
        status = passes.status_lines()
        self.assertIn(f"oah: {steps.RESTART_NOTE}", status)
        self.assertTrue(any(line.endswith(steps.RESTART_NOTE) and "(updated)" in line for line in status))
        self.configure("after_finish = true\ntimeout_seconds = 1\n")
        self.fake.set("claude", update={"sleep": 30})
        self.assertEqual(self.run_pass(), "needs-attention")
        self.assertEqual(len(owned), 1)
        self.assertIn("timed out; the tool may be partly updated", self.components()["claude"]["next_step"])

    def test_windows_cleanup_terminates_and_closes_only_the_started_job(self):
        from oah.engines import proc
        from oah.toolupdate import verdict

        cli = probes.Cli("claude", "Claude Code", env=self.fake.env, timeout=0.1, cwd=self.root)
        context = steps.Context(self.fake.env, records.stamp(records.utc_now()), cli, cli)
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                command = mock.Mock(returncode=0)
                command.communicate.side_effect = ([subprocess.TimeoutExpired(cli.base, 0.1), (b"", b"")]
                                                   if timed_out else [(b"updated", b"")])
                job = mock.Mock()
                job.start.side_effect = lambda child: setattr(child, "_oah_job", job)
                windows = mock.Mock()
                windows.name = "nt"
                with mock.patch.object(proc, "os", windows), mock.patch.object(proc, "_WindowsJob", return_value=job), \
                        mock.patch.object(proc.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                        mock.patch.object(proc.subprocess, "Popen", return_value=command) as launch, \
                        mock.patch.object(cli, "version", side_effect=[
                            verdict.confirmed("1"), verdict.confirmed("2")]), \
                        mock.patch.object(cli, "lists", return_value=verdict.confirmed(True)):
                    entry = steps.cli_update(context, cli)["claude"]
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[0], [*cli.base, "update"])
                self.assertEqual(launch.call_args.kwargs["creationflags"], 0x204)
                job.start.assert_called_once_with(command)
                job.close.assert_called_once_with()
                command.kill.assert_not_called()
                if timed_out:
                    job.terminate.assert_called_once_with()
                    self.assertEqual((entry["status"], entry["settled_version"]), ("failed", None))
                    self.assertIn("timed out; the tool may be partly updated", entry["next_step"])
                    self.assertIn("claude update", entry["next_step"])
                else:
                    job.terminate.assert_not_called()
                    self.assertEqual(entry["status"], "updated")


class RecordsTest(FakeToolsCase):
    def test_removed_kit_component_invalidates_state_and_history(self):
        self.run_pass()
        state = self.state_record()
        state["components"]["oah-skills"] = records.component("current", "abc", "abc", "", state["last_started"])
        self.write(records.state_path(), json.dumps(state))
        with self.assertRaisesRegex(records.StateUnavailable, "OAH_UPDATE_STATE_UNAVAILABLE"):
            records.read_state()
        run = dict(self.a_valid_run(), components=state["components"])
        self.write(records.runs_path(), json.dumps(run) + "\n")
        problems = []
        self.assertEqual(records.read_runs(report=problems.append), [])
        self.assertIn("skipped 1", "\n".join(problems))

    def test_history_changes_no_interval_or_settled_decision(self):
        self.run_pass()
        state = self.state_record()
        good = records.runs_path().read_bytes()
        for case in ("missing", "corrupt", "unreadable"):
            with self.subTest(case=case):
                records.runs_path().write_bytes(good)
                if case == "missing":
                    records.runs_path().unlink()
                elif case == "corrupt":
                    records.runs_path().write_bytes(b"{torn\n\xff\n")
                real_read = pathlib.Path.read_bytes

                def no_history_decision(path):
                    if path == records.runs_path():
                        raise AssertionError("a decision read history")
                    return real_read(path)

                self.fake.log.unlink(missing_ok=True)
                self.lines.clear()
                with mock.patch.object(pathlib.Path, "read_bytes", no_history_decision), \
                        mock.patch.object(passes, "_popen") as launch:
                    self.assertEqual(records.read_state(), state)
                    self.assertEqual(self.run_pass(trigger="after-finish"), "skipped")
                    self.assertFalse(passes.start_after_finish(self.root, self.state, self.fake.env,
                                                              out=self.lines.append))
                    launch.assert_not_called()
                self.assertEqual(self.fake.argv(), [])
                self.assertNotIn("UNAVAILABLE", "\n".join(self.lines))

    def test_missing_state_is_due_regardless_of_history(self):
        good = json.dumps(self.a_valid_run()) + "\n"
        for text in (None, good, "{torn\n"):
            with self.subTest(history=text):
                shutil.rmtree(records.updates_dir(), ignore_errors=True)
                if text is not None:
                    self.write(records.runs_path(), text)
                self.assertIsNone(records.read_state())
                self.assertTrue(records.due(records.read_state(), records.read_settings(self.root), records.utc_now()))
                self.fake.log.unlink(missing_ok=True)
                self.lines.clear()
                self.assertEqual(self.run_pass(trigger="after-finish"), "complete")
                self.assertTrue(self.fake.ran("claude", "update") and self.fake.ran("codex", "update"))
                self.assertNotIn("OAH_UPDATE_STATE_UNAVAILABLE", "\n".join(self.lines))

    def test_status_shows_newest_history_and_counts_bad_lines(self):
        self.run_pass()
        old = self.a_valid_run()
        newest = dict(self.a_valid_run(), finished="2026-11-02T00:01:00Z")
        self.write(records.runs_path(), json.dumps(old) + "\n{torn\n" + json.dumps(newest) + "\n")
        launched = len(self.launched)
        text = "\n".join(passes.status_lines())
        self.assertIn("skipped 1", text)
        self.assertIn(old["finished"], text)
        self.assertIn(newest["finished"], text)
        self.assertLess(text.index(newest["finished"]), text.index(old["finished"]))
        self.assertEqual(len(self.launched), launched)

    def test_lone_surrogates_in_timestamp_separators_are_rejected(self):
        good = self.a_valid_run()
        surrogate_time = "2026-11-01\ud80000:00:00Z"
        for field in ("started", "finished", "checked_at"):
            with self.subTest(field=field):
                bad = json.loads(json.dumps(good))
                if field == "checked_at":
                    bad["components"]["claude"][field] = surrogate_time
                else:
                    bad[field] = surrogate_time
                state = {"version": 1, "last_started": bad["started"], "last_finished": bad["finished"],
                         "trigger": bad["trigger"], "outcome": bad["outcome"], "components": bad["components"],
                         "models_refresh": []}
                self.write(records.state_path(), json.dumps(state))
                with self.assertRaises(records.StateUnavailable):
                    records.read_state()
                self.write(records.runs_path(), json.dumps(bad) + "\n" + json.dumps(good) + "\n")
                problems = []
                self.assertEqual(records.read_runs(report=problems.append), [good])
                self.assertIn("skipped 1", "\n".join(problems))

    def test_removed_hook_status_and_approval_digest_are_invalid_records(self):
        self.run_pass()
        state = self.state_record()
        for entry in (records.component("needs-hook-approval", "1", "2", "/hooks", state["last_started"]),
                      dict(state["components"]["codex"], hook_approval_digest="absent")):
            with self.subTest(fields=sorted(entry)):
                bad = dict(state, components=dict(state["components"], codex=entry))
                bad["outcome"] = records.outcome(bad["components"])
                self.write(records.state_path(), json.dumps(bad))
                with self.assertRaises(records.StateUnavailable):
                    records.read_state()
                run = dict(self.a_valid_run(bad["outcome"]), components=bad["components"])
                self.write(records.runs_path(), json.dumps(run) + "\n")
                problems = []
                self.assertEqual(records.read_runs(report=problems.append), [])
                self.assertIn("skipped 1", "\n".join(problems))

    def test_lone_surrogate_history_line_is_skipped_and_later_pass_survives(self):
        good = self.a_valid_run("needs-attention")
        good["components"]["codex"] = records.component("unverified", None, None, "retry", good["started"])
        bad = json.loads(json.dumps(good))
        bad["components"]["codex"]["next_step"] = "\ud800"
        self.write(records.runs_path(), json.dumps(bad) + "\n" + json.dumps(good) + "\n")
        problems = []
        self.assertEqual(records.read_runs(report=problems.append), [good])
        self.assertIn("skipped 1", "\n".join(problems))
        self.assertEqual(self.run_pass(), "complete")
        self.assertEqual(len(records.read_runs()), 2)

    def test_lone_surrogates_are_rejected_in_state_and_skipped_in_history(self):
        self.run_pass()
        good_state = self.state_record()
        good_run = records.read_runs()[0]
        for field in ("before", "after", "next_step"):
            with self.subTest(field=field):
                state = json.loads(json.dumps(good_state))
                state["components"]["claude"][field] = "\ud800"
                if field == "after":
                    state["components"]["claude"]["settled_version"] = "\ud800"
                self.write(records.state_path(), json.dumps(state))
                with self.assertRaises(records.StateUnavailable):
                    records.read_state()
                bad_run = dict(good_run, components=state["components"])
                self.write(records.runs_path(), json.dumps(bad_run) + "\n" + json.dumps(good_run) + "\n")
                problems = []
                self.assertEqual(records.read_runs(report=problems.append), [good_run])
                self.assertIn("skipped 1", "\n".join(problems))
                self.assertEqual(self.run_pass(), "complete")

    def test_finished_records_must_cover_every_fixed_component(self):
        self.run_pass()
        good_state = self.state_record()
        good_run = records.read_runs()[0]
        for missing in ("claude", "codex", "claude-marketplaces", "claude-plugins"):
            with self.subTest(missing=missing):
                components = {name: entry for name, entry in good_state["components"].items() if name != missing}
                state = dict(good_state, components=components, outcome=records.outcome(components))
                run = dict(good_run, components=components, outcome=records.outcome(components))
                self.assertIsNotNone(records.state_problem(state))
                self.assertIsNotNone(records.run_problem(run))

    def write(self, path: pathlib.Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def a_valid_run(self, outcome="complete") -> dict:
        return {"version": 1, "started": "2026-11-01T00:00:00Z", "finished": "2026-11-01T00:01:00Z",
                "trigger": "manual", "outcome": outcome, "components": {name: records.component("current", "1", "1", "", "2026-11-01T00:00:00Z")
                               for name in records.COMPONENTS}, "models_refresh": []}

    def test_only_updated_and_current_are_settled(self):
        self.fake.set("claude", update={"stdout": "ok\n", "set_version": "2.1.320"})
        self.fake.set("codex", update={"stdout": "maybe\n"})
        self.run_pass()
        components = self.components()
        self.assertEqual(components["claude"]["settled_version"], "2.1.320")
        self.assertIsNone(components["codex"]["settled_version"])

    def test_unavailable_state_is_reported_and_everything_is_checked_again(self):
        bad = {"corrupt": "{not json", "wrong type": "[]", "unknown status": None}
        for case, text in bad.items():
            with self.subTest(case=case):
                shutil.rmtree(self.fake.home / ".oah", ignore_errors=True)
                self.write(records.runs_path(), json.dumps(self.a_valid_run()) + "\n")
                if case == "unknown status":
                    record = {"version": 1, "last_started": "2026-11-01T00:00:00Z", "trigger": "manual",
                              "last_finished": "2026-11-01T00:01:00Z", "outcome": "complete", "models_refresh": [],
                              "components": {"claude": {"status": "great", "before": "1", "after": "1",
                                                        "settled_version": None, "next_step": "",
                                                        "checked_at": "2026-11-01T00:00:00Z"}}}
                    text = json.dumps(record)
                if text is not None:
                    self.write(records.state_path(), text)
                with self.assertRaises(records.StateUnavailable):
                    records.read_state()
                self.fake.log.unlink(missing_ok=True)
                self.lines.clear()
                self.run_pass()
                self.assertTrue(any(line.startswith("OAH_UPDATE_STATE_UNAVAILABLE") for line in self.lines))
                self.assertTrue(self.fake.ran("claude", "update") and self.fake.ran("codex", "update"))
                self.assertEqual(set(self.components()), set(records.COMPONENTS))  # a fresh, valid record

    def test_a_missing_updates_folder_is_fresh(self):
        self.assertIsNone(records.read_state())
        self.run_pass()
        self.assertFalse(any("UNAVAILABLE" in line for line in self.lines))

    def assert_invalid_component(self, entry):
        now = records.stamp(records.utc_now())
        run = dict(self.a_valid_run(), components=dict(self.a_valid_run()["components"], claude=entry), started=now, finished=now)
        self.assertIsNotNone(records.run_problem(run))
        state = {"version": 1, "last_started": run["started"], "last_finished": run["finished"],
                 "trigger": run["trigger"], "outcome": run["outcome"], "components": run["components"],
                 "models_refresh": []}
        self.write(records.state_path(), json.dumps(state))
        with self.assertRaises(records.StateUnavailable):
            records.read_state()
        with mock.patch.object(passes, "_popen") as launch:
            self.assertTrue(passes.start_after_finish(self.root, self.state, self.fake.env, out=self.lines.append))
        launch.assert_called_once()

    def test_settled_results_require_readable_before_and_after(self):
        for status in records.SETTLED:
            for before, after in ((None, "1"), ("", "1"), ("1", None), ("1", ""), ("�", "1")):
                with self.subTest(status=status, before=before, after=after):
                    self.assert_invalid_component(records.component(status, before, after, "", "2026-11-01T00:00:00Z"))

    def test_settled_status_requires_the_matching_version_relationship(self):
        for status, before, after in (("updated", "1", "1"), ("current", "1", "2")):
            with self.subTest(status=status):
                self.assert_invalid_component(records.component(status, before, after, "", "2026-11-01T00:00:00Z"))
        for status in records.STATUSES:
            with self.subTest(status=status):
                entry = records.component(status, "1", "2" if status == "updated" else "1", "",
                                          "2026-11-01T00:00:00Z")
                entry["settled_version"] = "wrong"
                self.assert_invalid_component(entry)

    def test_finished_outcome_must_match_components_and_bad_recent_state_is_due(self):
        for status, claimed in (("failed", "complete"), ("current", "needs-attention")):
            with self.subTest(status=status):
                now = records.stamp(records.utc_now())
                entry = records.component(status, "1", "1", "", now)
                run = dict(self.a_valid_run(claimed), started=now, finished=now, components=dict(self.a_valid_run()["components"], claude=entry))
                self.assertIsNotNone(records.run_problem(run))
                state = {"version": 1, "last_started": now, "last_finished": now, "trigger": "manual",
                         "outcome": claimed, "components": run["components"], "models_refresh": []}
                self.write(records.state_path(), json.dumps(state))
                self.lines.clear()
                self.assertEqual(self.run_pass(trigger="after-finish"), "complete")
                self.assertTrue(any("OAH_UPDATE_STATE_UNAVAILABLE" in line for line in self.lines))

    def test_bad_history_lines_are_display_only_and_a_later_pass_survives(self):
        self.run_pass()
        with records.runs_path().open("ab") as handle:
            handle.write(b'{torn\n\xff\n{"version": 1}\n')
        status = "\n".join(passes.status_lines())
        self.assertIn("OAH_UPDATE_STATE_UNAVAILABLE", status)
        self.assertIn("skipped 3", status)
        self.lines.clear()
        self.assertEqual(self.run_pass(trigger="after-finish"), "skipped")
        self.assertNotIn("UNAVAILABLE", "\n".join(self.lines))
        self.assertEqual(self.run_pass(), "complete")
        self.assertEqual(len(records.read_runs()), 2)

    def test_bad_run_lines_are_skipped_and_the_file_is_capped(self):
        lines = [json.dumps(self.a_valid_run()) for _ in range(records.RUNS_KEPT + 5)]
        lines.insert(3, "{torn")
        lines.insert(7, json.dumps({"version": 1}))
        self.write(records.runs_path(), "\n".join(lines) + "\n")
        self.assertEqual(len(records.read_runs()), records.RUNS_KEPT + 5)
        self.run_pass()
        kept = records.read_runs()
        self.assertEqual(len(kept), records.RUNS_KEPT)
        self.assertEqual(len(records.runs_path().read_text("utf-8").splitlines()), records.RUNS_KEPT)
        self.assertEqual(kept[-1]["outcome"], "complete")
        self.assertNotEqual(kept[-1]["started"], "2026-11-01T00:00:00Z")

    def test_status_survives_a_bad_state_and_runs_no_subprocess(self):
        self.write(records.runs_path(), "{torn\n" + json.dumps(dict(self.a_valid_run(), components=dict(self.a_valid_run()["components"],
            claude=records.component("current", "1", "1", "", "2026-11-01T00:00:00Z")))) + "\n")
        self.write(records.state_path(), "{not json")
        lines = passes.status_lines()
        self.assertTrue(any(line.startswith("OAH_UPDATE_STATE_UNAVAILABLE") for line in lines))
        self.assertTrue(any("recent pass finished 2026-11-01T00:01:00Z" in line for line in lines))
        self.assertNotIn("oah: Claude Code 1 (current)", lines)
        self.assertEqual(self.launched, [])

    def test_canary_environment_value_never_reaches_a_record(self):
        canary = "canary-" + "7f3e" * 6
        env = dict(self.fake.env, FAKE_CANARY_TOKEN=canary)
        self.fake.set("claude", update={"stdout": f"token {canary}\n", "set_version": "2.1.320"},
                      plugins=[{"id": "a@m", "version": f"1.0.0-{canary}", "scope": "user"}])
        self.run_pass(env=env)
        for path in (records.state_path(), records.runs_path()):
            self.assertNotIn(canary, path.read_text("utf-8"))
        self.assertNotIn(canary, "\n".join(self.lines))
        self.assertIn("[REDACTED]", records.state_path().read_text("utf-8"))


class LockTest(FakeToolsCase):
    def test_second_pass_is_busy_and_runs_nothing(self):
        records.updates_dir().mkdir(parents=True)
        with StateLock(records.lock_path()):
            self.assertEqual(self.run_pass(), "busy")
        self.assertEqual(self.fake.argv(), [])
        self.assertEqual(self.launched, [])
        self.assertTrue(self.lines[0].startswith("OAH_UPDATE_BUSY: another update pass is running"))
        self.assertFalse(records.state_path().exists())


def write_lease(state: pathlib.Path, *, expires: str) -> None:
    lease = {"id": "a1b2c3d4e5f60718", "task": "other", "agent": "codex", "scope": ["docs"], "note": "",
             "worktree": str(state.parent.parent), "branch": "main", "head_at_start": None,
             "created_at": "2026-11-01T00:00:00Z", "expires_at": expires}
    state.mkdir(parents=True, exist_ok=True)
    (state / "leases.json").write_text(json.dumps({"version": 1, "leases": [lease]}), encoding="utf-8")


def write_run(state: pathlib.Path, text: str | None, name: str = "r20261103T000000Z-0a1b2c3d") -> None:
    folder = state / "runs" / "t1" / name
    folder.mkdir(parents=True)
    if text is not None:
        (folder / "result.json").write_text(text, encoding="utf-8")


class ReadinessTest(FakeToolsCase):
    def future(self, hours=4) -> str:
        return records.stamp(records.utc_now() + dt.timedelta(hours=hours))

    def test_in_flight_work_defers_the_pass(self):
        cases = {
            "lease": lambda: write_lease(self.state, expires=self.future()),
            "running": lambda: write_run(self.state, json.dumps({"status": "running"})),
            "queued": lambda: write_run(self.state, json.dumps({"status": "queued"})),
            "unreadable": lambda: write_run(self.state, "{torn"),
            "missing record": lambda: write_run(self.state, None),
            "being created": lambda: write_run(self.state, None, name=".r20261103T000000Z-0a1b2c3d.tmp"),
        }
        for case, make in cases.items():
            with self.subTest(case=case):
                shutil.rmtree(self.state, ignore_errors=True)
                make()
                self.lines.clear()
                self.assertEqual(self.run_pass(), "deferred")
                self.assertTrue(self.lines[-1].startswith("OAH_UPDATE_DEFERRED"), self.lines)
                self.assertEqual(self.fake.argv(), [])
                self.assertEqual(records.read_runs(), [])  # nothing ran, so nothing is recorded
                self.assertFalse(records.state_path().exists())

    def test_stale_lease_and_finished_runs_are_ready(self):
        write_lease(self.state, expires="2026-01-01T00:00:00Z")
        write_run(self.state, json.dumps({"status": "finished"}))
        self.assertIsNone(passes.readiness(self.state))
        self.assertEqual(self.run_pass(), "complete")

    def test_linked_task_storage_defers_without_following_it(self):
        if os.name == "nt":
            self.skipTest("directory symlinks require Windows developer mode")
        outside = make_tmp_dir(self)
        write_run(outside, json.dumps({"status": "running"}))
        runs = self.state / "runs"
        runs.mkdir(parents=True)
        (runs / "t1").symlink_to(outside / "runs" / "t1", target_is_directory=True)
        self.assertIsNotNone(passes.readiness(self.state))
        self.assertEqual(self.run_pass(), "deferred")
        self.assertEqual(self.fake.argv(), [])

    def test_files_in_place_of_run_directories_defer(self):
        runs = self.state / "runs"
        for relative in ("runs", "runs/t1", "runs/t1/r20261103T000000Z-0a1b2c3d"):
            with self.subTest(path=relative):
                shutil.rmtree(runs, ignore_errors=True)
                runs.unlink(missing_ok=True)
                path = self.state / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("not a directory", encoding="utf-8")
                self.assertIsNotNone(passes.readiness(self.state))
                self.assertEqual(self.run_pass(), "deferred")
                self.assertEqual(self.fake.argv(), [])

    def test_unreadable_result_defers(self):
        write_run(self.state, json.dumps({"status": "finished"}))
        read_bytes = pathlib.Path.read_bytes

        def denied(path):
            if path.name == "result.json":
                raise PermissionError("result denied")
            return read_bytes(path)

        with mock.patch.object(pathlib.Path, "read_bytes", denied):
            self.assertIsNotNone(passes.readiness(self.state))
            self.assertEqual(self.run_pass(), "deferred")
        self.assertEqual(self.fake.argv(), [])


class StartAfterFinishTest(FakeToolsCase):
    """The trigger alone; oah finish's own lines and exit code are checked in test_lease_cli."""

    def start(self, *, env=None, os_name="posix", popen=None, now=None):
        popen = popen or mock.Mock()
        lines: list[str] = []
        with mock.patch.object(passes, "_popen", popen):
            started = passes.start_after_finish(self.root, self.state, env or self.fake.env, out=lines.append,
                                                os_name=os_name, **({"now": now} if now else {}))
        return started, popen, lines

    def test_all_clear_starts_one_detached_pass(self):
        started, popen, lines = self.start()
        self.assertTrue(started)
        popen.assert_called_once_with(
            [sys.executable, "-m", "oah", "update", "--after-finish", "--repo", str(self.root)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(self.root),
            env=dict(self.fake.env, OAH_STATE_DIR=str(self.state.resolve())), close_fds=True, start_new_session=True)
        self.assertEqual(lines, [passes.STARTED])
        self.assertEqual(self.launched, [])

    def test_windows_flags(self):
        _started, popen, _lines = self.start(os_name="nt")
        flags = popen.call_args.kwargs["creationflags"]
        self.assertEqual(flags, 0x00000200 | 0x00000008 | 0x08000000)
        self.assertNotIn("start_new_session", popen.call_args.kwargs)

    def test_nothing_starts_while_work_is_in_flight(self):
        for case, make in {"lease": lambda: write_lease(self.state, expires=records.stamp(
                               records.utc_now() + dt.timedelta(hours=1))),
                           "running": lambda: write_run(self.state, json.dumps({"status": "running"})),
                           "queued": lambda: write_run(self.state, json.dumps({"status": "queued"})),
                           "unreadable": lambda: write_run(self.state, "{torn")}.items():
            with self.subTest(case=case):
                shutil.rmtree(self.state, ignore_errors=True)
                make()
                started, popen, lines = self.start()
                self.assertFalse(started)
                popen.assert_not_called()
                self.assertEqual(lines, [])

    def test_not_due_starts_nothing_and_unreadable_or_future_state_is_due(self):
        records.updates_dir().mkdir(parents=True)
        now = records.utc_now()
        record = {"version": 1, "trigger": "after-finish", "last_finished": None, "outcome": None,
                  "components": {}, "models_refresh": []}
        cases = [("recent", records.stamp(now - dt.timedelta(hours=1)), False),
                 ("old", records.stamp(now - dt.timedelta(hours=25)), True),
                 ("future", records.stamp(now + dt.timedelta(hours=5)), True),
                 ("unreadable", None, True)]
        for case, started_at, expected in cases:
            with self.subTest(case=case):
                text = "{torn" if started_at is None else json.dumps(dict(record, last_started=started_at))
                records.state_path().write_text(text, encoding="utf-8")
                started, popen, _lines = self.start(now=lambda: now)
                self.assertIs(started, expected)
                self.assertEqual(popen.call_count, int(expected))

    def test_popen_failure_prints_one_skipped_line(self):
        started, _popen, lines = self.start(popen=mock.Mock(side_effect=OSError("no such file")))
        self.assertFalse(started)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("OAH_UPDATE_SKIPPED: the background update pass could not start"))

    def test_worker_and_off_switch_start_nothing_and_write_nothing(self):
        for case, env, config in (("worker", dict(self.fake.env, OAH_WORKER="1"), "after_finish = true\n"),
                                  ("off", None, "after_finish = false\n"), ("absent", None, None)):
            with self.subTest(case=case):
                self.configure(config)
                started, popen, lines = self.start(env=env)
                self.assertFalse(started)
                popen.assert_not_called()
                self.assertEqual(lines, [])
                self.assertFalse((self.fake.home / ".oah").exists())

    def test_unreadable_switch_prints_one_skipped_line(self):
        self.configure('after_finish = "sometimes"\n')
        started, popen, lines = self.start()
        self.assertFalse(started)
        popen.assert_not_called()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("OAH_UPDATE_SKIPPED: OAH_CONFIG_INVALID"))


class AfterFinishPassTest(FakeToolsCase):
    def test_log_holds_the_lines_and_a_not_due_pass_keeps_the_last_log(self):
        log = records.log_path()
        self.assertEqual(passes.run_pass(self.root, self.state, env=self.fake.env, trigger="after-finish",
                                         out=self.lines.append, log=log), "complete")
        first = log.read_text("utf-8")
        self.assertIn("OAH_UPDATE_FINISHED: outcome complete", first)
        self.assertEqual(passes.run_pass(self.root, self.state, env=self.fake.env, trigger="after-finish",
                                         out=self.lines.append, log=log), "skipped")
        self.assertEqual(log.read_text("utf-8"), first)
        self.assertEqual(passes.run_pass(self.root, self.state, env=self.fake.env, trigger="manual",
                                         out=self.lines.append), "complete")


class DoctorNoticesTest(FakeToolsCase):
    def test_off_by_absence_is_mentioned_once_and_explicit_settings_are_quiet(self):
        self.configure(None)
        notices = passes.doctor_notices(self.root)
        self.assertEqual([notice["code"] for notice in notices], ["OAH_UPDATE_OFF"])
        self.assertIn("oah update", notices[0]["message"])
        self.configure("after_finish = false\n")
        self.assertEqual(passes.doctor_notices(self.root), [])
        self.assertEqual(passes.doctor_notices(None), [])

    def test_unsettled_components_one_line_each(self):
        self.fake.set("codex", update={"stdout": "maybe\n"})
        self.run_pass()
        launched = len(self.launched)
        notices = passes.doctor_notices(self.root)
        self.assertEqual([(notice["code"], notice.get("component")) for notice in notices],
                         [("OAH_UPDATE_UNSETTLED", "codex"), ("OAH_UPDATE_UNSETTLED", "claude-plugins")])
        self.assertIn("(unverified); run codex update yourself", notices[0]["message"])
        self.assertEqual(len(self.launched), launched)  # doctor stays offline: it reads the records only


class CodesTest(ToolUpdateTestCase):
    def test_every_update_code_has_a_next_step_and_both_troubleshooting_rows(self):
        import re

        from oah.review import fix

        repo = pathlib.Path(__file__).resolve().parent.parent
        sources = [*(repo / "src" / "oah" / "toolupdate").glob("*.py"), repo / "src" / "oah" / "update_cli.py"]
        codes = set()
        for path in sources:
            codes |= set(re.findall(r"\bOAH_UPDATE_[A-Z_]+\b", path.read_text(encoding="utf-8")))
        self.assertEqual(codes, {"OAH_UPDATE_STARTED", "OAH_UPDATE_SKIPPED", "OAH_UPDATE_BUSY", "OAH_UPDATE_DEFERRED",
                                 "OAH_UPDATE_STATE_UNAVAILABLE", "OAH_UPDATE_FINISHED", "OAH_UPDATE_OFF",
                                 "OAH_UPDATE_UNSETTLED"})
        pages = [(repo / "docs" / page).read_text(encoding="utf-8")
                 for page in ("troubleshooting.md", "ko/troubleshooting.md")]
        for code in sorted(codes):
            with self.subTest(code=code):
                self.assertRegex(fix._ERROR_DIAGNOSTICS[code], r"oah |\.oah/config\.toml")
                for page in pages:
                    self.assertIn(f"| `{code}` |", page)


if __name__ == "__main__":
    unittest.main()
