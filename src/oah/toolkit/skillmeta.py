# 스킬 메타데이터 — SKILL.md 머리말(YAML 일부)을 읽고, 설치판 표시·번들 출처에서 버전·출처·라이선스를 찾으며, 적재 검사를 한다.
"""Read a skill folder the way Claude Code and Codex do, and check that it would load.

The frontmatter parser covers the YAML subset skills use: `key: value`, quoted scalars, `|` and `>`
block scalars, nested maps and `- item` lists. Anything else is refused rather than guessed.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass

SKILL_FILE = "SKILL.md"
MARKER_FILE = ".oah-skill.json"
UPSTREAM_FILE = "UPSTREAM.json"
UPSTREAM_SEARCH_DEPTH = 4

_KEY = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s+|$)(.*)$")
_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_INLINE_CODE = re.compile(r"(`+).*?\1")
_FENCE = re.compile(r"^\s*(```|~~~)")


class SkillError(Exception):
    code = "OAH_SKILL_INVALID"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    version: str | None
    source: str | None
    license: str | None
    path: pathlib.Path


def _scalar(raw: str, where: str) -> object:
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("#"):
        return None
    if text[0] == '"':
        if len(text) < 2 or not text.endswith('"'):
            raise SkillError(f"{where}: unterminated double-quoted value")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise SkillError(f"{where}: unreadable double-quoted value") from exc
    if text[0] == "'":
        if len(text) < 2 or not text.endswith("'"):
            raise SkillError(f"{where}: unterminated single-quoted value")
        return text[1:-1].replace("''", "'")
    if " #" in text:
        text = text.split(" #", 1)[0].rstrip()
    if text[0] in "[{&*!?%@`|>" or re.search(r":(?:\s|$)", text):
        raise SkillError(f"{where}: unsupported YAML value; use a quoted string or supported block")
    if text.lower() in ("null", "~"):
        return None
    if text.lower() in ("true", "false", "yes", "no", "on", "off"):
        return text.lower() in ("true", "yes", "on")
    if re.fullmatch(r"[-+]?[0-9]+(?:\.[0-9]+)?", text):
        return float(text) if "." in text else int(text)
    return text


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(lines: list[str], start: int, parent: int, style: str) -> tuple[str, int]:
    """A `|` or `>` block scalar; returns its text and the next line index."""
    body: list[str] = []
    index = start
    while index < len(lines) and (not lines[index].strip() or _indent(lines[index]) > parent):
        body.append(lines[index])
        index += 1
    while body and not body[-1].strip():
        body.pop()
    width = min((_indent(line) for line in body if line.strip()), default=0)
    rows = [line[width:] if line.strip() else "" for line in body]
    if style[0] == "|":
        text = "\n".join(rows)
    else:
        text, previous_blank = "", True
        for row in rows:
            if not row:
                text += "\n"
                previous_blank = True
            else:
                text += row if previous_blank else " " + row
                previous_blank = False
    if style.endswith("-"):
        return text, index
    return text + "\n", index


def _mapping(lines: list[str], start: int, indent: int, where: str) -> tuple[dict, int]:
    result: dict = {}
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        current = _indent(line)
        if current < indent:
            break
        if current > indent:
            raise SkillError(f"{where} line {index + 1}: unexpected indentation")
        match = _KEY.match(line.strip())
        if match is None:
            raise SkillError(f"{where} line {index + 1}: expected 'key: value'")
        key, rest = match.group(1), match.group(2).rstrip()
        if key in result:
            raise SkillError(f"{where} line {index + 1}: duplicate key {key!r}")
        index += 1
        if rest in ("|", "|-", "|+", ">", ">-", ">+"):
            result[key], index = _block(lines, index, current, rest)
        elif rest:
            result[key] = _scalar(rest, f"{where} line {index}")
        else:
            child = index
            while child < len(lines) and not lines[child].strip():
                child += 1
            if child < len(lines) and _indent(lines[child]) > current:
                if lines[child].lstrip().startswith("- "):
                    result[key], index = _sequence(lines, child, _indent(lines[child]), where)
                else:
                    result[key], index = _mapping(lines, child, _indent(lines[child]), where)
            else:
                result[key] = ""
    return result, index


def _sequence(lines: list[str], start: int, indent: int, where: str) -> tuple[list, int]:
    items: list = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if _indent(line) < indent:
            break
        stripped = line.strip()
        if _indent(line) != indent or not (stripped == "-" or stripped.startswith("- ")):
            raise SkillError(f"{where} line {index + 1}: expected '- item'")
        items.append(_scalar(stripped[1:], f"{where} line {index + 1}"))
        index += 1
    return items, index


def parse_frontmatter(text: str, where: str = SKILL_FILE) -> dict:
    """The YAML frontmatter between leading `---` lines; required fields retain their scalar types."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{where}: does not start with a '---' frontmatter block")
    try:
        end = next(number for number in range(1, len(lines)) if lines[number].strip() == "---")
    except StopIteration:
        raise SkillError(f"{where}: the frontmatter block has no closing '---'") from None
    if any("\t" in line[:_indent(line) + 1] for line in lines[1:end] if line.strip()):
        raise SkillError(f"{where}: frontmatter indentation must use spaces")
    data, _ = _mapping(lines[1:end], 0, 0, where)
    return data


def _skill_file(path: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(path)
    return path if path.name == SKILL_FILE else path / SKILL_FILE


def _read_json(path: pathlib.Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SkillError(f"{path}: unreadable ({exc}); reinstall the skill with oah skills install") from exc
    return data if isinstance(data, dict) else None


def find_upstream(folder: pathlib.Path) -> tuple[pathlib.Path, dict] | None:
    """The nearest UPSTREAM.json at or above folder, within a few levels."""
    current = pathlib.Path(folder)
    for _ in range(UPSTREAM_SEARCH_DEPTH + 1):
        data = _read_json(current / UPSTREAM_FILE)
        if data is not None:
            return current, data
        if current.parent == current:
            break
        current = current.parent
    return None


def _text(value: object) -> str | None:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value):
        return str(value)
    return None


def _required_metadata(data: dict, where: str) -> list[str]:
    problems = []
    for key in ("name", "description"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: frontmatter '{key}' must be a non-empty string; fix SKILL.md before retrying")
    name = data.get("name")
    if isinstance(name, str) and (len(name) > 64 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)):
        problems.append(f"{where}: name must be 1–64 lowercase letters, digits or single hyphens; fix SKILL.md")
    return problems


def read_skill(path: pathlib.Path) -> SkillInfo:
    """Name, description, version, source and license of the skill at path (a folder or SKILL.md).

    Installed copies answer from their .oah-skill.json marker, bundled copies from UPSTREAM.json,
    anything else from its own frontmatter.
    """
    skill_file = _skill_file(path)
    folder = skill_file.parent
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"{skill_file}: cannot be read ({exc.strerror or exc})") from exc
    data = parse_frontmatter(text, str(skill_file))
    problems = _required_metadata(data, str(skill_file))
    if problems:
        raise SkillError("; ".join(problems))
    name, description = data["name"], data["description"]
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    version = _text(data.get("version")) or _text(metadata.get("version"))
    source, license_id = None, _text(data.get("license"))
    record = _read_json(folder / MARKER_FILE)
    if record is None:
        found = find_upstream(folder)
        record = found[1] if found else None
    if record is not None:
        version = _text(record.get("commit")) or version
        source = _text(record.get("repository"))
        license_id = _text(record.get("spdx")) or license_id
    return SkillInfo(name=name, description=description.strip(), version=version, source=source,
                     license=license_id, path=folder)


def _links(text: str) -> list[str]:
    targets: list[str] = []
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        targets.extend(match.group(1) for match in _LINK.finditer(_INLINE_CODE.sub("", line)))
    return targets


def load_problems(skill_dir: pathlib.Path, *, item_root: pathlib.Path | None = None) -> list[str]:
    """Why this skill would not load: empty when the frontmatter parses, names itself and describes
    itself, and every relative Markdown link points at a file inside the item."""
    skill_file = _skill_file(skill_dir)
    folder = skill_file.parent
    root = pathlib.Path(item_root) if item_root is not None else folder
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{skill_file}: cannot be read as UTF-8 ({exc})"]
    problems: list[str] = []
    try:
        data = parse_frontmatter(text, str(skill_file))
    except SkillError as exc:
        return [str(exc)]
    problems.extend(_required_metadata(data, str(skill_file)))
    for target in _links(text[text.find("---", 3) + 3:]):
        if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith(("#", "/")):
            continue
        relative = target.split("#", 1)[0].split("?", 1)[0]
        if not relative:
            continue
        resolved = (folder / relative).resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            problems.append(f"{skill_file}: link {target} points outside the skill")
            continue
        if not resolved.exists():
            problems.append(f"{skill_file}: link {target} points at a missing file")
    return problems
