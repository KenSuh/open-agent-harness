# 테스트 패키지 초기화 — 설치하지 않고도 src/ 아래 oah 를 import 하도록 경로를 맨 앞에 두고, 어떤 테스트도 네트워크에 닿지 않게 막는다.
import pathlib
import sys

_SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tests.helpers import block_network  # noqa: E402

block_network()
