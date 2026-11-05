# 설계 전송 시험 — materials v2 검증·참조 계획·로컬 정책·DATA 설치·이력 노출·읽기 관측·원장·정리·청크·스냅샷 처분을 실제 코드로 본다.
"""B-T12 and B-T15 (SC4/B6): the reference transport, its durable origin and its lifecycle.

Most cases drive the real `oah` CLI with fake engines; a stream-json fake Claude reads the brief's DATA files as
the pinned Read tool reports them. Cases that need macOS arm64 (the only qualified reference profile) skip
elsewhere with that reason. Nothing here launches a real engine or reaches the network.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import pathlib
import platform
import stat
import subprocess
import sys
import unittest
from unittest import mock

from oah.design import task_context as tc
from oah.design.binding import PacketDesign, replace_section
from oah.design.model import DesignError, canonical
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir
from tests.test_design_store import TODAY, DesignRepo
from tests.test_intent_wiring import clean_env, packet_text

FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
FAKE_CODEX = [sys.executable, str(FAKES / "fake_codex.py")]
NATIVE = sys.platform == "darwin" and platform.machine() == "arm64"
NATIVE_ONLY = "the reference profile is qualified only for native macOS arm64"

FAKE_STREAM = '''# 스트림 가짜 Claude — 브리프의 Design data 파일을 Read 도구 이벤트로 읽은 것처럼 stream-json 을 낸다.
import json, os, re, sys
sys.path.insert(0, {fakes!r})
import fake_claude, fakelib

def main(argv):
    if "-p" in argv and os.environ.get("FAKE_REDIRECTED_CHILD"):
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        open(os.environ["FAKE_REDIRECTED_CHILD"], "w").write(str(child.pid))
    if "-p" not in argv or "stream-json" not in argv:
        return fake_claude.main(argv)
    prompt = fakelib.read_stdin()
    fakelib.record(argv, prompt)
    fakelib.misbehave()
    mode = os.environ.get("FAKE_STREAM_MODE", "full")
    out = [{{"type": "system", "subtype": "init", "session_id": "s1", "cwd": os.getcwd(),
            "tools": ["Read", "Grep", "Glob", "Bash"], "model": "fake"}}]
    n = 0
    for path in re.findall(r"^- `([^`]+)` — original .*: read whole$", prompt, re.M):
        if mode == "none":
            break
        lines = open(path, "rb").read().decode("utf-8").split("\\n")
        if lines and lines[-1] == "":
            lines.pop()
        step = 2000 if mode != "partial" else max(1, len(lines) // 2)
        stop = len(lines) if mode != "partial" else step
        start = 1
        while start <= stop:
            chunk = lines[start - 1:start - 1 + step]
            n += 1
            out.append({{"type": "assistant", "session_id": "s1", "parent_tool_use_id": None, "message": {{"content": [
                {{"type": "tool_use", "id": f"t{{n}}", "name": "Read", "input": {{"file_path": path}}}}]}}}})
            out.append({{"type": "user", "session_id": "s1", "parent_tool_use_id": None, "message": {{"content": [
                {{"type": "tool_result", "tool_use_id": f"t{{n}}",
                  "content": "\\n".join(f"{{i:6d}}\\u2192{{line}}" for i, line in enumerate(chunk, start))}}]}},
                "tool_use_result": {{"type": "text", "file": {{"filePath": path, "content": "\\n".join(chunk),
                                    "numLines": len(chunk), "startLine": start, "totalLines": len(lines)}}}}}})
            start += step
    if os.environ.get("FAKE_STREAM_GIT_ADD"):
        import subprocess
        subprocess.run(["git", "add", "-A"], check=True)
    out.append({{"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s1",
                "modelUsage": {{"claude-opus-fake": {{"inputTokens": 1, "outputTokens": 1}}}}}})
    for event in out:
        print(json.dumps(event))
    return fakelib.exit_code()

sys.exit(main(sys.argv[1:]))
'''


def license_text(seed: str, kb: int) -> bytes:
    line = f"Permission notice {seed}: the quick brown fox jumps over the lazy dog, again and lawfully."
    return ("\n".join(f"{line} #{i}" for i in range(kb * 1024 // (len(line) + 8))) + "\n").encode("utf-8")


def materials_part(blobs, *, provider: str = "github", member: bool = True, narrative: str = "We pick lib X.",
                   extra: dict | None = None) -> str:
    files, items = [], []
    for name, data in blobs:
        digest = hashlib.sha256(data).hexdigest()
        if member:
            files.append({"path": name, "mode": "100644", "blob": "b" * 40, "size": len(data), "sha256": digest})
        items.append({"id": digest, "kind": "license", "path_or_field": name, "encoding": "base64",
                      "bytes": len(data), "sha256": digest, "data": base64.b64encode(data).decode("ascii")})
    evidence = {"id": hashlib.sha256(b"evidence").hexdigest(),
                "source": {"provider": provider, "locator": "github.com/acme/lib"},
                "manifest": {"digest": "c" * 64, "files": files}, "blobs": items}
    record = {"version": 2, "lang": "en", "narrative": narrative, "decision_inputs": {"d1": {"need": "storage"}},
              "decisions": [{"id": "d1", "need": "storage", "alternatives": ["a", "b"]}], "materials": [],
              "stacks": [], "portable_evidence": [evidence], **(extra or {})}
    return f"## Materials\nThe library we chose and its license.\n```oah-materials\n{canonical(record).decode()}\n```\n"


BIG = (("LICENSE", license_text("a", 150)), ("NOTICE", license_text("b", 60)))


class MaterialsRecordTest(unittest.TestCase):
    """The neutral MaterialsRecordV2 validator: only verified public blob data may leave the inline text."""

    def test_recognized_record_and_projection_rebuild_it_exactly(self):
        text = materials_part(BIG)
        part, problem = tc.materials_part(text)
        self.assertIsNone(problem)
        self.assertEqual(part.human, "## Materials\nThe library we chose and its license.\n")
        self.assertEqual([item.projectable for item in part.originals], [True, True])
        refs = {(item.evidence, item.blob): item.sha256 for item in part.originals}
        projection = tc._projection(part, refs)
        self.assertNotIn(base64.b64encode(BIG[0][1]).decode()[:40], projection)
        self.assertIn("We pick lib X.", projection)
        self.assertTrue(tc._reconstructs(part, projection, refs, {(o.evidence, o.blob): o.data
                                                                  for o in part.originals}))
        body = json.loads(projection.split("\n")[3])
        original = json.loads(text.split("\n")[3])
        for blob in body["portable_evidence"][0]["blobs"]:
            self.assertEqual(set(blob["data"]), {"oah_read", "bytes", "sha256"})
        for blob in body["portable_evidence"][0]["blobs"] + original["portable_evidence"][0]["blobs"]:
            blob.pop("data")
        self.assertEqual(body, original)  # nothing but the data strings changed

    def test_parts_without_the_block_are_not_materials(self):
        self.assertEqual(tc.materials_part("## Design\nPeople book a lesson.\n"), (None, None))

    def test_edited_unknown_or_unverified_records_are_never_projected(self):
        good = materials_part(BIG[:1])
        body = good.split("\n")[3]
        record = json.loads(body)
        cases = {
            "non-canonical": good.replace(body, json.dumps(record, sort_keys=True)),
            "duplicate key": good.replace(body, body[:-1] + ',"version":2}'),
            "unknown field": materials_part(BIG[:1], extra={"note": "x"}),
            "fence not last": good + "trailing\n",
            "unclosed fence": good.replace("\n```\n", "\n``\n"),
        }
        broken = dict(record)
        broken["portable_evidence"] = [dict(record["portable_evidence"][0])]
        broken["portable_evidence"][0]["blobs"] = [dict(record["portable_evidence"][0]["blobs"][0], bytes=1)]
        cases["size mismatch"] = good.replace(body, canonical(broken).decode())
        broken["portable_evidence"][0]["blobs"] = [dict(record["portable_evidence"][0]["blobs"][0], data="!!")]
        cases["bad base64"] = good.replace(body, canonical(broken).decode())
        for name, text in cases.items():
            with self.subTest(name):
                part, problem = tc.materials_part(text)
                self.assertIsNone(part)
                self.assertIsNotNone(problem)

    def test_local_or_unlisted_evidence_stays_inline(self):
        for options in ({"provider": "local"}, {"member": False}):
            with self.subTest(options):
                part, problem = tc.materials_part(materials_part(BIG[:1], **options))
                self.assertIsNone(problem)
                self.assertEqual([item.projectable for item in part.originals], [False])
                self.assertIn("stays inline", part.originals[0].reason)


class CodecAndScreenTest(unittest.TestCase):
    def test_read_codec_limits(self):
        self.assertIsNone(tc.read_codec_problem(b"one\ntwo\n"))
        for data, word in ((b"a\r\nb\n", "CR"), ("﻿a\n".encode(), "byte-order"), (b"a\x00\n", "NUL"),
                           (b"\xff\xfe", "UTF-8"), (b"x" * 2001 + b"\n", "characters")):
            with self.subTest(word):
                self.assertIn(word, tc.read_codec_problem(data))

    def test_screening_names_the_rule_never_the_match(self):
        token = "s" + "k-" + "A1b2C3d4" * 4
        self.assertEqual(tc.screen_hit(f"see {token} here"), "key")
        self.assertIsNone(tc.screen_hit("an ordinary license line"))


class DesignTask(DesignRepo):
    """A real repository, an accepted design, a bound packet and a started lease, driven through `python -m oah`."""

    def build(self, parts: dict, *, claude_version: str = "2.1.318", scope: str = "src/"):
        self.repo = self.make_repo()
        store = self.store(self.repo)
        store.add_project("acme", title="Acme", event="ev-project", today=TODAY)
        r1 = store.new("acme", "booking", title="Booking", parts=parts, event="ev-r1", today=TODAY)
        store.accept("acme", "booking", revision=1, digest=r1.digest, reason="ok", today=TODAY)
        section = PacketDesign.of(store.context("acme", "booking").binding("accepted"))
        self.work = make_tmp_dir(self)
        fake = self.work / "fake_stream_claude.py"
        fake.write_text(FAKE_STREAM.format(fakes=str(FAKES)), encoding="utf-8")
        codex_home = self.work / "codex-home"
        codex_home.mkdir()
        (codex_home / "models_cache.json").write_text(json.dumps({"models": [{
            "slug": "gpt-test-a", "is_default": True,
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]}]}))
        toml = lambda argv: "[" + ", ".join(json.dumps(part) for part in argv) + "]"  # noqa: E731
        config = (f"[engines.claude]\ncommand = {toml([sys.executable, str(fake)])}\n"
                  "quality_order = [\"opus\", \"sonnet\"]\n"
                  f"[engines.codex]\ncommand = {toml(FAKE_CODEX)}\nquality_order = [\"gpt-test-a\"]\n")
        git_commit(self.repo, {".oah/tasks/t1/PACKET.md": replace_section(packet_text(), section),
                               "src/app.py": "x = 1\n", ".oah/config.toml": config}, "packet")
        self.state_dir = self.state(self.repo)
        self.record = self.work / "record.json"
        self.env = clean_env(self.work / "home", OAH_STATE_DIR=str(self.state_dir), CODEX_HOME=str(codex_home),
                             FAKE_RECORD=str(self.record), FAKE_CLAUDE_VERSION=claude_version)
        started = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", ".oah/tasks/t1/PACKET.md",
                           "--scope", scope)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        return started

    def oah(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "oah", *args], cwd=self.repo, capture_output=True, text=True,
                              encoding="utf-8", env=dict(self.env, **(env or {})), timeout=300)

    def events(self, prefix: str = "design_transport") -> list[dict]:
        path = self.state_dir / "events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [event for event in map(json.loads, lines) if event["event"].startswith(prefix)]

    def last_run(self) -> pathlib.Path:
        return sorted((self.state_dir / "runs" / "t1").iterdir())[-1]

    def returned(self) -> pathlib.Path:
        from oah import intent

        parsed = intent.parse((self.repo / ".oah/tasks/t1/PACKET.md").read_text(encoding="utf-8"))
        path = self.work / "return.txt"
        path.write_text("\n".join(["Summary: done.", parsed.line(), *(f"{ac}: met — ok" for ac in parsed.acs)])
                        + "\n", encoding="utf-8")
        return path

    def judged(self):
        from oah.lease import Store
        from oah.lease_cli import lease_design

        lease = next(lease for lease, _stale in Store(self.state_dir).snapshot() if lease.task == "t1")
        return lease, lease_design(lease, self.state_dir)


BIG_PARTS = {"design": "People book.\n", "request": "I want lessons.\n", "materials": materials_part(BIG)}


class TransportPlanTest(DesignTask, unittest.TestCase):
    def test_small_designs_stay_whole_inline(self):
        self.build({"design": "People book a lesson.\n"})
        _lease, judged = self.judged()
        block = tc.select(judged, task="t1", engine="codex")
        self.assertTrue(block.delivered)
        self.assertIsNone(block.transport)
        self.assertEqual(block.record["receiving"]["reference_delivery"], "not-required")
        self.assertFalse(block.record["delivered"])  # delivery is a terminal fact of a run

    def test_reference_plan_keeps_every_semantic_field_inline(self):
        self.build(BIG_PARTS)
        _lease, judged = self.judged()
        self.assertFalse(tc.select(judged, task="t1", engine="claude").delivered)
        plan = tc.prepare_transport(judged, task="t1", run="r1", root=self.repo, engine="claude", operation="work")
        text = plan.block.text
        self.assertLessEqual(len(text.encode("utf-8")), tc.INLINE_LIMIT)
        self.assertIn("We pick lib X.", text)
        self.assertIn("People book.", text)
        self.assertIn("### Design data", text)
        self.assertEqual(hashlib.sha256(plan.manifest_bytes).hexdigest(), plan.manifest)
        payload = json.loads(plan.manifest_bytes)
        self.assertEqual(set(payload), {"version", "target_identity", "files"})
        self.assertEqual(payload["target_identity"]["run"], "r1")
        kinds = sorted(item["kind"] for item in payload["files"])
        self.assertEqual(kinds, [tc.KIND_ORIGINAL, tc.KIND_ORIGINAL, tc.KIND_PART])
        for item in plan.files:
            self.assertEqual(hashlib.sha256(item.data).hexdigest(), item.name)
            self.assertIn(str(self.repo / tc.DATA_DIR / plan.manifest / item.name), text)
        self.assertEqual({item["reason"] for item in plan.record["omitted_inline"]}, {"portable-original-encoding"})
        self.assertNotIn("data", json.dumps(plan.record["parts"]))  # digest-only record, no narrative/originals
        self.assertNotIn("We pick lib X.", json.dumps(plan.record))

    def test_codex_and_windows_refuse_before_any_launch(self):
        self.build(BIG_PARTS)
        _lease, judged = self.judged()
        with self.assertRaises(DesignError) as caught:
            tc.select(judged, task="t1", engine="codex")
        self.assertEqual(caught.exception.code, tc.CODEX_REFERENCE_PENDING_CODE)
        with self.assertRaises(DesignError) as caught:
            tc.select(judged, task="t1", engine="claude", platform="win32")
        self.assertEqual(caught.exception.code, tc.REFERENCE_UNAVAILABLE_CODE)
        self.assertIn(tc.WINDOWS_PENDING, str(caught.exception))

    def test_caps_codec_and_privacy_refuse_with_measured_reasons(self):
        self.build(BIG_PARTS)
        _lease, judged = self.judged()
        prepare = lambda: tc.prepare_transport(judged, task="t1", run="r1", root=self.repo,  # noqa: E731
                                               engine="claude", operation="work")
        with mock.patch.object(tc, "BUNDLE_LIMIT", 1000):
            with self.assertRaises(DesignError) as caught:
                prepare()
        self.assertEqual(caught.exception.code, tc.TOO_LARGE_CODE)
        with mock.patch.object(tc, "ENTRY_LIMIT", 1):
            with self.assertRaises(DesignError) as caught:
                prepare()
        self.assertEqual(caught.exception.code, tc.TOO_LARGE_CODE)
        with mock.patch.object(tc, "read_codec_problem", return_value="it holds CR line ends"):
            with self.assertRaises(DesignError) as caught:
                prepare()
        self.assertEqual(caught.exception.code, tc.REFERENCE_UNAVAILABLE_CODE)
        with mock.patch.object(tc, "screen_hit", side_effect=lambda text: "key" if "fox" in text else None):
            with self.assertRaises(DesignError) as caught:
                prepare()
        self.assertIn("privacy rule key", str(caught.exception))
        self.assertNotIn("fox", str(caught.exception))

    def test_unverified_originals_cannot_shrink_a_design(self):
        self.build({"design": "People book.\n", "materials": materials_part(BIG, provider="local")})
        _lease, judged = self.judged()
        with self.assertRaises(DesignError) as caught:
            tc.prepare_transport(judged, task="t1", run="r1", root=self.repo, engine="claude", operation="work")
        self.assertEqual(caught.exception.code, tc.TOO_LARGE_CODE)

    def test_start_and_task_readouts_offer_the_semantic_block(self):
        started = self.build(BIG_PARTS)
        self.assertIn("### Design data", started.stdout)
        self.assertIn("installs them as read-only files", started.stdout)
        shown = self.oah("design", "task", "t1", "--json")
        data = json.loads(shown.stdout)
        self.assertFalse(data["delivered"])
        self.assertTrue(data["offered"])
        self.assertIn("We pick lib X.", data["block"])


@unittest.skipIf(os.path.exists("/Library/Application Support/ClaudeCode")
                 or os.path.exists("/Library/Managed Preferences"), "this host has managed Claude Code policy")
class PolicyCollectorTest(unittest.TestCase):
    def setUp(self):
        self.home = make_tmp_dir(self)
        self.root = make_tmp_dir(self)
        self.paths = [str(self.root / tc.DATA_DIR / ("a" * 64) / ("b" * 64))]

    def collect(self):
        return tc.collect_process_policy(self.root, {"HOME": str(self.home)}, operation="work",
                                         data_paths=self.paths)

    def settings(self, where: pathlib.Path, value) -> None:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def test_clean_local_profile_is_verified_with_only_the_remote_residual(self):
        found = self.collect()
        self.assertEqual(found["local_policy"], "verified")
        self.assertTrue(found["launch_eligible"])
        self.assertEqual(found["remote_policy"], "cli-enforced-unobserved")

    def test_local_denials_unknowns_and_irrelevant_hooks(self):
        user = self.home / ".claude" / "settings.json"
        cases = [
            ({"permissions": {"deny": ["Read(.oah-design-data/**)"]}}, "denied"),
            ({"permissions": {"ask": ["Read"]}}, "denied"),
            ({"hooks": {"PreToolUse": [{"matcher": "Read|Grep", "hooks": []}]}}, "unknown"),
            ({"hooks": {"PreToolUse": [{"matcher": "WebSearch", "hooks": []}]}}, "verified"),
            ({"hooks": {"SessionStart": [{"hooks": []}]}}, "verified"),
            ("{not json", "unknown"),
            ({"policyHelper": "x"}, "unknown"),
            ({"enabledPlugins": {"p@m": True}}, "unknown"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.settings(user, value)
                self.assertEqual(self.collect()["local_policy"], expected)

    def test_enabled_plugins_are_inventoried_without_running_them(self):
        base = self.home / "plugin"
        self.settings(self.home / ".claude" / "settings.json", {"enabledPlugins": {"p@m": True}})
        self.settings(self.home / ".claude" / "plugins" / "installed_plugins.json",
                      {"version": 2, "plugins": {"p@m": [{"scope": "user", "installPath": str(base)}]}})
        self.settings(base / "hooks" / "hooks.json", {"hooks": {"SessionStart": [{"hooks": []}]}})
        self.assertEqual(self.collect()["local_policy"], "verified")
        self.settings(base / "hooks" / "hooks.json", {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": []}]}})
        self.assertEqual(self.collect()["local_policy"], "unknown")

    def test_project_settings_of_the_launch_root_are_read(self):
        self.settings(self.root / ".claude" / "settings.local.json", {"permissions": {"deny": ["Read(**)"]}})
        self.assertEqual(self.collect()["local_policy"], "denied")


@unittest.skipIf(os.name == "nt", "the anchored POSIX install is the qualified route")
class InstallExposureTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        self.head = git_commit(self.repo, {"README.md": "x\n"}, "base")
        self.manifest = "a" * 64
        self.payloads = {hashlib.sha256(data).hexdigest(): data for data in (b"one\n", b"two\n")}
        self.record = {"manifest": self.manifest, "h0": {"head": self.head, "unborn": False}}

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True,
                              env=git_env()).stdout

    def installed(self) -> tc.Installation:
        done = tc.install(self.repo, self.manifest, self.payloads)
        self.addCleanup(done.close)
        return done

    def test_create_only_read_only_install_and_exact_roster(self):
        done = self.installed()
        directory = self.repo / tc.DATA_DIR / self.manifest
        self.assertEqual(sorted(os.listdir(directory)), sorted(self.payloads))
        for name in self.payloads:
            self.assertEqual(stat.S_IMODE(os.lstat(directory / name).st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), 0o555)
        self.assertIsNone(tc.check_leaves(done))
        self.assertTrue(done.data_created)
        with self.assertRaises(tc.InstallRefused):
            tc.install(self.repo, self.manifest, self.payloads)  # an existing M is never adopted
        os.chmod(directory, 0o755)
        (directory / "extra").write_text("x")
        self.assertIn("unexpected", tc.check_leaves(done))

    def test_existing_parent_is_kept_and_none_of_its_entries_adopted(self):
        (self.repo / tc.DATA_DIR).mkdir()
        (self.repo / tc.DATA_DIR / "mine.txt").write_text("user\n")
        done = self.installed()
        self.assertFalse(done.data_created)
        steps = []
        tc.remove_leaves(done, sorted(done.leaves), before=steps.append, after=lambda *a: steps.append(a))
        tc.remove_directories(done, before=steps.append, after=lambda *a: steps.append(a))
        self.assertTrue((self.repo / tc.DATA_DIR / "mine.txt").exists())
        self.assertFalse((self.repo / tc.DATA_DIR / self.manifest).exists())
        self.assertEqual(steps[-1], (self.manifest, "absent", "dir"))

    def test_product_commits_pass_and_every_exposure_is_found(self):
        done = self.installed()
        clean = tc.verify_transport_origin(self.record, self.repo, installation=done)
        self.assertEqual(clean["state"], "clean", clean)
        git_commit_paths(self.repo, ["src.py"], {"src.py": "y\n"})
        self.assertEqual(tc.verify_transport_origin(self.record, self.repo, installation=done)["state"], "clean")
        self.git("add", "-A")
        staged = tc.verify_transport_origin(self.record, self.repo, installation=done)
        self.assertEqual((staged["state"], staged["code"]), ("staged", tc.TRANSPORT_STAGED))
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "oops")
        self.git("rm", "-q", "-r", "--cached", tc.DATA_DIR)
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "undo")
        committed = tc.verify_transport_origin(self.record, self.repo, installation=done)
        self.assertEqual(committed["state"], "committed")  # add in one commit, delete in the next
        self.assertEqual(tc.intersects(self.record, self.repo)["state"], "committed")

    def test_a_merged_side_branch_is_scanned(self):
        done = self.installed()
        self.git("checkout", "-q", "-b", "side")
        self.git("add", "-A")
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "side data")
        self.git("rm", "-q", "-r", "--cached", tc.DATA_DIR)
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "side undo")
        self.git("checkout", "-q", "main")
        self.git("-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "-m", "merge", "side")
        self.assertEqual(tc.verify_transport_origin(self.record, self.repo, installation=done)["state"], "committed")

    def test_rewritten_or_replaced_history_is_unknown(self):
        done = self.installed()
        other = git_commit_paths(self.repo, ["b.txt"], {"b.txt": "b\n"})
        self.git("replace", other, self.head)
        self.assertEqual(tc.verify_transport_origin(self.record, self.repo, installation=done)["state"], "unknown")
        self.git("replace", "-d", other)
        self.git("checkout", "-q", "--orphan", "fresh")
        self.git("rm", "-q", "-r", "--cached", ".")
        git_commit_paths(self.repo, ["c.txt"], {"c.txt": "c\n"})
        self.assertEqual(tc.verify_transport_origin(self.record, self.repo, installation=done)["state"], "unknown")


def git_commit_paths(repo: pathlib.Path, paths, files: dict) -> str:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--", *paths], check=True, env=git_env())
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "product"],
                   check=True, env=git_env())
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


class ReadObservationTest(unittest.TestCase):
    def setUp(self):
        self.root = make_tmp_dir(self)
        self.manifest = "a" * 64
        self.data = b"".join(f"line {n}\n".encode() for n in range(1, 11))
        self.name = hashlib.sha256(self.data).hexdigest()
        self.path = str(self.root / tc.DATA_DIR / self.manifest / self.name)
        os.makedirs(os.path.dirname(self.path))

    def init(self, **extra):
        return {"type": "system", "subtype": "init", "session_id": "s", "cwd": str(self.root), "tools": ["Read"],
                **extra}

    def read(self, ident: str, start: int, count: int, *, content=None, model=None, parent=None, session="s",
             error=False, truncated=False):
        lines = self.data.decode().split("\n")[:-1][start - 1:start - 1 + count]
        text = "\n".join(lines) if content is None else content
        rendered = "\n".join(f"{n:6d}→{line}" for n, line in enumerate(lines, start)) if model is None else model
        info = {"filePath": self.path, "content": text, "numLines": count, "startLine": start, "totalLines": 10}
        if truncated:
            info["truncatedByTokenCap"] = True
        return [{"type": "assistant", "session_id": session, "parent_tool_use_id": parent, "message": {"content": [
                    {"type": "tool_use", "id": ident, "name": "Read", "input": {"file_path": self.path}}]}},
                {"type": "user", "session_id": session, "parent_tool_use_id": parent, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": ident, "content": rendered, "is_error": error}]},
                 "tool_use_result": {"type": "text", "file": info}}]

    def observe(self, events):
        return tc.observe_reads(events, root=self.root, manifest=self.manifest, payloads={self.name: self.data},
                                whole=[self.name])

    def test_two_exact_slices_cover_the_whole_original(self):
        found = self.observe([self.init(), *self.read("a", 1, 6), *self.read("b", 7, 4)])
        self.assertEqual(found["reference_delivery"], "complete")
        self.assertEqual(found["model_directed"], "matched")
        self.assertEqual(found["coverage"][self.name], {"lines": 10, "covered": 10, "complete": True})
        self.assertNotIn("content", json.dumps(found["rows"]))  # digests and counts only

    def test_partial_mismatched_truncated_denied_subagent_and_foreign_reads_do_not_count(self):
        cases = {
            "partial": [*self.read("a", 1, 5)],
            "normalized text": [*self.read("a", 1, 10, content=self.data.decode().replace("\n", "\r\n")[:-2])],
            "model rendering": [*self.read("a", 1, 10, model="one\ntwo")],
            "truncated": [*self.read("a", 1, 10, truncated=True)],
            "denied": [*self.read("a", 1, 10, error=True)],
            "subagent": [*self.read("a", 1, 10, parent="task-1")],
        }
        for name, events in cases.items():
            with self.subTest(name):
                self.assertNotEqual(self.observe([self.init(), *events])["reference_delivery"], "complete")
        foreign = self.observe([self.init(), *self.read("a", 1, 10, session="other")])
        self.assertIn("an event of another session", foreign["problems"])
        self.assertEqual(self.observe([*self.read("a", 1, 10)])["reference_delivery"], "unverified")

    def test_parallel_results_in_one_event_are_ambiguous(self):
        first, second = self.read("a", 1, 10), self.read("b", 1, 10)
        merged = dict(first[1], message={"content": first[1]["message"]["content"] + second[1]["message"]["content"]})
        found = self.observe([self.init(), first[0], second[0], merged])
        self.assertEqual({row["structured"] for row in found["rows"]}, {"ambiguous"})

    def test_the_result_line_is_the_json_output(self):
        stdout = json.dumps(self.init()) + "\n" + json.dumps({"type": "result", "result": "ok"}) + "\n"
        line, events, problem = tc.parse_stream(stdout)
        self.assertEqual(json.loads(line)["result"], "ok")
        self.assertIsNone(problem)
        self.assertEqual(tc.parse_stream(json.dumps(self.init()))[2], "the stream has no result event")


class TransportLedgerTest(DesignTask, unittest.TestCase):
    """lease.py's typed per-origin history: current authority for actions, the ORIGINAL proof for its exit."""

    def setUp(self):
        self.build({"design": "People book.\n"})
        from oah.lease import Store

        self.store = Store(self.state_dir)
        lease, _judged = self.judged()
        self.lease = lease
        self.origin_record = self.record_for(lease, run="rX")

    def record_for(self, lease, *, run: str, operation: str = "inspect") -> dict:
        design = lease.design
        plan = {"read_plan": {"files": [], "manifest_sha256": "sha256:" + "c" * 64}, "view": {}}
        return tc.origin_record(transport=plan, root=self.repo, repo="sha256:" + "0" * 64, run=run,
                                task_identity=design["task_identity"], basis=design["basis"],
                                epoch={"lease_id": lease.id, "generation": lease.generation, "agent": lease.agent},
                                profile={"id": tc.CLAUDE_PROFILE, "operation": operation}, h0={"head": None},
                                policy={}, nonce="00" * 32, continuity={}, boot={}, root_identity={})

    def proof(self, origin: str):
        return mock.Mock(holds=lambda value: value == origin)

    def test_stale_expected_record_refuses_without_writing(self):
        from oah.lease import LeaseError

        start = self.store.register_transport_origin(task="t1", lease_id=self.lease.id,
                                                     generation=self.lease.generation, record=self.origin_record)
        origin = start["record_digest"]
        first = self.store.observe_transport(task="t1", origin=origin, expected=start, kind="installed", fact={})
        before = len(self.events())
        with self.assertRaises(LeaseError) as caught:
            self.store.observe_transport(task="t1", origin=origin, expected=start, kind="spawn-intent", fact={})
        self.assertEqual(caught.exception.code, tc.TRANSPORT_CONFLICT)
        self.assertEqual(len(self.events()), before)
        view = tc.transport_views(self.state_dir)[origin]
        self.assertEqual(view.latest, first)
        self.assertIsNone(view.problem)

    def test_original_exit_is_recorded_after_expiry_but_current_actions_are_not(self):
        import datetime as dt

        from oah.lease import LeaseError, Store

        start = self.store.register_transport_origin(task="t1", lease_id=self.lease.id,
                                                     generation=self.lease.generation, record=self.origin_record)
        origin = start["record_digest"]
        latest = self.store.observe_transport(task="t1", origin=origin, expected=start, kind="spawn-intent", fact={})
        later = Store(self.state_dir, now=lambda: dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30))
        with self.assertRaises(LeaseError) as caught:
            later.observe_transport(task="t1", origin=origin, expected=latest, kind="cleanup-intent", fact={})
        self.assertEqual(caught.exception.code, tc.TRANSPORT_AUTHORITY)
        original = {"origin": origin, **{name: self.origin_record[name] for name in
                                         ("run", "task_identity", "task_basis_digest", "epoch", "root_digest",
                                          "manifest")}}
        fact = {"quiescent": True, "return_code": 0}
        exit_record, durable = later.observe_transport_exit(original=original, expected=latest, kind="exit-observed",
                                                            fact=fact, live_proof=self.proof(origin))
        self.assertFalse(durable)
        again, durable = later.observe_transport_exit(original=original, expected=latest, kind="exit-observed",
                                                      fact=fact, live_proof=self.proof(origin))
        self.assertEqual((again, durable), (exit_record, True))  # exact replay appends nothing
        with self.assertRaises(LeaseError) as caught:
            later.observe_transport_exit(original=original, expected=exit_record, kind="aborted", fact=fact,
                                         live_proof=self.proof(origin))
        self.assertEqual(caught.exception.code, tc.TRANSPORT_CONFLICT)  # a competing terminal fact
        for wrong in ({**original, "run": "rY"}, {**original, "epoch": {**original["epoch"], "generation": 9}}):
            with self.assertRaises(LeaseError):
                later.observe_transport_exit(original=wrong, expected=exit_record, kind="reads", fact={},
                                             live_proof=self.proof(origin))
        with self.assertRaises(LeaseError):
            later.observe_transport_exit(original=original, expected=exit_record, kind="reads", fact={},
                                         live_proof=mock.Mock(holds=lambda value: False))
        self.assertTrue(tc.transport_views(self.state_dir)[origin].quiescent)

    def test_a_broken_chain_is_unknown_for_finish(self):
        start = self.store.register_transport_origin(task="t1", lease_id=self.lease.id,
                                                     generation=self.lease.generation, record=self.origin_record)
        self.store.observe_transport(task="t1", origin=start["record_digest"], expected=start, kind="installed",
                                     fact={})
        path = self.state_dir / "events.jsonl"
        text = path.read_text(encoding="utf-8").replace('"kind": "installed"', '"kind": "tampered"')
        path.write_text(text, encoding="utf-8")
        found = tc.completion_guard(self.state_dir, "t1", root=self.repo, repo="sha256:" + "0" * 64, current=None)
        self.assertEqual(found.code, tc.TRANSPORT_HISTORY_UNKNOWN)

    def test_transport_history_never_supplies_a_task_binding(self):
        from oah.design.binding import task_view

        before = task_view(self.state_dir, "t1").record
        start = self.store.register_transport_origin(task="t1", lease_id=self.lease.id,
                                                     generation=self.lease.generation, record=self.origin_record)
        self.store.observe_transport(task="t1", origin=start["record_digest"], expected=start, kind="installed",
                                     fact={})
        self.assertEqual(task_view(self.state_dir, "t1").record, before)


@unittest.skipUnless(NATIVE, NATIVE_ONLY)
class ReferenceCallTest(DesignTask, unittest.TestCase):
    """End to end: `oah call claude` installs, observes, accounts and cleans the exact DATA files."""

    def call(self, engine: str = "claude", **env) -> subprocess.CompletedProcess:
        return self.oah("call", engine, "--stage", "initial" if engine == "claude" else "review", "--task", "t1",
                        "--tier", "low" if engine == "claude" else "high", env=env)

    def test_a_complete_read_is_delivered_and_cleaned(self):
        self.build(BIG_PARTS)
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kinds = [event["transport"].get("kind") for event in self.events()]
        self.assertEqual(kinds[:7], [None, "installed", "spawn-intent", "running", "exit-observed", "reads",
                                     "delivery-complete"])
        self.assertEqual(kinds[-1], "verified-cleaned")
        run = self.last_run()
        saved = json.loads((run / "design.json").read_text())
        self.assertTrue(saved["receiving"]["delivered"])
        self.assertEqual(saved["receiving"]["preflight"], "local-policy-verified-remote-unobserved")
        self.assertFalse((self.repo / tc.DATA_DIR).exists())
        argv = json.loads(self.record.read_text())["argv"]
        self.assertIn("stream-json", argv)
        self.assertIn("--verbose", argv)
        logged = (run / "stdout.log").read_text().strip().splitlines()
        self.assertEqual(len(logged), 1)  # the json result only, never the transcript
        self.assertEqual(json.loads(logged[0])["type"], "result")
        status = self.oah("design", "task", "t1", "--run", run.name)
        self.assertIn("state=VERIFIED_CLEANED", status.stdout)

    def test_partial_reads_keep_edits_but_cannot_complete_the_task(self):
        self.build(BIG_PARTS)
        result = self.call(FAKE_STREAM_MODE="partial")
        self.assertEqual(result.returncode, 0, result.stderr)
        warnings = json.loads((self.last_run() / "result.json").read_text())["warnings"]
        self.assertTrue(any(item.startswith(tc.TRANSPORT_NOT_DELIVERED) for item in warnings))
        finished = self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "s",
                            "--return", str(self.returned()))
        self.assertEqual(finished.returncode, 1)
        self.assertIn(tc.TRANSPORT_NOT_DELIVERED, finished.stderr)

    def test_staged_design_data_fails_the_run_and_stays_recorded(self):
        self.build(BIG_PARTS)
        result = self.call(FAKE_STREAM_GIT_ADD="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn(tc.TRANSPORT_STAGED, result.stdout + result.stderr)
        self.assertTrue((self.repo / tc.DATA_DIR).exists())  # nothing unstaged or deleted
        subprocess.run(["git", "-C", str(self.repo), "reset", "-q"], check=True, env=git_env())
        finished = self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "s",
                            "--return", str(self.returned()))
        self.assertEqual(finished.returncode, 1)
        self.assertIn(tc.TRANSPORT_STAGED, finished.stderr)  # sticky after unstaging

    def test_a_claude_inspection_reads_by_reference_in_plan_mode(self):
        self.build(BIG_PARTS)
        result = self.oah("call", "claude", "--stage", "review", "--task", "t1", "--tier", "high",
                          "--override-stage")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = json.loads(self.record.read_text())["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        origin = self.events("design_transport_origin")[0]["transport"]["record"]
        self.assertEqual(origin["profile"]["operation"], "inspect")
        self.assertTrue(json.loads((self.last_run() / "design.json").read_text())["receiving"]["delivered"])

    def test_continuity_is_recorded_typed_before_any_data(self):
        self.build(BIG_PARTS)
        self.assertEqual(self.call().returncode, 0)
        origin = self.events("design_transport_origin")[0]["transport"]["record"]
        self.assertEqual(origin["continuity"]["status"], "unsupported")  # recorded; not a launch prerequisite
        self.assertIsNone(origin["continuity"]["host"])
        self.assertEqual(origin["boot"]["status"], "unsupported")
        self.assertIsNone(origin["boot"]["value_digest"])
        self.assertTrue(all(root["volume_uuid_digest"] is None for root in origin["continuity"]["roots"]))

    def test_a_killed_runner_leaves_a_blocked_origin_and_its_data(self):
        import signal
        import time

        self.build(BIG_PARTS)
        started = self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low", "--background",
                           env={"FAKE_SLEEP": "4"})
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            kinds = [event["transport"].get("kind") for event in self.events()]
            if "running" in kinds:
                break
            time.sleep(0.1)
        self.assertIn("running", kinds)
        run = self.last_run()
        pid = json.loads((run / "result.json").read_text())["pid"]
        os.kill(pid, signal.SIGKILL)  # before any durable exit; the child keeps its own session
        time.sleep(6)
        view = next(iter(tc.transport_views(self.state_dir).values()))
        self.assertEqual(view.state, "RUNNING")
        self.assertFalse(view.quiescent)
        self.assertTrue((self.repo / tc.DATA_DIR).exists())  # nothing is deleted on a guess
        finished = self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "s",
                            "--return", str(self.returned()))
        self.assertEqual(finished.returncode, 1)
        self.assertIn(tc.TRANSPORT_NONTERMINAL, finished.stderr)
        latest = view.latest
        cleanup = self.oah("design", "task", "t1", "--run", run.name, "--transport-cleanup", "--evidence-plan",
                           view.record["manifest"], "--expect-origin", view.origin, "--expect-record",
                           f"{latest['sequence']}:{latest['record_digest']}", "--expect-task-basis",
                           view.record["task_basis_digest"], "--settle", "aborted")
        self.assertEqual(cleanup.returncode, 1)
        self.assertIn(tc.TRANSPORT_NONTERMINAL, cleanup.stderr)  # a lost runner needs a qualified changed boot

    def test_codex_and_unqualified_claude_refuse_before_a_child(self):
        self.build(BIG_PARTS)
        refused = self.call("codex")
        self.assertEqual(refused.returncode, 1)
        self.assertIn(tc.CODEX_REFERENCE_PENDING_CODE, refused.stderr)
        self.assertFalse(self.record.exists())
        refused = self.call(FAKE_CLAUDE_VERSION="2.1.309")
        self.assertEqual(refused.returncode, 1)
        self.assertIn(tc.REFERENCE_UNAVAILABLE_CODE, refused.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.events(), [])


class CodexDeliveryTest(DesignTask, unittest.TestCase):
    """B-T12 at the shared call seam (`oah fix` corrections use the same stage and preparation): whole-inline text
    reaches the child's stdin byte for byte; a design that needs references never reaches Codex."""

    def call(self, stage: str) -> subprocess.CompletedProcess:
        return self.oah("call", "codex", "--stage", stage, "--task", "t1", "--tier", "high")

    def stdin(self) -> str:
        return json.loads(self.record.read_text(encoding="utf-8"))["stdin"]

    def test_a_correction_receives_every_required_part(self):
        self.build({"design": "ALPHA people book a lesson.\n", "request": "BRAVO the owner asked for it.\n"})
        result = self.call("correction")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stdin = self.stdin()
        self.assertIn("ALPHA people book a lesson.\n", stdin)
        self.assertIn("BRAVO the owner asked for it.\n", stdin)
        saved = json.loads((self.last_run() / "design.json").read_text(encoding="utf-8"))
        self.assertEqual([item["part"] for item in saved["included"]], ["design", "request"])
        self.assertEqual(saved["receiving"]["reference_delivery"], "not-required")
        self.assertEqual(self.events(), [])  # whole-inline: no transport origin

    def test_a_review_receives_the_accepted_text_and_the_newer_draft(self):
        self.build({"design": "ALPHA people book a lesson.\n"})
        store = self.store(self.repo)
        context = store.context("acme", "booking")
        store.save("acme", "booking", expect_revision=1, expect_digest=context.current.digest, reason="draft",
                   parts={"design": "CHARLIE people book two lessons.\n"}, today=TODAY)
        refused = self.call("correction")  # work refuses an unresolved draft
        self.assertEqual(refused.returncode, 1)
        self.assertIn("OAH_DESIGN_NEEDS_REVIEW", refused.stderr)
        self.assertFalse(self.record.exists())
        result = self.call("review")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stdin = self.stdin()
        self.assertIn("ALPHA people book a lesson.", stdin)
        self.assertIn("CHARLIE people book two lessons.", stdin)

    def test_a_header_mismatch_fails_a_design_run_as_without_a_design(self):
        self.build({"design": "ALPHA people book a lesson.\n"})
        result = self.oah("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "high",
                          env={"FAKE_CODEX_HEADER_MODEL": "gpt-other"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        saved = json.loads((self.last_run() / "result.json").read_text())
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["header_code"], "OAH_MODEL_MISMATCH")
        self.assertIn("ALPHA people book a lesson.", self.stdin())  # the human header route is unchanged

    def test_a_design_that_needs_references_never_reaches_codex(self):
        self.build(BIG_PARTS)
        for stage in ("correction", "review"):
            with self.subTest(stage):
                refused = self.call(stage)
                self.assertEqual(refused.returncode, 1)
                self.assertIn(tc.CODEX_REFERENCE_PENDING_CODE, refused.stderr)
                self.assertFalse(self.record.exists())
        self.assertEqual(self.events(), [])


@unittest.skipIf(os.name == "nt", "POSIX process signals")
class DescendantPolicyTest(DesignTask, unittest.TestCase):
    """SC4-P5 ordinary descendant policy: a design-bound run stops its redirected background descendants before
    accounting; a run without a design keeps the baseline (it does not)."""

    @staticmethod
    def alive(pid: int) -> bool:
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            time.sleep(0.05)
        return True

    def test_design_runs_stop_redirected_descendants_and_plain_runs_do_not(self):
        import signal

        self.build({"design": "People book a lesson.\n"})
        pidfile = self.work / "child.pid"
        # The launcher lives briefly after starting its server, as a real one does; a descendant that is reparented
        # before the owned-tree watcher sees it is outside any guarantee (the existing _PosixTree limit).
        result = self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low",
                          env={"FAKE_REDIRECTED_CHILD": str(pidfile), "FAKE_SLEEP": "0.5"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.alive(int(pidfile.read_text())))
        git_commit(self.repo, {".oah/tasks/t2/PACKET.md": packet_text("t2", scope=("lib",))}, "plain packet")
        started = self.oah("start", "--task", "t2", "--agent", "claude", "--packet", ".oah/tasks/t2/PACKET.md")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        pidfile.unlink()
        result = self.oah("call", "claude", "--stage", "initial", "--task", "t2", "--tier", "low",
                          env={"FAKE_REDIRECTED_CHILD": str(pidfile), "FAKE_SLEEP": "0.5"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pid = int(pidfile.read_text())
        try:
            os.kill(pid, 0)
            survived = True
        except ProcessLookupError:
            survived = False
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertTrue(survived)


class RetainedOriginTest(DesignTask, unittest.TestCase):
    """A quiescent origin whose data was not removed: chunk reads, exact cleanup and ACK_RETAINED accounting."""

    def setUp(self):
        self.build({"design": "People book.\n"}, scope="src/")
        from oah.lease import Store

        self.store = Store(self.state_dir)
        self.lease, _judged = self.judged()
        self.data = b"alpha\nbeta\n"
        self.name = hashlib.sha256(self.data).hexdigest()
        files = [{"id": self.name, "kind": tc.KIND_ORIGINAL, "roles": [], "bytes": len(self.data),
                  "sha256": "sha256:" + self.name, "verified_relative_file": self.name, "read_method": "file",
                  "chunk_limit": tc.CHUNK_LIMIT, "access": "required-access", "read_obligation": "whole",
                  "required_slices": []}]
        target = {"version": 1, "task_identity": self.lease.design["task_identity"], "run": "rR"}
        manifest_bytes = canonical({"version": 1, "target_identity": target, "files": files}) + b"\n"
        self.manifest = hashlib.sha256(manifest_bytes).hexdigest()
        plan = {"read_plan": {"files": files, "manifest_sha256": "sha256:" + self.manifest}, "view": {}}
        from oah.design.binding import repo_identity

        record = tc.origin_record(
            transport=plan, root=self.repo, repo=repo_identity(self.repo), run="rR",
            task_identity=self.lease.design["task_identity"], basis=self.lease.design["basis"],
            epoch={"lease_id": self.lease.id, "generation": self.lease.generation, "agent": self.lease.agent},
            profile={"id": tc.CLAUDE_PROFILE, "operation": "inspect"},
            h0=tc.git_head(self.repo), policy={}, nonce="00" * 32, continuity={}, boot={}, root_identity={})
        start = self.store.register_transport_origin(task="t1", lease_id=self.lease.id,
                                                     generation=self.lease.generation, record=record)
        self.origin = start["record_digest"]
        done = tc.install(self.repo, self.manifest, {self.name: self.data, self.manifest: manifest_bytes})
        latest = self.store.observe_transport(task="t1", origin=self.origin, expected=start, kind="installed",
                                              fact=done.facts())
        done.close()
        original = {"origin": self.origin, **{key: record[key] for key in
                                              ("run", "task_identity", "task_basis_digest", "epoch", "root_digest",
                                               "manifest")}}
        self.store.observe_transport_exit(original=original, expected=latest, kind="no-spawn",
                                          fact={"quiescent": True, "reason": "test", "spawn_intent": False},
                                          live_proof=mock.Mock(holds=lambda value: True))

    def expect(self) -> list[str]:
        view = tc.transport_views(self.state_dir)[self.origin]
        return ["--run", "rR", "--evidence-plan", self.manifest, "--expect-origin", self.origin, "--expect-record",
                f"{view.latest['sequence']}:{view.latest['record_digest']}", "--expect-task-basis",
                view.record["task_basis_digest"], "--settle", "aborted"]

    def test_chunks_are_exact_and_bound_to_the_plan(self):
        chunk = self.oah("design", "task", "t1", "--run", "rR", "--evidence-plan", self.manifest, "--blob", self.name,
                         "--offset", "6", "--length", "4")
        self.assertEqual(chunk.returncode, 0, chunk.stderr)
        data = json.loads(chunk.stdout)
        self.assertEqual((data["data"], data["full_bytes"], data["encoding"]), ("beta", len(self.data), "utf-8"))
        self.assertEqual(data["slice_sha256"], "sha256:" + hashlib.sha256(b"beta").hexdigest())
        wrong = self.oah("design", "task", "t1", "--run", "rOther", "--evidence-plan", self.manifest, "--blob",
                         self.name)
        self.assertEqual(wrong.returncode, 1)

    def test_unresolved_data_blocks_finish_and_exact_retention_is_accounted(self):
        returned = str(self.returned())
        finish = ["finish", "--task", "t1", "--agent", "claude", "--summary", "s", "--return", returned]
        blocked = self.oah(*finish)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn(tc.TRANSPORT_RETAINED, blocked.stderr)
        worker = self.oah("design", "task", "t1", "--transport-cleanup", "--retain", *self.expect(),
                          env={"OAH_WORKER": "1"})
        self.assertEqual(worker.returncode, 1)
        acked = self.oah("design", "task", "t1", "--transport-cleanup", "--retain", *self.expect())
        self.assertEqual(acked.returncode, 0, acked.stderr)
        self.assertIn("OAH_DESIGN_TRANSPORT_ACK_RETAINED", acked.stdout)
        self.assertTrue((self.repo / tc.DATA_DIR / self.manifest / self.name).exists())
        passed = self.oah(*finish)
        self.assertNotIn("OAH_DESIGN_TRANSPORT", passed.stderr)  # retained exact files are not out of scope
        self.assertNotIn("OAH_SCOPE_EXCEEDED", passed.stderr)

    def test_manually_missing_files_are_acknowledged_never_claimed_as_cleaned(self):
        directory = self.repo / tc.DATA_DIR / self.manifest
        os.chmod(directory, 0o755)
        (directory / self.name).unlink()  # deleted by hand, with no cleanup intent
        cleaned = self.oah("design", "task", "t1", "--transport-cleanup", *self.expect())
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertIn("OAH_DESIGN_TRANSPORT_ACK_RETAINED", cleaned.stdout)
        view = tc.transport_views(self.state_dir)[self.origin]
        self.assertEqual(view.state, "ACK_RETAINED")
        leaves = view.resolution["fact"]["leaves"]
        self.assertEqual(leaves[self.name]["state"], "absent-unattributed")
        self.assertEqual(leaves[self.manifest]["state"], "removed-under-intent")

    def test_exact_cleanup_removes_only_the_created_files(self):
        stale = self.oah("design", "task", "t1", "--transport-cleanup", *self.expect()[:-6],
                         "--expect-record", "1:sha256:" + "0" * 64, *self.expect()[-4:])
        self.assertEqual(stale.returncode, 1)
        self.assertIn(tc.TRANSPORT_CONFLICT, stale.stderr)
        cleaned = self.oah("design", "task", "t1", "--transport-cleanup", *self.expect())
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertFalse((self.repo / tc.DATA_DIR).exists())
        self.assertEqual(tc.transport_views(self.state_dir)[self.origin].state, "VERIFIED_CLEANED")

    def test_a_run_without_a_recorded_exit_blocks_finish(self):
        from oah.lease import Store

        lease, _judged = self.judged()
        record = TransportLedgerTest.record_for(self, lease, run="rN", operation="work")
        start = Store(self.state_dir).register_transport_origin(task="t1", lease_id=lease.id,
                                                                generation=lease.generation, record=record)
        Store(self.state_dir).observe_transport(task="t1", origin=start["record_digest"], expected=start,
                                                kind="spawn-intent", fact={})
        found = tc.completion_guard(self.state_dir, "t1", root=self.repo, repo=None, current=None)
        self.assertEqual(found.code, tc.TRANSPORT_NONTERMINAL)


@unittest.skipIf(os.name == "nt", "guarded retirement is a POSIX route")
class SnapshotRetirementTest(unittest.TestCase):
    """SC4-P4-R/P4-R-I: exclusive disposal and the complete paged inventory."""

    def setUp(self):
        self.state = make_tmp_dir(self)
        self.allocation = {"version": 1, "run": "rS"}

    def container(self, files: int, *, name_length: int = 8) -> pathlib.Path:
        parent = make_tmp_dir(self) / "c"
        (parent / "tree").mkdir(parents=True)
        for n in range(files):
            (parent / "tree" / f"{n:0{name_length}d}").write_bytes(b"x")
        return parent

    def plan(self, parent: pathlib.Path):
        from oah.review import snapshot

        captured = snapshot.capture_retirement_plan(self.allocation, parent, self.state,
                                                    output_slot={"path": ["last-message.md"]})
        final = snapshot.finalize_retirement_plan(captured, {"presence": "absent"})
        return captured, final

    @staticmethod
    def pages(captured) -> list[bytes]:
        found, link = [], captured.descriptor["head"]
        while link is not None:
            data = (captured.pages_dir / f"{link['sha256']}.json").read_bytes()
            found.append(data)
            link = json.loads(data)["previous"]
        return found[::-1]

    def test_entry_and_byte_page_boundaries(self):
        from oah.review import snapshot

        # The entry bound alone (the byte bound raised): 8192 whole entries fill one page, one more opens another.
        with mock.patch.object(snapshot, "PAGE_BYTES", 64 * 1024 * 1024):
            captured, _final = self.plan(self.container(8192 - 1))  # with the tree folder: exactly 8192 entries
            self.assertEqual((captured.descriptor["page_count"], captured.descriptor["total_entries"]), (1, 8192))
            parent = self.container(8192)
            captured, final = self.plan(parent)
            self.assertEqual((captured.descriptor["page_count"], captured.descriptor["total_entries"]), (2, 8193))
            self.assertEqual(len(snapshot.validate_retirement_plan(final, parent, captured.pages_dir).entries), 8193)
        # The real 1 MiB byte bound: every page fits, and each is as full as the next whole entry allows.
        parent = self.container(8192)
        captured, final = self.plan(parent)
        pages = self.pages(captured)
        self.assertGreater(len(pages), 1)
        for data, following in zip(pages, pages[1:]):
            page = json.loads(data)
            self.assertLessEqual(len(data), snapshot.PAGE_BYTES)
            first = snapshot._canonical(json.loads(following)["entries"][0])
            self.assertTrue(len(page["entries"]) == snapshot.PAGE_ENTRIES
                            or len(data) + 1 + len(first) > snapshot.PAGE_BYTES)
        self.assertLessEqual(len(pages[-1]), snapshot.PAGE_BYTES)
        self.assertEqual(len(snapshot.validate_retirement_plan(final, parent, captured.pages_dir).entries), 8193)

    def test_exact_page_byte_boundary(self):
        from oah.review import snapshot

        def entry(name: str) -> dict:
            return {"path": [name], "type": "file", "mode": 420, "size": 1, "sha256": None, "object": None}

        allocation = {"version": 1, "run": "rB"}
        empty = len(snapshot._canonical({"version": 1, "allocation": allocation, "ordinal": 0, "previous": None,
                                         "entries": []}))
        one = len(snapshot._canonical(entry("a")))
        # Two entries whose page is EXACTLY PAGE_BYTES: name lengths chosen so the canonical bytes add up.
        target = snapshot.PAGE_BYTES - empty - 1  # one comma between the two entries
        first = "a" * (target // 2 - one + 1)
        second = "b" * (target - len(snapshot._canonical(entry(first))) - one + 1)
        exact = [entry(first), entry(second)]
        self.assertEqual(empty + 1 + sum(len(snapshot._canonical(item)) for item in exact), snapshot.PAGE_BYTES)
        pages = make_tmp_dir(self)
        fields = snapshot.write_pages(allocation, exact, pages)
        self.assertEqual((fields["page_count"], fields["total_page_bytes"]), (1, snapshot.PAGE_BYTES))
        over = [entry(first), entry(second + "c")]  # one byte over: the second entry moves to a new page
        fields = snapshot.write_pages(allocation, over, make_tmp_dir(self))
        self.assertEqual((fields["page_count"], fields["total_entries"]), (2, 2))

    def test_validation_rejects_tampered_missing_or_changed_inventory(self):
        from oah.review import snapshot

        parent = self.container(20)
        captured, final = self.plan(parent)
        self.assertEqual(len(snapshot.validate_retirement_plan(final, parent, captured.pages_dir).entries), 21)
        (parent / "tree" / "extra").write_bytes(b"y")
        with self.assertRaises(snapshot.Retained):
            snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
        (parent / "tree" / "extra").unlink()
        (parent / "tree" / "00000001").write_bytes(b"changed")
        with self.assertRaises(snapshot.Retained):
            snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
        (parent / "tree" / "00000001").write_bytes(b"x")
        page = next(captured.pages_dir.iterdir())
        os.chmod(page, 0o600)
        page.write_bytes(page.read_bytes().replace(b'"x"', b'"y"', 1) + b" ")
        with self.assertRaises(snapshot.Retained):
            snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
        page.unlink()
        with self.assertRaises(snapshot.Retained):
            snapshot.validate_retirement_plan(final, parent, captured.pages_dir)

    def test_guarded_removal_records_intent_first_and_stops_at_a_replaced_entry(self):
        from oah.review import snapshot

        parent = self.container(5)
        captured, final = self.plan(parent)
        validated = snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
        steps = []
        self.assertTrue(snapshot.remove(parent, expected_plan=validated,
                                        progress=lambda entry, state: steps.append(("/".join(entry["path"]), state))))
        self.assertFalse(parent.exists())
        self.assertEqual(steps[0][1], "intent")
        self.assertEqual(steps[1][1], "absent")
        self.assertEqual(steps[-2:], [("", "intent"), ("", "absent")])
        parent = self.container(5)
        captured, final = self.plan(parent)
        validated = snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
        victim = parent / "tree" / "00000002"
        victim.unlink()
        victim.write_bytes(b"x")  # same bytes, a different file object
        self.assertFalse(snapshot.remove(parent, expected_plan=validated, progress=lambda entry, state: None))
        self.assertTrue(victim.exists())  # never an rmtree fallback

    def test_the_disposal_callback_is_exclusive(self):
        from oah.review import snapshot

        source = make_git_repo(self)
        head = git_commit(source, {"README.md": "x\n"}, "one")
        for outcome, gone in ((("retained", "kept"), False), (RuntimeError("boom"), False)):
            seen = []

            def dispose(parent, created, outcome=outcome):
                seen.append(parent)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            warnings: list[str] = []
            with snapshot.make(source, head, base=head, warnings=warnings, dispose=dispose):
                pass
            self.assertEqual(seen[0].exists(), not gone)
            self.assertTrue(any(item.startswith("OAH_REVIEW_SNAPSHOT_RETAINED") for item in warnings))
            snapshot.remove(seen[0])

    def test_a_whole_inline_review_proves_no_origin_before_legacy_disposal(self):
        from oah.review import runner, snapshot

        source = make_git_repo(self)
        head = git_commit(source, {"README.md": "x\n"}, "one")
        outcomes: list = []
        with snapshot.make(source, head, base=head, dispose=runner._snapshot_disposal(self.state, "rS", outcomes)) \
                as snap:
            parent = snap.parent
        self.assertEqual(outcomes, [("legacy-disposed", None)])
        self.assertFalse(parent.exists())
        events = self.state / "events.jsonl"
        record = {"run": "rS"}
        origin = tc.origin_digest(record)
        events.write_text(json.dumps({"event": "design_transport_origin", "task": "t1", "design_schema": 2,
                                      "transport": {"version": 1, "origin": origin, "record": record}}) + "\n")
        outcomes.clear()
        with snapshot.make(source, head, base=head, dispose=runner._snapshot_disposal(self.state, "rS", outcomes)) \
                as snap:
            parent = snap.parent
        self.assertEqual(outcomes[0][0], "retained")
        self.assertTrue(parent.exists())
        snapshot.remove(parent)


if __name__ == "__main__":
    unittest.main()
