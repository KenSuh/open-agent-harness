# 리뷰 출력 스키마 — codex --output-schema 에 넘길 엄격한 JSON 스키마를 싣고, 표준 라이브러리만으로 모양·판정 일관성·ID 형식을 검사한다.
"""The reviewer's final message (PLAN 5.9 steps 5 and 7).

`SCHEMA` (shipped as `templates/review/output-schema.json`) is strict structured output: every property is
required and no object takes extra properties. `parse(raw, number=n)` decodes the final message, checks it
against the schema with a small stdlib checker (types, enums, required, no extras, array items), then checks
the rules that need no history:
- the verdict is `approve` exactly when no finding is P0 or P1;
- finding ids are `R<n>-F<k>` (k from 1), unique, with a non-empty title and detail;
- `classes` define only this round's new classes `R<n>-C<k>`, once each, with a non-empty mechanism and
  rule, and every finding that uses a new class finds it defined there.
Prior references and reused classes need the history; `oah.review.rounds` checks them. Every problem found
is raised together in `OutputInvalid` (`OAH_REVIEW_OUTPUT_INVALID`).
"""
from __future__ import annotations

import dataclasses
import json
import re
from importlib import resources
from typing import Iterable

from oah.review import ReviewError

VERDICTS = ("approve", "request_changes")
SEVERITIES = ("P0", "P1", "P2", "P3")
BLOCKING = ("P0", "P1")
AXES = ("goal", "tests", "quality", "security", "context", "regressions")
FIX_KINDS = ("local", "design", "stored-format")
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def text() -> str:
    return resources.files("oah").joinpath("templates/review/output-schema.json").read_text(encoding="utf-8")


SCHEMA: dict = json.loads(text())


class OutputInvalid(ReviewError):
    def __init__(self, problems: Iterable[str], code: str = "OAH_REVIEW_OUTPUT_INVALID") -> None:
        self.problems = tuple(problems)
        super().__init__(code, "the reviewer's output is not valid, so this round does not count: "
                         + "; ".join(self.problems))


def _matches(value: object, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "null":
        return value is None
    return isinstance(value, _TYPES[kind])


def shape_problems(value: object, node: dict | None = None, where: str = "output") -> list[str]:
    """Problems of `value` against the schema subset this package uses."""
    node = SCHEMA if node is None else node
    kinds = node.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds] if kinds else []
    if kinds and not any(_matches(value, kind) for kind in kinds):
        return [f"{where} must be {' or '.join(kinds)}"]
    problems: list[str] = []
    if "enum" in node and value not in node["enum"]:
        problems.append(f"{where} must be one of {', '.join(map(str, node['enum']))}, not {value!r}")
    if isinstance(value, dict):
        properties = node.get("properties", {})
        problems += [f"{where}.{key} is missing" for key in node.get("required", ()) if key not in value]
        if node.get("additionalProperties") is False:
            problems += [f"{where}.{key} is not allowed" for key in value if key not in properties]
        for key, child in properties.items():
            if key in value:
                problems += shape_problems(value[key], child, f"{where}.{key}")
    if isinstance(value, list) and "items" in node:
        for number, item in enumerate(value):
            problems += shape_problems(item, node["items"], f"{where}[{number}]")
    return problems


@dataclasses.dataclass(frozen=True)
class PriorRef:
    round: int
    receipt_digest: str
    finding_id: str
    class_id: str

    def to_json(self) -> dict:
        return {"round": self.round, "receipt_digest": self.receipt_digest, "finding_id": self.finding_id,
                "class_id": self.class_id}


@dataclasses.dataclass(frozen=True)
class Finding:
    finding_id: str
    class_id: str
    severity: str
    axis: str
    title: str
    detail: str
    location: str
    in_scope: bool
    fix_kind: str
    prior_refs: tuple[PriorRef, ...]

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING

    def to_json(self) -> dict:
        data = {field.name: getattr(self, field.name) for field in dataclasses.fields(self)}
        data["prior_refs"] = [ref.to_json() for ref in self.prior_refs]
        return data


@dataclasses.dataclass(frozen=True)
class ClassDef:
    class_id: str
    mechanism: str
    rule: str

    def to_json(self) -> dict:
        return {"class_id": self.class_id, "mechanism": self.mechanism, "rule": self.rule}


@dataclasses.dataclass(frozen=True)
class Output:
    verdict: str
    base: str
    head: str
    summary: str
    findings: tuple[Finding, ...]
    classes: tuple[ClassDef, ...]
    intent_return: str
    evidence: str
    db_impact: str

    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.blocking]


def _build(data: dict) -> Output:
    findings = tuple(Finding(**dict(item, prior_refs=tuple(PriorRef(**ref) for ref in item["prior_refs"])))
                     for item in data["findings"])
    classes = tuple(ClassDef(**item) for item in data["classes"])
    return Output(verdict=data["verdict"], base=data["base"], head=data["head"], summary=data["summary"],
                  findings=findings, classes=classes, intent_return=data["intent_return"],
                  evidence=data["evidence"], db_impact=data["db_impact"])


def _rules(output: Output, number: int) -> list[str]:
    problems: list[str] = []
    blocking = output.blocking()
    if output.verdict == "approve" and blocking:
        problems.append(f"the verdict is approve, but {', '.join(f.finding_id for f in blocking)} "
                        "are P0 or P1; approve only when no finding is P0 or P1")
    if output.verdict == "request_changes" and not blocking:
        problems.append("the verdict is request_changes, but no finding is P0 or P1")
    finding_id = re.compile(rf"R{number}-F[1-9][0-9]*")
    seen: set[str] = set()
    for finding in output.findings:
        if not finding_id.fullmatch(finding.finding_id):
            problems.append(f"finding id {finding.finding_id!r} must be R{number}-F<k> (k from 1) in round R{number}")
        elif finding.finding_id in seen:
            problems.append(f"finding id {finding.finding_id} is used twice")
        seen.add(finding.finding_id)
        if not finding.title.strip() or not finding.detail.strip():
            problems.append(f"finding {finding.finding_id} needs a non-empty title and detail")
        problems += [f"finding {finding.finding_id} refers to round {ref.round}, which is not a round number"
                     for ref in finding.prior_refs if ref.round < 1]
    class_id = re.compile(rf"R{number}-C[1-9][0-9]*")
    defined: set[str] = set()
    for item in output.classes:
        if not class_id.fullmatch(item.class_id):
            problems.append(f"class {item.class_id!r} is defined in round R{number}, so its id must be R{number}-C<k>; "
                            "reuse an earlier class by its id without defining it again")
        elif item.class_id in defined:
            problems.append(f"class {item.class_id} is defined twice")
        defined.add(item.class_id)
        if not item.mechanism.strip() or not item.rule.strip():
            problems.append(f"class {item.class_id} needs a non-empty mechanism and rule")
    problems += [f"finding {f.finding_id} uses class {f.class_id}, which is not defined in classes"
                 for f in output.findings if class_id.fullmatch(f.class_id) and f.class_id not in defined]
    return problems


def parse(raw: bytes, *, number: int) -> Output:
    """The validated output of round R<number>; raises OutputInvalid with every problem found."""
    try:
        decoded = raw.decode("utf-8").lstrip("﻿")
    except UnicodeDecodeError:
        raise OutputInvalid(["the final message is not UTF-8 text"]) from None
    try:
        data = json.loads(decoded)
    except ValueError as exc:
        raise OutputInvalid([f"the final message is not one JSON object ({exc})"]) from None
    problems = shape_problems(data)
    if problems:
        raise OutputInvalid(problems)
    output = _build(data)
    problems = _rules(output, number)
    if problems:
        raise OutputInvalid(problems)
    return output
