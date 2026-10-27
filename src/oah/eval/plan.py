# 평가 계획과 사전 등록 — 계획 TOML 을 읽고, 사례·변형·지표·채점 규칙을 얼린 JSON 과 그 SHA-256 을 쓰고 다시 검증한다.
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from oah.atomic import StorageError, write_text_atomic
from oah.eval.case import case_digest, file_sha256, is_inside, load_case, read_toml
from oah.eval.commands import resolve_argv, substitute
from oah.eval.errors import EXIT_ENVIRONMENT, EvalError
from oah.eval.grade import load_oracle
from oah.eval.workspace import (build_workspace, content_hashes, open_snapshot, oracle_path_checks,
                                remove_tree, scan_workspace, variant_environment)

FORMAT = "oah-eval-prereg/1"
METRICS = ("matched", "missed", "unmatched", "human_verdicts", "tests_fixed", "exit_code", "timed_out",
           "wall_seconds", "rounds", "corrections", "usage")
GRADER = {
    "defect": "a finding matches an expected defect when it names one of the defect's files (when files are "
              "listed) and contains every keyword, ignoring case",
    "test": "a test command must fail on the starting snapshot; passing on the variant's workspace is fixed",
    "unmatched_findings": "human-judgement",
    "false_positives": "only from a human verdict file",
}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CASE_KEYS = {"path", "used_for_development"}
_VARIANT_KEYS = {"name", "command", "fake", "timeout_seconds", "config"}


@dataclass(frozen=True)
class Variant:
    name: str
    command: tuple[str, ...]
    fake: bool
    timeout_seconds: float | None
    config_text: str | None


@dataclass(frozen=True)
class PlanCase:
    path: pathlib.Path
    used_for_development: bool


@dataclass(frozen=True)
class Plan:
    path: pathlib.Path
    folder: pathlib.Path
    cases: tuple[PlanCase, ...]
    variants: tuple[Variant, ...]


@dataclass(frozen=True)
class Source:
    """What `oah eval run` executes: a frozen preregistration (sha256 set) or an unfrozen plan (sha256 None)."""
    data: dict
    sha256: str | None
    path: pathlib.Path


def _plan_error(where: str, message: str) -> EvalError:
    return EvalError("OAH_EVAL_PLAN_INVALID", f"{where}: {message}")


def _tables(data: Mapping[str, Any], key: str, where: str) -> list[Mapping[str, Any]]:
    tables = data.get(key, [])
    if not isinstance(tables, list) or not tables or not all(isinstance(item, Mapping) for item in tables):
        raise _plan_error(where, f"add at least one [[{key}]] table")
    return tables


def load_plan(path: pathlib.Path) -> Plan:
    path = pathlib.Path(path).resolve()
    where = str(path)
    data = read_toml(path, "OAH_EVAL_PLAN_INVALID", "evaluation plan")
    unknown = set(data) - {"case", "variant"}
    if unknown:
        raise _plan_error(where, f"unknown key {sorted(unknown)[0]}; use [[case]] and [[variant]] tables")
    folder = path.parent
    cases = []
    for item in _tables(data, "case", where):
        extra = set(item) - _CASE_KEYS
        if extra:
            raise _plan_error(where, f"[[case]] has unknown key {sorted(extra)[0]}; use path and used_for_development")
        if not isinstance(item.get("path"), str) or not item["path"]:
            raise _plan_error(where, "each [[case]] needs path, the case folder relative to the plan")
        used = item.get("used_for_development", False)
        if not isinstance(used, bool):
            raise _plan_error(where, "used_for_development must be true or false")
        cases.append(PlanCase((folder / item["path"]).resolve(), used))
    variants = []
    for item in _tables(data, "variant", where):
        extra = set(item) - _VARIANT_KEYS
        if extra:
            raise _plan_error(where, f"[[variant]] has unknown key {sorted(extra)[0]}; use "
                                     + ", ".join(sorted(_VARIANT_KEYS)))
        name = item.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise _plan_error(where, "each [[variant]] needs a name of letters, digits, '.', '_' or '-'")
        if any(v.name == name for v in variants):
            raise _plan_error(where, f"variant name {name!r} is used twice; give each variant its own name")
        command = item.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
            raise _plan_error(where, f"variant {name}: command must be a non-empty list of strings")
        fake = item.get("fake", False)
        if not isinstance(fake, bool):
            raise _plan_error(where, f"variant {name}: fake must be true or false")
        timeout = item.get("timeout_seconds")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or timeout <= 0):
            raise _plan_error(where, f"variant {name}: timeout_seconds must be a positive number")
        config_text = None
        if item.get("config") is not None:
            if not isinstance(item["config"], str):
                raise _plan_error(where, f"variant {name}: config must be a path to an oah config file")
            config_path = folder / item["config"]
            try:
                config_text = config_path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise _plan_error(where, f"variant {name}: cannot read config {config_path} as UTF-8 ({exc})") from exc
        variants.append(Variant(name, tuple(command), fake, float(timeout) if timeout else None, config_text))
    return Plan(path, folder, tuple(cases), tuple(variants))


def _file_digests(command: Iterable[str], plan_dir: str) -> dict[str, str]:
    """SHA-256 of every command argument that names an existing file (other than the Python interpreter)."""
    digests = {}
    for arg in command:
        if arg == "{python}":
            continue
        path = pathlib.Path(substitute(arg, plan_dir))
        if path.is_absolute() and path.is_file():
            digests[arg] = file_sha256(path)
    return digests


def _source_data(plan: Plan, oracles: Mapping[str, Any] | None) -> dict:
    cases, seen = [], set()
    for planned in plan.cases:
        case = load_case(planned.path)
        if case.id in seen:
            raise _plan_error(str(plan.path), f"case id {case.id} appears twice; list each case once")
        seen.add(case.id)
        if case.split == "holdout" and planned.used_for_development:
            raise EvalError("OAH_EVAL_HOLDOUT_USED",
                            f"case {case.id} was used for development, so it cannot be a holdout case; set "
                            f'split = "dev" in {case.folder / "case.toml"}')
        entry = {"id": case.id, "split": case.split, "path": str(case.folder), "case_sha256": case_digest(case.folder),
                 "snapshot_commit": case.snapshot_commit, "used_for_development": planned.used_for_development}
        if oracles is not None:
            oracle = oracles[case.id]
            entry["oracle_sha256"] = oracle.sha256
            entry["oracle_match_sha256"] = sorted(content_hashes(oracle.path.read_bytes()))
        cases.append(entry)
    plan_dir = str(plan.folder)
    variants = [{"name": v.name, "command": list(v.command), "fake": v.fake, "timeout_seconds": v.timeout_seconds,
                 "config_text": v.config_text, "files": _file_digests(v.command, plan_dir)} for v in plan.variants]
    return {"format": FORMAT, "plan": str(plan.path), "plan_dir": plan_dir, "cases": cases, "variants": variants,
            "metrics": list(METRICS), "grader": dict(GRADER)}


def preregister(plan: Plan, oracle_paths: Iterable[pathlib.Path], out: pathlib.Path) -> str:
    """Write the frozen preregistration and <out>.sha256; return its SHA-256."""
    out = pathlib.Path(out)
    if out.exists():
        raise EvalError("OAH_EVAL_PREREG_EXISTS", f"{out} already exists; a preregistration is never rewritten, "
                                                  "so choose a new --out file")
    oracle_paths = tuple(pathlib.Path(path) for path in oracle_paths)
    oracles = {}
    for path in oracle_paths:
        oracle = load_oracle(pathlib.Path(path))
        if oracle.case in oracles:
            raise EvalError("OAH_EVAL_ORACLE_INVALID", f"two oracle files are for case {oracle.case}; pass one")
        oracles[oracle.case] = oracle
    case_folders = {}
    for planned in plan.cases:
        case = load_case(planned.path)
        case_folders[case.id] = case.folder
        if case.id not in oracles:
            raise EvalError("OAH_EVAL_ORACLE_MISSING",
                            f"no oracle file for case {case.id}; pass it with --oracle FILE")
    for oracle in oracles.values():
        if oracle.case not in case_folders:
            raise EvalError("OAH_EVAL_ORACLE_INVALID",
                            f"{oracle.path} is for case {oracle.case}, which the plan does not list")
        for folder in case_folders.values():
            if is_inside(oracle.path, folder):
                raise EvalError("OAH_EVAL_ORACLE_IN_CASE",
                                f"{oracle.path} is inside the case folder {folder}, where agents would see it; "
                                "move it outside the case folder")
    path_checks = oracle_path_checks(oracle_paths)
    hashes = {digest for oracle in oracles.values() for digest in content_hashes(oracle.path.read_bytes())}
    channel_files = {variant.name: {} for variant in plan.variants}
    # Check every available launch channel before writing either frozen file.
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="oah-eval-prereg-"))
    try:
        for index, planned in enumerate(plan.cases):
            case = load_case(planned.path)
            snapshot = open_snapshot(case, scratch / f"source-{index}")
            try:
                prompt = case.prompt.read_bytes().decode("utf-8-sig").encode("utf-8")
            except (OSError, UnicodeError):
                raise EvalError("OAH_EVAL_CHANNEL_UNCHECKED", "prompt cannot be read as UTF-8; supply a regular "
                                "UTF-8 prompt and preregister again") from None
            for variant in plan.variants:
                ws = build_workspace(snapshot, scratch / f"ws-{index}-{variant.name}", config_text=variant.config_text)
                env = variant_environment(os.environ, ws)
                argv = resolve_argv([substitute(arg, str(plan.folder)) for arg in variant.command], env, "variant")
                checked = scan_workspace(ws, snapshot, oracle_hashes=hashes, oracle_paths=path_checks,
                                         prompt=prompt, argv=argv, env=env, config_text=variant.config_text,
                                         plan_dir=str(plan.folder))
                channel_files[variant.name][case.id] = checked["channel_files"]
    finally:
        remove_tree(scratch)
    data = _source_data(plan, oracles)
    data["oracle_path_checks"] = path_checks
    for variant in data["variants"]:
        variant["channel_files"] = channel_files[variant["name"]]
    data["created_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        write_text_atomic(out, text)
        write_text_atomic(_sidecar(out), f"{digest}  {out.name}\n")
    except StorageError as exc:
        raise EvalError("OAH_EVAL_IO", str(exc), EXIT_ENVIRONMENT) from exc
    return digest


def _sidecar(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".sha256")


def load_frozen(path: pathlib.Path, expect_sha256: str | None = None) -> Source:
    """A preregistration whose bytes still match its recorded (or the given) SHA-256."""
    path = pathlib.Path(path).resolve()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise EvalError("OAH_EVAL_PLAN_INVALID", f"{path} does not exist; pass a plan .toml or a "
                                                 "preregistration .json") from None
    except OSError as exc:
        raise EvalError("OAH_EVAL_IO", f"{path} cannot be read ({exc}); check its permissions",
                        EXIT_ENVIRONMENT) from exc
    actual = hashlib.sha256(raw).hexdigest()
    expected = expect_sha256.strip().lower() if expect_sha256 else None
    if expected is None:
        try:
            expected = _sidecar(path).read_text(encoding="utf-8").split()[0].lower()
        except (OSError, IndexError):
            raise EvalError("OAH_EVAL_PREREG_HASH",
                            f"there is no {_sidecar(path).name} to check {path.name} against; keep the file "
                            "`oah eval prereg` wrote next to it, or pass --expect-sha256 HASH") from None
    if actual != expected:
        raise EvalError("OAH_EVAL_PREREG_HASH",
                        f"{path.name} has SHA-256 {actual}, not the preregistered {expected}; it changed after it "
                        "was frozen. Preregister again with oah eval prereg instead of editing it")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvalError("OAH_EVAL_PLAN_INVALID", f"{path} is not UTF-8 JSON ({exc})") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise EvalError("OAH_EVAL_PLAN_INVALID", f"{path} is not an oah eval preregistration ({FORMAT})")
    return Source(data, actual, path)


def load_run_source(path: pathlib.Path, expect_sha256: str | None = None) -> Source:
    """A .toml plan runs unfrozen (holdout cases are refused later); anything else is a preregistration."""
    path = pathlib.Path(path)
    if path.suffix.lower() == ".toml":
        if expect_sha256:
            raise EvalError("OAH_EVAL_PLAN_INVALID", "--expect-sha256 applies to a preregistration .json, "
                                                     "not to a plan .toml")
        plan = load_plan(path)
        return Source(_source_data(plan, None), None, plan.path)
    return load_frozen(path, expect_sha256)
