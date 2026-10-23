# 기본 DB 플러그인 — 한 방향 SQL 토큰 스캐너로 마이그레이션의 개체와 연결을 읽는다.
"""The default DB backend: SQL migration files to a small entity graph.

Names are folded to lower case and schemas dropped. Tables and views use db_table, functions,
procedures and triggers use db_routine, and GRANT/REVOKE roles use db_grant. Links and event line
numbers retain their source locations. A shared tokenizer handles quotes, comments and delimiters;
extraction never searches SQL text with regular expressions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

# Kept for callers that imported the original name pattern; extraction uses tokens exclusively.
_PART = r'"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*'
NAME = rf"(?:{_PART})(?:\s*\.\s*(?:{_PART}))*"


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    start: int
    end: int
    line: int
    body: tuple[_Token, ...] = ()
    raw: str = ""

    def is_word(self, *words: str) -> bool:
        return self.kind == "word" and self.value.upper() in words


def _tokenize(text: str) -> list[_Token]:
    """Consume tokens with a forward-only cursor, without backtracking or prefix windows.

    Dollar bodies have an opaque outer token and child SQL tokens for routine dependencies. The
    cursor consumes their contents only once; finding their closing tag is one linear delimiter
    lookup. Child scans cannot nest dollar bodies. Quoted text never produces outer SQL keywords.
    """
    i, line, delimiter, next_delimiter = 0, 1, ";", -1

    def scan(limit: int, *, body: bool = False) -> list[_Token]:
        nonlocal i, line, delimiter, next_delimiter
        tokens: list[_Token] = []
        line_start, expression = True, False
        name_context, name_list = False, False
        name_stack: list[bool] = []
        while i < limit:
            if not body and delimiter != ";" and 0 <= next_delimiter < i:
                next_delimiter = text.find(delimiter, i, limit)
            start, first_line, char = i, line, text[i]
            if char.isspace():
                if char == "\n":
                    line += 1
                    line_start = True
                i += 1
                continue
            if not body and line_start and text[i:i + 9].upper() == "DELIMITER" and \
                    (i + 9 == limit or text[i + 9].isspace()):
                i += 9
                begin = i
                while i < limit and text[i] not in "\r\n":
                    i += 1
                delimiter = text[begin:i].strip() or ";"
                next_delimiter = text.find(delimiter, i, limit) if delimiter != ";" else -1
                tokens.append(_Token("directive", "", start, i, first_line))
                line_start, expression = False, False
                continue
            # #> and #>> are PostgreSQL operators. A bare # after an operand is binary XOR.
            # At a token boundary without a left operand it starts a MySQL line comment.
            hash_comment = (char == "#" and not text.startswith("#>", i) and
                            (line_start or (not expression and (i == 0 or text[i - 1].isspace()))))
            if text.startswith("--", i) or hash_comment:
                while i < limit and text[i] != "\n":
                    i += 1
                tokens.append(_Token("comment", "", start, i, first_line))
                continue
            line_start = False
            if text.startswith("/*", i):
                depth = 1
                i += 2
                while i < limit and depth:
                    if text.startswith("/*", i):
                        depth, i = depth + 1, i + 2
                    elif text.startswith("*/", i):
                        depth, i = depth - 1, i + 2
                    else:
                        line += text[i] == "\n"
                        i += 1
                tokens.append(_Token("comment", "", start, i, first_line))
                continue
            if not body and i == next_delimiter:
                i += len(delimiter)
                tokens.append(_Token("end", ";", start, i, first_line))
                expression = False
                name_context, name_list = False, False
                continue
            if char in "'\"`[":
                close = "]" if char == "[" else char
                i += 1
                value, closed = [], False
                # PostgreSQL E'...' and MySQL strings permit backslash escapes. Backticks and
                # MySQL double-quoted strings also accept them; standard double identifiers do not.
                escapes = char in "'`" or (char == '"' and not name_context)
                while i < limit:
                    if text[i] == close:
                        if i + 1 < limit and text[i + 1] == close:
                            value.append(close)
                            i += 2
                            continue
                        i += 1
                        closed = True
                        break
                    if escapes and text[i] == "\\" and i + 1 < limit:
                        line += text[i + 1] == "\n"
                        value.append(text[i + 1])
                        i += 2
                    else:
                        line += text[i] == "\n"
                        value.append(text[i])
                        i += 1
                identifier = char in "`[" or (char == '"' and name_context)
                kind = ("identifier" if identifier else "string") if closed else "invalid"
                tokens.append(_Token(kind, "".join(value), start, i, first_line, raw=text[start:i]))
                expression = True
                name_context = False
                continue
            if char == "$" and not body and delimiter == ";":
                end = i + 1
                if end < limit and (text[end].isalpha() or text[end] == "_"):
                    end += 1
                    while end < limit and (text[end].isalnum() or text[end] == "_"):
                        end += 1
                if end < limit and text[end] == "$":
                    tag = text[i:end + 1]
                    closing = text.find(tag, end + 1, limit)
                    if closing < 0:
                        line += text.count("\n", i, limit)
                        i = limit
                        tokens.append(_Token("invalid", "", start, i, first_line))
                    else:
                        i = end + 1
                        children = scan(closing, body=True)
                        i = closing + len(tag)
                        tokens.append(_Token("dollar", tag, start, i, first_line, tuple(children)))
                    expression = True
                    continue
            if char.isalpha() or char == "_" or char.isdigit():
                i += 1
                while i < limit and (text[i].isalnum() or text[i] in "_$") and (body or i != next_delimiter):
                    i += 1
                value = text[start:i]
                kind = "number" if char.isdigit() else "word"
                tokens.append(_Token(kind, value, start, i, first_line))
                word = value.upper()
                if word in {"TABLE", "VIEW", "FUNCTION", "PROCEDURE", "TRIGGER", "INDEX", "REFERENCES",
                            "FROM", "JOIN", "INTO", "UPDATE", "ON", "TO", "SCHEMA", "DATABASE"}:
                    name_context, name_list = True, True
                elif not (name_context and word in {"IF", "NOT", "EXISTS", "ONLY", "CONCURRENTLY"}):
                    name_list = name_context
                    name_context = False
                expression = kind == "number" or value.upper() not in {
                    "SELECT", "AS", "DEFAULT", "VALUES", "WHERE", "AND", "OR", "SET", "THEN", "ELSE",
                    "RETURN", "ON", "TO", "FROM", "JOIN", "INTO", "UPDATE", "TABLE", "VIEW"}
                continue
            i += 1
            tokens.append(_Token("end" if char == ";" else "symbol", char, start, i, first_line))
            expression = char in ")]"
            if char == ".":
                name_context = True
            elif char == ",":
                name_context = name_list
            elif char == "(":
                name_stack.append(name_list)
                name_context, name_list = False, False
            elif char == ")":
                name_context, name_list = False, name_stack.pop() if name_stack else False
            else:
                name_context, name_list = False, False
        return tokens

    return scan(len(text))


def blank(text: str) -> tuple[str, str]:
    """(code, nocomment), with offsets/newlines preserved and MySQL delimiters normalized.

    Quoted identifiers stay intact. Extraction consumes their tokens only in identifier positions,
    so expression strings in double quotes cannot introduce SQL commands.
    """
    code, keep = list(text), list(text)

    def wipe(target: list[str], start: int, end: int) -> None:
        for i in range(start, end):
            if target[i] not in "\r\n":
                target[i] = " "

    for token in _tokenize(text):
        if token.kind in ("comment", "directive"):
            wipe(code, token.start, token.end)
            wipe(keep, token.start, token.end)
        elif token.kind in ("string", "dollar", "invalid"):
            padding = len(token.value) if token.kind == "dollar" else 1
            wipe(code, token.start + padding, max(token.start + padding, token.end - padding))
        elif token.kind == "end" and text[token.start:token.end] != ";":
            code[token.start] = ";"
            wipe(code, token.start + 1, token.end)
    return "".join(code), "".join(keep)


def _statements(tokens: Iterable[_Token]) -> Iterable[list[_Token]]:
    current, depth, after_end = [], 0, False
    header, compound = False, False
    live = [token for token in tokens if token.kind not in ("comment", "directive")]
    for i, token in enumerate(live):
        if after_end:
            after_end = False
            if token.is_word("IF", "LOOP", "WHILE", "REPEAT", "FOR", "CASE"):
                current.append(token)
                continue
        if current and current[0].is_word("CREATE") and not header and \
                token.is_word("TABLE", "VIEW", "FUNCTION", "PROCEDURE", "TRIGGER", "INDEX"):
            header, compound = True, token.is_word("FUNCTION", "PROCEDURE", "TRIGGER")
        if compound and token.is_word("BEGIN", "CASE"):
            depth += 1
        elif compound and token.is_word("END"):
            following = live[i + 1] if i + 1 < len(live) else None
            if following is None or not following.is_word("IF", "LOOP", "WHILE", "REPEAT", "FOR"):
                depth = max(0, depth - 1)
            after_end = True
        if token.kind == "end" and not depth:
            if current:
                yield current
            current = []
            header, compound = False, False
        else:
            current.append(token)
    if current:
        yield current


def _name(tokens: list[_Token], i: int, *, star: bool = False, role: bool = False) -> tuple[list[str], int]:
    parts = []
    while i < len(tokens):
        token = tokens[i]
        if token.kind in ("word", "identifier") or (role and token.kind == "string") or \
                (star and token.value == "*"):
            parts.append(token.value.lower())
            i += 1
        else:
            break
        if i + 1 >= len(tokens) or tokens[i].value != ".":
            break
        i += 1
    return parts, i


def _skip_exists(tokens: list[_Token], i: int) -> int:
    if i < len(tokens) and tokens[i].is_word("IF"):
        i += 1
        if i < len(tokens) and tokens[i].is_word("NOT"):
            i += 1
        if i < len(tokens) and tokens[i].is_word("EXISTS"):
            i += 1
    if i < len(tokens) and tokens[i].is_word("ONLY"):
        i += 1
    return i


def _refs(tokens: list[_Token], words: tuple[str, ...]) -> Iterable[tuple[str, _Token]]:
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.is_word(*words):
            names, end = _name(tokens, i + 1)
            if names:
                yield names[-1], token
                i = end
                continue
        elif token.kind == "dollar":
            yield from _refs(list(token.body), words)
        i += 1


def _top(tokens: list[_Token]) -> Iterable[tuple[int, _Token]]:
    depth = 0
    for i, token in enumerate(tokens):
        if token.kind == "symbol" and token.value == "(":
            depth += 1
        elif token.kind == "symbol" and token.value == ")":
            depth -= 1
            if depth < 0:
                return
        elif not depth:
            yield i, token


def _pieces(tokens: list[_Token]) -> Iterable[list[_Token]]:
    start = 0
    for i, token in _top(tokens):
        if token.kind == "symbol" and token.value == ",":
            yield tokens[start:i]
            start = i + 1
    yield tokens[start:]


def _privilege(tokens: list[_Token]) -> str:
    result, end = [], -1
    for token in tokens:
        if result and token.start > end:
            result.append(" ")
        result.append((token.raw or token.value).upper())
        end = token.end
    return "".join(result).strip()


class _Graph:
    def __init__(self) -> None:
        self.entities: dict[str, dict] = {}
        self.links: set[tuple[str, str, str, str, int]] = set()
        self.pending: list[tuple[str, str, str, str, int]] = []

    def entity(self, entity: str, kind: str | None = None) -> dict:
        record = self.entities.setdefault(entity, {"kind": kind or "table", "name": entity.split(":", 1)[1],
                                                   "events": [], "indexes": []})
        if kind is not None:
            record["kind"] = kind
        return record

    def event(self, entity: str, kind: str | None, file: str, line: int, op: str) -> None:
        self.entity(entity, kind)["events"].append([file, line, op])

    def result(self) -> dict:
        tables = {entity for entity in self.entities if entity.startswith("db_table:")}
        links = set(self.links)
        links.update(link for link in self.pending if link[1] in tables and link[1] != link[0])
        for info in self.entities.values():
            info["events"].sort(key=lambda event: (event[0], event[1]))
            info["indexes"].sort()
        return {"entities": dict(sorted(self.entities.items())), "links": sorted(list(link) for link in links)}


def _grant(graph: _Graph, file: str, tokens: list[_Token]) -> None:
    op, line = tokens[0].value.lower(), tokens[0].line
    boundary = next((i for i, token in _top(tokens) if token.is_word("ON")), None)
    if boundary is None:
        return  # Role membership grants have no object dependency.
    begin = 4 if len(tokens) > 3 and all(tokens[i].is_word(word) for i, word in
                                       enumerate(("GRANT", "OPTION", "FOR"), 1)) else 1
    privileges = ",".join(_privilege(piece) for piece in _pieces(tokens[begin:boundary]))
    i, otype = boundary + 1, ""
    if i < len(tokens) and tokens[i].is_word("ALL"):
        otype = "ALL"
        while i < len(tokens) and not tokens[i].is_word("SCHEMA"):
            i += 1
        i += 1
    elif i < len(tokens) and tokens[i].is_word("TABLE", "FUNCTION", "PROCEDURE", "ROUTINE", "SEQUENCE",
                                             "SCHEMA", "DATABASE"):
        otype = tokens[i].value.upper()
        i += 1
    body = tokens[i:]
    boundary = next((j for j, token in _top(body) if token.is_word("TO" if op == "grant" else "FROM")), None)
    if boundary is None:
        return
    objects = []
    for piece in _pieces(body[:boundary]):
        names, end = _name(piece, 0, star=True)
        if not names:
            continue
        if end < len(piece) and (piece[end].value != "(" or piece[-1].value != ")"):
            continue
        if otype in ("ALL", "SCHEMA", "DATABASE"):
            target = "db_schema:" + names[-1]
        elif names[-1] == "*":
            target = "db_schema:" + (names[-2] if len(names) > 1 else "*")
        elif otype in ("FUNCTION", "PROCEDURE", "ROUTINE"):
            target = "db_routine:" + names[-1]
        else:
            target = "db_table:" + names[-1]
        objects.append(target)
    for piece in _pieces(body[boundary + 1:]):
        names, _end = _name(piece, 0, role=True)
        if names:
            grant = "db_grant:" + names[0]
            graph.event(grant, "grant", file, line, op)
            for target in objects:
                graph.links.add((grant, target, f"{op}:{privileges}", file, line))


def _statement(graph: _Graph, file: str, tokens: list[_Token]) -> None:
    first, line = tokens[0], tokens[0].line
    if first.is_word("GRANT", "REVOKE"):
        _grant(graph, file, tokens)
        return
    if first.is_word("RENAME") and len(tokens) > 1 and tokens[1].is_word("TABLE"):
        for pair in _pieces(tokens[2:]):
            old, i = _name(pair, 0)
            new, _end = _name(pair, i + 1) if i < len(pair) and pair[i].is_word("TO") else ([], i)
            if old and new:
                entity, renamed = "db_table:" + old[-1], "db_table:" + new[-1]
                graph.event(entity, None, file, line, "rename-to:" + new[-1])
                graph.event(renamed, graph.entity(entity)["kind"], file, line, "rename-from:" + old[-1])
        return
    if not first.is_word("CREATE", "ALTER", "DROP"):
        return
    i = 1
    if first.is_word("CREATE"):
        # Modifiers (including DEFINER accounts) precede the object kind.
        while i < len(tokens) and not tokens[i].is_word("TABLE", "VIEW", "FUNCTION", "PROCEDURE", "TRIGGER", "INDEX"):
            i += 1
    elif i < len(tokens) and tokens[i].is_word("MATERIALIZED"):
        i += 1
    if i >= len(tokens) or not tokens[i].is_word("TABLE", "VIEW", "FUNCTION", "PROCEDURE", "TRIGGER", "INDEX"):
        return
    kind = tokens[i].value.lower()
    i += 1
    if kind == "index" and i < len(tokens) and tokens[i].is_word("CONCURRENTLY"):
        i += 1
    i = _skip_exists(tokens, i)
    if kind == "index":
        names, end = ([], i) if i < len(tokens) and tokens[i].is_word("ON") else _name(tokens, i)
        if end < len(tokens) and tokens[end].is_word("ON"):
            table, _end = _name(tokens, _skip_exists(tokens, end + 1))
            if table:
                graph.entity("db_table:" + table[-1])["indexes"].append(
                    [names[-1] if names else "(unnamed)", file, line])
        return
    prefix = "db_table:" if kind in ("table", "view") else "db_routine:"
    if first.is_word("DROP"):
        for piece in _pieces(tokens[i:]):
            names, _end = _name(piece, 0)
            if names:
                entity = prefix + names[-1]
                graph.event(entity, None if entity in graph.entities else kind, file, line, "drop")
        return
    names, end = _name(tokens, i)
    if not names:
        return
    entity, rest = prefix + names[-1], tokens[end:]
    if first.is_word("ALTER"):
        rename = next((j for j in range(len(rest) - 1) if rest[j].is_word("RENAME") and
                       rest[j + 1].is_word("TO")), None)
        renamed, _end = _name(rest, rename + 2) if rename is not None else ([], 0)
        if renamed:
            graph.event(entity, None, file, line, "rename-to:" + renamed[-1])
            graph.event(prefix + renamed[-1], graph.entity(entity)["kind"], file, line, "rename-from:" + names[-1])
        else:
            graph.event(entity, None, file, line, "alter")
    else:
        graph.event(entity, kind, file, line, "create")
    if kind == "table" or first.is_word("ALTER"):
        for name, ref in _refs(rest, ("REFERENCES",)):
            graph.links.add((entity, "db_table:" + name, "foreign-key", file, ref.line))
    elif kind == "view":
        for name, ref in _refs(rest, ("FROM", "JOIN")):
            graph.pending.append((entity, "db_table:" + name, "view-source", file, ref.line))
    else:
        if kind == "trigger":
            on = next(((name, ref) for name, ref in _refs(rest, ("ON",))), None)
            if on:
                graph.links.add((entity, "db_table:" + on[0], "trigger-on", file, on[1].line))
            for j, token in enumerate(rest[:-1]):
                if token.is_word("EXECUTE") and rest[j + 1].is_word("FUNCTION", "PROCEDURE"):
                    called, _end = _name(rest, j + 2)
                    if called:
                        graph.links.add((entity, "db_routine:" + called[-1], "calls", file, token.line))
            if on:
                rest = [token for token in rest if token.start > on[1].start]
        for name, ref in _refs(rest, ("FROM", "JOIN", "INTO", "UPDATE")):
            graph.pending.append((entity, "db_table:" + name, "uses", file, ref.line))


def extract(files: Mapping[str, str]) -> dict:
    """The entity graph of the given SQL files ({path: text}), read in path order."""
    graph = _Graph()
    for file in sorted(files):
        for statement in _statements(_tokenize(files[file])):
            _statement(graph, file, statement)
    return graph.result()
