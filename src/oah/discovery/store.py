# 스킬 탐색 저장소 — <state>/skills 아래의 검토 기록·검색 캐시·공개 질의 선택·작업 선택·출처 신원 표·안내 기록을 만들고 읽는다.
"""Everything discovery keeps lives under `<state>/skills/`, apart from the user's followed design sources.

    reviews/<h2>/<hex>.json        ReviewedSkillV1, create-only, named by the digest of the record without its id
    reviews/index.json             skill name and locator -> review ids (rebuilt atomically, never authoritative)
    queries/<h2>/<hex>.json        one search outcome per {provider, query, parser}
    identities.json                E source id -> the full 256-bit identity and canonical spec fields
    choices/<id>.json              PublicQueryChoiceV1 (a lead's explicit public query)
    task-selections/<id>.json      TaskSkillSelectionV1 (a lead's selection for one non-design task need)
    notice.json                    when the one-time external-search notice was shown
    sources/                       C's collector state for skill folders (a separate root)

Records are data. Nothing here installs, executes or delivers anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
from typing import Mapping

from oah.atomic import StateCorrupt, write_text_atomic
from oah.design.sources.state import StateProblem, loads

LIMIT = 1 << 20


class StoreProblem(Exception):
    """A discovery state file oah cannot use; it is never repaired or reset."""

    def __init__(self, path: pathlib.Path, detail: str) -> None:
        self.path = path
        super().__init__(f"OAH_SKILL_STATE_CORRUPT: {path}: {detail}; oah never repairs or resets it. Move the file "
                         "aside to start that part again")


def root(state_dir: os.PathLike | str) -> pathlib.Path:
    return pathlib.Path(state_dir) / "skills"


def canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(data: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(data)).hexdigest()


def with_id(record: Mapping) -> dict:
    """The record with `id` = the digest of everything else."""
    body = {key: value for key, value in record.items() if key != "id"}
    return {**body, "id": digest(body)}


def _read(path: pathlib.Path) -> object | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreProblem(path, f"cannot be read ({exc.strerror or exc})") from None
    if len(raw) > LIMIT:
        raise StoreProblem(path, "is larger than 1 MiB")
    try:
        return loads(raw, path.name)
    except StateProblem as exc:
        raise StoreProblem(path, exc.detail) from None


def _write_once(path: pathlib.Path, record: Mapping) -> str:
    """Create-only: 'stored', or 'present' when identical bytes are already there; different bytes refuse."""
    data = json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False) + "\n"
    existing = _read(path)
    if existing is not None:
        if existing != json.loads(data):
            raise StoreProblem(path, "already holds a different record under the same name")
        return "present"
    write_text_atomic(path, data)
    return "stored"


def _content_path(folder: pathlib.Path, record_id: str) -> pathlib.Path:
    hexdigest = record_id.split(":", 1)[1]
    return folder / hexdigest[:2] / f"{hexdigest}.json"


def _verified(record: object, record_id: str, path: pathlib.Path) -> dict | None:
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("id") != record_id or with_id(record)["id"] != record_id:
        raise StoreProblem(path, "does not match its own digest (changed after it was stored)")
    return record


# Reviews -------------------------------------------------------------------------------------------------------------

def put_review(state_dir: os.PathLike | str, record: Mapping) -> tuple[str, dict]:
    record = with_id(record)
    path = _content_path(root(state_dir) / "reviews", record["id"])
    word = _write_once(path, record)
    _index_review(state_dir, record)
    return word, record


def get_review(state_dir: os.PathLike | str, record_id: str) -> dict | None:
    if not isinstance(record_id, str) or not record_id.startswith("sha256:") or len(record_id) != 71:
        return None
    path = _content_path(root(state_dir) / "reviews", record_id)
    return _verified(_read(path), record_id, path)


def reviews(state_dir: os.PathLike | str) -> list[dict]:
    folder = root(state_dir) / "reviews"
    out = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("??/*.json")):
        record_id = "sha256:" + path.stem
        record = _verified(_read(path), record_id, path)
        if record is not None:
            out.append(record)
    return out


def reviews_of(state_dir: os.PathLike | str, locator: str) -> list[dict]:
    """The stored reviews of one source locator (through the index; each record is verified on read)."""
    index = _read(root(state_dir) / "reviews" / "index.json") or {}
    ids = (index.get("by_locator") or {}).get(locator, []) if isinstance(index, dict) else []
    return [record for record_id in ids if (record := get_review(state_dir, record_id)) is not None]


def _index_review(state_dir: os.PathLike | str, record: Mapping) -> None:
    path = root(state_dir) / "reviews" / "index.json"
    index = _read(path) or {"version": 1, "by_name": {}, "by_locator": {}}
    if not isinstance(index, dict) or set(index) != {"version", "by_name", "by_locator"}:
        raise StoreProblem(path, "is not a review index")
    name = (record.get("frontmatter") or {}).get("name") or (record.get("candidate") or {}).get("seed") or "-"
    locator = (record.get("source") or {}).get("locator") or "-"
    for key, value in (("by_name", name), ("by_locator", locator)):
        ids = index[key].setdefault(str(value), [])
        if record["id"] not in ids:
            ids.append(record["id"])
    write_text_atomic(path, json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


# Search outcomes -----------------------------------------------------------------------------------------------------

def query_id(provider: str, query: str, parser: str) -> str:
    return digest({"provider": provider, "query": query, "parser": parser})


def put_query(state_dir: os.PathLike | str, record: Mapping) -> None:
    path = _content_path(root(state_dir) / "queries", record["query_id"])
    write_text_atomic(path, json.dumps(dict(record), ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def get_query(state_dir: os.PathLike | str, ident: str) -> dict | None:
    if not isinstance(ident, str) or not ident.startswith("sha256:") or len(ident) != 71:
        return None
    path = _content_path(root(state_dir) / "queries", ident)
    record = _read(path)
    if record is not None and (not isinstance(record, dict) or record.get("query_id") != ident):
        raise StoreProblem(path, "is not the search outcome it is named after")
    return record


# Source identities ---------------------------------------------------------------------------------------------------

def identity(state_dir: os.PathLike | str, source_id: str) -> dict | None:
    table = _identities(state_dir)
    return table.get(source_id)


def remember_identity(state_dir: os.PathLike | str, source_id: str, full: str, fields: Mapping) -> str:
    """'stored' | 'present'; a different full identity under the same short id refuses (never overwrites)."""
    path = root(state_dir) / "identities.json"
    table = _identities(state_dir)
    entry = {"full": full, "fields": dict(fields)}
    if source_id in table:
        if table[source_id] != entry:
            raise StoreProblem(path, f"{source_id} already names a different skill source (a truncated identity "
                               "collision); oah refuses instead of overwriting it")
        return "present"
    table[source_id] = entry
    write_text_atomic(path, json.dumps({"version": 1, "sources": table}, ensure_ascii=False, indent=1,
                                       sort_keys=True) + "\n")
    return "stored"


def _identities(state_dir: os.PathLike | str) -> dict:
    path = root(state_dir) / "identities.json"
    data = _read(path)
    if data is None:
        return {}
    if not isinstance(data, dict) or set(data) != {"version", "sources"} or not isinstance(data["sources"], dict):
        raise StoreProblem(path, "is not a source identity table")
    return data["sources"]


# Choices and selections ----------------------------------------------------------------------------------------------

def put_record(state_dir: os.PathLike | str, folder: str, record: Mapping) -> tuple[str, dict]:
    record = with_id(record)
    path = root(state_dir) / folder / f"{record['id'].split(':', 1)[1]}.json"
    return _write_once(path, record), record


def get_record(state_dir: os.PathLike | str, folder: str, record_id: str) -> dict | None:
    if not isinstance(record_id, str) or not record_id.startswith("sha256:") or len(record_id) != 71:
        return None
    path = root(state_dir) / folder / f"{record_id.split(':', 1)[1]}.json"
    return _verified(_read(path), record_id, path)


def records(state_dir: os.PathLike | str, folder: str) -> list[dict]:
    path = root(state_dir) / folder
    if not path.is_dir():
        return []
    return [record for item in sorted(path.glob("*.json"))
            if (record := _verified(_read(item), "sha256:" + item.stem, item)) is not None]


# The one-time notice -------------------------------------------------------------------------------------------------

def notice_shown(state_dir: os.PathLike | str) -> bool:
    return _read(root(state_dir) / "notice.json") is not None


def record_notice(state_dir: os.PathLike | str, provider: str, at: str) -> None:
    write_text_atomic(root(state_dir) / "notice.json",
                      json.dumps({"version": 1, "provider": provider, "shown_at": at}, sort_keys=True) + "\n")


class ReviewEvidence:
    """coverage.Evidence over this state folder: reviewed skills, and verified source snapshots (E's and C's)."""

    def __init__(self, state_dir: os.PathLike | str) -> None:
        self.state_dir = pathlib.Path(state_dir)

    def review(self, record_id: str) -> Mapping | None:
        try:
            record = get_review(self.state_dir, record_id)
        except (StoreProblem, StateCorrupt):
            return None
        if record is None or record.get("status") != "reviewed":
            return None
        return record

    def source(self, snapshot: str) -> Mapping | None:
        from oah.design.sources.check import Sources
        from oah.design.sources.model import SourceError

        for folder in (self.state_dir, root(self.state_dir)):
            try:
                found = Sources(folder).snapshot(snapshot)
            except (SourceError, StateProblem, OSError, ValueError):
                found = None
            if found is not None:
                return found
        return None
