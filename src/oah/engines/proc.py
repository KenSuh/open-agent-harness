# 외부 명령 해석·실행 — .cmd/.bat 를 직접 띄우지 않고 npm shim 은 node + bin 스크립트로 바꾸며, 시간 초과 시 프로세스 트리를 끝낸다.
"""Find and run external programs safely on macOS and Windows.

Windows runs `.cmd`/`.bat` files through cmd.exe, which re-parses arguments even with
`shell=False`. So batch files are never launched. An npm shim is replaced by `node.exe` plus the
package's declared bin script, and anything else is refused with OAH_UNSUPPORTED_BATCH_SHIM.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence

BATCH_SUFFIXES = (".cmd", ".bat")
REAP_SECONDS = 5

UNSUPPORTED_HINT = (
    "Install the tool's native executable, or configure an explicit command such as "
    '["node.exe", "<path to the script>"] in .oah/config.toml.'
)


class UnsupportedBatchShim(Exception):
    """A Windows batch file that oah will not launch."""

    code = "OAH_UNSUPPORTED_BATCH_SHIM"

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        super().__init__(f"{self.code}: {path}: {reason}. {UNSUPPORTED_HINT}")


@dataclass(frozen=True)
class Resolved:
    argv: tuple[str, ...]
    source: str
    kind: str  # "native" or "npm-shim"


@dataclass(frozen=True)
class RunResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


def is_batch(path: str) -> bool:
    return path.lower().endswith(BATCH_SUFFIXES)


def resolve_command(name: str, search_path: str | None = None) -> Resolved | None:
    """Find `name` on the search path (PATH by default); None when it is not installed."""
    found = shutil.which(name, path=search_path)
    if found is None:
        return None
    return classify(pathlib.Path(found), name, platform=sys.platform, search_path=search_path)


def classify(found: pathlib.Path, name: str, *, platform: str, search_path: str | None) -> Resolved:
    """Turn a found file into the argv that runs it, replacing Windows npm shims."""
    if platform == "win32" and is_batch(found.name):
        script = _npm_bin_script(found, name)
        node = _node_executable(found, search_path)
        return Resolved(argv=(str(node), str(script)), source=str(found), kind="npm-shim")
    return Resolved(argv=(str(found),), source=str(found), kind="native")


def _modules_dir(shim: pathlib.Path) -> pathlib.Path:
    # A global install keeps shims beside node_modules; a project install keeps them in node_modules/.bin.
    if shim.parent.name.lower() == ".bin" and shim.parent.parent.name.lower() == "node_modules":
        return shim.parent.parent
    return shim.parent / "node_modules"


def _npm_bin_script(shim: pathlib.Path, name: str) -> pathlib.Path:
    modules = _modules_dir(shim)
    matches: list[pathlib.Path] = []
    if modules.is_dir():
        for package in _packages(modules):
            script = _declared_bin(package, modules, name)
            if script is not None:
                matches.append(script)
    if not matches:
        raise UnsupportedBatchShim(str(shim), "no installed npm package declares this command")
    if len(matches) > 1:
        raise UnsupportedBatchShim(str(shim), "several npm packages declare this command")
    return matches[0]


def _packages(modules: pathlib.Path) -> list[pathlib.Path]:
    packages: list[pathlib.Path] = []
    for entry in sorted(modules.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name.startswith("@"):
            packages.extend(sorted(p for p in entry.iterdir() if p.is_dir()))
        else:
            packages.append(entry)
    return packages


def _declared_bin(package: pathlib.Path, modules: pathlib.Path, name: str) -> pathlib.Path | None:
    try:
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    bin_field = manifest.get("bin")
    relative = None
    if isinstance(bin_field, str):
        command = str(manifest.get("name", "")).rsplit("/", 1)[-1]
        if command.lower() == name.lower():
            relative = bin_field
    elif isinstance(bin_field, dict):
        for command, target in bin_field.items():
            if str(command).lower() == name.lower():
                relative = target
                break
    if not isinstance(relative, str):
        return None
    script = package / pathlib.PurePosixPath(relative)
    try:
        script.resolve().relative_to(modules.resolve())
    except ValueError:
        return None  # a bin path that escapes node_modules is not trusted
    return script if script.is_file() else None


def _node_executable(shim: pathlib.Path, search_path: str | None) -> pathlib.Path:
    local = shim.parent / "node.exe"
    if local.is_file():
        return local
    found = shutil.which("node", path=search_path)
    if found is None or is_batch(found):
        raise UnsupportedBatchShim(str(shim), "node.exe was not found for this npm shim")
    return pathlib.Path(found)


def decode_output(data: bytes | None, encoding: str = "utf-8") -> str:
    return (data or b"").decode(encoding, errors="replace")


def run_command(argv: Sequence[str], *, timeout: float, cwd: str | None = None,
                env: Mapping[str, str] | None = None, encoding: str = "utf-8",
                stdin_text: str | None = None) -> RunResult:
    """Run argv without a shell. On timeout the whole process tree is killed."""
    if not argv:
        raise ValueError("empty command")
    if is_batch(str(argv[0])):
        raise UnsupportedBatchShim(str(argv[0]), "batch files are never launched directly")
    options: dict = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    proc = subprocess.Popen(
        [str(part) for part in argv],
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        **options,
    )
    data = stdin_text.encode(encoding) if stdin_text is not None else None
    try:
        out, err = proc.communicate(input=data, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return RunResult(None, decode_output(out, encoding), decode_output(err, encoding), True)
    return RunResult(proc.returncode, decode_output(out, encoding), decode_output(err, encoding), False)


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process started by run_command together with its children."""
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        taskkill = pathlib.Path(root) / "System32" / "taskkill.exe"
        if taskkill.is_file():
            subprocess.run([str(taskkill), "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
