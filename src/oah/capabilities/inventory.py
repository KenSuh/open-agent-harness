# 능력 목록 — 자식 엔진 설정에서 이름·켜짐·승인만 읽고(연결·스키마는 주장하지 않는다), 상위가 보고한 InventoryV1 의 해시와 증거 시간을 검증한다.
"""Inventories (DESIGN §6.2).

`configured.claude` and `configured.codex` read the child's own configuration files, bounded and read-only, and
establish registration, enablement and approval metadata only: connection, schema and callability stay `unknown`,
because a configuration file cannot prove a running server. Plugins count only when installed **and** enabled; a
plugin folder left in a cache is not an install. Values (commands, URLs, headers, env, args) are never read into a
record. `parent-reported` inventories are InventoryV1 documents the lead supplies; oah checks their closed keys,
digests and 300-second evidence window and labels them cooperative attestation, never the child's own connection.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import tomllib
from typing import Mapping

from oah.capabilities import (CLASSES, EFFECTS, EVIDENCE_SECONDS, INVENTORY_UNKNOWN_CODE, CapabilityRefused, check_id,
                              digest)

FILE_LIMIT = 1 << 20
ENTRY_KEYS = {"version", "id", "kind", "target_digest", "source", "schema", "registered", "installed", "enabled",
              "connection", "approval", "callability", "permission", "class", "entry_digest"}
SOURCE_KEYS = {"adapter", "host_session_digest", "evidence_digest", "observed_at", "expires_at"}
SCHEMA_KEYS = {"kind", "sha256", "metadata_digest", "provenance"}
PERMISSION_KEYS = {"effects", "scope_digest", "policy_digest", "known"}
INVENTORY_KEYS = {"version", "target", "observed_at", "expires_at", "entries", "completeness", "digest"}
ADAPTERS = ("configured.claude", "configured.codex", "parent-reported")
TRI = ("yes", "no", "unknown")
CONNECTIONS = ("connected", "disconnected", "failed", "unknown", "not-applicable")
APPROVALS = ("allowed", "pending", "denied", "unknown")
CALLABILITY = ("callable", "blocked", "unknown")
SCHEMA_KINDS = ("json-schema", "plugin-feature", "skill-body", "oah-command")
PROVENANCE = ("runtime", "verified-pin", "unknown")
COMPLETENESS = ("complete", "partial", "unobservable")
UNKNOWN_PERMISSION = {"effects": [], "scope_digest": None, "policy_digest": None, "known": False}


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def entry_digest(entry: Mapping) -> str:
    return digest({key: value for key, value in entry.items() if key != "entry_digest"})


def inventory_digest(inventory: Mapping) -> str:
    return digest({key: value for key, value in inventory.items() if key != "digest"})


def stable_digest(inventory: Mapping) -> str:
    """The digest of what a configuration-derived inventory says, without its observation times: re-reading the same
    files gives the same identity, so a retry never looks like a change (and never extends evidence either)."""
    entries = [{key: value for key, value in entry.items() if key not in ("source", "entry_digest")}
               | {"adapter": entry["source"]["adapter"]} for entry in inventory["entries"]]
    return digest({"target": inventory["target"], "entries": sorted(entries, key=lambda item: item["id"]),
                   "completeness": inventory["completeness"]})


def entry(capability: str, kind: str, *, adapter: str, observed: dt.datetime, target: str, registered: str = "yes",
          installed: str = "yes", enabled: str = "yes", approval: str = "unknown", cls: str | None = None,
          evidence: object = None) -> dict:
    """A configuration-derived entry: connection, schema and callability are unknown by construction."""
    record = {"version": 1, "id": check_id(capability), "kind": kind, "target_digest": target,
              "source": {"adapter": adapter, "host_session_digest": None, "evidence_digest": digest(evidence),
                         "observed_at": _iso(observed),
                         "expires_at": _iso(observed + dt.timedelta(seconds=EVIDENCE_SECONDS))},
              "schema": {"kind": "plugin-feature" if kind == "plugin" else "json-schema", "sha256": None,
                         "metadata_digest": None, "provenance": "unknown"},
              "registered": registered, "installed": installed, "enabled": enabled,
              "connection": "not-applicable" if kind == "plugin" else "unknown", "approval": approval,
              "callability": "unknown", "permission": dict(UNKNOWN_PERMISSION), "class": cls}
    record["entry_digest"] = entry_digest(record)
    return record


def _read_json(path: pathlib.Path) -> tuple[object, bool]:
    """(data, readable) — a missing file is (None, True); an unreadable or malformed one is (None, False)."""
    try:
        if path.stat().st_size > FILE_LIMIT:
            return None, False
        return json.loads(path.read_text(encoding="utf-8")), True
    except FileNotFoundError:
        return None, True
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None, False


def _names(value: object) -> list[str]:
    return sorted(name for name in value if isinstance(name, str)) if isinstance(value, Mapping) else []


def _name_list(value: object) -> set[str]:
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def configured_claude(*, root: pathlib.Path, env: Mapping[str, str], home: pathlib.Path, now: dt.datetime,
                      classes: Mapping[str, str]) -> tuple[list[dict], bool]:
    """(entries, complete) from Claude's documented configuration files for this working folder."""
    config_dir = pathlib.Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else home / ".claude"
    global_path = config_dir / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    target = digest({"engine": "claude", "config": str(config_dir), "root": str(root)})
    complete = True
    settings: dict = {"enabledPlugins": {}, "enabledMcpjsonServers": set(), "disabledMcpjsonServers": set(),
                      "enableAllProjectMcpServers": False}
    for path in (config_dir / "settings.json", root / ".claude" / "settings.json",
                 root / ".claude" / "settings.local.json"):
        data, readable = _read_json(path)
        complete &= readable
        if not isinstance(data, Mapping):
            continue
        if isinstance(data.get("enabledPlugins"), Mapping):
            settings["enabledPlugins"].update({key: value for key, value in data["enabledPlugins"].items()
                                               if isinstance(key, str) and isinstance(value, bool)})
        settings["enabledMcpjsonServers"] |= _name_list(data.get("enabledMcpjsonServers"))
        settings["disabledMcpjsonServers"] |= _name_list(data.get("disabledMcpjsonServers"))
        if data.get("enableAllProjectMcpServers") is True:
            settings["enableAllProjectMcpServers"] = True
    found: dict[str, dict] = {}

    def add(server: str, *, enabled: str, approval: str, evidence: object) -> None:
        capability = f"mcp:{server}/*"
        try:
            found[capability] = entry(capability, "mcp", adapter="configured.claude", observed=now, target=target,
                                      enabled=enabled, approval=approval, cls=classes.get(server),
                                      evidence=evidence)
        except ValueError:
            pass  # a server name outside the id grammar is not addressable; it is never guessed into one

    global_data, readable = _read_json(global_path)
    complete &= readable
    project = {}
    if isinstance(global_data, Mapping):
        for name in _names(global_data.get("mcpServers")):
            add(name, enabled="yes", approval="allowed", evidence=["user", name])
        projects = global_data.get("projects")
        project = projects.get(str(root), {}) if isinstance(projects, Mapping) else {}
        project = project if isinstance(project, Mapping) else {}
        disabled = _name_list(project.get("disabledMcpServers"))
        for name in _names(project.get("mcpServers")):
            add(name, enabled="no" if name in disabled else "yes", approval="allowed", evidence=["local", name])
    enabled_json = settings["enabledMcpjsonServers"] | _name_list(project.get("enabledMcpjsonServers"))
    disabled_json = settings["disabledMcpjsonServers"] | _name_list(project.get("disabledMcpjsonServers"))
    all_json = settings["enableAllProjectMcpServers"] or project.get("enableAllProjectMcpServers") is True
    data, readable = _read_json(root / ".mcp.json")
    complete &= readable
    for name in _names(data.get("mcpServers") if isinstance(data, Mapping) else None):
        if name in disabled_json:
            add(name, enabled="no", approval="denied", evidence=["project", name])
        else:
            add(name, enabled="yes", approval="allowed" if all_json or name in enabled_json else "pending",
                evidence=["project", name])
    installed, readable = _read_json(config_dir / "plugins" / "installed_plugins.json")
    complete &= readable
    plugins = installed.get("plugins") if isinstance(installed, Mapping) else None
    for key in _names(plugins):
        records = plugins[key] if isinstance(plugins[key], list) else [plugins[key]]
        paths = [pathlib.Path(item["installPath"]) for item in records
                 if isinstance(item, Mapping) and isinstance(item.get("installPath"), str)]
        state = settings["enabledPlugins"].get(key)
        enabled = "yes" if state is True else "no"
        short = key.partition("@")[0]
        try:
            found[f"plugin:{short}"] = entry(f"plugin:{short}", "plugin", adapter="configured.claude", observed=now,
                                             target=target, enabled=enabled, approval="allowed" if state else "unknown",
                                             evidence=["plugin", key])
        except ValueError:
            continue
        if enabled != "yes":
            continue  # a disabled plugin's servers are not the child's, whatever its cache still holds
        for path in paths:
            servers, readable = _read_json(path / ".mcp.json")
            complete &= readable
            manifest, readable = _read_json(path / ".claude-plugin" / "plugin.json")
            complete &= readable
            names = set(_names(servers.get("mcpServers") if isinstance(servers, Mapping) else None))
            if isinstance(manifest, Mapping):
                names |= set(_names(manifest.get("mcpServers")))
            for name in sorted(names):
                add(f"plugin_{short}_{name}", enabled="yes", approval="allowed", evidence=["plugin", key, name])
    return sorted(found.values(), key=lambda item: item["id"]), complete


def configured_codex(*, env: Mapping[str, str], home: pathlib.Path, now: dt.datetime,
                     classes: Mapping[str, str]) -> tuple[list[dict], bool]:
    """(entries, complete) from `[mcp_servers.<name>]` in the child's CODEX_HOME/config.toml (names and flags)."""
    codex_home = pathlib.Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"
    target = digest({"engine": "codex", "home": str(codex_home)})
    path = codex_home / "config.toml"
    try:
        if path.stat().st_size > FILE_LIMIT:
            return [], False
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], True
    except (OSError, ValueError, UnicodeDecodeError):
        return [], False
    servers = data.get("mcp_servers")
    out = []
    for name in _names(servers):
        table = servers[name] if isinstance(servers[name], Mapping) else {}
        enabled = "no" if table.get("enabled") is False else "yes"
        try:
            out.append(entry(f"mcp:{name}/*", "mcp", adapter="configured.codex", observed=now, target=target,
                             enabled=enabled, approval="unknown", cls=classes.get(name), evidence=["codex", name]))
        except ValueError:
            continue
        for tool in sorted(_name_list(table.get("disabled_tools"))):
            try:
                out.append(entry(f"mcp:{name}/{tool}", "mcp", adapter="configured.codex", observed=now,
                                 target=target, enabled="no", approval="unknown", cls=classes.get(name),
                                 evidence=["codex", name, tool]))
            except ValueError:
                continue
    return out, True


def child(engine: str, *, root: pathlib.Path, env: Mapping[str, str], home: pathlib.Path, now: dt.datetime,
          classes: Mapping[str, str]) -> dict:
    """The child's InventoryV1 from its configuration (`partial` when a file could not be read)."""
    if engine == "claude":
        entries, complete = configured_claude(root=root, env=env, home=home, now=now, classes=classes)
    else:
        entries, complete = configured_codex(env=env, home=home, now=now, classes=classes)
    inventory = {"version": 1, "target": {"engine": engine, "route": "process"}, "observed_at": _iso(now),
                 "expires_at": _iso(now + dt.timedelta(seconds=EVIDENCE_SECONDS)), "entries": entries,
                 "completeness": "complete" if complete else "partial"}
    inventory["digest"] = inventory_digest(inventory)
    return inventory


def _closed(value: object, keys: set[str], what: str) -> Mapping:
    if not isinstance(value, Mapping) or set(value) != keys:
        missing, extra = sorted(keys - set(value or {})), sorted(set(value or {}) - keys)
        raise ValueError(f"{what} has keys {sorted(keys)} (missing {missing}, unknown {extra})")
    return value


def _time(value: object, what: str) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{what} is not an ISO time") from None
    if moment.tzinfo is None:
        raise ValueError(f"{what} has no time zone")
    return moment


def check_parent(data: object, *, now: dt.datetime) -> dict:
    """A validated parent-reported InventoryV1; ValueError names the first problem."""
    inventory = _closed(data, INVENTORY_KEYS, "the inventory")
    if inventory["version"] != 1 or inventory["completeness"] not in COMPLETENESS:
        raise ValueError("the inventory has version 1 and completeness complete, partial or unobservable")
    observed, expires = _time(inventory["observed_at"], "observed_at"), _time(inventory["expires_at"], "expires_at")
    if expires - observed > dt.timedelta(seconds=EVIDENCE_SECONDS) or expires < observed:
        raise ValueError(f"the inventory's evidence window is at most {EVIDENCE_SECONDS} seconds")
    if not isinstance(inventory["entries"], list) or len(inventory["entries"]) > 512:
        raise ValueError("the inventory's entries are a list of at most 512")
    for item in inventory["entries"]:
        record = _closed(item, ENTRY_KEYS, "an inventory entry")
        check_id(record["id"])
        source = _closed(record["source"], SOURCE_KEYS, "an entry source")
        schema = _closed(record["schema"], SCHEMA_KEYS, "an entry schema")
        permission = _closed(record["permission"], PERMISSION_KEYS, "an entry permission")
        if (record["version"] != 1 or record["kind"] != record["id"].partition(":")[0]
                or source["adapter"] != "parent-reported" or schema["kind"] not in SCHEMA_KINDS
                or schema["provenance"] not in PROVENANCE or record["registered"] not in TRI
                or record["installed"] not in TRI or record["enabled"] not in TRI
                or record["connection"] not in CONNECTIONS or record["approval"] not in APPROVALS
                or record["callability"] not in CALLABILITY or record["class"] not in (None, *CLASSES)
                or not isinstance(permission["known"], bool) or not isinstance(permission["effects"], list)
                or any(effect not in EFFECTS for effect in permission["effects"])):
            raise ValueError(f"inventory entry {record['id']} has a value outside its closed lists")
        _time(source["observed_at"], "an entry observed_at")
        _time(source["expires_at"], "an entry expires_at")
        if record["entry_digest"] != entry_digest(record):
            raise ValueError(f"inventory entry {record['id']} does not match its entry_digest")
    if inventory["digest"] != inventory_digest(inventory):
        raise ValueError("the inventory does not match its digest")
    if expires <= now:
        raise ValueError("the inventory's evidence has expired; read the tools again and write a new inventory")
    return dict(inventory)


def read_parent(path: str | os.PathLike, *, now: dt.datetime) -> dict:
    """The lead's InventoryV1 file; OAH_CAPABILITY_INVENTORY_UNKNOWN (exit 69) when it cannot be read or trusted."""
    data, readable = _read_json(pathlib.Path(path))
    if data is None:
        why = "it is missing" if readable else "it is unreadable, over 1 MiB or not JSON"
        raise CapabilityRefused(INVENTORY_UNKNOWN_CODE, "", f"the parent inventory {path}: {why}")
    try:
        return check_parent(data, now=now)
    except ValueError as exc:
        raise CapabilityRefused(INVENTORY_UNKNOWN_CODE, "", f"the parent inventory {path}: {exc}") from None


def qualified(record: Mapping, now: dt.datetime) -> bool:
    """Exact current evidence: a known schema, enabled, connected, allowed, callable, known permission, fresh."""
    return (record["schema"]["sha256"] is not None and record["schema"]["provenance"] != "unknown"
            and record["enabled"] == "yes" and record["connection"] in ("connected", "not-applicable")
            and record["approval"] == "allowed" and record["callability"] == "callable"
            and record["permission"]["known"] and _time(record["source"]["expires_at"], "expires_at") > now)


def parent_entry(capability: str, *, schema: Mapping, observed: dt.datetime, effects: list[str],
                 connection: str = "connected", approval: str = "allowed", callability: str = "callable",
                 cls: str | None = None, session: str | None = None) -> dict:
    """A parent-reported entry built from the lead's own live tool descriptor (name, description, inputSchema)."""
    record = {"version": 1, "id": check_id(capability), "kind": capability.partition(":")[0],
              "target_digest": digest({"route": "parent"}),
              "source": {"adapter": "parent-reported", "host_session_digest": digest(session) if session else None,
                         "evidence_digest": digest(dict(schema)), "observed_at": _iso(observed),
                         "expires_at": _iso(observed + dt.timedelta(seconds=EVIDENCE_SECONDS))},
              "schema": {"kind": "json-schema", "sha256": digest(dict(schema)), "metadata_digest": None,
                         "provenance": "runtime"},
              "registered": "yes", "installed": "yes", "enabled": "yes", "connection": connection,
              "approval": approval, "callability": callability,
              "permission": {"effects": sorted(effects), "scope_digest": None, "policy_digest": None, "known": True},
              "class": cls}
    record["entry_digest"] = entry_digest(record)
    return record


def parent_inventory(entries: list[dict], *, observed: dt.datetime) -> dict:
    inventory = {"version": 1, "target": {"route": "parent"}, "observed_at": _iso(observed),
                 "expires_at": _iso(observed + dt.timedelta(seconds=EVIDENCE_SECONDS)),
                 "entries": sorted(entries, key=lambda item: item["id"]), "completeness": "partial"}
    inventory["digest"] = inventory_digest(inventory)
    return inventory
