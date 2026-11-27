# 프로젝트 위키 색인 어댑터 검사 — 가짜 gbrain 과 루프백 가짜 Ollama 로 선택 규칙, 격리된 실행, 중단 뒤 재개, 텍스트 검색 대체를 본다.
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import http.server
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import threading
import unittest
from unittest import mock

from oah import project_tools_wiki_index as wiki_index
from oah.lockfile import StateLock
from tests.helpers import directory_alias, git_blob, git_commit, git_env, make_git_repo, make_private_dir, make_tmp_dir

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
RETRY_NOTE = "docs/wiki/retry-policy.md"
POSTGRES_NOTE = "docs/wiki/decisions/0001-use-postgres.md"
QUEUE_NOTE = "docs/wiki/decisions/2026/q1/0002-queue.md"
CACHE_NOTE = "docs/wiki/결정/캐시-정책.md"  # a name and a text outside ASCII, as a Korean project has them
SELECTED = [POSTGRES_NOTE, QUEUE_NOTE, RETRY_NOTE, CACHE_NOTE]
LEDGER_LINE = "The ledger is stored in Postgres because the reports need joins."
BUDGET_LINE = "Every request gets a retry budget of three attempts."
CACHE_LINE = "캐시는 서버가 시작할 때 미리 채운다."
WIKI = {
    "docs/wiki/index.md": "# Index\n\n- retry-policy: the retry budget\n",
    "docs/wiki/log.md": "# Log\n\n## 2026-01-02 retry budget\n",
    "docs/wiki/log_archive_001.md": "# Old log\n\nThe retry budget of last year.\n",
    "docs/wiki/index_archive_001.md": "# Old index\n\n- the retry budget\n",
    RETRY_NOTE: "---\ntitle: Retry policy\nsources: [src/retry.py]\n---\n\n# Retry policy\n\n" + BUDGET_LINE
                + "\n\n## Backoff\n\nThe backoff doubles after each failed attempt of the request.\n",
    POSTGRES_NOTE: "---\ntitle: Use Postgres for the ledger\n---\n\n# Use Postgres for the ledger\n\n"
                   "Status: accepted last spring.\n\n## Decision\n\n" + LEDGER_LINE + "\n",
    QUEUE_NOTE: "# Queue choice\n\nA durable queue carries the ledger events between services.\n",
    CACHE_NOTE: "# 캐시 정책\n\n" + CACHE_LINE + "\n",
    "docs/wiki/raw/dump.md": "Raw material about the retry budget.\n",
    "docs/wiki/templates/note.md": "---\ntitle: Template\n---\n\nThe retry budget template text.\n",
    "docs/wiki/archive/old.md": "An archived note about the retry budget.\n",
    "docs/wiki/.obsidian/app.json": "{}\n",
    "docs/wiki/.hidden/private.md": "A hidden note about the retry budget.\n",
    "docs/wiki/diagram.png": b"\x89PNG\r\n\x1a\n",
    "docs/wiki/notes.txt": "The retry budget in a text file.\n",
    "docs/other/outside.md": "Outside the wiki: the retry budget.\n",
}
SKIPPED = {"archive": 3, "attachment": 2, "hidden": 2, "navigation": 2, "raw": 1, "template": 1}
# Committed as bytes, so the working tree and the blobs have the same line ends on every platform.
FILES = {name: content if isinstance(content, bytes) else content.encode("utf-8") for name, content in WIKI.items()}

FAKE_TOOL = r'''
import json
import os
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent


def control():
    try:
        return json.loads((HERE / "control.json").read_text(encoding="utf-8"))
    except OSError:
        return {}


def option(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def page_of(path):
    """(the page, its passages), or None for a note that declares a slug of its own: the real tool holds it back."""
    text = path.read_bytes().decode("utf-8")
    title = slug = path.stem
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            for line in lines[1:end]:
                key, _, value = line.partition(":")
                if key.strip() == "title":
                    title = value.strip()
                if key.strip() == "slug":
                    return None
            text = "\n".join(lines[end + 1:])
    return ({"slug": slug, "title": title, "source_id": "oah-wiki", "type": "concept"},
            [{"slug": slug, "title": title, "text": block.strip(), "embedded": False}
             for block in text.split("\n\n") if block.strip()])


def head_commit(mirror):
    git = pathlib.Path(mirror) / ".git"
    head = (git / "HEAD").read_text(encoding="utf-8").strip()
    return (git / head[5:]).read_text(encoding="utf-8").strip() if head.startswith("ref: ") else head


def main():
    args = sys.argv[1:]
    ctl = control()
    with open(HERE / "calls.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps({"args": args, "cwd": os.getcwd(), "env": dict(os.environ)}) + "\n")
    if args == ["--version"]:
        print("gbrain 0.0.0-fake")
        return 0
    command = args[0]
    if command in ctl.get("fail", []):
        sys.stderr.write("fake failure of " + command + "\n")
        return 1
    home = pathlib.Path(os.environ["GBRAIN_HOME"]) / ".gbrain"
    store = home / "fake.json"
    if command == "init":
        home.mkdir(parents=True)
        (home / "brain.pglite").mkdir()
        config = {"engine": "pglite", "database_path": str(home / "brain.pglite"),
                  "embedding_model": option(args, "--embedding-model"),
                  "embedding_dimensions": int(option(args, "--embedding-dimensions"))}
        config.update(ctl.get("config", {}))
        (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
        store.write_text(json.dumps({"sources": {}, "pages": [], "chunks": []}), encoding="utf-8")
        return 0
    state = json.loads(store.read_text(encoding="utf-8"))
    if command == "sources":
        if args[1] == "add":
            state["sources"][args[2]] = {"path": option(args, "--path"), "readonly": False}
            print(json.dumps({"id": args[2]}))
        elif args[1] == "mirror-readonly":
            state["sources"][args[2]]["readonly"] = True
        elif args[1] == "default":
            state["default"] = args[2]
    elif command == "sync":
        mirror = pathlib.Path(state["sources"][option(args, "--source")]["path"])
        found = [page_of(path) for path in sorted(mirror.glob("*.md"))]
        state["pages"] = [item[0] for item in found if item is not None]
        state["chunks"] = [chunk for item in found if item is not None for chunk in item[1]]
        commit = ctl.get("sync_commit") or head_commit(mirror)
        print("a banner before the answer")
        print(json.dumps({"sync_status": "ok", "target_commit": commit, "added": len(state["chunks"]),
                          "watermark": {"indexed_through": {"commit": commit}}}))
    elif command == "embed":
        stale = [chunk for chunk in state["chunks"] if not chunk["embedded"]]
        stop = ctl.get("embed_stop_after")
        if stop is not None and stale and len(state["chunks"]) - len(stale) >= stop:
            sys.stderr.write("fake embedding stopped\n")
            return 1
        batch = stale[:ctl.get("embed_batch", 64)]
        if batch:
            body = json.dumps({"model": "bge-m3", "input": [chunk["text"] for chunk in batch]}).encode("utf-8")
            request = urllib.request.Request(os.environ["OLLAMA_BASE_URL"] + "/embeddings", data=body,
                                             headers={"Content-Type": "application/json"})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20) as answer:
                    answer.read()
            except OSError as exc:
                sys.stderr.write("fake embedding failed: %s\n" % exc)
                return 1
            for chunk in batch:
                chunk["embedded"] = True
        print(json.dumps({"embedded": len(batch), "failures": ctl.get("embed_failures", 0)}))
    elif command == "stats":
        print(json.dumps({"page_count": len(state["pages"]), "chunk_count": len(state["chunks"]),
                          "embedded_count": sum(1 for chunk in state["chunks"] if chunk["embedded"])}))
        return 0
    elif command == "list":
        print(json.dumps(state["pages"][:int(option(args, "--limit", "50"))]))
        return 0
    elif command == "query":
        terms = args[1].lower().split()
        rows = []
        for index, chunk in enumerate(state["chunks"]):
            hits = sum(1 for term in terms if term in chunk["text"].lower())
            if hits:
                rows.append({"slug": chunk["slug"], "title": chunk["title"], "chunk_text": chunk["text"],
                             "chunk_index": index, "score": hits / len(terms),
                             "evidence": "keyword_exact" if hits == len(terms) else "weak_semantic",
                             "source_id": option(args, "--source-id")})
        rows.sort(key=lambda row: -row["score"])
        rows = ctl.get("extra_rows", []) + rows[:int(option(args, "--limit", "10"))]
        if ctl.get("degraded"):
            sys.stderr.write("[gbrain degraded: " + ctl["degraded"] + "]\n")
        print(json.dumps({"results": rows} if ctl.get("wrapped") else rows))
        return 0
    else:
        sys.stderr.write("fake gbrain: unknown command\n")
        return 2
    store.write_text(json.dumps(state), encoding="utf-8")
    return 0


raise SystemExit(main())
'''


def force_remove(path: pathlib.Path) -> None:
    """Remove a folder even where Git left read-only object files (Windows refuses those as they are)."""
    def retry(function, target, _info):
        os.chmod(target, stat.S_IRWXU)
        function(target)

    if not os.path.lexists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def inside(path: object, folder: object) -> bool:
    inner, outer = (os.path.normcase(os.path.realpath(str(item))) for item in (path, folder))
    try:
        return os.path.commonpath([inner, outer]) == outer
    except ValueError:
        return False


def tree(folder: pathlib.Path) -> dict:
    """Every path below folder with its size, to show that a call created and changed nothing."""
    return {str(path.relative_to(folder)): (path.stat().st_size if path.is_file() else -1)
            for path in sorted(folder.rglob("*"))} if folder.exists() else {}


class FakeOllama:
    """An Ollama-shaped server on a loopback port. It records every request, so a test can show what was never asked."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.requests: list[tuple[str, str]] = []
        self.embedded = 0  # passages embedded for the tool through the OpenAI-compatible endpoint
        self.dimensions = 8
        self.models = [{"name": "bge-m3:latest", "model": "bge-m3:latest", "size": 1157672605, "digest": "79" * 32,
                        "details": {"format": "gguf", "family": "bert"}, "capabilities": ["embedding"]}]
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status: int, payload: object) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path))
                if self.path == "/api/version":
                    self.answer(200, {"version": "0.0.0-fake"})
                elif self.path == "/api/tags":
                    self.answer(200, {"models": outer.models})
                else:
                    self.answer(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append(("POST", self.path))
                texts = body.get("input")
                texts = texts if isinstance(texts, list) else [texts]
                if self.path == "/api/embed":
                    self.answer(200, {"model": body.get("model"), "embeddings": [[0.5] * outer.dimensions for _ in texts]})
                elif self.path == "/v1/embeddings":
                    outer.embedded += len(texts)
                    self.answer(200, {"data": [{"embedding": [0.5] * outer.dimensions} for _ in texts]})
                else:
                    self.answer(404, {"error": "not found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        test.addCleanup(self.stop)

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(10)
            self.server = None


class IndexCase(unittest.TestCase):
    """A project with a committed wiki, a fake gbrain outside it, a fake Ollama and an empty private state folder."""

    maxDiff = None

    def setUp(self) -> None:
        self.repo = make_git_repo(self)
        self.addCleanup(force_remove, self.repo)
        self.head = git_commit(self.repo, FILES, "wiki")
        self.tools_dir = make_tmp_dir(self) / "tools"
        self.tools_dir.mkdir()
        self.script = self.tools_dir / "fake_gbrain.py"
        self.script.write_text(FAKE_TOOL, encoding="utf-8")
        self.ollama = FakeOllama(self)
        private = make_private_dir(self)
        self.addCleanup(force_remove, private)
        self.state_root = private / "state"
        self.tools = {"gbrain": [sys.executable, str(self.script)], "ollama_url": self.ollama.url,
                      "embedding_model": "bge-m3"}

    def control(self, **values: object) -> None:
        (self.tools_dir / "control.json").write_text(json.dumps(values), encoding="utf-8")

    def calls(self) -> list[dict]:
        log = self.tools_dir / "calls.jsonl"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def commands(self, since: int = 0) -> list[str]:
        return [" ".join(call["args"][:2]) if call["args"][0] == "sources" else call["args"][0]
                for call in self.calls()[since:]]

    def status(self, **options: object) -> dict:
        return wiki_index.status(self.repo, state_root=self.state_root, **{"tools": self.tools, **options})

    def build(self, **options: object) -> dict:
        return wiki_index.build(self.repo, options.pop("tools", self.tools), state_root=self.state_root, **options)

    def search(self, query: str, **options: object) -> dict:
        return wiki_index.search(self.repo, query, state_root=self.state_root, **{"tools": self.tools, **options})

    def index_dir(self) -> pathlib.Path:
        return self.state_root / "wiki-index" / wiki_index.worktree_id(os.path.realpath(self.repo))

    def generation(self) -> pathlib.Path:
        current = json.loads((self.index_dir() / "current.json").read_text(encoding="utf-8"))
        return self.index_dir() / "generations" / current["generation"]

    def manifest(self) -> dict:
        return json.loads((self.generation() / "manifest.json").read_text(encoding="utf-8"))

    def line_of(self, path: str, text: str, files: dict = WIKI) -> int:
        return files[path].split("\n").index(text) + 1

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True,
                              env=git_env()).stdout.strip()


class StatusTest(IndexCase):
    def test_status_only_reads_before_and_after_a_build(self):
        before = dict(os.environ)
        result = self.status()
        self.assertEqual((result["ok"], result["state"], result["reasons"], result["active"], result["pending"]),
                         (True, "not_built", [], None, None))
        self.assertEqual(result["source"], {
            "commit": self.head, "wiki_dir": "docs/wiki", "files": 4, "skipped": SKIPPED,
            "bytes": sum(len(FILES[path]) for path in SELECTED),
            "manifest_digest": result["source"]["manifest_digest"]})
        self.assertEqual(result["state_dir"], str(self.index_dir()))
        self.assertFalse(self.state_root.exists(), "status created the state folder")
        self.assertEqual((self.calls(), self.ollama.requests), ([], []), "status ran a tool or sent a request")
        self.assertEqual(self.git("status", "--porcelain"), "")

        self.assertTrue(self.build()["ok"])
        calls, requests, files = len(self.calls()), list(self.ollama.requests), tree(self.state_root)
        self.assertEqual(self.status()["state"], "ready")
        self.assertEqual((len(self.calls()), self.ollama.requests, tree(self.state_root)), (calls, requests, files))
        self.assertEqual(dict(os.environ), before)

    def test_links_and_submodules_in_the_snapshot_are_never_selected(self):
        target = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"], input=b"retry-policy.md",
                                check=True, capture_output=True, env=git_env()).stdout.decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{target},docs/wiki/linked.md")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.head},docs/wiki/vendored")
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "a link and a submodule")
        result = self.status()
        self.assertEqual(result["source"]["files"], 4)
        self.assertEqual(result["source"]["skipped"], dict(SKIPPED, submodule=1, symlink=1))

    def test_selection_is_explicit_and_stays_inside_the_repository(self):
        for bad in ("../outside", "/etc", "docs/../..", ".git/hooks", "C:\\wiki", "-rf", "", ".wiki",
                    "docs/.private", "docs/wiki/.hidden", "docs/wiki/raw", "docs/wiki/Archive", "templates/wiki"):
            with self.subTest(wiki_dir=bad):
                self.assertEqual(self.status(selection={"wiki_dir": bad})["code"], wiki_index.CODE_SELECTION)
        self.assertEqual(self.status(selection={"wiki_dir": "docs/wiki", "folders": []})["code"], wiki_index.CODE_USAGE)
        self.assertEqual(self.status(selection={"wiki_dir": "docs/other"})["source"]["files"], 1)
        missing = self.status(selection={"wiki_dir": "docs/absent"})
        self.assertEqual((missing["state"], missing["source"]["error"]["code"]),
                         ("not_built", wiki_index.CODE_SELECTION))
        git_commit(self.repo, {".oah/config.toml": b'[wiki]\ndir = "docs/other"\n'}, "wiki setting")
        self.assertEqual(self.status()["source"]["wiki_dir"], "docs/other", "the project's [wiki] dir is the default")

    def test_a_folder_without_a_note_and_a_repository_without_a_commit(self):
        git_commit(self.repo, {"docs/empty/index.md": b"# Index\n", "docs/empty/picture.png": b"\x89PNG"}, "no note")
        empty = self.status(selection={"wiki_dir": "docs/empty"})
        self.assertEqual((empty["state"], empty["source"]["error"]["code"]), ("not_built", wiki_index.CODE_NO_NOTES))
        self.assertEqual(self.build(selection={"wiki_dir": "docs/empty"})["code"], wiki_index.CODE_NO_NOTES)
        unborn = make_git_repo(self)
        self.addCleanup(force_remove, unborn)
        (unborn / "docs" / "wiki").mkdir(parents=True)
        (unborn / "docs" / "wiki" / "draft.md").write_bytes(b"# Draft\n\nNothing is committed yet.\n")
        status = wiki_index.status(unborn, state_root=self.state_root)
        self.assertEqual((status["ok"], status["state"], status["head"], status["source"]["error"]["code"]),
                         (True, "not_built", None, wiki_index.CODE_NO_SNAPSHOT))
        self.assertEqual(wiki_index.build(unborn, self.tools, state_root=self.state_root)["code"],
                         wiki_index.CODE_NO_SNAPSHOT)
        answer = wiki_index.search(unborn, "committed", tools=self.tools, state_root=self.state_root)
        self.assertEqual((answer["mode"], answer["fallback"]["code"], answer["results"][0]["path"]),
                         ("text", wiki_index.CODE_NOT_BUILT, "docs/wiki/draft.md"))
        self.assertEqual((self.calls(), self.state_root.exists()), ([], False))

    def test_a_folder_outside_git_is_refused(self):
        folder = make_tmp_dir(self)
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(folder.parent)}):
            result = wiki_index.status(folder, state_root=self.state_root)
        self.assertEqual((result["ok"], result["code"]), (False, wiki_index.CODE_NOT_A_REPO))

    def test_snapshot_bounds(self):
        over = self.status(limits=wiki_index.Limits(max_files=2))
        self.assertEqual(over["source"]["error"]["code"], wiki_index.CODE_TOO_LARGE)
        self.assertEqual(self.build(limits=wiki_index.Limits(max_files=2))["code"], wiki_index.CODE_TOO_LARGE)
        self.assertEqual(self.build(limits=wiki_index.Limits(max_total_bytes=10))["code"], wiki_index.CODE_TOO_LARGE)
        small = self.status(limits=wiki_index.Limits(max_file_bytes=len(FILES[QUEUE_NOTE])))
        self.assertEqual((small["source"]["files"], small["source"]["skipped"]["too_large"]), (2, 2))
        self.assertEqual((self.calls(), self.state_root.exists()), ([], False))


class BuildTest(IndexCase):
    def test_build_mirrors_the_verified_blobs_and_records_the_index(self):
        result = self.build()
        self.assertEqual((result["ok"], result["changed"], result["resumed"], result["state"], result["pending"]),
                         (True, True, False, "ready", None), result)
        manifest = self.manifest()
        self.assertEqual([entry["path"] for entry in manifest["entries"]], SELECTED)
        mirror = self.generation() / "mirror"
        for entry in manifest["entries"]:
            self.assertEqual((mirror / f"{entry['id']}.md").read_bytes(), git_blob(self.repo, entry["path"]))
            self.assertNotIn(pathlib.PurePosixPath(entry["path"]).stem, entry["id"])
        self.assertEqual(sorted(path.name for path in mirror.iterdir() if path.name != ".git"),
                         sorted(f"{entry['id']}.md" for entry in manifest["entries"]))
        self.assertEqual(manifest["source"], {"worktree": os.path.realpath(self.repo), "commit": self.head,
                                              "wiki_dir": "docs/wiki", "object_format": "sha1"})
        active = result["active"]
        self.assertEqual((active["commit"], active["files"], active["pages"], active["chunks"], active["unindexed"]),
                         (self.head, 4, 4, 12, {"count": 0, "paths": []}))
        self.assertEqual(active["model"], {"name": "bge-m3", "resolved": "bge-m3:latest", "digest": "79" * 32,
                                           "dimensions": 8})
        self.assertEqual((active["ollama_url"], active["tool"]["version"]), (self.ollama.url, "gbrain 0.0.0-fake"))
        self.assertEqual(self.commands(), ["init", "sources add", "sources mirror-readonly", "sources default",
                                           "sync", "embed", "embed", "stats", "list", "--version"])
        init, add = self.calls()[0]["args"], self.calls()[1]["args"]
        self.assertEqual(init, ["init", "--pglite", "--non-interactive", "--no-git", "--skip-embed-check",
                                "--embedding-model", "ollama:bge-m3", "--embedding-dimensions", "8",
                                "--search-mode", "conservative"])
        self.assertTrue(inside(add[add.index("--path") + 1], self.generation()))
        self.assertEqual(self.ollama.embedded, 12)
        self.assertEqual(self.git("status", "--porcelain"), "", "the build wrote into the project")
        mirrored = subprocess.run(["git", "-C", str(mirror), "rev-parse", "HEAD"], check=True, capture_output=True,
                                  text=True, env=git_env()).stdout.strip()
        self.assertEqual(mirrored, manifest["mirror_commit"])
        self.assertEqual([path.name for path in (mirror / ".git" / "hooks").glob("*")], [], "the mirror has hooks")
        self.assertIn("hooksPath", (self.generation() / "gitconfig").read_text(encoding="ascii"))

    def test_the_tool_runs_apart_from_the_project_and_from_the_callers_environment(self):
        leaked = {"OPENAI_API_KEY": "leak", "ANTHROPIC_API_KEY": "leak", "OLLAMA_HOST": "0.0.0.0:1",
                  "OLLAMA_API_KEY": "leak", "GBRAIN_DATABASE_URL": "leak", "GBRAIN_SELF_UPGRADE_MODE": "auto",
                  "HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "ALL_PROXY": "leak",
                  "BUN_INSTALL": "leak", "NODE_OPTIONS": "leak", "OAH_TEST_MARKER": "leak"}
        with mock.patch.dict(os.environ, leaked):
            before = dict(os.environ)
            self.assertTrue(self.build()["ok"])
            self.assertEqual(self.search("retry budget")["mode"], "semantic")
            self.assertEqual(dict(os.environ), before, "the caller's environment was changed")
        calls = self.calls()
        self.assertIn("query", [call["args"][0] for call in calls])
        # Names Windows itself needs, one macOS adds to every process, and constants the adapter sets on its own.
        passed = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
                  "OS", "__CF_USER_TEXT_ENCODING", "LC_ALL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM",
                  "GIT_TERMINAL_PROMPT",
                  *(name.upper() for name in wiki_index._TOOL_SWITCHES)}
        for call in calls:
            env = {key.upper(): value for key, value in call["env"].items()}
            with self.subTest(args=call["args"][:2]):
                self.assertTrue(inside(call["cwd"], self.state_root) and not inside(call["cwd"], self.repo))
                self.assertEqual({name for name in leaked if name.upper() in env
                                  and env[name.upper()] == leaked[name]}, set())
                self.assertEqual({name for name, value in before.items()
                                  if env.get(name.upper()) == value and value and name.upper() not in passed}, set(),
                                 "a variable of the caller reached the tool")
                self.assertEqual((env["GBRAIN_SELF_UPGRADE_MODE"], env["GBRAIN_SKIP_STARTUP_HOOKS"],
                                  env["GBRAIN_NO_AUTOPILOT_INSTALL"], env["GBRAIN_TELEMETRY"], env["OLLAMA_BASE_URL"]),
                                 ("off", "1", "1", "off", self.ollama.url + "/v1"))
                for name in ("HOME", "GBRAIN_HOME", "TMPDIR", "TEMP", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL"):
                    self.assertTrue(inside(env[name], self.state_root), name)
                self.assertNotIn(os.path.normcase(os.path.realpath(self.repo)),
                                 [os.path.normcase(os.path.realpath(part)) for part in env["PATH"].split(os.pathsep)])

    def test_a_new_commit_that_leaves_the_notes_alone_needs_no_rebuild(self):
        self.assertTrue(self.build()["ok"])
        calls = len(self.calls())
        newer = git_commit(self.repo, {"src/app.py": b"print('x')\n", "docs/wiki/log.md": b"# Log\n\nmore\n"}, "code")
        status = self.status()
        self.assertEqual((status["state"], status["head"], status["active"]["commit"]), ("ready", newer, self.head))
        again = self.build()
        self.assertEqual((again["ok"], again["changed"], again["state"]), (True, False, "ready"))
        self.assertEqual(len(self.calls()), calls, "an index that is current was rebuilt")

    def test_changed_notes_make_the_index_stale_and_a_rebuild_replaces_it(self):
        self.assertTrue(self.build()["ok"])
        first = self.generation()
        newer = git_commit(self.repo, {"docs/wiki/new-note.md": b"# New note\n\nThe cache is warmed at start.\n"}, "note")
        status = self.status()
        self.assertEqual((status["state"], status["reasons"], status["active"]["commit"]),
                         ("stale", ["content_changed"], self.head))
        result = self.build()
        self.assertEqual((result["ok"], result["changed"], result["state"], result["active"]["commit"]),
                         (True, True, "ready", newer))
        self.assertEqual(result["active"]["files"], 5)
        self.assertNotEqual(self.generation(), first)
        self.assertEqual([path.name for path in (self.index_dir() / "generations").iterdir()],
                         [self.generation().name], "the replaced generation was kept")
        self.assertEqual(self.search("cache warmed")["results"][0]["path"], "docs/wiki/new-note.md")

    def test_a_stopped_build_keeps_the_last_good_index_and_the_next_build_continues_it(self):
        self.assertTrue(self.build()["ok"])
        first = self.generation()
        git_commit(self.repo, {"docs/wiki/new-note.md": b"# New note\n\nThe cache is warmed at start.\n\nIt is small.\n"},
                   "note")
        calls, embedded = len(self.calls()), self.ollama.embedded
        self.control(embed_batch=1, embed_stop_after=2)
        stopped = self.build()
        self.assertEqual((stopped["ok"], stopped["code"], stopped["state"], stopped["reasons"]),
                         (False, wiki_index.CODE_INCOMPLETE, "stale", ["content_changed"]), stopped)
        self.assertEqual((stopped["active"]["generation"], stopped["active"]["commit"]), (first.name, self.head))
        pending = stopped["pending"]
        self.assertEqual((pending["phase"], pending["done"], pending["attempts"], pending["error"]["code"]),
                         ("embed", ["mirror", "brain", "sync"], 1, wiki_index.CODE_INCOMPLETE))
        self.assertNotEqual(pending["generation"], first.name)
        self.assertEqual(self.ollama.embedded - embedded, 2)

        answer = self.search("retry budget")  # the last good index still answers, and says it is behind
        self.assertEqual((answer["mode"], answer["index"]["state"], answer["index"]["pending"],
                          answer["results"][0]["path"]), ("semantic", "stale", True, RETRY_NOTE))
        self.assertEqual(self.generation(), first)

        self.control()
        again = self.build()
        self.assertEqual((again["ok"], again["changed"], again["resumed"], again["state"], again["pending"]),
                         (True, True, True, "ready", None), again)
        self.assertEqual(again["active"]["generation"], pending["generation"])
        self.assertFalse(first.exists(), "the replaced generation was kept")
        self.assertEqual(self.commands(calls).count("init"), 1, "continuing a build started it over")
        self.assertEqual(self.ollama.embedded - embedded, again["active"]["chunks"], "a passage was embedded twice")

    def test_a_first_build_that_stops_is_incomplete_which_is_not_stale(self):
        self.control(fail=["embed"])
        stopped = self.build()
        self.assertEqual((stopped["ok"], stopped["code"], stopped["state"], stopped["active"]),
                         (False, wiki_index.CODE_INCOMPLETE, "incomplete", None), stopped)
        self.assertEqual(stopped["pending"]["phase"], "embed")
        self.assertFalse((self.index_dir() / "current.json").exists())
        self.assertEqual(self.status()["state"], "incomplete")
        answer = self.search("retry budget")
        self.assertEqual((answer["mode"], answer["fallback"]["code"], answer["results"][0]["path"]),
                         ("text", wiki_index.CODE_INCOMPLETE, RETRY_NOTE))
        self.control()
        again = self.build()
        self.assertEqual((again["ok"], again["resumed"], again["state"]), (True, True, "ready"), again)

    def test_a_stopped_build_of_other_input_is_dropped_not_continued(self):
        self.control(fail=["sync"])
        stopped = self.build()
        self.assertEqual((stopped["code"], stopped["state"]), (wiki_index.CODE_INCOMPLETE, "incomplete"))
        abandoned = stopped["pending"]["generation"]
        git_commit(self.repo, {"docs/wiki/new-note.md": b"# New note\n\nThe cache is warmed at start.\n"}, "note")
        self.control()
        result = self.build()
        self.assertEqual((result["ok"], result["resumed"], result["state"]), (True, False, "ready"), result)
        self.assertEqual([path.name for path in (self.index_dir() / "generations").iterdir()],
                         [result["active"]["generation"]])
        self.assertNotEqual(result["active"]["generation"], abandoned)

    def test_embedding_that_reports_failures_or_stays_partial_is_not_a_complete_index(self):
        self.control(embed_failures=1)
        self.assertEqual(self.build()["code"], wiki_index.CODE_INCOMPLETE)
        self.control(sync_commit="0" * 40)
        self.assertEqual(self.build(selection={"wiki_dir": "docs/other"})["code"], wiki_index.CODE_INCOMPLETE)
        self.assertFalse((self.index_dir() / "current.json").exists())

    def test_a_blob_that_does_not_verify_is_refused_before_anything_is_indexed(self):
        real = wiki_index._git

        def tampered(root, *args, **options):
            result = real(root, *args, **options)
            if args[0] == "cat-file":
                return dataclasses.replace(result, stdout=result.stdout.replace("budget", "budgex"))
            return result

        with mock.patch.object(wiki_index, "_git", tampered):
            result = self.build()
        self.assertEqual((result["ok"], result["code"]), (False, wiki_index.CODE_SOURCE_UNVERIFIED), result)
        self.assertEqual(self.calls(), [], "a tool ran on unverified content")
        self.assertFalse((self.index_dir() / "current.json").exists())

    def test_an_index_the_tool_did_not_make_local_is_refused(self):
        for config in ({"engine": "postgres"}, {"database_url": "postgres://db.example.com/brain"},
                       {"database_path": str(self.tools_dir / "elsewhere.pglite")},
                       {"embedding_model": "openai:text-embedding-3-large"}, {"embedding_dimensions": 1536},
                       {"openai_api_key": "x"}):
            with self.subTest(config=config):
                self.control(config=config)
                result = self.build()
                self.assertEqual((result["ok"], result["code"], result["state"]),
                                 (False, wiki_index.CODE_NOT_LOCAL, "incomplete"), result)
                self.assertFalse((self.index_dir() / "current.json").exists())
                self.assertNotIn("sync", self.commands())

    def test_the_snapshot_is_head_or_one_of_its_ancestors(self):
        second = git_commit(self.repo, {"docs/wiki/new-note.md": b"# New note\n\nThe cache is warmed at start.\n"}, "note")
        self.git("checkout", "-q", "-b", "side", self.head)
        side = git_commit(self.repo, {"docs/wiki/side.md": b"# Side\n\nA note of another branch.\n"}, "side")
        self.git("checkout", "-q", "main")
        for commit in (side, self.head[:12], "HEAD", "main", "f" * 40):
            with self.subTest(commit=commit):
                self.assertEqual(self.build(commit=commit)["code"], wiki_index.CODE_NO_SNAPSHOT)
        self.assertEqual(self.calls(), [])
        older = self.build(commit=self.head)
        self.assertEqual((older["ok"], older["active"]["commit"], older["active"]["files"], older["state"]),
                         (True, self.head, 4, "stale"), older)
        newest = self.build(commit=second)
        self.assertEqual((newest["active"]["commit"], newest["active"]["files"], newest["state"]),
                         (second, 5, "ready"))

    def test_rebuild_makes_a_current_index_anew_beside_the_old_one(self):
        self.assertTrue(self.build()["ok"])
        first = self.generation()
        self.assertEqual(self.build(rebuild="yes")["code"], wiki_index.CODE_USAGE)
        self.control(fail=["sync"])
        stopped = self.build(rebuild=True)
        self.assertEqual((stopped["code"], stopped["state"], stopped["active"]["generation"]),
                         (wiki_index.CODE_INCOMPLETE, "ready", first.name), stopped)
        self.assertEqual(self.search("retry budget")["mode"], "semantic", "the old index was given up too early")
        self.control()
        again = self.build(rebuild=True)
        self.assertEqual((again["ok"], again["changed"], again["resumed"], again["state"]),
                         (True, True, True, "ready"), again)
        self.assertNotEqual(self.generation(), first)
        self.assertFalse(first.exists())
        self.assertEqual(self.search("retry budget")["results"][0]["path"], RETRY_NOTE)

    def test_a_damaged_record_is_no_index_and_the_next_build_replaces_it(self):
        self.assertTrue(self.build()["ok"])
        current = self.index_dir() / "current.json"
        good = current.read_bytes()
        for damage in ("the record", "the folder"):
            with self.subTest(damage):
                if damage == "the record":
                    current.write_bytes(b"{not json")
                else:
                    current.write_bytes(good)
                    (self.generation() / "manifest.json").unlink()
                status = self.status()
                self.assertEqual((status["ok"], status["state"], status["reasons"], status["active"]),
                                 (True, "not_built", ["state_unreadable"], None))
                answer = self.search("retry budget")
                self.assertEqual((answer["mode"], answer["fallback"]["code"]), ("text", wiki_index.CODE_NOT_BUILT))
                rebuilt = self.build()
                self.assertEqual((rebuilt["ok"], rebuilt["changed"], rebuilt["state"], rebuilt["reasons"]),
                                 (True, True, "ready", []), rebuilt)
                good = current.read_bytes()
                self.assertEqual([path.name for path in (self.index_dir() / "generations").iterdir()],
                                 [self.generation().name])

    def test_one_build_at_a_time(self):
        self.assertTrue(self.build()["ok"])
        git_commit(self.repo, {"docs/wiki/new-note.md": b"# New note\n\nThe cache is warmed at start.\n"}, "note")
        calls = len(self.calls())
        with StateLock(self.index_dir() / "build.lock"):
            result = self.build()
        self.assertEqual((result["ok"], result["code"], result["state"]), (False, wiki_index.CODE_BUSY, "stale"))
        self.assertEqual(len(self.calls()), calls)


class ToolRulesTest(IndexCase):
    def refused(self, tools: dict, code: str) -> None:
        for name, result in (("probe", wiki_index.probe(self.repo, tools, state_root=self.state_root)),
                             ("build", self.build(tools=tools))):
            self.assertEqual((name, result["ok"], result.get("code")), (name, False, code), result)
        answer = self.search("retry budget", tools=tools)
        self.assertEqual((answer["mode"], answer["fallback"]["code"], answer["results"][0]["path"]),
                         ("text", code, RETRY_NOTE))

    def test_the_command_is_an_executable_outside_the_project_and_never_a_shell(self):
        planted = self.repo / "bin" / ("gbrain.exe" if os.name == "nt" else "gbrain")
        planted.parent.mkdir()
        shutil.copyfile(sys.executable, planted)
        planted.chmod(0o755)
        script = self.repo / "tool.py"
        script.write_text("raise SystemExit(0)\n", encoding="utf-8")
        batch = self.tools_dir / "gbrain.cmd"
        batch.write_text("@echo off\n", encoding="utf-8")
        shell = (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe") if os.name == "nt"
                 else "/bin/sh")
        cases = {"inside the project": [str(planted)], "not absolute": ["gbrain"], "a shell": [shell, "-c", "true"],
                 "a batch file": [str(batch)], "an argument inside the project": [sys.executable, str(script)],
                 "an option value inside the project": [sys.executable, "--preload=" + str(script)],
                 "an argument with ..": [sys.executable, os.path.join("..", "tool.py")],
                 "inside the Git folder": [str(self.repo / ".git" / "gbrain"), "x"]}
        if os.name != "nt":
            link = self.tools_dir / "gbrain-link"
            link.symlink_to(planted)
            cases["a link into the project"] = [str(link)]
            cases["env"] = ["/usr/bin/env", "gbrain"]
        other_spelling = self.repo.parent / self.repo.name.swapcase() / "bin" / planted.name
        if other_spelling != planted and other_spelling.exists():  # a file system that ignores case
            cases["the project under another spelling"] = [str(other_spelling)]
        for name, command in cases.items():
            with self.subTest(name):
                self.refused(dict(self.tools, gbrain=command), wiki_index.CODE_TOOL_REJECTED)
        self.refused(dict(self.tools, gbrain=[str(self.tools_dir / "absent-gbrain")]), wiki_index.CODE_TOOL_MISSING)
        self.assertEqual(self.build(tools=None)["code"], wiki_index.CODE_TOOL_NOT_CONFIGURED)
        for bad in ({"gbrain": "gbrain"}, dict(self.tools, shell=True), dict(self.tools, embedding_dimensions="8"),
                    dict(self.tools, gbrain=[]), dict(self.tools, gbrain=[sys.executable, "a\nb"])):
            with self.subTest(tools=bad):
                self.assertEqual(self.build(tools=bad)["code"], wiki_index.CODE_USAGE)
        self.assertEqual((self.calls(), self.state_root.exists()), ([], False))

    def test_the_ollama_address_is_a_literal_loopback_address_with_a_port(self):
        port = self.ollama.port
        for url in (f"http://localhost:{port}", f"http://192.168.0.5:{port}", f"https://127.0.0.1:{port}",
                    "http://127.0.0.1", f"http://user@127.0.0.1:{port}", f"http://127.0.0.1:{port}/v1",
                    f"http://127.0.0.1:{port}?x=1", f"http://0.0.0.0:{port}", f"http://2130706433:{port}",
                    f"http://127.1:{port}", f"http://example.com:{port}", f" http://127.0.0.1:{port}", "ollama"):
            with self.subTest(url=url):
                self.refused(dict(self.tools, ollama_url=url), wiki_index.CODE_NOT_LOCAL)
        self.assertEqual(self.ollama.requests, [], "an address that was refused was contacted")
        self.assertEqual(wiki_index.probe(self.repo, dict(self.tools, ollama_url=f"http://[::1]:{port}"))["code"],
                         wiki_index.CODE_OLLAMA_DOWN, "the IPv6 loopback literal is a loopback address")
        self.assertEqual(wiki_index.probe(self.repo, dict(self.tools, ollama_url=f"http://127.0.0.1:{port}/"))["ok"],
                         True)

    def test_the_model_is_one_the_server_already_has_locally_and_nothing_is_downloaded(self):
        local = dict(self.ollama.models[0])
        cases = [
            ([], "bge-m3", None, wiki_index.CODE_MODEL_MISSING),
            ([dict(local, name="other:latest", model="other:latest")], "bge-m3", None, wiki_index.CODE_MODEL_MISSING),
            ([dict(local, remote_host="https://ollama.com", remote_model="bge-m3")], "bge-m3", None,
             wiki_index.CODE_NOT_LOCAL),
            ([dict(local, digest="", size=0)], "bge-m3", None, wiki_index.CODE_NOT_LOCAL),
            ([dict(local, capabilities=["completion"])], "bge-m3", None, wiki_index.CODE_MODEL_UNUSABLE),
            ([local], "bge-m3:cloud", None, wiki_index.CODE_NOT_LOCAL),
            ([local], "gpt-oss:120b-cloud", None, wiki_index.CODE_NOT_LOCAL),
            ([local], "bge-m3", 1024, wiki_index.CODE_MODEL_UNUSABLE),
            ([local], "bge m3; rm", None, wiki_index.CODE_USAGE),
        ]
        for models, name, dimensions, code in cases:
            with self.subTest(name=name, code=code, models=len(models)):
                self.ollama.models = models
                tools = dict(self.tools, embedding_model=name, embedding_dimensions=dimensions)
                for result in (wiki_index.probe(self.repo, tools, state_root=self.state_root), self.build(tools=tools)):
                    self.assertEqual((result["ok"], result["code"]), (False, code), result)
        self.assertLessEqual(set(self.ollama.requests),
                             {("GET", "/api/version"), ("GET", "/api/tags"), ("POST", "/api/embed")})
        self.assertEqual((self.calls(), self.state_root.exists()), ([], False))

    def test_a_server_that_is_down_is_reported_and_nothing_is_started(self):
        self.ollama.stop()
        for result in (wiki_index.probe(self.repo, self.tools, state_root=self.state_root), self.build()):
            self.assertEqual((result["ok"], result["code"]), (False, wiki_index.CODE_OLLAMA_DOWN), result)
        self.assertEqual((self.calls(), self.state_root.exists()), ([], False))

    def test_probe_reports_the_tools_and_keeps_nothing(self):
        result = wiki_index.probe(self.repo, self.tools, state_root=self.state_root)
        self.assertEqual(result, {
            "schema": 1, "ok": True, "action": "probe",
            "tool": {"command": self.tools["gbrain"], "version": "gbrain 0.0.0-fake"},
            "ollama": {"url": self.ollama.url, "version": "0.0.0-fake"},
            "model": {"name": "bge-m3", "resolved": "bge-m3:latest", "digest": "79" * 32, "dimensions": 8,
                      "local": True}})
        self.assertFalse(self.state_root.exists(), "a connection test wrote index state")
        self.assertEqual(self.commands(), ["--version"])
        self.assertFalse(os.path.exists(self.calls()[0]["cwd"]), "the folder of the version check was kept")
        self.assertEqual(self.ollama.requests, [("GET", "/api/version"), ("GET", "/api/tags"), ("POST", "/api/embed")])


class SearchTest(IndexCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(self.build()["ok"])

    def test_a_result_names_the_note_its_title_the_heading_and_the_line(self):
        answer = self.search("ledger Postgres joins")
        self.assertEqual((answer["ok"], answer["mode"], answer["fallback"], answer["dropped"], answer["degraded"]),
                         (True, "semantic", None, 0, []), answer)
        self.assertEqual(answer["index"], {"state": "ready", "reasons": [], "commit": self.head, "pending": False,
                                           "unindexed": 0, "built_at": answer["index"]["built_at"]})
        self.assertEqual(answer["results"][0], {
            "path": POSTGRES_NOTE, "title": "Use Postgres for the ledger", "heading": "Decision",
            "line": self.line_of(POSTGRES_NOTE, LEDGER_LINE), "located": True, "match": "strong", "current": True,
            "snippet": LEDGER_LINE, "score": 1.0})
        nested = self.search("durable queue")["results"][0]
        self.assertEqual((nested["path"], nested["title"], nested["heading"], nested["line"]),
                         (QUEUE_NOTE, "Queue choice", "Queue choice", 3))
        self.assertEqual(self.calls()[-1]["args"], [
            "query", "durable queue", "--json", "--source-id", "oah-wiki", "--no-expand", "--limit", "50",
            "--autocut", "false", "--token-budget", "100000", "--snippet-chars", "400"])

    def test_a_query_is_one_line_of_words_and_never_an_option(self):
        self.assertEqual(self.search("  --limit\n9999   retry\tbudget ")["query"], "limit 9999 retry budget")
        self.assertEqual(self.calls()[-1]["args"][1], "limit 9999 retry budget")
        for bad in ("", "   ", "---", None, 7):
            with self.subTest(query=bad):
                self.assertEqual(self.search(bad)["code"], wiki_index.CODE_USAGE)
        self.assertEqual(self.search("retry", limit=0)["code"], wiki_index.CODE_USAGE)
        self.assertEqual(self.search("retry", fallback="web")["code"], wiki_index.CODE_USAGE)
        self.assertEqual(len(self.search("x" * 2000 + " retry")["query"]), wiki_index.Limits().max_query_chars)

    def test_results_are_bounded(self):
        answer = self.search("the ledger retry request", limit=2)
        self.assertEqual((len(answer["results"]), answer["truncated"]), (2, True))
        self.assertEqual(self.calls()[-1]["args"][7], "6")
        self.assertEqual(len(self.search("the", limit=10_000, limits=wiki_index.Limits(max_results=3))["results"]), 3)
        short = self.search("retry budget", limits=wiki_index.Limits(snippet_chars=20))["results"][0]["snippet"]
        self.assertTrue(len(short) <= 20 and short.endswith("\u2026"), short)

    def test_a_page_that_cannot_be_tied_to_a_note_is_dropped_not_guessed(self):
        self.control(extra_rows=[{"slug": "people/alice", "title": "Alice", "chunk_text": BUDGET_LINE, "score": 9.0},
                                 {"title": "no slug", "chunk_text": BUDGET_LINE}, "not a row"], wrapped=True)
        answer = self.search("retry budget")
        self.assertEqual((answer["mode"], answer["dropped"]), ("semantic", 3))
        self.assertEqual({result["path"] for result in answer["results"]}, {RETRY_NOTE})

    def test_a_note_the_tool_holds_back_is_reported_and_still_found_as_text(self):
        held = "docs/wiki/renamed.md"
        git_commit(self.repo, {held: b"---\ntitle: Renamed\nslug: custom/place\n---\n\nA zeppelin landed on the roof.\n"},
                   "a note that declares a page name of its own")
        active = self.build()["active"]
        self.assertEqual((active["files"], active["pages"], active["unindexed"]), (5, 4, {"count": 1, "paths": [held]}))
        answer = self.search("zeppelin roof")
        self.assertEqual((answer["mode"], answer["results"], answer["index"]["unindexed"]), ("semantic", [], 1))
        self.assertEqual(self.search("zeppelin roof", tools=None)["results"][0]["path"], held)
        self.control(fail=["list"])  # a tool that cannot list its pages: the index is good, the report is unknown
        again = self.build(rebuild=True)
        self.assertEqual((again["ok"], again["active"]["unindexed"]), (True, None), again)
        self.assertEqual(self.search("retry budget")["index"]["unindexed"], None)

    def test_names_and_text_outside_ascii(self):
        expected = {"path": CACHE_NOTE, "title": "캐시 정책", "heading": "캐시 정책", "line": 3, "located": True,
                    "current": True, "snippet": CACHE_LINE}
        for tools, match in ((self.tools, "strong"), (None, "text")):
            with self.subTest(match):
                first = self.search("서버가 시작할", tools=tools)["results"][0]
                self.assertEqual({key: first[key] for key in (*expected, "match")}, dict(expected, match=match))
        self.assertEqual(self.search("캐시-정책", tools=None)["results"][0]["path"], CACHE_NOTE)

    def test_the_line_is_the_one_inside_a_long_passage_that_the_query_is_about(self):
        entry = self.manifest()["entries"][2]
        body = WIKI[RETRY_NOTE].split("---\n", 2)[2].strip()  # the tool returns a whole passage: here the note's body
        self.control(extra_rows=[{"slug": entry["id"], "chunk_text": body, "score": 3.0, "evidence": "high_vector_match"}])
        backoff = "The backoff doubles after each failed attempt of the request."
        first = self.search("when does the backoff double")["results"][0]
        self.assertEqual((first["path"], first["line"], first["heading"], first["snippet"], first["located"]),
                         (RETRY_NOTE, self.line_of(RETRY_NOTE, backoff), "Backoff", backoff, True))
        first = self.search("xyzzy plugh")["results"][0]  # no word in common: the passage's first line
        self.assertEqual((first["line"], first["heading"], first["match"]),
                         (self.line_of(RETRY_NOTE, "# Retry policy"), "Retry policy", "strong"))

    def test_a_passage_that_cannot_be_found_is_marked_not_located(self):
        self.control(extra_rows=[{"slug": self.manifest()["entries"][2]["id"], "chunk_text": "words the note lacks",
                                  "score": 2.0}])
        first = self.search("backoff")["results"][0]
        self.assertEqual((first["path"], first["located"], first["line"], first["snippet"]),
                         (RETRY_NOTE, False, 6, "words the note lacks"))

    def test_search_falls_back_to_text_and_says_why(self):
        absent = dict(self.tools, gbrain=[str(self.tools_dir / "absent-gbrain")])
        cases = [(None, wiki_index.CODE_TOOL_NOT_CONFIGURED), (absent, wiki_index.CODE_TOOL_MISSING),
                 (dict(self.tools, embedding_model="nomic-embed-text"), wiki_index.CODE_MODEL_CHANGED),
                 (dict(self.tools, embedding_dimensions=1024), wiki_index.CODE_MODEL_CHANGED)]
        queries = self.commands().count("query")
        for tools, code in cases:
            with self.subTest(code=code, tools=tools):
                answer = self.search("retry budget", tools=tools)
                self.assertEqual((answer["ok"], answer["mode"], answer["fallback"]["code"]), (True, "text", code))
                self.assertEqual(answer["results"][0], {
                    "path": RETRY_NOTE, "title": "Retry policy", "heading": "Retry policy",
                    "line": self.line_of(RETRY_NOTE, BUDGET_LINE), "located": True, "match": "text", "current": True,
                    "snippet": BUDGET_LINE + " ## Backoff The backoff doubles after each failed attempt of the request.",
                    "score": 1.0})
                self.assertTrue(answer["fallback"]["message"])
        self.ollama.models[0]["digest"] = "ab" * 32  # the same name, other weights: the vectors no longer compare
        self.assertEqual(self.search("retry budget")["fallback"]["code"], wiki_index.CODE_MODEL_CHANGED)
        self.ollama.models[0]["digest"] = "79" * 32
        self.control(fail=["query"])
        self.assertEqual(self.search("retry budget")["fallback"]["code"], wiki_index.CODE_TOOL_FAILED)
        self.control()
        self.assertEqual(self.search("retry budget")["mode"], "semantic")
        self.ollama.stop()
        down = self.search("retry budget")
        self.assertEqual((down["mode"], down["fallback"]["code"], down["index"]["state"]),
                         ("text", wiki_index.CODE_OLLAMA_DOWN, "ready"))
        self.assertEqual(self.commands().count("query"), queries + 2, "a tool ran although the index could not answer")
        only_why = self.search("retry budget", fallback="none")
        self.assertEqual((only_why["mode"], only_why["results"], only_why["fallback"]["code"]),
                         ("none", [], wiki_index.CODE_OLLAMA_DOWN))

    def test_without_an_index_search_is_text_search(self):
        other = self.state_root.parent / "other-state"
        answer = wiki_index.search(self.repo, "retry budget", tools=self.tools, state_root=other)
        self.assertEqual((answer["mode"], answer["fallback"]["code"], answer["index"]["state"]),
                         ("text", wiki_index.CODE_NOT_BUILT, "not_built"))
        self.assertFalse(other.exists(), "a search without an index created state")

    def test_a_stale_index_still_answers_and_says_which_lines_moved(self):
        moved = FILES[RETRY_NOTE].replace(b"# Retry policy\n", b"# Retry policy\n\nA new first paragraph.\n")
        git_commit(self.repo, {RETRY_NOTE: moved}, "edit")
        answer = self.search("retry budget")
        self.assertEqual((answer["mode"], answer["index"]["state"], answer["index"]["reasons"],
                          answer["index"]["commit"]), ("semantic", "stale", ["content_changed"], self.head))
        first = answer["results"][0]
        self.assertEqual((first["path"], first["line"], first["current"]),
                         (RETRY_NOTE, self.line_of(RETRY_NOTE, BUDGET_LINE), False))
        unchanged = self.search("durable queue")["results"][0]
        self.assertEqual((unchanged["path"], unchanged["current"]), (QUEUE_NOTE, True))

    def test_one_search_at_a_time_and_a_busy_index_falls_back(self):
        with StateLock(self.index_dir() / "query.lock"):
            answer = self.search("retry budget", timeouts=wiki_index.Timeouts(lock=0.0))
        self.assertEqual((answer["mode"], answer["fallback"]["code"], answer["results"][0]["path"]),
                         ("text", wiki_index.CODE_BUSY, RETRY_NOTE))
        self.assertEqual(self.search("retry budget")["mode"], "semantic")

    def test_a_degraded_answer_of_the_tool_is_reported(self):
        self.control(degraded="embed_timeout", wrapped=True)
        answer = self.search("retry budget")
        self.assertEqual((answer["mode"], answer["degraded"], answer["results"][0]["path"]),
                         ("semantic", ["embed_timeout"], RETRY_NOTE))

    def test_an_index_folder_that_changed_on_disk_is_not_trusted(self):
        entry = self.manifest()["entries"][2]
        mirrored = self.generation() / "mirror" / f"{entry['id']}.md"
        mirrored.write_bytes(mirrored.read_bytes().replace(b"three", b"seven"))
        answer = self.search("retry budget")
        self.assertEqual((answer["mode"], answer["fallback"]["code"]), ("text", wiki_index.CODE_STATE))
        self.assertIn("three attempts", answer["results"][0]["snippet"])


class TextSearchTest(IndexCase):
    def text(self, query: str, **options: object) -> dict:
        return wiki_index.text_search(self.repo, query, **options)

    def test_it_reads_the_files_as_they_are_by_the_same_rule(self):
        (self.repo / "docs/wiki/draft.md").write_bytes(b"# Draft\r\n\r\nAn uncommitted zebra crossing rule.\r\n")
        (self.repo / "docs/wiki/decisions/0003-zebra.md").write_bytes(b"A zebra decision without a heading.\n")
        answer = self.text("zebra crossing")
        self.assertEqual((answer["ok"], answer["mode"], answer["notes"], answer["truncated"]), (True, "text", 6, False))
        self.assertEqual(answer["results"], [
            {"path": "docs/wiki/draft.md", "title": "Draft", "heading": "Draft", "line": 3, "located": True,
             "match": "text", "current": True, "snippet": "An uncommitted zebra crossing rule.", "score": 1.0},
            {"path": "docs/wiki/decisions/0003-zebra.md", "title": "0003-zebra", "heading": "", "line": 1,
             "located": True, "match": "text", "current": True, "snippet": "A zebra decision without a heading.",
             "score": 0.5}])
        budget = self.text("retry budget")
        self.assertEqual({result["path"] for result in budget["results"]}, {RETRY_NOTE},
                         "navigation, archive, raw, template, hidden or non-Markdown files were searched")
        self.assertEqual(self.text("0002-queue")["results"][0]["path"], QUEUE_NOTE, "a file name can be searched")
        self.assertEqual((self.calls(), self.ollama.requests, self.state_root.exists()), ([], [], False))

    def test_it_follows_no_link(self):
        outside = make_tmp_dir(self) / "outside"
        outside.mkdir()
        (outside / "xylophone.md").write_bytes(b"A xylophone outside the project.\n")
        try:
            directory_alias(outside, self.repo / "docs/wiki/linked-folder")
            directory_alias(outside, self.repo / "docs/linked-wiki")
        except (OSError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"this account cannot create a folder link ({exc})")
        if os.name != "nt":
            (self.repo / "docs/wiki/linked.md").symlink_to(outside / "xylophone.md")
        self.assertEqual(self.text("xylophone")["results"], [])
        linked = self.text("xylophone", selection={"wiki_dir": "docs/linked-wiki"})
        self.assertEqual((linked["ok"], linked["results"], linked["notes"]), (True, [], 0))
        self.assertEqual(self.search("xylophone", tools=None)["results"], [])

    def test_it_is_bounded(self):
        answer = self.text("the", limit=2)
        self.assertEqual((len(answer["results"]), answer["truncated"]), (2, True))
        capped = self.text("the", limits=wiki_index.Limits(max_files=1))
        self.assertEqual((capped["notes"], capped["truncated"]), (1, True))
        self.assertEqual(self.text("ledger", limits=wiki_index.Limits(max_file_bytes=len(FILES[QUEUE_NOTE])))[
            "results"][0]["path"], QUEUE_NOTE)
        self.assertEqual(self.text("")["code"], wiki_index.CODE_USAGE)
        self.assertEqual(self.text("ledger", selection={"wiki_dir": "docs/absent"})["results"], [])


class BridgeTest(IndexCase):
    def ask(self, request: object) -> tuple[int, dict]:
        data = request if isinstance(request, bytes) else json.dumps(request).encode("utf-8")
        done = subprocess.run([sys.executable, "-m", "oah.project_tools_wiki_index"], input=data, capture_output=True,
                              env=dict(os.environ, PYTHONPATH=str(SRC)), timeout=300)
        self.assertEqual(done.stderr, b"")
        done.stdout.decode("ascii")
        return done.returncode, json.loads(done.stdout)

    def test_one_json_request_in_one_json_result_out(self):
        base = {"worktree": str(self.repo), "state_root": str(self.state_root)}
        code, result = self.ask(dict(base, action="status", tools=self.tools))
        self.assertEqual((code, result["ok"], result["state"], result["source"]["files"]), (0, True, "not_built", 4))
        code, result = self.ask(dict(base, action="build", tools=self.tools))
        self.assertEqual((code, result["ok"], result["state"]), (0, True, "ready"), result)
        code, result = self.ask(dict(base, action="search", tools=self.tools, query="ledger joins", limit=1))
        self.assertEqual((code, result["mode"], result["results"][0]["path"]), (0, "semantic", POSTGRES_NOTE))
        code, result = self.ask({"action": "text_search", "worktree": str(self.repo), "query": "durable queue"})
        self.assertEqual((code, result["results"][0]["path"]), (0, QUEUE_NOTE))
        code, result = self.ask(dict(base, action="search", tools=self.tools, query="서버가 시작할"))
        self.assertEqual((code, result["query"], result["results"][0]["path"], result["results"][0]["title"]),
                         (0, "서버가 시작할", CACHE_NOTE, "캐시 정책"))
        code, result = self.ask(dict(base, action="probe", tools=self.tools))
        self.assertEqual((code, result["model"]["dimensions"]), (0, 8))
        code, result = self.ask(dict(base, action="build", tools=self.tools, rebuild=True))
        self.assertEqual((code, result["changed"], result["state"]), (0, True, "ready"))

    def test_refusals_and_bad_requests_have_their_own_exit_codes(self):
        base = {"worktree": str(self.repo), "state_root": str(self.state_root)}
        self.assertEqual(self.ask(dict(base, action="build"))[0], 1)
        code, result = self.ask(dict(base, action="build"))
        self.assertEqual((code, result["code"]), (1, wiki_index.CODE_TOOL_NOT_CONFIGURED))
        for request in (b"not json", b"[]", {"action": "delete", "worktree": str(self.repo)},
                        dict(base, action="status", shell=True), {"action": "status"},
                        dict(base, action="status", state_root=7), dict(base, action="status", state_root="relative"),
                        dict(base, action="build", tools=self.tools, rebuild="yes"),
                        {"action": "text_search", "worktree": str(self.repo), "state_root": "x", "query": "q"}):
            with self.subTest(request=request):
                code, result = self.ask(request)
                self.assertEqual((code, result["ok"], result["code"]), (2, False, wiki_index.CODE_USAGE))
        with contextlib.redirect_stderr(io.StringIO()) as usage:
            self.assertEqual(wiki_index.main(["--help"]), 2)
        self.assertIn("standard input", usage.getvalue())


class PlaceTest(unittest.TestCase):
    def test_the_index_folder_of_a_worktree_follows_the_documented_rule(self):
        path = "C:\\work\\project" if os.name == "nt" else "/srv/work/project"
        data = path.encode("utf-16-le") if os.name == "nt" else path.encode("utf-8")
        self.assertEqual(wiki_index.worktree_id(path), hashlib.sha256(data).hexdigest()[:16])
        self.assertEqual(wiki_index.worktree_id(pathlib.Path(path)), wiki_index.worktree_id(path))
        home = "USERPROFILE" if os.name == "nt" else "HOME"
        folder = pathlib.Path(path)
        self.assertEqual(wiki_index.user_state_root({home: path}), folder / ".oah" / "state")
        self.assertEqual(wiki_index.user_state_root({home: path, "OAH_HOME": str(folder / "custom")}),
                         folder / "custom" / "state")
        self.assertEqual(wiki_index.user_state_root({"OAH_HOME": "relative"}),
                         pathlib.Path.cwd() / "relative" / "state")
        for env in ({}, {home: ""}, {home: "relative"}):
            with self.subTest(env=env), self.assertRaises(wiki_index.Refusal) as caught:
                wiki_index.user_state_root(env)
            self.assertEqual(caught.exception.code, wiki_index.CODE_STATE)


LIVE = os.environ.get("OAH_WIKI_INDEX_LIVE", "")


@unittest.skipUnless(LIVE, "set OAH_WIKI_INDEX_LIVE to the JSON of this machine's tools to use a real gbrain and Ollama")
class LiveLocalToolsTest(unittest.TestCase):
    """The same path with the real tools of this machine, in a private state folder of the test.

    OAH_WIKI_INDEX_LIVE is {"gbrain": [...], "ollama_url": "http://127.0.0.1:<port>", "embedding_model": "<name>"}.
    The Ollama server must already run; the test starts, pulls and deletes nothing."""

    def models(self, tools: dict) -> object:
        return wiki_index._ollama(tools["ollama_url"], "/api/tags", None, 10)

    def test_build_search_and_fall_back_with_the_real_tools(self):
        tools = json.loads(LIVE)
        repo = make_git_repo(self)
        self.addCleanup(force_remove, repo)
        head = git_commit(repo, FILES, "wiki")
        private = make_private_dir(self)
        self.addCleanup(force_remove, private)
        state_root = private / "state"
        before = self.models(tools)
        probe = wiki_index.probe(repo, tools, state_root=state_root)
        self.assertTrue(probe["ok"], probe)
        self.assertFalse(state_root.exists())
        built = wiki_index.build(repo, tools, state_root=state_root)
        self.assertEqual((built["ok"], built.get("state")), (True, "ready"), built)
        self.assertEqual((built["active"]["commit"], built["active"]["files"], built["active"]["pages"],
                          built["active"]["unindexed"]), (head, 4, 4, {"count": 0, "paths": []}))
        self.assertGreaterEqual(built["active"]["chunks"], 4)
        answer = wiki_index.search(repo, "Which database stores the ledger, and why?", tools=tools,
                                   state_root=state_root)
        self.assertEqual((answer["mode"], answer["fallback"], answer["dropped"]), ("semantic", None, 0), answer)
        first = answer["results"][0]
        self.assertEqual((first["path"], first["title"], first["located"], first["current"]),
                         (POSTGRES_NOTE, "Use Postgres for the ledger", True, True), answer)
        self.assertIn(first["line"], range(1, len(WIKI[POSTGRES_NOTE].split("\n")) + 1))
        again = wiki_index.build(repo, tools, state_root=state_root)
        self.assertEqual((again["ok"], again["changed"]), (True, False))
        absent = dict(tools, embedding_model="oah-absent-model")
        self.assertEqual(wiki_index.search(repo, "ledger", tools=absent, state_root=state_root)["mode"], "text")
        self.assertEqual(self.models(tools), before, "the models of the Ollama server changed")
        self.assertEqual(subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                                        check=True).stdout, b"")


if __name__ == "__main__":
    unittest.main()
