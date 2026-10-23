# 위키 노트 — 머리말(YAML 부분집합) 읽기, 제목→파일 이름, 템플릿 렌더링, index.md 등록, oah init 용 스캐폴드 파일을 만든다.
from __future__ import annotations

import datetime
import importlib.resources
import json
import re
import unicodedata

from oah.wiki.base import EXIT_REFUSED, WikiError
from oah.wiki.markdown import FenceTracker, eol_of, split_lines, strip_eol

TEMPLATES = importlib.resources.files("oah") / "templates"
KINDS = {"note": ("", "Notes"), "decision": ("decisions", "Decisions")}

_KEY = re.compile(r"^([A-Za-z_][\w.-]*)[ \t]*:(?:[ \t]+(.*))?$")
_ITEM = re.compile(r"^[ \t]*-(?:[ \t]+(.*))?$")
_LIST_LINE = re.compile(r"^ {0,3}[-*+][ \t]")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SLUG_MAX = 80


def packaged(*parts: str) -> str:
    """A packaged template as text with LF line endings, whatever the checkout did to it."""
    resource = TEMPLATES
    for part in parts:
        resource = resource / part
    return resource.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            return str(json.loads(value))
        except ValueError:
            return value.strip('"')
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    comment = re.search(r"[ \t]#", value)
    return (value[:comment.start()] if comment else value).strip()


def _flow_list(value: str) -> list[str]:
    inner = value.strip()[1:-1]
    items, current, quote = [], "", ""
    for char in inner:
        if quote:
            current += char
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current += char
        elif char == ",":
            items.append(current)
            current = ""
        else:
            current += char
    items.append(current)
    return [_scalar(item) for item in items if item.strip()]


def frontmatter_span(lines: list[str]) -> tuple[int, int] | None:
    """Indexes of the opening and closing delimiter lines, or None."""
    if not lines or strip_eol(lines[0]).lstrip("﻿").rstrip() != "---":
        return None
    for index in range(1, len(lines)):
        if strip_eol(lines[index]).rstrip() in ("---", "..."):
            return 0, index
    return None


def parse_frontmatter(text: str) -> dict | None:
    """The frontmatter as {key: str | list[str]}, or None when absent, unclosed or malformed.

    Only the subset notes use: `key: value`, `key: [a, b]`, and `key:` followed by `- item` lines.
    Indented continuation lines of other YAML forms are tolerated and ignored.
    """
    lines = text.split("\n")
    span = frontmatter_span(lines)
    if span is None:
        return None
    data: dict[str, object] = {}
    key: str | None = None
    for raw in lines[1:span[1]]:
        line = strip_eol(raw)
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _ITEM.match(line)
        if item and key is not None and (data[key] == "" or isinstance(data[key], list)):
            if data[key] == "":
                data[key] = []
            data[key].append(_scalar(item.group(1) or ""))
            continue
        if line[0] in " \t":
            continue
        match = _KEY.match(line)
        if match is None:
            return None
        key, value = match.group(1), (match.group(2) or "").strip()
        if value.startswith("[") and value.endswith("]"):
            data[key] = _flow_list(value)
        else:
            data[key] = _scalar(value)
    return data


def validate_title(title: str) -> str:
    cleaned = title.strip()
    if not cleaned or _CONTROL.search(cleaned) or len(cleaned) > 200:
        raise WikiError("OAH_WIKI_TITLE_INVALID", "give a one-line title (up to 200 characters) with letters "
                        'or digits, for example: oah wiki new "Retry policy"', EXIT_REFUSED)
    return cleaned


def slugify(title: str) -> str:
    """A file name stem that is safe on Windows, macOS and in Obsidian links."""
    text = unicodedata.normalize("NFC", title).lower()
    slug = "-".join(re.findall(r"[\w.-]+", text))
    slug = re.sub(r"-{2,}", "-", slug)[:SLUG_MAX].strip("-._")
    if not slug:
        raise WikiError("OAH_WIKI_TITLE_INVALID", f"the title {title!r} has no letters or digits to name a "
                        "file with; use a title such as \"Retry policy\"", EXIT_REFUSED)
    if slug.split(".")[0] in _RESERVED:
        slug += "-note"
    return slug


def _set_key(text: str, key: str, values: list[str]) -> str:
    lines = text.split("\n")
    span = frontmatter_span(lines)
    if span is None:
        return text
    eol = "\r" if lines[0].endswith("\r") else ""
    rendered = f"{key}: {json.dumps(values, ensure_ascii=False)}{eol}"
    start = None
    for index in range(1, span[1]):
        if _KEY.match(strip_eol(lines[index])) and strip_eol(lines[index]).split(":", 1)[0].strip() == key:
            start = index
            break
    if start is None:
        lines.insert(span[1], rendered)
        return "\n".join(lines)
    end = start + 1
    while end < span[1] and strip_eol(lines[end])[:1] in (" ", "\t", "-"):
        end += 1
    lines[start:end] = [rendered]
    return "\n".join(lines)


def render_note(template: str, *, title: str, today: datetime.date, sources: list[str] = (),
                tags: list[str] = ()) -> str:
    """Fill {{title}} and {{date}}; frontmatter titles become quoted YAML strings."""
    lines = template.split("\n")
    span = frontmatter_span(lines)
    end = span[1] + 1 if span else 0
    quoted = json.dumps(title, ensure_ascii=False)
    head = "\n".join(lines[:end])
    for form in ('"{{title}}"', "'{{title}}'", "{{title}}"):
        head = head.replace(form, quoted)
    body = "\n".join(lines[end:]).replace("{{title}}", title)
    text = (head + "\n" + body if span else body).replace("{{date}}", today.isoformat())
    if sources:
        text = _set_key(text, "sources", list(sources))
    if tags:
        meta = parse_frontmatter(text) or {}
        existing = meta.get("tags") if isinstance(meta.get("tags"), list) else []
        text = _set_key(text, "tags", list(dict.fromkeys([*existing, *tags])))
    return text


def index_item(slug: str, title: str) -> str:
    alias = re.sub(r"[\[\]|]", "", title).strip()
    return f"- [[{slug}]]" if alias in ("", slug) else f"- [[{slug}|{alias}]]"


def add_to_index(data: bytes, label: str, item: str) -> bytes:
    """Add item at the end of the list under the `<label>:` line, or append that label at the end."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WikiError("OAH_WIKI_INDEX_INVALID", f"index.md is not valid UTF-8 ({exc}); fix its encoding "
                        "and retry") from exc
    eol = eol_of(data)
    lines = split_lines(data)
    fences = FenceTracker()
    for number, line in enumerate(lines):
        text = line.decode("utf-8")
        if fences.feed(text) or strip_eol(text).rstrip() != f"{label}:":
            continue
        end = number + 1
        while end < len(lines) and _LIST_LINE.match(strip_eol(lines[end].decode("utf-8"))):
            end += 1
        if not lines[end - 1].endswith(b"\n"):
            lines[end - 1] += eol
        lines.insert(end, item.encode("utf-8") + eol)
        return b"".join(lines)
    out = data
    if out and not out.endswith(b"\n"):
        out += eol
    if out.strip() and not out.endswith(eol + eol):
        out += eol
    return out + f"{label}:".encode("utf-8") + eol + item.encode("utf-8") + eol


def scaffold_files(today: datetime.date) -> dict[str, bytes]:
    """Wiki-relative paths and bytes that `oah init` writes into a new wiki folder.

    The Obsidian templates under templates/ keep their {{title}}/{{date}} placeholders, which
    Obsidian's core Templates plugin fills. No editor workspace state is shipped.
    """
    files = {
        "index.md": packaged("wiki", "index.md"),
        "log.md": packaged("wiki", "log.md").replace("{{date}}", today.isoformat()),
        "wiki-guide.md": render_note(packaged("wiki", "guide.md"), title="How this wiki works", today=today),
        "templates/note.md": packaged("wiki", "note.md"),
        "templates/decision.md": packaged("wiki", "decision.md"),
    }
    out = {name: text.encode("utf-8") for name, text in files.items()}
    for resource in sorted((TEMPLATES / "obsidian").iterdir(), key=lambda item: item.name):
        if resource.name.endswith(".json"):
            out[f".obsidian/{resource.name}"] = resource.read_bytes().replace(b"\r\n", b"\n")
    return out


def gitignore_snippet() -> str:
    """Lines for the project's .gitignore that keep Obsidian's UI state (open panes, graph zoom) out (A8d)."""
    return packaged("obsidian", "gitignore-snippet.txt")
