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

Fetching
    Only at a catalog rebuild (`fetch=True`), never on a path that writes nothing or in `oah fix`, and only when the
    stored copy is absent, invalid or older than `[models] ttl_hours`. Each source is fetched at most once per
    command. A request is an HTTPS GET with no body, no query string, no cookie and no project, user or task data,
    sent with `User-Agent: oah/<version>` through an opener that takes no proxy from the environment and follows no
    redirect, with a 10 second timeout (`TIMEOUT`) for each wait and in total between reads, and a byte limit read in
    chunks (`LIMITS`). A failed, oversized or invalid reply keeps the last good copy and warns once
    (`OAH_SCORES_REFRESH_FAILED`); a launch never fails because of it.

    Epoch AI's CSV needs no key. Artificial Analysis needs the user's own key, `ARTIFICIAL_ANALYSIS_API_KEY`, which
    `oah.secrets` looks up (environment, then the OS keychain) right before that fetch only. It is revealed only to
    build the `x-api-key` header, checked to be visible ASCII first so that no header error can quote it, and
    dropped after the request. No error, warning or stored file holds it or the reply. With no key, nothing is sent
    there, and a stored Artificial Analysis copy is removed at that refresh, so a removed key stops its use.

    Artificial Analysis rows are matched by name (`aa_scores`), as the API documentation describes them; this is
    not yet confirmed against a live reply. `model_creator.slug` is `openai` for Codex and `anthropic` for Claude.
    A Codex slug `gpt-<major>[-<minor>]-<family>[-<effort>]` maps to `gpt-<major>[.<minor>]-<family>`, from its
    `max` row, else `xhigh`, else the row with no effort; other efforts are ignored. A Claude slug
    `claude-<family>-<major>[-<minor>]...` maps to that generation, and of rows that map to one generation the
    highest value counts. Only known families are kept. An engine uses the coding index when every model it
    matched has one, else the intelligence index; a value must be a finite number from 0 to 100.

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
import http.client
import io
import json
import math
import re
import time
import urllib.error
import urllib.request
from typing import Mapping, Sequence

from oah import scoring, secrets

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
AA_KEY_NAME = "ARTIFICIAL_ANALYSIS_API_KEY"
NAMES = {EPOCH: EPOCH_SOURCE, AA: AA_SOURCE}
TIMEOUT = 10.0
LIMITS = {EPOCH: 32 * 1024 * 1024, AA: 8 * 1024 * 1024}
FILE_INVALID = "OAH_SCORES_FILE_INVALID"
AA_UNUSED = "OAH_SCORES_AA_UNUSED"
REFRESH_FAILED = "OAH_SCORES_REFRESH_FAILED"
_CHUNK = 64 * 1024
_TIMED_OUT = f"did not answer within {TIMEOUT:g} seconds, a temporary problem"
_UNREACHABLE = "could not be reached"
_CREATORS = {"openai": "codex", "anthropic": "claude"}
_EFFORTS = {"max": 0, "xhigh": 1, None: 2}  # the row a Codex model takes, best first
_AA_CODEX = re.compile(r"gpt-(?P<major>[0-9]{1,3})(?:-(?P<minor>[0-9]{1,3}))?-(?P<family>[a-z]{2,16})"
                       r"(?:-(?P<effort>[a-z]+))?")
# A version number ends at a non-digit, so a dated slug such as claude-opus-5-20260101 is generation 5.
_AA_CLAUDE = re.compile(r"claude-(?P<family>[a-z]{2,16})-(?P<major>[0-9]{1,3})(?![0-9])"
                        r"(?:-(?P<minor>[0-9]{1,3})(?![0-9]))?")
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
    tried: set = dataclasses.field(default_factory=set)  # the sources this command has fetched or tried to
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
             previous=None, rebuilt: bool = False, fetch: bool = False, env: Mapping[str, str] | None = None,
             keychain=None, now: dt.datetime | None = None) -> Selection:
    """The one choice of score data for one engine and launch (see "One choice of data" above). `fetch` is true only
    in a command that rebuilt the catalog and may write; `env` and `keychain` are where the key is looked up."""
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
    if fetch:
        now = now or dt.datetime.now(dt.timezone.utc)
        for source in (EPOCH, AA):
            if source not in session.tried and _due(session.files[source], settings["ttl_hours"], now):
                session.tried.add(source)
                notices += _refresh(source, catalog, session, known=known, env=env or {}, keychain=keychain, now=now)
    data, missing = _choose(engine, entry, session.files, known)
    if missing and rebuilt:
        notices.append(_unused(engine, missing))
    before = _choose(engine, previous, session.first, known)[0] if previous is not None else data
    return Selection(data, before, tuple(notices))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the request, and the key header, to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib signature
        try:
            return None
        finally:
            del self, req, fp, code, msg, headers, newurl


def _opener() -> urllib.request.OpenerDirector:
    """The one way a score fetch reaches the network: no proxy from the environment, no redirect and no cookie."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class FetchFailed(Exception):
    """A fetch that gave no usable data. The message is plain words; it never holds the key or the reply, and it is
    raised outside every handler, so it carries no other exception either."""


def _close(thing) -> None:
    try:
        thing.close()
    except Exception:  # closing a finished reply must never turn a fetch into a failure of its own
        pass


def _size(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB" if limit % (1024 * 1024) == 0 else f"{limit} bytes"


def _http(code: int, keyed: bool) -> str:
    if 300 <= code < 400:
        return f"answered with a redirect (HTTP {code}), which oah does not follow"
    if code == 401 and keyed:
        return f"refused the key (HTTP 401); check the key stored as {AA_KEY_NAME}"
    if code == 429:
        return "is busy (HTTP 429), a temporary problem"
    return f"answered HTTP {code}"


def _read_body(response, limit: int, deadline: float) -> tuple[bytes | None, str | None]:
    status = getattr(response, "status", None)
    if status != 200:
        return None, f"answered HTTP {status}"
    length = response.headers.get("Content-Length")
    if isinstance(length, str) and length.strip().isdigit() and int(length) > limit:
        return None, f"sent more than {_size(limit)}"
    read = getattr(response, "read1", None) or response.read
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            return None, _TIMED_OUT
        chunk = read(_CHUNK)
        if not chunk:
            return b"".join(chunks), None
        total += len(chunk)
        if total > limit:
            return None, f"sent more than {_size(limit)}"
        chunks.append(chunk)


def _transfer(url: str, limit: int, key) -> tuple[bytes | None, str | None]:
    """The body, or why there is none. Only Exceptions are turned into a reason, and none leaves this function, so no
    error that saw the request, and its key header, reaches a caller; an interrupt still propagates."""
    from oah import __version__

    headers = {"User-Agent": f"oah/{__version__}"}
    request = response = value = None
    try:
        if key is not None:
            value = key.reveal()
            # http.client quotes a rejected header value in its error, so the key is checked before it is used.
            if not value or any(not 0x21 <= ord(char) <= 0x7E for char in value):
                return None, "was not asked: the stored key has characters a request header cannot carry; store it again"
            headers["x-api-key"] = value
        request = urllib.request.Request(url, headers=headers, method="GET")
        headers = value = None
        deadline = time.monotonic() + TIMEOUT
        try:
            response = _opener().open(request, timeout=TIMEOUT)
            request = None
            return _read_body(response, limit, deadline)
        except urllib.error.HTTPError as exc:
            reason = _http(exc.code, key is not None)
            _close(exc)
            return None, reason
        except urllib.error.URLError as exc:
            return None, _TIMED_OUT if isinstance(exc.reason, TimeoutError) else _UNREACHABLE
        except TimeoutError:
            return None, _TIMED_OUT
        except (OSError, http.client.HTTPException, ValueError):
            return None, _UNREACHABLE
    finally:
        if response is not None:
            _close(response)
        del headers, request, response, value, key


def _get(url: str, *, limit: int, key=None) -> bytes:
    body, reason = _transfer(url, limit, key)
    del key
    if reason is not None:
        raise FetchFailed(reason)
    return body


def _stamp(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _checked(source: str, data: dict) -> dict:
    problem = _problem(source, data)
    if problem is not None:
        raise FetchFailed(f"sent data that {problem}")
    return data


def fetch_epoch(now: dt.datetime) -> dict:
    """Epoch AI's CSV, read with `epoch_scores` and dated by the UTC day of `now`, as a stored file."""
    body = _get(EPOCH_URL, limit=LIMITS[EPOCH])
    scores = reason = None
    try:
        scores = epoch_scores(body)
    except DataError as exc:
        reason = f"sent data that {exc}"
    if reason is not None:
        raise FetchFailed(reason)
    moment = now.astimezone(dt.timezone.utc)
    return _checked(EPOCH, {**epoch_snapshot(scores, moment.date()), "fetched_at": _stamp(moment),
                            "source_url": EPOCH_URL})


def _json(body: bytes) -> object | None:
    try:
        return json.loads(body.decode("utf-8"), parse_constant=scoring._reject_constant)
    except (ValueError, RecursionError):  # the error keeps the text; it is dropped here
        return None


def _indexes(evaluations: object) -> dict[str, float]:
    """The valid index values of one row, from 0 to 1."""
    if not isinstance(evaluations, dict):
        return {}
    return {field: evaluations[field] / 100 for field in AA_INDEXES
            if scoring._number(evaluations.get(field)) and 0 <= evaluations[field] <= 100}


def aa_scores(reply: object, known: Mapping) -> dict[str, dict[str, float]] | None:
    """Per matched model of a known family, one index value from 0 to 1 under the index's name; None when the reply
    is not the documented shape. See "Fetching" above for the matching, which is unconfirmed against a live reply."""
    if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
        return None
    families = {"codex": set(known["engines"]["codex"]["families"]), "claude": set(known["engines"]["claude"]["aliases"])}
    rows: dict[str, dict[str, tuple[int, dict[str, float]]]] = {"codex": {}, "claude": {}}
    for row in reply["data"]:
        creator = row.get("model_creator") if isinstance(row, dict) else None
        engine = _CREATORS.get(creator.get("slug")) if isinstance(creator, dict) else None
        if engine is None or not isinstance(row.get("slug"), str):
            continue
        values = _indexes(row.get("evaluations"))
        if not values:
            continue
        if engine == "codex":
            match = _AA_CODEX.fullmatch(row["slug"])
            if match is None or match["family"] not in families["codex"] or match["effort"] not in _EFFORTS:
                continue
            minor = f".{int(match['minor'])}" if match["minor"] else ""
            model = f"gpt-{int(match['major'])}{minor}-{match['family']}"
            rank = _EFFORTS[match["effort"]]
            if model not in rows["codex"] or rank < rows["codex"][model][0]:
                rows["codex"][model] = (rank, values)
        else:
            match = _AA_CLAUDE.match(row["slug"])
            if match is None or match["family"] not in families["claude"]:
                continue
            minor = f"-{int(match['minor'])}" if match["minor"] else ""
            model = f"claude-{match['family']}-{int(match['major'])}{minor}"
            merged = dict(rows["claude"].get(model, (0, {}))[1])
            for field, value in values.items():
                merged[field] = max(value, merged.get(field, value))
            rows["claude"][model] = (0, merged)
    scores: dict[str, dict[str, float]] = {}
    for found in rows.values():
        coding = "artificial_analysis_coding_index"
        field = coding if all(coding in values for _rank, values in found.values()) else \
            "artificial_analysis_intelligence_index"
        scores.update((model, {AA_INDEXES[field]: values[field]}) for model, (_rank, values) in found.items()
                      if field in values)
    return scores


def fetch_aa(key, known: Mapping, now: dt.datetime) -> dict:
    """Artificial Analysis's model list, asked with the user's key, reduced to the matched index values."""
    body = _get(AA_URL, limit=LIMITS[AA], key=key)
    del key
    scores = aa_scores(_json(body), known)
    del body
    if scores is None:
        raise FetchFailed("sent a reply oah cannot read")
    if not scores:
        raise FetchFailed("sent no model of a family oah knows")
    moment = now.astimezone(dt.timezone.utc)
    return _checked(AA, {"schema": 1, "source": AA_SOURCE, "date": moment.date().isoformat(), "url": AA_SITE,
                         "credit": AA_CREDIT, "min_tasks": 1, "scores": scores, "fetched_at": _stamp(moment),
                         "source_url": AA_URL})


def _due(data: Mapping | None, ttl_hours: float, now: dt.datetime) -> bool:
    """Whether a stored copy should be fetched again: absent, invalid, older than the TTL, or dated in the future."""
    if data is None:
        return True
    age = now - _stamp_moment(data["fetched_at"])
    return age < dt.timedelta(0) or age >= dt.timedelta(hours=ttl_hours)


def _key(env: Mapping[str, str], keychain):
    try:
        return secrets.get(AA_KEY_NAME, env=env, keychain=keychain)
    except Exception:  # a broken keychain reads as no key, as for every optional feature
        return None


def _failed(source: str, reason: str, last_good: Mapping | None) -> str:
    using = (f"the copy fetched {last_good['fetched_at']}" if last_good is not None else
             "the snapshot shipped with oah" if source == EPOCH else "Epoch AI scores")
    return (f"{REFRESH_FAILED}: {NAMES[source]} {reason}; using {using}. oah tries again at the next catalog rebuild, "
            "and `[models] fetch_scores = false` in .oah/config.toml stops these requests")


def _refresh(source: str, catalog, session: Session, *, known: Mapping | None, env: Mapping[str, str], keychain,
             now: dt.datetime) -> list[str]:
    """Fetch one source and store it; the warning to give, if any."""
    key = None
    if source == AA:
        key = _key(env, keychain)
        if key is None:
            try:
                catalog.drop_scores(FILES[AA])
            except OSError:
                pass  # this command still ignores it, and the next refresh tries again
            session.files[AA] = None
            return []
        if known is None:
            return []  # the family rules cannot be read, so no data can be matched and no choice made
    reason = None
    try:
        data = fetch_aa(key, known, now) if source == AA else fetch_epoch(now)
    except FetchFailed as exc:
        reason = str(exc)
    finally:
        key = None
    if reason is None:
        try:
            catalog.save_scores(FILES[source], data)
        except OSError:
            reason = "sent data that could not be saved in the state folder"
        else:
            session.files[source] = data
            return []
    return session.say(_failed(source, reason, session.files[source]))
