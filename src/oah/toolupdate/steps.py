# 도구 업데이트 단계 — Claude Code·Codex 본체, Claude Code 플러그인, Codex 마켓플레이스·플러그인, oah 키트 스킬을 정해진 순서로 갱신하고 결과를 확인한다.
"""The steps of one pass, in order. Each returns {component name: entry} through `records.component`.

1. `claude update`; 2. `codex update`; 3. `claude plugin marketplace update`, then
`claude plugin update <id> --scope user --json` for each plugin installed at user scope; 4. `codex plugin
marketplace upgrade`, with each Git marketplace's revision and each installed Codex plugin's file digest read
before and after; 5. oah's kit skills through `toolkit.update.scheduled_update`. Nothing else is run: no
package manager, no shell, no confirmation flag, nothing about MCP servers, no process listing or signal.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import pathlib
from typing import Callable, Mapping

from oah.toolupdate import probes, records
from oah.toolupdate.probes import Cli

RESTART_NOTE = ("sessions already running keep the version and plugins they started with; restart them to use "
                "the new ones")
HOOK_STEP = "open Codex and approve its hooks again in /hooks"
LABELS = {"claude": "Claude Code", "codex": "Codex", "oah-skills": "oah kit skills",
          "claude-plugins": "Claude Code plugins", "claude-marketplaces": "Claude Code marketplaces",
          "codex-marketplaces": "Codex marketplaces", "claude-plugin": "Claude Code plugin",
          "codex-plugin": "Codex plugin", "codex-marketplace": "Codex marketplace"}


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


def _steps_for(command: str) -> dict[str, str]:
    return {"unverified": f"run {command} yourself and read what it says",
            "failed": f"run {command} yourself to see the error"}


def cli_update(context: Context, cli: Cli) -> dict:
    """Steps 1 and 2: `<cli> update`, verified by reading `<cli> --version` before and after."""
    command = f"{cli.name} update"
    if cli.base is None:
        return {cli.name: _entry(context, "unsupported", None, None, cli.problem)}
    before = cli.version()
    if not cli.lists("update"):
        return {cli.name: _entry(context, "unsupported", before, before,
                                 f"this {cli.label} has no `{command}`; update it the way you installed it")}
    ran = cli.run("update")
    after = cli.version()
    said_current = any(probes.said(name, ran.text) for name in probes.CURRENT_PHRASES[cli.name])
    status = probes.verdict(before, after, said_current=said_current, returncode=ran.returncode,
                            timed_out=ran.timed_out)
    return {cli.name: _entry(context, status, before, after, _steps_for(command).get(status, ""))}


def claude_plugins(context: Context) -> dict:
    """Step 3: refresh the marketplaces, then update each user-scope plugin, verified from the plugin list."""
    cli = context.claude
    if cli.base is None:
        return {}
    needed = (cli.lists("plugin"), cli.lists("plugin", "list"), cli.lists("plugin", "update"),
              cli.has_flag(("plugin", "list"), "--json"), cli.has_flag(("plugin", "update"), "--json"),
              cli.has_flag(("plugin", "update"), "--scope"))
    if not all(needed):
        return {"claude-plugins": _entry(context, "unsupported", None, None,
                                         "this Claude Code cannot update plugins with --json; update them in Claude "
                                         "Code with /plugin")}
    listing = cli.run("plugin", "list", "--json")
    before = probes.user_plugins(listing.stdout) if listing.ok else None
    if before is None:
        status = "failed" if listing.timed_out or listing.returncode != 0 else "unverified"
        return {"claude-plugins": _entry(context, status, None, None, "run claude plugin list yourself to see why "
                                                                        "the plugin list cannot be read")}
    results: dict[str, dict] = {}
    if cli.lists("plugin", "marketplace", "update"):
        refresh = cli.run("plugin", "marketplace", "update")
        if not refresh.ok:
            status = "failed" if refresh.timed_out or refresh.returncode != 0 else "unverified"
            results["claude-marketplaces"] = _entry(context, status, None, None, _steps_for(
                "claude plugin marketplace update")[status])
    else:
        results["claude-marketplaces"] = _entry(context, "unsupported", None, None, "this Claude Code has no "
                                                "`claude plugin marketplace update`; refresh them in Claude Code")
    answers = {ident: cli.run("plugin", "update", ident, "--scope", "user", "--json") for ident in sorted(before)}
    listing = cli.run("plugin", "list", "--json")
    after = (probes.user_plugins(listing.stdout) if listing.ok else None) or {}
    for ident, ran in answers.items():
        result = probes.plugin_result(ran.stdout) if ran.clean else None
        old, new = before[ident], after.get(ident)
        command = f"claude plugin update {ident}"
        if probes.plugin_needs_confirmation(result, ran.text) and not (old and new and old != new):
            entry = _entry(context, "needs-confirmation", old, new, f"run: {command}")
        else:
            status = probes.verdict(old, new, said_current=probes.plugin_said_current(result),
                                    returncode=ran.returncode, timed_out=ran.timed_out)
            entry = _entry(context, status, old, new, _steps_for(command).get(status, ""))
        results[f"claude-plugin:{ident}"] = entry
    return results


def _short(digest: str | None) -> str | None:
    return digest.removeprefix("sha256:")[:12] if digest else None


def codex_plugins(context: Context) -> dict:
    """Step 4: `codex plugin marketplace upgrade`, verified from Codex's records and its plugins' files."""
    cli = context.codex
    if cli.base is None:
        return {}
    home = probes.codex_home(context.env)
    try:
        before = probes.read_codex(home)
    except probes.CodexUnreadable:
        return {"codex-marketplaces": _entry(context, "unverified", None, None,
                                             "Codex's config.toml cannot be read; fix it, then run oah update")}
    if not before.marketplaces:
        return {}  # nothing configured to upgrade
    if not (cli.lists("plugin") and cli.lists("plugin", "marketplace") and cli.lists("plugin", "marketplace",
                                                                                     "upgrade")):
        return {"codex-marketplaces": _entry(context, "unsupported", None, None, "this Codex has no `codex plugin "
                                             "marketplace upgrade`; update Codex first")}
    digests = {ident: probes.plugin_digest(folder) for ident, folder in before.plugins.items()}
    ran = cli.run("plugin", "marketplace", "upgrade")
    try:
        after = probes.read_codex(home)
    except probes.CodexUnreadable:
        after = probes.CodexView({}, before.plugins)
    results: dict[str, dict] = {}
    command = "codex plugin marketplace upgrade"
    for name in sorted(before.marketplaces):
        old, new = before.marketplaces[name], after.marketplaces.get(name)
        status = probes.verdict(old, new, said_current=probes.marketplace_said_current(ran.text, name),
                                returncode=ran.returncode, timed_out=ran.timed_out)
        results[f"codex-marketplace:{name}"] = _entry(context, status, old, new, _steps_for(command).get(status, ""))
    for ident, folder in sorted(after.plugins.items()):
        old, new = digests.get(ident), probes.plugin_digest(folder)
        if old is not None and old == new:
            continue  # the plugin's files did not change
        if old is None or new is None:
            entry = _entry(context, "unverified", _short(old), _short(new),
                           f"its folder under CODEX_HOME could not be read; open Codex and check {ident} in /plugins")
        elif probes.declares_hooks(folder):
            entry = _entry(context, "needs-hook-approval", _short(old), _short(new), HOOK_STEP)
        else:
            entry = _entry(context, "updated", _short(old), _short(new), "")
        results[f"codex-plugin:{ident}"] = entry
    return results


# oah's kit skills ----------------------------------------------------------------------------------

def kit_version(state: pathlib.Path) -> str | None:
    """A digest of each bundled item's cached commit; None when a cached record cannot be read."""
    from oah.toolkit.catalog import CatalogError, load_catalog, read_upstream
    from oah.toolkit.skillmeta import UPSTREAM_FILE, SkillError
    from oah.toolkit.update import cache_dir

    try:
        pairs = []
        for entry in load_catalog().bundled:
            cached = cache_dir(state) / entry.name
            commit = read_upstream(cached).commit if (cached / UPSTREAM_FILE).is_file() else "-"
            pairs.append(f"{entry.name}={commit}")
    except (CatalogError, SkillError, OSError, ValueError, KeyError, TypeError):
        return None
    return "sha256:" + hashlib.sha256("\n".join(sorted(pairs)).encode("utf-8")).hexdigest()


def kit_skills(root: pathlib.Path, state: pathlib.Path, env: Mapping[str, str], now: dt.datetime,
               checked_at: str) -> dict:
    """Step 5: the existing automatic kit-cache check, which keeps [skills] auto_update and its own lease rules."""
    from oah.skills_cli import settings_for
    from oah.toolkit.catalog import kit_skills_dir, load_catalog
    from oah.toolkit.update import find_gitleaks, scheduled_update

    def entry(status: str, before: str | None, after: str | None, step: str) -> dict:
        next_step = RESTART_NOTE if status == "updated" else "" if status == "current" else step
        return {"oah-skills": records.component(status, _short(before), _short(after), next_step, checked_at)}

    before = kit_version(state)
    try:
        outcome, results = scheduled_update(catalog=load_catalog(), state_dir=state, bundle_dir=kit_skills_dir(),
                                            settings=settings_for(root), now=now,
                                            gitleaks=find_gitleaks(env.get("PATH")))
    except Exception as exc:  # the skills check reports its own errors; one of them never stops the pass
        return entry("failed", before, before, f"run oah skills update to see the error ({type(exc).__name__})")
    if outcome in ("disabled", "not-due"):
        return {}
    if outcome in ("busy", "deferred"):
        return entry("unverified", before, before, "the kit check was busy or waited for a lease; it runs at "
                                                   "the next pass, or run oah skills update")
    after = kit_version(state)
    statuses = {result.status for result in results if result.kind != "mcp"}
    if statuses & {"failed", "rolled-back"}:
        return entry("failed", before, after, "run oah skills update to see the error")
    if before is not None and after is not None and before != after:
        return entry("updated", before, after, "")
    if statuses <= {"current", "pinned"} and before is not None and before == after:
        return entry("current", before, after, "")
    return entry("unverified", before, after, "a kit update was held back or not confirmed; oah skills update "
                                              "--check shows why")


KitStep = Callable[[pathlib.Path, pathlib.Path, Mapping[str, str], dt.datetime, str], dict]
