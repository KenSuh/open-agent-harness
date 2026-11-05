# 출처 예약 시험 — 주입한 launchd·작업 스케줄러 출력으로 실제 등록 정의(프로그램·인자·폴더·주기·추가 트리거·동작·활성)를 비교하고, 명령 실행·협조 기록·OS 실행 관측이 서로 다른 증거로 남는지 본다.
from __future__ import annotations

import datetime as dt
import json
import pathlib
import unittest
from unittest import mock

from oah import sources_cli
from oah.engines.proc import RunResult
from oah.design.sources.adapters import Transport
from oah.design.sources.check import Sources
from oah.toolkit import schedule
from tests.helpers import make_tmp_dir

NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
PYTHON = "/usr/bin/python3"


def ok(stdout: str = "") -> RunResult:
    return RunResult(0, stdout, "", False)


def fail(code: int = 113) -> RunResult:
    return RunResult(code, "", "not found", False)


class Runner:
    """Answers OS tool argv by its subcommand; records every call and environment."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.calls: list[tuple] = []
        self.envs: list[dict | None] = []

    def __call__(self, argv, *, timeout, env=None, **_):
        self.calls.append(tuple(argv))
        self.envs.append(env)
        for key, value in self.answers.items():
            if key in argv:
                return value
        raise AssertionError(f"unexpected {argv}")


class ScheduleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_tmp_dir(self)
        self.enterContext(mock.patch.object(schedule, "_tool", lambda name, path: (name,)))
        self.enterContext(mock.patch.object(schedule, "_domain", lambda: "gui/501"))

    def entry(self, platform: str) -> schedule.Entry:
        return schedule.plan_entry(project_root=self.root / "my project", home=self.root / "home",
                                   state_dir=self.root / "state", platform=platform, python=PYTHON,
                                   purpose="sources")


class LaunchdTest(ScheduleCase):
    def print_text(self, entry: schedule.Entry, *, interval: str | None = "86400 seconds", arguments=None,
                   extra: tuple[str, ...] = (), environment: dict | None = None, spawn: str = "background (4)",
                   runs: str = "3", last_exit: str = "0") -> str:
        args = arguments if arguments is not None else entry.argv
        lines = [f"gui/501/{entry.name} = {{", "\tactive count = 0", f"\tpath = {entry.path}",
                 "\ttype = LaunchAgent", "\tstate = not running", "", f"\tprogram = {args[0]}", "\targuments = {"]
        lines += [f"\t\t{item}" for item in args] + ["\t}", "", f"\tworking directory = {entry.project_root}", "",
                                                     f"\tstdout path = {entry.log_path}",
                                                     f"\tstderr path = {entry.log_path}", "\tenvironment = {",
                                                     f"\t\tXPC_SERVICE_NAME => {entry.name}"]
        lines += [f"\t\t{key} => {value}" for key, value in (environment or {}).items()]
        lines += ["\t}", "", "\tdomain = gui/501 [100004]", f"\truns = {runs}", f"\tlast exit code = {last_exit}",
                  f"\tspawn type = {spawn}"]
        if interval is not None:
            lines.append(f"\trun interval = {interval}")
        lines += list(extra) + ["}"]
        return "\n".join(lines) + "\n"

    def disabled(self, entry: schedule.Entry, word: str | None = None) -> str:
        body = [f'\t"{entry.name}" => {word}'] if word else []
        return "\n".join(["disabled services = {", '\t"com.example.other" => enabled', *body, "}"]) + "\n"

    def inspect(self, entry: schedule.Entry, print_result: RunResult, disabled: RunResult | None = None,
                plist: bool = True) -> schedule.Inspection:
        if plist:
            entry.path.parent.mkdir(parents=True, exist_ok=True)
            entry.path.write_bytes(schedule.launchd_plist(entry))
        runner = Runner({"print": print_result, "print-disabled": disabled or ok(self.disabled(entry))})
        return schedule.inspect_entry(entry, runner=runner, now=lambda: NOW)

    def test_an_exact_daily_definition_is_scheduled(self) -> None:
        entry = self.entry("darwin")
        result = self.inspect(entry, ok(self.print_text(entry)))
        self.assertEqual(("scheduled", ()), (result.state, result.reasons))
        data = result.to_json()
        self.assertEqual(data["desired_digest"], data["effective_digest"])
        self.assertEqual("3", result.os_run["runs"])

    def test_missing_interval_is_unverifiable_even_with_an_identical_plist(self) -> None:
        entry = self.entry("darwin")
        result = self.inspect(entry, ok(self.print_text(entry, interval=None)))
        self.assertEqual("unverifiable", result.state)
        self.assertIn("run-interval", result.missing)

    def test_wrong_period_extra_trigger_and_changed_arguments_are_different(self) -> None:
        entry = self.entry("darwin")
        cases = {"triggers-differs": dict(interval="3600 seconds"),
                 "arguments-differs": dict(arguments=(*entry.argv[:-1], "--now")),
                 "environment-differs": dict(environment={"PYTHONPATH": "/elsewhere"}),
                 "constraints-differs": dict(spawn="adaptive (6)")}
        for reason, options in cases.items():
            with self.subTest(reason=reason):
                result = self.inspect(entry, ok(self.print_text(entry, **options)))
                self.assertEqual("scheduled-different", result.state)
                self.assertIn(reason, result.reasons)
        for marker in ("\tevent triggers = {\n\t\tcom.apple.notifyd.matching => {\n\t\t}\n\t}",
                       "\tproperties = runatload | inferred program", "\tkeepalive = 1"):
            with self.subTest(marker=marker):
                text = self.print_text(entry, extra=tuple(marker.split("\n")))
                result = self.inspect(entry, ok(text))
                self.assertEqual("scheduled-different", result.state)
                self.assertIn("triggers-differs", result.reasons)

    def test_disabled_override_unknown_enablement_file_only_and_absent(self) -> None:
        entry = self.entry("darwin")
        self.assertEqual("disabled", self.inspect(entry, ok(self.print_text(entry)),
                                                  ok(self.disabled(entry, "disabled"))).state)
        unknown = self.inspect(entry, ok(self.print_text(entry)), fail())
        self.assertEqual(("unverifiable", ("enablement",)), (unknown.state, unknown.missing))
        self.assertEqual("file-only", self.inspect(entry, fail()).state)
        entry.path.unlink()
        self.assertEqual("absent", self.inspect(entry, fail(), plist=False).state)
        garbage = self.inspect(entry, ok("something else\n"))
        self.assertEqual("unverifiable", garbage.state)
        self.assertIn("print-format", garbage.missing)

    def test_a_different_plist_on_disk_is_reported(self) -> None:
        entry = self.entry("darwin")
        entry.path.parent.mkdir(parents=True, exist_ok=True)
        entry.path.write_bytes(b"<plist/>")
        runner = Runner({"print": ok(self.print_text(entry)), "print-disabled": ok(self.disabled(entry))})
        result = schedule.inspect_entry(entry, runner=runner, now=lambda: NOW)
        self.assertEqual("scheduled-different", result.state)
        self.assertIn("plist-differs", result.reasons)


class TaskSchedulerTest(ScheduleCase):
    START = dt.datetime(2026, 11, 1, 10, 0)

    def exported(self, entry: schedule.Entry, change=None) -> str:
        """The task XML as Task Scheduler exports it: the generated task plus fields it adds itself."""
        import xml.etree.ElementTree as ET

        ns = "{" + schedule.TASK_NS + "}"
        task = ET.fromstring(schedule.task_xml(entry, start=self.START).split("\n", 1)[1])
        principal = task.find(f"{ns}Principals/{ns}Principal")
        user = ET.SubElement(principal, f"{ns}UserId")
        user.text = "S-1-5-21-1"
        settings = task.find(f"{ns}Settings")
        for tag, value in (("StopIfGoingOnBatteries", "true"), ("AllowStartOnDemand", "true"), ("Priority", "7")):
            ET.SubElement(settings, f"{ns}{tag}").text = value
        if change is not None:
            change(task, ns, ET)
        return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(task, encoding="unicode")

    def inspect(self, entry: schedule.Entry, text: str | None) -> schedule.Inspection:
        runner = Runner({"/Query": ok(text) if text is not None else fail(1)})
        with mock.patch.object(schedule, "_current_user_sid", create=True, return_value="S-1-5-21-1"):
            return schedule.inspect_entry(entry, runner=runner, now=lambda: NOW)

    def test_another_principal_is_different_and_unknown_identity_is_unverifiable(self) -> None:
        entry = self.entry("win32")
        def changed(task, ns, ET):
            task.find(f"{ns}Principals/{ns}Principal/{ns}UserId").text = "S-1-5-21-2"
        result = self.inspect(entry, self.exported(entry, changed))
        self.assertEqual("scheduled-different", result.state)
        self.assertIn("context-differs", result.reasons)
        with mock.patch.object(schedule, "_current_user_sid", return_value=None):
            result = schedule.inspect_entry(entry, runner=Runner({"/Query": ok(self.exported(entry))}), now=lambda: NOW)
        self.assertEqual("unverifiable", result.state)
        self.assertIn("current-user-identity", result.missing)

    def test_the_generated_daily_task_is_scheduled_with_its_installed_anchor(self) -> None:
        entry = self.entry("win32")
        self.assertIn("<Enabled>true</Enabled>", schedule.task_xml(entry, start=self.START))
        result = self.inspect(entry, self.exported(entry))
        self.assertEqual(("scheduled", ()), (result.state, result.reasons))
        self.assertEqual("2026-11-01T10:00:00", result.desired["triggers"][0]["start_boundary"])

    def test_changed_actions_triggers_and_settings_are_different(self) -> None:
        entry = self.entry("win32")

        def second_exec(task, ns, ET):
            action = ET.SubElement(task.find(f"{ns}Actions"), f"{ns}Exec")
            ET.SubElement(action, f"{ns}Command").text = "calc.exe"

        def logon_trigger(task, ns, ET):
            ET.SubElement(task.find(f"{ns}Triggers"), f"{ns}LogonTrigger")

        def every_two_days(task, ns, ET):
            task.find(f"{ns}Triggers/{ns}CalendarTrigger/{ns}ScheduleByDay/{ns}DaysInterval").text = "2"

        def repetition(task, ns, ET):
            trigger = task.find(f"{ns}Triggers/{ns}CalendarTrigger")
            ET.SubElement(ET.SubElement(trigger, f"{ns}Repetition"), f"{ns}Interval").text = "PT1H"

        def expired(task, ns, ET):
            ET.SubElement(task.find(f"{ns}Triggers/{ns}CalendarTrigger"), f"{ns}EndBoundary").text = \
                "2026-01-01T00:00:00"

        def arguments(task, ns, ET):
            task.find(f"{ns}Actions/{ns}Exec/{ns}Arguments").text = "-m oah sources check"

        def idle_only(task, ns, ET):
            ET.SubElement(task.find(f"{ns}Settings"), f"{ns}RunOnlyIfIdle").text = "true"

        for change, reason in ((second_exec, "actions-not-one-exec"), (logon_trigger, "triggers-differs"),
                               (every_two_days, "triggers-differs"), (repetition, "triggers-differs"),
                               (expired, "triggers-differs"), (arguments, "arguments-differs"),
                               (idle_only, "constraints-differs")):
            with self.subTest(change=change.__name__):
                result = self.inspect(entry, self.exported(entry, change))
                self.assertEqual("scheduled-different", result.state)
                self.assertIn(reason, result.reasons)

    def test_disabled_unknown_fields_and_absent_tasks(self) -> None:
        entry = self.entry("win32")

        def disabled(task, ns, ET):
            task.find(f"{ns}Settings/{ns}Enabled").text = "false"

        def trigger_off(task, ns, ET):
            task.find(f"{ns}Triggers/{ns}CalendarTrigger/{ns}Enabled").text = "false"

        def unknown_exec_field(task, ns, ET):
            ET.SubElement(task.find(f"{ns}Actions/{ns}Exec"), f"{ns}Shell").text = "cmd"

        def no_anchor(task, ns, ET):
            trigger = task.find(f"{ns}Triggers/{ns}CalendarTrigger")
            trigger.remove(trigger.find(f"{ns}StartBoundary"))

        self.assertEqual("disabled", self.inspect(entry, self.exported(entry, disabled)).state)
        self.assertEqual("disabled", self.inspect(entry, self.exported(entry, trigger_off)).state)
        unknown = self.inspect(entry, self.exported(entry, unknown_exec_field))
        self.assertEqual("unverifiable", unknown.state)
        self.assertIn("exec-field:Shell", unknown.missing)
        self.assertEqual("unverifiable", self.inspect(entry, self.exported(entry, no_anchor)).state)
        self.assertEqual("absent", self.inspect(entry, None).state)

    def test_trigger_execution_limit_and_delay_change_the_effective_definition(self) -> None:
        entry = self.entry("win32")
        for tag, value in (("ExecutionTimeLimit", "PT1S"), ("RandomDelay", "PT12H")):
            def changed(task, ns, ET):
                trigger = task.find(f"{ns}Triggers/{ns}CalendarTrigger")
                node = ET.Element(f"{ns}{tag}")
                node.text = value
                if tag == "ExecutionTimeLimit":
                    trigger.insert(1, node)
                else:
                    trigger.append(node)
            with self.subTest(tag=tag):
                result = self.inspect(entry, self.exported(entry, changed))
                self.assertEqual("scheduled-different", result.state)
                self.assertIn("triggers-differs", result.reasons)
                self.assertNotEqual(result.to_json()["desired_digest"], result.to_json()["effective_digest"])

    def test_os_run_observation_uses_a_fixed_query_with_the_name_as_data(self) -> None:
        entry = self.entry("win32")
        inspection = self.inspect(entry, self.exported(entry))
        info = json.dumps({"last_run_utc": "2026-11-01T01:00:00.0000000Z", "last_result": 0, "missed_runs": 0})
        runner = Runner({"-Command": ok(info)})
        observed = schedule.os_run_observation(entry, inspection, runner=runner, now=lambda: NOW)
        self.assertEqual("observed", observed["status"])
        self.assertEqual(entry.name, runner.envs[0]["OAH_TASK_NAME"])
        self.assertNotIn(entry.name, " ".join(runner.calls[0]))
        self.assertEqual(schedule.TASK_INFO_SCRIPT, runner.calls[0][-1])
        broken = schedule.os_run_observation(entry, inspection, runner=Runner({"-Command": ok("not json")}),
                                             now=lambda: NOW)
        self.assertEqual(("unknown", "unreadable"), (broken["status"], broken["reason"]))


class EvidenceChannelTest(ScheduleCase):
    def test_a_manual_scheduled_command_is_not_an_os_run(self) -> None:
        sources = Sources(self.root / "state" / "sources", transport=Transport(), now=lambda: NOW)
        self.assertEqual("complete", sources.scheduled(scheduled=True).outcome)
        entry = self.entry("darwin")
        report = sources_cli.schedule_status(entry, sources, auto_check=True, interval_hours=2.0,
                                             runner=Runner({"print": fail(), "print-disabled": fail()}))
        self.assertEqual("absent", report["registration_state"])
        self.assertEqual(86400, report["os_wakeup_seconds"])
        self.assertEqual(2.0, report["check_interval_hours"])
        channels = report["evidence"]
        self.assertEqual("sources check --scheduled", channels["source_command_run"]["command"])
        self.assertEqual("--scheduled", channels["cooperative_scheduled_run"]["flag"])
        self.assertEqual("unknown", channels["os_run_observation"]["status"])

    def test_auto_check_off_still_shows_a_registered_job(self) -> None:
        sources = Sources(self.root / "state" / "sources", settings={"auto_check": False}, transport=Transport(),
                          now=lambda: NOW)
        entry = self.entry("darwin")
        entry.path.parent.mkdir(parents=True, exist_ok=True)
        entry.path.write_bytes(schedule.launchd_plist(entry))
        text = LaunchdTest.print_text(LaunchdTest(), entry)
        report = sources_cli.schedule_status(entry, sources, auto_check=False, interval_hours=24.0,
                                             runner=Runner({"print": ok(text), "print-disabled": ok("{\n}\n")}))
        self.assertEqual("scheduled", report["registration_state"])
        self.assertEqual("auto-check-off", report["auto_check_note"])
        self.assertEqual("observed", report["evidence"]["os_run_observation"]["status"])

    def test_unsupported_platform_and_missing_tool_are_named(self) -> None:
        sources = Sources(self.root / "state" / "sources", transport=Transport(), now=lambda: NOW)
        self.assertEqual("unsupported", sources_cli.schedule_status(None, sources, auto_check=True,
                                                                    interval_hours=24.0)["registration_state"])

        def missing(name, path):
            raise schedule.ScheduleError("OAH_SCHEDULE_TOOL_MISSING", f"{name} was not found")

        with mock.patch.object(schedule, "_tool", missing):
            report = sources_cli.schedule_status(self.entry("darwin"), sources, auto_check=True, interval_hours=24.0)
        self.assertEqual("tool-missing", report["registration_state"])


if __name__ == "__main__":
    unittest.main()
