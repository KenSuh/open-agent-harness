# 모델 선택 내보내기 — 실행 폴더마다 단·모델 판정 자료와 결과만 한 줄 JSON 으로 내며, 프롬프트·경로·명령·작업 이름은 싣지 않고 쓰기·프로세스·네트워크·제공자 호출도 하지 않는다.
"""Lines for `oah models export`: each run's decision data and outcome, read from run records only.

`lines(state)` gives one JSON-ready object per run folder that `oah models report` counts, in the report's order (task
folder name, then run id). It reads through the report's own walk, reader, feature presence and value readers, so
both commands decide readability in one place. Like the report it starts no process, calls no provider, opens no
connection and writes no file.

A line holds only fixed fields, each read against its closed set or form; nothing else of the record is copied, so
prompts, briefs, paths, commands, the task name, warnings, errors, notes and worker output never appear. A null is
explained: `unreadable` names a field the export could not read, with the reason (`{"record": <reason>}` when the
whole run folder could not be read), and `not_recorded` names a field the record was written before. A null named in
neither is the record's own null. A value a correction record hashed (`sha256:` and 64 hex digits) is copied as it is
and named in `hashed`; nothing reverses or matches it.

The lines show what was chosen and how the run ended, not whether Jev's choices helped.
"""
from __future__ import annotations

import math
import hashlib
import pathlib

from oah import routing
from oah.engines import effort as effort_rules
from oah.models_report import (HASHED, JUDGE_RULES, TIER_SOURCES, UNUSED, _confidence, _dimension_readers, _features,
                               _flag, _jev_status, _judge_status, _pair as _report_pair, _plain, _Unreadable,
                               read_run, run_folders)
from oah.runs import RUN_ID_RE, TERMINAL, _PROGRESS

FORMAT = 1
STATUSES = (*_PROGRESS, *TERMINAL)
MODES = ("ranked", "pools", "scored", "deterministic")
# call.py and rerank/__init__.py write these non-error reasons.
UNUSED_REASONS = {"not-configured", "not-enabled", "tier-given", "no status", "secret", "pinned",
                  "floor-unresolved", "no-pool-for-tier", "disabled-by-brief", "unknown-provider", "no key",
                  "no-text", "no-candidates", "one-candidate", "too-many-candidates"}
# rerank/__init__.py: key-name, bad-response; rerank/base.py: internal, provider;
# rerank/typesafe.py: bad-response, timeout, network, invalid-header and HTTP status codes.
ERROR_CODES = {"key-name", "bad-response", "internal", "provider", "timeout", "network", "invalid-header",
               "unauthorized", "rate-limited", "overloaded"}
# Snapshot of packaged engines/benchmarks.json, families.json and normalization.json.
# Kept in code so export never reads packaged files, configuration, or a model catalog.
MODEL_NAMES = frozenset("""claude-fable-5 claude-fable-5-1 claude-haiku-5-5 claude-opus-4-6 claude-opus-4-7
claude-opus-4-8 claude-opus-5 claude-opus-5-5 claude-sonnet-4-6 claude-sonnet-5 claude-sonnet-5-5
gpt-5.6-luna gpt-5.6-sol gpt-5.6-terra gpt-6-astra gpt-6-luna gpt-6-sol gpt-6.1-sol gpt-6.1-terra
haiku sonnet opus fable luna terra sol astra""".split())
EFFORTS = frozenset((*effort_rules.ORDINARY, *effort_rules.BEHAVIOUR_CHANGING, effort_rules.HIGHEST))
AXES = {"kind": routing.KIND_SCORES, "intensity": routing.INTENSITY_SCORES,
        "reasoning": routing.REASONING_SCORES, "risk": routing.RISK_SCORES}
FIELDS = ("axes", "stage", "engine", "tier", "tier_source", "rule_tier", "jev_tier", "jev_confidence", "jev_status",
          "tier_floor_raised", "model_judge", "model_judge_confidence", "model_choice", "outcome")
# The exported fields of each feature `oah models report` classifies as absent, complete or partial.
FEATURE_FIELDS = {"jev_difficulty": ("rule_tier", "jev_tier", "jev_confidence", "jev_status"),
                  "model_judge": ("model_judge", "model_choice"),
                  "model_judge_confidence": ("model_judge_confidence",),
                  "tier_floor_raised": ("tier_floor_raised",)}
# When the report finds one of these counts inconsistent with its status, the values it compared are not exported.
CONSISTENCY = {"jev_vs_rule": ("jev_tier", "rule_tier"), "jev_confidence": ("jev_confidence",),
               "model_judge_confidence": ("model_judge_confidence",)}


class _Line:
    """One line being built: every value goes through `read` or `value`, which name each null they make."""

    def __init__(self, run_id: str) -> None:
        self.out = {"format": FORMAT, "run_id": None, **dict.fromkeys(FIELDS), "unreadable": {},
                    "not_recorded": [], "hashed": []}
        self.out["run_id"] = self.value("run_id", run_id, _form(RUN_ID_RE))

    def read(self, path: str, record: dict, key: str, reader):
        """A key of the record itself: one an older oah did not write is not recorded."""
        if key not in record:
            self.out["not_recorded"].append(path)
            return None
        return self.value(path, record[key], reader)

    def value(self, path: str, value, reader):
        """`value` read by `reader`; a hashed value is kept and named, an unreadable one is null with the reason."""
        if value is None:
            return None
        if isinstance(value, str) and HASHED.fullmatch(value):
            self.out["hashed"].append(path)
            return value
        try:
            return reader(value)
        except _Unreadable as exc:
            if isinstance(value, str):
                self.out["unreadable"][path] = exc.reason
                self.out["hashed"].append(path)
                # JSON can contain unpaired surrogate escapes; preserve them deterministically rather than aborting.
                return "sha256:" + hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
            self.out["unreadable"][path] = exc.reason
            return None

    def unreadable(self, name: str, reason: str) -> None:
        """A top-level field the report cannot read in this record: null, named, and never shown as hashed."""
        self.out[name] = None
        self.out["unreadable"].setdefault(name, reason)
        if name in self.out["hashed"]:
            self.out["hashed"].remove(name)

    def done(self) -> dict:
        self.out["not_recorded"].sort()
        self.out["hashed"].sort()
        return self.out


def _closed(words):
    return lambda value: _plain(value, words)


def _export_status(value, report_reader):
    status = report_reader(value)
    if status.startswith("unused("):
        reason = status[7:-1]
        if reason in UNUSED_REASONS:
            return status
        if reason.startswith("error:") and UNUSED.fullmatch(status):
            return status if reason[6:] in ERROR_CODES else "unused(error)"
        raise _Unreadable("unknown-value")
    return status


def _form(pattern):
    def read(value):
        if not isinstance(value, str):
            raise _Unreadable("wrong-type")
        if not pattern.fullmatch(value):
            raise _Unreadable("unknown-value")
        return value
    return read


_model = _closed(MODEL_NAMES)
_effort = _closed(EFFORTS)


def _exit_code(value) -> int:
    if type(value) is not int:
        raise _Unreadable("wrong-type")
    return value


def _duration(value) -> float | int:
    # isfinite is asked of floats only: a huge int would overflow converting to one.
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)) or value < 0:
        raise _Unreadable("wrong-type")
    return value


def _fields(line: _Line, path: str, fields: dict):
    """A reader for an object of fixed keys; any other key of the record's object is dropped, a missing one named."""
    def read(item):
        if not isinstance(item, dict):
            raise _Unreadable("wrong-type")
        result = {}
        for key, reader in fields.items():
            if key in item:
                result[key] = line.value(f"{path}.{key}", item[key], reader)
            else:
                line.out["unreadable"][f"{path}.{key}"] = "missing"
                result[key] = None
        return result
    return read


PAIR = {"model": _model, "effort": _effort}
FLOOR = {"tier": _closed(routing.TIERS), "model": _model, "effort": _effort}


def _pair(line: _Line, path: str, value) -> dict | None:
    return line.value(path, value, _fields(line, path, PAIR))


def _candidates(line: _Line, path: str, value) -> list | None:
    def read(items):
        if not isinstance(items, list):
            raise _Unreadable("wrong-type")
        result = []
        for n, item in enumerate(items):
            entry = f"{path}[{n}]"
            if isinstance(item, list):  # oah writes each candidate as [model, effort]
                if len(item) != 2:
                    line.out["unreadable"][entry] = "wrong-type"
                    result.append(None)
                    continue
                item = {"model": item[0], "effort": item[1]}
            result.append(_pair(line, entry, item))
        return result
    return line.value(path, value, read)


def _excluded(line: _Line, path: str, value) -> list | None:
    def read(items):
        if not isinstance(items, dict):
            raise _Unreadable("wrong-type")
        # The model name is the key here; the entries keep the record's order.
        return [{"model": line.value(f"{path}[{n}].model", model, _model),
                 "rule": line.value(f"{path}[{n}].rule", rule, _closed(JUDGE_RULES))}
                for n, (model, rule) in enumerate(items.items())]
    return line.value(path, value, read)


CHOICE = {"mode": lambda line, path, value: line.value(path, value, _closed(MODES)),
          "floor": lambda line, path, value: line.value(path, value, _fields(line, path, FLOOR)),
          "candidates": _candidates, "excluded": _excluded,
          "deterministic": _pair, "proposal": _pair, "requested": _pair}


def _model_choice(line: _Line):
    def read(choice):
        if not isinstance(choice, dict):
            raise _Unreadable("wrong-type")
        result = {}
        for key, reader in CHOICE.items():
            path = f"model_choice.{key}"
            if key in choice:
                result[key] = reader(line, path, choice[key])
            else:
                line.out["unreadable"][path] = "missing"
                result[key] = None
        return result
    return read


def _line(run_id: str, record: dict, stage: str, engine: str) -> dict:
    line = _Line(run_id)
    out = line.out
    out["stage"] = line.value("stage", stage, _closed(routing.STAGES))
    out["engine"] = line.value("engine", engine, _closed(routing.ENGINES))
    out["axes"] = line.read("axes", record, "axes",
                            _fields(line, "axes", {name: _closed(words) for name, words in AXES.items()}))
    out["outcome"] = {"status": line.read("outcome.status", record, "status", _closed(STATUSES)),
                      "exit_code": line.read("outcome.exit_code", record, "exit_code", _exit_code),
                      "timed_out": line.read("outcome.timed_out", record, "timed_out", _flag),
                      "duration_s": line.read("outcome.duration_s", record, "duration_s", _duration)}
    out["tier"] = line.read("tier", record, "tier", _closed(routing.TIERS))
    out["tier_source"] = line.read("tier_source", record, "tier_source", _closed(TIER_SOURCES))
    readers = {"rule_tier": _closed(routing.TIERS), "jev_tier": _closed(routing.TIERS), "jev_confidence": _confidence,
               "jev_status": lambda v: _export_status(v, _jev_status), "tier_floor_raised": _flag,
               "model_judge": lambda v: _export_status(v, _judge_status),
               "model_judge_confidence": _confidence, "model_choice": _model_choice(line)}
    features = _features(record)  # the report's decision: each feature absent, complete or partial
    for feature, names in FEATURE_FIELDS.items():
        for name in names:
            if features[feature] == "absent":
                out["not_recorded"].append(name)
            elif features[feature] == "partial":
                line.unreadable(name, "inconsistent")
            else:
                out[name] = line.value(name, record[name], readers[name])
    dimensions = _dimension_readers(record, features)
    if features["model_judge"] == "complete":
        try:
            dimensions["model_change"]()
        except _Unreadable as exc:
            # The comparison can fail even when individual values (including hashes) are readable.
            out["unreadable"]["model_choice"] = exc.reason
            if exc.reason == "wrong-type" and isinstance(record["model_choice"], dict):
                for name in ("deterministic", "requested"):
                    try:
                        _report_pair(record["model_choice"].get(name))
                    except _Unreadable as pair_error:
                        path = f"model_choice.{name}"
                        out["model_choice"][name] = None
                        out["unreadable"][path] = pair_error.reason
                        out["hashed"][:] = [item for item in out["hashed"] if not item.startswith(path + ".")]
    for dimension, names in CONSISTENCY.items():
        try:
            dimensions[dimension]()
        except _Unreadable as exc:
            # Any other reason comes from a value this line has already read and named.
            if exc.reason == "inconsistent":
                for name in names:
                    line.unreadable(name, "inconsistent")
    return line.done()


def lines(state: pathlib.Path) -> list[dict]:
    """One line per run folder of one state folder, read or unreadable with its reason."""
    result = []
    for entry in run_folders(pathlib.Path(state), {}):
        try:
            record, stage, engine = read_run(entry)
        except _Unreadable as exc:
            line = _Line(entry.name)
            line.out["unreadable"]["record"] = exc.reason
            result.append(line.done())
            continue
        result.append(_line(entry.name, record, stage, engine))
    return result
