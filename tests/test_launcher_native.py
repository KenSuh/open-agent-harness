# 네이티브 실행기 시험 — oah call --launcher native 가 정확한 메시지를 제안으로만 남기고(지출·라운드 없음), oah call settle 이 신원을 다시 확인해 협조적 증거로 정산하는지 본다.
"""E-T16 (DESIGN §5.5, §5.8, §9.3): the native route through the real `oah call`.

A real repository holds an accepted design whose coverage links a reviewed skill, as in test_skill_delivery. The
native route prints the exact message for the lead's own host to pass to a subagent and records the offer; nothing
starts the fake engine, reserves spend or counts a round. `oah call settle` re-resolves the identities the offer was
made under and records the host child's result as parent-attested, cooperative evidence.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import unittest
from unittest import mock

from tests.helpers import git_commit
from tests.test_design_store import TODAY
from tests.test_skill_delivery import DESIGN, GRILL, DeliveryTask, sha

OFFER_KEYS = {"version", "id", "run", "bundle_id", "pack_id", "prep_id", "task", "design", "target", "budget_profile",
              "message", "canary_sha256", "created_at", "expires_at", "state", "settlement", "authority", "counted"}
SETTLEMENT_KEYS = {"version", "id", "offer_id", "offer_digest", "host_child_id_digest", "task_fingerprint",
                   "bundle_id", "result", "outcome", "delivery", "canary", "receiving_ref", "attestation", "at",
                   "counted"}
TASK_KEYS = {"task", "subtask", "packet_digest", "intent", "scope_digest", "dispatch_stage", "kind", "risk", "lease"}
TARGET_KEYS = {"engine", "route", "cli_version", "profile_id", "model", "effort", "platform", "architecture",
               "root_digest", "config_digest"}
RECEIVING_KEYS = {"version", "id", "run", "bundle_id", "target_digest", "session_id_digest", "delivery", "items",
                  "reference_delivery", "capability_receiving_ref", "at"}
BEGIN_MESSAGE, END_MESSAGE = "----- BEGIN MESSAGE -----\n", "----- END MESSAGE -----\n"
CANARY = re.compile(r"oah-canary: (OAH-NATIVE-[0-9a-f]{16})")
UNKNOWN_RUN = "r20261109T000000Z-00000000"


class NativeTask(DeliveryTask):
    def setUp(self):
        super().setUp()
        self.build_skills(GRILL)
        self.agent("low")

    def agent(self, tier: str) -> None:
        """The tier agent `oah init` writes, in the user's Claude folder, as the host would find it."""
        from oah import init

        folder = self.work / "home" / ".claude" / "agents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"oah-{tier}.md").write_bytes(init.agent_text(tier, "haiku", tier))

    def offer(self, engine: str = "claude", stage: str = "initial", tier: str = "low", *extra: str,
              env: dict | None = None):
        self.record.unlink(missing_ok=True)
        return self.oah("call", engine, "--stage", stage, "--task", "t1", "--tier", tier, "--launcher", "native",
                        *extra, env=env)

    def offered(self, engine: str = "claude", stage: str = "initial", tier: str = "low", **options):
        result = self.offer(engine, stage, tier, "--json", **options)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        return data, self.state_dir / "runs" / "t1" / data["run"]  # run ids made in one second do not sort by time

    def settle(self, run, *, digest: str | None = None, child: str = "agent-1", outcome: str = "succeeded",
               reply: bytes = b"Done: the grill skill was applied.\n", canary: str | None = None,
               env: dict | None = None, name: str = "reply.txt"):
        path = self.work / name
        path.write_bytes(reply)
        if digest is None:
            digest = json.loads((run / "native-offer.json").read_text(encoding="utf-8"))["message"]["sha256"]
        extra = ("--canary", canary) if canary is not None else ()
        return self.oah("call", "settle", "--run", run.name, "--offer-digest", digest, "--host-child-id", child,
                        "--outcome", outcome, "--result-file", str(path), *extra, env=env)

    @staticmethod
    def read(run, name: str) -> dict:
        return json.loads((run / name).read_text(encoding="utf-8"))


class NativeOfferTest(NativeTask, unittest.TestCase):
    def test_the_offer_message_is_the_native_prompt_and_only_documented_host_keys_are_printed(self):
        data, run = self.offered()
        prompt = (run / "prompt.md").read_bytes()
        params = data["host"]["params"]
        self.assertEqual(set(params), {"subagent_type", "description", "prompt"})
        self.assertEqual(params["subagent_type"], "oah-low")
        self.assertEqual(params["prompt"].encode("utf-8"), prompt)
        self.assertEqual(data["message"], {"bytes": len(prompt), "sha256": sha(prompt)})

        offer = self.read(run, "native-offer.json")
        self.assertEqual(set(offer), OFFER_KEYS)
        self.assertEqual(set(offer["task"]), TASK_KEYS)
        self.assertEqual(set(offer["target"]), TARGET_KEYS)
        self.assertEqual((offer["version"], offer["run"], offer["state"], offer["settlement"], offer["authority"],
                          offer["counted"]), (1, run.name, "offered", None, "none", False))
        self.assertEqual(offer["message"], data["message"])
        self.assertEqual((offer["target"]["engine"], offer["target"]["route"], offer["target"]["profile_id"]),
                         ("claude", "native", "oah-low"))
        self.assertEqual(offer["bundle_id"], self.bundle(run)["digest"])
        self.assertEqual((offer["task"]["task"], offer["task"]["dispatch_stage"]), ("t1", "initial"))
        self.assertEqual(offer["design"]["ref"]["project"], "acme")
        self.assertEqual(data["offer"], offer)

        text = prompt.decode("utf-8")
        first = text.split("\n", 1)[0]
        self.assertTrue(first.startswith("You are a native subagent"), first)
        self.assertIn("not a separate oah process", first)
        self.assertIn("cooperative", first)
        (canary,) = CANARY.findall(first)
        self.assertEqual(offer["canary_sha256"], sha(canary.encode("utf-8")))
        self.assertIn(self.block(text), text)

        # Nothing was spent: no engine start, no reservation or round, no model resolution; the run stays prepared.
        self.assertFalse(self.record.exists(), "the fake engine was started")
        result = self.read(run, "result.json")
        self.assertEqual((result["status"], result["launcher"]["name"]), ("prepared", "native"))
        self.assertIsNone(result["model_judge"])
        self.assertEqual((result["resolved_model"], result["cli_version"]), ("unverified", "unverified"))
        self.assertEqual(result["jev_status"], "unused(native)")
        for log in self.state_dir.rglob("rounds.jsonl"):
            self.assertNotIn(run.name, log.read_text(encoding="utf-8"))

        # The text form prints the same bytes between the markers, the host parameters and the settle command.
        printed = self.offer()
        self.assertEqual(printed.returncode, 0, printed.stdout + printed.stderr)
        (second_id,) = re.findall(r"^OAH_NATIVE_OFFERED run=(\S+) ", printed.stdout, re.MULTILINE)
        second = self.state_dir / "runs" / "t1" / second_id
        message = printed.stdout.split(BEGIN_MESSAGE, 1)[1].split(END_MESSAGE, 1)[0]
        self.assertEqual(message.encode("utf-8"), (second / "prompt.md").read_bytes())
        head = printed.stdout.split(BEGIN_MESSAGE, 1)[0]
        self.assertIn("offered, not delivered", head)
        self.assertIn("subagent_type: oah-low", head)
        self.assertIn(f"oah call settle --run {second.name} --offer-digest ", printed.stdout)
        self.assertFalse(self.record.exists())

    def test_codex_gets_the_message_only(self):
        data, run = self.offered("codex", "correction", "high")
        self.assertEqual(set(data["host"]["params"]), {"message"})
        self.assertEqual(data["host"]["params"]["message"].encode("utf-8"), (run / "prompt.md").read_bytes())
        self.assertEqual(self.read(run, "native-offer.json")["target"]["engine"], "codex")
        self.assertFalse(self.record.exists())

    def test_a_missing_tier_agent_refuses_before_any_run(self):
        result = self.offer("claude", "initial", "high")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_SKILL_HOST_UNSUPPORTED"), result.stderr)
        self.assertIn("oah-high", result.stderr)
        self.assertEqual(self.runs(), [])

    def test_a_refused_admission_offers_nothing(self):
        hexdigest = self.reviews["grill"]["id"].split(":", 1)[1]
        (self.state_dir / "skills" / "reviews" / hexdigest[:2] / f"{hexdigest}.json").unlink()
        self.assert_refused_before_any_run(self.offer(), "OAH_SKILL_MANDATORY_MISSING")

    def test_background_does_not_apply(self):
        result = self.offer("claude", "initial", "low", "--background")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("OAH_USAGE", result.stderr)
        self.assertEqual(self.runs(), [])

    def test_the_configured_launcher_cannot_be_native(self):
        config = (self.repo / ".oah/config.toml").read_text(encoding="utf-8")
        git_commit(self.repo, {".oah/config.toml": config + "[call]\nlauncher = \"native\"\n"}, "native config")
        result = self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low")
        self.assertEqual(result.returncode, 69, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_CONFIG_INVALID"), result.stderr)
        self.assertIn("--launcher native", result.stderr)
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.record.exists())

    def test_a_native_offer_is_not_a_dispatch_authorization(self):
        from oah import call
        from oah.engines.effort import EngineError

        _data, run = self.offered()
        prepared = call.Prepared(run_dir=str(run), run_id=run.name, task="t1", engine="claude", stage="initial",
                                 argv=("claude",), cwd=str(self.repo), timeout_s=60.0, env_additions={},
                                 model="haiku", launch_effort="low", cli_version=None, warnings=())
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.repo):
            with self.assertRaises(EngineError) as raised:
                call.check_prepared(prepared)
        self.assertEqual(raised.exception.code, "OAH_RUN_LAUNCH_INVALID")
        self.assertIn("native offer", str(raised.exception))
        worker = self.oah("worker-run", run.name, "--stage", "initial")
        self.assertEqual(worker.returncode, 1, worker.stdout + worker.stderr)
        self.assertIn("OAH_RUN_NOT_QUEUED", worker.stderr)
        waited = self.oah("wait", run.name, "--timeout-seconds", "1")
        self.assertEqual(waited.returncode, 1, waited.stdout + waited.stderr)
        self.assertIn("native offer", waited.stdout + waited.stderr)
        self.assertEqual(self.read(run, "result.json")["status"], "prepared")
        self.assertFalse(self.record.exists())


class SettleTest(NativeTask, unittest.TestCase):
    def test_settle_without_and_with_a_canary(self):
        _data, run = self.offered()
        read = self.oah("skills", "read", "--run", run.name, "--item", "1")
        self.assertEqual(read.returncode, 0, read.stderr)
        reply = b"Done: the grill skill was applied.\n"
        settled = self.settle(run, reply=reply)
        self.assertEqual(settled.returncode, 0, settled.stdout + settled.stderr)
        self.assertIn("parent-attested", settled.stdout)
        self.assertIn("canary=absent", settled.stdout)

        receipt = self.read(run, "native-settlement.json")
        self.assertEqual(set(receipt), SETTLEMENT_KEYS)
        offer = self.read(run, "native-offer.json")
        self.assertEqual((receipt["outcome"], receipt["delivery"], receipt["canary"], receipt["attestation"],
                          receipt["counted"]), ("succeeded", "parent-attested", "absent", "cooperative", False))
        self.assertEqual(receipt["result"], {"bytes": len(reply), "sha256": sha(reply)})
        self.assertEqual(receipt["host_child_id_digest"], sha(b"agent-1"))
        self.assertEqual((receipt["offer_id"], receipt["offer_digest"], receipt["bundle_id"]),
                         (offer["id"], offer["message"]["sha256"], offer["bundle_id"]))
        self.assertEqual((offer["state"], offer["settlement"]), ("settled-succeeded", receipt["id"]))

        receiving = self.read(run, "skills-receiving.json")
        self.assertEqual(set(receiving), RECEIVING_KEYS)
        self.assertEqual((receiving["id"], receiving["delivery"], receiving["reference_delivery"]),
                         (receipt["receiving_ref"], "parent-attested", "not-needed"))
        (item,) = receiving["items"]
        self.assertEqual((item["ordinal"], item["read"], item["application"]["state"]), (1, "oah-mediated", "not-observed"))
        (observation,) = item["observations"]
        self.assertEqual((observation["kind"], observation["exact"]), ("oah-read", True))
        self.assertEqual(self.read(run, "result.json")["status"], "prepared")  # never counted or finished

        # An identical retry returns the first receipt; a contradictory one refuses and changes nothing.
        before = (run / "native-settlement.json").read_bytes()
        again = self.settle(run, reply=reply)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already settled", again.stdout)
        for changed in ({"outcome": "failed"}, {"child": "agent-2"}, {"reply": b"Other text.\n"},
                        {"canary": "OAH-NATIVE-0000000000000000"}, {"digest": "sha256:" + "0" * 64}):
            with self.subTest(changed=changed):
                refused = self.settle(run, **{"reply": reply, **changed})
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertTrue(refused.stderr.startswith("OAH_NATIVE_SETTLE_CONFLICT"), refused.stderr)
        self.assertEqual((run / "native-settlement.json").read_bytes(), before)

        # The canary the brief names is echoed back: match; another token: mismatch, recorded either way.
        _data, second = self.offered()
        (canary,) = CANARY.findall((second / "prompt.md").read_text(encoding="utf-8"))
        matched = self.settle(second, canary=canary)
        self.assertEqual(matched.returncode, 0, matched.stdout + matched.stderr)
        self.assertEqual(self.read(second, "native-settlement.json")["canary"], "match")
        (unread,) = self.read(second, "skills-receiving.json")["items"]
        self.assertEqual((unread["read"], unread["observations"]), ("unverified", []))
        _data, third = self.offered()
        mismatched = self.settle(third, canary="OAH-NATIVE-0000000000000000", outcome="failed")
        self.assertEqual(mismatched.returncode, 0, mismatched.stdout + mismatched.stderr)
        self.assertEqual(self.read(third, "native-settlement.json")["canary"], "mismatch")
        self.assertEqual(self.read(third, "native-offer.json")["state"], "settled-failed")

    def test_an_offer_chosen_by_axis_flags_settles(self):
        for tier in ("medium", "high", "max"):
            self.agent(tier)
        result = self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--risk", "high", "--launcher",
                          "native", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["tier_source"], "packet")  # the packet's plan names the tier; the flag sets the risk
        self.assertEqual(data["offer"]["task"]["risk"], "high")
        settled = self.settle(self.state_dir / "runs" / "t1" / data["run"])
        self.assertEqual(settled.returncode, 0, settled.stdout + settled.stderr)

    def test_a_dispatched_worker_cannot_settle(self):
        _data, run = self.offered()
        refused = self.settle(run, env={"OAH_WORKER": "1", "OAH_RUN_ID": run.name})
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertTrue(refused.stderr.startswith("OAH_SKILLS_WORKER_REFUSED"), refused.stderr)
        self.assertFalse((run / "native-settlement.json").exists())

    def test_the_result_file_is_short_utf8_data(self):
        _data, run = self.offered()
        for reply, reason in ((b"x" * 65_537, "too-large"), (b"\xff\xfe not text\n", "not-utf8"),
                              (b"fine\n  <!-- OAH_SKILL_TEXTS:END -->\n", "marker")):
            with self.subTest(reason=reason):
                refused = self.settle(run, reply=reply)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertTrue(refused.stderr.startswith(f"OAH_NATIVE_RESULT_INVALID({reason})"), refused.stderr)
        self.assertFalse((run / "native-settlement.json").exists())
        self.assertEqual(self.read(run, "native-offer.json")["state"], "offered")

    def test_settle_after_a_design_change_is_stale(self):
        _data, run = self.offered()
        offer = self.read(run, "native-offer.json")
        store = self.store(self.repo)
        current = store.context("acme", "booking").current
        newer = store.save("acme", "booking", expect_revision=1, expect_digest=current.digest, reason="cancel too",
                           parts={"design": DESIGN + "\nPeople can cancel a lesson.\n"}, today=TODAY)
        store.accept("acme", "booking", revision=2, digest=newer.digest, reason="ok", today=TODAY)
        carried = self.oah("design", "carry", "t1", "--agent", "claude", "--revision", "2", "--digest", newer.digest,
                           "--reason", "task unchanged")
        self.assertEqual(carried.returncode, 0, carried.stdout + carried.stderr)
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                stale = self.settle(run)
                self.assertEqual(stale.returncode, 1, stale.stdout + stale.stderr)
                self.assertTrue(stale.stderr.startswith("OAH_NATIVE_OFFER_STALE"), stale.stderr)
                self.assertIn("design", stale.stderr)
        self.assertFalse((run / "native-settlement.json").exists())
        after = self.read(run, "native-offer.json")
        self.assertEqual(after, offer)

    def test_an_unknown_or_unmanaged_run_refuses(self):
        unknown = self.oah("call", "settle", "--run", UNKNOWN_RUN, "--offer-digest", "sha256:" + "0" * 64,
                           "--host-child-id", "agent-1", "--outcome", "succeeded", "--result-file", "reply.txt")
        self.assertEqual(unknown.returncode, 1, unknown.stdout + unknown.stderr)
        self.assertTrue(unknown.stderr.startswith("OAH_SKILL_UNMANAGED_RUN"), unknown.stderr)
        read = self.oah("skills", "read", "--run", UNKNOWN_RUN, "--item", "1")
        self.assertEqual(read.returncode, 1, read.stdout + read.stderr)
        self.assertIn("OAH_SKILL_UNMANAGED_RUN", read.stderr)
        ordinary = self.call()  # a process run: it holds a bundle but no native offer
        self.assertEqual(ordinary.returncode, 0, ordinary.stdout + ordinary.stderr)
        (process_run,) = self.runs()
        raw = self.settle(process_run, digest="sha256:" + "0" * 64)
        self.assertEqual(raw.returncode, 1, raw.stdout + raw.stderr)
        self.assertTrue(raw.stderr.startswith("OAH_SKILL_UNMANAGED_RUN"), raw.stderr)


class ExplicitOnlyTest(unittest.TestCase):
    def test_native_comes_only_from_the_flag(self):
        from oah import launchers
        from oah.engines.effort import EngineError

        self.assertEqual(launchers.choose("native", {"launcher": "process"}), "native")
        self.assertEqual(launchers.choose(None, {"launcher": "orca"}), "orca")
        with self.assertRaises(EngineError) as raised:
            launchers.choose(None, {"launcher": "native"})
        self.assertEqual(raised.exception.code, "OAH_CONFIG_INVALID")
        self.assertIn("--launcher native", str(raised.exception))
        self.assertNotIn("native", launchers.NAMES)

    def test_the_config_template_names_native_as_a_flag_only(self):
        import datetime as dt
        import tomllib

        from oah import init

        text = init.render_config(branch="main", how="the current branch", jev=False, skill_updates=False,
                                  today=dt.date(2026, 11, 9))
        self.assertIn("# oah call --launcher native offers the brief", text)
        for key in ("# [skills.discovery]", "# [capabilities]", "# [call]"):
            self.assertIn(key, text)
        self.assertNotIn("call", tomllib.loads(text))  # every new key stays commented


class SettleParserTest(unittest.TestCase):
    """`settle` is a reserved first token of `oah call`, parsed before the engine positional."""

    def parse(self, *argv: str) -> argparse.Namespace:
        from oah import cli

        return cli.build_parser().parse_args(list(argv))

    def test_settle_and_engine_calls_parse_apart(self):
        args = self.parse("call", "settle", "--run", UNKNOWN_RUN, "--offer-digest", "sha256:" + "0" * 64,
                          "--host-child-id", "agent-1", "--outcome", "failed", "--result-file", "reply.txt")
        self.assertEqual((args.command, args.call_action, args.run, args.outcome, args.canary),
                         ("call", "settle", UNKNOWN_RUN, "failed", None))
        ordinary = self.parse("call", "claude", "--stage", "initial")
        self.assertEqual((ordinary.command, ordinary.engine, getattr(ordinary, "call_action", None)),
                         ("call", "claude", None))
        native = self.parse("call", "codex", "--stage", "review", "--launcher", "native")
        self.assertEqual(native.launcher, "native")
        for argv in (("call", "settle", "--run", UNKNOWN_RUN), ("call", "settle", "--outcome", "maybe"),
                     ("call", "nosuch", "--stage", "initial")):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.parse(*argv)
                self.assertEqual(raised.exception.code, 2)


V1_AGENT_TEMPLATE = """---
name: {{NAME}}
description: {{DESCRIPTION}}
model: {{MODEL}}
effort: {{EFFORT}}
---
You are an oah tier agent for {{TIER}}-difficulty work. The lead gives you one bounded subtask with
its scope; do that subtask and return what you found and changed.

<!-- OAH:TIER-RULES:BEGIN v1 -->
## Rules
- Stay inside the scope your brief names. Edit nothing outside it.
- Cite evidence for every claim: the file and line you read, the command you ran and its output.
- No destructive git: never push, merge, rebase, reset --hard, delete a branch or rewrite history.
- When you are blocked, a check fails or the brief is unclear, stop and return honestly what you found.
- You were dispatched for one task: skip any session-entry procedure and do only the task in your brief.

{{FOOTER}}<!-- OAH:TIER-RULES:END -->
"""


class TierAgentReceivingTest(unittest.TestCase):
    def test_the_tier_agent_names_the_receiving_rule_and_files_from_before_it_are_stale(self):
        from oah import init, intent

        expected = init.agent_text("max", "opus", "max")
        text = expected.decode("utf-8")
        self.assertIn("oah skills read --run", text)
        self.assertIn("oah-canary:", text)
        description = json.dumps("oah max tier: " + init.TIER_WORK["max"] + ". Model opus, effort max, from "
                                 ".oah/config.toml.", ensure_ascii=False)
        values = {"NAME": "oah-max", "DESCRIPTION": description, "MODEL": "opus", "EFFORT": "max", "TIER": "max",
                  "FOOTER": intent.FOOTER}
        older = init._PLACEHOLDER.sub(lambda match: values[match.group(1)], V1_AGENT_TEMPLATE).encode("utf-8")
        self.assertNotEqual(older, expected)
        self.assertEqual(init.agent_check("max", older, expected), ("stale", expected))
        self.assertEqual(init.agent_check("max", older.replace(b"\n", b"\r\n"), expected),
                         ("stale", expected.replace(b"\n", b"\r\n")))
        self.assertEqual(init.agent_check("max", older + b"- my rule\n", expected), ("edited", None))
        self.assertEqual(init.agent_check("max", expected, expected), ("current", None))


if __name__ == "__main__":
    unittest.main()
