# 벤치마크 자료 생성 도구 — Epoch AI CSV 에서 허용한 과제의 최대 강도 점수만 골라 패키지에 싣는 benchmarks.json 을 만든다.
"""Rebuild `src/oah/templates/engines/benchmarks.json` from a downloaded Epoch AI CSV.

    python tools/build_benchmarks.py <epoch.csv> --date YYYY-MM-DD [--output PATH]

Only the tasks in `TASKS` are kept: data from external projects keeps its own licence. Run this for each
release from the file at https://epoch.ai/data/benchmarks.csv. The output is plain sorted JSON, so a
refresh shows as a readable diff. Never point this at any other source's data.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "src" / "oah" / "templates" / "engines" / "benchmarks.json"
URL = "https://epoch.ai/benchmarks/use-this-data"
MODEL = r"(gpt-[0-9.]+-[a-z]+|claude-[a-z]+-[0-9-]+)_max"
TASKS = ("FrontierMath-Tiers-1-3-v2-Private", "FrontierMath-Tier-4-v2-Private", "Mystery Game Puzzles",
         "Furniture Assembly", "Chess Puzzles", "EBR-bench", "OTIS Mock AIME 2024-2025")
COLUMNS = ("model", "started_at", "Status", "mean_score", "original_task_name")
_MODEL = re.compile(MODEL)


class BuildError(Exception):
    pass


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


def read_scores(path: pathlib.Path) -> dict[str, dict[str, float]]:
    """{model id: {task: mean}}, the latest successful run of each pair. `newline=""` keeps multi-line fields whole."""
    latest: dict[tuple[str, str], tuple[dt.datetime, float]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = [name for name in COLUMNS if name not in (reader.fieldnames or ())]
            if missing:
                raise BuildError(f"{path} has no column {', '.join(missing)}; check that this is Epoch's benchmarks.csv")
            for row in reader:
                match = _MODEL.fullmatch(row["model"] or "")
                task = row["original_task_name"]
                mean = parse_mean(row["mean_score"] or "")
                if match is None or task not in TASKS or row["Status"] != "Success" or mean is None:
                    continue
                if not 0 <= mean <= 1:
                    raise BuildError(f"{match.group(1)} scores {mean} on {task}, outside 0 to 1, so the scale may have "
                                     "changed; check Epoch's data before shipping it")
                try:
                    started = _moment(row["started_at"])
                except (TypeError, ValueError):
                    raise BuildError(f"{match.group(1)} on {task} has the unreadable time {row['started_at']!r}; "
                                     "check Epoch's data before shipping it") from None
                key = (match.group(1), task)
                if key not in latest or started >= latest[key][0]:
                    latest[key] = (started, mean)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise BuildError(f"{path} cannot be read ({exc}); download Epoch's benchmarks.csv again") from None
    if not latest:
        raise BuildError(f"{path} has no rows for the allowed tasks; check that this is Epoch's benchmarks.csv")
    scores: dict[str, dict[str, float]] = {}
    for (model, task), (_started, mean) in sorted(latest.items()):
        scores.setdefault(model, {})[task] = mean
    return scores


def render(scores: dict[str, dict[str, float]], date: dt.date) -> bytes:
    accessed = f"{date.day} {date:%b %Y}"
    data = {"schema": 1, "source": "Epoch AI", "date": date.isoformat(), "license": "CC BY 4.0", "url": URL,
            "credit": f"Epoch AI, 'Capabilities & benchmarking'. Published online at epoch.ai. Retrieved from '{URL}'. "
                      f"Accessed {accessed}.",
            "scores": scores}
    return (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild benchmarks.json from an Epoch AI CSV.")
    parser.add_argument("csv", type=pathlib.Path, help="the downloaded Epoch AI benchmarks.csv")
    parser.add_argument("--date", required=True, help="the day it was downloaded, YYYY-MM-DD")
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT, help="where to write (default: the packaged file)")
    args = parser.parse_args(argv)
    try:
        try:
            date = dt.date.fromisoformat(args.date)
        except ValueError:
            raise BuildError(f"--date {args.date!r} is not a date; write it as YYYY-MM-DD") from None
        content = render(read_scores(args.csv), date)
    except BuildError as exc:
        print(f"build_benchmarks: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
