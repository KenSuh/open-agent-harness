# 프로젝트 도구 실행기 진입점 — 데스크톱 앱이 실행 파일 옆에 두고 부르는 독립 실행기의 main 이다. 인자를 엄격히 읽고, 선택된 작업 트리 하나의 영향 지도 연산을 오프라인으로 돌려 JSON 한 줄과 고정 종료 코드로 답한다.
"""The trusted entry of the standalone project-tools runner (`oah-project-tools`).

    oah-project-tools --version
    oah-project-tools impact status  --root <absolute folder> [--git <absolute git executable>]
    oah-project-tools impact refresh --root <absolute folder> [--git <absolute git executable>]

`--version` prints `oah-project-tools <version>` and exits 0. Every `impact` call prints exactly one ASCII JSON object
and a newline on standard output (the shape is `oah.project_tools_impact.Outcome.to_json`) and exits with:

    0   ok is true
    1   the arguments are wrong (error.code OAH_PROJECT_TOOLS_USAGE)
    69  refused or unavailable: ok is false and error.code says why
    70  an unexpected failure (error.code OAH_PROJECT_TOOLS_INTERNAL)

`status` is read only. `refresh` builds the first map and afterwards updates it; a map that is already fresh is left
untouched (performed is "none"). Both always run in app-selected mode: the folder is untrusted, git is pinned and
hardened, and nothing reaches the network.

The Impact Map action reads nothing from standard input. Jev and wiki-index take bounded input on a pipe.
A caller that needs a
wall-clock limit ends the process tree: the store is replaced atomically and its lock is released by the system.
On POSIX a TERM (or HUP) signal is the orderly stop: the run unwinds at once, the git call in progress ends its
child's process tree (`oah.engines.proc.run_command` does that for any interrupted caller), no answer is printed
and the exit status is 128 plus the signal number (143 for TERM). On Windows the caller ends its Job Object.
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import signal
import sys
from types import FrameType
from typing import Any, Callable, Iterator, Sequence

import oah
from oah import project_tools_impact as impact

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_INTERNAL = 0, 1, 69, 70
GROUPS = {
    "impact": ("status", "refresh"),
    "jev": ("status", "prepare", "enable", "disable", "key-state", "store-key", "delete-key", "prepare-test", "test"),
    "wiki-index": ("status", "probe", "build", "search"),
}
OPTIONS = ("--root", "--git")
OUTPUT_LIMIT = 1 << 18  # bytes of one answer
STOP_SIGNALS = ("SIGTERM", "SIGHUP")  # how a caller ends a run on POSIX
USAGE = ("usage: oah-project-tools --version | <impact|jev|wiki-index> <action> --root <absolute folder> "
         "[--git <absolute git executable> (impact only)]")


class _Usage(Exception):
    pass


def _parse(args: Sequence[str]) -> tuple[str, str, dict[str, str]]:
    if not args:
        raise _Usage("no action was given")
    group = args[0]
    if group not in GROUPS:
        raise _Usage("unknown group")
    if len(args) < 2 or args[1] not in GROUPS[group]:
        raise _Usage(f"{group} needs one of: {', '.join(GROUPS[group])}")
    options: dict[str, str] = {}
    rest = list(args[2:])
    while rest:
        name = rest.pop(0)
        if name not in OPTIONS or (name == "--git" and group != "impact"):
            raise _Usage("unknown option for this group")
        if name in options:
            raise _Usage(f"{name} was given twice")
        if not rest:
            raise _Usage(f"{name} needs a value")
        options[name] = rest.pop(0)
    if "--root" not in options:
        raise _Usage("--root is required")
    return group, args[1], options


def _bundle_problem() -> str | None:
    """A frozen runner must carry the impact sources: the map's tool identity is their digest."""
    if not getattr(sys, "frozen", False):
        return None
    from oah.impact import mapstore

    source = pathlib.Path(mapstore.__file__).parent / "mapstore.py"
    return None if source.is_file() else f"this build is incomplete: {source} is missing"


def _emit(document: dict[str, Any]) -> None:
    text = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
    if len(text) > OUTPUT_LIMIT:
        text = json.dumps(impact.failure(document.get("action"), "", "OAH_PROJECT_TOOLS_INTERNAL",
                                         "the answer was larger than the output limit").to_json(),
                          ensure_ascii=True, separators=(",", ":"))
    _write(text + "\n")


def _write(text: str) -> None:
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write(text.encode("ascii"))
    else:
        sys.stdout.write(text)
    sys.stdout.flush()


def _extension(group: str, action: str, root: str) -> int:
    from oah import project_tools_extensions as extensions

    folder = pathlib.Path(root)
    operate = extensions.run_jev if group == "jev" else extensions.run_wiki_index
    try:
        if not folder.is_absolute() or not folder.is_dir():
            raise ValueError("project must be an absolute directory")
        answer = operate(action, folder, sys.stdin.buffer)
        result = EXIT_OK if answer["ok"] else EXIT_REFUSED
    except ValueError:
        answer = {"ok": False}
        result = EXIT_USAGE
    except Exception:
        answer = {"ok": False}
        result = EXIT_INTERNAL
    _emit({"schema": 1, "version": oah.__version__, "tool": group, "action": action, "root": root, "result": answer})
    return result


@contextlib.contextmanager
def _stoppable() -> Iterator[None]:
    """While a run is in progress a stop signal raises SystemExit in the main thread, so every pending call
    unwinds through its own cleanup. The previous handlers are put back afterwards."""
    numbers = [getattr(signal, name) for name in STOP_SIGNALS if hasattr(signal, name)]

    def stop(number: int, _frame: object) -> None:
        for other in numbers:  # one stop is enough: a second signal must not cut the cleanup short
            signal.signal(other, signal.SIG_IGN)
        raise SystemExit(128 + number)

    previous: dict[int, int | Callable[[int, FrameType | None], Any] | None] = {}
    try:
        for number in numbers:
            previous[number] = signal.signal(number, stop)
    except ValueError:  # not the main thread: whoever called owns the signals
        pass
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        _write(f"{impact.RUNNER_NAME} {oah.__version__}\n")
        return EXIT_OK
    if args in (["--help"], ["-h"]):
        _write(USAGE + "\n")
        return EXIT_OK
    try:
        group, action, options = _parse(args)
    except _Usage as exc:
        _emit(impact.failure(None, "", "OAH_PROJECT_TOOLS_USAGE", f"{exc}; {USAGE}", tool=None).to_json())
        return EXIT_USAGE
    root = options["--root"]
    with _stoppable():
        if group != "impact":
            return _extension(group, action, root)
        try:
            problem = _bundle_problem()
            if problem:
                raise RuntimeError(problem)
            operation = impact.status if action == "status" else impact.refresh
            outcome = operation(root, selected=True, git=options.get("--git"))
            code = EXIT_OK if outcome.ok else EXIT_REFUSED
        except Exception as exc:  # the caller still gets an answer in the contract's shape
            outcome = impact.failure(action, root, "OAH_PROJECT_TOOLS_INTERNAL", f"{type(exc).__name__}: {exc}")
            code = EXIT_INTERNAL
        _emit(outcome.to_json())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
