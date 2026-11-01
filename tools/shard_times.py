#!/usr/bin/env python3
# CI 시간표 생성기 — 저장한 GitHub 작업 로그에서 테스트별 시간을 재어 분할 시간표를 다시 쓴다.
"""Regenerate the shard weights from saved job logs: `python tools/shard_times.py OUT.json LOG [LOG ...]`.

Each log is one job of `gh run view --log`: `job<TAB>step<TAB>TIMESTAMP text` lines from a run of
`tools/test_shard.py ... -v`. A test takes a value only when top-level results confirm both its start and its end:
the previous test's own final result (or the `shard N/M: K tests` line for the first test), and its own final result.
Subtest results, the next test's name and the run summary do not confirm an end. A test without a confirmed end
and its successor get no value from that log; timing re-anchors at the successor's top-level result.

A test's value is the median across the logs that have it, as integer milliseconds, at least 1. `default_ms` is the
mean of those values rounded down. `source` names the runs from the log file names, without the `-w<N>` job suffix.
The output holds test ids and those numbers only, so nothing else from a log reaches the repository.

The script then prints what it skipped and the minutes per shard that the file it wrote predicts for COUNT=5, which
discovers the tests in tests/ without running them. Standard library only.
"""
from __future__ import annotations

import collections
import datetime
import json
import pathlib
import re
import sys

PREDICTED_COUNT = 5
_STAMPED = re.compile(r"\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)(?: (.*))?")
_SHARD = re.compile(r"shard \d+/\d+: (\d+) tests\Z")
_RAN = re.compile(r"Ran \d+ tests? in ")
_NAME = re.compile(r"(\s*)(\S+) \(([^\s()]+)\)")
_RESULT = re.compile(r"(?:\A|\.\.\. )(?:ok|FAIL|ERROR|expected failure|unexpected success|skipped ['\"].*)\Z")
_JOB_SUFFIX = re.compile(r"-w\d+\Z")
_MILLISECOND = datetime.timedelta(milliseconds=1)


def _stamped_lines(text: str):
    for line in text.split("\n"):
        fields = line.rstrip("\r").split("\t", 2)
        stamped = _STAMPED.fullmatch(fields[2].rstrip()) if len(fields) == 3 else None
        if stamped:
            yield datetime.datetime.fromisoformat(stamped[1]), stamped[2] or ""


def parse_log(text: str) -> tuple[dict[str, int], collections.Counter]:
    """Return one log's times in milliseconds by test id, and counts of what it could not measure."""
    lines = _stamped_lines(text)
    planned = None
    for previous, body in lines:
        planned = _SHARD.match(body)
        if planned:
            break
    if not planned:
        raise ValueError("no `shard N/M: K tests` line")
    counts: collections.Counter = collections.Counter()
    ends: dict[str, datetime.datetime | None] = {}
    current = None
    for stamp, body in lines:
        if _RAN.match(body):
            break
        if body.startswith("##[error]"):
            break
        name = _NAME.match(body)
        if name and not name[3].endswith(f".{name[2]}"):
            continue  # `setUpClass (tests.module.Class) ... ERROR` names no test, so it ends none.
        if name and name[1]:
            counts["subtest lines"] += 1
            continue
        if name:
            counts["repeated"] += name[3] != current and name[3] in ends
            current = name[3]
            ends.setdefault(current, None)
        if current is not None and _RESULT.search(body):
            ends[current] = stamp
    times = {}
    for test, end in ends.items():
        if end is None:
            counts["unfinished"] += 1
        elif previous is None:
            counts["unconfirmed start"] += 1
        else:
            times[test] = max(1, (end - previous) // _MILLISECOND)
        previous = end
    counts["never reached"] = max(0, int(planned[1]) - len(ends))
    return times, counts


def median_ms(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    low = ordered[middle] if len(ordered) % 2 else ordered[middle - 1]
    return max(1, (low + ordered[middle]) // 2)


def run_labels(logs: list[pathlib.Path]) -> list[str]:
    return sorted({_JOB_SUFFIX.sub("", log.stem) for log in logs})


def build(logs: list[pathlib.Path]) -> tuple[dict[str, object], collections.Counter]:
    """Measure every log and return the data file's content with the summed counts."""
    measured: dict[str, list[int]] = collections.defaultdict(list)
    counts: collections.Counter = collections.Counter()
    for log in logs:
        try:
            times, found = parse_log(log.read_bytes().decode("utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit(f"shard_times: {log}: {error}") from None
        counts += found
        counts["measured"] += len(times)
        for test, value in times.items():
            measured[test].append(value)
    if not measured:
        raise SystemExit("shard_times: the logs hold no finished test")
    times_ms = {test: median_ms(values) for test, values in measured.items()}
    return {"default_ms": sum(times_ms.values()) // len(times_ms),
            "source": f"Median of {len(logs)} saved GitHub job logs from {', '.join(run_labels(logs))}",
            "times_ms": times_ms}, counts


def predicted_minutes(path: pathlib.Path) -> list[float]:
    """Minutes per shard for PREDICTED_COUNT, from the data file at `path`, by discovery alone."""
    import test_shard  # Beside this script, which puts its own folder on sys.path.

    default_ms, times_ms = test_shard.load_times(path)
    groups = test_shard.shards(PREDICTED_COUNT, weights=(default_ms, times_ms))
    return [sum(test_shard.weight(unit, default_ms, times_ms) for unit in group) / 60000 for group in groups]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python tools/shard_times.py OUT.json LOG [LOG ...]", file=sys.stderr)
        return 2
    out, logs = pathlib.Path(argv[0]), [pathlib.Path(log) for log in argv[1:]]
    data, counts = build(logs)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out}: {len(data['times_ms'])} ids from {len(logs)} logs, default_ms {data['default_ms']}")
    print(f"log entries measured: {counts['measured']}")
    print(f"log entries skipped: {counts['unfinished']} unfinished, "
          f"{counts['unconfirmed start']} dropped (unconfirmed start), "
          f"{counts['repeated']} repeated in one log, "
          f"{counts['never reached']} never reached")
    print(f"result lines folded into their test: {counts['subtest lines']} subtest lines")
    minutes = predicted_minutes(out)
    print(f"predicted minutes for COUNT={PREDICTED_COUNT}: {' '.join(f'{m:.1f}' for m in minutes)}; "
          f"total {sum(minutes):.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
