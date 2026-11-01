# Codex 어댑터 — models_cache 에서 지원 effort 를 읽고, 유효 설정(프로필 포함)으로 모델을 해석하며, codex exec 명령줄과 헤더 검사를 만든다.
"""The Codex CLI adapter (PLAN 5.5). Every function here is pure apart from reading files it names.

Lane P6 uses `build_command`, `resolve_effort` and `check_header` for review rounds.

- `parse_models_cache(text)` reads `$CODEX_HOME/models_cache.json`: a `models` list whose entries carry
  `slug` and `supported_reasoning_levels` (objects with `effort`, or plain strings). An entry marked
  `is_default` or `default` true is the CLI-reported default (A14). An entry whose `visibility` is
  `hide` or `hidden`, or that has `hidden` true, is not listed: it is never a judged candidate.
- `effective_model(launch_dir, codex_home)` resolves an empty tier model from Codex's configuration:
  `$CODEX_HOME/config.toml`, then `<launch_dir>/.codex/config.toml` (later wins), then the selected
  `profile`'s `[profiles.<name>]` model. `None` means nothing is configured.
- `resolve_effort(model, requested, *, cache)` applies `oah.engines.effort.resolve` to the model's levels.
- `build_command(...)` is `codex exec -C <dir> -s read-only|workspace-write -m M
  -c model_reasoning_effort="E" [--output-schema F] -o <out> [--ephemeral] -`; the prompt goes on stdin.
- `check_header(stderr_text, launch_effort)` reads the header's `reasoning effort: <e>` line, which
  reports the configured effort only, and compares it with the launch effort.
"""
from __future__ import annotations

import json
import pathlib
import re
import tomllib
from dataclasses import dataclass
from typing import Mapping, Sequence

from oah.engines.effort import (LABEL_RE, UNVERIFIED, EffortResolution, EngineError, HeaderCheck, compare_labels,
                                resolve)

CODEX_HOME_ENV = "CODEX_HOME"
CACHE_NAME = "models_cache.json"
_HEADER_EFFORT = re.compile(r"^\s*reasoning effort:\s*(\S+)\s*$", re.IGNORECASE)
_HEADER_MODEL = re.compile(r"^\s*model:\s*(\S+)\s*$", re.IGNORECASE)
_VERSION = re.compile(r"(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)")


@dataclass(frozen=True)
class CachedModel:
    slug: str
    levels: tuple[str, ...]
    default: bool
    listed: bool = True


def codex_home(env: Mapping[str, str]) -> pathlib.Path:
    override = env.get(CODEX_HOME_ENV)
    return pathlib.Path(override) if override else pathlib.Path.home() / ".codex"


def parse_version(text: str) -> str | None:
    """The version number in `codex --version` output, such as 0.163.0."""
    match = _VERSION.search(text or "")
    return match.group(1) if match else None


def _levels(raw: object) -> tuple[str, ...]:
    levels: list[str] = []
    for item in raw if isinstance(raw, list) else ():
        label = item.get("effort") if isinstance(item, dict) else item
        if isinstance(label, str) and LABEL_RE.fullmatch(label) and label not in levels:
            levels.append(label)
    return tuple(levels)


def parse_models_cache(text: str) -> dict[str, CachedModel]:
    """Models by slug. Raises ValueError when the text is not JSON; an unknown shape gives {}."""
    data = json.loads(text)
    entries = data.get("models") if isinstance(data, dict) else data
    models: dict[str, CachedModel] = {}
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug") or entry.get("id") or entry.get("model")
        if not isinstance(slug, str) or not slug or slug in models:
            continue
        default = entry.get("is_default") is True or entry.get("default") is True
        listed = entry.get("visibility") not in ("hide", "hidden") and entry.get("hidden") is not True
        models[slug] = CachedModel(slug=slug, levels=_levels(entry.get("supported_reasoning_levels")), default=default,
                                   listed=listed)
    return models


def read_models_cache(home: pathlib.Path) -> dict[str, CachedModel] | None:
    """The parsed cache, or None when it is missing, unreadable or not JSON."""
    try:
        return parse_models_cache((pathlib.Path(home) / CACHE_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _read_toml(path: pathlib.Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise EngineError("OAH_CODEX_CONFIG_INVALID", f"{path} cannot be read as UTF-8 ({exc}); fix or remove it, "
                          "or set the tier's model in .oah/config.toml") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EngineError("OAH_CODEX_CONFIG_INVALID", f"{path} is not valid TOML ({exc}); fix it so Codex and oah "
                          "can read it, or set the tier's model in .oah/config.toml") from exc


def effective_model(launch_dir: pathlib.Path, home: pathlib.Path) -> str | None:
    """The model Codex would use in launch_dir with its configured profile, or None."""
    layers = [_read_toml(pathlib.Path(home) / "config.toml"),
              _read_toml(pathlib.Path(launch_dir) / ".codex" / "config.toml")]
    model = profile = None
    profiles: dict[str, dict] = {}
    for layer in layers:
        if isinstance(layer.get("model"), str) and layer["model"]:
            model = layer["model"]
        if isinstance(layer.get("profile"), str) and layer["profile"]:
            profile = layer["profile"]
        for name, table in (layer.get("profiles") or {}).items():
            if isinstance(table, dict):
                profiles.setdefault(name, {}).update(table)
    if profile is not None:
        chosen = profiles.get(profile, {}).get("model")
        if isinstance(chosen, str) and chosen:
            model = chosen
    return model


def resolve_effort(model: str, requested: str, *, cache: Mapping[str, CachedModel] | None) -> EffortResolution:
    """The launch effort for `model`; levels come from the models cache when it has a usable entry."""
    entry = cache.get(model) if cache else None
    return resolve(requested, entry.levels if entry and entry.levels else None, model=model)


def build_command(base: Sequence[str], *, workdir: str, read_only: bool, model: str, effort: str,
                  output_last_message: str, output_schema: str | None = None, ephemeral: bool = False) -> list[str]:
    """The argv for one non-interactive Codex run; the prompt goes on stdin (`-`)."""
    if not model or model.startswith("-"):
        raise EngineError("OAH_MODEL_UNRESOLVED", f"Codex model {model!r} is not a concrete model name; set the "
                          "tier's model in .oah/config.toml")
    if not LABEL_RE.fullmatch(effort or ""):
        raise EngineError("OAH_EFFORT_INVALID", f"effort {effort!r} is not a valid level name")
    argv = [*base, "exec", "-C", workdir, "-s", "read-only" if read_only else "workspace-write", "-m", model,
            "-c", f'model_reasoning_effort="{effort}"']
    if output_schema:
        argv += ["--output-schema", output_schema]
    argv += ["-o", output_last_message]
    if ephemeral:
        argv.append("--ephemeral")
    argv.append("-")
    return argv


def check_header(stderr_text: str, launch_effort: str) -> HeaderCheck:
    """Compare the first header `reasoning effort:` line with the launch effort; none means unverified."""
    effort = model = None
    for line in (stderr_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if effort is None:
            match = _HEADER_EFFORT.match(line)
            if match:
                effort = match.group(1)
        if model is None:
            match = _HEADER_MODEL.match(line)
            if match:
                model = match.group(1)
    status, code = compare_labels(effort, launch_effort)
    return HeaderCheck(header_effort=effort or UNVERIFIED, status=status, code=code, header_model=model)

