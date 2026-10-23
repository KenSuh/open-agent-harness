# 도구 준비 — 도구 목록과 동의 기반 설치 안내를 doctor·init·설치 스크립트에 함께 내준다.
"""The tool catalog and the guided, consent-based tool setup."""
from __future__ import annotations

from oah.setup.tools import SignIn, Tool, catalog, display, needs, os_name_for

__all__ = ["SignIn", "Tool", "catalog", "display", "needs", "os_name_for"]
