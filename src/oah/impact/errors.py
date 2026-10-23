# 영향 지도 오류 — OAH_IMPACT_* 코드와 종료 코드(1 사용법, 69 환경)를 함께 싣는다.
from __future__ import annotations

EXIT_USAGE, EXIT_ENVIRONMENT = 1, 69


class ImpactError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_ENVIRONMENT) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")
