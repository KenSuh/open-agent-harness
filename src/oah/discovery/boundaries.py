# E 명령 오류 표 — 공통 기록 읽기의 분류와 각 명령의 공개 거절 코드를 한 표에서 연결한다.
from __future__ import annotations

import dataclasses
import pathlib
import functools
import contextvars
from contextlib import contextmanager
from typing import Callable, Final, Iterator, Literal, ParamSpec, TypeVar

from oah.engines.effort import EngineError

Boundary = Literal["admission", "launch", "freeze", "settle", "relay", "skills", "worker", "design", "wait"]
P = ParamSpec("P")
T = TypeVar("T")
Dispatch = Literal["engine", "runner"]


@dataclasses.dataclass
class DispatchFacts:
    kind: Dispatch | None = None


_DISPATCH: contextvars.ContextVar[DispatchFacts | None] = contextvars.ContextVar("oah_e_dispatch", default=None)


@contextmanager
def dispatch_scope() -> Iterator[None]:
    if _DISPATCH.get() is not None:
        yield
        return
    token = _DISPATCH.set(DispatchFacts())
    try:
        yield
    finally:
        _DISPATCH.reset(token)


def dispatched(kind: Dispatch) -> None:
    facts = _DISPATCH.get()
    if facts is not None:
        facts.kind = kind


def dispatch_kind() -> Dispatch | None:
    facts = _DISPATCH.get()
    return facts.kind if facts is not None else None


def followup_message(kind: Dispatch, message: str) -> str:
    fact = "the run already took place" if kind == "engine" else "the runner was already created"
    return f"{fact}; the follow-up read failed: {message}"


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


_CHECK_ERRORS: Final = ("RecordRefusal", "StateCorrupt", "StoreProblem", "ConfigError", "RoutingError", "RunError",
                       "BudgetRefused", "PackRefused", "CapabilityRefused")
_RUN_FILES: Final = ("prompt.md", "skills.json", "result.json", "capabilities.json", "relay-record", "relay-blob")
TABLE: Final[dict[Boundary, BoundarySpec]] = {
    "admission": BoundarySpec(tuple(Rule(name, "preserve", "; nothing was launched" if name == "NeedsInvalid" else "") for name in
                       ("BudgetRefused", "PackRefused", "CapabilityRefused", "NeedsInvalid", "StoreProblem",
                        "ScratchError", "EngineError")) + (Rule("RecordRefusal", "OAH_SKILL_STATE_CORRUPT"),
                                          Rule("OSError", "OAH_STATE_IO")),
        ("oah.call._admission_refusals",), ("oah.capabilities.inventory.read_parent", "oah.discovery.budget.profiles"),
        ("parent-inventory", "profile-data"), ("process", "native", "orca")),
    "launch": BoundarySpec(tuple(Rule(name, "OAH_SKILL_NOT_DELIVERABLE(changed)" if name in
                         ("BudgetRefused", "PackRefused", "ConfigError", "RoutingError", "ValueError")
                         else "OAH_RUN_LAUNCH_INVALID") for name in _CHECK_ERRORS)
              + (Rule("ValueError", "OAH_SKILL_NOT_DELIVERABLE(changed)"),)
              + (Rule("ScratchError", "preserve"), Rule("EngineError", "preserve"), Rule("OSError", "OAH_STATE_IO")),
        ("oah.call.prepared_e_digest", "oah.call._launch_inputs", "oah.call._check_e"),
        ("oah.skillpack.bundle_of", "oah.discovery.store._read"), _RUN_FILES, ("process",)),
    "freeze": BoundarySpec(tuple(Rule(name, "OAH_FIX_STATE_CHANGED") for name in _CHECK_ERRORS)
              + (Rule("ValueError", "OAH_FIX_STATE_CHANGED"),)
              + (Rule("ScratchError", "preserve", exit_code=1), Rule("EngineError", "preserve"), Rule("OSError", "OAH_STATE_IO")),
        ("oah.call._freeze_e", "oah.call._check_e"), ("oah.call._freeze_e_checked", "oah.discovery.original.lookup",
            "oah.discovery.store._read"), (*_RUN_FILES, "original-index", "original-record"), ("fix",)),
    "settle": BoundarySpec((Rule("ProtocolRefused", "preserve"), Rule("ScratchError", "preserve"))
              + tuple(Rule(name, "OAH_NATIVE_OFFER_STALE") for name in (*_CHECK_ERRORS, "EngineError"))
              + (Rule("ValueError", "OAH_NATIVE_OFFER_STALE"),)
              + (Rule("OSError", "OAH_STATE_IO"),),
        ("oah.call._native_offer_for_settle", "oah.call._settle_native_checked", "oah.call._check_e"),
        ("oah.call._settle_native_operation", "oah.launchers.native._read", "oah.discovery.store._read"),
        (*_RUN_FILES, "native-offer.json"), ("native",),
        ("native-settlement.json", "skills-receiving.json", "skills-reads.jsonl")),
    "relay": BoundarySpec((Rule("RecordRefusal", "OAH_CAPABILITIES_USAGE"), Rule("CapabilityRefused", "preserve"),
              Rule("StoreProblem", "preserve"), Rule("OSError", "OAH_STATE_IO")),
        ("oah.capabilities_cli._relay",), ("oah.discovery.store._read",),
        ("relay-record", "relay-blob"), ("capabilities",)),
    "skills": BoundarySpec((Rule("RecordRefusal", "OAH_SKILLS_IO", exit_code=69), Rule("RoutingError", "preserve")),
        (), (), ("skills.json", "prompt.md", "skills-receiving.json"), ("skills",)),
    "worker": BoundarySpec((Rule("RecordRefusal", "OAH_RUN_LAUNCH_INVALID"),),
        (), (), (*_RUN_FILES, "launch.json"), ("orca",)),
    "design": BoundarySpec((Rule("RecordRefusal", "OAH_SKILL_STATE_CORRUPT", exit_code=69),),
        (), (), ("review-record",), ("coverage",)),
    "wait": BoundarySpec((Rule("RecordRefusal", "OAH_RUN_CORRUPT"),),
        (), (), ("result.json",), ("wait",)),
}


CATCHES: Final[tuple[tuple[str, str, str], ...]] = (
    ("call.py", "_CorrectionInputs.publish_run", "Exception"),
    ("call.py", "_PosixTree.__init__", "Exception"),
    ("call.py", "_PosixTree._process_info", "(FileNotFoundError, ProcessLookupError)"),
    ("call.py", "_PosixTree._signal", "Exception"),
    ("call.py", "_PosixTree._signal", "ProcessLookupError"),
    ("call.py", "_PosixTree._watch", "Exception"),
    ("call.py", "_PosixTree.kill", "Exception"),
    ("call.py", "_admit_skills", "ValueError"),
    ("call.py", "_admit_skills", "skillpack.PackRefused"),
    ("call.py", "_admit_skills.prepare_e", "(ImportError, AttributeError, ConfigError)"),
    ("call.py", "_alive", "PermissionError"),
    ("call.py", "_alive", "ProcessLookupError"),
    ("call.py", "_capability_advice", "judge.JudgeError"),
    ("call.py", "_check_dispatch_lease", "OSError"),
    ("call.py", "_check_e.check_absence", "(ConfigError, PacketError, OSError, ValueError)"),
    ("call.py", "_check_e.check_absence", "EngineError"),
    ("call.py", "_check_skill_bundle", "ConfigError"),
    ("call.py", "_check_skill_bundle", "ValueError"),
    ("call.py", "_conclude_skill_reads", "OSError"),
    ("call.py", "_design_after", "(ValueError, KeyError, TypeError, AttributeError, RecursionError, OSError, design.DesignError)"),
    ("call.py", "_design_after", "OSError"),
    ("call.py", "_drain_observed.drain", "(OSError, ValueError)"),
    ("call.py", "_drain_observed.write", "(OSError, ValueError)"),
    ("call.py", "_drain_observed.write", "(OSError, ValueError)"),
    ("call.py", "_end_reference_failed", "Exception"),
    ("call.py", "_execute", "(KeyboardInterrupt, SystemExit)"),
    ("call.py", "_execute", "(OSError, ValueError)"),
    ("call.py", "_execute", "(OSError, ValueError, subprocess.SubprocessError, RootUnavailable)"),
    ("call.py", "_execute", "EngineError"),
    ("call.py", "_execute", "EngineError"),
    ("call.py", "_execute", "StorageError"),
    ("call.py", "_families", "_READ_FAILURES"),
    ("call.py", "_finish_inline_delivery", "EngineError"),
    ("call.py", "_launch_main", "EngineError"),
    ("call.py", "_launch_main", "OSError"),
    ("call.py", "_launch_main", "StorageError"),
    ("call.py", "_launcher_refusal", "ValueError"),
    ("call.py", "_paid_dispatch", "LockTimeout"),
    ("call.py", "_plan_prepare", "skillpack.PackRefused"),
    ("call.py", "_prepare", "(EngineError, briefs.BriefError)"),
    ("call.py", "_prepare", "skillpack.PackRefused"),
    ("call.py", "_prepare.check_budget", "LeaseError"),
    ("call.py", "_prepare_native", "(EngineError, briefs.BriefError, runs.RunError)"),
    ("call.py", "_prepare_native", "skillpack.PackRefused"),
    ("call.py", "_prepare_native", "skillpack.PackRefused"),
    ("call.py", "_record_refusal", "FileExistsError"),
    ("call.py", "_reference_design", "task_context.DesignError"),
    ("call.py", "_run_prepared", "BaseException"),
    ("call.py", "_run_prepared", "BaseException"),
    ("call.py", "_run_prepared", "Exception"),
    ("call.py", "_run_prepared", "OSError"),
    ("call.py", "_run_prepared.exchange", "BaseException"),
    ("call.py", "_skill_fit_advisor.advise", "(judge.JudgeError, StateProblem, KeyError, TypeError, AttributeError)"),
    ("call.py", "_start_reference", "(DesignError, LeaseError, OSError)"),
    ("call.py", "_stream_correction", "subprocess.TimeoutExpired"),
    ("call.py", "_stream_correction.drain", "(OSError, ValueError)"),
    ("call.py", "_stream_correction.feed", "(OSError, ValueError)"),
    ("call.py", "_task_design", "design.DesignError"),
    ("call.py", "_task_dispatch_gate", "ReviewError"),
    ("call.py", "advise", "Exception"),
    ("call.py", "advise", "ImportError"),
    ("call.py", "background_main", "(EngineError, StorageError)"),
    ("call.py", "check_prepared", "boundaries.CommandRefusal"),
    ("call.py", "dispatch_guard", "(LeaseError, EngineError)"),
    ("call.py", "dispatch_guard", "DesignError"),
    ("call.py", "dispatch_guard", "EngineError"),
    ("call.py", "dispatch_guard", "LeaseError"),
    ("call.py", "execute", "EngineError"),
    ("call.py", "judge_difficulty", "ConfigError"),
    ("call.py", "judge_difficulty", "Exception"),
    ("call.py", "judge_difficulty", "Exception"),
    ("call.py", "judge_difficulty", "Exception"),
    ("call.py", "judge_difficulty", "Exception"),
    ("call.py", "judge_engine", "EngineError"),
    ("call.py", "judge_engine", "Exception"),
    ("call.py", "judge_engine.review_rule", "EngineError"),
    ("call.py", "launch_main", "(EngineError, StorageError)"),
    ("call.py", "prepare", "LockTimeout"),
    ("call.py", "read_packet", "(OSError, oah_intent.SourceError, ValueError)"),
    ("call.py", "read_packet", "(oah_intent.IntentInvalid, ValueError, TypeError)"),
    ("call.py", "read_packet", "SectionInvalid"),
    ("call.py", "read_packet", "ValueError"),
    ("call.py", "read_packet", "routing.RoutingError"),
    ("call.py", "read_scores", "_READ_FAILURES"),
    ("call.py", "read_scores.one", "_READ_FAILURES"),
    ("call.py", "record_dispatch_binding", "LeaseError"),
    ("call.py", "resolve_engine", "scoring.FloorUnmet"),
    ("call.py", "settle_native", "LeaseError"),
    ("call.py", "settle_native", "runs.RunError"),
    ("call.py", "stored_choices", "EngineError"),
    ("call.py", "task_basis", "oah_intent.SourceError"),
    ("call.py", "task_needs", "OSError"),
    ("call.py", "wait", "launchers.DeadlineExpired"),
    ("call_cli.py", "_call", "(EngineError, routing.RoutingError, briefs.BriefError, RunError, LeaseError, ConfigError, StorageError)"),
    ("call_cli.py", "_worker_run", "FileExistsError"),
    ("call_cli.py", "_worker_run", "boundaries.CommandRefusal"),
    ("call_cli.py", "run", "(ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, PendingTransaction, StorageError, StoreProblem, ScratchError)"),
    ("call_cli.py", "run", "(routing.RoutingError, briefs.BriefError, RunError, LeaseError)"),
    ("call_cli.py", "run", "EngineError"),
    ("call_cli.py", "run", "OSError"),
    ("call_cli.py", "run", "launchers.DeadlineExpired"),
    ("capabilities/inventory.py", "EventSink._line", "(ValueError, RecursionError)"),
    ("capabilities/inventory.py", "_read_json", "(OSError, ValueError, UnicodeDecodeError, RecursionError)"),
    ("capabilities/inventory.py", "_read_json", "FileNotFoundError"),
    ("capabilities/inventory.py", "_time", "OverflowError"),
    ("capabilities/inventory.py", "_time", "ValueError"),
    ("capabilities/inventory.py", "configured_claude", "ValueError"),
    ("capabilities/inventory.py", "configured_claude.add", "ValueError"),
    ("capabilities/inventory.py", "configured_codex", "(OSError, ValueError, UnicodeDecodeError)"),
    ("capabilities/inventory.py", "configured_codex", "FileNotFoundError"),
    ("capabilities/inventory.py", "configured_codex", "ValueError"),
    ("capabilities/inventory.py", "configured_codex", "ValueError"),
    ("capabilities/inventory.py", "read_parent", "(OSError, ValueError, StateProblem)"),
    ("capabilities/inventory.py", "read_parent", "FileNotFoundError"),
    ("capabilities/inventory.py", "read_parent.checked", "ValueError"),
    ("capabilities/plan.py", "_relay_time", "ValueError"),
    ("capabilities/plan.py", "check_request", "(ValueError, RecursionError)"),
    ("capabilities/plan.py", "check_settings", "ValueError"),
    ("capabilities/plan.py", "check_settings", "ValueError"),
    ("capabilities/plan.py", "frozen_inputs", "OverflowError"),
    ("capabilities/plan.py", "ingest", "(ValueError, RecursionError)"),
    ("capabilities/plan.py", "ingest", "UnicodeDecodeError"),
    ("capabilities/plan.py", "load.checked_relay", "CapabilityRefused"),
    ("capabilities/select.py", "linked_evidence.evidence", "(SourceError, StateProblem, judge.JudgeError, OSError)"),
    ("capabilities_cli.py", "_plan", "CapabilityRefused"),
    ("capabilities_cli.py", "_plan", "skills_cli.Refused"),
    ("capabilities_cli.py", "_relay", "(OSError, ValueError)"),
    ("capabilities_cli.py", "run", "(ConfigError, NotARepo, GitMissing, StoreProblem, StateCorrupt, StorageError)"),
    ("capabilities_cli.py", "run", "(Refused, PackRefused, NeedsInvalid)"),
    ("capabilities_cli.py", "run", "CapabilityRefused"),
    ("capabilities_cli.py", "run", "EngineError"),
    ("capabilities_cli.py", "run", "OSError"),
    ("design_cli.py", "_Inputs.read", "SourceError"),
    ("design_cli.py", "_Inputs.read", "UnicodeDecodeError"),
    ("design_cli.py", "_carry", "_NothingToCarry"),
    ("design_cli.py", "_carry.update", "(oah_intent.SourceError, design.SectionInvalid)"),
    ("design_cli.py", "_carry.update", "design.RecordInvalid"),
    ("design_cli.py", "_carry.update", "design.SectionInvalid"),
    ("design_cli.py", "_coverage_edit", "SourceError"),
    ("design_cli.py", "_coverage_edit", "StateProblem"),
    ("design_cli.py", "_gate_history", "(OSError, ValueError)"),
    ("design_cli.py", "_gate_history", "FileNotFoundError"),
    ("design_cli.py", "_gate_history", "GateRefused"),
    ("design_cli.py", "_stage_advisor.advise", "judge.JudgeError"),
    ("design_cli.py", "_task_view", "SourceError"),
    ("design_cli.py", "_task_view", "SourceError"),
    ("design_cli.py", "_transport_cleanup", "tc.InstallRefused"),
    ("design_cli.py", "run", "(ConfigError, NotARepo, GitMissing, LockTimeout, StateCorrupt, StorageError, PendingTransaction)"),
    ("design_cli.py", "run", "LeaseError"),
    ("design_cli.py", "run", "PackRefused"),
    ("design_cli.py", "run", "WikiError"),
    ("design_cli.py", "run", "boundaries.CommandRefusal"),
    ("discovery/__init__.py", "refresh_links", "reviews.ReviewFailed"),
    ("discovery/attempts.py", "Attempt.request", "(OSError, ValueError)"),
    ("discovery/attempts.py", "Attempt.request", "(OSError, ValueError)"),
    ("discovery/boundaries.py", "command", "OSError"),
    ("discovery/boundaries.py", "command", "RecordRefusal"),
    ("discovery/boundaries.py", "refusals", "incoming(boundary)"),
    ("discovery/budget.py", "moment", "OverflowError"),
    ("discovery/budget.py", "profiles", "(ValueError, TypeError, KeyError, RecursionError)"),
    ("discovery/budget.py", "profiles", "OSError"),
    ("discovery/gateway.py", "receive", "OSError"),
    ("discovery/gateway.py", "send", "ValueError"),
    ("discovery/gateway.py", "send", "skillpack.PackRefused"),
    ("discovery/inputs.py", "json_value", "StateProblem"),
    ("discovery/inputs.py", "location", "OSError"),
    ("discovery/inputs.py", "read_bytes", "OSError"),
    ("discovery/inputs.py", "source_text", "(OSError, SourceError)"),
    ("discovery/original.py", "publish", "(ValueError, KeyError, TypeError, RecursionError)"),
    ("discovery/preparation.py", "parse", "BudgetRefused"),
    ("discovery/preparation_fields.py", "validate", "CapabilityRefused"),
    ("discovery/providers.py", "parse", "StateProblem"),
    ("discovery/providers.py", "search", "web.WebFailure"),
    ("discovery/records.py", "_parse", "(ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError, RecursionError, StateCorrupt, StateProblem)"),
    ("discovery/records.py", "_read", "OSError"),
    ("discovery/records.py", "_read", "StateCorrupt"),
    ("discovery/records.py", "_read", "StateProblem"),
    ("discovery/records.py", "_retry.attempt", "StorageError"),
    ("discovery/records.py", "_text", "UnicodeDecodeError"),
    ("discovery/records.py", "captured_json", "OSError"),
    ("discovery/records.py", "json_bytes", "StateProblem"),
    ("discovery/records.py", "location", "OSError"),
    ("discovery/review.py", "identity", "SourceError"),
    ("discovery/review.py", "review", "SourceError"),
    ("discovery/review.py", "review", "store.StoreProblem"),
    ("discovery/review.py", "review_hit", "ReviewFailed"),
    ("discovery/runfiles.py", "WorkerRun.__enter__", "BaseException"),
    ("discovery/store.py", "ReviewEvidence.review", "(StoreProblem, StateCorrupt)"),
    ("discovery/store.py", "ReviewEvidence.source", "(SourceError, StateProblem, OSError, ValueError)"),
    ("discovery/tasks.py", "parse_needs", "StateProblem"),
    ("launchers/native.py", "_create_only", "FileExistsError"),
    ("launchers/native.py", "_create_only", "OSError"),
    ("launchers/native.py", "_create_only", "OSError"),
    ("launchers/native.py", "read_receiving.checked", "ProtocolRefused"),
    ("launchers/native.py", "read_result", "OSError"),
    ("launchers/native.py", "read_result", "UnicodeDecodeError"),
    ("launchers/orca.py", "Orca.json", "(OSError, ValueError)"),
    ("launchers/orca.py", "Orca.json", "ValueError"),
    ("launchers/orca.py", "Orca.json", "launchers.DeadlineExpired"),
    ("launchers/orca.py", "_capture", "OrcaReplyError"),
    ("launchers/orca.py", "_preview", "OrcaReplyError"),
    ("launchers/orca.py", "check_runner", "UnsupportedBatchShim"),
    ("launchers/orca.py", "connect", "UnsupportedBatchShim"),
    ("launchers/orca.py", "existing_worktree", "OrcaReplyError"),
    ("launchers/orca.py", "find", "(OSError, ValueError)"),
    ("launchers/orca.py", "find", "UnsupportedBatchShim"),
    ("launchers/orca.py", "find", "launchers.DeadlineExpired"),
    ("launchers/orca.py", "new_worktree", "OrcaReplyError"),
    ("launchers/orca.py", "start", "OrcaReplyError"),
    ("launchers/orca.py", "wait", "OrcaReplyError"),
    ("launchers/orca.py", "wait", "launchers.DeadlineExpired"),
    ("skillpack.py", "_inside", "ValueError"),
    ("skillpack.py", "_installed_copy", "OSError"),
    ("skillpack.py", "_item_text", "ValueError"),
    ("skillpack.py", "_reference", "OSError"),
    ("skillpack.py", "_referenced", "OSError"),
    ("skillpack.py", "admit", "NotARepo"),
    ("skillpack.py", "admit.checked", "(PackRefused, CapabilityRefused, BudgetRefused, NeedsInvalid, EngineError, ConfigError, RoutingError, OSError, ValueError)"),
    ("skillpack.py", "parse_bundle", "(TypeError, ValueError)"),
    ("skills_cli.py", "_discovery_settings", "ValueError"),
    ("skills_cli.py", "_find_task", "call.EngineError"),
    ("skills_cli.py", "_read_run", "(runs.RunError, ValueError)"),
    ("skills_cli.py", "_search_advisor.advise", "judge.JudgeError"),
    ("skills_cli.py", "_select", "(OSError, UnicodeDecodeError, ValueError, AttributeError)"),
    ("skills_cli.py", "_task_context", "call.EngineError"),
    ("skills_cli.py", "before_mutation", "(CatalogError, ConfigError, FetchError, StageError, SkillError, LeaseError, GitMissing, NotARepo, LockTimeout, StateCorrupt, PendingTransaction, StorageError, OSError, ValueError)"),
    ("skills_cli.py", "run", "(CatalogError, ConfigError, FetchError, SkillError, NotARepo, GitMissing, LockTimeout, StateCorrupt, PendingTransaction, StorageError)"),
    ("skills_cli.py", "run", "(NeedInvalid, NeedsInvalid, SourceUnsupported)"),
    ("skills_cli.py", "run", "(Refused, InstallError, StageError, ScheduleError, LeaseError, ScopeError, LeasesActive, RoutingError)"),
    ("skills_cli.py", "run", "EngineError"),
    ("skills_cli.py", "run", "OSError"),
    ("skills_cli.py", "run", "PackRefused"),
    ("skills_cli.py", "run", "ReviewFailed"),
    ("skills_cli.py", "run", "StoreProblem"),
    ("skills_cli.py", "run", "WikiError"),
    ("skills_cli.py", "run", "boundaries.CommandRefusal"),
)


def error_types() -> dict[str, type[Exception]]:
    from oah.atomic import StateCorrupt
    from oah.capabilities import CapabilityRefused
    from oah.config import ConfigError
    from oah.discovery.budget import BudgetRefused
    from oah.discovery.records import RecordRefusal
    from oah.discovery.store import StoreProblem
    from oah.discovery.tasks import NeedsInvalid
    from oah.routing import RoutingError
    from oah.runs import RunError
    from oah.skillpack import PackRefused
    from oah.launchers.native import ProtocolRefused
    from oah.scratch import ScratchError

    return {kind.__name__: kind for kind in (StateCorrupt, CapabilityRefused, ConfigError, BudgetRefused,
            RecordRefusal, StoreProblem, NeedsInvalid, RoutingError, RunError, PackRefused, ProtocolRefused,
            EngineError, ScratchError, OSError, ValueError)}


def incoming(boundary: Boundary) -> tuple[type[Exception], ...]:
    kinds = error_types()
    return tuple(kinds[row.incoming] for row in TABLE[boundary].rules if row.incoming != "RecordRefusal")


class CommandRefusal(EngineError):
    def __init__(self, code: str, message: str, exit_code: int, path: pathlib.Path | None = None,
                 dispatched: Dispatch | None = None) -> None:
        self.exit_code, self.record_path = exit_code, path
        self.dispatched = dispatched
        super().__init__(code, message)


def mapped(boundary: Boundary, exc: Exception) -> Exception:
    from oah.atomic import StorageError
    from oah.discovery.budget import BudgetRefused
    from oah.scratch import ScratchError
    from oah.discovery.records import Category, RecordRefusal

    if isinstance(exc, RecordRefusal):
        happened = dispatch_kind()
        match exc.category:
            case Category.IO:
                message = followup_message(happened, str(exc)) if happened is not None else str(exc)
                return CommandRefusal("OAH_STATE_IO", message, 69, exc.path, happened)
            case Category.STALE:
                row = next(row for row in TABLE[boundary].rules if row.incoming == "RecordRefusal")
                remedy = ("offer again with oah call --launcher native" if boundary == "settle" else
                          "start a new run with oah call" if boundary == "worker" else "prepare again")
                message = (followup_message(happened, f"{exc}; {remedy}") if happened is not None else
                           f"{exc}; nothing was sent and the earlier record is kept; {remedy}")
                return CommandRefusal(row.outward, message,
                    row.exit_code if row.exit_code is not None else 69 if boundary == "admission" else 1,
                    exc.path, happened)

    kinds = error_types()
    row = next(row for row in TABLE[boundary].rules if isinstance(exc, kinds[row.incoming]))
    if isinstance(exc, EngineError) and exc.code == row.outward:
        return exc
    if row.outward == "OAH_STATE_IO":
        assert isinstance(exc, OSError)
        failure = exc if isinstance(exc, StorageError) else StorageError(pathlib.Path(exc.filename or "."), exc)
        happened = dispatch_kind()
        if happened is not None:
            return CommandRefusal("OAH_STATE_IO", followup_message(happened,
                str(failure).removeprefix("OAH_STATE_IO: ")), 69, dispatched=happened)
        return failure
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
    remedy = "offer again with oah call --launcher native" if boundary == "settle" else "prepare again"
    return EngineError(row.outward, f"{exc}; nothing was sent and the earlier record is kept; {remedy}")


@contextmanager
def command(boundary: Boundary) -> Iterator[None]:
    from oah.discovery.records import RecordRefusal

    with dispatch_scope():
        try:
            yield
        except RecordRefusal as exc:
            raise mapped(boundary, exc) from None
        except OSError as exc:
            if dispatch_kind() is None or not any(row.incoming == "OSError" for row in TABLE[boundary].rules):
                raise
            raise mapped(boundary, exc) from None


def entry(boundary: Boundary) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorate(operation: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(operation)
        def run(*args: P.args, **kwargs: P.kwargs) -> T:
            with command(boundary):
                return operation(*args, **kwargs)
        return run
    return decorate


@contextmanager
def refusals(boundary: Boundary) -> Iterator[None]:
    try:
        yield
    except incoming(boundary) as exc:
        failure = mapped(boundary, exc)
        if failure is exc:
            raise
        raise failure from None
