# CI 스타터 명령 — oah ci init 의 인자·설정·질문을 맡고, 파일 전체와 diff 를 보여 준 뒤 확인을 받은 때만 oah.ci.write 로 쓴다.
from __future__ import annotations

import argparse
import pathlib
import sys

from oah import ci
from oah.atomic import StorageError
from oah.config import ConfigError, load_config
from oah.paths import GitMissing, NotARepo, repo_info

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_ENVIRONMENT = 0, 1, 2, 69
COMMANDS = ("ci",)
USAGE_CODES = {"OAH_CI_OS_INVALID", "OAH_CI_TEST_COMMAND_INVALID"}
ENVIRONMENT_CODES = {"OAH_CI_TARGET_INVALID"}
GUIDE = "the oah CI starter guide (docs/ci-starter.md)"


class NotWritten(Exception):
    code = "OAH_CI_NOT_WRITTEN"


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("ci", help="set up GitHub Actions checks for this project")
    actions = parser.add_subparsers(dest="ci_command", metavar="<action>", required=True)
    init = actions.add_parser("init", help="write a starter workflow: tests on your OSes plus a secret scan",
                              description="Write .github/workflows/oah-ci.yml after showing it in full. "
                                          "Nothing is written until you confirm, or pass --yes.")
    init.add_argument("--os", help="comma-separated runners to test on: macos, windows, linux "
                                   "(default: this computer's OS)")
    init.add_argument("--test-command", help="the command that runs your tests (default: detected)")
    init.add_argument("--dry-run", action="store_true", help="show the workflow and write nothing")
    init.add_argument("--yes", action="store_true", help="accept the detected command and write without asking")
    init.add_argument("--force", action="store_true",
                      help="with --yes, replace an existing workflow file (its diff is still shown)")


def run(args: argparse.Namespace) -> int:
    try:
        return _init(args)
    except ci.CiError as exc:
        print(exc, file=sys.stderr)
        if exc.code in USAGE_CODES:
            return EXIT_USAGE
        return EXIT_ENVIRONMENT if exc.code in ENVIRONMENT_CODES else EXIT_REFUSED
    except NotWritten as exc:
        print(f"{NotWritten.code}: {exc} Nothing was written; run oah ci init again when you are ready.",
              file=sys.stderr)
        return EXIT_REFUSED
    except (ConfigError, NotARepo, GitMissing, StorageError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT


def _ask(question: str) -> str:
    """One answer line from stdin, stripped; end of input means no answer at all."""
    print(question, end=" ", flush=True)
    line = sys.stdin.readline()
    if not line:
        print()
        raise NotWritten("No answer was given (end of input).")
    return line.strip()


def _yes(question: str, *, default: bool) -> bool:
    answer = _ask(f"{question} [{'Y/n' if default else 'y/N'}]").lower()
    return default if not answer else answer in ("y", "yes")


def _settings(root: pathlib.Path) -> tuple[list[str], str | None]:
    """The [ci] config section, validated: (os names, test command or None)."""
    config = load_config(root)
    section = config.section("ci", ci.DEFAULTS)
    where = str(config.path) if config.path is not None else ".oah/config.toml"
    oses = section["os"]
    try:
        if any(not isinstance(name, str) for name in oses):
            raise ci.CiError("OAH_CI_OS_INVALID", "every entry must be a string")
        if oses:
            ci.parse_oses(oses)
        command = ci.check_test_command(section["test_command"]) if section["test_command"] else None
    except ci.CiError as exc:
        key = "ci.os" if exc.code == "OAH_CI_OS_INVALID" else "ci.test_command"
        raise ConfigError("OAH_CONFIG_INVALID", f"{where}: {key} is invalid ({str(exc).split(': ', 1)[1]}); "
                                                "fix it or remove it to use the default") from exc
    return list(oses), command


def _show_steps(steps: tuple[ci.Step, ...]) -> None:
    for step in steps:
        print(f"    {step.run}")


def _choose(root: pathlib.Path, args: argparse.Namespace) -> str | ci.Detection | object:
    """The test command for ci.plan: a Detection, a typed command, or ci.PLACEHOLDER."""
    found = ci.detect(root)
    interactive = not (args.yes or args.dry_run)
    if len(found) > 1:
        print("This project matches several kinds of tests:")
        for number, item in enumerate(found, 1):
            print(f"  {number}. {item.label}: {item.reason}")
            _show_steps(item.steps)
        if args.dry_run:
            print("(dry run: showing choice 1; a real run asks you to pick)")
            return found[0]
        if args.yes:
            raise ci.CiError("OAH_CI_AMBIGUOUS", "--yes cannot pick between "
                             f"{', '.join(item.label for item in found)}; rerun without --yes to pick one, "
                             "or pass the command with --test-command")
        answer = _ask(f"Which one should CI run? Type 1-{len(found)}:")
        if not answer.isdigit() or not 1 <= int(answer) <= len(found):
            raise NotWritten(f"{answer!r} is not one of 1-{len(found)}.")
        return found[int(answer) - 1]
    if found:
        item = found[0]
        print(f"Detected test command for {item.label} ({item.reason}):")
        _show_steps(item.steps)
        if not interactive or _yes("Use these steps?", default=True):
            return item
        prompt = "Type the command that runs your tests (or press Enter for a placeholder step to edit later):"
    else:
        print("oah could not find a test command (it looks for Python, Node.js, Go and Rust projects).")
        if not interactive:
            print("The workflow gets a placeholder step that fails until you edit it.")
            return ci.PLACEHOLDER
        prompt = "Type the command that runs your tests, or press Enter to leave a placeholder step to edit later:"
    typed = _ask(prompt)
    return typed if typed else ci.PLACEHOLDER


def _init(args: argparse.Namespace) -> int:
    root = repo_info(pathlib.Path.cwd()).root
    config_oses, config_command = _settings(root)
    oses = args.os.split(",") if args.os is not None else config_oses
    if args.os is not None:
        ci.parse_oses(oses)
    command = args.test_command if args.test_command is not None else config_command
    if command is not None:
        ci.check_test_command(command)
    else:
        command = _choose(root, args)
    plan = ci.plan(root, oses, command)

    state = "replaces the existing file" if plan.existing is not None else "new file"
    print(f"\n----- {ci.WORKFLOW_PATH} ({state}) -----")
    print(plan.text, end="")
    print(f"----- end of {ci.WORKFLOW_PATH} -----")
    if plan.placeholder:
        print("Note: the 'Run tests (edit me)' step fails on purpose until you put your test command in it.")
    if plan.up_to_date:
        print(f"{ci.WORKFLOW_PATH} is already up to date; nothing to do.")
        return EXIT_OK
    if plan.existing is not None:
        print(f"\nChanges to your existing {ci.WORKFLOW_PATH}:")
        print(plan.diff, end="")
    if args.dry_run:
        print("\ndry run: nothing was written.")
        return EXIT_OK

    overwrite = plan.existing is not None
    if args.yes:
        if overwrite and not args.force:
            raise ci.CiError("OAH_CI_EXISTS", f"{ci.WORKFLOW_PATH} already exists and --yes alone never "
                             "replaces it. Rerun without --yes to review the diff and answer, "
                             "or pass --force --yes to replace it")
    else:
        question = f"Replace {ci.WORKFLOW_PATH} with this version?" if overwrite else f"Write {ci.WORKFLOW_PATH}?"
        if not _yes(question, default=False):
            raise NotWritten("You did not confirm.")
    ci.write(plan, confirm=True, overwrite=overwrite)
    print(f"OAH_CI_WRITTEN path={ci.WORKFLOW_PATH}")
    print("Next: commit and push this file, then open the Actions tab of your repository on GitHub. "
          f"To make these checks required on your default branch, see {GUIDE}.")
    return EXIT_OK
