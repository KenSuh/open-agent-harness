# 선택 동반 앱 — Orca·Aside 의 공식 설치 항목, 동의 기반 연결 단계(CLI·스킬·프로젝트 등록·로그인·MCP), 선택 규칙 절을 담는다.
"""Optional companion apps: Orca and Aside (PLAN A20).

- `companion_tools()` gives the two optional catalog entries; `oah.setup.tools.catalog()` appends them,
  so `oah doctor` and the guided setup list, check and offer them like any other tool.
- `plan_steps` / `run_steps` / `companion_setup` walk the steps after an app is installed. The whole
  plan is printed first, and each command runs only after a `y` (or `yes`, which covers only the
  printed plan). Steps that write outside the project are labelled `[global]`.
- Checks use only the authentication status column or an exact MCP entry comparison, then discard output.
  Account identifiers and credentials are never extracted, shown, logged or saved.
- `rules_section(aside_registered)` is the optional rules text for the rules block (5.11).

`python -m oah.setup.companions [--app orca|aside]... [--os ...] [--project PATH] [--yes] [--dry-run]`
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from oah.engines.proc import UnsupportedBatchShim, is_batch, resolve_command, run_command
from oah.setup.tools import POWERSHELL_PREFIX, Argv, Tool, display, sh

URL_ORCA = "https://www.onorca.dev/download"
URL_ORCA_LINUX = "https://www.onorca.dev/docs/install"
URL_ORCA_CLI = "https://www.onorca.dev/docs/cli/overview"
URL_ASIDE_MAC = "https://aside.com/download?os=mac"
URL_ASIDE_WIN = "https://aside.com/download?os=win"
URL_ASIDE_DEV = "https://docs.aside.com/help/developers"

ORCA_CASK = "stablyai/orca/orca"  # the plain `orca` cask is an unrelated project
ASIDE_CLI_MACOS = "curl -fsSL https://releases.aside.com/install.sh | bash"
APPS = ("orca", "aside")
PROBE_TIMEOUT = 60.0
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates" / "companions"
RULES_BEGIN = "<!-- OAH:COMPANIONS:ASIDE:BEGIN v1 -->"
RULES_END = "<!-- OAH:COMPANIONS:ASIDE:END -->"
RULES_LANGS = ("en", "ko")
GITHUB_REMOTE = re.compile(
    r"^(?:(?:https?|ssh)://(?:[^@/]+@)?github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$")

Which = Callable[..., "str | None"]
Out = Callable[[str], None]
Ask = Callable[[str], "str | None"]  # like input(), but None at end of input
Runner = Callable[[Argv, pathlib.Path], int]
Probe = Callable[[Argv, pathlib.Path], "int | None"]  # 0 ready/exact entry, 1 absent/not ready, 2 unverified MCP, None unavailable

OK_STATUSES = ("done", "present", "manual")


def companion_tools() -> list[Tool]:
    """Orca and Aside as optional catalog entries, with each one's official install per OS."""
    return [
        Tool(name="orca", required=False, optional=True,
             purpose="optional app: runs coding agents side by side in isolated git worktrees",
             check=("orca", "--version"), version_args=("--version",),
             install={"macos": (("brew", "install", "--cask", ORCA_CASK),),
                      "windows": (("winget", "install", "--id", "StablyAI.Orca", "-e"),),
                      "linux": ()},
             url=URL_ORCA, os_url={"linux": URL_ORCA_LINUX},
             note={"macos": "Without Homebrew, download Orca from the official page. Then turn on the orca "
                            "command in Orca: Settings > General > Orca CLI.",
                   "windows": "Without winget, use the official installer from the download page. Then turn on "
                              "the orca command in Orca: Settings > General > Orca CLI.",
                   "linux": "Install Orca from the official Linux page."}),
        Tool(name="aside", required=False, optional=True,
             purpose="optional app: a browser with an AI agent that Claude Code and Codex use for web tasks",
             check=("aside", "--version"), version_args=("--version",),
             install={"macos": (("brew", "install", "--cask", "aside"),), "windows": (), "linux": ()},
             url=URL_ASIDE_MAC, os_url={"windows": URL_ASIDE_WIN, "linux": URL_ASIDE_DEV},
             note={"macos": "The aside command itself comes from Aside's CLI installer: "
                            f"{ASIDE_CLI_MACOS} ({URL_ASIDE_DEV}). python -m oah.setup.companions offers it.",
                   "windows": "Download the official installer from the page below and run it. The aside command "
                              "comes from Aside's CLI installer; python -m oah.setup.companions offers it.",
                   "linux": "Aside does not support Linux."}),
    ]


@dataclass(frozen=True)
class CompanionStep:
    key: str
    app: str  # "orca" or "aside"
    title: str
    argv: Argv | None  # None: the user does this step themselves, following `instruction`
    display: str  # the exact command text shown before asking
    source: str  # where the command comes from
    global_write: bool  # writes outside the project
    instruction: str = ""
    needs: tuple[str, ...] = ()  # programs that must be on PATH
    requires: str | None = None  # an earlier step that must be done or present
    skip_if: Argv | None = None  # probe 0: ready/exact entry; 2: preserve unverified MCP configuration
    skip_if_found: str | None = None  # this program on PATH: already done
    verify: Argv | None = None  # readiness check after the step
    already: str = "already done"


@dataclass(frozen=True)
class StepOutcome:
    step: CompanionStep
    status: str  # done, present, manual, declined, blocked, failed or pending


@dataclass
class CompanionResult:
    outcomes: list[StepOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.status in OK_STATUSES for o in self.outcomes)

    @property
    def pending(self) -> list[str]:
        return [o.step.key for o in self.outcomes if o.status not in OK_STATUSES]

    @property
    def aside_registered(self) -> bool:
        """True when oah registered Aside this run or found exactly the entry it would write."""
        return any(o.step.key.startswith("aside-mcp-") and o.status in ("done", "present") for o in self.outcomes)


def os_for(platform: str) -> str:
    if platform == "win32":
        return "windows"
    return "linux" if platform.startswith("linux") else "macos"


def github_slug(remote_url: str | None) -> str | None:
    """`owner/repo` from a GitHub remote URL, else None. Any user or token part of the URL is dropped."""
    match = GITHUB_REMOTE.match((remote_url or "").strip())
    return f"{match['owner']}/{match['repo']}" if match else None


def show(argv: Argv) -> str:
    """How a plain argv is shown: words joined by spaces, a word with whitespace in double quotes."""
    return " ".join(f'"{part}"' if not part or any(c.isspace() for c in part) else part for part in argv)


def _orca_steps(project_root: pathlib.Path, remote_url: str | None) -> list[CompanionStep]:
    steps = [
        CompanionStep(
            "orca-cli", "orca", "Turn on the orca command", None, "", URL_ORCA_CLI, False,
            instruction=("Open Orca, go to Settings > General > Orca CLI and register the orca command. "
                         "If Orca is not installed yet, install it first with `python -m oah.setup` or from "
                         f"{URL_ORCA}."),
            skip_if=("orca", "--version"), verify=("orca", "--version"), already="the orca command works"),
        CompanionStep(
            "orca-skills", "orca", "Add Orca's agent guides for your coding agents",
            ("orca", "skills", "install", "--all"), "orca skills install --all", "orca skills install --help", True,
            needs=("orca", "npx"), requires="orca-cli"),
    ]
    slug = github_slug(remote_url)
    title = "Register this project in Orca"
    if slug is None:
        steps.append(CompanionStep(
            "orca-project", "orca", title, None, "", "orca project setup-existing-folder --help", True,
            instruction=(f"This project has no GitHub remote, so oah cannot name it for Orca. Open Orca and add "
                         f"this folder from its sidebar: {project_root}"),
            requires="orca-cli"))
    else:
        argv = ("orca", "project", "setup-existing-folder", "--project", f"github:{slug}", "--host", "local",
                "--path", str(project_root), "--kind", "git")
        steps.append(CompanionStep("orca-project", "orca", title, argv, show(argv),
                                   "orca project setup-existing-folder --help", True, needs=("orca",),
                                   requires="orca-cli"))
    return steps


def _aside_steps(os_name: str, template_dir: pathlib.Path) -> list[CompanionStep]:
    title = "Install the aside command (Aside's CLI)"
    if os_name == "linux":
        return [CompanionStep("aside-cli", "aside", title, None, "", URL_ASIDE_DEV, False,
                              instruction="Aside does not support Linux, so there is nothing to set up here.")]
    if os_name == "windows":
        script = template_dir / "aside-cli-install.ps1"
        argv: Argv = POWERSHELL_PREFIX[:-1] + ("-File", str(script))
        text = script.read_text(encoding="utf-8").strip().replace("\r\n", "\n")
        cli = CompanionStep("aside-cli", "aside", title, argv, text, URL_ASIDE_DEV, True, needs=("powershell",),
                            skip_if_found="aside", verify=("aside", "--version"), already="the aside command is installed")
    else:
        cli = CompanionStep("aside-cli", "aside", title, sh(ASIDE_CLI_MACOS), ASIDE_CLI_MACOS, URL_ASIDE_DEV, True,
                            needs=("curl",), skip_if_found="aside", verify=("aside", "--version"),
                            already="the aside command is installed")
    steps = [cli, CompanionStep(
        "aside-login", "aside", "Sign in to Aside", None, "", URL_ASIDE_DEV, False,
        instruction=("1. Open Aside and sign in inside the app (a free plan exists). "
                     "2. In a new terminal window, run: aside login "
                     "3. Approve the code it shows in Aside: Settings > Account > Devices. "
                     "Your account stays with Aside; oah checks only the sign-in status column from "
                     "`aside account status`."),
        needs=("aside",), requires="aside-cli", skip_if=("aside", "account", "status"),
        verify=("aside", "account", "status"), already="the aside command is signed in")]
    for engine, label, argv in (
            ("claude", "Claude Code", ("claude", "mcp", "add", "--scope", "user", "--transport", "stdio", "aside",
                                      "--", "aside", "mcp")),
            ("codex", "Codex", ("codex", "mcp", "add", "aside", "--", "aside", "mcp"))):
        check = (engine, "mcp", "get", "aside")
        steps.append(CompanionStep(f"aside-mcp-{engine}", "aside", f"Let {label} use Aside (MCP server)", argv,
                                   show(argv), URL_ASIDE_DEV, True, needs=(engine, "aside"), requires="aside-cli",
                                   skip_if=check, verify=check, already=f"Aside is already registered for {label}"))
    return steps


def plan_steps(apps: Iterable[str], *, os_name: str, project_root: pathlib.Path, remote_url: str | None,
               template_dir: pathlib.Path | None = None) -> list[CompanionStep]:
    """The steps after installing each chosen app, in order. Plans only; runs nothing."""
    template_dir = TEMPLATES if template_dir is None else template_dir
    steps: list[CompanionStep] = []
    for app in apps:
        if app == "orca":
            steps += _orca_steps(project_root, remote_url)
        elif app == "aside":
            steps += _aside_steps(os_name, template_dir)
        else:
            raise ValueError(f"unknown companion app: {app}")
    return steps


def print_plan(steps: Sequence[CompanionStep], *, out: Out = print) -> None:
    out("Optional companion apps: the steps below. Nothing runs until you answer y (or you passed --yes).")
    out("--yes skips every step for unnamed apps; use --app orca or --app aside, or confirm the app interactively.")
    out("[global] marks a step that changes files outside this project, in your user folder.")
    for number, step in enumerate(steps, 1):
        out(f"  {number}. {step.app}: {step.title}{' [global]' if step.global_write else ''}")
        if step.argv is not None:
            lines = step.display.splitlines()
            out(f"       runs: {lines[0]}")
            for line in lines[1:]:
                out(f"             {line}")
        else:
            out(f"       you do it: {step.instruction}")
        out(f"       source: {step.source}")


def default_probe(argv: Argv, cwd: pathlib.Path) -> int | None:
    """Return readiness or an exact MCP entry match, not proof it can launch; discard output."""
    try:
        resolved = resolve_command(argv[0])
        if resolved is None:
            return None
        args = tuple(argv[1:])
        if argv == ("codex", "mcp", "get", "aside"):
            args += ("--json",)
        result = run_command(resolved.argv + args, timeout=PROBE_TIMEOUT, cwd=str(cwd))
    except (OSError, UnsupportedBatchShim):
        return None
    if result.timed_out:
        return None
    if result.returncode != 0:
        return result.returncode
    output = result.stdout.replace("\r\n", "\n")
    if argv == ("aside", "account", "status"):
        # Vendor output: marker/id, title, status, profiles. Never extract the identity columns.
        status = re.search(r"^\* u[0-9]+  [^\r\n]+  (signed in|signed out/paused|signed out|local)"
                           r"  profiles: [^\r\n]*$", output, re.MULTILINE)
        return 0 if status is not None and status[1] == "signed in" else 1
    if argv == ("codex", "mcp", "get", "aside"):
        try:
            data = json.loads(output)
        except (ValueError, TypeError):
            return 2
        if not isinstance(data, dict):
            return 2
        transport = data.get("transport")
        if not isinstance(transport, dict) or data.get("enabled") is not True:
            return 2
        # Vendor JSON includes unset defaults. Any override or unknown setting is a different entry.
        defaults = {"disabled_reason": None, "startup_timeout_sec": None, "tool_timeout_sec": None,
                    "enabled_tools": None, "disabled_tools": None}
        transport_defaults = {"env": None, "env_vars": [], "cwd": None}
        expected = defaults | {"name": "aside", "enabled": True, "transport": transport_defaults | {
            "type": "stdio", "command": "aside", "args": ["mcp"]}}
        actual = defaults | data | {"transport": transport_defaults | transport}
        return 0 if actual == expected else 2
    if argv == ("claude", "mcp", "get", "aside"):
        # Compare the complete displayed configuration, allowing only known presentation lines.
        # Connection failure is not a configuration difference; a disabled entry is.
        expected = {"Scope": "User config (available in all your projects)", "Type": "stdio",
                    "Command": "aside", "Args": "mcp"}
        settings = {}
        if not output.startswith("aside:\n"):
            return 2
        for line in output.splitlines()[1:]:
            if not line.strip() or line in ("  Environment:",
                    'To remove this server, run: claude mcp remove "aside" -s user'):
                continue
            if line.startswith("  Status: ") and "disabled" not in line.lower():
                continue
            field = re.fullmatch(r"  (Scope|Type|Command|Args): (.*)", line)
            if field is None or field[1] in settings:
                return 2
            settings[field[1]] = field[2]
        return 0 if settings == expected else 2
    return result.returncode


def default_runner(argv: Argv, cwd: pathlib.Path) -> int:
    """Run a step attached to this terminal, in the project folder, so its own prompts reach the user."""
    resolved = resolve_command(argv[0])
    if resolved is None:
        raise FileNotFoundError(f"{argv[0]} was not found on PATH")
    if is_batch(resolved.argv[0]):
        raise UnsupportedBatchShim(resolved.argv[0], "batch files are never launched directly")
    return subprocess.run(list(resolved.argv) + list(argv[1:]), check=False, cwd=str(cwd)).returncode


def _confirm(step: CompanionStep, ask: Ask, out: Out) -> bool:
    out("")
    out(f"{step.app}: {step.title}{' [global]' if step.global_write else ''}")
    lines = step.display.splitlines()
    out(f"  This runs: {lines[0]}")
    for line in lines[1:]:
        out(f"             {line}")
    out(f"  Source: {step.source}")
    if step.global_write:
        out("  It changes files outside this project, in your user folder.")
    answer = ask("  Run it now? [y/N] ")
    return answer is not None and answer.strip().lower() in ("y", "yes")


def _missing_program(program: str, step: CompanionStep) -> str:
    return (f"{step.app}: {step.title}: skipped, because `{program}` is not installed or not on PATH. Install it "
            "(`python -m oah.setup` lists it), open a new terminal, then run `python -m oah.setup.companions` "
            "again.")


def _instruction(step: CompanionStep, *, interactive: bool, ask: Ask, out: Out, check: Callable[[], bool],
                 refresh: Callable[[], None]) -> str:
    out("")
    out(f"{step.app}: {step.title}")
    out(f"  {step.instruction}")
    out(f"  More: {step.source}")
    if step.verify is None:
        return "manual"
    if not interactive:
        refresh()
        if check():
            out(f"  {step.app}: {step.already}.")
            return "done"
        out("  Not done yet. Finish it, then run `python -m oah.setup.companions` again.")
        return "pending"
    while True:
        answer = ask("  Press Enter here when you have done this, or type s and Enter to skip: ")
        if answer is None or answer.strip().lower() == "s":
            out("  Skipped. Run `python -m oah.setup.companions` again when you are ready.")
            return "pending"
        refresh()
        if check():
            out(f"  {step.app}: {step.already}.")
            return "done"
        out(f"  The check `{show(step.verify)}` did not pass yet. Finish the step above, then press Enter.")


def run_steps(steps: Sequence[CompanionStep], *, project_root: pathlib.Path, yes: bool, which: Which = shutil.which,
              ask: Ask, out: Out = print, runner: Runner = default_runner, probe: Probe = default_probe,
              refresh: Callable[[], None] | None = None, with_optional: Iterable[str] = ()) -> list[StepOutcome]:
    """Run steps for consented apps, asking per command unless `yes`; app consent is at companion_setup."""
    if refresh is None:
        from oah.setup.guide import refresh_path

        refresh = refresh_path
    statuses: dict[str, str] = {}
    outcomes = []

    def record(step: CompanionStep, status: str) -> None:
        statuses[step.key] = status
        outcomes.append(StepOutcome(step, status))

    for step in steps:
        if step.requires is not None and statuses.get(step.requires, "done") not in ("done", "present"):
            out(f"{step.app}: {step.title}: skipped until the earlier {step.app} step is done. Finish that "
                "step, then run `python -m oah.setup.companions` again.")
            record(step, "blocked")
            continue
        if step.skip_if_found is not None and which(step.skip_if_found) is not None:
            out(f"{step.app}: {step.already}.")
            record(step, "present")
            continue
        missing = next((program for program in step.needs if which(program) is None), None)
        if missing is not None:
            out(_missing_program(missing, step))
            record(step, "blocked")
            continue
        if step.skip_if is not None:
            state = probe(step.skip_if, project_root)
            if state == 0:
                out(f"{step.app}: {step.already}.")
                record(step, "present")
                continue
            if step.key.startswith("aside-mcp-") and state != 1:
                check = step.skip_if + (("--json",) if step.skip_if[0] == "codex" else ())
                out(f"{step.app}: {step.title}: existing entry, left unchanged, not verified. "
                    f"Run `{show(check)}` in your terminal to check it in your coding agent's MCP settings, "
                    "then run `python -m oah.setup.companions --app aside` again.")
                record(step, "pending")
                continue
        verify = step.verify
        if step.argv is None:
            record(step, _instruction(step, interactive=not yes, ask=ask, out=out, refresh=refresh,
                                      check=lambda: verify is not None and probe(verify, project_root) == 0))
            continue
        if not yes and not _confirm(step, ask, out):
            out(f"{step.app}: {step.title}: skipped. To do it later, run: {step.display.splitlines()[0]}")
            record(step, "declined")
            continue
        out(f"Running: {step.display.splitlines()[0]}")
        try:
            code = runner(step.argv, project_root)
        except (OSError, UnsupportedBatchShim) as exc:
            out(f"{step.app}: {step.title}: could not start: {exc}. Follow {step.source} instead.")
            record(step, "failed")
            continue
        if code != 0:
            out(f"{step.app}: {step.title}: the command exited with code {code}. Read its messages above, fix "
                "the cause, then run `python -m oah.setup.companions` again.")
            record(step, "failed")
            continue
        refresh()
        if verify is not None and not step.key.startswith("aside-mcp-") and probe(verify, project_root) != 0:
            out(f"{step.app}: {step.title}: the command finished, but `{show(verify)}` does not pass yet. Open a "
                "new terminal and run `python -m oah.setup.companions` again.")
            record(step, "pending")
            continue
        out(f"{step.app}: {step.title}: done.")
        record(step, "done")
    return outcomes


def companion_setup(*, apps: Iterable[str] = APPS, os_name: str, project_root: pathlib.Path, remote_url: str | None,
                    yes: bool = False, dry_run: bool = False, which: Which = shutil.which, ask: Ask,
                    out: Out = print, runner: Runner = default_runner, probe: Probe = default_probe,
                    refresh: Callable[[], None] | None = None, with_optional: Iterable[str] = ()) -> CompanionResult:
    """Print the plan, then run the steps (none in a dry run). The last line is a COMPANIONS_* token."""
    steps = plan_steps(apps, os_name=os_name, project_root=project_root, remote_url=remote_url)
    print_plan(steps, out=out)
    if dry_run:
        out(f"COMPANIONS_DRY_RUN steps={len(steps)}")
        return CompanionResult()
    named = set(with_optional)
    outcomes = []
    for app in dict.fromkeys(step.app for step in steps):
        app_steps = [step for step in steps if step.app == app]
        consented = app in named
        if not consented and not yes:
            purpose = next(tool.purpose for tool in companion_tools() if tool.name == app)
            out(f"{app}: {purpose}. Select it to review and confirm its steps.")
            answer = ask(f"  Set up {app}? [y/N] ")
            consented = answer is not None and answer.strip().lower() in ("y", "yes")
        if not consented:
            out(f"{app}: skipped. To set it up, run `python -m oah.setup.companions --app {app}` "
                "and confirm each step.")
            outcomes.extend(StepOutcome(step, "declined") for step in app_steps)
            continue
        outcomes.extend(run_steps(app_steps, project_root=project_root, yes=yes, which=which, ask=ask, out=out,
                                  runner=runner, probe=probe, refresh=refresh))
    result = CompanionResult(outcomes)
    out("")
    out("COMPANIONS_OK" if result.ok else f"COMPANIONS_INCOMPLETE pending={','.join(result.pending)}")
    return result


def rules_section(aside_registered: bool, *, lang: str = "en") -> str | None:
    """The optional Aside rules for the rules block, between its own markers; None when Aside is not registered."""
    if lang not in RULES_LANGS:
        raise ValueError(f"OAH_USAGE: no Aside rules text for language {lang!r}; use one of {', '.join(RULES_LANGS)}")
    if not aside_registered:
        return None
    body = (TEMPLATES / f"rules-aside.{lang}.md").read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    return f"{RULES_BEGIN}\n{body}\n{RULES_END}\n"


def _origin_url(project_root: pathlib.Path) -> str | None:
    """The `origin` remote URL, used only to derive a GitHub `owner/repo`. Never printed."""
    try:
        git = resolve_command("git")
        if git is None:
            return None
        result = run_command(git.argv + ("-C", str(project_root), "remote", "get-url", "origin"), timeout=10)
    except (OSError, UnsupportedBatchShim):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _ask_input(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    from oah.console import configure_stdio
    from oah.doctor import project_root as find_project_root

    configure_stdio()
    parser = argparse.ArgumentParser(prog="python -m oah.setup.companions",
                                     description="Connect the optional apps Orca and Aside to this project, "
                                                 "asking before each step.")
    parser.add_argument("--app", action="append", choices=APPS,
                        help="which app to set up; repeat for both (default: both)")
    parser.add_argument("--os", choices=("macos", "windows", "linux"), default=os_for(sys.platform),
                        help="which OS's commands to use (default: this one)")
    parser.add_argument("--project", type=pathlib.Path, help="the project folder (default: this git repository)")
    parser.add_argument("--yes", action="store_true",
                        help="confirm printed commands only for apps explicitly named with --app")
    parser.add_argument("--dry-run", action="store_true", help="only print the plan; run nothing")
    args = parser.parse_args(argv)
    root = args.project or find_project_root(None) or pathlib.Path.cwd()
    root = root.resolve()
    apps = tuple(dict.fromkeys(args.app)) if args.app else APPS
    result = companion_setup(apps=apps, os_name=args.os, project_root=root, remote_url=_origin_url(root),
                             yes=args.yes, dry_run=args.dry_run, ask=_ask_input, with_optional=args.app or ())
    return 0 if args.dry_run or result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
