# 픽스처에 쓰이는 패키지 자료와 실행 중 템플릿 읽기를 v0.4.0 태그의 원본 blob으로 고정한다.
"""Read fixture source bytes from Git, including the oracle's runtime resource reads."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
import subprocess
from typing import Final
from unittest.mock import patch

ROOT: Final = Path(__file__).resolve().parents[2]
TAG: Final = "v0.4.0"


def read_blob(path: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", f"{TAG}:{path}"],
                          capture_output=True, check=True).stdout


@contextmanager
def package_resources(base: Path) -> Iterator[None]:
    names = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-rz", "--name-only", TAG,
                            "--", "src/oah/templates/"], capture_output=True, check=True).stdout
    package = base / "frozen-package"
    for name in names.split(b"\0"):
        if name:
            path = name.decode("utf-8")
            target = package.joinpath(*path.removeprefix("src/oah/").split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read_blob(path))

    def files(anchor: str) -> Path:
        if anchor != "oah":
            raise SystemExit(f"conformance: unexpected package resource read: {anchor}")
        return package

    # Install before importing oah: wiki.notes caches its template root at import time.
    with patch.object(resources, "files", files):
        yield
