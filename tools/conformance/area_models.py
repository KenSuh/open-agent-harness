# models 영역: 패키지 점수 자료로 만든 점수 집합과 단·프로필별 선택, 빠진 이유, pin>pool>order>rule 우선순위, 후보 띠, Epoch CSV·저장 파일 검증, Codex 캐시·Claude 도움말 해석을 사례로 만든다.
"""Area `models`: score sets, choices, precedence, the candidate band, score files and CLI model lists."""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from fixture import Area, Case, Scratch, err, fn, ok

PROFILES = ("performance", "balanced", "efficient")
CODEX_LISTED = ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna", "gpt-6-astra", "gpt-5.6-terra", "gpt-7-luna",
                "codex-mini"]
CODEX_HIDDEN = ["gpt-6-sol"]
CLAUDE_LISTED = ["haiku", "sonnet", "opus", "fable", "claude-opus-4-8", "default"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def _entry(engine: str, listed: list[str], hidden: list[str] = ()):
    from oah.models import Entry, ModelInfo

    models = [ModelInfo(name, tuple(EFFORTS)) for name in listed]
    models += [ModelInfo(name, tuple(EFFORTS), listed=False) for name in hidden]
    return Entry(engine, "1.0.0", "2026-11-02T00:00:00Z", tuple(models), "fixture")


def _entry_json(listed: list[str], hidden: list[str] = ()) -> dict:
    return {"cli_version": "1.0.0", "checked_at": "2026-11-02T00:00:00Z", "source": "fixture",
            "models": [{"name": name, "efforts": EFFORTS, "default": False, "listed": True} for name in listed]
            + [{"name": name, "efforts": EFFORTS, "default": False, "listed": False} for name in hidden]}


def _set_json(scores) -> dict:
    def item(model) -> dict:
        return {"model": model.model, "family": model.family, "generation": list(model.generation),
                "score": model.score, "cost": model.cost}

    return {"engine": scores.engine, "source": scores.source, "date": scores.date, "credit": scores.credit,
            "models": [item(model) for model in scores.models], "left_out": dict(scores.left_out),
            "retired": [item(model) for model in scores.retired], "failure": scores.failure,
            "stays": dict(scores.stays), "scale": None if scores.scale is None else list(scores.scale),
            "fetched_at": scores.fetched_at}


def _choices(scores) -> dict:
    from oah import scoring
    from oah.engines.effort import EngineError
    from oah.routing import TIERS

    table = {}
    for tier in TIERS:
        for floor in TIERS:
            for profile in PROFILES:
                try:
                    choice = scoring.choose(scores, tier, floor, profile)
                except EngineError as exc:
                    table[f"{tier}/{floor}/{profile}"] = {"error": exc.code}
                    continue
                table[f"{tier}/{floor}/{profile}"] = {
                    "model": choice.model, "step": choice.step, "floor_step": choice.floor_step,
                    "required": choice.required, "lowest_step": choice.lowest_step, "source": choice.source,
                    "band": list(scoring.band(scores, choice))}
    return table


def build(base: pathlib.Path) -> Area:
    from importlib import resources

    from oah import benchmarks, models, scoring
    from oah.engines import claude, codex
    from oah.engines.effort import EngineError
    from oah.routing import TIERS

    packaged = {name: resources.files("oah").joinpath(f"templates/engines/{name}").read_bytes()
                for name in ("benchmarks.json", "families.json")}
    cases: list[Case] = []

    def score_case(name: str, engine: str, listed: list[str], hidden: list[str] = (), allowed: list[str] = (),
                   note: str = "") -> None:
        try:
            scores = scoring.score_set(engine, _entry(engine, listed, hidden), allowed=list(allowed))
            result = ok({"score_set": _set_json(scores), "choices": _choices(scores)})
        except EngineError as exc:
            result = err(exc.code)
        cases.append(Case(name, result, {"engine": engine, "entry": _entry_json(listed, hidden),
                                         "allowed": list(allowed), "data": "benchmarks.json",
                                         "families": "families.json"},
                          dict(packaged), function="oah.scoring.score_set", note=note))

    score_case("packaged-codex", "codex", CODEX_LISTED, CODEX_HIDDEN,
               note="every left-out reason: superseded, unscored (with stays), unknown-family; a hidden model")
    score_case("packaged-codex-allowed", "codex", CODEX_LISTED, CODEX_HIDDEN, ["gpt-6-luna", "gpt-6.1-terra"],
               note="allowed_models narrows the picks (not-allowed) and floors above it go unmet")
    score_case("packaged-claude", "claude", CLAUDE_LISTED)
    score_case("packaged-claude-allowed", "claude", CLAUDE_LISTED, allowed=["haiku", "sonnet"])
    score_case("packaged-codex-none-listed", "codex", ["codex-mini"])
    score_case("packaged-claude-nothing-allowed", "claude", CLAUDE_LISTED, allowed=["claude-opus-4-8"])

    # Precedence: pin > pool > quality_order > the score rule, per tier.
    def tier_case(name: str, engine: str, *, pins: dict | None = None, quality_order: list[str] = (),
                  pools: dict | None = None, allowed: list[str] = (), listed: list[str] | None = None,
                  effort_ok: list[str] | None = None, floor: str = "low", profile: str = "balanced",
                  catalog: bool = True, scores: bool = True, note: str = "") -> None:
        names = listed if listed is not None else (CODEX_LISTED if engine == "codex" else CLAUDE_LISTED)
        entry = _entry(engine, names) if catalog else None
        score_set = scoring.score_set(engine, entry, allowed=list(allowed)) if scores and catalog else None
        rules = None if effort_ok is None else (lambda model: model in effort_ok)
        table = {}
        for tier in TIERS:
            try:
                model, source = models.map_tier(engine, tier, pin=(pins or {}).get(tier, ""),
                                                quality_order=list(quality_order), entry=entry, scores=score_set,
                                                floor_tier=floor, profile=profile, pools=pools,
                                                allowed=list(allowed), effort_rules=rules)
                table[tier] = {"model": model, "source": source}
            except EngineError as exc:
                table[tier] = {"error": exc.code}
        cases.append(Case(name, ok(table), {
            "engine": engine, "pins": pins or {}, "quality_order": list(quality_order), "pools": pools or {},
            "allowed": list(allowed), "entry": _entry_json(names) if catalog else None, "effort_ok": effort_ok,
            "floor_tier": floor, "profile": profile, "scores": None if score_set is None else _set_json(score_set)},
            function="oah.models.map_tier", note=note))

    tier_case("precedence-pin-wins", "codex", pins={"high": "my-own-model"}, quality_order=["gpt-6-luna"],
              pools={"high": ["gpt-6.1-sol"]}, note="a pin needs no catalog entry and beats a pool and an order")
    tier_case("precedence-pool-over-order", "codex", pools={"high": ["gpt-9-absent", "gpt-6-astra", "gpt-6.1-sol"],
                                                            "low": ["gpt-6-luna"]},
              quality_order=["gpt-6.1-terra", "gpt-6-luna"])
    tier_case("precedence-pool-not-listed", "codex", pools={"max": ["gpt-9-absent"]})
    tier_case("precedence-pool-not-allowed", "codex", pools={"max": ["gpt-6-astra"]}, allowed=["gpt-6-luna"])
    tier_case("precedence-pool-not-runnable", "codex", pools={"medium": ["gpt-6-astra"]},
              effort_ok=["gpt-6-luna", "gpt-6.1-sol"])
    tier_case("precedence-order-by-tier", "codex", quality_order=["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"],
              note="low takes the last, medium the second, high and max the first")
    tier_case("precedence-order-one-model", "codex", quality_order=["gpt-6.1-terra", "gpt-9-absent"])
    tier_case("precedence-order-filtered", "codex", quality_order=["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"],
              allowed=["gpt-6-luna", "gpt-6.1-sol"], effort_ok=["gpt-6.1-sol"])
    tier_case("precedence-order-not-runnable", "codex", quality_order=["gpt-6-astra"], effort_ok=[])
    tier_case("precedence-claude-old-order-ignored", "claude", quality_order=["opus", "sonnet", "haiku"],
              note="the old template default is no user ranking, so the rule decides")
    tier_case("precedence-rule", "claude")
    tier_case("precedence-rule-floor-performance", "codex", floor="high", profile="performance")
    tier_case("precedence-rule-efficient", "codex", profile="efficient")
    tier_case("precedence-rule-effort-rules", "codex", effort_ok=["gpt-6-luna", "gpt-6.1-terra"])
    tier_case("precedence-no-catalog", "codex", pins={"max": "my-own-model"}, catalog=False)
    tier_case("precedence-no-scores", "codex", scores=False)

    # Epoch's CSV.
    head = "model,started_at,Status,mean_score,original_task_name,extra\n"
    rows = ("gpt-6-sol_max,2026-10-01T00:00:00Z,Success,0.5,Chess Puzzles,a\n"
            "gpt-6-sol_max,2026-10-02T00:00:00Z,Success,0.6,Chess Puzzles,b\n"
            "gpt-6-sol_max,2026-10-03T00:00:00Z,Failed,0.9,Chess Puzzles,c\n"
            "gpt-6-sol_max,2026-09-30T00:00:00+00:00,Success,0.4,Chess Puzzles,d\n"
            "gpt-6-sol_max,2026-10-01T00:00:00,Success,0.7,EBR-bench,e\n"
            "gpt-6-sol,2026-10-01T00:00:00Z,Success,0.8,EBR-bench,not max\n"
            "claude-opus-5-5_max,2026-10-01T00:00:00Z,Success,1,Mystery Game Puzzles,f\n"
            "claude-opus-5-5_max,2026-10-01T00:00:00Z,Success,nan,Furniture Assembly,g\n"
            "claude-opus-5-5_max,2026-10-01T00:00:00Z,Success,0.3,Other Task,h\n"
            "other-model_max,2026-10-01T00:00:00Z,Success,0.3,Chess Puzzles,i\n")
    csvs = {
        "basic": (head + rows).encode(),
        "crlf-multiline-field": (head + "gpt-6-luna_max,2026-10-01T00:00:00Z,Success,0.25,EBR-bench,\"two\r\nlines\"\r\n"
                                 ).replace("\n", "\r\n").replace("\r\r\n", "\r\n").encode(),
        "missing-column": b"model,started_at,mean_score,original_task_name\ngpt-6-sol_max,2026-10-01T00:00:00Z,0.5,"
                          b"Chess Puzzles\n",
        "score-out-of-range": (head + "gpt-6-sol_max,2026-10-01T00:00:00Z,Success,1.5,Chess Puzzles,x\n").encode(),
        "unreadable-time": (head + "gpt-6-sol_max,yesterday,Success,0.5,Chess Puzzles,x\n").encode(),
        "no-allowed-rows": (head + "gpt-6-sol_max,2026-10-01T00:00:00Z,Success,0.5,Other Task,x\n").encode(),
        "not-utf8": head.encode() + b"gpt-6-sol_max,2026-10-01T00:00:00Z,Success,0.5,Chess \xff,x\n",
    }
    for key, data in csvs.items():
        try:
            result = ok(benchmarks.epoch_scores(data))
        except benchmarks.DataError:
            result = err(benchmarks.REFRESH_FAILED)
        cases.append(Case(f"epoch-csv-{key}", result, files={"benchmarks.csv": data},
                          function="oah.benchmarks.epoch_scores"))

    # Stored score files.
    scores = {"gpt-6-sol": {"Chess Puzzles": 0.5, "EBR-bench": 0.25}, "claude-opus-5-5": {"Chess Puzzles": 0.75}}
    epoch_ok = {**benchmarks.epoch_snapshot(scores, dt.date(2026, 11, 2)), "fetched_at": "2026-11-02T10:00:00Z",
                "source_url": benchmarks.EPOCH_URL}
    aa_ok = {"schema": 1, "source": benchmarks.AA_SOURCE, "date": "2026-11-02", "url": benchmarks.AA_SITE,
             "credit": benchmarks.AA_CREDIT, "min_tasks": 1, "source_url": benchmarks.AA_URL,
             "fetched_at": "2026-11-02T10:00:00Z", "scores": {"gpt-6-sol": {"Coding Index": 0.5},
                                                             "claude-opus-5-5": {"Coding Index": 0.6}}}
    stored = {
        "epoch-valid": (benchmarks.EPOCH, json.dumps(epoch_ok).encode()),
        "epoch-absent": (benchmarks.EPOCH, None),
        "epoch-not-json": (benchmarks.EPOCH, b"{"),
        "epoch-nan-constant": (benchmarks.EPOCH, json.dumps(epoch_ok).replace("0.25", "NaN").encode()),
        "epoch-missing-field": (benchmarks.EPOCH, json.dumps({k: v for k, v in epoch_ok.items()
                                                              if k != "source_url"}).encode()),
        "epoch-wrong-license": (benchmarks.EPOCH, json.dumps({**epoch_ok, "license": "MIT"}).encode()),
        "epoch-bad-fetch-time": (benchmarks.EPOCH, json.dumps({**epoch_ok, "fetched_at": "2026-11-02"}).encode()),
        "epoch-date-not-fetch-date": (benchmarks.EPOCH, json.dumps({**epoch_ok, "date": "2026-11-01"}).encode()),
        "epoch-bad-credit": (benchmarks.EPOCH, json.dumps({**epoch_ok, "credit": "Epoch AI"}).encode()),
        "epoch-score-out-of-range": (benchmarks.EPOCH, json.dumps(
            {**epoch_ok, "scores": {"gpt-6-sol": {"Chess Puzzles": 1.5}}}).encode()),
        "epoch-unknown-task": (benchmarks.EPOCH, json.dumps(
            {**epoch_ok, "scores": {"gpt-6-sol": {"Coding Index": 0.5}}}).encode()),
        "epoch-bad-model-name": (benchmarks.EPOCH, json.dumps(
            {**epoch_ok, "scores": {"GPT-6": {"Chess Puzzles": 0.5}}}).encode()),
        "aa-valid": (benchmarks.AA, json.dumps(aa_ok).encode()),
        "aa-two-indexes-one-engine": (benchmarks.AA, json.dumps(
            {**aa_ok, "scores": {"gpt-6-sol": {"Coding Index": 0.5},
                                 "gpt-6-luna": {"Intelligence Index": 0.2}}}).encode()),
    }
    rows, results = {}, {}
    for key, (source, raw) in stored.items():
        data, problem = benchmarks.read_stored(source, raw)
        rows[key] = {"source": source, "raw": None if raw is None else raw.decode("utf-8")}
        results[key] = {"data": data, "problem": problem}
    cases.append(Case("stored-files", ok(results), {"calls": rows}, function="oah.benchmarks.read_stored",
                      note="a table: each call passes `raw` (the stored file's UTF-8 text, or null for no file)"))

    # Codex models_cache.json and Claude --help.
    cache = {"models": [
        {"slug": "gpt-6.1-sol", "is_default": True, "supported_reasoning_levels": [
            {"effort": "low"}, {"effort": "medium"}, {"effort": "high"}, {"effort": "xhigh"}, {"effort": "low"}]},
        {"slug": "gpt-6-luna", "visibility": "list", "supported_reasoning_levels": ["low", "BAD", 3]},
        {"slug": "gpt-6-astra", "visibility": "hide", "supported_reasoning_levels": ["max"]},
        {"id": "gpt-6-sol", "hidden": True, "default": True},
        {"model": "gpt-5.6-terra"}, {"slug": "gpt-6.1-sol", "supported_reasoning_levels": ["low"]},
        {"slug": ""}, "not an object", {"slug": 7}]}
    caches = {
        "models-object": json.dumps(cache, indent=1).encode(),
        "top-level-list": json.dumps(cache["models"][:2]).encode(),
        "bom": b"\xef\xbb\xbf" + json.dumps({"models": cache["models"][:1]}).encode(),
        "unknown-shape": b'{"data": []}',
        "not-json": b"{",
        "absent": None,
    }
    rows, results = {}, {}
    for key, raw in caches.items():
        scratch = Scratch(base)
        try:
            if raw is not None:
                scratch.write({codex.CACHE_NAME: raw})
            parsed = codex.read_models_cache(scratch.path)
        finally:
            scratch.close()
        rows[key] = {"models_cache.json": None if raw is None else raw.decode("utf-8")}
        results[key] = None if parsed is None else {slug: {"slug": item.slug, "levels": list(item.levels),
                                                           "default": item.default, "listed": item.listed}
                                                    for slug, item in parsed.items()}
    cases.append(Case("codex-cache", ok(results), {"calls": rows}, function="oah.engines.codex.read_models_cache",
                      note="a table: each call writes the given text (UTF-8, null for no file) as models_cache.json "
                           "in an empty CODEX_HOME and reads it"))

    help_text = ("Usage: claude [options] [command] [prompt]\n\nOptions:\n"
                 "  --effort <level>                      Effort level for the current session\n"
                 "                                        ({efforts})\n"
                 "  --model <model>                       Model for the current session. Provide\n"
                 "                                        an alias for the latest model (e.g.\n"
                 "                                        {aliases}) or a\n"
                 "                                        model's full name.\n"
                 "  --permission-mode <mode>              Permission mode to use for the session\n")
    helps = {
        "default": help_text.format(efforts="low, medium, high, xhigh, max", aliases="'fable', 'opus', 'sonnet'"),
        "crlf-other-lists": help_text.format(efforts="low, high, Bad Label, low",
                                             aliases="'opus', 'haiku', 'opus'").replace("\n", "\r\n"),
        "changed": "Usage: claude (help text changed)\n",
        "options-later": "  --model <model>  pick 'sonnet'\n  --verbose 'not-an-alias'\n  --effort  (low)\n",
    }
    results = {}
    for key, text in helps.items():
        info = claude.parse_help(text)
        results[key] = {"aliases": list(info.aliases), "efforts": list(info.efforts)}
    cases.append(Case("claude-help", ok(results), {"calls": {key: {"text": text} for key, text in helps.items()}},
                      function="oah.engines.claude.parse_help", note="a table of parse_help calls"))

    return Area(
        "models",
        functions=[fn("oah.scoring", "score_set"), fn("oah.scoring", "choose"), fn("oah.scoring", "band"),
                   fn("oah.scoring", "families"), fn("oah.scoring", "bundled"), fn("oah.models", "map_tier"),
                   fn("oah.benchmarks", "epoch_scores"), fn("oah.benchmarks", "read_stored"),
                   fn("oah.engines.codex", "read_models_cache"), fn("oah.engines.claude", "parse_help")],
        mined_from=["tests/test_scoring.py", "tests/test_models.py", "tests/test_benchmarks.py",
                    "tests/test_engines_claude.py"],
        cases=cases,
        omitted=[
            {"behaviour": "catalog rebuilds, probes of the installed CLIs and score refreshes over the network",
             "reason": "they spawn the agent CLIs or fetch from epoch.ai and artificialanalysis.ai; the parsers and "
                       "the stored-file check they feed are pinned"},
            {"behaviour": "the Artificial Analysis response parser and the API key lookup",
             "reason": "the key comes from the user's keychain; stored AA files are pinned through read_stored"},
            {"behaviour": "runnable() effort resolution per model",
             "reason": "not in the area's listed behaviour; map_tier cases pass the effort rules as a list"},
        ],
        notes=["packaged-* cases: the input files benchmarks.json and families.json are the files v0.4.0 ships in "
               "src/oah/templates/engines; score_set reads them. `choices` keys are tier/floor/profile; each value is "
               "the Choice with the candidate band, or {\"error\": code}. Scores are IEEE doubles as Python prints "
               "them; compare with a tolerance of 1e-9.",
               "precedence-* cases: map_tier for every tier. `effort_ok` lists the models the effort rules allow "
               "(null: no rules). `pins` maps a tier to its pinned model. `scores` null means no score set.",
               "epoch-csv-* error cases: epoch_scores raises DataError, which has no code; its only caller "
               "(fetch_epoch) turns it into a failed refresh, reported as OAH_SCORES_REFRESH_FAILED, so that is the "
               "code given.",
               "Table cases give `calls` (row name -> arguments) and expect a map of row name -> result."],
    )
