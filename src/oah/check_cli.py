# 이식 가능한 검사 명령 — 게이트 CHECK 줄에서 grep·파이프 없이 쓰는 oah check exists/contains/absent/json 을 맡는다.
"""check: portable assertions for gate CHECK lines.

Each subcommand prints `OAH_CHECK_OK <kind> ...` and exits 0 on success. A failure prints
`OAH_CHECK_FAILED: ...` on stderr and exits 1; usage errors exit 2. The failure text never contains
the success token, so an `EXPECT: OAH_CHECK_OK` gate cannot pass on a failure message. Paths are
relative to the current directory and files are read as UTF-8.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

COMMANDS = ("check",)
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
OK = "OAH_CHECK_OK"


class CheckFailed(Exception):
    pass


class CheckUsage(Exception):
    pass


def register(commands: argparse._SubParsersAction) -> None:
    check = commands.add_parser("check", help="portable assertions for gate CHECK lines")
    kinds = check.add_subparsers(dest="check_kind", metavar="<kind>", required=True)

    exists = kinds.add_parser("exists", help="every path exists")
    exists.add_argument("paths", nargs="+")

    contains = kinds.add_parser("contains", help="a file contains text")
    contains.add_argument("path")
    contains.add_argument("text")
    contains.add_argument("--regex", action="store_true", help="treat TEXT as a Python regular expression")

    absent = kinds.add_parser("absent", help="a path does not exist, or a file does not contain text")
    absent.add_argument("path")
    absent.add_argument("text", nargs="?")
    absent.add_argument("--regex", action="store_true", help="treat TEXT as a Python regular expression")

    as_json = kinds.add_parser("json", help="a file is valid JSON, optionally with a value at a key path")
    as_json.add_argument("path")
    as_json.add_argument("--key", help="dot-separated path such as items.0.name")
    as_json.add_argument("--equals", help="the expected value, written as JSON (\"text\", 3, true, null)")


def _read(path: str) -> str:
    target = pathlib.Path(path)
    if not target.is_file():
        raise CheckFailed(f"{path} does not exist or is not a file; fix the path in the CHECK line")
    try:
        return target.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckFailed(f"{path} is not UTF-8 text ({exc.reason}); save it as UTF-8") from exc
    except OSError as exc:
        raise CheckFailed(f"cannot read {path}: {exc.strerror or exc}") from exc


def _found(text: str, pattern: str, regex: bool) -> bool:
    if not regex:
        return pattern in text
    try:
        return re.search(pattern, text.replace("\r\n", "\n"), re.MULTILINE) is not None
    except re.error as exc:
        raise CheckUsage(f"invalid regular expression {pattern!r}: {exc}") from exc


def _exists(args: argparse.Namespace) -> str:
    missing = [path for path in args.paths if not pathlib.Path(path).exists()]
    if missing:
        raise CheckFailed(f"exists: missing {', '.join(missing)}; create it or fix the path in the CHECK line")
    return f"exists {len(args.paths)} path(s)"


def _contains(args: argparse.Namespace) -> str:
    if not _found(_read(args.path), args.text, args.regex):
        raise CheckFailed(f"contains: {args.path} does not contain {args.text!r}")
    return f"contains {args.path}"


def _absent(args: argparse.Namespace) -> str:
    if args.text is None:
        if pathlib.Path(args.path).exists() or pathlib.Path(args.path).is_symlink():
            raise CheckFailed(f"absent: {args.path} exists; remove it or fix the CHECK line")
        return f"absent {args.path}"
    if _found(_read(args.path), args.text, args.regex):
        raise CheckFailed(f"absent: {args.path} still contains {args.text!r}")
    return f"absent {args.text!r} in {args.path}"


def _lookup(value: object, key: str) -> object:
    for part in key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part) and int(part) < len(value):
            value = value[int(part)]
        else:
            raise CheckFailed(f"json: key {key} is not present (stopped at {part!r})")
    return value


def _json(args: argparse.Namespace) -> str:
    expected = None
    if args.equals is not None:
        try:
            expected = json.loads(args.equals)
        except ValueError as exc:
            raise CheckUsage(f"--equals must be JSON, such as '\"text\"', 3 or true ({exc})") from exc
    try:
        data = json.loads(_read(args.path))
    except ValueError as exc:
        raise CheckFailed(f"json: {args.path} is not valid JSON ({exc})") from exc
    value = _lookup(data, args.key) if args.key else data
    if args.equals is not None and (value != expected or type(value) is not type(expected)):
        where = args.key or "the document"
        raise CheckFailed(f"json: {where} is {json.dumps(value)[:200]}, expected {json.dumps(expected)[:200]}")
    return f"json {args.path}" + (f" {args.key}" if args.key else "")


HANDLERS = {"exists": _exists, "contains": _contains, "absent": _absent, "json": _json}


def run(args: argparse.Namespace) -> int:
    try:
        detail = HANDLERS[args.check_kind](args)
    except CheckFailed as exc:
        print(f"OAH_CHECK_FAILED: {str(exc).replace(OK, 'OAH_CHECK_(ok)')}", file=sys.stderr)
        return EXIT_FAILED
    except CheckUsage as exc:
        print(f"OAH_CHECK_USAGE: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"{OK} {detail}")
    return EXIT_OK
