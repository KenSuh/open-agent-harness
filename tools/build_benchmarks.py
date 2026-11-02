# 벤치마크 자료 생성 도구 — Epoch AI CSV 에서 허용한 과제의 최대 강도 점수만 골라 패키지에 싣는 benchmarks.json 을 만든다.
"""Rebuild `src/oah/templates/engines/benchmarks.json` from a downloaded Epoch AI CSV.

    python tools/build_benchmarks.py <epoch.csv> --date YYYY-MM-DD [--output PATH]

The parsing lives in `oah.benchmarks`, which also reads the copy a catalog rebuild fetches, so both give the same
scores for the same CSV. Only the tasks in `TASKS` are kept: data from external projects keeps its own licence. Run
this for each release from the file at https://epoch.ai/data/benchmarks.csv. The output is plain sorted JSON, so a
refresh shows as a readable diff. Never point this at any other source's data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))  # this checkout's oah, not an installed copy

from oah import benchmarks  # noqa: E402

OUTPUT = ROOT / "src" / "oah" / "templates" / "engines" / "benchmarks.json"
URL = benchmarks.EPOCH_CREDIT_URL
MODEL = benchmarks.MODEL
TASKS = benchmarks.TASKS
COLUMNS = benchmarks.COLUMNS
parse_mean = benchmarks.parse_mean
render = benchmarks.render


class BuildError(Exception):
    pass


def read_scores(path: pathlib.Path) -> dict[str, dict[str, float]]:
    """{model id: {task: mean}}, the latest successful run of each pair (`oah.benchmarks.epoch_scores`)."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"{path} cannot be read ({exc}); download Epoch's benchmarks.csv again") from None
    try:
        return benchmarks.epoch_scores(data)
    except benchmarks.DataError as exc:
        raise BuildError(f"{path} {exc}; check that this is Epoch's benchmarks.csv before shipping it") from None


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
