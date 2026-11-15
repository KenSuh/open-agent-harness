# 오류 표 시험 재료 — 실제 명령에서 같은 오류와 공개 거절 코드를 대조한다.
from __future__ import annotations

import dataclasses
import errno
import pathlib
from contextlib import contextmanager
from unittest import mock


@dataclasses.dataclass(frozen=True, slots=True)
class Sample:
    error: Exception
    code: str


@contextmanager
def mapped_by(test, boundary, sample, surface=None):
    from oah.discovery import boundaries

    mapped, seen = boundaries.mapped, []

    def checked(name, exc):
        if name == boundary and exc is sample.error:
            seen.append(type(exc).__name__)
        return mapped(name, exc)

    with mock.patch.object(boundaries, "mapped", checked):
        yield
    test.assertTrue(seen, f"the {boundary} table did not handle the supplied {type(sample.error).__name__}")
    from tests.e_c12_matrix import ROW_REACH

    surface = surface or {'admission': 'process', 'launch': 'process', 'freeze': 'fix', 'settle': 'native',
                          'relay': 'capabilities', 'skills': 'skills', 'worker': 'orca',
                          'design': 'coverage', 'wait': 'wait'}[boundary]
    ROW_REACH.add((boundary, surface, type(sample.error).__name__))


def samples(root: pathlib.Path) -> dict[str, Sample]:
    from oah.atomic import StateCorrupt
    from oah.capabilities import CapabilityRefused
    from oah.config import ConfigError
    from oah.discovery.budget import BudgetRefused
    from oah.discovery.records import InvalidRecord, Issue
    from oah.discovery.store import StoreProblem
    from oah.discovery.tasks import NeedsInvalid
    from oah.engines.effort import EngineError
    from oah.routing import RoutingError
    from oah.runs import RunError
    from oah.skillpack import PackRefused
    from oah.launchers.native import ProtocolRefused
    from oah.scratch import ScratchError

    return {
        "RecordRefusal": Sample(InvalidRecord(root / "fixture.json", Issue.CONTENT, "has invalid fixture data"), ""),
        "ValueError": Sample(ValueError("fixture live preparation changed"), ""),
        "StateCorrupt": Sample(StateCorrupt("OAH_STATE_CORRUPT: fixture record"), "OAH_STATE_CORRUPT"),
        "StoreProblem": Sample(StoreProblem(root / "fixture.json", "invalid fixture record"), "OAH_SKILL_STATE_CORRUPT"),
        "ConfigError": Sample(ConfigError("OAH_CONFIG_INVALID", "fixture configuration"), "OAH_CONFIG_INVALID"),
        "RoutingError": Sample(RoutingError("OAH_ROUTING_INVALID", "fixture route"), "OAH_ROUTING_INVALID"),
        "RunError": Sample(RunError("OAH_RUN_CORRUPT", "fixture run record"), "OAH_RUN_CORRUPT"),
        "BudgetRefused": Sample(BudgetRefused("skills", "fixture render is over budget"),
                                "OAH_SKILLS_BUDGET_EXCEEDED(skills)"),
        "PackRefused": Sample(PackRefused("OAH_SKILL_NOT_DELIVERABLE", "license", "fixture license"),
                              "OAH_SKILL_NOT_DELIVERABLE(license)"),
        "CapabilityRefused": Sample(CapabilityRefused("OAH_CAPABILITY_UNAVAILABLE", "disabled", "fixture capability"),
                                    "OAH_CAPABILITY_UNAVAILABLE(disabled)"),
        "NeedsInvalid": Sample(NeedsInvalid("malformed", "fixture needs"), "OAH_SKILL_NEEDS_INVALID(malformed)"),
        "EngineError": Sample(EngineError("OAH_MODEL_UNRESOLVED", "fixture target"), "OAH_MODEL_UNRESOLVED"),
        "ProtocolRefused": Sample(ProtocolRefused("OAH_NATIVE_SETTLE_CONFLICT", "fixture settlement claim"),
                                  "OAH_NATIVE_SETTLE_CONFLICT"),
        "ScratchError": Sample(ScratchError(root / "fixture-scratch", "fixture private-folder proof", "win32"),
                               "OAH_SCRATCH_UNSAFE"),
        "OSError": Sample(OSError(errno.EIO, "fixture disk read failed"), "OAH_STATE_IO"),
    }


def expected(rule, sample: Sample) -> tuple[int, str]:
    code = sample.code if rule.outward == "preserve" else rule.outward
    exit_code = (69 if code in ("OAH_STATE_IO", "OAH_SKILL_STATE_CORRUPT", "OAH_CAPABILITY_INVENTORY_UNKNOWN",
                               "OAH_STATE_CORRUPT", "OAH_CONFIG_INVALID", "OAH_SCRATCH_UNSAFE") else 1)
    return (rule.exit_code if rule.exit_code is not None else exit_code), code


def assert_cli(test, result, rule, sample: Sample) -> None:
    code, out, err = result
    exit_code, public = expected(rule, sample)
    test.assertEqual(code, exit_code, out + err)
    test.assertTrue(err.rstrip().splitlines()[-1].startswith(public + ":"), err)
    test.assertNotIn("Traceback", err)
