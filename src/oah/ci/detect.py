# 테스트 명령 감지 — 프로젝트 파일을 읽어 Python·Node·Go·Rust 의 설치·테스트 단계를 고른다. 깨진 파일은 설정이 없는 것으로 본다.
from __future__ import annotations

import json
import pathlib
import re
import tomllib
from dataclasses import dataclass
from typing import Any, Iterable

ECOSYSTEMS = ("python", "node", "go", "rust")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


@dataclass(frozen=True)
class Step:
    name: str
    run: str


@dataclass(frozen=True)
class Detection:
    ecosystem: str
    label: str
    reason: str
    steps: tuple[Step, ...]


def _read_text(path: pathlib.Path) -> str | None:
    """The file as text (BOM stripped, CRLF read as LF), or None when it is missing or not UTF-8."""
    try:
        return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return None


def _read_toml(path: pathlib.Path) -> dict[str, Any] | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _is_pytest(requirement: Any) -> bool:
    if not isinstance(requirement, str):
        return False
    match = _REQUIREMENT_NAME.match(requirement)
    if match is None:
        return False
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    return name == "pytest" or name.startswith("pytest-")


def _requirement_lists(pyproject: dict[str, Any]) -> Iterable[Any]:
    project = pyproject.get("project")
    if isinstance(project, dict):
        yield project.get("dependencies")
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            yield from optional.values()
    groups = pyproject.get("dependency-groups")
    if isinstance(groups, dict):
        yield from groups.values()


def _pytest_in_pyproject(pyproject: dict[str, Any]) -> bool:
    tool = pyproject.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
        return True
    return any(isinstance(items, list) and any(_is_pytest(item) for item in items)
               for items in _requirement_lists(pyproject))


def _has_python_tests(folder: pathlib.Path) -> bool:
    return folder.is_dir() and any(path.is_file() for path in folder.rglob("*.py"))


def _pytest_reason(root: pathlib.Path, pyproject: dict[str, Any] | None) -> str | None:
    """Where pytest is configured, or None."""
    if (root / "pytest.ini").is_file():
        return "pytest.ini"
    for conftest in ("conftest.py", "tests/conftest.py"):
        if (root / conftest).is_file():
            return conftest
    if pyproject is not None and _pytest_in_pyproject(pyproject):
        return "pyproject.toml"
    setup_cfg = _read_text(root / "setup.cfg")
    if setup_cfg is not None and any(line.strip() == "[tool:pytest]" for line in setup_cfg.split("\n")):
        return "setup.cfg"
    for requirements in sorted(root.glob("requirements*.txt")):
        text = _read_text(requirements)
        if text is not None and any(_is_pytest(line) for line in text.split("\n")):
            return requirements.name
    return None


def _python(root: pathlib.Path) -> Detection | None:
    markers = [name for name in ("pyproject.toml", "setup.cfg") if (root / name).is_file()]
    if not markers and _has_python_tests(root / "tests"):
        markers.append("a tests/ folder with Python files")
    if not markers:
        return None
    pyproject = _read_toml(root / "pyproject.toml") if (root / "pyproject.toml").is_file() else None
    steps = []
    if (root / "requirements.txt").is_file():
        steps.append(Step("Install requirements", "python -m pip install -r requirements.txt"))
    if pyproject is not None and isinstance(pyproject.get("project"), dict):
        steps.append(Step("Install the project", "python -m pip install -e ."))
    found = ", ".join(markers)
    pytest_from = _pytest_reason(root, pyproject)
    if pytest_from is not None:
        steps += [Step("Install pytest", "python -m pip install pytest"), Step("Run tests", "python -m pytest")]
        return Detection("python", "Python (pytest)", f"found {found}; pytest is configured in {pytest_from}",
                         tuple(steps))
    if any(path.is_file() and path.stem.isidentifier() for path in (root / "tests").glob("test*.py")):
        command = "python -m unittest discover -s tests"
    elif any(path.is_file() and path.stem.isidentifier() for path in root.glob("test*.py")):
        command = "python -m unittest discover"
    else:
        return None
    steps.append(Step("Run tests", command))
    return Detection("python", "Python (unittest)", f"found {found}; no pytest configuration, so unittest",
                     tuple(steps))


def _node(root: pathlib.Path) -> Detection | None:
    text = _read_text(root / "package.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test, str) or not test.strip():
        return None
    return Detection("node", "Node.js (npm)", "found package.json with a test script",
                     (Step("Install dependencies", "npm ci"), Step("Run tests", "npm test")))


def _go(root: pathlib.Path) -> Detection | None:
    if not (root / "go.mod").is_file():
        return None
    return Detection("go", "Go", "found go.mod", (Step("Run tests", "go test ./..."),))


def _rust(root: pathlib.Path) -> Detection | None:
    if not (root / "Cargo.toml").is_file():
        return None
    return Detection("rust", "Rust (cargo)", "found Cargo.toml", (Step("Run tests", "cargo test"),))


def detect(root: pathlib.Path) -> tuple[Detection, ...]:
    """Every ecosystem found at the project root, in the order Python, Node, Go, Rust."""
    root = pathlib.Path(root)
    found = (_python(root), _node(root), _go(root), _rust(root))
    return tuple(item for item in found if item is not None)
