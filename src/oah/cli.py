# oah 명령 진입점 — 파이썬 버전을 먼저 확인한 뒤 출력 인코딩을 안전하게 하고, 찾아낸 명령 모듈로 넘긴다.
from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from types import ModuleType

MIN_PYTHON = (3, 11)
COMMAND_MODULE_ATTRIBUTES = ("register", "COMMANDS", "run")


class _RootParser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        argv = list(sys.argv[1:] if args is None else args)
        # Stop at the first registered command, so another command's value "land"
        # cannot change its parser's established diagnostics.
        commands = next((action.choices for action in self._actions
                         if isinstance(action, argparse._SubParsersAction)), {})
        command = next((arg for arg in argv if arg in commands), None)
        self._landing = command == "land"
        self._landing_json = "--json" in argv
        if self._landing and any(arg not in ("-h", "--help", "--version") for arg in argv[:argv.index("land")]):
            self.error("")
        return super().parse_known_args(argv, namespace)

    def error(self, message):
        if getattr(self, "_landing", False):
            from oah.land_cli import EXIT_USAGE, USAGE_MESSAGE, _done

            _done("OAH_USAGE", USAGE_MESSAGE, EXIT_USAGE, self._landing_json)
            self.exit(EXIT_USAGE)
        super().error(message)


def command_modules() -> list[ModuleType]:
    """Every oah/<name>_cli.py module, sorted by module name."""
    import oah

    names = sorted(info.name for info in pkgutil.iter_modules(oah.__path__) if info.name.endswith("_cli"))
    return [importlib.import_module(f"oah.{name}") for name in names]


def _command_table(modules: list[ModuleType]) -> dict[str, ModuleType]:
    table: dict[str, ModuleType] = {}
    for module in modules:
        missing = [name for name in COMMAND_MODULE_ATTRIBUTES if not hasattr(module, name)]
        if missing:
            raise RuntimeError(f"oah command module {module.__name__} is missing {', '.join(missing)}")
        for command in module.COMMANDS:
            if command in table:
                raise RuntimeError(f"oah command {command!r} is claimed by both "
                                   f"{table[command].__name__} and {module.__name__}")
            table[command] = module
    return table


def _group_title(module: ModuleType) -> str:
    doc = (module.__doc__ or "").strip()
    if doc:
        return doc.splitlines()[0].rstrip(".")
    return module.__name__.rpartition(".")[2].removesuffix("_cli")


def _build(modules: list[ModuleType]) -> argparse.ArgumentParser:
    from oah import __version__

    _command_table(modules)
    groups = "\n".join(f"  {_group_title(module)}: {', '.join(module.COMMANDS)}" for module in modules)
    parser = _RootParser(
        prog="oah",
        description="open-agent-harness: run Claude Code and Codex CLI as a disciplined team on your repository.",
        epilog=f"command groups:\n{groups}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"oah {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>", parser_class=argparse.ArgumentParser)
    for module in modules:
        before = set(commands.choices)
        try:
            module.register(commands)
        except (argparse.ArgumentError, ValueError) as exc:
            if isinstance(exc, ValueError) and not str(exc).startswith(
                    ("conflicting subparser: ", "conflicting subparser alias: ")):
                raise
            raise RuntimeError(f"oah command module {module.__name__} failed to register: {exc}") from exc
        added = set(commands.choices) - before
        if added != set(module.COMMANDS):
            raise RuntimeError(f"oah command module {module.__name__} registered {sorted(added)} "
                               f"but COMMANDS claims {sorted(module.COMMANDS)}")
    return parser


def build_parser() -> argparse.ArgumentParser:
    return _build(command_modules())


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write("oah needs Python 3.11 or newer; this is Python %d.%d\n" % sys.version_info[:2])
        return 2
    from oah.console import configure_stdio

    configure_stdio()
    modules = command_modules()
    parser = _build(modules)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return _command_table(modules)[args.command].run(args)
