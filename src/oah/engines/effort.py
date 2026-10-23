# effort 공용 규칙 — 모델이 지원하는 목록에서 effort 를 고르고, 헤더 설정값과 비교하며, 버전별 규칙으로 추론 effort 를 정규화한다.
"""Effort rules shared by the engine adapters (PLAN 5.5, A1).

Levels
    `ORDINARY` is the ordered scale `none < minimal < low < medium < high < xhigh < max`. A label in
    `BEHAVIOUR_CHANGING` (`ultra`, which adds task delegation) is never chosen by `highest` and never
    compared as an ordinal. Any other label is unknown: it can be passed through but not ranked.

Resolution
    `resolve(requested, levels)` turns a tier effort into the literal launch effort. With usable
    metadata (a non-empty list) a literal must be listed (`OAH_EFFORT_UNSUPPORTED`); without it a
    literal passes unchanged. `highest` takes the top listed ordinary level and refuses with
    `OAH_EFFORT_METADATA_MISSING` when there is none. Nothing is ever guessed or lowered.

Checks
    `compare_labels(header, launch)` compares the configuration the CLI reports with the launch effort:
    `match`, `downgraded` (`OAH_EFFORT_DOWNGRADED`, both ordinary and lower), `mismatch`
    (`OAH_EFFORT_MISMATCH`) or `unverified` (no readable header). `verify_inference(...)` maps launch and
    header labels to inference efforts with the first rule that matches engine, CLI version prefix and
    model pattern (`templates/engines/normalization.json`). A normalized header lower than the
    normalized launch effort is `OAH_EFFORT_DOWNGRADED`; no rule means `unverified`. A run is
    effort-verified only with a matching header and a known normalization.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Mapping, Sequence

ORDINARY = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
BEHAVIOUR_CHANGING = frozenset({"ultra"})
HIGHEST = "highest"
UNVERIFIED = "unverified"
LABEL_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
DOWNGRADED, MISMATCH = "OAH_EFFORT_DOWNGRADED", "OAH_EFFORT_MISMATCH"


class EngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class EffortResolution:
    requested: str
    launch: str
    levels: tuple[str, ...] | None
    metadata: str  # "listed" when a usable level list was read, else "missing"


@dataclass(frozen=True)
class HeaderCheck:
    header_effort: str  # the reported label, or "unverified"
    status: str  # match | downgraded | mismatch | unverified
    code: str | None
    header_model: str | None


@dataclass(frozen=True)
class InferenceCheck:
    resolved_inference_effort: str
    normalization_basis: str
    code: str | None
    effort_verified: bool


def rank(label: str | None) -> int | None:
    return ORDINARY.index(label) if label in ORDINARY else None


def highest(levels: Sequence[str] | None) -> str | None:
    """The top ordinary level in `levels`; behaviour-changing and unknown labels are skipped."""
    ranked = [label for label in levels or () if rank(label) is not None]
    return max(ranked, key=rank) if ranked else None


def resolve(requested: str, levels: Sequence[str] | None, *, model: str = "the model") -> EffortResolution:
    """The literal launch effort for a tier effort, from the model's supported levels."""
    if requested != HIGHEST and not LABEL_RE.fullmatch(requested or ""):
        raise EngineError("OAH_EFFORT_INVALID", f"effort {requested!r} is not a valid level name; set the tier's "
                          "effort in .oah/config.toml to a level such as high, or to highest")
    usable = tuple(levels) if levels else None
    if requested == HIGHEST:
        top = highest(usable)
        if top is None:
            raise EngineError("OAH_EFFORT_METADATA_MISSING",
                              f"no ordinary reasoning levels are known for {model}, so 'highest' cannot be "
                              "resolved and oah never guesses a maximum; run `oah models --refresh` after "
                              "updating the CLI's model metadata, or set an explicit model and a literal "
                              "effort for this tier in .oah/config.toml")
        return EffortResolution(requested, top, usable, "listed")
    if usable is None:
        return EffortResolution(requested, requested, None, "missing")
    if requested not in usable:
        raise EngineError("OAH_EFFORT_UNSUPPORTED",
                          f"{model} supports {', '.join(usable)}, not {requested}; set the tier's effort in "
                          ".oah/config.toml to one of those levels, or to highest")
    return EffortResolution(requested, requested, usable, "listed")


def compare_labels(header: str | None, launch: str) -> tuple[str, str | None]:
    """Compare the header's configured effort with the launch effort, never ranking unknown labels."""
    if not header or header == UNVERIFIED:
        return "unverified", None
    if header == launch:
        return "match", None
    header_rank, launch_rank = rank(header), rank(launch)
    if header_rank is not None and launch_rank is not None and header_rank < launch_rank:
        return "downgraded", DOWNGRADED
    return "mismatch", MISMATCH


def load_rules() -> list[dict]:
    """The shipped normalization rules."""
    text = resources.files("oah").joinpath("templates/engines/normalization.json").read_text(encoding="utf-8")
    return list(json.loads(text)["rules"])


def normalize(engine: str, cli_version: str | None, model: str | None, label: str | None,
              rules: Sequence[Mapping]) -> tuple[str | None, str | None]:
    """(inference effort, rule id) from the first matching rule, or (None, None) when unknown."""
    if not (cli_version and model and label):
        return None, None
    for rule in rules:
        if rule.get("engine") != engine:
            continue
        if not any(prefix == "*" or cli_version.startswith(prefix) for prefix in rule.get("cli_versions", ())):
            continue
        if not any(fnmatch.fnmatchcase(model, pattern) for pattern in rule.get("models", ())):
            continue
        mapping = rule.get("map", {})
        if label in mapping:
            return mapping[label], rule.get("id")
    return None, None


def verify_inference(engine: str, cli_version: str | None, model: str | None, launch: str, header: HeaderCheck,
                     rules: Sequence[Mapping]) -> InferenceCheck:
    """The header-derived inference effort and whether the run counts as effort-verified."""
    if header.status == "unverified":
        return InferenceCheck(UNVERIFIED, "unverified: the header reported no reasoning effort", None, False)
    launch_value, _ = normalize(engine, cli_version, model, launch, rules)
    header_value, basis = normalize(engine, cli_version, model, header.header_effort, rules)
    if header_value is None:
        return InferenceCheck(UNVERIFIED, f"unverified: no normalization rule for {engine} {cli_version} {model} "
                              f"effort {header.header_effort}", None, False)
    code = None
    lower = rank(header_value), rank(launch_value)
    if None not in lower and lower[0] < lower[1]:
        code = DOWNGRADED
    verified = header.status == "match" and launch_value is not None and code is None
    return InferenceCheck(header_value, basis or UNVERIFIED, code, verified)
