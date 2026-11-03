# 모델 선택 보고 — 한 저장소의 실행 기록만 읽어 단·모델이 어떻게 골라졌는지 세며, 쓰기·프로세스·네트워크·제공자 호출은 하지 않는다.
"""Counts for `oah models report`: how each run's tier and model were chosen, read from run records only.

`collect(state)` reads `<state>/runs/<task>/<run-id>/result.json` and nothing else. It starts no process, calls
no provider, opens no connection and writes no file. Every run folder counts once, as read or as unreadable with
a reason, and a value the report cannot read is counted as unreadable, never as zero or as "not used".

A key a record does not have counts as `not_recorded`: that oah did not write it yet (v0.1.0 had no model judge,
v0.2.0 no Jev difficulty, and only this version records the judge's confidence and the floor flag). A key that
is null counts as `none`: the run stopped before that choice, or the choice had no value. A value hashed in a
correction record (`sha256:` and 64 hex digits) is unreadable only where a count needs it; model names are
compared for same or different, which a hash keeps.

These counts show how choices were made, not whether Jev's choices helped: no outcome or quality is compared.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import stat

from oah import routing
from oah.lease import TASK_RE
from oah.runs import RUN_ID_RE, RunError, _contained_path

FORMAT = 1
NOTE = ("These counts show how tiers and models were chosen, not whether Jev's choices helped: "
        "no outcome or quality is compared.")
BANDS = tuple(f"{n / 10:.1f}" for n in range(10))  # [0.0, 0.1) ... [0.9, 1.0]
DIMENSIONS = ("tier_source", "jev_status", "jev_vs_rule", "jev_confidence", "model_judge", "model_change",
              "model_judge_confidence", "tier_floor_raised")
TIER_SOURCES = ("flag", "packet", "rules", "jev")
JUDGE_RULES = ("outside-candidates", "not-allowed", "unsupported-effort", "unranked-model", "not-in-tier-pool",
               "below-floor", "above-cap", "outside-score-bound")
HASHED = re.compile(r"sha256:[0-9a-f]{64}")
# The unused reasons oah writes: a fixed word, or an error with its code (`error:timeout`) or its type name
# (`error: RuntimeError`). Anything else is not echoed.
UNUSED = re.compile(r"unused\((?:[a-z][a-z -]{0,40}|error:[a-z0-9-]{1,40}|error: [A-Za-z_][A-Za-z0-9_]{0,60})\)")
BELOW_THRESHOLD = "below-threshold"
_ABSENT = object()


class _Unreadable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def band(value: float) -> int:
    """The tenth a confidence from 0 to 1 falls in; 1.0 is in the last."""
    return next((k - 1 for k in range(1, 10) if value < k / 10), 9)


def _plain(value, words) -> str:
    """A string from a closed set, or why it cannot be read."""
    if isinstance(value, str) and HASHED.fullmatch(value):
        raise _Unreadable("hashed")
    if not isinstance(value, str):
        raise _Unreadable("wrong-type")
    if value not in words:
        raise _Unreadable("unknown-value")
    return value


def _confidence(value) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise _Unreadable("hashed" if isinstance(value, str) and HASHED.fullmatch(value) else "wrong-type")
    return float(value)


def _judge_status(value) -> str:
    if isinstance(value, str) and (value == "used" or UNUSED.fullmatch(value)):
        return value
    if isinstance(value, str) and value.startswith("rejected(") and value[len("rejected("):-1] in JUDGE_RULES \
            and value.endswith(")"):
        return value
    return _plain(value, ())


def _jev_status(value) -> str:
    if isinstance(value, str) and (value == "used" or UNUSED.fullmatch(value)):
        return value
    if isinstance(value, str) and value.startswith(BELOW_THRESHOLD + " "):
        return BELOW_THRESHOLD
    return _plain(value, ())


def _pair(value) -> tuple[str, str]:
    if not isinstance(value, dict) or not isinstance(value.get("model"), str) or not isinstance(value.get("effort"), str):
        raise _Unreadable("wrong-type")
    return value["model"], value["effort"]


def _same(left: str, right: str) -> bool:
    """Plain names compare as text, hashed names as hashes; one of each cannot be compared."""
    if bool(HASHED.fullmatch(left)) != bool(HASHED.fullmatch(right)):
        raise _Unreadable("hashed")
    return left == right


def _model_change(choice) -> str:
    if isinstance(choice, str) and HASHED.fullmatch(choice):
        raise _Unreadable("hashed")
    if not isinstance(choice, dict):
        raise _Unreadable("wrong-type")
    ran, rule = _pair(choice.get("requested")), _pair(choice.get("deterministic"))
    model, effort = not _same(ran[0], rule[0]), not _same(ran[1], rule[1])
    return "both" if model and effort else "model" if model else "effort" if effort else "same"


def _tally() -> dict:
    return {"values": {}, "none": 0, "not_recorded": 0, "unreadable": {}}


def _confidence_tally(*statuses: str) -> dict:
    return {**{status: [0] * 10 for status in statuses}, "no_confidence": 0, "not_answered": 0, "not_recorded": 0,
            "unreadable": {}}


def _counts() -> dict:
    counts = {"runs": 0}
    for name in DIMENSIONS:
        counts[name] = _tally()
    counts["jev_confidence"] = _confidence_tally("used", BELOW_THRESHOLD)
    counts["model_judge_confidence"] = _confidence_tally("used", "rejected")
    return counts


def _add(counts: dict, name: str, value: str) -> None:
    values = counts[name]["values"]
    values[value] = values.get(value, 0) + 1


def _count(counts: dict, name: str, read) -> bool:
    """Count one dimension of one record; False when it could not be read."""
    tally = counts[name]
    try:
        bucket = read()
    except _Unreadable as exc:
        tally["unreadable"][exc.reason] = tally["unreadable"].get(exc.reason, 0) + 1
        return False
    if bucket is _ABSENT:
        tally["not_recorded"] += 1
    elif bucket is None:
        tally["none"] += 1
    elif isinstance(bucket, tuple):  # (confidence status, tenth)
        tally[bucket[0]][bucket[1]] += 1
    elif bucket in ("no_confidence", "not_answered"):
        tally[bucket] += 1
    else:
        _add(counts, name, bucket)
    return True


def _features(record: dict) -> dict:
    """Classify each group of fields together, including the confidence dependency."""
    groups = {"jev_difficulty": ("rule_tier", "jev_tier", "jev_confidence", "jev_status", "jev_truncated"),
              "model_judge": ("model_judge", "model_choice"),
              "model_judge_confidence": ("model_judge_confidence",),
              "tier_floor_raised": ("tier_floor_raised",)}
    result = {}
    for feature, keys in groups.items():
        present = sum(key in record for key in keys)
        result[feature] = "absent" if not present else "complete" if present == len(keys) else "partial"
    if result["model_judge_confidence"] == "complete" and result["model_judge"] != "complete":
        result["model_judge"] = result["model_judge_confidence"] = "partial"
    return result


def _record_counts(counts: dict, record: dict, features: dict) -> bool:
    """Add one readable record to `counts`; False when any count could not read its value."""
    get = record.get
    counts["runs"] += 1

    def value(name: str, read):
        def reader():
            if name == "tier_source" and name not in record:
                return _ABSENT
            return None if record[name] is None else read(record[name])
        return reader

    def jev_vs_rule():
        if get("jev_status") is None:
            if get("jev_tier") is not None:
                raise _Unreadable("inconsistent")
            return None
        status = _jev_status(get("jev_status"))
        tier = get("jev_tier")
        answered = status in ("used", BELOW_THRESHOLD)
        if tier is None:
            if answered:
                raise _Unreadable("inconsistent")
            return "no-jev-tier"
        if not answered:
            raise _Unreadable("inconsistent")
        jev, rule = _plain(tier, routing.TIERS), get("rule_tier")
        if rule is None:
            raise _Unreadable("inconsistent")
        rule = _plain(rule, routing.TIERS)
        order = routing.TIERS.index(jev) - routing.TIERS.index(rule)
        return f"{status}:{'above' if order > 0 else 'below' if order < 0 else 'equal'}"

    def confidence(status_key: str, value_key: str, answered: dict):
        def reader():
            status = get(status_key)
            if status is None:
                kind = None
            else:
                read = _jev_status if status_key == "jev_status" else _judge_status
                status = read(status)
                kind = next((bucket for prefix, bucket in answered.items() if status.startswith(prefix)), None)
            number = _confidence(get(value_key))
            if kind is None:
                if number is not None:
                    raise _Unreadable("inconsistent")
                return "not_answered"
            if status_key == "jev_status":
                if status == "used" and number is None:
                    raise _Unreadable("inconsistent")
                if status == BELOW_THRESHOLD:
                    shown = "none" if number is None else format(number, ".3g")
                    if get(status_key) != f"{BELOW_THRESHOLD} {shown}":
                        raise _Unreadable("inconsistent")
            return "no_confidence" if number is None else (kind, band(number))
        return reader

    def floor(value):
        if type(value) is not bool:
            raise _Unreadable("hashed" if isinstance(value, str) and HASHED.fullmatch(value) else "wrong-type")
        return "true" if value else "false"

    readers = {
        "tier_source": value("tier_source", lambda v: _plain(v, TIER_SOURCES)),
        "jev_status": value("jev_status", _jev_status),
        "jev_vs_rule": jev_vs_rule,
        "jev_confidence": confidence("jev_status", "jev_confidence",
                                     {"used": "used", BELOW_THRESHOLD: BELOW_THRESHOLD}),
        "model_judge": value("model_judge", _judge_status),
        "model_change": value("model_choice", _model_change),
        "model_judge_confidence": confidence("model_judge", "model_judge_confidence",
                                             {"used": "used", "rejected(": "rejected"}),
        "tier_floor_raised": value("tier_floor_raised", floor),
    }
    feature_for = {"jev_status": "jev_difficulty", "jev_vs_rule": "jev_difficulty",
                   "jev_confidence": "jev_difficulty", "model_judge": "model_judge",
                   "model_change": "model_judge", "model_judge_confidence": "model_judge_confidence",
                   "tier_floor_raised": "tier_floor_raised"}

    def feature_reader(name):
        def reader():
            state = features[feature_for[name]]
            if state == "absent":
                return _ABSENT
            if state == "partial":
                raise _Unreadable("inconsistent")
            return readers[name]()
        return reader

    return all([_count(counts, name, feature_reader(name) if name in feature_for else readers[name])
                for name in DIMENSIONS])


def _identity(record: dict, name: str, words) -> str:
    if name not in record or record[name] is None:
        raise _Unreadable(f"{name}:missing")
    try:
        return _plain(record[name], words)
    except _Unreadable as exc:
        raise _Unreadable(f"{name}:{exc.reason}") from None


def _read(path: pathlib.Path) -> dict:
    """One run folder's record, or why it cannot be read. Reading never writes, waits or retries."""
    try:
        info = path.lstat()
    except OSError:
        raise _Unreadable("read-error") from None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise _Unreadable("linked")
    if not stat.S_ISDIR(info.st_mode):
        raise _Unreadable("not-a-folder")
    try:
        result = _contained_path(path.parents[2], "runs", path.parent.name, path.name, "result.json")
        raw = result.read_bytes()
    except RunError:
        raise _Unreadable("linked") from None
    except FileNotFoundError:
        raise _Unreadable("missing") from None
    except OSError:
        raise _Unreadable("read-error") from None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Unreadable("invalid-json") from None
    if not isinstance(record, dict):
        raise _Unreadable("not-object")
    return record


def _bump(table: dict, key: str) -> None:
    table[key] = table.get(key, 0) + 1


def collect(state: pathlib.Path) -> dict:
    """The report for one state folder, as one JSON-ready object."""
    state = pathlib.Path(state)
    records = {"run_folders": 0, "read": 0, "partial": 0, "unreadable": {}, "skipped": {}}
    written_before = {"model_judge": 0, "jev_difficulty": 0, "model_judge_confidence": 0, "tier_floor_raised": 0}
    groups: dict[tuple[str, str], dict] = {}
    totals = _counts()
    runs = _contained_path(state, "runs")  # a linked runs folder refuses: OAH_RUN_PATH_UNSAFE
    tasks = sorted(runs.iterdir()) if runs.is_dir() else []
    for task in tasks:
        if not task.is_dir() or not TASK_RE.fullmatch(task.name):
            _bump(records["skipped"], "unrecognized")
            continue
        try:
            folder = _contained_path(state, "runs", task.name)
        except RunError:
            _bump(records["skipped"], "linked-task-folder")
            continue
        for entry in sorted(folder.iterdir()):
            if entry.name == "intent-binding.json":
                continue  # dispatch keeps the task's Intent binding beside its run folders
            if not RUN_ID_RE.fullmatch(entry.name):
                created = entry.name.startswith(".") and entry.name.endswith(".tmp")
                _bump(records["skipped"], "being-created" if created else "unrecognized")
                continue
            records["run_folders"] += 1
            try:
                record = _read(entry)
                stage = _identity(record, "stage", routing.STAGES)
                engine = _identity(record, "engine", routing.ENGINES)
            except _Unreadable as exc:
                _bump(records["unreadable"], exc.reason)
                continue
            records["read"] += 1
            features = _features(record)
            for name, presence in features.items():
                if presence == "absent":
                    written_before[name] += 1
            group = groups.setdefault((stage, engine), _counts())
            whole = _record_counts(group, record, features)
            _record_counts(totals, record, features)
            if not whole:
                records["partial"] += 1
    order = {stage: n for n, stage in enumerate(routing.STAGES)}
    return {"format": FORMAT, "state": str(state), "note": NOTE, "bands": list(BANDS), "records": records,
            "written_before": written_before,
            "groups": [{"stage": stage, "engine": engine, "counts": groups[(stage, engine)]}
                       for stage, engine in sorted(groups, key=lambda key: (order[key[0]], key[1]))],
            "totals": totals}


# Text ----------------------------------------------------------------------------------------------

LABELS = {"tier_source": "tier source", "jev_status": "jev difficulty", "jev_vs_rule": "jev tier vs rule tier",
          "jev_confidence": "jev confidence", "model_judge": "model judge",
          "model_change": "pair that ran vs deterministic pair", "model_judge_confidence": "judge confidence",
          "tier_floor_raised": "floor raised the tier"}


def _parts(table: dict) -> str:
    return ", ".join(f"{key} {count}" for key, count in table.items() if count)


def _tally_text(tally: dict) -> str:
    parts = []
    for key, count in tally.items():
        if key == "values":
            parts += [f"{value} {n}" for value, n in count.items() if n]
        elif key == "unreadable":
            if count:
                parts.append(f"unreadable {sum(count.values())} ({_parts(count)})")
        elif isinstance(count, list):
            parts += [f"{key} {BANDS[n]} {hits}" for n, hits in enumerate(count) if hits]
        elif count:
            parts.append(f"{key.replace('_', ' ')} {count}")
    return " · ".join(parts) or "none"


def _block(title: str, counts: dict) -> list[str]:
    runs = counts["runs"]
    lines = [f"{title}: {runs} run{'' if runs == 1 else 's'}"]
    lines += [f"  {LABELS[name]}: {_tally_text(counts[name])}" for name in DIMENSIONS]
    return lines


def text(report: dict) -> list[str]:
    """The report as lines: the record counts, what the counts cannot show, each group and the totals."""
    records = report["records"]
    head = f"oah models report · {records['run_folders']} run folders: {records['read']} read"
    if records["partial"]:
        head += f" ({records['partial']} with a value that could not be read)"
    unreadable = records["unreadable"]
    head += f", {sum(unreadable.values())} unreadable" + (f" ({_parts(unreadable)})" if unreadable else "")
    if records["skipped"]:
        head += f" · skipped {sum(records['skipped'].values())} other entries ({_parts(records['skipped'])})"
    lines = [head, NOTE]
    if not records["run_folders"]:
        lines.append(f"no run records yet in {report['state']}")
        return lines
    before, read = report["written_before"], records["read"]
    lines.append(f"written before this oah recorded them: judge confidence {before['model_judge_confidence']} of "
                 f"{read}, floor flag {before['tier_floor_raised']} of {read} · before Jev difficulty "
                 f"{before['jev_difficulty']}, before the model judge {before['model_judge']}")
    for group in report["groups"]:
        lines += _block(f"{group['stage']} · {group['engine']}", group["counts"])
    lines += _block("total", report["totals"])
    return lines
