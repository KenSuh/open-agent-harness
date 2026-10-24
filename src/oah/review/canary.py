# 리뷰 카나리 — 스냅샷 루트에 무작위 값을 배타적으로 만들어 두고, 리뷰어 근거에 그 값의 줄이 정확히 있는지만 본다(파일 접근 증명일 뿐이다).
"""The review canary (PLAN 5.9 step 4).

`plant(tree)` writes a fresh random value to `<tree>/.oah-review-canary` with exclusive create, so a file
the repository tracks under that name refuses with `OAH_REVIEW_CANARY_CONFLICT` instead of being trusted.
The value never goes into the prompt. `verified(evidence, value)` requires the whole line
`REVIEWED-TREE-CANARY: <value>` (surrounding blanks ignored). `LIMIT` states what that proves.
"""
from __future__ import annotations

import pathlib
import secrets

from oah.review import ReviewError

FILE_NAME = ".oah-review-canary"
LINE = "REVIEWED-TREE-CANARY: "
LIMIT = ("canary verification proves access to the snapshot's token file only; it proves neither source or diff "
         "coverage, review quality, nor tree identity, and echoed SHAs do not establish those either")


def plant(tree: pathlib.Path) -> str:
    """Create the canary file and return its value."""
    value = secrets.token_hex(16)
    path = pathlib.Path(tree) / FILE_NAME
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(value + "\n")
    except FileExistsError:
        raise ReviewError("OAH_REVIEW_CANARY_CONFLICT", f"the reviewed tree already contains {FILE_NAME}, so the "
                          "repository tracks a file with that name; remove it from the repository, commit, and run "
                          "oah review again") from None
    return value


def verified(evidence: str, value: str) -> bool:
    """True when the evidence holds the canary line for `value` as a whole line."""
    if not value:
        return False
    wanted = LINE + value
    lines = (evidence or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return any(line.strip() == wanted for line in lines)
