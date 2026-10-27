# 영향 범위 보고 — 층마다 분석했는지·못했는지와 이유, 분석하지 못한 목록, 빈 결과 문구, 불완전할 때의 대체 점검(약한 결속)을 만든다.
from __future__ import annotations

import pathlib
import re
from typing import Iterable, Mapping, Sequence

from oah.impact import lexical
from oah.impact.query import normalize_entity

DEPTH = 1
LAYERS = ("gates", "lexical", "db", "graph", "cochange")
DYNAMIC_NOTE = ("dynamic calls, reflection and string-built paths (such as importlib, getattr or a path joined "
                "at run time) are not traced")
EMPTY = "nothing found in the analysed scope; this is not proof of no impact"
CONFIGURED = "[impact] fallback_checks"
_UNAVAILABLE = re.compile(r"\Aunavailable \((.*)\)\Z", re.S)


def _layer(status: str, reason: str) -> dict:
    return {"status": status, "reason": reason}


def layers(*, targets: Sequence[str], gate_status: str, db_available: bool, graph_status: tuple[str, str],
           branch_state: str, branch_reason: str) -> dict[str, dict]:
    """Each layer's status (analysed, unavailable, stale, stale-unknown or skipped) and why."""
    if not targets:
        return {name: _layer("skipped", "nothing was selected, so no layer ran") for name in LAYERS}
    files = [target for target in targets if normalize_entity(target) is None]
    entities_only = _layer("skipped", "only DB entities were queried; this layer reads file paths")
    stale = (f"the default-branch map is from the last fetched commit ({branch_reason}); checks added there "
             "since are missing") if branch_state == "stale" else ""

    def mapped(reason: str) -> dict:
        return _layer("stale", stale) if stale else _layer("analysed", reason)

    out = {}
    if not files:
        out["gates"] = entities_only
    else:
        missing = _UNAVAILABLE.match(gate_status)
        out["gates"] = (_layer("unavailable", missing.group(1)) if missing
                        else _layer("analysed", gate_status.split(";", 1)[0]))
    out["lexical"] = (entities_only if not files else
                      mapped("git grep for each target's path, file name and stem (wiki notes included)"))
    out["db"] = (_layer("unavailable", "no SQL migrations found") if not db_available
                 else mapped("SQL migrations parsed for tables, routines and grants"))
    out["graph"] = entities_only if not files else _layer(*graph_status)
    out["cochange"] = entities_only if not files else mapped("recent commits that touched each target")
    return out


def _ambiguous_item(item: Mapping) -> dict:
    detail = (f"{item['name']} ({item['via']} of {item['target']}) is shared by {item['files']} files, more than "
              f"max_ambiguity={item['limit']}, so it was not searched; search for it by hand or raise "
              "[impact] max_ambiguity")
    return dict(item, kind="ambiguous-name", detail=detail)


def _rename_item(item: Mapping) -> dict:
    how = "same file name" if item["similarity"] is None else f"git similarity {item['similarity']}%"
    detail = (f"{item['from']} -> {item['to']} looks like a rename ({how}); impact read it as a separate delete "
              "and add, so re-check references to both paths")
    return dict(item, kind="rename", detail=detail)


def report(layers: Mapping[str, Mapping], ambiguous: Sequence[Mapping], renames: Sequence[Mapping], *,
           rows_found: int) -> dict:
    """The scope block: depth, complete, per-layer status, what was not analysed, and a summary sentence."""
    unanalysed = [_ambiguous_item(item) for item in ambiguous]
    for name, layer in layers.items():
        if layer["status"] not in ("analysed", "skipped"):
            unanalysed.append({"kind": "layer", "layer": name, "status": layer["status"],
                               "detail": f"{name}: {layer['reason']}"})
    unanalysed += [_rename_item(item) for item in renames]
    unanalysed.append({"kind": "dynamic", "detail": DYNAMIC_NOTE})
    complete = all(layer["status"] == "analysed" for layer in layers.values()) and not ambiguous and not renames
    if not rows_found:
        summary = EMPTY
    elif complete:
        summary = (f"{rows_found} found in the analysed scope; every layer ran fresh, but dynamic calls and "
                   "string-built paths are still not traced")
    else:
        summary = (f"{rows_found} found in the analysed scope; the analysis is incomplete, so also run the "
                   "fallback checks")
    return {"depth": DEPTH, "complete": complete, "layers": {name: dict(layer) for name, layer in layers.items()},
            "unanalysed": unanalysed, "summary": summary}


def lines(scope: Mapping) -> list[str]:
    out = [f"scope: depth={scope['depth']} complete={'true' if scope['complete'] else 'false'}"]
    out += [f"  {layer['status']:<14}{name:<10}{layer['reason']}" for name, layer in scope["layers"].items()]
    out.append("not analysed:")
    out += [f"  - {item['detail']}" for item in scope["unanalysed"]]
    return out


def likely_renames(root: pathlib.Path, since: str, ref: str | None, *, timeout: float) -> list[dict]:
    """Deleted and added paths that look like one rename: git's -M pairs, then pairs with the same file name.

    Without ref the worktree is compared, and untracked files count as added."""
    from oah.impact.mapstore import _git

    fields = [field for field in _git(root, "diff", "-M", "--name-status", "-z", since, *([ref] if ref else []),
                                      "--", timeout=timeout).split("\0") if field]
    pairs, deleted, added, index = [], [], [], 0
    while index < len(fields):
        status = fields[index]
        if status[0] in "RC":
            if status[:1] == "R":
                pairs.append({"from": fields[index + 1], "to": fields[index + 2],
                              "similarity": int(status[1:]) if status[1:].isdigit() else None})
            else:
                added.append(fields[index + 2])
            index += 3
            continue
        if status == "D":
            deleted.append(fields[index + 1])
        elif status == "A":
            added.append(fields[index + 1])
        index += 2
    if ref is None:
        added += [name for name in _git(root, "ls-files", "--others", "--exclude-standard", "-z",
                                        timeout=timeout).split("\0") if name]
    for old in deleted:
        name = old.rsplit("/", 1)[-1]
        pairs += [{"from": old, "to": new, "similarity": None} for new in added if new.rsplit("/", 1)[-1] == name]
    return sorted(pairs, key=lambda pair: (pair["from"], pair["to"]))


def fallback_needles(target: str) -> set[str]:
    """The folder (as a path and dotted) and the dotted module names, down to two segments, of a file target."""
    parts = pathlib.PurePosixPath(target).parts
    if not parts:
        return set()
    folder = list(parts[:-1])
    needles = {"/".join(folder), ".".join(folder)} if folder else set()
    dotted = folder + [pathlib.PurePosixPath(parts[-1]).stem]
    needles.update(".".join(dotted[i:]) for i in range(len(dotted) - 1))
    return {needle for needle in needles if needle}


def _names(text: str, needles: Iterable[str]) -> list[str]:
    found = set()
    for match in lexical._TOKEN.finditer(text):
        token = match.group().replace("\\", "/")
        for needle in needles:
            if re.search(r"(?:\A|[/.])" + re.escape(needle) + r"(?=\Z|[/.])", token):
                found.add(needle)
    return sorted(found)


def fallback(gates: Sequence, targets: Sequence[str], *, strong: Sequence[Mapping],
             checks: Sequence[str]) -> list[dict]:
    """Weak checks for an incomplete result: every gate of each ledger whose CHECK or EXPECT names a file
    target's folder or module, minus gates already bound by pin or exec, then the configured checks."""
    files = sorted(target for target in targets if normalize_entity(target) is None)
    bound = {item["source"] for item in strong}
    ledgers: dict[str, dict[str, set[str]]] = {}
    for target in files:
        needles = fallback_needles(target)
        for gate in gates:
            for needle in _names(f"{gate.check}\n{gate.expect}", needles):
                ledgers.setdefault(gate.ledger, {}).setdefault(target, set()).add(needle)
    out = []
    for gate in gates:
        matched = ledgers.get(gate.ledger)
        if not matched or gate.label in bound:
            continue
        named = sorted({needle for needles in matched.values() for needle in needles})
        out.append({"source": gate.label, "line": None, "binding": "fallback", "check": gate.check,
                    "why": f"its ledger names {', '.join(named)}, a folder or module of the target",
                    "targets": sorted(matched)})
    out.sort(key=lambda item: item["source"])
    if files:
        out += [{"source": CONFIGURED, "line": None, "binding": "fallback", "check": check,
                 "why": f"configured in {CONFIGURED}", "targets": files} for check in checks]
    return out
