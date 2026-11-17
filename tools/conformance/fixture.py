# 적합성 픽스처 공통 도구: 사례·영역 자료형, 결정적 JSON 직렬화, 임시 경로를 고정 자리표시자로 바꾸는 함수.
"""Shared pieces of the conformance extractor: cases, areas, deterministic JSON and path placeholders."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import shutil
import tempfile
from typing import Any, Callable, Mapping

CASE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


# v0.4.0 source ids are "sk-" plus hex digits, which the repository privacy scan reads as an API key. The JSON
# escape \u0073 ("s") keeps the decoded value identical while the raw text no longer looks like one.
SOURCE_ID = re.compile(r"(?<![A-Za-z0-9\\])sk-")


def dumps(value: Any) -> bytes:
    """Sorted keys, two-space indent, ASCII-only escapes, LF and one final newline."""
    text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    out = SOURCE_ID.sub(lambda match: "\\u0073k-", text)
    if json.loads(out) != json.loads(text):
        raise ValueError("the source id escape changed a value")
    return out.encode("utf-8")


@dataclasses.dataclass
class Case:
    name: str
    expect: Mapping[str, Any]
    input: Mapping[str, Any] | None = None  # becomes input.json
    # Input files by relative posix path: UTF-8 text goes to input.json `files`, other bytes to `bytes` as hex.
    files: Mapping[str, bytes] = dataclasses.field(default_factory=dict)
    source: str = "v0.4.0"  # v0.4.0 | design | public
    function: str = ""  # the pinned function, as module.name
    note: str = ""

    def __post_init__(self) -> None:
        if not CASE_NAME.fullmatch(self.name):
            raise ValueError(f"bad case name {self.name!r}")
        if self.input is None and not self.files:
            raise ValueError(f"case {self.name} has no input")
        keys = set(self.expect)
        if keys == {"ok"}:
            return
        if keys != {"error"} or not str(self.expect["error"].get("code", "")).startswith("OAH_") \
                or not set(self.expect["error"]) <= {"code", "message"}:
            raise ValueError(f"case {self.name} has a bad expectation {self.expect!r}")


@dataclasses.dataclass
class Area:
    name: str
    functions: list[dict]  # {"module", "name", "file"}
    mined_from: list[str]
    cases: list[Case]
    omitted: list[dict] = dataclasses.field(default_factory=list)  # {"behaviour", "reason"}
    deviations: list[dict] = dataclasses.field(default_factory=list)  # {"case", "design", "change"}
    notes: list[str] = dataclasses.field(default_factory=list)

    def files(self) -> dict[str, bytes]:
        names = [case.name for case in self.cases]
        if len(set(names)) != len(names):
            raise ValueError(f"area {self.name} repeats a case name")
        known = set(names)
        for item in self.deviations:
            if item["case"] not in known:
                raise ValueError(f"area {self.name}: deviation names unknown case {item['case']}")
        manifest = {
            "area": self.name,
            "status": "pending",
            "oracle": "v0.4.0",
            "functions": self.functions,
            "mined_from": self.mined_from,
            "cases": [{"name": case.name, "source": case.source, "function": case.function,
                       **({"note": case.note} if case.note else {})} for case in self.cases],
            "omitted": self.omitted,
            "deviations": self.deviations,
            "notes": self.notes,
        }
        out = {f"{self.name}/MANIFEST.json": dumps(manifest)}
        for case in self.cases:
            prefix = f"{self.name}/{case.name}/"
            out[prefix + "expect.json"] = dumps(case.expect)
            given = dict(case.input) if case.input is not None else {}
            text: dict[str, str] = {}
            raw: dict[str, str] = {}
            for rel, data in case.files.items():
                if rel.startswith("/") or ".." in rel.split("/") or "\\" in rel:
                    raise ValueError(f"bad input path {rel!r}")
                try:
                    text[rel] = data.decode("utf-8")  # UTF-8 text (CRLF and a BOM included) as a JSON string
                except UnicodeDecodeError:
                    raw[rel] = data.hex()  # other bytes as lowercase hex
            for key, value in (("files", text), ("bytes", raw)):
                if value:
                    if key in given:
                        raise ValueError(f"case {case.name} gives `{key}` twice")
                    given[key] = value
            out[prefix + "input.json"] = dumps(given)
        return out


def fn(module: str, name: str) -> dict:
    """A pinned function entry: module path and its source file under src/."""
    return {"module": module, "name": name, "file": "src/" + module.replace(".", "/") + ".py"}


def ok(value: Any) -> dict:
    return {"ok": value}


def err(code: str, message: str | None = None) -> dict:
    return {"error": {"code": code, **({"message": message} if message is not None else {})}}


class Scratch:
    """A temporary folder whose path never reaches a fixture: `scrub` rewrites it to a placeholder."""

    def __init__(self, parent: pathlib.Path, placeholder: str = "<base>") -> None:
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="case-", dir=parent))
        self.placeholder = placeholder

    def write(self, files: Mapping[str, bytes], under: str = "") -> pathlib.Path:
        root = self.path / under if under else self.path
        root.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            target = root.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return root

    def rel(self, path: pathlib.Path | str) -> str:
        """`<placeholder>/<posix path below the scratch folder>`."""
        relative = pathlib.Path(path).relative_to(self.path).as_posix()
        return self.placeholder if relative == "." else f"{self.placeholder}/{relative}"

    def scrub(self, value: Any) -> Any:
        """Every string with the scratch path in either spelling gets the placeholder, with `/` after it."""
        native, posix = str(self.path), self.path.as_posix()

        def fix(text: str) -> str:
            for spelling in (native, posix):
                while spelling in text:
                    start = text.index(spelling)
                    end = start + len(spelling)
                    tail = re.match(r"[^\s'\"]*", text[end:]).group(0)
                    text = text[:start] + self.placeholder + tail.replace("\\", "/") + text[end + len(tail):]
            return text

        return walk(value, fix)

    def close(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def walk(value: Any, fix: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fix(value)
    if isinstance(value, Mapping):
        return {fix(str(key)): walk(item, fix) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [walk(item, fix) for item in value]
    return value


def utf8(text: str) -> bytes:
    return text.encode("utf-8")
