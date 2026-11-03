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

from oah.toolupdate import probes, records, verdict
from oah.toolupdate.probes import Cli

RESTART_NOTE = verdict.RESTART_NOTE
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


def _entry(context: Context, evidence: verdict.ComponentEvidence) -> dict:
    status, _reason, next_step = verdict.decide(evidence)

    def version(item: verdict.Evidence | None) -> str | None:
        return item.value if item is not None and item.state == "confirmed" and isinstance(item.value, str) else None

    return records.component(status, version(evidence.before), version(evidence.after), next_step, context.checked_at)


def missing(context: Context) -> dict:
    return _entry(context, verdict.ComponentEvidence(kind="cli", command="oah update",
        cli=verdict.unknown("this step recorded no result; run oah update to check it again")))


def _support(checks) -> verdict.Evidence:
    for check in checks:
        evidence = check()
        if evidence.state != "confirmed":
            return evidence
    return verdict.confirmed(True)


def cli_update(context: Context, cli: Cli) -> dict:
    """Steps 1 and 2: `<cli> update`, verified by reading `<cli> --version` before and after."""
    command = f"{cli.name} update"
    evidence = verdict.ComponentEvidence(kind="cli", command=command, cli=cli.resolution,
        unsupported_step=f"this {cli.label} has no `{command}`; update it the way you installed it")
    if cli.resolution.state != "confirmed":
        return {cli.name: _entry(context, evidence)}
    before = cli.version()
    evidence = dataclasses.replace(evidence, before=before)
    if before.timed_out:
        return {cli.name: _entry(context, evidence)}
    support = cli.lists("update")
    evidence = dataclasses.replace(evidence, help=support)
    if support.state != "confirmed":
        return {cli.name: _entry(context, evidence)}
    ran = cli.run("update")
    after = cli.version()
    said_current = any(probes.said(name, ran.text) for name in probes.CURRENT_PHRASES[cli.name])
    evidence = dataclasses.replace(evidence, after=after, attempted=True,
        update=probes.update_evidence(ran, before, after, said_current=said_current))
    return {cli.name: _entry(context, evidence)}


def claude_marketplaces(context: Context) -> dict:
    """Refresh marketplaces as a separate component, verified from their installed Git revisions."""
    cli = context.claude
    command = "claude plugin marketplace update"
    evidence = verdict.ComponentEvidence(kind="marketplace", command=command, cli=cli.resolution,
        unsupported_step="this Claude Code has no `claude plugin marketplace update`; refresh them in Claude Code")
    if cli.resolution.state != "confirmed":
        return {"claude-marketplaces": _entry(context, evidence)}
    support = _support((lambda: cli.lists("plugin"), lambda: cli.lists("plugin", "marketplace"),
                        lambda: cli.lists("plugin", "marketplace", "update")))
    evidence = dataclasses.replace(evidence, help=support)
    if support.state != "confirmed":
        return {"claude-marketplaces": _entry(context, evidence)}

    def revision() -> verdict.Evidence:
        version = probes.claude_marketplace_version(context.env)
        return verdict.confirmed(version) if version is not None else verdict.unknown("marketplace revision is unreadable")

    before = revision()
    ran = cli.run("plugin", "marketplace", "update")
    after = revision()
    evidence = dataclasses.replace(evidence, before=before, after=after, attempted=True,
        update=probes.update_evidence(ran, before, after,
                                      said_current=probes.said("claude-marketplaces-current", ran.text)))
    return {"claude-marketplaces": _entry(context, evidence)}


def _inventory(cli: Cli) -> verdict.Evidence:
    listing = cli.run("plugin", "list", "--json")
    problem = probes.read_problem(listing, "plugin inventory version check")
    if problem is not None:
        return problem
    plugins = probes.user_plugins(listing.stdout)
    if plugins is None or any(version is None for version in plugins.values()):
        return verdict.unknown("plugin inventory is unreadable; run claude plugin list yourself")
    return verdict.confirmed(plugins)


def _plugin_version(inventory: verdict.Evidence) -> verdict.Evidence:
    if inventory.state != "confirmed":
        return inventory
    return verdict.confirmed("sha256:" + hashlib.sha256(
        json.dumps(inventory.value, sort_keys=True).encode("utf-8")).hexdigest())


def claude_plugins(context: Context) -> dict:
    """Update user-scope plugins and record both the fixed group and each discovered plugin."""
    cli = context.claude
    evidence = verdict.ComponentEvidence(kind="plugins", command="claude plugin list", cli=cli.resolution,
        unsupported_step="this Claude Code cannot update plugins with --json and --scope; "
                         "update them in Claude Code with /plugin")
    if cli.resolution.state != "confirmed":
        return {"claude-plugins": _entry(context, evidence)}
    needed = (lambda: cli.lists("plugin"), lambda: cli.lists("plugin", "list"),
              lambda: cli.lists("plugin", "update"), lambda: cli.has_flag(("plugin", "list"), "--json"),
              lambda: cli.has_flag(("plugin", "update"), "--json"),
              lambda: cli.has_flag(("plugin", "update"), "--scope"))
    support = _support(needed)
    evidence = dataclasses.replace(evidence, help=support)
    if support.state != "confirmed":
        return {"claude-plugins": _entry(context, evidence)}
    before = _inventory(cli)
    if before.state == "confirmed" and not before.value:
        before = verdict.absent("no Claude Code plugins installed")
    evidence = dataclasses.replace(evidence, before=_plugin_version(before), inventory=before)
    if before.state != "confirmed":
        return {"claude-plugins": _entry(context, evidence)}
    results: dict[str, dict] = {}
    answers = {ident: cli.run("plugin", "update", ident, "--scope", "user", "--json") for ident in sorted(before.value)}
    after = _inventory(cli)
    inventory = verdict.combine((before, after), (before.value, after.value))
    if after.state == "confirmed" and before.value.keys() != after.value.keys():
        inventory = verdict.unknown("plugin inventory changed membership; run claude plugin list yourself")
    updates = []
    for ident, ran in answers.items():
        result = probes.plugin_result(ran.stdout) if ran.clean else None
        old = verdict.confirmed(before.value[ident])
        new = (verdict.confirmed(after.value[ident]) if after.state == "confirmed" and ident in after.value
               else after if after.state == "unknown" else verdict.unknown("plugin version is absent after the update"))
        command = f"claude plugin update {ident}"
        update = probes.update_evidence(ran, old, new, said_current=probes.plugin_said_current(result),
                                        confirmation=probes.plugin_needs_confirmation(result, ran.text))
        updates.append(update)
        item = verdict.ComponentEvidence(kind="plugin", command=command, cli=cli.resolution, help=support,
                                          before=old, after=new, update=update, inventory=inventory, attempted=True)
        results[f"claude-plugin:{ident}"] = _entry(context, item)
    answer = (verdict.CONFIRMATION if any(item.value == verdict.CONFIRMATION for item in updates) else
              verdict.CHANGED if any(item.value == verdict.CHANGED for item in updates) else verdict.UP_TO_DATE)
    pairs = tuple(zip(answers, updates))
    target = next((ident for ident, item in pairs if item.timed_out), None)
    target = target or next((ident for ident, item in pairs if item.state == "unknown"), None)
    target = target or next((ident for ident, item in pairs if item.value == verdict.CONFIRMATION), None)
    target = target or next(iter(answers), None)
    evidence = dataclasses.replace(evidence, after=_plugin_version(after), inventory=inventory, attempted=bool(answers),
        command=f"claude plugin update {target}" if target else evidence.command,
        update=verdict.combine(tuple(updates), answer))
    results["claude-plugins"] = _entry(context, evidence)
    return results
