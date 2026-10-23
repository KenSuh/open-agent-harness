# 기본 DB 플러그인 — SQL 마이그레이션을 정규식으로 읽어 테이블·뷰·함수·프로시저·트리거·인덱스·GRANT 개체와 그 사이 연결을 만든다(주석·문자열은 지운 뒤 읽는다).
"""The default DB backend: SQL migration files to a small entity graph.

It is regex-based and tolerant of Postgres, MySQL and SQLite syntax. Comments and string literals are
blanked first (newlines kept, so line numbers stay exact). Names are folded to lower case and the
schema is dropped, so `public."Users"` is `db_table:users`. Entities:
- `db_table:<name>` for tables and views (`kind` says which), with their indexes;
- `db_routine:<name>` for functions, procedures and triggers;
- `db_grant:<role>` for the roles that GRANT or REVOKE name (`'user'@'host'` becomes `user`).
Links are `[from, to, relation, file, line]`: foreign keys, view sources, trigger tables and functions,
tables a routine body uses, and grants.
"""
from __future__ import annotations

import bisect
import re
from typing import Iterable, Mapping

_PART = r'"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*'
NAME = rf"(?:{_PART})(?:\s*\.\s*(?:{_PART}))*"
_OBJ = rf"(?:{_PART}|\*)(?:\s*\.\s*(?:{_PART}|\*))*"
_WHO = rf"(?:'[^']*'(?:\s*@\s*'[^']*')?|`[^`]*`(?:\s*@\s*`[^`]*`)?|(?:{_PART})(?:\s*@\s*\S+)?)"
_F = re.I | re.S


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, _F)


_CREATE_TABLE = _rx(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?"
                    rf"(?:(?:TEMP|TEMPORARY|UNLOGGED|VIRTUAL)\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{NAME})")
_CREATE_VIEW = _rx(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:TEMP|TEMPORARY)\s+)?(?:ALGORITHM\s*=\s*\w+\s+)?"
                   rf"(?:DEFINER\s*=\s*\S+\s+)?(?:SQL\s+SECURITY\s+\w+\s+)?(?:MATERIALIZED\s+)?VIEW\s+"
                   rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{NAME})")
_CREATE_ROUTINE = _rx(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*\S+\s+)?(?P<kind>FUNCTION|PROCEDURE)\s+"
                      rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{NAME})")
_CREATE_TRIGGER = _rx(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*\S+\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+"
                      rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{NAME})")
_CREATE_INDEX = _rx(rf"\bCREATE\s+(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
                    rf"(?:IF\s+NOT\s+EXISTS\s+)?(?:(?!ON\b)(?P<name>{NAME})\s+)?ON\s+(?:ONLY\s+)?(?P<table>{NAME})")
_ALTER = _rx(rf"\bALTER\s+(?P<kind>TABLE|(?:MATERIALIZED\s+)?VIEW|FUNCTION|PROCEDURE)\s+(?:IF\s+EXISTS\s+)?"
             rf"(?:ONLY\s+)?(?P<name>{NAME})")
_DROP = _rx(rf"\bDROP\s+(?P<kind>TABLE|(?:MATERIALIZED\s+)?VIEW|FUNCTION|PROCEDURE|TRIGGER)\s+"
            rf"(?:IF\s+EXISTS\s+)?(?P<names>{NAME}(?:\s*,\s*{NAME})*)")
_RENAME_TABLE = _rx(rf"\bRENAME\s+TABLE\s+(?P<pairs>{NAME}\s+TO\s+{NAME}(?:\s*,\s*{NAME}\s+TO\s+{NAME})*)")
_GRANT_START = _rx(r"\b(?:GRANT|REVOKE)\b")
_GRANT = _rx(r"\b(?P<op>GRANT|REVOKE)\s+(?:GRANT\s+OPTION\s+FOR\s+)?(?P<privs>[^;]+?)\s+ON\s+")
_OBJECT_TYPE = _rx(r"(?:TABLE|FUNCTION|PROCEDURE|ROUTINE|SEQUENCE|SCHEMA|DATABASE|"
                   r"ALL\s+(?:TABLES|FUNCTIONS|PROCEDURES|ROUTINES|SEQUENCES)\s+IN\s+SCHEMA)\s+")
_OBJECT_NAME = _rx(_OBJ)
_ROLES = _rx(rf"\s*{_WHO}(?:\s*,\s*{_WHO})*")
_QUOTED_NAME_BEFORE = _rx(r"(?:\b(?:TABLE|VIEW|FUNCTION|PROCEDURE|TRIGGER|INDEX|REFERENCES|FROM|JOIN|"
                          r"INTO|UPDATE|ON|TO|SCHEMA|DATABASE)\s+(?:(?:IF\s+(?:NOT\s+)?EXISTS|ONLY)\s+)?"
                          rf"(?:{NAME}\s*,\s*)*|\.\s*)\Z")
_RENAME_TO = _rx(rf"\bRENAME\s+TO\s+(?P<name>{NAME})")
_REFERENCES = _rx(rf"\bREFERENCES\s+(?P<name>{NAME})")
_SOURCES = _rx(rf"\b(?:FROM|JOIN)\s+(?P<name>{NAME})")
_USES = _rx(rf"\b(?:FROM|JOIN|INTO|UPDATE)\s+(?P<name>{NAME})")
_ON = _rx(rf"\bON\s+(?P<name>{NAME})")
_CALLS = _rx(rf"\bEXECUTE\s+(?:FUNCTION|PROCEDURE)\s+(?P<name>{NAME})")
_BEGIN = _rx(r"\bBEGIN\b")
_BLOCK = _rx(r"\b(?P<word>BEGIN|CASE|END)\b(?:\s+(?P<after>IF|LOOP|WHILE|REPEAT|FOR|CASE)\b)?")
_DELIMITER = re.compile(r"[ \t]*DELIMITER[ \t]+(?P<delim>\S+)[ \t]*(?=\r?\n|\Z)", re.I)
_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_PARTS = re.compile(_PART + r"|\*")


def _quoted_end(text: str, start: int) -> int:
    quote = "]" if text[start] == "[" else text[start]
    end = start + 1
    while end < len(text):
        if text[end] == "\\":
            end += 2
        elif text[end] == quote:
            if text.startswith(quote * 2, end):
                end += 2
            else:
                return end + 1
        else:
            end += 1
    return len(text)


def blank(text: str) -> tuple[str, str]:
    """(code, nocomment): comments blanked in both, string bodies also blanked in code. Offsets are unchanged.

    MySQL `DELIMITER x` lines are blanked and later `x` delimiters become `;` in code.
    """
    code, keep = list(text), list(text)
    n, i, delimiter = len(text), 0, ";"

    def wipe(target: list[str], start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if target[k] not in "\r\n":
                target[k] = " "

    while i < n:
        if i == 0 or text[i - 1] == "\n":
            match = _DELIMITER.match(text, i)
            if match:
                delimiter = match.group("delim")
                wipe(code, i, match.end())
                wipe(keep, i, match.end())
                i = match.end()
                continue
        char = text[i]
        if text.startswith("--", i) or char == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
            wipe(code, i, end)
            wipe(keep, i, end)
            i = end
        elif text.startswith("/*", i):
            depth, end = 1, i + 2
            while end < n and depth:
                if text.startswith("/*", end):
                    depth, end = depth + 1, end + 2
                elif text.startswith("*/", end):
                    depth, end = depth - 1, end + 2
                else:
                    end += 1
            wipe(code, i, end)
            wipe(keep, i, end)
            i = end
        elif char in "'\"`[":
            end = _quoted_end(text, i)
            # Double quotes name objects after SQL name-introducing keywords or a schema dot.
            # In expression positions (VALUES, DEFAULT, etc.) MySQL accepts them as strings.
            identifier = char in "`[" or (char == '"' and
                _QUOTED_NAME_BEFORE.search("".join(code[max(0, i - 256):i])) is not None)
            if not identifier:
                wipe(code, i + 1, end - 1 if text[end - 1:end] == char else end)
            i = end
        elif delimiter != ";" and text.startswith(delimiter, i):
            code[i] = ";"
            wipe(code, i + 1, i + len(delimiter))
            i += len(delimiter)
        elif char == "$" and delimiter == ";" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            match = _DOLLAR.match(text, i)
            if match is None:
                i += 1
                continue
            tag = match.group()
            end = text.find(tag, match.end())
            end = n if end < 0 else end
            wipe(code, match.end(), end)
            i = end + len(tag)
        else:
            i += 1
    return "".join(code), "".join(keep)


def _parts(raw: str) -> list[str]:
    return [part.strip('"`[]').lower() for part in _PARTS.findall(raw)]


def _name(raw: str) -> str:
    return _parts(raw)[-1]


def _separators(raw: str) -> Iterable[tuple[int, str]]:
    """Top-level commas and TO/FROM words, skipping quotes and balanced signatures."""
    depth, i = 0, 0
    while i < len(raw):
        char = raw[i]
        if char in "'\"`[":
            i = _quoted_end(raw, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced SQL signature")
        elif not depth and char == ",":
            yield i, char
        elif char.isalnum() or char in "_$":
            end = i + 1
            while end < len(raw) and (raw[end].isalnum() or raw[end] in "_$"):
                end += 1
            word = raw[i:end].upper()
            if not depth and word in ("TO", "FROM"):
                yield i, word
            i = end
            continue
        i += 1
    if depth:
        raise ValueError("unbalanced SQL signature")


def _split(raw: str) -> list[str]:
    pieces, start = [], 0
    try:
        for offset, separator in _separators(raw):
            if separator == ",":
                pieces.append(raw[start:offset].strip())
                start = offset + 1
    except ValueError:
        return []
    pieces.append(raw[start:].strip())
    return [piece for piece in pieces if piece]


def _grants(code: str, nocomment: str) -> Iterable[tuple]:
    """Try one GRANT header per bounded statement; failed headers never rescan its suffix."""
    for statement in re.finditer(r"[^;]+", code):
        start = _GRANT_START.search(code, statement.start(), statement.end())
        if start is None:
            continue
        match = _GRANT.match(nocomment, start.start(), statement.end())
        if match is None:
            continue
        objects_start = match.end()
        kind = _OBJECT_TYPE.match(nocomment, objects_start, statement.end())
        if kind:
            objects_start = kind.end()
        body = nocomment[objects_start:statement.end()]
        try:
            boundary = next(((offset, word) for offset, word in _separators(body) if word != ","), None)
        except ValueError:
            continue
        if boundary is None:
            continue
        offset, word = boundary
        roles = _ROLES.match(body, offset + len(word))
        if roles:
            yield match, kind.group().strip() if kind else "", body[:offset], roles.group()


def _extent(code: str, start: int) -> int:
    """End offset of the statement starting at start: the next `;`, or past a BEGIN … END body."""
    semi = code.find(";", start)
    semi = len(code) if semi < 0 else semi
    begin = _BEGIN.search(code, start, semi)
    if begin is None:
        return semi
    depth = 0
    for match in _BLOCK.finditer(code, begin.start()):
        word, after = match.group("word").upper(), (match.group("after") or "").upper()
        if word in ("BEGIN", "CASE"):
            depth += 1
        elif after in ("", "CASE"):
            depth -= 1
        if depth == 0:
            end = code.find(";", match.end())
            return len(code) if end < 0 else end
    return len(code)


class _Graph:
    def __init__(self) -> None:
        self.entities: dict[str, dict] = {}
        self.links: set[tuple[str, str, str, str, int]] = set()
        self.pending: list[tuple[str, str, str, str, int]] = []  # links that need a known table

    def entity(self, entity: str, kind: str | None = None) -> dict:
        record = self.entities.setdefault(entity, {"kind": kind or "table", "name": entity.split(":", 1)[1],
                                                   "events": [], "indexes": []})
        if kind is not None:
            record["kind"] = kind
        return record

    def event(self, entity: str, kind: str | None, file: str, line: int, op: str) -> None:
        self.entity(entity, kind)["events"].append([file, line, op])

    def result(self) -> dict:
        tables = {entity for entity, info in self.entities.items() if entity.startswith("db_table:")}
        links = set(self.links)
        links.update(link for link in self.pending if link[1] in tables and link[1] != link[0])
        for info in self.entities.values():
            info["events"].sort(key=lambda event: (event[0], event[1]))
            info["indexes"].sort()
        return {"entities": dict(sorted(self.entities.items())), "links": sorted(list(link) for link in links)}


def _live(code: str, nocomment: str, position: int) -> bool:
    return code[position] == nocomment[position]


def _file(graph: _Graph, file: str, text: str) -> None:
    code, nocomment = blank(text)
    newlines = [index for index, char in enumerate(text) if char == "\n"]

    def line(offset: int) -> int:
        return bisect.bisect_right(newlines, offset - 1) + 1 if newlines else 1

    for match in _CREATE_TABLE.finditer(code):
        table = "db_table:" + _name(match.group("name"))
        graph.event(table, "table", file, line(match.start()), "create")
        for ref in _REFERENCES.finditer(code, match.end(), _extent(code, match.start())):
            graph.links.add((table, "db_table:" + _name(ref.group("name")), "foreign-key", file, line(ref.start())))
    for match in _CREATE_VIEW.finditer(code):
        view = "db_table:" + _name(match.group("name"))
        graph.event(view, "view", file, line(match.start()), "create")
        for ref in _SOURCES.finditer(code, match.end(), _extent(code, match.start())):
            graph.pending.append((view, "db_table:" + _name(ref.group("name")), "view-source", file,
                                  line(ref.start())))
    for match in _CREATE_ROUTINE.finditer(code):
        routine = "db_routine:" + _name(match.group("name"))
        graph.event(routine, match.group("kind").lower(), file, line(match.start()), "create")
        for ref in _USES.finditer(nocomment, match.end(), _extent(code, match.start())):
            graph.pending.append((routine, "db_table:" + _name(ref.group("name")), "uses", file, line(ref.start())))
    for match in _CREATE_TRIGGER.finditer(code):
        trigger = "db_routine:" + _name(match.group("name"))
        end = _extent(code, match.start())
        graph.event(trigger, "trigger", file, line(match.start()), "create")
        on = _ON.search(code, match.end(), end)
        if on:
            graph.links.add((trigger, "db_table:" + _name(on.group("name")), "trigger-on", file, line(on.start())))
        for ref in _CALLS.finditer(code, match.end(), end):
            graph.links.add((trigger, "db_routine:" + _name(ref.group("name")), "calls", file, line(ref.start())))
        for ref in _USES.finditer(nocomment, on.end() if on else match.end(), end):
            graph.pending.append((trigger, "db_table:" + _name(ref.group("name")), "uses", file, line(ref.start())))
    for match in _CREATE_INDEX.finditer(code):
        table = "db_table:" + _name(match.group("table"))
        index = _name(match.group("name")) if match.group("name") else "(unnamed)"
        graph.entity(table)["indexes"].append([index, file, line(match.start())])
    for match in _ALTER.finditer(code):
        kind = match.group("kind").split()[-1].lower()
        prefix = "db_table:" if kind in ("table", "view") else "db_routine:"
        entity = prefix + _name(match.group("name"))
        end = _extent(code, match.start())
        rename = _RENAME_TO.search(code, match.end(), end)
        if rename:
            new = prefix + _name(rename.group("name"))
            graph.event(entity, None, file, line(match.start()), f"rename-to:{new.split(':', 1)[1]}")
            graph.event(new, graph.entity(entity)["kind"], file, line(match.start()),
                        f"rename-from:{entity.split(':', 1)[1]}")
        else:
            graph.event(entity, None, file, line(match.start()), "alter")
        for ref in _REFERENCES.finditer(code, match.end(), end):
            graph.links.add((entity, "db_table:" + _name(ref.group("name")), "foreign-key", file, line(ref.start())))
    for match in _DROP.finditer(code):
        word = match.group("kind").split()[-1].lower()
        prefix, kind = {"table": ("db_table:", "table"), "view": ("db_table:", "view"),
                        "function": ("db_routine:", "function"), "procedure": ("db_routine:", "procedure"),
                        "trigger": ("db_routine:", "trigger")}[word]
        for raw in _split(match.group("names")):
            entity = prefix + _name(raw)
            known = entity in graph.entities
            graph.event(entity, None if known else kind, file, line(match.start()), "drop")
    for match in _RENAME_TABLE.finditer(code):
        for pair in _split(match.group("pairs")):
            old_raw, new_raw = re.split(r"\s+TO\s+", pair, maxsplit=1, flags=re.I)
            old, new = "db_table:" + _name(old_raw), "db_table:" + _name(new_raw)
            graph.event(old, None, file, line(match.start()), f"rename-to:{new.split(':', 1)[1]}")
            graph.event(new, graph.entity(old)["kind"], file, line(match.start()), f"rename-from:{old.split(':', 1)[1]}")
    for match, object_type, object_list, roles in _grants(code, nocomment):
        if not _live(code, nocomment, match.start()):
            continue
        op = match.group("op").lower()
        privileges = ",".join(" ".join(p.split()).upper() for p in _split(match.group("privs")))
        otype = " ".join(object_type.upper().split())
        objects = []
        for raw in _split(object_list):
            name = _OBJECT_NAME.match(raw)
            if name is None:
                continue
            suffix = raw[name.end():].strip()
            if suffix and not (suffix.startswith("(") and suffix.endswith(")")):
                continue
            parts = _parts(name.group())
            if otype in ("SCHEMA", "DATABASE") or otype.startswith("ALL "):
                objects.append("db_schema:" + parts[-1])
            elif parts[-1] == "*":
                objects.append("db_schema:" + (parts[-2] if len(parts) > 1 else "*"))
            elif otype in ("FUNCTION", "PROCEDURE", "ROUTINE"):
                objects.append("db_routine:" + parts[-1])
            else:
                objects.append("db_table:" + parts[-1])
        for raw in _split(roles):
            role = raw.split("@", 1)[0].strip().strip("'`\"[]").lower()
            grant = "db_grant:" + role
            graph.event(grant, "grant", file, line(match.start()), op)
            for target in objects:
                graph.links.add((grant, target, f"{op}:{privileges}", file, line(match.start())))


def extract(files: Mapping[str, str]) -> dict:
    """The entity graph of the given SQL files ({path: text}), read in path order."""
    graph = _Graph()
    for file in sorted(files):
        _file(graph, file, files[file])
    return graph.result()
