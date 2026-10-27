# 툴킷 허용 목록·출처 기록 — 키트와 함께 배포되는 upstreams.json 과 각 번들의 UPSTREAM.json 을 읽고 검증한다.
"""The kit's allowlist of upstreams (A15) and the provenance record of each bundled item.

`src/oah/templates/toolkit/upstreams.json` ships with the kit. Only the repositories it names are ever
fetched. A bundled item lives in `skills/<name>/` with an `UPSTREAM.json` that records where every file
came from; its `commit` is the pin. Its `mcp` list names the npm package and version each `mcp/`
template pins; the update check only reports newer versions of those, it never edits a pin.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Mapping

import oah
from oah.toolkit.skillmeta import UPSTREAM_FILE

CATALOG_PATH = pathlib.Path(oah.__file__).resolve().parent / "templates" / "toolkit" / "upstreams.json"
KIT_ENV = "OAH_KIT_DIR"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
KINDS = ("skill", "plugin")
NPM_PACKAGE_RE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
PIN_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class CatalogError(Exception):
    code = "OAH_TOOLKIT_CATALOG_INVALID"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class Bundled:
    name: str
    kind: str
    repository: str
    subpath: str
    ref: str
    commit: str
    spdx: str
    license_path: str
    notice_paths: tuple[str, ...]
    skills: tuple[str, ...]
    purpose: str
    official_install: str
    include: tuple[str, ...] = ()        # folders or files under subpath to vendor; empty = all of it
    default: bool = True                 # False: --all skips it, and the selector never picks it by default
    select_for: tuple[str, ...] = ()     # task tags the selector may pick it for, e.g. "korean-prose"
    support: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # skill -> item folders copied into it
    regional: str = ""                   # a regional item's language, e.g. "Korean"; shown as "regional: Korean"

    def fetch_paths(self) -> tuple[str, ...]:
        """Upstream paths a fetch needs: the vendored part, the license and the notices."""
        prefix = self.subpath.rstrip("/") + "/" if self.subpath else ""
        vendored = tuple(prefix + path for path in self.include) if self.include else (self.subpath,)
        return tuple(p for p in (*vendored, self.license_path, *self.notice_paths) if p)


@dataclass(frozen=True)
class Step:
    run: tuple[str, ...] | None = None
    manual: str | None = None

    def describe(self) -> str:
        return " ".join(self.run) if self.run is not None else f"(type in the tool) {self.manual}"


@dataclass(frozen=True)
class Linked:
    name: str
    kind: str
    source: str
    version: str
    license: str
    steps: tuple[Step, ...]
    reason: str
    purpose: str


@dataclass(frozen=True)
class Cli:
    """A recommended command-line tool; `oah doctor` checks these and prints the install command."""

    name: str
    command: str
    source: str
    version: str
    license: str
    install_macos: str  # from the inventory; Windows commands come from oah doctor's own table
    purpose: str


@dataclass(frozen=True)
class Mcp:
    """An npm-launched MCP server template in mcp/; the version is the pin oah reports on, never edits."""

    name: str
    package: str
    version: str
    templates: tuple[str, ...]  # mcp/claude/<name>.json and mcp/codex/<name>.toml
    source: str
    license: str
    purpose: str

    @property
    def spec(self) -> str:
        return f"{self.package}@{self.version}"


@dataclass(frozen=True)
class Catalog:
    bundled: tuple[Bundled, ...]
    linked: tuple[Linked, ...]
    clis: tuple[Cli, ...] = ()
    mcp: tuple[Mcp, ...] = ()

    def find(self, name: str) -> Bundled | Linked | None:
        for entry in (*self.bundled, *self.linked):
            if entry.name == name:
                return entry
        return None


@dataclass(frozen=True)
class Upstream:
    """The UPSTREAM.json of one bundled item."""

    name: str
    repository: str
    subpath: str
    ref: str
    commit: str
    spdx: str
    license_path: str
    license_file: str
    license_sha256: str
    notice_files: tuple[str, ...]
    executables: tuple[str, ...]
    files: Mapping[str, str] = field(default_factory=dict)
    fetched: str = ""
    include: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"name": self.name, "repository": self.repository, "subpath": self.subpath, "ref": self.ref,
                "commit": self.commit, "spdx": self.spdx, "license_path": self.license_path,
                "license_file": self.license_file, "license_sha256": self.license_sha256,
                "notice_files": list(self.notice_files), "executables": list(self.executables),
                "files": dict(sorted(self.files.items())), "fetched": self.fetched, "include": list(self.include)}

    @classmethod
    def from_json(cls, data: object, where: str) -> "Upstream":
        if not isinstance(data, dict):
            raise CatalogError(f"{where}: expected a JSON object")
        try:
            record = cls(name=_str(data, "name", where), repository=_str(data, "repository", where),
                         subpath=_str(data, "subpath", where, empty=True), ref=_str(data, "ref", where),
                         commit=_str(data, "commit", where), spdx=_str(data, "spdx", where),
                         license_path=_str(data, "license_path", where),
                         license_file=_str(data, "license_file", where),
                         license_sha256=_str(data, "license_sha256", where),
                         notice_files=_strings(data, "notice_files", where),
                         executables=_strings(data, "executables", where),
                         files=_file_map(data.get("files"), where), fetched=_str(data, "fetched", where),
                         include=_strings(data, "include", where))
        except KeyError as exc:
            raise CatalogError(f"{where}: missing {exc}") from None
        if not COMMIT_RE.fullmatch(record.commit):
            raise CatalogError(f"{where}: commit must be a full 40-character commit id")
        for name in (record.license_file, *record.notice_files, *record.executables, *record.files):
            _relative(name, where)
        return record


def _str(data: Mapping, key: str, where: str, *, empty: bool = False) -> str:
    value = data[key]
    if not isinstance(value, str) or (not empty and not value):
        raise CatalogError(f"{where}: {key} must be a non-empty string")
    return value


def _strings(data: Mapping, key: str, where: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise CatalogError(f"{where}: {key} must be a list of strings")
    return tuple(value)


def _file_map(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(blob, str) and COMMIT_RE.fullmatch(blob)
            for key, blob in value.items()):
        raise CatalogError(f"{where}: files must map each path to its git blob id")
    return dict(value)


def _relative(path: str, where: str, *, allow_dot: bool = False) -> str:
    """A forward-slash path inside the item: no root, drive, backslash or '..'."""
    if allow_dot and path == ".":
        return path
    if (not path or "\\" in path or ":" in path or pathlib.PurePosixPath(path).is_absolute()
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise CatalogError(f"{where}: {path!r} must be a relative path inside the item")
    return path


def validate_repository(url: str, where: str = "repository") -> str:
    """Only public sources: an https URL with no user info, query or fragment, or a local folder."""
    if not isinstance(url, str) or not url:
        raise CatalogError(f"{where}: must be a non-empty string")
    if "://" not in url:
        if pathlib.Path(url).is_absolute():
            return url
        raise CatalogError(f"{where}: {url!r} must be an https URL or an absolute local folder")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "file" and not parts.netloc and not parts.query and not parts.fragment:
        return url
    if (parts.scheme != "https" or not parts.hostname or "@" in parts.netloc or parts.query
            or parts.fragment or parts.port is not None):
        raise CatalogError(f"{where}: {url!r} must be a plain https URL with no user name, token, "
                           "query or fragment; credentials never belong in the allowlist")
    return url


def _bundled(data: object, where: str) -> Bundled:
    if not isinstance(data, dict):
        raise CatalogError(f"{where}: expected an object")
    try:
        entry = Bundled(name=_str(data, "name", where), kind=_str(data, "kind", where),
                        repository=validate_repository(_str(data, "repository", where), where),
                        subpath=_str(data, "subpath", where, empty=True), ref=_str(data, "ref", where),
                        commit=_str(data, "commit", where), spdx=_str(data, "spdx", where),
                        license_path=_relative(_str(data, "license_path", where), where),
                        notice_paths=tuple(_relative(p, where) for p in _strings(data, "notice_paths", where)),
                        skills=tuple(_relative(p, where, allow_dot=True) for p in _strings(data, "skills", where)),
                        purpose=_str(data, "purpose", where),
                        official_install=_str(data, "official_install", where),
                        include=tuple(_relative(p, where) for p in _strings(data, "include", where)),
                        default=data.get("default", True),
                        select_for=_strings(data, "select_for", where),
                        support=_support(data.get("support", {}), where),
                        regional=_str(data, "regional", where, empty=True) if "regional" in data else "")
    except KeyError as exc:
        raise CatalogError(f"{where}: missing {exc}") from None
    if not isinstance(entry.default, bool):
        raise CatalogError(f"{where}: default must be true or false")
    if not set(entry.support) <= set(entry.skills):
        raise CatalogError(f"{where}: support names a skill folder that is not in skills")
    if entry.subpath:
        _relative(entry.subpath, where)
    if not NAME_RE.fullmatch(entry.name) or entry.kind not in KINDS:
        raise CatalogError(f"{where}: name must be lowercase letters, digits, '.', '_' or '-', "
                           f"and kind one of {', '.join(KINDS)}")
    if not COMMIT_RE.fullmatch(entry.commit) or not entry.skills:
        raise CatalogError(f"{where}: needs a full commit id and at least one skill folder")
    return entry


def _support(data: object, where: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(data, dict) or not all(isinstance(value, list) for value in data.values()):
        raise CatalogError(f"{where}: support maps a skill folder to a list of item folders")
    return {_relative(key, where, allow_dot=True): tuple(_relative(path, where) for path in value)
            for key, value in data.items()}


def _step(data: object, where: str) -> Step:
    if isinstance(data, dict) and set(data) == {"run"} and isinstance(data["run"], list) and data["run"] \
            and all(isinstance(part, str) and part for part in data["run"]):
        return Step(run=tuple(data["run"]))
    if isinstance(data, dict) and set(data) == {"manual"} and isinstance(data["manual"], str) and data["manual"]:
        return Step(manual=data["manual"])
    raise CatalogError(f"{where}: each step is {{\"run\": [argv...]}} or {{\"manual\": \"text\"}}")


def _linked(data: object, where: str) -> Linked:
    if not isinstance(data, dict):
        raise CatalogError(f"{where}: expected an object")
    try:
        steps = data["steps"]
        if not isinstance(steps, list) or not steps:
            raise CatalogError(f"{where}: steps must be a non-empty list")
        entry = Linked(name=_str(data, "name", where), kind=_str(data, "kind", where),
                       source=_str(data, "source", where), version=_str(data, "version", where),
                       license=_str(data, "license", where),
                       steps=tuple(_step(step, f"{where} step {n}") for n, step in enumerate(steps, 1)),
                       reason=_str(data, "reason", where), purpose=_str(data, "purpose", where))
    except KeyError as exc:
        raise CatalogError(f"{where}: missing {exc}") from None
    if not NAME_RE.fullmatch(entry.name) or entry.kind not in KINDS:
        raise CatalogError(f"{where}: bad name or kind")
    return entry


def _cli(data: object, where: str) -> Cli:
    if not isinstance(data, dict):
        raise CatalogError(f"{where}: expected an object")
    try:
        return Cli(**{key: _str(data, key, where) for key in
                      ("name", "command", "source", "version", "license", "install_macos", "purpose")})
    except KeyError as exc:
        raise CatalogError(f"{where}: missing {exc}") from None


def _mcp(data: object, where: str) -> Mcp:
    if not isinstance(data, dict):
        raise CatalogError(f"{where}: expected an object")
    try:
        entry = Mcp(name=_str(data, "name", where), package=_str(data, "package", where),
                    version=_str(data, "version", where), templates=_strings(data, "templates", where),
                    source=validate_repository(_str(data, "source", where), where),
                    license=_str(data, "license", where), purpose=_str(data, "purpose", where))
    except KeyError as exc:
        raise CatalogError(f"{where}: missing {exc}") from None
    if not NAME_RE.fullmatch(entry.name) or not NPM_PACKAGE_RE.fullmatch(entry.package):
        raise CatalogError(f"{where}: name must be lowercase letters, digits, '.', '_' or '-', and package an "
                           "npm name such as @scope/name")
    if not PIN_RE.fullmatch(entry.version):
        raise CatalogError(f"{where}: version must be the plain X.Y.Z the templates pin")
    if entry.templates != (f"mcp/claude/{entry.name}.json", f"mcp/codex/{entry.name}.toml"):
        raise CatalogError(f"{where}: templates must be mcp/claude/{entry.name}.json and mcp/codex/{entry.name}.toml")
    if not entry.source.startswith("https://"):
        raise CatalogError(f"{where}: source must be the project's https URL")
    return entry


def load_catalog(path: pathlib.Path | None = None) -> Catalog:
    """Read and validate the allowlist; the shipped one when path is None."""
    path = pathlib.Path(path) if path is not None else CATALOG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"{path}: cannot be read ({exc}); reinstall oah to restore it") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise CatalogError(f"{path}: expected {{\"version\": 1, ...}}")
    bundled = tuple(_bundled(item, f"{path} bundled[{n}]") for n, item in enumerate(data.get("bundled", [])))
    linked = tuple(_linked(item, f"{path} linked[{n}]") for n, item in enumerate(data.get("linked", [])))
    clis = tuple(_cli(item, f"{path} clis[{n}]") for n, item in enumerate(data.get("clis", [])))
    if not isinstance(data.get("mcp", []), list):
        raise CatalogError(f"{path}: mcp must be a list")
    mcp = tuple(_mcp(item, f"{path} mcp[{n}]") for n, item in enumerate(data.get("mcp", [])))
    servers = [entry.name for entry in mcp]
    if len(set(servers)) != len(servers):
        raise CatalogError(f"{path}: duplicate mcp names")
    names = [entry.name for entry in (*bundled, *linked)]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise CatalogError(f"{path}: duplicate names {', '.join(duplicates)}")
    return Catalog(bundled=bundled, linked=linked, clis=clis, mcp=mcp)


def read_upstream(item_dir: pathlib.Path) -> Upstream:
    path = pathlib.Path(item_dir) / UPSTREAM_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"{path}: cannot be read ({exc})") from exc
    return Upstream.from_json(data, str(path))


def kit_skills_dir(env: Mapping[str, str] = os.environ) -> pathlib.Path | None:
    """The kit's bundled skills: $OAH_KIT_DIR/skills, else skills/ beside the source tree."""
    override = env.get(KIT_ENV)
    if override:
        folder = pathlib.Path(override) / "skills"
        return folder if folder.is_dir() else None
    package = pathlib.Path(oah.__file__).resolve().parent
    if package.parent.name == "src":
        folder = package.parent.parent / "skills"
        if folder.is_dir():
            return folder
    return None


def item_skill_dirs(item_dir: pathlib.Path, entry: Bundled) -> list[pathlib.Path]:
    """The skill folders of a bundled item that oah installs, in catalog order."""
    root = pathlib.Path(item_dir)
    return [root if relative == "." else root.joinpath(*relative.split("/")) for relative in entry.skills]


def skill_name(entry: Bundled, relative: str) -> str:
    """The folder name an installed skill gets: the item name for '.', else the folder's own name."""
    return entry.name if relative == "." else relative.rsplit("/", 1)[-1]
