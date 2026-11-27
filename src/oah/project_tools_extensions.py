from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
from typing import Any, BinaryIO

from oah import project_tools_jev as jev
from oah import project_tools_wiki_index as wiki
from oah.rerank import typesafe

_JEV_PROJECT_ACTIONS = ("status", "prepare", "enable", "disable")
_JEV_COMMANDS = {"store-key": "key-store", "delete-key": "key-delete",
                 "prepare-test": "test-prepare", "test": "test-run"}
INPUT_LIMIT = 64 * 1024
DEFAULT_MODEL = "bge-m3"
_STATUS = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
_INDEX_CODE = re.compile(r"OAH_WIKI_INDEX_[A-Z0-9_]{1,46}\Z")

_INDEX_MESSAGES = {
    wiki.CODE_NOT_A_REPO: "Open a Git project to build an index. You can still search the wiki files directly.",
    wiki.CODE_GIT: "Install Git or add it to your device's PATH, then try again.",
    wiki.CODE_NO_SNAPSHOT: "Commit the wiki notes before building a local index.",
    wiki.CODE_SELECTION: "Choose a project-relative wiki folder and review the index exclusions.",
    wiki.CODE_NO_NOTES: "Commit at least one Markdown note in the selected wiki folder.",
    wiki.CODE_TOO_LARGE: "The selected wiki exceeds the local index limits. Choose a smaller folder.",
    wiki.CODE_TOOL_NOT_CONFIGURED: "Choose a gbrain executable in Local Tools Setup or put it on your device's PATH.",
    wiki.CODE_TOOL_MISSING: "The gbrain executable was not found. Check your device's PATH.",
    wiki.CODE_TOOL_REJECTED: "Use a gbrain executable installed outside this project.",
    wiki.CODE_TOOL_FAILED: "gbrain could not complete this action. Check your device's installation.",
    wiki.CODE_OLLAMA_DOWN: "Start Ollama on the port in Local Tools Setup and try again.",
    wiki.CODE_MODEL_MISSING: "Choose an embedding model available in your local Ollama server.",
    wiki.CODE_MODEL_UNUSABLE: "Choose a local embedding model that can produce vectors in Ollama.",
    wiki.CODE_MODEL_CHANGED: "Refresh the index for the current embedding model.",
    wiki.CODE_NOT_BUILT: "Build the local index to search by meaning.",
    wiki.CODE_INCOMPLETE: "Build again to continue the incomplete local index.",
    wiki.CODE_BUSY: "Wait for the other local index action to finish.",
    wiki.CODE_STATE: "Check that your OAH state folder is accessible.",
}


def _plain(text: object, limit: int) -> str:
    return "".join(char for char in text[:limit] if char.isprintable()) if isinstance(text, str) else ""


def _destination(value: object) -> dict[str, str]:
    if isinstance(value, dict) and value.get("kind") in ("official", "custom", "loopback"):
        return {"kind": "official" if value["kind"] == "official" else "custom",
                "url": value["url"], "model": value["model"]}
    return {"kind": "refused", "url": "", "model": ""}


def _status_reply(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        return _simple_jev(result)
    scopes = [{"scope": scope, "state": state} for scope, state in result.get("scopes", {}).items()
              if scope == "impact" and state in ("granted", "consent-stale")]
    consent = result["consent"]
    if any(scope["state"] == "consent-stale" for scope in scopes):
        consent = "consent-stale"
    return {"ok": result["ok"] and result["destination_status"] == "ok", "status": result["destination_status"], "consent": consent,
            "destination": _destination(result.get("destination")), "scopes": scopes}


def _simple_jev(value: dict[str, Any]) -> dict[str, Any]:
    status = value.get("status", "error")
    return {"ok": value.get("ok") is True, "status": status if isinstance(status, str) and _STATUS.fullmatch(status) else "error"}


def _json_input(stream: BinaryIO) -> dict[str, Any]:
    body = stream.read(INPUT_LIMIT + 1)
    if len(body) > INPUT_LIMIT:
        raise ValueError("request too large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("an object is required")
    return value


def run_jev(action: str, root: pathlib.Path, stream: BinaryIO) -> dict[str, Any]:
    args = [_JEV_COMMANDS.get(action, action)]
    if action in _JEV_PROJECT_ACTIONS:
        args += ["--root", os.fspath(root)]
    code, value = jev.dispatch(args, stdin=stream, env={} if action in ("key-state", "test") else None)
    if code == 2:
        raise ValueError("invalid Jev request")
    if action == "status":
        return _status_reply(value)
    result = _simple_jev(value)
    if not value.get("ok"):
        if action == "test" and value.get("source") == "device":
            result["source"] = "device"
        return result
    if action == "prepare":
        destination = _destination(value["destination"])
        if destination["kind"] != "official":
            return {"ok": False, "status": "destination"}
        result.update(token=value["token"], destination=destination,
                      scopes=[{name: scope[name] for name in ("scope", "title", "sends", "labels", "never", "bounds")}
                              for scope in value["scopes"]])
    elif action == "prepare-test":
        result.update(token=value["token"], requests=value["requests"], timeout_seconds=value["timeout_seconds"],
                      destination={"kind": "official", "url": typesafe.OFFICIAL_URL,
                                   "model": _plain(value["request"].get("model"), 128)})
    elif action == "key-state":
        result["key_state"] = {"environment": False, "device": value["device"]}
    elif action == "test" and value.get("source") == "device":
        result["source"] = "device"
    return result


def _index_tools(model: str, gbrain: str, entry: str, port: int) -> wiki.LocalTools | None:
    path = os.environ.get("PATH", "")
    program = gbrain or shutil.which("gbrain.exe" if os.name == "nt" else "gbrain", path=path)
    if not program:
        return None
    if entry and (pathlib.Path(program).stem.lower() != "bun" or pathlib.Path(entry).name.lower() != "cli.ts"
                  or not pathlib.Path(entry).is_absolute()):
        raise ValueError("invalid Bun entry")
    command = (program, "run", entry) if entry else (program,)
    return wiki.LocalTools(command, f"http://127.0.0.1:{port}", model or DEFAULT_MODEL)


def _index_problem(code: object) -> dict[str, str]:
    code = code if isinstance(code, str) and _INDEX_CODE.fullmatch(code) else wiki.CODE_STATE
    return {"code": code, "message": _INDEX_MESSAGES.get(code, "The local wiki index could not complete this action.")}


def _index_reply(value: dict[str, Any]) -> dict[str, Any]:
    state = value.get("state", (value.get("index") or {}).get("state", "ready"))
    if state not in ("ready", "stale", "not_built", "incomplete"):
        state = "unavailable"
    if value.get("ok") is not True:
        if "state" not in value:
            state = "unavailable"
        return {"ok": False, "state": state, **_index_problem(value.get("code"))}
    action = value["action"]
    result: dict[str, Any] = {"ok": True, "state": state}
    source = value.get("source") or value.get("index") or {}
    if source.get("commit"):
        result["source"] = {"commit": source["commit"]}
        if isinstance(source.get("files"), int):
            result["source"]["notes"] = source["files"]
    elif isinstance(source.get("error"), dict):
        result.update(_index_problem(source["error"].get("code")))
    model = value.get("model") or (value.get("active") or {}).get("model")
    if isinstance(model, dict):
        result["model"] = {"name": _plain(model.get("name") or model.get("resolved"), 128)}
        if isinstance(model.get("dimensions"), int):
            result["model"]["dimensions"] = model["dimensions"]
    if action == "search":
        result.update(mode=value["mode"], truncated=value["truncated"],
                      results=[{"file": row["path"], "line": row["line"], "title": _plain(row.get("title"), 256),
                                "heading": _plain(row.get("heading"), 256), "snippet": _plain(row["snippet"], 512)}
                               for row in value["results"]])
        if isinstance(value.get("fallback"), dict):
            result.update(_index_problem(value["fallback"].get("code")))
    return result


def run_wiki_index(action: str, root: pathlib.Path, stream: BinaryIO) -> dict[str, Any]:
    request = _json_input(stream)
    required = {"wiki_dir", "exclude", "model", "device"} | ({"query"} if action == "search" else set())
    if set(request) != required or not isinstance(request["model"], str):
        raise ValueError("invalid index request")
    selection = {"wiki_dir": request["wiki_dir"]}
    if request["exclude"]:
        selection["exclude"] = request["exclude"]
    device = request["device"]
    if not isinstance(device, dict) or set(device) != {"gbrain", "bun_entry", "ollama_port"}:
        raise ValueError("invalid device settings")
    port = device["ollama_port"]
    if (any(not isinstance(device[name], str) for name in ("gbrain", "bun_entry"))
            or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("invalid local tools")
    tools = _index_tools(request["model"], device["gbrain"], device["bun_entry"], port)
    if action == "probe":
        value = wiki.probe(root, tools)
    elif action == "status":
        value = wiki.status(root, selection=selection, tools=tools)
    elif action == "build":
        value = wiki.build(root, tools, selection=selection)
    elif action == "search":
        value = wiki.search(root, request["query"], tools=tools, selection=selection, limit=20)
    else:
        raise ValueError("unsupported index action")
    return _index_reply(value)
