# MCP 템플릿 핀 검사 — 템플릿·카탈로그·Toolkit 페이지의 핀 일치, npm 레지스트리 질의, 알림만 하는 갱신 결과를 확인한다.
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import shutil
import tomllib
import unittest
from unittest import mock

from oah.atomic import read_jsonl
from oah.toolkit import npm
from oah.toolkit.catalog import Catalog, Mcp, load_catalog
from oah.toolkit.update import DEFAULTS, LOG_FILE, check_mcp, refresh_cache, run_update
from tests.fixtures.toolkit.registry import (FakeRegistry, closed_port_url, latest_answer, without_proxies)
from tests.fixtures.toolkit.upstream import write_catalog
from tests.helpers import make_tmp_dir

REPO = pathlib.Path(__file__).resolve().parent.parent
CATALOG = pathlib.Path("src") / "oah" / "templates" / "toolkit" / "upstreams.json"
PAGE = pathlib.Path("docs") / "toolkit.md"
NOW = dt.datetime(2026, 10, 27, 9, 0, tzinfo=dt.timezone.utc)
NPM_ARG = re.compile(r"^((?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)@(\d+\.\d+\.\d+)$")
PAGE_ROW = re.compile(r"^\| `([a-z0-9._-]+)` \| <([^>]+)> \| `([^`]+)` \| ([^|]+?) \| template \| (.+?) \|$", re.M)


def template_pins(root: pathlib.Path) -> dict[str, tuple[str, str]]:
    """Every npm-pinned template under root/mcp: repository path -> (package, version)."""
    pins = {}
    for path in sorted((root / "mcp" / "claude").glob("*.json")):
        for server in json.loads(path.read_text(encoding="utf-8"))["mcpServers"].values():
            pins.update(_pin(path, root, server.get("args", [])))
    for path in sorted((root / "mcp" / "codex").glob("*.toml")):
        for server in tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"].values():
            pins.update(_pin(path, root, server.get("args", [])))
    return pins


def _pin(path: pathlib.Path, root: pathlib.Path, args: list) -> dict[str, tuple[str, str]]:
    for arg in args:
        match = NPM_ARG.fullmatch(arg) if isinstance(arg, str) else None
        if match:
            return {path.relative_to(root).as_posix(): (match.group(1), match.group(2))}
    return {}


def page_rows(page: pathlib.Path) -> dict[str, tuple[str, str, str, tuple[str, ...]]]:
    text = page.read_text(encoding="utf-8")
    section = text[text.index("## MCP servers"):]
    section = section[:section.index("\n## ", 1)] if "\n## " in section[1:] else section
    return {name: (source, spec, license.strip(), tuple(re.findall(r"`([^`]+)`", templates)))
            for name, source, spec, license, templates in PAGE_ROW.findall(section)}


def pin_problems(root: pathlib.Path) -> list[str]:
    """Where the templates, the catalog mcp list and the Toolkit page disagree about a pin."""
    catalog = load_catalog(root / CATALOG)
    pins = template_pins(root)
    by_template = {template: entry for entry in catalog.mcp for template in entry.templates}
    problems = []
    for template, (package, version) in pins.items():
        entry = by_template.get(template)
        if entry is None:
            problems.append(f"{template} pins {package}@{version} but has no catalog mcp entry")
        elif (entry.package, entry.version) != (package, version):
            problems.append(f"{template} pins {package}@{version}, the catalog says {entry.spec}")
    rows = page_rows(root / PAGE)
    for entry in catalog.mcp:
        for template in entry.templates:
            if template not in pins:
                problems.append(f"catalog entry {entry.name} names {template}, which is missing or not pinned")
        expected = (entry.source, entry.spec, entry.license, entry.templates)
        if rows.get(entry.name) != expected:
            problems.append(f"{PAGE} row for {entry.name} is {rows.get(entry.name)}, expected {expected}")
    for name in sorted(set(rows) - {entry.name for entry in catalog.mcp}):
        problems.append(f"{PAGE} lists template {name}, which the catalog does not")
    return problems


def copy_kit(test: unittest.TestCase) -> pathlib.Path:
    root = make_tmp_dir(test) / "kit"
    shutil.copytree(REPO / "mcp", root / "mcp")
    for relative in (CATALOG, PAGE):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, root / relative)
    return root


def replace_in(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def snapshot(*paths: pathlib.Path) -> dict[str, bytes]:
    found = {}
    for path in paths:
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        found.update({str(file): file.read_bytes() for file in files})
    return found


class PinConsistencyTest(unittest.TestCase):
    def test_pins_agree_across_template_catalog_and_page(self):
        self.assertEqual(pin_problems(REPO), [])
        catalog = load_catalog()
        self.assertEqual(sorted(entry.name for entry in catalog.mcp), ["context7", "mcp-openai"])
        self.assertEqual(sorted(template_pins(REPO)), sorted(t for entry in catalog.mcp for t in entry.templates))

    def test_changing_one_pin_in_one_place_is_caught(self):
        edits = {
            "claude template": ("mcp/claude/context7.json", "@upstash/context7-mcp@4.4.4", "@upstash/context7-mcp@4.4.5"),
            "codex template": ("mcp/codex/mcp-openai.toml", "@mzxrai/mcp-openai@0.1.1", "@mzxrai/mcp-openai@0.1.2"),
            "catalog": (CATALOG.as_posix(), '"version": "4.4.4"', '"version": "4.5.0"'),
            "page": (PAGE.as_posix(), "`@upstash/context7-mcp@4.4.4`", "`@upstash/context7-mcp@4.4.9`"),
        }
        for where, (relative, old, new) in edits.items():
            with self.subTest(where=where):
                root = copy_kit(self)
                self.assertEqual(pin_problems(root), [])
                replace_in(root / relative, old, new)
                self.assertNotEqual(pin_problems(root), [], f"a pin changed only in the {where} went unnoticed")

    def test_npm_pinned_template_without_catalog_entry_is_caught(self):
        root = copy_kit(self)
        (root / "mcp" / "claude" / "extra.json").write_text(json.dumps(
            {"mcpServers": {"extra": {"command": "npx", "args": ["-y", "extra-mcp@1.2.3"]}}}), encoding="utf-8")
        self.assertTrue(any("extra.json" in problem and "no catalog" in problem for problem in pin_problems(root)))

    def test_korean_law_is_removed_everywhere(self):
        self.assertFalse((REPO / "mcp" / "claude" / "korean-law.json").exists())
        self.assertFalse((REPO / "mcp" / "codex" / "korean-law.toml").exists())
        for relative in ("mcp/README.md", PAGE.as_posix(), CATALOG.as_posix()):
            with self.subTest(file=relative):
                text = (REPO / relative).read_text(encoding="utf-8")
                self.assertNotIn("korean-law", text)
                self.assertNotIn("LAW_OC", text)

    def test_toolkit_page_explains_context7_and_the_notices(self):
        text = (REPO / PAGE).read_text(encoding="utf-8")
        section = text[text.index("## MCP servers"):text.index("## Command-line tools")]
        self.assertIn("documentation", section)
        self.assertIn("optional", section)
        self.assertIn("OAH_MCP_UPDATE_AVAILABLE", section)
        self.assertIn("never edits", section)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        without_proxies(self)

    def test_latest_version_reads_a_local_registry(self):
        registry = FakeRegistry(self, {"/@upstash/context7-mcp/latest": (200, latest_answer("@upstash/context7-mcp", "4.5.0")),
                                       "/plain-mcp/latest": (200, latest_answer("plain-mcp", "1.0.0-beta.2"))})
        self.assertEqual(npm.latest_version("@upstash/context7-mcp", registry=registry.url), "4.5.0")
        self.assertEqual(npm.latest_version("plain-mcp", registry=registry.url), "1.0.0-beta.2")
        path, headers = registry.requests[0]
        self.assertEqual(path, "/@upstash/context7-mcp/latest")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_invalid_answers_are_not_echoed(self):
        secretish = "4.5.0; rm -rf ~ \x1b[31m"
        answers = {
            "non-semver": latest_answer("demo", secretish),
            "too long": latest_answer("demo", "1.2.3-" + "a" * 80),
            "wrong name": latest_answer("other-package", "9.9.9"),
            "missing version": json.dumps({"name": "demo"}).encode(),
            "number version": json.dumps({"name": "demo", "version": 5}).encode(),
            "not json": b"<html>4.5.0 rm -rf</html>",
            "list": b'["4.5.0"]',
            "not utf-8": b'{"name": "demo", "version": "\xff"}',
        }
        for label, body in answers.items():
            with self.subTest(answer=label):
                registry = FakeRegistry(self, {"/demo/latest": (200, body)})
                with self.assertRaises(npm.RegistryInvalid) as caught:
                    npm.latest_version("demo", registry=registry.url)
                message = str(caught.exception)
                self.assertNotIn("rm -rf", message)
                self.assertNotIn("\x1b", message)
                self.assertNotIn("other-package", message)
                self.assertNotIn("9.9.9", message)

    def test_oversized_answer_is_refused(self):
        body = latest_answer("demo", "4.5.0", padding="x" * (npm.MAX_ANSWER_BYTES + 10))
        registry = FakeRegistry(self, {"/demo/latest": (200, body)})
        with self.assertRaises(npm.RegistryInvalid) as caught:
            npm.latest_version("demo", registry=registry.url)
        self.assertIn("larger than", str(caught.exception))

    def test_slow_answer_is_offline(self):
        registry = FakeRegistry(self, {"/demo/latest": (200, latest_answer("demo", "4.5.0"))}, delay=1.5)
        with self.assertRaises(npm.RegistryOffline):
            npm.latest_version("demo", registry=registry.url, timeout=0.3)

    def test_closed_port_is_offline(self):
        with self.assertRaises(npm.RegistryOffline):
            npm.latest_version("demo", registry=closed_port_url(), timeout=5)

    def test_http_error_is_registry_error(self):
        registry = FakeRegistry(self, {"/demo/latest": (500, b"oops")})
        with self.assertRaises(npm.RegistryInvalid) as caught:
            npm.latest_version("demo", registry=registry.url)
        self.assertIn("500", str(caught.exception))
        with self.assertRaises(npm.RegistryInvalid):
            npm.latest_version("missing", registry=registry.url)

    def test_redirect_is_not_followed(self):
        target = FakeRegistry(self, {"/demo/latest": (200, latest_answer("demo", "4.5.0"))})
        registry = FakeRegistry(self, {"/demo/latest": (302, b"", {"Location": target.url + "/demo/latest"})})
        with self.assertRaises(npm.RegistryInvalid):
            npm.latest_version("demo", registry=registry.url)
        self.assertEqual(target.requests, [])

    def test_proxy_with_credentials_is_refused_before_connecting(self):
        registry = FakeRegistry(self, {"/demo/latest": (200, latest_answer("demo", "4.5.0"))})
        proxy = "http://someone:x" + "@" + "127.0.0.1:9"
        with mock.patch.dict(os.environ, {"HTTP_PROXY": proxy}):
            with self.assertRaises(npm.RegistryOffline) as caught:
                npm.latest_version("demo", registry=registry.url)
        self.assertIn("HTTP_PROXY", str(caught.exception))
        self.assertNotIn("someone", str(caught.exception))
        self.assertEqual(registry.requests, [])

    def test_only_https_or_loopback_registries(self):
        for url in ("http://registry.example.org", "ftp://127.0.0.1", "https://user:x" + "@" + "example.org",
                    "https://example.org/?q=1"):
            with self.subTest(url=url), self.assertRaises(npm.RegistryInvalid):
                npm.latest_version("demo", registry=url)

    def test_semver_precedence(self):
        self.assertTrue(npm.is_semver("4.5.0"))
        self.assertTrue(npm.is_semver("1.0.0-rc.1+build.5"))
        for text in ("4.5", "v4.5.0", "04.5.0", "4.5.0-", "4.5.0 ", "latest", "", "1.2.3-" + "a" * 80):
            with self.subTest(text=text):
                self.assertFalse(npm.is_semver(text))
        self.assertTrue(npm.newer("4.5.0", "4.4.4"))
        self.assertTrue(npm.newer("4.10.0", "4.9.9"))
        self.assertTrue(npm.newer("1.0.0", "1.0.0-rc.1"))
        self.assertTrue(npm.newer("1.0.0-rc.10", "1.0.0-rc.2"))
        self.assertTrue(npm.newer("1.0.0-rc.1", "1.0.0-alpha"))
        self.assertFalse(npm.newer("4.4.4", "4.4.4"))
        self.assertFalse(npm.newer("4.4.4+build", "4.4.4"))
        self.assertFalse(npm.newer("4.4.3", "4.4.4"))
        self.assertFalse(npm.newer("1.0.0-rc.1", "1.0.0"))


def entry(name: str = "demo", package: str = "demo-mcp", version: str = "1.2.3") -> Mcp:
    return Mcp(name=name, package=package, version=version,
               templates=(f"mcp/claude/{name}.json", f"mcp/codex/{name}.toml"),
               source="https://github.com/example/demo-mcp", license="MIT", purpose="A demo server.")


def answers(mapping: dict[str, str | Exception]):
    def query(package: str) -> str:
        value = mapping[package]
        if isinstance(value, Exception):
            raise value
        return value
    return query


class CheckMcpTest(unittest.TestCase):
    def test_newer_version_is_a_notice_with_both_steps(self):
        [result] = check_mcp([entry()], registry=answers({"demo-mcp": "1.3.0"}))
        self.assertEqual((result.kind, result.status, result.old_version, result.new_version),
                         ("mcp", "notice", "1.2.3", "1.3.0"))
        text = result.describe()
        self.assertTrue(text.startswith("OAH_MCP_UPDATE_AVAILABLE demo 1.2.3 -> 1.3.0"), text)
        self.assertIn("your MCP config", text)
        self.assertIn("demo-mcp@1.3.0", text)
        self.assertIn("mcp/claude/demo.json", text)
        self.assertIn("catalog", text)
        self.assertEqual(result.to_json()["status"], "notice")
        self.assertEqual((result.to_json()["old_version"], result.to_json()["new_version"]), ("1.2.3", "1.3.0"))

    def test_same_or_older_version_is_current(self):
        for latest in ("1.2.3", "1.2.2", "1.2.3+build"):
            with self.subTest(latest=latest):
                [result] = check_mcp([entry()], registry=answers({"demo-mcp": latest}))
                self.assertEqual(result.status, "current")

    def test_failures_are_unused_and_the_check_continues(self):
        results = check_mcp([entry("a", "a-mcp"), entry("b", "b-mcp"), entry("c", "c-mcp"), entry("d", "d-mcp")],
                            registry=answers({"a-mcp": npm.RegistryOffline("could not reach it"),
                                              "b-mcp": npm.RegistryInvalid("the version field is not semver"),
                                              "c-mcp": "not a version; rm -rf",
                                              "d-mcp": "2.0.0"}))
        self.assertEqual([r.status for r in results],
                         ["unused(offline)", "unused(registry error)", "unused(registry error)", "notice"])
        self.assertTrue(all("rm -rf" not in r.describe() for r in results))
        self.assertIn("unused(offline) a", results[0].describe())


class UpdatePathTest(unittest.TestCase):
    """The MCP check rides the landed skill update path: same run record, log and exit rules."""

    def setUp(self):
        self.work = make_tmp_dir(self)
        path = write_catalog(self.work, entries_mcp=[entry()])
        self.catalog: Catalog = load_catalog(path)
        self.catalog_path = path
        self.state = self.work / "state"
        self.home = self.work / "home"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refresh_logs_a_notice_and_writes_nothing_else(self):
        before = snapshot(self.catalog_path)
        results = refresh_cache(catalog=self.catalog, state_dir=self.state, bundle_dir=None, settings=DEFAULTS,
                                now=NOW, why="update", registry=answers({"demo-mcp": "1.3.0"}))
        self.assertEqual([(r.kind, r.status) for r in results], [("mcp", "notice")])
        log = read_jsonl(self.state / "toolkit" / LOG_FILE)
        self.assertEqual([(r["item"], r["kind"], r["status"], r["old_version"], r["new_version"], r["why"])
                          for r in log], [("demo", "mcp", "notice", "1.2.3", "1.3.0", "update")])
        self.assertEqual(snapshot(self.catalog_path), before)

    def test_check_only_reports_but_logs_nothing(self):
        results = refresh_cache(catalog=self.catalog, state_dir=self.state, bundle_dir=None, settings=DEFAULTS,
                                now=NOW, check_only=True, registry=answers({"demo-mcp": "1.3.0"}))
        self.assertEqual([r.status for r in results], ["notice"])
        self.assertFalse((self.state / "toolkit").exists())

    def test_project_level_updates_do_not_query_the_registry(self):
        def refuse(package):
            raise AssertionError("an installed-copy update must not ask the registry")

        results = run_update(catalog=self.catalog, destinations=[], check_only=True, settings=DEFAULTS, now=NOW,
                             gitleaks=None, registry=refuse, home=self.home)
        self.assertEqual(results, [])

    def test_mcp_results_never_become_deferred(self):
        from oah.toolkit import update

        real_identity = update._identity
        calls = []

        def changing_identity(path):
            calls.append(path)
            return ("changed", len(calls))

        with mock.patch.object(update, "_identity", changing_identity):
            results = run_update(catalog=self.catalog, destinations=[], check_only=False, settings=DEFAULTS, now=NOW,
                                 gitleaks=None, mcp=self.catalog.mcp, registry=answers({"demo-mcp": "1.2.3"}),
                                 catalog_path=self.catalog_path, home=self.home)
        self.assertTrue(calls)
        self.assertIs(update._identity, real_identity)
        self.assertEqual([r.status for r in results], ["current"])

    def test_shipped_catalog_check_uses_the_real_query_against_a_local_registry(self):
        without_proxies(self)
        catalog = load_catalog()
        registry = FakeRegistry(self, {f"/{e.package}/latest": (200, latest_answer(e.package, e.version))
                                       for e in catalog.mcp})
        with mock.patch.object(npm, "REGISTRY", registry.url):
            results = check_mcp(catalog.mcp)
        self.assertEqual([r.status for r in results], ["current"] * len(catalog.mcp))
        self.assertEqual(sorted(path for path, _ in registry.requests),
                         sorted(f"/{e.package}/latest" for e in catalog.mcp))


if __name__ == "__main__":
    unittest.main()
