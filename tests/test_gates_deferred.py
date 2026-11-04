# 게이트 CI 위임 검사 — ci-check: ABANDON 줄의 파싱과 이름 오류, oah gates status 표시, Stop 훅 인계, oah land 가 정확한 HEAD 의 지정 체크 성공으로만 닫고 그 밖에는 OAH_LAND_GATES_DEFERRED 로 거부하며 기록에 이름을 남기지 않음을 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import unittest
from unittest.mock import patch

from oah import hook_stop, land, unlazy as unlazy_mod
from oah.gates import check as check_mod
from oah.gates import ledger_status, parse_text, structural_status
from oah.gates.bindings import BindingStore
from oah.lease import Store
from oah.paths import repo_info
from tests.fixtures.gates.support import StubNode, make_unlazy_dir, met_ledger, run_cli, stub_pin
from tests.helpers import git_commit, make_git_repo, make_tmp_dir
from tests.test_land import LandProject, describe
from tests.test_land_cli import land_command, records

FIRST, SECOND = "test (windows-latest, 1/2)", "test (windows-latest, 2/2)"
HIDDEN = "windows-latest"  # part of both names: no oah output or record may contain it
REASON = f'ci-check: "{FIRST}" "{SECOND}" — the Windows suite runs only in CI'
ROW = f"ABANDON: G2 {REASON}\n"
LEDGER = ("# Gates: t1\n\n- [ ] G1: the app exists\n  CHECK: oah check exists src/app.py\n  EXPECT: OAH_CHECK_OK\n"
          "  EVIDENCE: pending\n\n- [ ] G2: the suite passes on Windows\n  CHECK: python -m unittest\n  EXPECT: OK\n"
          "  EVIDENCE: pending\n\n")
ROW_LINE = LEDGER.count("\n") + 1
HANDOFF = "HANDOFF REQUIRED: 1 abandoned (met: 1)\n  GATES:G2\n"
NAMES_SHA256 = hashlib.sha256(json.dumps([FIRST, SECOND], ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()
CLOSED_ENTRY = {"position": 1, "digest": hashlib.sha256(b"G2").hexdigest(), "label": "G2", "ledger": 1,
                "checks": 2, "names_sha256": NAMES_SHA256}
MALFORMED = {"nothing after the prefix": "ci-check:", "bare words": "ci-check: windows suite",
             "unclosed": 'ci-check: "win', "second unclosed": 'ci-check: "win" "mac',
             "glued to text": 'ci-check: "win"mac', "glued names": 'ci-check: "win""mac"', "blank": 'ci-check: ""',
             "only spaces": 'ci-check: "  "', "control character": 'ci-check: "win\tmac"',
             "named twice": 'ci-check: "win" "win"'}


def run(name: str, conclusion: str | None = "success", **fields) -> dict:
    status = "completed" if conclusion is not None else "in_progress"
    return {"name": name, "status": status, "conclusion": conclusion, **fields}


class LedgerTest(unittest.TestCase):
    def test_a_ci_check_row_defers_the_gate_and_stays_an_abandon_row(self):
        ledger = parse_text(LEDGER + ROW)
        self.assertEqual(ledger.errors, ())
        self.assertEqual(ledger.deferred, {"G2": (FIRST, SECOND)})
        self.assertEqual(ledger.abandoned, {"G2": REASON})
        status = structural_status(parse_text(met_ledger(LEDGER + ROW, skip=("G2",))))
        self.assertEqual((status.verdict, status.abandoned), ("handoff", ("G2",)))

    def test_one_name_is_enough_and_free_text_is_optional(self):
        for row, names in (('ABANDON: G2 ci-check: "win"\n', ("win",)),
                           ('ABANDON: G2: ci-check:"win" "mac os"\n', ("win", "mac os")),
                           ('ABANDON: G2 ci-check: "win" see "the docs"\n', ("win",))):
            with self.subTest(row=row):
                ledger = parse_text(LEDGER + row)
                self.assertEqual((ledger.errors, ledger.deferred), ((), {"G2": names}))

    def test_other_reasons_are_plain_abandon_rows(self):
        for reason in ("the owner signs off", "ci-check pending", 'CI-check: "win"', 'see ci-check: "win"'):
            with self.subTest(reason=reason):
                ledger = parse_text(LEDGER + f"ABANDON: G2 {reason}\n")
                self.assertEqual((ledger.errors, ledger.deferred, ledger.abandoned), ((), {}, {"G2": reason}))

    def test_a_ci_check_reason_without_a_complete_quoted_name_is_a_named_error(self):
        for case, reason in MALFORMED.items():
            with self.subTest(case=case):
                ledger = parse_text(LEDGER + f"ABANDON: G2 {reason}\n")
                self.assertEqual(len(ledger.errors), 1, ledger.errors)
                self.assertTrue(ledger.errors[0].startswith(f"line {ROW_LINE}: ABANDON G2 ci-check: "), ledger.errors)
                self.assertEqual(ledger.deferred, {})
                self.assertEqual(structural_status(ledger).verdict, "invalid")


class GatesCommandTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/app.py": "print('v1')\n"})
        self.tmp = make_tmp_dir(self)
        self.stub = StubNode(self.tmp / "stub")
        self.unlazy = make_unlazy_dir(self.tmp / "unlazy")
        self.state = self.tmp / "state"
        self.ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        self.ledger.parent.mkdir(parents=True)

    def gates(self, *argv, unlazy=True):
        env = self.stub.env(OAH_UNLAZY_DIR=str(self.unlazy if unlazy else self.tmp / "absent"),
                            OAH_STATE_DIR=str(self.state))
        return run_cli(["gates", *argv], cwd=self.repo, env=env, pin=stub_pin(self.unlazy))

    def test_status_shows_a_deferred_gate_with_the_number_of_named_checks(self):
        self.stub.reply("gate-check.mjs", {"stdout": HANDOFF, "code": 1})
        for row, line in ((ROW, "OAH_GATE_DEFERRED G2 checks=2: deferred to CI; oah land closes it only when all 2 "
                                "named check runs succeeded at the exact HEAD"),
                          ('ABANDON: G2 ci-check: "win"\n', "OAH_GATE_DEFERRED G2 checks=1: deferred to CI; oah land "
                                                            "closes it only when its named check run succeeded at the "
                                                            "exact HEAD")):
            self.ledger.write_text(met_ledger(LEDGER + row, skip=("G2",)), encoding="utf-8")
            for unlazy in (False, True):
                with self.subTest(row=row, unlazy=unlazy):
                    shown = self.gates("status", "t1", unlazy=unlazy)
                    self.assertEqual(shown.returncode, 1, shown.stdout + shown.stderr)
                    self.assertIn(line, shown.stdout.splitlines())
                    self.assertIn("OAH_GATES_STATUS verdict=handoff", shown.stdout)
                    self.assertNotIn(HIDDEN, shown.stdout + shown.stderr)
                    self.assertNotIn('"win"', shown.stdout + shown.stderr)

    def test_a_plain_abandon_row_shows_no_deferred_line(self):
        self.ledger.write_text(met_ledger(LEDGER + "ABANDON: G2 the owner signs off\n", skip=("G2",)),
                               encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"stdout": HANDOFF, "code": 1})
        for unlazy in (False, True):
            with self.subTest(unlazy=unlazy):
                shown = self.gates("status", "t1", unlazy=unlazy)
                self.assertIn("OAH_GATES_STATUS verdict=handoff", shown.stdout)
                self.assertNotIn("OAH_GATE_DEFERRED", shown.stdout)

    def test_status_and_check_name_a_malformed_ci_check_row(self):
        self.stub.reply("gate-check.mjs", {"stdout": "  PASS GATES:G1: x\nGATES.md: 2 gates\n" + HANDOFF, "code": 1},
                        {"stdout": HANDOFF, "code": 1})
        for case, reason in (("unclosed", MALFORMED["unclosed"]), ("bare words", MALFORMED["bare words"])):
            self.ledger.write_text(met_ledger(LEDGER + f"ABANDON: G2 {reason}\n", skip=("G2",)), encoding="utf-8")
            for unlazy in (False, True):
                with self.subTest(case=case, unlazy=unlazy):
                    shown = self.gates("status", "t1", unlazy=unlazy)
                    self.assertEqual(shown.returncode, 1)
                    self.assertIn(f"line {ROW_LINE}: ABANDON G2 ci-check: needs one or more check-run names",
                                  shown.stdout)
                    self.assertIn("OAH_GATES_STATUS verdict=invalid", shown.stdout)
                    self.assertNotIn("OAH_GATE_DEFERRED", shown.stdout)
            with self.subTest(case=case, command="check"):
                checked = self.gates("check", "t1")
                self.assertEqual(checked.returncode, 1, checked.stdout + checked.stderr)
                self.assertTrue(checked.stderr.startswith("OAH_GATES_INVALID"), checked.stderr)
                self.assertIn("ABANDON G2 ci-check:", checked.stderr)
        self.assertEqual(self.stub.calls("gate-check.mjs"), [])


class StopHookTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_git_repo(self)
        git_commit(self.repo, {"src/app.py": "print('v1')\n"})
        self.tmp = make_tmp_dir(self)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.stub = StubNode(self.tmp / "stub")
        self.unlazy = make_unlazy_dir(self.tmp / "unlazy")
        self.state = self.tmp / "state"
        self.ledger = self.repo / ".oah" / "tasks" / "t1" / "GATES.md"
        self.ledger.parent.mkdir(parents=True)
        Store(self.state).start(task="t1", agent="claude", scope=("src",), note="", ttl_hours=1,
                                info=repo_info(self.repo))

    def env(self):
        return self.stub.env(OAH_UNLAZY_DIR=str(self.unlazy), OAH_STATE_DIR=str(self.state))

    def decide(self):
        payload = {"session_id": "s1", "cwd": str(self.repo), "hook_event_name": "Stop", "stop_hook_active": False}
        with patch.object(unlazy_mod, "PIN", stub_pin(self.unlazy)):
            return hook_stop.decide(payload, env=self.env(), home=self.home)

    def test_a_deferred_gate_is_an_honest_handoff_until_landing(self):
        text = LEDGER + ROW
        self.ledger.write_text(text, encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"write": {str(self.ledger): met_ledger(text, skip=("G2",))},
                                           "stdout": "  PASS GATES:G1: x\n" + HANDOFF, "code": 1},
                        {"stdout": HANDOFF, "code": 1})
        with patch.object(unlazy_mod, "PIN", stub_pin(self.unlazy)):
            report = check_mod.run_check(self.repo, self.ledger, BindingStore(self.state, "t1"),
                                         unlazy_dir=self.unlazy, node=self.stub.node, env=self.env())
        self.assertEqual(report.actions, {"G1": "run", "G2": "abandoned"})
        decision = self.decide()
        self.assertNotIn("decision", decision)
        self.assertIn("HANDOFF REQUIRED: abandoned gates t1:G2", decision["systemMessage"])
        self.assertNotIn(HIDDEN, decision["systemMessage"])

    def test_a_malformed_ci_check_row_blocks_as_an_invalid_ledger(self):
        self.ledger.write_text(met_ledger(LEDGER, skip=("G2",)) + f"ABANDON: G2 {MALFORMED['unclosed']}\n",
                               encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"stdout": HANDOFF, "code": 1})
        decision = self.decide()
        self.assertEqual(decision["decision"], "block", decision)
        self.assertIn("invalid", decision["reason"])
        self.assertIn(f"line {ROW_LINE}: ABANDON G2 ci-check:", decision["reason"])


@unittest.skipUnless(os.environ.get("OAH_TEST_UNLAZY_DIR"), "set OAH_TEST_UNLAZY_DIR to probe a real unlazy")
class PinnedUnlazyTest(unittest.TestCase):
    def test_the_pinned_unlazy_reads_a_ci_check_row_as_an_abandoned_gate(self):
        from tests.helpers import require_node

        require_node(self)
        folder = pathlib.Path(os.environ["OAH_TEST_UNLAZY_DIR"])
        self.assertEqual([path for path, digest in unlazy_mod.PIN.files.items()
                          if unlazy_mod.file_digest(folder / path) != digest], [])
        node = unlazy_mod.resolve_node(os.environ)
        repo = make_tmp_dir(self) / "repo"
        repo.mkdir()
        ledger = repo / "GATES.md"
        ledger.write_text("# Gates: probe\n\n- [x] G1: the wording is reviewed\n  EVIDENCE: reviewed by the owner\n\n"
                          "- [ ] G2: the suite passes on Windows\n  EVIDENCE: pending\n\n" + ROW, encoding="utf-8")
        result = unlazy_mod.gate_check(folder, ledger, node=node, mode="status", root=repo)
        self.assertEqual(result.verdict, "handoff", result.stdout + result.stderr)
        status = ledger_status(ledger, unlazy=folder, node=node, root=repo)
        self.assertEqual((status.verdict, status.source, status.abandoned), ("handoff", "unlazy", ("G2",)))


class DeferredProject(LandProject):
    """A LandProject whose ledger defers G2 to two named Windows check runs; G1 passes through the stub unlazy."""

    def __init__(self, test: unittest.TestCase, *, row: str = ROW, **options) -> None:
        super().__init__(test, ledger=LEDGER + row, **options)

    def gates_check(self, unmet: tuple[str, ...] = ()) -> None:
        text = self.ledger.read_text(encoding="utf-8")
        self.stub.reply("gate-check.mjs", {"write": {str(self.ledger): met_ledger(text, skip=("G2",))},
                                           "stdout": "  PASS GATES:G1: x\nGATES.md: 2 gates\n" + HANDOFF, "code": 1},
                        {"stdout": "GATES.md: 2 gates\n" + HANDOFF, "code": 1})
        self.oah("gates", "check", "t1")


class LandTest(unittest.TestCase):
    def exported(self, result: land.Assessment, project: LandProject) -> str:
        land.record(project.state, result, "check", None)
        return describe(result) + json.dumps(result.to_json()) + json.dumps(records(project)[-1])

    def test_a_deferred_gate_closes_when_every_named_check_run_succeeded_at_head(self):
        project = DeferredProject(self)
        for runs in ([run("tests"), run(FIRST), run(SECOND)],
                     [run(FIRST), run(SECOND, "failure", id=10), run(SECOND, id=11)]):
            with self.subTest(runs=len(runs)):
                project.ci(runs)
                result = project.assess()
                self.assertTrue(result.ready, describe(result))
                self.assertEqual(result.code, "OAH_LAND_READY")
                ci, gates = result.check("ci"), result.check("gates")
                self.assertEqual(ci.state, "passed")
                self.assertEqual(ci.detail["deferred"], [{**CLOSED_ENTRY, "closed": True, "not_succeeded": 0}])
                self.assertIn("deferred gate(s) closed", ci.message)
                self.assertEqual(gates.detail["handoff"], [])
                self.assertEqual(gates.detail["deferred"], [CLOSED_ENTRY])
                self.assertIn("deferred to CI", gates.message)
                self.assertNotIn("abandoned (a handoff", gates.message)
                self.assertNotIn(HIDDEN, self.exported(result, project))
                self.assertEqual(records(project)[-1]["deferred_gates"], [CLOSED_ENTRY])
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertTrue(pushed.landed, pushed.message)
        self.assertEqual(project.remote_main(), project.head)

    def test_a_deferred_gate_refuses_unless_each_named_run_completed_as_success(self):
        project = DeferredProject(self)
        cases = {"missing": ([run(FIRST)], [], 1, None),
                 "pending": ([run(FIRST), run(SECOND, None)], [], 1, "OAH_LAND_CI_PENDING"),
                 "failed": ([run(FIRST), run(SECOND, "failure")], [], 1, "OAH_LAND_CI_FAILED"),
                 "skipped": ([run(FIRST), run(SECOND, "skipped")], [], 1, None),
                 "neutral": ([run(FIRST, "neutral"), run(SECOND, "neutral")], [], 2, None),
                 "rerun failed": ([run(FIRST), run(SECOND, id=10), run(SECOND, "failure", id=11)], [], 1,
                                  "OAH_LAND_CI_FAILED"),
                 "status only": ([run(FIRST)], [{"context": SECOND, "state": "success"}], 1, None),
                 "no results": ([], [], 2, "OAH_LAND_CI_UNVERIFIED")}
        for case, (runs, statuses, missing, itself) in cases.items():
            with self.subTest(case=case):
                project.ci(runs, statuses)
                result = project.assess()
                ci = result.check("ci")
                self.assertFalse(result.ready, describe(result))
                self.assertEqual((result.failure.code, ci.state, ci.code),
                                 ("OAH_LAND_GATES_DEFERRED", "failed", "OAH_LAND_GATES_DEFERRED"), describe(result))
                self.assertIn("gate position 1 G2 digest", ci.message)
                self.assertIn(f"(2 named check runs): {missing} of them did not succeed", ci.message)
                self.assertEqual(ci.detail["deferred"], [{**CLOSED_ENTRY, "closed": False, "not_succeeded": missing}])
                if itself:
                    self.assertIn(f"CI itself reports {itself}", ci.message)
                else:
                    self.assertNotIn("CI itself reports", ci.message)
                self.assertNotIn(HIDDEN, self.exported(result, project))
                self.assertNotIn("deferred_gates", records(project)[-1])

    def test_no_ci_exception_or_unreadable_ci_closes_a_deferred_gate(self):
        project = DeferredProject(self)
        exception = DeferredProject(self, config='[land]\nci_unverified = "exception"\n')
        gh = project.gh_argv
        cases = (("gh missing", project, {}), ("--accept-unverified-ci", project,
                                              {"accept_unverified_ci": "CI is not set up yet"}),
                 ("exception policy", exception, {}), ("incomplete reading", project, {}))
        for case, chosen, options in cases:
            with self.subTest(case=case):
                incomplete = case == "incomplete reading"
                chosen.gh_argv = gh if incomplete else None
                chosen.ci([run(FIRST), run(SECOND)], **({"total_count": 3} if incomplete else {}))
                result = chosen.assess(**options)
                ci = result.check("ci")
                self.assertFalse(result.ready, describe(result))
                self.assertEqual((result.code, ci.state), ("OAH_LAND_GATES_DEFERRED", "failed"))
                self.assertIsNone(result.ci_exception)
                self.assertIn("so no named check run is confirmed", ci.message)
                self.assertIn("2 of them did not succeed", ci.message)
                land.record(chosen.state, result, "check", None)
                self.assertFalse((chosen.state / "land" / "t1" / "ci-exceptions.jsonl").exists())
                self.assertNotIn("deferred_gates", records(chosen)[-1])

    def test_a_named_run_that_fails_after_the_check_stops_the_push(self):
        project = DeferredProject(self)
        project.ci([run(FIRST), run(SECOND)])
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        project.ci([run(FIRST), run(SECOND, "failure")])
        with project.patched():
            pushed = land.push(project.repo, result, env=dict(project.env))
        self.assertEqual((pushed.landed, pushed.code), (False, "OAH_LAND_GATES_DEFERRED"))
        self.assertEqual(project.remote_main(), project.base)

    def test_the_command_lands_and_refuses_without_showing_names_or_writing_gate_files(self):
        project = DeferredProject(self)
        summary = project.repo / ".oah" / "tasks" / "t1" / "GATES.summary.md"
        before = (project.ledger.read_bytes(), summary.read_bytes())
        project.ci([run(FIRST)])
        refused = land_command(project, "--check", "--task", "t1", "--json")
        body = json.loads(refused.stdout)
        self.assertEqual((refused.returncode, body["code"]), (1, "OAH_LAND_GATES_DEFERRED"))
        self.assertIn("G2", body["message"])
        self.assertIn("1 of them did not succeed", body["message"])
        project.ci([run(FIRST), run(SECOND)])
        landed = land_command(project, "--merge", "--task", "t1", "--confirm", project.head)
        self.assertEqual(landed.returncode, 0, landed.stdout + landed.stderr)
        self.assertIn("deferred gate(s) closed", landed.stdout)
        self.assertEqual(project.remote_main(), project.head)
        self.assertEqual(records(project)[-1]["deferred_gates"], [CLOSED_ENTRY])
        for shown in (refused, landed):
            self.assertNotIn(HIDDEN, shown.stdout + shown.stderr)
        self.assertNotIn(HIDDEN, (project.state / "land" / "t1" / "records.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((project.ledger.read_bytes(), summary.read_bytes()), before)

    def test_a_plain_abandon_row_stays_a_handoff_as_before(self):
        project = DeferredProject(self, row="ABANDON: G2 the owner signs off on Windows\n")
        project.ci([run("tests")])
        result = project.assess()
        self.assertTrue(result.ready, describe(result))
        gates, ci = result.check("gates"), result.check("ci")
        self.assertEqual([item["label"] for item in gates.detail["handoff"]], ["G2"])
        self.assertIn("abandoned (a handoff, not met): position 1 G2", gates.message)
        self.assertNotIn("deferred", gates.message + ci.message + json.dumps(gates.detail) + json.dumps(ci.detail))
        land.record(project.state, result, "check", None)
        self.assertNotIn("deferred_gates", records(project)[-1])

    def test_deferred_gates_of_several_ledgers_keep_their_ledger_positions(self):
        root = make_git_repo(self)
        for folder, row in (("a", "ABANDON: G2 the owner signs off\n"), ("b", ROW)):
            path = root / folder / "GATES.md"
            path.parent.mkdir()
            path.write_text(LEDGER + row, encoding="utf-8")
        found = land._deferred_gates(root, "t1", ["a/GATES.md", "b/GATES.md"])
        self.assertEqual([(item.gate, item.ledger, item.label, item.names) for item in found],
                         [("G2", 2, "b/GATES.md:G2", (FIRST, SECOND))])
        self.assertIn("gate position 1 G2 digest", land._deferred_text(found[0].entry(1), True))
        self.assertIn("in ledger 2 (2 named check runs)", land._deferred_text(found[0].entry(1), True))
        self.assertNotIn(HIDDEN, repr(found))


if __name__ == "__main__":
    unittest.main()
