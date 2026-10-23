# doctor 명령 모듈 검사 — register/COMMANDS/run 규약과 --json 전달, main 의 dispatch 를 본다.
from __future__ import annotations

import argparse
import unittest
from unittest import mock

from oah import doctor_cli
from oah.cli import main


class DoctorCliTest(unittest.TestCase):
    def test_doctor_cli_convention(self):
        self.assertEqual(doctor_cli.COMMANDS, ("doctor",))
        parser = argparse.ArgumentParser()
        doctor_cli.register(parser.add_subparsers(dest="command"))
        self.assertEqual(vars(parser.parse_args(["doctor"])), {"command": "doctor", "json": False})
        self.assertEqual(vars(parser.parse_args(["doctor", "--json"])), {"command": "doctor", "json": True})

    def test_run_passes_json_flag(self):
        with mock.patch("oah.doctor.doctor_main", return_value=1) as doctor_main:
            self.assertEqual(doctor_cli.run(argparse.Namespace(command="doctor", json=True)), 1)
            self.assertEqual(doctor_cli.run(argparse.Namespace(command="doctor", json=False)), 1)
        self.assertEqual(doctor_main.call_args_list, [mock.call(json_output=True), mock.call(json_output=False)])

    def test_main_dispatches_doctor_to_doctor_cli(self):
        with mock.patch.object(doctor_cli, "run", return_value=5) as run:
            self.assertEqual(main(["doctor", "--json"]), 5)
        self.assertEqual(run.call_args.args[0].json, True)


if __name__ == "__main__":
    unittest.main()
