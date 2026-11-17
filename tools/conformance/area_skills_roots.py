# skills-roots 영역: 스킬 폴더 우선순위(default_roots·installed)와 installed_plugins.json 의 플러그인 폴더를 합성 홈·저장소로 사례로 만든다.
"""Area `skills-roots`: skill root precedence and plugin roots from installed_plugins.json."""
from __future__ import annotations

import json
import pathlib

from fixture import Area, Case, Scratch, fn, ok, utf8


def _skill(name: str, description: str, *, field_name: str | None = None) -> bytes:
    title = f"name: {field_name}\n" if field_name else ""
    return utf8(f"---\n{title}description: {description}\n---\n\nBody of {name}.\n")


def _expand(value, scratch: Scratch):
    """Replace the `<base>` placeholder in input values with the scratch folder, in the native spelling."""
    if isinstance(value, str):
        if value == "<base>" or value.startswith("<base>/"):
            return str(scratch.path.joinpath(*value.split("/")[1:]))
        return value
    if isinstance(value, dict):
        return {key: _expand(item, scratch) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, scratch) for item in value]
    return value


def _roots_json(roots, scratch: Scratch) -> list[dict]:
    return [{"path": scratch.rel(root.path), "origin": root.origin, "label": scratch.scrub(root.label),
             "plugin": root.plugin, "only": list(root.only)} for root in roots]


def build(base: pathlib.Path) -> Area:
    from oah import skillsel

    cases: list[Case] = []

    def roots_case(name: str, files: dict[str, bytes], *, env: dict | None = None, kit: str | None = None,
                   cache: bool = False, manifest=None, note: str = "") -> None:
        scratch = Scratch(base)
        try:
            all_files = dict(files)
            if manifest is not None:
                text = manifest if isinstance(manifest, str) else json.dumps(manifest, indent=1)
                all_files["userhome/.claude/plugins/installed_plugins.json"] = utf8(text)
            stored = dict(all_files)
            if manifest is not None and not isinstance(manifest, str):
                expanded = json.dumps(_expand(manifest, scratch), indent=1)
                all_files["userhome/.claude/plugins/installed_plugins.json"] = utf8(expanded)
            scratch.write(all_files)
            (scratch.path / "repo").mkdir(exist_ok=True)
            (scratch.path / "userhome").mkdir(exist_ok=True)
            roots = skillsel.default_roots(scratch.path / "repo", home=scratch.path / "userhome",
                                           env=_expand(env or {}, scratch),
                                           kit_cache=None if kit is None else _expand(kit, scratch),
                                           codex_plugin_cache=cache)
            result = ok(_roots_json(roots, scratch))
        finally:
            scratch.close()
        cases.append(Case(name, result, {"repo": "<base>/repo", "home": "<base>/userhome", "env": env or {}, "kit_cache": kit,
                                         "codex_plugin_cache": cache}, stored,
                          function="oah.skillsel.default_roots", note=note))

    plugins = {"version": 2, "plugins": {
        "superpowers@m": [{"scope": "user", "installPath": "<base>/userhome/.claude/plugins/cache/m/superpowers/2.0"}],
        "local-only@m": [{"scope": "project", "installPath": "<base>/userhome/.claude/plugins/cache/m/local-only/1.0",
                          "projectPath": "<base>/elsewhere"}],
        "mine@m": [{"scope": "local", "installPath": "<base>/userhome/.claude/plugins/cache/m/mine/1.0",
                    "projectPath": "<base>/repo"}],
        "no-scope@m": [{"installPath": "<base>/userhome/.claude/plugins/cache/m/no-scope/1.0"}],
        "broken@m": ["not an object", {"scope": "user"}, {"scope": "user", "installPath": 7}],
        "notalist@m": {"scope": "user", "installPath": "<base>/x"},
        "two@m": [{"scope": "user", "installPath": "<base>/userhome/.claude/plugins/cache/m/two/1.0"},
                  {"scope": "user", "installPath": "<base>/userhome/.claude/plugins/cache/m/two/2.0"}],
        "plain": [{"scope": "user", "installPath": "<base>/userhome/plugins/plain"}],
    }}
    roots_case("default-roots-layout", {}, kit="<base>/kit", manifest=plugins,
               note="installPath and projectPath in the stored manifest use the <base> placeholder; the extractor "
                    "wrote the real folder")
    roots_case("default-roots-no-manifest", {})
    roots_case("default-roots-env-overrides", {}, env={"CLAUDE_CONFIG_DIR": "<base>/userhome/cc", "CODEX_HOME": "<base>/userhome/cx"},
               note="installed_plugins.json is read from CLAUDE_CONFIG_DIR")
    roots_case("default-roots-bad-manifest", {}, manifest="{")
    roots_case("default-roots-manifest-not-object", {}, manifest="[1, 2]")
    roots_case("default-roots-plugins-not-object", {}, manifest='{"plugins": []}')
    cache = {f"userhome/.codex/plugins/cache/{rel}/SKILL.md": _skill(rel, "cached") for rel in (
        "market/helper/1.0/skills/unlazy", "market/helper/0.9/skills/unlazy", "Other/Zeta/1.0/skills/tool",
        "market/deep/a/b/skills/too-deep", "flat/skills/one", "skills/top", ".hidden/x/1.0/skills/no")}
    roots_case("default-roots-codex-cache-on", cache, cache=True,
               note="Codex's plugin cache comes last, sorted case-insensitively; at most six parts deep")
    roots_case("default-roots-codex-cache-off", cache)

    def installed_case(name: str, files: dict[str, bytes], roots: list[dict], note: str = "") -> None:
        scratch = Scratch(base)
        try:
            scratch.write(files)
            objects = [skillsel.SkillRoot(_expand(root["path"], scratch), root["origin"], root["label"],
                                          root.get("plugin", ""), tuple(root.get("only", ()))) for root in roots]
            found = skillsel.installed(objects)

            def skill(item) -> dict:
                return {"id": item.id, "name": item.name, "description": item.description,
                        "path": scratch.rel(item.path), "label": item.label, "origin": item.origin,
                        "digest": item.digest}

            result = ok({"skills": {key: skill(item) for key, item in found.skills.items()},
                         "shadowed": {key: [skill(item) for item in items] for key, items in found.shadowed.items()},
                         "issues": [scratch.scrub(issue) for issue in found.issues]})
        finally:
            scratch.close()
        cases.append(Case(name, result, {"roots": roots}, files, function="oah.skillsel.installed", note=note))

    layout = {
        "repo/.claude/skills/test-driven-development/SKILL.md": _skill("tdd", "project copy of tdd",
                                                                        field_name="test-driven-development"),
        "state/skills-cache/test-driven-development/SKILL.md": _skill("tdd", "kit copy"),
        "userhome/.claude/skills/systematic-debugging/SKILL.md": _skill("debugging", "find root causes"),
        "userhome/.claude/skills/unlazy/SKILL.md": _skill("unlazy", "user claude copy"),
        "userhome/.agents/skills/unlazy/SKILL.md": _skill("unlazy", "gates"),
        "userhome/.agents/skills/Mixed-Case/SKILL.md": _skill("mixed", "upper", field_name="Grill"),
        "userhome/.agents/skills/lower/SKILL.md": _skill("lower", "lower", field_name="grill"),
        "userhome/.agents/skills/broken/SKILL.md": b"---\nname: \xff\n---\n",
        "userhome/.agents/skills/empty-folder/README.md": b"no skill here\n",
        "userhome/.agents/skills/.hidden/SKILL.md": _skill("hidden", "hidden"),
        "userhome/.agents/skills/bom/SKILL.md": b"\xef\xbb\xbf" + _skill("bom", "byte order mark"),
        "plugin/skills/test-driven-development/SKILL.md": _skill("tdd", "plugin tdd",
                                                                 field_name="test-driven-development"),
        "plugin/skills/writing-plans/SKILL.md": _skill("plans", "plugin plans", field_name="writing-plans"),
        "kit2/a/SKILL.md": _skill("a", "only a"), "kit2/b/SKILL.md": _skill("b", "not listed"),
    }
    roots = [
        {"path": "<base>/plugin/skills", "origin": "plugin", "label": "~/plugin/skills", "plugin": "superpowers"},
        {"path": "<base>/userhome/.agents/skills", "origin": "user", "label": "~/.agents/skills"},
        {"path": "<base>/userhome/.claude/skills", "origin": "user", "label": "~/.claude/skills"},
        {"path": "<base>/state/skills-cache", "origin": "kit", "label": "kit cache"},
        {"path": "<base>/kit2", "origin": "kit", "label": "kit two", "only": ["a"]},
        {"path": "<base>/repo/.claude/skills", "origin": "project", "label": ".claude/skills"},
        {"path": "<base>/nowhere", "origin": "user", "label": "~/nowhere"},
    ]
    installed_case("installed-precedence", layout, roots,
                   note="precedence comes from the origin, then list order; ids compare case-insensitively")
    installed_case("installed-list-order-within-origin", layout, [roots[2], roots[1]])

    return Area(
        "skills-roots",
        functions=[fn("oah.skillsel", "default_roots"), fn("oah.skillsel", "_plugin_roots"),
                   fn("oah.skillsel", "_codex_cache_roots"), fn("oah.skillsel", "installed")],
        mined_from=["tests/test_skillsel.py"],
        cases=cases,
        omitted=[
            {"behaviour": "a SKILL.md larger than 1 MiB (OAH_SKILL_UNREADABLE)",
             "reason": "a 1 MiB fixture for one size check"},
            {"behaviour": "labels of roots outside both the home and the repository",
             "reason": "v0.4.0 writes such a label as the native absolute path, which differs by system"},
            {"behaviour": "kit_roots over the shipped toolkit catalog",
             "reason": "it reads oah's state folder and the packaged catalog, not the roots logic"},
        ],
        notes=["Paths are written as <base>/<relative path>, where <base> is the case's folder. The input files "
               "are the files under <base> (repo/, userhome/ and others). In input.json and in the stored "
               "installed_plugins.json, <base> stands for that folder; a runner substitutes its own folder before "
               "the call.",
               "`path` values in the expectation are relative to <base>; labels are as v0.4.0 writes them."],
    )
