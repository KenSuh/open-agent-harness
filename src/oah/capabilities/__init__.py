# 능력 라우팅 공통 — 스킬·MCP 도구·플러그인·oah 명령의 닫힌 ID, 능력 분류, 거부 코드, 정규 JSON 해시를 한곳에 둔다.
"""Capability routing (DESIGN §6): the closed IDs, classes, refusal codes and canonical hashing shared by the
inventory adapters, the plan and parent relays.

A capability is `skill:<id>`, `mcp:<server>/<tool>` (a configured server whose tools are unknown is the provisional
locator `mcp:<server>/*`), `plugin:<name>` or `oah:<command>`. Inventories and plans never record command lines,
URLs, headers, environment values or arguments: names, flags and digests only.
"""
from __future__ import annotations

import hashlib
import json
import re

KINDS = ("skill", "mcp", "plugin", "oah")
CLASSES = ("library-docs", "web-search", "web-browse", "code-host", "issue-tracker", "database", "design-files",
           "cloud-console", "payment-docs", "messaging", "file-storage", "browser-automation", "diagramming")
OAH_COMMANDS = ("skills-read", "design-task", "gates-check")
ROUTES = ("direct", "direct-unverified", "parent-relay", "unsupported")
EFFECTS = ("read-local", "read-network", "write-local", "write-remote", "message", "payment")
RELAY_EFFECTS = ("read-local", "read-network")  # a relay brings data back; it never carries out an effect
UNAVAILABLE_CODE = "OAH_CAPABILITY_UNAVAILABLE"
RELAY_REQUIRED_CODE = "OAH_CAPABILITY_RELAY_REQUIRED"
UNSUPPORTED_CODE = "OAH_CAPABILITY_UNSUPPORTED"
INVENTORY_UNKNOWN_CODE = "OAH_CAPABILITY_INVENTORY_UNKNOWN"
DEPENDENCY_CODE = "OAH_CAPABILITY_DEPENDENCY_UNSATISFIED"
EVIDENCE_SECONDS = 300
MAX_ROUNDS = 3
RELAY_MAX_BYTES = 65536
BEGIN, END = "<!-- OAH_CAPABILITIES:BEGIN -->", "<!-- OAH_CAPABILITIES:END -->"

_NAME = r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}"
_ID = re.compile(rf"(?:skill|plugin|oah):{_NAME}|mcp:{_NAME}/(?:\*|{_NAME})")


class CapabilityRefused(Exception):
    """A typed capability refusal; INVENTORY_UNKNOWN is an environment problem (exit 69), the rest refuse (1)."""

    def __init__(self, code: str, reason: str, detail: str, *, requests: tuple = ()) -> None:
        self.code, self.reason, self.requests = code, reason, tuple(requests)
        super().__init__(f"{code}({reason}): {detail}" if reason else f"{code}: {detail}")

    @property
    def message(self) -> str:
        return str(self).split(": ", 1)[1]

    @property
    def exit_code(self) -> int:
        return 69 if self.code == INVENTORY_UNKNOWN_CODE else 1


def check_id(text: object) -> str:
    if not isinstance(text, str) or not _ID.fullmatch(text):
        raise ValueError(f"{text!r} is not a capability id such as mcp:server/tool, plugin:name, skill:name or "
                         "oah:skills-read")
    if text.startswith("oah:") and text[4:] not in OAH_COMMANDS:
        raise ValueError(f"oah commands are {', '.join(OAH_COMMANDS)}")
    return text


def tool_id(runtime_name: str) -> str | None:
    """`mcp__<server>__<tool>` (the host's runtime tool name) as `mcp:<server>/<tool>`; None for other tools."""
    if not isinstance(runtime_name, str) or not runtime_name.startswith("mcp__"):
        return None
    server, sep, tool = runtime_name[5:].partition("__")
    candidate = f"mcp:{server}/{tool}"
    return candidate if sep and _ID.fullmatch(candidate) else None


def server_of(capability: str) -> str | None:
    return capability[4:].partition("/")[0] if capability.startswith("mcp:") else None


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()
