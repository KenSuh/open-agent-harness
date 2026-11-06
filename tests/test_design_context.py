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
import struct
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
        items.append({"id": "sha256:" + digest, "kind": "selected-file", "path_or_field": name, "encoding": "base64",
                      "bytes": len(data), "sha256": digest, "data": base64.b64encode(data).decode("ascii")})
    def record_digest(value):
        return "sha256:" + hashlib.sha256((json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1)
                                          + "\n").encode()).hexdigest()

    spec = {"provider": provider, "locator": "https://github.com/acme/lib", "track": "commit", "tag_prefix": "",
            "channel": "stable", "ref": "HEAD", "paths": [name for name, _data in blobs], "manifest_path": None,
            "release_order": "", "selection_policy": None, "verification_policy": "source-verification-v1"}
    spec_digest = "sha256:" + hashlib.sha256(canonical(spec)).hexdigest()
    source = {"id": "lib", **spec}
    manifest = {"kind": "oah-source-snapshot", "version": 1, "spec": spec, "spec_digest": spec_digest,
                "verified": True, "files": files, "license": {"files": [], "notices": []}, "document": None,
                "observed": {"commit": "c" * 40, "ref": "HEAD", "release": None}, "notices": [], "problems": [],
                "content_key": "sha256:" + hashlib.sha256(canonical(files)).hexdigest()}
    snapshot = record_digest(manifest)
    times = {key: "2026-11-01T12:00:00+00:00" for key in
             ("attempted_at", "observed_at", "confirmed_at", "verified_at")}
    observation = {"version": 1, "kind": "oah-source-observation", "outcome": "established", "head_generation": 1,
                   "source": {"id": "lib", "registration": "a" * 32, "spec_digest": spec_digest},
                   "snapshot": snapshot, "previous": None, "selection_observation": None,
                   "verification_policy": spec["verification_policy"], **times}
    observed = record_digest(observation)
    evidence = {"version": 1, "kind": "oah-portable-evidence", "source": source, "spec_digest": spec_digest,
                "snapshot": {"digest": snapshot, "manifest": manifest}, "blobs": items,
                "provenance": {"claim": "local-observation", "registration": "a" * 32,
                               "observation": observation, "observation_digest": observed,
                               "release_observation": None, "release_observation_digest": None}}
    material = {"id": "lib", "source": source, "snapshot": snapshot, "spec_digest": spec_digest,
                "portable_evidence_id": "sha256:" + hashlib.sha256(canonical(evidence)).hexdigest(),
                "checked": {"historical": "verified", "observation": observed, **times},
                "reviewed": {"files": files, "commit": "c" * 40, "ref": "HEAD"}, "release": None}
    record = {"version": 2, "lang": "en", "narrative": narrative, "decision_inputs": {"d1": {"need": "storage"}},
              "decisions": [{"id": "d1", "need": "storage", "alternatives": ["a", "b"]}], "materials": [material],
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

    def test_rebound_wire_identities_do_not_validate_unknown_or_boolean_wire_schemas(self):
        def digest(record):
            return "sha256:" + hashlib.sha256((json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1)
                                               + "\n").encode()).hexdigest()

        text = materials_part(BIG[:1])
        body = text.split("\n")[3]
        for mutation in ("manifest-version", "observation-version", "manifest-field", "source-track"):
            with self.subTest(mutation=mutation):
                record = json.loads(body)
                envelope = record["portable_evidence"][0]
                manifest = envelope["snapshot"]["manifest"]
                observation = envelope["provenance"]["observation"]
                if mutation == "manifest-version":
                    manifest["version"] = True
                elif mutation == "observation-version":
                    observation["version"] = True
                elif mutation == "manifest-field":
                    manifest["not-a-wire-field"] = "unlisted"
                else:
                    envelope["source"]["track"] = "unknown-track"
                    manifest["spec"]["track"] = "unknown-track"
                spec = {key: value for key, value in envelope["source"].items() if key != "id"}
                spec_digest = "sha256:" + hashlib.sha256(canonical(spec)).hexdigest()
                envelope["spec_digest"] = manifest["spec_digest"] = spec_digest
                observation["source"]["spec_digest"] = spec_digest
                envelope["snapshot"]["digest"] = observation["snapshot"] = digest(manifest)
                envelope["provenance"]["observation_digest"] = digest(observation)
                material = record["materials"][0]
                material.update(source=envelope["source"], snapshot=envelope["snapshot"]["digest"],
                                spec_digest=spec_digest)
                material["checked"]["observation"] = envelope["provenance"]["observation_digest"]
                material["portable_evidence_id"] = "sha256:" + hashlib.sha256(canonical(envelope)).hexdigest()
                part, problem = tc.materials_part(text.replace(body, canonical(record).decode()))
                self.assertTrue(part is None, mutation)
                self.assertIsNotNone(problem)

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
        part, problem = tc.materials_part(materials_part(BIG[:1], provider="local"))
        self.assertIsNone(problem)
        self.assertEqual([item.projectable for item in part.originals], [False])
        self.assertIn("stays inline", part.originals[0].reason)
        part, problem = tc.materials_part(materials_part(BIG[:1], member=False))
        self.assertIsNone(part)
        self.assertIn("unlisted", problem)


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

    def build(self, parts: dict[str, str], *, claude_version: str = "2.1.318", scope: str = "src/", allow_refusal: bool = False):
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
        config = ("[models]\nfetch_scores = false\n"
                  f"[engines.claude]\ncommand = {toml([sys.executable, str(fake)])}\n"
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
        if not allow_refusal:
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


class PrivacyScreenTest(DesignTask, unittest.TestCase):
    synthetic = "AK" + "IA" + "QWERTYUIOPASDFGH"
    preparation: subprocess.CompletedProcess[str] | None = None

    def build(self, parts: dict[str, str], **kwargs):
        self.preparation = super().build(parts, allow_refusal=True, **kwargs)
        return self.preparation

    def assert_refused_before_child(self):
        result = self.preparation
        if result is None:
            self.fail("the privacy fixture has not run preparation")
        if result.returncode == 0:
            result = self.oah("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "high")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(tc.REFERENCE_UNAVAILABLE_CODE, result.stdout + result.stderr)
        self.assertNotIn(self.synthetic, result.stdout + result.stderr)
        self.assertFalse(self.record.exists())
        runs = self.state_dir / "runs" / "t1"
        self.assertFalse(any(runs.rglob("prompt.md")) if runs.exists() else False)

    def test_utf16_original_is_refused_before_outgoing_artifacts(self):
        raw = ("Public notice " + self.synthetic + "\n").encode("utf-16-le")
        self.assertIn("\0", raw.decode("utf-8", errors="strict"))
        self.build({"design": "People book.\n", "materials": materials_part([("LICENSE", raw)])})
        self.assert_refused_before_child()

    def test_invalid_utf8_original_is_refused(self):
        self.build({"design": "People book.\n", "materials": materials_part([("LICENSE", b"\xff\xfe\x00\x01binary")])})
        self.assert_refused_before_child()

    def test_nul_and_binary_control_originals_are_refused(self):
        for raw in (b"public\x00notice", b"public\x01notice"):
            with self.subTest(raw=raw):
                self.build({"design": "People book.\n", "materials": materials_part([("LICENSE", raw)])})
                self.assert_refused_before_child()

    def test_mixed_reference_keeps_no_unscreenable_inline_original(self):
        public, _ = tc.materials_part(materials_part([("LICENSE", BIG[0][1])]))
        local, _ = tc.materials_part(materials_part([("README", b"\xff\xfe\x00binary")], provider="local"))
        if public is None or local is None:
            self.fail("fixture material must be recognized")
        record = public.record
        record["materials"].extend(local.record["materials"])
        record["materials"][-1]["id"] = "local-entry"
        record["portable_evidence"].extend(local.record["portable_evidence"])
        part = "Public selection.\n\n```oah-materials\n" + canonical(record).decode() + "\n```\n"
        self.build({"design": "People book.\n", "materials": part})
        preparation = self.preparation
        if preparation is None:
            self.fail("the mixed privacy fixture has not run preparation")
        if preparation.returncode != 0:
            self.assert_refused_before_child()
        else:
            with self.assertRaises(DesignError):
                tc.prepare_transport(self.judged()[1], task="t1", run="1" * 16, root=self.repo,
                                     engine="claude", operation="work")

    def test_utf8_key_original_and_material_semantic_key_are_refused(self):
        for part in (materials_part([("LICENSE", self.synthetic.encode())]),
                     materials_part([("LICENSE", b"Public notice.\n")], narrative=self.synthetic)):
            with self.subTest(part=part[:40]):
                self.build({"design": "People book.\n", "materials": part})
                self.assert_refused_before_child()

    def test_accepted_plain_design_and_request_are_screened(self):
        for name in ("design", "request"):
            with self.subTest(part=name):
                self.build({"design": "People book.\n", name: "Configuration " + self.synthetic + "\n"})
                self.assert_refused_before_child()

    def test_newer_plain_draft_is_screened(self):
        self.build({"design": "People book.\n"})
        store = self.store(self.repo)
        current = store.context("acme", "booking").current
        saved = store.save("acme", "booking", expect_revision=current.revision, expect_digest=current.digest,
                           reason="draft", parts={"request": self.synthetic + "\n"}, today=TODAY)
        if saved.digest is None or saved.revision is None:
            self.fail("the draft fixture was not saved")
        carried = self.oah("design", "carry", "t1", "--agent", "claude", "--revision", str(saved.revision),
                           "--digest", saved.digest, "--reason", "Only a fixture example changes")
        self.assertEqual(carried.returncode, 0, carried.stdout + carried.stderr)
        self.assert_refused_before_child()

    def test_public_utf8_and_safe_plain_text_keep_complete_encoding(self):
        raw = "Public permission notice.\r\n공개 원문.\t\n".encode()
        self.build({"design": "People book.\n", "materials": materials_part([("LICENSE", raw)])})
        selected = tc.select(self.judged()[1], task="t1", engine="codex")
        self.assertTrue(selected.delivered)
        if selected.text is None:
            self.fail("the complete inline block is missing")
        self.assertIn(base64.b64encode(raw).decode(), selected.text)
        result = self.oah("call", "codex", "--stage", "correction", "--task", "t1", "--tier", "high")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        received = json.loads(self.record.read_text())["stdin"]
        self.assertIn(base64.b64encode(raw).decode(), received)
        self.assertIn("People book.", received)


class TransportPlanTest(DesignTask, unittest.TestCase):
    def test_inline_originals_and_semantic_strings_are_screened_before_delivery(self):
        sensitive = ("s" + "k-" + "z" * 24).encode()
        self.build({"design": "People book.\n"})
        store = self.store(self.repo)
        current = store.context("acme", "booking").current
        saved = store.save("acme", "booking", expect_revision=current.revision, expect_digest=current.digest,
                           reason="new original", parts={"materials": materials_part([("LICENSE", sensitive)])},
                           today=TODAY)
        store.accept("acme", "booking", revision=saved.revision, digest=saved.digest, reason="ok", today=TODAY)
        _lease, judged = self.judged()
        with self.assertRaises(DesignError) as caught:
            tc.select(judged, task="t1", engine="codex")
        self.assertIn("privacy rule key", str(caught.exception))
        self.assertNotIn(sensitive.decode(), str(caught.exception))

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
class CodexPolicyTest(unittest.TestCase):
    class Preferences:
        def __init__(self, value=None, *, forced=False, sync=True, stale=False):
            self.value, self.forced, self.synced, self.stale = value, forced, sync, stale
            self.calls = 0

        def synchronize(self):
            return self.synced

        def is_forced(self, key):
            self.calls += 1
            return not self.forced if self.stale and self.calls == 2 else self.forced

        def copy_string(self, key):
            if not self.forced:
                raise AssertionError("an observed unforced preference must not be adopted")
            return self.value

    def collect(self, *, user=b'', preferences=None, argv=(), files=None):
        sources = {"/users/fixture/.codex/config.toml": user, **(files or {})}

        def read(path):
            raw = sources.get(str(path))
            return ("present", raw, None) if raw is not None else ("absent", None, None)

        def directory(path):
            return tc._source("managed-directory", path, "absent", None, None), []

        with mock.patch.object(tc, "_read_bracketed", side_effect=read), \
                mock.patch.object(tc, "_policy_directory", side_effect=directory):
            return tc.collect_process_policy(pathlib.Path('/workspace'), {'HOME': '/users/fixture'},
                        operation='work', data_paths=['/workspace/.oah-design-data/aa/bb'], engine='codex',
                        argv=['codex', *argv], cli_version='0.166.0', platform='darwin',
                        native_preferences=preferences or self.Preferences())

    def test_observed_unforced_is_local_evidence_and_reference_gate_stays_closed(self):
        found = self.collect(user=b'model = "gpt-6.1-sol"\nsandbox_mode = "workspace-write"\n')
        self.assertEqual(found['local_policy'], 'verified')
        self.assertTrue(found['candidate_local_eligible'])
        self.assertFalse(found['launch_eligible'])
        self.assertEqual(found['reference_profile'], tc.CODEX_REFERENCE_PENDING_CODE)
        self.assertEqual([x['presence'] for x in found['sources'] if x['kind'] == 'forced-preference'],
                         ['observed-unforced', 'observed-unforced'])

    def test_forced_deny_error_stale_and_selectors_drive_actual_reader(self):
        deny = base64.b64encode(b'deny_read = ["**"]\n').decode()
        forced = self.collect(preferences=self.Preferences(deny, forced=True),
                              argv=['-c', 'sandbox_mode="workspace-write"'])
        self.assertEqual(forced['local_policy'], 'denied')
        for preferences in (self.Preferences(sync=False), self.Preferences('not base64!', forced=True),
                            self.Preferences(deny, forced=True, stale=True)):
            with self.subTest(preferences=preferences):
                self.assertEqual(self.collect(preferences=preferences)['local_policy'], 'unknown')
        self.assertEqual(self.collect(argv=['--profile', 'missing'])['local_policy'], 'unknown')
        profile = self.collect(argv=['--profile', 'research'], files={
            '/users/fixture/.codex/research.config.toml': b'model = "gpt-6.1-sol"\n'})
        self.assertEqual(profile['local_policy'], 'verified')
        self.assertEqual(self.collect(argv=['--ignore-user-config'])['local_policy'], 'unknown')
        self.assertEqual(self.collect(user=b'hooks = { pre_tool_use = ["opaque"] }\n')['local_policy'], 'unknown')

    def test_project_trust_selects_only_applicable_local_configuration(self):
        files = {'/workspace/.codex/config.toml': b'deny_read = ["**"]\n'}
        ignored = self.collect(files=files)
        self.assertEqual(ignored['local_policy'], 'verified')
        trusted = b'[projects."/workspace"]\ntrust_level = "trusted"\n'
        found = self.collect(user=trusted, files=files)
        self.assertEqual(found['local_policy'], 'denied')
        self.assertEqual(next(row['trust_level'] for row in found['sources'] if row['kind'] == 'project-trust'),
                         'trusted')
        self.assertEqual(self.collect(user=trusted, files={'/workspace/.codex/config.toml':
                         b'model = "gpt-6.1-sol"\n'})['local_policy'], 'verified')
        self.assertEqual(self.collect(user=b'[projects."/workspace"]\ntrust_level = "maybe"\n',
                                      files=files)['local_policy'], 'unknown')

    def test_malformed_legacy_profile_table_is_unknown_without_throwing(self):
        for value in (b'profiles = 3\n', b'profiles = ["research"]\n'):
            with self.subTest(value=value):
                found = self.collect(user=value, argv=['--profile', 'research'], files={
                    '/users/fixture/.codex/research.config.toml': b'model = "gpt-6.1-sol"\n'})
                self.assertEqual(found['local_policy'], 'unknown')
                self.assertFalse(found['launch_eligible'])


class WindowsNativeRosterTest(unittest.TestCase):
    class Native:
        def __init__(self):
            self.nodes = {1: {'parent': None, 'name': 'root', 'type': 'directory', 'data': b'', 'mode': 0o755}}
            self.nodes[2] = {'parent': 1, 'name': 'work', 'type': 'directory', 'data': b'', 'mode': 0o755}
            self.handles, self.counter = {}, 100
            self.next_inode = 2
            self.root = self._handle(1)

        def _handle(self, inode):
            self.counter += 1
            self.handles[self.counter] = inode
            return self.counter

        def open_anchor(self, path, *, writable=False):
            handle = self._handle(1)
            anchors = [handle]
            for name in pathlib.PureWindowsPath(path).parts[1:]:
                handle, _ = self.open_child(handle, name, directory=True)
                anchors.append(handle)
            return handle, anchors

        def close(self, handle):
            inode = self.handles.pop(handle)
            if inode in self.nodes and self.nodes[inode].get('deleted'):
                del self.nodes[inode]

        def names(self, handle, *, limit=tc.ENTRY_LIMIT + 1):
            return sorted(item['name'] for item in self.nodes.values() if item['parent'] == self.handles[handle])

        def iter_names(self, handle):
            yield from self.names(handle)

        def open_child(self, parent, name, *, directory, create=False, writable=False, delete=False, inspect=False,
                       enumerated=False):
            parent_id = self.handles[parent]
            matches = [key for key, item in self.nodes.items() if item['parent'] == parent_id and item['name'] == name]
            if matches and create:
                raise FileExistsError(name)
            if not matches:
                if not create:
                    raise FileNotFoundError(name)
                self.next_inode += 1
                inode = self.next_inode
                self.nodes[inode] = {'parent': parent_id, 'name': name, 'type': 'directory' if directory else 'file',
                                     'data': b'', 'mode': 0o755 if directory else 0o644}
            else:
                inode = matches[0]
            if directory is not None and self.nodes[inode]['type'] != ('directory' if directory else 'file'):
                raise tc.InstallRefused('native fixture wrong object type')
            return self._handle(inode), create

        def info(self, handle):
            inode = self.handles[handle]
            item = self.nodes[inode]
            if item.get('reparse') or item.get('nlink', 1) != 1:
                raise tc.InstallRefused('native fixture reparse or hardlink')
            return {'dev': 10, 'ino': inode, 'uid': 'sha256:' + '1' * 64, 'mode': item['mode'],
                    'size': len(item['data']), 'nlink': 1, 'type': item['type']}

        def read(self, handle, bound):
            raw = self.nodes[self.handles[handle]]['data']
            if len(raw) > bound:
                raise tc.InstallRefused('native fixture over bound')
            return raw

        def write(self, handle, data):
            self.nodes[self.handles[handle]]['data'] = data

        def prefix(self, handle, limit):
            return self.nodes[self.handles[handle]]['data'][:limit]

        def digest(self, handle, size):
            return hashlib.sha256(self.read(handle, size)).hexdigest()

        def seal(self, handle):
            item = self.nodes[self.handles[handle]]
            item['mode'] = 0o555 if item['type'] == 'directory' else 0o444

        def flush(self, handle):
            pass

        def delete(self, handle):
            if self.names(handle):
                raise tc.InstallRefused('native fixture nonempty directory')
            self.nodes[self.handles[handle]]['deleted'] = True

    def test_create_only_native_roster_checked_bytes_guarded_cleanup_and_reopen(self):
        native = self.Native()
        manifest, first, second = 'a' * 64, 'b' * 64, 'c' * 64
        done = tc._install_windows(pathlib.PureWindowsPath(r'C:\work'), manifest,
                                   {first: b'first\n', second: b'second\n'}, native=native)
        self.assertIsNone(tc.check_leaves(done))
        installed = done.facts()
        done.close()
        reopened, states = tc._reopen_windows(pathlib.PureWindowsPath(r'C:\work'), manifest,
                                               installed, native=native)
        self.assertEqual(set(states.values()), {'present-unchanged'})
        progress = []
        def before(name):
            if name == second:
                raise tc.InstallRefused('controlled current authority change')
        with self.assertRaisesRegex(tc.InstallRefused, 'current authority'):
            tc.remove_leaves(reopened, [first, second], before=before, after=lambda *row: progress.append(row))
        self.assertEqual(progress, [(first, 'absent', 'file')])
        self.assertIn(second, native.names(reopened.m_fd))
        tc.remove_leaves(reopened, [second], before=lambda name: None, after=lambda *row: progress.append(row))
        tc.remove_directories(reopened, before=lambda name: None, after=lambda *row: progress.append(row))
        self.assertEqual(native.names(reopened.root_fd), [])
        reopened.close()

    def test_reparse_hardlink_replacement_and_ambiguous_paths_refuse(self):
        for bad in (r'C:relative', r'\\server\share\x', r'C:\work\CON', r'C:\work\file:stream',
                    r'C:\work\x.', r'C:\work\..\other'):
            with self.subTest(path=bad), self.assertRaises(tc.PolicyReadError):
                tc._safe_windows_path(bad)
        for defect in ('reparse', 'nlink', 'data'):
            with self.subTest(defect=defect):
                native = self.Native()
                done = tc._install_windows(pathlib.PureWindowsPath(r'C:\work'), 'a' * 64,
                                           {'b' * 64: b'original\n'}, native=native)
                leaf = native.nodes[native.handles[done.leaf_handles['b' * 64]]]
                leaf[defect] = 2 if defect == 'nlink' else b'changed\n' if defect == 'data' else True
                self.assertIsNotNone(tc.check_leaves(done))
                with self.assertRaises(tc.InstallRefused):
                    tc.remove_leaves(done, ['b' * 64], before=lambda name: None, after=lambda *row: None)
                done.close()

    def test_native_cleanup_requires_parent_durability_before_absence_progress(self):
        native = self.Native()
        name = 'b' * 64
        done = tc._install_windows(pathlib.PureWindowsPath(r'C:\work'), 'a' * 64,
                                   {name: b'original\n'}, native=native)
        self.addCleanup(done.close)
        progress = []
        with mock.patch.object(native, 'flush', side_effect=tc.InstallRefused('controlled directory flush failed')):
            with self.assertRaisesRegex(tc.InstallRefused, 'directory flush'):
                tc.remove_leaves(done, [name], before=lambda _: None, after=lambda *row: progress.append(row))
        self.assertEqual(progress, [])
        self.assertNotIn(name, native.names(done.m_fd))

    def test_native_read_manifest_and_package_pin_use_owned_handles(self):
        native = self.Native()
        data, run, task = b'public original\n', 'rNative', 'native-task'
        blob = hashlib.sha256(data).hexdigest()
        raw = canonical({'target_identity': {'run': run, 'task_identity': {'task_id': task}},
                         'files': [{'id': blob, 'bytes': len(data), 'sha256': 'sha256:' + blob}]})
        manifest = hashlib.sha256(raw).hexdigest()
        done = tc._install_windows(pathlib.PureWindowsPath(r'C:\work'), manifest,
                                   {blob: data, manifest: raw}, native=native)
        self.assertEqual(tc._windows_read_blob(pathlib.PureWindowsPath(r'C:\work'), task=task, run=run,
                         manifest=manifest, blob=blob, native=native), data)
        with self.assertRaises(DesignError):
            tc._windows_read_blob(pathlib.PureWindowsPath(r'C:\work'), task='other', run=run,
                                 manifest=manifest, blob=blob, native=native)
        root = done.root_fd
        image, _ = native.open_child(root, 'codex.exe', directory=False, create=True, writable=True)
        header = bytearray(512)
        header[:2] = b'MZ'
        struct.pack_into('<I', header, 60, 128)
        header[128:132] = b'PE\0\0'
        struct.pack_into('<HHIIIHH', header, 132, 0x8664, 2, 0, 0, 0, 240, 2)
        struct.pack_into('<H', header, 152, 0x20b)
        struct.pack_into('<H', header, 220, 3)
        native.write(image, bytes(header))
        native.close(image)
        candidate = tc.windows_package_candidate('codex', 'inspect', executable=r'C:\work\codex.exe',
                         cli_version='0.166.0', tools=[], native=native)
        self.assertEqual(candidate['candidate_status'], 'verified')
        self.assertEqual(candidate['status'], 'unsupported')
        self.assertEqual(candidate['machine'], 'x86_64')
        self.assertEqual(candidate['executable_sha256'], 'sha256:' + hashlib.sha256(header).hexdigest())
        bad = tc.windows_package_candidate('codex', 'inspect', executable=r'C:\work\codex.exe',
                         cli_version='0.166.1', tools=[], native=native)
        self.assertEqual(bad['candidate_status'], 'unknown')
        done.close()

    def test_native_snapshot_census_output_and_guarded_removal_share_exact_identity(self):
        from oah.review import snapshot

        native = self.Native()
        parent = pathlib.PureWindowsPath(r'C:\work')
        root, anchors = native.open_anchor(parent)
        leaf, _ = native.open_child(root, 'product.txt', directory=False, create=True, writable=True)
        native.write(leaf, b'product\n')
        native.close(leaf)
        entries = list(snapshot._windows_census(parent, native=native))
        pages = make_tmp_dir(self)
        allocation = {'run': 'rNative'}
        fields = snapshot.write_pages(allocation, entries, pages)
        descriptor = {'version': 1, 'allocation': allocation, **fields, 'output_slot': {'path': ['last-message.md']},
                      'container_object': snapshot._native_container(native.info(root))}
        plan = snapshot.RetirementPlan(descriptor, hashlib.sha256(snapshot._canonical(descriptor)).hexdigest(), pages)
        output, _ = native.open_child(root, 'last-message.md', directory=False, create=True, writable=True)
        native.write(output, b'final report\n')
        native.close(output)
        producer = {'run': 'rNative', 'checkpoint': 'sha256:' + '3' * 64}
        final = snapshot.finalize_retirement_plan(plan, snapshot._windows_output_slot(parent,
                    ['last-message.md'], producer=producer, native=native))
        for held in reversed(anchors):
            native.close(held)
        with mock.patch.object(snapshot, '_container_object', return_value=descriptor['container_object']), \
                mock.patch.object(snapshot, '_census', side_effect=lambda p: snapshot._windows_census(parent, native=native)), \
                mock.patch.object(snapshot, 'capture_output_slot', side_effect=lambda p, c, producer:
                                  snapshot._windows_output_slot(parent, c, producer=producer, native=native)):
            validated = snapshot.validate_retirement_plan(final, parent, pages)
        progress = []
        self.assertTrue(snapshot._windows_guarded_remove(parent, validated,
                         lambda item, state: progress.append((item['path'], state)), native=native))
        self.assertEqual(progress[-1], ([], 'absent'))
        self.assertNotIn(2, native.nodes)

    def test_native_file_information_and_short_write_api_buffers_are_checked(self):
        import ctypes as ct
        import types

        native = tc._WindowsIO.__new__(tc._WindowsIO)
        native.ct = ct
        defect = {'links': 1, 'reparse': 0}
        def information(handle, kind, target, size):
            raw = bytearray(size)
            if kind == 18:
                raw[:8] = (29).to_bytes(8, 'little')
                raw[8:] = (41).to_bytes(16, 'little')
            elif kind == 1:
                struct.pack_into('<qI', raw, 8, 6, defect['links'])
            elif kind == 9:
                struct.pack_into('<II', raw, 0, 0x400 if defect['reparse'] else 0x80, defect['reparse'])
            ct.memmove(target, bytes(raw), size)
            return 1
        native.kernel = types.SimpleNamespace(GetFileInformationByHandleEx=mock.Mock(side_effect=information),
                                              GetFileType=mock.Mock(return_value=1))
        native.security_digest = lambda handle: 'sha256:' + '2' * 64
        self.assertEqual((native.info(7)['dev'], native.info(7)['ino']), (29, 41))
        for key, value in (('links', 2), ('reparse', 0xA0000003)):
            defect.update(links=1, reparse=0)
            defect[key] = value
            with self.assertRaises(tc.InstallRefused):
                native.info(7)
        stored = bytearray()
        position, zero = [0], [False]
        def write(handle, source, size, count, overlapped):
            length = 0 if zero[0] else min(size, 3)
            stored.extend(ct.string_at(source, length))
            ct.cast(count, ct.POINTER(ct.c_uint32))[0] = length
            return 1
        def seek(handle, offset, result, method):
            position[0] = offset
            return 1
        def read(handle, target, size, count, overlapped):
            data = bytes(stored[position[0]:position[0] + size])
            ct.memmove(target, data, len(data))
            position[0] += len(data)
            ct.cast(count, ct.POINTER(ct.c_uint32))[0] = len(data)
            return 1
        native.kernel.WriteFile = mock.Mock(side_effect=write)
        native.kernel.ReadFile = mock.Mock(side_effect=read)
        native.kernel.SetFilePointerEx = mock.Mock(side_effect=seek)
        native.kernel.FlushFileBuffers = mock.Mock(return_value=1)
        native.write(7, b'complete native bytes')
        self.assertEqual(stored, b'complete native bytes')
        zero[0] = True
        with self.assertRaisesRegex(tc.InstallRefused, 'no-progress'):
            native.write(7, b'cannot progress')


class WindowsPolicyTest(unittest.TestCase):
    class Registry:
        HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = 1, 2
        KEY_READ, KEY_WOW64_64KEY, REG_SZ = 0x20019, 0x100, 1

        def __init__(self):
            self.values, self.accesses, self.error = {}, [], False

        def OpenKey(self, hive, path, reserved, access):
            self.accesses.append((hive, path, access))
            if self.error:
                raise PermissionError("controlled registry access failure")
            if not any(key[:2] == (hive, path) for key in self.values):
                raise FileNotFoundError()
            return (hive, path)

        def QueryValueEx(self, key, name):
            try:
                return self.values[(*key, name)]
            except KeyError:
                raise FileNotFoundError() from None

        def CloseKey(self, key):
            pass

    def test_registry_deny_absence_and_access_error_are_distinct(self):
        registry = self.Registry()
        with mock.patch.object(tc, "_windows_known_folder", return_value="C:\\ProgramData", create=True):
            factory = getattr(tc, "_WindowsPolicy", None)
            self.assertIsNotNone(factory, "the local native Windows reader must consume registry state")
            reader = factory(registry=registry)
            empty = reader.collect("claude")
            self.assertEqual(empty["problems"], [])
            registry.values[(1, r"SOFTWARE\Policies\ClaudeCode", "Settings")] = \
                ('{"permissions":{"deny":["Read"]}}', 1)
            denied = reader.collect("claude")
            self.assertEqual(json.loads(denied["values"][0][1])["permissions"]["deny"], ["Read"])
            registry.error = True
            self.assertTrue(reader.collect("claude")["problems"])

    def test_codex_knownfolder_and_64bit_registry_override_are_not_environment_guesses(self):
        registry = self.Registry()
        registry.values[(1, r"SOFTWARE\Policies\OpenAI\Codex", "RequirementsPath")] = \
            (r"D:\Policy\requirements.toml", 1)
        with mock.patch.object(tc, "_windows_known_folder", return_value=r"C:\ProgramData", create=True):
            factory = getattr(tc, "_WindowsPolicy", None)
            self.assertIsNotNone(factory, "the selected Windows system sources need a native reader")
            found = factory(registry=registry).collect("codex")
        self.assertEqual(str(found["files"][1][1]), r"D:\Policy\requirements.toml")
        self.assertTrue(all(access & 0x100 for _hive, _path, access in registry.accesses))


class NativeContinuityTest(unittest.TestCase):
    class Reader:
        def __init__(self):
            self.host_reads = 0
            self.fail = None
            self.mount_id = b"mount001"

        def host_uuid(self):
            self.host_reads += 1
            return bytes.fromhex("12345678123456781234567812345678")

        def principal(self):
            return (501, 501)

        def statfs(self, descriptor):
            data = bytearray(2168)
            data[48:56] = self.mount_id
            struct.pack_into("<I", data, 64, 0x1000)
            data[72:76] = b"hfs\0"
            data[88:90] = b"/\0"
            if self.fail == "remote":
                struct.pack_into("<I", data, 64, 0)
            if self.fail == "apfs":
                data[72:77] = b"apfs\0"
            return bytes(data)

        def attributes(self, descriptor, volume):
            size = 112 if volume else 32
            data = bytearray(size)
            struct.pack_into("<I", data, 0, size)
            struct.pack_into("<IIIII", data, 4, 0x80000000 if volume else 0x80000040,
                             0xC0060000 if volume else 0, 0, 0, 0)
            if volume:
                struct.pack_into("<I", data, 24, 0x24001)
                struct.pack_into("<I", data, 40, 0x24001)
                data[56:72] = b"volume-uuid-0001"
                struct.pack_into("<II", data, 72, 0x40, 0xC0060000)
                struct.pack_into("<II", data, 92, 0x40, 0xC0060000)
            else:
                data[24:32] = os.fstat(descriptor).st_ino.to_bytes(8, "little")
            if self.fail == "short":
                return bytes(data[:-1])
            if self.fail == "invalid-mask":
                data[4:24] = bytes(20)
            if self.fail == "zero-object" and not volume:
                data[24:32] = bytes(8)
            return bytes(data)

        def boot_uuid(self):
            return bytes.fromhex("abcdefabcdefabcdefabcdefabcdefab")

    def setUp(self):
        self.root = make_tmp_dir(self)
        self.reader = self.Reader()

    def capture(self):
        with mock.patch.object(tc, "_recovery_problem", return_value="recovery-profile-not-installed-qualified"), \
                mock.patch.object(tc, "_DarwinContinuity", return_value=self.reader, create=True):
            return tc.capture_local_continuity("12" * 32, {"receiving": self.root, "holder": self.root})

    def test_candidate_reads_are_real_but_do_not_assert_installed_qualification(self):
        found = self.capture()
        self.assertGreaterEqual(self.reader.host_reads, 2)
        self.assertEqual(found["status"], "unsupported")
        self.assertEqual(found["observation_provenance"]["candidate_status"], "verified")
        self.assertEqual(found["roots"][0]["roles"], ["holder", "receiving"])
        self.assertIsNotNone(found["roots"][0]["permanent_object_digest"])
        self.assertNotIn(str(os.getuid()), json.dumps(found))

    def test_native_buffers_remote_filesystem_and_invalid_attributes_refuse(self):
        for failure in ("remote", "apfs", "short", "invalid-mask", "zero-object"):
            with self.subTest(failure=failure):
                self.reader.fail = failure
                found = self.capture()
                self.assertNotEqual(found["observation_provenance"].get("candidate_status"), "verified")
                self.assertEqual(found["status"], "unknown" if failure in
                                 ("short", "invalid-mask", "zero-object") else "unsupported")


    def test_persistent_comparison_blocks_shared_state_other_host_and_all_unknown_sources(self):
        import copy

        old = self.capture()
        old["status"] = old["host"]["status"] = "qualified"
        for row in old["roots"]:
            row["status"] = "qualified"
        self.reader.mount_id = b"mount002"
        current = self.capture()
        current["status"] = current["host"]["status"] = "qualified"
        for row in current["roots"]:
            row["status"] = "qualified"
        self.assertEqual(current["roots"], old["roots"])
        boot = {"status": "qualified", "source": "kern.bootsessionuuid", "profile_id": tc.RECOVERY_PROFILE,
                "source_abi_digest": tc._DARWIN_ABI_DIGEST, "value_digest": "sha256:" + "a" * 64}
        next_boot = {**boot, "value_digest": "sha256:" + "b" * 64}
        leaf = self.root / "controlled-node"
        leaf.write_bytes(b"bytes")
        with mock.patch.object(tc, "_recovery_problem", return_value="recovery-profile-not-installed-qualified"), \
                mock.patch.object(tc, "_DarwinContinuity", return_value=self.reader):
            nodes = tc.capture_node_continuity("12" * 32, {"controlled-node": leaf})
        nodes["status"] = "qualified"
        record = {"nonce": "12" * 32, "continuity": old, "boot": boot}
        self.assertIsNone(tc.postboot_problem(record, current, next_boot, nodes, nodes))
        self.assertIsNotNone(tc.postboot_problem(record, current, boot, nodes, nodes))
        for field in ("kernel_host_digest", "principal_digest", "namespace"):
            changed = copy.deepcopy(current)
            changed["host"][field] = "different"
            self.assertIsNotNone(tc.postboot_problem(record, changed, next_boot, nodes, nodes))
        for key in ("permanent_object_digest", "volume_uuid_digest", "owner_mode_digest", "path_bytes_digest"):
            changed = copy.deepcopy(current)
            changed["roots"][0][key] = "sha256:" + "f" * 64
            self.assertIsNotNone(tc.postboot_problem(record, changed, next_boot, nodes, nodes))
        self.assertIsNotNone(tc.postboot_problem(record, {**current, "status": "unknown"}, next_boot, nodes, nodes))
        self.assertIsNotNone(tc.postboot_problem(record, current, next_boot, {}, nodes))

    def test_node_candidate_uses_native_permanent_object_and_refuses_replacement(self):
        leaf = self.root / "leaf"
        leaf.write_bytes(b"bytes")
        with mock.patch.object(tc, "_recovery_problem", return_value="recovery-profile-not-installed-qualified"), \
                mock.patch.object(tc, "_DarwinContinuity", return_value=self.reader):
            before = tc.capture_node_continuity("12" * 32, {"leaf": leaf})
            leaf.rename(self.root / "old-leaf")
            leaf.write_bytes(b"bytes")
            after = tc.capture_node_continuity("12" * 32, {"leaf": leaf})
        self.assertEqual(before["candidate_status"], "verified")
        self.assertEqual(before["status"], "unsupported")
        self.assertNotEqual(before["nodes"][0]["object_digest"], after["nodes"][0]["object_digest"])

    def test_settlement_rechecks_sources_and_cas_without_claiming_delivery(self):
        from oah.lease import LeaseError

        case = TransportLedgerTest("test_stale_expected_record_refuses_without_writing")
        self.addCleanup(case.doCleanups)
        case.setUp()
        nonce = "12" * 32
        roots = tc.current_role_roots(case.lease, case.state_dir, case.repo)
        manifest_bytes = b'{}'
        manifest = hashlib.sha256(manifest_bytes).hexdigest()
        done = tc.install(case.repo, manifest, {manifest: manifest_bytes})
        self.addCleanup(done.close)
        paths = tc.installation_node_paths(case.repo, manifest, tuple(done.leaves), data_created=True)
        with mock.patch.object(tc, "_recovery_problem", return_value="recovery-profile-not-installed-qualified"), \
                mock.patch.object(tc, "_DarwinContinuity", return_value=self.reader):
            continuity = tc.capture_local_continuity(nonce, roots)
            nodes = tc.capture_node_continuity(nonce, paths)
        continuity["status"] = continuity["host"]["status"] = nodes["status"] = "qualified"
        for row in continuity["roots"]:
            row["status"] = "qualified"
        boot = {"status": "qualified", "source": "kern.bootsessionuuid", "profile_id": tc.RECOVERY_PROFILE,
                "source_abi_digest": tc._DARWIN_ABI_DIGEST, "value_digest": "sha256:" + "a" * 64}
        record = tc.origin_record(transport={"read_plan": {"files": [], "manifest_sha256": "sha256:" + manifest},
                                             "view": {}}, root=case.repo,
                    repo=case.lease.design["task_identity"]["repo_common_identity"], run="rPostboot",
                    task_identity=case.lease.design["task_identity"], basis=case.lease.design["basis"],
                    epoch={"lease_id": case.lease.id, "generation": case.lease.generation},
                    profile={"operation": "inspect"}, h0=tc.git_head(case.repo), policy={}, nonce=nonce,
                    continuity=continuity, boot=boot, root_identity=done.root_identity)
        start = case.store.register_transport_origin(task="t1", lease_id=case.lease.id,
                                                      generation=case.lease.generation, record=record)
        origin = start["record_digest"]
        latest = case.store.observe_transport(task="t1", origin=origin, expected=start, kind="installed",
                    fact={**done.facts(), "persistent_nodes": nodes})
        with self.assertRaises(LeaseError):
            case.store.settle_transport(task="t1", origin=origin, expected=latest, root=case.repo, settle="aborted")
        self.assertEqual(tc.transport_views(case.state_dir)[origin].latest, latest)
        changed_boot = {**boot, "value_digest": "sha256:" + "b" * 64}
        with mock.patch.object(tc, "capture_local_continuity", return_value=continuity), \
                mock.patch.object(tc, "capture_node_continuity", return_value=nodes), \
                mock.patch.object(tc, "boot_identity", return_value=changed_boot):
            with self.assertRaises(LeaseError):
                case.store.settle_transport(task="t1", origin=origin, expected=start, root=case.repo, settle="aborted")
            result = case.store.settle_transport(task="t1", origin=origin, expected=latest, root=case.repo,
                                                  settle="aborted")
        view = tc.transport_views(case.state_dir)[origin]
        self.assertEqual(view.latest, result)
        self.assertTrue(view.quiescent)
        self.assertFalse(view.delivered)
        self.assertEqual(view.record, record)
        self.assertEqual(view.last("boot-settled")["fact"]["exit_observation"], "lost")


class PolicyCollectorTest(unittest.TestCase):
    def setUp(self):
        self.home = make_tmp_dir(self)
        self.root = make_git_repo(self)
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

    def test_managed_directory_absence_plain_link_and_error_are_distinct(self):
        concrete = type(self.root)
        managed = self.home / "managed-settings.d"
        linked = self.home / "external-managed"
        linked.mkdir()
        self.settings(linked / "deny.json", {"permissions": {"deny": ["Read"]}})

        def redirected(value, *parts):
            if str(value) == "/Library/Application Support/ClaudeCode/managed-settings.d":
                return managed
            return concrete(value, *parts)

        with mock.patch.object(tc.pathlib, "Path", side_effect=redirected):
            absent = self.collect()
            self.assertEqual(absent["local_policy"], "verified")
            managed.mkdir()
            plain = self.collect()
            self.assertEqual(plain["local_policy"], "verified")
            self.assertNotEqual(plain["policy_sha256"], absent["policy_sha256"])
            managed.rmdir()
            managed.symlink_to(linked, target_is_directory=True)
            self.assertEqual(self.collect()["local_policy"], "unknown")
            managed.unlink()
            managed.mkdir()
            real = concrete.iterdir

            def unreadable(path):
                if path == managed:
                    raise PermissionError("controlled directory failure")
                return real(path)

            with mock.patch.object(concrete, "iterdir", unreadable):
                self.assertEqual(self.collect()["local_policy"], "unknown")


@unittest.skipIf(os.name == "nt", "the anchored POSIX install is the qualified route")
class InstallExposureTest(unittest.TestCase):
    def test_replaced_root_and_lost_authority_refuse_before_the_next_install_mutation(self):
        import contextlib

        root = make_tmp_dir(self) / "root"
        root.mkdir()
        original = tc._identity(root.stat())
        old = root.with_name("old")
        root.rename(old)
        root.mkdir()
        with self.assertRaisesRegex(tc.InstallRefused, "root changed"):
            tc.install(root, "a" * 64, {"b" * 64: b"bytes"}, expected_root=original)
        self.assertFalse((root / tc.DATA_DIR).exists())
        steps = []

        @contextlib.contextmanager
        def guard(name, done):
            if steps:
                raise tc.InstallRefused("current authority revoked")
            yield

        with self.assertRaisesRegex(tc.InstallRefused, "authority revoked"):
            tc.install(root, "a" * 64, {"b" * 64: b"bytes"}, guard=guard,
                       progress=lambda name, done: steps.append((name, done.facts())))
        self.assertEqual([name for name, _fact in steps], [tc.DATA_DIR])
        self.assertTrue((root / tc.DATA_DIR).is_dir())
        self.assertFalse((root / tc.DATA_DIR / ("a" * 64)).exists())

    def test_live_role_roots_reject_a_copied_state_and_changed_ancestor(self):
        root = make_tmp_dir(self)
        state = root / "state"
        holder = root / "holder"
        state.mkdir()
        holder.mkdir()
        roots = {"state-dir": state, "holder-worktree": holder}
        live = tc.LiveRoots(roots)
        self.addCleanup(live.close)
        record = {"nonce": "c" * 64, "live_roots": live.facts("c" * 64)}
        self.assertIsNone(tc.verify_live_roots(record, roots))
        state.rename(root / "old-state")
        state.mkdir()
        self.assertFalse(live.current())
        self.assertIsNotNone(tc.verify_live_roots(record, roots))
        self.assertIsNotNone(tc.verify_live_roots(record, roots, retired_roles=["state-dir"]))

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

    def test_cleanup_flushes_each_changed_directory_before_publishing_progress(self):
        done = self.installed()
        targets = {"mode": done.m_fd, "file": done.m_fd, self.manifest: done.data_fd, tc.DATA_DIR: done.root_fd}
        flushed = []
        fsync = os.fsync

        def flush(descriptor):
            fsync(descriptor)
            flushed.append(descriptor)

        def after(name, observed, entry):
            target = targets[entry if entry != "dir" else name]
            self.assertTrue(flushed, (name, observed, entry))
            self.assertEqual(flushed.pop(), target)

        with mock.patch.object(os, "fsync", side_effect=flush):
            tc.remove_leaves(done, sorted(done.leaves), before=lambda _: None, after=after)
            tc.remove_directories(done, before=lambda _: None, after=after)
        self.assertFalse((self.repo / tc.DATA_DIR).exists())

    def test_failed_cleanup_flush_retains_unknown_progress_after_unlink(self):
        done = self.installed()
        names, progress = sorted(done.leaves), []
        with mock.patch.object(os, "fsync", side_effect=[None, OSError("controlled directory flush failed")]):
            with self.assertRaisesRegex(OSError, "directory flush"):
                tc.remove_leaves(done, names, before=lambda _: None, after=lambda *row: progress.append(row))
        self.assertEqual(progress, [(self.manifest, "writable", "mode")])
        self.assertFalse((self.repo / tc.DATA_DIR / self.manifest / names[0]).exists())
        self.assertTrue((self.repo / tc.DATA_DIR / self.manifest / names[1]).exists())

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

    def test_same_instance_rebind_allows_only_current_housekeeping_of_old_origin(self):
        from oah.lease import LeaseError

        start = self.store.register_transport_origin(task="t1", lease_id=self.lease.id,
                                                     generation=self.lease.generation, record=self.origin_record)
        origin = start["record_digest"]
        changed = self.oah("start", "--task", "t1", "--agent", "claude", "--packet",
                           ".oah/tasks/t1/PACKET.md", "--scope", "src,docs")
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        latest = self.store.observe_transport(task="t1", origin=origin, expected=start, kind="cleanup-intent", fact={})
        with self.assertRaises(LeaseError) as caught:
            self.store.observe_transport(task="t1", origin=origin, expected=latest, kind="delivery-complete", fact={})
        self.assertEqual(caught.exception.code, tc.TRANSPORT_AUTHORITY)
        self.assertEqual(tc.transport_views(self.state_dir)[origin].record, self.origin_record)

    def test_durable_exit_replay_needs_latest_checkpoint_but_no_fresh_handles(self):
        from oah.lease import LeaseError

        start = self.store.register_transport_origin(task="t1", lease_id=self.lease.id,
                                                     generation=self.lease.generation, record=self.origin_record)
        origin = start["record_digest"]
        original = {"origin": origin, **{name: self.origin_record[name] for name in
                    ("run", "task_identity", "task_basis_digest", "epoch", "root_digest", "manifest")}}
        fact = {"quiescent": True, "return_code": 0}
        exit_e, _ = self.store.observe_transport_exit(original=original, expected=start, kind="exit-observed",
                                                      fact=fact, live_proof=self.proof(origin))
        latest, _ = self.store.observe_transport_exit(original=original, expected=exit_e, kind="exposure",
                        fact={"state": "staged", "why": "controlled exposure"}, live_proof=self.proof(origin))
        with self.assertRaises(LeaseError) as caught:
            self.store.observe_transport_exit(original=original, expected=start, kind="exit-observed", fact=fact,
                                               live_proof=self.proof(origin))
        self.assertEqual(caught.exception.code, tc.TRANSPORT_CONFLICT)
        replay, durable = self.store.observe_transport_exit(original=original, expected=latest, kind="exit-observed",
                                                             fact=fact, live_proof=None)
        self.assertEqual((replay, durable), (latest, True))
        self.assertEqual(tc.transport_views(self.state_dir)[origin].exposure["state"], "staged")

    def test_a_published_spawn_intent_cannot_be_relabelled_prepared_no_spawn(self):
        from oah.lease import LeaseError

        start = self.store.register_transport_origin(task="t1", lease_id=self.lease.id,
                                                      generation=self.lease.generation, record=self.origin_record)
        origin = start["record_digest"]
        latest = self.store.observe_transport(task="t1", origin=origin, expected=start, kind="spawn-intent", fact={})
        original = {"origin": origin, **{name: self.origin_record[name] for name in
                    ("run", "task_identity", "task_basis_digest", "epoch", "root_digest", "manifest")}}
        with self.assertRaises(LeaseError) as caught:
            self.store.observe_transport_exit(original=original, expected=latest, kind="no-spawn",
                        fact={"quiescent": True, "spawn_intent": False}, live_proof=self.proof(origin))
        self.assertEqual(caught.exception.code, tc.TRANSPORT_NONTERMINAL)
        self.assertFalse(tc.transport_views(self.state_dir)[origin].quiescent)
        self.assertEqual(tc.transport_views(self.state_dir)[origin].latest, latest)

    def test_malformed_origin_registration_never_proves_absence_for_legacy_disposal(self):
        from oah.review.runner import _snapshot_disposal
        from oah.review import snapshot

        parent = make_tmp_dir(self) / "snapshot"
        parent.mkdir()
        (parent / "owned").write_text("keep")
        self.store._commit(self.store._load(), "design_transport_origin", "t1", "claude", self.lease.id,
                           design_schema=2, transport={"version": 1, "origin": "sha256:" + "f" * 64,
                                                      "record": self.origin_record})
        disposition = []
        outcome = _snapshot_disposal(self.state_dir, "rX", disposition)(parent, snapshot._object(parent.lstat()))
        self.assertEqual(outcome[0], "retained")
        self.assertTrue((parent / "owned").exists())

    def test_malformed_optional_origin_ownership_is_unknown_even_with_an_exact_digest(self):
        for field, value in (("retirement", "not-an-allocation"), ("live_roots", ["not-a-root-proof"])):
            with self.subTest(field=field):
                record = dict(self.origin_record, run="r-invalid-" + field, **{field: value})
                origin = tc.origin_digest(record)
                self.store._commit(self.store._load(), "design_transport_origin", "t1", "claude", self.lease.id,
                            design_schema=2, transport={"version": 1, "origin": origin, "record": record})
                views = tc.transport_views(self.state_dir, task="t1", run=record["run"])
                self.assertTrue(views)
                self.assertTrue(any(view.problem is not None for view in views.values()))
                self.assertFalse(tc.transport_absence(self.state_dir, run=record["run"])[0])

    def test_proved_disjoint_other_task_exposure_does_not_poison_completion(self):
        record = dict(self.origin_record, task_identity={**self.origin_record['task_identity'], 'task_id': 'other'})
        origin = tc.origin_digest(record)
        initial = {"sequence": 0, "record_digest": origin}
        fact = {"state": "staged", "why": "other task only"}
        events = [{"event": "design_transport_origin", "task": "other", "design_schema": 2,
                   "transport": {"version": 1, "origin": origin, "record": record}},
                  {"event": "design_transport_observation", "task": "other", "design_schema": 2,
                   "transport": {"version": 1, "origin": origin, "sequence": 1, "predecessor": initial,
                                 "kind": "exposure", "fact": fact,
                                 "record_digest": tc.observation_digest(origin, 1, initial, "exposure", fact)}}]
        with mock.patch.object(tc, "intersects", return_value={"state": "clean", "code": None, "why": "disjoint"}):
            found = tc.completion_guard(self.state_dir, "t1", root=self.repo, repo=None, current=None, events=events)
        self.assertTrue(found.ok, found)
        with mock.patch.object(tc, "intersects", return_value={"state": "unknown", "code": tc.TRANSPORT_HISTORY_UNKNOWN,
                                                               "why": "unreadable candidate"}):
            found = tc.completion_guard(self.state_dir, "t1", root=self.repo, repo=None, current=None, events=events)
        self.assertEqual(found.code, tc.TRANSPORT_HISTORY_UNKNOWN)

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
        again, durable = later.observe_transport_exit(original=original, expected=exit_record, kind="exit-observed",
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

    def read_fact_case(self, *, fail: bool):
        self.build(BIG_PARTS)
        program = '''import sys
from unittest import mock
from oah.lease import Store
from oah.cli import main
original = Store.observe_transport_exit
def persist(writer, **kwargs):
    if sys.argv[1] == "fault" and kwargs["kind"] == "reads":
        fact = kwargs["fact"]
        assert fact["reference_delivery"] == "complete"
        assert all(row["complete"] for row in fact["coverage"].values())
        print("CONTROL: complete actual fixture observations; controlled read-fact write failure", file=sys.stderr)
        raise OSError("controlled read-fact publication failure")
    return original(writer, **kwargs)
with mock.patch.object(Store, "observe_transport_exit", persist):
    raise SystemExit(main(["call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low"]))
'''
        result = subprocess.run([sys.executable, "-B", "-c", program, "fault" if fail else "normal"], cwd=self.repo,
                                env=self.env, text=True, capture_output=True, timeout=90)
        print("READ_FACT_CHILD", "fault" if fail else "normal", result.returncode, result.stdout, result.stderr)
        return result

    def test_failed_read_fact_retains_data_and_cannot_deliver_or_finish(self):
        result = self.read_fact_case(fail=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("complete actual fixture observations", result.stderr)
        kinds = [item["transport"].get("kind") for item in self.events()]
        self.assertNotIn("reads", kinds)
        self.assertNotIn("delivery-complete", kinds)
        self.assertNotIn("cleanup-intent", kinds)
        self.assertNotIn("verified-cleaned", kinds)
        self.assertTrue((self.repo / tc.DATA_DIR).exists())
        sidecar = self.last_run() / "design.json"
        saved = json.loads(sidecar.read_bytes())["receiving"]
        self.assertFalse(saved["delivered"])
        self.assertEqual(saved["reference_delivery"], "unverified")
        self.assertTrue(saved["observed_reads"])
        self.assertTrue(all(row["model_directed"] == "matched" for row in saved["observed_reads"]))
        sidecar.unlink()
        finished = self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "s",
                            "--return", str(self.returned()), "--allow-missing-wiki-maintenance", "--allow-structural")
        self.assertEqual(finished.returncode, 1, finished.stdout + finished.stderr)
        self.assertNotIn("reads", [item["transport"].get("kind") for item in self.events()])

    def test_durable_read_fact_precedes_delivery_cleanup_and_successful_finish(self):
        result = self.read_fact_case(fail=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kinds = [item["transport"].get("kind") for item in self.events()]
        self.assertLess(kinds.index("reads"), kinds.index("delivery-complete"))
        self.assertLess(kinds.index("delivery-complete"), kinds.index("cleanup-intent"))
        self.assertLess(kinds.index("cleanup-intent"), kinds.index("verified-cleaned"))
        self.assertFalse((self.repo / tc.DATA_DIR).exists())
        saved = json.loads((self.last_run() / "design.json").read_bytes())["receiving"]
        self.assertTrue(saved["delivered"])
        finished = self.oah("finish", "--task", "t1", "--agent", "claude", "--summary", "s",
                            "--return", str(self.returned()), "--allow-missing-wiki-maintenance", "--allow-structural")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)

    def test_a_complete_read_is_delivered_and_cleaned(self):
        self.build(BIG_PARTS)
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kinds = [event["transport"].get("kind") for event in self.events()]
        self.assertEqual([kind for kind in kinds if kind != "install-progress"][:7],
                         [None, "installed", "spawn-intent", "running", "exit-observed", "reads",
                                     "delivery-complete"])
        progress = [event["transport"]["fact"] for event in self.events()
                    if event["transport"].get("kind") == "install-progress"]
        self.assertEqual(progress[0]["step"], tc.DATA_DIR)
        self.assertEqual(progress[-1]["step"], "sealed")
        self.assertTrue(progress[-1]["leaves"])
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

        roots = tc.LiveRoots(tc.current_role_roots(self.lease, self.state_dir, self.repo))
        self.addCleanup(roots.close)
        record = tc.origin_record(
            transport=plan, root=self.repo, repo=repo_identity(self.repo), run="rR",
            task_identity=self.lease.design["task_identity"], basis=self.lease.design["basis"],
            epoch={"lease_id": self.lease.id, "generation": self.lease.generation, "agent": self.lease.agent},
            profile={"id": tc.CLAUDE_PROFILE, "operation": "inspect"},
            h0=tc.git_head(self.repo), policy={}, nonce="00" * 32, continuity={}, boot={},
            root_identity=roots.identity("receiving-root"), live_roots=roots.facts("00" * 32))
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

    def acknowledge(self):
        result = self.oah("design", "task", "t1", "--transport-cleanup", "--retain", *self.expect())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for worktree in (True, False):
            checked = tc.completion_guard(self.state_dir, "t1", root=self.repo, repo=None, current=None,
                                          worktree=worktree)
            self.assertTrue(checked.ok, checked.reason)

    def assert_changed_retention(self):
        for worktree in (True, False):
            checked = tc.completion_guard(self.state_dir, "t1", root=self.repo, repo=None, current=None,
                                          worktree=worktree)
            self.assertFalse(checked.ok, checked.reason)

    def test_retained_same_byte_new_object_refuses_completion_and_landing(self):
        self.acknowledge()
        directory = self.repo / tc.DATA_DIR / self.manifest
        path = directory / self.name
        before = path.stat()
        mode = stat.S_IMODE(directory.stat().st_mode)
        directory.chmod(0o755)
        replacement = directory / "replacement"
        replacement.write_bytes(self.data)
        replacement.chmod(stat.S_IMODE(before.st_mode))
        os.replace(replacement, path)
        directory.chmod(mode)
        self.assertNotEqual(path.stat().st_ino, before.st_ino)
        self.assert_changed_retention()
        self.assertEqual(path.read_bytes(), self.data)
        self.assertNotEqual(path.stat().st_ino, before.st_ino)

    def test_retained_content_change_refuses_completion_and_landing(self):
        self.acknowledge()
        path = self.repo / tc.DATA_DIR / self.manifest / self.name
        path.chmod(0o644)
        path.write_bytes(b"changed original\n")
        path.chmod(0o444)
        self.assert_changed_retention()
        self.assertEqual(path.read_bytes(), b"changed original\n")

    def test_retained_leaf_mode_change_refuses_completion_and_landing(self):
        self.acknowledge()
        path = self.repo / tc.DATA_DIR / self.manifest / self.name
        path.chmod(0o644)
        self.assert_changed_retention()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_retained_parent_mode_change_refuses_completion_and_landing(self):
        self.acknowledge()
        directory = self.repo / tc.DATA_DIR / self.manifest
        directory.chmod(0o700)
        self.assert_changed_retention()
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_retained_parent_replacement_refuses_completion_and_landing(self):
        self.acknowledge()
        directory = self.repo / tc.DATA_DIR / self.manifest
        before = directory.stat()
        moved = directory.with_name("old-parent")
        directory.rename(moved)
        directory.mkdir()
        for path in moved.iterdir():
            target = directory / path.name
            target.write_bytes(path.read_bytes())
            target.chmod(stat.S_IMODE(path.stat().st_mode))
        directory.chmod(stat.S_IMODE(before.st_mode))
        self.assert_changed_retention()
        self.assertTrue(moved.exists())
        self.assertEqual((directory / self.name).read_bytes(), self.data)

    def test_retained_owner_change_refuses_completion_and_landing(self):
        import types

        self.acknowledge()
        path = self.repo / tc.DATA_DIR / self.manifest / self.name
        original = path.stat()
        named, held = os.stat, os.fstat

        def changed(value):
            if (value.st_dev, value.st_ino) != (original.st_dev, original.st_ino):
                return value
            fields = {key: getattr(value, key) for key in dir(value) if key.startswith("st_")}
            return types.SimpleNamespace(**dict(fields, st_gid=value.st_gid + 1))

        with mock.patch.object(os, "stat", side_effect=lambda *a, **kw: changed(named(*a, **kw))), \
                mock.patch.object(os, "fstat", side_effect=lambda *a, **kw: changed(held(*a, **kw))):
            self.assert_changed_retention()
        self.assertEqual(path.read_bytes(), self.data)

    def test_landing_elsewhere_rechecks_the_original_retained_objects(self):
        self.acknowledge()
        candidate = make_git_repo(self)
        git_commit(candidate, {"README.md": "other candidate\n"}, "candidate")
        checked = tc.completion_guard(self.state_dir, "t1", root=candidate, repo=None, current=None, worktree=False)
        self.assertTrue(checked.ok, checked.reason)
        path = self.repo / tc.DATA_DIR / self.manifest / self.name
        path.chmod(0o644)
        checked = tc.completion_guard(self.state_dir, "t1", root=candidate, repo=None, current=None, worktree=False)
        self.assertFalse(checked.ok, checked.reason)

    def test_missing_manifest_cannot_adopt_a_replaced_surviving_data_parent(self):
        parent = self.repo / tc.DATA_DIR
        saved = parent.with_name("saved-data-parent")
        parent.rename(saved)
        parent.mkdir()
        result = self.oah("design", "task", "t1", "--transport-cleanup", "--retain", *self.expect())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("ack-retained", tc.transport_views(self.state_dir)[self.origin].kinds())
        self.assertTrue(saved.exists())
        self.assertTrue(parent.exists())

    def test_retained_root_ancestor_mode_change_refuses_completion_and_landing(self):
        self.acknowledge()
        ancestor = self.repo.parent
        previous = stat.S_IMODE(ancestor.stat().st_mode)
        ancestor.chmod(previous ^ stat.S_IXGRP)
        self.assert_changed_retention()
        self.assertEqual(stat.S_IMODE(ancestor.stat().st_mode), previous ^ stat.S_IXGRP)

    def test_exact_cleanup_removes_only_the_created_files(self):
        stale = self.oah("design", "task", "t1", "--transport-cleanup", *self.expect()[:-6],
                         "--expect-record", "1:sha256:" + "0" * 64, *self.expect()[-4:])
        self.assertEqual(stale.returncode, 1)
        self.assertIn(tc.TRANSPORT_CONFLICT, stale.stderr)
        cleaned = self.oah("design", "task", "t1", "--transport-cleanup", *self.expect())
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertFalse((self.repo / tc.DATA_DIR).exists())
        self.assertEqual(tc.transport_views(self.state_dir)[self.origin].state, "VERIFIED_CLEANED")

    def test_expiry_after_intent_refuses_before_the_first_permission_change_or_unlink(self):
        from oah import design_cli
        from oah.design import open_store
        from oah.lease import LeaseError, Store
        from tests.fixtures.gates.support import build_args

        directory = self.repo / tc.DATA_DIR / self.manifest
        before = set(os.listdir(directory))
        mode = stat.S_IMODE(directory.lstat().st_mode)
        observe = Store.observe_transport

        def expire_after_intent(writer, **kwargs):
            result = observe(writer, **kwargs)
            if kwargs["kind"] == "cleanup-intent":
                path = self.state_dir / "leases.json"
                data = json.loads(path.read_bytes())
                data["leases"][0]["expires_at"] = "2000-01-01T00:00:00Z"
                path.write_text(json.dumps(data))
            return result

        args = build_args(["design", "task", "t1", "--transport-cleanup", *self.expect()])
        store = open_store(self.repo, env={"OAH_STATE_DIR": str(self.state_dir)})
        with mock.patch.object(Store, "observe_transport", expire_after_intent):
            with self.assertRaises((LeaseError, DesignError)):
                design_cli._transport_cleanup(args, store)
        self.assertEqual(set(os.listdir(directory)), before)
        self.assertEqual(stat.S_IMODE(directory.lstat().st_mode), mode)

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

    def test_guarded_removal_rejects_a_replaced_parent_before_unlinking_its_original_child(self):
        from oah.review import snapshot

        parent = self.container(1)
        captured, final = self.plan(parent)
        validated = snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
        original = parent / "tree"
        held_aside = parent.parent / "original-tree"
        original.rename(held_aside)
        original.mkdir()
        leaf = original / "00000000"
        (held_aside / leaf.name).rename(leaf)
        self.assertFalse(snapshot.remove(parent, expected_plan=validated, progress=lambda _entry, _state: None))
        self.assertEqual(leaf.read_bytes(), b"x")

    def test_output_slot_keeps_the_final_object_through_validation_and_each_delete(self):
        from oah.review import snapshot

        for phase in ("validation", "delete"):
            with self.subTest(phase=phase):
                parent = self.container(1)
                captured, _ = self.plan(parent)
                output = parent / "last-message.md"
                output.write_bytes(b"result")
                final_slot = snapshot.capture_output_slot(parent, [output.name],
                                    producer={"run": "rS", "checkpoint": "final-output"})
                final = snapshot.finalize_retirement_plan(captured, final_slot)
                validated = snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
                replacement = parent / "replacement"
                replacement.write_bytes(b"result")
                replacement.replace(output)
                if phase == "validation":
                    with self.assertRaises(snapshot.Retained):
                        snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
                else:
                    self.assertFalse(snapshot.remove(parent, expected_plan=validated,
                                                       progress=lambda item, state: None))
                self.assertEqual(output.read_bytes(), b"result")

    def test_final_output_owner_change_cannot_reuse_the_same_object_and_bytes(self):
        import types
        from oah.review import snapshot

        parent = self.container(1)
        captured, _ = self.plan(parent)
        output = parent / "last-message.md"
        output.write_bytes(b"result")
        original = output.stat()
        final = snapshot.finalize_retirement_plan(captured, snapshot.capture_output_slot(
            parent, [output.name], producer={"run": "rS", "checkpoint": "final-output"}))
        named, held = os.stat, os.fstat

        def changed(info):
            if (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino):
                return info
            value = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            value["st_gid"] += 1
            return types.SimpleNamespace(**value)

        with mock.patch.object(os, "stat", side_effect=lambda *a, **kw: changed(named(*a, **kw))), \
                mock.patch.object(os, "fstat", side_effect=lambda *a, **kw: changed(held(*a, **kw))):
            with self.assertRaises(snapshot.Retained):
                snapshot.validate_retirement_plan(final, parent, captured.pages_dir)
        self.assertEqual(output.read_bytes(), b"result")

    def test_origin_bearing_snapshot_consumes_its_durable_complete_plan(self):
        from oah.design.binding import repo_identity
        from oah.lease import Store, persist_create_only
        from oah.review import runner, snapshot

        case = TransportLedgerTest("test_stale_expected_record_refuses_without_writing")
        self.addCleanup(case.doCleanups)
        case.setUp()
        head = subprocess.run(["git", "-C", str(case.repo), "rev-parse", "HEAD"], check=True,
                              text=True, capture_output=True).stdout.strip()
        disposition = []
        dispose = runner._snapshot_disposal(case.state_dir, "rS", disposition)
        parent = None
        with snapshot.make(case.repo, head, base=head, dispose=dispose) as snap:
            parent = snap.parent
            allocation = {"version": 1, "run": "rS", "task_identity": case.lease.design["task_identity"],
                          "task_basis_digest": case.lease.design["basis"]}
            captured = snapshot.capture_retirement_plan(allocation, parent, case.state_dir,
                                                        output_slot={"path": ["last-message.md"]})
            record = tc.origin_record(transport={"read_plan": {"files": [], "manifest_sha256": "sha256:" + "c" * 64},
                                                 "view": {}},
                        root=snap.tree, repo=repo_identity(case.repo), run="rS",
                        task_identity=case.lease.design["task_identity"], basis=case.lease.design["basis"],
                        epoch={"lease_id": case.lease.id, "generation": case.lease.generation},
                        profile={"operation": "inspect"}, h0=tc.git_head(snap.tree), policy={}, nonce="00" * 32,
                        continuity={}, boot={}, root_identity={})
            record["retirement"] = {"descriptor": captured.descriptor, "sha256": captured.descriptor_sha256,
                                    "pages_relative": list(captured.pages_dir.relative_to(case.state_dir).parts)}
            live_roots = tc.LiveRoots(tc.current_role_roots(case.lease, case.state_dir, snap.tree, retirement=True))
            try:
                record["live_roots"] = live_roots.facts(record["nonce"])
            finally:
                live_roots.close()
            writer = Store(case.state_dir)
            start = writer.register_transport_origin(task="t1", lease_id=case.lease.id,
                                                     generation=case.lease.generation, record=record)
            origin = start["record_digest"]
            original = {"origin": origin, **{k: record[k] for k in
                        ("run", "task_identity", "task_basis_digest", "epoch", "root_digest", "manifest")}}
            latest, _ = writer.observe_transport_exit(original=original, expected=start, kind="no-spawn",
                        fact={"quiescent": True, "spawn_intent": False}, live_proof=case.proof(origin))
            latest = writer.observe_transport(task="t1", origin=origin, expected=latest, kind="verified-cleaned",
                                              fact={"head": head})
            final = snapshot.finalize_retirement_plan(captured, {"presence": "absent"})
            raw = snapshot._canonical(final)
            final_sha = hashlib.sha256(raw).hexdigest()
            relative = [*captured.pages_dir.parent.relative_to(case.state_dir).parts, final_sha + ".json"]
            persist_create_only(case.state_dir.joinpath(*relative), raw)
            writer.observe_transport(task="t1", origin=origin, expected=latest, kind="retirement-checkpoint",
                        fact={"base_plan_sha256": captured.descriptor_sha256, "final_sha256": final_sha,
                              "final_bytes": len(raw), "final_relative": relative, "data_checkpoint": latest,
                              "head": head, "output_preserved": True})
        self.assertEqual(disposition[-1][0], "disposed", disposition)
        self.assertFalse(parent.exists())

    def interrupted_retirement(self, *, at_root: bool = False):
        from oah.design.binding import repo_identity
        from oah.lease import Store, persist_create_only
        from oah.review import runner, snapshot

        case = TransportLedgerTest("test_stale_expected_record_refuses_without_writing")
        self.addCleanup(case.doCleanups)
        case.setUp()
        head = tc.git_head(case.repo)["head"]
        if case.lease.design is None:
            self.fail("the retirement fixture has no design-bound lease")
        disposition, faulted = [], []
        observe = Store.observe_transport

        def fail_progress(writer, **kwargs):
            if kwargs["kind"] == "retirement-progress" and not faulted and (not at_root or not kwargs["fact"]["path"]):
                faulted.append(kwargs["fact"]["path"])
                raise OSError("controlled progress write failure after unlink and directory flush")
            return observe(writer, **kwargs)

        with mock.patch.object(Store, "observe_transport", fail_progress):
            with snapshot.make(case.repo, head, base=head,
                               dispose=runner._snapshot_disposal(case.state_dir, "rS", disposition)) as snap:
                parent = snap.parent
                if parent is None:
                    self.fail("the retirement fixture has no owned container")
                plan = snapshot.capture_retirement_plan(
                    {"version": 1, "run": "rS", "task_identity": case.lease.design["task_identity"],
                     "task_basis_digest": case.lease.design["basis"]}, parent, case.state_dir,
                    output_slot={"path": ["last-message.md"]})
                roots = tc.LiveRoots(tc.current_role_roots(case.lease, case.state_dir, snap.tree, retirement=True))
                try:
                    record = tc.origin_record(transport={"read_plan": {"files": [], "manifest_sha256": "sha256:" + "c" * 64},
                                "view": {}}, root=snap.tree, repo=repo_identity(case.repo), run="rS",
                                task_identity=case.lease.design["task_identity"], basis=case.lease.design["basis"],
                                epoch={"lease_id": case.lease.id, "generation": case.lease.generation},
                                profile={"operation": "inspect"}, h0=tc.git_head(snap.tree), policy={}, nonce="00" * 32,
                                continuity={}, boot={}, root_identity={}, live_roots=roots.facts("00" * 32))
                finally:
                    roots.close()
                record["retirement"] = {"descriptor": plan.descriptor, "sha256": plan.descriptor_sha256,
                                        "pages_relative": list(plan.pages_dir.relative_to(case.state_dir).parts)}
                writer = Store(case.state_dir)
                start = writer.register_transport_origin(task="t1", lease_id=case.lease.id,
                                                         generation=case.lease.generation, record=record)
                origin = start["record_digest"]
                original = {"origin": origin, **{key: record[key] for key in
                    ("run", "task_identity", "task_basis_digest", "epoch", "root_digest", "manifest")}}
                latest, _ = writer.observe_transport_exit(original=original, expected=start, kind="no-spawn",
                    fact={"quiescent": True, "spawn_intent": False}, live_proof=case.proof(origin))
                latest = writer.observe_transport(task="t1", origin=origin, expected=latest, kind="verified-cleaned",
                                                  fact={"head": head})
                final = snapshot.finalize_retirement_plan(plan, {"presence": "absent"})
                raw = snapshot._canonical(final)
                digest = hashlib.sha256(raw).hexdigest()
                relative = [*plan.pages_dir.parent.relative_to(case.state_dir).parts, digest + ".json"]
                persist_create_only(case.state_dir.joinpath(*relative), raw)
                writer.observe_transport(task="t1", origin=origin, expected=latest, kind="retirement-checkpoint",
                    fact={"base_plan_sha256": plan.descriptor_sha256, "final_sha256": digest,
                          "final_bytes": len(raw), "final_relative": relative, "data_checkpoint": latest,
                          "head": head, "output_preserved": True})
        self.assertEqual(disposition[-1][0], "retained", disposition)
        self.assertEqual(len(faulted), 1)
        self.assertFalse(parent.joinpath(*faulted[0]).exists())
        self.addCleanup(snapshot.remove, parent)
        return case, parent, origin, plan, final

    def test_pending_absence_recovers_exact_entry_without_counting_original_attempt(self):
        from oah.review import runner

        case, parent, origin, _plan, _final = self.interrupted_retirement()
        view = tc.transport_views(case.state_dir)[origin]
        old_history = list(view.observations)
        self.assertEqual(old_history[-1]["kind"], "retirement-intent")
        outcome = runner.retire_snapshot(case.state_dir, view, parent)
        self.assertEqual(outcome[0], "disposed-noncounting", outcome)
        self.assertFalse(parent.exists())
        after = tc.transport_views(case.state_dir)[origin]
        self.assertEqual(after.observations[:len(old_history)], old_history)
        ack = after.last("retirement-absence-ack")
        if ack is None:
            self.fail("the exact pending absence was not acknowledged")
        self.assertEqual(ack["fact"]["observed"], "absence-after-intent")
        self.assertEqual(ack["fact"]["causality"], "unknown")
        self.assertIs(ack["fact"]["counted"], False)
        self.assertEqual(ack["fact"]["path"], old_history[-1]["fact"]["path"])
        self.assertEqual(ack["fact"]["intent_record"], view.latest)
        retired = after.last("retired")
        if retired is None:
            self.fail("the complete recovered retirement was not recorded")
        self.assertIs(retired["fact"]["counted"], False)

    def test_pending_absence_requires_exact_plan_predecessor_and_complete_census(self):
        import copy
        from oah.review import snapshot

        case, parent, origin, plan, final = self.interrupted_retirement()
        view = tc.transport_views(case.state_dir)[origin]
        event = view.observations[-1]
        predecessor = view.observations[-2]
        proof = {"origin": origin, "sequence": event["sequence"], "record_digest": event["record_digest"],
                 "predecessor": {"sequence": predecessor["sequence"], "record_digest": predecessor["record_digest"]},
                 "fact": event["fact"]}
        with self.assertRaises(snapshot.Retained):
            snapshot.validate_retirement_plan(final, parent, plan.pages_dir)
        for field in ("plan_sha256", "final_sha256", "entry_sha256"):
            bad = copy.deepcopy(proof)
            bad["fact"][field] = "0" * 64
            with self.subTest(field=field), self.assertRaises(snapshot.Retained):
                snapshot.validate_retirement_plan(final, parent, plan.pages_dir, pending=bad)
        bad = copy.deepcopy(proof)
        bad["predecessor"]["record_digest"] = "sha256:" + "0" * 64
        with self.assertRaises(snapshot.Retained):
            snapshot.validate_retirement_plan(final, parent, plan.pages_dir, pending=bad)
        validated = snapshot.validate_retirement_plan(final, parent, plan.pages_dir, pending=proof)
        self.assertTrue(validated.pending_absent)
        replacement = parent.joinpath(*event["fact"]["path"])
        replacement.write_bytes(b"foreign replacement")
        with self.assertRaises(snapshot.Retained):
            snapshot.validate_retirement_plan(final, parent, plan.pages_dir, pending=proof)
        self.assertEqual(replacement.read_bytes(), b"foreign replacement")

    def test_pending_absence_cannot_borrow_missing_root_or_expired_authority(self):
        from oah.lease import LeaseError, Store
        from oah.review import runner, snapshot

        case, parent, origin, _plan, _final = self.interrupted_retirement()
        view = tc.transport_views(case.state_dir)[origin]
        with mock.patch.object(Store, "transport_action_basis", side_effect=LeaseError(tc.TRANSPORT_AUTHORITY, "expired")):
            with self.assertRaises(LeaseError):
                runner.retire_snapshot(case.state_dir, view, parent)
        self.assertEqual(tc.transport_views(case.state_dir)[origin].observations, view.observations)
        moved = parent.with_name(parent.name + "-moved")
        parent.rename(moved)
        self.addCleanup(snapshot.remove, moved)
        with self.assertRaises((OSError, ValueError, snapshot.Retained)):
            runner.retire_snapshot(case.state_dir, view, parent)

    def test_pending_absence_refuses_another_unexplained_missing_entry(self):
        from oah.review import runner, snapshot

        case, parent, origin, plan, final = self.interrupted_retirement()
        view = tc.transport_views(case.state_dir)[origin]
        pending_path = view.observations[-1]["fact"]["path"]
        pages = snapshot.validate_retirement_plan(final, parent, plan.pages_dir, pending={
            "origin": origin, "sequence": view.latest["sequence"], "record_digest": view.latest["record_digest"],
            "predecessor": {"sequence": view.observations[-2]["sequence"],
                            "record_digest": view.observations[-2]["record_digest"]},
            "fact": view.observations[-1]["fact"]})
        other = next(item for item in pages.all_entries if item["type"] == "file" and item["path"] != pending_path)
        parent.joinpath(*other["path"]).unlink()
        with self.assertRaises(snapshot.Retained):
            runner.retire_snapshot(case.state_dir, view, parent)
        self.assertEqual(tc.transport_views(case.state_dir)[origin].observations, view.observations)

    def test_pending_root_absence_uses_surviving_parent_and_never_counts(self):
        import copy
        from oah.review import runner, snapshot

        case, parent, origin, plan, final = self.interrupted_retirement(at_root=True)
        view = tc.transport_views(case.state_dir)[origin]
        event, previous = view.observations[-1], view.observations[-2]
        self.assertEqual(event["fact"]["path"], [])
        removed = frozenset(tuple(row["fact"]["path"]) for row in view.observations if row["kind"] == "retirement-progress")
        proof = {"origin": origin, "sequence": event["sequence"], "record_digest": event["record_digest"],
                 "predecessor": {"sequence": previous["sequence"], "record_digest": previous["record_digest"]},
                 "fact": event["fact"]}
        bad_fact = copy.deepcopy(event["fact"])
        bad_fact.pop("surviving_parent")
        predecessor = {"sequence": previous["sequence"], "record_digest": previous["record_digest"]}
        bad = {**proof, "fact": bad_fact, "record_digest": tc.observation_digest(
            origin, event["sequence"], predecessor, "retirement-intent", bad_fact)}
        with self.assertRaises(snapshot.Retained):
            snapshot.validate_retirement_plan(final, parent, plan.pages_dir, removed=removed, pending=bad)
        result = runner.retire_snapshot(case.state_dir, view, parent)
        self.assertEqual(result[0], "disposed-noncounting", result)
        current = tc.transport_views(case.state_dir)[origin]
        self.assertEqual(current.observations[:len(view.observations)], view.observations)
        acknowledged, retired = current.last("retirement-absence-ack"), current.last("retired")
        if acknowledged is None or retired is None:
            self.fail("the recovered root retirement has no complete non-counting history")
        self.assertEqual(acknowledged["fact"]["path"], [])
        self.assertIs(retired["fact"]["counted"], False)

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
