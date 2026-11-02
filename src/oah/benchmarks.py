# 벤치마크 점수 자료 — Epoch AI CSV 를 해석하고, 카탈로그를 새로 만들 때 점수를 받아 상태 폴더에 검증해 두며, 엔진마다 쓸 자료 하나를 고른다.
"""Benchmark data: Epoch AI's CSV, fetched score files and the one choice of data per engine.

Epoch AI CSV
    `epoch_scores(data)` keeps, for each allow-listed task (`TASKS`), the latest successful run of each model at
    maximum effort. Data from external projects keeps its own licence, so only Epoch's own tasks are kept.
    `tools/build_benchmarks.py` and a fetch call the same function, so both give the same scores for the same CSV.
    `render` writes the packaged snapshot's bytes.

Stored files
    `<state>/models/benchmarks-epoch.json` has the packaged snapshot's fields plus `fetched_at` and `source_url`.
    `<state>/models/benchmarks-aa.json` keeps, per matched model, one index value from 0 to 1, under the index's
    name, with `min_tasks` 1, the fetch date and Artificial Analysis's credit, and nothing else from the reply.
    `read_stored` checks every field on every read: a fixed field must hold its one value, a date must be the fetch
    date, names must be plain, and a score must be a finite number from 0 to 1. A file that fails counts as absent,
    with `OAH_SCORES_FILE_INVALID`, so no unchecked value reaches a choice or the output.

One choice of data
    `data_for` gives the data one engine uses for one launch. With `[models] fetch_scores = false` it is the
    snapshot shipped with oah, and the stored files are not read. Otherwise, in this order: the stored Artificial
    Analysis file, when it scores the newest listed generation of every known family of that engine; else the
    stored Epoch AI file; else the snapshot. A partial Artificial Analysis set never mixes with Epoch data, and a
    rebuild says once why it was not used (`OAH_SCORES_AA_UNUSED`). A `Session` is one command's view: each file is
    read and checked once, so every engine of a command sees the same version of it, and each notice is said once.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import json
import math
import re
from typing import Mapping, Sequence

from oah import scoring

EPOCH, AA = "epoch", "aa"
FILES = {EPOCH: "benchmarks-epoch.json", AA: "benchmarks-aa.json"}
EPOCH_SOURCE = "Epoch AI"
EPOCH_LICENSE = "CC BY 4.0"
EPOCH_CREDIT_URL = "https://epoch.ai/benchmarks/use-this-data"
EPOCH_URL = "https://epoch.ai/data/benchmarks.csv"
AA_SOURCE = "Artificial Analysis"
AA_SITE = "https://artificialanalysis.ai/"
AA_CREDIT = f"{AA_SOURCE}, {AA_SITE}"
AA_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
AA_INDEXES = {"artificial_analysis_coding_index": "Coding Index",
              "artificial_analysis_intelligence_index": "Intelligence Index"}  # the reply's field: the stored name
FILE_INVALID = "OAH_SCORES_FILE_INVALID"
AA_UNUSED = "OAH_SCORES_AA_UNUSED"
_KEYS = {EPOCH: ("schema", "source", "date", "license", "url", "credit", "scores", "fetched_at", "source_url"),
         AA: ("schema", "source", "date", "url", "credit", "min_tasks", "scores", "fetched_at", "source_url")}
_FIXED = {EPOCH: {"schema": 1, "source": EPOCH_SOURCE, "license": EPOCH_LICENSE, "url": EPOCH_CREDIT_URL,
                  "source_url": EPOCH_URL},
          AA: {"schema": 1, "source": AA_SOURCE, "url": AA_SITE, "credit": AA_CREDIT, "min_tasks": 1,
               "source_url": AA_URL}}
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_EPOCH_ID = re.compile(r"gpt-[0-9.]+-[a-z]+|claude-[a-z]+-[0-9-]+")  # MODEL without its effort
_AA_ID = re.compile(r"gpt-[0-9]{1,3}(?:\.[0-9]{1,3})?-[a-z]{2,16}|claude-[a-z]{2,16}-[0-9]{1,3}(?:-[0-9]{1,3})?")
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


def _scores_problem(source: str, scores: object) -> str | None:
    if not isinstance(scores, dict) or not scores:
        return "has no scores"
    names = _EPOCH_ID if source == EPOCH else _AA_ID
    tasks_allowed = TASKS if source == EPOCH else tuple(AA_INDEXES.values())
    indexes: dict[str, set[str]] = {}
    for model, tasks in scores.items():
        if not (scoring._NAME.fullmatch(model) and names.fullmatch(model)):
            return "has a model name oah does not write"
        if not isinstance(tasks, dict) or not tasks:
            return "has a model with no scores"
        for task, value in tasks.items():
            if task not in tasks_allowed:
                return "has a task oah does not use"
            if not (scoring._number(value) and 0 <= value <= 1):
                return "has a score that is not a number from 0 to 1"
        if source == AA:
            if len(tasks) != 1:
                return "has more than one index for a model"
            indexes.setdefault(model.split("-", 1)[0], set()).update(tasks)
    if any(len(found) > 1 for found in indexes.values()):
        return "mixes two indexes for one engine"
    return None


def _problem(source: str, data: object) -> str | None:
    """Why a parsed stored file is not one oah wrote, or None."""
    if not isinstance(data, dict) or set(data) != set(_KEYS[source]):
        return "does not have the fields oah writes"
    for key, value in _FIXED[source].items():
        if type(data[key]) is not type(value) or data[key] != value:
            return f"has a {key} that oah does not write"
    fetched = data["fetched_at"]
    if not (isinstance(fetched, str) and _STAMP.fullmatch(fetched) and _stamp_moment(fetched) is not None):
        return "has no valid fetch time"
    if data["date"] != fetched[:10]:
        return "has a date that is not its fetch date"
    credit = epoch_credit(dt.date.fromisoformat(data["date"])) if source == EPOCH else AA_CREDIT
    if data["credit"] != credit:
        return "has a credit that oah does not write"
    return _scores_problem(source, data["scores"])


def _stamp_moment(text: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_stored(source: str, raw: bytes | None) -> tuple[dict | None, str | None]:
    """The checked data of one stored file, or None and the plain reason it counts as absent; (None, None) when there
    is no file. Every read goes through here, so no unchecked value reaches a choice or the output."""
    if raw is None:
        return None, None
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=scoring._reject_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None, "is not valid JSON"
    problem = _problem(source, data)
    return (None, problem) if problem else (data, None)


@dataclasses.dataclass
class Session:
    """One command's view of the stored score files: each is read and checked once, so every engine of the command
    sees the same version, and each notice is said once."""

    files: dict = dataclasses.field(default_factory=dict)  # source -> checked data or None
    first: dict = dataclasses.field(default_factory=dict)  # source -> the data as first read, before any refresh
    said: set = dataclasses.field(default_factory=set)

    def say(self, notice: str) -> list[str]:
        if notice in self.said:
            return []
        self.said.add(notice)
        return [notice]


@dataclasses.dataclass(frozen=True)
class Selection:
    data: Mapping | None  # the checked data this engine uses; None: the snapshot shipped with oah
    before: Mapping | None  # the data the replaced entry was chosen with, for change notices; None as above
    notices: tuple[str, ...]


def _read(catalog, source: str) -> tuple[dict | None, str | None]:
    try:
        raw = catalog.score_bytes(FILES[source])
    except OSError:
        return None, "cannot be read"
    return read_stored(source, raw)


def _invalid(catalog, source: str, problem: str) -> str:
    return (f"{FILE_INVALID}: {catalog.dir / FILES[source]} {problem}, so it counts as absent and oah scores without "
            "it. Delete the file, or run `oah models --refresh` to fetch it again")


def _missing(engine: str, entry, data: Mapping, known: Mapping) -> list[str]:
    """The listed models a data set must rank and has no score for: the newest listed generation of each family."""
    spec = known["engines"][engine]
    newest: dict[str, scoring._Candidate] = {}
    for model in entry.models if entry is not None else ():
        found = scoring._candidate(engine, spec, model.name, data["scores"]) if model.listed else None
        if found is not None and (found.family not in newest or found.generation > newest[found.family].generation):
            newest[found.family] = found
    return sorted(item.model for item in newest.values() if not item.tasks)


def _choose(engine: str, entry, files: Mapping, known: Mapping | None) -> tuple[Mapping | None, list[str]]:
    """The data of the first usable source, and the models a stored Artificial Analysis file lacks, if any."""
    missing: list[str] = []
    if files.get(AA) is not None and known is not None:
        missing = _missing(engine, entry, files[AA], known)
        if not missing:
            return files[AA], []
    return files.get(EPOCH), missing


def _unused(engine: str, missing: Sequence[str]) -> str:
    from oah.models import PLAIN_MODEL

    shown = ", ".join(name for name in missing if PLAIN_MODEL.fullmatch(name)) or "a listed model"
    return (f"{AA_UNUSED}: Artificial Analysis has no score for {shown}, which the {engine} CLI lists, so Epoch AI "
            f"scores choose the {engine} model. It clears once Artificial Analysis scores that model; `oah models` "
            "shows the scores in use")


def data_for(engine: str, entry, *, catalog, settings: Mapping, known: Mapping | None, session: Session | None = None,
             previous=None, rebuilt: bool = False) -> Selection:
    """The one choice of score data for one engine and launch (see "One choice of data" above)."""
    if not settings["fetch_scores"]:
        return Selection(None, None, ())
    session = session if session is not None else Session()
    notices: list[str] = []
    for source in (AA, EPOCH):
        if source not in session.files:
            data, problem = _read(catalog, source)
            session.files[source] = session.first[source] = data
            if problem is not None:
                notices += session.say(_invalid(catalog, source, problem))
    data, missing = _choose(engine, entry, session.files, known)
    if missing and rebuilt:
        notices.append(_unused(engine, missing))
    before = _choose(engine, previous, session.first, known)[0] if previous is not None else data
    return Selection(data, before, tuple(notices))
