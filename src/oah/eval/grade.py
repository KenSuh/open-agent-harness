# 평가 채점 — 숨긴 정답 파일의 명시적 규칙(결함 파일·키워드, 스냅샷에서 실패해야 하는 테스트)으로 변형 결과를 대조하고, 짝 없는 지적은 사람 판단으로 남긴다.
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from oah.atomic import StateCorrupt, StorageError, read_json, write_json_atomic
from oah.engines.proc import run_command
from oah.eval.case import case_digest, file_sha256, is_inside, load_case, read_toml
from oah.eval.commands import resolve_argv, substitute
from oah.eval.errors import EXIT_ENVIRONMENT, EvalError
from oah.eval.workspace import build_workspace, open_snapshot, remove_tree, variant_environment

GRADE_FILE = "grade.json"
RUN_FILE = "run.json"
VERDICTS = ("false-positive", "valid", "duplicate", "out-of-scope")
NEEDS_HUMAN = "needs-human-judgement"
DEFAULT_TEST_TIMEOUT = 300.0
_TEXT_FIELDS = ("title", "summary", "detail", "evidence", "text")


@dataclass(frozen=True)
class Defect:
    id: str
    summary: str
    files: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class TestRule:
    id: str
    summary: str
    command: tuple[str, ...]
    timeout_seconds: float | None


@dataclass(frozen=True)
class Oracle:
    path: pathlib.Path
    case: str
    defects: tuple[Defect, ...]
    tests: tuple[TestRule, ...]
    sha256: str


def _oracle_error(path: pathlib.Path, message: str) -> EvalError:
    return EvalError("OAH_EVAL_ORACLE_INVALID", f"{path}: {message}")


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def load_oracle(path: pathlib.Path) -> Oracle:
    path = pathlib.Path(path).resolve()
    data = read_toml(path, "OAH_EVAL_ORACLE_INVALID", "oracle file")
    if not isinstance(data.get("case"), str) or not data["case"]:
        raise _oracle_error(path, 'add case = "<case id>" naming the case this oracle grades')
    ids: set[str] = set()

    def rule_id(item: Mapping[str, Any], kind: str) -> str:
        value = item.get("id")
        if not isinstance(value, str) or not value or value in ids:
            raise _oracle_error(path, f"each [[{kind}]] needs a unique id")
        ids.add(value)
        return value

    defects = []
    for item in data.get("defect", []):
        did = rule_id(item, "defect")
        files, keywords = item.get("files", []), item.get("keywords", [])
        if not _strings(files) or not _strings(keywords) or not (files or keywords):
            raise _oracle_error(path, f"defect {did} needs files and/or keywords, each a list of strings, so a "
                                      "finding can be matched by evidence")
        defects.append(Defect(did, str(item.get("summary", "")), tuple(files), tuple(keywords)))
    tests = []
    for item in data.get("test", []):
        tid = rule_id(item, "test")
        command, timeout = item.get("command"), item.get("timeout_seconds")
        if not _strings(command) or not command:
            raise _oracle_error(path, f"test {tid}: command must be a non-empty list of strings")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or timeout <= 0):
            raise _oracle_error(path, f"test {tid}: timeout_seconds must be a positive number")
        tests.append(TestRule(tid, str(item.get("summary", "")), tuple(command),
                              float(timeout) if timeout else None))
    if not defects and not tests:
        raise _oracle_error(path, "add at least one [[defect]] or [[test]] rule")
    return Oracle(path, data["case"], tuple(defects), tuple(tests), file_sha256(path))


def normalize_finding(raw: Mapping[str, Any], position: int) -> dict:
    """{"id", "files", "text"} from a variant's finding; a missing id becomes F<position>."""
    if "files" in raw and "text" in raw and isinstance(raw.get("files"), list):
        return {"id": str(raw.get("id") or f"F{position}"), "files": list(raw["files"]), "text": str(raw["text"])}
    files = []
    if isinstance(raw.get("file"), str) and raw["file"]:
        files.append(raw["file"])
    if isinstance(raw.get("files"), list):
        files.extend(item for item in raw["files"] if isinstance(item, str) and item)
    text = " ".join(str(raw[key]) for key in _TEXT_FIELDS if isinstance(raw.get(key), str) and raw[key])
    fid = raw.get("id")
    return {"id": str(fid) if isinstance(fid, (str, int)) and str(fid) else f"F{position}", "files": files,
            "text": text}


def _path(value: str) -> str:
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _matches(defect: Defect, finding: Mapping[str, Any]) -> bool:
    if defect.files and not {_path(f) for f in finding["files"]} & {_path(f) for f in defect.files}:
        return False
    text = finding["text"].lower()
    return all(keyword.lower() in text for keyword in defect.keywords)


def match_findings(defects: Iterable[Defect], findings: Iterable[Mapping[str, Any]]) -> dict:
    """Matched expected defects, missed ones, and unmatched findings (left for human judgement)."""
    normalized = [normalize_finding(item, n) for n, item in enumerate(findings, 1)]
    ids = [finding["id"] for finding in normalized]
    if len(ids) != len(set(ids)):
        raise EvalError("OAH_EVAL_FINDING_DUPLICATE", "finding IDs repeat after normalization; give every finding "
                        "in this variant result a unique ID, then grade again")
    matched, missed, used = [], [], set()
    for defect in defects:
        hits = [f["id"] for f in normalized if _matches(defect, f)]
        if hits:
            matched.append({"defect": defect.id, "findings": hits})
            used.update(hits)
        else:
            missed.append(defect.id)
    unmatched = [{"id": f["id"], "file": f["files"][0] if f["files"] else None, "text": f["text"],
                  "verdict": NEEDS_HUMAN} for f in normalized if f["id"] not in used]
    return {"matched": matched, "missed": missed, "unmatched": unmatched}


def _load_verdicts(path: pathlib.Path) -> dict[tuple[str, str, str], dict]:
    data = read_toml(path, "OAH_EVAL_VERDICT_INVALID", "human verdict file")
    verdicts = {}
    for item in data.get("verdict", []):
        key = tuple(item.get(name) for name in ("case", "variant", "finding"))
        if not all(isinstance(part, str) and part for part in key):
            raise EvalError("OAH_EVAL_VERDICT_INVALID", f"{path}: each [[verdict]] needs case, variant and finding")
        if item.get("verdict") not in VERDICTS:
            raise EvalError("OAH_EVAL_VERDICT_INVALID",
                            f"{path}: verdict {item.get('verdict')!r} for {key[2]} is not one of {', '.join(VERDICTS)}")
        if key in verdicts:
            raise EvalError("OAH_EVAL_VERDICT_INVALID", f"{path}: {key[2]} of {key[1]} has two verdicts; keep one")
        verdicts[key] = {"verdict": item["verdict"], "note": str(item.get("note", ""))}
    return verdicts


def load_run(run_dir: pathlib.Path) -> dict:
    path = pathlib.Path(run_dir) / RUN_FILE
    try:
        run = read_json(path, None)
    except (StateCorrupt, StorageError) as exc:
        raise EvalError("OAH_EVAL_RUN_INVALID", f"{exc}") from exc
    if not isinstance(run, dict):
        raise EvalError("OAH_EVAL_RUN_INVALID", f"{run_dir} is not an oah eval run folder (no {RUN_FILE}); pass "
                                                "the folder that oah eval run printed")
    return run


def load_result(run_dir: pathlib.Path, entry: Mapping[str, Any]) -> dict:
    path = pathlib.Path(run_dir) / entry["path"]
    try:
        result = read_json(path, None)
    except (StateCorrupt, StorageError) as exc:
        raise EvalError("OAH_EVAL_RUN_INVALID", f"{exc}") from exc
    if not isinstance(result, dict):
        raise EvalError("OAH_EVAL_RUN_INVALID", f"{path} is missing; the run folder is incomplete, so run again")
    return result


def _run_test(rule: TestRule, repo: pathlib.Path, env: Mapping[str, str], timeout: float) -> tuple[bool, bool]:
    """(passed, timed_out) for one test command in `repo`."""
    argv = resolve_argv([substitute(arg, "") for arg in rule.command], env, f"test {rule.id}", cwd=repo)
    try:
        result = run_command(argv, timeout=rule.timeout_seconds or timeout, cwd=str(repo), env=dict(env))
    except OSError as exc:
        raise EvalError("OAH_EVAL_COMMAND_MISSING", f"test {rule.id} could not start ({exc}); fix its command",
                        EXIT_ENVIRONMENT) from exc
    return result.returncode == 0 and not result.timed_out, result.timed_out


def _snapshot_tests(oracle: Oracle, case_path: pathlib.Path, env: Mapping[str, str], timeout: float) -> list[dict]:
    if not oracle.tests:
        return []
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="oah-eval-grade-"))
    try:
        snapshot = open_snapshot(load_case(case_path), scratch / "source")
        out = []
        for rule in oracle.tests:
            ws = build_workspace(snapshot, scratch / f"snapshot-{rule.id}")
            passed, timed_out = _run_test(rule, ws.repo, {**env, **variant_environment({}, ws)}, timeout)
            status = "fails" if not passed and not timed_out else (
                "timed out (oracle-invalid)" if timed_out else "passes (oracle-invalid)")
            out.append({"id": rule.id, "snapshot": status})
        return out
    finally:
        remove_tree(scratch)


def _variant_tests(oracle: Oracle, snapshot_tests: list[dict], workspace: pathlib.Path,
                   env: Mapping[str, str], timeout: float, *, case_path: pathlib.Path) -> list[dict]:
    out = []
    for rule, before in zip(oracle.tests, snapshot_tests):
        if before["snapshot"] != "fails":
            out.append({"id": rule.id, "result": "oracle-invalid"})
            continue
        if not workspace.is_dir():
            out.append({"id": rule.id, "result": "unknown"})
            continue
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="oah-eval-grade-"))
        try:
            snapshot = open_snapshot(load_case(case_path), scratch / "source")
            ws = build_workspace(snapshot, scratch / "workspace")
            remove_tree(ws.repo)
            shutil.copytree(workspace, ws.repo, symlinks=True)
            passed, timed_out = _run_test(rule, ws.repo, {**env, **variant_environment({}, ws)}, timeout)
        finally:
            remove_tree(scratch)
        out.append({"id": rule.id, "result": "fixed" if passed else ("timed-out" if timed_out else "not-fixed")})
    return out


def grade_run(run_dir: pathlib.Path, oracle_paths: Iterable[pathlib.Path], *,
              verdicts_path: pathlib.Path | None = None, env: Mapping[str, str] | None = None,
              timeout_seconds: float = DEFAULT_TEST_TIMEOUT) -> dict:
    """Grade every result of a run and write <run_dir>/grade.json."""
    run_dir = pathlib.Path(run_dir)
    env = dict(os.environ if env is None else env)
    run = load_run(run_dir)
    oracles: dict[str, Oracle] = {}
    for path in oracle_paths:
        oracle = load_oracle(pathlib.Path(path))
        if oracle.case in oracles:
            raise EvalError("OAH_EVAL_ORACLE_INVALID", f"two oracle files are for case {oracle.case}; pass one")
        oracles[oracle.case] = oracle
    verdicts = _load_verdicts(pathlib.Path(verdicts_path)) if verdicts_path else {}
    used_verdicts: set[tuple[str, str, str]] = set()
    graded_cases = {}
    for entry in run["cases"]:
        oracle = oracles.get(entry["id"])
        if oracle is None:
            raise EvalError("OAH_EVAL_ORACLE_MISSING", f"no oracle file for case {entry['id']}; pass it with --oracle")
        if is_inside(oracle.path, entry["path"]):
            raise EvalError("OAH_EVAL_ORACLE_IN_CASE",
                            f"{oracle.path} is inside the case folder {entry['path']}, where agents would see it; "
                            "move it outside and preregister again")
        if entry.get("oracle_sha256") and entry["oracle_sha256"] != oracle.sha256:
            raise EvalError("OAH_EVAL_ORACLE_CHANGED",
                            f"{oracle.path} has changed since preregistration (SHA-256 {oracle.sha256}, preregistered "
                            f"{entry['oracle_sha256']}); grade with the preregistered oracle")
        if case_digest(pathlib.Path(entry["path"])) != entry["case_sha256"]:
            raise EvalError("OAH_EVAL_CASE_CHANGED",
                            f"the case folder {entry['path']} changed after the run; restore it before grading")
        snapshot_tests = _snapshot_tests(oracle, pathlib.Path(entry["path"]), env, timeout_seconds)
        variants = {}
        for result_entry in (r for r in run["results"] if r["case"] == entry["id"]):
            result = load_result(run_dir, result_entry)
            name = result_entry["variant"]
            matched = match_findings(oracle.defects, result.get("findings", []))
            matched_ids = {fid for m in matched["matched"] for fid in m["findings"]}
            for item in matched["unmatched"]:
                key = (entry["id"], name, item["id"])
                if key in verdicts:
                    item["verdict"] = verdicts[key]["verdict"]
                    if verdicts[key]["note"]:
                        item["note"] = verdicts[key]["note"]
                    used_verdicts.add(key)
            for key in verdicts:
                if key[:2] == (entry["id"], name) and key[2] in matched_ids:
                    raise EvalError("OAH_EVAL_VERDICT_INVALID",
                                    f"{key[2]} of {name} matched an expected defect; verdicts apply only to "
                                    "unmatched findings, so remove that verdict")
            workspace = run_dir / pathlib.Path(result_entry["path"]).parent / "workspace"
            variants[name] = {**matched, "label": result.get("label"),
                              "tests": _variant_tests(oracle, snapshot_tests, workspace, env, timeout_seconds,
                                                      case_path=pathlib.Path(entry["path"]))}
        graded_cases[entry["id"]] = {"split": entry["split"], "expected": [d.id for d in oracle.defects],
                                     "tests": snapshot_tests, "variants": variants}
    stray = sorted(set(verdicts) - used_verdicts)
    if stray:
        case_id, variant, finding = stray[0]
        raise EvalError("OAH_EVAL_VERDICT_INVALID",
                        f"the verdict for {finding} of {variant} in case {case_id} names no unmatched finding of this "
                        "run; correct or remove it")
    graded = {"format": "oah-eval-grade/1", "run_id": run.get("run_id"), "prereg_sha256": run.get("prereg_sha256"),
              "oracles": {case_id: oracle.sha256 for case_id, oracle in oracles.items()},
              "verdicts_sha256": file_sha256(pathlib.Path(verdicts_path)) if verdicts_path else None,
              "cases": graded_cases}
    try:
        write_json_atomic(run_dir / GRADE_FILE, graded)
    except StorageError as exc:
        raise EvalError("OAH_EVAL_IO", str(exc), EXIT_ENVIRONMENT) from exc
    return graded


def render(graded: Mapping[str, Any]) -> str:
    lines = []
    for case_id, case in graded["cases"].items():
        tests = ", ".join(f"test {t['id']} {t['snapshot']} on the starting snapshot" for t in case["tests"])
        lines.append(f"case {case_id} ({case['split']})" + (f": {tests}" if tests else ""))
        for name, variant in case["variants"].items():
            missed = f" ({', '.join(variant['missed'])})" if variant["missed"] else ""
            fixed = sum(1 for t in variant["tests"] if t["result"] == "fixed")
            lines.append(f"  {name}: matched {len(variant['matched'])} · missed {len(variant['missed'])}{missed} · "
                         f"unmatched {len(variant['unmatched'])} · tests fixed {fixed} of {len(variant['tests'])}")
            for item in variant["unmatched"]:
                lines.append(f"    {item['id']} {item['file'] or '(no file)'}: {item['verdict']} — {item['text']}")
    lines.append("Unmatched findings are not counted as false positives. To record a human verdict, pass "
                 "--verdicts FILE (see docs/eval.md).")
    return "\n".join(lines) + "\n"

