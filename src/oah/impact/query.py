# 영향 질의 — 저장된 지도에서 게이트·어휘·DB·위키·그래프·공동 변경 층의 행을 만들고, 결속 강도로 순위를 매기며 약한 행만 상한을 둔다. 재순위 호출 자리도 여기 하나뿐이다.
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from oah.impact import lexical
from oah.impact.errors import EXIT_USAGE, ImpactError

BINDINGS = ("pin", "exec", "link", "mention")
STRONG = ("pin", "exec")
LAYERS = ("gates", "lexical", "db", "wiki", "graph", "cochange")
ENTITY = re.compile(r"\Adb_(table|routine|grant):(.+)\Z", re.S)
NO_SQL = "unavailable (no SQL migrations found)"


@dataclass(frozen=True)
class Row:
    binding: str  # pin, exec, link or mention
    layer: str
    kind: str  # the source's file kind, or "gate"
    source: str
    line: int | None
    target: str
    detail: str
    score: int = 0

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @property
    def where(self) -> str:
        return self.source if self.line is None else f"{self.source}:{self.line}"


def rank(rows: Iterable[Row]) -> list[Row]:
    """Binding first (pin > exec > link > mention), then layer, then score, then path."""
    return sorted(rows, key=lambda r: (BINDINGS.index(r.binding), LAYERS.index(r.layer), -r.score, r.source,
                                       r.line or 0, r.target, r.detail))


def cap(rows: Sequence[Row], *, limit: int, strong_only: bool) -> tuple[list[Row], int, int]:
    """(shown, total, overflow). Pin and exec rows are never capped; weaker rows stop at limit."""
    if strong_only:
        shown = [r for r in rows if r.binding in STRONG]
        return shown, len(shown), 0
    strong = [r for r in rows if r.binding in STRONG]
    weak = [r for r in rows if r.binding not in STRONG]
    shown = strong + weak[:max(int(limit), 0)]
    return rank(shown), len(rows), len(rows) - len(shown)


def count_line(total: int, shown: int, overflow: int) -> str:
    return f"total={total} shown={shown} overflow={overflow}"


def apply_rerank(rows: list[Row], *, disabled: bool = False, enabled: bool = False) -> tuple[list[Row], str]:
    """The single re-ranker call site (A2). A provider runs only when the user enabled it explicitly
    (`[impact] rerank = true`) and its key is present; the brief can still turn it off.

    Lane P3b's `oah.rerank.rerank("impact", rows, config=...)` plugs in after the enabled check, and its key
    lookup reports `unused(no-key)`. It may re-order rows but never drop pin or exec rows. Until it lands an
    enabled re-ranker has no provider, so nothing is re-ranked."""
    if disabled:
        return rows, "unused(disabled-by-brief)"
    if not enabled:
        return rows, "unused(not-enabled)"
    return rows, "unused(not-configured)"


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _under(path: str, folders: Iterable[str]) -> bool:
    return any(path == folder.rstrip("/") or path.startswith(folder.rstrip("/") + "/") for folder in folders)


def _owners(files: Iterable[str], min_needle: int) -> dict[str, int]:
    owners: dict[str, int] = {}
    for path in files:
        for needle in lexical.needles_for(path, min_needle):
            owners[needle] = owners.get(needle, 0) + 1
    return owners


def is_ambiguous(needle: str, via: str, owners: Mapping[str, int], settings: Mapping) -> bool:
    """A file name or stem shared by more than max_ambiguity files is not searched; a full path always is."""
    return via != "path" and owners.get(needle, 0) > settings["max_ambiguity"]


def ambiguous(data: Mapping, targets: Sequence[str], settings: Mapping) -> list[dict]:
    """The names the lexical layer skipped for each file target, with how many files share each name."""
    owners = _owners(data["files"], settings["min_needle"])
    skipped = []
    for target in targets:
        if normalize_entity(target):
            continue
        for needle, via in lexical.needles_for(target, settings["min_needle"]).items():
            if is_ambiguous(needle, via, owners, settings):
                skipped.append({"target": target, "name": needle, "via": via, "files": owners[needle],
                                "limit": settings["max_ambiguity"]})
    return skipped


def _lexical(data: Mapping, target: str, owners: Mapping[str, int], settings: Mapping) -> list[Row]:
    best: dict[tuple[str, str], list] = {}
    commands: dict[tuple[str, str, int], list] = {}
    for needle, via in lexical.needles_for(target, settings["min_needle"]).items():
        if is_ambiguous(needle, via, owners, settings):
            continue
        for source, line, flag in data["hits"].get(needle, []):
            if source == target:
                continue
            if flag in STRONG and via != "stem":
                bound = flag
            elif via == "path" or flag == "wikilink":
                bound = "link"
            else:
                bound = "mention"
            key = ("wiki" if _under(source, settings["wiki_dirs"]) else "lexical", source)
            if bound in STRONG:
                location = (*key, line)
                entry = commands.setdefault(location, [bound, via])
                if BINDINGS.index(bound) < BINDINGS.index(entry[0]):
                    entry[:] = [bound, via]
                continue
            entry = best.setdefault(key, [bound, line, via, set()])
            entry[3].add(line)
            if (BINDINGS.index(bound), line) < (BINDINGS.index(entry[0]), entry[1]):
                entry[0], entry[1], entry[2] = bound, line, via
    rows = [Row(bound, layer, data["files"].get(source, "code"), source, line, target, via, 1)
            for (layer, source, line), (bound, via) in commands.items()]
    strong_sources = {(layer, source) for layer, source, _line in commands}
    for (layer, source), (bound, line, via, lines) in best.items():
        if (layer, source) in strong_sources:
            continue
        detail = via if len(lines) == 1 else f"{via}, {len(lines)} lines"
        rows.append(Row(bound, layer, data["files"].get(source, "code"), source, line, target, detail, len(lines)))
    return rows


def _cochange(data: Mapping, target: str, settings: Mapping) -> list[Row]:
    touched = [names for _sha, names in data["history"] if target in names][:settings["cochange_commits"]]
    considered = [names for names in touched if len(names) <= settings["cochange_max_files"]]
    counts: dict[str, int] = {}
    for names in considered:
        for name in names:
            if name != target and name in data["files"]:
                counts[name] = counts.get(name, 0) + 1
    return [Row("mention", "cochange", data["files"][name], name, None, target,
                f"changed together in {count} of {len(considered)} commits", count)
            for name, count in counts.items()]


def load_graph(root: pathlib.Path, settings: Mapping) -> dict | None:
    """graphify's output as {"files": {node: file}, "edges": [[a, b, relation]]}; None when absent or unreadable."""
    return read_graph(root, settings)[0]


def read_graph(root: pathlib.Path, settings: Mapping, commit: str | None = None) -> tuple[dict | None, str, str]:
    """(graph, status, reason). The status is analysed only when the graph records commit; a graph with no
    checkable commit identity is stale-unknown, never assumed fresh."""
    name = settings["graph_file"]
    try:
        raw = json.loads((pathlib.Path(root) / name).read_bytes().decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("invalid graph document")
        nodes = raw.get("nodes")
        links = raw.get("links", raw.get("edges"))
        if (not isinstance(nodes, list) or not isinstance(links, list)
                or any(not isinstance(node, dict) or not isinstance(node.get("id"), str)
                       or any(key in node and not isinstance(node[key], str)
                              for key in ("source_file", "file", "path")) for node in nodes)):
            raise ValueError("invalid graph collections")
        declared = {node["id"] for node in nodes}
        if len(declared) != len(nodes):
            return None, "unavailable", (f"OAH_IMPACT_GRAPH_UNAVAILABLE: {name} has duplicate node IDs; "
                                         "run oah graph update to rebuild it")
        if any(not isinstance(edge, dict)
               or any(not isinstance(edge.get(key), str) or edge[key] not in declared
                      for key in ("source", "target")) for edge in links):
            raise ValueError("invalid graph endpoints")
        files = {}
        for node in nodes:
            file = node.get("source_file") or node.get("file") or node.get("path")
            if isinstance(file, str):
                files[node["id"]] = file.replace("\\", "/")
        edges = [[e["source"], e["target"], str(e.get("relation") or e.get("label") or "")]
                 for e in links]
        meta = raw.get("graph")
        recorded = raw.get("commit") or (meta.get("commit") if isinstance(meta, dict) else None)
    except FileNotFoundError:
        return None, "unavailable", f"{name} is missing; run oah graph update to build it"
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None, "unavailable", (f"OAH_IMPACT_GRAPH_UNAVAILABLE: {name} cannot be read as a graph; "
                                     "run oah graph update to rebuild it")
    graph = {"files": files, "edges": edges}
    if recorded is None:
        return graph, "stale-unknown", (f"{name} records no commit, so its freshness cannot be checked; "
                                        "re-check graph neighbours by hand")
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", recorded):
        return graph, "stale-unknown", (f"{name} records {str(recorded)[:40]!r}, which is not a full commit id, so "
                                        "its freshness cannot be checked; re-check graph neighbours by hand")
    recorded = recorded.lower()
    if commit is not None and recorded != commit.lower():
        return graph, "stale", (f"{name} was built at commit {recorded[:12]}, not {commit[:12]}; run oah graph "
                                "update to rebuild it")
    return graph, "analysed", f"{name} built at commit {recorded[:12]}"


def _graph(graph: Mapping | None, data: Mapping, target: str) -> list[Row]:
    if not graph:
        return []
    mine = {node for node, file in graph["files"].items() if file == target}
    neighbours: dict[str, set[str]] = {}
    for a, b, relation in graph["edges"]:
        for here, there in ((a, b), (b, a)):
            file = graph["files"].get(there)
            if here in mine and file and file != target and file in data["files"]:
                neighbours.setdefault(file, set()).add(relation)
    rows = []
    for file, relations in neighbours.items():
        named = ", ".join(sorted(r for r in relations if r))
        rows.append(Row("mention", "graph", data["files"][file], file, None, target,
                        f"graph neighbour ({named})" if named else "graph neighbour", len(relations)))
    return rows


def merge_maps(worktree: Sequence[Row], default_branch: Sequence[Row]) -> tuple[list[Row], dict[Row, str]]:
    """(ranked rows, where each was found: both, worktree or default-branch). Rows with the same binding, layer,
    source, line and target are one row; the worktree's copy is kept, so nothing is printed twice and no pin or
    exec row is dropped."""
    def key(row: Row) -> tuple:
        return row.binding, row.layer, row.source, row.line, row.target

    theirs = {key(row) for row in default_branch}
    found: dict[Row, str] = {row: "both" if key(row) in theirs else "worktree" for row in worktree}
    mine = {key(row) for row in worktree}
    for row in default_branch:
        if key(row) not in mine:
            mine.add(key(row))
            found[row] = "default-branch"
    return rank(found), found


def normalize_entity(text: str) -> str | None:
    match = ENTITY.match(text)
    return f"db_{match.group(1)}:{match.group(2).strip().lower()}" if match else None


def _db_files(data: Mapping, settings: Mapping) -> set[str]:
    from oah.impact.mapstore import db_files

    return set(db_files(data["files"], settings))


def _code_refs(data: Mapping, name: str, sql: set[str]) -> dict[str, list[int]]:
    refs: dict[str, list[int]] = {}
    for source, line in data["db_hits"].get(name, []):
        if source not in sql:
            refs.setdefault(source, []).append(line)
    return refs


def _ref_binding(kind: str) -> str:
    return "link" if kind in ("code", "test") else "mention"


def db_entity_rows(data: Mapping, entity: str, settings: Mapping) -> list[Row]:
    graph = data["db"]
    if not graph:
        raise ImpactError("OAH_IMPACT_DB_UNAVAILABLE", f"db-impact: {NO_SQL}; add SQL migration files, "
                          "or query file paths instead", EXIT_USAGE)
    info = graph["entities"].get(entity)
    if info is None:
        raise ImpactError("OAH_IMPACT_UNKNOWN_ENTITY", f"{entity} is not in the DB graph at this commit; "
                          "run oah impact <migration file> to see the entities it defines", EXIT_USAGE)
    files, rows = data["files"], []
    for file, line, op in info["events"]:
        rows.append(Row("link", "db", files.get(file, "migration"), file, line, entity, op))
    for name, file, line in info["indexes"]:
        rows.append(Row("link", "db", files.get(file, "migration"), file, line, entity, f"index {name}"))
    for source, target, relation, file, line in graph["links"]:
        if target == entity:
            rows.append(Row("link", "db", files.get(file, "migration"), file, line, entity, f"{relation} by {source}"))
        elif source == entity:
            rows.append(Row("mention", "db", files.get(file, "migration"), file, line, entity,
                            f"{relation} from {source} to {target}"))
    for source, lines in _code_refs(data, info["name"], _db_files(data, settings)).items():
        kind = files.get(source, "code")
        rows.append(Row(_ref_binding(kind), "db", kind, source, min(lines), entity, "code-ref", len(lines)))
    return rows


def _db_path(data: Mapping, target: str, settings: Mapping) -> list[Row]:
    graph = data["db"]
    if not graph:
        return []
    files, sql, rows = data["files"], _db_files(data, settings), []
    for entity, info in graph["entities"].items():
        if target in sql:
            events = [(line, op) for file, line, op in info["events"] if file == target]
            events += [(line, f"index {name}") for name, file, line in info["indexes"] if file == target]
            if not events:
                continue
            line, op = min(events)
            rows.append(Row("link", "db", files.get(target, "migration"), target, line, target, f"{op} {entity}"))
            for source, lines in _code_refs(data, info["name"], sql).items():
                kind = files.get(source, "code")
                rows.append(Row(_ref_binding(kind), "db", kind, source, min(lines), target, f"uses {entity}",
                                len(lines)))
        elif any(source == target for source, _line in data["db_hits"].get(info["name"], [])) and info["events"]:
            file, line, _op = info["events"][0]
            rows.append(Row("mention", "db", files.get(file, "migration"), file, line, target, f"uses {entity}"))
    return rows


def query(data: Mapping, targets: Sequence[str], *, gates: Sequence = (), graph: Mapping | None = None,
          settings: Mapping) -> list[Row]:
    """Every row for the targets (paths or db_* entities), ranked, uncapped."""
    from oah.impact import gatebind

    owners = _owners(data["files"], settings["min_needle"])
    rows: list[Row] = []
    for target in targets:
        entity = normalize_entity(target)
        if entity:
            rows += db_entity_rows(data, entity, settings)
            continue
        rows += gatebind.rows(gates, target, min_needle=settings["min_needle"])
        rows += _lexical(data, target, owners, settings)
        rows += _db_path(data, target, settings)
        rows += _graph(graph, data, target)
        rows += _cochange(data, target, settings)
    return rank(rows)


def row_json(row: Row, found: Mapping[Row, str] | None = None) -> dict:
    """A row as printed: with `found_in` (both, worktree or default-branch) when the maps were merged."""
    return row.to_json() if found is None else dict(row.to_json(), found_in=found.get(row, "worktree"))


def db_identity(data: Mapping, commit: str, shown: Sequence[Row], found: Mapping[Row, str] | None = None, *,
                default_branch: tuple[Mapping, str] | None = None) -> dict:
    """DB graph identities (A3), plus a separate digest of the returned DB rows when maps are merged.

    default_branch is (data, commit). Without its DB graph, retain P7i's single-worktree identity.
    """
    from oah.impact.mapstore import digest

    output = digest([row_json(row, found) for row in shown if row.layer == "db"])
    if default_branch is not None and default_branch[0]["db"] is not None:
        theirs, their_commit = default_branch
        maps = {}
        if data["db"] is not None:
            maps["worktree"] = {"commit": commit, "graph": digest(data["db"])}
        maps["default_branch"] = {"commit": their_commit, "graph": digest(theirs["db"])}
        return {"status": "available", "maps": maps, "merged_output": output}
    if data["db"] is None:
        return {"status": "unavailable", "reason": "no SQL migrations found"}
    return {"status": "available", "commit": commit, "graph": digest(data["db"]), "output": output}


def db_line(identity: Mapping) -> str:
    if identity["status"] != "available":
        return f"db-impact: {NO_SQL}"
    if "maps" in identity:
        maps = " ".join(f"{name.replace('_', '-')}=(commit={info['commit']} graph={info['graph']})"
                        for name, info in identity["maps"].items())
        return f"db-impact: {maps} merged-output={identity['merged_output']}"
    return f"db-impact: commit={identity['commit']} graph={identity['graph']} output={identity['output']}"


def rerun_plan(rows: Sequence[Row]) -> list[dict]:
    """Pin and exec rows grouped by gate or command line: what to re-run after these files changed."""
    plan: dict[tuple, dict] = {}
    for row in rank(rows):
        if row.binding not in STRONG:
            continue
        item = plan.setdefault((row.source, row.line), {"source": row.source, "line": row.line,
                                                        "binding": row.binding, "targets": []})
        if row.target not in item["targets"]:
            item["targets"].append(row.target)
    for item in plan.values():
        item["targets"].sort()
    return list(plan.values())


def select_scope(scope: Sequence[str], files: Mapping[str, str],
                 kinds: Sequence[str] | None = None) -> tuple[list[str], list[str]]:
    """Files selected by a task's Scope (and optional kinds), plus warnings. Never selects nothing silently (A4)."""
    paths, warnings = sorted(files), []
    if not scope:
        warnings.append("OAH_SCOPE_EMPTY: the task's Scope lists no paths, so impact selects the whole repository "
                        "instead. Add paths with oah renew --scope <path>.")
    elif "repo" not in scope:
        keys = [_fold(entry.rstrip("/")) for entry in scope]
        paths = [p for p in paths if any(_fold(p) == key or _fold(p).startswith(key + "/") for key in keys)]
    if kinds:
        paths = [p for p in paths if files[p] in kinds]
    if not paths:
        what = "/".join(kinds) + " " if kinds else ""
        warnings.append(f"OAH_SCOPE_NO_MATCH: no tracked {what}files match Scope "
                        f"{', '.join(scope) if scope else '(empty)'}, so nothing was selected. Widen the Scope "
                        "with oah renew --scope, or drop --kind.")
    return paths, warnings
