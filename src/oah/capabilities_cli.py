# 능력 명령 — oah capabilities show/plan/relay 의 인자, 리스·설계 읽기, 출력 형식, 종료 코드를 맡는다.
"""`oah capabilities`: the child's inventory, a task's capability plan, and a parent relay record (DESIGN §6.7).

show and plan are pure readouts: no tool, provider or Jev request, no run folder. relay stores a result the lead
already fetched with its own tools; it calls nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT = 0, 1, 69
COMMANDS = ("capabilities",)
USAGE_CODE = "OAH_CAPABILITIES_USAGE"


class Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("capabilities", help="show the child's capability inventory, plan a task's "
                                 "capabilities, or record a parent relay result")
    sub = parser.add_subparsers(dest="capabilities_command", metavar="<subcommand>", required=True)
    show = sub.add_parser("show", help="the child engine's configured capabilities: names and flags, never a "
                          "connection (reads only)")
    show.add_argument("--engine", choices=("claude", "codex"), default="claude")
    show.add_argument("--json", action="store_true", help="print machine-readable JSON")
    plan = sub.add_parser("plan", help="route a task's capability needs; prints relay requests when the lead must "
                          "fetch something (writes nothing)")
    plan.add_argument("--task", required=True)
    plan.add_argument("--stage", choices=("initial", "correction", "review"), required=True)
    plan.add_argument("--engine", choices=("claude", "codex"), default="claude")
    plan.add_argument("--parent-inventory", metavar="FILE", help="the lead's InventoryV1 of its own tools")
    plan.add_argument("--relay-result", metavar="HASH", action="append", default=[],
                      help="a stored relay result (repeatable)")
    plan.add_argument("--skill-needs", metavar="FILE", help="plan with this needs file, as oah call --skill-needs")
    plan.add_argument("--json", action="store_true", help="print machine-readable JSON")
    relay = sub.add_parser("relay", help="store the result of a tool call the lead made for a relay request")
    relay.add_argument("--request", metavar="FILE", required=True, help="the RelayRequestV1 that plan printed")
    relay.add_argument("--capability", metavar="ID", required=True, help="the capability id the request names")
    relay.add_argument("--result-file", metavar="FILE", required=True, help="the tool's result, UTF-8, ≤64 KiB")
    relay.add_argument("--invocation", metavar="FILE", required=True,
                       help="{outcome: ok|error|denied, at: ISO time, arguments_digest: sha256:...}")
    relay.add_argument("--json", action="store_true", help="print machine-readable JSON")


def _context():
    from oah.paths import repo_info, state_dir

    info = repo_info(pathlib.Path.cwd())
    return info, state_dir(info, os.environ)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _show(args: argparse.Namespace) -> int:
    from oah import skillsel
    from oah.capabilities import inventory, plan

    info, _state = _context()
    settings = plan.settings_for(info.root)
    found = inventory.child(args.engine, root=info.root, env=os.environ, home=skillsel.home_of(os.environ),
                            now=_now(), classes=settings["classes"])
    if args.json:
        print(json.dumps(found, indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"OAH_CAPABILITIES engine={args.engine} entries={len(found['entries'])} completeness="
          f"{found['completeness']} (configuration only: connection and schema are unknown)")
    for entry in found["entries"]:
        print(f"  {entry['id']:40} enabled={entry['enabled']:7} approval={entry['approval']:8} "
              f"class={entry['class'] or '-'}")
    return EXIT_OK


def _plan(args: argparse.Namespace) -> int:
    from oah import call, discovery, skillpack, skills_cli
    from oah.call import bound_lease
    from oah.capabilities import CapabilityRefused, plan
    from oah.lease_cli import lease_design

    info, state = _context()
    holder = bound_lease(state, args.task)
    if holder is None:
        raise Refused(USAGE_CODE, f"task {args.task} has no active lease; start it before planning")
    judged = lease_design(holder, state)
    settings, found_settings = skills_cli._discovery_settings(info)
    try:
        basis = skills_cli._task_context(info, state, args.task)[2]  # the task needs the call would admit
    except skills_cli.Refused as exc:
        raise Refused(exc.code, str(exc).split(": ", 1)[1]) from None
    needs = call.task_needs(basis, stage=args.stage, policy=discovery.policy_digest(found_settings),
                            needs_file=args.skill_needs)
    admitted = skillpack.admit(skillpack.Operation("dispatch", task=args.task, lease=(holder.id, holder.generation)),
                               task_design=judged, state_dir=state, engine=args.engine, stage=args.stage,
                               roots=skills_cli._roots(info, state, settings), needs=needs, policy=settings,
                               scope=basis.scope)
    try:
        found, _block = plan.for_task(holder=holder, task=args.task, task_design=judged, engine=args.engine,
                                      stage=args.stage, items=admitted.items, root=info.root, env=os.environ,
                                      state_dir=state, parent_path=args.parent_inventory,
                                      relay_refs=tuple(args.relay_result), now=_now(),
                                      settings=plan.settings_for(info.root), class_status="unused(plan-pure)")
    except CapabilityRefused as exc:
        if args.json:
            print(json.dumps({"refused": {"code": exc.code, "reason": exc.reason, "message": exc.message},
                              "requests": list(exc.requests)}, indent=2, ensure_ascii=False))
        else:
            for request in exc.requests:
                print(json.dumps(request, ensure_ascii=False, sort_keys=True))
        raise
    if args.json:
        print(json.dumps({"plan": found}, indent=2, ensure_ascii=False))
    elif found is None:
        print(f"OAH_CAPABILITY_PLAN {args.task} {args.stage} {args.engine}: no capability needs")
    else:
        print(f"OAH_CAPABILITY_PLAN {args.task} {args.stage} {args.engine}: prep {found['prep_id']}")
        for row in found["selected"]:
            print(f"  {row['id']:40} {row['route']:18} {'required' if row['required'] else 'optional':9} "
                  f"{row.get('reason') or ''}")
    return EXIT_OK


def _relay(args: argparse.Namespace) -> int:
    from oah.capabilities import plan
    from oah.discovery import boundaries, inputs, records

    if os.environ.get("OAH_WORKER") == "1":
        raise Refused("OAH_SKILLS_WORKER_REFUSED", "dispatched workers do not run oah capabilities relay; only the "
                      "lead records relay results")
    _info, state = _context()
    def input_json(name):
        raw = inputs.read_bytes(name, limit=records.LIMIT)
        if len(raw) > records.LIMIT:
            raise ValueError("the JSON input exceeds its bounded size")
        return inputs.json_value(raw, pathlib.Path(name).name)

    try:
        request = input_json(args.request)
        invocation = input_json(args.invocation)
        payload = inputs.read_bytes(args.result_file)
    except (OSError, ValueError) as exc:
        raise Refused(USAGE_CODE, f"cannot read the request, invocation or result file ({exc})") from None
    with boundaries.refusals("relay"):
        record, word = plan.ingest(state, request, capability=args.capability, payload=payload, invocation=invocation,
                                   now=_now())
    if args.json:
        print(json.dumps({"id": record["id"], "word": word, "need_id": record["need_id"],
                          "prep_id": record["prep_id"]}, indent=2, ensure_ascii=False))
    else:
        print(f"OAH_CAPABILITY_RELAY {word} {record['id']} for {record['capability_id']} "
              f"({record['payload']['bytes']} bytes, attestation {record['attestation']}); call again with "
              f"--relay-result {record['id']}")
    return EXIT_OK


HANDLERS = {"show": _show, "plan": _plan, "relay": _relay}


def run(args: argparse.Namespace) -> int:
    from oah.atomic import StateCorrupt, StorageError
    from oah.capabilities import CapabilityRefused
    from oah.config import ConfigError
    from oah.discovery.store import StoreProblem
    from oah.discovery.tasks import NeedsInvalid
    from oah.paths import GitMissing, NotARepo
    from oah.review.runner import credential_redactor
    from oah.skillpack import PackRefused
    from oah.engines.effort import EngineError

    redact = credential_redactor(os.environ)
    try:
        from oah.discovery import boundaries

        with boundaries.command("relay"):
            return HANDLERS[args.capabilities_command](args)
    except EngineError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return EXIT_ENVIRONMENT if exc.code in ("OAH_STATE_IO", "OAH_SKILL_STATE_CORRUPT",
                                                "OAH_CAPABILITY_INVENTORY_UNKNOWN") else EXIT_REFUSED
    except CapabilityRefused as exc:
        print(redact(str(exc)), file=sys.stderr)
        return exc.exit_code
    except (Refused, PackRefused, NeedsInvalid) as exc:
        print(redact(str(exc)), file=sys.stderr)
        return EXIT_REFUSED
    except (ConfigError, NotARepo, GitMissing, StoreProblem, StateCorrupt, StorageError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        return EXIT_ENVIRONMENT
    except OSError as exc:
        print(redact(f"OAH_CAPABILITIES_IO: {exc}. Check permissions and free disk space, then retry."),
              file=sys.stderr)
        return EXIT_ENVIRONMENT
