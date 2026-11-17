# hashes 영역: skills CLI 가 공개한 computedHash 시험 벡터 세 개를 사례로 쓰고, 공개 알고리즘으로 다시 계산해 맞는지 확인한다.
"""Area `hashes`: the three public `computedHash` vectors of the `skills` CLI."""
from __future__ import annotations

import hashlib
import pathlib

from fixture import Area, Case, ok

SOURCE = "https://raw.githubusercontent.com/vercel-labs/skills/main/README.md (section Lock File Content Hash)"
VECTORS = [
    ("no-files", {}, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("one-file", {"SKILL.md": "# hi\n"}, "93c36952d8843b33e68ae970ebabcc03e1687c9bb0dd1ecaf357060318952f55"),
    ("two-files", {"SKILL.md": "a\n", "docs/b.md": "b\n"},
     "b22c4d22aa319d2924312fa18fcbfedc88fa89c74dbd8f298c683e1caf7b7f24"),
]


def computed_hash(files: dict[str, bytes]) -> str:
    """The README's algorithm for files already collected, with `/` paths."""
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda name: name.encode("utf-8")):
        data = files[path]
        if b"\0" not in data:
            data = data.replace(b"\r\n", b"\n")
        digest.update(path.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return digest.hexdigest()


def build(base: pathlib.Path) -> Area:
    cases = []
    for name, files, expected in VECTORS:
        if computed_hash({path: text.encode("utf-8") for path, text in files.items()}) != expected:
            raise SystemExit(f"hashes: the public vector {name} does not match the documented algorithm")
        cases.append(Case(name, ok(expected), {"files": files}, source="public", function="skills computedHash"))
    return Area(
        "hashes",
        functions=[],
        mined_from=[SOURCE],
        cases=cases,
        omitted=[],
        notes=["No v0.4.0 function computes this hash; the expectations are the published vectors, which the "
               "extractor recomputes with the published algorithm before writing them.",
               "input.json `files` maps each path in the skill folder (with `/`) to its UTF-8 text."],
    )
