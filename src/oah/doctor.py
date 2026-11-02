# oah doctor — 도구 목록의 필수·권장 도구가 설치됐는지 확인하고, 빠진 도구는 이 OS 의 공식 설치 명령으로 안내한다.
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from oah.config import ConfigError, load_config
from oah.engines.proc import Resolved, RunResult, UnsupportedBatchShim, resolve_command, run_command
from oah.setup.guide import signed_in
from oah.setup.tools import Tool, catalog, display, os_name_for
from oah.unlazy import find_unlazy_dir, unlazy_version

MIN_PYTHON = (3, 11)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

PLATFORM_LABELS = {"darwin": "macOS", "win32": "Windows"}
AGENT_EDITED, AGENT_STALE = "OAH_AGENT_FILE_EDITED", "OAH_AGENT_FILE_STALE"


@dataclass
class ToolStatus:
    name: str
    requirement: str  # "required" or "recommended"
    status: str  # ok, missing, too-old, not-signed-in, unsupported-shim or error
    version: str | None
    source: str | None
    note: str


Resolver = Callable[..., "Resolved | None"]
Runner = Callable[..., RunResult]


def first_line(text: str) -> str | None:
    for line in ANSI.sub("", text).splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return None


def python_status(version_info: Sequence[int]) -> ToolStatus:
    version = ".".join(str(part) for part in version_info[:3])
    if tuple(version_info[:2]) >= MIN_PYTHON:
        return ToolStatus("python", "required", "ok", version, sys.executable, "runs oah")
    return ToolStatus("python", "required", "too-old", version, sys.executable, "oah needs Python 3.11 or newer")


def gates_in_use(root: pathlib.Path | None) -> bool:
    """True when the project keeps gate ledgers (.oah/tasks/*/GATES.md) or has a [gates] table that
    does not say `enabled = false`. An unreadable config counts only through its ledgers."""
    if root is None:
        return False
    if any(path.is_file() for path in (root / ".oah" / "tasks").glob("*/GATES.md")):
        return True
    try:
        gates = load_config(root).raw.get("gates")
    except ConfigError:
        return False
    return isinstance(gates, Mapping) and gates.get("enabled") is not False


def _requirement(tool: Tool, gates: bool) -> tuple[str, str]:
    if tool.required_for_gates and gates and not tool.required:
        return "required", f"{tool.purpose}; required because this project uses gates"
    return tool.requirement, tool.purpose


def _check_tool(tool: Tool, *, search_path: str | None, timeout: float, resolver: Resolver,
                runner: Runner, gates: bool = False) -> ToolStatus:
    name = tool.name
    requirement, purpose = _requirement(tool, gates)
    try:
        resolved = resolver(tool.check[0], search_path)
    except UnsupportedBatchShim as exc:
        return ToolStatus(name, requirement, "unsupported-shim", None, exc.path, str(exc))
    if resolved is None:
        return ToolStatus(name, requirement, "missing", None, None, purpose)
    try:
        result = runner(resolved.argv + tool.version_args, timeout=timeout)
    except OSError as exc:
        return ToolStatus(name, requirement, "error", None, resolved.source,
                          f"{purpose}; --version could not launch: {exc}")
    if result.timed_out:
        return ToolStatus(name, requirement, "error", None, resolved.source,
                          f"{purpose}; --version timed out after {timeout:g}s")
    version = first_line(result.stdout) or first_line(result.stderr)
    if result.returncode != 0:
        return ToolStatus(name, requirement, "error", version, resolved.source,
                          f"{purpose}; --version exited with code {result.returncode}")
    if tool.min_version is not None:
        match = SEMVER.search(version or "")
        if match is None or tuple(int(part) for part in match.groups()) < tool.min_version:
            wanted = ".".join(str(part) for part in tool.min_version)
            return ToolStatus(name, requirement, "too-old", version, resolved.source,
                              f"{purpose}; needs {wanted} or newer")
    if tool.sign_in is not None:
        # Only the exit code and a loggedIn flag are read; the status text is never shown or kept.
        command = f"'{name} {' '.join(tool.sign_in.status_args)}'"
        try:
            login = runner(resolved.argv + tool.sign_in.status_args, timeout=timeout)
        except OSError as exc:
            return ToolStatus(name, requirement, "error", version, resolved.source,
                              f"{purpose}; {command} could not launch: {exc}")
        if login.timed_out:
            return ToolStatus(name, requirement, "error", version, resolved.source, f"{purpose}; {command} timed out")
        if not signed_in(login.returncode, login.stdout):
            return ToolStatus(name, requirement, "not-signed-in", version, resolved.source,
                              f"{purpose}; not signed in, run: {tool.sign_in.login}")
        return ToolStatus(name, requirement, "ok", version, resolved.source, f"{purpose}; signed in")
    return ToolStatus(name, requirement, "ok", version, resolved.source, purpose)


def _check_unlazy(tool: Tool, *, env: Mapping[str, str], home: pathlib.Path,
                  repo_root: pathlib.Path | None) -> ToolStatus:
    folder, reason = find_unlazy_dir(repo_root, env, home)
    if folder is None:
        reason = reason.partition(" (install: ")[0]  # install advice comes from the pinned catalog
        return ToolStatus(tool.name, tool.requirement, "missing", None, None, f"{tool.purpose}; {reason}")
    version = unlazy_version(folder) or "unknown version"
    return ToolStatus(tool.name, tool.requirement, "ok", version, str(folder), f"{tool.purpose}; {folder}")


def check_tools(*, search_path: str | None, env: Mapping[str, str], home: pathlib.Path,
                repo_root: pathlib.Path | None, platform: str, timeout: float = 10.0,
                resolver: Resolver = resolve_command, runner: Runner = run_command) -> list[ToolStatus]:
    """Check every catalog tool, in catalog order. `platform` only affects the install hints shown later.

    `repo_root` is the project: its skill folders are searched for unlazy, and when it uses gates the
    tools marked `required_for_gates` (Node) count as required.
    """
    gates = gates_in_use(repo_root)
    statuses = []
    for tool in catalog():
        if tool.detect == "python":
            statuses.append(python_status(sys.version_info))
        elif tool.detect == "unlazy":
            statuses.append(_check_unlazy(tool, env=env, home=home, repo_root=repo_root))
        else:
            statuses.append(_check_tool(tool, search_path=search_path, timeout=timeout, resolver=resolver,
                                        runner=runner, gates=gates))
    return statuses


def exit_code(statuses: Sequence[ToolStatus]) -> int:
    return 0 if all(s.status == "ok" for s in statuses if s.requirement == "required") else 1


def _install_texts(name: str, platform: str) -> tuple[list[str], str]:
    os_name = os_name_for(platform)
    for tool in catalog():
        if tool.name == name:
            commands = [display(argv) for argv in tool.install.get(os_name, ())]
            if not commands and os_name in tool.note:
                commands = [tool.note[os_name]]
            return commands, tool.url_for(os_name)
    raise KeyError(name)


def mcp_notices(root: pathlib.Path | None, env: Mapping[str, str]) -> list[dict]:
    """The last recorded MCP pin notice per catalog entry, read from the update log; no network."""
    if root is None:
        return []
    from oah.paths import GitMissing, NotARepo, repo_info, state_dir
    from oah.toolkit.catalog import CatalogError, load_catalog
    from oah.toolkit.update import last_mcp_notices

    try:
        return last_mcp_notices(state_dir(repo_info(root), env), load_catalog())
    except (NotARepo, GitMissing, CatalogError, OSError):
        return []


def model_notices(root: pathlib.Path | None, env: Mapping[str, str]) -> list[dict]:
    """Unpooled-model notices from the stored catalog and current config; no network or engine CLI calls."""
    if root is None:
        return []
    from oah.call_cli import load_settings
    from oah.models import Catalog
    from oah.paths import repo_info, state_dir
    from oah.routing import ENGINES

    settings = load_settings(root)
    return Catalog(state_dir(repo_info(root), env)).unpooled_notices(
        {engine: settings.engines[engine]["tier_pools"] for engine in ENGINES})


def model_choices(root: pathlib.Path | None, env: Mapping[str, str]) -> list[dict]:
    """What `oah models` shows, from the stored catalog and current config; no network or engine CLI calls."""
    if root is None:
        return []
    from oah import call
    from oah.call_cli import load_settings
    from oah.models import _NOTICE_FAILURES, Catalog
    from oah.paths import repo_info, state_dir
    from oah.routing import ENGINES

    from oah.benchmarks import Session

    settings = load_settings(root)
    catalog = Catalog(state_dir(repo_info(root), env))
    try:
        entries = {engine: catalog.load(engine) for engine in ENGINES}
    except _NOTICE_FAILURES:  # a damaged catalog must not stop an advisory report
        return []
    session = Session()  # one view of the stored score files for both engines
    return [call.stored_choices(engine, entry, settings, catalog=catalog, session=session)
            for engine, entry in entries.items() if entry is not None]


def _session_hook_installed(root: pathlib.Path) -> bool:
    """Whether oah's SessionStart hook is in the project's Claude Code settings, local or shared."""
    from oah import hooks

    for shared in (False, True):
        try:
            _, data = hooks._load_settings(hooks.settings_path(root, shared))
        except hooks.HookError:
            continue
        if hooks._claude_installed(data, "session-start"):
            return True
    return False


def agent_notices(root: pathlib.Path | None, env: Mapping[str, str]) -> list[dict]:
    """Subagent files oah generated that an edit, or a stale choice no hook will update, left behind.

    From the stored catalog and scores and the current config (`init.tier_agents`): no network, no engine CLI, and
    nothing is written."""
    if root is None:
        return []
    from oah import call, init, models, routing
    from oah.hook_session_start import _agent_file
    from oah.paths import repo_info, state_dir

    root = pathlib.Path(root)
    present = [tier for tier in routing.TIERS if _agent_file(root, tier) is not None]
    if not present:
        return []
    config = load_config(root)
    section = config.section("models", models.DEFAULTS)
    expected = init.tier_agents(config.section("engines", call.ENGINE_DEFAULTS), profile=section["profile"],
                                models_section=section, state_dir=state_dir(repo_info(root), env))
    hooked = _session_hook_installed(root)
    step = "delete it and run `oah init --with-agents` to have oah write it again"
    notices = []
    for tier in present:
        relative = f"{init.AGENTS_DIR}/oah-{tier}.md"
        data = (root / relative).read_bytes()
        found, update = init.agent_check(tier, data, expected.files[relative])
        if found == "edited":
            notices.append({"code": AGENT_EDITED, "file": relative,
                            "message": f"{AGENT_EDITED}: {relative} was edited, so it is left alone and oah no longer "
                                       f"keeps its model current. To hand it back to oah, {step}"})
        elif found == "stale" and not hooked:
            (old, old_effort), (new, effort) = init.agent_values(data), init.agent_values(update)
            notices.append({"code": AGENT_STALE, "file": relative,
                            "message": f"{AGENT_STALE}: {relative} names {call._shown(old)} at effort "
                                       f"{call._shown(old_effort)}, but oah now chooses {call._shown(new)} at effort "
                                       f"{call._shown(effort)}, and no session-start hook will update it. Install it "
                                       f"with `oah hooks install session-start`, or {step}"})
    return notices


def _mcp_lines(notices: Sequence[Mapping]) -> list[str]:
    if not notices:
        return []
    lines = ["", "MCP template pins with a newer npm version (from the last oah skills update; no network used):"]
    for notice in notices:
        checked = f" (checked {notice['checked']})" if notice.get("checked") else ""
        lines.append(f"  {notice['item']}: {notice['package']} {notice['pinned']} -> {notice['latest']}{checked}")
    lines.append("      you: change the version in your MCP config when you choose to; oah never edits it")
    lines.append("      kit maintainers: bump the mcp/ templates and the catalog entry")
    return lines


def _model_lines(notices: Sequence[Mapping]) -> list[str]:
    if not notices:
        return []
    lines = ["", "Listed models in no tier pool (from the stored catalog and current config; no network used):"]
    for notice in notices:
        checked = f" (checked {notice['date']})" if notice.get("date") else ""
        lines.append(f"  {notice['message']}{checked}")
    lines.append("      this includes every currently unpooled model, not only new ones")
    return lines


def _agent_lines(notices: Sequence[Mapping], error: str | None) -> list[str]:
    if not notices and error is None:
        return []
    lines = ["", "Subagent files (from the stored catalog, the stored or packaged scores and current config; "
             "no network used):"]
    lines += [f"  {notice['message']}" for notice in notices]
    if error is not None:
        lines.append(f"  The subagent files could not be checked ({error}); fix .oah/config.toml, then run oah doctor")
    return lines


def update_notices(root: pathlib.Path | None) -> list[dict]:
    """The tool-update switch when it is off by absence, and the last pass's unsettled results; reads only."""
    from oah.toolupdate import doctor_notices

    return doctor_notices(root)


def _update_lines(notices: Sequence[Mapping], error: str | None) -> list[str]:
    if not notices and error is None:
        return []
    lines = ["", "Tool updates (oah update status shows the last pass):"]
    lines += [f"  {notice['message']}" for notice in notices]
    if error is not None:
        lines.append(f"  The tool-update records could not be read ({error}); run oah update status")
    return lines


def _choice_lines(choices: Sequence[Mapping]) -> list[str]:
    if not choices:
        return []
    from oah.benchmarks import describe

    lines = ["", "Model choice (from the stored catalog, the stored or packaged scores and current config; "
             "no network used):"]
    for row in choices:
        scores = row["scores"]
        where = f"scores {scores['source']} {scores['date']}" if scores else "no usable scores"
        lines.append(f"  {row['engine']}: profile {row['profile']} · {where}")
        lines.append("      " + " · ".join(f"{tier} {item.get('model') or item['error'].split(':')[0]}"
                                           for tier, item in row["tiers"].items()))
        if row["left_out"]:
            lines.append("      left out: " + " · ".join(f"{name} ({why})" for name, why in row["left_out"].items()))
        if scores:
            lines.append(f"      scores from: {describe(scores)}")
            lines.append(f"      credit: {scores['credit']}")
        lines += [f"      {notice}" for notice in row.get("notices", ())]
    return lines


def render_text(statuses: Sequence[ToolStatus], platform: str, mcp_notices: Sequence[Mapping] = (),
                model_notices: Sequence[Mapping] = (), model_notices_error: str | None = None,
                model_choices: Sequence[Mapping] = (), agent_notices: Sequence[Mapping] = (),
                agent_notices_error: str | None = None, *, update_notices: Sequence[Mapping] = (),
                update_notices_error: str | None = None) -> str:
    label = PLATFORM_LABELS.get(platform, f"{platform}, not a supported OS")
    rows = [("TOOL", "STATUS", "VERSION", "NOTE")]
    rows += [(s.name, s.status, s.version or "-", s.note) for s in statuses]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = [f"oah doctor ({label})"]
    for row in rows:
        lines.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:3])) + "  " + row[3])
    fixes = [s for s in statuses if s.status in ("missing", "too-old")]
    if fixes:
        lines += ["", "Install the missing tools:"]
        for status in fixes:
            commands, url = _install_texts(status.name, platform)
            optional = "" if status.requirement == "required" else f" ({status.requirement})"
            alternatives = "".join(f"   (or: {command})" for command in commands[1:])
            lines.append(f"  {status.name}{optional}: {commands[0] if commands else 'see'}{alternatives}")
            lines.append(f"      {url}")
    lines += _mcp_lines(mcp_notices)
    lines += _model_lines(model_notices)
    lines += _choice_lines(model_choices)
    lines += _agent_lines(agent_notices, agent_notices_error)
    lines += _update_lines(update_notices, update_notices_error)
    if model_notices_error is not None:
        lines.append(f"Model notices could not be read ({model_notices_error}); run oah models to see why.")
    missing_required = sum(1 for s in statuses if s.requirement == "required" and s.status != "ok")
    lines += ["", "DOCTOR_OK" if missing_required == 0 else f"DOCTOR_MISSING required={missing_required}"]
    return "\n".join(lines)


def render_json(statuses: Sequence[ToolStatus], platform: str, mcp_notices: Sequence[Mapping] = (),
                model_notices: Sequence[Mapping] = (), model_notices_error: str | None = None,
                model_choices: Sequence[Mapping] = (), agent_notices: Sequence[Mapping] = (),
                agent_notices_error: str | None = None, *, update_notices: Sequence[Mapping] = (),
                update_notices_error: str | None = None) -> str:
    rows = []
    for status in statuses:
        commands, url = _install_texts(status.name, platform)
        rows.append(dict(dataclasses.asdict(status), install=commands, url=url))
    payload = {"ok": exit_code(statuses) == 0, "platform": platform, "tools": rows,
               "mcp_notices": [dict(notice) for notice in mcp_notices],
               "model_notices": [dict(notice) for notice in model_notices],
               "model_choices": [dict(row) for row in model_choices]}
    if model_notices_error is not None:
        payload["model_notices_error"] = model_notices_error
    if agent_notices:  # only when there is something to say, so the landed shape stays as it was
        payload["agent_notices"] = [dict(notice) for notice in agent_notices]
    if agent_notices_error is not None:
        payload["agent_notices_error"] = agent_notices_error
    if update_notices:  # as with agent notices, only when there is something to say
        payload["update_notices"] = [dict(notice) for notice in update_notices]
    if update_notices_error is not None:
        payload["update_notices_error"] = update_notices_error
    return json.dumps(payload, indent=2, ensure_ascii=True)


def _repo_root(search_path: str | None, cwd: pathlib.Path | None = None) -> pathlib.Path | None:
    try:
        git = resolve_command("git", search_path)
    except UnsupportedBatchShim:
        return None
    if git is None:
        return None
    try:
        result = run_command(git.argv + ("rev-parse", "--show-toplevel"), timeout=10,
                             cwd=str(cwd) if cwd is not None else None)
    except OSError:
        return None
    top = result.stdout.strip()
    return pathlib.Path(top) if result.returncode == 0 and top else None


def project_root(search_path: str | None, *, cwd: pathlib.Path | None = None) -> pathlib.Path | None:
    """The git top level; without git, the nearest folder upwards that holds .oah or .git."""
    here = (pathlib.Path.cwd() if cwd is None else cwd).resolve()
    top = _repo_root(search_path, here)
    if top is not None:
        return top
    for folder in (here, *here.parents):
        if (folder / ".oah").is_dir() or (folder / ".git").exists():
            return folder
    return None


def doctor_main(*, json_output: bool) -> int:
    """`oah doctor` against the real machine."""
    root = project_root(None)
    statuses = check_tools(search_path=None, env=os.environ, home=pathlib.Path.home(),
                           repo_root=root, platform=sys.platform)
    mcp = mcp_notices(root, os.environ)
    model_error = None
    try:
        models = model_notices(root, os.environ)
        choices = model_choices(root, os.environ)
    except Exception as exc:
        models, choices = [], []
        model_error = type(exc).__name__
    agent_error = None
    try:
        agents = agent_notices(root, os.environ)
    except Exception as exc:  # an advisory check must not stop the report
        agents, agent_error = [], type(exc).__name__
    update_error = None
    try:
        updates = update_notices(root)
    except Exception as exc:  # the same for the tool-update records
        updates, update_error = [], type(exc).__name__
    render = render_json if json_output else render_text
    print(render(statuses, sys.platform, mcp, models, model_error, choices, agents, agent_error,
                 update_notices=updates, update_notices_error=update_error))
    return exit_code(statuses)
