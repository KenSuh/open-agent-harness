# 리뷰 결과 수정 — 최신 영수증의 P0/P1 발견을 Codex 작업자로 리스 작업 트리에서 한 번 고치고, 시도의 시작과 단 하나의 결과를 기록하며, 커밋 명령을 보여 주고 커밋을 시도에 묶는다.
"""`oah fix` without the command line (PLAN 5.9, A1, A6, A7, A17, A22).

Round instructions
    `round_instructions(...)` fills `templates/review/correction.md`: the source receipt, the findings with
    their ids and class ids, and the class definitions, as bullets plus one fenced JSON block. Placeholders
    are replaced in one pass, so text from a finding is never substituted.
"""
from __future__ import annotations

import json
import re
from importlib import resources
from typing import Mapping, Sequence

PLACEHOLDERS = ("TASK", "ATTEMPT", "RECEIPT", "RECEIPT_DIGEST", "BASE", "REVIEWED", "HEAD", "FINDINGS", "CLASSES")
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def _fence(text: str, language: str = "text") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text.rstrip(chr(10))}\n{fence}"


def _line(text: object) -> str:
    return " ".join(str(text).split())


def round_instructions(*, task: str, attempt_id: str, receipt: str, receipt_digest: str, base: str, reviewed: str,
                       head: str, findings: Sequence[Mapping], classes: Sequence[Mapping]) -> str:
    """The correction brief's round instructions; `findings` and `classes` are the receipt's own objects."""
    template = resources.files("oah").joinpath("templates/review/correction.md").read_text(encoding="utf-8")
    listed = "\n".join(f"- {item['finding_id']} · {item['severity']} · class {item['class_id']} · "
                       f"{_line(item.get('axis', ''))} · {_line(item.get('location', ''))} — "
                       f"{_line(item.get('title', ''))}" for item in findings)
    exact = json.dumps({"findings": list(findings), "classes": list(classes)}, ensure_ascii=False, indent=2,
                       sort_keys=True)
    values = {
        "TASK": task, "ATTEMPT": attempt_id, "RECEIPT": receipt, "RECEIPT_DIGEST": receipt_digest, "BASE": base,
        "REVIEWED": reviewed, "HEAD": head,
        "FINDINGS": listed + "\n\nThe findings and their classes exactly as the review recorded them:\n"
                    + _fence(exact, "json"),
        "CLASSES": "\n".join(f"- {item['class_id']}: {_line(item.get('mechanism', ''))}; rule: "
                             f"{_line(item.get('rule', ''))}" for item in classes) or "(none recorded)",
    }
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)
