# 수정 회귀 장치 — 검토된 선택이 있는 실제 일반 실행을 만들어 시간과 이전 기록의 시험에서 공유한다.
from __future__ import annotations

import contextlib
import json
import os
from unittest import mock


def selected_ordinary(test, *, skill_tools="", configure=None):
    from oah import call, call_cli
    from oah.discovery import policy_digest, providers, tasks
    from oah.secrets import NoKeychain
    from tests import test_fix as fixes
    from tests.test_skill_delivery import MIT_LICENSE, skill_md

    project = fixes.FixProject(test, config="\n[models]\nfetch_scores=false\n")
    test.upstream("acme/ledger", {"LICENSE": MIT_LICENSE,
        "skills/ledger/SKILL.md": skill_md("ledger", "Prepare the selected ledger rows.\n", extra=skill_tools)})
    reviewed = test.reviewed(project.state, "acme/ledger", "skills/ledger")
    if configure is not None:
        configure(project, reviewed)
    tools = call_cli.intent_tools()
    with mock.patch.dict(os.environ, project.env, clear=True), contextlib.chdir(project.repo):
        packet = call.read_packet(project.repo, project.repo / ".oah/tasks/t1/PACKET.md",
                                  parse_intent=tools.parse, read_source=tools.read_source)
        judged, _ = call._task_design(project.lease, packet, state_dir=project.state, stage="initial",
                                      task="t1", engine="codex")
        basis = call.task_basis(project.lease, judged, packet, root=project.repo)
        policy = policy_digest(providers.settings_of({}))
        if basis.design is None:
            needs_list = [tasks.need(basis, dispatch_stage="initial", policy=policy, required=True)]
            tasks.select(project.state, needs_list[0], [reviewed["id"]], basis=basis)
        else:
            needs_list = tasks.derive(basis, dispatch_stage="initial", policy=policy)
        need = needs_list[0]
        needs = project.work / "needs.json"
        needs.write_text(json.dumps({"version": 1, "task": "t1", "needs": needs_list}), encoding="utf-8")
        ordinary = call.prepare(call.CallRequest(engine="codex", stage="initial", task="t1", tier="high",
            override_stage=True, skill_needs=str(needs), no_rerank=True), call_cli.load_settings(project.repo),
            root=project.repo, state_dir=project.state, env=project.env, footer=tools.footer,
            has_footer=tools.has_footer, keychain=NoKeychain())
        result = call.execute(ordinary, base_env=project.env, rules=fixes.RULES)
        test.assertEqual(result["status"], "finished", result)
    project.changes_requested()
    project.queue_fix({}, {"write": {"src/app.py": "print('corrected')\n"}})
    return project, ordinary, need, basis
