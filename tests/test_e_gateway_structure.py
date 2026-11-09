from __future__ import annotations

import ast
import pathlib
import unittest


def dotted(node: ast.expr) -> str:
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(value=value, attr=name):
            return dotted(value) + "." + name
        case _:
            return ""


def request_violations(text: str, *, gateway_module: bool = False) -> list[int]:
    tree = ast.parse(text)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update({item.asname or item.name.split(".")[0]: item.name for item in node.names})
        elif isinstance(node, ast.ImportFrom):
            imports.update({item.asname or item.name: f"{node.module}.{item.name}" for item in node.names})

    def qualified(node):
        name = dotted(node)
        first, sep, tail = name.partition(".")
        return imports.get(first, first) + (sep + tail if sep else "")

    def through_gateway(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.Call) and qualified(node.func) in (
                    "oah.discovery.gateway.send", "gateway.send", *(["send"] if gateway_module else [])):
                return True
            if isinstance(node, ast.If) and dotted(node.test) == "":
                test = node.test
                if isinstance(test, ast.Compare) and isinstance(test.left, ast.Attribute) \
                        and test.left.attr == "rubric" and len(test.ops) == 1 and isinstance(test.ops[0], ast.In) \
                        and len(test.comparators) == 1 and dotted(test.comparators[0]) == "E_RUBRICS":
                    branch = next((branch for branch in node.orelse if branch is original or original in ast.walk(branch)),
                                  None)
                    if branch is not None:
                        return True
        return False

    def owner(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    violations = []
    for original in ast.walk(tree):
        if not isinstance(original, ast.Call):
            continue
        name = qualified(original.func)
        leaf = name.rsplit(".", 1)[-1]
        provider_judge = leaf == "judge" and "provider" in name.lower()
        network = dotted(original.func).split(".")[0] in imports and name.startswith((
            "urllib.request.", "http.client.", "requests.", "httpx.", "httpx2.", "aiohttp.", "socket."))
        transport = leaf in ("GETTER", "getter", "git_url", "score", "urlopen", "urlretrieve") \
            or provider_judge or network or name.endswith("web.get")
        function = owner(original)
        if name in ("oah.rerank.rerank", "rerank.rerank"):
            adapted = function is not None and any(isinstance(n, ast.Call) and qualified(n.func) in (
                "oah.discovery.gateway.SkillOrderProvider", "gateway.SkillOrderProvider") for n in ast.walk(function))
            forwarded = any(k.arg == "providers" and dotted(k.value) == "providers" for k in original.keywords)
            transport = not (adapted and forwarded)
        if name in ("oah.skillsel.choose", "skillsel.choose", "oah.skillsel.rerank_skills", "skillsel.rerank_skills"):
            transport = not any(k.arg == "admitted" and dotted(k.value) == "admitted" for k in original.keywords)
        if leaf in ("check_one", "refresh_for"):
            guarded = function is not None and all(any(isinstance(n, ast.FunctionDef) and n.name == callback
                and any(isinstance(c, ast.Call) and qualified(c.func) in ("oah.discovery.gateway.send", "gateway.send")
                        for c in ast.walk(n)) for n in ast.walk(function)) for callback in ("getter", "git_url"))
            transport = not guarded
        if transport and not through_gateway(original):
            violations.append(original.lineno)
    return violations


class GatewayStructureTest(unittest.TestCase):
    def test_e_transport_sites_are_inside_the_gateway(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src/oah"
        paths = set((root / "discovery").rglob("*.py")) | set((root / "capabilities").rglob("*.py"))
        paths.update(root / path for path in ("skillpack.py", "skillsel.py", "skills_cli.py", "call.py",
            "rerank/judge.py", "rerank/judgement_policy.py", "capabilities_cli.py", "design_cli.py", "call_cli.py",
            "design/stages.py", "design/coverage.py", "briefs.py", "init.py", "launchers/native.py",
            "launchers/orca.py", "launchers/__init__.py"))
        violations = {str(p.relative_to(root)): rows for p in sorted(paths)
                      if (rows := request_violations(p.read_text(encoding="utf-8"),
                                                    gateway_module=p == root / "discovery/gateway.py"))}
        self.assertEqual(violations, {})
        self.assertTrue((root / "discovery/gateway.py").is_file())

    def test_a_new_direct_or_aliased_transport_site_is_rejected(self):
        cases = ("GETTER(url)", "provider.score(kind, query, items)", "provider.judge(state, questions)",
                 "observer.getter(url)", "observer.git_url(locator)",
                 "from urllib.request import urlopen as fetch\nfetch(url)",
                 "import http.client as wire\nwire.HTTPSConnection(host)", "rerank.rerank(kind, items)",
                 "skillsel.choose(root)", "sources.check_one(spec)")
        for text in cases:
            with self.subTest(source=text):
                self.assertEqual(request_violations(text), [len(text.splitlines())])
        self.assertEqual(request_violations("gateway.send(admitted, kind, digest, lambda: GETTER(url))"), [])
