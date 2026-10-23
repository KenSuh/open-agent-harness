# CI 스타터 API — oah ci init 과 안내형 oah init(P7b)이 함께 쓰는 감지·계획·쓰기 함수를 한곳에서 내보낸다.
"""A starter GitHub Actions workflow for the user's own project (A21)."""
from __future__ import annotations

from oah.ci.detect import ECOSYSTEMS, Detection, Step, detect

__all__ = ["ECOSYSTEMS", "Detection", "Step", "detect"]
