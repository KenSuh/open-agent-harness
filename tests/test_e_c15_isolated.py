from __future__ import annotations

import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent
MUTATORS = frozenset({'add', 'append', 'extend', 'update', 'clear', 'pop', 'popitem',
                      'remove', 'discard', 'setdefault', 'insert', 'sort', 'reverse'})


def shared_test_writes(tree: ast.Module) -> list[tuple[int, str]]:
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    module_names = {node.id for statement in tree.body if isinstance(statement, (ast.Assign, ast.AnnAssign))
                    for node in ast.walk(statement) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    writes: list[tuple[str, str, int, str]] = []
    readers: dict[str, set[str]] = {}
    for case in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        methods = {node.name: node for node in case.body if isinstance(node, ast.FunctionDef)}
        shared = {node.attr for node in ast.walk(methods.get('setUpClass', ast.Pass()))
                  if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                  and isinstance(node.value, ast.Name) and node.value.id == 'cls'}
        shared.update(node.id for statement in case.body if isinstance(statement, (ast.Assign, ast.AnnAssign))
                      for node in ast.walk(statement) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
        pending = [(name, name) for name in methods if name.startswith('test_')]
        seen: set[tuple[str, str]] = set()
        while pending:
            test, name = pending.pop()
            if (test, name) in seen:
                continue
            seen.add((test, name))
            function = methods.get(name, functions.get(name))
            if function is None:
                continue
            global_names = {name for node in ast.walk(function) if isinstance(node, ast.Global) for name in node.names}
            local_names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)
                           and isinstance(node.ctx, ast.Store)} - global_names
            local_names.update(arg.arg for arg in ast.walk(function) if isinstance(arg, ast.arg))
            aliases = {'cls', case.name, 'self.__class__', 'type(self)'}
            for node in ast.walk(function):
                if isinstance(node, ast.Assign) and ast.unparse(node.value) in aliases:
                    aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))

            def shared_target(node: ast.expr, mutation: bool = False) -> str | None:
                while isinstance(node, ast.Subscript):
                    node = node.value
                if isinstance(node, ast.Name):
                    return 'module.' + node.id if node.id in global_names | (module_names - local_names) else None
                if not isinstance(node, ast.Attribute):
                    return None
                owner = ast.unparse(node.value)
                if owner in aliases or owner == 'self' and node.attr in shared and mutation:
                    return case.name + '.' + node.attr
                while isinstance(node, (ast.Attribute, ast.Subscript)):
                    node = node.value
                return 'module.' + node.id if isinstance(node, ast.Name) and node.id in module_names - local_names else None

            ignored: set[ast.AST] = set()
            for node in ast.walk(function):
                targets = []
                mutation = False
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                elif isinstance(node, ast.Delete):
                    targets = node.targets
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        if node.func.attr in methods and ast.unparse(node.func.value) in aliases | {'self'}:
                            pending.append((test, node.func.attr))
                        if node.func.attr in MUTATORS:
                            targets = [node.func.value]
                            mutation = True
                    elif isinstance(node.func, ast.Name):
                        if node.func.id in functions:
                            pending.append((test, node.func.id))
                        if node.func.id in ('setattr', 'delattr') and node.args \
                                and ast.unparse(node.args[0]) in aliases and len(node.args) > 1 \
                                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                            writes.append((case.name + '.' + node.args[1].value,
                                           case.name + '.' + test, node.lineno, ast.unparse(node)))
                for target in targets:
                    state = shared_target(target, mutation)
                    if state is not None:
                        writes.append((state, case.name + '.' + test, target.lineno, ast.unparse(node)))
                        ignored.update(ast.walk(target))
            for node in ast.walk(function):
                if node not in ignored and isinstance(node, (ast.Attribute, ast.Name)) \
                        and isinstance(node.ctx, ast.Load):
                    state = shared_target(node, mutation=True)
                    if state is not None:
                        readers.setdefault(state, set()).add(case.name + '.' + test)
    return sorted({(line, text) for state, writer, line, text in writes
                   if readers.get(state, set()) - {writer}})


class IsolatedTestsGuard(unittest.TestCase):
    def test_e_c15_test_methods_do_not_write_shared_coverage_state(self):
        self.maxDiff = None
        violations = []
        for path in sorted(ROOT.glob('*.py')):
            tree = ast.parse(path.read_bytes(), filename=str(path))
            violations.extend(f'{path.name}:{line}: {text}' for line, text in shared_test_writes(tree))
        self.assertEqual(violations, [], 'E-C15: a test reads class/module state written by a sibling test')


if __name__ == '__main__':
    unittest.main()
