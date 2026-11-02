# 벤치마크 점수 자료 — Epoch AI CSV 를 해석하고, 카탈로그를 새로 만들 때 점수를 받아 상태 폴더에 검증해 두며, 엔진마다 쓸 자료 하나를 고른다.
"""Benchmark data: Epoch AI's CSV, fetched score files and the one choice of data per engine.

Epoch AI CSV
    `epoch_scores(data)` keeps, for each allow-listed task (`TASKS`), the latest successful run of each model at
    maximum effort. Data from external projects keeps its own licence, so only Epoch's own tasks are kept.
    `tools/build_benchmarks.py` and a fetch call the same function, so both give the same scores for the same CSV.
    `render` writes the packaged snapshot's bytes.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from typing import Mapping

EPOCH_SOURCE = "Epoch AI"
EPOCH_LICENSE = "CC BY 4.0"
EPOCH_CREDIT_URL = "https://epoch.ai/benchmarks/use-this-data"
MODEL = r"(gpt-[0-9.]+-[a-z]+|claude-[a-z]+-[0-9-]+)_max"
TASKS = ("FrontierMath-Tiers-1-3-v2-Private", "FrontierMath-Tier-4-v2-Private", "Mystery Game Puzzles",
         "Furniture Assembly", "Chess Puzzles", "EBR-bench", "OTIS Mock AIME 2024-2025")
COLUMNS = ("model", "started_at", "Status", "mean_score", "original_task_name")
_MODEL = re.compile(MODEL)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # never the locale's


class DataError(Exception):
    """The data is not what oah expects; the message says why in plain words and names no file."""


def parse_mean(text: str) -> float | None:
    """The score, or None when the row has no finite mean."""
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _moment(text: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.timezone.utc)


def epoch_scores(data: bytes) -> dict[str, dict[str, float]]:
    """{model id: {task: mean}}, the latest successful run of each pair, from the bytes of Epoch's CSV.

    The bytes are decoded as UTF-8 whatever the locale, and `newline=""` keeps multi-line fields whole and reads
    Windows line endings the same."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DataError("is not UTF-8 text") from None
    latest: dict[tuple[str, str], tuple[dt.datetime, float]] = {}
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        missing = [name for name in COLUMNS if name not in (reader.fieldnames or ())]
        if missing:
            raise DataError(f"has no column {', '.join(missing)}")
        for row in reader:
            match = _MODEL.fullmatch(row["model"] or "")
            task = row["original_task_name"]
            mean = parse_mean(row["mean_score"] or "")
            if match is None or task not in TASKS or row["Status"] != "Success" or mean is None:
                continue
            if not 0 <= mean <= 1:
                raise DataError(f"says {match.group(1)} scores {mean} on {task}, outside 0 to 1, so the scale may "
                                "have changed")
            try:
                started = _moment(row["started_at"])
            except (TypeError, ValueError):
                raise DataError(f"gives {match.group(1)} on {task} the unreadable time "
                                f"{(row['started_at'] or '')[:40]!r}") from None
            key = (match.group(1), task)
            if key not in latest or started >= latest[key][0]:
                latest[key] = (started, mean)
    except csv.Error as exc:
        raise DataError(f"cannot be read as CSV ({exc})") from None
    if not latest:
        raise DataError("has no rows for the allowed tasks")
    scores: dict[str, dict[str, float]] = {}
    for (model, task), (_started, mean) in sorted(latest.items()):
        scores.setdefault(model, {})[task] = mean
    return scores


def epoch_credit(date: dt.date) -> str:
    """The credit Epoch AI asks for, for data read on `date`."""
    return (f"Epoch AI, 'Capabilities & benchmarking'. Published online at epoch.ai. Retrieved from "
            f"'{EPOCH_CREDIT_URL}'. Accessed {date.day} {_MONTHS[date.month - 1]} {date.year}.")


def epoch_snapshot(scores: Mapping[str, Mapping[str, float]], date: dt.date) -> dict:
    """The packaged snapshot's fields, in its order."""
    return {"schema": 1, "source": EPOCH_SOURCE, "date": date.isoformat(), "license": EPOCH_LICENSE,
            "url": EPOCH_CREDIT_URL, "credit": epoch_credit(date), "scores": scores}


def render(scores: Mapping[str, Mapping[str, float]], date: dt.date) -> bytes:
    """The bytes of `templates/engines/benchmarks.json`: plain, sorted JSON, so a refresh shows as a readable diff."""
    return (json.dumps(epoch_snapshot(scores, date), indent=2, ensure_ascii=True) + "\n").encode("utf-8")
