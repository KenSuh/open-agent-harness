# 능력 라우팅 시험 — 가짜 홈의 설정에서 목록을 읽고(연결 주장 없음·비밀 미복사), 계획·상위 중계·의존 단계를 실제 oah 명령과 공개 API 로 본다.
"""G21/AC18 (DESIGN §6): E-T18 inventories and E-T19 plans; E-T20 (live receiving) is outside this release."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import unittest

from tests.helpers import git_commit, make_tmp_dir
from tests.test_design_sources import LICENSE as MIT_LICENSE
from tests.test_skill_delivery import DeliveryTask
from tests.test_skill_discovery import skill_md

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


if __name__ == "__main__":
    unittest.main()
