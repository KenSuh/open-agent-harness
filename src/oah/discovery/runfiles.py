# 실행 기록 어댑터 — 기존 실행 수명 규칙을 쓰고 저장 기록 읽기만 공통 소유자로 보낸다.
from __future__ import annotations

from oah import runs
from oah.discovery import records


class Run(runs.Run):
    def file_path(self, name: str):
        resolve = super().file_path
        return records.location(self.path / name, lambda: resolve(name), deadline=self.deadline)

    def _read_json(self, name: str):
        return records.run_json(self, name)


class WorkerRun(runs.WorkerRun):
    def file_path(self, name: str):
        resolve = super().file_path
        return records.location(self.path / name, lambda: resolve(name), deadline=self.deadline)

    def _read_json(self, name: str):
        def checked(data):
            if name == "result.json" and (data.get("run_id") != self.id or data.get("task") != self.task
                    or self._validated is not None and any(data.get(key) != self._validated.get(key) for key in (
                        "engine", "stage", "created_at", "cwd", "resolved_model", "launch_effort", "cli_version",
                        "override_stage"))):
                raise runs._launch_invalid()
            return data

        return records.run_json(self, name, parse=checked)

    def bind(self, data: dict[str, records.Json]) -> None:
        records.bind_worker(self, data)
