# 평가 오류 — 모든 거부는 OAH_EVAL_* 코드로 시작하고 종료 코드를 함께 싣는다.
from __future__ import annotations

EXIT_REFUSED, EXIT_ENVIRONMENT = 1, 69


class EvalError(Exception):
    """A refusal or failure of `oah eval`; the message starts with the code and says what to do next."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")
