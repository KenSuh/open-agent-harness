# 툴킷 검사 — 스킬 메타데이터 파서, 허용 목록, 번들 출처, 받기·스테이징, 검사, MCP 템플릿을 확인한다.
from __future__ import annotations

import dataclasses
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import tomllib
import unittest
from unittest import mock

from oah.toolkit import SkillInfo, read_skill
from oah.toolkit.skillmeta import SkillError, load_problems, parse_frontmatter
from oah.engines.proc import Resolved, RunResult, run_command
from oah.paths import git_argv
from oah.toolkit.catalog import CatalogError, item_skill_dirs, load_catalog, read_upstream, validate_repository
from oah.toolkit.checks import (blob_id, builtin_privacy_problems, diff_summary, identify_license, privacy_problems,
                                suspicious, text_digest)
from oah.toolkit.fetch import GIT_SAFE_CONFIG, FetchError, StageError, fetch, git_env, stage_item
from tests.fixtures.toolkit.upstream import (APACHE_TEXT, MIT_TEXT, TODAY, bundled, commit, make_executable,
                                             make_upstream, write_catalog)
from tests.helpers import make_tmp_dir, write_posix_tool

REPO = pathlib.Path(__file__).resolve().parent.parent
USERINFO_URL = "https://someone:x" + "@" + "github.com/a/b"  # built from parts so it never reads as an address


def write_skill(folder: pathlib.Path, frontmatter: str, body: str = "# Skill\n") -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return folder


class FrontmatterTest(unittest.TestCase):
    def test_frontmatter_block_scalars_and_crlf(self):
        text = ("---\r\nname: demo\r\ndescription: >\r\n  Folded line one\r\n  and two.\r\n"
                "license: \"MIT\"\r\nmetadata:\r\n  version: '1.2'\r\n  tags:\r\n    - a\r\n    - b\r\n"
                "notes: |-\r\n  keep\r\n  lines\r\n---\r\n# Body\r\n")
        data = parse_frontmatter(text)
        self.assertEqual(data["name"], "demo")
        self.assertEqual(data["description"], "Folded line one and two.\n")
        self.assertEqual(data["license"], "MIT")
        self.assertEqual(data["metadata"], {"version": "1.2", "tags": ["a", "b"]})
        self.assertEqual(data["notes"], "keep\nlines")

    def test_frontmatter_rejects_broken_yaml(self):
        for text in ("# no frontmatter\n", "---\nname: x\n", "---\nname x\n---\n",
                     "---\nname: \"open\n---\n", "---\nname: a\nname: b\n---\n"):
            with self.subTest(text=text), self.assertRaises(SkillError) as caught:
                parse_frontmatter(text)
            self.assertTrue(str(caught.exception).startswith("OAH_SKILL_INVALID"))


class ReadSkillTest(unittest.TestCase):
    def test_read_skill_prefers_marker_then_upstream(self):
        root = make_tmp_dir(self)
        item = root / "item"
        skill = write_skill(item / "skills" / "demo", "name: demo\ndescription: Does a thing.\nversion: 0.1\n")
        info = read_skill(skill)
        self.assertIsInstance(info, SkillInfo)
        self.assertEqual((info.name, info.version, info.source, info.license), ("demo", "0.1", None, None))

        (item / "UPSTREAM.json").write_text(json.dumps({
            "name": "item", "repository": "https://github.com/example/item", "commit": "a" * 40,
            "spdx": "MIT"}), encoding="utf-8")
        info = read_skill(skill / "SKILL.md")
        self.assertEqual((info.version, info.source, info.license),
                         ("a" * 40, "https://github.com/example/item", "MIT"))
        self.assertEqual(info.path, skill)

        (skill / ".oah-skill.json").write_text(json.dumps({
            "item": "item", "skill": "demo", "repository": "https://github.com/example/other",
            "commit": "b" * 40, "spdx": "Apache-2.0"}), encoding="utf-8")
        info = read_skill(skill)
        self.assertEqual((info.version, info.source, info.license),
                         ("b" * 40, "https://github.com/example/other", "Apache-2.0"))

    def test_read_skill_reports_missing_skill(self):
        with self.assertRaises(SkillError):
            read_skill(make_tmp_dir(self) / "absent")


class LoadProblemsTest(unittest.TestCase):
    def test_required_metadata_uses_yaml_12_core_scalar_types(self):
        root = make_tmp_dir(self)
        scalars = ("null", "Null", "NULL", "~", "true", "True", "TRUE", "false", "False", "FALSE",
                   "0", "+12", "-12", "012", "0o17", "0x10", ".nan", ".NaN", ".NAN",
                   ".inf", "-.Inf", "+.INF", "1.0e+3", "1e3", "-2E-3", ".5", "1.", "+.5")
        for value in scalars:
            for suffix in ("", " # scalar comment", "\t# scalar comment"):
                with self.subTest(value=value, suffix=suffix):
                    skill = write_skill(root / "skill", f"name: demo\ndescription: {value}{suffix}\n")
                    self.assertTrue(load_problems(skill))
                    with self.assertRaises(SkillError):
                        read_skill(skill)
            for quoted in (json.dumps(value), "'" + value + "'"):
                with self.subTest(quoted=quoted):
                    skill = write_skill(root / "skill", f"name: demo\ndescription: {quoted}\n")
                    self.assertEqual(load_problems(skill), [])
                    self.assertEqual(read_skill(skill).description, value)
        # YAML 1.1-only bools, timestamps and non-core numbers remain strings in 1.2 core.
        for value in ("yes", "no", "on", "off", "0b10", "1_000", "1:20", "2026-10-23", "0xGG"):
            with self.subTest(string=value):
                skill = write_skill(root / "skill", f"name: demo\ndescription: {value}\n")
                self.assertEqual(load_problems(skill), [])
                self.assertEqual(read_skill(skill).description, value)

    def test_required_metadata_rejects_yaml_types_and_invalid_names(self):
        root = make_tmp_dir(self)
        for key, value in (("name", "[]"), ("description", "null"), ("description", "{a: b}"),
                           ("description", "\n  nested: value"), ("description", "\n  - value"),
                           ("description", "true"), ("description", "123"), ("name", "Bad_Name"),
                           ("description", "# comment"),
                           ("name", "a--b"), ("name", "-demo"), ("name", "x" * 65),
                           ("description", "&anchor text"), ("description", "*anchor")):
            fields = {"name": "demo", "description": "Does a thing."}
            fields[key] = value
            with self.subTest(key=key, value=value):
                skill = write_skill(root / "skill", "".join(f"{k}: {v}\n" for k, v in fields.items()))
                self.assertTrue(load_problems(skill))
                with self.assertRaises(SkillError):
                    read_skill(skill)

    def test_load_problems_missing_name_and_missing_reference(self):
        root = make_tmp_dir(self)
        good = write_skill(root / "good", "name: good\ndescription: ok\n", "See [ref](refs/a.md).\n")
        (good / "refs").mkdir()
        (good / "refs" / "a.md").write_text("a\n", encoding="utf-8")
        self.assertEqual(load_problems(good), [])

        nameless = write_skill(root / "nameless", "description: ok\n")
        self.assertTrue(any("name" in problem for problem in load_problems(nameless)))
        dangling = write_skill(root / "dangling", "name: d\ndescription: ok\n", "See [gone](missing.md#x).\n")
        self.assertTrue(any("missing.md" in problem for problem in load_problems(dangling)))
        self.assertTrue(load_problems(root / "no-such-skill"))

    def test_load_problems_ignores_code_links(self):
        root = make_tmp_dir(self)
        body = ("Inline `[a](nope.md)` and links [web](https://example.com) [top](#top).\n"
                "```md\n[b](also-nope.md)\n```\n")
        skill = write_skill(root / "s", "name: s\ndescription: ok\n", body)
        self.assertEqual(load_problems(skill), [])

    def test_load_problems_refuses_links_escaping_the_item(self):
        root = make_tmp_dir(self)
        (root / "outside.md").write_text("x\n", encoding="utf-8")
        skill = write_skill(root / "item" / "s", "name: s\ndescription: ok\n", "[x](../../outside.md)\n")
        self.assertTrue(load_problems(skill, item_root=root / "item"))




class CatalogTest(unittest.TestCase):
    def test_catalog_rejects_credentials_in_urls(self):
        for url in (USERINFO_URL, "https://github.com/a/b?access=1", "http://github.com/a/b",
                    "git@github.com:a/b", "ssh://github.com/a/b", "https://github.com:8443/a/b",
                    "https://github.com/a/b#frag", "relative/path"):
            with self.subTest(url=url), self.assertRaises(CatalogError):
                validate_repository(url)
        self.assertEqual(validate_repository("https://github.com/a/b"), "https://github.com/a/b")

    def test_catalog_validates_entries(self):
        folder = make_tmp_dir(self)
        upstream = make_upstream(self)
        path = write_catalog(folder, [bundled(upstream)])
        self.assertEqual(load_catalog(path).bundled[0].name, "demo")
        data = json.loads(path.read_text(encoding="utf-8"))
        for change in ({"commit": "short"}, {"subpath": "../x"}, {"skills": []}, {"name": "Bad Name"},
                       {"repository": USERINFO_URL}, {"license_path": "C:/x"}):
            broken = dict(data, bundled=[dict(data["bundled"][0], **change)])
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(CatalogError) as caught:
                load_catalog(path)
            self.assertTrue(str(caught.exception).startswith("OAH_TOOLKIT_CATALOG_INVALID"))


class FetchStageTest(unittest.TestCase):
    def test_git_environment_is_allowlisted_and_refuses_proxy_userinfo(self):
        with mock.patch.dict(os.environ, {"OAH_TEST_SECRET": "private", "LD_PRELOAD": "untrusted",
                                          "CURL_SSL_BACKEND": "untrusted", "LANG": "C",
                                          "HTTPS_PROXY": "http://proxy.invalid:8080"}, clear=True):
            env = git_env()
        self.assertNotIn("OAH_TEST_SECRET", env)
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("CURL_SSL_BACKEND", env)
        self.assertEqual(env["LANG"], "C")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.invalid:8080")
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            for authority in ("someone@proxy.invalid", "someone:generated@proxy.invalid", ":generated@proxy.invalid"):
                value = "http://" + authority
                with self.subTest(key=key, authority=authority), mock.patch.dict(os.environ, {key: value}, clear=True):
                    with self.assertRaises(FetchError) as caught:
                        git_env()
                    self.assertIn("OAH_", str(caught.exception))
                    self.assertIn("proxy without embedded credentials", str(caught.exception))
                    self.assertNotIn(value, str(caught.exception))

    def test_git_process_uses_a_temporary_empty_home(self):
        import oah.toolkit.fetch as fetch_module

        upstream = make_upstream(self)
        work = make_tmp_dir(self)
        homes = []
        original = fetch_module.run_command

        def run_with_home(*args, **kwargs):
            env = kwargs["env"]
            home = pathlib.Path(env["HOME"])
            self.assertTrue(home.is_dir())
            self.assertEqual(list(home.iterdir()), [])
            self.assertEqual({env[key] for key in ("USERPROFILE", "XDG_CONFIG_HOME", "CURL_HOME")}, {str(home)})
            homes.append(home)
            return original(*args, **kwargs)

        with mock.patch("oah.toolkit.fetch.run_command", side_effect=run_with_home):
            tree = fetch(str(upstream), "main", work / "scratch")
            tree.read(tree.entries("LICENSE")[0][1])
        self.assertTrue(homes)
        self.assertTrue(all(not home.exists() for home in homes))

    def test_fetch_refuses_authenticated_proxy_before_sending_credentials(self):
        seen = []

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self):
                seen.append(self.headers.get("Proxy-Authorization"))
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
                self.end_headers()

            def log_message(self, *args):
                pass

        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        except PermissionError:
            self.skipTest("sandbox blocks loopback sockets; proxy refusal is also tested without sockets")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        work = make_tmp_dir(self)
        proxy = "http://someone:" + os.urandom(12).hex() + f"@127.0.0.1:{server.server_address[1]}"
        for index, key in enumerate(("HTTPS_PROXY", "http_proxy", "ALL_PROXY")):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: proxy, "NO_PROXY": "", "no_proxy": ""}):
                with self.assertRaises(FetchError) as caught:
                    fetch("https://example.invalid/repo.git", "main", work / str(index), timeout=5)
                self.assertIn("proxy without embedded credentials", str(caught.exception))
        self.assertEqual(seen, [], "an authenticated proxy must never be contacted")

    def test_fetch_ignores_global_xdg_templates_filters_and_rewriting(self):
        upstream = make_upstream(self, {"skills/demo/lf.md": b"a\nb\n",
                                       "skills/demo/.gitattributes": "*.md filter=probe\n"})
        work = make_tmp_dir(self)
        home = work / "hostile-home"
        home.mkdir()
        marker = work / "filter-ran"
        script = home / "filter.py"
        script.write_text(f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text('ran')\n"
                          "sys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
        command = f'"{pathlib.Path(sys.executable).as_posix()}" "{script.as_posix()}"'
        templates = home / "templates"
        (templates / "hooks").mkdir(parents=True)
        (templates / "hooks" / "post-checkout").write_text("untrusted\n", encoding="utf-8")
        # URL rewriting lives in XDG, the filter and autocrlf in the global file.
        (home / ".gitconfig").write_text(
            f"[core]\n autocrlf = true\n[init]\n templateDir = {templates.as_posix()}\n"
            f"[filter \"probe\"]\n smudge = {command.replace(chr(34), chr(92) + chr(34))}\n required = true\n"
            f'[url "{(work / "missing").as_posix()}"]\n insteadOf = {upstream.as_posix()}\n',
            encoding="utf-8")
        (home / "git").mkdir()
        (home / "git" / "config").write_text(
            f'[url "{(work / "missing").as_posix()}"]\n insteadOf = {upstream.as_posix()}\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home),
                                          "XDG_CONFIG_HOME": str(home)}):
            tree = fetch(str(upstream), "main", work / "scratch", paths=("skills/demo", "LICENSE"))
            record = stage_item(bundled(upstream), tree, work / "item", fetched=TODAY)
        self.assertFalse(marker.exists())
        self.assertFalse((work / "scratch" / "skills").exists(), "fetch must never check out upstream content")
        self.assertFalse((work / "scratch" / ".git" / "hooks" / "post-checkout").exists())
        self.assertEqual((work / "item" / "lf.md").read_bytes(), b"a\nb\n")
        for name, blob in record.files.items():
            self.assertEqual(blob_id((work / "item" / name).read_bytes()), blob)

    def test_git_environment_hides_credential_homes_and_configuration(self):
        env = git_env()
        self.assertEqual(env.get("GIT_CONFIG_GLOBAL"), os.devnull)
        self.assertEqual(env.get("GIT_CONFIG_SYSTEM"), os.devnull)
        for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "CURL_HOME"):
            self.assertNotEqual(env.get(key), os.environ.get(key, "personal-home"))

    def test_scoped_headers_are_absent_from_effective_git_configuration(self):
        home = make_tmp_dir(self)
        (home / ".gitconfig").write_text('[http "https://example.com/"]\n extraHeader = Authorization: Basic '
                                         + "Q" * 20 + "\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            result = run_command((*git_argv(), *GIT_SAFE_CONFIG, "config", "--get-regexp", r"http\..*\.extraheader"),
                                 timeout=30, env=git_env())
        self.assertEqual(result.returncode, 1, "scoped credential headers must not be loaded")
        self.assertEqual(result.stdout, "")

    def test_git_ignores_scoped_extra_headers(self):
        seen = []

        class Unauthorized(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(401)
                self.end_headers()

            def log_message(self, *args):
                pass

        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Unauthorized)
        except PermissionError:
            self.skipTest("sandbox blocks loopback sockets; effective Git configuration is tested separately")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        home = make_tmp_dir(self)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        (home / ".gitconfig").write_text(f'[http "{url}/"]\n extraHeader = Authorization: Basic '
                                         + "Q" * 20 + "\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            run_command((*git_argv(), *GIT_SAFE_CONFIG, "-c", "protocol.http.allow=always", "ls-remote",
                         url + "/repo.git"), timeout=30, env=git_env())
        self.assertTrue(seen)
        self.assertEqual(set(seen), {None})

    def test_stage_rejects_portable_path_collisions_and_reserved_names(self):
        # Build real Git trees without checking out names the host filesystem cannot represent.
        upstream = make_upstream(self)
        work = make_tmp_dir(self)
        tree = fetch(str(upstream), "main", work / "scratch")
        blob = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD:skills/demo/ref.md"],
                              capture_output=True, text=True, check=True).stdout.strip()
        license_blob = tree.entries("LICENSE")[0][1]
        devices = ("CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                   *(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(10)),
                   "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³")
        reserved_paths = [(device + suffix,) for device in devices
                          for suffix in ("", ".txt", " .txt", "...txt", ".", " ", ".txt. ")]
        reserved_paths += [("nested/" + device.lower() + " .txt/ref.md",) for device in devices]
        for index, paths in enumerate([("Foo.md", "foo.md"), ("é.md", "e\u0301.md"),
                                       ("Foo", "foo/a.md"), ("Foo/a.md", "foo/b.md"),
                                       ("CON.txt",), ("a.",), ("a ",), ("a:b",), ("a\\b",),
                                       (".git/config",), ("upstream.JSON",), (".OAH-SKILL.JSON",),
                                       *reserved_paths]):
            with self.subTest(paths=paths):
                # mktree accepts arbitrary byte paths; nested names use a subtree.
                def make_tree(names, root=False):
                    rows = [f"100644 blob {license_blob}\tLICENSE\0"] if root else []
                    groups = {}
                    for name in names:
                        first, sep, rest = name.partition("/")
                        if sep:
                            groups.setdefault(first, []).append(rest)
                        else:
                            rows.append(f"100644 blob {blob}\t{first}\0")
                    for name, children in groups.items():
                        rows.append(f"040000 tree {make_tree(children)}\t{name}\0")
                    return subprocess.run(["git", "-C", str(tree.repo), "mktree", "-z", "--missing"],
                                          input="".join(rows), text=True, capture_output=True, check=True).stdout.strip()
                # Copy objects from the fetched repository: the blob is already available there.
                bad = dataclasses.replace(tree, commit=make_tree(paths, root=True))
                entry = bundled(upstream, subpath="", skills=(".",))
                with mock.patch("oah.toolkit.fetch._write") as write, self.assertRaises(StageError):
                    stage_item(entry, bad, work / f"item-{index}", fetched=TODAY)
                write.assert_not_called()
                self.assertFalse((work / f"item-{index}").exists())

    @unittest.skipUnless(os.name == "nt", "Windows device names must be refused before filesystem access")
    def test_windows_stage_rejects_reserved_devices(self):
        self.test_stage_rejects_portable_path_collisions_and_reserved_names()

    def test_stage_verifies_written_bytes_against_manifest(self):
        import oah.toolkit.fetch as fetch_module

        upstream = make_upstream(self)
        work = make_tmp_dir(self)
        tree = fetch(str(upstream), "main", work / "scratch")
        original = fetch_module._write

        def corrupt(path, data, executable):
            original(path, data + b"changed", executable)

        with mock.patch.object(fetch_module, "_write", corrupt), self.assertRaises(StageError):
            stage_item(bundled(upstream), tree, work / "item", fetched=TODAY)
        self.assertFalse((work / "item" / "UPSTREAM.json").exists())

    def test_fetch_and_stage_records_provenance(self):
        upstream = make_upstream(self)
        make_executable(upstream, "skills/demo/ref.md")
        work = make_tmp_dir(self)
        tree = fetch(str(upstream), "main", work / "scratch", paths=("skills/demo", "LICENSE"))
        record = stage_item(bundled(upstream), tree, work / "item", fetched=TODAY)
        self.assertEqual(record.commit, tree.commit)
        self.assertEqual(record.license_file, "LICENSE")
        self.assertEqual(record.spdx, "MIT")
        self.assertEqual(record.executables, ("ref.md",))
        self.assertEqual(sorted(record.files), ["LICENSE", "SKILL.md", "ref.md"])
        for name, blob in record.files.items():
            self.assertEqual(blob_id((work / "item" / name).read_bytes()), blob)
        self.assertEqual(record.license_sha256, text_digest(MIT_TEXT.encode("utf-8")))
        self.assertEqual(read_upstream(work / "item"), record)
        self.assertFalse((work / "item" / "README.md").exists())

    def test_stage_include_vendors_only_the_named_parts(self):
        upstream = make_upstream(self, {"scripts/check.py": "print('ok')\n", "CONTRIBUTORS.md": "people\n"})
        entry = dataclasses.replace(bundled(upstream, subpath="", skills=("skills/demo",)),
                                    include=("skills", "scripts", "LICENSE"))
        self.assertEqual(entry.fetch_paths(), ("skills", "scripts", "LICENSE", "LICENSE"))
        work = make_tmp_dir(self)
        record = stage_item(entry, fetch(str(upstream), "main", work / "s", paths=entry.fetch_paths()),
                            work / "item", fetched=TODAY)
        self.assertEqual(sorted(record.files), ["LICENSE", "scripts/check.py", "skills/demo/SKILL.md",
                                                "skills/demo/ref.md"])
        self.assertEqual(record.include, ("skills", "scripts", "LICENSE"))
        self.assertFalse((work / "item" / "CONTRIBUTORS.md").exists())
        missing = dataclasses.replace(entry, include=("skills", "nope"))
        with self.assertRaises(StageError):
            stage_item(missing, fetch(str(upstream), "main", work / "s2"), work / "item2", fetched=TODAY)

    def test_fetch_by_commit_id(self):
        upstream = make_upstream(self)
        first = commit(upstream, {"skills/demo/ref.md": "v2\n"})
        commit(upstream, {"skills/demo/ref.md": "v3\n"})
        tree = fetch(str(upstream), first, make_tmp_dir(self) / "scratch")
        self.assertEqual(tree.commit, first)

    def test_stage_keeps_bytes_exact(self):
        upstream = make_upstream(self, {"skills/demo/crlf.md": b"a\r\nb\r\n", "skills/demo/blob.bin": b"\0\x01\xff"})
        work = make_tmp_dir(self)
        tree = fetch(str(upstream), "main", work / "scratch")
        stage_item(bundled(upstream), tree, work / "item", fetched=TODAY)
        self.assertEqual((work / "item" / "crlf.md").read_bytes(), b"a\r\nb\r\n")
        self.assertEqual((work / "item" / "blob.bin").read_bytes(), b"\0\x01\xff")

    @unittest.skipUnless(os.name == "nt", "Git for Windows must also preserve exact blob bytes")
    def test_windows_stage_ignores_autocrlf_and_filters(self):
        self.test_fetch_ignores_global_xdg_templates_filters_and_rewriting()

    def test_stage_refuses_wrong_license(self):
        upstream = make_upstream(self, {"LICENSE": APACHE_TEXT})
        work = make_tmp_dir(self)
        tree = fetch(str(upstream), "main", work / "scratch")
        with self.assertRaises(StageError) as caught:
            stage_item(bundled(upstream), tree, work / "item", fetched=TODAY)
        self.assertIn("Apache-2.0", str(caught.exception))
        self.assertFalse((work / "item").exists())

    @unittest.skipIf(os.name == "nt", "git symlinks need privileges on Windows")
    def test_stage_refuses_symlinks(self):
        upstream = make_upstream(self)
        (upstream / "skills" / "demo" / "link").symlink_to("ref.md")
        commit(upstream, {})
        work = make_tmp_dir(self)
        with self.assertRaises(StageError):
            stage_item(bundled(upstream), fetch(str(upstream), "main", work / "s"), work / "item", fetched=TODAY)

    def test_fetch_never_prompts_for_credentials(self):
        class Unauthorized(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server naming
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="x"')
                self.end_headers()

            def log_message(self, *args):
                pass

        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Unauthorized)
        except PermissionError:
            self.skipTest("sandbox blocks loopback sockets; effective Git configuration is tested separately")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        home = make_tmp_dir(self)
        marker = home / "helper-ran"
        helper = sys_executable_helper(home, marker)
        (home / ".gitconfig").write_text(f"[credential]\n\thelper = {helper}\n", encoding="utf-8")
        saved = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None)
                                 for k, v in saved.items()])
        env = git_env()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        result = run_command((*git_argv(), *GIT_SAFE_CONFIG, "-c", "protocol.http.allow=always", "ls-remote",
                              f"http://127.0.0.1:{server.server_address[1]}/repo.git"), timeout=60, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(marker.exists(), "a credential helper ran")


def sys_executable_helper(folder: pathlib.Path, marker: pathlib.Path) -> str:
    """A credential helper command that records that it ran."""
    import sys

    script = folder / "helper.py"
    script.write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    return "!" + " ".join(f'"{part}"' for part in (pathlib.Path(sys.executable).as_posix(), script.as_posix()))


class ChecksTest(unittest.TestCase):
    def test_runtime_rules_match_repository_privacy_rules(self):
        from tools.privacy_scan import CONTENT_RULES as repository_rules, scan_text
        from oah.toolkit.checks import CONTENT_RULES

        self.assertEqual([(name, pattern.pattern) for name, pattern in CONTENT_RULES],
                         [(name, pattern.pattern) for name, pattern in repository_rules])
        folder = make_tmp_dir(self)
        for index, value in enumerate(("SERVICE_" + "PASSWORD=" + "aB3d" * 4,
                                        "ACCESS_" + "TOK" + "EN: '" + "xY9z" * 4 + "'",
                                        "API_" + "KEY=${KEY}", "API_" + "KEY=<your-key>",
                                        "mail " + "person" + "@" + "private.invalid-domain.com")):
            with self.subTest(value=index):
                (folder / "a.md").write_text(value, encoding="utf-8")
                self.assertEqual(bool(privacy_problems(folder, gitleaks=None)), bool(scan_text(value, ())))
        value = "SERVICE_" + "PASSWORD=" + "aB3d" * 4
        (folder / "a.md").write_text(value, encoding="utf-16")
        self.assertTrue(privacy_problems(folder, gitleaks=None))

    def test_identify_license(self):
        self.assertEqual(identify_license(MIT_TEXT), "MIT")
        self.assertEqual(identify_license(MIT_TEXT.replace("\n", "\r\n")), "MIT")
        self.assertEqual(identify_license(APACHE_TEXT), "Apache-2.0")
        self.assertIsNone(identify_license("All rights reserved."))
        self.assertEqual(text_digest(b"a\r\nb\n"), text_digest(b"a\nb\n"))

    def test_diff_summary_counts_lines(self):
        root = make_tmp_dir(self)
        (root / "old").mkdir()
        (root / "new").mkdir()
        (root / "old" / "a.md").write_text("one\ntwo\n", encoding="utf-8")
        (root / "new" / "a.md").write_text("one\nthree\nfour\n", encoding="utf-8")
        (root / "old" / "gone.md").write_text("x\n", encoding="utf-8")
        (root / "new" / "same.md").write_text("s\n", encoding="utf-8")
        (root / "old" / "same.md").write_text("s\n", encoding="utf-8")
        summary = diff_summary(root / "old", root / "new")
        self.assertEqual((summary.files_changed, summary.added, summary.removed), (2, 2, 2))

    def test_privacy_finds_keys_and_paths(self):
        folder = make_tmp_dir(self) / "item"
        folder.mkdir()
        fake_key = "s" + "k-" + "A1b2C3d4" * 4
        home = "/" + "Us" + "ers/" + "someone/notes"
        (folder / "a.md").write_text(f"key {fake_key}\npath {home}\nplaceholder /Us" "ers/<you>/x\n",
                                     encoding="utf-8")
        problems = privacy_problems(folder, gitleaks=None)
        self.assertEqual(len(problems), 2, problems)
        self.assertEqual(privacy_problems(make_tmp_dir(self), gitleaks=None), [])

    def test_privacy_counts_gitleaks_findings_and_failures(self):
        folder = make_tmp_dir(self)
        tool = Resolved(argv=("gitleaks",), source="gitleaks", kind="native")
        for code, expected in ((0, 0), (1, 1), (2, 1)):
            def runner(argv, timeout, code=code):
                self.assertIn("--no-git", argv)
                return RunResult(code, "", "boom", False)
            with self.subTest(code=code):
                self.assertEqual(len(privacy_problems(folder, gitleaks=tool, runner=runner)), expected)

    def test_suspicious_new_executable_binary_and_size_jump(self):
        root = make_tmp_dir(self)
        old, new = root / "old", root / "new"
        old.mkdir()
        new.mkdir()
        (old / "a.md").write_text("a\n", encoding="utf-8")
        (new / "a.md").write_text("a\n" * 2000, encoding="utf-8")
        (new / "tool.bin").write_bytes(b"\0ELF")
        common = dict(old_spdx="MIT", old_license="d1", old_executables=["a.md"], old_dir=old, new_dir=new,
                      size_jump_ratio=2.0, size_jump_min_bytes=100)
        reasons = suspicious(new_spdx="MIT", new_license="d1", new_executables=["a.md", "run.sh"], **common)
        self.assertEqual(len(reasons), 3, reasons)
        self.assertTrue(any("run.sh" in r for r in reasons))
        self.assertTrue(any("binary" in r for r in reasons))
        self.assertTrue(any("size" in r for r in reasons))
        self.assertTrue(suspicious(new_spdx="Apache-2.0", new_license="d1", new_executables=["a.md"], **common)[0]
                        .startswith("license changed"))
        self.assertIn("license text changed",
                      suspicious(new_spdx="MIT", new_license="d2", new_executables=["a.md"], **common))


# Plan inventory names that differ from the toolkit's item names.
INVENTORY_NAMES = {"skill-creator (Anthropic)": "skill-creator", "superpowers@claude-plugins-official": "superpowers",
                   "codex@openai-codex": "codex-plugin-cc", "ponytail@ponytail": "ponytail",
                   "omo@sisyphuslabs (LazyCodex)": "lazycodex", "openai (mcp-openai)": "mcp-openai"}
PLACEHOLDER = re.compile(r"\$\{[A-Z_][A-Z0-9_]*(?::-)?\}")


def inventory_rows() -> list[tuple[str, str, str]]:
    """(kind, toolkit name, verdict) for every row of the A13 inventory in docs/PLAN.md."""
    text = (REPO / "docs" / "PLAN.md").read_text(encoding="utf-8")
    section = text[text.index("#### A13 toolkit inventory"):]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) > 8 and cells[1] in ("skill", "plugin", "mcp", "cli"):
            rows.append((cells[1], INVENTORY_NAMES.get(cells[2], cells[2]), cells[-2]))
    return rows


def git_blobs(prefix: str) -> dict[str, str]:
    result = run_command((*git_argv(), "-C", str(REPO), "ls-files", "-s", "-z", "--", prefix), timeout=60)
    blobs = {}
    for record in result.stdout.split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            blobs[path] = meta.split()[1]
    return blobs


class ShippedToolkitTest(unittest.TestCase):
    def test_shipped_catalog_is_valid(self):
        catalog = load_catalog()
        self.assertEqual(len(catalog.bundled), 9)
        korean = catalog.find("humanize-korean")
        self.assertEqual((korean.default, korean.select_for), (False, ("korean-prose",)),
                         "humanize-korean is opt-in and chosen only for Korean prose tasks")
        self.assertEqual([entry.name for entry in catalog.bundled if not entry.default], ["humanize-korean"])
        for entry in catalog.bundled:
            with self.subTest(item=entry.name):
                record = read_upstream(REPO / "skills" / entry.name)
                self.assertEqual((record.repository, record.subpath, record.commit, record.spdx, record.license_path),
                                 (entry.repository, entry.subpath, entry.commit, entry.spdx, entry.license_path))
                self.assertTrue(entry.repository.startswith("https://github.com/"))
        self.assertTrue(all(step.run is None for step in catalog.find("unlazy").steps),
                        "unlazy installs only at the gates lane's pin, never through an unpinned installer")
        self.assertGreater(len(catalog.clis), 10)

    def test_vendored_items_have_provenance(self):
        catalog = load_catalog()
        folders = sorted(path.parent for path in (REPO / "skills").glob("*/UPSTREAM.json"))
        self.assertEqual([folder.name for folder in folders], sorted(entry.name for entry in catalog.bundled))
        notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for folder in folders:
            with self.subTest(item=folder.name):
                record = read_upstream(folder)
                self.assertRegex(record.commit, r"^[0-9a-f]{40}$")
                license_file = folder / record.license_file
                self.assertTrue(license_file.is_file(), f"{license_file} is missing")
                self.assertEqual(identify_license(license_file.read_text(encoding="utf-8")), record.spdx)
                self.assertEqual(text_digest(license_file.read_bytes()), record.license_sha256)
                for notice in record.notice_files:
                    self.assertTrue((folder / notice).is_file())
                self.assertRegex(record.fetched, r"^\d{4}-\d{2}-\d{2}$")
                self.assertIn(f"`{record.name}`", notices)
                self.assertIn(record.commit, notices)
                for skill in item_skill_dirs(folder, catalog.find(folder.name)):
                    self.assertEqual(load_problems(skill, item_root=folder), [])

    def test_notices_match_skills_folder_exactly(self):
        text = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        vendored_section = text[text.index("## Vendored skills"):]
        listed = sorted(re.findall(r"^\| `([a-z0-9._-]+)` \|", vendored_section, re.M))
        folders = sorted(path.parent.name for path in (REPO / "skills").glob("*/UPSTREAM.json"))
        self.assertEqual(listed, folders)
        not_vendored = text[:text.index("Ideas only")]
        self.assertNotIn("vendors no third-party code", text)
        for folder in folders:
            repository = read_upstream(REPO / "skills" / folder).repository
            with self.subTest(item=folder):
                self.assertNotIn(f"]({repository})", not_vendored, "a vendored project is listed as not in the repository")

    def test_vendored_files_are_upstream_blobs(self):
        tracked = git_blobs("skills/")
        if not tracked:
            self.skipTest("skills/ is not committed in this checkout")
        for folder in sorted(path.parent for path in (REPO / "skills").glob("*/UPSTREAM.json")):
            record = read_upstream(folder)
            with self.subTest(item=folder.name):
                prefix = f"skills/{folder.name}/"
                committed = {path[len(prefix):]: blob for path, blob in tracked.items() if path.startswith(prefix)}
                self.assertEqual(set(committed), set(record.files) | {"UPSTREAM.json"}, "added or missing files")
                for name, blob in record.files.items():
                    self.assertEqual(committed[name], blob, f"{prefix}{name} differs from upstream")

    def test_every_inventory_row_is_covered(self):
        catalog = load_catalog()
        bundled = {entry.name for entry in catalog.bundled}
        linked = {entry.name: entry for entry in catalog.linked}
        clis = {entry.name for entry in catalog.clis}
        rows = inventory_rows()
        self.assertGreater(len(rows), 50)
        for kind, name, verdict in rows:
            with self.subTest(name=name, verdict=verdict):
                if verdict == "vendor":
                    self.assertTrue(name in bundled or (name in linked and linked[name].reason), name)
                elif verdict == "link" and kind in ("skill", "plugin"):
                    self.assertIn(name, linked)
                elif verdict == "template":
                    self.assertTrue((REPO / "mcp" / "claude" / f"{name}.json").is_file())
                    self.assertTrue((REPO / "mcp" / "codex" / f"{name}.toml").is_file())
                elif kind == "cli":
                    self.assertIn(name, clis)

    def test_toolkit_pages_list_every_item_and_no_excluded_name(self):
        catalog = load_catalog()
        names = [entry.name for entry in (*catalog.bundled, *catalog.linked, *catalog.clis)]
        names += [name for kind, name, verdict in inventory_rows() if verdict in ("template", "link")]
        excluded = [name for _, name, verdict in inventory_rows()
                    if verdict == "exclude" and re.fullmatch(r"[a-z]+(?:[-_][a-z]+)+", name)]
        self.assertGreater(len(excluded), 3)
        for page in ("docs/toolkit.md", "docs/ko/toolkit.md"):
            text = (REPO / page).read_text(encoding="utf-8")
            for name in names:
                with self.subTest(page=page, name=name):
                    self.assertIn(f"`{name}`", text)
            for name in excluded:
                with self.subTest(page=page, excluded=name):
                    self.assertNotIn(name, text)


class McpTemplateTest(unittest.TestCase):
    def test_mcp_templates_use_placeholders_only(self):
        claude = sorted((REPO / "mcp" / "claude").glob("*.json"))
        codex = sorted((REPO / "mcp" / "codex").glob("*.toml"))
        self.assertEqual([p.stem for p in claude], [p.stem for p in codex])
        self.assertEqual(len(claude), 3)
        for path in claude:
            with self.subTest(path=path.name):
                servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
                self.assertEqual(list(servers), [path.stem])
                server = servers[path.stem]
                for key, value in server.get("env", {}).items():
                    self.assertRegex(key, r"^[A-Z_][A-Z0-9_]*$")
                    self.assertRegex(value, rf"^\$\{{{key}(?::-)?\}}$", "env values must be ${NAME} placeholders")
                for arg in server["args"]:
                    self.assertNotRegex(arg, r"[A-Za-z0-9_-]{24,}", "an argument looks like a credential")
                self.assertRegex(server["args"][-1], r"@\d+\.\d+\.\d+$", "pin the npm version")
        for path in codex:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                data = tomllib.loads(text)["mcp_servers"][path.stem]
                self.assertNotIn("env", data, "Codex takes keys through env_vars names, never values")
                self.assertTrue(all(re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) for name in data["env_vars"]))
        for path in (*claude, *codex, REPO / "mcp" / "README.md"):
            text = path.read_text(encoding="utf-8")
            for value in re.findall(r"\$\{[^}]*\}", text):
                self.assertRegex(value, PLACEHOLDER)
        self.assertEqual(builtin_privacy_problems(REPO / "mcp"), [])


if __name__ == "__main__":
    unittest.main()
