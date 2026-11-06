# 라이선스 근거와 적격성 — 고정한 스냅숏의 실제 라이선스 파일 전체를 엄격히 대조하고, 선택 경로마다 가장 가까운 상위·하위 라이선스와 알림 파일, 등록한 매니페스트의 선언을 근거로 형태(단일·선택·결합·미확인)와 정책별 적격성을 계산한다.
"""License evidence of one snapshot, and what a policy makes of it.

Evidence is gathered from the fetched commit only: for every selected path, the license-like files of the deepest
folder at or above it govern; license-like files nested inside a selected folder must read the same; notice files
are recorded. A declared expression is read only from the registered manifest. Nothing here reads a caller's claim,
GitHub's license field or a model's answer.

`classify_text` accepts a file only when its whole text is one supported license (MIT, ISC, BSD-2-Clause,
BSD-3-Clause, Apache-2.0) with only a title and copyright lines before it. A snippet, extra terms, two licenses in
one file or control characters read as unrecognized. A few other licenses are named by their titles, which is
enough to call them excluded, never eligible.

`verdict(evidence, read)` → form single | choice | conjunction | unknown; `eligibility(verdict, allow)` → eligible |
eligible-by-choice | excluded | unknown. Both are recomputed on every read, so a policy or matcher change needs no
new observation.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import tomllib
import unicodedata
from typing import Callable, Mapping, Sequence

from oah.design.sources import license_texts as texts

SUPPORTED = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC")
FILE_LIMIT = 1 << 20
MANIFEST_LIMIT = 256 * 1024
TREE_LIMIT = 20_000
EXPRESSION_LIMIT = 200
_LICENSE_NAME = re.compile(
    r"(?:un)?licen[cs]e(?:[-._](?:mit|apache(?:-?2(?:\.0)?)?|bsd(?:-[23]-clause)?|isc|gpl[a-z0-9.-]*|lgpl[a-z0-9.-]*"
    r"|mpl[a-z0-9.-]*))?(?:\.(?:md|txt|rst|markdown))?|copying(?:\.(?:lesser|lib))?(?:\.(?:md|txt))?",
    re.IGNORECASE)
_NOTICE_NAME = re.compile(r"(?:notice|copyright)(?:\.(?:md|txt|rst))?", re.IGNORECASE)
_TITLES = {"mit license", "the mit license", "mit license (mit)", "the mit license (mit)", "mit", "isc license",
           "the isc license", "isc license (isc)", "the isc license (isc)", "isc", "bsd 2-clause license",
           "bsd 3-clause license", "bsd-2-clause license", "bsd-3-clause license", "the bsd license", "bsd license",
           "the 2-clause bsd license", "the 3-clause bsd license", "new bsd license", "simplified bsd license"}
_HOLDER_ID = r"[A-Z][A-Za-z0-9]*(?:['-][A-Z][A-Za-z0-9]*)*"
_COPYRIGHT_NOTICE = re.compile(
    r"(?i:(?:portions )?copyright(?:\s*(?:\(c\)|©))?|\(c\)|©)\s+"
    r"[0-9]{4}(?:\s*[-,]\s*[0-9]{4})*\s+"
    + _HOLDER_ID +
    r"(?:, (?i:inc|ltd|llc|corp|co))?\.?"
    r"(?: <[\w.+-]+@[\w.-]+>)?"
    r"(?: (?i:all rights reserved)\.?)?")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"',
                         "„": '"', "‟": '"', "–": "-", "—": "-", "‐": "-", "‑": "-",
                         " ": " ", "\f": " "})
_LIST_MARKER = re.compile(r"(?:\(?[0-9]{1,2}[.)]|[*\-•])")
_ID_CANON = {name.casefold(): name for name in SUPPORTED}
_OTHER_IDS = {"gpl-2.0": "GPL-2.0", "gpl-2.0-only": "GPL-2.0", "gpl-2.0-or-later": "GPL-2.0",
              "gpl-3.0": "GPL-3.0", "gpl-3.0-only": "GPL-3.0", "gpl-3.0-or-later": "GPL-3.0",
              "lgpl-2.1": "LGPL-2.1", "lgpl-2.1-only": "LGPL-2.1", "lgpl-2.1-or-later": "LGPL-2.1",
              "lgpl-3.0": "LGPL-3.0", "lgpl-3.0-only": "LGPL-3.0", "lgpl-3.0-or-later": "LGPL-3.0",
              "agpl-3.0": "AGPL-3.0", "agpl-3.0-only": "AGPL-3.0", "agpl-3.0-or-later": "AGPL-3.0",
              "mpl-2.0": "MPL-2.0", "unlicense": "Unlicense", "busl-1.1": "BUSL-1.1", "sspl-1.0": "SSPL-1.0"}


def is_license_name(name: str) -> bool:
    return bool(_LICENSE_NAME.fullmatch(name))


def is_notice_name(name: str) -> bool:
    return bool(_NOTICE_NAME.fullmatch(name))


def normalize(text: str) -> str:
    return " ".join(text.translate(_QUOTES).lower().split())


def _template(text: str, slots: Mapping[str, str]) -> re.Pattern[str]:
    pattern = re.escape(text)
    for name, regex in slots.items():
        pattern = pattern.replace(re.escape("{" + name + "}"), regex)
    return re.compile(pattern)


_MIT = re.compile(re.escape(texts.MIT_BODY))
_BSD2 = _template(texts.BSD_OPENING + " " + texts.BSD_DISCLAIMER, {})
_BSD3 = _template(texts.BSD_OPENING + " " + texts.BSD_THIRD + " " + texts.BSD_DISCLAIMER,
                  {"HOLDER": r"[^.;]{1,200}?", "ITS": r"(?:its|their)"})
_ISC = _template(texts.ISC_BODY, {"AND": r"(?:and/or|and)", "DISCLAIM": r"(?:author disclaims|authors disclaim)",
                                  "AUTHOR": r"authors?"})
_APPENDIX = _template(texts.APACHE_APPENDIX, {"COPYRIGHT": r"copyright [^\n]{1,200}?"})


@dataclasses.dataclass(frozen=True)
class TextVerdict:
    spdx: str | None
    strict: bool          # the whole text is a supported license
    problem: str | None = None


def _preamble_end(lines: list[str]) -> int:
    """Index of the first line after an optional title and copyright block."""
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.strip().strip("#*_ ").strip()
        folded = normalize(line).rstrip(".:")
        if line and folded not in _TITLES and folded != "all rights reserved" and not _COPYRIGHT_NOTICE.fullmatch(line):
            return index
        index += 1
    return index


def _controls(text: str) -> bool:
    return any((ord(c) < 32 and c not in "\t\n\r\f") or ord(c) == 127 or unicodedata.category(c) == "Cf"
               for c in text if c != "﻿")


def classify_text(data: bytes) -> TextVerdict:
    """Which license this whole file is, strictly; or a title-only match that can only exclude."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return TextVerdict(None, False, "license text is not UTF-8")
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if _controls(text):
        return TextVerdict(None, False, "license text holds control or invisible formatting characters")
    lines = text.split("\n")
    body = normalize("\n".join(lines[_preamble_end(lines):]))
    if _MIT.fullmatch(body):
        return TextVerdict("MIT", True)
    if _ISC.fullmatch(body):
        return TextVerdict("ISC", True)
    bsd = " ".join(token for token in body.split(" ") if not _LIST_MARKER.fullmatch(token))
    bsd = bsd.replace("copyright owner", "copyright holder")
    if _BSD3.fullmatch(bsd):
        return TextVerdict("BSD-3-Clause", True)
    if _BSD2.fullmatch(bsd):
        return TextVerdict("BSD-2-Clause", True)
    apache = body.replace("https://www.apache.org/", "http://www.apache.org/")
    if apache.startswith(texts.APACHE_START):
        end = apache.find(texts.APACHE_END)
        if end >= 0:
            end += len(texts.APACHE_END)
            terms, rest = apache[:end], apache[end:].strip()
            if hashlib.sha256(terms.encode("utf-8")).hexdigest() == texts.APACHE_TERMS_SHA256 and (
                    not rest or _APPENDIX.fullmatch(rest)):
                return TextVerdict("Apache-2.0", True)
    whole = normalize(text)
    for spdx, phrases in texts.OTHER_TITLES:
        if all(phrase in whole[:4000] for phrase in phrases):
            return TextVerdict(spdx, False, f"reads as {spdx}, which this kit never treats as eligible")
    return TextVerdict(None, False, "not one complete supported license text (MIT, ISC, BSD-2-Clause, BSD-3-Clause "
                       "or Apache-2.0) with only a title and copyright lines before it")


# Declared expressions -----------------------------------------------------------------------------------------


def parse_expression(text: str) -> tuple[str, tuple[str, ...]] | None:
    """(single | choice | conjunction, ids) of a plain SPDX expression, or None for anything else (WITH, nesting,
    mixed AND/OR, unknown ids). Cargo's legacy `A/B` reads as `A OR B`."""
    if not isinstance(text, str) or not text.strip() or len(text) > EXPRESSION_LIMIT:
        return None
    value = text.strip()
    if "/" in value and " " not in value:
        value = " OR ".join(value.split("/"))
    while value.startswith("(") and value.endswith(")") and value.count("(") == 1:
        value = value[1:-1].strip()
    tokens = value.split()
    if not tokens or len(tokens) % 2 == 0 or any(c in value for c in "()"):
        return None
    ids, operators = tokens[0::2], {token.upper() for token in tokens[1::2]}
    if operators - {"AND", "OR"} or len(operators) > 1:
        return None
    canon = []
    for item in ids:
        name = _ID_CANON.get(item.casefold()) or _OTHER_IDS.get(item.casefold())
        if name is None:
            return None
        canon.append(name)
    if len(canon) == 1:
        return "single", (canon[0],)
    if len(set(canon)) != len(canon):
        return None
    return ("choice" if operators == {"OR"} else "conjunction"), tuple(canon)


def declared_from_manifest(path: str, data: bytes) -> tuple[dict | None, str | None]:
    """({path, field, value} or None, problem) from Cargo.toml, package.json or pyproject.toml bytes."""
    if len(data) > MANIFEST_LIMIT:
        return None, "the registered manifest is larger than 256 KiB"
    name = path.rsplit("/", 1)[-1]
    try:
        text = data.decode("utf-8")
        if name == "package.json":
            parsed = json.loads(text)
            value, field = (parsed.get("license") if isinstance(parsed, dict) else None), "license"
        else:
            parsed = tomllib.loads(text)
            table = parsed.get("package" if name == "Cargo.toml" else "project")
            value = table.get("license") if isinstance(table, dict) else None
            field = "package.license" if name == "Cargo.toml" else "project.license"
            if isinstance(value, dict):
                value = value.get("text") if set(value) == {"text"} else None
    except (UnicodeDecodeError, ValueError, RecursionError, tomllib.TOMLDecodeError):
        return None, "the registered manifest could not be read"
    if value is None:
        return None, f"the registered manifest has no {field} expression"
    if not isinstance(value, str) or not value.strip() or len(value) > EXPRESSION_LIMIT:
        return None, f"{field} in the registered manifest is not a short license expression"
    return {"path": path, "field": field, "value": value.strip()}, None


# Discovery from a fetched tree --------------------------------------------------------------------------------


def _ancestors(path: str, is_dir: bool) -> list[str]:
    parts = path.split("/") if path else []
    folders = [""]
    upto = parts if is_dir else parts[:-1]
    for index in range(len(upto)):
        folders.append("/".join(upto[:index + 1]))
    return folders


def discover(tree, paths: Sequence[str], manifest_path: str | None, store: Callable[[bytes], str], *,
             remaining: Callable[[], float] | None = None) -> dict:
    """License evidence for the manifest. `store(bytes) -> sha256 hex` keeps every text read.

    License problems (missing files, overrides, too large a scope) live inside the evidence and make the verdict
    unknown; they never make the observation unverified."""
    problems: list[str] = []
    files: dict[str, dict] = {}
    notices: dict[str, dict] = {}
    scopes = []

    def keep(mode: str, oid: str, name: str, into: dict) -> None:
        if remaining is not None:
            remaining()
        if name in into or mode not in ("100644", "100755"):
            return
        size = tree.size(oid)
        if size > FILE_LIMIT:
            problems.append(f"{name} is larger than 1 MiB")
            return
        into[name] = {"path": name, "blob": oid, "size": size, "sha256": store(tree.read(oid))}

    for item in (paths or ("",)):
        if remaining is not None:
            remaining()
        entries = tree.entries(item)
        is_dir = item == "" or any(name != item for _mode, _oid, name in entries)
        chain = _ancestors(item, is_dir)
        governing_level, governing = None, []
        for folder in chain:
            if remaining is not None:
                remaining()
            children = tree.children(folder)
            if any(mode == "040000" and name.rsplit("/", 1)[-1].casefold() == "licenses" for mode, _o, name in children):
                problems.append(f"{folder or 'the top folder'} holds a LICENSES folder (per-file licensing), which "
                                "oah does not review")
            found = [(mode, oid, name) for mode, oid, name in children if is_license_name(name.rsplit("/", 1)[-1])]
            for mode, oid, name in children:
                if remaining is not None:
                    remaining()
                if is_notice_name(name.rsplit("/", 1)[-1]):
                    keep(mode, oid, name, notices)
            if found:
                governing_level, governing = folder, found
        for mode, oid, name in governing:
            keep(mode, oid, name, files)
        nested = []
        if is_dir:
            listing = entries
            if len(listing) > TREE_LIMIT:
                problems.append(f"{item or 'the repository'} has more than {TREE_LIMIT} files, too many to check for "
                                "nested licenses")
            else:
                prefix = item.rstrip("/") + "/" if item else ""
                for mode, oid, name in listing:
                    if remaining is not None:
                        remaining()
                    base = name.rsplit("/", 1)[-1]
                    deeper = name.startswith(prefix) and "/" in name[len(prefix):]
                    if not deeper:
                        continue
                    if is_license_name(base):
                        nested.append(name)
                        keep(mode, oid, name, files)
                    elif is_notice_name(base):
                        keep(mode, oid, name, notices)
        scopes.append({"path": item, "level": governing_level, "files": sorted(name for _m, _o, name in governing),
                       "nested": sorted(nested)})
    declared = None
    manifest = None
    if manifest_path:
        found = [(mode, oid) for mode, oid, name in tree.entries(manifest_path) if name == manifest_path]
        if not found or found[0][0] not in ("100644", "100755"):
            problems.append("the registered manifest is missing at this commit")
        elif tree.size(found[0][1]) > MANIFEST_LIMIT:
            problems.append("the registered manifest is larger than 256 KiB")
        else:
            data = tree.read(found[0][1])
            manifest = {"path": manifest_path, "blob": found[0][1], "size": len(data), "sha256": store(data)}
            declared, problem = declared_from_manifest(manifest_path, data)
            if problem:
                problems.append(problem)
    if remaining is not None:
        remaining()
    result = {"scopes": scopes, "files": [files[key] for key in sorted(files)],
              "notices": [notices[key] for key in sorted(notices)], "declared": declared,
              "problems": sorted(set(problems))}
    if manifest is not None:
        result["manifest"] = manifest
    return result


# Verdicts and eligibility -------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Verdict:
    form: str                       # single | choice | conjunction | unknown
    expression: str | None
    ids: tuple[str, ...]
    strict: bool                    # every id is a complete supported text
    problems: tuple[str, ...]
    files: tuple[dict, ...] = ()    # each read file with its text verdict

    def to_json(self) -> dict:
        return {"form": self.form, "expression": self.expression, "ids": list(self.ids), "strict": self.strict,
                "problems": list(self.problems), "files": [dict(item) for item in self.files]}


def _unknown(problems: Sequence[str], files: Sequence[dict] = ()) -> Verdict:
    return Verdict("unknown", None, (), False, tuple(problems), tuple(files))


def verdict(evidence: Mapping | None, read: Callable[[str], bytes | None]) -> Verdict:
    """The license form of one snapshot's evidence; `read(sha256 hex)` returns stored bytes or None."""
    if not isinstance(evidence, Mapping):
        return _unknown(["no license evidence (documents are not adopted code)"])
    problems = list(evidence.get("problems") or [])
    manifest = evidence.get("manifest")
    if manifest is not None:
        data = read(manifest.get("sha256", ""))
        if data is None:
            problems.append("the stored package manifest is missing")
        else:
            declared, problem = declared_from_manifest(manifest.get("path", ""), data)
            if declared != evidence.get("declared"):
                problems.append("the declared license differs from the original package manifest")
            if problem:
                problems.append(problem)
    classified: dict[str, TextVerdict] = {}
    shown = []
    for item in evidence.get("files") or []:
        data = read(item.get("sha256", ""))
        result = TextVerdict(None, False, "the stored license text is missing") if data is None else classify_text(data)
        classified[item.get("path")] = result
        shown.append({"path": item.get("path"), "spdx": result.spdx, "strict": result.strict,
                      "problem": result.problem})
    if problems:
        return _unknown(problems, shown)
    sets = []
    for scope in evidence.get("scopes") or []:
        governing = scope.get("files") or []
        if not governing:
            return _unknown([f"no license file at or above {scope.get('path') or 'the repository top'}"], shown)
        ids = set()
        for path in governing:
            result = classified.get(path)
            if result is None or result.spdx is None:
                return _unknown([f"{path}: {result.problem if result else 'not read'}"], shown)
            ids.add(result.spdx)
        for path in scope.get("nested") or []:
            result = classified.get(path)
            if result is None or result.spdx is None or result.spdx not in ids:
                return _unknown([f"nested license file {path} does not read as the folder's license"], shown)
        sets.append(frozenset(ids))
    if not sets:
        return _unknown(["no selected scope"], shown)
    if len(set(sets)) != 1:
        return _unknown(["the selected paths are under different licenses"], shown)
    ids = sets[0]
    strict = all(result.strict for result in classified.values() if result.spdx in ids)
    declared = evidence.get("declared")
    if declared is None:
        if len(ids) != 1:
            return _unknown(["several license texts without a declared expression; register the package manifest "
                             "with --manifest"], shown)
        only = next(iter(ids))
        return Verdict("single", only, (only,), strict, (), tuple(shown))
    parsed = parse_expression(declared.get("value", ""))
    if parsed is None:
        return _unknown([f"the declared expression {declared.get('value')!r} is not a plain expression of known "
                         "licenses"], shown)
    form, declared_ids = parsed
    if set(declared_ids) != ids:
        return _unknown([f"the manifest declares {declared.get('value')} but the license files read as "
                         f"{', '.join(sorted(ids))}"], shown)
    joiner = " OR " if form == "choice" else " AND "
    return Verdict(form, joiner.join(declared_ids), declared_ids, strict, (), tuple(shown))


@dataclasses.dataclass(frozen=True)
class Eligibility:
    status: str                  # eligible | eligible-by-choice | excluded | unknown
    usable_under: str | None
    policy: tuple[str, ...]
    reason: str

    def to_json(self) -> dict:
        return {"status": self.status, "usable_under": self.usable_under, "policy": list(self.policy),
                "reason": self.reason}


def eligibility(result: Verdict, allow: Sequence[str]) -> Eligibility:
    """Pure: the same verdict and policy always give the same answer."""
    policy = tuple(sorted(set(allow)))
    if result.form == "unknown":
        return Eligibility("unknown", None, policy, "; ".join(result.problems) or "license evidence is incomplete")
    if not result.strict:
        return Eligibility("excluded", None, policy, f"{result.expression} is not a license this policy can allow")
    if result.form == "single":
        only = result.ids[0]
        if only in policy:
            return Eligibility("eligible", only, policy, f"{only} is allowed")
        return Eligibility("excluded", None, policy, f"{only} is not in the allowed licenses")
    if result.form == "choice":
        allowed = [item for item in result.ids if item in policy]
        if allowed:
            return Eligibility("eligible-by-choice", allowed[0], policy,
                               f"{result.expression} is a choice; usable under {allowed[0]}")
        return Eligibility("excluded", None, policy, f"no alternative of {result.expression} is allowed")
    if all(item in policy for item in result.ids):
        return Eligibility("eligible", result.expression, policy, f"every part of {result.expression} is allowed")
    return Eligibility("excluded", None, policy, f"{result.expression} needs a license that is not allowed")
