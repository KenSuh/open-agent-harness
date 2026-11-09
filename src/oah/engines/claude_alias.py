# Claude 별칭 확인 — 도움말에 없는 알려진 별칭을 자격 증명 없이 --bare 로 실행해, 과금 없이 CLI 가 그 별칭을 아는지만 본다.
"""The free alias check (mdr2).

`claude --help` names some aliases in an example sentence, so it can omit one that works, such as haiku. At a Claude
catalog rebuild, `confirm` asks the CLI about each known alias the help omits, with no credential, so no request can
be billed. Measured on Claude Code 2.1.316:

    claude --bare -p x --model <alias> --no-session-persistence --tools ""

with stdin closed and no Anthropic credential answers `Not logged in` at no cost, and an alias the CLI does not know
also prints `[claude-code:unrecognized_model]` on stderr. `--bare` reads Anthropic auth only from `ANTHROPIC_API_KEY`
or an `apiKeyHelper` passed with `--settings`, never OAuth or the keychain, so the child gets a copy of the
environment without any `ANTHROPIC_*` name, `CLAUDE_CODE_OAUTH_TOKEN` or `CLAUDE_CODE_USE_*` provider switch, and no
`--settings`. Names are compared in upper case, as Windows does.

The tag is undocumented, so each check first runs a control alias that cannot exist (`oah-control-<8 hex>`), which
must print it. An alias is confirmed only when the control printed the tag, the alias run did not, and it printed
`Not logged in` and exited non-zero. Anything else, a timeout included, leaves it unconfirmed. Each run gets its own
empty folder in the private scratch base, and nothing here can fail a rebuild except an unsafe scratch base, which
stops with OAH_SCRATCH_UNSAFE as every other oah command does.
"""
from __future__ import annotations

import secrets
from typing import Mapping, Sequence

from oah import scratch as oah_scratch
from oah.engines.proc import RunResult, UnsupportedBatchShim, run_command

TAG = "[claude-code:unrecognized_model]"
NOT_LOGGED_IN = "Not logged in"
TIMEOUT = 10.0  # each run; the catalog's own probes allow 30 seconds
_DROPPED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
_DROPPED_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN",)


def argv(base: Sequence[str], alias: str) -> list[str]:
    return [*base, "--bare", "-p", "x", "--model", alias, "--no-session-persistence", "--tools", ""]


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of `env` with no Anthropic credential or third-party provider switch."""
    return {name: value for name, value in env.items()
            if not (name.upper().startswith(_DROPPED_PREFIXES) or name.upper() in _DROPPED_NAMES)}


def _run(base: Sequence[str], alias: str, env: Mapping[str, str]) -> RunResult | None:
    try:
        with oah_scratch.directory("oah-alias-", ignore_cleanup_errors=True) as folder:
            return run_command(argv(base, alias), timeout=TIMEOUT, cwd=folder, env=child_env(env))
    except oah_scratch.ScratchError:
        raise  # an unsafe scratch folder stops oah like everywhere else
    except (OSError, ValueError, UnsupportedBatchShim):
        return None


def confirm(base: Sequence[str], aliases: Sequence[str], env: Mapping[str, str]) -> tuple[str, ...]:
    """The aliases the CLI is shown to know; nothing runs when there is none to check."""
    if not aliases:
        return ()
    control = _run(base, f"oah-control-{secrets.token_hex(4)}", env)
    if control is None or control.timed_out or TAG not in control.stderr:
        return ()
    confirmed = []
    for alias in aliases:
        result = _run(base, alias, env)
        if (result is not None and not result.timed_out and result.returncode not in (0, None)
                and TAG not in result.stderr and NOT_LOGGED_IN in result.stdout + result.stderr):
            confirmed.append(alias)
    return tuple(confirmed)
