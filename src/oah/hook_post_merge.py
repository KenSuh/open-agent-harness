# post-merge 훅 — pull·merge 뒤 영향 지도를 HEAD 에 맞춰 갱신한다(선택 설치; 실패해도 병합은 그대로다).
from __future__ import annotations

import argparse

from oah.impact.mapstore import run_hook


def run(args: argparse.Namespace) -> int:
    return run_hook("post-merge")
