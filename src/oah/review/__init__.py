# 리뷰 고리 패키지 — oah review 의 설정·기록·스냅샷·카나리·스키마·라운드 판정·실행 모듈과 공용 오류를 묶는다.
"""The review loop (PLAN 5.9): one validated round per `oah review`, immutable receipts, decision stops."""
from __future__ import annotations


class ReviewError(Exception):
    """A review refusal or failure; `code` is the OAH_* code that starts the message."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")
