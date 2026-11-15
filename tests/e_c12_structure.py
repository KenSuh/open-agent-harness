from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRY_MODULES = ('call.py', 'call_cli.py', 'skills_cli.py', 'capabilities_cli.py', 'skillpack.py',
                 'launchers/native.py', 'launchers/orca.py', 'design_cli.py')


def sources(root=ROOT):
    paths = [root / 'src/oah' / name for name in ENTRY_MODULES]
    paths += list((root / 'src/oah/discovery').glob('*.py'))
    paths += list((root / 'src/oah/capabilities').glob('*.py'))
    return sorted(set(paths))


def catches(root=ROOT):
    found = []

    def walk(node, module, names):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = (*names, node.name)
        if isinstance(node, ast.ExceptHandler):
            found.append((module, '.'.join(names), ast.unparse(node.type) if node.type else 'bare'))
        for child in ast.iter_child_nodes(node):
            walk(child, module, names)

    for path in sources(root):
        walk(ast.parse(path.read_text(encoding='utf-8')), path.relative_to(root / 'src/oah').as_posix(), ())
    return tuple(sorted(found))


def architectural_problems(root=ROOT):
    problems = []
    for path in sources(root):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                names = {part.id for part in ast.walk(node.type) if isinstance(part, ast.Name)} if node.type else set()
                if names & {'RecordRefusal', 'InvalidRecord'} and path.name != 'boundaries.py':
                    problems.append(f'{path.name}:{node.lineno}: final record refusal caught outside the command mapper')
            if not isinstance(node, ast.Call):
                continue
            called = ast.unparse(node.func)
            if path.name != 'records.py' and called in (
                    'records.RecordRefusal', 'records.InvalidRecord',
                    'record_files.RecordRefusal', 'record_files.InvalidRecord'):
                problems.append(f'{path.name}:{node.lineno}: final record refusal constructed outside its owner')
            if called in ('records.read_json', 'records.read_bytes', 'records.read_text') and node.args:
                argument = ast.unparse(node.args[0])
                if any(name in argument for name in ('file_path(', 'prompt_path', 'result_path', 'launch_path')):
                    problems.append(f'{path.name}:{node.lineno}: record path metadata evaluated before the reader')
            if called.endswith('.content') and called.split('.')[0] in ('records', 'record_files'):
                problems.append(f'{path.name}:{node.lineno}: caller-wide content wrapper')
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                    'stat', 'lstat', 'exists', 'is_file', 'read_bytes', 'read_text', 'open'):
                subject = ast.unparse(node.func.value)
                stored = any(word in subject for word in ('file_path(', 'prompt_path', 'result_path',
                    'skills.json', 'prompt.md', 'capabilities.json', 'RECEIVING_NAME', 'OFFER_NAME'))
                if stored and path.name not in ('records.py',):
                    problems.append(f'{path.name}:{node.lineno}: direct stored-record access {called}')
    return problems
