from __future__ import annotations

import datetime as dt
import contextlib
import errno
import io
import json
import os
import pathlib
import stat
import types
import unittest
from unittest import mock

from tests import e_c9_fixture
from tests import test_capabilities as capability_fixture
from tests import test_e_relay_times as relay_fixture
from tests import test_e_spent_evidence as spent_fixture
from tests.test_e_c10_boundaries import cli_result, files
from tests.test_skill_delivery import DeliveryTask


def relay_paths(state, saved):
    relay, = saved["prepared_e"]["facts"]["relay_results"]
    return {"relay-record": state / "skills/relays" / (relay["id"][7:] + ".json"),
            "relay-blob": state / "skills/relay-payloads" / (relay["payload"]["blob_ref"][7:] + ".json")}


@contextlib.contextmanager
def read_fault(path, error):
    read, read_text, seen = pathlib.Path.read_bytes, pathlib.Path.read_text, []

    def changed(current):
        if current == path:
            seen.append("read_bytes")
            raise error
        return read(current)

    def changed_text(current, *args, **options):
        if current == path:
            seen.append("read_text")
            raise error
        return read_text(current, *args, **options)

    with mock.patch.object(pathlib.Path, "read_bytes", changed), \
            mock.patch.object(pathlib.Path, "read_text", changed_text):
        yield seen


@contextlib.contextmanager
def changed_file(path, kind):
    earlier = path.read_bytes()
    fault = contextlib.nullcontext()
    if kind == "missing":
        path.unlink()
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "invalid":
        path.write_bytes(b"[]")
    elif kind == "null":
        path.write_bytes(b"null")
    elif kind == "denied":
        fault = read_fault(path, PermissionError(errno.EACCES, "fixture read denied"))
    elif kind == "io":
        fault = read_fault(path, OSError(errno.EIO, "fixture disk read failed"))
    try:
        yield fault
    finally:
        if path.is_dir():
            path.rmdir()
        path.write_bytes(earlier)


class CommandRecordTest(DeliveryTask, unittest.TestCase):
    request = relay_fixture.RelayTimesTest.request
    offered_parent_relay = spent_fixture.SpentEvidenceTest.offered_parent_relay

    def test_launch_last_guard_parent_disk_error_keeps_state_io_before_child(self):
        from oah import call
        from oah.capabilities import plan

        request, parent, now = self.request()
        relay, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        execute, snapshots = call.execute, []

        def changed(prepared, **options):
            paths = [prepared.run.file_path(name) for name in ("prompt.md", "skills.json", "capabilities.json")]
            snapshots.append({path: path.read_bytes() for path in paths})
            with read_fault(parent, OSError(errno.EIO, "fixture parent read failed at launch")) as reads:
                try:
                    return execute(prepared, **options)
                finally:
                    self.assertTrue(reads)

        with mock.patch.object(call, "execute", changed):
            code, out, err = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial",
                "--task", "t1", "--tier", "low", "--parent-inventory", str(parent),
                "--relay-result", relay["id"], "--json")

        self.assertEqual(code, 69, out + err)
        self.assertTrue(err.rstrip().splitlines()[-1].startswith("OAH_STATE_IO:"), err)
        self.assertFalse(self.record.exists())
        self.assertTrue(snapshots)
        for snapshot in snapshots:
            self.assertEqual({path: path.read_bytes() for path in snapshot}, snapshot)
        self.assertTrue(all(json.loads((run / "result.json").read_bytes())["status"] == "refused"
                            for run in self.runs()))

    def test_ordinary_last_guard_selection_disk_error_keeps_state_io_before_child(self):
        from oah import call, call_cli
        from oah.discovery import policy_digest, providers, tasks
        from tests import test_fix as fix_fixture, test_skill_delivery as delivery_fixture

        project = fix_fixture.FixProject(self, config="\n[models]\nfetch_scores = false\n")
        self.upstream("acme/late-io", {"LICENSE": delivery_fixture.MIT_LICENSE,
            "skills/plain/SKILL.md": delivery_fixture.skill_md("plain", "Check each recorded row.\n")})
        reviewed = self.reviewed(project.state, "acme/late-io", "skills/plain")
        tools, execute, snapshots = call_cli.intent_tools(), call.execute, []

        def changed(prepared, **options):
            self.assertIsNone(prepared.e_digest)
            packet = call.read_packet(project.repo, project.repo / ".oah/tasks/t1/PACKET.md",
                                      parse_intent=tools.parse, read_source=tools.read_source)
            design, _ = call._task_design(project.lease, packet, state_dir=project.state, stage="initial",
                                           task="t1", engine="codex")
            basis = call.task_basis(project.lease, design, packet, root=project.repo)
            (need,) = call.task_needs(basis, stage="initial", policy=policy_digest(providers.settings_of({})),
                                      needs_file=None)
            tasks.select(project.state, need, [reviewed["id"]], basis=basis)
            index = tasks._index_path(project.state)
            snapshots.append((index, index.read_bytes()))
            with read_fault(index, OSError(errno.EIO, "fixture selection read failed at launch")) as reads:
                try:
                    return execute(prepared, **options)
                finally:
                    self.assertTrue(reads)

        with mock.patch.object(call, "execute", changed):
            code, out, err = cli_result(project.repo, project.env, "call", "codex", "--stage", "initial",
                "--task", "t1", "--tier", "high", "--override-stage", "--no-rerank", "--json")

        self.assertEqual(code, 69, out + err)
        self.assertTrue(err.rstrip().splitlines()[-1].startswith("OAH_STATE_IO:"), err)
        self.assertFalse(project.fix_calls())
        self.assertTrue(snapshots)
        for path, before in snapshots:
            self.assertEqual(path.read_bytes(), before)

    def prepared_parent_relay(self):
        from oah import call, call_cli
        from oah.capabilities import plan
        from oah.secrets import NoKeychain

        request, parent, now = self.request()
        relay, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        tools = call_cli.intent_tools()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
            prepared = call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                parent_inventory=str(parent), relay_results=(relay["id"],)), call_cli.load_settings(self.repo),
                root=self.repo, state_dir=self.state_dir, env=self.env, footer=tools.footer,
                has_footer=tools.has_footer, keychain=NoKeychain())
        prepared.run.launch_path.write_text(json.dumps(prepared.to_json()), encoding="utf-8")
        return prepared, parent

    def test_background_entry_keeps_last_guard_state_io_without_traceback(self):
        from oah import call

        prepared, parent = self.prepared_parent_relay()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                read_fault(parent, OSError(errno.EIO, "fixture parent read failed in background")) as reads, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = call.background_main(str(prepared.run.path))

        self.assertTrue(reads)
        self.assertEqual(code, 69, out.getvalue() + err.getvalue())
        self.assertTrue(err.getvalue().rstrip().splitlines()[-1].startswith("OAH_STATE_IO:"), err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertFalse(self.record.exists())
        self.assertEqual(prepared.run.read()["status"], "refused")

    def test_windows_launcher_state_io_does_not_enter_the_process_retry(self):
        from oah import call

        prepared, parent = self.prepared_parent_relay()
        out, err = io.StringIO(), io.StringIO()
        popen, child = call.subprocess.Popen, []

        def launch(argv, *args, **options):
            if "creationflags" in options:
                child.append(argv)
                raise AssertionError("the fixture runner must not start")
            return popen(argv, *args, **options)

        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo), \
                mock.patch.object(call, "_windows", return_value=True), \
                mock.patch.object(call, "_paid_dispatch", wraps=call._paid_dispatch) as admitted, \
                mock.patch.object(call.subprocess, "Popen", launch), \
                read_fault(parent, OSError(errno.EIO, "fixture parent read failed in launcher")) as reads, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = call.launch_main(str(prepared.run.path))

        self.assertTrue(reads, out.getvalue() + err.getvalue())
        self.assertEqual(code, 69, out.getvalue() + err.getvalue())
        self.assertTrue(err.getvalue().rstrip().splitlines()[-1].startswith("OAH_STATE_IO:"), err.getvalue())
        self.assertEqual(admitted.call_count, 1)
        self.assertEqual(child, [])
        self.assertFalse(self.record.exists())

    def test_settle_missing_prompt_refuses_stale_and_keeps_the_offer(self):
        _now, run, command = self.offered_parent_relay()
        (run / "prompt.md").unlink()
        before = files(self.state_dir)

        result = self.oah(*command)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_NATIVE_OFFER_STALE:"), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(files(self.state_dir), before)
        self.assertFalse(self.record.exists())

    def test_settle_missing_native_offer_refuses_stale_for_the_recorded_native_run(self):
        _now, run, command = self.offered_parent_relay()
        (run / "native-offer.json").unlink()
        before = files(self.state_dir)

        result = self.oah(*command)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_NATIVE_OFFER_STALE:"), result.stderr)
        self.assertEqual(files(self.state_dir), before)
        self.assertFalse(self.record.exists())

    def test_settle_missing_existing_receipt_refuses_stale_and_keeps_the_first_receipt(self):
        _now, run, command = self.offered_parent_relay()
        self.assertEqual(self.oah(*command).returncode, 0)
        for name in ("native-settlement.json", "skills-receiving.json"):
            with self.subTest(file=name), changed_file(run / name, "missing"):
                before = files(self.state_dir)

                result = self.oah(*command)

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertTrue(result.stderr.startswith("OAH_NATIVE_OFFER_STALE:"), result.stderr)
                self.assertEqual(files(self.state_dir), before)
                self.assertFalse(self.record.exists())

    def test_settle_invalid_read_event_refuses_stale_without_a_settlement(self):
        _now, run, command = self.offered_parent_relay()
        (run / "skills-reads.jsonl").write_bytes(b"[]\n")
        before = files(self.state_dir)

        result = self.oah(*command)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_NATIVE_OFFER_STALE:"), result.stderr)
        self.assertEqual(files(self.state_dir), before)
        self.assertFalse(self.record.exists())

    def test_settle_null_existing_receipt_refuses_stale_and_keeps_the_first_receipt(self):
        _now, run, command = self.offered_parent_relay()
        self.assertEqual(self.oah(*command).returncode, 0)
        for name in ("native-settlement.json", "skills-receiving.json"):
            with self.subTest(file=name), changed_file(run / name, "null"):
                before = files(self.state_dir)

                result = self.oah(*command)

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertTrue(result.stderr.startswith("OAH_NATIVE_OFFER_STALE:"), result.stderr)
                self.assertEqual(files(self.state_dir), before)
                self.assertFalse(self.record.exists())

    def test_settle_missing_receiving_record_refuses_before_updating_the_offer(self):
        _now, run, command = self.offered_parent_relay()
        self.assertEqual(self.oah(*command).returncode, 0)
        offer = json.loads((run / "native-offer.json").read_bytes())
        offer["state"] = "offered"
        capability_fixture.write(run / "native-offer.json", offer)
        (run / "skills-receiving.json").unlink()
        before = files(self.state_dir)

        result = self.oah(*command)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_NATIVE_OFFER_STALE:"), result.stderr)
        self.assertEqual(files(self.state_dir), before)
        self.assertFalse(self.record.exists())

    def test_settle_optional_record_cases_are_generated_from_the_boundary_table(self):
        import oah.discovery.boundaries as boundaries

        _now, run, command = self.offered_parent_relay()
        read = self.oah("skills", "read", "--run", run.name, "--item", "1")
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        first = self.oah(*command)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        receipt = (run / "native-settlement.json").read_bytes()
        for name in boundaries.TABLE["settle"].optional_files:
            for kind in ("missing", "directory", "invalid", "null", "denied", "io"):
                with self.subTest(file=name, change=kind), changed_file(run / name, kind) as fault:
                    before = files(self.state_dir)
                    with fault:
                        code, out, err = cli_result(self.repo, self.env, *command)

                    expected = ((0, "") if name == "skills-reads.jsonl" and kind == "missing" else
                                (69, "OAH_STATE_IO:") if kind == "io" else (1, "OAH_NATIVE_OFFER_STALE:"))
                    self.assertEqual(code, expected[0], out + err)
                    if expected[1]:
                        self.assertTrue(err.startswith(expected[1]), err)
                    self.assertNotIn("Traceback", err)
                    self.assertFalse(self.record.exists())
                    self.assertEqual(files(self.state_dir), before)
                    if name != "native-settlement.json":
                        self.assertEqual((run / "native-settlement.json").read_bytes(), receipt)

    def test_prepared_launch_missing_or_directory_prompt_records_invalid_before_child(self):
        from oah import call
        from oah.capabilities import plan

        request, parent, now = self.request()
        relay, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        execute = call.execute
        for kind in ("missing", "directory"):
            with self.subTest(file="prompt.md", change=kind):
                def change(prepared, **options):
                    prompt = pathlib.Path(prepared.run_dir) / "prompt.md"
                    prompt.unlink()
                    if kind == "directory":
                        prompt.mkdir()
                    return execute(prepared, **options)

                with mock.patch.object(call, "execute", change):
                    code, out, err = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial",
                        "--task", "t1", "--tier", "low", "--parent-inventory", str(parent),
                        "--relay-result", relay["id"], "--json")

                self.assertEqual(code, 1, out + err)
                self.assertIn("OAH_RUN_LAUNCH_INVALID:", err)
                self.assertFalse(self.record.exists())
                recorded = [json.loads((run / "result.json").read_bytes()) for run in self.runs()]
                self.assertTrue(recorded)
                self.assertTrue(all(row["status"] == "refused" for row in recorded), str(recorded))
                self.assertTrue(all(row["error"].startswith("OAH_RUN_LAUNCH_INVALID:") for row in recorded))

    def test_nested_relay_request_or_invocation_refuses_without_writing(self):
        request, _parent, _now = self.request()
        source = capability_fixture.write(self.work / "request.json", request)
        payload = capability_fixture.write(self.work / "relay.txt", capability_fixture.RESULT)
        invocation = capability_fixture.write(self.work / "invocation.json", {
            "outcome": "ok", "at": request["created_at"], "arguments_digest": "sha256:" + "3" * 64})
        for path in (source, invocation):
            with self.subTest(input=path.name):
                earlier = path.read_bytes()
                path.write_text("[" * 1500 + "]" * 1500, encoding="utf-8")
                before = files(self.state_dir)
                try:
                    result = self.oah("capabilities", "relay", "--request", str(source), "--capability",
                        request["capability_id"], "--result-file", str(payload), "--invocation", str(invocation))

                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertTrue(result.stderr.startswith("OAH_CAPABILITIES_USAGE:"), result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(files(self.state_dir), before)
                    self.assertFalse(self.record.exists())
                finally:
                    path.write_bytes(earlier)

    def test_launch_file_cases_are_generated_from_the_boundary_table(self):
        from oah import call
        from oah.capabilities import plan
        import oah.discovery.boundaries as boundaries

        request, parent, now = self.request()
        relay, _ = plan.ingest(self.state_dir, request, capability=request["capability_id"],
            payload=capability_fixture.RESULT.encode("utf-8"), invocation={"outcome": "ok", "at": request["created_at"],
                "arguments_digest": "sha256:" + "3" * 64}, now=now)
        execute = call.execute
        for name in boundaries.TABLE["launch"].files:
            for kind in ("missing", "directory", "invalid", "null", "denied", "io"):
                with self.subTest(boundary="launch", file=name, change=kind):
                    observed = []

                    def change(prepared, **options):
                        run = pathlib.Path(prepared.run_dir)
                        saved = json.loads((run / "skills.json").read_bytes())
                        paths = {key: run / key for key in
                                 ("prompt.md", "skills.json", "result.json", "capabilities.json")}
                        paths.update(relay_paths(self.state_dir, saved))
                        with changed_file(paths[name], kind) as fault:
                            try:
                                with fault:
                                    return execute(prepared, **options)
                            finally:
                                observed.append(files(run))

                    with mock.patch.object(call, "execute", change):
                        code, out, err = cli_result(self.repo, self.env, "call", "claude", "--stage", "initial",
                            "--task", "t1", "--tier", "low", "--parent-inventory", str(parent),
                            "--relay-result", relay["id"], "--json")

                    expected = (69, "OAH_STATE_IO:") if kind == "io" else (1, "OAH_RUN_LAUNCH_INVALID:")
                    self.assertEqual(code, expected[0], out + err)
                    self.assertIn(expected[1], err)
                    self.assertNotIn("Traceback", err)
                    self.assertFalse(self.record.exists())
                    self.assertTrue(observed)
                    result = observed[-1].get(pathlib.Path("result.json"))
                    if result is not None and result not in (b"[]", b"null"):
                        self.assertNotEqual(json.loads(result)["status"], "running")
                    if name == "result.json" and kind != "io":
                        self.assertIn(b"OAH_RUN_LAUNCH_INVALID", observed[-1][pathlib.Path("stderr.log")])

    def test_windows_reparse_prompt_refuses_before_reading_it(self):
        _now, run, command = self.offered_parent_relay()
        prompt, lstat = run / "prompt.md", pathlib.Path.lstat
        earlier, before = prompt.read_bytes(), files(self.state_dir)

        def recorded_stat(path):
            if path == prompt:
                return types.SimpleNamespace(st_mode=stat.S_IFREG, st_size=len(earlier), st_file_attributes=0x400)
            return lstat(path)

        with mock.patch.object(pathlib.Path, "lstat", recorded_stat), read_fault(
                prompt, AssertionError("a recorded reparse point must be refused before reading")):
            code, out, err = cli_result(self.repo, self.env, *command)

        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.startswith("OAH_NATIVE_OFFER_STALE:"), err)
        self.assertEqual(files(self.state_dir), before)
        self.assertFalse(self.record.exists())

    def test_parent_scalar_source_refuses_inventory_unknown_before_admission(self):
        from oah.capabilities import inventory

        _request, parent, _now = self.request()
        saved = json.loads(parent.read_bytes())
        saved["entries"][0]["source"] = 1
        saved["entries"][0]["entry_digest"] = inventory.entry_digest(saved["entries"][0])
        saved["digest"] = inventory.inventory_digest(saved)
        capability_fixture.write(parent, saved)

        result = self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low",
                          "--parent-inventory", str(parent), "--json")

        self.assertEqual(result.returncode, 69, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_CAPABILITY_INVENTORY_UNKNOWN:"), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.runs(), [])

    def test_settle_relay_record_and_blob_disk_errors_keep_state_io(self):
        _now, run, command = self.offered_parent_relay()
        saved = json.loads((run / "skills.json").read_bytes())
        for label, path in relay_paths(self.state_dir, saved).items():
            with self.subTest(file=label):
                before = files(self.state_dir)
                with read_fault(path, OSError(errno.EIO, "fixture disk read failed")):
                    code, out, err = cli_result(self.repo, self.env, *command)

                self.assertEqual(code, 69, out + err)
                self.assertTrue(err.startswith("OAH_STATE_IO:"), err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(files(self.state_dir), before)
                self.assertFalse(self.record.exists())

    def test_settle_file_cases_are_generated_from_the_boundary_table(self):
        import oah.discovery.boundaries as boundaries

        _now, run, command = self.offered_parent_relay()
        paths = {name: run / name for name in
                 ("prompt.md", "skills.json", "result.json", "capabilities.json", "native-offer.json")}
        paths.update(relay_paths(self.state_dir, json.loads((run / "skills.json").read_bytes())))
        for name in boundaries.TABLE["settle"].files:
            for kind in ("missing", "directory", "invalid", "null", "denied", "io"):
                with self.subTest(boundary="settle", file=name, change=kind), changed_file(paths[name], kind) as fault:
                    before = files(self.state_dir)
                    with fault:
                        code, out, err = cli_result(self.repo, self.env, *command)

                    expected = (69, "OAH_STATE_IO:") if kind == "io" else (1, "OAH_NATIVE_OFFER_STALE:")
                    self.assertEqual(code, expected[0], out + err)
                    self.assertTrue(err.startswith(expected[1]), err)
                    self.assertNotIn("Traceback", err)
                    self.assertFalse(self.record.exists())
                    self.assertEqual(files(self.state_dir), before)


class FreezeRecordTest(e_c9_fixture.CorrectionFixture):
    def record_paths(self, project, original):
        from oah.discovery import original as lineage, store

        saved = json.loads(original.run.file_path("skills.json").read_bytes())
        index = lineage._index(project.state, "t1")
        pointer = json.loads(index.read_bytes())["original"][7:]
        return {**{name: original.run.file_path(name) for name in
                   ("prompt.md", "skills.json", "result.json", "capabilities.json")},
                **relay_paths(project.state, saved), "original-index": index,
                "original-record": store.root(project.state) / lineage.FOLDER / (pointer + ".json")}

    def check_read_failure(self, project, path, error, expected, exit_code):
        before, calls = files(project.state), project.fix_calls()
        with read_fault(path, error):
            code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

        self.assertEqual((code, result["code"]), (exit_code, expected), str(result) + err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(project.fix_calls(), calls)
        self.assertEqual(files(project.state), before)

    def test_freeze_all_record_permission_errors_refuse_state_changed_before_child(self):
        project, original, _now, _parent, _relays = self.selected_parent(relay=True)
        for label, path in self.record_paths(project, original).items():
            with self.subTest(file=label):
                self.check_read_failure(project, path, PermissionError(errno.EACCES, "fixture read denied"),
                                        "OAH_FIX_STATE_CHANGED", 1)

    def test_freeze_pointer_and_relay_disk_errors_keep_state_io_before_child(self):
        project, original, _now, _parent, _relays = self.selected_parent(relay=True)
        for label, path in self.record_paths(project, original).items():
            with self.subTest(file=label):
                self.check_read_failure(project, path, OSError(errno.EIO, "fixture disk read failed"),
                                        "OAH_STATE_IO", 69)

    def test_freeze_file_cases_are_generated_from_the_boundary_table(self):
        import oah.discovery.boundaries as boundaries

        project, original, _now, _parent, _relays = self.selected_parent(relay=True)
        paths, calls = self.record_paths(project, original), project.fix_calls()
        for name in boundaries.TABLE["freeze"].files:
            for kind in ("missing", "directory", "invalid", "null", "denied", "io"):
                with self.subTest(boundary="freeze", file=name, change=kind), changed_file(paths[name], kind) as fault:
                    before = files(project.state)
                    with fault:
                        code, result, err = self.correct(project, dt.datetime.now(dt.timezone.utc))

                    expected = (69, "OAH_STATE_IO") if kind == "io" else (1, "OAH_FIX_STATE_CHANGED")
                    self.assertEqual((code, result["code"]), expected, str(result) + err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(project.fix_calls(), calls)
                    self.assertEqual(files(project.state), before)
