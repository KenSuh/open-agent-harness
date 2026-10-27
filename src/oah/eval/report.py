# 평가 보고서 — 분할·변형별로 개수·재작업·시간·사용량(없으면 unknown)을 모으고 한계를 적으며, 우열 주장은 하지 않는다.
from __future__ import annotations

import json
import pathlib
from typing import Any, Mapping

from oah.atomic import StateCorrupt, StorageError, read_json
from oah.eval.errors import EvalError
from oah.eval.grade import GRADE_FILE, NEEDS_HUMAN, load_result, load_run
from oah.eval.runner import CONTROL_FLOW_ONLY, UNKNOWN

_REWORK = ("rounds", "corrections")


def _sum_known(values: list[Any]) -> Any:
    known = [v for v in values if v != UNKNOWN]
    if not known:
        return UNKNOWN
    if len(known) < len(values):
        return f"unknown ({len(known)} of {len(values)} recorded)"
    return sum(known)


def _sum_usage(values: list[Any]) -> Any:
    known = [v for v in values if isinstance(v, dict)]
    if not known:
        return UNKNOWN
    keys = {"input_tokens", "output_tokens", "cost_usd"} | {key for usage in known for key in usage}
    return {key: _sum_known([value.get(key, UNKNOWN) if isinstance(value, dict) else UNKNOWN
                            for value in values]) for key in sorted(keys)}


def build_report(run_dir: pathlib.Path) -> dict:
    run_dir = pathlib.Path(run_dir)
    run = load_run(run_dir)
    try:
        graded = read_json(run_dir / GRADE_FILE, None)
    except (StateCorrupt, StorageError) as exc:
        raise EvalError("OAH_EVAL_RUN_INVALID", str(exc)) from exc
    if not isinstance(graded, dict):
        raise EvalError("OAH_EVAL_NOT_GRADED", f"{run_dir} has not been graded; run oah eval grade {run_dir} "
                                               "--oracle FILE first")
    groups: dict[tuple[str, str], list[tuple[dict, dict, dict]]] = {}
    for entry in run["results"]:
        result = load_result(run_dir, entry)
        case = graded["cases"].get(entry["case"])
        if case is None or entry["variant"] not in case["variants"]:
            raise EvalError("OAH_EVAL_NOT_GRADED", f"{entry['case']}/{entry['variant']} has no grade; run oah eval "
                                                   "grade again")
        groups.setdefault((result["split"], result["variant"]), []).append(
            (result, case, case["variants"][entry["variant"]]))
    rows = []
    for (split, variant), items in sorted(groups.items()):
        unmatched = [u for _, _, g in items for u in g["unmatched"]]
        tests = [t for _, _, g in items for t in g["tests"]]
        rows.append({
            "split": split, "variant": variant, "cases": len(items),
            "control_flow_only": all(r["label"] == CONTROL_FLOW_ONLY for r, _, _ in items),
            "expected": sum(len(c["expected"]) for _, c, _ in items),
            "matched": sum(len(g["matched"]) for _, _, g in items),
            "missed": sum(len(g["missed"]) for _, _, g in items),
            "unmatched": len(unmatched),
            "awaiting_human": sum(1 for u in unmatched if u["verdict"] == NEEDS_HUMAN),
            "human_false_positive": sum(1 for u in unmatched if u["verdict"] == "false-positive"),
            "human_other": sum(1 for u in unmatched if u["verdict"] not in (NEEDS_HUMAN, "false-positive")),
            "tests_fixed": sum(1 for t in tests if t["result"] == "fixed"),
            "tests_total": len(tests),
            "tests_oracle_invalid": sum(1 for t in tests if t["result"] == "oracle-invalid"),
            "nonzero_exits": sum(1 for r, _, _ in items if r["exit_code"] not in (0,)),
            "timeouts": sum(1 for r, _, _ in items if r["timed_out"]),
            "wall_seconds": round(sum(r["wall_seconds"] for r, _, _ in items), 3),
            "rounds": _sum_known([r["rounds"] for r, _, _ in items]),
            "corrections": _sum_known([r["corrections"] for r, _, _ in items]),
            "usage": _sum_usage([r["usage"] for r, _, _ in items]),
        })
    return {"run_id": run.get("run_id"), "prereg_sha256": run.get("prereg_sha256"), "rows": rows,
            "limits": _limits(run, rows)}


def _limits(run: Mapping[str, Any], rows: list[dict]) -> list[str]:
    splits = [case["split"] for case in run["cases"]]
    limits = [f"cases run: {len(splits)} (" + ", ".join(f"{s} {splits.count(s)}" for s in ("dev", "control", "holdout"))
              + ")"]
    fake = sorted({row["variant"] for row in rows if row["control_flow_only"]})
    if fake:
        limits.append(f"control-flow-only variants: {', '.join(fake)}; fake engines check the loop's control flow, "
                      "not whether real defects are found")
    unknown: dict[str, list[str]] = {}
    for row in rows:
        metrics = {metric: row[metric] for metric in ("usage", *_REWORK)}
        if isinstance(row["usage"], dict):
            metrics.update({f"usage.{key}": value for key, value in row["usage"].items()})
        for metric, value in metrics.items():
            if not isinstance(value, (int, float, dict)) and metric not in unknown.setdefault(row["variant"], []):
                unknown[row["variant"]].append(metric)
    listed = "; ".join(f"{variant}: {', '.join(metrics)}" for variant, metrics in sorted(unknown.items()) if metrics)
    limits.append(f"unknown metrics: {listed or 'none'}")
    waiting = sum(row["awaiting_human"] for row in rows)
    limits.append(f"findings awaiting human judgement: {waiting}; they are not counted as false positives")
    if not run.get("prereg_sha256"):
        limits.append("no preregistration: this run is exploratory, and holdout cases could not run")
    limits.append("the isolation scan checks what each workspace contains; it is not a sandbox, so a variant that "
                  "reads other paths on this machine is not stopped")
    limits.append("these numbers describe these cases and variants only; they are not a general ranking")
    return limits


def _value(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}={value[key]}" for key in sorted(value))
    return str(value)


def render(report: Mapping[str, Any]) -> str:
    prereg = report["prereg_sha256"]
    lines = [f"oah eval report: run {report['run_id']} · preregistration "
             f"{prereg[:12] if prereg else 'none (exploratory)'}"]
    for row in report["rows"]:
        tag = f" [{CONTROL_FLOW_ONLY}]" if row["control_flow_only"] else ""
        lines += [
            f"split={row['split']} variant={row['variant']}{tag}",
            f"  cases {row['cases']} · expected defects {row['expected']} · matched {row['matched']} · "
            f"missed {row['missed']}",
            f"  unmatched findings {row['unmatched']} ({row['awaiting_human']} awaiting human judgement, "
            f"{row['human_false_positive']} marked false positive by a human, {row['human_other']} other human "
            "verdicts)",
            f"  tests fixed {row['tests_fixed']} of {row['tests_total']} (oracle-invalid {row['tests_oracle_invalid']})"
            f" · non-zero exits {row['nonzero_exits']} · timeouts {row['timeouts']}",
            f"  rework: rounds {_value(row['rounds'])} · corrections {_value(row['corrections'])}",
            f"  wall time {row['wall_seconds']} s · usage {_value(row['usage'])}",
        ]
    lines.append("Limits:")
    lines += [f"  - {limit}" for limit in report["limits"]]
    return "\n".join(lines) + "\n"


def dumps(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
