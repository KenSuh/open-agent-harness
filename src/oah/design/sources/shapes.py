from __future__ import annotations

from typing import TypeAlias, assert_never

Json: TypeAlias = None | bool | int | float | str | list["Json"] | dict[str, "Json"]
Shape: TypeAlias = type[str] | type[int] | type[bool] | type[None] | dict[str, "Shape"] | list["Shape"] | tuple["Shape", ...]


class ShapeError(Exception):
    def __init__(self, path: str, expected: str) -> None:
        self.path, self.expected = path, expected
        super().__init__(f"{path} must be {expected}")


def validate(value: Json, schema: Shape, path: str) -> None:
    match schema:
        case dict():
            if not isinstance(value, dict) or set(value) != set(schema):
                raise ShapeError(path, "an object with exactly " + ", ".join(sorted(schema)))
            for key, child in schema.items():
                validate(value[key], child, f"{path}.{key}")
        case list():
            if not isinstance(value, list):
                raise ShapeError(path, "a list")
            for index, item in enumerate(value):
                validate(item, schema[0], f"{path}[{index}]")
        case tuple():
            for alternative in schema:
                try:
                    validate(value, alternative, path)
                except ShapeError:
                    continue
                return
            raise ShapeError(path, "one of the permitted shapes")
        case type():
            if type(value) is not schema:
                raise ShapeError(path, schema.__name__)
        case unreachable:
            assert_never(unreachable)


TEXT: Shape = (str, type(None))
DECLARED: Shape = (type(None), {"path": str, "field": str, "value": str})
FILE: Shape = {"path": str, "mode": str, "blob": str, "size": int, "sha256": str}
NOTICE: Shape = {"path": str, "blob": str, "sha256": str}
ORIGINAL: Shape = {"path": str, "blob": str, "size": int, "sha256": str}
SCOPE: Shape = {"path": str, "level": TEXT, "files": [str], "nested": [str]}
SOURCE: Shape = {"id": str, "provider": str, "locator": str, "track": str, "tag_prefix": str, "channel": str,
                 "ref": TEXT, "paths": [str], "manifest_path": TEXT, "release_order": TEXT,
                 "selection_policy": TEXT, "verification_policy": str}
CHECKED: Shape = {"observation": TEXT, "attempted_at": TEXT, "observed_at": TEXT, "verified_at": TEXT,
                  "confirmed_at": TEXT, "historical": str, "freshness_when_recorded": str}
REVIEWED: Shape = (type(None),
                   {"commit": TEXT, "ref": TEXT, "tree": TEXT, "tag_object": TEXT, "files": [FILE]},
                   {"url": str, "final_url": str, "content_type": str, "bytes": int, "sha256": str,
                    "text_sha256": str, "text_bytes": int})
RELEASE: Shape = (type(None),
                  {"id": int, "tag": str, "tag_object": TEXT, "commit": str, "tree": str, "channel": str,
                   "prerelease": bool, "created_at": TEXT, "published_at": TEXT, "updated_at": TEXT,
                   "html_url": TEXT, "metadata_digest": str, "release_observation": str,
                   "notes": (type(None), {"bytes": int, "sha256": str}), "selection": str, "limitations": [str]})
LICENSE: Shape = (type(None),
                  {"form": str, "expression": TEXT, "ids": [str], "alternatives": [str], "strict": bool,
                   "eligibility": str, "usable_under": TEXT, "policy": [str], "policy_digest": str, "reason": str,
                   "evidence": [{"path": str, "blob": str, "sha256": str, "spdx": TEXT, "strict": bool}],
                   "notices": [NOTICE], "declared": DECLARED,
                   "scopes": [SCOPE], "problems": [str]})
