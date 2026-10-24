# oah review 부품 검사 — 설정, 불변 기록 저장소와 수정 시도 읽기, 스냅샷·카나리, 출력 스키마, 작성 엔진 판정, 맥락과 프롬프트를 실제 git 저장소로 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import unittest

from oah.config import ConfigError
from oah.review import ReviewError, canary, receipts, settings, snapshot
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

if __name__ == "__main__":
    unittest.main()
