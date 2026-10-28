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
    header labels to inference efforts with the first rule that matches engine, CLI version and model.
    A normalized header lower than the normalized launch effort is `OAH_EFFORT_DOWNGRADED`; no rule
    means `unverified`. A run is effort-verified only with a matching header and a known normalization.

Normalization rules
    A rule's `match` field picks how it matches. Without `match` (rules a caller passes in), a CLI
    version matches a listed prefix (`"*"` matches any) and a model matches a `fnmatch` pattern, and
    the basis is the rule id. With `"match": "exact"`, both must equal a listed string exactly, so a
    rule for `0.164.0` never matches `0.164.1`, `0.164.01` or `0.164.0-dev`. Any other `match` value
    never matches. The shipped rules (`templates/engines/normalization.json`) must be exact, map only
    `PROVEN_LABELS` to themselves and cite public source at a full commit (`parse_rules`); their basis
    says the mapping is expected from that source, not that the backend served that effort.
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
# Labels whose request mapping is proven from Codex source; ultra, persistent, none and minimal are not.
PROVEN_LABELS = ("low", "medium", "high", "xhigh", "max")
EXACT = "exact"
NORMALIZATION_INVALID = "OAH_NORMALIZATION_RULE_INVALID"
RULES_VERSION = 2
REQUIRED_ASSUMPTIONS = frozenset({"explicit-supported-effort", "fresh-codex-exec-session",
                                  "no-prior-runtime-effort-pin"})
_RULE_KEYS = frozenset({"id", "engine", "match", "cli_versions", "models", "map", "evidence", "assumptions", "note"})
_EVIDENCE_KEYS = frozenset({"kind", "repository", "tag", "commit", "references", "claim"})
_EXACT_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_EXACT_MODEL = _RULE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REFERENCE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*:[1-9][0-9]*(?:-[1-9][0-9]*)?")
_ASSUMPTION = re.compile(r"[a-z][a-z0-9-]{0,63}")


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


def _refuse(where: str, problem: str) -> EngineError:
    return EngineError(NORMALIZATION_INVALID,
                       f"{where} {problem}; shipped normalization rules must name one exact CLI version and exact "
                       "models, map only proven labels to themselves and cite public source at a full commit, so "
                       "reinstall oah to restore templates/engines/normalization.json")


def _strings(value: object, pattern: re.Pattern[str]) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and pattern.fullmatch(item) for item in value))


def _check_rule(rule: object, position: int) -> None:
    if not isinstance(rule, dict):
        raise _refuse(f"rule {position}", "is not an object")
    rule_id = rule.get("id")
    if not (isinstance(rule_id, str) and _RULE_ID.fullmatch(rule_id)):
        raise _refuse(f"rule {position}", "has no valid id")
    where = f"rule {rule_id!r}"
    extra = sorted(set(rule) - _RULE_KEYS)
    if extra:
        raise _refuse(where, f"has unknown keys {', '.join(extra)}")
    if rule.get("engine") != "codex":
        raise _refuse(where, "has an engine other than codex, the only engine with source evidence")
    if rule.get("match") != EXACT:
        raise _refuse(where, 'does not set "match": "exact"')
    if not _strings(rule.get("cli_versions"), _EXACT_VERSION):
        raise _refuse(where, "has cli_versions that are not plain MAJOR.MINOR.PATCH versions (no prefix, wildcard, "
                             "pre-release or leading zero)")
    if not _strings(rule.get("models"), _EXACT_MODEL):
        raise _refuse(where, "has models that are not exact model names (no glob or wildcard)")
    mapping = rule.get("map")
    if not (isinstance(mapping, dict) and mapping
            and all(label in PROVEN_LABELS and value == label for label, value in mapping.items())):
        raise _refuse(where, f"has a map that is not an identity over {', '.join(PROVEN_LABELS)}")
    evidence = rule.get("evidence")
    if not (isinstance(evidence, dict) and not set(evidence) - _EVIDENCE_KEYS
            and evidence.get("kind") == "public-source"
            and isinstance(evidence.get("repository"), str) and _REPOSITORY.fullmatch(evidence["repository"])
            and isinstance(evidence.get("commit"), str) and _COMMIT.fullmatch(evidence["commit"])
            and _strings(evidence.get("references"), _REFERENCE)
            and all(isinstance(evidence.get(name, ""), str) for name in ("tag", "claim"))):
        raise _refuse(where, "has no valid evidence object (kind public-source, repository, full 40-character "
                             "commit, file:line references)")
    assumptions = rule.get("assumptions")
    if not (_strings(assumptions, _ASSUMPTION) and REQUIRED_ASSUMPTIONS <= set(assumptions)):
        raise _refuse(where, f"has assumptions missing {', '.join(sorted(REQUIRED_ASSUMPTIONS))}")
    if not isinstance(rule.get("note", ""), str):
        raise _refuse(where, "has a note that is not text")


def parse_rules(text: str) -> list[dict]:
    """Validated shipped rules from the normalization file's text; anything unproven refuses."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise _refuse("the normalization file", f"is not valid JSON ({exc.msg})") from None
    if not isinstance(document, dict) or document.get("version") != RULES_VERSION:
        raise _refuse("the normalization file", f"does not have version {RULES_VERSION}")
    rules = document.get("rules")
    if not isinstance(rules, list) or set(document) - {"version", "rules"}:
        raise _refuse("the normalization file", "needs exactly a version and a rules list")
    seen: set[str] = set()
    for position, rule in enumerate(rules, 1):
        _check_rule(rule, position)
        if rule["id"] in seen:
            raise _refuse(f"rule {rule['id']!r}", "is a duplicate id")
        seen.add(rule["id"])
    return list(rules)


def load_rules() -> list[dict]:
    """The shipped normalization rules, validated by `parse_rules`."""
    text = resources.files("oah").joinpath("templates/engines/normalization.json").read_text(encoding="utf-8")
    return parse_rules(text)


def _matches(rule: Mapping, cli_version: str, model: str) -> bool:
    versions, models = rule.get("cli_versions", ()), rule.get("models", ())
    if "match" not in rule:  # legacy caller-supplied rules: version prefix and model glob
        return (any(prefix == "*" or cli_version.startswith(prefix) for prefix in versions)
                and any(fnmatch.fnmatchcase(model, pattern) for pattern in models))
    if rule["match"] == EXACT:
        return any(cli_version == item for item in versions) and any(model == item for item in models)
    return False  # an unknown policy never matches


def _match(engine: str, cli_version: str | None, model: str | None, label: str | None,
           rules: Sequence[Mapping]) -> tuple[str | None, Mapping | None]:
    if not (cli_version and model and label):
        return None, None
    for rule in rules:
        if rule.get("engine") != engine or not _matches(rule, cli_version, model):
            continue
        mapping = rule.get("map", {})
        if label in mapping:
            return mapping[label], rule
    return None, None


def _basis(rule: Mapping) -> str:
    evidence = rule.get("evidence")
    if rule.get("match") == EXACT and isinstance(evidence, Mapping) and evidence.get("commit"):
        return (f"{rule.get('id')}: expected mapping from CLI source at {evidence['commit']}; "
                "not observed served effort")
    return rule.get("id") or UNVERIFIED


def normalize(engine: str, cli_version: str | None, model: str | None, label: str | None,
              rules: Sequence[Mapping]) -> tuple[str | None, str | None]:
    """(inference effort, rule id) from the first matching rule, or (None, None) when unknown."""
    value, rule = _match(engine, cli_version, model, label, rules)
    return (value, rule.get("id")) if rule is not None else (None, None)


def verify_inference(engine: str, cli_version: str | None, model: str | None, launch: str, header: HeaderCheck,
                     rules: Sequence[Mapping]) -> InferenceCheck:
    """The header-derived inference effort and whether the run counts as effort-verified."""
    if header.status == "unverified":
        return InferenceCheck(UNVERIFIED, "unverified: the header reported no reasoning effort", None, False)
    if not header.header_model:
        return InferenceCheck(UNVERIFIED, "unverified: the header reported no model", None, False)
    if header.header_model != model:
        return InferenceCheck(UNVERIFIED, f"unverified: header model {header.header_model!r} differs from selected "
                              f"model {model!r}", None, False)
    launch_value, _ = _match(engine, cli_version, model, launch, rules)
    header_value, rule = _match(engine, cli_version, model, header.header_effort, rules)
    if header_value is None:
        return InferenceCheck(UNVERIFIED, f"unverified: no normalization rule for {engine} {cli_version} {model} "
                              f"effort {header.header_effort}", None, False)
    code = None
    lower = rank(header_value), rank(launch_value)
    if None not in lower and lower[0] < lower[1]:
        code = DOWNGRADED
    verified = header.status == "match" and launch_value is not None and code is None
    return InferenceCheck(header_value, _basis(rule), code, verified)
