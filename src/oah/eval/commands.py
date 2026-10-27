# 평가 명령 해석 — {python}·{plan_dir} 자리표시자를 채우고 실행 파일을 oah.engines.proc 로 찾는다(.cmd/.bat 직접 실행 없음).
from __future__ import annotations

import os
import pathlib
import sys
from typing import Mapping, Sequence

from oah.engines.proc import UnsupportedBatchShim, is_batch, resolve_command
from oah.eval.errors import EXIT_ENVIRONMENT, EvalError

PLACEHOLDERS = ("{python}", "{plan_dir}")


def substitute(arg: str, plan_dir: str) -> str:
    return arg.replace("{python}", sys.executable).replace("{plan_dir}", plan_dir)


def resolve_argv(argv: Sequence[str], env: Mapping[str, str], what: str, *,
                 cwd: pathlib.Path | None = None) -> list[str]:
    """argv with its program resolved; a missing program or a Windows batch file is refused before launch."""
    program, rest = argv[0], list(argv[1:])
    hint = f"install it or change the command of {what}"
    if is_batch(program):
        raise EvalError("OAH_EVAL_COMMAND_MISSING",
                        f"{what} names the batch file {program}, which oah never launches; use a native "
                        "executable or an explicit [\"node.exe\", \"<script>\"] command", EXIT_ENVIRONMENT)
    if os.path.isabs(program) or "/" in program or "\\" in program:
        path = pathlib.Path(program)
        if not path.is_absolute():
            path = (cwd or pathlib.Path.cwd()) / path
        if not path.is_file():
            raise EvalError("OAH_EVAL_COMMAND_MISSING", f"{what}: {program} does not exist; {hint}",
                            EXIT_ENVIRONMENT)
        return [str(path.absolute()), *rest]
    try:
        found = resolve_command(program, env.get("PATH"))
    except UnsupportedBatchShim as exc:
        raise EvalError("OAH_EVAL_COMMAND_MISSING", f"{what}: {exc}", EXIT_ENVIRONMENT) from exc
    if found is None:
        raise EvalError("OAH_EVAL_COMMAND_MISSING", f"{what}: {program} was not found on PATH; {hint}",
                        EXIT_ENVIRONMENT)
    return [*found.argv, *rest]
