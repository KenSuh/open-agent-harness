# 도구 업데이트 기록 — [updates] 스위치를 읽고, ~/.oah/updates 의 state.json·runs.jsonl 을 검증하며 읽고 원자적으로 쓴다.
"""The switch and the machine-wide records of the tool-update pass.

The switch is per repository: `[updates] after_finish` in `.oah/config.toml`, off when the key is absent.
The records and the lock are per machine, because installs are: `~/.oah/updates/`. `home()` is the only
place this package asks for the home folder, so tests point it at a temporary folder.

Every read validates the whole shape. A missing, unreadable or wrongly shaped `state.json` raises
`StateUnavailable` (OAH_UPDATE_STATE_UNAVAILABLE) once the pass has ever recorded anything; the pass then
checks every component again and writes a fresh record. Bad lines in `runs.jsonl` are skipped one by one.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import pathlib
import re
from typing import Any, Callable, Mapping, Sequence

from oah.atomic import write_json_atomic, write_text_atomic
from oah.config import ConfigError, load_config

SECTION = "updates"
DEFAULTS = {"after_finish": False, "interval_hours": 24.0, "timeout_seconds": 600.0}
MAX_TIMEOUT_SECONDS = 86400.0
FORMAT = 1
STATUSES = ("updated", "current", "unverified", "needs-confirmation", "needs-hook-approval", "unsupported", "failed")
SETTLED = ("updated", "current")
COMPLETE = ("updated", "current", "unsupported")  # anything else makes a pass "needs-attention"
TRIGGERS = ("after-finish", "manual")
OUTCOMES = ("complete", "needs-attention")
ENGINES = ("claude", "codex")
RUNS_KEPT = 200
TEXT_LIMIT = 600
COMPONENT_FIELDS = ("status", "before", "after", "settled_version", "checked_at", "next_step")
COMPONENT_RE = re.compile(r"(?:claude|codex|oah-skills|claude-plugins|claude-marketplaces|codex-marketplaces)|"
                          r"(?:claude-plugin|codex-plugin|codex-marketplace):[A-Za-z0-9][A-Za-z0-9._@-]{0,199}")
STATE_UNAVAILABLE = "OAH_UPDATE_STATE_UNAVAILABLE"


def home() -> pathlib.Path:
    """The user's home folder; nothing else in this package consults it."""
    return pathlib.Path.home()


def updates_dir() -> pathlib.Path:
    return home() / ".oah" / "updates"


def state_path() -> pathlib.Path:
    return updates_dir() / "state.json"


def runs_path() -> pathlib.Path:
    return updates_dir() / "runs.jsonl"


def lock_path() -> pathlib.Path:
    return updates_dir() / "pass.lock"


def log_path() -> pathlib.Path:
    return updates_dir() / "pass.log"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_stamp(text: object) -> dt.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


# The switch ----------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Settings:
    after_finish: bool
    interval_hours: float
    timeout_seconds: float
    explicit: bool  # the file names after_finish; False is "off by absence"


def read_settings(root: pathlib.Path) -> Settings:
    """The repository's [updates] settings; raises ConfigError when they cannot be read."""
    config = load_config(root)
    section = config.section(SECTION, DEFAULTS)
    limits = {"interval_hours": (None, "hours"), "timeout_seconds": (MAX_TIMEOUT_SECONDS, "seconds")}
    for key, (ceiling, unit) in limits.items():
        value = section[key]
        if not math.isfinite(value) or value <= 0 or (ceiling is not None and value > ceiling):
            bound = f" and at most {ceiling:g}" if ceiling is not None else ""
            raise ConfigError("OAH_CONFIG_INVALID",
                              f"{config.where}: {SECTION}.{key} must be a number of {unit} above 0{bound}, not "
                              f"{value!r}; change it, or remove it to use {DEFAULTS[key]:g}")
    raw = config.raw.get(SECTION)
    explicit = isinstance(raw, Mapping) and "after_finish" in raw
    return Settings(section["after_finish"] is True, float(section["interval_hours"]),
                    float(section["timeout_seconds"]), explicit)


def due(record: Mapping | None, settings: Settings, now: dt.datetime) -> bool:
    """A pass after a finish runs at most once per interval; no record or a future time counts as due."""
    last = parse_stamp(record.get("last_started")) if record is not None else None
    return last is None or last > now or now - last >= dt.timedelta(hours=settings.interval_hours)


# Validation ----------------------------------------------------------------------------------------

def _text(value: object, *, optional: bool) -> bool:
    if value is None:
        return optional
    return isinstance(value, str) and len(value) <= TEXT_LIMIT


def component_problem(name: object, value: object) -> str | None:
    """Why one component entry is wrongly shaped, or None."""
    if not isinstance(name, str) or not COMPONENT_RE.fullmatch(name):
        return "names an unknown component"
    if not isinstance(value, dict) or set(value) - {"hook_approval_digest"} != set(COMPONENT_FIELDS):
        return f"holds a component {name} with the wrong fields"
    status = value["status"]
    if status not in STATUSES:
        return f"holds an unknown status for {name}"
    if not (_text(value["before"], optional=True) and _text(value["after"], optional=True)
            and _text(value["settled_version"], optional=True) and _text(value["next_step"], optional=False)):
        return f"holds a value of the wrong type for {name}"
    if parse_stamp(value["checked_at"]) is None:
        return f"holds no valid check time for {name}"
    settled = value["settled_version"]
    if status in SETTLED and (settled is None or settled != value["after"]):
        return f"holds an {status} result for {name} without its settled version"
    if status not in SETTLED and settled is not None:
        return f"records a settled version for the {status} result of {name}"
    if status in SETTLED:
        before, after = value["before"], value["after"]
        if any(not isinstance(version, str) or not version.strip() or "�" in version for version in (before, after)):
            return f"holds an {status} result for {name} without readable before and after versions"
        if (before != after) != (status == "updated"):
            return f"holds an {status} result for {name} with contradictory versions"
    if "hook_approval_digest" in value:
        digest = value["hook_approval_digest"]
        readable = (digest is None or digest == "absent" or
                    isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None)
        if status != "needs-hook-approval" or not name.startswith("codex-plugin:") or not readable:
            return f"holds invalid hook approval evidence for {name}"
    return None


def _components_problem(components: object) -> str | None:
    if not isinstance(components, dict):
        return "holds components that are not a table"
    for name, value in components.items():
        problem = component_problem(name, value)
        if problem:
            return problem
    return None


def _engines_problem(value: object) -> str | None:
    if not (isinstance(value, list) and all(item in ENGINES for item in value) and len(set(value)) == len(value)):
        return "holds an invalid models_refresh list"
    return None


def state_problem(data: object) -> str | None:
    """Why state.json is wrongly shaped, or None."""
    fields = {"version", "last_started", "trigger", "last_finished", "outcome", "components", "models_refresh"}
    if not isinstance(data, dict) or set(data) != fields:
        return "is not a tool-update state record"
    if data["version"] != FORMAT:
        return f"is not a version-{FORMAT} record"
    if parse_stamp(data["last_started"]) is None or data["trigger"] not in TRIGGERS:
        return "holds no valid start time or trigger"
    finished, result = data["last_finished"], data["outcome"]
    if (finished is None) != (result is None):
        return "holds a finish time and an outcome that disagree"
    if finished is not None and (parse_stamp(finished) is None or result not in OUTCOMES):
        return "holds no valid finish time or outcome"
    problem = _components_problem(data["components"]) or _engines_problem(data["models_refresh"])
    if problem:
        return problem
    if finished is not None and result != outcome(data["components"]):
        return "holds an outcome that contradicts its components"
    return None


def run_problem(data: object) -> str | None:
    """Why one runs.jsonl line is wrongly shaped, or None."""
    fields = {"version", "started", "finished", "trigger", "outcome", "components", "models_refresh"}
    if not isinstance(data, dict) or set(data) != fields or data["version"] != FORMAT:
        return "is not a tool-update run record"
    if parse_stamp(data["started"]) is None or parse_stamp(data["finished"]) is None:
        return "holds no valid times"
    if data["trigger"] not in TRIGGERS or data["outcome"] not in OUTCOMES:
        return "holds an unknown trigger or outcome"
    problem = _components_problem(data["components"]) or _engines_problem(data["models_refresh"])
    if problem:
        return problem
    if data["outcome"] != outcome(data["components"]):
        return "holds an outcome that contradicts its components"
    return None


# Reading -------------------------------------------------------------------------------------------

class StateUnavailable(Exception):
    code = STATE_UNAVAILABLE

    def __init__(self, path: pathlib.Path, why: str) -> None:
        self.path = path
        super().__init__(f"{self.code}: {path} {why}, so oah trusts none of it and checks every tool again; "
                         "delete it and run oah update to write a fresh record")


def read_state() -> dict | None:
    """The last pass's record. None only when the pass has never recorded anything."""
    path = state_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        try:
            runs_path().lstat()
        except FileNotFoundError:
            return None  # the lock and the log share the folder, so only the records say a pass recorded anything
        except OSError as exc:
            raise StateUnavailable(runs_path(), f"cannot be read ({type(exc).__name__})") from None
        raise StateUnavailable(path, "is missing although earlier passes were recorded") from None
    except OSError as exc:
        raise StateUnavailable(path, f"cannot be read ({type(exc).__name__})") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise StateUnavailable(path, "is not valid UTF-8 JSON") from None
    problem = state_problem(data)
    if problem:
        raise StateUnavailable(path, problem)
    return data


def read_runs(*, report: Callable[[str], None] | None = None) -> list[dict]:
    """Valid lines, oldest first; report the skipped count, and raise StateUnavailable for unreadable history."""
    try:
        raw = runs_path().read_bytes()
    except FileNotFoundError:
        if runs_path().is_symlink():
            raise StateUnavailable(runs_path(), "cannot be read (broken link)") from None
        return []
    except OSError as exc:
        raise StateUnavailable(runs_path(), f"cannot be read ({type(exc).__name__})") from None
    records = []
    skipped = 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            skipped += 1
            continue
        if run_problem(data) is None:
            records.append(data)
        else:
            skipped += 1
    if skipped and report is not None:
        report(str(StateUnavailable(runs_path(), f"skipped {skipped} invalid run records")))
    return records


# Writing (the caller holds the pass lock) ----------------------------------------------------------

def write_state(data: Mapping[str, Any]) -> None:
    problem = state_problem(dict(data))
    if problem:
        raise ValueError(f"refusing to write a state record that {problem}")
    write_json_atomic(state_path(), dict(data))


def append_run(record: Mapping[str, Any]) -> None:
    """Add one pass to runs.jsonl, keeping the newest RUNS_KEPT valid lines; rewritten atomically."""
    problem = run_problem(dict(record))
    if problem:
        raise ValueError(f"refusing to write a run record that {problem}")
    try:
        history = read_runs()
    except StateUnavailable:
        history = []  # already reported at entry; unreadable history never prevents a later pass
    kept = [*history, dict(record)][-RUNS_KEPT:]
    write_text_atomic(runs_path(), "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                                           for item in kept))


def component(status: str, before: str | None, after: str | None, next_step: str, checked_at: str) -> dict:
    """One component entry; only updated and current results carry a settled version."""
    return {"status": status, "before": before, "after": after, "next_step": next_step, "checked_at": checked_at,
            "settled_version": after if status in SETTLED else None}


def redacted(value: Any, redact: Callable[[str], str]) -> Any:
    """Every string in a record, keys included, through the run-record redaction."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redacted(item, redact) for item in value]
    if isinstance(value, dict):
        return {redact(key): redacted(item, redact) for key, item in value.items()}
    return value


def outcome(components: Mapping[str, Mapping]) -> str:
    """complete only when every component is updated, current or unsupported."""
    return "complete" if all(item["status"] in COMPLETE for item in components.values()) else "needs-attention"


def counts(components: Mapping[str, Mapping], order: Sequence[str] = STATUSES) -> str:
    tally = {status: 0 for status in order}
    for item in components.values():
        tally[item["status"]] = tally.get(item["status"], 0) + 1
    return ", ".join(f"{number} {status}" for status, number in tally.items() if number) or "nothing to check"
