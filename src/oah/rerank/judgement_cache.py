# 판정 캐시 — 제한된 재사용 파일만 만료시키며 설계·제안이 참조하는 불변 평가 증거는 출처 저장소에 따로 남긴다.
from __future__ import annotations

import datetime as dt
import itertools
import os
from pathlib import Path
import re
from typing import Mapping

from oah.atomic import StorageError
from oah.design.sources.state import ASSESSMENT_LIMIT, State, StateProblem, _is_link, record_bytes
from oah.lockfile import LockTimeout

VERSION_MODEL = re.compile(r"jev-[0-9]+\.[0-9]+\.[0-9]+")
MAX_FILES = 4096
_FIELDS = {"version", "kind", "status", "purpose", "identity", "outcome", "choice", "labels", "answers",
           "provider", "applied", "answered_model", "assessed_at", "score_level"}


class CacheState(State):
    def __init__(self, state_dir: Path) -> None:
        super().__init__(state_dir)
        self.root = state_dir / "rerank" / "judgements"
        if any(_is_link(path) for path in (state_dir, self.root.parent, self.root)):
            raise StateProblem("symlink", "judgements", "cache directories must not be links or reparse points")


def _age(record: Mapping, now: dt.datetime) -> float | None:
    value = record.get("assessed_at")
    try:
        at = dt.datetime.fromisoformat(value)
        if at.tzinfo is None:
            return None
        age = (now - at).total_seconds()
        return age if age >= 0 else None
    except (TypeError, ValueError):
        return None


def load(state_dir: Path | None, key: str, identity: Mapping, now: dt.datetime,
         hours: float) -> dict | None:
    if state_dir is None or hours <= 0:
        return None
    try:
        state = CacheState(state_dir)
        record = state.read_json(f"{key[7:9]}/{key[7:]}.json", ASSESSMENT_LIMIT)
        if not isinstance(record, dict) or set(record) != _FIELDS or record.get("version") != 1 \
                or record.get("kind") != "oah-judgement" or record.get("identity") != identity \
                or record.get("status") != "used" or record.get("purpose") != identity.get("purpose") \
                or record.get("provider") != identity.get("provider") or record.get("applied") != "not-applied":
            return None
        answered, requested = record.get("answered_model"), identity.get("requested_model")
        if not isinstance(answered, str) or not VERSION_MODEL.fullmatch(answered):
            return None
        if isinstance(requested, str) and VERSION_MODEL.fullmatch(requested):
            if requested != answered:
                return None
            ttl = 30 * 86400
        else:
            ttl = min(hours * 3600, 30 * 86400)
        age = _age(record, now)
        return record if age is not None and age < ttl else None
    except (OSError, StateProblem, StorageError):
        return None


def save(state_dir: Path | None, key: str, record: Mapping, now: dt.datetime) -> None:
    if state_dir is None or record.get("status") != "used" or record.get("answered_model") is None \
            or len(record_bytes(record)) > ASSESSMENT_LIMIT:
        return
    try:
        state = CacheState(state_dir)
        state.root.mkdir(parents=True, exist_ok=True)
        if state.root.is_symlink() or state.root.parent.is_symlink():
            return
        with state.lock():
            seen = 0
            prefixes = list(itertools.islice(state.root.iterdir(), 258))
            if len(prefixes) > 257:
                return
            for prefix in sorted(prefixes):
                if not re.fullmatch(r"[0-9a-f]{2}", prefix.name) or prefix.is_symlink() or not prefix.is_dir():
                    continue
                for path in prefix.iterdir():
                    seen += 1
                    if seen > MAX_FILES:
                        return
                    if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name) or path.is_symlink():
                        continue
                    rel = f"{prefix.name}/{path.name}"
                    old = state.read_json(rel, ASSESSMENT_LIMIT)
                    age = _age(old, now) if isinstance(old, Mapping) else None
                    if age is not None and age > 30 * 86400:
                        os.unlink(state.path(rel))
            state.write_json(f"{key[7:9]}/{key[7:]}.json", record)
    except (OSError, StateProblem, StorageError, LockTimeout):
        return
