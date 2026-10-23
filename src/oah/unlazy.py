# unlazy 설치 위치 찾기 — OAH_UNLAZY_DIR, 프로젝트 스킬 폴더, 사용자 스킬 폴더 순서로 gate-check.mjs 가 있는 폴더를 고른다.
from __future__ import annotations

import json
import pathlib
from typing import Mapping

ENV_VAR = "OAH_UNLAZY_DIR"
INSTALL_HINT = "npx skills add Leonxlnx/unlazy"


def _is_unlazy(folder: pathlib.Path) -> bool:
    return (folder / "scripts" / "gate-check.mjs").is_file()


def find_unlazy_dir(repo_root: pathlib.Path | None, env: Mapping[str, str],
                    home: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """Return (folder, reason). An OAH_UNLAZY_DIR value is the only folder checked."""
    override = env.get(ENV_VAR)
    if override:
        folder = pathlib.Path(override)
        if _is_unlazy(folder):
            return folder, f"from {ENV_VAR}"
        return None, f"{ENV_VAR} has no scripts/gate-check.mjs"
    candidates: list[pathlib.Path] = []
    if repo_root is not None:
        candidates += [repo_root / ".agents" / "skills" / "unlazy", repo_root / ".claude" / "skills" / "unlazy"]
    candidates += [home / ".agents" / "skills" / "unlazy", home / ".claude" / "skills" / "unlazy"]
    for folder in candidates:
        if _is_unlazy(folder):
            return folder, "found"
    return None, f"unlazy not found (install: {INSTALL_HINT})"


def unlazy_version(unlazy_dir: pathlib.Path) -> str | None:
    """The version recorded in unlazy's package.json, or None."""
    try:
        manifest = json.loads((unlazy_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) else None
