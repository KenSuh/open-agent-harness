# 실행 기록 어댑터 — 기존 실행 수명 규칙을 쓰고 저장 기록 읽기만 공통 소유자로 보낸다.
from __future__ import annotations

import os

from oah import runs
from oah.discovery import records


class Run(runs.Run):
    def _read_json(self, name: str):
        return records.run_json(self, name)


class WorkerRun(runs.WorkerRun):
    def __enter__(self):
        runs._contained_path(self.path.parents[2], "runs", self.task, self.id)
        self._fd = runs._open_worker_directory(self.path)
        try:
            self._identity = os.fstat(self._fd)
            self._check_directory()
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise
        return self

    def _read_json(self, name: str):
        return records.run_json(self, name)

    def bind(self, data: dict[str, records.Json]) -> None:
        records.bind_worker(self, data)
