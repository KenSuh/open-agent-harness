# 도구 업데이트 단계 — Claude Code·Codex 본체, Claude Code 마켓플레이스·플러그인, oah 키트 스킬을 정해진 순서로 갱신하고 결과를 확인한다.
"""The steps of one pass, in order. Each returns {component name: entry} through `records.component`.

1. `claude update`; 2. `codex update`; 3. `claude plugin marketplace update`, then
`claude plugin update <id> --scope user --json` for each plugin installed at user scope; 4. oah's kit
skills through `toolkit.update.scheduled_update`. Codex plugins are only a read-only notice.
Nothing else is run: no package manager, no shell, no confirmation flag, nothing about MCP servers. Only a
command started by the shared runner can be stopped on timeout; unrelated processes are never listed, signalled or stopped.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import hashlib
import json
import pathlib
import subprocess
from typing import Callable, Mapping

from oah.toolupdate import probes, records
from oah.toolupdate.probes import Cli

RESTART_NOTE = ("sessions already running keep the version and plugins they started with; restart them to use "
                "the new ones")
CODEX_PLUGINS_NOTE = ("Codex plugins are not updated automatically: update them with codex plugin marketplace "
                      "upgrade, then approve their hooks again in /hooks")
LABELS = {"claude": "Claude Code", "codex": "Codex", "oah-skills": "oah kit skills",
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
                            timed_out=ran.timed_out, clean=ran.clean)
    return {cli.name: _entry(context, status, before, after,
                             _steps_for(command, timed_out=ran.timed_out).get(status, ""))}


def claude_marketplaces(context: Context) -> dict:
    """Refresh marketplaces as a separate component, verified from their installed Git revisions."""
    cli = context.claude
    command = "claude plugin marketplace update"
    if cli.base is None or not (cli.lists("plugin") and cli.lists("plugin", "marketplace")
                                and cli.lists("plugin", "marketplace", "update")):
        return {"claude-marketplaces": _entry(context, "unsupported", None, None,
                "this Claude Code has no `claude plugin marketplace update`; refresh them in Claude Code")}
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


def _short(digest: str | None) -> str | None:
    return digest.removeprefix("sha256:")[:12] if digest else None


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
               checked_at: str, timeout_seconds: float) -> dict:
    """Step 4: the existing automatic kit-cache check, which keeps [skills] auto_update and its own lease rules."""
    from oah.skills_cli import settings_for
    from oah.toolkit.catalog import kit_skills_dir, load_catalog
    from oah.toolkit.fetch import FETCH_TIMEOUT, fetch
    from oah.toolkit.update import find_gitleaks, scheduled_update

    def entry(status: str, before: str | None, after: str | None, step: str) -> dict:
        next_step = RESTART_NOTE if status == "updated" else "" if status == "current" else step
        return {"oah-skills": records.component(status, _short(before), _short(after), next_step, checked_at)}

    before = kit_version(state)
    try:
        outcome, results = scheduled_update(catalog=load_catalog(), state_dir=state, bundle_dir=kit_skills_dir(),
                                            settings=settings_for(root), now=now,
                                            gitleaks=find_gitleaks(env.get("PATH")),
                                            fetcher=functools.partial(fetch, timeout=min(timeout_seconds, FETCH_TIMEOUT)))
    except (TimeoutError, subprocess.TimeoutExpired):
        return entry("failed", before, before, _steps_for("oah skills update", timed_out=True)["failed"])
    except Exception as exc:  # the skills check reports its own errors; one of them never stops the pass
        return entry("failed", before, before, f"run oah skills update to see the error ({type(exc).__name__})")
    # scheduled_update exposes only fetcher's network timeout. Its other Git commands and gitleaks
    # retain their own limits; their runner/timeouts cannot be passed through this existing API.
    if outcome == "disabled":
        return entry("unsupported", before, before, "kit updates are disabled by [skills] auto_update")
    if outcome == "not-due":
        return entry("unverified", before, before, "the kit check is not due yet; run oah skills update to check now")
    if outcome in ("busy", "deferred"):
        return entry("unverified", before, before, "the kit check was busy or waited for a lease; it runs at "
                                                   "the next pass, or run oah skills update")
    after = kit_version(state)
    results = [result for result in results if result.kind != "mcp"]
    if any("timed out" in reason or "did not finish in time" in reason
           for result in results for reason in result.reasons):
        return entry("failed", before, after, _steps_for("oah skills update", timed_out=True)["failed"])
    statuses = {result.status for result in results}
    if statuses & {"failed", "rolled-back"}:
        return entry("failed", before, after, "run oah skills update to see the error")
    if before is not None and after is not None and before != after:
        return entry("updated", before, after, "")
    if statuses and statuses <= {"current", "pinned"} and before is not None and before == after:
        return entry("current", before, after, "")
    return entry("unverified", before, after, "a kit update was held back or not confirmed; oah skills update "
                                              "--check shows why")


KitStep = Callable[[pathlib.Path, pathlib.Path, Mapping[str, str], dt.datetime, str, float], dict]
