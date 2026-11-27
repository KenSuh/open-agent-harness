# 설정 로더 — .oah/config.toml 을 한 번 읽고, 각 명령이 넘긴 기본값과 대조해 섹션을 검증·병합한다.
from __future__ import annotations

import copy
import datetime as dt
import pathlib
import re
import tomllib
import types
from dataclasses import dataclass
from typing import Any, Mapping

CONFIG_RELATIVE_PATH = pathlib.PurePosixPath(".oah/config.toml")

_TYPE_WORDS = [
    (bool, "true or false"),
    (int, "an integer"),
    (float, "a number"),
    (str, "a string"),
    (dt.datetime, "a date-time"),
    (dt.date, "a date"),
    (dt.time, "a time"),
    ((list, tuple), "an array"),
    (Mapping, "a table"),
]


class ConfigError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return types.MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """A fresh, mutable copy: tables become dicts; arrays from the file become lists."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _copy_default(value: Any) -> Any:
    """A fresh copy of a default that keeps its list or tuple type."""
    if isinstance(value, Mapping):
        return {key: _copy_default(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_copy_default(item) for item in value)
    return copy.deepcopy(value)


def _describe(value: Any) -> str:
    for kind, word in _TYPE_WORDS:
        if isinstance(value, kind):
            return word
    return type(value).__name__


def _same_type(default: Any, value: Any) -> bool:
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    if isinstance(default, (list, tuple)):
        return isinstance(value, (list, tuple))
    return type(value) is type(default)


@dataclass(frozen=True)
class Config:
    path: pathlib.Path | None
    raw: Mapping[str, Any]
    label: str | None = None  # names bytes that came from somewhere else than a file, such as a commit

    @property
    def where(self) -> str:
        """How messages name this configuration."""
        return self._where()

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", _freeze(self.raw))

    def sections(self) -> tuple[str, ...]:
        """The top-level table names present in the file, in file order."""
        return tuple(name for name, value in self.raw.items() if isinstance(value, Mapping))

    def section(self, name: str, defaults: Mapping[str, Any]) -> dict:
        """The file's [name] table merged over `defaults`; a fresh dict every call."""
        if name not in self.raw:
            return _copy_default(defaults)
        given = self.raw[name]
        if not isinstance(given, Mapping):
            raise ConfigError("OAH_CONFIG_INVALID",
                              f"{self._where()}: {name} must be a table, not {_describe(given)}; "
                              f"write it as a [{name}] section")
        return self._merge(defaults, given, name)

    def _where(self) -> str:
        if self.label is not None:
            return self.label
        return str(self.path) if self.path is not None else str(CONFIG_RELATIVE_PATH)

    def _merge(self, defaults: Mapping[str, Any], given: Mapping[str, Any], dotted: str) -> dict:
        result = _copy_default(defaults)
        for key, value in given.items():
            name = f"{dotted}.{key}"
            if key not in defaults:
                if defaults:
                    known = ", ".join(sorted(defaults))
                    raise ConfigError("OAH_CONFIG_UNKNOWN_KEY",
                                      f"{self._where()}: unknown key {name}; "
                                      f"remove it or use one of: {known}")
                result[key] = _thaw(value)
                continue
            default = defaults[key]
            if default is None:
                result[key] = _thaw(value)
            elif isinstance(default, Mapping):
                if not isinstance(value, Mapping):
                    raise self._type_error(name, default, value)
                result[key] = self._merge(default, value, name)
            elif _same_type(default, value):
                result[key] = float(value) if isinstance(default, float) else _thaw(value)
            else:
                raise self._type_error(name, default, value)
        return result

    def _type_error(self, name: str, default: Any, value: Any) -> ConfigError:
        expected = _describe(default)
        return ConfigError("OAH_CONFIG_INVALID",
                           f"{self._where()}: {name} must be {expected}, not {_describe(value)}; "
                           f"change it to {expected} or remove it to use the default")


def parse_config(data: bytes, *, path: pathlib.Path | None = None, label: str | None = None) -> Config:
    """Parse configuration bytes read from `path`, or from the place `label` names (such as a commit)."""
    where = label or str(path or CONFIG_RELATIVE_PATH)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError("OAH_CONFIG_INVALID",
                          f"{where} is not UTF-8 (byte {exc.start}); save it as UTF-8") from exc
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("OAH_CONFIG_INVALID",
                          f"{where} is not valid TOML ({exc}); fix the syntax "
                          "or remove the file to use the defaults") from exc
    return Config(path=path, raw=parsed, label=label)


def load_config(root: pathlib.Path) -> Config:
    """Read <root>/.oah/config.toml; a missing file reads as empty."""
    path = pathlib.Path(root).joinpath(*CONFIG_RELATIVE_PATH.parts)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Config(path=None, raw={})
    except OSError as exc:
        raise ConfigError("OAH_CONFIG_INVALID",
                          f"{path} cannot be read ({exc.strerror or exc}); "
                          "make it a readable file or remove it to use the defaults") from exc
    return parse_config(data, path=path)


# Project tools — Project Wiki, Impact Map and Jev. The Rust core owns this part of the file's format and its
# defaults (crates/oah-core/src/config/tools.rs) and is the only writer; the source CLI reads the same keys here.
# A key the file does not give keeps the default every command already used. One key follows another: without
# [impact] wiki_dirs the Impact Map reads the folder [wiki] dir selects, and a list the file gives is an override
# read as written. Program paths, cache or index folders, host approvals, keys and network consent are per-machine
# state and are never read from this file.
PROJECT_TOOLS_TABLE = "project_tools"
NOT_PORTABLE = "OAH_CONFIG_NOT_PORTABLE"
UNKNOWN_NOTICE = "OAH_CONFIG_UNKNOWN_KEY"
PROJECT_TOOLS_DEFAULTS: Mapping[str, Any] = types.MappingProxyType({
    "wiki.dir": "docs/wiki",
    "impact.wiki_dirs": ("docs/wiki",),
    "impact.graph_file": "graphify-out/graph.json",
    "impact.rerank": False,
    "rerank.enabled": False,
    "rerank.provider": "",
})
_MANAGED_DEFAULTS: Mapping[str, Any] = _freeze({
    "wiki_index": {"managed": False, "exclude": [], "embedding_model": ""},
    "graphify": {"managed": False},
})
_RESERVED_NAMES = frozenset(("CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                             *(f"LPT{n}" for n in range(1, 10))))
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


def portable_problem(text: str, *, glob: bool = False) -> str | None:
    """Why text is not a portable project-relative path (or glob), or None when it is one: relative, written with
    /, without . or .., a drive or a scheme, and usable as a name on Windows as on Unix. One trailing / is accepted."""
    if not text:
        return "it is empty"
    if "\\" in text:
        return "it uses a backslash"
    if text.startswith("/"):
        return "it is an absolute path"
    if text.startswith("~"):
        return "it starts with ~"
    if ":" in text:
        return "it names a drive, a scheme or a stream"
    if any(char < " " or char == "\x7f" for char in text):
        return "it has a control character"
    if any(char in ('<>"|' if glob else '<>"|?*') for char in text):
        return "it has a character Windows does not allow in names"
    for part in text.removesuffix("/").split("/"):
        if not part:
            return "it has an empty name between two /"
        if part in (".", ".."):
            return "it has a . or .. part"
        if part.endswith((".", " ")):
            return "a name in it ends with a dot or a space"
        stem = part.split(".")[0]
        if stem.isascii() and stem.upper() in _RESERVED_NAMES:
            return "it uses a name Windows reserves"
    return None


def model_problem(text: str) -> str | None:
    """Why text is not a model name, or None when it is one or empty. A URL, a host and a file path are refused."""
    if not text:
        return None
    if _MODEL_NAME.fullmatch(text) is None or "//" in text or ".." in text:
        return "it is not a model name"
    return None


@dataclass(frozen=True)
class Setting:
    """A value and whether the file gives it; without the key the value is the default."""
    value: Any
    explicit: bool


@dataclass(frozen=True)
class ProjectTools:
    """The project-tools settings, as the Rust core reads them from the same file."""
    wiki_dir: Setting
    impact_wiki_dirs: Setting
    impact_graph_file: Setting
    impact_rerank: Setting
    jev_enabled: Setting
    jev_provider: Setting
    wiki_index_managed: Setting
    wiki_index_exclude: Setting
    wiki_index_embedding_model: Setting
    graphify_managed: Setting
    notices: tuple[tuple[str, str], ...]  # (code, dotted key): OAH_CONFIG_NOT_PORTABLE, OAH_CONFIG_UNKNOWN_KEY

    def view(self) -> dict:
        """The plain form both implementations are compared in (crates/oah-core/tests/config_samples)."""
        def plain(setting: Setting) -> dict:
            value = list(setting.value) if isinstance(setting.value, tuple) else setting.value
            return {"value": value, "explicit": setting.explicit}

        return {
            "wiki": {"dir": plain(self.wiki_dir)},
            "impact": {"wiki_dirs": plain(self.impact_wiki_dirs), "graph_file": plain(self.impact_graph_file),
                       "rerank": plain(self.impact_rerank)},
            "jev": {"enabled": plain(self.jev_enabled), "provider": plain(self.jev_provider)},
            "managed": {
                "wiki_index": {"managed": plain(self.wiki_index_managed), "exclude": plain(self.wiki_index_exclude),
                               "embedding_model": plain(self.wiki_index_embedding_model)},
                "graphify": {"managed": plain(self.graphify_managed)},
            },
            "notices": sorted([code, key] for code, key in self.notices),
        }


def _selected_wiki_dir(raw: Mapping[str, Any]) -> str | None:
    """The folder [wiki] dir gives, when it gives one the wiki commands accept: a string that is not blank."""
    wiki = raw.get("wiki")
    folder = wiki.get("dir") if isinstance(wiki, Mapping) else None
    return folder if isinstance(folder, str) and folder.strip() else None


def impact_section(config: Config, defaults: Mapping[str, Any]) -> dict:
    """config.section("impact", defaults) as the Impact Map reads it: without [impact] wiki_dirs the list is the
    folder [wiki] dir selects, and the default when the file selects none. A list the file gives is kept as
    written. A [wiki] table the wiki commands would refuse does not stop the map; it then has the default."""
    merged = config.section("impact", defaults)
    selected = _selected_wiki_dir(config.raw)
    if selected is not None and "wiki_dirs" not in config.raw.get("impact", {}):
        merged["wiki_dirs"] = [selected]
    return merged


def project_tools(config: Config) -> ProjectTools:
    """Read the Project Wiki, Impact Map and Jev settings of config. Reads only the keys of the shared contract;
    the other keys of [wiki], [impact] and [rerank] stay with the commands that own them. A wrong type, a table
    written as a value or a blank wiki folder is OAH_CONFIG_INVALID."""
    where = config.where
    notices: list[tuple[str, str]] = []

    def invalid(message: str) -> ConfigError:
        return ConfigError("OAH_CONFIG_INVALID", f"{where}: {message}")

    def table(name: str) -> Mapping[str, Any]:
        given = config.raw.get(name, {})
        if not isinstance(given, Mapping):
            raise invalid(f"{name} must be a table, not {_describe(given)}; write it as a [{name}] section")
        return given

    def strings(dotted: str, items: Any, what: str) -> tuple[str, ...]:
        if not all(isinstance(item, str) for item in items):
            raise invalid(f"{dotted} must list {what} as strings")
        return tuple(items)

    def read(dotted: str, kind: type, what: str = "") -> Setting:
        name, key = dotted.split(".")
        given, default = table(name), PROJECT_TOOLS_DEFAULTS[dotted]
        if key not in given:
            return Setting(default, False)
        value = given[key]
        if kind is tuple:
            if not isinstance(value, (list, tuple)):
                raise config._type_error(dotted, [], value)
            return Setting(strings(dotted, value, what), True)
        if type(value) is not kind:
            raise config._type_error(dotted, default, value)
        return Setting(value, True)

    def note(dotted: str, setting: Setting, *, glob: bool = False) -> None:
        if not setting.explicit:
            return
        items = setting.value if isinstance(setting.value, tuple) else (setting.value,)
        # An empty value names nothing to carry to another machine; it is read as it always was.
        if any(item and portable_problem(item, glob=glob) for item in items):
            notices.append((NOT_PORTABLE, dotted))

    wiki_dir = read("wiki.dir", str)
    if not wiki_dir.value.strip():
        raise invalid("wiki.dir must be a folder path; set it to a folder inside the project or remove it to "
                      "use the default")
    note("wiki.dir", wiki_dir)
    wiki_dirs = read("impact.wiki_dirs", tuple, "folder paths")
    if not wiki_dirs.explicit and wiki_dir.explicit:
        wiki_dirs = Setting((wiki_dir.value,), False)
    graph_file = read("impact.graph_file", str)
    rerank = read("impact.rerank", bool)
    note("impact.wiki_dirs", wiki_dirs)
    note("impact.graph_file", graph_file)
    enabled, provider = read("rerank.enabled", bool), read("rerank.provider", str)

    explicit: set[str] = set()

    def merge(defaults: Mapping[str, Any], given: Mapping[str, Any], dotted: str) -> dict:
        merged = {key: _thaw(value) for key, value in defaults.items()}
        for key, value in given.items():
            name = f"{dotted}.{key}"
            if key not in defaults:
                notices.append((UNKNOWN_NOTICE, name))
            elif isinstance(defaults[key], Mapping):
                if not isinstance(value, Mapping):
                    raise config._type_error(name, defaults[key], value)
                merged[key] = merge(defaults[key], value, name)
            elif not _same_type(defaults[key], value):
                raise config._type_error(name, defaults[key], value)
            else:
                merged[key] = value
                explicit.add(name)
        return merged

    managed = merge(_MANAGED_DEFAULTS, table(PROJECT_TOOLS_TABLE), PROJECT_TOOLS_TABLE)

    def chosen(*path: str) -> Setting:
        dotted = ".".join((PROJECT_TOOLS_TABLE, *path))
        value = managed
        for part in path:
            value = value[part]
        if isinstance(value, (list, tuple)):
            value = strings(dotted, value, "globs")
        return Setting(value, dotted in explicit)

    exclude, model = chosen("wiki_index", "exclude"), chosen("wiki_index", "embedding_model")
    note(f"{PROJECT_TOOLS_TABLE}.wiki_index.exclude", exclude, glob=True)
    if model_problem(model.value):
        notices.append((NOT_PORTABLE, f"{PROJECT_TOOLS_TABLE}.wiki_index.embedding_model"))
    return ProjectTools(
        wiki_dir=wiki_dir, impact_wiki_dirs=wiki_dirs, impact_graph_file=graph_file, impact_rerank=rerank,
        jev_enabled=enabled, jev_provider=provider,
        wiki_index_managed=chosen("wiki_index", "managed"), wiki_index_exclude=exclude,
        wiki_index_embedding_model=model, graphify_managed=chosen("graphify", "managed"),
        notices=tuple(notices))
