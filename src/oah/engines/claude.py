# Claude Code 어댑터 — --help 에서 모델 별칭·effort 목록을 읽고, claude -p 명령줄(리뷰는 읽기 전용)을 만들며, 결과 JSON 에서 관측된 값만 꺼낸다.
"""The Claude Code CLI adapter (PLAN 5.0, 5.5).

- `parse_help(text)` reads the `--model` option's quoted aliases and the `--effort` option's
  parenthesized level list from `claude --help`. A changed help text gives empty tuples, never a guess.
- `build_command(...)` is `claude -p --output-format json --model M --effort E --permission-mode <mode>
  --tools <list>`, with the prompt on stdin. `--tools` restricts which tools exist. A review
  (`read_only=True`) always runs in `plan` mode with `READ_ONLY_TOOLS`, whatever the config says.
- `observe(stdout_text)` reports the models named in `modelUsage` and an `effort` only when the result
  JSON carries one. Claude Code's JSON result does not report the inference effort, so runs record
  `effort_observable: false` and `unverified` until it does (A1, AC9).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Sequence

from oah.engines.effort import LABEL_RE, EffortResolution, EngineError, resolve

DEFAULT_PERMISSION_MODE = "acceptEdits"
PERMISSION_MODES = ("acceptEdits", "auto", "default", "dontAsk", "manual", "plan", "bypassPermissions")
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
DEFAULT_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")
_VERSION = re.compile(r"(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)")
_ALIAS = re.compile(r"'([A-Za-z0-9][A-Za-z0-9._-]*)'")
_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\([^,()]*\))?")


@dataclass(frozen=True)
class ClaudeHelp:
    aliases: tuple[str, ...]
    efforts: tuple[str, ...]


@dataclass(frozen=True)
class Observation:
    models: tuple[str, ...]
    effort: str | None
    effort_observable: bool


def parse_version(text: str) -> str | None:
    """The version number in `claude --version` output, such as 2.1.309."""
    match = _VERSION.search(text or "")
    return match.group(1) if match else None


def _option_block(lines: list[str], option: str) -> str:
    block: list[str] = []
    for line in lines:
        stripped = line.strip()
        if block and stripped.startswith("-"):
            break
        if block or stripped.startswith(option + " ") or stripped == option:
            block.append(stripped)
    return " ".join(block)


def parse_help(text: str) -> ClaudeHelp:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    aliases: list[str] = []
    for alias in _ALIAS.findall(_option_block(lines, "--model")):
        if alias not in aliases:
            aliases.append(alias)
    efforts: list[str] = []
    match = re.search(r"\(([^)]*)\)", _option_block(lines, "--effort"))
    for item in (match.group(1).split(",") if match else ()):
        label = item.strip()
        if LABEL_RE.fullmatch(label) and label not in efforts:
            efforts.append(label)
    return ClaudeHelp(aliases=tuple(aliases), efforts=tuple(efforts))


def resolve_effort(requested: str, *, efforts: Sequence[str] | None, model: str) -> EffortResolution:
    """The launch effort; levels come from the efforts `claude --help` lists, when it lists any."""
    return resolve(requested, tuple(efforts) if efforts else None, model=f"Claude model {model}")


def build_command(base: Sequence[str], *, model: str, effort: str, permission_mode: str, tools: Sequence[str],
                  read_only: bool) -> list[str]:
    """The argv for one headless Claude Code run; the prompt goes on stdin."""
    if not model or model.startswith("-"):
        raise EngineError("OAH_MODEL_UNRESOLVED", f"Claude model {model!r} is not a model name or alias; set the "
                          "tier's model or [engines.claude] quality_order in .oah/config.toml")
    if not LABEL_RE.fullmatch(effort or ""):
        raise EngineError("OAH_EFFORT_INVALID", f"effort {effort!r} is not a valid level name")
    if read_only:
        permission_mode, tools = "plan", READ_ONLY_TOOLS
    if permission_mode not in PERMISSION_MODES:
        raise EngineError("OAH_CONFIG_INVALID", f"[engines.claude] permission_mode {permission_mode!r} is not one "
                          f"of {', '.join(PERMISSION_MODES)}; the default is {DEFAULT_PERMISSION_MODE}")
    for tool in tools:
        if not _TOOL.fullmatch(tool):
            raise EngineError("OAH_CONFIG_INVALID", f"[engines.claude] tool {tool!r} is not a tool name such as "
                              "Read or Bash(git status:*); list one tool per entry")
    argv = [*base, "-p", "--output-format", "json", "--model", model, "--effort", effort,
            "--permission-mode", permission_mode]
    if tools:
        argv += ["--tools", ",".join(tools)]
    return argv


def observe(stdout_text: str) -> Observation:
    """What the result JSON shows about the run; absent values stay None."""
    data = None
    text = (stdout_text or "").strip()
    for candidate in (text, text.rsplit("\n", 1)[-1] if text else ""):
        try:
            data = json.loads(candidate)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        return Observation(models=(), effort=None, effort_observable=False)
    usage = data.get("modelUsage")
    models = tuple(name for name in usage if isinstance(name, str)) if isinstance(usage, dict) else ()
    effort = next((data[key] for key in ("effort", "reasoning_effort") if isinstance(data.get(key), str)), None)
    return Observation(models=models, effort=effort, effort_observable=effort is not None)
