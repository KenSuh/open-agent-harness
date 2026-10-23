# CI 워크플로 계획·쓰기 — 템플릿을 채워 CiPlan 을 만들고, 확인을 받은 계획만 원자적으로 쓴다. 기존 파일은 보여 준 그대로일 때만 바꾼다.
from __future__ import annotations

import difflib
import importlib.resources
import os
import pathlib
import sys
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from oah.atomic import write_text_atomic
from oah.ci.detect import Detection, Step, detect

WORKFLOW_PATH = pathlib.PurePosixPath(".github/workflows/oah-ci.yml")
DEFAULTS = {"os": [], "test_command": ""}
RUNNERS = {"macos": "macos-latest", "windows": "windows-latest", "linux": "ubuntu-latest"}
SETUP_TEMPLATES = {"python": "setup-python.yml", "node": "setup-node.yml", "go": "setup-go.yml"}


class CiError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class CiPlan:
    root: pathlib.Path
    path: pathlib.Path
    oses: tuple[str, ...]
    detection: Detection | None
    steps: tuple[Step, ...]
    placeholder: bool
    text: str
    existing: str | None

    @property
    def up_to_date(self) -> bool:
        return self.existing is not None and _lf(self.existing) == self.text

    @property
    def diff(self) -> str:
        """A unified diff from the current file to the new text; empty for a new or unchanged file."""
        if self.existing is None:
            return ""
        lines = difflib.unified_diff(_lf(self.existing).splitlines(keepends=True),
                                     self.text.splitlines(keepends=True),
                                     fromfile=f"{WORKFLOW_PATH} (current)", tofile=f"{WORKFLOW_PATH} (new)")
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def current_os(platform: str = sys.platform) -> str:
    if platform == "darwin":
        return "macos"
    if platform.startswith(("win32", "cygwin", "msys")):
        return "windows"
    return "linux"


def parse_oses(values: Iterable[str]) -> tuple[str, ...]:
    """Validated OS names, lower-cased and de-duplicated, in the order given."""
    oses: list[str] = []
    for value in values:
        name = value.strip().lower() if isinstance(value, str) else value
        if name not in RUNNERS:
            raise CiError("OAH_CI_OS_INVALID", f"unknown OS {value!r}; choose from {', '.join(RUNNERS)}, "
                                               "for example --os macos,windows")
        if name not in oses:
            oses.append(name)
    if not oses:
        raise CiError("OAH_CI_OS_INVALID", f"no OS given; choose from {', '.join(RUNNERS)}, "
                                           "for example --os macos,windows")
    return tuple(oses)


def check_test_command(command: str) -> str:
    """The command without surrounding spaces; refuses what cannot sit on one literal YAML line."""
    command = command.strip()
    hint = "give one command on one line, for example --test-command \"make test\""
    if not command:
        raise CiError("OAH_CI_TEST_COMMAND_INVALID", f"the test command is empty; {hint}")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in command):
        raise CiError("OAH_CI_TEST_COMMAND_INVALID",
                      f"the test command contains a line break or control character; {hint}")
    if "${{" in command:
        raise CiError("OAH_CI_TEST_COMMAND_INVALID",
                      "the test command contains a GitHub expression (${{ ... }}), which could pull secrets "
                      f"into the workflow; {hint}")
    return command


def _template(name: str) -> str:
    return importlib.resources.files("oah").joinpath(f"templates/ci/{name}").read_text(encoding="utf-8")


def _step_block(step: Step) -> str:
    return f"      - name: {step.name}\n        run: |\n          {step.run}\n"


def _fill(text: str, marker: str, blocks: list[str]) -> str:
    """Replace the line holding @@marker@@ with the blocks, each followed by a blank line."""
    line = f"@@{marker}@@\n"
    if text.count(line) != 1:
        raise RuntimeError(f"CI template must hold exactly one {line.strip()} line")
    return text.replace(line, "".join(block + "\n" for block in blocks))


def render(oses: tuple[str, ...], setups: Iterable[str], steps: tuple[Step, ...]) -> str:
    """The workflow text (LF); no steps means the failing placeholder step."""
    text = _template("oah-ci.yml").replace("@@RUNNERS@@", ", ".join(RUNNERS[name] for name in oses))
    text = _fill(text, "SETUP_STEPS", [_template(SETUP_TEMPLATES[name]) for name in setups
                                       if name in SETUP_TEMPLATES])
    text = _fill(text, "TEST_STEPS", [_step_block(step) for step in steps] if steps
                 else [_template("placeholder.yml")])
    if "@@" in text:
        raise RuntimeError("CI template has an unfilled @@ marker")
    return text


def _target(root: pathlib.Path) -> pathlib.Path:
    """The workflow path; refuses a target that is a link, not a file, or resolves outside the project."""
    path = root.joinpath(*WORKFLOW_PATH.parts)
    hint = "move it away or replace it with a regular file, then run oah ci init again"
    for depth in range(1, len(WORKFLOW_PATH.parts) + 1):
        part = root.joinpath(*WORKFLOW_PATH.parts[:depth])
        if part.is_symlink() or _is_junction(part):
            raise CiError("OAH_CI_TARGET_INVALID", f"{part} is a link; oah writes only real files; {hint}")
    if path.resolve() != root.resolve().joinpath(*WORKFLOW_PATH.parts):
        raise CiError("OAH_CI_TARGET_INVALID", f"{path} resolves outside {root}; {hint}")
    if path.exists() and not path.is_file():
        raise CiError("OAH_CI_TARGET_INVALID", f"{path} exists but is not a file; {hint}")
    return path


def _is_junction(path: pathlib.Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _read_existing(path: pathlib.Path) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CiError("OAH_CI_TARGET_INVALID", f"cannot read {path} ({exc.strerror or exc}); "
                                               "check its permissions, then run oah ci init again") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CiError("OAH_CI_TARGET_INVALID", f"{path} is not UTF-8 text (byte {exc.start}); "
                                               "move it away or save it as UTF-8, then run oah ci init again") from exc


def plan(root: pathlib.Path, oses: Iterable[str] | None = None,
         test_command: str | Detection | None = None) -> CiPlan:
    """What `oah ci init` would write. Reads the project; writes nothing.

    `oses` empty or None means the current OS. `test_command` None uses the only detected ecosystem
    (the placeholder step when none is found; OAH_CI_AMBIGUOUS when several are). A Detection uses
    that one; a string becomes one "Run tests" step after the setup of every detected ecosystem.
    """
    root = pathlib.Path(root)
    chosen = parse_oses(oses) if oses else (current_os(),)
    path = _target(root)
    existing = _read_existing(path)
    detection: Detection | None = None
    if isinstance(test_command, str):
        steps = (Step("Run tests", check_test_command(test_command)),)
        setups = [found.ecosystem for found in detect(root)]
    else:
        if test_command is None:
            found = detect(root)
            if len(found) > 1:
                raise CiError("OAH_CI_AMBIGUOUS",
                              f"this project matches several ecosystems ({', '.join(item.label for item in found)}); "
                              "pick one, or pass the command with --test-command")
            test_command = found[0] if found else None
        detection = test_command
        steps = detection.steps if detection else ()
        setups = [detection.ecosystem] if detection else []
    return CiPlan(root=root, path=path, oses=chosen, detection=detection, steps=steps, placeholder=not steps,
                  text=render(chosen, setups, steps), existing=existing)


def write(plan: CiPlan, *, confirm: bool, overwrite: bool = False) -> pathlib.Path:
    """Write the planned workflow once the user confirmed it.

    The file on disk must still be what the plan showed: OAH_CI_CHANGED otherwise. An existing file is
    replaced only with overwrite=True, keeping CRLF line endings when it used them.
    """
    if not confirm:
        raise CiError("OAH_CI_NOT_CONFIRMED", f"nothing was written to {plan.path}; "
                                              "confirm the workflow first (answer y, or pass --yes)")
    path = _target(plan.root)
    current = _read_existing(path)
    if current != plan.existing:
        raise CiError("OAH_CI_CHANGED", f"{path} changed after it was shown; nothing was written. "
                                        "Run oah ci init again to see the new diff")
    if current is not None and not overwrite:
        raise CiError("OAH_CI_EXISTS", f"{path} already exists; nothing was written. Run oah ci init "
                                       "without --yes to see the diff and confirm, or pass --force --yes")
    text = plan.text.replace("\n", "\r\n") if current is not None and "\r\n" in current else plan.text
    write_text_atomic(path, text)
    return path
