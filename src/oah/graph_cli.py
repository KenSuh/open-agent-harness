# graph 명령 — oah graph update/query/path/explain/report 를 graphify 로 넘기고 출력과 종료 코드를 그대로 돌려준다.
from __future__ import annotations

import argparse
import pathlib
import sys

from oah.engines.proc import UnsupportedBatchShim
from oah.graph import SUBCOMMANDS, GraphError, report, run_graph
from oah.paths import GitMissing, NotARepo, repo_info

EXIT_OK, EXIT_ENVIRONMENT = 0, 69
COMMANDS = ("graph",)
HELP = {
    "update": "re-extract code and update the graph (default path: the repository root)",
    "query": 'answer a question from the graph: oah graph query "<question>"',
    "path": 'shortest path between two nodes: oah graph path "A" "B"',
    "explain": 'explain a node and its neighbours: oah graph explain "X"',
    "report": "print graphify-out/GRAPH_REPORT.md",
}


class _GraphParser(argparse.ArgumentParser):
    """Pass-through commands own their entire argv, including leading options and `--`."""

    def parse_known_args(self, args=None, namespace=None):
        if self.get_default("graph_command") in SUBCOMMANDS[:-1]:
            namespace = namespace if namespace is not None else argparse.Namespace()
            namespace.args = list(sys.argv[1:] if args is None else args)
            return namespace, []
        return super().parse_known_args(args, namespace)


def register(commands: argparse._SubParsersAction) -> None:
    graph = commands.add_parser("graph", help="use the graphify knowledge graph when graphify is installed")
    sub = graph.add_subparsers(dest="graph_command", metavar="<graph command>", required=True,
                              parser_class=_GraphParser)
    for name in SUBCOMMANDS:
        command = sub.add_parser(name, help=HELP[name])
        if name != "report":
            command.set_defaults(graph_command=name)
            command.add_argument("args", nargs=argparse.REMAINDER, help="passed to graphify unchanged")


def run(args: argparse.Namespace) -> int:
    try:
        root = repo_info(pathlib.Path.cwd()).root
        if args.graph_command == "report":
            sys.stdout.write(report(root))
            return EXIT_OK
        result = run_graph(root, args.graph_command, args.args)
    except GraphError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    except (UnsupportedBatchShim, NotARepo, GitMissing) as exc:
        print(exc, file=sys.stderr)
        return EXIT_ENVIRONMENT
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print(f"OAH_GRAPH_TIMEOUT: graphify {args.graph_command} did not finish in time and was stopped; retry, "
              "or run graphify directly for very large repositories", file=sys.stderr)
        return EXIT_ENVIRONMENT
    return result.returncode if result.returncode is not None else EXIT_ENVIRONMENT
