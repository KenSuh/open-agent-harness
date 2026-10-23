# 게이트 원장 파서 — unlazy 의 엄격한 파서를 그대로 따라 읽기만 하는 구조 상태(박스·증거·ABANDON)를 계산한다.
"""A read-only parser for unlazy gate ledgers.

It mirrors the strict parser of unlazy at the pinned commit (scripts/lib/gates.mjs `parseGates`,
`gateDefinitionDigest`, `classifyGateEvidence` and `gateState`), so the structural fallback and unlazy
agree on ordinary ledgers. Known gaps, all of which fail closed when unlazy is available: EXPECT
regular expressions are not compiled (JavaScript and Python syntax differ), and length limits count
code points rather than UTF-16 units.

oah adds one optional attribute that unlazy ignores: an indented `OAH-INPUTS: a, b/c` line narrows a
gate's input set to those repository-relative path prefixes.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re

GATE_RE = re.compile(r"^- \[( |x|X)\] (.*)$")
ATTR_RE = re.compile(r"^(\s+)(CHECK|EXPECT|EVIDENCE|CWD):\s?(.*)$")
UNINDENTED_ATTR_RE = re.compile(r"^(CHECK|EXPECT|EVIDENCE|CWD):\s?(.*)$")
INPUTS_RE = re.compile(r"^\s+OAH-INPUTS:\s?(.*)$")
ABANDON_RE = re.compile(r"^ABANDON:\s*(\S*)\s*(.*)$")
INDENTED_ABANDON_RE = re.compile(r"^\s+ABANDON:")
OWNS_RE = re.compile(r"^OWNS:\s*(.*)$")
FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
FENCE_CLOSE_RE = re.compile(r"^( {0,3})(`+|~+)[ \t]*$")
ID_RE = re.compile(r"^(\S+?):(?:\s+|$)")
VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SUCCESS_RE = re.compile(r"^ exit=0; EXPECT=matched; output-sha256=[a-f0-9]{64}; "
                        r"output-bytes=(0|[1-9][0-9]{0,6}); shell=.")
MAX_AUTOMATIC_EVIDENCE_CHARS = 900
MAX_CHECK_OUTPUT_BYTES = 1024 * 1024
# JavaScript's String.prototype.trim: WhiteSpace and LineTerminator code points.
_JS_SPACE = "\t\n\v\f\r              " \
            "    　﻿"


def _trim(text: str) -> str:
    return text.strip(_JS_SPACE)


@dataclasses.dataclass
class Gate:
    id: str
    title: str
    checked: bool
    line: int
    check: str | None = None
    expect: str | None = None
    cwd: str | None = None
    evidence: str | None = None
    evidence_line: int = -1
    inputs: tuple[str, ...] | None = None

    @property
    def runnable(self) -> bool:
        return bool(self.check) and bool(self.expect)

    @property
    def definition(self) -> str | None:
        return definition_digest(self.check, self.expect, self.cwd)


@dataclasses.dataclass(frozen=True)
class Ledger:
    path: pathlib.Path | None
    gates: tuple[Gate, ...]
    abandoned: dict[str, str]
    errors: tuple[str, ...]
    owns: tuple[str, ...] = ()

    def gate(self, gate_id: str) -> Gate | None:
        return next((gate for gate in self.gates if gate.id == gate_id), None)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def definition_digest(check: str | None, expect: str | None, cwd: str | None) -> str | None:
    """unlazy's environment-independent definition digest; None for a manual gate."""
    if not check or not expect:
        return None
    payload = json.dumps(["unlazy.gate-definition", 1, check, expect, cwd], ensure_ascii=False,
                         separators=(",", ":"))
    return sha256_text(payload)


def classify_evidence(gate: Gate) -> str:
    """pending, automatic-current, automatic-stale or human."""
    evidence = gate.evidence or ""
    if evidence == "" or evidence.lower() == "pending":
        return "pending"
    digest = gate.definition
    if digest is not None:
        prefix = f"automatic-evidence=v1; definition-sha256={digest};"
        success = SUCCESS_RE.match(evidence[len(prefix):])
        if (len(evidence) <= MAX_AUTOMATIC_EVIDENCE_CHARS and evidence.startswith(prefix) and success
                and int(success.group(1)) <= MAX_CHECK_OUTPUT_BYTES):
            return "automatic-current"
    if evidence.startswith("automatic-evidence=") or evidence.startswith("exit=0; shell="):
        return "automatic-stale"
    return "human"


def gate_state(gate: Gate, abandoned: dict[str, str]) -> str:
    """met, unmet, unmet-no-evidence, stale-unmet or abandoned, exactly as unlazy's gateState."""
    if gate.id in abandoned:
        return "abandoned"
    if not gate.checked:
        return "unmet"
    evidence = classify_evidence(gate)
    if gate.definition is not None:
        return "met" if evidence == "automatic-current" else "stale-unmet"
    if evidence == "pending":
        return "unmet-no-evidence"
    if evidence in ("automatic-current", "automatic-stale"):
        return "stale-unmet"
    return "met"


def _normalize_owns(value: str) -> str | None:
    raw = value.strip().replace("\\", "/")
    raw = raw[2:] if raw.startswith("./") else raw
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw) or "\0" in raw:
        return None
    parts = raw.split("/")
    if ".." in parts:
        return None
    normalized = "/".join(part for part in parts if part not in ("", "."))
    return normalized or None


def _parse_inputs(value: str) -> tuple[tuple[str, ...], list[str]]:
    entries, bad = [], []
    for item in (part.strip() for part in value.split(",")):
        if not item:
            continue
        normalized = _normalize_owns(item)
        if normalized is None:
            bad.append(item)
        elif normalized not in entries:
            entries.append(normalized)
    return tuple(entries), bad


def parse_text(text: str, path: pathlib.Path | None = None) -> Ledger:
    """Parse ledger text. Never raises for bad content; problems are listed in `errors`."""
    lines = re.split(r"\r?\n", text)
    gates: list[Gate] = []
    abandoned: dict[str, str] = {}
    owns: list[str] = []
    errors: list[str] = []
    ids: dict[str, int] = {}
    seen_attrs: dict[int, set[str]] = {}
    current: Gate | None = None
    seen_gate = False
    fence: tuple[str, int] | None = None

    for index, line in enumerate(lines):
        number = index + 1
        if fence:
            close = FENCE_CLOSE_RE.match(line)
            if close and close.group(2)[0] == fence[0] and len(close.group(2)) >= fence[1]:
                fence = None
            continue
        opened = FENCE_OPEN_RE.match(line)
        if opened and not (opened.group(2)[0] == "`" and "`" in opened.group(3)):
            fence = (opened.group(2)[0], len(opened.group(2)))
            continue

        match = GATE_RE.match(line)
        if match:
            seen_gate = True
            raw_title = _trim(match.group(2))
            id_match = ID_RE.match(raw_title)
            gate_id = id_match.group(1) if id_match else f"L{number}"
            title = _trim(raw_title[id_match.end():]) if id_match else raw_title
            current = Gate(id=gate_id, title=title, checked=match.group(1).lower() == "x", line=index)
            gates.append(current)
            seen_attrs[id(current)] = set()
            if not id_match:
                errors.append(f"line {number}: gate needs an explicit ID followed by a colon")
            elif not VALID_ID_RE.match(gate_id):
                errors.append(f"line {number}: invalid gate id {gate_id}")
            if not title:
                errors.append(f"line {number}: gate outcome is blank")
            if gate_id in ids:
                errors.append(f"line {number}: duplicate gate id {gate_id} (first declared on line {ids[gate_id]})")
            else:
                ids[gate_id] = number
            continue

        if INDENTED_ABANDON_RE.match(line):
            errors.append(f"line {number}: indented ABANDON is not applied; start ABANDON at column 1")
            current = None
            continue

        unindented = UNINDENTED_ATTR_RE.match(line)
        if unindented:
            errors.append(f"line {number}: unindented {unindented.group(1)} is not attached to a gate; "
                          "indent attribute lines with spaces")
            current = None
            continue

        attr = ATTR_RE.match(line)
        if attr and current is None:
            errors.append(f"line {number}: orphan {attr.group(2)} is not attached to a gate")
            continue
        if attr:
            key = attr.group(2).lower()
            value = _trim(attr.group(3))
            if key in seen_attrs[id(current)]:
                errors.append(f"line {number}: duplicate {attr.group(2)} for gate {current.id}")
            seen_attrs[id(current)].add(key)
            if key == "evidence":
                current.evidence, current.evidence_line = value, index
            else:
                setattr(current, key, value)
            continue

        inputs = INPUTS_RE.match(line)
        if inputs and current is not None:
            entries, bad = _parse_inputs(inputs.group(1))
            if bad or not entries:
                errors.append(f"line {number}: OAH-INPUTS needs repository-relative paths without '..' "
                              f"(got {', '.join(bad) or 'nothing'})")
            elif current.inputs is not None:
                errors.append(f"line {number}: duplicate OAH-INPUTS for gate {current.id}")
            else:
                current.inputs = entries
            continue

        abandon = ABANDON_RE.match(line)
        if abandon:
            gate_id = abandon.group(1).removesuffix(":")
            reason = _trim(abandon.group(2))
            if not gate_id:
                errors.append(f"line {number}: ABANDON needs a gate id and reason")
            elif not reason:
                errors.append(f"line {number}: ABANDON {gate_id} needs a non-blank reason")
            elif gate_id in abandoned:
                errors.append(f"line {number}: duplicate ABANDON for {gate_id}")
            else:
                abandoned[gate_id] = reason
            current = None
            continue

        owns_match = OWNS_RE.match(line)
        if owns_match:
            if seen_gate:
                errors.append(f"line {number}: OWNS must appear before the first gate")
                current = None
                continue
            declared = [item.strip() for item in owns_match.group(1).split(",") if item.strip()]
            if not declared:
                errors.append(f"line {number}: OWNS declares no paths")
            for item in declared:
                normalized = _normalize_owns(item)
                if normalized is None:
                    errors.append(f"line {number}: invalid OWNS path {item}")
                else:
                    owns.append(normalized)
            continue
        if line.startswith("#") or line.startswith("- "):
            current = None

    if fence:
        errors.append("unclosed fenced block")
    for gate in gates:
        has_check = bool(gate.check)
        has_expect = bool(gate.expect)
        if has_check != has_expect:
            errors.append(f"gate {gate.id}: runnable gates require both non-blank CHECK and EXPECT")
        if gate.check == "" or gate.expect == "":
            errors.append(f"gate {gate.id}: CHECK and EXPECT cannot be blank")
    for gate_id in abandoned:
        if gate_id not in ids:
            errors.append(f"ABANDON references unknown gate {gate_id}")
    if not gates:
        errors.append("ledger contains zero live gates")
    return Ledger(path=path, gates=tuple(gates), abandoned=abandoned, errors=tuple(errors), owns=tuple(owns))


def parse_ledger(path: pathlib.Path) -> Ledger:
    """Read and parse a ledger file as UTF-8 (CRLF or LF)."""
    path = pathlib.Path(path)
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        return Ledger(path=path, gates=(), abandoned={}, errors=(f"cannot read {path}: {exc.strerror or exc}",))
    return parse_text(text, path)
