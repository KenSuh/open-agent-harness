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
    found = _find_command(name, search_path, platform=sys.platform)
    if found is None:
        return None
    return classify(pathlib.Path(found), name, platform=sys.platform, search_path=search_path)


def _find_command(name: str, search_path: str | None, *, platform: str) -> str | None:
    if platform != "win32":
        return shutil.which(name, path=search_path)
    # Python 3.11's Windows which() prepends cwd even with an explicit path.
    # Search directories ourselves, applying PATHEXT within each directory.
    path = os.environ.get("PATH", os.defpath) if search_path is None else search_path
    extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
    extensions = [ext for ext in extensions if ext.startswith(".") and "/" not in ext and "\\" not in ext]
    names = [name] if name.lower().endswith(tuple(ext.lower() for ext in extensions)) else [
        name + ext for ext in extensions
    ]
    for entry in path.split(";"):
        if entry.startswith('"') and entry.endswith('"'):
            entry = entry[1:-1]
        directory = pathlib.Path(entry)
        if not directory.is_absolute():
            continue
        for filename in names:
            candidate = directory / filename
            try:
                for item in candidate.parent.iterdir():
                    if item.name.lower() == candidate.name.lower() and item.is_file():
                        return str(item)
            except OSError:
                continue
    return None


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
    found = _find_command("node.exe", search_path, platform="win32")
    if found is None or is_batch(found):
        raise UnsupportedBatchShim(str(shim), "node.exe was not found for this npm shim")
    return pathlib.Path(found)


def decode_output(data: bytes | None, encoding: str = "utf-8") -> str:
    return (data or b"").decode(encoding, errors="replace")


class _WindowsJob:
    """Own a process tree even after its launcher exits; close kills any survivors."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ((ctypes.c_void_p, wintypes.LPCWSTR), wintypes.HANDLE),
            "SetInformationJobObject": ((wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD), wintypes.BOOL),
            "AssignProcessToJobObject": ((wintypes.HANDLE, wintypes.HANDLE), wintypes.BOOL),
            "TerminateJobObject": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
            "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def start(self, proc: subprocess.Popen) -> None:
        import ctypes
        from ctypes import wintypes

        if not self.api.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        proc._oah_job = self

        # Popen closes the primary thread handle. Find that still-suspended thread
        # with the documented Tool Help API, then resume it only after assignment.
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
                        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                        ("flags", wintypes.DWORD)]

        signatures = {
            "CreateToolhelp32Snapshot": ((wintypes.DWORD, wintypes.DWORD), wintypes.HANDLE),
            "Thread32First": ((wintypes.HANDLE, ctypes.POINTER(ThreadEntry)), wintypes.BOOL),
            "Thread32Next": ((wintypes.HANDLE, ctypes.POINTER(ThreadEntry)), wintypes.BOOL),
            "OpenThread": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
            "ResumeThread": ((wintypes.HANDLE,), wintypes.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            present = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if entry.process_id == proc.pid:
                    thread = self.api.OpenThread(0x2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        self.api.CloseHandle(thread)
                    return
                present = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("could not find the suspended process thread")
        finally:
            self.api.CloseHandle(snapshot)

    def terminate(self) -> None:
        import ctypes

        if not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def run_command(argv: Sequence[str], *, timeout: float, cwd: str | None = None,
                env: Mapping[str, str] | None = None, encoding: str = "utf-8",
                stdin_text: str | None = None) -> RunResult:
    """Run argv without a shell. On timeout the whole process tree is killed."""
    if not argv:
        raise ValueError("empty command")
    if is_batch(str(argv[0])):
        raise UnsupportedBatchShim(str(argv[0]), "batch files are never launched directly")
    options: dict = {}
    job = None
    if os.name == "nt":
        job = _WindowsJob()
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x4  # CREATE_SUSPENDED
    else:
        options["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            [str(part) for part in argv],
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            **options,
        )
        if job is not None:
            try:
                job.start(proc)
            except BaseException:
                proc.kill()
                proc.communicate(timeout=REAP_SECONDS)
                raise
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
    finally:
        if job is not None:
            job.close()


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process started by run_command together with its children."""
    if os.name == "nt":
        job = getattr(proc, "_oah_job", None)
        if job is not None:
            job.terminate()
            return
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
