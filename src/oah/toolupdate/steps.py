# 도구 업데이트 단계 — Claude Code·Codex 본체, Claude Code 마켓플레이스·플러그인을 정해진 순서로 갱신하고 결과를 확인한다.
"""The steps of one pass, in order. Each returns {component name: entry} through `records.component`.

1. `claude update`; 2. `codex update`; 3. `claude plugin marketplace update`, then
`claude plugin update <id> --scope user --json` for each plugin installed at user scope.
Codex plugins and oah's independent daily skills check are only notices.
Nothing else is run: no package manager, no shell, no confirmation flag, nothing about MCP servers. Only a
command started by the shared runner can be stopped on timeout; unrelated processes are never listed, signalled or stopped.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Mapping

from oah.toolupdate import probes, records
from oah.toolupdate.probes import Cli

RESTART_NOTE = ("sessions already running keep the version and plugins they started with; restart them to use "
                "the new ones")
CODEX_PLUGINS_NOTE = ("Codex plugins are not updated automatically: update them with codex plugin marketplace "
                      "upgrade, then approve their hooks again in /hooks")
SKILLS_NOTE = "oah's skills keep their own daily check (oah skills update)"
LABELS = {"claude": "Claude Code", "codex": "Codex",
          "claude-plugins": "Claude Code plugins", "claude-marketplaces": "Claude Code marketplaces",
          "claude-plugin": "Claude Code plugin"}


def label(name: str) -> str:
    kind, _, item = name.partition(":")
    return f"{LABELS[kind]} {item}" if item else LABELS[kind]


@dataclasses.dataclass
class Context:
    env: Mapping[str, str]
    checked_at: str
    claude: Cli
    codex: Cli


def _entry(context: Context, status: str, before: str | None, after: str | None, step: str) -> dict:
    next_step = RESTART_NOTE if status == "updated" else "" if status == "current" else step
    return records.component(status, before, after, next_step, context.checked_at)


def _steps_for(command: str, *, timed_out: bool = False) -> dict[str, str]:
    return {"unverified": f"run {command} yourself and read what it says",
            "failed": (f"timed out; the tool may be partly updated; run {command} yourself to check it"
                       if timed_out else f"run {command} yourself to see the error")}


def _capability_problem(context: Context, support: probes.Capability, before: str | None,
                        unsupported_step: str) -> dict | None:
    if support.listed is True:
        return None
    status = "unsupported" if support.listed is False else "failed" if support.timed_out else "unverified"
    return _entry(context, status, before, before, support.reason or unsupported_step)


def cli_update(context: Context, cli: Cli) -> dict:
    """Steps 1 and 2: `<cli> update`, verified by reading `<cli> --version` before and after."""
    command = f"{cli.name} update"
    if cli.base is None:
        return {cli.name: _entry(context, "unsupported", None, None, cli.problem)}
    before = cli.version()
    problem = _capability_problem(context, cli.lists("update"), before,
                                  f"this {cli.label} has no `{command}`; update it the way you installed it")
    if problem is not None:
        return {cli.name: problem}
    ran = cli.run("update")
    after = cli.version()
    said_current = any(probes.said(name, ran.text) for name in probes.CURRENT_PHRASES[cli.name])
    status = probes.verdict(before, after, said_current=said_current, returncode=ran.returncode,
                            timed_out=ran.timed_out, clean=ran.clean)
    return {cli.name: _entry(context, status, before, after,
                             _steps_for(command, timed_out=ran.timed_out).get(status, ""))}


def claude_marketplaces(context: Context) -> dict:
    """Refresh marketplaces as a separate component, verified from their installed Git revisions."""
    cli = context.claude
    command = "claude plugin marketplace update"
    if cli.base is None:
        return {"claude-marketplaces": _entry(context, "unsupported", None, None, cli.problem)}
    for path in (("plugin",), ("plugin", "marketplace"), ("plugin", "marketplace", "update")):
        problem = _capability_problem(context, cli.lists(*path), None,
            "this Claude Code has no `claude plugin marketplace update`; refresh them in Claude Code")
        if problem is not None:
            return {"claude-marketplaces": problem}
    before = probes.claude_marketplace_version(context.env)
    ran = cli.run("plugin", "marketplace", "update")
    after = probes.claude_marketplace_version(context.env)
    status = probes.verdict(before, after, said_current=probes.said("claude-marketplaces-current", ran.text),
                            returncode=ran.returncode, timed_out=ran.timed_out, clean=ran.clean)
    return {"claude-marketplaces": _entry(context, status, before, after,
            _steps_for(command, timed_out=ran.timed_out).get(status, ""))}


def _plugin_version(plugins: dict[str, str | None] | None) -> str | None:
    if plugins is None or any(version is None for version in plugins.values()):
        return None
    return "sha256:" + hashlib.sha256(json.dumps(plugins, sort_keys=True).encode("utf-8")).hexdigest()


def claude_plugins(context: Context) -> dict:
    """Update user-scope plugins and record both the fixed group and each discovered plugin."""
    cli = context.claude
    if cli.base is None:
        return {"claude-plugins": _entry(context, "unsupported", None, None, cli.problem)}
    needed = (lambda: cli.lists("plugin"), lambda: cli.lists("plugin", "list"),
              lambda: cli.lists("plugin", "update"), lambda: cli.has_flag(("plugin", "list"), "--json"),
              lambda: cli.has_flag(("plugin", "update"), "--json"),
              lambda: cli.has_flag(("plugin", "update"), "--scope"))
    for probe in needed:
        problem = _capability_problem(context, probe(), None,
            "this Claude Code cannot update plugins with --json and --scope; update them in Claude Code with /plugin")
        if problem is not None:
            return {"claude-plugins": problem}
    listing = cli.run("plugin", "list", "--json")
    before = probes.user_plugins(listing.stdout) if listing.ok else None
    if before is None:
        status = probes.verdict(None, None, said_current=False, returncode=listing.returncode,
                                timed_out=listing.timed_out, clean=listing.clean)
        return {"claude-plugins": _entry(context, status, None, None,
                                         _steps_for("claude plugin list", timed_out=listing.timed_out)[status])}
    if not before:
        return {"claude-plugins": _entry(context, "unsupported", None, None,
                                         "no Claude Code plugins are installed at user scope")}
    results: dict[str, dict] = {}
    answers = {ident: cli.run("plugin", "update", ident, "--scope", "user", "--json") for ident in sorted(before)}
    listing = cli.run("plugin", "list", "--json")
    after = probes.user_plugins(listing.stdout) if listing.ok else None
    for ident, ran in answers.items():
        result = probes.plugin_result(ran.stdout) if ran.clean else None
        old, new = before[ident], (after or {}).get(ident)
        command = f"claude plugin update {ident}"
        if not ran.timed_out and probes.plugin_needs_confirmation(result, ran.text) and not (old and new and old != new):
            entry = _entry(context, "needs-confirmation", old, new, f"run: {command}")
        else:
            status = probes.verdict(old, new, said_current=probes.plugin_said_current(result),
                                    returncode=ran.returncode, timed_out=ran.timed_out, clean=ran.clean)
            entry = _entry(context, status, old, new, _steps_for(command, timed_out=ran.timed_out).get(status, ""))
        results[f"claude-plugin:{ident}"] = entry
    statuses = {entry["status"] for entry in results.values()}
    old, new = _plugin_version(before), _plugin_version(after)
    status = probes.verdict(old, new, said_current=statuses == {"current"}, returncode=listing.returncode,
                            timed_out=listing.timed_out, clean=listing.clean)
    if not statuses <= set(records.SETTLED) and status in records.SETTLED:
        status = "unverified"
    results["claude-plugins"] = _entry(context, status, old, new,
        _steps_for("claude plugin list", timed_out=listing.timed_out).get(status, ""))
    return results
