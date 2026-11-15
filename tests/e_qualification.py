# 검증 프로필 시험 장치 — 제품에 없는 결정적 용량 근거를 시험 과정에만 명시적으로 주입한다.
from __future__ import annotations

import hashlib
import json
from unittest import mock


def source(target):
    from oah.discovery import budget
    from oah.discovery.store import digest

    profile = {"version": 1, "target": dict(target), "source": "qualified-profile",
               "observed_at": "2020-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z",
               "input_capacity_tokens": 1048576, "output_reserve_tokens": 32768, "host_reserve_tokens": 16384,
               "existing_context_upper_tokens": 0,
               "estimator": {"id": "utf8-upper/v1", "fixed_render_tokens": 2048, "tool_schema_upper_tokens": 8192},
               "transport_budget_bytes": 524288, "skill_budget_bytes": 196608,
               "evidence_ref": digest("DETERMINISTIC TEST FIXTURE ONLY: no measured host qualification")}
    profile["id"] = digest(profile)
    raw = json.dumps({"version": 1, "profiles": [profile]}, sort_keys=True).encode("utf-8")
    return budget.PinnedSource(raw, hashlib.sha256(raw).hexdigest())


def patches():
    from oah.design import task_context
    try:
        from oah.discovery import budget
    except ImportError:
        return ()

    real = task_context.qualify_profile

    def fake_reference(engine, operation, **kwargs):
        if engine == "claude" and kwargs.get("cli_version") == "2.1.318":
            return None
        return real(engine, operation, **kwargs)

    return (mock.patch.object(budget, "_profile_source", source),
            mock.patch.object(task_context, "qualify_profile", fake_reference))


def install():
    for patch in patches():
        patch.start()
