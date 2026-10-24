# oah review 부품 검사 — 설정, 불변 기록 저장소와 수정 시도 읽기, 스냅샷·카나리, 출력 스키마, 작성 엔진 판정, 맥락과 프롬프트를 실제 git 저장소로 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

from oah.config import ConfigError
from oah.review import (ReviewError, authors, canary, context, prompt, receipts, runner, schema, settings,
                        snapshot)
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def receipt_record(number: int, *, task: str = "t1", base: str = SHA_A, head: str = SHA_B,
                   findings: list | None = None, classes: list | None = None) -> dict:
    """The smallest receipt the record store accepts; the round tests use real receipts."""
    findings = findings if findings is not None else [
        {"finding_id": f"R{number}-F1", "class_id": f"R{number}-C1", "severity": "P1"}]
    classes = classes if classes is not None else [
        {"class_id": f"R{number}-C1", "mechanism": "m", "rule": "r"}]
    return {"schema": 1, "kind": "receipt", "task": task, "round": number, "series_round": number,
            "base": base, "head": head, "findings": findings, "classes": classes}


def attempt_start(number: int, source: str, digest: str, *, findings=("R1-F1",), classes=("R1-C1",),
                  task: str = "t1") -> dict:
    return {"schema": 1, "kind": "attempt-start", "task": task, "attempt_id": f"A{number}",
            "source_receipt": source, "source_receipt_digest": digest, "finding_ids": list(findings),
            "class_ids": list(classes), "before_head": SHA_B, "engine": "codex", "manual": False,
            "started_at": "2026-10-24T00:00:00Z"}


def attempt_outcome(number: int, outcome: str = "applied", after_head: str | None = None,
                    task: str = "t1") -> dict:
    return {"schema": 1, "kind": "attempt-outcome", "task": task, "attempt_id": f"A{number}", "outcome": outcome,
            "after_head": after_head, "evidence": "tests pass", "finished_at": "2026-10-24T00:01:00Z"}


def attempt_head(number: int, after_head: str = SHA_C, task: str = "t1") -> dict:
    return {"schema": 1, "kind": "attempt-head", "task": task, "attempt_id": f"A{number}",
            "after_head": after_head, "evidence": "committed", "bound_at": "2026-10-24T00:02:00Z"}


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=git_env()).stdout.strip()


class SettingsTest(unittest.TestCase):
    def write_config(self, root: pathlib.Path, text: str) -> None:
        (root / ".oah").mkdir(exist_ok=True)
        (root / ".oah" / "config.toml").write_text(text, encoding="utf-8")

    def test_settings_defaults_and_validation(self):
        root = make_tmp_dir(self)
        loaded = settings.load(root)
        self.assertIsNone(loaded.max_rounds)
        self.assertEqual(loaded.round_attempts, 2)
        self.assertIsNone(loaded.codex_sha256)
        self.assertIn(".github/workflows/**", loaded.critical_paths)
        self.assertIn("**/migrations/**", loaded.critical_paths)
        self.assertIn("**/AGENTS.md", loaded.critical_paths)
        pin = "AB" * 32
        self.write_config(root, f'[review]\nmax_rounds = 3\nround_attempts = 4\ncodex_sha256 = "sha256:{pin}"\n')
        loaded = settings.load(root)
        self.assertEqual((loaded.max_rounds, loaded.round_attempts, loaded.codex_sha256), (3, 4, pin.lower()))
        for text, word in (("max_rounds = 0", "max_rounds"), ("max_rounds = true", "max_rounds"),
                           ('max_rounds = "3"', "max_rounds"), ("round_attempts = 0", "round_attempts"),
                           ('codex_sha256 = "abc"', "codex_sha256"), ("critical_paths = [1]", "critical_paths"),
                           ("unknown = 1", "unknown")):
            with self.subTest(text=text):
                self.write_config(root, f"[review]\n{text}\n")
                with self.assertRaises(ConfigError) as caught:
                    settings.load(root)
                self.assertIn(word, str(caught.exception))
                self.assertIn(caught.exception.code, ("OAH_CONFIG_INVALID", "OAH_CONFIG_UNKNOWN_KEY"))


    def test_config_from_commit_bytes_names_its_source(self):
        from oah.config import parse_config

        config = parse_config(b"[review]\nmax_rounds = 0\n", label=".oah/config.toml at the review base abc123")
        self.assertIsNone(config.path)
        with self.assertRaises(ConfigError) as caught:
            settings.from_config(config)
        self.assertIn(".oah/config.toml at the review base abc123", str(caught.exception))
        self.assertEqual(settings.from_config(parse_config(b"", label="x")).round_attempts, 2)
        for data, word in ((b"\xff\xfe", "not UTF-8"), (b"[review\n", "not valid TOML")):
            with self.subTest(word=word):
                with self.assertRaises(ConfigError) as caught:
                    parse_config(data, label="the base config")
                self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")
                self.assertIn(f"the base config is {word}", str(caught.exception))
        self.assertIn(".oah/config.toml", settings.DEFAULTS["critical_paths"])


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.state = make_tmp_dir(self) / "state"

    def folder(self) -> pathlib.Path:
        return self.state / "reviews" / "t1"

    def index_lines(self) -> list[dict]:
        path = self.folder() / "index.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_publish_is_exclusive_and_indexed(self):
        digest = receipts.publish(self.state, "t1", "R1.json", receipt_record(1),
                                  summary={"round": 1, "base": SHA_A, "head": SHA_B, "verdict": "approve",
                                           "outcome": "converged"})
        data = (self.folder() / "R1.json").read_bytes()
        self.assertEqual(digest, "sha256:" + hashlib.sha256(data).hexdigest())
        self.assertEqual(json.loads(data.decode("utf-8")), receipt_record(1))
        self.assertTrue(data.endswith(b"}\n"))
        [line] = self.index_lines()
        self.assertEqual((line["kind"], line["name"], line["digest"], line["round"], line["outcome"]),
                         ("receipt", "R1.json", digest, 1, "converged"))
        self.assertRegex(line["recorded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        with self.assertRaises(receipts.ReviewError) as caught:
            receipts.publish(self.state, "t1", "R1.json", receipt_record(1, head=SHA_C))
        self.assertEqual(caught.exception.code, "OAH_REVIEW_RECORD_EXISTS")
        self.assertEqual((self.folder() / "R1.json").read_bytes(), data)
        self.assertEqual(len(self.index_lines()), 1)
        records = receipts.load(self.state, "t1")
        self.assertEqual(records.problems, ())
        self.assertEqual([(entry.kind, entry.name, entry.digest) for entry in records.entries],
                         [("receipt", "R1.json", digest)])

    def test_publish_refuses_mismatched_names_and_unsafe_tasks(self):
        for name, record in (("R2.json", receipt_record(1)), ("R1.json", dict(receipt_record(1), task="t2")),
                             ("A1-start.json", receipt_record(1)), ("x.json", receipt_record(1))):
            with self.subTest(name=name):
                with self.assertRaises(receipts.ReviewError) as caught:
                    receipts.publish(self.state, "t1", name, record)
                self.assertEqual(caught.exception.code, "OAH_REVIEW_RECORD_INVALID")
        with self.assertRaises(Exception) as caught:
            receipts.publish(self.state, "../t1", "R1.json", receipt_record(1, task="../t1"))
        self.assertEqual(getattr(caught.exception, "code", None), "OAH_TASK_INVALID")
        self.assertFalse((self.state / "reviews").exists() and any((self.state / "reviews").iterdir()))

    def test_digest_mismatch_is_invalid_history(self):
        receipts.publish(self.state, "t1", "R1.json", receipt_record(1))
        path = self.folder() / "R1.json"
        path.write_bytes(path.read_bytes().replace(b'"P1"', b'"P3"'))
        problems = receipts.load(self.state, "t1").problems
        self.assertEqual(len(problems), 1)
        self.assertIn("R1.json", problems[0])
        self.assertIn("digest", problems[0])

    def test_unindexed_record_is_invalid_history(self):
        receipts.publish(self.state, "t1", "R1.json", receipt_record(1))
        (self.folder() / "R2.json").write_text(json.dumps(receipt_record(2)), encoding="utf-8")
        (self.folder() / "rounds.jsonl").write_text("", encoding="utf-8")  # the round log is not a record
        problems = receipts.load(self.state, "t1").problems
        self.assertEqual(len(problems), 1)
        self.assertIn("R2.json", problems[0])
        self.assertIn("index.jsonl", problems[0])

    def test_index_problems_are_reported_not_repaired(self):
        receipts.publish(self.state, "t1", "R1.json", receipt_record(1))
        index = self.folder() / "index.jsonl"
        original = index.read_bytes()
        cases = {
            "duplicate": original + original,
            "torn": original + b'{"kind": "receipt"',
            "unknown kind": original + b'{"kind": "note", "name": "R2.json", "digest": "sha256:' + b"0" * 64 + b'"}\n',
            "missing file": original + json.dumps({"kind": "receipt", "name": "R2.json", "digest": "sha256:" + "0" * 64,
                                                   "recorded_at": "x"}).encode() + b"\n",
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                index.write_bytes(data)
                problems = receipts.load(self.state, "t1").problems
                self.assertTrue(problems, label)
                self.assertEqual(index.read_bytes(), data)
        index.write_bytes(original)
        (self.folder() / "R2.json").write_text(json.dumps(receipt_record(3)), encoding="utf-8")
        index.write_bytes(original + json.dumps({
            "kind": "receipt", "name": "R2.json", "recorded_at": "x",
            "digest": receipts.digest((self.folder() / "R2.json").read_bytes())}).encode() + b"\n")
        problems = receipts.load(self.state, "t1").problems
        self.assertTrue(any("round" in problem for problem in problems), problems)

    def test_attempt_reader_merges_records(self):
        source = receipts.publish(self.state, "t1", "R1.json", receipt_record(1))
        receipts.publish(self.state, "t1", "A1-start.json", attempt_start(1, "R1.json", source))
        receipts.publish(self.state, "t1", "A1-outcome.json", attempt_outcome(1))
        receipts.publish(self.state, "t1", "A1-head.json", attempt_head(1))
        records = receipts.load(self.state, "t1")
        self.assertEqual(records.problems, ())
        [attempt] = receipts.attempts(records)
        self.assertEqual(attempt.problems, ())
        self.assertEqual(attempt.to_json(), {
            "attempt_id": "A1", "source_receipt_digest": source, "finding_ids": ["R1-F1"], "class_ids": ["R1-C1"],
            "before_head": SHA_B, "after_head": SHA_C, "outcome": "applied", "evidence": "tests pass; committed"})
        self.assertEqual(attempt.names, ("A1-start.json", "A1-outcome.json", "A1-head.json"))
        self.assertTrue(attempt.terminal)

    def test_attempt_reader_rejects_each_broken_shape(self):
        two_findings = [{"finding_id": "R1-F1", "class_id": "R1-C1", "severity": "P1"},
                        {"finding_id": "R1-F2", "class_id": "R1-C2", "severity": "P1"}]
        cases = {
            "outcome before start": [("A1-outcome.json", lambda d: attempt_outcome(1, "failed")),
                                     ("A1-start.json", lambda d: attempt_start(1, "R1.json", d))],
            "outcome without start": [("A1-outcome.json", lambda d: attempt_outcome(1, "failed"))],
            "unknown source receipt": [("A1-start.json", lambda d: attempt_start(1, "R1.json", "sha256:" + "0" * 64)),
                                       ("A1-outcome.json", lambda d: attempt_outcome(1, "failed"))],
            "foreign finding": [("A1-start.json", lambda d: attempt_start(1, "R1.json", d, findings=("R9-F1",))),
                                ("A1-outcome.json", lambda d: attempt_outcome(1, "failed"))],
            "classes differ from findings": [
                ("A1-start.json", lambda d: attempt_start(1, "R1.json", d, findings=("R1-F1", "R1-F2"),
                                                          classes=("R1-C1",))),
                ("A1-outcome.json", lambda d: attempt_outcome(1, "failed"))],
            "unfinished": [("A1-start.json", lambda d: attempt_start(1, "R1.json", d))],
            "applied but not bound": [("A1-start.json", lambda d: attempt_start(1, "R1.json", d)),
                                      ("A1-outcome.json", lambda d: attempt_outcome(1, "applied"))],
            "head after no-change": [("A1-start.json", lambda d: attempt_start(1, "R1.json", d)),
                                     ("A1-outcome.json", lambda d: attempt_outcome(1, "no-change")),
                                     ("A1-head.json", lambda d: attempt_head(1))],
            "unknown outcome": [("A1-start.json", lambda d: attempt_start(1, "R1.json", d)),
                                ("A1-outcome.json", lambda d: attempt_outcome(1, "done"))],
            "short before head": [("A1-start.json", lambda d: dict(attempt_start(1, "R1.json", d), before_head="abc")),
                                  ("A1-outcome.json", lambda d: attempt_outcome(1, "failed"))],
        }
        expected = {"outcome before start": "start", "outcome without start": "start",
                    "unknown source receipt": "source receipt", "foreign finding": "R9-F1",
                    "classes differ from findings": "class", "unfinished": "unfinished",
                    "applied but not bound": "commit", "head after no-change": "head",
                    "unknown outcome": "outcome", "short before head": "before_head"}
        for label, steps in cases.items():
            with self.subTest(label=label):
                state = make_tmp_dir(self) / "state"
                source = receipts.publish(state, "t1", "R1.json", receipt_record(1, findings=two_findings, classes=[]))
                for name, build in steps:
                    receipts.publish(state, "t1", name, build(source))
                records = receipts.load(state, "t1")
                problems = list(records.problems) + [p for a in receipts.attempts(records) for p in a.problems]
                self.assertTrue(problems, label)
                self.assertTrue(any(expected[label] in problem for problem in problems), (label, problems))

    def test_empty_history_digest_is_explicit(self):
        self.assertEqual(receipts.history_digest([]), "sha256:" + hashlib.sha256(b"[]").hexdigest())
        items = [{"kind": "receipt", "name": "R1.json", "digest": "sha256:" + "1" * 64}]
        canonical = json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(receipts.history_digest(items), "sha256:" + hashlib.sha256(canonical).hexdigest())
        self.assertNotEqual(receipts.history_digest(items), receipts.history_digest([]))



class SnapshotTest(unittest.TestCase):
    def repo_with_history(self) -> tuple[pathlib.Path, str, str]:
        repo = make_git_repo(self)
        base = git_commit(repo, {"README.md": "base\n"})
        head = git_commit(repo, {"src/app.py": "print('hi')\n"})
        return repo, base, head

    def test_snapshot_is_detached_head_without_remote(self):
        repo = make_git_repo(self)
        base = git_commit(repo, {"README.md": "base\n"})
        worktree = make_tmp_dir(self) / "wt"
        git(repo, "worktree", "add", "-q", "-b", "feat", str(worktree))
        head = git_commit(worktree, {"src/app.py": "print('hi')\n"})
        temp_root = make_tmp_dir(self)
        with snapshot.make(worktree, head, base=base, temp_root=temp_root) as snap:
            tree = snap.tree
            self.assertEqual(git(tree, "rev-parse", "HEAD"), head)
            detached = subprocess.run(["git", "-C", str(tree), "symbolic-ref", "-q", "HEAD"], capture_output=True)
            self.assertNotEqual(detached.returncode, 0)
            self.assertEqual(git(tree, "remote"), "")
            self.assertEqual(git(tree, "cat-file", "-t", base), "commit")
            self.assertEqual((tree / "src" / "app.py").read_text(encoding="utf-8"), "print('hi')\n")
            self.assertFalse((tree / ".git" / "objects" / "info" / "alternates").exists())
            for source in (worktree, repo):
                self.assertFalse(tree.resolve().is_relative_to(source.resolve()))
            self.assertEqual(snap.stripped, ())
        self.assertEqual(list(temp_root.iterdir()), [])
        self.assertEqual(git(worktree, "status", "--porcelain"), "")

    def test_strip_is_case_insensitive_at_every_depth(self):
        repo = make_git_repo(self)
        base = git_commit(repo, {"README.md": "base\n"})
        kept = {"src/app.py": "a\n", "docs/agents-guide.md": "a\n", "CLAUDE.md.bak": "a\n",
                "notes/.claudeignore": "a\n"}
        removed = {"AGENTS.md": "a\n", "sub/agents.md": "a\n", "AGENTS.override.md": "a\n", "CLAUDE.md": "a\n",
                   "docs/Claude.local.md": "a\n", ".claude/settings.json": "{}\n", "pkg/.Codex/config.toml": "x = 1\n",
                   ".agents/skills/x/SKILL.md": "a\n"}
        head = git_commit(repo, {**kept, **removed})
        with snapshot.make(repo, head, base=base) as snap:
            self.assertEqual(list(snap.stripped), sorted(removed))
            for name in removed:
                self.assertFalse((snap.tree / name).exists(), name)
            for folder in (".claude", ".agents", "pkg/.Codex"):
                self.assertFalse((snap.tree / folder).exists(), folder)
            for name in kept:
                self.assertTrue((snap.tree / name).is_file(), name)
            self.assertEqual(git(snap.tree, "show", f"{head}:CLAUDE.md"), "a")
        self.assertEqual(snapshot.stripped([".claude", "x/.CLAUDE/y", "Agents.Override.md", "agents.md/x"]),
                         [".claude", "Agents.Override.md", "x/.CLAUDE/y"])

    def test_snapshot_removed_on_exception(self):
        repo, base, head = self.repo_with_history()
        temp_root = make_tmp_dir(self)
        for error in (RuntimeError, KeyboardInterrupt):
            with self.subTest(error=error.__name__):
                with self.assertRaises(error):
                    with snapshot.make(repo, head, base=base, temp_root=temp_root) as snap:
                        self.assertTrue(snap.tree.is_dir())
                        raise error("stop")
                self.assertEqual(list(temp_root.iterdir()), [])
        with self.assertRaises(ReviewError) as caught:
            with snapshot.make(repo, "f" * 40, base=base, temp_root=temp_root):
                self.fail("a missing head must not yield a snapshot")
        self.assertEqual(caught.exception.code, "OAH_REVIEW_SNAPSHOT_FAILED")
        self.assertEqual(list(temp_root.iterdir()), [])


class CanaryTest(unittest.TestCase):
    def test_canary_is_exclusive(self):
        tree = make_tmp_dir(self)
        value = canary.plant(tree)
        self.assertRegex(value, r"^[0-9a-f]{32}$")
        self.assertEqual((tree / ".oah-review-canary").read_bytes(), (value + "\n").encode("ascii"))
        with self.assertRaises(ReviewError) as caught:
            canary.plant(tree)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_CANARY_CONFLICT")
        self.assertEqual((tree / ".oah-review-canary").read_bytes(), (value + "\n").encode("ascii"))
        self.assertNotEqual(canary.plant(make_tmp_dir(self)), value)

    def test_tracked_canary_refuses(self):
        repo = make_git_repo(self)
        base = git_commit(repo, {"README.md": "base\n"})
        head = git_commit(repo, {".oah-review-canary": "planted\n"})
        with snapshot.make(repo, head, base=base) as snap:
            with self.assertRaises(ReviewError) as caught:
                canary.plant(snap.tree)
            self.assertEqual(caught.exception.code, "OAH_REVIEW_CANARY_CONFLICT")
            self.assertEqual((snap.tree / ".oah-review-canary").read_text(encoding="utf-8"), "planted\n")

    def test_canary_line_must_match_exactly(self):
        value = "0123456789abcdef0123456789abcdef"
        for evidence in (f"REVIEWED-TREE-CANARY: {value}", f"ran cat\r\n  REVIEWED-TREE-CANARY: {value}  \r\nmore"):
            with self.subTest(evidence=evidence):
                self.assertTrue(canary.verified(evidence, value))
        for evidence in (f"REVIEWED-TREE-CANARY: {value}0", f"see REVIEWED-TREE-CANARY: {value}",
                         f"REVIEWED-TREE-CANARY:{value}", "", f"reviewed-tree-canary: {value}", value):
            with self.subTest(evidence=evidence):
                self.assertFalse(canary.verified(evidence, value))
        self.assertFalse(canary.verified("REVIEWED-TREE-CANARY: ", ""))
        self.assertIn("token file", canary.LIMIT)
        self.assertIn("tree identity", canary.LIMIT)


def finding(number: int, k: int = 1, *, severity: str = "P1", class_id: str | None = None, refs: list | None = None,
            **fields) -> dict:
    item = {"finding_id": f"R{number}-F{k}", "class_id": class_id or f"R{number}-C{k}", "severity": severity,
            "axis": "quality", "title": "a title", "detail": "a detail", "location": "src/app.py:1",
            "in_scope": True, "fix_kind": "local", "prior_refs": refs or []}
    item.update(fields)
    return item


def output(number: int = 1, *, verdict: str = "approve", findings: list | None = None, classes: list | None = None,
           **fields) -> dict:
    findings = findings or []
    if classes is None:
        classes = [{"class_id": item["class_id"], "mechanism": "a mechanism", "rule": "a rule"}
                   for item in findings if item["class_id"].startswith(f"R{number}-C")]
    data = {"verdict": verdict, "base": SHA_A, "head": SHA_B, "summary": "s", "findings": findings,
            "classes": classes, "intent_return": "INTENT x@r1 sha256:" + "0" * 64, "evidence": "e",
            "db_impact": "db-impact: not touched"}
    data.update(fields)
    return data


def encoded(data: object) -> bytes:
    return json.dumps(data).encode("utf-8")


class SchemaTest(unittest.TestCase):
    def test_schema_file_is_strict(self):
        loaded = json.loads(schema.text())
        self.assertEqual(loaded, schema.SCHEMA)
        objects = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    objects.append(node)
                for key in ("$ref", "pattern", "format", "oneOf", "allOf", "patternProperties"):
                    self.assertNotIn(key, node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(loaded)
        self.assertGreaterEqual(len(objects), 4)
        for node in objects:
            self.assertIs(node["additionalProperties"], False)
            self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
        self.assertEqual(sorted(loaded["properties"]), sorted(
            ["verdict", "base", "head", "summary", "findings", "classes", "intent_return", "evidence", "db_impact"]))

    def test_parse_accepts_a_valid_output(self):
        parsed = schema.parse(encoded(output(2, verdict="request_changes", findings=[
            finding(2), finding(2, 2, severity="P3", class_id="R1-C1", refs=[
                {"round": 1, "receipt_digest": "sha256:" + "1" * 64, "finding_id": "R1-F1", "class_id": "R1-C1"}])])),
            number=2)
        self.assertEqual(parsed.verdict, "request_changes")
        self.assertEqual([item.finding_id for item in parsed.blocking()], ["R2-F1"])
        self.assertEqual(parsed.findings[1].prior_refs[0].class_id, "R1-C1")
        self.assertEqual([item.class_id for item in parsed.classes], ["R2-C1"])
        self.assertEqual(parsed.findings[0].to_json(), finding(2))

    def test_parse_rejects_shape_errors(self):
        good = output(1, verdict="request_changes", findings=[finding(1)])
        cases = {
            "not json": b"{not json",
            "not utf-8": b"\xff\xfe{}",
            "array": encoded([good]),
            "missing field": encoded({key: value for key, value in good.items() if key != "evidence"}),
            "extra field": encoded(dict(good, extra=1)),
            "wrong type": encoded(dict(good, findings=[dict(finding(1), in_scope="yes")])),
            "bad enum": encoded(dict(good, findings=[dict(finding(1), severity="P4")])),
            "nested extra": encoded(dict(good, findings=[finding(1, refs=[
                {"round": 1, "receipt_digest": "d", "finding_id": "f", "class_id": "c", "note": "x"}])])),
            "boolean round": encoded(dict(good, findings=[finding(1, refs=[
                {"round": True, "receipt_digest": "d", "finding_id": "f", "class_id": "c"}])])),
            "string round": encoded(dict(good, findings=[finding(1, refs=[
                {"round": "1", "receipt_digest": "d", "finding_id": "f", "class_id": "c"}])])),
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(schema.OutputInvalid) as caught:
                    schema.parse(raw, number=1)
                self.assertEqual(caught.exception.code, "OAH_REVIEW_OUTPUT_INVALID")
                self.assertTrue(caught.exception.problems)

    def test_verdict_findings_consistency(self):
        bad = {
            "approve with P1": output(1, findings=[finding(1)]),
            "approve with P0": output(1, findings=[finding(1, severity="P0")]),
            "changes without blocking": output(1, verdict="request_changes", findings=[finding(1, severity="P2")]),
            "changes without findings": output(1, verdict="request_changes"),
        }
        for label, data in bad.items():
            with self.subTest(label=label):
                with self.assertRaises(schema.OutputInvalid) as caught:
                    schema.parse(encoded(data), number=1)
                self.assertTrue(any("verdict" in problem for problem in caught.exception.problems), label)
        parsed = schema.parse(encoded(output(1, findings=[finding(1, severity="P2"), finding(1, 2, severity="P3")])),
                              number=1)
        self.assertEqual(parsed.blocking(), [])

    def test_finding_ids_unique_and_round_prefixed(self):
        cases = {
            "duplicate id": [finding(3), finding(3, class_id="R3-C2")],
            "other round": [finding(2, class_id="R3-C1")],
            "zero": [dict(finding(3), finding_id="R3-F0")],
            "lower case": [dict(finding(3), finding_id="r3-f1")],
        }
        for label, findings in cases.items():
            with self.subTest(label=label):
                classes = [{"class_id": c, "mechanism": "m", "rule": "r"}
                           for c in sorted({f["class_id"] for f in findings})]
                with self.assertRaises(schema.OutputInvalid) as caught:
                    schema.parse(encoded(output(3, verdict="request_changes", findings=findings, classes=classes)),
                                 number=3)
                self.assertTrue(any("finding" in problem for problem in caught.exception.problems), label)
        class_cases = {
            "class defined twice": [{"class_id": "R3-C1", "mechanism": "m", "rule": "r"}] * 2,
            "new class from another round": [{"class_id": "R3-C1", "mechanism": "m", "rule": "r"},
                                             {"class_id": "R2-C9", "mechanism": "m", "rule": "r"}],
            "empty mechanism": [{"class_id": "R3-C1", "mechanism": " ", "rule": "r"}],
            "empty rule": [{"class_id": "R3-C1", "mechanism": "m", "rule": ""}],
        }
        for label, classes in class_cases.items():
            with self.subTest(label=label):
                with self.assertRaises(schema.OutputInvalid) as caught:
                    schema.parse(encoded(output(3, verdict="request_changes", findings=[finding(3)], classes=classes)),
                                 number=3)
                self.assertTrue(any("class" in problem for problem in caught.exception.problems), label)


class AuthorsTest(unittest.TestCase):
    def test_engine_from_trailers(self):
        repo = make_git_repo(self)
        base = git_commit(repo, {"README.md": "base\n"})
        claude_line = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
        messages = {
            "claude": "work\n\nAgent-Engine: claude",
            "codex": "work\n\nAgent-Engine: Codex",
            "human": "work\n\nagent-engine: human",
            "co-author": f"work\n\n{claude_line}",
            "codex over co-author": f"fix\n\nAgent-Engine: codex\n{claude_line}",
            "unknown value": "work\n\nAgent-Engine: gpt",
            "two values": "work\n\nAgent-Engine: claude\nAgent-Engine: codex",
            "none": "work",
            "other co-author": "work\n\nCo-Authored-By: Claudette <someone@example.com>",
            "not a trailer": "work\n\nAgent-Engine: codex\n\nthe last paragraph is prose",
        }
        expected = {"claude": "claude", "codex": "codex", "human": "human", "co-author": "claude",
                    "codex over co-author": "codex", "unknown value": "unknown", "two values": "unknown",
                    "none": "unknown", "other co-author": "unknown", "not a trailer": "unknown"}
        made = {}
        for number, (label, message) in enumerate(messages.items()):
            made[git_commit(repo, {f"f{number}.txt": label}, message=message)] = label
        head = git(repo, "rev-parse", "HEAD")
        found = authors.commit_engines(repo, base, head)
        self.assertEqual(len(found), len(messages))
        for sha, engine in found:
            with self.subTest(label=made[sha]):
                self.assertEqual(engine, expected[made[sha]])

    def test_independence_labels(self):
        cases = [(["claude", "claude"], True), (["claude", "codex"], False), (["claude", "unknown"], "unknown"),
                 (["codex", "unknown"], False), (["human"], True), ([], "unknown")]
        for engines, label in cases:
            with self.subTest(engines=engines):
                self.assertEqual(authors.independence(engines, "codex"), label)


class ContextTest(unittest.TestCase):
    def history(self) -> tuple[pathlib.Path, dict]:
        """main: c1 c2 → feat: c3 c4; main then gains c5."""
        repo = make_git_repo(self)
        commits = {"c1": git_commit(repo, {"a.txt": "1\n"}), "c2": git_commit(repo, {"a.txt": "2\n"})}
        git(repo, "checkout", "-q", "-b", "feat")
        commits["c3"] = git_commit(repo, {"b.txt": "3\n"})
        commits["c4"] = git_commit(repo, {"b.txt": "4\n"})
        git(repo, "checkout", "-q", "main")
        commits["c5"] = git_commit(repo, {"a.txt": "5\n"})
        git(repo, "checkout", "-q", "feat")
        return repo, commits

    def test_default_base_is_merge_base(self):
        repo, c = self.history()
        found = context.resolve(repo, None)
        self.assertEqual((found.base, found.head), (c["c2"], c["c4"]))
        self.assertIn("refs/heads/main", found.base_label)
        self.assertEqual(context.resolve(repo, c["c1"][:10]).base, c["c1"])
        remote = make_tmp_dir(self) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "origin", "main")
        git(repo, "fetch", "-q", "origin")
        git(repo, "remote", "set-head", "origin", "main")
        git(repo, "branch", "-D", "main")
        found = context.resolve(repo, None)
        self.assertEqual(found.base, c["c2"])
        self.assertIn("refs/remotes/origin/main", found.base_label)
        (repo / ".oah").mkdir()
        for label, base in (("not an ancestor", c["c5"]), ("not a commit", "no-such-rev"), ("option", "--all")):
            with self.subTest(label=label):
                with self.assertRaises(ReviewError) as caught:
                    context.resolve(repo, base)
                self.assertEqual(caught.exception.code, "OAH_REVIEW_BASE_INVALID")

    def test_nothing_to_review_and_missing_default_branch(self):
        repo, c = self.history()
        git(repo, "checkout", "-q", "main")
        with self.assertRaises(ReviewError) as caught:
            context.resolve(repo, None)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_NOTHING_TO_REVIEW")
        git(repo, "checkout", "-q", "-b", "work")
        git(repo, "branch", "-D", "main")
        with self.assertRaises(ReviewError) as caught:
            context.resolve(repo, None)
        self.assertEqual(caught.exception.code, "OAH_REVIEW_BASE_INVALID")
        self.assertIn("--base", str(caught.exception))
        self.assertEqual(context.resolve(repo, c["c1"], branch="feat").base, c["c1"])
        found = context.resolve(repo, None, branch="feat")
        self.assertEqual(found.base, c["c2"])

    def test_tracked_dirty_ignores_untracked_files(self):
        repo, _c = self.history()
        (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
        self.assertEqual(context.tracked_dirty(repo), [])
        (repo / "b.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual(context.tracked_dirty(repo), ["b.txt"])
        git(repo, "add", "new.txt")
        self.assertEqual(context.tracked_dirty(repo), ["b.txt", "new.txt"])

    def test_changes_diff_stat_and_critical_paths(self):
        repo = make_git_repo(self)
        base = git_commit(repo, {"README.md": "base\n"})
        head = git_commit(repo, {".github/workflows/ci.yml": "on: push\n", "db/migrations/002.sql": "select 1;\n",
                                 "docs/Claude.md": "x\n", "src/app.py": "print(1)\n"})
        changed = context.changed_files(repo, base, head)
        self.assertEqual(changed, [".github/workflows/ci.yml", "db/migrations/002.sql", "docs/Claude.md", "src/app.py"])
        self.assertIn("4 files changed", context.diff_stat(repo, base, head))
        self.assertEqual(context.critical(changed, settings.DEFAULTS["critical_paths"]),
                         [".github/workflows/ci.yml", "db/migrations/002.sql", "docs/Claude.md"])

    def test_db_impact_line_for_touched_entities(self):
        from oah.impact import mapstore
        from oah.impact.defaults import configured

        repo = make_git_repo(self)
        base = git_commit(repo, {"db/migrations/001_init.sql": "CREATE TABLE widgets (id int);\n",
                                 "src/app.py": "print('start')\n", "README.md": "x\n"})
        head = git_commit(repo, {"src/app.py": "print('start')\nrows = query('select id from widgets')\n"})
        env = dict(os.environ, OAH_STATE_DIR=str(make_tmp_dir(self) / "state"))
        mapstore.ensure_fresh(repo, env=env)
        _info, data = mapstore.fresh_map(repo, refresh=False, env=env)
        found = context.db_impact(context.changed_files(repo, base, head), data, head, configured(repo))
        self.assertEqual(found.entities, ("db_table:widgets",))
        self.assertEqual(found.query, "oah impact db_table:widgets")
        self.assertRegex(found.line, rf"^db-impact: commit={head} graph=[0-9a-f]{{64}} output=[0-9a-f]{{64}}$")
        self.assertTrue(any("src/app.py" in row for row in found.rows), found.rows)
        untouched = context.db_impact(["README.md"], data, head, configured(repo))
        self.assertEqual((untouched.line, untouched.query, untouched.entities), (context.NOT_TOUCHED, None, ()))
        plain = make_git_repo(self)
        git_commit(plain, {"README.md": "x\n"})
        mapstore.ensure_fresh(plain, env=env)
        _info, empty = mapstore.fresh_map(plain, refresh=False, env=env)
        self.assertEqual(context.db_impact(["README.md"], empty, head, configured(plain)).line,
                         "db-impact: unavailable (no SQL migrations found)")


class PromptTest(unittest.TestCase):
    def context(self, **fields) -> prompt.PromptContext:
        values = dict(task="t1", number=3, series_round=2, base=SHA_A, head=SHA_B, scope=("src", "tests"),
                      conditions=(("AC1", "the report lists every file"), ("AC2", "errors say what to do")),
                      diff_stat=" src/app.py | 2 +-\n 1 file changed", critical=(".github/workflows/ci.yml",),
                      stripped=("AGENTS.md", ".claude/settings.json"),
                      db=context.DbImpact(line="db-impact: commit=x graph=y output=z", query="oah impact db_table:w",
                                          entities=("db_table:w",), rows=("link db db/1.sql:1 db_table:w create",)),
                      history=(prompt.HistoryItem("receipt", "R1.json", "sha256:" + "1" * 64, '{"round": 1}\n'),
                               prompt.HistoryItem("attempt-start", "A1-start.json", "sha256:" + "2" * 64, "{}\n")),
                      history_digest="sha256:" + "3" * 64, attempted=(("R1-C1", "A1", "applied"),),
                      decision=None, extra_round=False)
        values.update(fields)
        return prompt.PromptContext(**values)

    def test_prompt_parts_and_exclusions(self):
        import re

        text = prompt.round_instructions(self.context())
        for part in ("- Round: R3", f"- Base: {SHA_A}", f"- Head: {SHA_B}", f"git diff {SHA_A} {SHA_B}",
                     " src/app.py | 2 +-", ".github/workflows/ci.yml", "AGENTS.md", ".claude/settings.json",
                     f"git show {SHA_B}:<path>", "untrusted", "db-impact: commit=x graph=y output=z",
                     "oah impact db_table:w", "R1.json", "sha256:" + "1" * 64, "A1-start.json", "sha256:" + "3" * 64,
                     "R1-C1", "- AC1: the report lists every file", "- AC2: errors say what to do", "R3-F1", "R3-C1",
                     "REVIEWED-TREE-CANARY: <value>", ".oah-review-canary", "src, tests"):
            with self.subTest(part=part):
                self.assertIn(part, text)
        for axis in schema.AXES:
            self.assertIn(axis, text)
        self.assertIsNone(re.search(r"\boah(?:\.exe)?\s+(?:start|renew|finish)\b", text))
        self.assertEqual([line for line in text.splitlines() if line.startswith("db-impact:")],
                         ["db-impact: commit=x graph=y output=z"])
        first = prompt.round_instructions(self.context(number=1, series_round=1, history=(), attempted=(),
                                                       history_digest=receipts.history_digest([])))
        self.assertIn("explicitly empty", first)
        self.assertIn(receipts.history_digest([]), first)
        resumed = prompt.round_instructions(self.context(decision="fix the parser again", extra_round=True))
        self.assertIn("fix the parser again", resumed)
        self.assertIn("--extra-round", resumed)


class EnvTest(unittest.TestCase):
    def test_reviewer_env_keeps_only_the_allowlist(self):
        given = {"PATH": "p", "HOME": "h", "CODEX_HOME": "c", "https_proxy": "x", "LANG": "C.UTF-8",
                 "OPENAI_API_KEY": "k", "CODEX_API_KEY": "c2", "ANTHROPIC_API_KEY": "a", "GITHUB_TOKEN": "t",
                 "OAH_STATE_DIR": "s", "OAH_WORKER": "0", "PWD": "w", "CLAUDE_CONFIG_DIR": "d",
                 "OAH_PROBE_PRIVATE": "v"}
        kept = runner.reviewer_env(given)
        self.assertEqual(kept, {"PATH": "p", "HOME": "h", "CODEX_HOME": "c", "https_proxy": "x", "LANG": "C.UTF-8",
                                "OPENAI_API_KEY": "k", "CODEX_API_KEY": "c2"})

    @unittest.skipUnless(os.name == "nt", "Windows environment names ignore letter case")
    def test_reviewer_env_ignores_case_on_windows(self):
        kept = runner.reviewer_env({"Path": "p", "SystemRoot": "r", "UserProfile": "u", "OpenAI_Api_Key": "k"})
        self.assertEqual(kept, {"Path": "p", "SystemRoot": "r", "UserProfile": "u"})

    def test_reviewer_binary_is_the_script_after_an_interpreter(self):
        folder = make_tmp_dir(self)
        script = folder / "codex.js"
        script.write_bytes(b"console.log('codex')\n")
        native = folder / "codex"
        native.write_bytes(b"\x7fELF fake\n")
        expected = hashlib.sha256(script.read_bytes()).hexdigest()
        self.assertEqual(runner.reviewer_binary([sys.executable, str(script), "--flag"]),
                         (os.path.realpath(script), expected))
        self.assertEqual(runner.reviewer_binary([str(folder / "node.exe"), str(script)]),
                         (os.path.realpath(script), expected))
        self.assertEqual(runner.reviewer_binary([str(native), "--profile", "x"]),
                         (os.path.realpath(native), hashlib.sha256(native.read_bytes()).hexdigest()))
        with self.assertRaises(ReviewError) as caught:
            runner.reviewer_binary([str(folder / "missing")])
        self.assertEqual(caught.exception.code, "OAH_ENGINE_MISSING")

if __name__ == "__main__":
    unittest.main()
