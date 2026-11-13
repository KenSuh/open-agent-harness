# 능력 목록 — 자식 엔진 설정에서 이름·켜짐·승인만 읽고(연결·스키마는 주장하지 않는다), 상위가 보고한 InventoryV1 의 해시와 증거 시간을 검증한다.
"""Inventories (DESIGN §6.2).

`configured.claude` and `configured.codex` read the child's own configuration files, bounded and read-only, and
establish registration, enablement and approval metadata only: connection, schema and callability stay `unknown`,
because a configuration file cannot prove a running server. Plugins count only when installed **and** enabled; a
plugin folder left in a cache is not an install. Values (commands, URLs, headers, env, args) are never read into a
record. `parent-reported` inventories are InventoryV1 documents the lead supplies; oah checks their closed keys,
digests and 300-second evidence window and labels them cooperative attestation, never the child's own connection.

`EventSink` (DESIGN §6.5) reads a running child's own Claude stream-json stdout: its init event and each capability
tool use paired with its own result. It keeps digests and typed words only, and it is observation, not a barrier: a
child can act before its first line.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import time
import tomllib
from typing import Callable, Mapping

from oah.capabilities import (CLASSES, EFFECTS, EVIDENCE_SECONDS, INVENTORY_UNKNOWN_CODE, CapabilityRefused, check_id,
                              digest, tool_id)

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
        actual = set(value) if isinstance(value, Mapping) else set()
        missing, extra = sorted(keys - actual), sorted(actual - keys)
        raise ValueError(f"{what} has keys {sorted(keys)} (missing {missing}, unknown {extra})")
    return value


def _time(value: object, what: str) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{what} is not an ISO time") from None
    if moment.tzinfo is None:
        raise ValueError(f"{what} has no time zone")
    try:
        moment.astimezone(dt.timezone.utc)
    except OverflowError:
        raise ValueError(f"{what} cannot be represented in UTC") from None
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


def read_parent(path: str | os.PathLike, *, now: dt.datetime, recorded: bool = False) -> dict:
    """The lead's InventoryV1 file; OAH_CAPABILITY_INVENTORY_UNKNOWN (exit 69) when it cannot be read or trusted."""
    from oah.discovery import inputs, records
    from oah.design.sources.state import StateProblem, loads

    def checked(value):
        try:
            return check_parent(value, now=now)
        except ValueError as exc:
            raise CapabilityRefused(INVENTORY_UNKNOWN_CODE, "", f"the parent inventory {path}: {exc}") from None

    source = pathlib.Path(path)
    if recorded:
        return records.followed_bytes(source, limit=FILE_LIMIT,
            parse=lambda raw: checked(records.json_bytes(raw, source)))
    try:
        raw = inputs.read_bytes(source, limit=FILE_LIMIT)
        if len(raw) > FILE_LIMIT:
            raise ValueError("over 1 MiB")
        data, readable = loads(raw, source.name), True
    except FileNotFoundError:
        data, readable = None, True
    except (OSError, ValueError, StateProblem):
        data, readable = None, False
    if data is None:
        why = "it is missing" if readable else "it is unreadable, over 1 MiB or not JSON"
        raise CapabilityRefused(INVENTORY_UNKNOWN_CODE, "", f"the parent inventory {path}: {why}")
    return checked(data)


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



# Live stream observation (DESIGN §6.5) ---------------------------------------------------------------------------

STREAM_EVENT_LIMIT = 256 * 1024  # bytes in one stream-json line
STREAM_OPEN_LIMIT = 64  # capability tool uses waiting for their own result
STREAM_TOTAL_LIMIT = 16 * 1024 * 1024  # bytes of events read per run; the rest stays uninspected
STREAM_RECORD_LIMIT = 1024  # invocation records kept per run; the rest are counted
INIT_SECONDS = 10.0  # the init event's deadline, or the run's own timeout when that is shorter
RECEIVING_NAME = "capabilities-receiving.json"
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@\[\]-]{0,127}")  # a model or status kept from the stream
_SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")  # an MCP server name kept from the stream


def _unique_pairs(pairs: list) -> dict:
    found = {}
    for key, value in pairs:
        if key in found:
            raise ValueError(f"duplicate key {key!r}")
        found[key] = value
    return found


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _word(value: object) -> str | None:
    return value if isinstance(value, str) and _WORD.fullmatch(value) else None


class EventSink:
    """EventSinkV1 (DESIGN §6.5): the pure, bounded reader of one Claude stream-json stdout.

    `feed(stream, data)` returns None to go on or a typed stop reason; `overdue()` reports a missing init after its
    deadline; `finish(returncode, quiescent)` closes the reading once and returns the receiving projection. Complete
    lines are read as strict UTF-8 JSON (a CRLF ending is one line ending) and no more than one event is buffered.
    Until the init event qualifies, every problem asks the runner to stop the process tree: a first event that is not
    the init, an init that does not match the run, a line that is not strict JSON or that is larger than
    STREAM_EVENT_LIMIT. After it, an oversize line is skipped uninspected (a use it answered stays unobserved), and
    invalid JSON, a contradictory init or another session's event still stop.

    Each `mcp__<server>__<tool>` use is paired with its own result by exact id: `ok`, `error` (whatever its text),
    `denied` (the result event's permission denials) or `unobserved` (never answered, or not inspected). Records keep
    digests, capability IDs and typed words; no argument, result or stream text. The sink executes nothing and calls
    nothing; the runner owns processes, threads and kill, and serializes the calls."""

    def __init__(self, *, run: str, cwd: str, cli_version: str | None, permission_mode: str | None,
                 planned: Mapping[str, Mapping], bundle_id: str | None, timeout_s: float,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], dt.datetime] | None = None,
                 event_limit: int = STREAM_EVENT_LIMIT, open_limit: int = STREAM_OPEN_LIMIT,
                 total_limit: int = STREAM_TOTAL_LIMIT, record_limit: int = STREAM_RECORD_LIMIT) -> None:
        self.run, self.bundle_id, self.planned = run, bundle_id, dict(planned)
        self.cwd, self.cli_version, self.permission_mode = os.path.realpath(cwd), cli_version, permission_mode
        self.clock, self.wall = clock, wall or (lambda: dt.datetime.now(dt.timezone.utc))
        self.wait_s = min(INIT_SECONDS, timeout_s)
        self.deadline = clock() + self.wait_s
        self.event_limit, self.open_limit = event_limit, open_limit
        self.total_limit, self.record_limit = total_limit, record_limit
        self.buffer = bytearray()
        self.skipping = False  # inside an oversize line after the init: discard through its newline
        self.inspected = self.uninspected = self.omitted = 0
        self.truncated = False
        self.identity = self.summary = self.session = None
        self.reason = self.detail = None
        self.open: dict[str, dict] = {}
        self.closed: dict[str, dict] = {}
        self.records: list[dict] = []
        self.observation = None
        from oah.capabilities.usage import AvailableUsage

        self.usage = AvailableUsage()

    def _stop(self, reason: str, detail: str) -> str:
        if self.reason is None:
            self.reason, self.detail = reason, detail
        return self.reason

    def feed(self, stream: str, data: bytes) -> str | None:
        """None to go on, or the stop reason; only stdout carries events."""
        if self.reason is not None or self.observation is not None:
            return self.reason
        if stream != "stdout":
            return None
        start = 0
        while start < len(data) and not self.truncated:
            end = data.find(b"\n", start)
            piece = data[start:] if end < 0 else data[start:end]
            start = len(data) if end < 0 else end + 1
            if self.skipping:
                self.skipping = end < 0
                continue
            size = len(self.buffer) + len(piece)
            if size <= self.event_limit + 1 and end < 0:
                self.buffer += piece  # at most one event, plus the CR of a CRLF ending
                continue
            line = b"" if size > self.event_limit + 1 else bytes(self.buffer) + piece
            self.buffer = bytearray()
            line = line[:-1] if line.endswith(b"\r") else line
            if size > self.event_limit + 1 or len(line) > self.event_limit:
                if self.identity is None:
                    return self._stop("oversize", f"a stream event is larger than {self.event_limit} bytes")
                self.uninspected += 1
                self.skipping = end < 0
                continue
            stop = self._line(line)
            if stop is not None:
                return stop
        return None

    def _line(self, raw: bytes) -> str | None:
        if not raw.strip():
            return None
        if self.inspected + len(raw) > self.total_limit:
            self.truncated = True  # bounded reading: the rest of the stream stays uninspected
            return None
        self.inspected += len(raw)
        try:
            event = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return self._stop("invalid-json", "a stream line is not strict UTF-8 JSON")
        if not isinstance(event, dict):
            return self._stop("invalid-json", "a stream line is not a JSON object")
        return self._init(event) if self.identity is None else self._event(event)

    @staticmethod
    def _identity_of(event: Mapping) -> str:
        cwd = event.get("cwd")
        return digest({"session": event.get("session_id"), "version": event.get("claude_code_version"),
                       "cwd": os.path.realpath(cwd) if isinstance(cwd, str) else None, "model": event.get("model")})

    def _init(self, event: Mapping) -> str | None:
        if event.get("type") != "system" or event.get("subtype") != "init":
            return self._stop("missing-init", "the first stream event is not the session's init event")
        session, cwd, tools = event.get("session_id"), event.get("cwd"), event.get("tools")
        version, mode = event.get("claude_code_version"), event.get("permissionMode")
        servers = event.get("mcp_servers", [])
        if not isinstance(session, str) or not session:
            return self._stop("missing-field", "the init event has no session id")
        if not isinstance(cwd, str):
            return self._stop("missing-field", "the init event has no working directory")
        if not isinstance(tools, list) or not all(isinstance(name, str) for name in tools):
            return self._stop("missing-field", "the init event has no tool list")
        if not isinstance(version, str):
            return self._stop("missing-field", "the init event has no Claude Code version")
        if not isinstance(servers, list) or not all(isinstance(item, dict) and isinstance(item.get("name"), str)
                                                    and isinstance(item.get("status"), str) for item in servers):
            return self._stop("missing-field", "the init event's MCP servers are not each named with a status")
        if os.path.realpath(cwd) != self.cwd:
            return self._stop("init-mismatch", "the session's working directory is not the run's")
        if self.cli_version is not None and version != self.cli_version:
            return self._stop("init-mismatch", f"the session's Claude Code version is not the prepared "
                                               f"{self.cli_version}")
        if self.permission_mode is not None and mode is not None and mode != self.permission_mode:
            return self._stop("init-mismatch", f"the session's permission mode is not the prepared "
                                               f"{self.permission_mode}")
        self.session, self.identity = session, self._identity_of(event)
        self.summary = {"session_id_digest": _sha(session),
                        "cli_version": self.cli_version, "cli_version_check": "matched" if self.cli_version else
                        "unverified", "model": _word(event.get("model")), "tools_digest": digest(sorted(tools)),
                        "mcp_tools": sorted({capability for name in tools if (capability := tool_id(name))}),
                        "servers": {item["name"]: _word(item["status"]) or "unknown" for item in servers
                                    if _SERVER.fullmatch(item["name"])}}
        return None

    def _event(self, event: Mapping) -> str | None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            return None if self._identity_of(event) == self.identity else self._stop(
                "contradictory-init", "a second init event names another session, directory, version or model")
        session = event.get("session_id")
        if session is not None and session != self.session:
            return self._stop("other-session", "a stream event belongs to another session")
        self.usage.event(event)
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        parts = [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []
        if kind == "assistant":
            for part in parts:
                if part.get("type") == "tool_use":
                    self._use(part)
        elif kind == "user":
            for part in parts:
                if part.get("type") == "tool_result":
                    self._result(part)
        elif kind == "result" and isinstance(event.get("permission_denials"), list):
            for denial in event["permission_denials"]:
                if isinstance(denial, dict):
                    self._deny(denial.get("tool_use_id"))
        return None

    def _use(self, part: Mapping) -> None:
        capability, use_id = tool_id(part.get("name")), part.get("id")
        if capability is None or not isinstance(use_id, str) or not use_id or use_id in self.open \
                or use_id in self.closed:
            return
        if len(self.open) >= self.open_limit:
            self._close(next(iter(self.open)), "unobserved", None)  # the oldest waits no longer
        row = self.planned.get(capability)
        session = self.summary["session_id_digest"]
        self.open[use_id] = {
            "version": 1, "run": self.run, "bundle_id": self.bundle_id, "capability_id": capability,
            "entry_digest": (row or {}).get("entry_digest") or digest({"adapter": "effective.claude-init",
                                                                       "id": capability, "session": session}),
            "schema_digest": None, "session_id_digest": session, "tool_use_id_digest": _sha(use_id),
            "arguments_digest": digest(part.get("input")), "result": "unobserved", "result_sha256": None,
            "selection": "planned" if row is not None else "unplanned", "evidence": "host-observed", "at": None}

    def _result(self, part: Mapping) -> None:
        use_id = part.get("tool_use_id")
        if isinstance(use_id, str) and use_id in self.open:
            error = part.get("is_error")
            self._close(use_id, "ok" if error is None or error is False else "error", digest(part.get("content")))

    def _deny(self, use_id: object) -> None:
        if not isinstance(use_id, str):
            return
        if use_id in self.open:
            self._close(use_id, "denied", None)
        elif use_id in self.closed:
            self.closed[use_id]["result"] = "denied"

    def _close(self, use_id: str, result: str, result_sha256: str | None) -> None:
        record = self.open.pop(use_id)
        record.update(result=result, result_sha256=result_sha256, at=_iso(self.wall()))
        if len(self.records) < self.record_limit:
            self.records.append(record)
            self.closed[use_id] = record
        else:
            self.omitted += 1

    def overdue(self) -> str | None:
        """The stop reason, including a missing init after its deadline; None while the run may go on."""
        if self.reason is None and self.identity is None and self.observation is None \
                and self.clock() >= self.deadline:
            return self._stop("init-deadline", f"the session sent no init event within {self.wait_s:g} s")
        return self.reason

    def finish(self, returncode: int | None, quiescent: bool) -> dict:
        """The closed receiving projection, made once: every use still waiting is unobserved."""
        if self.observation is not None:
            return self.observation
        for use_id in list(self.open):
            self._close(use_id, "unobserved", None)
        status = "failed" if self.reason is not None else "observed" if self.identity is not None else "unobserved"
        observation = {"version": 1, "run": self.run, "bundle_id": self.bundle_id, "status": status,
                       "reason": self.reason, "detail": self.detail, "stopped": self.reason is not None,
                       "init": self.summary, "invocations": self.records, "omitted": self.omitted,
                       "inspected_bytes": self.inspected, "uninspected_events": self.uninspected,
                       "inspection": "truncated" if self.truncated else "complete",
                       "returncode": returncode, "quiescent": bool(quiescent),
                       "available_usage": self.usage.record()}
        observation["digest"] = digest(observation)
        self.observation = observation
        return observation
