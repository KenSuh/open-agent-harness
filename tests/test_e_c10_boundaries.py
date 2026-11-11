# E 명령 경계 회귀 시험 — 각 fixture의 준비·원본·중계 기록을 바꾸고 실제 CLI 결과와 보존 상태를 확인한다.
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import errno
import io
import json
import os
import pathlib
import unittest
from unittest import mock

from tests import e_c6_fixture, e_c9_fixture
from tests import test_capabilities as capability_fixture
from tests import test_e_relay_times as relay_fixture
from tests import test_e_spent_evidence as spent_fixture
from tests.test_skill_delivery import DeliveryTask


def cli_result(root: pathlib.Path, env: dict[str, str], *args: str) -> tuple[int, str, str]:
    from oah import cli

    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), contextlib.chdir(root), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def files(root: pathlib.Path, *, ignore: frozenset[pathlib.Path] = frozenset()) -> dict[pathlib.Path, bytes]:
    return {name: path.read_bytes() for path in root.rglob("*") if path.is_file()
            and (name := path.relative_to(root)) not in ignore}


class CallBoundaryTest(DeliveryTask, unittest.TestCase):
    request = relay_fixture.RelayTimesTest.request
    offered_parent_relay = spent_fixture.SpentEvidenceTest.offered_parent_relay

    def test_settle_reports_stale_for_missing_or_invalid_offered_evidence(self):
        _now, run, command = self.offered_parent_relay()
        skills = json.loads((run / "skills.json").read_bytes())
        relay, = skills["prepared_e"]["facts"]["relay_results"]
        skill_state = self.state_dir / "skills"
        blob = skill_state / "relay-payloads" / (relay["payload"]["blob_ref"][7:] + ".json")
        record = skill_state / "relays" / (relay["id"][7:] + ".json")
        for path, replacement in (
            (blob, None), (record, None), (record, b"{}"),
            (run / "skills.json", b"[]"), (run / "skills.json", b"{"),
            (run / "result.json", None), (run / "capabilities.json", None),
        ):
            with self.subTest(file=path.name, replacement=replacement):
                earlier = path.read_bytes()
                if replacement is None:
                    path.unlink()
                else:
                    path.write_bytes(replacement)
                try:
                    before = files(self.state_dir)

                    result = self.oah(*command)

                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertTrue(result.stderr.startswith("OAH_NATIVE_OFFER_STALE:"), result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(self.record.exists())
                    self.assertEqual(files(self.state_dir), before)
                finally:
                    path.write_bytes(earlier)

    def test_relay_ingest_refuses_short_digest_before_either_store_write(self):
        request, _parent, _now = self.request()
        source = capability_fixture.write(self.work / "request.json", request)
        result = capability_fixture.write(self.work / "relay.txt", capability_fixture.RESULT)
        invocation = capability_fixture.write(self.work / "invocation.json", {
            "outcome": "ok", "at": request["created_at"], "arguments_digest": "sha256:abc",
        })
        before = files(self.state_dir)

        refused = self.oah("capabilities", "relay", "--request", str(source), "--capability",
                           request["capability_id"], "--result-file", str(result), "--invocation", str(invocation))

        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertTrue(refused.stderr.startswith("OAH_CAPABILITY_RELAY_REQUIRED(invalid):"), refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual(files(self.state_dir), before)

    def test_call_and_native_offer_report_typed_admission_refusals(self):
        from oah import call, scoring
        from oah.capabilities import plan
        from oah.discovery import budget, tasks
        from oah.discovery.target_profiles import DATA

        request, parent, now = self.request()
        record, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        valid = ("--parent-inventory", str(parent), "--relay-result", record["id"])
        needs = capability_fixture.write(self.work / "needs.json", {})
        index = tasks._index_path(self.state_dir)
        agent = self.work / "home/.claude/agents/oah-low.md"
        diagnostics = frozenset(map(pathlib.Path, ("reviews/t1/.locks/task.lock", "skills-telemetry.jsonl")))
        cases = (
            ("capability", (), 1, "OAH_CAPABILITY_UNAVAILABLE(not-configured)"),
            ("budget", valid, 1, "OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown)"),
            ("needs", ("--skill-needs", str(needs)), 1, "OAH_SKILL_NEEDS_INVALID(malformed)"),
            ("store", (), 69, "OAH_SKILL_STATE_CORRUPT"),
            ("target", valid, 1, "OAH_MODEL_UNRESOLVED"),
            ("storage", valid, 69, "OAH_STATE_IO"),
        )
        for launcher in ("process", "native"):
            for kind, flags, expected, diagnostic in cases:
                with self.subTest(launcher=launcher, error=kind), contextlib.ExitStack() as fixture:
                    if kind == "store":
                        capability_fixture.write(index, [])
                        fixture.callback(index.unlink)
                    elif kind == "budget":
                        fixture.enter_context(mock.patch.object(
                            budget, "_profile_source", return_value=budget.PinnedSource(DATA, budget.SOURCE_PIN)))
                    elif kind == "target":
                        if launcher == "native":
                            earlier = agent.read_bytes()
                            agent.unlink()
                            fixture.callback(agent.write_bytes, earlier)
                            diagnostic = "OAH_SKILL_HOST_UNSUPPORTED"
                        else:
                            fixture.enter_context(mock.patch.object(call, "_e_target", side_effect=scoring.FloorUnmet(
                                "OAH_MODEL_UNRESOLVED", "no configured target met the fixture's floor")))
                    elif kind == "storage":
                        target = agent if launcher == "native" else self.work / "fake_stream_claude.py"
                        original_open = pathlib.Path.open

                        def open_target(path, *args, **kwargs):
                            if path == target:
                                raise OSError(errno.EIO, "fixture target could not be read")
                            return original_open(path, *args, **kwargs)

                        fixture.enter_context(mock.patch.object(pathlib.Path, "open", open_target))
                    before = files(self.state_dir, ignore=diagnostics)

                    rc, out, err = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial", "--task",
                                              "t1", "--tier", "low", "--launcher", launcher, *flags, "--json")

                    self.assertEqual(rc, expected, out + err)
                    self.assertIn(diagnostic + ":", err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(self.runs(), [])
                    self.assertFalse(self.record.exists())
                    self.assertEqual(files(self.state_dir, ignore=diagnostics), before)


class FixBoundaryTest(e_c9_fixture.CorrectionFixture):
    def publish_record(self, project, original, saved) -> None:
        from oah.discovery import original as lineage, store

        record = saved["prepared_e"]
        e_c9_fixture.reseal(record)
        original.run.file_path("skills.json").write_text(json.dumps(saved), encoding="utf-8")
        _, pointer = store.put_record(project.state, lineage.FOLDER,
                                      {"run_id": original.run_id, "digest": record["digest"]})
        capability_fixture.write(lineage._index(project.state, "t1"), {"original": pointer["id"]})

    def check_refusal(self, project, code: str, *, exit_code: int = 1, message: str = "") -> None:
        before, calls = files(project.state), project.fix_calls()

        actual, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

        self.assertEqual((actual, result["code"]), (exit_code, code), str(result) + err)
        if message:
            self.assertIn(message, result["message"])
        self.assertNotIn("Traceback", err)
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(files(project.state), before)

    def test_fix_refuses_out_of_range_utc_fields_with_the_pointer_republished(self):
        from oah.discovery import preparation, store

        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        saved = json.loads(original.run.file_path("skills.json").read_bytes())
        for keys in (("expires_at",), ("facts", "lease", "expires_at"),
                     ("profile", "observed_at"), ("profile", "expires_at")):
            with self.subTest(field=".".join(keys)):
                changed = copy.deepcopy(saved)
                record = changed["prepared_e"]
                field = record
                for key in keys[:-1]:
                    field = field[key]
                field[keys[-1]] = "0001-01-01T00:00:00+01:00"
                if keys[0] == "profile":
                    record["profile"] = store.with_id(record["profile"])
                    record["facts"]["profile"] = store.digest(record["profile"])
                self.publish_record(project, original, changed)

                self.check_refusal(project, "OAH_FIX_STATE_CHANGED")
                with self.assertRaisesRegex(ValueError, "representable in UTC"):
                    preparation.parse(record)

    def test_fix_validates_fields_with_the_pointer_republished(self):
        from oah.capabilities import inventory
        from oah.discovery import preparation

        project, original, _now, _parent, _relay = self.selected_parent(relay=True)
        saved = json.loads(original.run.file_path("skills.json").read_bytes())
        for label, detail in (
            ("expires_at", "UTC offset"),
            ("parent.observed_at", "UTC offset"),
            ("inventory.parent", "parent inventory is incomplete"),
            ("operation.kind", "task-bearing dispatch operation"),
        ):
            with self.subTest(field=label):
                changed = copy.deepcopy(saved)
                record = changed["prepared_e"]
                facts = record["facts"]
                if label == "expires_at":
                    record["expires_at"] = dt.datetime.fromisoformat(record["expires_at"]).replace(tzinfo=None).isoformat()
                elif label == "parent.observed_at":
                    parent = facts["inventory"]["parent"]
                    parent["observed_at"] = dt.datetime.fromisoformat(parent["observed_at"]).replace(tzinfo=None).isoformat()
                    parent["digest"] = inventory.inventory_digest(parent)
                elif label == "inventory.parent":
                    facts["inventory"]["parent"] = []
                else:
                    facts["operation"]["kind"] = "public-explore"
                self.publish_record(project, original, changed)

                self.check_refusal(project, "OAH_FIX_STATE_CHANGED")
                with self.assertRaisesRegex(ValueError, detail):
                    preparation.parse(record)

    def test_fix_refuses_a_directory_in_place_of_pointed_skills(self):
        project, original, _need, _basis = e_c6_fixture.selected_ordinary(self)
        skills = original.run.file_path("skills.json")
        skills.unlink()
        skills.mkdir()

        self.check_refusal(project, "OAH_FIX_STATE_CHANGED")
        self.assertTrue(skills.is_dir())

    def test_first_correction_keeps_its_capability_or_target_admission_refusal(self):
        from oah.discovery import budget
        from oah.discovery.target_profiles import DATA

        make_skill, make_link = e_c9_fixture.skill_md, e_c9_fixture.link_op
        for kind, expected, remedy in (
            ("capability", "OAH_CAPABILITY_UNAVAILABLE(not-configured)", "make it available and prepare a new initial"),
            ("capacity", "OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown)", "record a qualified route with evidence"),
        ):
            with self.subTest(admission=kind):
                self.upstreams.pop("acme/correction", None)
                with contextlib.ExitStack() as fixture:
                    if kind == "capability":
                        fixture.enter_context(mock.patch.object(e_c9_fixture, "skill_md",
                            side_effect=lambda *a, **kw: make_skill(*a, **kw, extra="allowed-tools: mcp__docs__lookup\n")))
                        fixture.enter_context(mock.patch.object(e_c9_fixture, "link_op",
                            side_effect=lambda *a, **kw: make_link(*a, **dict(kw, required=True))))
                    project, original = self.correction_only()
                saved = json.loads(original.run.file_path("skills.json").read_bytes())
                self.assertEqual(saved["prepared_e"]["profile"], {})
                self.assertNotIn("bundle", saved)
                override = (mock.patch.object(budget, "_profile_source",
                    return_value=budget.PinnedSource(DATA, budget.SOURCE_PIN)) if kind == "capacity"
                    else contextlib.nullcontext())

                with override:
                    self.check_refusal(project, expected, message=remedy)

    def test_fix_keeps_capability_plan_after_freeze(self):
        from oah import call

        project, original, now, _parent, _relay = self.selected_parent(relay=True)
        cap = original.run.file_path("capabilities.json")
        before = files(original.run.path)
        del before[pathlib.Path(cap.name)]
        calls, freeze = len(project.fix_calls()), call._freeze_e

        def remove_after_freeze(*args, **kwargs):
            freeze(*args, **kwargs)
            cap.unlink()

        with mock.patch.object(call, "_freeze_e", remove_after_freeze):
            code, result, err = self.correct(project, now)

        self.assertEqual((code, result["code"]), (0, "OAH_FIX_APPLIED"), str(result) + err)
        self.assertEqual(len(project.fix_calls()), calls + 1)
        self.assertIn(capability_fixture.RESULT.rstrip(), project.fix_calls()[-1]["prompt"])
        self.assertEqual(files(original.run.path), before)

    def test_fix_checks_capability_record_at_freeze_and_preserves_storage_error(self):
        project, original, _now, _parent, _relay = self.selected_parent(relay=True)
        cap = original.run.file_path("capabilities.json")
        before, read = cap.read_bytes(), pathlib.Path.read_bytes
        cap.unlink()
        self.check_refusal(project, "OAH_FIX_STATE_CHANGED")
        cap.write_bytes(b"not JSON")
        self.check_refusal(project, "OAH_FIX_STATE_CHANGED")
        cap.write_bytes(before)

        for error, exit_code, expected in (
            (PermissionError(errno.EACCES, "fixture cannot read the capability plan"), 1, "OAH_FIX_STATE_CHANGED"),
            (OSError(errno.EIO, "fixture storage failure"), 69, "OAH_STATE_IO"),
        ):
            with self.subTest(error=type(error).__name__):
                state, calls = files(project.state), project.fix_calls()

                def read_capability(path, fault=error):
                    if path == cap:
                        raise fault
                    return read(path)

                with mock.patch.object(pathlib.Path, "read_bytes", read_capability):
                    code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

                self.assertEqual((code, result["code"]), (exit_code, expected), str(result) + err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(project.fix_calls(), calls)
                self.assertEqual(files(project.state), state)

    def prepare_ordinary_after_managed(self, *, new_lease: bool, restore_selection: bool):
        from oah.discovery import tasks
        from oah.lease import Store
        from oah.paths import repo_info

        project, earlier, _need, _basis = e_c6_fixture.selected_ordinary(self)
        kept = files(earlier.run.path)
        index = tasks._index_path(project.state)
        selected = None
        if new_lease:
            with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo):
                store = Store(project.state)
                store.release(task="t1", agent="claude", reason="restart", force=True)
                project.lease = store.start(task="t1", agent="claude", scope=("src", ".oah/tasks/t1"), note="again",
                    ttl_hours=1, info=repo_info(project.repo), intent=project.binding)
        else:
            selected = index.read_bytes()
            index.unlink()
        project.fix_steps.clear()
        project.queue_fix({}, {}, {"write": {"src/app.py": "print('corrected')\n"}})

        rc, out, err = cli_result(project.repo, project.env, "call", "codex", "--stage", "initial", "--task", "t1",
                                  "--tier", "high", "--override-stage", "--no-rerank", "--json")
        self.assertEqual(rc, 0, out + err)
        run = project.state / "runs/t1" / json.loads(out)["run_id"]
        self.assertNotIn("prepared_e", json.loads((run / "skills.json").read_bytes()))
        self.assertEqual(json.loads((run / "result.json").read_bytes())["status"], "finished")
        if restore_selection and selected is not None:
            index.write_bytes(selected)
        return project, earlier, kept, run

    def check_ordinary_correction(self, *, new_lease: bool) -> None:
        from oah.discovery import original

        project, earlier, kept, ordinary = self.prepare_ordinary_after_managed(new_lease=new_lease,
                                                                              restore_selection=False)
        latest, calls = files(ordinary), len(project.fix_calls())

        rc, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

        self.assertEqual((rc, result["code"]), (0, "OAH_FIX_APPLIED"), str(result) + err)
        self.assertEqual(len(project.fix_calls()), calls + 1)
        self.assertNotIn("Prepare the selected ledger rows.", project.fix_calls()[-1]["prompt"])
        self.assertIsNone(original.resolve(project.state, "t1"))
        self.assertEqual(files(earlier.run.path), kept)
        self.assertEqual(files(ordinary), latest)

    def test_ordinary_latest_original_allows_ordinary_correction(self):
        self.check_ordinary_correction(new_lease=False)

    def test_ordinary_latest_original_under_new_lease_allows_correction(self):
        self.check_ordinary_correction(new_lease=True)

    def test_ordinary_latest_original_refuses_when_managed_content_is_needed_again(self):
        from oah.discovery import original

        project, earlier, kept, _ordinary = self.prepare_ordinary_after_managed(new_lease=False, restore_selection=True)

        self.check_refusal(project, "OAH_FIX_STATE_CHANGED")

        self.assertIsNone(original.resolve(project.state, "t1"))
        self.assertEqual(files(earlier.run.path), kept)
