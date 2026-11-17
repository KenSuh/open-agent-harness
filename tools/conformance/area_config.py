# config 영역: .oah/config.toml 읽기·병합·검증과 설계 §3.1 의 v0.4 → 1.0 읽기 표를 사례로 만든다.
"""Area `config`: parse and merge `.oah/config.toml` sections over the v0.4.0 defaults."""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from fixture import Area, Case, Scratch, err, fn, ok, utf8

DESIGN_NOTE = ("Expectation is the effective assignment and notice code of the design's legacy reading table "
               "(section 3.1), not a v0.4.0 output.")


def _defaults() -> dict:
    from oah import call, models, skillsel

    return {"stages": call.STAGE_DEFAULTS, "models": models.DEFAULTS, "engines": call.ENGINE_DEFAULTS,
            "skills": skillsel.config_defaults()}


def _run(base: pathlib.Path, data: bytes | None, section: str, defaults) -> dict:
    """load_config(<root>).section(name, defaults), with the file's path written as `.oah/config.toml`."""
    from oah.config import ConfigError, load_config

    scratch = Scratch(base, "<root>")
    try:
        if data is not None:
            scratch.write({".oah/config.toml": data})
        path = str(scratch.path.joinpath(".oah", "config.toml"))
        try:
            result = load_config(scratch.path).section(section, defaults)
        except ConfigError as exc:
            text = str(exc)
            if not text.startswith(exc.code + ": "):
                raise SystemExit(f"config: {exc.code} no longer starts its text with the code")
            # `message` is the ConfigError message argument: str(exc) without the leading "<code>: ".
            return {"error": {"code": exc.code,
                              "message": text[len(exc.code) + 2:].replace(path, ".oah/config.toml")}}
        roundtrip = load_config(scratch.path).section(section, json.loads(json.dumps(defaults)))
        if json.dumps(roundtrip, sort_keys=True) != json.dumps(result, sort_keys=True):
            raise SystemExit(f"config: JSON defaults change the result of section {section}")
        return {"ok": result}
    finally:
        scratch.close()


def build(base: pathlib.Path) -> Area:
    from oah import init

    defaults = _defaults()
    template = init.render_config(branch="main", how="read from the remote's HEAD", jev=False, skill_updates=True,
                                  today=dt.date(2026, 1, 1)).encode("utf-8")
    cases: list[Case] = []

    def case(name: str, data: bytes | None, section: str, *, message: bool = False, note: str = "") -> None:
        result = _run(base, data, section, defaults[section])
        if "error" in result and not message:
            result = err(result["error"]["code"])
        cases.append(Case(name, result, {"section": section, "defaults": defaults[section]},
                          {} if data is None else {".oah/config.toml": data}, function="oah.config.Config.section",
                          note=note))

    case("missing-file-stages", None, "stages", note="no .oah/config.toml: every section is its defaults")
    case("empty-file-models", b"", "models")
    case("missing-section-engines", b"[models]\nprofile = \"efficient\"\n", "engines")
    case("missing-section-skills", b"[stages]\ninitial = \"claude\"\n", "skills")
    case("init-template-stages", template, "stages", note="the file `oah init` writes, rendered by init.render_config")
    case("init-template-models", template, "models")
    case("init-template-engines", template, "engines")
    case("init-template-skills", template, "skills")
    case("crlf-stages", b'[stages]\r\ninitial = "codex"\r\nreview = "claude"\r\n', "stages")
    case("bom-stages", b'\xef\xbb\xbf[stages]\ncorrection = "\xec\xbd\x94\xeb\x8d\xb1\xec\x8a\xa4"\n', "stages",
         note="UTF-8 with a byte order mark")
    case("invalid-utf8", b'[stages]\nreview = "\xc4\xda\xb5\xa6"\n', "stages", note="cp949 bytes")
    case("invalid-toml", b"[stages\ninitial = \n", "stages")
    case("engines-deep-merge", b'[engines.claude]\ncommand = ["claude", "--verbose"]\n'
         b'[engines.claude.tiers.high]\nmodel = "opus"\n[engines.codex.tiers]\nlow = { effort = "medium" }\n', "engines",
         note="a table and an inline table both merge key by key over the defaults")
    case("engines-open-tables", b'[engines.codex.tier_pools]\nhigh = ["model-a", "model-b"]\n'
         b'[engines.codex.effort_caps]\n"model-a" = "high"\n', "engines",
         note="an empty default table accepts any key")
    case("models-int-for-float", b"[models]\nttl_hours = 2\nprofile = \"performance\"\n", "models")
    case("skills-merge", b"[skills]\nauto_update = false\npins = [\"grill\"]\n[skills.discovery]\nmax_queries = 2\n",
         "skills")
    case("type-string-for-int", b'[engines.claude.tiers.low]\ntimeout_min = "15"\n', "engines", message=True)
    case("type-int-for-bool", b"[models]\nfetch_scores = 1\n", "models", message=True)
    case("type-bool-for-float", b"[models]\nttl_hours = true\n", "models", message=True)
    case("type-float-for-int", b"[engines.codex.tiers.max]\ntimeout_min = 90.5\n", "engines", message=True)
    case("type-scalar-for-table", b"[engines]\nclaude = 3\n", "engines", message=True)
    case("type-table-for-array", b"[engines.claude.command]\nx = 1\n", "engines", message=True)
    case("type-section-not-table", b'stages = "claude"\n', "stages", message=True)
    case("unknown-key-stages", b'[stages]\nreveiw = "codex"\n', "stages", message=True)
    case("unknown-key-nested", b'[engines.claude.tiers.low]\nmodle = "x"\n', "engines", message=True)
    case("unknown-key-codex-permission", b'[engines.codex]\npermission_mode = "acceptEdits"\n', "engines",
         message=True, note="v0.4.0 has no permission_mode for codex")

    legacy = [
        ("legacy-only-legacy-keys", b'[stages]\ninitial = "codex"\nreview = "claude"\ncorrection = "codex"\n',
         {"stages": {"plan": "codex", "build": "codex", "review": "claude", "fix": "codex"}, "notices": []},
         "only legacy [stages] keys"),
        ("legacy-new-keys-win", b'[stages]\nplan = "claude"\nbuild = "claude"\nreview = "codex"\nfix = "codex"\n'
         b'initial = "codex"\ncorrection = "claude"\n',
         {"stages": {"plan": "claude", "build": "claude", "review": "codex", "fix": "codex"},
          "notices": ["OAH_STAGES_LEGACY_IGNORED"]},
         "new keys present: they win and the legacy keys are ignored with a notice"),
        ("legacy-engines-read-as-agents", b'[engines.claude]\ncommand = ["claude", "--verbose"]\n'
         b'quality_order = ["opus"]\npermission_mode = "acceptEdits"\ntools = ["Read"]\n'
         b'[engines.claude.tiers]\nhigh = { model = "", effort = "high", timeout_min = 60 }\n',
         {"agents": {"claude": {"command": ["claude", "--verbose"], "quality_order": ["opus"],
                                "tiers": {"high": {"model": "", "effort": "high"}}}},
          "unused_kept_in_file": ["permission_mode", "tiers.high.timeout_min", "tools"], "notices": []},
         "[engines.claude] without [agents.claude] is read as [agents.claude]; timeout_min, permission_mode and "
         "tools stay in the file unused"),
        ("legacy-agents-win", b'[engines.codex]\ncommand = ["codex-old"]\n[agents.codex]\ncommand = ["codex"]\n',
         {"agents": {"codex": {"command": ["codex"]}}, "unused_kept_in_file": [], "notices": []},
         "both present: [agents.codex] wins"),
    ]
    for name, data, effective, note in legacy:
        cases.append(Case(name, ok({**effective, "file_unchanged": True}), {"read": "legacy-to-1.0"},
                          {".oah/config.toml": data}, source="design", function="design:config-legacy-read",
                          note=f"{note}. {DESIGN_NOTE}"))

    return Area(
        "config",
        functions=[fn("oah.config", "load_config"), fn("oah.config", "parse_config"),
                   {"module": "oah.config", "name": "Config.section", "file": "src/oah/config.py"},
                   {"module": "oah.init", "name": "render_config", "file": "src/oah/init.py",
                    "template": "src/oah/templates/init/config.toml"},
                   {"module": "oah.call", "name": "STAGE_DEFAULTS, ENGINE_DEFAULTS", "file": "src/oah/call.py"},
                   {"module": "oah.models", "name": "DEFAULTS", "file": "src/oah/models.py"},
                   {"module": "oah.skillsel", "name": "config_defaults", "file": "src/oah/skillsel.py"}],
        mined_from=["tests/test_config.py"],
        cases=cases,
        omitted=[
            {"behaviour": "a .oah/config.toml path that exists but cannot be read (for example a folder)",
             "reason": "the operating system's error text is part of the message and differs by system"},
            {"behaviour": "messages of OAH_CONFIG_INVALID for invalid UTF-8 and invalid TOML",
             "reason": "they quote the TOML parser's own error text; only the code is pinned"},
            {"behaviour": "Config.raw being read-only and results being isolated copies",
             "reason": "Python object identity, not a file format; 1.0 returns owned values"},
        ],
        deviations=[
            {"case": "unknown-key-stages", "design": "3.1",
             "change": "1.0 reports an unknown key inside a table it reads as the notice OAH_CONFIG_UNKNOWN_KEY "
                       "and keeps reading; v0.4.0 refuses"},
            {"case": "unknown-key-nested", "design": "3.1", "change": "as unknown-key-stages"},
            {"case": "unknown-key-codex-permission", "design": "3.1",
             "change": "as unknown-key-stages; [engines.*] is read as [agents.*] in 1.0"},
        ],
        notes=["input.json `defaults` is the v0.4.0 default table the command passes for `section`.",
               "`error.message` is the ConfigError message: str(exc) without its leading `<code>: `. It names the "
               "file as `.oah/config.toml` (the extractor replaces the temporary path).",
               "Cases with source `design` take `{\"read\": \"legacy-to-1.0\"}` and expect the effective 1.0 "
               "reading: `stages` (section id -> agent), `agents` (only keys the file gives that 1.0 uses), "
               "`unused_kept_in_file` (dotted keys left in the file and not used), `notices` (codes) and "
               "`file_unchanged`."],
    )
