# graphify 연결 — 설치돼 있으면 graphify 를 저장소 루트에서 그대로 실행하고, 없으면 설치 안내를 낸다. 그래프를 스스로 다시 만들지 않는다.
from __future__ import annotations

import pathlib
from typing import Sequence

from oah.engines.proc import RunResult, resolve_command, run_command

SUBCOMMANDS = ("update", "query", "path", "explain", "report")
INSTALL_HINT = "uv tool install graphifyy"
REPORT = pathlib.PurePosixPath("graphify-out/GRAPH_REPORT.md")
TIMEOUTS = {"update": 3600.0}
DEFAULT_TIMEOUT = 600.0
EXIT_MISSING, EXIT_ENVIRONMENT = 1, 69


class GraphError(Exception):
    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _unavailable() -> GraphError:
    return GraphError("OAH_GRAPH_UNAVAILABLE", f"graphify is not installed. Install it with `{INSTALL_HINT}`, "
                      "then run `oah graph update` to build the graph. Everything else in oah works without it.",
                      EXIT_ENVIRONMENT)


def graphify_argv(search_path: str | None = None) -> tuple[str, ...]:
    """The argv that runs graphify; raises OAH_GRAPH_UNAVAILABLE (or the batch-shim refusal)."""
    found = resolve_command("graphify", search_path)
    if found is None:
        raise _unavailable()
    return found.argv


def arguments(command: str, extra: Sequence[str]) -> list[str]:
    """graphify's argv for one of SUBCOMMANDS other than report. `update` defaults to the repository root."""
    if command not in SUBCOMMANDS or command == "report":
        raise ValueError(f"not a graphify pass-through command: {command}")
    if command == "update" and not extra:
        return ["update", "."]
    return [command, *extra]


def run_graph(root: pathlib.Path, command: str, extra: Sequence[str], *,
              search_path: str | None = None) -> RunResult:
    """Run graphify from the repository root. Only `oah graph update` ever rebuilds the graph."""
    argv = graphify_argv(search_path) + tuple(arguments(command, extra))
    return run_command(argv, timeout=TIMEOUTS.get(command, DEFAULT_TIMEOUT), cwd=str(root))


def report(root: pathlib.Path, *, search_path: str | None = None) -> str:
    """graphify's report for this repository, read from graphify-out/GRAPH_REPORT.md."""
    path = pathlib.Path(root) / REPORT
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        graphify_argv(search_path)
        raise GraphError("OAH_GRAPH_NO_REPORT", f"{REPORT} does not exist yet. Run `oah graph update` to build "
                         "the graph, then retry.", EXIT_MISSING) from None
