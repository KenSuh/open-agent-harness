# 설정 로더 — .oah/config.toml 을 한 번 읽고, 각 명령이 넘긴 기본값과 대조해 섹션을 검증·병합한다.
from __future__ import annotations

import copy
import datetime as dt
import pathlib
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
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError("OAH_CONFIG_INVALID",
                          f"{path} is not UTF-8 (byte {exc.start}); save it as UTF-8") from exc
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("OAH_CONFIG_INVALID",
                          f"{path} is not valid TOML ({exc}); fix the syntax "
                          "or remove the file to use the defaults") from exc
    return Config(path=path, raw=parsed)
