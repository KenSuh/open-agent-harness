#!/usr/bin/env python3
# CI 영향 테스트 선택기 — PR 이 바꾼 파일에서 영향을 받을 수 있는 테스트 모듈만 골라 CI 가 그것만 돌리게 한다.
"""Pick the test modules a pull request can affect: `python tools/affected_tests.py --base SHA --head SHA`.

What runs when, in plain words:

* A pull request runs only the test modules its changes can reach, on as few shards as their measured time needs
  (about 30 minutes each, 1 to 5 shards per OS). A pull request that reaches no test runs one shard with no tests,
  so the install, version, privacy and installer steps of shard 1 still run.
* The whole suite runs on 5 shards per OS when the run was started by hand (any event other than `pull_request`),
  when the base commit is missing or unreadable, when a changed file is shared test infrastructure (listed below),
  when a changed file under `src/` or `tests/` reaches no test, or when anything here goes wrong. Doubt never means
  fewer tests.

Changed files are `git diff --name-only --no-renames <base>...<head>` (since the merge base); a rename counts as its
old and its new path. Every file is read from the head commit with git, never from the working tree and never by
importing it, so the answer depends only on the two commits. A changed file selects:

1. the whole suite when it is shared test infrastructure: tests/helpers.py, tests/__init__.py, anything under
   .github/workflows/, tools/test_shard.py, tools/test_shard_times.json, this file, pyproject.toml (the package
   layout and the console script), .gitattributes (line endings of every checked-out file) and src/oah/__init__.py
   (run by every import of the package);
2. itself, when it is a test module (`tests/**/test*.py` inside the tests package); a deleted one is simply gone;
3. every test module that imports it directly or through a chain of imports (`ast`, absolute and relative imports,
   parent packages, literal `importlib.import_module` names, and `from oah... import` lines written inside strings
   for a child Python). A test module's own chain includes imports inside functions, since it may call anything it
   imports. The tests package every test imports (tests/__init__.py and the helpers it calls at import time) counts
   only through module-level imports, since those helpers only set attributes. tests/helpers.py counts per top-level
   function or class: a test module depends on the helpers it names;
4. when it is under src/, every test module that starts the program as a child (`-m oah`, `runpy` of `oah`, the
   `oah` console script), directly or through a helper or a fake under tests/fakes/ that it names or imports;
5. every test module (or tests/helpers.py function, or fake) whose source names its repo-relative path or its file
   name; a changed non-Python file under src/ (package data) also counts every src/ module that names its file name
   as changed. Fakes and tools/ scripts that a change reaches are followed the same way, by name;
6. nothing else. A changed file under src/ or tests/ that selects no test module means the whole suite; a deleted
   test module and files outside src/ and tests/ (LICENSE, install/*, docs) may select nothing.

The shard count uses the measured times in tools/test_shard_times.json, the data tools/test_shard.py balances
with: a test the file does not list weighs `default_ms`.

Options: `--event NAME` (default pull_request), `--repo DIR` (default the current folder), `--summary` (a readable
report instead of one module per line or `ALL`), `--modules-file PATH` (the selected modules for
`tools/test_shard.py --modules`; removed when the whole suite runs), `--github-output PATH` (appends `whole`,
`count`, `shards` and `args` for a GitHub Actions step). Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import math
import os
import pathlib
import posixpath
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping

WHOLE_SUITE_PATHS = frozenset({
    "tests/helpers.py",
    "tests/__init__.py",
    "tools/test_shard.py",
    "tools/test_shard_times.json",
    "tools/affected_tests.py",
    "pyproject.toml",
    ".gitattributes",
    "src/oah/__init__.py",
})
WHOLE_SUITE_PREFIXES = (".github/workflows/",)
HELPERS = "tests/helpers.py"
PACKAGE_INIT = "tests/__init__.py"
AMBIENT_ROOTS = frozenset({PACKAGE_INIT, HELPERS})
TIMES = "tools/test_shard_times.json"
SHARD_MS = 30 * 60 * 1000
MAX_SHARDS = 5
WHOLE = "ALL"

# Starting the program as a child: `python -m oah`, `[sys.executable, "-m", "oah"...]`, runpy, the console script.
CHILD = re.compile(r"""["']-m["']\s*,\s*["']oah\b|-m\s+oah\b|run_module\(\s*["']oah\b|which\(\s*["']oah["']"""
                   r"""|\[\s*["']oah["']\s*,""")
# `from oah.x import a, b` / `import oah.x` anywhere in the text, such as code handed to a child Python.
TEXT_FROM = re.compile(r"\bfrom[ \t]+(oah(?:\.\w+)*)[ \t]+import[ \t]+\(?([\w \t,]+)")
TEXT_IMPORT = re.compile(r"\bimport[ \t]+(oah(?:\.\w+)*(?:[ \t]*,[ \t]*oah(?:\.\w+)*)*)")
DYNAMIC = frozenset({"import_module", "__import__", "find_spec", "run_module"})


class SelectorError(Exception):
    """Anything that leaves the answer in doubt; the caller runs the whole suite."""


@dataclasses.dataclass(frozen=True)
class Selection:
    whole: bool
    reason: str
    modules: tuple[str, ...]
    total: int
    count: int


def whole_suite(reason: str, total: int = 0) -> Selection:
    return Selection(True, reason, (), total, MAX_SHARDS)


def select(repo: pathlib.Path, base: str, head: str, event: str = "pull_request") -> Selection:
    """The affected test modules of `base...head`, or the whole suite; errors become the whole suite."""
    try:
        return _select(repo, base, head, event)
    except Exception as error:  # noqa: BLE001 - any failure means the whole suite, never fewer tests
        return whole_suite(f"selector error: {type(error).__name__}: {error}")


def _select(repo: pathlib.Path, base: str, head: str, event: str) -> Selection:
    if event != "pull_request":
        return whole_suite(f"the event is {event or 'missing'}, not pull_request")
    if not base:
        return whole_suite("no base commit")
    base_sha = _commit(repo, base, "base")
    head_sha = _commit(repo, head, "head")
    changed = changed_files(repo, base_sha, head_sha)
    tree = Tree.read(repo, head_sha)
    total = len(tree.test_modules)
    for path in changed:
        if path in WHOLE_SUITE_PATHS or path.startswith(WHOLE_SUITE_PREFIXES):
            return whole_suite(f"{path} is shared test infrastructure", total)
    graph = Graph(tree, changed)
    selected: set[str] = set()
    for path in changed:
        found = graph.tests_for(path)
        if not found and path.startswith(("src/", "tests/")) and not tree.is_deleted_test_module(path):
            return whole_suite(f"{path} reaches no test module", total)
        selected |= found
    modules = tuple(sorted(tree.test_modules[path] for path in selected))
    default_ms, times_ms = tree.times()
    weights = [module_weight(module, tree.test_ids(module), default_ms, times_ms) for module in modules]
    return Selection(False, "affected tests", modules, total, shard_count(weights))


def module_weight(module: str, ids: Iterable[str], default_ms: int, times_ms: Mapping[str, int]) -> int:
    """A module's measured time: its listed tests plus `default_ms` for each test the file does not list."""
    prefix = module + "."
    every = set(ids) | {name for name in times_ms if name.startswith(prefix)}
    return sum(times_ms.get(name, default_ms) for name in every)


def shard_count(weights: Iterable[int]) -> int:
    """Shards of about 30 minutes for these module weights, at least 1 and at most 5."""
    return max(1, min(MAX_SHARDS, math.ceil(sum(weights) / SHARD_MS)))


def _git(repo: pathlib.Path, *args: str, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=False)
    if done.returncode != 0:
        message = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SelectorError(f"git {args[0]} failed: {message[-1] if message else done.returncode}")
    return done.stdout


def _commit(repo: pathlib.Path, ref: str, label: str) -> str:
    if ref.startswith("-"):
        raise SelectorError(f"the {label} {ref!r} is not a commit")
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
    except SelectorError:
        raise SelectorError(f"the {label} commit {ref!r} is missing or unreadable") from None


def changed_files(repo: pathlib.Path, base: str, head: str) -> list[str]:
    """Paths changed since the merge base; a rename lists its old and its new path."""
    out = _git(repo, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", "--no-ext-diff", "-z",
               f"{base}...{head}", "--")
    return sorted({name.decode("utf-8", "surrogateescape") for name in out.split(b"\0") if name})


def _scanned(path: str) -> bool:
    if not path.endswith(".py"):
        return False
    return path.startswith(("src/", "tools/")) or (path.startswith("tests/") and not path.startswith("tests/fixtures/"))


class Tree:
    """The head commit's paths, the sources the rules read and the test modules discovery would find."""

    def __init__(self, paths: set[str], blobs: dict[str, bytes]):
        self.paths = paths
        self.blobs = blobs
        self.sources: dict[str, bytes] = {path: data for path, data in blobs.items() if _scanned(path)}
        self.texts = {path: data.decode("utf-8", "replace") for path, data in self.sources.items()}
        self.test_modules = {path: name for path in sorted(paths)
                             if (name := self._test_module(path, paths)) is not None}

    @classmethod
    def read(cls, repo: pathlib.Path, head: str) -> "Tree":
        listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", head)
        paths: set[str] = set()
        wanted: dict[str, str] = {}
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            meta, _, raw = entry.partition(b"\t")
            mode, kind, sha = meta.decode().split()
            path = raw.decode("utf-8", "surrogateescape")
            paths.add(path)
            if kind == "blob" and mode in ("100644", "100755") and (_scanned(path) or path == TIMES):
                wanted[path] = sha
        return cls(paths, _blobs(repo, wanted))

    @staticmethod
    def _test_module(path: str, paths: set[str]) -> str | None:
        parts = path.split("/")
        if parts[0] != "tests" or len(parts) < 2 or not path.endswith(".py"):
            return None
        stem = parts[-1][:-3]
        if not stem.startswith("test") or not stem.isidentifier():
            return None
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) + "/__init__.py" not in paths or not parts[depth - 1].isidentifier():
                return None
        return ".".join(parts[:-1] + [stem])

    def is_deleted_test_module(self, path: str) -> bool:
        return path not in self.paths and self._test_module(path, self.paths | {path}) is not None

    def times(self) -> tuple[int, dict[str, int]]:
        try:
            data = json.loads(self.blobs[TIMES].decode("utf-8"))
            default_ms, times_ms = data["default_ms"], data["times_ms"]
        except (KeyError, ValueError, TypeError) as error:
            raise SelectorError(f"cannot read {TIMES}: {error}") from None
        if type(default_ms) is not int or not isinstance(times_ms, dict) or \
                not all(type(value) is int for value in times_ms.values()):
            raise SelectorError(f"bad timing data in {TIMES}")
        return default_ms, times_ms

    def test_ids(self, module: str) -> list[str]:
        """`module.Class.test_x` for each test method of each top-level class, read with ast."""
        path = next(path for path, name in self.test_modules.items() if name == module)
        tree = ast.parse(self.sources[path], filename=path)
        return [f"{module}.{node.name}.{item.name}" for node in tree.body if isinstance(node, ast.ClassDef)
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test")]


def _blobs(repo: pathlib.Path, wanted: Mapping[str, str]) -> dict[str, bytes]:
    order = sorted(wanted)
    if not order:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{wanted[path]}\n" for path in order).encode())
    blobs: dict[str, bytes] = {}
    at = 0
    for path in order:
        end = out.index(b"\n", at)
        header = out[at:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise SelectorError(f"cannot read {path} from the head commit")
        size = int(header[2])
        blobs[path] = out[end + 1:end + 1 + size]
        at = end + 1 + size + 1
    return blobs


class Graph:
    """Who depends on what. Each test module's dependency set is computed forward and then matched to the changes.

    An import at module level is eager: it runs when its module is imported. An import inside a function body (or
    written into a string for a child Python) is lazy: it runs when that function is called. A test module may call
    anything it imports, so its own imports follow both kinds through any chain. The tests package (tests/__init__.py
    and the helpers it runs at import time) is shared by every test and follows eager imports only, because those
    helpers only set attributes of what they import. tests/helpers.py counts per top-level function or class: a
    test module depends on the helpers it names.
    """

    def __init__(self, tree: Tree, changed: Iterable[str]):
        self.tree = tree
        known = tree.paths | set(changed)
        self.known = known
        self.src_tops = sorted({path.split("/")[1] for path in known if path.startswith("src/") and path.count("/") >= 2
                                and path.split("/")[1].isidentifier()})
        self.names = {name: path for path in tree.sources if (name := self._module_name(path)) is not None}
        self.script_dirs = sorted({posixpath.dirname(path) for path in tree.sources
                                   if path.startswith("tests/") and self._module_name(path) is None} | {"tools"})
        self.scripts = {path for path in tree.sources if posixpath.dirname(path) in self.script_dirs}
        self.texts: dict[str, str] = {}
        self.eager: dict[str, set[str]] = {}
        self.lazy: dict[str, set[str]] = {}
        for path in sorted(tree.sources):
            if path == HELPERS:
                self._add_helpers(path)
            else:
                self.texts[path] = tree.texts[path]
                self.eager[path], self.lazy[path] = self._imports(path, [ast.parse(tree.sources[path], filename=path)],
                                                                  tree.texts[path])
        self._add_name_edges()
        self.child_nodes = {node for node in self.texts if self._test_side(node) and CHILD.search(self.texts[node])}
        self.ambient = self._reach({PACKAGE_INIT}, eager_only=True, stop=frozenset())
        self.depends = {path: self._reach(self._direct(path), eager_only=False, stop=AMBIENT_ROOTS)
                        | self.ambient | {path} for path in tree.test_modules}

    # Module names and import resolution.

    def _module_name(self, path: str) -> str | None:
        parts = path[:-3].split("/")
        if parts[0] == "src" and len(parts) >= 2:
            parts = parts[1:]
        elif parts[0] == "tools" and len(parts) == 2:
            pass
        elif parts[0] == "tests":
            for depth in range(1, len(parts)):
                if "/".join(parts[:depth]) + "/__init__.py" not in self.tree.paths:
                    return None
        else:
            return None
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts and all(part.isidentifier() for part in parts) else None

    def _test_side(self, node: str) -> bool:
        return node.startswith("tests/") or node in self.scripts

    def _candidates(self, name: str) -> set[str]:
        """The files importing `name` runs: the module or package and every parent package's __init__.py."""
        parts = name.split(".")
        if parts[0] in self.src_tops:
            root = "src/"
        elif parts[0] in ("tests", "tools"):
            root = ""
        else:
            return set()
        found: set[str] = set()
        for depth in range(1, len(parts) + 1):
            stem = root + "/".join(parts[:depth])
            found |= {stem + ".py", stem + "/__init__.py"}
        return found

    def _script(self, path: str, name: str) -> set[str]:
        """A top-level import of a script in a script folder (tests/fakes/, tools/) from code under tests/ or tools/."""
        if not path.startswith(("tests/", "tools/")):
            return set()
        top = name.split(".")[0]
        return {f"{folder}/{top}.py" for folder in self.script_dirs if f"{folder}/{top}.py" in self.known}

    def _package(self, path: str, level: int) -> str:
        name = self._module_name(path)
        if name is None:
            raise SelectorError(f"{path} uses a relative import outside a package")
        parts = name.split(".") if path.endswith("/__init__.py") else name.split(".")[:-1]
        if not parts or level - 1 > len(parts) - 1:
            raise SelectorError(f"{path} has a relative import beyond its top package")
        return ".".join(parts[:len(parts) - (level - 1)])

    def _imports(self, path: str, roots: list[ast.AST], text: str,
                 all_eager: bool = False) -> tuple[set[str], set[str]]:
        """(eager, lazy) files that the code under `roots` imports."""
        names: dict[bool, set[str]] = {False: set(), True: set()}
        package = self._module_name(path)
        if package is not None:
            names[False].add(package)
        for node, lazy in _walk(roots):
            lazy = lazy and not all_eager
            if isinstance(node, ast.Import):
                names[lazy] |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                base = (node.module or "") if node.level == 0 else self._package(path, node.level)
                if node.level and node.module:
                    base = f"{base}.{node.module}"
                names[lazy].add(base)
                names[lazy] |= {f"{base}.{alias.name}" for alias in node.names if alias.name != "*"}
            elif isinstance(node, ast.Call) and node.args and self._call_name(node.func) in DYNAMIC:
                names[lazy] |= self._dynamic(node.args[0])
        for match in TEXT_FROM.finditer(text):
            names[not all_eager].add(match.group(1))
            names[not all_eager] |= {f"{match.group(1)}.{word.strip()}" for word in match.group(2).split(",")
                                     if word.strip()}
        for match in TEXT_IMPORT.finditer(text):
            names[not all_eager] |= {word.strip() for word in match.group(1).split(",")}
        found = []
        for kind in (False, True):
            files: set[str] = set()
            for name in sorted(n for n in names[kind] if n):
                files |= self._candidates(name) | self._script(path, name)
            files.discard(path)
            found.append(files)
        return found[0], found[1] - found[0]

    @staticmethod
    def _call_name(func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        return func.attr if isinstance(func, ast.Attribute) else None

    def _dynamic(self, arg: ast.expr) -> set[str]:
        """A literal name, every known module under a literal f-string prefix, or else the whole package."""
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return {arg.value}
        prefix = ""
        if isinstance(arg, ast.JoinedStr) and arg.values and isinstance(arg.values[0], ast.Constant):
            prefix = str(arg.values[0].value)
        if prefix and prefix.split(".")[0] not in self.src_tops:
            return {name for name in self.names if name.startswith(prefix)}
        return {name for name in self.names if name.split(".")[0] in self.src_tops and name.startswith(prefix)}

    def _add_helpers(self, path: str) -> None:
        """tests/helpers.py: module-level code is one node; each top-level function or class is a node of its own."""
        text = self.tree.texts[path]
        tree = ast.parse(self.tree.sources[path], filename=path)
        defs = [node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        top = [node for node in tree.body if node not in defs]
        # Decorators, default values and base classes run at import time, so they belong to the module-level node.
        eager_parts = [part for node in defs for part in _import_time_parts(node)]
        module_text = "\n".join(ast.get_source_segment(text, node) or "" for node in [*top, *eager_parts])
        self.texts[path] = module_text
        self.eager[path], self.lazy[path] = self._imports(path, [*top, *eager_parts], module_text)
        for node in defs:
            key = f"{path}::{node.name}"
            segment = ast.get_source_segment(text, node) or ""
            self.texts[key] = segment
            eager, lazy = self._imports(path, [node], segment, all_eager=True)
            self.eager[key] = (eager | lazy | {path}) - {key}
            self.lazy[key] = set()

    def _add_name_edges(self) -> None:
        """Naming a helper is an eager edge to it; naming a script's file name is a lazy edge to the script."""
        helpers = sorted(node for node in self.texts if node.startswith(HELPERS + "::"))
        words = {helper: re.compile(rf"\b{re.escape(helper.split('::', 1)[1])}\b") for helper in helpers}
        for node in sorted(self.texts):
            if not self._test_side(node):
                continue
            text = self.texts[node]
            self.eager[node] |= {helper for helper in helpers if helper != node and words[helper].search(text)}
            self.lazy[node] |= {script for script in self.scripts
                                if script != node and posixpath.basename(script) in text} - self.eager[node]

    def _direct(self, path: str) -> set[str]:
        return self.eager.get(path, set()) | self.lazy.get(path, set())

    def _reach(self, start: Iterable[str], eager_only: bool, stop: frozenset[str]) -> set[str]:
        reached = set(start)
        queue = sorted(reached - stop)
        while queue:
            node = queue.pop()
            more = self.eager.get(node, set()) if eager_only else self._direct(node)
            for other in sorted(more - reached):
                reached.add(other)
                if other not in stop:
                    queue.append(other)
        return reached

    # Selection.

    def tests_for(self, path: str) -> set[str]:
        """The test module paths at head that the changed `path` can affect (rules 2 to 5)."""
        seeds = {path}
        name = posixpath.basename(path)
        if path.startswith("src/") and not path.endswith(".py"):
            seeds |= {node for node, text in self.texts.items() if node.startswith("src/") and name in text}
        mentioners = {node for node, text in self.texts.items() if self._test_side(node) and name in text}
        found = set()
        for test, depends in self.depends.items():
            if depends & seeds or depends & mentioners or (path.startswith("src/") and depends & self.child_nodes):
                found.add(test)
        return found


def _walk(roots: Iterable[ast.AST]):
    """Every node under `roots` with whether it runs only when a function is called (a body or a lambda)."""
    stack = [(root, False) for root in roots]
    while stack:
        node, lazy = stack.pop()
        yield node, lazy
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            body = node.body if isinstance(node.body, list) else [node.body]
            stack += [(child, True) for child in body]
            stack += [(child, lazy) for child in _import_time_parts(node)]
        else:
            stack += [(child, lazy) for child in ast.iter_child_nodes(node)]


def _import_time_parts(node: ast.AST) -> list[ast.AST]:
    """The parts of a definition that run when the definition does: decorators, defaults, bases."""
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]
    parts = [*getattr(node, "decorator_list", []), *node.args.defaults]
    return parts + [value for value in node.args.kw_defaults if value is not None]


def _summary(selection: Selection) -> str:
    if selection.whole:
        return f"whole suite: {selection.reason} ({selection.count} shards)\n"
    plural = "" if selection.count == 1 else "s"
    head = f"selected {len(selection.modules)} of {selection.total} test modules ({selection.count} shard{plural})"
    if not selection.modules:
        return head + ": no affected tests\n"
    return head + "\n" + "".join(f"  {module}\n" for module in selection.modules)


def _write_modules(path: pathlib.Path, selection: Selection) -> None:
    if selection.whole:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(f"{module}\n" for module in selection.modules), encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _github_output(selection: Selection, modules_file: pathlib.Path | None) -> str:
    args = "" if selection.whole or modules_file is None else f"--modules {modules_file}"
    return (f"whole={'true' if selection.whole else 'false'}\ncount={selection.count}\n"
            f"shards={json.dumps(list(range(1, selection.count + 1)))}\nargs={args}\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python tools/affected_tests.py",
                                     description="Pick the test modules a pull request can affect.")
    parser.add_argument("--base", required=True, help="the pull request's base commit (empty: whole suite)")
    parser.add_argument("--head", default="HEAD", help="the pull request's head commit")
    parser.add_argument("--event", default="pull_request", help="the GitHub event name")
    parser.add_argument("--repo", default=".", type=pathlib.Path, help="the repository folder")
    parser.add_argument("--summary", action="store_true", help="print a readable report")
    parser.add_argument("--modules-file", type=pathlib.Path, help="write the selected modules here")
    parser.add_argument("--github-output", type=pathlib.Path, help="append step outputs to this file")
    options = parser.parse_args(argv)
    selection = select(options.repo, options.base, options.head, options.event)
    if options.modules_file is not None:
        _write_modules(options.modules_file, selection)
    if options.github_output is not None:
        with open(options.github_output, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(_github_output(selection, options.modules_file))
    if options.summary:
        sys.stdout.write(_summary(selection))
    else:
        sys.stdout.write(f"{WHOLE}\n" if selection.whole else "".join(f"{module}\n" for module in selection.modules))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
