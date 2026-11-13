# E 명령 오류 표 — 공통 기록 읽기의 분류와 각 명령의 공개 거절 코드를 한 표에서 연결한다.
from __future__ import annotations

import dataclasses
import pathlib
from contextlib import contextmanager
from typing import Final, Iterator, Literal

from oah.engines.effort import EngineError

Boundary = Literal["admission", "launch", "freeze", "settle", "relay"]


@dataclasses.dataclass(frozen=True, slots=True)
class Rule:
    incoming: str
    outward: str
    suffix: str = ""
    exit_code: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class BoundarySpec:
    rules: tuple[Rule, ...]
    handlers: tuple[str, ...]
    readers: tuple[str, ...]
    files: tuple[str, ...]
    surfaces: tuple[str, ...]
    optional_files: tuple[str, ...] = ()


_CHECK_ERRORS: Final = ("InvalidRecord", "StateCorrupt", "StoreProblem", "ConfigError", "RoutingError", "RunError",
                       "BudgetRefused", "PackRefused", "CapabilityRefused")
_RUN_FILES: Final = ("prompt.md", "skills.json", "result.json", "capabilities.json", "relay-record", "relay-blob")
TABLE: Final[dict[Boundary, BoundarySpec]] = {
    "admission": BoundarySpec(tuple(Rule(name, "preserve", "; nothing was launched" if name == "NeedsInvalid" else "") for name in
                       ("BudgetRefused", "PackRefused", "CapabilityRefused", "NeedsInvalid", "StoreProblem",
                        "ScratchError", "EngineError")) + (Rule("InvalidRecord", "OAH_SKILL_STATE_CORRUPT"),
                                          Rule("OSError", "OAH_STATE_IO")),
        ("oah.call._admission_refusals",), ("oah.capabilities.inventory.read_parent", "oah.discovery.budget.profiles"),
        ("parent-inventory", "profile-data"), ("process", "native", "orca")),
    "launch": BoundarySpec(tuple(Rule(name, "OAH_SKILL_NOT_DELIVERABLE(changed)" if name in
                         ("BudgetRefused", "PackRefused", "ConfigError", "RoutingError")
                         else "OAH_RUN_LAUNCH_INVALID") for name in _CHECK_ERRORS)
              + (Rule("ScratchError", "preserve"), Rule("EngineError", "preserve"), Rule("OSError", "OAH_STATE_IO")),
        ("oah.call.prepared_e_digest", "oah.call._launch_inputs", "oah.call._check_e"),
        ("oah.skillpack.bundle_of", "oah.discovery.store._read"), _RUN_FILES, ("process",)),
    "freeze": BoundarySpec(tuple(Rule(name, "OAH_FIX_STATE_CHANGED") for name in _CHECK_ERRORS)
              + (Rule("ScratchError", "preserve", exit_code=1), Rule("EngineError", "preserve"), Rule("OSError", "OAH_STATE_IO")),
        ("oah.call._freeze_e", "oah.call._check_e"), ("oah.call._freeze_e_checked", "oah.discovery.original.lookup",
            "oah.discovery.store._read"), (*_RUN_FILES, "original-index", "original-record"), ("fix",)),
    "settle": BoundarySpec((Rule("ProtocolRefused", "preserve"), Rule("ScratchError", "preserve"))
              + tuple(Rule(name, "OAH_NATIVE_OFFER_STALE") for name in (*_CHECK_ERRORS, "EngineError"))
              + (Rule("OSError", "OAH_STATE_IO"),),
        ("oah.call._native_offer_for_settle", "oah.call._settle_native_checked", "oah.call._check_e"),
        ("oah.call._settle_native_operation", "oah.launchers.native._read", "oah.discovery.store._read"),
        (*_RUN_FILES, "native-offer.json"), ("native",),
        ("native-settlement.json", "skills-receiving.json", "skills-reads.jsonl")),
    "relay": BoundarySpec((Rule("InvalidRecord", "OAH_CAPABILITIES_USAGE"), Rule("CapabilityRefused", "preserve"),
              Rule("StoreProblem", "preserve"), Rule("OSError", "OAH_STATE_IO")),
        ("oah.capabilities_cli._relay",), (), ("request", "invocation", "payload"), ("capabilities",)),
}


def error_types() -> dict[str, type[Exception]]:
    from oah.atomic import StateCorrupt
    from oah.capabilities import CapabilityRefused
    from oah.config import ConfigError
    from oah.discovery.budget import BudgetRefused
    from oah.discovery.records import InvalidRecord
    from oah.discovery.store import StoreProblem
    from oah.discovery.tasks import NeedsInvalid
    from oah.routing import RoutingError
    from oah.runs import RunError
    from oah.skillpack import PackRefused
    from oah.launchers.native import ProtocolRefused
    from oah.scratch import ScratchError

    return {kind.__name__: kind for kind in (StateCorrupt, CapabilityRefused, ConfigError, BudgetRefused,
            InvalidRecord, StoreProblem, NeedsInvalid, RoutingError, RunError, PackRefused, ProtocolRefused,
            EngineError, ScratchError, OSError)}


def incoming(boundary: Boundary) -> tuple[type[Exception], ...]:
    kinds = error_types()
    return tuple(kinds[row.incoming] for row in TABLE[boundary].rules)


def mapped(boundary: Boundary, exc: Exception) -> Exception:
    from oah.atomic import StorageError
    from oah.discovery.budget import BudgetRefused
    from oah.scratch import ScratchError

    kinds = error_types()
    row = next(row for row in TABLE[boundary].rules if isinstance(exc, kinds[row.incoming]))
    if isinstance(exc, EngineError) and exc.code == row.outward:
        return exc
    if row.outward == "OAH_STATE_IO":
        assert isinstance(exc, OSError)
        return exc if isinstance(exc, StorageError) else StorageError(pathlib.Path(exc.filename or "."), exc)
    if row.outward == "preserve" and isinstance(exc, (EngineError, ScratchError)):
        return exc
    code = getattr(exc, "code", "OAH_SKILLS_BUDGET_EXCEEDED" if isinstance(exc, BudgetRefused) else "")
    reason = getattr(exc, "reason", "")
    public = f"{code}({reason})" if reason else code
    message = getattr(exc, "message", getattr(exc, "detail", None))
    if not isinstance(message, str):
        message = str(exc)
        if public and message.startswith(public + ": "):
            message = message[len(public) + 2:]
    if public == row.outward:
        return EngineError(public, message)
    if row.outward == "preserve":
        return EngineError(public, message + row.suffix)
    return EngineError(row.outward, f"{exc}; nothing was sent and the earlier record is kept; prepare again")


@contextmanager
def refusals(boundary: Boundary) -> Iterator[None]:
    try:
        yield
    except incoming(boundary) as exc:
        failure = mapped(boundary, exc)
        if failure is exc:
            raise
        raise failure from None
