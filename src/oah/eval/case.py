# 평가 사례 형식 — case.toml 과 inputs/ 를 읽고 검증하며, 사례 폴더 전체의 지문(SHA-256)을 만든다.
from __future__ import annotations

import hashlib
import pathlib
import re
import stat
import tomllib
from dataclasses import dataclass
from typing import Any, Mapping

from oah.eval.errors import EvalError

CASE_FILE = "case.toml"
INPUTS_DIR = "inputs"
SPLITS = ("dev", "control", "holdout")
FORMATS = ("bundle", "tar", "zip")
_COMMIT = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Case:
    folder: pathlib.Path
    id: str
    split: str
    provenance: str
    requirement: pathlib.Path
    prompt: pathlib.Path
    inputs: pathlib.Path
    forbidden: tuple[str, ...]
    snapshot_file: pathlib.Path
    snapshot_format: str
    snapshot_commit: str


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_toml(path: pathlib.Path, code: str, what: str) -> dict:
    """Parse a UTF-8 TOML file (CRLF accepted); every failure names the file and the fix."""
    try:
        text = pathlib.Path(path).read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        raise EvalError(code, f"{path} does not exist; check the path to the {what}") from None
    except OSError as exc:
        raise EvalError(code, f"{path} cannot be read ({exc.strerror or exc}); make it a readable file") from exc
    except UnicodeDecodeError as exc:
        raise EvalError(code, f"{path} is not UTF-8 (byte {exc.start}); save it as UTF-8") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EvalError(code, f"{path} is not valid TOML ({exc}); fix the syntax") from exc


def _string(data: Mapping[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalError("OAH_EVAL_CASE_INVALID", f"{where}: {key} is missing or not a non-empty string; add it")
    return value


def _inside(folder: pathlib.Path, relative: str, key: str, where: str) -> pathlib.Path:
    """A path named in case.toml, which must stay inside the case folder."""
    pure = pathlib.PurePosixPath(relative.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or any(":" in part for part in pure.parts):
        raise EvalError("OAH_EVAL_CASE_INVALID",
                        f"{where}: {key} = {relative!r} must be a relative path inside the case folder")
    path = folder.joinpath(*pure.parts)
    validate_case_path(path, folder)
    if not path.is_file():
        raise EvalError("OAH_EVAL_CASE_INVALID", f"{where}: {key} names {path}, which is not a file; add it")
    return path


def validate_case_path(path: pathlib.Path, folder: pathlib.Path, *, file_link: bool = False) -> None:
    """Named inputs have no linked ancestors; digest-only file links must stay inside the case."""
    try:
        if not path.resolve(strict=True).is_relative_to(folder):
            raise ValueError
        current = folder
        for part in path.relative_to(folder).parts:
            current = current / part
            info = current.lstat()
            linked = current.is_symlink() or bool(getattr(info, "st_file_attributes", 0)
                                                  & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            if linked and not (file_link and current == path and path.is_file()):
                raise ValueError
        if not (path.is_file() or path.is_dir()):
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise EvalError("OAH_EVAL_CASE_INVALID", "a case input is missing, special, outside the case folder "
                        "or has a linked ancestor; use regular files and directories inside the case folder") from None


def load_case(folder: pathlib.Path) -> Case:
    """Read and validate <folder>/case.toml and <folder>/inputs/."""
    folder = pathlib.Path(folder).resolve()
    where = str(folder / CASE_FILE)
    validate_case_path(folder / CASE_FILE, folder)
    data = read_toml(folder / CASE_FILE, "OAH_EVAL_CASE_INVALID", "case folder")
    case_id = _string(data, "id", where)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", case_id):
        raise EvalError("OAH_EVAL_CASE_INVALID",
                        f"{where}: id {case_id!r} may use only letters, digits, '.', '_' and '-'")
    split = _string(data, "split", where)
    if split not in SPLITS:
        raise EvalError("OAH_EVAL_CASE_INVALID", f"{where}: split is {split!r}; use dev, control or holdout")
    forbidden = data.get("forbidden", [])
    if not isinstance(forbidden, list) or not all(isinstance(item, str) and item for item in forbidden):
        raise EvalError("OAH_EVAL_CASE_INVALID", f"{where}: forbidden must be a list of file patterns")
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise EvalError("OAH_EVAL_CASE_INVALID",
                        f"{where}: add a [snapshot] table with file, format and commit")
    fmt = _string(snapshot, "format", where + " [snapshot]")
    if fmt not in FORMATS:
        raise EvalError("OAH_EVAL_CASE_INVALID", f"{where}: snapshot format is {fmt!r}; use bundle, tar or zip")
    commit = _string(snapshot, "commit", where + " [snapshot]").lower()
    if not _COMMIT.match(commit):
        raise EvalError("OAH_EVAL_CASE_INVALID", f"{where}: snapshot commit must be a full commit id")
    inputs = folder / INPUTS_DIR
    if not inputs.is_dir():
        raise EvalError("OAH_EVAL_CASE_INVALID", f"{folder}: add an {INPUTS_DIR}/ folder (it may hold only a note)")
    validate_case_path(inputs, folder)
    return Case(folder=folder, id=case_id, split=split, provenance=_string(data, "provenance", where),
                requirement=_inside(folder, _string(data, "requirement", where), "requirement", where),
                prompt=_inside(folder, _string(data, "prompt", where), "prompt", where),
                inputs=inputs, forbidden=tuple(forbidden),
                snapshot_file=_inside(folder, _string(snapshot, "file", where), "snapshot file", where),
                snapshot_format=fmt, snapshot_commit=commit)


def case_digest(folder: pathlib.Path) -> str:
    """SHA-256 over every file of the case folder: its relative path and the SHA-256 of its bytes."""
    folder = pathlib.Path(folder).resolve()
    digest = hashlib.sha256()
    def walk(base: pathlib.Path) -> None:
        for path in sorted(base.iterdir()):
            validate_case_path(path, folder, file_link=True)
            if path.is_file():
                relative = path.relative_to(folder).as_posix()
                digest.update(f"{relative}\0{file_sha256(path)}\n".encode("utf-8"))
            else:
                walk(path)

    walk(folder)
    return digest.hexdigest()


def is_inside(path: pathlib.Path, folder: pathlib.Path) -> bool:
    try:
        pathlib.Path(path).resolve().relative_to(pathlib.Path(folder).resolve())
    except ValueError:
        return False
    return True
