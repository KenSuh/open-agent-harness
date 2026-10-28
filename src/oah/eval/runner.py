# 평가 실행 — 사전 등록을 대조하고 모든 변형의 새 작업 공간을 격리 검사한 뒤에야 변형을 띄우며, 종료 코드·시간·사용량(없으면 unknown)을 기록한다.
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Mapping

from oah.atomic import StorageError, write_json_atomic, write_text_atomic
from oah.engines.proc import run_command
from oah.eval.case import Case, case_digest, file_sha256, load_case
from oah.eval.commands import resolve_argv, substitute
from oah.eval.errors import EXIT_ENVIRONMENT, EvalError
from oah.eval.grade import RUN_FILE, normalize_finding
from oah.eval.plan import Source
from oah.eval.workspace import (ORACLE_NORMALIZATION, RUNTIME_ENV, Workspace, build_workspace,
                                check_grader_separation, open_snapshot, remove_tree, scan_workspace,
                                variant_environment)

DEFAULTS = {"timeout_seconds": 1800.0, "test_timeout_seconds": 300.0}
RESULT_FILE = "result.json"
OUTPUT_FILE = "result.json"
UNKNOWN = "unknown"
CONTROL_FLOW_ONLY = "control-flow-only"
_RUNTIME_ENV = RUNTIME_ENV


@dataclass
class _Prepared:
    entry: Mapping[str, Any]
    case: Case
    variant: Mapping[str, Any]
    argv: list[str]
    workspace: Workspace
    isolation: dict
    prompt: str


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_cases(source: Source) -> list[tuple[Mapping[str, Any], Case]]:
    frozen = source.sha256 is not None
    checked = []
    for entry in source.data["cases"]:
        case = load_case(pathlib.Path(entry["path"]))
        if "holdout" in (entry["split"], case.split):
            if not frozen:
                raise EvalError("OAH_EVAL_PREREG_REQUIRED",
                                f"case {case.id} is a holdout case, which runs only from a matching "
                                "preregistration; run oah eval prereg first and pass its .json to oah eval run")
            if entry.get("used_for_development"):
                raise EvalError("OAH_EVAL_HOLDOUT_USED",
                                f"the preregistration says case {case.id} was used for development, so it cannot "
                                f'run as holdout; set split = "dev" in its case.toml and preregister again')
        if frozen:
            changed = [name for name, now, then in (
                ("id", case.id, entry["id"]), ("split", case.split, entry["split"]),
                ("snapshot commit", case.snapshot_commit, entry["snapshot_commit"]),
                ("contents", case_digest(case.folder), entry["case_sha256"])) if now != then]
            if changed:
                raise EvalError("OAH_EVAL_PREREG_MISMATCH",
                                f"case {entry['id']} no longer matches its preregistration ({', '.join(changed)} "
                                "changed). Restore it, or preregister again; a case you used to change the "
                                'implementation must be relabelled split = "dev"')
        checked.append((entry, case))
    return checked


def _check_variants(source: Source, env: Mapping[str, str]) -> list[tuple[Mapping[str, Any], list[str]]]:
    plan_dir = source.data["plan_dir"]
    checked = []
    for variant in source.data["variants"]:
        if source.sha256 is not None:
            for arg, digest in variant.get("files", {}).items():
                path = pathlib.Path(substitute(arg, plan_dir))
                if not path.is_file() or file_sha256(path) != digest:
                    raise EvalError("OAH_EVAL_PREREG_MISMATCH",
                                    f"variant {variant['name']}: {path} changed since preregistration; restore it or "
                                    "preregister again")
        argv = [substitute(arg, plan_dir) for arg in variant["command"]]
        checked.append((variant, argv))
    return checked


def _read_output(output: pathlib.Path) -> tuple[str, dict]:
    path = output / OUTPUT_FILE
    if not path.is_file():
        return "missing", {}
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"invalid: {exc}", {}
    if not isinstance(data, dict):
        return "invalid: not a JSON object", {}
    if not isinstance(data.get("findings", []), list):
        return "invalid: findings is not a list", {}
    return "ok", data


def _count(value: Any) -> int | str:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else UNKNOWN


def _usage(value: Any) -> dict | str:
    if isinstance(value, dict) and value and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value.values()):
        return dict(value)
    return UNKNOWN


def _launch(item: _Prepared, run_dir: pathlib.Path, prereg: str | None, env: Mapping[str, str],
            timeout_seconds: float) -> dict:
    ws, variant = item.workspace, item.variant
    folder = run_dir / "cases" / item.case.id / variant["name"]
    folder.mkdir(parents=True)
    variant_env = variant_environment(env, ws)
    started = time.monotonic()
    launch_error = None
    try:
        result = run_command(item.argv, timeout=variant.get("timeout_seconds") or timeout_seconds,
                             cwd=str(ws.repo), env=variant_env, stdin_text=item.prompt)
        exit_code, timed_out, stdout, stderr = result.returncode, result.timed_out, result.stdout, result.stderr
    except OSError as exc:
        exit_code, timed_out, stdout, stderr = None, False, "", ""
        launch_error = f"{exc}"
    wall = round(time.monotonic() - started, 3)
    write_text_atomic(folder / "stdout.txt", stdout)
    write_text_atomic(folder / "stderr.txt", stderr)
    output_state, output = _read_output(ws.output)
    fake = bool(variant.get("fake")) or output.get("engine") == "fake"
    shutil.copytree(ws.repo, folder / "workspace", symlinks=True)
    shutil.copytree(ws.output, folder / "output", symlinks=True)
    record = {
        "case": item.case.id, "split": item.case.split, "variant": variant["name"],
        "label": CONTROL_FLOW_ONLY if fake else "engine-run", "prereg_sha256": prereg,
        "snapshot_commit": item.case.snapshot_commit, "workspace_commit": ws.commit, "command": item.argv,
        "exit_code": exit_code, "timed_out": timed_out, "wall_seconds": float(wall), "launch_error": launch_error,
        "output": output_state, "usage": _usage(output.get("usage")), "rounds": _count(output.get("rounds")),
        "corrections": _count(output.get("corrections")), "isolation": item.isolation,
        "findings": [normalize_finding(f, n) for n, f in enumerate(output.get("findings", []), 1)
                     if isinstance(f, dict)],
    }
    write_json_atomic(folder / RESULT_FILE, record)
    return {"case": item.case.id, "variant": variant["name"],
            "path": (folder / RESULT_FILE).relative_to(run_dir).as_posix()}


def _variant_env(env: Mapping[str, str]) -> dict[str, str]:
    """Only runtime settings cross the variant boundary; no arbitrary caller metadata."""
    return variant_environment(env)


def run_eval(source: Source, runs_root: pathlib.Path, *, env: Mapping[str, str], timeout_seconds: float,
             run_id: str | None = None) -> pathlib.Path:
    """Run every variant on every case; nothing launches unless every workspace passed the isolation scan."""
    path_checks = source.data.get("oracle_path_checks", ())
    if source.sha256 is not None and not any(check.get("folder") for check in path_checks):
        raise EvalError("OAH_EVAL_CHANNEL_UNCHECKED", "this preregistration has no oracle path checks; "
                        "preregister again before running")
    if source.sha256 is not None and any("oracle_block_checks" not in entry for entry in source.data["cases"]):
        raise EvalError("OAH_EVAL_CHANNEL_UNCHECKED", "this preregistration has no oracle block checks; "
                        "preregister again before running")
    if source.sha256 is not None and source.data.get("oracle_normalization") != ORACLE_NORMALIZATION:
        raise EvalError("OAH_EVAL_CHANNEL_UNCHECKED", "this preregistration uses an older oracle normalization; "
                        "preregister again before running")
    run_id = run_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    run_dir = pathlib.Path(runs_root) / run_id
    check_grader_separation([pathlib.Path(entry["path"]) for entry in source.data["cases"]] + [run_dir],
                            path_checks)
    cases = _check_cases(source)
    env = _variant_env(env)
    variants = _check_variants(source, env)
    oracle_hashes = {digest for entry, _ in cases for digest in
                     (*entry.get("oracle_match_sha256", ()), entry.get("oracle_sha256")) if digest}
    oracle_blocks = [block for entry, _ in cases for block in entry.get("oracle_block_checks", ())]
    temporary: list[pathlib.Path] = []
    try:
        prepared: list[_Prepared] = []
        for entry, case in cases:
            scratch = pathlib.Path(tempfile.mkdtemp(prefix="oah-eval-src-"))
            temporary.append(scratch)
            snapshot = open_snapshot(case, scratch)
            try:
                prompt = case.prompt.read_bytes().decode("utf-8-sig")
            except (OSError, UnicodeError):
                raise EvalError("OAH_EVAL_CHANNEL_UNCHECKED", "prompt cannot be read as UTF-8; supply a regular "
                                "UTF-8 prompt and preregister again") from None
            commits = set()
            for variant, command in variants:
                # Each workspace is its own temporary folder, so no variant sits next to another's work.
                root = pathlib.Path(tempfile.mkdtemp(prefix="oah-eval-ws-"))
                temporary.append(root)
                ws = build_workspace(snapshot, root, config_text=variant.get("config_text"))
                argv = resolve_argv(command, env, f"variant {variant['name']}", cwd=ws.repo)
                isolation = scan_workspace(ws, snapshot, oracle_hashes=oracle_hashes, prompt=prompt.encode("utf-8"),
                                           oracle_paths=source.data.get("oracle_path_checks", ()), argv=argv,
                                           env=variant_environment(env, ws), config_text=variant.get("config_text"),
                                           plan_dir=source.data["plan_dir"], oracle_blocks=oracle_blocks)
                if source.sha256 is not None:
                    frozen_files = variant.get("channel_files", {}).get(case.id)
                    if frozen_files is None or any(frozen_files.get(path) != digest for path, digest in
                                                   isolation["channel_files"].items()):
                        raise EvalError("OAH_EVAL_PREREG_MISMATCH", "a variant file reference changed since "
                                        "preregistration; restore it or preregister again")
                isolation.pop("channel_files")
                commits.add(ws.commit)
                prepared.append(_Prepared(entry, case, variant, argv, ws, isolation, prompt))
            if len(commits) != 1:
                raise EvalError("OAH_EVAL_WORKSPACE", f"case {case.id}: the variants did not get the same starting "
                                                      "commit; report this as an oah bug")
        started = _now()
        try:
            run_dir.mkdir(parents=True)
            results = [_launch(item, run_dir, source.sha256, env, timeout_seconds) for item in prepared]
            run = {"format": "oah-eval-run/1", "run_id": run_id, "prereg_sha256": source.sha256,
                   "source": "sha256:" + (source.sha256 or hashlib.sha256(json.dumps(
                       source.data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()),
                   "started_at": started, "finished_at": _now(),
                   "cases": [{key: entry.get(key) for key in ("id", "split", "path", "case_sha256", "oracle_sha256",
                                                              "used_for_development")} for entry, _ in cases],
                   "variants": [{"name": v["name"], "fake": bool(v.get("fake"))} for v, _ in variants],
                   "results": results}
            write_json_atomic(run_dir / RUN_FILE, run)
        except (OSError, StorageError) as exc:
            raise EvalError("OAH_EVAL_IO", f"cannot write the run folder {run_dir} ({exc}); check free space and "
                                           "permissions", EXIT_ENVIRONMENT) from exc
        return run_dir
    finally:
        for path in temporary:
            remove_tree(path)
