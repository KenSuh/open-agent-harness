# 작업 스킬 탐색 시험 — 하위 작업의 스킬 필요(TaskSkillNeedV1)를 만들고 다시 확인하며, 탐색·리드 선택·실제 oah call 전달과 거부를 본다.
"""G20/AC17 F06 (DESIGN §4.1, §4.8): task skill needs, task discovery and the lead's task selection.

Unit tests build needs from a synthetic packet. The flow tests drive the real CLI in this process, so a loopback
server stands in for the search endpoint and local upstream repositories for GitHub, while the recording fake engine
is a real child process. Design-bound refusals run `python -m oah` with external search turned off, so nothing can
leave the machine.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import time
import unittest
import urllib.parse
from unittest import mock

from tests.helpers import git_commit, make_tmp_dir
from tests.test_design_context import FAKES
from tests.test_intent_wiring import packet_text
from tests.test_skill_delivery import DeliveryTask, PLAIN, link_op
from tests.test_skill_discovery import DiscoveryRepo, mit_repo, skill_md

POLICY = "sha256:" + "2" * 64
INTENT = {"id": "t1", "revision": 1, "digest": "sha256:" + "1" * 64}
AXES = {"initial": ("feature", "low"), "correction": ("fix", "low"), "review": ("review", "low")}
TEXT = packet_text("t1")
MIGRATE_BODY = "Plan each MySQL schema change as an online migration.\n"
TUNE_BODY = "Read the slow query log before adding an index.\n"
MYSQL_QUERIES = ["mysql backup recovery", "mysql migrations constraints", "mysql schema design"]


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def reid(record: dict) -> dict:
    """The record with its id recomputed the documented way: sorted compact JSON of every other field."""
    body = {key: value for key, value in record.items() if key != "id"}
    return dict(body, id=sha(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                            .encode("utf-8")))


def basis(text: str = TEXT, **changes):
    from oah.discovery import tasks

    values = dict(task="t1", text=text, intent=dict(INTENT), scope=("src",), axes=dict(AXES), lease=("lease-1", 2),
                  platform="macos")
    values.update(changes)
    return tasks.TaskBasis(**values)


def raw_needs(needs: list[dict], task: str = "t1") -> bytes:
    return json.dumps({"version": 1, "task": task, "needs": needs}).encode("utf-8")


class NeedsUnitTest(unittest.TestCase):
    """The need's identity, its re-resolution against the current task and its public queries, without a repository."""

    def test_a_need_names_its_requirement_by_bytes_and_hashes_every_field(self):
        from oah.discovery import tasks

        item = tasks.need(basis(), dispatch_stage="initial", policy=POLICY, subtask="migrate",
                          tech=[("database", "mysql")], constraints=["mysql 8.0"])
        self.assertEqual(set(item), tasks.NEED_KEYS)
        self.assertEqual(item, reid(item))
        data = TEXT.encode("utf-8")
        req = item["requirement"]
        part = data[req["offset"]:req["offset"] + req["length"]]
        self.assertEqual(req["source"], "packet-intent")
        self.assertTrue(part.startswith(b"<!-- OAH_INTENT:BEGIN -->") and part.endswith(b"<!-- OAH_INTENT:END -->\n"))
        self.assertEqual(req["sha256"], sha(part))
        self.assertNotIn("Reports miss files", json.dumps(item))  # a byte range and digest, never the text
        self.assertNotIn("8.0", json.dumps(item))  # a local constraint is kept as a digest only
        self.assertEqual((item["kind"], item["risk"], item["platform"], item["design"]), ("feature", "low", "macos",
                                                                                          None))
        self.assertEqual(item["packet"], {"source_digest": sha(data), "intent": INTENT})
        self.assertEqual(item["tech"], [{"label": "database", "value": "mysql", "public_token": "mysql",
                                         "version_token": None}])
        variants = {
            "subtask": dict(subtask="tune"),
            "constraint": dict(constraints=["mysql 5.7"]),
            "scope": dict(scope=["src/app.py"]),
            "required": dict(required=True),
            "dispatch stage": dict(dispatch_stage="review"),
            "technology": dict(tech=[("database", "postgresql")]),
            "policy": dict(policy="sha256:" + "3" * 64),
        }
        ids = {item["id"]}
        for name, change in variants.items():
            with self.subTest(name):
                options = dict(dispatch_stage="initial", policy=POLICY, subtask="migrate",
                               tech=[("database", "mysql")], constraints=["mysql 8.0"])
                options.update(change)
                ids.add(tasks.need(basis(), **options)["id"])
        ids.add(tasks.need(basis(platform="windows"), dispatch_stage="initial", policy=POLICY, subtask="migrate",
                           tech=[("database", "mysql")], constraints=["mysql 8.0"])["id"])
        self.assertEqual(len(ids), len(variants) + 2)

    def test_construction_refuses_what_a_need_cannot_name(self):
        from oah.discovery import tasks

        cases = {
            "scope outside the packet": (dict(scope=["docs"]), "scope"),
            "unknown technology label": (dict(tech=[("orm", "prisma")]), "malformed"),
            "a version as a value": (dict(tech=[("database", "mysql8")]), "malformed"),
            "a design stage without a design": (dict(stage="data", check="entities"), "malformed"),
            "unknown dispatch stage": (dict(dispatch_stage="landing"), "malformed"),
            "a subtask id with spaces": (dict(subtask="query tuning"), "malformed"),
        }
        for name, (change, reason) in cases.items():
            with self.subTest(name):
                with self.assertRaises(tasks.NeedsInvalid) as caught:
                    tasks.need(basis(), **dict(dict(dispatch_stage="initial", policy=POLICY), **change))
                self.assertEqual(caught.exception.reason, reason)
                self.assertTrue(str(caught.exception).startswith(f"OAH_SKILL_NEEDS_INVALID({reason}): "))

    def test_a_lead_needs_file_is_re_resolved_and_refused_when_anything_moved(self):
        from oah.discovery import tasks

        item = tasks.need(basis(), dispatch_stage="initial", policy=POLICY, subtask="migrate",
                          tech=[("database", "mysql")], constraints=["mysql 8.0"])
        self.assertEqual(tasks.parse_needs(raw_needs([item]), basis(), policy=POLICY), [item])

        def edited(**fields) -> dict:
            data = json.loads(json.dumps(item))
            for path, value in fields.items():
                node = data
                *parents, last = path.split(".")
                for key in parents:
                    node = node[int(key)] if isinstance(node, list) else node[key]
                if isinstance(node, list):
                    node[int(last)] = value
                else:
                    node[last] = value
            return reid(data)

        moved = TEXT.replace("Reports miss files.", "Reports miss many files.")
        cases = {
            "packet changed": (raw_needs([item]), basis(text=moved), POLICY, "stale"),
            "requirement bytes": (raw_needs([edited(**{"requirement.sha256": sha(b"x")})]), basis(), POLICY, "stale"),
            "range outside the section": (raw_needs([edited(**{"requirement.length": item["requirement"]["length"]
                                                               + 10})]), basis(), POLICY, "stale"),
            "replacement text": (raw_needs([edited(**{"requirement.text": "drop every table"})]), basis(), POLICY,
                                 "malformed"),
            "foreign file": (raw_needs([item], task="t2"), basis(), POLICY, "foreign"),
            "foreign need": (raw_needs([edited(task="t2")]), basis(), POLICY, "foreign"),
            "scope outside the packet": (raw_needs([edited(scope=["docs"])]), basis(), POLICY, "scope"),
            "a version token": (raw_needs([edited(**{"tech.0.version_token": "8.0"})]), basis(), POLICY,
                                "malformed"),
            "changed kind": (raw_needs([item]), basis(axes=dict(AXES, initial=("refactor", "high"))), POLICY,
                             "stale"),
            "changed policy": (raw_needs([item]), basis(), "sha256:" + "4" * 64, "stale"),
            "another platform": (raw_needs([item]), basis(platform="linux"), POLICY, "stale"),
            "a forged id": (raw_needs([dict(item, id="sha256:" + "5" * 64)]), basis(), POLICY, "stale"),
            "the need twice": (raw_needs([item, item]), basis(), POLICY, "malformed"),
            "too many needs": (raw_needs([item] * 65), basis(), POLICY, "malformed"),
            "no needs": (raw_needs([]), basis(), POLICY, "malformed"),
            "an extra field": (raw_needs([dict(item, note="x")]), basis(), POLICY, "malformed"),
            "not JSON": (b"{", basis(), POLICY, "malformed"),
            "over 64 KiB": (b" " * 65537, basis(), POLICY, "malformed"),
        }
        for name, (raw, current, policy, reason) in cases.items():
            with self.subTest(name):
                with self.assertRaises(tasks.NeedsInvalid) as caught:
                    tasks.parse_needs(raw, current, policy=policy)
                self.assertEqual(caught.exception.reason, reason, str(caught.exception))
        # The lead's own constraint digest is local: a needs file may carry another one, which is another need.
        other = edited(**{"compatibility.constraints_digest": sha(b'["mysql 5.7"]')})
        self.assertNotEqual(other["id"], item["id"])
        self.assertEqual(tasks.parse_needs(raw_needs([other]), basis(), policy=POLICY), [other])

    def test_queries_use_public_tokens_only(self):
        from oah.discovery import tasks

        def texts(**options):
            public, local = tasks.queries(tasks.need(basis(), dispatch_stage="initial", policy=POLICY, **options))
            return sorted(query["text"] for query in public), sorted(item["template"] for item in local)

        self.assertEqual(texts(tech=[("database", "mysql")]), (MYSQL_QUERIES, []))
        self.assertEqual(texts(tech=[("database", "other:acme-ledger")]),
                         ([], ["{database} backup recovery", "{database} migrations constraints",
                               "{database} schema design"]))
        self.assertEqual(texts(tech=[("database", "none")])[0], [])
        self.assertEqual(texts(tech=[("auth", "passkey"), ("platform", "supabase")])[0],
                         ["passkey authorization patterns", "supabase row level security"])
        self.assertEqual(texts(), ([], []))  # no technology: nothing to search, nothing to answer locally

    def test_a_design_bound_need_follows_the_coverage_and_a_new_database_changes_only_its_queries(self):
        from oah.discovery import tasks

        def record(database: str) -> dict:
            need = lambda check, required, dispatch, **extra: dict(  # noqa: E731
                {"id": f"n-{check}", "check": check, "dispatch_stages": dispatch, "invocation": "model",
                 "condition": "always", "required": required}, **extra)
            return {"tech": {"database": {"value": database, "decision": None, "note": None},
                             "ui": {"value": "web", "decision": None, "note": None}},
                    "stages": [{"id": "data", "needs": [need("entities", True, ["initial"])]},
                               {"id": "quality", "needs": [need("accessibility", False, ["initial", "review"]),
                                                           need("observability", False, ["correction"])]},
                               {"id": "server", "needs": [need("secrets", True, ["initial"], invocation="user-only")]}]}

        def derived(database: str) -> dict:
            design = {"ref": {"project": "acme", "design": "booking", "revision": 2, "digest": sha(b"r2")},
                      "coverage_basis": sha(database.encode()), "record": record(database)}
            needs = tasks.derive(basis(design=design, bound=True), dispatch_stage="initial", policy=POLICY)
            return {(item["design"]["stage"], item["design"]["check"]): item for item in needs}

        pg, my = derived("postgresql"), derived("mysql")
        self.assertEqual(set(pg), {("data", "entities"), ("quality", "accessibility")})  # no user-only, no other stage
        self.assertTrue(pg[("data", "entities")]["required"])
        self.assertFalse(pg[("quality", "accessibility")]["required"])
        query = lambda item: [entry["text"] for entry in tasks.queries(item)[0]]  # noqa: E731
        self.assertEqual(query(pg[("data", "entities")]), ["postgresql schema design"])
        self.assertEqual(query(my[("data", "entities")]), ["mysql schema design"])
        self.assertNotEqual(pg[("data", "entities")]["id"], my[("data", "entities")]["id"])
        # The unrelated quality need keeps its query (its cache entry stays valid); the new coverage basis still
        # names another need, so a selection made against the old coverage is never reused.
        self.assertEqual(query(pg[("quality", "accessibility")]), query(my[("quality", "accessibility")]))
        self.assertEqual(query(my[("quality", "accessibility")]), ["web accessibility audit"])
        # A design-bound task without an accepted coverage part derives nothing to search.
        self.assertEqual(tasks.derive(basis(bound=True), dispatch_stage="initial", policy=POLICY), [])
        self.assertEqual(len(tasks.derive(basis(), dispatch_stage="initial", policy=POLICY)), 1)

    def test_a_cached_query_is_served_after_the_request_budget_is_spent(self):
        import datetime as dt

        from oah import call, intent, skillpack
        from oah.design.sources import web
        from oah.discovery import providers, store, tasks

        state = make_tmp_dir(self) / "state"
        now = dt.datetime.now(dt.timezone.utc)
        cached = "mysql backup recovery"
        store.put_query(state, {"version": 1, "query_id": store.query_id(providers.PROVIDER, cached, providers.PARSER),
                                "provider": providers.PROVIDER, "query": cached, "parser": providers.PARSER,
                                "checked_at": now.isoformat(timespec="seconds"), "outcome": "zero-results",
                                "detail": "no results", "count": 0, "hits": [], "origin": "template", "choice": None,
                                "purpose": {}})
        sent = []

        def getter(url, **options):
            sent.append(urllib.parse.unquote(url.rsplit("=", 1)[1]))
            return web.WebResult(200, url, {"Content-Type": "application/json"}, b'{"skills": []}')

        settings = dict(providers.DEFAULTS, max_queries=1)
        class CachedTask(TaskRepo, unittest.TestCase):
            pass

        fixture = CachedTask()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start_task()
        config = fixture.repo / ".oah/config.toml"
        git_commit(fixture.repo, {".oah/config.toml": config.read_text(encoding="utf-8") +
                                  "\n[skills.discovery]\nmax_queries=1\n"}, "one query fixture")
        from oah.discovery import policy_digest

        with contextlib.chdir(fixture.repo):
            holder = call.bound_lease(fixture.state_dir, "t1")
            packet = call.read_packet(fixture.repo, fixture.repo / ".oah/tasks/t1/PACKET.md",
                                     parse_intent=intent.parse, read_source=intent.read_source)
            task_design, _ = call._task_design(holder, packet, state_dir=fixture.state_dir, stage="initial",
                                              task="t1", engine="claude")
            current = call.task_basis(holder, task_design, packet, root=fixture.repo)
            item = tasks.need(current, dispatch_stage="initial", policy=policy_digest(settings),
                              tech=[("database", "mysql")])
            admitted = call.admit_task_discovery(call.CallRequest(engine="claude", stage="initial", task="t1"),
                [item], root=fixture.repo, state_dir=fixture.state_dir, env=os.environ).admitted
            with mock.patch.object(providers, "GETTER", getter), contextlib.redirect_stderr(io.StringIO()):
                (found,) = tasks.prepare_task(admitted, [item], state_dir=state, settings=settings, now=now,
                                              basis=current)
        self.assertEqual(sent, ["mysql schema design"])  # one request: the budget
        self.assertEqual([item["query"] for item in found["deferred"]], ["mysql migrations constraints"])
        self.assertEqual([(search["query"], search["cached"]) for search in found["searches"]],
                         [("mysql schema design", False), (cached, True)])
        self.assertEqual((found["outcome"], found["gaps"]), ("zero-results", []))

    def test_a_task_bound_to_a_design_takes_no_task_selection(self):
        from oah.discovery import tasks

        item = tasks.need(basis(), dispatch_stage="initial", policy=POLICY)
        with self.assertRaises(tasks.NeedsInvalid) as caught:  # refused before anything is read or written
            tasks.select(pathlib.Path("unused"), item, ["sha256:" + "9" * 64], basis=basis(bound=True))
        self.assertEqual(caught.exception.reason, "design-bound")

    def test_a_proposal_notice_names_the_one_route_that_can_deliver_it(self):
        from oah import call
        from oah.discovery import tasks

        free = tasks.need(basis(), dispatch_stage="initial", policy=POLICY)
        self.assertFalse(tasks.design_bound(basis(), free))
        self.assertTrue(tasks.design_bound(basis(bound=True), free))  # bound to a design that is not accepted yet
        entry = {"need_id": free["id"], "gaps": [], "proposed_reviews": ["sha256:" + "b" * 64],
                 "selection_ref": None, "outcome": "ok"}
        for bound, route, other in ((set(), "oah skills select", "coverage"),
                                    ({free["id"]}, "link one in the design's coverage", "oah skills select")):
            with self.subTest(bound=bool(bound)):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    call._discovery_notices("t1", [entry], bound=bound)
                self.assertIn(route, err.getvalue())
                self.assertNotIn(other, err.getvalue())

    def test_task_discovery_runs_only_under_this_tasks_dispatch_admission(self):
        import datetime as dt

        from oah import skillpack
        from oah.discovery import providers, tasks

        item = tasks.need(basis(), dispatch_stage="initial", policy=POLICY, tech=[("database", "mysql")])
        now = dt.datetime.now(dt.timezone.utc)
        sent = []
        with mock.patch.object(providers, "GETTER", side_effect=lambda *a, **k: sent.append(a)):
            for admitted in (None,
                             skillpack.Admitted(skillpack.Operation("dispatch", task="t2", lease=("lease-1", 2),
                                                                    max_queries=4))):
                with self.subTest(admitted=admitted), self.assertRaises(skillpack.PackRefused) as caught:
                    tasks.prepare_task(admitted, [item], state_dir=pathlib.Path("unused"),
                                       settings=dict(providers.DEFAULTS), now=now, basis=basis())
                self.assertEqual(caught.exception.reason, "admission")
            with self.assertRaises(ValueError):
                tasks.prepare_task(skillpack.explore(4), [item], state_dir=pathlib.Path("unused"),
                                   settings=dict(providers.DEFAULTS), now=now, basis=basis())
            # A dispatch admitted with no request budget (a plan or a correction) sends nothing either.
            with self.assertRaises(skillpack.PackRefused) as caught:
                providers.search("mysql schema design", state_dir=pathlib.Path("unused"),
                                 settings=dict(providers.DEFAULTS), now=now,
                                 admitted=skillpack.Admitted(skillpack.Operation("dispatch", task="t1",
                                                                                 lease=("lease-1", 2))))
            self.assertEqual(caught.exception.reason, "admission")
        self.assertEqual(sent, [])


class TaskRepo(DiscoveryRepo):
    """An accepted, intent-bound packet with no design and a started lease; the CLI runs in this process."""

    def setUp(self):
        super().setUp()
        self.work = make_tmp_dir(self)
        self.record = self.work / "record.json"
        codex_home = self.work / "codex-home"
        codex_home.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"FAKE_RECORD": str(self.record),
                                                       "FAKE_CLAUDE_VERSION": "2.1.318",
                                                       "CODEX_HOME": str(codex_home), "PYTHONIOENCODING": "utf-8"}))
        self.state_dir = self.state(self.repo)

    def start_task(self, text: str = TEXT) -> None:
        toml = "[" + ", ".join(json.dumps(part) for part in (sys.executable, str(FAKES / "fake_claude.py"))) + "]"
        config = ("[models]\nfetch_scores = false\n"
                  f"[engines.claude]\ncommand = {toml}\nquality_order = [\"opus\", \"sonnet\"]\n")
        git_commit(self.repo, {".oah/tasks/t1/PACKET.md": text, "src/app.py": "x = 1\n", ".oah/config.toml": config},
                   "packet")
        code, out, err = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", ".oah/tasks/t1/PACKET.md",
                                  "--scope", "src/")
        self.assertEqual(code, 0, out + err)

    def find_task(self, *args: str, out: str | None = None, expect: int = 0) -> dict:
        extra = ["--out", str(self.files / out)] if out else []
        code, stdout, err = self.skills("find", "--task", "t1", *args, *extra, "--json", expect=expect)
        return json.loads(stdout) if stdout.strip() else {"stderr": err}

    def select(self, needs: str, *reviews: str, expect: int = 0) -> tuple[dict | None, str]:
        args = [part for review in reviews for part in ("--review", review)]
        code, out, err = self.skills("select", "--needs", str(self.files / needs), *args, "--json", expect=expect)
        return (json.loads(out) if out.strip() else None), err

    def call(self, *extra: str) -> tuple[int, str, str]:
        self.record.unlink(missing_ok=True)
        return self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low", "--no-rerank", *extra)

    def runs(self) -> list[pathlib.Path]:
        folder = self.state_dir / "runs" / "t1"
        return sorted(path for path in folder.iterdir() if path.is_dir()) if folder.is_dir() else []

    def stdin(self) -> str:
        return json.loads(self.record.read_text(encoding="utf-8"))["stdin"]

    def lock_free(self) -> bool:
        """Whether the lease store's lock (the final launch guard's lock) is free right now."""
        from oah.lease import Store
        from oah.lockfile import LockTimeout, StateLock

        try:
            with StateLock(Store(self.state_dir).lock_path, timeout=0):
                return True
        except LockTimeout:
            return False

    def probe_requests(self) -> list[str]:
        """Each external request records its query; one sent while the launch lock is held records `locked:`."""
        from oah.discovery import providers

        seen: list[str] = []
        real = providers.GETTER

        def getter(url, **options):
            seen.append(("" if self.lock_free() else "locked:") + urllib.parse.unquote(url.rsplit("=", 1)[1]))
            return real(url, **options)

        self.enterContext(mock.patch.object(providers, "GETTER", getter))
        return seen


class TaskDiscoveryTest(TaskRepo, unittest.TestCase):
    """F06: real CLI and child, MySQL migration versus query tuning, cache reuse, versions and stale needs."""

    def mysql_upstream(self) -> None:
        self.upstream("acme/db", mit_repo({"skills/mysql-migrate": skill_md("mysql-migrate", MIGRATE_BODY),
                                           "skills/mysql-tune": skill_md("mysql-tune", TUNE_BODY)}))
        self.server.hits("mysql migrations constraints", ("mysql-migrate", "acme/db"))
        self.server.hits("mysql schema design", ("mysql-tune", "acme/db"), ("mysql-migrate", "acme/db"))

    def test_a_plain_call_derives_a_local_need_and_sends_nothing(self):
        self.start_task()
        code, out, err = self.call()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.server.requests, [])
        self.assertNotIn("## Skill texts", self.stdin())
        self.assertFalse((self.state_dir / "skills" / "task-selections").exists())
        self.assertFalse((self.state_dir / "skills" / "task-discovery").exists())  # nothing to search or answer
        self.assertNotIn("OAH_SKILL_DISCOVERY", err)

    def test_two_mysql_subtasks_find_select_and_deliver_their_own_skill(self):
        self.mysql_upstream()
        self.start_task()
        with contextlib.ExitStack() as held:
            from oah.lease import Store
            from oah.lockfile import StateLock

            held.enter_context(StateLock(Store(self.state_dir).lock_path))
            self.assertFalse(self.lock_free())  # the probe sees a held launch lock
        self.assertTrue(self.lock_free())
        requests = self.probe_requests()

        migrate = self.find_task("--subtask", "migrate", "--tech", "database=mysql", "--constraint",
                                 "mysql 8.0 online DDL", out="migrate.json")
        self.assertEqual(sorted(self.server.requests), MYSQL_QUERIES)
        self.assertEqual((migrate["requests"], migrate["budget"]), (3, 4))
        self.assertEqual(migrate["operation"], {"kind": "dispatch", "task": "t1", "lease": migrate["operation"]["lease"],
                                                "design": None})
        tune = self.find_task("--subtask", "tune", "--tech", "database=mysql", out="tune.json")
        self.assertEqual(sorted(self.server.requests), MYSQL_QUERIES)  # the second subtask's queries were cached
        self.assertEqual(tune["requests"], 0)
        (m_need,), (t_need,) = migrate["needs"], tune["needs"]
        self.assertNotEqual(m_need["id"], t_need["id"])
        self.assertEqual((m_need["subtask"], t_need["subtask"], m_need["design"]), ("migrate", "tune", None))
        self.assertNotIn("online DDL", json.dumps(migrate))
        self.assertNotIn("Reports miss files", json.dumps(migrate))
        self.assertFalse(any("8.0" in query or "migrate" in query or "tune" in query
                             for query in self.server.requests))
        written = json.loads((self.files / "migrate.json").read_text(encoding="utf-8"))
        self.assertEqual(written, {"version": 1, "task": "t1", "needs": [m_need]})
        (m_found,) = migrate["discovery"]
        self.assertEqual((m_found["need_id"], m_found["outcome"], m_found["selection_ref"], m_found["gaps"]),
                         (m_need["id"], "ok", None, []))
        self.assertEqual(sorted(m_found["query_ids"]), sorted(search["query_id"] for search in m_found["searches"]))

        def query_id(found: dict, text: str) -> str:
            return next(search["query_id"] for search in found["searches"] if search["query"] == text)

        migrate_review = self.review_json("--hit", f"{query_id(m_found, 'mysql migrations constraints')}:0")[0]
        tune_review = self.review_json("--hit", f"{query_id(tune['discovery'][0], 'mysql schema design')}:0")[0]
        self.assertEqual((migrate_review["frontmatter"]["name"], tune_review["frontmatter"]["name"]),
                         ("mysql-migrate", "mysql-tune"))

        # Discovery proposes reviewed hits for each need; nothing is delivered until the lead selects.
        again = self.find_task("--subtask", "migrate", "--tech", "database=mysql", "--constraint",
                               "mysql 8.0 online DDL")
        self.assertIn(migrate_review["id"], again["discovery"][0]["proposed_reviews"])
        code, out, err = self.call("--skill-needs", str(self.files / "migrate.json"))
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("## Skill texts", self.stdin())
        self.assertIn("OAH_SKILL_DISCOVERY", err)  # the lead is told a reviewed skill waits for a selection
        self.assertIn("oah skills select", err)

        selected, _ = self.select("migrate.json", migrate_review["id"])
        self.assertEqual((selected["need_id"], selected["review_ids"], selected["selected_by"]),
                         (m_need["id"], [migrate_review["id"]], "lead"))
        self.assertEqual(selected["lease"]["id"], migrate["operation"]["lease"][0])
        self.assertEqual(self.select("migrate.json", migrate_review["id"])[0], selected)  # idempotent
        self.select("tune.json", tune_review["id"])

        for needs, skill, other, need in (("migrate.json", "mysql-migrate", "mysql-tune", m_need),
                                          ("tune.json", "mysql-tune", "mysql-migrate", t_need)):
            with self.subTest(skill):
                before = set(self.runs())
                code, out, err = self.call("--skill-needs", str(self.files / needs))
                self.assertEqual(code, 0, out + err)
                (run,) = set(self.runs()) - before
                stdin = self.stdin()
                self.assertIn(f"### 1. {skill} (optional)\n", stdin)
                self.assertIn(f"For task need {need['id'][7:19]}", stdin)
                self.assertNotIn(other, stdin)
                record = json.loads((run / "skills.json").read_text(encoding="utf-8"))
                (item,) = record["bundle"]["items"]
                self.assertEqual((item["skill"], item["need_id"], item["stage"], item["check"], item["origin"]),
                                 (skill, need["id"], None, None, "reviewed"))
                self.assertEqual([entry["need_id"] for entry in record["task_discovery"]], [need["id"]])
                planned = self.skills("plan", "--task", "t1", "--stage", "initial", "--skill-needs",
                                      str(self.files / needs), "--json")[1]
                self.assertEqual(json.loads(planned)["bundle"]["digest"], record["bundle"]["digest"])
        self.assertEqual(sorted(self.server.requests), MYSQL_QUERIES)  # every later lookup reused the fresh cache
        self.assertEqual(sorted(requests), MYSQL_QUERIES)  # and none was sent under the launch lock
        # An unchanged need with a fresh discovery is not searched or stored again by the next call, a second later.
        stored = sorted((self.state_dir / "skills" / "task-discovery").glob("*.json"))
        time.sleep(1.1)
        before = set(self.runs())
        code, out, err = self.call("--skill-needs", str(self.files / "tune.json"))
        self.assertEqual(code, 0, out + err)
        (run,) = set(self.runs()) - before
        again = json.loads((run / "skills.json").read_text(encoding="utf-8"))["task_discovery"]
        self.assertEqual(again, record["task_discovery"])
        self.assertEqual(sorted((self.state_dir / "skills" / "task-discovery").glob("*.json")), stored)
        # Without its needs file the call derives the task's own need, which has no selection: nothing is added.
        code, out, err = self.call()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("## Skill texts", self.stdin())

    def test_a_required_need_without_its_selection_refuses_and_another_version_is_another_need(self):
        self.mysql_upstream()
        self.start_task()
        v8 = self.find_task("--subtask", "migrate", "--tech", "database=mysql", "--constraint", "mysql 8.0",
                            "--required", out="v8.json")
        v57 = self.find_task("--subtask", "migrate", "--tech", "database=mysql", "--constraint", "mysql 5.7",
                             "--required", out="v57.json")
        self.assertNotEqual(v8["needs"][0]["id"], v57["needs"][0]["id"])
        self.assertTrue(v8["needs"][0]["required"])
        review = self.review_json("--github", "acme/db", "--path", "skills/mysql-migrate")[0]
        self.select("v8.json", review["id"])
        code, out, err = self.call("--skill-needs", str(self.files / "v57.json"))
        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.startswith("OAH_SKILL_MANDATORY_MISSING"), err)
        self.assertIn("nothing was launched", err)
        self.assertIn("oah skills select", err)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.runs(), [])
        planned = self.skills("plan", "--task", "t1", "--stage", "initial", "--skill-needs",
                              str(self.files / "v57.json"), expect=1)
        self.assertTrue(planned[2].startswith("OAH_SKILL_MANDATORY_MISSING"), planned[2])
        code, out, err = self.call("--skill-needs", str(self.files / "v8.json"))
        self.assertEqual(code, 0, out + err)
        self.assertIn("### 1. mysql-migrate (required)\n", self.stdin())
        # A selection belongs to the lease it was made under: after the task is started again it no longer applies.
        from oah.lease import Store

        Store(self.state_dir).release(task="t1", agent="claude", reason="restart the task", force=True)
        code, out, err = self.oah("start", "--task", "t1", "--agent", "claude", "--packet", ".oah/tasks/t1/PACKET.md",
                                  "--scope", "src/")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.call("--skill-needs", str(self.files / "v8.json"))
        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.startswith("OAH_SKILL_MANDATORY_MISSING"), err)

    def test_a_needs_file_from_before_a_packet_change_refuses_as_stale(self):
        self.start_task()
        self.find_task("--subtask", "migrate", "--tech", "database=other:acme-ledger", out="needs.json")
        git_commit(self.repo, {".oah/tasks/t1/PACKET.md": TEXT.replace("## Acceptance\n",
                                                                       "## Acceptance\n- every row is checked\n")},
                   "acceptance")
        code, out, err = self.call("--skill-needs", str(self.files / "needs.json"))
        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.startswith("OAH_SKILL_NEEDS_INVALID(stale)"), err)
        self.assertIn("nothing was launched", err)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.runs(), [])
        refused = self.select("needs.json", "sha256:" + "6" * 64, expect=1)[1]
        self.assertTrue(refused.startswith("OAH_SKILL_NEEDS_INVALID(stale)"), refused)

    def test_a_task_selection_changed_after_prepare_refuses_at_launch(self):
        from oah import call, call_cli
        from oah.secrets import NoKeychain

        self.mysql_upstream()
        self.start_task()
        self.find_task("--subtask", "migrate", "--tech", "database=mysql", out="needs.json")
        first = self.review_json("--github", "acme/db", "--path", "skills/mysql-migrate")[0]
        second = self.review_json("--github", "acme/db", "--path", "skills/mysql-tune")[0]
        self.select("needs.json", first["id"])
        tools = call_cli.intent_tools()
        with contextlib.chdir(self.repo):
            prepared = call.prepare(call.CallRequest(engine="claude", stage="initial", task="t1", tier="low",
                                                     no_rerank=True, skill_needs=str(self.files / "needs.json")),
                                    call_cli.load_settings(self.repo), root=self.repo, state_dir=self.state_dir,
                                    env=dict(os.environ), footer=tools.footer, has_footer=tools.has_footer,
                                    keychain=NoKeychain())
        self.select("needs.json", second["id"])
        with self.assertRaises(call.EngineError) as caught:
            with call.dispatch_guard(prepared):
                self.fail("the guard let a changed selection launch")
        self.assertIn("OAH_SKILL_NOT_DELIVERABLE(changed)", str(caught.exception))

    def test_a_native_offer_carries_the_lead_selection_and_records_the_discovery(self):
        from oah import init

        self.mysql_upstream()
        self.start_task()
        agents = self.home / ".claude" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "oah-low.md").write_bytes(init.agent_text("low", "haiku", "low"))
        report = self.find_task("--subtask", "migrate", "--tech", "database=mysql", out="needs.json")
        review = self.review_json("--github", "acme/db", "--path", "skills/mysql-migrate")[0]
        self.select("needs.json", review["id"])
        before = set(self.runs())
        self.record.unlink(missing_ok=True)
        code, out, err = self.oah("call", "claude", "--stage", "initial", "--task", "t1", "--tier", "low",
                                  "--launcher", "native", "--skill-needs", str(self.files / "needs.json"), "--json")
        self.assertEqual(code, 0, out + err)
        (run,) = set(self.runs()) - before
        prompt = (run / "prompt.md").read_text(encoding="utf-8")
        self.assertIn(f"For task need {report['needs'][0]['id'][7:19]}", prompt)
        self.assertIn(MIGRATE_BODY, prompt)
        skills = json.loads((run / "skills.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["need_id"] for entry in skills["task_discovery"]], [report["needs"][0]["id"]])
        self.assertEqual(skills["task_discovery"][0]["selection_ref"], self.select("needs.json", review["id"])[0]["id"])
        self.assertFalse(self.record.exists())  # an offer starts no engine

    def test_an_installed_skill_answers_a_local_need_without_any_request(self):
        folder = self.home / ".claude" / "skills" / "acme-ledger-guide"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(skill_md("acme-ledger-guide"), encoding="utf-8")
        self.start_task()
        report = self.find_task("--tech", "database=other:acme-ledger")
        self.assertEqual((report["requests"], self.server.requests), (0, []))
        (found,) = report["discovery"]
        self.assertEqual((found["outcome"], found["query_ids"]), ("local-only", []))
        self.assertEqual(found["gaps"], [{"code": "public-term-required", "need_id": report["needs"][0]["id"]}])
        self.assertEqual([item["skill"] for item in report["installed"]], ["acme-ledger-guide"])

    def test_workers_cannot_find_or_select_for_a_task(self):
        self.start_task()
        self.find_task("--subtask", "a", out="needs.json")
        with mock.patch.dict(os.environ, {"OAH_WORKER": "1"}):
            for args in (("find", "--task", "t1"), ("select", "--needs", str(self.files / "needs.json"),
                                                    "--review", "sha256:" + "7" * 64)):
                with self.subTest(args[0]):
                    code, _out, err = self.skills(*args, expect=1)
                    self.assertTrue(err.startswith("OAH_SKILLS_WORKER_REFUSED"), err)
        self.assertFalse((self.state_dir / "skills" / "task-selections").exists())

    def test_select_refuses_a_skill_that_is_not_a_stored_deliverable_review(self):
        self.start_task()
        self.find_task("--subtask", "a", out="needs.json")
        refused = self.select("needs.json", "sha256:" + "8" * 64, expect=1)[1]
        self.assertTrue(refused.startswith("OAH_SKILL_NEEDS_INVALID(malformed)"), refused)
        user_only = skill_md("manual", extra="disable-model-invocation: true\n")
        self.upstream("acme/manual", mit_repo({"skills/manual": user_only}))
        record = self.review_json("--github", "acme/manual", "--path", "skills/manual")[0]
        refused = self.select("needs.json", record["id"], expect=1)[1]
        self.assertTrue(refused.startswith("OAH_SKILL_NOT_DELIVERABLE(user-only)"), refused)
        self.assertFalse((self.state_dir / "skills" / "task-selections").exists())


class DesignBoundTaskTest(DeliveryTask, unittest.TestCase):
    """A task bound to an accepted design follows its coverage: a task selection never replaces acceptance, and a
    database decision changed in the coverage asks for the linked skill to be chosen again."""

    def build(self, parts, **options):
        started = super().build(parts, **options)
        config = (self.repo / ".oah/config.toml").read_text(encoding="utf-8") + '[skills.discovery]\nprovider = "none"\n'
        git_commit(self.repo, {".oah/config.toml": config}, "no external search in these tests")
        return started

    def find_task(self, *args: str, out: str = "needs.json") -> dict:
        path = self.work / out
        result = self.oah("skills", "find", "--task", "t1", *args, "--out", str(path), "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_select_refuses_for_a_design_bound_task(self):
        self.build_skills(PLAIN)
        report = self.find_task()
        self.assertEqual([(need["design"]["stage"], need["design"]["check"]) for need in report["needs"]],
                         [("data", "entities")])
        result = self.oah("skills", "select", "--needs", str(self.work / "needs.json"), "--review",
                          self.reviews["plain"]["id"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("OAH_SKILL_NEEDS_INVALID(design-bound)"), result.stderr)
        self.assertIn("accept", result.stderr)
        self.assertFalse((self.state_dir / "skills" / "task-selections").exists())

    def test_a_required_design_need_without_an_accepted_link_refuses(self):
        self.build_skills(PLAIN)
        self.find_task("--design-stage", "quality", "--check", "accessibility", "--required")
        result = self.call("claude", "initial", "--skill-needs", str(self.work / "needs.json"))
        self.assert_refused_before_any_run(result, "OAH_SKILL_MANDATORY_MISSING", planner=(
            "skills", "plan", "--task", "t1", "--stage", "initial", "--skill-needs", str(self.work / "needs.json")))
        self.assertIn("quality.accessibility", result.stderr)
        self.assertIn("accept", result.stderr)

    def test_a_changed_database_asks_to_reselect_the_linked_skill(self):
        from oah.discovery import providers, store

        query = "postgresql schema design"
        self.old_query = store.query_id(providers.PROVIDER, query, providers.PARSER)

        def mysql_link(record):
            return [{"op": "set-tech", "label": "database", "value": "mysql", "decision": None, "note": None},
                    link_op(record)]

        self.build_skills(PLAIN[:3] + (mysql_link,))
        report = self.find_task()
        (found,) = report["discovery"]
        self.assertEqual(found["outcome"], "not-searched")  # external search is off here; nothing was sent
        self.assertEqual(found["gaps"], [{"code": "reselect", "need_id": report["needs"][0]["id"]}])
        self.assertEqual([query["text"] for query in report["discovery"][0]["public"]], ["mysql schema design"])

    def reviewed(self, state: pathlib.Path, repo: str, path: str) -> dict:
        """Reviewed from a stored PostgreSQL search hit when the test names one, else straight from GitHub."""
        old = getattr(self, "old_query", None)
        if old is None:
            return super().reviewed(state, repo, path)
        from oah.design.sources import DEFAULTS
        from oah.discovery import providers, review, store
        from oah import skillpack

        hit = {"name": path.rsplit("/", 1)[1], "source": repo, "id": f"{repo}/{path.rsplit('/', 1)[1]}",
               "installs": 1}
        store.put_query(state, {"version": 1, "query_id": old, "provider": providers.PROVIDER,
                                "query": "postgresql schema design", "parser": providers.PARSER,
                                "checked_at": "2026-11-09T00:00:00+00:00", "outcome": "ok", "detail": "1 result(s)",
                                "count": 1, "hits": [hit], "origin": "template", "choice": None, "purpose": {}})
        record, _ = review.review(state, repo=repo, path=path, allow=list(DEFAULTS["license_allow"]),
                                  candidate={"provider": providers.PROVIDER, "query_id": old, "hit": hit, "seed": None},
                                  admitted=skillpack.explore(1))
        self.assertEqual((record["status"], record["license"]["eligibility"]), ("reviewed", "eligible"))
        return record


if __name__ == "__main__":
    unittest.main()
