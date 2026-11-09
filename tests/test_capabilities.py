# 능력 라우팅 시험 — 가짜 홈의 설정에서 목록을 읽고(연결 주장 없음·비밀 미복사), 계획·상위 중계·의존 단계를 실제 oah 명령과 공개 API 로 본다.
"""G21/AC18 (DESIGN §6): E-T18 inventories, E-T19 plans and E-T20's stream reader (EventSinkTest; the live stop
through `oah call` is in tests/test_skill_delivery.py)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import git_commit, make_tmp_dir
from tests.test_design_context import FAKES
from tests.test_design_sources import LICENSE as MIT_LICENSE
from tests.test_intent_wiring import packet_text
from tests.test_rerank import FakeServer, fake_key
from tests.test_skill_delivery import DeliveryTask
from tests.test_skill_discovery import skill_md
from tests.test_task_discovery import TaskRepo

PLANTED = "planted-value-7c1e9a52b4"  # stands in for a credential in every value field
NOW = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.timezone.utc)
SCHEMA = {"name": "lookup", "description": "Look up library docs", "inputSchema": {"type": "object"}}


def write(path: pathlib.Path, data) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def claude_home(home: pathlib.Path, root: pathlib.Path) -> None:
    """User, local, project and plugin MCP configuration with secrets planted in every value field."""
    write(home / ".claude.json", {
        "mcpServers": {"context7": {"command": "npx", "env": {"DOCS_LOGIN": PLANTED}},
                       "github": {"type": "http", "url": f"https://example.invalid/{PLANTED}",
                                  "headers": {"Authorization": f"Bearer {PLANTED}"}}},
        "projects": {str(root): {"mcpServers": {"localdb": {"command": "db", "args": [PLANTED]}},
                                 "disabledMcpServers": ["localdb"]}}})
    write(root / ".mcp.json", {"mcpServers": {"waiting": {"command": "w"}, "chosen": {"command": "c"},
                                              "refused": {"command": "r"}}})
    write(home / ".claude" / "settings.json", {"enabledPlugins": {"good@market": True, "off@market": False},
                                               "enabledMcpjsonServers": ["chosen"],
                                               "disabledMcpjsonServers": ["refused"]})
    good, off = home / ".claude" / "plugins" / "good", home / ".claude" / "plugins" / "off"
    write(good / ".mcp.json", {"mcpServers": {"srv": {"command": "g", "env": {"DOCS_LOGIN": PLANTED}}}})
    write(off / ".mcp.json", {"mcpServers": {"offsrv": {"command": "o"}}})
    write(home / ".claude" / "plugins" / "cache" / "ghost" / ".mcp.json", {"mcpServers": {"ghostsrv": {}}})
    write(home / ".claude" / "plugins" / "installed_plugins.json", {"version": 2, "plugins": {
        "good@market": [{"scope": "user", "installPath": str(good)}],
        "off@market": [{"scope": "user", "installPath": str(off)}]}})


class InventoryTest(unittest.TestCase):
    """E-T18: configured / disabled / pending-approval, never connected; plugins need install and enable."""

    def setUp(self):
        self.home = make_tmp_dir(self) / "home"
        self.root = make_tmp_dir(self) / "repo"
        self.root.mkdir()

    def test_claude_configuration_gives_names_and_flags_only(self):
        from oah.capabilities import inventory

        claude_home(self.home, self.root)
        found = inventory.child("claude", root=self.root, env={}, home=self.home, now=NOW,
                                classes={"context7": "library-docs"})
        entries = {entry["id"]: entry for entry in found["entries"]}
        self.assertEqual(set(entries), {"mcp:context7/*", "mcp:github/*", "mcp:localdb/*", "mcp:waiting/*",
                                        "mcp:chosen/*", "mcp:refused/*", "plugin:good", "plugin:off",
                                        "mcp:plugin_good_srv/*"})
        flags = {key: (entry["enabled"], entry["approval"]) for key, entry in entries.items()}
        self.assertEqual(flags["mcp:context7/*"], ("yes", "allowed"))
        self.assertEqual(flags["mcp:localdb/*"], ("no", "allowed"))
        self.assertEqual(flags["mcp:waiting/*"], ("yes", "pending"))
        self.assertEqual(flags["mcp:chosen/*"], ("yes", "allowed"))
        self.assertEqual(flags["mcp:refused/*"], ("no", "denied"))
        self.assertEqual((flags["plugin:good"][0], flags["plugin:off"][0]), ("yes", "no"))
        self.assertEqual(entries["mcp:context7/*"]["class"], "library-docs")
        for entry in entries.values():
            self.assertNotEqual(entry["connection"], "connected")
            self.assertEqual(entry["callability"], "unknown")
            self.assertIsNone(entry["schema"]["sha256"])
        self.assertEqual(found["completeness"], "complete")
        self.assertNotIn(PLANTED, json.dumps(found))

    def test_codex_configuration_and_a_broken_file(self):
        from oah.capabilities import inventory

        codex = self.home / ".codex"
        write(codex / "config.toml", "[mcp_servers.context7]\ncommand = \"npx\"\n"
              f"env = {{ DOCS_LOGIN = \"{PLANTED}\" }}\ndisabled_tools = [\"wipe\"]\n"
              "[mcp_servers.idle]\ncommand = \"i\"\nenabled = false\n")
        found = inventory.child("codex", root=self.root, env={}, home=self.home, now=NOW, classes={})
        entries = {entry["id"]: entry["enabled"] for entry in found["entries"]}
        self.assertEqual(entries, {"mcp:context7/*": "yes", "mcp:context7/wipe": "no", "mcp:idle/*": "no"})
        self.assertNotIn(PLANTED, json.dumps(found))
        write(codex / "config.toml", "[mcp_servers\nbroken")
        self.assertEqual(inventory.child("codex", root=self.root, env={}, home=self.home, now=NOW,
                                         classes={})["completeness"], "partial")

    def test_parent_inventory_is_validated_and_same_short_names_stay_distinct(self):
        from oah.capabilities import CapabilityRefused, inventory

        alpha = inventory.parent_entry("mcp:alpha/search", schema=SCHEMA, observed=NOW, effects=["read-network"])
        beta = inventory.parent_entry("mcp:beta/search", schema=dict(SCHEMA, description="other"), observed=NOW,
                                      effects=["read-network"])
        report = inventory.parent_inventory([alpha, beta], observed=NOW)
        checked = inventory.check_parent(json.loads(json.dumps(report)), now=NOW)
        self.assertEqual([entry["id"] for entry in checked["entries"]], ["mcp:alpha/search", "mcp:beta/search"])
        self.assertNotEqual(alpha["entry_digest"], beta["entry_digest"])
        self.assertTrue(inventory.qualified(alpha, NOW))
        self.assertFalse(inventory.qualified(alpha, NOW + dt.timedelta(seconds=301)))
        tampered = json.loads(json.dumps(report))
        tampered["entries"][0]["connection"] = "unknown"
        path = write(make_tmp_dir(self) / "inventory.json", tampered)
        with self.assertRaises(CapabilityRefused) as caught:
            inventory.read_parent(path, now=NOW)
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("OAH_CAPABILITY_INVENTORY_UNKNOWN", 69))
        with self.assertRaises(CapabilityRefused):
            inventory.read_parent(path.with_name("missing.json"), now=NOW)


class DependencyTest(unittest.TestCase):
    """E-T19: dependencies resolve within three rounds; deeper chains and cycles refuse with the chain."""

    def assess(self, requires, *, plugins=None):
        from oah.capabilities import inventory, plan

        child = {"version": 1, "target": {"engine": "claude"}, "completeness": "complete", "digest": "x",
                 "entries": [inventory.entry(f"plugin:{name}", "plugin", adapter="configured.claude", observed=NOW,
                                             target="t", enabled=state) for name, state in (plugins or {}).items()]}
        lead = [inventory.parent_entry(capability, schema=SCHEMA, observed=NOW, effects=["read-network"])
                for capability in ("mcp:docs/lookup", "mcp:a/x", "mcp:b/y", "mcp:c/z", "mcp:d/w")]
        parent = inventory.parent_inventory(lead, observed=NOW)
        need = {"capability_id": "mcp:docs/lookup", "class": None, "required": False, "origin": "skill",
                "by": ["skill:docs"], "id": "sha256:" + "1" * 64}
        return plan.assess(task={"task": "t1"}, design=None, engine="claude", stage="initial", needs=[need],
                           child=child, parent=parent, results=[], requires=requires, now=NOW, state_dir=".")

    def test_two_rounds_resolve_and_four_refuse(self):
        from oah.capabilities import CapabilityRefused

        resolved = self.assess({"mcp:docs/lookup": ["mcp:a/x"], "mcp:a/x": ["mcp:b/y"]})
        self.assertEqual(resolved["dependencies"]["rounds"], 2)
        self.assertEqual([row["route"] for row in resolved["selected"]], ["parent-relay"] * 3)
        with self.assertRaises(CapabilityRefused) as caught:
            self.assess({"mcp:docs/lookup": ["mcp:a/x"], "mcp:a/x": ["mcp:b/y"], "mcp:b/y": ["mcp:c/z"],
                         "mcp:c/z": ["mcp:d/w"]})
        self.assertEqual((caught.exception.code, caught.exception.reason),
                         ("OAH_CAPABILITY_DEPENDENCY_UNSATISFIED", "depth"))

    def test_a_cycle_ends_with_its_chain(self):
        from oah.capabilities import CapabilityRefused

        with self.assertRaises(CapabilityRefused) as caught:
            self.assess({"mcp:docs/lookup": ["mcp:a/x"], "mcp:a/x": ["mcp:docs/lookup"]})
        self.assertEqual(caught.exception.reason, "cycle")
        self.assertIn("mcp:docs/lookup → mcp:a/x → mcp:docs/lookup", str(caught.exception))

    def test_a_plugin_server_needs_its_plugin_enabled(self):
        from oah.capabilities import CapabilityRefused, inventory, plan

        for state, ok in (("yes", True), ("no", False)):
            with self.subTest(state=state):
                child = {"version": 1, "target": {"engine": "claude"}, "completeness": "complete", "digest": "x",
                         "entries": [inventory.entry("plugin:good", "plugin", adapter="configured.claude",
                                                     observed=NOW, target="t", enabled=state),
                                     inventory.entry("mcp:plugin_good_srv/*", "mcp", adapter="configured.claude",
                                                     observed=NOW, target="t", approval="allowed")]}
                need = {"capability_id": "mcp:plugin_good_srv/q", "class": None, "required": True,
                        "origin": "skill", "by": ["skill:s"], "id": "sha256:" + "2" * 64}
                call = lambda: plan.assess(task={"task": "t1"}, design=None, engine="claude",  # noqa: E731
                                           stage="initial", needs=[need], child=child, parent=None, results=[],
                                           requires={}, now=NOW, state_dir=".")
                with self.assertRaises(CapabilityRefused) as caught:
                    call()
                # The server itself is only named in configuration: required use is unsupported either way.
                expected = ("OAH_CAPABILITY_UNSUPPORTED" if ok else "OAH_CAPABILITY_DEPENDENCY_UNSATISFIED")
                self.assertEqual(caught.exception.code, expected)


def library(name: str, *, effects=("read-network",), cls: str | None = "library-docs",
            observed: dt.datetime = NOW) -> dict:
    """A lead-reported entry `mcp:<name>/lookup` of one class, built from its own live descriptor."""
    from oah.capabilities import inventory

    return inventory.parent_entry(f"mcp:{name}/lookup", schema=dict(SCHEMA, description=f"{name} docs"),
                                  observed=observed, effects=list(effects), cls=cls)


def public(text: str):
    """A public snapshot slice as `linked_evidence` returns it (select reads only its refs and digest)."""
    from oah.rerank import judge

    data = text.encode("utf-8")
    return judge.PublicSnapshotField("sha256:" + hashlib.sha256(b"snapshot " + data).hexdigest(), "document-text", 0,
                                     len(data), "sha256:" + hashlib.sha256(data).hexdigest(), text)


CLASS_NEED = {"id": "sha256:" + "4" * 64, "class": "library-docs", "capability_id": None, "required": False,
              "origin": "jev", "by": ["need:abcdefabcdef"], "advice_ref": "sha256:" + "5" * 64}
JUDGEMENT = "sha256:" + "8" * 64


def chose(option: str):
    return lambda request: {"status": "used", "applied": True, "choice": option, "reason": None,
                            "judgement_ref": JUDGEMENT}


class EntrySelectionTest(unittest.TestCase):
    """AC18 (DESIGN §7.1.1): a class need's candidates are the qualified entries of its class in inventory order;
    Jev chooses among two to eight of them only when every option has linked public documentation, and its choice
    moves the pick only after the recheck. Every other outcome is typed and keeps the first candidate."""

    def select(self, count: int = 2, *, advise=None, evidence=None):
        from oah.capabilities import select

        found = [(library(f"e{index}"), "parent-relay") for index in range(count)]
        texts = {entry["id"]: public(f"Documentation of {entry['id']}.") for entry, _route in found}
        asked: list[dict] = []

        def advisor(request):
            asked.append(request)
            return advise(request)

        record, pick, deferred = select.select(CLASS_NEED, found, prep="sha256:" + "6" * 64,
                                               inventory_digest="sha256:" + "7" * 64,
                                               evidence=evidence or texts.get,
                                               advise=None if advise is None else advisor)
        return record, pick, deferred, asked, found

    def test_candidates_are_qualified_entries_of_the_class_with_the_childs_own_first(self):
        from oah.capabilities import inventory, select

        child = {"entries": [library("own"),
                             inventory.entry("mcp:named/*", "mcp", adapter="configured.claude", observed=NOW,
                                             target="t", approval="allowed", cls="library-docs")]}
        lead = [library("own"), library("alpha"), library("writer", effects=("write-remote",)),
                library("search", cls="web-search"), library("old", observed=NOW - dt.timedelta(seconds=400)),
                library("beta")]
        found = select.candidates("library-docs", child, inventory.parent_inventory(lead, observed=NOW), NOW)
        # A configured name is never qualified; a write effect, another class or old evidence never enters.
        self.assertEqual([(entry["id"], route) for entry, route in found],
                         [("mcp:own/lookup", "direct"), ("mcp:alpha/lookup", "parent-relay"),
                          ("mcp:beta/lookup", "parent-relay")])

    def test_a_rechecked_choice_moves_the_pick_and_records_the_option_map(self):
        from oah.capabilities import digest

        record, pick, deferred, asked, found = self.select(advise=chose("o1"))
        (request,) = asked
        self.assertEqual([option["option"] for option in request["options"]], ["o0", "o1"])
        self.assertEqual(set(request["fields"]), {"o0", "o1"})
        self.assertEqual(request["option_map_digest"], digest(request["options"]))
        option = request["options"][1]
        self.assertEqual((option["capability_id"], option["entry_digest"], option["schema_digest"]),
                         (found[1][0]["id"], found[1][0]["entry_digest"], found[1][0]["schema"]["sha256"]))
        self.assertEqual(option["public_evidence"]["slice_digest"], request["fields"]["o1"].bytes_digest)
        self.assertEqual((record["outcome"], record["applied"], record["selected_option"]), ("selected", True, "o1"))
        self.assertEqual((record["selected_entry_digest"], record["rule_entry_digest"]),
                         (found[1][0]["entry_digest"], found[0][0]["entry_digest"]))
        self.assertEqual((record["judgement_ref"], record["option_map_digest"]),
                         (JUDGEMENT, request["option_map_digest"]))
        self.assertEqual(record["id"], digest({key: value for key, value in record.items() if key != "id"}))
        self.assertEqual((pick[0]["id"], deferred), (found[1][0]["id"], []))

    def test_every_other_answer_keeps_the_first_entry_with_a_typed_outcome(self):
        cases = {("used", False, None, "no-match"): ("no-match", "no-match"),
                 ("used", False, None, "uncertain"): ("uncertain", "uncertain"),
                 ("used", False, None, "stale-input"): ("stale", "stale-input"),
                 ("used", False, None, "field-changed:o1:capability_excerpt"):
                     ("stale", "field-changed:o1:capability_excerpt"),
                 ("used", False, None, "bad-response"): ("error", "bad-response"),
                 ("unused(task-text-off)", False, None, None): ("off", "task-text-off"),
                 ("unused(not-configured)", False, None, None): ("off", "not-configured"),
                 ("unused(error:timeout)", False, None, None): ("error", "error:timeout"),
                 ("used", True, "o9", None): ("error", "choice-outside-options")}
        for (status, applied, choice, reason), expected in cases.items():
            with self.subTest(status=status, reason=reason, choice=choice):
                record, pick, _deferred, _asked, found = self.select(advise=lambda request: {
                    "status": status, "applied": applied, "choice": choice, "reason": reason, "judgement_ref": None})
                self.assertEqual((record["outcome"], record["reason"]), expected)
                self.assertEqual((record["applied"], record["selected_option"]), (False, None))
                self.assertEqual(pick[0]["id"], found[0][0]["id"])

    def test_fewer_than_two_or_unlinked_options_ask_nothing(self):
        def never(request):
            self.fail("Jev was asked")

        record, pick, *_rest = self.select(0, advise=never)
        self.assertEqual((record["outcome"], record["reason"], pick), ("no-candidate", "no-qualified-entry", None))
        record, pick, _deferred, _asked, found = self.select(1, advise=never)
        self.assertEqual((record["outcome"], pick[0]["id"]), ("single-option", found[0][0]["id"]))
        linked = library("e0")["id"]
        record, pick, _deferred, _asked, found = self.select(
            2, advise=never, evidence=lambda capability: public("docs") if capability == linked else None)
        self.assertEqual((record["outcome"], record["reason"], record["option_map_digest"]),
                         ("no-public-candidate-evidence", "no linked public documentation for option o1", None))
        self.assertEqual(pick[0]["id"], found[0][0]["id"])
        record, pick, *_rest = self.select(2)
        self.assertEqual((record["outcome"], record["reason"], record["applied"]), ("off", "judgement-off", False))
        self.assertIsNotNone(record["option_map_digest"])

    def test_at_most_eight_options_and_the_rest_deferred(self):
        record, pick, deferred, asked, found = self.select(9, advise=chose("o7"))
        self.assertEqual([option["option"] for option in asked[0]["options"]], [f"o{index}" for index in range(8)])
        self.assertEqual(deferred, [found[8][0]["id"]])
        self.assertEqual((record["outcome"], pick[0]["id"]), ("selected", found[7][0]["id"]))


class ClassPlanTest(unittest.TestCase):
    """AC18 (DESIGN §6.3.4, §6.6, §6.7): class needs reach the plan as optional rows with their advice; they never
    refuse, never ask for a relay, never duplicate a row and never change the preparation identity."""

    def assess(self, class_needs=(), *, advise=None, lead=("alpha", "beta"), needs=(), class_status=None):
        from oah.capabilities import inventory, plan

        child = {"version": 1, "target": {"engine": "claude"}, "completeness": "complete", "digest": "x",
                 "entries": []}
        parent = inventory.parent_inventory([library(name) for name in lead], observed=NOW)
        return plan.assess(task={"task": "t1"}, design=None, engine="claude", stage="initial", needs=list(needs),
                           child=child, parent=parent, results=[], requires={}, now=NOW, state_dir=".",
                           class_needs=class_needs, class_status=class_status,
                           evidence=lambda capability: public(f"Documentation of {capability}."), advise=advise)

    def test_a_chosen_entry_is_an_optional_row_with_its_advice(self):
        asked = []

        def advise(need, request):
            asked.append((need["id"], [option["capability_id"] for option in request["options"]]))
            return chose("o1")(request)

        found = self.assess([CLASS_NEED], advise=advise)
        self.assertEqual(asked, [(CLASS_NEED["id"], ["mcp:alpha/lookup", "mcp:beta/lookup"])])
        (row,) = found["selected"]
        (advice,) = found["entry_advice"]
        self.assertEqual((row["id"], row["route"], row["required"], row["need_id"]),
                         ("mcp:beta/lookup", "parent-relay", False, CLASS_NEED["id"]))
        self.assertEqual((row["reason"], row["advice"], row["by"], row["deferred"]),
                         ("library-docs, chosen by Jev", advice["id"], CLASS_NEED["by"], []))
        # Two judgements changed this plan (the class and the entry), so no single judgement_ref stands for it.
        self.assertEqual(found["jev"], {"status": "used", "applied": True, "judgement_ref": None})
        self.assertEqual(found["class_advice_refs"], [CLASS_NEED["advice_ref"]])
        self.assertIn({"id": CLASS_NEED["id"], "class": "library-docs", "capability_id": None, "required": False,
                       "origin": "jev"}, found["needs"])
        self.assertEqual(found["prep_id"], self.assess()["prep_id"])  # class needs stay out of the prep identity

    def test_without_a_fitting_answer_the_first_entry_stands_and_nothing_refuses(self):
        found = self.assess([CLASS_NEED], class_status="used", advise=lambda need, request: {
            "status": "used", "applied": False, "choice": None, "reason": "no-match", "judgement_ref": JUDGEMENT})
        (row,) = found["selected"]
        self.assertEqual((row["id"], row["reason"]), ("mcp:alpha/lookup", "library-docs, first in configuration order"))
        self.assertEqual([(item["outcome"], item["reason"]) for item in found["entry_advice"]],
                         [("no-match", "no-match")])
        # Only the class judgement changed the plan; the entry judgement is kept in its advice record.
        self.assertEqual(found["jev"], {"status": "used", "applied": True, "judgement_ref": CLASS_NEED["advice_ref"]})
        self.assertEqual(found["entry_advice"][0]["judgement_ref"], JUDGEMENT)
        empty = self.assess([CLASS_NEED], lead=())
        self.assertEqual((empty["selected"], [item["outcome"] for item in empty["entry_advice"]]),
                         ([], ["no-candidate"]))

    def test_an_entry_a_skill_already_routes_is_not_listed_twice(self):
        skill_need = {"capability_id": "mcp:alpha/lookup", "class": None, "required": False, "origin": "skill",
                      "by": ["skill:docs"], "id": "sha256:" + "9" * 64}
        found = self.assess([CLASS_NEED], needs=[skill_need])
        skill_id = next(need["id"] for need in found["needs"] if need["origin"] == "skill")
        self.assertEqual([(row["id"], row["need_id"]) for row in found["selected"]], [("mcp:alpha/lookup", skill_id)])
        self.assertEqual(found["entry_advice"][0]["outcome"], "off")

    def test_docs_links_are_checked_when_read(self):
        from oah.capabilities import plan
        from oah.config import ConfigError

        self.assertEqual(plan.check_settings({"docs": {"mcp:docs/lookup": "docs:document-text"}})["docs"],
                         {"mcp:docs/lookup": "docs:document-text"})
        for key, value in (("mcp:docs/lookup", "document-text"), ("mcp:docs/lookup", "docs:/etc/hosts"),
                           ("mcp:docs/lookup", "docs:a/../b"), ("mcp:docs/lookup", ":README.md"),
                           ("mcp:docs/lookup", "docs:"), ("mcp:docs/lookup", 3), ("context7", "docs:README.md")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError) as caught:
                    plan.check_settings({"docs": {key: value}})
                self.assertEqual(caught.exception.code, "OAH_CONFIG_INVALID")

    def test_the_plan_names_why_jev_was_not_used(self):
        self.assertEqual(self.assess()["jev"], {"status": "unused(no-class-need)", "applied": False,
                                                "judgement_ref": None})
        self.assertEqual(self.assess(class_status="unused(task-text-off)")["jev"]["status"], "unused(task-text-off)")
        self.assertEqual(self.assess(class_status="unused(plan-pure)")["jev"]["status"], "unused(plan-pure)")


DOCS = ("acme/docs", "skills/docs",
        {"LICENSE": MIT_LICENSE,
         "skills/docs/SKILL.md": skill_md("docs", "Look the API up first.\n", extra="allowed-tools: mcp__docs__lookup\n")},
        {})
RESULT = "lookup(): returns the page for a name.\n"


class RelayFlowTest(DeliveryTask, unittest.TestCase):
    """E-T19: a parent-only MCP tool needs a relay; the stored result reaches the brief as data; secrets stay out."""

    def lead_inventory(self) -> pathlib.Path:
        from oah.capabilities import inventory

        now = dt.datetime.now(dt.timezone.utc)
        entry = inventory.parent_entry("mcp:docs/lookup", schema=SCHEMA, observed=now, effects=["read-network"])
        return write(self.work / "lead-inventory.json", inventory.parent_inventory([entry], observed=now))

    def test_relay_required_then_delivered_as_data(self):
        self.build_skills(DOCS)
        claude_home(self.work / "home", self.repo)
        lead = self.lead_inventory()
        planned = self.oah("capabilities", "plan", "--task", "t1", "--stage", "initial", "--parent-inventory",
                           str(lead), "--json")
        self.assertEqual(planned.returncode, 1, planned.stdout + planned.stderr)
        self.assertTrue(planned.stderr.startswith("OAH_CAPABILITY_RELAY_REQUIRED"), planned.stderr)
        (request,) = json.loads(planned.stdout)["requests"]
        self.assertEqual((request["capability_id"], request["run"]), ("mcp:docs/lookup", None))
        refused = self.call("claude", "initial", "--parent-inventory", str(lead))
        self.assert_refused_before_any_run(refused, "OAH_CAPABILITY_RELAY_REQUIRED", planner=(
            "capabilities", "plan", "--task", "t1", "--stage", "initial", "--parent-inventory", str(lead)))
        without_lead = self.call()
        self.assertEqual(without_lead.returncode, 1)
        self.assertTrue(without_lead.stderr.startswith("OAH_CAPABILITY_UNAVAILABLE(not-configured)"),
                        without_lead.stderr)

        request_file = write(self.work / "request.json", request)
        result_file = write(self.work / "result.txt", RESULT)
        invocation = write(self.work / "invocation.json", {"outcome": "ok", "at": request["created_at"],
                                                           "arguments_digest": "sha256:" + "3" * 64})
        worker = self.oah("capabilities", "relay", "--request", str(request_file), "--capability", "mcp:docs/lookup",
                          "--result-file", str(result_file), "--invocation", str(invocation),
                          env={"OAH_WORKER": "1", "OAH_RUN_ID": "r20261109T000000Z-00000000"})
        self.assertEqual(worker.returncode, 1)
        self.assertIn("OAH_SKILLS_WORKER_REFUSED", worker.stderr)
        stored = self.oah("capabilities", "relay", "--request", str(request_file), "--capability", "mcp:docs/lookup",
                          "--result-file", str(result_file), "--invocation", str(invocation), "--json")
        self.assertEqual(stored.returncode, 0, stored.stderr)
        relay = json.loads(stored.stdout)["id"]
        again = self.oah("capabilities", "relay", "--request", str(request_file), "--capability", "mcp:docs/lookup",
                         "--result-file", str(result_file), "--invocation", str(invocation), "--json")
        self.assertEqual(json.loads(again.stdout)["id"], relay)  # an identical retry is the same record

        replanned = self.oah("capabilities", "plan", "--task", "t1", "--stage", "initial", "--parent-inventory",
                             str(lead), "--relay-result", relay, "--json")
        self.assertEqual(replanned.returncode, 0, replanned.stderr)
        self.assertEqual(json.loads(replanned.stdout)["plan"]["prep_id"], request["prep_id"])
        self.assertEqual(json.loads(replanned.stdout)["plan"]["jev"]["status"], "unused(plan-pure)")  # DESIGN §6.7
        result = self.call("claude", "initial", "--parent-inventory", str(lead), "--relay-result", relay)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (run,) = self.runs()
        prompt = (run / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("## Capabilities\n<!-- OAH_CAPABILITIES:BEGIN -->", prompt)
        self.assertIn("````text\n" + RESULT + "````\n", prompt)
        self.assertLess(prompt.index("<!-- OAH_SKILL_TEXTS:END -->"), prompt.index("## Capabilities"))
        recorded = json.loads((run / "capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual((recorded["prep_id"], recorded["relay_refs"]), (request["prep_id"], [relay]))
        for text in (prompt, json.dumps(recorded)):
            self.assertNotIn(PLANTED, text)

        # The isolated review never takes a relay; a changed payload refuses as tampered.
        review = self.call("codex", "review", "--parent-inventory", str(lead), "--relay-result", relay)
        self.assertEqual(review.returncode, 1)
        self.assertTrue(review.stderr.startswith("OAH_CAPABILITY_UNSUPPORTED(review-isolated)"), review.stderr)
        hexdigest = relay.split(":", 1)[1]
        record = self.state_dir / "skills" / "relays" / f"{hexdigest}.json"
        record.write_text(record.read_text(encoding="utf-8").replace('"cooperative"', '"host-observed"'),
                          encoding="utf-8")
        tampered = self.call("claude", "initial", "--parent-inventory", str(lead), "--relay-result", relay)
        self.assertEqual(tampered.returncode, 1)
        self.assertTrue(tampered.stderr.startswith("OAH_CAPABILITY_RELAY_REQUIRED(tampered)"), tampered.stderr)

    def test_a_configured_name_cannot_carry_a_required_need_on_codex(self):
        self.build_skills(DOCS)
        write(self.work / "codex-home" / "config.toml", "[mcp_servers.docs]\ncommand = \"d\"\n")
        result = self.call("codex", "correction")
        self.assert_refused_before_any_run(result, "OAH_CAPABILITY_UNSUPPORTED(unknown-schema)", planner=(
            "capabilities", "plan", "--task", "t1", "--stage", "correction", "--engine", "codex"))

    def test_show_lists_the_child_inventory_without_secrets(self):
        self.build_skills()
        claude_home(self.work / "home", self.repo)
        shown = self.oah("capabilities", "show", "--engine", "claude", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        ids = {entry["id"] for entry in json.loads(shown.stdout)["entries"]}
        self.assertIn("mcp:context7/*", ids)
        self.assertNotIn(PLANTED, shown.stdout)
        text = self.oah("capabilities", "show", "--engine", "claude")
        self.assertIn("mcp:waiting/*", text.stdout)
        self.assertIn("pending", text.stdout)


LOOKUP_AC = "look up the parameters and return values of the date parser function"
UPGRADE_AC = "migrate the date parser code between library versions 2 and 3"
FLOW_TEXT = packet_text("t1", acs=(LOOKUP_AC, UPGRADE_AC))
REFERENCE_URL, UPGRADE_URL = "https://docs.example.org/reference", "https://docs.example.org/upgrades"
PAGES = {REFERENCE_URL: "Reference lookup: returns the parameters, return values and examples of one library "
                        "function.\n",
         UPGRADE_URL: "Upgrade guides: explain what changed between two library versions and how to migrate code.\n"}
DOC_LINKS = {"mcp:refdocs/lookup": ("refdocs", REFERENCE_URL), "mcp:guides/upgrade": ("guides", UPGRADE_URL)}


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


class CapabilitySelectFlowTest(TaskRepo, unittest.TestCase):
    """AC18 (DESIGN §6.3.4, §7.1.1 fixture): two qualified library-docs entries with distinct linked public slices
    and two non-design subtask needs with different intent spans, through the real `oah call`. With send_task_text
    the class judgement and the entry Choice each read only that need's own span; the bounded requests and the
    selected entries differ per need, so class=true alone cannot pass. Nothing is asked under the launch lock."""

    def setUp(self):
        super().setUp()
        from oah.design.sources.adapters import Transport
        from oah.design.sources.check import Sources
        from oah.design.sources.model import build_spec
        from tests.test_design_sources import Clock, Docs

        docs = Docs(Clock())
        docs.pages = dict(PAGES)
        sources = Sources(self.state_dir, repo_root=self.repo, transport=Transport(getter=docs))
        for source_id, url in DOC_LINKS.values():
            spec = sources.register(build_spec(url=url, source_id=source_id))[1]
            self.assertEqual(sources.check_one(spec).result, "changed")
        self.asked: list[tuple[bool, dict]] = []
        self.fits = 0.9  # the Choice's own fits answer
        self.jev_url = FakeServer(self, self.respond).url()

    def respond(self, path, body):
        """Jev for this fixture: library-docs fits every need; the Choice takes the excerpt sharing most words with
        task_need. Any other question gets HTTP 400, which leaves the breaker alone."""
        self.asked.append((self.lock_free(), body))
        state, questions = body.get("state") or {}, body.get("questions") or {}
        if state.get("purpose") != "capability-needs":
            return 400, {"error": "this fixture answers capability questions only"}, 0, {}
        fields = state["fields"]
        if "choice" not in questions:
            answers = {name: {"type": "noul", "noul": 0.9 if name == "library-docs" else 0.1} for name in questions}
        else:
            criteria = sorted(questions["choice"]["criteria"])
            overlap = {key: len(words(fields["task_need"]) & words(fields[f"{key}:capability_excerpt"]))
                       for key in criteria}
            option = max(criteria, key=lambda key: overlap[key])
            rest = 0.2 / (len(criteria) - 1)
            answers = {"choice": {"type": "choice", "choice": option, "confidence": 0.8,
                                  "probabilities": {key: 0.8 if key == option else rest for key in criteria}},
                       "fits": {"type": "noul", "noul": self.fits}}
        return 200, {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 1}}, 0, {}

    def capability_requests(self) -> list[tuple[bool, dict]]:
        return [(free, body) for free, body in self.asked
                if (body.get("state") or {}).get("purpose") == "capability-needs"]

    def start(self, *, task_text: bool = True) -> None:
        toml = "[" + ", ".join(json.dumps(part) for part in (sys.executable, str(FAKES / "fake_claude.py"))) + "]"
        links = ", ".join(f'"{capability}" = "{source}:document-text"'
                          for capability, (source, _url) in DOC_LINKS.items())
        config = ("[models]\nfetch_scores = false\n"
                  f"[engines.claude]\ncommand = {toml}\nquality_order = [\"opus\", \"sonnet\"]\n"
                  f"[rerank]\nenabled = true\nprovider = \"typesafe\"\n[rerank.typesafe]\nurl = \"{self.jev_url}\"\n"
                  f"[rerank.judgement]\nenabled = true\nsend_task_text = {'true' if task_text else 'false'}\n"
                  f"[capabilities]\ndocs = {{ {links} }}\n")
        git_commit(self.repo, {".oah/tasks/t1/PACKET.md": FLOW_TEXT, "src/app.py": "x = 1\n",
                               ".oah/config.toml": config}, "packet")
        code, out, err = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", ".oah/tasks/t1/PACKET.md",
                                  "--scope", "src/")
        self.assertEqual(code, 0, out + err)

    def needs(self) -> tuple[pathlib.Path, dict[str, str]]:
        """Two subtask needs over the two success-condition lines (byte ranges of the packet's intent section)."""
        from oah.discovery import store, tasks

        (whole,) = self.find_task("--subtask", "lookup", out="whole.json")["needs"]
        data = FLOW_TEXT.encode("utf-8")
        items, ids = [], {}
        for subtask, text in (("lookup", LOOKUP_AC), ("upgrade", UPGRADE_AC)):
            offset, size = data.index(text.encode("utf-8")), len(text.encode("utf-8"))
            requirement = {"source": "packet-intent", "offset": offset, "length": size,
                           "sha256": "sha256:" + hashlib.sha256(data[offset:offset + size]).hexdigest()}
            item = store.with_id(dict(whole, subtask=subtask, requirement=requirement))
            items.append(item)
            ids[subtask] = item["id"]
        return write(self.files / "needs.json", tasks.needs_file("t1", items)), ids

    def lead(self) -> pathlib.Path:
        from oah.capabilities import inventory

        now = dt.datetime.now(dt.timezone.utc)
        entries = [inventory.parent_entry(capability, schema=dict(SCHEMA, name=capability), observed=now,
                                          effects=["read-network"], cls="library-docs") for capability in DOC_LINKS]
        return write(self.work / "lead-inventory.json", inventory.parent_inventory(entries, observed=now))

    def dispatch(self, needs: pathlib.Path) -> tuple[int, str, str]:
        self.record.unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": fake_key()}):
            return self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low", "--skill-needs",
                            str(needs), "--parent-inventory", str(self.lead()))

    def test_each_subtask_need_gets_the_entry_its_own_span_needs(self):
        self.start()
        needs, ids = self.needs()
        code, out, err = self.dispatch(needs)
        self.assertEqual(code, 0, out + err)
        (run,) = self.runs()
        plan = json.loads((run / "capabilities.json").read_text(encoding="utf-8"))
        rows = {row["id"]: row for row in plan["selected"]}
        # Rule order would give both needs mcp:guides/upgrade (o0); each need's own span moved one of them.
        self.assertEqual({capability: row["by"] for capability, row in rows.items()},
                         {"mcp:refdocs/lookup": [f"need:{ids['lookup'][7:19]}"],
                          "mcp:guides/upgrade": [f"need:{ids['upgrade'][7:19]}"]})
        self.assertEqual({(row["route"], row["required"], row["reason"]) for row in rows.values()},
                         {("parent-relay", False, "library-docs, chosen by Jev")})
        self.assertEqual([(item["outcome"], item["selected_option"]) for item in plan["entry_advice"]],
                         [("selected", "o1"), ("selected", "o0")])
        self.assertEqual((plan["jev"]["status"], plan["jev"]["applied"], len(plan["class_advice_refs"])),
                         ("used", True, 2))
        self.assertEqual(sorted((need["class"], need["origin"]) for need in plan["needs"]),
                         [("library-docs", "jev"), ("library-docs", "jev")])

        sent = self.capability_requests()
        self.assertTrue(sent and all(free for free, _body in sent))  # never under the launch lock
        labels = [body["state"]["fields"] for _free, body in sent if "choice" not in body["questions"]]
        choices = [body["state"]["fields"] for _free, body in sent if "choice" in body["questions"]]
        self.assertEqual(labels, [{"task_need": LOOKUP_AC}, {"task_need": UPGRADE_AC}])
        excerpts = {"o0:capability_excerpt": PAGES[UPGRADE_URL], "o1:capability_excerpt": PAGES[REFERENCE_URL]}
        self.assertEqual(choices, [{"capability_class": "library-docs", "task_need": LOOKUP_AC, **excerpts},
                                   {"capability_class": "library-docs", "task_need": UPGRADE_AC, **excerpts}])
        for _free, body in sent:
            self.assertNotIn("Reports miss files", json.dumps(body))  # only the need's own span leaves
        brief = self.stdin()
        self.assertIn("- mcp:refdocs/lookup: parent-relay (library-docs, chosen by Jev), optional", brief)
        self.assertIn("- mcp:guides/upgrade: parent-relay (library-docs, chosen by Jev), optional", brief)

    def test_linked_evidence_reads_only_a_verified_file_linked_to_that_exact_id(self):
        from oah.capabilities import select

        evidence = select.linked_evidence({"mcp:refdocs/lookup": "refdocs:document-text",
                                           "mcp:other/lookup": "missing:document-text",
                                           "mcp:guides/upgrade": "guides:README.md"}, self.repo)
        field = evidence("mcp:refdocs/lookup")
        self.assertEqual((field.text, field.path_or_field, field.offset), (PAGES[REFERENCE_URL], "document-text", 0))
        self.assertIsNone(evidence("mcp:refdocs/other"))   # an ID is never matched by its name
        self.assertIsNone(evidence("mcp:other/lookup"))    # no such registered source
        self.assertIsNone(evidence("mcp:guides/upgrade"))  # the snapshot holds no such file

    def test_an_entry_answer_that_fits_nothing_keeps_the_first_entry(self):
        self.fits = 0.1  # a Choice is read, but its fits question says no option fits the need
        self.start()
        needs, _ids = self.needs()
        code, out, err = self.dispatch(needs)
        self.assertEqual(code, 0, out + err)
        (run,) = self.runs()
        plan = json.loads((run / "capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual([(item["outcome"], item["reason"], item["applied"]) for item in plan["entry_advice"]],
                         [("no-match", "no-match", False)] * 2)
        self.assertEqual([(row["id"], row["reason"]) for row in plan["selected"]],
                         [("mcp:guides/upgrade", "library-docs, first in configuration order")])
        self.assertEqual(len(self.capability_requests()), 4)  # two class questions, two entry Choices

    def test_without_permission_for_task_text_nothing_is_asked_and_no_plan_is_made(self):
        self.start(task_text=False)
        needs, _ids = self.needs()
        code, out, err = self.dispatch(needs)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.capability_requests(), [])
        (run,) = self.runs()
        self.assertFalse((run / "capabilities.json").exists())
        self.assertNotIn("## Capabilities", self.stdin())



DROP = object()
BUNDLE = "sha256:" + "b" * 64
ENTRY = "sha256:" + "e" * 64
WALL = dt.datetime(2026, 11, 9, 12, 0, 5, tzinfo=dt.timezone.utc)


def stream(*events, crlf: bool = False) -> bytes:
    """Stream-json lines as Claude Code writes them: one UTF-8 JSON object per line, never ASCII-escaped."""
    end = b"\r\n" if crlf else b"\n"
    return b"".join((event if isinstance(event, bytes) else json.dumps(event, ensure_ascii=False).encode("utf-8"))
                    + end for event in events)


def use(tool_use_id: str, name: str, arguments: dict | None = None, *, session: str = "s1") -> dict:
    return {"type": "assistant", "session_id": session, "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": arguments or {"q": "x"}}]}}


def answer(tool_use_id: str, content="found", *, error=None, session: str = "s1") -> dict:
    part = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if error is not None:
        part["is_error"] = error
    return {"type": "user", "session_id": session, "parent_tool_use_id": None, "message": {"content": [part]}}


def done(*denied: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s1",
            "permission_denials": [{"tool_name": "mcp__refdocs__lookup", "tool_use_id": tool_use_id, "tool_input": {}}
                                   for tool_use_id in denied]}


class EventSinkTest(unittest.TestCase):
    """E-T20 (DESIGN §6.5): EventSinkV1 reads one Claude stream-json stdout incrementally under fixed bounds. The
    first event must be an init that matches the run; every header problem asks the runner to stop with its type;
    each capability tool use pairs with its own result by exact id, and only digests are kept."""

    def setUp(self):
        self.cwd = os.path.realpath(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = [100.0]

    def sink(self, *, planned: dict | None = None, timeout_s: float = 600.0, **limits):
        from oah.capabilities import inventory

        return inventory.EventSink(run="r1", cwd=self.cwd, cli_version="2.1.318", permission_mode="acceptEdits",
                                   planned=planned or {}, bundle_id=BUNDLE, timeout_s=timeout_s,
                                   clock=lambda: self.now[0], wall=lambda: WALL, **limits)

    def init(self, **changes) -> dict:
        event = {"type": "system", "subtype": "init", "session_id": "s1", "cwd": self.cwd,
                 "tools": ["Read", "mcp__refdocs__lookup", "mcp__other__fetch"],
                 "mcp_servers": [{"name": "refdocs", "status": "connected"}, {"name": "other", "status": "failed"}],
                 "model": "claude-opus-5-5", "permissionMode": "acceptEdits", "claude_code_version": "2.1.318"}
        for key, value in changes.items():
            if value is DROP:
                event.pop(key)
            else:
                event[key] = value
        return event

    def test_a_matching_init_then_each_tool_use_pairs_with_its_own_result(self):
        from oah.capabilities import digest

        sink = self.sink(planned={"mcp:refdocs/lookup": {"entry_digest": ENTRY, "schema_digest": None}})
        data = stream(self.init(), use("t1", "mcp__refdocs__lookup", {"q": "slots"}), answer("t1", "found it"),
                      use("t2", "mcp__other__fetch"), answer("t2", "Connection closed", error=True),
                      use("t3", "mcp__refdocs__lookup"),
                      use("t4", "mcp__refdocs__lookup"), answer("t4", "not allowed", error=True),
                      use("r1", "Read", {"file_path": "notes.md"}), answer("r1", "text"),
                      answer("t9", "a result nobody asked for"), done("t4"))
        self.assertIsNone(sink.feed("stdout", data))
        self.assertIsNone(sink.feed("stderr", b"not JSON, and never read as events\n"))
        observed = sink.finish(0, True)
        self.assertEqual((observed["status"], observed["stopped"], observed["reason"], observed["inspection"]),
                         ("observed", False, None, "complete"))
        rows = observed["invocations"]
        self.assertEqual([(row["capability_id"], row["result"], row["selection"]) for row in rows], [
            ("mcp:refdocs/lookup", "ok", "planned"), ("mcp:other/fetch", "error", "unplanned"),
            ("mcp:refdocs/lookup", "denied", "planned"), ("mcp:refdocs/lookup", "unobserved", "planned")])
        self.assertEqual(set(rows[0]), {"version", "run", "bundle_id", "capability_id", "entry_digest",
                                        "schema_digest", "session_id_digest", "tool_use_id_digest",
                                        "arguments_digest", "result", "result_sha256", "selection", "evidence", "at"})
        first = rows[0]
        self.assertEqual((first["version"], first["run"], first["bundle_id"], first["entry_digest"],
                          first["schema_digest"], first["evidence"], first["at"]),
                         (1, "r1", BUNDLE, ENTRY, None, "host-observed", "2026-11-09T12:00:05+00:00"))
        self.assertEqual(first["session_id_digest"], "sha256:" + hashlib.sha256(b"s1").hexdigest())
        self.assertEqual(first["tool_use_id_digest"], "sha256:" + hashlib.sha256(b"t1").hexdigest())
        self.assertEqual((first["arguments_digest"], first["result_sha256"]), (digest({"q": "slots"}),
                                                                              digest("found it")))
        self.assertIsNone(rows[3]["result_sha256"])  # an unanswered use proves nothing
        self.assertTrue(rows[1]["entry_digest"].startswith("sha256:") and rows[1]["entry_digest"] != ENTRY)
        text = json.dumps(observed)
        for plain in ("found it", "slots", "Connection closed", "notes.md"):
            self.assertNotIn(plain, text)  # digests, IDs and typed words only
        self.assertEqual(observed["init"]["servers"], {"refdocs": "connected", "other": "failed"})
        self.assertEqual((observed["init"]["cli_version"], observed["init"]["model"]), ("2.1.318", "claude-opus-5-5"))
        self.assertEqual(observed["digest"], digest({key: value for key, value in observed.items() if key != "digest"}))
        self.assertIs(sink.finish(0, True), observed)  # finished once

    def test_an_error_result_stays_an_error_whatever_its_text(self):
        sink = self.sink()
        sink.feed("stdout", stream(self.init(), use("t1", "mcp__refdocs__lookup"), answer("t1", "ok", error=True),
                                   use("t2", "mcp__refdocs__lookup"), answer("t2", "ok", error="true"),
                                   use("t3", "mcp__refdocs__lookup"), answer("t3", "ok", error=False)))
        self.assertEqual([row["result"] for row in sink.finish(0, True)["invocations"]], ["error", "error", "ok"])

    def test_chunks_split_anywhere_and_crlf_lines_read_the_same(self):
        events = (self.init(), use("t1", "mcp__refdocs__lookup", {"q": "예약 ✓"}), answer("t1", "결과 ✓"),
                  use("t2", "mcp__refdocs__lookup"), done())
        whole = self.sink()
        self.assertIsNone(whole.feed("stdout", stream(*events)))
        expected = whole.finish(0, True)
        for crlf in (False, True):
            data = stream(*events, crlf=crlf)
            for size in (1, 2, 3, 7, 64):
                with self.subTest(crlf=crlf, size=size):
                    sink = self.sink()
                    for start in range(0, len(data), size):
                        self.assertIsNone(sink.feed("stdout", data[start:start + size]))
                    self.assertEqual(sink.finish(0, True), expected)

    def test_every_header_problem_asks_to_stop_with_its_type(self):
        cases = {
            "the first event is not init": (stream(answer("t1")), "missing-init"),
            "a result before init": (stream(done()), "missing-init"),
            "not JSON": (b'{"type": "system", "subtype": "init"\n', "invalid-json"),
            "not an object": (b'["init"]\n', "invalid-json"),
            "not UTF-8": (b'{"type": "\xff"}\n', "invalid-json"),
            "a duplicate key": (b'{"type": "system", "type": "system"}\n', "invalid-json"),
            "NaN": (b'{"type": NaN}\n', "invalid-json"),
            "no session id": (stream(self.init(session_id=DROP)), "missing-field"),
            "an empty session id": (stream(self.init(session_id="")), "missing-field"),
            "no working directory": (stream(self.init(cwd=DROP)), "missing-field"),
            "no tool list": (stream(self.init(tools="Read")), "missing-field"),
            "no Claude Code version": (stream(self.init(claude_code_version=DROP)), "missing-field"),
            "a server without a name": (stream(self.init(mcp_servers=[{"status": "connected"}])), "missing-field"),
            "another working directory": (stream(self.init(cwd=os.path.dirname(self.cwd))), "init-mismatch"),
            "another Claude Code version": (stream(self.init(claude_code_version="2.1.309")), "init-mismatch"),
            "another permission mode": (stream(self.init(permissionMode="bypassPermissions")), "init-mismatch"),
        }
        for name, (data, reason) in cases.items():
            with self.subTest(case=name):
                sink = self.sink()
                self.assertEqual(sink.feed("stdout", data), reason)
                self.assertEqual(sink.feed("stdout", stream(self.init())), reason)  # the stop stays
                self.assertEqual(sink.overdue(), reason)
                observed = sink.finish(-9, True)
                self.assertEqual((observed["status"], observed["stopped"], observed["reason"], observed["init"]),
                                 ("failed", True, reason, None))
                self.assertTrue(observed["detail"])

    def test_the_first_event_is_bounded_before_its_line_ends(self):
        from oah.capabilities import inventory

        fitting = json.dumps(self.init(pad="")).encode("utf-8")
        exact = fitting.replace(b'"pad": ""', b'"pad": "' + b"x" * (inventory.STREAM_EVENT_LIMIT - len(fitting)) + b'"')
        self.assertEqual(len(exact), inventory.STREAM_EVENT_LIMIT)
        sink = self.sink()
        self.assertIsNone(sink.feed("stdout", exact + b"\n"))  # exactly at the limit is one event
        self.assertEqual(sink.finish(0, True)["status"], "observed")
        sink = self.sink()
        self.assertIsNone(sink.feed("stdout", exact + b"\r\n"))  # a CRLF ending is not part of the event
        sink = self.sink()
        self.assertIsNone(sink.feed("stdout", exact[:-1] + b'x"'))  # one byte over could still end in a CR
        self.assertEqual(sink.feed("stdout", b"\n"), "oversize")
        sink = self.sink()
        self.assertEqual(sink.feed("stdout", exact[:-1] + b'xx"'), "oversize")  # stopped before its newline

    def test_after_init_an_oversize_event_is_skipped_and_its_use_stays_unobserved(self):
        from oah.capabilities import inventory

        data = stream(self.init(), use("t1", "mcp__refdocs__lookup"),
                      answer("t1", "x" * inventory.STREAM_EVENT_LIMIT), use("t2", "mcp__refdocs__lookup"),
                      answer("t2", "fine"))
        sink = self.sink()
        for start in range(0, len(data), 4096):
            self.assertIsNone(sink.feed("stdout", data[start:start + 4096]))
        observed = sink.finish(0, True)
        self.assertEqual(([row["result"] for row in observed["invocations"]], observed["uninspected_events"],
                          observed["status"]), (["ok", "unobserved"], 1, "observed"))

    def test_a_contradictory_init_or_another_session_stops_but_a_repeat_does_not(self):
        sink = self.sink()
        self.assertIsNone(sink.feed("stdout", stream(self.init(), self.init())))  # the same init again
        self.assertEqual(sink.feed("stdout", stream(self.init(session_id="s2"))), "contradictory-init")
        sink = self.sink()
        self.assertEqual(sink.feed("stdout", stream(self.init(), use("t1", "mcp__refdocs__lookup", session="s2"))),
                         "other-session")

    def test_no_init_within_its_deadline_asks_to_stop(self):
        from oah.capabilities import inventory

        self.assertEqual(inventory.INIT_SECONDS, 10.0)
        sink = self.sink()
        self.now[0] += 9.9
        self.assertIsNone(sink.overdue())
        self.now[0] += 0.2
        self.assertEqual(sink.overdue(), "init-deadline")
        self.assertEqual(sink.finish(None, True)["reason"], "init-deadline")
        short = self.sink(timeout_s=3.0)  # a shorter run timeout bounds the wait
        self.now[0] += 3.1
        self.assertEqual(short.overdue(), "init-deadline")
        started = self.sink()
        self.assertIsNone(started.feed("stdout", stream(self.init())))
        self.now[0] += 3600
        self.assertIsNone(started.overdue())

    def test_bounds_close_the_oldest_open_use_and_stop_inspecting(self):
        from oah.capabilities import inventory

        self.assertEqual((inventory.STREAM_EVENT_LIMIT, inventory.STREAM_OPEN_LIMIT, inventory.STREAM_TOTAL_LIMIT),
                         (256 * 1024, 64, 16 * 1024 * 1024))
        sink = self.sink()
        sink.feed("stdout", stream(self.init(), *(use(f"t{n}", "mcp__refdocs__lookup") for n in range(65)),
                                   answer("t0", "too late")))
        rows = sink.finish(0, True)["invocations"]
        self.assertEqual(len(rows), 65)
        self.assertEqual((rows[0]["tool_use_id_digest"], rows[0]["result"]),
                         ("sha256:" + hashlib.sha256(b"t0").hexdigest(), "unobserved"))  # closed as the 65th opened
        small = self.sink(total_limit=2048)
        events = [use(f"t{n}", "mcp__refdocs__lookup") for n in range(20)]
        self.assertIsNone(small.feed("stdout", stream(self.init(), *events, self.init(session_id="s9"))))
        observed = small.finish(0, True)
        self.assertEqual((observed["inspection"], observed["status"]), ("truncated", "observed"))
        self.assertLessEqual(observed["inspected_bytes"], 2048)
        capped = self.sink(record_limit=2)
        capped.feed("stdout", stream(self.init(), *(event for n in range(3)
                                                    for event in (use(f"t{n}", "mcp__refdocs__lookup"),
                                                                  answer(f"t{n}")))))
        observed = capped.finish(0, True)
        self.assertEqual((len(observed["invocations"]), observed["omitted"]), (2, 1))



class CorrectionDrainTest(unittest.TestCase):
    """DESIGN §6.5: the correction path's byte/digest drain feeds the same EventSinkV1, and a stop request stops the
    owned tree before the child's delayed write. No correction run turns a sink on in this release (§7.4)."""

    @unittest.skipIf(os.name == "nt", "builds the POSIX tree owner directly; process runs cover the Windows Job")
    def test_a_stop_request_from_the_sink_stops_the_correction_tree_first(self):
        from types import SimpleNamespace

        from oah import call
        from oah.capabilities import inventory

        work = make_tmp_dir(self)
        sentinel = work / "effect"
        line = json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "cwd": os.path.dirname(work),
                           "tools": [], "claude_code_version": "2.1.318"})
        script = (f"import sys, time\nsys.stdin.read()\nprint({line!r}, flush=True)\ntime.sleep(10)\n"
                  f"open({str(sentinel)!r}, 'w').write('effect')\n")
        child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, cwd=work, start_new_session=True)
        tree = call._PosixTree(child)
        self.addCleanup(tree.close)
        recorded = {}
        prepared = SimpleNamespace(timeout_s=60.0, run=SimpleNamespace(result=lambda **fields: recorded.update(fields)))
        sink = inventory.EventSink(run="r1", cwd=str(work), cli_version="2.1.318", permission_mode=None, planned={},
                                   bundle_id=None, timeout_s=60.0)
        streamed = call._stream_correction(child, prepared, "brief", sink=sink)
        self.assertFalse(sentinel.exists(), "the delayed write happened: the tree was not stopped first")
        self.assertNotEqual(streamed.returncode, 0)
        self.assertEqual(sink.finish(streamed.returncode, True)["reason"], "init-mismatch")
        self.assertEqual(recorded["worker_output"]["stdout"]["bytes"], len(line) + 1)  # every byte still hashed


if __name__ == "__main__":
    unittest.main()
